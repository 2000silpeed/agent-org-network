"""T9.2(c) — 운영자 콘솔 SSE 관전 피드 게이트 테스트 (ADR 0024·T9.2).

축(작업 지시 §5):
  - ConsoleFeed 허브: 구독/발행/드롭 정책(백프레셔)/스레드 안전(Barrier 결정론).
  - AskOrg emit 훅: Fake feed spy로 질문→라우팅→답 3사건 순서, 미주입 무회귀, emit 예외 흡수.
  - WebSocketDispatcher connect/disconnect emit.
  - SSE 라우트: TestClient로 스트림 프레임 파싱·인증 401·구독 해제.
  - 배선: 질문 1건→피드에 3사건 순서.

결정론: 순수 허브 + StubRuntime + 고정 clock. 실 LLM·실 네트워크 0.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.console import (
    ConsoleEvent,
    ConsoleFeed,
    QuestionReceived,
    WorkerConnected,
    WorkerDisconnected,
    serialize_console_sse,
    stream_console_frames,
)
from agent_org_network.runtime import StubRuntime
from agent_org_network.transport import RegisterWorker, WebSocketDispatcher
from agent_org_network.web import create_app

T0 = datetime(2026, 6, 27, 9, 0, 0, tzinfo=timezone.utc)


def _q(sid: str = "s1") -> QuestionReceived:
    return QuestionReceived(question="환불해줘", session_id=sid, at=T0)


# ── ConsoleFeed 허브 ───────────────────────────────────────────────────────


def test_subscribe_receives_emitted_event() -> None:
    feed = ConsoleFeed()
    sub = feed.subscribe()
    feed.emit(_q())
    got = sub.get(timeout=0.1)
    assert isinstance(got, QuestionReceived)
    assert got.session_id == "s1"


def test_emit_with_no_subscribers_is_noop() -> None:
    feed = ConsoleFeed()
    # 구독자 0이면 아무 일도 없다(예외 0).
    feed.emit(_q())
    assert feed.subscriber_count() == 0


def test_emit_fans_out_to_all_subscribers() -> None:
    feed = ConsoleFeed()
    a = feed.subscribe()
    b = feed.subscribe()
    feed.emit(_q())
    assert isinstance(a.get(timeout=0.1), QuestionReceived)
    assert isinstance(b.get(timeout=0.1), QuestionReceived)


def test_unsubscribe_stops_delivery() -> None:
    feed = ConsoleFeed()
    sub = feed.subscribe()
    feed.unsubscribe(sub)
    assert feed.subscriber_count() == 0
    feed.emit(_q())
    # 해제 후엔 큐에 안 들어온다.
    assert sub.get(timeout=0.05) is None


def test_unsubscribe_is_idempotent() -> None:
    feed = ConsoleFeed()
    sub = feed.subscribe()
    feed.unsubscribe(sub)
    feed.unsubscribe(sub)  # 두 번 해제해도 예외 없음.
    assert feed.subscriber_count() == 0


def test_get_timeout_returns_none_when_empty() -> None:
    feed = ConsoleFeed()
    sub = feed.subscribe()
    assert sub.get(timeout=0.05) is None


def test_backpressure_drops_oldest_over_capacity() -> None:
    # 상한 3 큐에 5건 넣으면 오래된 2건이 버려지고 최신 3건만 남는다(drop-oldest).
    feed = ConsoleFeed(maxsize=3)
    sub = feed.subscribe()
    events = [
        QuestionReceived(question=f"q{i}", session_id=f"s{i}", at=T0) for i in range(5)
    ]
    for ev in events:
        feed.emit(ev)
    drained: list[str] = []
    while True:
        got = sub.get(timeout=0.02)
        if got is None:
            break
        assert isinstance(got, QuestionReceived)
        drained.append(got.session_id)
    # 최신 3건(s2·s3·s4)만 남고 순서 보존.
    assert drained == ["s2", "s3", "s4"]


def test_thread_safe_concurrent_emit_barrier() -> None:
    # N 스레드가 Barrier로 동시에 emit — 총 N건이 유실 없이(상한 넉넉) 도착한다.
    n = 16
    feed = ConsoleFeed(maxsize=1024)
    sub = feed.subscribe()
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        feed.emit(QuestionReceived(question=f"q{i}", session_id=f"s{i}", at=T0))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seen: set[str] = set()
    while True:
        got = sub.get(timeout=0.05)
        if got is None:
            break
        assert isinstance(got, QuestionReceived)
        seen.add(got.session_id)
    assert seen == {f"s{i}" for i in range(n)}


def test_thread_safe_concurrent_subscribe_and_emit() -> None:
    # 구독 등록과 emit가 동시에 경합해도 크래시 없이 직렬화된다(RLock).
    feed = ConsoleFeed(maxsize=1024)
    barrier = threading.Barrier(8)

    def subscriber() -> None:
        barrier.wait()
        feed.subscribe()

    def emitter() -> None:
        barrier.wait()
        feed.emit(_q())

    threads = [
        threading.Thread(target=subscriber if i % 2 == 0 else emitter)
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert feed.subscriber_count() == 4


def test_serialize_console_sse_frame_shape() -> None:
    frame = serialize_console_sse(_q())
    assert frame.startswith("event: question_received\n")
    assert "data: " in frame
    assert frame.endswith("\n\n")
    assert "환불해줘" in frame


# ── AskOrg emit 훅 ─────────────────────────────────────────────────────────


class _SpyFeed(ConsoleFeed):
    """emit를 가로채 사건을 순서대로 기록하는 spy(발행 순서 단언용)."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[ConsoleEvent] = []

    def emit(self, event: ConsoleEvent) -> None:
        self.events.append(event)


