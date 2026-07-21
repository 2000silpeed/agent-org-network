"""Request-aware Unowned Manager 처분 애플리케이션(P17.4·ADR 0045).

legacy ManagerQueueService와 분리해 한 Question Request의 책임 공백만 닫는다. 이 모듈은
Router·Precedent를 모르며, 중앙 Authority write/read 증거 뒤 같은 Request를 재개하거나
명시적으로 Declined로 종결한다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, Protocol, TypeAlias, assert_never, final, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from agent_org_network.agent_card import domain_authorized
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.answer_finalization import (
    QuestionCompletionReader,
    canonical_completion_bundle,
)
from agent_org_network.conflict import ConflictEscalationCause
from agent_org_network.governance_authorization import (
    authorize_and_verify,
    canonical_authenticated_principal,
)
from agent_org_network.manager_queue import (
    AssignOwner,
    Dismiss,
    FromUnowned,
    ManagerItem,
    ManagerQueueStore,
    ManagerResolution,
    RequestAwareManagerQueueStore,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingApproval,
    AwaitingManager,
    DeclinedRequest,
    FailedRequest,
    HandlingAssignment,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.question_resolution import (
    AuthorityGrant,
    HandlingDeadlinePolicy,
    RouteAuthority,
)
from agent_org_network.registry import Registry, RegistryError
from agent_org_network.request_correlation import require_request_id
from agent_org_network.request_route_authority import RequestRouteGrantRejected

Clock: TypeAlias = Callable[[], datetime]
IdFactory: TypeAlias = Callable[[], object]


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
class ManagerPrincipal(_FrozenDto):
    org_id: str
    subject_id: str


ManagerOperationsPrincipal: TypeAlias = ManagerPrincipal | AuthenticatedPrincipal


@final
class AssignUnownedOwner(_FrozenDto):
    kind: Literal["assign_unowned_owner"] = "assign_unowned_owner"
    principal: ManagerOperationsPrincipal
    item_id: str
    agent_id: str
    rationale: str = ""


@final
class DismissUnowned(_FrozenDto):
    kind: Literal["dismiss_unowned"] = "dismiss_unowned"
    principal: ManagerOperationsPrincipal
    item_id: str
    rationale: str = ""


P17ManagerDispositionCommand: TypeAlias = Annotated[
    AssignUnownedOwner | DismissUnowned,
    Field(discriminator="kind"),
]


@final
class AssignDeadlockedOwner(_FrozenDto):
    kind: Literal["assign_deadlocked_owner"] = "assign_deadlocked_owner"
    principal: ManagerOperationsPrincipal
    item_id: str
    agent_id: str
    rationale: str = ""


@final
class DismissDeadlocked(_FrozenDto):
    kind: Literal["dismiss_deadlocked"] = "dismiss_deadlocked"
    principal: ManagerOperationsPrincipal
    item_id: str
    rationale: str = ""


DeadlockManagerDispositionCommand: TypeAlias = Annotated[
    AssignDeadlockedOwner | DismissDeadlocked,
    Field(discriminator="kind"),
]


@final
class ReservedAssignOwnerClaim(_FrozenDto):
    kind: Literal["reserved_assign_owner"] = "reserved_assign_owner"
    generation: str
    idempotency_key: str
    request_id: str
    item_id: str
    org_id: str
    by_manager: str
    intent: str
    agent_id: str
    requires_approval: bool
    rationale: str


@final
class SealedAssignOwnerClaim(_FrozenDto):
    kind: Literal["sealed_assign_owner"] = "sealed_assign_owner"
    generation: str
    idempotency_key: str
    request_id: str
    item_id: str
    org_id: str
    by_manager: str
    intent: str
    agent_id: str
    requires_approval: bool
    rationale: str


@final
class ReservedDismissClaim(_FrozenDto):
    kind: Literal["reserved_dismiss"] = "reserved_dismiss"
    generation: str
    idempotency_key: str
    request_id: str
    item_id: str
    org_id: str
    by_manager: str
    rationale: str
    reason_code: Literal["manager_declined"] = "manager_declined"


@final
class SealedDismissClaim(_FrozenDto):
    kind: Literal["sealed_dismiss"] = "sealed_dismiss"
    generation: str
    idempotency_key: str
    request_id: str
    item_id: str
    org_id: str
    by_manager: str
    rationale: str
    reason_code: Literal["manager_declined"] = "manager_declined"


ManagerDispositionClaim: TypeAlias = Annotated[
    ReservedAssignOwnerClaim | SealedAssignOwnerClaim | ReservedDismissClaim | SealedDismissClaim,
    Field(discriminator="kind"),
]


class _DeadlockManagerClaimDto(_FrozenDto):
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    item_id: str
    org_id: str
    by_manager: str
    intent: str
    round: int = Field(ge=1)
    cause: ConflictEscalationCause
    rationale: str

    @model_validator(mode="after")
    def _canonical_deadlock_links(self) -> _DeadlockManagerClaimDto:
        if self.idempotency_key != f"manager-disposition:{self.item_id}":
            raise ValueError("Deadlock Manager claim idempotency key가 Item과 다릅니다.")
        if self.cause.round != self.round:
            raise ValueError("Deadlock Manager claim cause round가 claim round와 다릅니다.")
        return self


@final
class ReservedDeadlockAssignClaim(_DeadlockManagerClaimDto):
    kind: Literal["reserved_deadlock_assign"] = "reserved_deadlock_assign"
    agent_id: str
    requires_approval: bool


@final
class SealedDeadlockAssignClaim(_DeadlockManagerClaimDto):
    kind: Literal["sealed_deadlock_assign"] = "sealed_deadlock_assign"
    agent_id: str
    requires_approval: bool


@final
class ReservedDeadlockDismissClaim(_DeadlockManagerClaimDto):
    kind: Literal["reserved_deadlock_dismiss"] = "reserved_deadlock_dismiss"
    reason_code: Literal["manager_declined"] = "manager_declined"


@final
class SealedDeadlockDismissClaim(_DeadlockManagerClaimDto):
    kind: Literal["sealed_deadlock_dismiss"] = "sealed_deadlock_dismiss"
    reason_code: Literal["manager_declined"] = "manager_declined"


DeadlockManagerDispositionClaim: TypeAlias = Annotated[
    ReservedDeadlockAssignClaim
    | SealedDeadlockAssignClaim
    | ReservedDeadlockDismissClaim
    | SealedDeadlockDismissClaim,
    Field(discriminator="kind"),
]


@final
class ReservationControlToken(_FrozenDto):
    generation: str
    token: str


@final
class SealedClaimHandle(_FrozenDto):
    generation: str
    forward_token: str


@final
class DeadlockManagerReservationControlToken(_FrozenDto):
    generation: str
    token: str


@final
class DeadlockManagerSealedClaimHandle(_FrozenDto):
    generation: str
    forward_token: str


@final
class ClaimAcquired(_FrozenDto):
    claim: ReservedAssignOwnerClaim | ReservedDismissClaim
    control_token: ReservationControlToken

    @model_validator(mode="after")
    def _generation_must_match(self) -> ClaimAcquired:
        if self.claim.generation != self.control_token.generation:
            raise ValueError("reservation claim과 control token generation이 다릅니다.")
        return self


@final
class ClaimInProgress(_FrozenDto):
    kind: Literal["in_progress"] = "in_progress"
    retryable: Literal[True] = True


@final
class SealedClaimAvailable(_FrozenDto):
    claim: SealedAssignOwnerClaim | SealedDismissClaim
    handle: SealedClaimHandle

    @model_validator(mode="after")
    def _generation_must_match(self) -> SealedClaimAvailable:
        if self.claim.generation != self.handle.generation:
            raise ValueError("sealed claim과 handle generation이 다릅니다.")
        return self


@final
class ClaimConflict(_FrozenDto):
    kind: Literal["conflict"] = "conflict"


ClaimAttempt: TypeAlias = ClaimAcquired | ClaimInProgress | SealedClaimAvailable | ClaimConflict


@final
class DeadlockManagerClaimAcquired(_FrozenDto):
    kind: Literal["acquired"] = "acquired"
    claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim
    control_token: DeadlockManagerReservationControlToken

    @model_validator(mode="after")
    def _generation_must_match(self) -> DeadlockManagerClaimAcquired:
        if self.claim.generation != self.control_token.generation:
            raise ValueError("Deadlock claim과 control token generation이 다릅니다.")
        return self


@final
class DeadlockManagerClaimInProgress(_FrozenDto):
    kind: Literal["in_progress"] = "in_progress"
    retryable: Literal[True] = True


@final
class DeadlockManagerSealedClaimAvailable(_FrozenDto):
    kind: Literal["sealed"] = "sealed"
    claim: SealedDeadlockAssignClaim | SealedDeadlockDismissClaim
    handle: DeadlockManagerSealedClaimHandle

    @model_validator(mode="after")
    def _generation_must_match(self) -> DeadlockManagerSealedClaimAvailable:
        if self.claim.generation != self.handle.generation:
            raise ValueError("Sealed Deadlock claim과 handle generation이 다릅니다.")
        return self


@final
class DeadlockManagerClaimConflict(_FrozenDto):
    kind: Literal["conflict"] = "conflict"


DeadlockManagerClaimAttempt: TypeAlias = Annotated[
    DeadlockManagerClaimAcquired
    | DeadlockManagerClaimInProgress
    | DeadlockManagerSealedClaimAvailable
    | DeadlockManagerClaimConflict,
    Field(discriminator="kind"),
]


@final
class ResumeEvidence(_FrozenDto):
    request_id: str
    from_revision: int = Field(ge=0)
    to_revision: int = Field(ge=1)
    route: RouteTarget
    attempt: Literal[1] = 1
    trigger_key: str

    @model_validator(mode="after")
    def _revision_and_trigger_must_match(self) -> ResumeEvidence:
        if self.to_revision != self.from_revision + 1:
            raise ValueError("ResumeEvidence revision은 정확히 한 칸 전진해야 합니다.")
        if self.trigger_key != f"request-dispatch:{self.request_id}:1":
            raise ValueError("ResumeEvidence trigger key가 Request attempt와 다릅니다.")
        return self


@final
class ExecutionStarted(_FrozenDto):
    kind: Literal["started"] = "started"


@final
class ExecutionAlreadyRunning(_FrozenDto):
    kind: Literal["already_running"] = "already_running"


@final
class ExecutionNotNeeded(_FrozenDto):
    kind: Literal["not_needed"] = "not_needed"


@final
class ExecutionDeferred(_FrozenDto):
    kind: Literal["deferred"] = "deferred"
    reason_code: str


ExecutionWake: TypeAlias = Annotated[
    ExecutionStarted | ExecutionAlreadyRunning | ExecutionNotNeeded | ExecutionDeferred,
    Field(discriminator="kind"),
]


@final
class TerminalPublished(_FrozenDto):
    kind: Literal["published"] = "published"


@final
class TerminalAlreadyPublished(_FrozenDto):
    kind: Literal["already_published"] = "already_published"


@final
class TerminalDeferred(_FrozenDto):
    kind: Literal["deferred"] = "deferred"
    reason_code: str


TerminalDelivery: TypeAlias = Annotated[
    TerminalPublished | TerminalAlreadyPublished | TerminalDeferred,
    Field(discriminator="kind"),
]


@final
class UnownedOwnerAssigned(_FrozenDto):
    kind: Literal["owner_assigned"] = "owner_assigned"
    request_id: str
    item_id: str
    route: RouteTarget
    wake: ExecutionWake


@final
class UnownedDismissed(_FrozenDto):
    kind: Literal["dismissed"] = "dismissed"
    request_id: str
    item_id: str
    reason_code: Literal["manager_declined"] = "manager_declined"
    delivery: TerminalDelivery


P17ManagerDispositionResult: TypeAlias = Annotated[
    UnownedOwnerAssigned | UnownedDismissed,
    Field(discriminator="kind"),
]


@final
class DeadlockOwnerAssigned(_FrozenDto):
    kind: Literal["deadlock_owner_assigned"] = "deadlock_owner_assigned"
    request_id: str
    case_id: str
    item_id: str
    route: RouteTarget
    wake: ExecutionWake


@final
class DeadlockDismissed(_FrozenDto):
    kind: Literal["deadlock_dismissed"] = "deadlock_dismissed"
    request_id: str
    case_id: str
    item_id: str
    reason_code: Literal["manager_declined"] = "manager_declined"
    delivery: TerminalDelivery


P17DeadlockManagerDispositionResult: TypeAlias = Annotated[
    DeadlockOwnerAssigned | DeadlockDismissed,
    Field(discriminator="kind"),
]


@final
class AuthorityAssignment(_FrozenDto):
    org_id: str
    request_id: str
    item_id: str
    intent: str
    agent_id: str
    assigned_by: str
    idempotency_key: str


@final
class AuthorityAssignmentReceipt(_FrozenDto):
    assignment: AuthorityAssignment
    grant_version: str


@final
class AuthorityAssignmentRejected(_FrozenDto):
    kind: Literal["rejected"] = "rejected"
    authority_write_applied: Literal[False] = False
    idempotency_write_applied: Literal[False] = False
    reason_code: str


AuthorityAssignmentResult: TypeAlias = AuthorityAssignmentReceipt | AuthorityAssignmentRejected


class ManagerDispositionError(RuntimeError):
    """외부 어댑터가 문자열 parsing 없이 매핑하는 닫힌 P17.4 오류."""

    code: str
    retryable: bool

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ManagerDispositionNotFound(ManagerDispositionError):
    code = "manager_disposition_not_found"
    retryable = False

    def __init__(self) -> None:
        super().__init__("Manager 처리 항목을 찾을 수 없습니다.")


class ManagerDispositionForbidden(ManagerDispositionError):
    code = "manager_disposition_forbidden"
    retryable = False

    def __init__(self) -> None:
        super().__init__("이 Manager 처리 항목을 처분할 권한이 없습니다.")


class ManagerDispositionInvalid(ManagerDispositionError):
    code = "manager_disposition_invalid"
    retryable = False

    def __init__(self, message: str = "Manager 처분 요청이 유효하지 않습니다.") -> None:
        super().__init__(message)


class ManagerDispositionConflict(ManagerDispositionError):
    code = "manager_disposition_conflict"
    retryable = False

    def __init__(self) -> None:
        super().__init__("Manager 처리 항목에 다른 처분이 이미 적용되었습니다.")


class ManagerDispositionInProgress(ManagerDispositionError):
    code = "manager_disposition_in_progress"
    retryable = True

    def __init__(self) -> None:
        super().__init__("같은 Manager 처분이 진행 중입니다.")


class ManagerDispositionDependency(ManagerDispositionError):
    code = "manager_disposition_dependency"
    retryable = True

    def __init__(self) -> None:
        super().__init__("Manager 처분 의존성을 일시적으로 확인할 수 없습니다.")


class ManagerAuthorizationUnavailable(ManagerDispositionDependency):
    """중앙 Manager 권한 의존성 장애의 고정 신호."""


class ManagerDispositionNotFoundOrDenied(ManagerDispositionError):
    code = "manager_disposition_not_found_or_denied"
    retryable = False

    def __init__(self) -> None:
        super().__init__("Manager 처리 항목을 찾을 수 없습니다.")


class ManagerDispositionIntegrity(ManagerDispositionError):
    code = "manager_disposition_integrity"
    retryable = False

    def __init__(self) -> None:
        super().__init__("Manager 처분의 저장 증거가 일치하지 않습니다.")


class AuthorityAssignmentConflictError(RuntimeError):
    """같은 Authority idempotency key가 다른 assignment에 사용됨."""


class RequestScopedRouteAuthority(RouteAuthority, Protocol):
    def assign_owner(self, assignment: AuthorityAssignment) -> AuthorityAssignmentResult: ...

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None: ...


@runtime_checkable
class RequestAwareManagerDispositionStore(
    RequestAwareManagerQueueStore,
    ManagerQueueStore,
    Protocol,
):
    def reserve_validated_action(
        self,
        item_id: str,
        command: P17ManagerDispositionCommand,
        validate: Callable[
            [ManagerItem],
            ReservedAssignOwnerClaim | ReservedDismissClaim,
        ],
    ) -> ClaimAttempt: ...

    def claim_for_item(self, item_id: str) -> ManagerDispositionClaim | None: ...

    def validate_action_reservation(
        self,
        claim: ReservedAssignOwnerClaim | ReservedDismissClaim,
        *,
        control_token: ReservationControlToken,
    ) -> None: ...

    def seal_claim(
        self,
        claim: ReservedAssignOwnerClaim | ReservedDismissClaim,
        *,
        control_token: ReservationControlToken,
    ) -> SealedClaimAvailable: ...

    def abandon_unmutated_claim(
        self,
        claim: ReservedAssignOwnerClaim,
        *,
        control_token: ReservationControlToken,
    ) -> None: ...

    def record_resume_evidence(
        self,
        handle: SealedClaimHandle,
        evidence: ResumeEvidence,
    ) -> None: ...

    def resume_evidence_for_claim(
        self,
        handle: SealedClaimHandle,
    ) -> ResumeEvidence | None: ...

    def resolve_for_claim(
        self,
        handle: SealedClaimHandle,
        resolved: ManagerItem,
    ) -> ManagerItem: ...


@runtime_checkable
class RequestAwareDeadlockManagerDispositionStore(
    RequestAwareManagerQueueStore,
    ManagerQueueStore,
    Protocol,
):
    def reserve_validated_deadlock_action(
        self,
        item_id: str,
        command: DeadlockManagerDispositionCommand,
        *,
        validate: Callable[
            [ManagerItem],
            ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        ],
    ) -> DeadlockManagerClaimAttempt: ...

    def deadlock_claim_for_item(
        self,
        item_id: str,
    ) -> DeadlockManagerDispositionClaim | None: ...

    def validate_deadlock_action_reservation(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
    ) -> None: ...

    def seal_deadlock_claim(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
    ) -> DeadlockManagerSealedClaimAvailable: ...

    def abandon_unmutated_deadlock_assign(
        self,
        claim: ReservedDeadlockAssignClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> None: ...

    def deadlock_claim_for_handle(
        self,
        handle: DeadlockManagerSealedClaimHandle,
    ) -> SealedDeadlockAssignClaim | SealedDeadlockDismissClaim: ...

    def record_resume_evidence(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
        evidence: ResumeEvidence,
    ) -> None: ...

    def resume_evidence_for_claim(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
    ) -> ResumeEvidence | None: ...

    def resolve_for_claim(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
        resolved: ManagerItem,
    ) -> ManagerItem: ...


class QuestionExecutionStarter(Protocol):
    def ensure_started(self, request_id: str) -> ExecutionWake: ...


class QuestionTerminalPublisher(Protocol):
    def publish_terminal(self, request_id: str) -> TerminalDelivery: ...


def manager_claim_matches_command(
    claim: ManagerDispositionClaim,
    command: P17ManagerDispositionCommand,
) -> bool:
    """Store가 기존 winner와 새 command의 semantic fingerprint를 비교한다."""
    match claim, command:
        case (
            (ReservedAssignOwnerClaim() | SealedAssignOwnerClaim()),
            AssignUnownedOwner(),
        ):
            return (
                claim.item_id == command.item_id
                and claim.org_id == command.principal.org_id
                and claim.by_manager == command.principal.subject_id
                and claim.agent_id == command.agent_id
                and claim.rationale == command.rationale
            )
        case ((ReservedDismissClaim() | SealedDismissClaim()), DismissUnowned()):
            return (
                claim.item_id == command.item_id
                and claim.org_id == command.principal.org_id
                and claim.by_manager == command.principal.subject_id
                and claim.rationale == command.rationale
                and claim.reason_code == "manager_declined"
            )
        case _:
            return False


def canonical_manager_command(
    command: P17ManagerDispositionCommand,
) -> AssignUnownedOwner | DismissUnowned:
    try:
        if type(command) is AssignUnownedOwner:
            return AssignUnownedOwner.model_validate(
                command.model_dump(mode="python", round_trip=True), strict=True
            )
        if type(command) is DismissUnowned:
            return DismissUnowned.model_validate(
                command.model_dump(mode="python", round_trip=True), strict=True
            )
    except Exception as error:
        raise ManagerDispositionIntegrity() from error
    raise ManagerDispositionIntegrity()


def canonical_deadlock_manager_command(
    command: DeadlockManagerDispositionCommand,
) -> AssignDeadlockedOwner | DismissDeadlocked:
    try:
        if type(command) is AssignDeadlockedOwner:
            return AssignDeadlockedOwner.model_validate(
                command.model_dump(mode="python", round_trip=True), strict=True
            )
        if type(command) is DismissDeadlocked:
            return DismissDeadlocked.model_validate(
                command.model_dump(mode="python", round_trip=True), strict=True
            )
    except Exception as error:
        raise ManagerDispositionIntegrity() from error
    raise ManagerDispositionIntegrity()


def canonical_deadlock_manager_claim(
    claim: DeadlockManagerDispositionClaim,
) -> DeadlockManagerDispositionClaim:
    try:
        if type(claim) is ReservedDeadlockAssignClaim:
            return ReservedDeadlockAssignClaim.model_validate(
                claim.model_dump(mode="python", round_trip=True), strict=True
            )
        if type(claim) is SealedDeadlockAssignClaim:
            return SealedDeadlockAssignClaim.model_validate(
                claim.model_dump(mode="python", round_trip=True), strict=True
            )
        if type(claim) is ReservedDeadlockDismissClaim:
            return ReservedDeadlockDismissClaim.model_validate(
                claim.model_dump(mode="python", round_trip=True), strict=True
            )
        if type(claim) is SealedDeadlockDismissClaim:
            return SealedDeadlockDismissClaim.model_validate(
                claim.model_dump(mode="python", round_trip=True), strict=True
            )
    except Exception as error:
        raise ManagerDispositionIntegrity() from error
    raise ManagerDispositionIntegrity()


def deadlock_manager_claim_matches_command(
    claim: DeadlockManagerDispositionClaim,
    command: DeadlockManagerDispositionCommand,
) -> bool:
    match claim, command:
        case (
            (ReservedDeadlockAssignClaim() | SealedDeadlockAssignClaim()),
            AssignDeadlockedOwner(),
        ):
            return (
                claim.item_id == command.item_id
                and claim.org_id == command.principal.org_id
                and claim.by_manager == command.principal.subject_id
                and claim.agent_id == command.agent_id
                and claim.rationale == command.rationale
            )
        case (
            (ReservedDeadlockDismissClaim() | SealedDeadlockDismissClaim()),
            DismissDeadlocked(),
        ):
            return (
                claim.item_id == command.item_id
                and claim.org_id == command.principal.org_id
                and claim.by_manager == command.principal.subject_id
                and claim.rationale == command.rationale
                and claim.reason_code == "manager_declined"
            )
        case _:
            return False


def _normalized_intent(value: str) -> str | None:
    normalized = value.strip()
    return normalized if normalized else None


def _canonical_claim_attempt(attempt: ClaimAttempt) -> ClaimAttempt:
    try:
        if type(attempt) is ClaimAcquired:
            return ClaimAcquired.model_validate(
                attempt.model_dump(mode="python", round_trip=True), strict=True
            )
        if type(attempt) is ClaimInProgress:
            return ClaimInProgress.model_validate(
                attempt.model_dump(mode="python", round_trip=True), strict=True
            )
        if type(attempt) is SealedClaimAvailable:
            return SealedClaimAvailable.model_validate(
                attempt.model_dump(mode="python", round_trip=True), strict=True
            )
        if type(attempt) is ClaimConflict:
            return ClaimConflict.model_validate(
                attempt.model_dump(mode="python", round_trip=True), strict=True
            )
    except Exception as error:
        raise ManagerDispositionIntegrity() from error
    raise ManagerDispositionIntegrity()


def canonical_manager_claim(claim: ManagerDispositionClaim) -> ManagerDispositionClaim:
    try:
        if type(claim) is ReservedAssignOwnerClaim:
            return ReservedAssignOwnerClaim.model_validate(claim.model_dump(), strict=True)
        if type(claim) is SealedAssignOwnerClaim:
            return SealedAssignOwnerClaim.model_validate(claim.model_dump(), strict=True)
        if type(claim) is ReservedDismissClaim:
            return ReservedDismissClaim.model_validate(claim.model_dump(), strict=True)
        if type(claim) is SealedDismissClaim:
            return SealedDismissClaim.model_validate(claim.model_dump(), strict=True)
    except Exception as error:
        raise ManagerDispositionIntegrity() from error
    raise ManagerDispositionIntegrity()


@final
class _RegistryTarget(_FrozenDto):
    agent_id: str
    requires_approval: bool


class P17ManagerDispositionApplication:
    """Request-aware FromUnowned를 같은 Request의 종결 또는 재실행으로 수렴한다."""

    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        managers: RequestAwareManagerDispositionStore,
        registry: Registry,
        route_authority: RequestScopedRouteAuthority,
        completion_reader: QuestionCompletionReader,
        deadline_policy: HandlingDeadlinePolicy,
        execution_starter: QuestionExecutionStarter,
        terminal_publisher: QuestionTerminalPublisher,
        generation_factory: IdFactory,
        clock: Clock,
        central_authorizer: CentralAuthorizer | None = None,
    ) -> None:
        self._requests = requests
        self._managers = managers
        self._registry = registry
        self._route_authority = route_authority
        self._completion_reader = completion_reader
        self._deadline_policy = deadline_policy
        self._execution_starter = execution_starter
        self._terminal_publisher = terminal_publisher
        self._generation_factory = generation_factory
        self._clock = clock
        self._central_authorizer = central_authorizer

    def matches_dependencies(
        self,
        *,
        requests: QuestionRequestStore,
        managers: RequestAwareManagerDispositionStore,
        registry: Registry,
        route_authority: RequestScopedRouteAuthority,
        completion_reader: QuestionCompletionReader,
        execution_starter: QuestionExecutionStarter,
        terminal_publisher: QuestionTerminalPublisher,
    ) -> bool:
        return (
            self._requests is requests
            and self._managers is managers
            and self._registry is registry
            and self._route_authority is route_authority
            and self._completion_reader is completion_reader
            and self._execution_starter is execution_starter
            and self._terminal_publisher is terminal_publisher
        )

    def act(self, command: P17ManagerDispositionCommand) -> P17ManagerDispositionResult:
        canonical = canonical_manager_command(command)
        if type(canonical.principal) is AuthenticatedPrincipal and self._central_authorizer is None:
            raise ManagerDispositionNotFoundOrDenied()
        item = self._read_item(canonical.item_id)
        request = self._read_linked_request(item)
        if self._central_authorizer is not None:
            principal = self._canonical_operations_principal(canonical.principal)
            if request.org_id != principal.org_id or item.manager_id != principal.subject_id:
                raise ManagerDispositionNotFoundOrDenied()
            self._authorize_manager_item(principal, "manager.act", item, request)
        stored_claim = self._claim_for_item(item.item_id)
        if stored_claim is not None:
            self._validate_stored_claim_context(stored_claim, item, request)
        self._validate_principal(canonical, item, request)
        self._validate_exact_context(item, request)
        if (
            not isinstance(request.state, AwaitingManager)
            and item.status == "open"
            and stored_claim is None
        ):
            raise ManagerDispositionConflict()

        match canonical:
            case AssignUnownedOwner():
                return self._assign(canonical)
            case DismissUnowned():
                return self._dismiss(canonical)
            case _ as never:
                assert_never(never)

    def pending_for(
        self,
        principal: ManagerOperationsPrincipal,
    ) -> list[ManagerItem]:
        """현재 Manager 귀속 open Item을 중앙 list 권한과 함께 조회한다."""
        canonical = self._canonical_operations_principal(principal)
        if self._central_authorizer is not None:
            self._authorize_governance(
                canonical,
                "manager.list",
                ResourceRef(
                    org_id=canonical.org_id,
                    kind="manager_item_collection",
                    owner_subject_id=canonical.subject_id,
                ),
            )
        try:
            raw_items = self._managers.pending_for_manager(canonical.subject_id)
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if type(raw_items) is not list:
            raise ManagerDispositionIntegrity()
        result: list[ManagerItem] = []
        seen: set[str] = set()
        for raw_item in raw_items:
            if type(raw_item) is not ManagerItem or raw_item.item_id in seen:
                raise ManagerDispositionIntegrity()
            seen.add(raw_item.item_id)
            item = self._read_item(raw_item.item_id)
            if item != raw_item or item.status != "open" or item.manager_id != canonical.subject_id:
                raise ManagerDispositionNotFoundOrDenied()
            request = self._read_linked_request(item)
            if request.org_id != canonical.org_id:
                raise ManagerDispositionNotFoundOrDenied()
            result.append(item)
        return sorted(result, key=lambda item: (item.created_at, item.item_id))

    def _canonical_operations_principal(
        self,
        principal: object,
    ) -> ManagerOperationsPrincipal:
        if self._central_authorizer is None:
            if type(principal) is not ManagerPrincipal:
                raise ManagerDispositionIntegrity()
            try:
                return ManagerPrincipal.model_validate(principal, strict=True)
            except Exception as error:
                raise ManagerDispositionIntegrity() from error
        canonical = canonical_authenticated_principal(principal)
        if canonical is None:
            raise ManagerDispositionNotFoundOrDenied()
        return canonical

    def _authorize_manager_item(
        self,
        principal: ManagerOperationsPrincipal,
        action: Literal["manager.act"],
        item: ManagerItem,
        request: QuestionRequest,
    ) -> None:
        self._authorize_governance(
            principal,
            action,
            ResourceRef(
                org_id=request.org_id,
                kind="manager_item",
                resource_id=item.item_id,
                owner_subject_id=item.manager_id,
            ),
        )

    def _authorize_governance(
        self,
        principal: ManagerOperationsPrincipal,
        action: Literal["manager.list", "manager.act"],
        resource: ResourceRef,
    ) -> None:
        authorizer = self._central_authorizer
        if authorizer is None:
            return
        if type(principal) is not AuthenticatedPrincipal:
            raise ManagerDispositionNotFoundOrDenied()
        outcome = authorize_and_verify(authorizer, principal, action, resource)
        if outcome == "unavailable":
            raise ManagerAuthorizationUnavailable()
        if outcome == "denied":
            raise ManagerDispositionNotFoundOrDenied()

    def _assign(self, command: AssignUnownedOwner) -> UnownedOwnerAssigned:
        def validate(item: ManagerItem) -> ReservedAssignOwnerClaim:
            request = self._read_linked_request(item)
            self._validate_principal(command, item, request)
            intent = self._validate_exact_context(item, request)
            self._require_new_reservation_context(item, request)
            if intent is None:
                raise ManagerDispositionInvalid()
            target = self._validate_registry(intent, command.agent_id)
            return ReservedAssignOwnerClaim(
                generation=self._new_generation(),
                idempotency_key=f"manager-disposition:{item.item_id}",
                request_id=request.request_id,
                item_id=item.item_id,
                org_id=request.org_id,
                by_manager=item.manager_id,
                intent=intent,
                agent_id=target.agent_id,
                requires_approval=target.requires_approval,
                rationale=command.rationale,
            )

        attempt = self._reserve(command, validate)
        control: ReservationControlToken | None = None
        sealed_available: SealedClaimAvailable | None = None
        if isinstance(attempt, ClaimAcquired):
            if not isinstance(attempt.claim, ReservedAssignOwnerClaim):
                raise ManagerDispositionIntegrity()
            claim: ReservedAssignOwnerClaim | SealedAssignOwnerClaim = attempt.claim
            control = attempt.control_token
        elif isinstance(attempt, SealedClaimAvailable):
            if not isinstance(attempt.claim, SealedAssignOwnerClaim):
                raise ManagerDispositionConflict()
            claim = attempt.claim
            sealed_available = attempt
        elif isinstance(attempt, ClaimInProgress):
            raise ManagerDispositionInProgress()
        else:
            raise ManagerDispositionConflict()
        try:
            claim = self._validate_assign_claim(claim, command)
        except ManagerDispositionError as error:
            if control is not None and isinstance(claim, ReservedAssignOwnerClaim):
                self._abandon_claim(claim, control)
                raise
            if isinstance(error, ManagerDispositionInvalid):
                raise ManagerDispositionIntegrity() from error
            raise
        assignment = self._assignment_for(claim)
        try:
            authority_result = self._route_authority.assign_owner(assignment)
        except AuthorityAssignmentConflictError as error:
            if control is not None and isinstance(claim, ReservedAssignOwnerClaim):
                self._seal_claim(claim, control)
            raise ManagerDispositionConflict() from error
        except Exception as error:
            if control is not None and isinstance(claim, ReservedAssignOwnerClaim):
                self._seal_claim(claim, control)
            raise ManagerDispositionDependency() from error

        if isinstance(authority_result, AuthorityAssignmentRejected):
            if (
                authority_result.authority_write_applied is False
                and authority_result.idempotency_write_applied is False
                and control is not None
                and isinstance(claim, ReservedAssignOwnerClaim)
            ):
                self._abandon_claim(claim, control)
                raise ManagerDispositionInvalid()
            raise ManagerDispositionIntegrity()
        if control is not None:
            if not isinstance(claim, ReservedAssignOwnerClaim):
                raise ManagerDispositionIntegrity()
            sealed_available = self._seal_claim(claim, control)
        if sealed_available is None:
            raise ManagerDispositionIntegrity()
        if not isinstance(sealed_available.claim, SealedAssignOwnerClaim):
            raise ManagerDispositionIntegrity()
        sealed = sealed_available.claim
        handle = sealed_available.handle
        receipt = self._canonical_receipt(authority_result)
        if receipt.assignment != assignment:
            raise ManagerDispositionIntegrity()
        try:
            grant = self._route_authority.authorize_for_request(
                sealed.org_id,
                sealed.request_id,
                sealed.intent,
                sealed.agent_id,
            )
        except Exception as error:
            raise ManagerDispositionDependency() from error
        try:
            if type(grant) is not AuthorityGrant:
                raise TypeError("request grant exact type mismatch")
            canonical_grant = AuthorityGrant.model_validate(
                grant.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        if canonical_grant.policy_version != receipt.grant_version:
            raise ManagerDispositionIntegrity()

        self._revalidate_registry(sealed)
        route = RouteTarget(
            intent=sealed.intent,
            agent_id=sealed.agent_id,
            requires_approval=sealed.requires_approval,
            authority_version=receipt.grant_version,
        )
        request = self._advance_assign(sealed, handle, route)
        action = AssignOwner(
            by_manager=sealed.by_manager,
            primary=sealed.agent_id,
            rationale=sealed.rationale,
        )
        self._resolve_item(sealed, handle, action)
        wake = self._wake(request.request_id)
        return UnownedOwnerAssigned(
            request_id=request.request_id,
            item_id=sealed.item_id,
            route=route,
            wake=wake,
        )

    def _dismiss(self, command: DismissUnowned) -> UnownedDismissed:
        def validate(item: ManagerItem) -> ReservedDismissClaim:
            request = self._read_linked_request(item)
            self._validate_principal(command, item, request)
            self._validate_exact_context(item, request)
            self._require_new_reservation_context(item, request)
            return ReservedDismissClaim(
                generation=self._new_generation(),
                idempotency_key=f"manager-disposition:{item.item_id}",
                request_id=request.request_id,
                item_id=item.item_id,
                org_id=request.org_id,
                by_manager=item.manager_id,
                rationale=command.rationale,
            )

        attempt = self._reserve(command, validate)
        if isinstance(attempt, ClaimAcquired):
            if not isinstance(attempt.claim, ReservedDismissClaim):
                raise ManagerDispositionIntegrity()
            sealed_available = self._seal_claim(attempt.claim, attempt.control_token)
        elif isinstance(attempt, SealedClaimAvailable):
            sealed_available = attempt
        elif isinstance(attempt, ClaimInProgress):
            raise ManagerDispositionInProgress()
        else:
            raise ManagerDispositionConflict()
        if not isinstance(sealed_available.claim, SealedDismissClaim):
            raise ManagerDispositionConflict()
        claim = self._validate_dismiss_claim(sealed_available.claim, command)
        request = self._advance_dismiss(claim)
        action = Dismiss(by_manager=claim.by_manager, rationale=claim.rationale)
        self._resolve_item(claim, sealed_available.handle, action)
        delivery = self._publish(request.request_id)
        return UnownedDismissed(
            request_id=request.request_id,
            item_id=claim.item_id,
            delivery=delivery,
        )

    def _reserve(
        self,
        command: P17ManagerDispositionCommand,
        validate: Callable[
            [ManagerItem],
            ReservedAssignOwnerClaim | ReservedDismissClaim,
        ],
    ) -> ClaimAttempt:
        try:
            attempt = self._managers.reserve_validated_action(
                command.item_id,
                command,
                validate,
            )
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        canonical = _canonical_claim_attempt(attempt)
        if isinstance(canonical, (ClaimAcquired, SealedClaimAvailable)):
            stored = self._claim_for_item(command.item_id)
            if stored is None or canonical_manager_claim(stored) != canonical.claim:
                raise ManagerDispositionIntegrity()
        if isinstance(canonical, ClaimAcquired):
            try:
                raw = self._managers.validate_action_reservation(
                    canonical.claim,
                    control_token=canonical.control_token,
                )
            except ManagerDispositionError:
                raise
            except Exception as error:
                raise ManagerDispositionDependency() from error
            if raw is not None:
                raise ManagerDispositionIntegrity()
        return canonical

    def _validate_assign_claim(
        self,
        claim: ReservedAssignOwnerClaim | SealedAssignOwnerClaim,
        command: AssignUnownedOwner,
    ) -> ReservedAssignOwnerClaim | SealedAssignOwnerClaim:
        item = self._read_item(command.item_id)
        request = self._read_linked_request(item)
        self._validate_principal(command, item, request)
        intent = self._validate_exact_context(item, request)
        if intent is None:
            raise ManagerDispositionInvalid()
        target = self._validate_registry(intent, command.agent_id)
        if (
            claim.item_id != item.item_id
            or claim.request_id != request.request_id
            or claim.org_id != request.org_id
            or claim.by_manager != item.manager_id
            or claim.intent != intent
            or claim.agent_id != command.agent_id
            or claim.agent_id != target.agent_id
            or claim.requires_approval != target.requires_approval
            or claim.rationale != command.rationale
            or claim.idempotency_key != f"manager-disposition:{item.item_id}"
        ):
            raise ManagerDispositionIntegrity()
        return claim

    def _validate_dismiss_claim(
        self,
        claim: SealedDismissClaim,
        command: DismissUnowned,
    ) -> SealedDismissClaim:
        item = self._read_item(command.item_id)
        request = self._read_linked_request(item)
        self._validate_principal(command, item, request)
        self._validate_exact_context(item, request)
        if (
            claim.item_id != item.item_id
            or claim.request_id != request.request_id
            or claim.org_id != request.org_id
            or claim.by_manager != item.manager_id
            or claim.rationale != command.rationale
            or claim.reason_code != "manager_declined"
            or claim.idempotency_key != f"manager-disposition:{item.item_id}"
        ):
            raise ManagerDispositionIntegrity()
        return claim

    def _read_item(self, item_id: str) -> ManagerItem:
        try:
            item = self._managers.get(item_id)
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if item is None:
            raise ManagerDispositionNotFound()
        return item

    def _claim_for_item(self, item_id: str) -> ManagerDispositionClaim | None:
        try:
            return self._managers.claim_for_item(item_id)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error

    @staticmethod
    def _validate_stored_claim_context(
        claim: ManagerDispositionClaim,
        item: ManagerItem,
        request: QuestionRequest,
    ) -> None:
        canonical = canonical_manager_claim(claim)
        if not isinstance(item.source, FromUnowned):
            raise ManagerDispositionIntegrity()
        intent = _normalized_intent(item.source.decision.intent)
        common_mismatch = (
            canonical.item_id != item.item_id
            or canonical.request_id != request.request_id
            or item.request_id != request.request_id
            or canonical.org_id != request.org_id
            or canonical.by_manager != item.manager_id
            or item.source.decision.escalated_to != item.manager_id
            or item.source.question != request.question
            or intent != request.intent
            or canonical.idempotency_key != f"manager-disposition:{item.item_id}"
        )
        if common_mismatch:
            raise ManagerDispositionIntegrity()
        if isinstance(canonical, (ReservedAssignOwnerClaim, SealedAssignOwnerClaim)):
            if canonical.intent != intent:
                raise ManagerDispositionIntegrity()
        elif canonical.reason_code != "manager_declined":
            raise ManagerDispositionIntegrity()

    def _seal_claim(
        self,
        claim: ReservedAssignOwnerClaim | ReservedDismissClaim,
        control_token: ReservationControlToken,
    ) -> SealedClaimAvailable:
        try:
            raw = self._managers.seal_claim(claim, control_token=control_token)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        canonical = _canonical_claim_attempt(raw)
        if not isinstance(canonical, SealedClaimAvailable):
            raise ManagerDispositionIntegrity()
        sealed = canonical.claim
        if isinstance(claim, ReservedAssignOwnerClaim):
            if not isinstance(sealed, SealedAssignOwnerClaim) or (
                sealed.generation,
                sealed.idempotency_key,
                sealed.request_id,
                sealed.item_id,
                sealed.org_id,
                sealed.by_manager,
                sealed.intent,
                sealed.agent_id,
                sealed.requires_approval,
                sealed.rationale,
            ) != (
                claim.generation,
                claim.idempotency_key,
                claim.request_id,
                claim.item_id,
                claim.org_id,
                claim.by_manager,
                claim.intent,
                claim.agent_id,
                claim.requires_approval,
                claim.rationale,
            ):
                raise ManagerDispositionIntegrity()
        elif not isinstance(sealed, SealedDismissClaim) or (
            sealed.generation,
            sealed.idempotency_key,
            sealed.request_id,
            sealed.item_id,
            sealed.org_id,
            sealed.by_manager,
            sealed.rationale,
            sealed.reason_code,
        ) != (
            claim.generation,
            claim.idempotency_key,
            claim.request_id,
            claim.item_id,
            claim.org_id,
            claim.by_manager,
            claim.rationale,
            "manager_declined",
        ):
            raise ManagerDispositionIntegrity()
        stored = self._claim_for_item(claim.item_id)
        if stored is None or canonical_manager_claim(stored) != sealed:
            raise ManagerDispositionIntegrity()
        return canonical

    def _abandon_claim(
        self,
        claim: ReservedAssignOwnerClaim,
        control_token: ReservationControlToken,
    ) -> None:
        try:
            raw = self._managers.abandon_unmutated_claim(
                claim,
                control_token=control_token,
            )
            if raw is not None:
                raise ManagerDispositionIntegrity()
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if self._claim_for_item(claim.item_id) is not None:
            raise ManagerDispositionIntegrity()

    def _read_linked_request(self, item: ManagerItem) -> QuestionRequest:
        if item.request_id is None:
            raise ManagerDispositionInvalid()
        try:
            request_id = require_request_id(item.request_id)
            request = self._requests.get(request_id)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if request is None:
            raise ManagerDispositionIntegrity()
        return request

    @staticmethod
    def _validate_principal(
        command: P17ManagerDispositionCommand,
        item: ManagerItem,
        request: QuestionRequest,
    ) -> None:
        if (
            command.principal.org_id != request.org_id
            or command.principal.subject_id != item.manager_id
        ):
            raise ManagerDispositionForbidden()

    @staticmethod
    def _validate_exact_context(
        item: ManagerItem,
        request: QuestionRequest,
    ) -> str | None:
        if not isinstance(item.source, FromUnowned):
            raise ManagerDispositionInvalid()
        if item.request_id != request.request_id:
            raise ManagerDispositionIntegrity()
        source = item.source
        if source.question != request.question or source.decision.escalated_to != item.manager_id:
            raise ManagerDispositionIntegrity()
        intent = _normalized_intent(source.decision.intent)
        if intent != request.intent:
            raise ManagerDispositionIntegrity()
        state = request.state
        if isinstance(state, AwaitingManager):
            if state.public_kind != "unowned" or state.item_id != item.item_id:
                raise ManagerDispositionInvalid()
        if item.status == "open" and item.resolution is not None:
            raise ManagerDispositionIntegrity()
        if item.status == "resolved" and item.resolution is None:
            raise ManagerDispositionIntegrity()
        return intent

    @staticmethod
    def _require_new_reservation_context(
        item: ManagerItem,
        request: QuestionRequest,
    ) -> None:
        state = request.state
        if (
            not isinstance(state, AwaitingManager)
            or state.public_kind != "unowned"
            or state.item_id != item.item_id
            or item.status != "open"
            or item.resolution is not None
        ):
            raise ManagerDispositionConflict()

    def _validate_registry(self, intent: str, agent_id: str) -> _RegistryTarget:
        try:
            with self._registry.consistency_guard():
                card = self._registry.get(agent_id)
                self._registry.get_user(card.owner)
        except KeyError as error:
            raise ManagerDispositionInvalid() from error
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if card.agent_id != agent_id or not domain_authorized(intent, card):
            raise ManagerDispositionInvalid()
        return _RegistryTarget(
            agent_id=card.agent_id,
            requires_approval=intent in card.approval_when,
        )

    def _revalidate_registry(self, claim: SealedAssignOwnerClaim) -> None:
        try:
            current = self._validate_registry(claim.intent, claim.agent_id)
        except ManagerDispositionDependency:
            raise
        except ManagerDispositionInvalid as error:
            raise ManagerDispositionIntegrity() from error
        if (
            current.agent_id != claim.agent_id
            or current.requires_approval != claim.requires_approval
        ):
            raise ManagerDispositionIntegrity()

    @staticmethod
    def _assignment_for(
        claim: ReservedAssignOwnerClaim | SealedAssignOwnerClaim,
    ) -> AuthorityAssignment:
        return AuthorityAssignment(
            org_id=claim.org_id,
            request_id=claim.request_id,
            item_id=claim.item_id,
            intent=claim.intent,
            agent_id=claim.agent_id,
            assigned_by=claim.by_manager,
            idempotency_key=claim.idempotency_key,
        )

    @staticmethod
    def _canonical_receipt(receipt: AuthorityAssignmentReceipt) -> AuthorityAssignmentReceipt:
        try:
            return AuthorityAssignmentReceipt.model_validate(
                receipt.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error

    def _advance_assign(
        self,
        claim: SealedAssignOwnerClaim,
        handle: SealedClaimHandle,
        route: RouteTarget,
    ) -> QuestionRequest:
        current = self._current_request_for_claim(claim)
        if self._same_assign_winner(current, route, claim.item_id):
            if isinstance(current.state, ReadyToDispatch):
                self._ensure_ready_resume_evidence(current, handle, route)
            self._validate_terminal_or_resume_evidence(current, handle, route)
            return current
        if not isinstance(current.state, AwaitingManager):
            raise ManagerDispositionConflict()
        self._validate_exact_context(self._read_item(claim.item_id), current)
        try:
            self._revalidate_registry(claim)
            transition_time = self._transition_time()
            due_at = self._deadline_policy.deadline_for(
                current.org_id,
                "ready_to_dispatch",
                transition_time,
            )
            trigger_key = f"request-dispatch:{current.request_id}:1"
            updated = current.transition(
                ReadyToDispatch(
                    route=route,
                    attempt=1,
                    trigger_key=trigger_key,
                    handling=HandlingAssignment(
                        kind="system",
                        ref=trigger_key,
                        due_at=due_at,
                    ),
                ),
                clock=lambda: transition_time,
            )
            # 각 검증 호출만 짧은 Registry snapshot이다. deadline·Request CAS 중에는
            # Registry lock을 보유하지 않아 UoW→Registry 경로와 lock 순서가 역전되지 않는다.
            self._revalidate_registry(claim)
            won = self._requests.compare_and_set(
                current.request_id,
                current.revision,
                current,
                updated,
            )
        except ManagerDispositionError:
            raise
        except RegistryError as error:
            raise ManagerDispositionIntegrity() from error
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if won:
            committed = self._current_request_for_claim(claim)
            if committed != updated:
                raise ManagerDispositionIntegrity()
            self._revalidate_registry(claim)
            evidence = ResumeEvidence(
                request_id=current.request_id,
                from_revision=current.revision,
                to_revision=updated.revision,
                route=route,
                trigger_key=trigger_key,
            )
            self._record_resume_evidence(handle, evidence)
            return committed
        latest = self._current_request_for_claim(claim)
        if self._same_assign_winner(latest, route, claim.item_id):
            if isinstance(latest.state, ReadyToDispatch):
                self._ensure_ready_resume_evidence(latest, handle, route)
            self._validate_terminal_or_resume_evidence(latest, handle, route)
            return latest
        raise ManagerDispositionConflict()

    def _advance_dismiss(self, claim: SealedDismissClaim) -> QuestionRequest:
        current = self._current_request_for_claim(claim)
        if isinstance(current.state, DeclinedRequest):
            if current.state.reason_code == "manager_declined":
                return current
            raise ManagerDispositionConflict()
        if not isinstance(current.state, AwaitingManager):
            raise ManagerDispositionConflict()
        self._validate_exact_context(self._read_item(claim.item_id), current)
        try:
            updated = current.transition(
                DeclinedRequest(reason_code="manager_declined"),
                clock=lambda: self._transition_time(),
            )
            won = self._requests.compare_and_set(
                current.request_id,
                current.revision,
                current,
                updated,
            )
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if won:
            committed = self._current_request_for_claim(claim)
            if committed != updated:
                raise ManagerDispositionIntegrity()
            return committed
        latest = self._current_request_for_claim(claim)
        if isinstance(latest.state, DeclinedRequest):
            if latest.state.reason_code == "manager_declined":
                return latest
        raise ManagerDispositionConflict()

    def _current_request_for_claim(
        self,
        claim: ManagerDispositionClaim,
    ) -> QuestionRequest:
        try:
            current = self._requests.get(claim.request_id)
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if current is None:
            raise ManagerDispositionIntegrity()
        if current.org_id != claim.org_id or current.request_id != claim.request_id:
            raise ManagerDispositionIntegrity()
        return current

    @staticmethod
    def _same_assign_winner(
        request: QuestionRequest,
        route: RouteTarget,
        item_id: str,
    ) -> bool:
        state = request.state
        if isinstance(state, ReadyToDispatch):
            return (
                state.route == route
                and state.attempt == 1
                and state.trigger_key == f"request-dispatch:{request.request_id}:1"
                and state.handling.kind == "system"
                and state.handling.ref == state.trigger_key
                and state.handling.due_at >= request.updated_at
            )
        if isinstance(state, (AwaitingAnswer, AwaitingApproval)):
            return state.route == route and state.attempt == 1
        if isinstance(state, (AnsweredRequest, FailedRequest)):
            del item_id
            return True
        return False

    def _record_resume_evidence(
        self,
        handle: SealedClaimHandle,
        evidence: ResumeEvidence,
    ) -> None:
        try:
            raw = self._managers.record_resume_evidence(handle, evidence)
            if raw is not None:
                raise ManagerDispositionIntegrity()
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error

    def _ensure_ready_resume_evidence(
        self,
        request: QuestionRequest,
        handle: SealedClaimHandle,
        route: RouteTarget,
    ) -> None:
        try:
            existing = self._managers.resume_evidence_for_claim(handle)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if existing is not None:
            return
        if not isinstance(request.state, ReadyToDispatch) or request.revision != 2:
            raise ManagerDispositionIntegrity()
        self._record_resume_evidence(
            handle,
            ResumeEvidence(
                request_id=request.request_id,
                from_revision=request.revision - 1,
                to_revision=request.revision,
                route=route,
                trigger_key=request.state.trigger_key,
            ),
        )

    def _validate_terminal_or_resume_evidence(
        self,
        request: QuestionRequest,
        handle: SealedClaimHandle,
        route: RouteTarget,
    ) -> None:
        try:
            evidence = self._managers.resume_evidence_for_claim(handle)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if (
            evidence is None
            or evidence.request_id != request.request_id
            or evidence.route != route
            or evidence.attempt != 1
            or evidence.from_revision != 1
            or evidence.to_revision != 2
            or evidence.trigger_key != f"request-dispatch:{request.request_id}:1"
        ):
            raise ManagerDispositionIntegrity()
        if isinstance(request.state, AnsweredRequest):
            try:
                bundle = self._completion_reader.by_request(request.request_id)
                canonical = None if bundle is None else canonical_completion_bundle(bundle)
            except Exception as error:
                raise ManagerDispositionIntegrity() from error
            if (
                canonical is None
                or canonical.request != request
                or canonical.terminal_audit.record_id != request.state.record_id
                or canonical.terminal_audit.route != route
                or canonical.terminal_audit.attempt != 1
            ):
                raise ManagerDispositionIntegrity()

    def _resolve_item(
        self,
        claim: ManagerDispositionClaim,
        handle: SealedClaimHandle,
        action: AssignOwner | Dismiss,
    ) -> ManagerItem:
        item = self._read_item(claim.item_id)
        if item.request_id != claim.request_id:
            raise ManagerDispositionIntegrity()
        resolved = item.resolve(ManagerResolution(action=action))
        try:
            stored = self._managers.resolve_for_claim(handle, resolved)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if stored != resolved or self._read_item(claim.item_id) != resolved:
            raise ManagerDispositionIntegrity()
        return stored

    def _wake(self, request_id: str) -> ExecutionWake:
        try:
            wake = self._execution_starter.ensure_started(request_id)
            return _canonical_execution_wake(wake)
        except ManagerDispositionError:
            raise
        except Exception:
            return ExecutionDeferred(reason_code="submission_failed")

    def _publish(self, request_id: str) -> TerminalDelivery:
        try:
            delivery = self._terminal_publisher.publish_terminal(request_id)
            return _canonical_terminal_delivery(delivery)
        except ManagerDispositionError:
            raise
        except Exception:
            return TerminalDeferred(reason_code="publish_failed")

    def _transition_time(self) -> datetime:
        try:
            return self._clock()
        except Exception as error:
            raise ManagerDispositionDependency() from error

    def _new_generation(self) -> str:
        try:
            generation = self._generation_factory()
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if type(generation) is not str or not generation.strip():
            raise ManagerDispositionIntegrity()
        return generation


def _canonical_execution_wake(wake: ExecutionWake) -> ExecutionWake:
    try:
        if type(wake) is ExecutionStarted:
            return ExecutionStarted.model_validate(wake.model_dump(), strict=True)
        if type(wake) is ExecutionAlreadyRunning:
            return ExecutionAlreadyRunning.model_validate(wake.model_dump(), strict=True)
        if type(wake) is ExecutionNotNeeded:
            return ExecutionNotNeeded.model_validate(wake.model_dump(), strict=True)
        if type(wake) is ExecutionDeferred:
            return ExecutionDeferred.model_validate(wake.model_dump(), strict=True)
    except Exception as error:
        raise ManagerDispositionIntegrity() from error
    raise ManagerDispositionIntegrity()


def _canonical_terminal_delivery(delivery: TerminalDelivery) -> TerminalDelivery:
    try:
        if type(delivery) is TerminalPublished:
            return TerminalPublished.model_validate(delivery.model_dump(), strict=True)
        if type(delivery) is TerminalAlreadyPublished:
            return TerminalAlreadyPublished.model_validate(delivery.model_dump(), strict=True)
        if type(delivery) is TerminalDeferred:
            return TerminalDeferred.model_validate(delivery.model_dump(), strict=True)
    except Exception as error:
        raise ManagerDispositionIntegrity() from error
    raise ManagerDispositionIntegrity()
