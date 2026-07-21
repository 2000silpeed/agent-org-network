from __future__ import annotations

import sqlite3
import threading
import re
from collections.abc import Callable
from pathlib import Path

from agent_org_network.sqlite_operational_tenant_sources import (
    migrate_sqlite_operational_tenant_sources,
)
from agent_org_network.sqlite_tenant_state_adapters import (
    SqliteTenantGraphAdapter,
    SqliteTenantHitlAdapter,
    SqliteTenantRegistryAdapter,
    SqliteTenantSessionAdapter,
)
from agent_org_network.tenant_operational_ports import ScopedUnavailable, TenantCard, TenantOrgId, TenantSession


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_operational_tenant_sources(connection)
    return connection


def _card(org: str, revision: int, card_id: str, owner_id: str) -> TenantCard:
    from agent_org_network.tenant_operational_ports import ResourceFingerprint

    return TenantCard(
        card_id,
        owner_id,
        ResourceFingerprint.from_scalars("tenant-card-v1", org, str(revision), card_id, owner_id),
    )


def test_registry_admit_transfer_and_root_first_graph_are_idempotent() -> None:
    connection = _connection()
    org = TenantOrgId("acme")
    repository = SqliteTenantRegistryAdapter(connection, org)
    root = _card("acme", 0, "a-root", "owner-a")
    assert isinstance(repository.admit(org, root), ScopedUnavailable)
    # Owner records are a source prerequisite; cards alone must not mint an owner.
    from agent_org_network.sqlite_operational_tenant_sources import open_sqlite_operational_tenant_sources

    assert open_sqlite_operational_tenant_sources(connection).registry("acme").compare_and_set(
        None,
        {"users": ["owner-a", "owner-b"], "cards": {}, "manager_refs": {}},
        "2026-07-19T00:00:00.000Z",
    )
    assert repository.admit(org, root) == _card("acme", 1, "a-root", "owner-a")
    assert repository.admit(org, _card("acme", 1, "a-root", "owner-a")) == _card("acme", 1, "a-root", "owner-a")
    assert repository.transfer(org, "a-root", "owner-b") == _card("acme", 2, "a-root", "owner-b")
    assert repository.transfer(org, "a-root", "owner-b") == _card("acme", 2, "a-root", "owner-b")
    assert SqliteTenantGraphAdapter(connection, org).derive(org) == (_card("acme", 2, "a-root", "owner-b"),)


def test_registry_admit_rejects_forged_or_stale_card_fingerprint_without_write() -> None:
    connection = _connection()
    org = TenantOrgId("acme")
    from agent_org_network.sqlite_operational_tenant_sources import open_sqlite_operational_tenant_sources
    from agent_org_network.tenant_operational_ports import ResourceFingerprint

    repository = SqliteTenantRegistryAdapter(connection, org)
    assert open_sqlite_operational_tenant_sources(connection).registry("acme").compare_and_set(
        None, {"users": ["owner"], "cards": {}, "manager_refs": {}}, "2026-07-19T00:00:00.000Z"
    )
    forged = TenantCard("card", "owner", ResourceFingerprint.from_scalars("forged", "card"))
    assert isinstance(repository.admit(org, forged), ScopedUnavailable)
    assert repository.card(org, "card") == ScopedUnavailable()
    assert isinstance(repository.admit(org, _card("acme", 9, "card", "owner")), ScopedUnavailable)
    assert repository.card(org, "card") == ScopedUnavailable()


def test_session_end_and_hitl_write_are_cas_idempotent() -> None:
    connection = _connection()
    org = TenantOrgId("acme")
    connection.execute(
        "INSERT INTO operational_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("acme", "s", "u", "active", "2026-07-19T00:00:00.000Z", "2026-07-19T00:00:00.000Z", 0),
    )
    connection.commit()
    sessions = SqliteTenantSessionAdapter(connection, org)
    ended = sessions.end(org, "s")
    assert isinstance(ended, TenantSession)
    assert ended.status == "ended"
    assert sessions.end(org, "s") == ended

    hitl = SqliteTenantHitlAdapter(connection, org)
    assert hitl.read(org, "card") is False
    assert hitl.write(org, "card", False) is False
    assert hitl.write(org, "card", True) is True
    assert hitl.write(org, "card", True) is True
    assert hitl.read(org, "card") is True


