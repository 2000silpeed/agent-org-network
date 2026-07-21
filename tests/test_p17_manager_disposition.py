from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock
from collections.abc import Callable
import ast
from pathlib import Path
from typing import cast

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_finalization import (
    AnswerCompletion,
    AnswerResponsibilitySnapshot,
    CompletionBundle,
    DeliveryOutboxEntry,
    NoApprovalEvidence,
    QuestionCompletionReader,
    TerminalAnswerAudit,
)
from agent_org_network.answer_record import AnswerRecord
from agent_org_network.conflict import Candidate, ConflictCase, DivergentVotes
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.decision import Unowned
from agent_org_network.demo_question_surfaces import DemoRouteAuthority
from agent_org_network.manager_queue import (
    AssignOwner as LegacyAssignOwner,
    Dismiss,
    FromDeadlock,
    FromUnowned,
    InMemoryManagerQueueStore,
    ManagerItem,
    ManagerQueueService,
    ManagerResolution,
)
from agent_org_network.p17_manager_disposition import (
    AssignUnownedOwner,
    ClaimAcquired,
    ClaimAttempt,
    ClaimConflict,
    ClaimInProgress,
    DismissUnowned,
    DeadlockManagerSealedClaimHandle,
    ExecutionAlreadyRunning,
    ExecutionStarted,
    ManagerDispositionInvalid,
    ManagerDispositionDependency,
    ManagerDispositionConflict,
    ManagerDispositionInProgress,
    ManagerDispositionClaim,
    ManagerDispositionIntegrity,
    ManagerPrincipal,
    ReservationControlToken,
    ReservedAssignOwnerClaim,
    ReservedDismissClaim,
    ResumeEvidence,
    SealedClaimAvailable,
    SealedClaimHandle,
    SealedAssignOwnerClaim,
    P17ManagerDispositionApplication,
    P17ManagerDispositionCommand,
    TerminalAlreadyPublished,
    TerminalPublished,
    UnownedDismissed,
    UnownedOwnerAssigned,
    AuthorityAssignment,
    AuthorityAssignmentReceipt,
    AuthorityAssignmentRejected,
)
from agent_org_network.question_request import (
    AwaitingManager,
    DeclinedRequest,
    AnsweredRequest,
    FailedRequest,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.question_resolution import AuthorityGrant
from agent_org_network.registry import Registry
from agent_org_network.user import User


NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


def _request_item(item_id: str = "item-1", request_id: str = "request-1") -> ManagerItem:
    return ManagerItem.for_request(
        request_id=request_id,
        manager_id="root",
        source=FromUnowned(
            decision=Unowned(escalated_to="root", intent="refund"),
            question="환불 기준은?",
        ),
        created_at=NOW,
        item_id=item_id,
    )


def _dismiss_command(rationale: str = "중복 질문") -> DismissUnowned:
    return DismissUnowned(
        principal=ManagerPrincipal(org_id="org-1", subject_id="root"),
        item_id="item-1",
        rationale=rationale,
    )


def _reserved_dismiss(generation: str, rationale: str = "중복 질문") -> ReservedDismissClaim:
    return ReservedDismissClaim(
        generation=generation,
        idempotency_key="manager-disposition:item-1",
        request_id="request-1",
        item_id="item-1",
        org_id="org-1",
        by_manager="root",
        rationale=rationale,
    )


def _assign_command(agent_id: str = "refund-card") -> AssignUnownedOwner:
    return AssignUnownedOwner(
        principal=ManagerPrincipal(org_id="org-1", subject_id="root"),
        item_id="item-1",
        agent_id=agent_id,
        rationale="환불 담당 지정",
    )


def _reserved_assign(generation: str, agent_id: str = "refund-card") -> ReservedAssignOwnerClaim:
    return ReservedAssignOwnerClaim(
        generation=generation,
        idempotency_key="manager-disposition:item-1",
        request_id="request-1",
        item_id="item-1",
        org_id="org-1",
        by_manager="root",
        intent="refund",
        agent_id=agent_id,
        requires_approval=False,
        rationale="환불 담당 지정",
    )


def test_manager_principal_is_a_strict_frozen_dto() -> None:
    principal = ManagerPrincipal(org_id="org-1", subject_id="root")

    assert principal.model_dump() == {"org_id": "org-1", "subject_id": "root"}


def test_claim_control_contract_has_generation_bound_tokens() -> None:
    claim = _reserved_dismiss("generation-1")
    acquired = ClaimAcquired(
        claim=claim,
        control_token=ReservationControlToken(
            generation="generation-1",
            token="control-1",
        ),
    )
    handle = SealedClaimHandle(
        generation="generation-1",
        forward_token="forward-1",
    )

    assert acquired.claim.generation == acquired.control_token.generation
    assert handle.generation == acquired.claim.generation
    assert ResumeEvidence.model_fields["attempt"].annotation is not None


def test_store_reservation은_owner_token과_sealed_forward_handle을_분리한다() -> None:
    store = InMemoryManagerQueueStore()
    store.create_or_get_for_request(_request_item())

    acquired = store.reserve_validated_action(
        "item-1",
        _dismiss_command(),
        lambda _item: _reserved_dismiss("generation-1"),
    )
    assert isinstance(acquired, ClaimAcquired)

    follower = store.reserve_validated_action(
        "item-1",
        _dismiss_command(),
        lambda _item: pytest.fail("같은 reservation을 다시 검증하면 안 됩니다."),
    )
    assert isinstance(follower, ClaimInProgress)
    assert isinstance(
        store.reserve_validated_action(
            "item-1",
            _dismiss_command("다른 처분"),
            lambda _item: pytest.fail("상충 command를 검증하면 안 됩니다."),
        ),
        ClaimConflict,
    )

    sealed = store.seal_claim(acquired.claim, control_token=acquired.control_token)
    assert isinstance(sealed, SealedClaimAvailable)
    retry = store.reserve_validated_action(
        "item-1",
        _dismiss_command(),
        lambda _item: pytest.fail("sealed retry는 검증 callback을 호출하면 안 됩니다."),
    )
    assert retry == sealed


def test_store_abandon뒤_새_generation은_stale_control과_handle의_ABA를_거부한다() -> None:
    store = InMemoryManagerQueueStore()
    store.create_or_get_for_request(_request_item())
    first = store.reserve_validated_action(
        "item-1", _assign_command(), lambda _item: _reserved_assign("generation-1")
    )
    assert isinstance(first, ClaimAcquired)
    assert isinstance(first.claim, ReservedAssignOwnerClaim)
    store.abandon_unmutated_claim(first.claim, control_token=first.control_token)

    second = store.reserve_validated_action(
        "item-1", _assign_command(), lambda _item: _reserved_assign("generation-2")
    )
    assert isinstance(second, ClaimAcquired)
    sealed = store.seal_claim(second.claim, control_token=second.control_token)

    with pytest.raises(ManagerDispositionIntegrity):
        store.seal_claim(first.claim, control_token=first.control_token)
    with pytest.raises(ManagerDispositionIntegrity):
        store.record_resume_evidence(
            SealedClaimHandle(generation="generation-1", forward_token=sealed.handle.forward_token),
            ResumeEvidence(
                request_id="request-1",
                from_revision=1,
                to_revision=2,
                route=RouteTarget(
                    intent="refund",
                    agent_id="refund-card",
                    requires_approval=False,
                    authority_version="grant-1",
                ),
                trigger_key="request-dispatch:request-1:1",
            ),
        )


def test_resume_evidence는_same_value만_멱등이고_stale_handle과_치환을_거부한다() -> None:
    store = InMemoryManagerQueueStore()
    store.create_or_get_for_request(_request_item())
    acquired = store.reserve_validated_action(
        "item-1", _assign_command(), lambda _item: _reserved_assign("generation-1")
    )
    assert isinstance(acquired, ClaimAcquired)
    sealed = store.seal_claim(acquired.claim, control_token=acquired.control_token)
    original = store.get("item-1")
    assert original is not None
    with pytest.raises(ManagerDispositionIntegrity):
        store.resolve_for_claim(
            sealed.handle,
            original.resolve(
                ManagerResolution(
                    action=LegacyAssignOwner(
                        by_manager="root",
                        primary="refund-card",
                        rationale="환불 담당 지정",
                    )
                )
            ),
        )
    evidence = ResumeEvidence(
        request_id="request-1",
        from_revision=1,
        to_revision=2,
        route=RouteTarget(
            intent="refund",
            agent_id="refund-card",
            requires_approval=False,
            authority_version="grant-1",
        ),
        trigger_key="request-dispatch:request-1:1",
    )

    with pytest.raises(ManagerDispositionIntegrity):
        store.record_resume_evidence(
            sealed.handle,
            evidence.model_copy(
                update={"route": evidence.route.model_copy(update={"agent_id": "tampered-card"})}
            ),
        )

    store.record_resume_evidence(sealed.handle, evidence)
    store.record_resume_evidence(sealed.handle, evidence)
    assert store.resume_evidence_for_claim(sealed.handle) == evidence
    with pytest.raises(ManagerDispositionIntegrity):
        store.record_resume_evidence(
            sealed.handle,
            evidence.model_copy(update={"from_revision": 2, "to_revision": 3}),
        )


def test_legacy_public_writes는_request_aware_item을_모두_거부한다() -> None:
    store = InMemoryManagerQueueStore()
    item = _request_item()
    with pytest.raises(ValueError, match="request-aware"):
        store.enqueue(item)
    store.create_or_get_for_request(item)
    resolved = item.resolve(
        ManagerResolution(action=Dismiss(by_manager="root", rationale="중복 질문"))
    )
    with pytest.raises(ValueError, match="request-aware"):
        store.mark_resolved(resolved)
    legacy_spoof = ManagerItem(
        manager_id=item.manager_id,
        source=item.source,
        created_at=item.created_at,
        item_id=item.item_id,
    ).resolve(ManagerResolution(action=Dismiss(by_manager="root")))
    with pytest.raises(ValueError, match="request-aware"):
        store.mark_resolved(legacy_spoof)
    with pytest.raises(ValueError, match="request-aware"):
        store.resolve_if_open(
            "item-1",
            Dismiss(by_manager="root"),
            lambda current, _once: current.resolve(
                ManagerResolution(action=Dismiss(by_manager="root"))
            ),
        )
    with pytest.raises(ValueError, match="P17ManagerDispositionApplication"):
        ManagerQueueService(store).act("item-1", Dismiss(by_manager="root"))

    case = ConflictCase.for_request(
        request_id="request-deadlock",
        intent="refund",
        question="누가 담당하나요?",
        candidates=(Candidate(agent_id="refund-card", owner="refund-owner"),),
        opened_at=NOW,
        case_id="case-1",
    )
    deadlock = ManagerItem.for_request(
        request_id="request-deadlock",
        manager_id="root",
        source=FromDeadlock(
            case=case,
            reason="divergent_votes",
            cause=DivergentVotes(round=1),
        ),
        created_at=NOW,
        item_id="deadlock-item",
    )
    with pytest.raises(ValueError, match="request-aware"):
        store.enqueue_deadlock_if_absent(deadlock)

    legacy_case = ConflictCase(
        intent="refund",
        question="legacy",
        candidates=(Candidate(agent_id="refund-card", owner="refund-owner"),),
        opened_at=NOW,
        case_id="legacy-case",
    )
    colliding_legacy = ManagerItem(
        manager_id="root",
        source=FromDeadlock(case=legacy_case),
        created_at=NOW,
        item_id="item-1",
    )
    with pytest.raises(ValueError, match="item_id"):
        store.enqueue_deadlock_if_absent(colliding_legacy)

    retry_store = InMemoryManagerQueueStore()
    assert retry_store.enqueue_deadlock_if_absent(colliding_legacy) == (
        colliding_legacy,
        True,
    )
    assert retry_store.enqueue_deadlock_if_absent(colliding_legacy) == (
        colliding_legacy,
        False,
    )


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        assert org_id == "org-1"
        assert state_kind == "ready_to_dispatch"
        return started_at + timedelta(minutes=30)


class _Starter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_started(self, request_id: str) -> ExecutionStarted | ExecutionAlreadyRunning:
        self.calls.append(request_id)
        return ExecutionStarted() if len(self.calls) == 1 else ExecutionAlreadyRunning()


class _Publisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def publish_terminal(self, request_id: str) -> TerminalPublished | TerminalAlreadyPublished:
        self.calls.append(request_id)
        return TerminalPublished() if len(self.calls) == 1 else TerminalAlreadyPublished()


class _Authority:
    def __init__(self) -> None:
        self.assignments: list[AuthorityAssignment] = []
        self.request_reads: list[tuple[str, str, str, str]] = []
        self.base_reads = 0
        self._receipt: AuthorityAssignmentReceipt | None = None
        self.writes = 0
        self._lock = RLock()

    def assign_owner(
        self,
        assignment: AuthorityAssignment,
    ) -> AuthorityAssignmentReceipt | AuthorityAssignmentRejected:
        with self._lock:
            self.assignments.append(assignment)
            if self._receipt is None:
                self._receipt = AuthorityAssignmentReceipt(
                    assignment=assignment,
                    grant_version="request-grant-v1",
                )
                self.writes += 1
            elif self._receipt.assignment != assignment:
                raise AssertionError("same key의 다른 assignment")
            return self._receipt

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None:
        self.request_reads.append((org_id, request_id, intent, agent_id))
        return AuthorityGrant(policy_version="request-grant-v1")

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        del org_id, intent, agent_id
        self.base_reads += 1
        return AuthorityGrant(policy_version="base-v1")


class _CompletionReader:
    def __init__(self) -> None:
        self.bundle: CompletionBundle | None = None

    def by_request(self, request_id: str) -> CompletionBundle | None:
        del request_id
        return self.bundle

    def by_record(self, record_id: str) -> CompletionBundle | None:
        del record_id
        return self.bundle


def _application_fixture(
    *,
    intent: str = "refund",
    include_card: bool = True,
    manager_store: InMemoryManagerQueueStore | None = None,
    authority_builder: Callable[[Registry], _Authority] | None = None,
    deadline_builder: Callable[[Registry], _Deadline] | None = None,
    request_store: InMemoryQuestionRequestStore | None = None,
    completion_reader: _CompletionReader | None = None,
    registry_instance: Registry | None = None,
    central_authorizer: object | None = None,
) -> tuple[
    P17ManagerDispositionApplication,
    InMemoryQuestionRequestStore,
    InMemoryManagerQueueStore,
    _Authority,
    _Starter,
    _Publisher,
]:
    requests = request_store if request_store is not None else InMemoryQuestionRequestStore()
    managers = manager_store if manager_store is not None else InMemoryManagerQueueStore()
    registry = Registry() if registry_instance is None else registry_instance
    registry.register_user(User(id="root"))
    registry.register_user(User(id="refund-owner", manager="root"))
    registry.register_user(User(id="backup-owner", manager="root"))
    if include_card:
        registry.register(
            AgentCard(
                agent_id="refund-card",
                owner="refund-owner",
                team="support",
                summary="환불 담당",
                domains=["refund"],
                last_reviewed_at=date(2026, 7, 1),
            )
        )
        registry.register(
            AgentCard(
                agent_id="backup-card",
                owner="backup-owner",
                team="support",
                summary="환불 백업",
                domains=["refund"],
                last_reviewed_at=date(2026, 7, 1),
            )
        )
    item = ManagerItem.for_request(
        request_id="request-1",
        manager_id="root",
        source=FromUnowned(
            decision=Unowned(escalated_to="root", intent=intent),
            question="환불 기준은?",
        ),
        created_at=NOW,
        item_id="item-1",
    )
    managers.create_or_get_for_request(item)
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불 기준은?",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(minutes=30),
    )
    requests.create(received)
    normalized = intent.strip() or None
    awaiting = received.record_initial_routing(
        intent=normalized,
        disposition="unowned",
        target=AwaitingManager(
            item_id="item-1",
            public_kind="unowned",
            handling=HandlingAssignment(
                kind="manager_item",
                ref="item-1",
                due_at=NOW + timedelta(minutes=30),
            ),
        ),
        clock=lambda: NOW,
    )
    assert requests.compare_and_set("request-1", 0, received, awaiting)
    authority = _Authority() if authority_builder is None else authority_builder(registry)
    starter = _Starter()
    publisher = _Publisher()
    reader: QuestionCompletionReader = (
        _CompletionReader() if completion_reader is None else completion_reader
    )
    generations = iter(("generation-1", "generation-2", "generation-3"))
    application = P17ManagerDispositionApplication(
        requests=requests,
        managers=managers,
        registry=registry,
        route_authority=authority,
        completion_reader=reader,
        deadline_policy=_Deadline() if deadline_builder is None else deadline_builder(registry),
        execution_starter=starter,
        terminal_publisher=publisher,
        generation_factory=lambda: next(generations),
        clock=lambda: NOW,
        central_authorizer=central_authorizer,  # type: ignore[arg-type]
    )
    return application, requests, managers, authority, starter, publisher


