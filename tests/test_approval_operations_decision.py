from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from typing import cast

import pytest
from pydantic import ValidationError

from agent_org_network.answer_finalization import (
    AnswerCompletion,
    AnswerResponsibilitySnapshot,
    CompletionBundle,
    CompletionFaultPoint,
    HumanApprovalEvidence,
    InMemoryQuestionCompletionUnitOfWork,
    QuestionCompletionReader,
    QuestionCompletionUnitOfWork,
)
from agent_org_network.approval import (
    ApprovalAction,
    ApprovalAuthorization,
    ApprovalAuthorizationDependencyError,
    ApprovalBoundary,
    ApprovalItem,
    ApprovalRequired,
    ApprovalSupersession,
    Approve,
    ApproveWithEdit,
    ApproverPrincipal,
    AnswerCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
    Reject,
)
from agent_org_network.approval_operations import (
    ApprovalAnswered,
    ApprovalDeclined,
    ApprovalOperationsApplication,
    ApprovalOperationsConflict,
    ApprovalOperationsDependency,
    ApprovalOperationsError,
    ApprovalOperationsIntegrityError,
    ApprovalOperationsInvalid,
    ApprovalOperationsNotFoundOrDenied,
    ApproveIntent,
    ApproveWithEditIntent,
    RejectIntent,
)
from agent_org_network.approval_evidence import (
    ApprovalEvent,
    ApprovalEventRecorder,
    InMemoryApprovalEventJournal,
)
from agent_org_network.p17_manager_disposition import (
    TerminalAlreadyPublished,
    TerminalDeferred,
    TerminalDelivery,
    TerminalPublished,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingApproval,
    DeclinedRequest,
    FailedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)


T0 = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
ROUTE = RouteTarget(
    intent="refund",
    agent_id="refund-card",
    requires_approval=True,
    authority_version="route-v1",
)


class _Policy:
    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: str,
    ) -> NoApprovalRequired | ApprovalRequired:
        del org_id, route, candidate_mode
        return ApprovalRequired(approver_id="alice", policy_version="approval-v1")


class _Authorizer:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.deny = False
        self.calls = 0

    def authorize(
        self,
        org_id: str,
        designated_approver_id: str,
        actor_id: str,
        action_kind: str,
        policy_version: str,
    ) -> ApprovalAuthorization | None:
        del action_kind
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.deny:
            return None
        if (org_id, designated_approver_id, actor_id) != ("org-1", "alice", "alice"):
            return None
        return ApprovalAuthorization(policy_version=policy_version)


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        del org_id, state_kind
        return started_at + timedelta(hours=1)


class _Resolver:
    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot:
        del org_id
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id="owner-1")


class _Publisher:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.malformed: object | None = None

    def publish_terminal(self, request_id: str) -> TerminalDelivery:
        self.calls.append(request_id)
        if self.error is not None:
            raise self.error
        if self.malformed is not None:
            return cast(TerminalDelivery, self.malformed)
        return TerminalPublished() if len(self.calls) == 1 else TerminalAlreadyPublished()


class _CommitThenLoseEventJournal:
    def __init__(self) -> None:
        self.target = InMemoryApprovalEventJournal()
        self.lose_once = True

    def append_batch_once(self, events: tuple[ApprovalEvent, ...]) -> tuple[ApprovalEvent, ...]:
        result = self.target.append_batch_once(events)
        if self.lose_once:
            self.lose_once = False
            raise RuntimeError("event append response lost")
        return result

    def append_once(self, event: ApprovalEvent) -> ApprovalEvent:
        return self.append_batch_once((event,))[0]

    def get(self, event_id: str) -> ApprovalEvent | None:
        return self.target.get(event_id)

    def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]:
        return self.target.for_request(org_id, request_id)


class _ToggleDecisionEventJournal(_CommitThenLoseEventJournal):
    def __init__(self) -> None:
        super().__init__()
        self.lose_once = False
        self.fail = True

    def append_batch_once(self, events: tuple[ApprovalEvent, ...]) -> tuple[ApprovalEvent, ...]:
        if self.fail:
            raise RuntimeError("journal unavailable")
        return self.target.append_batch_once(events)


class _CommitThenRaiseCompletion:
    def __init__(self, target: InMemoryQuestionCompletionUnitOfWork) -> None:
        self.target = target
        self.raise_once = True

    def complete(self, handoff: object) -> AnswerCompletion:
        result = self.target.complete(handoff)
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("commit response lost")
        return result

    def by_request(self, request_id: str) -> CompletionBundle | None:
        return self.target.by_request(request_id)

    def by_record(self, record_id: str) -> CompletionBundle | None:
        return self.target.by_record(record_id)


class _FailingReader:
    def __init__(self, target: InMemoryQuestionCompletionUnitOfWork) -> None:
        self.target = target
        self.fail = True

    def by_request(self, request_id: str) -> CompletionBundle | None:
        if self.fail:
            raise RuntimeError("reader unavailable")
        return self.target.by_request(request_id)

    def by_record(self, record_id: str) -> CompletionBundle | None:
        return self.target.by_record(record_id)


class _TamperedCompletion:
    def __init__(self, target: InMemoryQuestionCompletionUnitOfWork) -> None:
        self.target = target

    def complete(self, handoff: object) -> AnswerCompletion:
        completed = self.target.complete(handoff)
        return completed.model_copy(update={"record_id": "record-tampered"})

    def by_request(self, request_id: str) -> CompletionBundle | None:
        return self.target.by_request(request_id)

    def by_record(self, record_id: str) -> CompletionBundle | None:
        return self.target.by_record(record_id)


