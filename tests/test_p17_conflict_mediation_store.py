from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier

import pytest
from pydantic import ValidationError

from agent_org_network.conflict import Candidate, ConflictCase, DivergentVotes, Resolution
from agent_org_network.manager_queue import FromDeadlock, InMemoryManagerQueueStore, ManagerItem
from agent_org_network.p17_conflict_disposition import (
    ConcurOnConflict,
    ConcurrenceActionFingerprint,
    ConflictDispositionIntegrity,
    ConflictMediationHandle,
    ConflictMediationSealed,
    ConflictResolutionEvidence,
    ConflictResolutionEvidenceRecorded,
    FromManagerMediation,
    InMemoryConflictDispositionStore,
    OwnerConcurrenceEvidence,
    OwnerPrincipal,
    SealedConflictClaimAvailable,
    SealedConflictMediationAvailable,
    SealedDeadlockClaim,
    ValidatedMediationAssign,
    ValidatedMediationDismiss,
    ValidatedOwnerVote,
)
from agent_org_network.p17_manager_disposition import (
    AssignDeadlockedOwner,
    DeadlockManagerClaimAcquired,
    DeadlockManagerSealedClaimAvailable,
    DeadlockManagerSealedClaimHandle,
    DismissDeadlocked,
    ManagerPrincipal,
    SealedDeadlockAssignClaim,
    SealedDeadlockDismissClaim,
)
from agent_org_network.question_request import RouteTarget


NOW = datetime(2026, 7, 13, 17, 0, tzinfo=timezone.utc)


def _ids(*values: str) -> Callable[[], object]:
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


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
        principal=OwnerPrincipal(org_id="org-1", subject_id=owner),
        case_id="case-1",
        expected_round=1,
        on_agent=target,
    )


def _validated(case: ConflictCase, command: ConcurOnConflict) -> ValidatedOwnerVote:
    return ValidatedOwnerVote(
        request_id="request-1",
        case_id=case.case_id,
        org_id=command.principal.org_id,
        intent=case.intent,
        candidate_snapshot=case.candidates,
        trigger=ConcurrenceActionFingerprint(
            case_id=case.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        ),
        evidence=OwnerConcurrenceEvidence(
            round=command.expected_round,
            owner_id=command.principal.subject_id,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        ),
        target_requires_approval=False,
    )


def _conflict_store() -> tuple[
    InMemoryConflictDispositionStore,
    SealedConflictClaimAvailable,
]:
    store = InMemoryConflictDispositionStore(
        id_factory=_ids("conflict-generation", "conflict-forward", "mediation-forward")
    )
    store.create_or_get_for_request(_case())
    first = _command("owner-a", "refund-card")
    store.reserve_validated_concurrence(
        "case-1", first, validate=lambda case, command: _validated(case, command)
    )
    second = _command("owner-b", "finance-card")
    available = store.reserve_validated_concurrence(
        "case-1", second, validate=lambda case, command: _validated(case, command)
    )
    assert isinstance(available, SealedConflictClaimAvailable)
    assert isinstance(available.claim, SealedDeadlockClaim)
    current = store.get_request_case("case-1")
    assert current is not None
    store.transition_for_claim(available.handle, target=current.escalate("item-1"))
    return store, available


