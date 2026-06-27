"""Codex(OpenAI) 공급자 런타임 어댑터 — 대칭 슬라이스 (ADR 0027 결정 1·11)

슬라이스 1(게이트 내·결정론):
- CodexApiRuntime이 AgentRuntime 포트를 만족한다
- StubProviderTransport 주입으로 실 네트워크·OAuth·SDK 0
- ClaudeApiRuntime 대칭: 같은 파이프라인(build_provider_request → transport → assemble_stream → map_response_to_answer)
- build_provider_request에 model 키워드 파라미터 추가 — 기존 테스트 무회귀

불변식:
- Answer 계약 보존: text·sources·mode·snapshot_sha 필드만·새 필드 없음
- 노출 불변식 유지 (공급자 특권 없음 — claude 특권 없음)
- Authority 중앙 — 런타임은 답 생성이지 권한 선언 아님
- 공급자 중립: 코어 무의존 (openai SDK 0)

게이트 밖: 실 OAuth·실 OpenAI API·openai SDK·~/.codex/auth.json = 슬라이스 2
"""

from datetime import date

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.provider_runtime import (
    ClaudeApiRuntime,
    CodexApiRuntime,
    ProviderRequest,
    StubProviderTransport,
    build_provider_request,
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
    def test_codex_기본_모델이_gpt_5_2_codex이다(self, card: AgentCard) -> None:
        """CodexApiRuntime이 build_provider_request에 gpt-5.2-codex를 모델로 넘긴다."""
        captured_requests: list[ProviderRequest] = []

        class CapturingTransport:
            def __call__(self, request: ProviderRequest):  # type: ignore[override]
                captured_requests.append(request)
                return iter(["답"])

        runtime = CodexApiRuntime(transport=CapturingTransport())
        runtime.answer("질문", card)
        assert len(captured_requests) == 1
        assert captured_requests[0].model == "gpt-5.2-codex"


# ---------------------------------------------------------------------------
# build_provider_request — model 키워드 파라미터 (무회귀)
# ---------------------------------------------------------------------------


class TestBuildProviderRequestModelParam:
    def test_model_파라미터를_ProviderRequest_model에_싣는다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card, model="gpt-5.2-codex")
        assert req.model == "gpt-5.2-codex"

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
        req = build_provider_request("질문", card, context="맥락", model="gpt-5.2-codex")
        assert req.model == "gpt-5.2-codex"
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