class _TamperedBundleReader:
    def __init__(
        self,
        target: InMemoryQuestionCompletionUnitOfWork,
        tamper: str,
    ) -> None:
        self.target = target
        self.tamper = tamper

    def by_request(self, request_id: str) -> CompletionBundle | None:
        bundle = self.target.by_request(request_id)
        if bundle is None:
            return None
        if self.tamper == "audit":
            evidence = bundle.terminal_audit.approval.model_copy(
                update={"policy_version": "policy-tampered"}
            )
            audit = bundle.terminal_audit.model_copy(update={"approval": evidence})
            return bundle.model_copy(update={"terminal_audit": audit})
        if self.tamper == "revision":
            request = bundle.request.model_copy(update={"revision": 99})
            return bundle.model_copy(update={"request": request})
        raise AssertionError("unknown tamper")

    def by_record(self, record_id: str) -> CompletionBundle | None:
        return self.target.by_record(record_id)


class _TamperedDeclinedRequestUow(InMemoryQuestionCompletionUnitOfWork):
    tamper: str = "revision"

    def get(self, request_id: str) -> QuestionRequest | None:
        request = super().get(request_id)
        if request is None or not isinstance(request.state, DeclinedRequest):
            return request
        if self.tamper == "revision":
            return request.model_copy(update={"revision": request.revision + 1})
        if self.tamper == "time":
            return request.model_copy(
                update={"updated_at": request.updated_at + timedelta(seconds=1)}
            )
        raise AssertionError("unknown tamper")


class _TerminalViewTamperingUow(InMemoryQuestionCompletionUnitOfWork):
    tamper: str | None = None

    def get(self, request_id: str) -> QuestionRequest | None:
        request = super().get(request_id)
        if request is None or self.tamper is None:
            return request
        if self.tamper == "answered":
            return request.model_copy(update={"state": AnsweredRequest(record_id="forged")})
        if self.tamper == "declined":
            return request.model_copy(update={"state": DeclinedRequest(reason_code="forged")})
        if self.tamper == "failed":
            return request.model_copy(update={"state": FailedRequest(error_code="forged")})
        if self.tamper == "wrong_reason":
            return request.model_copy(update={"state": DeclinedRequest(reason_code="wrong-reason")})
        if self.tamper == "revision":
            return request.model_copy(update={"revision": request.revision + 1})
        if self.tamper == "time":
            return request.model_copy(
                update={"updated_at": request.updated_at + timedelta(seconds=1)}
            )
        raise AssertionError("unknown tamper")


class _TamperedIndexApprovalStore(InMemoryApprovalStore):
    tamper: str | None = None

    def get_by_request_attempt(
        self,
        request_id: str,
        attempt: int,
    ) -> ApprovalItem | None:
        item = super().get_by_request_attempt(request_id, attempt)
        if item is not None and self.tamper == "current":
            return item.model_copy(update={"org_id": "org-tampered"})
        return item

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        item = super().get_by_request_attempt_round(request_id, attempt, approval_round)
        if item is not None and self.tamper == "round":
            return item.model_copy(update={"org_id": "org-tampered"})
        return item


class _GenerationSwapApprovalStore(InMemoryApprovalStore):
    """첫 exact-read가 끝난 뒤 같은 ID의 다른 immutable generation을 제시한다."""

    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0
        self.armed = False
        self.resolve_writes = 0
        self._swapped: ApprovalItem | None = None

    def get(self, item_id: str) -> ApprovalItem | None:
        self.get_calls += 1
        if self.armed and self.get_calls == 2:
            legitimate = super().get(item_id)
            assert legitimate is not None
            self._swapped = legitimate.model_copy(
                update={
                    "approval_round": 2,
                    "supersedes_item_id": "ghost-predecessor",
                }
            )
        if self._swapped is not None:
            return self._swapped
        return super().get(item_id)

    def get_by_request_attempt(
        self,
        request_id: str,
        attempt: int,
    ) -> ApprovalItem | None:
        if self._swapped is not None:
            return self._swapped
        return super().get_by_request_attempt(request_id, attempt)

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        if self._swapped is not None:
            return self._swapped if approval_round == self._swapped.approval_round else None
        return super().get_by_request_attempt_round(request_id, attempt, approval_round)

    def resolve_if_open(
        self,
        item_id: str,
        action: ApprovalAction,
        transition: Callable[[ApprovalItem], ApprovalItem],
    ) -> ApprovalItem:
        del item_id, action
        assert self._swapped is not None
        resolved = transition(self._swapped)
        self.resolve_writes += 1
        self._swapped = resolved
        return resolved


class _PostResolveGenerationSwapStore(InMemoryApprovalStore):
    """정상 resolve 뒤 지정한 get 호출부터 다른 immutable generation을 반환한다."""

    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0
        self.swap_on_get = 3
        self._swapped: ApprovalItem | None = None

    def get(self, item_id: str) -> ApprovalItem | None:
        self.get_calls += 1
        if self.get_calls == self.swap_on_get:
            legitimate = super().get(item_id)
            assert legitimate is not None and legitimate.status == "resolved"
            self._swapped = legitimate.model_copy(
                update={
                    "approval_round": 2,
                    "supersedes_item_id": "ghost-predecessor",
                }
            )
        if self._swapped is not None:
            return self._swapped
        return super().get(item_id)

    def get_by_request_attempt(
        self,
        request_id: str,
        attempt: int,
    ) -> ApprovalItem | None:
        if self._swapped is not None:
            return self._swapped
        return super().get_by_request_attempt(request_id, attempt)

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        if self._swapped is not None:
            return self._swapped if approval_round == self._swapped.approval_round else None
        return super().get_by_request_attempt_round(request_id, attempt, approval_round)


