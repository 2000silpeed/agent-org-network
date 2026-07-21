"""P17.1 — InMemoryManagerQueueStore·ManagerQueueService 동시성 회귀.

FastAPI의 동기 라우트는 스레드풀에서 병렬 실행된다. 단순 메서드별 lock만으로는
`get → 판정/side effect → mark_resolved` 전체의 TOCTOU가 닫히지 않으므로,
Barrier로 모든 요청이 같은 open 항목을 먼저 읽게 한 뒤 전이 원자성을 검증한다.
실 sleep·LLM·네트워크는 사용하지 않는다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from agent_org_network.ask_org import AskOrg
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.conflict import (
    Candidate,
    ConflictCase,
    InMemoryConflictCaseStore,
    InMemoryPrecedentStore,
    Precedent,
    Resolution,
)
from agent_org_network.decision import Unowned
from agent_org_network.dispatch import LocalRuntimeDispatcher
from agent_org_network.manager_queue import (
    AssignOwner,
    Dismiss,
    FromDeadlock,
    FromUnowned,
    InMemoryManagerQueueStore,
    ManagerAction,
    ManagerItem,
    ManagerQueueService,
)
from agent_org_network.notify import FakeChannel, Notifier
from agent_org_network.runtime import StubRuntime

_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


class _IterationGatedDict(dict[str, ManagerItem]):
    """values 순회 중 writer가 store lock 획득을 시도할 때까지 reader를 붙든다.

    올바른 구현이면 writer가 lock 앞에서 대기하고 reader는 안정된 snapshot을 끝낸다.
    read lock이 빠지면 writer mutation이 활성 iterator와 겹쳐 RuntimeError가 재현된다.
    """

    def __init__(
        self,
        initial: dict[str, ManagerItem],
        iteration_started: threading.Event,
        writer_attempted_lock: threading.Event,
        reader_holds_lock: Callable[[], bool],
    ) -> None:
        super().__init__(initial)
        self._iteration_started = iteration_started
        self._writer_attempted_lock = writer_attempted_lock
        self._reader_holds_lock = reader_holds_lock

    def values(self) -> Any:  # type: ignore[override]
        iterator = iter(super().values())
        first = next(iterator)
        yield first
        self._iteration_started.set()
        if not self._writer_attempted_lock.wait(timeout=5.0):
            raise AssertionError("writer가 제한 시간 안에 store lock 획득을 시도하지 않았습니다.")
        # "writer가 lock 진입을 시도했다"는 신호만으로는 reader보다 먼저 mutation이
        # 일어났음을 보장하지 못해 lock 없는 구현도 스케줄링에 따라 통과할 수 있다.
        # 이 시점에 reader가 실제 lock owner인지 확인해 그 false green을 막는다.
        if not self._reader_holds_lock():
            raise AssertionError("pending snapshot 순회가 store lock 밖에서 실행됐습니다.")
        yield from iterator


class _WriterAttemptRLock:
    """writer의 lock 진입 시도를 신호하면서 실제 RLock 의미는 그대로 보존한다."""

    def __init__(self) -> None:
        self._delegate = threading.RLock()
        self.writer_ident: int | None = None
        self.writer_attempted = threading.Event()
        self._owner_ident: int | None = None
        self._depth = 0

    def __enter__(self) -> _WriterAttemptRLock:
        if threading.get_ident() == self.writer_ident:
            self.writer_attempted.set()
        self._delegate.acquire()
        ident = threading.get_ident()
        if self._owner_ident == ident:
            self._depth += 1
        else:
            self._owner_ident = ident
            self._depth = 1
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        if self._owner_ident != threading.get_ident() or self._depth < 1:
            raise AssertionError("RLock 소유자가 아닌 스레드가 lock을 해제했습니다.")
        self._depth -= 1
        if self._depth == 0:
            self._owner_ident = None
        self._delegate.release()

    def held_by_current_thread(self) -> bool:
        return self._owner_ident == threading.get_ident() and self._depth > 0


class _GetGatedManagerQueueStore(InMemoryManagerQueueStore):
    """모든 경쟁자가 같은 open item을 읽은 뒤에만 act를 계속하게 한다."""

    def __init__(self, parties: int) -> None:
        super().__init__()
        self._get_barrier = threading.Barrier(parties)

    def get(self, item_id: str) -> ManagerItem | None:
        item = super().get(item_id)
        if item is not None and item.status == "open":
            # timeout/BrokenBarrierError도 테스트 실패다. 경쟁 조건을 만들지 못했는데
            # 통과시키면 원자성 회귀를 놓치는 false green이 된다.
            self._get_barrier.wait(timeout=5.0)
        return item


class _DeadlockLookupGatedStore(InMemoryManagerQueueStore):
    """구현 전 get_by_case→enqueue TOCTOU를 결정론적으로 재현한다.

    기존 경로는 32개 호출이 모두 `None`을 읽은 뒤에야 진행한다. 원자 seam 경로는
    get_by_case를 호출하지 않으므로 이 barrier에 들어오지 않고 store 내부 CAS만 쓴다.
    """

    def __init__(self, parties: int) -> None:
        super().__init__()
        self._lookup_barrier = threading.Barrier(parties)

    def get_by_case(self, case_id: str) -> ManagerItem | None:
        item = super().get_by_case(case_id)
        self._lookup_barrier.wait(timeout=5.0)
        return item


class _FiveMethodManagerQueueStore:
    """선택적 원자 seam 없이 기존 공개 5메서드만 구현한 외부 store 대역."""

    def __init__(self) -> None:
        self._open: dict[str, ManagerItem] = {}
        self._by_case: dict[str, ManagerItem] = {}
        self._history: list[ManagerItem] = []

    def enqueue(self, item: ManagerItem) -> None:
        self._open[item.item_id] = item
        self._history.append(item)
        if isinstance(item.source, FromDeadlock):
            self._by_case[item.source.case.case_id] = item

    def get(self, item_id: str) -> ManagerItem | None:
        if item_id in self._open:
            return self._open[item_id]
        return next(
            (item for item in reversed(self._history) if item.item_id == item_id),
            None,
        )

    def pending_for_manager(self, manager_id: str) -> list[ManagerItem]:
        return [item for item in self._open.values() if item.manager_id == manager_id]

    def get_by_case(self, case_id: str) -> ManagerItem | None:
        return self._by_case.get(case_id)

    def mark_resolved(self, item: ManagerItem) -> None:
        self._open.pop(item.item_id, None)
        self._history.append(item)
        if isinstance(item.source, FromDeadlock):
            self._by_case.pop(item.source.case.case_id, None)


class _CountingPrecedentStore(InMemoryPrecedentStore):
    """AssignOwner의 외부 side effect 횟수를 스레드 안전하게 센다."""

    def __init__(self) -> None:
        super().__init__()
        self.record_calls = 0
        self._count_lock = threading.Lock()

    def record(self, resolution: Resolution) -> Precedent:
        with self._count_lock:
            self.record_calls += 1
            return super().record(resolution)


class _CountingConflictCaseStore(InMemoryConflictCaseStore):
    """AssignOwner의 ConflictCase 종결 side effect 횟수를 센다."""

    def __init__(self) -> None:
        super().__init__()
        self.mark_resolved_calls = 0
        self._count_lock = threading.Lock()

    def mark_resolved(self, case: ConflictCase) -> None:
        with self._count_lock:
            self.mark_resolved_calls += 1
            super().mark_resolved(case)


class _FailOnceConflictCaseStore(_CountingConflictCaseStore):
    """첫 mark_resolved는 commit 전에 실패하고 다음 호출부터 성공한다."""

    def __init__(self) -> None:
        super().__init__()
        self.mark_attempts = 0

    def mark_resolved(self, case: ConflictCase) -> None:
        self.mark_attempts += 1
        if self.mark_attempts == 1:
            raise RuntimeError("case store 일시 실패")
        super().mark_resolved(case)


class _FailOncePrecedentStore(_CountingPrecedentStore):
    """첫 record는 commit 전에 실패하고 다음 호출부터 성공한다."""

    def __init__(self) -> None:
        super().__init__()
        self.record_attempts = 0

    def record(self, resolution: Resolution) -> Precedent:
        self.record_attempts += 1
        if self.record_attempts == 1:
            raise RuntimeError("precedent store 일시 실패")
        return super().record(resolution)


def _unowned_item(item_id: str) -> ManagerItem:
    return ManagerItem(
        manager_id="root_manager",
        source=FromUnowned(
            decision=Unowned(escalated_to="root_manager", reason="후보 없음"),
            question=f"질문-{item_id}",
        ),
        created_at=_NOW,
        item_id=item_id,
    )


def _deadlock_case() -> ConflictCase:
    return ConflictCase(
        intent="보상",
        question="보상 기준이 어떻게 되나요?",
        candidates=(
            Candidate(agent_id="cs_ops", owner="cs_lead"),
            Candidate(agent_id="finance_ops", owner="finance_lead"),
        ),
        opened_at=_NOW,
        case_id="case-concurrent-act",
    )


def _ask_org_for_deadlock(
    store: InMemoryManagerQueueStore | _FiveMethodManagerQueueStore,
    notifier: Notifier,
) -> AskOrg:
    """enqueue_deadlock 전용 최소 조립. router는 이 테스트 경로에서 호출되지 않는다."""

    class _UnusedRouter:
        def route(self, question: str) -> None:  # noqa: ARG002
            raise AssertionError("enqueue_deadlock은 Router를 호출하면 안 됩니다.")

    return AskOrg(
        router=_UnusedRouter(),  # type: ignore[arg-type]
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        clock=lambda: _NOW,
        manager_queue_store=store,
        manager_root="root_manager",
        manager_of=lambda _owner: None,
        notifier=notifier,
    )


def _manager_service_with_deadlock(
    *,
    item_id: str,
    precedents: InMemoryPrecedentStore,
    cases: InMemoryConflictCaseStore,
) -> tuple[InMemoryManagerQueueStore, ManagerQueueService, AssignOwner]:
    store = InMemoryManagerQueueStore()
    case = _deadlock_case()
    cases.open_case(case)
    item = ManagerItem(
        manager_id="root_manager",
        source=FromDeadlock(case=case, reason="표 갈림"),
        created_at=_NOW,
        item_id=item_id,
    )
    store.enqueue(item)
    service = ManagerQueueService(
        queue_store=store,
        precedents=precedents,
        case_store=cases,
    )
    action = AssignOwner(by_manager="root_manager", primary="cs_ops")
    return store, service, action


def test_deadlock_원자_적재는_insert여부와_실제_open항목을_돌려준다() -> None:
    store = InMemoryManagerQueueStore()
    case = _deadlock_case()
    first = ManagerItem(
        manager_id="root_manager",
        source=FromDeadlock(case=case, reason="첫 교착"),
        created_at=_NOW,
        item_id="item-first-deadlock",
    )
    duplicate = ManagerItem(
        manager_id="root_manager",
        source=FromDeadlock(case=case, reason="중복 교착"),
        created_at=_NOW,
        item_id="item-duplicate-deadlock",
    )

    stored_first, inserted_first = store.enqueue_deadlock_if_absent(first)
    stored_duplicate, inserted_duplicate = store.enqueue_deadlock_if_absent(duplicate)

    assert (stored_first, inserted_first) == (first, True)
    assert (stored_duplicate, inserted_duplicate) == (first, False)
    assert store.pending_for_manager("root_manager") == [first]
    assert store.history == [first]


def test_공개_enqueue도_같은_deadlock_case의_orphan_open항목을_만들지_않는다() -> None:
    store = InMemoryManagerQueueStore()
    case = _deadlock_case()
    first = ManagerItem(
        manager_id="root_manager",
        source=FromDeadlock(case=case, reason="첫 교착"),
        created_at=_NOW,
        item_id="item-public-first",
    )
    duplicate = ManagerItem(
        manager_id="root_manager",
        source=FromDeadlock(case=case, reason="중복 교착"),
        created_at=_NOW,
        item_id="item-public-duplicate",
    )

    store.enqueue(first)
    store.enqueue(duplicate)

    assert store.pending_for_manager("root_manager") == [first]
    assert store.get_by_case(case.case_id) == first
    assert store.get(duplicate.item_id) is None
    assert store.history == [first]


def test_같은_deadlock_case_32개_동시_escalation은_적재와_통지가_1회다() -> None:
    workers = 32
    store = _DeadlockLookupGatedStore(workers)
    channel = FakeChannel()
    notifier = Notifier(subscriptions={"root_manager": channel})
    ask_org = _ask_org_for_deadlock(store, notifier)
    case = _deadlock_case()
    start = threading.Barrier(workers)

    def escalate(index: int) -> None:
        start.wait(timeout=5.0)
        ask_org.enqueue_deadlock(case, reason=f"교착-{index}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(escalate, range(workers)))

    pending = store.pending_for_manager("root_manager")
    assert len(pending) == 1
    assert len(store._open) == 1  # pyright: ignore[reportPrivateUsage]
    assert len(store.history) == 1
    assert store.history[0] == pending[0]
    assert len(channel.for_recipient("root_manager")) == 1


def test_원자_seam_없는_공개_5메서드_store는_기존_fallback으로_동작한다() -> None:
    store = _FiveMethodManagerQueueStore()
    channel = FakeChannel()
    notifier = Notifier(subscriptions={"root_manager": channel})
    ask_org = _ask_org_for_deadlock(store, notifier)
    case = _deadlock_case()

    ask_org.enqueue_deadlock(case, reason="첫 교착")
    ask_org.enqueue_deadlock(case, reason="중복 교착")

    pending = store.pending_for_manager("root_manager")
    assert len(pending) == 1
    assert len(channel.for_recipient("root_manager")) == 1


def test_공개_5메서드_store는_정상_순차처분_호환을_유지한다() -> None:
    """선택 seam 없는 구현은 정상 순차 경로만 호환하며 재시도 exactly-once는 범위 밖이다."""
    store = _FiveMethodManagerQueueStore()
    case = _deadlock_case()
    item = ManagerItem(
        manager_id="root_manager",
        source=FromDeadlock(case=case, reason="표 갈림"),
        created_at=_NOW,
        item_id="item-five-method-act",
    )
    store.enqueue(item)
    precedents = InMemoryPrecedentStore()
    cases = InMemoryConflictCaseStore()
    cases.open_case(case)
    service = ManagerQueueService(store, precedents, cases)

    resolved = service.act(
        item.item_id,
        AssignOwner(by_manager="root_manager", primary="cs_ops"),
    )

    assert resolved.status == "resolved"
    assert len(precedents.history) == 1
    assert cases.get(case.case_id) is None


def test_precedent성공_case실패_후_같은_action재시도는_side_effect를_중복하지_않는다() -> None:
    precedents = _CountingPrecedentStore()
    cases = _FailOnceConflictCaseStore()
    store, service, action = _manager_service_with_deadlock(
        item_id="item-retry-after-case-failure",
        precedents=precedents,
        cases=cases,
    )

    try:
        service.act("item-retry-after-case-failure", action)
    except RuntimeError as exc:
        assert str(exc) == "case store 일시 실패"
    else:
        raise AssertionError("첫 case side effect가 실패해야 합니다.")

    open_item = store.get("item-retry-after-case-failure")
    assert open_item is not None and open_item.status == "open"
    assert precedents.record_calls == 1
    assert cases.mark_attempts == 1
    assert cases.mark_resolved_calls == 0

    terminal = service.act("item-retry-after-case-failure", action)

    assert terminal.status == "resolved"
    assert terminal.resolution is not None
    assert terminal.resolution.action == action
    assert precedents.record_calls == 1
    assert len(precedents.history) == 1
    assert cases.mark_attempts == 2
    assert cases.mark_resolved_calls == 1
    assert sum(entry.status == "resolved" for entry in store.history) == 1


def test_부분성공_후_다른_action재시도는_상충으로_거부한다() -> None:
    precedents = _CountingPrecedentStore()
    cases = _FailOnceConflictCaseStore()
    store, service, action = _manager_service_with_deadlock(
        item_id="item-conflicting-retry",
        precedents=precedents,
        cases=cases,
    )

    try:
        service.act("item-conflicting-retry", action)
    except RuntimeError:
        pass
    else:
        raise AssertionError("첫 case side effect가 실패해야 합니다.")

    conflicting = Dismiss(by_manager="root_manager", rationale="다른 결론")
    try:
        service.act("item-conflicting-retry", conflicting)
    except ValueError as exc:
        assert "상충" in str(exc)
    else:
        raise AssertionError("부분 성공 뒤 다른 action은 명시적으로 거부해야 합니다.")

    current = store.get("item-conflicting-retry")
    assert current is not None and current.status == "open"
    assert precedents.record_calls == 1
    assert cases.mark_resolved_calls == 0
    assert cases.get(_deadlock_case().case_id) is not None
    assert sum(entry.status == "resolved" for entry in store.history) == 0


def test_첫_effect가_commit전_실패하면_같은_action으로_재시도할수있다() -> None:
    precedents = _FailOncePrecedentStore()
    cases = _CountingConflictCaseStore()
    store, service, action = _manager_service_with_deadlock(
        item_id="item-retry-first-effect",
        precedents=precedents,
        cases=cases,
    )

    try:
        service.act("item-retry-first-effect", action)
    except RuntimeError as exc:
        assert str(exc) == "precedent store 일시 실패"
    else:
        raise AssertionError("첫 precedent side effect가 실패해야 합니다.")

    open_item = store.get("item-retry-first-effect")
    assert open_item is not None and open_item.status == "open"
    assert precedents.record_calls == 0
    assert cases.mark_resolved_calls == 0

    terminal = service.act("item-retry-first-effect", action)

    assert terminal.status == "resolved"
    assert precedents.record_attempts == 2
    assert precedents.record_calls == 1
    assert cases.mark_resolved_calls == 1
    assert sum(entry.status == "resolved" for entry in store.history) == 1


def test_concurrent_enqueue와_pending_조회는_예외없이_일관된_snapshot을_낸다() -> None:
    store = InMemoryManagerQueueStore()
    first = _unowned_item("item-first")
    second = _unowned_item("item-second")
    store.enqueue(first)

    iteration_started = threading.Event()
    coordinated_lock = _WriterAttemptRLock()
    store._lock = coordinated_lock  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    store._open = _IterationGatedDict(  # pyright: ignore[reportPrivateUsage]
        dict(store._open),  # pyright: ignore[reportPrivateUsage]
        iteration_started,
        coordinated_lock.writer_attempted,
        coordinated_lock.held_by_current_thread,
    )

    def read_pending() -> list[ManagerItem]:
        return store.pending_for_manager("root_manager")

    def enqueue_second() -> None:
        assert iteration_started.wait(timeout=1.0)
        coordinated_lock.writer_ident = threading.get_ident()
        store.enqueue(second)

    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(read_pending)
        write_future = pool.submit(enqueue_second)
        snapshot = read_future.result()
        write_future.result()

    assert [item.item_id for item in snapshot] == ["item-first"]
    final = store.pending_for_manager("root_manager")
    assert [item.item_id for item in final] == ["item-first", "item-second"]


def test_같은_open_item_32개_동시_Dismiss_AssignOwner는_전이와_side_effect가_1회다() -> None:
    workers = 32
    store = _GetGatedManagerQueueStore(workers)
    case = _deadlock_case()
    item = ManagerItem(
        manager_id="root_manager",
        source=FromDeadlock(case=case, reason="표 갈림"),
        created_at=_NOW,
        item_id="item-concurrent-act",
    )
    store.enqueue(item)

    precedents = _CountingPrecedentStore()
    cases = _CountingConflictCaseStore()
    cases.open_case(case)
    services = [
        ManagerQueueService(queue_store=store, precedents=precedents, case_store=cases)
        for _ in range(workers)
    ]
    actions: list[ManagerAction] = [
        (
            AssignOwner(by_manager="root_manager", primary="cs_ops")
            if index % 2 == 0
            else Dismiss(by_manager="root_manager", rationale="중복")
        )
        for index in range(workers)
    ]

    def act(index: int) -> ManagerItem:
        return services[index].act(item.item_id, actions[index])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(act, range(workers)))

    final = store.get(item.item_id)
    assert final is not None
    assert final.status == "resolved"
    assert final.resolution is not None
    assert all(result == final for result in results)
    assert sum(entry.status == "resolved" for entry in store.history) == 1

    winning_action = final.resolution.action
    if isinstance(winning_action, AssignOwner):
        assert precedents.record_calls == 1
        assert cases.mark_resolved_calls == 1
    else:
        assert isinstance(winning_action, Dismiss)
        assert precedents.record_calls == 0
        assert cases.mark_resolved_calls == 0


def test_같은_open_item_32개_동시_AssignOwner는_외부_side_effect도_정확히_1회다() -> None:
    workers = 32
    store = _GetGatedManagerQueueStore(workers)
    case = _deadlock_case()
    item = ManagerItem(
        manager_id="root_manager",
        source=FromDeadlock(case=case, reason="표 갈림"),
        created_at=_NOW,
        item_id="item-concurrent-assign",
    )
    store.enqueue(item)

    precedents = _CountingPrecedentStore()
    cases = _CountingConflictCaseStore()
    cases.open_case(case)
    services = [
        ManagerQueueService(queue_store=store, precedents=precedents, case_store=cases)
        for _ in range(workers)
    ]
    action = AssignOwner(by_manager="root_manager", primary="cs_ops")

    def act(service: ManagerQueueService) -> ManagerItem:
        return service.act(item.item_id, action)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(act, services))

    final = store.get(item.item_id)
    assert final is not None
    assert final.status == "resolved"
    assert all(result == final for result in results)
    assert sum(entry.status == "resolved" for entry in store.history) == 1
    assert precedents.record_calls == 1
    assert cases.mark_resolved_calls == 1
