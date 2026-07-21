from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent_org_network.approval import (
    ApprovalDraft,
    ApprovalItem,
    ApprovalPendingSummary,
    ApprovalRequired,
    ApprovalSupersession,
    ApproverPrincipal,
    AnswerCandidate,
    InMemoryApprovalStore,
    Reject,
)
from agent_org_network.approval_operations import (
    ApprovalOperationsApplication,
    ApprovalOperationsAuthorizationUnavailable,
    ApprovalOperationsIntegrityError,
    ApprovalOperationsNotFoundOrDenied,
    ApprovalPendingDetail,
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.question_request import (
    AwaitingApproval,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)


T0 = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T1 + timedelta(hours=1)
ROUTE = RouteTarget(
    intent="refund",
    agent_id="refund-owner",
    requires_approval=True,
    authority_version="route-v1",
)


def _stage(
    requests: InMemoryQuestionRequestStore,
    approvals: InMemoryApprovalStore,
    *,
    org_id: str = "org-1",
    request_id: str = "request-1",
    item_id: str = "approval-1",
    approver_id: str = "alice",
    question: str = "환불해 주세요.",
    candidate_text: str = "환불할 수 있습니다.",
    assigned_at: datetime = T0,
    due_at: datetime = T1,
) -> ApprovalItem:
    received = QuestionRequest.receive(
        org_id=org_id,
        requester_id=f"requester:{request_id}",
        question=question,
        request_id_factory=lambda: request_id,
        clock=lambda: assigned_at,
        due_at=due_at,
    )
    requests.create(received)
    trigger_key = f"request-dispatch:{request_id}:1"
    ready = received.record_initial_routing(
        intent=ROUTE.intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=ROUTE,
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=due_at,
            ),
        ),
        clock=lambda: assigned_at,
    )
    assert requests.compare_and_set(request_id, 0, received, ready)
    awaiting = ready.transition(
        AwaitingApproval(
            route=ROUTE,
            attempt=1,
            draft_ref=item_id,
            handling=HandlingAssignment(
                kind="approval_item",
                ref=item_id,
                due_at=due_at,
            ),
        ),
        clock=lambda: assigned_at,
    )
    assert requests.compare_and_set(request_id, 1, ready, awaiting)
    draft = ApprovalDraft(
        draft_id=f"draft:{request_id}",
        request_id=request_id,
        attempt=1,
        route=ROUTE,
        candidate=AnswerCandidate(
            text=candidate_text,
            sources=(f"source:{request_id}.md",),
            mode="full",
            snapshot_sha=f"sha:{request_id}",
        ),
        created_at=assigned_at,
    )
    item = ApprovalItem(
        item_id=item_id,
        org_id=org_id,
        request_id=request_id,
        awaiting_revision=2,
        attempt=1,
        route=ROUTE,
        draft=draft,
        requirement=ApprovalRequired(
            approver_id=approver_id,
            policy_version="approval-v1",
        ),
        created_at=assigned_at,
        due_at=due_at,
    )
    stored, created = approvals.create_or_get(item)
    assert created is True
    assert stored == item
    return item


def _app(
    requests: InMemoryQuestionRequestStore,
    approvals: InMemoryApprovalStore,
) -> ApprovalOperationsApplication:
    return ApprovalOperationsApplication(requests=requests, approvals=approvals)


def _principal(
    *,
    org_id: str = "org-1",
    subject_id: str = "alice",
) -> ApproverPrincipal:
    return ApproverPrincipal(org_id=org_id, subject_id=subject_id)


class _CentralAuthorizer:
    def __init__(
        self,
        *,
        denied_kind: str | None = None,
        verify_result: bool = True,
        grant_action: str | None = None,
    ) -> None:
        self.denied_kind = denied_kind
        self.verify_result = verify_result
        self.grant_action = grant_action
        self.authorize_calls: list[tuple[object, object, object]] = []
        self.verify_calls: list[tuple[object, object, object, object]] = []

    def authorize(self, principal: object, action: object, resource: object) -> object:
        self.authorize_calls.append((principal, action, resource))
        if self.denied_kind is not None:
            return AuthorizationDenied(kind=self.denied_kind)  # type: ignore[arg-type]
        assert type(principal) is AuthenticatedPrincipal
        assert type(resource) is ResourceRef
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=self.grant_action or action,  # type: ignore[arg-type]
            resource=resource,
            roles=("approver",),
            policy_version="policy-v1",
            policy_digest="a" * 64,
        )

    def verify(
        self,
        grant: object,
        principal: object,
        action: object,
        resource: object,
    ) -> bool:
        self.verify_calls.append((grant, principal, action, resource))
        return self.verify_result


