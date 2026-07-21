"""R5.4b sealed, scoped Credential revoke operation.

This module deliberately owns a small companion ledger.  It never exposes the
legacy registry revoke method and it does not persist approval material.
"""

from __future__ import annotations

import sqlite3
from threading import RLock
import hashlib
import json
import re
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Protocol, cast, final

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
    SnapshotCentralAuthorizer,
)
from agent_org_network.credential_issue_materialization_verifier import (
    CurrentCredentialApprovalEvidenceResolver,
)
from agent_org_network.credential_scoped_read import (
    CredentialReadNotFoundOrDenied,
    CredentialReadUnavailable,
    CredentialReadView,
    CurrentCredentialReadSource,
)
from agent_org_network.credential_scoped_read import _current_scope_matches  # pyright: ignore[reportPrivateUsage]
from agent_org_network.credential_scoped_read import _view  # pyright: ignore[reportPrivateUsage]
from agent_org_network.durable_credentials import (
    CredentialApprovalEvidence,
    canonical_credential_command_digest,
    resource_fingerprint,
)
from agent_org_network.sqlite_durable_credential_scope_projections import (
    open_sqlite_durable_credential_scope_projections_for_scoped_read,
)

__all__ = (
    "CredentialRevokeConflict",
    "CredentialRevokeCommand",
    "CredentialRevokeResult",
    "CredentialScopedRevoke",
    "CredentialScopedRevokeCapability",
    "create_credential_scoped_revoke",
    "create_credential_scoped_revoke_capability",
)

_FACTORY: Final = object()
_SEAL: Final = object()
_OPS: Final = object()
_RECEIPTS: Final = "durable_credential_revoke_receipts_v1"
_AUDIT: Final = "durable_credential_revoke_audit_v1"
_OUTBOX: Final = "durable_credential_revoke_outbox_v1"
_COMPONENT: Final = "durable_credential_scoped_revoke_v1"
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FORBIDDEN: Final = (
    "secret",
    "grant",
    "evidence",
    "source",
    "digest",
    "hash",
    "delivery",
    "token",
    "password",
)


class CredentialRevokeApprovalProvider(Protocol):
    def acquire_revoke_approval(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        resource: ResourceRef,
        command_digest: str,
    ) -> CredentialApprovalEvidence: ...


@dataclass(frozen=True)
class CredentialRevokeResult:
    credential: CredentialReadView


@dataclass(frozen=True)
class CredentialRevokeCommand:
    command_id: str
    attempt: int
    expected_generation: int
    expected_revision: int


@dataclass(frozen=True)
class CredentialRevokeConflict:
    """A supplied receipt has different semantics, or no receipt covers a closed row."""


def _opaque_sql(name: str) -> str:
    return " AND ".join(
        (
            f"length({name}) BETWEEN 1 AND 128",
            f"{name} GLOB '[A-Za-z0-9]*'",
            f"{name} NOT GLOB '*[^A-Za-z0-9._:-]*'",
            *(f"instr(lower({name}),'{word}')=0" for word in _FORBIDDEN),
        )
    )


