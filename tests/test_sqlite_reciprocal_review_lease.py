from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Mapping, cast

import pytest

from agent_org_network.reciprocal_review import (
    AiAdvisoryFindingBatch,
    AiReviewerPrincipal,
    HumanPrincipal,
    RecordAiAdvisoryBatch,
)
from agent_org_network.sqlite_durable_reciprocal_review import (
    migrate_sqlite_durable_reciprocal_review_ledger,
)
from agent_org_network.sqlite_reciprocal_review_lease import (
    AiReviewerAssignment,
    ClaimReviewRun,
    HumanReviewerAssignment,
    RenewReviewLease,
    SqliteReciprocalReviewLeaseConflict,
    SqliteReciprocalReviewLeaseError,
    create_sqlite_reciprocal_review_lease_uow,
    migrate_sqlite_reciprocal_review_lease,
    validate_active_review_lease_proof,
)
from agent_org_network.sqlite_reciprocal_review_ai_batches import (
    create_sqlite_reciprocal_review_ai_batch_uow,
    migrate_sqlite_reciprocal_review_ai_batches,
)


NOW = datetime(2026, 7, 20, tzinfo=UTC)
SHA = "a" * 64


class _Authorizer:
    def authorize_human_reviewer(
        self, *, reviewer: HumanPrincipal, contributor_subject_ids: tuple[str, ...]
    ) -> bool:
        return reviewer.subject_id not in contributor_subject_ids

    def authorize_ai_reviewer(self, *, reviewer: AiReviewerPrincipal) -> bool:
        return reviewer.reviewer_id == "ai-reviewer"


class _Policy:
    def requirement_is_current(
        self,
        *,
        org_id: str,
        cycle_id: str,
        requirement_id: str,
        policy_digest: str,
        provenance_digest: str,
    ) -> bool:
        return (org_id, cycle_id, policy_digest, provenance_digest) == (
            "org",
            "cycle",
            "c" * 64,
            "b" * 64,
        ) and requirement_id in {"req", "req-2"}


class _BatchPolicy:
    def batch_is_current(self, **kwargs: object) -> bool:
        return kwargs["content_digest"] == SHA and kwargs["model_execution_ref"] == "execution"

    def permits_awaiting_human_disposition(self, **kwargs: object) -> bool:
        return True


def _seed(path: Path, *, human_contributor: str = "author", ai_requirement: bool = False) -> None:
    c = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(c)
    lineage = hashlib.sha256(b'["event"]').hexdigest()
    c.execute(
        "INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "artifact",
            "revision",
            1,
            None,
            "git:commit",
            SHA,
            "internal",
            "boundary",
            "human",
            "b" * 64,
            lineage,
            "e" * 64,
            None,
            1,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)",
        (
            "org",
            "event",
            "revision",
            "human",
            human_contributor,
            SHA,
            None,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
        ("org", "revision", "event", 1),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_cycles VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "cycle",
            "revision",
            1,
            "review_open",
            1,
            "b" * 64,
            "c" * 64,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_requirements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "req",
            "cycle",
            "ai" if ai_requirement else "human",
            "all",
            1,
            "independent",
            "rubric",
            "standard",
            "f" * 64,
            "2026-07-21T00:00:00.000Z",
            0,
        ),
    )
    migrate_sqlite_reciprocal_review_lease(c)
    c.commit()
    c.close()


def _human() -> HumanPrincipal:
    return HumanPrincipal(
        org_id="org", subject_id="reviewer", authenticated_at=NOW, authn_context_digest=SHA
    )


def _human_in(org_id: str, subject_id: str = "reviewer") -> HumanPrincipal:
    return HumanPrincipal(
        org_id=org_id, subject_id=subject_id, authenticated_at=NOW, authn_context_digest=SHA
    )


def _ai() -> AiReviewerPrincipal:
    return AiReviewerPrincipal(
        org_id="org",
        reviewer_id="ai-reviewer",
        model_execution_ref="execution",
        deployment_digest=SHA,
        rubric_digest=SHA,
    )


def _uow(path: Path, now: list[datetime]):
    return create_sqlite_reciprocal_review_lease_uow(
        path,
        reviewer_authorization=_Authorizer(),
        policy_snapshot=_Policy(),
        db_time=lambda connection: now[0],
        token_key=b"test-key",
    )


