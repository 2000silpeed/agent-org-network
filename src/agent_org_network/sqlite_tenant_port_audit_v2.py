"""ADR 0053 S3.1a additive tenant audit-v2 schema capability."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from agent_org_network.sqlite_operational_tenant_sources import (
    SqliteOperationalTenantSourcesError,
    open_sqlite_operational_tenant_sources,
)

COMPONENT_ID: Final = "operational_tenant_port_audit_v2"
SCHEMA_VERSION: Final = 2
TABLES: Final = ("operational_audit_events_v2",)
type FaultInjector = Callable[[str], None]

_TABLE_DDL: Final = (
    "CREATE TABLE operational_audit_events_v2 (org_id TEXT NOT NULL COLLATE BINARY, "
    "seq INTEGER NOT NULL CHECK(seq>=0), action TEXT NOT NULL COLLATE BINARY, "
    "subject_id TEXT NOT NULL COLLATE BINARY, outcome TEXT NOT NULL COLLATE BINARY "
    "CHECK(outcome IN ('succeeded','audit_pending')), fingerprint TEXT NOT NULL COLLATE BINARY "
    "CHECK(length(fingerprint)=64), event_digest TEXT NOT NULL COLLATE BINARY "
    "CHECK(length(event_digest)=64), created_at TEXT NOT NULL, PRIMARY KEY(org_id,seq), "
    "UNIQUE(org_id,event_digest))"
)


class SqliteTenantPortAuditV2Error(RuntimeError):
    """The isolated audit-v2 schema cannot safely be used."""


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _same_ddl(actual: str, expected: str) -> bool:
    return " ".join(actual.split()) == " ".join(expected.split())


def _expected_manifest() -> str:
    return _json({"component_id": COMPONENT_ID, "version": SCHEMA_VERSION, "tables": TABLES})


def _validate_parent(connection: sqlite3.Connection) -> None:
    try:
        open_sqlite_operational_tenant_sources(connection).validate_only()
    except SqliteOperationalTenantSourcesError as error:
        raise SqliteTenantPortAuditV2Error(
            "audit v2에는 canonical tenant operational v1 parent가 필요합니다."
        ) from error


def _validate(connection: sqlite3.Connection) -> None:
    """Validate parent and this component's catalog only; never inspect v1 audit rows."""
    _validate_parent(connection)
    row = connection.execute(
        "SELECT schema_version, manifest_json, manifest_sha256 FROM schema_component_manifests "
        "WHERE component_id COLLATE BINARY=?",
        (COMPONENT_ID,),
    ).fetchone()
    expected = _expected_manifest()
    if (
        row is None
        or type(row[0]) is not int
        or row[0] != SCHEMA_VERSION
        or row[1] != expected
        or row[2] != _digest(expected)
    ):
        raise SqliteTenantPortAuditV2Error("audit v2 manifest가 canonical하지 않습니다.")
    actual = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLES[0],)
    ).fetchone()
    if actual is None or not _same_ddl(actual[0], _TABLE_DDL):
        raise SqliteTenantPortAuditV2Error("audit v2 catalog가 canonical하지 않습니다.")


@dataclass(frozen=True)
class SqliteTenantPortAuditV2Capability:
    connection: sqlite3.Connection

    def validate_only(self) -> None:
        _validate(self.connection)


def open_sqlite_tenant_port_audit_v2(
    connection: sqlite3.Connection,
) -> SqliteTenantPortAuditV2Capability:
    _validate(connection)
    return SqliteTenantPortAuditV2Capability(connection)


def migrate_sqlite_tenant_port_audit_v2(
    connection: sqlite3.Connection, *, fault_injector: FaultInjector | None = None
) -> None:
    """Install the additive audit-v2 table atomically; never repair any drift."""
    _validate_parent(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_parent(connection)
        existing_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        marker = connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
            (COMPONENT_ID,),
        ).fetchone()
        if TABLES[0] in existing_tables or marker is not None:
            _validate(connection)
            connection.commit()
            return
        connection.execute(_TABLE_DDL)
        if fault_injector is not None:
            fault_injector("after_table_0")
        manifest = _expected_manifest()
        connection.execute(
            "INSERT INTO schema_component_manifests "
            "(component_id, schema_version, manifest_json, manifest_sha256) VALUES (?, ?, ?, ?)",
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
