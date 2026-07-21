from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import cast

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
from agent_org_network.conflict import Candidate, ConflictCase
from agent_org_network.demo_question_surfaces import DemoRouteAuthority
from agent_org_network.p17_conflict_disposition import (
    ConcurOnConflict,
    ConflictClaimAcquired,
    ConflictConcurrenceAttempt,
    ConflictDispositionConflict,
    ConflictDispositionDependency,
    ConflictDispositionError,
    ConflictDispositionInProgress,
    ConflictDispositionIntegrity,
    ConflictReservationControlToken,
    ConflictResolutionEvidence,
    ConflictResolved,
    ConflictSealedClaimHandle,
    InMemoryConflictDispositionStore,
    OwnerPrincipal,
    P17DirectConflictDispositionApplication,
    ReservedConsensusClaim,
    SealedConflictClaimAvailable,
    SealedConsensusClaim,
    SealedDeadlockClaim,
    ValidatedConcurrence,
)
from agent_org_network.p17_manager_disposition import ExecutionStarted, ExecutionWake
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingConflict,
    FailedRequest,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
)
from agent_org_network.question_resolution import AuthorityGrant
from agent_org_network.registry import Registry
from agent_org_network.request_route_authority import (
    RequestRouteAuthority,
    RequestRouteGrantAssignment,
    RequestRouteGrantConflict,
    RequestRouteGrantRejected,
    RequestRouteGrantResult,
)
from agent_org_network.user import User


NOW = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)


def _ids(*values: str) -> Callable[[], object]:
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        del org_id, state_kind
        return started_at + timedelta(hours=1)


class _Reader:
    def __init__(self) -> None:
        self.bundle: CompletionBundle | None = None

    def by_request(self, request_id: str) -> CompletionBundle | None:
        if self.bundle is not None and self.bundle.request.request_id == request_id:
            return self.bundle
        return None

    def by_record(self, record_id: str) -> CompletionBundle | None:
        if self.bundle is not None and self.bundle.completion.record_id == record_id:
            return self.bundle
        return None


class _Starter:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls = 0

    def ensure_started(self, request_id: str) -> ExecutionWake:
        del request_id
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("wake fault")
        return ExecutionStarted()


