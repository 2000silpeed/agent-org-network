"""모니터 영구 pending 버그 — red → green 테스트 (설계: 변경 1·2·3).

테스트 구성:
  1. handle 상관키: 비동기 Routed → AuditEntry에 tracking 박힘. 동기/Contested/Unowned → None.
  2. retrieve answered 기록: FakeDispatcher(AwaitingWorker→Delivered 제어) — 멱등 포함.
  3. summarize: answered 엔트리 → answered=True·mode 채워짐.
  4. dedupe_audit_records: tracking 기반 dedup·순서·인덱스 보존.
  5. 무회귀: as_record에 tracking 키 추가 확인.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg, Pending
from agent_org_network.audit import AuditEntry, InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.decision import Routed
from agent_org_network.dispatch import (
    AwaitingWorker,
    Delivered,
    DispatchOutcome,
    WorkTicket,
)
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import Answer, StubRuntime
from agent_org_network.user import User
from agent_org_network.web import dedupe_audit_records, summarize_audit_record

_FIXED_DT = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _FIXED_DT


def _card(agent_id: str, domains: list[str], owner: str = "owner_A") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
    )


class FakeDispatcher:
    """AwaitingWorker → Delivered 전이를 제어할 수 있는 결정론 디스패처.

    초기 상태: dispatch 후 poll = AwaitingWorker.
    deliver(ticket_id, answer) 호출 후 poll = Delivered.
    claim/submit은 사용하지 않음(no-op).
    """

    def __init__(self) -> None:
        self._tickets: dict[str, WorkTicket] = {}
        self._answers: dict[str, Answer] = {}

    def dispatch(self, question: str, card: AgentCard, context: str | None = None) -> WorkTicket:
        ticket = WorkTicket(
            owner_id=card.owner,
            agent_id=card.agent_id,
            question=question,
            enqueued_at=_FIXED_DT,
        )
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def poll(self, ticket: WorkTicket) -> DispatchOutcome:
        if ticket.ticket_id in self._answers:
            return Delivered(ticket=ticket, answer=self._answers[ticket.ticket_id])
        return AwaitingWorker(ticket=ticket, waited=timedelta(seconds=1))

    def deliver(self, ticket_id: str, answer: Answer) -> None:
        """테스트가 디스패처를 Delivered 상태로 전이시킨다."""
        self._answers[ticket_id] = answer

    def claim(self, owner_id: str) -> WorkTicket | None:
        return None

    def submit(self, ticket_id: str, answer: Answer) -> None:
        pass


def _make_ask_org(
    cards: list[AgentCard],
    intent: str,
    dispatcher: FakeDispatcher | None = None,
    audit_log: InMemoryAuditLog | None = None,
) -> tuple[AskOrg, InMemoryAuditLog, FakeDispatcher]:
    registry = Registry()
    for c in cards:
        registry.register(c)
    classifier = FakeClassifier(intent)
    router = Router(registry, classifier, root_user="root")
    log = audit_log or InMemoryAuditLog()
    disp = dispatcher or FakeDispatcher()
    ask = AskOrg(
        router=router,
        dispatcher=disp,
        audit_log=log,
        clock=_fixed_clock,
    )
    return ask, log, disp


# ── 1. handle 상관키 ──────────────────────────────────────────────────────────


def test_handle_비동기_Routed_감사엔트리에_tracking이_박힌다() -> None:
    """비동기 경로(AwaitingWorker) Routed → AuditEntry.tracking이 Pending.tracking과 동일."""
    c = _card("cs_ops", ["환불"])
    ask, log, _ = _make_ask_org([c], "환불")
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    # 사용자향 Pending(dispatched) — tracking 토큰 있음
    assert isinstance(reply, Pending)
    assert reply.tracking is not None

    # 감사 엔트리에 같은 tracking이 박혀야 한다
    assert len(log.entries) == 1
    entry = log.entries[0]
    assert entry.tracking == reply.tracking


def test_handle_동기_Delivered_감사엔트리_tracking_None() -> None:
    """동기(LocalRuntimeDispatcher·Delivered) → AuditEntry.tracking=None."""
    from agent_org_network.dispatch import LocalRuntimeDispatcher

    c = _card("cs_ops", ["환불"])
    registry = Registry()
    registry.register(c)
    classifier = FakeClassifier("환불")
    router = Router(registry, classifier, root_user="root")
    log = InMemoryAuditLog()
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=log,
        clock=_fixed_clock,
    )
    user = User(id="u1")

    ask.handle("환불 되나요?", user)

    assert len(log.entries) == 1
    assert log.entries[0].tracking is None


def test_handle_Contested_감사엔트리_tracking_None() -> None:
    """Contested → dispatch 없음 → AuditEntry.tracking=None."""
    c1 = _card("cs_ops", ["환불"], owner="owner_A")
    c2 = _card("sales_ops", ["환불"], owner="owner_B")
    ask, log, _ = _make_ask_org([c1, c2], "환불")
    user = User(id="u1")

    ask.handle("환불 되나요?", user)

    assert len(log.entries) == 1
    assert log.entries[0].tracking is None


def test_handle_Unowned_감사엔트리_tracking_None() -> None:
    """Unowned → dispatch 없음 → AuditEntry.tracking=None."""
    c = _card("cs_ops", ["환불"])
    ask, log, _ = _make_ask_org([c], "주차장")
    user = User(id="u1")

    ask.handle("주차장 정기권?", user)

    assert len(log.entries) == 1
    assert log.entries[0].tracking is None


# ── 2. retrieve answered 기록 (멱등 포함) ─────────────────────────────────────


def test_retrieve_Delivered_전이_시_answered_엔트리_추가된다() -> None:
    """FakeDispatcher AwaitingWorker → Delivered → retrieve → answered 엔트리 1건 추가."""
    c = _card("cs_ops", ["환불"])
    ask, log, disp = _make_ask_org([c], "환불")
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)
    assert isinstance(reply, Pending)
    tracking = reply.tracking
    assert tracking is not None

    # dispatch 엔트리 1건 (AwaitingWorker)
    assert len(log.entries) == 1
    assert log.entries[0].tracking == tracking

    # 아직 awaiting — retrieve 해도 새 엔트리 없음
    result = ask.retrieve(tracking)
    assert isinstance(result, Pending)
    assert len(log.entries) == 1

    # Delivered로 전이 → retrieve → answered 엔트리 추가
    answer = Answer(text="환불 가능합니다", mode="full")
    ticket = log.entries[0].dispatch_outcome
    assert isinstance(ticket, AwaitingWorker)
    disp.deliver(ticket.ticket.ticket_id, answer)

    result2 = ask.retrieve(tracking)
    from agent_org_network.ask_org import Answered
    assert isinstance(result2, Answered)
    assert result2.text == "환불 가능합니다"

    # answered 엔트리가 1건 추가돼야 한다
    assert len(log.entries) == 2
    answered_entry = log.entries[1]
    assert answered_entry.tracking == tracking
    assert isinstance(answered_entry.dispatch_outcome, Delivered)
    assert answered_entry.dispatch_outcome.answer.text == "환불 가능합니다"


def test_retrieve_반복_폴링_answered_엔트리_1건만_추가된다() -> None:
    """retrieve를 여러 번 호출해도 answered 엔트리는 정확히 1건(_answered_recorded 멱등)."""
    c = _card("cs_ops", ["환불"])
    ask, log, disp = _make_ask_org([c], "환불")
    user = User(id="u1")

    reply = ask.handle("환불?", user)
    assert isinstance(reply, Pending)
    tracking = reply.tracking
    assert tracking is not None

    # Delivered로 전이
    ticket_entry = log.entries[0]
    assert isinstance(ticket_entry.dispatch_outcome, AwaitingWorker)
    answer = Answer(text="OK", mode="full")
    disp.deliver(ticket_entry.dispatch_outcome.ticket.ticket_id, answer)

    # 첫 retrieve → answered 엔트리 추가
    ask.retrieve(tracking)
    assert len(log.entries) == 2

    # 두 번째 retrieve → 추가 없음
    ask.retrieve(tracking)
    assert len(log.entries) == 2

    # 세 번째 retrieve → 추가 없음
    ask.retrieve(tracking)
    assert len(log.entries) == 2


def test_retrieve_awaiting_상태에서는_엔트리_추가_없음() -> None:
    """Delivered 전이 전 retrieve(AwaitingWorker) → 새 엔트리 없음."""
    c = _card("cs_ops", ["환불"])
    ask, log, _ = _make_ask_org([c], "환불")
    user = User(id="u1")

    reply = ask.handle("환불?", user)
    assert isinstance(reply, Pending)
    tracking = reply.tracking
    assert tracking is not None

    ask.retrieve(tracking)
    ask.retrieve(tracking)
    assert len(log.entries) == 1  # dispatch 엔트리만


# ── 3. summarize — answered 엔트리 → answered=True ────────────────────────────


def test_summarize_answered_엔트리_answered_True_mode_채워짐() -> None:
    """Delivered AuditEntry → summarize_audit_record → answered=True, mode 채워짐."""
    ticket = WorkTicket(
        owner_id="owner_A",
        agent_id="cs_ops",
        question="환불?",
        enqueued_at=_FIXED_DT,
    )
    answer = Answer(text="환불 됩니다", mode="full")
    outcome = Delivered(ticket=ticket, answer=answer)
    card = _card("cs_ops", ["환불"])
    decision = Routed(primary=card, confidence=1.0, reason="test")
    entry = AuditEntry(
        timestamp=_FIXED_DT,
        user_id="u1",
        question="환불?",
        intent="환불",
        decision=decision,
        dispatch_outcome=outcome,
        tracking="tok_abc",
    )

    record = entry.as_record()
    summary = summarize_audit_record(0, record)

    assert summary["answered"] is True
    assert summary["mode"] == "full"


def test_summarize_AwaitingWorker_엔트리_answered_False() -> None:
    """AwaitingWorker AuditEntry → summarize → answered=False."""
    ticket = WorkTicket(
        owner_id="owner_A",
        agent_id="cs_ops",
        question="환불?",
        enqueued_at=_FIXED_DT,
    )
    outcome = AwaitingWorker(ticket=ticket, waited=timedelta(seconds=0))
    card = _card("cs_ops", ["환불"])
    decision = Routed(primary=card, confidence=1.0, reason="test")
    entry = AuditEntry(
        timestamp=_FIXED_DT,
        user_id="u1",
        question="환불?",
        intent="환불",
        decision=decision,
        dispatch_outcome=outcome,
        tracking="tok_abc",
    )

    record = entry.as_record()
    summary = summarize_audit_record(0, record)

    assert summary["answered"] is False


# ── 4. dedupe_audit_records ───────────────────────────────────────────────────


def _make_record(
    question: str,
    tracking: str | None = None,
    answered: bool = False,
) -> dict[str, Any]:
    """테스트용 최소 감사 레코드."""
    answer: dict[str, Any] | None = {"text": "답", "mode": "full", "sources": []} if answered else None
    return {
        "timestamp": _FIXED_DT.isoformat(),
        "user_id": "u1",
        "question": question,
        "intent": "환불",
        "decision": {"disposition": "routed", "primary": "cs_ops"},
        "answer": answer,
        "dispatch": {"disposition": "delivered" if answered else "awaiting_worker"},
        "tracking": tracking,
    }


def test_dedupe_tracking_있는_레코드_마지막만_남긴다() -> None:
    """같은 tracking → 마지막(answered) 레코드만 남기고 앞(pending dispatch)은 제거."""
    pending = _make_record("환불?", tracking="T1", answered=False)
    answered = _make_record("환불?", tracking="T1", answered=True)
    no_track = _make_record("주차장?", tracking=None, answered=False)

    records = [pending, answered, no_track]
    result = dedupe_audit_records(records)

    # answered(원래 인덱스 1)과 no_track(원래 인덱스 2)만 남아야 한다
    assert len(result) == 2
    idx0, rec0 = result[0]
    idx1, rec1 = result[1]
    assert idx0 == 1  # 원래 인덱스 보존
    assert rec0["answer"] is not None  # answered
    assert idx1 == 2
    assert rec1["question"] == "주차장?"


def test_dedupe_tracking_없는_레코드는_그대로() -> None:
    """tracking이 None/없는 레코드는 dedup 대상 아님 — 그대로 유지."""
    r0 = _make_record("계약 검토?", tracking=None, answered=True)
    r1 = _make_record("주차장?", tracking=None, answered=False)

    result = dedupe_audit_records([r0, r1])

    assert len(result) == 2
    assert result[0] == (0, r0)
    assert result[1] == (1, r1)


def test_dedupe_여러_tracking_그룹_독립_처리() -> None:
    """T1, T2 두 그룹 각각 마지막만 남긴다."""
    t1_pending = _make_record("Q1?", tracking="T1", answered=False)
    t2_pending = _make_record("Q2?", tracking="T2", answered=False)
    t1_answered = _make_record("Q1?", tracking="T1", answered=True)
    t2_answered = _make_record("Q2?", tracking="T2", answered=True)

    result = dedupe_audit_records([t1_pending, t2_pending, t1_answered, t2_answered])

    # t1_answered(인덱스 2), t2_answered(인덱스 3) 2건
    assert len(result) == 2
    indices = [idx for idx, _ in result]
    assert 2 in indices
    assert 3 in indices


def test_dedupe_빈_입력은_빈_출력() -> None:
    assert dedupe_audit_records([]) == []


def test_dedupe_tracking_없는_키_자체() -> None:
    """tracking 키가 아예 없는 레코드도 None과 동일하게 처리(그대로)."""
    r: dict[str, Any] = {"timestamp": "t", "question": "Q", "answer": None, "decision": {}, "dispatch": None}
    result = dedupe_audit_records([r])
    assert len(result) == 1
    assert result[0] == (0, r)


# ── 5. as_record에 tracking 키 추가 무회귀 ────────────────────────────────────


def test_AuditEntry_tracking_None이면_as_record에_tracking_None_키_있다() -> None:
    """tracking 기본 None → as_record에 tracking 키가 None으로 있어야 한다."""
    card = _card("cs_ops", ["환불"])
    decision = Routed(primary=card, confidence=1.0, reason="test")
    entry = AuditEntry(
        timestamp=_FIXED_DT,
        user_id="u1",
        question="환불?",
        intent="환불",
        decision=decision,
    )
    record = entry.as_record()
    assert "tracking" in record
    assert record["tracking"] is None


def test_AuditEntry_tracking_있으면_as_record에_그대로_실린다() -> None:
    """tracking 값이 있으면 as_record의 tracking 키에 그대로."""
    card = _card("cs_ops", ["환불"])
    decision = Routed(primary=card, confidence=1.0, reason="test")
    ticket = WorkTicket(owner_id="owner_A", agent_id="cs_ops", question="환불?", enqueued_at=_FIXED_DT)
    outcome = Delivered(ticket=ticket, answer=Answer(text="OK", mode="full"))
    entry = AuditEntry(
        timestamp=_FIXED_DT,
        user_id="u1",
        question="환불?",
        intent="환불",
        decision=decision,
        dispatch_outcome=outcome,
        tracking="my_token_abc",
    )
    record = entry.as_record()
    assert record["tracking"] == "my_token_abc"


# ── 6. 통합: handle→retrieve 흐름 모니터 목록 answered 보임 ──────────────────


def test_monitor_목록_handle_retrieve_후_answered_True로_보인다() -> None:
    """end-to-end: handle(pending) → retrieve(Delivered) → dedupe된 목록에서 answered=True."""
    c = _card("cs_ops", ["환불"])
    ask, log, disp = _make_ask_org([c], "환불")
    user = User(id="u1")

    reply = ask.handle("환불?", user)
    assert isinstance(reply, Pending)
    tracking = reply.tracking
    assert tracking is not None

    # 디스패치 엔트리만 있음 → dedup 후 목록에서 answered=False
    records_before = log.records()
    deduped_before = dedupe_audit_records(records_before)
    summaries_before = [summarize_audit_record(i, r) for i, r in deduped_before]
    assert len(summaries_before) == 1
    assert summaries_before[0]["answered"] is False

    # Delivered 전이 후 retrieve
    ticket_entry = log.entries[0]
    assert isinstance(ticket_entry.dispatch_outcome, AwaitingWorker)
    answer = Answer(text="환불 가능합니다", mode="full")
    disp.deliver(ticket_entry.dispatch_outcome.ticket.ticket_id, answer)
    ask.retrieve(tracking)

    # dedup 후 목록에서 answered=True (answered 엔트리만)
    records_after = log.records()
    deduped_after = dedupe_audit_records(records_after)
    summaries_after = [summarize_audit_record(i, r) for i, r in deduped_after]
    assert len(summaries_after) == 1
    assert summaries_after[0]["answered"] is True
    assert summaries_after[0]["mode"] == "full"
