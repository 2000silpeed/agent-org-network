"""T6.6 슬라이스 ii — timeout 분배(결정 8) + staleness 거부(결정 9) 단위 테스트.

ADR 0012 결정 8(t1/t2 2단 예산·primary 무응답 t1 후 backup 전환)·결정 9(stale 위임
거부→escalation). 전부 결정론: 주입 clock, Fake primary/backup send 콜백, 실 네트워크·
실 claude·별 프로세스 0. 6.6-i(등급 라우팅)는 이미 그린이고 여기선 그 위에 timeout
분배와 staleness 정책만 얹는다.

핵심 검증:
  - primary 연결됐으나 t1 무응답 → claim 회수 → primary 제외하고 backup으로 재push →
    backup submit → Delivered(mode=backup).
  - primary·backup 둘 다 무응답 → t2(전체 timeout) 후 EscalatedToManager.
  - 위임 스냅샷 stale(snapshot_at 오래) → backup 연결 있어도 push 안 함 → 큐 대기 →
    timeout escalation.
  - 위임 fresh → backup push 정상(mode=backup).
  - 위임 없음 → backup 건너뜀 → escalation.

슬라이스 ii 보강(code-reviewer Blocker/Major 회귀 — 다중 ticket):
  - head-of-line: 거부 작업(backup 부재) 하나가 뒤의 정상 작업(primary 연결됨)의 push를
    막지 않는다(Blocker 1, `claimable`/`claim_ticket` 기반 재설계).
  - primary 회복: t1 회수로 primary 제외 표식이 붙은 작업이, primary 재연결 후 다시
    primary로 push된다(Blocker 2, `_primary_exhausted` "1회 한정 제외").
  - agent_ids 대조: 위임 대상 외 카드 질문은 backup이 답하지 않고 escalation(Major 1).
"""

import pytest

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from agent_org_network.agent_card import AgentCard
from agent_org_network.dispatch import (
    AwaitingWorker,
    DelegationSnapshot,
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


class _MutableClock:
    """현재 시각을 외부에서 직접 정하는 결정론 clock(호출 횟수 비의존).

    `now` 필드를 테스트가 직접 진전시켜 "이 시점에 poll하면" 시나리오를 만든다.
    호출 횟수에 무관해(dispatch/claim/poll이 clock을 몇 번 부르든) 안정적이다.
    """

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# ── ① timeout 분배(결정 8): primary 연결+무응답 t1 경과 → backup 재push ──────


def test_primary_무응답_t1_경과시_backup으로_재push된다():
    """primary가 연결돼 push받았으나(claimed) t1 안에 답 없음 → 회수 후 backup 재push.

    결정 8-1 (b): primary 연결됐으나 무응답은 t1 후 backup 전환. 핵심 — 현재 구현은
    primary가 연결돼 있으면 늘 primary를 고르므로 backup 전환이 안 된다(6.6-i Minor).
    """
    # dispatch+claim 시각=BASE_TS, 이후 poll 시점에 t1 초과로 진전.
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=120),
        t1=timedelta(seconds=40),
    )
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("배송 문의", _fixed_card(owner="alice"))
    assert len(_pushes(primary)) == 1  # 처음엔 primary로
    assert _pushes(backup) == []

    # t1(40s) 초과·전체 timeout(120s) 전 시점에 poll — primary claim 회수 + backup 재push.
    clock.now = BASE_TS + timedelta(seconds=50)
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, AwaitingWorker)  # 아직 backup 답 전
    assert len(_pushes(backup)) == 1, "t1 경과 후 backup으로 재push되어야 함"
    assert _pushes(backup)[0].ticket.ticket_id == ticket.ticket_id


