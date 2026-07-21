"""P17-native Question Stream의 실행 생산자와 application-lifetime scheduler.

HTTP generator와 Runtime 실행 수명을 분리한다. Runtime token은 Approval과 공통
Finalization의 exact-read가 끝날 때까지 이 모듈 안에서만 buffer된다.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from typing import Annotated, Literal, Protocol, TypeAlias, assert_never, final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from agent_org_network.answer_finalization import (
    AnswerCompletion,
    CompletionBundle,
    QuestionCompletionReader,
    QuestionCompletionUnitOfWork,
    canonical_completion_bundle,
)
from agent_org_network.approval import (
    ApprovalBoundary,
    ApprovalPending,
    AnswerCandidate,
)
from agent_org_network.grounding_terminal_failure import (
    GroundingTerminalFailureError,
    GroundingTerminalFailureRecorder,
    GroundingTerminalFailureRequested,
)
from agent_org_network.p17_manager_disposition import (
    ExecutionAlreadyRunning,
    ExecutionDeferred,
    ExecutionNotNeeded,
    ExecutionStarted,
    ExecutionWake,
    TerminalAlreadyPublished,
    TerminalDeferred,
    TerminalDelivery,
    TerminalPublished,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingApproval,
    DeclinedRequest,
    FailedRequest,
    QuestionPendingKind,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
    Received,
    RequestStateKind,
    question_pending_kind,
)
from agent_org_network.question_resolution import (
    AskQuestion,
    InitialRoutingError,
    QuestionPrincipal,
    QuestionResolutionApplication,
    RequestNotFound,
    RequestPending,
)
from agent_org_network.question_stream import (
    AcceptedEvent,
    DeclinedEvent,
    FailedEvent,
    InMemoryQuestionStreamBroker,
    InterruptedEvent,
    PendingEvent,
    QuestionStreamSubscription,
    StreamCapacityError,
    TokenEvent,
)
from agent_org_network.runtime import AnswerMode
from agent_org_network.storage_capability import validate_question_completion_storage


class QuestionStreamExecutionError(RuntimeError):
    """P17 stream execution 경계의 명시적 실패."""


class ProducerSubmissionError(QuestionStreamExecutionError):
    """scheduler가 claim 뒤 작업을 executor에 제출하지 못함."""


class QuestionStreamRequestNotFoundError(QuestionStreamExecutionError):
    """미존재와 requester 권한 위반을 같은 field-free 오류로 숨긴다."""

    def __init__(self) -> None:
        super().__init__("질문 요청을 찾을 수 없습니다.")


class QuestionStreamUnavailableError(QuestionStreamExecutionError):
    """Request는 존재하지만 bounded 전송 구독을 열 수 없음."""

    def __init__(self, request_id: str) -> None:
        self.request_id = _require_request_id(request_id)
        super().__init__("질문 스트림을 열 수 없습니다.")


class QuestionSurfaceInterruptedError(QuestionStreamExecutionError):
    """접수된 Request의 초기 진행 중단을 세부정보 없이 표면에 전달한다."""

    def __init__(self, *, request_id: str, code: str, retryable: bool) -> None:
        self.request_id = _require_request_id(request_id)
        if not code.strip():
            raise ValueError("중단 code는 nonblank 문자열이어야 합니다.")
        self.code = code
        self.retryable = retryable
        super().__init__("질문 처리를 시작하지 못했습니다.")


class InvalidBufferedAnswerError(QuestionStreamExecutionError):
    """QuestionAnswerSource 반환이 strict BufferedAnswer 계약을 위반함."""

    def __init__(self, *, request_id: str, code: str, retryable: bool) -> None:
        self.request_id = _require_request_id(request_id)
        if not code.strip():
            raise ValueError("Buffered Answer 오류 code는 nonblank여야 합니다.")
        self.code = code
        self.retryable = retryable
        super().__init__("Question Answer Source를 완료하지 못했습니다.")


class QuestionStreamCompletionMismatchError(QuestionStreamExecutionError):
    """AnsweredRequest와 canonical CompletionBundle이 exact-link되지 않음."""


class ReentrantQuestionExecutionError(QuestionStreamExecutionError):
    """같은 Request의 source callback·중복 실행 재진입을 거부함."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @field_validator("*", mode="after")
    @classmethod
    def _strings_must_be_nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("문자열 값은 비어 있거나 공백일 수 없습니다.")
        return value


