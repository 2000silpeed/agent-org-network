from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from agent_org_network.answer_finalization import (
    AnswerCompletion,
    AnswerResponsibilitySnapshot,
    CompletionConcurrencyError,
    CompletionEvidenceError,
    CompletionFaultPoint,
    CompletionIdCollisionError,
    InMemoryQuestionCompletionUnitOfWork,
    ReentrantCompletionMutationError,
)
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalAuthorization,
    ApprovalBoundary,
    ApprovalItem,
    ApprovalRequired,
    Approve,
    ApproveWithEdit,
    ApprovedCandidate,
    ApproverPrincipal,
    AnswerCandidate,
    FinalizationCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.conflict import InMemoryConflictCaseStore
from agent_org_network.decision import RoutingDecision
from agent_org_network.manager_queue import InMemoryManagerQueueStore
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingApproval,
    FailedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RequestStateKind,
    RouteTarget,
)
from agent_org_network.question_resolution import (
    AuthorityGrant,
    QuestionResolutionApplication,
    RequestAnswered,
    RequesterPrincipal,
)
from agent_org_network.runtime import AnswerMode


NOW = datetime(2026, 7, 12, 14, 0, tzinfo=timezone.utc)


class _Policy:
    def __init__(self, *, required: bool = False) -> None:
        self.required = required

    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: str,
    ) -> NoApprovalRequired | ApprovalRequired:
        if self.required or route.requires_approval or candidate_mode == "draft_only":
            return ApprovalRequired(
                approver_id="legal-approver",
                policy_version="approval-v1",
            )
        return NoApprovalRequired(policy_version="approval-v1")


class _Authorizer:
    def authorize(
        self,
        org_id: str,
        designated_approver_id: str,
        actor_id: str,
        action_kind: str,
        policy_version: str,
    ) -> ApprovalAuthorization | None:
        if org_id == "org-1" and actor_id == "alice":
            return ApprovalAuthorization(policy_version=policy_version)
        return None


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        return started_at + timedelta(hours=1)


class _ApprovalItemSubclass(ApprovalItem):
    pass


