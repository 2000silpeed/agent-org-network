from __future__ import annotations

import json
from typing import Any, Literal, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.ask_org import AskOrg
from agent_org_network.demo import build_demo
from agent_org_network.demo_question_surfaces import build_demo_question_surface_composition
from agent_org_network.question_stream_execution import (
    AnsweredQuestionLookup,
    DeclinedQuestionLookup,
    FailedQuestionLookup,
    PendingQuestionLookup,
    QuestionSurfaceInterruptedError,
)
from agent_org_network.question_resolution import QuestionAuthorizationDeniedError, RequestNotFound
from agent_org_network.runtime import StubRuntime
from agent_org_network.session import InMemorySessionStore
from agent_org_network.web import create_app, serialize_legacy_question_lookup


def _post(
    client: TestClient,
    path: str,
    question: str,
    *,
    cookies: dict[str, str] | None = None,
) -> Response:
    http: Any = client
    return cast(
        Response,
        http.post(path, json={"question": question}, cookies=cookies),
    )


def _get(
    client: TestClient,
    path: str,
    *,
    cookies: dict[str, str] | None = None,
) -> Response:
    http: Any = client
    return cast(Response, http.get(path, cookies=cookies))


def _sse_events(response: Response) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for frame in response.text.split("\n\n"):
        lines = [line for line in frame.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        raw = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        payload = json.loads(raw)
        assert isinstance(payload, dict)
        events.append((event, cast(dict[str, object], payload)))
    return events


def _cookie_value(response: Response) -> str:
    header = response.headers.get("set-cookie", "")
    pair = next(part.strip() for part in header.split(";") if part.strip().startswith("aon_uid="))
    return pair.split("=", 1)[1]


def test_legacy_lookup_projection_is_pure_and_exposes_only_user_fields() -> None:
    answered = serialize_legacy_question_lookup(
        AnsweredQuestionLookup(
            answer_text="환불 답변",
            request_id="request-1",
            record_id="record-1",
            mode="full",
            sources=("refund.md",),
            review_status="not_required",
            answered_by="owner-1",
            agent_id="refund-card",
        )
    )
    declined = serialize_legacy_question_lookup(
        DeclinedQuestionLookup(
            request_id="request-2",
            reason_code="owner_declined",
            message="질문 처리가 거절되었습니다.",
        )
    )
    failed = serialize_legacy_question_lookup(
        FailedQuestionLookup(
            request_id="request-3",
            error_code="runtime_exhausted",
            message="질문을 처리하지 못했습니다.",
        )
    )

    assert answered == {
        "type": "answered",
        "request_id": "request-1",
        "record_id": "record-1",
        "text": "환불 답변",
        "answered_by": {"owner": "owner-1", "agent_id": "refund-card"},
        "mode": "full",
        "sources": ["refund.md"],
        "review_status": "not_required",
    }
    assert declined == {
        "type": "declined",
        "request_id": "request-2",
        "reason_code": "owner_declined",
        "message": "질문 처리가 거절되었습니다.",
    }
    assert failed == {
        "type": "failed",
        "request_id": "request-3",
        "error_code": "runtime_exhausted",
        "message": "질문을 처리하지 못했습니다.",
    }


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("routing", "dispatched"),
        ("routed", "dispatched"),
        ("contested", "contested"),
        ("unowned", "unowned"),
    ],
)
def test_legacy_pending_projection_uses_request_id_as_identity_alias(
    kind: Literal["routing", "routed", "contested", "unowned"],
    expected: Literal["dispatched", "contested", "unowned"],
) -> None:
    result = serialize_legacy_question_lookup(
        PendingQuestionLookup(
            request_id="request-1",
            kind=kind,
            state="received" if kind == "routing" else "awaiting_manager",
            retryable=kind == "routing",
            message="질문을 처리하고 있습니다.",
        )
    )

    assert result == {
        "type": "pending",
        "request_id": "request-1",
        "kind": expected,
        "state": "received" if kind == "routing" else "awaiting_manager",
        "retryable": kind == "routing",
        "message": "질문을 처리하고 있습니다.",
        "tracking": "request-1",
    }


def test_legacy_blocking_and_identity_alias_return_same_finalization() -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        blocking = _post(client, "/ask", "환불은 언제 되나요?")
        body = blocking.json()
        request_id = body["request_id"]

        retrieved = _get(client, f"/ask/{request_id}")

    assert blocking.status_code == 200
    assert blocking.headers["x-request-id"] == request_id
    assert body["type"] == "answered"
    assert retrieved.status_code == 200
    assert retrieved.headers["x-request-id"] == request_id
    assert retrieved.json() == body


def test_legacy_sse_uses_p17_events_and_identity_alias_returns_same_record() -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        streamed = _post(client, "/ask/stream", "환불은 언제 되나요?")
        events = _sse_events(streamed)
        request_id = streamed.headers["x-request-id"]
        done = next(payload for event, payload in events if event == "done")

        retrieved = _get(client, f"/ask/{request_id}")

    assert streamed.status_code == 200
    assert events[0][0] == "accepted"
    assert all(event != "meta" for event, _ in events)
    assert done["request_id"] == request_id
    assert retrieved.status_code == 200
    assert retrieved.json()["request_id"] == request_id
    assert retrieved.json()["record_id"] == done["record_id"]


