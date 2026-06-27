"""Codex(OpenAI) 공급자 런타임 어댑터 — 대칭 슬라이스 (ADR 0027 결정 1·11)

슬라이스 1(게이트 내·결정론):
- CodexApiRuntime이 AgentRuntime 포트를 만족한다
- StubProviderTransport 주입으로 실 네트워크·OAuth·SDK 0
- ClaudeApiRuntime 대칭: 같은 파이프라인(build_provider_request → transport → assemble_stream → map_response_to_answer)
- build_provider_request에 model 키워드 파라미터 추가 — 기존 테스트 무회귀

A(ii) OKF 접지(게이트 내·결정론):
- read_okf_bundle: tmp_path 기반 파일 I/O, stdlib만, 네트워크·SDK 0
- build_provider_request okf 키워드: okf 있으면 system에 OKF 섹션 추가, 없으면 기존과 동일(무회귀)
- CodexApiRuntime·ClaudeApiRuntime okf_root 주입: 센티넬 포함 여부로 접지 검증
- okf_root 없으면 기존 동작 그대로(무회귀)

불변식:
- Answer 계약 보존: text·sources·mode·snapshot_sha 필드만·새 필드 없음
- 노출 불변식 유지 (공급자 특권 없음 — claude 특권 없음)
- Authority 중앙 — 런타임은 답 생성이지 권한 선언 아님
- 공급자 중립: 코어 무의존 (openai SDK 0)

게이트 밖: 실 OAuth·실 OpenAI API·openai SDK·~/.codex/auth.json = 슬라이스 2
"""

from datetime import date
from pathlib import Path

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.provider_runtime import (
    ClaudeApiRuntime,
    CodexApiRuntime,
    ProviderRequest,
    StubProviderTransport,
    build_provider_request,
    read_okf_bundle,
)
from agent_org_network.runtime import AgentRuntime, Answer


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture()
def card() -> AgentCard:
    return AgentCard(
        agent_id="eng_ops",
        owner="daniel",
        team="Engineering팀",
        summary="엔지니어링 운영 담당",
        domains=["배포", "인프라"],
        last_reviewed_at=date(2026, 6, 27),
        knowledge_sources=["eng/runbook.md", "eng/oncall.md"],
    )


@pytest.fixture()
def minimal_card() -> AgentCard:
    return AgentCard(
        agent_id="eng-minimal",
        owner="eve",
        team="QA팀",
        summary="QA 담당",
        domains=["테스트"],
        last_reviewed_at=date(2026, 6, 27),
    )


# ---------------------------------------------------------------------------
# CodexApiRuntime — AgentRuntime 포트 만족
# ---------------------------------------------------------------------------


class TestCodexApiRuntimePortSatisfaction:
    def test_CodexApiRuntime은_AgentRuntime_포트를_만족한다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["codex 답변"])
        runtime: AgentRuntime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("배포 절차가 어떻게 되나요?", card)
        assert isinstance(answer, Answer)

    def test_AgentRuntime_포트_구조적_타이핑_확인(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = CodexApiRuntime(transport=transport)
        assert callable(getattr(runtime, "answer", None))


# ---------------------------------------------------------------------------
# CodexApiRuntime — Answer 계약 보존 (ClaudeApiRuntime 대칭)
# ---------------------------------------------------------------------------


class TestCodexApiRuntimeAnswer:
    def test_stub_transport_주입으로_answer가_Answer를_반환한다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["codex 테스트 답변입니다."])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("배포 절차?", card)
        assert isinstance(answer, Answer)

    def test_answer_text에_transport_청크_조립이_반영된다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["배포는 ", "CI/CD ", "파이프라인입니다."])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("배포 절차?", card)
        assert answer.text == "배포는 CI/CD 파이프라인입니다."

    def test_answer_sources에_카드_knowledge_sources가_실린다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.sources == tuple(card.knowledge_sources)

    def test_answer_mode는_full이다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.mode == "full"

    def test_answer_snapshot_sha는_None이다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.snapshot_sha is None

    def test_answer는_새_필드를_만들지_않는다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert set(answer.__dataclass_fields__) == {"text", "sources", "mode", "snapshot_sha"}

    def test_knowledge_sources_없는_카드는_빈_sources(self, minimal_card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답변"])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("테스트 절차?", minimal_card)
        assert answer.sources == ()

    def test_transport는_실_네트워크_SDK_없이_동작한다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["결정론 codex 응답"])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.text == "결정론 codex 응답"

    def test_answer_sources는_항상_tuple이다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert isinstance(answer.sources, tuple)

    def test_answer_mode는_AnswerMode_리터럴이다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.mode in ("draft_only", "full", "backup")

    def test_context_파라미터를_받는다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["맥락 포함 답변"])
        runtime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("질문", card, context="이전 대화 맥락")
        assert isinstance(answer, Answer)
        assert answer.text == "맥락 포함 답변"


