"""P17-native Question Request 스트림 이벤트와 bounded in-process broker."""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from threading import Condition, RLock
from typing import Annotated, Literal, Self, TypeAlias, TypeGuard, assert_never, final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from agent_org_network.answer_finalization import (
    AnswerFinalizationError,
    CompletionBundle,
    QuestionCompletionReader,
    canonical_completion_bundle,
)
from agent_org_network.question_request import (
    DeclinedRequest,
    FailedRequest,
    QuestionPendingKind,
    QuestionRequest,
    QuestionRequestStore,
    RequestStateKind,
)
from agent_org_network.runtime import AnswerMode


class QuestionStreamError(RuntimeError):
    """Question Request 스트림 경계의 기본 오류."""


class InvalidStreamEventError(QuestionStreamError):
    """이벤트 또는 terminal 증거가 canonical하지 않음."""


class StreamRequestMismatchError(QuestionStreamError):
    """구독 topic과 이벤트 request_id가 다름."""


class StreamEndConflictError(QuestionStreamError):
    """active topic 또는 구독에 서로 다른 terminal 결과가 생김."""


class UntrustedTerminalEventError(QuestionStreamError):
    """commit 증거 factory를 우회한 raw terminal 발행."""


class StreamCapacityError(QuestionStreamError):
    """bounded broker 또는 control queue의 용량 한도 초과."""


class StreamEventTooLargeError(StreamCapacityError):
    """제어 이벤트가 wire payload 상한을 초과함."""


class _StreamEventModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @field_validator("request_id", check_fields=False)
    @classmethod
    def _request_id_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request_id는 비어 있거나 공백일 수 없습니다.")
        return value


@final
class AcceptedEvent(_StreamEventModel):
    event_type: Literal["accepted"] = "accepted"
    request_id: str


@final
class TokenEvent(_StreamEventModel):
    event_type: Literal["token"] = "token"
    request_id: str
    text: str

    @field_validator("text")
    @classmethod
    def _text_must_not_be_empty(cls, value: str) -> str:
        if value == "":
            raise ValueError("token text는 빈 문자열일 수 없습니다.")
        return value


@final
class PendingEvent(_StreamEventModel):
    event_type: Literal["pending"] = "pending"
    request_id: str
    kind: QuestionPendingKind
    state: RequestStateKind
    retryable: bool
    message: str

    @field_validator("state")
    @classmethod
    def _state_must_be_nonterminal(cls, value: RequestStateKind) -> RequestStateKind:
        if value in ("answered", "declined", "failed"):
            raise ValueError("PendingEvent.state는 nonterminal이어야 합니다.")
        return value

    @field_validator("message")
    @classmethod
    def _message_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pending message는 비어 있거나 공백일 수 없습니다.")
        return value


@final
class DoneEvent(_StreamEventModel):
    """wire DTO. 발행 권한은 CompletionBundle factory가 별도로 부여한다."""

    event_type: Literal["done"] = "done"
    request_id: str
    record_id: str
    mode: AnswerMode
    sources: tuple[str, ...] = ()
    review_status: Literal["not_required", "approved"]
    answered_by: str
    agent_id: str

    @field_validator("record_id", "answered_by", "agent_id")
    @classmethod
    def _identifiers_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DoneEvent 식별자는 비어 있거나 공백일 수 없습니다.")
        return value

    @field_validator("sources")
    @classmethod
    def _sources_must_be_nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not source.strip() for source in value):
            raise ValueError("DoneEvent.sources에는 빈 출처를 둘 수 없습니다.")
        return value

    @classmethod
    def from_completion(cls, bundle: CompletionBundle) -> Self:
        try:
            canonical = canonical_completion_bundle(bundle)
        except AnswerFinalizationError as error:
            raise InvalidStreamEventError("CompletionBundle이 canonical하지 않습니다.") from error
        completion = canonical.completion
        return cls(
            request_id=completion.request_id,
            record_id=completion.record_id,
            mode=completion.mode,
            sources=completion.sources,
            review_status=completion.review_status,
            answered_by=completion.answered_by,
            agent_id=completion.agent_id,
        )


