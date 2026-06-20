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
from typing import Any, assert_never

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
            return {
                "type": "pending",
                "kind": reply.kind,
                "message": reply.message,
            }


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


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Org Network — 채팅·처리함(데모)")
    bundle = build_demo()

    @app.post("/ask")
    def ask_endpoint(req: AskRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        reply = bundle.ask.handle(req.question, _WEB_USER)
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

    return app


app = create_app()