def _receipt_evidence(
    *,
    transition_type: str,
    prior_epoch: int,
    lease_epoch: int,
    prior_state: str,
    expected_token_hash: str | None,
    expected_expires_at: str | None,
    new_token_hash: str | None,
    expires_at: str | None,
    db_time: str,
    predecessor: str | None,
) -> str:
    value = (
        "reciprocal-review-lease-transition-v2",
        transition_type,
        "run",
        prior_epoch,
        lease_epoch,
        prior_state,
        expected_token_hash,
        new_token_hash,
        expected_expires_at,
        expires_at,
        db_time,
        predecessor,
    )
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _record_evidence(evidence: str, transition_type: str, table: str) -> str:
    action = "audit" if table.endswith("audit") else "outbox"
    value = ("reciprocal-review-lease-evidence-v1", action, transition_type, evidence)
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def test_assign_claim_renew_and_reclaim_never_persist_or_emit_token(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    uow = _uow(path, now)
    assigned = uow.assign(
        HumanReviewerAssignment(
            receipt_id="assign",
            audit_id="audit",
            outbox_id="outbox",
            review_run_id="run",
            cycle_id="cycle",
            requirement_id="req",
            reviewer=_human(),
        )
    )
    assert assigned.review_run_id == "run"
    first = uow.claim(
        ClaimReviewRun(
            receipt_id="claim",
            audit_id="claim-audit",
            outbox_id="claim-outbox",
            review_run_id="run",
            reviewer=_human(),
            lease_for=timedelta(minutes=5),
        )
    )
    assert first.lease_token and first.lease_epoch == 1
    renewed = uow.renew(
        RenewReviewLease(
            receipt_id="renew",
            audit_id="renew-audit",
            outbox_id="renew-outbox",
            review_run_id="run",
            reviewer=_human(),
            lease_epoch=1,
            lease_token=first.lease_token,
            lease_for=timedelta(minutes=5),
        )
    )
    assert renewed.lease_token is None and renewed.lease_epoch == 1
    now[0] += timedelta(minutes=6)
    reclaimed = uow.claim(
        ClaimReviewRun(
            receipt_id="reclaim",
            audit_id="reclaim-audit",
            outbox_id="reclaim-outbox",
            review_run_id="run",
            reviewer=_human(),
            lease_for=timedelta(minutes=5),
        )
    )
    assert reclaimed.lease_epoch == 2 and reclaimed.lease_token
    connection = sqlite3.connect(path)
    text = " ".join(str(row) for row in connection.execute("SELECT sql FROM sqlite_master")) + " "
    for table in (
        "durable_reciprocal_review_runs",
        "reciprocal_review_lease_reviewer_assignments",
        "reciprocal_review_lease_state",
        "reciprocal_review_lease_tombstones",
        "reciprocal_review_lease_receipts",
        "reciprocal_review_lease_audit",
        "reciprocal_review_lease_outbox",
    ):
        text += " " + " ".join(str(row) for row in connection.execute(f"SELECT * FROM {table}"))
    connection.close()
    assert first.lease_token not in text and reclaimed.lease_token not in text
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.renew(
            RenewReviewLease(
                receipt_id="old",
                audit_id="old-a",
                outbox_id="old-o",
                review_run_id="run",
                reviewer=_human(),
                lease_epoch=1,
                lease_token=first.lease_token,
                lease_for=timedelta(minutes=5),
            )
        )


def test_ai_signed_empty_batch_is_terminal_projection_and_never_persists_token(
    tmp_path: Path,
) -> None:
    path = tmp_path / "batch.sqlite"
    _now, lease, command = _ai_batch_command(path)
    result = create_sqlite_reciprocal_review_ai_batch_uow(
        path,
        lease_uow=lease,
        policy_snapshot=_BatchPolicy(),
        trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
    ).record(command)
    assert (result.batch_id, result.unmet_requirements, result.next_cycle_state) == (
        "batch",
        0,
        "awaiting_human_disposition",
    )
    c = sqlite3.connect(path)
    assert c.execute("SELECT count(*) FROM reciprocal_review_ai_advisory_batches").fetchone() == (
        1,
    )
    assert c.execute("SELECT count(*) FROM reciprocal_review_ai_advisory_findings").fetchone() == (
        0,
    )
    for table in (
        "reciprocal_review_ai_batch_receipts",
        "reciprocal_review_ai_batch_audit",
        "reciprocal_review_ai_batch_outbox",
        "reciprocal_review_ai_batch_terminal_projections",
    ):
        assert c.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)
    assert c.execute(
        "SELECT state_kind FROM durable_reciprocal_review_cycles WHERE cycle_id='cycle'"
    ).fetchone() == ("awaiting_human_disposition",)
    c.close()


def test_ai_batch_same_command_32_way_race_has_one_batch_and_idempotent_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "batch-race.sqlite"
    _now, lease, command = _ai_batch_command(path)
    uow = create_sqlite_reciprocal_review_ai_batch_uow(
        path,
        lease_uow=lease,
        policy_snapshot=_BatchPolicy(),
        trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
    )

    def record_once(_: int):
        return uow.record(command)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(record_once, range(32)))
    assert {result.batch_id for result in results} == {"batch"}
    c = sqlite3.connect(path)
    assert c.execute("SELECT count(*) FROM reciprocal_review_ai_advisory_batches").fetchone() == (
        1,
    )
    assert c.execute(
        "SELECT state_kind FROM durable_reciprocal_review_cycles WHERE cycle_id='cycle'"
    ).fetchone() == ("awaiting_human_disposition",)
    c.close()


@pytest.mark.parametrize(
    "column,value",
    (
        ("unmet_requirements", "1"),
        ("next_cycle_state", "'review_open'"),
        ("projection_digest", "'" + "0" * 64 + "'"),
        ("created_at", "'bad'"),
    ),
)
def test_ai_batch_forged_projection_field_fails_closed_before_next_write(
    tmp_path: Path, column: str, value: str
) -> None:
    path = tmp_path / f"projection-{column}.sqlite"
    _now, lease, command = _ai_batch_command(path)
    uow = create_sqlite_reciprocal_review_ai_batch_uow(
        path,
        lease_uow=lease,
        policy_snapshot=_BatchPolicy(),
        trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
    )
    uow.record(command)
    c = sqlite3.connect(path)
    c.execute("DROP TRIGGER reciprocal_review_ai_batch_terminal_projections_no_update")
    c.execute(f"UPDATE reciprocal_review_ai_batch_terminal_projections SET {column}={value}")
    c.commit()
    c.close()
    with pytest.raises(Exception):
        uow.record(command)