@final
class DeclinedEvent(_StreamEventModel):
    event_type: Literal["declined"] = "declined"
    request_id: str
    reason_code: str
    message: str

    @field_validator("reason_code", "message")
    @classmethod
    def _values_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("declined reason/message는 비어 있거나 공백일 수 없습니다.")
        return value


@final
class FailedEvent(_StreamEventModel):
    event_type: Literal["failed"] = "failed"
    request_id: str
    error_code: str
    message: str

    @field_validator("error_code", "message")
    @classmethod
    def _values_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("failed code/message는 비어 있거나 공백일 수 없습니다.")
        return value


@final
class InterruptedEvent(_StreamEventModel):
    event_type: Literal["interrupted"] = "interrupted"
    request_id: str
    retryable: bool
    message: str

    @field_validator("message")
    @classmethod
    def _message_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("interrupted message는 비어 있거나 공백일 수 없습니다.")
        return value


QuestionStreamEvent: TypeAlias = Annotated[
    AcceptedEvent
    | TokenEvent
    | PendingEvent
    | DoneEvent
    | DeclinedEvent
    | FailedEvent
    | InterruptedEvent,
    Field(discriminator="event_type"),
]
TerminalStreamEvent: TypeAlias = DoneEvent | DeclinedEvent | FailedEvent
ConnectionEndEvent: TypeAlias = PendingEvent | InterruptedEvent

_EVENT_ADAPTER: TypeAdapter[QuestionStreamEvent] = TypeAdapter(QuestionStreamEvent)
_EVENT_TYPES = (
    AcceptedEvent,
    TokenEvent,
    PendingEvent,
    DoneEvent,
    DeclinedEvent,
    FailedEvent,
    InterruptedEvent,
)


def _canonical_event(event: object) -> QuestionStreamEvent:
    if type(event) not in _EVENT_TYPES:
        raise InvalidStreamEventError("알 수 없는 Question Stream 이벤트입니다.")
    assert isinstance(event, _StreamEventModel)
    try:
        return _EVENT_ADAPTER.validate_python(
            event.model_dump(mode="python", round_trip=True), strict=True
        )
    except (TypeError, ValueError) as error:
        raise InvalidStreamEventError("Question Stream 이벤트가 canonical하지 않습니다.") from error


def _is_terminal(event: QuestionStreamEvent) -> TypeGuard[TerminalStreamEvent]:
    return isinstance(event, (DoneEvent, DeclinedEvent, FailedEvent))


def _ends_connection(event: QuestionStreamEvent) -> bool:
    return _is_terminal(event) or isinstance(event, (PendingEvent, InterruptedEvent))


def _validated_int(value: object, *, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name}는 {minimum} 이상의 정수여야 합니다.")
    return value


def _validated_request_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("request_id는 nonblank 문자열이어야 합니다.")
    return value


def _wire_data(event: QuestionStreamEvent) -> dict[str, object]:
    match event:
        case AcceptedEvent(request_id=request_id):
            return {"request_id": request_id}
        case TokenEvent(request_id=request_id, text=text):
            return {"request_id": request_id, "text": text}
        case PendingEvent(
            request_id=request_id,
            kind=kind,
            state=state,
            retryable=retryable,
            message=message,
        ):
            return {
                "request_id": request_id,
                "kind": kind,
                "state": state,
                "retryable": retryable,
                "message": message,
            }
        case DoneEvent():
            return {
                "request_id": event.request_id,
                "record_id": event.record_id,
                "mode": event.mode,
                "sources": list(event.sources),
                "review_status": event.review_status,
                "answered_by": event.answered_by,
                "agent_id": event.agent_id,
            }
        case DeclinedEvent(request_id=request_id, reason_code=code, message=message):
            return {"request_id": request_id, "reason_code": code, "message": message}
        case FailedEvent(request_id=request_id, error_code=code, message=message):
            return {"request_id": request_id, "error_code": code, "message": message}
        case InterruptedEvent(request_id=request_id, retryable=retryable, message=message):
            return {"request_id": request_id, "retryable": retryable, "message": message}
        case _ as never:
            assert_never(never)


