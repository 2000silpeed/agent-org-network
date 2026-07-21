from __future__ import annotations

import ast
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalAuthorization,
    ApprovalBoundary,
    ApprovalAction,
    ApprovalConfigurationError,
    ApprovalConcurrencyError,
    ApprovalItem,
    ApprovalItemMismatchError,
    ApprovalPending,
    ApprovalPolicyViolationError,
    ApprovalRequired,
    ApprovalRejected,
    ApprovalResolution,
    ApprovalSupersession,
    ApprovalUnauthorizedError,
    Approve,
    ApprovedCandidate,
    ApproveWithEdit,
    ApproverPrincipal,
    AnswerCandidate,
    FinalizationCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
    Reject,
)
from agent_org_network.approval_evidence import (
    ApprovalEvent,
    ApprovalEventRecorder,
    ApprovalEvidenceDependency,
    ApprovalEventJournal,
    ApprovalRequestedEvent,
    InMemoryApprovalEventJournal,
)
from agent_org_network.notify import FakeChannel, Notification, Notifier
from agent_org_network.question_request import (
    AwaitingApproval,
    DeclinedRequest,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.question_stream_execution import (
    BufferedAnswer,
    QuestionStreamExecutionService,
    StableStreamResult,
)
from agent_org_network.runtime import AnswerMode


NOW = datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc)


class _Policy:
    def __init__(self, result: NoApprovalRequired | ApprovalRequired) -> None:
        self.result = result
        self.calls: list[tuple[str, RouteTarget, str]] = []

    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: str,
    ) -> NoApprovalRequired | ApprovalRequired:
        self.calls.append((org_id, route, candidate_mode))
        return self.result


class _Authorizer:
    def __init__(self, grant: ApprovalAuthorization | None = None) -> None:
        self.grant = grant
        self.calls: list[tuple[str, str, str, str, str]] = []

    def authorize(
        self,
        org_id: str,
        designated_approver_id: str,
        actor_id: str,
        action_kind: str,
        policy_version: str,
    ) -> ApprovalAuthorization | None:
        self.calls.append(
            (
                org_id,
                designated_approver_id,
                actor_id,
                action_kind,
                policy_version,
            )
        )
        return self.grant


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        assert state_kind == "awaiting_approval"
        return started_at + timedelta(hours=2)


def _ready_request(
    store: InMemoryQuestionRequestStore,
    *,
    requires_approval: bool,
    request_id: str = "req-1",
) -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="환불해 주세요",
        request_id_factory=lambda: request_id,
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )
    store.create(received)
    route = RouteTarget(
        intent="refund",
        agent_id="refund-owner",
        requires_approval=requires_approval,
        authority_version="route-rules-v1",
    )
    trigger_key = f"request-dispatch:{request_id}:1"
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW,
    )
    assert store.compare_and_set(request_id, 0, received, ready)
    return ready


def _candidate(
    mode: AnswerMode = "full",
    text: str = "환불 가능합니다.",
) -> AnswerCandidate:
    return AnswerCandidate(
        text=text,
        sources=("refund-policy.md",),
        mode=mode,
        snapshot_sha="abc123",
    )


def _route_of(request: QuestionRequest) -> RouteTarget:
    assert isinstance(request.state, ReadyToDispatch)
    return request.state.route


def _principal(
    subject_id: str = "alice",
    org_id: str = "org-1",
) -> ApproverPrincipal:
    return ApproverPrincipal(org_id=org_id, subject_id=subject_id)


def _boundary(
    *,
    requests: InMemoryQuestionRequestStore,
    approvals: InMemoryApprovalStore | None = None,
    policy: _Policy | None,
    authorizer: _Authorizer | None,
    production_style: bool = False,
    clock: Callable[[], datetime] | None = None,
    evidence_recorder: ApprovalEventRecorder | None = None,
    notifier: Notifier | None = None,
) -> tuple[ApprovalBoundary, InMemoryApprovalStore]:
    store = approvals or InMemoryApprovalStore()
    return (
        ApprovalBoundary(
            requests=requests,
            approvals=store,
            policy=policy,
            authorizer=authorizer,
            deadline_policy=_Deadline(),
            draft_id_factory=lambda: "draft-1",
            item_id_factory=lambda: "approval-1",
            clock=clock or (lambda: NOW),
            production_style=production_style,
            evidence_recorder=evidence_recorder,
            notifier=notifier,
        ),
        store,
    )


def test_policy_no_approval_returns_candidate_for_future_finalization_without_transition() -> None:
    requests = InMemoryQuestionRequestStore()
    ready = _ready_request(requests, requires_approval=False)
    policy = _Policy(NoApprovalRequired(policy_version="approval-rules-v1"))
    boundary, approvals = _boundary(
        requests=requests,
        policy=policy,
        authorizer=None,
    )

    result = boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    assert result == FinalizationCandidate(
        request_id="req-1",
        expected_revision=1,
        attempt=1,
        route=_route_of(ready),
        candidate=_candidate(),
        approval_evaluation=NoApprovalRequired(policy_version="approval-rules-v1"),
    )
    assert requests.get("req-1") == ready
    assert approvals.get_by_request_attempt("req-1", 1) is None


