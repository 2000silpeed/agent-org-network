from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    CompletionConcurrencyError,
)
from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionFaultPoint,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import (
    AnswerCandidate,
    FinalizationCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.question_request import (
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.runtime import AnswerMode
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema

NOW = datetime(2026, 7, 13, 11, 45, tzinfo=timezone.utc)


class _Counter:
    def __init__(self) -> None:
        self.value = 0
        self.lock = threading.Lock()

    def increment(self) -> int:
        with self.lock:
            self.value += 1
            return self.value


class _Policy:
    def __init__(self, calls: _Counter) -> None:
        self.calls = calls

    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: AnswerMode,
    ) -> NoApprovalRequired:
        self.calls.increment()
        return NoApprovalRequired(policy_version="approval-v1")


class _Resolver:
    def __init__(self, calls: _Counter) -> None:
        self.calls = calls

    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot:
        self.calls.increment()
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id="owner-1")


def _open(
    path: Path,
    *,
    index: int,
    policy_calls: _Counter,
    resolver_calls: _Counter,
    id_calls: _Counter,
    fault: object | None = None,
) -> SqliteQuestionCompletionUnitOfWork:
    def new_id() -> str:
        id_calls.increment()
        return f"record-{index}"

    return SqliteQuestionCompletionUnitOfWork(
        path,
        policy=_Policy(policy_calls),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=_Resolver(resolver_calls),
        record_id_factory=new_id,
        clock=lambda: NOW,
        fault_injector=fault,  # type: ignore[arg-type]
        timeout=10.0,
    )


def _seed(path: Path) -> QuestionRequest:
    counter = _Counter()
    store = _open(
        path,
        index=0,
        policy_calls=counter,
        resolver_calls=counter,
        id_calls=counter,
    )
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="동시 요청도 한 번만 답하나요?",
        request_id_factory=lambda: "req-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id="session-1",
    )
    store.create(received)
    route = RouteTarget(
        intent="concurrency",
        agent_id="platform-card",
        requires_approval=False,
        authority_version="route-v1",
    )
    trigger = "request-dispatch:req-1:1"
    ready = received.record_initial_routing(
        intent=route.intent,
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
    assert store.compare_and_set("req-1", 0, received, ready)
    store.close()
    return ready


def _handoff(ready: QuestionRequest, *, text: str) -> FinalizationCandidate:
    assert isinstance(ready.state, ReadyToDispatch)
    return FinalizationCandidate(
        request_id=ready.request_id,
        expected_revision=ready.revision,
        attempt=ready.state.attempt,
        route=ready.state.route,
        candidate=AnswerCandidate(
            text=text,
            sources=("concurrency.md",),
            mode="full",
        ),
        approval_evaluation=NoApprovalRequired(policy_version="approval-v1"),
    )


def _artifact_counts(path: Path) -> tuple[int, int, int, int, int]:
    with sqlite3.connect(path) as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "answer_records",
                "terminal_answer_audits",
                "request_session_turns",
                "question_delivery_outbox",
                "question_completion_receipts",
            )
        )  # type: ignore[return-value]


def test_서로_다른_32개_connection의_같은_handoff가_한_completion으로_수렴한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same.db"
    migrate_sqlite_completion_schema(path)
    ready = _seed(path)
    handoff = _handoff(ready, text="항상 한 번만 확정합니다.")
    policy_calls, resolver_calls, id_calls = _Counter(), _Counter(), _Counter()
    stores = [
        _open(
            path,
            index=index,
            policy_calls=policy_calls,
            resolver_calls=resolver_calls,
            id_calls=id_calls,
        )
        for index in range(32)
    ]
    barrier = threading.Barrier(32)

    def run(store: SqliteQuestionCompletionUnitOfWork) -> object:
        barrier.wait()
        return store.complete(handoff)

    try:
        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(run, stores))
    finally:
        for store in stores:
            store.close()

    assert len(set(results)) == 1
    assert policy_calls.value == resolver_calls.value == id_calls.value == 1
    assert _artifact_counts(path) == (1, 1, 1, 1, 1)


def test_서로_다른_32개_handoff_경쟁은_한_winner와_명시적_concurrency_error만_남긴다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "different.db"
    migrate_sqlite_completion_schema(path)
    ready = _seed(path)
    policy_calls, resolver_calls, id_calls = _Counter(), _Counter(), _Counter()
    stores = [
        _open(
            path,
            index=index,
            policy_calls=policy_calls,
            resolver_calls=resolver_calls,
            id_calls=id_calls,
        )
        for index in range(32)
    ]
    barrier = threading.Barrier(32)

    def run(index: int) -> tuple[str, object]:
        barrier.wait()
        try:
            return "ok", stores[index].complete(_handoff(ready, text=f"서로 다른 후보 {index}"))
        except CompletionConcurrencyError as error:
            return "conflict", error

    try:
        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(run, range(32)))
    finally:
        for store in stores:
            store.close()

    assert [kind for kind, _ in results].count("ok") == 1
    assert [kind for kind, _ in results].count("conflict") == 31
    assert policy_calls.value == resolver_calls.value == id_calls.value == 1
    assert _artifact_counts(path) == (1, 1, 1, 1, 1)


class _PauseBeforeReceipt:
    def __init__(self) -> None:
        self.writer_paused = threading.Event()
        self.release = threading.Event()

    def __call__(self, point: SqliteCompletionFaultPoint) -> None:
        if point == "after_outbox":
            self.writer_paused.set()
            assert self.release.wait(timeout=5)


def test_동시_reader는_부분_artifact가_아닌_None_or_complete_snapshot만_본다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reader.db"
    migrate_sqlite_completion_schema(path)
    ready = _seed(path)
    policy_calls, resolver_calls, id_calls = _Counter(), _Counter(), _Counter()
    pause = _PauseBeforeReceipt()
    writer = _open(
        path,
        index=1,
        policy_calls=policy_calls,
        resolver_calls=resolver_calls,
        id_calls=id_calls,
        fault=pause,
    )
    reader = _open(
        path,
        index=2,
        policy_calls=policy_calls,
        resolver_calls=resolver_calls,
        id_calls=id_calls,
    )
    handoff = _handoff(ready, text="원자 snapshot")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer.complete, handoff)
        assert pause.writer_paused.wait(timeout=5)
        assert reader.by_request(ready.request_id) is None
        pause.release.set()
        completion = future.result(timeout=5)

    bundle = reader.by_request(ready.request_id)
    assert bundle is not None
    assert bundle.completion == completion
    assert _artifact_counts(path) == (1, 1, 1, 1, 1)
    writer.close()
    reader.close()
