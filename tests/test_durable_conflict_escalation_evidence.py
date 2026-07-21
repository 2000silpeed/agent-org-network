"""S4.3b read-only Conflict escalation evidence contract."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.conflict_open_contract import (
    ConflictOpenCandidateClaim,
    RegistryConflictOpenSnapshotReader,
)
from agent_org_network.durable_conflict_escalation_evidence import (
    CandidateRegistryChanged,
    DivergentVotes,
    DurableConflictEscalationEvidenceError,
    DurableConflictEscalationEvidenceReader,
    Pending,
)
from agent_org_network.question_request import QuestionRequest, RouteTarget
from agent_org_network.registry import Registry
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_conflict_open_ingress import (
    DurableConflictOpenCommand,
    DurableConflictOpenIngressUnitOfWork,
    migrate_sqlite_durable_conflict_open_ingress_schema,
)
from agent_org_network.sqlite_durable_direct_conflict_uow import (
    migrate_sqlite_durable_direct_conflict_uow_schema,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)
from agent_org_network.user import User

NOW = datetime(2026, 7, 16, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


class _Authority:
    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
        return AuthorizationGrant(
            org_id=principal.org_id, subject_id=principal.subject_id, action=action,
            resource=resource, roles=("requester",), policy_version="p", policy_digest="a" * 64,
        )

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: Action,
        resource: ResourceRef,
    ) -> bool:
        return True


class _Scope:
    def proves_registry_org(self, *, registry: Registry, org_id: str) -> bool:
        return org_id in {_ref("org", "one"), _ref("org", "two")}


def _card(card: str, owner: str) -> AgentCard:
    from datetime import date

    return AgentCard(agent_id=card, owner=owner, team="t", summary="s", domains=["billing"], last_reviewed_at=date(2026, 1, 1))


def _prepared(tmp_path: Path) -> tuple[Path, Registry, str, str, str, tuple[ConflictOpenCandidateClaim, ...]]:
    path = tmp_path / "evidence.sqlite"
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_conflict_open_ingress_schema(str(path))
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    completion = SqliteQuestionCompletionUnitOfWork(path, policy=cast(Any, object()), approvals=cast(Any, object()), responsibility_resolver=cast(Any, object()), record_id_factory=lambda: "r", clock=lambda: NOW)
    org, request, conflict = _ref("org", "one"), _ref("request", "one"), _ref("conflict", "one")
    completion.create(QuestionRequest.receive(org_id=org, requester_id="requester", question="secret", request_id_factory=lambda: request, clock=lambda: NOW - timedelta(minutes=1), due_at=NOW + timedelta(hours=1)))
    registry = Registry()
    for subject in ("requester", "owner-1", "owner-2"):
        registry.register_user(User(id=subject))
    for card, owner in (("card-1", "owner-1"), ("card-2", "owner-2")):
        registry.register(_card(card, owner))
    claims = tuple(ConflictOpenCandidateClaim(card, "billing", RouteTarget(intent="billing", agent_id=card, requires_approval=False)) for card in ("card-1", "card-2"))
    from agent_org_network.central_authority import AuthenticatedPrincipal
    ingress = DurableConflictOpenIngressUnitOfWork(completion=completion, central_authorizer=cast(Any, _Authority()), registry_snapshot_reader=RegistryConflictOpenSnapshotReader(registry, _Scope()), clock=lambda: NOW, receipt_id_factory=lambda: "receipt", baseline_id_factory=lambda: "baseline")
    ingress.open(principal=AuthenticatedPrincipal(org_id=org, subject_id="requester", identity_provider="oidc", identity_session_id="s"), command=DurableConflictOpenCommand(conflict, request, claims))
    completion.close()
    return path, registry, org, request, conflict, claims


def _vote(path: Path, *, org: str, request: str, conflict: str, owner: str, target: str, receipt: str) -> None:
    import sqlite3
    from agent_org_network.sqlite_durable_direct_conflict_uow import _command_digest_for  # pyright: ignore[reportPrivateUsage]

    created = NOW.isoformat()
    owner_ref, target_ref, receipt_ref = _ref("subject", owner), _ref("card", target), _ref("receipt", receipt)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        candidate_sha = con.execute("SELECT candidate_set_sha256 FROM durable_linked_conflict_cases WHERE conflict_id=?", (conflict,)).fetchone()[0]
        row = {"org_id": org, "request_id": request, "conflict_id": conflict, "concurrence_round": 1, "actor_subject_ref": owner_ref, "owner_subject_ref": owner_ref, "target_card_ref": target_ref, "candidate_set_sha256": candidate_sha, "candidate_owner_count": 2, "action": "conflict.concur", "expected_request_revision": 1}
        digest = _command_digest_for(row)  # type: ignore[arg-type]
        con.execute("INSERT INTO durable_direct_conflict_votes VALUES(?,?,?,?,?,?,?,?,?,?)", (conflict, org, request, 1, owner_ref, target_ref, candidate_sha, 2, receipt_ref, created))
        con.execute("INSERT INTO durable_direct_conflict_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (receipt_ref, org, request, conflict, 1, digest, owner_ref, owner_ref, target_ref, candidate_sha, 2, "conflict.concur", 1, created))
        con.execute("INSERT INTO durable_direct_conflict_audit_intents VALUES(?,?,?,?,?,?)", (receipt_ref, org, request, "conflict.concur", digest, created))
        con.execute("INSERT INTO durable_direct_conflict_outbox_intents VALUES(?,?,?,?,?,?)", (receipt_ref, org, request, "conflict.concur", digest, created))
        con.execute("INSERT INTO durable_direct_conflict_result_projections VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (receipt_ref, org, request, conflict, 1, "vote_recorded", owner_ref, target_ref, candidate_sha, 2, 1, created))
        con.commit()
    finally:
        con.close()


def _reader(path: Path, registry: Registry) -> DurableConflictEscalationEvidenceReader:
    return DurableConflictEscalationEvidenceReader(path, RegistryConflictOpenSnapshotReader(registry, _Scope()))


def test_no_votes_is_pending_and_never_leaks_claim_preimages(tmp_path: Path) -> None:
    path, registry, org, request, conflict, claims = _prepared(tmp_path)
    outcome = _reader(path, registry).read(org_id=org, conflict_id=conflict, claims=claims)
    assert isinstance(outcome, Pending)
    assert outcome.request_ref == request and outcome.accepted_vote_count == 0
    assert "card-1" not in repr(outcome) and "billing" not in repr(outcome)


def test_all_distinct_valid_round_votes_seal_divergence(tmp_path: Path) -> None:
    path, registry, org, request, conflict, claims = _prepared(tmp_path)
    _vote(path, org=org, request=request, conflict=conflict, owner="owner-1", target="card-1", receipt="one")
    _vote(path, org=org, request=request, conflict=conflict, owner="owner-2", target="card-2", receipt="two")
    outcome = _reader(path, registry).read(org_id=org, conflict_id=conflict, claims=claims)
    assert isinstance(outcome, DivergentVotes)
    assert outcome.reason == "divergent_votes" and outcome.candidate_snapshot_sha256


def test_current_registry_drift_has_priority_over_vote_outcome(tmp_path: Path) -> None:
    path, registry, org, request, conflict, claims = _prepared(tmp_path)
    _vote(path, org=org, request=request, conflict=conflict, owner="owner-1", target="card-1", receipt="one")
    registry.register_user(User(id="owner-3"))
    registry.replace_card(_card("card-1", "owner-3"))
    outcome = _reader(path, registry).read(org_id=org, conflict_id=conflict, claims=claims)
    assert isinstance(outcome, CandidateRegistryChanged)
    assert outcome.reason == "snapshot_digest_mismatch"


def test_unanimous_open_case_is_fail_closed_not_escalated(tmp_path: Path) -> None:
    path, registry, org, request, conflict, claims = _prepared(tmp_path)
    _vote(path, org=org, request=request, conflict=conflict, owner="owner-1", target="card-1", receipt="one")
    _vote(path, org=org, request=request, conflict=conflict, owner="owner-2", target="card-1", receipt="two")
    with pytest.raises(DurableConflictEscalationEvidenceError):
        _reader(path, registry).read(org_id=org, conflict_id=conflict, claims=claims)
