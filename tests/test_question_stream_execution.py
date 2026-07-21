from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from threading import Barrier, Event, Lock
from typing import Literal

import pytest
from pydantic import ValidationError

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    CompletionBundle,
    CompletionFaultPoint,
    InMemoryQuestionCompletionUnitOfWork,
    QuestionCompletionReader,
)
from agent_org_network.approval import (
    ApprovalAuthorization,
    ApprovalBoundary,
    ApprovalRequired,
    AnswerCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.conflict import InMemoryConflictCaseStore
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.manager_queue import InMemoryManagerQueueStore
from agent_org_network.p17_manager_disposition import (
    ExecutionAlreadyRunning,
    ExecutionDeferred,
    ExecutionNotNeeded,
    ExecutionStarted,
    TerminalAlreadyPublished,
    TerminalPublished,
)
from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingManager,
    DeclinedRequest,
    FailedRequest,
    HandlingAssignment,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.question_resolution import (
    AskQuestion,
    AuthorityGrant,
    InitialRoutingDependencyError,
    QuestionResolutionApplication,
    RequesterPrincipal,
    RouteAuthority,
)
from agent_org_network.question_stream import (
    AcceptedEvent,
    DeclinedEvent,
    DoneEvent,
    FailedEvent,
    InMemoryQuestionStreamBroker,
    InterruptedEvent,
    PendingEvent,
    QuestionStreamEvent,
    QuestionStreamSubscription,
    TokenEvent,
)
from agent_org_network.question_stream_execution import (
    AnsweredQuestionLookup,
    BufferedAnswer,
    BufferedCompletion,
    DeclinedQuestionLookup,
    FailedQuestionLookup,
    OpenQuestionStream,
    PendingQuestionLookup,
    ProducerAlreadyRunning,
    ProducerCapacityExceeded,
    ProducerSchedulerClosed,
    ProducerStartDisposition,
    ProducerStarted,
    ProducerSubmissionError,
    QuestionProducerScheduler,
    QuestionStreamApplication,
    QuestionStreamCompletionMismatchError,
    QuestionStreamExecutionService,
    QuestionStreamRequestNotFoundError,
    QuestionSurfaceInterruptedError,
    QuestionStreamUnavailableError,
    StableStreamResult,
    ThreadedQuestionProducerScheduler,
)
from agent_org_network.runtime import AnswerMode
from agent_org_network.storage_capability import (
    NonDurableWorkflowCompositionError,
    QuestionCompletionStorageIdentityError,
)


NOW = datetime(2026, 7, 12, 16, 0, tzinfo=timezone.utc)


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
                approver_id="legal",
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
        return ApprovalAuthorization(policy_version=policy_version)


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        return started_at + timedelta(hours=1)


class _Authority:
    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        return AuthorityGrant(policy_version="route-v1")


class _DenyAuthority:
    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        return None


class _Resolver:
    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot | None:
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id="owner-1")


def _card(agent_id: str = "refund-card") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="owner-1",
        team="support",
        summary="환불 담당",
        domains=["refund"],
        last_reviewed_at=date(2026, 7, 1),
    )


class _Router:
    def __init__(
        self,
        decision: Routed | Contested | Unowned,
        *,
        before_route: Callable[[], None] | None = None,
    ) -> None:
        self.decision = decision
        self.before_route = before_route
        self.calls: list[str] = []

    def route(self, question: str) -> Routed | Contested | Unowned:
        if self.before_route is not None:
            self.before_route()
        self.calls.append(question)
        return self.decision


