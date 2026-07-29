from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

import agent_org_network.production_identity_sessions as module
from agent_org_network.production_identity_sessions import (
    ProductionIdentityUnavailable,
    SqliteProductionIdentitySessions,
)


RAW_SESSION = "s" * 32


def _v1(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            module._SCHEMA_V1  # pyright: ignore[reportPrivateUsage]
        )
        connection.execute(
            "INSERT INTO production_identity_sessions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                RAW_SESSION,
                "acme",
                "owner",
                "a" * 64,
                "b" * 64,
                7,
                "c" * 64,
                "2026-08-01T00:00:00+00:00",
                0,
            ),
        )
        return connection.execute(
            "SELECT * FROM production_identity_sessions"
        ).fetchone()


def test_nonempty_v1은_digest만추가하고모든byte_semantics를보존한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity.sqlite"
    before = _v1(path)
    SqliteProductionIdentitySessions.migrate_digest_lookup(path)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM production_identity_sessions"
        ).fetchone()
        assert row == (
            before[0],
            sha256(RAW_SESSION.encode()).hexdigest(),
            *before[1:],
        )
        assert row[1] != RAW_SESSION
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name LIKE 'central_owner_pairing_%'"
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE production_identity_sessions "
                "SET identity_session_digest=?",
                ("d" * 64,),
            )
    SqliteProductionIdentitySessions.migrate_digest_lookup(path)


@pytest.mark.parametrize(
    "point", ["before_rewrite", "after_schema", "after_copy", "before_commit"]
)
def test_migration_fault는_exact_v1과row를rollback한다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "identity.sqlite"
    before = _v1(path)

    def fault(current: str) -> None:
        if current == point:
            raise RuntimeError("internal-secret")

    with pytest.raises(RuntimeError):
        SqliteProductionIdentitySessions.migrate_digest_lookup(
            path, fault_injector=fault
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT * FROM production_identity_sessions"
        ).fetchone() == before
        assert (
            module._catalog(connection)  # pyright: ignore[reportPrivateUsage]
            == module._CATALOG_V1  # pyright: ignore[reportPrivateUsage]
        )


def test_concurrent_migration은한_exact_v2로수렴한다(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite"
    _v1(path)
    barrier = Barrier(16)

    def migrate(_index: int) -> None:
        barrier.wait()
        SqliteProductionIdentitySessions.migrate_digest_lookup(path)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(migrate, range(16)))
    with sqlite3.connect(path) as connection:
        assert (
            module._catalog(connection)  # pyright: ignore[reportPrivateUsage]
            == module._CATALOG  # pyright: ignore[reportPrivateUsage]
        )
        assert connection.execute(
            "SELECT identity_session_digest FROM production_identity_sessions"
        ).fetchone() == (sha256(RAW_SESSION.encode()).hexdigest(),)


def test_partial_or_tampered_v1은repair하지않는다(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite"
    _v1(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE INDEX attacker_index ON production_identity_sessions(org_id)"
        )
    with pytest.raises(ProductionIdentityUnavailable):
        SqliteProductionIdentitySessions.migrate_digest_lookup(path)
