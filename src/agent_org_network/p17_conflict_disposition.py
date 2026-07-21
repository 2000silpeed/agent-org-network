"""Request-aware Conflict disposition과 direct consensus 재개(ADR 0046 S1~S2).

legacy ``ConsensusService``와 분리해 한 Question Request의 concurrence 진행과
generation-bound 결론만 다룬다. Router·Precedent·ComplementEdge는 이 모듈의
의존성이 아니다.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from threading import RLock
from typing import Annotated, Literal, Protocol, TypeAlias, final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from agent_org_network.agent_card import AgentCard, domain_authorized
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.answer_finalization import (
    QuestionCompletionReader,
    canonical_completion_bundle,
)
from agent_org_network.manager_queue import (
    FromDeadlock,
    ManagerQueueStore,
    ManagerItem,
    RequestAwareManagerQueueStore,
    deadlock_reason,
)
from agent_org_network.governance_authorization import (
    authorize_and_verify,
    canonical_authenticated_principal,
)
from agent_org_network.conflict import (
    Candidate,
    CandidateRegistryChanged,
    ConflictCase,
    ConflictEscalationCause,
    DivergentVotes,
    InMemoryConflictCaseStore,
    RequestAwareConflictCaseStore,
    Resolution,
)
from agent_org_network.p17_manager_disposition import (
    DeadlockManagerSealedClaimHandle,
    ExecutionAlreadyRunning,
    ExecutionDeferred,
    ExecutionNotNeeded,
    ExecutionStarted,
    ExecutionWake,
    QuestionExecutionStarter,
    SealedDeadlockAssignClaim,
    SealedDeadlockDismissClaim,
)
from agent_org_network.question_request import RouteTarget
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingApproval,
    AwaitingConflict,
    AwaitingManager,
    FailedRequest,
    HandlingAssignment,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
)
from agent_org_network.question_resolution import (
    AuthorityGrant,
    HandlingDeadlinePolicy,
    RequestLockPool,
)
from agent_org_network.registry import Registry
from agent_org_network.request_route_authority import (
    FromOwnerConsensusGrant,
    RequestRouteGrantAssignment,
    RequestRouteGrantConflict,
    RequestRouteGrantReceipt,
    RequestRouteGrantResult,
    RequestRouteAuthority,
    RequestRouteGrantRejected,
)

IdFactory: TypeAlias = Callable[[], object]
_CandidateRegistryReason: TypeAlias = Literal[
    "candidate_missing",
    "owner_missing",
    "owner_changed",
    "under_claim_changed",
]
_REGISTRY_DRIFT_PRIORITY: tuple[_CandidateRegistryReason, ...] = (
    "candidate_missing",
    "owner_missing",
    "owner_changed",
    "under_claim_changed",
)


class _FrozenDto(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @field_validator("*", mode="after")
    @classmethod
    def _strings_must_be_nonblank(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and not value.strip() and info.field_name != "rationale":
            raise ValueError("문자열 값은 비어 있거나 공백일 수 없습니다.")
        return value


@final
class OwnerPrincipal(_FrozenDto):
    org_id: str
    subject_id: str


ConflictOperationsPrincipal: TypeAlias = OwnerPrincipal | AuthenticatedPrincipal


@final
class ConcurOnConflict(_FrozenDto):
    kind: Literal["concur_on_conflict"] = "concur_on_conflict"
    principal: ConflictOperationsPrincipal
    case_id: str
    expected_round: int = Field(ge=1)
    on_agent: str
    stance: Literal["withdraw", "keep_as_complement"] = "withdraw"
    rationale: str = ""


@final
class OwnerConcurrenceEvidence(_FrozenDto):
    round: int = Field(ge=1)
    owner_id: str
    on_agent: str
    stance: Literal["withdraw", "keep_as_complement"]
    rationale: str = ""


@final
class ConcurrenceActionFingerprint(_FrozenDto):
    case_id: str
    org_id: str
    owner_id: str
    expected_round: int = Field(ge=1)
    on_agent: str
    stance: Literal["withdraw", "keep_as_complement"]
    rationale: str


class _ClaimDto(_FrozenDto):
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    org_id: str
    intent: str
    round: int = Field(ge=1)
    candidate_snapshot: tuple[Candidate, ...]
    votes: tuple[OwnerConcurrenceEvidence, ...]
    trigger: ConcurrenceActionFingerprint

    @model_validator(mode="after")
    def _common_claim_shape(self) -> _ClaimDto:
        if self.idempotency_key != f"conflict-disposition:{self.case_id}:{self.round}":
            raise ValueError("Conflict claim idempotency key가 Case round와 다릅니다.")
        owner_order = tuple(dict.fromkeys(candidate.owner for candidate in self.candidate_snapshot))
        vote_owners = tuple(vote.owner_id for vote in self.votes)
        if any(vote.round != self.round for vote in self.votes):
            raise ValueError("Conflict claim vote round가 claim round와 다릅니다.")
        if len(vote_owners) != len(set(vote_owners)):
            raise ValueError("Conflict claim vote Owner는 유일해야 합니다.")
        if vote_owners != tuple(owner for owner in owner_order if owner in vote_owners):
            raise ValueError("Conflict claim vote 순서는 candidate Owner 순서여야 합니다.")
        if (
            self.trigger.case_id != self.case_id
            or self.trigger.org_id != self.org_id
            or self.trigger.expected_round != self.round
        ):
            raise ValueError("Conflict claim trigger가 Case·조직·round와 다릅니다.")
        return self


@final
class ReservedConsensusClaim(_ClaimDto):
    kind: Literal["reserved_consensus"] = "reserved_consensus"
    primary: str
    requires_approval: bool

    @model_validator(mode="after")
    def _consensus_shape(self) -> ReservedConsensusClaim:
        owner_order = tuple(dict.fromkeys(c.owner for c in self.candidate_snapshot))
        if tuple(vote.owner_id for vote in self.votes) != owner_order:
            raise ValueError("Consensus claim에는 candidate Owner 전원의 vote가 필요합니다.")
        if not self.votes or any(vote.on_agent != self.primary for vote in self.votes):
            raise ValueError("Consensus claim의 모든 vote target은 primary여야 합니다.")
        if self.primary not in tuple(candidate.agent_id for candidate in self.candidate_snapshot):
            raise ValueError("Consensus primary는 원 후보여야 합니다.")
        if not _trigger_is_vote(self.trigger, self.votes):
            raise ValueError("Consensus trigger는 canonical votes에 포함돼야 합니다.")
        return self


@final
class SealedConsensusClaim(_ClaimDto):
    kind: Literal["sealed_consensus"] = "sealed_consensus"
    primary: str
    requires_approval: bool

    @model_validator(mode="after")
    def _consensus_shape(self) -> SealedConsensusClaim:
        owner_order = tuple(dict.fromkeys(c.owner for c in self.candidate_snapshot))
        if tuple(vote.owner_id for vote in self.votes) != owner_order:
            raise ValueError("Consensus claim에는 candidate Owner 전원의 vote가 필요합니다.")
        if not self.votes or any(vote.on_agent != self.primary for vote in self.votes):
            raise ValueError("Consensus claim의 모든 vote target은 primary여야 합니다.")
        if self.primary not in tuple(candidate.agent_id for candidate in self.candidate_snapshot):
            raise ValueError("Consensus primary는 원 후보여야 합니다.")
        if not _trigger_is_vote(self.trigger, self.votes):
            raise ValueError("Consensus trigger는 canonical votes에 포함돼야 합니다.")
        return self


@final
class ReservedDeadlockClaim(_ClaimDto):
    kind: Literal["reserved_deadlock"] = "reserved_deadlock"
    cause: ConflictEscalationCause

    @model_validator(mode="after")
    def _deadlock_shape(self) -> ReservedDeadlockClaim:
        if isinstance(self.cause, DivergentVotes):
            owner_order = tuple(dict.fromkeys(c.owner for c in self.candidate_snapshot))
            if tuple(vote.owner_id for vote in self.votes) != owner_order:
                raise ValueError("DivergentVotes claim에는 Owner 전원의 vote가 필요합니다.")
        _validate_deadlock_combination(self.trigger, self.votes, self.cause)
        return self


@final
class SealedDeadlockClaim(_ClaimDto):
    kind: Literal["sealed_deadlock"] = "sealed_deadlock"
    cause: ConflictEscalationCause

    @model_validator(mode="after")
    def _deadlock_shape(self) -> SealedDeadlockClaim:
        if isinstance(self.cause, DivergentVotes):
            owner_order = tuple(dict.fromkeys(c.owner for c in self.candidate_snapshot))
            if tuple(vote.owner_id for vote in self.votes) != owner_order:
                raise ValueError("DivergentVotes claim에는 Owner 전원의 vote가 필요합니다.")
        _validate_deadlock_combination(self.trigger, self.votes, self.cause)
        return self


ConflictDispositionClaim: TypeAlias = Annotated[
    ReservedConsensusClaim | SealedConsensusClaim | ReservedDeadlockClaim | SealedDeadlockClaim,
    Field(discriminator="kind"),
]


def _trigger_is_vote(
    trigger: ConcurrenceActionFingerprint,
    votes: tuple[OwnerConcurrenceEvidence, ...],
) -> bool:
    return any(
        (
            vote.round,
            vote.owner_id,
            vote.on_agent,
            vote.stance,
            vote.rationale,
        )
        == (
            trigger.expected_round,
            trigger.owner_id,
            trigger.on_agent,
            trigger.stance,
            trigger.rationale,
        )
        for vote in votes
    )


def _validate_deadlock_combination(
    trigger: ConcurrenceActionFingerprint,
    votes: tuple[OwnerConcurrenceEvidence, ...],
    cause: ConflictEscalationCause,
) -> None:
    if cause.round != trigger.expected_round:
        raise ValueError("Deadlock cause round가 trigger와 다릅니다.")
    if isinstance(cause, DivergentVotes):
        if len({vote.on_agent for vote in votes}) < 2 or not _trigger_is_vote(trigger, votes):
            raise ValueError("DivergentVotes claim에는 갈린 canonical votes가 필요합니다.")


@final
class ValidatedOwnerVote(_FrozenDto):
    kind: Literal["validated_owner_vote"] = "validated_owner_vote"
    request_id: str
    case_id: str
    org_id: str
    intent: str
    candidate_snapshot: tuple[Candidate, ...]
    trigger: ConcurrenceActionFingerprint
    evidence: OwnerConcurrenceEvidence
    target_requires_approval: bool


@final
class ValidatedRegistryEscalation(_FrozenDto):
    kind: Literal["validated_registry_escalation"] = "validated_registry_escalation"
    request_id: str
    case_id: str
    org_id: str
    intent: str
    candidate_snapshot: tuple[Candidate, ...]
    trigger: ConcurrenceActionFingerprint
    cause: CandidateRegistryChanged


ValidatedConcurrence: TypeAlias = Annotated[
    ValidatedOwnerVote | ValidatedRegistryEscalation,
    Field(discriminator="kind"),
]


@final
class ConflictReservationControlToken(_FrozenDto):
    generation: str
    token: str


@final
class ConflictSealedClaimHandle(_FrozenDto):
    generation: str
    forward_token: str


@final
class ConcurrencePendingStored(_FrozenDto):
    kind: Literal["pending"] = "pending"
    current_round: int = Field(ge=1)
    pending_owners: tuple[str, ...]


@final
class ConflictClaimAcquired(_FrozenDto):
    kind: Literal["acquired"] = "acquired"
    claim: ReservedConsensusClaim
    control_token: ConflictReservationControlToken

    @model_validator(mode="after")
    def _generation_matches(self) -> ConflictClaimAcquired:
        if self.claim.generation != self.control_token.generation:
            raise ValueError("Conflict claim과 control token generation이 다릅니다.")
        return self


@final
class ConflictClaimInProgress(_FrozenDto):
    kind: Literal["in_progress"] = "in_progress"
    retryable: Literal[True] = True


@final
class SealedConflictClaimAvailable(_FrozenDto):
    kind: Literal["sealed"] = "sealed"
    claim: SealedConsensusClaim | SealedDeadlockClaim
    handle: ConflictSealedClaimHandle

    @model_validator(mode="after")
    def _generation_matches(self) -> SealedConflictClaimAvailable:
        if self.claim.generation != self.handle.generation:
            raise ValueError("Sealed Conflict claim과 handle generation이 다릅니다.")
        return self


@final
class ConflictClaimConflict(_FrozenDto):
    kind: Literal["conflict"] = "conflict"


ConflictConcurrenceAttempt: TypeAlias = Annotated[
    ConcurrencePendingStored
    | ConflictClaimAcquired
    | ConflictClaimInProgress
    | SealedConflictClaimAvailable
    | ConflictClaimConflict,
    Field(discriminator="kind"),
]


@final
class SupportingKnowledgeEvidence(_FrozenDto):
    agent_id: str
    affirmed_by_owner: str
    authority: Literal[0] = 0


@final
class FromDirectConsensus(_FrozenDto):
    kind: Literal["direct_consensus"] = "direct_consensus"
    round: int = Field(ge=1)
    votes: tuple[OwnerConcurrenceEvidence, ...]


@final
class FromManagerMediation(_FrozenDto):
    kind: Literal["manager_mediation"] = "manager_mediation"
    item_id: str
    by_manager: str


ConflictResolutionSource: TypeAlias = Annotated[
    FromDirectConsensus | FromManagerMediation,
    Field(discriminator="kind"),
]


@final
class ConflictResolutionEvidence(_FrozenDto):
    request_id: str
    case_id: str
    org_id: str
    intent: str
    route: RouteTarget
    source: ConflictResolutionSource
    supporting: tuple[SupportingKnowledgeEvidence, ...] = ()


class ConflictResolutionEvidenceReader(Protocol):
    """Answer Source가 resolution evidence와 같은 request-aware Case를 읽는 최소 포트."""

    def resolution_evidence_for_request(
        self, request_id: str
    ) -> ConflictResolutionEvidence | None: ...

    def get_request_case(self, case_id: str) -> ConflictCase | None: ...


@final
class ValidatedMediationAssign(_FrozenDto):
    kind: Literal["validated_mediation_assign"] = "validated_mediation_assign"
    conflict_claim: SealedDeadlockClaim
    conflict_handle: ConflictSealedClaimHandle
    manager_claim: SealedDeadlockAssignClaim
    manager_handle: DeadlockManagerSealedClaimHandle
    evidence: ConflictResolutionEvidence

    @model_validator(mode="after")
    def _exact_links(self) -> ValidatedMediationAssign:
        conflict = self.conflict_claim
        manager = self.manager_claim
        evidence = self.evidence
        if (
            self.conflict_handle.generation != conflict.generation
            or self.manager_handle.generation != manager.generation
            or manager.request_id != conflict.request_id
            or manager.case_id != conflict.case_id
            or manager.org_id != conflict.org_id
            or manager.intent != conflict.intent
            or manager.round != conflict.round
            or manager.cause != conflict.cause
            or manager.agent_id
            not in tuple(candidate.agent_id for candidate in conflict.candidate_snapshot)
            or evidence.request_id != conflict.request_id
            or evidence.case_id != conflict.case_id
            or evidence.org_id != conflict.org_id
            or evidence.intent != conflict.intent
            or evidence.route.intent != manager.intent
            or evidence.route.agent_id != manager.agent_id
            or evidence.route.requires_approval != manager.requires_approval
            or evidence.route.authority_version is None
            or not evidence.route.authority_version.strip()
            or type(evidence.source) is not FromManagerMediation
            or evidence.source.item_id != manager.item_id
            or evidence.source.by_manager != manager.by_manager
            or evidence.supporting != ()
        ):
            raise ValueError("Manager mediation Assign proof 링크가 다릅니다.")
        return self


@final
class ValidatedMediationDismiss(_FrozenDto):
    kind: Literal["validated_mediation_dismiss"] = "validated_mediation_dismiss"
    conflict_claim: SealedDeadlockClaim
    conflict_handle: ConflictSealedClaimHandle
    manager_claim: SealedDeadlockDismissClaim
    manager_handle: DeadlockManagerSealedClaimHandle
    reason_code: Literal["manager_declined"] = "manager_declined"

    @model_validator(mode="after")
    def _exact_links(self) -> ValidatedMediationDismiss:
        conflict = self.conflict_claim
        manager = self.manager_claim
        if (
            self.conflict_handle.generation != conflict.generation
            or self.manager_handle.generation != manager.generation
            or manager.request_id != conflict.request_id
            or manager.case_id != conflict.case_id
            or manager.org_id != conflict.org_id
            or manager.intent != conflict.intent
            or manager.round != conflict.round
            or manager.cause != conflict.cause
            or self.reason_code != manager.reason_code
        ):
            raise ValueError("Manager mediation Dismiss proof 링크가 다릅니다.")
        return self


ValidatedManagerMediation: TypeAlias = Annotated[
    ValidatedMediationAssign | ValidatedMediationDismiss,
    Field(discriminator="kind"),
]


@final
class ConflictMediationHandle(_FrozenDto):
    conflict_generation: str
    manager_generation: str
    forward_token: str


@final
class SealedConflictMediationAvailable(_FrozenDto):
    proof: ValidatedMediationAssign | ValidatedMediationDismiss
    handle: ConflictMediationHandle

    @model_validator(mode="after")
    def _generation_links(self) -> SealedConflictMediationAvailable:
        if (
            self.handle.conflict_generation != self.proof.conflict_claim.generation
            or self.handle.manager_generation != self.proof.manager_claim.generation
        ):
            raise ValueError("Mediation proof와 handle generation이 다릅니다.")
        return self


class _ProgressDto(_FrozenDto):
    position: int = Field(ge=1)
    case_id: str
    request_id: str


@final
class ConcurrenceVoteStored(_ProgressDto):
    kind: Literal["vote_stored"] = "vote_stored"
    round: int = Field(ge=1)
    evidence: OwnerConcurrenceEvidence


@final
class ConflictClaimReserved(_ProgressDto):
    kind: Literal["claim_reserved"] = "claim_reserved"
    round: int = Field(ge=1)
    claim: ReservedConsensusClaim | ReservedDeadlockClaim


@final
class ConflictClaimSealed(_ProgressDto):
    kind: Literal["claim_sealed"] = "claim_sealed"
    round: int = Field(ge=1)
    claim: SealedConsensusClaim | SealedDeadlockClaim


@final
class ConsensusRoundAbandoned(_ProgressDto):
    kind: Literal["round_abandoned"] = "round_abandoned"
    from_round: int = Field(ge=1)
    to_round: int = Field(ge=2)
    generation: str
    reason_code: Literal["authority_rejected_write_zero"] = "authority_rejected_write_zero"


@final
class ConflictResolutionEvidenceRecorded(_ProgressDto):
    kind: Literal["resolution_evidence_recorded"] = "resolution_evidence_recorded"
    round: int = Field(ge=1)
    evidence: ConflictResolutionEvidence


@final
class ConflictMediationSealed(_ProgressDto):
    kind: Literal["mediation_sealed"] = "mediation_sealed"
    round: int = Field(ge=1)
    item_id: str
    disposition: Literal["assign", "dismiss"]
    conflict_generation: str
    manager_generation: str


ConflictProgressEntry: TypeAlias = Annotated[
    ConcurrenceVoteStored
    | ConflictClaimReserved
    | ConflictClaimSealed
    | ConsensusRoundAbandoned
    | ConflictResolutionEvidenceRecorded
    | ConflictMediationSealed,
    Field(discriminator="kind"),
]


class ConflictDispositionError(RuntimeError):
    code: str
    retryable: bool


class ConflictDispositionNotFound(ConflictDispositionError):
    code = "conflict_disposition_not_found"
    retryable = False


class ConflictDispositionForbidden(ConflictDispositionError):
    code = "conflict_disposition_forbidden"
    retryable = False


class ConflictDispositionInvalid(ConflictDispositionError):
    code = "conflict_disposition_invalid"
    retryable = False


class ConflictDispositionInProgress(ConflictDispositionError):
    code = "conflict_disposition_in_progress"
    retryable = True


class ConflictDispositionConflict(ConflictDispositionError):
    code = "conflict_disposition_conflict"
    retryable = False


class ConflictDispositionDependency(ConflictDispositionError):
    code = "conflict_disposition_dependency"
    retryable = True


class ConflictAuthorizationUnavailable(ConflictDispositionDependency):
    """중앙 Conflict 권한 의존성 장애의 field-free 신호."""


class ConflictDispositionNotFoundOrDenied(ConflictDispositionError):
    """Case 부재·조직·후보·권한 거부를 같은 의미로 숨긴다."""

    code = "conflict_disposition_not_found_or_denied"
    retryable = False


class ConflictDispositionIntegrity(ConflictDispositionError):
    code = "conflict_disposition_integrity"
    retryable = False


@final
class ConcurrencePending(_FrozenDto):
    kind: Literal["concurrence_pending"] = "concurrence_pending"
    request_id: str
    case_id: str
    current_round: int = Field(ge=1)
    pending_owners: tuple[str, ...]


@final
class ConsensusRouteRejected(_FrozenDto):
    kind: Literal["consensus_route_rejected"] = "consensus_route_rejected"
    request_id: str
    case_id: str
    current_round: int = Field(ge=1)
    next_round: int = Field(ge=2)
    reason_code: str


@final
class ConflictResolved(_FrozenDto):
    kind: Literal["conflict_resolved"] = "conflict_resolved"
    request_id: str
    case_id: str
    route: RouteTarget
    wake: ExecutionWake


@final
class ConflictEscalated(_FrozenDto):
    kind: Literal["conflict_escalated"] = "conflict_escalated"
    request_id: str
    case_id: str
    cause: ConflictEscalationCause
    manager_item_id: str


P17DirectConcurrenceResult: TypeAlias = Annotated[
    ConcurrencePending | ConsensusRouteRejected | ConflictResolved,
    Field(discriminator="kind"),
]

P17ConcurrenceResult: TypeAlias = Annotated[
    ConcurrencePending | ConsensusRouteRejected | ConflictResolved | ConflictEscalated,
    Field(discriminator="kind"),
]


class RequestAwareConflictDispositionStore(RequestAwareConflictCaseStore, Protocol):
    def open_for_owner(self, owner_id: str) -> list[ConflictCase]: ...

    def reserve_validated_concurrence(
        self,
        case_id: str,
        command: ConcurOnConflict,
        *,
        validate: Callable[[ConflictCase, ConcurOnConflict], ValidatedConcurrence],
    ) -> ConflictConcurrenceAttempt: ...

    def claim_for_case(
        self, case_id: str
    ) -> ReservedConsensusClaim | SealedConsensusClaim | SealedDeadlockClaim | None: ...

    def validate_consensus_reservation(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
    ) -> None: ...

    def sealed_claim_for_case(self, case_id: str) -> SealedConflictClaimAvailable | None: ...

    def validate_sealed_claim(
        self,
        claim: SealedConsensusClaim | SealedDeadlockClaim,
        *,
        handle: ConflictSealedClaimHandle,
    ) -> None: ...

    def progress_history_for_case(self, case_id: str) -> tuple[ConflictProgressEntry, ...]: ...

    def seal_consensus_claim(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
    ) -> SealedConflictClaimAvailable: ...

    def abandon_unmutated_consensus_round(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> ConflictCase: ...

    def record_resolution_evidence(
        self,
        handle: ConflictSealedClaimHandle,
        evidence: ConflictResolutionEvidence,
    ) -> None: ...

    def resolution_evidence_for_request(
        self, request_id: str
    ) -> ConflictResolutionEvidence | None: ...

    def transition_for_claim(
        self,
        handle: ConflictSealedClaimHandle,
        *,
        target: ConflictCase,
    ) -> ConflictCase: ...


class RequestAwareConflictMediationStore(RequestAwareConflictDispositionStore, Protocol):
    def record_validated_mediation(
        self,
        conflict_handle: ConflictSealedClaimHandle,
        manager_handle: DeadlockManagerSealedClaimHandle,
        *,
        validate: Callable[
            [ConflictCase, SealedDeadlockClaim, DeadlockManagerSealedClaimHandle],
            ValidatedManagerMediation,
        ],
    ) -> SealedConflictMediationAvailable: ...

    def transition_for_mediation(
        self,
        handle: ConflictMediationHandle,
        *,
        target: ConflictCase,
    ) -> ConflictCase: ...


class RequestAwareConflictEscalationManagerStore(
    RequestAwareManagerQueueStore,
    ManagerQueueStore,
    Protocol,
):
    """S3 escalation writer가 request/case/item read-back에 쓰는 합성 포트."""


def _default_id() -> str:
    return uuid.uuid4().hex


def canonical_concur_command(command: ConcurOnConflict) -> ConcurOnConflict:
    if type(command) is not ConcurOnConflict:
        raise ConflictDispositionIntegrity("ConcurOnConflict exact type이 필요합니다.")
    try:
        return ConcurOnConflict.model_validate(
            command.model_dump(mode="python", round_trip=True), strict=True
        )
    except Exception as error:
        raise ConflictDispositionIntegrity("ConcurOnConflict가 손상됐습니다.") from error


class InMemoryConflictDispositionStore(InMemoryConflictCaseStore):
    """단일 프로세스 concurrence vote·claim·evidence Store.

    validation callback은 Case lock 안에서 실행된다. P17 direct application은 장기 outer
    Registry lock을 잡지 않고 callback 안의 짧은 snapshot만 써 ``Store → Registry``
    순서로 통일한다. process 전체의 lock-order/UoW 일반화는 P17.9 범위다.
    """

    def __init__(self, *, id_factory: IdFactory = _default_id) -> None:
        super().__init__()
        self._id_factory = id_factory
        self._active_votes: dict[str, dict[str, OwnerConcurrenceEvidence]] = {}
        self._vote_approval: dict[str, dict[str, bool]] = {}
        self._claims: dict[
            str,
            ReservedConsensusClaim
            | SealedConsensusClaim
            | ReservedDeadlockClaim
            | SealedDeadlockClaim,
        ] = {}
        self._control_tokens: dict[str, ConflictReservationControlToken] = {}
        self._sealed_handles: dict[str, ConflictSealedClaimHandle] = {}
        self._used_generations: set[str] = set()
        self._progress: dict[str, list[ConflictProgressEntry]] = {}
        self._resolution_evidence: dict[str, ConflictResolutionEvidence] = {}
        self._evidence_handles: dict[str, ConflictSealedClaimHandle | ConflictMediationHandle] = {}
        self._mediation_proofs: dict[str, ValidatedManagerMediation] = {}
        self._mediation_conflict_handles: dict[str, ConflictSealedClaimHandle] = {}
        self._mediation_manager_handles: dict[str, DeadlockManagerSealedClaimHandle] = {}
        self._mediation_handles: dict[str, ConflictMediationHandle] = {}
        self._mediation_validating: set[str] = set()
        self._mediation_reentered: set[str] = set()
        self._validating: set[str] = set()
        self._reentered: set[str] = set()

    def _new_secret(self) -> str:
        value = str(self._id_factory())
        if not value.strip():
            raise ConflictDispositionIntegrity("ID factory가 nonblank 값을 돌려야 합니다.")
        return value

    def _new_generation(self) -> str:
        generation = self._new_secret()
        if generation in self._used_generations:
            raise ConflictDispositionIntegrity("Conflict claim generation이 재사용됐습니다.")
        return generation

    def _append_progress(
        self,
        progress_case_id: str,
        entry_type: type[ConflictProgressEntry],
        **values: object,
    ) -> None:
        entries = self._progress.setdefault(progress_case_id, [])
        entry = entry_type(position=len(entries) + 1, **values)  # type: ignore[call-arg]
        entries.append(deepcopy(entry))

    def reserve_validated_concurrence(
        self,
        case_id: str,
        command: ConcurOnConflict,
        *,
        validate: Callable[[ConflictCase, ConcurOnConflict], ValidatedConcurrence],
    ) -> ConflictConcurrenceAttempt:
        canonical_command = canonical_concur_command(command)
        with self._lock:
            current = self._current_request_case_unlocked(case_id)
            if current is None:
                raise ConflictDispositionNotFound("request-aware ConflictCase가 없습니다.")
            if current.status != "open":
                raise ConflictDispositionConflict("open ConflictCase가 아닙니다.")
            if canonical_command.case_id != case_id:
                raise ConflictDispositionIntegrity("command Case ID가 요청 경로와 다릅니다.")
            if canonical_command.expected_round != current.concurrence_round:
                raise ConflictDispositionIntegrity("concurrence round가 저장 상태와 다릅니다.")
            if case_id in self._validating:
                self._reentered.add(case_id)
                raise ConflictDispositionIntegrity("validation callback 재진입은 금지됩니다.")
            self._validating.add(case_id)
            try:
                raw = validate(deepcopy(current), canonical_command.model_copy(deep=True))
            except BaseException:
                self._reentered.discard(case_id)
                raise
            finally:
                self._validating.remove(case_id)
            if case_id in self._reentered:
                self._reentered.remove(case_id)
                raise ConflictDispositionIntegrity("validation callback이 재진입했습니다.")
            validated = self._canonical_validated(raw)
            self._validate_callback_result(current, canonical_command, validated)
            return deepcopy(self._reserve_validated_unlocked(current, canonical_command, validated))

    @staticmethod
    def _canonical_validated(raw: ValidatedConcurrence) -> ValidatedConcurrence:
        try:
            if type(raw) is ValidatedOwnerVote:
                payload = raw.model_dump(mode="python", round_trip=True)
                payload["candidate_snapshot"] = deepcopy(raw.candidate_snapshot)
                payload["trigger"] = raw.trigger.model_copy(deep=True)
                payload["evidence"] = raw.evidence.model_copy(deep=True)
                return ValidatedOwnerVote.model_validate(payload, strict=True)
            if type(raw) is ValidatedRegistryEscalation:
                payload = raw.model_dump(mode="python", round_trip=True)
                payload["candidate_snapshot"] = deepcopy(raw.candidate_snapshot)
                payload["trigger"] = raw.trigger.model_copy(deep=True)
                payload["cause"] = raw.cause.model_copy(deep=True)
                return ValidatedRegistryEscalation.model_validate(payload, strict=True)
        except Exception as error:
            raise ConflictDispositionIntegrity("validation 결과가 손상됐습니다.") from error
        raise ConflictDispositionIntegrity("validation 결과 exact type이 아닙니다.")

    @staticmethod
    def _validate_callback_result(
        case: ConflictCase,
        command: ConcurOnConflict,
        validated: ValidatedConcurrence,
    ) -> None:
        request_id = case.request_id
        if request_id is None:
            raise ConflictDispositionIntegrity("request-aware Case에 Request ID가 없습니다.")
        expected_trigger = ConcurrenceActionFingerprint(
            case_id=case.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        )
        if (
            validated.request_id != request_id
            or validated.case_id != case.case_id
            or validated.org_id != command.principal.org_id
            or validated.intent != case.intent
            or validated.candidate_snapshot != case.candidates
            or validated.trigger != expected_trigger
        ):
            raise ConflictDispositionIntegrity("validation 결과가 Case·command와 다릅니다.")
        if isinstance(validated, ValidatedOwnerVote):
            evidence = validated.evidence
            if (
                evidence.round != command.expected_round
                or evidence.owner_id != command.principal.subject_id
                or evidence.on_agent != command.on_agent
                or evidence.stance != command.stance
                or evidence.rationale != command.rationale
                or evidence.owner_id
                not in tuple(dict.fromkeys(candidate.owner for candidate in case.candidates))
                or evidence.on_agent not in case.candidate_ids()
            ):
                raise ConflictDispositionIntegrity("validated vote가 command와 다릅니다.")
        elif validated.cause.round != command.expected_round:
            raise ConflictDispositionIntegrity("Registry drift cause round가 다릅니다.")

    def _reserve_validated_unlocked(
        self,
        case: ConflictCase,
        command: ConcurOnConflict,
        validated: ValidatedConcurrence,
    ) -> ConflictConcurrenceAttempt:
        existing_claim = self._claims.get(case.case_id)
        votes_by_owner = self._active_votes.setdefault(case.case_id, {})
        approvals_by_owner = self._vote_approval.setdefault(case.case_id, {})
        if isinstance(validated, ValidatedOwnerVote):
            evidence = validated.evidence
            previous = votes_by_owner.get(evidence.owner_id)
            if previous is not None and previous != evidence:
                return ConflictClaimConflict()
            if existing_claim is not None:
                if isinstance(existing_claim, (ReservedConsensusClaim, SealedConsensusClaim)):
                    if evidence not in existing_claim.votes:
                        return ConflictClaimConflict()
                    if isinstance(existing_claim, ReservedConsensusClaim):
                        return ConflictClaimInProgress()
                    return self._sealed_available_unlocked(case.case_id, existing_claim)
                if isinstance(existing_claim, ReservedDeadlockClaim):
                    raise ConflictDispositionIntegrity("Reserved deadlock claim이 노출됐습니다.")
                if isinstance(existing_claim.cause, DivergentVotes):
                    if evidence in existing_claim.votes:
                        return self._sealed_available_unlocked(case.case_id, existing_claim)
                return ConflictClaimConflict()
            if previous is not None:
                return self._pending(case, votes_by_owner)

            candidate_owners = tuple(dict.fromkeys(c.owner for c in case.candidates))
            proposed_votes = dict(votes_by_owner)
            proposed_votes[evidence.owner_id] = evidence
            canonical_votes = tuple(
                proposed_votes[owner] for owner in candidate_owners if owner in proposed_votes
            )
            pending = tuple(owner for owner in candidate_owners if owner not in proposed_votes)
            if pending:
                votes_by_owner[evidence.owner_id] = deepcopy(evidence)
                approvals_by_owner[evidence.owner_id] = validated.target_requires_approval
                self._append_progress(
                    case.case_id,
                    ConcurrenceVoteStored,
                    case_id=case.case_id,
                    request_id=validated.request_id,
                    round=case.concurrence_round,
                    evidence=evidence,
                )
                return ConcurrencePendingStored(
                    current_round=case.concurrence_round,
                    pending_owners=pending,
                )

            generation = self._new_generation()
            targets = {vote.on_agent for vote in canonical_votes}
            if len(targets) == 1:
                control_secret = self._new_secret()
                primary = next(iter(targets))
                reserved: ReservedConsensusClaim | ReservedDeadlockClaim = ReservedConsensusClaim(
                    generation=generation,
                    idempotency_key=(
                        f"conflict-disposition:{case.case_id}:{case.concurrence_round}"
                    ),
                    request_id=validated.request_id,
                    case_id=case.case_id,
                    org_id=validated.org_id,
                    intent=case.intent,
                    round=case.concurrence_round,
                    candidate_snapshot=case.candidates,
                    votes=canonical_votes,
                    trigger=validated.trigger,
                    primary=primary,
                    requires_approval=validated.target_requires_approval,
                )
                token = ConflictReservationControlToken(
                    generation=generation,
                    token=control_secret,
                )
                result: ConflictConcurrenceAttempt = ConflictClaimAcquired(
                    claim=reserved,
                    control_token=token,
                )
            else:
                forward_secret = self._new_secret()
                reserved = ReservedDeadlockClaim(
                    generation=generation,
                    idempotency_key=(
                        f"conflict-disposition:{case.case_id}:{case.concurrence_round}"
                    ),
                    request_id=validated.request_id,
                    case_id=case.case_id,
                    org_id=validated.org_id,
                    intent=case.intent,
                    round=case.concurrence_round,
                    candidate_snapshot=case.candidates,
                    votes=canonical_votes,
                    trigger=validated.trigger,
                    cause=DivergentVotes(round=case.concurrence_round),
                )
                sealed_deadlock = _seal_deadlock(reserved)
                handle = ConflictSealedClaimHandle(
                    generation=generation,
                    forward_token=forward_secret,
                )
                result = SealedConflictClaimAvailable(
                    claim=sealed_deadlock,
                    handle=handle,
                )

            votes_by_owner[evidence.owner_id] = deepcopy(evidence)
            approvals_by_owner[evidence.owner_id] = validated.target_requires_approval
            self._used_generations.add(generation)
            self._append_progress(
                case.case_id,
                ConcurrenceVoteStored,
                case_id=case.case_id,
                request_id=validated.request_id,
                round=case.concurrence_round,
                evidence=evidence,
            )
            self._claims[case.case_id] = deepcopy(reserved)
            self._append_progress(
                case.case_id,
                ConflictClaimReserved,
                case_id=case.case_id,
                request_id=validated.request_id,
                round=case.concurrence_round,
                claim=reserved,
            )
            if isinstance(result, ConflictClaimAcquired):
                self._control_tokens[case.case_id] = deepcopy(result.control_token)
            else:
                self._claims[case.case_id] = deepcopy(result.claim)
                self._sealed_handles[case.case_id] = deepcopy(result.handle)
                self._append_progress(
                    case.case_id,
                    ConflictClaimSealed,
                    case_id=case.case_id,
                    request_id=validated.request_id,
                    round=case.concurrence_round,
                    claim=result.claim,
                )
            return result

        if existing_claim is not None:
            if (
                isinstance(existing_claim, SealedDeadlockClaim)
                and isinstance(existing_claim.cause, CandidateRegistryChanged)
                and existing_claim.trigger == validated.trigger
                and existing_claim.cause == validated.cause
            ):
                return self._sealed_available_unlocked(case.case_id, existing_claim)
            return ConflictClaimConflict()

        generation = self._new_generation()
        forward_secret = self._new_secret()
        canonical_votes = self._canonical_votes(case, votes_by_owner)
        reserved_drift = ReservedDeadlockClaim(
            generation=generation,
            idempotency_key=f"conflict-disposition:{case.case_id}:{case.concurrence_round}",
            request_id=validated.request_id,
            case_id=case.case_id,
            org_id=validated.org_id,
            intent=case.intent,
            round=case.concurrence_round,
            candidate_snapshot=case.candidates,
            votes=canonical_votes,
            trigger=validated.trigger,
            cause=validated.cause,
        )
        sealed_drift = _seal_deadlock(reserved_drift)
        handle = ConflictSealedClaimHandle(
            generation=generation,
            forward_token=forward_secret,
        )
        self._used_generations.add(generation)
        self._claims[case.case_id] = deepcopy(sealed_drift)
        self._sealed_handles[case.case_id] = deepcopy(handle)
        self._append_progress(
            case.case_id,
            ConflictClaimReserved,
            case_id=case.case_id,
            request_id=validated.request_id,
            round=case.concurrence_round,
            claim=reserved_drift,
        )
        self._append_progress(
            case.case_id,
            ConflictClaimSealed,
            case_id=case.case_id,
            request_id=validated.request_id,
            round=case.concurrence_round,
            claim=sealed_drift,
        )
        return SealedConflictClaimAvailable(claim=sealed_drift, handle=handle)

    @staticmethod
    def _canonical_votes(
        case: ConflictCase,
        votes_by_owner: dict[str, OwnerConcurrenceEvidence],
    ) -> tuple[OwnerConcurrenceEvidence, ...]:
        owner_order = tuple(dict.fromkeys(candidate.owner for candidate in case.candidates))
        return tuple(votes_by_owner[owner] for owner in owner_order if owner in votes_by_owner)

    def _pending(
        self,
        case: ConflictCase,
        votes_by_owner: dict[str, OwnerConcurrenceEvidence],
    ) -> ConcurrencePendingStored:
        owner_order = tuple(dict.fromkeys(candidate.owner for candidate in case.candidates))
        return ConcurrencePendingStored(
            current_round=case.concurrence_round,
            pending_owners=tuple(owner for owner in owner_order if owner not in votes_by_owner),
        )

    def _sealed_available_unlocked(
        self,
        case_id: str,
        claim: SealedConsensusClaim | SealedDeadlockClaim,
    ) -> SealedConflictClaimAvailable:
        handle = self._sealed_handles.get(case_id)
        if handle is None or handle.generation != claim.generation:
            raise ConflictDispositionIntegrity("sealed Conflict handle이 손상됐습니다.")
        return SealedConflictClaimAvailable(claim=deepcopy(claim), handle=deepcopy(handle))

    def claim_for_case(
        self, case_id: str
    ) -> ReservedConsensusClaim | SealedConsensusClaim | SealedDeadlockClaim | None:
        with self._lock:
            claim = self._claims.get(case_id)
            if isinstance(claim, ReservedDeadlockClaim):
                raise ConflictDispositionIntegrity(
                    "Reserved deadlock claim이 lock 밖에 남았습니다."
                )
            return deepcopy(claim)

    def validate_consensus_reservation(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
    ) -> None:
        try:
            if type(claim) is not ReservedConsensusClaim:
                raise TypeError("ReservedConsensusClaim exact type이 필요합니다.")
            canonical_claim = ReservedConsensusClaim.model_validate(
                claim,
                strict=True,
            ).model_copy(deep=True)
            if type(control_token) is not ConflictReservationControlToken:
                raise TypeError("ConflictReservationControlToken exact type이 필요합니다.")
            canonical_control = ConflictReservationControlToken.model_validate(
                control_token.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ConflictDispositionIntegrity(
                "reserved consensus proof가 손상됐습니다."
            ) from error
        with self._lock:
            stored_claim = self._claims.get(canonical_claim.case_id)
            stored_control = self._control_tokens.get(canonical_claim.case_id)
            if (
                type(stored_claim) is not ReservedConsensusClaim
                or stored_claim != canonical_claim
                or type(stored_control) is not ConflictReservationControlToken
                or stored_control != canonical_control
            ):
                raise ConflictDispositionIntegrity(
                    "reserved consensus claim 또는 full token이 다릅니다."
                )

    def sealed_claim_for_case(self, case_id: str) -> SealedConflictClaimAvailable | None:
        with self._lock:
            claim = self._claims.get(case_id)
            if claim is None or isinstance(claim, ReservedConsensusClaim):
                return None
            if isinstance(claim, ReservedDeadlockClaim):
                raise ConflictDispositionIntegrity(
                    "Reserved deadlock claim이 lock 밖에 남았습니다."
                )
            return deepcopy(self._sealed_available_unlocked(case_id, claim))

    def validate_sealed_claim(
        self,
        claim: SealedConsensusClaim | SealedDeadlockClaim,
        *,
        handle: ConflictSealedClaimHandle,
    ) -> None:
        try:
            if type(claim) is SealedConsensusClaim:
                canonical_claim: SealedConsensusClaim | SealedDeadlockClaim = (
                    SealedConsensusClaim.model_validate(
                        claim,
                        strict=True,
                    ).model_copy(deep=True)
                )
            elif type(claim) is SealedDeadlockClaim:
                canonical_claim = SealedDeadlockClaim.model_validate(
                    claim,
                    strict=True,
                ).model_copy(deep=True)
            else:
                raise TypeError("sealed Conflict claim exact type이 필요합니다.")
            if type(handle) is not ConflictSealedClaimHandle:
                raise TypeError("ConflictSealedClaimHandle exact type이 필요합니다.")
            canonical_handle = ConflictSealedClaimHandle.model_validate(
                handle.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ConflictDispositionIntegrity("sealed Conflict proof가 손상됐습니다.") from error
        with self._lock:
            if (
                self._claims.get(canonical_claim.case_id) != canonical_claim
                or self._sealed_handles.get(canonical_claim.case_id) != canonical_handle
            ):
                raise ConflictDispositionIntegrity(
                    "sealed Conflict claim 또는 full handle이 다릅니다."
                )

    def progress_history_for_case(self, case_id: str) -> tuple[ConflictProgressEntry, ...]:
        with self._lock:
            return tuple(deepcopy(self._progress.get(case_id, [])))

    def seal_consensus_claim(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
    ) -> SealedConflictClaimAvailable:
        canonical_claim = deepcopy(claim)
        canonical_token = control_token.model_copy(deep=True)
        with self._lock:
            current = self._claims.get(canonical_claim.case_id)
            stored_token = self._control_tokens.get(canonical_claim.case_id)
            if current != canonical_claim or stored_token != canonical_token:
                raise ConflictDispositionIntegrity("reserved claim 또는 full token이 다릅니다.")
            forward_secret = self._new_secret()
            sealed = SealedConsensusClaim(
                generation=canonical_claim.generation,
                idempotency_key=canonical_claim.idempotency_key,
                request_id=canonical_claim.request_id,
                case_id=canonical_claim.case_id,
                org_id=canonical_claim.org_id,
                intent=canonical_claim.intent,
                round=canonical_claim.round,
                candidate_snapshot=canonical_claim.candidate_snapshot,
                votes=canonical_claim.votes,
                trigger=canonical_claim.trigger,
                primary=canonical_claim.primary,
                requires_approval=canonical_claim.requires_approval,
            )
            handle = ConflictSealedClaimHandle(
                generation=sealed.generation,
                forward_token=forward_secret,
            )
            self._claims[sealed.case_id] = deepcopy(sealed)
            self._control_tokens.pop(sealed.case_id, None)
            self._sealed_handles[sealed.case_id] = deepcopy(handle)
            self._append_progress(
                sealed.case_id,
                ConflictClaimSealed,
                case_id=sealed.case_id,
                request_id=sealed.request_id,
                round=sealed.round,
                claim=sealed,
            )
            return SealedConflictClaimAvailable(claim=sealed, handle=handle)

    def abandon_unmutated_consensus_round(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> ConflictCase:
        canonical_rejection = RequestRouteGrantRejected.model_validate(
            rejection.model_dump(mode="python", round_trip=True), strict=True
        )
        with self._lock:
            current_claim = self._claims.get(claim.case_id)
            current_token = self._control_tokens.get(claim.case_id)
            current_case = self._current_request_case_unlocked(claim.case_id)
            if (
                current_claim != claim
                or current_token != control_token
                or current_case is None
                or current_case.status != "open"
                or current_case.concurrence_round != claim.round
                or canonical_rejection.idempotency_key != claim.idempotency_key
                or claim.request_id in self._resolution_evidence
                or claim.case_id in self._sealed_handles
            ):
                raise ConflictDispositionIntegrity("consensus round를 abandon할 수 없습니다.")
            target = current_case.advance_concurrence_round()
            stored = self._replace_request_case_unlocked(target)
            self._claims.pop(claim.case_id, None)
            self._control_tokens.pop(claim.case_id, None)
            self._active_votes.pop(claim.case_id, None)
            self._vote_approval.pop(claim.case_id, None)
            self._append_progress(
                claim.case_id,
                ConsensusRoundAbandoned,
                case_id=claim.case_id,
                request_id=claim.request_id,
                from_round=claim.round,
                to_round=target.concurrence_round,
                generation=claim.generation,
            )
            return stored

    def record_resolution_evidence(
        self,
        handle: ConflictSealedClaimHandle,
        evidence: ConflictResolutionEvidence,
    ) -> None:
        canonical_handle = handle.model_copy(deep=True)
        canonical_evidence = ConflictResolutionEvidence.model_validate(
            evidence.model_dump(mode="python", round_trip=True), strict=True
        )
        with self._lock:
            case_id, claim = self._claim_for_handle_unlocked(canonical_handle)
            if not isinstance(claim, SealedConsensusClaim):
                raise ConflictDispositionIntegrity("direct consensus handle이 아닙니다.")
            self._validate_resolution_evidence(claim, canonical_evidence)
            existing = self._resolution_evidence.get(claim.request_id)
            if existing is not None:
                if (
                    existing == canonical_evidence
                    and self._evidence_handles.get(claim.request_id) == canonical_handle
                ):
                    return
                raise ConflictDispositionIntegrity("Request resolution evidence가 충돌합니다.")
            self._resolution_evidence[claim.request_id] = deepcopy(canonical_evidence)
            self._evidence_handles[claim.request_id] = deepcopy(canonical_handle)
            self._append_progress(
                case_id,
                ConflictResolutionEvidenceRecorded,
                case_id=case_id,
                request_id=claim.request_id,
                round=claim.round,
                evidence=canonical_evidence,
            )

    @staticmethod
    def _validate_resolution_evidence(
        claim: SealedConsensusClaim,
        evidence: ConflictResolutionEvidence,
    ) -> None:
        if (
            evidence.request_id != claim.request_id
            or evidence.case_id != claim.case_id
            or evidence.org_id != claim.org_id
            or evidence.intent != claim.intent
            or evidence.route.intent != claim.intent
            or evidence.route.agent_id != claim.primary
            or evidence.route.requires_approval != claim.requires_approval
            or evidence.route.authority_version is None
            or not isinstance(evidence.source, FromDirectConsensus)
            or evidence.source.round != claim.round
            or evidence.source.votes != claim.votes
        ):
            raise ConflictDispositionIntegrity("resolution evidence가 sealed claim과 다릅니다.")
        candidates = {candidate.agent_id: candidate.owner for candidate in claim.candidate_snapshot}
        votes = {vote.owner_id: vote for vote in claim.votes}
        expected_supporting = tuple(
            SupportingKnowledgeEvidence(
                agent_id=candidate.agent_id,
                affirmed_by_owner=candidate.owner,
            )
            for candidate in claim.candidate_snapshot
            if candidate.agent_id != claim.primary
            and votes[candidate.owner].stance == "keep_as_complement"
        )
        if evidence.supporting != expected_supporting or any(
            support.agent_id not in candidates for support in evidence.supporting
        ):
            raise ConflictDispositionIntegrity("supporting evidence가 vote와 다릅니다.")

    def resolution_evidence_for_request(self, request_id: str) -> ConflictResolutionEvidence | None:
        with self._lock:
            return deepcopy(self._resolution_evidence.get(request_id))

    @staticmethod
    def _canonical_conflict_handle(
        handle: ConflictSealedClaimHandle,
    ) -> ConflictSealedClaimHandle:
        try:
            if type(handle) is not ConflictSealedClaimHandle:
                raise ConflictDispositionIntegrity("Conflict handle exact type이 필요합니다.")
            return ConflictSealedClaimHandle.model_validate(
                handle.model_dump(mode="python", round_trip=True), strict=True
            )
        except ConflictDispositionIntegrity:
            raise
        except Exception as error:
            raise ConflictDispositionIntegrity("Conflict handle이 손상됐습니다.") from error

    @staticmethod
    def _canonical_manager_handle(
        handle: DeadlockManagerSealedClaimHandle,
    ) -> DeadlockManagerSealedClaimHandle:
        try:
            if type(handle) is not DeadlockManagerSealedClaimHandle:
                raise ConflictDispositionIntegrity("Manager handle exact type이 필요합니다.")
            return DeadlockManagerSealedClaimHandle.model_validate(
                handle.model_dump(mode="python", round_trip=True), strict=True
            )
        except ConflictDispositionIntegrity:
            raise
        except Exception as error:
            raise ConflictDispositionIntegrity("Manager handle이 손상됐습니다.") from error

    @staticmethod
    def _canonical_mediation(
        proof: ValidatedManagerMediation,
    ) -> ValidatedMediationAssign | ValidatedMediationDismiss:
        try:
            if type(proof) is ValidatedMediationAssign:
                return ValidatedMediationAssign(
                    conflict_claim=deepcopy(proof.conflict_claim),
                    conflict_handle=proof.conflict_handle.model_copy(deep=True),
                    manager_claim=proof.manager_claim.model_copy(deep=True),
                    manager_handle=proof.manager_handle.model_copy(deep=True),
                    evidence=proof.evidence.model_copy(deep=True),
                )
            if type(proof) is ValidatedMediationDismiss:
                return ValidatedMediationDismiss(
                    conflict_claim=deepcopy(proof.conflict_claim),
                    conflict_handle=proof.conflict_handle.model_copy(deep=True),
                    manager_claim=proof.manager_claim.model_copy(deep=True),
                    manager_handle=proof.manager_handle.model_copy(deep=True),
                    reason_code=proof.reason_code,
                )
        except Exception as error:
            raise ConflictDispositionIntegrity(
                "Mediation validation 결과가 손상됐습니다."
            ) from error
        raise ConflictDispositionIntegrity("Mediation validation 결과 exact type이 아닙니다.")

    def record_validated_mediation(
        self,
        conflict_handle: ConflictSealedClaimHandle,
        manager_handle: DeadlockManagerSealedClaimHandle,
        *,
        validate: Callable[
            [ConflictCase, SealedDeadlockClaim, DeadlockManagerSealedClaimHandle],
            ValidatedManagerMediation,
        ],
    ) -> SealedConflictMediationAvailable:
        canonical_conflict_handle = self._canonical_conflict_handle(conflict_handle)
        canonical_manager_handle = self._canonical_manager_handle(manager_handle)
        with self._lock:
            case_id, claim = self._claim_for_handle_unlocked(canonical_conflict_handle)
            if not isinstance(claim, SealedDeadlockClaim):
                raise ConflictDispositionIntegrity("Deadlock Conflict handle이 아닙니다.")
            current = self._current_request_case_unlocked(case_id)
            if current is None:
                raise ConflictDispositionIntegrity("ConflictCase를 찾을 수 없습니다.")
            if case_id in self._mediation_validating:
                self._mediation_reentered.add(case_id)
                raise ConflictDispositionIntegrity("Mediation callback 재진입은 금지됩니다.")
            self._mediation_validating.add(case_id)
            try:
                raw_proof = validate(
                    deepcopy(current),
                    deepcopy(claim),
                    canonical_manager_handle.model_copy(deep=True),
                )
            except BaseException:
                self._mediation_reentered.discard(case_id)
                raise
            finally:
                self._mediation_validating.remove(case_id)
            if case_id in self._mediation_reentered:
                self._mediation_reentered.remove(case_id)
                raise ConflictDispositionIntegrity("Mediation callback이 재진입했습니다.")
            proof = self._canonical_mediation(raw_proof)
            if (
                proof.conflict_claim != claim
                or proof.conflict_handle != canonical_conflict_handle
                or proof.manager_handle != canonical_manager_handle
                or proof.manager_claim.item_id != current.manager_item_id
                or proof.manager_claim.request_id != current.request_id
                or proof.manager_claim.case_id != current.case_id
                or proof.manager_claim.intent != current.intent
                or proof.manager_claim.round != current.concurrence_round
                or proof.manager_claim.cause != claim.cause
            ):
                raise ConflictDispositionIntegrity("Mediation proof가 저장 원본과 다릅니다.")

            existing = self._mediation_proofs.get(case_id)
            if existing is not None:
                handle = self._mediation_handles.get(case_id)
                if (
                    existing != proof
                    or self._mediation_conflict_handles.get(case_id) != canonical_conflict_handle
                    or self._mediation_manager_handles.get(case_id) != canonical_manager_handle
                    or handle is None
                ):
                    raise ConflictDispositionIntegrity("다른 mediation proof가 이미 저장됐습니다.")
                if isinstance(proof, ValidatedMediationAssign):
                    if (
                        self._resolution_evidence.get(proof.conflict_claim.request_id)
                        != proof.evidence
                        or self._evidence_handles.get(proof.conflict_claim.request_id) != handle
                    ):
                        raise ConflictDispositionIntegrity("Mediation evidence가 손상됐습니다.")
                elif proof.conflict_claim.request_id in self._resolution_evidence:
                    raise ConflictDispositionIntegrity("Dismiss mediation에 evidence가 있습니다.")
                return deepcopy(SealedConflictMediationAvailable(proof=proof, handle=handle))

            if current.status != "escalated":
                raise ConflictDispositionIntegrity(
                    "새 mediation proof에는 escalated Case가 필요합니다."
                )
            request_id = claim.request_id
            if isinstance(proof, ValidatedMediationAssign):
                if request_id in self._resolution_evidence:
                    raise ConflictDispositionIntegrity("Request resolution evidence가 충돌합니다.")
            elif request_id in self._resolution_evidence:
                raise ConflictDispositionIntegrity(
                    "Dismiss mediation은 evidence를 허용하지 않습니다."
                )

            handle = ConflictMediationHandle(
                conflict_generation=claim.generation,
                manager_generation=proof.manager_claim.generation,
                forward_token=self._new_secret(),
            )
            available = SealedConflictMediationAvailable(proof=proof, handle=handle)

            if isinstance(proof, ValidatedMediationAssign):
                self._resolution_evidence[request_id] = deepcopy(proof.evidence)
                self._evidence_handles[request_id] = deepcopy(handle)
                self._append_progress(
                    case_id,
                    ConflictResolutionEvidenceRecorded,
                    case_id=case_id,
                    request_id=request_id,
                    round=claim.round,
                    evidence=proof.evidence,
                )
                disposition: Literal["assign", "dismiss"] = "assign"
            else:
                disposition = "dismiss"
            self._mediation_proofs[case_id] = deepcopy(proof)
            self._mediation_conflict_handles[case_id] = deepcopy(canonical_conflict_handle)
            self._mediation_manager_handles[case_id] = deepcopy(canonical_manager_handle)
            self._mediation_handles[case_id] = deepcopy(handle)
            self._append_progress(
                case_id,
                ConflictMediationSealed,
                case_id=case_id,
                request_id=request_id,
                round=claim.round,
                item_id=proof.manager_claim.item_id,
                disposition=disposition,
                conflict_generation=claim.generation,
                manager_generation=proof.manager_claim.generation,
            )
            return deepcopy(available)

    def transition_for_mediation(
        self,
        handle: ConflictMediationHandle,
        *,
        target: ConflictCase,
    ) -> ConflictCase:
        try:
            if type(handle) is not ConflictMediationHandle:
                raise ConflictDispositionIntegrity("Mediation handle exact type이 필요합니다.")
            canonical_handle = ConflictMediationHandle.model_validate(
                handle.model_dump(mode="python", round_trip=True), strict=True
            )
        except ConflictDispositionIntegrity:
            raise
        except Exception as error:
            raise ConflictDispositionIntegrity("Mediation handle이 손상됐습니다.") from error
        canonical_target = deepcopy(target)
        with self._lock:
            case_id = next(
                (
                    stored_case_id
                    for stored_case_id, stored_handle in self._mediation_handles.items()
                    if stored_handle == canonical_handle
                ),
                None,
            )
            if case_id is None:
                raise ConflictDispositionIntegrity("full mediation handle이 다릅니다.")
            proof = self._mediation_proofs.get(case_id)
            current = self._current_request_case_unlocked(case_id)
            if proof is None or current is None:
                raise ConflictDispositionIntegrity("Mediation proof 또는 Case가 없습니다.")
            if (
                canonical_handle.conflict_generation != proof.conflict_claim.generation
                or canonical_handle.manager_generation != proof.manager_claim.generation
                or canonical_target.case_id != case_id
                or canonical_target.request_id != proof.conflict_claim.request_id
                or canonical_target.intent != proof.conflict_claim.intent
                or canonical_target.concurrence_round != proof.conflict_claim.round
                or canonical_target.manager_item_id != proof.manager_claim.item_id
            ):
                raise ConflictDispositionIntegrity("Mediation target 링크가 다릅니다.")
            if isinstance(proof, ValidatedMediationAssign):
                expected = Resolution(
                    intent=proof.manager_claim.intent,
                    primary=proof.manager_claim.agent_id,
                    rationale=proof.manager_claim.rationale,
                )
                if (
                    canonical_target.status != "resolved"
                    or canonical_target.resolution != expected
                    or canonical_target.decline_reason is not None
                    or self._resolution_evidence.get(proof.conflict_claim.request_id)
                    != proof.evidence
                    or self._evidence_handles.get(proof.conflict_claim.request_id)
                    != canonical_handle
                ):
                    raise ConflictDispositionIntegrity("Assign mediation terminal이 다릅니다.")
            elif (
                canonical_target.status != "declined"
                or canonical_target.resolution is not None
                or canonical_target.decline_reason != "manager_declined"
                or proof.conflict_claim.request_id in self._resolution_evidence
            ):
                raise ConflictDispositionIntegrity("Dismiss mediation terminal이 다릅니다.")
            if current.status in ("resolved", "declined"):
                if current == canonical_target:
                    return deepcopy(current)
                raise ConflictDispositionIntegrity("다른 terminal Case가 이미 저장됐습니다.")
            if current.status != "escalated":
                raise ConflictDispositionIntegrity("Mediation 전 Case가 escalated가 아닙니다.")
            try:
                return self._replace_request_case_unlocked(canonical_target)
            except ValueError as error:
                raise ConflictDispositionIntegrity("Mediation target 원형이 다릅니다.") from error

    def transition_for_claim(
        self,
        handle: ConflictSealedClaimHandle,
        *,
        target: ConflictCase,
    ) -> ConflictCase:
        with self._lock:
            case_id, claim = self._claim_for_handle_unlocked(handle)
            current = self._current_request_case_unlocked(case_id)
            if current is None:
                raise ConflictDispositionIntegrity("ConflictCase를 찾을 수 없습니다.")
            if isinstance(claim, SealedConsensusClaim):
                evidence = self._resolution_evidence.get(claim.request_id)
                rationale = _canonical_resolution_rationale(
                    claim.candidate_snapshot,
                    claim.votes,
                )
                if (
                    target.status != "resolved"
                    or current.concurrence_round != claim.round
                    or target.concurrence_round != claim.round
                    or target.manager_item_id is not None
                    or target.resolution is None
                    or evidence is None
                    or target.resolution.intent != claim.intent
                    or target.resolution.primary != claim.primary
                    or target.resolution.rationale != rationale
                ):
                    raise ConflictDispositionIntegrity("direct claim target 전이가 다릅니다.")
                if current.status == "resolved":
                    if current == target:
                        return deepcopy(current)
                    raise ConflictDispositionIntegrity("다른 terminal Case가 이미 저장됐습니다.")
                if current.status != "open":
                    raise ConflictDispositionIntegrity("direct claim의 Case가 open이 아닙니다.")
            elif (
                target.status != "escalated"
                or current.concurrence_round != claim.round
                or target.concurrence_round != claim.round
                or target.manager_item_id is None
                or target.resolution is not None
            ):
                raise ConflictDispositionIntegrity("deadlock claim target 전이가 다릅니다.")
            elif current.status == "escalated":
                if current == target:
                    return deepcopy(current)
                raise ConflictDispositionIntegrity("다른 escalation이 이미 저장됐습니다.")
            elif current.status != "open":
                raise ConflictDispositionIntegrity("deadlock claim의 Case가 open이 아닙니다.")
            try:
                return self._replace_request_case_unlocked(target)
            except ValueError as error:
                raise ConflictDispositionIntegrity(
                    "ConflictCase target이 저장 원형과 다릅니다."
                ) from error

    def _claim_for_handle_unlocked(
        self,
        handle: ConflictSealedClaimHandle,
    ) -> tuple[str, SealedConsensusClaim | SealedDeadlockClaim]:
        for case_id, stored_handle in self._sealed_handles.items():
            if stored_handle == handle:
                claim = self._claims.get(case_id)
                if (
                    isinstance(claim, (SealedConsensusClaim, SealedDeadlockClaim))
                    and claim.generation == handle.generation
                ):
                    return case_id, claim
                break
        raise ConflictDispositionIntegrity("full sealed Conflict handle이 다릅니다.")


def _seal_deadlock(claim: ReservedDeadlockClaim) -> SealedDeadlockClaim:
    return SealedDeadlockClaim(
        generation=claim.generation,
        idempotency_key=claim.idempotency_key,
        request_id=claim.request_id,
        case_id=claim.case_id,
        org_id=claim.org_id,
        intent=claim.intent,
        round=claim.round,
        candidate_snapshot=claim.candidate_snapshot,
        votes=claim.votes,
        trigger=claim.trigger,
        cause=claim.cause,
    )


def _seal_consensus(claim: ReservedConsensusClaim) -> SealedConsensusClaim:
    return SealedConsensusClaim(
        generation=claim.generation,
        idempotency_key=claim.idempotency_key,
        request_id=claim.request_id,
        case_id=claim.case_id,
        org_id=claim.org_id,
        intent=claim.intent,
        round=claim.round,
        candidate_snapshot=claim.candidate_snapshot,
        votes=claim.votes,
        trigger=claim.trigger,
        primary=claim.primary,
        requires_approval=claim.requires_approval,
    )


def _canonical_resolution_rationale(
    candidates: tuple[Candidate, ...],
    votes: tuple[OwnerConcurrenceEvidence, ...],
) -> str:
    by_owner = {vote.owner_id: vote for vote in votes}
    owner_order = tuple(dict.fromkeys(candidate.owner for candidate in candidates))
    return "; ".join(f"{owner}→{by_owner[owner].on_agent}" for owner in owner_order)


class P17DirectConflictDispositionApplication:
    """Owner direct consensus를 같은 Question Request의 attempt 1로 재개한다."""

    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        conflicts: RequestAwareConflictDispositionStore,
        registry: Registry,
        route_authority: RequestRouteAuthority,
        completion_reader: QuestionCompletionReader,
        deadline_policy: HandlingDeadlinePolicy,
        execution_starter: QuestionExecutionStarter,
        clock: Callable[[], datetime],
        central_authorizer: CentralAuthorizer | None = None,
    ) -> None:
        self._requests = requests
        self._conflicts = conflicts
        self._registry = registry
        self._route_authority = route_authority
        self._completion_reader = completion_reader
        self._deadline_policy = deadline_policy
        self._execution_starter = execution_starter
        self._clock = clock
        self._central_authorizer = central_authorizer
        self._recovery_lock = RLock()
        # Store seal/abandon 응답 유실을 같은 application 수명 안에서만 보수한다.
        # 프로세스 재시작을 넘는 control-token 복구는 P17.9 durable UoW/lease 범위다.
        self._reserved_recoveries: dict[
            str,
            tuple[ReservedConsensusClaim, ConflictReservationControlToken],
        ] = {}

    def concur(self, command: ConcurOnConflict) -> P17DirectConcurrenceResult:
        canonical = canonical_concur_command(command)
        if type(canonical.principal) is AuthenticatedPrincipal and self._central_authorizer is None:
            raise ConflictDispositionNotFoundOrDenied
        if self._central_authorizer is not None:
            principal = self._canonical_operations_principal(canonical.principal)
            case = self._read_case(canonical.case_id)
            request = self._read_linked_request(case)
            if request.org_id != principal.org_id or not self._is_current_candidate_owner(
                case, principal.subject_id
            ):
                raise ConflictDispositionNotFoundOrDenied
            self._authorize_case(principal, "conflict.concur", case, request)
        return self._concur_canonical(canonical)

    def pending_for(
        self,
        principal: ConflictOperationsPrincipal,
    ) -> list[ConflictCase]:
        """현재 후보 Owner의 request-aware open Case만 반환한다."""
        canonical = self._canonical_operations_principal(principal)
        if self._central_authorizer is not None:
            self._authorize_governance(
                canonical,
                "conflict.list",
                ResourceRef(
                    org_id=canonical.org_id,
                    kind="conflict_case_collection",
                    owner_subject_id=canonical.subject_id,
                ),
            )
        try:
            raw_cases = self._conflicts.open_for_owner(canonical.subject_id)
        except Exception as error:
            raise ConflictDispositionDependency from error
        if type(raw_cases) is not list:
            raise ConflictDispositionIntegrity
        result: list[ConflictCase] = []
        seen: set[str] = set()
        for raw_case in raw_cases:
            if type(raw_case) is not ConflictCase or raw_case.case_id in seen:
                raise ConflictDispositionIntegrity
            seen.add(raw_case.case_id)
            case = self._read_case(raw_case.case_id)
            if case != raw_case or case.request_id is None or case.status != "open":
                raise ConflictDispositionIntegrity
            request = self._read_linked_request(case)
            if request.org_id != canonical.org_id:
                raise ConflictDispositionNotFoundOrDenied
            if not self._is_current_candidate_owner(case, canonical.subject_id):
                raise ConflictDispositionNotFoundOrDenied
            result.append(case)
        return sorted(result, key=lambda case: (case.opened_at, case.case_id))

    def document(
        self,
        case_id: str,
        principal: ConflictOperationsPrincipal,
    ) -> ConflictCase:
        """질문 본문을 담는 Case 상세를 현재 후보 Owner에게만 제공한다."""
        canonical = self._canonical_operations_principal(principal)
        case = self._read_case(case_id)
        request = self._read_linked_request(case)
        if (
            case.status != "open"
            or request.org_id != canonical.org_id
            or not self._is_current_candidate_owner(case, canonical.subject_id)
        ):
            raise ConflictDispositionNotFoundOrDenied
        if self._central_authorizer is not None:
            self._authorize_case(canonical, "conflict.document.read", case, request)
        return case

    def _canonical_operations_principal(
        self,
        principal: object,
    ) -> ConflictOperationsPrincipal:
        if self._central_authorizer is None:
            if type(principal) is not OwnerPrincipal:
                raise ConflictDispositionIntegrity
            try:
                return OwnerPrincipal.model_validate(principal, strict=True)
            except Exception as error:
                raise ConflictDispositionIntegrity from error
        canonical = canonical_authenticated_principal(principal)
        if canonical is None:
            raise ConflictDispositionNotFoundOrDenied
        return canonical

    def _authorize_case(
        self,
        principal: ConflictOperationsPrincipal,
        action: Literal["conflict.concur", "conflict.document.read"],
        case: ConflictCase,
        request: QuestionRequest,
    ) -> None:
        self._authorize_governance(
            principal,
            action,
            ResourceRef(
                org_id=request.org_id,
                kind="conflict_case",
                resource_id=case.case_id,
                owner_subject_id=principal.subject_id,
            ),
        )

    def _authorize_governance(
        self,
        principal: ConflictOperationsPrincipal,
        action: Literal[
            "conflict.list",
            "conflict.concur",
            "conflict.document.read",
        ],
        resource: ResourceRef,
    ) -> None:
        authorizer = self._central_authorizer
        if authorizer is None:
            return
        if type(principal) is not AuthenticatedPrincipal:
            raise ConflictDispositionNotFoundOrDenied
        outcome = authorize_and_verify(authorizer, principal, action, resource)
        if outcome == "unavailable":
            raise ConflictAuthorizationUnavailable
        if outcome == "denied":
            raise ConflictDispositionNotFoundOrDenied

    def _is_current_candidate_owner(self, case: ConflictCase, subject_id: str) -> bool:
        candidates = tuple(
            candidate for candidate in case.candidates if candidate.owner == subject_id
        )
        if not candidates:
            return False
        try:
            with self._registry.consistency_guard():
                return any(
                    self._registry.get(candidate.agent_id).owner == subject_id
                    and domain_authorized(case.intent, self._registry.get(candidate.agent_id))
                    for candidate in candidates
                )
        except Exception:
            return False

    def _concur_canonical(
        self,
        canonical: ConcurOnConflict,
    ) -> P17DirectConcurrenceResult:
        case = self._read_case(canonical.case_id)
        request = self._read_linked_request(case)
        self._validate_request_context(case, request, canonical)
        if canonical.expected_round != case.concurrence_round:
            self._forget_reserved_recovery(case.case_id)
        if case.status == "resolved":
            return self._recover_resolved(case, request, canonical)
        if case.status == "escalated":
            self._validate_escalated_follower(case, canonical)
            raise ConflictDispositionInProgress("Conflict escalation이 진행 중입니다.")
        if case.status != "open":
            raise ConflictDispositionInProgress("Conflict escalation이 진행 중입니다.")
        if not isinstance(request.state, AwaitingConflict):
            return self._recover_after_request_cas(case, request, canonical)

        sealed_follower = self._sealed_consensus_follower(case.case_id, canonical)
        if sealed_follower is not None:
            attempt: ConflictConcurrenceAttempt = sealed_follower
        else:
            try:
                raw_attempt = self._conflicts.reserve_validated_concurrence(
                    case.case_id,
                    canonical,
                    validate=self._validate_for_store,
                )
            except ConflictDispositionError:
                raise
            except Exception as error:
                raise ConflictDispositionDependency(
                    "Conflict Store를 확인할 수 없습니다."
                ) from error
            attempt = self._canonical_concurrence_attempt(raw_attempt)

        if isinstance(attempt, ConcurrencePendingStored):
            return ConcurrencePending(
                request_id=request.request_id,
                case_id=case.case_id,
                current_round=attempt.current_round,
                pending_owners=attempt.pending_owners,
            )
        if isinstance(attempt, ConflictClaimInProgress):
            recovery = self._reserved_recovery_for(canonical)
            if recovery is None:
                raise ConflictDispositionInProgress("같은 consensus claim이 진행 중입니다.")
            claim, control = recovery
            sealed_available: SealedConflictClaimAvailable | None = None
        elif isinstance(attempt, ConflictClaimConflict):
            raise ConflictDispositionConflict("다른 concurrence action이 선점했습니다.")
        elif isinstance(attempt, SealedConflictClaimAvailable):
            if isinstance(attempt.claim, SealedDeadlockClaim):
                raise ConflictDispositionInProgress("Conflict escalation은 S3에서 이어집니다.")
            sealed_available = attempt
            control: ConflictReservationControlToken | None = None
            claim: ReservedConsensusClaim | SealedConsensusClaim = attempt.claim
        else:
            sealed_available = None
            control = attempt.control_token
            claim = attempt.claim

        if control is not None:
            if not isinstance(claim, ReservedConsensusClaim):
                raise ConflictDispositionIntegrity("reserved consensus claim이 아닙니다.")
            self._validate_consensus_reservation(claim, control)
        elif sealed_available is not None:
            self._validate_sealed_available(sealed_available)

        assignment = RequestRouteGrantAssignment(
            org_id=claim.org_id,
            request_id=claim.request_id,
            intent=claim.intent,
            agent_id=claim.primary,
            source=FromOwnerConsensusGrant(case_id=claim.case_id, round=claim.round),
            idempotency_key=claim.idempotency_key,
        )
        try:
            raw_grant = self._route_authority.grant_for_request(assignment)
        except Exception as error:
            if control is not None and isinstance(claim, ReservedConsensusClaim):
                self._seal(claim, control)
            raise ConflictDispositionDependency(
                "Request Route Authority write가 불명확합니다."
            ) from error
        try:
            grant_result = self._canonical_grant_result(raw_grant)
        except ConflictDispositionError:
            if control is not None and isinstance(claim, ReservedConsensusClaim):
                self._seal(claim, control)
            raise
        if isinstance(grant_result, RequestRouteGrantRejected):
            if grant_result.idempotency_key != claim.idempotency_key:
                if control is not None and isinstance(claim, ReservedConsensusClaim):
                    self._seal(claim, control)
                raise ConflictDispositionIntegrity("Authority reject idempotency key가 다릅니다.")
            if control is None or not isinstance(claim, ReservedConsensusClaim):
                raise ConflictDispositionIntegrity("sealed claim이 policy reject를 받았습니다.")
            try:
                raw_advanced = self._conflicts.abandon_unmutated_consensus_round(
                    claim,
                    control_token=control,
                    rejection=grant_result,
                )
            except ConflictDispositionError:
                self._remember_reserved_recovery(claim, control)
                raise
            except Exception as error:
                self._remember_reserved_recovery(claim, control)
                raise ConflictDispositionDependency(
                    "Consensus round abandon에 실패했습니다."
                ) from error
            advanced = self._canonical_case_result(raw_advanced, claim.case_id)
            stored_advanced = self._read_case(claim.case_id)
            expected_advanced = case.advance_concurrence_round()
            if advanced != stored_advanced or stored_advanced != expected_advanced:
                raise ConflictDispositionIntegrity("Consensus round abandon read-back이 다릅니다.")
            self._forget_reserved_recovery(claim.case_id)
            return ConsensusRouteRejected(
                request_id=claim.request_id,
                case_id=claim.case_id,
                current_round=claim.round,
                next_round=advanced.concurrence_round,
                reason_code=grant_result.reason_code,
            )
        if isinstance(grant_result, RequestRouteGrantConflict):
            if control is not None and isinstance(claim, ReservedConsensusClaim):
                self._seal(claim, control)
            raise ConflictDispositionConflict("Request Route Authority first-winner가 다릅니다.")
        if grant_result.assignment != assignment:
            if control is not None and isinstance(claim, ReservedConsensusClaim):
                self._seal(claim, control)
            raise ConflictDispositionIntegrity("Authority receipt assignment가 다릅니다.")
        if control is not None:
            if not isinstance(claim, ReservedConsensusClaim):
                raise ConflictDispositionIntegrity("reserved consensus claim이 아닙니다.")
            sealed_available = self._seal(claim, control)
        if sealed_available is None or not isinstance(sealed_available.claim, SealedConsensusClaim):
            raise ConflictDispositionIntegrity("sealed consensus claim을 찾을 수 없습니다.")
        sealed = sealed_available.claim
        handle = sealed_available.handle

        try:
            raw_readback = self._route_authority.authorize_for_request(
                sealed.org_id,
                sealed.request_id,
                sealed.intent,
                sealed.primary,
            )
        except Exception as error:
            raise ConflictDispositionDependency(
                "Request Route Authority read가 실패했습니다."
            ) from error
        try:
            if type(raw_readback) is not AuthorityGrant:
                raise TypeError("AuthorityGrant exact type이 필요합니다.")
            readback = AuthorityGrant.model_validate(
                raw_readback.model_dump(mode="python", round_trip=True), strict=True
            )
        except Exception as error:
            raise ConflictDispositionIntegrity("Authority read-back이 손상됐습니다.") from error
        if readback.policy_version != grant_result.grant_version:
            raise ConflictDispositionIntegrity("Authority receipt와 read-back version이 다릅니다.")

        revalidated = self._validate_for_store(case, canonical)
        if not isinstance(revalidated, ValidatedOwnerVote):
            raise ConflictDispositionIntegrity("Authority write 뒤 Registry가 drift했습니다.")
        if revalidated.target_requires_approval != sealed.requires_approval:
            raise ConflictDispositionIntegrity("Approval snapshot이 consensus claim과 다릅니다.")

        route = RouteTarget(
            intent=sealed.intent,
            agent_id=sealed.primary,
            requires_approval=sealed.requires_approval,
            authority_version=grant_result.grant_version,
        )
        evidence = self._resolution_evidence(sealed, route)
        try:
            raw = self._conflicts.record_resolution_evidence(handle, evidence)
            if raw is not None:
                raise ConflictDispositionIntegrity(
                    "Resolution evidence Store 반환값이 손상됐습니다."
                )
        except ConflictDispositionError:
            raise
        except Exception as error:
            raise ConflictDispositionDependency(
                "Resolution evidence 저장이 실패했습니다."
            ) from error

        current_request = self._advance_request(sealed, route)
        rationale = _canonical_resolution_rationale(sealed.candidate_snapshot, sealed.votes)
        target_case = case.resolve_for_request(sealed.primary, rationale)
        try:
            resolved_case = self._conflicts.transition_for_claim(handle, target=target_case)
        except ConflictDispositionError:
            raise
        except Exception as error:
            raise ConflictDispositionDependency("ConflictCase resolve가 실패했습니다.") from error
        if resolved_case != target_case:
            raise ConflictDispositionIntegrity("ConflictCase resolve read-back이 다릅니다.")
        wake = self._wake(current_request.request_id)
        return ConflictResolved(
            request_id=current_request.request_id,
            case_id=sealed.case_id,
            route=route,
            wake=wake,
        )

    def _read_case(self, case_id: str) -> ConflictCase:
        try:
            raw = self._conflicts.get_request_case(case_id)
        except Exception as error:
            raise ConflictDispositionDependency("ConflictCase read가 실패했습니다.") from error
        if raw is None:
            raise ConflictDispositionNotFound("request-aware ConflictCase가 없습니다.")
        return self._canonical_case_result(raw, case_id)

    @staticmethod
    def _canonical_case_result(raw: ConflictCase, case_id: str) -> ConflictCase:
        if type(raw) is not ConflictCase or raw.case_id != case_id:
            raise ConflictDispositionIntegrity("ConflictCase 반환값이 손상됐습니다.")
        return deepcopy(raw)

    def _sealed_consensus_follower(
        self,
        case_id: str,
        command: ConcurOnConflict,
    ) -> SealedConflictClaimAvailable | None:
        try:
            available = self._conflicts.sealed_claim_for_case(case_id)
        except ConflictDispositionError:
            raise
        except Exception as error:
            raise ConflictDispositionDependency(
                "sealed Conflict claim read가 실패했습니다."
            ) from error
        if available is None:
            return None
        canonical = self._canonical_concurrence_attempt(available)
        if not isinstance(canonical, SealedConflictClaimAvailable):
            raise ConflictDispositionIntegrity("sealed Conflict claim 반환이 다릅니다.")
        if not isinstance(canonical.claim, SealedConsensusClaim):
            return None
        fingerprint = ConcurrenceActionFingerprint(
            case_id=command.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        )
        if not _trigger_is_vote(fingerprint, canonical.claim.votes):
            raise ConflictDispositionConflict("sealed consensus의 다른 concurrence action입니다.")
        return canonical

    def _read_linked_request(self, case: ConflictCase) -> QuestionRequest:
        if case.request_id is None:
            raise ConflictDispositionInvalid("request-aware ConflictCase가 아닙니다.")
        try:
            request = self._requests.get(case.request_id)
        except Exception as error:
            raise ConflictDispositionDependency("Question Request read가 실패했습니다.") from error
        if request is None:
            raise ConflictDispositionIntegrity("linked Question Request가 없습니다.")
        return request

    @staticmethod
    def _validate_request_context(
        case: ConflictCase,
        request: QuestionRequest,
        command: ConcurOnConflict,
    ) -> None:
        if command.principal.org_id != request.org_id:
            raise ConflictDispositionForbidden("다른 조직의 concurrence action입니다.")
        if (
            case.request_id != request.request_id
            or case.question != request.question
            or case.intent != request.intent
            or request.initial_disposition != "contested"
        ):
            raise ConflictDispositionIntegrity("Case와 Question Request가 다릅니다.")
        if isinstance(request.state, AwaitingConflict):
            if request.state.case_id != case.case_id:
                raise ConflictDispositionIntegrity("Request가 다른 ConflictCase를 가리킵니다.")

    def _validate_for_store(
        self,
        case: ConflictCase,
        command: ConcurOnConflict,
    ) -> ValidatedConcurrence:
        request = self._read_linked_request(case)
        self._validate_request_context(case, request, command)
        if not isinstance(request.state, AwaitingConflict):
            raise ConflictDispositionConflict(
                "Question Request가 concurrence 대기 상태가 아닙니다."
            )
        trigger = ConcurrenceActionFingerprint(
            case_id=case.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        )
        stored_owners = tuple(dict.fromkeys(candidate.owner for candidate in case.candidates))
        current_owners: list[str] = []
        observed_causes: set[_CandidateRegistryReason] = set()
        target_card: AgentCard | None = None
        with self._registry.consistency_guard():
            for candidate in case.candidates:
                try:
                    card = self._registry.get(candidate.agent_id)
                except KeyError:
                    observed_causes.add("candidate_missing")
                    continue
                try:
                    self._registry.get_user(card.owner)
                except KeyError:
                    observed_causes.add("owner_missing")
                    continue
                current_owners.append(card.owner)
                if card.owner != candidate.owner:
                    observed_causes.add("owner_changed")
                elif not domain_authorized(case.intent, card):
                    observed_causes.add("under_claim_changed")
                if card.agent_id == command.on_agent:
                    target_card = card

        cause_code: _CandidateRegistryReason | None = None
        for reason in _REGISTRY_DRIFT_PRIORITY:
            if reason in observed_causes:
                cause_code = reason
                break
        if cause_code is not None:
            if command.principal.subject_id not in (*stored_owners, *current_owners):
                raise ConflictDispositionForbidden("Registry drift와 무관한 principal입니다.")
            return ValidatedRegistryEscalation(
                request_id=request.request_id,
                case_id=case.case_id,
                org_id=request.org_id,
                intent=case.intent,
                candidate_snapshot=case.candidates,
                trigger=trigger,
                cause=CandidateRegistryChanged(
                    round=case.concurrence_round,
                    reason_code=cause_code,
                ),
            )
        if command.principal.subject_id not in stored_owners:
            raise ConflictDispositionForbidden("원 후보 Owner가 아닙니다.")
        if command.on_agent not in case.candidate_ids() or target_card is None:
            raise ConflictDispositionInvalid("on_agent는 현재 유효한 원 후보여야 합니다.")
        evidence = OwnerConcurrenceEvidence(
            round=case.concurrence_round,
            owner_id=command.principal.subject_id,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        )
        return ValidatedOwnerVote(
            request_id=request.request_id,
            case_id=case.case_id,
            org_id=request.org_id,
            intent=case.intent,
            candidate_snapshot=case.candidates,
            trigger=trigger,
            evidence=evidence,
            target_requires_approval=case.intent in target_card.approval_when,
        )

    @staticmethod
    def _canonical_grant_result(raw: RequestRouteGrantResult) -> RequestRouteGrantResult:
        try:
            if type(raw) is RequestRouteGrantReceipt:
                return RequestRouteGrantReceipt.model_validate(
                    raw.model_dump(mode="python", round_trip=True), strict=True
                )
            if type(raw) is RequestRouteGrantRejected:
                return RequestRouteGrantRejected.model_validate(
                    raw.model_dump(mode="python", round_trip=True), strict=True
                )
            if type(raw) is RequestRouteGrantConflict:
                return RequestRouteGrantConflict.model_validate(
                    raw.model_dump(mode="python", round_trip=True), strict=True
                )
        except Exception as error:
            raise ConflictDispositionIntegrity("Authority result가 손상됐습니다.") from error
        raise ConflictDispositionIntegrity("Authority result exact type이 아닙니다.")

    @staticmethod
    def _canonical_concurrence_attempt(
        raw: ConflictConcurrenceAttempt,
    ) -> ConflictConcurrenceAttempt:
        try:
            if type(raw) is ConcurrencePendingStored:
                return ConcurrencePendingStored.model_validate(raw, strict=True).model_copy(
                    deep=True
                )
            if type(raw) is ConflictClaimAcquired:
                return ConflictClaimAcquired.model_validate(raw, strict=True).model_copy(deep=True)
            if type(raw) is ConflictClaimInProgress:
                return ConflictClaimInProgress.model_validate(raw, strict=True).model_copy(
                    deep=True
                )
            if type(raw) is SealedConflictClaimAvailable:
                return SealedConflictClaimAvailable.model_validate(raw, strict=True).model_copy(
                    deep=True
                )
            if type(raw) is ConflictClaimConflict:
                return ConflictClaimConflict.model_validate(raw, strict=True).model_copy(deep=True)
        except Exception as error:
            raise ConflictDispositionIntegrity(
                "Conflict concurrence attempt가 손상됐습니다."
            ) from error
        raise ConflictDispositionIntegrity("Conflict concurrence attempt exact type이 아닙니다.")

    def _validate_consensus_reservation(
        self,
        claim: ReservedConsensusClaim,
        control: ConflictReservationControlToken,
    ) -> None:
        try:
            raw = self._conflicts.validate_consensus_reservation(
                claim,
                control_token=control,
            )
        except ConflictDispositionError:
            raise
        except Exception as error:
            raise ConflictDispositionDependency(
                "reserved consensus proof를 확인할 수 없습니다."
            ) from error
        if raw is not None:
            raise ConflictDispositionIntegrity(
                "reserved consensus proof Store 반환값이 손상됐습니다."
            )

    def _validate_sealed_available(
        self,
        available: SealedConflictClaimAvailable,
    ) -> None:
        try:
            raw = self._conflicts.validate_sealed_claim(
                available.claim,
                handle=available.handle,
            )
        except ConflictDispositionError:
            raise
        except Exception as error:
            raise ConflictDispositionDependency(
                "sealed Conflict proof를 확인할 수 없습니다."
            ) from error
        if raw is not None:
            raise ConflictDispositionIntegrity("sealed Conflict proof Store 반환값이 손상됐습니다.")

    def _seal(
        self,
        claim: ReservedConsensusClaim,
        control: ConflictReservationControlToken,
    ) -> SealedConflictClaimAvailable:
        try:
            raw_sealed = self._conflicts.seal_consensus_claim(claim, control_token=control)
        except ConflictDispositionError:
            raise
        except Exception as error:
            try:
                available = self._conflicts.sealed_claim_for_case(claim.case_id)
            except Exception:
                available = None
            if available is not None:
                recovered = self._canonical_concurrence_attempt(available)
                if isinstance(
                    recovered, SealedConflictClaimAvailable
                ) and recovered.claim == _seal_consensus(claim):
                    self._validate_sealed_available(recovered)
                    self._forget_reserved_recovery(claim.case_id)
                    return recovered
            self._remember_reserved_recovery(claim, control)
            raise ConflictDispositionDependency("Consensus claim seal이 실패했습니다.") from error
        sealed_attempt = self._canonical_concurrence_attempt(raw_sealed)
        if not isinstance(sealed_attempt, SealedConflictClaimAvailable):
            raise ConflictDispositionIntegrity("Consensus claim seal 반환이 다릅니다.")
        self._validate_sealed_available(sealed_attempt)
        self._forget_reserved_recovery(claim.case_id)
        return sealed_attempt

    def _reserved_recovery_for(
        self,
        command: ConcurOnConflict,
    ) -> tuple[ReservedConsensusClaim, ConflictReservationControlToken] | None:
        fingerprint = ConcurrenceActionFingerprint(
            case_id=command.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        )
        with self._recovery_lock:
            recovery = self._reserved_recoveries.get(command.case_id)
            if recovery is None or not _trigger_is_vote(fingerprint, recovery[0].votes):
                return None
            return recovery[0].model_copy(deep=True), recovery[1].model_copy(deep=True)

    def _forget_reserved_recovery(self, case_id: str) -> None:
        with self._recovery_lock:
            self._reserved_recoveries.pop(case_id, None)

    def _remember_reserved_recovery(
        self,
        claim: ReservedConsensusClaim,
        control: ConflictReservationControlToken,
    ) -> None:
        with self._recovery_lock:
            self._reserved_recoveries[claim.case_id] = (
                claim.model_copy(deep=True),
                control.model_copy(deep=True),
            )

    @staticmethod
    def _resolution_evidence(
        claim: SealedConsensusClaim,
        route: RouteTarget,
    ) -> ConflictResolutionEvidence:
        by_owner = {vote.owner_id: vote for vote in claim.votes}
        supporting = tuple(
            SupportingKnowledgeEvidence(
                agent_id=candidate.agent_id,
                affirmed_by_owner=candidate.owner,
            )
            for candidate in claim.candidate_snapshot
            if candidate.agent_id != claim.primary
            and by_owner[candidate.owner].stance == "keep_as_complement"
        )
        return ConflictResolutionEvidence(
            request_id=claim.request_id,
            case_id=claim.case_id,
            org_id=claim.org_id,
            intent=claim.intent,
            route=route,
            source=FromDirectConsensus(round=claim.round, votes=claim.votes),
            supporting=supporting,
        )

    def _advance_request(
        self,
        claim: SealedConsensusClaim,
        route: RouteTarget,
    ) -> QuestionRequest:
        try:
            current = self._requests.get(claim.request_id)
        except Exception as error:
            raise ConflictDispositionDependency("Question Request read가 실패했습니다.") from error
        if current is None:
            raise ConflictDispositionIntegrity("Question Request가 사라졌습니다.")
        if isinstance(current.state, AwaitingConflict):
            if current.state.case_id != claim.case_id:
                raise ConflictDispositionIntegrity("Request가 다른 ConflictCase를 가리킵니다.")
            now = self._clock()
            try:
                due_at = self._deadline_policy.deadline_for(
                    current.org_id,
                    "ready_to_dispatch",
                    now,
                )
                target = ReadyToDispatch(
                    route=route,
                    attempt=1,
                    trigger_key=f"request-dispatch:{claim.request_id}:1",
                    handling=HandlingAssignment(
                        kind="system",
                        ref=f"request-dispatch:{claim.request_id}:1",
                        due_at=due_at,
                    ),
                )
                updated = current.transition(target, clock=lambda: now)
                won = self._requests.compare_and_set(
                    claim.request_id,
                    current.revision,
                    current,
                    updated,
                )
            except Exception as error:
                raise ConflictDispositionDependency(
                    "Question Request CAS가 실패했습니다."
                ) from error
            if type(won) is not bool:
                raise ConflictDispositionIntegrity("Question Request CAS 결과 타입이 다릅니다.")
            if won:
                try:
                    reread = self._requests.get(claim.request_id)
                except Exception as error:
                    raise ConflictDispositionDependency(
                        "CAS winner read-back이 실패했습니다."
                    ) from error
                if reread is None or reread != updated:
                    raise ConflictDispositionIntegrity("CAS winner read-back이 다릅니다.")
                return reread
            try:
                reread = self._requests.get(claim.request_id)
            except Exception as error:
                raise ConflictDispositionDependency(
                    "CAS loser read-back이 실패했습니다."
                ) from error
            if reread is None:
                raise ConflictDispositionIntegrity("CAS loser Request를 읽을 수 없습니다.")
            current = reread
        self._validate_resumed_request(current, route)
        return current

    def _validate_resumed_request(
        self,
        request: QuestionRequest,
        route: RouteTarget,
        *,
        allow_grounding_failure: bool = False,
    ) -> None:
        state = request.state
        if isinstance(state, ReadyToDispatch):
            trigger_key = f"request-dispatch:{request.request_id}:1"
            if (
                state.route == route
                and state.attempt == 1
                and state.trigger_key == trigger_key
                and state.handling.kind == "system"
                and state.handling.ref == trigger_key
                and request.revision == 2
            ):
                return
        elif isinstance(state, (AwaitingAnswer, AwaitingApproval)):
            if state.route == route and state.attempt == 1:
                return
        elif isinstance(state, AnsweredRequest):
            self._validate_completion(request, route)
            return
        elif isinstance(state, FailedRequest):
            if allow_grounding_failure and state.error_code in (
                "required_grounding_missing",
                "required_grounding_invalid",
            ):
                if request.revision != 3:
                    raise ConflictDispositionIntegrity(
                        "Grounding Failed Request revision이 consensus 수명과 다릅니다."
                    )
                return
            raise ConflictDispositionConflict("Question Request가 Failed로 종결됐습니다.")
        raise ConflictDispositionIntegrity("Question Request resume winner가 다릅니다.")

    def _validate_escalated_follower(
        self,
        case: ConflictCase,
        command: ConcurOnConflict,
    ) -> None:
        available = self._conflicts.sealed_claim_for_case(case.case_id)
        if available is None:
            raise ConflictDispositionIntegrity("escalated Case의 sealed deadlock claim이 없습니다.")
        if not isinstance(available.claim, SealedDeadlockClaim):
            raise ConflictDispositionIntegrity("escalated Case의 sealed deadlock claim이 없습니다.")
        claim = available.claim
        fingerprint = ConcurrenceActionFingerprint(
            case_id=command.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        )
        if isinstance(claim.cause, DivergentVotes):
            follows = _trigger_is_vote(fingerprint, claim.votes)
        else:
            follows = fingerprint == claim.trigger
        if not follows:
            raise ConflictDispositionConflict("escalated Case의 다른 concurrence action입니다.")

    def _validate_completion(self, request: QuestionRequest, route: RouteTarget) -> None:
        assert isinstance(request.state, AnsweredRequest)
        try:
            by_request = self._completion_reader.by_request(request.request_id)
            by_record = self._completion_reader.by_record(request.state.record_id)
            first = canonical_completion_bundle(by_request)
            second = canonical_completion_bundle(by_record)
        except Exception as error:
            raise ConflictDispositionIntegrity("Completion evidence가 손상됐습니다.") from error
        if (
            first != second
            or first.request != request
            or first.terminal_audit.route != route
            or first.terminal_audit.attempt != 1
            or first.terminal_audit.record_id != request.state.record_id
        ):
            raise ConflictDispositionIntegrity("Answered completion이 consensus route와 다릅니다.")

    def _recover_resolved(
        self,
        case: ConflictCase,
        request: QuestionRequest,
        command: ConcurOnConflict,
    ) -> ConflictResolved:
        available = self._conflicts.sealed_claim_for_case(case.case_id)
        if available is None or not isinstance(available.claim, SealedConsensusClaim):
            raise ConflictDispositionIntegrity("resolved Case의 sealed claim이 없습니다.")
        claim = available.claim
        fingerprint = ConcurrenceActionFingerprint(
            case_id=command.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        )
        if not _trigger_is_vote(fingerprint, claim.votes):
            raise ConflictDispositionConflict("terminal Case의 다른 concurrence action입니다.")
        evidence = self._conflicts.resolution_evidence_for_request(claim.request_id)
        if evidence is None:
            raise ConflictDispositionIntegrity("resolved Case의 resolution evidence가 없습니다.")
        self._validate_stored_evidence(claim, evidence)
        self._validate_resumed_request(
            request,
            evidence.route,
            allow_grounding_failure=True,
        )
        rationale = _canonical_resolution_rationale(claim.candidate_snapshot, claim.votes)
        if case.resolution != Resolution(
            intent=claim.intent, primary=claim.primary, rationale=rationale
        ):
            raise ConflictDispositionIntegrity("resolved Case가 sealed claim과 다릅니다.")
        wake = self._wake(request.request_id)
        return ConflictResolved(
            request_id=request.request_id,
            case_id=case.case_id,
            route=evidence.route,
            wake=wake,
        )

    def _recover_after_request_cas(
        self,
        case: ConflictCase,
        request: QuestionRequest,
        command: ConcurOnConflict,
    ) -> ConflictResolved:
        available = self._conflicts.sealed_claim_for_case(case.case_id)
        if available is None or not isinstance(available.claim, SealedConsensusClaim):
            raise ConflictDispositionIntegrity("resume 가능한 sealed consensus claim이 없습니다.")
        claim = available.claim
        fingerprint = ConcurrenceActionFingerprint(
            case_id=command.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        )
        if not _trigger_is_vote(fingerprint, claim.votes):
            raise ConflictDispositionConflict("다른 concurrence action은 복구할 수 없습니다.")
        evidence = self._conflicts.resolution_evidence_for_request(claim.request_id)
        if evidence is None:
            raise ConflictDispositionIntegrity("resume 가능한 resolution evidence가 없습니다.")
        self._validate_stored_evidence(claim, evidence)
        self._validate_resumed_request(request, evidence.route)
        rationale = _canonical_resolution_rationale(claim.candidate_snapshot, claim.votes)
        target = case.resolve_for_request(claim.primary, rationale)
        try:
            resolved = self._conflicts.transition_for_claim(available.handle, target=target)
        except ConflictDispositionError:
            raise
        except Exception as error:
            raise ConflictDispositionDependency(
                "ConflictCase forward repair가 실패했습니다."
            ) from error
        if resolved != target:
            raise ConflictDispositionIntegrity("ConflictCase forward repair read-back이 다릅니다.")
        wake = self._wake(request.request_id)
        return ConflictResolved(
            request_id=request.request_id,
            case_id=case.case_id,
            route=evidence.route,
            wake=wake,
        )

    @classmethod
    def _validate_stored_evidence(
        cls,
        claim: SealedConsensusClaim,
        evidence: ConflictResolutionEvidence,
    ) -> None:
        if evidence.route.authority_version is None:
            raise ConflictDispositionIntegrity(
                "Resolution evidence에 Authority version이 없습니다."
            )
        expected = cls._resolution_evidence(claim, evidence.route)
        if evidence != expected:
            raise ConflictDispositionIntegrity("저장된 resolution evidence가 claim과 다릅니다.")

    def _wake(self, request_id: str) -> ExecutionWake:
        try:
            raw = self._execution_starter.ensure_started(request_id)
        except Exception as error:
            raise ConflictDispositionDependency(
                "Question execution wake가 실패했습니다."
            ) from error
        try:
            if type(raw) is ExecutionStarted:
                return ExecutionStarted.model_validate(raw, strict=True).model_copy(deep=True)
            if type(raw) is ExecutionAlreadyRunning:
                return ExecutionAlreadyRunning.model_validate(raw, strict=True).model_copy(
                    deep=True
                )
            if type(raw) is ExecutionNotNeeded:
                return ExecutionNotNeeded.model_validate(raw, strict=True).model_copy(deep=True)
            if type(raw) is ExecutionDeferred:
                return ExecutionDeferred.model_validate(raw, strict=True).model_copy(deep=True)
        except Exception as error:
            raise ConflictDispositionIntegrity(
                "Question execution wake 반환값이 손상됐습니다."
            ) from error
        raise ConflictDispositionIntegrity("Question execution wake exact type이 아닙니다.")


class P17ConflictDispositionApplication:
    """Direct concurrence와 Deadlock escalation을 조합한 S3 request-aware facade."""

    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        conflicts: RequestAwareConflictDispositionStore,
        managers: RequestAwareConflictEscalationManagerStore,
        registry: Registry,
        route_authority: RequestRouteAuthority,
        completion_reader: QuestionCompletionReader,
        deadline_policy: HandlingDeadlinePolicy,
        execution_starter: QuestionExecutionStarter,
        clock: Callable[[], datetime],
        root_user_id: str,
        manager_item_id_factory: Callable[[], object] = _default_id,
        request_locks: RequestLockPool | None = None,
        central_authorizer: CentralAuthorizer | None = None,
    ) -> None:
        self._requests = requests
        self._conflicts = conflicts
        self._managers = managers
        self._registry = registry
        self._route_authority = route_authority
        self._completion_reader = completion_reader
        self._deadline_policy = deadline_policy
        self._execution_starter = execution_starter
        self._clock = clock
        self._root_user_id = root_user_id
        self._manager_item_id_factory = manager_item_id_factory
        self._direct = P17DirectConflictDispositionApplication(
            requests=requests,
            conflicts=conflicts,
            registry=registry,
            route_authority=route_authority,
            completion_reader=completion_reader,
            deadline_policy=deadline_policy,
            execution_starter=execution_starter,
            clock=clock,
            central_authorizer=central_authorizer,
        )
        self._request_locks = request_locks or RequestLockPool()
        # process-local 재시도에서 deadline policy를 두 번 호출하지 않는다. 재시작
        # 내구성은 linked workflow와 함께 P17.9가 맡는다.
        self._manager_deadlines: dict[str, datetime] = {}

    def matches_dependencies(
        self,
        *,
        requests: QuestionRequestStore,
        conflicts: RequestAwareConflictDispositionStore,
        managers: RequestAwareConflictEscalationManagerStore,
        registry: Registry,
        route_authority: RequestRouteAuthority,
        completion_reader: QuestionCompletionReader,
        execution_starter: QuestionExecutionStarter,
    ) -> bool:
        """composition이 소유한 Store·Registry·Authority·실행 경계를 검증한다."""
        return (
            self._requests is requests
            and self._conflicts is conflicts
            and self._managers is managers
            and self._registry is registry
            and self._route_authority is route_authority
            and self._completion_reader is completion_reader
            and self._execution_starter is execution_starter
        )

    def concur(self, command: ConcurOnConflict) -> P17ConcurrenceResult:
        canonical = canonical_concur_command(command)
        try:
            return self._direct.concur(canonical)
        except ConflictDispositionInProgress:
            available = self._read_deadlock_claim(canonical.case_id)
            if available is None:
                raise
            return self._escalate(canonical, available)

    def pending_for(
        self,
        principal: ConflictOperationsPrincipal,
    ) -> list[ConflictCase]:
        return self._direct.pending_for(principal)

    def document(
        self,
        case_id: str,
        principal: ConflictOperationsPrincipal,
    ) -> ConflictCase:
        return self._direct.document(case_id, principal)

    def _read_deadlock_claim(
        self,
        case_id: str,
    ) -> SealedConflictClaimAvailable | None:
        try:
            raw = self._conflicts.sealed_claim_for_case(case_id)
        except ConflictDispositionError:
            raise
        except Exception as error:
            raise ConflictDispositionDependency(
                "sealed Deadlock claim read가 실패했습니다."
            ) from error
        if raw is None:
            return None
        try:
            if type(raw) is not SealedConflictClaimAvailable:
                raise TypeError("SealedConflictClaimAvailable exact type이 필요합니다.")
            available = SealedConflictClaimAvailable.model_validate(raw, strict=True).model_copy(
                deep=True
            )
        except Exception as error:
            raise ConflictDispositionIntegrity("sealed Deadlock claim이 손상됐습니다.") from error
        if not isinstance(available.claim, SealedDeadlockClaim):
            return None
        if available.claim.case_id != case_id:
            raise ConflictDispositionIntegrity("sealed Deadlock claim의 Case ID가 다릅니다.")
        try:
            validation = self._conflicts.validate_sealed_claim(
                available.claim,
                handle=available.handle,
            )
        except ConflictDispositionError:
            raise
        except Exception as error:
            raise ConflictDispositionDependency(
                "sealed Deadlock proof를 확인할 수 없습니다."
            ) from error
        if validation is not None:
            raise ConflictDispositionIntegrity("sealed Deadlock proof Store 반환값이 손상됐습니다.")
        return available

    def _escalate(
        self,
        command: ConcurOnConflict,
        available: SealedConflictClaimAvailable,
    ) -> ConflictEscalated:
        if not isinstance(available.claim, SealedDeadlockClaim):
            raise ConflictDispositionIntegrity("sealed Deadlock claim이 아닙니다.")
        with self._request_locks.hold(available.claim.request_id):
            # lock 진입 전 읽은 snapshot을 신뢰하지 않고 full handle과 claim을 다시 읽는다.
            latest = self._read_deadlock_claim(command.case_id)
            if latest is None or latest != available:
                raise ConflictDispositionIntegrity("sealed Deadlock claim이 바뀌었습니다.")
            claim = latest.claim
            assert isinstance(claim, SealedDeadlockClaim)
            self._validate_deadlock_follower(claim, command)
            case = self._read_case(claim.case_id)
            request = self._read_linked_request(case)
            self._validate_request_context(case, request, command)
            self._validate_deadlock_context(case, request, claim)

            item = self._get_or_create_manager_item(case, request, claim)
            if not isinstance(item.source, FromDeadlock):
                raise ConflictDispositionIntegrity("ManagerItem 출처가 FromDeadlock이 아닙니다.")
            due_at = self._deadline_for_pending_request(request, item)
            target_case = item.source.case.escalate(item.item_id)
            try:
                stored_case = self._conflicts.transition_for_claim(
                    latest.handle,
                    target=target_case,
                )
            except ConflictDispositionError:
                raise
            except Exception as error:
                raise ConflictDispositionDependency(
                    "ConflictCase escalation 저장이 실패했습니다."
                ) from error
            if stored_case != target_case:
                raise ConflictDispositionIntegrity("ConflictCase escalation read-back이 다릅니다.")

            stored_request = self._advance_to_manager(request, item, due_at=due_at)
            self._validate_three_links(stored_request, stored_case, item, claim)
            return ConflictEscalated(
                request_id=claim.request_id,
                case_id=claim.case_id,
                cause=claim.cause,
                manager_item_id=item.item_id,
            )

    @staticmethod
    def _validate_deadlock_follower(
        claim: SealedDeadlockClaim,
        command: ConcurOnConflict,
    ) -> None:
        fingerprint = ConcurrenceActionFingerprint(
            case_id=command.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        )
        if (
            fingerprint.case_id != claim.case_id
            or fingerprint.org_id != claim.org_id
            or fingerprint.expected_round != claim.round
        ):
            raise ConflictDispositionConflict("sealed Deadlock의 Case·조직·round가 다릅니다.")
        if isinstance(claim.cause, DivergentVotes):
            follows = _trigger_is_vote(fingerprint, claim.votes)
        else:
            follows = fingerprint == claim.trigger
        if not follows:
            raise ConflictDispositionConflict("sealed Deadlock의 다른 concurrence action입니다.")

    @staticmethod
    def _validate_deadlock_context(
        case: ConflictCase,
        request: QuestionRequest,
        claim: SealedDeadlockClaim,
    ) -> None:
        if (
            claim.request_id != request.request_id
            or claim.case_id != case.case_id
            or claim.org_id != request.org_id
            or claim.intent != case.intent
            or claim.intent != request.intent
            or claim.round != case.concurrence_round
            or claim.candidate_snapshot != case.candidates
            or claim.cause.round != claim.round
        ):
            raise ConflictDispositionIntegrity("Deadlock claim과 Case·Request가 다릅니다.")
        if case.status == "escalated":
            if isinstance(request.state, AwaitingConflict):
                if request.revision != 1 or request.state.case_id != case.case_id:
                    raise ConflictDispositionIntegrity(
                        "escalated Case의 AwaitingConflict snapshot이 다릅니다."
                    )
            elif isinstance(request.state, AwaitingManager):
                if (
                    request.revision != 2
                    or request.state.public_kind != "contested"
                    or request.state.item_id != case.manager_item_id
                ):
                    raise ConflictDispositionIntegrity(
                        "escalated Case의 AwaitingManager snapshot이 다릅니다."
                    )
            else:
                raise ConflictDispositionIntegrity("escalated Case와 Request 상태가 다릅니다.")
        elif case.status == "open":
            if (
                not isinstance(request.state, AwaitingConflict)
                or request.revision != 1
                or request.state.case_id != case.case_id
            ):
                raise ConflictDispositionIntegrity("open Case와 Request 상태가 다릅니다.")
        else:
            raise ConflictDispositionIntegrity("Deadlock Case가 open/escalated가 아닙니다.")

    def _get_or_create_manager_item(
        self,
        case: ConflictCase,
        request: QuestionRequest,
        claim: SealedDeadlockClaim,
    ) -> ManagerItem:
        try:
            existing = self._managers.get_by_request(claim.request_id)
        except Exception as error:
            raise ConflictDispositionDependency("ManagerItem read가 실패했습니다.") from error
        if existing is not None:
            self._validate_manager_item(existing, case, request, claim)
            self._validate_manager_item_readback(existing, claim)
            return existing
        if case.status != "open":
            raise ConflictDispositionIntegrity("escalated Case의 ManagerItem이 없습니다.")

        manager_id = self._select_manager(claim)
        created_at = self._validated_item_time(case, request)
        item_id = str(self._manager_item_id_factory())
        if not item_id.strip():
            raise ConflictDispositionIntegrity("ManagerItem ID가 nonblank가 아닙니다.")
        try:
            proposed = ManagerItem.for_request(
                request_id=claim.request_id,
                manager_id=manager_id,
                source=FromDeadlock(
                    case=case,
                    cause=claim.cause,
                    reason=deadlock_reason(claim.cause),
                ),
                created_at=created_at,
                item_id=item_id,
            )
            raw_result = self._managers.create_or_get_for_request(proposed)
        except ValueError as error:
            raise ConflictDispositionIntegrity("ManagerItem fingerprint가 충돌합니다.") from error
        except Exception as error:
            raise ConflictDispositionDependency("ManagerItem 생성이 실패했습니다.") from error
        if type(raw_result) is not tuple or len(raw_result) != 2:
            raise ConflictDispositionIntegrity("ManagerItem create-or-get 결과가 손상됐습니다.")
        stored, created = raw_result
        if type(stored) is not ManagerItem or type(created) is not bool:
            raise ConflictDispositionIntegrity("ManagerItem create-or-get 결과 타입이 다릅니다.")
        self._validate_manager_item(
            stored,
            case,
            request,
            claim,
            expected_manager_id=manager_id,
        )
        if created and stored != proposed:
            raise ConflictDispositionIntegrity("새 ManagerItem 반환값이 입력 원형과 다릅니다.")
        self._validate_manager_item_readback(stored, claim)
        return stored

    def _select_manager(self, claim: SealedDeadlockClaim) -> str:
        try:
            with self._registry.consistency_guard():
                self._registry.get_user(self._root_user_id)
                for candidate in claim.candidate_snapshot:
                    try:
                        card = self._registry.get(candidate.agent_id)
                        owner = self._registry.get_user(card.owner)
                    except KeyError:
                        continue
                    if not domain_authorized(claim.intent, card):
                        continue
                    if owner.manager is None:
                        return self._root_user_id
                    try:
                        self._registry.get_user(owner.manager)
                    except KeyError:
                        return self._root_user_id
                    return owner.manager
                return self._root_user_id
        except KeyError as error:
            raise ConflictDispositionIntegrity("root User가 Registry에 없습니다.") from error
        except ConflictDispositionError:
            raise
        except Exception as error:
            raise ConflictDispositionDependency("Registry manager 선택이 실패했습니다.") from error

    @staticmethod
    def _pre_escalation_case(case: ConflictCase) -> ConflictCase:
        if case.status == "open":
            return case
        if case.status == "escalated":
            return ConflictCase(
                intent=case.intent,
                question=case.question,
                candidates=case.candidates,
                opened_at=case.opened_at,
                case_id=case.case_id,
                request_id=case.request_id,
                concurrence_round=case.concurrence_round,
            )
        raise ConflictDispositionIntegrity("Deadlock Case terminal에서는 escalation할 수 없습니다.")

    @classmethod
    def _validate_manager_item(
        cls,
        item: ManagerItem,
        case: ConflictCase,
        request: QuestionRequest,
        claim: SealedDeadlockClaim,
        *,
        expected_manager_id: str | None = None,
    ) -> None:
        if not isinstance(item.source, FromDeadlock):
            raise ConflictDispositionIntegrity("Request의 기존 ManagerItem 출처가 다릅니다.")
        if (
            item.request_id != claim.request_id
            or (expected_manager_id is not None and item.manager_id != expected_manager_id)
            or item.status != "open"
            or item.resolution is not None
            or item.source.case != cls._pre_escalation_case(case)
            or item.source.cause != claim.cause
            or item.source.reason != deadlock_reason(claim.cause)
        ):
            raise ConflictDispositionIntegrity("ManagerItem이 sealed Deadlock과 다릅니다.")
        if not cls._is_exact_aware_datetime(item.created_at) or item.created_at < max(
            case.opened_at,
            request.updated_at,
        ):
            raise ConflictDispositionIntegrity(
                "ManagerItem created_at이 linked snapshot보다 과거입니다."
            )
        if case.status == "escalated" and case.manager_item_id != item.item_id:
            raise ConflictDispositionIntegrity("Case가 다른 ManagerItem을 가리킵니다.")

    def _validate_manager_item_readback(
        self,
        item: ManagerItem,
        claim: SealedDeadlockClaim,
    ) -> None:
        try:
            by_id = self._managers.get(item.item_id)
            by_request = self._managers.get_by_request(claim.request_id)
            by_case = self._managers.get_by_case(claim.case_id)
        except Exception as error:
            raise ConflictDispositionDependency(
                "ManagerItem exact read-back이 실패했습니다."
            ) from error
        if (
            type(by_id) is not ManagerItem
            or type(by_request) is not ManagerItem
            or type(by_case) is not ManagerItem
            or by_id != item
            or by_request != item
            or by_case != item
        ):
            raise ConflictDispositionIntegrity("ManagerItem exact read-back이 다릅니다.")

    def _validated_item_time(
        self,
        case: ConflictCase,
        request: QuestionRequest,
    ) -> datetime:
        try:
            created_at = self._clock()
        except Exception as error:
            raise ConflictDispositionDependency("ManagerItem clock read가 실패했습니다.") from error
        if not self._is_exact_aware_datetime(created_at):
            raise ConflictDispositionDependency("ManagerItem clock이 aware datetime이 아닙니다.")
        if created_at < max(case.opened_at, request.updated_at):
            raise ConflictDispositionDependency(
                "ManagerItem clock이 linked snapshot보다 과거입니다."
            )
        return created_at

    @staticmethod
    def _is_exact_aware_datetime(value: object) -> bool:
        if type(value) is not datetime:
            return False
        try:
            return value.tzinfo is not None and value.utcoffset() is not None
        except Exception:
            return False

    def _advance_to_manager(
        self,
        initial_request: QuestionRequest,
        item: ManagerItem,
        *,
        due_at: datetime | None,
    ) -> QuestionRequest:
        try:
            current = self._requests.get(initial_request.request_id)
        except Exception as error:
            raise ConflictDispositionDependency("Question Request read가 실패했습니다.") from error
        if current is None:
            raise ConflictDispositionIntegrity("Question Request가 없습니다.")
        if not isinstance(item.source, FromDeadlock):
            raise ConflictDispositionIntegrity("ManagerItem 출처가 FromDeadlock이 아닙니다.")
        if isinstance(current.state, AwaitingManager):
            if (
                current.revision == 2
                and current.state.public_kind == "contested"
                and current.state.item_id == item.item_id
                and current.state.route is None
                and current.state.attempt is None
            ):
                return current
            raise ConflictDispositionIntegrity("Question Request의 Manager winner가 다릅니다.")
        if (
            not isinstance(current.state, AwaitingConflict)
            or current.revision != 1
            or current.state.case_id != item.source.case.case_id
        ):
            raise ConflictDispositionIntegrity(
                "Question Request가 AwaitingConflict revision 1이 아닙니다."
            )

        if due_at is None:
            raise ConflictDispositionIntegrity(
                "AwaitingConflict 전이에 Manager deadline이 없습니다."
            )
        try:
            target = AwaitingManager(
                item_id=item.item_id,
                public_kind="contested",
                handling=HandlingAssignment(
                    kind="manager_item",
                    ref=item.item_id,
                    due_at=due_at,
                ),
            )
            updated = current.transition(target, clock=lambda: item.created_at)
            won = self._requests.compare_and_set(
                current.request_id,
                current.revision,
                current,
                updated,
            )
        except Exception as error:
            raise ConflictDispositionDependency(
                "Question Request Manager CAS가 실패했습니다."
            ) from error
        if type(won) is not bool:
            raise ConflictDispositionIntegrity("Question Request CAS 결과 타입이 다릅니다.")
        try:
            reread = self._requests.get(current.request_id)
        except Exception as error:
            raise ConflictDispositionDependency(
                "Question Request CAS read-back이 실패했습니다."
            ) from error
        if reread is None:
            raise ConflictDispositionIntegrity("Question Request CAS read-back이 없습니다.")
        if won:
            if reread != updated:
                raise ConflictDispositionIntegrity("Question Request CAS winner가 다릅니다.")
            return reread
        if (
            isinstance(reread.state, AwaitingManager)
            and reread.revision == 2
            and reread.state.public_kind == "contested"
            and reread.state.item_id == item.item_id
        ):
            return reread
        raise ConflictDispositionIntegrity("Question Request CAS loser가 다릅니다.")

    def _deadline_for_item(self, request: QuestionRequest, item: ManagerItem) -> datetime:
        existing = self._manager_deadlines.get(item.item_id)
        if existing is not None:
            return existing
        try:
            raw_due_at = self._deadline_policy.deadline_for(
                request.org_id,
                "awaiting_manager",
                item.created_at,
            )
        except Exception as error:
            raise ConflictDispositionDependency("Manager deadline 계산이 실패했습니다.") from error
        if not self._is_exact_aware_datetime(raw_due_at) or raw_due_at < max(
            item.created_at,
            request.updated_at,
        ):
            raise ConflictDispositionDependency(
                "Manager deadline이 유효한 aware datetime이 아닙니다."
            )
        due_at = raw_due_at
        self._manager_deadlines[item.item_id] = due_at
        return due_at

    def _deadline_for_pending_request(
        self,
        request: QuestionRequest,
        item: ManagerItem,
    ) -> datetime | None:
        if isinstance(request.state, AwaitingManager):
            return None
        if not isinstance(request.state, AwaitingConflict):
            raise ConflictDispositionIntegrity("Manager escalation 대상 Request 상태가 다릅니다.")
        return self._deadline_for_item(request, item)

    def _validate_three_links(
        self,
        request: QuestionRequest,
        case: ConflictCase,
        item: ManagerItem,
        claim: SealedDeadlockClaim,
    ) -> None:
        try:
            reread_item = self._managers.get(item.item_id)
            by_request = self._managers.get_by_request(claim.request_id)
            by_case = self._managers.get_by_case(claim.case_id)
            reread_case = self._conflicts.get_request_case(claim.case_id)
            reread_request = self._requests.get(claim.request_id)
        except Exception as error:
            raise ConflictDispositionDependency(
                "escalation exact read-back이 실패했습니다."
            ) from error
        if (
            reread_item != item
            or by_request != item
            or by_case != item
            or reread_case != case
            or reread_request != request
            or case.status != "escalated"
            or case.manager_item_id != item.item_id
            or not isinstance(request.state, AwaitingManager)
            or request.revision != 2
            or request.state.public_kind != "contested"
            or request.state.item_id != item.item_id
        ):
            raise ConflictDispositionIntegrity("ManagerItem·Case·Request link가 다릅니다.")

    def _read_case(self, case_id: str) -> ConflictCase:
        try:
            case = self._conflicts.get_request_case(case_id)
        except Exception as error:
            raise ConflictDispositionDependency("ConflictCase read가 실패했습니다.") from error
        if case is None:
            raise ConflictDispositionNotFound("request-aware ConflictCase가 없습니다.")
        if type(case) is not ConflictCase or case.case_id != case_id:
            raise ConflictDispositionIntegrity("ConflictCase read-back key가 다릅니다.")
        return case

    def _read_linked_request(self, case: ConflictCase) -> QuestionRequest:
        if case.request_id is None:
            raise ConflictDispositionInvalid("request-aware ConflictCase가 아닙니다.")
        try:
            request = self._requests.get(case.request_id)
        except Exception as error:
            raise ConflictDispositionDependency("Question Request read가 실패했습니다.") from error
        if request is None:
            raise ConflictDispositionIntegrity("linked Question Request가 없습니다.")
        return request

    @staticmethod
    def _validate_request_context(
        case: ConflictCase,
        request: QuestionRequest,
        command: ConcurOnConflict,
    ) -> None:
        if command.principal.org_id != request.org_id:
            raise ConflictDispositionForbidden("다른 조직의 concurrence action입니다.")
        if (
            command.case_id != case.case_id
            or case.request_id != request.request_id
            or case.question != request.question
            or case.intent != request.intent
            or request.initial_disposition != "contested"
        ):
            raise ConflictDispositionIntegrity("Case와 Question Request가 다릅니다.")
        if isinstance(request.state, AwaitingConflict) and request.state.case_id != case.case_id:
            raise ConflictDispositionIntegrity("Request가 다른 ConflictCase를 가리킵니다.")