class _Harness:
    def __init__(
        self,
        *,
        candidate: AnswerCandidate | None = None,
        fault_injector: Callable[[CompletionFaultPoint], None] | None = None,
        uow_type: type[InMemoryQuestionCompletionUnitOfWork] = (
            InMemoryQuestionCompletionUnitOfWork
        ),
        approvals_type: type[InMemoryApprovalStore] = InMemoryApprovalStore,
    ) -> None:
        self.approvals = approvals_type()
        self.policy = _Policy()
        self.authorizer = _Authorizer()
        self.uow = uow_type(
            policy=self.policy,
            approvals=self.approvals,
            responsibility_resolver=_Resolver(),
            record_id_factory=lambda: "record-1",
            clock=lambda: T1,
            fault_injector=fault_injector,
        )
        self.boundary = ApprovalBoundary(
            requests=self.uow,
            approvals=self.approvals,
            policy=self.policy,
            authorizer=self.authorizer,
            deadline_policy=_Deadline(),
            draft_id_factory=lambda: "draft-1",
            item_id_factory=lambda: "approval-1",
            clock=lambda: T1,
            production_style=True,
        )
        received = QuestionRequest.receive(
            org_id="org-1",
            requester_id="requester-1",
            question="환불해 주세요.",
            request_id_factory=lambda: "request-1",
            clock=lambda: T0,
            due_at=T1,
        )
        self.uow.create(received)
        ready = received.record_initial_routing(
            intent=ROUTE.intent,
            disposition="routed",
            target=ReadyToDispatch(
                route=ROUTE,
                attempt=1,
                trigger_key="request-dispatch:request-1:1",
                handling=HandlingAssignment(
                    kind="system",
                    ref="request-dispatch:request-1:1",
                    due_at=T1,
                ),
            ),
            clock=lambda: T0,
        )
        assert self.uow.compare_and_set("request-1", 0, received, ready)
        self.candidate = candidate or AnswerCandidate(
            text="환불할 수 있습니다.",
            sources=("refund.md",),
            mode="full",
            snapshot_sha="sha-1",
        )
        self.boundary.gate_candidate(
            "request-1",
            expected_revision=1,
            candidate=self.candidate,
        )
        if isinstance(self.approvals, _GenerationSwapApprovalStore):
            self.approvals.get_calls = 0
            self.approvals.armed = True
        if isinstance(self.approvals, _PostResolveGenerationSwapStore):
            self.approvals.get_calls = 0
        self.publisher = _Publisher()
        self.app = ApprovalOperationsApplication(
            requests=self.uow,
            approvals=self.approvals,
            boundary=self.boundary,
            completion=self.uow,
            reader=self.uow,
            terminal_publisher=self.publisher,
        )

    def rebuild_app(
        self,
        *,
        completion: QuestionCompletionUnitOfWork | None = None,
        reader: QuestionCompletionReader | None = None,
    ) -> None:
        self.app = ApprovalOperationsApplication(
            requests=self.uow,
            approvals=self.approvals,
            boundary=self.boundary,
            completion=completion or self.uow,
            reader=reader or self.uow,
            terminal_publisher=self.publisher,
        )

    @property
    def principal(self) -> ApproverPrincipal:
        return ApproverPrincipal(org_id="org-1", subject_id="alice")


def test_decision_intents_are_strict_actor_free_and_exact() -> None:
    assert ApproveIntent().model_dump() == {"kind": "approve"}
    assert ApproveWithEditIntent(edited_text="수정 답변").model_dump() == {
        "kind": "approve_with_edit",
        "edited_text": "수정 답변",
    }
    assert RejectIntent(reason_code="unsupported").model_dump() == {
        "kind": "reject",
        "reason_code": "unsupported",
    }
    with pytest.raises(ValidationError):
        ApproveIntent.model_validate({"kind": "approve", "by_approver": "mallory"})
    with pytest.raises(ValidationError):
        ApproveWithEditIntent.model_validate(
            {"kind": "approve_with_edit", "edited_text": "x", "org_id": "org-1"}
        )

    class _IntentSubclass(ApproveIntent):
        pass

    harness = _Harness()
    with pytest.raises(ApprovalOperationsInvalid):
        harness.app.decide("approval-1", harness.principal, _IntentSubclass())


def test_decision_dependencies_are_all_or_none_and_exact_identity() -> None:
    harness = _Harness()

    with pytest.raises(ApprovalOperationsDependency) as caught:
        ApprovalOperationsApplication(
            requests=harness.uow,
            approvals=harness.approvals,
            boundary=harness.boundary,
        )

    assert caught.value.args == ()
    assert harness.boundary.matches_dependencies(
        requests=harness.uow,
        approvals=harness.approvals,
        policy=harness.policy,
        authorizer=harness.authorizer,
    )
    assert harness.app.matches_dependencies(
        requests=harness.uow,
        approvals=harness.approvals,
        boundary=harness.boundary,
        completion=harness.uow,
        reader=harness.uow,
        terminal_publisher=harness.publisher,
    )
    assert not harness.app.matches_dependencies(
        requests=harness.uow,
        approvals=InMemoryApprovalStore(),
        boundary=harness.boundary,
        completion=harness.uow,
        reader=harness.uow,
        terminal_publisher=harness.publisher,
    )


