"""웹 백엔드 — 이미 완성된 AskOrg 핸들러를 감싸는 얇은 어댑터.

비즈니스 로직 없음: POST /ask 가 질문을 받아 핸들러를 호출하고
OrgReply(Answered | Pending)를 JSON으로 직렬화해 돌려준다.
내부값(confidence·candidates·escalated_to)은 Answered/Pending에 필드 자체가
없으므로 구조적으로 새지 않는다. 사용자에겐 담당·모드·출처(또는 안내)만 간다.

처리함(Inbox)은 Owner向 *운영 화면*이라 다른 면이다 — 케이스의 후보·intent 등
내부값을 그대로 노출한다(실 사용자 채팅 OrgReply의 노출 불변식은 여기 적용 안 됨).
채팅과 처리함은 한 `DemoBundle`(공유 store)을 보므로, 처리함서 합의가 성립하면
채팅의 같은 질문이 판례 자동 라우팅으로 답해진다.

운영 면 인증(T6.5, ADR 0009·0016):
    운영 엔드포인트(처리함·Manager 큐·모니터링)는 *세션 신원*을 요구한다. 채팅
    (`/ask`·`/`)은 익명(다른 공간). `POST /login`(body `user_id`·Registry 실재
    검사·401)이 무비밀번호 서명 쿠키 세션을 set, `POST /logout`이 클리어 —
    starlette `SessionMiddleware`(`itsdangerous` 서명, `session_secret` env/주입).
    **신원 출처 = 세션**(path/body 아님 — 위조 차단): 자기 면 조회는 path param을
    제거(`/inbox/cases`·`/inbox/backup-reviews`·`/manager/queue` — 세션 owner/manager),
    1인칭 처분(concur·review·act)은 body `by_owner`/`by_manager`를 세션에서 채운다.
    스코프=자기 것만 — 도메인 1인칭 ValueError를 403으로 매핑(미로그인 401·미존재
    404·형식 400). 세션 읽기는 `_session_identity` 한 곳으로 격리(메커니즘 교체에
    엔드포인트 무흔들 — 헥사고날).
"""

import os
from pathlib import Path
from typing import Any, Literal, assert_never

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent_org_network.ask_org import Answered, OrgReply, Pending
from agent_org_network.audit import InMemoryAuditLog, JsonlAuditLog
from agent_org_network.conflict import (
    Agreed,
    ConcurOnPrimary,
    ConflictCase,
    ConsensusOutcome,
    Deadlocked,
    StillOpen,
)
from agent_org_network.demo import build_demo
from agent_org_network.dispatch import RuntimeDispatcher
from agent_org_network.manager_queue import (
    AssignOwner as MgrAssignOwner,
    Dismiss as MgrDismiss,
    ManagerAction,
    ManagerItem,
    ManagerQueueService,
    ManagerQueueStore,
    ManagerResolution,
    Reroute as MgrReroute,
)
from agent_org_network.review import (
    ApproveBackup,
    BackupReview,
    BackupReviewItem,
    BackupReviewService,
    BackupReviewStore,
    CorrectBackup,
    DismissBackup,
)
from agent_org_network.runtime import AgentRuntime
from agent_org_network.user import User

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
_INDEX_HTML = _WEB_DIR / "index.html"
_INBOX_HTML = _WEB_DIR / "inbox.html"
_MONITOR_HTML = _WEB_DIR / "monitor.html"

# 웹챗에서 오는 익명 end-user. 채팅(`/ask`·`/`)은 운영 세션을 요구하지 않는다
# (ADR 0009·0016 — 실 사용자 면은 운영 면과 다른 별개 공간, 익명 유지).
_WEB_USER = User(id="web_guest")

# 세션에 운영 신원을 담는 키(ADR 0016). 서명 쿠키 세션 dict의 이 키에 로그인된
# User.id가 박힌다 — `_session_identity`가 이 키로 읽는다.
_SESSION_USER_KEY = "operator_user_id"


class LoginRequest(BaseModel):
    """POST /login 요청 바디 — 무비밀번호 신원 선택(ADR 0016 결정 2).

    `user_id`는 Registry에 실재하는 User여야 한다(없으면 401). 비밀번호 없음 —
    v0는 *신원 선택*을 세션에 고정해 per-request 가장을 차단하는 것까지(PRD §6).
    """

    user_id: str


