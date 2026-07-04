"""Phase 12 마지막 조합 지점 — 분산(WebSocketDispatcher) 배선 오프라인 폴백 테스트.

"담당자 PC 꺼져도 답변"이 인프로세스 경로뿐 아니라 *분산 배선*에서도 성립함을 잠근다:
담당 워커가 미연결이라 `dispatch`가 작업을 push하지 못하면, 중앙이 주입된 `fallback_runtime`
으로 답을 대신 생성해 큐에 submit하고, 이어지는 `poll`이 `Delivered`를 돌려준다. 그래서
상위 `AskOrg`의 기존 Delivered 경로(Answered 투영·`_record_answer`의 presence 기반
needs_correction_review)가 그대로 태워진다(2라운드 배선 합류).

잠그는 것(결정론 — StubRuntime 폴백·실 LLM 0):
  1. 워커 미연결 + 폴백 런타임 주입 → `/ask`가 중앙 폴백 답(Answered)을 반환.
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
from agent_org_network.answer_record import InMemoryAnswerRecordStore
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
) -> tuple[TestClient, InMemoryAnswerRecordStore]:
    """분산 디스패처를 주입한 중앙 web 앱 — 워커는 아직 미연결(폴백 경로)."""
    ars = answer_record_store if answer_record_store is not None else InMemoryAnswerRecordStore()
    app = create_app(
        dispatcher=dispatcher,
        answer_record_store=ars,
        presence_of=presence_of,
    )
    return TestClient(app), ars


# ── 1·2·3. 워커 미연결 → 중앙 폴백 답 + 검토필요 적재 + 노출 불변식 ──────────


def test_워커_미연결이면_중앙_폴백_답을_반환한다() -> None:
    """담당 워커가 안 붙은 분산 중앙 — 폴백 런타임(StubRuntime)이 답을 대신 낸다(dispatched 아님)."""
    dispatcher = WebSocketDispatcher(fallback_runtime=StubRuntime())
    client, _ = _central_client(dispatcher=dispatcher)
    body = _json(_post(client, "/ask", {"question": "환불 절차 알려줘"}))
    # 워커 미연결이지만 dispatched(중립 안내)가 아니라 실제 답이 왔다 — 폴백 발동.
    assert body["type"] == "answered"
    # StubRuntime 답 형태 — 담당 cs_ops(환불 담당) 카드에서 파생.
    assert body["answered_by"]["agent_id"] == "cs_ops"
    assert body["answered_by"]["owner"] == "cs_lead"
    assert body["text"]


def test_오프라인_자동발신_폴백_답은_검토필요로_적재된다() -> None:
    """폴백 답도 기존 2라운드 배선에 합류 — offline·mode=full이면 needs_correction_review=True."""
    dispatcher = WebSocketDispatcher(fallback_runtime=StubRuntime())
    client, ars = _central_client(
        dispatcher=dispatcher, presence_of=_fixed_presence("offline")
    )
    body = _json(_post(client, "/ask", {"question": "환불 절차 알려줘"}))
    assert body["type"] == "answered"
    rec = ars.get(body["record_id"])
    assert rec is not None
    # 오프라인 자동발신 사후교정 표식이 적재됐다(담당자 복귀 후 검토 필터에 노출).
    assert rec.needs_correction_review is True
    assert rec.mode == "full"


def test_폴백_답_응답_표면에_내부값이_새지_않는다() -> None:
    """노출 불변식 — 답변 표면 키는 담당·승인·출처(+불투명 record_id)뿐. 폴백·미연결 내부값 0."""
    dispatcher = WebSocketDispatcher(fallback_runtime=StubRuntime())
    client, _ = _central_client(dispatcher=dispatcher)
    body = _json(_post(client, "/ask", {"question": "환불 절차 알려줘"}))
    assert set(body.keys()) <= {"type", "text", "answered_by", "mode", "sources", "record_id"}
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


def test_폴백_런타임_미주입이면_미연결은_기존_dispatched_그대로() -> None:
    """fallback_runtime 미주입(하위호환) — 워커 미연결이면 폴백 없이 dispatched 중립 안내."""
    dispatcher = WebSocketDispatcher()  # 폴백 없음
    client, _ = _central_client(dispatcher=dispatcher)
    body = _json(_post(client, "/ask", {"question": "환불 절차 알려줘"}))
    assert body["type"] == "pending"
    assert body["kind"] == "dispatched"


# ── 5. loopback 실 소켓: 워커 연결→해제 후 질문 → 폴백 답 ──────────────────


def _recv(conn: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], conn.receive_json())


def test_loopback_워커_연결후_해제되면_폴백이_답한다() -> None:
    """실 WS로 워커가 붙었다 끊긴 뒤 질문 → 중앙 폴백 답(담당자 PC 꺼져도 답변, 분산 성립점).

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
    # 미연결인데도 폴백이 답을 냈다(dispatched 아님).
    assert body["type"] == "answered"
    assert body["answered_by"]["agent_id"] == "cs_ops"