def test_approve_runs_boundary_finalization_exact_read_and_publish() -> None:
    harness = _Harness()

    result = harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert result == ApprovalAnswered(
        item_id="approval-1",
        approval_round=1,
        request_id="request-1",
        record_id="record-1",
        action="approve",
        delivery=TerminalPublished(),
    )
    bundle = harness.uow.by_request("request-1")
    assert bundle is not None
    assert bundle.completion.text == harness.candidate.text
    assert bundle.completion.sources == harness.candidate.sources
    assert bundle.completion.snapshot_sha == harness.candidate.snapshot_sha
    assert bundle.completion.review_status == "approved"
    assert isinstance(bundle.request.state, AnsweredRequest)
    assert bundle.request.revision == 3
    assert harness.publisher.calls == ["request-1"]


def test_approve_with_edit_changes_only_text_and_promotes_draft_only() -> None:
    candidate = AnswerCandidate(
        text="초안",
        sources=("refund.md",),
        mode="draft_only",
        snapshot_sha="sha-1",
    )
    harness = _Harness(candidate=candidate)

    result = harness.app.decide(
        "approval-1",
        harness.principal,
        ApproveWithEditIntent(edited_text="승인된 답변"),
    )

    assert isinstance(result, ApprovalAnswered)
    assert result.action == "approve_with_edit"
    bundle = harness.uow.by_request("request-1")
    assert bundle is not None
    assert bundle.completion.text == "승인된 답변"
    assert bundle.completion.sources == candidate.sources
    assert bundle.completion.snapshot_sha == candidate.snapshot_sha
    assert bundle.completion.mode == "full"


def test_reject_requires_exact_declined_request_and_no_completion() -> None:
    harness = _Harness()

    result = harness.app.decide(
        "approval-1",
        harness.principal,
        RejectIntent(reason_code="unsupported"),
    )

    assert result == ApprovalDeclined(
        item_id="approval-1",
        approval_round=1,
        request_id="request-1",
        reason_code="unsupported",
        delivery=TerminalPublished(),
    )
    request = harness.uow.get("request-1")
    assert request is not None and isinstance(request.state, DeclinedRequest)
    assert request.revision == 3
    assert harness.uow.by_request("request-1") is None


@pytest.mark.parametrize(
    ("intent", "result_type"),
    [(ApproveIntent(), ApprovalAnswered), (RejectIntent(reason_code="no"), ApprovalDeclined)],
)
def test_same_decision_retry_converges_and_only_delivery_observation_changes(
    intent: ApproveIntent | RejectIntent,
    result_type: type[ApprovalAnswered] | type[ApprovalDeclined],
) -> None:
    harness = _Harness()

    first = harness.app.decide("approval-1", harness.principal, intent)
    second = harness.app.decide("approval-1", harness.principal, intent)

    assert isinstance(first, result_type)
    assert isinstance(second, result_type)
    assert first.model_copy(update={"delivery": second.delivery}) == second
    assert first.delivery == TerminalPublished()
    assert second.delivery == TerminalAlreadyPublished()


def test_resolved_item_forward_repair_and_different_action_conflict() -> None:
    harness = _Harness()
    approved = harness.boundary.decide(
        "approval-1",
        harness.principal,
        # The application must be able to recover after this boundary response was lost.
        Approve(by_approver="alice"),
    )
    assert approved.request_id == "request-1"

    recovered = harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert isinstance(recovered, ApprovalAnswered)
    with pytest.raises(ApprovalOperationsConflict):
        harness.app.decide(
            "approval-1",
            harness.principal,
            RejectIntent(reason_code="changed"),
        )


@pytest.mark.parametrize("tamper", ["declined", "failed", "revision", "time"])
def test_resolved_approve_validates_stored_terminal_before_incoming_conflict(
    tamper: str,
) -> None:
    harness = _Harness(uow_type=_TerminalViewTamperingUow)
    harness.app.decide("approval-1", harness.principal, ApproveIntent())
    assert isinstance(harness.uow, _TerminalViewTamperingUow)
    harness.uow.tamper = tamper

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide(
            "approval-1",
            harness.principal,
            RejectIntent(reason_code="different"),
        )


@pytest.mark.parametrize(
    "tamper",
    ["answered", "failed", "wrong_reason", "revision", "time"],
)
def test_resolved_reject_validates_stored_terminal_before_incoming_conflict(
    tamper: str,
) -> None:
    harness = _Harness(uow_type=_TerminalViewTamperingUow)
    harness.app.decide(
        "approval-1",
        harness.principal,
        RejectIntent(reason_code="unsupported"),
    )
    assert isinstance(harness.uow, _TerminalViewTamperingUow)
    harness.uow.tamper = tamper

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())


@pytest.mark.parametrize(
    ("winner", "loser"),
    [
        (ApproveIntent(), RejectIntent(reason_code="different")),
        (RejectIntent(reason_code="unsupported"), ApproveIntent()),
    ],
)
def test_exact_terminal_keeps_different_incoming_action_as_field_free_conflict(
    winner: ApproveIntent | RejectIntent,
    loser: ApproveIntent | RejectIntent,
) -> None:
    harness = _Harness()
    harness.app.decide("approval-1", harness.principal, winner)

    with pytest.raises(ApprovalOperationsConflict) as caught:
        harness.app.decide("approval-1", harness.principal, loser)

    assert caught.value.args == () and caught.value.__dict__ == {}


def test_awaiting_approval_partial_repair_keeps_different_action_as_conflict() -> None:
    harness = _Harness()
    harness.boundary.decide(
        "approval-1",
        harness.principal,
        Approve(by_approver="alice"),
    )
    request = harness.uow.get("request-1")
    assert request is not None and isinstance(request.state, AwaitingApproval)

    with pytest.raises(ApprovalOperationsConflict) as caught:
        harness.app.decide(
            "approval-1",
            harness.principal,
            RejectIntent(reason_code="different"),
        )

    assert caught.value.args == () and caught.value.__dict__ == {}
    assert harness.uow.by_request("request-1") is None