class _Source:
    def __init__(
        self,
        answer: BufferedAnswer,
        *,
        callback: Callable[[QuestionRequest], None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.buffered = answer
        self.callback = callback
        self.error = error
        self.calls: list[QuestionRequest] = []

    def answer(self, request: QuestionRequest) -> BufferedAnswer:
        self.calls.append(request)
        if self.callback is not None:
            self.callback(request)
        if self.error is not None:
            raise self.error
        return self.buffered


class _ImmediateScheduler:
    def __init__(self, before_job: Callable[[str], None] | None = None) -> None:
        self.before_job = before_job
        self.started: list[str] = []

    def ensure_started(
        self,
        request_id: str,
        job: Callable[[], None],
    ) -> ProducerStartDisposition:
        self.started.append(request_id)
        if self.before_job is not None:
            self.before_job(request_id)
        job()
        return ProducerStarted()

    def is_running(self, request_id: str) -> bool:
        return False

    def shutdown(self, *, wait: bool = True) -> None:
        return None


class _FailingScheduler(_ImmediateScheduler):
    def ensure_started(
        self,
        request_id: str,
        job: Callable[[], None],
    ) -> ProducerStartDisposition:
        raise ProducerSubmissionError("submit failed")


class _CapacityScheduler(_ImmediateScheduler):
    def ensure_started(
        self,
        request_id: str,
        job: Callable[[], None],
    ) -> ProducerStartDisposition:
        return ProducerCapacityExceeded()


class _TracingReader:
    def __init__(self, inner: QuestionCompletionReader) -> None:
        self.inner = inner
        self.calls: list[tuple[str, str]] = []

    def by_request(self, request_id: str) -> CompletionBundle | None:
        self.calls.append(("request", request_id))
        return self.inner.by_request(request_id)

    def by_record(self, record_id: str) -> CompletionBundle | None:
        self.calls.append(("record", record_id))
        return self.inner.by_record(record_id)


class _TracingExecution(QuestionStreamExecutionService):
    def __init__(self, inner: QuestionStreamExecutionService) -> None:
        self.inner = inner
        self.execute_calls: list[str] = []
        self.snapshot_calls: list[str] = []

    def execute(self, request_id: str) -> BufferedCompletion | StableStreamResult:
        self.execute_calls.append(request_id)
        return self.inner.execute(request_id)

    def snapshot(self, request_id: str) -> BufferedCompletion | StableStreamResult:
        self.snapshot_calls.append(request_id)
        return self.inner.snapshot(request_id)


class _FailFirstCompletionPublishBroker(InMemoryQuestionStreamBroker):
    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        completions: QuestionCompletionReader,
    ) -> None:
        super().__init__(
            max_queue_size=16,
            requests=requests,
            completions=completions,
        )
        self.completion_publish_calls = 0

    def publish_completion(self, request_id: str) -> int:
        self.completion_publish_calls += 1
        if self.completion_publish_calls == 1:
            raise RuntimeError("first delivery failure")
        return super().publish_completion(request_id)


@dataclass
class _Harness:
    uow: InMemoryQuestionCompletionUnitOfWork
    approvals: InMemoryApprovalStore
    resolution: QuestionResolutionApplication
    approval: ApprovalBoundary
    source: _Source
    reader: QuestionCompletionReader
    broker: InMemoryQuestionStreamBroker
    scheduler: QuestionProducerScheduler
    execution: QuestionStreamExecutionService
    application: QuestionStreamApplication


def _build(
    *,
    source: _Source | None = None,
    requires_approval: bool = False,
    policy_required: bool = False,
    decision: Routed | Contested | Unowned | None = None,
    scheduler: QuestionProducerScheduler | None = None,
    fault_injector: Callable[[CompletionFaultPoint], None] | None = None,
    reader_factory: Callable[[QuestionCompletionReader], QuestionCompletionReader] | None = None,
    before_route: Callable[[], None] | None = None,
    max_preview_bytes: int = 4096,
    route_authority: RouteAuthority | None = None,
    production_style: bool = False,
    broker_factory: Callable[
        [QuestionRequestStore, QuestionCompletionReader],
        InMemoryQuestionStreamBroker,
    ]
    | None = None,
) -> _Harness:
    policy = _Policy(required=policy_required)
    approvals = InMemoryApprovalStore()
    uow = InMemoryQuestionCompletionUnitOfWork(
        policy=policy,
        approvals=approvals,
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW + timedelta(minutes=1),
        fault_injector=fault_injector,
    )
    routed = decision or Routed(
        primary=_card(),
        intent="refund",
        requires_approval=requires_approval,
    )
    resolution = QuestionResolutionApplication(
        requests=uow,
        router=_Router(routed, before_route=before_route),
        conflicts=InMemoryConflictCaseStore(),
        managers=InMemoryManagerQueueStore(),
        route_authority=route_authority or _Authority(),
        deadline_policy=_Deadline(),
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW,
    )
    approval = ApprovalBoundary(
        requests=uow,
        approvals=approvals,
        policy=policy,
        authorizer=_Authorizer(),
        deadline_policy=_Deadline(),
        draft_id_factory=lambda: "draft-1",
        item_id_factory=lambda: "approval-1",
        clock=lambda: NOW + timedelta(seconds=30),
    )
    answer_source = source or _Source(
        BufferedAnswer(
            candidate=AnswerCandidate(
                text="영업일 3일 안에 처리됩니다.",
                sources=("refund-policy.md",),
                snapshot_sha="sha-1",
            ),
            tokens=("영업일 ", "3일 안에 ", "처리됩니다."),
        )
    )
    reader = reader_factory(uow) if reader_factory is not None else uow
    execution = QuestionStreamExecutionService(
        requests=uow,
        resolution=resolution,
        source=answer_source,
        approval=approval,
        completion=uow,
        reader=reader,
        max_preview_bytes=max_preview_bytes,
        production_style=production_style,
    )
    broker = (
        broker_factory(uow, reader)
        if broker_factory is not None
        else InMemoryQuestionStreamBroker(
            max_queue_size=16,
            requests=uow,
            completions=reader,
        )
    )
    producer = scheduler or ThreadedQuestionProducerScheduler(max_workers=2)
    application = QuestionStreamApplication(
        resolution=resolution,
        execution=execution,
        broker=broker,
        scheduler=producer,
    )
    return _Harness(
        uow=uow,
        approvals=approvals,
        resolution=resolution,
        approval=approval,
        source=answer_source,
        reader=reader,
        broker=broker,
        scheduler=producer,
        execution=execution,
        application=application,
    )


def _command() -> AskQuestion:
    return AskQuestion(
        principal=RequesterPrincipal(org_id="org-1", subject_id="user-1"),
        question="환불은 언제 되나요?",
    )


def _application_with_tracing_execution(
    harness: _Harness,
) -> tuple[QuestionStreamApplication, _TracingExecution]:
    execution = _TracingExecution(harness.execution)
    return (
        QuestionStreamApplication(
            resolution=harness.resolution,
            execution=execution,
            broker=harness.broker,
            scheduler=harness.scheduler,
        ),
        execution,
    )


def _events_until_end(subscription: QuestionStreamSubscription) -> list[object]:
    events: list[object] = []
    for _ in range(10):
        event = subscription.get(timeout=2)
        assert event is not None
        events.append(event)
        if isinstance(event, (DoneEvent, PendingEvent, InterruptedEvent)):
            return events
    raise AssertionError("stream end event가 없습니다.")


def test_buffered_answer_is_strict_frozen_and_exactly_matches_candidate_text() -> None:
    answer = BufferedAnswer(
        candidate=AnswerCandidate(text="최종 답", sources=("policy.md",)),
        tokens=("최종 ", "답"),
    )

    assert "".join(answer.tokens) == answer.candidate.text
    with pytest.raises(ValidationError):
        BufferedAnswer(candidate=answer.candidate, tokens=("다른 답",))
    with pytest.raises(ValidationError):
        BufferedAnswer(candidate=answer.candidate, tokens=("최종 답", ""))
    with pytest.raises(ValidationError):
        BufferedAnswer.model_validate(
            {"candidate": answer.candidate, "tokens": ["최종 답"]},
            strict=True,
        )
    with pytest.raises(ValidationError):
        answer.tokens = ("변조",)  # type: ignore[misc]


def test_threaded_scheduler_claims_before_submit_and_runs_one_job_for_32_callers() -> None:
    entered = Event()
    release = Event()
    calls = 0
    lock = Lock()

    def job() -> None:
        nonlocal calls
        with lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=3)

    scheduler = ThreadedQuestionProducerScheduler(max_workers=4)
    try:

        def start(_: int) -> object:
            return scheduler.ensure_started("req-1", job)

        with ThreadPoolExecutor(max_workers=32) as callers:
            dispositions = list(callers.map(start, range(32)))
        assert entered.wait(timeout=3)
        assert sum(isinstance(item, ProducerStarted) for item in dispositions) == 1
        assert sum(isinstance(item, ProducerAlreadyRunning) for item in dispositions) == 31
        assert calls == 1
        assert scheduler.is_running("req-1")
    finally:
        release.set()
        scheduler.shutdown(wait=True)


