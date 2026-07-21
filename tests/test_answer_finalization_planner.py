from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

import agent_org_network.answer_finalization as answer_finalization_module
from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    CompletionArtifactCheckpoint,
    CompletionAttributionError,
    CompletionBundle,
    CompletionClockError,
    CompletionEvidenceError,
    CompletionFaultPoint,
    CompletionPlan,
    InMemoryQuestionCompletionUnitOfWork,
    InvalidCompletionHandoffError,
    NoApprovalEvidence,
    QuestionCompletionPlanner,
    canonical_completion_handoff,
)
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalDraft,
    ApprovalItem,
    ApprovalRequired,
    ApproveWithEdit,
    ApprovedCandidate,
    AnswerCandidate,
    FinalizationCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.question_request import (
    AwaitingApproval,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.session import SessionTurn


NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


class _Policy:
    def __init__(self, result: NoApprovalRequired | ApprovalRequired) -> None:
        self.result = result
        self.calls = 0

    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: str,
    ) -> NoApprovalRequired | ApprovalRequired:
        self.calls += 1
        return self.result


class _Resolver:
    def __init__(self, *, needs_correction_review: bool = False) -> None:
        self.calls = 0
        self.needs_correction_review = needs_correction_review

    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot | None:
        self.calls += 1
        return AnswerResponsibilitySnapshot(
            agent_id=route.agent_id,
            owner_id="owner-user-1",
            needs_correction_review=self.needs_correction_review,
        )


