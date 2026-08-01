"""Session-derived, transaction-current Registry registration foundation.

This private seam deliberately receives a *digest*, never a browser cookie or
OIDC claim.  It creates short-lived stores per request so the immutable Central
read-only Registry store cannot acquire a mutable registration capability.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import cast

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    ResourceRef,
    SnapshotCentralAuthorizer,
    load_authority_policy_yaml,
)
from agent_org_network.central_browser_auth import BrowserSession, BrowserSessionPrincipal
from agent_org_network.central_browser_auth_sqlite import validate_browser_auth_connection
from agent_org_network.agent_card import AgentCard
from agent_org_network.sqlite_production_agent_cards import (
    CurrentCardRegistrationAuthorization,
    ProductionAgentCardCommand,
    ProductionAgentCardDenied,
    ProductionAgentCardUnavailable,
    SqliteProductionAgentCards,
    validate_production_agent_card_connection,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    ProductionRegistryUserDenied,
    ProductionRegistryUser,
    ProductionRegistryUserUnavailable,
    SqliteProductionRegistryUsers,
    production_registry_user_fingerprint,
    validate_production_registry_user_connection,
)


_DIGEST = re.compile(r"[0-9a-f]{64}")


class SessionDerivedRegistryRegistrationUnavailable(RuntimeError):
    """The scoped admission dependencies cannot safely establish current state."""


class SessionDerivedRegistryRegistrationDenied(RuntimeError):
    """The durable browser session or current Authority denied admission."""


class SessionDerivedRegistryRegistrationUnauthenticated(RuntimeError):
    """The opaque browser session no longer exists or is not active."""


@dataclass(frozen=True, slots=True)
class SessionDerivedRegistryRegistrationApplication:
    """Request-owned User/Card stores sharing one safe browser principal."""

    users: SqliteProductionRegistryUsers
    cards: SqliteProductionAgentCards

    def close(self) -> None:
        self.users.close()
        self.cards.close()


class SessionDerivedRegistryRegistrationFactory:
    """Create per-request transaction-current authorizers from a session digest."""

    def __init__(
        self,
        *,
        database_path: Path,
        authority_snapshot_path: Path,
        org_id: str,
        provider_id: str,
        session_digest: str,
        clock: Callable[[], datetime],
    ) -> None:
        if (
            not database_path.is_absolute()
            or not authority_snapshot_path.is_absolute()
            or not org_id
            or not provider_id
            or type(session_digest) is not str
            or _DIGEST.fullmatch(session_digest) is None
            or not callable(clock)
        ):
            raise SessionDerivedRegistryRegistrationUnavailable()
        self._database_path = database_path
        self._authority_snapshot_path = authority_snapshot_path
        self._org_id = org_id
        self._provider_id = provider_id
        self._session_digest = session_digest
        self._clock = clock

    def create(self) -> SessionDerivedRegistryRegistrationApplication:
        try:
            user_authorizer = _SessionDerivedUserRegistrationAuthorizer(self)
            card_authorizer = _SessionDerivedCardRegistrationAuthorizer(self)
            users = SqliteProductionRegistryUsers(self._database_path, authorize=user_authorizer)
            try:
                cards = SqliteProductionAgentCards(self._database_path, authorize=card_authorizer)
            except Exception:
                users.close()
                raise
            return SessionDerivedRegistryRegistrationApplication(users=users, cards=cards)
        except SessionDerivedRegistryRegistrationUnavailable:
            raise
        except Exception as error:
            raise SessionDerivedRegistryRegistrationUnavailable() from error

    def current(
        self, *, action: str, command: object, transaction: sqlite3.Connection
    ) -> tuple[BrowserSessionPrincipal, AuthorizationGrant, ResourceRef]:
        """Reread all admission facts in the caller's BEGIN IMMEDIATE transaction."""
        try:
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise SessionDerivedRegistryRegistrationUnavailable()
            validate_browser_auth_connection(transaction)
            validate_production_registry_user_connection(transaction)
            if action == "card.register":
                validate_production_agent_card_connection(transaction)
            row = transaction.execute(
                "SELECT session_digest,registry_user_id,org_id,oidc_identity_binding_digest,csrf_digest,"
                "registry_fingerprint,registry_revision,established_at,expires_at,ended_at,terminal_reason "
                "FROM browser_sessions WHERE session_digest COLLATE BINARY=?",
                (self._session_digest,),
            ).fetchone()
            if row is None:
                raise SessionDerivedRegistryRegistrationUnauthenticated()
            session = BrowserSession(
                session_digest=str(row[0]), registry_user_id=str(row[1]), org_id=str(row[2]),
                oidc_identity_binding_digest=str(row[3]), csrf_digest=str(row[4]),
                registry_fingerprint=str(row[5]), registry_revision=int(row[6]),
                established_at=datetime.fromisoformat(str(row[7])), expires_at=datetime.fromisoformat(str(row[8])),
                ended_at=None if row[9] is None else datetime.fromisoformat(str(row[9])),
                terminal_reason=None if row[10] is None else str(row[10]),
            )
            if session.org_id != self._org_id or session.ended_at is not None or session.expires_at <= now:
                if session.ended_at is not None or session.expires_at <= now:
                    raise SessionDerivedRegistryRegistrationUnauthenticated()
                raise _AdmissionDenied()
            # Do not call the full aggregate companion validator here: precommit
            # deliberately runs after this UoW has inserted its prospective
            # User/Card but before its receipt/audit/outbox companions exist.
            # The exact canonical catalog plus this durable bound User row are
            # the admission facts needed at this point in the same transaction.
            user_row = transaction.execute(
                "SELECT org_id,user_id,email,manager_id,revision FROM production_registry_users "
                "WHERE org_id=? AND user_id=?",
                (session.org_id, session.registry_user_id),
            ).fetchone()
            if user_row is None:
                raise _AdmissionDenied()
            user = ProductionRegistryUser(
                org_id=str(user_row[0]), user_id=str(user_row[1]), email=str(user_row[2]),
                manager_id=None if user_row[3] is None else str(user_row[3]), revision=int(user_row[4]),
            )
            fingerprint = production_registry_user_fingerprint(
                user.org_id, user.user_id, user.email, user.manager_id, user.revision
            )
            if session.registry_revision != user.revision or session.registry_fingerprint != fingerprint:
                raise _AdmissionDenied()
            principal = BrowserSessionPrincipal.from_session(session)
            resource = _registration_resource(command, action)
            if resource.org_id != principal.org_id:
                raise _AdmissionDenied()
            result = SnapshotCentralAuthorizer(
                load_authority_policy_yaml(
                    self._authority_snapshot_path.read_text(encoding="utf-8"),
                    expected_org_id=self._org_id,
                )
            ).authorize(
                AuthenticatedPrincipal(
                    org_id=principal.org_id, subject_id=principal.registry_user_id,
                    identity_provider=self._provider_id, identity_session_id=principal.session_digest,
                ),
                action,
                resource,
            )
            if type(result) is AuthorizationGrant:
                return principal, result, resource
            if type(result) is AuthorizationDenied:
                raise _AdmissionDenied()
            raise SessionDerivedRegistryRegistrationUnavailable()
        except _AdmissionDenied:
            raise
        except SessionDerivedRegistryRegistrationUnauthenticated:
            raise
        except SessionDerivedRegistryRegistrationUnavailable:
            raise
        except (OSError, ValueError, sqlite3.Error, ProductionRegistryUserUnavailable) as error:
            raise SessionDerivedRegistryRegistrationUnavailable() from error

    def current_read(
        self, *, action: str, transaction: sqlite3.Connection
    ) -> BrowserSessionPrincipal:
        """Authorize a non-mutating Central admission projection.

        Read routes still derive their identity exclusively from the durable
        browser session and reload the Authority file on each request.  They
        intentionally use a fixed Registry resource rather than accepting a
        caller-selected User/Card identifier.
        """
        if action not in {"session.read", "user.register", "card.register"}:
            raise SessionDerivedRegistryRegistrationUnavailable()
        try:
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise SessionDerivedRegistryRegistrationUnavailable()
            validate_browser_auth_connection(transaction)
            validate_production_registry_user_connection(transaction)
            row = transaction.execute(
                "SELECT session_digest,registry_user_id,org_id,oidc_identity_binding_digest,csrf_digest,"
                "registry_fingerprint,registry_revision,established_at,expires_at,ended_at,terminal_reason "
                "FROM browser_sessions WHERE session_digest COLLATE BINARY=?",
                (self._session_digest,),
            ).fetchone()
            if row is None:
                raise SessionDerivedRegistryRegistrationUnauthenticated()
            session = BrowserSession(
                session_digest=str(row[0]), registry_user_id=str(row[1]), org_id=str(row[2]),
                oidc_identity_binding_digest=str(row[3]), csrf_digest=str(row[4]),
                registry_fingerprint=str(row[5]), registry_revision=int(row[6]),
                established_at=datetime.fromisoformat(str(row[7])), expires_at=datetime.fromisoformat(str(row[8])),
                ended_at=None if row[9] is None else datetime.fromisoformat(str(row[9])),
                terminal_reason=None if row[10] is None else str(row[10]),
            )
            if session.org_id != self._org_id or session.ended_at is not None or session.expires_at <= now:
                if session.ended_at is not None or session.expires_at <= now:
                    raise SessionDerivedRegistryRegistrationUnauthenticated()
                raise _AdmissionDenied()
            user_row = transaction.execute(
                "SELECT org_id,user_id,email,manager_id,revision FROM production_registry_users "
                "WHERE org_id=? AND user_id=?", (session.org_id, session.registry_user_id)
            ).fetchone()
            if user_row is None:
                raise _AdmissionDenied()
            user = ProductionRegistryUser(
                org_id=str(user_row[0]), user_id=str(user_row[1]), email=str(user_row[2]),
                manager_id=None if user_row[3] is None else str(user_row[3]), revision=int(user_row[4]),
            )
            if (
                session.registry_revision != user.revision
                or session.registry_fingerprint != production_registry_user_fingerprint(
                    user.org_id, user.user_id, user.email, user.manager_id, user.revision
                )
            ):
                raise _AdmissionDenied()
            principal = BrowserSessionPrincipal.from_session(session)
            result = SnapshotCentralAuthorizer(
                load_authority_policy_yaml(
                    self._authority_snapshot_path.read_text(encoding="utf-8"),
                    expected_org_id=self._org_id,
                )
            ).authorize(
                AuthenticatedPrincipal(
                    org_id=principal.org_id, subject_id=principal.registry_user_id,
                    identity_provider=self._provider_id, identity_session_id=principal.session_digest,
                ),
                action,
                ResourceRef(org_id=principal.org_id, kind="registry", resource_id=action),
            )
            if type(result) is not AuthorizationGrant:
                raise _AdmissionDenied()
            return principal
        except _AdmissionDenied:
            raise
        except SessionDerivedRegistryRegistrationUnauthenticated:
            raise
        except SessionDerivedRegistryRegistrationUnavailable:
            raise
        except (OSError, ValueError, sqlite3.Error, ProductionRegistryUserUnavailable) as error:
            raise SessionDerivedRegistryRegistrationUnavailable() from error

    def read_current(self, *, action: str) -> BrowserSessionPrincipal:
        """Run a read authorization in its own short-lived SQLite transaction."""
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(self._database_path))
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            principal = self.current_read(action=action, transaction=connection)
            connection.commit()
            return principal
        except _AdmissionDenied as error:
            if connection is not None:
                connection.rollback()
            raise SessionDerivedRegistryRegistrationDenied() from error
        except SessionDerivedRegistryRegistrationUnauthenticated:
            if connection is not None:
                connection.rollback()
            raise
        except SessionDerivedRegistryRegistrationUnavailable:
            if connection is not None:
                connection.rollback()
            raise
        except Exception as error:
            if connection is not None:
                connection.rollback()
            raise SessionDerivedRegistryRegistrationUnavailable() from error
        finally:
            if connection is not None:
                connection.close()

    def read_user_projection(
        self, *, action: str
    ) -> tuple[BrowserSessionPrincipal, int, tuple[ProductionRegistryUser, ...]]:
        """Authorize and read the safe Registry User projection atomically."""
        if action not in {"session.read", "user.register"}:
            raise SessionDerivedRegistryRegistrationUnavailable()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(self._database_path))
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            principal = self.current_read(action=action, transaction=connection)
            revision_row = connection.execute(
                "SELECT revision FROM production_registry_revisions WHERE org_id=?", (principal.org_id,)
            ).fetchone()
            if revision_row is None:
                raise SessionDerivedRegistryRegistrationUnavailable()
            users = tuple(
                ProductionRegistryUser.model_validate(dict(row))
                for row in connection.execute(
                    "SELECT org_id,user_id,email,manager_id,revision FROM production_registry_users "
                    "WHERE org_id=? ORDER BY user_id", (principal.org_id,)
                ).fetchall()
            )
            connection.commit()
            return principal, int(revision_row[0]), users
        except _AdmissionDenied as error:
            if connection is not None:
                connection.rollback()
            raise SessionDerivedRegistryRegistrationDenied() from error
        except SessionDerivedRegistryRegistrationUnauthenticated:
            if connection is not None:
                connection.rollback()
            raise
        except SessionDerivedRegistryRegistrationUnavailable:
            if connection is not None:
                connection.rollback()
            raise
        except Exception as error:
            if connection is not None:
                connection.rollback()
            raise SessionDerivedRegistryRegistrationUnavailable() from error
        finally:
            if connection is not None:
                connection.close()

    def read_card_projection(
        self, *, action: str
    ) -> tuple[BrowserSessionPrincipal, int, tuple[AgentCard, ...]]:
        """Authorize and read canonical Agent Cards in one current transaction."""
        if action not in {"card.register", "session.read"}:
            raise SessionDerivedRegistryRegistrationUnavailable()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(self._database_path))
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            principal = self.current_read(action=action, transaction=connection)
            validate_production_agent_card_connection(connection)
            revision_row = connection.execute(
                "SELECT revision FROM production_registry_revisions WHERE org_id=?", (principal.org_id,)
            ).fetchone()
            if revision_row is None:
                raise SessionDerivedRegistryRegistrationUnavailable()
            cards = tuple(
                SqliteProductionAgentCards._decode_card(row)  # pyright: ignore[reportPrivateUsage]
                for row in connection.execute(
                    "SELECT * FROM production_agent_cards WHERE org_id=? ORDER BY agent_id",
                    (principal.org_id,),
                ).fetchall()
            )
            connection.commit()
            return principal, int(revision_row[0]), cards
        except _AdmissionDenied as error:
            if connection is not None:
                connection.rollback()
            raise SessionDerivedRegistryRegistrationDenied() from error
        except SessionDerivedRegistryRegistrationUnauthenticated:
            if connection is not None:
                connection.rollback()
            raise
        except SessionDerivedRegistryRegistrationUnavailable:
            if connection is not None:
                connection.rollback()
            raise
        except Exception as error:
            if connection is not None:
                connection.rollback()
            raise SessionDerivedRegistryRegistrationUnavailable() from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def evidence_digest(
        principal: BrowserSessionPrincipal,
        grant: AuthorizationGrant,
        action: str,
        command: object,
        resource: ResourceRef,
    ) -> str:
        # Digest-only evidence intentionally excludes browser session identity:
        # a newly established session of the same actor can safely replay the
        # same command after current authorization succeeds.
        payload = json.dumps(
            {
                "subject_id": principal.registry_user_id,
                "org_id": principal.org_id,
                "action": action,
                "policy_digest": grant.policy_digest,
                "resource": resource.model_dump(mode="json"),
                "command": _canonical_command(command),
            },
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        return sha256(payload.encode()).hexdigest()


class _AdmissionDenied(Exception):
    pass


def _registration_resource(command: object, action: str) -> ResourceRef:
    if action == "user.register" and type(command) is ProductionRegistryUserCommand:
        return ResourceRef(org_id=command.org_id, kind="user", resource_id=command.user_id)
    if action == "card.register" and type(command) is ProductionAgentCardCommand:
        return ResourceRef(
            org_id=command.org_id,
            kind="agent_card",
            resource_id=command.card.agent_id,
            owner_subject_id=command.card.owner,
        )
    raise _AdmissionDenied()


def _canonical_command(command: object) -> dict[str, object]:
    if type(command) is ProductionRegistryUserCommand:
        return command.model_dump(mode="json")
    if type(command) is ProductionAgentCardCommand:
        value = cast(dict[str, object], command.model_dump(mode="json"))
        card = value.get("card")
        if type(card) is not dict:
            raise _AdmissionDenied()
        card = cast(dict[str, object], card)
        # last_reviewed_at is Central-clock material, not caller command meaning.
        card.pop("last_reviewed_at", None)
        return value
    raise _AdmissionDenied()


class _SessionDerivedUserRegistrationAuthorizer:
    def __init__(self, factory: SessionDerivedRegistryRegistrationFactory) -> None:
        self._factory = factory

    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection
    ) -> CurrentUserRegistrationAuthorization:
        try:
            principal, grant, resource = self._factory.current(
                action="user.register", command=command, transaction=transaction
            )
            if command.org_id != principal.org_id or command.principal_id != principal.registry_user_id:
                raise _AdmissionDenied()
            return CurrentUserRegistrationAuthorization(
                authority_epoch=0, policy_digest=grant.policy_digest,
                evidence_digest=self._factory.evidence_digest(
                    principal, grant, "user.register", command, resource
                ),
            )
        except _AdmissionDenied as error:
            raise ProductionRegistryUserDenied() from error
        except SessionDerivedRegistryRegistrationUnauthenticated:
            raise
        except Exception as error:
            raise ProductionRegistryUserUnavailable() from error

    def verify_precommit(
        self, command: ProductionRegistryUserCommand,
        evidence: CurrentUserRegistrationAuthorization, transaction: sqlite3.Connection,
    ) -> bool:
        try:
            current = self.current(command, transaction)
            return current == evidence
        except ProductionRegistryUserDenied:
            return False
        except SessionDerivedRegistryRegistrationUnauthenticated:
            raise
        except ProductionRegistryUserUnavailable:
            raise


