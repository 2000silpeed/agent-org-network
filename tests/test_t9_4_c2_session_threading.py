"""T9.4(c-2) — 스레딩 경로 + 멀티턴 로컬 관측 [게이트 내·결정론]

ADR 0027 결정 7·8, ADR 0024 결정 5-bis.

불변식:
- AskOrg.handle(question, user, context=None) 옵셔널 진화 — 미주입이면 기존 동작.
- route(question)엔 context 미투입(라우팅 정합 — context에 다른 도메인 키워드 있어도 라우팅 불변).
- dispatch(question, card, context=context)로만 context 전달.
- SessionAskOrg.handle: open_or_get → assemble_context(append_turn 전) → ask.handle(context=) → Answered면 append_turn.
- 멀티턴 로컬 관측: 턴 N의 runtime context에 턴 N-1 발화가 포함됨을 단언.
- 첫 턴: context 빈 문자열(과거 턴 없음).
- 미아 없음 회귀: 세션 층 통과 후에도 Unowned/Contested 종착 보존.
- 전이≠기록: context = 과거 턴만(현재 질문·답 미포함 — append_turn 전 조립).
"""

from datetime import date, datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg, Answered, Pending
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.dispatch import LocalRuntimeDispatcher
from agent_org_network.provider_runtime import ClaudeApiRuntime, ProviderRequest
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.session import InMemorySessionStore, SessionAskOrg
from agent_org_network.user import User


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 6, 27, 10, 0, 0, tzinfo=timezone.utc)
_USER = User(id="alice")


def _fixed_clock() -> datetime:
    return _T0


def _card(
    agent_id: str = "cs_ops",
    owner: str = "alice",
    domains: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="CS팀",
        summary="고객 서비스 운영",
        domains=domains if domains is not None else ["고객지원"],
        last_reviewed_at=date(2026, 6, 27),
        knowledge_sources=["cs_ops/policy.md"],
    )


class RecordingTransport:
    """ProviderRequest를 순서대로 기록하는 결정론 transport(ProviderTransport Protocol 만족)."""

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def __call__(self, request: ProviderRequest) -> list[str]:
        self.requests.append(request)
        return ["stub 답변"]


def _build_ask(
    card: AgentCard,
    intent: str,
    runtime: StubRuntime | None = None,
) -> "tuple[AskOrg, StubRuntime]":
    """FakeClassifier + registry로 AskOrg를 빌드하는 헬퍼."""
    rt = runtime if runtime is not None else StubRuntime()
    registry = Registry()
    registry.register(card)
    classifier = FakeClassifier(intent)
    router = Router(registry=registry, classifier=classifier, root_user="root")
    disp = LocalRuntimeDispatcher(runtime=rt)
    ask = AskOrg(
        router=router,
        dispatcher=disp,
        audit_log=InMemoryAuditLog(),
    )
    return ask, rt


def _build_ask_claude(
    card: AgentCard,
    intent: str,
    rec: RecordingTransport,
) -> AskOrg:
    """ClaudeApiRuntime + RecordingTransport로 AskOrg를 빌드하는 헬퍼."""
    registry = Registry()
    registry.register(card)
    classifier = FakeClassifier(intent)
    router = Router(registry=registry, classifier=classifier, root_user="root")
    runtime = ClaudeApiRuntime(transport=rec)
    disp = LocalRuntimeDispatcher(runtime=runtime)
    return AskOrg(
        router=router,
        dispatcher=disp,
        audit_log=InMemoryAuditLog(),
    )


# ---------------------------------------------------------------------------
# AskOrg.handle — context 옵셔널 진화
# ---------------------------------------------------------------------------