# ── 운영 면 인증 헬퍼 ──────────────────────────────────────────────────────


class NotAuthenticatedError(Exception):
    """운영 면 진입에 세션 신원이 없음 — 401로 매핑(ADR 0016 결정 4)."""


def _session_identity(request: Request) -> str:
    """세션에서 운영 신원(User.id)을 읽는다. 없으면 NotAuthenticatedError(→401).

    starlette `request.session`(SessionMiddleware 주입 dict-like)에서
    `_SESSION_USER_KEY`를 꺼낸다. SessionMiddleware 미부착(세션 속성 없음)도
    NotAuthenticated로 처리한다. 엔드포인트는 이 한 곳만 보고 신원을 얻어
    path/body 가장이 구조적으로 불가능해진다.
    """
    try:
        session = request.session
    except AssertionError:
        # SessionMiddleware 미부착 시 starlette가 AssertionError를 올린다.
        raise NotAuthenticatedError("세션 미들웨어 미부착")
    user_id: str | None = session.get(_SESSION_USER_KEY)
    if not user_id:
        raise NotAuthenticatedError("세션 신원 없음 — 로그인 필요")
    return user_id


class AskRequest(BaseModel):
    question: str


class ConcurRequest(BaseModel):
    """POST /cases/{case_id}/concur 요청 바디.

    인증 활성 시: by_owner는 세션에서 채워진다(body 값 무시). on_agent·rationale만 읽음.
    하위호환(미인증): by_owner를 body에서 읽는다(기존 테스트 보존).
    """

    on_agent: str
    rationale: str = ""
    by_owner: str = ""  # 하위호환 — 인증 활성 시 무시, 미활성 시 body에서 읽음


def serialize_reply(reply: OrgReply) -> dict[str, Any]:
    """OrgReply를 사용자에게 보낼 dict로 변환한다(내부값 미포함)."""
    match reply:
        case Answered():
            return {
                "type": "answered",
                "text": reply.text,
                "answered_by": {
                    "owner": reply.answered_by[0],
                    "agent_id": reply.answered_by[1],
                },
                "mode": reply.mode,
                "sources": list(reply.sources),
            }
        case Pending():
            body: dict[str, Any] = {
                "type": "pending",
                "kind": reply.kind,
                "message": reply.message,
            }
            # 답 회수용 불투명 추적 토큰(dispatched에만 존재, ADR 0011 결정 6-5).
            if reply.tracking is not None:
                body["tracking"] = reply.tracking
            return body
        case _ as never:
            assert_never(never)


def serialize_case(case: ConflictCase) -> dict[str, Any]:
    """ConflictCase를 처리함 운영 화면向 dict로 변환한다(내부값 노출 OK)."""
    return {
        "case_id": case.case_id,
        "intent": case.intent,
        "question": case.question,
        "candidates": [
            {"agent_id": c.agent_id, "owner": c.owner} for c in case.candidates
        ],
    }


def serialize_outcome(outcome: ConsensusOutcome) -> dict[str, Any]:
    """ConsensusOutcome(타입이 곧 상태)을 처리함向 dict로 변환한다."""
    match outcome:
        case Agreed():
            return {
                "type": "agreed",
                "primary": outcome.resolution.primary,
                "intent": outcome.resolution.intent,
            }
        case StillOpen():
            return {
                "type": "still_open",
                "pending_owners": list(outcome.pending_owners),
            }
        case Deadlocked():
            return {"type": "deadlocked"}
        case _:
            assert_never(outcome)


def serialize_review_item(item: BackupReviewItem) -> dict[str, Any]:
    """BackupReviewItem을 처리함 운영 화면向 dict로 변환한다(내부값 노출 OK)."""
    d: dict[str, Any] = {
        "item_id": item.item_id,
        "owner_id": item.owner_id,
        "agent_id": item.agent_id,
        "question": item.question,
        "backup_answer_text": item.backup_answer_text,
        "ticket_id": item.ticket_id,
        "snapshot_at": item.snapshot_at.isoformat(),
        "answered_at": item.answered_at.isoformat(),
        "status": item.status,
        "review": _serialize_backup_review(item.review) if item.review is not None else None,
    }
    return d