@pytest.mark.parametrize(
    ("requires_approval", "mode"),
    [(True, "full"), (False, "draft_only")],
)
def test_required_or_draft_candidate_stages_item_and_awaiting_approval(
    requires_approval: bool,
    mode: AnswerMode,
) -> None:
    requests = InMemoryQuestionRequestStore()
    ready = _ready_request(requests, requires_approval=requires_approval)
    policy = _Policy(
        ApprovalRequired(
            approver_id="legal-approver",
            policy_version="approval-rules-v3",
        )
    )
    boundary, approvals = _boundary(
        requests=requests,
        policy=policy,
        authorizer=_Authorizer(),
    )

    result = boundary.gate_candidate(
        "req-1",
        expected_revision=1,
        candidate=_candidate(mode),
    )

    assert result == ApprovalPending(request_id="req-1")
    item = approvals.get_by_request_attempt("req-1", 1)
    assert item is not None
    assert item.request_id == "req-1"
    assert item.awaiting_revision == 2
    assert item.attempt == 1
    assert item.route == _route_of(ready)
    assert item.draft.draft_id == "draft-1"
    assert item.draft.candidate == _candidate(mode)
    assert item.requirement.approver_id == "legal-approver"
    assert item.requirement.policy_version == "approval-rules-v3"
    stored = requests.get("req-1")
    assert stored is not None
    assert isinstance(stored.state, AwaitingApproval)
    assert stored.state.route == _route_of(ready)
    assert stored.state.attempt == 1
    assert stored.state.draft_ref == "approval-1"
    assert stored.state.handling.ref == "approval-1"


def test_policy_may_require_approval_even_when_route_and_candidate_do_not() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=False)
    boundary, _ = _boundary(
        requests=requests,
        policy=_Policy(
            ApprovalRequired(
                approver_id="risk-approver",
                policy_version="risk-v1",
            )
        ),
        authorizer=_Authorizer(),
    )

    result = boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    assert isinstance(result, ApprovalPending)


@pytest.mark.parametrize("requires_approval,mode", [(True, "full"), (False, "draft_only")])
def test_safety_required_candidate_cannot_be_relaxed_by_policy(
    requires_approval: bool,
    mode: AnswerMode,
) -> None:
    requests = InMemoryQuestionRequestStore()
    ready = _ready_request(requests, requires_approval=requires_approval)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(NoApprovalRequired(policy_version="bad-policy")),
        authorizer=_Authorizer(),
    )

    with pytest.raises(ApprovalPolicyViolationError):
        boundary.gate_candidate(
            "req-1",
            expected_revision=1,
            candidate=_candidate(mode),
        )

    assert requests.get("req-1") == ready
    assert approvals.get_by_request_attempt("req-1", 1) is None


def test_missing_policy_is_fail_closed_before_item_or_request_transition() -> None:
    requests = InMemoryQuestionRequestStore()
    ready = _ready_request(requests, requires_approval=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=None,
        authorizer=_Authorizer(),
    )

    with pytest.raises(ApprovalPolicyViolationError):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    assert requests.get("req-1") == ready
    assert approvals.get_by_request_attempt("req-1", 1) is None


@pytest.mark.parametrize(
    "evaluation",
    [
        NoApprovalRequired.model_construct(policy_version=" "),
        ApprovalRequired.model_construct(approver_id=" ", policy_version=" "),
    ],
)
def test_policy_result_is_canonically_revalidated_before_any_write(
    evaluation: NoApprovalRequired | ApprovalRequired,
) -> None:
    requests = InMemoryQuestionRequestStore()
    ready = _ready_request(requests, requires_approval=False)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(evaluation),
        authorizer=_Authorizer(),
    )

    with pytest.raises(ApprovalPolicyViolationError):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    assert requests.get("req-1") == ready
    assert approvals.get_by_request_attempt("req-1", 1) is None


def test_runtime_candidate_is_canonically_revalidated_before_policy_call() -> None:
    requests = InMemoryQuestionRequestStore()
    ready = _ready_request(requests, requires_approval=False)
    policy = _Policy(NoApprovalRequired(policy_version="policy-v1"))
    boundary, approvals = _boundary(
        requests=requests,
        policy=policy,
        authorizer=_Authorizer(),
    )
    invalid = AnswerCandidate.model_construct(
        text=" ",
        sources=(),
        mode="full",
        snapshot_sha=None,
    )

    with pytest.raises(ApprovalPolicyViolationError):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=invalid)

    assert policy.calls == []
    assert requests.get("req-1") == ready
    assert approvals.get_by_request_attempt("req-1", 1) is None


@pytest.mark.parametrize("missing", ["policy", "authorizer"])
def test_production_composition_rejects_missing_central_approval_dependency(
    missing: str,
) -> None:
    requests = InMemoryQuestionRequestStore()
    policy = _Policy(NoApprovalRequired(policy_version="policy-v1"))
    authorizer = _Authorizer(ApprovalAuthorization(policy_version="policy-v1"))

    with pytest.raises(ApprovalConfigurationError):
        _boundary(
            requests=requests,
            policy=None if missing == "policy" else policy,
            authorizer=None if missing == "authorizer" else authorizer,
            production_style=True,
        )


def test_approve_resolves_item_and_hands_off_approved_candidate_without_finalizing_request() -> (
    None
):
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    authorizer = _Authorizer(ApprovalAuthorization(policy_version="approval-rules-v3"))
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(
            ApprovalRequired(
                approver_id="legal-approver",
                policy_version="approval-rules-v3",
            )
        ),
        authorizer=authorizer,
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate("draft_only"))

    result = boundary.decide(
        "approval-1",
        _principal(),
        Approve(by_approver="alice"),
    )

    item = approvals.get("approval-1")
    assert item is not None
    assert result == ApprovedCandidate(
        request_id="req-1",
        item_id="approval-1",
        expected_revision=2,
        attempt=1,
        route=item.route,
        candidate=_candidate("draft_only"),
        approved_by="alice",
        approved_at=NOW,
        edited=False,
        policy_version="approval-rules-v3",
        assignment_generation=ApprovalAssignmentGeneration.from_item(item),
    )
    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, AwaitingApproval)
    assert item.status == "resolved"
    assert authorizer.calls == [
        ("org-1", "legal-approver", "alice", "approve", "approval-rules-v3")
    ]


