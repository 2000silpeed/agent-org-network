"""T6.3 슬라이스2b-i — 중앙 WS 핸들러 결정론 테스트 (TestClient WebSocket + Fake 워커).

Fake 워커 = TestClient WS 세션이 프레임을 주고받는다(실 네트워크·실 claude·별 프로세스 0,
ADR 0011 결정 6-6). 큐 도메인 자체(단조 종착·격리)는 test_dispatch가, 프레임 변환·디스패처
합성은 test_transport가 커버한다. 여기선 *WS 핸들러*(등록→push→submit→끊김 정리·인증 거부·
프레임 검증)만 검증한다.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from agent_org_network.agent_card import AgentCard
from agent_org_network.dispatch import (
    AwaitingWorker,
    Delivered,
    EscalatedToManager,
    InMemoryWorkQueueDispatcher,
)
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
    """pyright strict: TestClient.websocket_connect 반환을 명시 컨텍스트로 좁힌다."""
    http: Any = client
    return http.websocket_connect("/worker")


def _recv(conn: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], conn.receive_json())


# ── ① 등록 → Welcome ────────────────────────────────────────────────────────


def test_register_worker가_welcome을_받는다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    client = _client(dispatcher)

    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="alice").model_dump())
        welcome = _recv(conn)
        assert welcome["type"] == "welcome"


# ── ② 등록 → 대기 작업 PushWork 수신 → SubmitAnswer → poll Delivered ─────────


def test_등록하면_대기작업이_push되고_submit하면_Delivered():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    # 워커 연결 전에 작업 적재(미연결이라 큐 대기).
    ticket = dispatcher.dispatch("배송 얼마나?", _fixed_card(owner="alice"))

    client = _client(dispatcher)
    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="alice").model_dump())
        assert _recv(conn)["type"] == "welcome"

        # 등록 직후 대기 작업이 PushWork로 내려온다.
        push = _recv(conn)
        assert push["type"] == "push_work"
        assert push["ticket"]["ticket_id"] == ticket.ticket_id
        assert push["ticket"]["question"] == "배송 얼마나?"
        # owner_id는 연결 귀속이라 프레임에 없다(6-3).
        assert "owner_id" not in push["ticket"]

        # 워커가 답 회신.
        conn.send_json(
            SubmitAnswer(
                ticket_id=ticket.ticket_id,
                answer=AnswerFrame(text="2~3일 걸려요", sources=(), mode="full"),
            ).model_dump()
        )

    # 연결이 닫혀도(with 종료) submit은 이미 처리됐다 → poll Delivered.
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.text == "2~3일 걸려요"


def test_submit한_답의_mode가_보존된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    client = _client(dispatcher)
    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="alice").model_dump())
        _recv(conn)  # welcome
        _recv(conn)  # push_work
        conn.send_json(
            SubmitAnswer(
                ticket_id=ticket.ticket_id,
                answer=AnswerFrame(text="초안", sources=(), mode="draft_only"),
            ).model_dump()
        )

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.mode == "draft_only"


# ── ③ 미연결 dispatch → 큐 대기(AwaitingWorker) → 연결 시 push ──────────────


def test_연결_전_dispatch는_AwaitingWorker_연결하면_push():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    # 워커 연결 전: 큐 대기.
    assert isinstance(dispatcher.poll(ticket), AwaitingWorker)

    client = _client(dispatcher)
    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="alice").model_dump())
        assert _recv(conn)["type"] == "welcome"
        # 연결되자 push.
        push = _recv(conn)
        assert push["type"] == "push_work"
        assert push["ticket"]["ticket_id"] == ticket.ticket_id


# ── ④ 끊김 → claimed 작업 re-queue → 재연결 재push (작업 유실 0) ────────────


def test_끊김_후_재연결하면_작업이_다시_push된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    client = _client(dispatcher)
    # 첫 연결: push 받고 답 없이 끊는다(claimed 상태로 떠 있음).
    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="alice").model_dump())
        assert _recv(conn)["type"] == "welcome"
        push1 = _recv(conn)
        assert push1["type"] == "push_work"
        # 답 없이 with 종료 → 끊김 → disconnect → release_claims(re-queue).

    # 재연결: 같은 작업이 다시 push된다(미아 없음).
    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="alice").model_dump())
        assert _recv(conn)["type"] == "welcome"
        push2 = _recv(conn)
        assert push2["type"] == "push_work"
        assert push2["ticket"]["ticket_id"] == ticket.ticket_id

        # 이번엔 답 회신 → Delivered.
        conn.send_json(
            SubmitAnswer(
                ticket_id=ticket.ticket_id,
                answer=AnswerFrame(text="재연결 후 답", sources=(), mode="full"),
            ).model_dump()
        )

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.text == "재연결 후 답"


# ── ⑤ 중복 SubmitAnswer(같은 ticket_id) → 멱등(첫 답 고정) ──────────────────


def test_중복_submit은_첫_답을_덮어쓰지_않는다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    client = _client(dispatcher)
    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="alice").model_dump())
        _recv(conn)  # welcome
        _recv(conn)  # push_work
        conn.send_json(
            SubmitAnswer(
                ticket_id=ticket.ticket_id,
                answer=AnswerFrame(text="첫 답", sources=(), mode="full"),
            ).model_dump()
        )
        # 같은 ticket_id로 중복 submit(재연결 중복 시뮬).
        conn.send_json(
            SubmitAnswer(
                ticket_id=ticket.ticket_id,
                answer=AnswerFrame(text="중복 답", sources=(), mode="full"),
            ).model_dump()
        )
        # 핸들러가 두 프레임을 처리하도록 ping/heartbeat 하나 더 보내 왕복 보장.
        conn.send_json({"type": "heartbeat"})

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)
    assert outcome.answer.text == "첫 답"


# ── ⑥ 미인증 워커 거부 ──────────────────────────────────────────────────────


def test_빈_owner_id는_AuthError로_거부된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    client = _client(dispatcher)

    with _ws(client) as conn:
        conn.send_json(RegisterWorker(owner_id="").model_dump())
        reply = _recv(conn)
        assert reply["type"] == "auth_error"


def test_첫_프레임이_register가_아니면_거부된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    client = _client(dispatcher)

    with _ws(client) as conn:
        # 등록 없이 곧장 submit → 거부(첫 프레임은 register여야 함).
        conn.send_json(
            SubmitAnswer(
                ticket_id="x",
                answer=AnswerFrame(text="t", sources=(), mode="full"),
            ).model_dump()
        )
        reply = _recv(conn)
        assert reply["type"] == "auth_error"


# ── ⑦ timeout → EscalatedToManager 종착(미아 없음, 큐 위임 확인) ────────────


def test_미연결_timeout이면_poll이_EscalatedToManager로_종착한다():
    call_count = 0

    def timeout_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return BASE_TS if call_count == 1 else BASE_TS + timedelta(seconds=200)

    queue = InMemoryWorkQueueDispatcher(clock=timeout_clock, timeout=timedelta(seconds=60))
    dispatcher = WebSocketDispatcher(queue=queue)
    # 워커 연결 없이 적재 → timeout 경과.
    ticket = dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, EscalatedToManager)
