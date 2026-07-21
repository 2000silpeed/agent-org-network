from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from typing import cast
from unittest.mock import Mock

import pytest

from agent_org_network.approval_operations import (
    ApprovalOperationsApplication,
    ApprovalOperationsConflict,
    ApprovalOperationsDependency,
    ApprovalOperationsIntegrityError,
    ApproveIntent,
    RejectIntent,
)
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalAuthorization,
    ApprovalBoundary,
    ApprovalItem,
    ApprovalRequired,
    ApprovalStore,
    ApprovalSupersession,
    ApprovalUnavailable,
    ApprovalUnavailabilityEvidence,
    Approve,
    ApproverPrincipal,
)
from agent_org_network.approval_evidence import (
    ApprovalEvent,
    ApprovalEventRecorder,
    InMemoryApprovalEventJournal,
)
from agent_org_network.approval_retention import (
    ApprovalDraftRetentionDecision,
    ApprovalDraftTerminalEvidence,
)
from agent_org_network.question_request import FailedRequest, QuestionRequestStore
from test_approval_operations_decision import _Harness  # pyright: ignore[reportPrivateUsage]


T2 = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)


class _RetentionPolicy:
    def __init__(self, *, eligible: bool = True) -> None:
        self.eligible = eligible
        self.calls = 0
        self.terminals: list[ApprovalDraftTerminalEvidence] = []

    def evaluate(
        self,
        *,
        terminal: ApprovalDraftTerminalEvidence,
        evaluated_at: datetime,
    ) -> ApprovalDraftRetentionDecision:
        self.calls += 1
        self.terminals.append(terminal)
        return ApprovalDraftRetentionDecision(
            terminal=terminal,
            evaluated_at=evaluated_at,
            policy_version="retention-v1",
            retain_until=terminal.terminal_at + timedelta(days=1),
            purge_eligible=self.eligible,
        )


def _retention_app(
    harness: _Harness,
    policy: object,
    journal: object | None = None,
) -> ApprovalOperationsApplication:
    recorder = ApprovalEventRecorder(
        cast(InMemoryApprovalEventJournal, journal or InMemoryApprovalEventJournal())
    )
    return ApprovalOperationsApplication(
        requests=harness.uow,
        approvals=harness.approvals,
        reader=harness.uow,
        evidence_recorder=recorder,
        retention_policy=cast(_RetentionPolicy, policy),
    )


def test_retention_policy_configuration_fails_closed_without_exact_reader_and_recorder() -> None:
    with pytest.raises(ApprovalOperationsDependency):
        ApprovalOperationsApplication(
            requests=Mock(),
            approvals=Mock(),
            retention_policy=Mock(),
        )


def test_active_assignment_is_retained_without_calling_policy() -> None:
    harness = _Harness()
    policy = _RetentionPolicy()
    app = _retention_app(harness, policy)

    status = app.retention_status("approval-1", T2)

    assert status.kind == "retained"
    assert status.reason == "active_assignment"
    assert policy.calls == 0


def test_approved_resolution_waiting_for_finalization_is_retained_without_policy() -> None:
    harness = _Harness()
    harness.boundary.decide(
        "approval-1",
        harness.principal,
        Approve(by_approver="alice"),
    )
    policy = _RetentionPolicy()
    app = _retention_app(harness, policy)

    status = app.retention_status("approval-1", T2)

    assert status.kind == "retained"
    assert status.reason == "finalization_pending"
    assert policy.calls == 0