def test_ai_batch_factory_rejects_runtime_always_true_verifier_injection(tmp_path: Path) -> None:
    class AlwaysTrue:
        def verify(self, **kwargs: object) -> bool:
            return True

    path = tmp_path / "always-true.sqlite"
    _now, lease, _command = _ai_batch_command(path)
    with pytest.raises((AttributeError, ValueError, TypeError)):
        create_sqlite_reciprocal_review_ai_batch_uow(
            path,
            lease_uow=lease,
            policy_snapshot=_BatchPolicy(),
            trusted_execution_keys=cast(Mapping[str, bytes], AlwaysTrue()),
        )


def test_ai_recorded_run_blocks_claim_and_reclaim_after_authoritative_time(tmp_path: Path) -> None:
    path = tmp_path / "recorded-run.sqlite"
    _now, lease, command = _ai_batch_command(path)
    uow = create_sqlite_reciprocal_review_ai_batch_uow(
        path,
        lease_uow=lease,
        policy_snapshot=_BatchPolicy(),
        trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
    )
    uow.record(command)
    c = sqlite3.connect(path)
    assert c.execute(
        "SELECT state FROM durable_reciprocal_review_runs WHERE review_run_id='run'"
    ).fetchone() == ("recorded",)
    c.close()


def test_pre_record_lease_proof_cannot_be_reused_after_terminal_record(tmp_path: Path) -> None:
    path = tmp_path / "old-proof.sqlite"
    now, lease, command = _ai_batch_command(path)
    proof = lease.validate_active_lease(
        reviewer=_ai(),
        review_run_id="run",
        lease_epoch=command.lease_epoch,
        lease_token=command.lease_token,
    )
    create_sqlite_reciprocal_review_ai_batch_uow(
        path,
        lease_uow=lease,
        policy_snapshot=_BatchPolicy(),
        trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
    ).record(command)
    c = sqlite3.connect(path)
    before = str(c.execute("SELECT * FROM reciprocal_review_lease_state").fetchall())
    evidence_before = str(c.execute("SELECT * FROM reciprocal_review_lease_receipts").fetchall())
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        validate_active_review_lease_proof(c, proof, now=now[0])
    c.rollback()
    c.close()
    now[0] += timedelta(minutes=6)
    for receipt in ("late-claim", "late-reclaim"):
        with pytest.raises(SqliteReciprocalReviewLeaseConflict):
            lease.claim(
                ClaimReviewRun(
                    receipt,
                    f"{receipt}-audit",
                    f"{receipt}-outbox",
                    "run",
                    _ai(),
                    timedelta(minutes=1),
                )
            )
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        lease.renew(
            RenewReviewLease(
                "late-renew",
                "late-renew-audit",
                "late-renew-outbox",
                "run",
                _ai(),
                command.lease_epoch,
                command.lease_token,
                timedelta(minutes=1),
            )
        )
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        lease.validate_active_lease(
            reviewer=_ai(),
            review_run_id="run",
            lease_epoch=command.lease_epoch,
            lease_token=command.lease_token,
        )
    c = sqlite3.connect(path)
    assert str(c.execute("SELECT * FROM reciprocal_review_lease_state").fetchall()) == before
    assert (
        str(c.execute("SELECT * FROM reciprocal_review_lease_receipts").fetchall())
        == evidence_before
    )
    assert c.execute(
        "SELECT state_kind FROM durable_reciprocal_review_cycles WHERE cycle_id='cycle'"
    ).fetchone() == ("awaiting_human_disposition",)
    c.close()


def test_ai_batch_before_run_recorded_fault_rolls_everything_back(tmp_path: Path) -> None:
    path = tmp_path / "recorded-fault.sqlite"
    _now, lease, command = _ai_batch_command(path)

    def fail(point: str) -> None:
        if point == "before_run_recorded":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match="before_run_recorded"):
        create_sqlite_reciprocal_review_ai_batch_uow(
            path,
            lease_uow=lease,
            policy_snapshot=_BatchPolicy(),
            trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
            fault_injector=fail,
        ).record(command)
    _assert_no_ai_batch_residue(path)
    c = sqlite3.connect(path)
    assert c.execute(
        "SELECT state FROM durable_reciprocal_review_runs WHERE review_run_id='run'"
    ).fetchone() == ("leased",)
    c.close()


