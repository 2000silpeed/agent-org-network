"""R3.0 durable credential secure-delivery stage fence schema.

This module installs a persistence boundary only.  It does not claim a fence,
call a delivery adapter, or activate a credential MCP tool.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final


SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_COMPONENT_ID: Final = "durable_credential_stage_fence_v1"
SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_SCHEMA_VERSION: Final = 1
SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_MIGRATION_FAULT_POINTS: Final = (
    "after_manifest_table",
    "after_stage_fences",
    "before_manifest_insert",
    "after_manifest_insert",
)
type MigrationFaultInjector = Callable[[str], None]


class SqliteDurableCredentialStageFenceSchemaError(RuntimeError):
    """The durable credential parent or R3.0 fence schema is unavailable."""


_MANIFEST = "schema_component_manifests"
_FENCES = "durable_credential_stage_fences"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DELIVERY_REF = re.compile(r"delivery:v1:[0-9a-f]{64}\Z")
_FORBIDDEN_OPAQUE_TERMS: Final = (
    "secret", "grant", "rationale", "body", "password", "token", "key", "bearer", "authorization",
)
_STATES: Final = frozenset(
    {
        "PendingStage", "ClaimedStage", "Staged", "Committing", "Committed", "CleanupPending", "Cleaned",
    }
)

_MANIFEST_DDL: Final = """CREATE TABLE schema_component_manifests (
    component_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    schema_version INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL
)"""
def _opaque_sql(field: str, *, nullable: bool = False) -> str:
    predicate = " AND ".join(
        (
            f"length({field}) BETWEEN 1 AND 128",
            f"{field} GLOB '[A-Za-z0-9]*'",
            f"{field} NOT GLOB '*[^A-Za-z0-9._:-]*'",
            *(f"instr(lower({field}), '{term}') = 0" for term in _FORBIDDEN_OPAQUE_TERMS),
        )
    )
    return f"{field} IS NULL OR ({predicate})" if nullable else predicate


def _sha256_sql(field: str, *, nullable: bool = False) -> str:
    predicate = f"length({field}) = 64 AND {field} NOT GLOB '*[^0-9a-f]*'"
    return f"{field} IS NULL OR ({predicate})" if nullable else predicate


def _delivery_ref_sql(field: str) -> str:
    return f"length({field}) = 76 AND substr({field}, 1, 12) = 'delivery:v1:' AND substr({field}, 13) NOT GLOB '*[^0-9a-f]*'"


_FENCES_DDL: Final = f"""CREATE TABLE durable_credential_stage_fences (
 org_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('org_id')}),
 request_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('request_id')}),
 attempt INTEGER NOT NULL CHECK(attempt >= 1),
 credential_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('credential_id')}),
 principal_subject_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('principal_subject_id')}),
 action TEXT NOT NULL COLLATE BINARY CHECK(action = 'worker_credential.issue'),
 command_digest TEXT NOT NULL COLLATE BINARY CHECK({_sha256_sql('command_digest')}),
 resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK({_sha256_sql('resource_fingerprint')}),
 evidence_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('evidence_id')}),
 stage_key TEXT NOT NULL COLLATE BINARY CHECK({_sha256_sql('stage_key')}),
 secret_hash TEXT NOT NULL COLLATE BINARY CHECK({_sha256_sql('secret_hash')}),
 delivery_ref TEXT COLLATE BINARY CHECK(delivery_ref IS NULL OR ({_delivery_ref_sql('delivery_ref')})),
 claim_generation INTEGER NOT NULL CHECK(claim_generation >= 0),
 claim_token_hash TEXT COLLATE BINARY CHECK({_sha256_sql('claim_token_hash', nullable=True)}),
 state TEXT NOT NULL COLLATE BINARY CHECK(state IN ('PendingStage','ClaimedStage','Staged','Committing','Committed','CleanupPending','Cleaned')),
 PRIMARY KEY(org_id, request_id, attempt),
 UNIQUE(org_id, stage_key),
 FOREIGN KEY(org_id, credential_id) REFERENCES durable_credentials(org_id, credential_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 CHECK((state = 'PendingStage' AND claim_generation = 0 AND claim_token_hash IS NULL AND delivery_ref IS NULL) OR
       (state = 'ClaimedStage' AND claim_generation >= 1 AND claim_token_hash IS NOT NULL AND delivery_ref IS NULL) OR
       (state IN ('Staged','Committing','Committed','CleanupPending','Cleaned') AND claim_generation >= 1 AND claim_token_hash IS NOT NULL AND delivery_ref IS NOT NULL))
)"""

_PARENT_DDLS: Final = {
    "durable_credentials": """CREATE TABLE durable_credentials (
              credential_id TEXT NOT NULL, org_id TEXT NOT NULL, owner_subject_id TEXT NOT NULL,
              role TEXT NOT NULL, generation INTEGER NOT NULL, revision INTEGER NOT NULL,
              status TEXT NOT NULL, secret_hash TEXT NOT NULL, issued_at TEXT NOT NULL,
              expires_at TEXT, revoked_at TEXT,
              PRIMARY KEY (org_id, credential_id),
              CHECK (generation >= 1), CHECK (revision >= 1), CHECK (status IN ('active','revoked'))
            )""",
    "credential_command_receipts": """CREATE TABLE credential_command_receipts (
              org_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
              command_digest TEXT NOT NULL, credential_id TEXT NOT NULL, result_revision INTEGER NOT NULL,
              result_json TEXT NOT NULL, delivery_ref TEXT,
              PRIMARY KEY (org_id, request_id, attempt)
            )""",
    "credential_audit_intents": """CREATE TABLE credential_audit_intents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, action TEXT NOT NULL,
              credential_id TEXT NOT NULL, principal_subject_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
              detail_json TEXT NOT NULL
            )""",
    "credential_outbox_intents": """CREATE TABLE credential_outbox_intents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, kind TEXT NOT NULL,
              credential_id TEXT NOT NULL, payload_json TEXT NOT NULL
            )""",
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_opaque(value: object) -> bool:
    return (
        isinstance(value, str)
        and _OPAQUE.fullmatch(value) is not None
        and all(term not in value.lower() for term in _FORBIDDEN_OPAQUE_TERMS)
    )


def _same_ddl(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()) == " ".join(expected.split())


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()
    return None if row is None else row[0]


def _validate_parent(connection: sqlite3.Connection) -> None:
    for table, ddl in _PARENT_DDLS.items():
        if not _same_ddl(_table_sql(connection, table), ddl):
            raise SqliteDurableCredentialStageFenceSchemaError("durable credential parent schema가 canonical하지 않습니다.")


def _manifest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if _table_sql(connection, _MANIFEST) is None:
        return None
    return connection.execute(
        "SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id=?",
        (SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_COMPONENT_ID,),
    ).fetchone()


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    sql = _table_sql(connection, _FENCES)
    return {"tables": [{"name": _FENCES, "sql": " ".join(sql.split()) if sql is not None else None}]}


def _expected_manifest() -> str:
    return _canonical({"component_id": SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_COMPONENT_ID, "schema_version": SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_SCHEMA_VERSION, "catalog": {"tables": [{"name": _FENCES, "sql": " ".join(_FENCES_DDL.split())}]}})


def _validate_rows(connection: sqlite3.Connection) -> None:
    for row in connection.execute(f"SELECT * FROM {_FENCES}"):
        values = dict(row)
        state = values["state"]
        generation = values["claim_generation"]
        token = values["claim_token_hash"]
        delivery_ref = values["delivery_ref"]
        valid_material = (
            (state == "PendingStage" and generation == 0 and token is None and delivery_ref is None)
            or (state == "ClaimedStage" and isinstance(generation, int) and generation >= 1 and token is not None and delivery_ref is None)
            or (state in {"Staged", "Committing", "Committed", "CleanupPending", "Cleaned"} and isinstance(generation, int) and generation >= 1 and token is not None and delivery_ref is not None)
        )
        if (
            any(not _safe_opaque(values[key]) for key in ("org_id", "request_id", "credential_id", "principal_subject_id", "evidence_id"))
            or values["action"] != "worker_credential.issue"
            or any(not isinstance(values[key], str) or _SHA256.fullmatch(values[key]) is None for key in ("command_digest", "resource_fingerprint", "stage_key", "secret_hash"))
            or type(values["attempt"]) is not int or values["attempt"] < 1
            or type(generation) is not int or generation < 0
            or state not in _STATES
            or (token is not None and (not isinstance(token, str) or _SHA256.fullmatch(token) is None))
            or (delivery_ref is not None and (not isinstance(delivery_ref, str) or _DELIVERY_REF.fullmatch(delivery_ref) is None))
            or not valid_material
        ):
            raise SqliteDurableCredentialStageFenceSchemaError("credential stage fence row가 canonical하지 않습니다.")


def _validate(connection: sqlite3.Connection) -> None:
    _validate_parent(connection)
    if not _same_ddl(_table_sql(connection, _MANIFEST), _MANIFEST_DDL):
        raise SqliteDurableCredentialStageFenceSchemaError("공유 manifest catalog가 canonical하지 않습니다.")
    marker = _manifest(connection)
    expected = _expected_manifest()
    if marker is None or marker["schema_version"] != SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_SCHEMA_VERSION or marker["manifest_json"] != expected or marker["manifest_sha256"] != _digest(expected):
        raise SqliteDurableCredentialStageFenceSchemaError("credential stage fence manifest가 canonical하지 않습니다.")
    if not _same_ddl(_table_sql(connection, _FENCES), _FENCES_DDL) or _canonical(_catalog(connection)) != _canonical({"tables": [{"name": _FENCES, "sql": " ".join(_FENCES_DDL.split())}]}):
        raise SqliteDurableCredentialStageFenceSchemaError("credential stage fence catalog가 canonical하지 않습니다.")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SqliteDurableCredentialStageFenceSchemaError("credential stage fence foreign key가 canonical하지 않습니다.")
    _validate_rows(connection)


def validate_sqlite_durable_credential_stage_fence_connection(connection: sqlite3.Connection) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection)
    finally:
        connection.row_factory = previous


def migrate_sqlite_durable_credential_stage_fence_schema(db_path: str | Path, *, fault_injector: MigrationFaultInjector | None = None) -> None:
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        _validate_parent(connection)
        manifest_exists = _table_sql(connection, _MANIFEST) is not None
        if manifest_exists and not _same_ddl(_table_sql(connection, _MANIFEST), _MANIFEST_DDL):
            raise SqliteDurableCredentialStageFenceSchemaError("공유 manifest catalog가 canonical하지 않습니다.")
        if _manifest(connection) is not None:
            _validate(connection)
            connection.commit()
            return
        if _table_sql(connection, _FENCES) is not None:
            raise SqliteDurableCredentialStageFenceSchemaError("manifest 없는 partial credential stage fence는 복구하지 않습니다.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteDurableCredentialStageFenceSchemaError("migration 전 foreign_key_check가 실패했습니다.")
        if not manifest_exists:
            connection.execute(_MANIFEST_DDL)
        if fault_injector is not None:
            fault_injector("after_manifest_table")
        connection.execute(_FENCES_DDL)
        if fault_injector is not None:
            fault_injector("after_stage_fences")
        expected = _expected_manifest()
        if _canonical(_catalog(connection)) != _canonical({"tables": [{"name": _FENCES, "sql": " ".join(_FENCES_DDL.split())}]}):
            raise SqliteDurableCredentialStageFenceSchemaError("migration 결과 catalog가 canonical하지 않습니다.")
        if fault_injector is not None:
            fault_injector("before_manifest_insert")
        connection.execute("INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES(?,?,?,?)", (SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_COMPONENT_ID, SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_SCHEMA_VERSION, expected, _digest(expected)))
        if fault_injector is not None:
            fault_injector("after_manifest_insert")
        _validate(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _open(path: str | Path, *, readonly: bool) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise SqliteDurableCredentialStageFenceSchemaError("credential stage fence runtime은 기존 SQLite 파일만 엽니다.")
    try:
        return sqlite3.connect(f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode={'ro' if readonly else 'rw'}", uri=True, timeout=5.0)
    except sqlite3.Error as error:
        raise SqliteDurableCredentialStageFenceSchemaError("credential stage fence SQLite DB를 열 수 없습니다.") from error


def open_sqlite_durable_credential_stage_fence_connection(db_path: str | Path) -> sqlite3.Connection:
    connection = _open(db_path, readonly=False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _validate(connection)
    except Exception:
        connection.close()
        raise
    return connection


@dataclass(frozen=True)
class DurableCredentialStageFenceSchemaReconciliationReport:
    capable: bool
    detail: str
    credential_stage_fence_manifest_present: bool


def reconcile_sqlite_durable_credential_stage_fence_schema(db_path: str | Path) -> DurableCredentialStageFenceSchemaReconciliationReport:
    present = False
    try:
        connection = _open(db_path, readonly=True)
    except SqliteDurableCredentialStageFenceSchemaError as error:
        return DurableCredentialStageFenceSchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _manifest(connection) is not None
        _validate(connection)
        return DurableCredentialStageFenceSchemaReconciliationReport(True, "capable_v1", present)
    except Exception as error:
        return DurableCredentialStageFenceSchemaReconciliationReport(False, str(error), present)
    finally:
        connection.close()

