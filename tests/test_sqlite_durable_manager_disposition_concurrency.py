"""P17.9 S4.4e — Manager 처분(``DurableManagerDispositionUnitOfWork``) 경쟁/장애 게이트.

검증 전용 슬라이스: S4.4a~d의 동시성·장애 성질을 실 다중 커넥션으로 증명한다
(프로덕션 코드 변경 없음). 방식은 c.3c.4(``test_sqlite_durable_conflict_escalation_concurrency.py``)를
그대로 계승 — 독립 커넥션·barrier 동시-릴리즈·pause-release fault 주입·결정론 단언.
FromDeadlock 시나리오는 실 c.3 escalate UoW로 baseline을 커밋해 만든다(수동 조립 금지) —
``test_sqlite_durable_manager_disposition_uow`` 모듈의 helper를 그대로 재사용한다.
"""
from __future__ import annotations
# pyright: reportArgumentType=false, reportPrivateUsage=false

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import test_sqlite_durable_manager_disposition_uow as disposition_fixtures
from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.question_request import (
    AwaitingManager,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
)
from agent_org_network.sqlite_completion import (
    migrate_sqlite_completion_schema,
    validate_sqlite_completion_connection,
)
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    migrate_sqlite_durable_conflict_escalation_baseline_schema,
)
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    migrate_sqlite_durable_conflict_escalation_receipts_schema,
    open_sqlite_durable_conflict_escalation_receipts_connection,
    validate_sqlite_durable_conflict_escalation_receipts_connection,
)
from agent_org_network.sqlite_durable_conflict_escalation_reconciliation import (
    reconcile_sqlite_durable_conflict_escalation_gate,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
    validate_sqlite_durable_linked_aggregates_connection,
)
from agent_org_network.sqlite_durable_manager_disposition_uow import (
    DurableManagerAssignCommand,
    DurableManagerAssignTarget,
    DurableManagerDismissCommand,
    DurableManagerDismissed,
    DurableManagerDispositionCommand,
    DurableManagerDispositionConflict,
    DurableManagerDispositionUnitOfWork,
    DurableManagerOwnerAssigned,
    DurableManagerRegistry,
)

NOW = datetime(2026, 7, 22, tzinfo=UTC)
WORKERS = 16


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


@dataclass(frozen=True)
class _Scenario:
    org_id: str
    request_id: str
    item_id: str
    manager_subject_id: str


def _scenario(key: str) -> _Scenario:
    return _Scenario(
        org_id=_ref("org", f"org-{key}"),
        request_id=_ref("request", f"request-{key}"),
        item_id=_ref("manager", f"item-{key}"),
        manager_subject_id=f"manager-{key}",
    )


def _principal(scenario: _Scenario) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=scenario.org_id,
        subject_id=scenario.manager_subject_id,
        identity_provider="idp",
        identity_session_id="s1",
    )


def _seed_unowned(path: Path, scenarios: Sequence[_Scenario]) -> None:
    """각 시나리오를 FromUnowned 처분 대상(스펙 §7)으로 만든다: Received rev0 →
    AwaitingManager(unowned) rev1·ManagerItem awaiting_revision=0 open."""
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "seed-record",
        clock=lambda: NOW,
    )
    try:
        for scenario in scenarios:
            received = QuestionRequest.receive(
                org_id=scenario.org_id,
                requester_id="user",
                question="secret question",
                request_id_factory=lambda rid=scenario.request_id: rid,
                clock=lambda: NOW - timedelta(minutes=2),
                due_at=NOW + timedelta(hours=1),
            )
            completion.create(received)
            unowned_request = received.record_initial_routing(
                intent="refund",
                disposition="unowned",
                target=AwaitingManager(
                    item_id=scenario.item_id,
                    public_kind="unowned",
                    handling=HandlingAssignment(
                        kind="manager_item", ref=scenario.item_id, due_at=NOW + timedelta(hours=1)
                    ),
                ),
                clock=lambda: NOW - timedelta(minutes=1),
            )
            assert completion.compare_and_set(scenario.request_id, 0, received, unowned_request)
            tx = completion.durable_transaction()
            with tx.scope():
                tx.begin_immediate()
                tx.execute(
                    "INSERT INTO durable_linked_manager_items VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        scenario.item_id,
                        scenario.org_id,
                        scenario.request_id,
                        0,
                        "unowned",
                        _ref("source", scenario.request_id),
                        _ref("subject", scenario.manager_subject_id),
                        "open",
                        NOW.isoformat(),
                    ),
                )
                tx.commit()
    finally:
        completion.close()