class _MalformedWakeStarter(_Starter):
    def __init__(self, *, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def ensure_started(self, request_id: str) -> ExecutionWake:
        del request_id
        self.calls += 1
        if self.mode == "malformed":
            return object()  # type: ignore[return-value]
        if self.mode == "subclass":
            subclass = type("ExecutionStartedSubclass", (ExecutionStarted,), {})
            return subclass()  # type: ignore[return-value]
        return ExecutionStarted()


class _RejectAuthority:
    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        del org_id, intent, agent_id
        return None

    def grant_for_request(self, assignment: RequestRouteGrantAssignment) -> RequestRouteGrantResult:
        return RequestRouteGrantRejected(
            idempotency_key=assignment.idempotency_key,
            reason_code="policy_denied",
        )

    def authorize_for_request(
        self, org_id: str, request_id: str, intent: str, agent_id: str
    ) -> AuthorityGrant | None:
        del org_id, request_id, intent, agent_id
        return None


class _ConflictAuthority(_RejectAuthority):
    def grant_for_request(self, assignment: RequestRouteGrantAssignment) -> RequestRouteGrantResult:
        del assignment
        return RequestRouteGrantConflict()


class _WrongRejectAuthority(_RejectAuthority):
    def grant_for_request(self, assignment: RequestRouteGrantAssignment) -> RequestRouteGrantResult:
        del assignment
        return RequestRouteGrantRejected(
            idempotency_key="conflict-disposition:wrong-case:1",
            reason_code="policy_denied",
        )


class _RaiseAfterWriteAuthority:
    def __init__(self, delegate: DemoRouteAuthority) -> None:
        self.delegate = delegate
        self.raise_once = True

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        return self.delegate.authorize(org_id, intent, agent_id)

    def grant_for_request(self, assignment: RequestRouteGrantAssignment) -> RequestRouteGrantResult:
        result = self.delegate.grant_for_request(assignment)
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("receipt lost")
        return result

    def authorize_for_request(
        self, org_id: str, request_id: str, intent: str, agent_id: str
    ) -> AuthorityGrant | None:
        return self.delegate.authorize_for_request(org_id, request_id, intent, agent_id)


class _MalformedOnceAuthority:
    def __init__(self, delegate: DemoRouteAuthority) -> None:
        self.delegate = delegate
        self.malformed_once = True

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        return self.delegate.authorize(org_id, intent, agent_id)

    def grant_for_request(self, assignment: RequestRouteGrantAssignment) -> RequestRouteGrantResult:
        if self.malformed_once:
            self.malformed_once = False
            return cast(RequestRouteGrantResult, object())
        return self.delegate.grant_for_request(assignment)

    def authorize_for_request(
        self, org_id: str, request_id: str, intent: str, agent_id: str
    ) -> AuthorityGrant | None:
        return self.delegate.authorize_for_request(org_id, request_id, intent, agent_id)


class _MutateAfterWriteAuthority:
    def __init__(self, delegate: DemoRouteAuthority, registry: Registry) -> None:
        self.delegate = delegate
        self.registry = registry
        self.mutate_once = True

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        return self.delegate.authorize(org_id, intent, agent_id)

    def grant_for_request(self, assignment: RequestRouteGrantAssignment) -> RequestRouteGrantResult:
        result = self.delegate.grant_for_request(assignment)
        if self.mutate_once:
            self.mutate_once = False
            current = self.registry.get("refund-card")
            self.registry.replace_card(current.model_copy(update={"owner": "owner-b"}))
            raise RuntimeError("registry changed after authority write")
        return result

    def authorize_for_request(
        self, org_id: str, request_id: str, intent: str, agent_id: str
    ) -> AuthorityGrant | None:
        return self.delegate.authorize_for_request(org_id, request_id, intent, agent_id)


class _ReadbackFaultAuthority:
    def __init__(self, delegate: DemoRouteAuthority, *, mode: str) -> None:
        self.delegate = delegate
        self.mode = mode

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        return self.delegate.authorize(org_id, intent, agent_id)

    def grant_for_request(self, assignment: RequestRouteGrantAssignment) -> RequestRouteGrantResult:
        return self.delegate.grant_for_request(assignment)

    def authorize_for_request(
        self, org_id: str, request_id: str, intent: str, agent_id: str
    ) -> AuthorityGrant | None:
        grant = self.delegate.authorize_for_request(org_id, request_id, intent, agent_id)
        if self.mode == "exception":
            raise RuntimeError("authority read unavailable")
        if self.mode == "none":
            return None
        assert grant is not None
        if self.mode == "subclass":
            subclass = type("AuthorityGrantSubclass", (AuthorityGrant,), {})
            return subclass(policy_version=grant.policy_version)  # type: ignore[return-value]
        if self.mode == "wrong_version":
            return AuthorityGrant(policy_version="wrong-version")
        return grant


class _FaultConflictStore(InMemoryConflictDispositionStore):
    def __init__(self, *, evidence_fault: bool = False, transition_fault: bool = False) -> None:
        super().__init__(id_factory=_ids("generation-1", "control-1", "forward-1"))
        self.evidence_fault = evidence_fault
        self.transition_fault = transition_fault

    def record_resolution_evidence(
        self,
        handle: ConflictSealedClaimHandle,
        evidence: ConflictResolutionEvidence,
    ) -> None:
        if self.evidence_fault:
            self.evidence_fault = False
            raise RuntimeError("evidence fault")
        super().record_resolution_evidence(handle, evidence)

    def transition_for_claim(
        self,
        handle: ConflictSealedClaimHandle,
        *,
        target: ConflictCase,
    ) -> ConflictCase:
        if self.transition_fault:
            self.transition_fault = False
            raise RuntimeError("case transition fault")
        return super().transition_for_claim(handle, target=target)


class _MutatedReservationReturnConflictStore(_FaultConflictStore):
    def reserve_validated_concurrence(
        self,
        case_id: str,
        command: ConcurOnConflict,
        *,
        validate: Callable[[ConflictCase, ConcurOnConflict], ValidatedConcurrence],
    ) -> ConflictConcurrenceAttempt:
        attempt = super().reserve_validated_concurrence(
            case_id,
            command,
            validate=validate,
        )
        if isinstance(attempt, ConflictClaimAcquired):
            return attempt.model_copy(
                update={
                    "control_token": attempt.control_token.model_copy(
                        update={"token": "same-generation-other-secret"}
                    )
                }
            )
        return attempt


class _MutatedClaimReturnConflictStore(_FaultConflictStore):
    def reserve_validated_concurrence(
        self,
        case_id: str,
        command: ConcurOnConflict,
        *,
        validate: Callable[[ConflictCase, ConcurOnConflict], ValidatedConcurrence],
    ) -> ConflictConcurrenceAttempt:
        attempt = super().reserve_validated_concurrence(
            case_id,
            command,
            validate=validate,
        )
        if isinstance(attempt, ConflictClaimAcquired):
            return attempt.model_copy(
                update={"claim": attempt.claim.model_copy(update={"request_id": "forged-request"})}
            )
        return attempt


class _SealFaultConflictStore(_FaultConflictStore):
    def __init__(self) -> None:
        super().__init__()
        self.seal_fault = True

    def seal_consensus_claim(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
    ) -> SealedConflictClaimAvailable:
        if self.seal_fault:
            self.seal_fault = False
            raise RuntimeError("seal fault before commit")
        return super().seal_consensus_claim(claim, control_token=control_token)


class _SealResponseLossTamperedReadConflictStore(_FaultConflictStore):
    def __init__(self) -> None:
        super().__init__()
        self.lose_response = True
        self.tamper_recovery_read = True
        self.sealed_proof_calls = 0

    def seal_consensus_claim(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
    ) -> SealedConflictClaimAvailable:
        available = super().seal_consensus_claim(claim, control_token=control_token)
        if self.lose_response:
            self.lose_response = False
            raise RuntimeError("seal response lost after commit")
        return available

    def sealed_claim_for_case(self, case_id: str) -> SealedConflictClaimAvailable | None:
        available = super().sealed_claim_for_case(case_id)
        if available is not None and self.tamper_recovery_read:
            self.tamper_recovery_read = False
            return available.model_copy(
                update={
                    "handle": available.handle.model_copy(
                        update={"forward_token": "tampered-seal-recovery-forward"}
                    )
                }
            )
        return available

    def validate_sealed_claim(
        self,
        claim: SealedConsensusClaim | SealedDeadlockClaim,
        *,
        handle: ConflictSealedClaimHandle,
    ) -> None:
        self.sealed_proof_calls += 1
        return super().validate_sealed_claim(claim, handle=handle)


class _AbandonFaultConflictStore(_FaultConflictStore):
    def __init__(self) -> None:
        super().__init__()
        self.abandon_fault = True

    def abandon_unmutated_consensus_round(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> ConflictCase:
        if self.abandon_fault:
            self.abandon_fault = False
            raise RuntimeError("abandon fault before commit")
        return super().abandon_unmutated_consensus_round(
            claim,
            control_token=control_token,
            rejection=rejection,
        )


class _AbandonAfterCommitFaultConflictStore(_FaultConflictStore):
    def __init__(self) -> None:
        super().__init__()
        self.abandon_fault = True

    def abandon_unmutated_consensus_round(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> ConflictCase:
        result = super().abandon_unmutated_consensus_round(
            claim,
            control_token=control_token,
            rejection=rejection,
        )
        if self.abandon_fault:
            self.abandon_fault = False
            raise RuntimeError("abandon response lost after commit")
        return result


class _MutatedAbandonReturnConflictStore(_FaultConflictStore):
    def abandon_unmutated_consensus_round(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> ConflictCase:
        stored = super().abandon_unmutated_consensus_round(
            claim,
            control_token=control_token,
            rejection=rejection,
        )
        return replace(stored, concurrence_round=99)


class _AfterWriteFaultConflictStore(_FaultConflictStore):
    def __init__(
        self,
        *,
        evidence_after_write: bool = False,
        transition_after_write: bool = False,
    ) -> None:
        super().__init__()
        self.evidence_after_write = evidence_after_write
        self.transition_after_write = transition_after_write

    def record_resolution_evidence(
        self,
        handle: ConflictSealedClaimHandle,
        evidence: ConflictResolutionEvidence,
    ) -> None:
        super().record_resolution_evidence(handle, evidence)
        if self.evidence_after_write:
            self.evidence_after_write = False
            raise RuntimeError("evidence response lost after write")

    def transition_for_claim(
        self,
        handle: ConflictSealedClaimHandle,
        *,
        target: ConflictCase,
    ) -> ConflictCase:
        result = super().transition_for_claim(handle, target=target)
        if self.transition_after_write:
            self.transition_after_write = False
            raise RuntimeError("case response lost after write")
        return result


class _NonNoneEvidenceConflictStore(_FaultConflictStore):
    def record_resolution_evidence(
        self,
        handle: ConflictSealedClaimHandle,
        evidence: ConflictResolutionEvidence,
    ) -> None:
        super().record_resolution_evidence(handle, evidence)
        return cast(None, object())


class _AfterCommitFaultRequestStore(InMemoryQuestionRequestStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_after_commit = True

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        result = super().compare_and_set(request_id, expected_revision, current, updated)
        if result and isinstance(updated.state, ReadyToDispatch) and self.fail_after_commit:
            self.fail_after_commit = False
            raise RuntimeError("CAS response lost")
        return result


class _WrongReadyWinnerRequestStore(InMemoryQuestionRequestStore):
    def __init__(self) -> None:
        super().__init__()
        self.inject_wrong_winner = True

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        if (
            self.inject_wrong_winner
            and isinstance(updated.state, ReadyToDispatch)
            and isinstance(current.state, AwaitingConflict)
        ):
            self.inject_wrong_winner = False
            wrong_key = "request-dispatch:wrong-request:1"
            wrong_state = updated.state.model_copy(
                update={
                    "trigger_key": wrong_key,
                    "handling": updated.state.handling.model_copy(update={"ref": wrong_key}),
                }
            )
            wrong_updated = updated.model_copy(update={"state": wrong_state})
            assert super().compare_and_set(
                request_id,
                expected_revision,
                current,
                wrong_updated,
            )
            return False
        return super().compare_and_set(
            request_id,
            expected_revision,
            current,
            updated,
        )


class _CorruptWinnerReadbackRequestStore(InMemoryQuestionRequestStore):
    def __init__(self) -> None:
        super().__init__()
        self.corrupt_next_ready_read = False

    def get(self, request_id: str) -> QuestionRequest | None:
        stored = super().get(request_id)
        if (
            self.corrupt_next_ready_read
            and stored is not None
            and isinstance(stored.state, ReadyToDispatch)
        ):
            self.corrupt_next_ready_read = False
            return stored.model_copy(update={"revision": stored.revision + 1})
        return stored

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        result = super().compare_and_set(request_id, expected_revision, current, updated)
        if result and isinstance(updated.state, ReadyToDispatch):
            self.corrupt_next_ready_read = True
        return result


def _registry() -> Registry:
    registry = Registry()
    for owner in ("owner-a", "owner-b"):
        registry.register_user(User(id=owner))
    for agent_id, owner in (("refund-card", "owner-a"), ("finance-card", "owner-b")):
        registry.register(
            AgentCard(
                agent_id=agent_id,
                owner=owner,
                team="support",
                summary=agent_id,
                domains=["refund"],
                last_reviewed_at=date(2026, 7, 1),
            )
        )
    return registry


def _build(
    *,
    authority: RequestRouteAuthority | None = None,
    authority_factory: Callable[[Registry], RequestRouteAuthority] | None = None,
    conflicts: _FaultConflictStore | None = None,
    requests: InMemoryQuestionRequestStore | None = None,
    starter: _Starter | None = None,
    reader: _Reader | None = None,
) -> tuple[
    P17DirectConflictDispositionApplication,
    InMemoryQuestionRequestStore,
    _FaultConflictStore,
    _Starter,
]:
    registry = _registry()
    request_store = requests or InMemoryQuestionRequestStore()
    received = QuestionRequest.receive(
        org_id="demo-org",
        requester_id="requester",
        question="환불 기준은?",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )
    request_store.create(received)
    awaiting = received.record_initial_routing(
        intent="refund",
        disposition="contested",
        target=AwaitingConflict(
            case_id="case-1",
            handling=HandlingAssignment(
                kind="conflict_case", ref="case-1", due_at=NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW,
    )
    assert request_store.compare_and_set("request-1", 0, received, awaiting)
    conflict_store = conflicts or _FaultConflictStore()
    conflict_store.create_or_get_for_request(
        ConflictCase.for_request(
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
    )
    chosen_starter = starter or _Starter()
    if authority is not None and authority_factory is not None:
        raise AssertionError("authority와 authority_factory는 함께 쓸 수 없습니다.")
    chosen_authority = (
        authority
        if authority is not None
        else authority_factory(registry)
        if authority_factory is not None
        else DemoRouteAuthority(registry)
    )
    app = P17DirectConflictDispositionApplication(
        requests=cast(QuestionRequestStore, request_store),
        conflicts=conflict_store,
        registry=registry,
        route_authority=chosen_authority,
        completion_reader=reader or _Reader(),
        deadline_policy=_Deadline(),
        execution_starter=chosen_starter,
        clock=lambda: NOW,
    )
    return app, request_store, conflict_store, chosen_starter


def _vote(owner: str) -> ConcurOnConflict:
    return ConcurOnConflict(
        principal=OwnerPrincipal(org_id="demo-org", subject_id=owner),
        case_id="case-1",
        expected_round=1,
        on_agent="refund-card",
    )


def _prime(app: P17DirectConflictDispositionApplication) -> None:
    app.concur(_vote("owner-a"))


def test_typed_write0_reject만_round를_올리고_Request와_wake는_건드리지않는다() -> None:
    app, requests, conflicts, starter = _build(authority=_RejectAuthority())
    _prime(app)

    result = app.concur(_vote("owner-b"))

    assert result.kind == "consensus_route_rejected"
    assert result.next_round == 2
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.concurrence_round == 2
    request = requests.get("request-1")
    assert request is not None and isinstance(request.state, AwaitingConflict)
    assert starter.calls == 0


def test_resolution_evidence_void_port의_non_None은_Request_CAS전에_fail_closed한다() -> None:
    conflicts = _NonNoneEvidenceConflictStore()
    app, requests, conflicts, starter = _build(conflicts=conflicts)
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_vote("owner-b"))

    request = requests.get("request-1")
    case = conflicts.get_request_case("case-1")
    assert request is not None and isinstance(request.state, AwaitingConflict)
    assert case is not None and case.status == "open"
    assert conflicts.resolution_evidence_for_request("request-1") is not None
    assert starter.calls == 0


def test_reservation_return의_same_generation_other_secret은_Authority_write전에_거부한다() -> None:
    registry = _registry()
    authority = DemoRouteAuthority(registry)
    conflicts = _MutatedReservationReturnConflictStore()
    app, requests, conflicts, starter = _build(
        authority=authority,
        conflicts=conflicts,
    )
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_vote("owner-b"))

    assert (
        authority.authorize_for_request(
            "demo-org",
            "request-1",
            "refund",
            "refund-card",
        )
        is None
    )
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingConflict)
    assert isinstance(conflicts.claim_for_case("case-1"), ReservedConsensusClaim)
    assert starter.calls == 0


def test_reservation_return의_forged_Request_claim은_Authority_write전에_거부한다() -> None:
    registry = _registry()
    authority = DemoRouteAuthority(registry)
    conflicts = _MutatedClaimReturnConflictStore()
    app, requests, conflicts, starter = _build(
        authority=authority,
        conflicts=conflicts,
    )
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_vote("owner-b"))

    for request_id in ("request-1", "forged-request"):
        assert (
            authority.authorize_for_request(
                "demo-org",
                request_id,
                "refund",
                "refund-card",
            )
            is None
        )
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingConflict)
    assert isinstance(conflicts.claim_for_case("case-1"), ReservedConsensusClaim)
    assert starter.calls == 0


def test_authority_conflict는_claim을_seal하고_round를_reset하지않는다() -> None:
    app, _requests, conflicts, _starter = _build(authority=_ConflictAuthority())
    _prime(app)

    with pytest.raises(ConflictDispositionConflict):
        app.concur(_vote("owner-b"))

    claim = conflicts.claim_for_case("case-1")
    assert isinstance(claim, SealedConsensusClaim)
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.concurrence_round == 1


def test_wrong_key_policy_reject는_reserved_claim을_보수적으로_seal한다() -> None:
    app, _requests, conflicts, _starter = _build(authority=_WrongRejectAuthority())
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity, match="idempotency key"):
        app.concur(_vote("owner-b"))

    assert isinstance(conflicts.claim_for_case("case-1"), SealedConsensusClaim)


def test_abandon_commit전_transient_fault는_same_action_retry로_round를_올린다() -> None:
    conflicts = _AbandonFaultConflictStore()
    app, requests, conflicts, starter = _build(
        authority=_RejectAuthority(),
        conflicts=conflicts,
    )
    _prime(app)

    with pytest.raises(ConflictDispositionDependency, match="abandon"):
        app.concur(_vote("owner-b"))
    assert isinstance(conflicts.claim_for_case("case-1"), ReservedConsensusClaim)

    result = app.concur(_vote("owner-b"))
    assert result.kind == "consensus_route_rejected"
    assert result.next_round == 2
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.concurrence_round == 2
    request = requests.get("request-1")
    assert request is not None and isinstance(request.state, AwaitingConflict)
    assert starter.calls == 0


def test_abandon_commit뒤_response_lost는_stale_retry에서_local_token을_폐기한다() -> None:
    conflicts = _AbandonAfterCommitFaultConflictStore()
    app, _requests, conflicts, _starter = _build(
        authority=_RejectAuthority(),
        conflicts=conflicts,
    )
    _prime(app)

    with pytest.raises(ConflictDispositionDependency, match="abandon"):
        app.concur(_vote("owner-b"))
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.concurrence_round == 2

    with pytest.raises(ConflictDispositionIntegrity, match="round"):
        app.concur(_vote("owner-b"))
    recoveries = cast(dict[str, object], app.__dict__["_reserved_recoveries"])
    assert recoveries == {}


def test_abandon_return_변조는_backing_round와_다른_성공응답을_내지않는다() -> None:
    conflicts = _MutatedAbandonReturnConflictStore()
    app, requests, conflicts, starter = _build(
        authority=_RejectAuthority(),
        conflicts=conflicts,
    )
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_vote("owner-b"))

    case = conflicts.get_request_case("case-1")
    assert case is not None and case.concurrence_round == 2
    request = requests.get("request-1")
    assert request is not None and isinstance(request.state, AwaitingConflict)
    assert starter.calls == 0


def test_receipt_lost는_sealed_claim으로_같은_action_forward_retry한다() -> None:
    registry = _registry()
    authority = _RaiseAfterWriteAuthority(DemoRouteAuthority(registry))
    app, requests, conflicts, _starter = _build(authority=authority)
    # app은 별 Registry를 갖지만 같은 카드 snapshot이므로 Authority 결과는 exact하다.
    _prime(app)

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_vote("owner-b"))
    assert isinstance(conflicts.claim_for_case("case-1"), SealedConsensusClaim)

    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)