def _serialize_backup_review(review: BackupReview) -> dict[str, Any]:
    match review:
        case ApproveBackup():
            return {"type": "approve", "by_owner": review.by_owner, "rationale": review.rationale}
        case CorrectBackup():
            return {
                "type": "correct",
                "by_owner": review.by_owner,
                "corrected_text": review.corrected_text,
                "sources": list(review.sources),
                "rationale": review.rationale,
            }
        case DismissBackup():
            return {"type": "dismiss", "by_owner": review.by_owner, "rationale": review.rationale}
        case _ as never:
            assert_never(never)


def serialize_manager_item(item: ManagerItem) -> dict[str, Any]:
    """ManagerItem을 Manager 큐 운영 화면向 dict로 변환한다(내부값 노출 OK)."""
    from agent_org_network.manager_queue import FromDeadlock, FromDispatch, FromUnowned

    source = item.source
    source_dict: dict[str, Any]
    match source:
        case FromUnowned():
            source_dict = {
                "type": "from_unowned",
                "question": source.question,
                "escalated_to": source.decision.escalated_to,
            }
        case FromDeadlock():
            source_dict = {
                "type": "from_deadlock",
                "case_id": source.case.case_id,
                "intent": source.case.intent,
                "question": source.case.question,
                "reason": source.reason,
            }
        case FromDispatch():
            source_dict = {
                "type": "from_dispatch",
                "ticket_id": source.outcome.ticket.ticket_id,
                "owner_id": source.outcome.ticket.owner_id,
                "question": source.outcome.ticket.question,
                "manager_id": source.outcome.manager_id,
                "reason": source.outcome.reason,
            }
        case _ as never:
            assert_never(never)

    d: dict[str, Any] = {
        "item_id": item.item_id,
        "manager_id": item.manager_id,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
        "source": source_dict,
    }
    if item.resolution is not None:
        d["resolution"] = _serialize_manager_resolution(item.resolution)
    return d


def _serialize_manager_resolution(resolution: ManagerResolution) -> dict[str, Any]:
    r = resolution
    action_dict: dict[str, Any]
    match r.action:
        case MgrAssignOwner():
            action_dict = {
                "type": "assign_owner",
                "by_manager": r.action.by_manager,
                "primary": r.action.primary,
                "rationale": r.action.rationale,
            }
        case MgrReroute():
            action_dict = {
                "type": "reroute",
                "by_manager": r.action.by_manager,
                "to_agent": r.action.to_agent,
                "rationale": r.action.rationale,
            }
        case MgrDismiss():
            action_dict = {
                "type": "dismiss",
                "by_manager": r.action.by_manager,
                "rationale": r.action.rationale,
            }
        case _ as never:
            assert_never(never)
    return {"action": action_dict}


def summarize_audit_record(index: int, record: dict[str, Any]) -> dict[str, Any]:
    """감사 레코드(dict)를 운영 모니터링 *목록 요약*向으로 줄인다(T5.1, 운영 면)."""
    decision: dict[str, Any] = record.get("decision") or {}
    answer: dict[str, Any] | None = record.get("answer")
    return {
        "index": index,
        "timestamp": record.get("timestamp"),
        "user_id": record.get("user_id"),
        "question": record.get("question"),
        "intent": record.get("intent"),
        "disposition": decision.get("disposition"),
        "mode": answer.get("mode") if answer is not None else None,
        "answered": answer is not None,
    }


class ManagerActionRequest(BaseModel):
    """POST /manager/items/{item_id}/act 요청 바디.

    인증 활성 시: by_manager는 세션에서 채워진다(body 값 무시).
    하위호환(미인증): by_manager를 body에서 읽는다(기존 테스트 보존).
    """

    type: Literal["assign_owner", "reroute", "dismiss"]
    by_manager: str = ""  # 하위호환 — 인증 활성 시 무시, 미활성 시 body에서 읽음
    primary: str = ""
    to_agent: str = ""
    rationale: str = ""


