# pyright: reportArgumentType=false, reportPrivateUsage=false
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any, cast

import pytest

from agent_org_network.answer_finalization import AnswerResponsibilitySnapshot
from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalDraft,
    ApprovalItem,
    ApprovalReassignmentAuthorization,
    ApprovalRequired,
    ApprovalSupersession,
    ApprovalUnavailable,
    ApprovalUnavailabilityEvidence,
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
    decode_approval_assignment_v2,
    encode_approval_assignment_v2,
    migrate_sqlite_approval_assignments_v2_schema,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_approval_lifecycle import (
    migrate_sqlite_durable_approval_lifecycle_schema,
    reconcile_sqlite_durable_approval_lifecycle_schema,
)
from agent_org_network.sqlite_durable_approval_reassignment import (
    DurableApprovalReassignmentConflict,
    DurableApprovalReassignmentUnavailable,
    DurableApprovalReassignmentUnitOfWork,
)
from agent_org_network.sqlite_durable_approval_expiry import (
    DurableApprovalExpiryConflict,
    DurableApprovalExpiryUnavailable,
    DurableApprovalExpiryUnitOfWork,
)

NOW = datetime(2026, 7, 16, tzinfo=UTC)


class _Policy:
    def evaluate(self, *_: object) -> object:
        raise AssertionError


class _Resolver:
    def resolve(self, *, org_id: str, route: RouteTarget) -> AnswerResponsibilitySnapshot:
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id="owner")


class _Authority:
    def __init__(self) -> None:
        self.calls = 0
        self.allow = True

    def authorize(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef
    ) -> AuthorizationGrant:
        self.calls += 1
        assert action == "approval.reassign" and resource.resource_id == "item-1"
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,
            resource=resource,
            roles=("approver",),
            policy_version="v1",
            policy_digest="0" * 64,
        )  # type: ignore[arg-type]

    def verify(self, grant: object, principal: object, action: object, resource: object) -> bool:
        return self.allow


def _prepared(
    tmp_path: Path,
    *,
    authority: _Authority | None = None,
    fault: Callable[[str], None] | None = None,
    authorization_factory: Callable[[AuthenticatedPrincipal, ApprovalItem, str], object]
    | None = None,
    receipt_id_factory: Callable[[], str] | None = None,
    assignment_id_factory: Callable[[], str] | None = None,
    command_clock: Callable[[], datetime] | None = None,
):
    db = tmp_path / "w.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    migrate_sqlite_durable_approval_lifecycle_schema(db)
    c = SqliteQuestionCompletionUnitOfWork(
        db,
        policy=_Policy(),
        approvals=object(),
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "r",
        clock=lambda: NOW,
    )  # type: ignore[arg-type]
    route = RouteTarget(
        intent="refund", agent_id="card", requires_approval=True, authority_version="v1"
    )
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="u",
        question="secret",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW - timedelta(minutes=3),
        due_at=NOW + timedelta(hours=1),
    )
    c.create(received)
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
            attempt=1,
            trigger_key="t",
            handling=HandlingAssignment(kind="system", ref="t", due_at=NOW + timedelta(hours=1)),
        ),
        clock=lambda: NOW - timedelta(minutes=2),
    )
    assert c.compare_and_set("request-1", 0, received, ready)
    item = ApprovalItem(
        item_id="item-1",
        org_id="org-1",
        request_id="request-1",
        awaiting_revision=2,
        attempt=1,
        route=route,
        draft=ApprovalDraft(
            draft_id="draft",
            request_id="request-1",
            attempt=1,
            route=route,
            candidate=AnswerCandidate(text="secret"),
            created_at=NOW,
        ),
        requirement=ApprovalRequired(approver_id="approver-1", policy_version="v1"),
        created_at=NOW,
        due_at=NOW + timedelta(hours=1),
    )
    waiting = ready.transition(
        AwaitingApproval(
            route=route,
            attempt=1,
            draft_ref="item-1",
            handling=HandlingAssignment(kind="approval_item", ref="item-1", due_at=item.due_at),
        ),
        clock=lambda: NOW,
    )
    assert c.compare_and_set("request-1", 1, ready, waiting)
    body, sha = encode_approval_assignment_v2(item)
    c._connection.execute(
        "INSERT INTO durable_approval_assignments_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("item-1", "org-1", "request-1", 2, 1, 1, None, "open", body, sha, 1),
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    auth = authority or _Authority()

    def default_reassignment(
        p: AuthenticatedPrincipal, current: ApprovalItem, target: str
    ) -> object:
        return ApprovalReassignmentAuthorization(
            assignment_generation=ApprovalAssignmentGeneration.from_item(current),
            org_id=current.org_id,
            actor_id=p.subject_id,
            target_approver_id=target,
            requirement=ApprovalRequired(approver_id=target, policy_version="v2"),
            due_at=NOW + timedelta(hours=2),
            policy_version="policy-v2",
            authority_version="authority-v2",
            evidence_ref="evidence-1",
        )

    reassignment = authorization_factory or default_reassignment

    return (
        c,
        DurableApprovalReassignmentUnitOfWork(
            completion=c,
            central_authorizer=cast(Any, auth),
            reassignment_authorizer=reassignment,
            clock=command_clock or (lambda: NOW),
            receipt_id_factory=receipt_id_factory or (lambda: "receipt-1"),
            assignment_id_factory=assignment_id_factory or (lambda: "item-2"),
            fault_injector=fault,
        ),
        auth,
    )


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="org-1", subject_id="approver-1", identity_provider="test", identity_session_id="s"
    )