def test_t1_경과_backup_재push후_backup_submit이면_Delivered_mode_backup():
    """t1 후 backup 전환 → backup이 답하면 Delivered(mode=backup, 연결 등급이 진실)."""
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=120),
        t1=timedelta(seconds=40),
    )
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    clock.now = BASE_TS + timedelta(seconds=50)
    dispatcher.poll(ticket)  # t1 경과 → backup 전환

    # backup이 full로 회신해도 backup으로 강제(결정 4).
    dispatcher.submit(ticket.ticket_id, Answer(text="백업 답", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "backup"
    assert outcome.answer.text == "백업 답"


def test_t1_경과_전이면_primary로_그대로_둔다():
    """t1 전이면 primary claim 유지 — backup 전환 없음(primary 우선 보존)."""
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=120),
        t1=timedelta(seconds=40),
    )
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    clock.now = BASE_TS + timedelta(seconds=20)
    outcome = dispatcher.poll(ticket)  # t1(40s) 전(20s)

    assert isinstance(outcome, AwaitingWorker)
    assert _pushes(backup) == [], "t1 전엔 backup 전환 없어야 함"
    assert len(_pushes(primary)) == 1


def test_t1_경과후_primary가_답해도_먼저_온_답이_고정된다():
    """결정 8-2: primary가 t1 후 늦게 답해도 backup이 먼저 답했으면 backup 답 고정(멱등)."""
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=120),
        t1=timedelta(seconds=40),
    )
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    clock.now = BASE_TS + timedelta(seconds=50)
    dispatcher.poll(ticket)  # t1 경과 → backup 전환

    # backup이 먼저 답
    dispatcher.submit(ticket.ticket_id, Answer(text="백업 답", sources=(), mode="full"))
    # primary가 뒤늦게 답 — 멱등이라 무시
    dispatcher.submit(ticket.ticket_id, Answer(text="primary 늦은 답", sources=(), mode="full"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.text == "백업 답"
    assert outcome.answer.mode == "backup"


# ── ② timeout 분배: primary·backup 둘 다 무응답 → t2(전체 timeout) escalation ──


def test_primary_backup_둘다_무응답이면_t2후_EscalatedToManager():
    """primary t1 후 backup 전환 → backup도 무응답 → 전체 timeout(t2 합류) escalation.

    미아 없음: 백업도 못 답하면 기존 escalation 종착(결정 1).
    """
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=120),
        t1=timedelta(seconds=40),
    )
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    clock.now = BASE_TS + timedelta(seconds=50)  # t1 경과·전체 timeout 전
    out1 = dispatcher.poll(ticket)  # t1 경과 → backup 전환(아직 대기)
    assert isinstance(out1, AwaitingWorker)
    assert len(_pushes(backup)) == 1

    clock.now = BASE_TS + timedelta(seconds=130)  # 전체 timeout(120s) 초과
    out2 = dispatcher.poll(ticket)  # → escalation
    assert isinstance(out2, EscalatedToManager)


def test_t1_미설정이면_기존_단일_timeout_동작_유지():
    """t1 미주입(하위호환)이면 backup 전환 없이 기존 단일 timeout escalation만(회귀 방지)."""
    call_count = 0

    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + timedelta(seconds=200)

    queue = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timedelta(seconds=60))
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)
    # t1 미설정 → backup 전환 없음(primary로만 갔다가 escalation).
    assert _pushes(backup) == []


# ── ③ staleness 거부(결정 9): stale 위임은 backup push 안 함 ──────────────────


def _stale_snapshot(owner: str = "alice", days_old: int = 30) -> DelegationSnapshot:
    return DelegationSnapshot(
        owner_id=owner,
        agent_ids=("cs_ops",),
        snapshot_at=BASE_TS - timedelta(days=days_old),
    )


def _fresh_snapshot(owner: str = "alice") -> DelegationSnapshot:
    return DelegationSnapshot(
        owner_id=owner,
        agent_ids=("cs_ops",),
        snapshot_at=BASE_TS - timedelta(hours=1),
    )


def test_위임_stale이면_backup_연결있어도_push안하고_큐대기():
    """결정 9: snapshot_at 임계 초과면 backup push 안 함 → 큐 대기(→ escalation)."""
    queue = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    dispatcher = WebSocketDispatcher(
        queue=queue,
        staleness_threshold=timedelta(days=7),
    )
    dispatcher.register_delegation(_stale_snapshot("alice", days_old=30))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    # backup 연결이 있어도 stale이라 push 안 됨.
    assert _pushes(backup) == [], "stale 위임이면 backup push 거부(escalation으로)"
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, AwaitingWorker)