class _BoomFeed(ConsoleFeed):
    """emit가 항상 폭발하는 feed(예외 흡수 계약 검증용)."""

    def emit(self, event: ConsoleEvent) -> None:
        raise RuntimeError("boom")


def _build_ask(console_feed: ConsoleFeed | None) -> Any:
    from agent_org_network.demo import build_demo

    return build_demo(runtime=StubRuntime(), console_feed=console_feed).ask


def test_ask_emits_three_events_in_order() -> None:
    spy = _SpyFeed()
    ask = _build_ask(spy)
    from agent_org_network.user import User

    ask.handle("환불 처리 방법 알려줘", User(id="u1"))
    kinds = [type(e).__name__ for e in spy.events]
    # 질문 인입 → 라우팅 결정 → (Routed 즉답이면) 답 확정 순.
    assert kinds[0] == "QuestionReceived"
    assert kinds[1] == "RoutingDecisionRecorded"
    assert "AnswerSent" in kinds
    assert kinds.index("AnswerSent") > kinds.index("RoutingDecisionRecorded")


def test_ask_question_received_carries_session_id() -> None:
    spy = _SpyFeed()
    ask = _build_ask(spy)
    from agent_org_network.user import User

    ask.handle("환불", User(id="alice-session"))
    qr = spy.events[0]
    assert isinstance(qr, QuestionReceived)
    assert qr.session_id == "alice-session"


def test_ask_without_feed_no_regression() -> None:
    # 미주입이면 발화 0·기존 동작 무변경(예외 없이 답이 나온다).
    ask = _build_ask(None)
    from agent_org_network.user import User

    reply = ask.handle("환불", User(id="u1"))
    assert reply is not None


def test_ask_absorbs_emit_exception() -> None:
    # emit가 폭발해도 본 흐름(질문 처리)은 안 깨진다(관전이 본 흐름을 못 깨는 계약).
    ask = _build_ask(_BoomFeed())
    from agent_org_network.user import User

    reply = ask.handle("환불", User(id="u1"))
    assert reply is not None


# ── WebSocketDispatcher connect/disconnect emit ─────────────────────────────


def _noop_send(frame: Any) -> None:
    return None


def test_dispatcher_emits_worker_connected_on_register() -> None:
    spy = _SpyFeed()
    disp = WebSocketDispatcher(console_feed=spy)
    disp.register(RegisterWorker(owner_id="alice", role="primary"), _noop_send)
    connected = [e for e in spy.events if isinstance(e, WorkerConnected)]
    assert len(connected) == 1
    assert connected[0].owner_id == "alice"
    assert connected[0].role == "primary"


def test_dispatcher_emits_worker_disconnected() -> None:
    spy = _SpyFeed()
    disp = WebSocketDispatcher(console_feed=spy)
    disp.register(RegisterWorker(owner_id="bob", role="backup"), _noop_send)
    disp.disconnect("bob", "backup")
    disconnected = [e for e in spy.events if isinstance(e, WorkerDisconnected)]
    assert len(disconnected) == 1
    assert disconnected[0].owner_id == "bob"
    assert disconnected[0].role == "backup"


def test_dispatcher_no_emit_on_auth_failure() -> None:
    # 인증 거부(빈 owner_id)면 등록 실패라 WorkerConnected를 emit하지 않는다.
    spy = _SpyFeed()
    disp = WebSocketDispatcher(console_feed=spy)
    disp.register(RegisterWorker(owner_id="", role="primary"), _noop_send)
    assert not any(isinstance(e, WorkerConnected) for e in spy.events)


def test_dispatcher_without_feed_no_regression() -> None:
    disp = WebSocketDispatcher()  # console_feed 미주입.
    reply = disp.register(RegisterWorker(owner_id="alice"), _noop_send)
    assert reply is not None  # 발화 0·정상 register.


