"""중앙 WebSocket 핸들러 — owner 워커의 아웃바운드 연결을 받는다 (T6.3 슬라이스2b-i, ADR 0011 결정 6).

owner 워커가 중앙에 *아웃바운드* WS로 연결하면(중앙은 `@app.websocket`로 받기만, 6-1),
이 핸들러가 (1) `RegisterWorker`로 owner 신원을 받아 인증·레지스트리 등록(6-5), (2) 그
owner 큐의 작업을 `PushWork`로 그 소켓에 내보내고(6-3), (3) 워커가 보낸 `SubmitAnswer`를
받아 내부 큐에 회신하며(`submit`), (4) 연결이 끊기면 `disconnect`로 in-flight 작업을
re-queue한다(6-4, 미아 없음).

전송 ≠ 도메인: 이 핸들러는 *전송*만 한다 — 큐 상태기계(단조 종착·idempotency·timeout
escalation)는 합성한 `WebSocketDispatcher`(→`InMemoryWorkQueueDispatcher`)가 소유한다.
핸들러는 프레임을 도메인 호출로 중계할 뿐이다.

동시성 모델(결정론 가능하게): send 콜백은 *동기*(`SendFrame`)다 — 다른 곳(예: 사용자
`/ask`가 dispatch)에서 그 owner에게 push가 발생할 수 있으므로. 그 동기 콜백은 outbound
`asyncio.Queue`에 프레임을 *thread-safe하게* 넣기만 하고(`call_soon_threadsafe`), async
핸들러의 *송신 루프*가 그걸 꺼내 실제 `send_json`한다. 수신 루프와 송신 루프를 동시에
돌려, 워커가 아무 프레임을 안 보내도 push가 흘러나간다. FastAPI `TestClient`의 WebSocket
지원으로 in-process 결정론 검증(실 네트워크·실 claude 0, ADR 0011 결정 6-6).
"""

import asyncio
from typing import Any, cast

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from agent_org_network.transport import (
    Ack,
    AuthError,
    CentralFrame,
    Heartbeat,
    RegisterWorker,
    SubmitAnswer,
    WebSocketDispatcher,
    WorkerFrame,
    from_answer_frame,
)


def _parse_worker_frame(raw: object) -> WorkerFrame | None:
    """수신 JSON을 워커 프레임으로 검증·복원한다(미지/불량은 None).

    `type` 판별 필드로 갈라 pydantic v2로 검증한다. 알 수 없거나 검증 실패면 None을
    돌려 핸들러가 무시한다(와이어 안전 — 미지 프레임이 핸들러를 깨지 않는다).
    """
    if not isinstance(raw, dict):
        return None
    payload = cast(dict[str, Any], raw)
    frame_type = payload.get("type")
    model: type[WorkerFrame]
    if frame_type == "register_worker":
        model = RegisterWorker
    elif frame_type == "submit_answer":
        model = SubmitAnswer
    elif frame_type == "heartbeat":
        model = Heartbeat
    elif frame_type == "ack":
        model = Ack
    else:
        return None
    try:
        return model.model_validate(payload)
    except ValidationError:
        return None


