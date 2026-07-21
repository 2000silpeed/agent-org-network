from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Never, cast

import pytest
from pydantic import TypeAdapter

from agent_org_network.answer_finalization import (
    CompletionBundle,
    QuestionCompletionReader,
    QuestionCompletionUnitOfWork,
)
from agent_org_network.approval import AnswerCandidate, ApprovalBoundary
from agent_org_network.grounding_terminal_failure import (
    GroundingTerminalFailureCode,
    GroundingTerminalFailureRecorder,
    GroundingTerminalFailureRequested,
    QuestionRequestGroundingTerminalFailureRecorder,
)
from agent_org_network.question_request import (
    FailedRequest,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.question_resolution import (
    AskQuestion,
    QuestionResolutionApplication,
    RequestFailed,
    RequestNotFound,
    RequestPending,
    RequesterPrincipal,
)
from agent_org_network.question_stream import (
    AcceptedEvent,
    FailedEvent,
    InMemoryQuestionStreamBroker,
    InterruptedEvent,
)
from agent_org_network.question_stream_execution import (
    BufferedAnswer,
    FailedQuestionLookup,
    InvalidBufferedAnswerError,
    ProducerStartDisposition,
    ProducerStarted,
    QuestionAnswerSource,
    QuestionAnswerSourceResult,
    QuestionProducerScheduler,
    QuestionStreamApplication,
    QuestionStreamExecutionService,
    QuestionSurfaceInterruptedError,
)


NOW = datetime(2026, 7, 13, 19, 0, tzinfo=timezone.utc)
PRINCIPAL = RequesterPrincipal(org_id="org-1", subject_id="requester-1")


def _command() -> AskQuestion:
    return AskQuestion(principal=PRINCIPAL, question="환불 기준은?")


def _stored_ready() -> tuple[InMemoryQuestionRequestStore, QuestionRequest]:
    received = QuestionRequest.receive(
        org_id=PRINCIPAL.org_id,
        requester_id=PRINCIPAL.subject_id,
        question="환불 기준은?",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=RouteTarget(
                intent="refund",
                agent_id="refund-card",
                requires_approval=False,
                authority_version="route-v1",
            ),
            attempt=1,
            trigger_key="request-dispatch:req-1:1",
            handling=HandlingAssignment(
                kind="system",
                ref="request-dispatch:req-1:1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW,
    )
    store = InMemoryQuestionRequestStore()
    store.create(received)
    assert store.compare_and_set("req-1", 0, received, ready)
    return store, ready


class _StoredResolution:
    def __init__(self, requests: QuestionRequestStore) -> None:
        self._requests = requests
        self.result_actions: list[Literal["question.read", "question.stream"] | None] = []

    def ask(
        self,
        command: AskQuestion,
        *,
        result_action: Literal["question.read", "question.stream"] | None = None,
    ) -> RequestPending | RequestFailed:
        assert command.principal == PRINCIPAL
        self.result_actions.append(result_action)
        return self._project()

    def retrieve(
        self,
        request_id: str,
        principal: RequesterPrincipal,
        *,
        action: Literal["question.read", "question.stream"] = "question.read",
    ) -> RequestPending | RequestFailed | RequestNotFound:
        del action
        if request_id != "req-1" or principal != PRINCIPAL:
            return RequestNotFound()
        return self._project()

    def advance(self, request_id: str, *, expected_revision: object) -> Never:
        raise AssertionError(
            f"stored Ready에서 advance를 호출하면 안 됩니다: {request_id}/{expected_revision}"
        )

    def _project(self) -> RequestPending | RequestFailed:
        current = self._requests.get("req-1")
        assert current is not None
        if isinstance(current.state, FailedRequest):
            return RequestFailed(
                request_id=current.request_id,
                error_code=current.state.error_code,
                message="질문을 처리하지 못했습니다.",
            )
        assert isinstance(current.state, ReadyToDispatch)
        return RequestPending(
            request_id=current.request_id,
            state=current.state.kind,
            retryable=True,
            message="질문을 처리하고 있습니다.",
        )


class _Source:
    def __init__(self, result: object, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[QuestionRequest] = []

    def answer(self, request: QuestionRequest) -> object:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class _NoCallApproval:
    def __init__(self) -> None:
        self.calls = 0

    def gate_candidate(self, *args: object, **kwargs: object) -> Never:
        self.calls += 1
        raise AssertionError("grounding terminal branch가 Approval을 호출했습니다.")


class _NoCallCompletion:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *args: object, **kwargs: object) -> Never:
        self.calls += 1
        raise AssertionError("grounding terminal branch가 Completion을 호출했습니다.")


class _NullReader:
    def by_request(self, request_id: str) -> CompletionBundle | None:
        return None

    def by_record(self, record_id: str) -> CompletionBundle | None:
        return None


class _ImmediateScheduler:
    def ensure_started(
        self,
        request_id: str,
        job: Callable[[], None],
    ) -> ProducerStartDisposition:
        job()
        return ProducerStarted()

    def is_running(self, request_id: str) -> bool:
        return False

    def shutdown(self, *, wait: bool = True) -> None:
        return None


class _CountingBroker(InMemoryQuestionStreamBroker):
    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        completions: QuestionCompletionReader,
    ) -> None:
        super().__init__(
            max_queue_size=8,
            requests=requests,
            completions=completions,
        )
        self.terminal_publish_calls = 0

    def publish_request_terminal(self, request_id: str, *, message: str) -> int:
        self.terminal_publish_calls += 1
        return super().publish_request_terminal(request_id, message=message)


class _GroundingReadInterrupted(RuntimeError):
    code = "grounding_read_interrupted"
    retryable = True


class _ForgingRecorder:
    """terminal 값을 반환하지만 authoritative Store에는 쓰지 않는다."""

    def __init__(self, requests: QuestionRequestStore) -> None:
        self._requests = requests

    def fail_if_ready(
        self,
        *,
        request_id: str,
        expected_revision: int,
        error_code: GroundingTerminalFailureCode,
    ) -> QuestionRequest:
        current = self._requests.get(request_id)
        assert current is not None and current.revision == expected_revision
        return current.transition(
            FailedRequest(error_code=error_code),
            clock=lambda: NOW + timedelta(minutes=1),
        )


RecorderFactory = Callable[
    [QuestionRequestStore],
    GroundingTerminalFailureRecorder,
]


@dataclass
class _Harness:
    requests: InMemoryQuestionRequestStore
    ready: QuestionRequest
    resolution: _StoredResolution
    source: _Source
    approval: _NoCallApproval
    completion: _NoCallCompletion
    broker: _CountingBroker
    execution: QuestionStreamExecutionService
    application: QuestionStreamApplication


def _build(
    result: object,
    *,
    source_error: Exception | None = None,
    with_recorder: bool = True,
    recorder_factory: RecorderFactory | None = None,
) -> _Harness:
    requests, ready = _stored_ready()
    resolution = _StoredResolution(requests)
    source = _Source(result, error=source_error)
    approval = _NoCallApproval()
    completion = _NoCallCompletion()
    reader = _NullReader()
    if not with_recorder:
        recorder = None
    elif recorder_factory is not None:
        recorder = recorder_factory(requests)
    else:
        recorder = QuestionRequestGroundingTerminalFailureRecorder(
            requests=requests,
            clock=lambda: NOW + timedelta(minutes=1),
        )
    execution = QuestionStreamExecutionService(
        requests=requests,
        resolution=cast(QuestionResolutionApplication, resolution),
        source=cast(QuestionAnswerSource, source),
        approval=cast(ApprovalBoundary, approval),
        completion=cast(QuestionCompletionUnitOfWork, completion),
        reader=cast(QuestionCompletionReader, reader),
        grounding_terminal_failure_recorder=recorder,
    )
    broker = _CountingBroker(
        requests=requests,
        completions=cast(QuestionCompletionReader, reader),
    )
    application = QuestionStreamApplication(
        resolution=cast(QuestionResolutionApplication, resolution),
        execution=execution,
        broker=broker,
        scheduler=cast(QuestionProducerScheduler, _ImmediateScheduler()),
    )
    return _Harness(
        requests=requests,
        ready=ready,
        resolution=resolution,
        source=source,
        approval=approval,
        completion=completion,
        broker=broker,
        execution=execution,
        application=application,
    )


def _failure_command(
    *,
    request_id: str = "req-1",
    expected_revision: int = 1,
    error_code: GroundingTerminalFailureCode = "required_grounding_missing",
) -> GroundingTerminalFailureRequested:
    return GroundingTerminalFailureRequested(
        request_id=request_id,
        expected_revision=expected_revision,
        error_code=error_code,
    )


def test_answer_source_result는_kind_discriminator로_두_결과를_구분한다() -> None:
    buffered = BufferedAnswer(
        candidate=AnswerCandidate(text="완성 답"),
        tokens=("완성 답",),
    )
    command = _failure_command()
    adapter: TypeAdapter[QuestionAnswerSourceResult] = TypeAdapter(QuestionAnswerSourceResult)

    assert buffered.kind == "buffered_answer"
    assert (
        adapter.validate_python(buffered.model_dump(mode="python", round_trip=True), strict=True)
        == buffered
    )
    assert (
        adapter.validate_python(command.model_dump(mode="python", round_trip=True), strict=True)
        == command
    )


def test_blocking_failure_command는_store_Failed를_그대로_조회하고_gate를_건너뛴다() -> None:
    harness = _build(_failure_command(error_code="required_grounding_invalid"))

    lookup = harness.application.ask(_command())

    assert isinstance(lookup, FailedQuestionLookup)
    assert lookup.error_code == "required_grounding_invalid"
    stored = harness.requests.get("req-1")
    assert stored is not None and isinstance(stored.state, FailedRequest)
    assert stored.revision == harness.ready.revision + 1
    assert harness.approval.calls == 0
    assert harness.completion.calls == 0


def test_active와_late_stream은_저장된_Failed_event로_같이_수렴한다() -> None:
    harness = _build(_failure_command())

    opened = harness.application.open_stream(_command())
    active_events = [
        opened.subscription.get(timeout=1),
        opened.subscription.get(timeout=1),
    ]
    late = harness.application.subscribe("req-1", PRINCIPAL)
    late_event = late.get(timeout=1)

    assert [type(event) for event in active_events] == [AcceptedEvent, FailedEvent]
    assert isinstance(active_events[-1], FailedEvent)
    assert active_events[-1].error_code == "required_grounding_missing"
    assert isinstance(late_event, FailedEvent)
    assert late_event == active_events[-1]
    assert harness.broker.terminal_publish_calls == 1
    assert harness.approval.calls == 0
    assert harness.completion.calls == 0


def test_stream_application은_접수와_stream_열기에_서로_다른_결과_action을_전달한다() -> None:
    harness = _build(_failure_command())

    harness.application.ask(_command())
    opened = harness.application.open_stream(_command())
    opened.subscription.close()

    assert harness.resolution.result_actions == [
        "question.read",
        "question.stream",
    ]


def test_reader_style_source_exception은_Ready를_유지하고_Interrupted만_발행한다() -> None:
    harness = _build(
        _failure_command(),
        source_error=_GroundingReadInterrupted("reader unavailable"),
    )

    opened = harness.application.open_stream(_command())
    events = [
        opened.subscription.get(timeout=1),
        opened.subscription.get(timeout=1),
    ]

    assert [type(event) for event in events] == [AcceptedEvent, InterruptedEvent]
    interrupted = events[-1]
    assert isinstance(interrupted, InterruptedEvent) and interrupted.retryable is True
    stored = harness.requests.get("req-1")
    assert stored == harness.ready
    assert harness.broker.terminal_publish_calls == 0
    assert harness.approval.calls == 0
    assert harness.completion.calls == 0


@pytest.mark.parametrize(
    "command",
    [
        _failure_command(request_id="forged-request"),
        _failure_command(expected_revision=2),
    ],
)
def test_current_Request와_exact_link되지_않은_failure_command는_nonretryable이다(
    command: GroundingTerminalFailureRequested,
) -> None:
    harness = _build(command)

    with pytest.raises(QuestionSurfaceInterruptedError) as caught:
        harness.application.ask(_command())

    assert caught.value.code == "invalid_grounding_terminal_failure"
    assert caught.value.retryable is False
    assert harness.requests.get("req-1") == harness.ready
    assert harness.approval.calls == 0
    assert harness.completion.calls == 0


def test_failure_command에_recorder가_없으면_nonretryable_configuration_failure다() -> None:
    harness = _build(_failure_command(), with_recorder=False)

    with pytest.raises(QuestionSurfaceInterruptedError) as caught:
        harness.application.ask(_command())

    assert caught.value.code == "grounding_terminal_failure_recorder_unavailable"
    assert caught.value.retryable is False
    assert harness.requests.get("req-1") == harness.ready


def test_source_result는_discriminated_union으로_strict_revalidate된다() -> None:
    forged = _failure_command().model_copy(update={"expected_revision": "1"})
    harness = _build(forged)

    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        with pytest.raises(InvalidBufferedAnswerError) as caught:
            harness.execution.execute("req-1")

    assert caught.value.code == "invalid_buffered_answer"
    assert caught.value.retryable is False
    assert harness.requests.get("req-1") == harness.ready


def test_recorder_반환은_execution_store의_exact_terminal_readback과_같아야_한다() -> None:
    harness = _build(
        _failure_command(),
        recorder_factory=lambda requests: cast(
            GroundingTerminalFailureRecorder,
            _ForgingRecorder(requests),
        ),
    )

    with pytest.raises(InvalidBufferedAnswerError) as caught:
        harness.execution.execute("req-1")

    assert caught.value.code == "invalid_grounding_terminal_failure_result"
    assert caught.value.retryable is False
    assert harness.requests.get("req-1") == harness.ready
    assert harness.approval.calls == 0
    assert harness.completion.calls == 0
