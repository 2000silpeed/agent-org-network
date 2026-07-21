"""R5.4a sealed secret-free scoped Credential read capability."""

from __future__ import annotations

import sqlite3
from threading import RLock
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias, cast, final

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
    SnapshotCentralAuthorizer,
)
from agent_org_network.sqlite_durable_credential_scope_bindings import CredentialScopeSnapshot
from agent_org_network.sqlite_durable_credential_scope_projections import (
    open_sqlite_durable_credential_scope_projections_for_scoped_read,
)

__all__ = (
    "CredentialReadList",
    "CredentialReadNotFoundOrDenied",
    "CredentialReadUnavailable",
    "CredentialReadView",
    "CredentialScopedRead",
    "CredentialScopedReadCapability",
    "create_credential_scoped_read",
    "create_credential_scoped_read_capability",
)


class CurrentCredentialReadSource(Protocol):
    """Current read proof; deliberately distinct from the issue scope source port."""

    def resolve_credential_read_scope(
        self, org_id: str, credential_id: str, agent_card_id: str
    ) -> CredentialScopeSnapshot | None: ...


@dataclass(frozen=True)
class CredentialReadView:
    credential_id: str
    org_id: str
    owner_subject_id: str
    role: str
    generation: int
    revision: int
    status: Literal["active", "revoked"]
    issued_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class CredentialReadNotFoundOrDenied:
    """Neutral result: callers cannot distinguish absence from denied access."""


@dataclass(frozen=True)
class CredentialReadUnavailable:
    """Integrity, catalog, source, or authorization drift closed the read boundary."""


@dataclass(frozen=True)
class CredentialReadList:
    credentials: tuple[CredentialReadView, ...]


CredentialGetResult: TypeAlias = (
    CredentialReadView | CredentialReadNotFoundOrDenied | CredentialReadUnavailable
)
CredentialListResult: TypeAlias = CredentialReadList | CredentialReadUnavailable

_CAPABILITY_FACTORY_SEAL: Final = object()
_CAPABILITY_SEAL: Final = object()
_OPERATIONS_SEAL: Final = object()


def _current_scope_matches(row: sqlite3.Row, source: CurrentCredentialReadSource) -> bool:
    try:
        current = source.resolve_credential_read_scope(
            row["org_id"], row["credential_id"], row["agent_card_id"]
        )
        return type(current) is CredentialScopeSnapshot and (
            current.org_id,
            current.credential_id,
            current.agent_card_id,
            current.owner_subject_id,
            current.credential_resource_fingerprint,
            current.card_resource_fingerprint,
            current.owner_resource_fingerprint,
            current.source_kind,
            current.source_instance_ref,
            current.scope_revision,
            current.snapshot_digest,
        ) == (
            row["org_id"],
            row["credential_id"],
            row["agent_card_id"],
            row["owner_subject_id"],
            row["credential_resource_fingerprint"],
            row["card_resource_fingerprint"],
            row["owner_resource_fingerprint"],
            row["source_kind"],
            row["source_instance_ref"],
            row["scope_revision"],
            row["snapshot_digest"],
        )
    except Exception:
        return False


def _view(row: sqlite3.Row) -> CredentialReadView | None:
    values = (
        row["credential_id"],
        row["org_id"],
        row["owner_subject_id"],
        row["role"],
        row["issued_at"],
    )
    if (
        not all(type(value) is str and value for value in values)
        or type(row["generation"]) is not int
        or row["generation"] < 1
        or type(row["revision"]) is not int
        or row["revision"] < 1
        or row["status"] not in ("active", "revoked")
        or row["revoked_at"] is not None
        and type(row["revoked_at"]) is not str
    ):
        return None
    return CredentialReadView(
        row["credential_id"],
        row["org_id"],
        row["owner_subject_id"],
        row["role"],
        row["generation"],
        row["revision"],
        row["status"],
        row["issued_at"],
        row["revoked_at"],
    )


