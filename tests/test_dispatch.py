"""T6.3 슬라이스 1 — InMemoryWorkQueueDispatcher + DispatchingRuntime 단위 테스트.

결정론: 고정 clock·ticket_id 주입, 실제 LLM·sleep·스레드 0.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from agent_org_network.agent_card import AgentCard
from agent_org_network.dispatch import (
    AwaitingWorker,
    Delivered,
    DispatchingRuntime,
    EscalatedToManager,
    InMemoryWorkQueueDispatcher,
    RuntimeDispatcher,
    WorkTicket,
)
from agent_org_network.runtime import Answer


# ── 공용 픽스처 ──────────────────────────────────────────────────────────────

def _fixed_clock(ts: datetime) -> Callable[[], datetime]:
    """단순 고정 시각 clock."""
    return lambda: ts


def _fixed_card(owner: str = "alice", agent_id: str = "cs_ops") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="고객 지원 담당",
        domains=["cs"],
        last_reviewed_at=date(2026, 6, 20),
    )


BASE_TS = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── ① dispatch → ticket (owner_id·agent_id·question 정확) ───────────────────

def test_dispatch가_WorkTicket을_반환하고_필드가_정확하다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice", agent_id="cs_ops")
    ticket = dispatcher.dispatch("배송은 얼마나 걸려요?", card)

    assert isinstance(ticket, WorkTicket)
    assert ticket.owner_id == "alice"
    assert ticket.agent_id == "cs_ops"
    assert ticket.question == "배송은 얼마나 걸려요?"
    assert ticket.enqueued_at == BASE_TS


def test_dispatch_후_history에_ticket이_쌓인다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card()
    t1 = dispatcher.dispatch("Q1", card)
    t2 = dispatcher.dispatch("Q2", card)

    assert t1 in dispatcher.history
    assert t2 in dispatcher.history
    assert len(dispatcher.history) == 2


# ── ② claim이 owner 큐에서 회수 ─────────────────────────────────────────────

def test_claim이_해당_owner_큐의_ticket을_반환한다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q1", card)

    claimed = dispatcher.claim("alice")
    assert claimed == ticket


def test_claim_후_같은_ticket은_다시_claim되지_않는다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")
    dispatcher.dispatch("Q1", card)

    dispatcher.claim("alice")
    second_claim = dispatcher.claim("alice")
    assert second_claim is None


def test_claim이_FIFO_순서로_반환한다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")
    t1 = dispatcher.dispatch("Q1", card)
    t2 = dispatcher.dispatch("Q2", card)

    first = dispatcher.claim("alice")
    second = dispatcher.claim("alice")
    assert first == t1
    assert second == t2


def test_해당_owner가_없으면_claim은_None을_반환한다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    assert dispatcher.claim("nobody") is None


# ── ③ submit 후 poll → Delivered ─────────────────────────────────────────────

def test_submit_후_poll이_Delivered를_반환한다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q1", card)
    dispatcher.claim("alice")

    answer = Answer(text="답변입니다", sources=(), mode="full")
    dispatcher.submit(ticket.ticket_id, answer)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.ticket == ticket
    assert outcome.answer == answer


def test_Delivered_answer_mode가_보존된다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card()
    ticket = dispatcher.dispatch("Q", card)
    dispatcher.claim(card.owner)

    answer = Answer(text="초안", sources=(), mode="draft_only")
    dispatcher.submit(ticket.ticket_id, answer)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "draft_only"


# ── ④ submit 전 poll → AwaitingWorker ───────────────────────────────────────

def test_submit_전_poll이_AwaitingWorker를_반환한다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card()
    ticket = dispatcher.dispatch("Q", card)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, AwaitingWorker)
    assert outcome.ticket == ticket


def test_AwaitingWorker의_waited가_경과시간을_담는다():
    waited_delta = timedelta(seconds=30)
    later_ts = BASE_TS + waited_delta

    # dispatch 시점 고정, poll 시점에 30초 경과
    call_count = 0
    def advancing_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else later_ts

    dispatcher = InMemoryWorkQueueDispatcher(clock=advancing_clock)
    card = _fixed_card()
    ticket = dispatcher.dispatch("Q", card)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, AwaitingWorker)
    assert outcome.waited == waited_delta


# ── ⑤ timeout 경과 clock → poll → EscalatedToManager ───────────────────────

def test_timeout_경과_후_poll이_EscalatedToManager를_반환한다():
    timeout = timedelta(seconds=60)
    elapsed = timedelta(seconds=61)

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + elapsed

    dispatcher = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timeout)
    card = _fixed_card()
    ticket = dispatcher.dispatch("Q", card)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)
    assert outcome.ticket == ticket


def test_EscalatedToManager_reason에_owner_정보가_담긴다():
    timeout = timedelta(seconds=1)
    elapsed = timedelta(seconds=10)

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + elapsed

    dispatcher = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timeout)
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)
    assert "alice" in outcome.reason


def test_EscalatedToManager_reason에_manager_정보가_담긴다():
    timeout = timedelta(seconds=1)
    elapsed = timedelta(seconds=10)

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + elapsed

    def manager_of(owner_id: str) -> str:
        return "boss_" + owner_id

    dispatcher = InMemoryWorkQueueDispatcher(
        clock=timeout_clock, timeout=timeout, manager_of=manager_of
    )
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)
    assert "boss_alice" in outcome.reason


# ── ⑥ DispatchingRuntime.answer가 동기 워커 시뮬로 Answer를 반환 ─────────────

def test_DispatchingRuntime_answer가_Answer를_반환한다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")

    canned_answer = Answer(text="동기 워커 답변", sources=(), mode="full")

    def fake_worker(d: RuntimeDispatcher) -> None:
        ticket = d.claim("alice")
        assert ticket is not None
        d.submit(ticket.ticket_id, canned_answer)

    runtime = DispatchingRuntime(dispatcher, worker=fake_worker)
    result = runtime.answer("Q?", card)

    assert isinstance(result, Answer)
    assert result.text == "동기 워커 답변"


def test_DispatchingRuntime_timeout_시_폴백_Answer를_반환한다():
    timeout = timedelta(seconds=1)
    elapsed = timedelta(seconds=10)

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + elapsed

    dispatcher = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timeout)
    card = _fixed_card(owner="alice")

    # worker가 submit 안 하면 timeout → EscalatedToManager → 폴백 Answer
    def no_op_worker(d: RuntimeDispatcher) -> None:
        pass

    runtime = DispatchingRuntime(dispatcher, worker=no_op_worker)
    result = runtime.answer("Q?", card)

    assert isinstance(result, Answer)
    assert result.mode == "full"


# ── ⑦ owner별 큐 격리 ───────────────────────────────────────────────────────

def test_alice_claim이_bob_ticket을_가져오지_않는다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    alice_card = _fixed_card(owner="alice", agent_id="cs_ops")
    bob_card = _fixed_card(owner="bob", agent_id="finance_ops")

    dispatcher.dispatch("Alice 질문", alice_card)
    bob_ticket = dispatcher.dispatch("Bob 질문", bob_card)

    # alice가 먼저 claim
    alice_claimed = dispatcher.claim("alice")
    # bob이 claim
    bob_claimed = dispatcher.claim("bob")

    assert alice_claimed is not None
    assert alice_claimed.owner_id == "alice"

    assert bob_claimed is not None
    assert bob_claimed == bob_ticket
    assert bob_claimed.owner_id == "bob"


def test_owner_큐가_완전히_독립되어_교차_간섭_없다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    alice_card = _fixed_card(owner="alice")
    bob_card = _fixed_card(owner="bob", agent_id="finance_ops")

    dispatcher.dispatch("A1", alice_card)
    dispatcher.dispatch("A2", alice_card)
    dispatcher.dispatch("B1", bob_card)

    # bob 큐를 먼저 비워도 alice 큐에 영향 없음
    dispatcher.claim("bob")
    assert dispatcher.claim("alice") is not None
    assert dispatcher.claim("alice") is not None
    assert dispatcher.claim("alice") is None  # alice 2개 소진


# ── ⑧ ticket_id 결정론 — WorkTicket에 ticket_id 명시 주입 ──────────────────

def test_WorkTicket에_ticket_id를_명시_주입하면_고정된다():
    ticket = WorkTicket(
        owner_id="alice",
        agent_id="cs_ops",
        question="Q",
        enqueued_at=BASE_TS,
        ticket_id="fixed-id-001",
    )
    assert ticket.ticket_id == "fixed-id-001"


def test_dispatch가_반환한_ticket_id로_submit_poll이_연결된다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card()
    ticket = dispatcher.dispatch("Q", card)

    answer = Answer(text="고정 답", sources=(), mode="full")
    dispatcher.submit(ticket.ticket_id, answer)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.text == "고정 답"


def test_두_dispatch_ticket_id가_서로_다르다():
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card()
    t1 = dispatcher.dispatch("Q1", card)
    t2 = dispatcher.dispatch("Q2", card)
    assert t1.ticket_id != t2.ticket_id


# ── ⑨ [blocker] escalation·회신 단조성 ─────────────────────────────────────

def test_escalation_후_늦은_submit이_Delivered로_되살아나지_않는다():
    """timeout으로 EscalatedToManager가 난 뒤 늦게 submit 해도 Delivered로 부활하면 안 됨."""
    timeout = timedelta(seconds=60)
    elapsed = timedelta(seconds=61)

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + elapsed

    dispatcher = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timeout)
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)

    # 첫 poll — timeout 경과 → EscalatedToManager (expired 상태로 고정)
    outcome1 = dispatcher.poll(ticket)
    assert isinstance(outcome1, EscalatedToManager)

    # 늦게 submit
    late_answer = Answer(text="늦은 답변", sources=(), mode="full")
    dispatcher.submit(ticket.ticket_id, late_answer)

    # 다시 poll — 이미 expired이므로 Delivered로 되살아나면 안 됨
    outcome2 = dispatcher.poll(ticket)
    assert isinstance(outcome2, EscalatedToManager), (
        f"expired 후 늦은 submit이 Delivered로 부활했음: {outcome2}"
    )


def test_한번_escalated되면_poll이_계속_EscalatedToManager다():
    """EscalatedToManager는 멱등 — 이후 poll마다 동일 결과."""
    timeout = timedelta(seconds=60)
    elapsed = timedelta(seconds=61)

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        # 첫 호출(dispatch)은 BASE_TS, 이후는 모두 timeout 경과
        return BASE_TS if call_count == 1 else BASE_TS + elapsed

    dispatcher = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timeout)
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)

    outcome1 = dispatcher.poll(ticket)
    assert isinstance(outcome1, EscalatedToManager)

    outcome2 = dispatcher.poll(ticket)
    assert isinstance(outcome2, EscalatedToManager)

    outcome3 = dispatcher.poll(ticket)
    assert isinstance(outcome3, EscalatedToManager)


def test_한번_Delivered면_poll이_계속_Delivered다():
    """Delivered는 단조 — 이후 poll마다 동일 결과."""
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)
    dispatcher.claim("alice")

    answer = Answer(text="답변", sources=(), mode="full")
    dispatcher.submit(ticket.ticket_id, answer)

    outcome1 = dispatcher.poll(ticket)
    assert isinstance(outcome1, Delivered)

    outcome2 = dispatcher.poll(ticket)
    assert isinstance(outcome2, Delivered)
    assert outcome2.answer == answer


def test_expired_ticket에_늦은_submit은_무시된다():
    """expired된 ticket_id로 submit이 와도 상태가 뒤집히지 않음."""
    timeout = timedelta(seconds=60)
    elapsed = timedelta(seconds=61)

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + elapsed

    dispatcher = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timeout)
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)

    dispatcher.poll(ticket)  # expired로 고정

    dispatcher.submit(ticket.ticket_id, Answer(text="무시되어야 함", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)


# ── ⑩ [major] DispatchOutcome 망라 — match 분기 커버리지 ─────────────────────

def test_DispatchingRuntime_answer가_Delivered_경로를_처리한다():
    """Delivered 분기: 워커가 정상 submit → answer 그대로 반환."""
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")

    canned = Answer(text="정상 답변", sources=(), mode="full")

    def fake_worker(d: RuntimeDispatcher) -> None:
        t = d.claim("alice")
        assert t is not None
        d.submit(t.ticket_id, canned)

    runtime = DispatchingRuntime(dispatcher, worker=fake_worker)
    result = runtime.answer("Q?", card)
    assert result.text == "정상 답변"


def test_DispatchingRuntime_answer가_EscalatedToManager_경로를_처리한다():
    """EscalatedToManager 분기: timeout → [escalated] 폴백."""
    timeout = timedelta(seconds=1)
    elapsed = timedelta(seconds=10)

    call_count = 0
    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + elapsed

    dispatcher = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timeout)
    card = _fixed_card(owner="alice")

    def no_op(d: RuntimeDispatcher) -> None:
        pass

    runtime = DispatchingRuntime(dispatcher, worker=no_op)
    result = runtime.answer("Q?", card)
    assert "[escalated]" in result.text


def test_DispatchingRuntime_answer가_AwaitingWorker_경로를_처리한다():
    """AwaitingWorker 분기: 워커가 claim만 하고 submit 안 함 → [awaiting] 폴백."""
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")

    def claim_only_worker(d: RuntimeDispatcher) -> None:
        d.claim("alice")  # submit 없이 claim만

    runtime = DispatchingRuntime(dispatcher, worker=claim_only_worker)
    result = runtime.answer("Q?", card)
    assert "[awaiting]" in result.text


# ── ⑪ [minor] answered 작업 재claim 방지 ────────────────────────────────────

def test_answered_작업은_다시_claim되지_않는다():
    """submit(answered) 완료된 작업은 claim이 다시 가져가지 않음."""
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")
    dispatcher.dispatch("Q", card)

    # claim → submit (answered 상태)
    claimed = dispatcher.claim("alice")
    assert claimed is not None
    dispatcher.submit(claimed.ticket_id, Answer(text="완료", sources=(), mode="full"))

    # answered 상태에서 다시 claim 시도
    second_claim = dispatcher.claim("alice")
    assert second_claim is None, f"answered 작업이 재claim됨: {second_claim}"
