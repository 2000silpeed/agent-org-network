"""분산 WebSocket 운영 기능과 P17 사용자 질문 경계 회귀 테스트.

P17 `/ask`는 legacy WebSocket dispatch·fallback·tracking 부작용과 분리된다. WS 연결·폴백
기계장치는 디스패처 단위에서 계속 검증하고, 사용자 답은 canonical Request/Finalization 결과와
감독용 AnswerRecord 투영을 검증한다.

잠그는 것(결정론 — StubRuntime 폴백·실 LLM 0):
  1. 워커 미연결·legacy 폴백 주입 여부와 무관하게 `/ask`가 P17 Answered를 반환.
  2. 오프라인 자동발신이면 AnswerRecord.needs_correction_review=True로 적재.
  3. 노출 불변식: 응답 표면에 "워커 미연결·폴백" 내부값이 새지 않는다(답변 표면=담당·승인·출처).
  4. 워커 연결 시 → 폴백 미발동, 기존 워커 회신 경로 그대로(회귀 0).
  5. loopback 실 소켓: 워커 연결→해제 후 질문 → 폴백 답(실 WS 한 바퀴).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response
from starlette.testclient import WebSocketTestSession

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_record import AnswerRecordReader, InMemoryAnswerRecordStore
from agent_org_network.presence import PresenceStatus
from agent_org_network.runtime import StubRuntime
from agent_org_network.server import create_worker_app
from agent_org_network.transport import PushWork, RegisterWorker, WebSocketDispatcher
from agent_org_network.web import create_app


def _fixed_presence(status: PresenceStatus) -> Callable[[str], PresenceStatus]:
    def _lookup(_agent_id: str) -> PresenceStatus:
        return status

    return _lookup


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> Response:
    http: Any = client
    return cast(Response, http.post(url, json=payload))


def _json(res: Response) -> dict[str, Any]:
    return cast(dict[str, Any], res.json())


def _central_client(
    *,
    dispatcher: WebSocketDispatcher,
    presence_of: Callable[[str], PresenceStatus] | None = None,
    answer_record_store: InMemoryAnswerRecordStore | None = None,
) -> tuple[TestClient, AnswerRecordReader]:
    """분산 디스패처를 주입한 중앙 web 앱 — 워커는 아직 미연결(폴백 경로)."""
    ars = answer_record_store if answer_record_store is not None else InMemoryAnswerRecordStore()
    app = create_app(
        dispatcher=dispatcher,
        answer_record_store=ars,
        presence_of=presence_of,
    )
    return TestClient(app), cast(AnswerRecordReader, app.state.answer_record_view)


# ── 1·2·3. P17 답 + 오프라인 감독 표식 + 노출 불변식 ───────────────────────


def test_P17_질문은_legacy_WS_미연결과_무관하게_Answered이다() -> None:
    """사용자 질문은 미연결 WS의 legacy dispatch/fallback 부작용을 타지 않는다."""
    dispatcher = WebSocketDispatcher(fallback_runtime=StubRuntime())
    client, _ = _central_client(dispatcher=dispatcher)
    body = _json(_post(client, "/ask", {"question": "환불 절차 알려줘"}))
    assert body["type"] == "answered"
    assert body["request_id"]
    assert body["record_id"]
    assert body["review_status"] == "not_required"
    assert "tracking" not in body
    assert body["answered_by"]["agent_id"] == "cs_ops"
    assert body["answered_by"]["owner"] == "cs_lead"
    assert body["text"]


def test_P17_답은_무복제_read_view에서_조회된다() -> None:
    """P17 AnswerRecord는 legacy 복제 없이 합성 감독 read view에서 조회된다."""
    dispatcher = WebSocketDispatcher(fallback_runtime=StubRuntime())
    client, ars = _central_client(dispatcher=dispatcher, presence_of=_fixed_presence("offline"))
    body = _json(_post(client, "/ask", {"question": "환불 절차 알려줘"}))
    assert body["type"] == "answered"
    rec = ars.get(body["record_id"])
    assert rec is not None
    # offline 자동발신의 policy evidence가 Finalization audit와 record에 함께 보존된다.
    assert rec.needs_correction_review is True
    assert rec.mode == "full"


def test_폴백_답_응답_표면에_내부값이_새지_않는다() -> None:
    """노출 불변식 — 답변 표면 키는 담당·승인·출처(+불투명 record_id)뿐. 폴백·미연결 내부값 0."""
    dispatcher = WebSocketDispatcher(fallback_runtime=StubRuntime())
    client, _ = _central_client(dispatcher=dispatcher)
    body = _json(_post(client, "/ask", {"question": "환불 절차 알려줘"}))
    assert set(body.keys()) == {
        "type",
        "request_id",
        "record_id",
        "text",
        "answered_by",
        "mode",
        "sources",
        "review_status",
    }
    assert body["request_id"]
    assert body["record_id"]
    # 내부 상태값(워커 연결/폴백/신뢰/trace)이 표면에 없다.
    for leaked in ("fallback", "offline", "worker", "presence", "ticket", "tracking", "confidence"):
        assert leaked not in body


# ── 4. 워커 연결 시 → 폴백 미발동, 기존 회신 경로 그대로(회귀 0) ──────────────


def _card(owner: str, agent_id: str) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="고객 지원",
        domains=["cs"],
        last_reviewed_at=date(2026, 6, 20),
    )


def test_워커_연결시엔_폴백이_발동하지_않는다() -> None:
    """워커가 push를 받으면(claimed) 폴백은 조기 반환 — 답은 워커 submit이 낸다(회귀 0).

    디스패처 단위에서 직접 확인: 폴백 런타임을 주입해도, 연결된 워커로 push가 나가면
    작업은 claimed라 폴백이 큐에 submit하지 않는다(poll은 워커 submit 전엔 AwaitingWorker).
    """
    from agent_org_network.dispatch import AwaitingWorker

    sent: list[Any] = []
    dispatcher = WebSocketDispatcher(fallback_runtime=StubRuntime())
    dispatcher.register(RegisterWorker(owner_id="cs_lead"), sent.append)
    card = _card("cs_lead", "cs_ops")
    ticket = dispatcher.dispatch("환불 문의", card)
    # 워커에게 push됐다(폴백이 큐를 채우지 않았다).
    pushes = [f for f in sent if isinstance(f, PushWork)]
    assert len(pushes) == 1
    # 워커가 아직 submit 안 했으니 대기 — 폴백이 답을 미리 낸 게 아니다.
    assert isinstance(dispatcher.poll(ticket), AwaitingWorker)


def test_P17_질문은_legacy_폴백_런타임_미주입과_무관하게_완결된다() -> None:
    """P17 사용자 경로는 미연결 WS의 legacy dispatched 부작용을 만들지 않는다."""
    dispatcher = WebSocketDispatcher()  # 폴백 없음
    client, _ = _central_client(dispatcher=dispatcher)
    body = _json(_post(client, "/ask", {"question": "환불 절차 알려줘"}))
    assert body["type"] == "answered"
    assert body["request_id"]
    assert body["record_id"]
    assert body["review_status"] == "not_required"
    assert "tracking" not in body


# ── 5. loopback 실 소켓: 워커 연결→해제 후 질문 → 폴백 답 ──────────────────


def _recv(conn: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], conn.receive_json())


def test_loopback_워커_연결후_해제돼도_P17_질문은_canonical_답이다() -> None:
    """실 WS 연결·해제 뒤에도 사용자 질문을 legacy fallback 경로로 되돌리지 않는다.

    한 디스패처를 worker WS 앱과 중앙 web 앱이 공유한다(create_central_app 정신). 워커가
    연결(register)됐다 with 블록을 빠져나가며 끊긴 뒤, /ask가 폴백 답을 낸다.
    """
    dispatcher = WebSocketDispatcher(fallback_runtime=StubRuntime())
    worker_client = TestClient(create_worker_app(dispatcher))
    central_client, _ = _central_client(dispatcher=dispatcher)
    whttp: Any = worker_client

    # 워커 연결→해제(with 블록 종료 시 disconnect).
    with whttp.websocket_connect("/worker") as conn:
        ws = cast(WebSocketTestSession, conn)
        ws.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        assert _recv(ws)["type"] == "welcome"
    # 소켓이 닫혔다 — cs_lead 워커 미연결 상태.

    body = _json(_post(central_client, "/ask", {"question": "환불 절차 알려줘"}))
    assert body["type"] == "answered"
    assert body["request_id"]
    assert body["record_id"]
    assert "tracking" not in body
    assert body["answered_by"]["agent_id"] == "cs_ops"