def _serialize_canonical(event: QuestionStreamEvent) -> str:
    payload = json.dumps(_wire_data(event), ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.event_type}\ndata: {payload}\n\n"


def _event_size(event: QuestionStreamEvent) -> int:
    return len(_serialize_canonical(event).encode("utf-8"))


class QuestionStreamSubscription:
    """한 request topic의 bounded 구독. 공개 offer는 nonterminal만 받는다."""

    def __init__(
        self,
        *,
        request_id: str,
        max_queue_size: int,
        on_close: Callable[[QuestionStreamSubscription], None],
        max_event_bytes: int = 65_536,
    ) -> None:
        self._request_id = _validated_request_id(request_id)
        self._max_queue_size = _validated_int(max_queue_size, name="max_queue_size", minimum=2)
        self._max_event_bytes = _validated_int(max_event_bytes, name="max_event_bytes", minimum=1)
        if not callable(on_close):
            raise ValueError("on_close callback은 callable이어야 합니다.")
        self._on_close = on_close
        self._events: deque[QuestionStreamEvent] = deque()
        self._condition = Condition(RLock())
        self._closed = False
        self._accepted = False
        self._connection_end: ConnectionEndEvent | None = None
        self._terminal: TerminalStreamEvent | None = None

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def _payload_allowed(self, event: QuestionStreamEvent) -> bool:
        if _event_size(event) <= self._max_event_bytes:
            return True
        if isinstance(event, TokenEvent):
            return False
        raise StreamEventTooLargeError("제어 이벤트가 max_event_bytes를 초과했습니다.")

    def offer(self, event: QuestionStreamEvent) -> bool:
        canonical = _canonical_event(event)
        if _is_terminal(canonical):
            raise UntrustedTerminalEventError(
                "terminal은 completion/request terminal reconcile API로만 발행합니다."
            )
        if canonical.request_id != self._request_id:
            raise StreamRequestMismatchError("구독과 이벤트의 request_id가 다릅니다.")
        if not self._payload_allowed(canonical):
            return False
        with self._condition:
            if self._closed or self._terminal is not None or self._connection_end is not None:
                return False
            if isinstance(canonical, AcceptedEvent) and self._accepted:
                return False
            if isinstance(canonical, TokenEvent) and len(self._events) >= self._max_queue_size:
                return False
            if len(self._events) >= self._max_queue_size and not self._discard_token():
                raise StreamCapacityError("control-only 구독 큐가 가득 찼습니다.")
            self._events.append(canonical)
            if isinstance(canonical, AcceptedEvent):
                self._accepted = True
            if isinstance(canonical, (PendingEvent, InterruptedEvent)):
                self._connection_end = canonical
            self._condition.notify()
            return True

    def _discard_token(self) -> bool:
        for index, queued in enumerate(self._events):
            if isinstance(queued, TokenEvent):
                del self._events[index]
                return True
        return False

    def _preflight_terminal(self, event: TerminalStreamEvent) -> None:
        if event.request_id != self._request_id:
            raise StreamRequestMismatchError("구독과 terminal request_id가 다릅니다.")
        self._payload_allowed(event)
        with self._condition:
            if self._terminal is not None and self._terminal != event:
                raise StreamEndConflictError("구독에 다른 terminal 결과가 이미 있습니다.")

    def _offer_terminal(self, event: TerminalStreamEvent) -> bool:
        canonical = _canonical_event(event)
        if not _is_terminal(canonical):
            raise UntrustedTerminalEventError("내부 terminal offer에는 terminal만 허용합니다.")
        self._preflight_terminal(canonical)
        with self._condition:
            if self._closed:
                return False
            if self._terminal is not None:
                return False
            self._events = deque(
                queued
                for queued in self._events
                if not isinstance(queued, (PendingEvent, InterruptedEvent))
            )
            self._connection_end = None
            if len(self._events) >= self._max_queue_size and not self._discard_token():
                raise StreamCapacityError("terminal을 보존할 bounded queue 자리가 없습니다.")
            self._terminal = canonical
            self._events.append(canonical)
            self._condition.notify()
            return True

    def get(self, timeout: float | None = None) -> QuestionStreamEvent | None:
        if timeout is not None and (
            isinstance(timeout, bool) or timeout < 0 or not math.isfinite(timeout)
        ):
            raise ValueError("timeout은 0 이상의 유한한 수여야 합니다.")
        with self._condition:
            ready = self._condition.wait_for(
                lambda: bool(self._events) or self._closed, timeout=timeout
            )
            if not ready or not self._events:
                return None
            return _canonical_event(self._events.popleft())

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._events.clear()
            self._condition.notify_all()
        self._on_close(self)