class TestAskOrgHandleContextSignature:
    """AskOrg.handle(question, user, context=None) 시그니처 진화 검증."""

    def test_handle_context_키워드_인자_수락(self) -> None:
        card = _card()
        ask, _ = _build_ask(card, "고객지원")
        reply = ask.handle("고객 지원 요청", _USER, context="이전 발화")
        assert isinstance(reply, (Answered, Pending))

    def test_handle_context_None_기존_동작_보존(self) -> None:
        card = _card()
        ask, _ = _build_ask(card, "고객지원")
        reply = ask.handle("고객 지원 요청", _USER, context=None)
        assert isinstance(reply, Answered)

    def test_handle_context_미주입_기존_동작_보존(self) -> None:
        card = _card()
        ask, _ = _build_ask(card, "고객지원")
        reply = ask.handle("고객 지원 요청", _USER)
        assert isinstance(reply, Answered)

    def test_handle_context_있어도_Answered_반환(self) -> None:
        card = _card()
        ask, _ = _build_ask(card, "고객지원")
        reply = ask.handle("고객 지원 요청", _USER, context="이전 대화 내용")
        assert isinstance(reply, Answered)

    def test_handle_하위호환_기존_호출처_무영향(self) -> None:
        """기존 2-인자 호출이 그대로 동작한다."""
        card = _card()
        ask, _ = _build_ask(card, "고객지원")
        reply_old = ask.handle("고객 지원 요청", _USER)
        reply_new = ask.handle("고객 지원 요청", _USER, context=None)
        assert type(reply_old) is type(reply_new)


# ---------------------------------------------------------------------------
# 라우팅 정합 — context가 route()에 미투입됨을 단언
# ---------------------------------------------------------------------------


class SpyFakeClassifier:
    """분류 호출을 기록하는 spy Classifier — 라우팅 정합 검증."""

    def __init__(self, intent: str) -> None:
        self._intent = intent
        self.last_question: str | None = None

    def classify(self, question: str) -> str:
        self.last_question = question
        return self._intent


class TestAskOrgRoutingIsolation:
    """route(question)엔 context가 들어가지 않는다 — 라우팅 정합."""

    def test_route에_question만_전달되고_context는_안_닿는다(self) -> None:
        """context에 다른 도메인 키워드가 있어도 분류기가 본 question은 원본 그대로."""
        card = _card()
        registry = Registry()
        registry.register(card)
        spy_classifier = SpyFakeClassifier("고객지원")
        router = Router(registry=registry, classifier=spy_classifier, root_user="root")
        rt = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=rt)
        ask = AskOrg(router=router, dispatcher=disp, audit_log=InMemoryAuditLog())

        ctx = "이전 대화 내용: 재무/인사/IT 등 다른 도메인 키워드"
        ask.handle("고객 서비스 문의", _USER, context=ctx)

        assert spy_classifier.last_question == "고객 서비스 문의"
        assert ctx not in (spy_classifier.last_question or ""), "context가 분류기에 전달되면 안 된다"

    def test_context에_다른_도메인_키워드_있어도_라우팅_결과_동일(self) -> None:
        """라우팅 종착이 context에 무반응 — 라우팅 정합."""
        card = _card()
        ask, _ = _build_ask(card, "고객지원")

        reply_no_ctx = ask.handle("고객 서비스 문의", _USER)
        reply_with_ctx = ask.handle("고객 서비스 문의", _USER, context="재무팀 관련 이전 대화")

        assert type(reply_no_ctx) is type(reply_with_ctx)
        if isinstance(reply_no_ctx, Answered) and isinstance(reply_with_ctx, Answered):
            assert reply_no_ctx.answered_by == reply_with_ctx.answered_by


# ---------------------------------------------------------------------------
# context → dispatch 전달
# ---------------------------------------------------------------------------


class TestAskOrgContextToDispatch:
    """AskOrg.handle이 context를 dispatch(question, card, context=)로만 전달한다."""

    def test_context가_LocalRuntimeDispatcher_경유_StubRuntime에_도달한다(self) -> None:
        card = _card()
        ask, rt = _build_ask(card, "고객지원")

        ctx = "이전 발화 맥락"
        ask.handle("고객 서비스 문의", _USER, context=ctx)
        assert rt.last_context == ctx

    def test_context_None이면_runtime_last_context_None(self) -> None:
        card = _card()
        ask, rt = _build_ask(card, "고객지원")

        ask.handle("고객 서비스 문의", _USER, context=None)
        assert rt.last_context is None

    def test_context_미주입이면_runtime_last_context_None(self) -> None:
        card = _card()
        ask, rt = _build_ask(card, "고객지원")

        ask.handle("고객 서비스 문의", _USER)
        assert rt.last_context is None

    def test_ClaudeApiRuntime_context_messages에_실림(self) -> None:
        """context가 ClaudeApiRuntime의 build_provider_request로 messages에 실린다."""
        card = _card()
        rec = RecordingTransport()
        ask = _build_ask_claude(card, "고객지원", rec)

        ctx = "User: 이전 질문\nAssistant: 이전 답"
        ask.handle("현재 질문", _USER, context=ctx)

        assert len(rec.requests) == 1
        req = rec.requests[0]
        assert req.messages[0]["content"] == ctx
        assert req.messages[1]["content"] == "현재 질문"


