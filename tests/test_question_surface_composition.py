from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import Literal, cast, get_type_hints

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    InMemoryQuestionCompletionUnitOfWork,
    QuestionCompletionReader,
    ResponsibilitySnapshotResolver,
)
from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.approval import (
    ApprovalAuthorizer,
    ApprovalAuthorization,
    ApprovalConfigurationError,
    ApprovalExpiryPolicy,
    ApprovalPolicy,
    ApprovalReassignmentAuthorizer,
    ApprovalRequired,
    ApprovalStore,
    ApproverPrincipal,
    AnswerCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.approval_operations import ApproveIntent, ApprovalOperationsApplication
from agent_org_network.approval_evidence import (
    ApprovalEvent,
    ApprovalEventRecorder,
    InMemoryApprovalEventJournal,
)
from agent_org_network.approval_retention import (
    ApprovalDraftRetentionDecision,
    ApprovalDraftRetentionPolicy,
    ApprovalDraftTerminalEvidence,
)
from agent_org_network.conflict import InMemoryConflictCaseStore
from agent_org_network.knowledge_store import (
    GroundingKnowledgeMissing,
    GroundingKnowledgeReader,
)
from agent_org_network.notify import Notification, Notifier
from agent_org_network.decision import Routed
from agent_org_network.manager_queue import InMemoryManagerQueueStore
from agent_org_network.p17_conflict_disposition import InMemoryConflictDispositionStore
from agent_org_network.p17_manager_disposition import (
    AuthorityAssignment,
    AuthorityAssignmentReceipt,
    QuestionTerminalPublisher,
)
from agent_org_network.production_bootstrap import (
    ProductionBootstrapConfig,
    ProductionBootstrapHandle,
    ProductionCompositionRejected,
    ProductionDependencies,
    bootstrap_production,
)
from agent_org_network.request_route_authority import (
    RequestRouteGrantAssignment,
    RequestRouteGrantReceipt,
)
from agent_org_network.question_resolution import (
    AskQuestion,
    AuthorityGrant,
    RequesterPrincipal,
)
from agent_org_network.question_stream_execution import (
    AnsweredQuestionLookup,
    BufferedAnswer,
    QuestionProducerScheduler,
)
from agent_org_network.question_surface_composition import (
    ApprovalEvidenceConfiguration,
    ApprovalLifecycleConfiguration,
    AtomicQuestionCompletionStorage,
    QuestionAnswerRecordReadCapabilityError,
    QuestionAnswerSourceDependencyIdentityError,
    QuestionApprovalLifecycleConfigurationError,
    QuestionApprovalEvidenceConfigurationError,
    QuestionApprovalEvidenceIdentityError,
    QuestionApprovalOperationsIdentityError,
    QuestionCompletionDependencyIdentityError,
    QuestionContestedSurfaceCapabilityError,
    QuestionManagerDispositionCapabilityError,
    P17ContestedSurfaceConfiguration,
    QuestionContestedSurfaceConfigurationError,
    QuestionSurfaceComposition,
    UnsupportedQuestionAnswerSourceError,
    build_question_surface_composition,
)
from agent_org_network.registry import Registry
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.storage_capability import (
    NonDurableWorkflowCompositionError,
    QuestionCompletionStorageCapabilityError,
    UnknownWorkflowDurabilityError,
)
from agent_org_network.user import User

NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


def test_approval_store_protocol_declares_workflow_durability() -> None:
    assert get_type_hints(ApprovalStore)["workflow_durability"] == Literal["ephemeral", "durable"]


def _card() -> AgentCard:
    return AgentCard(
        agent_id="refund-card",
        owner="owner-1",
        team="support",
        summary="환불 문의",
        domains=["refund"],
        last_reviewed_at=NOW.date(),
        knowledge_sources=["refund.md"],
    )


class _Router:
    def route(self, question: str) -> Routed:
        return Routed(primary=_card(), intent="refund")


class _Authority:
    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant:
        return AuthorityGrant(policy_version="authority-v1")


class _ManagerAuthority(_Authority):
    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant:
        return AuthorityGrant(policy_version="request-authority-v1")

    def assign_owner(self, assignment: AuthorityAssignment) -> AuthorityAssignmentReceipt:
        return AuthorityAssignmentReceipt(
            assignment=assignment,
            grant_version="request-authority-v1",
        )

    def grant_for_request(
        self, assignment: RequestRouteGrantAssignment
    ) -> RequestRouteGrantReceipt:
        return RequestRouteGrantReceipt(
            assignment=assignment,
            grant_version="request-authority-v1",
        )


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        return started_at + timedelta(minutes=5)


class _Policy:
    def evaluate(
        self,
        org_id: str,
        route: object,
        candidate_mode: str,
    ) -> NoApprovalRequired | ApprovalRequired:
        return NoApprovalRequired(policy_version="approval-v1")


class _Authorizer:
    def authorize(
        self,
        org_id: str,
        designated_approver_id: str,
        actor_id: str,
        action_kind: str,
        policy_version: str,
    ) -> ApprovalAuthorization:
        return ApprovalAuthorization(policy_version=policy_version)


class _LifecycleExpiryPolicy:
    def evaluate(self, *, assignment: object, now: datetime) -> object:
        raise AssertionError(f"empty Store에서는 호출되지 않습니다: {assignment!r}, {now!r}")


class _LifecycleReassignmentAuthorizer:
    def authorize(
        self,
        *,
        assignment: object,
        principal: object,
        target_approver_id: str,
        requested_at: datetime,
    ) -> object:
        raise AssertionError(
            "empty Store에서는 호출되지 않습니다: "
            f"{assignment!r}, {principal!r}, {target_approver_id!r}, {requested_at!r}"
        )


class _Resolver:
    def resolve(self, *, org_id: str, route: object) -> AnswerResponsibilitySnapshot:
        return AnswerResponsibilitySnapshot(agent_id="refund-card", owner_id="owner-1")


class _Source:
    question_answer_source_capability = "completed_inline_v1"

    def __init__(self) -> None:
        self.calls = 0

    def answer(self, request: object) -> BufferedAnswer:
        self.calls += 1
        return BufferedAnswer(
            candidate=AnswerCandidate(
                text="영업일 3일 안에 처리됩니다.",
                sources=("refund.md",),
            ),
            tokens=("영업일 3일 안에 처리됩니다.",),
        )


class _IdentitySource(_Source):
    def __init__(self, *, registry: Registry, route_authority: object) -> None:
        super().__init__()
        self.registry = registry
        self.route_authority = route_authority

    def matches_question_answer_dependencies(
        self,
        *,
        registry: Registry,
        route_authority: object,
    ) -> bool:
        return self.registry is registry and self.route_authority is route_authority


class _GroundingReader:
    def read(self, agent_id: str) -> GroundingKnowledgeMissing:
        return GroundingKnowledgeMissing(agent_id=agent_id)


class _ContestedIdentitySource(_IdentitySource):
    def __init__(
        self,
        *,
        registry: Registry,
        route_authority: object,
        conflicts: object,
        grounding_reader: GroundingKnowledgeReader,
    ) -> None:
        super().__init__(registry=registry, route_authority=route_authority)
        self.conflicts = conflicts
        self.grounding_reader = grounding_reader

    def matches_contested_question_answer_dependencies(
        self,
        *,
        registry: Registry,
        route_authority: object,
        conflict_resolution_evidence_reader: object,
        grounding_knowledge_reader: GroundingKnowledgeReader,
    ) -> bool:
        return (
            self.registry is registry
            and self.route_authority is route_authority
            and self.conflicts is conflict_resolution_evidence_reader
            and self.grounding_reader is grounding_knowledge_reader
        )


class _MissingAnswerRecordReadStorage(InMemoryQuestionCompletionUnitOfWork):
    answer_record = None  # pyright: ignore[reportAssignmentType]
    answer_records_for_agent = None  # pyright: ignore[reportAssignmentType]


class _MissingConflictReservationProofStore(InMemoryConflictDispositionStore):
    validate_consensus_reservation = None  # pyright: ignore[reportAssignmentType]


class _MissingConflictSealedProofStore(InMemoryConflictDispositionStore):
    validate_sealed_claim = None  # pyright: ignore[reportAssignmentType]


class _MissingManagerReservationProofStore(InMemoryManagerQueueStore):
    validate_action_reservation = None  # pyright: ignore[reportAssignmentType]


class _MissingDeadlockReservationProofStore(InMemoryManagerQueueStore):
    validate_deadlock_action_reservation = None  # pyright: ignore[reportAssignmentType]


def _in_memory_storage_factory(
    *,
    policy: ApprovalPolicy,
    approvals: ApprovalStore,
    responsibility_resolver: ResponsibilitySnapshotResolver,
) -> AtomicQuestionCompletionStorage:
    return InMemoryQuestionCompletionUnitOfWork(
        policy=policy,
        approvals=approvals,
        responsibility_resolver=responsibility_resolver,
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
    )


def _composition(
    *,
    source: object | None = None,
    storage_type: type[InMemoryQuestionCompletionUnitOfWork] = InMemoryQuestionCompletionUnitOfWork,
    scheduler: object | None = None,
    lifecycle_configuration: ApprovalLifecycleConfiguration | None = None,
    approval_item_id_factory: Callable[[], str] | None = None,
    clock: Callable[[], datetime] | None = None,
    evidence_configuration: ApprovalEvidenceConfiguration | None = None,
    approval_policy: ApprovalPolicy | None = None,
):
    approvals = InMemoryApprovalStore()
    policy = approval_policy or _Policy()

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return storage_type(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-1",
            clock=lambda: NOW + timedelta(seconds=3),
        )

    chosen_source = source if source is not None else _Source()
    chosen_item_id_factory = approval_item_id_factory or (lambda: "approval-1")
    chosen_clock = clock or (lambda: NOW)
    composition = build_question_surface_composition(
        storage_factory=storage_factory,
        router=_Router(),
        conflicts=InMemoryConflictCaseStore(),
        managers=InMemoryManagerQueueStore(),
        route_authority=_Authority(),
        handling_deadline_policy=_Deadline(),
        approval_store=approvals,
        approval_policy=policy,
        approval_authorizer=_Authorizer(),
        approval_deadline_policy=_Deadline(),
        responsibility_resolver=_Resolver(),
        answer_source=chosen_source,
        request_id_factory=lambda: "request-1",
        draft_id_factory=lambda: "draft-1",
        approval_item_id_factory=chosen_item_id_factory,
        clock=chosen_clock,
        approval_lifecycle_configuration=lifecycle_configuration,
        approval_evidence_configuration=evidence_configuration,
        scheduler=cast(QuestionProducerScheduler | None, scheduler),
    )
    return composition, composition.storage, chosen_source


class _DurableConflictStore(InMemoryConflictCaseStore):
    workflow_durability: Literal["ephemeral", "durable"] = "durable"


class _DurableManagerStore(InMemoryManagerQueueStore):
    workflow_durability: Literal["ephemeral", "durable"] = "durable"


class _DurableApprovalStore(InMemoryApprovalStore):
    workflow_durability: Literal["ephemeral", "durable"] = "durable"


class _DurableCompletionStorage(InMemoryQuestionCompletionUnitOfWork):
    workflow_durability: Literal["ephemeral", "durable"] = "durable"


class _ObservedCompletionStorage(InMemoryQuestionCompletionUnitOfWork):
    close_calls = 0

    def close(self) -> None:
        type(self).close_calls += 1


class _ApprovalGateObservedStorage(_ObservedCompletionStorage):
    def matches_question_completion_dependencies(
        self,
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> bool:
        del policy, approvals, responsibility_resolver
        return True


class _PassiveScheduler:
    def ensure_started(self, request_id: str, job: object) -> object:
        raise AssertionError(f"producer를 시작하지 않습니다: {request_id}, {job!r}")

    def is_running(self, request_id: str) -> bool:
        del request_id
        return False

    def shutdown(self, *, wait: bool = True) -> None:
        assert wait is True


class _InvalidAtomicDurableStorage(_ObservedCompletionStorage):
    workflow_durability: Literal["ephemeral", "durable"] = "durable"
    question_completion_storage_capability = "atomic_v0"  # pyright: ignore[reportAssignmentType]


class _MismatchedDurableStorage(_DurableCompletionStorage):
    def matches_question_completion_dependencies(
        self,
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> bool:
        del policy, approvals, responsibility_resolver
        return False


def _build_production_contract_surface(
    *,
    storage_factory: Callable[..., AtomicQuestionCompletionStorage],
    conflicts: InMemoryConflictCaseStore | None = None,
    managers: InMemoryManagerQueueStore | None = None,
    approvals: InMemoryApprovalStore | None = None,
    scheduler: QuestionProducerScheduler | None = None,
):
    return build_question_surface_composition(
        storage_factory=storage_factory,
        router=_Router(),
        conflicts=conflicts or _DurableConflictStore(),
        managers=managers or _DurableManagerStore(),
        route_authority=_Authority(),
        handling_deadline_policy=_Deadline(),
        approval_store=approvals or _DurableApprovalStore(),
        approval_policy=_Policy(),
        approval_authorizer=_Authorizer(),
        approval_deadline_policy=_Deadline(),
        responsibility_resolver=_Resolver(),
        answer_source=_Source(),
        request_id_factory=lambda: "request-production-1",
        draft_id_factory=lambda: "draft-production-1",
        approval_item_id_factory=lambda: "approval-production-1",
        clock=lambda: NOW,
        scheduler=scheduler,
        production_style=True,
    )


def _valid_production_environ() -> dict[str, str]:
    return {
        "AON_PRODUCTION_ORG_ID": "org-production",
        "AON_PRODUCTION_DATABASE_DSN": "postgresql://aon@db.example.test/aon",
        "AON_PRODUCTION_OIDC_ISSUER": "https://identity.example.test/",
        "AON_PRODUCTION_OIDC_CLIENT_ID": "aon-production",
        "AON_PRODUCTION_OIDC_CLIENT_SECRET": "oidc-secret-do-not-reflect",
        "AON_PRODUCTION_SESSION_SECRET": "session-secret-do-not-reflect-32-bytes",
        "AON_PRODUCTION_AUTHORITY_POLICY_REF": "authority-policy-v1",
        "AON_PRODUCTION_PROVIDER": "openai",
        "AON_PRODUCTION_PROVIDER_CREDENTIAL": "provider-secret-do-not-reflect",
    }


_SEALED_PRODUCTION_DEPENDENCY_FIELDS = (
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
_SEALED_WIRING_MESSAGE = "production composition wiring is sealed"
_LIFECYCLE_OWNERSHIP_MESSAGE = "question surface lifecycle owner mismatch"


class _MutatedDependency:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def shutdown(self, *, wait: bool = True) -> None:
        assert wait is True
        self._events.append("mutated")

    def close(self) -> None:
        self._events.append("mutated")


def _assert_wiring_mutation_is_sealed(
    composition: QuestionSurfaceComposition,
    *,
    field_name: str,
    delete: bool,
    replacement: object,
) -> None:
    try:
        if delete:
            delattr(composition, field_name)
        else:
            setattr(composition, field_name, replacement)
    except Exception as error:
        assert str(error) == _SEALED_WIRING_MESSAGE
    else:
        pytest.fail("attested production composition wiring mutation이 허용됐습니다.")


def _capture_duplicate_close_error(composition: QuestionSurfaceComposition) -> Exception | None:
    try:
        composition.close()
    except Exception as error:
        return error
    return None


def _assert_lifecycle_ownership_error(error: Exception | None) -> None:
    assert error is not None
    assert type(error).__name__ == "QuestionSurfaceLifecycleOwnershipError"
    assert str(error) == _LIFECYCLE_OWNERSHIP_MESSAGE


class _BootstrapDependencyFactory:
    def __init__(self, dependencies: ProductionDependencies) -> None:
        self._dependencies = dependencies

    def open(self, config: ProductionBootstrapConfig) -> ProductionDependencies:
        assert config.org_id == "org-production"
        return self._dependencies


class _ObservedCanonicalScheduler:
    def __init__(self, events: list[str], *, fail_once: bool = False) -> None:
        self._events = events
        self._fail_once = fail_once

    def ensure_started(self, request_id: str, job: object) -> object:
        raise AssertionError(f"producer를 시작하지 않습니다: {request_id}, {job!r}")

    def is_running(self, request_id: str) -> bool:
        del request_id
        return False

    def shutdown(self, *, wait: bool = True) -> None:
        assert wait is True
        self._events.append("scheduler")
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("canonical-scheduler-secret-do-not-reflect")


def _canonical_production_composition(
    events: list[str],
    *,
    fail_scheduler_once: bool = False,
    fail_storage_once: bool = False,
) -> QuestionSurfaceComposition:
    storage_should_fail = fail_storage_once

    class _ObservedDurableCompletionStorage(_DurableCompletionStorage):
        def close(self) -> None:
            nonlocal storage_should_fail
            events.append("storage")
            if storage_should_fail:
                storage_should_fail = False
                raise RuntimeError("canonical-storage-secret-do-not-reflect")

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return _ObservedDurableCompletionStorage(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-canonical-production",
            clock=lambda: NOW,
        )

    return _build_production_contract_surface(
        storage_factory=storage_factory,
        scheduler=cast(
            QuestionProducerScheduler,
            _ObservedCanonicalScheduler(events, fail_once=fail_scheduler_once),
        ),
    )


def _bootstrap_composition(
    composition: QuestionSurfaceComposition,
    *,
    close_external: Callable[[], None],
) -> ProductionBootstrapHandle | ProductionCompositionRejected:
    def composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        assert production_style is True
        return composition

    result = bootstrap_production(
        environ=_valid_production_environ(),
        dependency_factory=_BootstrapDependencyFactory(
            ProductionDependencies(
                composition_factory=composition_factory,
                close=close_external,
            )
        ),
    )
    return cast(ProductionBootstrapHandle | ProductionCompositionRejected, result)


def test_canonical_production_composition_is_the_only_success_handle() -> None:
    events: list[str] = []
    composition = _canonical_production_composition(events)

    result = _bootstrap_composition(
        composition,
        close_external=lambda: events.append("external_dependency"),
    )

    assert type(result) is ProductionBootstrapHandle
    assert result.composition is composition
    assert result.readiness_scope == "composition_contract_only"
    result.close()
    result.close()
    assert events == ["scheduler", "storage", "external_dependency"]


@pytest.mark.parametrize("field_name", _SEALED_PRODUCTION_DEPENDENCY_FIELDS)
def test_attested_composition_seals_every_public_dependency_before_claim(
    field_name: str,
) -> None:
    events: list[str] = []
    composition = _canonical_production_composition(events)
    original = getattr(composition, field_name)
    replacement = _MutatedDependency(events)

    try:
        _assert_wiring_mutation_is_sealed(
            composition,
            field_name=field_name,
            delete=False,
            replacement=replacement,
        )
        assert getattr(composition, field_name) is original
        _assert_wiring_mutation_is_sealed(
            composition,
            field_name=field_name,
            delete=True,
            replacement=replacement,
        )
        assert getattr(composition, field_name) is original
    finally:
        if not hasattr(composition, field_name) or getattr(composition, field_name) is not original:
            object.__setattr__(composition, field_name, original)
        composition.close()


def test_shallow_copy_cannot_close_canonical_resources_before_original_claim() -> None:
    events: list[str] = []
    original = _canonical_production_composition(events)
    duplicate = copy(original)

    duplicate_error = _capture_duplicate_close_error(duplicate)
    result = _bootstrap_composition(
        original,
        close_external=lambda: events.append("external_dependency"),
    )
    try:
        assert type(result) is ProductionBootstrapHandle
        result.close()
    finally:
        original.close()

    _assert_lifecycle_ownership_error(duplicate_error)
    assert events == ["scheduler", "storage", "external_dependency"]


def test_shallow_copy_cannot_preclose_claimed_handle_resources() -> None:
    events: list[str] = []
    original = _canonical_production_composition(events)
    result = _bootstrap_composition(
        original,
        close_external=lambda: events.append("external_dependency"),
    )
    assert type(result) is ProductionBootstrapHandle
    duplicate = copy(original)

    duplicate_error = _capture_duplicate_close_error(duplicate)
    result.close()

    _assert_lifecycle_ownership_error(duplicate_error)
    assert events == ["scheduler", "storage", "external_dependency"]


def test_dataclass_replace_cannot_alias_canonical_lifecycle_ownership() -> None:
    events: list[str] = []
    original = _canonical_production_composition(events)
    creation_error: Exception | None = None
    try:
        replace(original)
    except Exception as error:
        creation_error = error

    result = _bootstrap_composition(
        original,
        close_external=lambda: events.append("external_dependency"),
    )
    try:
        assert type(result) is ProductionBootstrapHandle
        result.close()
    finally:
        original.close()

    _assert_lifecycle_ownership_error(creation_error)
    assert events == ["scheduler", "storage", "external_dependency"]


def test_default_composition_copy_cannot_close_original_lifecycle_resources() -> None:
    events: list[str] = []

    class _ObservedDefaultStorage(InMemoryQuestionCompletionUnitOfWork):
        def close(self) -> None:
            events.append("storage")

    original, _, _ = _composition(
        storage_type=_ObservedDefaultStorage,
        scheduler=_ObservedCanonicalScheduler(events),
    )
    duplicate = copy(original)

    duplicate_error = _capture_duplicate_close_error(duplicate)
    original.close()

    _assert_lifecycle_ownership_error(duplicate_error)
    assert events == ["scheduler", "storage"]


@pytest.mark.parametrize(
    ("field_name", "delete"),
    [("application", False), ("storage", True)],
)
def test_claimed_handle_rejects_mutation_and_closes_only_original_dependencies(
    field_name: str,
    delete: bool,
) -> None:
    events: list[str] = []
    composition = _canonical_production_composition(events)
    original = getattr(composition, field_name)
    replacement = _MutatedDependency(events)
    result = _bootstrap_composition(
        composition,
        close_external=lambda: events.append("external_dependency"),
    )
    assert type(result) is ProductionBootstrapHandle

    try:
        _assert_wiring_mutation_is_sealed(
            composition,
            field_name=field_name,
            delete=delete,
            replacement=replacement,
        )
        assert getattr(composition, field_name) is original
    finally:
        if not hasattr(composition, field_name) or getattr(composition, field_name) is not original:
            object.__setattr__(composition, field_name, original)
        result.close()

    assert events == ["scheduler", "storage", "external_dependency"]


def test_attested_wiring_is_immutable_during_32_way_claim_close_race() -> None:
    events: list[str] = []
    composition = _canonical_production_composition(events)
    originals = {
        field_name: getattr(composition, field_name)
        for field_name in _SEALED_PRODUCTION_DEPENDENCY_FIELDS
    }
    replacement = _MutatedDependency(events)
    barrier = Barrier(32)

    def claim_and_close() -> object:
        barrier.wait()
        result = _bootstrap_composition(
            composition,
            close_external=lambda: events.append("external_dependency"),
        )
        if type(result) is ProductionBootstrapHandle:
            result.close()
        return result

    def mutate(index: int) -> str:
        barrier.wait()
        field_name = _SEALED_PRODUCTION_DEPENDENCY_FIELDS[
            index % len(_SEALED_PRODUCTION_DEPENDENCY_FIELDS)
        ]
        try:
            if index % 2:
                delattr(composition, field_name)
            else:
                setattr(composition, field_name, replacement)
        except Exception as error:
            return str(error)
        return "mutation_succeeded"

    try:
        with ThreadPoolExecutor(max_workers=32) as executor:
            claim_future = executor.submit(claim_and_close)
            mutation_futures = [executor.submit(mutate, index) for index in range(31)]
            claim_result = claim_future.result(timeout=10)
            mutation_results = [future.result(timeout=10) for future in mutation_futures]

        assert type(claim_result) is ProductionBootstrapHandle
        assert mutation_results == [_SEALED_WIRING_MESSAGE] * 31
        assert all(
            getattr(composition, field_name) is original
            for field_name, original in originals.items()
        )
        assert events == ["scheduler", "storage", "external_dependency"]
    finally:
        for field_name, original in originals.items():
            if (
                not hasattr(composition, field_name)
                or getattr(composition, field_name) is not original
            ):
                object.__setattr__(composition, field_name, original)
        composition.close()


def test_hostile_deleted_attested_field_is_secret_safe_and_permanently_revoked() -> None:
    events: list[str] = []
    composition = _canonical_production_composition(events)
    field_name = "approval_operations"
    original = composition.approval_operations
    object.__delattr__(composition, field_name)
    first: object | None = None
    second: object | None = None

    try:
        first = _bootstrap_composition(
            composition,
            close_external=lambda: events.append("first_external"),
        )
        assert type(first) is ProductionCompositionRejected
        rendered = f"{first!s} {first!r} {first.model_dump(mode='json')!r}"
        assert field_name not in rendered
        assert "hostile-secret-do-not-reflect" not in rendered
        assert events == ["first_external"]

        object.__setattr__(composition, field_name, original)
        second = _bootstrap_composition(
            composition,
            close_external=lambda: events.append("second_external"),
        )
        assert type(second) is ProductionCompositionRejected
        assert events == ["first_external", "second_external"]
    finally:
        if not hasattr(composition, field_name):
            object.__setattr__(composition, field_name, original)
        if type(first) is ProductionBootstrapHandle:
            first.close()
        if type(second) is ProductionBootstrapHandle:
            second.close()
        composition.close()


def test_default_ephemeral_composition_cannot_be_promoted_by_ignoring_factory_flag() -> None:
    events: list[str] = []

    class _ObservedEphemeralStorage(InMemoryQuestionCompletionUnitOfWork):
        def close(self) -> None:
            events.append("storage")

    composition, _, _ = _composition(
        storage_type=_ObservedEphemeralStorage,
        scheduler=_ObservedCanonicalScheduler(events),
    )

    def close_unclaimed_factory_resources() -> None:
        composition.close()
        events.append("external_dependency")

    result = _bootstrap_composition(
        composition,
        close_external=close_unclaimed_factory_resources,
    )

    assert type(result) is ProductionCompositionRejected
    assert events == ["scheduler", "storage", "external_dependency"]


def test_handle_cleanup_retries_canonical_composition_before_external_dependencies() -> None:
    events: list[str] = []
    composition = _canonical_production_composition(events, fail_scheduler_once=True)
    result = _bootstrap_composition(
        composition,
        close_external=lambda: events.append("external_dependency"),
    )
    assert type(result) is ProductionBootstrapHandle

    with pytest.raises(RuntimeError, match=r"^production bootstrap cleanup failed$"):
        result.close()
    result.close()

    assert events == [
        "scheduler",
        "storage",
        "external_dependency",
        "scheduler",
        "storage",
    ]


def test_handle_cleanup_retries_only_failed_external_step_for_canonical_composition() -> None:
    events: list[str] = []
    external_attempts = 0
    composition = _canonical_production_composition(events)

    def close_external() -> None:
        nonlocal external_attempts
        external_attempts += 1
        events.append("external_dependency")
        if external_attempts == 1:
            raise RuntimeError("external-secret-do-not-reflect")

    result = _bootstrap_composition(composition, close_external=close_external)
    assert type(result) is ProductionBootstrapHandle

    with pytest.raises(RuntimeError, match=r"^production bootstrap cleanup failed$"):
        result.close()
    result.close()

    assert events == [
        "scheduler",
        "storage",
        "external_dependency",
        "external_dependency",
    ]


def test_production_attestation_does_not_transfer_to_shallow_copied_composition() -> None:
    events: list[str] = []
    original = _canonical_production_composition(events)
    copied = copy(original)

    copied_result = _bootstrap_composition(
        copied,
        close_external=lambda: events.append("copied_external"),
    )

    assert type(copied_result) is ProductionCompositionRejected
    assert events == ["copied_external"]

    original_result = _bootstrap_composition(
        original,
        close_external=lambda: events.append("original_external"),
    )
    assert type(original_result) is ProductionBootstrapHandle
    original_result.close()
    assert events == [
        "copied_external",
        "scheduler",
        "storage",
        "original_external",
    ]


def test_deepcopy_never_promotes_a_production_composition_copy() -> None:
    events: list[str] = []
    original = _canonical_production_composition(events)
    try:
        copied = deepcopy(original)
    except (TypeError, ValueError):
        copied = None

    if copied is not None:
        _assert_lifecycle_ownership_error(_capture_duplicate_close_error(copied))
        copied_result = _bootstrap_composition(
            copied,
            close_external=lambda: events.append("copied_external"),
        )
        assert type(copied_result) is ProductionCompositionRejected
        assert events == ["copied_external"]

    original_result = _bootstrap_composition(
        original,
        close_external=lambda: events.append("original_external"),
    )
    assert type(original_result) is ProductionBootstrapHandle
    original_result.close()


def test_production_attestation_is_permanently_revoked_after_dependency_identity_mutation() -> None:
    events: list[str] = []
    composition = _canonical_production_composition(events)
    original_registry = composition.registry
    object.__setattr__(composition, "registry", Registry())

    first = _bootstrap_composition(
        composition,
        close_external=lambda: events.append("first_external"),
    )

    assert type(first) is ProductionCompositionRejected
    assert events == ["first_external"]
    object.__setattr__(composition, "registry", original_registry)

    second = _bootstrap_composition(
        composition,
        close_external=lambda: events.append("second_external"),
    )
    try:
        assert type(second) is ProductionCompositionRejected
        assert events == ["first_external", "second_external"]
    finally:
        if type(second) is ProductionBootstrapHandle:
            second.close()
        else:
            composition.close()


@pytest.mark.parametrize("failure_step", ["scheduler", "storage"])
def test_close_start_revokes_attestation_even_when_composition_close_fails(
    failure_step: str,
) -> None:
    events: list[str] = []
    composition = _canonical_production_composition(
        events,
        fail_scheduler_once=failure_step == "scheduler",
        fail_storage_once=failure_step == "storage",
    )

    with pytest.raises(RuntimeError):
        composition.close()
    assert events == ["scheduler", "storage"]

    result = _bootstrap_composition(
        composition,
        close_external=lambda: events.append("external_dependency"),
    )
    try:
        assert type(result) is ProductionCompositionRejected
        assert events == ["scheduler", "storage", "external_dependency"]
    finally:
        if type(result) is ProductionBootstrapHandle:
            result.close()
        else:
            composition.close()

    composition.close()


def test_claimed_composition_cannot_be_bootstrapped_twice_or_closed_by_rejection() -> None:
    events: list[str] = []
    composition = _canonical_production_composition(events)
    first = _bootstrap_composition(
        composition,
        close_external=lambda: events.append("first_external"),
    )
    assert type(first) is ProductionBootstrapHandle

    second = _bootstrap_composition(
        composition,
        close_external=lambda: events.append("second_external"),
    )

    assert type(second) is ProductionCompositionRejected
    assert events == ["second_external"]
    first.close()
    assert events == ["second_external", "scheduler", "storage", "first_external"]


@pytest.mark.parametrize("missing_dependency", ["policy", "authorizer"])
def test_default_surface_rejects_missing_approval_dependency_and_closes_storage(
    missing_dependency: str,
) -> None:
    _ApprovalGateObservedStorage.close_calls = 0
    policy = cast(ApprovalPolicy, None) if missing_dependency == "policy" else _Policy()
    authorizer = (
        cast(ApprovalAuthorizer, None) if missing_dependency == "authorizer" else _Authorizer()
    )

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return _ApprovalGateObservedStorage(
            policy=_Policy(),
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-default-approval-gate",
            clock=lambda: NOW,
        )

    rejected = False
    try:
        build_question_surface_composition(
            storage_factory=storage_factory,
            router=_Router(),
            conflicts=InMemoryConflictCaseStore(),
            managers=InMemoryManagerQueueStore(),
            route_authority=_Authority(),
            handling_deadline_policy=_Deadline(),
            approval_store=InMemoryApprovalStore(),
            approval_policy=policy,
            approval_authorizer=authorizer,
            approval_deadline_policy=_Deadline(),
            responsibility_resolver=_Resolver(),
            answer_source=_Source(),
            request_id_factory=lambda: "request-default-approval-gate",
            draft_id_factory=lambda: "draft-default-approval-gate",
            approval_item_id_factory=lambda: "item-default-approval-gate",
            clock=lambda: NOW,
            scheduler=cast(QuestionProducerScheduler, _PassiveScheduler()),
        )
    except ApprovalConfigurationError:
        rejected = True

    assert _ApprovalGateObservedStorage.close_calls == 1
    assert rejected is True


class _UnknownDurabilityComponent:
    workflow_durability = "unknown"


@pytest.mark.parametrize("component_name", ["conflict", "manager", "approval"])
@pytest.mark.parametrize("marker_kind", ["missing", "unknown"])
def test_production_surface_rejects_missing_or_unknown_linked_durability_before_storage(
    component_name: str,
    marker_kind: str,
) -> None:
    factory_calls = 0
    invalid_component = object() if marker_kind == "missing" else _UnknownDurabilityComponent()

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        nonlocal factory_calls
        factory_calls += 1
        return _DurableCompletionStorage(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-invalid-durability",
            clock=lambda: NOW,
        )

    conflicts = cast(
        InMemoryConflictCaseStore,
        invalid_component if component_name == "conflict" else _DurableConflictStore(),
    )
    managers = cast(
        InMemoryManagerQueueStore,
        invalid_component if component_name == "manager" else _DurableManagerStore(),
    )
    approvals = cast(
        InMemoryApprovalStore,
        invalid_component if component_name == "approval" else _DurableApprovalStore(),
    )

    with pytest.raises(UnknownWorkflowDurabilityError):
        _build_production_contract_surface(
            storage_factory=storage_factory,
            conflicts=conflicts,
            managers=managers,
            approvals=approvals,
        )

    assert factory_calls == 0


@pytest.mark.parametrize("ephemeral_component", ["conflict", "manager", "approval"])
def test_production_surface_rejects_ephemeral_linked_stores_before_storage_factory(
    ephemeral_component: str,
) -> None:
    factory_calls = 0

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        nonlocal factory_calls
        factory_calls += 1
        return _DurableCompletionStorage(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-production-1",
            clock=lambda: NOW,
        )

    conflicts = (
        InMemoryConflictCaseStore()
        if ephemeral_component == "conflict"
        else _DurableConflictStore()
    )
    managers = (
        InMemoryManagerQueueStore() if ephemeral_component == "manager" else _DurableManagerStore()
    )
    approvals = (
        InMemoryApprovalStore() if ephemeral_component == "approval" else _DurableApprovalStore()
    )

    with pytest.raises(NonDurableWorkflowCompositionError):
        _build_production_contract_surface(
            storage_factory=storage_factory,
            conflicts=conflicts,
            managers=managers,
            approvals=approvals,
        )

    assert factory_calls == 0


def test_production_surface_closes_ephemeral_completion_storage_after_rejection() -> None:
    _ObservedCompletionStorage.close_calls = 0

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return _ObservedCompletionStorage(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-production-1",
            clock=lambda: NOW,
        )

    with pytest.raises(NonDurableWorkflowCompositionError):
        _build_production_contract_surface(storage_factory=storage_factory)

    assert _ObservedCompletionStorage.close_calls == 1


@pytest.mark.parametrize(
    ("storage_type", "error_type"),
    [
        (_InvalidAtomicDurableStorage, QuestionCompletionStorageCapabilityError),
        (_MismatchedDurableStorage, QuestionCompletionDependencyIdentityError),
    ],
)
def test_production_surface_preserves_atomic_and_dependency_identity_gates(
    storage_type: type[InMemoryQuestionCompletionUnitOfWork],
    error_type: type[Exception],
) -> None:
    _ObservedCompletionStorage.close_calls = 0

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return storage_type(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-production-1",
            clock=lambda: NOW,
        )

    with pytest.raises(error_type):
        _build_production_contract_surface(storage_factory=storage_factory)


def test_production_surface_propagates_production_style_to_resolution_and_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_org_network import question_surface_composition as composition_module

    observed: list[tuple[str, object]] = []
    resolution_type = composition_module.QuestionResolutionApplication
    execution_type = composition_module.QuestionStreamExecutionService

    def observed_resolution(*args: object, **kwargs: object):
        observed.append(("resolution", kwargs.get("production_style")))
        return resolution_type(*args, **kwargs)

    def observed_execution(*args: object, **kwargs: object):
        observed.append(("execution", kwargs.get("production_style")))
        return execution_type(*args, **kwargs)

    monkeypatch.setattr(
        composition_module,
        "QuestionResolutionApplication",
        observed_resolution,
    )
    monkeypatch.setattr(
        composition_module,
        "QuestionStreamExecutionService",
        observed_execution,
    )

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return _DurableCompletionStorage(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-production-1",
            clock=lambda: NOW,
        )

    composition = _build_production_contract_surface(storage_factory=storage_factory)
    try:
        assert observed == [("resolution", True), ("execution", True)]
    finally:
        composition.close()


class _RetentionPolicy:
    def evaluate(
        self,
        *,
        terminal: ApprovalDraftTerminalEvidence,
        evaluated_at: datetime,
    ) -> ApprovalDraftRetentionDecision:
        return ApprovalDraftRetentionDecision(
            terminal=terminal,
            evaluated_at=evaluated_at,
            policy_version="retention-v1",
            retain_until=terminal.terminal_at + timedelta(days=30),
            purge_eligible=False,
        )


class _RequiredPolicy:
    def evaluate(
        self,
        org_id: str,
        route: object,
        candidate_mode: str,
    ) -> NoApprovalRequired | ApprovalRequired:
        del org_id, route, candidate_mode
        return ApprovalRequired(approver_id="alice", policy_version="approval-v1")


class _EligibleRetentionPolicy:
    def evaluate(
        self,
        *,
        terminal: ApprovalDraftTerminalEvidence,
        evaluated_at: datetime,
    ) -> ApprovalDraftRetentionDecision:
        retain_until = terminal.terminal_at + timedelta(days=30)
        return ApprovalDraftRetentionDecision(
            terminal=terminal,
            evaluated_at=evaluated_at,
            policy_version="retention-v1",
            retain_until=retain_until,
            purge_eligible=evaluated_at >= retain_until,
        )


def test_surface_composition은_Approval_evidence를_하나의_journal과_recorder로_묶는다() -> None:
    journal = InMemoryApprovalEventJournal()
    retention_policy = cast(ApprovalDraftRetentionPolicy, _RetentionPolicy())
    configuration = ApprovalEvidenceConfiguration(
        journal=journal,
        retention_policy=retention_policy,
    )

    composition, storage, _ = _composition(evidence_configuration=configuration)
    try:
        assert composition.approval_events is journal
        assert (
            composition.approval_operations.matches_retention_dependencies(
                retention_policy=retention_policy,
                reader=storage,
                evidence_recorder=cast(ApprovalEventRecorder, object()),
            )
            is False
        )
    finally:
        composition.close()


@pytest.mark.parametrize("missing", ["append", "get", "policy", "notify"])
def test_surface_composition은_Approval_evidence_capability를_storage_생성_전에_거부한다(
    missing: str,
) -> None:
    constructed = 0

    class _TrackedStorage(InMemoryQuestionCompletionUnitOfWork):
        def __init__(self, **kwargs: object) -> None:
            nonlocal constructed
            constructed += 1
            super().__init__(**kwargs)  # pyright: ignore[reportArgumentType]

    journal: object = InMemoryApprovalEventJournal()
    policy: object = _RetentionPolicy()
    notifier: object | None = Notifier()
    if missing == "append":
        journal = object()
    elif missing == "get":

        class _MissingGet:
            def append_batch_once(
                self, events: tuple[ApprovalEvent, ...]
            ) -> tuple[ApprovalEvent, ...]:
                return events

            def append_once(self, event: ApprovalEvent) -> ApprovalEvent:
                return event

            get = None

            def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]:
                del org_id, request_id
                return ()

        journal = _MissingGet()
    elif missing == "policy":
        policy = object()
    else:
        notifier = object()
    configuration = ApprovalEvidenceConfiguration(
        journal=cast(InMemoryApprovalEventJournal, journal),
        retention_policy=cast(ApprovalDraftRetentionPolicy, policy),
        notifier=cast(Notifier | None, notifier),
    )

    with pytest.raises(QuestionApprovalEvidenceConfigurationError):
        _composition(
            storage_type=_TrackedStorage,
            evidence_configuration=configuration,
        )

    assert constructed == 0


def test_surface_composition은_Approval_evidence_hostile_descriptor를_숨기고_storage를_만들지_않는다() -> (
    None
):
    constructed = 0

    class _TrackedStorage(InMemoryQuestionCompletionUnitOfWork):
        def __init__(self, **kwargs: object) -> None:
            nonlocal constructed
            constructed += 1
            super().__init__(**kwargs)  # pyright: ignore[reportArgumentType]

    class _HostilePolicy:
        @property
        def evaluate(self) -> object:
            raise RuntimeError("retention-secret")

    configuration = ApprovalEvidenceConfiguration(
        journal=InMemoryApprovalEventJournal(),
        retention_policy=cast(ApprovalDraftRetentionPolicy, _HostilePolicy()),
    )

    with pytest.raises(QuestionApprovalEvidenceConfigurationError) as captured:
        _composition(
            storage_type=_TrackedStorage,
            evidence_configuration=configuration,
        )

    assert constructed == 0
    assert "retention-secret" not in str(captured.value)


def test_surface_composition은_Approval_evidence_identity_실패에서_scheduler다음_storage를_회수한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _CloseableStorage(InMemoryQuestionCompletionUnitOfWork):
        def close(self) -> None:
            calls.append("storage")

    class _Scheduler:
        def ensure_started(self, request_id: str, job: object) -> object:
            raise AssertionError(f"producer를 시작하지 않습니다: {request_id}, {job!r}")

        def is_running(self, request_id: str) -> bool:
            return False

        def shutdown(self, *, wait: bool = True) -> None:
            assert wait is True
            calls.append("scheduler")

    def never_matches_journal(
        self: ApprovalEventRecorder,
        journal: object,
    ) -> bool:
        del self, journal
        return False

    monkeypatch.setattr(ApprovalEventRecorder, "matches_journal", never_matches_journal)
    configuration = ApprovalEvidenceConfiguration(
        journal=InMemoryApprovalEventJournal(),
        retention_policy=cast(ApprovalDraftRetentionPolicy, _RetentionPolicy()),
    )

    with pytest.raises(QuestionApprovalEvidenceIdentityError):
        _composition(
            storage_type=_CloseableStorage,
            scheduler=_Scheduler(),
            evidence_configuration=configuration,
        )

    assert calls == ["scheduler", "storage"]


def test_surface_composition은_requested_decision_retention을_같은_journal에_본문_없이_남긴다() -> (
    None
):
    journal = InMemoryApprovalEventJournal()

    class _FailingChannel:
        def send(self, notification: Notification) -> None:
            raise RuntimeError(f"push-down: {notification.subject_ref}")

    configuration = ApprovalEvidenceConfiguration(
        journal=journal,
        retention_policy=cast(ApprovalDraftRetentionPolicy, _EligibleRetentionPolicy()),
        notifier=Notifier({"alice": _FailingChannel()}),
    )
    composition, _, _ = _composition(
        approval_policy=cast(ApprovalPolicy, _RequiredPolicy()),
        evidence_configuration=configuration,
    )
    try:
        composition.application.ask(
            AskQuestion(
                principal=RequesterPrincipal(org_id="demo-org", subject_id="user-1"),
                question="환불은 언제 되나요?",
            )
        )
        composition.approval_operations.decide(
            "approval-1",
            ApproverPrincipal(org_id="demo-org", subject_id="alice"),
            ApproveIntent(),
        )
        status = composition.approval_operations.retention_status(
            "approval-1",
            NOW + timedelta(days=31),
        )

        events = journal.for_request("demo-org", "request-1")
        assert [event.kind for event in events] == [
            "requested",
            "approved",
            "retention_eligible",
        ]
        assert status.purge_eligible is True
        assert composition.approval_events is journal
        serialized = " ".join(str(event.model_dump(mode="json")) for event in events)
        assert "환불은 언제" not in serialized
        assert "영업일 3일" not in serialized
    finally:
        composition.close()


def test_surface_composition은_lifecycle과_evidence를_함께_같은_surface에_묶는다() -> None:
    expiry_policy = cast(ApprovalExpiryPolicy, _LifecycleExpiryPolicy())
    reassignment_authorizer = cast(
        ApprovalReassignmentAuthorizer,
        _LifecycleReassignmentAuthorizer(),
    )
    lifecycle = ApprovalLifecycleConfiguration(
        expiry_policy=expiry_policy,
        reassignment_authorizer=reassignment_authorizer,
    )
    journal = InMemoryApprovalEventJournal()
    evidence = ApprovalEvidenceConfiguration(
        journal=journal,
        retention_policy=cast(ApprovalDraftRetentionPolicy, _RetentionPolicy()),
    )

    def item_id_factory() -> str:
        return "approval-1"

    def clock() -> datetime:
        return NOW

    composition, storage, _ = _composition(
        lifecycle_configuration=lifecycle,
        evidence_configuration=evidence,
        approval_item_id_factory=item_id_factory,
        clock=clock,
    )
    try:
        assert composition.approval_events is journal
        assert composition.approval_operations.matches_lifecycle_dependencies(
            expiry_policy=expiry_policy,
            reassignment_authorizer=reassignment_authorizer,
            item_id_factory=item_id_factory,
            clock=clock,
            reader=storage,
            terminal_publisher=composition.application,
        )
        assert composition.approval_operations.expire_due(NOW, limit=1) == []
    finally:
        composition.close()


def test_surface_composition은_호출자_소유_journal과_notifier를_닫지_않는다() -> None:
    closed: list[str] = []

    class _OwnedJournal:
        def __init__(self) -> None:
            self.delegate = InMemoryApprovalEventJournal()

        def append_batch_once(
            self,
            events: tuple[ApprovalEvent, ...],
        ) -> tuple[ApprovalEvent, ...]:
            return self.delegate.append_batch_once(events)

        def append_once(self, event: ApprovalEvent) -> ApprovalEvent:
            return self.delegate.append_once(event)

        def get(self, event_id: str) -> ApprovalEvent | None:
            return self.delegate.get(event_id)

        def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]:
            return self.delegate.for_request(org_id, request_id)

        def close(self) -> None:
            closed.append("journal")

    class _OwnedNotifier(Notifier):
        def close(self) -> None:
            closed.append("notifier")

    journal = _OwnedJournal()
    notifier = _OwnedNotifier()
    composition, _, _ = _composition(
        evidence_configuration=ApprovalEvidenceConfiguration(
            journal=journal,
            retention_policy=cast(ApprovalDraftRetentionPolicy, _RetentionPolicy()),
            notifier=notifier,
        )
    )

    composition.close()

    assert closed == []


def test_surface_composition이_하나의_storage로_blocking_finalization을_닫는다() -> None:
    composition, storage, source = _composition()
    try:
        result = composition.application.ask(
            AskQuestion(
                principal=RequesterPrincipal(org_id="demo-org", subject_id="user-1"),
                question="환불은 언제 되나요?",
            )
        )

        assert isinstance(result, AnsweredQuestionLookup)
        assert result.request_id == "request-1"
        assert result.record_id == "record-1"
        assert storage.by_request("request-1") is not None
        assert isinstance(source, _Source)
        assert source.calls == 1
    finally:
        composition.close()


def test_surface_composition은_Manager_기능을_명시하지_않으면_기존_조립과_호환된다() -> None:
    composition, _, _ = _composition()
    try:
        assert type(composition.approval_operations) is ApprovalOperationsApplication
        assert isinstance(composition.manager_store, InMemoryManagerQueueStore)
        assert composition.manager_disposition is None
    finally:
        composition.close()


def test_surface_composition은_Approval_operations_identity_mismatch에서_자원을_회수한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _CloseableStorage(InMemoryQuestionCompletionUnitOfWork):
        def close(self) -> None:
            calls.append("storage")

    class _Scheduler:
        def ensure_started(self, request_id: str, job: object) -> object:
            raise AssertionError("producer를 시작하지 않습니다.")

        def is_running(self, request_id: str) -> bool:
            return False

        def shutdown(self, *, wait: bool = True) -> None:
            assert wait is True
            calls.append("scheduler")

    def never_matches(
        self: ApprovalOperationsApplication,
        **dependencies: object,
    ) -> bool:
        del self, dependencies
        return False

    monkeypatch.setattr(ApprovalOperationsApplication, "matches_dependencies", never_matches)

    with pytest.raises(QuestionApprovalOperationsIdentityError):
        _composition(storage_type=_CloseableStorage, scheduler=_Scheduler())

    assert calls == ["scheduler", "storage"]


def test_surface_composition은_Approval_lifecycle_의존성을_같은_identity로_묶는다() -> None:
    expiry_policy = cast(ApprovalExpiryPolicy, _LifecycleExpiryPolicy())
    reassignment_authorizer = cast(
        ApprovalReassignmentAuthorizer,
        _LifecycleReassignmentAuthorizer(),
    )

    def item_id_factory() -> str:
        return "approval-1"

    def clock() -> datetime:
        return NOW

    configuration = ApprovalLifecycleConfiguration(
        expiry_policy=expiry_policy,
        reassignment_authorizer=reassignment_authorizer,
    )

    composition, storage, _ = _composition(
        lifecycle_configuration=configuration,
        approval_item_id_factory=item_id_factory,
        clock=clock,
    )
    try:
        operations = composition.approval_operations
        assert operations.matches_lifecycle_dependencies(
            expiry_policy=expiry_policy,
            reassignment_authorizer=reassignment_authorizer,
            item_id_factory=item_id_factory,
            clock=clock,
            reader=storage,
            terminal_publisher=composition.application,
        )
        assert operations.expire_due(NOW, limit=1) == []
        assert not operations.matches_lifecycle_dependencies(
            expiry_policy=cast(ApprovalExpiryPolicy, _LifecycleExpiryPolicy()),
            reassignment_authorizer=reassignment_authorizer,
            item_id_factory=item_id_factory,
            clock=clock,
            reader=storage,
            terminal_publisher=composition.application,
        )
        assert not operations.matches_lifecycle_dependencies(
            expiry_policy=expiry_policy,
            reassignment_authorizer=cast(
                ApprovalReassignmentAuthorizer,
                _LifecycleReassignmentAuthorizer(),
            ),
            item_id_factory=item_id_factory,
            clock=clock,
            reader=storage,
            terminal_publisher=composition.application,
        )
        assert not operations.matches_lifecycle_dependencies(
            expiry_policy=expiry_policy,
            reassignment_authorizer=reassignment_authorizer,
            item_id_factory=lambda: "approval-1",
            clock=clock,
            reader=storage,
            terminal_publisher=composition.application,
        )
        assert not operations.matches_lifecycle_dependencies(
            expiry_policy=expiry_policy,
            reassignment_authorizer=reassignment_authorizer,
            item_id_factory=item_id_factory,
            clock=lambda: NOW,
            reader=storage,
            terminal_publisher=composition.application,
        )
        assert not operations.matches_lifecycle_dependencies(
            expiry_policy=expiry_policy,
            reassignment_authorizer=reassignment_authorizer,
            item_id_factory=item_id_factory,
            clock=clock,
            reader=cast(QuestionCompletionReader, object()),
            terminal_publisher=composition.application,
        )
        assert not operations.matches_lifecycle_dependencies(
            expiry_policy=expiry_policy,
            reassignment_authorizer=reassignment_authorizer,
            item_id_factory=item_id_factory,
            clock=clock,
            reader=storage,
            terminal_publisher=cast(QuestionTerminalPublisher, object()),
        )
    finally:
        composition.close()


def test_surface_composition은_Approval_lifecycle_identity_mismatch에서_회수한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _CloseableStorage(InMemoryQuestionCompletionUnitOfWork):
        def close(self) -> None:
            calls.append("storage")

    class _Scheduler:
        def ensure_started(self, request_id: str, job: object) -> object:
            raise AssertionError("producer를 시작하지 않습니다.")

        def is_running(self, request_id: str) -> bool:
            return False

        def shutdown(self, *, wait: bool = True) -> None:
            assert wait is True
            calls.append("scheduler")

    def never_matches_lifecycle_dependencies(
        self: ApprovalOperationsApplication,
        *,
        expiry_policy: ApprovalExpiryPolicy,
        reassignment_authorizer: ApprovalReassignmentAuthorizer,
        item_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
        reader: QuestionCompletionReader,
        terminal_publisher: QuestionTerminalPublisher,
    ) -> bool:
        del (
            self,
            expiry_policy,
            reassignment_authorizer,
            item_id_factory,
            clock,
            reader,
            terminal_publisher,
        )
        return False

    monkeypatch.setattr(
        ApprovalOperationsApplication,
        "matches_lifecycle_dependencies",
        never_matches_lifecycle_dependencies,
    )
    configuration = ApprovalLifecycleConfiguration(
        expiry_policy=cast(ApprovalExpiryPolicy, _LifecycleExpiryPolicy()),
        reassignment_authorizer=cast(
            ApprovalReassignmentAuthorizer,
            _LifecycleReassignmentAuthorizer(),
        ),
    )

    with pytest.raises(QuestionApprovalOperationsIdentityError):
        _composition(
            storage_type=_CloseableStorage,
            scheduler=_Scheduler(),
            lifecycle_configuration=configuration,
        )

    assert calls == ["scheduler", "storage"]


@pytest.mark.parametrize("missing", ["expiry", "authorizer"])
def test_surface_composition은_Approval_lifecycle_callable을_fail_fast한다(
    missing: str,
) -> None:
    expiry = object() if missing == "expiry" else _LifecycleExpiryPolicy()
    authorizer = object() if missing == "authorizer" else _LifecycleReassignmentAuthorizer()
    configuration = ApprovalLifecycleConfiguration(
        expiry_policy=cast(ApprovalExpiryPolicy, expiry),
        reassignment_authorizer=cast(ApprovalReassignmentAuthorizer, authorizer),
    )

    with pytest.raises(QuestionApprovalLifecycleConfigurationError):
        _composition(lifecycle_configuration=configuration)


def test_surface_composition은_Approval_lifecycle_descriptor_예외를_숨긴다() -> None:
    class _HostileExpiryPolicy:
        @property
        def evaluate(self) -> object:
            raise RuntimeError("expiry-secret")

    configuration = ApprovalLifecycleConfiguration(
        expiry_policy=cast(ApprovalExpiryPolicy, _HostileExpiryPolicy()),
        reassignment_authorizer=cast(
            ApprovalReassignmentAuthorizer,
            _LifecycleReassignmentAuthorizer(),
        ),
    )

    with pytest.raises(QuestionApprovalLifecycleConfigurationError) as captured:
        _composition(lifecycle_configuration=configuration)

    assert "expiry-secret" not in str(captured.value)


def test_surface_composition은_Manager_의존성과_같은_Stream_application을_한번에_묶는다() -> None:
    approvals = InMemoryApprovalStore()
    policy = _Policy()
    managers = InMemoryManagerQueueStore()
    authority = _ManagerAuthority()
    registry = Registry()
    registry.register_user(User(id="owner-1"))
    registry.register(_card())
    source = _IdentitySource(registry=registry, route_authority=authority)

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return InMemoryQuestionCompletionUnitOfWork(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-1",
            clock=lambda: NOW,
        )

    composition = build_question_surface_composition(
        storage_factory=storage_factory,
        router=_Router(),
        conflicts=InMemoryConflictCaseStore(),
        managers=managers,
        route_authority=authority,
        handling_deadline_policy=_Deadline(),
        approval_store=approvals,
        approval_policy=policy,
        approval_authorizer=_Authorizer(),
        approval_deadline_policy=_Deadline(),
        responsibility_resolver=_Resolver(),
        answer_source=source,
        request_id_factory=lambda: "request-1",
        draft_id_factory=lambda: "draft-1",
        approval_item_id_factory=lambda: "approval-1",
        manager_registry=registry,
        manager_generation_factory=lambda: "generation-1",
        clock=lambda: NOW,
    )
    try:
        assert composition.manager_store is managers
        manager_application = composition.manager_disposition
        assert manager_application is not None
        assert manager_application.matches_dependencies(
            requests=composition.storage,
            managers=managers,
            registry=registry,
            route_authority=authority,
            completion_reader=composition.storage,
            execution_starter=composition.application,
            terminal_publisher=composition.application,
        )
    finally:
        composition.close()


def test_surface_composition은_Manager_Registry만_주어지면_기능을_반쪽_활성화하지_않는다() -> None:
    registry = Registry()
    with pytest.raises(QuestionManagerDispositionCapabilityError):
        approvals = InMemoryApprovalStore()
        build_question_surface_composition(
            storage_factory=_in_memory_storage_factory,
            router=_Router(),
            conflicts=InMemoryConflictCaseStore(),
            managers=InMemoryManagerQueueStore(),
            route_authority=_ManagerAuthority(),
            handling_deadline_policy=_Deadline(),
            approval_store=approvals,
            approval_policy=_Policy(),
            approval_authorizer=_Authorizer(),
            approval_deadline_policy=_Deadline(),
            responsibility_resolver=_Resolver(),
            answer_source=_Source(),
            request_id_factory=lambda: "request-1",
            draft_id_factory=lambda: "draft-1",
            approval_item_id_factory=lambda: "approval-1",
            manager_registry=registry,
            clock=lambda: NOW,
        )


def test_surface_composition은_Manager와_Answer_Source의_Registry_identity_mismatch를_거부한다() -> (
    None
):
    approvals = InMemoryApprovalStore()
    registry = Registry()
    authority = _ManagerAuthority()
    with pytest.raises(QuestionAnswerSourceDependencyIdentityError):
        build_question_surface_composition(
            storage_factory=_in_memory_storage_factory,
            router=_Router(),
            conflicts=InMemoryConflictCaseStore(),
            managers=InMemoryManagerQueueStore(),
            route_authority=authority,
            handling_deadline_policy=_Deadline(),
            approval_store=approvals,
            approval_policy=_Policy(),
            approval_authorizer=_Authorizer(),
            approval_deadline_policy=_Deadline(),
            responsibility_resolver=_Resolver(),
            answer_source=_IdentitySource(
                registry=Registry(),
                route_authority=authority,
            ),
            request_id_factory=lambda: "request-1",
            draft_id_factory=lambda: "draft-1",
            approval_item_id_factory=lambda: "approval-1",
            manager_registry=registry,
            manager_generation_factory=lambda: "generation-1",
            clock=lambda: NOW,
        )


def test_surface_composition은_legacy_Manager_args와_P17_5_config_혼용을_거부한다() -> None:
    registry = Registry()
    grounding_reader = _GroundingReader()
    with pytest.raises(QuestionContestedSurfaceConfigurationError):
        approvals = InMemoryApprovalStore()
        build_question_surface_composition(
            storage_factory=_in_memory_storage_factory,
            router=_Router(),
            conflicts=InMemoryConflictDispositionStore(),
            managers=InMemoryManagerQueueStore(),
            route_authority=_ManagerAuthority(),
            handling_deadline_policy=_Deadline(),
            approval_store=approvals,
            approval_policy=_Policy(),
            approval_authorizer=_Authorizer(),
            approval_deadline_policy=_Deadline(),
            responsibility_resolver=_Resolver(),
            answer_source=_Source(),
            request_id_factory=lambda: "request-1",
            draft_id_factory=lambda: "draft-1",
            approval_item_id_factory=lambda: "approval-1",
            manager_registry=registry,
            manager_generation_factory=lambda: "legacy-generation",
            contested_configuration=P17ContestedSurfaceConfiguration(
                registry=registry,
                grounding_knowledge_reader=grounding_reader,
                root_user_id="root",
                manager_item_id_factory=lambda: "manager-item-1",
                generation_factory=lambda: "generation-1",
            ),
            clock=lambda: NOW,
        )


def test_surface_composition은_P17_5_의존성을_같은_identity로_한번에_묶는다() -> None:
    approvals = InMemoryApprovalStore()
    policy = _Policy()
    conflicts = InMemoryConflictDispositionStore()
    managers = InMemoryManagerQueueStore()
    authority = _ManagerAuthority()
    registry = Registry()
    registry.register_user(User(id="root"))
    registry.register_user(User(id="owner-1", manager="root"))
    registry.register(_card())
    grounding_reader = _GroundingReader()
    source = _ContestedIdentitySource(
        registry=registry,
        route_authority=authority,
        conflicts=conflicts,
        grounding_reader=grounding_reader,
    )

    class _RegistryResolver(_Resolver):
        def matches_registry(self, registry: Registry) -> bool:
            return registry is registry_under_test

    registry_under_test = registry

    composition = build_question_surface_composition(
        storage_factory=_in_memory_storage_factory,
        router=_Router(),
        conflicts=conflicts,
        managers=managers,
        route_authority=authority,
        handling_deadline_policy=_Deadline(),
        approval_store=approvals,
        approval_policy=policy,
        approval_authorizer=_Authorizer(),
        approval_deadline_policy=_Deadline(),
        responsibility_resolver=_RegistryResolver(),
        answer_source=source,
        request_id_factory=lambda: "request-1",
        draft_id_factory=lambda: "draft-1",
        approval_item_id_factory=lambda: "approval-1",
        contested_configuration=P17ContestedSurfaceConfiguration(
            registry=registry,
            grounding_knowledge_reader=grounding_reader,
            root_user_id="root",
            manager_item_id_factory=lambda: "manager-item-1",
            generation_factory=lambda: "generation-1",
        ),
        clock=lambda: NOW,
    )
    try:
        assert composition.conflict_store is conflicts
        assert composition.conflict_disposition is not None
        assert composition.deadlock_manager_disposition is not None
        assert composition.registry is registry
        assert composition.route_authority is authority
        assert composition.grounding_knowledge_reader is grounding_reader
        recorder = composition.grounding_terminal_failure_recorder
        assert recorder is not None
        assert recorder.matches_request_store(composition.storage)
    finally:
        composition.close()


@pytest.mark.parametrize(
    ("conflict_store_type", "manager_store_type"),
    [
        (_MissingConflictReservationProofStore, InMemoryManagerQueueStore),
        (_MissingConflictSealedProofStore, InMemoryManagerQueueStore),
        (InMemoryConflictDispositionStore, _MissingManagerReservationProofStore),
        (InMemoryConflictDispositionStore, _MissingDeadlockReservationProofStore),
    ],
)
def test_surface_composition은_reservation_proof_capability가_하나라도_없으면_거부한다(
    conflict_store_type: type[InMemoryConflictDispositionStore],
    manager_store_type: type[InMemoryManagerQueueStore],
) -> None:
    conflicts = conflict_store_type()
    managers = manager_store_type()
    authority = _ManagerAuthority()
    registry = Registry()
    registry.register_user(User(id="root"))
    registry.register_user(User(id="owner-1", manager="root"))
    registry.register(_card())
    grounding_reader = _GroundingReader()
    source = _ContestedIdentitySource(
        registry=registry,
        route_authority=authority,
        conflicts=conflicts,
        grounding_reader=grounding_reader,
    )
    approvals = InMemoryApprovalStore()

    with pytest.raises(
        (QuestionContestedSurfaceCapabilityError, QuestionManagerDispositionCapabilityError)
    ):
        build_question_surface_composition(
            storage_factory=_in_memory_storage_factory,
            router=_Router(),
            conflicts=conflicts,
            managers=managers,
            route_authority=authority,
            handling_deadline_policy=_Deadline(),
            approval_store=approvals,
            approval_policy=_Policy(),
            approval_authorizer=_Authorizer(),
            approval_deadline_policy=_Deadline(),
            responsibility_resolver=_Resolver(),
            answer_source=source,
            request_id_factory=lambda: "request-1",
            draft_id_factory=lambda: "draft-1",
            approval_item_id_factory=lambda: "approval-1",
            contested_configuration=P17ContestedSurfaceConfiguration(
                registry=registry,
                grounding_knowledge_reader=grounding_reader,
                root_user_id="root",
                manager_item_id_factory=lambda: "manager-item-1",
                generation_factory=lambda: "generation-1",
            ),
            clock=lambda: NOW,
        )


def test_surface_composition은_inline_complete_source가_아니면_접수_전에_거부한다() -> None:
    class _UnsupportedSource(_Source):
        question_answer_source_capability = "async_pending_v1"

    with pytest.raises(UnsupportedQuestionAnswerSourceError):
        _composition(source=_UnsupportedSource())


def test_surface_composition은_AnswerRecord_read_capability가_없으면_조립에서_거부한다() -> None:
    with pytest.raises(QuestionAnswerRecordReadCapabilityError):
        _composition(storage_type=_MissingAnswerRecordReadStorage)


def test_surface_composition은_Finalizer가_다른_Approval_의존성을_보면_거부한다() -> None:
    foreign_approvals = InMemoryApprovalStore()
    foreign_policy = _Policy()
    foreign_resolver = _Resolver()

    def mismatched_storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        del policy, approvals, responsibility_resolver
        return InMemoryQuestionCompletionUnitOfWork(
            policy=foreign_policy,
            approvals=foreign_approvals,
            responsibility_resolver=foreign_resolver,
            record_id_factory=lambda: "record-1",
            clock=lambda: NOW,
        )

    approvals = InMemoryApprovalStore()
    with pytest.raises(QuestionCompletionDependencyIdentityError):
        build_question_surface_composition(
            storage_factory=mismatched_storage_factory,
            router=_Router(),
            conflicts=InMemoryConflictCaseStore(),
            managers=InMemoryManagerQueueStore(),
            route_authority=_Authority(),
            handling_deadline_policy=_Deadline(),
            approval_store=approvals,
            approval_policy=_Policy(),
            approval_authorizer=_Authorizer(),
            approval_deadline_policy=_Deadline(),
            responsibility_resolver=_Resolver(),
            answer_source=_Source(),
            request_id_factory=lambda: "request-1",
            draft_id_factory=lambda: "draft-1",
            approval_item_id_factory=lambda: "approval-1",
            clock=lambda: NOW,
        )


def test_surface_composition_close는_멱등이다() -> None:
    calls: list[str] = []

    class _CloseableStorage(InMemoryQuestionCompletionUnitOfWork):
        def close(self) -> None:
            calls.append("storage")

    class _Scheduler:
        def __bool__(self) -> bool:
            return False

        def ensure_started(self, request_id: str, job: object) -> object:
            raise AssertionError("이 테스트에서는 producer를 시작하지 않습니다.")

        def is_running(self, request_id: str) -> bool:
            return False

        def shutdown(self, *, wait: bool = True) -> None:
            assert wait is True
            calls.append("scheduler")

    composition, _, _ = _composition(storage_type=_CloseableStorage, scheduler=_Scheduler())

    composition.close()
    composition.close()

    assert calls == ["scheduler", "storage"]


@pytest.mark.parametrize("failure", ["scheduler", "storage", "both"])
def test_surface_composition_close는_부분_실패_뒤_재시도할_수_있다(failure: str) -> None:
    calls: list[str] = []

    class _RetryStorage(InMemoryQuestionCompletionUnitOfWork):
        close_calls = 0

        def close(self) -> None:
            type(self).close_calls += 1
            calls.append("storage")
            if failure in ("storage", "both") and type(self).close_calls == 1:
                raise RuntimeError("storage-close-failure")

    class _RetryScheduler:
        shutdown_calls = 0

        def ensure_started(self, request_id: str, job: object) -> object:
            raise AssertionError("이 테스트에서는 producer를 시작하지 않습니다.")

        def is_running(self, request_id: str) -> bool:
            return False

        def shutdown(self, *, wait: bool = True) -> None:
            type(self).shutdown_calls += 1
            calls.append("scheduler")
            if failure in ("scheduler", "both") and type(self).shutdown_calls == 1:
                raise RuntimeError("scheduler-shutdown-failure")

    composition, _, _ = _composition(
        storage_type=_RetryStorage,
        scheduler=_RetryScheduler(),
    )

    with pytest.raises(RuntimeError, match="failure"):
        composition.close()
    composition.close()

    assert calls == ["scheduler", "storage", "scheduler", "storage"]


def test_surface_composition은_storage_생성_뒤_조립_실패하면_close한다() -> None:
    close_calls: list[str] = []

    class _InvalidStorage:
        workflow_durability = "ephemeral"
        question_completion_storage_capability = "invalid"

        def close(self) -> None:
            close_calls.append("storage")

    def invalid_storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        del policy, approvals, responsibility_resolver
        return cast(AtomicQuestionCompletionStorage, _InvalidStorage())

    approvals = InMemoryApprovalStore()
    with pytest.raises(QuestionCompletionStorageCapabilityError):
        build_question_surface_composition(
            storage_factory=invalid_storage_factory,
            router=_Router(),
            conflicts=InMemoryConflictCaseStore(),
            managers=InMemoryManagerQueueStore(),
            route_authority=_Authority(),
            handling_deadline_policy=_Deadline(),
            approval_store=approvals,
            approval_policy=_Policy(),
            approval_authorizer=_Authorizer(),
            approval_deadline_policy=_Deadline(),
            responsibility_resolver=_Resolver(),
            answer_source=_Source(),
            request_id_factory=lambda: "request-1",
            draft_id_factory=lambda: "draft-1",
            approval_item_id_factory=lambda: "approval-1",
            clock=lambda: NOW,
        )

    assert close_calls == ["storage"]


def test_surface_composition은_명시_migration된_SQLite에서_재시작_복원된다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "question-surfaces.db"
    migrate_sqlite_completion_schema(db_path)
    approvals = InMemoryApprovalStore()
    policy = _Policy()

    def storage_factory(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return SqliteQuestionCompletionUnitOfWork(
            db_path,
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=lambda: "record-1",
            clock=lambda: NOW + timedelta(seconds=3),
        )

    composition = build_question_surface_composition(
        storage_factory=storage_factory,
        router=_Router(),
        conflicts=InMemoryConflictCaseStore(),
        managers=InMemoryManagerQueueStore(),
        route_authority=_Authority(),
        handling_deadline_policy=_Deadline(),
        approval_store=approvals,
        approval_policy=policy,
        approval_authorizer=_Authorizer(),
        approval_deadline_policy=_Deadline(),
        responsibility_resolver=_Resolver(),
        answer_source=_Source(),
        request_id_factory=lambda: "request-1",
        draft_id_factory=lambda: "draft-1",
        approval_item_id_factory=lambda: "approval-1",
        clock=lambda: NOW,
    )
    result = composition.application.ask(
        AskQuestion(
            principal=RequesterPrincipal(org_id="demo-org", subject_id="user-1"),
            question="환불은 언제 되나요?",
        )
    )
    assert isinstance(result, AnsweredQuestionLookup)
    composition.close()

    reopened = SqliteQuestionCompletionUnitOfWork(
        db_path,
        policy=policy,
        approvals=approvals,
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "unused-record",
        clock=lambda: NOW + timedelta(seconds=4),
    )
    try:
        restored = reopened.by_request("request-1")
        assert restored is not None
        assert restored.completion.record_id == "record-1"
        assert restored.completion.text == result.answer_text
    finally:
        reopened.close()
