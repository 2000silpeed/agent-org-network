"""ADR 0031 결정 2·3 게이트 내 — AskOrg.handle_stream / SessionAskOrg.handle_stream 오케스트레이션.

결정론 보장:
  - stub 스트리밍 런타임/블로킹 stub 주입으로 고정 청크열 → 결정 가능한 AskEvent열.
  - 라우터·디스패처·audit 모두 결정론 stub/fake.

잠근 불변식(ADR 0031 결정 2·3):
  - decide 정확히 1회(route 호출 카운트).
  - audit 정확히 1회(스트림 완료 시점·record 카운트).
  - meta 1회·token N회(스트리밍)/1회(폴백)·done 1회.
  - Contested/Unowned → pending 단독(meta/token/done 없음)·분기 부수효과 1회.
  - Approval 게이트: requires_approval → done.mode == "draft_only"(최종 권위는 done).
  - 노출 불변식: token 페이로드에 내부값 0.
  - 기존 handle 무회귀(상호 배타).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import (
    AskOrg,
    DoneEvent,
    ErrorEvent,
    MetaEvent,
    PendingEvent,
    TokenEvent,
)
from agent_org_network.audit import AuditEntry, InMemoryAuditLog
from agent_org_network.conflict import InMemoryConflictCaseStore
from agent_org_network.decision import Contested, Routed, RoutingDecision, Unowned
from agent_org_network.dispatch import LocalStreamingDispatcher
from agent_org_network.runtime import AgentRuntime, StubRuntime, StubStreamingRuntime
from agent_org_network.session import InMemorySessionStore, SessionAskOrg
from agent_org_network.user import User


def fixed_clock() -> datetime:
    return datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def card(agent_id: str = "finance_ops", owner: str = "alice") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="finance",
        summary="요약",
        domains=["finance"],
        last_reviewed_at=date(2026, 1, 1),
        knowledge_sources=["위키/예산"],
    )


class CountingRouter:
    """route 호출 횟수를 세는 결정론 라우터(고정 결정 반환)."""

    def __init__(self, decision: RoutingDecision) -> None:
        self._decision = decision
        self.route_calls = 0

    def route(self, question: str) -> RoutingDecision:
        self.route_calls += 1
        return self._decision


class CountingAudit(InMemoryAuditLog):
    """record 호출 횟수를 세는 audit log."""

    def __init__(self) -> None:
        super().__init__()
        self.record_calls = 0

    def record(self, entry: AuditEntry) -> None:
        self.record_calls += 1
        super().record(entry)


def make_ask(
    decision: RoutingDecision,
    *,
    runtime: AgentRuntime | None = None,
    case_store: InMemoryConflictCaseStore | None = None,
) -> tuple[AskOrg, CountingRouter, CountingAudit]:
    rt = runtime if runtime is not None else StubStreamingRuntime(deltas=("A", "B", "C"))
    router = CountingRouter(decision)
    audit = CountingAudit()
    ask = AskOrg(
        router=router,
        dispatcher=LocalStreamingDispatcher(rt),
        audit_log=audit,
        clock=fixed_clock,
        case_store=case_store,
    )
    return ask, router, audit


# ── Routed: meta → token* → done ──────────────────────────────────────────


def test_Routed_스트림은_meta_token들_done_순서로_난다() -> None:
    ask, _, _ = make_ask(Routed(primary=card(), intent="finance"))
    events = list(ask.handle_stream("질문", User(id="u1")))

    assert isinstance(events[0], MetaEvent)
    assert isinstance(events[-1], DoneEvent)
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert [t.text for t in tokens] == ["A", "B", "C"]


def test_Routed_meta는_정확히_1회_done도_1회() -> None:
    ask, _, _ = make_ask(Routed(primary=card(), intent="finance"))
    events = list(ask.handle_stream("질문", User(id="u1")))

    assert sum(1 for e in events if isinstance(e, MetaEvent)) == 1
    assert sum(1 for e in events if isinstance(e, DoneEvent)) == 1


def test_Routed_meta는_담당_초기mode_sources를_싣는다() -> None:
    ask, _, _ = make_ask(Routed(primary=card(), intent="finance"))
    events = list(ask.handle_stream("질문", User(id="u1")))
    meta = next(e for e in events if isinstance(e, MetaEvent))
    assert meta.answered_by == ("alice", "finance_ops")
    assert meta.sources == ("위키/예산",)
    assert meta.mode == "full"


def test_Routed_token은_델타마다_N회_스트리밍런타임() -> None:
    ask, _, _ = make_ask(
        Routed(primary=card(), intent="finance"),
        runtime=StubStreamingRuntime(deltas=("토1", "토2", "토3", "토4")),
    )
    events = list(ask.handle_stream("질문", User(id="u1")))
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert len(tokens) == 4


def test_Routed_폴백런타임은_token이_1회() -> None:
    # StubRuntime은 StreamingRuntime이 아님 → answer 1델타 폴백.
    ask, _, _ = make_ask(
        Routed(primary=card(), intent="finance"),
        runtime=StubRuntime(),
    )
    events = list(ask.handle_stream("질문", User(id="u1")))
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert len(tokens) == 1


# ── decide 1회 ────────────────────────────────────────────────────────────


def test_Routed_route는_정확히_1회_호출된다() -> None:
    ask, router, _ = make_ask(Routed(primary=card(), intent="finance"))
    list(ask.handle_stream("질문", User(id="u1")))
    assert router.route_calls == 1


# ── audit-once: 스트림 완료 시 정확히 1회 ─────────────────────────────────


def test_Routed_audit는_스트림_완료시_정확히_1회() -> None:
    ask, _, audit = make_ask(Routed(primary=card(), intent="finance"))
    list(ask.handle_stream("질문", User(id="u1")))
    assert audit.record_calls == 1


def test_Routed_audit는_done_직전에_기록된다() -> None:
    """스트림을 다 흘리기 전(done 전)에는 record가 안 되고, 다 흘린 뒤 1회."""
    ask, _, audit = make_ask(Routed(primary=card(), intent="finance"))
    gen = ask.handle_stream("질문", User(id="u1"))

    seen: list[object] = []
    for event in gen:
        if isinstance(event, DoneEvent):
            # done을 받기 직전까지 record가 안 됐어야 한다.
            assert audit.record_calls == 1
        seen.append(event)
    assert audit.record_calls == 1


def test_Routed_audit_엔트리는_완성_답을_본다() -> None:
    ask, _, audit = make_ask(Routed(primary=card(), intent="finance"))
    list(ask.handle_stream("질문", User(id="u1")))
    entry = audit.entries[-1]
    assert entry.answer is not None
    assert entry.answer.text == "ABC"


def test_Routed_token까지만_소비하면_audit_아직_0() -> None:
    """스트림을 끝까지 안 흘리면(토큰만 소비) audit record가 아직 안 됐어야 한다."""
    ask, _, audit = make_ask(Routed(primary=card(), intent="finance"))
    gen = ask.handle_stream("질문", User(id="u1"))

    # meta + 첫 토큰만 소비.
    first = next(gen)
    assert isinstance(first, MetaEvent)
    second = next(gen)
    assert isinstance(second, TokenEvent)
    # 아직 스트림이 안 끝났으므로 audit 0.
    assert audit.record_calls == 0

    # 나머지 다 흘리면 1.
    for _ in gen:
        pass
    assert audit.record_calls == 1


# ── Approval 게이트: done.mode가 최종 권위 ────────────────────────────────


def test_requires_approval면_done_mode가_draft_only() -> None:
    ask, _, _ = make_ask(Routed(primary=card(), requires_approval=True, intent="finance"))
    events = list(ask.handle_stream("질문", User(id="u1")))
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.mode == "draft_only"


def test_requires_approval여도_meta_mode는_초기_full() -> None:
    ask, _, _ = make_ask(Routed(primary=card(), requires_approval=True, intent="finance"))
    events = list(ask.handle_stream("질문", User(id="u1")))
    meta = next(e for e in events if isinstance(e, MetaEvent))
    assert meta.mode == "full"


def test_requires_approval_없으면_done_mode_full() -> None:
    ask, _, _ = make_ask(Routed(primary=card(), intent="finance"))
    events = list(ask.handle_stream("질문", User(id="u1")))
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.mode == "full"


# ── Contested → pending 단독 ──────────────────────────────────────────────


def test_Contested는_pending_단독_meta_token_done_없음() -> None:
    cards = (card("cs_ops", "owner_cs"), card("sales_ops", "owner_sales"))
    ask, _, _ = make_ask(Contested(candidates=cards, intent="환불"))
    events = list(ask.handle_stream("환불 되나요?", User(id="u1")))

    assert len(events) == 1
    assert isinstance(events[0], PendingEvent)
    assert events[0].kind == "contested"
    assert not any(isinstance(e, (MetaEvent, TokenEvent, DoneEvent)) for e in events)


def test_Contested_부수효과_ConflictCase_open_1회() -> None:
    cards = (card("cs_ops", "owner_cs"), card("sales_ops", "owner_sales"))
    case_store = InMemoryConflictCaseStore()
    ask, _, _ = make_ask(
        Contested(candidates=cards, intent="환불"), case_store=case_store
    )
    list(ask.handle_stream("환불 되나요?", User(id="u1")))
    assert len(case_store.open_for_owner("owner_cs")) == 1


def test_Contested_route_1회_audit_1회() -> None:
    cards = (card("cs_ops", "owner_cs"), card("sales_ops", "owner_sales"))
    ask, router, audit = make_ask(Contested(candidates=cards, intent="환불"))
    list(ask.handle_stream("환불 되나요?", User(id="u1")))
    assert router.route_calls == 1
    assert audit.record_calls == 1


# ── Unowned → pending 단독 ────────────────────────────────────────────────


def test_Unowned는_pending_단독() -> None:
    ask, _, _ = make_ask(Unowned(escalated_to="root", intent="unknown"))
    events = list(ask.handle_stream("모르는 질문", User(id="u1")))

    assert len(events) == 1
    assert isinstance(events[0], PendingEvent)
    assert events[0].kind == "unowned"


def test_Unowned_route_1회_audit_1회() -> None:
    ask, router, audit = make_ask(Unowned(escalated_to="root", intent="unknown"))
    list(ask.handle_stream("모르는 질문", User(id="u1")))
    assert router.route_calls == 1
    assert audit.record_calls == 1


# ── 노출 불변식: token에 내부값 0 ─────────────────────────────────────────


def test_token_이벤트는_텍스트만_들고_내부값_0() -> None:
    ask, _, _ = make_ask(Routed(primary=card(), intent="finance"))
    events = list(ask.handle_stream("질문", User(id="u1")))
    for e in events:
        if isinstance(e, TokenEvent):
            # TokenEvent는 text 필드만 — owner/agent_id/mode/sources 없음.
            assert not hasattr(e, "owner")
            assert not hasattr(e, "mode")
            assert not hasattr(e, "sources")


# ── 기존 handle 무회귀 (상호 배타) ────────────────────────────────────────


def test_handle와_handle_stream은_상호_배타_기존_handle_무변경() -> None:
    from agent_org_network.ask_org import Answered

    ask, _, audit = make_ask(Routed(primary=card(), intent="finance"))
    reply = ask.handle("질문", User(id="u1"))
    assert isinstance(reply, Answered)
    # handle만 탔으므로 audit 1회.
    assert audit.record_calls == 1


# ── SessionAskOrg.handle_stream ───────────────────────────────────────────


def make_session_ask(
    decision: RoutingDecision,
    *,
    runtime: AgentRuntime | None = None,
) -> tuple[SessionAskOrg, InMemorySessionStore, CountingAudit]:
    rt = runtime if runtime is not None else StubStreamingRuntime(deltas=("X", "Y"))
    router = CountingRouter(decision)
    audit = CountingAudit()
    ask = AskOrg(
        router=router,
        dispatcher=LocalStreamingDispatcher(rt),
        audit_log=audit,
        clock=fixed_clock,
    )
    store = InMemorySessionStore(clock=fixed_clock)
    session_ask = SessionAskOrg(ask=ask, session_store=store, clock=fixed_clock)
    return session_ask, store, audit


def test_Session_handle_stream_Routed는_meta_token_done() -> None:
    session_ask, _, _ = make_session_ask(Routed(primary=card(), intent="finance"))
    events = list(session_ask.handle_stream("질문", User(id="u1")))
    assert isinstance(events[0], MetaEvent)
    assert isinstance(events[-1], DoneEvent)
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert [t.text for t in tokens] == ["X", "Y"]


def test_Session_handle_stream_완성답으로_append_turn_1회() -> None:
    session_ask, store, _ = make_session_ask(Routed(primary=card(), intent="finance"))
    list(session_ask.handle_stream("질문", User(id="u1")))
    session = store.active_for_user("u1")
    assert session is not None
    assert len(session.transcript) == 1
    assert session.transcript[0].answer_text == "XY"
    assert session.transcript[0].answered_by == "finance_ops"


def test_Session_handle_stream_audit_1회() -> None:
    session_ask, _, audit = make_session_ask(Routed(primary=card(), intent="finance"))
    list(session_ask.handle_stream("질문", User(id="u1")))
    assert audit.record_calls == 1


def test_Session_handle_stream_Pending은_턴_미적재() -> None:
    cards = (card("cs_ops", "owner_cs"), card("sales_ops", "owner_sales"))
    session_ask, store, _ = make_session_ask(Contested(candidates=cards, intent="환불"))
    events = list(session_ask.handle_stream("환불 되나요?", User(id="u1")))
    assert len(events) == 1
    assert isinstance(events[0], PendingEvent)
    session = store.active_for_user("u1")
    assert session is not None
    assert len(session.transcript) == 0


def test_Session_handle_stream_과거턴이_맥락으로_dispatch에_전달된다() -> None:
    # 첫 턴 후 둘째 턴에서 assemble_context가 과거 턴을 런타임 context로 넘기는지.
    rt = StubRuntime()
    router = CountingRouter(Routed(primary=card(), intent="finance"))
    audit = CountingAudit()
    ask = AskOrg(
        router=router,
        dispatcher=LocalStreamingDispatcher(rt),
        audit_log=audit,
        clock=fixed_clock,
    )
    store = InMemorySessionStore(clock=fixed_clock)
    session_ask = SessionAskOrg(ask=ask, session_store=store, clock=fixed_clock)

    list(session_ask.handle_stream("첫 질문", User(id="u1")))
    list(session_ask.handle_stream("둘째 질문", User(id="u1")))

    # 둘째 턴에서 런타임이 받은 context에 첫 질문 흔적이 있어야 한다.
    assert rt.last_context is not None
    assert "첫 질문" in rt.last_context


def test_ErrorEvent_타입이_존재한다() -> None:
    # error 이벤트 타입 자체는 게이트 내 정의(실 런타임 실패 투영은 게이트 밖).
    e = ErrorEvent(message="중립")
    assert e.message == "중립"
