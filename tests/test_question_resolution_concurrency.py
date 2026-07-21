from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier, Lock

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.conflict import (
    Candidate,
    ConflictCase,
    InMemoryConflictCaseStore,
    Resolution,
)
from agent_org_network.decision import Contested, Routed, RoutingDecision, Unowned
from agent_org_network.manager_queue import (
    Dismiss,
    FromUnowned,
    InMemoryManagerQueueStore,
    ManagerItem,
    ManagerResolution,
)
from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingConflict,
    AwaitingManager,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    ReadyToDispatch,
)
from agent_org_network.question_resolution import (
    AuthorityGrant,
    ConcurrentInitialRoutingError,
    InitialRoutingDependencyError,
    InitialRoutingConflictError,
    QuestionResolutionApplication,
    RequestLockPool,
    RequestPending,
)
from agent_org_network.request_correlation import LinkedEntityMismatchError


NOW = datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)


def _card(agent_id: str, owner: str) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="support",
        summary=agent_id,
        domains=["refund"],
        last_reviewed_at=date(2026, 7, 1),
    )


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        return started_at + timedelta(hours=1)


class _Authority:
    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant:
        return AuthorityGrant(policy_version="rules-v1")


class _CorruptibleManagerStore(InMemoryManagerQueueStore):
    """복구 fail-closed 테스트가 정상 public write를 우회해 손상 상태만 만든다."""

    def force_resolve_for_corruption_test(self, item: ManagerItem) -> None:
        with self._lock:
            self._mark_resolved_unlocked(item)


class _CorruptibleConflictStore(InMemoryConflictCaseStore):
    """복구 fail-closed 테스트가 claim-bound public 전이를 우회해 손상만 만든다."""

    def force_transition_for_corruption_test(self, case: ConflictCase) -> None:
        with self._lock:
            self._replace_request_case_unlocked(case)


class _CountingRouter:
    def __init__(self, decision: RoutingDecision | Exception) -> None:
        self.decision: RoutingDecision | Exception = decision
        self.calls: int = 0
        self._lock = Lock()

    def route(self, question: str) -> RoutingDecision:
        with self._lock:
            self.calls += 1
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


class _BarrierRouter(_CountingRouter):
    def __init__(self, decision: RoutingDecision, barrier: Barrier) -> None:
        super().__init__(decision)
        self._barrier = barrier

    def route(self, question: str) -> RoutingDecision:
        with self._lock:
            self.calls += 1
        self._barrier.wait(timeout=5)
        assert not isinstance(self.decision, Exception)
        return self.decision


def _received(
    store: InMemoryQuestionRequestStore,
    *,
    request_id: str = "req-1",
    question: str = "환불 규정은?",
) -> QuestionRequest:
    request = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question=question,
        request_id_factory=lambda: request_id,
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )
    store.create(request)
    return request


def _app(
    *,
    requests: InMemoryQuestionRequestStore,
    router: _CountingRouter,
    conflicts: InMemoryConflictCaseStore | None = None,
    managers: InMemoryManagerQueueStore | None = None,
    request_locks: RequestLockPool | None = None,
) -> QuestionResolutionApplication:
    return QuestionResolutionApplication(
        requests=requests,
        router=router,
        conflicts=conflicts or InMemoryConflictCaseStore(),
        managers=managers or InMemoryManagerQueueStore(),
        route_authority=_Authority(),
        deadline_policy=_Deadline(),
        request_id_factory=lambda: "unused",
        clock=lambda: NOW,
        request_locks=request_locks,
    )


def _contested() -> Contested:
    return Contested(
        candidates=(
            _card("refund-owner", "owner-1"),
            _card("refund-backup", "owner-2"),
        ),
        intent="refund",
    )


def test_same_application_32_way_advance_routes_once_and_lock_pool_cleans_up() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    conflicts = _CorruptibleConflictStore()
    locks = RequestLockPool()
    router = _CountingRouter(_contested())
    app = _app(
        requests=requests,
        router=router,
        conflicts=conflicts,
        request_locks=locks,
    )
    start = Barrier(32)

    def advance() -> RequestPending:
        start.wait(timeout=5)
        outcome = app.advance("req-1", expected_revision=0)
        assert isinstance(outcome, RequestPending)
        return outcome

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(advance) for _ in range(32)]
        outcomes = [future.result(timeout=5) for future in futures]

    assert {outcome.state for outcome in outcomes} == {"awaiting_conflict"}
    assert router.calls == 1
    assert len(conflicts.history) == 1
    assert locks.active_count == 0


