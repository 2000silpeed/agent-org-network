"""Canonical SQLite persistence for digest-only Central browser auth values."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
import re
import sqlite3
from threading import RLock

from agent_org_network.central_browser_auth import BrowserOidcTransaction, BrowserSession
from agent_org_network.sqlite_production_registry_users import (
    ProductionRegistryUser,
    ProductionRegistryUserUnavailable,
    production_registry_user_fingerprint,
    validate_production_registry_user_connection,
    validate_production_registry_user_rows,
)


_TRANSACTIONS = """
CREATE TABLE browser_oidc_transactions (
    transaction_digest TEXT PRIMARY KEY NOT NULL,
    provider_digest TEXT NOT NULL,
    redirect_uri_digest TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    nonce_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    terminal_reason TEXT
);
"""
_SESSIONS = """
CREATE TABLE browser_sessions (
    session_digest TEXT PRIMARY KEY NOT NULL,
    registry_user_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    oidc_identity_binding_digest TEXT NOT NULL,
    csrf_digest TEXT NOT NULL,
    registry_fingerprint TEXT NOT NULL,
    registry_revision INTEGER NOT NULL,
    established_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ended_at TEXT,
    terminal_reason TEXT
);
"""
_TRANSACTION_INDEX = """
CREATE INDEX idx_browser_oidc_transactions_expiry
    ON browser_oidc_transactions(expires_at, transaction_digest);
"""
_SESSION_INDEX = """
CREATE INDEX idx_browser_sessions_registry
    ON browser_sessions(org_id, registry_user_id, session_digest);
