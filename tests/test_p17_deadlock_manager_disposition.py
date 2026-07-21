from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_finalization import (
    AnswerCompletion,
    AnswerResponsibilitySnapshot,
    CompletionBundle,
    DeliveryOutboxEntry,
    NoApprovalEvidence,
    TerminalAnswerAudit,
)
from agent_org_network.answer_record import AnswerRecord
from agent_org_network.conflict import Candidate, ConflictCase, DivergentVotes, Resolution
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.demo_question_surfaces import DEMO_ORG_ID, DemoRouteAuthority
from agent_org_network.manager_queue import (
    AssignOwner,
    Dismiss,
    FromDeadlock,
    InMemoryManagerQueueStore,
    ManagerItem,
    ManagerResolution,
)
from agent_org_network.p17_conflict_disposition import (
    ConcurOnConflict,
    ConcurrenceActionFingerprint,
    ConflictResolutionEvidence,
    ConflictMediationHandle,
    ConflictSealedClaimHandle,
    FromManagerMediation,
    InMemoryConflictDispositionStore,
    OwnerConcurrenceEvidence,
    OwnerPrincipal,
    SealedConflictClaimAvailable,
    SealedConflictMediationAvailable,
    SealedDeadlockClaim,
    ValidatedOwnerVote,
    ValidatedManagerMediation,
)
from agent_org_network.p17_deadlock_manager_disposition import (
    P17DeadlockManagerDispositionApplication,
)
from agent_org_network.p17_manager_disposition import (
    AssignDeadlockedOwner,
    DeadlockDismissed,
    DeadlockManagerClaimAcquired,
    DeadlockManagerClaimAttempt,
    DeadlockManagerDispositionCommand,
    DeadlockManagerReservationControlToken,
    DeadlockManagerSealedClaimAvailable,
    DeadlockManagerSealedClaimHandle,
    DeadlockOwnerAssigned,
    DismissDeadlocked,
    ExecutionAlreadyRunning,
    ExecutionDeferred,
    ExecutionStarted,
    ExecutionWake,
    ManagerDispositionConflict,
    ManagerDispositionDependency,
    ManagerDispositionForbidden,
    ManagerDispositionIntegrity,
    ManagerDispositionInvalid,
    ManagerDispositionNotFoundOrDenied,
    ManagerPrincipal,
    ReservedDeadlockAssignClaim,
    ReservedDeadlockDismissClaim,
    ResumeEvidence,
    SealedClaimHandle,
    SealedDeadlockAssignClaim,
    SealedDeadlockDismissClaim,
    TerminalAlreadyPublished,
    TerminalDeferred,
    TerminalDelivery,
    TerminalPublished,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingApproval,
    AwaitingConflict,
    AwaitingManager,
    DeclinedRequest,
    FailedRequest,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    ReadyToDispatch,
)
from agent_org_network.question_resolution import AuthorityGrant, RequestLockPool
from agent_org_network.registry import Registry
from agent_org_network.request_route_authority import (
    FromDeadlockManagerGrant,
    RequestRouteAuthority,
    RequestRouteGrantAssignment,
    RequestRouteGrantConflict,
    RequestRouteGrantReceipt,
    RequestRouteGrantRejected,
    RequestRouteGrantResult,
)
from agent_org_network.user import User


NOW = datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc)


def _ids(*values: str) -> Callable[[], object]:
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


def _card(
    agent_id: str,
    owner: str,
    *,
    approval_when: tuple[str, ...] = (),
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="support",
        summary=agent_id,
        domains=["refund"],
        approval_when=list(approval_when),
        last_reviewed_at=date(2026, 7, 1),
    )


def _case() -> ConflictCase:
    return ConflictCase.for_request(
        request_id="request-1",
        intent="refund",
        question="환불 기준은?",
        candidates=(
            Candidate(agent_id="refund-card", owner="owner-a"),
            Candidate(agent_id="finance-card", owner="owner-b"),
        ),
        opened_at=NOW,
        case_id="case-1",
    )


def _command(owner: str, target: str) -> ConcurOnConflict:
    return ConcurOnConflict(
        principal=OwnerPrincipal(org_id=DEMO_ORG_ID, subject_id=owner),
        case_id="case-1",
        expected_round=1,
        on_agent=target,
    )


def _validated(case: ConflictCase, command: ConcurOnConflict) -> ValidatedOwnerVote:
    return ValidatedOwnerVote(
        request_id="request-1",
        case_id="case-1",
        org_id=DEMO_ORG_ID,
        intent="refund",
        candidate_snapshot=case.candidates,
        trigger=ConcurrenceActionFingerprint(
            case_id="case-1",
            org_id=DEMO_ORG_ID,
            owner_id=command.principal.subject_id,
            expected_round=1,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        ),
        evidence=OwnerConcurrenceEvidence(
            round=1,
            owner_id=command.principal.subject_id,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        ),
        target_requires_approval=False,
    )


class _DeadlinePolicy:
    def __init__(self, *, mode: str = "valid") -> None:
        self.calls = 0
        self.mode = mode

    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        assert org_id == DEMO_ORG_ID
        assert state_kind == "ready_to_dispatch"
        self.calls += 1
        if self.mode == "exception":
            raise RuntimeError("deadline unavailable")
        if self.mode == "naive":
            return datetime(2026, 7, 13, 19, 0)
        if self.mode == "regressing":
            return started_at - timedelta(seconds=1)
        if self.mode == "malformed":
            return object()  # type: ignore[return-value]
        return started_at + timedelta(hours=1)


class _Starter:
    def __init__(self, *, mode: str = "started") -> None:
        self.calls = 0
        self.mode = mode

    def ensure_started(self, request_id: str) -> ExecutionWake:
        assert request_id == "request-1"
        self.calls += 1
        if self.mode == "exception":
            raise RuntimeError("submission unavailable")
        if self.mode == "already":
            return ExecutionAlreadyRunning()
        if self.mode == "deferred":
            return ExecutionDeferred(reason_code="busy")
        if self.mode == "malformed":
            return object()  # type: ignore[return-value]
        if self.mode == "subclass":
            subclass = type("ExecutionStartedSubclass", (ExecutionStarted,), {})
            return subclass()  # type: ignore[return-value]
        return ExecutionStarted()


class _Publisher:
    def __init__(self, *, mode: str = "published") -> None:
        self.calls = 0
        self.mode = mode

    def publish_terminal(self, request_id: str) -> TerminalDelivery:
        assert request_id == "request-1"
        self.calls += 1
        if self.mode == "exception":
            raise RuntimeError("publisher unavailable")
        if self.mode == "already":
            return TerminalAlreadyPublished()
        if self.mode == "deferred":
            return TerminalDeferred(reason_code="busy")
        if self.mode == "malformed":
            return object()  # type: ignore[return-value]
        if self.mode == "subclass":
            subclass = type("TerminalPublishedSubclass", (TerminalPublished,), {})
            return subclass()  # type: ignore[return-value]
        return TerminalPublished()


class _Reader:
    def __init__(self) -> None:
        self.bundle: CompletionBundle | None = None

    def by_request(self, request_id: str) -> CompletionBundle | None:
        assert request_id == "request-1"
        return self.bundle

    def by_record(self, record_id: str) -> CompletionBundle | None:
        del record_id
        return self.bundle


