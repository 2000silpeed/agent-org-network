"""Production verified-email identity proof and opaque durable sessions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
import re
import secrets
import sqlite3

from pydantic import BaseModel

from agent_org_network.central_authority import AuthenticatedPrincipal
from agent_org_network.sqlite_production_registry_users import (
    ProductionRegistryUser,
    SqliteProductionRegistryUsers,
)


class ProductionIdentityUnavailable(Exception):
    pass


class VerifiedEmailIdentityProof(BaseModel, frozen=True):
    provider_id: str
    issuer: str
    email: str
    email_verified: bool


class ProductionAuthenticatedIdentity(BaseModel, frozen=True):
    identity_session_id: str
    principal: AuthenticatedPrincipal
    registry_revision: int
    expires_at: datetime


_SCHEMA_V1 = """
CREATE TABLE production_identity_sessions (
 identity_session_id TEXT PRIMARY KEY,
 org_id TEXT NOT NULL, user_id TEXT NOT NULL,
 email_digest TEXT NOT NULL, registry_fingerprint TEXT NOT NULL,
 registry_revision INTEGER NOT NULL, provider_digest TEXT NOT NULL,
 expires_at TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE TRIGGER production_identity_sessions_identity_immutable
BEFORE UPDATE OF identity_session_id,org_id,user_id,email_digest,registry_fingerprint,
registry_revision,provider_digest,expires_at ON production_identity_sessions
BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""

_SCHEMA = """
CREATE TABLE production_identity_sessions (
 identity_session_id TEXT PRIMARY KEY,
 identity_session_digest TEXT NOT NULL UNIQUE CHECK(length(identity_session_digest)=64),
 org_id TEXT NOT NULL, user_id TEXT NOT NULL,
 email_digest TEXT NOT NULL, registry_fingerprint TEXT NOT NULL,
 registry_revision INTEGER NOT NULL, provider_digest TEXT NOT NULL,
 expires_at TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE TRIGGER production_identity_sessions_identity_immutable
BEFORE UPDATE OF identity_session_id,identity_session_digest,org_id,user_id,email_digest,registry_fingerprint,
registry_revision,provider_digest,expires_at ON production_identity_sessions
BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""


def _catalog(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,replace(replace(trim(sql),char(10),' '),'  ',' ') "
            "FROM sqlite_master WHERE name LIKE 'production_identity_%' "
            "OR tbl_name='production_identity_sessions' ORDER BY type,name"
        )
    )


def _canonical_catalog() -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return _catalog(connection)
    finally:
        connection.close()


_CATALOG = _canonical_catalog()


def _catalog_for(schema: str) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(schema)
        return _catalog(connection)
    finally:
        connection.close()


_CATALOG_V1 = _catalog_for(_SCHEMA_V1)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _fingerprint(user: ProductionRegistryUser) -> str:
    return _digest(
        f"{user.org_id}\x00{user.user_id}\x00{user.email}\x00"
        f"{user.manager_id or ''}\x00{user.revision}"
    )


class SqliteProductionIdentitySessions:
    @classmethod
    def migrate(cls, path: str | Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name LIKE 'production_identity_%'"
            ).fetchone():
                raise ProductionIdentityUnavailable()
            connection.executescript(_SCHEMA)
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def migrate_digest_lookup(
        cls,
        path: str | Path,
        *,
        fault_injector: Callable[[str], None] = lambda _point: None,
    ) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = _catalog(connection)
            if current == _CATALOG:
                connection.commit()
                return
            if current != _CATALOG_V1:
                raise ProductionIdentityUnavailable()
            rows = connection.execute(
                "SELECT identity_session_id,org_id,user_id,email_digest,"
                "registry_fingerprint,registry_revision,provider_digest,expires_at,active "
                "FROM production_identity_sessions ORDER BY identity_session_id"
            ).fetchall()
            digests: set[str] = set()
            for row in rows:
                if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", row[0]) is None:
                    raise ProductionIdentityUnavailable()
                digest = _digest(row[0])
                if digest in digests:
                    raise ProductionIdentityUnavailable()
                digests.add(digest)
            fault_injector("before_rewrite")
            connection.execute(
                "DROP TRIGGER production_identity_sessions_identity_immutable"
            )
            connection.execute(
                "ALTER TABLE production_identity_sessions "
                "RENAME TO production_identity_sessions_v1"
            )
            connection.execute(
                "CREATE TABLE production_identity_sessions ("
                " identity_session_id TEXT PRIMARY KEY,"
                " identity_session_digest TEXT NOT NULL UNIQUE "
                "CHECK(length(identity_session_digest)=64),"
                " org_id TEXT NOT NULL, user_id TEXT NOT NULL,"
                " email_digest TEXT NOT NULL, registry_fingerprint TEXT NOT NULL,"
                " registry_revision INTEGER NOT NULL, provider_digest TEXT NOT NULL,"
                " expires_at TEXT NOT NULL, active INTEGER NOT NULL "
                "CHECK(active IN (0,1)) )"
            )
            connection.execute(
                "CREATE TRIGGER production_identity_sessions_identity_immutable "
                "BEFORE UPDATE OF identity_session_id,identity_session_digest,org_id,"
                "user_id,email_digest,registry_fingerprint, registry_revision,"
                "provider_digest,expires_at ON production_identity_sessions "
                "BEGIN SELECT RAISE(ABORT,'immutable'); END"
            )
            fault_injector("after_schema")
            connection.executemany(
                "INSERT INTO production_identity_sessions VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (row[0], _digest(row[0]), *row[1:])
                    for row in rows
                ],
            )
            fault_injector("after_copy")
            connection.execute("DROP TABLE production_identity_sessions_v1")
            if _catalog(connection) != _CATALOG:
                raise ProductionIdentityUnavailable()
            fault_injector("before_commit")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def __init__(
        self,
        path: str | Path,
        *,
        registry: SqliteProductionRegistryUsers,
        configured_org_id: str,
        provider_id: str,
        issuer: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        fault_injector: Callable[[str], None] = lambda _point: None,
        _identity_session_id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if not Path(path).is_file() or type(registry) is not SqliteProductionRegistryUsers:
            raise ProductionIdentityUnavailable()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        if _catalog(self._connection) != _CATALOG:
            raise ProductionIdentityUnavailable()
        self._registry = registry
        self._org = configured_org_id
        self._provider = provider_id
        self._issuer = issuer
        self._clock = clock
        self._fault = fault_injector
        self._identity_session_id_factory = _identity_session_id_factory

    def establish(
        self,
        proof: VerifiedEmailIdentityProof,
        *,
        expires_at: datetime | None = None,
    ) -> ProductionAuthenticatedIdentity:
        if (
            type(proof) is not VerifiedEmailIdentityProof
            or not proof.email_verified
            or proof.provider_id != self._provider
            or proof.issuer != self._issuer
        ):
            raise ProductionIdentityUnavailable()
        identity_session_id = self._identity_session_id_factory()
        if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", identity_session_id) is None:
            raise ProductionIdentityUnavailable()
        user = self._registry.user_by_global_email(proof.email)
        if user is None or user.org_id != self._org:
            raise ProductionIdentityUnavailable()
        expiry = expires_at or self._clock() + timedelta(hours=8)
        if expiry <= self._clock():
            raise ProductionIdentityUnavailable()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "INSERT INTO production_identity_sessions VALUES (?,?,?,?,?,?,?,?,?,1)",
                (
                    identity_session_id,
                    _digest(identity_session_id),
                    self._org,
                    user.user_id,
                    _digest(proof.email),
                    _fingerprint(user),
                    user.revision,
                    _digest(f"{proof.provider_id}\x00{proof.issuer}"),
                    expiry.isoformat(),
                ),
            )
            self._fault("before_commit")
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return self._envelope(identity_session_id, user, expiry)

    def resolve(self, identity_session_id: str) -> ProductionAuthenticatedIdentity:
        row = self._connection.execute(
            "SELECT * FROM production_identity_sessions WHERE identity_session_id=?",
            (identity_session_id,),
        ).fetchone()
        if row is None or int(row["active"]) != 1:
            raise ProductionIdentityUnavailable()
        if (
            row["org_id"] != self._org
            or row["provider_digest"] != _digest(f"{self._provider}\x00{self._issuer}")
        ):
            raise ProductionIdentityUnavailable()
        expiry = datetime.fromisoformat(str(row["expires_at"]))
        if expiry <= self._clock():
            raise ProductionIdentityUnavailable()
        users = self._registry.users(str(row["org_id"]))
        user = next((item for item in users if item.user_id == row["user_id"]), None)
        if (
            user is None
            or user.revision != int(row["registry_revision"])
            or _digest(user.email) != row["email_digest"]
            or _fingerprint(user) != row["registry_fingerprint"]
        ):
            raise ProductionIdentityUnavailable()
        return self._envelope(identity_session_id, user, expiry)

    def revoke(self, identity_session_id: str) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            updated = self._connection.execute(
                "UPDATE production_identity_sessions SET active=0 WHERE identity_session_id=?",
                (identity_session_id,),
            )
            if updated.rowcount != 1:
                raise ProductionIdentityUnavailable()
            self._fault("revoke_before_commit")
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM production_identity_sessions").fetchone()[0])

    def _envelope(
        self, session_id: str, user: ProductionRegistryUser, expiry: datetime
    ) -> ProductionAuthenticatedIdentity:
        return ProductionAuthenticatedIdentity(
            identity_session_id=session_id,
            principal=AuthenticatedPrincipal(
                org_id=user.org_id,
                subject_id=user.user_id,
                identity_provider=self._provider,
                identity_session_id=session_id,
            ),
            registry_revision=user.revision,
            expires_at=expiry,
        )


class ProductionPrincipalResolver:
    """Opaque cookie value를 durable current session으로만 해석하는 production seam."""

    def __init__(self, sessions: SqliteProductionIdentitySessions) -> None:
        if type(sessions) is not SqliteProductionIdentitySessions:
            raise ProductionIdentityUnavailable()
        self._sessions = sessions

    def resolve(self, identity_session_id: str) -> ProductionAuthenticatedIdentity:
        return self._sessions.resolve(identity_session_id)

    def sso_link_status(self, identity_session_id: str, user_id: str) -> str:
        identity = self.resolve(identity_session_id)
        return (
            "verified_email_match"
            if identity.principal.subject_id == user_id
            else "unlinked"
        )

    def establish(
        self, proof: VerifiedEmailIdentityProof
    ) -> ProductionAuthenticatedIdentity:
        return self._sessions.establish(proof)

    def revoke(self, identity_session_id: str) -> None:
        self._sessions.revoke(identity_session_id)


__all__ = [
    "ProductionAuthenticatedIdentity",
    "ProductionIdentityUnavailable",
    "ProductionPrincipalResolver",
    "SqliteProductionIdentitySessions",
    "VerifiedEmailIdentityProof",
]