"""
_DIGEST = re.compile(r"[0-9a-f]{64}")


class BrowserAuthSqliteUnavailable(RuntimeError):
    """The narrow browser-auth store is absent, malformed, or unavailable."""


BrowserAuthMigrationFaultInjector = Callable[[str], None]


class BrowserSessionEstablishmentOutcome(str, Enum):
    """Opaque, typed outcomes for the future callback application boundary."""

    ESTABLISHED = "established"
    INVALID = "invalid"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class BrowserSessionCurrentOutcome(str, Enum):
    """Result of a current browser-session/Registry readback."""

    ACTIVE = "active"
    UNAUTHENTICATED = "unauthenticated"
    UNAVAILABLE = "unavailable"


def migrate_browser_auth_schema(
    path: Path, *, fault_injector: BrowserAuthMigrationFaultInjector | None = None
) -> None:
    """Create the narrow schema exactly once; foreign/tampered catalog fails closed."""
    connection = sqlite3.connect(path)
    fault = fault_injector or _no_fault
    try:
        catalog = _catalog(connection)
        if catalog == _CANONICAL_CATALOG:
            return
        if catalog != _EMPTY_CATALOG:
            raise BrowserAuthSqliteUnavailable()
        connection.execute("BEGIN IMMEDIATE")
        try:
            fault("before-browser-auth-schema")
            connection.execute(_TRANSACTIONS)
            connection.execute(_SESSIONS)
            connection.execute(_TRANSACTION_INDEX)
            connection.execute(_SESSION_INDEX)
            fault("before-browser-auth-readback")
            _validate(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except BrowserAuthSqliteUnavailable:
        raise
    except Exception as error:
        raise BrowserAuthSqliteUnavailable() from error
    finally:
        connection.close()


def browser_auth_schema_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        validate_browser_auth_connection(connection)
        return True
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


class CentralBrowserAuthSqliteStore:
    """Digest-only store with the callback's atomic establishment unit of work."""

    def __init__(self, path: Path) -> None:
        if not browser_auth_schema_ready(path):
            raise BrowserAuthSqliteUnavailable()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._lock = RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_transaction(self, transaction: BrowserOidcTransaction) -> None:
        if type(transaction) is not BrowserOidcTransaction:
            raise BrowserAuthSqliteUnavailable()
        try:
            with self._lock:
                _validate(self._connection)
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO browser_oidc_transactions("
                        "transaction_digest,provider_digest,redirect_uri_digest,state_digest,"
                        "nonce_digest,created_at,expires_at,consumed_at,terminal_reason) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        _transaction_values(transaction),
                    )
        except Exception as error:
            raise BrowserAuthSqliteUnavailable() from error

    def get_transaction(self, transaction_digest: str) -> BrowserOidcTransaction | None:
        try:
            with self._lock:
                _validate(self._connection)
                row = self._connection.execute(
                    "SELECT transaction_digest,provider_digest,redirect_uri_digest,state_digest,"
                    "nonce_digest,created_at,expires_at,consumed_at,terminal_reason "
                    "FROM browser_oidc_transactions WHERE transaction_digest COLLATE BINARY=?",
                    (transaction_digest,),
                ).fetchone()
                return None if row is None else _transaction_from_row(row)
        except Exception as error:
            raise BrowserAuthSqliteUnavailable() from error

    def mark_transaction_consumed(
        self, transaction_digest: str, terminal_reason: str, consumed_at: datetime
    ) -> bool:
        """A one-way terminal update; the callback application will invoke it atomically."""
        try:
            # Validate without accepting a raw handle at this boundary.
            BrowserOidcTransaction(
                transaction_digest=transaction_digest,
                provider_digest="0" * 64,
                redirect_uri_digest="0" * 64,
                state_digest="0" * 64,
                nonce_digest="0" * 64,
                created_at=consumed_at,
                expires_at=consumed_at + timedelta(days=1),
                consumed_at=consumed_at,
                terminal_reason=terminal_reason,
            )
            with self._lock:
                _validate(self._connection)
                with self._connection:
                    result = self._connection.execute(
                        "UPDATE browser_oidc_transactions SET consumed_at=?, terminal_reason=? "
                        "WHERE transaction_digest COLLATE BINARY=? AND consumed_at IS NULL "
                        "AND terminal_reason IS NULL",
                        (consumed_at.isoformat(), terminal_reason, transaction_digest),
                    )
                return result.rowcount == 1
        except Exception as error:
            raise BrowserAuthSqliteUnavailable() from error

    def get_session(self, session_digest: str) -> BrowserSession | None:
        try:
            with self._lock:
                _validate(self._connection)
                row = self._connection.execute(
                    "SELECT session_digest,registry_user_id,org_id,oidc_identity_binding_digest,csrf_digest,"
                    "registry_fingerprint,registry_revision,established_at,expires_at,ended_at,terminal_reason "
                    "FROM browser_sessions WHERE session_digest COLLATE BINARY=?",
                    (session_digest,),
                ).fetchone()
                return None if row is None else _session_from_row(row)
        except Exception as error:
            raise BrowserAuthSqliteUnavailable() from error

    def read_current_session(
        self, session_digest: str, *, now: datetime
    ) -> tuple[BrowserSessionCurrentOutcome, BrowserSession | None]:
        """Read one active session and its current Registry User binding.

        This runs through the same canonical SQLite connection under the store
        lock, so a session does not become current merely because an older
        cross-connection Registry User object happened to be cached.
        """
        try:
            with self._lock:
                session = read_current_browser_session_connection(
                    self._connection, session_digest, now=now
                )
                return (
                    (BrowserSessionCurrentOutcome.UNAUTHENTICATED, None)
                    if session is None
                    else (BrowserSessionCurrentOutcome.ACTIVE, session)
                )
        except Exception:
            return BrowserSessionCurrentOutcome.UNAVAILABLE, None

    def end_session(self, session_digest: str, terminal_reason: str, ended_at: datetime) -> bool:
        try:
            BrowserSession(
                session_digest=session_digest,
                registry_user_id="validation",
                org_id="validation",
                oidc_identity_binding_digest="0" * 64,
                csrf_digest="0" * 64,
                registry_fingerprint="0" * 64,
                registry_revision=0,
                established_at=ended_at,
                expires_at=ended_at + timedelta(days=1),
                ended_at=ended_at,
                terminal_reason=terminal_reason,
            )
            with self._lock:
                _validate(self._connection)
                with self._connection:
                    row = self._connection.execute(
                        "SELECT session_digest,registry_user_id,org_id,oidc_identity_binding_digest,csrf_digest,"
                        "registry_fingerprint,registry_revision,established_at,expires_at,ended_at,terminal_reason "
                        "FROM browser_sessions WHERE session_digest COLLATE BINARY=?",
                        (session_digest,),
                    ).fetchone()
                    if row is None:
                        return False
                    current = _session_from_row(row)
                    if current.ended_at is not None:
                        return False
                    if ended_at < current.established_at:
                        raise BrowserAuthSqliteUnavailable()
                    result = self._connection.execute(
                        "UPDATE browser_sessions SET ended_at=?, terminal_reason=? "
                        "WHERE session_digest COLLATE BINARY=? AND ended_at IS NULL "
                        "AND terminal_reason IS NULL",
                        (ended_at.isoformat(), terminal_reason, session_digest),
                    )
                return result.rowcount == 1
        except BrowserAuthSqliteUnavailable:
            raise
        except Exception as error:
            raise BrowserAuthSqliteUnavailable() from error

    def establish_session(
        self,
        transaction_digest: str,
        session: BrowserSession,
        *,
        now: datetime,
        precommit_authorize: Callable[[sqlite3.Connection], bool],
    ) -> BrowserSessionEstablishmentOutcome:
        """Atomically establish one session and consume one valid transaction.

        The callback application supplies the final current-Authority read.  It
        runs after all durable transaction/Registry read-backs and immediately
        before the two mutations, preventing a split consume/session outcome.
        """
        if type(session) is not BrowserSession or not callable(precommit_authorize):
            return BrowserSessionEstablishmentOutcome.UNAVAILABLE
        try:
            with self._lock:
                _validate_time_for_store(now)
                if type(transaction_digest) is not str or _DIGEST.fullmatch(transaction_digest) is None:
                    return BrowserSessionEstablishmentOutcome.INVALID
                _validate(self._connection)
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    row = self._connection.execute(
                        "SELECT transaction_digest,provider_digest,redirect_uri_digest,state_digest,"
                        "nonce_digest,created_at,expires_at,consumed_at,terminal_reason "
                        "FROM browser_oidc_transactions WHERE transaction_digest COLLATE BINARY=?",
                        (transaction_digest,),
                    ).fetchone()
                    if row is None:
                        self._connection.rollback()
                        return BrowserSessionEstablishmentOutcome.INVALID
                    transaction = _transaction_from_row(row)
                    if transaction.consumed_at is not None or transaction.expires_at <= now:
                        self._connection.rollback()
                        return BrowserSessionEstablishmentOutcome.INVALID
                    try:
                        validate_production_registry_user_connection(self._connection)
                        registered = self._connection.execute(
                            "SELECT 1 FROM production_registry_users "
                            "WHERE org_id=? AND user_id=?",
                            (session.org_id, session.registry_user_id),
                        ).fetchone()
                        if registered is None:
                            self._connection.rollback()
                            return BrowserSessionEstablishmentOutcome.DENIED
                        registry_user = validate_production_registry_user_rows(
                            self._connection, session.org_id, session.registry_user_id
                        )
                    except ProductionRegistryUserUnavailable:
                        self._connection.rollback()
                        return BrowserSessionEstablishmentOutcome.UNAVAILABLE
                    fingerprint = production_registry_user_fingerprint(
                        registry_user.org_id,
                        registry_user.user_id,
                        registry_user.email,
                        registry_user.manager_id,
                        registry_user.revision,
                    )
                    if (
                        registry_user.revision != session.registry_revision
                        or fingerprint != session.registry_fingerprint
                    ):
                        self._connection.rollback()
                        return BrowserSessionEstablishmentOutcome.DENIED
                    try:
                        allowed = precommit_authorize(self._connection)
                    except Exception:
                        self._connection.rollback()
                        return BrowserSessionEstablishmentOutcome.UNAVAILABLE
                    if allowed is not True:
                        self._connection.rollback()
                        return BrowserSessionEstablishmentOutcome.DENIED
                    self._connection.execute(
                        "INSERT INTO browser_sessions("
                        "session_digest,registry_user_id,org_id,oidc_identity_binding_digest,csrf_digest,"
                        "registry_fingerprint,registry_revision,established_at,expires_at,ended_at,terminal_reason) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        _session_values(session),
                    )
                    result = self._connection.execute(
                        "UPDATE browser_oidc_transactions SET consumed_at=?, terminal_reason=? "
                        "WHERE transaction_digest COLLATE BINARY=? AND consumed_at IS NULL "
                        "AND terminal_reason IS NULL",
                        (now.isoformat(), "completed", transaction_digest),
                    )
                    if result.rowcount != 1:
                        self._connection.rollback()
                        return BrowserSessionEstablishmentOutcome.INVALID
                    self._connection.commit()
                    return BrowserSessionEstablishmentOutcome.ESTABLISHED
                except sqlite3.IntegrityError:
                    self._connection.rollback()
                    return BrowserSessionEstablishmentOutcome.INVALID
                except Exception:
                    self._connection.rollback()
                    return BrowserSessionEstablishmentOutcome.UNAVAILABLE
        except Exception:
            return BrowserSessionEstablishmentOutcome.UNAVAILABLE


