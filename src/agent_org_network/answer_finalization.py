"""P17.3a 공통 Answer Finalization의 InMemory 원자 경계.

Question Request와 사용자에게 수용되는 다섯 결과를 하나의 backing state에서
copy-on-write로 확정한다. 기존 독립 Store의 공개 쓰기 메서드를 순서대로 호출하지
않으며, 이 모듈의 보장은 단일 프로세스 메모리 범위다.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Annotated, Literal, Protocol, TypeAlias, assert_never

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_org_network.answer_record import AnswerRecord
from agent_org_network.approval import (
    ApprovalPolicy,
    ApprovalItem,
    ApprovalStore,
    Approve,
    ApprovedCandidate,
    ApproveWithEdit,
    AnswerCandidate,
    FinalizationCandidate,
    NoApprovalRequired,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingApproval,
    DuplicateQuestionRequestError,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
    validate_compare_and_set_semantics,
    validate_new_question_request_semantics,
)
from agent_org_network.runtime import AnswerMode
from agent_org_network.session import SessionTurn

Clock: TypeAlias = Callable[[], datetime]
RecordIdFactory: TypeAlias = Callable[[], str]
CompletionFaultPoint: TypeAlias = Literal[
    "after_answer_record",
    "after_request",
    "after_audit",
    "after_session",
    "after_outbox",
    "before_commit",
]
FaultInjector: TypeAlias = Callable[[CompletionFaultPoint], None]
CompletionArtifactCheckpointPoint: TypeAlias = Literal[
    "after_answer_record",
    "after_request",
    "after_audit",
    "after_session",
    "after_outbox",
]
CompletionArtifactCheckpoint: TypeAlias = Callable[
    [CompletionArtifactCheckpointPoint],
    None,
]


class AnswerFinalizationError(RuntimeError):
    """Answer Finalization의 구조화된 기본 오류."""


class InvalidCompletionHandoffError(AnswerFinalizationError):
    """공개 sealed handoff가 아니거나 canonical validation에 실패함."""


class CompletionNotFoundError(AnswerFinalizationError):
    """Question Request 또는 승인 증거를 찾을 수 없음."""


class CompletionEvidenceError(AnswerFinalizationError):
    """Request snapshot·Approval 증거가 handoff와 다름."""


class CompletionAttributionError(AnswerFinalizationError):
    """현재 책임 Agent Card와 Owner snapshot을 확정할 수 없음."""


class CompletionClockError(AnswerFinalizationError):
    """Finalization 시각이 유효하지 않거나 역행함."""


class CompletionConcurrencyError(AnswerFinalizationError):
    """같은 Request에 서로 다른 terminal 후보가 경쟁함."""


class CompletionIdCollisionError(AnswerFinalizationError):
    """record ID가 다른 Request의 record와 충돌함."""


class IncompleteCompletionStateError(AnswerFinalizationError):
    """AnsweredRequest와 completion artifact 집합이 불완전함."""


class DirectAnsweredTransitionError(AnswerFinalizationError):
    """공개 Request CAS로 Answered terminalization을 우회함."""


class ReentrantCompletionMutationError(AnswerFinalizationError):
    """Finalization callback이 같은 backing state에 재진입해 쓰려 함."""


class CompletionDependencyError(AnswerFinalizationError):
    """중앙 정책·Approval Store·책임 resolver가 구성되지 않음."""


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


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}은 timezone-aware여야 합니다.")
    return value


class AnswerResponsibilitySnapshot(_FrozenModel):
    """Finalization 시점의 책임 Agent Card와 Owner User snapshot."""

    agent_id: str
    owner_id: str
    needs_correction_review: bool = Field(
        default=False,
        exclude_if=lambda value: value is False,
    )


class NoApprovalEvidence(_FrozenModel):
    kind: Literal["not_required"] = "not_required"
    policy_version: str
    needs_correction_review: bool = Field(
        default=False,
        exclude_if=lambda value: value is False,
    )


class HumanApprovalEvidence(_FrozenModel):
    kind: Literal["approved"] = "approved"
    item_id: str
    action: Literal["approve", "approve_with_edit"]
    approved_by: str
    approved_at: datetime
    policy_version: str

    @field_validator("approved_at", mode="after")
    @classmethod
    def _approved_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "HumanApprovalEvidence.approved_at")


ApprovalEvidence: TypeAlias = Annotated[
    NoApprovalEvidence | HumanApprovalEvidence,
    Field(discriminator="kind"),
]


class AnswerCompletion(_FrozenModel):
    request_id: str
    record_id: str
    text: str
    answered_by: str
    agent_id: str
    mode: AnswerMode
    sources: tuple[str, ...] = ()
    snapshot_sha: str | None = None
    review_status: Literal["not_required", "approved"]
    completed_at: datetime

    @field_validator("sources", mode="after")
    @classmethod
    def _sources_must_be_nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not source.strip() for source in value):
            raise ValueError("AnswerCompletion.sources에는 빈 출처를 둘 수 없습니다.")
        return value

    @field_validator("completed_at", mode="after")
    @classmethod
    def _completed_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "AnswerCompletion.completed_at")


class TerminalAnswerAudit(_FrozenModel):
    request_id: str
    record_id: str
    org_id: str
    requester_id: str
    attempt: int = Field(ge=1)
    route: RouteTarget
    responsibility: AnswerResponsibilitySnapshot
    candidate_mode: AnswerMode
    final_mode: AnswerMode
    approval: ApprovalEvidence
    completed_at: datetime

    @field_validator("completed_at", mode="after")
    @classmethod
    def _completed_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "TerminalAnswerAudit.completed_at")

    @model_validator(mode="after")
    def _responsibility_must_match_route(self) -> TerminalAnswerAudit:
        if self.responsibility.agent_id != self.route.agent_id:
            raise ValueError("terminal audit 책임 Agent Card가 RouteTarget과 다릅니다.")
        if isinstance(self.approval, NoApprovalEvidence):
            if self.candidate_mode == "draft_only" or self.final_mode != self.candidate_mode:
                raise ValueError("승인 불필요 audit의 candidate/final mode가 올바르지 않습니다.")
        elif self.candidate_mode == "draft_only":
            if self.final_mode != "full":
                raise ValueError("승인된 draft_only의 final mode는 full이어야 합니다.")
        elif self.final_mode != self.candidate_mode:
            raise ValueError("승인 후 mode는 draft_only 승격 외에는 바뀔 수 없습니다.")
        return self


class DeliveryOutboxEntry(_FrozenModel):
    kind: Literal["answer_ready"] = "answer_ready"
    request_id: str
    record_id: str
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "DeliveryOutboxEntry.created_at")


class CompletionBundle(_FrozenModel):
    completion: AnswerCompletion
    request: QuestionRequest
    answer_record: AnswerRecord
    terminal_audit: TerminalAnswerAudit
    session_turn: SessionTurn | None
    delivery: DeliveryOutboxEntry

    @model_validator(mode="after")
    def _artifacts_must_be_exact(self) -> CompletionBundle:
        state = self.request.state
        if not isinstance(state, AnsweredRequest):
            raise ValueError("CompletionBundle.request는 AnsweredRequest여야 합니다.")
        request_id = self.completion.request_id
        record_id = self.completion.record_id
        if (
            self.request.request_id != request_id
            or state.record_id != record_id
            or self.answer_record.request_id != request_id
            or self.answer_record.record_id != record_id
            or self.terminal_audit.request_id != request_id
            or self.terminal_audit.record_id != record_id
            or self.delivery.request_id != request_id
            or self.delivery.record_id != record_id
        ):
            raise ValueError("Completion artifact의 request/record ID가 다릅니다.")
        if (
            self.answer_record.question != self.request.question
            or self.answer_record.session_id != self.request.session_id
            or self.answer_record.answer_text != self.completion.text
            or self.answer_record.answered_by != self.completion.answered_by
            or self.answer_record.agent_id != self.completion.agent_id
            or self.answer_record.mode != self.completion.mode
            or self.answer_record.sources != self.completion.sources
            or self.answer_record.snapshot_sha != self.completion.snapshot_sha
            or self.answer_record.needs_correction_review
            != self.terminal_audit.responsibility.needs_correction_review
        ):
            raise ValueError("AnswerRecord와 AnswerCompletion payload가 다릅니다.")
        audit = self.terminal_audit
        expected_correction_review = (
            audit.approval.needs_correction_review
            if isinstance(audit.approval, NoApprovalEvidence)
            else False
        )
        if (
            audit.org_id != self.request.org_id
            or audit.requester_id != self.request.requester_id
            or audit.route.agent_id != self.completion.agent_id
            or audit.responsibility.agent_id != self.completion.agent_id
            or audit.responsibility.owner_id != self.completion.answered_by
            or audit.final_mode != self.completion.mode
            or audit.completed_at != self.completion.completed_at
            or self.answer_record.answered_at != self.completion.completed_at
            or self.request.updated_at != self.completion.completed_at
            or self.delivery.created_at != self.completion.completed_at
            or audit.responsibility.needs_correction_review != expected_correction_review
        ):
            raise ValueError("Completion 책임·mode·시각 snapshot이 다릅니다.")
        expected_review = (
            "approved" if isinstance(audit.approval, HumanApprovalEvidence) else "not_required"
        )
        if self.completion.review_status != expected_review:
            raise ValueError("Completion review_status와 audit Approval 증거가 다릅니다.")
        if self.session_turn is not None and (
            self.session_turn.request_id != request_id
            or self.session_turn.question != self.request.question
            or self.session_turn.answer_text != self.completion.text
            or self.session_turn.answered_by != self.completion.agent_id
            or self.session_turn.at != self.completion.completed_at
        ):
            raise ValueError("SessionTurn이 Completion payload와 다릅니다.")
        if (self.request.session_id is None) != (self.session_turn is None):
            raise ValueError("session_id 유무와 request-correlated SessionTurn이 다릅니다.")
        return self


def canonical_completion_bundle(bundle: object) -> CompletionBundle:
    """Completion artifact 전체를 plain-data canonical 복사본으로 재검증한다."""
    if type(bundle) is not CompletionBundle:
        raise IncompleteCompletionStateError("CompletionBundle exact type이 필요합니다.")
    assert isinstance(bundle, CompletionBundle)
    try:
        completion = AnswerCompletion.model_validate(
            bundle.completion.model_dump(mode="python", round_trip=True), strict=True
        )
        request = QuestionRequest.model_validate(
            bundle.request.model_dump(mode="python", round_trip=True), strict=True
        )
        answer_record = AnswerRecord.model_validate(
            {name: getattr(bundle.answer_record, name) for name in AnswerRecord.model_fields},
            strict=True,
        )
        terminal_audit = TerminalAnswerAudit.model_validate(
            bundle.terminal_audit.model_dump(mode="python", round_trip=True), strict=True
        )
        turn = bundle.session_turn
        session_turn = (
            None
            if turn is None
            else SessionTurn(
                question=turn.question,
                answer_text=turn.answer_text,
                answered_by=turn.answered_by,
                at=turn.at,
                request_id=turn.request_id,
            )
        )
        delivery = DeliveryOutboxEntry.model_validate(
            bundle.delivery.model_dump(mode="python", round_trip=True), strict=True
        )
        return CompletionBundle(
            completion=completion,
            request=request,
            answer_record=answer_record,
            terminal_audit=terminal_audit,
            session_turn=session_turn,
            delivery=delivery,
        )
    except Exception as error:
        raise IncompleteCompletionStateError(
            "Completion artifact canonical validation에 실패했습니다."
        ) from error


CompletionHandoff: TypeAlias = FinalizationCandidate | ApprovedCandidate


def canonical_completion_handoff(raw: object) -> CompletionHandoff:
    """공개 sealed handoff를 plain-data 복사본으로 strict 재검증한다."""
    try:
        if type(raw) is FinalizationCandidate:
            assert isinstance(raw, FinalizationCandidate)
            return FinalizationCandidate.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        if type(raw) is ApprovedCandidate:
            assert isinstance(raw, ApprovedCandidate)
            return ApprovedCandidate.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
    except Exception as error:
        raise InvalidCompletionHandoffError(
            "Completion handoff canonical validation에 실패했습니다."
        ) from error
    raise InvalidCompletionHandoffError(
        "FinalizationCandidate 또는 ApprovedCandidate만 허용합니다."
    )


def _canonical_completion_request(raw: QuestionRequest) -> QuestionRequest:
    try:
        return QuestionRequest.model_validate(
            raw.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception as error:
        raise CompletionEvidenceError(
            "Question Request aggregate canonical validation에 실패했습니다."
        ) from error


class ResponsibilitySnapshotResolver(Protocol):
    """현재 Agent Card·Owner 귀속만 푼다.

    ``needs_correction_review``의 유일한 판정 원천은 ApprovalPolicy의
    ``NoApprovalRequired`` evidence이며 resolver가 자체 판정해 올 수 없다.
    """

    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot | None: ...


class QuestionCompletionReader(Protocol):
    def by_request(self, request_id: str) -> CompletionBundle | None: ...

    def by_record(self, record_id: str) -> CompletionBundle | None: ...


class ApprovalCompletionReader(Protocol):
    """승인 Completion 검증에만 필요한 immutable assignment reader."""

    def get(self, item_id: str) -> ApprovalItem | None: ...

    def get_by_request_attempt(self, request_id: str, attempt: int) -> ApprovalItem | None: ...

    def get_by_request_attempt_round(
        self, request_id: str, attempt: int, approval_round: int
    ) -> ApprovalItem | None: ...


class QuestionCompletionUnitOfWork(QuestionCompletionReader, Protocol):
    def complete(self, handoff: CompletionHandoff) -> AnswerCompletion: ...


@dataclass(frozen=True)
class CompletionPlan:
    """검증된 handoff와 Request snapshot으로 만든 exact completion 계획."""

    handoff: CompletionHandoff
    expected_request: QuestionRequest
    bundle: CompletionBundle


class QuestionCompletionPlanner:
    """승인·책임·시각 증거를 검증해 persistence 독립적인 bundle을 만든다."""

    def __init__(
        self,
        *,
        policy: ApprovalPolicy | None,
        approvals: ApprovalStore | None,
        responsibility_resolver: ResponsibilitySnapshotResolver | None,
        record_id_factory: RecordIdFactory,
        clock: Clock,
    ) -> None:
        if policy is None or approvals is None or responsibility_resolver is None:
            raise CompletionDependencyError(
                "Finalization에는 ApprovalPolicy·ApprovalStore·책임 resolver가 필요합니다."
            )
        self._policy = policy
        self._approvals = approvals
        self._responsibility_resolver = responsibility_resolver
        self._record_id_factory = record_id_factory
        self._clock = clock

    def matches_dependencies(
        self,
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> bool:
        """surface composition이 Approval/책임 단일 원천 identity를 검증하는 손잡이."""
        return (
            self._policy is policy
            and self._approvals is approvals
            and self._responsibility_resolver is responsibility_resolver
        )

    def plan(
        self,
        request: QuestionRequest,
        canonical_handoff: CompletionHandoff,
        *,
        checkpoint: CompletionArtifactCheckpoint | None = None,
    ) -> CompletionPlan:
        expected_request = _canonical_completion_request(request)
        handoff = canonical_completion_handoff(canonical_handoff)
        if expected_request.request_id != handoff.request_id:
            raise CompletionEvidenceError("Completion handoff가 대상 Question Request와 다릅니다.")
        if isinstance(expected_request.state, AnsweredRequest):
            raise IncompleteCompletionStateError(
                "AnsweredRequest에 completion artifact가 없습니다."
            )
        if expected_request.is_terminal:
            raise CompletionEvidenceError(
                "이미 다른 terminal outcome인 Request는 답으로 최종화할 수 없습니다."
            )

        route, attempt, candidate, approval = self._validate_handoff(
            expected_request,
            handoff,
        )
        responsibility = self._resolve_responsibility(
            expected_request,
            route,
            approval,
        )
        completed_at = self._completion_time(expected_request, approval)
        record_id = self._new_record_id()
        final_mode: AnswerMode = (
            "full"
            if isinstance(handoff, ApprovedCandidate) and candidate.mode == "draft_only"
            else candidate.mode
        )
        review_status: Literal["not_required", "approved"] = (
            "approved" if isinstance(approval, HumanApprovalEvidence) else "not_required"
        )
        completion = AnswerCompletion(
            request_id=expected_request.request_id,
            record_id=record_id,
            text=candidate.text,
            answered_by=responsibility.owner_id,
            agent_id=responsibility.agent_id,
            mode=final_mode,
            sources=candidate.sources,
            snapshot_sha=candidate.snapshot_sha,
            review_status=review_status,
            completed_at=completed_at,
        )
        record = AnswerRecord.for_request(
            request_id=expected_request.request_id,
            record_id=record_id,
            question=expected_request.question,
            answer_text=candidate.text,
            answered_by=responsibility.owner_id,
            agent_id=responsibility.agent_id,
            mode=final_mode,
            sources=candidate.sources,
            snapshot_sha=candidate.snapshot_sha,
            session_id=expected_request.session_id,
            answered_at=completed_at,
            needs_correction_review=responsibility.needs_correction_review,
        )
        self._checkpoint(checkpoint, "after_answer_record")
        answered = expected_request.transition(
            AnsweredRequest(record_id=record_id),
            clock=lambda: completed_at,
        )
        self._checkpoint(checkpoint, "after_request")
        audit = TerminalAnswerAudit(
            request_id=expected_request.request_id,
            record_id=record_id,
            org_id=expected_request.org_id,
            requester_id=expected_request.requester_id,
            attempt=attempt,
            route=route,
            responsibility=responsibility,
            candidate_mode=candidate.mode,
            final_mode=final_mode,
            approval=approval,
            completed_at=completed_at,
        )
        self._checkpoint(checkpoint, "after_audit")
        turn = (
            None
            if expected_request.session_id is None
            else SessionTurn.for_request(
                request_id=expected_request.request_id,
                question=expected_request.question,
                answer_text=candidate.text,
                answered_by=responsibility.agent_id,
                at=completed_at,
            )
        )
        self._checkpoint(checkpoint, "after_session")
        delivery = DeliveryOutboxEntry(
            request_id=expected_request.request_id,
            record_id=record_id,
            created_at=completed_at,
        )
        self._checkpoint(checkpoint, "after_outbox")
        bundle = canonical_completion_bundle(
            CompletionBundle(
                completion=completion,
                request=answered,
                answer_record=record,
                terminal_audit=audit,
                session_turn=turn,
                delivery=delivery,
            )
        )
        return CompletionPlan(
            handoff=canonical_completion_handoff(handoff),
            expected_request=_canonical_completion_request(expected_request),
            bundle=bundle,
        )

    def plan_with_approval_reader(
        self,
        request: QuestionRequest,
        canonical_handoff: CompletionHandoff,
        *,
        approval_reader: ApprovalCompletionReader,
    ) -> CompletionPlan:
        """한 UoW 호출에만 주입되는 reader로 승인 handoff를 검증한다.

        shared planner의 ApprovalStore를 바꾸지 않아, 다른 command로 authority가
        새거나 in-memory fallback으로 돌아가는 일을 막는다.
        """
        if not isinstance(canonical_handoff, ApprovedCandidate):
            return self.plan(request, canonical_handoff)
        scoped = QuestionCompletionPlanner(
            policy=self._policy,
            approvals=approval_reader,  # type: ignore[arg-type]
            responsibility_resolver=self._responsibility_resolver,
            record_id_factory=self._record_id_factory,
            clock=self._clock,
        )
        return scoped.plan(request, canonical_handoff)

    def _validate_handoff(
        self,
        request: QuestionRequest,
        handoff: CompletionHandoff,
    ) -> tuple[RouteTarget, int, AnswerCandidate, ApprovalEvidence]:
        match handoff:
            case FinalizationCandidate():
                return self._validate_no_approval(request, handoff)
            case ApprovedCandidate():
                return self._validate_approved(request, handoff)
            case _ as never:
                assert_never(never)

    def _validate_no_approval(
        self,
        request: QuestionRequest,
        handoff: FinalizationCandidate,
    ) -> tuple[RouteTarget, int, AnswerCandidate, NoApprovalEvidence]:
        state = request.state
        if not isinstance(state, (ReadyToDispatch, AwaitingAnswer)):
            raise CompletionEvidenceError(
                "승인 불필요 후보는 ReadyToDispatch/AwaitingAnswer에서만 확정합니다."
            )
        if (
            request.revision != handoff.expected_revision
            or state.route != handoff.route
            or state.attempt != handoff.attempt
        ):
            raise CompletionEvidenceError(
                "FinalizationCandidate가 현재 Request revision/route/attempt와 다릅니다."
            )
        if handoff.route.requires_approval or handoff.candidate.mode == "draft_only":
            raise CompletionEvidenceError("Approval이 필요한 후보는 직접 최종화할 수 없습니다.")
        try:
            raw = self._policy.evaluate(
                request.org_id,
                handoff.route,
                handoff.candidate.mode,
            )
            if not isinstance(raw, NoApprovalRequired):
                raise CompletionEvidenceError("현재 중앙 ApprovalPolicy가 사람 승인을 요구합니다.")
            current = NoApprovalRequired(
                kind=raw.kind,
                policy_version=raw.policy_version,
                needs_correction_review=raw.needs_correction_review,
            )
        except CompletionEvidenceError:
            raise
        except Exception as error:
            raise CompletionEvidenceError(
                "현재 중앙 ApprovalPolicy를 검증할 수 없습니다."
            ) from error
        if current != handoff.approval_evaluation:
            raise CompletionEvidenceError(
                "FinalizationCandidate의 Approval policy version이 현재 정책과 다릅니다."
            )
        return (
            state.route,
            state.attempt,
            handoff.candidate,
            NoApprovalEvidence(
                policy_version=current.policy_version,
                needs_correction_review=current.needs_correction_review,
            ),
        )

    def _validate_approved(
        self,
        request: QuestionRequest,
        handoff: ApprovedCandidate,
    ) -> tuple[RouteTarget, int, AnswerCandidate, HumanApprovalEvidence]:
        state = request.state
        if not isinstance(state, AwaitingApproval):
            raise CompletionEvidenceError("사람 승인 후보는 AwaitingApproval에서만 확정합니다.")
        if (
            request.revision != handoff.expected_revision
            or state.draft_ref != handoff.item_id
            or state.route != handoff.route
            or state.attempt != handoff.attempt
        ):
            raise CompletionEvidenceError(
                "ApprovedCandidate가 현재 Request/item/revision/route/attempt와 다릅니다."
            )
        try:
            raw = self._approvals.get(handoff.item_id)
        except Exception as error:
            raise CompletionEvidenceError(
                "ApprovalItem 저장값을 읽거나 검증할 수 없습니다."
            ) from error
        if raw is None:
            raise CompletionNotFoundError(f"ApprovalItem이 없습니다: {handoff.item_id!r}")
        if type(raw) is not ApprovalItem:
            raise CompletionEvidenceError("ApprovalItem 저장값이 손상됐습니다.")
        try:
            item = ApprovalItem.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise CompletionEvidenceError("ApprovalItem 저장값이 손상됐습니다.") from error
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
            if type(raw_current) is not ApprovalItem or type(raw_round) is not ApprovalItem:
                raise CompletionEvidenceError("ApprovalItem current/round index가 손상됐습니다.")
            current = ApprovalItem.model_validate(
                raw_current.model_dump(mode="python", round_trip=True),
                strict=True,
            )
            round_item = ApprovalItem.model_validate(
                raw_round.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except CompletionEvidenceError:
            raise
        except Exception as error:
            raise CompletionEvidenceError(
                "ApprovalItem current/round index를 검증할 수 없습니다."
            ) from error
        if current != item or round_item != item:
            raise CompletionEvidenceError(
                "ApprovalItem get/current/round snapshot이 exact-link되지 않습니다."
            )
        if not handoff.assignment_generation.matches_item(item):
            raise CompletionEvidenceError(
                "ApprovedCandidate의 assignment generation이 현재 ApprovalItem과 다릅니다."
            )
        resolution = item.resolution
        if (
            item.item_id != handoff.item_id
            or item.org_id != request.org_id
            or item.request_id != request.request_id
            or item.awaiting_revision != request.revision
            or item.attempt != handoff.attempt
            or item.route != handoff.route
            or item.due_at != state.handling.due_at
            or item.status != "resolved"
            or resolution is None
            or resolution.approved_candidate != handoff
            or not isinstance(resolution.action, (Approve, ApproveWithEdit))
        ):
            raise CompletionEvidenceError(
                "ApprovedCandidate가 resolved ApprovalItem의 exact snapshot이 아닙니다."
            )
        return (
            item.route,
            item.attempt,
            handoff.candidate,
            HumanApprovalEvidence(
                item_id=item.item_id,
                action=resolution.action.kind,
                approved_by=handoff.approved_by,
                approved_at=handoff.approved_at,
                policy_version=handoff.policy_version,
            ),
        )

    def _resolve_responsibility(
        self,
        request: QuestionRequest,
        route: RouteTarget,
        approval: ApprovalEvidence,
    ) -> AnswerResponsibilitySnapshot:
        try:
            raw = self._responsibility_resolver.resolve(
                org_id=request.org_id,
                route=route,
            )
            if raw is None:
                raise CompletionAttributionError("책임 snapshot이 없습니다.")
            if raw.needs_correction_review:
                raise CompletionAttributionError(
                    "책임 resolver는 사후교정 필요 여부를 자체 판정할 수 없습니다."
                )
            snapshot = AnswerResponsibilitySnapshot(
                agent_id=raw.agent_id,
                owner_id=raw.owner_id,
                needs_correction_review=(
                    approval.needs_correction_review
                    if isinstance(approval, NoApprovalEvidence)
                    else False
                ),
            )
        except CompletionAttributionError:
            raise
        except Exception as error:
            raise CompletionAttributionError(
                "책임 Agent Card와 Owner snapshot을 검증할 수 없습니다."
            ) from error
        if snapshot.agent_id != route.agent_id:
            raise CompletionAttributionError("책임 snapshot의 Agent Card가 RouteTarget과 다릅니다.")
        return snapshot

    def _completion_time(
        self,
        request: QuestionRequest,
        approval: ApprovalEvidence,
    ) -> datetime:
        try:
            now = _aware(self._clock(), "Finalization clock")
        except Exception as error:
            raise CompletionClockError("Finalization clock이 유효하지 않습니다.") from error
        if now < request.updated_at:
            raise CompletionClockError("Finalization clock은 Request보다 역행할 수 없습니다.")
        if isinstance(approval, HumanApprovalEvidence) and now < approval.approved_at:
            raise CompletionClockError("Finalization clock은 Approval보다 역행할 수 없습니다.")
        return now

    def _new_record_id(self) -> str:
        return self._validate_record_id(self._record_id_factory())

    @staticmethod
    def _checkpoint(
        checkpoint: CompletionArtifactCheckpoint | None,
        point: CompletionArtifactCheckpointPoint,
    ) -> None:
        if checkpoint is not None:
            checkpoint(point)

    @staticmethod
    def _validate_record_id(raw: object) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise CompletionIdCollisionError("record ID는 nonblank 문자열이어야 합니다.")
        return raw


@dataclass(frozen=True)
class _CompletionState:
    requests: dict[str, QuestionRequest]
    records_by_id: dict[str, AnswerRecord]
    record_id_by_request: dict[str, str]
    audits_by_request: dict[str, TerminalAnswerAudit]
    turns_by_request: dict[str, SessionTurn]
    outbox_by_request: dict[str, DeliveryOutboxEntry]
    completions_by_request: dict[str, AnswerCompletion]
    handoffs_by_request: dict[str, CompletionHandoff]


def _empty_state() -> _CompletionState:
    return _CompletionState(
        requests={},
        records_by_id={},
        record_id_by_request={},
        audits_by_request={},
        turns_by_request={},
        outbox_by_request={},
        completions_by_request={},
        handoffs_by_request={},
    )


class InMemoryQuestionCompletionUnitOfWork:
    """QuestionRequestStore·completion UoW·reader를 공유 state로 제공한다."""

    workflow_durability: Literal["ephemeral", "durable"] = "ephemeral"
    question_completion_storage_capability: Literal["atomic_v1"] = "atomic_v1"

    def __init__(
        self,
        *,
        policy: ApprovalPolicy | None,
        approvals: ApprovalStore | None,
        responsibility_resolver: ResponsibilitySnapshotResolver | None,
        record_id_factory: RecordIdFactory,
        clock: Clock,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._planner = QuestionCompletionPlanner(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=record_id_factory,
            clock=clock,
        )
        self._fault_injector = fault_injector
        self._state = _empty_state()
        self._lock = RLock()
        self._completion_in_progress = False

    def matches_question_completion_dependencies(
        self,
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> bool:
        """ApprovalBoundary와 Finalization이 같은 정책·Store·책임 resolver를 보는지 판정."""
        return self._planner.matches_dependencies(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
        )

    def create(self, request: QuestionRequest) -> QuestionRequest:
        request = self._canonical_request(request)
        validate_new_question_request_semantics(request)
        with self._lock:
            self._reject_reentrant_mutation()
            if request.request_id in self._state.requests:
                raise DuplicateQuestionRequestError(
                    f"이미 존재하는 Question Request: {request.request_id!r}"
                )
            requests = dict(self._state.requests)
            requests[request.request_id] = request
            self._state = self._replace_state(requests=requests)
            return self._canonical_request(request)

    def get(self, request_id: str) -> QuestionRequest | None:
        with self._lock:
            request = self._state.requests.get(request_id)
            return None if request is None else self._canonical_request(request)

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        current = self._canonical_request(current)
        updated = self._canonical_request(updated)
        validate_compare_and_set_semantics(
            request_id,
            expected_revision,
            current,
            updated,
        )
        if isinstance(updated.state, AnsweredRequest):
            raise DirectAnsweredTransitionError(
                "AnsweredRequest는 QuestionCompletionUnitOfWork만 확정할 수 있습니다."
            )
        with self._lock:
            self._reject_reentrant_mutation()
            stored = self._state.requests.get(request_id)
            if stored is None or stored.revision != expected_revision or stored != current:
                return False
            requests = dict(self._state.requests)
            requests[request_id] = updated
            self._state = self._replace_state(requests=requests)
            return True

    def nonterminal(self) -> list[QuestionRequest]:
        with self._lock:
            snapshot = [
                self._canonical_request(request)
                for request in self._state.requests.values()
                if not request.is_terminal
            ]
        return sorted(snapshot, key=lambda item: (item.created_at, item.request_id))

    def by_request(self, request_id: str) -> CompletionBundle | None:
        with self._lock:
            if request_id not in self._state.record_id_by_request:
                request = self._state.requests.get(request_id)
                if request is not None and isinstance(request.state, AnsweredRequest):
                    raise IncompleteCompletionStateError(
                        "AnsweredRequest에 completion record index가 없습니다."
                    )
                return None
            return self._bundle_for_request(request_id)

    def by_record(self, record_id: str) -> CompletionBundle | None:
        with self._lock:
            record = self._state.records_by_id.get(record_id)
            if record is None:
                referenced = (
                    record_id in self._state.record_id_by_request.values()
                    or any(
                        isinstance(request.state, AnsweredRequest)
                        and request.state.record_id == record_id
                        for request in self._state.requests.values()
                    )
                    or any(
                        audit.record_id == record_id
                        for audit in self._state.audits_by_request.values()
                    )
                    or any(
                        delivery.record_id == record_id
                        for delivery in self._state.outbox_by_request.values()
                    )
                    or any(
                        completion.record_id == record_id
                        for completion in self._state.completions_by_request.values()
                    )
                )
                if referenced:
                    raise IncompleteCompletionStateError(
                        "AnswerRecord는 없지만 native completion artifact가 record ID를 참조합니다."
                    )
                return None
            if record.record_id != record_id or record.request_id is None:
                raise IncompleteCompletionStateError(
                    "AnswerRecord 단건 조회 키·request 상관키가 손상됐습니다."
                )
            bundle = self._bundle_for_request(record.request_id)
            if bundle.completion.record_id != record_id:
                raise IncompleteCompletionStateError(
                    "record lookup key와 Request completion index가 다릅니다."
                )
            return bundle

    def answer_record(self, record_id: str) -> AnswerRecord | None:
        """completion exact-link를 전부 검증한 다음 AnswerRecord만 투영한다."""
        bundle = self.by_record(record_id)
        return None if bundle is None else bundle.answer_record

    def answer_records_for_agent(self, agent_id: str) -> list[AnswerRecord]:
        """전 completion artifact를 exact-read한 뒤 카드별 기록을 돌려준다."""
        with self._lock:
            answered_pairs: set[tuple[str, str]] = set()
            expected_turn_requests: set[str] = set()
            for request_id, request in self._state.requests.items():
                canonical = self._canonical_request(request)
                if canonical.request_id != request_id:
                    raise IncompleteCompletionStateError(
                        "Question Request 인덱스와 request_id가 다릅니다."
                    )
                if isinstance(canonical.state, AnsweredRequest):
                    answered_pairs.add((request_id, canonical.state.record_id))
                    if canonical.session_id is not None:
                        expected_turn_requests.add(request_id)

            index_pairs = set(self._state.record_id_by_request.items())
            record_pairs: set[tuple[str, str]] = set()
            for record_id, raw in self._state.records_by_id.items():
                if raw.record_id != record_id or raw.request_id is None:
                    raise IncompleteCompletionStateError(
                        "AnswerRecord 인덱스·request 상관키가 손상됐습니다."
                    )
                record_pairs.add((raw.request_id, record_id))

            native_requests = {request_id for request_id, _ in answered_pairs}
            if not (
                answered_pairs == index_pairs == record_pairs
                and set(self._state.audits_by_request) == native_requests
                and set(self._state.outbox_by_request) == native_requests
                and set(self._state.completions_by_request) == native_requests
                and set(self._state.handoffs_by_request) == native_requests
                and set(self._state.turns_by_request) == expected_turn_requests
            ):
                raise IncompleteCompletionStateError(
                    "AnswerRecord 목록의 completion artifact 집합이 exact-link되지 않습니다."
                )

            records: list[AnswerRecord] = []
            for request_id, record_id in sorted(answered_pairs):
                canonical_completion_handoff(self._state.handoffs_by_request[request_id])
                bundle = self._bundle_for_request(request_id)
                if bundle.answer_record.record_id != record_id:
                    raise IncompleteCompletionStateError(
                        "AnswerRecord ID와 completion bundle이 exact-link되지 않습니다."
                    )
                records.append(bundle.answer_record)
        return sorted(
            (record for record in records if record.agent_id == agent_id),
            key=lambda record: (record.answered_at, record.record_id),
        )

    def complete(self, handoff: object) -> AnswerCompletion:
        canonical = canonical_completion_handoff(handoff)
        with self._completion_scope():
            existing_record_id = self._state.record_id_by_request.get(canonical.request_id)
            if existing_record_id is not None:
                stored_handoff = self._state.handoffs_by_request.get(canonical.request_id)
                if stored_handoff is None:
                    raise IncompleteCompletionStateError(
                        "확정된 completion의 idempotency handoff가 없습니다."
                    )
                if stored_handoff != canonical:
                    raise CompletionConcurrencyError(
                        "같은 Question Request에 다른 Finalization 후보가 이미 확정됐습니다."
                    )
                return self._bundle_for_request(canonical.request_id).completion

            request = self._state.requests.get(canonical.request_id)
            if request is None:
                raise CompletionNotFoundError(
                    f"Question Request가 없습니다: {canonical.request_id!r}"
                )
            if isinstance(request.state, AnsweredRequest):
                raise IncompleteCompletionStateError(
                    "AnsweredRequest에 completion artifact가 없습니다."
                )
            if request.is_terminal:
                raise CompletionEvidenceError(
                    "이미 다른 terminal outcome인 Request는 답으로 최종화할 수 없습니다."
                )

            plan = self._planner.plan(
                request,
                canonical,
                checkpoint=self._fault,
            )
            self._fault("before_commit")
            self._commit(
                plan.handoff,
                plan.bundle,
                expected_request=plan.expected_request,
            )
            return plan.bundle.completion

    def _commit(
        self,
        handoff: CompletionHandoff,
        bundle: CompletionBundle,
        *,
        expected_request: QuestionRequest,
    ) -> None:
        request_id = bundle.completion.request_id
        record_id = bundle.completion.record_id
        if self._state.requests.get(request_id) != expected_request:
            raise CompletionConcurrencyError(
                "Finalization 중 Question Request snapshot이 바뀌었습니다."
            )
        existing = self._state.records_by_id.get(record_id)
        if existing is not None:
            raise CompletionIdCollisionError(f"record ID가 이미 존재합니다: {record_id!r}")
        residual_indexes = (
            request_id in self._state.audits_by_request,
            request_id in self._state.turns_by_request,
            request_id in self._state.outbox_by_request,
            request_id in self._state.completions_by_request,
            request_id in self._state.handoffs_by_request,
        )
        if any(residual_indexes):
            raise IncompleteCompletionStateError(
                "record index 없이 일부 completion artifact가 이미 존재합니다."
            )

        # Pydantic frozen model도 object.__setattr__로 강제 변조할 수 있다. 저장 객체를
        # 호출자가 보유한 local bundle과 분리해 public 반환값이 backing state의 alias가
        # 되지 않도록 한다.
        stored_bundle = self._canonical_bundle(bundle)
        stored_handoff = canonical_completion_handoff(handoff)

        requests = dict(self._state.requests)
        records = dict(self._state.records_by_id)
        record_ids = dict(self._state.record_id_by_request)
        audits = dict(self._state.audits_by_request)
        turns = dict(self._state.turns_by_request)
        outbox = dict(self._state.outbox_by_request)
        completions = dict(self._state.completions_by_request)
        handoffs = dict(self._state.handoffs_by_request)
        requests[request_id] = stored_bundle.request
        records[record_id] = stored_bundle.answer_record
        record_ids[request_id] = record_id
        audits[request_id] = stored_bundle.terminal_audit
        if stored_bundle.session_turn is not None:
            turns[request_id] = stored_bundle.session_turn
        outbox[request_id] = stored_bundle.delivery
        completions[request_id] = stored_bundle.completion
        handoffs[request_id] = stored_handoff
        self._state = _CompletionState(
            requests=requests,
            records_by_id=records,
            record_id_by_request=record_ids,
            audits_by_request=audits,
            turns_by_request=turns,
            outbox_by_request=outbox,
            completions_by_request=completions,
            handoffs_by_request=handoffs,
        )

    def _bundle_for_request(self, request_id: str) -> CompletionBundle:
        try:
            record_id = self._state.record_id_by_request[request_id]
            request = self._state.requests[request_id]
            record = self._state.records_by_id[record_id]
            audit = self._state.audits_by_request[request_id]
            delivery = self._state.outbox_by_request[request_id]
            completion = self._state.completions_by_request[request_id]
        except KeyError as error:
            raise IncompleteCompletionStateError(
                "Question Request의 completion artifact 집합이 불완전합니다."
            ) from error
        turn = self._state.turns_by_request.get(request_id)
        if request.session_id is not None and turn is None:
            raise IncompleteCompletionStateError(
                "session_id가 있는 Request의 SessionTurn이 없습니다."
            )
        try:
            bundle = CompletionBundle(
                completion=completion,
                request=request,
                answer_record=record,
                terminal_audit=audit,
                session_turn=turn,
                delivery=delivery,
            )
            return self._canonical_bundle(bundle)
        except Exception as error:
            if isinstance(error, IncompleteCompletionStateError):
                raise
            raise IncompleteCompletionStateError(
                "저장된 completion artifact의 exact-link 검증에 실패했습니다."
            ) from error

    @staticmethod
    def _canonical_request(request: QuestionRequest) -> QuestionRequest:
        return _canonical_completion_request(request)

    @staticmethod
    def _canonical_bundle(bundle: CompletionBundle) -> CompletionBundle:
        return canonical_completion_bundle(bundle)

    def _fault(self, point: CompletionFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @contextmanager
    def _completion_scope(self) -> Generator[None]:
        with self._lock:
            if self._completion_in_progress:
                raise ReentrantCompletionMutationError(
                    "Finalization 중 같은 completion state에 재진입할 수 없습니다."
                )
            self._completion_in_progress = True
            try:
                yield
            finally:
                self._completion_in_progress = False

    def _reject_reentrant_mutation(self) -> None:
        if self._completion_in_progress:
            raise ReentrantCompletionMutationError(
                "Finalization callback은 Question Request state를 변경할 수 없습니다."
            )

    def _replace_state(
        self,
        *,
        requests: dict[str, QuestionRequest],
    ) -> _CompletionState:
        return _CompletionState(
            requests=requests,
            records_by_id=self._state.records_by_id,
            record_id_by_request=self._state.record_id_by_request,
            audits_by_request=self._state.audits_by_request,
            turns_by_request=self._state.turns_by_request,
            outbox_by_request=self._state.outbox_by_request,
            completions_by_request=self._state.completions_by_request,
            handoffs_by_request=self._state.handoffs_by_request,
        )
