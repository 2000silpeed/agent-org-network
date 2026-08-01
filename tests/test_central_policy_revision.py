from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from agent_org_network.central_policy_revision import (
    ActivatePolicy,
    ApprovalReference,
    ImportPolicy,
    PolicyApprovalEvidence,
    PolicyRevisionApplication,
    PolicyRevisionConflict,
    PolicyRevisionUnavailable,
    RollbackPolicy,
    SqlitePolicyApprovalPort,
    migrate_central_policy_revision_schema,
    policy_revision_schema_ready,
)


def _document(version: str = "policy-1") -> dict[str, object]:
    return {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": version,
        "subject_roles": [
            {"org_id": "acme", "subject_id": "user-1", "roles": ["requester"]},
        ],
        "role_permissions": [
            {"role": "requester", "actions": ["question.create", "question.read"]},
        ],
        "route_rules": [],
        "worker_bindings": [],
    }


class _Approval:
    def __init__(self) -> None:
        self.last: PolicyApprovalEvidence | None = None

    def resolve_and_claim(
        self, *, org_id: str, actor_user_id: str, approval: ApprovalReference,
        command_digest: str, active_pointer_fingerprint: str,
    ) -> PolicyApprovalEvidence | None:
        self.last = PolicyApprovalEvidence(
            evidence_id=approval.evidence_id,
            evidence_digest=approval.evidence_digest,
            org_id=org_id,
            actor_user_id=actor_user_id,
            action="policy.write",
            resource_kind="authority_policy",
            resource_id=org_id,
            command_digest=command_digest,
            active_pointer_fingerprint=active_pointer_fingerprint,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        return self.last


def test_policy_bootstrap_activate_and_replay_are_epoch_monotonic(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_policy_revision_schema(database)
    assert policy_revision_schema_ready(database)
    approval = _Approval()
    app = PolicyRevisionApplication(
        database, approval, clock=lambda: datetime(2026, 8, 1, tzinfo=UTC)
    )
    first = app.bootstrap(org_id="acme", actor_user_id="admin", document=_document())
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT revision_id,policy_digest FROM central_policy_bootstrap_receipts"
        ).fetchone() == (first.revision_id, first.policy_digest)
    changed = _document("policy-2")
    command = ActivatePolicy(
        expected_epoch=1,
        expected_digest=first.policy_digest,
        document=changed,
        approval=ApprovalReference(evidence_id="approval-1", evidence_digest="a" * 64),
    )
    receipt = app.apply(org_id="acme", actor_user_id="admin", command=command, idempotency_key="policy-activate-1")
    assert receipt.operation == "activated"
    assert receipt.epoch == 2
    assert app.active("acme").policy_version == "policy-2"
    replay = app.apply(org_id="acme", actor_user_id="admin", command=command, idempotency_key="policy-activate-1")
    assert replay.replayed is True
    assert replay.receipt_id == receipt.receipt_id
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM central_policy_change_audits WHERE receipt_id=?",
            (receipt.receipt_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT delivered FROM central_policy_change_outbox WHERE receipt_id=?",
            (receipt.receipt_id,),
        ).fetchone() == (0,)


def test_policy_replay_rechecks_current_approval_expiry(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_policy_revision_schema(database)

    class ExpiringApproval(_Approval):
        calls = 0

        def resolve_and_claim(
            self, *, org_id: str, actor_user_id: str, approval: ApprovalReference,
            command_digest: str, active_pointer_fingerprint: str,
        ) -> PolicyApprovalEvidence | None:
            evidence = super().resolve_and_claim(
                org_id=org_id, actor_user_id=actor_user_id, approval=approval,
                command_digest=command_digest, active_pointer_fingerprint=active_pointer_fingerprint,
            )
            assert evidence is not None
            self.calls += 1
            if self.last is not None and self.last.evidence_id == "approval-expiring" and self.calls > 2:
                evidence = PolicyApprovalEvidence(
                    **evidence.model_dump(exclude={"expires_at"}),
                    expires_at=datetime(2026, 7, 31, tzinfo=UTC),
                )
                self.last = evidence
            return evidence

    approval = ExpiringApproval()
    app = PolicyRevisionApplication(
        database, approval, clock=lambda: datetime(2026, 8, 1, tzinfo=UTC)
    )
    first = app.bootstrap(org_id="acme", actor_user_id="admin", document=_document())
    command = ActivatePolicy(
        expected_epoch=1,
        expected_digest=first.policy_digest,
        document=_document("policy-expiring"),
        approval=ApprovalReference(evidence_id="approval-expiring", evidence_digest="d" * 64),
    )
    app.apply(org_id="acme", actor_user_id="admin", command=command, idempotency_key="policy-expiring")
    with pytest.raises(PolicyRevisionConflict, match="expired"):
        app.apply(org_id="acme", actor_user_id="admin", command=command, idempotency_key="policy-expiring")
    assert app.active("acme").epoch == 2


def test_policy_stale_pointer_and_invalid_document_write_zero(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_policy_revision_schema(database)
    approval = _Approval()
    app = PolicyRevisionApplication(database, approval)
    first = app.bootstrap(org_id="acme", actor_user_id="admin", document=_document())
    with pytest.raises(PolicyRevisionConflict):
        app.apply(
            org_id="acme",
            actor_user_id="admin",
            command=ActivatePolicy(
                expected_epoch=1,
                expected_digest="b" * 64,
                document=_document("policy-2"),
                approval=ApprovalReference(evidence_id="approval-2", evidence_digest="b" * 64),
            ),
            idempotency_key="policy-activate-stale",
        )
    invalid = deepcopy(_document("policy-3"))
    invalid["unknown"] = True
    with pytest.raises(PolicyRevisionUnavailable):
        app.apply(
            org_id="acme",
            actor_user_id="admin",
            command=ActivatePolicy(
                expected_epoch=1,
                expected_digest=first.policy_digest,
                document=invalid,
                approval=ApprovalReference(evidence_id="approval-3", evidence_digest="c" * 64),
            ),
            idempotency_key="policy-activate-invalid",
        )
    assert app.active("acme").epoch == 1


def test_policy_rollback_creates_a_new_epoch_from_retained_revision(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_policy_revision_schema(database)
    approval = _Approval()
    app = PolicyRevisionApplication(
        database, approval, clock=lambda: datetime(2026, 8, 1, tzinfo=UTC)
    )
    first = app.bootstrap(org_id="acme", actor_user_id="admin", document=_document())
    activated = app.apply(
        org_id="acme",
        actor_user_id="admin",
        command=ActivatePolicy(
            expected_epoch=1,
            expected_digest=first.policy_digest,
            document=_document("policy-2"),
            approval=ApprovalReference(evidence_id="approval-activate", evidence_digest="a" * 64),
        ),
        idempotency_key="policy-activate-rollback",
    )
    rollback = app.apply(
        org_id="acme",
        actor_user_id="admin",
        command=RollbackPolicy(
            expected_epoch=2,
            expected_digest=activated.policy_digest,
            target_revision_id=first.revision_id,
            target_digest=first.policy_digest,
            approval=ApprovalReference(evidence_id="approval-rollback", evidence_digest="b" * 64),
        ),
        idempotency_key="policy-rollback-1",
    )
    assert rollback.operation == "rolled_back"
    assert rollback.epoch == 3
    assert rollback.previous_epoch == 2
    active = app.active("acme")
    assert active.epoch == 3
    assert active.policy_digest == first.policy_digest
    assert active.revision_id != first.revision_id


@pytest.mark.parametrize("field", ["org_id", "actor_user_id", "command_digest", "active_pointer_fingerprint"])
def test_policy_rejects_foreign_or_mismatched_approval_without_write(
    tmp_path: Path, field: str
) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_policy_revision_schema(database)

    class WrongApproval(_Approval):
        def resolve_and_claim(
            self, *, org_id: str, actor_user_id: str, approval: ApprovalReference,
            command_digest: str, active_pointer_fingerprint: str,
        ) -> PolicyApprovalEvidence | None:
            evidence = super().resolve_and_claim(
                org_id=org_id, actor_user_id=actor_user_id, approval=approval,
                command_digest=command_digest, active_pointer_fingerprint=active_pointer_fingerprint,
            )
            assert evidence is not None
            values = evidence.model_dump()
            if field == "org_id":
                values["org_id"] = "other-org"
            elif field == "actor_user_id":
                values["actor_user_id"] = "other-user"
            elif field == "command_digest":
                values["command_digest"] = "f" * 64
            else:
                values["active_pointer_fingerprint"] = "e" * 64
            return PolicyApprovalEvidence.model_validate(values)

    app = PolicyRevisionApplication(database, WrongApproval())
    first = app.bootstrap(org_id="acme", actor_user_id="admin", document=_document())
    with pytest.raises(PolicyRevisionConflict):
        app.apply(
            org_id="acme",
            actor_user_id="admin",
            command=ImportPolicy(
                expected_epoch=1,
                expected_digest=first.policy_digest,
                document=_document("policy-2"),
                approval=ApprovalReference(evidence_id="approval-wrong", evidence_digest="c" * 64),
            ),
            idempotency_key=f"policy-wrong-{field}",
        )
    assert app.active("acme").epoch == 1


def test_sqlite_policy_approval_port_claims_one_command_binding(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_policy_revision_schema(database)
    port = SqlitePolicyApprovalPort(
        database, clock=lambda: datetime(2026, 8, 1, tzinfo=UTC)
    )
    evidence = PolicyApprovalEvidence(
        evidence_id="approval-sqlite",
        evidence_digest="e" * 64,
        org_id="acme",
        actor_user_id="admin",
        action="policy.write",
        resource_kind="authority_policy",
        resource_id="acme",
        command_digest="c" * 64,
        active_pointer_fingerprint="f" * 64,
        expires_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    port.issue(evidence)
    claimed = port.resolve_and_claim(
        org_id="acme", actor_user_id="admin",
        approval=ApprovalReference(evidence_id="approval-sqlite", evidence_digest="e" * 64),
        command_digest="c" * 64, active_pointer_fingerprint="f" * 64,
    )
    assert claimed == evidence
    assert port.resolve_and_claim(
        org_id="acme", actor_user_id="admin",
        approval=ApprovalReference(evidence_id="approval-sqlite", evidence_digest="e" * 64),
        command_digest="d" * 64, active_pointer_fingerprint="f" * 64,
    ) is None