def test_approve_with_edit_changes_only_text_and_preserves_sources() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, _ = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    result = boundary.decide(
        "approval-1",
        _principal(),
        ApproveWithEdit(by_approver="alice", edited_text="수수료를 빼고 환불됩니다."),
    )

    assert isinstance(result, ApprovedCandidate)
    assert result.candidate.text == "수수료를 빼고 환불됩니다."
    assert result.candidate.sources == ("refund-policy.md",)
    assert result.candidate.snapshot_sha == "abc123"
    assert result.edited is True


def test_reject_resolves_item_and_declines_question_request() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    result = boundary.decide(
        "approval-1",
        _principal(),
        Reject(by_approver="alice", reason_code="unsupported_claim"),
    )

    assert result == ApprovalRejected(
        request_id="req-1",
        reason_code="unsupported_claim",
    )
    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, DeclinedRequest)
    assert stored.state.reason_code == "unsupported_claim"
    item = approvals.get("approval-1")
    assert item is not None and item.status == "resolved"


def test_missing_or_denied_authorizer_leaves_item_and_request_open() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=None,
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    with pytest.raises(ApprovalUnauthorizedError):
        boundary.decide(
            "approval-1",
            _principal("mallory"),
            Approve(by_approver="mallory"),
        )

    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, AwaitingApproval)
    item = approvals.get("approval-1")
    assert item is not None and item.status == "open"


def test_authorizer_policy_version_mismatch_leaves_item_and_request_open() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="stale-policy")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    with pytest.raises(ApprovalUnauthorizedError):
        boundary.decide(
            "approval-1",
            _principal(),
            Approve(by_approver="alice"),
        )

    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, AwaitingApproval)
    item = approvals.get("approval-1")
    assert item is not None and item.status == "open"


class _PretendsEqualToPolicyVersion:
    def __eq__(self, other: object) -> bool:
        return True


def test_authorization_grant_is_canonically_revalidated_before_resolution() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    invalid_grant = ApprovalAuthorization.model_construct(
        policy_version=_PretendsEqualToPolicyVersion()
    )
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(invalid_grant),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    with pytest.raises(ApprovalUnauthorizedError):
        boundary.decide(
            "approval-1",
            _principal(),
            Approve(by_approver="alice"),
        )

    item = approvals.get("approval-1")
    assert item is not None and item.status == "open"


def test_same_decision_is_idempotent_but_different_decision_conflicts() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, _ = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    action = Approve(by_approver="alice")

    first = boundary.decide("approval-1", _principal(), action)
    second = boundary.decide("approval-1", _principal(), action)

    assert first == second
    with pytest.raises(ApprovalConcurrencyError):
        boundary.decide(
            "approval-1",
            _principal(),
            Reject(by_approver="alice", reason_code="changed_mind"),
        )


def test_concurrent_same_approval_action_converges_to_one_resolution() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    action = Approve(by_approver="alice")

    def decide_same(_: int) -> object:
        return boundary.decide("approval-1", _principal(), action)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(decide_same, range(32)))

    assert all(result == results[0] for result in results)
    assert isinstance(results[0], ApprovedCandidate)
    assert len(approvals.history) == 2


def test_concurrent_different_approval_actions_have_one_explicit_winner() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    actions: tuple[ApprovalAction, ...] = (
        Approve(by_approver="alice"),
        Reject(by_approver="bob", reason_code="unsupported"),
    )

    def decide(action: ApprovalAction) -> object:
        try:
            return boundary.decide(
                "approval-1",
                _principal(action.by_approver),
                action,
            )
        except ApprovalConcurrencyError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, actions))

    assert sum(isinstance(result, ApprovalConcurrencyError) for result in results) == 1
    assert sum(isinstance(result, (ApprovedCandidate, ApprovalRejected)) for result in results) == 1
    assert len(approvals.history) == 2


class _RaiseAfterGateCasStore(InMemoryQuestionRequestStore):
    def __init__(self) -> None:
        super().__init__()
        self._raise_after_gate_cas = True

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        result = super().compare_and_set(
            request_id,
            expected_revision,
            current,
            updated,
        )
        if expected_revision == 1 and self._raise_after_gate_cas:
            self._raise_after_gate_cas = False
            raise RuntimeError("caller lost the committed gate result")
        return result


class _FalseAfterGateCasStore(InMemoryQuestionRequestStore):
    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        result = super().compare_and_set(
            request_id,
            expected_revision,
            current,
            updated,
        )
        if expected_revision == 1 and result:
            return False
        return result


def test_gate_retry_recovers_when_request_cas_committed_before_caller_failure() -> None:
    requests = _RaiseAfterGateCasStore()
    _ready_request(requests, requires_approval=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
    )

    with pytest.raises(RuntimeError, match="lost the committed"):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    assert boundary.gate_candidate(
        "req-1", expected_revision=1, candidate=_candidate()
    ) == ApprovalPending(request_id="req-1")
    assert len(approvals.history) == 1


class _RaiseAfterResolveStore(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self._raise_after_resolve = True

    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem:
        result = super().resolve_if_open(item_id, action, transition)
        if self._raise_after_resolve:
            self._raise_after_resolve = False
            raise RuntimeError("caller lost the committed approval result")
        return result


def test_reject_retry_finishes_request_when_item_resolved_before_caller_failure() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _RaiseAfterResolveStore()
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    action = Reject(by_approver="alice", reason_code="unsupported")

    with pytest.raises(RuntimeError, match="lost the committed"):
        boundary.decide("approval-1", _principal(), action)

    assert boundary.decide("approval-1", _principal(), action) == ApprovalRejected(
        request_id="req-1",
        reason_code="unsupported",
    )
    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, DeclinedRequest)
    assert len(approvals.history) == 2