class _ManagerCentralAuthorizer:
    def __init__(self, *, verify_result: bool = True, unavailable: bool = False) -> None:
        self.verify_result = verify_result
        self.unavailable = unavailable
        self.calls: list[tuple[object, object, object]] = []
        self.verify_calls = 0

    def authorize(self, principal: object, action: object, resource: object) -> object:
        self.calls.append((principal, action, resource))
        if self.unavailable:
            return AuthorizationDenied(kind="policy_unavailable")
        assert type(principal) is AuthenticatedPrincipal
        assert type(resource) is ResourceRef
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,  # type: ignore[arg-type]
            resource=resource,
            roles=("manager",),
            policy_version="policy-v1",
            policy_digest="c" * 64,
        )

    def verify(self, grant: object, principal: object, action: object, resource: object) -> bool:
        del grant, principal, action, resource
        self.verify_calls += 1
        return self.verify_result


def _authenticated_manager(subject_id: str = "root") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="org-1",
        subject_id=subject_id,
        identity_provider="oidc",
        identity_session_id=f"session:{subject_id}",
    )


def _authenticated_dismiss(subject_id: str = "root") -> DismissUnowned:
    return DismissUnowned(
        principal=_authenticated_manager(subject_id),
        item_id="item-1",
        rationale="중복 질문",
    )