def _parse_manager_action(req: ManagerActionRequest, by_manager: str) -> ManagerAction:
    if req.type == "assign_owner":
        return MgrAssignOwner(
            by_manager=by_manager,
            primary=req.primary,
            rationale=req.rationale,
        )
    elif req.type == "reroute":
        return MgrReroute(
            by_manager=by_manager,
            to_agent=req.to_agent,
            rationale=req.rationale,
        )
    else:
        return MgrDismiss(by_manager=by_manager, rationale=req.rationale)


class BackupReviewRequest(BaseModel):
    """POST /backup-reviews/{item_id} 요청 바디.

    인증 활성 시: by_owner는 세션에서 채워진다(body 값 무시).
    하위호환(미인증): by_owner를 body에서 읽는다(기존 테스트 보존).
    """

    type: Literal["approve", "correct", "dismiss"]
    by_owner: str = ""  # 하위호환 — 인증 활성 시 무시, 미활성 시 body에서 읽음
    corrected_text: str = ""
    sources: list[str] = []
    rationale: str = ""


def _parse_backup_review(req: BackupReviewRequest, by_owner: str) -> BackupReview:
    if req.type == "approve":
        return ApproveBackup(by_owner=by_owner, rationale=req.rationale)
    elif req.type == "correct":
        return CorrectBackup(
            by_owner=by_owner,
            corrected_text=req.corrected_text,
            sources=tuple(req.sources),
            rationale=req.rationale,
        )
    else:
        return DismissBackup(by_owner=by_owner, rationale=req.rationale)


