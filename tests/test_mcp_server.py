"""중앙 MCP 서버 진입점 `ask_org` — 결정론 단위 + in-memory 스모크(T3.2).

ADR 0006: 중앙=단일 MCP 서버, `ask_org`가 진입점. 일반 MCP 클라이언트(Claude
Desktop·IDE 등)에선 담당·신뢰·출처 같은 신뢰 표식이 우리 UI가 아니라 *텍스트로*
노출된다(내용 보존). 그래서 도구 결과는 사람이 읽는 한국어 텍스트에 담당·신뢰
상태(mode)·출처를 박는다.

순수 투영 함수 `reply_to_mcp_text`는 SDK 없이 결정론으로 검증한다(web의
serialize_reply와 같은 노출 규율 — 단 web은 JSON dict, MCP는 텍스트). 도구
등록·호출은 FastMCP in-memory(`await mcp.call_tool(...)`)로 1~2개 스모크만
(StubRuntime 주입 — 실 claude·실 stdio·실 네트워크 0).
"""

import asyncio
from typing import Any

from agent_org_network.ask_org import Answered, Pending
from agent_org_network.demo import build_demo
from agent_org_network.mcp_server import create_mcp_server, reply_to_mcp_text
from agent_org_network.runtime import StubRuntime

# 라우팅 내부 구조값 — MCP 텍스트/structured에 절대 새면 안 되는 토큰들.
# web `_LEAKY_KEYS`(confidence·candidates·escalated_to·reason·primary·intent)의
# *합집합* + MCP 분산 경로 내부값(manager_id·ticket_id) — 노출 금지어를 web과 대칭
# (오히려 상위)으로 맞춰 회귀 방어를 강화한다. `intent`·`primary`도 조직 내부값이다.
_LEAKY_TOKENS = (
    "confidence",
    "candidate",
    "escalated_to",
    "manager_id",
    "ticket_id",
    "reason",
    "primary",
    "intent",
)


# ── reply_to_mcp_text — Answered 투영 ──────────────────────────────────


def test_answered_텍스트에_답본문_담당_모드_출처가_담긴다():
    reply = Answered(
        text="계약 조건은 표준계약서 5조를 따릅니다.",
        answered_by=("legal_lead", "contract_ops"),
        mode="full",
        sources=("위키/계약가이드", "Notion/표준계약서"),
    )
    text = reply_to_mcp_text(reply)

    # 답 본문이 그대로 들어간다.
    assert "계약 조건은 표준계약서 5조를 따릅니다." in text
    # 담당(owner/agent_id) 표식.
    assert "legal_lead" in text
    assert "contract_ops" in text
    # 신뢰 상태(mode) 노출 — full도 그대로 텍스트에.
    assert "full" in text
    # 출처 둘 다.
    assert "위키/계약가이드" in text
    assert "Notion/표준계약서" in text


def test_answered_draft_only_모드가_텍스트에_정확히_노출된다():
    reply = Answered(
        text="초안 답변입니다.",
        answered_by=("legal_lead", "contract_ops"),
        mode="draft_only",
        sources=("위키/계약가이드",),
    )
    text = reply_to_mcp_text(reply)

    assert "초안 답변입니다." in text
    assert "draft_only" in text


def test_answered_backup_모드가_텍스트에_정확히_노출된다():
    reply = Answered(
        text="담당 부재 중 백업 답변입니다.",
        answered_by=("cs_lead", "cs_ops"),
        mode="backup",
        sources=("위키/환불정책",),
    )
    text = reply_to_mcp_text(reply)

    assert "담당 부재 중 백업 답변입니다." in text
    assert "backup" in text
    assert "cs_lead" in text
    assert "cs_ops" in text


def test_answered_출처가_없어도_투영된다():
    reply = Answered(
        text="출처 없는 답.",
        answered_by=("finance_lead", "finance_ops"),
        mode="full",
        sources=(),
    )
    text = reply_to_mcp_text(reply)

    assert "출처 없는 답." in text
    assert "finance_lead" in text
    assert "finance_ops" in text


def test_answered에_조직_내부값이_새지_않는다():
    # 내부값처럼 보이는 토큰이 우연히 답 본문/출처에 없도록 중립적인 내용을 쓴다.
    reply = Answered(
        text="환불은 영업일 기준 3일 내 처리됩니다.",
        answered_by=("cs_lead", "cs_ops"),
        mode="full",
        sources=("위키/환불정책",),
    )
    text = reply_to_mcp_text(reply)

    for token in _LEAKY_TOKENS:
        assert token not in text


# ── reply_to_mcp_text — Pending 투영 ───────────────────────────────────


def test_pending_contested_중립안내가_담기고_내부값은_없다():
    reply = Pending(kind="contested", message="담당을 확인하고 있어요. 정해지면 답변드릴게요.")
    text = reply_to_mcp_text(reply)

    assert "담당을 확인하고 있어요. 정해지면 답변드릴게요." in text
    for token in _LEAKY_TOKENS:
        assert token not in text
    # contested는 tracking이 없으니 추적 안내가 없어야 한다.
    assert "tracking" not in text