_TIME_SQL: Final = "length({field})=24 AND {field} GLOB '????-??-??T??:??:??.???Z' AND COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',{field})={field},0)"
_DDL: Final = (
    f"CREATE TABLE {_RECEIPTS}(contract_version INTEGER NOT NULL CHECK(contract_version=1),org_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('org_id')}),command_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('command_id')}),attempt INTEGER NOT NULL CHECK(attempt>=1),credential_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('credential_id')}),principal_subject_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('principal_subject_id')}),identity_provider TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('identity_provider')}),identity_session_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('identity_session_id')}),action TEXT NOT NULL CHECK(action='worker_credential.revoke'),expected_generation INTEGER NOT NULL CHECK(expected_generation>=1),expected_revision INTEGER NOT NULL CHECK(expected_revision>=1),result_revision INTEGER NOT NULL CHECK(result_revision=expected_revision+1),revoked_at TEXT NOT NULL CHECK({_TIME_SQL.format(field='revoked_at')}),PRIMARY KEY(org_id,command_id,attempt),UNIQUE(org_id,command_id,attempt,credential_id),FOREIGN KEY(org_id,credential_id) REFERENCES durable_credentials(org_id,credential_id) ON UPDATE RESTRICT ON DELETE RESTRICT);"
    f"CREATE TABLE {_AUDIT}(org_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('org_id')}),command_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('command_id')}),attempt INTEGER NOT NULL CHECK(attempt>=1),credential_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('credential_id')}),event TEXT NOT NULL CHECK(event='credential_revoked'),occurred_at TEXT NOT NULL CHECK({_TIME_SQL.format(field='occurred_at')}),PRIMARY KEY(org_id,command_id,attempt),FOREIGN KEY(org_id,command_id,attempt,credential_id) REFERENCES {_RECEIPTS}(org_id,command_id,attempt,credential_id) ON UPDATE RESTRICT ON DELETE RESTRICT);"
    f"CREATE TABLE {_OUTBOX}(org_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('org_id')}),command_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('command_id')}),attempt INTEGER NOT NULL CHECK(attempt>=1),credential_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql('credential_id')}),event TEXT NOT NULL CHECK(event='credential_revoked'),occurred_at TEXT NOT NULL CHECK({_TIME_SQL.format(field='occurred_at')}),PRIMARY KEY(org_id,command_id,attempt),FOREIGN KEY(org_id,command_id,attempt,credential_id) REFERENCES {_RECEIPTS}(org_id,command_id,attempt,credential_id) ON UPDATE RESTRICT ON DELETE RESTRICT);"
)
_TRIGGERS: Final = tuple(
    f"CREATE TRIGGER {table}_no_{verb} BEFORE {verb.upper()} ON {table} BEGIN SELECT RAISE(ABORT,'immutable revoke companion'); END"
    for table in (_RECEIPTS, _AUDIT, _OUTBOX)
    for verb in ("update", "delete")
)


def _canon(value: str) -> str:
    return " ".join(value.split()).replace("( ", "(").replace(" )", ")")


def _manifest() -> str:
    return json.dumps(
        {"component_id": _COMPONENT, "schema_version": 1, "ddl": _DDL, "triggers": _TRIGGERS},
        sort_keys=True,
        separators=(",", ":"),
    )


def _catalog_sql(connection: sqlite3.Connection, kind: str, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind, name)
    ).fetchone()
    return None if row is None else cast(str, row[0])


def _valid_value(value: object) -> bool:
    return (
        type(value) is str
        and _OPAQUE.fullmatch(value) is not None
        and all(word not in value.lower() for word in _FORBIDDEN)
    )


def _valid_timestamp(value: object) -> bool:
    if type(value) is not str or len(value) != 24:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True