# ---------------------------------------------------------------------------
# CodexApiRuntime — 기본 모델 값 (ADR 0027 결정 10)
# ---------------------------------------------------------------------------


class TestCodexApiRuntimeDefaultModel:
    def test_codex_기본_모델이_gpt_5_5이다(self, card: AgentCard) -> None:
        """CodexApiRuntime이 build_provider_request에 gpt-5.5를 모델로 넘긴다(실 시연 검증값)."""
        captured_requests: list[ProviderRequest] = []

        class CapturingTransport:
            def __call__(self, request: ProviderRequest):  # type: ignore[override]
                captured_requests.append(request)
                return iter(["답"])

        runtime = CodexApiRuntime(transport=CapturingTransport())
        runtime.answer("질문", card)
        assert len(captured_requests) == 1
        assert captured_requests[0].model == "gpt-5.5"


# ---------------------------------------------------------------------------
# build_provider_request — model 키워드 파라미터 (무회귀)
# ---------------------------------------------------------------------------


class TestBuildProviderRequestModelParam:
    def test_model_파라미터를_ProviderRequest_model에_싣는다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card, model="gpt-5.5")
        assert req.model == "gpt-5.5"

    def test_model_파라미터로_claude_모델_지정(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card, model="claude-opus-4-8")
        assert req.model == "claude-opus-4-8"

    def test_model_파라미터로_임의_모델_지정(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card, model="some-future-model-v1")
        assert req.model == "some-future-model-v1"

    def test_model_기본값은_placeholder(self, card: AgentCard) -> None:
        """기존 테스트 무회귀: 기본값 시그니처는 유지된다."""
        req = build_provider_request("질문", card)
        assert req.model != ""  # 비어있지 않음 — 기존 단언

    def test_model_파라미터는_context와_함께_사용_가능(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card, context="맥락", model="gpt-5.5")
        assert req.model == "gpt-5.5"
        assert isinstance(req.model, str)


# ---------------------------------------------------------------------------
# 공급자 대칭 — ClaudeApiRuntime vs CodexApiRuntime (같은 파이프라인)
# ---------------------------------------------------------------------------


class TestProviderSymmetry:
    def test_claude와_codex는_같은_Answer_형태를_반환한다(self, card: AgentCard) -> None:
        stub = StubProviderTransport(chunks=["동일 답변"])
        claude_runtime = ClaudeApiRuntime(transport=stub)
        codex_runtime = CodexApiRuntime(transport=stub)

        claude_answer = claude_runtime.answer("질문", card)
        codex_answer = codex_runtime.answer("질문", card)

        assert type(claude_answer) is type(codex_answer)
        assert set(claude_answer.__dataclass_fields__) == set(codex_answer.__dataclass_fields__)
        assert claude_answer.mode == codex_answer.mode
        assert claude_answer.snapshot_sha == codex_answer.snapshot_sha

    def test_공급자_중립_같은_청크는_같은_text(self, card: AgentCard) -> None:
        chunks = ["공급자", " 중립", " 답변"]
        claude_answer = ClaudeApiRuntime(transport=StubProviderTransport(chunks=chunks)).answer(
            "질문", card
        )
        codex_answer = CodexApiRuntime(transport=StubProviderTransport(chunks=chunks)).answer(
            "질문", card
        )
        assert claude_answer.text == codex_answer.text

    def test_둘_다_AgentRuntime_포트_만족(self, card: AgentCard) -> None:
        def check_port(runtime: AgentRuntime) -> None:
            answer = runtime.answer("질문", card)
            assert isinstance(answer, Answer)

        check_port(ClaudeApiRuntime(transport=StubProviderTransport(chunks=["답"])))
        check_port(CodexApiRuntime(transport=StubProviderTransport(chunks=["답"])))


