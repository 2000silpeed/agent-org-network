"""Immutable Bootstrap Admin seal for the Central installation database.

Its catalog check is scoped to this schema, just as Registry and Question schemas
are scoped, so all durable Central components can share one database safely.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import threading

from pydantic import BaseModel, ConfigDict, field_validator


class CentralBootstrapSealUnavailable(RuntimeError):
    """The seal catalog or its immutable evidence cannot be trusted."""


class CentralBootstrapSealConflict(RuntimeError):
    """A different bootstrap command already owns the configured organization."""


FaultInjector = Callable[[str], None]
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TABLE = "central_bootstrap_admin_seals"
_SCHEMA = f"""
CREATE TABLE {_TABLE} (
 org_id TEXT PRIMARY KEY NOT NULL,
 attestation_id TEXT NOT NULL UNIQUE,
 attestation_digest TEXT NOT NULL CHECK(length(attestation_digest)=64),
 registry_user_id TEXT NOT NULL,
 registration_command_digest TEXT NOT NULL CHECK(length(registration_command_digest)=64),
 authority_policy_digest TEXT NOT NULL CHECK(length(authority_policy_digest)=64),
 sealed_at TEXT NOT NULL,
 seal_digest TEXT NOT NULL UNIQUE CHECK(length(seal_digest)=64)
);
CREATE TRIGGER central_bootstrap_admin_seals_immutable
BEFORE UPDATE ON {_TABLE} BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_bootstrap_admin_seals_no_delete
BEFORE DELETE ON {_TABLE} BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""


def _no_fault(_point: str) -> None:
    return None


