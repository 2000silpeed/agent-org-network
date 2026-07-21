from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.approval import ApproverPrincipal
from agent_org_network.approval_http import (
    approval_operations_http_error,
    create_approval_router,
)
from agent_org_network.approval_operations import (
    ApprovalMadeUnavailable,
    ApprovalOperationsApplication,
    ApprovalOperationsConflict,
    ApprovalOperationsDependency,
    ApprovalOperationsIntegrityError,
    ApprovalOperationsInvalid,
    ApprovalOperationsNotFoundOrDenied,
    ApprovalReassigned,
    ApproveIntent,
)
from agent_org_network.demo import build_demo
from agent_org_network.demo_question_surfaces import build_demo_question_surface_composition
from agent_org_network.question_resolution import AskQuestion, RequesterPrincipal
from agent_org_network.question_stream_execution import PendingQuestionLookup
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app


def _response(client: TestClient, method: str, path: str, **kwargs: object) -> Response:
    http: Any = client
    return cast(Response, getattr(http, method)(path, **kwargs))


def _approval_app() -> Any:
    return create_app(
        runtime=StubRuntime(),
        session_secret="approval-http-test-secret",
        presence_of=lambda _owner_id: "online",
    )


def _open_pending(client: TestClient) -> tuple[str, str]:
    asked = _response(client, "post", "/ask", json={"question": "환불 기준을 알려 주세요."})
    assert asked.status_code == 200
    payload = asked.json()
    assert payload["type"] == "pending"
    assert payload["state"] == "awaiting_approval"
    request_id = cast(str, payload["request_id"])

    logged_in = _response(client, "post", "/login", json={"user_id": "cs_lead"})
    assert logged_in.status_code == 200
    queued = _response(client, "get", "/inbox/approvals")
    assert queued.status_code == 200
    items = cast(list[dict[str, object]], queued.json())
    assert len(items) == 1
    return request_id, cast(str, items[0]["item_id"])


def test_approval_queue는_세션_principal만_쓰고_본문을_노출하지_않는다() -> None:
    app = _approval_app()

    with TestClient(app) as client:
        unauthenticated = _response(client, "get", "/inbox/approvals")
        request_id, item_id = _open_pending(client)
        queue = _response(client, "get", "/inbox/approvals")
        detail = _response(client, "get", f"/inbox/approvals/{item_id}")

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"] == {
        "code": "approval_not_authenticated",
        "message": "승인 처리함에 로그인해야 합니다.",
        "retryable": False,
    }
    assert queue.json() == [
        {
            "item_id": item_id,
            "request_id": request_id,
            "approval_round": 1,
            "assigned_at": queue.json()[0]["assigned_at"],
            "due_at": queue.json()[0]["due_at"],
        }
    ]
    forbidden = {"question", "candidate", "draft", "approver", "policy", "history"}
    assert forbidden.isdisjoint(queue.json()[0])
    assert detail.status_code == 200
    assert detail.json()["item_id"] == item_id
    assert detail.json()["request_id"] == request_id
    assert detail.json()["question"] == "환불 기준을 알려 주세요."
    assert detail.json()["candidate"]["text"]
    assert "approver_id" not in detail.json()
    assert "policy_version" not in detail.json()


def test_no_auth_mode에서도_exact_approval_path는_legacy_owner_path에_가려지지_않는다() -> None:
    app = create_app(
        runtime=StubRuntime(),
        presence_of=lambda _owner_id: "online",
    )

    with TestClient(app) as client:
        response = _response(client, "get", "/inbox/approvals")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "approval_not_authenticated"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/inbox/approvals/missing-item/decide",
            {"kind": "approve", "principal": {"subject_id": "secret-decider"}},
        ),
        (
            "/inbox/approvals/missing-item/reassign",
            {"approver_id": 7, "actor_id": "secret-reassigner"},
        ),
    ],
)
def test_approval_command는_미인증이면_body_validation보다_먼저_exact_401을_반환한다(
    path: str,
    body: dict[str, object],
) -> None:
    app = _approval_app()

    with TestClient(app) as client:
        response = _response(client, "post", path, json=body)

    assert response.status_code == 401
    assert response.json() == {
        "detail": {
            "code": "approval_not_authenticated",
            "message": "승인 처리함에 로그인해야 합니다.",
            "retryable": False,
        }
    }
    assert "secret-" not in response.text


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/inbox/approvals/missing-item/decide",
            {"kind": "approve", "actor_id": "secret-authenticated-decider"},
        ),
        ("/inbox/approvals/missing-item/decide", {"kind": "approve_with_edit"}),
        (
            "/inbox/approvals/missing-item/reassign",
            {"approver_id": "finance_lead", "principal": "secret-authenticated-principal"},
        ),
        ("/inbox/approvals/missing-item/reassign", {"approver_id": 7}),
    ],
)
def test_approval_command는_인증_principal_해석_후_malformed_body를_422로_거부한다(
    path: str,
    body: dict[str, object],
) -> None:
    app = _approval_app()

    with TestClient(app) as client:
        assert _response(client, "post", "/login", json={"user_id": "cs_lead"}).status_code == 200
        response = _response(client, "post", path, json=body)

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "approval_body_invalid",
            "message": "승인 요청 본문이 유효하지 않습니다.",
            "retryable": False,
        }
    }
    assert "secret-" not in response.text