def test_malformed_authority_result도_claim을_seal하고_forward_retry한다() -> None:
    def authority_factory(registry: Registry) -> RequestRouteAuthority:
        return _MalformedOnceAuthority(DemoRouteAuthority(registry))

    app, requests, conflicts, _starter = _build(authority_factory=authority_factory)
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity, match="exact type"):
        app.concur(_vote("owner-b"))
    assert isinstance(conflicts.claim_for_case("case-1"), SealedConsensusClaim)

    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)


def test_seal_commit전_transient_fault는_같은_application_retry가_control을_복구한다() -> None:
    conflicts = _SealFaultConflictStore()
    app, requests, conflicts, _starter = _build(conflicts=conflicts)
    _prime(app)

    with pytest.raises(ConflictDispositionDependency, match="seal"):
        app.concur(_vote("owner-b"))
    assert isinstance(conflicts.claim_for_case("case-1"), ReservedConsensusClaim)

    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)


def test_seal_response_loss_복구_read도_full_handle_proof를_검증한다() -> None:
    conflicts = _SealResponseLossTamperedReadConflictStore()
    app, requests, _returned_conflicts, starter = _build(conflicts=conflicts)
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_vote("owner-b"))

    assert conflicts.sealed_proof_calls == 1
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingConflict)
    assert conflicts.resolution_evidence_for_request("request-1") is None
    assert starter.calls == 0

    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)
    assert starter.calls == 1


