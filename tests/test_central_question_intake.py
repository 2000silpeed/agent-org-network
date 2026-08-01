"""RB3.1a Central Question Intake application contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.central_question_intake import (
    CentralQuestionIntakeApplication,
    CentralQuestionIntakeConflict,
    CentralQuestionIntakeForbidden,
    CentralQuestionIntakeInvalid,
    CentralQuestionIntakeNotFound,
    CentralQuestionIntakeUnavailable,
    ReceivedQuestionProjection,
)
from agent_org_network.question_request import (
    InMemoryQuestionRequestStore,
    QuestionRequest,
)
from agent_org_network.sqlite_stores import SqliteQuestionRequestStore


NOW = datetime(2026, 7, 31, 4, 5, tzinfo=UTC)


def _principal(subject_id: str = "user-1", org_id: str = "org-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=org_id,
        subject_id=subject_id,
        identity_provider="company-oidc",
        identity_session_id=f"session-{subject_id}",
    )


class _DeadlinePolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, datetime]] = []

    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        self.calls.append((org_id, state_kind, started_at))
        return started_at + timedelta(minutes=5)


class _Authority:
    def __init__(self) -> None:
        self.allowed = {"question.create", "question.read"}
        self.unavailable: set[str] = set()
        self.raise_on: set[str] = set()
        self.verify_result: bool | object = True
        self.authorized: list[tuple[AuthenticatedPrincipal, object, ResourceRef]] = []
        self.verified: list[tuple[AuthorizationGrant, object, ResourceRef]] = []
        self._issued: set[int] = set()

    def authorize(
        self,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> AuthorizationGrant | AuthorizationDenied:
        self.authorized.append((principal, action, resource))
        if action in self.raise_on:
            raise RuntimeError("internal-authority-detail")
        if action in self.unavailable:
            return AuthorizationDenied(kind="policy_unavailable")
        if action not in self.allowed:
            return AuthorizationDenied(kind="not_found_or_denied")
        grant = AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=cast(object, action),  # type: ignore[arg-type]
            resource=resource,
            roles=("requester",),
            policy_version="v1",
            policy_digest="a" * 64,
        )
        self._issued.add(id(grant))
        return grant

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> bool:
        self.verified.append((grant, action, resource))
        if self.verify_result == "raise":
            raise RuntimeError("internal-verify-detail")
        return bool(
            self.verify_result is True
            and id(grant) in self._issued
            and grant.subject_id == principal.subject_id
            and grant.action == action
            and grant.resource == resource
        )


class _CountingStore:
    workflow_durability = "ephemeral"

    def __init__(self) -> None:
        self.inner = InMemoryQuestionRequestStore()
        self.create_calls = 0

    def create(self, request: QuestionRequest) -> QuestionRequest:
        self.create_calls += 1
        return self.inner.create(request)

    def get(self, request_id: str) -> QuestionRequest | None:
        return self.inner.get(request_id)

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        return self.inner.compare_and_set(request_id, expected_revision, current, updated)

    def nonterminal(self) -> list[QuestionRequest]:
        return self.inner.nonterminal()


def _application(
    *,
    store: object | None = None,
    authority: _Authority | None = None,
    request_id: str = "request-1",
    deadline_policy: _DeadlinePolicy | None = None,
) -> tuple[CentralQuestionIntakeApplication, _CountingStore | object, _Authority]:
    requests = store or _CountingStore()
    central_authority = authority or _Authority()
    application = CentralQuestionIntakeApplication(
        requests=cast(object, requests),  # type: ignore[arg-type]
        central_authorizer=central_authority,
        deadline_policy=deadline_policy or _DeadlinePolicy(),
        request_id_factory=lambda: request_id,
        clock=lambda: NOW,
    )
    return application, requests, central_authority


def test_create_and_read_own_return_only_received_projection() -> None:
    deadline_policy = _DeadlinePolicy()
    app, store, authority = _application(deadline_policy=deadline_policy)

    created = app.create("환불 규정은?", _principal())
    read = app.read_own(created.request_id, _principal())

    assert created == read == ReceivedQuestionProjection(
        request_id="request-1",
        state="received",
        created_at=NOW,
    )
    assert set(created.model_dump()) == {"request_id", "state", "created_at"}
    assert "환불" not in created.model_dump_json()
    assert deadline_policy.calls == [("org-1", "received", NOW)]
    assert isinstance(store, _CountingStore)
    aggregate = store.get("request-1")
    assert aggregate is not None
    assert aggregate.revision == 0
    assert aggregate.created_at == aggregate.updated_at == NOW
    assert aggregate.state.kind == "received"
    assert aggregate.state.handling.due_at == NOW + timedelta(minutes=5)
    assert [call[1] for call in authority.authorized] == [
        "question.create",
        "question.read",
    ]
    assert authority.authorized[1][2] == ResourceRef(
        org_id="org-1",
        kind="question",
        resource_id="request-1",
        owner_subject_id="user-1",
    )


def test_sqlite_store_can_be_reopened_and_read_by_a_new_application(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    first_store = SqliteQuestionRequestStore(database)
    first, _, _ = _application(store=first_store)
    created = first.create("재시작 뒤에도 남나요?", _principal())
    first_store.close()

    reopened_store = SqliteQuestionRequestStore(database)
    restarted, _, _ = _application(store=reopened_store, request_id="unused")
    assert restarted.read_own(created.request_id, _principal()) == created
    reopened_store.close()


@pytest.mark.parametrize("mode", ["denied", "unavailable", "raise", "verify_false", "verify_raise"])
def test_create_authority_failure_writes_nothing(mode: str) -> None:
    authority = _Authority()
    if mode == "denied":
        authority.allowed.remove("question.create")
    elif mode == "unavailable":
        authority.unavailable.add("question.create")
    elif mode == "raise":
        authority.raise_on.add("question.create")
    elif mode == "verify_false":
        authority.verify_result = False
    else:
        authority.verify_result = "raise"
    app, store, _ = _application(authority=authority)
    expected = (
        CentralQuestionIntakeForbidden
        if mode in {"denied", "verify_false"}
        else CentralQuestionIntakeUnavailable
    )

    with pytest.raises(expected):
        app.create("저장되면 안 됩니다", _principal())
    assert isinstance(store, _CountingStore)
    assert store.create_calls == 0
    assert store.get("request-1") is None


def test_unknown_foreign_and_denied_read_are_the_same_fieldless_not_found() -> None:
    app, _, authority = _application()
    created = app.create("소유권 평탄화", _principal())
    errors: list[CentralQuestionIntakeNotFound] = []
    for request_id, principal in (
        ("unknown", _principal()),
        (created.request_id, _principal("user-2")),
        (created.request_id, _principal("user-1", "other-org")),
    ):
        with pytest.raises(CentralQuestionIntakeNotFound) as caught:
            app.read_own(request_id, principal)
        errors.append(caught.value)
    authority.allowed.remove("question.read")
    with pytest.raises(CentralQuestionIntakeNotFound) as caught:
        app.read_own(created.request_id, _principal())
    errors.append(caught.value)
    assert all(error.args == () and not error.__dict__ for error in errors)


def test_read_rechecks_current_authority_and_maps_authority_failure_to_unavailable() -> None:
    app, _, authority = _application()
    created = app.create("권한 회수 확인", _principal())
    authority.unavailable.add("question.read")
    with pytest.raises(CentralQuestionIntakeUnavailable) as caught:
        app.read_own(created.request_id, _principal())
    assert caught.value.args == ()

    authority.unavailable.clear()
    authority.raise_on.add("question.read")
    with pytest.raises(CentralQuestionIntakeUnavailable):
        app.read_own(created.request_id, _principal())


def test_duplicate_request_id_is_a_separate_conflict() -> None:
    app, store, _ = _application(request_id="collision")
    app.create("첫 질문", _principal())
    with pytest.raises(CentralQuestionIntakeConflict) as caught:
        app.create("둘째 질문", _principal())
    assert caught.value.args == ()
    assert isinstance(store, _CountingStore)
    assert store.create_calls == 2
    assert store.get("collision") is not None
    assert store.get("collision").question == "첫 질문"  # type: ignore[union-attr]


@pytest.mark.parametrize("question", ["", "   ", 3, None])
def test_malformed_question_is_rejected_before_authority_or_write(question: object) -> None:
    app, store, authority = _application()
    with pytest.raises(CentralQuestionIntakeInvalid):
        app.create(cast(str, question), _principal())
    assert isinstance(store, _CountingStore)
    assert store.create_calls == 0
    assert authority.authorized == []


def test_malformed_principal_and_request_id_are_rejected_without_leaks() -> None:
    app, store, authority = _application()
    with pytest.raises(CentralQuestionIntakeInvalid) as create_error:
        app.create("질문", cast(AuthenticatedPrincipal, object()))
    with pytest.raises(CentralQuestionIntakeInvalid) as read_error:
        app.read_own("", _principal())
    assert create_error.value.args == read_error.value.args == ()
    assert isinstance(store, _CountingStore)
    assert store.create_calls == 0
    assert authority.authorized == []


def test_projection_is_frozen_strict_and_rejects_leaked_fields() -> None:
    projection = ReceivedQuestionProjection(
        request_id="request-1", state="received", created_at=NOW
    )
    with pytest.raises(ValidationError):
        projection.request_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ReceivedQuestionProjection.model_validate(
            {
                "request_id": "request-1",
                "state": "received",
                "created_at": NOW,
                "question": "raw question leak",
            }
        )