@pytest.mark.parametrize(
    ("authenticated", "expected_status", "expected_code"),
    [
        (False, 401, "approval_not_authenticated"),
        (True, 422, "approval_body_invalid"),
    ],
)
def test_approval_command의_깨진_json도_principal_먼저_필드_없이_거부한다(
    authenticated: bool,
    expected_status: int,
    expected_code: str,
) -> None:
    app = _approval_app()

    with TestClient(app) as client:
        if authenticated:
            assert (
                _response(client, "post", "/login", json={"user_id": "cs_lead"}).status_code == 200
            )
        response = _response(
            client,
            "post",
            "/inbox/approvals/missing-item/decide",
            content='{"kind":"approve_with_edit","edited_text":"secret-broken"',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    assert "secret-" not in response.text


def test_approval_detail은_현재_지정_승인자가_아니면_404로_숨긴다() -> None:
    app = _approval_app()

    with TestClient(app) as client:
        _request_id, item_id = _open_pending(client)
        assert (
            _response(client, "post", "/login", json={"user_id": "finance_lead"}).status_code == 200
        )
        hidden = _response(client, "get", f"/inbox/approvals/{item_id}")

    assert hidden.status_code == 404
    assert hidden.json()["detail"] == {
        "code": "approval_not_found_or_denied",
        "message": "승인 항목을 찾을 수 없습니다.",
        "retryable": False,
    }


@pytest.mark.parametrize(
    ("body", "expected_action"),
    [
        ({"kind": "approve"}, "approve"),
        (
            {"kind": "approve_with_edit", "edited_text": "수정한 최종 답변"},
            "approve_with_edit",
        ),
        (
            {"kind": "reject", "reason_code": "needs_revision"},
            None,
        ),
    ],
)
def test_approval_decide가_세_처분을_같은_terminal_의미로_반환한다(
    body: dict[str, str],
    expected_action: str | None,
) -> None:
    app = _approval_app()

    with TestClient(app) as client:
        request_id, item_id = _open_pending(client)
        decided = _response(
            client,
            "post",
            f"/inbox/approvals/{item_id}/decide",
            json=body,
        )
        lookup = _response(client, "get", f"/requests/{request_id}")

    assert decided.status_code == 200
    assert decided.json()["item_id"] == item_id
    assert decided.json()["request_id"] == request_id
    assert decided.json()["approval_round"] == 1
    if expected_action is None:
        assert decided.json()["reason_code"] == "needs_revision"
        assert "action" not in decided.json()
        assert lookup.json()["reason_code"] == "needs_revision"
        assert "record_id" not in lookup.json()
    else:
        assert decided.json()["action"] == expected_action
        assert decided.json()["record_id"]
        assert lookup.json()["record_id"] == decided.json()["record_id"]
        assert "reason_code" not in lookup.json()
    assert lookup.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "approve", "actor_id": "cs_lead"},
        {"kind": "approve", "org_id": "demo-org"},
        {"kind": "approve", "principal": {"subject_id": "cs_lead"}},
        {"kind": "approve", "edited_text": "허용되지 않음"},
        {"kind": "approve_with_edit"},
        {"kind": "reject", "reason_code": "x", "by_approver": "cs_lead"},
    ],
)
def test_approval_decide_body는_actor_org_principal과_arm_외_필드를_거부한다(
    body: dict[str, object],
) -> None:
    app = _approval_app()

    with TestClient(app) as client:
        _request_id, item_id = _open_pending(client)
        response = _response(
            client,
            "post",
            f"/inbox/approvals/{item_id}/decide",
            json=body,
        )

    assert response.status_code == 422


def test_approval_reassign은_approver_id만_받고_새_지정자에게_옮긴다() -> None:
    app = _approval_app()

    with TestClient(app) as client:
        request_id, item_id = _open_pending(client)
        invalid = _response(
            client,
            "post",
            f"/inbox/approvals/{item_id}/reassign",
            json={"approver_id": "finance_lead", "actor_id": "cs_lead"},
        )
        unknown_target = _response(
            client,
            "post",
            f"/inbox/approvals/{item_id}/reassign",
            json={"approver_id": "unknown-user"},
        )
        moved = _response(
            client,
            "post",
            f"/inbox/approvals/{item_id}/reassign",
            json={"approver_id": "finance_lead"},
        )
        old_queue = _response(client, "get", "/inbox/approvals")
        assert (
            _response(client, "post", "/login", json={"user_id": "finance_lead"}).status_code == 200
        )
        new_queue = _response(client, "get", "/inbox/approvals")

    assert invalid.status_code == 422
    assert unknown_target.status_code == 404
    assert moved.status_code == 200
    assert moved.json()["predecessor_item_id"] == item_id
    assert moved.json()["request_id"] == request_id
    assert moved.json()["approval_round"] == 2
    assert moved.json()["reason"] == "reassigned"
    assert old_queue.json() == []
    assert new_queue.json()[0]["item_id"] == moved.json()["successor_item_id"]