def test_Manager_wake_32way는_같은_Request의_실행을_한번만_시작한다() -> None:
    entered = Event()
    release = Event()

    def block_source(_: QuestionRequest) -> None:
        entered.set()
        assert release.wait(timeout=5)

    source = _Source(
        BufferedAnswer(
            candidate=AnswerCandidate(
                text="영업일 3일 안에 처리됩니다.",
                sources=("refund-policy.md",),
                snapshot_sha="sha-1",
            ),
            tokens=("영업일 ", "3일 안에 ", "처리됩니다."),
        ),
        callback=block_source,
    )
    scheduler = ThreadedQuestionProducerScheduler(max_workers=1)
    harness = _build(
        source=source,
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=scheduler,
    )
    harness.resolution.ask(_command())
    current = harness.uow.get("req-1")
    assert current is not None
    trigger_key = "request-dispatch:req-1:1"
    ready = current.transition(
        ReadyToDispatch(
            route=RouteTarget(
                intent="refund",
                agent_id="refund-card",
                requires_approval=False,
                authority_version="route-v1",
            ),
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert harness.uow.compare_and_set("req-1", current.revision, current, ready)
    barrier = Barrier(33)

    def wake(_: int) -> object:
        barrier.wait(timeout=5)
        return harness.application.ensure_started("req-1")

    try:
        with ThreadPoolExecutor(max_workers=32) as callers:
            futures = [callers.submit(wake, index) for index in range(32)]
            barrier.wait(timeout=5)
            results = [future.result(timeout=5) for future in futures]
        assert entered.wait(timeout=5)
        assert sum(isinstance(result, ExecutionStarted) for result in results) == 1
        assert sum(isinstance(result, ExecutionAlreadyRunning) for result in results) == 31
        assert len(source.calls) == 1
    finally:
        release.set()
        harness.application.shutdown(wait=True)


def test_threaded_scheduler_rejects_new_start_after_managed_shutdown() -> None:
    scheduler = ThreadedQuestionProducerScheduler(max_workers=1)
    scheduler.shutdown(wait=True)

    disposition = scheduler.ensure_started("req-1", lambda: pytest.fail("closed"))

    assert isinstance(disposition, ProducerSchedulerClosed)
    assert not scheduler.is_running("req-1")


def test_threaded_scheduler_bounds_queued_plus_running_inflight_work() -> None:
    entered = Event()
    release = Event()
    unexpected = Event()
    scheduler = ThreadedQuestionProducerScheduler(max_workers=1, max_inflight=1)

    def blocked() -> None:
        entered.set()
        assert release.wait(timeout=3)

    try:
        assert isinstance(scheduler.ensure_started("req-1", blocked), ProducerStarted)
        assert entered.wait(timeout=2)

        disposition = scheduler.ensure_started("req-2", unexpected.set)

        assert isinstance(disposition, ProducerCapacityExceeded)
        assert not unexpected.is_set()
        assert not scheduler.is_running("req-2")
    finally:
        release.set()
        scheduler.shutdown(wait=True)


def test_stream_execution_module_does_not_reuse_legacy_stream_events_as_payloads() -> None:
    # P17.3b orchestration may publish these public controls, but buffered answers never
    # embed stream events or expose an approval draft body.
    assert not isinstance(
        BufferedAnswer(candidate=AnswerCandidate(text="답"), tokens=("답",)),
        (DoneEvent, InterruptedEvent, PendingEvent, TokenEvent),
    )


def test_open_stream_stores_request_then_subscribes_before_immediate_execution() -> None:
    holder: dict[str, _Harness] = {}

    def before_job(request_id: str) -> None:
        harness = holder["value"]
        current = harness.uow.get(request_id)
        assert current is not None and isinstance(current.state, ReadyToDispatch)
        assert harness.broker.subscriber_count(request_id) == 1

    scheduler = _ImmediateScheduler(before_job)
    harness = _build(scheduler=scheduler)
    holder["value"] = harness

    opened = harness.application.open_stream(_command())

    assert isinstance(opened, OpenQuestionStream)
    assert opened.request_id == "req-1"
    events = _events_until_end(opened.subscription)
    assert [type(event) for event in events] == [
        AcceptedEvent,
        TokenEvent,
        TokenEvent,
        TokenEvent,
        DoneEvent,
    ]
    assert "".join(event.text for event in events if isinstance(event, TokenEvent)) == (
        "영업일 3일 안에 처리됩니다."
    )


def test_nonretryable_initial_routing_error_keeps_request_id_and_opens_neutral_stream() -> None:
    scheduler = _ImmediateScheduler()
    harness = _build(
        scheduler=scheduler,
        route_authority=_DenyAuthority(),
    )

    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert opened.request_id == "req-1"
    assert [type(event) for event in events] == [AcceptedEvent, InterruptedEvent]
    interrupted = events[-1]
    assert isinstance(interrupted, InterruptedEvent)
    assert interrupted.retryable is False
    assert "authority" not in interrupted.message.lower()
    current = harness.uow.get("req-1")
    assert current is not None and current.state.kind == "received"
    assert scheduler.started == []
    assert harness.source.calls == []


@pytest.mark.parametrize(
    ("requires_approval", "mode"),
    [(True, "full"), (False, "draft_only")],
)
def test_approval_or_draft_discards_all_tokens_and_returns_bodyless_pending(
    requires_approval: bool,
    mode: AnswerMode,
) -> None:
    source = _Source(
        BufferedAnswer(
            candidate=AnswerCandidate(text="승인 전 비공개 초안", mode=mode),
            tokens=("승인 전 ", "비공개 초안"),
        )
    )
    harness = _build(
        source=source,
        requires_approval=requires_approval,
        scheduler=_ImmediateScheduler(),
    )

    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert [type(event) for event in events] == [AcceptedEvent, PendingEvent]
    pending = events[-1]
    assert isinstance(pending, PendingEvent)
    assert pending.state == "awaiting_approval"
    assert "초안" not in pending.message
    assert harness.uow.by_request("req-1") is None


def test_completion_is_exact_read_before_buffered_tokens_can_be_published() -> None:
    tracing: _TracingReader | None = None

    def make_reader(reader: QuestionCompletionReader) -> QuestionCompletionReader:
        nonlocal tracing
        tracing = _TracingReader(reader)
        return tracing

    harness = _build(reader_factory=make_reader, scheduler=_ImmediateScheduler())
    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert tracing is not None
    # service가 token을 반환하기 전 exact-read하고, evidence-backed broker가 Done mint 전
    # 같은 Store를 다시 exact-read한다.
    assert tracing.calls == [("request", "req-1"), ("request", "req-1")]
    assert [type(event) for event in events[-4:]] == [
        TokenEvent,
        TokenEvent,
        TokenEvent,
        DoneEvent,
    ]
    done = events[-1]
    assert isinstance(done, DoneEvent) and done.record_id == "record-1"


def test_production_execution_rejects_reader_proxy_before_it_can_read() -> None:
    tracing: _TracingReader | None = None

    def make_reader(reader: QuestionCompletionReader) -> QuestionCompletionReader:
        nonlocal tracing
        tracing = _TracingReader(reader)
        return tracing

    with pytest.raises(QuestionCompletionStorageIdentityError):
        _build(reader_factory=make_reader, production_style=True)

    assert tracing is not None
    assert tracing.calls == []


def test_production_execution_rejects_same_ephemeral_uow() -> None:
    with pytest.raises(NonDurableWorkflowCompositionError):
        _build(production_style=True)


def test_commit_then_publish_failure_retries_done_without_replaying_tokens() -> None:
    broker_holder: dict[str, _FailFirstCompletionPublishBroker] = {}

    def broker_factory(
        requests: QuestionRequestStore,
        reader: QuestionCompletionReader,
    ) -> InMemoryQuestionStreamBroker:
        broker = _FailFirstCompletionPublishBroker(
            requests=requests,
            completions=reader,
        )
        broker_holder["value"] = broker
        return broker

    harness = _build(
        scheduler=_ImmediateScheduler(),
        broker_factory=broker_factory,
    )

    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert [type(event) for event in events] == [
        AcceptedEvent,
        TokenEvent,
        TokenEvent,
        TokenEvent,
        DoneEvent,
    ]
    assert broker_holder["value"].completion_publish_calls == 2
    assert not any(isinstance(event, InterruptedEvent) for event in events)
    assert harness.uow.by_request("req-1") is not None


def test_finalization_fault_publishes_no_token_or_done_and_preserves_ready_request() -> None:
    def fail(point: CompletionFaultPoint) -> None:
        if point == "before_commit":
            raise RuntimeError("injected")

    harness = _build(fault_injector=fail, scheduler=_ImmediateScheduler())
    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert [type(event) for event in events] == [AcceptedEvent, InterruptedEvent]
    current = harness.uow.get("req-1")
    assert current is not None and isinstance(current.state, ReadyToDispatch)
    assert harness.uow.by_request("req-1") is None


def test_scheduler_submission_failure_keeps_request_and_emits_neutral_interrupted() -> None:
    harness = _build(scheduler=_FailingScheduler())

    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert [type(event) for event in events] == [AcceptedEvent, InterruptedEvent]
    interrupted = events[-1]
    assert isinstance(interrupted, InterruptedEvent) and interrupted.retryable is True
    assert "submit" not in interrupted.message
    current = harness.uow.get("req-1")
    assert current is not None and isinstance(current.state, ReadyToDispatch)


def test_scheduler_capacity_rejection_is_failclosed_and_never_calls_source() -> None:
    harness = _build(scheduler=_CapacityScheduler())

    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert [type(event) for event in events] == [AcceptedEvent, InterruptedEvent]
    assert harness.source.calls == []
    current = harness.uow.get("req-1")
    assert current is not None and isinstance(current.state, ReadyToDispatch)


def test_disconnect_does_not_cancel_background_completion() -> None:
    entered = Event()
    release = Event()

    def block(_: QuestionRequest) -> None:
        entered.set()
        assert release.wait(timeout=3)

    source = _Source(
        BufferedAnswer(candidate=AnswerCandidate(text="완료"), tokens=("완료",)),
        callback=block,
    )
    scheduler = ThreadedQuestionProducerScheduler(max_workers=1)
    harness = _build(source=source, scheduler=scheduler)
    opened = harness.application.open_stream(_command())
    assert opened.subscription.get(timeout=1) == AcceptedEvent(request_id="req-1")
    assert entered.wait(timeout=2)

    opened.subscription.close()
    release.set()
    scheduler.shutdown(wait=True)

    bundle = harness.uow.by_request("req-1")
    assert bundle is not None and bundle.completion.text == "완료"


@pytest.mark.parametrize(
    ("surface", "expected_action"),
    [("ask", "question.read"), ("open_stream", "question.stream")],
)
def test_intake_surface는_결과_action을_resolution에_명시한다(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    expected_action: str,
) -> None:
    harness = _build(scheduler=_ImmediateScheduler())
    original = harness.resolution.ask
    actions: list[object] = []

    def recording_ask(
        command: AskQuestion,
        *,
        result_action: Literal["question.read", "question.stream"] | None = None,
    ) -> object:
        actions.append(result_action)
        return original(command, result_action=result_action)

    monkeypatch.setattr(harness.resolution, "ask", recording_ask)

    if surface == "ask":
        harness.application.ask(_command())
    else:
        opened = harness.application.open_stream(_command())
        opened.subscription.close()

    assert actions == [expected_action]


def test_late_terminal_subscriber_gets_same_done_without_token_replay() -> None:
    harness = _build(scheduler=_ImmediateScheduler())
    first = harness.application.open_stream(_command())
    first_events = _events_until_end(first.subscription)
    first_done = first_events[-1]
    assert isinstance(first_done, DoneEvent)

    late = harness.application.subscribe("req-1", _command().principal)
    late_event = late.get(timeout=1)

    assert late_event == first_done
    assert late.get(timeout=0) is None


def test_other_principal_is_hidden_by_field_free_stream_not_found() -> None:
    harness = _build(scheduler=_ImmediateScheduler())
    harness.application.open_stream(_command())

    with pytest.raises(QuestionStreamRequestNotFoundError) as caught:
        harness.application.subscribe(
            "req-1",
            RequesterPrincipal(org_id="org-1", subject_id="other-user"),
        )

    assert vars(caught.value) == {}
    assert "req-1" not in str(caught.value)
    assert harness.broker.subscriber_count("req-1") == 1


def test_source_reentry_is_single_claim_and_exception_only_interrupts_nonterminal_request() -> None:
    scheduler = ThreadedQuestionProducerScheduler(max_workers=2)
    reentry: list[ProducerStartDisposition] = []

    def callback(request: QuestionRequest) -> None:
        reentry.append(scheduler.ensure_started(request.request_id, lambda: pytest.fail("reentry")))

    source = _Source(
        BufferedAnswer(candidate=AnswerCandidate(text="미사용"), tokens=("미사용",)),
        callback=callback,
        error=RuntimeError("runtime failed"),
    )
    harness = _build(source=source, scheduler=scheduler)
    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)
    scheduler.shutdown(wait=True)

    assert len(reentry) == 1 and isinstance(reentry[0], ProducerAlreadyRunning)
    assert [type(event) for event in events] == [AcceptedEvent, InterruptedEvent]
    current = harness.uow.get("req-1")
    assert current is not None and isinstance(current.state, ReadyToDispatch)
    assert harness.uow.by_request("req-1") is None


def test_ready_request_can_retry_on_authorized_resubscribe_after_interruption() -> None:
    source = _Source(
        BufferedAnswer(candidate=AnswerCandidate(text="재시도 성공"), tokens=("재시도 ", "성공")),
        error=RuntimeError("first attempt"),
    )
    harness = _build(source=source, scheduler=_ImmediateScheduler())
    first = harness.application.open_stream(_command())
    assert [type(event) for event in _events_until_end(first.subscription)] == [
        AcceptedEvent,
        InterruptedEvent,
    ]
    source.error = None

    retry = harness.application.subscribe("req-1", _command().principal)
    retry_events = _events_until_end(retry)

    assert [type(event) for event in retry_events] == [TokenEvent, TokenEvent, DoneEvent]
    assert len(source.calls) == 2


def test_source_cannot_reenter_execution_service_and_leave_partial_state() -> None:
    source = _Source(BufferedAnswer(candidate=AnswerCandidate(text="미사용"), tokens=("미사용",)))
    harness = _build(source=source, scheduler=_ImmediateScheduler())

    def reenter(request: QuestionRequest) -> None:
        harness.execution.execute(request.request_id)

    source.callback = reenter

    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert [type(event) for event in events] == [AcceptedEvent, InterruptedEvent]
    current = harness.uow.get("req-1")
    assert current is not None and isinstance(current.state, ReadyToDispatch)
    assert harness.uow.by_request("req-1") is None


def test_preview_byte_limit_drops_only_tokens_after_successful_commit() -> None:
    harness = _build(scheduler=_ImmediateScheduler(), max_preview_bytes=2)

    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert [type(event) for event in events] == [AcceptedEvent, DoneEvent]
    assert harness.uow.by_request("req-1") is not None


class _MissingReader:
    def by_request(self, request_id: str) -> CompletionBundle | None:
        return None

    def by_record(self, record_id: str) -> CompletionBundle | None:
        return None


class _BlockAfterInterruptedBroker(InMemoryQuestionStreamBroker):
    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        completions: QuestionCompletionReader,
    ) -> None:
        super().__init__(
            max_queue_size=16,
            requests=requests,
            completions=completions,
        )
        self.interrupted_published = Event()
        self.release_publish = Event()

    def publish(self, event: QuestionStreamEvent) -> int:
        delivered = super().publish(event)
        if isinstance(event, InterruptedEvent):
            self.interrupted_published.set()
            assert self.release_publish.wait(timeout=3)
        return delivered