def test_backward_decision_clock_fails_before_item_resolution_and_can_retry() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    decision_time = [NOW]
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
        clock=lambda: decision_time[0],
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    action = Reject(by_approver="alice", reason_code="unsupported")
    decision_time[0] = NOW - timedelta(hours=1)

    with pytest.raises(ApprovalPolicyViolationError, match="역행"):
        boundary.decide("approval-1", _principal(), action)

    item = approvals.get("approval-1")
    assert item is not None and item.status == "open"
    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, AwaitingApproval)

    decision_time[0] = NOW + timedelta(minutes=1)
    assert boundary.decide("approval-1", _principal(), action) == ApprovalRejected(
        request_id="req-1",
        reason_code="unsupported",
    )


class _AdvanceClockInsideResolveStore(InMemoryApprovalStore):
    def __init__(self, decision_time: list[datetime]) -> None:
        super().__init__()
        self._decision_time = decision_time

    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem:
        def advance_at_linearization(current: ApprovalItem) -> ApprovalItem:
            self._decision_time[0] = current.due_at
            return transition(current)

        return super().resolve_if_open(item_id, action, advance_at_linearization)


def test_open_decision_at_due_is_rejected_inside_store_linearization() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    decision_time = [NOW]
    approvals = _AdvanceClockInsideResolveStore(decision_time)
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
        clock=lambda: decision_time[0],
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    with pytest.raises(ApprovalConcurrencyError, match="기한"):
        boundary.decide(
            "approval-1",
            _principal(),
            Approve(by_approver="alice"),
        )

    item = approvals.get("approval-1")
    request = requests.get("req-1")
    assert item is not None and item.status == "open"
    assert request is not None and isinstance(request.state, AwaitingApproval)
    assert len(approvals.history) == 1


def test_resolved_same_action_retry_still_repairs_after_due() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    decision_time = [NOW]
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
        clock=lambda: decision_time[0],
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    action = Reject(by_approver="alice", reason_code="unsupported")
    first = boundary.decide("approval-1", _principal(), action)
    item = approvals.get("approval-1")
    assert item is not None
    decision_time[0] = item.due_at + timedelta(seconds=1)

    assert boundary.decide("approval-1", _principal(), action) == first
    assert len(approvals.history) == 2


def test_backward_gate_clock_fails_before_approval_item_write() -> None:
    requests = InMemoryQuestionRequestStore()
    ready = _ready_request(requests, requires_approval=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        clock=lambda: NOW - timedelta(minutes=1),
    )

    with pytest.raises(ApprovalPolicyViolationError, match="역행"):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    assert requests.get("req-1") == ready
    assert approvals.get_by_request_attempt("req-1", 1) is None


@pytest.mark.parametrize(
    "candidate_update",
    [
        {"request_id": "other-request"},
        {"item_id": "other-item"},
        {"expected_revision": 999},
        {"attempt": 2},
        {
            "route": RouteTarget(
                intent="other",
                agent_id="other-owner",
                requires_approval=True,
                authority_version="route-rules-v2",
            )
        },
        {"approved_by": "bob"},
        {"policy_version": "other-policy"},
        {"approved_at": NOW - timedelta(minutes=1)},
        {"edited": True},
        {"candidate": _candidate(text="승인하지 않은 다른 본문")},
    ],
)
def test_resolved_approval_item_rejects_cross_linked_or_forged_candidate(
    candidate_update: dict[str, object],
) -> None:
    item = _resolved_approval_item()
    assert item.resolution is not None
    candidate = item.resolution.approved_candidate
    assert candidate is not None
    bad_candidate = candidate.model_copy(update=candidate_update)
    bad_resolution = item.resolution.model_copy(update={"approved_candidate": bad_candidate})

    with pytest.raises(ValueError):
        _rebuild_resolved_item(item, resolution=bad_resolution)


def test_resolved_approval_item_rejects_time_before_creation() -> None:
    item = _resolved_approval_item()
    assert item.resolution is not None
    candidate = item.resolution.approved_candidate
    assert candidate is not None
    before_creation = NOW - timedelta(minutes=1)
    bad_resolution = item.resolution.model_copy(
        update={
            "resolved_at": before_creation,
            "approved_candidate": candidate.model_copy(update={"approved_at": before_creation}),
        }
    )

    with pytest.raises(ValueError, match="빠를"):
        _rebuild_resolved_item(item, resolution=bad_resolution)


def test_resolved_edit_item_preserves_non_text_candidate_evidence() -> None:
    item = _resolved_approval_item(
        ApproveWithEdit(by_approver="alice", edited_text="수정 승인 본문")
    )
    assert item.resolution is not None
    candidate = item.resolution.approved_candidate
    assert candidate is not None
    forged = candidate.model_copy(
        update={
            "candidate": candidate.candidate.model_copy(update={"sources": ("forged-source.md",)})
        }
    )
    bad_resolution = item.resolution.model_copy(update={"approved_candidate": forged})

    with pytest.raises(ValueError, match="본문 외"):
        _rebuild_resolved_item(item, resolution=bad_resolution)


def _resolved_approval_item(
    action: ApprovalAction | None = None,
) -> ApprovalItem:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    boundary.decide(
        "approval-1",
        _principal(),
        action or Approve(by_approver="alice"),
    )
    item = approvals.get("approval-1")
    assert item is not None and item.status == "resolved"
    return item


def _rebuild_resolved_item(
    item: ApprovalItem,
    *,
    resolution: ApprovalResolution,
) -> ApprovalItem:
    return ApprovalItem(
        item_id=item.item_id,
        org_id=item.org_id,
        request_id=item.request_id,
        awaiting_revision=item.awaiting_revision,
        attempt=item.attempt,
        route=item.route,
        draft=item.draft,
        requirement=item.requirement,
        created_at=item.created_at,
        due_at=item.due_at,
        status="resolved",
        resolution=resolution,
    )