def test_central_manager_list_and_act_require_permission_and_current_manager() -> None:
    authorizer = _ManagerCentralAuthorizer()
    app, requests, managers, _authority, _starter, _publisher = _application_fixture(
        central_authorizer=authorizer
    )

    pending = app.pending_for(_authenticated_manager())
    result = app.act(_authenticated_dismiss())

    assert [item.item_id for item in pending] == ["item-1"]
    assert isinstance(result, UnownedDismissed)
    assert [call[1] for call in authorizer.calls] == ["manager.list", "manager.act"]
    assert authorizer.calls[1][2] == ResourceRef(
        org_id="org-1",
        kind="manager_item",
        resource_id="item-1",
        owner_subject_id="root",
    )
    assert authorizer.verify_calls == 2
    assert managers.claim_for_item("item-1") is not None
    assert isinstance(requests.get("request-1").state, DeclinedRequest)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("authorizer", "subject_id", "unavailable"),
    [
        (_ManagerCentralAuthorizer(verify_result=False), "root", False),
        (_ManagerCentralAuthorizer(unavailable=True), "root", True),
        (_ManagerCentralAuthorizer(), "other-manager", False),
    ],
)
def test_central_manager_denied_unavailable_or_stale_assignment_has_zero_side_effects(
    authorizer: _ManagerCentralAuthorizer,
    subject_id: str,
    unavailable: bool,
) -> None:
    from agent_org_network.p17_manager_disposition import (
        ManagerAuthorizationUnavailable,
        ManagerDispositionNotFoundOrDenied,
    )

    app, requests, managers, authority, starter, publisher = _application_fixture(
        central_authorizer=authorizer
    )
    expected = (
        ManagerAuthorizationUnavailable if unavailable else ManagerDispositionNotFoundOrDenied
    )

    with pytest.raises(expected):
        app.act(_authenticated_dismiss(subject_id))

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    assert managers.claim_for_item("item-1") is None
    assert authority.assignments == []
    assert starter.calls == []
    assert publisher.calls == []
    if subject_id != "root":
        assert authorizer.calls == []


def test_authenticated_manager_principal_without_central_authorizer_is_fail_closed() -> None:
    from agent_org_network.p17_manager_disposition import ManagerDispositionNotFoundOrDenied

    app, requests, managers, authority, starter, publisher = _application_fixture()

    with pytest.raises(ManagerDispositionNotFoundOrDenied):
        app.act(_authenticated_dismiss())

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    assert managers.claim_for_item("item-1") is None
    assert authority.assignments == []
    assert starter.calls == []
    assert publisher.calls == []


def test_dismiss는_same_request를_manager_declined로_닫고_item을_같이_종결한다() -> None:
    app, requests, managers, authority, _starter, publisher = _application_fixture()

    result = app.act(_dismiss_command())

    assert isinstance(result, UnownedDismissed)
    assert result.reason_code == "manager_declined"
    assert isinstance(result.delivery, TerminalPublished)
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, DeclinedRequest)
    assert stored.state.reason_code == "manager_declined"
    item = managers.get("item-1")
    assert item is not None and item.status == "resolved"
    assert authority.assignments == []
    assert publisher.calls == ["request-1"]


def test_assign은_request_scoped_grant로_same_request를_Router없이_재개한다() -> None:
    app, requests, managers, authority, starter, _publisher = _application_fixture()

    result = app.act(_assign_command())

    assert isinstance(result, UnownedOwnerAssigned)
    assert isinstance(result.wake, ExecutionStarted)
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)
    assert stored.state.route == result.route
    assert stored.state.route.authority_version == "request-grant-v1"
    assert stored.state.attempt == 1
    assert stored.state.trigger_key == "request-dispatch:request-1:1"
    assert authority.base_reads == 0
    assert authority.request_reads == [("org-1", "request-1", "refund", "refund-card")]
    assert len(authority.assignments) == 1
    item = managers.get("item-1")
    assert item is not None and item.status == "resolved"
    assert starter.calls == ["request-1"]


def test_blank_intent는_assign만_거부하고_Dismiss는_허용한다() -> None:
    assign_app, _requests, managers, *_ = _application_fixture(intent=" ")
    with pytest.raises(ManagerDispositionInvalid):
        assign_app.act(_assign_command())
    assert managers.claim_for_item("item-1") is None

    dismiss_app, requests2, *_ = _application_fixture(intent=" ")
    assert isinstance(dismiss_app.act(_dismiss_command()), UnownedDismissed)
    stored = requests2.get("request-1")
    assert stored is not None and isinstance(stored.state, DeclinedRequest)


