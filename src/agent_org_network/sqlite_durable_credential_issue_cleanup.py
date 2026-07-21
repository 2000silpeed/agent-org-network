"""R4 durable cleanup reconciliation for persisted credential issue targets.

This module never stages, releases, or materializes a credential.  It only
retries an opaque delivery abort for an already persisted ``CleanupPending``
target/fence pair.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from datetime import UTC, datetime

from agent_org_network.sqlite_durable_credential_issue_targets import (
    open_sqlite_durable_credential_issue_targets_connection,
    validate_sqlite_durable_credential_issue_targets_connection,
)

COMPONENT_ID: Final = "durable_credential_issue_cleanup_v1"
SCHEMA_VERSION: Final = 1
_INTENTS: Final = "credential_issue_cleanup_intents_v1"
_ATTEMPTS: Final = "credential_issue_cleanup_attempts_v1"
_RESULTS: Final = "credential_issue_cleanup_results_v1"
_FENCE_REF_INDEX: Final = "credential_issue_cleanup_fence_ref"
_LOCK_GUARD = threading.Lock()
_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_TIME_CHECK: Final = "length({field})=24 AND {field} GLOB '????-??-??T??:??:??.???Z' AND COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', {field})={field},0)"
_CREATED_AT_CHECK: Final = _TIME_CHECK.format(field="created_at")
_RECORDED_AT_CHECK: Final = _TIME_CHECK.format(field="recorded_at")

_INTENTS_DDL: Final = f"""CREATE TABLE {_INTENTS} (
 org_id TEXT NOT NULL COLLATE BINARY, target_id TEXT NOT NULL COLLATE BINARY,
 delivery_ref TEXT NOT NULL COLLATE BINARY CHECK(length(delivery_ref)=76 AND substr(delivery_ref,1,12)='delivery:v1:'),
 created_at TEXT NOT NULL COLLATE BINARY CHECK({_CREATED_AT_CHECK}),
 PRIMARY KEY(org_id,target_id),
 UNIQUE(org_id,target_id,delivery_ref),
 FOREIGN KEY(org_id,target_id) REFERENCES durable_credential_issue_targets_v1(org_id,target_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(org_id,target_id,delivery_ref) REFERENCES credential_issue_stage_fences_v2(org_id,target_id,delivery_ref) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_ATTEMPTS_DDL: Final = f"""CREATE TABLE {_ATTEMPTS} (
 org_id TEXT NOT NULL COLLATE BINARY, target_id TEXT NOT NULL COLLATE BINARY, attempt INTEGER NOT NULL CHECK(attempt>=1),
 delivery_ref TEXT NOT NULL COLLATE BINARY CHECK(length(delivery_ref)=76 AND substr(delivery_ref,1,12)='delivery:v1:'),
 created_at TEXT NOT NULL COLLATE BINARY CHECK({_CREATED_AT_CHECK}),
 PRIMARY KEY(org_id,target_id,attempt),
 UNIQUE(org_id,target_id,attempt,delivery_ref),
 FOREIGN KEY(org_id,target_id,delivery_ref) REFERENCES {_INTENTS}(org_id,target_id,delivery_ref) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_RESULTS_DDL: Final = f"""CREATE TABLE {_RESULTS} (
 org_id TEXT NOT NULL COLLATE BINARY, target_id TEXT NOT NULL COLLATE BINARY, attempt INTEGER NOT NULL CHECK(attempt>=1),
 delivery_ref TEXT NOT NULL COLLATE BINARY CHECK(length(delivery_ref)=76 AND substr(delivery_ref,1,12)='delivery:v1:'),
 outcome TEXT NOT NULL COLLATE BINARY CHECK(outcome IN ('aborted','unavailable')), recorded_at TEXT NOT NULL COLLATE BINARY CHECK({_RECORDED_AT_CHECK}),
 PRIMARY KEY(org_id,target_id,attempt),
 FOREIGN KEY(org_id,target_id,attempt,delivery_ref) REFERENCES {_ATTEMPTS}(org_id,target_id,attempt,delivery_ref) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_DDLS: Final = (_INTENTS_DDL, _ATTEMPTS_DDL, _RESULTS_DDL)
_FENCE_REF_INDEX_DDL: Final = f"CREATE UNIQUE INDEX {_FENCE_REF_INDEX} ON credential_issue_stage_fences_v2(org_id,target_id,delivery_ref)"
_IMMUTABLE_TRIGGERS: Final = tuple(
    (
        f"{table}_{operation}_immutable",
        f"CREATE TRIGGER {table}_{operation}_immutable BEFORE {operation.upper()} ON {table} FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'immutable cleanup evidence'); END",
    )
    for table in (_INTENTS, _ATTEMPTS, _RESULTS)
    for operation in ("update", "delete")
)


class SqliteCredentialIssueCleanupError(RuntimeError):
    pass


class CredentialDeliveryAbort(Protocol):
    def abort(self, delivery_ref: str) -> None: ...


type CleanupFaultInjector = Callable[[str], None]


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _same(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()) == " ".join(expected.split())


def _timestamp(value: object) -> bool:
    if type(value) is not str or len(value) != 24:
        return False
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
            .replace(tzinfo=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
            == value
        )
    except ValueError:
        return False


def _manifest() -> str:
    return _canonical(
        {
            "component_id": COMPONENT_ID,
            "version": SCHEMA_VERSION,
            "tables": [_INTENTS, _ATTEMPTS, _RESULTS],
            "index": _FENCE_REF_INDEX,
            "triggers": [name for name, _ddl in _IMMUTABLE_TRIGGERS],
        }
    )


def _validate(connection: sqlite3.Connection) -> None:
    try:
        validate_sqlite_durable_credential_issue_targets_connection(connection)
    except Exception as error:
        raise SqliteCredentialIssueCleanupError(
            "R4에는 canonical v2 issue target parent가 필요합니다."
        ) from error
    marker = connection.execute(
        "SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id=?",
        (COMPONENT_ID,),
    ).fetchone()
    manifest = _manifest()
    if marker is None or tuple(marker) != (SCHEMA_VERSION, manifest, _digest(manifest)):
        raise SqliteCredentialIssueCleanupError("R4 cleanup manifest가 canonical하지 않습니다.")
    for name, ddl in zip((_INTENTS, _ATTEMPTS, _RESULTS), _DDLS, strict=True):
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if row is None or not _same(row[0], ddl):
            raise SqliteCredentialIssueCleanupError("R4 cleanup catalog가 canonical하지 않습니다.")
    index = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (_FENCE_REF_INDEX,)
    ).fetchone()
    if index is None or not _same(index[0], _FENCE_REF_INDEX_DDL):
        raise SqliteCredentialIssueCleanupError(
            "R4 cleanup fence-ref index가 canonical하지 않습니다."
        )
    for name, ddl in _IMMUTABLE_TRIGGERS:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if trigger is None or not _same(trigger[0], ddl):
            raise SqliteCredentialIssueCleanupError(
                "R4 cleanup immutable trigger가 canonical하지 않습니다."
            )
    for table, field in (
        (_INTENTS, "created_at"),
        (_ATTEMPTS, "created_at"),
        (_RESULTS, "recorded_at"),
    ):
        if any(
            not _timestamp(row[0]) for row in connection.execute(f"SELECT {field} FROM {table}")
        ):
            raise SqliteCredentialIssueCleanupError(
                "R4 cleanup timestamp row가 canonical하지 않습니다."
            )
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SqliteCredentialIssueCleanupError("R4 cleanup foreign key가 손상되었습니다.")


def migrate_sqlite_durable_credential_issue_cleanup_schema(
    connection: sqlite3.Connection, *, fault_injector: Callable[[str], None] | None = None
) -> None:
    validate_sqlite_durable_credential_issue_targets_connection(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone()
        present = connection.execute(
            "SELECT name FROM sqlite_schema WHERE name IN (?,?,?,?,?,?,?,?,?,?)",
            (
                *(_INTENTS, _ATTEMPTS, _RESULTS, _FENCE_REF_INDEX),
                *(name for name, _ddl in _IMMUTABLE_TRIGGERS),
            ),
        ).fetchall()
        if exists is not None or present:
            _validate(connection)
            connection.commit()
            return
        for ddl, point in zip(
            _DDLS, ("after_intents", "after_attempts", "after_results"), strict=True
        ):
            connection.execute(ddl)
            if fault_injector:
                fault_injector(point)
        connection.execute(_FENCE_REF_INDEX_DDL)
        for _name, ddl in _IMMUTABLE_TRIGGERS:
            connection.execute(ddl)
        if fault_injector:
            fault_injector("before_manifest")
        manifest = _manifest()
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES(?,?,?,?)",
            (COMPONENT_ID, SCHEMA_VERSION, manifest, _digest(manifest)),
        )
        if fault_injector:
            fault_injector("after_manifest")
        _validate(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def open_sqlite_durable_credential_issue_cleanup_connection(path: str | Path) -> sqlite3.Connection:
    try:
        connection = open_sqlite_durable_credential_issue_targets_connection(path)
    except Exception as error:
        raise SqliteCredentialIssueCleanupError(
            "R4 parent capability가 unavailable입니다."
        ) from error
    try:
        _validate(connection)
        return connection
    except Exception:
        connection.close()
        raise


def _lock(path: str | Path, org_id: str, target_id: str) -> threading.Lock:
    key = (str(Path(path).expanduser().resolve()), org_id, target_id)
    with _LOCK_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


@dataclass(frozen=True)
class CleanupReconciliation:
    target_id: str
    attempted: bool
    cleaned: bool


def reconcile_sqlite_durable_credential_issue_cleanup(
    path: str | Path,
    org_id: str,
    target_id: str,
    now: str,
    aborter: CredentialDeliveryAbort,
    *,
    fault_injector: CleanupFaultInjector | None = None,
) -> CleanupReconciliation:
    """Durably record an attempt before aborting one persisted opaque reference."""
    if not callable(getattr(aborter, "abort", None)):
        raise SqliteCredentialIssueCleanupError("delivery abort capability가 필요합니다.")
    if not _timestamp(now):
        raise SqliteCredentialIssueCleanupError("cleanup timestamp가 canonical하지 않습니다.")
    with _lock(path, org_id, target_id):
        connection = open_sqlite_durable_credential_issue_cleanup_connection(path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT state FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            fence = connection.execute(
                "SELECT state,delivery_ref FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            if (
                target is None
                or fence is None
                or target[0] != "CleanupPending"
                or fence[0] != "CleanupPending"
                or not isinstance(fence[1], str)
            ):
                connection.rollback()
                return CleanupReconciliation(target_id, False, False)
            ref = fence[1]
            intent = connection.execute(
                f"SELECT delivery_ref FROM {_INTENTS} WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            if intent is None:
                connection.execute(
                    f"INSERT INTO {_INTENTS} VALUES(?,?,?,?)", (org_id, target_id, ref, now)
                )
            elif intent[0] != ref:
                raise SqliteCredentialIssueCleanupError("cleanup delivery ref가 drift했습니다.")
            attempt = connection.execute(
                f"SELECT COALESCE(MAX(attempt),0)+1 FROM {_ATTEMPTS} WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()[0]
            connection.execute(
                f"INSERT INTO {_ATTEMPTS} VALUES(?,?,?,?,?)", (org_id, target_id, attempt, ref, now)
            )
            connection.commit()
            if fault_injector is not None:
                fault_injector("after_attempt_committed")
            try:
                aborter.abort(ref)
                outcome = "aborted"
            except Exception:
                outcome = "unavailable"
            if fault_injector is not None:
                fault_injector("after_abort")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"INSERT INTO {_RESULTS} VALUES(?,?,?,?,?,?)",
                (org_id, target_id, attempt, ref, outcome, now),
            )
            if fault_injector is not None:
                fault_injector("after_result")
            if outcome == "aborted":
                target_changed = connection.execute(
                    "UPDATE durable_credential_issue_targets_v1 SET state='Cleaned',updated_at=? WHERE org_id=? AND target_id=? AND state='CleanupPending'",
                    (now, org_id, target_id),
                ).rowcount
                fence_changed = connection.execute(
                    "UPDATE credential_issue_stage_fences_v2 SET state='Cleaned',updated_at=? WHERE org_id=? AND target_id=? AND state='CleanupPending' AND delivery_ref=?",
                    (now, org_id, target_id, ref),
                ).rowcount
                if target_changed != 1 or fence_changed != 1:
                    raise SqliteCredentialIssueCleanupError("cleanup CAS가 stale입니다.")
                if fault_injector is not None:
                    fault_injector("after_cleaned_cas")
            connection.commit()
            return CleanupReconciliation(target_id, True, outcome == "aborted")
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