def _transaction_values(transaction: BrowserOidcTransaction) -> tuple[object, ...]:
    return (
        transaction.transaction_digest,
        transaction.provider_digest,
        transaction.redirect_uri_digest,
        transaction.state_digest,
        transaction.nonce_digest,
        transaction.created_at.isoformat(),
        transaction.expires_at.isoformat(),
        None if transaction.consumed_at is None else transaction.consumed_at.isoformat(),
        transaction.terminal_reason,
    )


def _session_values(session: BrowserSession) -> tuple[object, ...]:
    return (
        session.session_digest,
        session.registry_user_id,
        session.org_id,
        session.oidc_identity_binding_digest,
        session.csrf_digest,
        session.registry_fingerprint,
        session.registry_revision,
        session.established_at.isoformat(),
        session.expires_at.isoformat(),
        None if session.ended_at is None else session.ended_at.isoformat(),
        session.terminal_reason,
    )


def _transaction_from_row(row: sqlite3.Row) -> BrowserOidcTransaction:
    return BrowserOidcTransaction(
        transaction_digest=str(row["transaction_digest"]),
        provider_digest=str(row["provider_digest"]),
        redirect_uri_digest=str(row["redirect_uri_digest"]),
        state_digest=str(row["state_digest"]),
        nonce_digest=str(row["nonce_digest"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        consumed_at=(
            None if row["consumed_at"] is None else datetime.fromisoformat(str(row["consumed_at"]))
        ),
        terminal_reason=None if row["terminal_reason"] is None else str(row["terminal_reason"]),
    )


def _session_from_row(row: sqlite3.Row) -> BrowserSession:
    return BrowserSession(
        session_digest=str(row["session_digest"]),
        registry_user_id=str(row["registry_user_id"]),
        org_id=str(row["org_id"]),
        oidc_identity_binding_digest=str(row["oidc_identity_binding_digest"]),
        csrf_digest=str(row["csrf_digest"]),
        registry_fingerprint=str(row["registry_fingerprint"]),
        registry_revision=int(row["registry_revision"]),
        established_at=datetime.fromisoformat(str(row["established_at"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        ended_at=None if row["ended_at"] is None else datetime.fromisoformat(str(row["ended_at"])),
        terminal_reason=None if row["terminal_reason"] is None else str(row["terminal_reason"]),
    )


def _current_registry_user(
    connection: sqlite3.Connection, org_id: str, registry_user_id: str
) -> ProductionRegistryUser:
    """Read the Registry User only after canonical catalog and row validation."""
    validate_production_registry_user_connection(connection)
    registered = connection.execute(
        "SELECT 1 FROM production_registry_users WHERE org_id=? AND user_id=?",
        (org_id, registry_user_id),
    ).fetchone()
    if registered is None:
        raise ProductionRegistryUserUnavailable()
    return validate_production_registry_user_rows(connection, org_id, registry_user_id)


def read_current_browser_session_connection(
    connection: sqlite3.Connection, session_digest: str, *, now: datetime
) -> BrowserSession | None:
    """Canonical same-connection browser-session + Registry User currentness proof."""
    _validate_time_for_store(now)
    if type(session_digest) is not str or _DIGEST.fullmatch(session_digest) is None:
        return None
    _validate(connection)
    row = connection.execute(
        "SELECT session_digest,registry_user_id,org_id,oidc_identity_binding_digest,csrf_digest,"
        "registry_fingerprint,registry_revision,established_at,expires_at,ended_at,terminal_reason "
        "FROM browser_sessions WHERE session_digest COLLATE BINARY=?",
        (session_digest,),
    ).fetchone()
    if row is None:
        return None
    session = _session_from_row(row)
    if session.ended_at is not None or session.expires_at <= now:
        return None
    registry_user = _current_registry_user(connection, session.org_id, session.registry_user_id)
    fingerprint = production_registry_user_fingerprint(
        registry_user.org_id, registry_user.user_id, registry_user.email,
        registry_user.manager_id, registry_user.revision,
    )
    if (
        registry_user.revision != session.registry_revision
        or fingerprint != session.registry_fingerprint
    ):
        return None
    return session


def _validate_time_for_store(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")


def _no_fault(_point: str) -> None:
    return None


def _validate(connection: sqlite3.Connection) -> None:
    validate_browser_auth_connection(connection)


def validate_browser_auth_connection(connection: sqlite3.Connection) -> None:
    """Validate the exact browser auth catalog on an already-open transaction."""
    if _catalog(connection) != _CANONICAL_CATALOG:
        raise BrowserAuthSqliteUnavailable()


def _normalized_sql(value: object) -> str:
    return " ".join(str(value or "").split())


def _catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    names = ("browser_oidc_transactions", "browser_sessions")
    placeholders = ",".join("?" for _name in names)
    objects = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            f"WHERE name IN ({placeholders}) OR tbl_name IN ({placeholders}) ORDER BY type,name",
            names + names,
        )
    )
    normalized_objects = tuple((a, b, c, _normalized_sql(d)) for a, b, c, d in objects)
    infos = tuple(
        (name, tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info({name})")))
        for name in names
    )
    foreign_keys = tuple(
        (name, tuple(tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({name})")))
        for name in names
    )
    indexes = tuple(
        (name, tuple(tuple(row) for row in connection.execute(f"PRAGMA index_list({name})")))
        for name in names
    )
    return normalized_objects, infos, foreign_keys, indexes


def _canonical_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_TRANSACTIONS)
        connection.execute(_SESSIONS)
        connection.execute(_TRANSACTION_INDEX)
        connection.execute(_SESSION_INDEX)
        return _catalog(connection)
    finally:
        connection.close()


def _empty_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        return _catalog(connection)
    finally:
        connection.close()


_EMPTY_CATALOG = _empty_catalog()
_CANONICAL_CATALOG = _canonical_catalog()


__all__ = [
    "BrowserAuthSqliteUnavailable",
    "BrowserAuthMigrationFaultInjector",
    "BrowserSessionEstablishmentOutcome",
    "BrowserSessionCurrentOutcome",
    "CentralBrowserAuthSqliteStore",
    "browser_auth_schema_ready",
    "migrate_browser_auth_schema",
    "read_current_browser_session_connection",
    "validate_browser_auth_connection",
]
