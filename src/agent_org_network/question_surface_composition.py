"""P17 사용자 표면을 하나의 Request/Finalization 경계로 묶는 조립 루트.

웹과 MCP 어댑터는 이 조립이 내놓는 ``QuestionStreamApplication``만 사용한다.
Question Request Store·Completion UoW·Completion Reader는 같은 원자 저장 객체다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Final, Literal, Protocol, Self, cast
from weakref import ReferenceType, ref

from agent_org_network.answer_finalization import (
    QuestionCompletionReader,
    QuestionCompletionUnitOfWork,
    ResponsibilitySnapshotResolver,
)
from agent_org_network.answer_record import CompletionAnswerRecordReader
from agent_org_network.approval import (
    ApprovalAuthorizer,
    ApprovalBoundary,
    ApprovalDeadlinePolicy,
    ApprovalExpiryPolicy,
    ApprovalPolicy,
    ApprovalReassignmentAuthorizer,
    ApprovalStore,
)
from agent_org_network.approval_operations import ApprovalOperationsApplication
from agent_org_network.approval_evidence import (
    ApprovalEventJournal,
    ApprovalEventReader,
    ApprovalEventRecorder,
)
from agent_org_network.approval_retention import ApprovalDraftRetentionPolicy
from agent_org_network.conflict import RequestAwareConflictCaseStore
from agent_org_network.grounding_terminal_failure import (
    QuestionRequestGroundingTerminalFailureRecorder,
)
from agent_org_network.knowledge_store import GroundingKnowledgeReader
from agent_org_network.manager_queue import ManagerQueueStore, RequestAwareManagerQueueStore
from agent_org_network.notify import Notifier
from agent_org_network.p17_conflict_disposition import (
    P17ConflictDispositionApplication,
    RequestAwareConflictEscalationManagerStore,
    RequestAwareConflictMediationStore,
)
from agent_org_network.p17_deadlock_manager_disposition import (
    P17DeadlockManagerDispositionApplication,
)
from agent_org_network.p17_manager_disposition import (
    P17ManagerDispositionApplication,
    RequestAwareDeadlockManagerDispositionStore,
    RequestAwareManagerDispositionStore,
    RequestScopedRouteAuthority,
)
from agent_org_network.question_request import QuestionRequestStore, RequestIdFactory
from agent_org_network.question_resolution import (
    CentralAuthorizer,
    HandlingDeadlinePolicy,
    QuestionResolutionApplication,
    RouteAuthority,
)
from agent_org_network.question_stream import InMemoryQuestionStreamBroker
from agent_org_network.question_stream_execution import (
    QuestionAnswerSource,
    QuestionProducerScheduler,
    QuestionStreamApplication,
    QuestionStreamExecutionService,
    ThreadedQuestionProducerScheduler,
)
from agent_org_network.router import RouterPort
from agent_org_network.registry import Registry
from agent_org_network.request_route_authority import RequestRouteAuthority
from agent_org_network.storage_capability import (
    NonDurableWorkflowCompositionError,
    validate_question_completion_storage,
    workflow_durability_of,
)


class QuestionSurfaceCompositionError(ValueError):
    """P17 사용자 표면 조립 계약 위반."""


class QuestionSurfaceLifecycleOwnershipError(QuestionSurfaceCompositionError):
    """Question Surface 자원 수명은 최초 조립된 exact 객체만 소유한다."""


class UnsupportedQuestionAnswerSourceError(QuestionSurfaceCompositionError):
    """완성 답을 호출 안에서 확정하지 못하는 실행 source를 거부한다."""


class QuestionCompletionDependencyIdentityError(QuestionSurfaceCompositionError):
    """ApprovalBoundary와 Finalization의 정책·Store·책임 원천이 서로 다름."""


class QuestionApprovalOperationsIdentityError(QuestionSurfaceCompositionError):
    """Approval 처리 application이 surface와 다른 상태·경계·배달 원천을 봄."""


class QuestionApprovalLifecycleConfigurationError(QuestionSurfaceCompositionError):
    """Approval lifecycle 정책·권한 구성의 callable capability가 불완전함."""


class QuestionApprovalEvidenceConfigurationError(QuestionSurfaceCompositionError):
    """Approval 사건·보존 구성이 부분 활성화되었거나 capability가 불완전함."""


class QuestionApprovalEvidenceIdentityError(QuestionSurfaceCompositionError):
    """Approval 사건·보존 경계가 서로 다른 의존성 인스턴스를 봄."""


class QuestionAnswerRecordReadCapabilityError(QuestionSurfaceCompositionError):
    """surface 감독 read view에 필요한 AnswerRecord 읽기 능력이 없음."""


class QuestionManagerDispositionCapabilityError(QuestionSurfaceCompositionError):
    """P17.4 Manager 처분을 안전하게 조립할 필수 포트가 불완전함."""


class QuestionManagerDispositionIdentityError(QuestionSurfaceCompositionError):
    """Manager 처분 application이 surface와 다른 의존성 인스턴스를 봄."""


class QuestionAnswerSourceDependencyIdentityError(QuestionSurfaceCompositionError):
    """Manager 재실행 source가 surface와 다른 Registry·Authority를 봄."""


class QuestionContestedSurfaceConfigurationError(QuestionSurfaceCompositionError):
    """P17.5 contested surface 구성이 부분 활성화되거나 모호함."""


class QuestionContestedSurfaceCapabilityError(QuestionSurfaceCompositionError):
    """P17.5 contested surface에 필요한 Store·Authority 포트가 불완전함."""


class QuestionContestedSurfaceIdentityError(QuestionSurfaceCompositionError):
    """P17.5 contested surface가 서로 다른 상태 원천을 봄."""


IdFactory = Callable[[], str]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class P17ContestedSurfaceConfiguration:
    """P17.4+P17.5를 한 번에 활성화하는 필수 구성 묶음."""

    registry: Registry
    grounding_knowledge_reader: GroundingKnowledgeReader
    root_user_id: str
    manager_item_id_factory: Callable[[], object]
    generation_factory: Callable[[], object]


@dataclass(frozen=True)
class ApprovalLifecycleConfiguration:
    """S3 expiry 정책과 manual 재지정 중앙 권한기를 원자적으로 활성화한다."""

    expiry_policy: ApprovalExpiryPolicy
    reassignment_authorizer: ApprovalReassignmentAuthorizer


@dataclass(frozen=True)
class ApprovalEvidenceConfiguration:
    """Approval 사건 journal·보존 정책·push를 원자적으로 활성화한다."""

    journal: ApprovalEventJournal
    retention_policy: ApprovalDraftRetentionPolicy
    notifier: Notifier | None = None


_PRODUCTION_CONTRACT_DEPENDENCY_FIELDS: Final = (
    "application",
    "storage",
    "approval_operations",
    "manager_store",
    "manager_disposition",
    "conflict_store",
    "conflict_disposition",
    "deadlock_manager_disposition",
    "registry",
    "route_authority",
    "grounding_knowledge_reader",
    "grounding_terminal_failure_recorder",
    "approval_events",
)
_SEALED_PRODUCTION_WIRING_MESSAGE: Final = "production composition wiring is sealed"
_LIFECYCLE_OWNERSHIP_MESSAGE: Final = "question surface lifecycle owner mismatch"


class _QuestionSurfaceLifecycleOwner:
    """Bind one resource lifecycle to one exact composition for its whole lifetime."""

    def __init__(self) -> None:
        self._owner_ref: ReferenceType[QuestionSurfaceComposition] | None = None
        self._bound_once = False
        self._closed = False
        self._lock = RLock()

    def bind(self, composition: QuestionSurfaceComposition) -> None:
        with self._lock:
            if self._bound_once:
                raise QuestionSurfaceLifecycleOwnershipError(_LIFECYCLE_OWNERSHIP_MESSAGE)
            self._owner_ref = ref(composition)
            self._bound_once = True

    def assert_owner(self, composition: QuestionSurfaceComposition) -> None:
        with self._lock:
            if (
                not self._bound_once
                or self._owner_ref is None
                or self._owner_ref() is not composition
            ):
                raise QuestionSurfaceLifecycleOwnershipError(_LIFECYCLE_OWNERSHIP_MESSAGE)

    def mark_closed(self, composition: QuestionSurfaceComposition) -> None:
        with self._lock:
            if (
                not self._bound_once
                or self._owner_ref is None
                or self._owner_ref() is not composition
            ):
                raise QuestionSurfaceLifecycleOwnershipError(_LIFECYCLE_OWNERSHIP_MESSAGE)
            self._closed = True

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        del memo
        return self


class _QuestionSurfaceProductionContractAttestation:
    """Bind one production contract claim to one exact completed composition."""

    def __init__(self, composition: QuestionSurfaceComposition) -> None:
        self._composition_ref: ReferenceType[QuestionSurfaceComposition] = ref(composition)
        self._dependency_snapshot = tuple(
            getattr(composition, name) for name in _PRODUCTION_CONTRACT_DEPENDENCY_FIELDS
        )
        self._state: Literal["issued", "claimed", "revoked"] = "issued"
        self._lock = RLock()

    def claim(self, value: object, *, closed: bool) -> bool:
        with self._lock:
            composition = self._composition_ref()
            if (
                composition is None
                or composition is not value
                or type(value) is not QuestionSurfaceComposition
            ):
                return False
            if self._state != "issued":
                return False
            try:
                identity_mismatch = any(
                    getattr(composition, name) is not expected
                    for name, expected in zip(
                        _PRODUCTION_CONTRACT_DEPENDENCY_FIELDS,
                        self._dependency_snapshot,
                        strict=True,
                    )
                )
            except Exception:
                self._state = "revoked"
                return False
            if closed or identity_mismatch:
                self._state = "revoked"
                return False
            self._state = "claimed"
            return True

    def revoke(self, value: object) -> None:
        with self._lock:
            if self._composition_ref() is value:
                self._state = "revoked"


class _QuestionSurfaceProductionAuthorityBinding:
    """실제 Question Resolution 경로에 결박된 중앙 권한 조립 증거.

    이 객체는 production composition root만 만들 수 있다. capability가 별도
    authorizer/resolver를 들고 있다는 사실만으로는 충분하지 않으며, application이
    실제로 같은 central authorizer를 통해 질문을 처리하는지도 확인한다.
    """

    def __init__(
        self,
        *,
        composition: QuestionSurfaceComposition,
        central_authorizer: CentralAuthorizer,
        identity_resolver: object,
        operational_authorization: object,
    ) -> None:
        self._composition_ref: ReferenceType[QuestionSurfaceComposition] = ref(composition)
        self._central_authorizer = central_authorizer
        self._identity_resolver = identity_resolver
        self._operational_authorization = operational_authorization

    def matches(
        self,
        composition: object,
        *,
        central_authorizer: object,
        identity_resolver: object,
        operational_authorization: object,
    ) -> bool:
        if (
            self._composition_ref() is not composition
            or type(composition) is not QuestionSurfaceComposition
            or self._central_authorizer is not central_authorizer
            or self._identity_resolver is not identity_resolver
            or self._operational_authorization is not operational_authorization
            or type(composition.application) is not QuestionStreamApplication
        ):
            return False
        try:
            resolution = composition.application._resolution  # pyright: ignore[reportPrivateUsage]
        except Exception:
            return False
        return bool(
            type(resolution) is QuestionResolutionApplication
            and getattr(resolution, "_central_authorizer", None) is central_authorizer
        )

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        del memo
        return self


class QuestionCompletionStorageFactory(Protocol):
    """같은 Approval·책임 의존성으로 원자 저장 객체 하나를 만드는 조립 포트."""

    def __call__(
        self,
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage: ...


class AtomicQuestionCompletionStorage(
    QuestionRequestStore,
    QuestionCompletionReader,
    CompletionAnswerRecordReader,
    Protocol,
):
    """P17 surface가 요구하는 한 객체짜리 Request/Completion 저장 shape."""

    @property
    def question_completion_storage_capability(self) -> Literal["atomic_v1"]: ...

    @property
    def workflow_durability(self) -> Literal["ephemeral", "durable"]: ...

    def matches_question_completion_dependencies(
        self,
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> bool: ...

    def complete(self, handoff: object) -> object: ...


def _validate_answer_source(source: object) -> QuestionAnswerSource:
    capability = getattr(source, "question_answer_source_capability", None)
    if capability != "completed_inline_v1" or not callable(getattr(source, "answer", None)):
        raise UnsupportedQuestionAnswerSourceError(
            "P17 단일 프로세스 사용자 표면에는 completed_inline_v1 "
            "Question Answer Source가 필요합니다."
        )
    return cast(QuestionAnswerSource, source)


@dataclass
class QuestionSurfaceComposition:
    """한 application과 그 application이 독점하는 원자 저장 객체의 수명."""

    application: QuestionStreamApplication
    storage: AtomicQuestionCompletionStorage
    approval_operations: ApprovalOperationsApplication
    manager_store: ManagerQueueStore | None = None
    manager_disposition: P17ManagerDispositionApplication | None = None
    conflict_store: RequestAwareConflictMediationStore | None = None
    conflict_disposition: P17ConflictDispositionApplication | None = None
    deadlock_manager_disposition: P17DeadlockManagerDispositionApplication | None = None
    registry: Registry | None = None
    route_authority: RequestRouteAuthority | None = None
    grounding_knowledge_reader: GroundingKnowledgeReader | None = None
    grounding_terminal_failure_recorder: QuestionRequestGroundingTerminalFailureRecorder | None = (
        None
    )
    approval_events: ApprovalEventReader | None = None
    _close_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _production_contract_attestation: _QuestionSurfaceProductionContractAttestation | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _production_authority_binding: _QuestionSurfaceProductionAuthorityBinding | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _production_authority_claim: object | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _production_contract_sealed: bool = field(default=False, init=False, repr=False, compare=False)
    _lifecycle_owner: _QuestionSurfaceLifecycleOwner = field(
        default_factory=_QuestionSurfaceLifecycleOwner,
        init=True,
        kw_only=True,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        self._lifecycle_owner.bind(self)

    def __setattr__(self, name: str, value: object) -> None:
        if name not in _PRODUCTION_CONTRACT_DEPENDENCY_FIELDS:
            object.__setattr__(self, name, value)
            return
        try:
            close_lock = object.__getattribute__(self, "_close_lock")
        except AttributeError:
            object.__setattr__(self, name, value)
            return
        with close_lock:
            if object.__getattribute__(self, "_production_contract_sealed"):
                raise AttributeError(_SEALED_PRODUCTION_WIRING_MESSAGE)
            object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name not in _PRODUCTION_CONTRACT_DEPENDENCY_FIELDS:
            object.__delattr__(self, name)
            return
        try:
            close_lock = object.__getattribute__(self, "_close_lock")
        except AttributeError:
            object.__delattr__(self, name)
            return
        with close_lock:
            if object.__getattribute__(self, "_production_contract_sealed"):
                raise AttributeError(_SEALED_PRODUCTION_WIRING_MESSAGE)
            object.__delattr__(self, name)

    @property
    def answer_records(self) -> CompletionAnswerRecordReader:
        """Finalization 저장소의 exact-link 검증을 유지한 감독용 읽기 손잡이."""
        return self.storage

    def close(self) -> None:
        """producer를 먼저 회수하고 저장 객체를 마지막에 멱등 종료한다."""
        with self._close_lock:
            self._lifecycle_owner.assert_owner(self)
            if self._closed:
                return
            authority_claim = self._production_authority_claim
            revoke = getattr(authority_claim, "revoke", None)
            if callable(revoke):
                try:
                    revoke()
                except Exception:
                    # 정리는 권한 capability 내부 오류 때문에 생략되지 않는다.
                    pass
            _revoke_question_surface_production_contract_attestation(self)
            failure: Exception | None = None
            try:
                self.application.shutdown(wait=True)
            except Exception as error:
                failure = error
            try:
                close_storage = getattr(self.storage, "close", None)
                if callable(close_storage):
                    close_storage()
            except Exception as error:
                if failure is None:
                    failure = error
                else:
                    failure.add_note(f"storage close도 실패했습니다: {type(error).__name__}")
            if failure is None:
                self._closed = True
                self._lifecycle_owner.mark_closed(self)
                return
            raise failure


def _issue_question_surface_production_contract_attestation(
    composition: QuestionSurfaceComposition,
) -> None:
    with composition._close_lock:  # pyright: ignore[reportPrivateUsage]
        object.__setattr__(
            composition,
            "_production_contract_attestation",
            _QuestionSurfaceProductionContractAttestation(composition),
        )
        object.__setattr__(composition, "_production_contract_sealed", True)


def _bind_question_surface_production_authority(
    composition: QuestionSurfaceComposition,
    *,
    central_authorizer: CentralAuthorizer,
    identity_resolver: object,
    operational_authorization: object,
) -> bool:
    """Factory가 실제 resolution path와 중앙 권한 graph를 함께 seal한다."""
    with composition._close_lock:  # pyright: ignore[reportPrivateUsage]
        if (
            composition._closed  # pyright: ignore[reportPrivateUsage]
            or composition._production_contract_attestation is None  # pyright: ignore[reportPrivateUsage]
            or composition._production_authority_binding is not None  # pyright: ignore[reportPrivateUsage]
            or identity_resolver is None
            or operational_authorization is None
            or type(composition.application) is not QuestionStreamApplication
        ):
            return False
        try:
            resolution = composition.application._resolution  # pyright: ignore[reportPrivateUsage]
        except Exception:
            return False
        if (
            type(resolution) is not QuestionResolutionApplication
            or getattr(resolution, "_central_authorizer", None) is not central_authorizer
        ):
            return False
        object.__setattr__(
            composition,
            "_production_authority_binding",
            _QuestionSurfaceProductionAuthorityBinding(
                composition=composition,
                central_authorizer=central_authorizer,
                identity_resolver=identity_resolver,
                operational_authorization=operational_authorization,
            ),
        )
        return True


def _question_surface_matches_production_authority(  # pyright: ignore[reportUnusedFunction]
    composition: object,
    *,
    central_authorizer: object,
    identity_resolver: object,
    operational_authorization: object,
) -> bool:
    if type(composition) is not QuestionSurfaceComposition:
        return False
    assert isinstance(composition, QuestionSurfaceComposition)
    with composition._close_lock:  # pyright: ignore[reportPrivateUsage]
        binding = composition._production_authority_binding  # pyright: ignore[reportPrivateUsage]
        return bool(
            not composition._closed  # pyright: ignore[reportPrivateUsage]
            and type(binding) is _QuestionSurfaceProductionAuthorityBinding
            and binding.matches(
                composition,
                central_authorizer=central_authorizer,
                identity_resolver=identity_resolver,
                operational_authorization=operational_authorization,
            )
        )


def _register_question_surface_production_authority_claim(  # pyright: ignore[reportUnusedFunction]
    composition: QuestionSurfaceComposition,
    claim: object,
) -> bool:
    """claim/close를 QSC lifecycle lock 하나로 직렬화한다."""
    with composition._close_lock:  # pyright: ignore[reportPrivateUsage]
        if composition._closed or composition._production_authority_claim is not None:  # pyright: ignore[reportPrivateUsage]
            return False
        object.__setattr__(composition, "_production_authority_claim", claim)
        return True


def _revoke_question_surface_production_contract_attestation(
    composition: QuestionSurfaceComposition,
) -> None:
    with composition._close_lock:  # pyright: ignore[reportPrivateUsage]
        attestation = composition._production_contract_attestation  # pyright: ignore[reportPrivateUsage]
        if attestation is not None:
            attestation.revoke(composition)


def _claim_question_surface_production_contract_attestation(  # pyright: ignore[reportUnusedFunction]
    value: object,
) -> bool:
    if type(value) is not QuestionSurfaceComposition:
        return False
    assert isinstance(value, QuestionSurfaceComposition)
    with value._close_lock:  # pyright: ignore[reportPrivateUsage]
        attestation = value._production_contract_attestation  # pyright: ignore[reportPrivateUsage]
        return attestation is not None and attestation.claim(
            value,
            closed=value._closed,  # pyright: ignore[reportPrivateUsage]
        )


def _validate_completion_dependency_identity(
    storage: AtomicQuestionCompletionStorage,
    *,
    policy: ApprovalPolicy,
    approvals: ApprovalStore,
    responsibility_resolver: ResponsibilitySnapshotResolver,
) -> None:
    matcher = getattr(storage, "matches_question_completion_dependencies", None)
    try:
        matches = (
            matcher(
                policy=policy,
                approvals=approvals,
                responsibility_resolver=responsibility_resolver,
            )
            if callable(matcher)
            else False
        )
    except Exception as error:
        raise QuestionCompletionDependencyIdentityError(
            "Question Completion dependency identity를 검증할 수 없습니다."
        ) from error
    if matches is not True:
        raise QuestionCompletionDependencyIdentityError(
            "ApprovalBoundary와 Finalization은 같은 ApprovalPolicy·ApprovalStore·"
            "책임 resolver 인스턴스를 사용해야 합니다."
        )


def _validate_approval_operations_identity(
    operations: ApprovalOperationsApplication,
    boundary: ApprovalBoundary,
    *,
    requests: QuestionRequestStore,
    approvals: ApprovalStore,
    policy: ApprovalPolicy,
    authorizer: ApprovalAuthorizer,
    completion: QuestionCompletionUnitOfWork,
    reader: QuestionCompletionReader,
    terminal_publisher: QuestionStreamApplication,
    lifecycle_configuration: ApprovalLifecycleConfiguration | None,
    item_id_factory: IdFactory,
    clock: Clock,
) -> None:
    try:
        boundary_matches = boundary.matches_dependencies(
            requests=requests,
            approvals=approvals,
            policy=policy,
            authorizer=authorizer,
        )
        operations_match = operations.matches_dependencies(
            requests=requests,
            approvals=approvals,
            boundary=boundary,
            completion=completion,
            reader=reader,
            terminal_publisher=terminal_publisher,
        )
        lifecycle_matches = (
            operations.matches_lifecycle_dependencies(
                expiry_policy=lifecycle_configuration.expiry_policy,
                reassignment_authorizer=(lifecycle_configuration.reassignment_authorizer),
                item_id_factory=item_id_factory,
                clock=clock,
                reader=reader,
                terminal_publisher=terminal_publisher,
            )
            if lifecycle_configuration is not None
            else True
        )
    except Exception as error:
        raise QuestionApprovalOperationsIdentityError(
            "Approval operations dependency identity를 검증할 수 없습니다."
        ) from error
    if (
        boundary_matches is not True
        or operations_match is not True
        or lifecycle_matches is not True
    ):
        raise QuestionApprovalOperationsIdentityError(
            "Approval operations는 surface와 같은 Request/Completion 저장 객체, "
            "Approval Store·정책·권한 경계·ID·clock·terminal publisher를 사용해야 합니다."
        )


def _validate_approval_lifecycle_configuration(
    configuration: ApprovalLifecycleConfiguration | None,
) -> None:
    if configuration is None:
        return
    try:
        missing = tuple(
            name
            for name, dependency, method in (
                ("expiry_policy.evaluate", configuration.expiry_policy, "evaluate"),
                (
                    "reassignment_authorizer.authorize",
                    configuration.reassignment_authorizer,
                    "authorize",
                ),
            )
            if not callable(getattr(dependency, method, None))
        )
    except Exception as error:
        raise QuestionApprovalLifecycleConfigurationError(
            "Approval lifecycle callable을 검증할 수 없습니다."
        ) from error
    if missing:
        raise QuestionApprovalLifecycleConfigurationError(
            "Approval lifecycle 필수 callable이 없습니다: " + ", ".join(missing)
        )


def _validate_approval_evidence_configuration(
    configuration: ApprovalEvidenceConfiguration | None,
) -> None:
    if configuration is None:
        return
    if type(configuration) is not ApprovalEvidenceConfiguration:
        raise QuestionApprovalEvidenceConfigurationError(
            "Approval evidence 구성은 exact ApprovalEvidenceConfiguration이어야 합니다."
        )
    try:
        required = (
            ("journal.append_batch_once", configuration.journal, "append_batch_once"),
            ("journal.append_once", configuration.journal, "append_once"),
            ("journal.get", configuration.journal, "get"),
            ("journal.for_request", configuration.journal, "for_request"),
            ("retention_policy.evaluate", configuration.retention_policy, "evaluate"),
        )
        if configuration.notifier is not None:
            required += (("notifier.notify", configuration.notifier, "notify"),)
        missing = tuple(
            name
            for name, dependency, method in required
            if not callable(getattr(dependency, method, None))
        )
    except Exception as error:
        raise QuestionApprovalEvidenceConfigurationError(
            "Approval evidence callable을 검증할 수 없습니다."
        ) from error
    if missing:
        raise QuestionApprovalEvidenceConfigurationError(
            "Approval evidence 필수 callable이 없습니다: " + ", ".join(missing)
        )


def _validate_approval_evidence_identity(
    *,
    configuration: ApprovalEvidenceConfiguration,
    recorder: ApprovalEventRecorder,
    boundary: ApprovalBoundary,
    operations: ApprovalOperationsApplication,
    reader: QuestionCompletionReader,
) -> None:
    try:
        matches = (
            recorder.matches_journal(configuration.journal)
            and boundary.matches_evidence_dependencies(
                evidence_recorder=recorder,
                notifier=configuration.notifier,
            )
            and operations.matches_evidence_dependencies(
                evidence_recorder=recorder,
                notifier=configuration.notifier,
            )
            and operations.matches_retention_dependencies(
                retention_policy=configuration.retention_policy,
                reader=reader,
                evidence_recorder=recorder,
            )
        )
    except Exception as error:
        raise QuestionApprovalEvidenceIdentityError(
            "Approval evidence dependency identity를 검증할 수 없습니다."
        ) from error
    if matches is not True:
        raise QuestionApprovalEvidenceIdentityError(
            "ApprovalBoundary·Operations·Retention은 같은 recorder·journal·"
            "notifier·policy·terminal reader 인스턴스를 사용해야 합니다."
        )


def _validate_answer_record_reader(storage: object) -> None:
    missing = tuple(
        name
        for name in ("answer_record", "answer_records_for_agent")
        if not callable(getattr(storage, name, None))
    )
    if missing:
        raise QuestionAnswerRecordReadCapabilityError(
            "P17 surface AnswerRecord 읽기 필수 callable이 없습니다: " + ", ".join(missing)
        )


def _validate_manager_disposition_capabilities(
    *,
    managers: object,
    route_authority: object,
) -> tuple[RequestAwareManagerDispositionStore, RequestScopedRouteAuthority]:
    if not isinstance(managers, RequestAwareManagerDispositionStore):
        raise QuestionManagerDispositionCapabilityError(
            "P17.4 Manager Store에 request-aware claim·evidence·resolve 능력이 필요합니다."
        )
    missing_authority = tuple(
        name
        for name in ("authorize", "authorize_for_request", "assign_owner")
        if not callable(getattr(route_authority, name, None))
    )
    if missing_authority:
        raise QuestionManagerDispositionCapabilityError(
            "P17.4 중앙 Authority 필수 callable이 없습니다: " + ", ".join(missing_authority)
        )
    return managers, cast(RequestScopedRouteAuthority, route_authority)


def _manager_queue_view(managers: object) -> ManagerQueueStore | None:
    required = ("enqueue", "get", "pending_for_manager", "get_by_case", "mark_resolved")
    if all(callable(getattr(managers, name, None)) for name in required):
        return cast(ManagerQueueStore, managers)
    return None


def _validate_answer_source_dependency_identity(
    source: object,
    *,
    registry: Registry,
    route_authority: RouteAuthority,
) -> None:
    matcher = getattr(source, "matches_question_answer_dependencies", None)
    try:
        matches = (
            matcher(registry=registry, route_authority=route_authority)
            if callable(matcher)
            else False
        )
    except Exception as error:
        raise QuestionAnswerSourceDependencyIdentityError(
            "Question Answer Source dependency identity를 검증할 수 없습니다."
        ) from error
    if matches is not True:
        raise QuestionAnswerSourceDependencyIdentityError(
            "Manager 재실행 source는 같은 Registry·Route Authority 인스턴스를 사용해야 합니다."
        )


def _validate_contested_capabilities(
    *,
    conflicts: object,
    managers: object,
    route_authority: object,
    grounding_knowledge_reader: object,
) -> tuple[
    RequestAwareConflictMediationStore,
    RequestAwareConflictEscalationManagerStore,
    RequestAwareDeadlockManagerDispositionStore,
    RequestRouteAuthority,
]:
    conflict_methods = (
        "create_or_get_for_request",
        "get_request_case",
        "reserve_validated_concurrence",
        "claim_for_case",
        "validate_consensus_reservation",
        "sealed_claim_for_case",
        "validate_sealed_claim",
        "progress_history_for_case",
        "seal_consensus_claim",
        "abandon_unmutated_consensus_round",
        "record_resolution_evidence",
        "resolution_evidence_for_request",
        "transition_for_claim",
        "record_validated_mediation",
        "transition_for_mediation",
    )
    missing_conflicts = tuple(
        name for name in conflict_methods if not callable(getattr(conflicts, name, None))
    )
    if missing_conflicts:
        raise QuestionContestedSurfaceCapabilityError(
            "P17.5 Conflict Store 필수 callable이 없습니다: " + ", ".join(missing_conflicts)
        )
    if not isinstance(managers, RequestAwareManagerDispositionStore) or not isinstance(
        managers, RequestAwareDeadlockManagerDispositionStore
    ):
        raise QuestionContestedSurfaceCapabilityError(
            "P17.5 Manager Store에 Unowned·Deadlock claim/resolve 능력이 모두 필요합니다."
        )
    missing_authority = tuple(
        name
        for name in ("authorize", "authorize_for_request", "assign_owner", "grant_for_request")
        if not callable(getattr(route_authority, name, None))
    )
    if missing_authority:
        raise QuestionContestedSurfaceCapabilityError(
            "P17.5 중앙 Authority 필수 callable이 없습니다: " + ", ".join(missing_authority)
        )
    if not callable(getattr(grounding_knowledge_reader, "read", None)):
        raise QuestionContestedSurfaceCapabilityError(
            "P17.5 GroundingKnowledgeReader.read callable이 필요합니다."
        )
    return (
        cast(RequestAwareConflictMediationStore, conflicts),
        cast(RequestAwareConflictEscalationManagerStore, managers),
        cast(RequestAwareDeadlockManagerDispositionStore, managers),
        cast(RequestRouteAuthority, route_authority),
    )


def _validate_contested_identity(
    source: object,
    responsibility_resolver: object,
    *,
    registry: Registry,
    route_authority: RequestRouteAuthority,
    conflicts: RequestAwareConflictMediationStore,
    grounding_knowledge_reader: GroundingKnowledgeReader,
) -> None:
    source_matcher = getattr(source, "matches_contested_question_answer_dependencies", None)
    resolver_matcher = getattr(responsibility_resolver, "matches_registry", None)
    try:
        source_matches = (
            source_matcher(
                registry=registry,
                route_authority=route_authority,
                conflict_resolution_evidence_reader=conflicts,
                grounding_knowledge_reader=grounding_knowledge_reader,
            )
            if callable(source_matcher)
            else False
        )
        resolver_matches = (
            resolver_matcher(registry=registry) if callable(resolver_matcher) else False
        )
    except Exception as error:
        raise QuestionContestedSurfaceIdentityError(
            "P17.5 dependency identity를 검증할 수 없습니다."
        ) from error
    if source_matches is not True or resolver_matches is not True:
        raise QuestionContestedSurfaceIdentityError(
            "P17.5 Answer Source·책임 resolver는 composition과 같은 상태 원천을 사용해야 합니다."
        )


def _best_effort_close_storage(storage: object) -> None:
    close_storage = getattr(storage, "close", None)
    if not callable(close_storage):
        return
    try:
        close_storage()
    except Exception:
        return


def _validate_production_linked_store_durability(
    *,
    conflicts: object,
    managers: object,
    approvals: object,
) -> None:
    """Reject ephemeral linked writers before acquiring completion storage."""
    for label, component in (
        ("Conflict", conflicts),
        ("Manager", managers),
        ("Approval", approvals),
    ):
        if workflow_durability_of(component) != "durable":
            raise NonDurableWorkflowCompositionError(
                f"production-style Question Surface에는 durable {label} Store가 필요합니다."
            )


def build_question_surface_composition(
    *,
    storage_factory: QuestionCompletionStorageFactory,
    router: RouterPort,
    conflicts: RequestAwareConflictCaseStore,
    managers: RequestAwareManagerQueueStore,
    route_authority: RouteAuthority,
    handling_deadline_policy: HandlingDeadlinePolicy,
    approval_store: ApprovalStore,
    approval_policy: ApprovalPolicy,
    approval_authorizer: ApprovalAuthorizer,
    approval_deadline_policy: ApprovalDeadlinePolicy,
    responsibility_resolver: ResponsibilitySnapshotResolver,
    answer_source: object,
    request_id_factory: RequestIdFactory,
    draft_id_factory: IdFactory,
    approval_item_id_factory: IdFactory,
    clock: Clock,
    approval_lifecycle_configuration: ApprovalLifecycleConfiguration | None = None,
    approval_evidence_configuration: ApprovalEvidenceConfiguration | None = None,
    manager_registry: Registry | None = None,
    manager_generation_factory: Callable[[], object] | None = None,
    contested_configuration: P17ContestedSurfaceConfiguration | None = None,
    scheduler: QuestionProducerScheduler | None = None,
    max_preview_bytes: int = 65_536,
    broker_max_queue_size: int = 64,
    production_style: bool = False,
    central_authorizer: CentralAuthorizer | None = None,
    production_identity_resolver: object | None = None,
    operational_authorization: object | None = None,
) -> QuestionSurfaceComposition:
    """단일 프로세스 파일럿용 P17 사용자 표면을 fail-closed로 조립한다.

    SQLite migration이나 production 설정 선택은 이 함수의 책임이 아니다. 호출자는
    이미 설치·검증 가능한 저장 객체 하나를 넘기며, 이 함수는 그 한 객체를 모든
    Request/Completion 읽기·쓰기 자리에 그대로 배선한다.
    """

    source = _validate_answer_source(answer_source)
    _validate_approval_lifecycle_configuration(approval_lifecycle_configuration)
    _validate_approval_evidence_configuration(approval_evidence_configuration)
    if production_style:
        _validate_production_linked_store_durability(
            conflicts=conflicts,
            managers=managers,
            approvals=approval_store,
        )
    if contested_configuration is not None and (
        manager_registry is not None or manager_generation_factory is not None
    ):
        raise QuestionContestedSurfaceConfigurationError(
            "P17.5 구성과 legacy P17.4 Manager 인자를 함께 지정할 수 없습니다."
        )
    manager_enabled = (
        contested_configuration is not None
        or manager_registry is not None
        and manager_generation_factory is not None
    )
    if (manager_registry is None) != (manager_generation_factory is None):
        raise QuestionManagerDispositionCapabilityError(
            "P17.4 Manager 처분은 Registry와 generation factory를 함께 지정해야 합니다."
        )
    effective_registry = (
        contested_configuration.registry
        if contested_configuration is not None
        else manager_registry
    )
    effective_generation_factory = (
        contested_configuration.generation_factory
        if contested_configuration is not None
        else manager_generation_factory
    )
    manager_dependencies: (
        tuple[RequestAwareManagerDispositionStore, RequestScopedRouteAuthority] | None
    ) = None
    contested_dependencies: (
        tuple[
            RequestAwareConflictMediationStore,
            RequestAwareConflictEscalationManagerStore,
            RequestAwareDeadlockManagerDispositionStore,
            RequestRouteAuthority,
        ]
        | None
    ) = None
    manager_store = _manager_queue_view(managers)
    if manager_enabled:
        assert effective_registry is not None
        assert effective_generation_factory is not None
        manager_dependencies = _validate_manager_disposition_capabilities(
            managers=managers,
            route_authority=route_authority,
        )
        _validate_answer_source_dependency_identity(
            answer_source,
            registry=effective_registry,
            route_authority=route_authority,
        )
    if contested_configuration is not None:
        if not contested_configuration.root_user_id.strip():
            raise QuestionContestedSurfaceConfigurationError(
                "P17.5 root User ID는 비어 있을 수 없습니다."
            )
        try:
            contested_configuration.registry.get_user(contested_configuration.root_user_id)
        except KeyError as error:
            raise QuestionContestedSurfaceConfigurationError(
                "P17.5 root User가 Registry에 없습니다."
            ) from error
        contested_dependencies = _validate_contested_capabilities(
            conflicts=conflicts,
            managers=managers,
            route_authority=route_authority,
            grounding_knowledge_reader=(contested_configuration.grounding_knowledge_reader),
        )
        conflict_store, _, _, request_route_authority = contested_dependencies
        _validate_contested_identity(
            answer_source,
            responsibility_resolver,
            registry=contested_configuration.registry,
            route_authority=request_route_authority,
            conflicts=conflict_store,
            grounding_knowledge_reader=(contested_configuration.grounding_knowledge_reader),
        )
    storage = storage_factory(
        policy=approval_policy,
        approvals=approval_store,
        responsibility_resolver=responsibility_resolver,
    )
    producer: QuestionProducerScheduler | None = None
    try:
        validate_question_completion_storage(
            requests=storage,
            completion_uow=storage,
            completion_reader=storage,
            require_durable=production_style,
        )
        _validate_answer_record_reader(storage)
        _validate_completion_dependency_identity(
            storage,
            policy=approval_policy,
            approvals=approval_store,
            responsibility_resolver=responsibility_resolver,
        )
        requests = cast(QuestionRequestStore, storage)
        completion = cast(QuestionCompletionUnitOfWork, storage)
        reader = cast(QuestionCompletionReader, storage)
        terminal_failure_recorder: QuestionRequestGroundingTerminalFailureRecorder | None = None
        if contested_configuration is not None:
            terminal_failure_recorder = QuestionRequestGroundingTerminalFailureRecorder(
                requests=requests,
                clock=clock,
            )
            if not terminal_failure_recorder.matches_request_store(requests):
                raise QuestionContestedSurfaceIdentityError(
                    "Grounding terminal recorder가 surface와 다른 Request Store를 봅니다."
                )

        resolution = QuestionResolutionApplication(
            requests=requests,
            router=router,
            conflicts=conflicts,
            managers=managers,
            route_authority=route_authority,
            deadline_policy=handling_deadline_policy,
            request_id_factory=request_id_factory,
            clock=clock,
            production_style=production_style,
            central_authorizer=central_authorizer,
        )
        evidence_recorder = (
            ApprovalEventRecorder(approval_evidence_configuration.journal)
            if approval_evidence_configuration is not None
            else None
        )
        approval = ApprovalBoundary(
            requests=requests,
            approvals=approval_store,
            policy=approval_policy,
            authorizer=approval_authorizer,
            deadline_policy=approval_deadline_policy,
            draft_id_factory=draft_id_factory,
            item_id_factory=approval_item_id_factory,
            clock=clock,
            production_style=True,
            evidence_recorder=evidence_recorder,
            notifier=(
                approval_evidence_configuration.notifier
                if approval_evidence_configuration is not None
                else None
            ),
        )
        execution = QuestionStreamExecutionService(
            requests=requests,
            resolution=resolution,
            source=source,
            approval=approval,
            completion=completion,
            reader=reader,
            grounding_terminal_failure_recorder=terminal_failure_recorder,
            max_preview_bytes=max_preview_bytes,
            production_style=production_style,
        )
        broker = InMemoryQuestionStreamBroker(
            max_queue_size=broker_max_queue_size,
            requests=requests,
            completions=reader,
        )
        producer = scheduler if scheduler is not None else ThreadedQuestionProducerScheduler()
        application = QuestionStreamApplication(
            resolution=resolution,
            execution=execution,
            broker=broker,
            scheduler=producer,
        )
        approval_operations = ApprovalOperationsApplication(
            requests=requests,
            approvals=approval_store,
            boundary=approval,
            completion=completion,
            reader=reader,
            terminal_publisher=application,
            expiry_policy=(
                approval_lifecycle_configuration.expiry_policy
                if approval_lifecycle_configuration is not None
                else None
            ),
            reassignment_authorizer=(
                approval_lifecycle_configuration.reassignment_authorizer
                if approval_lifecycle_configuration is not None
                else None
            ),
            item_id_factory=(
                approval_item_id_factory if approval_lifecycle_configuration is not None else None
            ),
            clock=clock if approval_lifecycle_configuration is not None else None,
            evidence_recorder=evidence_recorder,
            notifier=(
                approval_evidence_configuration.notifier
                if approval_evidence_configuration is not None
                else None
            ),
            retention_policy=(
                approval_evidence_configuration.retention_policy
                if approval_evidence_configuration is not None
                else None
            ),
        )
        _validate_approval_operations_identity(
            approval_operations,
            approval,
            requests=requests,
            approvals=approval_store,
            policy=approval_policy,
            authorizer=approval_authorizer,
            completion=completion,
            reader=reader,
            terminal_publisher=application,
            lifecycle_configuration=approval_lifecycle_configuration,
            item_id_factory=approval_item_id_factory,
            clock=clock,
        )
        if approval_evidence_configuration is not None:
            assert evidence_recorder is not None
            _validate_approval_evidence_identity(
                configuration=approval_evidence_configuration,
                recorder=evidence_recorder,
                boundary=approval,
                operations=approval_operations,
                reader=reader,
            )
        manager_application: P17ManagerDispositionApplication | None = None
        if manager_dependencies is not None:
            assert effective_registry is not None
            assert effective_generation_factory is not None
            disposition_store, request_route_authority = manager_dependencies
            manager_application = P17ManagerDispositionApplication(
                requests=requests,
                managers=disposition_store,
                registry=effective_registry,
                route_authority=request_route_authority,
                completion_reader=reader,
                deadline_policy=handling_deadline_policy,
                execution_starter=application,
                terminal_publisher=application,
                generation_factory=effective_generation_factory,
                clock=clock,
            )
            if not manager_application.matches_dependencies(
                requests=requests,
                managers=disposition_store,
                registry=effective_registry,
                route_authority=request_route_authority,
                completion_reader=reader,
                execution_starter=application,
                terminal_publisher=application,
            ):
                raise QuestionManagerDispositionIdentityError(
                    "Manager 처분 application dependency identity가 surface 조립과 다릅니다."
                )
            manager_store = disposition_store
        conflict_application: P17ConflictDispositionApplication | None = None
        deadlock_application: P17DeadlockManagerDispositionApplication | None = None
        conflict_store_handle: RequestAwareConflictMediationStore | None = None
        request_route_authority_handle: RequestRouteAuthority | None = None
        if contested_dependencies is not None:
            assert contested_configuration is not None
            (
                conflict_store,
                conflict_manager_store,
                deadlock_manager_store,
                request_route_authority,
            ) = contested_dependencies
            conflict_application = P17ConflictDispositionApplication(
                requests=requests,
                conflicts=conflict_store,
                managers=conflict_manager_store,
                registry=contested_configuration.registry,
                route_authority=request_route_authority,
                completion_reader=reader,
                deadline_policy=handling_deadline_policy,
                execution_starter=application,
                clock=clock,
                root_user_id=contested_configuration.root_user_id,
                manager_item_id_factory=(contested_configuration.manager_item_id_factory),
            )
            if not conflict_application.matches_dependencies(
                requests=requests,
                conflicts=conflict_store,
                managers=conflict_manager_store,
                registry=contested_configuration.registry,
                route_authority=request_route_authority,
                completion_reader=reader,
                execution_starter=application,
            ):
                raise QuestionContestedSurfaceIdentityError(
                    "Conflict disposition이 surface와 다른 의존성을 봅니다."
                )
            deadlock_application = P17DeadlockManagerDispositionApplication(
                requests=requests,
                conflicts=conflict_store,
                managers=deadlock_manager_store,
                registry=contested_configuration.registry,
                route_authority=request_route_authority,
                completion_reader=reader,
                deadline_policy=handling_deadline_policy,
                execution_starter=application,
                terminal_publisher=application,
                generation_factory=contested_configuration.generation_factory,
                clock=clock,
            )
            if not deadlock_application.matches_dependencies(
                requests=requests,
                conflicts=conflict_store,
                managers=deadlock_manager_store,
                registry=contested_configuration.registry,
                route_authority=request_route_authority,
                completion_reader=reader,
                execution_starter=application,
                terminal_publisher=application,
            ):
                raise QuestionContestedSurfaceIdentityError(
                    "Deadlock Manager disposition이 surface와 다른 의존성을 봅니다."
                )
            conflict_store_handle = conflict_store
            request_route_authority_handle = request_route_authority
        composition = QuestionSurfaceComposition(
            application=application,
            storage=storage,
            approval_operations=approval_operations,
            approval_events=(
                approval_evidence_configuration.journal
                if approval_evidence_configuration is not None
                else None
            ),
            manager_store=manager_store,
            manager_disposition=manager_application,
            conflict_store=conflict_store_handle,
            conflict_disposition=conflict_application,
            deadlock_manager_disposition=deadlock_application,
            registry=(
                contested_configuration.registry if contested_configuration is not None else None
            ),
            route_authority=request_route_authority_handle,
            grounding_knowledge_reader=(
                contested_configuration.grounding_knowledge_reader
                if contested_configuration is not None
                else None
            ),
            grounding_terminal_failure_recorder=terminal_failure_recorder,
        )
        if production_style:
            _issue_question_surface_production_contract_attestation(composition)
            authority_inputs = (
                central_authorizer,
                production_identity_resolver,
                operational_authorization,
            )
            if any(value is not None for value in authority_inputs) and not all(
                value is not None for value in authority_inputs
            ):
                raise QuestionSurfaceCompositionError(
                    "production central authority는 authorizer·identity resolver·operational boundary를 함께 요구합니다."
                )
            if all(value is not None for value in authority_inputs):
                assert central_authorizer is not None
                assert production_identity_resolver is not None
                assert operational_authorization is not None
                if not _bind_question_surface_production_authority(
                    composition,
                    central_authorizer=central_authorizer,
                    identity_resolver=production_identity_resolver,
                    operational_authorization=operational_authorization,
                ):
                    raise QuestionSurfaceCompositionError(
                        "production central authority를 Question Resolution에 결박할 수 없습니다."
                    )
        return composition
    except Exception:
        if producer is not None:
            try:
                producer.shutdown(wait=True)
            except Exception:
                pass
        _best_effort_close_storage(storage)
        raise
