from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from typing import Literal

import pytest
from pydantic import ValidationError

from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    CompletionEvidenceError,
    InMemoryQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalAuthorization,
    ApprovalBoundary,
    ApprovalConcurrencyError,
    ApprovalDraft,
    ApprovalItem,
    ApprovalItemMismatchError,
    ApprovalPending,
    ApprovalRequired,
    ApprovalSupersession,
    ApprovalUnavailable,
    ApprovalUnavailabilityEvidence,
    Approve,
    ApprovedCandidate,
    ApproverPrincipal,
    AnswerCandidate,
    InMemoryApprovalStore,
    Reject,
)
from agent_org_network.question_request import (
    AwaitingApproval,
    CompareAndSetError,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    QuestionRequestTransitionError,
    ReadyToDispatch,
    RouteTarget,
    validate_compare_and_set_semantics,
)


T0 = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=10)
T2 = T1 + timedelta(minutes=10)
T3 = T2 + timedelta(minutes=10)
ROUTE = RouteTarget(
    intent="refund",
    agent_id="refund-owner",
    requires_approval=True,
    authority_version="route-v1",
)
CANDIDATE = AnswerCandidate(
    text="환불할 수 있습니다.",
    sources=("refund-policy.md",),
    mode="full",
    snapshot_sha="candidate-sha",
)
DRAFT = ApprovalDraft(
    draft_id="draft-1",
    request_id="request-1",
    attempt=1,
    route=ROUTE,
    candidate=CANDIDATE,
    created_at=T0,
)


def _item(
    *,
    item_id: str = "approval-1",
    org_id: str = "org-1",
    request_id: str = "request-1",
    awaiting_revision: int = 2,
    attempt: int = 1,
    route: RouteTarget = ROUTE,
    draft: ApprovalDraft = DRAFT,
    approver_id: str = "alice",
    policy_version: str = "approval-v1",
    created_at: datetime = T0,
    due_at: datetime = T2,
    approval_round: int = 1,
    supersedes_item_id: str | None = None,
) -> ApprovalItem:
    return ApprovalItem(
        item_id=item_id,
        org_id=org_id,
        request_id=request_id,
        awaiting_revision=awaiting_revision,
        attempt=attempt,
        route=route,
        draft=draft,
        requirement=ApprovalRequired(
            approver_id=approver_id,
            policy_version=policy_version,
        ),
        created_at=created_at,
        due_at=due_at,
        approval_round=approval_round,
        supersedes_item_id=supersedes_item_id,
    )


def _successor(
    *,
    predecessor: ApprovalItem | None = None,
    item_id: str = "approval-2",
    approver_id: str = "bob",
    created_at: datetime = T1,
    due_at: datetime = T2,
) -> ApprovalItem:
    previous = predecessor or _item()
    return ApprovalItem(
        item_id=item_id,
        org_id=previous.org_id,
        request_id=previous.request_id,
        awaiting_revision=previous.awaiting_revision + 1,
        attempt=previous.attempt,
        route=previous.route,
        draft=previous.draft,
        requirement=ApprovalRequired(
            approver_id=approver_id,
            policy_version="approval-v2",
        ),
        created_at=created_at,
        due_at=due_at,
        approval_round=previous.approval_round + 1,
        supersedes_item_id=previous.item_id,
    )


def _supersession(
    *,
    successor_item_id: str = "approval-2",
    reason: Literal["expired", "reassigned"] = "reassigned",
    superseded_at: datetime = T1,
) -> ApprovalSupersession:
    return ApprovalSupersession(
        reason=reason,
        successor_item_id=successor_item_id,
        superseded_at=superseded_at,
    )


def _unavailability(
    item: ApprovalItem,
    *,
    unavailable_at: datetime = T2,
    policy_version: str = "expiry-v1",
    evidence_ref: str = "eligibility:no-fallback:v1",
) -> ApprovalUnavailabilityEvidence:
    return ApprovalUnavailabilityEvidence(
        decision=ApprovalUnavailable(
            assignment_generation=ApprovalAssignmentGeneration.from_item(item),
            policy_version=policy_version,
            authority_version="org-directory-v1",
            evidence_ref=evidence_ref,
        ),
        unavailable_at=unavailable_at,
    )


def _awaiting_request(
    *,
    store: InMemoryQuestionRequestStore | None = None,
    request_id: str = "request-1",
) -> QuestionRequest:
    requests = store or InMemoryQuestionRequestStore()
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불해 주세요.",
        request_id_factory=lambda: request_id,
        clock=lambda: T0,
        due_at=T1,
    )
    requests.create(received)
    trigger_key = f"request-dispatch:{request_id}:1"
    ready = received.record_initial_routing(
        intent=ROUTE.intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=ROUTE,
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=T1,
            ),
        ),
        clock=lambda: T0,
    )
    assert requests.compare_and_set(request_id, 0, received, ready)
    awaiting = ready.transition(
        AwaitingApproval(
            route=ROUTE,
            attempt=1,
            draft_ref="approval-1",
            handling=HandlingAssignment(
                kind="approval_item",
                ref="approval-1",
                due_at=T1,
            ),
        ),
        clock=lambda: T0,
    )
    assert requests.compare_and_set(request_id, 1, ready, awaiting)
    return awaiting