# ---------------------------------------------------------------------------
# A(ii) OKF 접지 — read_okf_bundle (순수 헬퍼, stdlib·IO만·SDK 0)
# ---------------------------------------------------------------------------


class TestReadOkfBundle:
    def test_두_md_파일을_파일명_정렬로_연결한다(self, tmp_path: Path) -> None:
        """okf_root/{agent_id}/a.md · b.md → 파일명 정렬(a 먼저) 연결 반환."""
        bundle = tmp_path / "cs_ops"
        bundle.mkdir()
        (bundle / "b.md").write_text("환불 규정 B")
        (bundle / "a.md").write_text("환불 규정 A")

        result = read_okf_bundle(tmp_path, "cs_ops")

        assert "### a.md" in result
        assert "환불 규정 A" in result
        assert "### b.md" in result
        assert "환불 규정 B" in result
        # 파일명 정렬: a.md 섹션이 b.md 섹션보다 먼저 나와야 한다
        assert result.index("### a.md") < result.index("### b.md")

    def test_okf_root가_None이면_빈_문자열(self) -> None:
        result = read_okf_bundle(None, "cs_ops")
        assert result == ""

    def test_번들_디렉터리가_없으면_빈_문자열(self, tmp_path: Path) -> None:
        result = read_okf_bundle(tmp_path, "존재하지않는_에이전트")
        assert result == ""

    def test_md_파일이_없으면_빈_문자열(self, tmp_path: Path) -> None:
        bundle = tmp_path / "cs_ops"
        bundle.mkdir()
        (bundle / "readme.txt").write_text("텍스트 파일")
        result = read_okf_bundle(tmp_path, "cs_ops")
        assert result == ""

    def test_파일_내용이_헤더와_함께_반환된다(self, tmp_path: Path) -> None:
        bundle = tmp_path / "refund_agent"
        bundle.mkdir()
        (bundle / "policy.md").write_text("환불 7일 이내")
        result = read_okf_bundle(tmp_path, "refund_agent")
        assert "### policy.md" in result
        assert "환불 7일 이내" in result

    def test_Path_객체를_okf_root로_받는다(self, tmp_path: Path) -> None:
        bundle = tmp_path / "eng_ops"
        bundle.mkdir()
        (bundle / "runbook.md").write_text("배포 절차")
        result = read_okf_bundle(tmp_path, "eng_ops")
        assert "배포 절차" in result

    def test_문자열_okf_root도_허용한다(self, tmp_path: Path) -> None:
        bundle = tmp_path / "eng_ops"
        bundle.mkdir()
        (bundle / "runbook.md").write_text("배포 절차")
        result = read_okf_bundle(str(tmp_path), "eng_ops")
        assert "배포 절차" in result

    def test_총_길이_100000자_초과시_자르고_표식_추가(self, tmp_path: Path) -> None:
        bundle = tmp_path / "big_agent"
        bundle.mkdir()
        # 100001자 넘는 내용 생성
        (bundle / "huge.md").write_text("x" * 110_000)
        result = read_okf_bundle(tmp_path, "big_agent")
        assert len(result) <= 100_000 + len("\n…(생략)")
        assert result.endswith("\n…(생략)")


# ---------------------------------------------------------------------------
# A(ii) OKF 접지 — build_provider_request okf 키워드 (무회귀 + 접지)
# ---------------------------------------------------------------------------


class TestBuildProviderRequestOkf:
    def test_okf_있으면_system에_OKF_헤더와_내용_포함(self, card: AgentCard) -> None:
        req = build_provider_request("환불?", card, okf="환불은 7일 이내 가능")
        assert "지식 베이스(OKF)" in req.system
        assert "환불은 7일 이내 가능" in req.system

    def test_okf_미지정_기본이면_system에_OKF_헤더_없음(self, card: AgentCard) -> None:
        """무회귀: okf 기본값(빈 문자열)이면 기존 system 프롬프트와 동일."""
        req_default = build_provider_request("환불?", card)
        req_empty = build_provider_request("환불?", card, okf="")
        assert "지식 베이스(OKF)" not in req_default.system
        assert "지식 베이스(OKF)" not in req_empty.system
        assert req_default.system == req_empty.system

    def test_okf_있어도_기존_knowledge_sources_라벨_줄_유지(self, card: AgentCard) -> None:
        req = build_provider_request("환불?", card, okf="내용")
        for src in card.knowledge_sources:
            assert src in req.system

    def test_okf_있어도_Answer_계약_무변경(self, card: AgentCard) -> None:
        """OKF가 주입돼도 ProviderRequest 구조는 동일(model·system·messages 3필드)."""
        req = build_provider_request("환불?", card, okf="내용")
        assert isinstance(req, ProviderRequest)
        assert hasattr(req, "model")
        assert hasattr(req, "system")
        assert hasattr(req, "messages")

    def test_okf_지시문에_근거_제한_포함(self, card: AgentCard) -> None:
        req = build_provider_request("환불?", card, okf="환불 규정 내용")
        assert "여기에 없으면 모른다고 말하라" in req.system