class _TamperableApprovalStore(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.tamper: str | None = None

    def get(self, item_id: str) -> ApprovalItem | None:
        item = super().get(item_id)
        if item is None:
            return None
        if self.tamper == "org":
            return item.model_copy(update={"org_id": "org-2"})
        if self.tamper == "due":
            return item.model_copy(update={"due_at": item.due_at + timedelta(minutes=5)})
        if self.tamper == "subclass":
            return _ApprovalItemSubclass.model_validate(
                item.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        if self.tamper == "generation":
            return ApprovalItem.model_validate(
                {
                    **item.model_dump(mode="python", round_trip=True),
                    "approval_round": 2,
                    "supersedes_item_id": "approval-bogus",
                },
                strict=True,
            )
        return item

    def get_by_request_attempt(
        self,
        request_id: str,
        attempt: int,
    ) -> ApprovalItem | None:
        item = super().get_by_request_attempt(request_id, attempt)
        if item is None:
            return None
        if self.tamper == "current_subclass":
            return self._subclass(item)
        if self.tamper == "current_key":
            return self._different_item_key(item)
        return item

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        item = super().get_by_request_attempt_round(
            request_id,
            attempt,
            approval_round,
        )
        if item is None:
            return None
        if self.tamper == "round_subclass":
            return self._subclass(item)
        if self.tamper == "round_key":
            return self._different_item_key(item)
        return item

    @staticmethod
    def _subclass(item: ApprovalItem) -> ApprovalItem:
        return _ApprovalItemSubclass.model_validate(
            item.model_dump(mode="python", round_trip=True),
            strict=True,
        )

    @staticmethod
    def _different_item_key(item: ApprovalItem) -> ApprovalItem:
        resolution = item.resolution
        assert resolution is not None
        candidate = resolution.approved_candidate
        assert candidate is not None
        forged_resolution = resolution.model_copy(
            update={
                "approved_candidate": candidate.model_copy(update={"item_id": "approval-other"})
            }
        )
        return ApprovalItem.model_validate(
            {
                **item.model_dump(mode="python", round_trip=True),
                "item_id": "approval-other",
                "resolution": forged_resolution,
            },
            strict=True,
        )


class _Resolver:
    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot | None:
        return AnswerResponsibilitySnapshot(
            agent_id=route.agent_id,
            owner_id="owner-user-1",
        )


def _components(
    *,
    required: bool = False,
    record_id_factory: Callable[[], str] | None = None,
    fault_injector: Callable[[CompletionFaultPoint], None] | None = None,
) -> tuple[
    InMemoryQuestionCompletionUnitOfWork,
    InMemoryApprovalStore,
    _Policy,
]:
    policy = _Policy(required=required)
    approvals = InMemoryApprovalStore()
    uow = InMemoryQuestionCompletionUnitOfWork(
        policy=policy,
        approvals=approvals,
        responsibility_resolver=_Resolver(),
        record_id_factory=record_id_factory or (lambda: "record-1"),
        clock=lambda: NOW,
        fault_injector=fault_injector,
    )
    return uow, approvals, policy


def _ready(
    store: InMemoryQuestionCompletionUnitOfWork,
    *,
    requires_approval: bool = False,
    session_id: str | None = None,
    request_id: str = "req-1",
) -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="환불은 언제 처리되나요?",
        request_id_factory=lambda: request_id,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id=session_id,
    )
    store.create(received)
    route = RouteTarget(
        intent="refund",
        agent_id="refund-card",
        requires_approval=requires_approval,
        authority_version="route-v1",
    )
    trigger = f"request-dispatch:{request_id}:1"
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
            attempt=1,
            trigger_key=trigger,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert store.compare_and_set(request_id, 0, received, ready)
    return ready


def _candidate(
    mode: AnswerMode = "full",
    text: str = "3일 안에 환불됩니다.",
) -> AnswerCandidate:
    return AnswerCandidate(
        text=text,
        sources=("refund-policy.md", "sla.md"),
        mode=mode,
        snapshot_sha="sha-1",
    )


def test_no_approval_completion_commits_all_artifacts_together() -> None:
    uow, _, _ = _components()
    ready = _ready(uow, session_id="session-1")
    assert isinstance(ready.state, ReadyToDispatch)
    handoff = FinalizationCandidate(
        request_id="req-1",
        expected_revision=1,
        attempt=1,
        route=ready.state.route,
        candidate=_candidate(),
        approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
    )

    completion = uow.complete(handoff)

    assert completion.request_id == "req-1"
    assert completion.record_id == "record-1"
    assert completion.text == "3일 안에 환불됩니다."
    assert completion.answered_by == "owner-user-1"
    assert completion.agent_id == "refund-card"
    assert completion.mode == "full"
    assert completion.sources == ("refund-policy.md", "sla.md")
    assert completion.snapshot_sha == "sha-1"
    assert completion.review_status == "not_required"
    assert completion.completed_at == NOW

    stored = uow.get("req-1")
    assert stored is not None and isinstance(stored.state, AnsweredRequest)
    assert stored.state.record_id == "record-1"
    bundle = uow.by_request("req-1")
    assert bundle is not None
    assert uow.by_record("record-1") == bundle
    assert bundle.answer_record.request_id == "req-1"
    assert bundle.answer_record.sources == ("refund-policy.md", "sla.md")
    assert bundle.answer_record.snapshot_sha == "sha-1"
    assert bundle.terminal_audit.approval.kind == "not_required"
    assert bundle.terminal_audit.candidate_mode == "full"
    assert bundle.terminal_audit.final_mode == "full"
    assert bundle.session_turn is not None
    assert bundle.session_turn.request_id == "req-1"
    assert bundle.session_turn.answered_by == "refund-card"
    assert bundle.delivery.kind == "answer_ready"
    assert bundle.delivery.request_id == "req-1"
    assert bundle.delivery.record_id == "record-1"


def test_sessionless_completion_does_not_create_fake_turn() -> None:
    uow, _, _ = _components()
    ready = _ready(uow)
    assert isinstance(ready.state, ReadyToDispatch)

    uow.complete(
        FinalizationCandidate(
            request_id="req-1",
            expected_revision=1,
            attempt=1,
            route=ready.state.route,
            candidate=_candidate(),
            approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
        )
    )

    bundle = uow.by_request("req-1")
    assert bundle is not None and bundle.session_turn is None


def test_human_approved_draft_is_finalized_as_full_with_original_evidence() -> None:
    uow, approvals, policy = _components(required=True)
    _ready(uow, requires_approval=True)
    boundary = ApprovalBoundary(
        requests=uow,
        approvals=approvals,
        policy=policy,
        authorizer=_Authorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-1",
        item_id_factory=lambda: "approval-1",
        clock=lambda: NOW - timedelta(seconds=30),
    )
    pending = boundary.gate_candidate(
        "req-1",
        expected_revision=1,
        candidate=_candidate("draft_only"),
    )
    assert pending.request_id == "req-1"
    approved = boundary.decide(
        "approval-1",
        ApproverPrincipal(org_id="org-1", subject_id="alice"),
        ApproveWithEdit(by_approver="alice", edited_text="검토 후 3일 안에 환불됩니다."),
    )
    assert isinstance(approved, ApprovedCandidate)

    completion = uow.complete(approved)

    assert completion.mode == "full"
    assert completion.text == "검토 후 3일 안에 환불됩니다."
    assert completion.review_status == "approved"
    bundle = uow.by_request("req-1")
    assert bundle is not None
    assert bundle.answer_record.mode == "full"
    assert bundle.answer_record.sources == ("refund-policy.md", "sla.md")
    assert bundle.terminal_audit.candidate_mode == "draft_only"
    assert bundle.terminal_audit.final_mode == "full"
    assert bundle.terminal_audit.approval.kind == "approved"
    assert bundle.terminal_audit.approval.item_id == "approval-1"
    assert bundle.terminal_audit.approval.action == "approve_with_edit"
    assert bundle.terminal_audit.approval.approved_by == "alice"


@pytest.mark.parametrize(
    "tamper",
    [
        "org",
        "due",
        "subclass",
        "generation",
        "current_subclass",
        "current_key",
        "round_subclass",
        "round_key",
    ],
)
def test_human_approval_exact_link_failure_writes_no_completion_artifact(
    tamper: str,
) -> None:
    policy = _Policy(required=True)
    approvals = _TamperableApprovalStore()
    uow = InMemoryQuestionCompletionUnitOfWork(
        policy=policy,
        approvals=approvals,
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
    )
    _ready(uow, requires_approval=True)
    boundary = ApprovalBoundary(
        requests=uow,
        approvals=approvals,
        policy=policy,
        authorizer=_Authorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-1",
        item_id_factory=lambda: "approval-1",
        clock=lambda: NOW - timedelta(seconds=30),
    )
    boundary.gate_candidate(
        "req-1",
        expected_revision=1,
        candidate=_candidate("draft_only"),
    )
    approved = boundary.decide(
        "approval-1",
        ApproverPrincipal(org_id="org-1", subject_id="alice"),
        Approve(by_approver="alice"),
    )
    assert isinstance(approved, ApprovedCandidate)
    awaiting = uow.get("req-1")
    assert awaiting is not None and isinstance(awaiting.state, AwaitingApproval)
    approvals.tamper = tamper

    with pytest.raises(CompletionEvidenceError):
        uow.complete(approved)

    assert uow.get("req-1") == awaiting
    assert uow.by_request("req-1") is None
    assert uow.by_record("record-1") is None
    assert uow.answer_records_for_agent("refund-card") == []


def test_same_handoff_32_way_commits_one_bundle_and_returns_one_result() -> None:
    id_calls: list[str] = []

    def record_id() -> str:
        id_calls.append("called")
        return "record-1"

    uow, _, _ = _components(record_id_factory=record_id)
    ready = _ready(uow)
    assert isinstance(ready.state, ReadyToDispatch)
    handoff = FinalizationCandidate(
        request_id="req-1",
        expected_revision=1,
        attempt=1,
        route=ready.state.route,
        candidate=_candidate(),
        approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
    )

    def complete(_: int) -> AnswerCompletion:
        return uow.complete(handoff)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(complete, range(32)))

    assert all(result == results[0] for result in results)
    assert id_calls == ["called"]
    bundle = uow.by_request("req-1")
    assert bundle is not None
    assert bundle.request.revision == 2
    assert uow.by_record("record-1") == bundle