def test_legacy_approval_item_payload_gets_round_one_defaults() -> None:
    payload = _item().model_dump(exclude={"approval_round", "supersedes_item_id", "supersession"})

    restored = ApprovalItem.model_validate(payload, strict=True)

    assert restored.approval_round == 1
    assert restored.supersedes_item_id is None
    assert restored.supersession is None
    assert restored.status == "open"


def test_later_generation_can_reuse_the_exact_earlier_draft() -> None:
    successor = _successor(created_at=T1)

    assert successor.draft == DRAFT
    assert successor.draft.draft_id == DRAFT.draft_id
    assert successor.created_at > successor.draft.created_at
    assert successor.approval_round == 2
    assert successor.supersedes_item_id == "approval-1"


@pytest.mark.parametrize(
    "updates",
    [
        {"approval_round": 1, "supersedes_item_id": "approval-0"},
        {"approval_round": 2, "supersedes_item_id": None},
        {"created_at": T0 - timedelta(seconds=1)},
    ],
)
def test_approval_item_rejects_invalid_generation_lineage(
    updates: dict[str, object],
) -> None:
    payload = _item().model_dump()
    payload.update(updates)

    with pytest.raises(ValidationError):
        ApprovalItem.model_validate(payload, strict=True)


def test_superseded_item_requires_exact_typed_evidence() -> None:
    item = _item()
    evidence = _supersession()

    superseded = item.supersede(evidence)

    assert superseded.status == "superseded"
    assert superseded.resolution is None
    assert superseded.supersession == evidence

    missing = superseded.model_dump(exclude={"supersession"})
    with pytest.raises(ValidationError):
        ApprovalItem.model_validate(missing, strict=True)


@pytest.mark.parametrize(
    ("reason", "successor_item_id", "superseded_at"),
    [
        ("timeout", "approval-2", T1),
        ("reassigned", " ", T1),
        ("expired", "approval-2", T1.replace(tzinfo=None)),
    ],
)
def test_supersession_evidence_is_strict(
    reason: str,
    successor_item_id: str,
    superseded_at: datetime,
) -> None:
    with pytest.raises(ValidationError):
        ApprovalSupersession.model_validate(
            {
                "reason": reason,
                "successor_item_id": successor_item_id,
                "superseded_at": superseded_at,
            },
            strict=True,
        )


def test_resolve_preserves_generation_lineage_and_rejects_superseded_item() -> None:
    successor = _successor()
    action = Reject(by_approver="bob", reason_code="unsupported")

    resolved = successor.resolve(
        action=action,
        approved_candidate=None,
        resolved_at=T2 - timedelta(microseconds=1),
    )

    assert resolved.status == "resolved"
    assert resolved.approval_round == 2
    assert resolved.supersedes_item_id == "approval-1"
    assert resolved.supersession is None

    superseded = _item().supersede(_supersession())
    with pytest.raises(ApprovalConcurrencyError):
        superseded.resolve(
            action=Reject(by_approver="alice", reason_code="late"),
            approved_candidate=None,
            resolved_at=T2,
        )


def test_unavailable_item_uses_system_evidence_not_human_resolution() -> None:
    item = _item()
    evidence = _unavailability(item)

    unavailable = item.close_unavailable(evidence)

    assert unavailable.status == "unavailable"
    assert unavailable.resolution is None
    assert unavailable.supersession is None
    assert unavailable.unavailability == evidence
    with pytest.raises(ValidationError):
        ApprovalItem.model_validate(
            unavailable.model_dump(exclude={"unavailability"}),
            strict=True,
        )


def test_store_due_scan_and_unavailable_close_are_exact_and_idempotent() -> None:
    store = InMemoryApprovalStore()
    item = _item()
    store.create_or_get(item)

    assert store.due_open(T2, limit=10) == [item]
    evidence = _unavailability(item)
    closed, created = store.close_unavailable_if_open(
        item.item_id,
        ApprovalAssignmentGeneration.from_item(item),
        evidence,
    )
    replay, replay_created = store.close_unavailable_if_open(
        item.item_id,
        ApprovalAssignmentGeneration.from_item(item),
        evidence,
    )

    assert created is True
    assert replay_created is False
    assert replay == closed
    assert closed.status == "unavailable"
    assert store.get(item.item_id) == closed
    assert store.get_by_request_attempt(item.request_id, item.attempt) == closed
    assert store.get_by_request_attempt_round(item.request_id, item.attempt, 1) == closed
    assert store.due_open(T3, limit=10) == []
    assert len(store.history) == 2


@pytest.mark.parametrize("invalid_limit", [True, False, 1.5, "1", None])
def test_store_due_scan_requires_exact_positive_integer_limit(
    invalid_limit: object,
) -> None:
    store = InMemoryApprovalStore()
    store.create_or_get(_item())

    with pytest.raises(ValueError, match="양의 정수"):
        store.due_open(T2, invalid_limit)  # type: ignore[arg-type]


