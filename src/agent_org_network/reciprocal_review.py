"""P18 S1a reciprocal-review immutable domain shapes (ADR 0047)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def _strict_strings(cls, value: object, info: ValidationInfo) -> object:
        name = info.field_name or ""
        if isinstance(value, str) and not value.strip():
            raise ValueError("문자열은 nonblank여야 합니다.")
        if (
            name.endswith(("_id", "_ref"))
            and value is not None
            and (type(value) is not str or _OPAQUE.fullmatch(value) is None)
        ):
            raise ValueError("ID/ref는 opaque해야 합니다.")
        if value is not None and name.endswith(("_digest", "_sha256", "_hash")) and (
            type(value) is not str or _SHA.fullmatch(value) is None
        ):
            raise ValueError("digest/hash는 SHA-256이어야 합니다.")
        if isinstance(value, datetime) and (
            value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
            or value.microsecond % 1000
        ):
            raise ValueError("시각은 canonical UTC milliseconds여야 합니다.")
        return value


class HumanPrincipal(_Frozen):
    kind: Literal["human"] = "human"
    org_id: str
    subject_id: str
    authenticated_at: datetime
    authn_context_digest: str

    @field_validator("org_id", "subject_id")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("principal ID는 opaque해야 합니다.")
        return value

    @field_validator("authn_context_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("digest는 SHA-256이어야 합니다.")
        return value


class AiReviewerPrincipal(_Frozen):
    kind: Literal["ai_reviewer"] = "ai_reviewer"
    org_id: str
    reviewer_id: str
    model_execution_ref: str
    deployment_digest: str
    rubric_digest: str

    @field_validator("org_id", "reviewer_id", "model_execution_ref")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("AI reviewer field는 opaque해야 합니다.")
        return value

    @field_validator("deployment_digest", "rubric_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("digest는 SHA-256이어야 합니다.")
        return value


ReviewPrincipal: TypeAlias = Annotated[
    HumanPrincipal | AiReviewerPrincipal, Field(discriminator="kind")
]


class EffectiveAuthorshipProvenance(_Frozen):
    kind: Literal["human", "ai", "mixed", "unknown"]
    digest: str

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("digest는 SHA-256이어야 합니다.")
        return value


class AuthorshipEvent(_Frozen):
    event_id: str
    org_id: str
    revision_id: str
    contributor: ReviewPrincipal
    content_digest: str
    created_at: datetime

    @field_validator("event_id", "org_id", "revision_id")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("event identifier는 opaque해야 합니다.")
        return value

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("content digest는 SHA-256이어야 합니다.")
        return value

    @model_validator(mode="after")
    def _same_org(self) -> AuthorshipEvent:
        if self.contributor.org_id != self.org_id:
            raise ValueError("authorship event는 조직을 혼합할 수 없습니다.")
        return self


class ArtifactRevision(_Frozen):
    org_id: str
    artifact_id: str
    revision_id: str
    revision_no: int = Field(ge=1)
    parent_revision_id: str | None
    kind: Literal["knowledge", "answer", "template", "eval_case"]
    content_ref: str
    content_sha256: str
    lineage_event_ids: tuple[str, ...] = Field(min_length=1)
    authenticated_provenance_events: tuple[AuthorshipEvent, ...] = Field(min_length=1)
    provenance_resolution_receipt_ids: tuple[str, ...] = ()
    effective_provenance: EffectiveAuthorshipProvenance
    data_classification: Literal["public", "internal", "confidential", "restricted"]
    data_boundary_snapshot_ref: str
    data_boundary_digest: str
    declassification_receipt_id: str | None = None
    created_at: datetime
    schema_version: int = Field(ge=1)

    @field_validator(
        "org_id", "artifact_id", "revision_id", "content_ref", "data_boundary_snapshot_ref"
    )
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("revision reference는 opaque immutable ref여야 합니다.")
        return value

    @field_validator("content_sha256", "data_boundary_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("digest는 SHA-256이어야 합니다.")
        return value

    @model_validator(mode="after")
    def _provenance_is_not_contradictory(self) -> ArtifactRevision:
        kinds = {event.contributor.kind for event in self.authenticated_provenance_events}
        expected = "mixed" if len(kinds) > 1 else "human" if kinds == {"human"} else "ai"
        if self.effective_provenance.kind not in (expected, "unknown"):
            raise ValueError("effective provenance가 authenticated event와 모순됩니다.")
        if any(
            event.org_id != self.org_id or event.revision_id != self.revision_id
            for event in self.authenticated_provenance_events
        ):
            raise ValueError("revision provenance event가 exact revision/org에 결박돼야 합니다.")
        return self


class RegisterArtifactRevision(_Frozen):
    """Body-free registration intent; provenance, boundary, and policy are server-derived."""

    receipt_id: str
    artifact_id: str
    revision_id: str
    kind: Literal["knowledge", "answer", "template", "eval_case"]
    content_ref: str
    content_sha256: str
    parent_revision_id: str | None = None
    provenance_event_id: str
    audit_id: str
    outbox_id: str

    @field_validator(
        "receipt_id",
        "artifact_id",
        "revision_id",
        "content_ref",
        "parent_revision_id",
        "provenance_event_id",
        "audit_id",
        "outbox_id",
    )
    @classmethod
    def _opaque(cls, value: str | None) -> str | None:
        if value is not None and _OPAQUE.fullmatch(value) is None:
            raise ValueError("등록 command reference는 opaque해야 합니다.")
        return value

    @field_validator("content_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("content digest는 SHA-256이어야 합니다.")
        return value


class RegisteredArtifactRevision(_Frozen):
    org_id: str
    revision_id: str
    cycle_id: str
    receipt_id: str
    command_digest: str

    @field_validator("command_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("command digest는 SHA-256이어야 합니다.")
        return value


class ReviewRequirement(_Frozen):
    requirement_id: str
    org_id: str
    cycle_id: str
    reviewer_kind: Literal["ai", "human"]
    completion_rule: Literal["all", "any", "quorum"]
    required_count: int = Field(ge=1)
    independence_rule: str
    rubric_version: str
    deadline_at: datetime
    risk_class: str
    waivable: bool


class ReviewOpen(_Frozen):
    kind: Literal["review_open"] = "review_open"


class AwaitingHumanDisposition(_Frozen):
    kind: Literal["awaiting_human_disposition"] = "awaiting_human_disposition"


class BindingReady(_Frozen):
    kind: Literal["binding_ready"] = "binding_ready"
    action: Literal["approve_revision", "request_changes", "reject_revision"]
    human_disposition_receipt: HumanDispositionReceipt

    @model_validator(mode="after")
    def _exact_revision_disposition(self) -> BindingReady:
        disposition = self.human_disposition_receipt.disposition
        if not isinstance(disposition, (ApproveRevision, RequestChanges, RejectRevision)):
            raise ValueError("BindingReady에는 revision disposition receipt가 필요합니다.")
        if self.action != disposition.kind:
            raise ValueError("BindingReady action과 human receipt disposition이 일치해야 합니다.")
        return self


class BindingPending(_Frozen):
    kind: Literal["binding_pending"] = "binding_pending"


class Bound(_Frozen):
    kind: Literal["bound"] = "bound"
    outcome: Literal["approved", "changes_requested", "rejected"]
    source_receipt_id: str


class Superseded(_Frozen):
    kind: Literal["superseded"] = "superseded"
    reason: str


ReviewCycleState: TypeAlias = Annotated[
    ReviewOpen | AwaitingHumanDisposition | BindingReady | BindingPending | Bound | Superseded,
    Field(discriminator="kind"),
]


class ReviewRun(_Frozen):
    review_run_id: str
    org_id: str
    requirement_id: str
    run_attempt: int = Field(ge=1)
    lease_epoch: int = Field(ge=1)
    lease_token_hash: str
    state: Literal["queued", "leased", "recorded", "expired"]
    created_at: datetime

    @field_validator("lease_token_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("lease token은 hash만 보관합니다.")
        return value


class ReviewFinding(_Frozen):
    finding_id: str
    severity: Literal["info", "warning", "blocking"]
    criterion_ref: str = "criterion"
    evidence_digest: str
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered_span(self) -> ReviewFinding:
        if self.evidence_end <= self.evidence_start:
            raise ValueError("finding evidence span은 non-empty여야 합니다.")
        return self


class AiAdvisoryFindingBatch(_Frozen):
    batch_id: str
    org_id: str
    review_run_id: str
    model_execution_ref: str
    prompt_digest: str
    rubric_digest: str
    input_digest: str
    signature: str
    signature_algorithm: str = "ed25519"
    signing_key_id: str = "ai-signing-key"
    signed_payload_digest: str = "0" * 64
    findings: tuple[ReviewFinding, ...]
    batch_digest: str = ""
    created_at: datetime

    @model_validator(mode="after")
    def _canonical_batch_digest(self) -> AiAdvisoryFindingBatch:
        payload = {
            "batch_id": self.batch_id,
            "org_id": self.org_id,
            "review_run_id": self.review_run_id,
            "model_execution_ref": self.model_execution_ref,
            "prompt_digest": self.prompt_digest,
            "rubric_digest": self.rubric_digest,
            "input_digest": self.input_digest,
            "signature": self.signature,
            "findings": [finding.model_dump(mode="json") for finding in self.findings],
            "created_at": self.created_at.isoformat(timespec="milliseconds"),
        }
        import hashlib
        import json

        expected = hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        if self.batch_digest and self.batch_digest != expected:
            raise ValueError("AI advisory batch digest가 canonical payload와 일치해야 합니다.")
        return self


class RecordAiAdvisoryBatch(_Frozen):
    """Lease-fenced, advisory-only persistence command for a completed AI review run."""

    receipt_id: str
    audit_id: str
    outbox_id: str
    principal: AiReviewerPrincipal
    lease_epoch: int = Field(ge=1)
    lease_token: str
    batch: AiAdvisoryFindingBatch

    @model_validator(mode="after")
    def _same_org_and_run(self) -> RecordAiAdvisoryBatch:
        if self.principal.org_id != self.batch.org_id:
            raise ValueError("AI batch principal과 batch 조직이 일치해야 합니다.")
        return self


class AcceptFinding(_Frozen):
    kind: Literal["accept_finding"] = "accept_finding"
    finding_id: str


class RejectFinding(_Frozen):
    kind: Literal["reject_finding"] = "reject_finding"
    finding_id: str


class DeferFinding(_Frozen):
    kind: Literal["defer_finding"] = "defer_finding"
    finding_id: str
    assignee_subject_id: str
    due_at: datetime
    sla_ref: str


class ApproveRevision(_Frozen):
    kind: Literal["approve_revision"] = "approve_revision"


class RequestChanges(_Frozen):
    kind: Literal["request_changes"] = "request_changes"


class RejectRevision(_Frozen):
    kind: Literal["reject_revision"] = "reject_revision"


FindingDisposition: TypeAlias = Annotated[
    AcceptFinding | RejectFinding | DeferFinding, Field(discriminator="kind")
]
RevisionDisposition: TypeAlias = Annotated[
    ApproveRevision | RequestChanges | RejectRevision, Field(discriminator="kind")
]


class HumanDispositionReceipt(_Frozen):
    receipt_id: str
    org_id: str
    cycle_id: str
    principal: HumanPrincipal
    disposition: FindingDisposition | RevisionDisposition
    command_digest: str
    created_at: datetime

    @model_validator(mode="after")
    def _human_is_bound_to_org(self) -> HumanDispositionReceipt:
        if self.principal.org_id != self.org_id:
            raise ValueError("사람 처분 영수증은 조직을 혼합할 수 없습니다.")
        return self


class SubmitHumanDisposition(_Frozen):
    """Authenticated human's body-free intent to make a review cycle binding-ready."""

    receipt_id: str
    audit_id: str
    outbox_id: str
    cycle_id: str
    expected_cycle_revision: int = Field(ge=1)
    idempotency_key: str
    disposition: RevisionDisposition