def _manager_store(
    *,
    dismiss: bool = False,
) -> tuple[InMemoryManagerQueueStore, DeadlockManagerSealedClaimAvailable]:
    case = _case()
    item = ManagerItem.for_request(
        request_id="request-1",
        manager_id="manager-1",
        source=FromDeadlock(
            case=case,
            reason="divergent_votes",
            cause=DivergentVotes(round=1),
        ),
        created_at=NOW,
        item_id="item-1",
    )
    store = InMemoryManagerQueueStore()
    store.create_or_get_for_request(item)
    if dismiss:
        command = DismissDeadlocked(
            principal=ManagerPrincipal(org_id="org-1", subject_id="manager-1"),
            item_id="item-1",
            rationale="담당 없음",
        )

        def validate(_item: ManagerItem):  # type: ignore[no-untyped-def]
            from agent_org_network.p17_manager_disposition import ReservedDeadlockDismissClaim

            return ReservedDeadlockDismissClaim(
                generation="manager-generation",
                idempotency_key="manager-disposition:item-1",
                request_id="request-1",
                case_id="case-1",
                item_id="item-1",
                org_id="org-1",
                by_manager="manager-1",
                intent="refund",
                round=1,
                cause=DivergentVotes(round=1),
                rationale="담당 없음",
            )

    else:
        command = AssignDeadlockedOwner(
            principal=ManagerPrincipal(org_id="org-1", subject_id="manager-1"),
            item_id="item-1",
            agent_id="refund-card",
            rationale="환불 담당으로 중재",
        )

        def validate(_item: ManagerItem):  # type: ignore[no-untyped-def]
            from agent_org_network.p17_manager_disposition import ReservedDeadlockAssignClaim

            return ReservedDeadlockAssignClaim(
                generation="manager-generation",
                idempotency_key="manager-disposition:item-1",
                request_id="request-1",
                case_id="case-1",
                item_id="item-1",
                org_id="org-1",
                by_manager="manager-1",
                intent="refund",
                round=1,
                cause=DivergentVotes(round=1),
                agent_id="refund-card",
                requires_approval=False,
                rationale="환불 담당으로 중재",
            )

    acquired = store.reserve_validated_deadlock_action("item-1", command, validate=validate)
    assert isinstance(acquired, DeadlockManagerClaimAcquired)
    sealed = store.seal_deadlock_claim(acquired.claim, control_token=acquired.control_token)
    return store, sealed


def _evidence() -> ConflictResolutionEvidence:
    return ConflictResolutionEvidence(
        request_id="request-1",
        case_id="case-1",
        org_id="org-1",
        intent="refund",
        route=RouteTarget(
            intent="refund",
            agent_id="refund-card",
            requires_approval=False,
            authority_version="grant-1",
        ),
        source=FromManagerMediation(item_id="item-1", by_manager="manager-1"),
        supporting=(),
    )


def _assign_proof(
    conflict: SealedConflictClaimAvailable,
    manager: DeadlockManagerSealedClaimAvailable,
) -> ValidatedMediationAssign:
    assert isinstance(conflict.claim, SealedDeadlockClaim)
    assert isinstance(manager.claim, SealedDeadlockAssignClaim)
    return ValidatedMediationAssign(
        conflict_claim=conflict.claim,
        conflict_handle=conflict.handle,
        manager_claim=manager.claim,
        manager_handle=manager.handle,
        evidence=_evidence(),
    )


def _dismiss_proof(
    conflict: SealedConflictClaimAvailable,
    manager: DeadlockManagerSealedClaimAvailable,
) -> ValidatedMediationDismiss:
    assert isinstance(conflict.claim, SealedDeadlockClaim)
    assert isinstance(manager.claim, SealedDeadlockDismissClaim)
    return ValidatedMediationDismiss(
        conflict_claim=conflict.claim,
        conflict_handle=conflict.handle,
        manager_claim=manager.claim,
        manager_handle=manager.handle,
        reason_code="manager_declined",
    )


