"""Durable worker credential의 sealed SQLite verifier/current source."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from agent_org_network.transport import RegisterWorker
from agent_org_network.worker_authorization import WorkerConnectionPrincipal

_COLUMNS = (
    "credential_id",
    "org_id",
    "owner_subject_id",
    "role",
    "generation",
    "revision",
    "status",
    "secret_hash",
    "issued_at",
    "expires_at",
    "revoked_at",
)


class SqliteDurableWorkerCredentialVerifier:
    """`credential_id.raw_secret` wire credential을 canonical row와 대조한다."""

    def __init__(self, path: str | Path, *, org_id: str) -> None:
        self._path = Path(path)
        self._org_id = org_id
        self._validate_schema()

    def authenticate(
        self, frame: RegisterWorker, *, now: datetime
    ) -> WorkerConnectionPrincipal | None:
        if frame.token is None or now.utcoffset() is None:
            return None
        credential_id, separator, raw_secret = frame.token.partition(".")
        if not separator or not credential_id or not raw_secret:
            return None
        row = self._row(credential_id)
        if row is None or not self._active(row, now):
            return None
        digest = hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, row["secret_hash"]):
            return None
        if row["owner_subject_id"] != frame.owner_id or row["role"] != frame.role:
            return None
        return WorkerConnectionPrincipal(
            org_id=row["org_id"],
            owner_id=row["owner_subject_id"],
            credential_id=row["credential_id"],
            credential_generation=row["generation"],
            role=row["role"],
            connection_epoch=uuid.uuid4().hex,
        )

    def is_current(self, principal: WorkerConnectionPrincipal, *, now: datetime) -> bool:
        row = self._row(principal.credential_id)
        return bool(
            row is not None
            and self._active(row, now)
            and row["owner_subject_id"] == principal.owner_id
            and row["role"] == principal.role
            and row["generation"] == principal.credential_generation
        )

    def _row(self, credential_id: str) -> sqlite3.Row | None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._path)
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM durable_credentials WHERE org_id=? AND credential_id=?",
                (self._org_id, credential_id),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _active(row: sqlite3.Row, now: datetime) -> bool:
        if row["status"] != "active" or row["revoked_at"] is not None:
            return False
        expires = row["expires_at"]
        if expires is None:
            return True
        try:
            expiry = datetime.fromisoformat(expires.replace("Z", "+00:00")).astimezone(UTC)
        except (TypeError, ValueError):
            return False
        return now.astimezone(UTC) < expiry

    def _validate_schema(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._path)
            rows = connection.execute("PRAGMA table_info(durable_credentials)").fetchall()
            schema_row = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name='durable_credentials'"
            ).fetchone()
        except sqlite3.Error as error:
            raise ValueError("durable worker credential schema unavailable") from error
        finally:
            if connection is not None:
                connection.close()
        sql = "" if schema_row is None else "".join(schema_row[0].lower().split())
        if (
            tuple(row[1] for row in rows) != _COLUMNS
            or tuple(row[5] for row in rows[:2]) != (2, 1)
            or "check(generation>=1)" not in sql
            or "check(revision>=1)" not in sql
            or "check(statusin('active','revoked'))" not in sql
        ):
            raise ValueError("durable worker credential schema unavailable")


__all__ = ["SqliteDurableWorkerCredentialVerifier"]