def test_different_candidate_32_way_has_one_winner_and_explicit_losers() -> None:
    uow, _, _ = _components()
    ready = _ready(uow)
    assert isinstance(ready.state, ReadyToDispatch)
    handoffs = [
        FinalizationCandidate(
            request_id="req-1",
            expected_revision=1,
            attempt=1,
            route=ready.state.route,
            candidate=_candidate(text=f"후보 답 {index}"),
            approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
        )
        for index in range(32)
    ]

    def complete(handoff: FinalizationCandidate) -> object:
        try:
            return uow.complete(handoff)
        except CompletionConcurrencyError as error:
            return error

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(complete, handoffs))

    winners = [result for result in results if isinstance(result, AnswerCompletion)]
    losers = [result for result in results if isinstance(result, CompletionConcurrencyError)]
    assert len(winners) == 1
    assert len(losers) == 31
    bundle = uow.by_request("req-1")
    assert bundle is not None
    assert bundle.answer_record.answer_text == winners[0].text


def test_same_text_with_different_sources_is_not_treated_as_same_handoff() -> None:
    uow, _, _ = _components()
    ready = _ready(uow)
    assert isinstance(ready.state, ReadyToDispatch)
    first = FinalizationCandidate(
        request_id="req-1",
        expected_revision=1,
        attempt=1,
        route=ready.state.route,
        candidate=_candidate(),
        approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
    )
    uow.complete(first)
    different_evidence = first.model_copy(
        update={"candidate": first.candidate.model_copy(update={"sources": ("other-policy.md",)})}
    )

    with pytest.raises(CompletionConcurrencyError):
        uow.complete(different_evidence)


