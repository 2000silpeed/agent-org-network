"""Question Request의 최소 중앙 Approval 경계(P17.6a).

후보 답을 승인 전 초안으로 보관하고 Request를 ``AwaitingApproval``로 옮긴다.
승인·수정승인은 P17.3 Finalization이 소비할 후보만 돌려주며 AnswerRecord를 만들지
않는다. 반려만 사용자 의미가 있는 ``DeclinedRequest``로 종결한다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import RLock
from typing import Annotated, Literal, Protocol, Self, TypeAlias, assert_never

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_org_network.approval_evidence import (
    ApprovalEventRecorder,
    ApprovalEvidenceDependency,
    ApprovalEvidenceIntegrity,
    ApprovalRequestedEvent,
    ApprovalSystemSubject,
    approval_candidate_digest,
)
from agent_org_network.notify import Notification, Notifier
from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingApproval,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.runtime import AnswerMode

Clock: TypeAlias = Callable[[], datetime]
IdFactory: TypeAlias = Callable[[], str]
ApprovalActionKind: TypeAlias = Literal["approve", "approve_with_edit", "reject"]


class ApprovalError(RuntimeError):
    """Approval 경계의 명시적 실패."""


class ApprovalPolicyViolationError(ApprovalError):
    """중앙 정책 부재·오류 또는 안전 요구 완화 시도."""


class ApprovalConfigurationError(ApprovalError):
    """production-style Approval 조립에 필수 중앙 의존성이 없음."""


class ApprovalUnauthorizedError(ApprovalError):
    """중앙 승인 권한이 행위자를 허용하지 않음."""


class ApprovalAuthorizationDependencyError(ApprovalError):
    """중앙 승인 권한 의존성을 일시적으로 확인할 수 없음."""


class ApprovalConcurrencyError(ApprovalError):
    """Request revision 또는 Approval 처분 경쟁이 서로 다름."""


class ApprovalExpiredError(ApprovalConcurrencyError):
    """open ApprovalItem의 처분 시각이 assignment 기한에 도달함."""


class ApprovalItemMismatchError(ApprovalError):
    """같은 request/attempt의 draft·정책 payload가 다름."""


class ApprovalNotFoundError(ApprovalError):
    """Question Request 또는 ApprovalItem을 찾지 못함."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @field_validator("*", mode="after")
    @classmethod
    def _strings_must_be_nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("문자열 값은 비어 있거나 공백일 수 없습니다.")
        return value


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}은 timezone-aware여야 합니다.")
    return value


class AnswerCandidate(_FrozenModel):
    """Runtime 후보 답의 Finalization 전 불변 snapshot."""

    text: str
    sources: tuple[str, ...] = ()
    mode: AnswerMode = "full"
    snapshot_sha: str | None = None

    @field_validator("sources", mode="after")
    @classmethod
    def _sources_must_be_nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not source.strip() for source in value):
            raise ValueError("AnswerCandidate.sources에는 빈 출처를 둘 수 없습니다.")
        return value


class ApproverPrincipal(_FrozenModel):
    """인증 어댑터가 확정해 Approval 경계에 넘기는 승인자 주체."""

    org_id: str
    subject_id: str


class NoApprovalRequired(_FrozenModel):
    kind: Literal["not_required"] = "not_required"
    policy_version: str
    needs_correction_review: bool = Field(
        default=False,
        exclude_if=lambda value: value is False,
    )


class ApprovalRequired(_FrozenModel):
    kind: Literal["required"] = "required"
    approver_id: str
    policy_version: str


ApprovalEvaluation: TypeAlias = NoApprovalRequired | ApprovalRequired


class ApprovalAuthorization(_FrozenModel):
    policy_version: str


class Approve(_FrozenModel):
    kind: Literal["approve"] = "approve"
    by_approver: str


class ApproveWithEdit(_FrozenModel):
    kind: Literal["approve_with_edit"] = "approve_with_edit"
    by_approver: str
    edited_text: str


class Reject(_FrozenModel):
    kind: Literal["reject"] = "reject"
    by_approver: str
    reason_code: str


ApprovalAction: TypeAlias = Annotated[
    Approve | ApproveWithEdit | Reject,
    Field(discriminator="kind"),
]


class ApprovalDraft(_FrozenModel):
    draft_id: str
    request_id: str
    attempt: int = Field(ge=1)
    route: RouteTarget
    candidate: AnswerCandidate
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ApprovalDraft.created_at")


class ApprovalAssignmentGeneration(_FrozenModel):
    """Approval lifecycle status와 분리된 immutable assignment generation snapshot."""

    item_id: str
    org_id: str
    request_id: str
    awaiting_revision: int = Field(ge=1)
    attempt: int = Field(ge=1)
    route: RouteTarget
    draft: ApprovalDraft
    requirement: ApprovalRequired
    created_at: datetime
    due_at: datetime
    approval_round: int = Field(ge=1)
    supersedes_item_id: str | None = None

    @field_validator("created_at", "due_at", mode="after")
    @classmethod
    def _timestamps_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ApprovalAssignmentGeneration timestamp")

    @model_validator(mode="after")
    def _links_must_be_exact(self) -> Self:
        if (
            self.draft.request_id != self.request_id
            or self.draft.attempt != self.attempt
            or self.draft.route != self.route
            or self.created_at < self.draft.created_at
            or self.due_at < self.created_at
        ):
            raise ValueError("Approval assignment generation 링크가 일치하지 않습니다.")
        if self.approval_round == 1:
            if self.supersedes_item_id is not None:
                raise ValueError("첫 Approval generation에는 predecessor를 둘 수 없습니다.")
        elif self.supersedes_item_id is None:
            raise ValueError("후속 Approval generation에는 predecessor가 필요합니다.")
        if self.supersedes_item_id == self.item_id:
            raise ValueError("Approval generation은 자기 자신을 supersede할 수 없습니다.")
        return self

    @classmethod
    def from_item(cls, item: object) -> ApprovalAssignmentGeneration:
        if type(item) is not ApprovalItem:
            raise ValueError("ApprovalItem exact type이 필요합니다.")
        assert isinstance(item, ApprovalItem)
        return cls(
            item_id=item.item_id,
            org_id=item.org_id,
            request_id=item.request_id,
            awaiting_revision=item.awaiting_revision,
            attempt=item.attempt,
            route=item.route,
            draft=item.draft,
            requirement=item.requirement,
            created_at=item.created_at,
            due_at=item.due_at,
            approval_round=item.approval_round,
            supersedes_item_id=item.supersedes_item_id,
        )

    def matches_item(self, item: object) -> bool:
        if type(item) is not ApprovalItem:
            return False
        assert isinstance(item, ApprovalItem)
        return self == ApprovalAssignmentGeneration.from_item(item)


class ReassignExpiredApproval(_FrozenModel):
    """만료 정책이 새 assignment를 선택했다는 sealed 결과."""

    kind: Literal["reassign"] = "reassign"
    assignment_generation: ApprovalAssignmentGeneration
    requirement: ApprovalRequired
    due_at: datetime
    policy_version: str
    authority_version: str
    evidence_ref: str

    @field_validator("due_at", mode="after")
    @classmethod
    def _due_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ReassignExpiredApproval.due_at")


class ApprovalUnavailable(_FrozenModel):
    """유효 승인자와 fallback이 영구히 없다는 sealed 정책 결과."""

    kind: Literal["unavailable"] = "unavailable"
    reason: Literal["no_valid_approver_or_fallback"] = "no_valid_approver_or_fallback"
    assignment_generation: ApprovalAssignmentGeneration
    policy_version: str
    authority_version: str
    evidence_ref: str


ApprovalExpiryResult: TypeAlias = Annotated[
    ReassignExpiredApproval | ApprovalUnavailable,
    Field(discriminator="kind"),
]


class ApprovalReassignmentAuthorization(_FrozenModel):
    """중앙 authorizer가 manual target과 새 assignment를 결박한 결과."""

    kind: Literal["authorized"] = "authorized"
    assignment_generation: ApprovalAssignmentGeneration
    org_id: str
    actor_id: str
    target_approver_id: str
    requirement: ApprovalRequired
    due_at: datetime
    policy_version: str
    authority_version: str
    evidence_ref: str

    @field_validator("due_at", mode="after")
    @classmethod
    def _due_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ApprovalReassignmentAuthorization.due_at")

    @model_validator(mode="after")
    def _links_must_match(self) -> Self:
        if self.org_id != self.assignment_generation.org_id:
            raise ValueError("manual 재지정 authorization 조직이 assignment와 다릅니다.")
        if self.target_approver_id != self.requirement.approver_id:
            raise ValueError("manual 재지정 target과 새 Approval requirement가 다릅니다.")
        return self


