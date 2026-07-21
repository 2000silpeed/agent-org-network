from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier

import pytest
from pydantic import ValidationError

from agent_org_network.conflict import (
    Candidate,
    ConflictCase,
    DivergentVotes,
    Resolution,
)
from agent_org_network.decision import Unowned
from agent_org_network.manager_queue import (
    AssignOwner,
    Dismiss,
    FromDeadlock,
    FromUnowned,
    InMemoryManagerQueueStore,
    ManagerItem,
    ManagerResolution,
)
from agent_org_network.p17_manager_disposition import (
    AssignDeadlockedOwner,
    AssignUnownedOwner,
    ClaimConflict,
    DeadlockManagerClaimAcquired,
    DeadlockManagerClaimConflict,
    DeadlockManagerClaimInProgress,
    DeadlockManagerReservationControlToken,
    DeadlockManagerSealedClaimAvailable,
    DeadlockManagerSealedClaimHandle,
    DismissDeadlocked,
    ManagerDispositionIntegrity,
    ManagerPrincipal,
    ReservedAssignOwnerClaim,
    ReservedDeadlockAssignClaim,
    ReservedDeadlockDismissClaim,
    ResumeEvidence,
    SealedDeadlockAssignClaim,
    SealedDeadlockDismissClaim,
)
from agent_org_network.question_request import RouteTarget
from agent_org_network.request_route_authority import RequestRouteGrantRejected


NOW = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)


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