def _resolve_boundary_only(
    harness: _Harness,
    stored_kind: str,
) -> ApproveIntent | RejectIntent:
    if stored_kind == "approve":
        harness.boundary.decide(
            "approval-1",
            harness.principal,
            Approve(by_approver="alice"),
        )
        return RejectIntent(reason_code="different")
    item = harness.approvals.get("approval-1")
    assert item is not None
    stored_action = Reject(by_approver="alice", reason_code="unsupported")
    harness.approvals.resolve_if_open(
        item.item_id,
        stored_action,
        lambda current: current.resolve(
            action=stored_action,
            approved_candidate=None,
            resolved_at=T1,
        ),
    )
    return ApproveIntent()


@pytest.mark.parametrize("stored_kind", ["approve", "reject"])
@pytest.mark.parametrize("reader_mode", ["completion", "error", "none"])
def test_resolved_awaiting_partial_validates_completion_absence_before_conflict(
    stored_kind: str,
    reader_mode: str,
) -> None:
    harness = _Harness()
    incoming = _resolve_boundary_only(harness, stored_kind)
    request = harness.uow.get("request-1")
    assert request is not None and isinstance(request.state, AwaitingApproval)
    if reader_mode == "completion":
        completed = _Harness()
        completed.app.decide("approval-1", completed.principal, ApproveIntent())
        harness.rebuild_app(reader=completed.uow)
        expected_error: type[ApprovalOperationsError] = ApprovalOperationsIntegrityError
    elif reader_mode == "error":
        harness.rebuild_app(reader=_FailingReader(harness.uow))
        expected_error = ApprovalOperationsDependency
    else:
        expected_error = ApprovalOperationsConflict

    with pytest.raises(expected_error) as caught:
        harness.app.decide("approval-1", harness.principal, incoming)

    assert caught.value.args == () and caught.value.__dict__ == {}


def test_designated_principal_and_guessed_ids_are_indistinguishably_hidden() -> None:
    harness = _Harness()
    attempts = (
        ("missing-secret-id", harness.principal),
        ("approval-1", ApproverPrincipal(org_id="org-2", subject_id="alice")),
        ("approval-1", ApproverPrincipal(org_id="org-1", subject_id="mallory")),
    )

    errors: list[ApprovalOperationsNotFoundOrDenied] = []
    for item_id, principal in attempts:
        with pytest.raises(ApprovalOperationsNotFoundOrDenied) as caught:
            harness.app.decide(item_id, principal, ApproveIntent())
        errors.append(caught.value)

    assert all(error.args == () and error.__dict__ == {} for error in errors)


def test_authorizer_exception_is_dependency_not_denial() -> None:
    harness = _Harness()
    harness.authorizer.error = RuntimeError("lower secret")

    with pytest.raises(ApprovalOperationsDependency) as caught:
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert caught.value.args == ()
    assert isinstance(caught.value.__cause__, ApprovalAuthorizationDependencyError)
    assert "lower secret" not in str(caught.value)


def test_authorizer_explicit_denial_is_hidden_not_dependency() -> None:
    harness = _Harness()
    harness.authorizer.deny = True

    with pytest.raises(ApprovalOperationsNotFoundOrDenied) as caught:
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert caught.value.args == ()
    assert harness.uow.by_request("request-1") is None


def test_completion_fault_can_retry_same_resolved_action_to_one_record() -> None:
    fail = True

    def inject(point: CompletionFaultPoint) -> None:
        nonlocal fail
        if fail and point == "before_commit":
            fail = False
            raise RuntimeError("precommit")

    harness = _Harness(fault_injector=inject)
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    result = harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert isinstance(result, ApprovalAnswered)
    assert result.record_id == "record-1"
    assert harness.publisher.calls == ["request-1"]


def test_commit_response_loss_retry_returns_original_record() -> None:
    harness = _Harness()
    lossy = _CommitThenRaiseCompletion(harness.uow)
    harness.rebuild_app(completion=lossy)

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    result = harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert isinstance(result, ApprovalAnswered)
    assert result.record_id == "record-1"
    bundle = harness.uow.by_request("request-1")
    assert bundle is not None and bundle.completion.record_id == "record-1"
    assert harness.publisher.calls == ["request-1"]


def test_reader_failure_never_publishes_and_retry_repairs_forward() -> None:
    harness = _Harness()
    reader = _FailingReader(harness.uow)
    harness.rebuild_app(reader=reader)

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert harness.publisher.calls == []
    reader.fail = False
    result = harness.app.decide("approval-1", harness.principal, ApproveIntent())
    assert isinstance(result, ApprovalAnswered)
    assert result.record_id == "record-1"


def test_tampered_completion_return_is_integrity_before_publish() -> None:
    harness = _Harness()
    harness.rebuild_app(completion=_TamperedCompletion(harness.uow))

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert harness.publisher.calls == []


@pytest.mark.parametrize("tamper", ["audit", "revision"])
def test_tampered_completion_bundle_is_integrity_before_publish(tamper: str) -> None:
    harness = _Harness()
    harness.rebuild_app(reader=_TamperedBundleReader(harness.uow, tamper))

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert harness.publisher.calls == []


@pytest.mark.parametrize("tamper", ["revision", "time"])
def test_reject_same_reason_with_wrong_causal_proof_is_integrity(tamper: str) -> None:
    _TamperedDeclinedRequestUow.tamper = tamper
    harness = _Harness(uow_type=_TamperedDeclinedRequestUow)

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide(
            "approval-1",
            harness.principal,
            RejectIntent(reason_code="unsupported"),
        )

    assert harness.publisher.calls == []


