"""Request-aware Deadlock Manager 처분 애플리케이션(ADR 0046 S3-E).

원 Conflict claim과 Manager claim을 full handle로 묶은 mediation proof가 저장된 뒤에만
같은 Question Request를 재개하거나 명시적으로 거절한다. 이 경계는 Router와 전역 학습,
실행 Runtime을 모르며, 단일 프로세스 InMemory control recovery만 보장한다.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from threading import RLock
from typing import TypeAlias, assert_never

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
from agent_org_network.conflict import ConflictCase, Resolution
from agent_org_network.manager_queue import (
    AssignOwner,
    Dismiss,
    FromDeadlock,
    ManagerItem,
    ManagerResolution,
)
from agent_org_network.governance_authorization import (
    authorize_and_verify,
    canonical_authenticated_principal,
)
from agent_org_network.p17_conflict_disposition import (
    ConflictDispositionConflict,
    ConflictDispositionDependency,
    ConflictDispositionError,
    ConflictDispositionIntegrity,
    ConflictMediationHandle,
    ConflictResolutionEvidence,
    FromManagerMediation,
    RequestAwareConflictMediationStore,
    SealedConflictClaimAvailable,
    SealedConflictMediationAvailable,
    SealedDeadlockClaim,
    ValidatedMediationAssign,
    ValidatedMediationDismiss,
)
from agent_org_network.p17_manager_disposition import (
    AssignDeadlockedOwner,
    DeadlockDismissed,
    DeadlockManagerClaimAcquired,
    DeadlockManagerClaimAttempt,
    DeadlockManagerClaimConflict,
    DeadlockManagerClaimInProgress,
    DeadlockManagerDispositionCommand,
    DeadlockManagerReservationControlToken,
    DeadlockManagerSealedClaimAvailable,
    DeadlockManagerSealedClaimHandle,
    DeadlockOwnerAssigned,
    DismissDeadlocked,
    ExecutionAlreadyRunning,
    ExecutionDeferred,
    ExecutionNotNeeded,
    ExecutionStarted,
    ExecutionWake,
    ManagerDispositionConflict,
    ManagerDispositionDependency,
    ManagerDispositionError,
    ManagerDispositionForbidden,
    ManagerDispositionInProgress,
    ManagerDispositionIntegrity,
    ManagerAuthorizationUnavailable,
    ManagerDispositionNotFoundOrDenied,
    ManagerOperationsPrincipal,
    ManagerDispositionInvalid,
    ManagerDispositionNotFound,
    P17DeadlockManagerDispositionResult,
    QuestionExecutionStarter,
    QuestionTerminalPublisher,
    RequestAwareDeadlockManagerDispositionStore,
    ReservedDeadlockAssignClaim,
    ReservedDeadlockDismissClaim,
    ResumeEvidence,
    SealedDeadlockAssignClaim,
    SealedDeadlockDismissClaim,
    TerminalAlreadyPublished,
    TerminalDeferred,
    TerminalDelivery,
    TerminalPublished,
    canonical_deadlock_manager_claim,
    canonical_deadlock_manager_command,
    deadlock_manager_claim_matches_command,
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
    RequestLockPool,
)
from agent_org_network.registry import Registry, RegistryError
from agent_org_network.request_route_authority import (
    FromDeadlockManagerGrant,
    RequestRouteAuthority,
    RequestRouteGrantAssignment,
    RequestRouteGrantConflict,
    RequestRouteGrantReceipt,
    RequestRouteGrantRejected,
    RequestRouteGrantResult,
)


Clock: TypeAlias = Callable[[], datetime]
IdFactory: TypeAlias = Callable[[], object]


class _Context:
    def __init__(
        self,
        *,
        item: ManagerItem,
        request: QuestionRequest,
        case: ConflictCase,
        conflict: SealedConflictClaimAvailable,
    ) -> None:
        if not isinstance(conflict.claim, SealedDeadlockClaim):
            raise ManagerDispositionIntegrity()
        self.item = deepcopy(item)
        self.request = request.model_copy(deep=True)
        self.case = deepcopy(case)
        self.conflict = conflict.model_copy(deep=True)
        self.conflict_claim: SealedDeadlockClaim = deepcopy(conflict.claim)


class _RegistryTarget:
    def __init__(self, *, agent_id: str, owner_id: str, requires_approval: bool) -> None:
        self.agent_id = agent_id
        self.owner_id = owner_id
        self.requires_approval = requires_approval

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is _RegistryTarget
            and self.agent_id == other.agent_id
            and self.owner_id == other.owner_id
            and self.requires_approval == other.requires_approval
        )


class _ReadyPlan:
    def __init__(self, *, transitioned_at: datetime, due_at: datetime) -> None:
        self.transitioned_at = transitioned_at
        self.due_at = due_at


class P17DeadlockManagerDispositionApplication:
    """Escalated request-aware ConflictCase를 Assign 또는 Dismiss로 정확히 종결한다."""

    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        conflicts: RequestAwareConflictMediationStore,
        managers: RequestAwareDeadlockManagerDispositionStore,
        registry: Registry,
        route_authority: RequestRouteAuthority,
        completion_reader: QuestionCompletionReader,
        deadline_policy: HandlingDeadlinePolicy,
        execution_starter: QuestionExecutionStarter,
        terminal_publisher: QuestionTerminalPublisher,
        generation_factory: IdFactory,
        clock: Clock,
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
        self._terminal_publisher = terminal_publisher
        self._generation_factory = generation_factory
        self._clock = clock
        self._request_locks = request_locks or RequestLockPool()
        self._central_authorizer = central_authorizer
        self._recovery_lock = RLock()
        self._reserved_recoveries: dict[
            str,
            tuple[
                ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
                DeadlockManagerReservationControlToken,
            ],
        ] = {}

    @property
    def request_lock_count(self) -> int:
        """테스트·조립 검증용 bounded lock 관찰 손잡이."""

        return self._request_locks.active_count

    def matches_dependencies(
        self,
        *,
        requests: QuestionRequestStore,
        conflicts: RequestAwareConflictMediationStore,
        managers: RequestAwareDeadlockManagerDispositionStore,
        registry: Registry,
        route_authority: RequestRouteAuthority,
        completion_reader: QuestionCompletionReader,
        execution_starter: QuestionExecutionStarter,
        terminal_publisher: QuestionTerminalPublisher,
    ) -> bool:
        return (
            self._requests is requests
            and self._conflicts is conflicts
            and self._managers is managers
            and self._registry is registry
            and self._route_authority is route_authority
            and self._completion_reader is completion_reader
            and self._execution_starter is execution_starter
            and self._terminal_publisher is terminal_publisher
        )

    def act(
        self,
        command: DeadlockManagerDispositionCommand,
    ) -> P17DeadlockManagerDispositionResult:
        canonical = canonical_deadlock_manager_command(command)
        if type(canonical.principal) is AuthenticatedPrincipal and self._central_authorizer is None:
            raise ManagerDispositionNotFoundOrDenied()
        initial = self._load_context(canonical)
        with self._request_locks.hold(initial.request.request_id):
            context = self._load_context(canonical)
            if self._central_authorizer is not None:
                principal = self._canonical_operations_principal(canonical.principal)
                if (
                    context.request.org_id != principal.org_id
                    or context.item.manager_id != principal.subject_id
                ):
                    raise ManagerDispositionNotFoundOrDenied()
                self._authorize_manager_item(principal, context.item, context.request)
            match canonical:
                case AssignDeadlockedOwner():
                    return self._assign(canonical, context)
                case DismissDeadlocked():
                    return self._dismiss(canonical, context)
                case _ as never:
                    assert_never(never)

    def _canonical_operations_principal(
        self,
        principal: object,
    ) -> ManagerOperationsPrincipal:
        canonical = canonical_authenticated_principal(principal)
        if canonical is None:
            raise ManagerDispositionNotFoundOrDenied()
        return canonical

    def _authorize_manager_item(
        self,
        principal: ManagerOperationsPrincipal,
        item: ManagerItem,
        request: QuestionRequest,
    ) -> None:
        authorizer = self._central_authorizer
        if authorizer is None:
            return
        if type(principal) is not AuthenticatedPrincipal:
            raise ManagerDispositionNotFoundOrDenied()
        resource = ResourceRef(
            org_id=request.org_id,
            kind="manager_item",
            resource_id=item.item_id,
            owner_subject_id=item.manager_id,
        )
        outcome = authorize_and_verify(authorizer, principal, "manager.act", resource)
        if outcome == "unavailable":
            raise ManagerAuthorizationUnavailable()
        if outcome == "denied":
            raise ManagerDispositionNotFoundOrDenied()

    def _assign(
        self,
        command: AssignDeadlockedOwner,
        context: _Context,
    ) -> DeadlockOwnerAssigned:
        target = self._validate_registry_target(
            context.conflict_claim,
            command.agent_id,
        )
        ready_plan = self._prepare_ready_plan(context.request)

        def validate(item: ManagerItem) -> ReservedDeadlockAssignClaim:
            request = self._read_request(context.request.request_id)
            self._validate_item_context(item, request, context.case, context.conflict_claim)
            self._require_new_reservation(item, request, context.case)
            callback_target = self._validate_registry_target(
                context.conflict_claim,
                command.agent_id,
            )
            if callback_target != target:
                raise ManagerDispositionIntegrity()
            return ReservedDeadlockAssignClaim(
                generation=self._new_generation(),
                idempotency_key=f"manager-disposition:{item.item_id}",
                request_id=request.request_id,
                case_id=context.case.case_id,
                item_id=item.item_id,
                org_id=request.org_id,
                by_manager=item.manager_id,
                intent=context.case.intent,
                round=context.case.concurrence_round,
                cause=context.conflict_claim.cause,
                agent_id=target.agent_id,
                requires_approval=target.requires_approval,
                rationale=command.rationale,
            )

        attempt = self._reserve(command, validate)
        reserved, control, available = self._assign_attempt(command, attempt)
        if reserved is not None:
            if control is None:
                raise ManagerDispositionIntegrity()
            self._validate_reservation(reserved, control)
            claim: ReservedDeadlockAssignClaim | SealedDeadlockAssignClaim = reserved
        else:
            claim, _unused_handle = self._require_assign_available(available)
        try:
            self._validate_assign_claim(claim, command, self._load_context(command))
        except ManagerDispositionError:
            if reserved is not None and control is not None:
                self._seal_preserving(reserved, control, command)
            raise

        assignment = self._authority_assignment(claim)
        try:
            raw_result = self._route_authority.grant_for_request(assignment)
        except Exception as error:
            if reserved is not None and control is not None:
                self._seal_preserving(reserved, control, command)
            raise ManagerDispositionDependency() from error
        try:
            grant_result = self._canonical_grant_result(raw_result)
        except ManagerDispositionError:
            if reserved is not None and control is not None:
                self._seal_preserving(reserved, control, command)
            raise

        if isinstance(grant_result, RequestRouteGrantRejected):
            if grant_result.idempotency_key != claim.idempotency_key:
                if reserved is not None and control is not None:
                    self._seal_preserving(reserved, control, command)
                raise ManagerDispositionIntegrity()
            if reserved is None or control is None:
                raise ManagerDispositionIntegrity()
            self._abandon_rejected(reserved, control, grant_result)
            raise ManagerDispositionInvalid()
        if isinstance(grant_result, RequestRouteGrantConflict):
            if reserved is not None and control is not None:
                self._seal_preserving(reserved, control, command)
            raise ManagerDispositionConflict()
        if grant_result.assignment != assignment:
            if reserved is not None and control is not None:
                self._seal_preserving(reserved, control, command)
            raise ManagerDispositionIntegrity()

        if reserved is not None:
            assert control is not None
            available = self._seal(reserved, control, command)
        sealed, handle = self._require_assign_available(available)
        if sealed.generation != handle.generation:
            raise ManagerDispositionIntegrity()

        grant = self._read_grant(sealed, grant_result)
        route = RouteTarget(
            intent=sealed.intent,
            agent_id=sealed.agent_id,
            requires_approval=sealed.requires_approval,
            authority_version=grant.policy_version,
        )
        self._revalidate_registry(sealed)
        mediation = self._record_assign_mediation(
            context=self._load_context(command),
            manager_claim=sealed,
            manager_handle=handle,
            route=route,
        )
        request = self._advance_assign(sealed, route, ready_plan=ready_plan)
        self._revalidate_registry(sealed)
        self._ensure_resume_evidence(handle, request, route)
        case = self._resolve_case(mediation, sealed)
        self._resolve_assign_item(handle, sealed, case)
        wake = self._wake(request.request_id)
        return DeadlockOwnerAssigned(
            request_id=sealed.request_id,
            case_id=sealed.case_id,
            item_id=sealed.item_id,
            route=route,
            wake=wake,
        )

    def _dismiss(
        self,
        command: DismissDeadlocked,
        context: _Context,
    ) -> DeadlockDismissed:
        transitioned_at = self._prepare_dismiss_time(context.request)

        def validate(item: ManagerItem) -> ReservedDeadlockDismissClaim:
            request = self._read_request(context.request.request_id)
            self._validate_item_context(item, request, context.case, context.conflict_claim)
            self._require_new_reservation(item, request, context.case)
            return ReservedDeadlockDismissClaim(
                generation=self._new_generation(),
                idempotency_key=f"manager-disposition:{item.item_id}",
                request_id=request.request_id,
                case_id=context.case.case_id,
                item_id=item.item_id,
                org_id=request.org_id,
                by_manager=item.manager_id,
                intent=context.case.intent,
                round=context.case.concurrence_round,
                cause=context.conflict_claim.cause,
                rationale=command.rationale,
            )

        attempt = self._reserve(command, validate)
        if isinstance(attempt, DeadlockManagerClaimAcquired):
            if not isinstance(attempt.claim, ReservedDeadlockDismissClaim):
                raise ManagerDispositionIntegrity()
            self._validate_reservation(attempt.claim, attempt.control_token)
            available = self._seal(attempt.claim, attempt.control_token, command)
        elif isinstance(attempt, DeadlockManagerSealedClaimAvailable):
            available = attempt
        elif isinstance(attempt, DeadlockManagerClaimInProgress):
            recovery = self._reserved_recovery(command)
            if recovery is None:
                raise ManagerDispositionInProgress()
            claim, control = recovery
            if not isinstance(claim, ReservedDeadlockDismissClaim):
                raise ManagerDispositionConflict()
            self._validate_reservation(claim, control)
            available = self._seal(claim, control, command)
        else:
            raise ManagerDispositionConflict()
        if not isinstance(available.claim, SealedDeadlockDismissClaim):
            raise ManagerDispositionConflict()
        sealed = available.claim
        self._validate_dismiss_claim(sealed, command, self._load_context(command))
        mediation = self._record_dismiss_mediation(
            context=self._load_context(command),
            manager_claim=sealed,
            manager_handle=available.handle,
        )
        request = self._advance_dismiss(sealed, transitioned_at=transitioned_at)
        case = self._decline_case(mediation, sealed)
        self._resolve_dismiss_item(available.handle, sealed, case)
        delivery = self._publish(request.request_id)
        return DeadlockDismissed(
            request_id=sealed.request_id,
            case_id=sealed.case_id,
            item_id=sealed.item_id,
            delivery=delivery,
        )

    def _load_context(self, command: DeadlockManagerDispositionCommand) -> _Context:
        item = self._read_item(command.item_id)
        if item.request_id is None or not isinstance(item.source, FromDeadlock):
            raise ManagerDispositionInvalid()
        request = self._read_request(item.request_id)
        case = self._read_case(item.source.case.case_id)
        conflict = self._read_deadlock_claim(case.case_id)
        if not isinstance(conflict.claim, SealedDeadlockClaim):
            raise ManagerDispositionIntegrity()
        self._validate_principal(command, item, request)
        self._validate_item_context(item, request, case, conflict.claim)
        stored_claim = self._read_manager_claim(item.item_id)
        if stored_claim is not None:
            if not deadlock_manager_claim_matches_command(stored_claim, command):
                raise ManagerDispositionConflict()
            self._validate_stored_claim(stored_claim, item, request, case, conflict.claim)
        elif item.status != "open" or case.status != "escalated":
            raise ManagerDispositionIntegrity()
        return _Context(item=item, request=request, case=case, conflict=conflict)

    def _read_item(self, item_id: str) -> ManagerItem:
        try:
            raw = self._managers.get(item_id)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if raw is None:
            raise ManagerDispositionNotFound()
        if type(raw) is not ManagerItem or raw.item_id != item_id:
            raise ManagerDispositionIntegrity()
        return deepcopy(raw)

    def _read_request(self, request_id: str) -> QuestionRequest:
        try:
            raw = self._requests.get(request_id)
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if raw is None:
            raise ManagerDispositionIntegrity()
        try:
            if type(raw) is not QuestionRequest:
                raise TypeError("QuestionRequest exact type이 필요합니다.")
            request = QuestionRequest.model_validate(
                raw.model_dump(mode="python", round_trip=True), strict=True
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        if request.request_id != request_id:
            raise ManagerDispositionIntegrity()
        return request

    def _read_case(self, case_id: str) -> ConflictCase:
        try:
            raw = self._conflicts.get_request_case(case_id)
        except ConflictDispositionError as error:
            raise self._manager_error(error) from error
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if raw is None:
            raise ManagerDispositionIntegrity()
        if type(raw) is not ConflictCase or raw.case_id != case_id:
            raise ManagerDispositionIntegrity()
        return deepcopy(raw)

    def _read_deadlock_claim(self, case_id: str) -> SealedConflictClaimAvailable:
        try:
            raw = self._conflicts.sealed_claim_for_case(case_id)
        except ConflictDispositionError as error:
            raise self._manager_error(error) from error
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if raw is None:
            raise ManagerDispositionIntegrity()
        try:
            if type(raw) is not SealedConflictClaimAvailable:
                raise TypeError("SealedConflictClaimAvailable exact type이 필요합니다.")
            available = SealedConflictClaimAvailable.model_validate(raw, strict=True).model_copy(
                deep=True
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        if (
            not isinstance(available.claim, SealedDeadlockClaim)
            or available.claim.case_id != case_id
            or available.claim.generation != available.handle.generation
        ):
            raise ManagerDispositionIntegrity()
        try:
            validation = self._conflicts.validate_sealed_claim(
                available.claim,
                handle=available.handle,
            )
        except ConflictDispositionError as error:
            raise self._manager_error(error) from error
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if validation is not None:
            raise ManagerDispositionIntegrity()
        return available

    def _read_manager_claim(
        self,
        item_id: str,
    ) -> (
        ReservedDeadlockAssignClaim
        | ReservedDeadlockDismissClaim
        | SealedDeadlockAssignClaim
        | SealedDeadlockDismissClaim
        | None
    ):
        try:
            raw = self._managers.deadlock_claim_for_item(item_id)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if raw is None:
            return None
        return canonical_deadlock_manager_claim(raw)

    @staticmethod
    def _validate_principal(
        command: DeadlockManagerDispositionCommand,
        item: ManagerItem,
        request: QuestionRequest,
    ) -> None:
        if (
            command.principal.org_id != request.org_id
            or command.principal.subject_id != item.manager_id
        ):
            raise ManagerDispositionForbidden()

    @staticmethod
    def _validate_item_context(
        item: ManagerItem,
        request: QuestionRequest,
        case: ConflictCase,
        conflict: SealedDeadlockClaim,
    ) -> None:
        source = item.source
        if not isinstance(source, FromDeadlock) or source.cause is None:
            raise ManagerDispositionInvalid()
        original = source.case
        static_case_mismatch = (
            original.request_id != request.request_id
            or original.case_id != case.case_id
            or original.intent != case.intent
            or original.question != case.question
            or original.candidates != case.candidates
            or original.concurrence_round != case.concurrence_round
            or original.status != "open"
            or original.resolution is not None
            or original.manager_item_id is not None
            or original.decline_reason is not None
        )
        if (
            item.request_id != request.request_id
            or item.manager_id == ""
            or source.reason
            != (
                "divergent_votes"
                if source.cause.kind == "divergent_votes"
                else f"candidate_registry_changed:{source.cause.reason_code}"
            )
            or static_case_mismatch
            or request.question != original.question
            or request.intent != original.intent
            or request.initial_disposition != "contested"
            or conflict.request_id != request.request_id
            or conflict.case_id != case.case_id
            or conflict.org_id != request.org_id
            or conflict.intent != case.intent
            or conflict.round != case.concurrence_round
            or conflict.candidate_snapshot != case.candidates
            or conflict.cause != source.cause
            or conflict.cause.round != case.concurrence_round
        ):
            raise ManagerDispositionIntegrity()
        if case.status in ("escalated", "resolved", "declined"):
            if case.manager_item_id != item.item_id:
                raise ManagerDispositionIntegrity()
        else:
            raise ManagerDispositionIntegrity()
        if item.status == "open" and item.resolution is not None:
            raise ManagerDispositionIntegrity()
        if item.status == "resolved" and item.resolution is None:
            raise ManagerDispositionIntegrity()

    @staticmethod
    def _require_new_reservation(
        item: ManagerItem,
        request: QuestionRequest,
        case: ConflictCase,
    ) -> None:
        if (
            item.status != "open"
            or item.resolution is not None
            or case.status != "escalated"
            or case.manager_item_id != item.item_id
            or not isinstance(request.state, AwaitingManager)
            or request.revision != 2
            or request.state.public_kind != "contested"
            or request.state.item_id != item.item_id
            or request.state.route is not None
            or request.state.attempt is not None
        ):
            raise ManagerDispositionConflict()

    @staticmethod
    def _validate_stored_claim(
        claim: (
            ReservedDeadlockAssignClaim
            | ReservedDeadlockDismissClaim
            | SealedDeadlockAssignClaim
            | SealedDeadlockDismissClaim
        ),
        item: ManagerItem,
        request: QuestionRequest,
        case: ConflictCase,
        conflict: SealedDeadlockClaim,
    ) -> None:
        if (
            claim.item_id != item.item_id
            or claim.request_id != request.request_id
            or claim.case_id != case.case_id
            or claim.org_id != request.org_id
            or claim.by_manager != item.manager_id
            or claim.intent != case.intent
            or claim.round != case.concurrence_round
            or claim.cause != conflict.cause
            or claim.idempotency_key != f"manager-disposition:{item.item_id}"
        ):
            raise ManagerDispositionIntegrity()

    def _validate_registry_target(
        self,
        conflict: SealedDeadlockClaim,
        agent_id: str,
    ) -> _RegistryTarget:
        candidate = next(
            (
                candidate
                for candidate in conflict.candidate_snapshot
                if candidate.agent_id == agent_id
            ),
            None,
        )
        if candidate is None:
            raise ManagerDispositionInvalid()
        try:
            with self._registry.consistency_guard():
                card = self._registry.get(agent_id)
                owner = self._registry.get_user(card.owner)
        except KeyError as error:
            raise ManagerDispositionInvalid() from error
        except RegistryError as error:
            raise ManagerDispositionIntegrity() from error
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if (
            card.agent_id != agent_id
            or card.owner != candidate.owner
            or owner.id != candidate.owner
            or not domain_authorized(conflict.intent, card)
        ):
            raise ManagerDispositionInvalid()
        return _RegistryTarget(
            agent_id=card.agent_id,
            owner_id=owner.id,
            requires_approval=conflict.intent in card.approval_when,
        )

    def _revalidate_registry(self, claim: SealedDeadlockAssignClaim) -> None:
        try:
            available = self._read_deadlock_claim(claim.case_id)
            if not isinstance(available.claim, SealedDeadlockClaim):
                raise ManagerDispositionIntegrity()
            target = self._validate_registry_target(
                available.claim,
                claim.agent_id,
            )
        except ManagerDispositionInvalid as error:
            raise ManagerDispositionIntegrity() from error
        if target.agent_id != claim.agent_id or target.requires_approval != claim.requires_approval:
            raise ManagerDispositionIntegrity()

    def _reserve(
        self,
        command: DeadlockManagerDispositionCommand,
        validate: Callable[
            [ManagerItem],
            ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        ],
    ) -> DeadlockManagerClaimAttempt:
        try:
            raw = self._managers.reserve_validated_deadlock_action(
                command.item_id,
                command,
                validate=validate,
            )
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        attempt = self._canonical_attempt(raw)
        if isinstance(
            attempt,
            (DeadlockManagerClaimAcquired, DeadlockManagerSealedClaimAvailable),
        ):
            stored = self._read_manager_claim(command.item_id)
            if stored != attempt.claim:
                raise ManagerDispositionIntegrity()
        if isinstance(attempt, DeadlockManagerClaimAcquired):
            self._validate_reservation(attempt.claim, attempt.control_token)
            self._remember_reserved(attempt.claim, attempt.control_token)
        return attempt

    def _validate_reservation(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        control: DeadlockManagerReservationControlToken,
    ) -> None:
        try:
            raw = self._managers.validate_deadlock_action_reservation(
                claim,
                control_token=control,
            )
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if raw is not None:
            raise ManagerDispositionIntegrity()

    @staticmethod
    def _canonical_attempt(raw: DeadlockManagerClaimAttempt) -> DeadlockManagerClaimAttempt:
        try:
            if type(raw) is DeadlockManagerClaimAcquired:
                return DeadlockManagerClaimAcquired.model_validate(
                    raw.model_dump(mode="python", round_trip=True), strict=True
                )
            if type(raw) is DeadlockManagerClaimInProgress:
                return DeadlockManagerClaimInProgress.model_validate(
                    raw.model_dump(mode="python", round_trip=True), strict=True
                )
            if type(raw) is DeadlockManagerSealedClaimAvailable:
                return DeadlockManagerSealedClaimAvailable.model_validate(
                    raw.model_dump(mode="python", round_trip=True), strict=True
                )
            if type(raw) is DeadlockManagerClaimConflict:
                return DeadlockManagerClaimConflict.model_validate(
                    raw.model_dump(mode="python", round_trip=True), strict=True
                )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        raise ManagerDispositionIntegrity()

    def _assign_attempt(
        self,
        command: AssignDeadlockedOwner,
        attempt: DeadlockManagerClaimAttempt,
    ) -> tuple[
        ReservedDeadlockAssignClaim | None,
        DeadlockManagerReservationControlToken | None,
        DeadlockManagerSealedClaimAvailable | None,
    ]:
        if isinstance(attempt, DeadlockManagerClaimAcquired):
            if not isinstance(attempt.claim, ReservedDeadlockAssignClaim):
                raise ManagerDispositionIntegrity()
            return attempt.claim, attempt.control_token, None
        if isinstance(attempt, DeadlockManagerSealedClaimAvailable):
            if not isinstance(attempt.claim, SealedDeadlockAssignClaim):
                raise ManagerDispositionConflict()
            return None, None, attempt
        if isinstance(attempt, DeadlockManagerClaimInProgress):
            recovery = self._reserved_recovery(command)
            if recovery is None:
                raise ManagerDispositionInProgress()
            claim, control = recovery
            if not isinstance(claim, ReservedDeadlockAssignClaim):
                raise ManagerDispositionConflict()
            return claim, control, None
        raise ManagerDispositionConflict()

    @staticmethod
    def _require_assign_available(
        available: DeadlockManagerSealedClaimAvailable | None,
    ) -> tuple[SealedDeadlockAssignClaim, DeadlockManagerSealedClaimHandle]:
        if available is None or not isinstance(available.claim, SealedDeadlockAssignClaim):
            raise ManagerDispositionIntegrity()
        return available.claim, available.handle

    def _reserved_recovery(
        self,
        command: DeadlockManagerDispositionCommand,
    ) -> (
        tuple[
            ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
            DeadlockManagerReservationControlToken,
        ]
        | None
    ):
        with self._recovery_lock:
            recovery = self._reserved_recoveries.get(command.item_id)
            if recovery is None or not deadlock_manager_claim_matches_command(recovery[0], command):
                return None
            return recovery[0].model_copy(deep=True), recovery[1].model_copy(deep=True)

    def _remember_reserved(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        control: DeadlockManagerReservationControlToken,
    ) -> None:
        with self._recovery_lock:
            self._reserved_recoveries[claim.item_id] = (
                claim.model_copy(deep=True),
                control.model_copy(deep=True),
            )

    def _forget_reserved(self, item_id: str) -> None:
        with self._recovery_lock:
            self._reserved_recoveries.pop(item_id, None)

    def _validate_assign_claim(
        self,
        claim: ReservedDeadlockAssignClaim | SealedDeadlockAssignClaim,
        command: AssignDeadlockedOwner,
        context: _Context,
    ) -> None:
        target = self._validate_registry_target(context.conflict_claim, command.agent_id)
        self._validate_stored_claim(
            claim,
            context.item,
            context.request,
            context.case,
            context.conflict_claim,
        )
        if (
            claim.agent_id != command.agent_id
            or claim.agent_id != target.agent_id
            or claim.requires_approval != target.requires_approval
            or claim.rationale != command.rationale
        ):
            raise ManagerDispositionIntegrity()

    def _validate_dismiss_claim(
        self,
        claim: SealedDeadlockDismissClaim,
        command: DismissDeadlocked,
        context: _Context,
    ) -> None:
        self._validate_stored_claim(
            claim,
            context.item,
            context.request,
            context.case,
            context.conflict_claim,
        )
        if claim.rationale != command.rationale or claim.reason_code != "manager_declined":
            raise ManagerDispositionIntegrity()

    @staticmethod
    def _authority_assignment(
        claim: ReservedDeadlockAssignClaim | SealedDeadlockAssignClaim,
    ) -> RequestRouteGrantAssignment:
        return RequestRouteGrantAssignment(
            org_id=claim.org_id,
            request_id=claim.request_id,
            intent=claim.intent,
            agent_id=claim.agent_id,
            source=FromDeadlockManagerGrant(
                case_id=claim.case_id,
                item_id=claim.item_id,
                by_manager=claim.by_manager,
            ),
            idempotency_key=claim.idempotency_key,
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
            raise ManagerDispositionIntegrity() from error
        raise ManagerDispositionIntegrity()

    def _seal(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        control: DeadlockManagerReservationControlToken,
        command: DeadlockManagerDispositionCommand,
    ) -> DeadlockManagerSealedClaimAvailable:
        try:
            raw = self._managers.seal_deadlock_claim(claim, control_token=control)
        except ManagerDispositionError:
            raise
        except Exception as error:
            # Post-write response loss is recoverable through the source-specific follower API.
            try:
                follower = self._managers.reserve_validated_deadlock_action(
                    command.item_id,
                    command,
                    validate=lambda _item: (_ for _ in ()).throw(ManagerDispositionIntegrity()),
                )
            except Exception:
                follower = None
            if isinstance(follower, DeadlockManagerSealedClaimAvailable):
                available = self._canonical_attempt(follower)
                assert isinstance(available, DeadlockManagerSealedClaimAvailable)
                self._validate_sealed_payload(claim, available)
                self._forget_reserved(claim.item_id)
                return available
            self._remember_reserved(claim, control)
            raise ManagerDispositionDependency() from error
        available = self._canonical_attempt(raw)
        if not isinstance(available, DeadlockManagerSealedClaimAvailable):
            raise ManagerDispositionIntegrity()
        self._validate_sealed_payload(claim, available)
        stored = self._read_manager_claim(claim.item_id)
        if stored != available.claim:
            raise ManagerDispositionIntegrity()
        try:
            by_handle = self._managers.deadlock_claim_for_handle(available.handle)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if canonical_deadlock_manager_claim(by_handle) != available.claim:
            raise ManagerDispositionIntegrity()
        self._forget_reserved(claim.item_id)
        return available

    def _seal_preserving(
        self,
        claim: ReservedDeadlockAssignClaim,
        control: DeadlockManagerReservationControlToken,
        command: AssignDeadlockedOwner,
    ) -> None:
        self._seal(claim, control, command)

    @staticmethod
    def _validate_sealed_payload(
        reserved: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        available: DeadlockManagerSealedClaimAvailable,
    ) -> None:
        sealed = available.claim
        expected = reserved.model_dump(mode="python", round_trip=True)
        expected["kind"] = (
            "sealed_deadlock_assign"
            if isinstance(reserved, ReservedDeadlockAssignClaim)
            else "sealed_deadlock_dismiss"
        )
        try:
            if isinstance(reserved, ReservedDeadlockAssignClaim):
                canonical = SealedDeadlockAssignClaim.model_validate(expected, strict=True)
            else:
                canonical = SealedDeadlockDismissClaim.model_validate(expected, strict=True)
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        if sealed != canonical or available.handle.generation != reserved.generation:
            raise ManagerDispositionIntegrity()

    def _abandon_rejected(
        self,
        claim: ReservedDeadlockAssignClaim,
        control: DeadlockManagerReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> None:
        try:
            raw = self._managers.abandon_unmutated_deadlock_assign(
                claim,
                control_token=control,
                rejection=rejection,
            )
            if raw is not None:
                raise ManagerDispositionIntegrity()
        except ManagerDispositionError:
            raise
        except Exception as error:
            try:
                existing = self._managers.deadlock_claim_for_item(claim.item_id)
            except Exception as read_error:
                self._remember_reserved(claim, control)
                raise ManagerDispositionDependency() from read_error
            if existing is not None:
                self._remember_reserved(claim, control)
                raise ManagerDispositionDependency() from error
        if self._read_manager_claim(claim.item_id) is not None:
            raise ManagerDispositionIntegrity()
        self._forget_reserved(claim.item_id)

    def _read_grant(
        self,
        claim: SealedDeadlockAssignClaim,
        receipt: RequestRouteGrantReceipt,
    ) -> AuthorityGrant:
        try:
            raw = self._route_authority.authorize_for_request(
                claim.org_id,
                claim.request_id,
                claim.intent,
                claim.agent_id,
            )
        except Exception as error:
            raise ManagerDispositionDependency() from error
        try:
            if type(raw) is not AuthorityGrant:
                raise TypeError("AuthorityGrant exact type이 필요합니다.")
            grant = AuthorityGrant.model_validate(
                raw.model_dump(mode="python", round_trip=True), strict=True
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        if grant.policy_version != receipt.grant_version:
            raise ManagerDispositionIntegrity()
        return grant

    def _record_assign_mediation(
        self,
        *,
        context: _Context,
        manager_claim: SealedDeadlockAssignClaim,
        manager_handle: DeadlockManagerSealedClaimHandle,
        route: RouteTarget,
    ) -> SealedConflictMediationAvailable:
        evidence = ConflictResolutionEvidence(
            request_id=manager_claim.request_id,
            case_id=manager_claim.case_id,
            org_id=manager_claim.org_id,
            intent=manager_claim.intent,
            route=route,
            source=FromManagerMediation(
                item_id=manager_claim.item_id,
                by_manager=manager_claim.by_manager,
            ),
            supporting=(),
        )

        def validate(
            case: ConflictCase,
            conflict_claim: SealedDeadlockClaim,
            callback_handle: DeadlockManagerSealedClaimHandle,
        ) -> ValidatedMediationAssign:
            manager = self._manager_claim_for_handle(callback_handle)
            if (
                case != context.case
                or conflict_claim != context.conflict_claim
                or callback_handle != manager_handle
                or manager != manager_claim
            ):
                raise ConflictDispositionIntegrity("Mediation callback snapshot이 다릅니다.")
            return ValidatedMediationAssign(
                conflict_claim=conflict_claim,
                conflict_handle=context.conflict.handle,
                manager_claim=manager_claim,
                manager_handle=manager_handle,
                evidence=evidence,
            )

        return self._record_mediation(
            context.conflict,
            manager_handle,
            validate=validate,
        )

    def _record_dismiss_mediation(
        self,
        *,
        context: _Context,
        manager_claim: SealedDeadlockDismissClaim,
        manager_handle: DeadlockManagerSealedClaimHandle,
    ) -> SealedConflictMediationAvailable:
        def validate(
            case: ConflictCase,
            conflict_claim: SealedDeadlockClaim,
            callback_handle: DeadlockManagerSealedClaimHandle,
        ) -> ValidatedMediationDismiss:
            manager = self._manager_claim_for_handle(callback_handle)
            if (
                case != context.case
                or conflict_claim != context.conflict_claim
                or callback_handle != manager_handle
                or manager != manager_claim
            ):
                raise ConflictDispositionIntegrity("Mediation callback snapshot이 다릅니다.")
            return ValidatedMediationDismiss(
                conflict_claim=conflict_claim,
                conflict_handle=context.conflict.handle,
                manager_claim=manager_claim,
                manager_handle=manager_handle,
            )

        return self._record_mediation(
            context.conflict,
            manager_handle,
            validate=validate,
        )

    def _manager_claim_for_handle(
        self,
        handle: DeadlockManagerSealedClaimHandle,
    ) -> SealedDeadlockAssignClaim | SealedDeadlockDismissClaim:
        try:
            raw = self._managers.deadlock_claim_for_handle(handle)
        except ManagerDispositionError as error:
            raise ConflictDispositionIntegrity("Manager full handle이 다릅니다.") from error
        except Exception as error:
            raise ConflictDispositionDependency("Manager claim read가 실패했습니다.") from error
        try:
            canonical = canonical_deadlock_manager_claim(raw)
        except ManagerDispositionError as error:
            raise ConflictDispositionIntegrity("Manager claim이 손상됐습니다.") from error
        if not isinstance(canonical, (SealedDeadlockAssignClaim, SealedDeadlockDismissClaim)):
            raise ConflictDispositionIntegrity("sealed Manager claim이 아닙니다.")
        return canonical

    def _record_mediation(
        self,
        conflict: SealedConflictClaimAvailable,
        manager_handle: DeadlockManagerSealedClaimHandle,
        *,
        validate: Callable[
            [ConflictCase, SealedDeadlockClaim, DeadlockManagerSealedClaimHandle],
            ValidatedMediationAssign | ValidatedMediationDismiss,
        ],
    ) -> SealedConflictMediationAvailable:
        try:
            raw = self._conflicts.record_validated_mediation(
                conflict.handle,
                manager_handle,
                validate=validate,
            )
        except ConflictDispositionError as error:
            raise self._manager_error(error) from error
        except Exception as error:
            raise ManagerDispositionDependency() from error
        try:
            if type(raw) is not SealedConflictMediationAvailable:
                raise TypeError("SealedConflictMediationAvailable exact type이 필요합니다.")
            if type(raw.proof) is ValidatedMediationAssign:
                proof: ValidatedMediationAssign | ValidatedMediationDismiss = (
                    ValidatedMediationAssign(
                        conflict_claim=deepcopy(raw.proof.conflict_claim),
                        conflict_handle=raw.proof.conflict_handle.model_copy(deep=True),
                        manager_claim=raw.proof.manager_claim.model_copy(deep=True),
                        manager_handle=raw.proof.manager_handle.model_copy(deep=True),
                        evidence=raw.proof.evidence.model_copy(deep=True),
                    )
                )
            elif type(raw.proof) is ValidatedMediationDismiss:
                proof = ValidatedMediationDismiss(
                    conflict_claim=deepcopy(raw.proof.conflict_claim),
                    conflict_handle=raw.proof.conflict_handle.model_copy(deep=True),
                    manager_claim=raw.proof.manager_claim.model_copy(deep=True),
                    manager_handle=raw.proof.manager_handle.model_copy(deep=True),
                    reason_code=raw.proof.reason_code,
                )
            else:
                raise TypeError("Validated mediation exact type이 필요합니다.")
            available = SealedConflictMediationAvailable(
                proof=proof,
                handle=raw.handle.model_copy(deep=True),
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        if (
            available.proof.conflict_handle != conflict.handle
            or available.proof.manager_handle != manager_handle
            or available.handle.conflict_generation != conflict.handle.generation
            or available.handle.manager_generation != manager_handle.generation
        ):
            raise ManagerDispositionIntegrity()
        return available

    def _advance_assign(
        self,
        claim: SealedDeadlockAssignClaim,
        route: RouteTarget,
        *,
        ready_plan: _ReadyPlan | None,
    ) -> QuestionRequest:
        current = self._read_request(claim.request_id)
        if not isinstance(current.state, AwaitingManager):
            if not isinstance(current.state, FailedRequest):
                self._validate_assign_winner(current, route)
            return current
        self._validate_awaiting_manager(current, claim)
        if ready_plan is None:
            raise ManagerDispositionIntegrity()
        now = ready_plan.transitioned_at
        due_at = ready_plan.due_at
        trigger_key = f"request-dispatch:{claim.request_id}:1"
        try:
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
                clock=lambda: now,
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        self._revalidate_registry(claim)
        return self._cas_or_recover(current, updated, assign_route=route)

    def _advance_dismiss(
        self,
        claim: SealedDeadlockDismissClaim,
        *,
        transitioned_at: datetime | None,
    ) -> QuestionRequest:
        current = self._read_request(claim.request_id)
        if isinstance(current.state, DeclinedRequest):
            if current.revision == 3 and current.state.reason_code == "manager_declined":
                return current
            raise ManagerDispositionConflict()
        self._validate_awaiting_manager(current, claim)
        if transitioned_at is None:
            raise ManagerDispositionIntegrity()
        now = transitioned_at
        try:
            updated = current.transition(
                DeclinedRequest(reason_code="manager_declined"),
                clock=lambda: now,
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        return self._cas_or_recover(current, updated, assign_route=None)

    @staticmethod
    def _validate_awaiting_manager(
        request: QuestionRequest,
        claim: (SealedDeadlockAssignClaim | SealedDeadlockDismissClaim),
    ) -> None:
        state = request.state
        if (
            not isinstance(state, AwaitingManager)
            or request.revision != 2
            or state.public_kind != "contested"
            or state.item_id != claim.item_id
            or state.route is not None
            or state.attempt is not None
        ):
            raise ManagerDispositionConflict()

    def _cas_or_recover(
        self,
        current: QuestionRequest,
        updated: QuestionRequest,
        *,
        assign_route: RouteTarget | None,
    ) -> QuestionRequest:
        try:
            raw_won = self._requests.compare_and_set(
                current.request_id,
                current.revision,
                current,
                updated,
            )
        except Exception as error:
            latest = self._read_request(current.request_id)
            if latest == updated:
                return latest
            if latest == current:
                raise ManagerDispositionDependency() from error
            self._validate_cas_winner(latest, assign_route)
            return latest
        if type(raw_won) is not bool:
            raise ManagerDispositionIntegrity()
        latest = self._read_request(current.request_id)
        if raw_won:
            if latest != updated:
                raise ManagerDispositionIntegrity()
            return latest
        self._validate_cas_winner(latest, assign_route)
        return latest

    def _validate_cas_winner(
        self,
        latest: QuestionRequest,
        assign_route: RouteTarget | None,
    ) -> None:
        if assign_route is None:
            if (
                isinstance(latest.state, DeclinedRequest)
                and latest.revision == 3
                and latest.state.reason_code == "manager_declined"
            ):
                return
            raise ManagerDispositionConflict()
        self._validate_assign_winner(latest, assign_route)

    def _validate_assign_winner(
        self,
        request: QuestionRequest,
        route: RouteTarget,
    ) -> None:
        state = request.state
        trigger = f"request-dispatch:{request.request_id}:1"
        if isinstance(state, ReadyToDispatch):
            if (
                request.revision == 3
                and state.route == route
                and state.attempt == 1
                and state.trigger_key == trigger
                and state.handling.kind == "system"
                and state.handling.ref == trigger
            ):
                return
        elif isinstance(state, (AwaitingAnswer, AwaitingApproval)):
            if state.route == route and state.attempt == 1:
                return
        elif isinstance(state, AnsweredRequest):
            self._validate_completion(request, route)
            return
        elif isinstance(state, FailedRequest):
            raise ManagerDispositionConflict()
        raise ManagerDispositionConflict()

    def _ensure_resume_evidence(
        self,
        handle: DeadlockManagerSealedClaimHandle,
        request: QuestionRequest,
        route: RouteTarget,
    ) -> None:
        existing = self._read_resume_evidence(handle)
        if existing is None:
            if not isinstance(request.state, ReadyToDispatch) or request.revision != 3:
                raise ManagerDispositionIntegrity()
            expected = ResumeEvidence(
                request_id=request.request_id,
                from_revision=2,
                to_revision=3,
                route=route,
                trigger_key=f"request-dispatch:{request.request_id}:1",
            )
            try:
                raw = self._managers.record_resume_evidence(handle, expected)
                if raw is not None:
                    raise ManagerDispositionIntegrity()
            except ManagerDispositionError:
                raise
            except Exception as error:
                reread = self._read_resume_evidence(handle)
                if reread != expected:
                    raise ManagerDispositionDependency() from error
            existing = self._read_resume_evidence(handle)
        expected = ResumeEvidence(
            request_id=request.request_id,
            from_revision=2,
            to_revision=3,
            route=route,
            trigger_key=f"request-dispatch:{request.request_id}:1",
        )
        if existing != expected:
            raise ManagerDispositionIntegrity()
        if not isinstance(request.state, FailedRequest):
            self._validate_assign_winner(request, route)

    def _read_resume_evidence(
        self,
        handle: DeadlockManagerSealedClaimHandle,
    ) -> ResumeEvidence | None:
        try:
            raw = self._managers.resume_evidence_for_claim(handle)
        except ManagerDispositionError:
            raise
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if raw is None:
            return None
        try:
            if type(raw) is not ResumeEvidence:
                raise TypeError("ResumeEvidence exact type이 필요합니다.")
            return ResumeEvidence.model_validate(
                raw.model_dump(mode="python", round_trip=True), strict=True
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error

    def _resolve_case(
        self,
        mediation: SealedConflictMediationAvailable,
        claim: SealedDeadlockAssignClaim,
    ) -> ConflictCase:
        current = self._read_case(claim.case_id)
        expected = Resolution(
            intent=claim.intent,
            primary=claim.agent_id,
            rationale=claim.rationale,
        )
        if current.status == "resolved":
            if current.resolution != expected or current.manager_item_id != claim.item_id:
                raise ManagerDispositionIntegrity()
            target = current
        else:
            try:
                target = current.resolve_for_request(claim.agent_id, claim.rationale)
            except Exception as error:
                raise ManagerDispositionIntegrity() from error
        return self._transition_case(mediation.handle, target)

    def _decline_case(
        self,
        mediation: SealedConflictMediationAvailable,
        claim: SealedDeadlockDismissClaim,
    ) -> ConflictCase:
        current = self._read_case(claim.case_id)
        if current.status == "declined":
            target = current
        else:
            try:
                target = current.decline()
            except Exception as error:
                raise ManagerDispositionIntegrity() from error
        return self._transition_case(mediation.handle, target)

    def _transition_case(
        self,
        handle: ConflictMediationHandle,
        target: ConflictCase,
    ) -> ConflictCase:
        try:
            raw = self._conflicts.transition_for_mediation(handle, target=target)
        except ConflictDispositionError as error:
            raise self._manager_error(error) from error
        except Exception as error:
            current = self._read_case(target.case_id)
            if current == target:
                return current
            raise ManagerDispositionDependency() from error
        if type(raw) is not ConflictCase or raw != target:
            raise ManagerDispositionIntegrity()
        reread = self._read_case(target.case_id)
        if reread != target:
            raise ManagerDispositionIntegrity()
        return reread

    def _resolve_assign_item(
        self,
        handle: DeadlockManagerSealedClaimHandle,
        claim: SealedDeadlockAssignClaim,
        case: ConflictCase,
    ) -> ManagerItem:
        expected_resolution = Resolution(
            intent=claim.intent,
            primary=claim.agent_id,
            rationale=claim.rationale,
        )
        if case.resolution != expected_resolution:
            raise ManagerDispositionIntegrity()
        item = self._read_item(claim.item_id)
        if item.status == "resolved":
            target = item
        else:
            target = item.resolve(
                ManagerResolution(
                    action=AssignOwner(
                        by_manager=claim.by_manager,
                        primary=claim.agent_id,
                        rationale=claim.rationale,
                    ),
                    resolution=expected_resolution,
                )
            )
        return self._resolve_item(handle, target)

    def _resolve_dismiss_item(
        self,
        handle: DeadlockManagerSealedClaimHandle,
        claim: SealedDeadlockDismissClaim,
        case: ConflictCase,
    ) -> ManagerItem:
        if case.status != "declined" or case.decline_reason != "manager_declined":
            raise ManagerDispositionIntegrity()
        item = self._read_item(claim.item_id)
        if item.status == "resolved":
            target = item
        else:
            target = item.resolve(
                ManagerResolution(
                    action=Dismiss(
                        by_manager=claim.by_manager,
                        rationale=claim.rationale,
                    )
                )
            )
        return self._resolve_item(handle, target)

    def _resolve_item(
        self,
        handle: DeadlockManagerSealedClaimHandle,
        target: ManagerItem,
    ) -> ManagerItem:
        try:
            raw = self._managers.resolve_for_claim(handle, target)
        except ManagerDispositionError:
            raise
        except Exception as error:
            current = self._read_item(target.item_id)
            if current == target:
                return current
            raise ManagerDispositionDependency() from error
        if type(raw) is not ManagerItem or raw != target:
            raise ManagerDispositionIntegrity()
        reread = self._read_item(target.item_id)
        if reread != target:
            raise ManagerDispositionIntegrity()
        return reread

    def _validate_completion(self, request: QuestionRequest, route: RouteTarget) -> None:
        if not isinstance(request.state, AnsweredRequest):
            raise ManagerDispositionIntegrity()
        try:
            by_request = self._completion_reader.by_request(request.request_id)
            by_record = self._completion_reader.by_record(request.state.record_id)
            first = canonical_completion_bundle(by_request)
            second = canonical_completion_bundle(by_record)
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        if (
            first != second
            or first.request != request
            or first.terminal_audit.request_id != request.request_id
            or first.terminal_audit.record_id != request.state.record_id
            or first.terminal_audit.route != route
            or first.terminal_audit.attempt != 1
        ):
            raise ManagerDispositionIntegrity()

    def _wake(self, request_id: str) -> ExecutionWake:
        try:
            raw = self._execution_starter.ensure_started(request_id)
        except Exception:
            return ExecutionDeferred(reason_code="submission_failed")
        try:
            if type(raw) is ExecutionStarted:
                return ExecutionStarted.model_validate(raw.model_dump(), strict=True)
            if type(raw) is ExecutionAlreadyRunning:
                return ExecutionAlreadyRunning.model_validate(raw.model_dump(), strict=True)
            if type(raw) is ExecutionNotNeeded:
                return ExecutionNotNeeded.model_validate(raw.model_dump(), strict=True)
            if type(raw) is ExecutionDeferred:
                return ExecutionDeferred.model_validate(raw.model_dump(), strict=True)
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        raise ManagerDispositionIntegrity()

    def _publish(self, request_id: str) -> TerminalDelivery:
        try:
            raw = self._terminal_publisher.publish_terminal(request_id)
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
            raise ManagerDispositionIntegrity() from error
        raise ManagerDispositionIntegrity()

    def _transition_time(self, current: QuestionRequest) -> datetime:
        try:
            now = self._clock()
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if not self._is_aware(now) or now < current.updated_at:
            raise ManagerDispositionDependency()
        return now

    def _prepare_ready_plan(self, request: QuestionRequest) -> _ReadyPlan | None:
        if not isinstance(request.state, AwaitingManager):
            return None
        now = self._transition_time(request)
        try:
            due_at = self._deadline_policy.deadline_for(
                request.org_id,
                "ready_to_dispatch",
                now,
            )
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if not self._is_aware(due_at) or due_at < max(now, request.updated_at):
            raise ManagerDispositionDependency()
        return _ReadyPlan(transitioned_at=now, due_at=due_at)

    def _prepare_dismiss_time(self, request: QuestionRequest) -> datetime | None:
        if not isinstance(request.state, AwaitingManager):
            return None
        return self._transition_time(request)

    @staticmethod
    def _is_aware(value: object) -> bool:
        if type(value) is not datetime:
            return False
        try:
            return value.tzinfo is not None and value.utcoffset() is not None
        except Exception:
            return False

    def _new_generation(self) -> str:
        try:
            raw = self._generation_factory()
        except Exception as error:
            raise ManagerDispositionDependency() from error
        if type(raw) is not str or not raw.strip():
            raise ManagerDispositionIntegrity()
        return raw

    @staticmethod
    def _manager_error(error: ConflictDispositionError) -> ManagerDispositionError:
        if isinstance(error, ConflictDispositionDependency):
            return ManagerDispositionDependency()
        if isinstance(error, ConflictDispositionConflict):
            return ManagerDispositionConflict()
        if isinstance(error, ConflictDispositionIntegrity):
            return ManagerDispositionIntegrity()
        return ManagerDispositionIntegrity()
