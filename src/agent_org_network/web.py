"""웹 백엔드 — 이미 완성된 AskOrg 핸들러를 감싸는 얇은 어댑터.

비즈니스 로직 없음: POST /ask 가 질문을 받아 핸들러를 호출하고
OrgReply(Answered | Pending)를 JSON으로 직렬화해 돌려준다.
내부값(confidence·candidates·escalated_to)은 Answered/Pending에 필드 자체가
없으므로 구조적으로 새지 않는다. 사용자에겐 담당·모드·출처(또는 안내)만 간다.

처리함(Inbox)은 Owner向 *운영 화면*이라 다른 면이다 — 케이스의 후보·intent 등
내부값을 그대로 노출한다(실 사용자 채팅 OrgReply의 노출 불변식은 여기 적용 안 됨).
채팅과 처리함은 한 `DemoBundle`(공유 store)을 보므로, 처리함서 합의가 성립하면
채팅의 같은 질문이 판례 자동 라우팅으로 답해진다.
"""

from pathlib import Path
from typing import Any, Literal, assert_never

from fastapi import FastAPI, HTTPException
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

# 웹챗에서 오는 익명 end-user. (로그인은 후순위 — walking skeleton)
_WEB_USER = User(id="web_guest")


class AskRequest(BaseModel):
    question: str


class ConcurRequest(BaseModel):
    by_owner: str
    on_agent: str
    rationale: str = ""


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
            # 사용자/UI가 이 토큰으로 GET /ask/{tracking}을 폴링한다. 토큰은 uuid4 hex라
            # owner_id·ticket_id·구조를 비추지 않는다 — 노출 불변식의 정밀화(_LEAKY_KEYS
            # 의 조직 내부값은 여전히 안 샌다). contested/unowned는 tracking이 None이라 생략.
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
    """BackupReviewItem을 처리함 운영 화면向 dict로 변환한다(내부값 노출 OK).

    검토는 *운영 면*(처리함)이라 내부값 노출 OK(채팅 OrgReply 불변식과 다른 면).
    """
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
    """ManagerItem을 Manager 큐 운영 화면向 dict로 변환한다(내부값 노출 OK).

    Manager 큐는 운영 면이라 manager_id·source 등 내부값을 그대로 노출한다
    (ConflictCase가 처리함에 후보를 노출하듯 — 채팅 OrgReply 불변식과 다른 면).
    """
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
    """감사 레코드(dict)를 운영 모니터링 *목록 요약*向으로 줄인다(T5.1, 운영 면).

    모니터링은 *운영 면*이라 내부값 노출 OK(채팅 OrgReply 불변식의 반대 — 처리함·
    Manager 큐와 같은 면). 목록은 한눈에 보는 요약(`index`로 상세 주소 지정 — append-only
    라 인덱스 안정)이라 큰 본문(answer text·sources·collaborators 전부)은 상세
    (`GET /monitor/{index}`)로 미루고 요약 키만 싣는다. **순수 읽기** — 새 전이·기록 0.
    """
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
    """POST /manager/items/{item_id}/act 요청 바디."""

    type: Literal["assign_owner", "reroute", "dismiss"]
    by_manager: str
    primary: str = ""
    to_agent: str = ""
    rationale: str = ""


def _parse_manager_action(req: ManagerActionRequest) -> ManagerAction:
    if req.type == "assign_owner":
        return MgrAssignOwner(
            by_manager=req.by_manager,
            primary=req.primary,
            rationale=req.rationale,
        )
    elif req.type == "reroute":
        return MgrReroute(
            by_manager=req.by_manager,
            to_agent=req.to_agent,
            rationale=req.rationale,
        )
    else:
        return MgrDismiss(by_manager=req.by_manager, rationale=req.rationale)


class BackupReviewRequest(BaseModel):
    """POST /backup-reviews/{item_id} 요청 바디 — 검토 type + by_owner 필수."""

    type: Literal["approve", "correct", "dismiss"]
    by_owner: str
    corrected_text: str = ""
    sources: list[str] = []
    rationale: str = ""


def _parse_backup_review(req: BackupReviewRequest) -> BackupReview:
    if req.type == "approve":
        return ApproveBackup(by_owner=req.by_owner, rationale=req.rationale)
    elif req.type == "correct":
        return CorrectBackup(
            by_owner=req.by_owner,
            corrected_text=req.corrected_text,
            sources=tuple(req.sources),
            rationale=req.rationale,
        )
    else:
        return DismissBackup(by_owner=req.by_owner, rationale=req.rationale)