class _Fixture:
    def __init__(
        self,
        *,
        approval: bool = False,
        requests: InMemoryQuestionRequestStore | None = None,
        conflicts: InMemoryConflictDispositionStore | None = None,
        managers: InMemoryManagerQueueStore | None = None,
    ) -> None:
        self.registry = Registry()
        for user in (
            User(id="root"),
            User(id="manager-a", manager="root"),
            User(id="owner-a", manager="manager-a"),
            User(id="owner-b", manager="manager-a"),
        ):
            self.registry.register_user(user)
        self.registry.register(
            _card("refund-card", "owner-a", approval_when=("refund",) if approval else ())
        )
        self.registry.register(_card("finance-card", "owner-b"))

        self.requests = requests or InMemoryQuestionRequestStore()
        received = QuestionRequest.receive(
            org_id=DEMO_ORG_ID,
            requester_id="requester",
            question="환불 기준은?",
            request_id_factory=lambda: "request-1",
            clock=lambda: NOW,
            due_at=NOW + timedelta(hours=1),
        )
        self.requests.create(received)
        awaiting_conflict = received.record_initial_routing(
            intent="refund",
            disposition="contested",
            target=AwaitingConflict(
                case_id="case-1",
                handling=HandlingAssignment(
                    kind="conflict_case",
                    ref="case-1",
                    due_at=NOW + timedelta(hours=1),
                ),
            ),
            clock=lambda: NOW,
        )
        assert self.requests.compare_and_set("request-1", 0, received, awaiting_conflict)

        self.conflicts = conflicts or InMemoryConflictDispositionStore(
            id_factory=_ids("conflict-generation", "conflict-forward", "mediation-forward")
        )
        self.conflicts.create_or_get_for_request(_case())
        self.conflicts.reserve_validated_concurrence(
            "case-1",
            _command("owner-a", "refund-card"),
            validate=_validated,
        )
        conflict_available = self.conflicts.reserve_validated_concurrence(
            "case-1",
            _command("owner-b", "finance-card"),
            validate=_validated,
        )
        assert isinstance(conflict_available, SealedConflictClaimAvailable)
        assert isinstance(conflict_available.claim, SealedDeadlockClaim)
        self.conflict_available = conflict_available

        self.managers = managers or InMemoryManagerQueueStore()
        item = ManagerItem.for_request(
            request_id="request-1",
            manager_id="manager-a",
            source=FromDeadlock(
                case=_case(),
                cause=DivergentVotes(round=1),
                reason="divergent_votes",
            ),
            created_at=NOW,
            item_id="item-1",
        )
        self.managers.create_or_get_for_request(item)
        escalated_case = self.conflicts.transition_for_claim(
            conflict_available.handle,
            target=_case().escalate("item-1"),
        )
        awaiting_manager = awaiting_conflict.transition(
            AwaitingManager(
                item_id="item-1",
                public_kind="contested",
                handling=HandlingAssignment(
                    kind="manager_item",
                    ref="item-1",
                    due_at=NOW + timedelta(hours=1),
                ),
            ),
            clock=lambda: NOW,
        )
        assert escalated_case.status == "escalated"
        assert self.requests.compare_and_set("request-1", 1, awaiting_conflict, awaiting_manager)

        self.authority = DemoRouteAuthority(self.registry)
        self.deadlines = _DeadlinePolicy()
        self.starter = _Starter()
        self.publisher = _Publisher()
        self.reader = _Reader()
        self.app = self.build_app()

    def build_app(
        self,
        *,
        route_authority: RequestRouteAuthority | None = None,
        generation_factory: Callable[[], object] | None = None,
        request_locks: RequestLockPool | None = None,
        clock: Callable[[], datetime] | None = None,
        deadline_policy: _DeadlinePolicy | None = None,
        starter: _Starter | None = None,
        publisher: _Publisher | None = None,
        central_authorizer: object | None = None,
    ) -> P17DeadlockManagerDispositionApplication:
        return P17DeadlockManagerDispositionApplication(
            requests=self.requests,
            conflicts=self.conflicts,
            managers=self.managers,
            registry=self.registry,
            route_authority=route_authority or self.authority,
            completion_reader=self.reader,
            deadline_policy=deadline_policy or self.deadlines,
            execution_starter=starter or self.starter,
            terminal_publisher=publisher or self.publisher,
            generation_factory=generation_factory or (lambda: "manager-generation"),
            clock=clock or (lambda: NOW),
            request_locks=request_locks,
            central_authorizer=central_authorizer,  # type: ignore[arg-type]
        )


def test_authenticated_deadlock_manager_without_central_authorizer_is_fail_closed() -> None:
    fixture = _Fixture()
    principal = AuthenticatedPrincipal(
        org_id=DEMO_ORG_ID,
        subject_id="manager-a",
        identity_provider="oidc",
        identity_session_id="session-manager-a",
    )

    with pytest.raises(ManagerDispositionNotFoundOrDenied):
        fixture.app.act(
            DismissDeadlocked(
                principal=principal,
                item_id="item-1",
                rationale="우회 거부",
            )
        )

    item = fixture.managers.get("item-1")
    request = fixture.requests.get("request-1")
    assert item is not None and item.status == "open"
    assert request is not None and isinstance(request.state, AwaitingManager)
    assert fixture.managers.deadlock_claim_for_item("item-1") is None


class _DeadlockCentralAuthorizer:
    def __init__(self, *, verified: bool = True) -> None:
        self.verified = verified
        self.calls: list[tuple[object, object, object]] = []
        self.verify_calls = 0

    def authorize(self, principal: object, action: object, resource: object) -> object:
        self.calls.append((principal, action, resource))
        assert type(principal) is AuthenticatedPrincipal
        assert type(resource) is ResourceRef
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,  # type: ignore[arg-type]
            resource=resource,
            roles=("manager",),
            policy_version="policy-v1",
            policy_digest="d" * 64,
        )

    def verify(self, grant: object, principal: object, action: object, resource: object) -> bool:
        del grant, principal, action, resource
        self.verify_calls += 1
        return self.verified


def test_central_deadlock_manager_act_verifies_exact_item_grant_before_write() -> None:
    fixture = _Fixture()
    authorizer = _DeadlockCentralAuthorizer()
    app = fixture.build_app(central_authorizer=authorizer)
    principal = AuthenticatedPrincipal(
        org_id=DEMO_ORG_ID,
        subject_id="manager-a",
        identity_provider="oidc",
        identity_session_id="session-manager-a",
    )

    result = app.act(
        DismissDeadlocked(
            principal=principal,
            item_id="item-1",
            rationale="명시적 거절",
        )
    )

    assert isinstance(result, DeadlockDismissed)
    assert authorizer.verify_calls == 1
    assert authorizer.calls[0][1:] == (
        "manager.act",
        ResourceRef(
            org_id=DEMO_ORG_ID,
            kind="manager_item",
            resource_id="item-1",
            owner_subject_id="manager-a",
        ),
    )


def test_central_deadlock_manager_forged_grant_has_zero_write() -> None:
    fixture = _Fixture()
    app = fixture.build_app(central_authorizer=_DeadlockCentralAuthorizer(verified=False))
    principal = AuthenticatedPrincipal(
        org_id=DEMO_ORG_ID,
        subject_id="manager-a",
        identity_provider="oidc",
        identity_session_id="session-manager-a",
    )

    with pytest.raises(ManagerDispositionNotFoundOrDenied):
        app.act(
            DismissDeadlocked(
                principal=principal,
                item_id="item-1",
                rationale="위조 grant",
            )
        )

    assert fixture.managers.deadlock_claim_for_item("item-1") is None
    item = fixture.managers.get("item-1")
    assert item is not None and item.status == "open"


class _ControlledAuthority:
    def __init__(self, registry: Registry, *, mode: str = "success") -> None:
        self.registry = registry
        self.delegate = DemoRouteAuthority(registry)
        self.mode = mode
        self.grant_calls: list[RequestRouteGrantAssignment] = []
        self.durable_grants = 0
        self.read_calls = 0
        self._mutated = False

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        return self.delegate.authorize(org_id, intent, agent_id)

    def grant_for_request(
        self,
        assignment: RequestRouteGrantAssignment,
    ) -> RequestRouteGrantResult:
        self.grant_calls.append(assignment)
        if self.mode == "reject":
            return RequestRouteGrantRejected(
                idempotency_key=assignment.idempotency_key,
                reason_code="policy_denied",
            )
        if self.mode == "wrong_reject_key":
            return RequestRouteGrantRejected(
                idempotency_key="other-key",
                reason_code="policy_denied",
            )
        if self.mode == "conflict":
            return RequestRouteGrantConflict()
        if self.mode == "exception":
            raise RuntimeError("ambiguous authority result")
        if self.mode == "malformed":
            return object()  # type: ignore[return-value]
        before = self.delegate.authorize_for_request(
            assignment.org_id,
            assignment.request_id,
            assignment.intent,
            assignment.agent_id,
        )
        result = self.delegate.grant_for_request(assignment)
        after = self.delegate.authorize_for_request(
            assignment.org_id,
            assignment.request_id,
            assignment.intent,
            assignment.agent_id,
        )
        if before is None and after is not None:
            self.durable_grants += 1
        if self.mode == "post_write_exception":
            raise RuntimeError("authority response lost after write")
        if self.mode == "tampered_receipt" and isinstance(result, RequestRouteGrantReceipt):
            wrong = result.assignment.model_copy(
                update={
                    "source": FromDeadlockManagerGrant(
                        case_id="case-1",
                        item_id="other-item",
                        by_manager="manager-a",
                    )
                }
            )
            return result.model_copy(update={"assignment": wrong})
        if self.mode == "mutate_after_write" and not self._mutated:
            self.registry.replace_card(_card("refund-card", "owner-b"))
            self._mutated = True
        return result

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None:
        self.read_calls += 1
        if self.mode == "read_exception":
            raise RuntimeError("authority read unavailable")
        if self.mode == "read_none":
            return None
        result = self.delegate.authorize_for_request(org_id, request_id, intent, agent_id)
        if self.mode == "wrong_readback" and result is not None:
            return AuthorityGrant(policy_version="wrong-version")
        if self.mode == "read_subclass" and result is not None:
            subclass = type("AuthorityGrantSubclass", (AuthorityGrant,), {})
            return subclass.model_construct(  # type: ignore[return-value]
                policy_version=result.policy_version
            )
        return result


class _FaultConflictStore(InMemoryConflictDispositionStore):
    def __init__(self, *, point: str, fail_before: bool) -> None:
        super().__init__(
            id_factory=_ids("conflict-generation", "conflict-forward", "mediation-forward")
        )
        self.point = point
        self.fail_before = fail_before
        self.failed = False
        self.mediation_commits = 0
        self.case_commits = 0

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
        if self.point == "mediation" and self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("mediation pre-write")
        before = len(
            [
                entry
                for entry in self.progress_history_for_case("case-1")
                if entry.kind == "mediation_sealed"
            ]
        )
        result = super().record_validated_mediation(
            conflict_handle,
            manager_handle,
            validate=validate,
        )
        after = len(
            [
                entry
                for entry in self.progress_history_for_case("case-1")
                if entry.kind == "mediation_sealed"
            ]
        )
        self.mediation_commits += after - before
        if self.point == "mediation" and not self.failed:
            self.failed = True
            raise RuntimeError("mediation post-write")
        return result

    def transition_for_mediation(
        self,
        handle: ConflictMediationHandle,
        *,
        target: ConflictCase,
    ) -> ConflictCase:
        if self.point == "case" and self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("case pre-write")
        before = self.get_request_case(target.case_id)
        result = super().transition_for_mediation(handle, target=target)
        if before != result:
            self.case_commits += 1
        if self.point == "case" and not self.failed:
            self.failed = True
            raise RuntimeError("case post-write")
        return result