def _ai_batch_command(path: Path):
    _seed(path, ai_requirement=True)
    now = [NOW]
    lease = _uow(path, now)
    lease.assign(AiReviewerAssignment("assign", "audit", "outbox", "run", "cycle", "req", _ai()))
    claimed = lease.claim(
        ClaimReviewRun("claim", "claim-audit", "claim-outbox", "run", _ai(), timedelta(minutes=5))
    )
    assert claimed.lease_token is not None
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_ai_batches(c)
    c.close()
    signed = hashlib.sha256(
        json.dumps(
            {
                "org_id": "org",
                "batch_id": "batch",
                "review_run_id": "run",
                "model_execution_ref": "execution",
                "rubric_digest": SHA,
                "prompt_digest": "b" * 64,
                "input_digest": SHA,
                "findings": [],
                "cycle_id": "cycle",
                "requirement_id": "req",
                "policy_digest": "c" * 64,
                "provenance_digest": "b" * 64,
                "content_digest": SHA,
                "deployment_digest": SHA,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    signature = hmac.new(b"test-signing-key", signed.encode(), hashlib.sha256).hexdigest()
    command = RecordAiAdvisoryBatch(
        receipt_id="batch-receipt",
        audit_id="batch-audit",
        outbox_id="batch-outbox",
        principal=_ai(),
        lease_epoch=claimed.lease_epoch,
        lease_token=claimed.lease_token,
        batch=AiAdvisoryFindingBatch(
            batch_id="batch",
            org_id="org",
            review_run_id="run",
            model_execution_ref="execution",
            prompt_digest="b" * 64,
            rubric_digest=SHA,
            input_digest=SHA,
            signature=signature,
            signature_algorithm="hmac-sha256",
            signed_payload_digest=signed,
            findings=(),
            created_at=NOW,
        ),
    )
    return now, lease, command


def _assert_no_ai_batch_residue(path: Path) -> None:
    c = sqlite3.connect(path)
    for table in (
        "reciprocal_review_ai_advisory_batches",
        "reciprocal_review_ai_advisory_findings",
        "reciprocal_review_ai_batch_receipts",
        "reciprocal_review_ai_batch_audit",
        "reciprocal_review_ai_batch_outbox",
        "reciprocal_review_ai_batch_terminal_projections",
    ):
        assert c.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert c.execute(
        "SELECT state_kind FROM durable_reciprocal_review_cycles WHERE cycle_id='cycle'"
    ).fetchone() == ("review_open",)
    c.close()


def test_ai_batch_expiry_renew_reclaim_and_signature_rejection_leave_zero_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "batch-stale.sqlite"
    now, lease, command = _ai_batch_command(path)
    now[0] += timedelta(minutes=6)
    uow = create_sqlite_reciprocal_review_ai_batch_uow(
        path,
        lease_uow=lease,
        policy_snapshot=_BatchPolicy(),
        trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
    )
    with pytest.raises(Exception):
        uow.record(command)
    _assert_no_ai_batch_residue(path)
    # A verifier rejection (including a forged signature) is equally write-zero.
    path2 = tmp_path / "batch-signature.sqlite"
    _now2, lease2, command2 = _ai_batch_command(path2)
    with pytest.raises(Exception):
        create_sqlite_reciprocal_review_ai_batch_uow(
            path2,
            lease_uow=lease2,
            policy_snapshot=_BatchPolicy(),
            trusted_execution_keys={"other-key": b"test-signing-key"},
        ).record(command2)
    _assert_no_ai_batch_residue(path2)


def test_ai_batch_old_token_or_epoch_after_renew_and_reclaim_is_write_zero(tmp_path: Path) -> None:
    path = tmp_path / "batch-renew-reclaim.sqlite"
    now, lease, command = _ai_batch_command(path)
    lease.renew(
        RenewReviewLease(
            "renew",
            "renew-audit",
            "renew-outbox",
            "run",
            _ai(),
            command.lease_epoch,
            command.lease_token,
            timedelta(minutes=5),
        )
    )
    stale_token = command.model_copy(update={"lease_token": "stale-token"})
    with pytest.raises(Exception):
        create_sqlite_reciprocal_review_ai_batch_uow(
            path,
            lease_uow=lease,
            policy_snapshot=_BatchPolicy(),
            trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
        ).record(stale_token)
    _assert_no_ai_batch_residue(path)
    now[0] += timedelta(minutes=6)
    reclaimed = lease.claim(
        ClaimReviewRun(
            "reclaim", "reclaim-audit", "reclaim-outbox", "run", _ai(), timedelta(minutes=5)
        )
    )
    assert reclaimed.lease_epoch == command.lease_epoch + 1
    with pytest.raises(Exception):
        create_sqlite_reciprocal_review_ai_batch_uow(
            path,
            lease_uow=lease,
            policy_snapshot=_BatchPolicy(),
            trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
        ).record(command)
    _assert_no_ai_batch_residue(path)


@pytest.mark.parametrize("point", ("after_findings", "after_outbox"))
def test_ai_batch_faults_rollback_every_stage_and_cycle(tmp_path: Path, point: str) -> None:
    path = tmp_path / f"batch-{point}.sqlite"
    _now, lease, command = _ai_batch_command(path)

    def fail(actual: str) -> None:
        if actual == point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=point):
        create_sqlite_reciprocal_review_ai_batch_uow(
            path,
            lease_uow=lease,
            policy_snapshot=_BatchPolicy(),
            trusted_execution_keys={"ai-signing-key": b"test-signing-key"},
            fault_injector=fail,
        ).record(command)
    _assert_no_ai_batch_residue(path)


def test_self_review_and_policy_drift_write_nothing(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path, human_contributor="reviewer")
    now = [NOW]
    uow = _uow(path, now)
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.assign(
            HumanReviewerAssignment(
                receipt_id="assign",
                audit_id="audit",
                outbox_id="outbox",
                review_run_id="run",
                cycle_id="cycle",
                requirement_id="req",
                reviewer=_human(),
            )
        )
    assert sqlite3.connect(path).execute(
        "SELECT count(*) FROM reciprocal_review_lease_state"
    ).fetchone() == (0,)


def test_32_way_expired_reclaim_has_one_epoch_winner(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    uow = _uow(path, now)
    uow.assign(
        HumanReviewerAssignment("assign", "audit", "outbox", "run", "cycle", "req", _human())
    )
    uow.claim(ClaimReviewRun("claim", "a", "o", "run", _human(), timedelta(minutes=1)))
    now[0] += timedelta(minutes=2)

    def reclaim(index: int) -> int | None:
        try:
            command = ClaimReviewRun(
                f"reclaim-{index}",
                f"a-{index}",
                f"o-{index}",
                "run",
                _human(),
                timedelta(minutes=1),
            )
            return _uow(path, now).claim(command).lease_epoch
        except SqliteReciprocalReviewLeaseConflict:
            return None

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(reclaim, range(32)))
    assert results.count(2) == 1
    assert sqlite3.connect(path).execute(
        "SELECT count(*) FROM reciprocal_review_lease_tombstones"
    ).fetchone() == (1,)


def test_wrong_old_token_and_epoch_after_reclaim_leave_lease_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    uow = _uow(path, now)
    uow.assign(HumanReviewerAssignment("assign", "a", "o", "run", "cycle", "req", _human()))
    first = uow.claim(ClaimReviewRun("claim", "a1", "o1", "run", _human(), timedelta(minutes=1)))
    assert first.lease_token is not None
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.renew(
            RenewReviewLease(
                "bad", "a2", "o2", "run", _human(), 1, "wrong-token", timedelta(minutes=1)
            )
        )
    now[0] += timedelta(minutes=1)  # Exact DB-time equality is expired/reclaimable.
    second = uow.claim(ClaimReviewRun("reclaim", "a3", "o3", "run", _human(), timedelta(minutes=1)))
    assert second.lease_epoch == 2
    before = (
        sqlite3.connect(path)
        .execute("SELECT lease_epoch,token_hash,expires_at FROM reciprocal_review_lease_state")
        .fetchone()
    )
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.renew(
            RenewReviewLease(
                "old", "a4", "o4", "run", _human(), 1, first.lease_token, timedelta(minutes=1)
            )
        )
    assert (
        sqlite3.connect(path)
        .execute("SELECT lease_epoch,token_hash,expires_at FROM reciprocal_review_lease_state")
        .fetchone()
        == before
    )


def test_policy_drift_and_forged_check_bypass_row_fail_closed(tmp_path: Path) -> None:
    class DriftPolicy(_Policy):
        current = True

        def requirement_is_current(self, **kwargs: str) -> bool:
            return self.current and super().requirement_is_current(**kwargs)

    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    policy = DriftPolicy()
    uow = create_sqlite_reciprocal_review_lease_uow(
        path,
        reviewer_authorization=_Authorizer(),
        policy_snapshot=policy,
        db_time=lambda connection: now[0],
        token_key=b"test-key",
    )
    uow.assign(HumanReviewerAssignment("assign", "a", "o", "run", "cycle", "req", _human()))
    policy.current = False
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.claim(ClaimReviewRun("claim", "a1", "o1", "run", _human(), timedelta(minutes=1)))
    policy.current = True
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("UPDATE reciprocal_review_lease_state SET state='forged'")
    connection.commit()
    connection.close()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        uow.claim(ClaimReviewRun("claim", "a1", "o1", "run", _human(), timedelta(minutes=1)))


@pytest.mark.parametrize(
    "ddl",
    (
        "CREATE TABLE reciprocal_review_lease_extra(x)",
        "CREATE INDEX reciprocal_review_lease_extra_idx ON reciprocal_review_lease_state(state)",
        "CREATE TRIGGER reciprocal_review_lease_extra_trigger AFTER INSERT ON reciprocal_review_lease_state BEGIN SELECT 1; END",
    ),
)
def test_companion_extra_catalog_is_unavailable_without_repair(tmp_path: Path, ddl: str) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    connection = sqlite3.connect(path)
    connection.execute(ddl)
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()


@pytest.mark.parametrize(
    "ddl",
    (
        "DROP TABLE reciprocal_review_lease_tombstones",
        "DROP INDEX reciprocal_review_lease_state_queue_idx",
        "DROP TRIGGER reciprocal_review_lease_assignments_no_update",
    ),
)
def test_partial_companion_is_unavailable_without_repair(tmp_path: Path, ddl: str) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    connection = sqlite3.connect(path)
    connection.execute(ddl)
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()


def test_companion_schema_or_composite_fk_drift_is_unavailable_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DROP TABLE reciprocal_review_lease_state")
    connection.execute(
        "CREATE TABLE reciprocal_review_lease_state (org_id TEXT NOT NULL, review_run_id TEXT NOT NULL, owner_kind TEXT, owner_ref TEXT, lease_epoch INTEGER NOT NULL, token_hash TEXT, expires_at TEXT, state TEXT NOT NULL, forged_column TEXT, PRIMARY KEY(org_id,review_run_id), FOREIGN KEY(org_id) REFERENCES reciprocal_review_lease_reviewer_assignments(org_id))"
    )
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()


def test_same_name_trigger_tamper_and_fk_off_orphan_are_unavailable_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER reciprocal_review_lease_assignments_no_update")
    connection.execute(
        "CREATE TRIGGER reciprocal_review_lease_assignments_no_update "
        "BEFORE UPDATE ON reciprocal_review_lease_reviewer_assignments "
        "BEGIN SELECT 1; END"
    )
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()

    path = tmp_path / "orphan.sqlite"
    _seed(path)
    _uow(path, [NOW]).assign(
        HumanReviewerAssignment("assign", "a", "o", "run", "cycle", "req", _human())
    )
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("UPDATE reciprocal_review_lease_state SET review_run_id='orphan-run'")
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()


def test_catalog_drift_during_db_time_callback_rolls_back_all_lease_writes(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)

    def drifting_time(connection: sqlite3.Connection) -> datetime:
        connection.execute("CREATE TABLE durable_reciprocal_review_callback_drift(value TEXT)")
        return NOW

    uow = create_sqlite_reciprocal_review_lease_uow(
        path,
        reviewer_authorization=_Authorizer(),
        policy_snapshot=_Policy(),
        db_time=drifting_time,
        token_key=b"test-key",
    )
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        uow.assign(HumanReviewerAssignment("assign", "a", "o", "run", "cycle", "req", _human()))
    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_schema WHERE name='durable_reciprocal_review_callback_drift'"
        ).fetchone()
        is None
    )
    assert connection.execute("SELECT count(*) FROM durable_reciprocal_review_runs").fetchone() == (
        0,
    )
    assert connection.execute(
        "SELECT count(*) FROM reciprocal_review_lease_reviewer_assignments"
    ).fetchone() == (0,)
    connection.close()


