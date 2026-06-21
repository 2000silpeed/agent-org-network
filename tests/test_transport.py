"""T6.3 슬라이스2b-i — 전송 프레임↔도메인 변환 + WebSocketDispatcher 단위 테스트.

전부 결정론: 고정 clock·ticket_id 주입, Fake send 콜백, 실 네트워크·실 claude·스레드 0.
WebSocketDispatcher는 InMemoryWorkQueueDispatcher를 *합성*하므로(상속 아님), 큐 도메인
자체(단조 종착·격리 등)는 슬라이스1 test_dispatch가 커버한다. 여기선 *전송층*만 검증한다 —
프레임 변환, 연결 레지스트리 push, 끊김 re-queue, 인증 hook.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from agent_org_network.agent_card import AgentCard
from agent_org_network.dispatch import (
    AwaitingWorker,
    Delivered,
    InMemoryWorkQueueDispatcher,
    WorkTicket,
)
from agent_org_network.runtime import Answer
from agent_org_network.transport import (
    AnswerFrame,
    AuthError,
    CentralFrame,
    PushWork,
    RegisterWorker,
    TicketFrame,
    WebSocketDispatcher,
    Welcome,
    from_answer_frame,
    from_ticket_frame,
    to_answer_frame,
    to_ticket_frame,
)


# ── 공용 픽스처 ──────────────────────────────────────────────────────────────


def _fixed_clock(ts: datetime) -> Callable[[], datetime]:
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


class _Recorder:
    """워커 소켓 send 콜백 stub — 내보낸 프레임을 기록만 한다(Fake 워커 측면)."""

    def __init__(self) -> None:
        self.sent: list[CentralFrame] = []

    def __call__(self, frame: CentralFrame) -> None:
        self.sent.append(frame)


# ── ① 프레임 ↔ 도메인 변환 4종 ──────────────────────────────────────────────


def test_to_ticket_frame이_WorkTicket을_와이어로_투영하고_owner를_생략한다():
    ticket = WorkTicket(
        owner_id="alice",
        agent_id="cs_ops",
        question="배송 얼마나 걸려요?",
        enqueued_at=BASE_TS,
        ticket_id="tid-1",
    )
    frame = to_ticket_frame(ticket)

    assert isinstance(frame, TicketFrame)
    assert frame.ticket_id == "tid-1"
    assert frame.agent_id == "cs_ops"
    assert frame.question == "배송 얼마나 걸려요?"
    assert frame.enqueued_at == BASE_TS
    # owner_id는 연결 귀속이라 프레임에 실리지 않는다(6-3).
    assert not hasattr(frame, "owner_id")


def test_from_ticket_frame이_연결_owner를_붙여_WorkTicket을_복원한다():
    frame = TicketFrame(
        ticket_id="tid-2",
        agent_id="cs_ops",
        question="Q",
        enqueued_at=BASE_TS,
    )
    ticket = from_ticket_frame(frame, owner_id="alice")

    assert isinstance(ticket, WorkTicket)
    assert ticket.owner_id == "alice"
    assert ticket.ticket_id == "tid-2"
    assert ticket.agent_id == "cs_ops"
    assert ticket.question == "Q"
    assert ticket.enqueued_at == BASE_TS


def test_ticket_round_trip이_owner를_보존한다():
    original = WorkTicket(
        owner_id="bob",
        agent_id="finance_ops",
        question="견적 어떻게?",
        enqueued_at=BASE_TS,
        ticket_id="tid-3",
    )
    restored = from_ticket_frame(to_ticket_frame(original), owner_id="bob")
    assert restored == original


def test_to_answer_frame이_Answer를_와이어로_투영하고_mode를_보존한다():
    answer = Answer(text="초안입니다", sources=("위키/환불정책",), mode="draft_only")
    frame = to_answer_frame(answer)

    assert isinstance(frame, AnswerFrame)
    assert frame.text == "초안입니다"
    assert frame.sources == ("위키/환불정책",)
    assert frame.mode == "draft_only"


def test_from_answer_frame이_Answer를_복원하고_mode를_보존한다():
    frame = AnswerFrame(text="답입니다", sources=("출처1", "출처2"), mode="full")
    answer = from_answer_frame(frame)

    assert isinstance(answer, Answer)
    assert answer.text == "답입니다"
    assert answer.sources == ("출처1", "출처2")
    assert answer.mode == "full"


def test_answer_round_trip이_값을_보존한다():
    original = Answer(text="t", sources=("a", "b"), mode="draft_only")
    restored = from_answer_frame(to_answer_frame(original))
    assert restored == original


# ── ② dispatch — 연결된 워커에 push, 미연결이면 큐 대기 ──────────────────────


def test_연결된_워커에게_dispatch가_PushWork를_보낸다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)

    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("배송 문의", card)

    push = [f for f in rec.sent if isinstance(f, PushWork)]
    assert len(push) == 1
    assert push[0].ticket.ticket_id == ticket.ticket_id
    assert push[0].ticket.question == "배송 문의"


def test_미연결_워커면_dispatch는_push없이_큐에_대기한다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    # 아무도 register 안 함 → 연결 레지스트리 비어 있음.
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("배송 문의", card)

    # poll하면 아직 회신 없음 → AwaitingWorker(큐에 대기).
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, AwaitingWorker)


def test_나중에_연결되면_대기작업이_그_워커에게_push된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("배송 문의", card)  # 미연결 상태 적재

    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)

    push = [f for f in rec.sent if isinstance(f, PushWork)]
    assert len(push) == 1
    assert push[0].ticket.ticket_id == ticket.ticket_id


def test_다른_owner_연결은_내_작업을_받지_않는다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    alice_card = _fixed_card(owner="alice")
    dispatcher.dispatch("alice 작업", alice_card)

    bob_rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="bob"), bob_rec)

    # bob이 연결돼도 alice 작업은 push되지 않는다(owner별 격리).
    assert [f for f in bob_rec.sent if isinstance(f, PushWork)] == []


# ── ③ poll — 내부 큐 위임 ───────────────────────────────────────────────────


def test_submit_후_poll이_Delivered를_반환한다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)

    answer = Answer(text="회신", sources=(), mode="full")
    dispatcher.submit(ticket.ticket_id, answer)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer == answer


# ── ④ claim/submit — 큐 위임 ────────────────────────────────────────────────


def test_claim이_큐에_위임되어_ticket을_반환한다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)

    claimed = dispatcher.claim("alice")
    assert claimed == ticket


# ── ⑤ register — 인증 hook ──────────────────────────────────────────────────


def test_register가_Welcome을_반환하고_레지스트리에_올린다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()
    reply = dispatcher.register(RegisterWorker(owner_id="alice"), rec)

    assert isinstance(reply, Welcome)
    # 등록됐으므로 이후 dispatch가 push된다.
    dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert any(isinstance(f, PushWork) for f in rec.sent)


def test_미인증_owner_id_빈값은_AuthError로_거부된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()
    reply = dispatcher.register(RegisterWorker(owner_id=""), rec)

    assert isinstance(reply, AuthError)
    # 거부됐으므로 레지스트리에 없다 — dispatch해도 push 없음.
    dispatcher.dispatch("Q", _fixed_card(owner=""))
    assert [f for f in rec.sent if isinstance(f, PushWork)] == []


# ── ⑥ disconnect — claimed 작업 re-queue ────────────────────────────────────


def test_disconnect가_claimed_작업을_re_queue한다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)  # 연결돼 있으므로 push + claimed

    requeued = dispatcher.disconnect("alice")
    assert ticket in requeued

    # re-queue됐으므로 다시 claim 가능(미아 없음).
    again = dispatcher.claim("alice")
    assert again is not None
    assert again.ticket_id == ticket.ticket_id


def test_disconnect_후_재연결하면_작업이_다시_push된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec1 = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec1)
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)

    dispatcher.disconnect("alice")

    # 재연결 — 대기 작업이 새 소켓으로 다시 push된다.
    rec2 = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec2)

    push = [f for f in rec2.sent if isinstance(f, PushWork)]
    assert len(push) == 1
    assert push[0].ticket.ticket_id == ticket.ticket_id


def test_disconnect가_answered_작업은_되살리지_않는다():
    """단조성: 이미 회신된 작업은 끊김으로 re-queue되지 않는다."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)
    dispatcher.submit(ticket.ticket_id, Answer(text="회신", sources=(), mode="full"))

    requeued = dispatcher.disconnect("alice")
    assert ticket not in requeued

    # 여전히 Delivered(부활/재큐 없음).
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)


# ── ⑦ 중복 submit 멱등 (큐 위임으로 보장) ───────────────────────────────────


def test_중복_submit이_첫_답을_덮어쓰지_않는다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)
    card = _fixed_card(owner="alice")
    ticket = dispatcher.dispatch("Q", card)

    first = Answer(text="첫 답", sources=(), mode="full")
    second = Answer(text="중복 답", sources=(), mode="full")
    dispatcher.submit(ticket.ticket_id, first)
    dispatcher.submit(ticket.ticket_id, second)

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.text == "첫 답"


# ── ⑧ 주입 queue로 timeout escalation 위임 확인 ─────────────────────────────


def test_timeout이면_poll이_EscalatedToManager로_종착한다():
    from agent_org_network.dispatch import EscalatedToManager

    call_count = 0

    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + timedelta(seconds=200)

    queue = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timedelta(seconds=60))
    dispatcher = WebSocketDispatcher(queue=queue)
    # 미연결 상태로 적재(push 안 됨) → timeout 경과.
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)
