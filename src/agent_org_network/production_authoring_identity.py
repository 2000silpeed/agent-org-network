"""Same-transaction O1 identity verification for production authoring."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
import hmac
import json
import re
import sqlite3

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from agent_org_network.sqlite_production_registry_users import (
    ProductionRegistryUserUnavailable,
    validate_production_registry_user_connection,
    validate_production_registry_user_rows,
)
from agent_org_network.sqlite_production_agent_cards import (
    ProductionAgentCardUnavailable,
    validate_production_agent_card_rows,
)


class ProductionAuthoringIdentityUnavailable(Exception):
    pass


class AuthoringIdentitySessionRef(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: SecretStr

    @field_validator("value")
    @classmethod
    def _opaque(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value.get_secret_value()) is None:
            raise ValueError("opaque O1 session required")
        return value


class AuthoringInvocation(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    session: AuthoringIdentitySessionRef
    org_id: str
    principal_id: str
    identity_provider: str

    @field_validator("org_id", "principal_id", "identity_provider")
    @classmethod
    def _bounded(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
            raise ValueError("bounded invocation identity required")
        return value


class CurrentIdentitySessionEvidence(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    identity_session_digest: str
    identity_evidence_digest: str
    registry_revision: int

    @field_validator("identity_session_digest", "identity_evidence_digest")
    @classmethod
    def _sha(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("lowercase sha256 required")
        return value


_IDENTITY_SCHEMA = """
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


def _identity_catalog(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,replace(replace(trim(sql),char(10),' '),'  ',' ') "
            "FROM sqlite_master WHERE name LIKE 'production_identity_%' "
            "OR tbl_name='production_identity_sessions' ORDER BY type,name"
        )
    )


def _canonical_identity_catalog() -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_IDENTITY_SCHEMA)
        return _identity_catalog(connection)
    finally:
        connection.close()


_IDENTITY_CATALOG = _canonical_identity_catalog()


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ProductionAuthoringIdentityVerifier:
    def __init__(
        self,
        *,
        provider_id: str,
        issuer: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not provider_id or not issuer:
            raise ProductionAuthoringIdentityUnavailable()
        self._provider_digest = _digest(f"{provider_id}\x00{issuer}")
        self._provider_id = provider_id
        self._clock = clock

    def current(
        self,
        invocation: AuthoringInvocation,
        transaction: sqlite3.Connection,
    ) -> CurrentIdentitySessionEvidence:
        if type(invocation) is not AuthoringInvocation:
            raise ProductionAuthoringIdentityUnavailable()
        before_changes = transaction.total_changes
        try:
            if _identity_catalog(transaction) != _IDENTITY_CATALOG:
                raise ProductionAuthoringIdentityUnavailable()
            validate_production_registry_user_connection(transaction)
            raw_session = invocation.session.value.get_secret_value()
            row = transaction.execute(
                "SELECT identity_session_id,identity_session_digest,org_id,user_id,"
                "email_digest,registry_fingerprint,registry_revision,provider_digest,"
                "expires_at,active "
                "FROM production_identity_sessions WHERE identity_session_digest=?",
                (_digest(raw_session),),
            ).fetchone()
            if row is None:
                raise ProductionAuthoringIdentityUnavailable()
            user = validate_production_registry_user_rows(
                transaction, invocation.org_id, invocation.principal_id
            )
            validate_production_agent_card_rows(transaction, invocation.org_id)
            expiry = datetime.fromisoformat(str(row[8]))
            revision = user.revision
            fingerprint = _digest(
                f"{invocation.org_id}\x00{invocation.principal_id}\x00{user.email}"
                f"\x00{user.manager_id or ''}\x00{revision}"
            )
            if (
                not hmac.compare_digest(str(row[0]), raw_session)
                or row[1] != _digest(raw_session)
                or row[2] != invocation.org_id
                or row[3] != invocation.principal_id
                or invocation.identity_provider != self._provider_id
                or int(row[9]) != 1
                or row[7] != self._provider_digest
                or expiry <= self._clock()
                or int(row[6]) != revision
                or row[4] != _digest(user.email)
                or row[5] != fingerprint
            ):
                raise ProductionAuthoringIdentityUnavailable()
            session_digest = _digest(raw_session)
            evidence_digest = _digest(
                _canonical(
                    {
                        "identity_session_digest": session_digest,
                        "org_id": row[2],
                        "principal_id": row[3],
                        "provider_digest": row[7],
                        "registry_revision": revision,
                        "email_digest": row[4],
                        "registry_fingerprint": row[5],
                        "expires_at": row[8],
                    }
                )
            )
            return CurrentIdentitySessionEvidence(
                identity_session_digest=session_digest,
                identity_evidence_digest=evidence_digest,
                registry_revision=revision,
            )
        except ProductionAuthoringIdentityUnavailable:
            raise
        except (
            ValueError,
            TypeError,
            sqlite3.Error,
            ProductionRegistryUserUnavailable,
            ProductionAgentCardUnavailable,
        ) as error:
            raise ProductionAuthoringIdentityUnavailable() from error
        finally:
            if transaction.total_changes != before_changes:
                raise ProductionAuthoringIdentityUnavailable()


__all__ = [
    "AuthoringIdentitySessionRef",
    "AuthoringInvocation",
    "CurrentIdentitySessionEvidence",
    "ProductionAuthoringIdentityUnavailable",
    "ProductionAuthoringIdentityVerifier",
]