class _Authority:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
        with self._lock:
            self.calls += 1
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,
            resource=resource,
            roles=("manager",),
            policy_version="v1",
            policy_digest="0" * 64,
        )

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: Action,
        resource: ResourceRef,
    ) -> bool:
        assert action == "manager.act"
        return True


class _NoopRegistry:
    """Dismiss 전용 경합에서는 절대 호출되면 안 되는 Registry — calls==0 회귀 앵커."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None:
        with self._lock:
            self.calls += 1
        return None


class _AssignRegistry:
    def __init__(self, *, agent_id: str = "card-a", requires_approval: bool = False) -> None:
        self.calls = 0
        self._lock = threading.Lock()
        self._target = DurableManagerAssignTarget(
            agent_id=agent_id,
            owner_subject_ref=_ref("subject", "owner-a"),
            requires_approval=requires_approval,
        )

    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None:
        with self._lock:
            self.calls += 1
        return self._target


class _IdFactory:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._count = 0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            self._count += 1
            return f"{self._prefix}-{self._count}"


def _open_uow(
    path: Path,
    *,
    index: int,
    authority: _Authority,
    registry: DurableManagerRegistry,
    receipt_ids: _IdFactory,
    fault_injector: Callable[[str], None] | None = None,
) -> tuple[SqliteQuestionCompletionUnitOfWork, DurableManagerDispositionUnitOfWork]:
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: f"record-{index}",
        clock=lambda: NOW,
        timeout=10.0,
    )
    uow = DurableManagerDispositionUnitOfWork(
        completion=completion,
        registry=registry,
        central_authorizer=authority,
        clock=lambda: NOW,
        receipt_id_factory=receipt_ids,
        fault_injector=fault_injector,
    )
    return completion, uow


def _verify_state(path: Path, request_id: str) -> QuestionRequest:
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "verify-record",
        clock=lambda: NOW,
    )
    try:
        request = completion.get(request_id)
        assert request is not None
        return request
    finally:
        completion.close()


def _assert_schema_capable(path: Path, org_id: str | None) -> None:
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "schema-verify-record",
        clock=lambda: NOW,
    )
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.validate_component_in_transaction(
                lambda connection: validate_sqlite_durable_linked_aggregates_connection(
                    connection, org_id=org_id
                )
            )
            tx.validate_component_in_transaction(validate_sqlite_completion_connection)
            tx.commit()
    finally:
        completion.close()


def _assert_single_dismiss_commit(path: Path, scenario: _Scenario) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        for table in (
            "durable_linked_command_receipts",
            "durable_linked_audit_intents",
            "durable_linked_outbox_intents",
        ):
            count = connection.execute(
                f"SELECT count(*) FROM {table} WHERE request_id=?", (scenario.request_id,)
            ).fetchone()[0]
            assert count == 1, f"{table}: expected 1 row for {scenario.request_id}, got {count}"
        item = connection.execute(
            "SELECT status FROM durable_linked_manager_items WHERE manager_item_id=?",
            (scenario.item_id,),
        ).fetchone()
        assert item is not None and item["status"] == "dismissed"
    finally:
        connection.close()
    request = _verify_state(path, scenario.request_id)
    assert isinstance(request.state, DeclinedRequest)
    assert request.state.reason_code == "manager_declined"
    assert request.revision == 2


def _assert_single_assign_commit(path: Path, scenario: _Scenario, *, agent_id: str) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        for table in (
            "durable_linked_command_receipts",
            "durable_linked_audit_intents",
            "durable_linked_outbox_intents",
        ):
            count = connection.execute(
                f"SELECT count(*) FROM {table} WHERE request_id=?", (scenario.request_id,)
            ).fetchone()[0]
            assert count == 1, f"{table}: expected 1 row for {scenario.request_id}, got {count}"
        item = connection.execute(
            "SELECT status FROM durable_linked_manager_items WHERE manager_item_id=?",
            (scenario.item_id,),
        ).fetchone()
        assert item is not None and item["status"] == "resolved"
    finally:
        connection.close()
    request = _verify_state(path, scenario.request_id)
    assert isinstance(request.state, ReadyToDispatch)
    assert request.state.route.agent_id == agent_id
    assert request.revision == 2


# ---------------------------------------------------------------------------
# 1. N-way(16) 동일 Dismiss command 경합 — 정확히 1 fresh write·나머지 replay
# ---------------------------------------------------------------------------


def test_16_way_동일_dismiss_command_경합은_정확히_1_fresh_write와_나머지_replay로_수렴한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same-dismiss.sqlite"
    scenario = _scenario("same-dismiss")
    _seed_unowned(path, [scenario])
    authority = _Authority()
    registry = _NoopRegistry()
    receipt_ids = _IdFactory("receipt")
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableManagerDispositionUnitOfWork] = []
    for index in range(WORKERS):
        completion, uow = _open_uow(
            path, index=index, authority=authority, registry=registry, receipt_ids=receipt_ids
        )
        completions.append(completion)
        uows.append(uow)
    barrier = threading.Barrier(WORKERS)
    principal = _principal(scenario)
    command = DurableManagerDismissCommand(scenario.item_id, scenario.request_id, 1)

    def run(uow: DurableManagerDispositionUnitOfWork) -> object:
        barrier.wait()
        return uow.act(principal=principal, command=command)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(run, uows))
    finally:
        for completion in completions:
            completion.close()

    assert len(results) == WORKERS
    assert all(isinstance(result, DurableManagerDismissed) for result in results)
    assert len(set(results)) == 1
    assert authority.calls == 1
    assert registry.calls == 0
    _assert_single_dismiss_commit(path, scenario)
    _assert_schema_capable(path, scenario.org_id)


# ---------------------------------------------------------------------------
# 2. 동일 item에 Assign vs Dismiss 혼합 경합 — 정확히 1 winner(다른 digest, replay 아님)
# ---------------------------------------------------------------------------


def test_assign_vs_dismiss_혼합_경합은_정확히_1_winner이고_나머지는_다른_digest_conflict이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed-assign-dismiss.sqlite"
    scenario = _scenario("mixed")
    _seed_unowned(path, [scenario])
    authority = _Authority()
    registry = _AssignRegistry(agent_id="card-a")
    receipt_ids = _IdFactory("receipt")
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableManagerDispositionUnitOfWork] = []
    commands: list[DurableManagerDispositionCommand] = []
    for index in range(WORKERS):
        completion, uow = _open_uow(
            path, index=index, authority=authority, registry=registry, receipt_ids=receipt_ids
        )
        completions.append(completion)
        uows.append(uow)
        if index % 2 == 0:
            commands.append(
                DurableManagerAssignCommand(
                    scenario.item_id, scenario.request_id, "card-a", 1, rationale=f"assign-{index}"
                )
            )
        else:
            commands.append(
                DurableManagerDismissCommand(
                    scenario.item_id, scenario.request_id, 1, rationale=f"dismiss-{index}"
                )
            )
    barrier = threading.Barrier(WORKERS)
    principal = _principal(scenario)

    def run(index: int) -> object:
        barrier.wait()
        try:
            return uows[index].act(principal=principal, command=commands[index])
        except DurableManagerDispositionConflict as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(run, range(WORKERS)))
    finally:
        for completion in completions:
            completion.close()

    successes = [
        result for result in results if isinstance(result, DurableManagerOwnerAssigned | DurableManagerDismissed)
    ]
    conflicts = [result for result in results if isinstance(result, DurableManagerDispositionConflict)]
    assert len(successes) == 1
    assert len(conflicts) == WORKERS - 1
    assert authority.calls == 1

    winner = successes[0]
    if isinstance(winner, DurableManagerOwnerAssigned):
        assert registry.calls == 1
        _assert_single_assign_commit(path, scenario, agent_id="card-a")
    else:
        assert registry.calls == 0
        _assert_single_dismiss_commit(path, scenario)
    _assert_schema_capable(path, scenario.org_id)


# ---------------------------------------------------------------------------
# 3. write fault × 경쟁 — pause-release fault 주입, 남은 스레드 중 1 winner
# ---------------------------------------------------------------------------

_FAULT_POINTS = (
    "after_receipt",
    "after_audit_intent",
    "after_outbox_intent",
    "after_manager_item",
    "after_request",
)


@pytest.mark.parametrize("point", _FAULT_POINTS)
def test_write_fault_지점을_경쟁_중_주입해도_partial_write_0이고_남은_스레드_중_1_winner가_된다(
    tmp_path: Path, point: str
) -> None:
    workers = 8
    fault_index = 0
    path = tmp_path / f"fault-{point}.sqlite"
    scenario = _scenario(f"fault-{point}")
    _seed_unowned(path, [scenario])
    authority = _Authority()
    registry = _NoopRegistry()
    receipt_ids = _IdFactory("receipt")
    paused = threading.Event()
    release = threading.Event()

    def pause_then_raise(injected: str) -> None:
        if injected == point:
            paused.set()
            assert release.wait(timeout=5)
            raise RuntimeError(injected)

    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableManagerDispositionUnitOfWork] = []
    for index in range(workers):
        completion, uow = _open_uow(
            path,
            index=index,
            authority=authority,
            registry=registry,
            receipt_ids=receipt_ids,
            fault_injector=pause_then_raise if index == fault_index else None,
        )
        completions.append(completion)
        uows.append(uow)
    principal = _principal(scenario)
    command = DurableManagerDismissCommand(scenario.item_id, scenario.request_id, 1)

    def run_fault() -> object:
        try:
            return uows[fault_index].act(principal=principal, command=command)
        except RuntimeError as error:
            return error

    def run_normal(index: int) -> object:
        return uows[index].act(principal=principal, command=command)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fault_future = pool.submit(run_fault)
            assert paused.wait(timeout=5)
            normal_futures = [
                pool.submit(run_normal, index) for index in range(workers) if index != fault_index
            ]
            release.set()
            fault_result = fault_future.result(timeout=5)
            normal_results = [future.result(timeout=5) for future in normal_futures]
    finally:
        for completion in completions:
            completion.close()

    assert type(fault_result) is RuntimeError and str(fault_result) == point
    assert len(set(normal_results)) == 1
    assert all(isinstance(result, DurableManagerDismissed) for result in normal_results)
    _assert_single_dismiss_commit(path, scenario)
    _assert_schema_capable(path, scenario.org_id)


# ---------------------------------------------------------------------------
# 4. FromDeadlock N-way(16) 동일 Assign command 경합 — Case escalated 불변·
#    처분 후 S4.3d reconciliation capable
# ---------------------------------------------------------------------------


def test_fromdeadlock_16_way_동일_assign_command_경합은_1_winner이고_case는_불변이며_reconciliation은_capable하다(
    tmp_path: Path,
) -> None:
    path, seed_completion = disposition_fixtures._deadlock_completion(
        tmp_path, name="deadlock-race.sqlite"
    )
    try:
        baseline = disposition_fixtures._deadlock_baseline(seed_completion)
    finally:
        seed_completion.close()

    authority = _Authority()
    registry = _AssignRegistry(agent_id="card-x")
    receipt_ids = _IdFactory("receipt")
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableManagerDispositionUnitOfWork] = []
    for index in range(WORKERS):
        completion, uow = _open_uow(
            path, index=index, authority=authority, registry=registry, receipt_ids=receipt_ids
        )
        completions.append(completion)
        uows.append(uow)
    barrier = threading.Barrier(WORKERS)
    principal = disposition_fixtures._deadlock_principal()
    command = disposition_fixtures._deadlock_assign_command(baseline)

    def run(uow: DurableManagerDispositionUnitOfWork) -> object:
        barrier.wait()
        return uow.act(principal=principal, command=command)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(run, uows))
    finally:
        for completion in completions:
            completion.close()

    assert len(results) == WORKERS
    assert all(isinstance(result, DurableManagerOwnerAssigned) for result in results)
    assert len(set(results)) == 1
    assert authority.calls == 1
    assert registry.calls == 1

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        case = connection.execute(
            "SELECT status, awaiting_revision FROM durable_linked_conflict_cases WHERE conflict_id=?",
            (disposition_fixtures.DEADLOCK_CONFLICT_ID,),
        ).fetchone()
    finally:
        connection.close()
    assert case is not None and case["status"] == "escalated" and case["awaiting_revision"] == 0

    request = _verify_state(path, disposition_fixtures.DEADLOCK_REQUEST_ID)
    assert isinstance(request.state, ReadyToDispatch)
    assert request.state.route.agent_id == "card-x"
    assert request.revision == 3

    report = reconcile_sqlite_durable_conflict_escalation_gate(
        path, org_id=disposition_fixtures.DEADLOCK_ORG_ID
    )
    assert report.capable is True
    assert report.violations == ()

    _assert_schema_capable(path, disposition_fixtures.DEADLOCK_ORG_ID)
    receipts_connection = open_sqlite_durable_conflict_escalation_receipts_connection(
        path, org_id=disposition_fixtures.DEADLOCK_ORG_ID
    )
    try:
        validate_sqlite_durable_conflict_escalation_receipts_connection(
            receipts_connection, org_id=disposition_fixtures.DEADLOCK_ORG_ID
        )
    finally:
        receipts_connection.close()


# ---------------------------------------------------------------------------
# 5. 다른 item(16) 병행 처분 — 전부 성공·교차 오염 0
# ---------------------------------------------------------------------------


def test_16_way_다른_item_병행_dismiss는_전부_성공하고_교차_오염이_없다(tmp_path: Path) -> None:
    path = tmp_path / "different-items.sqlite"
    scenarios = [_scenario(f"parallel-{index}") for index in range(WORKERS)]
    _seed_unowned(path, scenarios)
    authority = _Authority()
    registry = _NoopRegistry()
    receipt_ids = _IdFactory("receipt")
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableManagerDispositionUnitOfWork] = []
    for index in range(WORKERS):
        completion, uow = _open_uow(
            path, index=index, authority=authority, registry=registry, receipt_ids=receipt_ids
        )
        completions.append(completion)
        uows.append(uow)
    barrier = threading.Barrier(WORKERS)

    def run(item: tuple[_Scenario, DurableManagerDispositionUnitOfWork]) -> object:
        scenario, uow = item
        barrier.wait()
        command = DurableManagerDismissCommand(scenario.item_id, scenario.request_id, 1)
        return uow.act(principal=_principal(scenario), command=command)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(run, zip(scenarios, uows, strict=True)))
    finally:
        for completion in completions:
            completion.close()

    assert len(results) == WORKERS
    typed_results: list[DurableManagerDismissed] = []
    for scenario, result in zip(scenarios, results, strict=True):
        assert isinstance(result, DurableManagerDismissed)
        assert result.item_id == scenario.item_id
        assert result.request_id == scenario.request_id
        _assert_single_dismiss_commit(path, scenario)
        typed_results.append(result)
    receipt_ids_seen = {result.receipt_id for result in typed_results}
    assert len(receipt_ids_seen) == WORKERS
    assert authority.calls == WORKERS
    assert registry.calls == 0
    _assert_schema_capable(path, None)
