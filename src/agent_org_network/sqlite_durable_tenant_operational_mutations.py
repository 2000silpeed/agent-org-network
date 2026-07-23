"""R1.0 durable tenant operational-mutation schema and SQLite source scope.

This module installs only the receipt/audit-intent/outbox-intent persistence
boundary.  Command planning and all state mutation remain owned by R1.1.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agent_org_network.sqlite_operational_tenant_sources import (
    SqliteOperationalTenantSourcesError,
    open_sqlite_operational_tenant_sources,
)
from agent_org_network.sqlite_tenant_port_audit_v2 import (
    SqliteTenantPortAuditV2Error,
    open_sqlite_tenant_port_audit_v2,
)

COMPONENT_ID: Final = "durable_tenant_operational_mutations_v1"
SCHEMA_VERSION: Final = 1
MIGRATION_FAULT_POINTS: Final = (
    "after_receipts",
    "after_audit_intents",
    "after_outbox_intents",
    "before_manifest",
    "after_manifest",
)
type FaultInjector = Callable[[str], None]

_MANIFEST = "schema_component_manifests"
_RECEIPTS = "durable_tenant_operational_mutation_receipts"
_AUDIT = "durable_tenant_operational_mutation_audit_intents"
_OUTBOX = "durable_tenant_operational_mutation_outbox_intents"
_OWNED: Final = (_RECEIPTS, _AUDIT, _OUTBOX)
_SOURCE_TABLES: Final = (
    "operational_registry_state",
    "operational_sessions",
    "operational_audit_records",
    "operational_hitl_toggles",
    "operational_audit_events_v2",
)

_RECEIPTS_DDL: Final = """CREATE TABLE durable_tenant_operational_mutation_receipts (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 command_id TEXT NOT NULL COLLATE BINARY,
 principal_id TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY CHECK(length(command_digest)=64),
 source_database_identity TEXT NOT NULL COLLATE BINARY,
 source_schema_manifest_digest TEXT NOT NULL COLLATE BINARY CHECK(length(source_schema_manifest_digest)=64),
 source_revision TEXT NOT NULL COLLATE BINARY,
 source_snapshot_digest TEXT NOT NULL COLLATE BINARY CHECK(length(source_snapshot_digest)=64),
 created_at TEXT NOT NULL,
 UNIQUE(org_id, command_id),
 UNIQUE(org_id, command_digest),
 UNIQUE(org_id, receipt_id)
)"""
_AUDIT_DDL: Final = """CREATE TABLE durable_tenant_operational_mutation_audit_intents (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 audit_seq INTEGER NOT NULL CHECK(audit_seq>=0),
 action TEXT NOT NULL COLLATE BINARY,
 subject_id TEXT NOT NULL COLLATE BINARY,
 outcome TEXT NOT NULL COLLATE BINARY CHECK(outcome IN ('succeeded','audit_pending')),
 fingerprint TEXT NOT NULL COLLATE BINARY CHECK(length(fingerprint)=64),
 event_digest TEXT NOT NULL COLLATE BINARY CHECK(length(event_digest)=64),
 created_at TEXT NOT NULL,
 UNIQUE(org_id, audit_seq),
 UNIQUE(org_id, event_digest),
 FOREIGN KEY(org_id, receipt_id) REFERENCES durable_tenant_operational_mutation_receipts(org_id, receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_OUTBOX_DDL: Final = """CREATE TABLE durable_tenant_operational_mutation_outbox_intents (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY CHECK(length(command_digest)=64),
 created_at TEXT NOT NULL,
 FOREIGN KEY(org_id, receipt_id) REFERENCES durable_tenant_operational_mutation_receipts(org_id, receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_DDLS: Final = (_RECEIPTS_DDL, _AUDIT_DDL, _OUTBOX_DDL)


class SqliteDurableTenantOperationalMutationsError(RuntimeError):
    """The R1.0 schema or its current source scope is unavailable."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same_ddl(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()) == " ".join(expected.split())


def _validate_parents(connection: sqlite3.Connection) -> None:
    try:
        open_sqlite_operational_tenant_sources(connection).validate_only()
        open_sqlite_tenant_port_audit_v2(connection).validate_only()
    except (SqliteOperationalTenantSourcesError, SqliteTenantPortAuditV2Error) as error:
        raise SqliteDurableTenantOperationalMutationsError(
            "R1.0에는 canonical S2 및 S3.1a parent가 필요합니다."
        ) from error


def _require_foreign_keys(connection: sqlite3.Connection) -> None:
    """A schema with FK clauses is not a capability while SQLite ignores them."""
    connection.execute("PRAGMA foreign_keys=ON")
    row = connection.execute("PRAGMA foreign_keys").fetchone()
    if row is None or row[0] != 1:
        raise SqliteDurableTenantOperationalMutationsError(
            "R1.0 SQLite foreign key enforcement가 켜져 있지 않습니다."
        )


def _manifest_json() -> str:
    return _canonical({"component_id": COMPONENT_ID, "version": SCHEMA_VERSION, "tables": _OWNED})


def _validate(connection: sqlite3.Connection) -> None:
    _require_foreign_keys(connection)
    _validate_parents(connection)
    marker = connection.execute(
        "SELECT schema_version, manifest_json, manifest_sha256 FROM schema_component_manifests "
        "WHERE component_id COLLATE BINARY=?",
        (COMPONENT_ID,),
    ).fetchone()
    manifest = _manifest_json()
    if marker is None or marker[0] != SCHEMA_VERSION or marker[1] != manifest or marker[2] != _digest(manifest):
        raise SqliteDurableTenantOperationalMutationsError("R1.0 manifest가 canonical하지 않습니다.")
    for table, ddl in zip(_OWNED, _DDLS, strict=True):
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None or not _same_ddl(row[0], ddl):
            raise SqliteDurableTenantOperationalMutationsError("R1.0 catalog가 canonical하지 않습니다.")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SqliteDurableTenantOperationalMutationsError("R1.0 foreign key가 손상되었습니다.")


def migrate_sqlite_durable_tenant_operational_mutations(
    connection: sqlite3.Connection, *, fault_injector: FaultInjector | None = None
) -> None:
    """Install this component atomically, never repairing a partial catalog."""
    _require_foreign_keys(connection)
    _validate_parents(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_parents(connection)
        existing = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        marker = connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
            (COMPONENT_ID,),
        ).fetchone()
        if marker is not None or set(_OWNED) & existing:
            _validate(connection)
            connection.commit()
            return
        for ddl, point in zip(_DDLS, MIGRATION_FAULT_POINTS[:3], strict=True):
            connection.execute(ddl)
            if fault_injector is not None:
                fault_injector(point)
        if fault_injector is not None:
            fault_injector("before_manifest")
        manifest = _manifest_json()
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) "
            "VALUES(?,?,?,?)",
            (COMPONENT_ID, SCHEMA_VERSION, manifest, _digest(manifest)),
        )
        if fault_injector is not None:
            fault_injector("after_manifest")
        _validate(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _file_identity(connection: sqlite3.Connection) -> str:
    rows = connection.execute("PRAGMA database_list").fetchall()
    main = next((row for row in rows if row[1] == "main"), None)
    if main is None or type(main[2]) is not str or not main[2] or main[2] == ":memory:":
        raise SqliteDurableTenantOperationalMutationsError(
            "R1.0 scope snapshot은 named SQLite file만 허용합니다."
        )
    path = Path(main[2]).expanduser()
    if not path.is_file():
        raise SqliteDurableTenantOperationalMutationsError(
            "R1.0 scope snapshot SQLite file identity가 유효하지 않습니다."
        )
    return str(path.resolve())


def _source_state(connection: sqlite3.Connection) -> tuple[str, str, str]:
    manifest_rows = connection.execute(
        "SELECT component_id,schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests "
        "WHERE component_id COLLATE BINARY IN (?,?) ORDER BY component_id COLLATE BINARY",
        ("operational_tenant_sources_v1", "operational_tenant_port_audit_v2"),
    ).fetchall()
    if len(manifest_rows) != 2:
        raise SqliteDurableTenantOperationalMutationsError("source parent manifest가 없습니다.")
    manifests = [list(row) for row in manifest_rows]
    manifest_digest = _digest(_canonical(manifests))
    tables: dict[str, list[list[object]]] = {}
    for table in _SOURCE_TABLES:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_xinfo("{table}")')]
        if not columns:
            raise SqliteDurableTenantOperationalMutationsError("source table catalog가 없습니다.")
        order = ",".join(f'"{column}" COLLATE BINARY' for column in columns)
        tables[table] = [list(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
    schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
    revision = f"schema:{schema_version}"
    snapshot_digest = _digest(_canonical({"manifests": manifests, "revision": revision, "tables": tables}))
    return manifest_digest, revision, snapshot_digest


@dataclass(frozen=True)
class SqliteTenantOperationalMutationScopeSnapshot:
    """Typed current scope proof for one named SQLite source instance."""

    database_identity: str
    schema_manifest_digest: str
    source_revision: str
    snapshot_digest: str

    def is_current(self, connection: sqlite3.Connection) -> bool:
        try:
            return self == capture_sqlite_tenant_operational_mutation_scope_snapshot(connection)
        except SqliteDurableTenantOperationalMutationsError:
            return False


def capture_sqlite_tenant_operational_mutation_scope_snapshot(
    connection: sqlite3.Connection,
) -> SqliteTenantOperationalMutationScopeSnapshot:
    """Capture S2/S3.1a source state; no DDL or state mutation is performed."""
    _validate_parents(connection)
    identity = _file_identity(connection)
    manifest_digest, revision, snapshot_digest = _source_state(connection)
    return SqliteTenantOperationalMutationScopeSnapshot(
        database_identity=identity,
        schema_manifest_digest=manifest_digest,
        source_revision=revision,
        snapshot_digest=snapshot_digest,
    )


@dataclass(frozen=True)
class SqliteDurableTenantOperationalMutationsCapability:
    connection: sqlite3.Connection
    scope_snapshot: SqliteTenantOperationalMutationScopeSnapshot

    def validate_only(self) -> None:
        """Re-check schema/catalog canonicality only.

        This does not re-compare ``scope_snapshot`` against a fresh capture:
        every caller invokes ``open(...).validate_only()`` back-to-back with
        no work in between, so the two captures are two separate untransacted
        reads of mutable source-table content with no atomicity linking them.
        Any unrelated writer committing in that window would make them
        legitimately differ, which is not evidence R1.0 itself is
        unavailable. Callers that need a source-content CAS compare a fresh
        ``capture_sqlite_tenant_operational_mutation_scope_snapshot`` against
        their own command-time ``expected_scope`` explicitly (see R1.1's
        ``_require_scope``); ``scope_snapshot.is_current(...)`` remains
        available directly for callers that hold this capability across real
        intervening work and want that staleness check.
        """
        _validate(self.connection)


def open_sqlite_durable_tenant_operational_mutations(
    connection: sqlite3.Connection,
) -> SqliteDurableTenantOperationalMutationsCapability:
    _require_foreign_keys(connection)
    _validate(connection)
    return SqliteDurableTenantOperationalMutationsCapability(
        connection=connection,
        scope_snapshot=capture_sqlite_tenant_operational_mutation_scope_snapshot(connection),
    )
