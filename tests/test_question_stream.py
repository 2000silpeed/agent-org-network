from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent_org_network.answer_finalization import (
    AnswerCompletion,
    AnswerResponsibilitySnapshot,
    CompletionBundle,
    DeliveryOutboxEntry,
    NoApprovalEvidence,
    TerminalAnswerAudit,
    QuestionCompletionReader,
)
from agent_org_network.answer_record import AnswerRecord
from agent_org_network.question_request import (
    AnsweredRequest,
    FailedRequest,
    HandlingAssignment,
    QuestionRequestStore,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.question_stream import (
    QUESTION_STREAM_SSE_KEEPALIVE,
    QUESTION_STREAM_SSE_PRIMING,
    AcceptedEvent,
    DeclinedEvent,
    DoneEvent,
    FailedEvent,
    InMemoryQuestionStreamBroker,
    InvalidStreamEventError,
    InterruptedEvent,
    PendingEvent,
    QuestionStreamSubscription,
    StreamCapacityError,
    StreamEndConflictError,
    StreamEventTooLargeError,
    StreamRequestMismatchError,
    TokenEvent,
    UntrustedTerminalEventError,
    iter_question_stream_frames,
    serialize_question_stream_sse,
)
from agent_org_network.session import SessionTurn

NOW = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)


class _EvidenceRequests:
    """terminal projection 테스트 전용 read-mostly trusted Request store."""

    def __init__(self, *requests: QuestionRequest) -> None:
        self._requests = {request.request_id: request for request in requests}

    def create(self, request: QuestionRequest) -> QuestionRequest:
        self._requests[request.request_id] = request
        return request

    def get(self, request_id: str) -> QuestionRequest | None:
        return self._requests.get(request_id)

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        if self._requests.get(request_id) != current or current.revision != expected_revision:
            return False
        self._requests[request_id] = updated
        return True

    def nonterminal(self) -> list[QuestionRequest]:
        return [request for request in self._requests.values() if not request.is_terminal]


class _EvidenceCompletions:
    """terminal projection 테스트 전용 exact-read completion reader."""

    def __init__(self, *bundles: CompletionBundle) -> None:
        self._by_request = {bundle.completion.request_id: bundle for bundle in bundles}

    def by_request(self, request_id: str) -> CompletionBundle | None:
        return self._by_request.get(request_id)

    def by_record(self, record_id: str) -> CompletionBundle | None:
        return next(
            (
                bundle
                for bundle in self._by_request.values()
                if bundle.completion.record_id == record_id
            ),
            None,
        )

    def remove(self, request_id: str) -> None:
        self._by_request.pop(request_id, None)

    def add(self, bundle: CompletionBundle) -> None:
        self._by_request[bundle.completion.request_id] = bundle


def _evidence_broker(
    *,
    bundle: CompletionBundle | None = None,
    request: QuestionRequest | None = None,
    max_queue_size: int = 4,
) -> InMemoryQuestionStreamBroker:
    stored = request if request is not None else (None if bundle is None else bundle.request)
    requests: QuestionRequestStore = _EvidenceRequests(*((stored,) if stored is not None else ()))
    completions: QuestionCompletionReader = _EvidenceCompletions(
        *((bundle,) if bundle is not None else ())
    )
    return InMemoryQuestionStreamBroker(
        requests=requests,
        completions=completions,
        max_queue_size=max_queue_size,
    )


def _failed_request() -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="org-secret",
        requester_id="requester-secret",
        question="비공개 질문",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW - timedelta(minutes=1),
        due_at=NOW + timedelta(hours=1),
    )
    return received.transition(FailedRequest(error_code="runtime_failed"), clock=lambda: NOW)


def _received_request() -> QuestionRequest:
    return QuestionRequest.receive(
        org_id="org-secret",
        requester_id="requester-secret",
        question="아직 처리 중인 질문",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )


def _bundle(*, with_session: bool = False) -> CompletionBundle:
    received = QuestionRequest.receive(
        org_id="org-secret",
        requester_id="requester-secret",
        question="비공개 질문",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id="session-1" if with_session else None,
    )
    route = RouteTarget(
        intent="refund-secret",
        agent_id="refund-card",
        requires_approval=False,
        authority_version="policy-secret",
    )
    ready = received.record_initial_routing(
        intent="refund-secret",
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
            attempt=1,
            trigger_key="request-dispatch:req-1:1",
            handling=HandlingAssignment(
                kind="system",
                ref="request-dispatch:req-1:1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    answered = ready.transition(AnsweredRequest(record_id="rec-1"), clock=lambda: NOW)
    completion = AnswerCompletion(
        request_id="req-1",
        record_id="rec-1",
        text="최종 답 본문",
        answered_by="owner-1",
        agent_id="refund-card",
        mode="full",
        sources=("policy.md",),
        snapshot_sha="snapshot-secret",
        review_status="not_required",
        completed_at=NOW,
    )
    record = AnswerRecord.for_request(
        request_id="req-1",
        record_id="rec-1",
        question=received.question,
        answer_text=completion.text,
        answered_by="owner-1",
        agent_id="refund-card",
        mode="full",
        sources=("policy.md",),
        snapshot_sha="snapshot-secret",
        session_id="session-1" if with_session else None,
        answered_at=NOW,
    )
    audit = TerminalAnswerAudit(
        request_id="req-1",
        record_id="rec-1",
        org_id="org-secret",
        requester_id="requester-secret",
        attempt=1,
        route=route,
        responsibility=AnswerResponsibilitySnapshot(agent_id="refund-card", owner_id="owner-1"),
        candidate_mode="full",
        final_mode="full",
        approval=NoApprovalEvidence(policy_version="policy-secret"),
        completed_at=NOW,
    )
    return CompletionBundle(
        completion=completion,
        request=answered,
        answer_record=record,
        terminal_audit=audit,
        session_turn=(
            SessionTurn.for_request(
                request_id="req-1",
                question=received.question,
                answer_text=completion.text,
                answered_by="refund-card",
                at=NOW,
            )
            if with_session
            else None
        ),
        delivery=DeliveryOutboxEntry(request_id="req-1", record_id="rec-1", created_at=NOW),
    )


def _parse(event: object) -> tuple[str, dict[str, object]]:
    frame = serialize_question_stream_sse(event)  # type: ignore[arg-type]
    lines = frame.splitlines()
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


def test_events_are_strict_frozen_and_sealed() -> None:
    event = AcceptedEvent(request_id="req-1")
    with pytest.raises(ValidationError):
        AcceptedEvent.model_validate({"event_type": "accepted", "request_id": 1}, strict=True)
    with pytest.raises(ValidationError):
        AcceptedEvent.model_validate(
            {"event_type": "accepted", "request_id": "req-1", "route": "secret"},
            strict=True,
        )
    with pytest.raises(ValidationError):
        event.request_id = "changed"  # type: ignore[misc]


def test_all_seven_events_have_exact_public_wire_shape() -> None:
    events = [
        AcceptedEvent(request_id="req-1"),
        TokenEvent(request_id="req-1", text="토큰"),
        PendingEvent(
            request_id="req-1",
            kind="routed",
            state="awaiting_approval",
            retryable=False,
            message="확인 중",
        ),
        DoneEvent.from_completion(_bundle()),
        DeclinedEvent(request_id="req-1", reason_code="declined", message="거절됨"),
        FailedEvent(request_id="req-1", error_code="failed", message="처리 실패"),
        InterruptedEvent(
            request_id="req-1",
            retryable=True,
            message="잠시 후 다시 연결해 주세요.",
        ),
    ]
    expected = {
        "accepted": {"request_id"},
        "token": {"request_id", "text"},
        "pending": {"request_id", "kind", "state", "retryable", "message"},
        "done": {
            "request_id",
            "record_id",
            "mode",
            "sources",
            "review_status",
            "answered_by",
            "agent_id",
        },
        "declined": {"request_id", "reason_code", "message"},
        "failed": {"request_id", "error_code", "message"},
        "interrupted": {"request_id", "retryable", "message"},
    }
    for event in events:
        name, data = _parse(event)
        assert set(data) == expected[name]
        assert serialize_question_stream_sse(event).endswith("\n\n")
    done_frame = serialize_question_stream_sse(events[3])
    for secret in (
        "org-secret",
        "requester-secret",
        "refund-secret",
        "policy-secret",
        "snapshot-secret",
        "최종 답 본문",
        "비공개 질문",
    ):
        assert secret not in done_frame


def test_done_projection_is_a_plain_data_defensive_copy() -> None:
    bundle = _bundle()
    event = DoneEvent.from_completion(bundle)
    object.__setattr__(bundle.completion, "record_id", "corrupt")
    assert event.record_id == "rec-1"


def test_done_projection_accepts_completion_bundle_with_session_turn() -> None:
    event = DoneEvent.from_completion(_bundle(with_session=True))
    assert event.request_id == "req-1" and event.record_id == "rec-1"


def test_done_projection_rejects_corrupted_nested_artifact() -> None:
    bundle = _bundle()
    object.__setattr__(bundle.answer_record, "answer_text", "corrupted")
    with pytest.raises(InvalidStreamEventError):
        DoneEvent.from_completion(bundle)


def test_done_projection_rejects_malformed_and_jointly_corrupted_bundle() -> None:
    with pytest.raises(InvalidStreamEventError):
        DoneEvent.from_completion(CompletionBundle.model_construct())

    bundle = _bundle(with_session=True)
    assert bundle.session_turn is not None
    object.__setattr__(bundle.request, "question", "")
    object.__setattr__(bundle.answer_record, "question", "")
    object.__setattr__(bundle.answer_record, "needs_correction_review", object())
    object.__setattr__(bundle.session_turn, "question", "")
    with pytest.raises(InvalidStreamEventError):
        DoneEvent.from_completion(bundle)


def test_broker_is_request_keyed_and_never_replays_tokens() -> None:
    broker = InMemoryQuestionStreamBroker(max_queue_size=2)
    assert broker.publish(TokenEvent(request_id="req-1", text="before")) == 0
    first = broker.subscribe("req-1")
    other = broker.subscribe("req-2")
    assert first.get(timeout=0) is None
    assert broker.publish(TokenEvent(request_id="req-1", text="after")) == 1
    assert first.get(timeout=0) == TokenEvent(request_id="req-1", text="after")
    assert other.get(timeout=0) is None


def test_bounded_queue_drops_overflow_tokens_but_keeps_control_and_terminal() -> None:
    bundle = _bundle()
    broker = _evidence_broker(bundle=bundle, max_queue_size=2)
    sub = broker.subscribe("req-1")
    sub.offer(TokenEvent(request_id="req-1", text="one"))
    sub.offer(TokenEvent(request_id="req-1", text="two"))
    assert not sub.offer(TokenEvent(request_id="req-1", text="dropped"))
    assert sub.offer(
        PendingEvent(
            request_id="req-1",
            kind="routed",
            state="awaiting_approval",
            retryable=False,
            message="대기",
        )
    )
    assert not sub.offer(InterruptedEvent(request_id="req-1", retryable=True, message="재연결"))
    assert broker.reconcile_completion(sub, "req-1")
    queued = [sub.get(timeout=0), sub.get(timeout=0)]
    assert any(isinstance(event, DoneEvent) for event in queued)
    assert not any(isinstance(event, PendingEvent) for event in queued)
    assert not any(isinstance(event, InterruptedEvent) for event in queued)


def test_same_end_event_dedupes_and_different_end_fails_closed() -> None:
    bundle = _bundle()
    requests = _EvidenceRequests(bundle.request)
    completions = _EvidenceCompletions(bundle)
    broker = InMemoryQuestionStreamBroker(
        requests=requests,
        completions=completions,
        max_queue_size=4,
    )
    sub = broker.subscribe("req-1")
    done = DoneEvent.from_completion(_bundle())
    with pytest.raises(UntrustedTerminalEventError):
        sub.offer(done)
    with pytest.raises(UntrustedTerminalEventError):
        broker.publish(done)
    assert broker.reconcile_completion(sub, "req-1")
    assert not broker.reconcile_completion(sub, "req-1")
    with pytest.raises(UntrustedTerminalEventError):
        sub.offer(FailedEvent(request_id="req-1", error_code="x", message="실패"))
    requests.create(_failed_request())
    completions.remove("req-1")
    with pytest.raises(StreamEndConflictError):
        broker.publish_request_terminal("req-1", message="실패")


def test_topic_terminal_conflict_fails_before_any_fanout() -> None:
    bundle = _bundle()
    requests = _EvidenceRequests(bundle.request)
    completions = _EvidenceCompletions(bundle)
    broker = InMemoryQuestionStreamBroker(
        requests=requests,
        completions=completions,
        max_queue_size=4,
    )
    first = broker.subscribe("req-1")
    second = broker.subscribe("req-1")
    assert broker.reconcile_completion(first, "req-1")
    requests.create(_failed_request())
    completions.remove("req-1")
    with pytest.raises(StreamEndConflictError):
        broker.publish_request_terminal("req-1", message="실패")
    assert second.get(timeout=0) is None
    requests.create(bundle.request)
    completions.add(bundle)
    assert broker.publish_completion("req-1") == 1
    assert isinstance(second.get(timeout=0), DoneEvent)


def test_terminal_apis_require_persisted_exact_read_evidence() -> None:
    untrusted = InMemoryQuestionStreamBroker(max_queue_size=4)
    subscription = untrusted.subscribe("req-1")
    for operation in (
        lambda: untrusted.publish_completion("req-1"),
        lambda: untrusted.reconcile_completion(subscription, "req-1"),
        lambda: untrusted.publish_request_terminal("req-1", message="실패"),
        lambda: untrusted.reconcile_request_terminal(subscription, "req-1", message="실패"),
    ):
        with pytest.raises(UntrustedTerminalEventError):
            operation()

    forged = _bundle()
    empty_evidence = _evidence_broker()
    forged_subscription = empty_evidence.subscribe("req-1")
    with pytest.raises(UntrustedTerminalEventError):
        empty_evidence.publish_completion(forged.completion.request_id)
    with pytest.raises(UntrustedTerminalEventError):
        empty_evidence.reconcile_completion(forged_subscription, forged.completion.request_id)
    with pytest.raises(UntrustedTerminalEventError):
        empty_evidence.publish(DoneEvent.from_completion(forged))


def test_terminal_evidence_rejects_missing_nonterminal_and_incomplete_state() -> None:
    empty_evidence = _evidence_broker()
    with pytest.raises(UntrustedTerminalEventError):
        empty_evidence.publish_request_terminal("req-1", message="실패")

    nonterminal = _evidence_broker(request=_received_request())
    with pytest.raises(InvalidStreamEventError):
        nonterminal.publish_request_terminal("req-1", message="아직 처리 중")

    bundle = _bundle()
    object.__setattr__(bundle.answer_record, "answer_text", "corrupted")
    incomplete = _evidence_broker(bundle=bundle)
    with pytest.raises(InvalidStreamEventError):
        incomplete.publish_completion("req-1")


def test_request_terminal_is_minted_only_from_stored_terminal_request() -> None:
    request = _failed_request()
    broker = _evidence_broker(request=request)
    first = broker.subscribe("req-1")
    assert broker.publish_request_terminal("req-1", message="처리 실패") == 1
    assert first.get(timeout=0) == FailedEvent(
        request_id="req-1",
        error_code="runtime_failed",
        message="처리 실패",
    )

    late = broker.subscribe("req-1")
    assert late.get(timeout=0) == FailedEvent(
        request_id="req-1",
        error_code="runtime_failed",
        message="처리 실패",
    )


def test_terminal_topic_seals_late_subscriber_and_rejects_stale_nonterminal() -> None:
    bundle = _bundle()
    broker = _evidence_broker(bundle=bundle, max_queue_size=4)
    first = broker.subscribe("req-1")
    assert broker.publish(TokenEvent(request_id="req-1", text="preview")) == 1
    assert broker.publish_completion("req-1") == 1

    late = broker.subscribe("req-1")
    assert broker.publish(TokenEvent(request_id="req-1", text="stale")) == 0
    assert (
        broker.publish(
            PendingEvent(
                request_id="req-1",
                kind="unowned",
                state="awaiting_manager",
                retryable=False,
                message="stale pending",
            )
        )
        == 0
    )
    assert late.get(timeout=0) == DoneEvent.from_completion(bundle)
    assert late.get(timeout=0) is None
    assert first.get(timeout=0) == TokenEvent(request_id="req-1", text="preview")
    assert first.get(timeout=0) == DoneEvent.from_completion(bundle)


def test_public_offer_checks_request_and_returns_defensive_copies() -> None:
    broker = InMemoryQuestionStreamBroker(max_queue_size=2)
    first = broker.subscribe("req-1")
    second = broker.subscribe("req-1")
    event = PendingEvent(
        request_id="req-1",
        kind="routed",
        state="awaiting_approval",
        retryable=False,
        message="원본",
    )
    broker.publish(event)
    object.__setattr__(event, "message", "변조")
    one = first.get(timeout=0)
    assert isinstance(one, PendingEvent)
    object.__setattr__(one, "message", "재변조")
    assert second.get(timeout=0) == PendingEvent(
        request_id="req-1",
        kind="routed",
        state="awaiting_approval",
        retryable=False,
        message="원본",
    )
    with pytest.raises(StreamRequestMismatchError):
        first.offer(AcceptedEvent(request_id="req-2"))


def test_close_is_idempotent_and_cleans_empty_topic() -> None:
    broker = InMemoryQuestionStreamBroker(max_queue_size=2)
    first = broker.subscribe("req-1")
    second = broker.subscribe("req-1")
    assert broker.topic_count() == 1 and broker.subscriber_count("req-1") == 2
    first.close()
    first.close()
    assert broker.topic_count() == 1 and broker.subscriber_count("req-1") == 1
    second.close()
    assert broker.topic_count() == 0 and broker.subscriber_count("req-1") == 0


def test_request_id_is_readonly_and_close_clears_frames_without_keepalive_burst() -> None:
    broker = InMemoryQuestionStreamBroker(max_queue_size=2)
    sub = broker.subscribe("req-1")
    sub.offer(TokenEvent(request_id="req-1", text="queued"))
    with pytest.raises(AttributeError):
        sub.request_id = "req-2"  # type: ignore[misc]
    sub.close()
    assert list(iter_question_stream_frames(sub, max_polls=4, poll_timeout=0)) == [
        QUESTION_STREAM_SSE_PRIMING
    ]


def test_broker_configuration_and_subscription_key_fail_closed() -> None:
    for invalid in (True, 1, 2.5, "2"):
        with pytest.raises(ValueError):
            InMemoryQuestionStreamBroker(max_queue_size=invalid)  # type: ignore[arg-type]
    broker = InMemoryQuestionStreamBroker(max_queue_size=2)
    for invalid in (None, 1, object()):
        with pytest.raises(ValueError):
            broker.subscribe(invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        QuestionStreamSubscription(
            request_id="req-1",
            max_queue_size=2,
            on_close=None,  # type: ignore[arg-type]
        )
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            QuestionStreamSubscription(
                request_id="req-1",
                max_queue_size=invalid,  # type: ignore[arg-type]
                on_close=lambda _: None,
            )
    for invalid in (True, 1.5, "4"):
        with pytest.raises(ValueError):
            list(
                iter_question_stream_frames(
                    broker.subscribe("req-polls"),
                    max_polls=invalid,  # type: ignore[arg-type]
                    poll_timeout=0,
                )
            )


def test_broker_count_and_payload_caps_are_enforced() -> None:
    broker = InMemoryQuestionStreamBroker(
        max_queue_size=2,
        max_topics=1,
        max_subscribers_per_topic=1,
        max_event_bytes=128,
    )
    broker.subscribe("req-1")
    with pytest.raises(StreamCapacityError):
        broker.subscribe("req-1")
    with pytest.raises(StreamCapacityError):
        broker.subscribe("req-2")
    assert broker.publish(TokenEvent(request_id="req-1", text="x" * 1_000)) == 0
    with pytest.raises(StreamEventTooLargeError):
        broker.publish(
            PendingEvent(
                request_id="req-1",
                kind="unowned",
                state="awaiting_manager",
                retryable=False,
                message="x" * 1_000,
            )
        )


def test_finite_frame_iterator_emits_priming_keepalive_and_stops_at_end() -> None:
    sub = InMemoryQuestionStreamBroker(max_queue_size=4).subscribe("req-1")
    empty = list(iter_question_stream_frames(sub, max_polls=1, poll_timeout=0))
    assert empty == [QUESTION_STREAM_SSE_PRIMING, QUESTION_STREAM_SSE_KEEPALIVE]
    sub.offer(AcceptedEvent(request_id="req-1"))
    sub.offer(InterruptedEvent(request_id="req-1", retryable=True, message="다시 연결"))
    frames = list(iter_question_stream_frames(sub, max_polls=4, poll_timeout=0))
    assert frames[0] == QUESTION_STREAM_SSE_PRIMING
    assert [frame.splitlines()[0] for frame in frames[1:]] == [
        "event: accepted",
        "event: interrupted",
    ]

    pending_sub = InMemoryQuestionStreamBroker(max_queue_size=2).subscribe("req-2")
    pending_sub.offer(
        PendingEvent(
            request_id="req-2",
            kind="unowned",
            state="awaiting_manager",
            retryable=False,
            message="담당 확인 중",
        )
    )
    pending_frames = list(iter_question_stream_frames(pending_sub, max_polls=4, poll_timeout=0))
    assert [frame.splitlines()[0] for frame in pending_frames[1:]] == ["event: pending"]