class ApprovalReassignmentDenied(_FrozenModel):
    """manual 재지정이 중앙 정책에서 명시적으로 거부된 결과."""

    kind: Literal["denied"] = "denied"
    assignment_generation: ApprovalAssignmentGeneration
    org_id: str
    actor_id: str
    target_approver_id: str
    reason: Literal["not_authorized"] = "not_authorized"

    @model_validator(mode="after")
    def _org_must_match(self) -> Self:
        if self.org_id != self.assignment_generation.org_id:
            raise ValueError("manual 재지정 denial 조직이 assignment와 다릅니다.")
        return self


ApprovalReassignmentAuthorizationResult: TypeAlias = Annotated[
    ApprovalReassignmentAuthorization | ApprovalReassignmentDenied,
    Field(discriminator="kind"),
]


class ApprovedCandidate(_FrozenModel):
    """사람 승인을 거쳐 P17.3 Finalization으로 넘길 후보. 아직 terminal이 아니다."""

    request_id: str
    item_id: str
    expected_revision: int = Field(ge=0)
    attempt: int = Field(ge=1)
    route: RouteTarget
    candidate: AnswerCandidate
    approved_by: str
    approved_at: datetime
    edited: bool
    policy_version: str
    assignment_generation: ApprovalAssignmentGeneration

    @field_validator("approved_at", mode="after")
    @classmethod
    def _approved_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ApprovedCandidate.approved_at")

    @model_validator(mode="after")
    def _assignment_generation_must_match_handoff(self) -> Self:
        generation = self.assignment_generation
        original = generation.draft.candidate
        if self.approved_at < generation.created_at:
            raise ValueError(
                "ApprovedCandidate.approved_at은 assignment generation보다 빠를 수 없습니다."
            )
        if (
            self.item_id != generation.item_id
            or self.request_id != generation.request_id
            or self.expected_revision != generation.awaiting_revision
            or self.attempt != generation.attempt
            or self.route != generation.route
            or self.policy_version != generation.requirement.policy_version
        ):
            raise ValueError("ApprovedCandidate가 immutable assignment generation과 다릅니다.")
        if (
            self.candidate.sources != original.sources
            or self.candidate.mode != original.mode
            or self.candidate.snapshot_sha != original.snapshot_sha
            or (not self.edited and self.candidate.text != original.text)
        ):
            raise ValueError(
                "ApprovedCandidate는 수정승인 본문 외 generation 근거를 바꿀 수 없습니다."
            )
        return self


class ApprovalResolution(_FrozenModel):
    action: ApprovalAction
    approved_candidate: ApprovedCandidate | None = None
    resolved_at: datetime

    @field_validator("resolved_at", mode="after")
    @classmethod
    def _resolved_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ApprovalResolution.resolved_at")

    @model_validator(mode="after")
    def _candidate_matches_action(self) -> Self:
        if isinstance(self.action, Reject):
            if self.approved_candidate is not None:
                raise ValueError("Reject resolution에는 approved candidate를 둘 수 없습니다.")
            return self
        candidate = self.approved_candidate
        if candidate is None:
            raise ValueError("Approve resolution에는 approved candidate가 필요합니다.")
        if candidate.approved_by != self.action.by_approver:
            raise ValueError("Approval action 주체와 approved candidate 주체가 다릅니다.")
        if candidate.approved_at != self.resolved_at:
            raise ValueError("approved_at과 resolved_at은 같아야 합니다.")
        if isinstance(self.action, Approve):
            if candidate.edited:
                raise ValueError("Approve candidate는 edited일 수 없습니다.")
        else:
            if not candidate.edited or candidate.candidate.text != self.action.edited_text:
                raise ValueError("ApproveWithEdit candidate가 수정 명령과 다릅니다.")
        return self


class ApprovalSupersession(_FrozenModel):
    """닫힌 이전 assignment가 가리키는 immutable successor 증거."""

    reason: Literal["expired", "reassigned"]
    successor_item_id: str
    superseded_at: datetime
    policy_version: str | None = None
    authority_version: str | None = None
    evidence_ref: str | None = None
    actor_id: str | None = None
    target_approver_id: str | None = None

    @field_validator("superseded_at", mode="after")
    @classmethod
    def _superseded_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ApprovalSupersession.superseded_at")

    @model_validator(mode="after")
    def _command_basis_is_all_or_none(self) -> Self:
        shared = (
            self.policy_version,
            self.authority_version,
            self.evidence_ref,
            self.target_approver_id,
        )
        if all(value is None for value in shared) and self.actor_id is None:
            return self
        if any(value is None for value in shared):
            raise ValueError("Approval supersession command basis가 불완전합니다.")
        if self.reason == "reassigned" and self.actor_id is None:
            raise ValueError("manual Approval 재지정에는 actor_id가 필요합니다.")
        if self.reason == "expired" and self.actor_id is not None:
            raise ValueError("system expiry supersession에는 actor_id를 둘 수 없습니다.")
        return self


class ApprovalUnavailabilityEvidence(_FrozenModel):
    """사람의 Reject와 분리된 시스템 unavailable 닫힘 증거."""

    decision: ApprovalUnavailable
    unavailable_at: datetime

    @field_validator("unavailable_at", mode="after")
    @classmethod
    def _unavailable_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ApprovalUnavailabilityEvidence.unavailable_at")

    @model_validator(mode="after")
    def _must_be_at_or_after_due(self) -> Self:
        if self.unavailable_at < self.decision.assignment_generation.due_at:
            raise ValueError("Approval unavailable은 assignment 기한 전일 수 없습니다.")
        return self