async def _handle_worker(websocket: WebSocket, dispatcher: WebSocketDispatcher) -> None:
    """한 워커 연결의 수명을 처리한다 — 등록→push/submit 펌프→끊김 정리.

    1) accept 후 첫 프레임은 `RegisterWorker`여야 한다. `dispatcher.register`로 인증·등록
       하고 응답(Welcome/AuthError)을 보낸다. AuthError면 닫는다(미인증 거부, 6-5).
    2) 등록 성공이면 송신 루프(outbound 큐 → send_json)와 수신 루프(워커 프레임 처리)를
       동시에 돈다. register 시점에 대기 작업이 있으면 이미 outbound 큐에 PushWork가 들어
       있어 송신 루프가 내보낸다.
    3) 어느 쪽이든 끝나면(워커 끊김 등) `disconnect`로 in-flight claimed 작업을 re-queue.
    """
    await websocket.accept()

    loop = asyncio.get_running_loop()
    outbound: asyncio.Queue[CentralFrame] = asyncio.Queue()

    def send(frame: CentralFrame) -> None:
        # 동기 콜백(다른 컨텍스트에서 호출될 수 있음) → outbound 큐에 thread-safe하게 적재.
        # 실제 send_json은 송신 루프가 수행한다.
        loop.call_soon_threadsafe(outbound.put_nowait, frame)

    # 1) 등록 — 첫 프레임은 RegisterWorker.
    try:
        first_raw: object = await websocket.receive_json()
    except WebSocketDisconnect:
        return
    first = _parse_worker_frame(first_raw)
    if not isinstance(first, RegisterWorker):
        await websocket.send_json(
            AuthError(reason="첫 프레임은 register_worker여야 함").model_dump(mode="json")
        )
        await websocket.close()
        return

    reply = dispatcher.register(first, send)
    if isinstance(reply, AuthError):
        # 미인증 거부 — 응답만 보내고 닫는다(레지스트리 미등록, disconnect 불요).
        await websocket.send_json(reply.model_dump(mode="json"))
        await websocket.close()
        return
    await websocket.send_json(reply.model_dump(mode="json"))  # Welcome

    owner_id = first.owner_id
    # 등급(ADR 0012 결정 2)을 잡아 끊김 시 *그 등급* 연결만 제거한다(같은 owner의 다른
    # 등급은 남김). register는 frame 전체를 넘겨 role이 이미 전달됐다.
    role = first.role

    # 2) push/submit 펌프 — 송신·수신 루프 동시 실행.
    async def send_loop() -> None:
        while True:
            frame = await outbound.get()
            # mode="json": TicketFrame.enqueued_at(datetime)을 ISO 문자열로 직렬화한다
            # (와이어 안전 — send_json의 json.dumps가 datetime을 못 다룬다).
            await websocket.send_json(frame.model_dump(mode="json"))

    async def recv_loop() -> None:
        while True:
            raw: object = await websocket.receive_json()
            frame = _parse_worker_frame(raw)
            if isinstance(frame, SubmitAnswer):
                # 회신을 내부 큐로 중계 — 멱등(ticket_id)·단조 종착은 큐가 보장(6-4).
                dispatcher.submit(frame.ticket_id, from_answer_frame(frame.answer))
            # Heartbeat/Ack/미지 프레임은 생존 신호로만(생존 판정 보강, 6-4). 큐 전이 없음.

    send_task = asyncio.ensure_future(send_loop())
    recv_task = asyncio.ensure_future(recv_loop())
    try:
        # 어느 한쪽이 끝날 때까지(워커 끊김 → recv_loop가 WebSocketDisconnect로 종료).
        await asyncio.wait(
            {send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        send_task.cancel()
        recv_task.cancel()
        # 3) 끊김 정리 — 그 등급 연결 제거 + in-flight claimed 작업 re-queue(미아 없음,
        # 6-4). 같은 owner의 다른 등급(예: backup)이 남아 있으면 disconnect 안에서 재push된다.
        dispatcher.disconnect(owner_id, role)


def create_worker_app(dispatcher: WebSocketDispatcher) -> FastAPI:
    """owner 워커의 아웃바운드 WS 연결을 받는 중앙 앱을 조립한다.

    `dispatcher`(WebSocketDispatcher)를 주입받아 `@app.websocket("/worker")` 한 엔드포인트를
    등록한다 — 결정론 테스트는 고정 clock·주입 큐를 박은 디스패처를 넘겨 `TestClient`
    WebSocket으로 검증한다(실 네트워크·실 claude 0).
    """
    app = FastAPI(title="Agent Org Network — 중앙 워커 WS")
    _mount_worker_endpoint(app, dispatcher)
    return app


def _mount_worker_endpoint(app: FastAPI, dispatcher: WebSocketDispatcher) -> None:
    """`@app.websocket("/worker")`를 주어진 앱에 단다(단독·통합 앱 공용 조립).

    `create_worker_app`(단독)과 `create_central_app`(web과 한 앱·한 dispatcher)이 같은
    엔드포인트 등록을 공유하게 뽑아낸다 — 워커 연결 처리는 한 곳(`_handle_worker`)에서.
    """

    @app.websocket("/worker")
    async def worker_endpoint(websocket: WebSocket) -> None:  # pyright: ignore[reportUnusedFunction]
        await _handle_worker(websocket, dispatcher)


def create_central_app() -> FastAPI:
    """end-to-end 한 프로세스 중앙 앱 — 사용자 web 라우트 + owner 워커 WS를 *한 dispatcher*로.

    end-to-end(중앙↔워커↔실 claude↔답 회수)를 닫으려면 사용자 질문(`POST /ask`)이 만드는
    작업과 워커 회신(`/worker` WS의 `SubmitAnswer`)이 *같은 `WebSocketDispatcher` 인스턴스*를
    통과해야 한다 — dispatch로 큐에 든 작업이 연결된 워커에게 push되고, 워커의 submit이 그
    사용자의 `GET /ask/{tracking}` 회수로 도달하게. 그래서 디스패처 하나를 만들어 (1)
    `web.create_app(dispatcher=...)`로 채팅·처리함·회수 라우트를 얹고, (2) 그 위에
    `/worker` WS 엔드포인트를 추가한다(같은 디스패처 공유).

    이 앱은 실 owner 워커 프로세스가 붙는 *수동 시연용* 진입점이다(`uvicorn`으로 띄움). 결정론
    테스트는 여전히 `create_worker_app`(주입 디스패처)·`web.create_app`을 따로 쓴다 — 이
    팩토리는 기본 시계·기본 큐로 실제 한 바퀴를 돌리는 조립이라 게이트가 보지 않는다.
    """
    # 지연 import — server.py는 web.py에 의존하지 않는 게 기본(web은 server를 import할 수
    # 있어 순환 위험). 통합 진입점에서만 web을 끌어와 단방향으로 합친다.
    from agent_org_network.web import create_app

    dispatcher = WebSocketDispatcher()
    app = create_app(dispatcher=dispatcher)
    _mount_worker_endpoint(app, dispatcher)
    return app


# `uvicorn agent_org_network.server:central_app`로 띄우는 모듈 수준 ASGI 앱(수동 시연).
central_app = create_central_app()