def test_unavailable_store_item_cannot_be_overwritten_by_resolve_transition() -> None:
    store = InMemoryApprovalStore()
    item = _item()
    store.create_or_get(item)
    unavailable, _ = store.close_unavailable_if_open(
        item.item_id,
        ApprovalAssignmentGeneration.from_item(item),
        _unavailability(item),
    )
    action = Reject(by_approver="alice", reason_code="late")
    forged_resolved = item.resolve(
        action=action,
        approved_candidate=None,
        resolved_at=T2 - timedelta(microseconds=1),
    )
    transition_called = False

    def transition(_: ApprovalItem) -> ApprovalItem:
        nonlocal transition_called
        transition_called = True
        return forged_resolved

    with pytest.raises(ApprovalConcurrencyError, match="open 상태"):
        store.resolve_if_open(item.item_id, action, transition)

    assert transition_called is False
    assert store.get(item.item_id) == unavailable


def test_approval_item_rejects_expired_supersession_before_due() -> None:
    item = _item()

    with pytest.raises(ValidationError, match="due_at"):
        item.supersede(
            _supersession(
                reason="expired",
                superseded_at=T1,
            )
        )


def test_store_unavailable_and_expired_reassignment_enforce_due_and_generation() -> None:
    store = InMemoryApprovalStore()
    predecessor = _item()
    store.create_or_get(predecessor)
    wrong_generation = ApprovalAssignmentGeneration.from_item(
        predecessor.model_copy(update={"due_at": T3})
    )

    with pytest.raises(ApprovalItemMismatchError):
        store.close_unavailable_if_open(
            predecessor.item_id,
            wrong_generation,
            _unavailability(predecessor),
        )
    with pytest.raises(ApprovalConcurrencyError, match="기한"):
        store.supersede_and_create_if_open(
            predecessor.item_id,
            _supersession(reason="expired", superseded_at=T1),
            _successor(predecessor=predecessor, created_at=T1, due_at=T3),
            expected_generation=ApprovalAssignmentGeneration.from_item(predecessor),
        )

    successor = _successor(predecessor=predecessor, created_at=T2, due_at=T3)
    stored, created = store.supersede_and_create_if_open(
        predecessor.item_id,
        _supersession(reason="expired", superseded_at=T2),
        successor,
        expected_generation=ApprovalAssignmentGeneration.from_item(predecessor),
    )
    assert created is True
    assert stored == successor


def test_store_rejects_supersession_target_that_differs_from_successor_requirement() -> None:
    store = InMemoryApprovalStore()
    predecessor = _item()
    successor = _successor(
        predecessor=predecessor,
        approver_id="carol",
        created_at=T1,
        due_at=T3,
    )
    store.create_or_get(predecessor)
    mismatched = ApprovalSupersession(
        reason="reassigned",
        successor_item_id=successor.item_id,
        superseded_at=T1,
        policy_version="manual-v1",
        authority_version="authority-v1",
        evidence_ref="grant-1",
        actor_id="operator-1",
        target_approver_id="bob",
    )

    with pytest.raises(ApprovalItemMismatchError):
        store.supersede_and_create_if_open(
            predecessor.item_id,
            mismatched,
            successor,
            expected_generation=ApprovalAssignmentGeneration.from_item(predecessor),
        )

    assert store.get(predecessor.item_id) == predecessor
    assert store.get(successor.item_id) is None


def test_store_keeps_current_and_round_history_after_successor_resolution() -> None:
    store = InMemoryApprovalStore()
    predecessor = _item()
    successor = _successor(predecessor=predecessor, due_at=T3)
    store.create_or_get(predecessor)

    stored_successor, created = store.supersede_and_create_if_open(
        predecessor.item_id,
        _supersession(),
        successor,
    )

    assert created is True
    assert stored_successor == successor
    stored_predecessor = store.get(predecessor.item_id)
    assert stored_predecessor is not None
    assert stored_predecessor.status == "superseded"
    assert stored_predecessor.supersession == _supersession()
    assert store.get_by_request_attempt("request-1", 1) == successor
    assert store.get_by_request_attempt_round("request-1", 1, 1) == stored_predecessor
    assert store.get_by_request_attempt_round("request-1", 1, 2) == successor
    assert store.generations("request-1", 1) == [stored_predecessor, successor]
    assert stored_predecessor.due_at == T2
    assert successor.due_at == T3
    assert len(store.history) == 3

    action = Reject(by_approver="bob", reason_code="unsupported")
    resolved = store.resolve_if_open(
        successor.item_id,
        action,
        lambda current: current.resolve(
            action=action,
            approved_candidate=None,
            resolved_at=T2,
        ),
    )

    assert store.get_by_request_attempt("request-1", 1) == resolved
    assert store.get_by_request_attempt_round("request-1", 1, 2) == resolved
    assert store.generations("request-1", 1) == [stored_predecessor, resolved]
    assert resolved.due_at == T3
    assert len(store.history) == 4


