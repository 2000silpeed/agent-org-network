"""P17-native Question Request HTTP/SSE thin adapter.

이 모듈은 인증 신원을 Request-first 명령에 결박하고 이미 열린 구독을 HTTP로
직렬화할 뿐이다. Runtime·Approval·Finalization·producer 수명은 application이 소유한다.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Generator, Mapping
from typing import Protocol, TypeAlias, cast, final

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.responses import ContentStream
from starlette.types import Receive, Scope, Send

from agent_org_network.central_authority import AuthenticatedPrincipal
from agent_org_network.question_resolution import (
    AskQuestion,
    QuestionAuthorizationDeniedError,
    QuestionPrincipal,
    RequesterPrincipal,
)
from agent_org_network.question_stream import (
    DeclinedEvent,
    DoneEvent,
    FailedEvent,
    InterruptedEvent,
    PendingEvent,
    QUESTION_STREAM_SSE_KEEPALIVE,
    QUESTION_STREAM_SSE_PRIMING,
    QuestionStreamSubscription,
    serialize_question_stream_sse,
)
from agent_org_network.question_stream_execution import (
    AnsweredQuestionLookup,
    DeclinedQuestionLookup,
    FailedQuestionLookup,
    OpenQuestionStream,
    PendingQuestionLookup,
    QuestionStreamLookup,
    QuestionStreamRequestNotFoundError,
    QuestionStreamUnavailableError,
)


class RequesterNotAuthenticatedError(RuntimeError):
    """인증 정보가 없다는 principal resolver의 공개 신호."""


class _RequesterResolutionUnavailableError(RuntimeError):
    """resolver 실패·잘못된 principal을 외부 세부정보 없이 감싼다."""


@final
class CreateQuestionRequest(BaseModel):
    """신원 필드를 의도적으로 포함하지 않는 Request-first HTTP body."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    question: str = Field(min_length=1, max_length=16_384)
    session_id: str | None = Field(default=None, max_length=512)
    context_snapshot: str | None = Field(default=None, max_length=65_536)

    @field_validator("question")
    @classmethod
    def _question_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question은 비어 있거나 공백일 수 없습니다.")
        return value

    @field_validator("session_id", "context_snapshot")
    @classmethod
    def _optional_strings_must_be_nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("선택 문자열은 공백일 수 없습니다.")
        return value


class QuestionStreamHttpApplication(Protocol):
    def open_stream(self, command: AskQuestion) -> OpenQuestionStream: ...

    def subscribe(
        self,
        request_id: str,
        principal: QuestionPrincipal,
    ) -> QuestionStreamSubscription: ...

    def lookup(
        self,
        request_id: str,
        principal: QuestionPrincipal,
    ) -> QuestionStreamLookup: ...


PrincipalResolver: TypeAlias = Callable[[Request], object]

_SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")
_LOOKUP_TYPES = (
    AnsweredQuestionLookup,
    PendingQuestionLookup,
    DeclinedQuestionLookup,
    FailedQuestionLookup,
)
_STREAM_TERMINAL_TYPES = (
    DoneEvent,
    DeclinedEvent,
    FailedEvent,
    InterruptedEvent,
)

_NOT_AUTHENTICATED = "인증이 필요합니다."
_AUTH_UNAVAILABLE = "인증 서비스를 사용할 수 없습니다."
_NOT_FOUND = "질문 요청을 찾을 수 없습니다."
_STREAM_UNAVAILABLE = "질문 스트림을 열 수 없습니다."
_REQUEST_UNAVAILABLE = "질문 요청을 처리할 수 없습니다."
_FORBIDDEN = "질문 권한이 없습니다."
_INVALID_WATCH = "watch query가 유효하지 않습니다."
_INVALID_REQUEST = "질문 요청 본문이 유효하지 않습니다."