def test_manual_reassignment_is_atomic_and_replays(tmp_path: Path) -> None:
    c, u, _ = _prepared(tmp_path)
    result = u.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )
    assert result.successor_item_id == "item-2" and result.request_revision == 3
    assert (
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
        == result
    )
    rows = c._connection.execute(
        "SELECT status FROM durable_approval_assignments_v2 ORDER BY approval_round"
    ).fetchall()  # pyright: ignore[reportPrivateUsage]
    assert [row[0] for row in rows] == ["superseded", "open"]
    assert (
        c._connection.execute(
            "SELECT COUNT(*) FROM durable_approval_lifecycle_receipts"
        ).fetchone()[0]
        == 1
    )  # pyright: ignore[reportPrivateUsage]
    migrate_sqlite_durable_approval_lifecycle_schema(tmp_path / "w.sqlite")


def test_manual_replay_rejects_request_revision_rollback_without_repair(tmp_path: Path) -> None:
    c, u, _ = _prepared(tmp_path)
    original = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT state_kind, state_json, revision, updated_at FROM question_requests WHERE request_id='request-1'"
    ).fetchone()
    assert original is not None
    u.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )
    c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE question_requests SET state_kind=?, state_json=?, revision=?, updated_at=? WHERE request_id='request-1'",
        tuple(original),
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DurableApprovalReassignmentUnavailable):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
    persisted = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT state_kind, state_json, revision, updated_at FROM question_requests WHERE request_id='request-1'"
    ).fetchone()
    assert persisted is not None and tuple(persisted) == tuple(original)


def test_manual_replay_rejects_correct_hash_predecessor_supersession_mutation(
    tmp_path: Path,
) -> None:
    c, u, _ = _prepared(tmp_path)
    u.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )
    row = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id='item-1'"
    ).fetchone()
    assert row is not None
    predecessor = decode_approval_assignment_v2(
        assignment_json=row["assignment_json"],
        assignment_sha256=row["assignment_sha256"],
        org_id=row["org_id"],
        request_id=row["request_id"],
    )
    assert predecessor.supersession is not None
    changed = predecessor.model_copy(
        update={
            "supersession": ApprovalSupersession(
                reason="reassigned",
                successor_item_id=predecessor.supersession.successor_item_id,
                superseded_at=predecessor.supersession.superseded_at,
                policy_version=predecessor.supersession.policy_version,
                authority_version="tampered-authority",
                evidence_ref=predecessor.supersession.evidence_ref,
                actor_id=predecessor.supersession.actor_id,
                target_approver_id=predecessor.supersession.target_approver_id,
            )
        }
    )
    body, digest = encode_approval_assignment_v2(changed)
    c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE durable_approval_assignments_v2 SET assignment_json=?, assignment_sha256=? "
        "WHERE assignment_id='item-1'",
        (body, digest),
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DurableApprovalReassignmentUnavailable):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
    assert not reconcile_sqlite_durable_approval_lifecycle_schema(tmp_path / "w.sqlite").capable
    persisted = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT assignment_json, assignment_sha256 FROM durable_approval_assignments_v2 "
        "WHERE assignment_id='item-1'"
    ).fetchone()
    assert persisted is not None and tuple(persisted) == (body, digest)