def test_record_id_collision_with_other_request_rolls_back_second_request() -> None:
    uow, _, _ = _components()
    first = _ready(uow, request_id="req-1")
    assert isinstance(first.state, ReadyToDispatch)
    uow.complete(
        FinalizationCandidate(
            request_id="req-1",
            expected_revision=1,
            attempt=1,
            route=first.state.route,
            candidate=_candidate(),
            approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
        )
    )
    second = _ready(uow, request_id="req-2")
    assert isinstance(second.state, ReadyToDispatch)

    with pytest.raises(CompletionIdCollisionError):
        uow.complete(
            FinalizationCandidate(
                request_id="req-2",
                expected_revision=1,
                attempt=1,
                route=second.state.route,
                candidate=_candidate(text="두 번째 답"),
                approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
            )
        )

    assert uow.get("req-2") == second
    assert uow.by_request("req-2") is None


class _FailOnce:
    def __init__(self, target: CompletionFaultPoint) -> None:
        self.target = target
        self.failed = False

    def __call__(self, point: CompletionFaultPoint) -> None:
        if point == self.target and not self.failed:
            self.failed = True
            raise RuntimeError(f"injected:{point}")


class _ReentrantFailure:
    def __init__(self) -> None:
        self.uow: InMemoryQuestionCompletionUnitOfWork | None = None
        self.enabled = True

    def __call__(self, point: CompletionFaultPoint) -> None:
        if point != "after_request" or not self.enabled:
            return
        assert self.uow is not None
        current = self.uow.get("req-1")
        assert current is not None
        failed = current.transition(
            FailedRequest(error_code="reentrant_failure"),
            clock=lambda: NOW,
        )
        self.uow.compare_and_set(
            current.request_id,
            current.revision,
            current,
            failed,
        )


class _InPlaceMutationFailure:
    def __init__(self) -> None:
        self.uow: InMemoryQuestionCompletionUnitOfWork | None = None

    def __call__(self, point: CompletionFaultPoint) -> None:
        if point != "after_request":
            return
        assert self.uow is not None
        exposed = self.uow.get("req-1")
        assert exposed is not None
        object.__setattr__(
            exposed,
            "state",
            FailedRequest(error_code="forged_in_place"),
        )
        raise RuntimeError("injected:in_place_mutation")