def create_app(
    runtime: AgentRuntime | None = None,
    dispatcher: RuntimeDispatcher | None = None,
    review_store: BackupReviewStore | None = None,
    review_service: BackupReviewService | None = None,
    manager_queue_store: ManagerQueueStore | None = None,
    audit_log: JsonlAuditLog | InMemoryAuditLog | None = None,
    session_secret: str | None = None,
) -> FastAPI:
    """웹 앱을 조립한다. 기본 런타임은 `build_demo`의 기본(진짜 Claude).

    결정론이 필요한 테스트는 `runtime=StubRuntime()`을 넘겨 실제 claude 호출을 막는다.
    `session_secret`(T6.5·ADR 0016): 운영 면 세션 서명 키. 주입 시 `SessionMiddleware`를
    부착해 운영 엔드포인트가 세션 신원을 요구한다 — 테스트는 고정 키 주입(결정론),
    운영은 env. 커밋 금지. **미주입이면 세션 미부착**(하위호환 — 기존 동작·기존 테스트 보존).
    """
    from starlette.middleware.sessions import SessionMiddleware

    app = FastAPI(title="Agent Org Network — 채팅·처리함(데모)")

    # SessionMiddleware 부착 (T6.5 슬라이스 1 — ADR 0016 결정 1).
    # session_secret 주입 시에만 붙인다(미주입이면 인증 없이 동작 — 하위호환).
    _auth_enabled = session_secret is not None
    if _auth_enabled:
        app.add_middleware(SessionMiddleware, secret_key=session_secret)

    bundle = build_demo(
        runtime=runtime,
        dispatcher=dispatcher,
        review_store=review_store,
        manager_queue_store=manager_queue_store,
        audit_log=audit_log,
    )
    _review_store = review_store
    _review_service = review_service
    _manager_queue_store = manager_queue_store

    # NotAuthenticatedError → 401 매핑
    from fastapi import Request as FastApiRequest
    from fastapi.responses import JSONResponse

    @app.exception_handler(NotAuthenticatedError)
    async def not_authenticated_handler(  # pyright: ignore[reportUnusedFunction]
        request: FastApiRequest, exc: NotAuthenticatedError
    ) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.post("/ask")
    def ask_endpoint(req: AskRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        reply = bundle.ask.handle(req.question, _WEB_USER)
        return serialize_reply(reply)

    @app.get("/ask/{tracking}")
    def retrieve_endpoint(tracking: str) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        reply = bundle.ask.retrieve(tracking)
        if reply is None:
            raise HTTPException(status_code=404, detail="알 수 없는 추적 토큰")
        return serialize_reply(reply)

    # ── 운영 면 인증 라우트 (T6.5 슬라이스 1) ───────────────────────────────

    @app.post("/login")
    def login(req: LoginRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """무비밀번호 로그인 — `user_id`를 세션에 박는다(ADR 0016 결정 2).

        `req.user_id`가 Registry에 실재하는 User여야 한다(없으면 401). 검사 출처는
        bundle이 보는 Registry(데모 6명). 유효하면 `request.session[_SESSION_USER_KEY]`에 저장.
        """
        if req.user_id not in bundle.registry.user_ids():
            raise HTTPException(status_code=401, detail=f"미존재 사용자: {req.user_id!r}")
        request.session[_SESSION_USER_KEY] = req.user_id
        return {"ok": True, "user_id": req.user_id}

    @app.post("/logout")
    def logout(request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """로그아웃 — 세션 클리어(ADR 0016 결정 2)."""
        request.session.clear()
        return {"ok": True}

    @app.get("/")
    def index() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_INDEX_HTML)

    @app.get("/inbox")
    def inbox_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_INBOX_HTML)

    # ── T6.5 슬라이스 2: 신원을 세션에서 읽는 운영 엔드포인트 ──────────────────

    @app.get("/inbox/cases")
    def inbox_cases(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """세션 owner의 처리함 케이스 조회 (ADR 0016 결정 3).

        path param 제거 — 세션 신원으로 자기 처리함만(남의 것 지목 표면 없음).
        """
        owner_id = _session_identity(request)
        cases = bundle.case_store.open_for_owner(owner_id)
        return [serialize_case(c) for c in cases]

    @app.get("/inbox/backup-reviews")
    def inbox_backup_reviews(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """세션 owner의 백업 검토 탭 조회 (ADR 0016 결정 3).

        path param 제거 — 세션 신원으로 자기 검토 탭만.
        """
        owner_id = _session_identity(request)
        if _review_store is None:
            return []
        items = _review_store.pending_for_owner(owner_id)
        return [serialize_review_item(it) for it in items]

    @app.post("/cases/{case_id}/concur")
    def concur(case_id: str, req: ConcurRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """1인칭 합의 표 — 인증 활성 시 by_owner를 세션에서, 미활성 시 body에서.

        스코프(인증 활성): 세션 owner가 그 case의 후보가 아니면 ValueError → 403.
        """
        if _auth_enabled:
            by_owner = _session_identity(request)
        else:
            by_owner = req.by_owner
        vote = ConcurOnPrimary(
            by_owner=by_owner,
            on_agent=req.on_agent,
            rationale=req.rationale,
        )
        try:
            outcome = bundle.consensus.concur(case_id, vote)
        except ValueError as exc:
            msg = str(exc)
            if "미존재" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            if "후보 owner 아님" in msg:
                raise HTTPException(status_code=403, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        if isinstance(outcome, Deadlocked):
            case = bundle.case_store.get(case_id)
            if case is not None:
                bundle.ask.enqueue_deadlock(case, reason=outcome.reason)
        return serialize_outcome(outcome)

    @app.get("/manager/queue")
    def manager_queue(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """세션 manager_id의 큐 조회 (ADR 0016 결정 3).

        path param 제거 — 세션 신원의 큐만.
        """
        manager_id = _session_identity(request)
        if _manager_queue_store is None:
            return []
        items = _manager_queue_store.pending_for_manager(manager_id)
        return [serialize_manager_item(it) for it in items]

    @app.post("/manager/items/{item_id}/act")
    def manager_act(item_id: str, req: ManagerActionRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Manager 처분 — 인증 활성 시 by_manager를 세션에서, 미활성 시 body에서.

        스코프(인증 활성): 세션 신원 ≠ item.manager_id면 ValueError → 403.
        """
        if _auth_enabled:
            by_manager = _session_identity(request)
        else:
            by_manager = req.by_manager
        if _manager_queue_store is None:
            raise HTTPException(status_code=404, detail="Manager 큐가 비활성화되어 있습니다.")
        action = _parse_manager_action(req, by_manager)
        svc = ManagerQueueService(
            queue_store=_manager_queue_store,
            precedents=bundle.precedents,
            case_store=bundle.case_store,
        )
        try:
            resolved = svc.act(item_id, action)
        except ValueError as exc:
            msg = str(exc)
            if "미존재" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            if "1인칭 위반" in msg:
                raise HTTPException(status_code=403, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        return serialize_manager_item(resolved)

    @app.post("/backup-reviews/{item_id}")
    def review_backup(item_id: str, req: BackupReviewRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """백업 답 검토 — 인증 활성 시 by_owner를 세션에서, 미활성 시 body에서.

        스코프(인증 활성): 세션 owner ≠ item.owner_id면 ValueError → 403.
        """
        if _auth_enabled:
            by_owner = _session_identity(request)
        else:
            by_owner = req.by_owner
        if _review_store is None or _review_service is None:
            raise HTTPException(status_code=404, detail="검토 기능이 비활성화되어 있습니다.")
        review = _parse_backup_review(req, by_owner)
        try:
            reviewed = _review_service.review(item_id, review)
        except ValueError as exc:
            msg = str(exc)
            if "미존재" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            if "검토자" in msg and "다름" in msg:
                raise HTTPException(status_code=403, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        return serialize_review_item(reviewed)

    @app.get("/monitor")
    def monitor_logs(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """운영 모니터링 — 인증 활성 시 로그인 필요(ADR 0016 결정 5: 인증만)."""
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        if bundle.audit_reader is None:
            return []
        records = bundle.audit_reader.records()
        return [summarize_audit_record(i, r) for i, r in enumerate(records)]

    # 정적 경로(/monitor/view)를 동적(/monitor/{index})보다 *먼저* 등록한다 —
    # 그러지 않으면 "view"가 {index}(int)에 잡혀 422가 난다(FastAPI 매칭 순서).
    @app.get("/monitor/view")
    def monitor_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_MONITOR_HTML)

    @app.get("/monitor/{index}")
    def monitor_detail(index: int, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """운영 모니터링 상세 — 인증 활성 시 로그인 필요(ADR 0016 결정 5)."""
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        if bundle.audit_reader is None:
            raise HTTPException(status_code=404, detail="모니터링 로그가 비활성화되어 있습니다.")
        record = bundle.audit_reader.record_at(index)
        if record is None:
            raise HTTPException(status_code=404, detail="알 수 없는 로그 인덱스")
        return record

    # ── 하위호환 path 라우트 (인증 OFF 환경 전용 — 데모/기존 테스트) ───────────────
    # **인증 ON(session_secret 주입)이면 이 path 가장 경로를 *등록하지 않는다*** — 그래야
    # `/inbox/{owner_id}` 같은 신원-지목 경로 자체가 존재하지 않아 세션 스코프 우회가
    # 구조적으로 불가능하다(ADR 0016 보안: 신원 출처를 세션으로 *옮긴다* — path/body에
    # 남겨두면 우회 표면이 된다). 인증 OFF(secret 미주입)는 데모/기존 테스트 전용 모드라
    # path param 가장을 허용한다(이 모드는 "데모용 로그인 가장"임을 명시 — ADR 0009).
    if not _auth_enabled:

        @app.get("/inbox/{owner_id}/backup-reviews")
        def inbox_backup_reviews_legacy(owner_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
            """하위호환(인증 OFF 전용): path param으로 owner 지정."""
            if _review_store is None:
                return []
            items = _review_store.pending_for_owner(owner_id)
            return [serialize_review_item(it) for it in items]

        @app.get("/inbox/{owner_id}")
        def inbox_cases_legacy(owner_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
            """하위호환(인증 OFF 전용): path param으로 owner 지정."""
            cases = bundle.case_store.open_for_owner(owner_id)
            return [serialize_case(c) for c in cases]

        @app.get("/manager/{manager_id}")
        def manager_queue_legacy(manager_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
            """하위호환(인증 OFF 전용): path param으로 manager 지정."""
            if _manager_queue_store is None:
                return []
            items = _manager_queue_store.pending_for_manager(manager_id)
            return [serialize_manager_item(it) for it in items]

    return app


# OPERATOR_SESSION_SECRET env 설정 시 인증 ON(프로덕션), 미설정 시 인증 OFF(데모).
# 프로덕션에서는 반드시 OPERATOR_SESSION_SECRET 환경변수를 설정할 것. 하드코딩 금지.
app = create_app(session_secret=os.environ.get("OPERATOR_SESSION_SECRET"))
