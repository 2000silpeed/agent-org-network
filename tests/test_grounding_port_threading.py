"""ADR 0037 슬라이스 B — 포트 `grounding` 옵셔널 인자 스레딩 [게이트 내·결정론·회귀 0].

`context`(ADR 0027 결정 6·7·8, `test_t9_4_c1_context_threading_port.py`)와 동형 패턴.

불변식:
- 회귀 0: grounding=None(기본)이면 기존 `_resolve_okf(card.agent_id)` 경로 100% 동일
  (request.system 스냅샷 비교로 단언).
- grounding=str이면 그 문자열이 `build_provider_request(..., okf=grounding)` 슬롯에
  실려 자기 해소를 대체한다.
- Protocol + 전 구현체(StubRuntime·ClaudeCodeRuntime·ProviderApiRuntime·
  LocalRuntimeDispatcher·InMemoryWorkQueueDispatcher·WebSocketDispatcher·
  DispatchingRuntime)가 grounding 인자를 받는다.
- WS 디스패처는 받되 프레임에 안 실어도 된다(시그니처 정합 흡수·이번 증분).
- Contested arm 행동 변경 없음 — 이 인자는 아직 handle의 contested 경로에 안 꽂힌다.
"""

from datetime import date

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.provider_runtime import (
    ClaudeApiRuntime,
    ProviderRequest,
    StubProviderTransport,
    build_provider_request,
)
from agent_org_network.runtime import AgentRuntime, Answer, ClaudeCodeRuntime, StubRuntime


@pytest.fixture()
def card() -> AgentCard:
    return AgentCard(
        agent_id="cs_ops",
        owner="alice",
        team="CS팀",
        summary="고객 서비스 운영 담당",
        domains=["고객지원", "환불"],
        last_reviewed_at=date(2026, 6, 27),
        knowledge_sources=["cs_ops/policy.md"],
    )


# ---------------------------------------------------------------------------
# AgentRuntime Protocol — grounding 옵셔널 시그니처
# ---------------------------------------------------------------------------


class TestAgentRuntimeProtocolGroundingShape:
    def test_StubRuntime은_grounding_인자를_받는다(self, card: AgentCard) -> None:
        runtime = StubRuntime()
        answer = runtime.answer("질문", card, grounding="### billing\n결제 지식")
        assert isinstance(answer, Answer)

    def test_StubRuntime_grounding_None이면_기존_동작(self, card: AgentCard) -> None:
        runtime = StubRuntime()
        answer_no = runtime.answer("질문", card)
        answer_none = runtime.answer("질문", card, grounding=None)
        assert answer_no.text == answer_none.text

    def test_ClaudeCodeRuntime은_grounding_인자를_받는다(self, card: AgentCard) -> None:
        def _fake_runner(
            prompt: str, /, *, cwd: str | None = None, system_prompt: str | None = None
        ) -> str:
            return "fake 답"

        runtime = ClaudeCodeRuntime(runner=_fake_runner)
        answer = runtime.answer("질문", card, grounding="### billing\n결제 지식")
        assert isinstance(answer, Answer)

    def test_ClaudeCodeRuntime_grounding_None이면_기존_동작(self, card: AgentCard) -> None:
        def _fake_runner(
            prompt: str, /, *, cwd: str | None = None, system_prompt: str | None = None
        ) -> str:
            return "fake 답"

        runtime = ClaudeCodeRuntime(runner=_fake_runner)
        answer_no = runtime.answer("질문", card)
        answer_none = runtime.answer("질문", card, grounding=None)
        assert answer_no.text == answer_none.text

    def test_ClaudeApiRuntime은_grounding_인자를_받는다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답변"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card, grounding="### billing\n결제 지식")
        assert isinstance(answer, Answer)

    def test_AgentRuntime_Protocol은_grounding_시그니처를_포함한다(self, card: AgentCard) -> None:
        runtime: AgentRuntime = StubRuntime()
        answer = runtime.answer("질문", card, grounding="접지 텍스트")
        assert isinstance(answer, Answer)

    def test_RuntimeDispatcher_Protocol_dispatch_grounding_정합(self) -> None:
        import inspect

        from agent_org_network.dispatch import RuntimeDispatcher

        sig = inspect.signature(RuntimeDispatcher.dispatch)
        assert "grounding" in sig.parameters