def _authenticated(subject_id: str = "alice", org_id: str = "org-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=org_id,
        subject_id=subject_id,
        identity_provider="oidc",
        identity_session_id=f"session:{subject_id}",
    )


def test_central_approval_list_requires_exact_authenticated_principal_before_store_read() -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = InMemoryApprovalStore()
    _stage(requests, approvals)
    authorizer = _CentralAuthorizer()
    app = ApprovalOperationsApplication(
        requests=requests,
        approvals=approvals,
        central_authorizer=authorizer,  # type: ignore[arg-type]
    )

    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        app.pending_for(_principal())

    result = app.pending_for(_authenticated())

    assert [summary.item_id for summary in result] == ["approval-1"]
    assert len(authorizer.authorize_calls) == len(authorizer.verify_calls) == 1
    _, action, resource = authorizer.authorize_calls[0]
    assert action == "approval.list"
    assert resource == ResourceRef(
        org_id="org-1",
        kind="approval_collection",
        owner_subject_id="alice",
    )


@pytest.mark.parametrize(
    ("authorizer", "expected_error"),
    [
        (_CentralAuthorizer(verify_result=False), ApprovalOperationsNotFoundOrDenied),
        (
            _CentralAuthorizer(grant_action="approval.read"),
            ApprovalOperationsNotFoundOrDenied,
        ),
        (
            _CentralAuthorizer(denied_kind="policy_unavailable"),
            ApprovalOperationsAuthorizationUnavailable,
        ),
    ],
)
def test_central_approval_list_rejects_forged_or_unavailable_grants_field_free(
    authorizer: _CentralAuthorizer,
    expected_error: type[Exception],
) -> None:
    app = ApprovalOperationsApplication(
        requests=InMemoryQuestionRequestStore(),
        approvals=InMemoryApprovalStore(),
        central_authorizer=authorizer,  # type: ignore[arg-type]
    )

    with pytest.raises(expected_error) as caught:
        app.pending_for(_authenticated())

    assert caught.value.args == ()
    assert caught.value.__cause__ is None
    assert vars(caught.value) == {}


def test_central_approval_detail_combines_permission_with_current_assignment() -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = InMemoryApprovalStore()
    item = _stage(requests, approvals)
    authorizer = _CentralAuthorizer()
    app = ApprovalOperationsApplication(
        requests=requests,
        approvals=approvals,
        central_authorizer=authorizer,  # type: ignore[arg-type]
    )

    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        app.detail(item.item_id, _authenticated("bob"))

    detail = app.detail(item.item_id, _authenticated())

    assert detail.item_id == item.item_id
    assert len(authorizer.authorize_calls) == 1
    assert authorizer.authorize_calls[0][1:] == (
        "approval.read",
        ResourceRef(
            org_id="org-1",
            kind="approval_item",
            resource_id=item.item_id,
            owner_subject_id="alice",
        ),
    )


def test_approval_item_requires_org_and_assignment_deadline() -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = InMemoryApprovalStore()
    item = _stage(requests, approvals)

    for missing in ("org_id", "due_at"):
        payload = item.model_dump(exclude={missing})
        with pytest.raises(ValidationError):
            ApprovalItem.model_validate(payload, strict=True)

    for due_at in (T0 - timedelta(seconds=1), T1.replace(tzinfo=None)):
        payload = item.model_dump()
        payload["due_at"] = due_at
        with pytest.raises(ValidationError):
            ApprovalItem.model_validate(payload, strict=True)


def test_store_queue_is_org_scoped_body_free_and_deterministic() -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = InMemoryApprovalStore()
    later = _stage(
        requests,
        approvals,
        request_id="request-z",
        item_id="approval-z",
        question="비밀 질문 Z",
        candidate_text="비밀 후보 Z",
        assigned_at=T0 + timedelta(minutes=1),
    )
    earlier = _stage(
        requests,
        approvals,
        request_id="request-a",
        item_id="approval-a",
        question="비밀 질문 A",
        candidate_text="비밀 후보 A",
    )
    _stage(
        requests,
        approvals,
        org_id="org-2",
        request_id="request-other-org",
        item_id="approval-other-org",
        question="다른 조직 비밀 질문",
        candidate_text="다른 조직 비밀 후보",
    )
    _stage(
        requests,
        approvals,
        request_id="request-bob",
        item_id="approval-bob",
        approver_id="bob",
    )

    summaries = approvals.open_for_designated_approver("org-1", "alice")

    assert [summary.item_id for summary in summaries] == [earlier.item_id, later.item_id]
    assert all(type(summary) is ApprovalPendingSummary for summary in summaries)
    assert all(
        set(summary.model_dump())
        == {"item_id", "request_id", "approval_round", "assigned_at", "due_at"}
        for summary in summaries
    )
    serialized = "".join(summary.model_dump_json() for summary in summaries)
    for forbidden in (
        "비밀 질문",
        "비밀 후보",
        "source:",
        "candidate",
        "draft",
        "question",
        "text",
        "sources",
    ):
        assert forbidden not in serialized
    assert approvals.open_for_designated_approver("org-2", "alice")[0].item_id == (
        "approval-other-org"
    )