def test_same_Dismiss_retry는_resolved_guard를_검증하고_재발행만_멱등한다() -> None:
    app, requests, managers, _authority, _starter, publisher = _application_fixture()

    first = app.act(_dismiss_command())
    second = app.act(_dismiss_command())

    assert isinstance(first, UnownedDismissed)
    assert isinstance(second, UnownedDismissed)
    assert isinstance(second.delivery, TerminalAlreadyPublished)
    assert requests.get("request-1") is not None
    assert managers.get("item-1") is not None
    assert publisher.calls == ["request-1", "request-1"]


def test_same_Assign_retry는_resume_evidence와_resolved_guard를_검증한다() -> None:
    app, requests, managers, authority, starter, _publisher = _application_fixture()

    first = app.act(_assign_command())
    second = app.act(_assign_command())

    assert isinstance(first, UnownedOwnerAssigned)
    assert isinstance(second, UnownedOwnerAssigned)
    assert isinstance(second.wake, ExecutionAlreadyRunning)
    assert requests.get("request-1") is not None
    assert managers.get("item-1") is not None
    assert len(authority.assignments) == 2
    assert authority.assignments[0] == authority.assignments[1]
    assert starter.calls == ["request-1", "request-1"]


def test_demo_authority의_request_grant는_base_policy와_다른_request를_바꾸지_않는다() -> None:
    registry = Registry()
    registry.register_user(User(id="refund-owner"))
    registry.register(
        AgentCard(
            agent_id="refund-card",
            owner="refund-owner",
            team="support",
            summary="환불",
            domains=["refund"],
            last_reviewed_at=date(2026, 7, 1),
        )
    )
    authority = DemoRouteAuthority(registry)
    first = AuthorityAssignment(
        org_id="demo-org",
        request_id="request-1",
        item_id="item-1",
        intent="refund",
        agent_id="refund-card",
        assigned_by="root",
        idempotency_key="manager-disposition:item-1",
    )
    second = first.model_copy(
        update={
            "request_id": "request-2",
            "item_id": "item-2",
            "idempotency_key": "manager-disposition:item-2",
        }
    )

    first_receipt = authority.assign_owner(first)
    assert authority.assign_owner(first) == first_receipt
    second_receipt = authority.assign_owner(second)
    assert isinstance(first_receipt, AuthorityAssignmentReceipt)
    assert isinstance(second_receipt, AuthorityAssignmentReceipt)
    assert first_receipt.grant_version != second_receipt.grant_version
    assert authority.authorize_for_request(
        "demo-org", "request-1", "refund", "refund-card"
    ) == AuthorityGrant(policy_version=first_receipt.grant_version)
    assert authority.authorize_for_request(
        "demo-org", "request-2", "refund", "refund-card"
    ) == AuthorityGrant(policy_version=second_receipt.grant_version)
    assert authority.authorize_for_request("demo-org", "request-3", "refund", "refund-card") is None
    assert authority.authorize("demo-org", "refund", "refund-card") == AuthorityGrant(
        policy_version="demo-route-v1"
    )


def test_missing_card_validation은_claim을_남기지_않아_다른_처분을_막지_않는다() -> None:
    app, _requests, managers, *_ = _application_fixture(include_card=False)

    with pytest.raises(ManagerDispositionInvalid):
        app.act(_assign_command())

    assert managers.claim_for_item("item-1") is None
    assert isinstance(app.act(_dismiss_command()), UnownedDismissed)


def test_same_Assign_32way는_한_grant_write와_한_Request_winner로_수렴한다() -> None:
    app, requests, managers, authority, _starter, _publisher = _application_fixture()

    def run(_index: int) -> object:
        try:
            return app.act(_assign_command())
        except ManagerDispositionInProgress as error:
            return error

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(run, range(32)))

    assert authority.writes == 1
    assert sum(isinstance(result, UnownedOwnerAssigned) for result in results) >= 1
    assert all(
        isinstance(result, (UnownedOwnerAssigned, ManagerDispositionInProgress))
        for result in results
    )
    stored = requests.get("request-1")
    item = managers.get("item-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)
    assert stored.revision == 2
    assert item is not None and item.status == "resolved"


def test_Assign_Dismiss_32way는_첫_claim_하나만_이기고_terminal을_섞지_않는다() -> None:
    app, requests, managers, authority, _starter, _publisher = _application_fixture()

    def run(index: int) -> object:
        command = _assign_command() if index % 2 == 0 else _dismiss_command()
        try:
            return app.act(command)
        except (ManagerDispositionConflict, ManagerDispositionInProgress) as error:
            return error

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(run, range(32)))

    stored = requests.get("request-1")
    item = managers.get("item-1")
    assert stored is not None and item is not None and item.status == "resolved"
    winners = [
        result for result in results if isinstance(result, (UnownedOwnerAssigned, UnownedDismissed))
    ]
    assert winners
    if isinstance(stored.state, ReadyToDispatch):
        assert all(isinstance(winner, UnownedOwnerAssigned) for winner in winners)
        assert authority.writes == 1
    else:
        assert isinstance(stored.state, DeclinedRequest)
        assert all(isinstance(winner, UnownedDismissed) for winner in winners)
        assert authority.writes == 0


def test_Assign_CAS뒤_ResumeEvidence_prewrite_failure는_same_action_retry로_복구한다() -> None:
    class _FailOnceEvidenceStore(InMemoryManagerQueueStore):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def record_resume_evidence(
            self,
            handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
            evidence: ResumeEvidence,
        ) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("evidence pre-write fault")
            super().record_resume_evidence(handle, evidence)

    managers = _FailOnceEvidenceStore()
    app, requests, _, authority, starter, _publisher = _application_fixture(manager_store=managers)

    with pytest.raises(ManagerDispositionDependency):
        app.act(_assign_command())
    after_fault = requests.get("request-1")
    assert after_fault is not None and isinstance(after_fault.state, ReadyToDispatch)
    assert managers.get("item-1") is not None

    result = app.act(_assign_command())

    assert isinstance(result, UnownedOwnerAssigned)
    assert managers.attempts == 2
    assert authority.writes == 1
    assert starter.calls == ["request-1"]