class _FaultManagerStore(InMemoryManagerQueueStore):
    def __init__(self, *, point: str, fail_before: bool) -> None:
        super().__init__()
        self.point = point
        self.fail_before = fail_before
        self.failed = False
        self.seal_commits = 0
        self.resume_commits = 0
        self.resume_reads = 0
        self.resume_writes = 0
        self.resume_mode = "normal"
        self.item_commits = 0
        self.abandon_commits = 0

    def seal_deadlock_claim(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
    ) -> DeadlockManagerSealedClaimAvailable:
        if self.point == "seal" and self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("seal pre-write")
        result = super().seal_deadlock_claim(claim, control_token=control_token)
        self.seal_commits += 1
        if self.point == "seal" and not self.failed:
            self.failed = True
            raise RuntimeError("seal post-write")
        return result

    def abandon_unmutated_deadlock_assign(
        self,
        claim: ReservedDeadlockAssignClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> None:
        if self.point == "abandon" and self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("abandon pre-write")
        before = self.deadlock_claim_for_item(claim.item_id)
        super().abandon_unmutated_deadlock_assign(
            claim,
            control_token=control_token,
            rejection=rejection,
        )
        if before is not None and self.deadlock_claim_for_item(claim.item_id) is None:
            self.abandon_commits += 1
        if self.point == "abandon" and not self.failed:
            self.failed = True
            raise RuntimeError("abandon post-write")

    def record_resume_evidence(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
        evidence: ResumeEvidence,
    ) -> None:
        self.resume_writes += 1
        if self.point == "resume" and self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("resume pre-write")
        before = self.resume_evidence_for_claim(handle)
        super().record_resume_evidence(handle, evidence)
        if before is None:
            self.resume_commits += 1
        if self.point == "resume" and not self.failed:
            self.failed = True
            raise RuntimeError("resume post-write")

    def resume_evidence_for_claim(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
    ) -> ResumeEvidence | None:
        self.resume_reads += 1
        evidence = super().resume_evidence_for_claim(handle)
        if self.resume_mode == "hidden":
            return None
        if self.resume_mode == "tampered" and evidence is not None:
            return evidence.model_copy(
                update={
                    "route": evidence.route.model_copy(
                        update={"authority_version": "tampered-version"}
                    )
                }
            )
        return evidence

    def resolve_for_claim(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
        resolved: ManagerItem,
    ) -> ManagerItem:
        if self.point == "item" and self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("item pre-write")
        before = self.get(resolved.item_id)
        result = super().resolve_for_claim(handle, resolved)
        if before != result:
            self.item_commits += 1
        if self.point == "item" and not self.failed:
            self.failed = True
            raise RuntimeError("item post-write")
        return result


class _FaultRequestStore(InMemoryQuestionRequestStore):
    def __init__(self, *, fail_before: bool) -> None:
        super().__init__()
        self.fail_before = fail_before
        self.failed = False
        self.disposition_commits = 0

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        is_disposition = expected_revision == 2
        if is_disposition and self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("request pre-write")
        result = super().compare_and_set(request_id, expected_revision, current, updated)
        if is_disposition and result:
            self.disposition_commits += 1
        if is_disposition and not self.failed:
            self.failed = True
            raise RuntimeError("request post-write")
        return result


class _TamperManagerStore(InMemoryManagerQueueStore):
    def __init__(self) -> None:
        super().__init__()
        self.mode = "normal"

    def get(self, item_id: str) -> ManagerItem | None:
        item = super().get(item_id)
        if self.mode == "source" and item is not None and isinstance(item.source, FromDeadlock):
            source = item.source
            tampered_case = replace(source.case, question="치환된 원 질문")
            return replace(
                item,
                source=FromDeadlock(
                    case=tampered_case,
                    reason=source.reason,
                    cause=source.cause,
                ),
            )
        return item

    def seal_deadlock_claim(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
    ) -> DeadlockManagerSealedClaimAvailable:
        available = super().seal_deadlock_claim(claim, control_token=control_token)
        if self.mode == "wrong_seal_handle":
            return available.model_copy(
                update={
                    "handle": available.handle.model_copy(
                        update={"forward_token": "wrong-forward-token"}
                    )
                }
            )
        return available

    def deadlock_claim_for_handle(
        self,
        handle: DeadlockManagerSealedClaimHandle,
    ) -> SealedDeadlockAssignClaim | SealedDeadlockDismissClaim:
        if self.mode == "handle_exception":
            raise RuntimeError("manager handle unavailable")
        claim = super().deadlock_claim_for_handle(handle)
        if self.mode == "wrong_handle_claim":
            return claim.model_copy(update={"rationale": "tampered rationale"})
        return claim


class _MutatedControlReturnManagerStore(InMemoryManagerQueueStore):
    def reserve_validated_deadlock_action(
        self,
        item_id: str,
        command: DeadlockManagerDispositionCommand,
        *,
        validate: Callable[
            [ManagerItem],
            ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        ],
    ) -> DeadlockManagerClaimAttempt:
        attempt = super().reserve_validated_deadlock_action(
            item_id,
            command,
            validate=validate,
        )
        if isinstance(attempt, DeadlockManagerClaimAcquired):
            return attempt.model_copy(
                update={
                    "control_token": attempt.control_token.model_copy(
                        update={"token": "same-generation-other-secret"}
                    )
                }
            )
        return attempt


class _MalformedVoidManagerStore(_FaultManagerStore):
    def __init__(self, *, point: str) -> None:
        super().__init__(point="none", fail_before=True)
        self.void_point = point

    def abandon_unmutated_deadlock_assign(
        self,
        claim: ReservedDeadlockAssignClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> None:
        super().abandon_unmutated_deadlock_assign(
            claim,
            control_token=control_token,
            rejection=rejection,
        )
        if self.void_point == "abandon":
            return object()  # type: ignore[return-value]

    def record_resume_evidence(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
        evidence: ResumeEvidence,
    ) -> None:
        super().record_resume_evidence(handle, evidence)
        if self.void_point == "resume":
            return object()  # type: ignore[return-value]


class _TamperConflictStore(InMemoryConflictDispositionStore):
    def __init__(self) -> None:
        super().__init__(
            id_factory=_ids("conflict-generation", "conflict-forward", "mediation-forward")
        )
        self.mode = "normal"

    def sealed_claim_for_case(self, case_id: str) -> SealedConflictClaimAvailable | None:
        available = super().sealed_claim_for_case(case_id)
        if available is None:
            return None
        if self.mode == "source":
            return available.model_copy(
                update={"claim": available.claim.model_copy(update={"request_id": "other"})}
            )
        if self.mode == "wrong_conflict_handle":
            return available.model_copy(
                update={
                    "handle": available.handle.model_copy(
                        update={"forward_token": "wrong-conflict-forward"}
                    )
                }
            )
        return available

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
        available = super().record_validated_mediation(
            conflict_handle,
            manager_handle,
            validate=validate,
        )
        if self.mode == "malformed_mediation":
            return object()  # type: ignore[return-value]
        if self.mode == "subclass_mediation":
            subclass = type(
                "SealedConflictMediationAvailableSubclass",
                (SealedConflictMediationAvailable,),
                {},
            )
            return subclass.model_construct(  # type: ignore[return-value]
                proof=available.proof,
                handle=available.handle,
            )
        return available


class _MalformedCasStore(InMemoryQuestionRequestStore):
    malformed = False

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        won = super().compare_and_set(request_id, expected_revision, current, updated)
        if self.malformed and expected_revision == 2:
            return 1  # type: ignore[return-value]
        return won


class _BlockingCasStore(InMemoryQuestionRequestStore):
    def __init__(self) -> None:
        super().__init__()
        self.block = False
        self.entered = Event()
        self.release = Event()

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        if self.block and expected_revision == 2:
            self.entered.set()
            if not self.release.wait(timeout=3):
                raise RuntimeError("test CAS release timeout")
        return super().compare_and_set(request_id, expected_revision, current, updated)


class _TamperActiveReadStore(InMemoryQuestionRequestStore):
    tamper: str | None = None

    def get(self, request_id: str) -> QuestionRequest | None:
        request = super().get(request_id)
        if (
            request is None
            or self.tamper is None
            or not isinstance(request.state, (AwaitingAnswer, AwaitingApproval))
        ):
            return request
        route = request.state.route
        if self.tamper == "route":
            route = route.model_copy(update={"agent_id": "finance-card"})
        attempt = 2 if self.tamper == "attempt" else request.state.attempt
        return request.model_copy(
            update={"state": request.state.model_copy(update={"route": route, "attempt": attempt})}
        )


def _assign_command(*, agent_id: str = "refund-card") -> AssignDeadlockedOwner:
    return AssignDeadlockedOwner(
        principal=ManagerPrincipal(org_id=DEMO_ORG_ID, subject_id="manager-a"),
        item_id="item-1",
        agent_id=agent_id,
        rationale="환불 담당으로 중재",
    )


def _dismiss_command() -> DismissDeadlocked:
    return DismissDeadlocked(
        principal=ManagerPrincipal(org_id=DEMO_ORG_ID, subject_id="manager-a"),
        item_id="item-1",
        rationale="질문 무효",
    )


def _store_exact_answered_completion(
    fixture: _Fixture,
) -> tuple[QuestionRequest, CompletionBundle, TerminalAnswerAudit]:
    ready = fixture.requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    completed_at = NOW + timedelta(seconds=1)
    answered = ready.transition(
        AnsweredRequest(record_id="record-1"),
        clock=lambda: completed_at,
    )
    assert fixture.requests.compare_and_set(
        "request-1",
        ready.revision,
        ready,
        answered,
    )
    route = ready.state.route
    completion = AnswerCompletion(
        request_id="request-1",
        record_id="record-1",
        text="환불 답변",
        answered_by="owner-a",
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
        answered_by="owner-a",
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
        org_id=DEMO_ORG_ID,
        requester_id="requester",
        attempt=1,
        route=route,
        responsibility=AnswerResponsibilitySnapshot(
            agent_id="refund-card",
            owner_id="owner-a",
        ),
        candidate_mode="full",
        final_mode="full",
        approval=NoApprovalEvidence(
            policy_version=route.authority_version or "missing-authority-version"
        ),
        completed_at=completed_at,
    )
    bundle = CompletionBundle(
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
    fixture.reader.bundle = bundle
    return answered, bundle, audit


def _advance_ready_to_active(
    fixture: _Fixture,
    state_kind: str,
) -> QuestionRequest:
    ready = fixture.requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    route = ready.state.route
    if state_kind == "awaiting_answer":
        target = AwaitingAnswer(
            route=route,
            attempt=1,
            ticket_id="ticket-1",
            handling=HandlingAssignment(
                kind="runtime_ticket",
                ref="ticket-1",
                due_at=NOW + timedelta(hours=1),
            ),
        )
    else:
        target = AwaitingApproval(
            route=route,
            attempt=1,
            draft_ref="draft-1",
            handling=HandlingAssignment(
                kind="approval_item",
                ref="draft-1",
                due_at=NOW + timedelta(hours=1),
            ),
        )
    active = ready.transition(target, clock=lambda: NOW + timedelta(seconds=1))
    assert fixture.requests.compare_and_set(
        "request-1",
        ready.revision,
        ready,
        active,
    )
    return active


def test_Assign은_Authority_evidence_Request_Case_Item_wake를_순서대로_닫는다() -> None:
    fixture = _Fixture(approval=True)

    result = fixture.app.act(
        AssignDeadlockedOwner(
            principal=ManagerPrincipal(org_id=DEMO_ORG_ID, subject_id="manager-a"),
            item_id="item-1",
            agent_id="refund-card",
            rationale="환불 담당으로 중재",
        )
    )
    assert isinstance(result, DeadlockOwnerAssigned)

    assert result == DeadlockOwnerAssigned(
        request_id="request-1",
        case_id="case-1",
        item_id="item-1",
        route=result.route,
        wake=ExecutionStarted(),
    )
    assert result.route.intent == "refund"
    assert result.route.agent_id == "refund-card"
    assert result.route.requires_approval is True
    assert result.route.authority_version == "demo-request-grant-v1"
    request = fixture.requests.get("request-1")
    assert request is not None and request.revision == 3
    assert isinstance(request.state, ReadyToDispatch)
    assert request.state.route == result.route
    evidence = fixture.conflicts.resolution_evidence_for_request("request-1")
    assert evidence == ConflictResolutionEvidence(
        request_id="request-1",
        case_id="case-1",
        org_id=DEMO_ORG_ID,
        intent="refund",
        route=result.route,
        source=FromManagerMediation(item_id="item-1", by_manager="manager-a"),
        supporting=(),
    )
    case = fixture.conflicts.get_request_case("case-1")
    assert case is not None
    assert case.resolution == Resolution(
        intent="refund", primary="refund-card", rationale="환불 담당으로 중재"
    )
    item = fixture.managers.get("item-1")
    assert item is not None
    assert item.resolution == ManagerResolution(
        action=AssignOwner(
            by_manager="manager-a",
            primary="refund-card",
            rationale="환불 담당으로 중재",
        ),
        resolution=Resolution(
            intent="refund", primary="refund-card", rationale="환불 담당으로 중재"
        ),
    )
    claim = fixture.managers.deadlock_claim_for_item("item-1")
    assert claim is not None and claim.kind == "sealed_deadlock_assign"
    handle = fixture.managers.reserve_validated_deadlock_action(
        "item-1",
        AssignDeadlockedOwner(
            principal=ManagerPrincipal(org_id=DEMO_ORG_ID, subject_id="manager-a"),
            item_id="item-1",
            agent_id="refund-card",
            rationale="환불 담당으로 중재",
        ),
        validate=lambda _item: (_ for _ in ()).throw(AssertionError("follower callback")),
    )
    assert handle.kind == "sealed"
    resume = fixture.managers.resume_evidence_for_claim(handle.handle)
    assert resume is not None
    assert (resume.from_revision, resume.to_revision, resume.route) == (2, 3, result.route)
    assert fixture.starter.calls == 1
    assert fixture.publisher.calls == 0


def test_Dismiss는_Authority_evidence_Runtime없이_Request_Case_Item_publish를_닫는다() -> None:
    fixture = _Fixture()

    result = fixture.app.act(
        DismissDeadlocked(
            principal=ManagerPrincipal(org_id=DEMO_ORG_ID, subject_id="manager-a"),
            item_id="item-1",
            rationale="질문 무효",
        )
    )
    assert isinstance(result, DeadlockDismissed)

    assert result == DeadlockDismissed(
        request_id="request-1",
        case_id="case-1",
        item_id="item-1",
        delivery=TerminalPublished(),
    )
    request = fixture.requests.get("request-1")
    assert request is not None and request.revision == 3
    assert request.state == DeclinedRequest(reason_code="manager_declined")
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None
    case = fixture.conflicts.get_request_case("case-1")
    assert case is not None and case.status == "declined"
    item = fixture.managers.get("item-1")
    assert item is not None
    assert item.resolution == ManagerResolution(
        action=Dismiss(by_manager="manager-a", rationale="질문 무효")
    )
    assert (
        fixture.authority.authorize_for_request(DEMO_ORG_ID, "request-1", "refund", "refund-card")
        is None
    )
    assert fixture.starter.calls == 0
    assert fixture.publisher.calls == 1


def test_Assign은_exact_Deadlock_Manager_provenance와_key를_Authority에_쓴다() -> None:
    fixture = _Fixture()
    authority = _ControlledAuthority(fixture.registry)
    app = fixture.build_app(route_authority=authority)

    result = app.act(_assign_command())

    assert isinstance(result, DeadlockOwnerAssigned)
    assert authority.grant_calls == [
        RequestRouteGrantAssignment(
            org_id=DEMO_ORG_ID,
            request_id="request-1",
            intent="refund",
            agent_id="refund-card",
            source=FromDeadlockManagerGrant(
                case_id="case-1",
                item_id="item-1",
                by_manager="manager-a",
            ),
            idempotency_key="manager-disposition:item-1",
        )
    ]
    assert authority.read_calls == 1


def test_Authority_typed_write0_reject만_reserved_Assign을_abandon한다() -> None:
    fixture = _Fixture()
    authority = _ControlledAuthority(fixture.registry, mode="reject")
    app = fixture.build_app(route_authority=authority)
    before_request = fixture.requests.get("request-1")
    before_case = fixture.conflicts.get_request_case("case-1")

    with pytest.raises(ManagerDispositionInvalid):
        app.act(_assign_command())

    assert fixture.managers.deadlock_claim_for_item("item-1") is None
    assert fixture.requests.get("request-1") == before_request
    assert fixture.conflicts.get_request_case("case-1") == before_case
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None


def test_reservation_return의_same_generation_other_secret은_Authority_write전에_거부한다() -> None:
    managers = _MutatedControlReturnManagerStore()
    fixture = _Fixture(managers=managers)
    authority = _ControlledAuthority(fixture.registry)
    app = fixture.build_app(route_authority=authority)

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    assert authority.grant_calls == []
    assert authority.durable_grants == 0
    assert isinstance(
        managers.deadlock_claim_for_item("item-1"),
        ReservedDeadlockAssignClaim,
    )
    request = fixture.requests.get("request-1")
    case = fixture.conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert request is not None and request.revision == 2
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert fixture.starter.calls == 0


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("conflict", ManagerDispositionConflict),
        ("exception", ManagerDispositionDependency),
        ("malformed", ManagerDispositionIntegrity),
        ("tampered_receipt", ManagerDispositionIntegrity),
        ("wrong_reject_key", ManagerDispositionIntegrity),
        ("wrong_readback", ManagerDispositionIntegrity),
        ("post_write_exception", ManagerDispositionDependency),
        ("read_exception", ManagerDispositionDependency),
        ("read_none", ManagerDispositionIntegrity),
        ("read_subclass", ManagerDispositionIntegrity),
    ],
)
def test_Authority_ambiguous_or_mismatch는_claim을_seal하고_same_command만_복구한다(
    mode: str,
    error_type: type[Exception],
) -> None:
    fixture = _Fixture()
    authority = _ControlledAuthority(fixture.registry, mode=mode)
    app = fixture.build_app(route_authority=authority)
    before_request = fixture.requests.get("request-1")
    before_case = fixture.conflicts.get_request_case("case-1")

    with pytest.raises(error_type):
        app.act(_assign_command())

    claim = fixture.managers.deadlock_claim_for_item("item-1")
    assert claim is not None and claim.kind == "sealed_deadlock_assign"
    assert fixture.requests.get("request-1") == before_request
    assert fixture.conflicts.get_request_case("case-1") == before_case
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None

    authority.mode = "success"
    recovered = app.act(_assign_command())
    assert isinstance(recovered, DeadlockOwnerAssigned)
    assert authority.durable_grants == 1


@pytest.mark.parametrize("fail_before", [True, False])
def test_typed_write0_reject_abandon_pre_post_fault는_한_delete로_복구한다(
    fail_before: bool,
) -> None:
    managers = _FaultManagerStore(point="abandon", fail_before=fail_before)
    fixture = _Fixture(managers=managers)
    authority = _ControlledAuthority(fixture.registry, mode="reject")
    app = fixture.build_app(route_authority=authority)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            app.act(_assign_command())
        claim = managers.deadlock_claim_for_item("item-1")
        assert claim is not None and claim.kind == "reserved_deadlock_assign"
        with pytest.raises(ManagerDispositionInvalid):
            app.act(_assign_command())
    else:
        with pytest.raises(ManagerDispositionInvalid):
            app.act(_assign_command())

    assert managers.deadlock_claim_for_item("item-1") is None
    assert managers.abandon_commits == 1
    request = fixture.requests.get("request-1")
    case = fixture.conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert request is not None and request.revision == 2
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None


@pytest.mark.parametrize("agent_id", ["outside-card", "finance-card"])
def test_Assign은_현재도_유효한_원_candidate와_snapshot_Owner만_허용한다(
    agent_id: str,
) -> None:
    fixture = _Fixture()
    if agent_id == "outside-card":
        fixture.registry.register(_card("outside-card", "owner-a"))
    else:
        fixture.registry.replace_card(_card("finance-card", "owner-a"))
    before_request = fixture.requests.get("request-1")

    with pytest.raises(ManagerDispositionInvalid):
        fixture.app.act(_assign_command(agent_id=agent_id))

    assert fixture.managers.deadlock_claim_for_item("item-1") is None
    assert fixture.requests.get("request-1") == before_request


def test_Authority_write뒤_Registry_Owner_drift는_sealed를_보존하고_terminal_write0이다() -> None:
    fixture = _Fixture()
    authority = _ControlledAuthority(fixture.registry, mode="mutate_after_write")
    app = fixture.build_app(route_authority=authority)
    before_request = fixture.requests.get("request-1")
    before_case = fixture.conflicts.get_request_case("case-1")

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    claim = fixture.managers.deadlock_claim_for_item("item-1")
    assert claim is not None and claim.kind == "sealed_deadlock_assign"
    assert fixture.requests.get("request-1") == before_request
    assert fixture.conflicts.get_request_case("case-1") == before_case
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None

    fixture.registry.replace_card(_card("refund-card", "owner-a"))
    authority.mode = "success"
    assert isinstance(app.act(_assign_command()), DeadlockOwnerAssigned)


def test_sealed_winner뒤_다른_target_rationale_action은_conflict이고_다른_principal은_forbidden이다() -> (
    None
):
    fixture = _Fixture()
    assert isinstance(fixture.app.act(_assign_command()), DeadlockOwnerAssigned)

    alternatives: tuple[AssignDeadlockedOwner | DismissDeadlocked, ...] = (
        _assign_command(agent_id="finance-card"),
        AssignDeadlockedOwner(
            principal=ManagerPrincipal(org_id=DEMO_ORG_ID, subject_id="manager-a"),
            item_id="item-1",
            agent_id="refund-card",
            rationale="다른 중재 근거",
        ),
        _dismiss_command(),
    )
    for command in alternatives:
        with pytest.raises(ManagerDispositionConflict):
            fixture.app.act(command)

    with pytest.raises(ManagerDispositionForbidden):
        fixture.app.act(
            AssignDeadlockedOwner(
                principal=ManagerPrincipal(org_id=DEMO_ORG_ID, subject_id="owner-a"),
                item_id="item-1",
                agent_id="refund-card",
                rationale="환불 담당으로 중재",
            )
        )


def test_유효_candidate가_0이어도_Dismiss는_Authority없이_정확히_닫는다() -> None:
    fixture = _Fixture()
    fixture.registry.replace_card(_card("refund-card", "missing-owner"))
    fixture.registry.replace_card(_card("finance-card", "missing-owner"))
    authority = _ControlledAuthority(fixture.registry)
    app = fixture.build_app(route_authority=authority)

    with pytest.raises(ManagerDispositionInvalid):
        app.act(_assign_command())
    dismissed = app.act(_dismiss_command())

    assert isinstance(dismissed, DeadlockDismissed)
    assert authority.grant_calls == []
    assert authority.read_calls == 0
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None


@pytest.mark.parametrize("fail_before", [True, False])
def test_mediation_pre_post_write_fault는_same_Assign_retry로_한_proof만_남긴다(
    fail_before: bool,
) -> None:
    conflicts = _FaultConflictStore(point="mediation", fail_before=fail_before)
    fixture = _Fixture(conflicts=conflicts)

    with pytest.raises(ManagerDispositionDependency):
        fixture.app.act(_assign_command())

    request = fixture.requests.get("request-1")
    assert request is not None and request.revision == 2
    if fail_before:
        assert conflicts.resolution_evidence_for_request("request-1") is None
    else:
        assert conflicts.resolution_evidence_for_request("request-1") is not None
    result = fixture.app.act(_assign_command())
    assert isinstance(result, DeadlockOwnerAssigned)
    assert conflicts.mediation_commits == 1
    assert (
        len(
            [
                entry
                for entry in conflicts.progress_history_for_case("case-1")
                if entry.kind == "mediation_sealed"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("fail_before", [True, False])
def test_Request_CAS_pre_write는_retry하고_post_response_loss는_같은호출에서_보수한다(
    fail_before: bool,
) -> None:
    requests = _FaultRequestStore(fail_before=fail_before)
    fixture = _Fixture(requests=requests)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            fixture.app.act(_assign_command())
        current = requests.get("request-1")
        assert current is not None and current.revision == 2
        result = fixture.app.act(_assign_command())
    else:
        result = fixture.app.act(_assign_command())

    assert isinstance(result, DeadlockOwnerAssigned)
    current = requests.get("request-1")
    assert current is not None and current.revision == 3
    assert requests.disposition_commits == 1


@pytest.mark.parametrize("fail_before", [True, False])
def test_Manager_seal_pre_write는_control_recovery로_retry하고_post_loss는_forward한다(
    fail_before: bool,
) -> None:
    managers = _FaultManagerStore(point="seal", fail_before=fail_before)
    fixture = _Fixture(managers=managers)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            fixture.app.act(_assign_command())
        assert managers.deadlock_claim_for_item("item-1") is not None
        result = fixture.app.act(_assign_command())
    else:
        result = fixture.app.act(_assign_command())

    assert isinstance(result, DeadlockOwnerAssigned)
    assert managers.seal_commits == 1


@pytest.mark.parametrize("fail_before", [True, False])
def test_ResumeEvidence_pre_write는_retry하고_post_loss는_exact_readback으로_보수한다(
    fail_before: bool,
) -> None:
    managers = _FaultManagerStore(point="resume", fail_before=fail_before)
    fixture = _Fixture(managers=managers)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            fixture.app.act(_assign_command())
        request = fixture.requests.get("request-1")
        assert request is not None and request.revision == 3
        assert fixture.conflicts.get_request_case("case-1") is not None
        result = fixture.app.act(_assign_command())
    else:
        result = fixture.app.act(_assign_command())

    assert isinstance(result, DeadlockOwnerAssigned)
    assert managers.resume_commits == 1


@pytest.mark.parametrize("fail_before", [True, False])
def test_Case_terminal_pre_write는_retry하고_post_loss는_exact_target으로_보수한다(
    fail_before: bool,
) -> None:
    conflicts = _FaultConflictStore(point="case", fail_before=fail_before)
    fixture = _Fixture(conflicts=conflicts)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            fixture.app.act(_assign_command())
        current = conflicts.get_request_case("case-1")
        assert current is not None and current.status == "escalated"
        result = fixture.app.act(_assign_command())
    else:
        result = fixture.app.act(_assign_command())

    assert isinstance(result, DeadlockOwnerAssigned)
    assert conflicts.case_commits == 1


@pytest.mark.parametrize("fail_before", [True, False])
def test_ManagerItem_terminal_pre_write는_retry하고_post_loss는_exact_target으로_보수한다(
    fail_before: bool,
) -> None:
    managers = _FaultManagerStore(point="item", fail_before=fail_before)
    fixture = _Fixture(managers=managers)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            fixture.app.act(_assign_command())
        current = managers.get("item-1")
        assert current is not None and current.status == "open"
        result = fixture.app.act(_assign_command())
    else:
        result = fixture.app.act(_assign_command())

    assert isinstance(result, DeadlockOwnerAssigned)
    assert managers.item_commits == 1


def _assert_dismiss_never_uses_assign_dependencies(
    fixture: _Fixture,
    *,
    authority: _ControlledAuthority,
    managers: _FaultManagerStore,
) -> None:
    assert authority.grant_calls == []
    assert authority.read_calls == 0
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None
    assert managers.resume_reads == 0
    assert managers.resume_writes == 0
    assert fixture.starter.calls == 0


@pytest.mark.parametrize("fail_before", [True, False])
def test_Dismiss_mediation_pre_post_fault는_한_proof로_복구하고_Assign_dependency를_쓰지_않는다(
    fail_before: bool,
) -> None:
    conflicts = _FaultConflictStore(point="mediation", fail_before=fail_before)
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(conflicts=conflicts, managers=managers)
    authority = _ControlledAuthority(fixture.registry)
    app = fixture.build_app(route_authority=authority)

    with pytest.raises(ManagerDispositionDependency):
        app.act(_dismiss_command())

    current = fixture.requests.get("request-1")
    assert current is not None and current.revision == 2
    result = app.act(_dismiss_command())

    assert isinstance(result, DeadlockDismissed)
    assert conflicts.mediation_commits == 1
    assert (
        len(
            [
                entry
                for entry in conflicts.progress_history_for_case("case-1")
                if entry.kind == "mediation_sealed"
            ]
        )
        == 1
    )
    _assert_dismiss_never_uses_assign_dependencies(
        fixture,
        authority=authority,
        managers=managers,
    )


@pytest.mark.parametrize("fail_before", [True, False])
def test_Dismiss_Manager_seal_pre_post_fault는_forward_repair하고_Assign_dependency는_0이다(
    fail_before: bool,
) -> None:
    managers = _FaultManagerStore(point="seal", fail_before=fail_before)
    fixture = _Fixture(managers=managers)
    authority = _ControlledAuthority(fixture.registry)
    app = fixture.build_app(route_authority=authority)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            app.act(_dismiss_command())
        result = app.act(_dismiss_command())
    else:
        result = app.act(_dismiss_command())

    assert isinstance(result, DeadlockDismissed)
    assert managers.seal_commits == 1
    _assert_dismiss_never_uses_assign_dependencies(
        fixture,
        authority=authority,
        managers=managers,
    )


@pytest.mark.parametrize("fail_before", [True, False])
def test_Dismiss_Request_CAS_pre_post_fault는_exact_Declined로_한번만_commit한다(
    fail_before: bool,
) -> None:
    requests = _FaultRequestStore(fail_before=fail_before)
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(requests=requests, managers=managers)
    authority = _ControlledAuthority(fixture.registry)
    app = fixture.build_app(route_authority=authority)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            app.act(_dismiss_command())
        current = requests.get("request-1")
        assert current is not None and current.revision == 2
        result = app.act(_dismiss_command())
    else:
        result = app.act(_dismiss_command())

    assert isinstance(result, DeadlockDismissed)
    current = requests.get("request-1")
    assert current is not None and current.revision == 3
    assert current.state == DeclinedRequest(reason_code="manager_declined")
    assert requests.disposition_commits == 1
    _assert_dismiss_never_uses_assign_dependencies(
        fixture,
        authority=authority,
        managers=managers,
    )


@pytest.mark.parametrize("fail_before", [True, False])
def test_Dismiss_Case_terminal_pre_post_fault는_exact_declined로_한번만_commit한다(
    fail_before: bool,
) -> None:
    conflicts = _FaultConflictStore(point="case", fail_before=fail_before)
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(conflicts=conflicts, managers=managers)
    authority = _ControlledAuthority(fixture.registry)
    app = fixture.build_app(route_authority=authority)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            app.act(_dismiss_command())
        current = conflicts.get_request_case("case-1")
        assert current is not None and current.status == "escalated"
        result = app.act(_dismiss_command())
    else:
        result = app.act(_dismiss_command())

    assert isinstance(result, DeadlockDismissed)
    assert conflicts.case_commits == 1
    _assert_dismiss_never_uses_assign_dependencies(
        fixture,
        authority=authority,
        managers=managers,
    )


@pytest.mark.parametrize("fail_before", [True, False])
def test_Dismiss_ManagerItem_terminal_pre_post_fault는_exact_resolution로_한번만_commit한다(
    fail_before: bool,
) -> None:
    managers = _FaultManagerStore(point="item", fail_before=fail_before)
    fixture = _Fixture(managers=managers)
    authority = _ControlledAuthority(fixture.registry)
    app = fixture.build_app(route_authority=authority)

    if fail_before:
        with pytest.raises(ManagerDispositionDependency):
            app.act(_dismiss_command())
        current = managers.get("item-1")
        assert current is not None and current.status == "open"
        result = app.act(_dismiss_command())
    else:
        result = app.act(_dismiss_command())

    assert isinstance(result, DeadlockDismissed)
    assert managers.item_commits == 1
    _assert_dismiss_never_uses_assign_dependencies(
        fixture,
        authority=authority,
        managers=managers,
    )


def test_Assign_wake_exception은_Deferred를_반환하고_same_action_retry는_terminal에서_재호출한다() -> (
    None
):
    managers = _FaultManagerStore(point="none", fail_before=True)
    conflicts = _FaultConflictStore(point="none", fail_before=True)
    requests = _FaultRequestStore(fail_before=False)
    fixture = _Fixture(requests=requests, conflicts=conflicts, managers=managers)
    starter = _Starter(mode="exception")
    app = fixture.build_app(starter=starter)

    first = app.act(_assign_command())

    assert isinstance(first, DeadlockOwnerAssigned)
    assert first.wake == ExecutionDeferred(reason_code="submission_failed")
    starter.mode = "already"
    second = app.act(_assign_command())
    assert isinstance(second, DeadlockOwnerAssigned)
    assert second.wake == ExecutionAlreadyRunning()
    assert starter.calls == 2
    assert requests.disposition_commits == 1
    assert conflicts.mediation_commits == 1
    assert conflicts.case_commits == 1
    assert managers.resume_commits == 1
    assert managers.item_commits == 1


@pytest.mark.parametrize("mode", ["malformed", "subclass"])
def test_Assign_wake_adapter는_exact_closed_return만_허용하고_terminal은_복구가능하다(
    mode: str,
) -> None:
    fixture = _Fixture()
    starter = _Starter(mode=mode)
    app = fixture.build_app(starter=starter)

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    request = fixture.requests.get("request-1")
    case = fixture.conflicts.get_request_case("case-1")
    item = fixture.managers.get("item-1")
    assert request is not None and request.revision == 3
    assert case is not None and case.status == "resolved"
    assert item is not None and item.status == "resolved"
    starter.mode = "already"
    assert isinstance(app.act(_assign_command()), DeadlockOwnerAssigned)


def test_Dismiss_publish_exception은_Deferred를_반환하고_same_action_retry는_terminal에서_재호출한다() -> (
    None
):
    managers = _FaultManagerStore(point="none", fail_before=True)
    conflicts = _FaultConflictStore(point="none", fail_before=True)
    requests = _FaultRequestStore(fail_before=False)
    fixture = _Fixture(requests=requests, conflicts=conflicts, managers=managers)
    publisher = _Publisher(mode="exception")
    app = fixture.build_app(publisher=publisher)

    first = app.act(_dismiss_command())

    assert isinstance(first, DeadlockDismissed)
    assert first.delivery == TerminalDeferred(reason_code="publish_failed")
    publisher.mode = "already"
    second = app.act(_dismiss_command())
    assert isinstance(second, DeadlockDismissed)
    assert second.delivery == TerminalAlreadyPublished()
    assert publisher.calls == 2
    assert requests.disposition_commits == 1
    assert conflicts.mediation_commits == 1
    assert conflicts.case_commits == 1
    assert managers.item_commits == 1
    assert managers.resume_reads == 0
    assert managers.resume_writes == 0


@pytest.mark.parametrize("mode", ["malformed", "subclass"])
def test_Dismiss_publish_adapter는_exact_closed_return만_허용하고_terminal은_복구가능하다(
    mode: str,
) -> None:
    fixture = _Fixture()
    publisher = _Publisher(mode=mode)
    app = fixture.build_app(publisher=publisher)

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_dismiss_command())

    request = fixture.requests.get("request-1")
    case = fixture.conflicts.get_request_case("case-1")
    item = fixture.managers.get("item-1")
    assert request is not None and request.revision == 3
    assert case is not None and case.status == "declined"
    assert item is not None and item.status == "resolved"
    publisher.mode = "already"
    assert isinstance(app.act(_dismiss_command()), DeadlockDismissed)


def _assert_preflight_write_zero(
    fixture: _Fixture,
    authority: _ControlledAuthority,
) -> None:
    request = fixture.requests.get("request-1")
    case = fixture.conflicts.get_request_case("case-1")
    item = fixture.managers.get("item-1")
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingManager)
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert fixture.managers.deadlock_claim_for_item("item-1") is None
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None
    assert authority.grant_calls == []
    assert authority.read_calls == 0
    assert fixture.starter.calls == 0
    assert fixture.publisher.calls == 0


@pytest.mark.parametrize("bad_now", [datetime(2026, 7, 13, 18, 0), NOW - timedelta(seconds=1)])
def test_Assign_bad_clock는_모든_write전에_거부되고_같은_app을_poison하지_않는다(
    bad_now: datetime,
) -> None:
    fixture = _Fixture()
    authority = _ControlledAuthority(fixture.registry)
    current = [bad_now]
    app = fixture.build_app(route_authority=authority, clock=lambda: current[0])

    with pytest.raises(ManagerDispositionDependency):
        app.act(_assign_command())

    _assert_preflight_write_zero(fixture, authority)
    current[0] = NOW
    assert isinstance(app.act(_assign_command()), DeadlockOwnerAssigned)


@pytest.mark.parametrize("bad_now", [datetime(2026, 7, 13, 18, 0), NOW - timedelta(seconds=1)])
def test_Dismiss_bad_clock는_모든_write전에_거부되고_같은_app을_poison하지_않는다(
    bad_now: datetime,
) -> None:
    fixture = _Fixture()
    authority = _ControlledAuthority(fixture.registry)
    current = [bad_now]
    app = fixture.build_app(route_authority=authority, clock=lambda: current[0])

    with pytest.raises(ManagerDispositionDependency):
        app.act(_dismiss_command())

    _assert_preflight_write_zero(fixture, authority)
    current[0] = NOW
    assert isinstance(app.act(_dismiss_command()), DeadlockDismissed)


@pytest.mark.parametrize("mode", ["naive", "regressing", "exception", "malformed"])
def test_Assign_bad_deadline는_모든_write전에_거부되고_policy_repair뒤_성공한다(
    mode: str,
) -> None:
    fixture = _Fixture()
    authority = _ControlledAuthority(fixture.registry)
    deadlines = _DeadlinePolicy(mode=mode)
    app = fixture.build_app(route_authority=authority, deadline_policy=deadlines)

    with pytest.raises(ManagerDispositionDependency):
        app.act(_assign_command())

    _assert_preflight_write_zero(fixture, authority)
    deadlines.mode = "valid"
    assert isinstance(app.act(_assign_command()), DeadlockOwnerAssigned)


@pytest.mark.parametrize("side", ["manager_source", "conflict_source"])
def test_original_FromDeadlock_or_Conflict_source_tamper는_claim과_Authority_write0이다(
    side: str,
) -> None:
    managers = _TamperManagerStore()
    conflicts = _TamperConflictStore()
    fixture = _Fixture(managers=managers, conflicts=conflicts)
    authority = _ControlledAuthority(fixture.registry)
    app = fixture.build_app(route_authority=authority)
    if side == "manager_source":
        managers.mode = "source"
    else:
        conflicts.mode = "source"

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    assert managers.deadlock_claim_for_item("item-1") is None
    assert authority.grant_calls == []
    assert conflicts.resolution_evidence_for_request("request-1") is None
    request = fixture.requests.get("request-1")
    assert request is not None and request.revision == 2


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("wrong_seal_handle", ManagerDispositionIntegrity),
        ("wrong_handle_claim", ManagerDispositionIntegrity),
        ("handle_exception", ManagerDispositionDependency),
    ],
)
def test_Manager_full_handle_readback_tamper는_mediation전_거부되고_same_action으로_복구한다(
    mode: str,
    error_type: type[Exception],
) -> None:
    managers = _TamperManagerStore()
    fixture = _Fixture(managers=managers)
    managers.mode = mode
    app = fixture.app

    with pytest.raises(error_type):
        app.act(_assign_command())

    request = fixture.requests.get("request-1")
    assert request is not None and request.revision == 2
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None
    assert not any(
        entry.kind == "mediation_sealed"
        for entry in fixture.conflicts.progress_history_for_case("case-1")
    )
    managers.mode = "normal"
    assert isinstance(app.act(_assign_command()), DeadlockOwnerAssigned)