def test_위임_stale이면_timeout시_escalation으로_종착():
    """stale 거부 후 큐 대기 → timeout → EscalatedToManager(미아 없음)."""
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock, timeout=timedelta(seconds=60))
    dispatcher = WebSocketDispatcher(
        queue=queue,
        staleness_threshold=timedelta(days=7),
    )
    dispatcher.register_delegation(_stale_snapshot("alice", days_old=30))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert _pushes(backup) == []

    clock.now = BASE_TS + timedelta(seconds=200)
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)


def test_위임_fresh면_backup으로_정상_push():
    """fresh 위임이면 backup push 정상(mode=backup, 6.6-i)."""
    queue = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    dispatcher = WebSocketDispatcher(
        queue=queue,
        staleness_threshold=timedelta(days=7),
    )
    dispatcher.register_delegation(_fresh_snapshot("alice"))
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert len(_pushes(backup)) == 1

    dispatcher.submit(ticket.ticket_id, Answer(text="백업 답", sources=(), mode="full"))
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "backup"


def test_위임_없는_owner는_backup_건너뛰고_escalation():
    """위임 자체가 없으면(register_delegation 안 함) backup 단계 건너뜀 → escalation."""
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock, timeout=timedelta(seconds=60))
    dispatcher = WebSocketDispatcher(
        queue=queue,
        staleness_threshold=timedelta(days=7),
    )
    # register_delegation 호출 안 함 — 위임 없음.
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert _pushes(backup) == [], "위임 없으면 backup 건너뜀"

    clock.now = BASE_TS + timedelta(seconds=200)
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)


def test_staleness_미설정이면_위임검사없이_backup_push():
    """staleness_threshold 미주입(하위호환)이면 위임 검사 없이 backup push(6.6-i 그대로).

    6.6-i 테스트가 register_delegation 없이도 backup push되던 동작을 보존한다(회귀 방지).
    """
    queue = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    dispatcher = WebSocketDispatcher(queue=queue)  # staleness_threshold 미주입
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert len(_pushes(backup)) == 1, "staleness 미설정이면 위임 검사 없이 push(6.6-i 보존)"


# ── ④ staleness는 primary push에 영향 없다(결정 9 — stale은 backup만 가른다) ───


def test_staleness는_primary_push를_막지_않는다():
    """staleness는 backup 단계만 가른다 — primary는 stale 위임과 무관하게 push."""
    queue = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    dispatcher = WebSocketDispatcher(
        queue=queue,
        staleness_threshold=timedelta(days=7),
    )
    dispatcher.register_delegation(_stale_snapshot("alice", days_old=30))
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    # primary는 stale 위임과 무관하게 push.
    assert len(_pushes(primary)) == 1
    dispatcher.submit(ticket.ticket_id, Answer(text="primary 답", sources=(), mode="full"))
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "full"


# ── ⑤ DelegationSnapshot 값 객체 ────────────────────────────────────────────


def test_DelegationSnapshot은_frozen이고_필드를_보존한다():
    snap = DelegationSnapshot(
        owner_id="alice",
        agent_ids=("cs_ops", "finance_ops"),
        snapshot_at=BASE_TS,
    )
    assert snap.owner_id == "alice"
    assert snap.agent_ids == ("cs_ops", "finance_ops")
    assert snap.snapshot_at == BASE_TS


# ── ⑦ head-of-line blocking 해소(Blocker 1): 거부 작업이 정상 작업을 막지 않는다 ──