def test_ResumeEvidence_void_port의_non_None은_Item과_wake전에_fail_closed한다() -> None:
    class _NonNoneEvidenceStore(InMemoryManagerQueueStore):
        def record_resume_evidence(
            self,
            handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
            evidence: ResumeEvidence,
        ) -> None:
            super().record_resume_evidence(handle, evidence)
            return cast(None, object())

    managers = _NonNoneEvidenceStore()
    app, requests, _, authority, starter, _publisher = _application_fixture(manager_store=managers)

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    stored = requests.get("request-1")
    item = managers.get("item-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)
    assert item is not None and item.status == "open"
    assert authority.writes == 1
    assert starter.calls == []


@pytest.mark.parametrize("action", ["assign", "dismiss"])
def test_Request_CAS뒤_Item_resolve_prewrite_failure는_same_action_retry로_복구한다(
    action: str,
) -> None:
    class _FailOnceResolveStore(InMemoryManagerQueueStore):
        def __init__(self) -> None:
            super().__init__()
            self.resolve_attempts = 0

        def resolve_for_claim(
            self,
            handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
            resolved: ManagerItem,
        ) -> ManagerItem:
            self.resolve_attempts += 1
            if self.resolve_attempts == 1:
                raise RuntimeError("resolve pre-write fault")
            return super().resolve_for_claim(handle, resolved)

    managers = _FailOnceResolveStore()
    app, requests, _, authority, starter, publisher = _application_fixture(manager_store=managers)
    command = _assign_command() if action == "assign" else _dismiss_command()

    with pytest.raises(ManagerDispositionDependency):
        app.act(command)
    after_fault = requests.get("request-1")
    assert after_fault is not None
    assert managers.get("item-1") is not None

    result = app.act(command)

    assert managers.resolve_attempts == 2
    item = managers.get("item-1")
    assert item is not None and item.status == "resolved"
    if action == "assign":
        assert isinstance(after_fault.state, ReadyToDispatch)
        assert isinstance(result, UnownedOwnerAssigned)
        assert authority.writes == 1
        assert starter.calls == ["request-1"]
    else:
        assert isinstance(after_fault.state, DeclinedRequest)
        assert isinstance(result, UnownedDismissed)
        assert authority.writes == 0
        assert publisher.calls == ["request-1"]


def test_Store가_claim_request를_치환하면_Authority와_Request_side_effect전에_거부한다() -> None:
    class _SubstitutingStore(InMemoryManagerQueueStore):
        def reserve_validated_action(
            self,
            item_id: str,
            command: P17ManagerDispositionCommand,
            validate: Callable[[ManagerItem], ReservedAssignOwnerClaim | ReservedDismissClaim],
        ) -> ClaimAttempt:
            raw = super().reserve_validated_action(item_id, command, validate)
            assert isinstance(raw, ClaimAcquired)
            assert isinstance(raw.claim, ReservedAssignOwnerClaim)
            return ClaimAcquired(
                claim=raw.claim.model_copy(update={"request_id": "substituted-request"}),
                control_token=raw.control_token,
            )

    managers = _SubstitutingStore()
    app, requests, _, authority, starter, _publisher = _application_fixture(manager_store=managers)

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    assert authority.assignments == []
    assert starter.calls == []


def test_typed_reject_write0는_reservation을_버려_다른_target을_선택할_수_있다() -> None:
    class _RejectOnce(_Authority):
        def __init__(self) -> None:
            super().__init__()
            self.rejected = False

        def assign_owner(
            self,
            assignment: AuthorityAssignment,
        ) -> AuthorityAssignmentReceipt | AuthorityAssignmentRejected:
            if not self.rejected:
                self.rejected = True
                return AuthorityAssignmentRejected(reason_code="policy_denied")
            return super().assign_owner(assignment)

    authority = _RejectOnce()
    app, requests, managers, *_ = _application_fixture(
        authority_builder=lambda _registry: authority
    )

    with pytest.raises(ManagerDispositionInvalid):
        app.act(_assign_command())
    assert managers.claim_for_item("item-1") is None

    result = app.act(_assign_command("backup-card"))

    assert isinstance(result, UnownedOwnerAssigned)
    assert result.route.agent_id == "backup-card"
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)


def test_typed_reject_abandon_void_port의_non_None은_fail_closed한다() -> None:
    class _NonNoneAbandonStore(InMemoryManagerQueueStore):
        def abandon_unmutated_claim(
            self,
            claim: ReservedAssignOwnerClaim,
            *,
            control_token: ReservationControlToken,
        ) -> None:
            super().abandon_unmutated_claim(claim, control_token=control_token)
            return cast(None, object())

    class _RejectingAuthority(_Authority):
        def assign_owner(
            self,
            assignment: AuthorityAssignment,
        ) -> AuthorityAssignmentReceipt | AuthorityAssignmentRejected:
            del assignment
            return AuthorityAssignmentRejected(reason_code="policy_denied")

    managers = _NonNoneAbandonStore()
    app, requests, _, _authority, starter, _publisher = _application_fixture(
        manager_store=managers,
        authority_builder=lambda _registry: _RejectingAuthority(),
    )

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    assert managers.claim_for_item("item-1") is None
    assert starter.calls == []


@pytest.mark.parametrize("change", ["cannot_answer", "approval_when"])
def test_claim뒤_Registry_underclaim_or_approval_change는_CAS전에_거부한다(
    change: str,
) -> None:
    class _ChangingAuthority(_Authority):
        def __init__(self, registry: Registry) -> None:
            super().__init__()
            self.registry = registry

        def assign_owner(self, assignment: AuthorityAssignment) -> AuthorityAssignmentReceipt:
            current = self.registry.get("refund-card")
            replacement = current.model_copy(
                update={change: ["refund"]},
            )
            self.registry.replace_card(replacement)
            receipt = super().assign_owner(assignment)
            assert isinstance(receipt, AuthorityAssignmentReceipt)
            return receipt

    app, requests, managers, authority, starter, _publisher = _application_fixture(
        authority_builder=_ChangingAuthority
    )

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    assert managers.get("item-1") is not None
    assert authority.writes == 1
    assert starter.calls == []


def test_claim뒤_valid_Owner_transfer는_현재_owner로_계속_진행한다() -> None:
    class _TransferAuthority(_Authority):
        def __init__(self, registry: Registry) -> None:
            super().__init__()
            self.registry = registry

        def assign_owner(self, assignment: AuthorityAssignment) -> AuthorityAssignmentReceipt:
            current = self.registry.get("refund-card")
            self.registry.replace_card(current.model_copy(update={"owner": "backup-owner"}))
            receipt = super().assign_owner(assignment)
            assert isinstance(receipt, AuthorityAssignmentReceipt)
            return receipt

    app, requests, *_ = _application_fixture(authority_builder=_TransferAuthority)

    result = app.act(_assign_command())

    assert isinstance(result, UnownedOwnerAssigned)
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)


@pytest.mark.parametrize("action", ["assign", "dismiss"])
def test_rationale_생략_default_empty는_유효한_처분이다(action: str) -> None:
    app, *_ = _application_fixture()
    command: P17ManagerDispositionCommand
    if action == "assign":
        command = AssignUnownedOwner(
            principal=ManagerPrincipal(org_id="org-1", subject_id="root"),
            item_id="item-1",
            agent_id="refund-card",
        )
    else:
        command = DismissUnowned(
            principal=ManagerPrincipal(org_id="org-1", subject_id="root"),
            item_id="item-1",
        )

    result = app.act(command)

    assert isinstance(result, (UnownedOwnerAssigned, UnownedDismissed))


def test_deadline_dependency가_Registry를_바꾸면_CAS_직전_revalidate가_거부한다() -> None:
    class _ChangingDeadline(_Deadline):
        def __init__(self, registry: Registry) -> None:
            self.registry = registry

        def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
            current = self.registry.get("refund-card")
            self.registry.replace_card(current.model_copy(update={"approval_when": ["refund"]}))
            return super().deadline_for(org_id, state_kind, started_at)

    app, requests, managers, authority, starter, _publisher = _application_fixture(
        deadline_builder=_ChangingDeadline
    )

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    item = managers.get("item-1")
    assert item is not None and item.status == "open"
    assert authority.writes == 1
    assert starter.calls == []


def test_Request_CAS가_대기하는동안_다른_thread_Registry_mutation은_완료된다() -> None:
    class _BlockingRequestStore(InMemoryQuestionRequestStore):
        def __init__(self) -> None:
            super().__init__()
            self.block_ready_cas = False
            self.cas_entered = Event()
            self.release_cas = Event()

        def compare_and_set(
            self,
            request_id: str,
            expected_revision: int,
            current: QuestionRequest,
            updated: QuestionRequest,
        ) -> bool:
            if self.block_ready_cas and isinstance(updated.state, ReadyToDispatch):
                self.cas_entered.set()
                if not self.release_cas.wait(timeout=2):
                    raise TimeoutError("test did not release Request CAS")
            return super().compare_and_set(request_id, expected_revision, current, updated)

    requests = _BlockingRequestStore()
    registry_box: list[Registry] = []

    def capture_registry(registry: Registry) -> _Authority:
        registry_box.append(registry)
        return _Authority()

    app, *_ = _application_fixture(
        request_store=requests,
        authority_builder=capture_registry,
    )
    registry = registry_box[0]
    requests.block_ready_cas = True
    mutation_started = Event()
    mutation_done = Event()

    def mutate_unrelated_card() -> None:
        mutation_started.set()
        card = registry.get("backup-card")
        registry.replace_card(card.model_copy(update={"summary": "변경된 백업 카드"}))
        mutation_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        action = pool.submit(app.act, _assign_command())
        assert requests.cas_entered.wait(timeout=1)
        pool.submit(mutate_unrelated_card)
        try:
            assert mutation_started.wait(timeout=1)
            assert mutation_done.wait(timeout=1), "Request CAS 중 Registry lock을 보유했습니다."
        finally:
            requests.release_cas.set()

        result = action.result(timeout=2)

    assert isinstance(result, UnownedOwnerAssigned)