def test_authority_write뒤_registry_drift는_sealed_direct와_write0로_fail_closed한다() -> None:
    def authority_factory(registry: Registry) -> RequestRouteAuthority:
        return _MutateAfterWriteAuthority(DemoRouteAuthority(registry), registry)

    app, requests, conflicts, starter = _build(authority_factory=authority_factory)
    _prime(app)

    with pytest.raises(ConflictDispositionDependency, match="불명확"):
        app.concur(_vote("owner-b"))
    assert isinstance(conflicts.claim_for_case("case-1"), SealedConsensusClaim)
    assert conflicts.resolution_evidence_for_request("request-1") is None
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, AwaitingConflict)
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.status == "open"
    assert starter.calls == 0

    with pytest.raises(ConflictDispositionIntegrity, match="Registry가 drift"):
        app.concur(_vote("owner-b"))
    assert conflicts.resolution_evidence_for_request("request-1") is None
    assert starter.calls == 0


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("exception", ConflictDispositionDependency),
        ("none", ConflictDispositionIntegrity),
        ("subclass", ConflictDispositionIntegrity),
        ("wrong_version", ConflictDispositionIntegrity),
    ],
)
def test_direct_Authority_read_fault는_terminal_write0이고_same_action으로_복구한다(
    mode: str,
    error_type: type[ConflictDispositionError],
) -> None:
    holder: dict[str, _ReadbackFaultAuthority] = {}

    def authority_factory(registry: Registry) -> RequestRouteAuthority:
        authority = _ReadbackFaultAuthority(DemoRouteAuthority(registry), mode=mode)
        holder["authority"] = authority
        return authority

    app, requests, conflicts, starter = _build(authority_factory=authority_factory)
    _prime(app)

    with pytest.raises(error_type):
        app.concur(_vote("owner-b"))

    request = requests.get("request-1")
    case = conflicts.get_request_case("case-1")
    assert request is not None and isinstance(request.state, AwaitingConflict)
    assert case is not None and case.status == "open"
    assert isinstance(conflicts.claim_for_case("case-1"), SealedConsensusClaim)
    assert conflicts.resolution_evidence_for_request("request-1") is None
    assert starter.calls == 0

    holder["authority"].mode = "valid"
    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)