def test_store_rejects_invalid_successor_links_before_any_write() -> None:
    invalid_updates = (
        {"request_id": "other-request"},
        {"org_id": "other-org"},
        {"awaiting_revision": 9},
        {"attempt": 2},
        {"route": ROUTE.model_copy(update={"agent_id": "other-owner"})},
        {"draft": DRAFT.model_copy(update={"draft_id": "other-draft"})},
        {"created_at": T2},
        {"approval_round": 3},
        {"supersedes_item_id": "approval-0"},
    )

    for updates in invalid_updates:
        store = InMemoryApprovalStore()
        predecessor = _item()
        store.create_or_get(predecessor)
        successor = _successor().model_copy(update=updates)

        with pytest.raises(ApprovalItemMismatchError):
            store.supersede_and_create_if_open(
                predecessor.item_id,
                _supersession(),
                successor,
            )

        assert store.get(predecessor.item_id) == predecessor
        assert store.get("approval-2") is None


def test_supersede_same_canonical_retry_follows_stored_successor() -> None:
    store = InMemoryApprovalStore()
    predecessor = _item()
    successor = _successor()
    evidence = _supersession()
    store.create_or_get(predecessor)

    first, first_created = store.supersede_and_create_if_open(
        predecessor.item_id,
        evidence,
        successor,
    )
    retried, retry_created = store.supersede_and_create_if_open(
        predecessor.item_id,
        evidence,
        successor,
    )

    assert first_created is True
    assert retry_created is False
    assert retried == first
    assert store.get(predecessor.item_id) is not None
    assert store.get(predecessor.item_id).status == "superseded"  # type: ignore[union-attr]

    conflicting = _successor(item_id="approval-other", approver_id="carol")
    with pytest.raises(ApprovalConcurrencyError):
        store.supersede_and_create_if_open(
            predecessor.item_id,
            _supersession(successor_item_id="approval-other"),
            conflicting,
        )


def test_original_retry_follows_successor_after_later_lifecycle_changes() -> None:
    resolved_store = InMemoryApprovalStore()
    predecessor = _item()
    successor = _successor()
    evidence = _supersession()
    resolved_store.create_or_get(predecessor)
    resolved_store.supersede_and_create_if_open(
        predecessor.item_id,
        evidence,
        successor,
    )
    action = Reject(by_approver="bob", reason_code="unsupported")
    resolved = resolved_store.resolve_if_open(
        successor.item_id,
        action,
        lambda current: current.resolve(
            action=action,
            approved_candidate=None,
            resolved_at=T2 - timedelta(microseconds=1),
        ),
    )

    followed_resolved, created = resolved_store.supersede_and_create_if_open(
        predecessor.item_id,
        evidence,
        successor,
    )
    assert created is False
    assert followed_resolved == resolved

    superseded_store = InMemoryApprovalStore()
    superseded_store.create_or_get(predecessor)
    superseded_store.supersede_and_create_if_open(
        predecessor.item_id,
        evidence,
        successor,
    )
    third = _successor(
        predecessor=successor,
        item_id="approval-3",
        approver_id="carol",
        created_at=T2,
    )
    superseded_store.supersede_and_create_if_open(
        successor.item_id,
        _supersession(successor_item_id="approval-3", superseded_at=T2),
        third,
    )

    followed_superseded, created = superseded_store.supersede_and_create_if_open(
        predecessor.item_id,
        evidence,
        successor,
    )
    assert created is False
    assert followed_superseded.status == "superseded"
    assert followed_superseded.item_id == successor.item_id


def test_successor_item_id_collision_is_zero_write() -> None:
    store = InMemoryApprovalStore()
    predecessor = _item()
    occupied_draft = DRAFT.model_copy(
        update={"draft_id": "occupied-draft", "request_id": "request-other"}
    )
    occupied = _item(
        item_id="approval-2",
        request_id="request-other",
        draft=occupied_draft,
    )
    store.create_or_get(predecessor)
    store.create_or_get(occupied)

    with pytest.raises(ApprovalItemMismatchError, match="item_id"):
        store.supersede_and_create_if_open(
            predecessor.item_id,
            _supersession(),
            _successor(),
        )

    assert store.get(predecessor.item_id) == predecessor
    assert store.get_by_request_attempt("request-1", 1) == predecessor
    assert store.generations("request-1", 1) == [predecessor]
    assert len(store.history) == 2


class _ApprovalItemSubclass(ApprovalItem):
    injected: str


class _NoExtraApprovalItemSubclass(ApprovalItem):
    pass


@pytest.mark.parametrize("subclass_type", [_ApprovalItemSubclass, _NoExtraApprovalItemSubclass])
def test_noncanonical_successor_subclass_is_zero_write(
    subclass_type: type[ApprovalItem],
) -> None:
    store = InMemoryApprovalStore()
    predecessor = _item()
    store.create_or_get(predecessor)
    payload = _successor().model_dump(mode="python", round_trip=True)
    if subclass_type is _ApprovalItemSubclass:
        payload["injected"] = "unexpected"
    subclass = subclass_type(**payload)

    with pytest.raises(ApprovalItemMismatchError, match="exact type"):
        store.supersede_and_create_if_open(
            predecessor.item_id,
            _supersession(),
            subclass,
        )

    assert store.get(predecessor.item_id) == predecessor
    assert store.get("approval-2") is None
    assert len(store.history) == 1


