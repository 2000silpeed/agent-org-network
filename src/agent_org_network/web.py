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
) -> FastAPI:
    """웹 앱을 조립한다. 기본 런타임은 `build_demo`의 기본(진짜 Claude).

    결정론이 필요한 테스트는 `runtime=StubRuntime()`을 넘겨 실제 claude 호출을 막는다.
    `dispatcher`를 주입하면 분산 회수 경로(dispatched→retrieve)를 결정론으로 검증할 수
    있다(`WebSocketDispatcher` 등) — 미주입이면 기본 즉답 디스패처.
    `review_store`/`review_service`를 주입하면 백업 검토 탭을 활성화한다. 미주입이면
    검토 라우트가 404를 돌려준다(검토 기능 미사용 환경에서 안전).
    """
    app = FastAPI(title="Agent Org Network — 채팅·처리함(데모)")
    # review_store를 build_demo에도 전달 — bundle.ask._review_store가 실 store를 가리켜
    # retrieve 덧씌움(web 경로 GET /ask/{tracking})이 검토 결과를 반영한다(ADR 0012 결정 7,
    # code-reviewer [Major 1] 수정). 미주입이면 None(하위호환 — 검토 없이 동작·라우트 404).
    bundle = build_demo(runtime=runtime, dispatcher=dispatcher, review_store=review_store)
    _review_store = review_store
    _review_service = review_service

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
        return serialize_outcome(outcome)

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

    return app


app = create_app()