@pytest.mark.parametrize("fault", ["evidence", "case", "wake"])
def test_commit단계_fault는_same_action으로_앞으로_보수한다(fault: str) -> None:
    conflicts = _FaultConflictStore(
        evidence_fault=fault == "evidence",
        transition_fault=fault == "case",
    )
    starter = _Starter(fail_once=fault == "wake")
    app, requests, conflicts, starter = _build(conflicts=conflicts, starter=starter)
    _prime(app)

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_vote("owner-b"))

    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.status == "resolved"


@pytest.mark.parametrize("mode", ["malformed", "subclass"])
def test_direct_wake_변조는_typed_Integrity이고_same_action으로_복구한다(mode: str) -> None:
    starter = _MalformedWakeStarter(mode=mode)
    app, requests, conflicts, _returned_starter = _build(starter=starter)
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_vote("owner-b"))

    request = requests.get("request-1")
    case = conflicts.get_request_case("case-1")
    assert request is not None and isinstance(request.state, ReadyToDispatch)
    assert case is not None and case.status == "resolved"
    assert conflicts.resolution_evidence_for_request("request-1") is not None

    starter.mode = "valid"
    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)
    assert result.wake == ExecutionStarted()


@pytest.mark.parametrize("fault", ["evidence_after_write", "case_after_write"])
def test_commit뒤_response_lost도_same_action_retry로_앞으로_보수한다(fault: str) -> None:
    conflicts = _AfterWriteFaultConflictStore(
        evidence_after_write=fault == "evidence_after_write",
        transition_after_write=fault == "case_after_write",
    )
    app, requests, conflicts, _starter = _build(conflicts=conflicts)
    _prime(app)

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_vote("owner-b"))

    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)
    stored = requests.get("request-1")
    assert stored is not None and isinstance(stored.state, ReadyToDispatch)
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.status == "resolved"