def test_completion_callback_cannot_reenter_and_overwrite_request_state() -> None:
    callback = _ReentrantFailure()
    uow, _, _ = _components(fault_injector=callback)
    callback.uow = uow
    ready = _ready(uow)
    assert isinstance(ready.state, ReadyToDispatch)
    handoff = FinalizationCandidate(
        request_id="req-1",
        expected_revision=1,
        attempt=1,
        route=ready.state.route,
        candidate=_candidate(),
        approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
    )

    with pytest.raises(ReentrantCompletionMutationError):
        uow.complete(handoff)

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None
    callback.enabled = False
    assert uow.complete(handoff).record_id == "record-1"


def test_public_request_read_is_not_a_mutable_alias_of_backing_state() -> None:
    callback = _InPlaceMutationFailure()
    uow, _, _ = _components(fault_injector=callback)
    callback.uow = uow
    ready = _ready(uow)
    assert isinstance(ready.state, ReadyToDispatch)
    handoff = FinalizationCandidate(
        request_id="req-1",
        expected_revision=1,
        attempt=1,
        route=ready.state.route,
        candidate=_candidate(),
        approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
    )

    with pytest.raises(RuntimeError, match="in_place_mutation"):
        uow.complete(handoff)

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None


def test_returned_completion_and_bundle_are_not_backing_state_aliases() -> None:
    uow, _, _ = _components()
    ready = _ready(uow)
    assert isinstance(ready.state, ReadyToDispatch)
    completion = uow.complete(
        FinalizationCandidate(
            request_id="req-1",
            expected_revision=1,
            attempt=1,
            route=ready.state.route,
            candidate=_candidate(),
            approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
        )
    )
    object.__setattr__(completion, "text", "변조된 반환값")
    first_read = uow.by_request("req-1")
    assert first_read is not None
    object.__setattr__(first_read.answer_record, "answer_text", "변조된 record")
    object.__setattr__(
        first_read.request,
        "state",
        FailedRequest(error_code="forged_reader_state"),
    )

    second_read = uow.by_request("req-1")
    assert second_read is not None
    assert second_read.completion.text == "3일 안에 환불됩니다."
    assert second_read.answer_record.answer_text == "3일 안에 환불됩니다."
    assert isinstance(second_read.request.state, AnsweredRequest)


@pytest.mark.parametrize(
    "point",
    [
        "after_answer_record",
        "after_request",
        "after_audit",
        "after_session",
        "after_outbox",
        "before_commit",
    ],
)
def test_every_staging_fault_rolls_back_all_artifacts_and_retry_succeeds(
    point: CompletionFaultPoint,
) -> None:
    failure = _FailOnce(point)
    uow, _, _ = _components(fault_injector=failure)
    ready = _ready(uow, session_id="session-1")
    assert isinstance(ready.state, ReadyToDispatch)
    handoff = FinalizationCandidate(
        request_id="req-1",
        expected_revision=1,
        attempt=1,
        route=ready.state.route,
        candidate=_candidate(),
        approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
    )

    with pytest.raises(RuntimeError, match=f"injected:{point}"):
        uow.complete(handoff)

    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None
    assert uow.by_record("record-1") is None

    completion = uow.complete(handoff)
    assert completion.record_id == "record-1"
    stored = uow.get("req-1")
    assert stored is not None and isinstance(stored.state, AnsweredRequest)


class _NeverRouter:
    def route(self, question: str) -> RoutingDecision:
        raise AssertionError("retrieve는 Router를 호출하면 안 됩니다.")