def test_mediation_DTO는두_claim_handle_evidence를_exact_link한다() -> None:
    _store, conflict = _conflict_store()
    _managers, manager = _manager_store()
    proof = _assign_proof(conflict, manager)
    available = SealedConflictMediationAvailable(
        proof=proof,
        handle=ConflictMediationHandle(
            conflict_generation="conflict-generation",
            manager_generation="manager-generation",
            forward_token="mediation-forward",
        ),
    )
    assert available.handle.conflict_generation == proof.conflict_claim.generation
    assert available.handle.manager_generation == proof.manager_claim.generation

    with pytest.raises(ValidationError):
        SealedConflictMediationAvailable(
            proof=proof,
            handle=available.handle.model_copy(update={"manager_generation": "other"}),
        )
    with pytest.raises(ValidationError):
        ValidatedMediationAssign(
            conflict_claim=proof.conflict_claim,
            conflict_handle=proof.conflict_handle,
            manager_claim=proof.manager_claim.model_copy(update={"request_id": "other"}),
            manager_handle=proof.manager_handle,
            evidence=proof.evidence,
        )
    with pytest.raises(ValidationError):
        ValidatedMediationAssign(
            conflict_claim=proof.conflict_claim,
            conflict_handle=proof.conflict_handle,
            manager_claim=proof.manager_claim,
            manager_handle=proof.manager_handle,
            evidence=proof.evidence.model_copy(update={"supporting": (object(),)}),
        )


def test_record_assign은_evidence와_proof를원자기록하고_same_retry는_noop이다() -> None:
    store, conflict = _conflict_store()
    managers, manager = _manager_store()
    calls = 0

    def validate(
        case: ConflictCase,
        claim: SealedDeadlockClaim,
        handle: DeadlockManagerSealedClaimHandle,
    ) -> ValidatedMediationAssign:
        nonlocal calls
        calls += 1
        assert case.status == "escalated"
        assert claim == conflict.claim
        assert managers.deadlock_claim_for_handle(handle) == manager.claim
        return _assign_proof(conflict, manager)

    first = store.record_validated_mediation(
        conflict.handle,
        manager.handle,
        validate=validate,
    )
    second = store.record_validated_mediation(
        conflict.handle,
        manager.handle,
        validate=validate,
    )
    assert first == second
    assert first is not second
    assert calls == 2
    assert store.resolution_evidence_for_request("request-1") == _evidence()
    progress = store.progress_history_for_case("case-1")
    assert isinstance(progress[-2], ConflictResolutionEvidenceRecorded)
    assert isinstance(progress[-1], ConflictMediationSealed)
    assert [entry.kind for entry in progress].count("resolution_evidence_recorded") == 1
    assert [entry.kind for entry in progress].count("mediation_sealed") == 1
    projected = progress[-1].model_dump()
    assert "forward_token" not in projected
    assert "conflict-forward" not in str(projected)
    assert "mediation-forward" not in str(projected)
    assert "manager" not in projected or projected["manager_generation"] == "manager-generation"


def test_record_dismiss는_evidence를쓰지않고_existing_evidence를거부한다() -> None:
    store, conflict = _conflict_store()
    managers, manager = _manager_store(dismiss=True)

    result = store.record_validated_mediation(
        conflict.handle,
        manager.handle,
        validate=lambda _case, _claim, handle: (
            _dismiss_proof(conflict, manager)
            if managers.deadlock_claim_for_handle(handle) == manager.claim
            else pytest.fail("manager claim mismatch")
        ),
    )
    assert isinstance(result.proof, ValidatedMediationDismiss)
    assert store.resolution_evidence_for_request("request-1") is None
    assert [entry.kind for entry in store.progress_history_for_case("case-1")].count(
        "mediation_sealed"
    ) == 1

    other_store, other_conflict = _conflict_store()
    assign_managers, assign_manager = _manager_store()
    other_store.record_validated_mediation(
        other_conflict.handle,
        assign_manager.handle,
        validate=lambda _case, _claim, _handle: _assign_proof(other_conflict, assign_manager),
    )
    before = other_store.progress_history_for_case("case-1")
    with pytest.raises(ConflictDispositionIntegrity):
        other_store.record_validated_mediation(
            other_conflict.handle,
            manager.handle,
            validate=lambda _case, _claim, handle: (
                _dismiss_proof(other_conflict, manager)
                if managers.deadlock_claim_for_handle(handle) == manager.claim
                else pytest.fail("manager claim mismatch")
            ),
        )
    assert other_store.progress_history_for_case("case-1") == before
    assert other_store.resolution_evidence_for_request("request-1") == _evidence()
    assert assign_managers.deadlock_claim_for_handle(assign_manager.handle) == assign_manager.claim