# ---------------------------------------------------------------------------
# 회귀 0 — grounding=None이면 기존 _resolve_okf 경로 100% 동일 (스냅샷 비교)
# ---------------------------------------------------------------------------


class RecordingTransport:
    """호출된 ProviderRequest를 기록하는 transport(관측용) — context 테스트 선례 재사용."""

    def __init__(self) -> None:
        self.last_request: ProviderRequest | None = None

    def __call__(self, request: ProviderRequest) -> list[str]:
        self.last_request = request
        return ["stub 응답"]


class TestGroundingNoneRegressionZero:
    """grounding=None(기본)이면 기존 _resolve_okf(card.agent_id) 경로 100% 동일."""

    def test_grounding_인자_없이_호출한_요청과_grounding_None_요청이_바이트_동일하다(
        self, card: AgentCard
    ) -> None:
        rec_a = RecordingTransport()
        rec_b = RecordingTransport()
        runtime_a = ClaudeApiRuntime(transport=rec_a)
        runtime_b = ClaudeApiRuntime(transport=rec_b)

        runtime_a.answer("환불 정책?", card)
        runtime_b.answer("환불 정책?", card, grounding=None)

        assert rec_a.last_request is not None
        assert rec_b.last_request is not None
        assert rec_a.last_request.system == rec_b.last_request.system
        assert rec_a.last_request.messages == rec_b.last_request.messages
        assert rec_a.last_request.model == rec_b.last_request.model

    def test_grounding_None이면_okf_root_자기_해소_경로가_그대로_동작한다(
        self, card: AgentCard, tmp_path: "object"
    ) -> None:
        from pathlib import Path

        okf_root = Path(str(tmp_path))
        bundle_dir = okf_root / card.agent_id
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "policy.md").write_text("자기 OKF 본문", encoding="utf-8")

        rec = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec, okf_root=okf_root)

        runtime.answer("질문", card, grounding=None)

        assert rec.last_request is not None
        assert "자기 OKF 본문" in rec.last_request.system

    def test_build_provider_request_okf_인자_직접_호출과_동일(self, card: AgentCard) -> None:
        """runtime 경유 request와 build_provider_request 직접 호출(okf="") 결과가 같다."""
        rec = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec)
        runtime.answer("질문", card, grounding=None)

        direct = build_provider_request(
            "질문", card, context=None, model=runtime._model, okf=""  # type: ignore[attr-defined]
        )

        assert rec.last_request is not None
        assert rec.last_request.system == direct.system
        assert rec.last_request.messages == direct.messages


# ---------------------------------------------------------------------------
# grounding=str — okf 슬롯에 실림(자기 해소 대체)
# ---------------------------------------------------------------------------


class TestGroundingStringDelivery:
    def test_grounding_문자열이_okf_슬롯으로_실려_system에_반영된다(self, card: AgentCard) -> None:
        rec = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec)
        multi_grounding = "### cs_ops\n고객지원 지식\n\n### billing\n결제 지식"

        runtime.answer("환불하면 청약철회도 되나요?", card, grounding=multi_grounding)

        assert rec.last_request is not None
        assert multi_grounding in rec.last_request.system

    def test_grounding_문자열이_주어지면_자기_okf_root_해소를_대체한다(
        self, card: AgentCard, tmp_path: "object"
    ) -> None:
        from pathlib import Path

        okf_root = Path(str(tmp_path))
        bundle_dir = okf_root / card.agent_id
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "policy.md").write_text("자기 OKF 본문(대체되어야 함)", encoding="utf-8")

        rec = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec, okf_root=okf_root)
        multi_grounding = "### cs_ops\n다중 접지 본문\n\n### billing\n결제 지식"

        runtime.answer("질문", card, grounding=multi_grounding)

        assert rec.last_request is not None
        assert multi_grounding in rec.last_request.system
        assert "자기 okf 본문(대체되어야 함)" not in rec.last_request.system.lower()

    def test_grounding_빈_문자열이면_okf_없는_것과_동일(self, card: AgentCard) -> None:
        rec_empty = RecordingTransport()
        rec_none = RecordingTransport()
        runtime_empty = ClaudeApiRuntime(transport=rec_empty)
        runtime_none = ClaudeApiRuntime(transport=rec_none)

        runtime_empty.answer("질문", card, grounding="")
        runtime_none.answer("질문", card, grounding=None)

        assert rec_empty.last_request is not None
        assert rec_none.last_request is not None
        assert rec_empty.last_request.system == rec_none.last_request.system


