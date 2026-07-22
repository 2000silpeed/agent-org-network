"""S4.3c.1 sealed HITL 승인 증거 값 객체(ADR 0065·ADR 0050 §11).

`conflict.escalate` 승인 증거는 credential 도메인과 무관하다. 이 모듈은
`durable_credentials`를 import하지 않고, canonical command/resource
fingerprint를 conflict-escalation 도메인 로컬로 재현한다(4필드 hash 중복이
cross-domain import보다 싸다). c.1은 소비 가능한 `granted` 스냅샷 하나만
정의하며, 만료/취소/재승인 lifecycle union은 S4.3c.2/c.3로 이월한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Final, Literal

from agent_org_network.central_authority import ResourceRef
from agent_org_network.durable_conflict_escalation_evidence import (
    CandidateRegistryChanged,
    DivergentVotes,
    SealedEscalationEvidence,
)

ESCALATE_ACTION: Final = "conflict.escalate"

_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ConflictEscalationApprovalEvidence:
    """이미 검증된 escalation 사람 승인 증거의 secret-free snapshot(granted 변이 하나).

    `command_digest`·`resource_fingerprint`에 더해 `escalation_cause_digest`·
    `graph_selection_digest`가 이 증거를 이 명령·이 Case·이 escalation
    원인(S4.3b sealed evidence)·이 graph 선택(S4.3c.0 snapshot)에 exact
    결박한다.
    """

    evidence_id: str
    status: Literal["granted"]
    action: Literal["conflict.escalate"]
    command_digest: str
    resource_fingerprint: str
    escalation_cause_digest: str
    graph_selection_digest: str

    def __post_init__(self) -> None:
        if (
            not self.evidence_id.strip()
            or self.status != "granted"
            or self.action != ESCALATE_ACTION
        ):
            raise ValueError("Conflict escalation approval evidence가 올바르지 않습니다.")
        if any(
            _DIGEST_PATTERN.fullmatch(value) is None
            for value in (
                self.command_digest,
                self.resource_fingerprint,
                self.escalation_cause_digest,
                self.graph_selection_digest,
            )
        ):
            raise ValueError("Conflict escalation approval evidence digest가 올바르지 않습니다.")


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def escalation_resource_fingerprint(resource: ResourceRef) -> str:
    return _sha(
        {
            "org_id": resource.org_id,
            "kind": resource.kind,
            "resource_id": resource.resource_id,
            "owner_subject_id": resource.owner_subject_id,
        }
    )


def canonical_escalate_command_digest(*, resource: ResourceRef, command: object) -> str:
    """승인 증거와 (후속) receipt가 공유할 결정론 command digest."""
    return _sha(
        {
            "action": ESCALATE_ACTION,
            "resource_fingerprint": escalation_resource_fingerprint(resource),
            "command": command,
        }
    )


def escalation_cause_digest(cause: SealedEscalationEvidence) -> str:
    """S4.3b sealed escalation evidence를 안정 필드만으로 canonical 결박한다.

    `evaluated_at`은 비결정 timestamp라 제외한다.
    """
    if type(cause) is DivergentVotes:
        kind = "DivergentVotes"
        variant: dict[str, object] = {"candidate_owner_count": cause.candidate_owner_count}
    elif type(cause) is CandidateRegistryChanged:
        kind = "CandidateRegistryChanged"
        variant = {"current_candidate_snapshot_sha256": cause.current_candidate_snapshot_sha256}
    else:
        raise TypeError(
            "sealed escalation evidence(DivergentVotes|CandidateRegistryChanged)가 필요합니다."
        )
    return _sha(
        {
            "kind": kind,
            "org_ref": cause.org_ref,
            "conflict_ref": cause.conflict_ref,
            "request_ref": cause.request_ref,
            "awaiting_revision": cause.awaiting_revision,
            "concurrence_round": cause.concurrence_round,
            "candidate_snapshot_sha256": cause.candidate_snapshot_sha256,
            "baseline_sha256": cause.baseline_sha256,
            "candidate_claim_sha256": cause.candidate_claim_sha256,
            "vote_set_sha256": cause.vote_set_sha256,
            **variant,
        }
    )