# ---------------------------------------------------------------------------
# A(ii) OKF 접지 — CodexApiRuntime okf_root 주입 (센티넬 검증)
# ---------------------------------------------------------------------------


class CapturingTransport:
    """마지막으로 받은 ProviderRequest를 캡처하는 결정론 transport (SDK·네트워크 0)."""

    def __init__(self, reply: str = "stub 응답") -> None:
        self.last_request: ProviderRequest | None = None
        self._reply = reply

    def __call__(self, request: ProviderRequest) -> list[str]:
        self.last_request = request
        return [self._reply]


class TestCodexApiRuntimeOkfGrounding:
    def test_okf_root_주입시_센티넬이_system에_포함된다(
        self, tmp_path: Path, card: AgentCard
    ) -> None:
        bundle = tmp_path / card.agent_id
        bundle.mkdir()
        (bundle / "refund.md").write_text("SENTINEL_환불_7일")

        transport = CapturingTransport()
        runtime = CodexApiRuntime(transport=transport, okf_root=tmp_path)
        runtime.answer("환불 규정?", card)

        assert transport.last_request is not None
        assert "SENTINEL_환불_7일" in transport.last_request.system

    def test_okf_root_없으면_기존_동작_유지(self, card: AgentCard) -> None:
        transport = CapturingTransport()
        runtime = CodexApiRuntime(transport=transport)
        runtime.answer("환불 규정?", card)

        assert transport.last_request is not None
        assert "지식 베이스(OKF)" not in transport.last_request.system

    def test_번들_디렉터리_없으면_okf_없이_동작(
        self, tmp_path: Path, card: AgentCard
    ) -> None:
        transport = CapturingTransport()
        runtime = CodexApiRuntime(transport=transport, okf_root=tmp_path)
        runtime.answer("환불 규정?", card)

        assert transport.last_request is not None
        assert "지식 베이스(OKF)" not in transport.last_request.system


# ---------------------------------------------------------------------------
# A(ii) OKF 접지 — ClaudeApiRuntime okf_root 주입 (대칭 동일 테스트)
# ---------------------------------------------------------------------------


class TestClaudeApiRuntimeOkfGrounding:
    def test_okf_root_주입시_센티넬이_system에_포함된다(
        self, tmp_path: Path, card: AgentCard
    ) -> None:
        bundle = tmp_path / card.agent_id
        bundle.mkdir()
        (bundle / "refund.md").write_text("SENTINEL_환불_7일")

        transport = CapturingTransport()
        runtime = ClaudeApiRuntime(transport=transport, okf_root=tmp_path)
        runtime.answer("환불 규정?", card)

        assert transport.last_request is not None
        assert "SENTINEL_환불_7일" in transport.last_request.system

    def test_okf_root_없으면_기존_동작_유지(self, card: AgentCard) -> None:
        transport = CapturingTransport()
        runtime = ClaudeApiRuntime(transport=transport)
        runtime.answer("환불 규정?", card)

        assert transport.last_request is not None
        assert "지식 베이스(OKF)" not in transport.last_request.system

    def test_번들_디렉터리_없으면_okf_없이_동작(
        self, tmp_path: Path, card: AgentCard
    ) -> None:
        transport = CapturingTransport()
        runtime = ClaudeApiRuntime(transport=transport, okf_root=tmp_path)
        runtime.answer("환불 규정?", card)

        assert transport.last_request is not None
        assert "지식 베이스(OKF)" not in transport.last_request.system
