"""중앙 답변 경로 — KnowledgeStore 소비 전환 (Phase 12 S2, ADR 0033 결정 1).

`ProviderApiRuntime`(`ClaudeApiRuntime`/`CodexApiRuntime`)의 지식 접지 원천을
디스크(`read_okf_bundle`)에서 `KnowledgeStore`로 옮기는 최소 수술을 검증한다.
`AgentRuntime` 포트(`answer(question, card, context) -> Answer`) 시그니처는 무변경 —
`ProviderApiRuntime` 생성자에 `knowledge_store` 선택 주입만 늘어난다.

불변식:
  - 포트 무변경(ADR 0033 결정 1) — answer 시그니처·Answer 계약(text·sources·mode·
    snapshot_sha) 그대로.
  - 폴백 정책 — 스토어에 그 agent_id 본문이 없으면 기존 디스크(`okf_root`) 경로로
    폴백한다(회귀 0 — 스토어 미배선 기존 호출부는 100% 기존 동작).
  - stale 표식 — is_stale이면 답 자체는 그대로 나가되(라우팅/답변 차단 아님·미아
    없음 보존) 관측 seam(`last_knowledge_stale`)에 경고 플래그가 남는다.
  - 핵심 가치(오프라인 owner라도 답) — FakeKnowledgeStore + StubProviderTransport로
    담당자 PC/디스크 없이도 스토어 지식만으로 답이 나온다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc
from agent_org_network.presence import (
    InMemoryPresenceTracker,
    resolve_mode_with_presence,
)
from agent_org_network.provider_runtime import (
    ClaudeApiRuntime,
    ProviderRequest,
    StubProviderTransport,
    resolve_knowledge_text,
)

_T0 = datetime(2026, 7, 4, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def card() -> AgentCard:
    return AgentCard(
        agent_id="refund-bot",
        owner="alice",
        team="CS팀",
        summary="환불 담당",
        domains=["환불"],
        last_reviewed_at=date(2026, 7, 4),
        knowledge_sources=["refund-bot/policy.md"],
    )


def _content(
    agent_id: str = "refund-bot",
    documents: tuple[KnowledgeDoc, ...] = (KnowledgeDoc(path="policy.md", body="7일 이내 환불 가능"),),
    version: str = "sync-1",
    synced_at: datetime = _T0,
) -> KnowledgeBundleContent:
    return KnowledgeBundleContent(
        agent_id=agent_id, documents=documents, version=version, synced_at=synced_at
    )


class _RecordingTransport:
    """넘겨받은 ProviderRequest.system을 기록하는 결정론 transport(관측 seam)."""

    def __init__(self, chunks: tuple[str, ...] = ("stub 응답",)) -> None:
        self._chunks = chunks
        self.last_request: ProviderRequest | None = None

    def __call__(self, request: ProviderRequest) -> Iterable[str]:
        self.last_request = request
        return iter(self._chunks)


# ── resolve_knowledge_text — 순수 헬퍼(스토어 우선, 디스크 폴백) ──────────────


class TestResolveKnowledgeText:
    def test_스토어에_본문_있으면_그_본문을_쓴다(self) -> None:
        store = InMemoryKnowledgeStore()
        store.put(_content())

        text, stale = resolve_knowledge_text(
            store, None, "refund-bot", now=_T0, threshold_s=1800
        )

        assert "7일 이내 환불 가능" in text
        assert stale is False

    def test_스토어에_본문_없으면_디스크로_폴백한다(self, tmp_path: Path) -> None:
        bundle = tmp_path / "refund-bot"
        bundle.mkdir()
        (bundle / "policy.md").write_text("디스크 정책 본문")
        store = InMemoryKnowledgeStore()  # 비어 있음

        text, stale = resolve_knowledge_text(
            store, tmp_path, "refund-bot", now=_T0, threshold_s=1800
        )

        assert "디스크 정책 본문" in text
        assert stale is False

    def test_스토어_없고_okf_root도_없으면_빈_문자열(self) -> None:
        text, stale = resolve_knowledge_text(
            None, None, "refund-bot", now=_T0, threshold_s=1800
        )
        assert text == ""
        assert stale is False

    def test_스토어_None이면_디스크로_폴백한다(self, tmp_path: Path) -> None:
        bundle = tmp_path / "refund-bot"
        bundle.mkdir()
        (bundle / "policy.md").write_text("디스크 본문")

        text, stale = resolve_knowledge_text(
            None, tmp_path, "refund-bot", now=_T0, threshold_s=1800
        )

        assert "디스크 본문" in text
        assert stale is False

    def test_stale_지식이면_stale_플래그가_True다(self) -> None:
        store = InMemoryKnowledgeStore()
        store.put(_content(synced_at=_T0))

        later = _T0 + timedelta(seconds=3600)  # threshold(1800s) 초과
        text, stale = resolve_knowledge_text(
            store, None, "refund-bot", now=later, threshold_s=1800
        )

        assert "7일 이내 환불 가능" in text  # 답 자체는 차단되지 않는다
        assert stale is True

    def test_스토어에_다른_agent_id만_있으면_디스크로_폴백한다(self, tmp_path: Path) -> None:
        bundle = tmp_path / "refund-bot"
        bundle.mkdir()
        (bundle / "policy.md").write_text("디스크 본문")
        store = InMemoryKnowledgeStore()
        store.put(_content(agent_id="other-bot"))

        text, stale = resolve_knowledge_text(
            store, tmp_path, "refund-bot", now=_T0, threshold_s=1800
        )

        assert "디스크 본문" in text
        assert stale is False


# ── ProviderApiRuntime — knowledge_store 주입 + 소비 전환 ────────────────────


class TestProviderApiRuntimeKnowledgeStore:
    def test_knowledge_store_미주입시_기존_동작_100프로_보존(self, card: AgentCard) -> None:
        """회귀 0 — knowledge_store 인자를 아예 안 주면 기존 okf_root(None) 동작 그대로."""
        transport = _RecordingTransport()
        runtime = ClaudeApiRuntime(transport=transport)

        answer = runtime.answer("환불 언제 되나요?", card)

        assert answer.text == "stub 응답"
        assert transport.last_request is not None
        assert "지식 베이스" not in transport.last_request.system

    def test_knowledge_store에_본문_있으면_system에_접지된다(self, card: AgentCard) -> None:
        store = InMemoryKnowledgeStore()
        store.put(_content())
        transport = _RecordingTransport()
        runtime = ClaudeApiRuntime(transport=transport, knowledge_store=store)

        runtime.answer("환불 언제 되나요?", card)

        assert transport.last_request is not None
        assert "7일 이내 환불 가능" in transport.last_request.system

    def test_스토어_비어있으면_okf_root_디스크로_폴백한다(self, card: AgentCard, tmp_path: Path) -> None:
        bundle = tmp_path / "refund-bot"
        bundle.mkdir()
        (bundle / "policy.md").write_text("디스크 폴백 본문")
        store = InMemoryKnowledgeStore()  # 비어 있음
        transport = _RecordingTransport()
        runtime = ClaudeApiRuntime(transport=transport, knowledge_store=store, okf_root=tmp_path)

        runtime.answer("환불 언제 되나요?", card)

        assert transport.last_request is not None
        assert "디스크 폴백 본문" in transport.last_request.system

    def test_Answer_계약은_그대로다(self, card: AgentCard) -> None:
        store = InMemoryKnowledgeStore()
        store.put(_content())
        runtime = ClaudeApiRuntime(
            transport=StubProviderTransport(chunks=["답변"]), knowledge_store=store
        )

        answer = runtime.answer("질문", card)

        assert answer.text == "답변"
        assert answer.mode == "full"
        assert answer.sources == tuple(card.knowledge_sources)
        assert answer.snapshot_sha is None

    def test_answer_stream도_스토어를_소비한다(self, card: AgentCard) -> None:
        store = InMemoryKnowledgeStore()
        store.put(_content())
        transport = _RecordingTransport(chunks=("델타1", "델타2"))
        runtime = ClaudeApiRuntime(transport=transport, knowledge_store=store)

        deltas = list(runtime.answer_stream("질문", card))

        assert [d.text_delta for d in deltas] == ["델타1", "델타2"]
        assert transport.last_request is not None
        assert "7일 이내 환불 가능" in transport.last_request.system

    def test_stale_지식으로_답해도_답이_나온다(self, card: AgentCard) -> None:
        """stale은 표식일 뿐 차단이 아니다 — 미아 없음 보존."""
        store = InMemoryKnowledgeStore()
        store.put(_content(synced_at=_T0))
        clock_calls = {"now": _T0 + timedelta(hours=2)}
        runtime = ClaudeApiRuntime(
            transport=StubProviderTransport(chunks=["답변"]),
            knowledge_store=store,
            clock=lambda: clock_calls["now"],
        )

        answer = runtime.answer("질문", card)

        assert answer.text == "답변"  # 답 차단 안 됨
        assert runtime.last_knowledge_stale is True  # 관측 seam에 경고 표식

    def test_신선한_지식은_stale_플래그가_False다(self, card: AgentCard) -> None:
        store = InMemoryKnowledgeStore()
        store.put(_content(synced_at=_T0))
        runtime = ClaudeApiRuntime(
            transport=StubProviderTransport(chunks=["답변"]),
            knowledge_store=store,
            clock=lambda: _T0 + timedelta(seconds=10),
        )

        runtime.answer("질문", card)

        assert runtime.last_knowledge_stale is False


# ── 핵심 결정론 단언 — 담당자(owner) 오프라인이어도 답이 나온다 ───────────────


class TestOfflineOwnerStillAnswers:
    """FakeKnowledgeStore + StubRuntime + PresenceTracker(offline) 종단 결정론.

    담당자 워커(PC)가 꺼져 있어(디스크 okf_root 접근 불가 가정) 오프라인이어도,
    중앙 런타임이 KnowledgeStore에 동기화된 지식으로 답을 생성한다(ADR 0033 결정 1
    핵심 가치 — 가용성). 디스크 okf_root는 아예 주지 않아(None) "owner PC 없이도
    답 나옴"을 구조적으로 보장한다.
    """

    def test_오프라인_담당자도_스토어_지식으로_답이_나온다(self, card: AgentCard) -> None:
        store = InMemoryKnowledgeStore()
        store.put(_content(documents=(KnowledgeDoc(path="policy.md", body="환불 정책 본문"),)))
        presence = InMemoryPresenceTracker()
        # 프레즌스는 owner(담당자) 키로 기록된다(크로스머신 시연 실결함 4호 정정).
        presence.observe_disconnect(card.owner, at=_T0)  # 담당자 워커 연결 끊김

        assert presence.status(card.owner) == "offline"

        transport = _RecordingTransport()
        # okf_root=None → 디스크 경로 완전 차단(owner PC 부재 시뮬레이션).
        runtime = ClaudeApiRuntime(transport=transport, knowledge_store=store, okf_root=None)

        answer = runtime.answer("환불 언제 되나요?", card)

        assert answer.text == "stub 응답"
        assert transport.last_request is not None
        assert "환불 정책 본문" in transport.last_request.system

    def test_오프라인이면_resolve_mode_with_presence가_사후교정_플래그를_낸다(
        self, card: AgentCard
    ) -> None:
        """오프라인 자동 발신 시 S4 사후교정 플래그가 답변 경로에 전달되는 배선.

        플래그 *소비*(정정 대상 적재 등)는 S5 몫 — 여기선 플래그가 답변 경로까지
        전달되는 배선만 결정론으로 확인한다.
        """
        presence = InMemoryPresenceTracker()
        # 프레즌스는 owner(담당자) 키로 기록된다(크로스머신 시연 실결함 4호 정정).
        presence.observe_disconnect(card.owner, at=_T0)
        status = presence.status(card.owner)

        mode, needs_correction_review = resolve_mode_with_presence(
            requires_approval=False,
            hitl_on=False,
            presence_status=status,
            current_mode="full",
            return_flag=True,
        )

        assert mode == "full"
        assert needs_correction_review is True  # 오프라인 자동발신 → 사후교정 대상 표식


# ── 라우팅 회귀 0 — 기존 provider_runtime 계약은 그대로다 ────────────────────


class TestNoRoutingRegression:
    def test_AgentRuntime_포트_시그니처_무변경(self, card: AgentCard) -> None:
        runtime = ClaudeApiRuntime(transport=StubProviderTransport(chunks=["답"]))
        answer = runtime.answer("질문", card, context=None)
        assert answer.text == "답"

    def test_context_인자는_여전히_받되_system에_안_실린다(self, card: AgentCard) -> None:
        store = InMemoryKnowledgeStore()
        store.put(_content())
        transport = _RecordingTransport()
        runtime = ClaudeApiRuntime(transport=transport, knowledge_store=store)

        runtime.answer("질문", card, context="이전 대화 맥락")

        assert transport.last_request is not None
        assert "이전 대화 맥락" not in transport.last_request.system
        assert transport.last_request.messages[0]["content"] == "이전 대화 맥락"
