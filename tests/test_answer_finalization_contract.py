from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    CompletionAttributionError,
    CompletionClockError,
    CompletionEvidenceError,
    DirectAnsweredTransitionError,
    InMemoryQuestionCompletionUnitOfWork,
    InvalidCompletionHandoffError,
    ResponsibilitySnapshotResolver,
)
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalBoundary,
    ApprovalRequired,
    ApprovedCandidate,
    AnswerCandidate,
    FinalizationCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)


NOW = datetime(2026, 7, 12, 13, 0, tzinfo=timezone.utc)


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


class _Resolver:
    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot | None:
        if org_id != "org-1":
            return None
        return AnswerResponsibilitySnapshot(
            agent_id=route.agent_id,
            owner_id="owner-1",
        )


def _uow(
    *,
    policy: _Policy | None = None,
    resolver: ResponsibilitySnapshotResolver | None = None,
    clock: Callable[[], datetime] | None = None,
) -> InMemoryQuestionCompletionUnitOfWork:
    return InMemoryQuestionCompletionUnitOfWork(
        policy=policy or _Policy(NoApprovalRequired(policy_version="approval-v1")),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=resolver or _Resolver(),
        record_id_factory=lambda: "record-1",
        clock=clock or (lambda: NOW),
    )


def _ready(
    store: InMemoryQuestionCompletionUnitOfWork,
    *,
    requires_approval: bool = False,
) -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="환불은 언제 처리되나요?",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id=None,
    )
    store.create(received)
    route = RouteTarget(
        intent="refund",
        agent_id="refund-owner",
        requires_approval=requires_approval,
        authority_version="route-v1",
    )
    trigger = "request-dispatch:req-1:1"
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
            attempt=1,
            trigger_key=trigger,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert store.compare_and_set("req-1", 0, received, ready)
    return ready


def _handoff(request: QuestionRequest) -> FinalizationCandidate:
    assert isinstance(request.state, ReadyToDispatch)
    return FinalizationCandidate(
        request_id=request.request_id,
        expected_revision=request.revision,
        attempt=request.state.attempt,
        route=request.state.route,
        candidate=AnswerCandidate(
            text="영업일 기준 3일 안에 처리됩니다.",
            sources=("refund-policy.md",),
            mode="full",
            snapshot_sha="sha-1",
        ),
        approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
    )


@pytest.mark.parametrize(
    "invalid",
    [
        AnswerCandidate(text="답", mode="full"),
        {"request_id": "req-1"},
        object(),
    ],
)
def test_only_sealed_completion_handoff_is_accepted(invalid: object) -> None:
    uow = _uow()
    ready = _ready(uow)

    with pytest.raises(InvalidCompletionHandoffError):
        uow.complete(invalid)

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None


def test_public_request_cas_cannot_bypass_completion_bundle() -> None:
    uow = _uow()
    ready = _ready(uow)
    answered = ready.transition(
        AnsweredRequest(record_id="forged-record"),
        clock=lambda: NOW,
    )

    with pytest.raises(DirectAnsweredTransitionError):
        uow.compare_and_set("req-1", 1, ready, answered)

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None


@pytest.mark.parametrize("mutation", ["revision", "route", "attempt"])
def test_handoff_must_match_current_request_snapshot(mutation: str) -> None:
    uow = _uow()
    ready = _ready(uow)
    handoff = _handoff(ready)
    if mutation == "revision":
        handoff = handoff.model_copy(update={"expected_revision": 0})
    elif mutation == "attempt":
        handoff = handoff.model_copy(update={"attempt": 2})
    else:
        handoff = handoff.model_copy(
            update={
                "route": RouteTarget(
                    intent="refund",
                    agent_id="other-owner",
                    requires_approval=False,
                    authority_version="route-v1",
                )
            }
        )

    with pytest.raises(CompletionEvidenceError):
        uow.complete(handoff)

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None


@pytest.mark.parametrize("reason", ["route", "draft", "policy_changed"])
def test_no_approval_handoff_cannot_bypass_current_approval_requirement(
    reason: str,
) -> None:
    policy = _Policy(NoApprovalRequired(policy_version="approval-v1"))
    uow = _uow(policy=policy)
    ready = _ready(uow, requires_approval=reason == "route")
    handoff = _handoff(ready)
    if reason == "draft":
        handoff = handoff.model_copy(
            update={"candidate": handoff.candidate.model_copy(update={"mode": "draft_only"})}
        )
    if reason == "policy_changed":
        policy.result = ApprovalRequired(
            approver_id="legal",
            policy_version="approval-v2",
        )

    with pytest.raises(CompletionEvidenceError):
        uow.complete(handoff)

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None


def test_model_construct_policy_output_is_revalidated_before_commit() -> None:
    policy = _Policy(NoApprovalRequired.model_construct(policy_version=" "))
    uow = _uow(policy=policy)
    ready = _ready(uow)

    with pytest.raises(CompletionEvidenceError):
        uow.complete(_handoff(ready))

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None


class _WrongResolver:
    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot | None:
        return AnswerResponsibilitySnapshot(
            agent_id="different-card",
            owner_id="owner-1",
        )


def test_responsibility_snapshot_must_match_route_before_commit() -> None:
    uow = _uow(resolver=_WrongResolver())
    ready = _ready(uow)

    with pytest.raises(CompletionAttributionError):
        uow.complete(_handoff(ready))

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None


def test_completion_clock_cannot_precede_request_snapshot() -> None:
    uow = _uow(clock=lambda: NOW - timedelta(minutes=2))
    ready = _ready(uow)

    with pytest.raises(CompletionClockError):
        uow.complete(_handoff(ready))

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None


class _ApprovalDeadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        return started_at + timedelta(hours=1)


def test_unresolved_approval_item_cannot_be_forged_into_approved_handoff() -> None:
    policy = _Policy(ApprovalRequired(approver_id="legal", policy_version="approval-v1"))
    approvals = InMemoryApprovalStore()
    uow = InMemoryQuestionCompletionUnitOfWork(
        policy=policy,
        approvals=approvals,
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
    )
    ready = _ready(uow, requires_approval=True)
    boundary = ApprovalBoundary(
        requests=uow,
        approvals=approvals,
        policy=policy,
        authorizer=None,
        deadline_policy=_ApprovalDeadline(),
        draft_id_factory=lambda: "draft-1",
        item_id_factory=lambda: "approval-1",
        clock=lambda: NOW - timedelta(seconds=30),
    )
    boundary.gate_candidate(
        "req-1",
        expected_revision=1,
        candidate=AnswerCandidate(text="초안", mode="draft_only"),
    )
    assert isinstance(ready.state, ReadyToDispatch)
    item = approvals.get("approval-1")
    assert item is not None
    forged = ApprovedCandidate(
        request_id="req-1",
        item_id="approval-1",
        expected_revision=2,
        attempt=1,
        route=ready.state.route,
        candidate=item.draft.candidate,
        approved_by="alice",
        approved_at=NOW - timedelta(seconds=20),
        edited=False,
        policy_version="approval-v1",
        assignment_generation=ApprovalAssignmentGeneration.from_item(item),
    )

    with pytest.raises(CompletionEvidenceError):
        uow.complete(forged)

    assert uow.by_request("req-1") is None