def _ready(
    *,
    session_id: str | None = "session-1",
    requires_approval: bool = False,
) -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불은 언제 처리되나요?",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id=session_id,
    )
    route = RouteTarget(
        intent="refund",
        agent_id="refund-card",
        requires_approval=requires_approval,
        authority_version="route-v1",
    )
    trigger = "request-dispatch:req-1:1"
    return received.record_initial_routing(
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


def _handoff(
    request: QuestionRequest,
    *,
    needs_correction_review: bool = False,
) -> FinalizationCandidate:
    assert isinstance(request.state, ReadyToDispatch)
    return FinalizationCandidate(
        request_id=request.request_id,
        expected_revision=request.revision,
        attempt=request.state.attempt,
        route=request.state.route,
        candidate=AnswerCandidate(
            text="영업일 기준 3일 안에 처리됩니다.",
            sources=("refund-policy.md", "sla.md"),
            mode="full",
            snapshot_sha="snapshot-1",
        ),
        approval_evaluation=NoApprovalRequired(
            policy_version="approval-v1",
            needs_correction_review=needs_correction_review,
        ),
    )


def _planner(
    *,
    approvals: InMemoryApprovalStore | None = None,
    policy: _Policy | None = None,
    resolver: _Resolver | None = None,
    clock: Callable[[], datetime] | None = None,
) -> QuestionCompletionPlanner:
    completion_clock = clock or (lambda: NOW)
    return QuestionCompletionPlanner(
        policy=policy or _Policy(NoApprovalRequired(policy_version="approval-v1")),
        approvals=approvals or InMemoryApprovalStore(),
        responsibility_resolver=resolver or _Resolver(),
        record_id_factory=lambda: "record-1",
        clock=completion_clock,
    )


@pytest.mark.parametrize(
    "raw",
    [
        FinalizationCandidate.model_construct(
            request_id=" ",
            expected_revision=1,
            attempt=1,
            route=RouteTarget(
                intent="refund",
                agent_id="refund-card",
                requires_approval=False,
                authority_version="route-v1",
            ),
            candidate=AnswerCandidate(text="답", mode="full"),
            approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
        ),
        ApprovedCandidate.model_construct(request_id="req-1"),
        {"request_id": "req-1"},
    ],
)
def test_canonical_completion_handoff_rejects_unvalidated_or_unsealed_input(
    raw: object,
) -> None:
    with pytest.raises(InvalidCompletionHandoffError):
        canonical_completion_handoff(raw)


def test_canonical_completion_handoff_breaks_alias_and_revalidates_forced_mutation() -> None:
    ready = _ready()
    handoff = _handoff(ready)

    canonical = canonical_completion_handoff(handoff)

    assert canonical == handoff
    assert canonical is not handoff
    assert canonical.candidate is not handoff.candidate
    object.__setattr__(handoff.candidate, "sources", (" ",))
    with pytest.raises(InvalidCompletionHandoffError):
        canonical_completion_handoff(handoff)
    assert canonical.candidate.sources == ("refund-policy.md", "sla.md")


def test_planner_builds_canonical_no_approval_bundle_with_all_exact_artifacts() -> None:
    ready = _ready(session_id="session-1")
    handoff = _handoff(ready)
    policy = _Policy(NoApprovalRequired(policy_version="approval-v1"))
    resolver = _Resolver()
    planner = _planner(policy=policy, resolver=resolver)

    plan = planner.plan(ready, handoff)

    assert isinstance(plan, CompletionPlan)
    assert plan.handoff == handoff and plan.handoff is not handoff
    assert plan.expected_request == ready and plan.expected_request is not ready
    assert plan.bundle.completion.text == handoff.candidate.text
    assert plan.bundle.completion.sources == ("refund-policy.md", "sla.md")
    assert plan.bundle.completion.snapshot_sha == "snapshot-1"
    assert plan.bundle.completion.answered_by == "owner-user-1"
    assert plan.bundle.completion.agent_id == "refund-card"
    assert plan.bundle.completion.completed_at == NOW
    assert plan.bundle.terminal_audit.approval.kind == "not_required"
    assert plan.bundle.session_turn is not None
    assert plan.bundle.session_turn.request_id == "req-1"
    assert plan.bundle.delivery.record_id == "record-1"
    assert policy.calls == 1
    assert resolver.calls == 1
    object.__setattr__(handoff.candidate, "text", "호출자 변조")
    object.__setattr__(ready, "question", "호출자 변조")
    assert plan.handoff.candidate.text == "영업일 기준 3일 안에 처리됩니다."
    assert plan.expected_request.question == "환불은 언제 처리되나요?"
    assert plan.bundle.answer_record.question == "환불은 언제 처리되나요?"
    with pytest.raises(FrozenInstanceError):
        plan.expected_request = ready  # type: ignore[misc]


def test_planner가_사후교정_책임증거를_record와_audit에_exact_link한다() -> None:
    ready = _ready(session_id=None)
    plan = _planner(
        policy=_Policy(
            NoApprovalRequired(
                policy_version="approval-v1",
                needs_correction_review=True,
            )
        ),
    ).plan(
        ready,
        _handoff(ready, needs_correction_review=True),
    )

    assert plan.bundle.answer_record.needs_correction_review is True
    assert plan.bundle.terminal_audit.responsibility.needs_correction_review is True
    with pytest.raises(ValueError, match="AnswerRecord와 AnswerCompletion payload"):
        CompletionBundle(
            completion=plan.bundle.completion,
            request=plan.bundle.request,
            answer_record=plan.bundle.answer_record.model_copy(
                update={"needs_correction_review": False}
            ),
            terminal_audit=plan.bundle.terminal_audit,
            session_turn=plan.bundle.session_turn,
            delivery=plan.bundle.delivery,
        )


def test_false_presence_evidence는_기존_JSON_shape를_유지한다() -> None:
    false_snapshot = AnswerResponsibilitySnapshot(
        agent_id="refund-card",
        owner_id="owner-user-1",
    )
    true_snapshot = false_snapshot.model_copy(update={"needs_correction_review": True})

    assert false_snapshot.model_dump(mode="json") == {
        "agent_id": "refund-card",
        "owner_id": "owner-user-1",
    }
    assert true_snapshot.model_dump(mode="json")["needs_correction_review"] is True
    assert NoApprovalRequired(policy_version="approval-v1").model_dump(mode="json") == {
        "kind": "not_required",
        "policy_version": "approval-v1",
    }
    assert NoApprovalEvidence(policy_version="approval-v1").model_dump(mode="json") == {
        "kind": "not_required",
        "policy_version": "approval-v1",
    }


def test_책임_resolver가_review_flag를_자체판정하면_fail_closed한다() -> None:
    ready = _ready(session_id=None)

    with pytest.raises(CompletionAttributionError, match="자체 판정"):
        _planner(resolver=_Resolver(needs_correction_review=True)).plan(
            ready,
            _handoff(ready),
        )


def test_planner_uses_exact_resolved_edit_evidence_and_does_not_make_fake_session_turn() -> None:
    ready = _ready(session_id=None, requires_approval=True)
    assert isinstance(ready.state, ReadyToDispatch)
    route = ready.state.route
    item_id = "approval-1"
    awaiting = ready.transition(
        AwaitingApproval(
            route=route,
            attempt=1,
            draft_ref=item_id,
            handling=HandlingAssignment(
                kind="approval_item",
                ref=item_id,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW - timedelta(seconds=40),
    )
    original = AnswerCandidate(
        text="초안",
        sources=("refund-policy.md",),
        mode="draft_only",
        snapshot_sha="snapshot-2",
    )
    action = ApproveWithEdit(by_approver="alice", edited_text="법무 검토 완료 답")
    draft = ApprovalDraft(
        draft_id="draft-1",
        request_id="req-1",
        attempt=1,
        route=route,
        candidate=original,
        created_at=NOW - timedelta(seconds=30),
    )
    open_item = ApprovalItem(
        item_id=item_id,
        org_id=ready.org_id,
        request_id="req-1",
        awaiting_revision=awaiting.revision,
        attempt=1,
        route=route,
        draft=draft,
        requirement=ApprovalRequired(
            approver_id="legal",
            policy_version="approval-v2",
        ),
        created_at=draft.created_at,
        due_at=NOW + timedelta(hours=1),
    )
    approved = ApprovedCandidate(
        request_id="req-1",
        item_id=item_id,
        expected_revision=awaiting.revision,
        attempt=1,
        route=route,
        candidate=original.model_copy(update={"text": "법무 검토 완료 답"}),
        approved_by="alice",
        approved_at=NOW - timedelta(seconds=10),
        edited=True,
        policy_version="approval-v2",
        assignment_generation=ApprovalAssignmentGeneration.from_item(open_item),
    )
    approvals = InMemoryApprovalStore()
    approvals.create_or_get(open_item)
    approvals.resolve_if_open(
        item_id,
        action,
        lambda item: item.resolve(
            action=action,
            approved_candidate=approved,
            resolved_at=approved.approved_at,
        ),
    )

    plan = _planner(approvals=approvals).plan(awaiting, approved)

    assert plan.bundle.completion.text == "법무 검토 완료 답"
    assert plan.bundle.completion.mode == "full"
    assert plan.bundle.completion.sources == original.sources
    assert plan.bundle.completion.snapshot_sha == original.snapshot_sha
    assert plan.bundle.completion.review_status == "approved"
    assert plan.bundle.terminal_audit.candidate_mode == "draft_only"
    assert plan.bundle.terminal_audit.approval.kind == "approved"
    assert plan.bundle.terminal_audit.approval.action == "approve_with_edit"
    assert plan.bundle.session_turn is None


def test_planner_revalidates_request_snapshot_and_clock_before_building_plan() -> None:
    ready = _ready()
    handoff = _handoff(ready)
    object.__setattr__(ready, "requester_id", " ")

    with pytest.raises(CompletionEvidenceError):
        _planner().plan(ready, handoff)

    clean = _ready()
    with pytest.raises(CompletionClockError):
        _planner(clock=lambda: NOW - timedelta(minutes=2)).plan(
            clean,
            _handoff(clean),
        )


def test_in_memory_completion_delegates_new_completion_to_common_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    original = QuestionCompletionPlanner.plan

    def tracked(
        self: QuestionCompletionPlanner,
        request: QuestionRequest,
        handoff: FinalizationCandidate | ApprovedCandidate,
        *,
        checkpoint: CompletionArtifactCheckpoint | None = None,
    ) -> CompletionPlan:
        calls.append((request.request_id, handoff.request_id))
        return original(self, request, handoff, checkpoint=checkpoint)

    monkeypatch.setattr(QuestionCompletionPlanner, "plan", tracked)
    policy = _Policy(NoApprovalRequired(policy_version="approval-v1"))
    uow = InMemoryQuestionCompletionUnitOfWork(
        policy=policy,
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
    )
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불은 언제 처리되나요?",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id="session-1",
    )
    uow.create(received)
    ready = _ready()
    assert uow.compare_and_set("req-1", 0, received, ready)
    handoff = _handoff(ready)

    first = uow.complete(handoff)
    second = uow.complete(handoff)

    assert first == second
    assert calls == [("req-1", "req-1")]
    assert policy.calls == 1


def test_artifact_checkpoints_keep_pre_refactor_order_when_session_turn_build_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoints: list[CompletionFaultPoint] = []
    uow = InMemoryQuestionCompletionUnitOfWork(
        policy=_Policy(NoApprovalRequired(policy_version="approval-v1")),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
        fault_injector=checkpoints.append,
    )
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불은 언제 처리되나요?",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id="session-1",
    )
    ready = _ready(session_id="session-1")
    uow.create(received)
    assert uow.compare_and_set("req-1", 0, received, ready)

    def fail_session_turn(**_: object) -> SessionTurn:
        raise RuntimeError("session turn failure")

    monkeypatch.setattr(SessionTurn, "for_request", fail_session_turn)

    with pytest.raises(RuntimeError, match="session turn failure"):
        uow.complete(_handoff(ready))

    assert checkpoints == [
        "after_answer_record",
        "after_request",
        "after_audit",
    ]
    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None


def test_bundle_canonical_failure_occurs_after_sessionless_outbox_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoints: list[CompletionFaultPoint] = []
    uow = InMemoryQuestionCompletionUnitOfWork(
        policy=_Policy(NoApprovalRequired(policy_version="approval-v1")),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
        fault_injector=checkpoints.append,
    )
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불은 언제 처리되나요?",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id=None,
    )
    ready = _ready(session_id=None)
    uow.create(received)
    assert uow.compare_and_set("req-1", 0, received, ready)

    def fail_bundle(_: object) -> object:
        raise RuntimeError("bundle canonical failure")

    monkeypatch.setattr(
        answer_finalization_module,
        "canonical_completion_bundle",
        fail_bundle,
    )

    with pytest.raises(RuntimeError, match="bundle canonical failure"):
        uow.complete(_handoff(ready))

    assert checkpoints == [
        "after_answer_record",
        "after_request",
        "after_audit",
        "after_session",
        "after_outbox",
    ]
    assert uow.get("req-1") == ready
    assert uow.by_request("req-1") is None