def test_bound_org_and_corrupt_rows_are_unavailable() -> None:
    connection = _connection()
    org = TenantOrgId("acme")
    foreign = TenantOrgId("other")
    assert isinstance(SqliteTenantHitlAdapter(connection, org).read(foreign, "card"), ScopedUnavailable)
    connection.execute(
        "INSERT INTO operational_hitl_toggles VALUES (?, ?, ?, ?, ?, ?)",
        ("acme", "bad", 1, 0, 0, "2026-07-19T00:00:00.000Z"),
    )
    assert isinstance(SqliteTenantHitlAdapter(connection, org).read(org, "bad"), ScopedUnavailable)


def test_graph_is_root_first_then_binary_card_id_regardless_of_payload_order() -> None:
    connection = _connection()
    org = TenantOrgId("acme")
    from agent_org_network.sqlite_operational_tenant_sources import open_sqlite_operational_tenant_sources

    # `child -> manager`; card map order must never become graph traversal order.
    payload = {
        "users": ["owner"],
        "cards": {
            "z-root": {"owner": "owner"},
            "a-child": {"owner": "owner"},
            "b-root": {"owner": "owner"},
            "A-child": {"owner": "owner"},
            "a-root": {"owner": "owner"},
        },
        "manager_refs": {"a-child": "a-root", "A-child": "a-root"},
    }
    assert open_sqlite_operational_tenant_sources(connection).registry("acme").compare_and_set(
        None, payload, "2026-07-19T00:00:00.000Z"
    )
    expected = (
        "a-root",
        "b-root",
        "z-root",
        "A-child",
        "a-child",
    )
    first = SqliteTenantGraphAdapter(connection, org).derive(org)
    assert not isinstance(first, ScopedUnavailable)
    assert tuple(card.card_id for card in first) == expected
    reordered = {
        "users": ["owner"],
        "cards": {
            "a-root": {"owner": "owner"},
            "A-child": {"owner": "owner"},
            "b-root": {"owner": "owner"},
            "a-child": {"owner": "owner"},
            "z-root": {"owner": "owner"},
        },
        "manager_refs": {"A-child": "a-root", "a-child": "a-root"},
    }
    assert open_sqlite_operational_tenant_sources(connection).registry("acme").compare_and_set(
        0, reordered, "2026-07-19T00:00:01.000Z"
    )
    second = SqliteTenantGraphAdapter(connection, org).derive(org)
    assert not isinstance(second, ScopedUnavailable)
    assert tuple(card.card_id for card in second) == expected


def test_same_ids_are_isolated_between_tenant_state_adapters() -> None:
    connection = _connection()
    from agent_org_network.sqlite_operational_tenant_sources import open_sqlite_operational_tenant_sources

    capability = open_sqlite_operational_tenant_sources(connection)
    for org_id, owner, user, on in (("acme", "owner-a", "user-a", 0), ("other", "owner-b", "user-b", 1)):
        assert capability.registry(org_id).compare_and_set(
            None,
            {"users": [owner], "cards": {"same": {"owner": owner}}, "manager_refs": {}},
            "2026-07-19T00:00:00.000Z",
        )
        connection.commit()
        connection.execute(
            "INSERT INTO operational_sessions VALUES (?, 'same', ?, 'active', ?, ?, 0)",
            (org_id, user, "2026-07-19T00:00:00.000Z", "2026-07-19T00:00:00.000Z"),
        )
        connection.execute(
            "INSERT INTO operational_hitl_toggles VALUES (?, 'same', ?, 1, 0, ?)",
            (org_id, on, "2026-07-19T00:00:00.000Z"),
        )
        connection.commit()
    connection.commit()
    acme, other = TenantOrgId("acme"), TenantOrgId("other")
    assert SqliteTenantRegistryAdapter(connection, acme).card(acme, "same").owner_id == "owner-a"  # type: ignore[union-attr]
    assert SqliteTenantRegistryAdapter(connection, other).card(other, "same").owner_id == "owner-b"  # type: ignore[union-attr]
    assert SqliteTenantSessionAdapter(connection, acme).session(acme, "same").user_id == "user-a"  # type: ignore[union-attr]
    assert SqliteTenantSessionAdapter(connection, other).session(other, "same").user_id == "user-b"  # type: ignore[union-attr]
    assert SqliteTenantHitlAdapter(connection, acme).read(acme, "same") is False
    assert SqliteTenantHitlAdapter(connection, other).read(other, "same") is True