def test_answered_without_exact_completion_bundle_fails_closed_before_token_or_done() -> None:
    harness = _build(
        reader_factory=lambda _: _MissingReader(),
        scheduler=_ImmediateScheduler(),
    )
    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert [type(event) for event in events] == [AcceptedEvent, InterruptedEvent]
    with pytest.raises(QuestionStreamCompletionMismatchError):
        harness.execution.snapshot("req-1")


def test_late_subscribe_during_interrupted_claim_gets_current_pending_instead_of_hanging() -> None:
    source = _Source(
        BufferedAnswer(candidate=AnswerCandidate(text="미사용"), tokens=("미사용",)),
        error=RuntimeError("runtime failed"),
    )
    scheduler = ThreadedQuestionProducerScheduler(max_workers=1)
    broker_holder: dict[str, _BlockAfterInterruptedBroker] = {}

    def broker_factory(
        requests: QuestionRequestStore,
        reader: QuestionCompletionReader,
    ) -> InMemoryQuestionStreamBroker:
        broker = _BlockAfterInterruptedBroker(
            requests=requests,
            completions=reader,
        )
        broker_holder["value"] = broker
        return broker

    harness = _build(
        source=source,
        scheduler=scheduler,
        broker_factory=broker_factory,
    )
    broker = broker_holder["value"]
    try:
        first = harness.application.open_stream(_command())
        assert first.subscription.get(timeout=1) == AcceptedEvent(request_id="req-1")
        assert broker.interrupted_published.wait(timeout=2)
        assert scheduler.is_running("req-1")

        late = harness.application.subscribe("req-1", _command().principal)
        late_event = late.get(timeout=1)

        assert isinstance(late_event, PendingEvent)
        assert late_event.state == "ready_to_dispatch"
    finally:
        broker.release_publish.set()
        scheduler.shutdown(wait=True)


