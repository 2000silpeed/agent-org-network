import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.sqlite_durable_worker_credential import (
    SqliteDurableWorkerCredentialVerifier,
)
from agent_org_network.transport import RegisterWorker

NOW = datetime(2026, 7, 27, tzinfo=UTC)


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "credentials.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE durable_credentials (credential_id TEXT NOT NULL, org_id TEXT NOT NULL,"
        "owner_subject_id TEXT NOT NULL, role TEXT NOT NULL, generation INTEGER NOT NULL,"
        "revision INTEGER NOT NULL, status TEXT NOT NULL, secret_hash TEXT NOT NULL,"
        "issued_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT,"
        "PRIMARY KEY(org_id,credential_id),CHECK(generation>=1),CHECK(revision>=1),"
        "CHECK(status IN ('active','revoked')))"
    )
    connection.execute(
        "INSERT INTO durable_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "cred-7",
            "org",
            "owner",
            "primary",
            7,
            1,
            "active",
            hashlib.sha256(b"secret").hexdigest(),
            NOW.isoformat(),
            (NOW + timedelta(hours=1)).isoformat(),
            None,
        ),
    )
    connection.commit()
    connection.close()
    return path


def test_canonical_row_generation을_그대로_principal에_보존한다(tmp_path: Path) -> None:
    verifier = SqliteDurableWorkerCredentialVerifier(_db(tmp_path), org_id="org")
    principal = verifier.authenticate(
        RegisterWorker(owner_id="owner", role="primary", token="cred-7.secret"),
        now=NOW,
    )
    assert principal is not None
    assert principal.credential_generation == 7
    assert verifier.is_current(principal, now=NOW)


def test_secret_owner_role_mismatch와_revoke_expiry는_fail_closed(tmp_path: Path) -> None:
    path = _db(tmp_path)
    verifier = SqliteDurableWorkerCredentialVerifier(path, org_id="org")
    for frame in (
        RegisterWorker(owner_id="owner", token="cred-7.wrong"),
        RegisterWorker(owner_id="other", token="cred-7.secret"),
        RegisterWorker(owner_id="owner", role="backup", token="cred-7.secret"),
    ):
        assert verifier.authenticate(frame, now=NOW) is None
    assert (
        verifier.authenticate(
            RegisterWorker(owner_id="owner", token="cred-7.secret"),
            now=NOW + timedelta(hours=2),
        )
        is None
    )
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE durable_credentials SET status='revoked',revoked_at=?", (NOW.isoformat(),)
    )
    connection.commit()
    connection.close()
    assert (
        verifier.authenticate(
            RegisterWorker(owner_id="owner", token="cred-7.secret"), now=NOW
        )
        is None
    )


def test_legacy_only_or_schema_missing은_자동_DDL없이_거부한다(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    sqlite3.connect(path).close()
    with pytest.raises(ValueError):
        SqliteDurableWorkerCredentialVerifier(path, org_id="org")
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT name FROM sqlite_schema WHERE name='durable_credentials'"
    ).fetchone() is None