@pytest.mark.parametrize(("org_id", "approver_id"), [(" ", "alice"), ("org-1", " ")])
def test_store_queue_scope_must_be_nonblank(org_id: str, approver_id: str) -> None:
    with pytest.raises(ValueError):
        InMemoryApprovalStore().open_for_designated_approver(org_id, approver_id)


def test_pending_for_revalidates_links_and_returns_only_safe_summaries() -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = InMemoryApprovalStore()
    item = _stage(requests, approvals)
    app = _app(requests, approvals)

    result = app.pending_for(_principal())

    assert result == [
        ApprovalPendingSummary(
            item_id=item.item_id,
            request_id=item.request_id,
            approval_round=1,
            assigned_at=T0,
            due_at=T1,
        )
    ]
    assert "환불" not in result[0].model_dump_json()


def test_detail_exposes_question_and_draft_only_to_current_designated_principal() -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = InMemoryApprovalStore()
    item = _stage(requests, approvals)

    detail = _app(requests, approvals).detail(item.item_id, _principal())

    assert detail == ApprovalPendingDetail(
        item_id=item.item_id,
        request_id=item.request_id,
        approval_round=1,
        assigned_at=T0,
        due_at=T1,
        question="환불해 주세요.",
        draft_id=item.draft.draft_id,
        candidate=item.draft.candidate,
    )


def test_detail_denials_are_indistinguishable_and_field_free() -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = InMemoryApprovalStore()
    open_item = _stage(requests, approvals)
    other_org = _stage(
        requests,
        approvals,
        org_id="org-2",
        request_id="request-other",
        item_id="approval-other",
    )
    resolved = _stage(
        requests,
        approvals,
        request_id="request-resolved",
        item_id="approval-resolved",
    )
    action = Reject(by_approver="alice", reason_code="unsupported")
    approvals.resolve_if_open(
        resolved.item_id,
        action,
        lambda current: current.resolve(
            action=action,
            approved_candidate=None,
            resolved_at=T0,
        ),
    )
    predecessor = _stage(
        requests,
        approvals,
        request_id="request-old",
        item_id="approval-old",
    )
    successor = ApprovalItem(
        item_id="approval-new",
        org_id=predecessor.org_id,
        request_id=predecessor.request_id,
        awaiting_revision=predecessor.awaiting_revision + 1,
        attempt=predecessor.attempt,
        route=predecessor.route,
        draft=predecessor.draft,
        requirement=ApprovalRequired(
            approver_id="alice",
            policy_version="approval-v2",
        ),
        created_at=T0 + timedelta(minutes=1),
        due_at=T2,
        approval_round=2,
        supersedes_item_id=predecessor.item_id,
    )
    approvals.supersede_and_create_if_open(
        predecessor.item_id,
        ApprovalSupersession(
            reason="reassigned",
            successor_item_id=successor.item_id,
            superseded_at=successor.created_at,
        ),
        successor,
    )
    app = _app(requests, approvals)
    attempts = (
        ("missing", _principal()),
        (open_item.item_id, _principal(org_id="org-2")),
        (open_item.item_id, _principal(subject_id="mallory")),
        (other_org.item_id, _principal()),
        (resolved.item_id, _principal()),
        (predecessor.item_id, _principal()),
    )

    errors: list[ApprovalOperationsNotFoundOrDenied] = []
    for item_id, principal in attempts:
        with pytest.raises(ApprovalOperationsNotFoundOrDenied) as caught:
            app.detail(item_id, principal)
        errors.append(caught.value)

    assert all(type(error) is ApprovalOperationsNotFoundOrDenied for error in errors)
    assert all(error.args == () and error.__dict__ == {} for error in errors)


class _NoExtraPrincipal(ApproverPrincipal):
    pass


