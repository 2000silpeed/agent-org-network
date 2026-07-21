from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal

import pytest

from agent_org_network.conflict import Candidate, ConflictCase
from agent_org_network.p17_conflict_disposition import (
    CandidateRegistryChanged,
    ConcurOnConflict,
    ConcurrenceActionFingerprint,
    ConcurrencePendingStored,
    ConflictClaimAcquired,
    ConflictClaimConflict,
    ConflictDispositionIntegrity,
    ConflictResolutionEvidence,
    ConflictResolutionEvidenceRecorded,
    ConflictSealedClaimHandle,
    ConcurrenceVoteStored,
    FromDirectConsensus,
    InMemoryConflictDispositionStore,
    OwnerConcurrenceEvidence,
    OwnerPrincipal,
    ReservedConsensusClaim,
    SealedConflictClaimAvailable,
    SealedDeadlockClaim,
    SupportingKnowledgeEvidence,
    ValidatedOwnerVote,
    ValidatedRegistryEscalation,
)
from agent_org_network.question_request import RouteTarget
from agent_org_network.request_route_authority import RequestRouteGrantRejected


NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


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


def _command(
    owner: str,
    target: str,
    rationale: str = "",
    stance: Literal["withdraw", "keep_as_complement"] = "withdraw",
) -> ConcurOnConflict:
    return ConcurOnConflict(
        principal=OwnerPrincipal(org_id="org-1", subject_id=owner),
        case_id="case-1",
        expected_round=1,
        on_agent=target,
        stance=stance,
        rationale=rationale,
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


def _store(*ids: str) -> InMemoryConflictDispositionStore:
    store = InMemoryConflictDispositionStore(id_factory=_ids(*ids))
    stored, created = store.create_or_get_for_request(_case())
    assert created is True
    assert stored == _case()
    return store


def test_request_aware_case는_round_1이고_legacy_mutator와_alias를_차단한다() -> None:
    store = _store("unused")
    case = store.get_request_case("case-1")
    assert case is not None
    assert case.concurrence_round == 1
    assert case.status == "open"
    assert store.get("case-1") == case
    assert store.get("case-1") is not case

    with pytest.raises(ValueError, match="request-aware"):
        store.open_case(case)
    with pytest.raises(ValueError, match="request-aware"):
        store.mark_resolved(case.resolve_for_request("refund-card", "owners agreed"))


def test_concurrence_store는_pending뒤_direct_claim을_한번만_예약하고_seal한다() -> None:
    store = _store("generation-1", "control-1", "forward-1")
    first_command = _command("owner-a", "refund-card")
    first = store.reserve_validated_concurrence(
        "case-1",
        first_command,
        validate=lambda case, command: _validated(case, command),
    )
    assert first == ConcurrencePendingStored(current_round=1, pending_owners=("owner-b",))

    second_command = _command("owner-b", "refund-card")
    acquired = store.reserve_validated_concurrence(
        "case-1",
        second_command,
        validate=lambda case, command: _validated(case, command),
    )
    assert isinstance(acquired, ConflictClaimAcquired)
    assert isinstance(acquired.claim, ReservedConsensusClaim)
    assert acquired.claim.votes == (
        _validated(_case(), first_command).evidence,
        _validated(_case(), second_command).evidence,
    )

    follower = store.reserve_validated_concurrence(
        "case-1",
        first_command,
        validate=lambda case, command: _validated(case, command),
    )
    assert follower.kind == "in_progress"

    sealed = store.seal_consensus_claim(
        acquired.claim,
        control_token=acquired.control_token,
    )
    assert sealed.handle.generation == acquired.claim.generation
    retry = store.reserve_validated_concurrence(
        "case-1",
        first_command,
        validate=lambda case, command: _validated(case, command),
    )
    assert retry == sealed

    progress = store.progress_history_for_case("case-1")
    assert [entry.kind for entry in progress] == [
        "vote_stored",
        "vote_stored",
        "claim_reserved",
        "claim_sealed",
    ]
    assert [entry.position for entry in progress] == [1, 2, 3, 4]


def test_same_owner의_다른_표와_stale_round는_write없이_conflict다() -> None:
    store = _store("generation-1", "control-1")
    first = _command("owner-a", "refund-card")
    store.reserve_validated_concurrence(
        "case-1", first, validate=lambda case, command: _validated(case, command)
    )
    before = store.progress_history_for_case("case-1")

    different = _command("owner-a", "finance-card")
    result = store.reserve_validated_concurrence(
        "case-1", different, validate=lambda case, command: _validated(case, command)
    )
    assert isinstance(result, ConflictClaimConflict)
    assert store.progress_history_for_case("case-1") == before

    stale = first.model_copy(update={"expected_round": 2})
    with pytest.raises(ConflictDispositionIntegrity):
        store.reserve_validated_concurrence(
            "case-1", stale, validate=lambda case, command: _validated(case, command)
        )
    assert store.progress_history_for_case("case-1") == before


def test_divergent_votes는_same_lock에서_deadlock_claim과_full_handle을_seal한다() -> None:
    store = _store("generation-1", "forward-1")
    first = _command("owner-a", "refund-card")
    store.reserve_validated_concurrence(
        "case-1", first, validate=lambda case, command: _validated(case, command)
    )
    second = _command("owner-b", "finance-card")
    result = store.reserve_validated_concurrence(
        "case-1", second, validate=lambda case, command: _validated(case, command)
    )

    assert isinstance(result, SealedConflictClaimAvailable)
    assert isinstance(result.claim, SealedDeadlockClaim)
    assert result.claim.cause.kind == "divergent_votes"
    assert [entry.kind for entry in store.progress_history_for_case("case-1")] == [
        "vote_stored",
        "vote_stored",
        "claim_reserved",
        "claim_sealed",
    ]
    assert store.sealed_claim_for_case("case-1") == result


def test_registry_drift는_action을_vote로_쓰지_않고_exact_trigger만_follower다() -> None:
    store = _store("generation-1", "forward-1")
    command = _command("owner-a", "refund-card")

    def validate(case: ConflictCase, action: ConcurOnConflict) -> ValidatedRegistryEscalation:
        vote = _validated(case, action)
        return ValidatedRegistryEscalation(
            request_id=vote.request_id,
            case_id=vote.case_id,
            org_id=vote.org_id,
            intent=vote.intent,
            candidate_snapshot=vote.candidate_snapshot,
            trigger=vote.trigger,
            cause=CandidateRegistryChanged(round=1, reason_code="owner_changed"),
        )

    result = store.reserve_validated_concurrence("case-1", command, validate=validate)
    assert isinstance(result, SealedConflictClaimAvailable)
    assert result.claim.votes == ()
    assert [entry.kind for entry in store.progress_history_for_case("case-1")] == [
        "claim_reserved",
        "claim_sealed",
    ]
    assert store.reserve_validated_concurrence("case-1", command, validate=validate) == result

    other = _command("owner-b", "refund-card")
    assert isinstance(
        store.reserve_validated_concurrence("case-1", other, validate=validate),
        ConflictClaimConflict,
    )


def test_저장된_same_vote와_동일한_drift_invocation은_새_vote없이_기존_vote만_보존한다() -> None:
    store = _store("generation-1", "forward-1")
    command = _command("owner-a", "refund-card")
    store.reserve_validated_concurrence(
        "case-1", command, validate=lambda case, action: _validated(case, action)
    )

    def drift(case: ConflictCase, action: ConcurOnConflict) -> ValidatedRegistryEscalation:
        vote = _validated(case, action)
        return ValidatedRegistryEscalation(
            request_id=vote.request_id,
            case_id=vote.case_id,
            org_id=vote.org_id,
            intent=vote.intent,
            candidate_snapshot=vote.candidate_snapshot,
            trigger=vote.trigger,
            cause=CandidateRegistryChanged(round=1, reason_code="owner_changed"),
        )

    result = store.reserve_validated_concurrence("case-1", command, validate=drift)

    assert isinstance(result, SealedConflictClaimAvailable)
    assert result.claim.votes == (_validated(_case(), command).evidence,)
    progress_kinds = [entry.kind for entry in store.progress_history_for_case("case-1")]
    assert progress_kinds == [
        "vote_stored",
        "claim_reserved",
        "claim_sealed",
    ]
    assert progress_kinds.count("vote_stored") == 1


def test_write0_rejection만_round를_올리고_vote를_비우며_stale_token을_거부한다() -> None:
    store = _store("generation-1", "control-1", "generation-2", "control-2")
    first = _command("owner-a", "refund-card")
    second = _command("owner-b", "refund-card")
    store.reserve_validated_concurrence(
        "case-1", first, validate=lambda case, command: _validated(case, command)
    )
    acquired = store.reserve_validated_concurrence(
        "case-1", second, validate=lambda case, command: _validated(case, command)
    )
    assert isinstance(acquired, ConflictClaimAcquired)

    advanced = store.abandon_unmutated_consensus_round(
        acquired.claim,
        control_token=acquired.control_token,
        rejection=RequestRouteGrantRejected(
            idempotency_key=acquired.claim.idempotency_key,
            reason_code="policy_denied",
        ),
    )
    assert advanced.status == "open"
    assert advanced.concurrence_round == 2
    assert store.claim_for_case("case-1") is None
    assert store.progress_history_for_case("case-1")[-1].kind == "round_abandoned"

    with pytest.raises(ConflictDispositionIntegrity):
        store.seal_consensus_claim(acquired.claim, control_token=acquired.control_token)


def test_abandon뒤_generation_재사용은_새_vote나_claim을_쓰지않는다() -> None:
    store = _store("generation-1", "control-1", "generation-1")
    first_round = (
        _command("owner-a", "refund-card"),
        _command("owner-b", "refund-card"),
    )
    store.reserve_validated_concurrence(
        "case-1",
        first_round[0],
        validate=lambda case, command: _validated(case, command),
    )
    acquired = store.reserve_validated_concurrence(
        "case-1",
        first_round[1],
        validate=lambda case, command: _validated(case, command),
    )
    assert isinstance(acquired, ConflictClaimAcquired)
    store.abandon_unmutated_consensus_round(
        acquired.claim,
        control_token=acquired.control_token,
        rejection=RequestRouteGrantRejected(
            idempotency_key=acquired.claim.idempotency_key,
            reason_code="policy_denied",
        ),
    )

    second_round = (
        first_round[0].model_copy(update={"expected_round": 2}),
        first_round[1].model_copy(update={"expected_round": 2}),
    )
    store.reserve_validated_concurrence(
        "case-1",
        second_round[0],
        validate=lambda case, command: _validated(case, command),
    )
    before = store.progress_history_for_case("case-1")

    with pytest.raises(ConflictDispositionIntegrity, match="generation"):
        store.reserve_validated_concurrence(
            "case-1",
            second_round[1],
            validate=lambda case, command: _validated(case, command),
        )

    assert store.progress_history_for_case("case-1") == before
    assert store.claim_for_case("case-1") is None


def test_resolution_evidence는_full_handle에_묶여_같은값만_noop이다() -> None:
    store = _store("generation-1", "control-1", "forward-1")
    commands = (
        _command("owner-a", "refund-card", "A"),
        _command("owner-b", "refund-card", "B", "keep_as_complement"),
    )
    store.reserve_validated_concurrence(
        "case-1", commands[0], validate=lambda case, command: _validated(case, command)
    )
    acquired = store.reserve_validated_concurrence(
        "case-1", commands[1], validate=lambda case, command: _validated(case, command)
    )
    assert isinstance(acquired, ConflictClaimAcquired)
    sealed = store.seal_consensus_claim(acquired.claim, control_token=acquired.control_token)
    evidence = ConflictResolutionEvidence(
        request_id="request-1",
        case_id="case-1",
        org_id="org-1",
        intent="refund",
        route=RouteTarget(
            intent="refund",
            agent_id="refund-card",
            requires_approval=False,
            authority_version="grant-v1",
        ),
        source=FromDirectConsensus(round=1, votes=sealed.claim.votes),
        supporting=(
            SupportingKnowledgeEvidence(
                agent_id="finance-card",
                affirmed_by_owner="owner-b",
            ),
        ),
    )

    missing_support = evidence.model_copy(update={"supporting": ()})
    with pytest.raises(ConflictDispositionIntegrity, match="supporting"):
        store.record_resolution_evidence(sealed.handle, missing_support)
    extra_support = evidence.model_copy(
        update={
            "supporting": evidence.supporting
            + (
                SupportingKnowledgeEvidence(
                    agent_id="refund-card",
                    affirmed_by_owner="owner-a",
                ),
            )
        }
    )
    with pytest.raises(ConflictDispositionIntegrity, match="supporting"):
        store.record_resolution_evidence(sealed.handle, extra_support)
    assert store.resolution_evidence_for_request("request-1") is None

    store.record_resolution_evidence(sealed.handle, evidence)
    store.record_resolution_evidence(sealed.handle, evidence)
    assert store.resolution_evidence_for_request("request-1") == evidence
    assert (
        sum(
            isinstance(entry, ConflictResolutionEvidenceRecorded)
            for entry in store.progress_history_for_case("case-1")
        )
        == 1
    )

    forged = ConflictSealedClaimHandle(
        generation=sealed.handle.generation,
        forward_token="different-secret",
    )
    with pytest.raises(ConflictDispositionIntegrity):
        store.record_resolution_evidence(forged, evidence)

    current = store.get_request_case("case-1")
    assert current is not None
    with pytest.raises(ConflictDispositionIntegrity, match="target"):
        store.transition_for_claim(sealed.handle, target=current)
    wrong_rationale = current.resolve_for_request("refund-card", "tampered")
    with pytest.raises(ConflictDispositionIntegrity, match="target"):
        store.transition_for_claim(sealed.handle, target=wrong_rationale)
    target = current.resolve_for_request(
        "refund-card",
        "owner-a→refund-card; owner-b→refund-card",
    )
    assert store.transition_for_claim(sealed.handle, target=target) == target
    assert store.transition_for_claim(sealed.handle, target=target) == target

    wrong_round = replace(target, concurrence_round=2)
    with pytest.raises(ConflictDispositionIntegrity, match="target"):
        store.transition_for_claim(sealed.handle, target=wrong_round)

    assert isinstance(store.progress_history_for_case("case-1")[0], ConcurrenceVoteStored)


def test_validation_callback_reentry와_exception은_progress_write_0이다() -> None:
    store = _store("generation-1", "control-1")
    command = _command("owner-a", "refund-card")

    def reenter(case: ConflictCase, action: ConcurOnConflict) -> ValidatedOwnerVote:
        with pytest.raises(ConflictDispositionIntegrity):
            store.reserve_validated_concurrence(
                case.case_id,
                action,
                validate=lambda nested_case, nested_action: _validated(nested_case, nested_action),
            )
        return _validated(case, action)

    with pytest.raises(ConflictDispositionIntegrity):
        store.reserve_validated_concurrence("case-1", command, validate=reenter)
    assert store.progress_history_for_case("case-1") == ()

    with pytest.raises(RuntimeError, match="boom"):
        store.reserve_validated_concurrence(
            "case-1",
            command,
            validate=lambda _case, _command: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    assert store.progress_history_for_case("case-1") == ()


def test_validation_callback이_frozen반환값을_강제변조해도_progress_write_0이다() -> None:
    store = _store("generation-1", "control-1")
    command = _command("owner-a", "refund-card")

    def tamper(case: ConflictCase, action: ConcurOnConflict) -> ValidatedOwnerVote:
        result = _validated(case, action)
        object.__setattr__(result.evidence, "on_agent", "finance-card")
        return result

    with pytest.raises(ConflictDispositionIntegrity, match="validated vote"):
        store.reserve_validated_concurrence("case-1", command, validate=tamper)

    assert store.progress_history_for_case("case-1") == ()
    assert store.claim_for_case("case-1") is None
