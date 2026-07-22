"""S4.3c.1 escalation 승인 증거의 순수 검증 계약(ADR 0065 §6·§7).

취득·재확인 포트와 4결박 순수 검증만 연다. DB write·transaction·real store
resolver는 이 모듈의 범위가 아니다(S4.3c.3). 취득(`acquire_escalation_approval`)은
provider를 정확히 1회 호출하고, write 직전 재확인과 replay 재검증은
`reconfirm_escalation_approval`을 공유하며 승인을 재취득하지 않는다(R5.4
`_acquire_proof`/`_prewrite_proof` 정신).
"""

from __future__ import annotations

from typing import Protocol

from agent_org_network.central_authority import AuthenticatedPrincipal, ResourceRef
from agent_org_network.conflict_escalation_approval_evidence import (
    ESCALATE_ACTION,
    ConflictEscalationApprovalEvidence,
    canonical_escalate_command_digest,
    escalation_cause_digest,
    escalation_resource_fingerprint,
)
from agent_org_network.conflict_escalation_registry_snapshot import (
    ConflictEscalationRegistrySnapshot,
)
from agent_org_network.durable_conflict_escalation_evidence import SealedEscalationEvidence


class EscalationApprovalProvider(Protocol):
    def acquire_escalate_approval(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        resource: ResourceRef,
        command_digest: str,
    ) -> ConflictEscalationApprovalEvidence: ...


class CurrentEscalationApprovalEvidenceResolver(Protocol):
    """evidence ID가 아직 current sealed 승인 snapshot인지 확인하는 proof."""

    def resolve_escalation_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> ConflictEscalationApprovalEvidence | None: ...


def escalation_approval_binds(
    evidence: object,
    *,
    resource: ResourceRef,
    command: object,
    cause: SealedEscalationEvidence,
    graph_snapshot: ConflictEscalationRegistrySnapshot,
) -> bool:
    """증거가 이 명령·이 resource·이 escalation 원인·이 graph 선택에 exact 결박됐는지만 본다."""
    if type(evidence) is not ConflictEscalationApprovalEvidence:
        return False
    if type(graph_snapshot) is not ConflictEscalationRegistrySnapshot:
        return False
    try:
        return (
            evidence.status == "granted"
            and evidence.action == ESCALATE_ACTION
            and evidence.command_digest
            == canonical_escalate_command_digest(resource=resource, command=command)
            and evidence.resource_fingerprint == escalation_resource_fingerprint(resource)
            and evidence.escalation_cause_digest == escalation_cause_digest(cause)
            and evidence.graph_selection_digest == graph_snapshot.graph_digest
        )
    except Exception:
        return False


def acquire_escalation_approval(
    principal: AuthenticatedPrincipal,
    action: str,
    resource: ResourceRef,
    command_digest: str,
    *,
    provider: EscalationApprovalProvider,
    resolver: CurrentEscalationApprovalEvidenceResolver,
    command: object,
    cause: SealedEscalationEvidence,
    graph_snapshot: ConflictEscalationRegistrySnapshot,
) -> ConflictEscalationApprovalEvidence | None:
    """취득은 정확히 1회다. 취득 직후 same-evidence 재확인·4결박까지 통과해야 유효하다."""
    try:
        evidence = provider.acquire_escalate_approval(principal, action, resource, command_digest)
        if type(evidence) is not ConflictEscalationApprovalEvidence:
            return None
        current = resolver.resolve_escalation_approval_evidence(
            org_id=resource.org_id, evidence_id=evidence.evidence_id
        )
        valid = (
            type(current) is ConflictEscalationApprovalEvidence
            and current == evidence
            and escalation_approval_binds(
                evidence,
                resource=resource,
                command=command,
                cause=cause,
                graph_snapshot=graph_snapshot,
            )
        )
        return evidence if valid else None
    except Exception:
        return None


def reconfirm_escalation_approval(
    evidence: ConflictEscalationApprovalEvidence,
    *,
    resolver: CurrentEscalationApprovalEvidenceResolver,
    org_id: str,
    resource: ResourceRef,
    command: object,
    cause: SealedEscalationEvidence,
    graph_snapshot: ConflictEscalationRegistrySnapshot,
) -> bool:
    """write 직전 재확인과 replay 재검증이 공유하는 순수 계약. 승인을 재취득하지 않는다."""
    try:
        current = resolver.resolve_escalation_approval_evidence(
            org_id=org_id, evidence_id=evidence.evidence_id
        )
        return (
            type(current) is ConflictEscalationApprovalEvidence
            and current == evidence
            and escalation_approval_binds(
                evidence,
                resource=resource,
                command=command,
                cause=cause,
                graph_snapshot=graph_snapshot,
            )
        )
    except Exception:
        return False