@pytest.mark.parametrize("method", ["pending", "detail"])
def test_operations_require_exact_canonical_principal(method: str) -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = InMemoryApprovalStore()
    item = _stage(requests, approvals)
    app = _app(requests, approvals)
    principal = _NoExtraPrincipal(org_id="org-1", subject_id="alice")

    with pytest.raises(ApprovalOperationsIntegrityError) as caught:
        if method == "pending":
            app.pending_for(principal)
        else:
            app.detail(item.item_id, principal)

    assert caught.value.args == ()
    assert caught.value.__dict__ == {}


class _TamperedApprovalStore(InMemoryApprovalStore):
    summary_update: dict[str, object] | None = None
    item_update: dict[str, object] | None = None
    hide_current: bool = False

    def open_for_designated_approver(
        self,
        org_id: str,
        approver_id: str,
    ) -> list[ApprovalPendingSummary]:
        summaries = super().open_for_designated_approver(org_id, approver_id)
        if self.summary_update is None:
            return summaries
        return [summaries[0].model_copy(update=self.summary_update)]

    def get(self, item_id: str) -> ApprovalItem | None:
        item = super().get(item_id)
        if item is None or self.item_update is None:
            return item
        return item.model_copy(update=self.item_update)

    def get_by_request_attempt(
        self,
        request_id: str,
        attempt: int,
    ) -> ApprovalItem | None:
        if self.hide_current:
            return None
        return super().get_by_request_attempt(request_id, attempt)


@pytest.mark.parametrize(
    "update",
    [
        {"item_id": "missing"},
        {"request_id": "other-request"},
        {"approval_round": 9},
        {"assigned_at": T0 + timedelta(seconds=1)},
        {"due_at": T2},
    ],
)
def test_pending_for_rejects_tampered_store_summary(update: dict[str, object]) -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = _TamperedApprovalStore()
    _stage(requests, approvals)
    approvals.summary_update = update

    with pytest.raises(ApprovalOperationsIntegrityError) as caught:
        _app(requests, approvals).pending_for(_principal())

    assert caught.value.args == ()


@pytest.mark.parametrize("update", [{"org_id": "org-2"}, {"due_at": T2}])
def test_pending_for_rejects_tampered_item(update: dict[str, object]) -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = _TamperedApprovalStore()
    _stage(requests, approvals)
    approvals.item_update = update

    with pytest.raises(ApprovalOperationsIntegrityError):
        _app(requests, approvals).pending_for(_principal())


def test_pending_for_rejects_missing_current_generation() -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = _TamperedApprovalStore()
    _stage(requests, approvals)
    approvals.hide_current = True

    with pytest.raises(ApprovalOperationsIntegrityError):
        _app(requests, approvals).pending_for(_principal())


class _TamperedRequestStore(InMemoryQuestionRequestStore):
    tamper: str | None = None

    def get(self, request_id: str) -> QuestionRequest | None:
        request = super().get(request_id)
        if request is None or self.tamper is None:
            return request
        state = request.state
        assert isinstance(state, AwaitingApproval)
        if self.tamper == "org":
            return request.model_copy(update={"org_id": "org-2"})
        if self.tamper == "due":
            handling = state.handling.model_copy(update={"due_at": T2})
            return request.model_copy(
                update={"state": state.model_copy(update={"handling": handling})}
            )
        if self.tamper == "ref":
            handling = state.handling.model_copy(update={"ref": "other-item"})
            forged = state.model_copy(update={"draft_ref": "other-item", "handling": handling})
            return request.model_copy(update={"state": forged})
        raise AssertionError("unknown tamper")


@pytest.mark.parametrize("tamper", ["org", "due", "ref"])
@pytest.mark.parametrize("method", ["pending", "detail"])
def test_operations_reject_tampered_request_links(tamper: str, method: str) -> None:
    requests = _TamperedRequestStore()
    approvals = InMemoryApprovalStore()
    item = _stage(requests, approvals)
    requests.tamper = tamper
    app = _app(requests, approvals)

    with pytest.raises(ApprovalOperationsIntegrityError) as caught:
        if method == "pending":
            app.pending_for(_principal())
        else:
            app.detail(item.item_id, _principal())

    assert caught.value.args == ()


@pytest.mark.parametrize("update", [{"org_id": "org-2"}, {"due_at": T2}])
def test_detail_rejects_item_read_that_differs_from_current(
    update: dict[str, object],
) -> None:
    requests = InMemoryQuestionRequestStore()
    approvals = _TamperedApprovalStore()
    item = _stage(requests, approvals)
    approvals.item_update = update

    with pytest.raises(ApprovalOperationsIntegrityError):
        _app(requests, approvals).detail(item.item_id, _principal())