def test_reviewer_kind_and_contributor_independence_are_closed_by_policy(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path, human_contributor="reviewer")
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO durable_reciprocal_review_requirements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "req-ai",
            "cycle",
            "ai",
            "all",
            1,
            "independent",
            "rubric",
            "standard",
            "f" * 64,
            "2026-07-21T00:00:00.000Z",
            0,
        ),
    )
    connection.commit()
    connection.close()

    class Policy(_Policy):
        def requirement_is_current(self, **kwargs: str) -> bool:
            return kwargs["requirement_id"] == "req-ai" or super().requirement_is_current(**kwargs)

    uow = create_sqlite_reciprocal_review_lease_uow(
        path,
        reviewer_authorization=_Authorizer(),
        policy_snapshot=Policy(),
        db_time=lambda connection: NOW,
        token_key=b"test-key",
    )
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.assign(
            HumanReviewerAssignment("human-on-ai", "a", "o", "run", "cycle", "req-ai", _human())
        )
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.assign(AiReviewerAssignment("ai-on-human", "a2", "o2", "run-2", "cycle", "req", _ai()))
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.assign(HumanReviewerAssignment("self", "a3", "o3", "run-3", "cycle", "req", _human()))


def test_cross_org_same_identifiers_cannot_claim_renew_or_obtain_token(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    uow = _uow(path, now)
    uow.assign(HumanReviewerAssignment("assign", "a", "o", "run", "cycle", "req", _human()))
    lease = uow.claim(ClaimReviewRun("claim", "a1", "o1", "run", _human(), timedelta(minutes=1)))
    assert lease.lease_token is not None
    foreign = _human_in("other-org")
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.claim(ClaimReviewRun("claim", "a1", "o1", "run", foreign, timedelta(minutes=1)))
    with pytest.raises(SqliteReciprocalReviewLeaseConflict):
        uow.renew(
            RenewReviewLease(
                "renew", "a2", "o2", "run", foreign, 1, lease.lease_token, timedelta(minutes=1)
            )
        )


def test_unrelated_runs_can_be_claimed_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO durable_reciprocal_review_requirements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "req-2",
            "cycle",
            "human",
            "all",
            1,
            "independent",
            "rubric",
            "standard",
            "f" * 64,
            "2026-07-21T00:00:00.000Z",
            0,
        ),
    )
    connection.commit()
    connection.close()
    now = [NOW]
    uow = _uow(path, now)
    reviewers = {"run-a": (_human(), "req"), "run-b": (_human_in("org", "reviewer-2"), "req-2")}
    for run, (reviewer, requirement_id) in reviewers.items():
        uow.assign(
            HumanReviewerAssignment(
                f"assign-{run}", f"a-{run}", f"o-{run}", run, "cycle", requirement_id, reviewer
            )
        )

    def claim(run: str) -> int:
        return (
            _uow(path, now)
            .claim(
                ClaimReviewRun(
                    f"claim-{run}",
                    f"ca-{run}",
                    f"co-{run}",
                    run,
                    reviewers[run][0],
                    timedelta(minutes=1),
                )
            )
            .lease_epoch
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(claim, ("run-a", "run-b"))) == [1, 1]