def _deadlock_item() -> ManagerItem:
    case = _case()
    return ManagerItem.for_request(
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


def _unowned_item() -> ManagerItem:
    return ManagerItem.for_request(
        request_id="request-1",
        manager_id="manager-1",
        source=FromUnowned(
            decision=Unowned(escalated_to="manager-1", intent="refund"),
            question="환불 기준은?",
        ),
        created_at=NOW,
        item_id="item-1",
    )


def _assign_command(agent_id: str = "refund-card") -> AssignDeadlockedOwner:
    return AssignDeadlockedOwner(
        principal=ManagerPrincipal(org_id="org-1", subject_id="manager-1"),
        item_id="item-1",
        agent_id=agent_id,
        rationale="환불 담당으로 중재",
    )


def _dismiss_command(rationale: str = "담당 없음") -> DismissDeadlocked:
    return DismissDeadlocked(
        principal=ManagerPrincipal(org_id="org-1", subject_id="manager-1"),
        item_id="item-1",
        rationale=rationale,
    )


def _reserved_assign(generation: str = "generation-1") -> ReservedDeadlockAssignClaim:
    return ReservedDeadlockAssignClaim(
        generation=generation,
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


def _reserved_dismiss(generation: str = "generation-1") -> ReservedDeadlockDismissClaim:
    return ReservedDeadlockDismissClaim(
        generation=generation,
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


def _evidence() -> ResumeEvidence:
    return ResumeEvidence(
        request_id="request-1",
        from_revision=2,
        to_revision=3,
        route=RouteTarget(
            intent="refund",
            agent_id="refund-card",
            requires_approval=False,
            authority_version="grant-1",
        ),
        trigger_key="request-dispatch:request-1:1",
    )


def _store(item: ManagerItem | None = None) -> InMemoryManagerQueueStore:
    store = InMemoryManagerQueueStore()
    stored, created = store.create_or_get_for_request(item or _deadlock_item())
    assert created is True
    assert stored == (item or _deadlock_item())
    return store


def test_deadlock_DTO는_generation과_canonical_claim_shape를_닫는다() -> None:
    claim = _reserved_assign()
    acquired = DeadlockManagerClaimAcquired(
        claim=claim,
        control_token=DeadlockManagerReservationControlToken(
            generation="generation-1",
            token="control-1",
        ),
    )
    sealed = SealedDeadlockAssignClaim(
        generation=claim.generation,
        idempotency_key=claim.idempotency_key,
        request_id=claim.request_id,
        case_id=claim.case_id,
        item_id=claim.item_id,
        org_id=claim.org_id,
        by_manager=claim.by_manager,
        intent=claim.intent,
        round=claim.round,
        cause=claim.cause,
        agent_id=claim.agent_id,
        requires_approval=claim.requires_approval,
        rationale=claim.rationale,
    )
    available = DeadlockManagerSealedClaimAvailable(
        claim=sealed,
        handle=DeadlockManagerSealedClaimHandle(
            generation="generation-1",
            forward_token="forward-1",
        ),
    )

    assert acquired.claim.generation == acquired.control_token.generation
    assert available.claim.model_dump(exclude={"kind"}) == claim.model_dump(exclude={"kind"})
    assert available.handle.generation == available.claim.generation

    with pytest.raises(ValidationError):
        DeadlockManagerClaimAcquired(
            claim=claim,
            control_token=DeadlockManagerReservationControlToken(
                generation="other",
                token="control-1",
            ),
        )
    with pytest.raises(ValidationError):
        ReservedDeadlockAssignClaim.model_validate(
            {**claim.model_dump(), "idempotency_key": "wrong"}, strict=True
        )
    with pytest.raises(ValidationError):
        ReservedDeadlockAssignClaim.model_validate(
            {**claim.model_dump(), "cause": DivergentVotes(round=2)}, strict=True
        )


def test_deadlock_reservation은_same_command만_follower이고_다른_command는_conflict다() -> None:
    store = _store()
    calls = 0

    def validate(_item: ManagerItem) -> ReservedDeadlockAssignClaim:
        nonlocal calls
        calls += 1
        return _reserved_assign()

    acquired = store.reserve_validated_deadlock_action(
        "item-1",
        _assign_command(),
        validate=validate,
    )
    assert isinstance(acquired, DeadlockManagerClaimAcquired)
    assert calls == 1

    follower = store.reserve_validated_deadlock_action(
        "item-1",
        _assign_command(),
        validate=lambda _item: pytest.fail("follower callback은 다시 호출되면 안 됩니다."),
    )
    assert isinstance(follower, DeadlockManagerClaimInProgress)
    assert isinstance(
        store.reserve_validated_deadlock_action(
            "item-1",
            _dismiss_command(),
            validate=lambda _item: pytest.fail("상충 callback은 호출되면 안 됩니다."),
        ),
        DeadlockManagerClaimConflict,
    )

    sealed = store.seal_deadlock_claim(
        acquired.claim,
        control_token=acquired.control_token,
    )
    assert isinstance(sealed, DeadlockManagerSealedClaimAvailable)
    assert (
        store.reserve_validated_deadlock_action(
            "item-1",
            _assign_command(),
            validate=lambda _item: pytest.fail("sealed follower callback은 호출되면 안 됩니다."),
        )
        == sealed
    )
    assert store.deadlock_claim_for_item("item-1") == sealed.claim
    assert store.deadlock_claim_for_handle(sealed.handle) == sealed.claim


def test_P174와_deadlock_API는_같은_item_winner를_공유하되_출처를_가장하지_않는다() -> None:
    deadlock_store = _store()
    unowned_command = AssignUnownedOwner(
        principal=ManagerPrincipal(org_id="org-1", subject_id="manager-1"),
        item_id="item-1",
        agent_id="refund-card",
        rationale="환불 담당으로 중재",
    )
    unowned_claim = ReservedAssignOwnerClaim(
        generation="generation-u",
        idempotency_key="manager-disposition:item-1",
        request_id="request-1",
        item_id="item-1",
        org_id="org-1",
        by_manager="manager-1",
        intent="refund",
        agent_id="refund-card",
        requires_approval=False,
        rationale="환불 담당으로 중재",
    )
    with pytest.raises(ManagerDispositionIntegrity):
        deadlock_store.reserve_validated_action(
            "item-1",
            unowned_command,
            lambda _item: unowned_claim,
        )
    assert deadlock_store.deadlock_claim_for_item("item-1") is None
    with pytest.raises(ManagerDispositionIntegrity):
        deadlock_store.claim_for_item("item-1")

    unowned_store = _store(_unowned_item())
    with pytest.raises(ManagerDispositionIntegrity):
        unowned_store.reserve_validated_deadlock_action(
            "item-1",
            _assign_command(),
            validate=lambda _item: _reserved_assign(),
        )
    assert unowned_store.claim_for_item("item-1") is None
    with pytest.raises(ManagerDispositionIntegrity):
        unowned_store.deadlock_claim_for_item("item-1")

    acquired = deadlock_store.reserve_validated_deadlock_action(
        "item-1", _assign_command(), validate=lambda _item: _reserved_assign()
    )
    assert isinstance(acquired, DeadlockManagerClaimAcquired)
    with pytest.raises(ManagerDispositionIntegrity):
        deadlock_store.claim_for_item("item-1")
    assert isinstance(
        deadlock_store.reserve_validated_action(
            "item-1",
            unowned_command,
            lambda _item: pytest.fail("다른 출처 winner 뒤 callback은 금지됩니다."),
        ),
        ClaimConflict,
    )


@pytest.mark.parametrize(
    "update",
    [
        {"request_id": "other"},
        {"case_id": "other"},
        {"item_id": "other", "idempotency_key": "manager-disposition:other"},
        {"org_id": "other"},
        {"by_manager": "other"},
        {"intent": "other"},
        {"round": 2, "cause": DivergentVotes(round=2)},
        {"agent_id": "outside-card"},
    ],
)
def test_deadlock_callback의_request_case_item_org_intent_round_cause를_exact검증한다(
    update: dict[str, object],
) -> None:
    store = _store()
    payload = _reserved_assign().model_dump(exclude={"kind"})
    payload.update(update)
    bad = ReservedDeadlockAssignClaim.model_validate(payload, strict=True)

    with pytest.raises(ManagerDispositionIntegrity):
        store.reserve_validated_deadlock_action(
            "item-1", _assign_command(), validate=lambda _item: bad
        )
    assert store.deadlock_claim_for_item("item-1") is None


def test_deadlock_callback_예외_재진입_subclass_변조는_write0이다() -> None:
    store = _store()

    def explode(_item: ManagerItem) -> ReservedDeadlockAssignClaim:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        store.reserve_validated_deadlock_action("item-1", _assign_command(), validate=explode)
    assert store.deadlock_claim_for_item("item-1") is None

    def reenter(_item: ManagerItem) -> ReservedDeadlockAssignClaim:
        with pytest.raises(ManagerDispositionIntegrity):
            store.reserve_validated_deadlock_action(
                "item-1", _assign_command(), validate=lambda _nested: _reserved_assign()
            )
        return _reserved_assign()

    with pytest.raises(ManagerDispositionIntegrity):
        store.reserve_validated_deadlock_action("item-1", _assign_command(), validate=reenter)
    assert store.deadlock_claim_for_item("item-1") is None

    subclass = type(
        "ReservedDeadlockAssignClaimSubclass",
        (ReservedDeadlockAssignClaim,),
        {},
    )(**_reserved_assign().model_dump(exclude={"kind"}))
    with pytest.raises(ManagerDispositionIntegrity):
        store.reserve_validated_deadlock_action(
            "item-1", _assign_command(), validate=lambda _item: subclass
        )
    assert store.deadlock_claim_for_item("item-1") is None

    mutated = _reserved_assign()
    object.__setattr__(mutated, "idempotency_key", "tampered")
    with pytest.raises(ManagerDispositionIntegrity):
        store.reserve_validated_deadlock_action(
            "item-1", _assign_command(), validate=lambda _item: mutated
        )
    assert store.deadlock_claim_for_item("item-1") is None


def test_deadlock_seal_abandon은_full_token과_exact_rejection_generation을_검증한다() -> None:
    store = _store()
    first = store.reserve_validated_deadlock_action(
        "item-1", _assign_command(), validate=lambda _item: _reserved_assign()
    )
    assert isinstance(first, DeadlockManagerClaimAcquired)
    assert isinstance(first.claim, ReservedDeadlockAssignClaim)
    rejection = RequestRouteGrantRejected(
        idempotency_key=first.claim.idempotency_key,
        reason_code="candidate_invalid",
    )

    with pytest.raises(ManagerDispositionIntegrity):
        store.abandon_unmutated_deadlock_assign(
            first.claim,
            control_token=first.control_token,
            rejection=rejection.model_copy(update={"idempotency_key": "wrong"}),
        )
    assert store.deadlock_claim_for_item("item-1") == first.claim
    store.abandon_unmutated_deadlock_assign(
        first.claim,
        control_token=first.control_token,
        rejection=rejection,
    )
    assert store.deadlock_claim_for_item("item-1") is None

    with pytest.raises(ManagerDispositionIntegrity):
        store.reserve_validated_deadlock_action(
            "item-1", _assign_command(), validate=lambda _item: _reserved_assign()
        )
    second = store.reserve_validated_deadlock_action(
        "item-1",
        _assign_command(),
        validate=lambda _item: _reserved_assign("generation-2"),
    )
    assert isinstance(second, DeadlockManagerClaimAcquired)
    with pytest.raises(ManagerDispositionIntegrity):
        store.seal_deadlock_claim(first.claim, control_token=first.control_token)
    with pytest.raises(ManagerDispositionIntegrity):
        store.seal_deadlock_claim(
            second.claim,
            control_token=DeadlockManagerReservationControlToken(
                generation=second.control_token.generation,
                token="wrong-secret",
            ),
        )


def test_deadlock_resume_evidence는_revision2_to3와_full_secret만_허용한다() -> None:
    store = _store()
    acquired = store.reserve_validated_deadlock_action(
        "item-1", _assign_command(), validate=lambda _item: _reserved_assign()
    )
    assert isinstance(acquired, DeadlockManagerClaimAcquired)
    sealed = store.seal_deadlock_claim(acquired.claim, control_token=acquired.control_token)
    evidence = _evidence()

    store.record_resume_evidence(sealed.handle, evidence)
    store.record_resume_evidence(sealed.handle, evidence)
    assert store.resume_evidence_for_claim(sealed.handle) == evidence
    assert store.resume_evidence_for_claim(sealed.handle) is not evidence

    wrong_secret = DeadlockManagerSealedClaimHandle(
        generation=sealed.handle.generation,
        forward_token="wrong",
    )
    with pytest.raises(ManagerDispositionIntegrity):
        store.resume_evidence_for_claim(wrong_secret)
    with pytest.raises(ManagerDispositionIntegrity):
        store.record_resume_evidence(
            sealed.handle,
            evidence.model_copy(update={"from_revision": 1, "to_revision": 2}),
        )
    with pytest.raises(ManagerDispositionIntegrity):
        store.record_resume_evidence(
            sealed.handle,
            evidence.model_copy(
                update={"route": evidence.route.model_copy(update={"authority_version": None})}
            ),
        )


def test_deadlock_resolve_for_claim은_assign_evidence와_exact_resolution을_요구하고_멱등이다() -> (
    None
):
    store = _store()
    acquired = store.reserve_validated_deadlock_action(
        "item-1", _assign_command(), validate=lambda _item: _reserved_assign()
    )
    assert isinstance(acquired, DeadlockManagerClaimAcquired)
    sealed = store.seal_deadlock_claim(acquired.claim, control_token=acquired.control_token)
    item = store.get("item-1")
    assert item is not None
    target = item.resolve(
        ManagerResolution(
            action=AssignOwner(
                by_manager="manager-1",
                primary="refund-card",
                rationale="환불 담당으로 중재",
            ),
            resolution=Resolution(
                intent="refund",
                primary="refund-card",
                rationale="환불 담당으로 중재",
            ),
        )
    )
    with pytest.raises(ManagerDispositionIntegrity):
        store.resolve_for_claim(sealed.handle, target)
    store.record_resume_evidence(sealed.handle, _evidence())
    assert store.resolve_for_claim(sealed.handle, target) == target
    assert store.resolve_for_claim(sealed.handle, target) == target
    with pytest.raises(ManagerDispositionIntegrity):
        store.resolve_for_claim(
            sealed.handle,
            replace(
                target,
                resolution=ManagerResolution(
                    action=AssignOwner(
                        by_manager="manager-1",
                        primary="finance-card",
                        rationale="환불 담당으로 중재",
                    ),
                    resolution=Resolution(
                        intent="refund",
                        primary="finance-card",
                        rationale="환불 담당으로 중재",
                    ),
                ),
            ),
        )


def test_deadlock_dismiss_resolve는_evidence없이_exact_action으로_닫는다() -> None:
    store = _store()
    acquired = store.reserve_validated_deadlock_action(
        "item-1", _dismiss_command(), validate=lambda _item: _reserved_dismiss()
    )
    assert isinstance(acquired, DeadlockManagerClaimAcquired)
    sealed = store.seal_deadlock_claim(acquired.claim, control_token=acquired.control_token)
    assert isinstance(sealed.claim, SealedDeadlockDismissClaim)
    with pytest.raises(ManagerDispositionIntegrity):
        store.record_resume_evidence(sealed.handle, _evidence())
    item = store.get("item-1")
    assert item is not None
    target = item.resolve(
        ManagerResolution(
            action=Dismiss(by_manager="manager-1", rationale="담당 없음"),
        )
    )
    assert store.resolve_for_claim(sealed.handle, target) == target


def test_deadlock_same_action과_mixed_action_32way는_claim하나로_수렴한다() -> None:
    store = _store()
    barrier = Barrier(32)

    def act(index: int) -> object:
        barrier.wait()
        command = _assign_command() if index % 2 == 0 else _dismiss_command()
        try:
            return store.reserve_validated_deadlock_action(
                "item-1",
                command,
                validate=(
                    (lambda _item: _reserved_assign("generation-assign"))
                    if index % 2 == 0
                    else (lambda _item: _reserved_dismiss("generation-dismiss"))
                ),
            )
        except ManagerDispositionIntegrity as error:
            return error

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(act, range(32)))

    winners = [result for result in results if isinstance(result, DeadlockManagerClaimAcquired)]
    conflicts = [result for result in results if isinstance(result, DeadlockManagerClaimConflict)]
    followers = [result for result in results if isinstance(result, DeadlockManagerClaimInProgress)]
    assert len(winners) == 1
    assert len(conflicts) == 16
    assert len(followers) == 15
    stored = store.deadlock_claim_for_item("item-1")
    assert stored == winners[0].claim


def test_deadlock_same_action_32way는_callback한번과_follower31개다() -> None:
    store = _store()
    barrier = Barrier(32)
    calls = 0

    def act(_index: int) -> object:
        nonlocal calls
        barrier.wait()

        def validate(_item: ManagerItem) -> ReservedDeadlockAssignClaim:
            nonlocal calls
            calls += 1
            return _reserved_assign()

        return store.reserve_validated_deadlock_action(
            "item-1", _assign_command(), validate=validate
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(act, range(32)))

    assert calls == 1
    assert sum(isinstance(result, DeadlockManagerClaimAcquired) for result in results) == 1
    assert sum(isinstance(result, DeadlockManagerClaimInProgress) for result in results) == 31
