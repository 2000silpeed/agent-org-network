"""Durable immutable Bootstrap Admin seal contract."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest

from agent_org_network.central_bootstrap_sqlite import (
    CentralBootstrapAdminSealStore,
    CentralBootstrapSealConflict,
    CentralBootstrapSealUnavailable,
    central_bootstrap_admin_schema_ready,
    migrate_central_bootstrap_admin_schema,
)


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _seal(store: CentralBootstrapAdminSealStore, **changes: object) -> object:
    values: dict[str, object] = {
        "org_id": "acme",
        "attestation_id": "att-1",
        "attestation_digest": "a" * 64,
        "registry_user_id": "root",
        "registration_command_digest": "b" * 64,
        "authority_policy_digest": "c" * 64,
        "sealed_at": NOW,
    }
    values.update(changes)
    return store.seal(**values)  # type: ignore[arg-type]


def test_migration_and_exact_replay_are_immutable(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.sqlite3"
    migrate_central_bootstrap_admin_schema(path)
    store = CentralBootstrapAdminSealStore(path)
    first = _seal(store)
    replay = _seal(store, sealed_at=NOW.replace(minute=1))

    assert central_bootstrap_admin_schema_ready(path)
    assert replay == first
    assert store.get("acme") == first
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        connection.execute("UPDATE central_bootstrap_admin_seals SET registry_user_id='other'")
    connection.close()


def test_other_attestation_or_command_is_conflict(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.sqlite3"
    migrate_central_bootstrap_admin_schema(path)
    store = CentralBootstrapAdminSealStore(path)
    _seal(store)
    with pytest.raises(CentralBootstrapSealConflict):
        _seal(store, attestation_id="att-2", attestation_digest="d" * 64)
    with pytest.raises(CentralBootstrapSealConflict):
        _seal(store, registration_command_digest="d" * 64)


def test_partial_or_tampered_catalog_is_not_repaired(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE central_bootstrap_admin_seals (bad TEXT)")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(CentralBootstrapSealUnavailable):
        migrate_central_bootstrap_admin_schema(path)
    assert path.read_bytes() == before
    assert not central_bootstrap_admin_schema_ready(path)


def test_fault_does_not_leave_a_claimed_schema(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.sqlite3"

    def fault(point: str) -> None:
        if point == "before_commit":
            raise RuntimeError

    with pytest.raises(RuntimeError):
        migrate_central_bootstrap_admin_schema(path, fault_injector=fault)
    assert not central_bootstrap_admin_schema_ready(path)
    migrate_central_bootstrap_admin_schema(path)
    assert central_bootstrap_admin_schema_ready(path)


def test_restart_readback_rejects_digest_tampering(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.sqlite3"
    migrate_central_bootstrap_admin_schema(path)
    store = CentralBootstrapAdminSealStore(path)
    _seal(store)
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER central_bootstrap_admin_seals_immutable")
    connection.execute("UPDATE central_bootstrap_admin_seals SET seal_digest='d' || substr(seal_digest,2)")
    connection.commit()
    connection.close()

    assert not central_bootstrap_admin_schema_ready(path)
    with pytest.raises(CentralBootstrapSealUnavailable):
        CentralBootstrapAdminSealStore(path)