def test_in_memory_approval_store_public_objects_do_not_alias_backing_state() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, source_store = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    source = source_store.get("approval-1")
    assert source is not None

    store = InMemoryApprovalStore()
    created, was_created = store.create_or_get(source)
    assert was_created is True
    object.__setattr__(source, "status", "resolved")
    object.__setattr__(created, "status", "resolved")
    from_get = store.get("approval-1")
    from_key = store.get_by_request_attempt("req-1", 1)
    from_history = store.history[0]
    assert from_get is not None and from_get.status == "open"
    assert from_key is not None and from_key.status == "open"
    assert from_history.status == "open"

    object.__setattr__(from_get, "status", "resolved")
    object.__setattr__(from_key, "status", "resolved")
    object.__setattr__(from_history, "status", "resolved")
    assert store.get("approval-1") is not None
    assert store.get("approval-1").status == "open"  # type: ignore[union-attr]
    assert store.history[0].status == "open"

    action = Reject(by_approver="alice", reason_code="unsupported")
    resolved = store.resolve_if_open(
        "approval-1",
        action,
        lambda current: current.resolve(
            action=action,
            approved_candidate=None,
            resolved_at=NOW,
        ),
    )
    object.__setattr__(resolved, "status", "open")
    final = store.get("approval-1")
    assert final is not None and final.status == "resolved"
    assert store.history[-1].status == "resolved"


def test_approval_resolve_transition_cannot_reenter_and_overwrite_winner() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, store = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    outer_action = Reject(by_approver="alice", reason_code="outer")
    inner_action = Reject(by_approver="mallory", reason_code="inner")

    def reentrant_transition(current: ApprovalItem) -> ApprovalItem:
        store.resolve_if_open(
            "approval-1",
            inner_action,
            lambda inner: inner.resolve(
                action=inner_action,
                approved_candidate=None,
                resolved_at=NOW,
            ),
        )
        return current.resolve(
            action=outer_action,
            approved_candidate=None,
            resolved_at=NOW,
        )

    with pytest.raises(ApprovalConcurrencyError, match="재진입"):
        store.resolve_if_open(
            "approval-1",
            outer_action,
            reentrant_transition,
        )

    item = store.get("approval-1")
    assert item is not None and item.status == "open"
    assert len(store.history) == 1


class _CorruptResolvedReadStore(InMemoryApprovalStore):
    corrupt_reads: bool = False

    def get(self, item_id: str) -> ApprovalItem | None:
        item = super().get(item_id)
        if (
            not self.corrupt_reads
            or item is None
            or item.resolution is None
            or item.resolution.approved_candidate is None
        ):
            return item
        forged_candidate = item.resolution.approved_candidate.model_copy(
            update={"request_id": "other-request"}
        )
        forged_resolution = item.resolution.model_copy(
            update={"approved_candidate": forged_candidate}
        )
        return item.model_copy(update={"resolution": forged_resolution})


class _SubstituteResolvedItemStore(InMemoryApprovalStore):
    substitute_reads: bool = False

    def get(self, item_id: str) -> ApprovalItem | None:
        item = super().get(item_id)
        if (
            not self.substitute_reads
            or item is None
            or item.resolution is None
            or item.resolution.approved_candidate is None
        ):
            return item
        substituted_candidate = item.resolution.approved_candidate.model_copy(
            update={"item_id": "approval-other"}
        )
        substituted_resolution = item.resolution.model_copy(
            update={"approved_candidate": substituted_candidate}
        )
        return item.model_copy(
            update={
                "item_id": "approval-other",
                "resolution": substituted_resolution,
            }
        )


class _CorruptRetryLinkStore(InMemoryApprovalStore):
    corrupt_retry_link: bool = False

    def get_by_request_attempt(
        self,
        request_id: str,
        attempt: int,
    ) -> ApprovalItem | None:
        item = super().get_by_request_attempt(request_id, attempt)
        if not self.corrupt_retry_link or item is None:
            return item
        forged_draft = item.draft.model_copy(update={"request_id": "other-request"})
        return item.model_copy(update={"request_id": "other-request", "draft": forged_draft})


class _CorruptResolveReturnStore(InMemoryApprovalStore):
    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem:
        item = super().resolve_if_open(item_id, action, transition)
        assert item.resolution is not None
        candidate = item.resolution.approved_candidate
        assert candidate is not None
        forged_candidate = candidate.model_copy(update={"request_id": "other-request"})
        forged_resolution = item.resolution.model_copy(
            update={"approved_candidate": forged_candidate}
        )
        return item.model_copy(update={"resolution": forged_resolution})


class _CorruptResolveRevisionStore(InMemoryApprovalStore):
    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem:
        item = super().resolve_if_open(item_id, action, transition)
        assert item.resolution is not None
        candidate = item.resolution.approved_candidate
        assert candidate is not None
        forged_candidate = candidate.model_copy(update={"expected_revision": 999})
        forged_resolution = item.resolution.model_copy(
            update={"approved_candidate": forged_candidate}
        )
        return item.model_copy(update={"resolution": forged_resolution})


class _UnpersistedResolveReturnStore(InMemoryApprovalStore):
    def __init__(self, *, at_due: bool) -> None:
        super().__init__()
        self._at_due = at_due

    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem:
        del transition
        current = super().get(item_id)
        assert current is not None
        resolved_at = current.due_at if self._at_due else current.created_at + timedelta(minutes=1)
        return current.model_copy(
            update={
                "status": "resolved",
                "resolution": ApprovalResolution(
                    action=action,
                    approved_candidate=None,
                    resolved_at=resolved_at,
                ),
            }
        )


class _ForgedGenerationReadStore(InMemoryApprovalStore):
    forge_get = False
    resolve_calls = 0

    def get(self, item_id: str) -> ApprovalItem | None:
        item = super().get(item_id)
        if not self.forge_get or item is None:
            return item
        return ApprovalItem.model_validate(
            {
                **item.model_dump(mode="python", round_trip=True),
                "approval_round": 2,
                "supersedes_item_id": "approval-bogus",
            },
            strict=True,
        )

    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem:
        self.resolve_calls += 1
        return super().resolve_if_open(item_id, action, transition)