@pytest.mark.parametrize("point", ("after_claim_cas", "after_renew_cas", "after_tombstone"))
def test_claim_renew_reclaim_faults_roll_back_state_and_tombstone(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    stable = _uow(path, now)
    stable.assign(HumanReviewerAssignment("assign", "a", "o", "run", "cycle", "req", _human()))
    first = stable.claim(ClaimReviewRun("claim", "a1", "o1", "run", _human(), timedelta(minutes=1)))
    assert first.lease_token is not None
    if point == "after_claim_cas":
        path = tmp_path / "claim.sqlite"
        _seed(path)
        stable = _uow(path, now)
        stable.assign(
            HumanReviewerAssignment("assign2", "a2", "o2", "run", "cycle", "req", _human())
        )
    elif point == "after_tombstone":
        now[0] += timedelta(minutes=1)
    failing = create_sqlite_reciprocal_review_lease_uow(
        path,
        reviewer_authorization=_Authorizer(),
        policy_snapshot=_Policy(),
        db_time=lambda connection: now[0],
        token_key=b"test-key",
        fault_injector=lambda actual: (
            (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
        ),
    )
    with pytest.raises(RuntimeError, match=point):
        if point == "after_renew_cas":
            failing.renew(
                RenewReviewLease(
                    "renew", "ra", "ro", "run", _human(), 1, first.lease_token, timedelta(minutes=1)
                )
            )
        else:
            failing.claim(
                ClaimReviewRun("reclaim", "ca", "co", "run", _human(), timedelta(minutes=1))
            )


@pytest.mark.parametrize(
    "point",
    (
        "after_run",
        "after_assignment",
        "after_lease_state",
        "after_operation",
        "after_audit",
        "after_outbox",
        "after_receipt",
    ),
)
def test_assignment_write_fault_rolls_back_all_companion_records(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    failing = create_sqlite_reciprocal_review_lease_uow(
        path,
        reviewer_authorization=_Authorizer(),
        policy_snapshot=_Policy(),
        db_time=lambda connection: now[0],
        token_key=b"test-key",
        fault_injector=lambda actual: (
            (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
        ),
    )
    with pytest.raises(RuntimeError, match=point):
        failing.assign(HumanReviewerAssignment("assign", "a", "o", "run", "cycle", "req", _human()))
    connection = sqlite3.connect(path)
    assert [
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        for table in (
            "durable_reciprocal_review_runs",
            "reciprocal_review_lease_reviewer_assignments",
            "reciprocal_review_lease_state",
            "reciprocal_review_lease_receipts",
            "reciprocal_review_lease_audit",
            "reciprocal_review_lease_outbox",
        )
    ] == [(0,), (0,), (0,), (0,), (0,), (0,)]
    connection.close()


@pytest.mark.parametrize(
    ("table", "identifier", "replacement"),
    (
        ("reciprocal_review_lease_tombstones", "lease_epoch", "99"),
        ("reciprocal_review_lease_receipts", "command_digest", "'f' || substr(command_digest, 2)"),
        ("reciprocal_review_lease_audit", "event_digest", "'f' || substr(event_digest, 2)"),
        ("reciprocal_review_lease_outbox", "payload_digest", "'f' || substr(payload_digest, 2)"),
    ),
)
def test_immutable_evidence_direct_mutation_or_delete_cannot_be_repaired(
    tmp_path: Path, table: str, identifier: str, replacement: str
) -> None:
    """Every persisted lease evidence row is append-only and validator-visible."""
    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    uow = _uow(path, now)
    uow.assign(HumanReviewerAssignment("assign", "aa", "ao", "run", "cycle", "req", _human()))
    first = uow.claim(ClaimReviewRun("claim", "ca", "co", "run", _human(), timedelta(minutes=1)))
    now[0] += timedelta(minutes=1)
    uow.claim(ClaimReviewRun("reclaim", "ra", "ro", "run", _human(), timedelta(minutes=1)))

    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f"UPDATE {table} SET {identifier}={replacement}")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f"DELETE FROM {table}")
    trigger_prefix = table.removeprefix("reciprocal_review_lease_")
    for operation in ("no_update", "no_delete"):
        connection.execute(f"DROP TRIGGER reciprocal_review_lease_{trigger_prefix}_{operation}")
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(f"UPDATE {table} SET {identifier}={replacement}")
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        _uow(path, now).claim(
            ClaimReviewRun("after-forgery", "fa", "fo", "run", _human(), timedelta(minutes=1))
        )
    assert first.lease_token is not None


def test_evidence_graph_requires_exact_receipt_audit_outbox_and_complete_reclaim_epochs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    uow = _uow(path, now)
    uow.assign(HumanReviewerAssignment("assign", "aa", "ao", "run", "cycle", "req", _human()))
    uow.claim(ClaimReviewRun("claim", "ca", "co", "run", _human(), timedelta(minutes=1)))
    now[0] += timedelta(minutes=1)
    uow.claim(ClaimReviewRun("reclaim", "ra", "ro", "run", _human(), timedelta(minutes=1)))

    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER reciprocal_review_lease_audit_no_delete")
    connection.execute("DELETE FROM reciprocal_review_lease_audit WHERE audit_id='ca'")
    connection.execute(
        "CREATE TRIGGER reciprocal_review_lease_audit_no_delete "
        "BEFORE DELETE ON reciprocal_review_lease_audit "
        "BEGIN SELECT RAISE(ABORT, 'immutable review lease audit'); END"
    )
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()


def test_direct_complete_looking_receipt_audit_outbox_is_not_transition_evidence(
    tmp_path: Path,
) -> None:
    """A syntactically complete triple cannot invent a lease transition."""
    path = tmp_path / "lease.sqlite"
    _seed(path)
    _uow(path, [NOW]).assign(
        HumanReviewerAssignment("assign", "aa", "ao", "run", "cycle", "req", _human())
    )
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER reciprocal_review_lease_receipts_no_update")
    connection.execute(
        "CREATE TRIGGER reciprocal_review_lease_receipts_no_update "
        "BEFORE UPDATE ON reciprocal_review_lease_receipts "
        "BEGIN SELECT RAISE(ABORT, 'immutable review lease receipt'); END"
    )
    connection.execute(
        "INSERT INTO reciprocal_review_lease_receipts "
        "SELECT org_id,'forged','forged-audit','forged-outbox',command_digest,review_run_id,"
        "lease_epoch,transition_type,prior_epoch,prior_state,expected_token_hash,expected_expires_at,"
        "new_token_hash,expires_at,db_time,predecessor_transition_digest,"
        "evidence_digest,created_at "
        "FROM reciprocal_review_lease_receipts WHERE receipt_id='assign'"
    )
    connection.execute(
        "INSERT INTO reciprocal_review_lease_audit "
        "SELECT org_id,'forged-audit','forged',command_digest,review_run_id,lease_epoch,event_digest,created_at "
        "FROM reciprocal_review_lease_audit WHERE receipt_id='assign'"
    )
    connection.execute(
        "INSERT INTO reciprocal_review_lease_outbox "
        "SELECT org_id,'forged-outbox','forged',command_digest,review_run_id,lease_epoch,payload_digest,created_at "
        "FROM reciprocal_review_lease_outbox WHERE receipt_id='assign'"
    )
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()


def test_nonterminal_self_consistent_phantom_claim_receipt_makes_component_unavailable(
    tmp_path: Path,
) -> None:
    """A receipt chain must have exactly one head: the current state anchor."""
    path = tmp_path / "lease.sqlite"
    _seed(path)
    _uow(path, [NOW]).assign(
        HumanReviewerAssignment("assign", "aa", "ao", "run", "cycle", "req", _human())
    )
    connection = sqlite3.connect(path)
    assign = connection.execute(
        "SELECT command_digest,evidence_digest,created_at FROM reciprocal_review_lease_receipts "
        "WHERE receipt_id='assign'"
    ).fetchone()
    assert assign is not None
    command_digest, predecessor, created_at = assign
    token_hash = "d" * 64
    expires_at = "2026-07-20T00:05:00.000Z"
    evidence = _receipt_evidence(
        transition_type="claim",
        prior_epoch=1,
        lease_epoch=1,
        prior_state="queued",
        expected_token_hash=None,
        expected_expires_at=None,
        new_token_hash=token_hash,
        expires_at=expires_at,
        db_time=created_at,
        predecessor=predecessor,
    )
    connection.execute(
        "INSERT INTO reciprocal_review_lease_receipts "
        "(org_id,receipt_id,audit_id,outbox_id,command_digest,review_run_id,lease_epoch,"
        "transition_type,prior_epoch,prior_state,expected_token_hash,expected_expires_at,"
        "new_token_hash,expires_at,db_time,predecessor_transition_digest,evidence_digest,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "phantom",
            "phantom-audit",
            "phantom-outbox",
            command_digest,
            "run",
            1,
            "claim",
            1,
            "queued",
            None,
            None,
            token_hash,
            expires_at,
            created_at,
            predecessor,
            evidence,
            created_at,
        ),
    )
    for table, record_id in (
        ("reciprocal_review_lease_audit", "phantom-audit"),
        ("reciprocal_review_lease_outbox", "phantom-outbox"),
    ):
        connection.execute(
            f"INSERT INTO {table} "
            f"SELECT org_id,?, 'phantom', command_digest, review_run_id, lease_epoch, ?, created_at "
            "FROM reciprocal_review_lease_receipts WHERE receipt_id='phantom'",
            (record_id, _record_evidence(evidence, "claim", table)),
        )
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()


def test_v1_component_is_unavailable_without_receipt_chain_backfill(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("UPDATE reciprocal_review_lease_manifest SET version=1")
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()


def test_reclaim_tombstone_token_hash_is_bound_to_the_expired_lease(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lease.sqlite"
    _seed(path)
    now = [NOW]
    uow = _uow(path, now)
    uow.assign(HumanReviewerAssignment("assign", "aa", "ao", "run", "cycle", "req", _human()))
    uow.claim(ClaimReviewRun("claim", "ca", "co", "run", _human(), timedelta(minutes=1)))
    now[0] += timedelta(minutes=1)
    uow.claim(ClaimReviewRun("reclaim", "ra", "ro", "run", _human(), timedelta(minutes=1)))

    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER reciprocal_review_lease_tombstones_no_update")
    connection.execute(
        "UPDATE reciprocal_review_lease_tombstones SET token_hash=?",
        ("f" * 64,),
    )
    connection.execute(
        "CREATE TRIGGER reciprocal_review_lease_tombstones_no_update "
        "BEFORE UPDATE ON reciprocal_review_lease_tombstones "
        "BEGIN SELECT RAISE(ABORT, 'immutable review lease tombstone'); END"
    )
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()

    path = tmp_path / "missing-epoch.sqlite"
    _seed(path)
    now = [NOW]
    uow = _uow(path, now)
    uow.assign(HumanReviewerAssignment("assign", "aa", "ao", "run", "cycle", "req", _human()))
    uow.claim(ClaimReviewRun("claim", "ca", "co", "run", _human(), timedelta(minutes=1)))
    now[0] += timedelta(minutes=1)
    uow.claim(ClaimReviewRun("reclaim", "ra", "ro", "run", _human(), timedelta(minutes=1)))
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER reciprocal_review_lease_tombstones_no_delete")
    connection.execute("DELETE FROM reciprocal_review_lease_tombstones")
    connection.execute(
        "CREATE TRIGGER reciprocal_review_lease_tombstones_no_delete "
        "BEFORE DELETE ON reciprocal_review_lease_tombstones "
        "BEGIN SELECT RAISE(ABORT, 'immutable review lease tombstone'); END"
    )
    connection.commit()
    with pytest.raises(SqliteReciprocalReviewLeaseError):
        migrate_sqlite_reciprocal_review_lease(connection)
    connection.close()
