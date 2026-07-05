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

import pytest

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


# ── reply_to_mcp_text — record_id 노출 (계획 §10.4) ─────────────────────


def test_answered에_record_id가_있으면_피드백_참조_라인이_붙는다():
    reply = Answered(
        text="환불은 영업일 3일 내 처리됩니다.",
        answered_by=("cs_lead", "cs_ops"),
        mode="full",
        sources=("위키/환불정책",),
        record_id="deadbeefcafef00d",
    )
    text = reply_to_mcp_text(reply)

    assert "피드백 참조: deadbeefcafef00d" in text
    # 불투명 uuid hex라 내부 구조를 비추지 않는다(노출 불변식 정밀화 — tracking과 같은 결).
    for token in _LEAKY_TOKENS:
        assert token not in text


def test_answered에_record_id가_None이면_피드백_참조_라인이_없다():
    reply = Answered(
        text="출처 없는 답.",
        answered_by=("finance_lead", "finance_ops"),
        mode="full",
        sources=(),
        record_id=None,
    )
    text = reply_to_mcp_text(reply)

    assert "피드백 참조" not in text


# ── submit_feedback 도구 (계획 §10.4 — 결정론·스텁 주입) ────────────────


def _feedback_server():
    """answer_record_store·feedback_store를 주입한 MCP 서버 + 그 두 스토어를 돌려준다.

    `build_demo(answer_record_store=...)`로 `ask`가 답 확정 시 `AnswerRecord`를 적재하게
    하고, 같은 answer_record_store와 InMemoryFeedbackStore를 `create_mcp_server`에 물린다.
    """
    from agent_org_network.answer_record import (
        InMemoryAnswerRecordStore,
        InMemoryFeedbackStore,
    )

    answer_store = InMemoryAnswerRecordStore()
    feedback_store = InMemoryFeedbackStore()
    ask = build_demo(runtime=StubRuntime(), answer_record_store=answer_store).ask
    server = create_mcp_server(
        ask, feedback_store=feedback_store, answer_record_store=answer_store
    )
    return server, answer_store, feedback_store


def _call_submit_feedback(
    server: Any, record_id: str, verdict: str, comment: str = ""
) -> str:
    content, _ = asyncio.run(
        server.call_tool(
            "submit_feedback",
            {"record_id": record_id, "verdict": verdict, "comment": comment},
        )
    )
    return content[0].text


def test_submit_feedback_도구는_스토어_주입시에만_등록된다():
    # 미주입이면 ask_org만(하위호환), 주입이면 submit_feedback도 등록.
    plain = create_mcp_server(build_demo(runtime=StubRuntime()).ask)
    plain_tools = {t.name for t in asyncio.run(plain.list_tools())}
    assert "submit_feedback" not in plain_tools

    server, _answer, _feedback = _feedback_server()
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert "submit_feedback" in tools


def test_submit_feedback_도구는_submitted_by를_파라미터로_안_받는다_가장_불가():
    # ADR 0009: submitted_by는 서버 설정값(mcp_guest)이지 도구 파라미터가 아니다.
    server, _answer, _feedback = _feedback_server()
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "submit_feedback")
    props = tool.inputSchema.get("properties", {})

    assert set(props) >= {"record_id", "verdict"}
    assert "submitted_by" not in props
    assert "user" not in props
    assert "user_id" not in props


def test_submit_feedback_정상_접수():
    server, _answer_store, feedback_store = _feedback_server()
    # ask_org로 답을 받아 record_id 손잡이를 얻는다(StubRuntime — 결정론).
    text, _ = _call_ask_org(server, "이 계약 조건 바꿔도 돼?")
    record_id = text.split("피드백 참조: ")[1].strip()

    result = _call_submit_feedback(server, record_id, "bad", "틀린 것 같아요")

    assert "접수" in result
    # 스토어에 mcp_guest 신원으로 실린다(도구 파라미터가 아니라 서버 설정값).
    latest = feedback_store.latest_for_record(record_id)
    assert latest is not None
    assert latest.verdict == "bad"
    assert latest.submitted_by == "mcp_guest"
    assert latest.comment == "틀린 것 같아요"


def test_submit_feedback_미존재_record_id는_거부_안내():
    server, _answer, feedback_store = _feedback_server()

    result = _call_submit_feedback(server, "ghost-record", "good")

    assert "알 수 없는" in result
    # 미존재면 스토어에 적재하지 않는다.
    assert feedback_store.latest_for_record("ghost-record") is None


def test_submit_feedback_잘못된_verdict는_스키마에서_거부():
    # verdict는 Literal["good","bad"]이라 MCP 입력 스키마 단에서 거부된다(FeedbackVerdict SSOT).
    server, _answer, _feedback = _feedback_server()
    with pytest.raises(Exception):
        asyncio.run(
            server.call_tool(
                "submit_feedback",
                {"record_id": "rec-1", "verdict": "meh"},
            )
        )


def test_submit_feedback_verdict_스키마가_good_bad_이진값이다():
    server, _answer, _feedback = _feedback_server()
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "submit_feedback")
    verdict_schema = tool.inputSchema["properties"]["verdict"]
    # enum(또는 anyOf const)로 good|bad만 허용된다.
    assert "good" in str(verdict_schema)
    assert "bad" in str(verdict_schema)


def test_bad_피드백이_담당자_모니터링_검토필요에_뜬다_e2e():
    # 코어 조인(monitoring_for_owner) 재사용 확인 — MCP 피드백이 감독 면에 도달.
    from agent_org_network.answer_record import monitoring_for_owner
    from agent_org_network.answer_record import InMemoryCorrectionStore

    server, answer_store, feedback_store = _feedback_server()
    text, _ = _call_ask_org(server, "이 계약 조건 바꿔도 돼?")
    record_id = text.split("피드백 참조: ")[1].strip()

    # 적재된 레코드의 agent_id로 감독 조회.
    rec = answer_store.get(record_id)
    assert rec is not None
    correction_store = InMemoryCorrectionStore()

    # 피드백 전 — 검토 필요 아님(온라인·표식 없음).
    before = monitoring_for_owner(
        answer_store, correction_store, agent_id=rec.agent_id, feedback_store=feedback_store
    )
    item_before = next(it for it in before if it.record.record_id == record_id)
    assert item_before.needs_correction_review is False

    # bad 피드백 후 — 검토 필요 축에 합류(두 축 OR).
    _call_submit_feedback(server, record_id, "bad", "사실 오류")
    after = monitoring_for_owner(
        answer_store, correction_store, agent_id=rec.agent_id, feedback_store=feedback_store
    )
    item_after = next(it for it in after if it.record.record_id == record_id)
    assert item_after.needs_correction_review is True
    assert item_after.feedback is not None
    assert item_after.feedback.verdict == "bad"