def test_manual_replay_allows_a_legitimate_later_reassignment(tmp_path: Path) -> None:
    class Authority:
        def authorize(
            self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef
        ) -> AuthorizationGrant:
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
            return True

    c, first, authority = _prepared(tmp_path, authority=cast(Any, Authority()))
    initial = first.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )

    def authorize(
        principal: AuthenticatedPrincipal, item: ApprovalItem, target: str
    ) -> ApprovalReassignmentAuthorization:
        return ApprovalReassignmentAuthorization(
            assignment_generation=ApprovalAssignmentGeneration.from_item(item),
            org_id=item.org_id,
            actor_id=principal.subject_id,
            target_approver_id=target,
            requirement=ApprovalRequired(approver_id=target, policy_version="v3"),
            due_at=NOW + timedelta(hours=3),
            policy_version="policy-v3",
            authority_version="authority-v3",
            evidence_ref="evidence-3",
        )

    second = DurableApprovalReassignmentUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        reassignment_authorizer=authorize,
        clock=lambda: NOW,
        receipt_id_factory=lambda: "receipt-2",
        assignment_id_factory=lambda: "item-3",
    )
    second.reassign(
        principal=_principal(), predecessor_item_id="item-2", target_approver_id="approver-3"
    )
    assert (
        first.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
        == initial
    )


class _DatabaseClock:
    def __init__(self, value: datetime) -> None:
        self.value, self.calls = value, 0

    def now(self, _transaction: object) -> datetime:
        self.calls += 1
        return self.value


class _ExpiryPolicy:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable, self.calls = unavailable, 0

    def evaluate(self, *, assignment: ApprovalItem, now: datetime) -> object:
        self.calls += 1
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


class _ExpiryAuthority(_Authority):
    def authorize(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef
    ) -> AuthorizationGrant:
        self.calls += 1
        assert action == "approval.expire" and resource.resource_id == "item-1"
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,
            resource=resource,
            roles=("operator",),
            policy_version="v1",
            policy_digest="0" * 64,
        )  # type: ignore[arg-type]


def test_expiry_reassigns_at_due_boundary_once(tmp_path: Path) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)
    policy, clock = _ExpiryPolicy(), _DatabaseClock(NOW + timedelta(hours=1))
    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=policy,
        database_clock=clock,
        receipt_id_factory=lambda: "expiry-receipt-1",
        assignment_id_factory=lambda: "item-2",
    )
    result = u.expire(principal=_principal(), predecessor_item_id="item-1")
    assert result.successor_item_id == "item-2" and result.request_revision == 3
    assert clock.calls == policy.calls == 1
    assert u.expire(principal=_principal(), predecessor_item_id="item-1") == result
    assert clock.calls == policy.calls == 1


def test_expiry_replay_rejects_correct_hash_successor_route_draft_corruption(
    tmp_path: Path,
) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)
    policy, clock = _ExpiryPolicy(), _DatabaseClock(NOW + timedelta(hours=1))
    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=policy,
        database_clock=clock,
        receipt_id_factory=lambda: "expiry-receipt-1",
        assignment_id_factory=lambda: "item-2",
    )
    u.expire(principal=_principal(), predecessor_item_id="item-1")
    row = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id='item-2'"
    ).fetchone()
    assert row is not None
    successor = decode_approval_assignment_v2(
        assignment_json=row["assignment_json"],
        assignment_sha256=row["assignment_sha256"],
        org_id=row["org_id"],
        request_id=row["request_id"],
    )
    changed_route = RouteTarget(
        intent=successor.route.intent,
        agent_id="different-card",
        requires_approval=True,
        authority_version=successor.route.authority_version,
    )
    changed = successor.model_copy(
        update={
            "route": changed_route,
            "draft": successor.draft.model_copy(update={"route": changed_route}),
        }
    )
    body, digest = encode_approval_assignment_v2(changed)
    c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE durable_approval_assignments_v2 SET assignment_json=?, assignment_sha256=? "
        "WHERE assignment_id='item-2'",
        (body, digest),
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    before = (body, digest)
    with pytest.raises(DurableApprovalExpiryUnavailable):
        u.expire(principal=_principal(), predecessor_item_id="item-1")
    persisted = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT assignment_json, assignment_sha256 FROM durable_approval_assignments_v2 "
        "WHERE assignment_id='item-2'"
    ).fetchone()
    assert persisted is not None and tuple(persisted) == before