@pytest.mark.parametrize(
    ("question", "kind", "state"),
    [
        ("평가 기준을 알려 주세요.", "dispatched", "awaiting_approval"),
        ("보상 기준은 무엇인가요?", "contested", "awaiting_conflict"),
        ("주차 등록은 어떻게 하나요?", "unowned", "awaiting_manager"),
    ],
)
def test_legacy_blocking_pending_tracking_is_request_id(
    question: str,
    kind: str,
    state: str,
) -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        response = _post(client, "/ask", question)
        body = response.json()

    assert body["type"] == "pending"
    assert body["kind"] == kind
    assert body["state"] == state
    assert body["tracking"] == body["request_id"]
    assert "text" not in body
    assert "record_id" not in body


def test_legacy_identity_alias_hides_request_from_other_cookie() -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        created = _post(client, "/ask", "환불은 언제 되나요?")
        request_id = created.json()["request_id"]
        hidden = _get(
            client,
            f"/ask/{request_id}",
            cookies={"aon_uid": "other-browser-00002"},
        )

    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "질문 요청을 찾을 수 없습니다."}


def test_legacy_blocking_maps_surface_interruption_to_neutral_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = build_demo_question_surface_composition(build_demo(runtime=StubRuntime()))

    def interrupted(command: object) -> object:
        del command
        raise QuestionSurfaceInterruptedError(
            request_id="request-interrupted",
            code="router_unavailable",
            retryable=True,
        )

    monkeypatch.setattr(composition.application, "ask", interrupted)
    app = create_app(
        runtime=StubRuntime(),
        question_surface_composition=composition,
    )

    with TestClient(app) as client:
        response = _post(client, "/ask", "환불은 언제 되나요?")

    assert response.status_code == 503
    assert response.headers["x-request-id"] == "request-interrupted"
    assert response.json() == {"detail": "질문 요청을 처리할 수 없습니다."}


@pytest.mark.parametrize("path", ["/ask", "/ask/stream"])
def test_legacy_create_alias는_central_deny를_field_free_403으로_보존한다(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    composition = build_demo_question_surface_composition(build_demo(runtime=StubRuntime()))

    def denied(command: object, **_: object) -> object:
        del command
        raise QuestionAuthorizationDeniedError()

    if path == "/ask":
        monkeypatch.setattr(composition.application, "ask", denied)
    else:
        monkeypatch.setattr(composition.application, "open_stream", denied)
    app = create_app(runtime=StubRuntime(), question_surface_composition=composition)

    with TestClient(app) as client:
        response = _post(client, path, "secret-question")

    assert response.status_code == 403
    assert response.json() == {"detail": "질문 권한이 없습니다."}
    assert "secret-question" not in response.text


def test_legacy_lookup_alias는_application_read_deny를_기존_404로_숨긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = build_demo_question_surface_composition(build_demo(runtime=StubRuntime()))
    actions: list[str] = []

    def denied_retrieve(
        request_id: str, principal: object, *, action: str = "question.read"
    ) -> object:
        del request_id, principal
        actions.append(action)
        return RequestNotFound()

    monkeypatch.setattr(
        composition.application._resolution,  # pyright: ignore[reportPrivateUsage]
        "retrieve",
        denied_retrieve,
    )
    app = create_app(runtime=StubRuntime(), question_surface_composition=composition)

    with TestClient(app) as client:
        response = _get(client, "/ask/secret-request")

    assert response.status_code == 404
    assert response.json() == {"detail": "질문 요청을 찾을 수 없습니다."}
    assert "secret-request" not in response.text
    assert actions == ["question.read"]
    assert "router" not in response.text.lower()


def test_legacy_user_routes_never_call_ask_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("legacy URI adapter가 AskOrg를 호출했습니다.")

    monkeypatch.setattr(AskOrg, "handle", forbidden)
    monkeypatch.setattr(AskOrg, "handle_stream", forbidden)
    monkeypatch.setattr(AskOrg, "retrieve", forbidden)
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        blocking = _post(client, "/ask", "환불은 언제 되나요?")
        streamed = _post(client, "/ask/stream", "환불은 언제 되나요?")
        retrieved = _get(client, f"/ask/{blocking.json()['request_id']}")

    assert blocking.status_code == 200
    assert streamed.status_code == 200
    assert retrieved.status_code == 200


def test_legacy_user_routes_do_not_write_legacy_session_store() -> None:
    sessions = InMemorySessionStore()
    app = create_app(runtime=StubRuntime(), session_store=sessions)

    with TestClient(app) as client:
        blocking = _post(client, "/ask", "환불은 언제 되나요?")
        subject_id = _cookie_value(blocking)
        streamed = _post(client, "/ask/stream", "환불은 언제 되나요?")

    assert blocking.status_code == 200
    assert streamed.status_code == 200
    assert sessions.active_for_user(subject_id) is None