def test_approval_operations_error_mapping은_고정_본문만_노출한다() -> None:
    cases = (
        (ApprovalOperationsInvalid("secret-invalid"), 400, "approval_invalid", None),
        (
            ApprovalOperationsNotFoundOrDenied("secret-hidden"),
            404,
            "approval_not_found_or_denied",
            None,
        ),
        (ApprovalOperationsConflict("secret-conflict"), 409, "approval_conflict", None),
        (
            ApprovalOperationsDependency("secret-dependency"),
            503,
            "approval_dependency",
            "1",
        ),
        (
            ApprovalOperationsIntegrityError("secret-integrity"),
            500,
            "approval_integrity",
            None,
        ),
    )

    for error, status, code, retry_after in cases:
        response = approval_operations_http_error(error)
        assert response.status_code == status
        assert (response.headers or {}).get("Retry-After") == retry_after
        assert isinstance(response.detail, dict)
        detail = cast(dict[str, object], response.detail)
        assert detail["code"] == code
        assert detail["retryable"] is (status == 503)
        assert "secret-" not in str(detail)


def test_approval_router는_principal_resolver가_callable이_아니면_조립을_거부한다() -> None:
    with pytest.raises(ValueError, match="principal_resolver"):
        create_approval_router(
            application=cast(ApprovalOperationsApplication, object()),
            principal_resolver=cast(Any, None),
        )


def test_demo_default가_만료_fallback과_unavailable을_결정론적으로_배선한다() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    request_ids = iter(("request-expiry",))
    item_ids = iter(("approval-1", "approval-2"))
    composition = build_demo_question_surface_composition(
        build_demo(runtime=StubRuntime()),
        presence_of=lambda _owner_id: "online",
        clock=lambda: now,
        request_id_factory=lambda: next(request_ids),
        draft_id_factory=lambda: "draft-expiry",
        approval_item_id_factory=lambda: next(item_ids),
    )
    try:
        pending = composition.application.ask(
            AskQuestion(
                principal=RequesterPrincipal(org_id="demo-org", subject_id="browser-expiry"),
                question="환불 기준을 알려 주세요.",
            )
        )
        assert isinstance(pending, PendingQuestionLookup)
        first = composition.approval_operations.expire_due(now + timedelta(minutes=31), 10)
        second = composition.approval_operations.expire_due(now + timedelta(minutes=62), 10)

        assert len(first) == 1
        assert isinstance(first[0], ApprovalReassigned)
        assert first[0].predecessor_item_id == "approval-1"
        assert first[0].successor_item_id == "approval-2"
        assert first[0].reason == "expired"
        assert len(second) == 1
        assert isinstance(second[0], ApprovalMadeUnavailable)
        assert second[0].item_id == "approval-2"
        assert second[0].error_code == "approval_unavailable"
    finally:
        composition.close()


def test_demo_default가_사건_journal과_terminal_30일_보존을_배선한다() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    composition = build_demo_question_surface_composition(
        build_demo(runtime=StubRuntime()),
        presence_of=lambda _owner_id: "online",
        clock=lambda: now,
        request_id_factory=lambda: "request-retention",
        record_id_factory=lambda: "record-retention",
        draft_id_factory=lambda: "draft-retention",
        approval_item_id_factory=lambda: "approval-retention",
    )
    try:
        pending = composition.application.ask(
            AskQuestion(
                principal=RequesterPrincipal(org_id="demo-org", subject_id="browser-retention"),
                question="환불 기준을 알려 주세요.",
            )
        )
        assert isinstance(pending, PendingQuestionLookup)
        queue = composition.approval_operations.pending_for(
            ApproverPrincipal(org_id="demo-org", subject_id="cs_lead")
        )
        assert queue[0].item_id == "approval-retention"
        composition.approval_operations.decide(
            "approval-retention",
            ApproverPrincipal(org_id="demo-org", subject_id="cs_lead"),
            ApproveIntent(),
        )

        status = composition.approval_operations.retention_status(
            "approval-retention",
            now + timedelta(days=31),
        )

        assert composition.approval_events is not None
        assert status.kind == "evaluated"
        assert status.retain_until == now + timedelta(days=30)
        assert status.purge_eligible is True
    finally:
        composition.close()
