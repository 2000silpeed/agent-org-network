from datetime import date, datetime, timedelta, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg, Answered, Pending
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.conflict import (
    Agreed,
    ConcurOnPrimary,
    ConsensusService,
    InMemoryConflictCaseStore,
    InMemoryPrecedentStore,
)
from agent_org_network.dispatch import (
    InMemoryWorkQueueDispatcher,
    LocalRuntimeDispatcher,
    RuntimeDispatcher,
)
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import Answer, StubRuntime
from agent_org_network.transport import WebSocketDispatcher
from agent_org_network.user import User


def fixed_clock() -> datetime:
    return datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def card(agent_id: str, domains: list[str], owner: str = "D", knowledge_sources: list[str] | None = None) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=knowledge_sources or [],
    )


def ask_org_with(
    cards: list[AgentCard],
    intent: str,
    case_store: InMemoryConflictCaseStore | None = None,
    precedents: InMemoryPrecedentStore | None = None,
) -> AskOrg:
    registry = Registry()
    for c in cards:
        registry.register(c)
    classifier = FakeClassifier(intent)
    router = Router(registry, classifier, root_user="root", precedents=precedents)
    return AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        clock=fixed_clock,
        case_store=case_store,
    )


def test_Routed면_Answered로_담당과_출처가_붙는다():
    sources = ["위키/계약가이드", "Notion/FAQ"]
    c = card("contract_ops", ["계약 검토"], knowledge_sources=sources)
    ask = ask_org_with([c], "계약 검토")
    user = User(id="u1")

    reply = ask.handle("이 계약 조건 바꿔도 돼?", user)

    assert isinstance(reply, Answered)
    assert reply.answered_by[1] == "contract_ops"
    assert reply.sources == tuple(sources)
    assert reply.mode == "full"


def test_Unowned면_Pending_unowned로_안내만():
    c = card("contract_ops", ["계약 검토"])
    ask = ask_org_with([c], "주차장")
    user = User(id="u1")

    reply = ask.handle("주차장 정기권 어떻게 갱신해요?", user)

    assert isinstance(reply, Pending)
    assert reply.kind == "unowned"


def test_Contested면_Pending_contested():
    cards = [card("cs_ops", ["환불"]), card("sales_ops", ["환불"])]
    ask = ask_org_with(cards, "환불")
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Pending)
    assert reply.kind == "contested"


# ── T4.2 통합: case_store 주입 ─────────────────────────────────────────


def test_Contested_질문시_ConflictCase가_case_store에_생성된다():
    cs = card("cs_ops", ["환불"], owner="owner_CS")
    sales = card("sales_ops", ["환불"], owner="owner_Sales")
    case_store = InMemoryConflictCaseStore()
    ask = ask_org_with([cs, sales], "환불", case_store=case_store)
    user = User(id="u1")

    ask.handle("환불 되나요?", user)

    cases_cs = case_store.open_for_owner("owner_CS")
    cases_sales = case_store.open_for_owner("owner_Sales")
    assert len(cases_cs) == 1
    assert len(cases_sales) == 1
    assert cases_cs[0].case_id == cases_sales[0].case_id


def test_같은_intent_두_번_handle시_케이스는_1개만():
    cs = card("cs_ops", ["환불"], owner="owner_CS")
    sales = card("sales_ops", ["환불"], owner="owner_Sales")
    case_store = InMemoryConflictCaseStore()
    ask = ask_org_with([cs, sales], "환불", case_store=case_store)
    user = User(id="u1")

    ask.handle("환불 되나요?", user)
    ask.handle("환불 다시 물어봐요", user)

    all_cases = case_store.open_for_owner("owner_CS")
    assert len(all_cases) == 1


def test_Routed_질문은_case_store에_케이스_생성_안_함():
    c = card("cs_ops", ["계약 검토"])
    case_store = InMemoryConflictCaseStore()
    ask = ask_org_with([c], "계약 검토", case_store=case_store)
    user = User(id="u1")

    ask.handle("계약서 검토해줘", user)

    assert case_store.open_for_owner("D") == []


