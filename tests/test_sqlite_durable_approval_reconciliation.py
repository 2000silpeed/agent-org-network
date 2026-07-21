# pyright: reportArgumentType=false
"""P17.9 S3.2d one-shot durable expiry reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from agent_org_network.answer_finalization import AnswerResponsibilitySnapshot
from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalDraft,
    ApprovalItem,
    ApprovalRequired,
    ApprovalUnavailable,
    AnswerCandidate,
    ReassignExpiredApproval,
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.question_request import (
    AwaitingApproval,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_approval_assignments_v2 import (
    encode_approval_assignment_v2,
    migrate_sqlite_approval_assignments_v2_schema,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_approval_lifecycle import (
    migrate_sqlite_durable_approval_lifecycle_schema,
)
from agent_org_network.sqlite_durable_approval_reconciliation import (
    DurableApprovalExpiryReconciliationError,
    DurableApprovalExpiryReconciler,
)

NOW = datetime(2026, 7, 16, tzinfo=UTC)


class _Policy:
    def __init__(self, *, unavailable: bool = False, error: Exception | None = None) -> None:
        self.unavailable, self.error, self.calls = unavailable, error, 0

    def evaluate(self, *, assignment: ApprovalItem, now: datetime) -> object:
        self.calls += 1
        if self.error:
            raise self.error
        generation = ApprovalAssignmentGeneration.from_item(assignment)
        if self.unavailable:
            return ApprovalUnavailable(
                assignment_generation=generation,
                policy_version="expiry-v1",
                authority_version="authority-v1",
                evidence_ref="expiry-evidence",
            )
        return ReassignExpiredApproval(
            assignment_generation=generation,
            requirement=ApprovalRequired(approver_id="fallback-1", policy_version="v2"),
            due_at=now + timedelta(hours=1),
            policy_version="expiry-v1",
            authority_version="authority-v1",
            evidence_ref="expiry-evidence",
        )


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value, self.calls = value, 0

    def now(self, _tx: object) -> datetime:
        self.calls += 1
        return self.value


class _Authority:
    def __init__(self) -> None:
        self.allow, self.calls = True, 0

    def authorize(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef
    ) -> AuthorizationGrant:
        self.calls += 1
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,
            resource=resource,
            roles=("operator",),
            policy_version="v1",
            policy_digest="0" * 64,
        )  # type: ignore[arg-type]

    def verify(self, *_: object) -> bool:
        return self.allow


class _Resolver:
    def resolve(self, *, org_id: str, route: RouteTarget) -> AnswerResponsibilitySnapshot:
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id="owner")


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="org-1", subject_id="operator-1", identity_provider="test", identity_session_id="s"
    )


def _prepared(
    tmp_path: Path,
    *,
    due: datetime = NOW,
    org_id: str = "org-1",
    request_id: str = "request-1",
    item_id: str = "item-1",
) -> SqliteQuestionCompletionUnitOfWork:
    db = tmp_path / "reconcile.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    migrate_sqlite_durable_approval_lifecycle_schema(db)
    completion = SqliteQuestionCompletionUnitOfWork(
        db, policy=object(), approvals=object(), responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record", clock=lambda: NOW,
    )  # type: ignore[arg-type]
    route = RouteTarget(intent="refund", agent_id="card", requires_approval=True, authority_version="v1")
    received = QuestionRequest.receive(org_id=org_id, requester_id="u", question="q", request_id_factory=lambda: request_id, clock=lambda: NOW - timedelta(minutes=3), due_at=NOW + timedelta(days=1))
    completion.create(received)
    ready = received.record_initial_routing(intent="refund", disposition="routed", target=ReadyToDispatch(route=route, attempt=1, trigger_key="t", handling=HandlingAssignment(kind="system", ref="t", due_at=NOW + timedelta(days=1))), clock=lambda: NOW - timedelta(minutes=2))
    assert completion.compare_and_set(request_id, 0, received, ready)
    item = ApprovalItem(item_id=item_id, org_id=org_id, request_id=request_id, awaiting_revision=2, attempt=1, route=route, draft=ApprovalDraft(draft_id=f"draft-{item_id}", request_id=request_id, attempt=1, route=route, candidate=AnswerCandidate(text="q"), created_at=NOW), requirement=ApprovalRequired(approver_id="approver-1", policy_version="v1"), created_at=NOW, due_at=due)
    waiting = ready.transition(AwaitingApproval(route=route, attempt=1, draft_ref=item.item_id, handling=HandlingAssignment(kind="approval_item", ref=item.item_id, due_at=due)), clock=lambda: NOW)
    assert completion.compare_and_set(request_id, 1, ready, waiting)
    body, digest = encode_approval_assignment_v2(item)
    completion._connection.execute("INSERT INTO durable_approval_assignments_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)", (item.item_id, item.org_id, item.request_id, 2, 1, 1, None, "open", body, digest, 1))  # pyright: ignore[reportPrivateUsage]
    completion._connection.commit()  # pyright: ignore[reportPrivateUsage]
    return completion


def _runner(completion: SqliteQuestionCompletionUnitOfWork, *, clock: _Clock, policy: _Policy, authority: _Authority | None = None) -> DurableApprovalExpiryReconciler:
    ids = iter(("receipt-1", "receipt-2", "receipt-3"))
    assignment_ids = iter(("item-2", "item-3", "item-4"))
    return DurableApprovalExpiryReconciler(completion=completion, central_authorizer=cast(Any, authority or _Authority()), expiry_policy=policy, database_clock=clock, receipt_id_factory=lambda: next(ids), assignment_id_factory=lambda: next(assignment_ids))


def test_due_boundary_and_future_item_are_deterministic_and_bounded(tmp_path: Path) -> None:
    completion = _prepared(tmp_path, due=NOW)
    clock, policy = _Clock(NOW), _Policy()
    report = _runner(completion, clock=clock, policy=policy).reconcile(principal=_principal(), limit=1)
    assert [outcome.kind for outcome in report.outcomes] == ["reassigned"]
    assert policy.calls == 1 and report.database_time == NOW
    # A new runner has no scan cache; committed predecessor is no longer open.
    assert _runner(completion, clock=clock, policy=policy).reconcile(principal=_principal(), limit=1).outcomes == ()

    future_path = tmp_path / "future"
    future_path.mkdir()
    future = _prepared(future_path, due=NOW + timedelta(seconds=1))
    future_report = _runner(future, clock=_Clock(NOW), policy=_Policy()).reconcile(principal=_principal(), limit=1)
    assert future_report.outcomes == ()


def test_direct_due_successor_is_not_chained_in_one_invocation(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    clock, policy = _Clock(NOW), _Policy()
    # The first command's own DB-time read is later than its scan-time read, but
    # its successor is still excluded because the ID list was sealed first.
    report = _runner(completion, clock=clock, policy=policy).reconcile(principal=_principal(), limit=8)
    assert len(report.outcomes) == 1 and policy.calls == 1


def test_cross_org_due_assignment_is_never_disclosed_or_evaluated(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    # Same database, independently canonical request/assignment for another org.
    _prepared(tmp_path, org_id="org-2", request_id="request-2", item_id="other-2")
    clock, policy, authority = _Clock(NOW), _Policy(), _Authority()
    report = _runner(completion, clock=clock, policy=policy, authority=authority).reconcile(
        principal=_principal(), limit=8
    )
    assert [outcome.predecessor_item_id for outcome in report.outcomes] == ["item-1"]
    # One in-scope expiry authorizes twice (start/commit); the other-org row
    # never reaches policy or authorization and is absent from the report.
    assert policy.calls == 1 and authority.calls == 2


def test_cross_org_corrupt_row_does_not_block_scoped_reconciliation(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    _prepared(tmp_path, org_id="org-2", request_id="request-2", item_id="other-2")
    # Correct DB schema but a noncanonical row in another organization.  This
    # caller has no authority to inspect it, so scoped validation must not turn
    # it into a cross-organization denial of service.
    completion._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE durable_approval_assignments_v2 SET assignment_json='{}' WHERE assignment_id='other-2'"
    )
    completion._connection.commit()  # pyright: ignore[reportPrivateUsage]
    policy, authority = _Policy(), _Authority()
    report = _runner(completion, clock=_Clock(NOW), policy=policy, authority=authority).reconcile(
        principal=_principal(), limit=1
    )
    assert [outcome.kind for outcome in report.outcomes] == ["reassigned"]
    assert policy.calls == 1 and authority.calls == 2


def test_in_scope_corrupt_row_fails_closed_before_policy_or_authority(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    completion._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE durable_approval_assignments_v2 SET assignment_json='{}' WHERE assignment_id='item-1'"
    )
    completion._connection.commit()  # pyright: ignore[reportPrivateUsage]
    policy, authority = _Policy(), _Authority()
    runner = _runner(completion, clock=_Clock(NOW), policy=policy, authority=authority)
    with pytest.raises(DurableApprovalExpiryReconciliationError):
        runner.reconcile(principal=_principal(), limit=1)
    assert policy.calls == authority.calls == 0


def test_dependency_isolated_but_capability_corruption_is_fatal(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    dependency = _runner(completion, clock=_Clock(NOW), policy=_Policy(error=RuntimeError("down")))
    assert dependency.reconcile(principal=_principal(), limit=1).outcomes[0].kind == "dependency"

    corrupt_path = tmp_path / "corrupt"
    corrupt_path.mkdir()
    corrupt = _prepared(corrupt_path)
    corrupt._connection.execute("UPDATE schema_component_manifests SET manifest_sha256='0' WHERE component_id='durable_approval_lifecycle_v1'")  # pyright: ignore[reportPrivateUsage]
    corrupt._connection.commit()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DurableApprovalExpiryReconciliationError):
        _runner(corrupt, clock=_Clock(NOW), policy=_Policy())


@pytest.mark.parametrize("limit", [0, -1, True, 1.0])
def test_limit_is_exact_positive_integer(tmp_path: Path, limit: object) -> None:
    with pytest.raises(DurableApprovalExpiryReconciliationError):
        _runner(_prepared(tmp_path), clock=_Clock(NOW), policy=_Policy()).reconcile(principal=_principal(), limit=cast(Any, limit))