def test_unavailable_pending_then_exact_terminal_controls_policy_boundary() -> None:
    harness = _Harness()
    item = harness.approvals.get("approval-1")
    assert item is not None
    unavailable_at = item.due_at + timedelta(minutes=1)
    evidence = ApprovalUnavailabilityEvidence(
        decision=ApprovalUnavailable(
            assignment_generation=ApprovalAssignmentGeneration.from_item(item),
            policy_version="expiry-v1",
            authority_version="directory-v1",
            evidence_ref="no-fallback-v1",
        ),
        unavailable_at=unavailable_at,
    )
    harness.approvals.close_unavailable_if_open(
        item.item_id,
        ApprovalAssignmentGeneration.from_item(item),
        evidence,
    )
    policy = _RetentionPolicy()
    app = _retention_app(harness, policy)

    pending = app.retention_status("approval-1", T2)
    assert pending.kind == "retained"
    assert pending.reason == "terminalization_pending"
    assert policy.calls == 0

    request = harness.uow.get("request-1")
    assert request is not None
    failed = request.transition(
        FailedRequest(error_code="approval_unavailable"),
        clock=lambda: unavailable_at,
    )
    assert harness.uow.compare_and_set(
        request.request_id,
        request.revision,
        request,
        failed,
    )

    terminal = app.retention_status("approval-1", T2)
    assert terminal.kind == "evaluated"
    assert policy.calls == 1
    observed = policy.terminals[0]
    assert observed.kind == "unavailable"
    assert observed.current_item_id == "approval-1"
    assert observed.request_revision == failed.revision
    assert observed.terminal_at == unavailable_at


def test_predecessor_and_current_queries_converge_on_full_shared_draft_lineage() -> None:
    harness = _Harness()
    predecessor = harness.approvals.get("approval-1")
    assert predecessor is not None
    created_at = predecessor.created_at + timedelta(minutes=5)
    successor = ApprovalItem(
        item_id="approval-2",
        org_id=predecessor.org_id,
        request_id=predecessor.request_id,
        awaiting_revision=predecessor.awaiting_revision + 1,
        attempt=predecessor.attempt,
        route=predecessor.route,
        draft=predecessor.draft,
        requirement=ApprovalRequired(
            approver_id="bob",
            policy_version="approval-v2",
        ),
        created_at=created_at,
        due_at=created_at + timedelta(hours=1),
        approval_round=2,
        supersedes_item_id=predecessor.item_id,
    )
    harness.approvals.supersede_and_create_if_open(
        predecessor.item_id,
        ApprovalSupersession(
            reason="reassigned",
            successor_item_id=successor.item_id,
            superseded_at=created_at,
        ),
        successor,
        expected_generation=ApprovalAssignmentGeneration.from_item(predecessor),
    )
    request = harness.uow.get("request-1")
    assert request is not None
    reassigned = request.reassign_approval(
        previous_item_id=predecessor.item_id,
        successor_item_id=successor.item_id,
        due_at=successor.due_at,
        clock=lambda: created_at,
    )
    assert harness.uow.compare_and_set(
        request.request_id,
        request.revision,
        request,
        reassigned,
    )
    policy = _RetentionPolicy()
    app = _retention_app(harness, policy)

    from_predecessor = app.retention_status(predecessor.item_id, T2)
    from_current = app.retention_status(successor.item_id, T2)

    assert from_predecessor == from_current
    assert from_current.kind == "retained"
    assert from_current.reason == "active_assignment"
    assert policy.calls == 0


@pytest.mark.parametrize(
    "intent",
    [ApproveIntent(), RejectIntent(reason_code="unsupported")],
)
def test_exact_terminal_is_evaluated_once_and_eligible_event_is_bodyless(
    intent: ApproveIntent | RejectIntent,
) -> None:
    harness = _Harness()
    harness.app.decide("approval-1", harness.principal, intent)
    policy = _RetentionPolicy()
    journal = InMemoryApprovalEventJournal()
    app = _retention_app(harness, policy, journal)

    first = app.retention_status("approval-1", T2)
    second = app.retention_status("approval-1", T2)

    assert first == second
    assert first.kind == "evaluated"
    assert first.purge_eligible is True
    assert policy.calls == 1
    events = journal.for_request("org-1", "request-1")
    assert len(events) == 1 and events[0].kind == "retention_eligible"
    payload = events[0].model_dump(mode="json")
    serialized = repr(payload)
    assert "환불해 주세요" not in serialized
    assert "환불할 수 있습니다" not in serialized
    assert "unsupported" not in serialized
    assert not ({"question", "candidate", "edited_text", "reason_code", "source"} & payload.keys())