def test_Unowned_질문은_case_store에_케이스_생성_안_함():
    c = card("cs_ops", ["계약 검토"])
    case_store = InMemoryConflictCaseStore()
    ask = ask_org_with([c], "주차장", case_store=case_store)
    user = User(id="u1")

    ask.handle("주차장 어디예요?", user)

    assert case_store.history == []


def test_case_store_None이면_기존_Contested_동작_불변():
    cards = [card("cs_ops", ["환불"]), card("sales_ops", ["환불"])]
    ask = ask_org_with(cards, "환불", case_store=None)
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Pending)
    assert reply.kind == "contested"


# ── T4.2 완결: Contested→합의→Precedent→재질문시 Routed ─────────────────


def test_합의_후_같은_intent_재질문시_Routed로_자동_라우팅():
    """핵심 회귀 게이트: Contested→합의→Precedent→재질문 Routed."""
    cs = card("cs_ops", ["환불"], owner="owner_CS")
    sales = card("sales_ops", ["환불"], owner="owner_Sales")
    case_store = InMemoryConflictCaseStore()
    precedents = InMemoryPrecedentStore(clock=fixed_clock)
    ask = ask_org_with([cs, sales], "환불", case_store=case_store, precedents=precedents)
    user = User(id="u1")

    # 1) 첫 질문 → Contested → 케이스 생성
    first_reply = ask.handle("환불 되나요?", user)
    assert isinstance(first_reply, Pending)
    assert first_reply.kind == "contested"

    # 2) 케이스 조회
    open_cases = case_store.open_for_owner("owner_CS")
    assert len(open_cases) == 1
    case_id = open_cases[0].case_id

    # 3) ConsensusService로 전원 합의
    svc = ConsensusService(case_store=case_store, precedents=precedents)
    svc.concur(case_id, ConcurOnPrimary(by_owner="owner_CS", on_agent="cs_ops"))
    outcome = svc.concur(case_id, ConcurOnPrimary(by_owner="owner_Sales", on_agent="cs_ops"))
    assert isinstance(outcome, Agreed)

    # 4) 같은 intent 재질문 → Precedent 적용 → Routed
    second_reply = ask.handle("환불 정책이 어떻게 되나요?", user)
    assert isinstance(second_reply, Answered)
    assert second_reply.answered_by[1] == "cs_ops"


# ── T6.3 슬라이스2a 신규 — DispatchOutcome 비동기 결말 투영 ──────────────────

BASE_TS = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _ask_org_with_queue_dispatcher(
    cards: list[AgentCard],
    intent: str,
    dispatcher: RuntimeDispatcher,
) -> AskOrg:
    """RuntimeDispatcher 주입 AskOrg 조립 헬퍼(비동기 결말·회수 테스트용)."""
    registry = Registry()
    for c in cards:
        registry.register(c)
    classifier = FakeClassifier(intent)
    router = Router(registry, classifier, root_user="root")
    return AskOrg(
        router=router,
        dispatcher=dispatcher,
        audit_log=InMemoryAuditLog(),
        clock=fixed_clock,
    )


def test_AwaitingWorker일_때_Pending_dispatched가_반환된다():
    """dispatch 후 poll이 AwaitingWorker → OrgReply가 Pending(kind='dispatched')."""
    c = card("cs_ops", ["환불"])
    dispatcher = InMemoryWorkQueueDispatcher(clock=lambda: BASE_TS)
    ask = _ask_org_with_queue_dispatcher([c], "환불", dispatcher)
    user = User(id="u1")

    # 워커가 claim하지 않으므로 poll → AwaitingWorker
    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Pending)
    assert reply.kind == "dispatched"