@dataclass
class _Topic:
    subscribers: set[QuestionStreamSubscription]
    terminal: TerminalStreamEvent | None = None


class InMemoryQuestionStreamBroker:
    """token/history를 저장하지 않는 bounded request-keyed fan-out broker."""

    def __init__(
        self,
        *,
        max_queue_size: int = 64,
        max_topics: int = 1_024,
        max_subscribers_per_topic: int = 64,
        max_event_bytes: int = 65_536,
        requests: QuestionRequestStore | None = None,
        completions: QuestionCompletionReader | None = None,
    ) -> None:
        self._max_queue_size = _validated_int(max_queue_size, name="max_queue_size", minimum=2)
        self._max_topics = _validated_int(max_topics, name="max_topics", minimum=1)
        self._max_subscribers_per_topic = _validated_int(
            max_subscribers_per_topic,
            name="max_subscribers_per_topic",
            minimum=1,
        )
        self._max_event_bytes = _validated_int(max_event_bytes, name="max_event_bytes", minimum=1)
        self._requests = requests
        self._completions = completions
        self._lock = RLock()
        self._topics: dict[str, _Topic] = {}

    def subscribe(self, request_id: str) -> QuestionStreamSubscription:
        request_id = _validated_request_id(request_id)
        with self._lock:
            topic = self._topics.get(request_id)
            if topic is None:
                if len(self._topics) >= self._max_topics:
                    raise StreamCapacityError("max_topics 한도를 초과했습니다.")
                topic = _Topic(subscribers=set())
                self._topics[request_id] = topic
            if len(topic.subscribers) >= self._max_subscribers_per_topic:
                raise StreamCapacityError("topic 구독자 한도를 초과했습니다.")
            subscription = QuestionStreamSubscription(
                request_id=request_id,
                max_queue_size=self._max_queue_size,
                max_event_bytes=self._max_event_bytes,
                on_close=self._remove,
            )
            if topic.terminal is not None:
                subscription._offer_terminal(  # pyright: ignore[reportPrivateUsage]
                    topic.terminal
                )
            topic.subscribers.add(subscription)
            return subscription

    def _payload_allowed(self, event: QuestionStreamEvent) -> bool:
        if _event_size(event) <= self._max_event_bytes:
            return True
        if isinstance(event, TokenEvent):
            return False
        raise StreamEventTooLargeError("제어 이벤트가 max_event_bytes를 초과했습니다.")

    def publish(self, event: QuestionStreamEvent) -> int:
        canonical = _canonical_event(event)
        if _is_terminal(canonical):
            raise UntrustedTerminalEventError(
                "terminal은 publish_completion/publish_request_terminal로만 발행합니다."
            )
        with self._lock:
            topic = self._topics.get(canonical.request_id)
            if topic is None or topic.terminal is not None:
                return 0
            if not self._payload_allowed(canonical):
                return 0
            return sum(subscription.offer(canonical) for subscription in tuple(topic.subscribers))

    def publish_completion(self, request_id: str) -> int:
        return self._publish_terminal(self._completion_event(request_id))

    def publish_request_terminal(self, request_id: str, *, message: str) -> int:
        return self._publish_terminal(self._request_terminal_event(request_id, message=message))

    def reconcile_completion(
        self,
        subscription: QuestionStreamSubscription,
        request_id: str,
    ) -> bool:
        """한 late subscriber에 commit 증거 기반 Done을 원자적으로 재조정한다."""
        return self._reconcile_terminal(
            subscription,
            self._completion_event(request_id),
        )

    def reconcile_request_terminal(
        self,
        subscription: QuestionStreamSubscription,
        request_id: str,
        *,
        message: str,
    ) -> bool:
        """한 late subscriber에 canonical Declined/Failed를 원자적으로 재조정한다."""
        return self._reconcile_terminal(
            subscription,
            self._request_terminal_event(request_id, message=message),
        )

    def _evidence_ports(
        self,
    ) -> tuple[QuestionRequestStore, QuestionCompletionReader]:
        if self._requests is None or self._completions is None:
            raise UntrustedTerminalEventError(
                "terminal 발행에는 trusted Request store와 completion reader가 필요합니다."
            )
        return self._requests, self._completions

    def _exact_request(self, request_id: str) -> QuestionRequest:
        correlated_id = _validated_request_id(request_id)
        requests, _ = self._evidence_ports()
        try:
            stored = requests.get(correlated_id)
        except Exception as error:
            raise UntrustedTerminalEventError(
                "Question Request terminal 증거를 exact-read하지 못했습니다."
            ) from error
        if stored is None:
            raise UntrustedTerminalEventError(
                "저장되지 않은 Question Request로 terminal을 발행할 수 없습니다."
            )
        canonical = _canonical_request(stored)
        if canonical.request_id != correlated_id:
            raise UntrustedTerminalEventError(
                "Request store lookup key와 terminal 증거 request_id가 다릅니다."
            )
        return canonical

    def _completion_event(self, request_id: str) -> DoneEvent:
        correlated_id = _validated_request_id(request_id)
        stored = self._exact_request(correlated_id)
        _, completions = self._evidence_ports()
        try:
            raw = completions.by_request(correlated_id)
        except Exception as error:
            raise UntrustedTerminalEventError(
                "CompletionBundle terminal 증거를 exact-read하지 못했습니다."
            ) from error
        if raw is None:
            raise UntrustedTerminalEventError(
                "저장되지 않은 CompletionBundle로 Done을 발행할 수 없습니다."
            )
        try:
            bundle = canonical_completion_bundle(raw)
        except AnswerFinalizationError as error:
            raise InvalidStreamEventError(
                "저장된 CompletionBundle이 canonical하지 않습니다."
            ) from error
        if bundle.completion.request_id != correlated_id or bundle.request != stored:
            raise InvalidStreamEventError(
                "Request store와 completion reader의 terminal 증거가 일치하지 않습니다."
            )
        return DoneEvent.from_completion(bundle)

    def _request_terminal_event(
        self,
        request_id: str,
        *,
        message: str,
    ) -> DeclinedEvent | FailedEvent:
        correlated_id = _validated_request_id(request_id)
        stored = self._exact_request(correlated_id)
        _, completions = self._evidence_ports()
        try:
            completion = completions.by_request(correlated_id)
        except Exception as error:
            raise UntrustedTerminalEventError(
                "Completion terminal 충돌 여부를 exact-read하지 못했습니다."
            ) from error
        if completion is not None:
            raise InvalidStreamEventError(
                "CompletionBundle이 존재하는 Request를 Declined/Failed로 발행할 수 없습니다."
            )
        return _event_from_request_terminal(stored, message=message)

    def _preflight_topic(self, topic: _Topic, event: TerminalStreamEvent) -> None:
        if topic.terminal is not None and topic.terminal != event:
            raise StreamEndConflictError("active topic에 다른 terminal 결과가 이미 있습니다.")
        for subscription in topic.subscribers:
            subscription._preflight_terminal(event)  # pyright: ignore[reportPrivateUsage]

    def _publish_terminal(self, event: TerminalStreamEvent) -> int:
        self._payload_allowed(event)
        with self._lock:
            topic = self._topics.get(event.request_id)
            if topic is None:
                return 0
            self._preflight_topic(topic, event)
            if topic.terminal is None:
                topic.terminal = event
            return sum(
                subscription._offer_terminal(event)  # pyright: ignore[reportPrivateUsage]
                for subscription in tuple(topic.subscribers)
            )

    def _reconcile_terminal(
        self,
        subscription: QuestionStreamSubscription,
        event: TerminalStreamEvent,
    ) -> bool:
        self._payload_allowed(event)
        with self._lock:
            topic = self._topics.get(subscription.request_id)
            if topic is None or subscription not in topic.subscribers:
                return False
            self._preflight_topic(topic, event)
            if topic.terminal is None:
                topic.terminal = event
            return subscription._offer_terminal(event)  # pyright: ignore[reportPrivateUsage]

    def _remove(self, subscription: QuestionStreamSubscription) -> None:
        with self._lock:
            topic = self._topics.get(subscription.request_id)
            if topic is None:
                return
            topic.subscribers.discard(subscription)
            if not topic.subscribers:
                del self._topics[subscription.request_id]

    def topic_count(self) -> int:
        with self._lock:
            return len(self._topics)

    def subscriber_count(self, request_id: str | None = None) -> int:
        with self._lock:
            if request_id is None:
                return sum(len(topic.subscribers) for topic in self._topics.values())
            topic = self._topics.get(request_id)
            return 0 if topic is None else len(topic.subscribers)