def test_false_decision_is_cached_and_does_not_emit_eligible_event() -> None:
    harness = _Harness()
    harness.app.decide("approval-1", harness.principal, ApproveIntent())
    policy = _RetentionPolicy(eligible=False)
    journal = InMemoryApprovalEventJournal()
    app = _retention_app(harness, policy, journal)

    assert app.retention_status("approval-1", T2).purge_eligible is False
    assert app.retention_status("approval-1", T2).purge_eligible is False

    assert policy.calls == 1
    assert journal.for_request("org-1", "request-1") == ()


def test_same_exact_evaluation_is_single_flight_under_32_callers() -> None:
    harness = _Harness()
    harness.app.decide("approval-1", harness.principal, ApproveIntent())
    policy = _RetentionPolicy()
    journal = InMemoryApprovalEventJournal()
    app = _retention_app(harness, policy, journal)
    barrier = Barrier(32)

    def evaluate() -> object:
        barrier.wait()
        return app.retention_status("approval-1", T2)

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(evaluate) for _ in range(32)]
        results = [future.result() for future in futures]

    assert all(result == results[0] for result in results)
    assert policy.calls == 1
    assert len(journal.for_request("org-1", "request-1")) == 1


def test_synchronous_same_key_policy_reentry_is_conflict() -> None:
    harness = _Harness()
    harness.app.decide("approval-1", harness.principal, ApproveIntent())
    journal = InMemoryApprovalEventJournal()
    app: ApprovalOperationsApplication

    class ReenteringPolicy(_RetentionPolicy):
        def evaluate(
            self,
            *,
            terminal: ApprovalDraftTerminalEvidence,
            evaluated_at: datetime,
        ) -> ApprovalDraftRetentionDecision:
            app.retention_status("approval-1", evaluated_at)
            raise AssertionError("unreachable")

    policy = ReenteringPolicy()
    app = _retention_app(harness, policy, journal)

    with pytest.raises(ApprovalOperationsConflict) as caught:
        app.retention_status("approval-1", T2)

    assert caught.value.args == ()


class _FailOnceJournal:
    def __init__(self) -> None:
        self.target = InMemoryApprovalEventJournal()
        self.failed = False

    def append_batch_once(self, events: tuple[ApprovalEvent, ...]) -> tuple[ApprovalEvent, ...]:
        if not self.failed:
            self.failed = True
            raise RuntimeError("journal unavailable")
        return self.target.append_batch_once(events)

    def append_once(self, event: ApprovalEvent) -> ApprovalEvent:
        return self.append_batch_once((event,))[0]

    def get(self, event_id: str) -> ApprovalEvent | None:
        return self.target.get(event_id)

    def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]:
        return self.target.for_request(org_id, request_id)


def test_eligible_fast_path_repairs_event_after_journal_failure_without_policy_recall() -> None:
    harness = _Harness()
    harness.app.decide("approval-1", harness.principal, ApproveIntent())
    policy = _RetentionPolicy()
    journal = _FailOnceJournal()
    app = _retention_app(harness, policy, journal)

    with pytest.raises(ApprovalOperationsDependency):
        app.retention_status("approval-1", T2)
    status = app.retention_status("approval-1", T2)

    assert status.purge_eligible is True
    assert policy.calls == 1
    assert len(journal.for_request("org-1", "request-1")) == 1


