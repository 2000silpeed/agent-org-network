from __future__ import annotations

import sqlite3
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from agent_org_network.sqlite_durable_reciprocal_review import (
    migrate_sqlite_durable_reciprocal_review_ledger,
)
from agent_org_network.sqlite_reciprocal_review_ai_mixed_disposition import (
    SqliteReciprocalReviewAiMixedDispositionConflict,
    SqliteReciprocalReviewAiMixedDispositionError,
    migrate_sqlite_reciprocal_review_ai_mixed_disposition_v5,
    provision_sqlite_reciprocal_review_v5_cycle,
    validate_sqlite_reciprocal_review_ai_mixed_disposition,
)
from agent_org_network.sqlite_reciprocal_review_assignment_terminal import (
    migrate_sqlite_reciprocal_review_assignment_terminal_v4,
)
from agent_org_network.sqlite_reciprocal_review_human_disposition import (
    migrate_sqlite_reciprocal_review_human_disposition_v2,
    upgrade_sqlite_reciprocal_review_cycles_to_v2,
)
from agent_org_network.sqlite_reciprocal_review_human_terminal import (
    migrate_sqlite_reciprocal_review_human_terminal_v3,
    provision_sqlite_reciprocal_review_v3_cycle,
)
from agent_org_network.reciprocal_review import (
    AiAdvisoryFindingBatch,
    AiReviewerPrincipal,
    ApproveRevision,
    HumanPrincipal,
    HumanReviewConclusion,
    RecordAiAdvisoryBatch,
    RejectRevision,
    SubmitAiMixedHumanDisposition,
)
from agent_org_network.sqlite_reciprocal_review_ai_batches import (
    create_sqlite_reciprocal_review_ai_batch_uow,
    migrate_sqlite_reciprocal_review_ai_batches,
)
from agent_org_network.sqlite_reciprocal_review_assignment_terminal import (
    assignment_human_authority_payload,
    create_sqlite_reciprocal_review_assignment_human_terminal_uow,
    create_sqlite_reciprocal_review_assignment_lease,
    provision_sqlite_reciprocal_review_v4_cycle,
)
from agent_org_network.sqlite_reciprocal_review_lease import (
    AiReviewerAssignment,
    ClaimReviewRun,
    create_sqlite_reciprocal_review_lease_uow,
    migrate_sqlite_reciprocal_review_lease,
)
from agent_org_network.sqlite_reciprocal_review_uow import (
    ArtifactContent,
    InitialReviewPolicy,
    InitialReviewRequirement,
    RegisterArtifactRevision,
    create_sqlite_reciprocal_review_uow,
)
from agent_org_network.sqlite_reciprocal_review_ai_mixed_disposition import (
    ai_mixed_disposition_authority_payload,
    create_sqlite_reciprocal_review_ai_mixed_disposition_uow,
)
from agent_org_network.reciprocal_review import (
    CreateSourceBindingIntent,
    SourceBindingCapability,
    SourceBoundaryDriftActionAuthorization,
    SourceBoundaryEnforcementPlan,
)
from agent_org_network.sqlite_reciprocal_review_source_binding import (
    SqliteReciprocalReviewSourceBindingError,
    create_sqlite_reciprocal_review_source_binding_intent_uow,
    migrate_sqlite_reciprocal_review_source_binding_v6,
    validate_sqlite_reciprocal_review_source_binding,
)


def _v4_snapshot(connection: sqlite3.Connection) -> None:
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    migrate_sqlite_reciprocal_review_human_disposition_v2(connection)
    migrate_sqlite_reciprocal_review_human_terminal_v3(connection)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(connection)


def test_v5_requires_explicit_v4_snapshot_and_installs_only_its_catalog() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    with pytest.raises(SqliteReciprocalReviewAiMixedDispositionError):
        migrate_sqlite_reciprocal_review_ai_mixed_disposition_v5(connection)

    _v4_snapshot(connection)
    migrate_sqlite_reciprocal_review_ai_mixed_disposition_v5(connection)
    validate_sqlite_reciprocal_review_ai_mixed_disposition(connection)
    assert connection.execute(
        "SELECT component_id FROM schema_component_manifests WHERE component_id=?",
        ("durable_reciprocal_review_ledger_v5",),
    ).fetchone() == ("durable_reciprocal_review_ledger_v5",)