class _ResponseLossApprovalStore(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.lose_once = True

    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]:
        result = super().supersede_and_create_if_open(
            item_id,
            supersession,
            successor,
            expected_generation=expected_generation,
        )
        if self.lose_once:
            self.lose_once = False
            raise RuntimeError("caller lost the committed successor")
        return result


def test_supersede_response_loss_retry_reads_the_committed_successor() -> None:
    store = _ResponseLossApprovalStore()
    predecessor = _item()
    successor = _successor()
    evidence = _supersession()
    store.create_or_get(predecessor)

    with pytest.raises(RuntimeError, match="lost the committed successor"):
        store.supersede_and_create_if_open(predecessor.item_id, evidence, successor)

    retried, created = store.supersede_and_create_if_open(
        predecessor.item_id,
        evidence,
        successor,
    )
    assert created is False
    assert retried == successor
    assert store.get_by_request_attempt("request-1", 1) == successor


def test_concurrent_supersede_has_one_successor_winner() -> None:
    store = InMemoryApprovalStore()
    predecessor = _item()
    store.create_or_get(predecessor)
    proposals = (
        (_supersession(successor_item_id="approval-2"), _successor(item_id="approval-2")),
        (
            _supersession(successor_item_id="approval-3"),
            _successor(item_id="approval-3", approver_id="carol"),
        ),
    )
    barrier = Barrier(2)

    def compete(
        proposal: tuple[ApprovalSupersession, ApprovalItem],
    ) -> tuple[ApprovalItem, bool] | ApprovalConcurrencyError:
        barrier.wait()
        try:
            return store.supersede_and_create_if_open(
                predecessor.item_id,
                proposal[0],
                proposal[1],
            )
        except ApprovalConcurrencyError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, proposals))

    assert sum(isinstance(result, ApprovalConcurrencyError) for result in results) == 1
    winners = [result for result in results if not isinstance(result, ApprovalConcurrencyError)]
    assert len(winners) == 1
    winner, created = winners[0]
    assert created is True
    assert store.get_by_request_attempt("request-1", 1) == winner
    assert len(store.generations("request-1", 1)) == 2


def test_concurrent_same_supersede_converges_to_one_creation() -> None:
    store = InMemoryApprovalStore()
    predecessor = _item()
    successor = _successor()
    evidence = _supersession()
    store.create_or_get(predecessor)

    def supersede(_: int) -> tuple[ApprovalItem, bool]:
        return store.supersede_and_create_if_open(
            predecessor.item_id,
            evidence,
            successor,
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(supersede, range(32)))

    assert sum(created for _, created in results) == 1
    assert all(stored == successor for stored, _ in results)
    assert store.get_by_request_attempt("request-1", 1) == successor
    assert len(store.history) == 3


def test_open_items_for_approver_are_current_deterministic_canonical_copies() -> None:
    store = InMemoryApprovalStore()
    later_request_draft = DRAFT.model_copy(
        update={"draft_id": "draft-2", "request_id": "request-2", "created_at": T1}
    )
    later = _item(
        item_id="approval-z",
        request_id="request-2",
        draft=later_request_draft,
        created_at=T1,
    )
    earlier_request_draft = DRAFT.model_copy(
        update={"draft_id": "draft-0", "request_id": "request-0"}
    )
    earlier = _item(
        item_id="approval-a",
        request_id="request-0",
        draft=earlier_request_draft,
    )
    other_approver_draft = DRAFT.model_copy(
        update={"draft_id": "draft-3", "request_id": "request-3"}
    )
    other_approver = _item(
        item_id="approval-c",
        request_id="request-3",
        draft=other_approver_draft,
        approver_id="carol",
    )
    for item in (later, other_approver, earlier):
        store.create_or_get(item)

    queue = store.open_for_designated_approver("org-1", "alice")

    assert [item.item_id for item in queue] == ["approval-a", "approval-z"]
    object.__setattr__(queue[0], "item_id", "mutated")
    fresh = store.open_for_designated_approver("org-1", "alice")
    assert [item.item_id for item in fresh] == ["approval-a", "approval-z"]
    carol = store.open_for_designated_approver("org-1", "carol")
    assert [item.item_id for item in carol] == [other_approver.item_id]


