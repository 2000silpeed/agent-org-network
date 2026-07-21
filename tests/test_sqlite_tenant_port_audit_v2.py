from __future__ import annotations

import sqlite3
from hashlib import sha256

import pytest

from agent_org_network.sqlite_operational_tenant_sources import (
    migrate_sqlite_operational_tenant_sources,
)
from agent_org_network.sqlite_tenant_port_audit_v2 import (
    SqliteTenantPortAuditV2Error,
    migrate_sqlite_tenant_port_audit_v2,
    open_sqlite_tenant_port_audit_v2,
)


def _v1_sql_and_manifest(connection: sqlite3.Connection) -> tuple[str, tuple[object, ...]]:
    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operational_audit_records'"
    ).fetchone()
    manifest = connection.execute(
        "SELECT schema_version, manifest_json, manifest_sha256 FROM schema_component_manifests "
        "WHERE component_id='operational_tenant_sources_v1'"
    ).fetchone()
    assert table is not None
    assert manifest is not None
    return table[0], manifest


def test_v2_migration_is_additive_and_reopen_is_validate_only() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    connection.execute(
        "INSERT INTO operational_audit_records VALUES (?, ?, ?, ?, ?)",
        ("acme", 0, '{"action":"legacy"}', sha256(b'{"action":"legacy"}').hexdigest(), "now"),
    )
    connection.commit()
    v1_before = _v1_sql_and_manifest(connection)

    migrate_sqlite_tenant_port_audit_v2(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)
    open_sqlite_tenant_port_audit_v2(connection).validate_only()

    assert _v1_sql_and_manifest(connection) == v1_before
    assert connection.execute("SELECT count(*) FROM operational_audit_records").fetchone() == (1,)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='operational_audit_events_v2'"
        ).fetchone()
        is not None
    )


def test_v2_requires_exact_parent_and_never_repairs_parent_drift() -> None:
    connection = sqlite3.connect(":memory:")
    with pytest.raises(SqliteTenantPortAuditV2Error):
        migrate_sqlite_tenant_port_audit_v2(connection)

    migrate_sqlite_operational_tenant_sources(connection)
    connection.execute(
        "UPDATE schema_component_manifests SET manifest_sha256='bad' WHERE component_id='operational_tenant_sources_v1'"
    )

    with pytest.raises(SqliteTenantPortAuditV2Error):
        migrate_sqlite_tenant_port_audit_v2(connection)
    with pytest.raises(SqliteTenantPortAuditV2Error):
        open_sqlite_tenant_port_audit_v2(connection)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='operational_audit_events_v2'"
        ).fetchone()
        is None
    )


def test_v2_partial_or_manifest_corruption_fails_closed_without_repair() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    connection.execute("CREATE TABLE operational_audit_events_v2 (bad TEXT)")

    with pytest.raises(SqliteTenantPortAuditV2Error):
        migrate_sqlite_tenant_port_audit_v2(connection)
    with pytest.raises(SqliteTenantPortAuditV2Error):
        open_sqlite_tenant_port_audit_v2(connection)

    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)
    connection.execute(
        "UPDATE schema_component_manifests SET manifest_sha256='bad' WHERE component_id='operational_tenant_port_audit_v2'"
    )
    with pytest.raises(SqliteTenantPortAuditV2Error):
        open_sqlite_tenant_port_audit_v2(connection)


@pytest.mark.parametrize("point", ["after_table_0", "after_manifest"])
def test_v2_fault_rolls_back_owned_schema_and_manifest(point: str) -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)

    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_tenant_port_audit_v2(
            connection,
            fault_injector=lambda current: (
                (_ for _ in ()).throw(RuntimeError(current)) if current == point else None
            ),
        )

    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='operational_audit_events_v2'"
        ).fetchone()
        is None
    )
    assert (
        connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id='operational_tenant_port_audit_v2'"
        ).fetchone()
        is None
    )


def test_v2_catalog_is_exact_and_never_reads_or_backfills_v1_rows() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)
    connection.execute(
        "INSERT INTO operational_audit_records VALUES ('acme', 0, '{bad', 'bad', 'now')"
    )

    open_sqlite_tenant_port_audit_v2(connection).validate_only()
    ddl = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operational_audit_events_v2'"
    ).fetchone()[0]
    assert ddl == (
        "CREATE TABLE operational_audit_events_v2 (org_id TEXT NOT NULL COLLATE BINARY, seq INTEGER NOT NULL CHECK(seq>=0), action TEXT NOT NULL COLLATE BINARY, subject_id TEXT NOT NULL COLLATE BINARY, outcome TEXT NOT NULL COLLATE BINARY CHECK(outcome IN ('succeeded','audit_pending')), fingerprint TEXT NOT NULL COLLATE BINARY CHECK(length(fingerprint)=64), event_digest TEXT NOT NULL COLLATE BINARY CHECK(length(event_digest)=64), created_at TEXT NOT NULL, PRIMARY KEY(org_id,seq), UNIQUE(org_id,event_digest))"
    )