def create_app(
    runtime: AgentRuntime | None = None,
    dispatcher: RuntimeDispatcher | None = None,
    review_store: BackupReviewStore | None = None,
    review_service: BackupReviewService | None = None,
    manager_queue_store: ManagerQueueStore | None = None,
    audit_log: JsonlAuditLog | InMemoryAuditLog | None = None,
) -> FastAPI:
    """웹 앱을 조립한다. 기본 런타임은 `build_demo`의 기본(진짜 Claude).

    결정론이 필요한 테스트는 `runtime=StubRuntime()`을 넘겨 실제 claude 호출을 막는다.
    `dispatcher`를 주입하면 분산 회수 경로(dispatched→retrieve)를 결정론으로 검증할 수
    있다(`WebSocketDispatcher` 등) — 미주입이면 기본 즉답 디스패처.
    `review_store`/`review_service`를 주입하면 백업 검토 탭을 활성화한다. 미주입이면
    검토 라우트가 404를 돌려준다(검토 기능 미사용 환경에서 안전).
    `manager_queue_store`를 주입하면 Manager 큐 라우트가 활성화된다(T5.2).
    `audit_log`를 주입하면 그 인스턴스로 audit 기록·읽기가 동시에 된다(T5.1 결정론
    테스트용 — InMemoryAuditLog 주입으로 실 파일 IO 없이 모니터링 라운드 검증).
    """
    app = FastAPI(title="Agent Org Network — 채팅·처리함(데모)")
    # review_store를 build_demo에도 전달 — bundle.ask._review_store가 실 store를 가리켜
    # retrieve 덧씌움(web 경로 GET /ask/{tracking})이 검토 결과를 반영한다(ADR 0012 결정 7,
    # code-reviewer [Major 1] 수정). 미주입이면 None(하위호환 — 검토 없이 동작·라우트 404).
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

    @app.post("/ask")
    def ask_endpoint(req: AskRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        reply = bundle.ask.handle(req.question, _WEB_USER)
        return serialize_reply(reply)

    @app.get("/ask/{tracking}")
    def retrieve_endpoint(tracking: str) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        # 답 회수 조회(ADR 0011 결정 6-5, pull 한정). dispatched로 받은 불투명 토큰으로
        # 나중에 회신된 답을 가져온다 — 워커가 회신했으면 Answered, 아직이면 dispatched
        # (같은 토큰 유지)로 투영. 모르는 토큰은 404(존재하지 않는 추적). 푸시는 범위 밖.
        reply = bundle.ask.retrieve(tracking)
        if reply is None:
            raise HTTPException(status_code=404, detail="알 수 없는 추적 토큰")
        return serialize_reply(reply)

    @app.get("/")
    def index() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_INDEX_HTML)

    @app.get("/inbox")
    def inbox_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_INBOX_HTML)

    @app.get("/inbox/{owner_id}")
    def inbox_cases(owner_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        cases = bundle.case_store.open_for_owner(owner_id)
        return [serialize_case(c) for c in cases]

    @app.post("/cases/{case_id}/concur")
    def concur(case_id: str, req: ConcurRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        vote = ConcurOnPrimary(
            by_owner=req.by_owner,
            on_agent=req.on_agent,
            rationale=req.rationale,
        )
        try:
            outcome = bundle.consensus.concur(case_id, vote)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # [Blocker 1] Deadlocked → Manager 큐 적재(미아 없음 — 세 출처 모두 web 경로에서 종착).
        # 도메인 헬퍼(ask_org.enqueue_deadlock)가 중복 방지·manager_id 결정을 담당한다.
        if isinstance(outcome, Deadlocked):
            case = bundle.case_store.get(case_id)
            if case is not None:
                bundle.ask.enqueue_deadlock(case, reason=outcome.reason)
        return serialize_outcome(outcome)

    @app.get("/manager/{manager_id}")
    def manager_queue(manager_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """Manager 큐 — 그 Manager의 pending escalation 목록(T5.2 운영 면).

        Manager 큐는 운영 면이라 내부값(manager_id·source·reason) 노출 OK.
        """
        if _manager_queue_store is None:
            return []
        items = _manager_queue_store.pending_for_manager(manager_id)
        return [serialize_manager_item(it) for it in items]

    @app.post("/manager/items/{item_id}/act")
    def manager_act(item_id: str, req: ManagerActionRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Manager 처분 — AssignOwner·Reroute·Dismiss(T5.2 운영 면).

        by_manager가 item.manager_id여야 한다(1인칭 강제 — ValueError → 400).
        item_id 미존재 → 404. manager_queue_store 미주입 → 404.
        """
        if _manager_queue_store is None:
            raise HTTPException(status_code=404, detail="Manager 큐가 비활성화되어 있습니다.")
        action = _parse_manager_action(req)
        # [Blocker 2] precedents·case_store 주입 — AssignOwner+FromDeadlock 가
        # Precedent 기록 + ConflictCase 종결을 수행해야 한다(ConsensusService.Agreed 대칭).
        # bundle 이 노출하는 precedents·case_store 를 같은 인스턴스로 전달한다.
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
            raise HTTPException(status_code=400, detail=msg) from exc
        return serialize_manager_item(resolved)

    @app.get("/inbox/{owner_id}/backup-reviews")
    def inbox_backup_reviews(owner_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """owner 처리함의 백업 검토 탭 — pending 항목 조회(ADR 0012 결정 7, T6.6 슬라이스 iii).

        검토는 *운영 면*(처리함)이라 내부값(backup_answer_text·ticket_id·snapshot_at) 노출 OK.
        채팅 OrgReply 노출 불변식과 다른 면. review_store 미주입이면 빈 목록.
        """
        if _review_store is None:
            return []
        items = _review_store.pending_for_owner(owner_id)
        return [serialize_review_item(it) for it in items]

    @app.post("/backup-reviews/{item_id}")
    def review_backup(item_id: str, req: BackupReviewRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """백업 답 검토 — Approve·Correct·Dismiss 1인칭 처분(ADR 0012 결정 7, T6.6 슬라이스 iii).

        by_owner가 item.owner_id여야 한다(1인칭 강제 — ValueError → 400).
        item_id 미존재 → 404. review_store/review_service 미주입 → 404(검토 기능 비활성).
        """
        if _review_store is None or _review_service is None:
            raise HTTPException(status_code=404, detail="검토 기능이 비활성화되어 있습니다.")
        review = _parse_backup_review(req)
        try:
            reviewed = _review_service.review(item_id, review)
        except ValueError as exc:
            msg = str(exc)
            if "미존재" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        return serialize_review_item(reviewed)

    @app.get("/monitor")
    def monitor_logs() -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """운영 모니터링 — 모든 Q&A 절차의 요약 목록(T5.1 운영 면, PRD §7 시나리오 5).

        감사 로그를 *순수 읽기*로 본다(`bundle.audit_reader.records()` → 요약). 모니터링은
        운영 면이라 내부값 노출 OK(채팅 OrgReply 불변식과 다른 면 — 처리함·Manager 큐와 같다).
        각 항목의 인덱스로 상세(`GET /monitor/{index}`)를 연다(append-only라 인덱스 안정).
        audit_reader 미주입이면 빈 목록(경계). 동작은 tdd 슬라이스가 채운다(요약 직렬화).
        """
        if bundle.audit_reader is None:
            return []
        records = bundle.audit_reader.records()
        return [summarize_audit_record(i, r) for i, r in enumerate(records)]

    # 정적 경로(/monitor/view)를 동적(/monitor/{index})보다 *먼저* 등록한다 —
    # 그러지 않으면 "view"가 {index}(int)에 잡혀 422가 난다(FastAPI 매칭 순서).
    @app.get("/monitor/view")
    def monitor_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        """모니터링 HTML 페이지(자리). 데이터 경로(`/monitor`·`/monitor/{index}`)와
        충돌을 피해 `/monitor/view`로 둔다(`/`·`/ask`가 갈리듯). 실 화면은 수동 시연.
        """
        return FileResponse(_MONITOR_HTML)

    @app.get("/monitor/{index}")
    def monitor_detail(index: int) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """운영 모니터링 상세 — 한 Q&A 절차의 *전체* 감사 레코드(T5.1 운영 면).

        목록 인덱스로 그 줄 전체(decision·dispatch·answer 원형 — 내부값까지)를 돌려준다.
        범위 밖 인덱스/audit_reader 미주입 → 404(존재하지 않는 항목). 상세는 요약과 달리
        레코드 dict를 *그대로* 노출한다(운영 면 — `as_record` 전체). 순수 읽기.
        """
        if bundle.audit_reader is None:
            raise HTTPException(status_code=404, detail="모니터링 로그가 비활성화되어 있습니다.")
        record = bundle.audit_reader.record_at(index)
        if record is None:
            raise HTTPException(status_code=404, detail="알 수 없는 로그 인덱스")
        return record

    return app


app = create_app()