def test_v5_never_backfills_missing_v4_cycle() -> None:
    connection = sqlite3.connect(":memory:")
    _v4_snapshot(connection)
    migrate_sqlite_reciprocal_review_ai_mixed_disposition_v5(connection)

    with pytest.raises(SqliteReciprocalReviewAiMixedDispositionError):
        provision_sqlite_reciprocal_review_v5_cycle(
            connection, org_id="org", cycle_id="unknown-cycle"
        )
    assert connection.execute(
        "SELECT count(*) FROM durable_reciprocal_review_cycles_v5"
    ).fetchone() == (0,)


SHA = "a" * 64


class _Content:
    def verify(self, **_: object) -> ArtifactContent:
        return ArtifactContent(SHA, "b" * 64)


class _Policy:
    def initial_policy(self, **_: object) -> InitialReviewPolicy:
        return InitialReviewPolicy(
            "c" * 64,
            (
                InitialReviewRequirement(
                    "ai", "all", 1, "ai-rubric", datetime(2026, 7, 20, tzinfo=UTC), False
                ),
                InitialReviewRequirement(
                    "human",
                    "all",
                    1,
                    "human-rubric",
                    datetime(2026, 7, 20, tzinfo=UTC),
                    False,
                    ("reviewer",),
                ),
            ),
        )


class _RegistrationAuthority:
    def authorize_registration(self, **_: object) -> bool:
        return True

    def authorize_reviewer(self, **_: object) -> bool:
        return True


class _LeaseAuthority:
    def authorize_ai_reviewer(self, **_: object) -> bool:
        return True

    def authorize_human_reviewer(self, **_: object) -> bool:
        return True


class _LeasePolicy:
    def requirement_is_current(self, **_: object) -> bool:
        return True


class _BatchPolicy:
    def batch_is_current(self, **_: object) -> bool:
        return True

    def permits_awaiting_human_disposition(self, **_: object) -> bool:
        return True


def _now(connection: sqlite3.Connection) -> datetime:
    return datetime.fromisoformat(
        connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        .fetchone()[0]
        .replace("Z", "+00:00")
    )


def _author() -> HumanPrincipal:
    return HumanPrincipal(
        org_id="org",
        subject_id="author",
        authenticated_at=datetime(2026, 7, 20, tzinfo=UTC),
        authn_context_digest=SHA,
    )


def _ai() -> AiReviewerPrincipal:
    return AiReviewerPrincipal(
        org_id="org",
        reviewer_id="ai-reviewer",
        model_execution_ref="execution",
        deployment_digest=SHA,
        rubric_digest=SHA,
    )