class _NeverAuthority:
    def authorize(
        self,
        org_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None:
        raise AssertionError("retrieve는 RouteAuthority를 호출하면 안 됩니다.")


class _QuestionDeadline:
    def deadline_for(
        self,
        org_id: str,
        state_kind: RequestStateKind,
        started_at: datetime,
    ) -> datetime:
        return started_at + timedelta(hours=1)


def test_async_awaiting_answer_and_application_retrieve_share_terminal_record() -> None:
    uow, _, _ = _components()
    ready = _ready(uow)
    assert isinstance(ready.state, ReadyToDispatch)
    awaiting = ready.transition(
        AwaitingAnswer(
            route=ready.state.route,
            attempt=1,
            ticket_id="ticket-1",
            handling=HandlingAssignment(
                kind="runtime_ticket",
                ref="ticket-1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW - timedelta(seconds=30),
    )
    assert uow.compare_and_set("req-1", 1, ready, awaiting)
    completion = uow.complete(
        FinalizationCandidate(
            request_id="req-1",
            expected_revision=2,
            attempt=1,
            route=ready.state.route,
            candidate=_candidate(),
            approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
        )
    )
    app = QuestionResolutionApplication(
        requests=uow,
        router=_NeverRouter(),
        conflicts=InMemoryConflictCaseStore(),
        managers=InMemoryManagerQueueStore(),
        route_authority=_NeverAuthority(),
        deadline_policy=_QuestionDeadline(),
        request_id_factory=lambda: "unused",
        clock=lambda: NOW,
    )

    result = app.retrieve(
        "req-1",
        RequesterPrincipal(org_id="org-1", subject_id="user-1"),
    )

    assert result == RequestAnswered(
        request_id="req-1",
        record_id=completion.record_id,
    )


def test_resolved_approval_survives_finalization_fault_and_same_handoff_retries() -> None:
    failure = _FailOnce("after_audit")
    policy = _Policy(required=True)
    approvals = InMemoryApprovalStore()
    uow = InMemoryQuestionCompletionUnitOfWork(
        policy=policy,
        approvals=approvals,
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
        fault_injector=failure,
    )
    _ready(uow, requires_approval=True)
    boundary = ApprovalBoundary(
        requests=uow,
        approvals=approvals,
        policy=policy,
        authorizer=_Authorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-1",
        item_id_factory=lambda: "approval-1",
        clock=lambda: NOW - timedelta(seconds=30),
    )
    boundary.gate_candidate(
        "req-1",
        expected_revision=1,
        candidate=_candidate("draft_only"),
    )
    approved = boundary.decide(
        "approval-1",
        ApproverPrincipal(org_id="org-1", subject_id="alice"),
        ApproveWithEdit(by_approver="alice", edited_text="승인된 답"),
    )
    assert isinstance(approved, ApprovedCandidate)

    with pytest.raises(RuntimeError, match="injected:after_audit"):
        uow.complete(approved)

    request = uow.get("req-1")
    assert request is not None and request.state.kind == "awaiting_approval"
    item = approvals.get("approval-1")
    assert item is not None and item.status == "resolved"
    assert uow.by_request("req-1") is None

    assert uow.complete(approved).text == "승인된 답"


def test_public_approval_read_cannot_forge_authorization_for_finalization() -> None:
    policy = _Policy(required=True)
    approvals = InMemoryApprovalStore()
    uow = InMemoryQuestionCompletionUnitOfWork(
        policy=policy,
        approvals=approvals,
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
    )
    _ready(uow, requires_approval=True)
    boundary = ApprovalBoundary(
        requests=uow,
        approvals=approvals,
        policy=policy,
        authorizer=_Authorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-1",
        item_id_factory=lambda: "approval-1",
        clock=lambda: NOW - timedelta(seconds=30),
    )
    boundary.gate_candidate(
        "req-1",
        expected_revision=1,
        candidate=_candidate("draft_only"),
    )
    exposed = approvals.get("approval-1")
    assert exposed is not None and exposed.status == "open"
    forged = ApprovedCandidate(
        request_id=exposed.request_id,
        item_id=exposed.item_id,
        expected_revision=exposed.awaiting_revision,
        attempt=exposed.attempt,
        route=exposed.route,
        candidate=exposed.draft.candidate,
        approved_by="mallory",
        approved_at=NOW - timedelta(seconds=15),
        edited=False,
        policy_version=exposed.requirement.policy_version,
        assignment_generation=ApprovalAssignmentGeneration.from_item(exposed),
    )
    forged_item = exposed.resolve(
        action=Approve(by_approver="mallory"),
        approved_candidate=forged,
        resolved_at=forged.approved_at,
    )
    object.__setattr__(exposed, "status", "resolved")
    object.__setattr__(exposed, "resolution", forged_item.resolution)

    with pytest.raises(CompletionEvidenceError):
        uow.complete(forged)

    stored = approvals.get("approval-1")
    request = uow.get("req-1")
    assert stored is not None and stored.status == "open"
    assert request is not None and request.state.kind == "awaiting_approval"
    assert uow.by_request("req-1") is None