def test_expiry_replay_rejects_correct_hash_successor_policy_mutation(
    tmp_path: Path,
) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)
    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=_ExpiryPolicy(),
        database_clock=_DatabaseClock(NOW + timedelta(hours=1)),
        receipt_id_factory=lambda: "expiry-receipt-1",
        assignment_id_factory=lambda: "item-2",
    )
    u.expire(principal=_principal(), predecessor_item_id="item-1")
    row = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id='item-2'"
    ).fetchone()
    assert row is not None
    successor = decode_approval_assignment_v2(
        assignment_json=row["assignment_json"],
        assignment_sha256=row["assignment_sha256"],
        org_id=row["org_id"],
        request_id=row["request_id"],
    )
    changed = successor.model_copy(update={"due_at": successor.due_at + timedelta(minutes=1)})
    body, digest = encode_approval_assignment_v2(changed)
    c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE durable_approval_assignments_v2 SET assignment_json=?, assignment_sha256=? "
        "WHERE assignment_id='item-2'",
        (body, digest),
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DurableApprovalExpiryUnavailable):
        u.expire(principal=_principal(), predecessor_item_id="item-1")
    persisted = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT assignment_json, assignment_sha256 FROM durable_approval_assignments_v2 "
        "WHERE assignment_id='item-2'"
    ).fetchone()
    assert persisted is not None and tuple(persisted) == (body, digest)


def test_expiry_replay_rejects_request_revision_rollback_without_repair(tmp_path: Path) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)
    original = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT state_kind, state_json, revision, updated_at FROM question_requests WHERE request_id='request-1'"
    ).fetchone()
    assert original is not None
    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=_ExpiryPolicy(),
        database_clock=_DatabaseClock(NOW + timedelta(hours=1)),
        receipt_id_factory=lambda: "expiry-receipt-1",
        assignment_id_factory=lambda: "item-2",
    )
    u.expire(principal=_principal(), predecessor_item_id="item-1")
    c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE question_requests SET state_kind=?, state_json=?, revision=?, updated_at=? WHERE request_id='request-1'",
        tuple(original),
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DurableApprovalExpiryUnavailable):
        u.expire(principal=_principal(), predecessor_item_id="item-1")
    persisted = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT state_kind, state_json, revision, updated_at FROM question_requests WHERE request_id='request-1'"
    ).fetchone()
    assert persisted is not None and tuple(persisted) == tuple(original)


def test_expiry_rejects_future_item_before_policy(tmp_path: Path) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)
    policy, clock = _ExpiryPolicy(), _DatabaseClock(NOW)
    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=policy,
        database_clock=clock,
        receipt_id_factory=lambda: "receipt-1",
        assignment_id_factory=lambda: "item-2",
    )
    with pytest.raises(DurableApprovalExpiryConflict):
        u.expire(principal=_principal(), predecessor_item_id="item-1")
    assert policy.calls == 0


def test_expiry_reassign_successor_due_equal_to_db_time_is_zero_write(tmp_path: Path) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)
    now = NOW + timedelta(hours=1)

    class EqualDuePolicy:
        def evaluate(self, *, assignment: ApprovalItem, now: datetime) -> object:
            return ReassignExpiredApproval(
                assignment_generation=ApprovalAssignmentGeneration.from_item(assignment),
                requirement=ApprovalRequired(approver_id="fallback-1", policy_version="v2"),
                due_at=now,
                policy_version="expiry-v1",
                authority_version="authority-v1",
                evidence_ref="expiry-evidence",
            )

    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=cast(Any, EqualDuePolicy()),
        database_clock=_DatabaseClock(now),
        receipt_id_factory=lambda: "receipt-1",
        assignment_id_factory=lambda: "item-2",
    )
    with pytest.raises(DurableApprovalExpiryConflict):
        u.expire(principal=_principal(), predecessor_item_id="item-1")
    assert (
        c._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT COUNT(*) FROM durable_approval_lifecycle_receipts"
        ).fetchone()[0]
        == 0
    )