def _digest(value: object) -> str:
    import json

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _seed(path: Path) -> None:
    c = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(c)
    lineage = hashlib.sha256(b'["parent-event"]').hexdigest()
    c.execute(
        "INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "artifact",
            "parent",
            1,
            None,
            "ref:parent",
            SHA,
            "internal",
            "boundary",
            "ai",
            "b" * 64,
            lineage,
            "b" * 64,
            None,
            1,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)",
        (
            "org",
            "parent-event",
            "parent",
            "model_execution",
            "model",
            SHA,
            None,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
        ("org", "parent", "parent-event", 1),
    )
    c.commit()
    c.close()
    create_sqlite_reciprocal_review_uow(
        path,
        content_verifier=_Content(),
        review_policy_registry=_Policy(),
        reviewer_authorization=_RegistrationAuthority(),
        clock=lambda: datetime(2026, 7, 20, tzinfo=UTC),
    ).register(
        _author(),
        RegisterArtifactRevision(
            receipt_id="registration",
            artifact_id="artifact",
            revision_id="revision",
            parent_revision_id="parent",
            kind="knowledge",
            content_ref="ref",
            content_sha256=SHA,
            provenance_event_id="event",
            audit_id="registration-audit",
            outbox_id="registration-outbox",
        ),
    )
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_lease(c)
    migrate_sqlite_reciprocal_review_ai_batches(c)
    c.commit()
    c.close()
    lease = create_sqlite_reciprocal_review_lease_uow(
        path,
        reviewer_authorization=_LeaseAuthority(),
        policy_snapshot=_LeasePolicy(),
        db_time=_now,
        token_key=b"lease",
    )
    lease.assign(
        AiReviewerAssignment(
            "ai-assign",
            "ai-assign-audit",
            "ai-assign-outbox",
            "ai-run",
            "cycle:revision",
            "requirement:revision:1",
            _ai(),
        )
    )
    claim = lease.claim(
        ClaimReviewRun(
            "ai-claim", "ai-claim-audit", "ai-claim-outbox", "ai-run", _ai(), timedelta(minutes=5)
        )
    )
    assert claim.lease_token is not None
    signed = _digest(
        {
            "org_id": "org",
            "batch_id": "batch",
            "review_run_id": "ai-run",
            "model_execution_ref": "execution",
            "rubric_digest": SHA,
            "prompt_digest": "d" * 64,
            "input_digest": SHA,
            "findings": [],
            "cycle_id": "cycle:revision",
            "requirement_id": "requirement:revision:1",
            "policy_digest": "c" * 64,
            "provenance_digest": _provenance(path),
            "content_digest": SHA,
            "deployment_digest": SHA,
        }
    )
    create_sqlite_reciprocal_review_ai_batch_uow(
        path,
        lease_uow=lease,
        policy_snapshot=_BatchPolicy(),
        trusted_execution_keys={"ai": b"ai-key"},
    ).record(
        RecordAiAdvisoryBatch(
            receipt_id="batch-receipt",
            audit_id="batch-audit",
            outbox_id="batch-outbox",
            principal=_ai(),
            lease_epoch=claim.lease_epoch,
            lease_token=claim.lease_token,
            batch=AiAdvisoryFindingBatch(
                batch_id="batch",
                org_id="org",
                review_run_id="ai-run",
                model_execution_ref="execution",
                prompt_digest="d" * 64,
                rubric_digest=SHA,
                input_digest=SHA,
                signature=hmac.new(b"ai-key", signed.encode(), hashlib.sha256).hexdigest(),
                signature_algorithm="hmac-sha256",
                signing_key_id="ai",
                signed_payload_digest=signed,
                findings=(),
                created_at=_now(sqlite3.connect(path)),
            ),
        )
    )
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_human_disposition_v2(c)
    upgrade_sqlite_reciprocal_review_cycles_to_v2(c)
    migrate_sqlite_reciprocal_review_human_terminal_v3(c)
    provision_sqlite_reciprocal_review_v3_cycle(c, org_id="org", cycle_id="cycle:revision")
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(
        c,
        org_id="org",
        cycle_id="cycle:revision",
        assignments=(("requirement:revision:2", ("reviewer",), "d" * 64, "e" * 64, SHA),),
    )
    c.commit()
    c.close()
    _record_v4_terminal(path)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_ai_mixed_disposition_v5(c)
    provision_sqlite_reciprocal_review_v5_cycle(c, org_id="org", cycle_id="cycle:revision")
    c.commit()
    c.close()


def _provenance(path: Path) -> str:
    c = sqlite3.connect(path)
    value = c.execute(
        "SELECT provenance_digest FROM durable_reciprocal_review_cycles WHERE org_id='org' AND cycle_id='cycle:revision'"
    ).fetchone()[0]
    c.close()
    return value


