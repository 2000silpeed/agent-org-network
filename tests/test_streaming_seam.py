"""ADR 0031 게이트 내 — 스트리밍 seam 값 객체·포트·디스패처·SSE 직렬화·stub 어댑터.

결정론 보장:
  - stub 스트리밍 런타임/블로킹 stub 주입으로 고정 청크열 → 결정 가능한 이벤트열·문자열.
  - 순수 SSE 직렬화 함수(IO 0·네트워크 0·SDK 0).

잠근 불변식(ADR 0031 결정 1·3·4):
  - 코어 포트 AgentRuntime.answer 무변경.
  - StreamingRuntime = @runtime_checkable Protocol(isinstance 감지).
  - dispatch_stream: StreamingRuntime이면 델타 그대로, 미지원이면 answer 1델타 폴백.
  - SSE 직렬화 망라성(5종)·노출 불변식(token에 내부값 0·error에 내부 예외 0).
"""

from __future__ import annotations

import json
from datetime import date

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import (
    AskEvent,
    DoneEvent,
    ErrorEvent,
    MetaEvent,
    PendingEvent,
    TokenEvent,
    serialize_sse_event,
)
from agent_org_network.dispatch import LocalStreamingDispatcher
from agent_org_network.runtime import (
    AnswerChunk,
    StreamingRuntime,
    StubRuntime,
    StubStreamingRuntime,
)


def card(agent_id: str = "finance_ops", owner: str = "alice") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="finance",
        summary="요약",
        domains=["finance"],
        last_reviewed_at=date(2026, 1, 1),
        knowledge_sources=["위키/예산"],
    )


# ── 값 객체: AnswerChunk ──────────────────────────────────────────────────


def test_AnswerChunk_는_text_delta_를_든다() -> None:
    chunk = AnswerChunk(text_delta="안녕")
    assert chunk.text_delta == "안녕"


def test_AnswerChunk_는_frozen_이다() -> None:
    import dataclasses

    chunk = AnswerChunk(text_delta="x")
    assert dataclasses.is_dataclass(chunk) or hasattr(chunk, "model_dump")
    try:
        chunk.text_delta = "y"  # type: ignore[misc]
        raised = False
    except (AttributeError, TypeError, ValueError):
        raised = True
    assert raised


# ── 포트: StreamingRuntime (runtime_checkable) ────────────────────────────


def test_StubStreamingRuntime_은_StreamingRuntime_이다() -> None:
    assert isinstance(StubStreamingRuntime(), StreamingRuntime)


def test_StubRuntime_은_StreamingRuntime_이_아니다() -> None:
    # 블로킹 stub은 answer_stream을 구현하지 않아 폴백 대상.
    assert not isinstance(StubRuntime(), StreamingRuntime)


def test_StubStreamingRuntime_은_고정_델타_시퀀스를_yield한다() -> None:
    runtime = StubStreamingRuntime(deltas=("토큰1", "토큰2", "토큰3"))
    chunks = list(runtime.answer_stream("질문", card()))
    assert [c.text_delta for c in chunks] == ["토큰1", "토큰2", "토큰3"]


def test_StubStreamingRuntime_의_answer_는_델타를_합친_완성_Answer() -> None:
    runtime = StubStreamingRuntime(deltas=("가", "나", "다"))
    answer = runtime.answer("질문", card())
    assert answer.text == "가나다"
    assert answer.sources == ("위키/예산",)
    assert answer.mode == "full"


# ── 스트리밍 디스패처: LocalStreamingDispatcher.dispatch_stream ────────────


def test_dispatch_stream_은_StreamingRuntime이면_델타를_그대로_흘린다() -> None:
    runtime = StubStreamingRuntime(deltas=("A", "B", "C"))
    dispatcher = LocalStreamingDispatcher(runtime)
    chunks = list(dispatcher.dispatch_stream("질문", card()))
    assert [c.text_delta for c in chunks] == ["A", "B", "C"]


def test_dispatch_stream_은_미지원_런타임이면_answer를_한_델타로_yield한다() -> None:
    # StubRuntime은 StreamingRuntime이 아님 → answer 완성 텍스트 1델타 폴백.
    dispatcher = LocalStreamingDispatcher(StubRuntime())
    chunks = list(dispatcher.dispatch_stream("질문", card()))
    assert len(chunks) == 1
    assert chunks[0].text_delta == StubRuntime().answer("질문", card()).text


def test_dispatch_stream_델타들을_합치면_완성_Answer_텍스트와_같다() -> None:
    runtime = StubStreamingRuntime(deltas=("하나", "둘", "셋"))
    dispatcher = LocalStreamingDispatcher(runtime)
    chunks = list(dispatcher.dispatch_stream("질문", card()))
    joined = "".join(c.text_delta for c in chunks)
    assert joined == runtime.answer("질문", card()).text


def test_dispatch_stream_도_context를_런타임까지_전달한다() -> None:
    runtime = StubRuntime()
    dispatcher = LocalStreamingDispatcher(runtime)
    list(dispatcher.dispatch_stream("질문", card(), context="과거맥락"))
    assert runtime.last_context == "과거맥락"


def test_LocalStreamingDispatcher_는_기존_dispatch_poll도_지원한다() -> None:
    # 스트리밍 변형이되 RuntimeDispatcher 포트(블로킹 폴백)도 보존.
    from agent_org_network.dispatch import Delivered

    dispatcher = LocalStreamingDispatcher(StubRuntime())
    ticket = dispatcher.dispatch("질문", card())
    outcome = dispatcher.poll(ticket)
    assert isinstance(outcome, Delivered)