def test_dedicated_reassignment_updates_only_item_ref_sla_and_revision() -> None:
    current = _awaiting_request()

    updated = current.reassign_approval(
        previous_item_id="approval-1",
        successor_item_id="approval-2",
        due_at=T3,
        clock=lambda: T1,
    )

    assert updated.revision == current.revision + 1
    assert updated.updated_at == T1
    updated_state = updated.state
    current_state = current.state
    assert isinstance(updated_state, AwaitingApproval)
    assert isinstance(current_state, AwaitingApproval)
    assert updated_state.route == current_state.route
    assert updated_state.attempt == current_state.attempt
    assert updated_state.draft_ref == "approval-2"
    assert updated_state.handling == HandlingAssignment(
        kind="approval_item",
        ref="approval-2",
        due_at=T3,
    )
    validate_compare_and_set_semantics(
        current.request_id,
        current.revision,
        current,
        updated,
    )

    with pytest.raises(QuestionRequestTransitionError, match="same-state"):
        current.transition(updated_state, clock=lambda: T1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"previous_item_id": "other-item"},
        {"successor_item_id": "approval-1"},
        {"successor_item_id": " "},
        {"due_at": T0 - timedelta(seconds=1)},
        {"clock": lambda: T0 - timedelta(seconds=1)},
    ],
)
def test_dedicated_reassignment_rejects_stale_or_invalid_links(
    kwargs: dict[str, object],
) -> None:
    current = _awaiting_request()
    arguments: dict[str, object] = {
        "previous_item_id": "approval-1",
        "successor_item_id": "approval-2",
        "due_at": T3,
        "clock": lambda: T1,
    }
    arguments.update(kwargs)

    with pytest.raises(QuestionRequestTransitionError):
        current.reassign_approval(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "state",
    [
        AwaitingApproval(
            route=ROUTE,
            attempt=1,
            draft_ref="approval-1",
            handling=HandlingAssignment(
                kind="approval_item",
                ref="approval-1",
                due_at=T3,
            ),
        ),
        AwaitingApproval(
            route=ROUTE.model_copy(update={"agent_id": "other-owner"}),
            attempt=1,
            draft_ref="approval-2",
            handling=HandlingAssignment(
                kind="approval_item",
                ref="approval-2",
                due_at=T3,
            ),
        ),
        AwaitingApproval(
            route=ROUTE,
            attempt=2,
            draft_ref="approval-2",
            handling=HandlingAssignment(
                kind="approval_item",
                ref="approval-2",
                due_at=T3,
            ),
        ),
    ],
)
def test_cas_rejects_forged_same_state_updates(state: AwaitingApproval) -> None:
    current = _awaiting_request()
    forged = QuestionRequest.model_validate(
        {
            **current.model_dump(),
            "state": state,
            "revision": current.revision + 1,
            "updated_at": T1,
        },
        strict=True,
    )

    with pytest.raises(CompareAndSetError):
        validate_compare_and_set_semantics(
            current.request_id,
            current.revision,
            current,
            forged,
        )


def test_cas_rejects_unvalidated_same_state_handling_forgery() -> None:
    current = _awaiting_request()
    current_state = current.state
    assert isinstance(current_state, AwaitingApproval)
    forged_state = current_state.model_copy(
        update={
            "draft_ref": "approval-2",
            "handling": current_state.handling.model_copy(
                update={"ref": "other-item", "due_at": T3}
            ),
        }
    )
    forged = current.model_copy(
        update={
            "state": forged_state,
            "revision": current.revision + 1,
            "updated_at": T1,
        }
    )

    with pytest.raises(CompareAndSetError, match="exact-link"):
        validate_compare_and_set_semantics(
            current.request_id,
            current.revision,
            current,
            forged,
        )


class _RequiredPolicy:
    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: str,
    ) -> ApprovalRequired:
        return ApprovalRequired(approver_id="alice", policy_version="approval-v1")


class _AllowAuthorizer:
    def authorize(
        self,
        org_id: str,
        designated_approver_id: str,
        actor_id: str,
        action_kind: str,
        policy_version: str,
    ) -> ApprovalAuthorization:
        return ApprovalAuthorization(policy_version=policy_version)


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        return started_at + timedelta(hours=1)


class _FailFirstApprovalCasStore(InMemoryQuestionRequestStore):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False
        self.approval_cas_calls = 0
        self.reported_updated_at: datetime | None = None

    def get(self, request_id: str) -> QuestionRequest | None:
        request = super().get(request_id)
        if request is None or self.reported_updated_at is None:
            return request
        return QuestionRequest.model_validate(
            {
                **request.model_dump(mode="python", round_trip=True),
                "updated_at": self.reported_updated_at,
            },
            strict=True,
        )

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        if expected_revision == 1 and isinstance(updated.state, AwaitingApproval):
            self.approval_cas_calls += 1
            if not self.failed:
                self.failed = True
                return False
        return super().compare_and_set(
            request_id,
            expected_revision,
            current,
            updated,
        )


class _Responsibility:
    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot:
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id="owner-1")