def _server_timestamp(clock: Callable[[], datetime]) -> str | None:
    try:
        now = clock()
    except Exception:
        return None
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        return None
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _validate_companion(connection: sqlite3.Connection) -> None:
    manifest = _manifest()
    marker = connection.execute(
        "SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id=?",
        (_COMPONENT,),
    ).fetchone()
    expected_triggers = {
        f"{table}_no_{verb}"
        for table in (_RECEIPTS, _AUDIT, _OUTBOX)
        for verb in ("update", "delete")
    }
    actual = {
        (row[0], row[1])
        for table in (_RECEIPTS, _AUDIT, _OUTBOX)
        for row in connection.execute(
            "SELECT type,name FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
            (table,),
        )
    }
    if (
        marker is None
        or tuple(marker) != (1, manifest, hashlib.sha256(manifest.encode()).hexdigest())
        or any(
            _canon(_catalog_sql(connection, "table", table) or "") != _canon(sql)
            for table, sql in zip((_RECEIPTS, _AUDIT, _OUTBOX), _DDL.split(";")[:3], strict=True)
        )
        or actual != {("trigger", name) for name in expected_triggers}
        or any(
            _canon(_catalog_sql(connection, "trigger", name) or "") != _canon(sql)
            for name, sql in zip(sorted(expected_triggers), sorted(_TRIGGERS), strict=True)
        )
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise RuntimeError("revoke companion unavailable")
    expected_fks = {
        _RECEIPTS: {
            ("durable_credentials", "org_id", "org_id"),
            ("durable_credentials", "credential_id", "credential_id"),
        },
        _AUDIT: {
            (_RECEIPTS, "org_id", "org_id"),
            (_RECEIPTS, "command_id", "command_id"),
            (_RECEIPTS, "attempt", "attempt"),
            (_RECEIPTS, "credential_id", "credential_id"),
        },
        _OUTBOX: {
            (_RECEIPTS, "org_id", "org_id"),
            (_RECEIPTS, "command_id", "command_id"),
            (_RECEIPTS, "attempt", "attempt"),
            (_RECEIPTS, "credential_id", "credential_id"),
        },
    }
    for table in (_RECEIPTS, _AUDIT, _OUTBOX):
        if {
            (row[2], row[3], row[4])
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        } != expected_fks[table]:
            raise RuntimeError("revoke companion foreign key unavailable")
        for row in connection.execute(f"SELECT * FROM {table}"):
            ids = ("org_id", "command_id", "credential_id")
            invalid = not all(_valid_value(row[key]) for key in ids)
            if table == _RECEIPTS:
                invalid = (
                    invalid
                    or not all(
                        _valid_value(row[key])
                        for key in (
                            "principal_subject_id",
                            "identity_provider",
                            "identity_session_id",
                        )
                    )
                    or row["contract_version"] != 1
                    or row["action"] != "worker_credential.revoke"
                    or not all(
                        type(row[key]) is int and row[key] >= 1
                        for key in (
                            "attempt",
                            "expected_generation",
                            "expected_revision",
                            "result_revision",
                        )
                    )
                    or row["result_revision"] != row["expected_revision"] + 1
                    or not _valid_timestamp(row["revoked_at"])
                )
            else:
                invalid = (
                    invalid
                    or type(row["attempt"]) is not int
                    or row["attempt"] < 1
                    or row["event"] != "credential_revoked"
                    or not _valid_timestamp(row["occurred_at"])
                )
            if invalid:
                raise RuntimeError("revoke companion row unavailable")


def _migrate(connection: sqlite3.Connection) -> None:
    present = (
        any(
            _catalog_sql(connection, "table", table) is not None
            for table in (_RECEIPTS, _AUDIT, _OUTBOX)
        )
        or connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (_COMPONENT,)
        ).fetchone()
        is not None
    )
    if present:
        _validate_companion(connection)
        return
    for statement in _DDL.split(";"):
        if statement:
            connection.execute(statement)
    for statement in _TRIGGERS:
        connection.execute(statement)
    manifest = _manifest()
    connection.execute(
        "INSERT INTO schema_component_manifests VALUES(?,?,?,?)",
        (_COMPONENT, 1, manifest, hashlib.sha256(manifest.encode()).hexdigest()),
    )
    _validate_companion(connection)


@final
class CredentialScopedRevokeCapability:
    def __init__(self, value: "CredentialScopedRevoke", seal: object) -> None:
        if seal is not _FACTORY or type(value) is not CredentialScopedRevoke:
            raise TypeError("R5.4b revoke capability는 factory로만 조립합니다.")
        self._value = value
        self._seal = _SEAL
        self._used = False
        self._claim_lock = RLock()

    def _claim(self) -> "CredentialScopedRevoke":
        with self._claim_lock:
            if self._seal is not _SEAL or self._used:
                raise ValueError("R5.4b revoke capability를 claim할 수 없습니다.")
            self._used = True
            return self._value