def test_head_of_line_거부작업이_뒤의_정상작업_push를_막지_않는다():
    """Blocker 1: 같은 owner 큐에 backup 부재로 t1 회수된 작업 A와, primary로 갈 새 작업 B가
    섞이면, A가 push 불가(거부)여도 B는 primary로 push되어야 한다.

    기존 `_push_pending`은 거부 작업을 만나면 unclaim+return해, 큐 앞의 거부 작업 하나가
    뒤의 정상 작업을 claim조차 못 하게 막았다(head-of-line). claimable 기반 재설계로
    거부는 건너뛰고 다음 claimable을 push해야 한다.
    """
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=120),
        t1=timedelta(seconds=40),
    )
    # backup 없음 → A는 t1 회수돼도 primary 제외라 갈 곳 없어 거부(큐에 잔존).
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)

    # A: primary로 push + claimed.
    ticket_a = dispatcher.dispatch("A", _fixed_card(owner="alice"))
    assert len(_pushes(primary)) == 1

    # t1 경과 → A claim 회수 + 표식. backup 부재라 A는 거부되어 queued로 잔존.
    clock.now = BASE_TS + timedelta(seconds=50)
    dispatcher.poll(ticket_a)
    # A는 갈 곳이 없어 다시 push되지 않음(primary 제외·backup 부재).
    pushes_after_recover = len(_pushes(primary))

    # B: 새 작업 — A가 큐 앞에 거부 상태로 있어도 B는 primary로 push되어야 한다.
    ticket_b = dispatcher.dispatch("B", _fixed_card(owner="alice"))
    pushes_b = [p for p in _pushes(primary) if p.ticket.ticket_id == ticket_b.ticket_id]
    assert len(pushes_b) == 1, "거부 작업 A가 정상 작업 B의 primary push를 막으면 안 됨"
    assert len(_pushes(primary)) == pushes_after_recover + 1


def test_head_of_line_거부작업은_큐에_남아_timeout_escalation으로_종착():
    """Blocker 1 미아 없음: 거부로 push 못 한 작업도 큐에 남아 자기 timeout으로 escalation."""
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=120),
        t1=timedelta(seconds=40),
    )
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)

    ticket_a = dispatcher.dispatch("A", _fixed_card(owner="alice"))
    clock.now = BASE_TS + timedelta(seconds=50)
    dispatcher.poll(ticket_a)  # t1 회수, backup 부재 → 거부(큐 잔존)

    clock.now = BASE_TS + timedelta(seconds=130)  # 전체 timeout 초과
    outcome = dispatcher.poll(ticket_a)
    assert isinstance(outcome, EscalatedToManager)


# ── ⑧ primary 회복(Blocker 2): 표식은 "1회 한정", primary 재연결 시 복귀 ──────


def test_primary_재연결되면_t1_회수작업이_다시_primary로_push된다():
    """Blocker 2: t1 회수로 primary 제외 표식이 붙은 작업이, primary가 끊겼다 다시 붙으면
    그 작업은 다시 primary로 push되어야 한다(표식이 영구 잔존하면 회복 불가).

    backup 부재로 t1 회수분이 큐에 거부 상태로 남는데, 표식(`_primary_exhausted`)이 push
    성공 시에만 정리되면 escalation까지 잔존해 primary가 재연결돼도 영영 primary로 안 간다.
    표식 의미를 "이번 라우팅 1회 한정 primary 제외"로 좁혀야 한다(ADR 8-2 primary 회복).
    """
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=300),  # 회복 검증 동안 escalation 안 나게 넉넉히
        t1=timedelta(seconds=40),
    )
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert len(_pushes(primary)) == 1

    # t1 경과 → claim 회수 + primary 제외 표식. backup 부재라 push 못 함(큐 잔존).
    clock.now = BASE_TS + timedelta(seconds=50)
    dispatcher.poll(ticket)

    # primary 끊김 → 재연결(예: 워커 재기동). 재연결 재동기에서 다시 primary로 가야 한다.
    dispatcher.disconnect("alice", role="primary")
    primary2 = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary2)

    pushes2 = _pushes(primary2)
    assert len(pushes2) == 1, "primary 재연결 후 표식이 소비돼 다시 primary로 push되어야 함"
    assert pushes2[0].ticket.ticket_id == ticket.ticket_id

    # 그리고 primary가 답하면 mode 보존(full) — primary로 복귀했으니 backup 강제 없음.
    dispatcher.submit(ticket.ticket_id, Answer(text="primary 답", sources=(), mode="full"))
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "full"