def test_declined_request_with_any_completion_bundle_is_integrity() -> None:
    approved = _Harness()
    approved.app.decide("approval-1", approved.principal, ApproveIntent())
    declined = _Harness()
    declined.rebuild_app(reader=approved.uow)

    with pytest.raises(ApprovalOperationsIntegrityError):
        declined.app.decide(
            "approval-1",
            declined.principal,
            RejectIntent(reason_code="unsupported"),
        )

    assert declined.publisher.calls == []


def test_superseded_predecessor_is_hidden_before_boundary_write() -> None:
    harness = _Harness()
    predecessor = harness.approvals.get("approval-1")
    assert predecessor is not None
    successor = ApprovalItem(
        item_id="approval-2",
        org_id=predecessor.org_id,
        request_id=predecessor.request_id,
        awaiting_revision=predecessor.awaiting_revision + 1,
        attempt=predecessor.attempt,
        route=predecessor.route,
        draft=predecessor.draft,
        requirement=ApprovalRequired(
            approver_id="alice",
            policy_version="approval-v2",
        ),
        created_at=T1 + timedelta(seconds=1),
        due_at=T1 + timedelta(hours=1),
        approval_round=2,
        supersedes_item_id=predecessor.item_id,
    )
    harness.approvals.supersede_and_create_if_open(
        predecessor.item_id,
        ApprovalSupersession(
            reason="reassigned",
            successor_item_id=successor.item_id,
            superseded_at=successor.created_at,
        ),
        successor,
    )

    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert harness.uow.by_request("request-1") is None
    assert harness.publisher.calls == []


@pytest.mark.parametrize(
    ("tamper", "error_type"),
    [
        ("current", ApprovalOperationsIntegrityError),
        ("round", ApprovalOperationsIntegrityError),
    ],
)
def test_tampered_generation_indexes_fail_before_any_decision_write(
    tamper: str,
    error_type: type[ApprovalOperationsError],
) -> None:
    harness = _Harness(approvals_type=_TamperedIndexApprovalStore)
    assert isinstance(harness.approvals, _TamperedIndexApprovalStore)
    harness.approvals.tamper = tamper

    with pytest.raises(error_type):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    item = harness.approvals.get("approval-1")
    assert item is not None and item.status == "open"
    request = harness.uow.get("request-1")
    assert request is not None and isinstance(request.state, AwaitingApproval)
    assert harness.uow.by_request("request-1") is None
    assert harness.authorizer.calls == 0
    assert harness.publisher.calls == []


def test_generation_swap_between_precheck_and_boundary_is_zero_write_integrity() -> None:
    harness = _Harness(approvals_type=_GenerationSwapApprovalStore)
    assert isinstance(harness.approvals, _GenerationSwapApprovalStore)

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    item = harness.approvals.get("approval-1")
    assert item is not None and item.status == "open" and item.approval_round == 2
    request = harness.uow.get("request-1")
    assert request is not None
    assert request.revision == 2 and isinstance(request.state, AwaitingApproval)
    assert harness.approvals.resolve_writes == 0
    assert harness.uow.by_request("request-1") is None
    assert harness.publisher.calls == []
    assert harness.authorizer.calls == 0


def test_post_boundary_generation_swap_blocks_completion_and_publish() -> None:
    harness = _Harness(approvals_type=_PostResolveGenerationSwapStore)
    assert isinstance(harness.approvals, _PostResolveGenerationSwapStore)
    harness.approvals.swap_on_get = 5

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert harness.approvals.get_calls >= 3
    assert harness.uow.by_request("request-1") is None
    assert harness.publisher.calls == []


def test_finalization_read_generation_swap_is_zero_completion_and_publish() -> None:
    harness = _Harness(approvals_type=_PostResolveGenerationSwapStore)
    assert isinstance(harness.approvals, _PostResolveGenerationSwapStore)
    harness.approvals.swap_on_get = 6

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert harness.uow.by_request("request-1") is None
    request = harness.uow.get("request-1")
    assert request is not None
    assert request.revision == 2 and isinstance(request.state, AwaitingApproval)
    assert harness.publisher.calls == []


def test_post_completion_generation_swap_blocks_publish_but_keeps_legit_completion() -> None:
    harness = _Harness(approvals_type=_PostResolveGenerationSwapStore)
    assert isinstance(harness.approvals, _PostResolveGenerationSwapStore)
    harness.approvals.swap_on_get = 7

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())

    bundle = harness.uow.by_request("request-1")
    assert bundle is not None
    assert bundle.completion.record_id == "record-1"
    evidence = bundle.terminal_audit.approval
    assert isinstance(evidence, HumanApprovalEvidence)
    assert evidence.item_id == "approval-1"
    assert harness.publisher.calls == []


def test_reject_post_boundary_generation_swap_blocks_publish() -> None:
    harness = _Harness(approvals_type=_PostResolveGenerationSwapStore)
    assert isinstance(harness.approvals, _PostResolveGenerationSwapStore)
    harness.approvals.swap_on_get = 5

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide(
            "approval-1",
            harness.principal,
            RejectIntent(reason_code="unsupported"),
        )

    request = harness.uow.get("request-1")
    assert request is not None
    assert request.revision == 3 and isinstance(request.state, DeclinedRequest)
    assert harness.uow.by_request("request-1") is None
    assert harness.publisher.calls == []


