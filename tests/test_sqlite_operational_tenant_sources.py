from __future__ import annotations

import sqlite3
import threading
from hashlib import sha256
from pathlib import Path

import pytest

from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_operational_tenant_sources import (
    SqliteOperationalTenantSourcesError,
    migrate_sqlite_operational_tenant_sources,
    open_sqlite_operational_tenant_sources,
)


def test_bootstrap_reopen_and_legacy_table_untouched() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE legacy_sessions (id TEXT)")
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_operational_tenant_sources(connection)
    open_sqlite_operational_tenant_sources(connection).validate_only()
    assert connection.execute("SELECT sql FROM sqlite_master WHERE name='legacy_sessions'").fetchone()


def test_fault_atomic_and_open_never_repairs_partial_schema() -> None:
    connection = sqlite3.connect(":memory:")

    with pytest.raises(RuntimeError):
        migrate_sqlite_operational_tenant_sources(
            connection, fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(point))
            if point == "after_table_1"
            else None,
        )
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='operational_sessions'").fetchone() is None

    connection.execute("CREATE TABLE operational_sessions (bad TEXT)")
    with pytest.raises(SqliteOperationalTenantSourcesError):
        open_sqlite_operational_tenant_sources(connection)
    with pytest.raises(SqliteOperationalTenantSourcesError):
        migrate_sqlite_operational_tenant_sources(connection)


def test_manifest_or_catalog_corruption_fails_closed() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    connection.execute("UPDATE schema_component_manifests SET manifest_sha256='bad'")
    with pytest.raises(SqliteOperationalTenantSourcesError):
        open_sqlite_operational_tenant_sources(connection)


def test_corrupt_shared_manifest_catalog_blocks_migration_before_owned_write() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE schema_component_manifests (component_id TEXT)")

    with pytest.raises(SqliteOperationalTenantSourcesError):
        migrate_sqlite_operational_tenant_sources(connection)

    for table in (
        "operational_registry_state",
        "operational_sessions",
        "operational_audit_records",
        "operational_hitl_toggles",
    ):
        assert connection.execute("SELECT name FROM sqlite_master WHERE name=?", (table,)).fetchone() is None


def test_reuses_canonical_shared_manifest_from_completion_component(tmp_path: Path) -> None:
    path = tmp_path / "operational.sqlite3"
    migrate_sqlite_completion_schema(path)
    connection = sqlite3.connect(path)

    migrate_sqlite_operational_tenant_sources(connection)

    open_sqlite_operational_tenant_sources(connection).validate_only()


def test_registry_cas_and_tenant_isolation() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    capability = open_sqlite_operational_tenant_sources(connection)
    acme = capability.registry("acme")
    other = capability.registry("other")
    payload = {"users": ["u1"], "cards": {"card": {"owner": "u1"}}, "manager_refs": {}}

    assert acme.compare_and_set(None, payload, "2026-01-01T00:00:00.000Z") is True
    assert acme.compare_and_set(None, payload, "2026-01-01T00:00:00.000Z") is False
    assert acme.read() is not None
    assert other.read() is None


def test_registry_digest_and_invalid_reference_fail_closed() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    registry = open_sqlite_operational_tenant_sources(connection).registry("acme")
    with pytest.raises(SqliteOperationalTenantSourcesError):
        registry.compare_and_set(None, {"users": [], "cards": {"c": {"owner": "none"}}, "manager_refs": {}}, "now")


def test_registry_revision_is_strict_and_corruption_is_typed_error() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    registry = open_sqlite_operational_tenant_sources(connection).registry("acme")
    payload = {"users": ["u"], "cards": {"c": {"owner": "u"}}, "manager_refs": {}}
    for revision in (True, -1, "0"):
        with pytest.raises(SqliteOperationalTenantSourcesError):
            registry.compare_and_set(revision, payload, "now")  # type: ignore[arg-type]
    connection.execute("INSERT INTO operational_registry_state VALUES ('acme', 'bad', '{}', ?, 'now')", (sha256(b'{}').hexdigest(),))
    with pytest.raises(SqliteOperationalTenantSourcesError):
        registry.read()