class SubmittedHumanDisposition(_Frozen):
    org_id: str
    cycle_id: str
    receipt_id: str
    command_digest: str
    cycle_revision: int = Field(ge=1)
    cycle_state: Literal["binding_ready"] = "binding_ready"
    action: Literal["approve_revision", "request_changes", "reject_revision"]
    created_at: datetime

    @field_validator("command_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("command digest는 SHA-256이어야 합니다.")
        return value


class SubmitAiMixedHumanDisposition(_Frozen):
    """Body-free v5 intent; the v4 snapshot and evidence are server-derived."""

    receipt_id: str
    audit_id: str
    outbox_id: str
    cycle_id: str
    expected_upstream_revision: int = Field(ge=1)
    idempotency_key: str
    disposition: RevisionDisposition


class SubmittedAiMixedHumanDisposition(_Frozen):
    org_id: str
    cycle_id: str
    receipt_id: str
    command_digest: str
    upstream_revision: int = Field(ge=1)
    result_revision: int = Field(ge=1)
    cycle_state: Literal["binding_ready"] = "binding_ready"
    action: Literal["approve_revision", "request_changes", "reject_revision"]
    created_at: datetime

    @field_validator("command_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("command digest는 SHA-256이어야 합니다.")
        return value


class HumanReviewConclusion(_Frozen):
    """S1b.5a deliberately accepts only a finding-free human terminal."""

    finding_count: Literal[0] = 0
    content_digest: str
    rubric_digest: str
    input_digest: str

    @field_validator("content_digest", "rubric_digest", "input_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA.fullmatch(value) is None:
            raise ValueError("terminal digest는 SHA-256이어야 합니다.")
        return value


class RecordHumanReviewTerminal(_Frozen):
    receipt_id: str
    audit_id: str
    outbox_id: str
    idempotency_key: str
    cycle_id: str
    requirement_id: str
    review_run_id: str
    lease_epoch: int = Field(ge=1)
    lease_token: str
    conclusion: HumanReviewConclusion


class HumanReviewTerminalReceipt(_Frozen):
    receipt_id: str
    org_id: str
    cycle_id: str
    requirement_id: str
    review_run_id: str
    principal: HumanPrincipal
    command_digest: str
    created_at: datetime


class RecordedHumanReviewTerminal(_Frozen):
    org_id: str
    receipt_id: str
    cycle_id: str
    requirement_id: str
    review_run_id: str
    command_digest: str
    cycle_revision: int = Field(ge=1)
    cycle_state: Literal["review_open", "awaiting_human_disposition"]
    created_at: datetime


class ReviewerAssignment(_Frozen):
    assignment_id: str
    org_id: str
    cycle_id: str
    requirement_id: str
    reviewer: HumanPrincipal
    ordinal: int = Field(ge=1)
    assignment_digest: str


class AssignmentReviewRun(_Frozen):
    assignment_run_id: str
    org_id: str
    assignment_id: str
    attempt: int = Field(ge=1)
    lease_epoch: int = Field(ge=1)
    lease_token_hash: str
    state: Literal["queued", "leased", "recorded", "expired"]


class FindingFreeHumanTerminal(_Frozen):
    receipt_id: str
    org_id: str
    assignment_id: str
    assignment_run_id: str
    finding_count: Literal[0] = 0
    content_digest: str
    rubric_digest: str
    input_digest: str


# P18 S1c.  These are deliberately body-free: the source adapter derives the
# effective boundary and authority at the DB-time intent linearization point.
class SourceBoundaryEnforcementPlan(_Frozen):
    plan_id: str
    source_ref: str
    expected_source_revision: str
    revision_id: str
    content_digest: str
    boundary_digest: str
    enforcement_mode: Literal["gateway", "native"]
    expires_at: datetime | None = None


class SourceBoundaryDriftActionAuthorization(_Frozen):
    authorization_id: str
    source_ref: str
    expected_source_revision: str
    boundary_digest: str
    action: Literal["source_deny_reads", "source_unpublish"]
    expires_at: datetime


class SourceBindingCapability(_Frozen):
    expected_source_revision_cas: Literal[True] = True
    semantic_idempotency: Literal[True] = True
    worker_fencing: Literal[True] = True
    stable_exact_readback: Literal[True] = True
    continuous_enforcement: Literal[True] = True
    every_read_attestation: Literal[True] = True


class SourceBindingAttestation(_Frozen):
    """Trusted source-adapter proof; a public capability self-claim is insufficient."""

    key_id: str
    issued_at: datetime
    expires_at: datetime
    capability: SourceBindingCapability
    plan: SourceBoundaryEnforcementPlan
    drift_authorization: SourceBoundaryDriftActionAuthorization
    signature: str

    @model_validator(mode="after")
    def _fresh_interval(self) -> SourceBindingAttestation:
        if self.expires_at <= self.issued_at:
            raise ValueError("source binding attestation expiry가 issued_at 뒤여야 합니다.")
        return self


class SourceBindingAuthorityReceipt(_Frozen):
    """Central-authority, short-lived grant for one source-binding intent."""

    key_id: str
    org_id: str
    source_ref: str
    expected_source_revision: str
    revision_id: str
    content_digest: str
    data_classification: Literal["public", "internal", "confidential", "restricted"]
    boundary_snapshot_ref: str
    boundary_digest: str
    declassification_receipt_id: str | None = None
    issued_at: datetime
    expires_at: datetime
    signature: str

    @model_validator(mode="after")
    def _fresh_interval(self) -> SourceBindingAuthorityReceipt:
        if self.expires_at <= self.issued_at:
            raise ValueError("source binding authority expiry가 issued_at 뒤여야 합니다.")
        return self


class CreateSourceBindingIntent(_Frozen):
    receipt_id: str
    audit_id: str
    outbox_id: str
    idempotency_key: str
    cycle_id: str
    expected_upstream_revision: int = Field(ge=1)
    source_ref: str
    # v6 is closed by ADR 0059; keep the historical request DTO decodable so
    # legacy evidence can be inspected without manufacturing a receipt.
    authority_receipt: SourceBindingAuthorityReceipt | None = None


class CreatedSourceBindingIntent(_Frozen):
    org_id: str
    cycle_id: str
    receipt_id: str
    command_digest: str
    binding_generation: int = Field(ge=1)
    cycle_state: Literal["binding_pending"] = "binding_pending"
    created_at: datetime


class CreateSourceBindingIntentV7(_Frozen):
    """Public body-free request.  Authority/capability envelopes are never caller input."""

    receipt_id: str
    audit_id: str
    outbox_id: str
    idempotency_key: str
    cycle_id: str
    expected_upstream_revision: int = Field(ge=1)
    source_ref: str


class IntegrationEnforcementProfileV7(_Frozen):
    integration_id: str
    profile_version: str
    profile_digest: str
    enforcement_plan_digest: str
    expected_source_revision_cas: Literal[True] = True
    semantic_idempotency: Literal[True] = True
    worker_fencing: Literal[True] = True
    stable_exact_readback: Literal[True] = True
    continuous_enforcement: Literal[True] = True
    every_read_attestation: Literal[True] = True


class SourceResourceRefV7(_Frozen):
    org_id: str
    resource_kind: Literal["source"]
    resource_id: str


class SourceBindingAuthorizationEnvelopeV7(_Frozen):
    format_version: Literal["v7"]
    receipt_id: str
    key_id: str
    org_id: str
    source_ref: str
    source_resource: SourceResourceRefV7
    expected_source_revision: str
    revision_id: str
    content_digest: str
    data_classification: Literal["public", "internal", "confidential", "restricted"]
    boundary_snapshot_ref: str
    boundary_digest: str
    declassification_receipt_id: str | None = None
    declassification_receipt_digest: str | None = None
    declassification_expires_at: datetime | None = None
    action: Literal["reciprocal_review.source_bind"]
    policy_version: str
    policy_digest: str
    principal_grant_digest: str
    integration_id: str
    integration_profile_version: str
    integration_profile_digest: str
    enforcement_plan_digest: str
    every_read_mode: Literal["gateway", "native"]
    drift_action: Literal["source_deny_reads", "source_unpublish"]
    drift_action_digest: str
    drift_expires_at: datetime
    issued_at: datetime
    expires_at: datetime
    payload_digest: str
    intent_semantic_digest: str
    signature: str