def test_contested_open_stream_never_starts_scheduler_or_source_and_returns_pending() -> None:
    scheduler = _ImmediateScheduler()
    harness = _build(
        decision=Contested(
            candidates=(_card("refund-card"), _card("billing-card")),
            intent="refund",
        ),
        scheduler=scheduler,
    )

    opened = harness.application.open_stream(_command())
    events = _events_until_end(opened.subscription)

    assert [type(event) for event in events] == [AcceptedEvent, PendingEvent]
    assert scheduler.started == []
    assert harness.source.calls == []


def test_execution_result_is_a_sealed_buffered_or_stable_value() -> None:
    harness = _build(scheduler=_ImmediateScheduler())
    outcome = harness.resolution.ask(_command())
    assert outcome.request_id == "req-1"

    result = harness.execution.execute("req-1")

    assert isinstance(result, BufferedCompletion)
    assert result.bundle.completion.request_id == "req-1"
    assert result.tokens
    replay = harness.execution.execute("req-1")
    assert isinstance(replay, BufferedCompletion)
    assert replay.tokens == ()
    assert replay.bundle == result.bundle
    assert len(harness.source.calls) == 1

    pending_harness = _build(
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=_ImmediateScheduler(),
    )
    pending_harness.resolution.ask(_command())
    stable = pending_harness.execution.snapshot("req-1")
    assert isinstance(stable, StableStreamResult)
    assert isinstance(stable.end, PendingEvent)


def test_execute_advances_a_stored_received_once_then_uses_the_saved_route() -> None:
    harness = _build(scheduler=_ImmediateScheduler())
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="환불은 언제 되나요?",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )
    harness.uow.create(received)

    result = harness.execution.execute("req-1")

    assert isinstance(result, BufferedCompletion)
    assert len(harness.source.calls) == 1
    assert harness.source.calls[0].request_id == "req-1"
    assert isinstance(harness.source.calls[0].state, ReadyToDispatch)


@pytest.mark.parametrize("terminal", ["declined", "failed"])
def test_terminal_request_is_reprojected_exactly_without_source_execution(terminal: str) -> None:
    harness = _build(
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=_ImmediateScheduler(),
    )
    harness.resolution.ask(_command())
    current = harness.uow.get("req-1")
    assert current is not None
    target = (
        DeclinedRequest(reason_code="owner_declined")
        if terminal == "declined"
        else FailedRequest(error_code="runtime_exhausted")
    )
    updated = current.transition(target, clock=lambda: NOW + timedelta(minutes=1))
    assert harness.uow.compare_and_set("req-1", current.revision, current, updated)

    result = harness.execution.execute("req-1")

    assert isinstance(result, StableStreamResult)
    if terminal == "declined":
        assert result.end == DeclinedEvent(
            request_id="req-1",
            reason_code="owner_declined",
            message="질문 처리가 거절되었습니다.",
        )
    else:
        assert result.end == FailedEvent(
            request_id="req-1",
            error_code="runtime_exhausted",
            message="질문을 처리하지 못했습니다.",
        )
    assert harness.source.calls == []