def test_policy_result_must_bind_exact_terminal_and_evaluation() -> None:
    harness = _Harness()
    harness.app.decide("approval-1", harness.principal, ApproveIntent())

    class WrongPolicy(_RetentionPolicy):
        def evaluate(
            self,
            *,
            terminal: ApprovalDraftTerminalEvidence,
            evaluated_at: datetime,
        ) -> ApprovalDraftRetentionDecision:
            return ApprovalDraftRetentionDecision(
                terminal=terminal,
                evaluated_at=evaluated_at + timedelta(seconds=1),
                policy_version="retention-v1",
                retain_until=terminal.terminal_at + timedelta(days=1),
                purge_eligible=True,
            )

    with pytest.raises(ApprovalOperationsIntegrityError) as caught:
        _retention_app(harness, WrongPolicy()).retention_status("approval-1", T2)

    assert caught.value.args == ()


def test_invalid_time_is_rejected_before_store_or_policy_access() -> None:
    class DatetimeSubclass(datetime):
        pass

    requests = Mock()
    approvals = Mock()
    reader = Mock()
    policy = _RetentionPolicy()
    app = ApprovalOperationsApplication(
        requests=requests,
        approvals=approvals,
        reader=reader,
        evidence_recorder=ApprovalEventRecorder(InMemoryApprovalEventJournal()),
        retention_policy=policy,
    )

    for invalid in (
        datetime(2026, 7, 16, 9, 0),
        DatetimeSubclass(2026, 7, 16, 9, 0, tzinfo=timezone.utc),
    ):
        with pytest.raises(Exception) as caught:
            app.retention_status("approval-1", invalid)
        assert type(caught.value).__name__ == "ApprovalOperationsInvalid"

    approvals.get.assert_not_called()
    assert policy.calls == 0


def test_pending_request_timestamp_must_exactly_match_current_assignment() -> None:
    harness = _Harness()

    class TamperedRequests:
        def get(self, request_id: str) -> object:
            request = harness.uow.get(request_id)
            assert request is not None
            return request.model_copy(
                update={"updated_at": request.updated_at + timedelta(seconds=1)}
            )

    policy = _RetentionPolicy()
    app = ApprovalOperationsApplication(
        requests=cast(QuestionRequestStore, TamperedRequests()),
        approvals=harness.approvals,
        reader=harness.uow,
        evidence_recorder=ApprovalEventRecorder(InMemoryApprovalEventJournal()),
        retention_policy=policy,
    )

    with pytest.raises(ApprovalOperationsIntegrityError) as caught:
        app.retention_status("approval-1", T2)

    assert caught.value.args == ()
    assert policy.calls == 0


def test_policy_cannot_leak_application_error_payload() -> None:
    harness = _Harness()
    harness.app.decide("approval-1", harness.principal, ApproveIntent())

    class HostilePolicy(_RetentionPolicy):
        def evaluate(
            self,
            *,
            terminal: ApprovalDraftTerminalEvidence,
            evaluated_at: datetime,
        ) -> ApprovalDraftRetentionDecision:
            del terminal, evaluated_at
            raise ApprovalOperationsConflict("secret")

    with pytest.raises(ApprovalOperationsDependency) as caught:
        _retention_app(harness, HostilePolicy()).retention_status("approval-1", T2)

    assert caught.value.args == ()
    assert "secret" not in str(caught.value)


def test_evaluation_before_terminal_is_invalid_without_calling_policy() -> None:
    harness = _Harness()
    harness.app.decide("approval-1", harness.principal, ApproveIntent())
    policy = _RetentionPolicy()
    bundle = harness.uow.by_request("request-1")
    assert bundle is not None
    too_early = bundle.completion.completed_at - timedelta(microseconds=1)

    with pytest.raises(Exception) as caught:
        _retention_app(harness, policy).retention_status("approval-1", too_early)

    assert type(caught.value).__name__ == "ApprovalOperationsInvalid"
    assert caught.value.args == ()
    assert policy.calls == 0