def test_CAS_commit뒤_response_lost는_vote_grant_evidence없이_case와_wake만_보수한다() -> None:
    requests = _AfterCommitFaultRequestStore()
    app, requests, conflicts, starter = _build(requests=requests)
    _prime(app)

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_vote("owner-b"))
    before = conflicts.progress_history_for_case("case-1")
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.status == "open"

    result = app.concur(_vote("owner-b"))

    assert isinstance(result, ConflictResolved)
    after = conflicts.progress_history_for_case("case-1")
    assert after == before
    assert starter.calls == 1


def test_open_Case의_grounding_Failed는_terminal_recovery로_위장하지_못한다() -> None:
    conflicts = _FaultConflictStore(transition_fault=True)
    app, requests, conflicts, starter = _build(conflicts=conflicts)
    _prime(app)

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_vote("owner-b"))
    ready = requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    failed = ready.transition(
        FailedRequest(error_code="required_grounding_missing"),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert requests.compare_and_set("request-1", ready.revision, ready, failed)

    with pytest.raises(ConflictDispositionConflict, match="Failed"):
        app.concur(_vote("owner-b"))
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.status == "open"
    assert starter.calls == 0


def test_resolved_Case의_grounding_Failed도_exact_revision_3만_복구한다() -> None:
    app, requests, conflicts, starter = _build()
    _prime(app)
    app.concur(_vote("owner-b"))
    ready = requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    failed = ready.transition(
        FailedRequest(error_code="required_grounding_invalid"),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    forged = failed.model_copy(update={"revision": 4})
    with requests._lock:  # pyright: ignore[reportPrivateUsage]
        requests._requests["request-1"] = forged  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ConflictDispositionIntegrity, match="revision"):
        app.concur(_vote("owner-b"))
    assert conflicts.get_request_case("case-1") is not None
    assert starter.calls == 1


@pytest.mark.parametrize(
    "error_code",
    ["required_grounding_missing", "required_grounding_invalid"],
)
def test_resolved_Case는_exact_grounding_Failed만_same_action으로_복구한다(
    error_code: str,
) -> None:
    app, requests, conflicts, starter = _build()
    _prime(app)
    first = app.concur(_vote("owner-b"))
    ready = requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    failed = ready.transition(
        FailedRequest(error_code=error_code),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert requests.compare_and_set("request-1", ready.revision, ready, failed)

    retried = app.concur(_vote("owner-b"))

    assert isinstance(first, ConflictResolved)
    assert isinstance(retried, ConflictResolved)
    assert retried.route == first.route
    assert conflicts.get_request_case("case-1") is not None
    assert starter.calls == 2


def test_resolved_Case의_다른_Failed_error는_fail_closed한다() -> None:
    app, requests, _conflicts, starter = _build()
    _prime(app)
    app.concur(_vote("owner-b"))
    ready = requests.get("request-1")
    assert ready is not None and isinstance(ready.state, ReadyToDispatch)
    failed = ready.transition(
        FailedRequest(error_code="runtime_failed"),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert requests.compare_and_set("request-1", ready.revision, ready, failed)

    with pytest.raises(ConflictDispositionConflict, match="Failed"):
        app.concur(_vote("owner-b"))
    assert starter.calls == 1


def test_CAS_loser는_same_route라도_wrong_trigger_winner를_거부한다() -> None:
    requests = _WrongReadyWinnerRequestStore()
    app, _requests, conflicts, starter = _build(requests=requests)
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity, match="winner"):
        app.concur(_vote("owner-b"))

    case = conflicts.get_request_case("case-1")
    assert case is not None and case.status == "open"
    assert starter.calls == 0


def test_CAS_true뒤_corrupt_readback은_integrity뒤_same_action으로_보수한다() -> None:
    requests = _CorruptWinnerReadbackRequestStore()
    app, _requests, conflicts, starter = _build(requests=requests)
    _prime(app)

    with pytest.raises(ConflictDispositionIntegrity, match="read-back"):
        app.concur(_vote("owner-b"))
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.status == "open"

    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)
    assert starter.calls == 1


def test_escalated_case는_canonical_vote만_inprogress이고_다른_action은_conflict다() -> None:
    app, _requests, conflicts, _starter = _build()
    _prime(app)
    divergent = _vote("owner-b").model_copy(update={"on_agent": "finance-card"})

    with pytest.raises(ConflictDispositionInProgress):
        app.concur(divergent)
    available = conflicts.sealed_claim_for_case("case-1")
    assert available is not None and isinstance(available.claim, SealedDeadlockClaim)
    current = conflicts.get_request_case("case-1")
    assert current is not None
    conflicts.transition_for_claim(
        available.handle,
        target=current.escalate("manager-item-1"),
    )

    with pytest.raises(ConflictDispositionInProgress):
        app.concur(divergent)
    with pytest.raises(ConflictDispositionConflict):
        app.concur(_vote("owner-b"))


def test_answered_terminal_retry는_completion의_request_record_route_attempt를_exact검증한다() -> (
    None
):
    reader = _Reader()
    conflicts = _AfterWriteFaultConflictStore(transition_after_write=True)
    app, requests, conflicts, starter = _build(conflicts=conflicts, reader=reader)
    _prime(app)

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_vote("owner-b"))
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
        org_id="demo-org",
        requester_id="requester",
        attempt=1,
        route=route,
        responsibility=AnswerResponsibilitySnapshot(
            agent_id="refund-card",
            owner_id="owner-a",
        ),
        candidate_mode="full",
        final_mode="full",
        approval=NoApprovalEvidence(policy_version=route.authority_version or "missing"),
        completed_at=completed_at,
    )
    exact = CompletionBundle(
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
    reader.bundle = exact.model_copy(
        update={"terminal_audit": audit.model_copy(update={"attempt": 2})}
    )
    with pytest.raises(ConflictDispositionIntegrity, match="completion"):
        app.concur(_vote("owner-b"))
    assert starter.calls == 0

    reader.bundle = exact
    result = app.concur(_vote("owner-b"))
    assert isinstance(result, ConflictResolved)
    assert starter.calls == 1