def _validated_max_polls(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
        raise ValueError("max_polls는 1 이상 10000 이하의 정수여야 합니다.")
    return value


def _validated_poll_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 300
    ):
        raise ValueError("poll_timeout은 0 초과 300 이하의 유한한 수여야 합니다.")
    return float(value)


def _validated_pending_is_end(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("pending_is_end는 bool이어야 합니다.")
    assert isinstance(value, bool)
    return value


def _watch_requested(request: Request) -> bool | JSONResponse:
    """native reconnect의 명시적 단일 watch=true/false만 허용한다."""
    values = request.query_params.getlist("watch")
    if not values or values == ["false"]:
        return False
    if values == ["true"]:
        return True
    return _error_response(400, _INVALID_WATCH)


def _safe_request_id(value: object) -> str | None:
    if not isinstance(value, str) or _SAFE_REQUEST_ID.fullmatch(value) is None:
        return None
    return value


def _resolve_principal(
    resolver: PrincipalResolver,
    request: Request,
) -> QuestionPrincipal:
    try:
        raw = resolver(request)
    except RequesterNotAuthenticatedError:
        raise
    except Exception as error:
        raise _RequesterResolutionUnavailableError() from error
    if type(raw) not in (RequesterPrincipal, AuthenticatedPrincipal):
        raise _RequesterResolutionUnavailableError()
    assert isinstance(raw, (RequesterPrincipal, AuthenticatedPrincipal))
    try:
        principal = type(raw).model_validate(
            raw.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception as error:
        raise _RequesterResolutionUnavailableError() from error
    if any(
        len(value) > 512
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
        for value in (
            principal.org_id,
            principal.subject_id,
            *(
                (principal.identity_provider, principal.identity_session_id)
                if isinstance(principal, AuthenticatedPrincipal)
                else ()
            ),
        )
    ):
        raise _RequesterResolutionUnavailableError()
    return principal


def _error_response(
    status_code: int,
    detail: str,
    *,
    request_id: object | None = None,
) -> JSONResponse:
    safe_id = _safe_request_id(request_id)
    headers = {"X-Request-ID": safe_id} if safe_id is not None else None
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers=headers,
    )


def _canonical_lookup(value: object) -> QuestionStreamLookup:
    if type(value) not in _LOOKUP_TYPES:
        raise TypeError("알 수 없는 Question Stream lookup 투영입니다.")
    assert isinstance(
        value,
        (
            AnsweredQuestionLookup,
            PendingQuestionLookup,
            DeclinedQuestionLookup,
            FailedQuestionLookup,
        ),
    )
    model_type = type(value)
    return model_type.model_validate(
        value.model_dump(mode="python", round_trip=True),
        strict=True,
    )


def _close_subscription(value: object) -> None:
    if not isinstance(value, QuestionStreamSubscription):
        return
    try:
        value.close()
    except Exception:
        # close callback 오류가 이미 시작된 응답에 내부 세부정보를 싣지 않게 한다.
        return


def _close_embedded_subscription(value: object) -> None:
    try:
        embedded: object = getattr(value, "subscription")
    except Exception:
        return
    _close_subscription(embedded)


def _iter_http_question_stream_frames(
    subscription: QuestionStreamSubscription,
    *,
    max_polls: int,
    poll_timeout: float,
    pending_is_end: bool = True,
) -> Generator[str, None, None]:
    """기존 event만 직렬화하고 어떤 실패에서도 HTTP 자체 event를 만들지 않는다."""
    pending_ends_stream = _validated_pending_is_end(pending_is_end)
    try:
        yield QUESTION_STREAM_SSE_PRIMING
        idle_polls = 0
        while idle_polls < max_polls:
            event = subscription.get(timeout=poll_timeout)
            if event is None:
                if subscription.closed:
                    return
                idle_polls += 1
                yield QUESTION_STREAM_SSE_KEEPALIVE
                continue
            idle_polls = 0
            yield serialize_question_stream_sse(event)
            if isinstance(event, _STREAM_TERMINAL_TYPES) or (
                pending_ends_stream and isinstance(event, PendingEvent)
            ):
                return
    finally:
        _close_subscription(subscription)


@final
class _SubscriptionStreamingResponse(StreamingResponse):
    """body iterator가 시작되기 전 ASGI 취소에서도 구독을 회수한다."""

    def __init__(
        self,
        subscription: QuestionStreamSubscription,
        content: ContentStream,
        *,
        headers: Mapping[str, str],
        media_type: str,
    ) -> None:
        self._question_subscription = subscription
        super().__init__(content, headers=headers, media_type=media_type)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            _close_subscription(self._question_subscription)


def build_question_streaming_response(
    subscription: QuestionStreamSubscription,
    *,
    request_id: str,
    max_polls: int,
    poll_timeout: float,
    pending_is_end: bool = True,
) -> StreamingResponse:
    """검증된 P17 구독을 canonical SSE 응답으로 감싸는 사용자 표면 공용 helper."""
    pending_ends_stream = _validated_pending_is_end(pending_is_end)
    return _SubscriptionStreamingResponse(
        subscription,
        _iter_http_question_stream_frames(
            subscription,
            max_polls=max_polls,
            poll_timeout=poll_timeout,
            pending_is_end=pending_ends_stream,
        ),
        media_type="text/event-stream",
        headers={
            "X-Request-ID": request_id,
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


# P17.3b 단위 테스트가 직접 검증하던 private seam은 하위호환으로 보존한다.
# legacy URI adapter는 위 public 이름을 쓰고, canonical router의 생성 실패
# fault-injection은 아래 alias를 교체해 기존 회귀 계약을 그대로 지킨다.
_build_streaming_response = build_question_streaming_response


def _streaming_response_or_error(
    subscription: QuestionStreamSubscription,
    *,
    request_id: object,
    max_polls: int,
    poll_timeout: float,
    pending_is_end: bool = True,
) -> Response:
    safe_id = _safe_request_id(request_id)
    if safe_id is None or subscription.request_id != safe_id:
        _close_subscription(subscription)
        return _error_response(503, _STREAM_UNAVAILABLE)
    try:
        raw_response = cast(
            object,
            _build_streaming_response(
                subscription,
                request_id=safe_id,
                max_polls=max_polls,
                poll_timeout=poll_timeout,
                pending_is_end=pending_is_end,
            ),
        )
    except Exception:
        _close_subscription(subscription)
        return _error_response(503, _STREAM_UNAVAILABLE, request_id=safe_id)
    if not isinstance(raw_response, StreamingResponse):
        _close_subscription(subscription)
        return _error_response(503, _STREAM_UNAVAILABLE, request_id=safe_id)
    return raw_response


def create_question_stream_router(
    *,
    application: QuestionStreamHttpApplication,
    principal_resolver: PrincipalResolver,
    max_polls: object = 80,
    poll_timeout: object = 15.0,
) -> APIRouter:
    """P17-native 세 경로만 가진 router를 만든다.

    P17.2c-2부터 기본 web 앱도 이 router를 포함한다. legacy `/ask*`는 별도 URI adapter지만
    같은 application을 사용하며, 이 router 자체에는 legacy 경로가 들어가지 않는다.
    """
    if not callable(principal_resolver):
        raise TypeError("principal_resolver는 호출 가능해야 합니다.")
    polls = _validated_max_polls(max_polls)
    timeout = _validated_poll_timeout(poll_timeout)
    router = APIRouter()

    def resolve_question_principal(request: Request) -> QuestionPrincipal:
        try:
            return _resolve_principal(principal_resolver, request)
        except RequesterNotAuthenticatedError:
            raise HTTPException(status_code=401, detail=_NOT_AUTHENTICATED) from None
        except _RequesterResolutionUnavailableError:
            raise HTTPException(status_code=503, detail=_AUTH_UNAVAILABLE) from None

    @router.post("/requests", response_model=None)
    async def create_request(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        principal: QuestionPrincipal = Depends(resolve_question_principal),
    ) -> Response:
        try:
            payload = CreateQuestionRequest.model_validate(await request.json(), strict=True)
        except (ValidationError, ValueError, TypeError):
            return _error_response(422, _INVALID_REQUEST)
        try:
            raw_opened: object = application.open_stream(
                AskQuestion(
                    principal=principal,
                    question=payload.question,
                    session_id=payload.session_id,
                    context_snapshot=payload.context_snapshot,
                )
            )
        except QuestionAuthorizationDeniedError:
            return _error_response(403, _FORBIDDEN)
        except QuestionStreamUnavailableError as error:
            return _error_response(
                503,
                _STREAM_UNAVAILABLE,
                request_id=error.request_id,
            )
        except Exception:
            return _error_response(503, _REQUEST_UNAVAILABLE)
        if type(raw_opened) is not OpenQuestionStream:
            _close_embedded_subscription(raw_opened)
            return _error_response(503, _STREAM_UNAVAILABLE)
        assert isinstance(raw_opened, OpenQuestionStream)
        raw_subscription = cast(object, raw_opened.subscription)
        if not isinstance(raw_subscription, QuestionStreamSubscription):
            return _error_response(503, _STREAM_UNAVAILABLE)
        return _streaming_response_or_error(
            raw_subscription,
            request_id=raw_opened.request_id,
            max_polls=polls,
            poll_timeout=timeout,
        )

    @router.get("/requests/{request_id}/stream", response_model=None)
    def reconnect_request_stream(  # pyright: ignore[reportUnusedFunction]
        request_id: str,
        request: Request,
        principal: QuestionPrincipal = Depends(resolve_question_principal),
    ) -> Response:
        safe_id = _safe_request_id(request_id)
        if safe_id is None:
            return _error_response(404, _NOT_FOUND)
        watch = _watch_requested(request)
        if isinstance(watch, JSONResponse):
            return watch
        try:
            raw_subscription = cast(
                object,
                application.subscribe(safe_id, principal),
            )
        except QuestionStreamRequestNotFoundError:
            return _error_response(404, _NOT_FOUND)
        except QuestionStreamUnavailableError as error:
            reflected_id = error.request_id if error.request_id == safe_id else None
            return _error_response(
                503,
                _STREAM_UNAVAILABLE,
                request_id=reflected_id,
            )
        except Exception:
            return _error_response(503, _STREAM_UNAVAILABLE)
        if not isinstance(raw_subscription, QuestionStreamSubscription):
            return _error_response(503, _STREAM_UNAVAILABLE)
        return _streaming_response_or_error(
            raw_subscription,
            request_id=safe_id,
            max_polls=polls,
            poll_timeout=timeout,
            pending_is_end=not watch,
        )

    @router.get("/requests/{request_id}", response_model=None)
    def get_request_result(  # pyright: ignore[reportUnusedFunction]
        request_id: str,
        principal: QuestionPrincipal = Depends(resolve_question_principal),
    ) -> Response:
        safe_id = _safe_request_id(request_id)
        if safe_id is None:
            return _error_response(404, _NOT_FOUND)
        try:
            lookup = _canonical_lookup(application.lookup(safe_id, principal))
        except QuestionStreamRequestNotFoundError:
            return _error_response(404, _NOT_FOUND)
        except Exception:
            return _error_response(503, _REQUEST_UNAVAILABLE)
        if lookup.request_id != safe_id or _safe_request_id(lookup.request_id) is None:
            return _error_response(503, _REQUEST_UNAVAILABLE)
        return JSONResponse(
            status_code=200,
            content=lookup.model_dump(mode="json"),
            headers={"X-Request-ID": safe_id},
        )

    return router