class _SwapGenerationBeforeResolveStore(InMemoryApprovalStore):
    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem:
        original = super().get(item_id)
        assert original is not None
        swapped = ApprovalItem.model_validate(
            {
                **original.model_dump(mode="python", round_trip=True),
                "approval_round": 2,
                "supersedes_item_id": "approval-bogus",
            },
            strict=True,
        )
        key = (swapped.request_id, swapped.attempt)
        with self._lock:
            self._latest[item_id] = swapped
            self._current_by_request_attempt[key] = swapped
            self._by_request_attempt_round[
                (swapped.request_id, swapped.attempt, swapped.approval_round)
            ] = swapped
        return super().resolve_if_open(item_id, action, transition)


def test_store_returned_model_copy_is_strictly_rehydrated_before_replay() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _CorruptResolvedReadStore()
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    action = Approve(by_approver="alice")
    boundary.decide("approval-1", _principal(), action)
    approvals.corrupt_reads = True

    with pytest.raises(ApprovalItemMismatchError):
        boundary.decide("approval-1", _principal(), action)


def test_resolved_replay_requires_store_key_to_equal_requested_item_id() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _SubstituteResolvedItemStore()
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    action = Approve(by_approver="alice")
    boundary.decide("approval-1", _principal(), action)
    approvals.substitute_reads = True

    with pytest.raises(ApprovalItemMismatchError):
        boundary.decide("approval-1", _principal(), action)


def test_awaiting_approval_retry_requires_exact_request_item_link() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _CorruptRetryLinkStore()
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    approvals.corrupt_retry_link = True

    with pytest.raises(ApprovalItemMismatchError):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())


def test_resolve_return_is_strictly_rehydrated_before_handoff() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _CorruptResolveReturnStore()
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    action = Approve(by_approver="alice")

    with pytest.raises(ApprovalItemMismatchError):
        boundary.decide("approval-1", _principal(), action)

    stored = approvals.get("approval-1")
    assert stored is not None
    assert stored.resolution is not None
    candidate = stored.resolution.approved_candidate
    assert candidate is not None and candidate.request_id == "req-1"


def test_resolve_return_cannot_forge_finalization_expected_revision() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _CorruptResolveRevisionStore()
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    with pytest.raises(ApprovalItemMismatchError):
        boundary.decide(
            "approval-1",
            _principal(),
            Approve(by_approver="alice"),
        )


@pytest.mark.parametrize("at_due", [False, True])
def test_decide_rejects_unpersisted_or_late_resolve_return_before_request_write(
    at_due: bool,
) -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _UnpersistedResolveReturnStore(at_due=at_due)
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    before_request = requests.get("req-1")

    with pytest.raises(ApprovalItemMismatchError):
        boundary.decide(
            "approval-1",
            _principal(),
            Reject(by_approver="alice", reason_code="unsupported"),
        )

    item = approvals.get("approval-1")
    assert item is not None and item.status == "open"
    assert requests.get("req-1") == before_request


def test_decide_rejects_forged_get_generation_before_authority_or_write() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _ForgedGenerationReadStore()
    authorizer = _Authorizer(ApprovalAuthorization(policy_version="policy-v1"))
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=authorizer,
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    before_item = approvals.get_by_request_attempt("req-1", 1)
    before_round = approvals.get_by_request_attempt_round("req-1", 1, 1)
    before_history = approvals.history
    before_request = requests.get("req-1")
    approvals.forge_get = True

    with pytest.raises(ApprovalItemMismatchError):
        boundary.decide(
            "approval-1",
            _principal(),
            Approve(by_approver="alice"),
        )

    assert authorizer.calls == []
    assert approvals.resolve_calls == 0
    assert approvals.get_by_request_attempt("req-1", 1) == before_item
    assert approvals.get_by_request_attempt_round("req-1", 1, 1) == before_round
    assert approvals.history == before_history
    assert requests.get("req-1") == before_request


def test_decide_resolve_callback_rejects_generation_changed_after_prechecks() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _SwapGenerationBeforeResolveStore()
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(ApprovalAuthorization(policy_version="policy-v1")),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    before_request = requests.get("req-1")

    with pytest.raises((ApprovalConcurrencyError, ApprovalItemMismatchError)):
        boundary.decide(
            "approval-1",
            _principal(),
            Approve(by_approver="alice"),
        )

    current = approvals.get_by_request_attempt("req-1", 1)
    assert current is not None
    assert current.approval_round == 2
    assert current.status == "open"
    assert len(approvals.history) == 1
    assert requests.get("req-1") == before_request


@pytest.mark.parametrize(
    "principal,action",
    [
        (
            ApproverPrincipal(org_id="other-org", subject_id="alice"),
            Approve(by_approver="alice"),
        ),
        (
            ApproverPrincipal(org_id="org-1", subject_id="mallory"),
            Approve(by_approver="alice"),
        ),
    ],
)
def test_authenticated_principal_must_match_request_org_and_claimed_approver(
    principal: ApproverPrincipal,
    action: Approve,
) -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    authorizer = _Authorizer(ApprovalAuthorization(policy_version="policy-v1"))
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=authorizer,
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    with pytest.raises(ApprovalUnauthorizedError):
        boundary.decide("approval-1", principal, action)

    assert authorizer.calls == []
    item = approvals.get("approval-1")
    assert item is not None and item.status == "open"


def test_same_request_attempt_with_different_candidate_is_not_silently_reused() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    boundary, _ = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
    )
    boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    with pytest.raises(ApprovalItemMismatchError):
        boundary.gate_candidate(
            "req-1",
            expected_revision=1,
            candidate=_candidate(text="서로 다른 초안"),
        )


