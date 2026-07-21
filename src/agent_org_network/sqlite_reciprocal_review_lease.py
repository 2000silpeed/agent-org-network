"""P18 S1b.2 sealed, token-safe reviewer assignment and durable lease UoW."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from agent_org_network.reciprocal_review import AiReviewerPrincipal, HumanPrincipal
from agent_org_network.sqlite_durable_reciprocal_review import (
    validate_sqlite_durable_reciprocal_review_ledger,
)


class SqliteReciprocalReviewLeaseError(RuntimeError):
    """Lease operation cannot safely proceed; messages intentionally omit tokens."""


class SqliteReciprocalReviewLeaseConflict(SqliteReciprocalReviewLeaseError):
    """A stale, unauthorized, or semantically different operation was rejected."""


Reviewer = HumanPrincipal | AiReviewerPrincipal


@dataclass(frozen=True, slots=True)
class HumanReviewerAssignment:
    receipt_id: str
    audit_id: str
    outbox_id: str
    review_run_id: str
    cycle_id: str
    requirement_id: str
    reviewer: HumanPrincipal


@dataclass(frozen=True, slots=True)
class AiReviewerAssignment:
    receipt_id: str
    audit_id: str
    outbox_id: str
    review_run_id: str
    cycle_id: str
    requirement_id: str
    reviewer: AiReviewerPrincipal


@dataclass(frozen=True, slots=True)
class ClaimReviewRun:
    receipt_id: str
    audit_id: str
    outbox_id: str
    review_run_id: str
    reviewer: Reviewer
    lease_for: timedelta


@dataclass(frozen=True, slots=True)
class RenewReviewLease:
    receipt_id: str
    audit_id: str
    outbox_id: str
    review_run_id: str
    reviewer: Reviewer
    lease_epoch: int
    lease_token: str
    lease_for: timedelta


@dataclass(frozen=True, slots=True)
class ReviewLease:
    org_id: str
    review_run_id: str
    lease_epoch: int
    expires_at: datetime
    lease_token: str | None


@dataclass(frozen=True, slots=True)
class ActiveReviewLeaseProof:
    """Opaque-at-rest proof emitted only after full-token verification."""

    org_id: str
    review_run_id: str
    reviewer_kind: str
    reviewer_ref: str
    lease_epoch: int
    token_hash: str


@dataclass(frozen=True, slots=True)
class _LeaseTransition:
    transition_type: str
    prior_epoch: int
    prior_state: str
    expected_token_hash: str | None
    expected_expires_at: str | None
    new_token_hash: str | None
    expires_at: str | None
    db_time: str


class ReviewerAssignmentAuthorization(Protocol):
    def authorize_human_reviewer(
        self, *, reviewer: HumanPrincipal, contributor_subject_ids: tuple[str, ...]
    ) -> bool: ...
    def authorize_ai_reviewer(self, *, reviewer: AiReviewerPrincipal) -> bool: ...


class ReviewPolicySnapshot(Protocol):
    def requirement_is_current(
        self,
        *,
        org_id: str,
        cycle_id: str,
        requirement_id: str,
        policy_digest: str,
        provenance_digest: str,
    ) -> bool: ...


class DbTransactionTime(Protocol):
    def __call__(self, connection: sqlite3.Connection) -> datetime: ...


_CAPABILITY = object()
_ASSIGN = "reciprocal_review_lease_reviewer_assignments"
_STATE = "reciprocal_review_lease_state"
_TOMB = "reciprocal_review_lease_tombstones"
_RECEIPT = "reciprocal_review_lease_receipts"
_AUDIT = "reciprocal_review_lease_audit"
_OUTBOX = "reciprocal_review_lease_outbox"
_MARKER = "reciprocal_review_lease_manifest"
_STATE_QUEUE_INDEX = "reciprocal_review_lease_state_queue_idx"
_ASSIGN_NO_UPDATE = "reciprocal_review_lease_assignments_no_update"
_ASSIGN_NO_DELETE = "reciprocal_review_lease_assignments_no_delete"
_TOMB_NO_UPDATE = "reciprocal_review_lease_tombstones_no_update"
_TOMB_NO_DELETE = "reciprocal_review_lease_tombstones_no_delete"
_RECEIPT_NO_UPDATE = "reciprocal_review_lease_receipts_no_update"
_RECEIPT_NO_DELETE = "reciprocal_review_lease_receipts_no_delete"
_AUDIT_NO_UPDATE = "reciprocal_review_lease_audit_no_update"
_AUDIT_NO_DELETE = "reciprocal_review_lease_audit_no_delete"
_OUTBOX_NO_UPDATE = "reciprocal_review_lease_outbox_no_update"
_OUTBOX_NO_DELETE = "reciprocal_review_lease_outbox_no_delete"

_DDLS = {
    _MARKER: f"CREATE TABLE {_MARKER} (version INTEGER NOT NULL CHECK(version=3), PRIMARY KEY(version))",
    _ASSIGN: f"CREATE TABLE {_ASSIGN} (org_id TEXT NOT NULL, review_run_id TEXT NOT NULL, cycle_id TEXT NOT NULL, requirement_id TEXT NOT NULL, reviewer_kind TEXT NOT NULL CHECK(reviewer_kind IN ('human','ai')), reviewer_ref TEXT NOT NULL, policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64), provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64), assignment_digest TEXT NOT NULL CHECK(length(assignment_digest)=64), created_at TEXT NOT NULL, PRIMARY KEY(org_id,review_run_id), UNIQUE(org_id,requirement_id,reviewer_kind,reviewer_ref))",
    _STATE: f"CREATE TABLE {_STATE} (org_id TEXT NOT NULL, review_run_id TEXT NOT NULL, owner_kind TEXT, owner_ref TEXT, lease_epoch INTEGER NOT NULL CHECK(lease_epoch>=1), token_hash TEXT, expires_at TEXT, state TEXT NOT NULL CHECK(state IN ('queued','leased')), transition_digest TEXT, PRIMARY KEY(org_id,review_run_id), FOREIGN KEY(org_id,review_run_id) REFERENCES {_ASSIGN}(org_id,review_run_id))",
    _TOMB: f"CREATE TABLE {_TOMB} (org_id TEXT NOT NULL, review_run_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, token_hash TEXT NOT NULL CHECK(length(token_hash)=64), expired_at TEXT NOT NULL, transition_digest TEXT, PRIMARY KEY(org_id,review_run_id,lease_epoch))",
    _RECEIPT: f"CREATE TABLE {_RECEIPT} (org_id TEXT NOT NULL, receipt_id TEXT NOT NULL, audit_id TEXT NOT NULL, outbox_id TEXT NOT NULL, command_digest TEXT NOT NULL CHECK(length(command_digest)=64), review_run_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, transition_type TEXT NOT NULL CHECK(transition_type IN ('assign','claim','renew','reclaim')), prior_epoch INTEGER NOT NULL CHECK(prior_epoch>=0), prior_state TEXT NOT NULL CHECK(prior_state IN ('absent','queued','leased')), expected_token_hash TEXT, expected_expires_at TEXT, new_token_hash TEXT, expires_at TEXT, db_time TEXT NOT NULL, predecessor_transition_digest TEXT, evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64), created_at TEXT NOT NULL, PRIMARY KEY(org_id,receipt_id), UNIQUE(org_id,audit_id), UNIQUE(org_id,outbox_id), FOREIGN KEY(org_id,review_run_id) REFERENCES {_ASSIGN}(org_id,review_run_id))",
    _AUDIT: f"CREATE TABLE {_AUDIT} (org_id TEXT NOT NULL, audit_id TEXT NOT NULL, receipt_id TEXT NOT NULL, command_digest TEXT NOT NULL CHECK(length(command_digest)=64), review_run_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, event_digest TEXT NOT NULL CHECK(length(event_digest)=64), created_at TEXT NOT NULL, PRIMARY KEY(org_id,audit_id), UNIQUE(org_id,receipt_id), FOREIGN KEY(org_id,receipt_id) REFERENCES {_RECEIPT}(org_id,receipt_id), FOREIGN KEY(org_id,review_run_id) REFERENCES {_ASSIGN}(org_id,review_run_id))",
    _OUTBOX: f"CREATE TABLE {_OUTBOX} (org_id TEXT NOT NULL, outbox_id TEXT NOT NULL, receipt_id TEXT NOT NULL, command_digest TEXT NOT NULL CHECK(length(command_digest)=64), review_run_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64), created_at TEXT NOT NULL, PRIMARY KEY(org_id,outbox_id), UNIQUE(org_id,receipt_id), FOREIGN KEY(org_id,receipt_id) REFERENCES {_RECEIPT}(org_id,receipt_id), FOREIGN KEY(org_id,review_run_id) REFERENCES {_ASSIGN}(org_id,review_run_id))",
}
_INDEX_DDLS = {
    _STATE_QUEUE_INDEX: f"CREATE INDEX {_STATE_QUEUE_INDEX} ON {_STATE}(org_id,state,expires_at)"
}
_TRIGGER_DDLS = {
    _ASSIGN_NO_UPDATE: f"CREATE TRIGGER {_ASSIGN_NO_UPDATE} BEFORE UPDATE ON {_ASSIGN} BEGIN SELECT RAISE(ABORT, 'immutable review lease assignment'); END",
    _ASSIGN_NO_DELETE: f"CREATE TRIGGER {_ASSIGN_NO_DELETE} BEFORE DELETE ON {_ASSIGN} BEGIN SELECT RAISE(ABORT, 'immutable review lease assignment'); END",
    _TOMB_NO_UPDATE: f"CREATE TRIGGER {_TOMB_NO_UPDATE} BEFORE UPDATE ON {_TOMB} WHEN OLD.transition_digest IS NOT NULL OR NEW.org_id IS NOT OLD.org_id OR NEW.review_run_id IS NOT OLD.review_run_id OR NEW.lease_epoch IS NOT OLD.lease_epoch OR NEW.token_hash IS NOT OLD.token_hash OR NEW.expired_at IS NOT OLD.expired_at OR NEW.transition_digest IS NULL BEGIN SELECT RAISE(ABORT, 'immutable review lease tombstone'); END",
    _TOMB_NO_DELETE: f"CREATE TRIGGER {_TOMB_NO_DELETE} BEFORE DELETE ON {_TOMB} BEGIN SELECT RAISE(ABORT, 'immutable review lease tombstone'); END",
    _RECEIPT_NO_UPDATE: f"CREATE TRIGGER {_RECEIPT_NO_UPDATE} BEFORE UPDATE ON {_RECEIPT} BEGIN SELECT RAISE(ABORT, 'immutable review lease receipt'); END",
    _RECEIPT_NO_DELETE: f"CREATE TRIGGER {_RECEIPT_NO_DELETE} BEFORE DELETE ON {_RECEIPT} BEGIN SELECT RAISE(ABORT, 'immutable review lease receipt'); END",
    _AUDIT_NO_UPDATE: f"CREATE TRIGGER {_AUDIT_NO_UPDATE} BEFORE UPDATE ON {_AUDIT} BEGIN SELECT RAISE(ABORT, 'immutable review lease audit'); END",
    _AUDIT_NO_DELETE: f"CREATE TRIGGER {_AUDIT_NO_DELETE} BEFORE DELETE ON {_AUDIT} BEGIN SELECT RAISE(ABORT, 'immutable review lease audit'); END",
    _OUTBOX_NO_UPDATE: f"CREATE TRIGGER {_OUTBOX_NO_UPDATE} BEFORE UPDATE ON {_OUTBOX} BEGIN SELECT RAISE(ABORT, 'immutable review lease outbox'); END",
    _OUTBOX_NO_DELETE: f"CREATE TRIGGER {_OUTBOX_NO_DELETE} BEFORE DELETE ON {_OUTBOX} BEGIN SELECT RAISE(ABORT, 'immutable review lease outbox'); END",
}

_TABLE_COLUMNS = {
    _MARKER: ("version",),
    _ASSIGN: (
        "org_id",
        "review_run_id",
        "cycle_id",
        "requirement_id",
        "reviewer_kind",
        "reviewer_ref",
        "policy_digest",
        "provenance_digest",
        "assignment_digest",
        "created_at",
    ),
    _STATE: (
        "org_id",
        "review_run_id",
        "owner_kind",
        "owner_ref",
        "lease_epoch",
        "token_hash",
        "expires_at",
        "state",
        "transition_digest",
    ),
    _TOMB: (
        "org_id",
        "review_run_id",
        "lease_epoch",
        "token_hash",
        "expired_at",
        "transition_digest",
    ),
    _RECEIPT: (
        "org_id",
        "receipt_id",
        "audit_id",
        "outbox_id",
        "command_digest",
        "review_run_id",
        "lease_epoch",
        "transition_type",
        "prior_epoch",
        "prior_state",
        "expected_token_hash",
        "expected_expires_at",
        "new_token_hash",
        "expires_at",
        "db_time",
        "predecessor_transition_digest",
        "evidence_digest",
        "created_at",
    ),
    _AUDIT: (
        "org_id",
        "audit_id",
        "receipt_id",
        "command_digest",
        "review_run_id",
        "lease_epoch",
        "event_digest",
        "created_at",
    ),
    _OUTBOX: (
        "org_id",
        "outbox_id",
        "receipt_id",
        "command_digest",
        "review_run_id",
        "lease_epoch",
        "payload_digest",
        "created_at",
    ),
}
_OPAQUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")


def _same(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()) == " ".join(expected.split())


def _catalog_sql(connection: sqlite3.Connection, kind: str, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind, name)
    ).fetchone()
    return None if row is None else row[0]


def _companion_catalog(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type IN ('table','index','trigger') "
            "AND name LIKE 'reciprocal_review_lease_%'"
        )
    }


def _time(value: datetime) -> str:
    if (
        value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
        or value.microsecond % 1000
    ):
        raise SqliteReciprocalReviewLeaseError("DB time은 canonical UTC milliseconds여야 합니다.")
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _transition_digest(
    transition_type: str,
    review_run_id: str,
    prior_epoch: int,
    lease_epoch: int,
    prior_state: str,
    expected_token_hash: str | None,
    expected_expires_at: str | None,
    new_token_hash: str | None,
    expires_at: str | None,
    db_time: str,
    predecessor_transition_digest: str | None,
) -> str:
    return _digest(
        (
            "reciprocal-review-lease-transition-v2",
            transition_type,
            review_run_id,
            prior_epoch,
            lease_epoch,
            prior_state,
            expected_token_hash,
            new_token_hash,
            expected_expires_at,
            expires_at,
            db_time,
            predecessor_transition_digest,
        )
    )


def _evidence_record_digest(evidence_digest: str, transition_type: str, table: str) -> str:
    action = "audit" if table == _AUDIT else "outbox"
    return _digest(
        ("reciprocal-review-lease-evidence-v1", action, transition_type, evidence_digest)
    )


def _result_state(transition_type: str) -> str:
    return "queued" if transition_type == "assign" else "leased"


def _valid_transition(prior: tuple[object, ...], receipt: tuple[object, ...]) -> bool:
    transition_type = receipt[7]
    prior_epoch = prior[6]
    epoch = receipt[6]
    if not isinstance(prior_epoch, int) or not isinstance(epoch, int):
        return False
    if transition_type == "claim":
        return prior[7] == "assign" and epoch == prior_epoch
    if transition_type == "renew":
        return prior[7] in {"claim", "renew", "reclaim"} and epoch == prior_epoch
    if transition_type == "reclaim":
        return prior[7] in {"claim", "renew", "reclaim"} and epoch == prior_epoch + 1
    return False


def _reviewer_ref(reviewer: Reviewer) -> tuple[str, str]:
    return (
        ("human", reviewer.subject_id)
        if isinstance(reviewer, HumanPrincipal)
        else ("ai", reviewer.reviewer_id)
    )


def _install(connection: sqlite3.Connection) -> None:
    connection.execute(_DDLS[_MARKER])
    connection.execute(f"INSERT INTO {_MARKER} VALUES(3)")
    for ddl in (*_DDLS.values(), *_INDEX_DDLS.values(), *_TRIGGER_DDLS.values()):
        if ddl != _DDLS[_MARKER]:
            connection.execute(ddl)


def migrate_sqlite_reciprocal_review_lease(connection: sqlite3.Connection) -> None:
    """Install the additive S1b.2 companion before its sealed UoW is enabled."""
    connection.execute("SAVEPOINT reciprocal_review_lease_migration")
    try:
        if not _companion_catalog(connection):
            _install(connection)
        _validate_companion(connection)
    except Exception:
        connection.execute("ROLLBACK TO reciprocal_review_lease_migration")
        connection.execute("RELEASE reciprocal_review_lease_migration")
        raise
    connection.execute("RELEASE reciprocal_review_lease_migration")


def _validate_companion(connection: sqlite3.Connection) -> None:
    expected = {
        _MARKER,
        _ASSIGN,
        _STATE,
        _TOMB,
        _RECEIPT,
        _AUDIT,
        _OUTBOX,
        _STATE_QUEUE_INDEX,
        *_TRIGGER_DDLS,
    }
    actual = _companion_catalog(connection)
    if actual != expected:
        raise SqliteReciprocalReviewLeaseError(
            "review lease companion catalog가 canonical하지 않습니다."
        )
    if (
        any(not _same(_catalog_sql(connection, "table", name), ddl) for name, ddl in _DDLS.items())
        or any(
            not _same(_catalog_sql(connection, "index", name), ddl)
            for name, ddl in _INDEX_DDLS.items()
        )
        or any(
            not _same(_catalog_sql(connection, "trigger", name), ddl)
            for name, ddl in _TRIGGER_DDLS.items()
        )
    ):
        raise SqliteReciprocalReviewLeaseError(
            "review lease companion DDL가 canonical하지 않습니다."
        )
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SqliteReciprocalReviewLeaseError(
            "review lease companion FK가 canonical하지 않습니다."
        )
    marker = connection.execute(f"SELECT version FROM {_MARKER}").fetchall()
    if marker != [(3,)]:
        raise SqliteReciprocalReviewLeaseError(
            "review lease companion marker가 canonical하지 않습니다."
        )
    if any(
        tuple(column[1] for column in connection.execute(f"PRAGMA table_info({table})")) != columns
        for table, columns in _TABLE_COLUMNS.items()
    ):
        raise SqliteReciprocalReviewLeaseError(
            "review lease companion schema가 canonical하지 않습니다."
        )
    expected_foreign_keys = {
        _STATE: {(_ASSIGN, "org_id", "org_id"), (_ASSIGN, "review_run_id", "review_run_id")},
        _RECEIPT: {(_ASSIGN, "org_id", "org_id"), (_ASSIGN, "review_run_id", "review_run_id")},
        _AUDIT: {
            (_RECEIPT, "org_id", "org_id"),
            (_RECEIPT, "receipt_id", "receipt_id"),
            (_ASSIGN, "org_id", "org_id"),
            (_ASSIGN, "review_run_id", "review_run_id"),
        },
        _OUTBOX: {
            (_RECEIPT, "org_id", "org_id"),
            (_RECEIPT, "receipt_id", "receipt_id"),
            (_ASSIGN, "org_id", "org_id"),
            (_ASSIGN, "review_run_id", "review_run_id"),
        },
    }
    if any(
        {
            (row[2], row[3], row[4])
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        }
        != foreign_keys
        for table, foreign_keys in expected_foreign_keys.items()
    ):
        raise SqliteReciprocalReviewLeaseError(
            "review lease companion FK가 canonical하지 않습니다."
        )
    for row in connection.execute(
        f"SELECT owner_kind,owner_ref,lease_epoch,token_hash,expires_at,state,transition_digest FROM {_STATE}"
    ):
        owner_kind, owner_ref, epoch, token_hash, expires_at, state, transition_digest = row
        if not (
            type(epoch) is int
            and epoch >= 1
            and state in {"queued", "leased"}
            and (
                (
                    state == "queued"
                    and owner_kind is None
                    and owner_ref is None
                    and token_hash is None
                    and expires_at is None
                    and isinstance(transition_digest, str)
                )
                or (
                    state == "leased"
                    and owner_kind in {"human", "ai"}
                    and isinstance(owner_ref, str)
                    and isinstance(token_hash, str)
                    and len(token_hash) == 64
                    and isinstance(expires_at, str)
                    and isinstance(transition_digest, str)
                )
            )
        ):
            raise SqliteReciprocalReviewLeaseError("forged review lease state가 있습니다.")
    _validate_companion_rows(connection)


def _validate_companion_rows(connection: sqlite3.Connection) -> None:
    """Recheck all companion grammar and relationships after untrusted PRAGMAs."""

    def opaque(value: object) -> bool:
        return isinstance(value, str) and _OPAQUE_RE.fullmatch(value) is not None

    def digest(value: object) -> bool:
        return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None

    def timestamp(value: object) -> bool:
        return isinstance(value, str) and _TIME_RE.fullmatch(value) is not None

    assignments = {
        (row[0], row[1]): row[2:] for row in connection.execute(f"SELECT * FROM {_ASSIGN}")
    }
    for (org_id, run_id), row in assignments.items():
        cycle_id, requirement_id, kind, reviewer_ref, policy, provenance, assignment, created = row
        parent = connection.execute(
            "SELECT r.cycle_id,r.reviewer_kind,c.active,c.policy_digest,c.provenance_digest "
            "FROM durable_reciprocal_review_requirements r JOIN durable_reciprocal_review_cycles c "
            "ON c.org_id=r.org_id AND c.cycle_id=r.cycle_id "
            "WHERE r.org_id=? AND r.requirement_id=?",
            (org_id, requirement_id),
        ).fetchone()
        if not (
            opaque(org_id)
            and opaque(run_id)
            and opaque(cycle_id)
            and opaque(requirement_id)
            and kind in {"human", "ai"}
            and opaque(reviewer_ref)
            and digest(policy)
            and digest(provenance)
            and digest(assignment)
            and timestamp(created)
            and parent == (cycle_id, kind, 1, policy, provenance)
        ):
            raise SqliteReciprocalReviewLeaseError("forged review lease assignment가 있습니다.")
    states = {(row[0], row[1]): row[2:] for row in connection.execute(f"SELECT * FROM {_STATE}")}
    if set(states) != set(assignments):
        raise SqliteReciprocalReviewLeaseError(
            "review lease assignment/state 관계가 canonical하지 않습니다."
        )
    for row in states.values():
        owner_kind, owner_ref, epoch, token_hash, expires_at, state, transition_digest = row
        if not (
            type(epoch) is int
            and epoch >= 1
            and state in {"queued", "leased"}
            and (
                (
                    state == "queued"
                    and owner_kind is owner_ref is token_hash is expires_at is None
                    and digest(transition_digest)
                )
                or (
                    state == "leased"
                    and owner_kind in {"human", "ai"}
                    and opaque(owner_ref)
                    and digest(token_hash)
                    and timestamp(expires_at)
                    and digest(transition_digest)
                )
            )
        ):
            raise SqliteReciprocalReviewLeaseError("forged review lease state가 있습니다.")
    tombstones: dict[tuple[str, str], set[int]] = {}
    for org_id, run_id, epoch, token_hash, expired_at, transition_digest in connection.execute(
        f"SELECT * FROM {_TOMB}"
    ):
        state = states.get((org_id, run_id))
        if not (
            state is not None
            and opaque(org_id)
            and opaque(run_id)
            and type(epoch) is int
            and epoch >= 1
            and digest(token_hash)
            and timestamp(expired_at)
            and digest(transition_digest)
            and epoch < state[2]
        ):
            raise SqliteReciprocalReviewLeaseError("forged review lease tombstone가 있습니다.")
        tombstones.setdefault((org_id, run_id), set()).add(epoch)
    if any(tombstones.get(key, set()) != set(range(1, state[2])) for key, state in states.items()):
        raise SqliteReciprocalReviewLeaseError(
            "review lease reclaim tombstone epoch 관계가 canonical하지 않습니다."
        )
    receipts: dict[tuple[str, str], tuple[object, ...]] = {}
    for row in connection.execute(f"SELECT * FROM {_RECEIPT}"):
        (
            org_id,
            receipt_id,
            audit_id,
            outbox_id,
            command_digest,
            run_id,
            epoch,
            transition_type,
            prior_epoch,
            prior_state,
            expected_token_hash,
            expected_expires_at,
            new_token_hash,
            expires_at,
            db_time,
            predecessor,
            evidence_digest,
            created_at,
        ) = row
        state = states.get((org_id, run_id))
        if not (
            state is not None
            and opaque(org_id)
            and opaque(receipt_id)
            and opaque(audit_id)
            and opaque(outbox_id)
            and digest(command_digest)
            and opaque(run_id)
            and type(epoch) is int
            and epoch >= 1
            and epoch <= state[2]
            and transition_type in {"assign", "claim", "renew", "reclaim"}
            and type(prior_epoch) is int
            and prior_epoch >= 0
            and prior_state in {"absent", "queued", "leased"}
            and (expected_token_hash is None or digest(expected_token_hash))
            and (expected_expires_at is None or timestamp(expected_expires_at))
            and (new_token_hash is None or digest(new_token_hash))
            and (expires_at is None or timestamp(expires_at))
            and timestamp(db_time)
            and timestamp(created_at)
            and db_time == created_at
            and (predecessor is None or digest(predecessor))
            and digest(evidence_digest)
            and evidence_digest
            == _transition_digest(
                transition_type,
                run_id,
                prior_epoch,
                epoch,
                prior_state,
                expected_token_hash,
                expected_expires_at,
                new_token_hash,
                expires_at,
                db_time,
                predecessor,
            )
        ):
            raise SqliteReciprocalReviewLeaseError("forged review lease receipt가 있습니다.")
        receipts[(org_id, receipt_id)] = row
    for (org_id, run_id), state in states.items():
        run_receipts = [row for row in receipts.values() if row[0] == org_id and row[5] == run_id]
        by_anchor = {str(row[16]): row for row in run_receipts}
        if len(by_anchor) != len(run_receipts):
            raise SqliteReciprocalReviewLeaseError(
                "review lease transition anchor가 중복되었습니다."
            )
        roots = [row for row in run_receipts if row[15] is None]
        if len(roots) != 1 or roots[0][7] != "assign":
            raise SqliteReciprocalReviewLeaseError(
                "review lease assignment root가 canonical하지 않습니다."
            )
        successors: dict[str, tuple[object, ...]] = {}
        for receipt in run_receipts:
            predecessor = receipt[15]
            if predecessor is None:
                continue
            if (
                not isinstance(predecessor, str)
                or predecessor not in by_anchor
                or predecessor in successors
            ):
                raise SqliteReciprocalReviewLeaseError(
                    "review lease receipt chain가 canonical하지 않습니다."
                )
            prior = by_anchor[predecessor]
            if not (
                (receipt[9], receipt[8], receipt[10], receipt[11])
                == (_result_state(str(prior[7])), prior[6], prior[12], prior[13])
            ):
                raise SqliteReciprocalReviewLeaseError(
                    "review lease receipt predecessor가 stale입니다."
                )
            if not _valid_transition(prior, receipt):
                raise SqliteReciprocalReviewLeaseError(
                    "review lease receipt transition가 canonical하지 않습니다."
                )
            successors[str(predecessor)] = receipt
        root = roots[0]
        if not (root[8:14] == (0, "absent", None, None, None, None) and root[6] == 1):
            raise SqliteReciprocalReviewLeaseError(
                "review lease assignment root가 forged되었습니다."
            )
        visited: list[tuple[object, ...]] = []
        current = root
        while True:
            visited.append(current)
            successor = successors.get(str(current[16]))
            if successor is None:
                break
            current = successor
        if len(visited) != len(run_receipts) or str(current[16]) != state[6]:
            raise SqliteReciprocalReviewLeaseError(
                "review lease receipt chain가 current anchor로 끝나지 않습니다."
            )
        if not (
            current[6] == state[2]
            and current[12] == state[3]
            and current[13] == state[4]
            and _result_state(str(current[7])) == state[5]
        ):
            raise SqliteReciprocalReviewLeaseError(
                "forged review lease state transition가 있습니다."
            )
    for org_id, run_id, epoch, token_hash, expired_at, transition_digest in connection.execute(
        f"SELECT * FROM {_TOMB}"
    ):
        receipt = next(
            (
                row
                for row in receipts.values()
                if row[0] == org_id
                and row[5] == run_id
                and row[7] == "reclaim"
                and row[8] == epoch
                and row[16] == transition_digest
            ),
            None,
        )
        if receipt is None or receipt[10] != token_hash or receipt[11] != expired_at:
            raise SqliteReciprocalReviewLeaseError(
                "forged review lease reclaim tombstone가 있습니다."
            )
    for table, identifier, value_column in (
        (_AUDIT, "audit_id", "event_digest"),
        (_OUTBOX, "outbox_id", "payload_digest"),
    ):
        records: dict[tuple[str, str], tuple[str, str, int, str, str]] = {}
        for (
            org_id,
            record_id,
            receipt_id,
            command_digest,
            run_id,
            epoch,
            value,
            created_at,
        ) in connection.execute(f"SELECT * FROM {table}"):
            receipt = receipts.get((org_id, receipt_id))
            if not (
                (org_id, run_id) in assignments
                and opaque(org_id)
                and opaque(record_id)
                and opaque(receipt_id)
                and opaque(run_id)
                and digest(command_digest)
                and type(epoch) is int
                and epoch >= 1
                and digest(value)
                and timestamp(created_at)
                and receipt is not None
                and record_id == receipt[2 if table == _AUDIT else 3]
                and (command_digest, run_id, epoch, created_at)
                == (receipt[4], receipt[5], receipt[6], receipt[17])
                and value == _evidence_record_digest(str(receipt[16]), str(receipt[7]), table)
            ):
                raise SqliteReciprocalReviewLeaseError(
                    f"forged review lease {identifier}/{value_column}가 있습니다."
                )
            records[(org_id, receipt_id)] = (record_id, command_digest, epoch, value, created_at)
        if set(records) != set(receipts):
            raise SqliteReciprocalReviewLeaseError(
                f"review lease receipt/{identifier} 관계가 canonical하지 않습니다."
            )


def _validate_boundaries(connection: sqlite3.Connection) -> None:
    try:
        validate_sqlite_durable_reciprocal_review_ledger(connection)
    except RuntimeError as error:
        raise SqliteReciprocalReviewLeaseError(
            "review lease parent ledger가 unavailable입니다."
        ) from error
    _validate_companion(connection)


def validate_sqlite_reciprocal_review_lease(connection: sqlite3.Connection) -> None:
    """Validate the sealed lease catalog for a dependent fenced write."""
    _validate_boundaries(connection)


def validate_active_review_lease_proof(
    connection: sqlite3.Connection, proof: ActiveReviewLeaseProof, *, now: datetime
) -> None:
    """Recheck an already full-token-validated proof without receiving that token again."""
    row = connection.execute(
        f"SELECT s.token_hash,r.state FROM {_STATE} s "
        "JOIN durable_reciprocal_review_runs r ON r.org_id=s.org_id AND r.review_run_id=s.review_run_id "
        "WHERE s.org_id=? AND s.review_run_id=? AND s.owner_kind=? AND s.owner_ref=? "
        "AND s.lease_epoch=? AND s.state='leased' AND s.expires_at>?",
        (
            proof.org_id,
            proof.review_run_id,
            proof.reviewer_kind,
            proof.reviewer_ref,
            proof.lease_epoch,
            _time(now),
        ),
    ).fetchone()
    if row is None or row[1] != "leased" or not hmac.compare_digest(row[0], proof.token_hash):
        raise SqliteReciprocalReviewLeaseConflict("lease proof가 stale입니다.")


class SqliteReciprocalReviewLeaseUnitOfWork:
    def __init__(
        self,
        path: str | Path,
        *,
        reviewer_authorization: ReviewerAssignmentAuthorization,
        policy_snapshot: ReviewPolicySnapshot,
        db_time: DbTransactionTime,
        token_key: bytes,
        fault_injector: Callable[[str], None] | None,
        _capability: object,
    ) -> None:
        if _capability is not _CAPABILITY or not token_key:
            raise TypeError("Use create_sqlite_reciprocal_review_lease_uow().")
        self._path = Path(path)
        self._authorization = reviewer_authorization
        self._policy = policy_snapshot
        self._db_time = db_time
        self._token_key = token_key
        self._fault_injector = fault_injector

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def assign(self, command: HumanReviewerAssignment | AiReviewerAssignment) -> ReviewLease:
        reviewer = command.reviewer
        kind, ref = _reviewer_ref(reviewer)
        semantic = (
            "assign",
            command.review_run_id,
            command.cycle_id,
            command.requirement_id,
            kind,
            ref,
        )
        return self._write(
            reviewer.org_id,
            command.receipt_id,
            command.audit_id,
            command.outbox_id,
            semantic,
            lambda c, now: self._assign(c, command, kind, ref, now),
        )

    def claim(self, command: ClaimReviewRun) -> ReviewLease:
        self._lease_duration(command.lease_for)
        kind, ref = _reviewer_ref(command.reviewer)
        semantic = ("claim", command.review_run_id, kind, ref, command.lease_for.total_seconds())
        return self._write(
            command.reviewer.org_id,
            command.receipt_id,
            command.audit_id,
            command.outbox_id,
            semantic,
            lambda c, now: self._claim(c, command, kind, ref, now),
        )

    def renew(self, command: RenewReviewLease) -> ReviewLease:
        self._lease_duration(command.lease_for)
        kind, ref = _reviewer_ref(command.reviewer)
        # The token is represented only by a keyed digest in receipt semantics.
        token_hash = self._hash(command.lease_token)
        semantic = (
            "renew",
            command.review_run_id,
            kind,
            ref,
            command.lease_epoch,
            token_hash,
            command.lease_for.total_seconds(),
        )
        return self._write(
            command.reviewer.org_id,
            command.receipt_id,
            command.audit_id,
            command.outbox_id,
            semantic,
            lambda c, now: self._renew(c, command, kind, ref, token_hash, now),
        )

    def validate_active_lease(
        self, *, reviewer: Reviewer, review_run_id: str, lease_epoch: int, lease_token: str
    ) -> ActiveReviewLeaseProof:
        """Verify a full token once, returning only a hash-bound sealed proof."""
        kind, ref = _reviewer_ref(reviewer)
        token_hash = self._hash(lease_token)
        c = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE")
            _validate_boundaries(c)
            now = _time(self._db_time(c))
            if c.execute(
                "SELECT state FROM durable_reciprocal_review_runs WHERE org_id=? AND review_run_id=?",
                (reviewer.org_id, review_run_id),
            ).fetchone() == ("recorded",):
                raise SqliteReciprocalReviewLeaseConflict(
                    "recorded review run은 lease proof를 사용할 수 없습니다."
                )
            row = c.execute(
                f"SELECT token_hash FROM {_STATE} WHERE org_id=? AND review_run_id=? "
                "AND owner_kind=? AND owner_ref=? AND lease_epoch=? AND state='leased' "
                "AND expires_at>?",
                (reviewer.org_id, review_run_id, kind, ref, lease_epoch, now),
            ).fetchone()
            self._current_assignment(c, reviewer.org_id, review_run_id)
            if row is None or not hmac.compare_digest(row[0], token_hash):
                raise SqliteReciprocalReviewLeaseConflict(
                    "lease owner/epoch/expiry/token이 stale입니다."
                )
            c.commit()
            return ActiveReviewLeaseProof(
                reviewer.org_id, review_run_id, kind, ref, lease_epoch, token_hash
            )
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    def validate_active_lease_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        reviewer: Reviewer,
        review_run_id: str,
        lease_epoch: int,
        lease_token: str,
    ) -> tuple[ActiveReviewLeaseProof, datetime]:
        """The only full-token fence usable by a dependent atomic write."""
        kind, ref = _reviewer_ref(reviewer)
        token_hash = self._hash(lease_token)
        now = self._db_time(connection)
        if connection.execute(
            "SELECT state FROM durable_reciprocal_review_runs WHERE org_id=? AND review_run_id=?",
            (reviewer.org_id, review_run_id),
        ).fetchone() == ("recorded",):
            raise SqliteReciprocalReviewLeaseConflict(
                "recorded review run은 lease proof를 사용할 수 없습니다."
            )
        row = connection.execute(
            f"SELECT token_hash FROM {_STATE} WHERE org_id=? AND review_run_id=? "
            "AND owner_kind=? AND owner_ref=? AND lease_epoch=? AND state='leased' "
            "AND expires_at>?",
            (reviewer.org_id, review_run_id, kind, ref, lease_epoch, _time(now)),
        ).fetchone()
        self._current_assignment(connection, reviewer.org_id, review_run_id)
        if row is None or not hmac.compare_digest(row[0], token_hash):
            raise SqliteReciprocalReviewLeaseConflict(
                "lease owner/epoch/expiry/token이 stale입니다."
            )
        return ActiveReviewLeaseProof(
            reviewer.org_id, review_run_id, kind, ref, lease_epoch, token_hash
        ), now

    def _write(
        self,
        org_id: str,
        receipt_id: str,
        audit_id: str,
        outbox_id: str,
        semantic: tuple[object, ...],
        operation: Callable[[sqlite3.Connection, datetime], tuple[ReviewLease, _LeaseTransition]],
    ) -> ReviewLease:
        digest = _digest(semantic)
        c = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE")
            _validate_boundaries(c)
            now = self._db_time(c)
            now_text = _time(now)
            old = c.execute(
                f"SELECT command_digest,review_run_id,lease_epoch FROM {_RECEIPT} WHERE org_id=? AND receipt_id=?",
                (org_id, receipt_id),
            ).fetchone()
            if old is not None:
                if old[0] != digest:
                    raise SqliteReciprocalReviewLeaseConflict(
                        "receipt semantic command가 다릅니다."
                    )
                row = c.execute(
                    f"SELECT expires_at FROM {_STATE} WHERE org_id=? AND review_run_id=?",
                    (org_id, old[1]),
                ).fetchone()
                if row is None:
                    raise SqliteReciprocalReviewLeaseError("receipt lease 상태가 없습니다.")
                c.commit()
                return ReviewLease(org_id, old[1], old[2], _dt(row[0]) if row[0] else now, None)
            result, transition = operation(c, now)
            self._fault("after_operation")
            prior_anchor_row = c.execute(
                f"SELECT transition_digest FROM {_STATE} WHERE org_id=? AND review_run_id=?",
                (org_id, result.review_run_id),
            ).fetchone()
            predecessor = None if transition.transition_type == "assign" else prior_anchor_row[0]
            if transition.transition_type != "assign" and not isinstance(predecessor, str):
                raise SqliteReciprocalReviewLeaseError(
                    "lease predecessor transition anchor가 없습니다."
                )
            evidence_digest = _transition_digest(
                transition.transition_type,
                result.review_run_id,
                transition.prior_epoch,
                result.lease_epoch,
                transition.prior_state,
                transition.expected_token_hash,
                transition.expected_expires_at,
                transition.new_token_hash,
                transition.expires_at,
                transition.db_time,
                predecessor,
            )
            c.execute(
                f"INSERT INTO {_RECEIPT} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    org_id,
                    receipt_id,
                    audit_id,
                    outbox_id,
                    digest,
                    result.review_run_id,
                    result.lease_epoch,
                    transition.transition_type,
                    transition.prior_epoch,
                    transition.prior_state,
                    transition.expected_token_hash,
                    transition.expected_expires_at,
                    transition.new_token_hash,
                    transition.expires_at,
                    transition.db_time,
                    predecessor,
                    evidence_digest,
                    now_text,
                ),
            )
            if (
                c.execute(
                    f"UPDATE {_STATE} SET transition_digest=? WHERE org_id=? AND review_run_id=? "
                    "AND lease_epoch=? AND token_hash IS ? AND expires_at IS ?",
                    (
                        evidence_digest,
                        org_id,
                        result.review_run_id,
                        result.lease_epoch,
                        transition.new_token_hash,
                        transition.expires_at,
                    ),
                ).rowcount
                != 1
            ):
                raise SqliteReciprocalReviewLeaseError("lease transition anchor가 stale입니다.")
            if (
                transition.transition_type == "reclaim"
                and c.execute(
                    f"UPDATE {_TOMB} SET transition_digest=? WHERE org_id=? AND review_run_id=? "
                    "AND lease_epoch=? AND token_hash=? AND transition_digest IS NULL",
                    (
                        evidence_digest,
                        org_id,
                        result.review_run_id,
                        transition.prior_epoch,
                        transition.expected_token_hash,
                    ),
                ).rowcount
                != 1
            ):
                raise SqliteReciprocalReviewLeaseError("reclaim tombstone evidence가 stale입니다.")
            self._fault("after_receipt")
            c.execute(
                f"INSERT INTO {_AUDIT} VALUES(?,?,?,?,?,?,?,?)",
                (
                    org_id,
                    audit_id,
                    receipt_id,
                    digest,
                    result.review_run_id,
                    result.lease_epoch,
                    _evidence_record_digest(evidence_digest, transition.transition_type, _AUDIT),
                    now_text,
                ),
            )
            self._fault("after_audit")
            c.execute(
                f"INSERT INTO {_OUTBOX} VALUES(?,?,?,?,?,?,?,?)",
                (
                    org_id,
                    outbox_id,
                    receipt_id,
                    digest,
                    result.review_run_id,
                    result.lease_epoch,
                    _evidence_record_digest(evidence_digest, transition.transition_type, _OUTBOX),
                    now_text,
                ),
            )
            self._fault("after_outbox")
            # External authorization, policy, and transaction-time callbacks have all
            # returned.  Drift anywhere in either sealed catalog aborts every write.
            _validate_boundaries(c)
            c.commit()
            return result
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    def _assign(
        self,
        c: sqlite3.Connection,
        command: HumanReviewerAssignment | AiReviewerAssignment,
        kind: str,
        ref: str,
        now: datetime,
    ) -> tuple[ReviewLease, _LeaseTransition]:
        row = c.execute(
            "SELECT r.reviewer_kind,c.policy_digest,c.provenance_digest FROM durable_reciprocal_review_requirements r JOIN durable_reciprocal_review_cycles c ON c.org_id=r.org_id AND c.cycle_id=r.cycle_id WHERE r.org_id=? AND r.requirement_id=? AND r.cycle_id=? AND c.active=1",
            (command.reviewer.org_id, command.requirement_id, command.cycle_id),
        ).fetchone()
        if (
            row is None
            or row[0] != kind
            or not self._policy.requirement_is_current(
                org_id=command.reviewer.org_id,
                cycle_id=command.cycle_id,
                requirement_id=command.requirement_id,
                policy_digest=row[1],
                provenance_digest=row[2],
            )
        ):
            raise SqliteReciprocalReviewLeaseConflict(
                "cycle/requirement/policy/provenance snapshot drift가 있습니다."
            )
        if isinstance(command, HumanReviewerAssignment):
            contributors = tuple(
                sorted(
                    {
                        provenance[0]
                        for provenance in c.execute(
                            "SELECT p.principal_ref "
                            "FROM durable_reciprocal_review_cycles c "
                            "JOIN durable_reciprocal_review_lineage_members l "
                            "ON l.org_id=c.org_id AND l.revision_id=c.revision_id "
                            "JOIN durable_reciprocal_review_provenance_events p "
                            "ON p.org_id=l.org_id AND p.event_id=l.event_id "
                            "WHERE c.org_id=? AND c.cycle_id=? AND p.principal_kind='human' "
                            "ORDER BY p.principal_ref",
                            (command.reviewer.org_id, command.cycle_id),
                        )
                    }
                )
            )
            allowed = self._authorization.authorize_human_reviewer(
                reviewer=command.reviewer, contributor_subject_ids=contributors
            )
        else:
            allowed = self._authorization.authorize_ai_reviewer(reviewer=command.reviewer)
        if not allowed:
            raise SqliteReciprocalReviewLeaseConflict(
                "reviewer assignment 권한 또는 독립성이 없습니다."
            )
        # Recheck the exact durable provenance after the external authorizer returns,
        # before this transaction creates its first row.
        _validate_boundaries(c)
        assignment = _digest(
            (
                command.review_run_id,
                command.cycle_id,
                command.requirement_id,
                kind,
                ref,
                row[1],
                row[2],
            )
        )
        try:
            c.execute(
                "INSERT INTO durable_reciprocal_review_runs VALUES(?,?,?,?,?,?,?,?)",
                (
                    command.reviewer.org_id,
                    command.review_run_id,
                    command.requirement_id,
                    1,
                    1,
                    self._hash(secrets.token_urlsafe(32)),
                    "queued",
                    _time(now),
                ),
            )
            self._fault("after_run")
            c.execute(
                f"INSERT INTO {_ASSIGN} VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    command.reviewer.org_id,
                    command.review_run_id,
                    command.cycle_id,
                    command.requirement_id,
                    kind,
                    ref,
                    row[1],
                    row[2],
                    assignment,
                    _time(now),
                ),
            )
            self._fault("after_assignment")
            c.execute(
                f"INSERT INTO {_STATE} VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    command.reviewer.org_id,
                    command.review_run_id,
                    None,
                    None,
                    1,
                    None,
                    None,
                    "queued",
                    None,
                ),
            )
            self._fault("after_lease_state")
        except sqlite3.IntegrityError as error:
            raise SqliteReciprocalReviewLeaseConflict(
                "review run assignment 충돌입니다."
            ) from error
        return (
            ReviewLease(command.reviewer.org_id, command.review_run_id, 1, now, None),
            _LeaseTransition("assign", 0, "absent", None, None, None, None, _time(now)),
        )

    def _claim(
        self, c: sqlite3.Connection, command: ClaimReviewRun, kind: str, ref: str, now: datetime
    ) -> tuple[ReviewLease, _LeaseTransition]:
        self._current_assignment(c, command.reviewer.org_id, command.review_run_id)
        if c.execute(
            "SELECT state FROM durable_reciprocal_review_runs WHERE org_id=? AND review_run_id=?",
            (command.reviewer.org_id, command.review_run_id),
        ).fetchone() == ("recorded",):
            raise SqliteReciprocalReviewLeaseConflict(
                "recorded review run은 다시 claim할 수 없습니다."
            )
        row = c.execute(
            f"SELECT a.reviewer_kind,a.reviewer_ref,s.lease_epoch,s.token_hash,s.expires_at,s.state FROM {_ASSIGN} a JOIN {_STATE} s USING(org_id,review_run_id) WHERE a.org_id=? AND a.review_run_id=?",
            (command.reviewer.org_id, command.review_run_id),
        ).fetchone()
        if row is None or row[0] != kind or row[1] != ref:
            raise SqliteReciprocalReviewLeaseConflict("assigned reviewer가 아닙니다.")
        epoch = row[2]
        expired = row[5] == "leased" and (row[4] is None or _dt(row[4]) <= now)
        if row[5] == "leased" and not expired:
            raise SqliteReciprocalReviewLeaseConflict("active lease가 있습니다.")
        if expired:
            c.execute(
                f"INSERT INTO {_TOMB} VALUES(?,?,?,?,?,?)",
                (command.reviewer.org_id, command.review_run_id, epoch, row[3], row[4], None),
            )
            self._fault("after_tombstone")
            epoch += 1
        token = secrets.token_urlsafe(32)
        token_hash = self._hash(token)
        expiry = now + command.lease_for
        changed = c.execute(
            f"UPDATE {_STATE} SET owner_kind=?,owner_ref=?,lease_epoch=?,token_hash=?,expires_at=?,state='leased' WHERE org_id=? AND review_run_id=? AND lease_epoch=? AND state=? AND (expires_at IS NULL OR expires_at<=?)",
            (
                kind,
                ref,
                epoch,
                token_hash,
                _time(expiry),
                command.reviewer.org_id,
                command.review_run_id,
                row[2],
                row[5],
                _time(now),
            ),
        ).rowcount
        if changed != 1:
            raise SqliteReciprocalReviewLeaseConflict("lease CAS가 stale입니다.")
        run_state = c.execute(
            "SELECT state FROM durable_reciprocal_review_runs WHERE org_id=? AND review_run_id=?",
            (command.reviewer.org_id, command.review_run_id),
        ).fetchone()
        if (
            run_state == ("queued",)
            and c.execute(
                "UPDATE durable_reciprocal_review_runs SET state='leased' WHERE org_id=? AND review_run_id=? AND state='queued'",
                (command.reviewer.org_id, command.review_run_id),
            ).rowcount
            != 1
        ):
            raise SqliteReciprocalReviewLeaseConflict("review run terminal state가 stale입니다.")
        self._fault("after_claim_cas")
        return (
            ReviewLease(command.reviewer.org_id, command.review_run_id, epoch, expiry, token),
            _LeaseTransition(
                "reclaim" if expired else "claim",
                row[2],
                row[5],
                row[3],
                row[4],
                token_hash,
                _time(expiry),
                _time(now),
            ),
        )

    def _renew(
        self,
        c: sqlite3.Connection,
        command: RenewReviewLease,
        kind: str,
        ref: str,
        token_hash: str,
        now: datetime,
    ) -> tuple[ReviewLease, _LeaseTransition]:
        self._current_assignment(c, command.reviewer.org_id, command.review_run_id)
        if c.execute(
            "SELECT state FROM durable_reciprocal_review_runs WHERE org_id=? AND review_run_id=?",
            (command.reviewer.org_id, command.review_run_id),
        ).fetchone() == ("recorded",):
            raise SqliteReciprocalReviewLeaseConflict("recorded review run은 renew할 수 없습니다.")
        expiry = now + command.lease_for
        row = c.execute(
            f"SELECT token_hash,expires_at FROM {_STATE} WHERE org_id=? AND review_run_id=? AND owner_kind=? AND owner_ref=? AND lease_epoch=? AND state='leased' AND expires_at>?",
            (
                command.reviewer.org_id,
                command.review_run_id,
                kind,
                ref,
                command.lease_epoch,
                _time(now),
            ),
        ).fetchone()
        if row is None or not hmac.compare_digest(row[0], token_hash):
            raise SqliteReciprocalReviewLeaseConflict(
                "lease owner/epoch/expiry/token이 stale입니다."
            )
        if (
            c.execute(
                f"UPDATE {_STATE} SET expires_at=? WHERE org_id=? AND review_run_id=? AND owner_kind=? AND owner_ref=? AND lease_epoch=? AND token_hash=? AND expires_at>?",
                (
                    _time(expiry),
                    command.reviewer.org_id,
                    command.review_run_id,
                    kind,
                    ref,
                    command.lease_epoch,
                    token_hash,
                    _time(now),
                ),
            ).rowcount
            != 1
        ):
            raise SqliteReciprocalReviewLeaseConflict("lease renew CAS가 stale입니다.")
        self._fault("after_renew_cas")
        return (
            ReviewLease(
                command.reviewer.org_id, command.review_run_id, command.lease_epoch, expiry, None
            ),
            _LeaseTransition(
                "renew",
                command.lease_epoch,
                "leased",
                token_hash,
                row[1],
                token_hash,
                _time(expiry),
                _time(now),
            ),
        )

    def _current_assignment(
        self, connection: sqlite3.Connection, org_id: str, review_run_id: str
    ) -> None:
        row = connection.execute(
            f"SELECT a.cycle_id,a.requirement_id,a.policy_digest,a.provenance_digest,"
            "r.cycle_id,r.reviewer_kind,c.policy_digest,c.provenance_digest,c.active "
            f"FROM {_ASSIGN} a JOIN durable_reciprocal_review_requirements r "
            "ON r.org_id=a.org_id AND r.requirement_id=a.requirement_id "
            "JOIN durable_reciprocal_review_cycles c "
            "ON c.org_id=r.org_id AND c.cycle_id=r.cycle_id "
            "WHERE a.org_id=? AND a.review_run_id=?",
            (org_id, review_run_id),
        ).fetchone()
        if (
            row is None
            or row[0] != row[4]
            or row[5] not in {"human", "ai"}
            or row[2] != row[6]
            or row[3] != row[7]
            or row[8] != 1
            or not self._policy.requirement_is_current(
                org_id=org_id,
                cycle_id=row[0],
                requirement_id=row[1],
                policy_digest=row[2],
                provenance_digest=row[3],
            )
        ):
            raise SqliteReciprocalReviewLeaseConflict(
                "review requirement cycle/policy/provenance snapshot drift가 있습니다."
            )

    def _hash(self, token: str) -> str:
        return hmac.new(self._token_key, token.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _lease_duration(value: timedelta) -> None:
        if value <= timedelta() or value > timedelta(days=1):
            raise SqliteReciprocalReviewLeaseError("lease duration이 유효하지 않습니다.")


def create_sqlite_reciprocal_review_lease_uow(
    path: str | Path,
    *,
    reviewer_authorization: ReviewerAssignmentAuthorization,
    policy_snapshot: ReviewPolicySnapshot,
    db_time: DbTransactionTime,
    token_key: bytes,
    fault_injector: Callable[[str], None] | None = None,
) -> SqliteReciprocalReviewLeaseUnitOfWork:
    return SqliteReciprocalReviewLeaseUnitOfWork(
        path,
        reviewer_authorization=reviewer_authorization,
        policy_snapshot=policy_snapshot,
        db_time=db_time,
        token_key=token_key,
        fault_injector=fault_injector,
        _capability=_CAPABILITY,
    )
