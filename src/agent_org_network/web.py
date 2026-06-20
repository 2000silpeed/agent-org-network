"""웹 백엔드 — 이미 완성된 AskOrg 핸들러를 감싸는 얇은 어댑터.

비즈니스 로직 없음: POST /ask 가 질문을 받아 핸들러를 호출하고
OrgReply(Answered | Pending)를 JSON으로 직렬화해 돌려준다.
내부값(confidence·candidates·escalated_to)은 Answered/Pending에 필드 자체가
없으므로 구조적으로 새지 않는다. 사용자에겐 담당·모드·출처(또는 안내)만 간다.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent_org_network.ask_org import Answered, OrgReply, Pending
from agent_org_network.demo import build_demo_ask_org
from agent_org_network.user import User

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
_INDEX_HTML = _WEB_DIR / "index.html"

# 웹챗에서 오는 익명 end-user. (로그인은 후순위 — walking skeleton)
_WEB_USER = User(id="web_guest")


class AskRequest(BaseModel):
    question: str


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


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Org Network — 채팅(데모)")
    ask = build_demo_ask_org()

    @app.post("/ask")
    def ask_endpoint(req: AskRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        reply = ask.handle(req.question, _WEB_USER)
        return serialize_reply(reply)

    @app.get("/")
    def index() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_INDEX_HTML)

    return app


app = create_app()
