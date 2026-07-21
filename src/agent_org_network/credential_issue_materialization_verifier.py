"""R3.2의 현재 credential issue 검증 조합.

이 module은 commit boundary를 열지 않는다. 현재 Authority, principal, resource owner와
사람 승인 evidence를 모두 제공하는 sealed capability만 production verifier를 꺼낼 수
있다. legacy credential MCP는 이 조합을 소비하지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast, final

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
    SnapshotCentralAuthorizer,
)
from agent_org_network.durable_credentials import (
    CredentialApprovalEvidence,
    canonical_credential_command_digest,
    resource_fingerprint,
)
from agent_org_network._credential_issue_transition_core import (
    CredentialIssueMaterializationSnapshot,
    CredentialIssueMaterializationVerifier,
    MaterializationVerification,
    _PRODUCTION_COMMIT_SEAL,  # pyright: ignore[reportPrivateUsage]
    _commit_sqlite_durable_credential_issue_target_with_production_verifier,  # pyright: ignore[reportPrivateUsage]
    _verified_materialization_verification,  # pyright: ignore[reportPrivateUsage]
)


class CurrentCredentialPrincipalResolver(Protocol):
    """reservation의 principal ID에 대한 현재 server-side identity proof."""

    def resolve_credential_principal(
        self, *, org_id: str, subject_id: str
    ) -> AuthenticatedPrincipal | None: ...


class CurrentCredentialResourceResolver(Protocol):
    """credential ID의 현재 owner를 서버 source에서 읽는 proof."""

    def resolve_credential_resource(
        self, *, org_id: str, credential_id: str
    ) -> ResourceRef | None: ...


class CurrentCredentialApprovalEvidenceResolver(Protocol):
    """evidence ID가 아직 유효한 sealed approval snapshot인지 확인하는 proof."""

    def resolve_credential_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> CredentialApprovalEvidence | None: ...


_CAPABILITY_SEAL: Final = object()
_CAPABILITY_FACTORY_SEAL: Final = object()
_OPERATIONS_SEAL: Final = object()


@dataclass(frozen=True)
class _MaterializationTarget:
    org_id: str
    target_id: str
    credential_id: str
    command_digest: str
    principal_id: str
    owner_subject_id: str
    role: str
    expires_at: str | None
    resource_fingerprint: str
    approval_evidence_id: str
    approval_command_digest: str
    approval_resource_fingerprint: str
    target_generation: int


def _target(snapshot: CredentialIssueMaterializationSnapshot) -> _MaterializationTarget | None:
    try:
        raw_object = json.loads(snapshot.target_json)
        if type(raw_object) is not dict:
            return None
        raw = cast(dict[str, object], raw_object)
        required = {
            "approval_command_digest",
            "approval_evidence_id",
            "approval_resource_fingerprint",
            "command_digest",
            "credential_id",
            "expires_at",
            "org_id",
            "owner_subject_id",
            "principal_id",
            "resource_fingerprint",
            "role",
            "target_generation",
            "target_id",
        }
        if set(raw) != required:
            return None
        expires_at = raw["expires_at"]
        target_generation = raw["target_generation"]
        if (expires_at is not None and type(expires_at) is not str) or type(
            target_generation
        ) is not int:
            return None
        value = _MaterializationTarget(
            org_id=cast(str, raw["org_id"]),
            target_id=cast(str, raw["target_id"]),
            credential_id=cast(str, raw["credential_id"]),
            command_digest=cast(str, raw["command_digest"]),
            principal_id=cast(str, raw["principal_id"]),
            owner_subject_id=cast(str, raw["owner_subject_id"]),
            role=cast(str, raw["role"]),
            expires_at=expires_at,
            resource_fingerprint=cast(str, raw["resource_fingerprint"]),
            approval_evidence_id=cast(str, raw["approval_evidence_id"]),
            approval_command_digest=cast(str, raw["approval_command_digest"]),
            approval_resource_fingerprint=cast(str, raw["approval_resource_fingerprint"]),
            target_generation=target_generation,
        )
        if (
            value.target_id != snapshot.target_id
            or value.target_generation != snapshot.target_generation
            or any(
                type(item) is not str or not item.strip()
                for item in (
                    value.org_id,
                    value.credential_id,
                    value.command_digest,
                    value.principal_id,
                    value.owner_subject_id,
                    value.role,
                    value.resource_fingerprint,
                    value.approval_evidence_id,
                    value.approval_command_digest,
                    value.approval_resource_fingerprint,
                )
            )
            or type(value.target_generation) is not int
            or value.target_generation < 1
            or (value.expires_at is not None and type(value.expires_at) is not str)
        ):
            return None
        return value
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


@final
class CredentialIssueMaterializationVerifierCapability:
    """single-use production verifier capability; partial/fake objects cannot claim it."""

    def __init__(
        self, verifier: "_ProductionCredentialIssueMaterializationVerifier", seal: object
    ) -> None:
        if (
            seal is not _CAPABILITY_FACTORY_SEAL
            or type(verifier) is not _ProductionCredentialIssueMaterializationVerifier
        ):
            raise TypeError("R3.2 materialization verifier capability는 factory로만 조립합니다.")
        self._verifier = verifier
        self._seal = _CAPABILITY_SEAL
        self._claimed = False

    def _claim_for_operations(self) -> CredentialIssueMaterializationVerifier:
        if self._seal is not _CAPABILITY_SEAL or self._claimed:
            raise ValueError("R3.2 materialization verifier capability를 claim할 수 없습니다.")
        self._claimed = True
        return self._verifier


@final
class CredentialIssueMaterializationOperations:
    """R3.2 production materialization boundary bound to one exact capability."""

    def __init__(
        self,
        verifier: CredentialIssueMaterializationVerifier,
        seal: object,
    ) -> None:
        if seal is not _OPERATIONS_SEAL:
            raise TypeError("R3.2 materialization operations는 factory로만 조립합니다.")
        self._verifier = verifier

    def commit(
        self,
        path: str | Path,
        org_id: str,
        target_id: str,
        now: str,
        release: Callable[[str], None] | None = None,
    ) -> None:
        result = _commit_sqlite_durable_credential_issue_target_with_production_verifier(
            path,
            org_id,
            target_id,
            now,
            self._verifier,
            production_seal=_PRODUCTION_COMMIT_SEAL,
        )
        if release is not None:
            try:
                release(result.delivery_ref)
            except Exception as error:
                from agent_org_network._credential_issue_transition_core import (
                    SqliteCredentialIssueCommitError,
                )

                raise SqliteCredentialIssueCommitError(
                    "persisted delivery release가 unavailable입니다."
                ) from error


@final
class _ProductionCredentialIssueMaterializationVerifier:
    def __init__(
        self,
        *,
        central_authorizer: SnapshotCentralAuthorizer,
        principal_resolver: CurrentCredentialPrincipalResolver,
        resource_resolver: CurrentCredentialResourceResolver,
        approval_evidence_resolver: CurrentCredentialApprovalEvidenceResolver,
    ) -> None:
        self._central_authorizer = central_authorizer
        self._principal_resolver = principal_resolver
        self._resource_resolver = resource_resolver
        self._approval_evidence_resolver = approval_evidence_resolver

    def _current(self, snapshot: CredentialIssueMaterializationSnapshot) -> bool:
        target = _target(snapshot)
        if target is None:
            return False
        try:
            principal = self._principal_resolver.resolve_credential_principal(
                org_id=target.org_id, subject_id=target.principal_id
            )
            resource = self._resource_resolver.resolve_credential_resource(
                org_id=target.org_id, credential_id=target.credential_id
            )
            evidence = self._approval_evidence_resolver.resolve_credential_approval_evidence(
                org_id=target.org_id, evidence_id=target.approval_evidence_id
            )
        except Exception:
            return False
        if (
            type(principal) is not AuthenticatedPrincipal
            or type(resource) is not ResourceRef
            or type(evidence) is not CredentialApprovalEvidence
            or principal.org_id != target.org_id
            or principal.subject_id != target.principal_id
            or resource.org_id != target.org_id
            or resource.kind != "worker_credential"
            or resource.resource_id != target.credential_id
            or resource.owner_subject_id != target.owner_subject_id
        ):
            return False
        command = {
            "owner_subject_id": target.owner_subject_id,
            "role": target.role,
            "expires_at": target.expires_at,
        }
        try:
            digest = canonical_credential_command_digest(
                action="worker_credential.issue", resource=resource, command=command
            )
            fingerprint = resource_fingerprint(resource)
        except Exception:
            return False
        if (
            digest != target.command_digest
            or fingerprint != target.resource_fingerprint
            or evidence.evidence_id != target.approval_evidence_id
            or evidence.action != "worker_credential.issue"
            or evidence.command_digest != target.approval_command_digest != digest
            or evidence.resource_fingerprint != target.approval_resource_fingerprint != fingerprint
        ):
            return False
        try:
            grant = self._central_authorizer.authorize(
                principal, "worker_credential.issue", resource
            )
            return (
                type(grant) is AuthorizationGrant
                and self._central_authorizer.verify(
                    grant, principal, "worker_credential.issue", resource
                )
                is True
            )
        except Exception:
            return False

    def prepare(
        self, snapshot: CredentialIssueMaterializationSnapshot
    ) -> MaterializationVerification:
        if not self._current(snapshot):
            raise RuntimeError("current credential issue verification이 필요합니다.")
        return _verified_materialization_verification(snapshot)

    def verify_prewrite(
        self,
        proof: MaterializationVerification,
        snapshot: CredentialIssueMaterializationSnapshot,
    ) -> bool:
        return self._current(snapshot)


def create_credential_issue_materialization_verifier_capability(
    *,
    central_authorizer: SnapshotCentralAuthorizer,
    principal_resolver: CurrentCredentialPrincipalResolver,
    resource_resolver: CurrentCredentialResourceResolver,
    approval_evidence_resolver: CurrentCredentialApprovalEvidenceResolver,
) -> CredentialIssueMaterializationVerifierCapability:
    """R3.2 production composition을 완전한 exact dependency set에서만 만든다."""
    if (
        type(central_authorizer) is not SnapshotCentralAuthorizer
        or not callable(getattr(principal_resolver, "resolve_credential_principal", None))
        or not callable(getattr(resource_resolver, "resolve_credential_resource", None))
        or not callable(
            getattr(approval_evidence_resolver, "resolve_credential_approval_evidence", None)
        )
    ):
        raise TypeError("R3.2 materialization verifier의 전체 exact 조립 요소가 필요합니다.")
    verifier = _ProductionCredentialIssueMaterializationVerifier(
        central_authorizer=central_authorizer,
        principal_resolver=principal_resolver,
        resource_resolver=resource_resolver,
        approval_evidence_resolver=approval_evidence_resolver,
    )
    return CredentialIssueMaterializationVerifierCapability(verifier, _CAPABILITY_FACTORY_SEAL)


def create_credential_issue_materialization_operations(
    capability: CredentialIssueMaterializationVerifierCapability,
) -> CredentialIssueMaterializationOperations:
    """Claim an exact production capability once and bind its verifier privately."""
    if type(capability) is not CredentialIssueMaterializationVerifierCapability:
        raise TypeError("R3.2 materialization operations의 exact capability가 필요합니다.")
    return CredentialIssueMaterializationOperations(
        capability._claim_for_operations(),  # pyright: ignore[reportPrivateUsage]
        _OPERATIONS_SEAL,
    )