def test_gate_retry_uses_first_stored_assignment_time_and_due_at() -> None:
    requests = _FailFirstApprovalCasStore()
    approvals = InMemoryApprovalStore()
    policy = _RequiredPolicy()
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불해 주세요.",
        request_id_factory=lambda: "request-1",
        clock=lambda: T0,
        due_at=T3,
    )
    requests.create(received)
    trigger_key = "request-dispatch:request-1:1"
    ready = received.record_initial_routing(
        intent=ROUTE.intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=ROUTE,
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=T3,
            ),
        ),
        clock=lambda: T0,
    )
    assert requests.compare_and_set("request-1", 0, received, ready)

    first_boundary = ApprovalBoundary(
        requests=requests,
        approvals=approvals,
        policy=policy,
        authorizer=_AllowAuthorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-first",
        item_id_factory=lambda: "approval-first",
        clock=lambda: T0,
    )
    with pytest.raises(ApprovalConcurrencyError, match="CAS"):
        first_boundary.gate_candidate(
            "request-1",
            expected_revision=1,
            candidate=CANDIDATE,
        )

    first_item = approvals.get("approval-first")
    assert first_item is not None
    assert first_item.created_at == T0
    assert first_item.due_at == T0 + timedelta(hours=1)
    assert requests.get("request-1") == ready

    retry_boundary = ApprovalBoundary(
        requests=requests,
        approvals=approvals,
        policy=policy,
        authorizer=_AllowAuthorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-retry",
        item_id_factory=lambda: "approval-retry",
        clock=lambda: T1,
    )
    result = retry_boundary.gate_candidate(
        "request-1",
        expected_revision=1,
        candidate=CANDIDATE,
    )

    assert isinstance(result, ApprovalPending)
    assert result.request_id == "request-1"
    assert approvals.get("approval-first") == first_item
    assert approvals.get("approval-retry") is None
    assert len(approvals.history) == 1
    stored_request = requests.get("request-1")
    assert stored_request is not None
    assert stored_request.updated_at == first_item.created_at
    assert isinstance(stored_request.state, AwaitingApproval)
    assert stored_request.state.draft_ref == "approval-first"
    assert stored_request.state.handling == HandlingAssignment(
        kind="approval_item",
        ref="approval-first",
        due_at=T0 + timedelta(hours=1),
    )


def test_gate_retry_after_stored_due_repairs_request_from_first_assignment() -> None:
    requests = _FailFirstApprovalCasStore()
    approvals = InMemoryApprovalStore()
    policy = _RequiredPolicy()
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불해 주세요.",
        request_id_factory=lambda: "request-1",
        clock=lambda: T0,
        due_at=T3,
    )
    requests.create(received)
    trigger_key = "request-dispatch:request-1:1"
    ready = received.record_initial_routing(
        intent=ROUTE.intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=ROUTE,
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=T3,
            ),
        ),
        clock=lambda: T0,
    )
    assert requests.compare_and_set("request-1", 0, received, ready)
    first_boundary = ApprovalBoundary(
        requests=requests,
        approvals=approvals,
        policy=policy,
        authorizer=_AllowAuthorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-first",
        item_id_factory=lambda: "approval-first",
        clock=lambda: T0,
    )
    with pytest.raises(ApprovalConcurrencyError, match="CAS"):
        first_boundary.gate_candidate(
            "request-1",
            expected_revision=1,
            candidate=CANDIDATE,
        )
    first_item = approvals.get("approval-first")
    assert first_item is not None
    assert first_item.due_at == T0 + timedelta(hours=1)

    late_retry = ApprovalBoundary(
        requests=requests,
        approvals=approvals,
        policy=policy,
        authorizer=_AllowAuthorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-retry",
        item_id_factory=lambda: "approval-retry",
        clock=lambda: T0 + timedelta(hours=2),
    )

    result = late_retry.gate_candidate(
        "request-1",
        expected_revision=1,
        candidate=CANDIDATE,
    )

    assert isinstance(result, ApprovalPending)
    assert approvals.generations("request-1", 1) == [first_item]
    assert approvals.get("approval-retry") is None
    repaired = requests.get("request-1")
    assert repaired is not None
    assert repaired.revision == ready.revision + 1
    assert repaired.updated_at == first_item.created_at
    assert isinstance(repaired.state, AwaitingApproval)
    assert repaired.state.draft_ref == first_item.item_id
    assert repaired.state.handling.due_at == first_item.due_at


def test_gate_retry_rejects_assignment_older_than_current_request_without_cas() -> None:
    requests = _FailFirstApprovalCasStore()
    approvals = InMemoryApprovalStore()
    policy = _RequiredPolicy()
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불해 주세요.",
        request_id_factory=lambda: "request-1",
        clock=lambda: T0,
        due_at=T3,
    )
    requests.create(received)
    trigger_key = "request-dispatch:request-1:1"
    ready = received.record_initial_routing(
        intent=ROUTE.intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=ROUTE,
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=T3,
            ),
        ),
        clock=lambda: T0,
    )
    assert requests.compare_and_set("request-1", 0, received, ready)
    boundary = ApprovalBoundary(
        requests=requests,
        approvals=approvals,
        policy=policy,
        authorizer=_AllowAuthorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-first",
        item_id_factory=lambda: "approval-first",
        clock=lambda: T0,
    )
    with pytest.raises(ApprovalConcurrencyError, match="CAS"):
        boundary.gate_candidate(
            "request-1",
            expected_revision=1,
            candidate=CANDIDATE,
        )
    first_item = approvals.get("approval-first")
    assert first_item is not None
    before_history = approvals.history
    requests.reported_updated_at = T1
    retry_boundary = ApprovalBoundary(
        requests=requests,
        approvals=approvals,
        policy=policy,
        authorizer=_AllowAuthorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-retry",
        item_id_factory=lambda: "approval-retry",
        clock=lambda: T1,
    )

    with pytest.raises(ApprovalItemMismatchError, match="assignment"):
        retry_boundary.gate_candidate(
            "request-1",
            expected_revision=1,
            candidate=CANDIDATE,
        )

    assert requests.approval_cas_calls == 1
    assert approvals.get("approval-first") == first_item
    assert approvals.history == before_history
    requests.reported_updated_at = None
    assert requests.get("request-1") == ready