def _canonical_request(request: object) -> QuestionRequest:
    if type(request) is not QuestionRequest:
        raise InvalidStreamEventError("QuestionRequest exact type이 필요합니다.")
    assert isinstance(request, QuestionRequest)
    try:
        return QuestionRequest.model_validate(
            request.model_dump(mode="python", round_trip=True), strict=True
        )
    except Exception as error:
        raise InvalidStreamEventError("QuestionRequest가 canonical하지 않습니다.") from error


def _event_from_request_terminal(
    request: QuestionRequest,
    *,
    message: str,
) -> DeclinedEvent | FailedEvent:
    canonical = _canonical_request(request)
    state = canonical.state
    if isinstance(state, DeclinedRequest):
        return DeclinedEvent(
            request_id=canonical.request_id,
            reason_code=state.reason_code,
            message=message,
        )
    if isinstance(state, FailedRequest):
        return FailedEvent(
            request_id=canonical.request_id,
            error_code=state.error_code,
            message=message,
        )
    raise InvalidStreamEventError(
        "publish_request_terminal에는 DeclinedRequest 또는 FailedRequest가 필요합니다."
    )


def serialize_question_stream_sse(event: QuestionStreamEvent) -> str:
    return _serialize_canonical(_canonical_event(event))


QUESTION_STREAM_SSE_PRIMING = ": connected\n\n"
QUESTION_STREAM_SSE_KEEPALIVE = ": keep-alive\n\n"


def iter_question_stream_frames(
    subscription: QuestionStreamSubscription,
    *,
    max_polls: int,
    poll_timeout: float = 15.0,
) -> Iterator[str]:
    polls = _validated_int(max_polls, name="max_polls", minimum=0)
    yield QUESTION_STREAM_SSE_PRIMING
    for _ in range(polls):
        event = subscription.get(timeout=poll_timeout)
        if event is None:
            if subscription.closed:
                return
            yield QUESTION_STREAM_SSE_KEEPALIVE
            continue
        yield serialize_question_stream_sse(event)
        if _ends_connection(event):
            return