def test_initial_convergence_does_not_accept_a_later_lifecycle_revision() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    router = _CountingRouter(Routed(primary=_card("refund-owner", "owner-1"), intent="refund"))
    app = _app(requests=requests, router=router)
    app.advance("req-1", expected_revision=0)
    ready = requests.get("req-1")
    assert ready is not None
    assert isinstance(ready.state, ReadyToDispatch)
    awaiting_answer = ready.transition(
        AwaitingAnswer(
            route=ready.state.route,
            attempt=ready.state.attempt,
            ticket_id="ticket-1",
            handling=HandlingAssignment(
                kind="runtime_ticket",
                ref="ticket-1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW,
    )
    assert requests.compare_and_set("req-1", 1, ready, awaiting_answer)
    assert isinstance(awaiting_answer.state, AwaitingAnswer)
    awaiting_manager = awaiting_answer.transition(
        AwaitingManager(
            item_id="item-1",
            public_kind="dispatched",
            route=awaiting_answer.state.route,
            attempt=awaiting_answer.state.attempt,
            handling=HandlingAssignment(
                kind="manager_item",
                ref="item-1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW,
    )
    assert requests.compare_and_set("req-1", 2, awaiting_answer, awaiting_manager)

    with pytest.raises(InitialRoutingConflictError):
        app.advance("req-1", expected_revision=2)


def test_lock_entry_is_released_after_router_failure_and_retry_can_progress() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    locks = RequestLockPool()
    router = _CountingRouter(RuntimeError("temporary"))
    app = _app(requests=requests, router=router, request_locks=locks)

    with pytest.raises(InitialRoutingDependencyError):
        app.advance("req-1", expected_revision=0)
    assert locks.active_count == 0

    router.decision = Routed(primary=_card("refund-owner", "owner-1"), intent="refund")
    outcome = app.advance("req-1", expected_revision=0)

    assert isinstance(outcome, RequestPending)
    assert outcome.state == "ready_to_dispatch"
    assert router.calls == 2
    assert locks.active_count == 0


def test_existing_conflict_orphan_is_reconciled_before_router() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    conflicts = _CorruptibleConflictStore()
    case = ConflictCase.for_request(
        request_id="req-1",
        intent="refund",
        question="환불 규정은?",
        candidates=(
            Candidate(agent_id="refund-backup", owner="owner-2"),
            Candidate(agent_id="refund-owner", owner="owner-1"),
        ),
        opened_at=NOW,
        case_id="case-existing",
    )
    conflicts.create_or_get_for_request(case)
    router = _CountingRouter(AssertionError("Router must not be called"))
    app = _app(requests=requests, router=router, conflicts=conflicts)

    outcome = app.advance("req-1", expected_revision=0)

    assert isinstance(outcome, RequestPending)
    assert outcome.state == "awaiting_conflict"
    assert router.calls == 0
    stored = requests.get("req-1")
    assert stored is not None
    assert isinstance(stored.state, AwaitingConflict)
    assert stored.state.handling.ref == "case-existing"


def test_existing_unowned_orphan_is_reconciled_before_router() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    managers = InMemoryManagerQueueStore()
    decision = Unowned(escalated_to="root-user", intent="")
    item = ManagerItem.for_request(
        request_id="req-1",
        manager_id="root-user",
        source=FromUnowned(decision=decision, question="환불 규정은?"),
        created_at=NOW,
        item_id="item-existing",
    )
    managers.create_or_get_for_request(item)
    router = _CountingRouter(AssertionError("Router must not be called"))
    app = _app(requests=requests, router=router, managers=managers)

    outcome = app.advance("req-1", expected_revision=0)

    assert isinstance(outcome, RequestPending)
    assert outcome.state == "awaiting_manager"
    assert router.calls == 0
    stored = requests.get("req-1")
    assert stored is not None
    assert stored.intent is None
    assert isinstance(stored.state, AwaitingManager)
    assert stored.state.handling.ref == "item-existing"


def test_both_linked_orphans_fail_without_router_or_cas() -> None:
    requests = InMemoryQuestionRequestStore()
    original = _received(requests)
    conflicts = InMemoryConflictCaseStore()
    conflicts.create_or_get_for_request(
        ConflictCase.for_request(
            request_id="req-1",
            intent="refund",
            question=original.question,
            candidates=(
                Candidate(agent_id="a", owner="oa"),
                Candidate(agent_id="b", owner="ob"),
            ),
            opened_at=NOW,
        )
    )
    managers = InMemoryManagerQueueStore()
    decision = Unowned(escalated_to="root-user")
    managers.create_or_get_for_request(
        ManagerItem.for_request(
            request_id="req-1",
            manager_id="root-user",
            source=FromUnowned(decision=decision, question=original.question),
            created_at=NOW,
        )
    )
    router = _CountingRouter(AssertionError("Router must not be called"))
    app = _app(
        requests=requests,
        router=router,
        conflicts=conflicts,
        managers=managers,
    )

    with pytest.raises(LinkedEntityMismatchError):
        app.advance("req-1", expected_revision=0)

    assert router.calls == 0
    assert requests.get("req-1") == original


@pytest.mark.parametrize("resolved", [False, True])
def test_conflict_orphan_with_wrong_question_or_closed_state_fails_closed(
    resolved: bool,
) -> None:
    requests = InMemoryQuestionRequestStore()
    original = _received(requests)
    conflicts = _CorruptibleConflictStore()
    case = ConflictCase.for_request(
        request_id="req-1",
        intent="refund",
        question="다른 질문" if not resolved else original.question,
        candidates=(
            Candidate(agent_id="a", owner="oa"),
            Candidate(agent_id="b", owner="ob"),
        ),
        opened_at=NOW,
    )
    conflicts.create_or_get_for_request(case)
    if resolved:
        conflicts.force_transition_for_corruption_test(
            case.resolve(Resolution(intent="refund", primary="a"))
        )
    router = _CountingRouter(AssertionError("Router must not be called"))
    app = _app(requests=requests, router=router, conflicts=conflicts)

    with pytest.raises(LinkedEntityMismatchError):
        app.advance("req-1", expected_revision=0)

    assert router.calls == 0
    assert requests.get("req-1") == original


def test_manager_orphan_that_is_closed_fails_closed() -> None:
    requests = InMemoryQuestionRequestStore()
    original = _received(requests)
    managers = _CorruptibleManagerStore()
    decision = Unowned(escalated_to="root-user")
    item = ManagerItem.for_request(
        request_id="req-1",
        manager_id="root-user",
        source=FromUnowned(decision=decision, question=original.question),
        created_at=NOW,
    )
    managers.create_or_get_for_request(item)
    managers.force_resolve_for_corruption_test(
        item.resolve(ManagerResolution(action=Dismiss(by_manager="root-user")))
    )
    router = _CountingRouter(AssertionError("Router must not be called"))
    app = _app(requests=requests, router=router, managers=managers)

    with pytest.raises(LinkedEntityMismatchError):
        app.advance("req-1", expected_revision=0)

    assert router.calls == 0
    assert requests.get("req-1") == original


def test_stale_initial_advance_rejects_resolved_conflict_ahead_of_request() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    conflicts = _CorruptibleConflictStore()
    app = _app(
        requests=requests,
        router=_CountingRouter(_contested()),
        conflicts=conflicts,
    )
    app.advance("req-1", expected_revision=0)
    case = conflicts.get_by_request("req-1")
    assert case is not None
    conflicts.force_transition_for_corruption_test(
        case.resolve(Resolution(intent=case.intent, primary="refund-owner"))
    )

    with pytest.raises(LinkedEntityMismatchError):
        app.advance("req-1", expected_revision=0)


def test_stale_initial_advance_rejects_resolved_manager_item_ahead_of_request() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    managers = _CorruptibleManagerStore()
    app = _app(
        requests=requests,
        router=_CountingRouter(Unowned(escalated_to="root-user")),
        managers=managers,
    )
    app.advance("req-1", expected_revision=0)
    item = managers.get_by_request("req-1")
    assert item is not None
    managers.force_resolve_for_corruption_test(
        item.resolve(ManagerResolution(action=Dismiss(by_manager="root-user")))
    )

    with pytest.raises(LinkedEntityMismatchError):
        app.advance("req-1", expected_revision=0)


def test_two_app_instances_same_contested_winner_converge_after_cas_loss() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    conflicts = InMemoryConflictCaseStore()
    barrier = Barrier(2)
    left_router = _BarrierRouter(_contested(), barrier)
    right_router = _BarrierRouter(_contested(), barrier)
    left = _app(requests=requests, router=left_router, conflicts=conflicts)
    right = _app(requests=requests, router=right_router, conflicts=conflicts)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(left.advance, "req-1", expected_revision=0),
            executor.submit(right.advance, "req-1", expected_revision=0),
        ]
        outcomes = [future.result(timeout=5) for future in futures]

    assert all(isinstance(outcome, RequestPending) for outcome in outcomes)
    assert {outcome.state for outcome in outcomes if isinstance(outcome, RequestPending)} == {
        "awaiting_conflict"
    }
    total_router_calls: int = left_router.calls + right_router.calls
    assert total_router_calls == 2
    assert len(conflicts.history) == 1


def test_two_app_instances_different_routed_winners_report_explicit_conflict() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    barrier = Barrier(2)
    left = _app(
        requests=requests,
        router=_BarrierRouter(
            Routed(primary=_card("refund-owner", "owner-1"), intent="refund"),
            barrier,
        ),
    )
    right = _app(
        requests=requests,
        router=_BarrierRouter(
            Routed(primary=_card("refund-backup", "owner-2"), intent="refund"),
            barrier,
        ),
    )

    results: list[object] = []

    def run(app: QuestionResolutionApplication) -> None:
        try:
            results.append(app.advance("req-1", expected_revision=0))
        except Exception as error:  # 결과와 명시 오류를 함께 수집
            results.append(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run, (left, right)))

    assert sum(isinstance(value, RequestPending) for value in results) == 1
    assert sum(isinstance(value, ConcurrentInitialRoutingError) for value in results) == 1


def test_cross_app_contested_unowned_race_is_not_hidden_on_later_advance() -> None:
    requests = InMemoryQuestionRequestStore()
    _received(requests)
    conflicts = InMemoryConflictCaseStore()
    managers = InMemoryManagerQueueStore()
    barrier = Barrier(2)
    contested_app = _app(
        requests=requests,
        router=_BarrierRouter(_contested(), barrier),
        conflicts=conflicts,
        managers=managers,
    )
    unowned_app = _app(
        requests=requests,
        router=_BarrierRouter(Unowned(escalated_to="root-user"), barrier),
        conflicts=conflicts,
        managers=managers,
    )
    first_results: list[object] = []

    def run(app: QuestionResolutionApplication) -> None:
        try:
            first_results.append(app.advance("req-1", expected_revision=0))
        except Exception as error:
            first_results.append(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run, (contested_app, unowned_app)))

    assert sum(isinstance(value, RequestPending) for value in first_results) == 1
    assert sum(isinstance(value, ConcurrentInitialRoutingError) for value in first_results) == 1
    assert conflicts.get_by_request("req-1") is not None
    assert managers.get_by_request("req-1") is not None

    with pytest.raises(LinkedEntityMismatchError):
        contested_app.advance("req-1", expected_revision=0)


def test_linked_writer_failure_leaves_received_and_retry_uses_created_orphan() -> None:
    class WriteThenFailOnce(InMemoryConflictCaseStore):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def create_or_get_for_request(
            self,
            case: ConflictCase,
        ) -> tuple[ConflictCase, bool]:
            result = super().create_or_get_for_request(case)
            if not self.failed:
                self.failed = True
                raise RuntimeError("connection dropped after write")
            return result

    requests = InMemoryQuestionRequestStore()
    original = _received(requests)
    conflicts = WriteThenFailOnce()
    router = _CountingRouter(_contested())
    app = _app(requests=requests, router=router, conflicts=conflicts)

    with pytest.raises(InitialRoutingDependencyError):
        app.advance("req-1", expected_revision=0)
    assert requests.get("req-1") == original
    assert len(conflicts.history) == 1

    outcome = app.advance("req-1", expected_revision=0)

    assert isinstance(outcome, RequestPending)
    assert outcome.state == "awaiting_conflict"
    assert router.calls == 1
    assert len(conflicts.history) == 1