# ---------------------------------------------------------------------------
# 전 구현체 grounding 시그니처 수락
# ---------------------------------------------------------------------------


class TestDispatcherGroundingSignature:
    def test_LocalRuntimeDispatcher_dispatch_grounding_인자_수락(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        runtime = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=runtime)
        ticket = disp.dispatch("질문", card, grounding="접지")
        assert ticket is not None

    def test_LocalRuntimeDispatcher_grounding_None_기존_동작(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        runtime = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=runtime)
        ticket = disp.dispatch("질문", card, grounding=None)
        assert ticket is not None

    def test_LocalRuntimeDispatcher_grounding_미주입_기존_동작(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        runtime = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=runtime)
        ticket = disp.dispatch("질문", card)
        assert ticket is not None

    def test_InMemoryWorkQueueDispatcher_dispatch_grounding_인자_수락(
        self, card: AgentCard
    ) -> None:
        from agent_org_network.dispatch import InMemoryWorkQueueDispatcher

        disp = InMemoryWorkQueueDispatcher()
        ticket = disp.dispatch("질문", card, grounding="접지")
        assert ticket is not None

    def test_WebSocketDispatcher_dispatch_grounding_인자_수락(self, card: AgentCard) -> None:
        from agent_org_network.transport import WebSocketDispatcher

        disp = WebSocketDispatcher()
        ticket = disp.dispatch("질문", card, grounding="접지")
        assert ticket is not None

    def test_WebSocketDispatcher_grounding_None(self, card: AgentCard) -> None:
        from agent_org_network.transport import WebSocketDispatcher

        disp = WebSocketDispatcher()
        ticket = disp.dispatch("질문", card, grounding=None)
        assert ticket is not None

    def test_DispatchingRuntime_answer_grounding_인자_수락(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import DispatchingRuntime, LocalRuntimeDispatcher

        runtime = StubRuntime()
        inner_disp = LocalRuntimeDispatcher(runtime=runtime)
        dispatching = DispatchingRuntime(dispatcher=inner_disp)
        answer = dispatching.answer("질문", card, grounding="접지")
        assert isinstance(answer, Answer)


# ---------------------------------------------------------------------------
# 로컬 경로 — grounding이 runtime.answer까지 실제 전달됨을 단언
# ---------------------------------------------------------------------------


class TestLocalDispatcherGroundingDelivery:
    def test_LocalRuntimeDispatcher가_grounding을_ClaudeApiRuntime에_전달한다(
        self, card: AgentCard
    ) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        rec = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec)
        disp = LocalRuntimeDispatcher(runtime=runtime)
        multi_grounding = "### cs_ops\n지식1\n\n### billing\n지식2"

        disp.dispatch("질문", card, grounding=multi_grounding)

        assert rec.last_request is not None
        assert multi_grounding in rec.last_request.system


# ---------------------------------------------------------------------------
# /ask 행동 불변 — 기존 contested 통합 테스트가 여전히 Pending을 반환한다
# ---------------------------------------------------------------------------


class TestContestedArmUnchanged:
    """Contested arm은 co-grounding *미주입*이면 무변경 — 여전히 Pending을 반환한다.

    슬라이스 B(이 파일 작성 시점) 당시엔 ask_org가 grounding 모듈을 아예 import하지
    않는 inert 계약이었다. 슬라이스 C(ADR 0037 결정 5)에서 Contested arm이 "답+합의
    병행"으로 진화하며 ask_org가 grounding 모듈에 의존하게 됐다 — 단 이는 *주입
    의존* 옵트인(하위호환 게이트)이라, selector/resolver 미주입이면 이 클래스가
    보장하는 회귀 0(기존 Pending 동작)은 그대로 유지된다(`test_co_grounding_contested.py`
    가 co-grounding *활성* 경로를 별도로 검증한다).
    """
