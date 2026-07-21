"""P17.3b Request-first SSE HTTP adapter 계약.

HTTP는 인증 신원을 명령에 결박하고 구독만 소비한다. 실행 생산자와 canonical
lookup은 application service가 소유하며, 어댑터는 내부 라우팅 값을 노출하지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import Response
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from agent_org_network.central_authority import AuthenticatedPrincipal
from agent_org_network.question_resolution import (
    AskQuestion,
    QuestionAuthorizationDeniedError,
    QuestionAuthorizationUnavailableError,
    QuestionPrincipal,
    RequesterPrincipal,
)
from agent_org_network.question_stream import (
    AcceptedEvent,
    DeclinedEvent,
    InterruptedEvent,
    PendingEvent,
    QuestionStreamEvent,
    QuestionStreamSubscription,
    TokenEvent,
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
from agent_org_network.question_stream_http import (
    CreateQuestionRequest,
    RequesterNotAuthenticatedError,
    _build_streaming_response,  # pyright: ignore[reportPrivateUsage]
    _iter_http_question_stream_frames,  # pyright: ignore[reportPrivateUsage]
    create_question_stream_router,
)


_PRINCIPAL = RequesterPrincipal(org_id="org-1", subject_id="user-1")


class _FakeApplication:
    def __init__(self) -> None:
        self.opened: list[AskQuestion] = []
        self.subscribed: list[tuple[str, QuestionPrincipal]] = []
        self.looked_up: list[tuple[str, QuestionPrincipal]] = []
        self.closed_request_ids: list[str] = []
        self.open_error: Exception | None = None
        self.subscribe_error: Exception | None = None
        self.lookup_error: Exception | None = None
        self.lookup_result: QuestionStreamLookup = PendingQuestionLookup(
            request_id="req-1",
            kind="unowned",
            state="awaiting_manager",
            retryable=False,
            message="담당 결정을 기다리고 있습니다.",
        )
        self.open_events: tuple[object, ...] = (
            AcceptedEvent(request_id="req-1"),
            PendingEvent(
                request_id="req-1",
                kind="unowned",
                state="awaiting_manager",
                retryable=False,
                message="담당 결정을 기다리고 있습니다.",
            ),
        )
        self.open_request_id = "req-1"

    def _subscription(
        self,
        request_id: str,
        events: tuple[object, ...],
    ) -> QuestionStreamSubscription:
        subscription = QuestionStreamSubscription(
            request_id=request_id,
            max_queue_size=max(8, len(events) + 2),
            on_close=lambda _: self.closed_request_ids.append(request_id),
        )
        for event in events:
            assert isinstance(
                event,
                (AcceptedEvent, TokenEvent, PendingEvent, InterruptedEvent),
            )
            subscription.offer(event)
        return subscription

    def open_stream(self, command: AskQuestion) -> OpenQuestionStream:
        self.opened.append(command)
        if self.open_error is not None:
            raise self.open_error
        return OpenQuestionStream(
            request_id=self.open_request_id,
            subscription=self._subscription(self.open_request_id, self.open_events),
        )

    def subscribe(
        self,
        request_id: str,
        principal: QuestionPrincipal,
    ) -> QuestionStreamSubscription:
        self.subscribed.append((request_id, principal))
        if self.subscribe_error is not None:
            raise self.subscribe_error
        return self._subscription(
            request_id,
            (
                PendingEvent(
                    request_id=request_id,
                    kind="unowned",
                    state="awaiting_manager",
                    retryable=False,
                    message="담당 결정을 기다리고 있습니다.",
                ),
            ),
        )

    def lookup(
        self,
        request_id: str,
        principal: QuestionPrincipal,
    ) -> QuestionStreamLookup:
        self.looked_up.append((request_id, principal))
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.lookup_result


def _resolver(_: Request) -> RequesterPrincipal:
    return _PRINCIPAL


def _app(
    application: _FakeApplication,
    *,
    resolver: Callable[[Request], object] = _resolver,
    max_polls: object = 1,
    poll_timeout: object = 0.001,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_question_stream_router(
            application=application,
            principal_resolver=resolver,
            max_polls=max_polls,
            poll_timeout=poll_timeout,
        )
    )
    return app


def _post(client: TestClient, body: dict[str, object]) -> Response:
    http: Any = client
    return cast(Response, http.post("/requests", json=body))


def _get(client: TestClient, path: str) -> Response:
    http: Any = client
    return cast(Response, http.get(path))


def test_router_exposes_only_the_three_request_first_routes() -> None:
    application = _FakeApplication()
    router = create_question_stream_router(
        application=application,
        principal_resolver=_resolver,
        max_polls=1,
        poll_timeout=0.001,
    )

    paths = {getattr(route, "path", None) for route in router.routes}

    assert "/requests" in paths
    assert "/requests/{request_id}/stream" in paths
    assert "/requests/{request_id}" in paths
    assert "/ask/stream" not in paths


def test_post_binds_only_authenticated_principal_and_sets_safe_sse_headers() -> None:
    application = _FakeApplication()
    response = _post(
        TestClient(_app(application)),
        {
            "question": "환불은 언제 되나요?",
            "session_id": "session-1",
            "context_snapshot": "이전 대화",
        },
    )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-1"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"].startswith("text/event-stream")
    assert application.opened == [
        AskQuestion(
            principal=_PRINCIPAL,
            question="환불은 언제 되나요?",
            session_id="session-1",
            context_snapshot="이전 대화",
        )
    ]
    assert "event: accepted" in response.text
    assert "event: pending" in response.text
    assert application.closed_request_ids == ["req-1"]


def test_post_preserves_exact_authenticated_principal_identity_fields() -> None:
    application = _FakeApplication()
    principal = AuthenticatedPrincipal(
        org_id="org-1",
        subject_id="user-1",
        identity_provider="company-oidc",
        identity_session_id="oidc-session-1",
    )

    response = _post(
        TestClient(_app(application, resolver=lambda _: principal)),
        {"question": "질문"},
    )

    assert response.status_code == 200
    assert application.opened[0].principal == principal
    assert type(application.opened[0].principal) is AuthenticatedPrincipal
    assert application.opened[0].principal is not principal


def test_post_maps_central_authorization_deny_to_field_free_403() -> None:
    application = _FakeApplication()
    application.open_error = QuestionAuthorizationDeniedError()

    response = _post(TestClient(_app(application)), {"question": "secret-question"})

    assert response.status_code == 403
    assert response.json() == {"detail": "질문 권한이 없습니다."}
    assert "secret-question" not in response.text
    assert "request" not in response.headers


def test_post_maps_central_authorization_unavailable_to_neutral_503() -> None:
    application = _FakeApplication()
    application.open_error = QuestionAuthorizationUnavailableError()

    response = _post(TestClient(_app(application)), {"question": "secret-question"})

    assert response.status_code == 503
    assert response.json() == {"detail": "질문 요청을 처리할 수 없습니다."}
    assert "secret-question" not in response.text


def test_principal_dependency_runs_before_malformed_json_body_validation() -> None:
    application = _FakeApplication()
    calls = 0

    def resolver(_: Request) -> RequesterPrincipal:
        nonlocal calls
        calls += 1
        return _PRINCIPAL

    client = TestClient(_app(application, resolver=resolver))
    http: Any = client
    response = cast(
        Response,
        http.post(
            "/requests",
            content="{malformed-json",
            headers={"content-type": "application/json"},
        ),
    )

    assert response.status_code == 422
    assert calls == 1
    assert application.opened == []


def test_body_cannot_self_report_org_or_user_and_is_strictly_bounded() -> None:
    application = _FakeApplication()
    client = TestClient(_app(application))

    extra = _post(
        client,
        {
            "question": "질문",
            "org_id": "forged-org",
            "subject_id": "forged-user",
        },
    )
    blank = _post(client, {"question": "   "})
    oversized = _post(client, {"question": "가" * 16_385})

    assert extra.status_code == 422
    assert blank.status_code == 422
    assert oversized.status_code == 422
    assert application.opened == []


def test_known_authentication_failure_is_401_without_calling_application() -> None:
    application = _FakeApplication()

    def unauthenticated(_: Request) -> object:
        raise RequesterNotAuthenticatedError()

    response = _post(
        TestClient(_app(application, resolver=unauthenticated)),
        {"question": "질문"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "인증이 필요합니다."}
    assert application.opened == []


def _missing_principal(_: Request) -> object:
    return None


def _mapping_principal(_: Request) -> object:
    return {"org_id": "org-1", "subject_id": "user-1"}


def _broken_principal(_: Request) -> object:
    raise RuntimeError("secret resolver failure")


@pytest.mark.parametrize(
    "resolver",
    [
        _missing_principal,
        _mapping_principal,
        _broken_principal,
    ],
)
def test_malformed_or_broken_principal_resolver_fails_closed_as_503(
    resolver: Callable[[Request], object],
) -> None:
    application = _FakeApplication()
    response = _post(
        TestClient(_app(application, resolver=resolver)),
        {"question": "질문"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "인증 서비스를 사용할 수 없습니다."}
    assert "secret" not in response.text
    assert application.opened == []


def test_forcibly_corrupted_requester_principal_is_revalidated_exactly() -> None:
    application = _FakeApplication()
    corrupted = RequesterPrincipal(org_id="org-1", subject_id="user-1")
    object.__setattr__(corrupted, "org_id", "")

    response = _post(
        TestClient(_app(application, resolver=lambda _: corrupted)),
        {"question": "질문"},
    )

    assert response.status_code == 503
    assert application.opened == []


@pytest.mark.parametrize(
    "principal",
    [
        RequesterPrincipal(org_id="org-1\r\nx-injected", subject_id="user-1"),
        RequesterPrincipal(org_id="조직", subject_id="사" * 513),
    ],
)
def test_control_or_oversized_principal_fails_closed_before_application(
    principal: RequesterPrincipal,
) -> None:
    application = _FakeApplication()

    response = _post(
        TestClient(_app(application, resolver=lambda _: principal)),
        {"question": "질문"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "인증 서비스를 사용할 수 없습니다."}
    assert "x-injected" not in response.text
    assert application.opened == []


def test_reconnect_subscribes_before_response_and_closes_only_subscription() -> None:
    application = _FakeApplication()
    response = _get(TestClient(_app(application)), "/requests/req-1/stream")

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-1"
    assert application.subscribed == [("req-1", _PRINCIPAL)]
    assert application.closed_request_ids == ["req-1"]
    assert "event: pending" in response.text


def test_reconnect_watch_true_keeps_pending_open_until_bounded_idle_timeout() -> None:
    application = _FakeApplication()
    response = _get(
        TestClient(_app(application, max_polls=1, poll_timeout=0.001)),
        "/requests/req-1/stream?watch=true",
    )

    assert response.status_code == 200
    assert "event: pending" in response.text
    assert ": keep-alive" in response.text
    assert application.subscribed == [("req-1", _PRINCIPAL)]
    assert application.closed_request_ids == ["req-1"]


@pytest.mark.parametrize("query", ["watch=yes", "watch=1", "watch=true&watch=false"])
def test_reconnect_rejects_unsafe_or_ambiguous_watch_query(query: str) -> None:
    application = _FakeApplication()
    response = _get(
        TestClient(_app(application)),
        f"/requests/req-1/stream?{query}",
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "watch query가 유효하지 않습니다."}
    assert application.subscribed == []


def test_missing_and_other_owner_are_the_same_field_free_404() -> None:
    application = _FakeApplication()
    application.subscribe_error = QuestionStreamRequestNotFoundError()
    response = _get(
        TestClient(_app(application)),
        "/requests/private-request/stream",
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "질문 요청을 찾을 수 없습니다."}
    assert "private-request" not in response.text
    assert "x-request-id" not in response.headers


@pytest.mark.parametrize("method", ["open", "subscribe"])
def test_request_saved_but_stream_unavailable_is_503_with_canonical_id(
    method: str,
) -> None:
    application = _FakeApplication()
    error = QuestionStreamUnavailableError("req-1")
    if method == "open":
        application.open_error = error
        response = _post(TestClient(_app(application)), {"question": "질문"})
    else:
        application.subscribe_error = error
        response = _get(TestClient(_app(application)), "/requests/req-1/stream")

    assert response.status_code == 503
    assert response.headers["x-request-id"] == "req-1"
    assert response.json() == {"detail": "질문 스트림을 열 수 없습니다."}


def test_unsafe_generated_request_id_is_never_reflected_into_headers() -> None:
    application = _FakeApplication()
    application.open_request_id = "req-1\r\nx-injected: yes"
    application.open_events = ()

    response = _post(TestClient(_app(application)), {"question": "질문"})

    assert response.status_code == 503
    assert "x-request-id" not in response.headers
    assert "x-injected" not in response.headers
    assert application.closed_request_ids == [application.open_request_id]


def test_malformed_open_result_closes_any_returned_subscription() -> None:
    application = _FakeApplication()
    subscription = application._subscription(  # pyright: ignore[reportPrivateUsage]
        "req-1",
        (),
    )

    class _MalformedOpen:
        def __init__(self) -> None:
            self.subscription = subscription

    def malformed_open(_: AskQuestion) -> OpenQuestionStream:
        return cast(OpenQuestionStream, cast(object, _MalformedOpen()))

    application.open_stream = malformed_open  # type: ignore[method-assign]

    response = _post(TestClient(_app(application)), {"question": "질문"})

    assert response.status_code == 503
    assert "x-request-id" not in response.headers
    assert subscription.closed is True
    assert application.closed_request_ids == ["req-1"]


def test_reconnect_subscription_id_mismatch_closes_without_reflecting_either_id() -> None:
    application = _FakeApplication()

    def mismatched_subscription(
        _: str,
        __: RequesterPrincipal,
    ) -> QuestionStreamSubscription:
        return application._subscription(  # pyright: ignore[reportPrivateUsage]
            "other-request",
            (),
        )

    application.subscribe = mismatched_subscription  # type: ignore[method-assign]

    response = _get(TestClient(_app(application)), "/requests/req-1/stream")

    assert response.status_code == 503
    assert "x-request-id" not in response.headers
    assert application.closed_request_ids == ["other-request"]


def test_path_request_id_with_control_or_non_ascii_is_404_and_never_reflected() -> None:
    application = _FakeApplication()
    client = TestClient(_app(application))

    control = _get(client, "/requests/%0Dbad/stream")
    non_ascii = _get(client, "/requests/%ED%95%9C%EA%B8%80/stream")

    assert control.status_code == 404
    assert non_ascii.status_code == 404
    assert "x-request-id" not in control.headers
    assert "x-request-id" not in non_ascii.headers
    assert application.subscribed == []


def test_many_ready_events_do_not_spend_the_idle_poll_budget_before_end() -> None:
    application = _FakeApplication()
    application.open_events = (
        AcceptedEvent(request_id="req-1"),
        TokenEvent(request_id="req-1", text="하나"),
        TokenEvent(request_id="req-1", text="둘"),
        TokenEvent(request_id="req-1", text="셋"),
        PendingEvent(
            request_id="req-1",
            kind="routed",
            state="awaiting_approval",
            retryable=False,
            message="승인을 기다리고 있습니다.",
        ),
    )

    response = _post(TestClient(_app(application, max_polls=1)), {"question": "질문"})

    assert response.status_code == 200
    assert response.text.count("event: token") == 3
    assert "event: pending" in response.text


def test_idle_poll_exhaustion_emits_only_keepalive_and_closes() -> None:
    application = _FakeApplication()
    application.open_events = (AcceptedEvent(request_id="req-1"),)

    response = _post(TestClient(_app(application, max_polls=1)), {"question": "질문"})

    assert response.status_code == 200
    assert ": keep-alive" in response.text
    assert "event: interrupted" not in response.text
    assert application.closed_request_ids == ["req-1"]


class _ExplodingSubscription(QuestionStreamSubscription):
    def get(self, timeout: float | None = None) -> QuestionStreamEvent | None:
        raise RuntimeError("secret get failure")


class _SequencedSubscription(QuestionStreamSubscription):
    def __init__(self, events: list[QuestionStreamEvent | None]) -> None:
        super().__init__(
            request_id="req-1",
            max_queue_size=8,
            on_close=lambda _: None,
        )
        self._scripted_events = events

    def get(self, timeout: float | None = None) -> QuestionStreamEvent | None:
        if not self._scripted_events:
            return None
        return self._scripted_events.pop(0)


def test_ready_event_resets_consecutive_idle_budget() -> None:
    subscription = _SequencedSubscription(
        [
            None,
            TokenEvent(request_id="req-1", text="답"),
            None,
            PendingEvent(
                request_id="req-1",
                kind="routed",
                state="awaiting_approval",
                retryable=False,
                message="승인을 기다리고 있습니다.",
            ),
        ]
    )

    frames = list(
        _iter_http_question_stream_frames(
            subscription,
            max_polls=2,
            poll_timeout=0.001,
        )
    )

    assert frames.count(": keep-alive\n\n") == 2
    assert any("event: token" in frame for frame in frames)
    assert any("event: pending" in frame for frame in frames)
    assert subscription.closed is True


def test_watch_mode_does_not_treat_pending_as_end_but_still_stops_at_terminal() -> None:
    subscription = _SequencedSubscription(
        [
            PendingEvent(
                request_id="req-1",
                kind="unowned",
                state="awaiting_manager",
                retryable=False,
                message="담당 결정을 기다리고 있습니다.",
            ),
            DeclinedEvent(
                request_id="req-1",
                reason_code="manager_declined",
                message="질문 처리가 거절되었습니다.",
            ),
        ]
    )

    frames = list(
        _iter_http_question_stream_frames(
            subscription,
            max_polls=2,
            poll_timeout=0.001,
            pending_is_end=False,
        )
    )

    assert any("event: pending" in frame for frame in frames)
    assert any("event: declined" in frame for frame in frames)
    assert subscription.closed is True


def test_response_started_get_failure_adds_no_data_event_and_always_closes() -> None:
    application = _FakeApplication()
    subscription = _ExplodingSubscription(
        request_id="req-1",
        max_queue_size=8,
        on_close=lambda _: application.closed_request_ids.append("req-1"),
    )
    frames = _iter_http_question_stream_frames(
        subscription,
        max_polls=1,
        poll_timeout=0.001,
    )

    assert next(frames) == ": connected\n\n"
    with pytest.raises(RuntimeError, match="secret get failure"):
        next(frames)
    assert application.closed_request_ids == ["req-1"]


@pytest.mark.parametrize("failure", ["get", "serialize"])
def test_asgi_stream_failure_closes_without_synthetic_or_internal_message(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _FakeApplication()
    if failure == "get":

        def open_with_broken_subscription(_: AskQuestion) -> OpenQuestionStream:
            subscription = _ExplodingSubscription(
                request_id="req-1",
                max_queue_size=8,
                on_close=lambda _: application.closed_request_ids.append("req-1"),
            )
            return OpenQuestionStream(request_id="req-1", subscription=subscription)

        application.open_stream = open_with_broken_subscription  # type: ignore[method-assign]
    else:

        def fail_serialize(_: QuestionStreamEvent) -> str:
            raise RuntimeError("secret serialize failure")

        monkeypatch.setattr(
            "agent_org_network.question_stream_http.serialize_question_stream_sse",
            fail_serialize,
        )

    response = _post(
        TestClient(_app(application), raise_server_exceptions=False),
        {"question": "질문"},
    )

    assert response.status_code == 200
    assert "event: interrupted" not in response.text
    assert "secret" not in response.text
    assert application.closed_request_ids == ["req-1"]


def test_generator_exit_after_start_closes_subscription() -> None:
    application = _FakeApplication()
    subscription = application._subscription(  # pyright: ignore[reportPrivateUsage]
        "req-1",
        (AcceptedEvent(request_id="req-1"),),
    )
    frames = _iter_http_question_stream_frames(
        subscription,
        max_polls=5,
        poll_timeout=0.01,
    )

    assert next(frames) == ": connected\n\n"
    frames.close()

    assert subscription.closed is True
    assert application.closed_request_ids == ["req-1"]


def test_response_construction_failure_closes_never_consumed_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _FakeApplication()

    def fail_response(*_: object, **__: object) -> object:
        assert application.opened
        raise RuntimeError("header construction failed")

    monkeypatch.setattr(
        "agent_org_network.question_stream_http._build_streaming_response",
        fail_response,
    )
    response = _post(TestClient(_app(application)), {"question": "질문"})

    assert response.status_code == 503
    assert response.headers["x-request-id"] == "req-1"
    assert application.closed_request_ids == ["req-1"]
    assert "header construction" not in response.text


def test_asgi_immediate_disconnect_closes_subscription_before_iterator_start() -> None:
    application = _FakeApplication()
    subscription = application._subscription(  # pyright: ignore[reportPrivateUsage]
        "req-1",
        (),
    )
    response = _build_streaming_response(
        subscription,
        request_id="req-1",
        max_polls=1,
        poll_timeout=0.001,
    )
    scope = cast(Scope, {"type": "http", "asgi": {"spec_version": "2.0"}})

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_: Message) -> None:
        await asyncio.sleep(0)

    asyncio.run(response(scope, receive, send))

    assert subscription.closed is True
    assert application.closed_request_ids == ["req-1"]


def test_asgi_response_start_failure_closes_subscription_before_iterator_start() -> None:
    application = _FakeApplication()
    subscription = application._subscription(  # pyright: ignore[reportPrivateUsage]
        "req-1",
        (),
    )
    response = _build_streaming_response(
        subscription,
        request_id="req-1",
        max_polls=1,
        poll_timeout=0.001,
    )
    scope = cast(Scope, {"type": "http", "asgi": {"spec_version": "2.4"}})

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_: Message) -> None:
        raise OSError("client disconnected before response start")

    with pytest.raises(ClientDisconnect):
        asyncio.run(response(scope, receive, send))

    assert subscription.closed is True
    assert application.closed_request_ids == ["req-1"]


def test_get_answered_is_exact_safe_projection_only() -> None:
    application = _FakeApplication()
    application.lookup_result = AnsweredQuestionLookup(
        answer_text="영업일 3일 안에 처리됩니다.",
        request_id="req-1",
        record_id="record-1",
        mode="full",
        sources=("refund-policy.md",),
        review_status="not_required",
        answered_by="owner-1",
        agent_id="refund-card",
    )

    response = _get(TestClient(_app(application)), "/requests/req-1")

    assert response.status_code == 200
    assert response.json() == {
        "answer_text": "영업일 3일 안에 처리됩니다.",
        "request_id": "req-1",
        "record_id": "record-1",
        "mode": "full",
        "sources": ["refund-policy.md"],
        "review_status": "not_required",
        "answered_by": "owner-1",
        "agent_id": "refund-card",
    }
    leaked = {
        "question",
        "route",
        "confidence",
        "candidates",
        "audit",
        "policy_version",
        "requester_id",
        "org_id",
    }
    assert leaked.isdisjoint(response.json())
    assert application.looked_up == [("req-1", _PRINCIPAL)]


@pytest.mark.parametrize(
    "projection,expected",
    [
        (
            PendingQuestionLookup(
                request_id="req-1",
                kind="routed",
                state="awaiting_approval",
                retryable=False,
                message="승인을 기다리고 있습니다.",
            ),
            {
                "request_id": "req-1",
                "kind": "routed",
                "state": "awaiting_approval",
                "retryable": False,
                "message": "승인을 기다리고 있습니다.",
            },
        ),
        (
            DeclinedQuestionLookup(
                request_id="req-1",
                reason_code="owner_declined",
                message="질문 처리가 거절되었습니다.",
            ),
            {
                "request_id": "req-1",
                "reason_code": "owner_declined",
                "message": "질문 처리가 거절되었습니다.",
            },
        ),
        (
            FailedQuestionLookup(
                request_id="req-1",
                error_code="terminal_failure",
                message="질문을 처리하지 못했습니다.",
            ),
            {
                "request_id": "req-1",
                "error_code": "terminal_failure",
                "message": "질문을 처리하지 못했습니다.",
            },
        ),
    ],
)
def test_get_nonanswered_results_are_sealed_safe_projections(
    projection: QuestionStreamLookup,
    expected: dict[str, object],
) -> None:
    application = _FakeApplication()
    application.lookup_result = projection

    response = _get(TestClient(_app(application)), "/requests/req-1")

    assert response.status_code == 200
    assert response.json() == expected
    assert "question" not in response.json()
    assert "route" not in response.json()


def test_get_missing_or_wrong_owner_is_field_free_404() -> None:
    application = _FakeApplication()
    application.lookup_error = QuestionStreamRequestNotFoundError()

    response = _get(TestClient(_app(application)), "/requests/private-request")

    assert response.status_code == 404
    assert response.json() == {"detail": "질문 요청을 찾을 수 없습니다."}
    assert "private-request" not in response.text
    assert "x-request-id" not in response.headers


@pytest.mark.parametrize(
    ("max_polls", "poll_timeout"),
    [
        (0, 1.0),
        (True, 1.0),
        (1.2, 1.0),
        (1, 0),
        (1, True),
        (1, float("inf")),
        (1, "1"),
    ],
)
def test_poll_configuration_is_strict_and_bounded(
    max_polls: object,
    poll_timeout: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _app(
            _FakeApplication(),
            max_polls=max_polls,
            poll_timeout=poll_timeout,
        )


def test_create_body_model_is_frozen_extra_forbid_and_strict() -> None:
    model = CreateQuestionRequest(question="질문")

    with pytest.raises(Exception):
        model.question = "변조"  # pyright: ignore[reportAttributeAccessIssue]