def test_EscalatedToManager일_때_Pending_dispatched가_반환된다():
    """timeout 경과 clock 주입 → poll EscalatedToManager → Pending(kind='dispatched')."""
    c = card("cs_ops", ["환불"])

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        # 첫 호출(dispatch enqueued_at)은 BASE_TS, 이후(poll waited 계산)는 timeout 경과
        return BASE_TS if call_count == 1 else BASE_TS + timedelta(seconds=200)

    dispatcher = InMemoryWorkQueueDispatcher(
        clock=timeout_clock,
        timeout=timedelta(seconds=60),
    )
    ask = _ask_org_with_queue_dispatcher([c], "환불", dispatcher)
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Pending)
    assert reply.kind == "dispatched"


def test_Pending_dispatched에_manager_id_reason이_새지_않는다():
    """노출 불변식: Pending(dispatched)에 manager_id·reason 필드가 없다."""
    c = card("cs_ops", ["환불"])

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + timedelta(seconds=200)

    dispatcher = InMemoryWorkQueueDispatcher(
        clock=timeout_clock,
        timeout=timedelta(seconds=60),
        manager_of=lambda owner_id: "boss_" + owner_id,
    )
    ask = _ask_org_with_queue_dispatcher([c], "환불", dispatcher)
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Pending)
    # Pending 데이터클래스에 manager_id·reason 필드가 존재하지 않아야 한다
    assert not hasattr(reply, "manager_id")
    assert not hasattr(reply, "reason")
    assert not hasattr(reply, "ticket_id")
    assert not hasattr(reply, "waited")


# ── T6.3 슬라이스2b-i — 답 회수(retrieve) + 불투명 추적 토큰 ────────────────


def test_dispatched면_불투명_tracking_토큰이_실린다():
    """미회신(AwaitingWorker)이면 Pending(dispatched)에 tracking 토큰이 채워진다."""
    c = card("cs_ops", ["환불"])
    dispatcher = InMemoryWorkQueueDispatcher(clock=lambda: BASE_TS)
    ask = _ask_org_with_queue_dispatcher([c], "환불", dispatcher)

    reply = ask.handle("환불 되나요?", User(id="u1"))

    assert isinstance(reply, Pending)
    assert reply.kind == "dispatched"
    assert reply.tracking is not None
    assert len(reply.tracking) > 0


def test_tracking_토큰은_ticket_id를_노출하지_않는다():
    """불투명성: tracking 토큰이 내부 ticket_id와 다른 별도 ID여야 한다(구조 미인코딩)."""
    c = card("cs_ops", ["환불"])
    dispatcher = InMemoryWorkQueueDispatcher(clock=lambda: BASE_TS)
    ask = _ask_org_with_queue_dispatcher([c], "환불", dispatcher)

    reply = ask.handle("환불 되나요?", User(id="u1"))

    assert isinstance(reply, Pending)
    assert reply.tracking is not None
    # dispatcher.history에 쌓인 ticket_id·owner가 tracking에 인코딩돼 있지 않아야 한다.
    ticket = dispatcher.history[0]
    assert reply.tracking != ticket.ticket_id
    assert ticket.ticket_id not in reply.tracking
    assert ticket.owner_id not in reply.tracking


def test_retrieve가_회신_전에는_dispatched_같은_토큰을_유지한다():
    """워커가 아직 회신 안 했으면 retrieve는 Pending(dispatched, 같은 토큰)을 돌려준다."""
    c = card("cs_ops", ["환불"])
    dispatcher = WebSocketDispatcher(clock=lambda: BASE_TS)
    ask = _ask_org_with_queue_dispatcher([c], "환불", dispatcher)

    first = ask.handle("환불 되나요?", User(id="u1"))
    assert isinstance(first, Pending)
    assert first.tracking is not None

    again = ask.retrieve(first.tracking)
    assert isinstance(again, Pending)
    assert again.kind == "dispatched"
    assert again.tracking == first.tracking