def test_registry_cycle_and_audit_secret_or_nested_record_fail_closed() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    capability = open_sqlite_operational_tenant_sources(connection)
    registry = capability.registry("acme")
    with pytest.raises(SqliteOperationalTenantSourcesError):
        registry.compare_and_set(None, {"users": ["u"], "cards": {"a": {"owner": "u"}, "b": {"owner": "u"}}, "manager_refs": {"a": "b", "b": "a"}}, "now")
    audit = capability.audit("acme")
    with pytest.raises(SqliteOperationalTenantSourcesError):
        audit.append(0, {"action": "x", "secret": "raw"}, "now")
    with pytest.raises(SqliteOperationalTenantSourcesError):
        audit.append(0, {"action": {"nested": "x"}}, "now")


def test_corrupt_persisted_audit_session_and_hitl_rows_fail_closed() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    capability = open_sqlite_operational_tenant_sources(connection)
    connection.execute("INSERT INTO operational_audit_records VALUES ('acme', 0, '{bad', 'x', 'now')")
    with pytest.raises(SqliteOperationalTenantSourcesError):
        capability.audit("acme").records()
    connection.execute("INSERT INTO operational_sessions VALUES ('acme', 's', 'u', 'bad', 'a', 'b', 0)")
    with pytest.raises(SqliteOperationalTenantSourcesError):
        capability.sessions("acme").get("s")
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("INSERT INTO operational_hitl_toggles VALUES ('acme', 'c', 2, 1, 0, 'now')")
    with pytest.raises(SqliteOperationalTenantSourcesError):
        capability.hitl("acme").get("c")


def test_independent_connections_registry_cas_has_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite3"
    bootstrap = sqlite3.connect(path)
    migrate_sqlite_operational_tenant_sources(bootstrap)
    bootstrap.close()
    payload = {"users": ["u"], "cards": {"c": {"owner": "u"}}, "manager_refs": {}}
    outcomes: list[bool] = []
    lock = threading.Lock()

    def contender() -> None:
        connection = sqlite3.connect(path, timeout=5)
        result = open_sqlite_operational_tenant_sources(connection).registry("acme").compare_and_set(
            None, payload, "2026-01-01T00:00:00.000Z"
        )
        with lock:
            outcomes.append(result)
        connection.close()

    threads = [threading.Thread(target=contender) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(True) == 1


def test_session_audit_hitl_same_ids_are_tenant_isolated_and_dropped_schema_closes() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    capability = open_sqlite_operational_tenant_sources(connection)
    for org_id in ("acme", "other"):
        connection.execute("INSERT INTO operational_sessions VALUES (?, 'same', ?, 'active', 'a', 'b', 0)", (org_id, org_id))
        raw = '{"action":"x"}'
        connection.execute(
            "INSERT INTO operational_audit_records VALUES (?, 0, ?, ?, 'now')",
            (org_id, raw, sha256(raw.encode()).hexdigest()),
        )
        connection.execute(
            "INSERT INTO operational_hitl_toggles VALUES (?, 'same', 1, 1, 0, '2026-01-01T00:00:00.000Z')",
            (org_id,),
        )
    acme_session = capability.sessions("acme").get("same")
    other_session = capability.sessions("other").get("same")
    assert acme_session is not None and acme_session[1] == "acme"
    assert other_session is not None and other_session[1] == "other"
    assert capability.audit("acme").records() == [{"action": "x"}]
    assert capability.hitl("other").get("same") == (True, True, 0)
    for table, operation in (
        ("operational_registry_state", lambda: capability.registry("acme").read()),
        ("operational_sessions", lambda: capability.sessions("acme").get("same")),
        ("operational_audit_records", lambda: capability.audit("acme").records()),
        ("operational_hitl_toggles", lambda: capability.hitl("acme").get("same")),
    ):
        connection.execute(f"DROP TABLE {table}")
        with pytest.raises(SqliteOperationalTenantSourcesError):
            operation()