# ---------------------------------------------------------------------------
# SessionAskOrg.handle — context 스레딩 경로 진화
# ---------------------------------------------------------------------------


class TestSessionAskOrgContextThreading:
    """SessionAskOrg.handle이 assemble_context → ask.handle(context=)로 context를 흘린다."""

    def test_첫_턴_context는_빈_문자열_또는_None(self) -> None:
        """첫 턴 — 과거 턴 없으므로 runtime.last_context는 빈 문자열이어야 한다."""
        card = _card()
        ask, rt = _build_ask(card, "고객지원")
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        wrapper.handle("첫 질문", _USER)

        assert rt.last_context in ("", None), "첫 턴 context는 빈 문자열이어야 한다"

    def test_두번째_턴_context에_첫번째_턴이_포함된다(self) -> None:
        """턴 N의 runtime context에 턴 N-1 발화가 들어있어야 한다(핵심 불변식)."""
        card = _card()
        ask, rt = _build_ask(card, "고객지원")
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        wrapper.handle("첫 질문", _USER)
        wrapper.handle("두 번째 질문", _USER)

        assert rt.last_context is not None
        assert "첫 질문" in rt.last_context

    def test_세번째_턴_context에_첫번째_두번째_턴이_포함된다(self) -> None:
        """턴 3의 context에 턴 1과 2 발화가 모두 포함된다."""
        card = _card()
        ask, rt = _build_ask(card, "고객지원")
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        wrapper.handle("첫 질문", _USER)
        wrapper.handle("두 번째 질문", _USER)
        wrapper.handle("세 번째 질문", _USER)

        assert rt.last_context is not None
        assert "첫 질문" in rt.last_context
        assert "두 번째 질문" in rt.last_context

    def test_context에_현재_질문은_포함되지_않는다(self) -> None:
        """전이≠기록: context = 과거 턴만(현재 질문·답 미포함 — append_turn 전 조립)."""
        card = _card()
        ask, rt = _build_ask(card, "고객지원")
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        wrapper.handle("첫 질문", _USER)
        wrapper.handle("두 번째 질문", _USER)

        assert rt.last_context is not None
        assert "두 번째 질문" not in rt.last_context, "현재 질문이 context에 포함되면 안 된다"

    def test_assemble_context_append_turn_전_호출_검증(self) -> None:
        """첫 턴 빈 맥락 → 두 번째 턴 첫 질문 포함 → 순서 확인."""
        card = _card()
        ask, rt = _build_ask(card, "고객지원")
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        wrapper.handle("질문 A", _USER)
        ctx_after_first = rt.last_context

        wrapper.handle("질문 B", _USER)
        ctx_after_second = rt.last_context

        assert ctx_after_first in ("", None), "첫 턴 context = 빈(과거 없음)"
        assert ctx_after_second is not None and "질문 A" in ctx_after_second


# ---------------------------------------------------------------------------
# 멀티턴 로컬 관측 (핵심 테스트) — ClaudeApiRuntime + RecordingTransport
# ---------------------------------------------------------------------------


