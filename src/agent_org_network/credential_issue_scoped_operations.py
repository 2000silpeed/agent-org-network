"""Sealed R5.2a scoped Credential Issue Target stage bridge."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, cast, final
from collections.abc import Callable

from agent_org_network._credential_issue_transition_core import (
    CommittedCredentialDelivery,
    CredentialIssueMaterializationGuard,
    CredentialIssueMaterializationCompanion,
    CredentialIssueMaterializationSnapshot,
    MaterializationPrewriteFaultInjector,
    CredentialIssueTransitionError,
    ExistingReservedStageRequest,
    ExistingReservedStageGuard,
    StageFaultInjector,
    _TRANSITION_ENTRY_SEAL,  # pyright: ignore[reportPrivateUsage]
    _stage_existing_reserved_credential_issue_target,  # pyright: ignore[reportPrivateUsage]
    _materialize_sqlite_durable_credential_issue_target,  # pyright: ignore[reportPrivateUsage]
    _canonical_committed_delivery_ref,  # pyright: ignore[reportPrivateUsage]
    _snapshot,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
    SnapshotCentralAuthorizer,
)
from agent_org_network.credential_delivery import DeliveryStage, StageMissing
from agent_org_network.credential_issue_materialization_verifier import (
    CurrentCredentialApprovalEvidenceResolver,
    CurrentCredentialPrincipalResolver,
)
from agent_org_network.durable_credentials import (
    CredentialApprovalEvidence,
    canonical_credential_command_digest,
    resource_fingerprint,
)
from agent_org_network.sqlite_durable_credential_issue_targets import (
    DurableCredentialIssueTargetReservation,
)
from agent_org_network.sqlite_durable_credential_scope_bindings import (
    CredentialScopeSnapshot,
    CredentialScopeSource,
    open_sqlite_durable_credential_scope_bindings,
    reserve_sqlite_credential_scope_binding,
    validate_sqlite_durable_credential_scope_bindings_connection,
)
from agent_org_network.sqlite_durable_credential_scope_projections import (
    insert_sqlite_durable_credential_scope_projection,
    validate_sqlite_durable_credential_scope_projections_connection,
)

__all__ = (
    "CommittedCredentialDelivery",
    "CredentialIssueScopedOperations",
    "CredentialIssueScopedOperationsCapability",
    "ScopedCredentialIssueUnavailable",
    "create_credential_issue_scoped_operations",
    "create_credential_issue_scoped_operations_capability",
)


class ScopedCredentialIssueUnavailable(RuntimeError):
    """R5.2a scope, authority, evidence, or delivery precondition is unavailable."""


@dataclass(frozen=True)
class NeedsInitialStage:
    pass


@dataclass(frozen=True)
class AlreadyStaged:
    delivery_ref: str


@dataclass(frozen=True)
class AlreadyCommitted:
    delivery_ref: str


@dataclass(frozen=True)
class ClaimedRecovery:
    pass


class ScopedCredentialDelivery(Protocol):
    def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing: ...

    def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage: ...

    def release(self, delivery_ref: str) -> None: ...


class CredentialIssueApprovalProvider(Protocol):
    def acquire_issue_approval(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        resource: ResourceRef,
        command_digest: str,
    ) -> CredentialApprovalEvidence: ...


class ScopedReservationCommand(Protocol):
    @property
    def org_id(self) -> str: ...
    @property
    def target_id(self) -> str: ...
    @property
    def credential_id(self) -> str: ...
    @property
    def agent_card_id(self) -> str: ...
    @property
    def owner_subject_id(self) -> str: ...
    @property
    def role(self) -> str: ...
    @property
    def request_id(self) -> str: ...
    @property
    def attempt(self) -> int: ...
    @property
    def expires_at(self) -> str | None: ...
    @property
    def stage_key(self) -> str: ...
    @property
    def created_at(self) -> str: ...


_CAPABILITY_FACTORY_SEAL: Final = object()
_CAPABILITY_SEAL: Final = object()
_OPERATIONS_SEAL: Final = object()


@final
class _ScopedStageGuard(ExistingReservedStageGuard):
    def __init__(
        self,
        *,
        binding_source: CredentialScopeSource,
        principal_resolver: CurrentCredentialPrincipalResolver,
        server_principal: AuthenticatedPrincipal,
        central_authorizer: SnapshotCentralAuthorizer,
        approval_resolver: CurrentCredentialApprovalEvidenceResolver,
    ) -> None:
        self._binding_source = binding_source
        self._principal_resolver = principal_resolver
        self._server_principal = server_principal
        self._central_authorizer = central_authorizer
        self._approval_resolver = approval_resolver

    @property
    def server_principal(self) -> AuthenticatedPrincipal:
        return self._server_principal

    @property
    def binding_source(self) -> CredentialScopeSource:
        return self._binding_source

    @property
    def central_authorizer(self) -> SnapshotCentralAuthorizer:
        return self._central_authorizer

    @property
    def approval_resolver(self) -> CurrentCredentialApprovalEvidenceResolver:
        return self._approval_resolver

    def _current(self, request: ExistingReservedStageRequest, binding: sqlite3.Row) -> bool:
        try:
            card_id = binding["agent_card_id"]
            if type(card_id) is not str:
                return False
            snapshot = self._binding_source.resolve_issue_scope(
                request.reservation.org_id, request.reservation.credential_id, card_id
            )
            principal = self._principal_resolver.resolve_credential_principal(
                org_id=request.reservation.org_id, subject_id=request.reservation.principal_id
            )
            evidence = self._approval_resolver.resolve_credential_approval_evidence(
                org_id=request.reservation.org_id,
                evidence_id=request.reservation.approval_evidence_id,
            )
        except Exception:
            return False
        reservation = request.reservation
        resource = ResourceRef(
            org_id=reservation.org_id,
            kind="worker_credential",
            resource_id=reservation.credential_id,
            owner_subject_id=reservation.owner_subject_id,
        )
        command = {
            "owner_subject_id": reservation.owner_subject_id,
            "role": reservation.role,
            "expires_at": reservation.expires_at,
        }
        try:
            digest = canonical_credential_command_digest(
                action="worker_credential.issue", resource=resource, command=command
            )
            fingerprint = resource_fingerprint(resource)
            grant = self._central_authorizer.authorize(
                self._server_principal, "worker_credential.issue", resource
            )
        except Exception:
            return False
        return (
            type(snapshot) is CredentialScopeSnapshot
            and snapshot.binding_row(reservation.target_id, binding["created_at"]) == tuple(binding)
            and type(principal) is AuthenticatedPrincipal
            and principal == self._server_principal
            and principal.org_id == reservation.org_id
            and principal.subject_id == reservation.principal_id
            and digest == reservation.command_digest == reservation.approval_command_digest
            and fingerprint
            == reservation.resource_fingerprint
            == reservation.approval_resource_fingerprint
            and type(evidence) is CredentialApprovalEvidence
            and evidence.evidence_id == reservation.approval_evidence_id
            and evidence.action == "worker_credential.issue"
            and evidence.command_digest == reservation.approval_command_digest
            and evidence.resource_fingerprint == reservation.approval_resource_fingerprint
            and type(grant) is AuthorizationGrant
            and self._central_authorizer.verify(
                grant, self._server_principal, "worker_credential.issue", resource
            )
            is True
        )

    def validate_existing_reserved_stage(
        self, connection: sqlite3.Connection, request: ExistingReservedStageRequest
    ) -> None:
        try:
            validate_sqlite_durable_credential_scope_bindings_connection(
                connection, source=self._binding_source
            )
        except Exception as error:
            raise ScopedCredentialIssueUnavailable(
                "R5.1 scope binding이 unavailable입니다."
            ) from error
        binding = connection.execute(
            "SELECT * FROM durable_credential_scope_bindings_v1 WHERE org_id=? AND target_id=?",
            (request.reservation.org_id, request.reservation.target_id),
        ).fetchone()
        if binding is None or not self._current(request, binding):
            raise ScopedCredentialIssueUnavailable("current scope-bound issue proof가 필요합니다.")

    def validate_preexternal_stage(
        self, path: str | Path, request: ExistingReservedStageRequest
    ) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = open_sqlite_durable_credential_scope_bindings(
                path, source=self._binding_source
            )
            binding = connection.execute(
                "SELECT * FROM durable_credential_scope_bindings_v1 WHERE org_id=? AND target_id=?",
                (request.reservation.org_id, request.reservation.target_id),
            ).fetchone()
            if binding is None or not self._current(request, binding):
                raise ScopedCredentialIssueUnavailable(
                    "pre-external current issue proof가 필요합니다."
                )
        finally:
            if connection is not None:
                connection.close()

    def materialization_dependencies(
        self,
    ) -> tuple[
        CredentialScopeSource,
        CurrentCredentialPrincipalResolver,
        AuthenticatedPrincipal,
        SnapshotCentralAuthorizer,
        CurrentCredentialApprovalEvidenceResolver,
    ]:
        return (
            self._binding_source,
            self._principal_resolver,
            self._server_principal,
            self._central_authorizer,
            self._approval_resolver,
        )


@final
class _ScopedMaterializationGuard(CredentialIssueMaterializationGuard):
    def __init__(self, stage_guard: _ScopedStageGuard) -> None:
        (
            self._binding_source,
            self._principal_resolver,
            self._server_principal,
            self._central_authorizer,
            self._approval_resolver,
        ) = stage_guard.materialization_dependencies()

    def _current(
        self, connection: sqlite3.Connection, snapshot: CredentialIssueMaterializationSnapshot
    ) -> bool:
        try:
            raw = json.loads(snapshot.target_json)
            if type(raw) is not dict:
                return False
            target = cast(dict[str, object], raw)
            required = {
                "org_id",
                "target_id",
                "credential_id",
                "principal_id",
                "owner_subject_id",
                "role",
                "expires_at",
                "command_digest",
                "resource_fingerprint",
                "approval_evidence_id",
                "approval_command_digest",
                "approval_resource_fingerprint",
                "target_generation",
            }
            if set(target) != required or any(
                type(target[key]) is not str
                for key in required - {"expires_at", "target_generation"}
            ):
                return False
            binding = connection.execute(
                "SELECT * FROM durable_credential_scope_bindings_v1 WHERE org_id=? AND target_id=?",
                (target["org_id"], target["target_id"]),
            ).fetchone()
            if binding is None:
                return False
            validate_sqlite_durable_credential_scope_bindings_connection(
                connection, source=self._binding_source
            )  # pyright: ignore[reportPrivateUsage]
            card_id = binding["agent_card_id"]
            current_scope = self._binding_source.resolve_issue_scope(
                cast(str, target["org_id"]), cast(str, target["credential_id"]), card_id
            )  # pyright: ignore[reportPrivateUsage]
            principal = self._principal_resolver.resolve_credential_principal(
                org_id=cast(str, target["org_id"]), subject_id=cast(str, target["principal_id"])
            )  # pyright: ignore[reportPrivateUsage]
            evidence = self._approval_resolver.resolve_credential_approval_evidence(
                org_id=cast(str, target["org_id"]),
                evidence_id=cast(str, target["approval_evidence_id"]),
            )  # pyright: ignore[reportPrivateUsage]
            resource = ResourceRef(
                org_id=cast(str, target["org_id"]),
                kind="worker_credential",
                resource_id=cast(str, target["credential_id"]),
                owner_subject_id=cast(str, target["owner_subject_id"]),
            )
            digest = canonical_credential_command_digest(
                action="worker_credential.issue",
                resource=resource,
                command={
                    "owner_subject_id": target["owner_subject_id"],
                    "role": target["role"],
                    "expires_at": target["expires_at"],
                },
            )
            fingerprint = resource_fingerprint(resource)
            grant = self._central_authorizer.authorize(
                self._server_principal, "worker_credential.issue", resource
            )  # pyright: ignore[reportPrivateUsage]
            return (
                type(current_scope) is CredentialScopeSnapshot
                and current_scope.binding_row(cast(str, target["target_id"]), binding["created_at"])
                == tuple(binding)
                and type(principal) is AuthenticatedPrincipal
                and principal == self._server_principal
                and digest == target["command_digest"] == target["approval_command_digest"]
                and fingerprint
                == target["resource_fingerprint"]
                == target["approval_resource_fingerprint"]
                and type(evidence) is CredentialApprovalEvidence
                and evidence.evidence_id == target["approval_evidence_id"]
                and evidence.action == "worker_credential.issue"
                and evidence.command_digest == digest
                and evidence.resource_fingerprint == fingerprint
                and type(grant) is AuthorizationGrant
                and self._central_authorizer.verify(
                    grant, self._server_principal, "worker_credential.issue", resource
                )
                is True
            )  # pyright: ignore[reportPrivateUsage]
        except Exception:
            return False

    def prepare(
        self, connection: sqlite3.Connection, snapshot: CredentialIssueMaterializationSnapshot
    ) -> str | None:
        return snapshot.snapshot_digest if self._current(connection, snapshot) else None

    @property
    def binding_source(self) -> CredentialScopeSource:
        return self._binding_source

    def verify_prewrite(
        self,
        connection: sqlite3.Connection,
        snapshot: CredentialIssueMaterializationSnapshot,
        permit_digest: str,
    ) -> bool:
        return permit_digest == snapshot.snapshot_digest and self._current(connection, snapshot)


@final
class _ScopedProjectionCompanion(CredentialIssueMaterializationCompanion):
    def __init__(self, source: CredentialScopeSource) -> None:
        self._source = source

    def persist(self, connection: sqlite3.Connection, target: sqlite3.Row, now: str) -> None:
        insert_sqlite_durable_credential_scope_projection(
            connection, org_id=target["org_id"], target_id=target["target_id"], now=now,
            source=self._source,
        )

    def verify(self, connection: sqlite3.Connection, target: sqlite3.Row, now: str) -> bool:
        try:
            del now
            validate_sqlite_durable_credential_scope_projections_connection(
                connection, source=self._source
            )
            row = connection.execute(
                "SELECT 1 FROM durable_credential_scope_projections_v1 "
                "WHERE org_id=? AND target_id=?",
                (target["org_id"], target["target_id"]),
            ).fetchone()
            return row is not None
        except Exception:
            return False


@final
class CredentialIssueScopedOperationsCapability:
    def __init__(self, operations: "CredentialIssueScopedOperations", seal: object) -> None:
        if (
            seal is not _CAPABILITY_FACTORY_SEAL
            or type(operations) is not CredentialIssueScopedOperations
        ):
            raise TypeError("scoped credential operations는 sealed factory로만 조립합니다.")
        self._operations = operations
        self._seal = _CAPABILITY_SEAL
        self._claimed = False

    def _claim(self) -> "CredentialIssueScopedOperations":
        if self._seal is not _CAPABILITY_SEAL or self._claimed:
            raise ValueError("scoped credential operations capability를 claim할 수 없습니다.")
        self._claimed = True
        return self._operations


@final
class CredentialIssueScopedOperations:
    def __init__(
        self,
        guard: _ScopedStageGuard,
        delivery: ScopedCredentialDelivery,
        approval_provider: CredentialIssueApprovalProvider | None,
        seal: object,
    ) -> None:
        if seal is not _OPERATIONS_SEAL:
            raise TypeError("scoped credential operations는 factory로만 조립합니다.")
        self._guard = guard
        self._materialization_guard = _ScopedMaterializationGuard(guard)
        self._projection_companion = _ScopedProjectionCompanion(guard.binding_source)
        self._delivery = delivery
        self._approval_provider = approval_provider

    def stage_readiness(self, path: str | Path, org_id: str, target_id: str) -> NeedsInitialStage | AlreadyStaged | AlreadyCommitted | ClaimedRecovery:
        connection: sqlite3.Connection | None = None
        try:
            connection = open_sqlite_durable_credential_scope_bindings(
                path, source=self._guard.binding_source
            )
            target = connection.execute(
                "SELECT state FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            fence = connection.execute(
                "SELECT state,delivery_ref FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            if target is None:
                raise ScopedCredentialIssueUnavailable("exact scoped target이 필요합니다.")
            if fence is None and target["state"] == "Reserved":
                return NeedsInitialStage()
            if fence is None:
                raise ScopedCredentialIssueUnavailable("target/fence lifecycle이 unavailable입니다.")
            if fence["state"] == "ClaimedStage":
                return ClaimedRecovery()
            if fence["state"] == "Staged" and type(fence["delivery_ref"]) is str:
                return AlreadyStaged(fence["delivery_ref"])
            if fence["state"] == "Committed" and type(fence["delivery_ref"]) is str:
                return AlreadyCommitted(fence["delivery_ref"])
            raise ScopedCredentialIssueUnavailable("target/fence lifecycle이 unavailable입니다.")
        except ScopedCredentialIssueUnavailable:
            raise
        except Exception as error:
            raise ScopedCredentialIssueUnavailable("scoped stage readiness가 unavailable입니다.") from error
        finally:
            if connection is not None:
                connection.close()

    def reserve(self, path: str | Path, command: ScopedReservationCommand) -> DurableCredentialIssueTargetReservation:
        guard = self._guard
        if self._approval_provider is None:
            raise ScopedCredentialIssueUnavailable("issue approval provider가 unavailable입니다.")
        principal = guard.server_principal
        resource = ResourceRef(
            org_id=command.org_id,
            kind="worker_credential",
            resource_id=command.credential_id,
            owner_subject_id=command.owner_subject_id,
        )
        payload = {
            "owner_subject_id": command.owner_subject_id,
            "role": command.role,
            "expires_at": command.expires_at,
        }
        try:
            current_scope = guard.binding_source.resolve_issue_scope(
                command.org_id, command.credential_id, command.agent_card_id
            )
            digest = canonical_credential_command_digest(
                action="worker_credential.issue", resource=resource, command=payload
            )
            fingerprint = resource_fingerprint(resource)
            grant = guard.central_authorizer.authorize(
                principal, "worker_credential.issue", resource
            )
            if (
                type(current_scope) is not CredentialScopeSnapshot
                or current_scope.org_id != command.org_id
                or current_scope.owner_subject_id != command.owner_subject_id
                or current_scope.credential_id != command.credential_id
                or current_scope.agent_card_id != command.agent_card_id
                or current_scope.credential_resource_fingerprint != fingerprint
                or type(grant) is not AuthorizationGrant
                or guard.central_authorizer.verify(
                    grant, principal, "worker_credential.issue", resource
                ) is not True
            ):
                raise ScopedCredentialIssueUnavailable("current scope 또는 grant가 필요합니다.")
            evidence = self._approval_provider.acquire_issue_approval(
                principal, "worker_credential.issue", resource, digest
            )
            resolved = guard.approval_resolver.resolve_credential_approval_evidence(
                org_id=command.org_id, evidence_id=evidence.evidence_id
            )
            if (
                type(evidence) is not CredentialApprovalEvidence
                or type(resolved) is not CredentialApprovalEvidence
                or resolved != evidence
                or evidence.action != "worker_credential.issue"
                or evidence.command_digest != digest
                or evidence.resource_fingerprint != fingerprint
            ):
                raise ScopedCredentialIssueUnavailable("current approval evidence가 필요합니다.")
            # provider 호출 뒤 owner/scope/policy가 바뀌었으면 write 전에 닫는다.
            fresh_scope = guard.binding_source.resolve_issue_scope(
                command.org_id, command.credential_id, command.agent_card_id
            )
            fresh_grant = guard.central_authorizer.authorize(
                principal, "worker_credential.issue", resource
            )
            if (
                fresh_scope != current_scope
                or type(fresh_grant) is not AuthorizationGrant
                or guard.central_authorizer.verify(
                    fresh_grant, principal, "worker_credential.issue", resource
                ) is not True
            ):
                raise ScopedCredentialIssueUnavailable("pre-reservation current proof가 필요합니다.")
            reservation = DurableCredentialIssueTargetReservation(
                org_id=command.org_id,
                target_id=command.target_id,
                credential_id=command.credential_id,
                command_digest=digest,
                principal_id=principal.subject_id,
                owner_subject_id=command.owner_subject_id,
                role=command.role,
                expires_at=command.expires_at,
                resource_fingerprint=fingerprint,
                approval_evidence_id=evidence.evidence_id,
                approval_command_digest=digest,
                approval_resource_fingerprint=fingerprint,
                target_generation=command.attempt,
                created_at=command.created_at,
            )
            reserve_sqlite_credential_scope_binding(
                path,
                reservation=reservation,
                agent_card_id=command.agent_card_id,
                now=command.created_at,
                source=guard.binding_source,
            )
            return reservation
        except ScopedCredentialIssueUnavailable:
            raise
        except Exception as error:
            raise ScopedCredentialIssueUnavailable("scoped reservation이 unavailable입니다.") from error

    def stage(
        self,
        path: str | Path,
        reservation: DurableCredentialIssueTargetReservation,
        stage_key: str,
        raw_secret: str,
        now: str,
        *,
        fault_injector: StageFaultInjector | None = None,
    ) -> DeliveryStage:
        request = ExistingReservedStageRequest(
            reservation=reservation,
            stage_key=stage_key,
            raw_secret=raw_secret,
            now=now,
            guard=self._guard,
        )
        try:
            return _stage_existing_reserved_credential_issue_target(
                path,
                request,
                self._delivery,
                fault_injector=fault_injector,
                entry_seal=_TRANSITION_ENTRY_SEAL,
            )
        except CredentialIssueTransitionError as error:
            raise ScopedCredentialIssueUnavailable(str(error)) from error

    def materialize(
        self, path: str | Path, org_id: str, target_id: str, now: str
    ) -> CommittedCredentialDelivery:
        return self._materialize_for_test(path, org_id, target_id, now)

    def _materialize_for_test(
        self,
        path: str | Path,
        org_id: str,
        target_id: str,
        now: str,
        *,
        prewrite_fault_injector: MaterializationPrewriteFaultInjector | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> CommittedCredentialDelivery:
        def scoped_fault(point: str) -> None:
            if fault_injector is None:
                return
            # Core owns an arbitrary companion, never a Scope Projection. Keep
            # legacy deterministic seams at this scoped adapter boundary.
            legacy = {
                "after_companion_persist": "after_projection_insert",
                "after_companion_readback": "after_projection_readback",
            }.get(point, point)
            fault_injector(legacy)

        try:
            return _materialize_sqlite_durable_credential_issue_target(
                path,
                org_id,
                target_id,
                now,
                cast(Any, object()),
                fault_injector=scoped_fault if fault_injector is not None else None,
                guard=self._materialization_guard,
                companion=self._projection_companion,
                prewrite_fault_injector=prewrite_fault_injector,
            )
        except Exception as error:
            raise ScopedCredentialIssueUnavailable(
                "scoped materialization이 unavailable입니다."
            ) from error

    def read_committed(
        self, path: str | Path, org_id: str, target_id: str
    ) -> CommittedCredentialDelivery:
        connection: sqlite3.Connection | None = None
        try:
            connection = open_sqlite_durable_credential_scope_bindings(
                path, source=self._materialization_guard.binding_source
            )
            validate_sqlite_durable_credential_scope_projections_connection(
                connection, source=self._materialization_guard.binding_source
            )
            target = connection.execute(
                "SELECT * FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            fence = connection.execute(
                "SELECT * FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            if target is None or fence is None:
                raise ScopedCredentialIssueUnavailable("exact committed target이 필요합니다.")
            snapshot = _snapshot(target, fence)
            if (
                self._materialization_guard.prepare(connection, snapshot)
                != snapshot.snapshot_digest
            ):
                raise ScopedCredentialIssueUnavailable(
                    "current committed scope proof가 필요합니다."
                )
            ref = _canonical_committed_delivery_ref(connection, target, fence)
            return CommittedCredentialDelivery(org_id, target_id, target["credential_id"], ref)
        except ScopedCredentialIssueUnavailable:
            raise
        except Exception as error:
            raise ScopedCredentialIssueUnavailable(
                "scoped committed readback이 unavailable입니다."
            ) from error
        finally:
            if connection is not None:
                connection.close()


def create_credential_issue_scoped_operations_capability(
    *,
    binding_source: CredentialScopeSource,
    principal_resolver: CurrentCredentialPrincipalResolver,
    server_principal: AuthenticatedPrincipal,
    central_authorizer: SnapshotCentralAuthorizer,
    approval_resolver: CurrentCredentialApprovalEvidenceResolver,
    approval_provider: CredentialIssueApprovalProvider | None = None,
    delivery: ScopedCredentialDelivery,
) -> CredentialIssueScopedOperationsCapability:
    if (
        type(server_principal) is not AuthenticatedPrincipal
        or type(central_authorizer) is not SnapshotCentralAuthorizer
        or not callable(getattr(binding_source, "resolve_issue_scope", None))
        or not callable(getattr(principal_resolver, "resolve_credential_principal", None))
        or not callable(getattr(approval_resolver, "resolve_credential_approval_evidence", None))
        or not callable(getattr(delivery, "recover_stage", None))
        or not callable(getattr(delivery, "stage_once", None))
        or not callable(getattr(delivery, "release", None))
    ):
        raise TypeError("R5.2a scoped credential operation의 전체 composition이 필요합니다.")
    guard = _ScopedStageGuard(
        binding_source=binding_source,
        principal_resolver=principal_resolver,
        server_principal=server_principal,
        central_authorizer=central_authorizer,
        approval_resolver=approval_resolver,
    )
    return CredentialIssueScopedOperationsCapability(
        CredentialIssueScopedOperations(guard, delivery, approval_provider, _OPERATIONS_SEAL), _CAPABILITY_FACTORY_SEAL
    )


def create_credential_issue_scoped_operations(
    capability: CredentialIssueScopedOperationsCapability,
) -> CredentialIssueScopedOperations:
    if type(capability) is not CredentialIssueScopedOperationsCapability:
        raise TypeError("exact scoped credential operations capability가 필요합니다.")
    return capability._claim()  # pyright: ignore[reportPrivateUsage]