def test_mediation_callback_예외_재진입_tamper는proof_evidence_progress_write0이다() -> None:
    store, conflict = _conflict_store()
    _managers, manager = _manager_store()
    before = store.progress_history_for_case("case-1")

    def explode(
        _case: ConflictCase,
        _claim: SealedDeadlockClaim,
        _handle: DeadlockManagerSealedClaimHandle,
    ) -> ValidatedMediationAssign:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        store.record_validated_mediation(conflict.handle, manager.handle, validate=explode)
    assert store.progress_history_for_case("case-1") == before
    assert store.resolution_evidence_for_request("request-1") is None

    def reenter(
        _case: ConflictCase,
        _claim: SealedDeadlockClaim,
        _handle: DeadlockManagerSealedClaimHandle,
    ) -> ValidatedMediationAssign:
        with pytest.raises(ConflictDispositionIntegrity):
            store.record_validated_mediation(
                conflict.handle,
                manager.handle,
                validate=lambda _nested_case, _nested_claim, _nested_handle: _assign_proof(
                    conflict, manager
                ),
            )
        return _assign_proof(conflict, manager)

    with pytest.raises(ConflictDispositionIntegrity):
        store.record_validated_mediation(conflict.handle, manager.handle, validate=reenter)
    assert store.progress_history_for_case("case-1") == before
    assert store.resolution_evidence_for_request("request-1") is None

    tampered = _assign_proof(conflict, manager)
    object.__setattr__(
        tampered,
        "manager_handle",
        tampered.manager_handle.model_copy(update={"forward_token": "wrong"}),
    )
    with pytest.raises(ConflictDispositionIntegrity):
        store.record_validated_mediation(
            conflict.handle,
            manager.handle,
            validate=lambda _case, _claim, _handle: tampered,
        )
    assert store.progress_history_for_case("case-1") == before
    assert store.resolution_evidence_for_request("request-1") is None


def test_mediation은두원본full_handle과local_full_handle의secret을검증한다() -> None:
    store, conflict = _conflict_store()
    _managers, manager = _manager_store()
    wrong_conflict = conflict.handle.model_copy(update={"forward_token": "wrong"})
    wrong_manager = manager.handle.model_copy(update={"forward_token": "wrong"})
    before = store.progress_history_for_case("case-1")

    with pytest.raises(ConflictDispositionIntegrity):
        store.record_validated_mediation(
            wrong_conflict,
            manager.handle,
            validate=lambda _case, _claim, _handle: _assign_proof(conflict, manager),
        )
    with pytest.raises(ConflictDispositionIntegrity):
        store.record_validated_mediation(
            conflict.handle,
            wrong_manager,
            validate=lambda _case, _claim, _handle: _assign_proof(conflict, manager),
        )
    assert store.progress_history_for_case("case-1") == before

    available = store.record_validated_mediation(
        conflict.handle,
        manager.handle,
        validate=lambda _case, _claim, _handle: _assign_proof(conflict, manager),
    )
    current = store.get_request_case("case-1")
    assert current is not None
    target = current.resolve_for_request("refund-card", "환불 담당으로 중재")
    with pytest.raises(ConflictDispositionIntegrity):
        store.transition_for_mediation(
            available.handle.model_copy(update={"forward_token": "wrong"}),
            target=target,
        )