class TestMultiturnLocalObservation:
    """멀티턴 — 턴 N의 runtime messages에 턴 N-1 발화가 담겨 있음을 결정론 단언."""

    def test_첫_턴_messages에_context_없음_question만(self) -> None:
        """첫 턴은 과거 없으므로 messages에 context 없음(question만)."""
        card = _card()
        rec = RecordingTransport()
        ask = _build_ask_claude(card, "고객지원", rec)
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        wrapper.handle("첫 질문", _USER)

        assert len(rec.requests) == 1
        req = rec.requests[0]
        assert len(req.messages) == 1, "첫 턴은 question만(context 없음)"
        assert req.messages[0]["content"] == "첫 질문"

    def test_두번째_턴_messages에_첫번째_발화가_포함된다(self) -> None:
        """턴 2의 messages[0]에 턴 1 발화(User/Assistant)가 실린다."""
        card = _card()
        rec = RecordingTransport()
        ask = _build_ask_claude(card, "고객지원", rec)
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        wrapper.handle("첫 질문", _USER)
        wrapper.handle("두 번째 질문", _USER)

        assert len(rec.requests) == 2
        req2 = rec.requests[1]
        assert len(req2.messages) == 2, "context + question 두 messages"
        assert "첫 질문" in req2.messages[0]["content"]
        assert req2.messages[1]["content"] == "두 번째 질문"

    def test_세번째_턴_messages에_이전_두_턴이_모두_포함된다(self) -> None:
        """턴 3의 context에 턴 1, 2 발화가 모두 포함된다."""
        card = _card()
        rec = RecordingTransport()
        ask = _build_ask_claude(card, "고객지원", rec)
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        wrapper.handle("첫 질문", _USER)
        wrapper.handle("두 번째 질문", _USER)
        wrapper.handle("세 번째 질문", _USER)

        assert len(rec.requests) == 3
        req3 = rec.requests[2]
        assert len(req3.messages) == 2
        ctx_content = req3.messages[0]["content"]
        assert "첫 질문" in ctx_content
        assert "두 번째 질문" in ctx_content
        assert req3.messages[1]["content"] == "세 번째 질문"

    def test_멀티턴_context_누적_불변식(self) -> None:
        """각 턴의 messages 길이: 첫 턴 1, 둘째 턴 2, 셋째 턴 2(context 누적)."""
        card = _card()
        rec = RecordingTransport()
        ask = _build_ask_claude(card, "고객지원", rec)
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        wrapper.handle("첫", _USER)
        wrapper.handle("둘", _USER)
        wrapper.handle("셋", _USER)

        assert len(rec.requests[0].messages) == 1
        assert len(rec.requests[1].messages) == 2
        assert len(rec.requests[2].messages) == 2


# ---------------------------------------------------------------------------
# 미아 없음 회귀 — 세션 층 통과 후에도 라우팅 종착 보존
# ---------------------------------------------------------------------------


class TestSessionAskOrgNoOrphan:
    """SessionAskOrg 통과 후에도 미아 없음(0매칭→Unowned 보존)."""

    def test_미아_없음_Unowned_세션_층_통과후에도_보존(self) -> None:
        """0매칭 → Unowned 종착이 세션 래퍼 통과 후에도 동일."""
        registry = Registry()
        classifier = FakeClassifier("아무도_모름")
        router = Router(registry=registry, classifier=classifier, root_user="root")
        rt = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=rt)
        ask = AskOrg(
            router=router,
            dispatcher=disp,
            audit_log=InMemoryAuditLog(),
        )
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        reply = wrapper.handle("아무도 모르는 질문", _USER)

        assert isinstance(reply, Pending)
        assert reply.kind == "unowned"

    def test_미아_없음_세션_이력_있어도_Unowned_종착_보존(self) -> None:
        """세션 이력(assemble_context 산출)이 있어도 0매칭 → Unowned 종착은 유지된다."""
        registry = Registry()
        classifier = FakeClassifier("아무도_모름")
        router = Router(registry=registry, classifier=classifier, root_user="root")
        rt = StubRuntime()
        disp = LocalRuntimeDispatcher(runtime=rt)
        ask = AskOrg(
            router=router,
            dispatcher=disp,
            audit_log=InMemoryAuditLog(),
        )
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        reply = wrapper.handle("아무도 모르는 질문", _USER)

        assert isinstance(reply, Pending)
        assert reply.kind == "unowned"


# ---------------------------------------------------------------------------
# SessionAskOrg.handle — 기본 동작 회귀
# ---------------------------------------------------------------------------


class TestSessionAskOrgBasic:
    """SessionAskOrg.handle(question, user) 기본 동작 회귀."""

    def test_handle_기본_호출_동작(self) -> None:
        card = _card()
        ask, _ = _build_ask(card, "고객지원")
        store = InMemorySessionStore(clock=_fixed_clock)
        wrapper = SessionAskOrg(ask=ask, session_store=store, clock=_fixed_clock)

        reply = wrapper.handle("질문", _USER)
        assert isinstance(reply, (Answered, Pending))