# ── SSE 직렬화 순수 함수 ──────────────────────────────────────────────────


def _parse_sse(frame: str) -> tuple[str, dict[str, object]]:
    """`event: <type>\\ndata: <json>\\n\\n` 프레임을 (event, data dict)로 파싱."""
    lines = frame.split("\n")
    event_line = next(line for line in lines if line.startswith("event:"))
    data_line = next(line for line in lines if line.startswith("data:"))
    event = event_line[len("event:") :].strip()
    data: dict[str, object] = json.loads(data_line[len("data:") :].strip())
    return event, data


def test_meta_이벤트_직렬화_event_type과_담당_mode_sources() -> None:
    event = MetaEvent(
        answered_by=("alice", "finance_ops"),
        mode="full",
        sources=("위키/예산",),
    )
    frame = serialize_sse_event(event)
    name, data = _parse_sse(frame)
    assert name == "meta"
    assert data["answered_by"] == {"owner": "alice", "agent_id": "finance_ops"}
    assert data["mode"] == "full"
    assert data["sources"] == ["위키/예산"]


def test_token_이벤트는_텍스트_델타만_싣는다() -> None:
    event = TokenEvent(text="토막")
    frame = serialize_sse_event(event)
    name, data = _parse_sse(frame)
    assert name == "token"
    assert data == {"text": "토막"}


def test_token_페이로드에_owner_agent_id_mode_sources_내부값_0() -> None:
    event = TokenEvent(text="델타")
    _, data = _parse_sse(serialize_sse_event(event))
    leaky = {"owner", "agent_id", "answered_by", "mode", "sources", "confidence", "tracking"}
    assert set(data.keys()).isdisjoint(leaky)
    assert set(data.keys()) == {"text"}


def test_done_이벤트_직렬화_최종_mode와_sources() -> None:
    event = DoneEvent(mode="draft_only", sources=("위키/예산", "Notion"))
    frame = serialize_sse_event(event)
    name, data = _parse_sse(frame)
    assert name == "done"
    assert data["mode"] == "draft_only"
    assert data["sources"] == ["위키/예산", "Notion"]


def test_pending_이벤트_직렬화_kind와_message() -> None:
    event = PendingEvent(kind="contested", message="담당 확인 중")
    frame = serialize_sse_event(event)
    name, data = _parse_sse(frame)
    assert name == "pending"
    assert data["kind"] == "contested"
    assert data["message"] == "담당 확인 중"
    assert "tracking" not in data


def test_pending_이벤트_dispatched는_tracking_토큰을_싣는다() -> None:
    event = PendingEvent(kind="dispatched", message="전달했어요", tracking="tok123")
    _, data = _parse_sse(serialize_sse_event(event))
    assert data["tracking"] == "tok123"


def test_error_이벤트_직렬화_중립_안내만() -> None:
    event = ErrorEvent(message="일시적인 문제로 답하지 못했어요.")
    frame = serialize_sse_event(event)
    name, data = _parse_sse(frame)
    assert name == "error"
    assert data == {"message": "일시적인 문제로 답하지 못했어요."}


def test_error_페이로드에_내부_예외나_스택_0() -> None:
    event = ErrorEvent(message="중립 안내")
    _, data = _parse_sse(serialize_sse_event(event))
    assert set(data.keys()) == {"message"}


def test_SSE_프레임은_event와_data_두_줄_빈줄로_끝난다() -> None:
    frame = serialize_sse_event(TokenEvent(text="x"))
    assert frame.startswith("event: token\n")
    assert "data: " in frame
    assert frame.endswith("\n\n")


def test_SSE_직렬화_순수성_고정_이벤트열_고정_문자열() -> None:
    events: list[AskEvent] = [
        MetaEvent(answered_by=("alice", "finance_ops"), mode="full", sources=()),
        TokenEvent(text="가"),
        TokenEvent(text="나"),
        DoneEvent(mode="full", sources=()),
    ]
    frames = [serialize_sse_event(e) for e in events]
    # 같은 입력 → 같은 출력(결정론).
    frames2 = [serialize_sse_event(e) for e in events]
    assert frames == frames2


def test_AskEvent_5종_모두_직렬화를_통과한다() -> None:
    events: list[AskEvent] = [
        MetaEvent(answered_by=("a", "b"), mode="full", sources=()),
        TokenEvent(text="x"),
        DoneEvent(mode="full", sources=()),
        PendingEvent(kind="unowned", message="m"),
        ErrorEvent(message="e"),
    ]
    for e in events:
        frame = serialize_sse_event(e)
        assert frame.startswith("event: ")


# ── 노출 투영 SSOT — serialize_reply와 공유 ───────────────────────────────


def test_serialize_reply_와_meta_done이_같은_투영을_공유한다() -> None:
    """serialize_reply(Answered)의 answered_by·mode·sources 투영이
    meta/done 투영과 동형 — 노출 SSOT 보존(같은 헬퍼)."""
    from agent_org_network.ask_org import Answered
    from agent_org_network.web import serialize_reply

    answered = Answered(
        text="본문",
        answered_by=("alice", "finance_ops"),
        mode="full",
        sources=("위키/예산",),
    )
    reply_dict = serialize_reply(answered)
    meta = MetaEvent(
        answered_by=("alice", "finance_ops"), mode="full", sources=("위키/예산",)
    )
    _, meta_data = _parse_sse(serialize_sse_event(meta))
    assert reply_dict["answered_by"] == meta_data["answered_by"]
    assert reply_dict["sources"] == meta_data["sources"]