def test_assign_mediation_transition은exact_resolution만허용하고_terminal_noop이다() -> None:
    store, conflict = _conflict_store()
    _managers, manager = _manager_store()
    available = store.record_validated_mediation(
        conflict.handle,
        manager.handle,
        validate=lambda _case, _claim, _handle: _assign_proof(conflict, manager),
    )
    current = store.get_request_case("case-1")
    assert current is not None
    target = current.resolve_for_request("refund-card", "환불 담당으로 중재")
    before_history = len(store.history)
    assert store.transition_for_mediation(available.handle, target=target) == target
    after_history = len(store.history)
    assert after_history == before_history + 1
    assert store.transition_for_mediation(available.handle, target=target) == target
    assert len(store.history) == after_history

    wrong = replace(
        target,
        resolution=Resolution(
            intent="refund",
            primary="finance-card",
            rationale="환불 담당으로 중재",
        ),
    )
    with pytest.raises(ConflictDispositionIntegrity):
        store.transition_for_mediation(available.handle, target=wrong)


def test_dismiss_mediation_transition은manager_declined만허용하고_evidence가없다() -> None:
    store, conflict = _conflict_store()
    _managers, manager = _manager_store(dismiss=True)
    available = store.record_validated_mediation(
        conflict.handle,
        manager.handle,
        validate=lambda _case, _claim, _handle: _dismiss_proof(conflict, manager),
    )
    current = store.get_request_case("case-1")
    assert current is not None
    target = current.decline()
    assert store.transition_for_mediation(available.handle, target=target) == target
    assert store.transition_for_mediation(available.handle, target=target) == target
    with pytest.raises(ConflictDispositionIntegrity):
        store.transition_for_mediation(
            available.handle,
            target=replace(
                target,
                status="resolved",
                resolution=Resolution(intent="refund", primary="refund-card"),
                decline_reason=None,
            ),
        )


def test_same_assign_proof_32way는handle_evidence_progress하나로수렴한다() -> None:
    store, conflict = _conflict_store()
    managers, manager = _manager_store()
    barrier = Barrier(32)

    def record(_index: int) -> SealedConflictMediationAvailable:
        barrier.wait()
        return store.record_validated_mediation(
            conflict.handle,
            manager.handle,
            validate=lambda _case, _claim, handle: (
                _assign_proof(conflict, manager)
                if managers.deadlock_claim_for_handle(handle) == manager.claim
                else pytest.fail("manager claim mismatch")
            ),
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(record, range(32)))

    assert all(result.handle == results[0].handle for result in results)
    progress = store.progress_history_for_case("case-1")
    assert [entry.kind for entry in progress].count("resolution_evidence_recorded") == 1
    assert [entry.kind for entry in progress].count("mediation_sealed") == 1
    assert store.resolution_evidence_for_request("request-1") == _evidence()


def test_exact와_tampered_assign_proof_32way는exact하나만저장한다() -> None:
    store, conflict = _conflict_store()
    managers, manager = _manager_store()
    barrier = Barrier(32)

    def record(index: int) -> object:
        barrier.wait()
        proof = _assign_proof(conflict, manager)
        if index % 2:
            object.__setattr__(
                proof,
                "manager_handle",
                proof.manager_handle.model_copy(update={"forward_token": "tampered"}),
            )
        try:
            return store.record_validated_mediation(
                conflict.handle,
                manager.handle,
                validate=lambda _case, _claim, handle: (
                    proof
                    if managers.deadlock_claim_for_handle(handle) == manager.claim
                    else pytest.fail("manager claim mismatch")
                ),
            )
        except ConflictDispositionIntegrity as error:
            return error

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(record, range(32)))

    assert sum(isinstance(result, SealedConflictMediationAvailable) for result in results) == 16
    assert sum(isinstance(result, ConflictDispositionIntegrity) for result in results) == 16
    progress = store.progress_history_for_case("case-1")
    assert [entry.kind for entry in progress].count("resolution_evidence_recorded") == 1
    assert [entry.kind for entry in progress].count("mediation_sealed") == 1
    assert store.resolution_evidence_for_request("request-1") == _evidence()