def test_application_lookup_projects_exact_completion_without_internal_routing_fields() -> None:
    harness = _build(scheduler=_ImmediateScheduler())
    opened = harness.application.open_stream(_command())
    _events_until_end(opened.subscription)

    result = harness.application.lookup("req-1", _command().principal)

    assert result == AnsweredQuestionLookup(
        answer_text="영업일 3일 안에 처리됩니다.",
        request_id="req-1",
        record_id="record-1",
        mode="full",
        sources=("refund-policy.md",),
        review_status="not_required",
        answered_by="owner-1",
        agent_id="refund-card",
    )
    assert set(result.model_dump()) == {
        "answer_text",
        "request_id",
        "record_id",
        "mode",
        "sources",
        "review_status",
        "answered_by",
        "agent_id",
    }


def test_application_ask_executes_only_ready_request_and_reuses_lookup_projection() -> None:
    harness = _build(scheduler=_ImmediateScheduler())
    application, execution = _application_with_tracing_execution(harness)

    asked = application.ask(_command())

    assert asked == AnsweredQuestionLookup(
        answer_text="영업일 3일 안에 처리됩니다.",
        request_id="req-1",
        record_id="record-1",
        mode="full",
        sources=("refund-policy.md",),
        review_status="not_required",
        answered_by="owner-1",
        agent_id="refund-card",
    )
    assert execution.execute_calls == ["req-1"]
    assert execution.snapshot_calls == []
    assert len(harness.source.calls) == 1
    assert harness.uow.by_request("req-1") is not None

    assert application.lookup("req-1", _command().principal) == asked
    assert execution.execute_calls == ["req-1"]
    assert execution.snapshot_calls == ["req-1"]
    assert len(harness.source.calls) == 1


@pytest.mark.parametrize(
    ("decision", "expected_kind", "expected_state"),
    [
        (
            Contested(
                candidates=(_card("refund-card"), _card("billing-card")),
                intent="refund",
            ),
            "contested",
            "awaiting_conflict",
        ),
        (
            Unowned(intent="refund", escalated_to="root-manager"),
            "unowned",
            "awaiting_manager",
        ),
    ],
)
def test_application_ask_snapshots_nonrouted_disposition_without_execution(
    decision: Contested | Unowned,
    expected_kind: str,
    expected_state: str,
) -> None:
    harness = _build(decision=decision, scheduler=_ImmediateScheduler())
    application, execution = _application_with_tracing_execution(harness)

    result = application.ask(_command())

    assert isinstance(result, PendingQuestionLookup)
    assert result.request_id == "req-1"
    assert result.kind == expected_kind
    assert result.state == expected_state
    assert result.retryable is False
    assert execution.execute_calls == []
    assert execution.snapshot_calls == ["req-1"]
    assert harness.source.calls == []
    assert harness.uow.by_request("req-1") is None


def test_application_ask_projects_approval_pending_as_routed_without_answer_body() -> None:
    harness = _build(
        requires_approval=True,
        scheduler=_ImmediateScheduler(),
    )
    application, execution = _application_with_tracing_execution(harness)

    result = application.ask(_command())

    assert result == PendingQuestionLookup(
        request_id="req-1",
        kind="routed",
        state="awaiting_approval",
        retryable=False,
        message="질문을 처리하고 있습니다.",
    )
    assert execution.execute_calls == ["req-1"]
    assert execution.snapshot_calls == []
    assert len(harness.source.calls) == 1
    assert harness.uow.by_request("req-1") is None


def test_application_ask_normalizes_nonretryable_initial_routing_error() -> None:
    harness = _build(
        route_authority=_DenyAuthority(),
        scheduler=_ImmediateScheduler(),
    )
    application, execution = _application_with_tracing_execution(harness)

    with pytest.raises(QuestionSurfaceInterruptedError) as caught:
        application.ask(_command())

    assert caught.value.request_id == "req-1"
    assert caught.value.code == "route_authority_denied"
    assert caught.value.retryable is False
    assert "authority" not in str(caught.value).lower()
    current = harness.uow.get("req-1")
    assert current is not None and current.state.kind == "received"
    assert execution.execute_calls == []
    assert execution.snapshot_calls == []
    assert harness.source.calls == []

    result = application.lookup("req-1", _command().principal)

    assert result == PendingQuestionLookup(
        request_id="req-1",
        kind="routing",
        state="received",
        retryable=True,
        message="질문을 처리하고 있습니다.",
    )
    assert execution.execute_calls == []
    assert execution.snapshot_calls == ["req-1"]


def test_application_ask_normalizes_retryable_dependency_error_without_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(scheduler=_ImmediateScheduler())

    def store_then_interrupt(
        command: AskQuestion,
        *,
        result_action: Literal["question.read", "question.stream"] | None = None,
    ) -> object:
        assert result_action == "question.read"
        received = QuestionRequest.receive(
            org_id=command.principal.org_id,
            requester_id=command.principal.subject_id,
            question=command.question,
            request_id_factory=lambda: "req-1",
            clock=lambda: NOW,
            due_at=NOW + timedelta(hours=1),
            session_id=command.session_id,
            context_snapshot=command.context_snapshot,
        )
        harness.uow.create(received)
        raise InitialRoutingDependencyError("req-1", "router")

    monkeypatch.setattr(harness.resolution, "ask", store_then_interrupt)
    application, execution = _application_with_tracing_execution(harness)

    with pytest.raises(QuestionSurfaceInterruptedError) as caught:
        application.ask(_command())

    assert caught.value.request_id == "req-1"
    assert caught.value.code == "router_unavailable"
    assert caught.value.retryable is True
    assert "router" not in str(caught.value).lower()
    current = harness.uow.get("req-1")
    assert current is not None and current.state.kind == "received"
    assert execution.execute_calls == []
    assert execution.snapshot_calls == []
    assert harness.source.calls == []

    result = application.lookup("req-1", _command().principal)

    assert isinstance(result, PendingQuestionLookup)
    assert result.kind == "routing"
    assert result.state == "received"
    assert result.retryable is True
    assert execution.snapshot_calls == ["req-1"]