def test_primary_재연결_회복은_backup_있으면_여전히_backup_우선이_아니다():
    """Blocker 2 정밀: t1 회수 후 표식 소비 → primary 재연결 시 primary가 다시 후보.

    backup이 있어도 primary가 재연결되면(끊겼다 다시 붙으면) 그 작업은 primary로 가야
    한다 — t1 회수 표식이 "1회 한정"이라 다음 라우팅엔 primary가 정상 후보이기 때문.
    """
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=300),
        t1=timedelta(seconds=40),
    )
    dispatcher = WebSocketDispatcher(queue=queue)
    primary = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    clock.now = BASE_TS + timedelta(seconds=50)
    dispatcher.poll(ticket)  # t1 회수 + 표식(backup 부재라 push 못 함)

    # primary 재연결 — backup 없이도 primary 복귀해야 한다.
    dispatcher.disconnect("alice", role="primary")
    primary2 = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary2)
    assert len(_pushes(primary2)) == 1, "표식 1회 소비 후 primary 재연결 시 primary로 복귀"


# ── ⑨ agent_ids 대조(Major 1): 위임 대상 외 카드는 backup 거부 ─────────────────


def test_위임_대상외_카드면_backup_거부하고_escalation():
    """Major 1: 위임 스냅샷 agent_ids에 없는 카드의 질문은 fresh 위임이어도 backup이 답하지
    않고 escalation(CONTEXT 위임 정의·결정 9 "모르면 넘김").

    `_backup_allowed`가 owner·snapshot_at만 보고 ticket의 agent_id가 위임 대상인지 대조하지
    않으면, 위임 안 한 영역까지 backup이 owner 이름으로 답한다(agent_ids 죽은 필드).
    """
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock, timeout=timedelta(seconds=60))
    dispatcher = WebSocketDispatcher(
        queue=queue,
        staleness_threshold=timedelta(days=7),
    )
    # 위임 대상은 cs_ops 뿐 — fresh 스냅샷.
    dispatcher.register_delegation(
        DelegationSnapshot(
            owner_id="alice",
            agent_ids=("cs_ops",),
            snapshot_at=BASE_TS - timedelta(hours=1),
        )
    )
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    # 질문은 위임 안 한 카드(finance_ops) — backup 거부되어야 한다.
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice", agent_id="finance_ops"))
    assert _pushes(backup) == [], "위임 대상 외 카드면 backup push 거부"

    clock.now = BASE_TS + timedelta(seconds=200)
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)


def test_위임_대상_카드면_backup_정상_push_mode_backup():
    """Major 1 대조군: 위임 대상(agent_ids에 든) 카드면 fresh 위임 시 backup 정상 push."""
    queue = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    dispatcher = WebSocketDispatcher(
        queue=queue,
        staleness_threshold=timedelta(days=7),
    )
    dispatcher.register_delegation(
        DelegationSnapshot(
            owner_id="alice",
            agent_ids=("cs_ops", "finance_ops"),
            snapshot_at=BASE_TS - timedelta(hours=1),
        )
    )
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice", agent_id="finance_ops"))
    assert len(_pushes(backup)) == 1, "위임 대상 카드면 backup push 허용"

    dispatcher.submit(ticket.ticket_id, Answer(text="백업 답", sources=(), mode="full"))
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "backup"


# ── ⑩ claimable / claim_ticket 큐 API(Blocker 1 토대) 단위 검증 ───────────────


def test_claimable는_queued_ticket만_FIFO로_반환한다():
    """`claimable(owner)`은 queued 상태 ticket을 FIFO로 반환한다(claimed/answered 제외)."""
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock)
    card = _fixed_card(owner="alice")

    t1 = queue.dispatch("A", card)
    t2 = queue.dispatch("B", card)
    claimable = queue.claimable("alice")
    assert [t.ticket_id for t in claimable] == [t1.ticket_id, t2.ticket_id]

    # 하나 claim하면 claimable에서 빠진다.
    assert queue.claim_ticket(t1.ticket_id) is True
    assert [t.ticket_id for t in queue.claimable("alice")] == [t2.ticket_id]