def test_approval_module_does_not_import_answer_record_or_legacy_ask_org() -> None:
    path = Path(__file__).parents[1] / "src/agent_org_network/approval.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"answer_record", "ask_org", "worker"}
    imported = {
        node.module.rsplit(".", maxsplit=1)[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported.isdisjoint(forbidden)


def test_requested_evidence_and_push_are_bodyless_and_exactly_once_on_retries() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    journal = InMemoryApprovalEventJournal()
    channel = FakeChannel()
    notifier = Notifier({"approver": channel})
    recorder = ApprovalEventRecorder(journal)
    boundary, _ = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=recorder,
        notifier=notifier,
    )

    first = boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    retry = boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    recovered = boundary.ensure_requested("req-1")

    assert first == retry == recovered == ApprovalPending(request_id="req-1")
    events = journal.for_request("org-1", "req-1")
    assert len(events) == 1
    event = events[0]
    assert type(event) is ApprovalRequestedEvent
    assert event.item_id == "approval-1"
    assert event.draft_id == "draft-1"
    assert event.approval_round == 1
    assert event.occurred_at == NOW
    assert event.policy_version == "policy-v1"
    serialized = event.model_dump(mode="python", round_trip=True)
    assert "text" not in serialized
    assert "candidate" not in serialized
    assert channel.delivered == [
        Notification(
            recipient_id="approver",
            kind="approval_assignment_ready",
            subject_ref="approval-1",
            created_at=NOW,
        )
    ]
    assert boundary.matches_evidence_dependencies(
        evidence_recorder=recorder,
        notifier=notifier,
    )
    assert not boundary.matches_evidence_dependencies(
        evidence_recorder=ApprovalEventRecorder(journal),
        notifier=notifier,
    )


class _SwitchableJournal(ApprovalEventJournal):
    def __init__(self, *, fail_after_append: bool) -> None:
        self.delegate = InMemoryApprovalEventJournal()
        self.fail = True
        self.fail_after_append = fail_after_append

    def append_batch_once(
        self,
        events: tuple[ApprovalEvent, ...],
    ) -> tuple[ApprovalEvent, ...]:
        if self.fail_after_append:
            result = self.delegate.append_batch_once(events)
            if self.fail:
                raise RuntimeError("lost journal response")
            return result
        if self.fail:
            raise RuntimeError("journal unavailable")
        return self.delegate.append_batch_once(events)

    def append_once(self, event: ApprovalEvent) -> ApprovalEvent:
        return self.append_batch_once((event,))[0]

    def get(self, event_id: str) -> ApprovalEvent | None:
        return self.delegate.get(event_id)

    def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]:
        return self.delegate.for_request(org_id, request_id)


def test_requested_evidence_before_append_failure_recovers_without_domain_rerun() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    journal = _SwitchableJournal(fail_after_append=False)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
    )

    with pytest.raises(ApprovalEvidenceDependency) as raised:
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    assert raised.value.retryable is True
    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, AwaitingApproval)
    assert len(approvals.history) == 1
    assert journal.for_request("org-1", "req-1") == ()

    journal.fail = False
    assert boundary.ensure_requested("req-1") == ApprovalPending(request_id="req-1")
    assert len(journal.for_request("org-1", "req-1")) == 1
    assert len(approvals.history) == 1


class _NeverRuntimeSource:
    def __init__(self) -> None:
        self.calls = 0

    def answer(self, request: QuestionRequest) -> BufferedAnswer:
        self.calls += 1
        raise AssertionError("AwaitingApproval recovery must not rerun Runtime")


def test_execute_recovers_requested_evidence_without_runtime_rerun() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    journal = _SwitchableJournal(fail_after_append=False)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
    )
    with pytest.raises(ApprovalEvidenceDependency):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    journal.fail = False
    source = _NeverRuntimeSource()
    service = QuestionStreamExecutionService(
        requests=requests,
        resolution=cast(Any, object()),
        source=source,
        approval=boundary,
        completion=cast(Any, object()),
        reader=cast(Any, object()),
    )

    result = service.execute("req-1")

    assert isinstance(result, StableStreamResult)
    assert isinstance(result.request.state, AwaitingApproval)
    assert source.calls == 0
    assert len(journal.for_request("org-1", "req-1")) == 1
    assert len(approvals.history) == 1


def test_closed_initial_item_recovers_requested_event_without_stale_push() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    journal = _SwitchableJournal(fail_after_append=False)
    channel = FakeChannel()
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
        notifier=Notifier({"approver": channel}),
    )
    with pytest.raises(ApprovalEvidenceDependency):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    action = Reject(by_approver="approver", reason_code="unsupported")
    approvals.resolve_if_open(
        "approval-1",
        action,
        lambda current: current.resolve(
            action=action,
            approved_candidate=None,
            resolved_at=NOW + timedelta(minutes=1),
        ),
    )
    stored_request = requests.get("req-1")
    stored_item = approvals.get("approval-1")
    assert stored_request is not None and isinstance(stored_request.state, AwaitingApproval)
    assert stored_item is not None and stored_item.status == "resolved"
    journal.fail = False

    assert boundary.ensure_requested("req-1") == ApprovalPending(request_id="req-1")

    events = journal.for_request("org-1", "req-1")
    assert len(events) == 1
    assert type(events[0]) is ApprovalRequestedEvent
    assert channel.delivered == []


def test_requested_evidence_append_response_loss_is_repaired_in_same_call() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    journal = _SwitchableJournal(fail_after_append=True)
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
    )

    assert boundary.gate_candidate(
        "req-1", expected_revision=1, candidate=_candidate()
    ) == ApprovalPending(request_id="req-1")
    assert len(journal.for_request("org-1", "req-1")) == 1
    assert len(approvals.history) == 1


