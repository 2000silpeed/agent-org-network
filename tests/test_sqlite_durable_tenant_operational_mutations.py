from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    COMPONENT_ID,
    MIGRATION_FAULT_POINTS,
    SqliteDurableTenantOperationalMutationsError,
    capture_sqlite_tenant_operational_mutation_scope_snapshot,
    migrate_sqlite_durable_tenant_operational_mutations,
    open_sqlite_durable_tenant_operational_mutations,
)
from agent_org_network.sqlite_operational_tenant_sources import (
    migrate_sqlite_operational_tenant_sources,
)
from agent_org_network.sqlite_tenant_port_audit_v2 import (
    migrate_sqlite_tenant_port_audit_v2,
)


def _parent(connection: sqlite3.Connection) -> None:
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)


def test_installs_receipt_audit_outbox_one_to_one_schema_after_source_parents(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "operational.sqlite")
    try:
        _parent(connection)
        migrate_sqlite_durable_tenant_operational_mutations(connection)

        capability = open_sqlite_durable_tenant_operational_mutations(connection)
        snapshot = capture_sqlite_tenant_operational_mutation_scope_snapshot(connection)
        assert capability.scope_snapshot == snapshot
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone() is not None
        audit_columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_xinfo("durable_tenant_operational_mutation_audit_intents")'
            )
        }
        assert {
            "org_id", "audit_seq", "action", "subject_id", "outcome", "fingerprint",
            "event_digest", "created_at",
        } <= audit_columns
        for table in (
            "durable_tenant_operational_mutation_audit_intents",
            "durable_tenant_operational_mutation_outbox_intents",
        ):
            foreign_keys = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            assert any(row[3] == "receipt_id" for row in foreign_keys)
    finally:
        connection.close()


@pytest.mark.parametrize("point", MIGRATION_FAULT_POINTS)
def test_migration_is_fault_atomic(tmp_path: Path, point: str) -> None:
    connection = sqlite3.connect(tmp_path / "operational.sqlite")
    try:
        _parent(connection)
        with pytest.raises(RuntimeError, match=point):
            migrate_sqlite_durable_tenant_operational_mutations(
                connection,
                fault_injector=lambda actual: (_ for _ in ()).throw(RuntimeError(actual))
                if actual == point
                else None,
            )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'durable_tenant_operational_mutation_%'"
        ).fetchall() == []
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone() is None
    finally:
        connection.close()


def test_requires_s2_and_s31a_parents_and_never_repairs_partial_schema(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "operational.sqlite")
    try:
        with pytest.raises(SqliteDurableTenantOperationalMutationsError):
            migrate_sqlite_durable_tenant_operational_mutations(connection)
        migrate_sqlite_operational_tenant_sources(connection)
        with pytest.raises(SqliteDurableTenantOperationalMutationsError):
            migrate_sqlite_durable_tenant_operational_mutations(connection)
        migrate_sqlite_tenant_port_audit_v2(connection)
        connection.execute("CREATE TABLE durable_tenant_operational_mutation_receipts (x TEXT)")
        with pytest.raises(SqliteDurableTenantOperationalMutationsError):
            migrate_sqlite_durable_tenant_operational_mutations(connection)
    finally:
        connection.close()


def test_scope_snapshot_rejects_memory_or_unnamed_connection_and_detects_source_drift(tmp_path: Path) -> None:
    memory = sqlite3.connect(":memory:")
    try:
        _parent(memory)
        with pytest.raises(SqliteDurableTenantOperationalMutationsError):
            capture_sqlite_tenant_operational_mutation_scope_snapshot(memory)
    finally:
        memory.close()

    connection = sqlite3.connect(tmp_path / "operational.sqlite")
    try:
        _parent(connection)
        snapshot = capture_sqlite_tenant_operational_mutation_scope_snapshot(connection)
        assert snapshot.is_current(connection)
        connection.execute(
            "INSERT INTO operational_audit_events_v2 VALUES ('acme', 0, 'x', 'subject', 'succeeded', ?, ?, '2026-01-01T00:00:00.000Z')",
            ("a" * 64, "b" * 64),
        )
        assert not snapshot.is_current(connection)
    finally:
        connection.close()


def test_open_rejects_catalog_corruption_after_migration(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "operational.sqlite")
    try:
        _parent(connection)
        migrate_sqlite_durable_tenant_operational_mutations(connection)
        connection.execute("DROP TABLE durable_tenant_operational_mutation_outbox_intents")
        with pytest.raises(SqliteDurableTenantOperationalMutationsError):
            open_sqlite_durable_tenant_operational_mutations(connection)
    finally:
        connection.close()


def test_database_enforces_one_to_one_same_org_receipt_children(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "operational.sqlite")
    try:
        _parent(connection)
        migrate_sqlite_durable_tenant_operational_mutations(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute(
            "INSERT INTO durable_tenant_operational_mutation_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("receipt-1", "acme", "command-1", "principal", "hitl.write", "a" * 64, "db", "b" * 64,
             "schema:1", "c" * 64, "2026-01-01T00:00:00.000Z"),
        )
        audit = (
            "receipt-1", "acme", 0, "hitl.write", "subject", "succeeded", "d" * 64,
            "e" * 64, "2026-01-01T00:00:00.000Z",
        )
        connection.execute(
            "INSERT INTO durable_tenant_operational_mutation_audit_intents VALUES(?,?,?,?,?,?,?,?,?)",
            audit,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO durable_tenant_operational_mutation_audit_intents VALUES(?,?,?,?,?,?,?,?,?)",
                ("receipt-1", "acme", 1, "hitl.write", "subject", "succeeded", "f" * 64,
                 "f" * 64, "2026-01-01T00:00:00.000Z"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO durable_tenant_operational_mutation_outbox_intents VALUES(?,?,?,?,?)",
                ("receipt-1", "other", "hitl.write", "a" * 64, "2026-01-01T00:00:00.000Z"),
            )
    finally:
        connection.close()


def test_open_fails_closed_when_foreign_keys_cannot_be_enabled(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite"
    connection = sqlite3.connect(path)
    try:
        _parent(connection)
        migrate_sqlite_durable_tenant_operational_mutations(connection)
    finally:
        connection.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN")  # SQLite cannot change foreign_keys inside a transaction.
        with pytest.raises(SqliteDurableTenantOperationalMutationsError, match="foreign key"):
            open_sqlite_durable_tenant_operational_mutations(connection)
    finally:
        connection.rollback()
        connection.close()