@final
class CredentialScopedRevoke:
    def __init__(
        self,
        principal: AuthenticatedPrincipal,
        authorizer: SnapshotCentralAuthorizer,
        source: CurrentCredentialReadSource,
        provider: CredentialRevokeApprovalProvider,
        resolver: CurrentCredentialApprovalEvidenceResolver,
        clock: Callable[[], datetime],
        seal: object,
    ) -> None:
        if seal is not _OPS:
            raise TypeError("R5.4b revoke는 factory로만 조립합니다.")
        self._principal = principal
        self._authorizer = authorizer
        self._source = source
        self._provider = provider
        self._resolver = resolver
        self._clock = clock

    def _acquire_proof(
        self, row: sqlite3.Row, expected_generation: int, expected_revision: int
    ) -> CredentialApprovalEvidence | None:
        resource = ResourceRef(
            org_id=row["org_id"],
            kind="worker_credential",
            resource_id=row["credential_id"],
            owner_subject_id=row["owner_subject_id"],
        )
        digest = canonical_credential_command_digest(
            action="worker_credential.revoke",
            resource=resource,
            command={
                "expected_generation": expected_generation,
                "expected_revision": expected_revision,
            },
        )
        try:
            grant = self._authorizer.authorize(
                self._principal, "worker_credential.revoke", resource
            )
            evidence = self._provider.acquire_revoke_approval(
                self._principal, "worker_credential.revoke", resource, digest
            )
            current = self._resolver.resolve_credential_approval_evidence(
                org_id=row["org_id"], evidence_id=evidence.evidence_id
            )
            valid = (
                _current_scope_matches(row, self._source)
                and type(grant) is AuthorizationGrant
                and self._authorizer.verify(
                    grant, self._principal, "worker_credential.revoke", resource
                )
                is True
                and type(evidence) is CredentialApprovalEvidence
                and type(current) is CredentialApprovalEvidence
                and current == evidence
                and evidence.action == "worker_credential.revoke"
                and evidence.command_digest == digest
                and evidence.resource_fingerprint == resource_fingerprint(resource)
            )
            return evidence if valid else None
        except Exception:
            return None

    def _prewrite_proof(
        self,
        row: sqlite3.Row,
        expected_generation: int,
        expected_revision: int,
        evidence: CredentialApprovalEvidence,
    ) -> bool:
        resource = ResourceRef(
            org_id=row["org_id"],
            kind="worker_credential",
            resource_id=row["credential_id"],
            owner_subject_id=row["owner_subject_id"],
        )
        digest = canonical_credential_command_digest(
            action="worker_credential.revoke",
            resource=resource,
            command={
                "expected_generation": expected_generation,
                "expected_revision": expected_revision,
            },
        )
        try:
            grant = self._authorizer.authorize(
                self._principal, "worker_credential.revoke", resource
            )
            current = self._resolver.resolve_credential_approval_evidence(
                org_id=row["org_id"], evidence_id=evidence.evidence_id
            )
            return (
                _current_scope_matches(row, self._source)
                and type(grant) is AuthorizationGrant
                and self._authorizer.verify(
                    grant, self._principal, "worker_credential.revoke", resource
                )
                is True
                and type(current) is CredentialApprovalEvidence
                and current == evidence
                and evidence.action == "worker_credential.revoke"
                and evidence.command_digest == digest
                and evidence.resource_fingerprint == resource_fingerprint(resource)
            )
        except Exception:
            return False

    def _current_access(self, row: sqlite3.Row) -> bool:
        """Validate replay's current scope and authority without reacquiring HITL."""
        resource = ResourceRef(
            org_id=row["org_id"],
            kind="worker_credential",
            resource_id=row["credential_id"],
            owner_subject_id=row["owner_subject_id"],
        )
        try:
            grant = self._authorizer.authorize(
                self._principal, "worker_credential.revoke", resource
            )
            return (
                _current_scope_matches(row, self._source)
                and type(grant) is AuthorizationGrant
                and self._authorizer.verify(
                    grant, self._principal, "worker_credential.revoke", resource
                )
                is True
            )
        except Exception:
            return False

    def revoke(
        self,
        path: str | Path,
        org_id: str,
        credential_id: str,
        *,
        command: CredentialRevokeCommand,
        fault_injector: object | None = None,
    ) -> (
        CredentialRevokeResult
        | CredentialReadNotFoundOrDenied
        | CredentialReadUnavailable
        | CredentialRevokeConflict
    ):
        if (
            type(command) is not CredentialRevokeCommand
            or type(command.command_id) is not str
            or not command.command_id
            or type(command.attempt) is not int
            or command.attempt < 1
            or type(command.expected_generation) is not int
            or command.expected_generation < 1
            or type(command.expected_revision) is not int
            or command.expected_revision < 1
        ):
            return CredentialReadUnavailable()
        command_id = command.command_id
        attempt = command.attempt
        expected_generation = command.expected_generation
        expected_revision = command.expected_revision
        try:
            connection = open_sqlite_durable_credential_scope_projections_for_scoped_read(path)
        except Exception:
            return CredentialReadUnavailable()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _migrate(connection)
            receipt = connection.execute(
                f"SELECT * FROM {_RECEIPTS} WHERE org_id=? AND command_id=? AND attempt=?",
                (org_id, command_id, attempt),
            ).fetchone()
            if receipt is not None and (
                receipt["credential_id"] != credential_id
                or receipt["expected_generation"] != expected_generation
                or receipt["expected_revision"] != expected_revision
                or receipt["principal_subject_id"] != self._principal.subject_id
                or receipt["identity_provider"] != self._principal.identity_provider
                or receipt["identity_session_id"] != self._principal.identity_session_id
                or receipt["action"] != "worker_credential.revoke"
            ):
                connection.rollback()
                return CredentialRevokeConflict()
            row = connection.execute(
                "SELECT p.*,c.role,c.generation,c.revision,c.status,c.issued_at,c.revoked_at FROM durable_credential_scope_projections_v1 p JOIN durable_credentials c ON c.org_id=p.org_id AND c.credential_id=p.credential_id WHERE p.org_id=? AND p.credential_id=?",
                (org_id, credential_id),
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT 1 FROM durable_credentials WHERE org_id=? AND credential_id=?",
                    (org_id, credential_id),
                ).fetchone()
                connection.rollback()
                return CredentialReadUnavailable() if exists else CredentialReadNotFoundOrDenied()
            if receipt is not None:
                if not self._current_access(row):
                    connection.rollback()
                    return CredentialReadUnavailable()
                result = _view(row)
                if (
                    result is None
                    or row["revision"] != receipt["result_revision"]
                    or row["status"] != "revoked"
                ):
                    connection.rollback()
                    return CredentialReadUnavailable()
                connection.rollback()
                return CredentialRevokeResult(result)
            evidence = self._acquire_proof(row, expected_generation, expected_revision)
            if evidence is None:
                connection.rollback()
                return CredentialReadUnavailable()
            # callbacks above are external reads: re-open canonical anchor inside this write transaction.
            fresh = connection.execute(
                "SELECT p.*,c.role,c.generation,c.revision,c.status,c.issued_at,c.revoked_at FROM durable_credential_scope_projections_v1 p JOIN durable_credentials c ON c.org_id=p.org_id AND c.credential_id=p.credential_id WHERE p.org_id=? AND p.credential_id=?",
                (org_id, credential_id),
            ).fetchone()
            if (
                fresh is None
                or tuple(fresh) != tuple(row)
                or not self._prewrite_proof(fresh, expected_generation, expected_revision, evidence)
            ):
                connection.rollback()
                return CredentialReadUnavailable()
            if (
                fresh["status"] != "active"
                or fresh["generation"] != expected_generation
                or fresh["revision"] != expected_revision
            ):
                connection.rollback()
                return CredentialRevokeConflict()
            revoked_at = _server_timestamp(self._clock)
            if revoked_at is None:
                connection.rollback()
                return CredentialReadUnavailable()
            updated = connection.execute(
                "UPDATE durable_credentials SET status='revoked',revision=revision+1,revoked_at=? WHERE org_id=? AND credential_id=? AND generation=? AND revision=? AND status='active'",
                (revoked_at, org_id, credential_id, expected_generation, expected_revision),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return CredentialRevokeConflict()
            if callable(fault_injector):
                cast(object, fault_injector)("update")  # type: ignore[operator]
            new_revision = expected_revision + 1
            connection.execute(
                f"INSERT INTO {_RECEIPTS} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    1,
                    org_id,
                    command_id,
                    attempt,
                    credential_id,
                    self._principal.subject_id,
                    self._principal.identity_provider,
                    self._principal.identity_session_id,
                    "worker_credential.revoke",
                    expected_generation,
                    expected_revision,
                    new_revision,
                    revoked_at,
                ),
            )
            if callable(fault_injector):
                cast(object, fault_injector)("receipt")  # type: ignore[operator]
            connection.execute(
                f"INSERT INTO {_AUDIT} VALUES(?,?,?,?,?,?)",
                (org_id, command_id, attempt, credential_id, "credential_revoked", revoked_at),
            )
            if callable(fault_injector):
                cast(object, fault_injector)("audit")  # type: ignore[operator]
            connection.execute(
                f"INSERT INTO {_OUTBOX} VALUES(?,?,?,?,?,?)",
                (org_id, command_id, attempt, credential_id, "credential_revoked", revoked_at),
            )
            if callable(fault_injector):
                cast(object, fault_injector)("outbox")  # type: ignore[operator]
            result_row = connection.execute(
                "SELECT p.*,c.role,c.generation,c.revision,c.status,c.issued_at,c.revoked_at FROM durable_credential_scope_projections_v1 p JOIN durable_credentials c ON c.org_id=p.org_id AND c.credential_id=p.credential_id WHERE p.org_id=? AND p.credential_id=?",
                (org_id, credential_id),
            ).fetchone()
            result = _view(result_row) if result_row is not None else None
            if result is None:
                raise RuntimeError("readback unavailable")
            _validate_companion(connection)
            if callable(fault_injector):
                cast(object, fault_injector)("readback")  # type: ignore[operator]
            connection.commit()
            return CredentialRevokeResult(result)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            return CredentialReadUnavailable()
        finally:
            connection.close()