def test_terminal_predecessor_and_current_share_one_policy_result_and_event() -> None:
    harness = _Harness()
    predecessor = harness.approvals.get("approval-1")
    assert predecessor is not None
    created_at = predecessor.created_at + timedelta(minutes=5)
    successor = ApprovalItem(
        item_id="approval-2",
        org_id=predecessor.org_id,
        request_id=predecessor.request_id,
        awaiting_revision=predecessor.awaiting_revision + 1,
        attempt=predecessor.attempt,
        route=predecessor.route,
        draft=predecessor.draft,
        requirement=ApprovalRequired(
            approver_id="bob",
            policy_version="approval-v2",
        ),
        created_at=created_at,
        due_at=created_at + timedelta(hours=1),
        approval_round=2,
        supersedes_item_id=predecessor.item_id,
    )
    harness.approvals.supersede_and_create_if_open(
        predecessor.item_id,
        ApprovalSupersession(
            reason="reassigned",
            successor_item_id=successor.item_id,
            superseded_at=created_at,
        ),
        successor,
        expected_generation=ApprovalAssignmentGeneration.from_item(predecessor),
    )
    request = harness.uow.get("request-1")
    assert request is not None
    reassigned = request.reassign_approval(
        previous_item_id=predecessor.item_id,
        successor_item_id=successor.item_id,
        due_at=successor.due_at,
        clock=lambda: created_at,
    )
    assert harness.uow.compare_and_set(
        request.request_id,
        request.revision,
        request,
        reassigned,
    )

    class Deadline:
        def deadline_for(
            self,
            org_id: str,
            state_kind: str,
            started_at: datetime,
        ) -> datetime:
            del org_id, state_kind
            return started_at + timedelta(hours=1)

    class Authorizer:
        def authorize(
            self,
            org_id: str,
            designated_approver_id: str,
            actor_id: str,
            action_kind: str,
            policy_version: str,
        ) -> ApprovalAuthorization:
            del org_id, designated_approver_id, actor_id, action_kind
            return ApprovalAuthorization(policy_version=policy_version)

    boundary = ApprovalBoundary(
        requests=harness.uow,
        approvals=harness.approvals,
        policy=harness.policy,
        authorizer=Authorizer(),
        deadline_policy=Deadline(),
        draft_id_factory=lambda: "unused-draft",
        item_id_factory=lambda: "unused-item",
        clock=lambda: created_at + timedelta(minutes=1),
        production_style=True,
    )
    approved = boundary.decide(
        successor.item_id,
        ApproverPrincipal(org_id="org-1", subject_id="bob"),
        Approve(by_approver="bob"),
    )
    harness.uow._planner._clock = (  # pyright: ignore[reportPrivateUsage]
        lambda: created_at + timedelta(minutes=2)
    )
    harness.uow.complete(approved)
    policy = _RetentionPolicy()
    journal = InMemoryApprovalEventJournal()
    app = _retention_app(harness, policy, journal)

    from_predecessor = app.retention_status(predecessor.item_id, T2)
    from_current = app.retention_status(successor.item_id, T2)

    assert from_predecessor == from_current
    assert policy.calls == 1
    assert len(journal.for_request("org-1", "request-1")) == 1


def test_hostile_round_index_is_integrity_before_policy() -> None:
    harness = _Harness()

    class TamperedApprovals:
        def __getattr__(self, name: str) -> object:
            return getattr(harness.approvals, name)

        def get_by_request_attempt_round(
            self,
            request_id: str,
            attempt: int,
            approval_round: int,
        ) -> ApprovalItem | None:
            item = harness.approvals.get_by_request_attempt_round(
                request_id,
                attempt,
                approval_round,
            )
            if item is None:
                return None
            return item.model_copy(update={"org_id": "hostile-org"})

    policy = _RetentionPolicy()
    app = ApprovalOperationsApplication(
        requests=harness.uow,
        approvals=cast(ApprovalStore, TamperedApprovals()),
        reader=harness.uow,
        evidence_recorder=ApprovalEventRecorder(InMemoryApprovalEventJournal()),
        retention_policy=policy,
    )

    with pytest.raises(ApprovalOperationsIntegrityError) as caught:
        app.retention_status("approval-1", T2)

    assert caught.value.args == ()
    assert policy.calls == 0