def test_expiry_unavailable_fails_request_without_successor(tmp_path: Path) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)
    policy, clock = _ExpiryPolicy(unavailable=True), _DatabaseClock(NOW + timedelta(hours=1))
    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=policy,
        database_clock=clock,
        receipt_id_factory=lambda: "receipt-1",
        assignment_id_factory=lambda: "item-2",
    )
    result = u.expire(principal=_principal(), predecessor_item_id="item-1")
    assert result.failure_code == "approval_unavailable" and result.successor_item_id is None
    request = c.get("request-1")
    assert request is not None and request.state.kind == "failed"
    assert (
        c._connection.execute("SELECT COUNT(*) FROM durable_approval_assignments_v2").fetchone()[0]
        == 1
    )  # pyright: ignore[reportPrivateUsage]


def test_expiry_unavailable_replay_rejects_correct_hash_policy_mutation(tmp_path: Path) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)
    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=_ExpiryPolicy(unavailable=True),
        database_clock=_DatabaseClock(NOW + timedelta(hours=1)),
        receipt_id_factory=lambda: "receipt-1",
        assignment_id_factory=lambda: "item-2",
    )
    u.expire(principal=_principal(), predecessor_item_id="item-1")
    row = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id='item-1'"
    ).fetchone()
    assert row is not None
    predecessor = decode_approval_assignment_v2(
        assignment_json=row["assignment_json"],
        assignment_sha256=row["assignment_sha256"],
        org_id=row["org_id"],
        request_id=row["request_id"],
    )
    assert predecessor.unavailability is not None
    changed_decision = predecessor.unavailability.decision.model_copy(
        update={"policy_version": "tampered-policy"}
    )
    changed = predecessor.model_copy(
        update={
            "unavailability": ApprovalUnavailabilityEvidence(
                decision=changed_decision,
                unavailable_at=predecessor.unavailability.unavailable_at,
            )
        }
    )
    body, digest = encode_approval_assignment_v2(changed)
    c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE durable_approval_assignments_v2 SET assignment_json=?, assignment_sha256=? "
        "WHERE assignment_id='item-1'",
        (body, digest),
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    before = (body, digest)
    with pytest.raises(DurableApprovalExpiryUnavailable):
        u.expire(principal=_principal(), predecessor_item_id="item-1")
    assert not reconcile_sqlite_durable_approval_lifecycle_schema(tmp_path / "w.sqlite").capable
    persisted = c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT assignment_json, assignment_sha256 FROM durable_approval_assignments_v2 "
        "WHERE assignment_id='item-1'"
    ).fetchone()
    assert persisted is not None and tuple(persisted) == before


def test_expiry_commit_time_authority_revocation_rolls_back(tmp_path: Path) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)
    policy, clock = _ExpiryPolicy(), _DatabaseClock(NOW + timedelta(hours=1))

    def revoke_after_first(_: str) -> None:
        authority.allow = False

    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=policy,
        database_clock=clock,
        receipt_id_factory=lambda: "receipt-1",
        assignment_id_factory=lambda: "item-2",
        fault_injector=revoke_after_first,
    )
    # The injector fires only after domain writes, so it cannot model a grant revocation;
    # a policy that flips verify before the commit reauthorization does.
    authority.calls = 0
    original = authority.verify

    def verify(grant: object, principal: object, action: object, resource: object) -> bool:
        if authority.calls >= 2:
            return False
        return original(grant, principal, action, resource)

    authority.verify = verify  # type: ignore[method-assign]
    with pytest.raises(DurableApprovalExpiryConflict):
        u.expire(principal=_principal(), predecessor_item_id="item-1")
    assert (
        c._connection.execute(
            "SELECT COUNT(*) FROM durable_approval_lifecycle_receipts"
        ).fetchone()[0]
        == 0
    )  # pyright: ignore[reportPrivateUsage]


def test_reassignment_rechecks_revoked_authority_and_rolls_back(tmp_path: Path) -> None:
    authority = _Authority()
    c, u, _ = _prepared(tmp_path, authority=authority)
    # authorize is also used to seal the receipt, so revoke after the initial
    # central check and prove the commit-time check writes nothing.
    calls = 0

    def verify(*args: object) -> bool:
        nonlocal calls
        calls += 1
        return calls < 2

    authority.verify = cast(Any, verify)
    with pytest.raises(DurableApprovalReassignmentConflict):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
    assert (
        c._connection.execute(
            "SELECT COUNT(*) FROM durable_approval_lifecycle_receipts"
        ).fetchone()[0]
        == 0
    )  # pyright: ignore[reportPrivateUsage]