def test_request_cas_response_loss_retry_records_requested_once() -> None:
    requests = _RaiseAfterGateCasStore()
    _ready_request(requests, requires_approval=True)
    journal = InMemoryApprovalEventJournal()
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
    )

    with pytest.raises(RuntimeError, match="lost the committed"):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    assert journal.for_request("org-1", "req-1") == ()
    assert boundary.gate_candidate(
        "req-1", expected_revision=1, candidate=_candidate()
    ) == ApprovalPending(request_id="req-1")
    assert len(journal.for_request("org-1", "req-1")) == 1
    assert len(approvals.history) == 1


def test_request_cas_same_winner_false_result_records_requested_once() -> None:
    requests = _FalseAfterGateCasStore()
    _ready_request(requests, requires_approval=True)
    journal = InMemoryApprovalEventJournal()
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
    )

    assert boundary.gate_candidate(
        "req-1", expected_revision=1, candidate=_candidate()
    ) == ApprovalPending(request_id="req-1")
    assert len(journal.for_request("org-1", "req-1")) == 1
    assert len(approvals.history) == 1


class _HostileGenerationsStore(InMemoryApprovalStore):
    corrupt = False

    def generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        result = super().generations(request_id, attempt)
        return [] if self.corrupt else result


def test_hostile_generation_index_fails_before_requested_event() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    approvals = _HostileGenerationsStore()
    approvals.corrupt = True
    journal = InMemoryApprovalEventJournal()
    boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
    )

    with pytest.raises(ApprovalItemMismatchError):
        boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())

    assert journal.for_request("org-1", "req-1") == ()
    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, AwaitingApproval)


class _ExplodingNotifier(Notifier):
    def notify(self, notification: Notification) -> None:
        raise RuntimeError("push unavailable")


def test_notification_failure_never_rolls_back_pull_assignment_or_evidence() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    journal = InMemoryApprovalEventJournal()
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
        notifier=_ExplodingNotifier(),
    )

    assert boundary.gate_candidate(
        "req-1", expected_revision=1, candidate=_candidate()
    ) == ApprovalPending(request_id="req-1")
    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, AwaitingApproval)
    assert approvals.open_for_designated_approver("org-1", "approver")[0].item_id == "approval-1"
    assert len(journal.for_request("org-1", "req-1")) == 1


def test_successor_current_recovers_round_one_requested_without_stale_push() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    initial_boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver-1", policy_version="policy-v1")),
        authorizer=_Authorizer(),
    )
    initial_boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
    predecessor = approvals.get("approval-1")
    request = requests.get("req-1")
    assert predecessor is not None and request is not None
    successor_at = NOW + timedelta(minutes=10)
    supersession = ApprovalSupersession(
        successor_item_id="approval-2",
        reason="reassigned",
        superseded_at=successor_at,
        actor_id="manager-1",
        policy_version="policy-v2",
        authority_version="authority-v2",
        evidence_ref="manual-command-1",
        target_approver_id="approver-2",
    )
    successor = ApprovalItem(
        item_id="approval-2",
        org_id=predecessor.org_id,
        request_id=predecessor.request_id,
        awaiting_revision=predecessor.awaiting_revision + 1,
        attempt=predecessor.attempt,
        route=predecessor.route,
        draft=predecessor.draft,
        requirement=ApprovalRequired(
            approver_id="approver-2",
            policy_version="policy-v2",
        ),
        created_at=successor_at,
        due_at=successor_at + timedelta(hours=2),
        approval_round=2,
        supersedes_item_id=predecessor.item_id,
    )
    stored_successor, created = approvals.supersede_and_create_if_open(
        predecessor.item_id,
        supersession,
        successor,
        expected_generation=ApprovalAssignmentGeneration.from_item(predecessor),
    )
    assert created and stored_successor == successor
    updated = request.reassign_approval(
        previous_item_id=predecessor.item_id,
        successor_item_id=successor.item_id,
        due_at=successor.due_at,
        clock=lambda: successor_at,
    )
    assert requests.compare_and_set(request.request_id, request.revision, request, updated)

    journal = InMemoryApprovalEventJournal()
    old_channel = FakeChannel()
    new_channel = FakeChannel()
    recovery_boundary, _ = _boundary(
        requests=requests,
        approvals=approvals,
        policy=_Policy(ApprovalRequired(approver_id="unused", policy_version="unused")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
        notifier=Notifier({"approver-1": old_channel, "approver-2": new_channel}),
    )

    assert recovery_boundary.ensure_requested("req-1") == ApprovalPending(request_id="req-1")
    events = journal.for_request("org-1", "req-1")
    assert len(events) == 1
    assert events[0].item_id == predecessor.item_id
    assert events[0].approval_round == 1
    assert old_channel.delivered == []
    assert new_channel.delivered == []


def test_32_way_same_gate_records_and_notifies_once() -> None:
    requests = InMemoryQuestionRequestStore()
    _ready_request(requests, requires_approval=True)
    journal = InMemoryApprovalEventJournal()
    channel = FakeChannel()
    boundary, approvals = _boundary(
        requests=requests,
        policy=_Policy(ApprovalRequired(approver_id="approver", policy_version="policy-v1")),
        authorizer=_Authorizer(),
        evidence_recorder=ApprovalEventRecorder(journal),
        notifier=Notifier({"approver": channel}),
    )

    def gate(_: int) -> ApprovalPending:
        result = boundary.gate_candidate("req-1", expected_revision=1, candidate=_candidate())
        assert isinstance(result, ApprovalPending)
        return result

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(gate, range(32)))

    assert results == [ApprovalPending(request_id="req-1")] * 32
    assert len(journal.for_request("org-1", "req-1")) == 1
    assert len(channel.delivered) == 1
    assert len(approvals.history) == 1