def test_reservation_Registry_card와_owner_read는_한_snapshot이다() -> None:
    class _PausingRegistry(Registry):
        def __init__(self) -> None:
            super().__init__()
            self.pause_next_target_read = False
            self.target_read = Event()
            self.continue_read = Event()

        def get(self, agent_id: str) -> AgentCard:
            card = super().get(agent_id)
            if self.pause_next_target_read and agent_id == "refund-card":
                self.pause_next_target_read = False
                self.target_read.set()
                if not self.continue_read.wait(timeout=2):
                    raise TimeoutError("test did not continue Registry read")
            return card

    registry = _PausingRegistry()
    app, requests, *_ = _application_fixture(registry_instance=registry)
    registry.pause_next_target_read = True
    mutation_started = Event()
    mutation_done = Event()

    def mutate_target_card() -> None:
        mutation_started.set()
        card = registry.get("refund-card")
        registry.replace_card(card.model_copy(update={"approval_when": ["refund"]}))
        mutation_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        action = pool.submit(app.act, _assign_command())
        assert registry.target_read.wait(timeout=1)
        mutation = pool.submit(mutate_target_card)
        try:
            assert mutation_started.wait(timeout=1)
            assert not mutation_done.wait(timeout=0.2), (
                "card와 Owner User read 사이 Registry mutation이 끼어들었습니다."
            )
        finally:
            registry.continue_read.set()

        mutation.result(timeout=2)
        with pytest.raises(ManagerDispositionIntegrity):
            action.result(timeout=2)

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)


def test_reservation_validation_callback의_same_item_재진입은_fail_closed한다() -> None:
    store = InMemoryManagerQueueStore()
    store.create_or_get_for_request(_request_item())

    def reentrant(_item: ManagerItem) -> ReservedAssignOwnerClaim:
        store.reserve_validated_action(
            "item-1",
            _assign_command(),
            lambda _inner: _reserved_assign("inner-generation"),
        )
        return _reserved_assign("outer-generation")

    with pytest.raises(ManagerDispositionIntegrity):
        store.reserve_validated_action("item-1", _assign_command(), reentrant)

    assert store.claim_for_item("item-1") is None


@pytest.mark.parametrize("action", ["assign", "dismiss"])
def test_Request_CAS_true뒤_readback_치환은_Item_resolve전에_거부한다(action: str) -> None:
    class _SubstitutingRequestStore(InMemoryQuestionRequestStore):
        tamper = False

        def compare_and_set(
            self,
            request_id: str,
            expected_revision: int,
            current: QuestionRequest,
            updated: QuestionRequest,
        ) -> bool:
            won = super().compare_and_set(request_id, expected_revision, current, updated)
            if won and self.tamper:
                self._requests[request_id] = updated.model_copy(update={"question": "치환된 질문"})
            return won

    requests = _SubstitutingRequestStore()
    app, _stored, managers, _authority, starter, publisher = _application_fixture(
        request_store=requests
    )
    requests.tamper = True
    command = _assign_command() if action == "assign" else _dismiss_command()

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(command)

    item = managers.get("item-1")
    assert item is not None and item.status == "open"
    assert starter.calls == []
    assert publisher.calls == []


def test_Failed_open_item_retry는_기록된_ResumeEvidence로만_repair한다() -> None:
    class _FailOnceResolveStore(InMemoryManagerQueueStore):
        failed = False

        def resolve_for_claim(
            self,
            handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
            resolved: ManagerItem,
        ) -> ManagerItem:
            if not self.failed:
                self.failed = True
                raise RuntimeError("resolve pre-write fault")
            return super().resolve_for_claim(handle, resolved)

    managers = _FailOnceResolveStore()
    app, requests, *_ = _application_fixture(manager_store=managers)
    with pytest.raises(ManagerDispositionDependency):
        app.act(_assign_command())
    ready = requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    failed = ready.transition(FailedRequest(error_code="runtime_failed"), clock=lambda: NOW)
    assert requests.compare_and_set("request-1", ready.revision, ready, failed)

    result = app.act(_assign_command())

    assert isinstance(result, UnownedOwnerAssigned)
    item = managers.get("item-1")
    assert item is not None and item.status == "resolved"


def test_Answered_retry는_same_completion_terminal_audit를_exact검증한다() -> None:
    reader = _CompletionReader()
    app, requests, _managers, *_ = _application_fixture(completion_reader=reader)
    app.act(_assign_command())
    ready = requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    completed_at = NOW + timedelta(seconds=1)
    answered = ready.transition(
        AnsweredRequest(record_id="record-1"),
        clock=lambda: completed_at,
    )
    assert requests.compare_and_set("request-1", ready.revision, ready, answered)
    route = ready.state.route
    completion = AnswerCompletion(
        request_id="request-1",
        record_id="record-1",
        text="환불 답변",
        answered_by="refund-owner",
        agent_id="refund-card",
        mode="full",
        sources=("policy.md",),
        snapshot_sha="sha-1",
        review_status="not_required",
        completed_at=completed_at,
    )
    record = AnswerRecord.for_request(
        request_id="request-1",
        record_id="record-1",
        question=answered.question,
        answer_text=completion.text,
        answered_by="refund-owner",
        agent_id="refund-card",
        mode="full",
        sources=completion.sources,
        snapshot_sha=completion.snapshot_sha,
        session_id=None,
        answered_at=completed_at,
    )
    audit = TerminalAnswerAudit(
        request_id="request-1",
        record_id="record-1",
        org_id="org-1",
        requester_id="requester-1",
        attempt=1,
        route=route,
        responsibility=AnswerResponsibilitySnapshot(
            agent_id="refund-card",
            owner_id="refund-owner",
        ),
        candidate_mode="full",
        final_mode="full",
        approval=NoApprovalEvidence(policy_version="request-grant-v1"),
        completed_at=completed_at,
    )
    reader.bundle = CompletionBundle(
        completion=completion,
        request=answered,
        answer_record=record,
        terminal_audit=audit,
        session_turn=None,
        delivery=DeliveryOutboxEntry(
            request_id="request-1",
            record_id="record-1",
            created_at=completed_at,
        ),
    )

    result = app.act(_assign_command())

    assert isinstance(result, UnownedOwnerAssigned)

    base_bundle = reader.bundle
    assert base_bundle is not None
    tampered_audits = (
        audit.model_copy(
            update={"route": route.model_copy(update={"authority_version": "tampered"})}
        ),
        audit.model_copy(update={"attempt": 2}),
        audit.model_copy(update={"record_id": "other-record"}),
    )
    for tampered in tampered_audits:
        reader.bundle = base_bundle.model_copy(update={"terminal_audit": tampered})
        with pytest.raises(ManagerDispositionIntegrity):
            app.act(_assign_command())


