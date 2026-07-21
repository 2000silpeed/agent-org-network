"""P17.6b Approval 처리함의 안전한 조회·처분 application.

목록은 본문 없는 current Item 요약만 제공한다. 상세의 질문·초안 후보는 같은 조직의
현재 지정 승인자에게만 보이며, 외부에 구분 가능한 존재 여부 신호를 남기지 않는다.
처분은 중앙 Approval 경계, 원자 Finalization, exact-read, terminal publish를 순서대로
연결한다. 보장은 조립된 저장 구현의 범위이며 durable exactly-once를 주장하지 않는다.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Condition, RLock, get_ident
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_org_network.answer_finalization import (
    AnswerCompletion,
    AnswerFinalizationError,
    CompletionConcurrencyError,
    CompletionEvidenceError,
    CompletionIdCollisionError,
    CompletionNotFoundError,
    HumanApprovalEvidence,
    IncompleteCompletionStateError,
    InvalidCompletionHandoffError,
    QuestionCompletionReader,
    QuestionCompletionUnitOfWork,
    canonical_completion_bundle,
)
from agent_org_network.approval import (
    ApprovalAuthorizationDependencyError,
    ApprovalAssignmentGeneration,
    ApprovalBoundary,
    ApprovalConcurrencyError,
    ApprovalExpiryPolicy,
    ApprovalItem,
    ApprovalItemMismatchError,
    ApprovalNotFoundError,
    ApprovalPendingSummary,
    ApprovalPolicyViolationError,
    ApprovalReassignmentAuthorization,
    ApprovalReassignmentAuthorizer,
    ApprovalReassignmentDenied,
    ApprovalRejected,
    ApprovalStore,
    ApprovalSupersession,
    ApprovalUnavailable,
    ApprovalUnavailabilityEvidence,
    ApprovalUnauthorizedError,
    Approve,
    ApprovedCandidate,
    ApproveWithEdit,
    ApproverPrincipal,
    AnswerCandidate,
    ReassignExpiredApproval,
    Reject,
)
from agent_org_network.approval_evidence import (
    ApprovalApprovedEvent,
    ApprovalApprovedWithEditEvent,
    ApprovalEvent,
    ApprovalEventRecorder,
    ApprovalEvidenceDependency,
    ApprovalEvidenceIntegrity,
    ApprovalExpiredEvent,
    ApprovalHumanSubject,
    ApprovalReassignedEvent,
    ApprovalRejectedEvent,
    ApprovalRetentionEligibleEvent,
    ApprovalSystemSubject,
    ApprovalUnavailableEvent,
    approval_action_digest,
    approval_candidate_digest,
    approval_event_digest,
)
from agent_org_network.approval_retention import (
    ApprovalAnsweredTerminalEvidence,
    ApprovalDeclinedTerminalEvidence,
    ApprovalDraftRetentionDecision,
    ApprovalDraftRetentionEvaluated,
    ApprovalDraftRetentionPolicy,
    ApprovalDraftRetentionStatus,
    ApprovalDraftRetained,
    ApprovalDraftTerminalEvidence,
    ApprovalUnavailableTerminalEvidence,
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.governance_authorization import (
    authorize_and_verify,
    canonical_authenticated_principal,
)
from agent_org_network.notify import Notification, Notifier
from agent_org_network.p17_manager_disposition import (
    QuestionTerminalPublisher,
    TerminalAlreadyPublished,
    TerminalDeferred,
    TerminalDelivery,
    TerminalPublished,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingApproval,
    DeclinedRequest,
    FailedRequest,
    QuestionRequest,
    QuestionRequestStore,
)


class ApprovalOperationsError(RuntimeError):
    """Approval read application의 field-free 기본 오류."""


class ApprovalOperationsNotFoundOrDenied(ApprovalOperationsError):
    """Item 부재와 접근 거부를 외부에서 구분할 수 없는 결과."""

    retryable = False


class ApprovalOperationsInvalid(ApprovalOperationsError):
    """공개 intent 또는 principal shape가 유효하지 않음."""

    retryable = False


class ApprovalOperationsConflict(ApprovalOperationsError):
    """같은 Item에 다른 처분이 이미 적용됐거나 경쟁에서 패함."""

    retryable = False


class ApprovalOperationsDependency(ApprovalOperationsError):
    """처분 의존성을 일시적으로 확인하거나 실행할 수 없음."""

    retryable = True


class ApprovalOperationsAuthorizationUnavailable(ApprovalOperationsDependency):
    """중앙 권한 의존성 장애를 원인 없이 고정한 일시 실패."""


class ApprovalOperationsIntegrityError(ApprovalOperationsError):
    """Store·Request·summary exact-link 손상. payload를 반사하지 않는다."""

    retryable = False


class _LifecycleReentryError(ApprovalOperationsConflict):
    """같은 process-local lifecycle 임계 구역의 동기 재진입."""


class _RetentionReentrySignal(RuntimeError):
    """외부 policy 오류와 구분하는 process-local 동기 재진입 신호."""


# S1 공개 이름은 유지하면서 S2의 짧은 오류 taxonomy도 같은 exact type을 가리킨다.
ApprovalOperationsIntegrity = ApprovalOperationsIntegrityError

ApprovalOperationsPrincipal: TypeAlias = ApproverPrincipal | AuthenticatedPrincipal


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


class ApprovalPendingDetail(_FrozenModel):
    """인증된 현재 지정 승인자에게만 제공하는 Approval 상세."""

    item_id: str
    request_id: str
    approval_round: int = Field(ge=1)
    assigned_at: datetime
    due_at: datetime
    question: str
    draft_id: str
    candidate: AnswerCandidate

    @field_validator("assigned_at", "due_at", mode="after")
    @classmethod
    def _timestamps_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ApprovalPendingDetail 시간은 timezone-aware여야 합니다.")
        return value

    @model_validator(mode="after")
    def _due_at_must_not_precede_assignment(self) -> Self:
        if self.due_at < self.assigned_at:
            raise ValueError("ApprovalPendingDetail.due_at은 assigned_at보다 빠를 수 없습니다.")
        return self


class ApproveIntent(_FrozenModel):
    """행위자 필드를 받지 않는 승인 intent."""

    kind: Literal["approve"] = "approve"


class ApproveWithEditIntent(_FrozenModel):
    """행위자 필드를 받지 않는 수정승인 intent."""

    kind: Literal["approve_with_edit"] = "approve_with_edit"
    edited_text: str


class RejectIntent(_FrozenModel):
    """행위자 필드를 받지 않는 명시적 반려 intent."""

    kind: Literal["reject"] = "reject"
    reason_code: str


ApprovalDecisionIntent: TypeAlias = Annotated[
    ApproveIntent | ApproveWithEditIntent | RejectIntent,
    Field(discriminator="kind"),
]


class ApprovalAnswered(_FrozenModel):
    """답 본문을 포함하지 않는 승인·Finalization 결과."""

    item_id: str
    approval_round: int = Field(ge=1)
    request_id: str
    record_id: str
    action: Literal["approve", "approve_with_edit"]
    delivery: TerminalDelivery


class ApprovalDeclined(_FrozenModel):
    """질문·초안 본문을 포함하지 않는 반려 결과."""

    item_id: str
    approval_round: int = Field(ge=1)
    request_id: str
    reason_code: str
    delivery: TerminalDelivery


ApprovalOperationsDecision: TypeAlias = ApprovalAnswered | ApprovalDeclined


class ManualApprovalReassignmentTarget(_FrozenModel):
    """actor·기한·정책을 받지 않는 manual 재지정 target."""

    approver_id: str


class ApprovalReassigned(_FrozenModel):
    """본문을 싣지 않는 새 Approval assignment 결과."""

    predecessor_item_id: str
    successor_item_id: str
    request_id: str
    approval_round: int = Field(ge=2)
    due_at: datetime
    reason: Literal["reassigned", "expired"]

    @field_validator("due_at", mode="after")
    @classmethod
    def _due_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ApprovalReassigned.due_at은 timezone-aware여야 합니다.")
        return value


class ApprovalMadeUnavailable(_FrozenModel):
    """본문을 싣지 않는 approval_unavailable terminal 결과."""

    item_id: str
    request_id: str
    error_code: Literal["approval_unavailable"] = "approval_unavailable"
    delivery: TerminalDelivery


ApprovalLifecycleOutcome: TypeAlias = ApprovalReassigned | ApprovalMadeUnavailable


class ApprovalLifecycleFailure(_FrozenModel):
    """배치의 다른 Item 진행을 막지 않는 본문 없는 per-Item 실패."""

    item_id: str
    request_id: str
    error_code: Literal["conflict", "dependency", "integrity"]
    retryable: bool

    @model_validator(mode="after")
    def _retryability_matches_error(self) -> Self:
        if self.retryable != (self.error_code == "dependency"):
            raise ValueError("Approval lifecycle 실패의 retryable 분류가 다릅니다.")
        return self


ApprovalLifecycleScanResult: TypeAlias = ApprovalLifecycleOutcome | ApprovalLifecycleFailure


class _ReassignmentWork(_FrozenModel):
    kind: Literal["reassignment"] = "reassignment"
    source: Literal["manual", "expiry"]
    predecessor: ApprovalAssignmentGeneration
    principal: ApproverPrincipal | None = None
    target: ManualApprovalReassignmentTarget | None = None
    authorization: ApprovalReassignmentAuthorization | None = None
    expiry_decision: ReassignExpiredApproval | None = None
    supersession: ApprovalSupersession
    successor: ApprovalItem

    @model_validator(mode="after")
    def _manual_basis_is_exact(self) -> Self:
        if self.source == "manual":
            if (
                self.principal is None
                or self.target is None
                or self.authorization is None
                or self.expiry_decision is not None
            ):
                raise ValueError(
                    "manual 재지정 work에는 principal·target·authorization이 필요합니다."
                )
            if (
                self.authorization.assignment_generation != self.predecessor
                or self.authorization.org_id != self.principal.org_id
                or self.authorization.actor_id != self.principal.subject_id
                or self.authorization.target_approver_id != self.target.approver_id
                or self.authorization.requirement != self.successor.requirement
                or self.authorization.due_at != self.successor.due_at
                or self.supersession.reason != "reassigned"
                or self.supersession.superseded_at != self.successor.created_at
                or self.supersession.policy_version != self.authorization.policy_version
                or self.supersession.authority_version != self.authorization.authority_version
                or self.supersession.evidence_ref != self.authorization.evidence_ref
                or self.supersession.actor_id != self.principal.subject_id
                or self.supersession.target_approver_id != self.target.approver_id
            ):
                raise ValueError("manual 재지정 work와 authorization이 다릅니다.")
        elif (
            self.principal is not None
            or self.target is not None
            or self.authorization is not None
            or self.expiry_decision is None
        ):
            raise ValueError("expiry 재지정 work에는 manual command basis를 둘 수 없습니다.")
        elif (
            self.expiry_decision.assignment_generation != self.predecessor
            or self.expiry_decision.requirement != self.successor.requirement
            or self.expiry_decision.due_at != self.successor.due_at
            or self.supersession.reason != "expired"
            or self.supersession.superseded_at != self.successor.created_at
            or self.supersession.policy_version != self.expiry_decision.policy_version
            or self.supersession.authority_version != self.expiry_decision.authority_version
            or self.supersession.evidence_ref != self.expiry_decision.evidence_ref
            or self.supersession.actor_id is not None
            or self.supersession.target_approver_id != self.expiry_decision.requirement.approver_id
        ):
            raise ValueError("expiry 재지정 work와 sealed policy result가 다릅니다.")
        if (
            self.successor.supersedes_item_id != self.predecessor.item_id
            or self.supersession.successor_item_id != self.successor.item_id
        ):
            raise ValueError("재지정 work의 predecessor·successor 링크가 다릅니다.")
        return self


class _UnavailableWork(_FrozenModel):
    kind: Literal["unavailable"] = "unavailable"
    predecessor: ApprovalAssignmentGeneration
    evidence: ApprovalUnavailabilityEvidence

    @model_validator(mode="after")
    def _generation_is_exact(self) -> Self:
        if self.evidence.decision.assignment_generation != self.predecessor:
            raise ValueError("unavailable work generation이 evidence와 다릅니다.")
        return self


_LifecycleWork: TypeAlias = _ReassignmentWork | _UnavailableWork


class ApprovalOperationsApplication:
    """Approval 처리함 목록·상세·처분의 단일 안전 application."""

    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        approvals: ApprovalStore,
        boundary: ApprovalBoundary | None = None,
        completion: QuestionCompletionUnitOfWork | None = None,
        reader: QuestionCompletionReader | None = None,
        terminal_publisher: QuestionTerminalPublisher | None = None,
        expiry_policy: ApprovalExpiryPolicy | None = None,
        reassignment_authorizer: ApprovalReassignmentAuthorizer | None = None,
        item_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        evidence_recorder: ApprovalEventRecorder | None = None,
        notifier: Notifier | None = None,
        retention_policy: ApprovalDraftRetentionPolicy | None = None,
        central_authorizer: CentralAuthorizer | None = None,
    ) -> None:
        decision_dependencies = (boundary, completion, terminal_publisher)
        if any(dependency is not None for dependency in decision_dependencies) and not (
            all(dependency is not None for dependency in decision_dependencies)
            and reader is not None
        ):
            raise ApprovalOperationsDependency
        lifecycle_dependencies = (
            expiry_policy,
            reassignment_authorizer,
            item_id_factory,
            clock,
        )
        if any(dependency is not None for dependency in lifecycle_dependencies) and not all(
            dependency is not None for dependency in lifecycle_dependencies
        ):
            raise ApprovalOperationsDependency
        if any(dependency is not None for dependency in lifecycle_dependencies) and (
            reader is None or terminal_publisher is None
        ):
            raise ApprovalOperationsDependency
        if (
            reader is not None
            and not any(dependency is not None for dependency in decision_dependencies)
            and not any(dependency is not None for dependency in lifecycle_dependencies)
            and retention_policy is None
        ):
            raise ApprovalOperationsDependency
        if retention_policy is not None and (reader is None or evidence_recorder is None):
            raise ApprovalOperationsDependency
        self._requests = requests
        self._approvals = approvals
        self._boundary = boundary
        self._completion = completion
        self._reader = reader
        self._terminal_publisher = terminal_publisher
        self._expiry_policy = expiry_policy
        self._reassignment_authorizer = reassignment_authorizer
        self._item_id_factory = item_id_factory
        self._clock = clock
        self._evidence_recorder = evidence_recorder
        self._notifier = notifier
        self._retention_policy = retention_policy
        self._central_authorizer = central_authorizer
        self._retention_lock = RLock()
        self._retention_condition = Condition(self._retention_lock)
        self._retention_inflight: dict[tuple[str, str, str, str, datetime], int] = {}
        self._retention_decisions: dict[
            tuple[str, str, str, str, datetime],
            tuple[ApprovalDraftTerminalEvidence, ApprovalDraftRetentionDecision],
        ] = {}
        self._lifecycle_lock = RLock()
        self._lifecycle_work: dict[str, _LifecycleWork] = {}
        self._lifecycle_results: dict[
            str,
            tuple[_LifecycleWork, ApprovalLifecycleOutcome],
        ] = {}
        self._lifecycle_queue: deque[str] = deque()
        self._lifecycle_queued: set[str] = set()
        self._lifecycle_quarantined: set[str] = set()
        self._lifecycle_due_candidates: dict[str, ApprovalItem] = {}
        self._lifecycle_inflight: set[str] = set()
        self._lifecycle_scan_active = False

    def matches_dependencies(
        self,
        *,
        requests: QuestionRequestStore,
        approvals: ApprovalStore,
        boundary: ApprovalBoundary,
        completion: QuestionCompletionUnitOfWork,
        reader: QuestionCompletionReader,
        terminal_publisher: QuestionTerminalPublisher,
    ) -> bool:
        """composition이 read/write/publish 단일 원천 identity를 검증하는 손잡이."""
        return (
            self._requests is requests
            and self._approvals is approvals
            and self._boundary is boundary
            and self._completion is completion
            and self._reader is reader
            and self._terminal_publisher is terminal_publisher
        )

    def matches_lifecycle_dependencies(
        self,
        *,
        expiry_policy: ApprovalExpiryPolicy,
        reassignment_authorizer: ApprovalReassignmentAuthorizer,
        item_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
        reader: QuestionCompletionReader,
        terminal_publisher: QuestionTerminalPublisher,
    ) -> bool:
        """expiry·manual repair가 canonical surface와 같은 원천을 쓰는지 검증한다."""
        return (
            self._expiry_policy is expiry_policy
            and self._reassignment_authorizer is reassignment_authorizer
            and self._item_id_factory is item_id_factory
            and self._clock is clock
            and self._reader is reader
            and self._terminal_publisher is terminal_publisher
        )

    def matches_evidence_dependencies(
        self,
        *,
        evidence_recorder: ApprovalEventRecorder | None,
        notifier: Notifier | None,
    ) -> bool:
        """composition이 사건 journal과 push service의 exact identity를 검증한다."""
        return self._evidence_recorder is evidence_recorder and self._notifier is notifier

    def matches_retention_dependencies(
        self,
        *,
        retention_policy: ApprovalDraftRetentionPolicy | None,
        reader: QuestionCompletionReader | None,
        evidence_recorder: ApprovalEventRecorder | None,
    ) -> bool:
        """composition이 보존 판정과 exact terminal 원천 identity를 검증하는 손잡이."""
        return (
            self._retention_policy is retention_policy
            and self._reader is reader
            and self._evidence_recorder is evidence_recorder
        )

    def retention_status(
        self,
        item_id: str,
        evaluated_at: datetime,
    ) -> ApprovalDraftRetentionStatus:
        """전체 세대와 exact terminal을 확인해 Draft 보존 상태만 판정한다."""
        policy = self._retention_policy
        if policy is None or self._reader is None or self._evidence_recorder is None:
            raise ApprovalOperationsDependency
        canonical_item_id = self._canonical_item_id(item_id)
        canonical_evaluated_at = self._canonical_retention_time(evaluated_at)
        current = self._retention_current_item(canonical_item_id)
        request = self._read_decision_request(current.request_id)
        terminal = self._retention_terminal(current, request)
        if isinstance(terminal, ApprovalDraftRetained):
            return terminal
        assert type(terminal) in (
            ApprovalAnsweredTerminalEvidence,
            ApprovalDeclinedTerminalEvidence,
            ApprovalUnavailableTerminalEvidence,
        )
        if canonical_evaluated_at < terminal.terminal_at:
            raise ApprovalOperationsInvalid
        decision = self._evaluate_retention_once(
            policy=policy,
            terminal=terminal,
            evaluated_at=canonical_evaluated_at,
        )
        if decision.purge_eligible:
            self._record_retention_eligible(current, terminal, decision)
        try:
            return ApprovalDraftRetentionEvaluated(
                retain_until=decision.retain_until,
                purge_eligible=decision.purge_eligible,
                policy_version=decision.policy_version,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    def pending_for(
        self,
        principal: ApprovalOperationsPrincipal,
    ) -> list[ApprovalPendingSummary]:
        principal = self._canonical_operations_principal(principal)
        self._authorize_governance(
            principal,
            "approval.list",
            ResourceRef(
                org_id=principal.org_id,
                kind="approval_collection",
                owner_subject_id=principal.subject_id,
            ),
        )
        try:
            raw_summaries = self._approvals.open_for_designated_approver(
                principal.org_id,
                principal.subject_id,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        if type(raw_summaries) is not list:
            raise ApprovalOperationsIntegrityError

        verified: list[ApprovalPendingSummary] = []
        seen_item_ids: set[str] = set()
        for raw_summary in raw_summaries:
            summary = self._canonical_summary(raw_summary)
            if summary.item_id in seen_item_ids:
                raise ApprovalOperationsIntegrityError
            seen_item_ids.add(summary.item_id)
            item = self._get_item_for_pending(summary.item_id)
            if (
                item.status != "open"
                or item.org_id != principal.org_id
                or item.requirement.approver_id != principal.subject_id
                or self._summary_for(item) != summary
            ):
                raise ApprovalOperationsIntegrityError
            self._require_current(item)
            self._require_request_link(item)
            verified.append(self._summary_for(item))

        return sorted(
            verified,
            key=lambda summary: (
                summary.assigned_at,
                summary.request_id,
                summary.approval_round,
                summary.item_id,
            ),
        )

    def detail(
        self,
        item_id: str,
        principal: ApprovalOperationsPrincipal,
    ) -> ApprovalPendingDetail:
        principal = self._canonical_operations_principal(principal)
        if not item_id.strip():
            raise ApprovalOperationsNotFoundOrDenied
        try:
            raw_item = self._approvals.get(item_id)
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        if raw_item is None:
            raise ApprovalOperationsNotFoundOrDenied
        item = self._canonical_item(raw_item)
        if item.item_id != item_id:
            raise ApprovalOperationsIntegrityError
        if item.status != "open":
            raise ApprovalOperationsNotFoundOrDenied
        self._require_current(item)
        if item.org_id != principal.org_id or item.requirement.approver_id != principal.subject_id:
            raise ApprovalOperationsNotFoundOrDenied
        self._authorize_approval_item(principal, "approval.read", item)
        request = self._require_request_link(item)
        try:
            return ApprovalPendingDetail(
                item_id=item.item_id,
                request_id=item.request_id,
                approval_round=item.approval_round,
                assigned_at=item.created_at,
                due_at=item.due_at,
                question=request.question,
                draft_id=item.draft.draft_id,
                candidate=item.draft.candidate,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    def decide(
        self,
        item_id: str,
        principal: ApprovalOperationsPrincipal,
        intent: ApprovalDecisionIntent,
    ) -> ApprovalOperationsDecision:
        """현재 지정 승인자의 actor-free intent를 terminal 결과까지 수렴시킨다."""
        boundary, completion, reader, terminal_publisher = self._decision_dependencies()
        authorization_principal = self._canonical_operations_principal(principal)
        canonical_principal = self._domain_approver_principal(authorization_principal)
        canonical_intent = self._canonical_decision_intent(intent)
        canonical_item_id = self._canonical_item_id(item_id)
        action = self._action_for(canonical_principal, canonical_intent)
        item, _ = self._decision_snapshot(
            canonical_item_id,
            canonical_principal,
            action,
        )
        self._authorize_approval_item(authorization_principal, "approval.decide", item)
        try:
            boundary_result = boundary.decide(
                canonical_item_id,
                canonical_principal,
                action,
                expected_item=item,
            )
        except (ApprovalNotFoundError, ApprovalUnauthorizedError) as error:
            raise ApprovalOperationsNotFoundOrDenied from error
        except ApprovalAuthorizationDependencyError as error:
            raise ApprovalOperationsDependency from error
        except ApprovalConcurrencyError as error:
            raise ApprovalOperationsConflict from error
        except ApprovalItemMismatchError as error:
            raise ApprovalOperationsIntegrityError from error
        except ApprovalPolicyViolationError as error:
            raise ApprovalOperationsDependency from error
        except Exception as error:
            raise ApprovalOperationsDependency from error

        if isinstance(action, (Approve, ApproveWithEdit)):
            candidate = self._canonical_boundary_candidate(boundary_result)
            resolved = self._resolved_item(
                canonical_item_id,
                action,
                expected_item=item,
            )
            resolution = resolved.resolution
            if resolution is None or resolution.approved_candidate != candidate:
                raise ApprovalOperationsIntegrityError
            try:
                raw_completion = completion.complete(candidate)
            except CompletionConcurrencyError as error:
                raise ApprovalOperationsConflict from error
            except (
                CompletionEvidenceError,
                CompletionIdCollisionError,
                CompletionNotFoundError,
                IncompleteCompletionStateError,
                InvalidCompletionHandoffError,
            ) as error:
                raise ApprovalOperationsIntegrityError from error
            except AnswerFinalizationError as error:
                raise ApprovalOperationsDependency from error
            except Exception as error:
                raise ApprovalOperationsDependency from error
            completed = self._canonical_completion(raw_completion)
            bundle = self._read_completion_bundle(reader, resolved.request_id)
            resolved = self._resolved_item(
                canonical_item_id,
                action,
                expected_item=item,
            )
            self._verify_approved_completion(resolved, candidate, completed, bundle)
            self._record_decision_event(resolved, action, terminal_record_id=completed.record_id)
            delivery = self._publish(terminal_publisher, resolved.request_id)
            try:
                return ApprovalAnswered(
                    item_id=resolved.item_id,
                    approval_round=resolved.approval_round,
                    request_id=resolved.request_id,
                    record_id=completed.record_id,
                    action=action.kind,
                    delivery=delivery,
                )
            except Exception as error:
                raise ApprovalOperationsIntegrityError from error

        rejected = self._canonical_boundary_rejection(boundary_result)
        resolved = self._resolved_item(
            canonical_item_id,
            action,
            expected_item=item,
        )
        if rejected.request_id != resolved.request_id or rejected.reason_code != action.reason_code:
            raise ApprovalOperationsIntegrityError
        request = self._read_decision_request(resolved.request_id)
        self._verify_rejected_request(resolved, action, request)
        try:
            completion_bundle = reader.by_request(resolved.request_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if completion_bundle is not None:
            raise ApprovalOperationsIntegrityError
        self._record_decision_event(resolved, action)
        delivery = self._publish(terminal_publisher, resolved.request_id)
        try:
            return ApprovalDeclined(
                item_id=resolved.item_id,
                approval_round=resolved.approval_round,
                request_id=resolved.request_id,
                reason_code=action.reason_code,
                delivery=delivery,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    def reassign(
        self,
        item_id: str,
        principal: ApprovalOperationsPrincipal,
        target: ManualApprovalReassignmentTarget,
    ) -> ApprovalReassigned:
        """인증 principal과 중앙 authorizer로 새 ApprovalItem 세대를 만든다."""
        _, authorizer, item_id_factory, clock, _, _ = self._lifecycle_dependencies()
        canonical_item_id = self._canonical_item_id(item_id)
        authorization_principal = self._canonical_operations_principal(principal)
        canonical_principal = self._domain_approver_principal(authorization_principal)
        canonical_target = self._canonical_reassignment_target(target)
        with self._lifecycle_lock, self._lifecycle_item_guard(canonical_item_id):
            existing = self._lifecycle_work.get(canonical_item_id)
            if existing is not None:
                if (
                    existing.predecessor.org_id != canonical_principal.org_id
                    or type(existing) is not _ReassignmentWork
                    or existing.source != "manual"
                    or existing.principal != canonical_principal
                ):
                    raise ApprovalOperationsNotFoundOrDenied
                if existing.target != canonical_target:
                    raise ApprovalOperationsConflict
                return self._converge_manual_reassignment(existing)

            item, _ = self._lifecycle_open_snapshot(
                canonical_item_id,
                org_id=canonical_principal.org_id,
                hide_closed=True,
            )
            self._authorize_approval_item(
                authorization_principal,
                "approval.reassign",
                item,
            )
            requested_at = self._lifecycle_now(clock)
            if requested_at < item.created_at:
                raise ApprovalOperationsIntegrityError
            assignment = ApprovalAssignmentGeneration.from_item(item)
            try:
                raw_authorization = authorizer.authorize(
                    assignment=assignment,
                    principal=canonical_principal,
                    target_approver_id=canonical_target.approver_id,
                    requested_at=requested_at,
                )
            except Exception as error:
                raise ApprovalOperationsDependency from error
            authorization = self._canonical_reassignment_authorization(raw_authorization)
            self._verify_reassignment_authorization(
                authorization,
                assignment=assignment,
                principal=canonical_principal,
                target=canonical_target,
                requested_at=requested_at,
            )
            if isinstance(authorization, ApprovalReassignmentDenied):
                raise ApprovalOperationsNotFoundOrDenied
            successor_id = self._new_lifecycle_item_id(item_id_factory)
            try:
                successor = ApprovalItem(
                    item_id=successor_id,
                    org_id=item.org_id,
                    request_id=item.request_id,
                    awaiting_revision=item.awaiting_revision + 1,
                    attempt=item.attempt,
                    route=item.route,
                    draft=item.draft,
                    requirement=authorization.requirement,
                    created_at=requested_at,
                    due_at=authorization.due_at,
                    approval_round=item.approval_round + 1,
                    supersedes_item_id=item.item_id,
                )
                work = _ReassignmentWork(
                    source="manual",
                    predecessor=assignment,
                    principal=canonical_principal,
                    target=canonical_target,
                    authorization=authorization,
                    supersession=ApprovalSupersession(
                        reason="reassigned",
                        successor_item_id=successor_id,
                        superseded_at=requested_at,
                        policy_version=authorization.policy_version,
                        authority_version=authorization.authority_version,
                        evidence_ref=authorization.evidence_ref,
                        actor_id=canonical_principal.subject_id,
                        target_approver_id=canonical_target.approver_id,
                    ),
                    successor=successor,
                )
            except Exception as error:
                raise ApprovalOperationsIntegrityError from error
            self._lifecycle_work[item.item_id] = work
            return self._converge_manual_reassignment(work)

    def _converge_manual_reassignment(
        self,
        work: _ReassignmentWork,
    ) -> ApprovalReassigned:
        try:
            return self._converge_reassignment(work)
        except ApprovalOperationsConflict as error:
            try:
                retired = self._retire_definitive_expiry_loser(work)
            except ApprovalOperationsError as retirement_error:
                self._schedule_lifecycle_retry(work.predecessor.item_id, retirement_error)
                raise retirement_error
            if retired:
                self._lifecycle_due_candidates.pop(work.predecessor.item_id, None)
                raise error
            retry_error = ApprovalOperationsDependency()
            self._schedule_lifecycle_retry(work.predecessor.item_id, retry_error)
            raise retry_error from error
        except ApprovalOperationsError as error:
            self._schedule_lifecycle_retry(work.predecessor.item_id, error)
            raise

    def expire_due(
        self,
        now: datetime,
        limit: int,
    ) -> list[ApprovalLifecycleScanResult]:
        """due·pending Item을 공정하게 한 번씩 시도하고 성공과 실패를 함께 돌려준다."""
        expiry_policy, _, item_id_factory, _, _, _ = self._lifecycle_dependencies()
        canonical_now = self._canonical_lifecycle_time(now)
        if type(limit) is not int or limit <= 0:
            raise ApprovalOperationsInvalid
        with self._lifecycle_lock, self._lifecycle_scan_guard():
            if set(self._lifecycle_results).difference(self._lifecycle_work):
                raise ApprovalOperationsIntegrityError
            pending_candidates: list[_LifecycleWork] = []
            for work in self._lifecycle_work.values():
                completed = self._cached_lifecycle_result(work)
                if completed is None and (
                    type(work) is _UnavailableWork or type(work) is _ReassignmentWork
                ):
                    pending_candidates.append(work)
            pending = sorted(
                pending_candidates,
                key=lambda work: (
                    work.predecessor.due_at,
                    work.predecessor.request_id,
                    work.predecessor.item_id,
                ),
            )
            for work in pending:
                self._enqueue_lifecycle(work.predecessor.item_id)

            # 이미 알려진 open poison Item을 건너 다음 due Item까지 발견할 수 있게
            # 처리 상한과 별도로 process-local queue 길이만큼 read window를 넓힌다.
            scan_limit = limit + len(self._lifecycle_queue) + len(self._lifecycle_quarantined)
            try:
                raw_due = self._approvals.due_open(canonical_now, scan_limit)
            except Exception as error:
                raise ApprovalOperationsDependency from error
            if type(raw_due) is not list:
                raise ApprovalOperationsIntegrityError
            due_items = [self._canonical_item(item) for item in raw_due]
            if len(due_items) > scan_limit:
                raise ApprovalOperationsIntegrityError
            previous_key: tuple[datetime, datetime, str, int, int, str] | None = None
            seen_due_item_ids: set[str] = set()
            seen_due_assignments: set[tuple[str, int]] = set()
            for item in due_items:
                assignment_key = (item.request_id, item.attempt)
                if item.item_id in seen_due_item_ids or assignment_key in seen_due_assignments:
                    raise ApprovalOperationsIntegrityError
                seen_due_item_ids.add(item.item_id)
                seen_due_assignments.add(assignment_key)
                if item.item_id in self._lifecycle_results:
                    completed_work = self._lifecycle_work.get(item.item_id)
                    if completed_work is None:
                        raise ApprovalOperationsIntegrityError
                    self._cached_lifecycle_result(completed_work)
                    self._exact_lifecycle_item(completed_work.predecessor)
                    raise ApprovalOperationsIntegrityError
                key = (
                    item.due_at,
                    item.created_at,
                    item.request_id,
                    item.attempt,
                    item.approval_round,
                    item.item_id,
                )
                if (
                    item.status != "open"
                    or item.due_at > canonical_now
                    or previous_key is not None
                    and key <= previous_key
                ):
                    raise ApprovalOperationsIntegrityError
                previous_key = key
                self._lifecycle_due_candidates[item.item_id] = item
                self._enqueue_lifecycle(item.item_id)

            results: list[ApprovalLifecycleScanResult] = []
            processed_assignments: set[tuple[str, int]] = set()
            cycle_size = len(self._lifecycle_queue)
            inspected = 0
            attempted = 0
            while inspected < cycle_size and attempted < limit:
                item_id = self._lifecycle_queue.popleft()
                self._lifecycle_queued.remove(item_id)
                inspected += 1
                if item_id in self._lifecycle_results:
                    completed_work = self._lifecycle_work.get(item_id)
                    if completed_work is None:
                        raise ApprovalOperationsIntegrityError
                    completed = self._cached_lifecycle_result(completed_work)
                    if completed is None:
                        raise ApprovalOperationsIntegrityError
                    self._verify_cached_lifecycle_postcondition(completed_work, completed)
                    self._lifecycle_due_candidates.pop(item_id, None)
                    continue
                work = self._lifecycle_work.get(item_id)
                due_item = self._lifecycle_due_candidates.get(item_id)
                if work is None and due_item is None:
                    continue
                if work is None and due_item is not None and due_item.due_at > canonical_now:
                    self._enqueue_lifecycle(item_id)
                    continue
                if (
                    work is None
                    and due_item is not None
                    and self._has_pending_predecessor(due_item)
                ):
                    self._enqueue_lifecycle(item_id)
                    continue
                if work is not None:
                    request_id = work.predecessor.request_id
                    attempt = work.predecessor.attempt
                else:
                    assert due_item is not None
                    request_id = due_item.request_id
                    attempt = due_item.attempt
                assignment_key = (request_id, attempt)
                # 같은 scan에서 한 Request의 여러 generation을 연쇄 처분하지 않는다.
                if assignment_key in processed_assignments:
                    self._enqueue_lifecycle(item_id)
                    continue
                processed_assignments.add(assignment_key)
                attempted += 1
                try:
                    with self._lifecycle_item_guard(item_id, within_scan=True):
                        if work is None:
                            assert due_item is not None
                            work = self._plan_due_work(
                                due_item,
                                now=canonical_now,
                                expiry_policy=expiry_policy,
                                item_id_factory=item_id_factory,
                            )
                            self._lifecycle_work[item_id] = work
                        outcome = self._converge_lifecycle_work(work)
                except _LifecycleReentryError:
                    raise
                except ApprovalOperationsConflict as error:
                    planned = self._lifecycle_work.get(item_id)
                    retired = False
                    if planned is not None:
                        try:
                            retired = self._retire_definitive_expiry_loser(planned)
                        except ApprovalOperationsError as retirement_error:
                            results.append(
                                self._lifecycle_failure(
                                    item_id=item_id,
                                    request_id=request_id,
                                    error=retirement_error,
                                )
                            )
                            self._schedule_lifecycle_retry(item_id, retirement_error)
                            continue
                    elif due_item is not None:
                        try:
                            retired = self._retire_due_candidate_loser(due_item)
                        except ApprovalOperationsError as retirement_error:
                            results.append(
                                self._lifecycle_failure(
                                    item_id=item_id,
                                    request_id=request_id,
                                    error=retirement_error,
                                )
                            )
                            self._schedule_lifecycle_retry(item_id, retirement_error)
                            continue
                    if retired:
                        result_error: ApprovalOperationsError = error
                        self._lifecycle_due_candidates.pop(item_id, None)
                    else:
                        result_error = ApprovalOperationsDependency()
                        self._schedule_lifecycle_retry(item_id, result_error)
                    results.append(
                        self._lifecycle_failure(
                            item_id=item_id,
                            request_id=request_id,
                            error=result_error,
                        )
                    )
                except ApprovalOperationsError as error:
                    results.append(
                        self._lifecycle_failure(
                            item_id=item_id,
                            request_id=request_id,
                            error=error,
                        )
                    )
                    self._schedule_lifecycle_retry(item_id, error)
                else:
                    results.append(outcome)
                    self._lifecycle_due_candidates.pop(item_id, None)
            return results

    @contextmanager
    def _lifecycle_item_guard(
        self,
        item_id: str,
        *,
        within_scan: bool = False,
    ) -> Generator[None, None, None]:
        if item_id in self._lifecycle_inflight or (self._lifecycle_scan_active and not within_scan):
            raise _LifecycleReentryError
        self._lifecycle_inflight.add(item_id)
        try:
            yield
        finally:
            self._lifecycle_inflight.remove(item_id)

    @contextmanager
    def _lifecycle_scan_guard(self) -> Generator[None, None, None]:
        if self._lifecycle_scan_active or self._lifecycle_inflight:
            raise _LifecycleReentryError
        self._lifecycle_scan_active = True
        try:
            yield
        finally:
            self._lifecycle_scan_active = False

    def _has_pending_predecessor(self, item: ApprovalItem) -> bool:
        for work in self._lifecycle_work.values():
            if self._cached_lifecycle_result(work) is not None:
                continue
            if (
                work.predecessor.request_id == item.request_id
                and work.predecessor.attempt == item.attempt
                and work.predecessor.approval_round < item.approval_round
            ):
                return True
        return False

    def _enqueue_lifecycle(self, item_id: str) -> None:
        if item_id in self._lifecycle_queued or item_id in self._lifecycle_quarantined:
            return
        self._lifecycle_queue.append(item_id)
        self._lifecycle_queued.add(item_id)

    def _schedule_lifecycle_retry(
        self,
        item_id: str,
        error: ApprovalOperationsError,
    ) -> None:
        if isinstance(error, ApprovalOperationsDependency):
            # 같은 Item에 새 sealed manual work가 생기면 과거 due-plan 격리는
            # 더 이상 현재 repair 권한이 아니다.
            self._lifecycle_quarantined.discard(item_id)
            self._enqueue_lifecycle(item_id)
            return
        self._lifecycle_quarantined.add(item_id)

    def _retire_due_candidate_loser(self, expected: ApprovalItem) -> bool:
        current = self._exact_lifecycle_item(ApprovalAssignmentGeneration.from_item(expected))
        return current.status != "open"

    def _exact_lifecycle_item(
        self,
        expected: ApprovalAssignmentGeneration,
    ) -> ApprovalItem:
        try:
            raw_item = self._approvals.get(expected.item_id)
            raw_current = self._approvals.get_by_request_attempt(
                expected.request_id,
                expected.attempt,
            )
            raw_round = self._approvals.get_by_request_attempt_round(
                expected.request_id,
                expected.attempt,
                expected.approval_round,
            )
            raw_generations = self._approvals.generations(
                expected.request_id,
                expected.attempt,
            )
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if (
            raw_item is None
            or raw_current is None
            or raw_round is None
            or type(raw_generations) is not list
        ):
            raise ApprovalOperationsIntegrityError
        item = self._canonical_item(raw_item)
        current = self._canonical_item(raw_current)
        round_item = self._canonical_item(raw_round)
        if item != round_item or not item.matches_assignment_generation(expected):
            raise ApprovalOperationsIntegrityError
        generations = [self._canonical_item(generation) for generation in raw_generations]
        self._require_full_generation_lineage(generations, current)
        if item.status != "superseded":
            if current != item:
                raise ApprovalOperationsIntegrityError
            return item
        if current.approval_round <= item.approval_round:
            raise ApprovalOperationsIntegrityError
        lineage = generations[item.approval_round - 1 : current.approval_round]
        if not lineage or lineage[0] != item or lineage[-1] != current:
            raise ApprovalOperationsIntegrityError
        for earlier, later in zip(lineage[:-1], lineage[1:], strict=True):
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
                raise ApprovalOperationsIntegrityError
        return item

    @staticmethod
    def _lifecycle_failure(
        *,
        item_id: str,
        request_id: str,
        error: ApprovalOperationsError,
    ) -> ApprovalLifecycleFailure:
        if isinstance(error, ApprovalOperationsDependency):
            error_code = "dependency"
            retryable = True
        elif isinstance(error, ApprovalOperationsIntegrityError):
            error_code = "integrity"
            retryable = False
        else:
            error_code = "conflict"
            retryable = False
        try:
            return ApprovalLifecycleFailure(
                item_id=item_id,
                request_id=request_id,
                error_code=error_code,
                retryable=retryable,
            )
        except Exception as failure_error:
            raise ApprovalOperationsIntegrityError from failure_error

    def _plan_due_work(
        self,
        item: ApprovalItem,
        *,
        now: datetime,
        expiry_policy: ApprovalExpiryPolicy,
        item_id_factory: Callable[[], str],
    ) -> _LifecycleWork:
        exact_item, _ = self._lifecycle_open_snapshot(item.item_id, org_id=item.org_id)
        if exact_item != item:
            raise ApprovalOperationsIntegrityError
        assignment = ApprovalAssignmentGeneration.from_item(item)
        try:
            raw_result = expiry_policy.evaluate(assignment=assignment, now=now)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        policy_result = self._canonical_expiry_result(raw_result)
        if policy_result.assignment_generation != assignment:
            raise ApprovalOperationsIntegrityError
        if isinstance(policy_result, ReassignExpiredApproval):
            if policy_result.due_at < now:
                raise ApprovalOperationsIntegrityError
            successor_id = self._new_lifecycle_item_id(item_id_factory)
            try:
                successor = ApprovalItem(
                    item_id=successor_id,
                    org_id=item.org_id,
                    request_id=item.request_id,
                    awaiting_revision=item.awaiting_revision + 1,
                    attempt=item.attempt,
                    route=item.route,
                    draft=item.draft,
                    requirement=policy_result.requirement,
                    created_at=now,
                    due_at=policy_result.due_at,
                    approval_round=item.approval_round + 1,
                    supersedes_item_id=item.item_id,
                )
                return _ReassignmentWork(
                    source="expiry",
                    predecessor=assignment,
                    expiry_decision=policy_result,
                    supersession=ApprovalSupersession(
                        reason="expired",
                        successor_item_id=successor_id,
                        superseded_at=now,
                        policy_version=policy_result.policy_version,
                        authority_version=policy_result.authority_version,
                        evidence_ref=policy_result.evidence_ref,
                        target_approver_id=policy_result.requirement.approver_id,
                    ),
                    successor=successor,
                )
            except Exception as error:
                raise ApprovalOperationsIntegrityError from error
        try:
            return _UnavailableWork(
                predecessor=assignment,
                evidence=ApprovalUnavailabilityEvidence(
                    decision=policy_result,
                    unavailable_at=now,
                ),
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    def _lifecycle_dependencies(
        self,
    ) -> tuple[
        ApprovalExpiryPolicy,
        ApprovalReassignmentAuthorizer,
        Callable[[], str],
        Callable[[], datetime],
        QuestionCompletionReader,
        QuestionTerminalPublisher,
    ]:
        expiry_policy = self._expiry_policy
        authorizer = self._reassignment_authorizer
        item_id_factory = self._item_id_factory
        clock = self._clock
        reader = self._reader
        publisher = self._terminal_publisher
        if (
            expiry_policy is None
            or authorizer is None
            or item_id_factory is None
            or clock is None
            or reader is None
            or publisher is None
        ):
            raise ApprovalOperationsDependency
        return expiry_policy, authorizer, item_id_factory, clock, reader, publisher

    def _lifecycle_open_snapshot(
        self,
        item_id: str,
        *,
        org_id: str,
        hide_closed: bool = False,
    ) -> tuple[ApprovalItem, QuestionRequest]:
        try:
            raw_item = self._approvals.get(item_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw_item is None:
            raise ApprovalOperationsNotFoundOrDenied
        item = self._canonical_item(raw_item)
        if item.item_id != item_id:
            raise ApprovalOperationsIntegrityError
        if item.org_id != org_id:
            raise ApprovalOperationsNotFoundOrDenied
        if item.status != "open":
            if hide_closed:
                raise ApprovalOperationsNotFoundOrDenied
            raise ApprovalOperationsConflict
        self._require_decision_indexes(item)
        try:
            raw_generations = self._approvals.generations(item.request_id, item.attempt)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if type(raw_generations) is not list:
            raise ApprovalOperationsIntegrityError
        generations = [self._canonical_item(generation) for generation in raw_generations]
        self._require_full_generation_lineage(generations, item)
        request = self._read_decision_request(item.request_id)
        if request.org_id != item.org_id:
            raise ApprovalOperationsIntegrityError
        self._verify_open_request(item, request)
        if request.updated_at != item.created_at:
            raise ApprovalOperationsIntegrityError
        return item, request

    def _converge_lifecycle_work(
        self,
        work: _LifecycleWork,
    ) -> ApprovalLifecycleOutcome:
        if type(work) is _ReassignmentWork:
            return self._converge_reassignment(work)
        if type(work) is _UnavailableWork:
            return self._converge_unavailable(work)
        raise ApprovalOperationsIntegrityError

    def _cached_lifecycle_result(
        self,
        work: _LifecycleWork,
    ) -> ApprovalLifecycleOutcome | None:
        exact_work = self._strict_lifecycle_work(work)
        raw_cached: object = self._lifecycle_results.get(exact_work.predecessor.item_id)
        if raw_cached is None:
            return None
        if type(raw_cached) is not tuple or len(raw_cached) != 2:
            raise ApprovalOperationsIntegrityError
        cached_work, outcome = raw_cached
        if type(cached_work) not in (_ReassignmentWork, _UnavailableWork) or type(outcome) not in (
            ApprovalReassigned,
            ApprovalMadeUnavailable,
        ):
            raise ApprovalOperationsIntegrityError
        exact_cached_work = self._strict_lifecycle_work(cached_work)
        exact_outcome = self._strict_lifecycle_outcome(outcome)
        if exact_cached_work != exact_work or not self._lifecycle_outcome_matches_work(
            exact_work,
            exact_outcome,
        ):
            raise ApprovalOperationsIntegrityError
        return exact_outcome

    def _cache_lifecycle_result(
        self,
        work: _LifecycleWork,
        outcome: ApprovalLifecycleOutcome,
    ) -> None:
        exact_work = self._strict_lifecycle_work(work)
        exact_outcome = self._strict_lifecycle_outcome(outcome)
        if not self._lifecycle_outcome_matches_work(exact_work, exact_outcome):
            raise ApprovalOperationsIntegrityError
        item_id = exact_work.predecessor.item_id
        cached = self._lifecycle_results.get(item_id)
        entry = (exact_work, exact_outcome)
        if cached is not None and cached != entry:
            raise ApprovalOperationsIntegrityError
        self._lifecycle_results[item_id] = entry

    @staticmethod
    def _strict_lifecycle_work(raw: object) -> _LifecycleWork:
        try:
            if type(raw) is _ReassignmentWork:
                return _ReassignmentWork.model_validate(
                    raw.model_dump(round_trip=True, warnings="error"),
                    strict=True,
                )
            if type(raw) is _UnavailableWork:
                return _UnavailableWork.model_validate(
                    raw.model_dump(round_trip=True, warnings="error"),
                    strict=True,
                )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        raise ApprovalOperationsIntegrityError

    @staticmethod
    def _strict_lifecycle_outcome(raw: object) -> ApprovalLifecycleOutcome:
        try:
            if type(raw) is ApprovalReassigned:
                return ApprovalReassigned.model_validate(
                    raw.model_dump(round_trip=True, warnings="error"),
                    strict=True,
                )
            if type(raw) is ApprovalMadeUnavailable:
                return ApprovalMadeUnavailable.model_validate(
                    raw.model_dump(round_trip=True, warnings="error"),
                    strict=True,
                )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        raise ApprovalOperationsIntegrityError

    def _verify_cached_lifecycle_postcondition(
        self,
        work: _LifecycleWork,
        outcome: ApprovalLifecycleOutcome,
    ) -> None:
        if not self._lifecycle_outcome_matches_work(work, outcome):
            raise ApprovalOperationsIntegrityError
        if type(work) is _ReassignmentWork:
            assert isinstance(work, _ReassignmentWork)
            try:
                raw_successor = self._approvals.get(work.successor.item_id)
            except Exception as error:
                raise ApprovalOperationsDependency from error
            if raw_successor is None:
                raise ApprovalOperationsIntegrityError
            successor = self._canonical_item(raw_successor)
            if not successor.matches_assignment_generation(work.successor):
                raise ApprovalOperationsIntegrityError
            current, lineage = self._require_reassignment_indexes(work, successor)
            request = self._read_decision_request(work.predecessor.request_id)
            if self._request_matches_predecessor(request, work.predecessor):
                raise ApprovalOperationsIntegrityError
            self._verify_reassignment_progress(
                request,
                work,
                successor=successor,
                current=current,
                lineage=lineage,
            )
            return
        if type(work) is _UnavailableWork:
            assert isinstance(work, _UnavailableWork)
            try:
                raw_item = self._approvals.get(work.predecessor.item_id)
            except Exception as error:
                raise ApprovalOperationsDependency from error
            if raw_item is None:
                raise ApprovalOperationsIntegrityError
            item = self._canonical_item(raw_item)
            if (
                item.status != "unavailable"
                or item.unavailability != work.evidence
                or not item.matches_assignment_generation(work.predecessor)
            ):
                raise ApprovalOperationsIntegrityError
            self._require_unavailable_indexes(item, work.predecessor)
            request = self._read_decision_request(item.request_id)
            self._verify_unavailable_request(request, work)
            reader = self._reader_or_dependency()
            try:
                completion = reader.by_request(item.request_id)
            except Exception as error:
                raise ApprovalOperationsDependency from error
            if completion is not None:
                raise ApprovalOperationsIntegrityError
            return
        raise ApprovalOperationsIntegrityError

    @staticmethod
    def _lifecycle_outcome_matches_work(
        work: _LifecycleWork,
        outcome: ApprovalLifecycleOutcome,
    ) -> bool:
        if type(work) is _ReassignmentWork:
            if type(outcome) is not ApprovalReassigned:
                return False
            assert isinstance(work, _ReassignmentWork)
            assert isinstance(outcome, ApprovalReassigned)
            return (
                outcome.predecessor_item_id == work.predecessor.item_id
                and outcome.successor_item_id == work.successor.item_id
                and outcome.request_id == work.successor.request_id
                and outcome.approval_round == work.successor.approval_round
                and outcome.due_at == work.successor.due_at
                and outcome.reason == work.supersession.reason
            )
        if type(work) is _UnavailableWork:
            if type(outcome) is not ApprovalMadeUnavailable:
                return False
            assert isinstance(work, _UnavailableWork)
            assert isinstance(outcome, ApprovalMadeUnavailable)
            return (
                outcome.item_id == work.predecessor.item_id
                and outcome.request_id == work.predecessor.request_id
                and outcome.error_code == "approval_unavailable"
                and type(outcome.delivery) in (TerminalPublished, TerminalAlreadyPublished)
            )
        return False

    def _retire_definitive_expiry_loser(self, work: _LifecycleWork) -> bool:
        """다른 Store winner가 닫은 expiry plan만 pending drain에서 제거한다."""
        item = self._exact_lifecycle_item(work.predecessor)
        if type(work) is _ReassignmentWork:
            committed_by_this_work = (
                item.status == "superseded" and item.supersession == work.supersession
            )
        else:
            if type(work) is not _UnavailableWork:
                raise ApprovalOperationsIntegrityError
            committed_by_this_work = (
                item.status == "unavailable" and item.unavailability == work.evidence
            )
        if item.status == "open" or committed_by_this_work:
            return False
        self._lifecycle_work.pop(work.predecessor.item_id, None)
        return True

    def _converge_reassignment(self, work: _ReassignmentWork) -> ApprovalReassigned:
        completed = self._cached_lifecycle_result(work)
        if completed is not None:
            if type(completed) is not ApprovalReassigned:
                raise ApprovalOperationsIntegrityError
            self._verify_cached_lifecycle_postcondition(work, completed)
            self._record_reassignment_events(work)
            self._notify_reassignment(work)
            return completed
        try:
            raw_store_result = self._approvals.supersede_and_create_if_open(
                work.predecessor.item_id,
                work.supersession,
                work.successor,
                expected_generation=work.predecessor,
            )
        except ApprovalItemMismatchError as error:
            raise ApprovalOperationsIntegrityError from error
        except (ApprovalConcurrencyError, ApprovalNotFoundError) as error:
            raise ApprovalOperationsConflict from error
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if (
            type(raw_store_result) is not tuple
            or len(raw_store_result) != 2
            or type(raw_store_result[1]) is not bool
        ):
            raise ApprovalOperationsIntegrityError
        raw_successor = raw_store_result[0]
        successor = self._canonical_item(raw_successor)
        if (
            successor.item_id != work.successor.item_id
            or not successor.matches_assignment_generation(work.successor)
        ):
            raise ApprovalOperationsIntegrityError
        current_assignment, lineage = self._require_reassignment_indexes(work, successor)
        request = self._read_decision_request(work.predecessor.request_id)
        if self._request_matches_predecessor(request, work.predecessor):
            try:
                updated = request.reassign_approval(
                    previous_item_id=work.predecessor.item_id,
                    successor_item_id=successor.item_id,
                    due_at=successor.due_at,
                    clock=lambda: successor.created_at,
                )
                raw_swapped = self._requests.compare_and_set(
                    request.request_id,
                    request.revision,
                    request,
                    updated,
                )
            except Exception as error:
                raise ApprovalOperationsDependency from error
            if type(raw_swapped) is not bool:
                raise ApprovalOperationsIntegrityError
            request = self._read_decision_request(work.predecessor.request_id)
            if self._request_matches_predecessor(request, work.predecessor):
                raise ApprovalOperationsDependency
        self._verify_reassignment_progress(
            request,
            work,
            successor=successor,
            current=current_assignment,
            lineage=lineage,
        )
        try:
            result = ApprovalReassigned(
                predecessor_item_id=work.predecessor.item_id,
                successor_item_id=successor.item_id,
                request_id=successor.request_id,
                approval_round=successor.approval_round,
                due_at=successor.due_at,
                reason=work.supersession.reason,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        self._record_reassignment_events(work)
        self._notify_reassignment(work)
        self._cache_lifecycle_result(work, result)
        return result

    def _converge_unavailable(self, work: _UnavailableWork) -> ApprovalMadeUnavailable:
        completed = self._cached_lifecycle_result(work)
        if completed is not None:
            if type(completed) is not ApprovalMadeUnavailable:
                raise ApprovalOperationsIntegrityError
            self._verify_cached_lifecycle_postcondition(work, completed)
            self._record_unavailable_events(work)
            return completed
        try:
            raw_store_result = self._approvals.close_unavailable_if_open(
                work.predecessor.item_id,
                work.predecessor,
                work.evidence,
            )
        except ApprovalItemMismatchError as error:
            raise ApprovalOperationsIntegrityError from error
        except (ApprovalConcurrencyError, ApprovalNotFoundError) as error:
            raise ApprovalOperationsConflict from error
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if (
            type(raw_store_result) is not tuple
            or len(raw_store_result) != 2
            or type(raw_store_result[1]) is not bool
        ):
            raise ApprovalOperationsIntegrityError
        raw_item = raw_store_result[0]
        item = self._canonical_item(raw_item)
        if (
            item.item_id != work.predecessor.item_id
            or item.status != "unavailable"
            or item.unavailability != work.evidence
            or not item.matches_assignment_generation(work.predecessor)
        ):
            raise ApprovalOperationsIntegrityError
        self._require_unavailable_indexes(item, work.predecessor)
        request = self._read_decision_request(item.request_id)
        if self._request_matches_predecessor(request, work.predecessor):
            try:
                failed = request.transition(
                    FailedRequest(error_code="approval_unavailable"),
                    clock=lambda: work.evidence.unavailable_at,
                )
                raw_swapped = self._requests.compare_and_set(
                    request.request_id,
                    request.revision,
                    request,
                    failed,
                )
            except Exception as error:
                raise ApprovalOperationsDependency from error
            if type(raw_swapped) is not bool:
                raise ApprovalOperationsIntegrityError
            request = self._read_decision_request(item.request_id)
            if self._request_matches_predecessor(request, work.predecessor):
                raise ApprovalOperationsDependency
        self._verify_unavailable_request(request, work)
        _, _, _, _, reader, publisher = self._lifecycle_dependencies()
        try:
            completion = reader.by_request(item.request_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if completion is not None:
            raise ApprovalOperationsIntegrityError
        self._record_unavailable_events(work)
        delivery = self._publish(publisher, item.request_id)
        try:
            result = ApprovalMadeUnavailable(
                item_id=item.item_id,
                request_id=item.request_id,
                delivery=delivery,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        if not isinstance(delivery, TerminalDeferred):
            self._cache_lifecycle_result(work, result)
        return result

    def _record_decision_event(
        self,
        item: ApprovalItem,
        action: Approve | ApproveWithEdit | Reject,
        *,
        terminal_record_id: str | None = None,
    ) -> None:
        recorder = self._evidence_recorder
        if recorder is None:
            return
        resolution = item.resolution
        if resolution is None or resolution.action != action:
            raise ApprovalOperationsIntegrityError
        try:
            subject = ApprovalHumanSubject(subject_id=action.by_approver)
            action_digest = approval_action_digest(action)
            if isinstance(action, Reject):
                if terminal_record_id is not None:
                    raise ApprovalOperationsIntegrityError
                event: ApprovalEvent = ApprovalRejectedEvent(
                    org_id=item.org_id,
                    request_id=item.request_id,
                    item_id=item.item_id,
                    draft_id=item.draft.draft_id,
                    approval_round=item.approval_round,
                    subject=subject,
                    candidate_digest=approval_candidate_digest(item.draft.candidate),
                    policy_version=item.requirement.policy_version,
                    occurred_at=resolution.resolved_at,
                    action_digest=action_digest,
                    reason_digest=approval_action_digest({"reason_code": action.reason_code}),
                )
            else:
                candidate = resolution.approved_candidate
                if candidate is None or terminal_record_id is None:
                    raise ApprovalOperationsIntegrityError
                event_type = (
                    ApprovalApprovedEvent
                    if isinstance(action, Approve)
                    else ApprovalApprovedWithEditEvent
                )
                event = event_type(
                    org_id=item.org_id,
                    request_id=item.request_id,
                    item_id=item.item_id,
                    draft_id=item.draft.draft_id,
                    approval_round=item.approval_round,
                    subject=subject,
                    candidate_digest=approval_candidate_digest(candidate.candidate),
                    policy_version=item.requirement.policy_version,
                    occurred_at=resolution.resolved_at,
                    action_digest=action_digest,
                    terminal_record_id=terminal_record_id,
                )
        except ApprovalOperationsError:
            raise
        except ApprovalEvidenceIntegrity as error:
            raise ApprovalOperationsIntegrityError from error
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        self._record_approval_events((event,))

    def _record_reassignment_events(self, work: _ReassignmentWork) -> None:
        recorder = self._evidence_recorder
        if recorder is None:
            return
        try:
            candidate_digest = approval_candidate_digest(work.predecessor.draft.candidate)
            policy_version = work.supersession.policy_version
            if policy_version is None:
                raise ApprovalOperationsIntegrityError
            if work.source == "manual":
                principal = work.principal
                authorization = work.authorization
                if principal is None or authorization is None:
                    raise ApprovalOperationsIntegrityError
                subject = ApprovalHumanSubject(subject_id=principal.subject_id)
                action_basis: object = authorization
                events: tuple[ApprovalEvent, ...] = (
                    ApprovalReassignedEvent(
                        org_id=work.successor.org_id,
                        request_id=work.successor.request_id,
                        item_id=work.successor.item_id,
                        draft_id=work.successor.draft.draft_id,
                        approval_round=work.successor.approval_round,
                        subject=subject,
                        candidate_digest=candidate_digest,
                        policy_version=policy_version,
                        occurred_at=work.successor.created_at,
                        predecessor_item_id=work.predecessor.item_id,
                        action_digest=approval_action_digest(action_basis),
                    ),
                )
            else:
                decision = work.expiry_decision
                if decision is None:
                    raise ApprovalOperationsIntegrityError
                subject = ApprovalSystemSubject(system_id="approval_expiry")
                action_digest = approval_action_digest(decision)
                events = (
                    ApprovalExpiredEvent(
                        org_id=work.predecessor.org_id,
                        request_id=work.predecessor.request_id,
                        item_id=work.predecessor.item_id,
                        draft_id=work.predecessor.draft.draft_id,
                        approval_round=work.predecessor.approval_round,
                        subject=subject,
                        candidate_digest=candidate_digest,
                        policy_version=policy_version,
                        occurred_at=work.successor.created_at,
                        action_digest=action_digest,
                    ),
                    ApprovalReassignedEvent(
                        org_id=work.successor.org_id,
                        request_id=work.successor.request_id,
                        item_id=work.successor.item_id,
                        draft_id=work.successor.draft.draft_id,
                        approval_round=work.successor.approval_round,
                        subject=subject,
                        candidate_digest=candidate_digest,
                        policy_version=policy_version,
                        occurred_at=work.successor.created_at,
                        predecessor_item_id=work.predecessor.item_id,
                        action_digest=action_digest,
                    ),
                )
        except ApprovalOperationsError:
            raise
        except ApprovalEvidenceIntegrity as error:
            raise ApprovalOperationsIntegrityError from error
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        self._record_approval_events(events)

    def _record_unavailable_events(self, work: _UnavailableWork) -> None:
        recorder = self._evidence_recorder
        if recorder is None:
            return
        decision = work.evidence.decision
        try:
            candidate_digest = approval_candidate_digest(work.predecessor.draft.candidate)
            action_digest = approval_action_digest(decision)
            subject = ApprovalSystemSubject(system_id="approval_expiry")
            events: tuple[ApprovalEvent, ...] = (
                ApprovalExpiredEvent(
                    org_id=work.predecessor.org_id,
                    request_id=work.predecessor.request_id,
                    item_id=work.predecessor.item_id,
                    draft_id=work.predecessor.draft.draft_id,
                    approval_round=work.predecessor.approval_round,
                    subject=subject,
                    candidate_digest=candidate_digest,
                    policy_version=decision.policy_version,
                    occurred_at=work.evidence.unavailable_at,
                    action_digest=action_digest,
                ),
                ApprovalUnavailableEvent(
                    org_id=work.predecessor.org_id,
                    request_id=work.predecessor.request_id,
                    item_id=work.predecessor.item_id,
                    draft_id=work.predecessor.draft.draft_id,
                    approval_round=work.predecessor.approval_round,
                    subject=subject,
                    candidate_digest=candidate_digest,
                    policy_version=decision.policy_version,
                    occurred_at=work.evidence.unavailable_at,
                    action_digest=action_digest,
                ),
            )
        except ApprovalOperationsError:
            raise
        except ApprovalEvidenceIntegrity as error:
            raise ApprovalOperationsIntegrityError from error
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        self._record_approval_events(events)

    @staticmethod
    def _notify_failure_safe(notifier: Notifier, notification: Notification) -> None:
        try:
            notifier.notify(notification)
        except Exception:
            # push는 pull queue와 사건 기록의 성공 여부를 바꾸지 않는다.
            return

    def _notify_reassignment(self, work: _ReassignmentWork) -> None:
        notifier = self._notifier
        if notifier is None:
            return
        try:
            notification = Notification(
                recipient_id=work.successor.requirement.approver_id,
                kind="approval_assignment_ready",
                subject_ref=work.successor.item_id,
                created_at=work.successor.created_at,
            )
        except Exception:
            # 통지 객체가 오염돼도 canonical pull assignment는 유지한다.
            return
        self._notify_failure_safe(notifier, notification)

    def _record_approval_events(self, events: tuple[ApprovalEvent, ...]) -> None:
        recorder = self._evidence_recorder
        if recorder is None:
            return
        try:
            recorder.record_batch(events)
        except ApprovalEvidenceDependency as error:
            raise ApprovalOperationsDependency from error
        except ApprovalEvidenceIntegrity as error:
            raise ApprovalOperationsIntegrityError from error
        except Exception as error:
            raise ApprovalOperationsDependency from error

    @staticmethod
    def _canonical_retention_time(value: object) -> datetime:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ApprovalOperationsInvalid
        assert isinstance(value, datetime)
        return value.astimezone(UTC)

    def _retention_current_item(self, item_id: str) -> ApprovalItem:
        try:
            raw_queried = self._approvals.get(item_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw_queried is None:
            raise ApprovalOperationsNotFoundOrDenied
        queried = self._canonical_item(raw_queried)
        if queried.item_id != item_id:
            raise ApprovalOperationsIntegrityError
        try:
            raw_current = self._approvals.get_by_request_attempt(
                queried.request_id,
                queried.attempt,
            )
            raw_generations = self._approvals.generations(
                queried.request_id,
                queried.attempt,
            )
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw_current is None or type(raw_generations) is not list:
            raise ApprovalOperationsIntegrityError
        current = self._canonical_item(raw_current)
        generations = [self._canonical_item(raw) for raw in raw_generations]
        self._require_full_generation_lineage(generations, current)
        if (
            queried not in generations
            or current.org_id != queried.org_id
            or current.request_id != queried.request_id
            or current.attempt != queried.attempt
            or current.draft != queried.draft
        ):
            raise ApprovalOperationsIntegrityError
        return current

    def _retention_terminal(
        self,
        item: ApprovalItem,
        request: QuestionRequest,
    ) -> ApprovalDraftTerminalEvidence | ApprovalDraftRetained:
        if request.request_id != item.request_id or request.org_id != item.org_id:
            raise ApprovalOperationsIntegrityError
        state = request.state
        if item.status == "open":
            self._verify_open_request(item, request)
            self._require_no_completion(item.request_id)
            return ApprovalDraftRetained(reason="active_assignment")

        resolution = item.resolution
        if item.status == "resolved" and resolution is not None:
            action = resolution.action
            if isinstance(state, AwaitingApproval):
                self._verify_open_request(item, request)
                self._require_no_completion(item.request_id)
                reason: Literal["finalization_pending", "terminalization_pending"] = (
                    "finalization_pending"
                    if isinstance(action, (Approve, ApproveWithEdit))
                    else "terminalization_pending"
                )
                return ApprovalDraftRetained(reason=reason)
            if isinstance(action, (Approve, ApproveWithEdit)):
                candidate = resolution.approved_candidate
                if candidate is None or not isinstance(state, AnsweredRequest):
                    raise ApprovalOperationsIntegrityError
                bundle = self._read_completion_bundle(self._reader_or_dependency(), item.request_id)
                completed = self._canonical_completion(bundle.completion)
                self._verify_approved_completion(item, candidate, completed, bundle)
                try:
                    return ApprovalAnsweredTerminalEvidence(
                        org_id=item.org_id,
                        request_id=item.request_id,
                        current_item_id=item.item_id,
                        draft_id=item.draft.draft_id,
                        approval_round=item.approval_round,
                        request_revision=request.revision,
                        record_id=completed.record_id,
                        terminal_digest=approval_event_digest(bundle),
                        candidate_digest=approval_candidate_digest(candidate.candidate),
                        action_digest=approval_action_digest(action),
                        approval_policy_version=item.requirement.policy_version,
                        terminal_at=completed.completed_at,
                    )
                except ApprovalEvidenceIntegrity as error:
                    raise ApprovalOperationsIntegrityError from error
                except Exception as error:
                    raise ApprovalOperationsIntegrityError from error
            if not isinstance(state, DeclinedRequest):
                raise ApprovalOperationsIntegrityError
            self._verify_rejected_request(item, action, request)
            self._require_no_completion(item.request_id)
            try:
                return ApprovalDeclinedTerminalEvidence(
                    org_id=item.org_id,
                    request_id=item.request_id,
                    current_item_id=item.item_id,
                    draft_id=item.draft.draft_id,
                    approval_round=item.approval_round,
                    request_revision=request.revision,
                    reason_digest=approval_action_digest({"reason_code": action.reason_code}),
                    action_digest=approval_action_digest(action),
                    approval_policy_version=item.requirement.policy_version,
                    terminal_at=resolution.resolved_at,
                )
            except ApprovalEvidenceIntegrity as error:
                raise ApprovalOperationsIntegrityError from error
            except Exception as error:
                raise ApprovalOperationsIntegrityError from error

        evidence = item.unavailability
        if item.status == "unavailable" and evidence is not None:
            if isinstance(state, AwaitingApproval):
                self._verify_open_request(item, request)
                self._require_no_completion(item.request_id)
                return ApprovalDraftRetained(reason="terminalization_pending")
            work = _UnavailableWork(
                predecessor=ApprovalAssignmentGeneration.from_item(item),
                evidence=evidence,
            )
            self._verify_unavailable_request(request, work)
            self._require_no_completion(item.request_id)
            try:
                return ApprovalUnavailableTerminalEvidence(
                    org_id=item.org_id,
                    request_id=item.request_id,
                    current_item_id=item.item_id,
                    draft_id=item.draft.draft_id,
                    approval_round=item.approval_round,
                    request_revision=request.revision,
                    evidence_digest=approval_event_digest(evidence),
                    candidate_digest=approval_candidate_digest(item.draft.candidate),
                    approval_policy_version=item.requirement.policy_version,
                    lifecycle_policy_version=evidence.decision.policy_version,
                    terminal_at=evidence.unavailable_at,
                )
            except ApprovalEvidenceIntegrity as error:
                raise ApprovalOperationsIntegrityError from error
            except Exception as error:
                raise ApprovalOperationsIntegrityError from error
        raise ApprovalOperationsIntegrityError

    def _require_no_completion(self, request_id: str) -> None:
        try:
            completion = self._reader_or_dependency().by_request(request_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if completion is not None:
            raise ApprovalOperationsIntegrityError

    def _evaluate_retention_once(
        self,
        *,
        policy: ApprovalDraftRetentionPolicy,
        terminal: ApprovalDraftTerminalEvidence,
        evaluated_at: datetime,
    ) -> ApprovalDraftRetentionDecision:
        try:
            terminal_digest = approval_event_digest(terminal)
        except ApprovalEvidenceIntegrity as error:
            raise ApprovalOperationsIntegrityError from error
        key = (
            terminal.org_id,
            terminal.request_id,
            terminal.draft_id,
            terminal_digest,
            evaluated_at,
        )
        owner = get_ident()
        with self._retention_condition:
            while key in self._retention_inflight:
                if self._retention_inflight[key] == owner:
                    raise _RetentionReentrySignal
                self._retention_condition.wait()
            cached = self._retention_decisions.get(key)
            if cached is not None:
                cached_terminal, cached_decision = cached
                canonical_terminal = self._canonical_retention_terminal(cached_terminal)
                canonical_decision = self._canonical_retention_decision(cached_decision)
                if (
                    canonical_terminal != terminal
                    or canonical_decision.terminal != terminal
                    or canonical_decision.evaluated_at != evaluated_at
                ):
                    raise ApprovalOperationsIntegrityError
                return canonical_decision
            self._retention_inflight[key] = owner

        try:
            try:
                raw = policy.evaluate(terminal=terminal, evaluated_at=evaluated_at)
            except _RetentionReentrySignal as error:
                raise ApprovalOperationsConflict from error
            except Exception as error:
                raise ApprovalOperationsDependency from error
            decision = self._canonical_retention_decision(raw)
            if decision.terminal != terminal or decision.evaluated_at != evaluated_at:
                raise ApprovalOperationsIntegrityError
            with self._retention_condition:
                existing = self._retention_decisions.get(key)
                if existing is not None and existing != (terminal, decision):
                    raise ApprovalOperationsIntegrityError
                self._retention_decisions[key] = (terminal, decision)
            return decision
        finally:
            with self._retention_condition:
                if self._retention_inflight.get(key) == owner:
                    del self._retention_inflight[key]
                self._retention_condition.notify_all()

    @staticmethod
    def _canonical_retention_decision(raw: object) -> ApprovalDraftRetentionDecision:
        if type(raw) is not ApprovalDraftRetentionDecision:
            raise ApprovalOperationsIntegrityError
        assert isinstance(raw, ApprovalDraftRetentionDecision)
        try:
            decision = ApprovalDraftRetentionDecision.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        if decision != raw:
            raise ApprovalOperationsIntegrityError
        return decision

    @staticmethod
    def _canonical_retention_terminal(raw: object) -> ApprovalDraftTerminalEvidence:
        allowed = (
            ApprovalAnsweredTerminalEvidence,
            ApprovalDeclinedTerminalEvidence,
            ApprovalUnavailableTerminalEvidence,
        )
        if type(raw) not in allowed:
            raise ApprovalOperationsIntegrityError
        assert isinstance(raw, allowed)
        try:
            terminal = type(raw).model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        if terminal != raw:
            raise ApprovalOperationsIntegrityError
        return terminal

    def _record_retention_eligible(
        self,
        item: ApprovalItem,
        terminal: ApprovalDraftTerminalEvidence,
        decision: ApprovalDraftRetentionDecision,
    ) -> None:
        try:
            event: ApprovalEvent = ApprovalRetentionEligibleEvent(
                org_id=item.org_id,
                request_id=item.request_id,
                item_id=item.item_id,
                draft_id=item.draft.draft_id,
                approval_round=item.approval_round,
                subject=ApprovalSystemSubject(system_id="approval_retention"),
                candidate_digest=approval_candidate_digest(item.draft.candidate),
                policy_version=decision.policy_version,
                occurred_at=decision.retain_until,
                terminal_kind=terminal.kind,
                request_revision=terminal.request_revision,
                terminal_at=terminal.terminal_at,
                terminal_evidence_digest=approval_event_digest(terminal),
                retain_until=decision.retain_until,
            )
        except ApprovalEvidenceIntegrity as error:
            raise ApprovalOperationsIntegrityError from error
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        self._record_approval_events((event,))

    def _require_reassignment_indexes(
        self,
        work: _ReassignmentWork,
        successor: ApprovalItem,
    ) -> tuple[ApprovalItem, tuple[ApprovalItem, ...]]:
        try:
            raw_successor_direct = self._approvals.get(successor.item_id)
            raw_predecessor_direct = self._approvals.get(work.predecessor.item_id)
            raw_current = self._approvals.get_by_request_attempt(
                successor.request_id,
                successor.attempt,
            )
            raw_predecessor = self._approvals.get_by_request_attempt_round(
                successor.request_id,
                successor.attempt,
                work.predecessor.approval_round,
            )
            raw_successor = self._approvals.get_by_request_attempt_round(
                successor.request_id,
                successor.attempt,
                successor.approval_round,
            )
            raw_generations = self._approvals.generations(
                successor.request_id,
                successor.attempt,
            )
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if (
            raw_successor_direct is None
            or raw_predecessor_direct is None
            or raw_current is None
            or raw_predecessor is None
            or raw_successor is None
            or type(raw_generations) is not list
        ):
            raise ApprovalOperationsIntegrityError
        successor_direct = self._canonical_item(raw_successor_direct)
        predecessor_direct = self._canonical_item(raw_predecessor_direct)
        current = self._canonical_item(raw_current)
        predecessor = self._canonical_item(raw_predecessor)
        round_successor = self._canonical_item(raw_successor)
        if (
            successor_direct != round_successor
            or not successor_direct.matches_assignment_generation(successor)
            or predecessor_direct != predecessor
            or predecessor.status != "superseded"
            or predecessor.supersession != work.supersession
            or not predecessor.matches_assignment_generation(work.predecessor)
        ):
            raise ApprovalOperationsIntegrityError
        generations = [self._canonical_item(item) for item in raw_generations]
        if (
            not generations
            or generations[-1] != current
            or current.status == "superseded"
            or len({item.item_id for item in generations}) != len(generations)
            or tuple(item.approval_round for item in generations)
            != tuple(range(1, current.approval_round + 1))
        ):
            raise ApprovalOperationsIntegrityError
        self._require_full_generation_indexes(generations)
        lineage = generations[work.predecessor.approval_round - 1 :]
        if (
            not lineage
            or lineage[0] != predecessor
            or len(lineage) < 2
            or lineage[1] != round_successor
            or lineage[-1] != current
        ):
            raise ApprovalOperationsIntegrityError
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
                raise ApprovalOperationsIntegrityError
        return current, tuple(lineage)

    def _require_full_generation_indexes(
        self,
        generations: list[ApprovalItem],
    ) -> None:
        for generation in generations:
            try:
                raw_direct = self._approvals.get(generation.item_id)
                raw_round = self._approvals.get_by_request_attempt_round(
                    generation.request_id,
                    generation.attempt,
                    generation.approval_round,
                )
            except Exception as error:
                raise ApprovalOperationsDependency from error
            if raw_direct is None or raw_round is None:
                raise ApprovalOperationsIntegrityError
            direct = self._canonical_item(raw_direct)
            round_item = self._canonical_item(raw_round)
            if direct != generation or round_item != generation:
                raise ApprovalOperationsIntegrityError

    def _require_full_generation_lineage(
        self,
        generations: list[ApprovalItem],
        current: ApprovalItem,
    ) -> None:
        if (
            not generations
            or generations[-1] != current
            or current.status == "superseded"
            or len({generation.item_id for generation in generations}) != len(generations)
            or tuple(generation.approval_round for generation in generations)
            != tuple(range(1, current.approval_round + 1))
        ):
            raise ApprovalOperationsIntegrityError
        self._require_full_generation_indexes(generations)
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
                raise ApprovalOperationsIntegrityError

    def _require_unavailable_indexes(
        self,
        item: ApprovalItem,
        predecessor: ApprovalAssignmentGeneration,
    ) -> None:
        try:
            raw_direct = self._approvals.get(item.item_id)
            raw_current = self._approvals.get_by_request_attempt(item.request_id, item.attempt)
            raw_round = self._approvals.get_by_request_attempt_round(
                item.request_id,
                item.attempt,
                item.approval_round,
            )
            raw_generations = self._approvals.generations(item.request_id, item.attempt)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if (
            raw_direct is None
            or raw_current is None
            or raw_round is None
            or type(raw_generations) is not list
        ):
            raise ApprovalOperationsIntegrityError
        direct = self._canonical_item(raw_direct)
        current = self._canonical_item(raw_current)
        round_item = self._canonical_item(raw_round)
        if (
            direct != item
            or current != item
            or round_item != item
            or not current.matches_assignment_generation(predecessor)
        ):
            raise ApprovalOperationsIntegrityError
        generations = [self._canonical_item(generation) for generation in raw_generations]
        if (
            not generations
            or generations[-1] != current
            or current.status == "superseded"
            or len({generation.item_id for generation in generations}) != len(generations)
            or tuple(generation.approval_round for generation in generations)
            != tuple(range(1, current.approval_round + 1))
        ):
            raise ApprovalOperationsIntegrityError
        self._require_full_generation_indexes(generations)
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
                raise ApprovalOperationsIntegrityError

    @staticmethod
    def _request_matches_predecessor(
        request: QuestionRequest,
        predecessor: ApprovalAssignmentGeneration,
    ) -> bool:
        state = request.state
        return (
            request.request_id == predecessor.request_id
            and request.org_id == predecessor.org_id
            and request.revision == predecessor.awaiting_revision
            and request.updated_at == predecessor.created_at
            and isinstance(state, AwaitingApproval)
            and state.draft_ref == predecessor.item_id
            and state.handling.ref == predecessor.item_id
            and state.route == predecessor.route
            and state.attempt == predecessor.attempt
            and state.handling.due_at == predecessor.due_at
        )

    def _verify_reassignment_progress(
        self,
        request: QuestionRequest,
        work: _ReassignmentWork,
        *,
        successor: ApprovalItem,
        current: ApprovalItem,
        lineage: tuple[ApprovalItem, ...],
    ) -> None:
        if (
            current.request_id != successor.request_id
            or current.org_id != successor.org_id
            or current.attempt != successor.attempt
            or current.approval_round < successor.approval_round
            or not successor.matches_assignment_generation(work.successor)
        ):
            raise ApprovalOperationsIntegrityError
        request_item = current
        if isinstance(request.state, AwaitingApproval):
            matching = tuple(
                item
                for item in lineage[1:]
                if item.item_id == request.state.draft_ref
                and item.awaiting_revision == request.revision
            )
            if len(matching) != 1:
                raise ApprovalOperationsIntegrityError
            request_item = matching[0]
        self._verify_assignment_request_or_terminal(request, request_item)

    def _verify_assignment_request_or_terminal(
        self,
        request: QuestionRequest,
        item: ApprovalItem,
    ) -> None:
        state = request.state
        if request.request_id != item.request_id or request.org_id != item.org_id:
            raise ApprovalOperationsIntegrityError
        if isinstance(state, AwaitingApproval):
            if (
                request.revision != item.awaiting_revision
                or request.updated_at != item.created_at
                or state.draft_ref != item.item_id
                or state.handling.ref != item.item_id
                or state.route != item.route
                or state.attempt != item.attempt
                or state.handling.due_at != item.due_at
            ):
                raise ApprovalOperationsIntegrityError
            if item.status == "resolved":
                reader = self._reader_or_dependency()
                try:
                    completion = reader.by_request(request.request_id)
                except Exception as error:
                    raise ApprovalOperationsDependency from error
                if completion is not None:
                    raise ApprovalOperationsIntegrityError
            return
        resolution = item.resolution
        if item.status == "resolved" and resolution is not None:
            if resolution.approved_candidate is not None and isinstance(state, AnsweredRequest):
                bundle = self._read_completion_bundle(
                    self._reader_or_dependency(), request.request_id
                )
                completed = self._canonical_completion(bundle.completion)
                self._verify_approved_completion(
                    item,
                    resolution.approved_candidate,
                    completed,
                    bundle,
                )
                return
            if isinstance(resolution.action, Reject) and isinstance(state, DeclinedRequest):
                self._verify_rejected_request(item, resolution.action, request)
                reader = self._reader_or_dependency()
                try:
                    completion = reader.by_request(request.request_id)
                except Exception as error:
                    raise ApprovalOperationsDependency from error
                if completion is not None:
                    raise ApprovalOperationsIntegrityError
                return
            raise ApprovalOperationsIntegrityError
        evidence = item.unavailability
        if item.status == "unavailable" and evidence is not None:
            if (
                request.revision != item.awaiting_revision + 1
                or request.updated_at != evidence.unavailable_at
                or not isinstance(state, FailedRequest)
                or state.error_code != "approval_unavailable"
            ):
                raise ApprovalOperationsIntegrityError
            reader = self._reader_or_dependency()
            try:
                completion = reader.by_request(request.request_id)
            except Exception as error:
                raise ApprovalOperationsDependency from error
            if completion is not None:
                raise ApprovalOperationsIntegrityError
            return
        raise ApprovalOperationsIntegrityError

    def _reader_or_dependency(self) -> QuestionCompletionReader:
        reader = self._reader
        if reader is None:
            raise ApprovalOperationsDependency
        return reader

    @staticmethod
    def _verify_unavailable_request(
        request: QuestionRequest,
        work: _UnavailableWork,
    ) -> None:
        if (
            request.request_id != work.predecessor.request_id
            or request.org_id != work.predecessor.org_id
            or request.revision != work.predecessor.awaiting_revision + 1
            or request.updated_at != work.evidence.unavailable_at
            or not isinstance(request.state, FailedRequest)
            or request.state.error_code != "approval_unavailable"
        ):
            raise ApprovalOperationsIntegrityError

    @staticmethod
    def _canonical_reassignment_target(
        target: object,
    ) -> ManualApprovalReassignmentTarget:
        if type(target) is not ManualApprovalReassignmentTarget:
            raise ApprovalOperationsInvalid
        assert isinstance(target, ManualApprovalReassignmentTarget)
        try:
            return ManualApprovalReassignmentTarget.model_validate(
                target.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsInvalid from error

    @staticmethod
    def _canonical_reassignment_authorization(
        raw: object,
    ) -> ApprovalReassignmentAuthorization | ApprovalReassignmentDenied:
        try:
            if type(raw) is ApprovalReassignmentAuthorization:
                assert isinstance(raw, ApprovalReassignmentAuthorization)
                return ApprovalReassignmentAuthorization.model_validate(
                    raw.model_dump(mode="python", round_trip=True),
                    strict=True,
                )
            if type(raw) is ApprovalReassignmentDenied:
                assert isinstance(raw, ApprovalReassignmentDenied)
                return ApprovalReassignmentDenied.model_validate(
                    raw.model_dump(mode="python", round_trip=True),
                    strict=True,
                )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        raise ApprovalOperationsIntegrityError

    @staticmethod
    def _verify_reassignment_authorization(
        authorization: ApprovalReassignmentAuthorization | ApprovalReassignmentDenied,
        *,
        assignment: ApprovalAssignmentGeneration,
        principal: ApproverPrincipal,
        target: ManualApprovalReassignmentTarget,
        requested_at: datetime,
    ) -> None:
        if (
            authorization.assignment_generation != assignment
            or authorization.org_id != principal.org_id
            or authorization.actor_id != principal.subject_id
            or authorization.target_approver_id != target.approver_id
        ):
            raise ApprovalOperationsIntegrityError
        if isinstance(authorization, ApprovalReassignmentAuthorization) and (
            authorization.requirement.approver_id != target.approver_id
            or authorization.due_at < requested_at
        ):
            raise ApprovalOperationsIntegrityError

    @staticmethod
    def _canonical_expiry_result(
        raw: object,
    ) -> ReassignExpiredApproval | ApprovalUnavailable:
        try:
            if type(raw) is ReassignExpiredApproval:
                assert isinstance(raw, ReassignExpiredApproval)
                return ReassignExpiredApproval.model_validate(
                    raw.model_dump(mode="python", round_trip=True),
                    strict=True,
                )
            if type(raw) is ApprovalUnavailable:
                assert isinstance(raw, ApprovalUnavailable)
                return ApprovalUnavailable.model_validate(
                    raw.model_dump(mode="python", round_trip=True),
                    strict=True,
                )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        raise ApprovalOperationsIntegrityError

    @staticmethod
    def _new_lifecycle_item_id(factory: Callable[[], str]) -> str:
        try:
            item_id = factory()
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if type(item_id) is not str or not item_id.strip():
            raise ApprovalOperationsIntegrityError
        return item_id

    @classmethod
    def _lifecycle_now(cls, clock: Callable[[], datetime]) -> datetime:
        try:
            return cls._canonical_lifecycle_time(clock())
        except ApprovalOperationsInvalid as error:
            raise ApprovalOperationsDependency from error
        except Exception as error:
            raise ApprovalOperationsDependency from error

    @staticmethod
    def _canonical_lifecycle_time(value: object) -> datetime:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ApprovalOperationsInvalid
        assert isinstance(value, datetime)
        return value

    def _decision_dependencies(
        self,
    ) -> tuple[
        ApprovalBoundary,
        QuestionCompletionUnitOfWork,
        QuestionCompletionReader,
        QuestionTerminalPublisher,
    ]:
        boundary = self._boundary
        completion = self._completion
        reader = self._reader
        publisher = self._terminal_publisher
        if boundary is None or completion is None or reader is None or publisher is None:
            raise ApprovalOperationsDependency
        return boundary, completion, reader, publisher

    def _decision_snapshot(
        self,
        item_id: str,
        principal: ApproverPrincipal,
        action: Approve | ApproveWithEdit | Reject,
    ) -> tuple[ApprovalItem, QuestionRequest]:
        try:
            raw_item = self._approvals.get(item_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw_item is None:
            raise ApprovalOperationsNotFoundOrDenied
        item = self._canonical_item(raw_item)
        if item.item_id != item_id:
            raise ApprovalOperationsIntegrityError
        if item.status in ("superseded", "unavailable"):
            raise ApprovalOperationsNotFoundOrDenied
        if item.org_id != principal.org_id or item.requirement.approver_id != principal.subject_id:
            raise ApprovalOperationsNotFoundOrDenied
        request: QuestionRequest | None = None
        if item.status == "open":
            try:
                self._require_decision_indexes(item)
                request = self._read_decision_request(item.request_id)
                if request.request_id != item.request_id or request.org_id != item.org_id:
                    raise ApprovalOperationsIntegrityError
                self._verify_open_request(item, request)
            except (ApprovalOperationsIntegrityError, ApprovalOperationsNotFoundOrDenied):
                item, request = self._recover_stale_open_decision(item_id, item)
            else:
                return item, request
        else:
            self._require_decision_indexes(item)
        if request is None:
            request = self._read_decision_request(item.request_id)
            if request.request_id != item.request_id or request.org_id != item.org_id:
                raise ApprovalOperationsIntegrityError
        resolution = item.resolution
        if resolution is None:
            raise ApprovalOperationsIntegrityError
        self._verify_assignment_request_or_terminal(request, item)
        if resolution.action != action:
            raise ApprovalOperationsConflict
        return item, request

    def _recover_stale_open_decision(
        self,
        item_id: str,
        stale_open: ApprovalItem,
    ) -> tuple[ApprovalItem, QuestionRequest]:
        """open 조회 뒤 끝난 exact winner를 재조회해 conflict 또는 forward repair로 분류한다."""
        try:
            raw_latest = self._approvals.get(item_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw_latest is None:
            raise ApprovalOperationsIntegrityError
        latest = self._canonical_item(raw_latest)
        if latest.item_id != item_id or not latest.matches_assignment_generation(stale_open):
            raise ApprovalOperationsIntegrityError
        request = self._read_decision_request(latest.request_id)
        if request.request_id != latest.request_id or request.org_id != latest.org_id:
            raise ApprovalOperationsIntegrityError
        if latest.status in ("superseded", "unavailable"):
            self._require_lifecycle_decision_conflict(latest, stale_open, request)
            raise ApprovalOperationsConflict
        if latest.status != "resolved":
            raise ApprovalOperationsIntegrityError
        self._require_decision_indexes(latest)
        return latest, request

    def _require_lifecycle_decision_conflict(
        self,
        latest: ApprovalItem,
        stale_open: ApprovalItem,
        request: QuestionRequest,
    ) -> None:
        """정상 lifecycle winner만 stale 처분의 명시적 conflict로 인정한다."""
        predecessor = ApprovalAssignmentGeneration.from_item(stale_open)
        if latest.status == "unavailable":
            self._require_unavailable_indexes(latest, predecessor)
            evidence = latest.unavailability
            if (
                evidence is None
                or request.request_id != latest.request_id
                or request.org_id != latest.org_id
                or request.revision != latest.awaiting_revision + 1
                or request.updated_at != evidence.unavailable_at
                or not isinstance(request.state, FailedRequest)
                or request.state.error_code != "approval_unavailable"
            ):
                raise ApprovalOperationsIntegrityError
            return

        if latest.status != "superseded":
            raise ApprovalOperationsIntegrityError
        try:
            raw_current = self._approvals.get_by_request_attempt(
                latest.request_id,
                latest.attempt,
            )
            raw_round = self._approvals.get_by_request_attempt_round(
                latest.request_id,
                latest.attempt,
                latest.approval_round,
            )
            raw_generations = self._approvals.generations(
                latest.request_id,
                latest.attempt,
            )
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw_current is None or raw_round is None or type(raw_generations) is not list:
            raise ApprovalOperationsIntegrityError
        current = self._canonical_item(raw_current)
        round_item = self._canonical_item(raw_round)
        generations = [self._canonical_item(generation) for generation in raw_generations]
        if (
            round_item != latest
            or not latest.matches_assignment_generation(predecessor)
            or len(generations) <= latest.approval_round
            or generations[latest.approval_round - 1] != latest
        ):
            raise ApprovalOperationsIntegrityError
        self._require_full_generation_lineage(generations, current)
        self._verify_open_request(current, request)

    def _require_decision_indexes(self, item: ApprovalItem) -> None:
        try:
            raw_current = self._approvals.get_by_request_attempt(
                item.request_id,
                item.attempt,
            )
            raw_round = self._approvals.get_by_request_attempt_round(
                item.request_id,
                item.attempt,
                item.approval_round,
            )
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw_current is None or raw_round is None:
            raise ApprovalOperationsIntegrityError
        current = self._canonical_item(raw_current)
        round_item = self._canonical_item(raw_round)
        if round_item != item:
            raise ApprovalOperationsIntegrityError
        if current != item:
            raise ApprovalOperationsNotFoundOrDenied

    def _read_decision_request(self, request_id: str) -> QuestionRequest:
        try:
            raw_request = self._requests.get(request_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw_request is None:
            raise ApprovalOperationsIntegrityError
        return self._canonical_request(raw_request)

    @staticmethod
    def _verify_open_request(item: ApprovalItem, request: QuestionRequest) -> None:
        state = request.state
        if (
            request.revision != item.awaiting_revision
            or request.updated_at != item.created_at
            or not isinstance(state, AwaitingApproval)
            or state.draft_ref != item.item_id
            or state.handling.ref != item.item_id
            or state.route != item.route
            or state.attempt != item.attempt
            or state.handling.due_at != item.due_at
        ):
            raise ApprovalOperationsIntegrityError

    def _resolved_item(
        self,
        item_id: str,
        action: Approve | ApproveWithEdit | Reject,
        *,
        expected_item: ApprovalItem,
    ) -> ApprovalItem:
        try:
            raw = self._approvals.get(item_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw is None:
            raise ApprovalOperationsIntegrityError
        item = self._canonical_item(raw)
        if not item.matches_assignment_generation(expected_item):
            raise ApprovalOperationsIntegrityError
        self._require_resolved_indexes(item, expected_item)
        if (
            item.item_id != item_id
            or item.status != "resolved"
            or item.resolution is None
            or item.resolution.action != action
        ):
            raise ApprovalOperationsIntegrityError
        return item

    def _require_resolved_indexes(
        self,
        item: ApprovalItem,
        expected_item: ApprovalItem,
    ) -> None:
        try:
            raw_current = self._approvals.get_by_request_attempt(
                item.request_id,
                item.attempt,
            )
            raw_round = self._approvals.get_by_request_attempt_round(
                item.request_id,
                item.attempt,
                item.approval_round,
            )
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw_current is None or raw_round is None:
            raise ApprovalOperationsIntegrityError
        current = self._canonical_item(raw_current)
        round_item = self._canonical_item(raw_round)
        if (
            current != item
            or round_item != item
            or not current.matches_assignment_generation(expected_item)
            or not round_item.matches_assignment_generation(expected_item)
        ):
            raise ApprovalOperationsIntegrityError

    @staticmethod
    def _canonical_boundary_candidate(raw: object) -> ApprovedCandidate:
        if type(raw) is not ApprovedCandidate:
            raise ApprovalOperationsIntegrityError
        assert isinstance(raw, ApprovedCandidate)
        try:
            return ApprovedCandidate.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    @staticmethod
    def _canonical_boundary_rejection(raw: object) -> ApprovalRejected:
        if type(raw) is not ApprovalRejected:
            raise ApprovalOperationsIntegrityError
        assert isinstance(raw, ApprovalRejected)
        try:
            return ApprovalRejected.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    @staticmethod
    def _canonical_completion(raw: object) -> AnswerCompletion:
        if type(raw) is not AnswerCompletion:
            raise ApprovalOperationsIntegrityError
        assert isinstance(raw, AnswerCompletion)
        try:
            return AnswerCompletion.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    @staticmethod
    def _read_completion_bundle(
        reader: QuestionCompletionReader,
        request_id: str,
    ):
        try:
            raw = reader.by_request(request_id)
        except Exception as error:
            raise ApprovalOperationsDependency from error
        if raw is None:
            raise ApprovalOperationsIntegrityError
        try:
            return canonical_completion_bundle(raw)
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    def _verify_approved_completion(
        self,
        item: ApprovalItem,
        candidate: ApprovedCandidate,
        completed: AnswerCompletion,
        bundle: object,
    ) -> None:
        # canonical_completion_bundle 위에서 exact CompletionBundle로 좁혀졌다.
        canonical = canonical_completion_bundle(bundle)
        current_request = self._read_decision_request(item.request_id)
        resolution = item.resolution
        if resolution is None or resolution.approved_candidate != candidate:
            raise ApprovalOperationsIntegrityError
        action = resolution.action
        if not isinstance(action, (Approve, ApproveWithEdit)):
            raise ApprovalOperationsIntegrityError
        request = canonical.request
        state = request.state
        audit = canonical.terminal_audit
        evidence = audit.approval
        final_mode = (
            "full" if candidate.candidate.mode == "draft_only" else candidate.candidate.mode
        )
        original = item.draft.candidate
        candidate_nontext_matches = (
            candidate.candidate.sources == original.sources
            and candidate.candidate.mode == original.mode
            and candidate.candidate.snapshot_sha == original.snapshot_sha
        )
        candidate_text_matches = (
            candidate.candidate.text == original.text
            if isinstance(action, Approve)
            else candidate.candidate.text == action.edited_text
        )
        if (
            canonical.completion != completed
            or current_request != request
            or not isinstance(state, AnsweredRequest)
            or request.request_id != item.request_id
            or request.org_id != item.org_id
            or request.revision != item.awaiting_revision + 1
            or request.updated_at != completed.completed_at
            or state.record_id != completed.record_id
            or completed.request_id != item.request_id
            or completed.text != candidate.candidate.text
            or completed.sources != candidate.candidate.sources
            or completed.snapshot_sha != candidate.candidate.snapshot_sha
            or completed.mode != final_mode
            or completed.review_status != "approved"
            or completed.agent_id != item.route.agent_id
            or audit.request_id != item.request_id
            or audit.record_id != completed.record_id
            or audit.org_id != item.org_id
            or audit.route != item.route
            or audit.attempt != item.attempt
            or audit.candidate_mode != candidate.candidate.mode
            or audit.final_mode != final_mode
            or type(evidence) is not HumanApprovalEvidence
            or evidence.item_id != item.item_id
            or evidence.action != action.kind
            or evidence.approved_by != candidate.approved_by
            or evidence.approved_at != candidate.approved_at
            or evidence.policy_version != candidate.policy_version
            or candidate.request_id != item.request_id
            or candidate.item_id != item.item_id
            or candidate.expected_revision != item.awaiting_revision
            or candidate.attempt != item.attempt
            or candidate.route != item.route
            or candidate.approved_by != action.by_approver
            or candidate.policy_version != item.requirement.policy_version
            or not candidate.assignment_generation.matches_item(item)
            or not candidate_nontext_matches
            or not candidate_text_matches
        ):
            raise ApprovalOperationsIntegrityError

    @staticmethod
    def _verify_rejected_request(
        item: ApprovalItem,
        action: Reject,
        request: QuestionRequest,
    ) -> None:
        resolution = item.resolution
        if (
            resolution is None
            or resolution.action != action
            or resolution.approved_candidate is not None
            or request.request_id != item.request_id
            or request.org_id != item.org_id
            or request.revision != item.awaiting_revision + 1
            or request.updated_at != resolution.resolved_at
            or not isinstance(request.state, DeclinedRequest)
            or request.state.reason_code != action.reason_code
        ):
            raise ApprovalOperationsIntegrityError

    @staticmethod
    def _publish(
        publisher: QuestionTerminalPublisher,
        request_id: str,
    ) -> TerminalDelivery:
        try:
            raw = publisher.publish_terminal(request_id)
        except Exception:
            return TerminalDeferred(reason_code="publish_failed")
        try:
            if type(raw) is TerminalPublished:
                return TerminalPublished.model_validate(raw.model_dump(), strict=True)
            if type(raw) is TerminalAlreadyPublished:
                return TerminalAlreadyPublished.model_validate(raw.model_dump(), strict=True)
            if type(raw) is TerminalDeferred:
                return TerminalDeferred.model_validate(raw.model_dump(), strict=True)
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        raise ApprovalOperationsIntegrityError

    @staticmethod
    def _canonical_item_id(item_id: object) -> str:
        if type(item_id) is not str or not item_id.strip():
            raise ApprovalOperationsInvalid
        return item_id

    @staticmethod
    def _canonical_decision_principal(principal: object) -> ApproverPrincipal:
        if type(principal) is not ApproverPrincipal:
            raise ApprovalOperationsInvalid
        assert isinstance(principal, ApproverPrincipal)
        try:
            return ApproverPrincipal.model_validate(
                principal.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsInvalid from error

    @staticmethod
    def _canonical_decision_intent(intent: object) -> ApprovalDecisionIntent:
        try:
            if type(intent) is ApproveIntent:
                assert isinstance(intent, ApproveIntent)
                return ApproveIntent.model_validate(
                    intent.model_dump(mode="python", round_trip=True), strict=True
                )
            if type(intent) is ApproveWithEditIntent:
                assert isinstance(intent, ApproveWithEditIntent)
                return ApproveWithEditIntent.model_validate(
                    intent.model_dump(mode="python", round_trip=True), strict=True
                )
            if type(intent) is RejectIntent:
                assert isinstance(intent, RejectIntent)
                return RejectIntent.model_validate(
                    intent.model_dump(mode="python", round_trip=True), strict=True
                )
        except Exception as error:
            raise ApprovalOperationsInvalid from error
        raise ApprovalOperationsInvalid

    @staticmethod
    def _action_for(
        principal: ApproverPrincipal,
        intent: ApprovalDecisionIntent,
    ) -> Approve | ApproveWithEdit | Reject:
        try:
            if isinstance(intent, ApproveWithEditIntent):
                return ApproveWithEdit(
                    by_approver=principal.subject_id,
                    edited_text=intent.edited_text,
                )
            if isinstance(intent, RejectIntent):
                return Reject(
                    by_approver=principal.subject_id,
                    reason_code=intent.reason_code,
                )
            return Approve(by_approver=principal.subject_id)
        except Exception as error:
            raise ApprovalOperationsInvalid from error

    def _get_item_for_pending(self, item_id: str) -> ApprovalItem:
        try:
            raw = self._approvals.get(item_id)
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        if raw is None:
            raise ApprovalOperationsIntegrityError
        item = self._canonical_item(raw)
        if item.item_id != item_id:
            raise ApprovalOperationsIntegrityError
        return item

    def _require_current(self, item: ApprovalItem) -> None:
        try:
            raw_current = self._approvals.get_by_request_attempt(
                item.request_id,
                item.attempt,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        if raw_current is None:
            raise ApprovalOperationsIntegrityError
        current = self._canonical_item(raw_current)
        if current != item:
            raise ApprovalOperationsIntegrityError

    def _require_request_link(self, item: ApprovalItem) -> QuestionRequest:
        try:
            raw_request = self._requests.get(item.request_id)
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
        if raw_request is None:
            raise ApprovalOperationsIntegrityError
        request = self._canonical_request(raw_request)
        state = request.state
        if (
            request.request_id != item.request_id
            or request.org_id != item.org_id
            or request.revision != item.awaiting_revision
            or not isinstance(state, AwaitingApproval)
            or state.draft_ref != item.item_id
            or state.handling.ref != item.item_id
            or state.route != item.route
            or state.attempt != item.attempt
            or state.handling.due_at != item.due_at
        ):
            raise ApprovalOperationsIntegrityError
        return request

    @staticmethod
    def _summary_for(item: ApprovalItem) -> ApprovalPendingSummary:
        try:
            return ApprovalPendingSummary(
                item_id=item.item_id,
                request_id=item.request_id,
                approval_round=item.approval_round,
                assigned_at=item.created_at,
                due_at=item.due_at,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    @staticmethod
    def _canonical_principal(principal: ApproverPrincipal) -> ApproverPrincipal:
        if type(principal) is not ApproverPrincipal:
            raise ApprovalOperationsIntegrityError
        try:
            return ApproverPrincipal.model_validate(
                principal.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    def _canonical_operations_principal(
        self,
        principal: object,
    ) -> ApprovalOperationsPrincipal:
        if self._central_authorizer is None:
            if type(principal) is not ApproverPrincipal:
                raise ApprovalOperationsIntegrityError
            return self._canonical_principal(principal)
        canonical = canonical_authenticated_principal(principal)
        if canonical is None:
            raise ApprovalOperationsNotFoundOrDenied
        return canonical

    @staticmethod
    def _domain_approver_principal(
        principal: ApprovalOperationsPrincipal,
    ) -> ApproverPrincipal:
        if type(principal) is ApproverPrincipal:
            return principal
        try:
            return ApproverPrincipal(
                org_id=principal.org_id,
                subject_id=principal.subject_id,
            )
        except Exception as error:
            raise ApprovalOperationsNotFoundOrDenied from error

    def _authorize_approval_item(
        self,
        principal: ApprovalOperationsPrincipal,
        action: Literal["approval.read", "approval.decide", "approval.reassign"],
        item: ApprovalItem,
    ) -> None:
        self._authorize_governance(
            principal,
            action,
            ResourceRef(
                org_id=item.org_id,
                kind="approval_item",
                resource_id=item.item_id,
                owner_subject_id=item.requirement.approver_id,
            ),
        )

    def _authorize_governance(
        self,
        principal: ApprovalOperationsPrincipal,
        action: Literal[
            "approval.list",
            "approval.read",
            "approval.decide",
            "approval.reassign",
        ],
        resource: ResourceRef,
    ) -> None:
        authorizer = self._central_authorizer
        if authorizer is None:
            return
        if type(principal) is not AuthenticatedPrincipal:
            raise ApprovalOperationsNotFoundOrDenied
        outcome = authorize_and_verify(authorizer, principal, action, resource)
        if outcome == "unavailable":
            raise ApprovalOperationsAuthorizationUnavailable
        if outcome == "denied":
            raise ApprovalOperationsNotFoundOrDenied

    @staticmethod
    def _canonical_summary(summary: ApprovalPendingSummary) -> ApprovalPendingSummary:
        if type(summary) is not ApprovalPendingSummary:
            raise ApprovalOperationsIntegrityError
        try:
            return ApprovalPendingSummary.model_validate(
                summary.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    @staticmethod
    def _canonical_item(item: ApprovalItem) -> ApprovalItem:
        if type(item) is not ApprovalItem:
            raise ApprovalOperationsIntegrityError
        try:
            return ApprovalItem.model_validate(
                item.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error

    @staticmethod
    def _canonical_request(request: QuestionRequest) -> QuestionRequest:
        if type(request) is not QuestionRequest:
            raise ApprovalOperationsIntegrityError
        try:
            return QuestionRequest.model_validate(
                request.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ApprovalOperationsIntegrityError from error
