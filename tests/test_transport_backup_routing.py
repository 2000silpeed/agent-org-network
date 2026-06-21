"""T6.6 슬라이스 i — 디스패처 등급 라우팅(owner 백업 폴백 push) 단위 테스트.

ADR 0012 결정 2(등급 라우팅)·결정 4(mode=backup 강제 하향). 전부 결정론:
고정 clock, Fake primary/backup send 콜백, 실 네트워크·실 claude·별 프로세스 0.

이번 슬라이스 범위 = *디스패처 라우팅만* — `WebSocketDispatcher._connections`를
owner당 등급별(primary/backup) 연결로 확장하고, 우선순위 push(primary 우선, 없으면
backup)와 백업 답의 mode=backup 강제를 검증한다. timeout 분배(6.6-ii)·staleness·
검토 루프·백업 배치는 다음 슬라이스(여기 없음).
"""

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from agent_org_network.agent_card import AgentCard
from agent_org_network.dispatch import (
    AwaitingWorker,
    Delivered,
    EscalatedToManager,
    InMemoryWorkQueueDispatcher,
)
from agent_org_network.runtime import Answer
from agent_org_network.transport import (
    CentralFrame,
    PushWork,
    RegisterWorker,
    WebSocketDispatcher,
)


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


def _pushes(rec: _Recorder) -> list[PushWork]:
    return [f for f in rec.sent if isinstance(f, PushWork)]


# ── ① primary 등록 → push → submit → Delivered, mode 보존 ────────────────────


