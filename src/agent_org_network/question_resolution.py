"""Request-first 질문 접수·초기 라우팅 애플리케이션 경계(P17.2c-1b·1c).

질문을 먼저 ``Received``로 저장한 뒤 Router 결과를 Question Request 상태와 요청별
ConflictCase/ManagerItem으로 옮긴다. 실행·답변·감사·전송은 이 모듈의 책임이 아니다.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from threading import Lock
from typing import Literal, Protocol, TypeAlias, assert_never

from pydantic import BaseModel, ConfigDict, field_validator

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied as CentralAuthorizationDenied,
    AuthorizationGrant as CentralAuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.conflict import (
    Candidate,
    ConflictCase,
    RequestAwareConflictCaseStore,
)
from agent_org_network.decision import Contested, Routed, RoutingDecision, Unowned
from agent_org_network.manager_queue import (
    FromUnowned,
    ManagerItem,
    RequestAwareManagerQueueStore,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingConflict,
    AwaitingManager,
    DeclinedRequest,
    FailedRequest,
    HandlingAssignment,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
    Received,
    RequestIdFactory,
    RequestStateKind,
    RouteTarget,
)
from agent_org_network.request_correlation import (
    LinkedEntityMismatchError,
    require_request_id,
)
from agent_org_network.router import RouterPort
from agent_org_network.storage_capability import validate_workflow_composition

Clock: TypeAlias = Callable[[], datetime]


class _FrozenDto(BaseModel):
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


class RequesterPrincipal(_FrozenDto):
    """인증 계층이 넘기는 조직·요청자 신원."""

    org_id: str
    subject_id: str


QuestionPrincipal: TypeAlias = RequesterPrincipal | AuthenticatedPrincipal


class AskQuestion(_FrozenDto):
    """Question Request 신규 접수 명령."""

    principal: QuestionPrincipal
    question: str
    session_id: str | None = None
    context_snapshot: str | None = None


class AuthorityGrant(_FrozenDto):
    """중앙 Route Authority가 발행한 허용 증거."""

    policy_version: str


class RequestPending(_FrozenDto):
    request_id: str
    state: RequestStateKind
    retryable: bool
    message: str


class RequestAnswered(_FrozenDto):
    request_id: str
    record_id: str


class RequestDeclined(_FrozenDto):
    request_id: str
    reason_code: str
    message: str


class RequestFailed(_FrozenDto):
    request_id: str
    error_code: str
    message: str


class RequestNotFound(_FrozenDto):
    """미존재와 소유권 불일치를 구분하지 않는 field-free 조회 결과."""


QuestionOutcome: TypeAlias = RequestPending | RequestAnswered | RequestDeclined | RequestFailed
QuestionLookupResult: TypeAlias = QuestionOutcome | RequestNotFound


class HandlingDeadlinePolicy(Protocol):
    def deadline_for(
        self,
        org_id: str,
        state_kind: RequestStateKind,
        started_at: datetime,
    ) -> datetime: ...


class RouteAuthority(Protocol):
    def authorize(
        self,
        org_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None: ...


class InitialRoutingError(RuntimeError):
    """Received 이후 초기 라우팅이 완료되지 못한 구조화 오류."""

    def __init__(
        self,
        *,
        request_id: str,
        code: str,
        retryable: bool,
        message: str,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.code = code
        self.retryable = retryable


class InvalidInitialRoutingError(InitialRoutingError):
    def __init__(self, request_id: str, message: str) -> None:
        super().__init__(
            request_id=request_id,
            code="invalid_routing_decision",
            retryable=False,
            message=message,
        )


class RouteAuthorityDeniedError(InitialRoutingError):
    def __init__(self, request_id: str) -> None:
        super().__init__(
            request_id=request_id,
            code="route_authority_denied",
            retryable=False,
            message="중앙 Route Authority가 실행 대상을 허용하지 않았습니다.",
        )


class QuestionAuthorizationDeniedError(RuntimeError):
    """중앙 질문 권한 거부·오류를 세부정보 없이 고정하는 접수 실패."""

    def __init__(self) -> None:
        super().__init__("질문 권한이 거부되었습니다.")


class QuestionAuthorizationUnavailableError(RuntimeError):
    """중앙 질문 권한 의존성 장애를 세부정보 없이 고정하는 실패."""

    def __init__(self) -> None:
        super().__init__("질문 권한 서비스를 사용할 수 없습니다.")


class InitialRoutingDependencyError(InitialRoutingError):
    def __init__(self, request_id: str, dependency: str) -> None:
        super().__init__(
            request_id=request_id,
            code=f"{dependency}_unavailable",
            retryable=True,
            message=f"초기 라우팅 의존성 오류: {dependency}",
        )


class InitialRoutingConflictError(InitialRoutingError):
    def __init__(self, request_id: str, message: str) -> None:
        super().__init__(
            request_id=request_id,
            code="initial_routing_conflict",
            retryable=False,
            message=message,
        )


class ConcurrentInitialRoutingError(InitialRoutingError):
    """CAS 경쟁자가 서로 다른 초기 책임 상태를 제안했거나 수렴할 winner가 없음."""

    def __init__(self, request_id: str, message: str) -> None:
        super().__init__(
            request_id=request_id,
            code="concurrent_initial_routing",
            retryable=False,
            message=message,
        )


class _RequestLockEntry:
    def __init__(self) -> None:
        self.lock = Lock()
        self.references = 0


class RequestLockPool:
    """같은 프로세스의 request별 advance를 직렬화하는 bounded lock pool.

    ``references``는 lock holder와 대기자를 모두 센다. 마지막 참조가 빠질 때도
    동일 entry인지 확인한 뒤 제거해, 새 entry를 오래된 해제가 지우지 못하게 한다.
    """

    def __init__(self) -> None:
        self._guard = Lock()
        self._entries: dict[str, _RequestLockEntry] = {}

    @contextmanager
    def hold(self, request_id: str) -> Generator[None, None, None]:
        correlated_request_id = require_request_id(request_id)
        with self._guard:
            entry = self._entries.get(correlated_request_id)
            if entry is None:
                entry = _RequestLockEntry()
                self._entries[correlated_request_id] = entry
            entry.references += 1

        acquired = False
        try:
            entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._guard:
                entry.references -= 1
                if entry.references == 0 and self._entries.get(correlated_request_id) is entry:
                    del self._entries[correlated_request_id]

    @property
    def active_count(self) -> int:
        """테스트·진단용 현재 holder+waiter key 수(snapshot)."""
        with self._guard:
            return len(self._entries)


_PENDING_MESSAGE = "질문을 처리하고 있습니다."
_DECLINED_MESSAGE = "질문 처리가 거절되었습니다."
_FAILED_MESSAGE = "질문을 처리하지 못했습니다."


class QuestionResolutionApplication:
    """Question Request를 먼저 남기고 초기 책임 배정까지만 수행한다."""

    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        router: RouterPort,
        conflicts: RequestAwareConflictCaseStore,
        managers: RequestAwareManagerQueueStore,
        route_authority: RouteAuthority,
        deadline_policy: HandlingDeadlinePolicy,
        request_id_factory: RequestIdFactory,
        clock: Clock,
        request_locks: RequestLockPool | None = None,
        production_style: bool = False,
        central_authorizer: CentralAuthorizer | None = None,
    ) -> None:
        if production_style:
            validate_workflow_composition(
                requests=requests,
                conflicts=conflicts,
                managers=managers,
                require_durable=True,
            )
        self._requests = requests
        self._router = router
        self._conflicts = conflicts
        self._managers = managers
        self._route_authority = route_authority
        self._deadline_policy = deadline_policy
        self._request_id_factory = request_id_factory
        self._clock = clock
        self._request_locks = request_locks or RequestLockPool()
        self._central_authorizer = central_authorizer

    def ask(
        self,
        command: AskQuestion,
        *,
        result_action: Literal["question.read", "question.stream"] | None = None,
    ) -> QuestionOutcome:
        """Received를 영속화한 뒤 한 차례 초기 라우팅을 시도한다."""
        if self._central_authorizer is not None:
            if type(result_action) is not str or result_action not in (
                "question.read",
                "question.stream",
            ):
                raise QuestionAuthorizationDeniedError()
        principal = self._authorize_create(command.principal)
        if result_action is not None and self._central_authorizer is not None:
            if type(principal) is not AuthenticatedPrincipal:
                raise QuestionAuthorizationDeniedError()
            assert isinstance(principal, AuthenticatedPrincipal)
            if result_action not in ("question.read", "question.stream"):
                raise QuestionAuthorizationDeniedError()
            intake_resource = ResourceRef(
                org_id=principal.org_id,
                kind="question",
                owner_subject_id=principal.subject_id,
            )
            result_authorization = self._central_authorization(
                principal,
                result_action,
                intake_resource,
            )
            if result_authorization == "unavailable":
                raise QuestionAuthorizationUnavailableError()
            if result_authorization == "denied":
                raise QuestionAuthorizationDeniedError()
        request_id = self._request_id_factory()
        started_at = self._clock()
        try:
            due_at = self._intake_deadline(
                principal.org_id,
                started_at,
                request_id,
            )
        except InitialRoutingDependencyError:
            # Deadline 의존성이 죽어도 질문 자체를 잃지 않는다. 즉시 만료된 zero-SLA
            # Received로 남겨 recovery/운영자가 볼 수 있게 하고 Router는 호출하지 않는다.
            due_at = started_at
            deadline_failed = True
        else:
            deadline_failed = False
        received = QuestionRequest.receive(
            org_id=principal.org_id,
            requester_id=principal.subject_id,
            question=command.question,
            request_id_factory=lambda: request_id,
            clock=lambda: started_at,
            due_at=due_at,
            session_id=command.session_id,
            context_snapshot=command.context_snapshot,
        )
        self._requests.create(received)
        if deadline_failed:
            return self._project(received)
        try:
            result = self.advance(received.request_id, expected_revision=0)
        except InitialRoutingError as error:
            if not error.retryable:
                raise
            current = self._requests.get(received.request_id)
            return self._project(current if current is not None else received)
        if isinstance(result, RequestNotFound):
            raise InitialRoutingConflictError(
                received.request_id,
                "저장 직후 Question Request를 다시 찾을 수 없습니다.",
            )
        return result

    def retrieve(
        self,
        request_id: str,
        principal: QuestionPrincipal,
        *,
        action: Literal["question.read", "question.stream"] = "question.read",
    ) -> QuestionLookupResult:
        """조직과 요청자가 모두 같은 Request만 결과로 투영한다."""
        if self._central_authorizer is not None and type(principal) is not AuthenticatedPrincipal:
            return RequestNotFound()
        request = self._requests.get(request_id)
        if self._central_authorizer is not None:
            if request is None or type(principal) is not AuthenticatedPrincipal:
                return RequestNotFound()
            try:
                canonical_principal = AuthenticatedPrincipal.model_validate(principal)
            except Exception:
                return RequestNotFound()
            resource = ResourceRef(
                org_id=request.org_id,
                kind="question",
                resource_id=request.request_id,
                owner_subject_id=request.requester_id,
            )
            authorization = self._central_authorization(canonical_principal, action, resource)
            if authorization == "unavailable":
                raise QuestionAuthorizationUnavailableError()
            if authorization == "denied":
                return RequestNotFound()
            if (
                request.org_id != canonical_principal.org_id
                or request.requester_id != canonical_principal.subject_id
            ):
                return RequestNotFound()
            return self._project(request)
        if (
            request is None
            or request.org_id != principal.org_id
            or request.requester_id != principal.subject_id
        ):
            return RequestNotFound()
        return self._project(request)

    def _authorize_create(self, principal: object) -> QuestionPrincipal:
        if self._central_authorizer is None:
            if type(principal) not in (RequesterPrincipal, AuthenticatedPrincipal):
                raise QuestionAuthorizationDeniedError()
            assert isinstance(principal, (RequesterPrincipal, AuthenticatedPrincipal))
            try:
                return type(principal).model_validate(principal)
            except Exception:
                raise QuestionAuthorizationDeniedError() from None
        if type(principal) is not AuthenticatedPrincipal:
            raise QuestionAuthorizationDeniedError()
        try:
            canonical = AuthenticatedPrincipal.model_validate(principal)
            resource = ResourceRef(org_id=canonical.org_id, kind="question")
        except Exception:
            raise QuestionAuthorizationDeniedError() from None
        authorization = self._central_authorization(canonical, "question.create", resource)
        if authorization == "unavailable":
            raise QuestionAuthorizationUnavailableError()
        if authorization == "denied":
            raise QuestionAuthorizationDeniedError()
        return canonical

    def _central_authorization(
        self,
        principal: AuthenticatedPrincipal,
        action: Literal["question.create", "question.read", "question.stream"],
        resource: ResourceRef,
    ) -> Literal["allowed", "denied", "unavailable"]:
        authorizer = self._central_authorizer
        if authorizer is None:
            return "allowed"
        try:
            raw = authorizer.authorize(principal, action, resource)
        except Exception:
            return "unavailable"
        if type(raw) is CentralAuthorizationDenied:
            try:
                denied = CentralAuthorizationDenied.model_validate(raw)
            except Exception:
                return "denied"
            return "unavailable" if denied.kind == "policy_unavailable" else "denied"
        if type(raw) is not CentralAuthorizationGrant:
            return "denied"
        try:
            CentralAuthorizationGrant.model_validate(raw)
            fields_match = bool(
                raw.org_id == principal.org_id == resource.org_id
                and raw.subject_id == principal.subject_id
                and raw.action == action
                and raw.resource == resource
                and raw.roles
            )
        except Exception:
            return "denied"
        if not fields_match:
            return "denied"
        try:
            verifier = authorizer.verify
            if not callable(verifier):
                return "unavailable"
            verified = verifier(raw, principal, action, resource)
        except Exception:
            return "unavailable"
        if type(verified) is not bool:
            return "unavailable"
        return "allowed" if verified else "denied"

    def advance(
        self,
        request_id: str,
        *,
        expected_revision: object,
    ) -> QuestionLookupResult:
        """현재 revision의 Received를 초기 책임 상태로 한 칸 전진시킨다."""
        with self._request_locks.hold(request_id):
            return self._advance_locked(
                request_id,
                expected_revision=expected_revision,
            )

    def _advance_locked(
        self,
        request_id: str,
        *,
        expected_revision: object,
    ) -> QuestionLookupResult:
        request = self._requests.get(request_id)
        if request is None:
            return RequestNotFound()
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise InitialRoutingConflictError(
                request_id,
                "expected_revision은 0 이상의 정수여야 합니다.",
            )
        if request.revision != expected_revision:
            if self._is_converged_initial_winner(request, expected_revision):
                self._validate_winner_link_consistency(request)
                return self._project(request)
            raise InitialRoutingConflictError(
                request_id,
                "Question Request revision이 예상값과 다릅니다.",
            )
        if not isinstance(request.state, Received):
            return self._project(request)

        updated = self._update_from_linked_orphan(request)
        if updated is None:
            try:
                decision = self._router.route(request.question)
            except Exception as error:
                raise InitialRoutingDependencyError(request_id, "router") from error
            updated = self._initial_update(request, decision)
        if not self._requests.compare_and_set(
            request_id,
            expected_revision,
            request,
            updated,
        ):
            winner = self._requests.get(request_id)
            if winner is not None and self._same_initial_winner(winner, updated):
                self._validate_winner_link_consistency(winner)
                return self._project(winner)
            raise ConcurrentInitialRoutingError(
                request_id,
                "초기 라우팅 CAS 경쟁 결과가 같은 winner로 수렴하지 않았습니다.",
            )
        return self._project(updated)

    @staticmethod
    def _is_converged_initial_winner(
        request: QuestionRequest,
        expected_revision: int,
    ) -> bool:
        return (
            expected_revision == 0
            and request.revision == 1
            and request.initial_disposition is not None
            and isinstance(
                request.state,
                (ReadyToDispatch, AwaitingConflict, AwaitingManager),
            )
        )

    @staticmethod
    def _same_initial_winner(
        winner: QuestionRequest,
        proposed: QuestionRequest,
    ) -> bool:
        if (
            winner.request_id != proposed.request_id
            or winner.revision != proposed.revision
            or winner.intent != proposed.intent
            or winner.initial_disposition != proposed.initial_disposition
        ):
            return False
        left = winner.state
        right = proposed.state
        if isinstance(left, ReadyToDispatch) and isinstance(right, ReadyToDispatch):
            return (
                left.route == right.route
                and left.attempt == right.attempt
                and left.trigger_key == right.trigger_key
                and left.handling.kind == right.handling.kind
                and left.handling.ref == right.handling.ref
            )
        if isinstance(left, AwaitingConflict) and isinstance(right, AwaitingConflict):
            return (
                left.case_id == right.case_id
                and left.handling.kind == right.handling.kind
                and left.handling.ref == right.handling.ref
            )
        if isinstance(left, AwaitingManager) and isinstance(right, AwaitingManager):
            return (
                left.item_id == right.item_id
                and left.public_kind == right.public_kind
                and left.route == right.route
                and left.attempt == right.attempt
                and left.handling.kind == right.handling.kind
                and left.handling.ref == right.handling.ref
            )
        return False

    def _update_from_linked_orphan(
        self,
        request: QuestionRequest,
    ) -> QuestionRequest | None:
        """Received에 먼저 남은 request-aware Case/Item snapshot으로 Router 없이 복구."""
        case, item = self._read_linked_snapshots(request.request_id)
        if case is not None and item is not None:
            raise LinkedEntityMismatchError(
                f"Question Request {request.request_id!r}에 ConflictCase와 "
                "ManagerItem이 동시에 연결돼 있습니다."
            )
        if case is not None:
            return self._update_from_conflict_orphan(request, case)
        if item is not None:
            return self._update_from_manager_orphan(request, item)
        return None

    def _read_linked_snapshots(
        self,
        request_id: str,
    ) -> tuple[ConflictCase | None, ManagerItem | None]:
        try:
            return (
                self._conflicts.get_by_request(request_id),
                self._managers.get_by_request(request_id),
            )
        except LinkedEntityMismatchError:
            raise
        except Exception as error:
            raise InitialRoutingDependencyError(
                request_id,
                "linked_store",
            ) from error

    def _validate_winner_link_consistency(self, request: QuestionRequest) -> None:
        """stale initial advance가 갈라진 linked workflow를 성공으로 숨기지 않게 한다."""
        case, item = self._read_linked_snapshots(request.request_id)
        state = request.state
        if isinstance(state, ReadyToDispatch):
            if case is None and item is None:
                return
        elif isinstance(state, AwaitingConflict):
            if (
                case is not None
                and item is None
                and case.case_id == state.case_id
                and case.request_id == request.request_id
                and case.question == request.question
                and case.intent == request.intent
                and case.status == "open"
                and case.resolution is None
            ):
                try:
                    self._canonical_candidate_values(
                        request.request_id,
                        case.candidates,
                    )
                except InitialRoutingError as error:
                    raise LinkedEntityMismatchError(
                        f"Question Request {request.request_id!r}의 ConflictCase "
                        "후보 snapshot이 유효하지 않습니다."
                    ) from error
                return
        elif isinstance(state, AwaitingManager):
            source = item.source if item is not None else None
            linked_intent = source.decision.intent if isinstance(source, FromUnowned) else None
            normalized_intent = (
                linked_intent if linked_intent is not None and linked_intent.strip() else None
            )
            if (
                case is None
                and item is not None
                and item.item_id == state.item_id
                and item.request_id == request.request_id
                and item.status == "open"
                and item.resolution is None
                and state.public_kind == "unowned"
                and isinstance(source, FromUnowned)
                and source.question == request.question
                and source.decision.escalated_to == item.manager_id
                and normalized_intent == request.intent
            ):
                return
        raise LinkedEntityMismatchError(
            f"Question Request {request.request_id!r}의 initial winner와 linked Store가 "
            "정확히 일치하지 않습니다."
        )

    def _update_from_conflict_orphan(
        self,
        request: QuestionRequest,
        case: ConflictCase,
    ) -> QuestionRequest:
        if (
            case.request_id != request.request_id
            or case.question != request.question
            or case.status != "open"
            or case.resolution is not None
        ):
            raise LinkedEntityMismatchError(
                f"Question Request {request.request_id!r}의 ConflictCase snapshot이 "
                "Received 원형과 맞지 않습니다."
            )
        intent = self._require_nonblank(
            case.intent,
            request.request_id,
            "ConflictCase.intent",
        )
        self._canonical_candidate_values(request.request_id, case.candidates)
        transitioned_at = self._transition_time(request)
        due_at = self._transition_deadline(
            request,
            "awaiting_conflict",
            transitioned_at,
        )
        return self._record_conflict_routing(
            request,
            intent=intent,
            case_id=case.case_id,
            transitioned_at=transitioned_at,
            due_at=due_at,
        )

    def _update_from_manager_orphan(
        self,
        request: QuestionRequest,
        item: ManagerItem,
    ) -> QuestionRequest:
        source = item.source
        if (
            item.request_id != request.request_id
            or item.status != "open"
            or item.resolution is not None
            or not isinstance(source, FromUnowned)
            or source.question != request.question
            or source.decision.escalated_to != item.manager_id
        ):
            raise LinkedEntityMismatchError(
                f"Question Request {request.request_id!r}의 ManagerItem snapshot이 "
                "Received 원형과 맞지 않습니다."
            )
        self._require_nonblank(
            item.manager_id,
            request.request_id,
            "ManagerItem.manager_id",
        )
        intent = source.decision.intent if source.decision.intent.strip() else None
        transitioned_at = self._transition_time(request)
        due_at = self._transition_deadline(
            request,
            "awaiting_manager",
            transitioned_at,
        )
        return self._record_manager_routing(
            request,
            intent=intent,
            item_id=item.item_id,
            transitioned_at=transitioned_at,
            due_at=due_at,
        )

    def _initial_update(
        self,
        request: QuestionRequest,
        decision: RoutingDecision,
    ) -> QuestionRequest:
        match decision:
            case Routed():
                return self._route_routed(request, decision)
            case Contested():
                return self._route_contested(request, decision)
            case Unowned():
                return self._route_unowned(request, decision)
            case _ as never:
                try:
                    assert_never(never)
                except AssertionError as error:
                    raise InvalidInitialRoutingError(
                        request.request_id,
                        "알 수 없는 RoutingDecision입니다.",
                    ) from error

    def _route_routed(
        self,
        request: QuestionRequest,
        decision: Routed,
    ) -> QuestionRequest:
        intent = self._require_nonblank(
            decision.intent,
            request.request_id,
            "Routed.intent",
        )
        agent_id = self._require_nonblank(
            decision.primary.agent_id,
            request.request_id,
            "Routed.primary.agent_id",
        )
        try:
            grant = self._route_authority.authorize(request.org_id, intent, agent_id)
        except Exception as error:
            raise InitialRoutingDependencyError(
                request.request_id,
                "route_authority",
            ) from error
        if grant is None:
            raise RouteAuthorityDeniedError(request.request_id)
        policy_version = self._require_nonblank(
            grant.policy_version,
            request.request_id,
            "AuthorityGrant.policy_version",
        )

        transitioned_at = self._transition_time(request)
        due_at = self._transition_deadline(
            request,
            "ready_to_dispatch",
            transitioned_at,
        )
        trigger_key = f"request-dispatch:{request.request_id}:1"
        target = ReadyToDispatch(
            route=RouteTarget(
                intent=intent,
                agent_id=agent_id,
                requires_approval=decision.requires_approval,
                authority_version=policy_version,
            ),
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=due_at,
            ),
        )
        return request.record_initial_routing(
            intent=intent,
            disposition="routed",
            target=target,
            clock=lambda: transitioned_at,
        )

    def _route_contested(
        self,
        request: QuestionRequest,
        decision: Contested,
    ) -> QuestionRequest:
        intent = self._require_nonblank(
            decision.intent,
            request.request_id,
            "Contested.intent",
        )
        candidates = self._canonical_candidates(request.request_id, decision)
        transitioned_at = self._transition_time(request)
        due_at = self._transition_deadline(
            request,
            "awaiting_conflict",
            transitioned_at,
        )
        proposed = ConflictCase.for_request(
            request_id=request.request_id,
            intent=intent,
            question=request.question,
            candidates=candidates,
            opened_at=transitioned_at,
        )
        try:
            case, _ = self._conflicts.create_or_get_for_request(proposed)
        except LinkedEntityMismatchError:
            raise
        except Exception as error:
            raise InitialRoutingDependencyError(
                request.request_id,
                "conflict_store",
            ) from error
        return self._record_conflict_routing(
            request,
            intent=intent,
            case_id=case.case_id,
            transitioned_at=transitioned_at,
            due_at=due_at,
        )

    @staticmethod
    def _record_conflict_routing(
        request: QuestionRequest,
        *,
        intent: str,
        case_id: str,
        transitioned_at: datetime,
        due_at: datetime,
    ) -> QuestionRequest:
        target = AwaitingConflict(
            case_id=case_id,
            handling=HandlingAssignment(
                kind="conflict_case",
                ref=case_id,
                due_at=due_at,
            ),
        )
        return request.record_initial_routing(
            intent=intent,
            disposition="contested",
            target=target,
            clock=lambda: transitioned_at,
        )

    def _route_unowned(
        self,
        request: QuestionRequest,
        decision: Unowned,
    ) -> QuestionRequest:
        manager_id = self._require_nonblank(
            decision.escalated_to,
            request.request_id,
            "Unowned.escalated_to",
        )
        intent = decision.intent if decision.intent.strip() else None
        transitioned_at = self._transition_time(request)
        due_at = self._transition_deadline(
            request,
            "awaiting_manager",
            transitioned_at,
        )
        proposed = ManagerItem.for_request(
            request_id=request.request_id,
            manager_id=manager_id,
            source=FromUnowned(decision=decision, question=request.question),
            created_at=transitioned_at,
        )
        try:
            item, _ = self._managers.create_or_get_for_request(proposed)
        except LinkedEntityMismatchError:
            raise
        except Exception as error:
            raise InitialRoutingDependencyError(
                request.request_id,
                "manager_store",
            ) from error
        return self._record_manager_routing(
            request,
            intent=intent,
            item_id=item.item_id,
            transitioned_at=transitioned_at,
            due_at=due_at,
        )

    @staticmethod
    def _record_manager_routing(
        request: QuestionRequest,
        *,
        intent: str | None,
        item_id: str,
        transitioned_at: datetime,
        due_at: datetime,
    ) -> QuestionRequest:
        target = AwaitingManager(
            item_id=item_id,
            public_kind="unowned",
            handling=HandlingAssignment(
                kind="manager_item",
                ref=item_id,
                due_at=due_at,
            ),
        )
        return request.record_initial_routing(
            intent=intent,
            disposition="unowned",
            target=target,
            clock=lambda: transitioned_at,
        )

    def _intake_deadline(
        self,
        org_id: str,
        started_at: datetime,
        request_id: str,
    ) -> datetime:
        return self._deadline(org_id, "received", started_at, request_id=request_id)

    def _transition_deadline(
        self,
        request: QuestionRequest,
        state_kind: RequestStateKind,
        started_at: datetime,
    ) -> datetime:
        return self._deadline(
            request.org_id,
            state_kind,
            started_at,
            request_id=request.request_id,
        )

    def _deadline(
        self,
        org_id: str,
        state_kind: RequestStateKind,
        started_at: datetime,
        *,
        request_id: str,
    ) -> datetime:
        try:
            due_at = self._deadline_policy.deadline_for(
                org_id,
                state_kind,
                started_at,
            )
            if due_at.tzinfo is None or due_at.utcoffset() is None:
                raise ValueError("deadline은 timezone-aware여야 합니다.")
            if due_at < started_at:
                raise ValueError("deadline은 상태 시작 시각보다 빠를 수 없습니다.")
            return due_at
        except Exception as error:
            raise InitialRoutingDependencyError(request_id, "deadline_policy") from error

    def _transition_time(self, request: QuestionRequest) -> datetime:
        try:
            value = self._clock()
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("clock은 timezone-aware여야 합니다.")
            if value < request.updated_at:
                raise ValueError("clock은 Question Request.updated_at보다 역행할 수 없습니다.")
            return value
        except Exception as error:
            raise InitialRoutingDependencyError(request.request_id, "clock") from error

    @staticmethod
    def _require_nonblank(value: str, request_id: str, field: str) -> str:
        if not value.strip():
            raise InvalidInitialRoutingError(
                request_id,
                f"{field}는 nonblank 문자열이어야 합니다.",
            )
        return value

    @classmethod
    def _canonical_candidates(
        cls,
        request_id: str,
        decision: Contested,
    ) -> tuple[Candidate, ...]:
        return cls._canonical_candidate_values(
            request_id,
            tuple(
                Candidate(agent_id=card.agent_id, owner=card.owner) for card in decision.candidates
            ),
        )

    @classmethod
    def _canonical_candidate_values(
        cls,
        request_id: str,
        candidates: tuple[Candidate, ...],
    ) -> tuple[Candidate, ...]:
        by_agent: dict[str, str] = {}
        for candidate in candidates:
            agent_id = cls._require_nonblank(
                candidate.agent_id,
                request_id,
                "Contested.candidate.agent_id",
            )
            owner = cls._require_nonblank(
                candidate.owner,
                request_id,
                "Contested.candidate.owner",
            )
            existing_owner = by_agent.get(agent_id)
            if existing_owner is not None and existing_owner != owner:
                raise InvalidInitialRoutingError(
                    request_id,
                    "같은 candidate agent_id에 서로 다른 Owner가 지정됐습니다.",
                )
            by_agent[agent_id] = owner
        if len(by_agent) < 2:
            raise InvalidInitialRoutingError(
                request_id,
                "Contested에는 서로 다른 후보가 둘 이상 필요합니다.",
            )
        return tuple(
            Candidate(agent_id=agent_id, owner=by_agent[agent_id]) for agent_id in sorted(by_agent)
        )

    @staticmethod
    def _project(request: QuestionRequest) -> QuestionOutcome:
        state = request.state
        if isinstance(state, AnsweredRequest):
            return RequestAnswered(
                request_id=request.request_id,
                record_id=state.record_id,
            )
        if isinstance(state, DeclinedRequest):
            return RequestDeclined(
                request_id=request.request_id,
                reason_code=state.reason_code,
                message=_DECLINED_MESSAGE,
            )
        if isinstance(state, FailedRequest):
            return RequestFailed(
                request_id=request.request_id,
                error_code=state.error_code,
                message=_FAILED_MESSAGE,
            )
        return RequestPending(
            request_id=request.request_id,
            state=state.kind,
            retryable=True,
            message=_PENDING_MESSAGE,
        )
