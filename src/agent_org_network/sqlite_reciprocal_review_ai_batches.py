"""P18 S1b.3 fenced, advisory-only AI finding-batch recorder."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Protocol

from agent_org_network.reciprocal_review import AiReviewerPrincipal, RecordAiAdvisoryBatch
from agent_org_network.sqlite_durable_reciprocal_review import (
    validate_sqlite_durable_reciprocal_review_ledger,
)
from agent_org_network.sqlite_reciprocal_review_lease import (
    ActiveReviewLeaseProof,
    SqliteReciprocalReviewLeaseConflict,
    SqliteReciprocalReviewLeaseUnitOfWork,
    validate_sqlite_reciprocal_review_lease,
)


class SqliteReciprocalReviewAiBatchError(RuntimeError):
    """An AI advisory batch was unsafe to record; messages deliberately contain no body/token."""


class SqliteReciprocalReviewAiBatchConflict(SqliteReciprocalReviewAiBatchError):
    """A receipt or batch identifier denotes a different semantic batch."""


class AiBatchPolicySnapshot(Protocol):
    def batch_is_current(
        self,
        *,
        org_id: str,
        cycle_id: str,
        requirement_id: str,
        policy_digest: str,
        provenance_digest: str,
        reviewer: AiReviewerPrincipal,
        model_execution_ref: str,
        deployment_digest: str,
        rubric_digest: str,
        prompt_digest: str,
        input_digest: str,
        content_digest: str,
    ) -> bool: ...

    def permits_awaiting_human_disposition(
        self,
        *,
        org_id: str,
        cycle_id: str,
        policy_digest: str,
        provenance_digest: str,
    ) -> bool: ...


class _TrustedHmacAiExecutionVerifier:
    """Sealed verifier; callers submit signatures, never verifier behaviour."""

    def __init__(self, keys: Mapping[str, bytes]) -> None:
        if not keys or any(not key or not value for key, value in keys.items()):
            raise ValueError("trusted AI execution key registry가 비어 있습니다.")
        self._keys = dict(keys)

    def verify(
        self,
        *,
        algorithm: str,
        key_id: str,
        signature: str,
        signed_payload_digest: str,
        canonical_payload_digest: str,
    ) -> bool:
        key = self._keys.get(key_id)
        if (
            algorithm != "hmac-sha256"
            or key is None
            or signed_payload_digest != canonical_payload_digest
        ):
            return False
        expected = hmac.new(key, canonical_payload_digest.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _time(value: datetime) -> str:
    if (
        value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
        or value.microsecond % 1000
    ):
        raise SqliteReciprocalReviewAiBatchError("DB time은 canonical UTC milliseconds여야 합니다.")
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


_CAPABILITY = object()
_MARKER = "reciprocal_review_ai_batch_manifest"
_BATCH = "reciprocal_review_ai_advisory_batches"
_FINDING = "reciprocal_review_ai_advisory_findings"
_RECEIPT = "reciprocal_review_ai_batch_receipts"
_AUDIT = "reciprocal_review_ai_batch_audit"
_OUTBOX = "reciprocal_review_ai_batch_outbox"
_PROJECTION = "reciprocal_review_ai_batch_terminal_projections"
_TABLES = (_MARKER, _BATCH, _FINDING, _RECEIPT, _AUDIT, _OUTBOX, _PROJECTION)
_DDLS = {
    _MARKER: f"CREATE TABLE {_MARKER} (version INTEGER NOT NULL CHECK(version=1), PRIMARY KEY(version))",
    _BATCH: f"CREATE TABLE {_BATCH} (org_id TEXT NOT NULL,batch_id TEXT NOT NULL,review_run_id TEXT NOT NULL,cycle_id TEXT NOT NULL,requirement_id TEXT NOT NULL,model_execution_ref TEXT NOT NULL,deployment_digest TEXT NOT NULL CHECK(length(deployment_digest)=64),rubric_digest TEXT NOT NULL CHECK(length(rubric_digest)=64),prompt_digest TEXT NOT NULL CHECK(length(prompt_digest)=64),input_digest TEXT NOT NULL CHECK(length(input_digest)=64),signature TEXT NOT NULL CHECK(length(signature)>0),signature_algorithm TEXT NOT NULL,signing_key_id TEXT NOT NULL,signed_payload_digest TEXT NOT NULL CHECK(length(signed_payload_digest)=64),batch_digest TEXT NOT NULL CHECK(length(batch_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,batch_id),UNIQUE(org_id,review_run_id),FOREIGN KEY(org_id,review_run_id) REFERENCES durable_reciprocal_review_runs(org_id,review_run_id))",
    _FINDING: f"CREATE TABLE {_FINDING} (org_id TEXT NOT NULL,finding_id TEXT NOT NULL,batch_id TEXT NOT NULL,criterion_ref TEXT NOT NULL,severity TEXT NOT NULL CHECK(severity IN ('info','warning','blocking')),evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),span_start INTEGER NOT NULL CHECK(span_start>=0),span_end INTEGER NOT NULL CHECK(span_end>span_start),PRIMARY KEY(org_id,finding_id),FOREIGN KEY(org_id,batch_id) REFERENCES {_BATCH}(org_id,batch_id))",
    _RECEIPT: f"CREATE TABLE {_RECEIPT} (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,audit_id TEXT NOT NULL,outbox_id TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),batch_id TEXT NOT NULL,cycle_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id),UNIQUE(org_id,audit_id),UNIQUE(org_id,outbox_id),FOREIGN KEY(org_id,batch_id) REFERENCES {_BATCH}(org_id,batch_id))",
    _AUDIT: f"CREATE TABLE {_AUDIT} (org_id TEXT NOT NULL,audit_id TEXT NOT NULL,receipt_id TEXT NOT NULL,event_digest TEXT NOT NULL CHECK(length(event_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,audit_id),FOREIGN KEY(org_id,receipt_id) REFERENCES {_RECEIPT}(org_id,receipt_id))",
    _OUTBOX: f"CREATE TABLE {_OUTBOX} (org_id TEXT NOT NULL,outbox_id TEXT NOT NULL,receipt_id TEXT NOT NULL,payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,outbox_id),FOREIGN KEY(org_id,receipt_id) REFERENCES {_RECEIPT}(org_id,receipt_id))",
    _PROJECTION: f"CREATE TABLE {_PROJECTION} (org_id TEXT NOT NULL,cycle_id TEXT NOT NULL,review_run_id TEXT NOT NULL,batch_id TEXT NOT NULL,terminal_kind TEXT NOT NULL CHECK(terminal_kind='recorded'),unmet_requirements INTEGER NOT NULL CHECK(unmet_requirements>=0),next_cycle_state TEXT NOT NULL CHECK(next_cycle_state IN ('review_open','awaiting_human_disposition')),projection_digest TEXT NOT NULL CHECK(length(projection_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,review_run_id),FOREIGN KEY(org_id,batch_id) REFERENCES {_BATCH}(org_id,batch_id))",
}
_TRIGGERS = {
    f"{table}_no_{action}": f"CREATE TRIGGER {table}_no_{action} BEFORE {action.upper()} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable AI advisory batch'); END"
    for table in _TABLES[1:]
    for action in ("update", "delete")
}


def _same(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()) == " ".join(expected.split())


def _validate_catalog(c: sqlite3.Connection) -> None:
    expected = set(_TABLES) | set(_TRIGGERS)
    actual = {
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'reciprocal_review_ai_%'"
        )
    }
    if actual != expected:
        raise SqliteReciprocalReviewAiBatchError("AI batch catalog가 canonical하지 않습니다.")
    if any(
        not _same(
            c.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (n,)
            ).fetchone()[0],
            ddl,
        )
        for n, ddl in _DDLS.items()
    ) or any(
        not _same(
            c.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (n,)
            ).fetchone()[0],
            ddl,
        )
        for n, ddl in _TRIGGERS.items()
    ):
        raise SqliteReciprocalReviewAiBatchError("AI batch catalog가 canonical하지 않습니다.")
    marker = c.execute(f"SELECT version FROM {_MARKER}").fetchall()
    if marker != [(1,)] or c.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SqliteReciprocalReviewAiBatchError(
            "AI batch manifest 또는 foreign key가 canonical하지 않습니다."
        )
    _validate_rows(c)


def validate_sqlite_reciprocal_review_ai_batches(
    c: sqlite3.Connection, *, trusted_execution_keys: Mapping[str, bytes]
) -> None:
    """Validate the persisted AI batch graph with the currently trusted keys.

    Schema-only validation deliberately cannot establish that a recorded batch was
    signed by a key which remains trusted.  Consumers that use an AI batch as
    authority must call this composition boundary, rather than injecting a
    verifier or reimplementing selected parts of its graph.
    """
    verifier = _TrustedHmacAiExecutionVerifier(trusted_execution_keys)
    _validate_catalog(c)
    for row in c.execute(
        "SELECT b.org_id,b.batch_id,b.review_run_id,b.model_execution_ref,"
        "b.rubric_digest,b.prompt_digest,b.input_digest,b.signature,"
        "b.signature_algorithm,b.signing_key_id,b.signed_payload_digest,"
        "b.deployment_digest,a.cycle_id,a.requirement_id,a.policy_digest,"
        "a.provenance_digest,r.content_sha256 "
        f"FROM {_BATCH} b "
        "JOIN reciprocal_review_lease_reviewer_assignments a "
        "ON a.org_id=b.org_id AND a.review_run_id=b.review_run_id "
        "JOIN durable_reciprocal_review_cycles cy "
        "ON cy.org_id=a.org_id AND cy.cycle_id=a.cycle_id "
        "JOIN durable_reciprocal_review_artifact_revisions r "
        "ON r.org_id=cy.org_id AND r.revision_id=cy.revision_id"
    ):
        findings = [
            {
                "finding_id": finding[0],
                "criterion_ref": finding[1],
                "severity": finding[2],
                "evidence_digest": finding[3],
                "evidence_start": finding[4],
                "evidence_end": finding[5],
            }
            for finding in c.execute(
                f"SELECT finding_id,criterion_ref,severity,evidence_digest,span_start,span_end FROM {_FINDING} WHERE org_id=? AND batch_id=? ORDER BY finding_id",
                (row[0], row[1]),
            )
        ]
        signed = _digest(
            {
                "org_id": row[0],
                "batch_id": row[1],
                "review_run_id": row[2],
                "model_execution_ref": row[3],
                "rubric_digest": row[4],
                "prompt_digest": row[5],
                "input_digest": row[6],
                "findings": findings,
                "cycle_id": row[12],
                "requirement_id": row[13],
                "policy_digest": row[14],
                "provenance_digest": row[15],
                "content_digest": row[16],
                "deployment_digest": row[11],
            }
        )
        if not verifier.verify(
            algorithm=row[8],
            key_id=row[9],
            signature=row[7],
            signed_payload_digest=row[10],
            canonical_payload_digest=signed,
        ):
            raise SqliteReciprocalReviewAiBatchError(
                "AI batch execution signature가 current key로 검증되지 않았습니다."
            )


def _canonical_batch_row(c: sqlite3.Connection, row: tuple[object, ...]) -> str:
    findings = [
        {
            "finding_id": finding[0],
            "criterion_ref": finding[1],
            "severity": finding[2],
            "evidence_digest": finding[3],
            "evidence_start": finding[4],
            "evidence_end": finding[5],
        }
        for finding in c.execute(
            f"SELECT finding_id,criterion_ref,severity,evidence_digest,span_start,span_end FROM {_FINDING} WHERE org_id=? AND batch_id=? ORDER BY finding_id",
            (row[0], row[1]),
        )
    ]
    return _digest(
        {
            "batch_id": row[1],
            "review_run_id": row[2],
            "cycle_id": row[3],
            "requirement_id": row[4],
            "model_execution_ref": row[5],
            "deployment_digest": row[6],
            "rubric_digest": row[7],
            "prompt_digest": row[8],
            "input_digest": row[9],
            "signature": row[10],
            "signature_algorithm": row[11],
            "signing_key_id": row[12],
            "signed_payload_digest": row[13],
            "findings": findings,
        }
    )


def _validate_rows(c: sqlite3.Connection) -> None:
    for row in c.execute(
        f"SELECT org_id,batch_id,review_run_id,cycle_id,requirement_id,model_execution_ref,deployment_digest,rubric_digest,prompt_digest,input_digest,signature,signature_algorithm,signing_key_id,signed_payload_digest,batch_digest FROM {_BATCH}"
    ):
        if not all(isinstance(value, str) and value for value in row) or row[
            14
        ] != _canonical_batch_row(c, row):
            raise SqliteReciprocalReviewAiBatchError("AI batch row가 canonical하지 않습니다.")
        binding = c.execute(
            "SELECT cycle_id,requirement_id,reviewer_kind FROM reciprocal_review_lease_reviewer_assignments WHERE org_id=? AND review_run_id=?",
            (row[0], row[2]),
        ).fetchone()
        receipts = c.execute(
            f"SELECT receipt_id,audit_id,outbox_id,command_digest,cycle_id FROM {_RECEIPT} WHERE org_id=? AND batch_id=?",
            (row[0], row[1]),
        ).fetchall()
        projection = c.execute(
            f"SELECT cycle_id,review_run_id,batch_id FROM {_PROJECTION} WHERE org_id=? AND batch_id=?",
            (row[0], row[1]),
        ).fetchall()
        if (
            binding != (row[3], row[4], "ai")
            or len(receipts) != 1
            or len(projection) != 1
            or projection[0] != (row[3], row[2], row[1])
        ):
            raise SqliteReciprocalReviewAiBatchError(
                "AI batch evidence graph가 canonical하지 않습니다."
            )
        projected = c.execute(
            f"SELECT unmet_requirements,next_cycle_state,projection_digest FROM {_PROJECTION} WHERE org_id=? AND batch_id=?",
            (row[0], row[1]),
        ).fetchone()
        cycle = c.execute(
            "SELECT state_kind FROM durable_reciprocal_review_cycles WHERE org_id=? AND cycle_id=?",
            (row[0], row[3]),
        ).fetchone()
        requirements = c.execute(
            "SELECT r.completion_rule,r.required_count,COUNT(b.batch_id) FROM durable_reciprocal_review_requirements r LEFT JOIN reciprocal_review_lease_reviewer_assignments a ON a.org_id=r.org_id AND a.requirement_id=r.requirement_id LEFT JOIN reciprocal_review_ai_advisory_batches b ON b.org_id=a.org_id AND b.review_run_id=a.review_run_id WHERE r.org_id=? AND r.cycle_id=? GROUP BY r.requirement_id,r.completion_rule,r.required_count",
            (row[0], row[3]),
        ).fetchall()
        unmet = sum(
            1
            for rule, required, count in requirements
            if count < (1 if rule == "any" else required)
        )
        expected_state = "awaiting_human_disposition" if unmet == 0 else "review_open"
        if (
            projected is None
            or projected[:2] != (unmet, expected_state)
            or cycle != (projected[1],)
            or projected[2] != _digest((row[1], row[2], projected[0], projected[1]))
        ):
            raise SqliteReciprocalReviewAiBatchError(
                "AI batch cycle projection이 canonical하지 않습니다."
            )
        receipt_id, audit_id, outbox_id, command_digest, cycle_id = receipts[0]
        audit = c.execute(
            f"SELECT event_digest FROM {_AUDIT} WHERE org_id=? AND audit_id=? AND receipt_id=?",
            (row[0], audit_id, receipt_id),
        ).fetchall()
        outbox = c.execute(
            f"SELECT payload_digest FROM {_OUTBOX} WHERE org_id=? AND outbox_id=? AND receipt_id=?",
            (row[0], outbox_id, receipt_id),
        ).fetchall()
        if (
            cycle_id != row[3]
            or len(audit) != 1
            or len(outbox) != 1
            or audit[0][0] != _digest(("ai_batch_audit", command_digest))
            or outbox[0][0] != _digest(("ai_batch_outbox", command_digest))
        ):
            raise SqliteReciprocalReviewAiBatchError(
                "AI batch audit/outbox가 canonical하지 않습니다."
            )


def migrate_sqlite_reciprocal_review_ai_batches(c: sqlite3.Connection) -> None:
    try:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (_MARKER,)
        ).fetchone()
        if existing:
            _validate_catalog(c)
        else:
            for ddl in _DDLS.values():
                c.execute(ddl)
            for ddl in _TRIGGERS.values():
                c.execute(ddl)
            c.execute(f"INSERT INTO {_MARKER} VALUES(1)")
            _validate_catalog(c)
        c.commit()
    except Exception:
        if c.in_transaction:
            c.rollback()
        raise


@dataclass(frozen=True, slots=True)
class RecordedAiAdvisoryBatch:
    org_id: str
    batch_id: str
    review_run_id: str
    command_digest: str
    unmet_requirements: int
    next_cycle_state: str


class SqliteReciprocalReviewAiBatchUnitOfWork:
    def __init__(
        self,
        path: str | Path,
        *,
        lease_uow: SqliteReciprocalReviewLeaseUnitOfWork,
        policy_snapshot: AiBatchPolicySnapshot,
        signature_verifier: _TrustedHmacAiExecutionVerifier,
        fault_injector: Callable[[str], None] | None,
        _capability: object,
    ) -> None:
        if _capability is not _CAPABILITY:
            raise TypeError("Use create_sqlite_reciprocal_review_ai_batch_uow().")
        (
            self._path,
            self._lease_uow,
            self._policy,
            self._signature_verifier,
            self._fault_injector,
        ) = Path(path), lease_uow, policy_snapshot, signature_verifier, fault_injector

    def record(self, command: RecordAiAdvisoryBatch) -> RecordedAiAdvisoryBatch:
        # A lease secret is a capability, never evidence.  Reject equality leaks before
        # a command digest, error, or any SQLite row can be produced.
        public_values = _canonical(
            {
                "receipt_id": command.receipt_id,
                "audit_id": command.audit_id,
                "outbox_id": command.outbox_id,
                "principal": command.principal.model_dump(mode="json"),
                "batch": command.batch.model_dump(mode="json"),
            }
        )
        if command.lease_token in public_values:
            raise SqliteReciprocalReviewAiBatchConflict(
                "AI batch public evidence에 lease secret이 있습니다."
            )
        semantic = self._semantic(command)
        digest = _digest(semantic)
        c = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE")
            validate_sqlite_durable_reciprocal_review_ledger(c)
            validate_sqlite_reciprocal_review_lease(c)
            _validate_catalog(c)
            old = c.execute(
                f"SELECT command_digest,batch_id FROM {_RECEIPT} WHERE org_id=? AND receipt_id=?",
                (command.batch.org_id, command.receipt_id),
            ).fetchone()
            if old is not None:
                if old[0] != digest or old[1] != command.batch.batch_id:
                    raise SqliteReciprocalReviewAiBatchConflict(
                        "AI batch receipt semantic command가 다릅니다."
                    )
                result = self._result(c, command.batch.org_id, command.batch.batch_id, digest)
                c.commit()
                return result
            proof, now = self._lease_uow.validate_active_lease_in_transaction(
                c,
                reviewer=command.principal,
                review_run_id=command.batch.review_run_id,
                lease_epoch=command.lease_epoch,
                lease_token=command.lease_token,
            )
            self._assert_current(c, command, proof)
            self._insert(c, command, digest, now)
            _validate_catalog(c)
            validate_sqlite_durable_reciprocal_review_ledger(c)
            validate_sqlite_reciprocal_review_lease(c)
            c.commit()
            return self._result(c, command.batch.org_id, command.batch.batch_id, digest)
        except SqliteReciprocalReviewLeaseConflict as error:
            if c.in_transaction:
                c.rollback()
            raise SqliteReciprocalReviewAiBatchConflict("AI batch lease가 stale입니다.") from error
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _semantic(command: RecordAiAdvisoryBatch) -> dict[str, object]:
        return {
            "principal": command.principal.model_dump(mode="json"),
            "epoch": command.lease_epoch,
            "batch": command.batch.model_dump(mode="json"),
            "receipt": command.receipt_id,
            "audit": command.audit_id,
            "outbox": command.outbox_id,
        }

    def _assert_current(
        self, c: sqlite3.Connection, command: RecordAiAdvisoryBatch, proof: ActiveReviewLeaseProof
    ) -> None:
        batch = command.batch
        row = c.execute(
            "SELECT a.cycle_id,a.requirement_id,a.reviewer_kind,a.reviewer_ref,a.policy_digest,a.provenance_digest,r.content_sha256 "
            "FROM reciprocal_review_lease_reviewer_assignments a "
            "JOIN durable_reciprocal_review_cycles cy ON cy.org_id=a.org_id AND cy.cycle_id=a.cycle_id "
            "JOIN durable_reciprocal_review_artifact_revisions r ON r.org_id=cy.org_id AND r.revision_id=cy.revision_id "
            "WHERE a.org_id=? AND a.review_run_id=? AND cy.active=1",
            (batch.org_id, batch.review_run_id),
        ).fetchone()
        if (
            row is None
            or row[2:] == ()
            or row[2] != "ai"
            or row[3] != command.principal.reviewer_id
            or proof.reviewer_ref != command.principal.reviewer_id
            or batch.model_execution_ref != command.principal.model_execution_ref
            or batch.rubric_digest != command.principal.rubric_digest
        ):
            raise SqliteReciprocalReviewAiBatchConflict(
                "AI reviewer/run provenance가 일치하지 않습니다."
            )
        if not self._policy.batch_is_current(
            org_id=batch.org_id,
            cycle_id=row[0],
            requirement_id=row[1],
            policy_digest=row[4],
            provenance_digest=row[5],
            reviewer=command.principal,
            model_execution_ref=batch.model_execution_ref,
            deployment_digest=command.principal.deployment_digest,
            rubric_digest=batch.rubric_digest,
            prompt_digest=batch.prompt_digest,
            input_digest=batch.input_digest,
            content_digest=row[6],
        ):
            raise SqliteReciprocalReviewAiBatchConflict(
                "AI batch policy/model/provenance가 current하지 않습니다."
            )
        signed = _digest(
            {
                "org_id": batch.org_id,
                "batch_id": batch.batch_id,
                "review_run_id": batch.review_run_id,
                "model_execution_ref": batch.model_execution_ref,
                "rubric_digest": batch.rubric_digest,
                "prompt_digest": batch.prompt_digest,
                "input_digest": batch.input_digest,
                "findings": [finding.model_dump(mode="json") for finding in batch.findings],
                "cycle_id": row[0],
                "requirement_id": row[1],
                "policy_digest": row[4],
                "provenance_digest": row[5],
                "content_digest": row[6],
                "deployment_digest": command.principal.deployment_digest,
            }
        )
        if batch.signed_payload_digest != signed or not self._signature_verifier.verify(
            algorithm=batch.signature_algorithm,
            key_id=batch.signing_key_id,
            signature=batch.signature,
            signed_payload_digest=batch.signed_payload_digest,
            canonical_payload_digest=signed,
        ):
            raise SqliteReciprocalReviewAiBatchConflict(
                "AI execution signature가 검증되지 않았습니다."
            )

    def _insert(
        self, c: sqlite3.Connection, command: RecordAiAdvisoryBatch, digest: str, now: datetime
    ) -> None:
        b, now_text = command.batch, _time(now)
        row = c.execute(
            "SELECT cycle_id,requirement_id FROM reciprocal_review_lease_reviewer_assignments WHERE org_id=? AND review_run_id=?",
            (b.org_id, b.review_run_id),
        ).fetchone()
        assert row is not None
        findings = [
            {
                "finding_id": finding.finding_id,
                "criterion_ref": finding.criterion_ref,
                "severity": finding.severity,
                "evidence_digest": finding.evidence_digest,
                "evidence_start": finding.evidence_start,
                "evidence_end": finding.evidence_end,
            }
            for finding in sorted(b.findings, key=lambda value: value.finding_id)
        ]
        batch_digest = _digest(
            {
                "batch_id": b.batch_id,
                "review_run_id": b.review_run_id,
                "cycle_id": row[0],
                "requirement_id": row[1],
                "model_execution_ref": b.model_execution_ref,
                "deployment_digest": command.principal.deployment_digest,
                "rubric_digest": b.rubric_digest,
                "prompt_digest": b.prompt_digest,
                "input_digest": b.input_digest,
                "signature": b.signature,
                "signature_algorithm": b.signature_algorithm,
                "signing_key_id": b.signing_key_id,
                "signed_payload_digest": b.signed_payload_digest,
                "findings": findings,
            }
        )
        if b.batch_digest and b.batch_digest != batch_digest:
            raise SqliteReciprocalReviewAiBatchConflict("AI batch digest가 canonical하지 않습니다.")
        try:
            c.execute(
                f"INSERT INTO {_BATCH} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    b.org_id,
                    b.batch_id,
                    b.review_run_id,
                    row[0],
                    row[1],
                    b.model_execution_ref,
                    command.principal.deployment_digest,
                    b.rubric_digest,
                    b.prompt_digest,
                    b.input_digest,
                    b.signature,
                    b.signature_algorithm,
                    b.signing_key_id,
                    b.signed_payload_digest,
                    batch_digest,
                    now_text,
                ),
            )
            for finding in b.findings:
                c.execute(
                    f"INSERT INTO {_FINDING} VALUES(?,?,?,?,?,?,?,?)",
                    (
                        b.org_id,
                        finding.finding_id,
                        b.batch_id,
                        finding.criterion_ref,
                        finding.severity,
                        finding.evidence_digest,
                        finding.evidence_start,
                        finding.evidence_end,
                    ),
                )
            self._fault("after_findings")
        except sqlite3.IntegrityError as error:
            raise SqliteReciprocalReviewAiBatchConflict(
                "AI batch 또는 finding 식별자가 충돌합니다."
            ) from error
        unmet, next_state = self._projection(c, b.org_id, row[0])
        if next_state == "awaiting_human_disposition":
            policy, provenance = c.execute(
                "SELECT policy_digest,provenance_digest FROM durable_reciprocal_review_cycles WHERE org_id=? AND cycle_id=?",
                (b.org_id, row[0]),
            ).fetchone()
            if not self._policy.permits_awaiting_human_disposition(
                org_id=b.org_id, cycle_id=row[0], policy_digest=policy, provenance_digest=provenance
            ):
                next_state = "review_open"
            elif (
                c.execute(
                    "UPDATE durable_reciprocal_review_cycles SET state_kind='awaiting_human_disposition' WHERE org_id=? AND cycle_id=? AND state_kind='review_open' AND active=1",
                    (b.org_id, row[0]),
                ).rowcount
                != 1
            ):
                raise SqliteReciprocalReviewAiBatchConflict("cycle completion CAS가 stale입니다.")
        projection_digest = _digest((b.batch_id, b.review_run_id, unmet, next_state))
        c.execute(
            f"INSERT INTO {_PROJECTION} VALUES(?,?,?,?,?,?,?,?,?)",
            (
                b.org_id,
                row[0],
                b.review_run_id,
                b.batch_id,
                "recorded",
                unmet,
                next_state,
                projection_digest,
                now_text,
            ),
        )
        self._fault("before_run_recorded")
        if (
            c.execute(
                "UPDATE durable_reciprocal_review_runs SET state='recorded' WHERE org_id=? AND review_run_id=? AND state='leased' AND lease_epoch=?",
                (b.org_id, b.review_run_id, command.lease_epoch),
            ).rowcount
            != 1
        ):
            raise SqliteReciprocalReviewAiBatchConflict("AI batch run terminal CAS가 stale입니다.")
        c.execute(
            f"INSERT INTO {_RECEIPT} VALUES(?,?,?,?,?,?,?,?)",
            (
                b.org_id,
                command.receipt_id,
                command.audit_id,
                command.outbox_id,
                digest,
                b.batch_id,
                row[0],
                now_text,
            ),
        )
        c.execute(
            f"INSERT INTO {_AUDIT} VALUES(?,?,?,?,?)",
            (
                b.org_id,
                command.audit_id,
                command.receipt_id,
                _digest(("ai_batch_audit", digest)),
                now_text,
            ),
        )
        c.execute(
            f"INSERT INTO {_OUTBOX} VALUES(?,?,?,?,?)",
            (
                b.org_id,
                command.outbox_id,
                command.receipt_id,
                _digest(("ai_batch_outbox", digest)),
                now_text,
            ),
        )
        self._fault("after_outbox")

    @staticmethod
    def _projection(c: sqlite3.Connection, org_id: str, cycle_id: str) -> tuple[int, str]:
        rows = c.execute(
            "SELECT r.requirement_id,r.completion_rule,r.required_count,COUNT(b.batch_id) FROM durable_reciprocal_review_requirements r LEFT JOIN reciprocal_review_lease_reviewer_assignments a ON a.org_id=r.org_id AND a.requirement_id=r.requirement_id LEFT JOIN reciprocal_review_ai_advisory_batches b ON b.org_id=a.org_id AND b.review_run_id=a.review_run_id WHERE r.org_id=? AND r.cycle_id=? GROUP BY r.requirement_id,r.completion_rule,r.required_count",
            (org_id, cycle_id),
        ).fetchall()
        unmet = sum(
            1 for _id, rule, required, count in rows if count < (1 if rule == "any" else required)
        )
        return unmet, "awaiting_human_disposition" if unmet == 0 else "review_open"

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @staticmethod
    def _result(
        c: sqlite3.Connection, org_id: str, batch_id: str, digest: str
    ) -> RecordedAiAdvisoryBatch:
        row = c.execute(
            f"SELECT review_run_id,unmet_requirements,next_cycle_state FROM {_PROJECTION} WHERE org_id=? AND batch_id=?",
            (org_id, batch_id),
        ).fetchone()
        if row is None:
            raise SqliteReciprocalReviewAiBatchError("AI batch projection이 없습니다.")
        return RecordedAiAdvisoryBatch(org_id, batch_id, row[0], digest, row[1], row[2])


def create_sqlite_reciprocal_review_ai_batch_uow(
    path: str | Path,
    *,
    lease_uow: SqliteReciprocalReviewLeaseUnitOfWork,
    policy_snapshot: AiBatchPolicySnapshot,
    trusted_execution_keys: Mapping[str, bytes],
    fault_injector: Callable[[str], None] | None = None,
) -> SqliteReciprocalReviewAiBatchUnitOfWork:
    return SqliteReciprocalReviewAiBatchUnitOfWork(
        path,
        lease_uow=lease_uow,
        policy_snapshot=policy_snapshot,
        signature_verifier=_TrustedHmacAiExecutionVerifier(trusted_execution_keys),
        fault_injector=fault_injector,
        _capability=_CAPABILITY,
    )