class _SessionDerivedCardRegistrationAuthorizer:
    def __init__(self, factory: SessionDerivedRegistryRegistrationFactory) -> None:
        self._factory = factory

    def current(
        self, command: ProductionAgentCardCommand, transaction: sqlite3.Connection
    ) -> CurrentCardRegistrationAuthorization:
        try:
            principal, grant, resource = self._factory.current(
                action="card.register", command=command, transaction=transaction
            )
            if command.org_id != principal.org_id or command.principal_id != principal.registry_user_id:
                raise _AdmissionDenied()
            return CurrentCardRegistrationAuthorization(
                authority_epoch=0, policy_digest=grant.policy_digest,
                evidence_digest=self._factory.evidence_digest(
                    principal, grant, "card.register", command, resource
                ),
            )
        except _AdmissionDenied as error:
            raise ProductionAgentCardDenied() from error
        except SessionDerivedRegistryRegistrationUnauthenticated:
            raise
        except Exception as error:
            raise ProductionAgentCardUnavailable() from error

    def verify_precommit(
        self, command: ProductionAgentCardCommand,
        evidence: CurrentCardRegistrationAuthorization, transaction: sqlite3.Connection,
    ) -> bool:
        try:
            current = self.current(command, transaction)
            return current == evidence
        except ProductionAgentCardDenied:
            return False
        except SessionDerivedRegistryRegistrationUnauthenticated:
            raise
        except ProductionAgentCardUnavailable:
            raise


__all__ = [
    "SessionDerivedRegistryRegistrationApplication",
    "SessionDerivedRegistryRegistrationDenied",
    "SessionDerivedRegistryRegistrationUnauthenticated",
    "SessionDerivedRegistryRegistrationFactory",
    "SessionDerivedRegistryRegistrationUnavailable",
]