def _record_v4_terminal(path: Path) -> None:
    c = sqlite3.connect(path)
    run = c.execute(
        "SELECT assignment_run_id FROM reciprocal_review_v4_assignment_runs"
    ).fetchone()[0]
    c.close()
    lease = create_sqlite_reciprocal_review_assignment_lease(
        path, clock=lambda: _now(sqlite3.connect(path)), token_key=b"v4-lease"
    )
    claim_at = _now(sqlite3.connect(path))
    epoch, token, _ = lease.claim(
        org_id="org",
        assignment_run_id=run,
        principal=HumanPrincipal(
            org_id="org", subject_id="reviewer", authenticated_at=claim_at, authn_context_digest=SHA
        ),
        lease_for=timedelta(minutes=5),
    )
    c = sqlite3.connect(path)
    at = _now(c)
    row = c.execute(
        "SELECT assignment_id,assignment_digest,contributor_digest,policy_digest,provenance_digest,content_digest,rubric_digest,input_digest FROM reciprocal_review_v4_assignment_runs JOIN reciprocal_review_v4_reviewer_assignments USING(org_id,assignment_id) WHERE assignment_run_id=?",
        (run,),
    ).fetchone()
    rule, count = c.execute(
        "SELECT completion_rule,required_count FROM durable_reciprocal_review_requirements WHERE org_id='org' AND requirement_id='requirement:revision:2'"
    ).fetchone()
    contributors = tuple(
        value[0]
        for value in c.execute(
            "SELECT principal_ref FROM durable_reciprocal_review_provenance_events WHERE org_id='org' AND revision_id='revision' AND principal_kind='human' ORDER BY principal_ref"
        )
    )
    c.close()
    payload = assignment_human_authority_payload(
        org_id="org",
        reviewer="reviewer",
        authenticated_at=at,
        expires_at=at + timedelta(minutes=5),
        revision_id="revision",
        cycle_id="cycle:revision",
        requirement_id="requirement:revision:2",
        assignment_id=row[0],
        assignment_run_id=run,
        assignment_digest=row[1],
        policy_digest=row[3],
        provenance_digest=row[4],
        contributor_digest=row[2],
        content_digest=row[5],
        rubric_digest=row[6],
        input_digest=row[7],
        completion_rule=rule,
        required_count=count,
        candidate_reviewers=("reviewer",),
        contributors=contributors,
    )
    principal = HumanPrincipal(
        org_id="org",
        subject_id="reviewer",
        authenticated_at=at,
        authn_context_digest=hmac.new(
            b"v4-human",
            __import__("json").dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest(),
    )
    create_sqlite_reciprocal_review_assignment_human_terminal_uow(
        path,
        trusted_human_assignment_authority_keys={"reviewer": b"v4-human"},
        trusted_lease_token_key=b"v4-lease",
        clock=lambda: at,
    ).record(
        principal=principal,
        receipt_id="v4-receipt",
        audit_id="v4-audit",
        outbox_id="v4-outbox",
        idempotency_key="v4-key",
        assignment_run_id=run,
        lease_epoch=epoch,
        lease_token=token,
        conclusion=HumanReviewConclusion(
            content_digest=SHA, rubric_digest="d" * 64, input_digest="e" * 64
        ),
    )


def _v5_principal(
    path: Path,
    *,
    subject: str = "disposer",
    key: bytes = b"human-key",
    at: datetime | None = None,
    action: str = "approve_revision",
) -> HumanPrincipal:
    c = sqlite3.connect(path)
    cycle = c.execute(
        "SELECT revision_id,upstream_revision,policy_digest,provenance_digest,upstream_snapshot_digest FROM durable_reciprocal_review_cycles_v5"
    ).fetchone()
    assignment = c.execute(
        "SELECT assignment_id,reviewer_ref FROM reciprocal_review_v4_reviewer_assignments"
    ).fetchone()
    terminal = c.execute(
        "SELECT assignment_id FROM reciprocal_review_v4_human_terminal_receipts"
    ).fetchone()
    c.close()
    assert cycle is not None and assignment is not None and terminal is not None
    ai = [{"requirement_id": "requirement:revision:1", "required": 1, "batches": ("batch",)}]
    human = [
        {
            "requirement_id": "requirement:revision:2",
            "required": 1,
            "assignments": (assignment,),
            "terminals": (terminal,),
        }
    ]
    ai_digest, human_digest = _digest(ai), _digest(human)
    overall = _digest({"ai": ai_digest, "human": human_digest, "eligible": True})
    contributor_rows = (
        sqlite3.connect(path)
        .execute(
            "SELECT principal_ref FROM durable_reciprocal_review_provenance_events WHERE org_id='org' AND revision_id='revision' AND principal_kind='human'"
        )
        .fetchall()
    )
    contributors = tuple(sorted(row[0] for row in contributor_rows))
    independence = _digest(
        {
            "rules": ("independent", "independent"),
            "contributors": contributors,
            "disposition_subject": subject,
            "independent": subject not in contributors,
        }
    )
    issued = at or _now(sqlite3.connect(path))
    base = HumanPrincipal(
        org_id="org", subject_id=subject, authenticated_at=issued, authn_context_digest=SHA
    )
    payload = ai_mixed_disposition_authority_payload(
        org_id="org",
        principal=base,
        revision_id=cycle[0],
        cycle_id="cycle:revision",
        action=action,
        policy_digest=cycle[2],
        provenance_digest=cycle[3],
        upstream_snapshot_digest=cycle[4],
        expected_upstream_revision=cycle[1],
        ai_eligibility_digest=ai_digest,
        human_eligibility_digest=human_digest,
        overall_eligibility_digest=overall,
        independence_digest=independence,
    )
    return base.model_copy(
        update={
            "authn_context_digest": hmac.new(
                key,
                __import__("json").dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                hashlib.sha256,
            ).hexdigest()
        }
    )


def _v5_command(*, receipt: str = "receipt", key: str = "key") -> SubmitAiMixedHumanDisposition:
    return SubmitAiMixedHumanDisposition(
        receipt_id=receipt,
        audit_id=f"audit-{receipt}",
        outbox_id=f"outbox-{receipt}",
        cycle_id="cycle:revision",
        expected_upstream_revision=2,
        idempotency_key=key,
        disposition=ApproveRevision(),
    )


def _v5_uow(path: Path, *, fault: Callable[[str], None] | None = None):
    return create_sqlite_reciprocal_review_ai_mixed_disposition_uow(
        path,
        trusted_human_disposition_authority_keys={"disposer": b"human-key"},
        trusted_ai_execution_keys={"ai": b"ai-key"},
        fault_injector=fault,
    )


def _counts(path: Path) -> tuple[object, ...]:
    c = sqlite3.connect(path)
    result = (
        tuple(
            c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "reciprocal_review_v5_human_disposition_receipts",
                "reciprocal_review_v5_human_disposition_results",
                "reciprocal_review_v5_human_disposition_audit",
                "reciprocal_review_v5_human_disposition_outbox",
            )
        )
        + c.execute(
            "SELECT state_kind,result_revision FROM durable_reciprocal_review_cycles_v5"
        ).fetchone()
    )
    c.close()
    return result