# ── SSE 프레임 제너레이터(결정론 — TestClient 무한 스트림 우회) ───────────────
#
# StreamingResponse 무한 제너레이터는 TestClient(portal)에서 응답 시작 전 블록되는 하네스
# 한계가 있다(실 uvicorn·브라우저 EventSource는 정상 — 실 관전은 게이트 밖 시연). 그래서
# 라우트가 감싸는 순수 프레임 로직(`stream_console_frames`)을 유한 `stop`으로 직접 돌려
# 프레임 시퀀스·구독 해제를 결정론으로 단언한다.


def _drive_frames(feed: ConsoleFeed, stop_after: int) -> list[str]:
    """`stream_console_frames`를 유한 종료로 소비해 흘린 프레임을 리스트로 모은다.

    `stop_after`번 yield된 뒤 stop이 True가 되게 해 무한 루프를 끊는다(테스트 유한화).
    poll_timeout은 짧게(0.05) 둬 빈 큐면 keep-alive로 빠르게 넘어간다.
    """
    counter = {"n": 0}

    def stop() -> bool:
        return counter["n"] >= stop_after

    frames: list[str] = []
    for frame in stream_console_frames(feed, stop=stop, poll_timeout=0.05):
        frames.append(frame)
        counter["n"] += 1
        if counter["n"] >= stop_after:
            break
    return frames


def test_stream_frames_first_is_priming() -> None:
    feed = ConsoleFeed()
    frames = _drive_frames(feed, stop_after=1)
    assert frames[0] == ": connected\n\n"


def test_stream_frames_serializes_injected_event() -> None:
    feed = ConsoleFeed()
    feed.emit(_q("route-test"))  # 미리 큐에 넣어 둔다(구독은 제너레이터가 시작 시 붙임).
    # 제너레이터가 subscribe한 뒤 emit돼야 하므로, 별도 스레드가 구독 후 주입한다.
    result: list[str] = []

    def run() -> None:
        counter = {"n": 0}

        def stop() -> bool:
            return counter["n"] >= 5

        for frame in stream_console_frames(feed, stop=stop, poll_timeout=0.05):
            result.append(frame)
            counter["n"] += 1
            # 실제 이벤트 프레임을 받으면 조기 종료.
            if frame.startswith("event: question_received"):
                break

    def emitter() -> None:
        _wait_until(lambda: feed.subscriber_count() >= 1)
        feed.emit(_q("route-test"))

    t_run = threading.Thread(target=run)
    t_emit = threading.Thread(target=emitter, daemon=True)
    t_run.start()
    t_emit.start()
    t_run.join(timeout=3.0)
    t_emit.join(timeout=1.0)
    joined = "".join(result)
    assert "event: question_received" in joined
    assert "route-test" in joined


def test_stream_frames_keepalive_when_idle() -> None:
    feed = ConsoleFeed()
    # 이벤트 0 → 프라이밍 후 keep-alive가 흐른다(프록시 idle 방지).
    frames = _drive_frames(feed, stop_after=3)
    assert frames[0] == ": connected\n\n"
    assert any(f == ": keep-alive\n\n" for f in frames[1:])


def test_stream_frames_unsubscribes_on_exit() -> None:
    feed = ConsoleFeed()
    _drive_frames(feed, stop_after=2)
    # 제너레이터 finally가 unsubscribe해야 한다(자원 해제).
    assert feed.subscriber_count() == 0


def test_console_feed_route_registered() -> None:
    app = create_app(runtime=StubRuntime())
    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/console/feed" in routes
    assert "/console/view" in routes


def test_console_feed_route_requires_auth_when_enabled() -> None:
    app = create_app(runtime=StubRuntime(), session_secret="test-secret")
    client = TestClient(app)
    http = cast(Any, client)
    res = cast(Response, http.get("/console/feed"))
    assert res.status_code == 401


def test_console_view_route_serves_html() -> None:
    app = create_app(runtime=StubRuntime())
    client = TestClient(app)
    http = cast(Any, client)
    res = cast(Response, http.get("/console/view"))
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


# ── 배선(질문 1건 → 피드에 3사건 순서) ──────────────────────────────────────


def test_wiring_ask_endpoint_flows_events_to_feed() -> None:
    feed = _SpyFeed()
    app = create_app(runtime=StubRuntime(), console_feed=feed)
    client = TestClient(app)
    http = cast(Any, client)
    res = cast(Response, http.post("/ask", json={"question": "환불 처리 방법"}))
    assert res.status_code == 200
    kinds = [type(e).__name__ for e in feed.events]
    assert kinds[0] == "QuestionReceived"
    assert kinds[1] == "RoutingDecisionRecorded"
    assert "AnswerSent" in kinds


# ── 헬퍼 ────────────────────────────────────────────────────────────────────


def _wait_until(cond: Any, tries: int = 200, delay: float = 0.01) -> None:
    import time

    for _ in range(tries):
        if cond():
            return
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
