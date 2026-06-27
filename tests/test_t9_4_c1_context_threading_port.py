"""T9.4(c-1) — AgentRuntime 포트 옵셔널 context 진화 + 5 dispatcher context 흡수 [게이트 내·결정론]

ADR 0027 결정 6·7·8.

불변식:
- 하위호환: context=None이면 기존 동작 동일(기존 50 provider 테스트 회귀 0).
- context 도달: ClaudeApiRuntime + StubProviderTransport로 messages에 context가 실리는지 단언.
- StubRuntime 관측 seam: context가 런타임까지 도달했음을 last_context 로 단언.
- ClaudeCodeRuntime: context 받되 이번 증분에서 무시(기존 동작 보존).
- dispatcher 5곳 Protocol 정합: dispatch(question, card, context=None) 시그니처.
- 로컬 경로만 즉시 전달(LocalRuntimeDispatcher → runtime.answer(context=)).
- WS 계열 3종은 context 흡수만·미전파(T9.7 후속).
- Answer 계약 보존: text·sources·mode·snapshot_sha.
- Authority 중앙 불변.
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


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


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
# AgentRuntime Protocol — 포트 옵셔널 context 시그니처 검증
# ---------------------------------------------------------------------------


class TestAgentRuntimeProtocolShape:
    """AgentRuntime Protocol이 context: str | None = None 을 받는지 구조 검증."""

    def test_StubRuntime은_context_인자를_받는다(self, card: AgentCard) -> None:
        runtime = StubRuntime()
        answer = runtime.answer("질문", card, context="과거 맥락")
        assert isinstance(answer, Answer)

    def test_StubRuntime_context_None이면_기존_동작(self, card: AgentCard) -> None:
        runtime = StubRuntime()
        answer_no_ctx = runtime.answer("질문", card)
        answer_ctx_none = runtime.answer("질문", card, context=None)
        assert answer_no_ctx.text == answer_ctx_none.text

    def test_StubRuntime은_context_받되_답에_싣지_않는다(self, card: AgentCard) -> None:
        """StubRuntime은 canned 답 결정론 보존 — context 여부에 무관하게 같은 답."""
        runtime = StubRuntime()
        answer_with = runtime.answer("질문", card, context="이전 대화")
        answer_without = runtime.answer("질문", card)
        assert answer_with.text == answer_without.text
        assert answer_with.sources == answer_without.sources
        assert answer_with.mode == answer_without.mode

    def test_StubRuntime_last_context_관측_seam(self, card: AgentCard) -> None:
        """StubRuntime이 context를 last_context 속성으로 기록한다(관측 seam)."""
        runtime = StubRuntime()
        runtime.answer("질문", card, context="관측할 맥락")
        assert runtime.last_context == "관측할 맥락"

    def test_StubRuntime_last_context_None이면_None(self, card: AgentCard) -> None:
        runtime = StubRuntime()
        runtime.answer("질문", card)
        assert runtime.last_context is None

    def test_ClaudeCodeRuntime은_context_인자를_받는다(self, card: AgentCard) -> None:
        """ClaudeCodeRuntime이 context 파라미터를 수락한다(이번 증분에서 무시)."""

        def _fake_runner(prompt: str, /, *, cwd: str | None = None) -> str:
            return "fake 답"

        runtime = ClaudeCodeRuntime(runner=_fake_runner)
        answer = runtime.answer("질문", card, context="과거 맥락")
        assert isinstance(answer, Answer)

    def test_ClaudeCodeRuntime_context_None이면_기존_동작(self, card: AgentCard) -> None:
        def _fake_runner(prompt: str, /, *, cwd: str | None = None) -> str:
            return "fake 답"

        runtime = ClaudeCodeRuntime(runner=_fake_runner)
        answer_no = runtime.answer("질문", card)
        answer_none = runtime.answer("질문", card, context=None)
        assert answer_no.text == answer_none.text

    def test_ClaudeApiRuntime은_context_인자를_받는다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답변"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card, context="과거 맥락")
        assert isinstance(answer, Answer)

    def test_AgentRuntime_Protocol은_context_시그니처를_포함한다(self, card: AgentCard) -> None:
        """Protocol 구조적 타이핑: StubRuntime이 context 포함 시그니처를 만족한다."""
        runtime: AgentRuntime = StubRuntime()
        answer = runtime.answer("질문", card, context="맥락 테스트")
        assert isinstance(answer, Answer)


# ---------------------------------------------------------------------------
# ClaudeApiRuntime — context가 messages에 실제 도달·소비됨을 단언
# ---------------------------------------------------------------------------


class RecordingTransport:
    """호출된 ProviderRequest를 기록하는 transport(관측용)."""

    def __init__(self) -> None:
        self.last_request: ProviderRequest | None = None

    def __call__(self, request: ProviderRequest) -> list[str]:
        self.last_request = request
        return ["stub 응답"]


class TestClaudeApiRuntimeContextConsumption:
    """ClaudeApiRuntime.answer(context=)가 build_provider_request(context=)로 실제 소비됨."""

    def test_context가_messages에_선행_user_메시지로_실린다(self, card: AgentCard) -> None:
        """context가 None 아닐 때 messages[0]에 context가 들어간다."""
        rec_transport = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec_transport)
        ctx = "User: 첫 질문\nAssistant: 첫 답"

        runtime.answer("두 번째 질문", card, context=ctx)

        assert rec_transport.last_request is not None
        req = rec_transport.last_request
        assert len(req.messages) >= 2, "context + question 최소 2 메시지"
        assert req.messages[0]["content"] == ctx, "첫 메시지가 context여야 한다"

    def test_context_None이면_messages에_question만_있다(self, card: AgentCard) -> None:
        rec_transport = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec_transport)

        runtime.answer("질문", card, context=None)

        req = rec_transport.last_request
        assert req is not None
        assert len(req.messages) == 1, "context 없으면 question만"
        assert req.messages[0]["content"] == "질문"

    def test_context_미주입이면_messages에_question만_있다(self, card: AgentCard) -> None:
        rec_transport = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec_transport)

        runtime.answer("질문", card)

        req = rec_transport.last_request
        assert req is not None
        assert len(req.messages) == 1

    def test_context_있으면_질문은_마지막_messages에_붙는다(self, card: AgentCard) -> None:
        rec_transport = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec_transport)

        runtime.answer("두 번째 질문", card, context="과거 발화")

        req = rec_transport.last_request
        assert req is not None
        assert req.messages[-1]["content"] == "두 번째 질문"

    def test_Answer_계약_보존_context_있어도_동일(self, card: AgentCard) -> None:
        """context가 있어도 Answer 필드 구조 불변."""
        transport = StubProviderTransport(chunks=["답변"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card, context="맥락")
        assert set(answer.__dataclass_fields__) == {"text", "sources", "mode", "snapshot_sha"}
        assert answer.mode == "full"
        assert answer.snapshot_sha is None

    def test_build_provider_request_context_messages_구조(self, card: AgentCard) -> None:
        """build_provider_request 순수 함수 직접 단언 — context가 messages에 선행."""
        ctx = "User: 안녕\nAssistant: 안녕하세요"
        req = build_provider_request("다음 질문", card, context=ctx)
        assert req.messages[0] == {"role": "user", "content": ctx}
        assert req.messages[1] == {"role": "user", "content": "다음 질문"}

    def test_하위호환_context_None_기존_provider_tests_미파괴(self, card: AgentCard) -> None:
        """기존 provider test가 context 없이 호출해도 동일 결과."""
        transport = StubProviderTransport(chunks=["기존 응답"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer1 = runtime.answer("질문", card)
        answer2 = runtime.answer("질문", card, context=None)
        assert answer1.text == answer2.text


# ---------------------------------------------------------------------------
# 5 dispatcher — dispatch(question, card, context=None) 시그니처 흡수
# ---------------------------------------------------------------------------


class TestDispatcherContextSignature:
    """5 dispatcher 구현이 context=None 옵셔널 인자를 받는다."""

    def test_LocalRuntimeDispatcher_dispatch_context_인자_수락(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        runtime = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=runtime)
        ticket = disp.dispatch("질문", card, context="맥락")
        assert ticket is not None

    def test_LocalRuntimeDispatcher_context_None_기존_동작(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        runtime = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=runtime)
        ticket = disp.dispatch("질문", card, context=None)
        assert ticket is not None

    def test_LocalRuntimeDispatcher_context_미주입_기존_동작(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        runtime = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=runtime)
        ticket = disp.dispatch("질문", card)
        assert ticket is not None

    def test_InMemoryWorkQueueDispatcher_dispatch_context_인자_수락(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import InMemoryWorkQueueDispatcher

        disp = InMemoryWorkQueueDispatcher()
        ticket = disp.dispatch("질문", card, context="맥락")
        assert ticket is not None

    def test_InMemoryWorkQueueDispatcher_context_None(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import InMemoryWorkQueueDispatcher

        disp = InMemoryWorkQueueDispatcher()
        ticket = disp.dispatch("질문", card, context=None)
        assert ticket is not None

    def test_WebSocketDispatcher_dispatch_context_인자_수락(self, card: AgentCard) -> None:
        from agent_org_network.transport import WebSocketDispatcher

        disp = WebSocketDispatcher()
        ticket = disp.dispatch("질문", card, context="맥락")
        assert ticket is not None

    def test_WebSocketDispatcher_context_None(self, card: AgentCard) -> None:
        from agent_org_network.transport import WebSocketDispatcher

        disp = WebSocketDispatcher()
        ticket = disp.dispatch("질문", card, context=None)
        assert ticket is not None

    def test_DispatchingRuntime_answer_context_인자_수락(self, card: AgentCard) -> None:
        """DispatchingRuntime은 AgentRuntime 포트라 answer(context=)로 받는다."""
        from agent_org_network.dispatch import DispatchingRuntime, LocalRuntimeDispatcher

        runtime = StubRuntime()
        inner_disp = LocalRuntimeDispatcher(runtime=runtime)
        dispatching = DispatchingRuntime(dispatcher=inner_disp)
        answer = dispatching.answer("질문", card, context="맥락")
        assert isinstance(answer, Answer)

    def test_RuntimeDispatcher_Protocol_dispatch_context_정합(self, card: AgentCard) -> None:
        """RuntimeDispatcher Protocol이 context 파라미터를 선언한다."""
        import inspect
        from agent_org_network.dispatch import RuntimeDispatcher

        sig = inspect.signature(RuntimeDispatcher.dispatch)
        params = sig.parameters
        assert "context" in params, "RuntimeDispatcher.dispatch 에 context 파라미터가 있어야 한다"


# ---------------------------------------------------------------------------
# 로컬 경로 — context가 runtime.answer 까지 실제 전달됨을 단언
# ---------------------------------------------------------------------------


class TestLocalDispatcherContextDelivery:
    """LocalRuntimeDispatcher가 dispatch context를 runtime.answer(context=)로 즉시 전달한다."""

    def test_LocalRuntimeDispatcher_context_가_StubRuntime에_도달한다(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        runtime = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=runtime)
        disp.dispatch("질문", card, context="맥락 도달 테스트")
        assert runtime.last_context == "맥락 도달 테스트"

    def test_LocalRuntimeDispatcher_context_None_StubRuntime_last_context_None(
        self, card: AgentCard
    ) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        runtime = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=runtime)
        disp.dispatch("질문", card, context=None)
        assert runtime.last_context is None

    def test_LocalRuntimeDispatcher_context_미주입_last_context_None(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        runtime = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=runtime)
        disp.dispatch("질문", card)
        assert runtime.last_context is None

    def test_ClaudeApiRuntime_로컬_context_도달_via_recording_transport(
        self, card: AgentCard
    ) -> None:
        """ClaudeApiRuntime + RecordingTransport로 context가 messages에 실림을 단언."""
        from agent_org_network.dispatch import LocalRuntimeDispatcher

        rec_transport = RecordingTransport()
        runtime = ClaudeApiRuntime(transport=rec_transport)
        disp = LocalRuntimeDispatcher(runtime=runtime)

        ctx = "User: 이전 질문\nAssistant: 이전 답"
        disp.dispatch("현재 질문", card, context=ctx)

        req = rec_transport.last_request
        assert req is not None
        assert len(req.messages) == 2
        assert req.messages[0]["content"] == ctx
        assert req.messages[1]["content"] == "현재 질문"


# ---------------------------------------------------------------------------
# WS 계열 — context 흡수만·미전파(T9.7 후속)
# ---------------------------------------------------------------------------


class TestWSDispatchersContextAbsorption:
    """WS 계열 dispatcher는 context 인자를 받되 큐·WorkTicket에 싣지 않는다."""

    def test_InMemoryWorkQueueDispatcher_context_흡수_WorkTicket에_context_없음(
        self, card: AgentCard
    ) -> None:
        from agent_org_network.dispatch import InMemoryWorkQueueDispatcher

        disp = InMemoryWorkQueueDispatcher()
        ticket = disp.dispatch("질문", card, context="흡수할 맥락")
        assert not hasattr(ticket, "context"), "WorkTicket에 context 필드가 없어야 한다"

    def test_WebSocketDispatcher_context_흡수_WorkTicket에_context_없음(
        self, card: AgentCard
    ) -> None:
        from agent_org_network.transport import WebSocketDispatcher

        disp = WebSocketDispatcher()
        ticket = disp.dispatch("질문", card, context="흡수할 맥락")
        assert not hasattr(ticket, "context"), "WorkTicket에 context 필드가 없어야 한다"
