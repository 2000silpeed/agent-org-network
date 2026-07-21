"""P17.2b 비종결 QuestionRequest 재기동 reconciliation 러너 테스트."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingApproval,
    AwaitingConflict,
    AwaitingManager,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.request_recovery import (
    ReconcileReport,
    RecoveryError,
    RequestRecoveryRunner,
)
from agent_org_network.sqlite_stores import SqliteQuestionRequestStore

_T0 = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(minutes=1)
_T2 = _T0 + timedelta(minutes=2)
_DUE = _T0 + timedelta(hours=1)


def _handling(
    kind: str,
    ref: str,
) -> HandlingAssignment:
    return HandlingAssignment.model_validate({"kind": kind, "ref": ref, "due_at": _DUE})


def _route(*, requires_approval: bool = False) -> RouteTarget:
    return RouteTarget(
        intent="환불",
        agent_id="cs_ops",
        requires_approval=requires_approval,
        authority_version="authority-v1",
    )


def _received(request_id: str) -> QuestionRequest:
    return QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="환불이 되나요?",
        request_id_factory=lambda: request_id,
        clock=lambda: _T0,
        due_at=_DUE,
    )


def _ready(
    request_id: str,
    *,
    route: RouteTarget | None = None,
    attempt: int = 1,
    trigger_key: str = "initial-route",
) -> QuestionRequest:
    received = _received(request_id)
    target = ReadyToDispatch(
        route=route or _route(),
        attempt=attempt,
        trigger_key=trigger_key,
        handling=_handling("system", trigger_key),
    )
    if attempt == 1:
        return received.record_initial_routing(
            intent="환불",
            disposition="routed",
            target=target,
            clock=lambda: _T1,
        )
    payload = received.model_dump()
    payload.update(
        {
            "intent": "환불",
            "initial_disposition": "routed",
            "state": target,
            "revision": 1,
            "updated_at": _T1,
        }
    )
    return QuestionRequest.model_validate(payload)


def _create_ready(
    store: QuestionRequestStore,
    request_id: str,
    *,
    route: RouteTarget | None = None,
) -> QuestionRequest:
    received = _received(request_id)
    ready = _ready(request_id, route=route)
    store.create(received)
    assert store.compare_and_set(request_id, 0, received, ready)
    return ready


def _seed_waiting_requests(store: QuestionRequestStore) -> list[QuestionRequest]:
    ready_answer = _create_ready(store, "waiting-answer")
    waiting_answer = ready_answer.transition(
        AwaitingAnswer(
            route=_route(),
            attempt=1,
            ticket_id="ticket-1",
            handling=_handling("runtime_ticket", "ticket-1"),
        ),
        clock=lambda: _T2,
    )
    assert store.compare_and_set("waiting-answer", 1, ready_answer, waiting_answer)

    received_conflict = _received("waiting-conflict")
    store.create(received_conflict)
    waiting_conflict = received_conflict.record_initial_routing(
        intent="환불",
        disposition="contested",
        target=AwaitingConflict(
            case_id="case-1",
            handling=_handling("conflict_case", "case-1"),
        ),
        clock=lambda: _T1,
    )
    assert store.compare_and_set("waiting-conflict", 0, received_conflict, waiting_conflict)

    received_manager = _received("waiting-manager")
    store.create(received_manager)
    waiting_manager = received_manager.record_initial_routing(
        intent="환불",
        disposition="unowned",
        target=AwaitingManager(
            item_id="manager-1",
            public_kind="unowned",
            handling=_handling("manager_item", "manager-1"),
        ),
        clock=lambda: _T1,
    )
    assert store.compare_and_set("waiting-manager", 0, received_manager, waiting_manager)

    approval_route = _route(requires_approval=True)
    ready_approval = _create_ready(
        store,
        "waiting-approval",
        route=approval_route,
    )
    waiting_approval = ready_approval.transition(
        AwaitingApproval(
            route=approval_route,
            attempt=1,
            draft_ref="draft-1",
            handling=_handling("approval_item", "draft-1"),
        ),
        clock=lambda: _T2,
    )
    assert store.compare_and_set("waiting-approval", 1, ready_approval, waiting_approval)
    return [
        waiting_answer,
        waiting_conflict,
        waiting_manager,
        waiting_approval,
    ]


def test_run_once는_Received와_Ready만_exact인자로_호출하고_나머지는_waiting이다() -> None:
    store = InMemoryQuestionRequestStore()
    received = _received("received-1")
    store.create(received)
    _create_ready(store, "ready-1")
    _seed_waiting_requests(store)
    received_calls: list[tuple[str, int]] = []
    ready_calls: list[tuple[str, int, int]] = []
    runner = RequestRecoveryRunner(
        store,
        recover_received=lambda request_id, revision: received_calls.append((request_id, revision)),
        recover_ready=lambda request_id, revision, attempt: ready_calls.append(
            (request_id, revision, attempt)
        ),
    )

    report = runner.run_once()

    assert report == ReconcileReport(
        scanned=6,
        received_attempted=1,
        ready_attempted=1,
        waiting=4,
        stale=0,
        errors=(),
    )
    assert received_calls == [("received-1", 0)]
    assert ready_calls == [("ready-1", 1, 1)]


def test_SQLite재오픈뒤_Ready_request를_복구hook에_정확히_전달한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "recovery-restart.db"
    writer = SqliteQuestionRequestStore(db_path)
    ready = _create_ready(writer, "ready-after-restart")
    writer.close()
    calls: list[tuple[str, int, int]] = []

    reader = SqliteQuestionRequestStore(db_path)
    runner = RequestRecoveryRunner(
        reader,
        recover_received=lambda _request_id, _revision: None,
        recover_ready=lambda request_id, revision, attempt: calls.append(
            (request_id, revision, attempt)
        ),
    )

    report = runner.run_once()

    assert report.ready_attempted == 1
    assert report.errors == ()
    assert isinstance(ready.state, ReadyToDispatch)
    assert calls == [(ready.request_id, ready.revision, ready.state.attempt)]
    reader.close()


@pytest.mark.parametrize("changed_kind", ["revision", "state_or_attempt"])
def test_snapshot뒤_revision이나_state_attempt가_달라지면_hook없이_stale다(
    changed_kind: str,
) -> None:
    snapshot = _ready("stale-1")
    if changed_kind == "revision":
        changed = snapshot.transition(
            AwaitingAnswer(
                route=_route(),
                attempt=1,
                ticket_id="ticket-stale",
                handling=_handling("runtime_ticket", "ticket-stale"),
            ),
            clock=lambda: _T2,
        )
    else:
        payload = snapshot.model_dump()
        payload["state"] = ReadyToDispatch(
            route=_route(),
            attempt=2,
            trigger_key="reassigned",
            handling=_handling("system", "reassigned"),
        )
        changed = QuestionRequest.model_validate(payload)
    store = _SnapshotChangingStore(snapshot, changed)
    calls: list[tuple[str, int, int]] = []
    runner = RequestRecoveryRunner(
        store,
        recover_received=lambda _request_id, _revision: None,
        recover_ready=lambda request_id, revision, attempt: calls.append(
            (request_id, revision, attempt)
        ),
    )

    report = runner.run_once()

    assert report.stale == 1
    assert report.ready_attempted == 0
    assert report.waiting == 0
    assert calls == []


def test_hook오류는_상태를_건드리지않고_구조화하며_다음_run에서_재시도한다() -> None:
    store = InMemoryQuestionRequestStore()
    ready = _create_ready(store, "retry-1")
    attempts = 0

    def recover_ready(_request_id: str, _revision: int, _attempt: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("일시 장애")

    runner = RequestRecoveryRunner(
        store,
        recover_received=lambda _request_id, _revision: None,
        recover_ready=recover_ready,
    )

    first = runner.run_once()
    assert store.get("retry-1") == ready
    assert first.errors == (
        RecoveryError(
            request_id="retry-1",
            revision=1,
            state_kind="ready_to_dispatch",
            error_type="ValueError",
            message="일시 장애",
        ),
    )

    second = runner.run_once()
    assert attempts == 2
    assert second.ready_attempted == 1
    assert second.errors == ()
    assert store.get("retry-1") == ready


def test_hook가_CAS로_waiting전이하면_다음_run은_재호출하지_않아_idempotent하다() -> None:
    store = InMemoryQuestionRequestStore()
    _create_ready(store, "idempotent-1")
    calls = 0

    def recover_ready(request_id: str, revision: int, attempt: int) -> None:
        nonlocal calls
        calls += 1
        current = store.get(request_id)
        assert current is not None
        assert isinstance(current.state, ReadyToDispatch)
        updated = current.transition(
            AwaitingAnswer(
                route=current.state.route,
                attempt=attempt,
                ticket_id="ticket-idempotent",
                handling=_handling("runtime_ticket", "ticket-idempotent"),
            ),
            clock=lambda: _T2,
        )
        assert store.compare_and_set(request_id, revision, current, updated)

    runner = RequestRecoveryRunner(
        store,
        recover_received=lambda _request_id, _revision: None,
        recover_ready=recover_ready,
    )

    first = runner.run_once()
    second = runner.run_once()

    assert calls == 1
    assert first.ready_attempted == 1
    assert second.ready_attempted == 0
    assert second.waiting == 1


def test_동일runner의_동시_run_once는_lock으로_직렬화해_hook을_한번만_실행한다() -> None:
    store = InMemoryQuestionRequestStore()
    _create_ready(store, "concurrent-1")
    calls = 0
    calls_lock = threading.Lock()

    def recover_ready(request_id: str, revision: int, attempt: int) -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
        current = store.get(request_id)
        assert current is not None
        assert isinstance(current.state, ReadyToDispatch)
        updated = current.transition(
            AwaitingAnswer(
                route=current.state.route,
                attempt=attempt,
                ticket_id="ticket-concurrent",
                handling=_handling("runtime_ticket", "ticket-concurrent"),
            ),
            clock=lambda: _T2,
        )
        assert store.compare_and_set(request_id, revision, current, updated)

    runner = RequestRecoveryRunner(
        store,
        recover_received=lambda _request_id, _revision: None,
        recover_ready=recover_ready,
    )
    barrier = threading.Barrier(2)

    def run(_index: int) -> ReconcileReport:
        barrier.wait(timeout=2)
        return runner.run_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(run, range(2)))

    assert calls == 1
    assert sorted(report.ready_attempted for report in reports) == [0, 1]
    assert sorted(report.waiting for report in reports) == [0, 1]


def test_ReconcileReport와_RecoveryError는_frozen이다() -> None:
    error = RecoveryError("request", 0, "received", "ValueError", "오류")
    report = ReconcileReport(1, 1, 0, 0, 0, (error,))

    with pytest.raises(FrozenInstanceError):
        setattr(error, "message", "변조")
    with pytest.raises(FrozenInstanceError):
        setattr(report, "scanned", 99)


class _SnapshotChangingStore:
    """nonterminal snapshot과 재조회 사이의 외부 전이를 결정론적으로 재현."""

    def __init__(self, snapshot: QuestionRequest, changed: QuestionRequest) -> None:
        self._snapshot = snapshot
        self._changed = changed

    def create(self, request: QuestionRequest) -> QuestionRequest:
        raise AssertionError(f"사용하지 않는 create: {request.request_id}")

    def get(self, request_id: str) -> QuestionRequest | None:
        return self._changed if request_id == self._snapshot.request_id else None

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        raise AssertionError(
            f"사용하지 않는 CAS: {request_id}/{expected_revision}/{current}/{updated}"
        )

    def nonterminal(self) -> list[QuestionRequest]:
        return [self._snapshot]