def create_credential_scoped_revoke_capability(
    *,
    server_principal: object,
    central_authorizer: object,
    reader_source: object,
    approval_provider: object,
    approval_resolver: object,
    server_clock: object,
) -> CredentialScopedRevokeCapability:
    if (
        type(server_principal) is not AuthenticatedPrincipal
        or type(central_authorizer) is not SnapshotCentralAuthorizer
        or not callable(getattr(reader_source, "resolve_credential_read_scope", None))
        or not callable(getattr(approval_provider, "acquire_revoke_approval", None))
        or not callable(getattr(approval_resolver, "resolve_credential_approval_evidence", None))
        or not callable(server_clock)
    ):
        raise TypeError("R5.4b exact revoke dependencies가 필요합니다.")
    return CredentialScopedRevokeCapability(
        CredentialScopedRevoke(
            server_principal,
            central_authorizer,
            cast(CurrentCredentialReadSource, reader_source),
            cast(CredentialRevokeApprovalProvider, approval_provider),
            cast(CurrentCredentialApprovalEvidenceResolver, approval_resolver),
            cast(Callable[[], datetime], server_clock),
            _OPS,
        ),
        _FACTORY,
    )


def create_credential_scoped_revoke(
    capability: CredentialScopedRevokeCapability,
) -> CredentialScopedRevoke:
    if type(capability) is not CredentialScopedRevokeCapability:
        raise TypeError("exact R5.4b revoke capability가 필요합니다.")
    return capability._claim()  # pyright: ignore[reportPrivateUsage]
