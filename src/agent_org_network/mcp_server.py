"""중앙 MCP 서버 진입점 — 이미 완성된 AskOrg 핸들러를 MCP 도구로 노출하는 어댑터(T3.2).

ADR 0006: 중앙=단일 MCP 서버, `ask_org`가 1급 진입점. 여러 클라이언트(Claude
Desktop·IDE 등)가 같은 백엔드를 본다. 결정적 결과: 일반 MCP 클라이언트에선 담당·
승인 같은 신뢰 표식이 *우리 UI가 아니라 텍스트로* 노출된다(내용 보존). 그래서 도구
결과는 사람이 읽는 한국어 텍스트에 담당·신뢰 상태(mode)·출처를 박는다(불변식 "답엔
항상 담당·신뢰 상태가 붙는다").

노출 규율은 web의 `serialize_reply`와 같다 — `OrgReply`(Answered | Pending)에서만
투영하고 confidence·candidates·escalated_to·manager_id·reason·ticket_id 등 조직 내부값은
절대 싣지 않는다. 다른 점은 출력 형식뿐이다(web은 JSON dict, MCP는 텍스트). 내부값이
새지 않는 안전성은 구조적이다 — Answered/Pending에 그 필드 자체가 없다.

비즈니스 로직 없음: `ask_org` 도구는 `ask.handle(question, User(...))`를 호출하고
`reply_to_mcp_text`로 텍스트만 투영한다. 미아 없음·Authority 중앙·전이≠기록은 `handle`이
이미 보장한다(MCP는 표현층).

범위 밖(자리만): MCP 리소스·프롬프트·다중 도구·SSE/streamable-http 전송은 이 진입점에
없다. 실 stdio 서버 기동(`main`)은 *수동 시연*이다 — worker.py의 실 WS 셸과 같은 경계
(게이트 밖). 사용자 인증(도구 파라미터가 아니라 서버 설정값)은 T6.5·ADR 0009 연결점.
"""

from typing import assert_never

from mcp.server.fastmcp import FastMCP

from agent_org_network.ask_org import Answered, AskOrg, OrgReply, Pending
from agent_org_network.user import User

# 사용자 신원은 서버 *설정값*이지 도구 파라미터가 아니다(ADR 0009 연결점). walking
# skeleton이라 익명 guest로 고정한다 — 도구가 user를 받게 두면 누구든 남을 가장할 수
# 있으므로 막는다(T6.5에서 실 인증 주체로 대체할 자리). web의 `_WEB_USER`와 같은 정신.
_DEFAULT_MCP_USER_ID = "mcp_guest"


def reply_to_mcp_text(reply: OrgReply) -> str:
    """OrgReply를 MCP 클라이언트가 텍스트로 읽을 사용자向 답으로 투영한다(내부값 미포함).

    순수 함수다 — SDK·IO 없이 결정론으로 테스트한다(이 모듈의 노출 규율 핵심).
    web의 `serialize_reply`와 같은 경계: `OrgReply`에서만 투영하므로 조직 내부값은
    구조적으로 새지 않는다(Answered/Pending에 필드 자체가 없다). 다른 점은 형식뿐
    (dict가 아니라 사람이 읽는 텍스트).

    Answered → 답 본문 + 담당(owner/agent_id)·신뢰 상태(mode)·출처 메타 라인. `mode`는
    full/draft_only/backup을 *그대로* 노출한다 — 본디 사용자에게 알려야 할 신뢰값이다
    (ADR 0012 결정 4, web과 동일). 출처가 없으면 "(없음)"으로 표기한다.

    Pending → kind별 중립 안내(`message`). `dispatched`면 답 회수용 *불투명 추적 토큰*
    1개를 안내에 덧붙인다(ADR 0011 결정 6-5 — 토큰은 uuid4 hex라 owner_id·ticket_id·구조를
    비추지 않으므로 노출 OK). contested/unowned는 tracking이 None이라 토큰 안내가 없다.

    match+assert_never로 OrgReply(Answered | Pending) sealed sum을 망라한다.
    """
    match reply:
        case Answered():
            owner, agent_id = reply.answered_by
            sources = " · ".join(reply.sources) if reply.sources else "(없음)"
            meta = f"담당: {owner}/{agent_id} · 신뢰: {reply.mode} · 출처: {sources}"
            return f"{reply.text}\n\n{meta}"
        case Pending():
            # 답 회수용 불투명 추적 토큰(dispatched에만 존재, ADR 0011 결정 6-5). 사용자/
            # 클라이언트가 이 토큰으로 나중에 답을 회수한다 — 토큰은 uuid4 hex라
            # owner_id·ticket_id·구조를 비추지 않는다(노출 불변식의 정밀화). contested/
            # unowned는 tracking이 None이라 토큰 안내를 생략한다.
            if reply.tracking is not None:
                return f"{reply.message}\n\n추적 토큰: {reply.tracking}"
            return reply.message
        case _ as never:
            assert_never(never)


def create_mcp_server(ask: AskOrg, *, user_id: str = _DEFAULT_MCP_USER_ID) -> FastMCP:
    """AskOrg 핸들러를 `ask_org` 도구로 노출하는 FastMCP 서버를 조립한다.

    도구 본문은 `ask.handle(question, User(id=user_id))`를 호출하고 `reply_to_mcp_text`로
    텍스트를 투영한다 — 비즈니스 로직은 전부 `handle`이 진다(MCP는 표현층). `user_id`는
    서버 *설정값*이라 도구 파라미터가 아니다(ADR 0009 연결점). 기본은 익명 guest이고
    도구는 question만 받는다 — 누구도 남을 가장할 수 없다(인증은 T6.5에서 실 주체로 대체).

    결정론 테스트는 `create_mcp_server(build_demo(runtime=StubRuntime()).ask)`로 만들어
    `await server.call_tool("ask_org", {...})`(in-memory)로 호출한다 — 실 stdio·실 claude·
    실 네트워크 0.
    """
    mcp = FastMCP("Agent Org Network — 조직에 묻기")

    @mcp.tool(
        name="ask_org",
        description=(
            "회사 조직에 질문하면 담당이 답합니다. 질문을 분류해 담당 영역으로 라우팅하고, "
            "담당의 답을 담당·신뢰 상태·출처와 함께 돌려줍니다. 담당이 정해지지 않았거나 "
            "(다툼) 아직 없으면(미배정) 처리 안내를, 담당에게 전달됐지만 답이 준비 중이면 "
            "답 회수용 추적 토큰을 돌려줍니다."
        ),
    )
    def ask_org(question: str) -> str:  # pyright: ignore[reportUnusedFunction]
        reply = ask.handle(question, User(id=user_id))
        return reply_to_mcp_text(reply)

    return mcp


def main() -> None:
    """CLI 진입점 — 데모 백엔드로 MCP 서버를 stdio로 기동한다(수동 시연).

    `build_demo().ask`(기본 런타임=진짜 Claude)로 서버를 만들어 `mcp.run()`(stdio 기본)을
    돈다. 실 stdio 서버 기동은 *게이트 밖 수동 시연*이다(worker.py의 실 WS 셸과 같은
    경계) — 결정론 테스트는 in-memory `call_tool`로만 돈다. Claude Desktop·IDE 등 MCP
    클라이언트가 이 프로세스를 stdio로 띄워 `ask_org` 도구를 쓴다.
    """
    from agent_org_network.demo import build_demo

    server = create_mcp_server(build_demo().ask)
    server.run()


if __name__ == "__main__":
    main()