class ApprovalItem(_FrozenModel):
    item_id: str
    org_id: str
    request_id: str
    awaiting_revision: int = Field(ge=1)
    attempt: int = Field(ge=1)
    route: RouteTarget
    draft: ApprovalDraft
    requirement: ApprovalRequired
    created_at: datetime
    due_at: datetime
    approval_round: int = Field(default=1, ge=1)
    supersedes_item_id: str | None = None
    status: Literal["open", "resolved", "superseded", "unavailable"] = "open"
    resolution: ApprovalResolution | None = None
    supersession: ApprovalSupersession | None = None
    unavailability: ApprovalUnavailabilityEvidence | None = None

    @field_validator("created_at", "due_at", mode="after")
    @classmethod
    def _timestamps_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ApprovalItem timestamp")

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        if (
            self.draft.request_id != self.request_id
            or self.draft.attempt != self.attempt
            or self.draft.route != self.route
        ):
            raise ValueError("ApprovalItem과 ApprovalDraft의 request/attempt/route가 다릅니다.")
        if self.created_at < self.draft.created_at:
            raise ValueError("ApprovalItem.created_at은 ApprovalDraft보다 빠를 수 없습니다.")
        if self.due_at < self.created_at:
            raise ValueError("ApprovalItem.due_at은 created_at보다 빠를 수 없습니다.")
        if self.approval_round == 1:
            if self.supersedes_item_id is not None:
                raise ValueError("첫 Approval round에는 supersedes_item_id를 둘 수 없습니다.")
        elif self.supersedes_item_id is None:
            raise ValueError("후속 Approval round에는 supersedes_item_id가 필요합니다.")
        if self.supersedes_item_id == self.item_id:
            raise ValueError("ApprovalItem은 자기 자신을 supersede할 수 없습니다.")

        if self.status == "open":
            if (
                self.resolution is not None
                or self.supersession is not None
                or self.unavailability is not None
            ):
                raise ValueError("open ApprovalItem에는 닫힘 증거를 둘 수 없습니다.")
        elif self.status == "resolved":
            if (
                self.resolution is None
                or self.supersession is not None
                or self.unavailability is not None
            ):
                raise ValueError("resolved ApprovalItem에는 resolution만 필요합니다.")
        elif self.status == "superseded" and (
            self.resolution is not None
            or self.supersession is None
            or self.unavailability is not None
        ):
            raise ValueError("superseded ApprovalItem에는 supersession만 필요합니다.")
        elif self.status == "unavailable" and (
            self.resolution is not None
            or self.supersession is not None
            or self.unavailability is None
        ):
            raise ValueError("unavailable ApprovalItem에는 unavailability 증거만 필요합니다.")
        if self.resolution is not None:
            self._validate_resolution_links(self.resolution)
        if self.supersession is not None:
            if self.supersession.successor_item_id == self.item_id:
                raise ValueError("Approval supersession successor는 현재 Item과 달라야 합니다.")
            if self.supersession.superseded_at < self.created_at:
                raise ValueError("superseded_at은 ApprovalItem.created_at보다 빠를 수 없습니다.")
            if (
                self.supersession.reason == "expired"
                and self.supersession.superseded_at < self.due_at
            ):
                raise ValueError(
                    "expired superseded_at은 ApprovalItem.due_at보다 빠를 수 없습니다."
                )
        if self.unavailability is not None:
            if not self.unavailability.decision.assignment_generation.matches_item(self):
                raise ValueError("unavailable 증거의 assignment generation이 다릅니다.")
            if self.unavailability.unavailable_at < self.due_at:
                raise ValueError("unavailable_at은 ApprovalItem.due_at보다 빠를 수 없습니다.")
        return self

    def _validate_resolution_links(self, resolution: ApprovalResolution) -> None:
        if resolution.resolved_at < self.created_at:
            raise ValueError("resolved_at은 ApprovalItem.created_at보다 빠를 수 없습니다.")
        if resolution.resolved_at >= self.due_at:
            raise ValueError("resolved_at은 ApprovalItem.due_at보다 빨라야 합니다.")
        candidate = resolution.approved_candidate
        if candidate is None:
            return
        if (
            candidate.request_id != self.request_id
            or candidate.item_id != self.item_id
            or candidate.expected_revision != self.awaiting_revision
            or candidate.attempt != self.attempt
            or candidate.route != self.route
        ):
            raise ValueError(
                "approved candidate의 request/item/attempt/route가 ApprovalItem과 다릅니다."
            )
        if candidate.policy_version != self.requirement.policy_version:
            raise ValueError("approved candidate의 policy version이 요구사항과 다릅니다.")
        if not candidate.assignment_generation.matches_item(self):
            raise ValueError(
                "approved candidate의 assignment generation이 ApprovalItem과 다릅니다."
            )
        action = resolution.action
        if isinstance(action, Approve):
            if candidate.candidate != self.draft.candidate:
                raise ValueError("Approve candidate는 원 ApprovalDraft와 같아야 합니다.")
            return
        if isinstance(action, ApproveWithEdit):
            original = self.draft.candidate
            edited = candidate.candidate
            if (
                edited.text != action.edited_text
                or edited.sources != original.sources
                or edited.mode != original.mode
                or edited.snapshot_sha != original.snapshot_sha
            ):
                raise ValueError("ApproveWithEdit는 본문 외 candidate 근거를 바꿀 수 없습니다.")

    def resolve(
        self,
        *,
        action: ApprovalAction,
        approved_candidate: ApprovedCandidate | None,
        resolved_at: datetime,
    ) -> ApprovalItem:
        if self.status != "open":
            raise ApprovalConcurrencyError("이미 닫힌 ApprovalItem은 resolve할 수 없습니다.")
        return ApprovalItem(
            item_id=self.item_id,
            org_id=self.org_id,
            request_id=self.request_id,
            awaiting_revision=self.awaiting_revision,
            attempt=self.attempt,
            route=self.route,
            draft=self.draft,
            requirement=self.requirement,
            created_at=self.created_at,
            due_at=self.due_at,
            approval_round=self.approval_round,
            supersedes_item_id=self.supersedes_item_id,
            status="resolved",
            resolution=ApprovalResolution(
                action=action,
                approved_candidate=approved_candidate,
                resolved_at=resolved_at,
            ),
        )

    def matches_assignment_generation(self, expected: object) -> bool:
        """lifecycle status와 무관하게 같은 immutable assignment generation인지 판정한다."""
        if type(expected) is ApprovalAssignmentGeneration:
            assert isinstance(expected, ApprovalAssignmentGeneration)
            return expected.matches_item(self)
        if type(expected) is not ApprovalItem:
            return False
        assert isinstance(expected, ApprovalItem)
        return _approval_identity(self) == _approval_identity(expected)

    def supersede(self, supersession: ApprovalSupersession) -> ApprovalItem:
        if self.status != "open":
            raise ApprovalConcurrencyError("이미 닫힌 ApprovalItem은 supersede할 수 없습니다.")
        return ApprovalItem(
            item_id=self.item_id,
            org_id=self.org_id,
            request_id=self.request_id,
            awaiting_revision=self.awaiting_revision,
            attempt=self.attempt,
            route=self.route,
            draft=self.draft,
            requirement=self.requirement,
            created_at=self.created_at,
            due_at=self.due_at,
            approval_round=self.approval_round,
            supersedes_item_id=self.supersedes_item_id,
            status="superseded",
            supersession=supersession,
        )

    def close_unavailable(
        self,
        evidence: ApprovalUnavailabilityEvidence,
    ) -> ApprovalItem:
        if self.status != "open":
            raise ApprovalConcurrencyError(
                "이미 닫힌 ApprovalItem은 unavailable로 닫을 수 없습니다."
            )
        return ApprovalItem(
            item_id=self.item_id,
            org_id=self.org_id,
            request_id=self.request_id,
            awaiting_revision=self.awaiting_revision,
            attempt=self.attempt,
            route=self.route,
            draft=self.draft,
            requirement=self.requirement,
            created_at=self.created_at,
            due_at=self.due_at,
            approval_round=self.approval_round,
            supersedes_item_id=self.supersedes_item_id,
            status="unavailable",
            unavailability=evidence,
        )


class ApprovalPendingSummary(_FrozenModel):
    """승인자 queue에 노출할 수 있는 본문 없는 current Item 요약."""

    item_id: str
    request_id: str
    approval_round: int = Field(ge=1)
    assigned_at: datetime
    due_at: datetime

    @field_validator("assigned_at", "due_at", mode="after")
    @classmethod
    def _timestamps_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "ApprovalPendingSummary timestamp")

    @model_validator(mode="after")
    def _due_at_must_not_precede_assignment(self) -> Self:
        if self.due_at < self.assigned_at:
            raise ValueError("ApprovalPendingSummary.due_at은 assigned_at보다 빠를 수 없습니다.")
        return self


class FinalizationCandidate(_FrozenModel):
    """승인 불필요 후보의 P17.3 handoff. 저장이나 terminal 전이는 아직 없다."""

    request_id: str
    expected_revision: int = Field(ge=0)
    attempt: int = Field(ge=1)
    route: RouteTarget
    candidate: AnswerCandidate
    approval_evaluation: NoApprovalRequired


class ApprovalPending(_FrozenModel):
    """사용자에게 linked item ID나 초안 본문을 노출하지 않는 대기 결과."""

    request_id: str


class ApprovalRejected(_FrozenModel):
    request_id: str
    reason_code: str


ApprovalGateResult: TypeAlias = FinalizationCandidate | ApprovalPending
ApprovalDecisionResult: TypeAlias = ApprovedCandidate | ApprovalRejected


class ApprovalPolicy(Protocol):
    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: AnswerMode,
    ) -> ApprovalEvaluation: ...


class ApprovalAuthorizer(Protocol):
    def authorize(
        self,
        org_id: str,
        designated_approver_id: str,
        actor_id: str,
        action_kind: ApprovalActionKind,
        policy_version: str,
    ) -> ApprovalAuthorization | None: ...


class ApprovalDeadlinePolicy(Protocol):
    def deadline_for(
        self,
        org_id: str,
        state_kind: str,
        started_at: datetime,
    ) -> datetime: ...


class ApprovalExpiryPolicy(Protocol):
    def evaluate(
        self,
        *,
        assignment: ApprovalAssignmentGeneration,
        now: datetime,
    ) -> ApprovalExpiryResult: ...


class ApprovalReassignmentAuthorizer(Protocol):
    def authorize(
        self,
        *,
        assignment: ApprovalAssignmentGeneration,
        principal: ApproverPrincipal,
        target_approver_id: str,
        requested_at: datetime,
    ) -> ApprovalReassignmentAuthorizationResult: ...


class ApprovalStore(Protocol):
    workflow_durability: Literal["ephemeral", "durable"]

    def create_or_get(self, item: ApprovalItem) -> tuple[ApprovalItem, bool]: ...

    def get(self, item_id: str) -> ApprovalItem | None: ...

    def get_by_request_attempt(
        self,
        request_id: str,
        attempt: int,
    ) -> ApprovalItem | None: ...

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None: ...

    def generations(
        self,
        request_id: str,
        attempt: int,
    ) -> list[ApprovalItem]: ...

    def open_for_designated_approver(
        self,
        org_id: str,
        approver_id: str,
    ) -> list[ApprovalPendingSummary]: ...

    def due_open(self, now: datetime, limit: int) -> list[ApprovalItem]: ...

    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]: ...

    def close_unavailable_if_open(
        self,
        item_id: str,
        expected_generation: ApprovalAssignmentGeneration,
        evidence: ApprovalUnavailabilityEvidence,
    ) -> tuple[ApprovalItem, bool]: ...

    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem: ...


def _approval_fingerprint(item: ApprovalItem) -> tuple[object, ...]:
    return (
        item.org_id,
        item.request_id,
        item.awaiting_revision,
        item.attempt,
        item.route,
        item.draft.candidate,
        item.requirement,
        item.approval_round,
        item.supersedes_item_id,
    )


def _approval_identity(item: ApprovalItem) -> tuple[object, ...]:
    return (
        item.item_id,
        item.org_id,
        item.request_id,
        item.awaiting_revision,
        item.attempt,
        item.route,
        item.draft,
        item.requirement,
        item.created_at,
        item.due_at,
        item.approval_round,
        item.supersedes_item_id,
    )