def test_Authority_timeout은_claim을_seal하고_same_action만_forward_수렴한다() -> None:
    class _LostResponseAuthority(_Authority):
        lost = False

        def assign_owner(self, assignment: AuthorityAssignment) -> AuthorityAssignmentReceipt:
            receipt = super().assign_owner(assignment)
            assert isinstance(receipt, AuthorityAssignmentReceipt)
            if not self.lost:
                self.lost = True
                raise TimeoutError("response lost after write")
            return receipt

    authority = _LostResponseAuthority()
    app, requests, managers, *_ = _application_fixture(
        authority_builder=lambda _registry: authority
    )

    with pytest.raises(ManagerDispositionDependency):
        app.act(_assign_command())
    with pytest.raises(ManagerDispositionConflict):
        app.act(_dismiss_command())

    result = app.act(_assign_command())

    assert isinstance(result, UnownedOwnerAssigned)
    assert authority.writes == 1
    stored = requests.get("request-1")
    item = managers.get("item-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)
    assert item is not None and item.status == "resolved"


def test_Failed_open_item에서_ResumeEvidence가_보이지_않으면_repair하지_않는다() -> None:
    class _HiddenEvidenceStore(InMemoryManagerQueueStore):
        failed = False
        hide = False

        def resolve_for_claim(
            self,
            handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
            resolved: ManagerItem,
        ) -> ManagerItem:
            if not self.failed:
                self.failed = True
                raise RuntimeError("resolve pre-write fault")
            return super().resolve_for_claim(handle, resolved)

        def resume_evidence_for_claim(
            self,
            handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
        ) -> ResumeEvidence | None:
            return None if self.hide else super().resume_evidence_for_claim(handle)

    managers = _HiddenEvidenceStore()
    app, requests, *_ = _application_fixture(manager_store=managers)
    with pytest.raises(ManagerDispositionDependency):
        app.act(_assign_command())
    ready = requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    failed = ready.transition(FailedRequest(error_code="runtime_failed"), clock=lambda: NOW)
    assert requests.compare_and_set("request-1", ready.revision, ready, failed)
    managers.hide = True

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    item = managers.get("item-1")
    assert item is not None and item.status == "open"


@pytest.mark.parametrize("action", ["assign", "dismiss"])
def test_seal_claim_payload_치환은_Authority_or_Request이후_CAS전에_거부한다(action: str) -> None:
    class _SubstitutingSealStore(InMemoryManagerQueueStore):
        def seal_claim(
            self,
            claim: ReservedAssignOwnerClaim | ReservedDismissClaim,
            *,
            control_token: ReservationControlToken,
        ) -> SealedClaimAvailable:
            sealed = super().seal_claim(claim, control_token=control_token)
            changed = sealed.claim.model_copy(update={"generation": "substituted-generation"})
            return SealedClaimAvailable.model_construct(
                claim=changed,
                handle=sealed.handle.model_copy(update={"generation": "substituted-generation"}),
            )

    managers = _SubstitutingSealStore()
    app, requests, _, authority, starter, publisher = _application_fixture(manager_store=managers)
    command = _assign_command() if action == "assign" else _dismiss_command()

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(command)

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    assert starter.calls == [] and publisher.calls == []
    assert authority.writes == (1 if action == "assign" else 0)


def test_validation_callback이_sealed_claim을_반환해도_store를_오염시키지_않는다() -> None:
    store = InMemoryManagerQueueStore()
    store.create_or_get_for_request(_request_item())
    reserved = _reserved_assign("generation-1")
    sealed = SealedAssignOwnerClaim(
        generation=reserved.generation,
        idempotency_key=reserved.idempotency_key,
        request_id=reserved.request_id,
        item_id=reserved.item_id,
        org_id=reserved.org_id,
        by_manager=reserved.by_manager,
        intent=reserved.intent,
        agent_id=reserved.agent_id,
        requires_approval=reserved.requires_approval,
        rationale=reserved.rationale,
    )

    def invalid_callback(_item: ManagerItem) -> ReservedAssignOwnerClaim:
        return cast(ReservedAssignOwnerClaim, sealed)

    with pytest.raises(ManagerDispositionIntegrity):
        store.reserve_validated_action(
            "item-1",
            _assign_command(),
            invalid_callback,
        )

    assert store.claim_for_item("item-1") is None


def test_closed_errors는_untrusted_input을_문자열에_반사하지_않고_Router_Precedent에_의존하지_않는다() -> (
    None
):
    secret = "evil-item\nsecret"
    errors = (
        ManagerDispositionInvalid(),
        ManagerDispositionIntegrity(),
        ManagerDispositionConflict(),
        ManagerDispositionDependency(),
    )
    assert all(secret not in str(error) for error in errors)
    tree = ast.parse(
        (Path(__file__).parents[1] / "src/agent_org_network/p17_manager_disposition.py").read_text(
            encoding="utf-8"
        )
    )
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert all("router" not in name and "precedent" not in name for name in imports)


def test_resolve_for_claim_return_치환은_wake전에_거부한다() -> None:
    class _SubstitutingResolveStore(InMemoryManagerQueueStore):
        def resolve_for_claim(
            self,
            handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
            resolved: ManagerItem,
        ) -> ManagerItem:
            original = self.get(resolved.item_id)
            assert original is not None
            super().resolve_for_claim(handle, resolved)
            return original

    managers = _SubstitutingResolveStore()
    app, _requests, _, _authority, starter, _publisher = _application_fixture(
        manager_store=managers
    )

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    assert starter.calls == []


def test_claim_for_item_store_fault는_closed_Dependency로_정규화한다() -> None:
    class _BrokenClaimReadStore(InMemoryManagerQueueStore):
        def claim_for_item(self, item_id: str) -> ManagerDispositionClaim | None:
            del item_id
            raise OSError("store secret")

    managers = _BrokenClaimReadStore()
    app, requests, *_ = _application_fixture(manager_store=managers)
    current = requests.get("request-1")
    assert current is not None
    ready = current.transition(
        ReadyToDispatch(
            route=RouteTarget(
                intent="refund",
                agent_id="refund-card",
                requires_approval=False,
                authority_version="grant",
            ),
            attempt=1,
            trigger_key="request-dispatch:request-1:1",
            handling=HandlingAssignment(
                kind="system",
                ref="request-dispatch:request-1:1",
                due_at=NOW + timedelta(minutes=30),
            ),
        ),
        clock=lambda: NOW,
    )
    assert requests.compare_and_set("request-1", current.revision, current, ready)

    with pytest.raises(ManagerDispositionDependency) as caught:
        app.act(_assign_command())

    assert "store secret" not in str(caught.value)


@pytest.mark.parametrize("action", ["assign", "dismiss"])
def test_reservation_return만_하고_persist하지_않는_store는_side_effect전에_거부한다(
    action: str,
) -> None:
    class _LyingReservationStore(InMemoryManagerQueueStore):
        def reserve_validated_action(
            self,
            item_id: str,
            command: P17ManagerDispositionCommand,
            validate: Callable[[ManagerItem], ReservedAssignOwnerClaim | ReservedDismissClaim],
        ) -> ClaimAttempt:
            del command
            item = self.get(item_id)
            assert item is not None
            claim = validate(item)
            return ClaimAcquired(
                claim=claim,
                control_token=ReservationControlToken(
                    generation=claim.generation,
                    token="lying-token",
                ),
            )

    managers = _LyingReservationStore()
    app, requests, _, authority, starter, publisher = _application_fixture(manager_store=managers)
    command = _assign_command() if action == "assign" else _dismiss_command()

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(command)

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    assert authority.assignments == []
    assert starter.calls == [] and publisher.calls == []


def test_reservation_return의_same_generation_other_secret은_Authority_write전에_거부한다() -> None:
    class _MutatedControlReturnStore(InMemoryManagerQueueStore):
        def reserve_validated_action(
            self,
            item_id: str,
            command: P17ManagerDispositionCommand,
            validate: Callable[[ManagerItem], ReservedAssignOwnerClaim | ReservedDismissClaim],
        ) -> ClaimAttempt:
            attempt = super().reserve_validated_action(item_id, command, validate)
            if isinstance(attempt, ClaimAcquired):
                return attempt.model_copy(
                    update={
                        "control_token": attempt.control_token.model_copy(
                            update={"token": "same-generation-other-secret"}
                        )
                    }
                )
            return attempt

    managers = _MutatedControlReturnStore()
    app, requests, _, authority, starter, publisher = _application_fixture(manager_store=managers)

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    assert isinstance(managers.claim_for_item("item-1"), ReservedAssignOwnerClaim)
    assert authority.assignments == []
    assert authority.writes == 0
    assert starter.calls == [] and publisher.calls == []


def test_typed_reject에서_abandon_noop_store는_다른_target을_열었다고_간주하지_않는다() -> None:
    class _LyingAbandonStore(InMemoryManagerQueueStore):
        def abandon_unmutated_claim(
            self,
            claim: ReservedAssignOwnerClaim,
            *,
            control_token: ReservationControlToken,
        ) -> None:
            del claim, control_token

    class _RejectingAuthority(_Authority):
        def assign_owner(
            self,
            assignment: AuthorityAssignment,
        ) -> AuthorityAssignmentReceipt | AuthorityAssignmentRejected:
            del assignment
            return AuthorityAssignmentRejected(reason_code="policy_denied")

    managers = _LyingAbandonStore()
    app, requests, _, authority, starter, _publisher = _application_fixture(
        manager_store=managers,
        authority_builder=lambda _registry: _RejectingAuthority(),
    )

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingManager)
    assert managers.claim_for_item("item-1") is not None
    assert authority.writes == 0
    assert starter.calls == []


