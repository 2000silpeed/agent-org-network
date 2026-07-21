from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_org_network.sqlite_operational_tenant_sources import (
    migrate_sqlite_operational_tenant_sources,
)
from agent_org_network.sqlite_tenant_port_audit_adapter import (
    SqliteTenantAuditReader,
    SqliteTenantAuditWriter,
)
from agent_org_network.sqlite_tenant_port_audit_v2 import migrate_sqlite_tenant_port_audit_v2
from agent_org_network.tenant_operational_ports import (
    ResourceFingerprint,
    SafeAuditEvent,
    ScopedUnavailable,
    TenantOrgId,
)


def _event(action: str, subject: str, outcome: str = "succeeded") -> SafeAuditEvent:
    return SafeAuditEvent(
        action,
        subject,
        outcome,  # type: ignore[arg-type]
        ResourceFingerprint.from_scalars("acme", action, subject, outcome),
    )


def _ready() -> tuple[
    sqlite3.Connection, TenantOrgId, SqliteTenantAuditReader, SqliteTenantAuditWriter
]:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)
    org = TenantOrgId("acme")
    return (
        connection,
        org,
        SqliteTenantAuditReader(connection, org),
        SqliteTenantAuditWriter(connection, org),
    )


def test_append_is_idempotent_and_reader_has_ordered_safe_dtos() -> None:
    connection, acme, reader, writer = _ready()
    other = TenantOrgId("other")
    first, second = _event("session.end", "s1"), _event("session.end", "s2", "audit_pending")

    assert writer.append(acme, first) is None
    assert writer.append(acme, first) is None
    assert writer.append(acme, second) is None
    other_writer = SqliteTenantAuditWriter(connection, other)
    other_reader = SqliteTenantAuditReader(connection, other)
    assert other_writer.append(other, first) is None
    assert reader.list(acme) == (first, second)
    assert reader.detail(acme, 0) == first
    assert reader.detail(acme, 1) == second
    assert other_reader.list(other) == (first,)
    assert (
        connection.execute(
            "SELECT seq, created_at FROM operational_audit_events_v2 WHERE org_id='acme' ORDER BY seq"
        ).fetchall()[0][0]
        == 0
    )
    created_at = connection.execute(
        "SELECT created_at FROM operational_audit_events_v2 WHERE org_id='acme' LIMIT 1"
    ).fetchone()[0]
    assert datetime.fromisoformat(created_at.replace("Z", "+00:00")).tzinfo is UTC


def test_reader_writer_fail_closed_on_invalid_input_and_corrupt_row() -> None:
    connection, org, reader, writer = _ready()
    event = _event("session.end", "s1")

    with pytest.raises(ValueError):
        SqliteTenantAuditReader(connection, "acme")  # type: ignore[arg-type]
    assert isinstance(writer.append(TenantOrgId("acme"), event), type(None))
    assert isinstance(writer.append("acme", event), ScopedUnavailable)  # type: ignore[arg-type]
    assert isinstance(writer.append(TenantOrgId("other"), event), ScopedUnavailable)
    assert isinstance(writer.append(org, "raw"), ScopedUnavailable)  # type: ignore[arg-type]
    assert isinstance(reader.detail(org, True), ScopedUnavailable)
    assert isinstance(reader.detail(org, -1), ScopedUnavailable)
    connection.execute("UPDATE operational_audit_events_v2 SET created_at='now'")
    assert isinstance(reader.list(org), ScopedUnavailable)
    assert isinstance(reader.detail(org, 0), ScopedUnavailable)


def test_digest_mismatch_gap_and_v1_corruption_are_unavailable_without_v1_read() -> None:
    connection, org, reader, writer = _ready()
    event = _event("session.end", "s1")
    assert writer.append(org, event) is None
    connection.execute("UPDATE operational_audit_events_v2 SET event_digest=?", ("x" * 64,))
    assert isinstance(reader.list(org), ScopedUnavailable)

    connection, org, reader, writer = _ready()
    assert writer.append(org, event) is None
    connection.execute("UPDATE operational_audit_events_v2 SET seq=2")
    assert isinstance(reader.detail(org, 2), ScopedUnavailable)
    connection.execute(
        "INSERT INTO operational_audit_records VALUES ('acme', 0, '{bad', 'bad', 'now')"
    )
    assert isinstance(
        SqliteTenantAuditReader(connection, TenantOrgId("other")).list(TenantOrgId("other")), tuple
    )


def test_independent_connection_concurrency_creates_one_row_for_same_event(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    bootstrap = sqlite3.connect(path)
    migrate_sqlite_operational_tenant_sources(bootstrap)
    migrate_sqlite_tenant_port_audit_v2(bootstrap)
    bootstrap.close()
    event, org = _event("session.end", "s1"), TenantOrgId("acme")
    outcomes: list[object] = []
    lock = threading.Lock()

    def append() -> None:
        connection = sqlite3.connect(path, timeout=5.0)
        try:
            outcome = SqliteTenantAuditWriter(connection, org).append(org, event)
            with lock:
                outcomes.append(outcome)
        finally:
            connection.close()

    threads = [threading.Thread(target=append) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    verify = sqlite3.connect(path)
    assert outcomes == [None] * 8
    assert verify.execute("SELECT count(*) FROM operational_audit_events_v2").fetchone() == (1,)