def test_claim_ticket는_queued만_claimed로_전이하고_아니면_False():
    """`claim_ticket(tid)`은 queued면 claimed로 전이하고 True, 아니면 no-op False(멱등)."""
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock)
    ticket = queue.dispatch("Q", _fixed_card(owner="alice"))

    assert queue.claim_ticket(ticket.ticket_id) is True
    # 이미 claimed → 재claim 거부(False).
    assert queue.claim_ticket(ticket.ticket_id) is False
    # 미존재 ticket → False.
    assert queue.claim_ticket("nonexistent") is False


def test_claim_ticket으로_claimed된뒤_t1경과면_stale_claims가_회수한다():
    """claim_ticket도 claim 시각을 기록해 t1 경과 판정 대상이 된다(claim과 동치)."""
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock, t1=timedelta(seconds=40))
    ticket = queue.dispatch("Q", _fixed_card(owner="alice"))
    assert queue.claim_ticket(ticket.ticket_id) is True

    clock.now = BASE_TS + timedelta(seconds=50)
    recovered = queue.stale_claims("alice")
    assert [t.ticket_id for t in recovered] == [ticket.ticket_id]


# ── ⑪ t1 < timeout 강제(Minor): t1 >= timeout이면 ValueError ──────────────────


def test_t1이_timeout_이상이면_ValueError():
    """Minor: t1 >= timeout이면 backup 단계가 조용히 무력화 — 생성자에서 거부."""
    with pytest.raises(ValueError):
        InMemoryWorkQueueDispatcher(timeout=timedelta(seconds=40), t1=timedelta(seconds=40))
    with pytest.raises(ValueError):
        InMemoryWorkQueueDispatcher(timeout=timedelta(seconds=40), t1=timedelta(seconds=60))


def test_t1이_timeout_미만이면_정상_생성():
    """t1 < timeout이면 정상(경계 정상값 회귀 방지)."""
    queue = InMemoryWorkQueueDispatcher(timeout=timedelta(seconds=120), t1=timedelta(seconds=40))
    assert queue is not None


def test_t1_None이면_검증_없이_정상_생성():
    """t1=None(하위호환)이면 t1<timeout 검증 자체가 무관 — 정상 생성."""
    queue = InMemoryWorkQueueDispatcher(timeout=timedelta(seconds=120), t1=None)
    assert queue is not None


# ── ⑥ 합류: stale 위임은 t1 전환 시점에도 backup 거부 ────────────────────────


def test_stale_위임이면_t1_경과해도_backup_전환_거부하고_escalation():
    """결정 8·9 합류: primary 무응답 t1 경과해도 위임 stale이면 backup 전환 안 함 → escalation.

    "stale면 t1 후에도 backup 전환 안 함 → 곧장 escalation 경로"(ADR 결정 9 shape).
    """
    clock = _MutableClock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=120),
        t1=timedelta(seconds=40),
    )
    dispatcher = WebSocketDispatcher(
        queue=queue,
        staleness_threshold=timedelta(days=7),
    )
    dispatcher.register_delegation(_stale_snapshot("alice", days_old=30))
    primary = _Recorder()
    backup = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), primary)
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), backup)

    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    clock.now = BASE_TS + timedelta(seconds=50)  # t1 경과·전체 timeout 전
    out1 = dispatcher.poll(ticket)  # t1 경과 — 하지만 stale이라 backup 전환 거부
    assert isinstance(out1, AwaitingWorker)
    assert _pushes(backup) == [], "stale면 t1 경과해도 backup 전환 거부"

    clock.now = BASE_TS + timedelta(seconds=130)  # 전체 timeout(120s) 경과
    out2 = dispatcher.poll(ticket)  # → escalation
    assert isinstance(out2, EscalatedToManager)