def test_application_ask_preserves_request_id_on_ready_execution_failure() -> None:
    source = _Source(
        BufferedAnswer(candidate=AnswerCandidate(text="미사용"), tokens=("미사용",)),
        error=RuntimeError("provider unavailable"),
    )
    harness = _build(source=source, scheduler=_ImmediateScheduler())
    application, execution = _application_with_tracing_execution(harness)

    with pytest.raises(QuestionSurfaceInterruptedError) as caught:
        application.ask(_command())

    assert caught.value.request_id == "req-1"
    assert caught.value.code == "answer_source_failed"
    assert caught.value.retryable is True
    assert "provider" not in str(caught.value)

    current = harness.uow.get("req-1")
    assert current is not None and isinstance(current.state, ReadyToDispatch)
    assert current.revision == 1
    assert execution.execute_calls == ["req-1"]
    assert execution.snapshot_calls == []
    assert len(source.calls) == 1
    assert harness.uow.by_request("req-1") is None

    pending = application.lookup("req-1", _command().principal)
    assert isinstance(pending, PendingQuestionLookup)
    assert pending.request_id == "req-1"
    assert pending.kind == "routed"
    assert pending.state == "ready_to_dispatch"


@pytest.mark.parametrize("terminal", ["declined", "failed"])
def test_application_ask_snapshots_terminal_intake_result_without_execution(
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    harness = _build(
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=_ImmediateScheduler(),
    )
    original_ask = harness.resolution.ask

    def ask_then_close(
        command: AskQuestion,
        *,
        result_action: Literal["question.read", "question.stream"] | None = None,
    ) -> object:
        assert result_action == "question.read"
        original_ask(command, result_action=result_action)
        current = harness.uow.get("req-1")
        assert current is not None
        target = (
            DeclinedRequest(reason_code="owner_declined")
            if terminal == "declined"
            else FailedRequest(error_code="runtime_exhausted")
        )
        updated = current.transition(target, clock=lambda: NOW + timedelta(minutes=1))
        assert harness.uow.compare_and_set("req-1", current.revision, current, updated)
        return harness.resolution.retrieve("req-1", command.principal)

    monkeypatch.setattr(harness.resolution, "ask", ask_then_close)
    application, execution = _application_with_tracing_execution(harness)

    result = application.ask(_command())

    if terminal == "declined":
        assert result == DeclinedQuestionLookup(
            request_id="req-1",
            reason_code="owner_declined",
            message="질문 처리가 거절되었습니다.",
        )
    else:
        assert result == FailedQuestionLookup(
            request_id="req-1",
            error_code="runtime_exhausted",
            message="질문을 처리하지 못했습니다.",
        )
    assert execution.execute_calls == []
    assert execution.snapshot_calls == ["req-1"]
    assert harness.source.calls == []


def test_application_ask_rechecks_principal_on_execution_result() -> None:
    visible = _build(scheduler=_ImmediateScheduler())
    foreign = _build(scheduler=_ImmediateScheduler())
    foreign_command = AskQuestion(
        principal=RequesterPrincipal(org_id="foreign-org", subject_id="foreign-user"),
        question="외부 질문",
    )
    foreign.resolution.ask(foreign_command)
    completed = foreign.execution.execute("req-1")
    assert isinstance(completed, BufferedCompletion)
    execution = _TracingExecution(foreign.execution)
    application = QuestionStreamApplication(
        resolution=visible.resolution,
        execution=execution,
        broker=visible.broker,
        scheduler=visible.scheduler,
    )

    with pytest.raises(QuestionStreamRequestNotFoundError) as caught:
        application.ask(_command())

    assert str(caught.value) == "질문 요청을 찾을 수 없습니다."
    assert execution.execute_calls == ["req-1"]
    assert execution.snapshot_calls == []
    assert visible.source.calls == []


@pytest.mark.parametrize(
    ("public_kind", "expected_kind"),
    [("contested", "contested"), ("dispatched", "routed")],
)
def test_application_lookup_maps_manager_public_kind_from_request_state(
    public_kind: Literal["contested", "dispatched"],
    expected_kind: Literal["contested", "routed"],
) -> None:
    harness = _build(
        decision=(
            Contested(
                candidates=(_card("refund-card"), _card("billing-card")),
                intent="refund",
            )
            if public_kind == "contested"
            else None
        ),
        scheduler=_ImmediateScheduler(),
    )
    harness.resolution.ask(_command())
    current = harness.uow.get("req-1")
    assert current is not None
    if public_kind == "dispatched":
        assert isinstance(current.state, ReadyToDispatch)
        awaiting_answer = current.transition(
            AwaitingAnswer(
                route=current.state.route,
                attempt=current.state.attempt,
                ticket_id="ticket-1",
                handling=HandlingAssignment(
                    kind="runtime_ticket",
                    ref="ticket-1",
                    due_at=NOW + timedelta(hours=1),
                ),
            ),
            clock=lambda: NOW + timedelta(seconds=1),
        )
        assert harness.uow.compare_and_set(
            "req-1",
            current.revision,
            current,
            awaiting_answer,
        )
        current = awaiting_answer
    manager = current.transition(
        AwaitingManager(
            item_id="manager-1",
            public_kind=public_kind,
            route=current.state.route if isinstance(current.state, AwaitingAnswer) else None,
            attempt=current.state.attempt if isinstance(current.state, AwaitingAnswer) else None,
            handling=HandlingAssignment(
                kind="manager_item",
                ref="manager-1",
                due_at=NOW + timedelta(hours=2),
            ),
        ),
        clock=lambda: NOW + timedelta(seconds=2),
    )
    assert harness.uow.compare_and_set(
        "req-1",
        current.revision,
        current,
        manager,
    )

    result = harness.application.lookup("req-1", _command().principal)

    assert isinstance(result, PendingQuestionLookup)
    assert result.kind == expected_kind
    assert result.state == "awaiting_manager"
    assert harness.source.calls == []


def test_application_lookup_projects_each_request_state_without_source_execution() -> None:
    harness = _build(
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=_ImmediateScheduler(),
    )
    harness.resolution.ask(_command())

    pending = harness.application.lookup("req-1", _command().principal)

    assert pending == PendingQuestionLookup(
        request_id="req-1",
        kind="unowned",
        state="awaiting_manager",
        retryable=False,
        message="질문을 처리하고 있습니다.",
    )
    assert harness.source.calls == []

    current = harness.uow.get("req-1")
    assert current is not None
    declined_request = current.transition(
        DeclinedRequest(reason_code="owner_declined"),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    assert harness.uow.compare_and_set(
        "req-1",
        current.revision,
        current,
        declined_request,
    )
    assert harness.application.lookup("req-1", _command().principal) == DeclinedQuestionLookup(
        request_id="req-1",
        reason_code="owner_declined",
        message="질문 처리가 거절되었습니다.",
    )

    failed_harness = _build(
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=_ImmediateScheduler(),
    )
    failed_harness.resolution.ask(_command())
    failed_current = failed_harness.uow.get("req-1")
    assert failed_current is not None
    failed_request = failed_current.transition(
        FailedRequest(error_code="runtime_exhausted"),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    assert failed_harness.uow.compare_and_set(
        "req-1",
        failed_current.revision,
        failed_current,
        failed_request,
    )
    assert failed_harness.application.lookup("req-1", _command().principal) == FailedQuestionLookup(
        request_id="req-1",
        error_code="runtime_exhausted",
        message="질문을 처리하지 못했습니다.",
    )


def test_application_lookup_hides_missing_and_other_requester_with_same_error() -> None:
    harness = _build(
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=_ImmediateScheduler(),
    )
    harness.resolution.ask(_command())
    other = RequesterPrincipal(org_id="org-1", subject_id="other-user")

    with pytest.raises(QuestionStreamRequestNotFoundError) as missing:
        harness.application.lookup("missing", _command().principal)
    with pytest.raises(QuestionStreamRequestNotFoundError) as forbidden:
        harness.application.lookup("req-1", other)

    assert str(missing.value) == str(forbidden.value)


@pytest.mark.parametrize("foreign_state", ["pending", "answered"])
def test_application_lookup_rechecks_owner_after_snapshot_to_close_toctou(
    foreign_state: str,
) -> None:
    visible = _build(scheduler=_ImmediateScheduler())
    visible.resolution.ask(_command())
    foreign = _build(
        decision=(
            Unowned(intent="refund", escalated_to="root-manager")
            if foreign_state == "pending"
            else None
        ),
        scheduler=_ImmediateScheduler(),
    )
    foreign_command = AskQuestion(
        principal=RequesterPrincipal(org_id="foreign-org", subject_id="foreign-user"),
        question="외부 질문",
    )
    foreign.resolution.ask(foreign_command)
    if foreign_state == "answered":
        completed = foreign.execution.execute("req-1")
        assert isinstance(completed, BufferedCompletion)

    mixed = QuestionStreamApplication(
        resolution=visible.resolution,
        execution=foreign.execution,
        broker=visible.broker,
        scheduler=_ImmediateScheduler(),
    )

    with pytest.raises(QuestionStreamRequestNotFoundError) as caught:
        mixed.lookup("req-1", _command().principal)

    assert str(caught.value) == "질문 요청을 찾을 수 없습니다."


@pytest.mark.parametrize("operation", ["open", "subscribe"])
def test_broker_capacity_after_request_exists_preserves_canonical_id(
    operation: str,
) -> None:
    broker_holder: dict[str, InMemoryQuestionStreamBroker] = {}

    def broker_factory(
        requests: QuestionRequestStore,
        completions: QuestionCompletionReader,
    ) -> InMemoryQuestionStreamBroker:
        broker = InMemoryQuestionStreamBroker(
            max_queue_size=8,
            max_topics=1,
            requests=requests,
            completions=completions,
        )
        broker_holder["value"] = broker
        return broker

    harness = _build(
        scheduler=_ImmediateScheduler(),
        broker_factory=broker_factory,
    )
    if operation == "subscribe":
        outcome = harness.resolution.ask(_command())
        assert outcome.request_id == "req-1"
    occupied = broker_holder["value"].subscribe("occupied")
    try:
        with pytest.raises(QuestionStreamUnavailableError) as caught:
            if operation == "open":
                harness.application.open_stream(_command())
            else:
                harness.application.subscribe("req-1", _command().principal)

        assert caught.value.request_id == "req-1"
        assert harness.uow.get("req-1") is not None
        assert harness.source.calls == []
    finally:
        occupied.close()


def test_manager_wake_reuses_stream_scheduler_and_exact_execution_path() -> None:
    scheduler = _ImmediateScheduler()
    harness = _build(
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=scheduler,
    )
    harness.resolution.ask(_command())
    current = harness.uow.get("req-1")
    assert current is not None
    trigger_key = "request-dispatch:req-1:1"
    ready = current.transition(
        ReadyToDispatch(
            route=RouteTarget(
                intent="refund",
                agent_id="refund-card",
                requires_approval=False,
                authority_version="route-v1",
            ),
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert harness.uow.compare_and_set("req-1", current.revision, current, ready)

    wake = harness.application.ensure_started("req-1")

    assert wake == ExecutionStarted()
    assert scheduler.started == ["req-1"]
    assert len(harness.source.calls) == 1
    assert isinstance(
        harness.application.lookup("req-1", _command().principal), AnsweredQuestionLookup
    )
    assert harness.application.ensure_started("req-1") == ExecutionNotNeeded()


def test_manager_wake_maps_scheduler_capacity_to_retryable_deferred() -> None:
    harness = _build(
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=_CapacityScheduler(),
    )
    harness.resolution.ask(_command())
    current = harness.uow.get("req-1")
    assert current is not None
    trigger_key = "request-dispatch:req-1:1"
    ready = current.transition(
        ReadyToDispatch(
            route=RouteTarget(
                intent="refund",
                agent_id="refund-card",
                requires_approval=False,
                authority_version="route-v1",
            ),
            attempt=1,
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert harness.uow.compare_and_set("req-1", current.revision, current, ready)

    assert harness.application.ensure_started("req-1") == ExecutionDeferred(
        reason_code="producer_capacity_exceeded"
    )
    assert harness.source.calls == []


def test_manager_terminal_publish_delivers_to_active_and_late_subscribers() -> None:
    harness = _build(
        decision=Unowned(intent="refund", escalated_to="root-manager"),
        scheduler=_ImmediateScheduler(),
    )
    harness.resolution.ask(_command())
    active = harness.application.subscribe("req-1", _command().principal)
    current = harness.uow.get("req-1")
    assert current is not None
    declined = current.transition(
        DeclinedRequest(reason_code="manager_declined"),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert harness.uow.compare_and_set("req-1", current.revision, current, declined)

    assert harness.application.publish_terminal("req-1") == TerminalPublished()
    assert active.get(timeout=1) == DeclinedEvent(
        request_id="req-1",
        reason_code="manager_declined",
        message="질문 처리가 거절되었습니다.",
    )
    assert harness.application.publish_terminal("req-1") == TerminalAlreadyPublished()
    late = harness.application.subscribe("req-1", _command().principal)
    assert late.get(timeout=1) == DeclinedEvent(
        request_id="req-1",
        reason_code="manager_declined",
        message="질문 처리가 거절되었습니다.",
    )
