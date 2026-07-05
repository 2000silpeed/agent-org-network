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
import logging
import os
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from agent_org_network.knowledge_sync import SyncKnowledge
from agent_org_network.oidc import OidcProvider
from agent_org_network.transport import (
    Ack,
    AuthError,
    CentralFrame,
    DocumentContent,
    Heartbeat,
    PublishIndex,
    RegisterWorker,
    SubmitAnswer,
    WebSocketDispatcher,
    WorkerFrame,
    from_answer_frame,
)

if TYPE_CHECKING:
    from agent_org_network.console import ConsoleFeed
    from agent_org_network.hitl import HitlToggleMap
    from agent_org_network.token import TokenStore

_log = logging.getLogger("agent_org_network.server")


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
    elif frame_type == "publish_index":
        model = PublishIndex  # ADR 0028 §14 결정 A — 추가 한 줄(기존 분기 무회귀·새 키)
    elif frame_type == "document_content":
        model = DocumentContent  # ADR 0028 §15 결정 A — 추가 한 줄(기존 분기 무회귀·새 키)
    elif frame_type == "sync_knowledge":
        model = SyncKnowledge  # Phase 12 (B)·ADR 0033 결정 3 — 추가 한 줄(기존 분기 무회귀·새 키)
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
    # outbound는 CentralFrame(push_work/welcome/…)뿐 아니라 KnowledgeSyncAck(지식 동기화
    # 회신·Phase 12 (B))도 나른다 — 둘 다 pydantic BaseModel(`model_dump(mode="json")`
    # 가능)이라 송신 루프는 타입 구분 없이 직렬화한다. 그래서 큐/콜백 타입을 BaseModel로
    # 넓힌다(전송층 공통 상위형 — SyncKnowledge를 CentralFrame union에 넣지 않는 이유는
    # 그 union이 "중앙→워커 정규 다운스트림"의 sealed 집합이고 ack는 요청-응답 회신이라
    # 결이 다르기 때문. 회신은 전송 경로만 공유한다).
    outbound: asyncio.Queue[BaseModel] = asyncio.Queue()

    def send_frame(frame: BaseModel) -> None:
        # 동기 콜백(다른 컨텍스트에서 호출될 수 있음) → outbound 큐에 thread-safe하게 적재.
        # 실제 send_json은 송신 루프가 수행한다.
        loop.call_soon_threadsafe(outbound.put_nowait, frame)

    def send(frame: CentralFrame) -> None:
        # 디스패처 `SendFrame`(Callable[[CentralFrame], None]) 계약용 좁힌 콜백 — register에
        # 넘긴다. 내부적으로 같은 outbound 큐를 쓴다(BaseModel 상위형으로 흡수).
        send_frame(frame)

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
            elif isinstance(frame, PublishIndex):
                # 인덱스 배포 수용(ADR 0028 §14 결정 F) — *연결 세션의 인증 owner*와 묶어
                # 스코핑(B)·over-claim 필터(D)·staleness put(C). owner는 프레임에 없다(소켓이
                # 곧 그 owner). 처리 로직은 dispatcher.accept_index→accept_published_index
                # (순수·결정론). 스코핑/staleness 거부는 질문 종착과 무관(미아 없음)이라 큐를
                # 안 깨지만 *조용히 버리지 않는다* — 거부면 보안 이벤트(사칭/미등록 시도) 또는
                # 미배선(B1 회귀)이므로 가시화한다(m1 보안 가시화·register AuthError와 대비).
                accepted = dispatcher.accept_index(owner_id, frame)
                if not accepted:
                    _log.warning(
                        "PublishIndex 거부 — owner=%s agent_id=%s "
                        "(스코핑 거부=사칭/미등록 시도 또는 store 미배선)",
                        owner_id,
                        frame.index.agent_id,
                    )
            elif isinstance(frame, DocumentContent):
                # on-demand 문서 fetch 회신(ADR 0028 §15 결정 B) — request_id로 대기 중인
                # web 핸들러 슬롯을 깨운다. 본문은 슬롯을 거쳐 web 응답으로 통과만 한다
                # (중앙 저장 0·비소유 중계, 결정 E). 미지 request_id(타임아웃/중복)는 멱등
                # 무시(resolve_fetch 안에서). 큐 전이 없음(읽기 중계라 작업 큐 무관).
                dispatcher.resolve_fetch(frame)
            elif isinstance(frame, SyncKnowledge):
                # 지식 동기화 수용(Phase 12 (B)·ADR 0033 결정 3) — *연결 세션의 인증 owner*와
                # 묶어 스코핑·admission·store put을 한다. owner는 프레임에 없다(소켓이 곧 그
                # owner·PublishIndex 수용과 대칭).
                #
                # ⚠️ M3 계약(code-reviewer·2026-07-04): 이 수신부는 `store.put`을 *직접
                # 호출하지 않는다* — 반드시 `dispatcher.accept_knowledge_sync_frame`
                # (→`accept_and_store_knowledge_sync`) 경유한다. admission 판정과 보관을
                # 한 조합 함수로 접합해 admission을 우회할 수 없게 한다(전이≠기록·수용 관문
                # 단일화). 수용/거부 응답(`KnowledgeSyncAck`)을 워커에 회신해 재시도/수정
                # 판단이 가능하게 한다(PublishIndex는 회신이 없지만 지식 동기화는 owner가
                # "왜 거부됐는지"를 알아야 지정 경계·민감 필터를 고칠 수 있다). 미배선
                # (store/registry 미주입)이면 ack=None이라 회신하지 않는다(하위호환·no-op).
                ack = dispatcher.accept_knowledge_sync_frame(owner_id, frame)
                if ack is not None:
                    send_frame(ack)
                    if not ack.accepted:
                        _log.warning(
                            "SyncKnowledge 거부 — owner=%s agent_id=%s reason=%s",
                            owner_id,
                            frame.content.agent_id,
                            ack.reason,
                        )
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