def test_primary로_push된_답은_mode가_보존된다_full():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)

    ticket = dispatcher.dispatch("배송 문의", _fixed_card(owner="alice"))
    assert len(_pushes(primary)) == 1

    dispatcher.submit(ticket.ticket_id, Answer(text="2~3일", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "full"


def test_primary로_push된_답은_draft_only도_보존된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    dispatcher.submit(ticket.ticket_id, Answer(text="초안", sources=(), mode="draft_only"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "draft_only"


# ── ② primary 부재·backup 등록 → backup push → submit → mode=backup 강제 ─────


def test_primary_부재면_backup으로_push된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("배송 문의", _fixed_card(owner="alice"))

    pushes = _pushes(backup)
    assert len(pushes) == 1
    assert pushes[0].ticket.ticket_id == ticket.ticket_id


def test_backup이_full로_회신해도_mode가_backup으로_강제된다():
    """ADR 0012 결정 4 — 백업 사실은 연결 등급이 진실, 워커 자기보고 아님."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    # 워커가 full로 회신해도 디스패처가 backup으로 덮는다.
    dispatcher.submit(ticket.ticket_id, Answer(text="백업 답", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "backup"
    # 본문·출처는 보존, mode만 하향.
    assert outcome.answer.text == "백업 답"


def test_backup이_draft_only로_회신해도_backup이_우선된다():
    """backup이 draft_only를 우선(결정 4 — 백업 답은 어차피 owner 미검토라 더 강한 하향)."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    dispatcher.submit(ticket.ticket_id, Answer(text="초안", sources=(), mode="draft_only"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "backup"


# ── ③ primary·backup 동시 등록 → primary 우선 ───────────────────────────────


def test_primary_backup_동시_등록이면_primary로_push된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    assert len(_pushes(primary)) == 1
    assert _pushes(backup) == []


def test_primary_backup_동시면_submit_답_mode가_보존된다():
    """primary로 push됐으니 backup 강제 없음 — full 그대로."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    dispatcher.submit(ticket.ticket_id, Answer(text="primary 답", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "full"


# ── ④ primary·backup 둘 다 부재 → 큐 대기 → timeout → EscalatedToManager ─────


def test_둘_다_부재면_큐에_대기한다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, AwaitingWorker)


def test_둘_다_부재_timeout이면_EscalatedToManager로_종착한다():
    """미아 없음: 백업도 없으면 곧장 기존 escalation 종착(결정 1)."""
    call_count = 0

    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + timedelta(seconds=200)

    queue = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timedelta(seconds=60))
    dispatcher = WebSocketDispatcher(queue=queue)
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)


# ── ⑤ backup만 있다가 primary 등록되면 이후 push는 primary로 ─────────────────


def test_backup만_있다가_primary_등록되면_이후_push는_primary로():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    # primary 등록 — 아직 작업 없음.
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)

    # 이후 dispatch는 primary로.
    dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert len(_pushes(primary)) == 1
    assert _pushes(backup) == []


def test_backup_등록후_대기작업은_backup으로_primary가_나중에_와도_이미_backup():
    """backup 연결 시점에 대기 작업이 있으면 그 작업은 backup으로 push(재동기)."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))  # 미연결 적재

    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    pushes = _pushes(backup)
    assert len(pushes) == 1
    assert pushes[0].ticket.ticket_id == ticket.ticket_id


# ── ⑥ disconnect role별 제거 + re-queue → 남은 연결 재push ───────────────────


def test_primary_disconnect후_backup_남아있으면_requeue작업이_backup으로():
    """owner의 어느 워커가 끊겨도 그 owner claimed 작업 re-queue 후 남은 연결로 재push."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))  # primary로 push + claimed
    assert len(_pushes(primary)) == 1

    # primary 끊김 — claimed 작업 re-queue, 남은 backup으로 재push.
    requeued = dispatcher.disconnect("alice", role="primary")
    assert ticket in requeued

    pushes = _pushes(backup)
    assert len(pushes) == 1
    assert pushes[0].ticket.ticket_id == ticket.ticket_id


def test_backup_disconnect는_primary_연결을_지우지_않는다():
    """role별 제거 — backup만 끊겨도 primary 연결은 남는다."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    dispatcher.disconnect("alice", role="backup")

    # primary 연결은 살아 있으므로 새 작업이 primary로 push된다.
    dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert len(_pushes(primary)) == 1


def test_primary_disconnect후_backup으로_재push된_답은_mode_backup():
    """re-queue 후 backup으로 처리되면 그 답은 backup 하향(연결 등급이 진실)."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    dispatcher.disconnect("alice", role="primary")  # re-queue → backup으로 재push

    dispatcher.submit(ticket.ticket_id, Answer(text="백업 답", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "backup"


# ── ⑦ 중복 submit 멱등(첫 답 고정) ──────────────────────────────────────────


def test_backup_중복_submit이_첫_답을_덮어쓰지_않는다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    dispatcher.submit(ticket.ticket_id, Answer(text="첫 답", sources=(), mode="full"))
    dispatcher.submit(ticket.ticket_id, Answer(text="중복 답", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.text == "첫 답"
    assert outcome.answer.mode == "backup"


# ── ⑧ 하위호환: role 기본 primary ───────────────────────────────────────────


def test_role_미지정_RegisterWorker는_primary로_등록된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    worker = _Recorder()
    # role 없이 등록(하위호환) → primary로 취급.
    dispatcher.register(RegisterWorker(owner_id="alice"), worker)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    dispatcher.submit(ticket.ticket_id, Answer(text="답", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    # primary로 push됐으니 mode 보존(backup 강제 안 됨).
    assert outcome.answer.mode == "full"


def test_disconnect_role_미지정은_primary를_제거한다():
    """하위호환: disconnect(owner_id)만 부르면 primary 제거(기존 시그니처)."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    worker = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), worker)
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    requeued = dispatcher.disconnect("alice")
    assert ticket in requeued


# ── ⑨ _backup_tickets discard 검증 (경계 A·B·E) ─────────────────────────────


def test_경계A_backup_push후_primary_재push면_mode가_full이어야_한다():
    """경계 A: backup으로 push → backup disconnect → primary 등록·재push → primary full submit
    → outcome.answer.mode == 'full'.

    _backup_tickets에서 ticket_id가 discard되지 않으면 primary로 재push됐어도
    submit 시 backup으로 강제 하향되는 버그가 발생한다.
    ADR 0012 결정 4 정밀화 — 마지막으로 push된 등급이 진실.
    """
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    # 1. backup으로 push(ticket이 _backup_tickets에 추가됨)
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert len(_pushes(backup)) == 1

    # 2. backup 끊김 → release_claims로 re-queue(ticket은 _backup_tickets에 잔존 — 버그 지점)
    dispatcher.disconnect("alice", role="backup")

    # 3. primary 등록 → re-queue된 작업이 primary로 재push
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    assert len(_pushes(primary)) == 1

    # 4. primary가 full로 submit → mode가 full이어야 한다(마지막 push가 primary)
    dispatcher.submit(ticket.ticket_id, Answer(text="primary 실시간 답", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "full", (
        "primary로 재push됐으니 backup 강제 하향이 없어야 함 — "
        "_backup_tickets.discard(ticket_id) 누락 버그"
    )


def test_경계B_backup_submit_종착후_primary_재등록시_backup_강제_없어야_한다():
    """경계 B: backup push → submit(종착) → 이후 같은 ticket이 primary로 재push돼도
    backup 강제를 받지 않는다(종착 후 _backup_tickets 잔존 시 누수 — 행위로 검증).

    종착 후 _backup_tickets에 ticket_id가 영구 잔존하면 메모리 누수
    (2b-i _tracking 누수와 같은 클래스). submit 종착 후 discard가 필요하다.
    행위로 검증: backup으로 종착된 뒤 새 작업이 primary로 push돼 submit되면 mode가 full이어야 한다
    — 이전 backup ticket의 잔존이 새 작업에 영향을 주지 않는다는 것을 확인.
    (별개 ticket을 primary로 push해 _backup_tickets 누수 오염이 없음을 확인.)
    """
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    # 1. backup으로 push → submit → answered 종착
    ticket1 = dispatcher.dispatch("Q1", _fixed_card(owner="alice"))
    dispatcher.submit(ticket1.ticket_id, Answer(text="백업 답", sources=(), mode="full"))
    outcome1 = dispatcher.poll(ticket1)
    assert isinstance(outcome1, Delivered)
    assert outcome1.answer.mode == "backup"

    # 2. primary도 등록 → 이후 dispatch는 primary로 push(backup_tickets 누수 오염 없어야 함)
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)

    ticket2 = dispatcher.dispatch("Q2", _fixed_card(owner="alice"))
    assert len(_pushes(primary)) == 1  # primary로 push됨

    # 3. primary가 full로 submit → mode가 full이어야 한다(ticket1 종착 잔존이 ticket2에 없음)
    dispatcher.submit(ticket2.ticket_id, Answer(text="primary 답", sources=(), mode="full"))
    outcome2 = dispatcher.poll(ticket2)
    assert isinstance(outcome2, Delivered)
    assert outcome2.answer.mode == "full"


def test_경계E_primary_push후_primary_disconnect_후_backup_재push_submit은_backup이어야_한다():
    """경계 E(회귀 방지): primary push → primary disconnect → backup 재push → submit
    → mode == 'backup' (마지막이 backup이라 backup 유지 — ADR 8-2 부합).

    경계 A 수정(primary 재push 시 discard)이 이 동작을 깨지 않아야 한다.
    """
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    # 1. primary로 push
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert len(_pushes(primary)) == 1

    # 2. primary 끊김 → re-queue → backup이 있어 backup으로 재push
    dispatcher.disconnect("alice", role="primary")
    assert len(_pushes(backup)) == 1

    # 3. backup이 full로 submit → 마지막 push가 backup이라 mode=backup이어야 한다.
    dispatcher.submit(ticket.ticket_id, Answer(text="백업 답", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "backup"