def test_manager_store_ingress_egress_history는_backing_alias를_공유하지_않는다() -> None:
    store = InMemoryManagerQueueStore()
    original = _request_item()
    stored, _ = store.create_or_get_for_request(original)

    object.__setattr__(original, "manager_id", "mutated-input")
    object.__setattr__(stored, "manager_id", "mutated-return")
    fetched = store.get("item-1")
    assert fetched is not None
    object.__setattr__(fetched, "manager_id", "mutated-get")
    snapshot = store.history
    object.__setattr__(snapshot[0], "manager_id", "mutated-history-entry")
    snapshot.clear()

    current = store.get("item-1")
    assert current is not None and current.manager_id == "root"
    assert len(store.history) == 1
    assert store.history[0].manager_id == "root"


def test_manager_claim_token_handle_evidence는_backing_alias를_공유하지_않는다() -> None:
    store = InMemoryManagerQueueStore()
    store.create_or_get_for_request(_request_item())
    acquired = store.reserve_validated_action(
        "item-1", _assign_command(), lambda _item: _reserved_assign("generation-alias")
    )
    assert isinstance(acquired, ClaimAcquired)
    assert isinstance(acquired.claim, ReservedAssignOwnerClaim)
    original_claim = acquired.claim.model_copy(deep=True)
    original_control = acquired.control_token.model_copy(deep=True)

    object.__setattr__(acquired.claim, "agent_id", "forged-card")
    object.__setattr__(acquired.control_token, "token", "forged-token")
    assert store.claim_for_item("item-1") == original_claim
    sealed = store.seal_claim(original_claim, control_token=original_control)
    original_handle = sealed.handle.model_copy(deep=True)
    object.__setattr__(sealed.claim, "agent_id", "forged-sealed")
    object.__setattr__(sealed.handle, "forward_token", "forged-forward")

    retry = store.reserve_validated_action(
        "item-1",
        _assign_command(),
        lambda _item: pytest.fail("sealed retry callback must not run"),
    )
    assert isinstance(retry, SealedClaimAvailable)
    assert isinstance(retry.claim, SealedAssignOwnerClaim)
    assert retry.claim.agent_id == "refund-card"
    assert retry.handle == original_handle

    evidence = ResumeEvidence(
        request_id="request-1",
        from_revision=1,
        to_revision=2,
        route=RouteTarget(
            intent="refund",
            agent_id="refund-card",
            requires_approval=False,
            authority_version="grant-1",
        ),
        trigger_key="request-dispatch:request-1:1",
    )
    store.record_resume_evidence(original_handle, evidence)
    object.__setattr__(evidence, "request_id", "forged-request")
    read = store.resume_evidence_for_claim(original_handle)
    assert read is not None and read.request_id == "request-1"
    object.__setattr__(read, "request_id", "forged-read")
    reread = store.resume_evidence_for_claim(original_handle)
    assert reread is not None and reread.request_id == "request-1"


def test_demo_authority_receipt_return은_backing_grant와_alias되지_않는다() -> None:
    registry = Registry()
    registry.register_user(User(id="owner"))
    registry.register(
        AgentCard(
            agent_id="refund-card",
            owner="owner",
            team="ops",
            summary="refund",
            domains=["refund"],
            last_reviewed_at=date(2026, 1, 1),
        )
    )
    authority = DemoRouteAuthority(registry)
    original_assignment = AuthorityAssignment(
        org_id="demo-org",
        request_id="request-alias",
        item_id="item-alias",
        intent="refund",
        agent_id="refund-card",
        assigned_by="root",
        idempotency_key="manager-disposition:item-alias",
    )
    receipt = authority.assign_owner(original_assignment)
    assert isinstance(receipt, AuthorityAssignmentReceipt)
    original_version = receipt.grant_version

    object.__setattr__(original_assignment, "agent_id", "forged-input")
    object.__setattr__(receipt, "grant_version", "forged-version")
    object.__setattr__(receipt.assignment, "request_id", "forged-request")

    retry = authority.assign_owner(
        AuthorityAssignment(
            org_id="demo-org",
            request_id="request-alias",
            item_id="item-alias",
            intent="refund",
            agent_id="refund-card",
            assigned_by="root",
            idempotency_key="manager-disposition:item-alias",
        )
    )
    assert isinstance(retry, AuthorityAssignmentReceipt)
    assert retry.grant_version == original_version
    assert retry.assignment.request_id == "request-alias"
    assert authority.authorize_for_request(
        "demo-org", "request-alias", "refund", "refund-card"
    ) == AuthorityGrant(policy_version=original_version)


@pytest.mark.parametrize("action", ["assign", "dismiss"])
def test_existing_claim과_Manager_context_불일치는_Forbidden아닌_Integrity다(action: str) -> None:
    class _TamperingReadStore(InMemoryManagerQueueStore):
        tamper = False

        def get(self, item_id: str) -> ManagerItem | None:
            item = super().get(item_id)
            if item is not None and self.tamper:
                object.__setattr__(item, "manager_id", "tampered-manager")
            return item

    class _LostAuthority(_Authority):
        def assign_owner(self, assignment: AuthorityAssignment) -> AuthorityAssignmentReceipt:
            receipt = super().assign_owner(assignment)
            assert isinstance(receipt, AuthorityAssignmentReceipt)
            raise TimeoutError("lost")

    class _FailingRequestStore(InMemoryQuestionRequestStore):
        fail = False

        def compare_and_set(
            self,
            request_id: str,
            expected_revision: int,
            current: QuestionRequest,
            updated: QuestionRequest,
        ) -> bool:
            if self.fail:
                raise OSError("cas unavailable")
            return super().compare_and_set(request_id, expected_revision, current, updated)

    managers = _TamperingReadStore()
    requests = _FailingRequestStore()
    authority_builder: Callable[[Registry], _Authority] | None = None
    if action == "assign":

        def build_lost_authority(_registry: Registry) -> _Authority:
            return _LostAuthority()

        authority_builder = build_lost_authority
    app, *_ = _application_fixture(
        manager_store=managers,
        request_store=requests,
        authority_builder=authority_builder,
    )
    command = _assign_command() if action == "assign" else _dismiss_command()
    if action == "dismiss":
        requests.fail = True

    with pytest.raises(ManagerDispositionDependency):
        app.act(command)
    assert managers.claim_for_item("item-1") is not None
    managers.tamper = True

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(command)
