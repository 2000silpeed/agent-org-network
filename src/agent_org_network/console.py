"""운영자 콘솔 SSE 이벤트 직렬화 — ConsoleEvent sealed sum + serialize_console_event (T9.2(a)).

도메인 사건을 SSE 이벤트 페이로드(dict)로 투영하는 순수 함수.
render_mcp_notification·serialize_reply 정신 — 도메인 값에서만 투영, IO 0, SDK 0.

ConsoleEvent sealed sum 4+1 variant:
  - QuestionReceived: 질문 인입
  - RoutingDecisionRecorded: RoutingDecision(Routed/Contested/Unowned) 기존 sealed sum 재사용
  - AnswerSent: 답 전송
  - WorkerConnected: 워커 연결
  - WorkerDisconnected: 워커 해제

노출 불변식:
  - 콘솔은 운영 면 → 내부값(agent_id·decision 상세) 노출 OK.
  - 사용자向 비밀은 렌더 규율 — 도메인 값에서만 투영하므로 구조적으로 안 샘.
  - 사용자 채팅(OrgReply)과 다른 면(운영 면).

망라성(exhaustiveness):
  - match + assert_never로 ConsoleEvent(union) 전 variant를 망라한다.
  - 새 variant 추가 시 assert_never가 타입 에러를 냄(render_mcp_notification 정신).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, assert_never

from agent_org_network.decision import Contested, Routed, RoutingDecision, Unowned
from agent_org_network.token import WorkerRole

AnswerMode = Literal["full", "draft_only", "backup"]


# ── ConsoleEvent sealed sum ──────────────────────────────────────────────
#
# 운영자 콘솔이 SSE 피드로 노출하는 도메인 사건 4종.
# "타입이 곧 상태"(RoutingDecision·ReevalOutcome 정신) — 각 variant가 자기 필드를 들고
# serialize_console_event가 match+assert_never로 망라 직렬화.


@dataclass(frozen=True)
class QuestionReceived:
    """질문 인입 사건 — 사용자 질문이 중앙에 들어왔다."""

    question: str
    session_id: str
    at: datetime


@dataclass(frozen=True)
class RoutingDecisionRecorded:
    """라우팅 결정 사건 — Routed/Contested/Unowned 중 하나가 결정됐다.

    RoutingDecision(기존 sealed sum)을 그대로 재사용 — 새 추상 0.
    """

    decision: RoutingDecision
    at: datetime


@dataclass(frozen=True)
class AnswerSent:
    """답 전송 사건 — 워커(또는 런타임)가 답을 회신해 사용자에게 전달됐다."""

    ticket_id: str
    answered_by: str
    mode: AnswerMode
    at: datetime


@dataclass(frozen=True)
class WorkerConnected:
    """워커 연결 사건 — owner 워커가 중앙에 WebSocket으로 연결했다."""

    owner_id: str
    role: WorkerRole
    at: datetime


@dataclass(frozen=True)
class WorkerDisconnected:
    """워커 해제 사건 — owner 워커의 WebSocket 연결이 끊겼다."""

    owner_id: str
    role: WorkerRole
    at: datetime


ConsoleEvent = (
    QuestionReceived | RoutingDecisionRecorded | AnswerSent | WorkerConnected | WorkerDisconnected
)


# ── 직렬화 순수 함수 ─────────────────────────────────────────────────────
#
# serialize_console_event(event) -> dict:
#   도메인 사건 → SSE 이벤트 페이로드(dict). 순수 함수 — IO 0·SDK 0.
#   match+assert_never로 ConsoleEvent 전 variant 망라.
#   노출 불변식: 콘솔은 운영 면이라 내부값(agent_id·decision) OK.
#   실 SSE 스트리밍·브라우저 push는 게이트 밖(T9.2(c)).


def _serialize_routing_decision(decision: RoutingDecision) -> dict[str, object]:
    """RoutingDecision sealed sum → dict 투영(Routed/Contested/Unowned 망라)."""
    match decision:
        case Routed() as r:
            return {
                "decision_kind": "routed",
                "primary_agent_id": r.primary.agent_id,
                "confidence": r.confidence,
                "reason": r.reason,
                "intent": r.intent,
            }
        case Contested() as c:
            return {
                "decision_kind": "contested",
                "candidate_agent_ids": [card.agent_id for card in c.candidates],
                "reason": c.reason,
                "intent": c.intent,
            }
        case Unowned() as u:
            return {
                "decision_kind": "unowned",
                "escalated_to": u.escalated_to,
                "reason": u.reason,
                "intent": u.intent,
            }
        case _ as never:
            assert_never(never)


def serialize_console_event(event: ConsoleEvent) -> dict[str, object]:
    """ConsoleEvent → SSE 이벤트 페이로드(dict) — 순수 함수·망라성 보장.

    match+assert_never로 ConsoleEvent 전 variant를 망라한다(render_mcp_notification 정신).
    새 variant 추가 시 pyright가 assert_never 도달 가능으로 에러를 낸다.

    노출 불변식: 콘솔은 운영 면이라 내부값 노출 OK. 도메인 값에서만 투영하므로
    사용자向 비밀이 구조적으로 안 샘(Notification이 식별자만 든 것과 같은 결).
    """
    match event:
        case QuestionReceived(question=q, session_id=sid, at=at):
            return {
                "event_type": "question_received",
                "question": q,
                "session_id": sid,
                "at": at.isoformat(),
            }
        case RoutingDecisionRecorded(decision=decision, at=at):
            payload: dict[str, object] = {
                "event_type": "routing_decision_recorded",
                "at": at.isoformat(),
            }
            payload.update(_serialize_routing_decision(decision))
            return payload
        case AnswerSent(ticket_id=tid, answered_by=by, mode=mode, at=at):
            return {
                "event_type": "answer_sent",
                "ticket_id": tid,
                "answered_by": by,
                "mode": mode,
                "at": at.isoformat(),
            }
        case WorkerConnected(owner_id=oid, role=role, at=at):
            return {
                "event_type": "worker_connected",
                "owner_id": oid,
                "role": role,
                "at": at.isoformat(),
            }
        case WorkerDisconnected(owner_id=oid, role=role, at=at):
            return {
                "event_type": "worker_disconnected",
                "owner_id": oid,
                "role": role,
                "at": at.isoformat(),
            }
        case _ as never:
            assert_never(never)


# ── ConsoleFeed 허브 (T9.2(c) — 인프로세스 관전 브로드캐스트) ────────────────
#
# 운영자 콘솔 관전 화면에 도메인 사건을 실시간으로 흘리는 *인프로세스* 브로드캐스트 허브다.
# 발행자(AskOrg·WebSocketDispatcher)가 `emit(event)`으로 사건을 밀어 넣으면, 그 순간
# 구독 중인 모든 SSE 스트림(GET /console/feed)에 fan-out한다. 새 도메인 상태·전이 0 —
# 이미 일어난 도메인 사건을 *관전*용으로 복제해 흘릴 뿐이다(전이≠기록 정신 — 이건 기록도
# 아닌 관전 미러라 유실 허용).
#
# 동시성 모델: web이 스레드풀 병렬 실행이라(`/ask` 핸들러 스레드 ↔ `/console/feed` 스트림
# 스레드 ↔ 워커 WS 이벤트 루프) 구독자 집합 변경(subscribe/unsubscribe)과 fan-out(emit)이
# 경합한다. `RLock`으로 그 임계구역을 직렬화한다(web.py의 기존 락 결정과 동형). 구독 핸들은
# 스레드 안전 `queue.Queue`(자체 락 보유)라 emit가 put하고 스트림 스레드가 get한다.
#
# 백프레셔(느린 구독자 유실 정책): 각 구독 큐는 상한(`maxsize`)을 둔다. 상한 초과 시
# **가장 오래된 것을 버리고 최신을 넣는다**(drop-oldest) — 관전 피드는 유실 허용이라
# (감사 로그가 아님) 느린 구독자 하나가 emit를 블록해 발행자(본 흐름)를 멈추게 두지
# 않는다. 본 흐름 보호가 관전 완전성보다 우선이다.


class ConsoleSubscription:
    """한 관전 스트림의 구독 핸들 — 스레드 안전 큐 위 얇은 래퍼(T9.2(c)).

    `ConsoleFeed.subscribe()`가 만들어 돌려준다. SSE 스트림 스레드가 `get(timeout=)`으로
    이벤트를 꺼내고(없으면 timeout에 None), `ConsoleFeed.emit`가 fan-out으로 `_offer`한다.
    큐는 상한(`maxsize`)을 두고, 초과 시 drop-oldest로 최신 이벤트를 보존한다(유실 허용).
    """

    def __init__(self, maxsize: int) -> None:
        # 스레드 안전 FIFO(자체 락) — emit(put) ↔ 스트림(get) 경합을 큐가 흡수한다.
        self._queue: queue.Queue[ConsoleEvent] = queue.Queue(maxsize=maxsize)

    def offer(self, event: ConsoleEvent) -> None:
        """이벤트를 큐에 넣는다 — 가득 차면 가장 오래된 것을 버리고 최신을 넣는다(drop-oldest).

        `ConsoleFeed.emit`가 fan-out으로 호출한다(같은 모듈 협력자). 관전 피드는 유실
        허용이라 발행자를 블록하지 않는다.

        put_nowait가 Full이면 한 건 꺼내 버리고 재시도한다(경합 상황에서도 최선노력·
        블록 0). 그 사이 다른 스레드가 비워 다시 Full이 아닐 수도 있으므로 루프.
        """
        while True:
            try:
                self._queue.put_nowait(event)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    # 경합으로 이미 비워짐 — 재시도하면 이번엔 들어간다.
                    pass

    def get(self, timeout: float) -> ConsoleEvent | None:
        """이벤트 1건을 꺼낸다(스트림 스레드용). timeout 내 없으면 None(keep-alive 신호)."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class ConsoleFeed:
    """인프로세스 관전 브로드캐스트 허브 — emit를 모든 구독자에게 fan-out(T9.2(c)).

    발행자(AskOrg·WebSocketDispatcher)가 `emit(event)`로 도메인 사건을 밀어 넣고, 그 순간
    구독 중인 모든 `ConsoleSubscription` 큐에 복제한다. 구독자 0이면 no-op. subscribe/
    unsubscribe/emit는 `RLock`으로 직렬화한다(스레드풀 병렬 경합 방어).

    `maxsize`: 구독 큐 상한(느린 구독자 백프레셔). 초과 시 drop-oldest(유실 허용 — 관전
    피드는 감사 로그가 아니라 발행자 본 흐름 보호가 우선).
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._lock = threading.RLock()
        self._subscribers: list[ConsoleSubscription] = []

    def subscribe(self) -> ConsoleSubscription:
        """새 구독 핸들을 등록해 돌려준다 — 이후 emit가 이 큐에도 fan-out한다."""
        sub = ConsoleSubscription(self._maxsize)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: ConsoleSubscription) -> None:
        """구독을 해제한다 — 스트림 종료 시(finally) 호출. 미등록 핸들은 조용히 무시(멱등)."""
        with self._lock:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                pass

    def emit(self, event: ConsoleEvent) -> None:
        """이벤트를 모든 구독자 큐에 fan-out한다 — 구독자 0이면 no-op.

        락 안에서 현재 구독자 스냅샷을 잡아 각 큐에 `_offer`(drop-oldest)한다. 큐 offer는
        블록하지 않으므로(백프레셔 정책) 느린 구독자가 emit를 멈추지 않는다.
        """
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            sub.offer(event)

    def subscriber_count(self) -> int:
        """현재 구독자 수(테스트·관찰용)."""
        with self._lock:
            return len(self._subscribers)


def serialize_console_sse(event: ConsoleEvent) -> str:
    """ConsoleEvent를 SSE 프레임 문자열로 직렬화한다(순수 — `serialize_sse_event` 정신).

    프레임 형식: `event: <event_type>\\ndata: <json>\\n\\n`. `serialize_console_event`(순수
    페이로드 투영)를 감싸 SSE 와이어 형식으로 만든다 — 페이로드의 `event_type`을 SSE
    이벤트 이름으로도 쓴다(브라우저 EventSource가 `addEventListener(type)`로 갈래 처리).
    """
    import json

    payload = serialize_console_event(event)
    name = payload["event_type"]
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# SSE 코멘트 프레임(EventSource가 무시) — 프라이밍(연결 확정)·keep-alive(프록시 idle 방지).
CONSOLE_SSE_PRIMING = ": connected\n\n"
CONSOLE_SSE_KEEPALIVE = ": keep-alive\n\n"


def stream_console_frames(
    feed: ConsoleFeed,
    *,
    stop: Callable[[], bool],
    before_event: Callable[[], bool] | None = None,
    poll_timeout: float = 15.0,
) -> Iterator[str]:
    """관전 피드를 구독해 SSE 프레임 문자열을 흘리는 제너레이터(web 라우트·테스트 공유).

    절차:
      1. 프라이밍 프레임(`: connected`)을 즉시 yield — 헤더·연결을 바로 확정(프록시 버퍼
         방지·클라이언트 연결 인지).
      2. 루프: 구독 큐에서 이벤트를 `poll_timeout`만큼 기다려 pop.
        - 이벤트 있으면 `before_event`가 허용한 뒤 `serialize_console_sse` 프레임 yield.
          권한처럼 이벤트마다 다시 확인해야 하는 adapter는 이 seam으로, 큐에서 꺼낸
          사건이 권한 철회 뒤 외부로 나가지 않게 한다. False면 프레임을 내보내지 않고
          스트림을 끝낸다.
         - 없으면(timeout) keep-alive 코멘트 프레임 yield(프록시 idle 타임아웃 방지).
      3. `stop()`이 True면 루프 종료. **연결 종료·해제는 호출자(web 라우트)의 finally가
         `feed.unsubscribe`로 처리**한다 — 이 제너레이터는 구독 핸들만 만들어 쓰고, 종료
         정리는 소유자(라우트)가 한다(자원 소유 경계). `stop`은 테스트가 유한 종료를
         주입하는 seam(프로덕션 라우트는 항상 False를 넘겨 무한 스트림).

    web 라우트가 얇은 어댑터로 이 제너레이터를 감싼다(StreamingResponse·인증·finally
    unsubscribe). 이 제너레이터는 순수 로직(구독→pop→프레임)이라 결정론 단위 테스트가
    `stop`으로 유한하게 돌려 프레임 시퀀스를 단언할 수 있다(TestClient 무한 스트림 우회).
    """
    sub = feed.subscribe()
    try:
        yield CONSOLE_SSE_PRIMING
        while not stop():
            event = sub.get(timeout=poll_timeout)
            if event is None:
                yield CONSOLE_SSE_KEEPALIVE
            else:
                if before_event is not None and not before_event():
                    return
                yield serialize_console_sse(event)
    finally:
        feed.unsubscribe(sub)