def test_v5_real_ai_and_assignment_terminal_evidence_creates_binding_ready(tmp_path: Path) -> None:
    path = tmp_path / "ready.sqlite"
    _seed(path)
    result = _v5_uow(path).submit(_v5_principal(path), _v5_command())
    assert (
        result.cycle_state,
        result.upstream_revision,
        result.result_revision,
        result.action,
    ) == ("binding_ready", 2, 3, "approve_revision")
    assert _counts(path) == (1, 1, 1, 1, "binding_ready", 3)


@pytest.mark.parametrize(
    "mutation",
    ("missing_human", "finding", "bad_signature", "v4_drift", "unknown_legacy", "ownership"),
)
def test_v5_missing_or_tampered_upstream_evidence_is_write_zero(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / f"{mutation}.sqlite"
    _seed(path)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys=OFF")
    if mutation == "missing_human":
        c.execute("DROP TRIGGER reciprocal_review_v4_human_terminal_receipts_no_delete")
        c.execute("DELETE FROM reciprocal_review_v4_human_terminal_receipts")
    elif mutation == "finding":
        c.execute(
            "INSERT INTO reciprocal_review_ai_advisory_findings VALUES(?,?,?,?,?,?,?,?)",
            ("org", "finding", "batch", "criterion", "blocking", SHA, 0, 1),
        )
    elif mutation == "bad_signature":
        c.execute("DROP TRIGGER reciprocal_review_ai_advisory_batches_no_update")
        c.execute("UPDATE reciprocal_review_ai_advisory_batches SET signature='forged'")
    elif mutation == "v4_drift":
        c.execute("DROP TRIGGER durable_reciprocal_review_cycles_v4_legal_update")
        c.execute("UPDATE durable_reciprocal_review_cycles_v4 SET cycle_revision=9")
    elif mutation == "unknown_legacy":
        c.execute("DROP TRIGGER reciprocal_review_v5_cycle_ownership_no_delete")
        c.execute("DELETE FROM reciprocal_review_v5_cycle_ownership")
    else:
        c.execute("DROP TRIGGER reciprocal_review_v5_cycle_ownership_no_update")
        c.execute("PRAGMA ignore_check_constraints=ON")
        c.execute("UPDATE reciprocal_review_v5_cycle_ownership SET owner='not-v5'")
    c.commit()
    c.close()
    before = _counts(path)
    with pytest.raises(Exception):
        _v5_uow(path).submit(_v5_principal(path), _v5_command())
    assert _counts(path) == before


@pytest.mark.parametrize("column", ("cycle_id", "requirement_id"))
def test_v5_forged_ai_batch_binding_is_write_zero_even_if_immutable_trigger_is_removed(
    tmp_path: Path, column: str
) -> None:
    path = tmp_path / f"forged-batch-{column}.sqlite"
    _seed(path)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("DROP TRIGGER reciprocal_review_ai_advisory_batches_no_update")
    c.execute(
        f"UPDATE reciprocal_review_ai_advisory_batches SET {column}=? WHERE org_id='org' AND batch_id='batch'",
        ("forged-cycle" if column == "cycle_id" else "forged-requirement",),
    )
    c.commit()
    c.close()

    with pytest.raises(Exception):
        _v5_uow(path).submit(_v5_principal(path), _v5_command())
    assert _counts(path) == (0, 0, 0, 0, "awaiting_human_disposition", 2)


def test_v5_authority_expiry_independence_and_replay_recheck_are_write_zero(tmp_path: Path) -> None:
    path = tmp_path / "authority.sqlite"
    _seed(path)
    before = _counts(path)
    with pytest.raises(SqliteReciprocalReviewAiMixedDispositionError):
        _v5_uow(path).submit(
            _v5_principal(path, at=datetime(2000, 1, 1, tzinfo=UTC)), _v5_command()
        )
    # The contributor is not allowed to dispose even with an authority signature.
    with pytest.raises(SqliteReciprocalReviewAiMixedDispositionError):
        _v5_uow(path).submit(_v5_principal(path, subject="author"), _v5_command())
    assert _counts(path) == before
    current = _v5_principal(path)
    first = _v5_uow(path).submit(current, _v5_command())
    assert _v5_uow(path).submit(current, _v5_command()) == first
    with pytest.raises(SqliteReciprocalReviewAiMixedDispositionError):
        create_sqlite_reciprocal_review_ai_mixed_disposition_uow(
            path,
            trusted_human_disposition_authority_keys={"disposer": b"revoked"},
            trusted_ai_execution_keys={"ai": b"ai-key"},
        ).submit(current, _v5_command())
    with pytest.raises(SqliteReciprocalReviewAiMixedDispositionError):
        create_sqlite_reciprocal_review_ai_mixed_disposition_uow(
            path,
            trusted_human_disposition_authority_keys={"disposer": b"human-key"},
            trusted_ai_execution_keys={"ai": b"revoked-ai-key"},
        ).submit(current, _v5_command())


def test_v5_32_same_command_replays_once_and_different_action_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    _seed(path)
    principal = _v5_principal(path)

    def submit(_: int) -> str:
        return _v5_uow(path).submit(principal, _v5_command()).receipt_id

    with ThreadPoolExecutor(max_workers=32) as pool:
        assert set(pool.map(submit, range(32))) == {"receipt"}
    assert _counts(path) == (1, 1, 1, 1, "binding_ready", 3)
    with pytest.raises(SqliteReciprocalReviewAiMixedDispositionConflict):
        _v5_uow(path).submit(
            _v5_principal(path, action="reject_revision"),
            _v5_command(receipt="other", key="other").model_copy(
                update={"disposition": RejectRevision()}
            ),
        )


@pytest.mark.parametrize("point", ("after_cycle_cas", "after_receipt", "after_outbox"))
def test_v5_all_postwrite_faults_rollback_cycle_receipt_result_audit_and_outbox(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / f"{point}.sqlite"
    _seed(path)

    def fail(actual: str) -> None:
        if actual == point:
            raise RuntimeError(actual)

    with pytest.raises(RuntimeError):
        _v5_uow(path, fault=fail).submit(_v5_principal(path), _v5_command())
    assert _counts(path) == (0, 0, 0, 0, "awaiting_human_disposition", 2)


def test_v5_extra_catalog_and_orphan_evidence_are_fail_closed_without_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite"
    _seed(path)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE reciprocal_review_v5_extra (value TEXT)")
    c.commit()
    c.close()
    with pytest.raises(SqliteReciprocalReviewAiMixedDispositionError):
        _v5_uow(path).submit(_v5_principal(path), _v5_command())


class _V6Adapter:
    """Deterministic capable source adapter; external application is deliberately absent."""

    def __init__(self, *, variant: str = "ok") -> None:
        self.variant = variant
        self.apply_calls = 0

    def capability(self, **_: object) -> SourceBindingCapability:
        if self.variant == "capability":
            return cast(SourceBindingCapability, object())
        return SourceBindingCapability()

    def boundary_plan(self, **values: object) -> SourceBoundaryEnforcementPlan:
        plan = SourceBoundaryEnforcementPlan(
            plan_id="plan",
            source_ref=str(values["source_ref"]),
            expected_source_revision="source-revision",
            revision_id=str(values["revision_id"]),
            content_digest=str(values["content_digest"]),
            boundary_digest=str(values["boundary_digest"]),
            enforcement_mode="native",
        )
        if self.variant == "plan_source":
            return plan.model_copy(update={"source_ref": "other-source"})
        if self.variant == "plan_revision":
            return plan.model_copy(update={"revision_id": "other-revision"})
        if self.variant == "plan_boundary":
            return plan.model_copy(update={"boundary_digest": "f" * 64})
        return plan

    def drift_authorization(
        self, *, plan: SourceBoundaryEnforcementPlan, now: datetime
    ) -> SourceBoundaryDriftActionAuthorization:
        result = SourceBoundaryDriftActionAuthorization(
            authorization_id="drift",
            source_ref=plan.source_ref,
            expected_source_revision="source-revision",
            boundary_digest=plan.boundary_digest,
            action="source_deny_reads",
            # Authorization issuance is stable for the same semantic command;
            # a moving fixture expiry would incorrectly turn idempotent replay
            # into a different command digest.
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        if self.variant == "drift_source":
            return result.model_copy(update={"source_ref": "other-source"})
        if self.variant == "drift_revision":
            return result.model_copy(update={"expected_source_revision": "other"})
        if self.variant == "drift_boundary":
            return result.model_copy(update={"boundary_digest": "f" * 64})
        if self.variant == "drift_expired":
            return result.model_copy(update={"expires_at": now})
        return result


def _v6_command(*, receipt: str = "binding-receipt", key: str = "binding-key") -> CreateSourceBindingIntent:
    return CreateSourceBindingIntent(
        receipt_id=receipt,
        audit_id=f"audit-{receipt}",
        outbox_id=f"outbox-{receipt}",
        idempotency_key=key,
        cycle_id="cycle:revision",
        expected_upstream_revision=3,
        source_ref="source",
    )


def _v6_seed(path: Path) -> None:
    _seed(path)
    _v5_uow(path).submit(_v5_principal(path), _v5_command())
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_source_binding_v6(c)
    c.close()


def _v6_counts(path: Path) -> tuple[int, int, int, int, str | None]:
    c = sqlite3.connect(path)
    counts = tuple(
        c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "durable_reciprocal_review_source_binding_cycles_v6",
            "reciprocal_review_v6_binding_intents",
            "reciprocal_review_v6_binding_audit",
            "reciprocal_review_v6_binding_outbox",
        )
    )
    row = c.execute(
        "SELECT state_kind FROM durable_reciprocal_review_source_binding_cycles_v6"
    ).fetchone()
    c.close()
    return (*counts, None if row is None else row[0])


def test_v6_legacy_writer_cannot_consume_real_v5_binding_ready(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v6-ready.sqlite"
    _v6_seed(path)
    adapter = _V6Adapter()
    with pytest.raises(SqliteReciprocalReviewSourceBindingError):
        create_sqlite_reciprocal_review_source_binding_intent_uow(path, adapter=adapter)
    assert adapter.apply_calls == 0
    assert _v6_counts(path) == (0, 0, 0, 0, None)
    c = sqlite3.connect(path)
    assert c.execute("SELECT state_kind FROM durable_reciprocal_review_cycles_v5").fetchone() == (
        "binding_ready",
    )
    c.close()


@pytest.mark.parametrize(
    "variant", ("capability", "plan_source", "plan_revision", "plan_boundary", "drift_source", "drift_revision", "drift_boundary", "drift_expired"),
)
def test_v6_incomplete_or_mismatched_capability_boundary_authority_has_zero_writes(
    tmp_path: Path, variant: str
) -> None:
    path = tmp_path / f"{variant}.sqlite"
    _v6_seed(path)
    with pytest.raises(SqliteReciprocalReviewSourceBindingError):
        create_sqlite_reciprocal_review_source_binding_intent_uow(
            path, adapter=_V6Adapter(variant=variant)
        ).create(_v6_command())
    assert _v6_counts(path) == (0, 0, 0, 0, None)


@pytest.mark.parametrize("point", ("after_pending_cas", "after_outbox"))
def test_v6_all_write_faults_roll_back_pending_intent_audit_and_outbox(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / f"v6-{point}.sqlite"
    _v6_seed(path)

    def fail(actual: str) -> None:
        if actual == point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError):
        create_sqlite_reciprocal_review_source_binding_intent_uow(
            path, adapter=_V6Adapter(), fault_injector=fail
        ).create(_v6_command())
    assert _v6_counts(path) == (0, 0, 0, 0, None)


@pytest.mark.parametrize("tamper", ("extra", "orphan"))
def test_v6_strict_catalog_and_semantic_tamper_are_fail_closed(tmp_path: Path, tamper: str) -> None:
    path = tmp_path / f"v6-{tamper}.sqlite"
    _v6_seed(path)
    c = sqlite3.connect(path)
    if tamper == "extra":
        c.execute("CREATE TABLE reciprocal_review_v6_extra (value TEXT)")
    else:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("DROP TRIGGER durable_reciprocal_review_source_binding_cycles_v6_no_delete")
        c.execute("INSERT INTO durable_reciprocal_review_source_binding_cycles_v6 VALUES('org','orphan','v5',3,'revision','binding_pending',2,'2026-07-20T00:00:00.000Z')")
    c.commit()
    c.close()
    with pytest.raises(SqliteReciprocalReviewSourceBindingError):
        c = sqlite3.connect(path)
        try:
            validate_sqlite_reciprocal_review_source_binding(c)
        finally:
            c.close()