def test_publisher_exception_is_deferred_and_malformed_result_is_integrity() -> None:
    harness = _Harness()
    harness.publisher.error = RuntimeError("publish secret")

    first = harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert isinstance(first, ApprovalAnswered)
    assert first.delivery == TerminalDeferred(reason_code="publish_failed")
    harness.publisher.error = None
    recovered = harness.app.decide("approval-1", harness.principal, ApproveIntent())
    assert isinstance(recovered, ApprovalAnswered)
    assert recovered.delivery == TerminalAlreadyPublished()
    assert harness.publisher.calls == ["request-1", "request-1"]
    harness.publisher.malformed = object()
    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())


def test_terminal_delivery_subclass_is_rejected_as_integrity() -> None:
    harness = _Harness()
    subclass = type("TerminalPublishedSubclass", (TerminalPublished,), {})
    harness.publisher.malformed = subclass()

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())


def test_approve_and_reject_race_has_one_terminal_winner() -> None:
    harness = _Harness()

    def race(index: int) -> ApprovalAnswered | ApprovalDeclined | ApprovalOperationsConflict:
        intent = ApproveIntent() if index % 2 == 0 else RejectIntent(reason_code="unsupported")
        try:
            return harness.app.decide("approval-1", harness.principal, intent)
        except ApprovalOperationsConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(race, range(32)))

    terminal = [
        result for result in results if isinstance(result, (ApprovalAnswered, ApprovalDeclined))
    ]
    assert terminal
    assert {type(result) for result in terminal} in ({ApprovalAnswered}, {ApprovalDeclined})
    assert all(
        isinstance(result, ApprovalOperationsConflict)
        for result in results
        if result not in terminal
    )
    request = harness.uow.get("request-1")
    assert request is not None
    assert isinstance(request.state, (AnsweredRequest, DeclinedRequest))


def test_public_errors_remain_field_free_and_nonreflective() -> None:
    harness = _Harness()
    failures: list[ApprovalOperationsError] = []

    for item_id, principal, intent in (
        (" ", harness.principal, ApproveIntent()),
        ("missing", harness.principal, ApproveIntent()),
    ):
        try:
            harness.app.decide(item_id, principal, intent)
        except ApprovalOperationsError as error:
            failures.append(error)

    harness.app.decide("approval-1", harness.principal, ApproveIntent())
    try:
        harness.app.decide(
            "approval-1",
            harness.principal,
            RejectIntent(reason_code="secret-reason"),
        )
    except ApprovalOperationsError as error:
        failures.append(error)

    assert {type(error) for error in failures} == {
        ApprovalOperationsInvalid,
        ApprovalOperationsNotFoundOrDenied,
        ApprovalOperationsConflict,
    }
    assert all(error.args == () and error.__dict__ == {} for error in failures)


def test_same_action_concurrency_converges_on_one_terminal_record() -> None:
    harness = _Harness()

    def approve(_: int) -> ApprovalAnswered | ApprovalDeclined:
        return harness.app.decide("approval-1", harness.principal, ApproveIntent())

    with ThreadPoolExecutor(max_workers=32) as pool:
        raw_results = list(pool.map(approve, range(32)))

    assert all(isinstance(result, ApprovalAnswered) for result in raw_results)
    results = [result for result in raw_results if isinstance(result, ApprovalAnswered)]
    assert {result.record_id for result in results} == {"record-1"}
    assert sum(result.delivery == TerminalPublished() for result in results) == 1


def _decision_app_with_evidence(
    harness: _Harness,
    recorder: ApprovalEventRecorder,
    *,
    completion: QuestionCompletionUnitOfWork | None = None,
) -> ApprovalOperationsApplication:
    return ApprovalOperationsApplication(
        requests=harness.uow,
        approvals=harness.approvals,
        boundary=harness.boundary,
        completion=completion or harness.uow,
        reader=harness.uow,
        terminal_publisher=harness.publisher,
        evidence_recorder=recorder,
    )


@pytest.mark.parametrize(
    ("intent", "expected_kind"),
    [
        (ApproveIntent(), "approved"),
        (ApproveWithEditIntent(edited_text="수정된 최종 답변"), "approved_with_edit"),
        (RejectIntent(reason_code="sensitive-reason"), "rejected"),
    ],
)
def test_decision_records_one_body_free_event_after_terminal_verification(
    intent: ApproveIntent | ApproveWithEditIntent | RejectIntent,
    expected_kind: str,
) -> None:
    harness = _Harness()
    journal = InMemoryApprovalEventJournal()
    harness.app = _decision_app_with_evidence(harness, ApprovalEventRecorder(journal))

    first = harness.app.decide("approval-1", harness.principal, intent)
    second = harness.app.decide("approval-1", harness.principal, intent)

    assert type(second) is type(first)
    events = journal.for_request("org-1", "request-1")
    assert [event.kind for event in events] == [expected_kind]
    payload = str(events[0].model_dump(mode="json"))
    assert "환불할 수 있습니다." not in payload
    assert "수정된 최종 답변" not in payload
    assert "sensitive-reason" not in payload
    assert events[0].subject.kind == "human"
    assert events[0].subject.subject_id == "alice"


def test_decision_event_survives_publish_failure_and_append_response_loss() -> None:
    harness = _Harness()
    journal = _CommitThenLoseEventJournal()
    harness.publisher.error = RuntimeError("publisher down")
    harness.app = _decision_app_with_evidence(harness, ApprovalEventRecorder(journal))

    result = harness.app.decide("approval-1", harness.principal, ApproveIntent())

    assert isinstance(result, ApprovalAnswered)
    assert result.delivery == TerminalDeferred(reason_code="publish_failed")
    assert [event.kind for event in journal.for_request("org-1", "request-1")] == ["approved"]