@final
class BufferedAnswer(_FrozenModel):
    """Approval 전에 외부로 내보내지 않는 Runtime 완성 후보와 token buffer."""

    kind: Literal["buffered_answer"] = "buffered_answer"
    candidate: AnswerCandidate
    tokens: tuple[str, ...]

    @field_validator("tokens", mode="after")
    @classmethod
    def _tokens_must_not_contain_empty_values(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(token == "" for token in value):
            raise ValueError("BufferedAnswer token은 빈 문자열일 수 없습니다.")
        return value

    @model_validator(mode="after")
    def _tokens_must_reconstruct_candidate(self) -> BufferedAnswer:
        if "".join(self.tokens) != self.candidate.text:
            raise ValueError("BufferedAnswer token 연결값과 candidate.text가 다릅니다.")
        return self


QuestionAnswerSourceResult: TypeAlias = Annotated[
    BufferedAnswer | GroundingTerminalFailureRequested,
    Field(discriminator="kind"),
]
_QUESTION_ANSWER_SOURCE_RESULT_ADAPTER: TypeAdapter[QuestionAnswerSourceResult] = TypeAdapter(
    QuestionAnswerSourceResult
)


class QuestionAnswerSource(Protocol):
    """저장된 Question Request snapshot으로 Runtime 후보를 만드는 포트."""

    def answer(self, request: QuestionRequest) -> QuestionAnswerSourceResult: ...


StableEnd: TypeAlias = Annotated[
    PendingEvent | DeclinedEvent | FailedEvent,
    Field(discriminator="event_type"),
]


@final
class BufferedCompletion(_FrozenModel):
    """commit·exact-read 완료 뒤에만 공개 가능한 token preview와 증거 bundle."""

    tokens: tuple[str, ...]
    bundle: CompletionBundle

    @field_validator("tokens", mode="after")
    @classmethod
    def _tokens_must_not_contain_empty_values(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(token == "" for token in value):
            raise ValueError("BufferedCompletion token은 빈 문자열일 수 없습니다.")
        return value

    @model_validator(mode="after")
    def _tokens_must_match_committed_text(self) -> BufferedCompletion:
        if self.tokens and "".join(self.tokens) != self.bundle.completion.text:
            raise ValueError("공개 token 연결값과 committed answer text가 다릅니다.")
        return self


@final
class StableStreamResult(_FrozenModel):
    """Runtime 계산 없이 현재 Request snapshot에서 재구성한 안정 상태."""

    request: QuestionRequest
    end: StableEnd

    @model_validator(mode="after")
    def _event_must_match_request(self) -> StableStreamResult:
        if self.end.request_id != self.request.request_id:
            raise ValueError("stream end와 Question Request ID가 다릅니다.")
        state = self.request.state
        match self.end:
            case PendingEvent(kind=pending_kind, state=kind):
                if (
                    self.request.is_terminal
                    or kind != state.kind
                    or pending_kind != question_pending_kind(self.request)
                ):
                    raise ValueError("PendingEvent가 현재 nonterminal state와 다릅니다.")
            case DeclinedEvent(reason_code=reason_code):
                if not isinstance(state, DeclinedRequest) or state.reason_code != reason_code:
                    raise ValueError("DeclinedEvent가 현재 DeclinedRequest와 다릅니다.")
            case FailedEvent(error_code=error_code):
                if not isinstance(state, FailedRequest) or state.error_code != error_code:
                    raise ValueError("FailedEvent가 현재 FailedRequest와 다릅니다.")
            case _ as never:
                assert_never(never)
        return self


QuestionExecutionResult: TypeAlias = BufferedCompletion | StableStreamResult


@final
class OpenQuestionStream(_FrozenModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        arbitrary_types_allowed=True,
    )

    request_id: str
    subscription: QuestionStreamSubscription


@final
class AnsweredQuestionLookup(_FrozenModel):
    """canonical completion에서만 만드는 질문자용 최소 답변 투영."""

    answer_text: str
    request_id: str
    record_id: str
    mode: AnswerMode
    sources: tuple[str, ...] = ()
    review_status: Literal["not_required", "approved"]
    answered_by: str
    agent_id: str

    @field_validator("sources", mode="after")
    @classmethod
    def _sources_must_be_nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not source.strip() for source in value):
            raise ValueError("AnsweredQuestionLookup.sources에는 빈 출처를 둘 수 없습니다.")
        return value


@final
class PendingQuestionLookup(_FrozenModel):
    request_id: str
    kind: QuestionPendingKind
    state: RequestStateKind
    retryable: bool
    message: str


@final
class DeclinedQuestionLookup(_FrozenModel):
    request_id: str
    reason_code: str
    message: str


@final
class FailedQuestionLookup(_FrozenModel):
    request_id: str
    error_code: str
    message: str


QuestionStreamLookup: TypeAlias = (
    AnsweredQuestionLookup | PendingQuestionLookup | DeclinedQuestionLookup | FailedQuestionLookup
)


_PENDING_MESSAGE = "질문을 처리하고 있습니다."
_DECLINED_MESSAGE = "질문 처리가 거절되었습니다."
_FAILED_MESSAGE = "질문을 처리하지 못했습니다."
_INTERRUPTED_MESSAGE = "처리가 일시 중단됐습니다. 잠시 후 다시 연결해 주세요."


class QuestionStreamExecutionService:
    """저장된 Request만 소비해 Runtime→Approval→Finalization을 안전하게 조립한다."""

    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        resolution: QuestionResolutionApplication,
        source: QuestionAnswerSource,
        approval: ApprovalBoundary,
        completion: QuestionCompletionUnitOfWork,
        reader: QuestionCompletionReader,
        max_preview_bytes: object = 65_536,
        production_style: bool = False,
        grounding_terminal_failure_recorder: GroundingTerminalFailureRecorder | None = None,
    ) -> None:
        if production_style:
            # 이 경계가 직접 받는 세 포트의 terminal 원자성만 조기 검증한다.
            # Resolution·Approval·broker까지의 동일 인스턴스 배선은 P17.2c-2의
            # 실제 composition root가 별도로 강제해야 한다.
            validate_question_completion_storage(
                requests=requests,
                completion_uow=completion,
                completion_reader=reader,
                require_durable=True,
            )
        if (
            isinstance(max_preview_bytes, bool)
            or not isinstance(max_preview_bytes, int)
            or max_preview_bytes < 0
        ):
            raise ValueError("max_preview_bytes는 0 이상의 정수여야 합니다.")
        self._requests = requests
        self._resolution = resolution
        self._source = source
        self._approval = approval
        self._completion = completion
        self._reader = reader
        self._grounding_terminal_failure_recorder = grounding_terminal_failure_recorder
        self._max_preview_bytes = max_preview_bytes
        self._execution_lock = RLock()
        self._active_request_ids: set[str] = set()

    def execute(self, request_id: str) -> QuestionExecutionResult:
        correlated_id = _require_request_id(request_id)
        with self._execution_lock:
            if correlated_id in self._active_request_ids:
                raise ReentrantQuestionExecutionError(
                    "같은 Question Request 실행에 재진입할 수 없습니다."
                )
            self._active_request_ids.add(correlated_id)
        try:
            return self._execute_claimed(correlated_id)
        finally:
            with self._execution_lock:
                self._active_request_ids.remove(correlated_id)

    def _execute_claimed(self, correlated_id: str) -> QuestionExecutionResult:
        request = self._read_request(correlated_id)
        if isinstance(request.state, Received):
            # 한 호출에서 initial routing은 정확히 한 번만 시도한다. 결과 DTO가 아니라
            # 다시 읽은 저장 snapshot을 다음 판단의 단일 출처로 삼는다.
            self._resolution.advance(
                correlated_id,
                expected_revision=request.revision,
            )
            request = self._read_request(correlated_id)

        if isinstance(request.state, AwaitingApproval):
            # domain CAS 후 requested journal 기록이 실패한 경우 Runtime을 다시
            # 호출하지 않고 현재 assignment에서 증거만 멱등 복구한다.
            self._approval.ensure_requested(request.request_id)
            request = self._read_request(request.request_id)
        if not isinstance(request.state, ReadyToDispatch):
            return self._project(request)

        source_result = self._produce(request)
        if isinstance(source_result, GroundingTerminalFailureRequested):
            terminal = self._record_grounding_terminal_failure(request, source_result)
            return self._project(terminal)
        buffered = source_result
        gate = self._approval.gate_candidate(
            request.request_id,
            expected_revision=request.revision,
            candidate=buffered.candidate,
        )
        if isinstance(gate, ApprovalPending):
            # 승인 전 token은 전부 폐기한다. ApprovalBoundary가 확정한 현재 state만 투영한다.
            awaiting = self._read_request(request.request_id)
            original_state = request.state
            if (
                gate.request_id != request.request_id
                or not isinstance(awaiting.state, AwaitingApproval)
                or awaiting.revision != request.revision + 1
                or awaiting.state.route != original_state.route
                or awaiting.state.attempt != original_state.attempt
            ):
                raise QuestionStreamCompletionMismatchError(
                    "ApprovalPending과 현재 Question Request가 다릅니다."
                )
            return self._project(awaiting)
        completion = self._canonical_completion(self._completion.complete(gate))
        bundle = self._read_exact_completion(request.request_id, completion)
        tokens = self._preview_tokens(buffered.tokens)
        return BufferedCompletion(tokens=tokens, bundle=bundle)

    def snapshot(self, request_id: str) -> QuestionExecutionResult:
        """Runtime·Approval을 호출하지 않고 현재 Request/completion만 재투영한다."""
        return self._project(self._read_request(_require_request_id(request_id)))

    def _project(self, request: QuestionRequest) -> QuestionExecutionResult:
        state = request.state
        if isinstance(state, AnsweredRequest):
            bundle = self._read_exact_completion(request.request_id, expected=None)
            return BufferedCompletion(tokens=(), bundle=bundle)
        if isinstance(state, DeclinedRequest):
            return StableStreamResult(
                request=request,
                end=DeclinedEvent(
                    request_id=request.request_id,
                    reason_code=state.reason_code,
                    message=_DECLINED_MESSAGE,
                ),
            )
        if isinstance(state, FailedRequest):
            return StableStreamResult(
                request=request,
                end=FailedEvent(
                    request_id=request.request_id,
                    error_code=state.error_code,
                    message=_FAILED_MESSAGE,
                ),
            )
        retryable = isinstance(state, (Received, ReadyToDispatch, AwaitingAnswer))
        return StableStreamResult(
            request=request,
            end=PendingEvent(
                request_id=request.request_id,
                kind=question_pending_kind(request),
                state=state.kind,
                retryable=retryable,
                message=_PENDING_MESSAGE,
            ),
        )

    def _read_request(self, request_id: str) -> QuestionRequest:
        request = self._requests.get(request_id)
        if request is None:
            raise QuestionStreamRequestNotFoundError()
        return self._canonical_request(request)

    def _produce(self, request: QuestionRequest) -> QuestionAnswerSourceResult:
        try:
            raw = self._source.answer(self._canonical_request(request))
        except Exception as error:
            raw_code = getattr(error, "code", None)
            code = (
                raw_code
                if isinstance(raw_code, str) and raw_code.strip()
                else "answer_source_failed"
            )
            raw_retryable = getattr(error, "retryable", None)
            retryable = raw_retryable if isinstance(raw_retryable, bool) else True
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code=code,
                retryable=retryable,
            ) from error
        if type(raw) not in (BufferedAnswer, GroundingTerminalFailureRequested):
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code="invalid_buffered_answer",
                retryable=False,
            )
        assert isinstance(raw, (BufferedAnswer, GroundingTerminalFailureRequested))
        try:
            return _QUESTION_ANSWER_SOURCE_RESULT_ADAPTER.validate_python(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code="invalid_buffered_answer",
                retryable=False,
            ) from error

    def _record_grounding_terminal_failure(
        self,
        request: QuestionRequest,
        command: GroundingTerminalFailureRequested,
    ) -> QuestionRequest:
        if (
            command.request_id != request.request_id
            or command.expected_revision != request.revision
        ):
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code="invalid_grounding_terminal_failure",
                retryable=False,
            )
        recorder = self._grounding_terminal_failure_recorder
        if recorder is None:
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code="grounding_terminal_failure_recorder_unavailable",
                retryable=False,
            )
        try:
            raw_terminal = recorder.fail_if_ready(
                request_id=command.request_id,
                expected_revision=command.expected_revision,
                error_code=command.error_code,
            )
        except GroundingTerminalFailureError as error:
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code=error.code,
                retryable=error.retryable,
            ) from error
        except Exception as error:
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code="grounding_terminal_failure_recorder_failed",
                retryable=True,
            ) from error

        terminal = self._canonical_grounding_terminal_result(
            raw_terminal,
            request_id=request.request_id,
        )
        try:
            stored = self._read_request(request.request_id)
        except QuestionStreamRequestNotFoundError as error:
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code="invalid_grounding_terminal_failure_result",
                retryable=False,
            ) from error
        except QuestionStreamCompletionMismatchError as error:
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code="invalid_grounding_terminal_failure_result",
                retryable=False,
            ) from error
        except Exception as error:
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code="grounding_terminal_failure_read_failed",
                retryable=True,
            ) from error
        if terminal != stored or not stored.is_terminal:
            raise InvalidBufferedAnswerError(
                request_id=request.request_id,
                code="invalid_grounding_terminal_failure_result",
                retryable=False,
            )
        return stored

    @staticmethod
    def _canonical_grounding_terminal_result(
        raw: object,
        *,
        request_id: str,
    ) -> QuestionRequest:
        if type(raw) is not QuestionRequest:
            raise InvalidBufferedAnswerError(
                request_id=request_id,
                code="invalid_grounding_terminal_failure_result",
                retryable=False,
            )
        assert isinstance(raw, QuestionRequest)
        try:
            canonical = QuestionRequest.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise InvalidBufferedAnswerError(
                request_id=request_id,
                code="invalid_grounding_terminal_failure_result",
                retryable=False,
            ) from error
        if canonical.request_id != request_id:
            raise InvalidBufferedAnswerError(
                request_id=request_id,
                code="invalid_grounding_terminal_failure_result",
                retryable=False,
            )
        return canonical

    def _read_exact_completion(
        self,
        request_id: str,
        expected: AnswerCompletion | None,
    ) -> CompletionBundle:
        try:
            raw = self._reader.by_request(request_id)
            if raw is None:
                raise QuestionStreamCompletionMismatchError(
                    "AnsweredRequest의 CompletionBundle이 없습니다."
                )
            bundle = self._canonical_bundle(raw)
        except QuestionStreamCompletionMismatchError:
            raise
        except Exception as error:
            raise QuestionStreamCompletionMismatchError(
                "CompletionReader exact-read에 실패했습니다."
            ) from error
        state = bundle.request.state
        if (
            bundle.completion.request_id != request_id
            or bundle.request.request_id != request_id
            or not isinstance(state, AnsweredRequest)
            or state.record_id != bundle.completion.record_id
            or bundle.answer_record.record_id != bundle.completion.record_id
            or expected is not None
            and bundle.completion != expected
        ):
            raise QuestionStreamCompletionMismatchError(
                "Finalization 반환과 CompletionReader bundle/record가 exact-link되지 않습니다."
            )
        return bundle

    def _preview_tokens(self, tokens: tuple[str, ...]) -> tuple[str, ...]:
        if sum(len(token.encode("utf-8")) for token in tokens) > self._max_preview_bytes:
            return ()
        return tokens

    @staticmethod
    def _canonical_request(request: QuestionRequest) -> QuestionRequest:
        try:
            return QuestionRequest.model_validate(
                request.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise QuestionStreamCompletionMismatchError(
                "Question Request canonical validation에 실패했습니다."
            ) from error

    @staticmethod
    def _canonical_completion(completion: AnswerCompletion) -> AnswerCompletion:
        try:
            return AnswerCompletion.model_validate(
                completion.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise QuestionStreamCompletionMismatchError(
                "Finalization completion 반환이 canonical하지 않습니다."
            ) from error

    @staticmethod
    def _canonical_bundle(bundle: CompletionBundle) -> CompletionBundle:
        try:
            return canonical_completion_bundle(bundle)
        except Exception as error:
            if isinstance(error, QuestionStreamCompletionMismatchError):
                raise
            raise QuestionStreamCompletionMismatchError(
                "CompletionBundle canonical validation에 실패했습니다."
            ) from error


@final
class ProducerStarted(_FrozenModel):
    kind: Literal["started"] = "started"


@final
class ProducerAlreadyRunning(_FrozenModel):
    kind: Literal["already_running"] = "already_running"


@final
class ProducerSchedulerClosed(_FrozenModel):
    kind: Literal["closed"] = "closed"


@final
class ProducerCapacityExceeded(_FrozenModel):
    kind: Literal["capacity_exceeded"] = "capacity_exceeded"


ProducerStartDisposition: TypeAlias = (
    ProducerStarted | ProducerAlreadyRunning | ProducerSchedulerClosed | ProducerCapacityExceeded
)
ProducerJob: TypeAlias = Callable[[], None]


class QuestionProducerScheduler(Protocol):
    def ensure_started(
        self,
        request_id: str,
        job: ProducerJob,
    ) -> ProducerStartDisposition: ...

    def is_running(self, request_id: str) -> bool: ...

    def shutdown(self, *, wait: bool = True) -> None: ...


class ThreadedQuestionProducerScheduler:
    """request별 single running claim을 가진 application-lifetime scheduler."""

    def __init__(
        self,
        *,
        max_workers: object = 4,
        max_inflight: object = 64,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers는 1 이상의 정수여야 합니다.")
        if isinstance(max_inflight, bool) or not isinstance(max_inflight, int) or max_inflight < 1:
            raise ValueError("max_inflight는 1 이상의 정수여야 합니다.")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="question-producer",
        )
        self._max_inflight = max_inflight
        self._lock = RLock()
        self._running: set[str] = set()
        self._closed = False

    def ensure_started(
        self,
        request_id: str,
        job: ProducerJob,
    ) -> ProducerStartDisposition:
        correlated_id = _require_request_id(request_id)
        if not callable(job):
            raise TypeError("producer job은 호출 가능해야 합니다.")
        with self._lock:
            if self._closed:
                return ProducerSchedulerClosed()
            if correlated_id in self._running:
                return ProducerAlreadyRunning()
            if len(self._running) >= self._max_inflight:
                return ProducerCapacityExceeded()
            # submit 전에 claim해야 즉시 실행·재진입도 같은 request를 다시 제출하지 못한다.
            self._running.add(correlated_id)
            try:
                self._executor.submit(self._run_claimed, correlated_id, job)
            except Exception as error:
                self._running.discard(correlated_id)
                raise ProducerSubmissionError("producer 작업 제출에 실패했습니다.") from error
        return ProducerStarted()

    def _run_claimed(self, request_id: str, job: ProducerJob) -> None:
        try:
            job()
        finally:
            with self._lock:
                self._running.discard(request_id)

    def is_running(self, request_id: str) -> bool:
        correlated_id = _require_request_id(request_id)
        with self._lock:
            return correlated_id in self._running

    def shutdown(self, *, wait: object = True) -> None:
        if not isinstance(wait, bool):
            raise TypeError("wait는 bool이어야 합니다.")
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def __enter__(self) -> ThreadedQuestionProducerScheduler:
        return self

    def __exit__(self, *args: object) -> None:
        self.shutdown(wait=True)


class QuestionStreamApplication:
    """Request-first intake, broker 구독, background producer를 순서대로 조립한다."""

    def __init__(
        self,
        *,
        resolution: QuestionResolutionApplication,
        execution: QuestionStreamExecutionService,
        broker: InMemoryQuestionStreamBroker,
        scheduler: QuestionProducerScheduler,
    ) -> None:
        self._resolution = resolution
        self._execution = execution
        self._broker = broker
        self._scheduler = scheduler

    def ask(self, command: AskQuestion) -> QuestionStreamLookup:
        """Request-first 접수 뒤 현재 결과를 blocking 사용자 결과로 투영한다."""
        try:
            outcome = self._resolution.ask(command, result_action="question.read")
            request_id = _require_request_id(outcome.request_id)
        except InitialRoutingError as error:
            # Request는 Router보다 먼저 저장됐다. 접수 호출에는 중단 사실만 돌려주고,
            # 저장된 Received의 Pending 투영은 이후 canonical lookup이 맡는다.
            raise QuestionSurfaceInterruptedError(
                request_id=error.request_id,
                code=error.code,
                retryable=error.retryable,
            ) from None

        if isinstance(outcome, RequestPending) and outcome.state == "ready_to_dispatch":
            # 실행 실패는 호출자에게 전파한다. Request 상태를 여기서 임의로 바꾸지 않는다.
            try:
                result = self._execution.execute(request_id)
            except InvalidBufferedAnswerError as error:
                raise QuestionSurfaceInterruptedError(
                    request_id=request_id,
                    code=error.code,
                    retryable=error.retryable,
                ) from error
            except QuestionStreamCompletionMismatchError as error:
                raise QuestionSurfaceInterruptedError(
                    request_id=request_id,
                    code="completion_evidence_unavailable",
                    retryable=False,
                ) from error
            except Exception as error:
                raise QuestionSurfaceInterruptedError(
                    request_id=request_id,
                    code="question_execution_interrupted",
                    retryable=True,
                ) from error
        else:
            result = self._execution.snapshot(request_id)
        return self._project_lookup(request_id, command.principal, result)

    def open_stream(self, command: AskQuestion) -> OpenQuestionStream:
        # ask가 Received 저장과 initial route를 끝낸 뒤에만 topic을 만든다.
        try:
            outcome = self._resolution.ask(command, result_action="question.stream")
        except InitialRoutingError as error:
            request_id = _require_request_id(error.request_id)
            subscription = self._subscribe_or_unavailable(request_id)
            try:
                subscription.offer(AcceptedEvent(request_id=request_id))
                subscription.offer(self._interrupted(request_id, retryable=error.retryable))
            except StreamCapacityError as capacity_error:
                subscription.close()
                raise QuestionStreamUnavailableError(request_id) from capacity_error
            return OpenQuestionStream(
                request_id=request_id,
                subscription=subscription,
            )
        request_id = outcome.request_id
        subscription = self._subscribe_or_unavailable(request_id)
        try:
            subscription.offer(AcceptedEvent(request_id=request_id))

            if isinstance(outcome, RequestPending) and outcome.state == "ready_to_dispatch":
                disposition = self._start_or_interrupt(request_id, subscription)
                self._reconcile_already_running(
                    request_id,
                    subscription,
                    disposition,
                )
            else:
                self._reconcile(subscription, self._execution.snapshot(request_id))
        except StreamCapacityError as error:
            subscription.close()
            raise QuestionStreamUnavailableError(request_id) from error
        except Exception:
            try:
                subscription.offer(self._interrupted(request_id))
            except StreamCapacityError as error:
                subscription.close()
                raise QuestionStreamUnavailableError(request_id) from error
        return OpenQuestionStream(
            request_id=request_id,
            subscription=subscription,
        )

    def subscribe(
        self,
        request_id: str,
        principal: QuestionPrincipal,
    ) -> QuestionStreamSubscription:
        correlated_id = _require_request_id(request_id)
        visible = self._resolution.retrieve(
            correlated_id,
            principal,
            action="question.stream",
        )
        if isinstance(visible, RequestNotFound):
            raise QuestionStreamRequestNotFoundError()

        # 검증 뒤 먼저 등록해야 terminal commit/publish와 snapshot 재조회 사이를 잃지 않는다.
        subscription = self._subscribe_or_unavailable(correlated_id)
        try:
            current = self._resolution.retrieve(
                correlated_id,
                principal,
                action="question.stream",
            )
            if isinstance(current, RequestNotFound):
                raise QuestionStreamRequestNotFoundError()
            if isinstance(current, RequestPending) and current.state == "ready_to_dispatch":
                disposition = self._start_or_interrupt(correlated_id, subscription)
                self._reconcile_already_running(
                    correlated_id,
                    subscription,
                    disposition,
                )
            else:
                self._reconcile(subscription, self._execution.snapshot(correlated_id))
        except StreamCapacityError as error:
            subscription.close()
            raise QuestionStreamUnavailableError(correlated_id) from error
        except Exception:
            subscription.close()
            raise
        return subscription

    def lookup(
        self,
        request_id: str,
        principal: QuestionPrincipal,
    ) -> QuestionStreamLookup:
        """Requester 소유권 확인 뒤 canonical Request/completion만 안전 DTO로 투영한다."""
        correlated_id = _require_request_id(request_id)
        visible = self._resolution.retrieve(
            correlated_id,
            principal,
            action="question.read",
        )
        if isinstance(visible, RequestNotFound):
            raise QuestionStreamRequestNotFoundError()
        try:
            result = self._execution.snapshot(correlated_id)
        except QuestionStreamRequestNotFoundError:
            raise
        return self._project_lookup(correlated_id, principal, result)

    def _project_lookup(
        self,
        correlated_id: str,
        principal: QuestionPrincipal,
        result: QuestionExecutionResult,
    ) -> QuestionStreamLookup:
        """blocking·retrieve가 공유하는 ownership 검증과 사용자 결과 투영."""
        if isinstance(result, BufferedCompletion):
            bundle = canonical_completion_bundle(result.bundle)
            request = bundle.request
            completion = bundle.completion
            if (
                request.request_id != correlated_id
                or request.org_id != principal.org_id
                or request.requester_id != principal.subject_id
            ):
                raise QuestionStreamRequestNotFoundError()
            return AnsweredQuestionLookup(
                answer_text=completion.text,
                request_id=completion.request_id,
                record_id=completion.record_id,
                mode=completion.mode,
                sources=completion.sources,
                review_status=completion.review_status,
                answered_by=completion.answered_by,
                agent_id=completion.agent_id,
            )

        request = result.request
        if (
            request.request_id != correlated_id
            or request.org_id != principal.org_id
            or request.requester_id != principal.subject_id
        ):
            raise QuestionStreamRequestNotFoundError()
        match result.end:
            case PendingEvent(state=state, retryable=retryable, message=message):
                return PendingQuestionLookup(
                    request_id=correlated_id,
                    kind=question_pending_kind(request),
                    state=state,
                    retryable=retryable,
                    message=message,
                )
            case DeclinedEvent(reason_code=reason_code, message=message):
                return DeclinedQuestionLookup(
                    request_id=correlated_id,
                    reason_code=reason_code,
                    message=message,
                )
            case FailedEvent(error_code=error_code, message=message):
                return FailedQuestionLookup(
                    request_id=correlated_id,
                    error_code=error_code,
                    message=message,
                )
            case _ as never:
                assert_never(never)

    def shutdown(self, *, wait: bool = True) -> None:
        self._scheduler.shutdown(wait=wait)

    def ensure_started(self, request_id: str) -> ExecutionWake:
        """Manager 재개 요청을 기존 snapshot·scheduler·producer 경로로만 연결한다."""
        correlated_id = _require_request_id(request_id)
        try:
            current = self._execution.snapshot(correlated_id)
        except Exception:
            return ExecutionDeferred(reason_code="execution_snapshot_failed")
        if isinstance(current, BufferedCompletion):
            return ExecutionNotNeeded()
        if not (
            isinstance(current.end, PendingEvent)
            and isinstance(current.request.state, ReadyToDispatch)
        ):
            return ExecutionNotNeeded()
        try:
            disposition = self._scheduler.ensure_started(
                correlated_id,
                lambda: self._run_producer(correlated_id),
            )
        except Exception:
            return ExecutionDeferred(reason_code="producer_submission_failed")
        match disposition:
            case ProducerStarted():
                return ExecutionStarted()
            case ProducerAlreadyRunning():
                return ExecutionAlreadyRunning()
            case ProducerSchedulerClosed():
                return ExecutionDeferred(reason_code="producer_scheduler_closed")
            case ProducerCapacityExceeded():
                return ExecutionDeferred(reason_code="producer_capacity_exceeded")
            case _ as never:
                assert_never(never)

    def publish_terminal(self, request_id: str) -> TerminalDelivery:
        """Manager 종결을 canonical exact-read 뒤 기존 broker terminal 경로로 전달한다."""
        correlated_id = _require_request_id(request_id)
        try:
            current = self._execution.snapshot(correlated_id)
            if isinstance(current, BufferedCompletion):
                delivered = self._broker.publish_completion(correlated_id)
            elif isinstance(current.end, (DeclinedEvent, FailedEvent)):
                delivered = self._broker.publish_request_terminal(
                    correlated_id,
                    message=current.end.message,
                )
            else:
                return TerminalDeferred(reason_code="request_not_terminal")
        except Exception:
            return TerminalDeferred(reason_code="terminal_publish_failed")
        if delivered > 0:
            return TerminalPublished()
        return TerminalAlreadyPublished()

    def _subscribe_or_unavailable(
        self,
        request_id: str,
    ) -> QuestionStreamSubscription:
        try:
            return self._broker.subscribe(request_id)
        except StreamCapacityError as error:
            raise QuestionStreamUnavailableError(request_id) from error

    def _start_or_interrupt(
        self,
        request_id: str,
        subscription: QuestionStreamSubscription,
    ) -> ProducerStartDisposition | None:
        try:
            disposition = self._scheduler.ensure_started(
                request_id,
                lambda: self._run_producer(request_id),
            )
        except Exception:
            subscription.offer(self._interrupted(request_id))
            return None
        if isinstance(disposition, (ProducerSchedulerClosed, ProducerCapacityExceeded)):
            subscription.offer(self._interrupted(request_id))
        return disposition

    def _reconcile_already_running(
        self,
        request_id: str,
        subscription: QuestionStreamSubscription,
        disposition: ProducerStartDisposition | None,
    ) -> None:
        if not isinstance(disposition, ProducerAlreadyRunning):
            return
        try:
            # 이미 지나간 Interrupted는 broker가 재생하지 않는다. 현재 Request가
            # 여전히 Ready면 bodyless Pending으로 연결을 닫고 다음 reconnect가 retry한다.
            self._reconcile(subscription, self._execution.snapshot(request_id))
        except Exception:
            subscription.offer(self._interrupted(request_id))

    def _run_producer(self, request_id: str) -> None:
        try:
            self._publish(self._execution.execute(request_id))
        except Exception:
            self._recover_after_failure(request_id)

    def _recover_after_failure(self, request_id: str) -> None:
        """commit 뒤 publish 실패와 commit 전 실행 실패를 Request 사실로 구분한다."""
        try:
            current = self._execution.snapshot(request_id)
            if isinstance(current, BufferedCompletion):
                # exact-read 가능한 terminal commit이면 token은 재발행하지 않고 done만 수렴한다.
                self._broker.publish_completion(request_id)
                return
            if not isinstance(current.end, PendingEvent):
                self._broker.publish_request_terminal(
                    request_id,
                    message=current.end.message,
                )
                return
        except Exception:
            pass
        try:
            self._broker.publish(self._interrupted(request_id))
        except Exception:
            # 전송 충돌은 Request/completion 사실을 바꾸지 않는다. durable delivery는 P17.9.
            return

    def _publish(self, result: QuestionExecutionResult) -> None:
        if isinstance(result, BufferedCompletion):
            for token in result.tokens:
                self._broker.publish(
                    TokenEvent(
                        request_id=result.bundle.completion.request_id,
                        text=token,
                    )
                )
            self._broker.publish_completion(result.bundle.completion.request_id)
            return
        end = result.end
        if isinstance(end, PendingEvent):
            self._broker.publish(end)
            return
        self._broker.publish_request_terminal(
            result.request.request_id,
            message=end.message,
        )

    def _reconcile(
        self,
        subscription: QuestionStreamSubscription,
        result: QuestionExecutionResult,
    ) -> None:
        if isinstance(result, BufferedCompletion):
            self._broker.reconcile_completion(
                subscription,
                result.bundle.completion.request_id,
            )
            return
        end = result.end
        if isinstance(end, PendingEvent):
            subscription.offer(end)
            return
        self._broker.reconcile_request_terminal(
            subscription,
            result.request.request_id,
            message=end.message,
        )

    @staticmethod
    def _interrupted(
        request_id: str,
        *,
        retryable: bool = True,
    ) -> InterruptedEvent:
        return InterruptedEvent(
            request_id=request_id,
            retryable=retryable,
            message=_INTERRUPTED_MESSAGE,
        )


def _require_request_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("request_id는 nonblank 문자열이어야 합니다.")
    return value