def test_retrieve가_워커_회신_후_Answered를_돌려준다():
    """워커가 submit하면 같은 토큰으로 retrieve 시 Answered(담당·출처)로 투영된다."""
    c = card("cs_ops", ["환불"], owner="D", knowledge_sources=["위키/환불정책"])
    dispatcher = WebSocketDispatcher(clock=lambda: BASE_TS)
    ask = _ask_org_with_queue_dispatcher([c], "환불", dispatcher)

    pending = ask.handle("환불 되나요?", User(id="u1"))
    assert isinstance(pending, Pending)
    assert pending.tracking is not None

    # 워커가 (claim 후) 회신 — claim은 dispatch가 연결 없으면 안 했으므로 직접 claim.
    ticket = dispatcher.claim("D")
    assert ticket is not None
    dispatcher.submit(ticket.ticket_id, Answer(text="환불 가능합니다", sources=("위키/환불정책",), mode="full"))

    answered = ask.retrieve(pending.tracking)
    assert isinstance(answered, Answered)
    assert answered.text == "환불 가능합니다"
    assert answered.answered_by == ("D", "cs_ops")
    assert "위키/환불정책" in answered.sources


def test_retrieve가_모르는_토큰이면_None():
    """존재하지 않는 추적 토큰은 None(조회 실패)."""
    c = card("cs_ops", ["환불"])
    dispatcher = WebSocketDispatcher(clock=lambda: BASE_TS)
    ask = _ask_org_with_queue_dispatcher([c], "환불", dispatcher)

    assert ask.retrieve("없는토큰") is None


# ── T6.2 — intent 단일 출처화: ask_org가 decision.intent를 소비한다 ──────────


def test_AskOrg_생성자에_classifier_인자가_없다():
    """구조적 보장: AskOrg.__init__이 classifier 파라미터를 받지 않는다."""
    import inspect
    sig = inspect.signature(AskOrg.__init__)
    assert "classifier" not in sig.parameters


def test_ConflictCase가_decision_intent로_열린다():
    """Contested 분기: ConflictCase.intent == decision.intent (router가 채운 값)."""
    from agent_org_network.conflict import InMemoryConflictCaseStore

    cs = card("cs_ops", ["환불"])
    sales = card("sales_ops", ["환불"])
    case_store = InMemoryConflictCaseStore()
    ask = ask_org_with([cs, sales], "환불", case_store=case_store)
    user = User(id="u1")

    ask.handle("환불 되나요?", user)

    open_cases = case_store.open_for_owner("D")
    assert len(open_cases) == 1
    assert open_cases[0].intent == "환불"


def test_AuditEntry_intent가_decision_intent와_일치한다():
    """AuditEntry.intent == router가 decision에 실어준 intent (단일 출처)."""
    from agent_org_network.audit import InMemoryAuditLog

    c = card("contract_ops", ["계약 검토"])
    audit_log = InMemoryAuditLog()
    registry = Registry()
    registry.register(c)
    from agent_org_network.classifier import FakeClassifier
    classifier = FakeClassifier("계약 검토")
    router = Router(registry, classifier, root_user="root")
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=audit_log,
        clock=fixed_clock,
    )

    ask.handle("계약서 검토해줘", User(id="u1"))

    records = audit_log.records()
    assert len(records) == 1
    assert records[0]["intent"] == "계약 검토"


def test_Contested_AuditEntry_intent가_decision_intent와_일치한다():
    """Contested 경로: AuditEntry.intent == decision.intent."""
    from agent_org_network.audit import InMemoryAuditLog

    cs = card("cs_ops", ["환불"])
    sales = card("sales_ops", ["환불"])
    audit_log = InMemoryAuditLog()
    registry = Registry()
    registry.register(cs)
    registry.register(sales)
    from agent_org_network.classifier import FakeClassifier
    classifier = FakeClassifier("환불")
    router = Router(registry, classifier, root_user="root")
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=audit_log,
        clock=fixed_clock,
    )

    ask.handle("환불 되나요?", User(id="u1"))

    records = audit_log.records()
    assert len(records) == 1
    assert records[0]["intent"] == "환불"