def test_wrong_Conflict_full_handle는_proof와_Request_write전에_거부되고_복구된다() -> None:
    conflicts = _TamperConflictStore()
    fixture = _Fixture(conflicts=conflicts)
    conflicts.mode = "wrong_conflict_handle"

    with pytest.raises(ManagerDispositionIntegrity):
        fixture.app.act(_assign_command())

    request = fixture.requests.get("request-1")
    assert request is not None and request.revision == 2
    assert (
        fixture.authority.authorize_for_request(
            DEMO_ORG_ID,
            "request-1",
            "refund",
            "refund-card",
        )
        is None
    )
    assert conflicts.resolution_evidence_for_request("request-1") is None
    assert not any(
        entry.kind == "mediation_sealed" for entry in conflicts.progress_history_for_case("case-1")
    )
    conflicts.mode = "normal"
    assert isinstance(fixture.app.act(_assign_command()), DeadlockOwnerAssigned)


@pytest.mark.parametrize("mode", ["malformed_mediation", "subclass_mediation"])
def test_mediation_port는_exact_available만_허용하고_저장된_proof로_forward_repair한다(
    mode: str,
) -> None:
    conflicts = _TamperConflictStore()
    fixture = _Fixture(conflicts=conflicts)
    conflicts.mode = mode

    with pytest.raises(ManagerDispositionIntegrity):
        fixture.app.act(_assign_command())

    request = fixture.requests.get("request-1")
    assert request is not None and request.revision == 2
    assert conflicts.resolution_evidence_for_request("request-1") is not None
    conflicts.mode = "normal"
    assert isinstance(fixture.app.act(_assign_command()), DeadlockOwnerAssigned)
    assert (
        len(
            [
                entry
                for entry in conflicts.progress_history_for_case("case-1")
                if entry.kind == "mediation_sealed"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("action", ["assign", "dismiss"])
def test_Request_CAS_port의_int_true는_bool로_수용하지_않고_exact_state에서_복구한다(
    action: str,
) -> None:
    requests = _MalformedCasStore()
    fixture = _Fixture(requests=requests)
    requests.malformed = True
    command = _assign_command() if action == "assign" else _dismiss_command()

    with pytest.raises(ManagerDispositionIntegrity):
        fixture.app.act(command)

    current = requests.get("request-1")
    case = fixture.conflicts.get_request_case("case-1")
    item = fixture.managers.get("item-1")
    assert current is not None and current.revision == 3
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    requests.malformed = False
    recovered = fixture.app.act(command)
    if action == "assign":
        assert isinstance(recovered, DeadlockOwnerAssigned)
    else:
        assert isinstance(recovered, DeadlockDismissed)


def test_ResumeEvidence_port의_malformed_non_None은_durable_write뒤에도_Integrity이다() -> None:
    managers = _MalformedVoidManagerStore(point="resume")
    fixture = _Fixture(managers=managers)

    with pytest.raises(ManagerDispositionIntegrity):
        fixture.app.act(_assign_command())

    request = fixture.requests.get("request-1")
    case = fixture.conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert request is not None and request.revision == 3
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert managers.resume_commits == 1
    managers.void_point = "none"
    assert isinstance(fixture.app.act(_assign_command()), DeadlockOwnerAssigned)
    assert managers.resume_commits == 1


def test_abandon_port의_malformed_non_None은_durable_delete뒤에도_Integrity이다() -> None:
    managers = _MalformedVoidManagerStore(point="abandon")
    fixture = _Fixture(managers=managers)
    authority = _ControlledAuthority(fixture.registry, mode="reject")
    app = fixture.build_app(route_authority=authority)

    with pytest.raises(ManagerDispositionIntegrity):
        app.act(_assign_command())

    assert managers.deadlock_claim_for_item("item-1") is None
    assert managers.abandon_commits == 1
    request = fixture.requests.get("request-1")
    case = fixture.conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert request is not None and request.revision == 2
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert fixture.conflicts.resolution_evidence_for_request("request-1") is None


def _run_32(
    apps: list[P17DeadlockManagerDispositionApplication],
    commands: list[AssignDeadlockedOwner | DismissDeadlocked],
) -> list[object]:
    barrier = Barrier(32)

    def invoke(index: int) -> object:
        barrier.wait()
        try:
            return apps[index].act(commands[index])
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=32) as executor:
        return list(executor.map(invoke, range(32)))


@pytest.mark.parametrize("action", ["assign", "dismiss"])
@pytest.mark.parametrize("scope", ["one_app", "cross_app"])
def test_same_action_32way는_한_durable_terminal로_수렴하고_lock_pool을_회수한다(
    action: str,
    scope: str,
) -> None:
    requests = _FaultRequestStore(fail_before=False)
    conflicts = _FaultConflictStore(point="none", fail_before=True)
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(requests=requests, conflicts=conflicts, managers=managers)
    authority = _ControlledAuthority(fixture.registry)
    locks = RequestLockPool()
    first = fixture.build_app(route_authority=authority, request_locks=locks)
    if scope == "one_app":
        apps = [first] * 32
    else:
        apps = [
            fixture.build_app(route_authority=authority, request_locks=locks) for _ in range(32)
        ]
    command = _assign_command() if action == "assign" else _dismiss_command()

    results = _run_32(apps, [command] * 32)

    expected_type = DeadlockOwnerAssigned if action == "assign" else DeadlockDismissed
    assert all(isinstance(result, expected_type) for result in results)
    assert requests.disposition_commits == 1
    assert conflicts.mediation_commits == 1
    assert conflicts.case_commits == 1
    assert managers.item_commits == 1
    assert managers.seal_commits == 1
    assert locks.active_count == 0
    assert all(app.request_lock_count == 0 for app in apps)
    assert (
        len(
            [
                entry
                for entry in conflicts.progress_history_for_case("case-1")
                if entry.kind == "mediation_sealed"
            ]
        )
        == 1
    )
    if action == "assign":
        assert authority.durable_grants == 1
        assert managers.resume_commits == 1
        assert conflicts.resolution_evidence_for_request("request-1") is not None
    else:
        assert authority.durable_grants == 0
        assert authority.grant_calls == []
        assert managers.resume_reads == 0
        assert managers.resume_writes == 0
        assert conflicts.resolution_evidence_for_request("request-1") is None


def test_mixed_Assign_Dismiss_cross_app_32way는_first_winner만_terminal이고_lock_pool은_0이다() -> (
    None
):
    requests = _FaultRequestStore(fail_before=False)
    conflicts = _FaultConflictStore(point="none", fail_before=True)
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(requests=requests, conflicts=conflicts, managers=managers)
    authority = _ControlledAuthority(fixture.registry)
    locks = RequestLockPool()
    apps = [fixture.build_app(route_authority=authority, request_locks=locks) for _ in range(32)]
    commands: list[AssignDeadlockedOwner | DismissDeadlocked] = [
        *[_assign_command() for _ in range(16)],
        *[_dismiss_command() for _ in range(16)],
    ]

    results = _run_32(apps, commands)

    successes = [
        result
        for result in results
        if isinstance(result, (DeadlockOwnerAssigned, DeadlockDismissed))
    ]
    conflicts_returned = [
        result for result in results if isinstance(result, ManagerDispositionConflict)
    ]
    assert len(successes) == 16
    assert len(conflicts_returned) == 16
    assert len({type(result) for result in successes}) == 1
    assert requests.disposition_commits == 1
    assert conflicts.mediation_commits == 1
    assert conflicts.case_commits == 1
    assert managers.item_commits == 1
    assert managers.seal_commits == 1
    assert locks.active_count == 0
    assert all(app.request_lock_count == 0 for app in apps)
    request = requests.get("request-1")
    case = conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert request is not None and request.revision == 3
    assert case is not None and item is not None and item.status == "resolved"
    if isinstance(successes[0], DeadlockOwnerAssigned):
        assert isinstance(request.state, ReadyToDispatch)
        assert case.status == "resolved"
        assert authority.durable_grants == 1
        assert managers.resume_commits == 1
    else:
        assert request.state == DeclinedRequest(reason_code="manager_declined")
        assert case.status == "declined"
        assert authority.grant_calls == []
        assert managers.resume_reads == 0
        assert managers.resume_writes == 0


def test_blocking_Request_CAS는_Registry_mutation을_막지_않고_post_CAS_drift를_잡는다() -> None:
    requests = _BlockingCasStore()
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(requests=requests, managers=managers)
    requests.block = True

    with ThreadPoolExecutor(max_workers=2) as executor:
        disposition = executor.submit(fixture.app.act, _assign_command())
        assert requests.entered.wait(timeout=1)
        mutation = executor.submit(
            fixture.registry.replace_card,
            _card("refund-card", "owner-b"),
        )
        mutation.result(timeout=1)
        requests.release.set()
        with pytest.raises(ManagerDispositionIntegrity):
            disposition.result(timeout=2)

    request = requests.get("request-1")
    case = fixture.conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert request is not None and request.revision == 3
    assert isinstance(request.state, ReadyToDispatch)
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert managers.resume_reads == 0
    assert managers.resume_writes == 0
    assert fixture.starter.calls == 0
    assert fixture.app.request_lock_count == 0

    requests.block = False
    fixture.registry.replace_card(_card("refund-card", "owner-a"))
    assert isinstance(fixture.app.act(_assign_command()), DeadlockOwnerAssigned)


def test_Answered_terminal_retry는_ResumeEvidence와_same_Completion을_모두_exact검증한다() -> None:
    fixture = _Fixture()
    first = fixture.app.act(_assign_command())
    assert isinstance(first, DeadlockOwnerAssigned)
    _answered, exact, audit = _store_exact_answered_completion(fixture)

    assert isinstance(fixture.app.act(_assign_command()), DeadlockOwnerAssigned)

    tampered_audits = (
        audit.model_copy(
            update={
                "route": audit.route.model_copy(update={"authority_version": "tampered-version"})
            }
        ),
        audit.model_copy(update={"attempt": 2}),
        audit.model_copy(update={"record_id": "other-record"}),
    )
    for tampered in tampered_audits:
        fixture.reader.bundle = exact.model_copy(update={"terminal_audit": tampered})
        with pytest.raises(ManagerDispositionIntegrity):
            fixture.app.act(_assign_command())


def test_Answered_state만_있고_ResumeEvidence가_없으면_exact_Completion이어도_repair하지_않는다() -> (
    None
):
    managers = _FaultManagerStore(point="resume", fail_before=True)
    fixture = _Fixture(managers=managers)

    with pytest.raises(ManagerDispositionDependency):
        fixture.app.act(_assign_command())

    _store_exact_answered_completion(fixture)
    with pytest.raises(ManagerDispositionIntegrity):
        fixture.app.act(_assign_command())

    case = fixture.conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert fixture.starter.calls == 0


def test_Failed_terminal은_기록된_exact_ResumeEvidence로만_Case_Item을_forward_repair한다() -> None:
    conflicts = _FaultConflictStore(point="case", fail_before=True)
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(conflicts=conflicts, managers=managers)

    with pytest.raises(ManagerDispositionDependency):
        fixture.app.act(_assign_command())

    ready = fixture.requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    failed = ready.transition(
        FailedRequest(error_code="required_grounding_missing"),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert fixture.requests.compare_and_set("request-1", ready.revision, ready, failed)

    recovered = fixture.app.act(_assign_command())

    assert isinstance(recovered, DeadlockOwnerAssigned)
    case = conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert case is not None and case.status == "resolved"
    assert item is not None and item.status == "resolved"


@pytest.mark.parametrize("resume_mode", ["hidden", "tampered"])
def test_Failed_terminal의_ResumeEvidence가_없거나_다르면_Case_Item을_닫지_않는다(
    resume_mode: str,
) -> None:
    conflicts = _FaultConflictStore(point="case", fail_before=True)
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(conflicts=conflicts, managers=managers)

    with pytest.raises(ManagerDispositionDependency):
        fixture.app.act(_assign_command())

    ready = fixture.requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    failed = ready.transition(
        FailedRequest(error_code="required_grounding_missing"),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert fixture.requests.compare_and_set("request-1", ready.revision, ready, failed)
    managers.resume_mode = resume_mode

    with pytest.raises(ManagerDispositionIntegrity):
        fixture.app.act(_assign_command())

    case = conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert fixture.starter.calls == 0


@pytest.mark.parametrize("state_kind", ["awaiting_answer", "awaiting_approval"])
def test_active_execution_retry는_exact_route_attempt와_ResumeEvidence로_Case_Item을_보수한다(
    state_kind: str,
) -> None:
    conflicts = _FaultConflictStore(point="case", fail_before=True)
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(conflicts=conflicts, managers=managers)

    with pytest.raises(ManagerDispositionDependency):
        fixture.app.act(_assign_command())

    active = _advance_ready_to_active(fixture, state_kind)
    result = fixture.app.act(_assign_command())

    assert isinstance(result, DeadlockOwnerAssigned)
    assert fixture.requests.get("request-1") == active
    case = conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert case is not None and case.status == "resolved"
    assert item is not None and item.status == "resolved"
    assert managers.resume_commits == 1


@pytest.mark.parametrize("state_kind", ["awaiting_answer", "awaiting_approval"])
def test_active_execution_state만_있고_ResumeEvidence가_없으면_Case_Item을_닫지_않는다(
    state_kind: str,
) -> None:
    managers = _FaultManagerStore(point="resume", fail_before=True)
    fixture = _Fixture(managers=managers)

    with pytest.raises(ManagerDispositionDependency):
        fixture.app.act(_assign_command())

    _advance_ready_to_active(fixture, state_kind)
    with pytest.raises(ManagerDispositionIntegrity):
        fixture.app.act(_assign_command())

    case = fixture.conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert fixture.starter.calls == 0


@pytest.mark.parametrize("state_kind", ["awaiting_answer", "awaiting_approval"])
@pytest.mark.parametrize("tamper", ["route", "attempt"])
def test_active_execution의_route_or_attempt가_다르면_ResumeEvidence가_있어도_winner가_아니다(
    state_kind: str,
    tamper: str,
) -> None:
    requests = _TamperActiveReadStore()
    conflicts = _FaultConflictStore(point="case", fail_before=True)
    managers = _FaultManagerStore(point="none", fail_before=True)
    fixture = _Fixture(requests=requests, conflicts=conflicts, managers=managers)

    with pytest.raises(ManagerDispositionDependency):
        fixture.app.act(_assign_command())

    _advance_ready_to_active(fixture, state_kind)
    requests.tamper = tamper
    with pytest.raises(ManagerDispositionConflict):
        fixture.app.act(_assign_command())

    case = conflicts.get_request_case("case-1")
    item = managers.get("item-1")
    assert case is not None and case.status == "escalated"
    assert item is not None and item.status == "open"
    assert fixture.starter.calls == 0


def test_S3_E_application은_Router_Precedent_ComplementEdge_Runtime을_import하지_않는다() -> None:
    path = Path("src/agent_org_network/p17_deadlock_manager_disposition.py")
    tree = ast.parse(path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    lowered = {name.lower() for name in imported}
    assert not any("router" in name for name in lowered)
    assert "precedent" not in lowered
    assert "complementedge" not in lowered
    assert "agentruntime" not in lowered