def _canonical_approval_item(item: ApprovalItem) -> ApprovalItem:
    """외부 인스턴스와 backing state 사이의 객체 alias를 끊는다."""
    if type(item) is not ApprovalItem:
        raise ApprovalItemMismatchError("ApprovalItem exact type이 필요합니다.")
    try:
        return ApprovalItem.model_validate(
            item.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception as error:
        raise ApprovalItemMismatchError(
            "ApprovalItem canonical validation에 실패했습니다."
        ) from error


def _canonical_approval_action(action: ApprovalAction) -> ApprovalAction:
    try:
        if type(action) is Approve:
            return Approve.model_validate(
                action.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        if type(action) is ApproveWithEdit:
            return ApproveWithEdit.model_validate(
                action.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        if type(action) is Reject:
            return Reject.model_validate(
                action.model_dump(mode="python", round_trip=True),
                strict=True,
            )
    except Exception as error:
        raise ApprovalConcurrencyError(
            "Approval action canonical validation에 실패했습니다."
        ) from error
    raise ApprovalConcurrencyError("지원하지 않는 Approval action입니다.")


def _canonical_approval_supersession(
    supersession: ApprovalSupersession,
) -> ApprovalSupersession:
    if type(supersession) is not ApprovalSupersession:
        raise ApprovalItemMismatchError("ApprovalSupersession exact type이 필요합니다.")
    try:
        return ApprovalSupersession.model_validate(
            supersession.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception as error:
        raise ApprovalItemMismatchError(
            "Approval supersession canonical validation에 실패했습니다."
        ) from error


def _canonical_assignment_generation(
    generation: ApprovalAssignmentGeneration,
) -> ApprovalAssignmentGeneration:
    if type(generation) is not ApprovalAssignmentGeneration:
        raise ApprovalItemMismatchError("ApprovalAssignmentGeneration exact type이 필요합니다.")
    try:
        return ApprovalAssignmentGeneration.model_validate(
            generation.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception as error:
        raise ApprovalItemMismatchError(
            "Approval assignment generation canonical validation에 실패했습니다."
        ) from error


def _canonical_unavailability_evidence(
    evidence: ApprovalUnavailabilityEvidence,
) -> ApprovalUnavailabilityEvidence:
    if type(evidence) is not ApprovalUnavailabilityEvidence:
        raise ApprovalItemMismatchError("ApprovalUnavailabilityEvidence exact type이 필요합니다.")
    try:
        return ApprovalUnavailabilityEvidence.model_validate(
            evidence.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception as error:
        raise ApprovalItemMismatchError(
            "Approval unavailability evidence canonical validation에 실패했습니다."
        ) from error


def _canonical_approval_pending_summary(
    summary: ApprovalPendingSummary,
) -> ApprovalPendingSummary:
    if type(summary) is not ApprovalPendingSummary:
        raise ApprovalItemMismatchError("ApprovalPendingSummary exact type이 필요합니다.")
    try:
        return ApprovalPendingSummary.model_validate(
            summary.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception as error:
        raise ApprovalItemMismatchError(
            "ApprovalPendingSummary canonical validation에 실패했습니다."
        ) from error


class InMemoryApprovalStore:
    """단일 프로세스 Approval current index와 immutable generation Store."""

    workflow_durability: Literal["ephemeral", "durable"] = "ephemeral"

    def __init__(self) -> None:
        self._latest: dict[str, ApprovalItem] = {}
        self._current_by_request_attempt: dict[tuple[str, int], ApprovalItem] = {}
        self._by_request_attempt_round: dict[tuple[str, int, int], ApprovalItem] = {}
        self._history: list[ApprovalItem] = []
        self._resolving_item_ids: set[str] = set()
        self._lock = RLock()

    @property
    def history(self) -> list[ApprovalItem]:
        with self._lock:
            return [_canonical_approval_item(item) for item in self._history]

    def create_or_get(self, item: ApprovalItem) -> tuple[ApprovalItem, bool]:
        item = _canonical_approval_item(item)
        if item.status != "open" or item.resolution is not None:
            raise ValueError("ApprovalItem 생성은 open 원형만 받습니다.")
        if item.approval_round != 1 or item.supersedes_item_id is not None:
            raise ValueError("첫 ApprovalItem 생성은 round 1 원형만 받습니다.")
        key = (item.request_id, item.attempt)
        with self._lock:
            existing = self._current_by_request_attempt.get(key)
            if existing is not None:
                if _approval_fingerprint(existing) != _approval_fingerprint(item):
                    raise ApprovalItemMismatchError(
                        "같은 request/attempt의 ApprovalItem payload가 다릅니다."
                    )
                return _canonical_approval_item(existing), False
            if item.item_id in self._latest:
                raise ApprovalItemMismatchError(
                    f"ApprovalItem.item_id가 충돌합니다: {item.item_id!r}"
                )
            self._latest[item.item_id] = item
            self._current_by_request_attempt[key] = item
            self._by_request_attempt_round[(item.request_id, item.attempt, 1)] = item
            self._history.append(item)
            return _canonical_approval_item(item), True

    def get(self, item_id: str) -> ApprovalItem | None:
        with self._lock:
            item = self._latest.get(item_id)
            return None if item is None else _canonical_approval_item(item)

    def get_by_request_attempt(
        self,
        request_id: str,
        attempt: int,
    ) -> ApprovalItem | None:
        with self._lock:
            item = self._current_by_request_attempt.get((request_id, attempt))
            return None if item is None else _canonical_approval_item(item)

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        with self._lock:
            item = self._by_request_attempt_round.get((request_id, attempt, approval_round))
            return None if item is None else _canonical_approval_item(item)

    def generations(
        self,
        request_id: str,
        attempt: int,
    ) -> list[ApprovalItem]:
        with self._lock:
            snapshot = [
                item
                for (stored_request_id, stored_attempt, _), item in (
                    self._by_request_attempt_round.items()
                )
                if stored_request_id == request_id and stored_attempt == attempt
            ]
        return [
            _canonical_approval_item(item)
            for item in sorted(
                snapshot,
                key=lambda item: (item.approval_round, item.item_id),
            )
        ]

    def open_for_designated_approver(
        self,
        org_id: str,
        approver_id: str,
    ) -> list[ApprovalPendingSummary]:
        if not org_id.strip() or not approver_id.strip():
            raise ValueError("org_id와 approver_id는 nonblank 문자열이어야 합니다.")
        with self._lock:
            snapshot = [
                item
                for item in self._current_by_request_attempt.values()
                if item.status == "open"
                and item.org_id == org_id
                and item.requirement.approver_id == approver_id
            ]
        return [
            _canonical_approval_pending_summary(
                ApprovalPendingSummary(
                    item_id=item.item_id,
                    request_id=item.request_id,
                    approval_round=item.approval_round,
                    assigned_at=item.created_at,
                    due_at=item.due_at,
                )
            )
            for item in sorted(
                snapshot,
                key=lambda item: (
                    item.created_at,
                    item.request_id,
                    item.attempt,
                    item.approval_round,
                    item.item_id,
                ),
            )
        ]

    def due_open(self, now: datetime, limit: int) -> list[ApprovalItem]:
        now = _require_aware(now, "Approval due scan time")
        if type(limit) is not int or limit <= 0:
            raise ValueError("Approval due scan limit은 양의 정수여야 합니다.")
        with self._lock:
            snapshot = [
                item
                for item in self._current_by_request_attempt.values()
                if item.status == "open" and item.due_at <= now
            ]
        ordered = sorted(
            snapshot,
            key=lambda item: (
                item.due_at,
                item.created_at,
                item.request_id,
                item.attempt,
                item.approval_round,
                item.item_id,
            ),
        )[:limit]
        return [_canonical_approval_item(item) for item in ordered]

    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]:
        supersession = _canonical_approval_supersession(supersession)
        successor = _canonical_approval_item(successor)
        expected = (
            None
            if expected_generation is None
            else _canonical_assignment_generation(expected_generation)
        )
        with self._lock:
            if item_id in self._resolving_item_ids:
                raise ApprovalConcurrencyError(
                    "resolve 중인 ApprovalItem은 supersede할 수 없습니다."
                )
            predecessor = self._latest.get(item_id)
            if predecessor is None:
                raise ApprovalNotFoundError(f"ApprovalItem이 없습니다: {item_id!r}")
            if expected is not None and not expected.matches_item(predecessor):
                raise ApprovalItemMismatchError(
                    "Approval supersede 대상 generation이 expected snapshot과 다릅니다."
                )
            if predecessor.status == "superseded":
                return self._follow_stored_successor(
                    predecessor,
                    supersession,
                    successor,
                )
            if predecessor.status != "open":
                raise ApprovalConcurrencyError("resolved ApprovalItem은 supersede할 수 없습니다.")
            if supersession.reason == "expired" and supersession.superseded_at < predecessor.due_at:
                raise ApprovalConcurrencyError(
                    "ApprovalItem assignment 기한 전에는 expired 처리할 수 없습니다."
                )
            self._validate_successor(predecessor, supersession, successor)
            key = (predecessor.request_id, predecessor.attempt)
            current = self._current_by_request_attempt.get(key)
            if current != predecessor:
                raise ApprovalConcurrencyError(
                    "ApprovalItem이 request/attempt의 현재 세대가 아닙니다."
                )
            if successor.item_id in self._latest:
                raise ApprovalItemMismatchError(
                    f"ApprovalItem.item_id가 충돌합니다: {successor.item_id!r}"
                )
            generation_key = (
                successor.request_id,
                successor.attempt,
                successor.approval_round,
            )
            if generation_key in self._by_request_attempt_round:
                raise ApprovalItemMismatchError(
                    "같은 request/attempt/round의 ApprovalItem이 이미 있습니다."
                )

            superseded = predecessor.supersede(supersession)
            self._latest[predecessor.item_id] = superseded
            self._latest[successor.item_id] = successor
            self._current_by_request_attempt[key] = successor
            self._by_request_attempt_round[
                (predecessor.request_id, predecessor.attempt, predecessor.approval_round)
            ] = superseded
            self._by_request_attempt_round[generation_key] = successor
            self._history.extend((superseded, successor))
            return _canonical_approval_item(successor), True

    def _follow_stored_successor(
        self,
        predecessor: ApprovalItem,
        supersession: ApprovalSupersession,
        proposed: ApprovalItem,
    ) -> tuple[ApprovalItem, bool]:
        stored_evidence = predecessor.supersession
        if stored_evidence is None:
            raise ApprovalItemMismatchError("superseded ApprovalItem의 successor 증거가 없습니다.")
        stored = self._latest.get(stored_evidence.successor_item_id)
        if (
            stored_evidence != supersession
            or stored is None
            or _approval_identity(stored) != _approval_identity(proposed)
        ):
            raise ApprovalConcurrencyError(
                "ApprovalItem이 다른 successor payload로 이미 superseded됐습니다."
            )
        return _canonical_approval_item(stored), False

    @staticmethod
    def _validate_successor(
        predecessor: ApprovalItem,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
    ) -> None:
        if (
            successor.status != "open"
            or successor.resolution is not None
            or successor.supersession is not None
            or successor.item_id != supersession.successor_item_id
            or successor.org_id != predecessor.org_id
            or successor.request_id != predecessor.request_id
            or successor.awaiting_revision != predecessor.awaiting_revision + 1
            or successor.attempt != predecessor.attempt
            or successor.route != predecessor.route
            or successor.draft != predecessor.draft
            or successor.created_at != supersession.superseded_at
            or successor.approval_round != predecessor.approval_round + 1
            or successor.supersedes_item_id != predecessor.item_id
            or supersession.target_approver_id is not None
            and supersession.target_approver_id != successor.requirement.approver_id
        ):
            raise ApprovalItemMismatchError(
                "Approval successor가 이전 Item의 generation 계약과 다릅니다."
            )

    def close_unavailable_if_open(
        self,
        item_id: str,
        expected_generation: ApprovalAssignmentGeneration,
        evidence: ApprovalUnavailabilityEvidence,
    ) -> tuple[ApprovalItem, bool]:
        expected = _canonical_assignment_generation(expected_generation)
        evidence = _canonical_unavailability_evidence(evidence)
        with self._lock:
            if item_id in self._resolving_item_ids:
                raise ApprovalConcurrencyError(
                    "resolve 중인 ApprovalItem은 unavailable로 닫을 수 없습니다."
                )
            current = self._latest.get(item_id)
            if current is None:
                raise ApprovalNotFoundError(f"ApprovalItem이 없습니다: {item_id!r}")
            if not expected.matches_item(current):
                raise ApprovalItemMismatchError(
                    "Approval unavailable 대상 generation이 expected snapshot과 다릅니다."
                )
            if current.status == "unavailable":
                if current.unavailability != evidence:
                    raise ApprovalConcurrencyError(
                        "ApprovalItem이 다른 unavailable 증거로 이미 닫혔습니다."
                    )
                return _canonical_approval_item(current), False
            if current.status != "open":
                raise ApprovalConcurrencyError(
                    "이미 resolved 또는 superseded된 ApprovalItem은 unavailable로 닫을 수 없습니다."
                )
            key = (current.request_id, current.attempt)
            round_key = (current.request_id, current.attempt, current.approval_round)
            if (
                self._current_by_request_attempt.get(key) != current
                or self._by_request_attempt_round.get(round_key) != current
            ):
                raise ApprovalConcurrencyError(
                    "unavailable 대상이 current/round index와 exact-link되지 않습니다."
                )
            if not evidence.decision.assignment_generation.matches_item(current):
                raise ApprovalItemMismatchError(
                    "Approval unavailable 증거 generation이 현재 Item과 다릅니다."
                )
            if evidence.unavailable_at < current.due_at:
                raise ApprovalConcurrencyError(
                    "ApprovalItem assignment 기한 전에는 unavailable로 닫을 수 없습니다."
                )
            closed = current.close_unavailable(evidence)
            self._latest[item_id] = closed
            self._current_by_request_attempt[key] = closed
            self._by_request_attempt_round[round_key] = closed
            self._history.append(closed)
            return _canonical_approval_item(closed), True

    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem:
        action = _canonical_approval_action(action)
        with self._lock:
            if item_id in self._resolving_item_ids:
                raise ApprovalConcurrencyError(
                    "같은 ApprovalItem resolve transition에 재진입할 수 없습니다."
                )
            current = self._latest.get(item_id)
            if current is None:
                raise ApprovalNotFoundError(f"ApprovalItem이 없습니다: {item_id!r}")
            if current.status == "superseded":
                raise ApprovalConcurrencyError("superseded ApprovalItem은 resolve할 수 없습니다.")
            if current.status == "resolved":
                assert current.resolution is not None
                if current.resolution.action != action:
                    raise ApprovalConcurrencyError("이미 다른 Approval 처분으로 resolved됐습니다.")
                return _canonical_approval_item(current)
            if current.status != "open":
                raise ApprovalConcurrencyError(
                    "open 상태가 아닌 ApprovalItem은 resolve할 수 없습니다."
                )
            key = (current.request_id, current.attempt)
            indexed = self._current_by_request_attempt.get(key)
            indexed_round = self._by_request_attempt_round.get(
                (current.request_id, current.attempt, current.approval_round)
            )
            if indexed != current or indexed_round != current:
                raise ApprovalConcurrencyError(
                    "resolve 대상이 current/round index와 exact-link되지 않습니다."
                )
            self._resolving_item_ids.add(item_id)
            try:
                resolved = _canonical_approval_item(transition(_canonical_approval_item(current)))
            finally:
                self._resolving_item_ids.remove(item_id)
            if (
                resolved.item_id != item_id
                or _approval_identity(resolved) != _approval_identity(current)
                or resolved.status != "resolved"
                or resolved.resolution is None
                or resolved.resolution.action != action
                or resolved.resolution.resolved_at >= current.due_at
            ):
                raise ApprovalConcurrencyError("Approval resolve transition이 올바르지 않습니다.")
            if (
                self._latest.get(item_id) != current
                or self._current_by_request_attempt.get(key) != current
                or self._by_request_attempt_round.get(
                    (current.request_id, current.attempt, current.approval_round)
                )
                != current
            ):
                raise ApprovalConcurrencyError(
                    "Approval resolve transition 중 backing state가 바뀌었습니다."
                )
            self._latest[item_id] = resolved
            self._current_by_request_attempt[key] = resolved
            self._by_request_attempt_round[
                (resolved.request_id, resolved.attempt, resolved.approval_round)
            ] = resolved
            self._history.append(resolved)
            return _canonical_approval_item(resolved)


class ApprovalBoundary:
    """후보 답 staging과 사람 Approval 처분을 Request 수명에 연결한다."""

    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        approvals: ApprovalStore,
        policy: ApprovalPolicy | None,
        authorizer: ApprovalAuthorizer | None,
        deadline_policy: ApprovalDeadlinePolicy,
        draft_id_factory: IdFactory,
        item_id_factory: IdFactory,
        clock: Clock,
        production_style: bool = False,
        evidence_recorder: ApprovalEventRecorder | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        if production_style and (policy is None or authorizer is None):
            raise ApprovalConfigurationError(
                "production-style Approval에는 중앙 ApprovalPolicy와 "
                "ApprovalAuthorizer가 모두 필요합니다."
            )
        self._requests = requests
        self._approvals = approvals
        self._policy = policy
        self._authorizer = authorizer
        self._deadline_policy = deadline_policy
        self._draft_id_factory = draft_id_factory
        self._item_id_factory = item_id_factory
        self._clock = clock
        self._evidence_recorder = evidence_recorder
        self._notifier = notifier

    def matches_dependencies(
        self,
        *,
        requests: QuestionRequestStore,
        approvals: ApprovalStore,
        policy: ApprovalPolicy,
        authorizer: ApprovalAuthorizer,
    ) -> bool:
        """composition이 Approval 단일 원천 identity를 검증하는 손잡이."""
        return (
            self._requests is requests
            and self._approvals is approvals
            and self._policy is policy
            and self._authorizer is authorizer
        )

    def matches_evidence_dependencies(
        self,
        *,
        evidence_recorder: ApprovalEventRecorder | None,
        notifier: Notifier | None,
    ) -> bool:
        """composition이 사건 journal과 push service의 exact identity를 검증한다."""
        return self._evidence_recorder is evidence_recorder and self._notifier is notifier

    def gate_candidate(
        self,
        request_id: str,
        *,
        expected_revision: object,
        candidate: AnswerCandidate,
    ) -> ApprovalGateResult:
        candidate = self._canonical_candidate(candidate)
        request = self._requests.get(request_id)
        if request is None:
            raise ApprovalNotFoundError(f"Question Request가 없습니다: {request_id!r}")
        revision = self._strict_revision(expected_revision)
        if request.revision == revision + 1 and isinstance(request.state, AwaitingApproval):
            item = self._approvals.get_by_request_attempt(
                request.request_id,
                request.state.attempt,
            )
            if item is not None:
                item = self._canonical_item(item)
            if (
                item is None
                or item.item_id != request.state.draft_ref
                or item.org_id != request.org_id
                or item.request_id != request.request_id
                or item.awaiting_revision != request.revision
                or item.attempt != request.state.attempt
                or item.route != request.state.route
                or item.due_at != request.state.handling.due_at
                or item.draft.candidate != candidate
            ):
                raise ApprovalItemMismatchError(
                    "AwaitingApproval과 재시도 candidate가 일치하지 않습니다."
                )
            return self._finish_requested(request.request_id, expected_candidate=candidate)
        if request.revision != revision:
            raise ApprovalConcurrencyError("Question Request revision이 예상값과 다릅니다.")

        state = request.state
        if isinstance(state, (ReadyToDispatch, AwaitingAnswer)):
            route = state.route
            attempt = state.attempt
        else:
            raise ApprovalConcurrencyError(
                "후보 답은 ReadyToDispatch/AwaitingAnswer에서만 gate할 수 있습니다."
            )

        evaluation = self._evaluate_policy(request, route, candidate)
        safety_required = route.requires_approval or candidate.mode == "draft_only"
        if isinstance(evaluation, NoApprovalRequired):
            if safety_required:
                raise ApprovalPolicyViolationError(
                    "route 또는 draft_only가 요구한 Approval을 정책이 완화할 수 없습니다."
                )
            return FinalizationCandidate(
                request_id=request.request_id,
                expected_revision=request.revision,
                attempt=attempt,
                route=route,
                candidate=candidate,
                approval_evaluation=evaluation,
            )
        now = self._gate_time(request)
        due_at = self._approval_deadline(request.org_id, now)
        draft = ApprovalDraft(
            draft_id=self._draft_id_factory(),
            request_id=request.request_id,
            attempt=attempt,
            route=route,
            candidate=candidate,
            created_at=now,
        )
        proposed = ApprovalItem(
            item_id=self._item_id_factory(),
            org_id=request.org_id,
            request_id=request.request_id,
            awaiting_revision=request.revision + 1,
            attempt=attempt,
            route=route,
            draft=draft,
            requirement=evaluation,
            created_at=now,
            due_at=due_at,
        )
        item, _ = self._approvals.create_or_get(proposed)
        item = self._canonical_item(item)
        if item.status != "open" or _approval_fingerprint(item) != _approval_fingerprint(proposed):
            raise ApprovalItemMismatchError(
                "ApprovalStore가 다른 request/attempt/candidate/requirement를 반환했습니다."
            )
        assignment_at = item.created_at
        if assignment_at < request.updated_at or assignment_at > now:
            raise ApprovalItemMismatchError(
                "Approval assignment 시각이 Request와 retry clock 사이에 있지 않습니다."
            )
        updated = request.transition(
            AwaitingApproval(
                route=route,
                attempt=attempt,
                draft_ref=item.item_id,
                handling=HandlingAssignment(
                    kind="approval_item",
                    ref=item.item_id,
                    due_at=item.due_at,
                ),
            ),
            clock=lambda: assignment_at,
        )
        if not self._requests.compare_and_set(
            request.request_id,
            request.revision,
            request,
            updated,
        ):
            winner = self._requests.get(request.request_id)
            if (
                winner is not None
                and isinstance(winner.state, AwaitingApproval)
                and winner.state.draft_ref == item.item_id
                and winner.state.route == route
                and winner.state.attempt == attempt
                and winner.state.handling.due_at == item.due_at
            ):
                return self._finish_requested(
                    request.request_id,
                    expected_candidate=candidate,
                )
            raise ApprovalConcurrencyError("AwaitingApproval CAS가 다른 전이와 충돌했습니다.")
        return self._finish_requested(request.request_id, expected_candidate=candidate)

    def ensure_requested(self, request_id: str) -> ApprovalPending:
        """이미 확정된 AwaitingApproval의 requested 증거를 멱등 복구한다.

        recorder·notifier가 모두 없으면 기존 조립처와 동일하게 no-op이다.
        증거나 알림이 조립되면 exact-link를 검증한다. 현재 assignment가 후속
        generation이면 round 1 사건만 복구하고, 지난 최초 배정자에게 알림을
        다시 보내지 않는다.
        """
        return self._finish_requested(request_id, expected_candidate=None)

    def _finish_requested(
        self,
        request_id: str,
        *,
        expected_candidate: AnswerCandidate | None,
    ) -> ApprovalPending:
        if self._evidence_recorder is None and self._notifier is None:
            # S4 조립을 사용하지 않는 기존 경계는 추가 port read 없이 동작한다.
            return ApprovalPending(request_id=request_id)
        request, current, initial = self._requested_evidence_snapshot(request_id)
        if expected_candidate is not None and initial.draft.candidate != expected_candidate:
            raise ApprovalItemMismatchError(
                "AwaitingApproval과 재시도 candidate가 일치하지 않습니다."
            )

        recorder = self._evidence_recorder
        if recorder is not None:
            try:
                event = ApprovalRequestedEvent(
                    org_id=initial.org_id,
                    request_id=initial.request_id,
                    item_id=initial.item_id,
                    draft_id=initial.draft.draft_id,
                    approval_round=initial.approval_round,
                    subject=ApprovalSystemSubject(system_id="approval_boundary"),
                    candidate_digest=approval_candidate_digest(initial.draft.candidate),
                    policy_version=initial.requirement.policy_version,
                    occurred_at=initial.created_at,
                )
                recorder.record(event)
            except ApprovalEvidenceIntegrity:
                raise
            except ApprovalEvidenceDependency:
                raise
            except Exception as error:
                raise ApprovalEvidenceDependency() from error

        notifier = self._notifier
        if notifier is not None and current.approval_round == 1 and current.status == "open":
            try:
                notifier.notify(
                    Notification(
                        recipient_id=initial.requirement.approver_id,
                        kind="approval_assignment_ready",
                        subject_ref=initial.item_id,
                        created_at=initial.created_at,
                    )
                )
            except Exception:
                # push는 pull assignment·requested 사건의 성공 여부를 바꾸지 않는다.
                pass
        return ApprovalPending(request_id=request.request_id)

    def _requested_evidence_snapshot(
        self,
        request_id: str,
    ) -> tuple[QuestionRequest, ApprovalItem, ApprovalItem]:
        try:
            raw_request = self._requests.get(request_id)
        except Exception as error:
            raise ApprovalEvidenceDependency() from error
        if raw_request is None:
            raise ApprovalNotFoundError(f"Question Request가 없습니다: {request_id!r}")
        try:
            if type(raw_request) is not QuestionRequest:
                raise ApprovalItemMismatchError("Question Request 저장값이 exact type이 아닙니다.")
            request = QuestionRequest.model_validate(
                raw_request.model_dump(mode="python", round_trip=True, warnings="error"),
                strict=True,
            )
        except ApprovalItemMismatchError:
            raise
        except Exception as error:
            raise ApprovalItemMismatchError(
                "Question Request 저장값의 불변식이 손상됐습니다."
            ) from error
        if request.request_id != request_id or not isinstance(request.state, AwaitingApproval):
            raise ApprovalConcurrencyError(
                "requested 증거는 현재 AwaitingApproval Request에서만 확정할 수 있습니다."
            )

        state = request.state
        try:
            raw_direct = self._approvals.get(state.draft_ref)
            raw_current = self._approvals.get_by_request_attempt(
                request.request_id,
                state.attempt,
            )
            raw_generations = self._approvals.generations(
                request.request_id,
                state.attempt,
            )
        except Exception as error:
            raise ApprovalEvidenceDependency() from error
        if raw_direct is None or raw_current is None or type(raw_generations) is not list:
            raise ApprovalItemMismatchError(
                "ApprovalItem direct/current/generations snapshot이 없습니다."
            )
        direct = self._canonical_item(raw_direct)
        current = self._canonical_item(raw_current)
        generations = [self._canonical_item(item) for item in raw_generations]
        if (
            direct != current
            or current.item_id != state.draft_ref
            or current.status == "superseded"
            or not generations
            or generations[-1] != current
            or len({item.item_id for item in generations}) != len(generations)
            or tuple(item.approval_round for item in generations)
            != tuple(range(1, current.approval_round + 1))
        ):
            raise ApprovalItemMismatchError(
                "ApprovalItem current generation lineage가 exact-link되지 않습니다."
            )

        for item in generations:
            try:
                raw_item = self._approvals.get(item.item_id)
                raw_round = self._approvals.get_by_request_attempt_round(
                    item.request_id,
                    item.attempt,
                    item.approval_round,
                )
            except Exception as error:
                raise ApprovalEvidenceDependency() from error
            if raw_item is None or raw_round is None:
                raise ApprovalItemMismatchError("ApprovalItem direct/round snapshot이 없습니다.")
            if self._canonical_item(raw_item) != item or self._canonical_item(raw_round) != item:
                raise ApprovalItemMismatchError(
                    "ApprovalItem direct/round snapshot이 exact-link되지 않습니다."
                )

        for earlier, later in zip(generations[:-1], generations[1:], strict=True):
            if (
                earlier.status != "superseded"
                or earlier.supersession is None
                or earlier.supersession.successor_item_id != later.item_id
                or later.supersedes_item_id != earlier.item_id
                or later.org_id != earlier.org_id
                or later.request_id != earlier.request_id
                or later.attempt != earlier.attempt
                or later.route != earlier.route
                or later.draft != earlier.draft
                or later.awaiting_revision != earlier.awaiting_revision + 1
                or later.created_at != earlier.supersession.superseded_at
                or earlier.supersession.target_approver_id is not None
                and earlier.supersession.target_approver_id != later.requirement.approver_id
            ):
                raise ApprovalItemMismatchError(
                    "ApprovalItem generation lineage가 exact-link되지 않습니다."
                )

        if (
            request.org_id != current.org_id
            or request.revision != current.awaiting_revision
            or request.updated_at != current.created_at
            or state.draft_ref != current.item_id
            or state.handling.ref != current.item_id
            or state.route != current.route
            or state.attempt != current.attempt
            or state.handling.due_at != current.due_at
        ):
            raise ApprovalItemMismatchError(
                "AwaitingApproval Request와 current ApprovalItem이 exact-link되지 않습니다."
            )
        initial = generations[0]
        if initial.approval_round != 1 or initial.supersedes_item_id is not None:
            raise ApprovalItemMismatchError(
                "Approval requested 최초 generation이 올바르지 않습니다."
            )
        return request, current, initial

    def decide(
        self,
        item_id: str,
        principal: ApproverPrincipal,
        action: ApprovalAction,
        *,
        expected_item: ApprovalItem | None = None,
    ) -> ApprovalDecisionResult:
        principal, action = self._canonical_decision_input(principal, action)
        expected = None if expected_item is None else self._canonical_item(expected_item)
        if expected is not None and expected.item_id != item_id:
            raise ApprovalItemMismatchError("Approval expected snapshot과 조회 item ID가 다릅니다.")
        item = self._approvals.get(item_id)
        if item is None:
            raise ApprovalNotFoundError(f"ApprovalItem이 없습니다: {item_id!r}")
        item = self._canonical_item(item)
        if item.item_id != item_id:
            raise ApprovalItemMismatchError("ApprovalStore 조회 키와 반환 Item ID가 다릅니다.")
        expected_generation = expected if expected is not None else item
        if not item.matches_assignment_generation(expected_generation):
            raise ApprovalItemMismatchError(
                "ApprovalItem immutable generation이 expected snapshot과 달라졌습니다."
            )
        if item.status == "superseded":
            raise ApprovalConcurrencyError("superseded ApprovalItem은 처분할 수 없습니다.")
        self._require_exact_item_indexes(item)
        request = self._requests.get(item.request_id)
        if request is None:
            raise ApprovalNotFoundError(f"Question Request가 없습니다: {item.request_id!r}")
        if item.org_id != request.org_id:
            raise ApprovalItemMismatchError("ApprovalItem과 Question Request 조직이 다릅니다.")
        self._validate_principal(request, principal, action)
        if item.status == "resolved":
            assert item.resolution is not None
            if item.resolution.action != action:
                raise ApprovalConcurrencyError("이미 다른 Approval 처분이 적용됐습니다.")
            if item.resolution.resolved_at >= item.due_at:
                raise ApprovalItemMismatchError(
                    "Approval resolution이 assignment 기한 안의 처분이 아닙니다."
                )
            return self._finish_resolved(item)

        if (
            not isinstance(request.state, AwaitingApproval)
            or request.state.draft_ref != item.item_id
            or request.revision != item.awaiting_revision
            or request.state.route != item.route
            or request.state.attempt != item.attempt
            or request.state.handling.due_at != item.due_at
        ):
            raise ApprovalConcurrencyError(
                "ApprovalItem과 현재 Question Request 상태가 일치하지 않습니다."
            )
        self._authorize(request, item, principal, action)

        def resolve_expected(current: ApprovalItem) -> ApprovalItem:
            current = self._canonical_item(current)
            if not current.matches_assignment_generation(expected_generation):
                raise ApprovalItemMismatchError(
                    "Approval resolve 대상이 expected immutable generation과 달라졌습니다."
                )
            # Store는 이 callback을 같은 Item lock 안에서 실행한다. precheck 뒤
            # clock 경계를 지난 처분이 승인으로 새지 않도록 여기서 시각을 읽는다.
            now = self._decision_time(request, current)
            if now >= current.due_at:
                raise ApprovalExpiredError(
                    "ApprovalItem assignment 기한에 도달해 새 처분을 받을 수 없습니다."
                )
            approved_candidate: ApprovedCandidate | None
            match action:
                case Approve():
                    approved_candidate = self._approved_candidate(
                        request,
                        current,
                        action.by_approver,
                        current.draft.candidate,
                        now,
                        edited=False,
                    )
                case ApproveWithEdit():
                    approved_candidate = self._approved_candidate(
                        request,
                        current,
                        action.by_approver,
                        current.draft.candidate.model_copy(update={"text": action.edited_text}),
                        now,
                        edited=True,
                    )
                case Reject():
                    approved_candidate = None
                case _ as never:
                    assert_never(never)
            return current.resolve(
                action=action,
                approved_candidate=approved_candidate,
                resolved_at=now,
            )

        resolved = self._approvals.resolve_if_open(
            item.item_id,
            action,
            resolve_expected,
        )
        resolved = self._canonical_item(resolved)
        if (
            not resolved.matches_assignment_generation(expected_generation)
            or resolved.resolution is None
            or resolved.resolution.action != action
            or resolved.resolution.resolved_at >= resolved.due_at
        ):
            raise ApprovalItemMismatchError(
                "ApprovalStore가 요청한 Item/처분과 다른 resolve 결과를 반환했습니다."
            )
        self._require_exact_item_indexes(resolved)
        return self._finish_resolved(resolved)

    def _finish_resolved(self, item: ApprovalItem) -> ApprovalDecisionResult:
        resolution = item.resolution
        assert resolution is not None
        if resolution.approved_candidate is not None:
            return resolution.approved_candidate
        action = resolution.action
        if not isinstance(action, Reject):
            raise ApprovalConcurrencyError("resolved ApprovalItem 결과가 손상됐습니다.")
        request = self._requests.get(item.request_id)
        if request is None:
            raise ApprovalNotFoundError(f"Question Request가 없습니다: {item.request_id!r}")
        if request.org_id != item.org_id:
            raise ApprovalItemMismatchError("ApprovalItem과 Question Request 조직이 다릅니다.")
        if isinstance(request.state, DeclinedRequest):
            if (
                request.state.reason_code != action.reason_code
                or request.revision != item.awaiting_revision + 1
                or request.updated_at != resolution.resolved_at
            ):
                raise ApprovalConcurrencyError(
                    "Question Request가 이 Reject resolution의 exact 결과가 아닙니다."
                )
        elif isinstance(request.state, AwaitingApproval) and (
            request.state.draft_ref == item.item_id
            and request.state.route == item.route
            and request.state.attempt == item.attempt
            and request.state.handling.due_at == item.due_at
            and request.revision == item.awaiting_revision
        ):
            declined = request.transition(
                DeclinedRequest(reason_code=action.reason_code),
                clock=lambda: resolution.resolved_at,
            )
            if not self._requests.compare_and_set(
                request.request_id,
                request.revision,
                request,
                declined,
            ):
                winner = self._requests.get(request.request_id)
                if not (
                    winner is not None
                    and winner.org_id == item.org_id
                    and isinstance(winner.state, DeclinedRequest)
                    and winner.state.reason_code == action.reason_code
                    and winner.revision == item.awaiting_revision + 1
                    and winner.updated_at == resolution.resolved_at
                ):
                    raise ApprovalConcurrencyError(
                        "Reject 뒤 Question Request CAS가 다른 전이와 충돌했습니다."
                    )
        else:
            raise ApprovalConcurrencyError(
                "Reject resolution과 Question Request 상태가 일치하지 않습니다."
            )
        return ApprovalRejected(
            request_id=item.request_id,
            reason_code=action.reason_code,
        )

    def _require_exact_item_indexes(self, item: ApprovalItem) -> None:
        try:
            raw_item = self._approvals.get(item.item_id)
            raw_current = self._approvals.get_by_request_attempt(
                item.request_id,
                item.attempt,
            )
            raw_round = self._approvals.get_by_request_attempt_round(
                item.request_id,
                item.attempt,
                item.approval_round,
            )
            if raw_item is None or raw_current is None or raw_round is None:
                raise ApprovalItemMismatchError(
                    "ApprovalItem get/current/round snapshot이 없습니다."
                )
            direct = self._canonical_item(raw_item)
            current = self._canonical_item(raw_current)
            round_item = self._canonical_item(raw_round)
        except ApprovalItemMismatchError:
            raise
        except Exception as error:
            raise ApprovalItemMismatchError(
                "ApprovalItem current/round index를 검증할 수 없습니다."
            ) from error
        if direct != item or current != item or round_item != item:
            raise ApprovalItemMismatchError(
                "ApprovalItem get/current/round snapshot이 exact-link되지 않습니다."
            )

    def _evaluate_policy(
        self,
        request: QuestionRequest,
        route: RouteTarget,
        candidate: AnswerCandidate,
    ) -> ApprovalEvaluation:
        """정적 Protocol을 위반한 외부 구현도 호출부에서 fail-closed 검증한다."""
        if self._policy is None:
            raise ApprovalPolicyViolationError("중앙 ApprovalPolicy가 없습니다.")
        try:
            raw = self._policy.evaluate(
                request.org_id,
                route,
                candidate.mode,
            )
            return self._canonical_evaluation(raw)
        except ApprovalPolicyViolationError:
            raise
        except Exception as error:
            raise ApprovalPolicyViolationError(
                "ApprovalPolicy 결과 검증에 실패했습니다."
            ) from error

    @staticmethod
    def _canonical_evaluation(raw: object) -> ApprovalEvaluation:
        if isinstance(raw, NoApprovalRequired):
            return NoApprovalRequired(
                kind=raw.kind,
                policy_version=raw.policy_version,
                needs_correction_review=raw.needs_correction_review,
            )
        if isinstance(raw, ApprovalRequired):
            return ApprovalRequired(
                kind=raw.kind,
                approver_id=raw.approver_id,
                policy_version=raw.policy_version,
            )
        raise ApprovalPolicyViolationError("알 수 없는 ApprovalPolicy 결과입니다.")

    def _authorize(
        self,
        request: QuestionRequest,
        item: ApprovalItem,
        principal: ApproverPrincipal,
        action: ApprovalAction,
    ) -> None:
        if self._authorizer is None:
            raise ApprovalUnauthorizedError("중앙 ApprovalAuthorizer가 없습니다.")
        try:
            raw_grant = self._authorizer.authorize(
                request.org_id,
                item.requirement.approver_id,
                principal.subject_id,
                action.kind,
                item.requirement.policy_version,
            )
        except Exception as error:
            raise ApprovalAuthorizationDependencyError(
                "Approval 권한 확인에 실패했습니다."
            ) from error
        try:
            grant = (
                None
                if raw_grant is None
                else ApprovalAuthorization(
                    policy_version=raw_grant.policy_version,
                )
            )
        except Exception as error:
            raise ApprovalUnauthorizedError("Approval 권한 결과가 유효하지 않습니다.") from error
        if grant is None or grant.policy_version != item.requirement.policy_version:
            raise ApprovalUnauthorizedError("Approval 행위가 중앙 정책에서 허용되지 않았습니다.")

    @staticmethod
    def _canonical_candidate(candidate: AnswerCandidate) -> AnswerCandidate:
        try:
            return AnswerCandidate(
                text=candidate.text,
                sources=candidate.sources,
                mode=candidate.mode,
                snapshot_sha=candidate.snapshot_sha,
            )
        except Exception as error:
            raise ApprovalPolicyViolationError(
                "Runtime AnswerCandidate가 유효하지 않습니다."
            ) from error

    @staticmethod
    def _canonical_decision_input(
        principal: ApproverPrincipal,
        action: ApprovalAction,
    ) -> tuple[ApproverPrincipal, ApprovalAction]:
        try:
            canonical_principal = ApproverPrincipal(
                org_id=principal.org_id,
                subject_id=principal.subject_id,
            )
            match action:
                case Approve():
                    canonical_action: ApprovalAction = Approve(
                        kind=action.kind,
                        by_approver=action.by_approver,
                    )
                case ApproveWithEdit():
                    canonical_action = ApproveWithEdit(
                        kind=action.kind,
                        by_approver=action.by_approver,
                        edited_text=action.edited_text,
                    )
                case Reject():
                    canonical_action = Reject(
                        kind=action.kind,
                        by_approver=action.by_approver,
                        reason_code=action.reason_code,
                    )
                case _ as never:
                    assert_never(never)
            return canonical_principal, canonical_action
        except Exception as error:
            raise ApprovalUnauthorizedError(
                "Approval principal 또는 action이 유효하지 않습니다."
            ) from error

    @staticmethod
    def _canonical_item(item: ApprovalItem) -> ApprovalItem:
        try:
            return _canonical_approval_item(item)
        except Exception as error:
            raise ApprovalItemMismatchError(
                "ApprovalItem 저장값의 불변식이 손상됐습니다."
            ) from error

    @staticmethod
    def _validate_principal(
        request: QuestionRequest,
        principal: ApproverPrincipal,
        action: ApprovalAction,
    ) -> None:
        if principal.org_id != request.org_id or principal.subject_id != action.by_approver:
            raise ApprovalUnauthorizedError(
                "인증된 승인자 주체가 Question Request 조직 또는 명령 주체와 다릅니다."
            )

    @staticmethod
    def _approved_candidate(
        request: QuestionRequest,
        item: ApprovalItem,
        approved_by: str,
        candidate: AnswerCandidate,
        approved_at: datetime,
        *,
        edited: bool,
    ) -> ApprovedCandidate:
        return ApprovedCandidate(
            request_id=request.request_id,
            item_id=item.item_id,
            expected_revision=request.revision,
            attempt=item.attempt,
            route=item.route,
            candidate=candidate,
            approved_by=approved_by,
            approved_at=approved_at,
            edited=edited,
            policy_version=item.requirement.policy_version,
            assignment_generation=ApprovalAssignmentGeneration.from_item(item),
        )

    @staticmethod
    def _strict_revision(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ApprovalConcurrencyError("expected_revision은 0 이상의 정수여야 합니다.")
        return value

    def _now(self) -> datetime:
        try:
            return _require_aware(self._clock(), "Approval clock")
        except Exception as error:
            raise ApprovalPolicyViolationError("Approval clock이 유효하지 않습니다.") from error

    def _decision_time(
        self,
        request: QuestionRequest,
        item: ApprovalItem,
    ) -> datetime:
        now = self._now()
        if now < request.updated_at or now < item.created_at or now < item.draft.created_at:
            raise ApprovalPolicyViolationError(
                "Approval 처분 시각은 Request 또는 ApprovalItem보다 역행할 수 없습니다."
            )
        return now

    def _gate_time(self, request: QuestionRequest) -> datetime:
        now = self._now()
        if now < request.updated_at:
            raise ApprovalPolicyViolationError(
                "Approval 대기 진입 시각은 Question Request보다 역행할 수 없습니다."
            )
        return now

    def _approval_deadline(self, org_id: str, started_at: datetime) -> datetime:
        try:
            due_at = _require_aware(
                self._deadline_policy.deadline_for(
                    org_id,
                    "awaiting_approval",
                    started_at,
                ),
                "Approval deadline",
            )
            if due_at < started_at:
                raise ValueError("Approval deadline이 시작 시각보다 빠릅니다.")
            return due_at
        except Exception as error:
            raise ApprovalPolicyViolationError(
                "Approval deadline 정책이 유효하지 않습니다."
            ) from error
