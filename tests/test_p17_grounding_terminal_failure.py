from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from typing import cast

import pytest
from pydantic import ValidationError

from agent_org_network.grounding_terminal_failure import (
    GroundingTerminalFailureConflict,
    GroundingTerminalFailureDependency,
    GroundingTerminalFailureIntegrity,
    GroundingTerminalFailureRequested,
    QuestionRequestGroundingTerminalFailureRecorder,
)
from agent_org_network.question_request import (
    AwaitingAnswer,
    FailedRequest,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
    RouteTarget,
)


NOW = datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc)


def _received_request(*, request_id: str = "request-1") -> QuestionRequest:
    return QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불 기준은?",
        request_id_factory=lambda: request_id,
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )


def _ready_request(*, request_id: str = "request-1") -> QuestionRequest:
    received = _received_request(request_id=request_id)
    trigger_key = f"request-dispatch:{request_id}:1"
    return received.record_initial_routing(
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
            trigger_key=trigger_key,
            handling=HandlingAssignment(
                kind="system",
                ref=trigger_key,
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW,
    )


def _failed(request: QuestionRequest, code: str = "other_failure") -> QuestionRequest:
    return request.transition(
        FailedRequest(error_code=code),
        clock=lambda: NOW + timedelta(minutes=1),
    )


def _other_nonterminal(request: QuestionRequest) -> QuestionRequest:
    state = request.state
    assert isinstance(state, ReadyToDispatch)
    return request.transition(
        AwaitingAnswer(
            route=state.route,
            attempt=state.attempt,
            ticket_id="ticket-1",
            handling=HandlingAssignment(
                kind="runtime_ticket",
                ref="ticket-1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW + timedelta(seconds=30),
    )


class _ScenarioStore:
    def __init__(
        self,
        initial: QuestionRequest,
        *,
        cas_result: object = True,
        cas_error: Exception | None = None,
        post: object = "target",
    ) -> None:
        self.initial = initial
        self.cas_result = cas_result
        self.cas_error = cas_error
        self.post = post
        self.get_calls = 0
        self.cas_calls = 0
        self.updated: QuestionRequest | None = None

    def create(self, request: QuestionRequest) -> QuestionRequest:
        raise AssertionError(f"unexpected create: {request.request_id}")

    def get(self, request_id: str) -> QuestionRequest | None:
        self.get_calls += 1
        if self.get_calls == 1:
            return self.initial
        if self.post == "target":
            assert self.updated is not None
            return self.updated
        if self.post == "current":
            return self.initial
        return cast(QuestionRequest | None, self.post)

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        self.cas_calls += 1
        assert request_id == self.initial.request_id
        assert expected_revision == self.initial.revision
        assert current == self.initial
        self.updated = updated
        if self.cas_error is not None:
            raise self.cas_error
        return cast(bool, self.cas_result)

    def nonterminal(self) -> list[QuestionRequest]:
        raise AssertionError("unexpected nonterminal scan")


class _CountingRequestStore(InMemoryQuestionRequestStore):
    def __init__(self) -> None:
        super().__init__()
        self._count_lock = Lock()
        self.failure_commits = 0

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        changed = super().compare_and_set(
            request_id,
            expected_revision,
            current,
            updated,
        )
        if changed and isinstance(updated.state, FailedRequest):
            with self._count_lock:
                self.failure_commits += 1
        return changed


def _record(
    store: object,
    *,
    request: QuestionRequest | None = None,
) -> QuestionRequest:
    current = request or _ready_request()
    recorder = QuestionRequestGroundingTerminalFailureRecorder(
        requests=cast(QuestionRequestStore, store),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    return recorder.fail_if_ready(
        request_id=current.request_id,
        expected_revision=current.revision,
        error_code="required_grounding_missing",
    )


def test_grounding_terminal_failure_command는_strict_frozen_discriminated_value다() -> None:
    command = GroundingTerminalFailureRequested(
        request_id="request-1",
        expected_revision=1,
        error_code="required_grounding_invalid",
    )

    assert command.kind == "grounding_terminal_failure_requested"
    with pytest.raises(ValidationError):
        GroundingTerminalFailureRequested.model_validate(
            {
                "request_id": "request-1",
                "expected_revision": "1",
                "error_code": "required_grounding_invalid",
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        command.request_id = "forged"  # type: ignore[misc]


def test_recorder는_Ready_exact_revision을_Failed로_CAS하고_store_identity를_밝힌다() -> None:
    received = _received_request()
    request = _ready_request()
    store = InMemoryQuestionRequestStore()
    store.create(received)
    assert store.compare_and_set(request.request_id, 0, received, request)
    recorder = QuestionRequestGroundingTerminalFailureRecorder(
        requests=store,
        clock=lambda: NOW + timedelta(minutes=1),
    )

    failed = recorder.fail_if_ready(
        request_id=request.request_id,
        expected_revision=request.revision,
        error_code="required_grounding_invalid",
    )

    assert recorder.matches_request_store(store)
    assert isinstance(failed.state, FailedRequest)
    assert failed.state.error_code == "required_grounding_invalid"
    assert failed.revision == request.revision + 1
    assert store.get(request.request_id) == failed


def test_recorder는_이미_terminal인_winner를_CAS없이_반환한다() -> None:
    ready = _ready_request()
    terminal = _failed(ready)
    store = _ScenarioStore(terminal)

    result = _record(store, request=ready)

    assert result == terminal
    assert store.cas_calls == 0


def test_Missing_Invalid_terminal_recorder_32way는_한_Failed_winner로_수렴한다() -> None:
    received = _received_request()
    ready = _ready_request()
    store = _CountingRequestStore()
    store.create(received)
    assert store.compare_and_set(ready.request_id, received.revision, received, ready)
    recorder = QuestionRequestGroundingTerminalFailureRecorder(
        requests=store,
        clock=lambda: NOW + timedelta(minutes=1),
    )
    barrier = Barrier(33)
    codes = ("required_grounding_missing", "required_grounding_invalid") * 16

    def record(code: str) -> QuestionRequest:
        barrier.wait(timeout=5)
        return recorder.fail_if_ready(
            request_id=ready.request_id,
            expected_revision=ready.revision,
            error_code=code,  # type: ignore[arg-type]
        )

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(record, code) for code in codes]
        barrier.wait(timeout=5)
        results = [future.result(timeout=5) for future in futures]

    stored = store.get(ready.request_id)
    assert stored is not None and isinstance(stored.state, FailedRequest)
    assert stored.state.error_code in {
        "required_grounding_missing",
        "required_grounding_invalid",
    }
    assert stored.revision == ready.revision + 1
    assert store.failure_commits == 1
    assert all(result == stored for result in results)


@pytest.mark.parametrize(
    ("current", "expected_revision"),
    [
        (_ready_request().model_copy(update={"revision": 2}), 1),
        (_other_nonterminal(_ready_request()), 2),
    ],
)
def test_recorder는_expected_Ready가_아닌_nonterminal을_conflict로_닫는다(
    current: QuestionRequest,
    expected_revision: int,
) -> None:
    store = _ScenarioStore(current)
    recorder = QuestionRequestGroundingTerminalFailureRecorder(
        requests=cast(QuestionRequestStore, store),
        clock=lambda: NOW + timedelta(minutes=1),
    )

    with pytest.raises(GroundingTerminalFailureConflict):
        recorder.fail_if_ready(
            request_id=current.request_id,
            expected_revision=expected_revision,
            error_code="required_grounding_missing",
        )
    assert store.cas_calls == 0


def test_CAS_True는_exact_target_readback만_성공한다() -> None:
    ready = _ready_request()
    assert isinstance(_record(_ScenarioStore(ready)).state, FailedRequest)

    forged = _failed(ready, "different_failure")
    with pytest.raises(GroundingTerminalFailureIntegrity):
        _record(_ScenarioStore(ready, post=forged))


def test_CAS_False는_terminal_winner를_반환하고_nonterminal은_conflict다() -> None:
    ready = _ready_request()
    terminal = _failed(ready, "concurrent_failure")
    assert _record(_ScenarioStore(ready, cas_result=False, post=terminal)) == terminal

    with pytest.raises(GroundingTerminalFailureConflict):
        _record(_ScenarioStore(ready, cas_result=False, post="current"))
    with pytest.raises(GroundingTerminalFailureConflict):
        _record(_ScenarioStore(ready, cas_result=False, post=_other_nonterminal(ready)))


def test_CAS_exception은_target과_terminal을_복구하고_same_current만_retryable이다() -> None:
    ready = _ready_request()
    target_store = _ScenarioStore(
        ready,
        cas_error=OSError("response lost"),
        post="target",
    )
    assert isinstance(_record(target_store).state, FailedRequest)

    terminal = _failed(ready, "concurrent_failure")
    assert (
        _record(_ScenarioStore(ready, cas_error=OSError("response lost"), post=terminal))
        == terminal
    )

    with pytest.raises(GroundingTerminalFailureDependency) as caught:
        _record(_ScenarioStore(ready, cas_error=OSError("before write"), post="current"))
    assert caught.value.retryable is True

    with pytest.raises(GroundingTerminalFailureConflict):
        _record(
            _ScenarioStore(
                ready,
                cas_error=OSError("lost race"),
                post=_other_nonterminal(ready),
            )
        )


def test_non_bool_CAS와_missing_wrong_type_wrong_id_readback은_integrity다() -> None:
    ready = _ready_request()
    with pytest.raises(GroundingTerminalFailureIntegrity):
        _record(_ScenarioStore(ready, cas_result=1))
    with pytest.raises(GroundingTerminalFailureIntegrity):
        _record(_ScenarioStore(ready, post=None))
    with pytest.raises(GroundingTerminalFailureIntegrity):
        _record(_ScenarioStore(ready, post=object()))
    with pytest.raises(GroundingTerminalFailureIntegrity):
        _record(_ScenarioStore(ready, post=_failed(_ready_request(request_id="wrong"))))


def test_exact_type이지만_model_construct로_손상된_readback도_integrity다() -> None:
    ready = _ready_request()
    damaged = QuestionRequest.model_construct(
        request_id=ready.request_id,
        org_id=ready.org_id,
        requester_id=ready.requester_id,
        session_id=ready.session_id,
        question=ready.question,
        context_snapshot=ready.context_snapshot,
        intent=ready.intent,
        initial_disposition=ready.initial_disposition,
        state=ready.state,
        revision=ready.revision,
        created_at=ready.created_at,
        updated_at=NOW - timedelta(minutes=1),
    )

    with pytest.raises(GroundingTerminalFailureIntegrity):
        _record(_ScenarioStore(ready, post=damaged))
