"""R1.2 sealed approval vocabulary for tenant operational commands."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Literal, Protocol, TypeAlias

from agent_org_network.central_authority import AuthenticatedPrincipal, ResourceRef
from agent_org_network.operational_authorization import OperationalAction
from agent_org_network.tenant_operational_ports import ResourceFingerprint, ScopedUnavailable


@dataclass(frozen=True)
class TenantOperationalApprovalEvidence:
    evidence_id: str
    approver_subject_id: str
    action: OperationalAction
    command_digest: str
    resource_fingerprint: ResourceFingerprint
    approved_at: str
    kind: Literal["approved"] = "approved"

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not str or not value
                for value in (
                    self.evidence_id,
                    self.approver_subject_id,
                    self.command_digest,
                    self.approved_at,
                )
            )
            or len(self.command_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.command_digest)
            or self.action
            not in {"card.register", "card.transfer_owner", "session.end", "hitl.write"}
            or type(self.resource_fingerprint) is not ResourceFingerprint
            or self.kind != "approved"
        ):
            raise ValueError("tenant operational approval evidence가 strict하지 않습니다.")


@dataclass(frozen=True)
class TenantOperationalApprovalDenied:
    kind: Literal["denied"] = "denied"

    def __post_init__(self) -> None:
        if self.kind != "denied":
            raise ValueError("denied discriminator가 유효하지 않습니다.")


TenantOperationalApprovalOutcome: TypeAlias = (
    TenantOperationalApprovalEvidence | TenantOperationalApprovalDenied | ScopedUnavailable
)


class TenantOperationalApprovalPort(Protocol):
    def approve(
        self,
        principal: AuthenticatedPrincipal,
        action: OperationalAction,
        resource: ResourceRef,
        command_digest: str,
        resource_fingerprint: ResourceFingerprint,
    ) -> TenantOperationalApprovalOutcome: ...


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def canonical_tenant_operational_command_digest(
    *,
    org_id: str,
    principal_id: str,
    action: OperationalAction,
    resource_fingerprint: ResourceFingerprint,
    effect: object,
) -> str:
    if (
        type(org_id) is not str
        or type(principal_id) is not str
        or type(resource_fingerprint) is not ResourceFingerprint
    ):
        raise ValueError("canonical tenant operational command 입력이 strict하지 않습니다.")
    return sha256(
        _canonical(
            {
                "domain": "tenant-operational-command-v2",
                "org_id": org_id,
                "principal_id": principal_id,
                "action": action,
                "resource_fingerprint": resource_fingerprint.value,
                "effect": effect,
            }
        ).encode()
    ).hexdigest()


def canonical_tenant_operational_resource_fingerprint(
    *, resource: ResourceRef, state: object, source_digest: str
) -> ResourceFingerprint:
    if (
        type(resource) is not ResourceRef
        or type(source_digest) is not str
        or len(source_digest) != 64
    ):
        raise ValueError("canonical tenant operational resource 입력이 strict하지 않습니다.")
    return ResourceFingerprint(
        sha256(
            _canonical(
                {
                    "domain": "tenant-operational-resource-v1",
                    "resource": resource.model_dump(mode="json"),
                    "state": state,
                    "source_digest": source_digest,
                }
            ).encode()
        ).hexdigest()
    )