def test_different_target_cannot_replay_committed_predecessor(tmp_path: Path) -> None:
    _, u, _ = _prepared(tmp_path)
    u.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )
    with pytest.raises(DurableApprovalReassignmentConflict):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-3"
        )


@pytest.mark.parametrize(
    "table, column, payload",
    [
        ("durable_approval_lifecycle_results", "result_json", {"raw": "question prose"}),
        ("durable_approval_lifecycle_evidence", "evidence_json", {"raw": "한글 본문"}),
    ],
)
def test_corrupt_correct_hash_receipt_never_replays(
    tmp_path: Path, table: str, column: str, payload: object
) -> None:
    c, u, _ = _prepared(tmp_path)
    u.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        f"UPDATE {table} SET {column}=?, {column.replace('_json', '_sha256')}=? WHERE receipt_id='receipt-1'",
        (raw, hashlib.sha256(raw.encode()).hexdigest()),
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DurableApprovalReassignmentUnavailable):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
    assert (
        c._connection.execute(  # pyright: ignore[reportPrivateUsage]
            f"SELECT {column} FROM {table} WHERE receipt_id='receipt-1'"
        ).fetchone()[0]
        == raw
    )


def test_partial_receipt_never_replays_or_repairs(tmp_path: Path) -> None:
    c, u, _ = _prepared(tmp_path)
    u.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )
    c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "DELETE FROM durable_approval_lifecycle_results WHERE receipt_id='receipt-1'"
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DurableApprovalReassignmentUnavailable):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
    assert (
        c._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT COUNT(*) FROM durable_approval_lifecycle_results WHERE receipt_id='receipt-1'"
        ).fetchone()[0]
        == 0
    )


def test_different_but_canonical_intent_time_never_replays_or_repairs(tmp_path: Path) -> None:
    c, u, _ = _prepared(tmp_path)
    u.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )
    c._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE durable_approval_lifecycle_audit_intents "
        "SET created_at='2026-07-16T00:00:01+00:00' WHERE receipt_id='receipt-1'"
    )
    c._connection.commit()  # pyright: ignore[reportPrivateUsage]
    before = (tmp_path / "w.sqlite").read_bytes()
    with pytest.raises(DurableApprovalReassignmentUnavailable):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
    assert (tmp_path / "w.sqlite").read_bytes() == before


def test_final_authorization_snapshot_seals_final_not_planning_value(tmp_path: Path) -> None:
    calls = 0

    def authorization(p: AuthenticatedPrincipal, current: ApprovalItem, target: str) -> object:
        nonlocal calls
        calls += 1
        version = "planning" if calls == 1 else "final"
        return ApprovalReassignmentAuthorization(
            assignment_generation=ApprovalAssignmentGeneration.from_item(current),
            org_id=current.org_id,
            actor_id=p.subject_id,
            target_approver_id=target,
            requirement=ApprovalRequired(approver_id=target, policy_version=version),
            due_at=NOW + timedelta(hours=2),
            policy_version=version,
            authority_version="authority-v2",
            evidence_ref="evidence-1",
        )

    c, u, _ = _prepared(tmp_path, authorization_factory=authorization)
    u.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )
    evidence = json.loads(
        c._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT evidence_json FROM durable_approval_lifecycle_evidence"
        ).fetchone()[0]
    )
    assert evidence["policy_digest"] == hashlib.sha256(b"final").hexdigest()


def test_revoked_central_authority_blocks_replay(tmp_path: Path) -> None:
    authority = _Authority()
    _, u, _ = _prepared(tmp_path, authority=authority)
    u.reassign(
        principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
    )
    authority.allow = False
    with pytest.raises(DurableApprovalReassignmentConflict):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )


