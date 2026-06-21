"""T6.6 슬라이스 i — 중앙 WS 핸들러 등급 라우팅 결정론 테스트 (TestClient WS + Fake 워커).

ADR 0012 결정 2·4. Fake primary/backup 워커 = TestClient WS 세션이 role을 실어 등록하고
프레임을 주고받는다(실 네트워크·실 claude·별 프로세스 0). 핸들러가 `RegisterWorker.role`을
디스패처 register로 전달하는지, 그 결과 우선순위 push·mode=backup 강제가 와이어 끝까지
보존되는지 검증한다.
"""

from datetime import date, datetime, timezone
from typing import Any, Callable, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from agent_org_network.agent_card import AgentCard
from agent_org_network.dispatch import Delivered
from agent_org_network.server import create_worker_app
from agent_org_network.transport import (
    AnswerFrame,
    RegisterWorker,
    SubmitAnswer,
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


def _client(dispatcher: WebSocketDispatcher) -> TestClient:
    app: FastAPI = create_worker_app(dispatcher)
    return TestClient(app)


def _ws(client: TestClient) -> Any:
    http: Any = client
    return http.websocket_connect("/worker")


def _recv(conn: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], conn.receive_json())


# ── ① backup 워커가 role을 실어 등록 → push 받고 → submit → mode=backup 강제 ──


def test_backup_워커가_등록하면_push받고_submit하면_mode_backup():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    ticket = dispatcher.dispatch("배송 얼마나?", _fixed_card(owner="alice"))

    client = _client(dispatcher)
    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="alice", role="backup").model_dump())
        assert _recv(conn)["type"] == "welcome"

        push = _recv(conn)
        assert push["type"] == "push_work"
        assert push["ticket"]["ticket_id"] == ticket.ticket_id

        # 워커가 full로 회신해도 backup으로 강제된다(결정 4).
        conn.send_json(
            SubmitAnswer(
                ticket_id=ticket.ticket_id,
                answer=AnswerFrame(text="백업 답", sources=(), mode="full"),
            ).model_dump()
        )

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "backup"
    assert outcome.answer.text == "백업 답"


# ── ② primary 워커는 role을 실어 등록 → submit 답 mode 보존 ──────────────────


def test_primary_워커_답은_mode가_보존된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    client = _client(dispatcher)
    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="alice", role="primary").model_dump())
        _recv(conn)  # welcome
        _recv(conn)  # push_work
        conn.send_json(
            SubmitAnswer(
                ticket_id=ticket.ticket_id,
                answer=AnswerFrame(text="primary 답", sources=(), mode="full"),
            ).model_dump()
        )

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "full"


# ── ③ 하위호환: role 미지정 워커는 primary로 등록 ────────────────────────────


def test_role_미지정_워커는_primary로_등록되어_mode_보존():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    client = _client(dispatcher)
    with _ws(client) as conn:
        # role 없이 등록(기존 워커 하위호환).
        conn.send_json(RegisterWorker(owner_id="alice").model_dump())
        _recv(conn)  # welcome
        _recv(conn)  # push_work
        conn.send_json(
            SubmitAnswer(
                ticket_id=ticket.ticket_id,
                answer=AnswerFrame(text="답", sources=(), mode="full"),
            ).model_dump()
        )

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "full"
