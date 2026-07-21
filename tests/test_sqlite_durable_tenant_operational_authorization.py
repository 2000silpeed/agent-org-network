from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_org_network.sqlite_durable_tenant_operational_authorization import (
    COMPONENT_ID,
    SqliteDurableTenantOperationalAuthorizationError,
    migrate_sqlite_durable_tenant_operational_authorization,
    open_sqlite_durable_tenant_operational_authorization,
)
from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    migrate_sqlite_durable_tenant_operational_mutations,
)
from agent_org_network.sqlite_operational_tenant_sources import (
    migrate_sqlite_operational_tenant_sources,
)
from agent_org_network.sqlite_tenant_port_audit_v2 import migrate_sqlite_tenant_port_audit_v2


def _parent(connection: sqlite3.Connection) -> None:
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)
    migrate_sqlite_durable_tenant_operational_mutations(connection)


def test_r12_requires_r10_and_installs_same_org_receipt_companion(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "r12.sqlite")
    try:
        with pytest.raises(Exception):
            migrate_sqlite_durable_tenant_operational_authorization(connection)
        _parent(connection)
        migrate_sqlite_durable_tenant_operational_authorization(connection)
        open_sqlite_durable_tenant_operational_authorization(connection)
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone()
        assert connection.execute(
            'PRAGMA foreign_key_list("durable_tenant_operational_authorization_evidence")'
        ).fetchall()
    finally:
        connection.close()


@pytest.mark.parametrize("point", ["after_evidence", "before_manifest", "after_manifest"])
def test_r12_migration_is_fault_atomic(tmp_path: Path, point: str) -> None:
    connection = sqlite3.connect(tmp_path / f"{point}.sqlite")
    try:
        _parent(connection)
        with pytest.raises(RuntimeError, match=point):
            migrate_sqlite_durable_tenant_operational_authorization(
                connection,
                fault_injector=lambda actual: (
                    (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
                ),
            )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='durable_tenant_operational_authorization_evidence'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_r12_rejects_catalog_tamper(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "r12.sqlite")
    try:
        _parent(connection)
        migrate_sqlite_durable_tenant_operational_authorization(connection)
        connection.execute("DROP TABLE durable_tenant_operational_authorization_evidence")
        with pytest.raises(SqliteDurableTenantOperationalAuthorizationError):
            open_sqlite_durable_tenant_operational_authorization(connection)
    finally:
        connection.close()