def test_pending_unowned_중립안내가_담기고_내부값은_없다():
    reply = Pending(
        kind="unowned", message="아직 담당이 없어 매니저에게 전달했어요. 답변되면 알림드릴게요."
    )
    text = reply_to_mcp_text(reply)

    assert "아직 담당이 없어 매니저에게 전달했어요. 답변되면 알림드릴게요." in text
    for token in _LEAKY_TOKENS:
        assert token not in text


def test_pending_dispatched_안내에_tracking_토큰이_담긴다():
    reply = Pending(
        kind="dispatched",
        message="담당에게 질문을 전달했어요. 답변이 준비되면 알림드릴게요.",
        tracking="deadbeefcafef00d",
    )
    text = reply_to_mcp_text(reply)

    assert "담당에게 질문을 전달했어요. 답변이 준비되면 알림드릴게요." in text
    # 불투명 추적 토큰은 노출 OK(ADR 0011 결정 6-5 — 답 회수용 1개 토큰).
    assert "deadbeefcafef00d" in text
    # 그러나 조직 내부 구조값은 여전히 없다.
    for token in _LEAKY_TOKENS:
        assert token not in text


def test_pending_dispatched_tracking이_None이면_토큰_안내가_없다():
    # 구조적으론 dispatched면 tracking이 채워지지만, None 방어를 확인한다.
    reply = Pending(
        kind="dispatched",
        message="담당에게 질문을 전달했어요. 답변이 준비되면 알림드릴게요.",
        tracking=None,
    )
    text = reply_to_mcp_text(reply)

    assert "담당에게 질문을 전달했어요. 답변이 준비되면 알림드릴게요." in text
    # 함수명대로 — tracking이 None이면 토큰 안내가 없어야 한다(음성 단언, N-1).
    assert "추적 토큰" not in text


# ── 도구 in-memory 스모크 (StubRuntime — 결정론) ───────────────────────


def _call_ask_org(server: Any, question: str) -> tuple[str, Any]:
    """FastMCP in-memory call_tool로 ask_org를 호출하고 (텍스트, structured)를 꺼낸다.

    call_tool은 async이며 `(content_blocks, structured)` 튜플을 돌려준다.
    content_blocks[0]가 TextContent라 `.text`에 우리 투영 텍스트가 담긴다. structured는
    SDK가 str 반환 도구에 자동 생성한 `{'result': <텍스트>}`(노출 검사용). `server: Any`로
    둬 `content[0].text`(ContentBlock 유니온) 접근이 pyright strict를 통과한다(인메모리 경계).
    """
    content, structured = asyncio.run(server.call_tool("ask_org", {"question": question}))
    return content[0].text, structured


def test_스모크_계약질문이_contract_ops_답으로_돌아온다():
    ask = build_demo(runtime=StubRuntime()).ask
    server = create_mcp_server(ask)

    text, structured = _call_ask_org(server, "이 계약 조건 바꿔도 돼?")

    # StubRuntime은 카드 summary를 답으로 돌려준다 → contract_ops 담당.
    assert "contract_ops" in text
    assert "legal_lead" in text
    assert "full" in text
    # 노출 불변식 — 텍스트 채널.
    for token in _LEAKY_TOKENS:
        assert token not in text
    # 노출 불변식 — structured 채널(M-1). SDK는 str 반환 도구에 outputSchema를 자동
    # 생성해 structured `{'result': <텍스트>}`로 투영 텍스트를 복제한다 — 일반 MCP
    # 클라이언트가 structured를 읽을 수도 있으므로(ADR 0006) 이 채널에도 내부값이 0이어야.
    structured_text = str(structured)
    for token in _LEAKY_TOKENS:
        assert token not in structured_text


def test_스모크_주차장질문은_unowned_안내로_돌아온다():
    ask = build_demo(runtime=StubRuntime()).ask
    server = create_mcp_server(ask)

    text, _ = _call_ask_org(server, "주차장 정기권 어떻게 갱신해요?")

    # 미아 없음 — 담당 없으면 매니저 전달 안내(unowned 중립 문구).
    assert "매니저" in text
    for token in _LEAKY_TOKENS:
        assert token not in text


def test_도구가_user_파라미터를_받지_않는다_가장_불가():
    # ADR 0009 연결점: 사용자 신원은 서버 설정값(기본 guest)이지 도구 파라미터가 아니다.
    # 도구 스키마에 question만 있고 user/user_id가 없어야 가장(impersonation)이 막힌다.
    ask = build_demo(runtime=StubRuntime()).ask
    server = create_mcp_server(ask)

    tools = asyncio.run(server.list_tools())
    ask_org_tool = next(t for t in tools if t.name == "ask_org")
    props = ask_org_tool.inputSchema.get("properties", {})

    assert "question" in props
    assert "user" not in props
    assert "user_id" not in props
