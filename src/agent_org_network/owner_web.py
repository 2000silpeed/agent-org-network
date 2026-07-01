"""owner 로컬 검토 웹 어댑터 — 초안 보류(Pending Draft)를 owner가 눈으로 보고 처분하는 면.

owner 워커가 HITL on 힌트를 받아 *즉시 회신하지 않고* 보류한 초안(`WorkerLogic._pending_drafts`,
ADR 0025 결정 4·T9.7 S2)을 owner가 로컬에서 검토·승인·수정하는 최소 로컬 웹이다. 검토 UI는
**owner측 로컬 면**이라 중앙 토큰 0·비소유가 성립한다(ADR 0025 결정 4·5, ADR 0030 저작 UI와
대칭 — 저작·검토 모두 owner 환경에 머문다). frontend/(Next.js·중앙 운영 면)를 쓰지 않고 중앙
`web/*.html`처럼 빌드 없는 순수 HTML/CSS/fetch로 서빙한다(D1).

단일 프로세스 겸직(D2): owner 워커가 WS 클라이언트(중앙에 아웃바운드)이면서 이 로컬 HTTP 서버를
겸직한다 — 별도 서버 프로세스를 두지 않는다. `create_owner_app`은 그 워커의 *같은* `WorkerLogic`
인스턴스(보류 store를 든 그것)를 받아 조회·처분하고, 처분으로 나온 `SubmitAnswer`를 `submit_sink`
콜백으로 흘려 실 배선에선 활성 WS 연결로 송신한다(테스트에선 fake sink가 관측).

노출 경계: 이 면은 owner 자기 로컬(localhost)이라 별도 인증을 이번 범위에 두지 않는다 — bind는
127.0.0.1 기본이라 외부에서 도달하지 않는다(운영 면 세션 인증은 중앙 web.py의 몫이고, 여긴 owner
자기 PC 로컬 면이라 다른 공간). 직렬화는 순수 함수(`serialize_pending_draft`)로 분리해 라우트가
도메인 값 객체를 그대로 흘리지 않게 한다(web.py `serialize_reply`/`serialize_case` 경계 정신).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent_org_network.transport import SubmitAnswer
from agent_org_network.worker import PendingDraft, WorkerLogic

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
_OWNER_DRAFTS_HTML = _WEB_DIR / "owner-drafts.html"


def serialize_pending_draft(draft: PendingDraft) -> dict[str, Any]:
    """`PendingDraft`(frozen 값 객체)를 검토 화면向 dict로 변환한다(순수 함수·경계).

    owner 자기 로컬 면이라 초안 본문·출처·mode를 그대로 노출한다(사용자向 OrgReply 노출
    불변식의 반대 — 여긴 owner가 답을 검토하는 면이라 내부값 노출이 목적이다). `context`
    (발화 스레드)는 표시하지 않는다 — 검토 대상은 *만들어진 답*이지 대화 원문 전체가 아니다.
    `made_at`은 ISO 문자열(관측·감사 연결점).
    """
    answer = draft.draft_answer
    return {
        "ticket_id": draft.ticket_id,
        "question": draft.question,
        "agent_id": draft.agent_id,
        "draft_answer": answer.text,
        "sources": list(answer.sources),
        "mode": answer.mode,
        "made_at": draft.made_at.isoformat(),
    }


class SubmitDraftRequest(BaseModel):
    """POST /drafts/{ticket_id}/submit 요청 바디 — 승인(원문) 또는 수정 전송.

    `edited_text`가 None(또는 미지정)이면 보류된 초안 그대로 승인, 문자열이면 그 텍스트로
    교체해 회신한다(`WorkerLogic.submit_pending_draft` 계약 그대로 — source·mode는 원 초안
    보존). owner 검토 루프(CONTEXT "owner 검토 루프")의 처분 입력.
    """

    edited_text: str | None = None


def create_owner_app(
    logic: WorkerLogic,
    submit_sink: Callable[[SubmitAnswer], None],
) -> FastAPI:
    """owner 로컬 검토 웹 앱을 조립한다 — 보류 초안 조회·처분(승인/수정) 면.

    `logic`은 워커의 *같은* `WorkerLogic` 인스턴스(보류 store를 든 그것) — 조회는
    `logic.pending_draft`, 처분은 `logic.submit_pending_draft`로 위임한다(도메인 로직
    재구현 없음). 처분으로 나온 `SubmitAnswer`는 `submit_sink`로 흘려보낸다 — 실 배선에선
    활성 WS 연결로 송신, 결정론 테스트에선 fake sink가 관측한다(WS·소켓 0). 이 어댑터는
    비즈니스 로직을 두지 않고 조회·직렬화·처분 위임만 한다(web.py 얇은 어댑터 정신).
    """
    app = FastAPI(title="Agent Org Network — owner 초안 검토(로컬)")

    @app.get("/drafts")
    def list_drafts() -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        # 보류 store 전량을 검토 화면向으로 직렬화한다. 순서는 store 삽입 순서(dict) —
        # 워커가 받은 순서라 owner에게도 자연스럽다. TTL 없음(방치 보류의 종착은 중앙 큐
        # timeout→escalation 단일 진실이 떠받침, ADR 0025 결정 4).
        return [serialize_pending_draft(d) for d in logic.pending_drafts()]

    @app.get("/drafts/{ticket_id}")
    def get_draft(ticket_id: str) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        draft = logic.pending_draft(ticket_id)
        if draft is None:
            raise HTTPException(status_code=404, detail=f"보류된 초안이 없습니다: {ticket_id}")
        return serialize_pending_draft(draft)

    @app.post("/drafts/{ticket_id}/submit")
    def submit_draft(  # pyright: ignore[reportUnusedFunction]
        ticket_id: str, req: SubmitDraftRequest
    ) -> dict[str, Any]:
        # 처분 위임 — 미보류(이미 제출·미지)면 도메인이 KeyError를 올리므로 404로 매핑한다
        # (호출자 오용 방어·존재 여부는 GET으로 먼저 확인 가능). 승인/수정 판정은 도메인
        # (`edited_text` None 여부)이 소유한다 — 어댑터는 그저 전달한다.
        try:
            submit = logic.submit_pending_draft(ticket_id, edited_text=req.edited_text)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"보류된 초안이 없습니다: {ticket_id}")
        # 처분된 답을 활성 WS로(실 배선)·fake sink로(테스트) 흘린다. 전송 후 보류 항목은
        # 이미 store에서 제거됐다(submit_pending_draft가 처리 — 전이 ≠ 기록).
        submit_sink(submit)
        return {"ticket_id": submit.ticket_id, "submitted": True}

    @app.get("/")
    def index() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_OWNER_DRAFTS_HTML)

    return app