@pytest.mark.parametrize(
    ("receipt", "successor"),
    [("한글 영수증", "item-2"), ("receipt-1", "english prose with spaces"), ("x" * 129, "item-2")],
)
def test_prose_or_oversize_generated_identifiers_write_zero(
    tmp_path: Path, receipt: str, successor: str
) -> None:
    c, u, _ = _prepared(
        tmp_path,
        receipt_id_factory=lambda: receipt,
        assignment_id_factory=lambda: successor,
    )
    with pytest.raises(DurableApprovalReassignmentConflict):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
    assert (
        c._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT COUNT(*) FROM durable_approval_lifecycle_receipts"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("invalid_clock", [True, False])
def test_noncanonical_clock_or_authorization_timestamp_writes_zero(
    tmp_path: Path, invalid_clock: bool
) -> None:
    odd = datetime(2026, 7, 16, tzinfo=timezone(timedelta(seconds=1)))

    def authorization(p: AuthenticatedPrincipal, current: ApprovalItem, target: str) -> object:
        return ApprovalReassignmentAuthorization(
            assignment_generation=ApprovalAssignmentGeneration.from_item(current),
            org_id=current.org_id,
            actor_id=p.subject_id,
            target_approver_id=target,
            requirement=ApprovalRequired(approver_id=target, policy_version="v2"),
            due_at=odd if not invalid_clock else NOW + timedelta(hours=2),
            policy_version="policy-v2",
            authority_version="authority-v2",
            evidence_ref="evidence-1",
        )

    c, u, _ = _prepared(
        tmp_path,
        authorization_factory=authorization,
        command_clock=(lambda: odd) if invalid_clock else None,
    )
    with pytest.raises(DurableApprovalReassignmentConflict):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
    assert (
        c._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT COUNT(*) FROM durable_approval_lifecycle_receipts"
        ).fetchone()[0]
        == 0
    )


def test_fault_before_commit_rolls_everything_back(tmp_path: Path) -> None:
    def fault(point: str) -> None:
        if point == "before_commit":
            raise RuntimeError(point)

    c, u, _ = _prepared(tmp_path, fault=fault)
    with pytest.raises(RuntimeError):
        u.reassign(
            principal=_principal(), predecessor_item_id="item-1", target_approver_id="approver-2"
        )
    assert (
        c._connection.execute(
            "SELECT COUNT(*) FROM durable_approval_lifecycle_receipts"
        ).fetchone()[0]
        == 0
    )  # pyright: ignore[reportPrivateUsage]


def test_expiry_policy_generation_spoof_is_zero_write(tmp_path: Path) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)

    class Spoof:
        def evaluate(self, *, assignment: ApprovalItem, now: datetime) -> object:
            return ApprovalUnavailable(
                assignment_generation=ApprovalAssignmentGeneration.from_item(assignment).model_copy(
                    update={"item_id": "other-item"}
                ),
                policy_version="expiry-v1",
                authority_version="authority-v1",
                evidence_ref="expiry-evidence",
            )

    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=cast(Any, Spoof()),
        database_clock=_DatabaseClock(NOW + timedelta(hours=1)),
        receipt_id_factory=lambda: "receipt-1",
        assignment_id_factory=lambda: "item-2",
    )
    with pytest.raises(DurableApprovalExpiryConflict):
        u.expire(principal=_principal(), predecessor_item_id="item-1")
    assert (
        c._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT COUNT(*) FROM durable_approval_lifecycle_receipts"
        ).fetchone()[0]
        == 0
    )


def test_expiry_fault_after_domain_rolls_everything_back(tmp_path: Path) -> None:
    authority = _ExpiryAuthority()
    c, _, _ = _prepared(tmp_path, authority=authority)

    def fail(point: str) -> None:
        if point == "after_domain":
            raise RuntimeError("injected")

    u = DurableApprovalExpiryUnitOfWork(
        completion=c,
        central_authorizer=cast(Any, authority),
        expiry_policy=_ExpiryPolicy(),
        database_clock=_DatabaseClock(NOW + timedelta(hours=1)),
        receipt_id_factory=lambda: "receipt-1",
        assignment_id_factory=lambda: "item-2",
        fault_injector=fail,
    )
    with pytest.raises(RuntimeError, match="injected"):
        u.expire(principal=_principal(), predecessor_item_id="item-1")
    request = c.get("request-1")
    assert request is not None and request.revision == 2
    assert (
        c._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT COUNT(*) FROM durable_approval_lifecycle_receipts"
        ).fetchone()[0]
        == 0
    )
    assert (
        c._connection.execute(
            "SELECT status FROM durable_approval_assignments_v2 WHERE assignment_id='item-1'"
        ).fetchone()[0]
        == "open"
    )  # pyright: ignore[reportPrivateUsage]