def test_gate_retry_rejects_stored_assignment_from_future_without_cas() -> None:
    requests = _FailFirstApprovalCasStore()
    approvals = InMemoryApprovalStore()
    policy = _RequiredPolicy()
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불해 주세요.",
        request_id_factory=lambda: "request-1",
        clock=lambda: T0,
        due_at=T3,
    )
    requests.create(received)
    trigger_key = "request-dispatch:request-1:1"
    ready = received.record_initial_routing(
        intent=ROUTE.intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=ROUTE,
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=T3,
            ),
        ),
        clock=lambda: T0,
    )
    assert requests.compare_and_set("request-1", 0, received, ready)
    first_boundary = ApprovalBoundary(
        requests=requests,
        approvals=approvals,
        policy=policy,
        authorizer=_AllowAuthorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-first",
        item_id_factory=lambda: "approval-first",
        clock=lambda: T1,
    )
    with pytest.raises(ApprovalConcurrencyError, match="CAS"):
        first_boundary.gate_candidate(
            "request-1",
            expected_revision=1,
            candidate=CANDIDATE,
        )
    first_item = approvals.get("approval-first")
    assert first_item is not None and first_item.created_at == T1
    before_history = approvals.history
    retry_boundary = ApprovalBoundary(
        requests=requests,
        approvals=approvals,
        policy=policy,
        authorizer=_AllowAuthorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-retry",
        item_id_factory=lambda: "approval-retry",
        clock=lambda: T0,
    )

    with pytest.raises(ApprovalItemMismatchError, match="assignment"):
        retry_boundary.gate_candidate(
            "request-1",
            expected_revision=1,
            candidate=CANDIDATE,
        )

    assert requests.approval_cas_calls == 1
    assert requests.get("request-1") == ready
    assert approvals.get("approval-first") == first_item
    assert approvals.get("approval-retry") is None
    assert approvals.history == before_history


def test_only_resolved_successor_can_finalize_after_reassignment() -> None:
    approvals = InMemoryApprovalStore()
    policy = _RequiredPolicy()
    completion = InMemoryQuestionCompletionUnitOfWork(
        policy=policy,
        approvals=approvals,
        responsibility_resolver=_Responsibility(),
        record_id_factory=lambda: "record-1",
        clock=lambda: T3,
    )
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불해 주세요.",
        request_id_factory=lambda: "request-1",
        clock=lambda: T0,
        due_at=T1,
    )
    completion.create(received)
    trigger_key = "request-dispatch:request-1:1"
    ready = received.record_initial_routing(
        intent=ROUTE.intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=ROUTE,
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=T1,
            ),
        ),
        clock=lambda: T0,
    )
    assert completion.compare_and_set("request-1", 0, received, ready)
    approval_now = [T0]
    boundary = ApprovalBoundary(
        requests=completion,
        approvals=approvals,
        policy=policy,
        authorizer=_AllowAuthorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-1",
        item_id_factory=lambda: "approval-1",
        clock=lambda: approval_now[0],
    )
    boundary.gate_candidate(
        "request-1",
        expected_revision=1,
        candidate=CANDIDATE,
    )
    predecessor = approvals.get("approval-1")
    assert predecessor is not None
    successor = _successor(predecessor=predecessor, due_at=T3)
    approvals.supersede_and_create_if_open(
        predecessor.item_id,
        _supersession(),
        successor,
    )
    awaiting = completion.get("request-1")
    assert awaiting is not None
    reassigned = awaiting.reassign_approval(
        previous_item_id=predecessor.item_id,
        successor_item_id=successor.item_id,
        due_at=successor.due_at,
        clock=lambda: T1,
    )
    assert completion.compare_and_set(
        awaiting.request_id,
        awaiting.revision,
        awaiting,
        reassigned,
    )

    forged_predecessor_handoff = ApprovedCandidate(
        request_id=predecessor.request_id,
        item_id=predecessor.item_id,
        expected_revision=predecessor.awaiting_revision,
        attempt=predecessor.attempt,
        route=predecessor.route,
        candidate=predecessor.draft.candidate,
        approved_by="alice",
        approved_at=T2,
        edited=False,
        policy_version=predecessor.requirement.policy_version,
        assignment_generation=ApprovalAssignmentGeneration.from_item(predecessor),
    )
    with pytest.raises(CompletionEvidenceError):
        completion.complete(forged_predecessor_handoff)
    with pytest.raises(ApprovalConcurrencyError):
        boundary.decide(
            predecessor.item_id,
            ApproverPrincipal(org_id="org-1", subject_id="alice"),
            Approve(by_approver="alice"),
        )

    approval_now[0] = T2
    approved = boundary.decide(
        successor.item_id,
        ApproverPrincipal(org_id="org-1", subject_id="bob"),
        Approve(by_approver="bob"),
    )
    assert isinstance(approved, ApprovedCandidate)
    finalized = completion.complete(approved)
    assert finalized.request_id == "request-1"
    assert finalized.record_id == "record-1"
    assert finalized.review_status == "approved"