def test_completion_response_loss_retry_repairs_missing_decision_event() -> None:
    harness = _Harness()
    journal = InMemoryApprovalEventJournal()
    lossy_completion = _CommitThenRaiseCompletion(harness.uow)
    harness.app = _decision_app_with_evidence(
        harness,
        ApprovalEventRecorder(journal),
        completion=lossy_completion,
    )

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())
    assert journal.for_request("org-1", "request-1") == ()

    harness.app.decide("approval-1", harness.principal, ApproveIntent())
    assert [event.kind for event in journal.for_request("org-1", "request-1")] == ["approved"]


def test_same_intent_retry_repairs_event_missing_after_terminal_commit() -> None:
    harness = _Harness()
    journal = _ToggleDecisionEventJournal()
    harness.app = _decision_app_with_evidence(harness, ApprovalEventRecorder(journal))

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.decide("approval-1", harness.principal, ApproveIntent())
    bundle = harness.uow.by_request("request-1")
    assert bundle is not None and bundle.completion.record_id == "record-1"
    assert journal.for_request("org-1", "request-1") == ()
    assert harness.publisher.calls == []

    journal.fail = False
    repaired = harness.app.decide("approval-1", harness.principal, ApproveIntent())
    assert isinstance(repaired, ApprovalAnswered)
    assert [event.kind for event in journal.for_request("org-1", "request-1")] == ["approved"]


@pytest.mark.parametrize(
    ("intent", "expected_kind", "expected_state"),
    [
        (ApproveIntent(), "approved", AnsweredRequest),
        (
            ApproveWithEditIntent(edited_text="경쟁에서 확정할 수정 답변"),
            "approved_with_edit",
            AnsweredRequest,
        ),
        (RejectIntent(reason_code="unsupported"), "rejected", DeclinedRequest),
    ],
)
def test_32_way_same_decision_converges_to_one_terminal_and_one_event(
    intent: ApproveIntent | ApproveWithEditIntent | RejectIntent,
    expected_kind: str,
    expected_state: type[AnsweredRequest] | type[DeclinedRequest],
) -> None:
    harness = _Harness()
    journal = InMemoryApprovalEventJournal()
    harness.app = _decision_app_with_evidence(harness, ApprovalEventRecorder(journal))
    start = Barrier(32)

    def decide(_: int) -> ApprovalAnswered | ApprovalDeclined:
        start.wait()
        return harness.app.decide("approval-1", harness.principal, intent)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(decide, range(32)))

    expected_result = ApprovalDeclined if expected_kind == "rejected" else ApprovalAnswered
    assert all(type(result) is expected_result for result in results)
    request = harness.uow.get("request-1")
    assert request is not None and type(request.state) is expected_state
    bundle = harness.uow.by_request("request-1")
    if expected_kind == "rejected":
        assert bundle is None
    else:
        assert bundle is not None and bundle.completion.record_id == "record-1"
    assert [event.kind for event in journal.for_request("org-1", "request-1")] == [expected_kind]


def test_32_way_different_decisions_have_one_canonical_payload_winner() -> None:
    harness = _Harness()
    journal = InMemoryApprovalEventJournal()
    harness.app = _decision_app_with_evidence(harness, ApprovalEventRecorder(journal))
    intents: tuple[ApproveIntent | ApproveWithEditIntent | RejectIntent, ...] = (
        ApproveIntent(),
        ApproveWithEditIntent(edited_text="수정안 A"),
        ApproveWithEditIntent(edited_text="수정안 B"),
        RejectIntent(reason_code="unsupported-a"),
        RejectIntent(reason_code="unsupported-b"),
    )
    start = Barrier(32)

    def decide(
        index: int,
    ) -> tuple[
        ApproveIntent | ApproveWithEditIntent | RejectIntent,
        ApprovalAnswered | ApprovalDeclined | ApprovalOperationsConflict,
    ]:
        intent = intents[index % len(intents)]
        start.wait()
        try:
            return intent, harness.app.decide("approval-1", harness.principal, intent)
        except ApprovalOperationsConflict as error:
            return intent, error

    with ThreadPoolExecutor(max_workers=32) as pool:
        observed = list(pool.map(decide, range(32)))

    item = harness.approvals.get("approval-1")
    assert item is not None and item.resolution is not None
    action = item.resolution.action
    if type(action) is Approve:
        winning_intent: ApproveIntent | ApproveWithEditIntent | RejectIntent = ApproveIntent()
        expected_event = "approved"
    elif type(action) is ApproveWithEdit:
        winning_intent = ApproveWithEditIntent(edited_text=action.edited_text)
        expected_event = "approved_with_edit"
    else:
        assert type(action) is Reject
        winning_intent = RejectIntent(reason_code=action.reason_code)
        expected_event = "rejected"

    winners = [result for intent, result in observed if intent == winning_intent]
    losers = [result for intent, result in observed if intent != winning_intent]
    expected_winner_type = ApprovalDeclined if type(action) is Reject else ApprovalAnswered
    assert winners and all(type(result) is expected_winner_type for result in winners)
    assert losers and all(type(result) is ApprovalOperationsConflict for result in losers)
    for result in losers:
        error = cast(ApprovalOperationsConflict, result)
        assert error.args == () and error.__dict__ == {}

    request = harness.uow.get("request-1")
    assert request is not None
    assert type(request.state) is (DeclinedRequest if type(action) is Reject else AnsweredRequest)
    bundle = harness.uow.by_request("request-1")
    assert (bundle is None) is (type(action) is Reject)
    if bundle is not None:
        assert bundle.completion.record_id == "record-1"
    assert [event.kind for event in journal.for_request("org-1", "request-1")] == [expected_event]