_ROW_QUERY: Final = (
    "SELECT p.*,c.role,c.generation,c.revision,c.status,c.issued_at,c.revoked_at "
    "FROM durable_credential_scope_projections_v1 p JOIN durable_credentials c "
    "ON c.org_id=p.org_id AND c.credential_id=p.credential_id "
)


def _same_row(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    return tuple(left) == tuple(right)


@final
class CredentialScopedReadCapability:
    def __init__(self, read: "CredentialScopedRead", seal: object) -> None:
        if seal is not _CAPABILITY_FACTORY_SEAL or type(read) is not CredentialScopedRead:
            raise TypeError("R5.4a scoped read capability는 factory로만 조립합니다.")
        self._read = read
        self._seal = _CAPABILITY_SEAL
        self._claimed = False
        self._claim_lock = RLock()

    def _claim(self) -> "CredentialScopedRead":
        with self._claim_lock:
            if self._seal is not _CAPABILITY_SEAL or self._claimed:
                raise ValueError("R5.4a scoped read capability를 claim할 수 없습니다.")
            self._claimed = True
            return self._read


@final
class CredentialScopedRead:
    def __init__(
        self,
        principal: AuthenticatedPrincipal,
        authorizer: SnapshotCentralAuthorizer,
        reader_source: CurrentCredentialReadSource,
        seal: object,
    ) -> None:
        if seal is not _OPERATIONS_SEAL:
            raise TypeError("R5.4a scoped read는 factory로만 조립합니다.")
        self._principal = principal
        self._authorizer = authorizer
        self._reader_source = reader_source

    def _authorized(
        self, org_id: str, credential_id: str | None, owner_subject_id: str | None
    ) -> bool:
        resource = ResourceRef(
            org_id=org_id,
            kind="worker_credential",
            resource_id=credential_id,
            owner_subject_id=owner_subject_id,
        )
        try:
            grant = self._authorizer.authorize(self._principal, "worker_credential.read", resource)
            return (
                type(grant) is AuthorizationGrant
                and self._authorizer.verify(
                    grant, self._principal, "worker_credential.read", resource
                )
                is True
            )
        except Exception:
            return False

    def _open(self, path: str | Path) -> sqlite3.Connection | None:
        try:
            # R5.3 catalog/immutable issuance anchor validation stays mandatory.
            return open_sqlite_durable_credential_scope_projections_for_scoped_read(path)
        except Exception:
            return None

    @staticmethod
    def _unprojected_exists(connection: sqlite3.Connection, org_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM durable_credentials c LEFT JOIN durable_credential_scope_projections_v1 p "
                "ON p.org_id=c.org_id AND p.credential_id=c.credential_id "
                "WHERE c.org_id=? AND p.credential_id IS NULL LIMIT 1",
                (org_id,),
            ).fetchone()
            is not None
        )

    def _current_and_authorized(self, row: sqlite3.Row) -> bool:
        return _current_scope_matches(row, self._reader_source) and self._authorized(
            row["org_id"], row["credential_id"], row["owner_subject_id"]
        )

    def get(self, path: str | Path, org_id: str, credential_id: str) -> CredentialGetResult:
        connection = self._open(path)
        if connection is None:
            return CredentialReadUnavailable()
        try:
            row = connection.execute(
                _ROW_QUERY + "WHERE p.org_id=? AND p.credential_id=?", (org_id, credential_id)
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT 1 FROM durable_credentials WHERE org_id=? AND credential_id=?",
                    (org_id, credential_id),
                ).fetchone()
                return (
                    CredentialReadUnavailable()
                    if exists is not None
                    else CredentialReadNotFoundOrDenied()
                )
            # Complete every external proof callback before the final anchor
            # validation.  A callback may itself mutate the SQLite catalog.
            if not self._current_and_authorized(row) or not self._current_and_authorized(row):
                return CredentialReadUnavailable()
            # Source callbacks and authorization are external reads. Reopen the
            # canonical anchor after them and return only a stable, unchanged row.
            final_connection = self._open(path)
            if final_connection is None:
                return CredentialReadUnavailable()
            try:
                final_row = final_connection.execute(
                    _ROW_QUERY + "WHERE p.org_id=? AND p.credential_id=?", (org_id, credential_id)
                ).fetchone()
                if final_row is None or not _same_row(row, final_row):
                    return CredentialReadUnavailable()
                final_connection.execute("BEGIN")
                stable_row = final_connection.execute(
                    _ROW_QUERY + "WHERE p.org_id=? AND p.credential_id=?", (org_id, credential_id)
                ).fetchone()
                if stable_row is None or not _same_row(final_row, stable_row):
                    return CredentialReadUnavailable()
            finally:
                if final_connection.in_transaction:
                    final_connection.rollback()
                final_connection.close()
            result = _view(stable_row)
            return result if result is not None else CredentialReadUnavailable()
        except Exception:
            return CredentialReadUnavailable()
        finally:
            connection.close()

    def list(self, path: str | Path, org_id: str) -> CredentialListResult:
        connection = self._open(path)
        if connection is None:
            return CredentialReadUnavailable()
        try:
            # Org-level grant is checked before exposing even an empty result.
            if not self._authorized(org_id, None, None):
                return CredentialReadUnavailable()
            # A catalog row outside the immutable companion is not an invisible
            # legacy entry: it makes the organization read unavailable.
            if self._unprojected_exists(connection, org_id):
                return CredentialReadUnavailable()
            rows = connection.execute(
                _ROW_QUERY + "WHERE p.org_id=? ORDER BY p.credential_id", (org_id,)
            ).fetchall()
            for row in rows:
                if not self._current_and_authorized(row) or not self._current_and_authorized(row):
                    return CredentialReadUnavailable()
            final_connection = self._open(path)
            if final_connection is None:
                return CredentialReadUnavailable()
            try:
                if self._unprojected_exists(final_connection, org_id):
                    return CredentialReadUnavailable()
                final_rows = final_connection.execute(
                    _ROW_QUERY + "WHERE p.org_id=? ORDER BY p.credential_id", (org_id,)
                ).fetchall()
                if len(rows) != len(final_rows) or any(
                    not _same_row(before, after)
                    for before, after in zip(rows, final_rows, strict=True)
                ):
                    return CredentialReadUnavailable()
                final_connection.execute("BEGIN")
                stable_rows = final_connection.execute(
                    _ROW_QUERY + "WHERE p.org_id=? ORDER BY p.credential_id", (org_id,)
                ).fetchall()
                if len(final_rows) != len(stable_rows) or any(
                    not _same_row(before, after)
                    for before, after in zip(final_rows, stable_rows, strict=True)
                ):
                    return CredentialReadUnavailable()
            finally:
                if final_connection.in_transaction:
                    final_connection.rollback()
                final_connection.close()
            views = tuple(_view(row) for row in stable_rows)
            if any(view is None for view in views):
                return CredentialReadUnavailable()
            return CredentialReadList(cast(tuple[CredentialReadView, ...], views))
        except Exception:
            return CredentialReadUnavailable()
        finally:
            connection.close()


def create_credential_scoped_read_capability(
    *,
    server_principal: object,
    central_authorizer: object,
    reader_source: object,
) -> CredentialScopedReadCapability:
    if (
        type(server_principal) is not AuthenticatedPrincipal
        or type(central_authorizer) is not SnapshotCentralAuthorizer
        or not callable(getattr(reader_source, "resolve_credential_read_scope", None))
    ):
        raise TypeError(
            "R5.4a scoped read의 exact principal, authority, current reader source가 필요합니다."
        )
    return CredentialScopedReadCapability(
        CredentialScopedRead(
            server_principal,
            central_authorizer,
            cast(CurrentCredentialReadSource, reader_source),
            _OPERATIONS_SEAL,
        ),
        _CAPABILITY_FACTORY_SEAL,
    )


def create_credential_scoped_read(
    capability: CredentialScopedReadCapability,
) -> CredentialScopedRead:
    if type(capability) is not CredentialScopedReadCapability:
        raise TypeError("exact R5.4a scoped read capability가 필요합니다.")
    return capability._claim()  # pyright: ignore[reportPrivateUsage]