def create_central_app(
    session_secret: str | None = None,
    oidc_provider: OidcProvider | None = None,
    token_store: "TokenStore | None" = None,
    hitl_toggles: "HitlToggleMap | None" = None,
    console_feed: "ConsoleFeed | None" = None,
) -> FastAPI:
    """end-to-end 한 프로세스 중앙 앱 — 사용자 web 라우트 + owner 워커 WS를 *한 dispatcher*로.

    end-to-end(중앙↔워커↔실 claude↔답 회수)를 닫으려면 사용자 질문(`POST /ask`)이 만드는
    작업과 워커 회신(`/worker` WS의 `SubmitAnswer`)이 *같은 `WebSocketDispatcher` 인스턴스*를
    통과해야 한다 — dispatch로 큐에 든 작업이 연결된 워커에게 push되고, 워커의 submit이 그
    사용자의 `GET /ask/{tracking}` 회수로 도달하게. 그래서 디스패처 하나를 만들어 (1)
    `web.create_app(dispatcher=...)`로 채팅·처리함·회수 라우트를 얹고, (2) 그 위에
    `/worker` WS 엔드포인트를 추가한다(같은 디스패처 공유).

    백업 검토 end-to-end(ADR 0012 결정 4·7, T6.6 슬라이스 iv): backup 워커가 owner 이름으로
    낸 답이 미검토 검토 항목으로 쌓이고 owner가 처리함에서 검토하려면, `BackupReviewStore`·
    `BackupReviewService` *하나씩*을 만들어 세 곳에 **같은 인스턴스**로 주입해야 한다 —
      (1) `WebSocketDispatcher(review_store=...)`: backup 답 종착 시 검토 항목을 add(생성
          트리거, 결정 7-1),
      (2) `create_app(review_store=, review_service=)`: 처리함 검토 탭(GET/POST)과 retrieve
          덧씌움(검토 결과 재노출, 결정 7-3) — create_app이 그 store를 `build_demo`에도 넘겨
          `ask._review_store`가 같은 인스턴스를 가리키게 한다(retrieve가 검토를 반영).
    셋이 같은 인스턴스를 봐야 "backup이 답함→처리함에 뜸→owner가 검토→재회수에 반영"이 한
    바퀴 돈다.

    위임 스냅샷(`register_delegation`, 결정 3·9): backup이 그 owner의 영역을 답하려면 owner가
    *명시적으로 위임*했어야 한다(opt-in, Authority 중앙 — 카드 자기보고 아님). staleness 임계를
    두고 데모 owner들(legal_lead·cs_lead·finance_lead)의 위임 스냅샷을 fresh하게 등록해 backup
    push가 허용되게 한다. 임계 초과(stale)면 backup이 거부되고 escalation으로 종착한다("모르면
    넘김", 결정 9). 실 동기화 파이프라인·실 데이터 스냅샷은 후속(연결점만, ADR 0012 범위 밖).

    `session_secret`(T6.5·ADR 0016 결정 1): 운영 면 세션 서명 키. 주입 시 SessionMiddleware를
    부착해 운영 엔드포인트가 세션 신원을 요구한다 — 미주입 시 인증 OFF(데모·하위호환).
    프로덕션은 OPERATOR_SESSION_SECRET env 변수로 주입한다. 커밋 금지.

    `oidc_provider`(T7.1·ADR 0021): SSO 신원 검증 포트. 주입 시 SSO 모드(`POST /login/sso`
    활성·무비밀번호 `POST /login` 403 거부). 미주입이면 기존 동작(OFF/무비밀번호). 실 IdP
    연동(`HttpOidcProvider`)은 게이트 밖 수동 — 모듈 기본 앱은 미주입(env 분기는 후속).

    `token_store`(T9.2(b)·T9.5(c)·ADR 0026): 워커 admission 토큰 포트. **주입 시에는** 콘솔이
    발급한 토큰으로 워커가 실제 register되는 단일 원천이 된다 — 이 함수가 받은 *같은 인스턴스*를
    `WebSocketDispatcher(token_store=)`와 `create_app(token_store=)` 양쪽에 물린다. **미주입이면**
    `select_token_store_or_none()`(`storage_select.py`, T9.8 durable 배선)이 고른다 —
    `AON_DB`(SQLite 파일 경로) env 설정 시 실 `SqliteTokenStore(path)`를 만들어 *같은 인스턴스*를
    양쪽에 물리고(durable 스토리지가 켜지는 순간 실 토큰 검증도 자연히 켜짐), **미설정이면
    기존처럼 `None`을 그대로 양쪽에 물린다**(하위호환 최우선 — 기존 `create_central_app()`
    호출·테스트가 `RegisterWorker(token=None)`으로 register하는 관행을 절대 깨면 안 된다.
    `_authenticate`는 `token_store=None`이면 owner_id만 있어도 통과하는 T9.5(b) stub 동작으로
    폴백한다). 명시 주입은 항상 `AON_DB` env보다 우선한다.

    published 인덱스 라이브 배선(T10.4·ADR 0028 §14 결정 F): `AON_ROUTER=index`면 `create_app`
    안의 `build_demo`가 `TwoStageRouter`가 보는 published 인덱스 스토어를 만들어 `DemoBundle`로
    노출하고, `create_app`이 *그 같은 인스턴스*를 이 디스패처에 `bind_published_index`로 꽂는다
    (라우터↔디스패처 공유). 그래서 워커 publish(`/worker` WS의 `PublishIndex`→`recv_loop`→
    `accept_index`→`put`)가 라우터가 라우팅에 쓰는 store에 도달한다(워커 publish의 더 새
    `generated_at`이 시드를 교체). 디스패처를 store 미배선으로 두면 `accept_index`가 무조건
    no-op이라 publish가 조용히 버려진다(B1) — 배선이 그 회귀를 막는다.

    reeval 인덱스-수용 훅 라이브 배선(ADR 0030 S4, T11.7e E1·minor-1): 실 WS 수신 경로
    (`accept_index`)가 더 새 인덱스를 수용할 때마다 영향 Precedent·답이 stale 표식·
    `reeval_store`에 실제로 적재되려면 실 `StalenessPropagator`가 필요하다 — 단 이 함수
    시점엔 아직 `precedents`가 없다(`build_demo`가 그걸 만드는 게 `create_app` 안이라서,
    이 함수는 `create_app`보다 *먼저* `dispatcher`를 만들어야 하는 닭-달걀). 그래서 propagator
    자체는 여기서 만들지 않는다 — `create_app`이 `build_demo` 완료 후 `bundle.precedents`
    (판례가 실제로 담기는 그 store)·`bundle.audit_reader`(이 함수가 넘긴 `audit_log`와 같은
    인스턴스)·`bundle.registry` 기반 `owner_of`로 실 propagator를 구성해 `dispatcher.
    bind_propagator`로 사후 주입한다(`bind_published_index`와 대칭인 seam — T10.4 Blocker
    B1 해소와 동형). `audit_log`는 이 함수가 자체 소유(`InMemoryAuditLog`)해 `create_app
    (audit_log=...)`에도 *같은* 인스턴스를 넘긴다 — `/ask`가 남기는 routed 기록과 propagator의
    Answer 축 판정이 같은 로그를 본다. `reeval_store`를 `create_app`에 넘기는 것 자체가
    propagator 구성의 신호다(reeval_store 없으면 `create_app`은 배선하지 않는다 — 기존
    `WebSocketDispatcher` 단위 테스트[`_ws_demo_app`류] 무회귀).

    `hitl_toggles`(T9.3(b)·ADR 0025 결정 5·T9.7 S2): HITL 런타임 토글 맵. **콘솔이 set하는
    그 인스턴스가 디스패처가 push 힌트 계산에 read하는 인스턴스와 같아야** 콘솔 토글 변경이
    다음 dispatch의 `TicketFrame.hitl` 힌트에 반영된다(e2e). 이 함수가 받은 *같은 인스턴스*를
    `WebSocketDispatcher(hitl_toggles=)`와 `create_app(hitl_toggles=)` 양쪽에 물린다
    (`token_store`와 동일 패턴 — 콘솔 라우트는 `create_app`이 물린 인스턴스를 set한다).
    미주입이면 이 함수가 `HitlToggleMap()`을 새로 만들어 양쪽에 물린다(하위호환 — 미주입
    `create_app()` 단독 호출과 달리 이 통합 조립은 dispatcher가 힌트를 계산해야 하므로 기본값이
    필요하다. 힌트는 미set 상태면 항상 False = 기존 즉시 전송 동작 그대로 보존).

    이 앱은 실 owner 워커 프로세스가 붙는 *수동 시연용* 진입점이다(`uvicorn`으로 띄움). 결정론
    테스트는 여전히 `create_worker_app`(주입 디스패처)·`web.create_app`을 따로 쓴다 — 이
    팩토리는 기본 시계·기본 큐로 실제 한 바퀴를 돌리는 조립이라 게이트가 보지 않는다.
    """
    # 지연 import — server.py는 web.py에 의존하지 않는 게 기본(web은 server를 import할 수
    # 있어 순환 위험). 통합 진입점에서만 web을 끌어와 단방향으로 합친다.
    from datetime import timedelta

    from agent_org_network.audit import InMemoryAuditLog
    from agent_org_network.demo import demo_delegations, seed_demo_reeval_items
    from agent_org_network.hitl import HitlToggleMap
    from agent_org_network.reeval import InMemoryReevalStore, ReevalService
    from agent_org_network.review import BackupReviewService, InMemoryBackupReviewStore
    from agent_org_network.storage_select import select_token_store_or_none
    from agent_org_network.web import create_app

    # 검토 store·service 하나씩 — 디스패처(생성 트리거)와 web(검토 탭·retrieve 덧씌움)이
    # 같은 인스턴스를 봐야 검토 루프가 end-to-end로 닫힌다(결정 7).
    review_store = InMemoryBackupReviewStore()
    review_service = BackupReviewService(review_store)

    # 재평가(세 번째 탭·ADR 0019 결정 5) store·service 하나씩 — web(GET `/inbox/reeval`·
    # POST `/reeval/{item_id}/review`)이 같은 인스턴스를 본다. 둘째 탭과 동형. 데모 가시성은
    # 시드(`seed_demo_reeval_items`)가 댄다 — 실 OKF 커밋→StalenessPropagator 자동 적재도
    # 이제 라이브로 돈다(T11.7e E1, 아래 propagator 배선). 시드는 자동 적재 전이라도 owner가
    # 처리함 세 번째 탭에서 볼 항목을 미리 둔다(둘 다 공존 — 시드 + 실 적재).
    reeval_store = InMemoryReevalStore()
    reeval_service = ReevalService(reeval_store)
    seed_demo_reeval_items(reeval_store)

    # T11.7e minor-1: 이 함수가 audit_log를 자체 소유(`InMemoryAuditLog`) — `create_app`에도
    # 같은 인스턴스로 넘겨 `/ask` routed 기록과 Answer 축 판정이 같은 로그를 보게 한다(정합).
    # precedents는 여기서 만들지 않는다 — `build_demo`가 만드는 실 precedents(판례가 실제로
    # 담기는 store)를 `create_app`이 `build_demo` 완료 후 propagator 구성에 쓴다(위 docstring
    # "닭-달걀" 참조). 이 함수는 dispatcher를 propagator 없이 만들고, `create_app`이 reeval_store
    # 주입을 신호로 사후 `bind_propagator`로 배선한다.
    audit_log = InMemoryAuditLog()

    # staleness 임계 — 데모는 넉넉히(30일). 위임 스냅샷이 이 임계 내 fresh여야 backup이
    # 그 영역을 답한다(결정 9). 데모 스냅샷은 fresh로 등록하므로 backup push가 허용된다.
    staleness_threshold = timedelta(days=30)
    # 콘솔 발급 토큰으로 워커가 실제 register되는 단일 원천(T9.2(b)·T9.5(c)·ADR 0026) —
    # *주입받은 그대로*(None 포함) dispatcher·create_app 양쪽에 물린다. 이 함수가 기본값을
    # 강제 생성하지 않는다 — 강제하면 기존 `create_central_app()`(무인자) 호출의
    # `RegisterWorker(token=None)` register가 전부 AuthError로 깨진다(하위호환 위반).
    # 단, `AON_DB`(T9.8 durable 배선) 설정 시엔 `select_token_store()`로 실 durable
    # TokenStore를 만들어 *같은 인스턴스*를 양쪽에 물린다 — durable 토큰 스토리지가
    # 켜지는 순간은 실 토큰 검증도 자연히 켜진다(`_authenticate` stub 폴백은 여전히
    # token_store=None 조건 그대로라 `AON_DB` 미설정이면 기존 stub 동작 100% 보존).
    _resolved_token_store = (
        token_store if token_store is not None else select_token_store_or_none()
    )
    # 콘솔 set·디스패처 read가 *같은* HitlToggleMap 인스턴스를 봐야 콘솔 토글 변경이 다음
    # dispatch 힌트에 반영된다(e2e, ADR 0025 결정 5). token_store와 동일 패턴 — 이 함수가
    # 받은 그대로(또는 새로 만든 기본값)를 dispatcher·create_app 양쪽에 물린다.
    _resolved_hitl_toggles = hitl_toggles if hitl_toggles is not None else HitlToggleMap()
    # 콘솔 관전 피드(T9.2(c)·ADR 0024): 콘솔 SSE 라우트가 구독하는 그 피드에 워커 연결/종료
    # (dispatcher)와 질문 처리 사건(AskOrg via create_app)이 *한 인스턴스*로 모여야 관전
    # 스트림에 전 사건이 흐른다. 이 함수가 한 인스턴스를 만들어(또는 받은 것을) dispatcher·
    # create_app 양쪽에 물린다(token_store·hitl_toggles와 동일 단일 원천 패턴).
    from agent_org_network.console import ConsoleFeed

    _resolved_console_feed = console_feed if console_feed is not None else ConsoleFeed()
    # 프레즌스 추적기(Phase 12 (A)·ADR 0033 결정 5) — 워커 WS 연결/해제를 담당자
    # 온라인/오프라인 1급 상태로 도출한다. InMemory가 정당(프레즌스는 휘발 — 재시작하면
    # 연결도 끊겨 있으므로 WS 연결 자체가 진실 원천). 디스패처가 register/disconnect에서
    # observe하고 `_resolve_hitl_hint`가 프레즌스를 HITL 입력에 결합한다(결정 5).
    from agent_org_network.presence import InMemoryPresenceTracker

    _presence_tracker = InMemoryPresenceTracker()
    # 중앙 지식 저장소(Phase 12 (B)(C)·ADR 0033 결정 1·3, SQLite 확장) — 워커가 동기화한
    # 본문을 agent_id별 보관한다. `select_knowledge_store()`로 결정(`AON_DB` 설정 시
    # `SqliteKnowledgeStore(path)` durable, 미설정 시 기존 `InMemoryKnowledgeStore()` —
    # 하위호환). *같은 인스턴스*를 (1) 디스패처(SyncKnowledge 수신부가 M3 계약
    # `accept_and_store_knowledge_sync` 경유로 put)와 (2) 답변 런타임(`select_runtime`에
    # knowledge_store 주입 — 답 생성이 이 스토어를 소비, ADR 0033 결정 1) 양쪽에 물려
    # "워커 동기화→중앙 저장→중앙 답변" 한 축이 닫힌다(단일 원천).
    from agent_org_network.storage_select import (
        select_answer_record_store,
        select_correction_store,
        select_feedback_store,
        select_knowledge_store,
    )

    _knowledge_store = select_knowledge_store()
    # 담당자 감독 저장소(Phase 12 (A)(B)·ADR 0033 결정 4, SQLite 확장) — 중앙이 낸 답의
    # 감사 단위(`AnswerRecord`)와 그 사후 정정(`CorrectionEvent`)을 담는다.
    # `select_answer_record_store()`/`select_correction_store()`로 결정(`AON_DB` 설정
    # 시 `SqliteAnswerRecordStore`/`SqliteCorrectionStore` durable, 미설정 시 기존
    # InMemory — 하위호환). *같은 인스턴스*를 `create_app`에 물려 AskOrg 적재(답 확정
    # 시)·감독 라우트(모니터링·정정·질문자 배지)가 한 원천을 본다. 프레즌스는
    # `_presence_tracker.status`를 답변 적재 시 오프라인 자동발신 판정
    # (needs_correction_review)에 물린다.
    _answer_record_store = select_answer_record_store()
    _correction_store = select_correction_store()
    # 답변 피드백 스토어(계획 §10) — 질문자 좋음/싫음. `monitoring_for_owner`가 조인해
    # "검토 필요" 판정에 bad 피드백 축을 더한다(§10.3). 항상 InMemory(§10.8 — SQLite
    # durable은 tasks 잔여).
    _feedback_store = select_feedback_store()

    # 중앙 답변 런타임 실 주입(Phase 12 (C)·ADR 0033 결정 1) — 위 `_knowledge_store`를
    # 소비하는 런타임을 `select_runtime`으로 고른다. `AON_PROVIDER`가 인프로세스 공급자
    # (claude-api·codex)면 그 어댑터가 `resolve_knowledge_text`로 중앙 지식 저장소를 우선
    # 소비하고 부재 시 디스크로 폴백한다(스토어 우선·디스크 폴백). 미설정(레거시 claude-code)
    # 이면 ClaudeCodeRuntime cwd 접지(스토어 미소비·하위호환). okf_root는 데모 OKF 루트(폴백원).
    #
    # 이 런타임을 디스패처의 **오프라인 폴백원**으로 꽂는다(Phase 12 마지막 조합 지점) —
    # 담당 워커가 미연결이라 `dispatch`가 작업을 push하지 못하면 중앙이 이 런타임으로 답을
    # 대신 생성해 큐에 submit하고, 이어지는 poll이 Delivered를 돌려준다. 그래야 "담당자 PC
    # 꺼져도 답변"이 인프로세스 경로뿐 아니라 *분산 배선*에서도 성립한다. 워커가 연결돼 있으면
    # push가 성공해 폴백은 발동하지 않는다(회귀 0 — 기존 워커 회신 경로 그대로). `create_app`은
    # dispatcher 주입 시 runtime 인자를 무시(build_demo가 디스패처에 답 획득을 전담시킴)하므로
    # 중앙 런타임은 이 폴백원 자리로만 실제 소비된다.
    from agent_org_network.demo import DEMO_OKF_ROOT
    from agent_org_network.runtime_select import select_runtime

    _central_runtime = select_runtime(DEMO_OKF_ROOT, knowledge_store=_knowledge_store)

    dispatcher = WebSocketDispatcher(
        staleness_threshold=staleness_threshold,
        review_store=review_store,
        token_store=_resolved_token_store,
        hitl_toggles=_resolved_hitl_toggles,
        console_feed=_resolved_console_feed,
        presence_tracker=_presence_tracker,
        knowledge_store=_knowledge_store,
        fallback_runtime=_central_runtime,
    )
    # 데모 owner들의 위임 스냅샷을 주입(opt-in 위임 — owner가 자기 영역을 백업에 위임).
    # 없으면 staleness_threshold가 설정된 상태에서 backup push가 거부된다(결정 9).
    for snapshot in demo_delegations():
        dispatcher.register_delegation(snapshot)

    app = create_app(
        runtime=_central_runtime,
        dispatcher=dispatcher,
        review_store=review_store,
        audit_log=audit_log,
        review_service=review_service,
        reeval_store=reeval_store,
        reeval_service=reeval_service,
        session_secret=session_secret,
        oidc_provider=oidc_provider,
        token_store=_resolved_token_store,
        hitl_toggles=_resolved_hitl_toggles,
        console_feed=_resolved_console_feed,
        answer_record_store=_answer_record_store,
        correction_store=_correction_store,
        feedback_store=_feedback_store,
        presence_of=_presence_tracker.status,
    )
    _mount_worker_endpoint(app, dispatcher)
    return app


# `uvicorn agent_org_network.server:central_app`로 띄우는 모듈 수준 ASGI 앱(수동 시연).
# OPERATOR_SESSION_SECRET env 설정 시 인증 ON(프로덕션), 미설정 시 인증 OFF(데모).
# 프로덕션에서는 반드시 OPERATOR_SESSION_SECRET 환경변수를 설정할 것. 하드코딩 금지.
central_app = create_central_app(session_secret=os.environ.get("OPERATOR_SESSION_SECRET"))
