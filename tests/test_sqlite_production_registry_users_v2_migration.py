from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from typing import cast

import pytest

import agent_org_network.sqlite_production_registry_users as registry_module
from agent_org_network.sqlite_production_registry_users import (
    LegacyProductionRegistryUsersUnverifiable,
    ProductionRegistryUserUnavailable,
    SqliteProductionRegistryUsers,
    validate_production_registry_user_connection,
)


def _legacy(path: Path) -> None:
    schema = cast(str, getattr(registry_module, "_LEGACY_V1_SCHEMA"))
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    connection.commit()
    connection.close()


def _columns(path: Path, table: str) -> tuple[str, ...]:
    connection = sqlite3.connect(path)
    result = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
    connection.close()
    return result


def test_empty_canonical_v1만_v2로_migrate하고_reopen은_idempotent다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    _legacy(path)
    SqliteProductionRegistryUsers.migrate_v2(path)
    assert "operation" in _columns(path, "production_registry_user_command_receipts")
    before = path.read_bytes()
    SqliteProductionRegistryUsers.migrate_v2(path)
    assert path.read_bytes() == before
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    validate_production_registry_user_connection(connection)
    assert connection.execute("SELECT count(*) FROM production_registry_users").fetchone()[0] == 0


def test_nonempty_v1은_counts_reason만가진_typed_unverifiable_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    _legacy(path)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO production_registry_revisions VALUES ('acme',1)")
    connection.execute(
        "INSERT INTO production_registry_users VALUES "
        "('acme','owner','private@example.com',NULL,1)"
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(LegacyProductionRegistryUsersUnverifiable) as raised:
        SqliteProductionRegistryUsers.migrate_v2(path)
    assert raised.value.reason == "legacy_nonempty"
    assert raised.value.counts["production_registry_users"] == 1
    assert "private@example.com" not in repr(raised.value.__dict__)
    assert path.read_bytes() == before


def test_rows0이어도_owned_autoincrement_history가_있으면_unverifiable_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    _legacy(path)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO production_registry_revisions VALUES ('acme',1)")
    connection.execute(
        "INSERT INTO production_registry_users VALUES "
        "('acme','owner','private@example.com',NULL,1)"
    )
    connection.execute(
        "INSERT INTO production_registry_user_audit "
        "(org_id,action,principal_id,subject_id,approval_evidence_digest,"
        "command_digest,result_revision) VALUES "
        "('acme','UserRegistered','owner','owner','evidence','command',1)"
    )
    connection.execute(
        "INSERT INTO production_registry_user_outbox "
        "(org_id,kind,subject_id,command_digest,result_revision) VALUES "
        "('acme','registry.user_registered','owner','command',1)"
    )
    connection.execute("DELETE FROM production_registry_user_outbox")
    connection.execute("DELETE FROM production_registry_user_audit")
    connection.execute("DELETE FROM production_registry_users")
    connection.execute("DELETE FROM production_registry_revisions")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(LegacyProductionRegistryUsersUnverifiable) as raised:
        SqliteProductionRegistryUsers.migrate_v2(path)

    assert raised.value.reason == "legacy_history_unverifiable"
    assert raised.value.counts["autoincrement_history"] == 2
    assert "private@example.com" not in repr(raised.value.__dict__)
    assert path.read_bytes() == before


def test_unknown_owned_sqlite_sequence_entry는_unverifiable_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    _legacy(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO sqlite_sequence(name,seq) VALUES "
        "('production_registry_users',0)"
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(LegacyProductionRegistryUsersUnverifiable) as raised:
        SqliteProductionRegistryUsers.migrate_v2(path)

    assert raised.value.reason == "legacy_history_unverifiable"
    assert raised.value.counts["autoincrement_history"] == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "names",
    [
        ("production_registry_user_audit",),
        ("production_registry_user_outbox",),
        (
            "production_registry_user_audit",
            "production_registry_user_outbox",
        ),
    ],
)
def test_owned_autoincrement_single_seq0조합은_never_used로_migrate한다(
    tmp_path: Path, names: tuple[str, ...]
) -> None:
    path = tmp_path / "registry.db"
    _legacy(path)
    connection = sqlite3.connect(path)
    connection.executemany(
        "INSERT INTO sqlite_sequence(name,seq) VALUES (?,0)",
        ((name,) for name in names),
    )
    connection.commit()
    connection.close()

    SqliteProductionRegistryUsers.migrate_v2(path)

    assert "operation" in _columns(path, "production_registry_user_command_receipts")


@pytest.mark.parametrize("sequences", [(0, 0), (0, 1)])
def test_owned_autoincrement_duplicate_sequence는_unverifiable_write0이다(
    tmp_path: Path, sequences: tuple[int, int]
) -> None:
    path = tmp_path / "registry.db"
    _legacy(path)
    connection = sqlite3.connect(path)
    connection.executemany(
        "INSERT INTO sqlite_sequence(name,seq) VALUES "
        "('production_registry_user_audit',?)",
        ((sequence,) for sequence in sequences),
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(LegacyProductionRegistryUsersUnverifiable) as raised:
        SqliteProductionRegistryUsers.migrate_v2(path)

    assert raised.value.reason == "legacy_history_unverifiable"
    assert raised.value.counts["autoincrement_history"] >= 1
    assert path.read_bytes() == before


def test_partial_extra_tampered_v1은_unavailable_migration0이다(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE production_registry_users(x TEXT)")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ProductionRegistryUserUnavailable):
        SqliteProductionRegistryUsers.migrate_v2(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "point", ["pre-drop", "after-drop", "mid-DDL", "pre-readback", "precommit"]
)
def test_v1_migration_fault는_original_exact_v1으로_rollback한다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "registry.db"
    _legacy(path)
    original_columns = _columns(path, "production_registry_user_command_receipts")

    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError("fault")

    with pytest.raises(RuntimeError):
        SqliteProductionRegistryUsers.migrate_v2(path, fault_injector=fault)
    assert _columns(path, "production_registry_user_command_receipts") == original_columns
    assert "operation" not in original_columns


def test_two_concurrent_migrations는_exact_v2_partial0으로_수렴한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    _legacy(path)

    def migrate(_index: int) -> None:
        SqliteProductionRegistryUsers.migrate_v2(path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(migrate, range(2)))
    assert "operation" in _columns(path, "production_registry_user_command_receipts")
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE name LIKE 'production_registry_%' AND sql IS NULL"
    ).fetchone()[0] == 0
