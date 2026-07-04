"""Phase 12 (A) 프레즌스 배선 단위 테스트 — WebSocketDispatcher register/disconnect 결합.

`presence.py` 코어(observe_connect/disconnect·presence_to_hitl·resolve_mode_with_presence)는
`test_presence.py`가 커버한다. 여기선 그 코어를 *디스패처 연결 생명주기*에 결합한 배선을
검증한다(register→online·전 등급 disconnect→offline·backup 잔존→online 유지·HITL 힌트가
프레즌스를 결합).

전부 결정론: Fake send 콜백·주입 PresenceTracker·실 네트워크 0. 미주입(하위호환) 경로도 잠근다.
"""

from datetime import date, datetime, timezone
from typing import Callable

from agent_org_network.agent_card import AgentCard
from agent_org_network.hitl import HitlToggleMap
from agent_org_network.presence import InMemoryPresenceTracker
from agent_org_network.registry import Registry
from agent_org_network.transport import (
    CentralFrame,
    PushWork,
    RegisterWorker,
    WebSocketDispatcher,
    Welcome,
)
from agent_org_network.user import User

ROOT_USER = "root_manager"

BASE_TS = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock(ts: datetime) -> Callable[[], datetime]:
    return lambda: ts


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[CentralFrame] = []

    def __call__(self, frame: CentralFrame) -> None:
        self.sent.append(frame)


def _card(owner: str = "alice", agent_id: str = "cs_ops", approval: bool = False) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="고객 지원 담당",
        domains=["cs"],
        last_reviewed_at=date(2026, 6, 20),
        approval_when=["민감 사안"] if approval else [],
    )


def _registry(card: AgentCard) -> Registry:
    reg = Registry()
    reg.register_user(User(id=ROOT_USER))
    reg.register_user(User(id=card.owner, manager=ROOT_USER))
    reg.register(card)
    return reg


def test_register가_프레즌스를_online으로_도출한다() -> None:
    tracker = InMemoryPresenceTracker()
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), presence_tracker=tracker)
    assert tracker.status("alice") == "offline"
    dispatcher.register(RegisterWorker(owner_id="alice"), _Recorder())
    assert tracker.status("alice") == "online"


def test_전_등급_disconnect가_프레즌스를_offline으로_도출한다() -> None:
    tracker = InMemoryPresenceTracker()
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), presence_tracker=tracker)
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), _Recorder())
    assert tracker.status("alice") == "online"
    dispatcher.disconnect("alice", "primary")
    assert tracker.status("alice") == "offline"


def test_backup이_남아있으면_online_유지된다() -> None:
    """primary가 끊겨도 backup 연결이 남으면 그 담당자는 여전히 online(어느 워커든 붙어 있으면)."""
    tracker = InMemoryPresenceTracker()
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), presence_tracker=tracker)
    dispatcher.register(RegisterWorker(owner_id="alice", role="primary"), _Recorder())
    dispatcher.register(RegisterWorker(owner_id="alice", role="backup"), _Recorder())
    dispatcher.disconnect("alice", "primary")
    # primary만 끊겼고 backup이 남아 있다 — 여전히 online.
    assert tracker.status("alice") == "online"
    dispatcher.disconnect("alice", "backup")
    assert tracker.status("alice") == "offline"


def test_프레즌스_미주입이면_기존_동작_그대로() -> None:
    """presence_tracker 미주입(하위호환) — register/disconnect가 프레즌스 없이 정상 동작."""
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    reply = dispatcher.register(RegisterWorker(owner_id="alice"), _Recorder())
    assert isinstance(reply, Welcome)
    # 예외 없이 끊김도 처리(프레즌스 결합 없음).
    dispatcher.disconnect("alice")


def test_온라인_담당자면_HITL_힌트가_상향된다() -> None:
    """워커 온라인 = 사전 검토(ADR 0033 결정 5) — push되는 TicketFrame.hitl이 True.

    카드 approval_when 없음·토글 off인데도 프레즌스 online이 힌트를 True로 상향한다(온라인 분기).
    """
    card = _card(owner="alice", agent_id="cs_ops", approval=False)
    tracker = InMemoryPresenceTracker()
    dispatcher = WebSocketDispatcher(
        clock=_fixed_clock(BASE_TS),
        presence_tracker=tracker,
        registry=_registry(card),
        hitl_toggles=HitlToggleMap(),
    )
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)  # → online
    dispatcher.dispatch("환불 문의", card)
    pushes = [f for f in rec.sent if isinstance(f, PushWork)]
    assert len(pushes) == 1
    # 온라인이라 사전 검토 힌트 — hitl=True.
    assert pushes[0].ticket.hitl is True


def test_프레즌스_미주입이면_HITL_힌트는_기존_계산() -> None:
    """presence_tracker 미주입 — 힌트는 카드 approval·토글만 본다(회귀 0·online 상향 없음)."""
    card = _card(owner="alice", agent_id="cs_ops", approval=False)
    dispatcher = WebSocketDispatcher(
        clock=_fixed_clock(BASE_TS),
        registry=_registry(card),
        hitl_toggles=HitlToggleMap(),
    )
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)
    dispatcher.dispatch("환불 문의", card)
    pushes = [f for f in rec.sent if isinstance(f, PushWork)]
    assert len(pushes) == 1
    # 프레즌스 결합 없음·approval 없음·토글 off → 기존 즉시 전송(hitl=False).
    assert pushes[0].ticket.hitl is False