class BootstrapAdminSeal(BaseModel, frozen=True):
    """The canonical, public-safe immutable completion proof."""

    model_config = ConfigDict(extra="forbid", strict=True)

    org_id: str
    attestation_id: str
    attestation_digest: str
    registry_user_id: str
    registration_command_digest: str
    authority_policy_digest: str
    sealed_at: datetime
    seal_digest: str

    @field_validator("org_id", "attestation_id", "registry_user_id")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator(
        "attestation_digest",
        "registration_command_digest",
        "authority_policy_digest",
        "seal_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("lowercase SHA-256 digest required")
        return value

    @field_validator("sealed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware seal time required")
        return value


def bootstrap_admin_seal_digest(
    *,
    org_id: str,
    attestation_id: str,
    attestation_digest: str,
    registry_user_id: str,
    registration_command_digest: str,
    authority_policy_digest: str,
    sealed_at: datetime,
) -> str:
    payload = {
        "org_id": org_id,
        "attestation_id": attestation_id,
        "attestation_digest": attestation_digest,
        "registry_user_id": registry_user_id,
        "registration_command_digest": registration_command_digest,
        "authority_policy_digest": authority_policy_digest,
        "sealed_at": sealed_at.astimezone(UTC).isoformat(),
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _catalog(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (str(row[0]), str(row[1]), str(row[2]), " ".join(str(row[3] or "").split()))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name='central_bootstrap_admin_seals' "
            "OR tbl_name='central_bootstrap_admin_seals' ORDER BY type,name"
        )
    )


def _canonical_catalog() -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return _catalog(connection)
    finally:
        connection.close()


_CANONICAL_CATALOG = _canonical_catalog()


def _validate(connection: sqlite3.Connection) -> None:
    if _catalog(connection) != _CANONICAL_CATALOG:
        raise CentralBootstrapSealUnavailable()
    rows = connection.execute(
        f"SELECT org_id,attestation_id,attestation_digest,registry_user_id,"
        f"registration_command_digest,authority_policy_digest,sealed_at,seal_digest FROM {_TABLE}"
    ).fetchall()
    for row in rows:
        try:
            sealed_at = datetime.fromisoformat(str(row[6]))
            expected = bootstrap_admin_seal_digest(
                org_id=str(row[0]),
                attestation_id=str(row[1]),
                attestation_digest=str(row[2]),
                registry_user_id=str(row[3]),
                registration_command_digest=str(row[4]),
                authority_policy_digest=str(row[5]),
                sealed_at=sealed_at,
            )
            if expected != str(row[7]):
                raise ValueError
            BootstrapAdminSeal(
                org_id=str(row[0]),
                attestation_id=str(row[1]),
                attestation_digest=str(row[2]),
                registry_user_id=str(row[3]),
                registration_command_digest=str(row[4]),
                authority_policy_digest=str(row[5]),
                sealed_at=sealed_at,
                seal_digest=str(row[7]),
            )
        except Exception as error:
            raise CentralBootstrapSealUnavailable() from error


def migrate_central_bootstrap_admin_schema(
    path: Path, *, fault_injector: FaultInjector | None = None
) -> None:
    """Install exactly the immutable seal schema; partial/foreign files fail closed."""
    fault = fault_injector or _no_fault
    connection = sqlite3.connect(path)
    try:
        catalog = _catalog(connection)
        if catalog == _CANONICAL_CATALOG:
            _validate(connection)
            return
        if catalog:
            raise CentralBootstrapSealUnavailable()
        connection.execute("BEGIN EXCLUSIVE")
        if _catalog(connection):
            raise CentralBootstrapSealUnavailable()
        statement = ""
        for line in _SCHEMA.splitlines(keepends=True):
            statement += line
            if not sqlite3.complete_statement(statement):
                continue
            sql = statement.strip()
            statement = ""
            if sql:
                connection.execute(sql)
        if statement.strip():
            raise CentralBootstrapSealUnavailable()
        fault("pre-readback")
        _validate(connection)
        fault("before_commit")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def central_bootstrap_admin_schema_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        _validate(connection)
        return True
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


class CentralBootstrapAdminSealStore:
    """Small immutable-seal repository.  It never sees OIDC raw identity values."""

    def __init__(self, path: Path, *, fault_injector: FaultInjector | None = None) -> None:
        if not central_bootstrap_admin_schema_ready(path):
            raise CentralBootstrapSealUnavailable()
        self._fault = fault_injector or _no_fault
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self._connection.close()

    def get(self, org_id: str) -> BootstrapAdminSeal | None:
        with self._lock:
            try:
                _validate(self._connection)
                row = self._connection.execute(
                    f"SELECT org_id,attestation_id,attestation_digest,registry_user_id,"
                    f"registration_command_digest,authority_policy_digest,sealed_at,seal_digest "
                    f"FROM {_TABLE} WHERE org_id=?", (org_id,)
                ).fetchone()
                return None if row is None else _from_row(row)
            except CentralBootstrapSealUnavailable:
                raise
            except Exception as error:
                raise CentralBootstrapSealUnavailable() from error

    def seal(
        self,
        *,
        org_id: str,
        attestation_id: str,
        attestation_digest: str,
        registry_user_id: str,
        registration_command_digest: str,
        authority_policy_digest: str,
        sealed_at: datetime,
    ) -> BootstrapAdminSeal:
        try:
            candidate = _new_seal(
                org_id=org_id,
                attestation_id=attestation_id,
                attestation_digest=attestation_digest,
                registry_user_id=registry_user_id,
                registration_command_digest=registration_command_digest,
                authority_policy_digest=authority_policy_digest,
                sealed_at=sealed_at,
            )
        except Exception as error:
            raise CentralBootstrapSealUnavailable() from error
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                _validate(self._connection)
                existing = self.get(org_id)
                if existing is not None:
                    if _same_command(existing, candidate):
                        self._connection.commit()
                        return existing
                    raise CentralBootstrapSealConflict()
                self._connection.execute(
                    f"INSERT INTO {_TABLE} VALUES(?,?,?,?,?,?,?,?)",
                    (
                        candidate.org_id,
                        candidate.attestation_id,
                        candidate.attestation_digest,
                        candidate.registry_user_id,
                        candidate.registration_command_digest,
                        candidate.authority_policy_digest,
                        candidate.sealed_at.astimezone(UTC).isoformat(),
                        candidate.seal_digest,
                    ),
                )
                self._fault("before_commit")
                _validate(self._connection)
                self._connection.commit()
                return candidate
            except CentralBootstrapSealConflict:
                self._connection.rollback()
                raise
            except Exception as error:
                self._connection.rollback()
                raise CentralBootstrapSealUnavailable() from error


def _new_seal(**values: object) -> BootstrapAdminSeal:
    timestamp = values["sealed_at"]
    if not isinstance(timestamp, datetime):
        raise ValueError
    digest = bootstrap_admin_seal_digest(
        org_id=str(values["org_id"]),
        attestation_id=str(values["attestation_id"]),
        attestation_digest=str(values["attestation_digest"]),
        registry_user_id=str(values["registry_user_id"]),
        registration_command_digest=str(values["registration_command_digest"]),
        authority_policy_digest=str(values["authority_policy_digest"]),
        sealed_at=timestamp,
    )
    return BootstrapAdminSeal(**values, seal_digest=digest)  # type: ignore[arg-type]


def _from_row(row: sqlite3.Row) -> BootstrapAdminSeal:
    return BootstrapAdminSeal(
        org_id=str(row["org_id"]),
        attestation_id=str(row["attestation_id"]),
        attestation_digest=str(row["attestation_digest"]),
        registry_user_id=str(row["registry_user_id"]),
        registration_command_digest=str(row["registration_command_digest"]),
        authority_policy_digest=str(row["authority_policy_digest"]),
        sealed_at=datetime.fromisoformat(str(row["sealed_at"])),
        seal_digest=str(row["seal_digest"]),
    )


def _same_command(left: BootstrapAdminSeal, right: BootstrapAdminSeal) -> bool:
    return (
        left.org_id,
        left.attestation_id,
        left.attestation_digest,
        left.registry_user_id,
        left.registration_command_digest,
        left.authority_policy_digest,
    ) == (
        right.org_id,
        right.attestation_id,
        right.attestation_digest,
        right.registry_user_id,
        right.registration_command_digest,
        right.authority_policy_digest,
    )


__all__ = [
    "BootstrapAdminSeal",
    "CentralBootstrapAdminSealStore",
    "CentralBootstrapSealConflict",
    "CentralBootstrapSealUnavailable",
    "bootstrap_admin_seal_digest",
    "central_bootstrap_admin_schema_ready",
    "migrate_central_bootstrap_admin_schema",
]