def test_independent_connections_cas_races_leave_one_state_transition(tmp_path: Path) -> None:
    path = tmp_path / "tenant-state.sqlite3"
    bootstrap = sqlite3.connect(path)
    migrate_sqlite_operational_tenant_sources(bootstrap)
    from agent_org_network.sqlite_operational_tenant_sources import open_sqlite_operational_tenant_sources

    source = open_sqlite_operational_tenant_sources(bootstrap)
    assert source.registry("acme").compare_and_set(
        None, {"users": ["owner-a", "owner-b"], "cards": {}, "manager_refs": {}}, "2026-07-19T00:00:00.000Z"
    )
    started_at = "2026-07-19T00:00:00.000Z"
    bootstrap.execute(
        "INSERT INTO operational_sessions VALUES ('acme', 's', 'u', 'active', ?, ?, 0)",
        (started_at, started_at),
    )
    bootstrap.commit()

    def race(operation: Callable[[sqlite3.Connection], object]) -> list[object]:
        barrier = threading.Barrier(8)
        failures: list[BaseException] = []
        outcomes: list[object] = []
        lock = threading.Lock()

        def contender() -> None:
            connection = sqlite3.connect(path, timeout=5)
            try:
                barrier.wait(timeout=5)
                outcome = operation(connection)
                with lock:
                    outcomes.append(outcome)
            except BaseException as error:  # pragma: no cover - assertion below reports it
                failures.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=contender) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not failures
        assert len(outcomes) == 8
        return outcomes

    org = TenantOrgId("acme")
    admitted = race(lambda connection: SqliteTenantRegistryAdapter(connection, org).admit(org, _card("acme", 0, "card", "owner-a")))
    assert sum(isinstance(result, TenantCard) for result in admitted) == 1
    assert all(isinstance(result, (TenantCard, ScopedUnavailable)) for result in admitted)
    check = sqlite3.connect(path)
    capability = open_sqlite_operational_tenant_sources(check)
    registry = capability.registry("acme").read()
    assert registry is not None and registry[0] == 1
    transferred = race(lambda connection: SqliteTenantRegistryAdapter(connection, org).transfer(org, "card", "owner-b"))
    assert all(isinstance(result, TenantCard) and result.owner_id == "owner-b" for result in transferred)
    registry = capability.registry("acme").read()
    assert registry is not None and registry[0] == 2
    ended = race(lambda connection: SqliteTenantSessionAdapter(connection, org).end(org, "s"))
    assert all(isinstance(result, TenantSession) and result.status == "ended" for result in ended)
    session_row = capability.sessions("acme").get("s")
    assert session_row is not None
    assert session_row[2] == "ended"
    assert session_row[5] == 1
    last_active = session_row[4]
    assert type(last_active) is str
    assert last_active != started_at
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", last_active)
    hitl = race(lambda connection: SqliteTenantHitlAdapter(connection, org).write(org, "card", True))
    assert hitl == [True] * 8
    assert capability.hitl("acme").get("card") == (True, True, 0)
    conflicts = race(
        lambda connection: SqliteTenantRegistryAdapter(connection, org).admit(
            org, _card("acme", 2, "card", "owner-a")
        )
    )
    assert all(isinstance(result, ScopedUnavailable) for result in conflicts)
    assert capability.registry("acme").read() is not None and capability.registry("acme").read()[0] == 2  # type: ignore[index]
    check.close()
