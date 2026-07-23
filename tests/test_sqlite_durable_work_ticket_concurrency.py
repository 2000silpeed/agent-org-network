"""P17.9 S4.5b — durable WorkTicket enqueue(``DurableWorkTicketEnqueueUnitOfWork``)
경쟁/장애 게이트.

검증 전용 슬라이스: S4.5a의 동시성·장애 성질을 실 다중 커넥션으로 증명한다
(프로덕션 코드 변경 없음). 방식은 S4.4e(``test_sqlite_durable_manager_disposition_concurrency.py``)
·S4.3c.4(``test_sqlite_durable_conflict_escalation_concurrency.py``)를 그대로 계승
— 독립 커넥션·barrier 동시-릴리즈·pause-release fault 주입·결정론 단언, 커넥션
timeout 10s.

S4.5a 독립 최종 리뷰 P2 이월 2건도 이 파일에서 마감한다:
- P2-2: ``_stored_result``의 receipt 하위검사(target_kind·request_id·
  expected_request_revision) 개별 변조 → Unavailable red 3건(단일 커넥션).
- P2-3(필수): commit-time ``compare_and_set_question_request`` False 분기 red.
  ``BEGIN IMMEDIATE``는 read-time guard부터 CAS까지 같은 SQLite transaction
  안에서 write lock을 계속 쥔다(RESERVED lock은 단일 holder만 허용) — 그래서
  진짜 다중 커넥션 경쟁으로는 guard를 통과한 뒤 CAS만 False가 되는 경로를
  만들 수 없다(다른 writer는 이 lock이 풀리기 전까지 절대 끼어들 수 없고,
  lock을 얻은 다음 읽으면 이미 read-time guard가 먼저 막는다 — S4.5a red 4
  동형). 이 파일은 CAS 호출 자체만 결정론적으로 가로채 False를 강제하는
  얇은 delegate로 이 분기를 겨냥하고, 그 외 모든 read/write/commit/rollback은
  실 ``SqliteCompletionTransaction``에 위임해 진짜 rollback·partial write 0을
  실측한다. 이어서 같은 DB에서 real 다중 커넥션 경쟁이 정상적으로 승자를
  낸다는 것까지 검증해 이 강제 경로가 DB를 오염시키지 않았음을 증명한다.
"""

from __future__ import annotations
# pyright: reportArgumentType=false, reportPrivateUsage=false

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.question_request import (
    AwaitingAnswer,
    FailedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_completion import (
    migrate_sqlite_completion_schema,
    validate_sqlite_completion_connection,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
    validate_sqlite_durable_linked_aggregates_connection,
)
from agent_org_network.sqlite_durable_work_ticket_uow import (
    DurableWorkTicketConflict,
    DurableWorkTicketEnqueueCommand,
    DurableWorkTicketEnqueued,
    DurableWorkTicketEnqueueUnitOfWork,
    DurableWorkTicketRegistry,
    DurableWorkTicketUnavailable,
)

NOW = datetime(2026, 7, 23, tzinfo=UTC)
WORKERS = 32


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


@dataclass(frozen=True)
class _Scenario:
    org_id: str
    request_id: str
    agent_id: str
    owner: str


def _scenario(key: str) -> _Scenario:
    return _Scenario(
        org_id=_ref("org", f"org-{key}"),
        request_id=_ref("request", f"request-{key}"),
        agent_id=f"card-{key}",
        owner=_ref("subject", f"owner-{key}"),
    )


def _seed_scenarios(path: Path, scenarios: Sequence[_Scenario]) -> None:
    """각 시나리오를 Received rev0 → ReadyToDispatch rev1(routed)로 직행시킨다
    (S4.5a ``_seed_ready``의 다중 시나리오 판)."""
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
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
                question="refund question",
                request_id_factory=lambda rid=scenario.request_id: rid,
                clock=lambda: NOW - timedelta(minutes=2),
                due_at=NOW + timedelta(hours=1),
            )
            completion.create(received)
            trigger_ref = _ref("trigger", scenario.request_id)
            ready = received.record_initial_routing(
                intent="refund",
                disposition="routed",
                target=ReadyToDispatch(
                    route=RouteTarget(
                        intent="refund", agent_id=scenario.agent_id, requires_approval=False
                    ),
                    attempt=1,
                    trigger_key=trigger_ref,
                    handling=HandlingAssignment(
                        kind="system", ref=trigger_ref, due_at=NOW + timedelta(hours=1)
                    ),
                ),
                clock=lambda: NOW - timedelta(minutes=1),
            )
            assert completion.compare_and_set(scenario.request_id, 0, received, ready)
    finally:
        completion.close()


class _Registry:
    """owner 주소 해석 Fake — 고정 owner 또는 (org_id, agent_id)별 매핑을 지원한다."""

    def __init__(
        self,
        *,
        owner: str | None = None,
        owners: dict[tuple[str, str], str] | None = None,
        raises: bool = False,
    ) -> None:
        self._owner = owner
        self._owners = owners
        self._raises = raises
        self.calls = 0
        self._lock = threading.Lock()

    def resolve_owner_subject(self, *, org_id: str, agent_id: str) -> str | None:
        with self._lock:
            self.calls += 1
        if self._raises:
            raise RuntimeError("registry unavailable")
        if self._owners is not None:
            return self._owners.get((org_id, agent_id))
        return self._owner


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
    registry: DurableWorkTicketRegistry,
    ticket_ids: _IdFactory,
    receipt_ids: _IdFactory,
    fault_injector: Callable[[str], None] | None = None,
) -> tuple[SqliteQuestionCompletionUnitOfWork, DurableWorkTicketEnqueueUnitOfWork]:
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: f"record-{index}",
        clock=lambda: NOW,
        timeout=10.0,
    )
    uow = DurableWorkTicketEnqueueUnitOfWork(
        completion=completion,
        registry=registry,
        clock=lambda: NOW,
        ticket_id_factory=ticket_ids,
        receipt_id_factory=receipt_ids,
        fault_injector=fault_injector,
    )
    return completion, uow


def _command(
    request_id: str, *, expected_request_revision: int = 1, attempt: int = 1
) -> DurableWorkTicketEnqueueCommand:
    return DurableWorkTicketEnqueueCommand(request_id, expected_request_revision, attempt)


def _row_counts_for(path: Path, request_id: str) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: connection.execute(
                f"SELECT count(*) FROM {table} WHERE request_id=?", (request_id,)
            ).fetchone()[0]
            for table in (
                "durable_linked_command_receipts",
                "durable_linked_audit_intents",
                "durable_linked_outbox_intents",
                "durable_linked_work_tickets",
            )
        }
    finally:
        connection.close()


def _final_request(path: Path, request_id: str) -> QuestionRequest:
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


# ---------------------------------------------------------------------------
# 1. 32-way 동일 enqueue command 경합 — 정확히 1 fresh write·31 멱등 replay
# ---------------------------------------------------------------------------


def test_32_way_동일_enqueue_command_경합은_정확히_1_fresh_write와_31_replay로_수렴한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same-command.sqlite"
    scenario = _scenario("same-command")
    _seed_scenarios(path, [scenario])
    registry = _Registry(owner=scenario.owner)
    ticket_ids = _IdFactory("ticket")
    receipt_ids = _IdFactory("receipt")
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableWorkTicketEnqueueUnitOfWork] = []
    for index in range(WORKERS):
        completion, uow = _open_uow(
            path, index=index, registry=registry, ticket_ids=ticket_ids, receipt_ids=receipt_ids
        )
        completions.append(completion)
        uows.append(uow)
    barrier = threading.Barrier(WORKERS)
    command = _command(scenario.request_id)

    def run(uow: DurableWorkTicketEnqueueUnitOfWork) -> object:
        barrier.wait()
        return uow.enqueue(command=command)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(run, uows))
    finally:
        for completion in completions:
            completion.close()

    assert len(results) == WORKERS
    assert all(isinstance(result, DurableWorkTicketEnqueued) for result in results)
    assert len(set(results)) == 1
    assert registry.calls == 1
    counts = _row_counts_for(path, scenario.request_id)
    assert all(count == 1 for count in counts.values())
    request = _final_request(path, scenario.request_id)
    assert isinstance(request.state, AwaitingAnswer)
    assert request.revision == 2
    _assert_schema_capable(path, scenario.org_id)


# ---------------------------------------------------------------------------
# 2. fault 5지점 × 실경쟁 — 고립 실패는 ReadyToDispatch 잔류·write 0, 이어서
#    같은 DB에서 실 경쟁은 부분 쓰기 0으로 1 winner에 수렴
# ---------------------------------------------------------------------------

_FAULT_POINTS = (
    "after_receipt",
    "after_audit_intent",
    "after_outbox_intent",
    "after_work_ticket",
    "after_request",
)


@pytest.mark.parametrize("point", _FAULT_POINTS)
def test_각_fault_point는_고립_실패에서_readytodispatch_잔류이고_실경쟁에서_1_winner로_수렴한다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / f"fault-{point}.sqlite"
    scenario = _scenario(f"fault-{point}")
    _seed_scenarios(path, [scenario])
    registry = _Registry(owner=scenario.owner)
    ticket_ids = _IdFactory("ticket")
    receipt_ids = _IdFactory("receipt")
    command = _command(scenario.request_id)

    # Phase A: 고립된 실 커넥션 fault — RuntimeError·write 0·ReadyToDispatch 잔류.
    def raise_at_point(injected: str) -> None:
        if injected == point:
            raise RuntimeError(injected)

    isolated_completion, isolated_uow = _open_uow(
        path,
        index=0,
        registry=registry,
        ticket_ids=ticket_ids,
        receipt_ids=receipt_ids,
        fault_injector=raise_at_point,
    )
    try:
        with pytest.raises(RuntimeError, match=point):
            isolated_uow.enqueue(command=command)
    finally:
        isolated_completion.close()
    assert all(count == 0 for count in _row_counts_for(path, scenario.request_id).values())
    isolated_request = _final_request(path, scenario.request_id)
    assert isinstance(isolated_request.state, ReadyToDispatch)
    assert isolated_request.revision == 1

    # Phase B: 실 경쟁 — fault 스레드가 write lock을 쥔 채 주입 지점에서 정지,
    # 정상 스레드들을 제출한 뒤 release·rollback시킨다. 남은 스레드는 같은
    # command라 정확히 1 fresh write + 나머지는 멱등 replay로 수렴한다.
    workers = 8
    fault_index = 0
    paused = threading.Event()
    release = threading.Event()

    def pause_then_raise(injected: str) -> None:
        if injected == point:
            paused.set()
            assert release.wait(timeout=5)
            raise RuntimeError(injected)

    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableWorkTicketEnqueueUnitOfWork] = []
    for index in range(workers):
        completion, uow = _open_uow(
            path,
            index=index,
            registry=registry,
            ticket_ids=ticket_ids,
            receipt_ids=receipt_ids,
            fault_injector=pause_then_raise if index == fault_index else None,
        )
        completions.append(completion)
        uows.append(uow)

    def run_fault() -> object:
        try:
            return uows[fault_index].enqueue(command=command)
        except RuntimeError as error:
            return error

    def run_normal(index: int) -> object:
        return uows[index].enqueue(command=command)

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
    assert len(normal_results) == workers - 1
    assert all(isinstance(result, DurableWorkTicketEnqueued) for result in normal_results)
    assert len(set(normal_results)) == 1
    final_counts = _row_counts_for(path, scenario.request_id)
    assert all(count == 1 for count in final_counts.values())
    final_request = _final_request(path, scenario.request_id)
    assert isinstance(final_request.state, AwaitingAnswer)
    assert final_request.revision == 2
    _assert_schema_capable(path, scenario.org_id)


# ---------------------------------------------------------------------------
# 3. 32 다른 request 병행 enqueue — 전부 성공·교차 오염 0
# ---------------------------------------------------------------------------


def test_32_way_다른_request_병행_enqueue는_전부_성공하고_교차_오염이_없다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "different-requests.sqlite"
    scenarios = [_scenario(f"parallel-{index}") for index in range(WORKERS)]
    _seed_scenarios(path, scenarios)
    registry = _Registry(owners={(s.org_id, s.agent_id): s.owner for s in scenarios})
    ticket_ids = _IdFactory("ticket")
    receipt_ids = _IdFactory("receipt")
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableWorkTicketEnqueueUnitOfWork] = []
    for index in range(WORKERS):
        completion, uow = _open_uow(
            path, index=index, registry=registry, ticket_ids=ticket_ids, receipt_ids=receipt_ids
        )
        completions.append(completion)
        uows.append(uow)
    barrier = threading.Barrier(WORKERS)

    def run(item: tuple[_Scenario, DurableWorkTicketEnqueueUnitOfWork]) -> object:
        scenario, uow = item
        barrier.wait()
        return uow.enqueue(command=_command(scenario.request_id))

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(run, zip(scenarios, uows, strict=True)))
    finally:
        for completion in completions:
            completion.close()

    assert len(results) == WORKERS
    ticket_ids_seen: set[str] = set()
    receipt_ids_seen: set[str] = set()
    for scenario, result in zip(scenarios, results, strict=True):
        assert isinstance(result, DurableWorkTicketEnqueued)
        assert result.request_id == scenario.request_id
        counts = _row_counts_for(path, scenario.request_id)
        assert all(count == 1 for count in counts.values())
        ticket_ids_seen.add(result.ticket_id)
        receipt_ids_seen.add(result.receipt_id)
    assert len(ticket_ids_seen) == WORKERS
    assert len(receipt_ids_seen) == WORKERS
    assert registry.calls == WORKERS
    _assert_schema_capable(path, None)


# ---------------------------------------------------------------------------
# 4. 동시 상태 경합 — enqueue vs 다른 전이(수동 CAS로 FailedRequest) 실 경쟁,
#    enqueue는 Conflict로 닫히고 어중간 상태 0
# ---------------------------------------------------------------------------


def test_enqueue와_다른_전이의_동시_상태_경합은_conflict로_닫히고_어중간_상태가_없다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state-race.sqlite"
    scenario = _scenario("state-race")
    _seed_scenarios(path, [scenario])
    registry = _Registry(owner=scenario.owner)
    ticket_ids = _IdFactory("ticket")
    receipt_ids = _IdFactory("receipt")
    workers = 8
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableWorkTicketEnqueueUnitOfWork] = []
    for index in range(workers):
        completion, uow = _open_uow(
            path, index=index, registry=registry, ticket_ids=ticket_ids, receipt_ids=receipt_ids
        )
        completions.append(completion)
        uows.append(uow)
    command = _command(scenario.request_id)

    mutator = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "mutator-record",
        clock=lambda: NOW,
        timeout=10.0,
    )

    def run_enqueue(uow: DurableWorkTicketEnqueueUnitOfWork) -> object:
        try:
            return uow.enqueue(command=command)
        except DurableWorkTicketConflict as error:
            return error

    try:
        mutator_tx = mutator.durable_transaction()
        with mutator_tx.scope():
            mutator_tx.begin_immediate()
            request = mutator_tx.select_question_request(scenario.request_id)
            assert request is not None and isinstance(request.state, ReadyToDispatch)
            failed = request.transition(
                FailedRequest(error_code="external_decline"), clock=lambda: NOW
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                # mutator가 이 write lock을 먼저 쥐고 있으므로(begin_immediate가
                # 이미 위에서 호출됨) 모든 enqueue 스레드는 자신의 begin_immediate()
                # 뒤로 줄을 선다 — 순서는 스케줄링과 무관하게 결정론적이다.
                futures = [pool.submit(run_enqueue, uow) for uow in uows]
                assert mutator_tx.compare_and_set_question_request(
                    scenario.request_id, request.revision, request, failed
                )
                mutator_tx.commit()
                results = [future.result(timeout=10) for future in futures]
    finally:
        mutator.close()
        for completion in completions:
            completion.close()

    assert len(results) == workers
    assert all(isinstance(result, DurableWorkTicketConflict) for result in results)
    counts = _row_counts_for(path, scenario.request_id)
    assert all(count == 0 for count in counts.values())
    final_request = _final_request(path, scenario.request_id)
    assert isinstance(final_request.state, FailedRequest)
    assert final_request.state.error_code == "external_decline"
    assert final_request.revision == 2
    _assert_schema_capable(path, scenario.org_id)


# ---------------------------------------------------------------------------
# S4.5a P2-2 이월 — _stored_result receipt 하위검사 개별 변조 → Unavailable
# (단일 커넥션)
# ---------------------------------------------------------------------------


def _seed_and_enqueue(
    tmp_path: Path, key: str
) -> tuple[
    Path, _Scenario, SqliteQuestionCompletionUnitOfWork, DurableWorkTicketEnqueueUnitOfWork, str
]:
    path = tmp_path / f"{key}.sqlite"
    scenario = _scenario(key)
    _seed_scenarios(path, [scenario])
    registry = _Registry(owner=scenario.owner)
    ticket_ids = _IdFactory("ticket")
    receipt_ids = _IdFactory("receipt")
    completion, uow = _open_uow(
        path, index=0, registry=registry, ticket_ids=ticket_ids, receipt_ids=receipt_ids
    )
    uow.enqueue(command=_command(scenario.request_id))
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        receipt = tx.execute(
            "SELECT command_digest FROM durable_linked_command_receipts WHERE request_id=?",
            (scenario.request_id,),
        ).fetchone()
        digest = receipt["command_digest"]
        tx.commit()
    return path, scenario, completion, uow, digest


def test_stored_result_receipt_target_kind_변조는_unavailable이다(tmp_path: Path) -> None:
    _, scenario, completion, uow, digest = _seed_and_enqueue(tmp_path, "p2-2-target-kind")
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            # target_kind와 target_ref를 함께 바꿔 S4.1 스키마 자체 검증(형식)은
            # 통과시키고 _stored_result의 의미 검사만 겨냥한다.
            tx.execute(
                "UPDATE durable_linked_command_receipts SET target_kind='manager_item', "
                "target_ref=? WHERE command_digest=?",
                (_ref("manager", "corrupted-target"), digest),
            )
            tx.commit()
        with pytest.raises(DurableWorkTicketUnavailable):
            uow.enqueue(command=_command(scenario.request_id))
    finally:
        completion.close()


def test_stored_result_receipt_expected_request_revision_변조는_unavailable이다(
    tmp_path: Path,
) -> None:
    _, scenario, completion, uow, digest = _seed_and_enqueue(
        tmp_path, "p2-2-expected-revision"
    )
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_command_receipts SET expected_request_revision=99 "
                "WHERE command_digest=?",
                (digest,),
            )
            tx.commit()
        with pytest.raises(DurableWorkTicketUnavailable):
            uow.enqueue(command=_command(scenario.request_id))
    finally:
        completion.close()


def test_stored_result_receipt_request_id_변조는_unavailable이다(tmp_path: Path) -> None:
    path, scenario, completion, uow, digest = _seed_and_enqueue(
        tmp_path, "p2-2-request-id"
    )
    other_request_id = _ref("request", "p2-2-request-id-other")
    try:
        other_completion = SqliteQuestionCompletionUnitOfWork(
            path,
            policy=object(),
            approvals=object(),
            responsibility_resolver=object(),
            record_id_factory=lambda: "other-record",
            clock=lambda: NOW,
        )
        try:
            # 같은 org에 두 번째 유효 request를 심어 FK/lineage를 지켜낸 채
            # receipt.request_id만 다른 request로 돌려 unavailable을 겨냥한다
            # (S4.5a `test_stored_result_ticket_id_교차_불일치는_unavailable이다`와 동형).
            received = QuestionRequest.receive(
                org_id=scenario.org_id,
                requester_id="user",
                question="other request",
                request_id_factory=lambda: other_request_id,
                clock=lambda: NOW - timedelta(minutes=2),
                due_at=NOW + timedelta(hours=1),
            )
            other_completion.create(received)
        finally:
            other_completion.close()

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            # audit·outbox mirror(row["request_id"] == audit/outbox 동일)도 함께
            # 맞춰야 S4.1 검증이 receipt corruption보다 먼저 걸리지 않는다.
            for table in (
                "durable_linked_command_receipts",
                "durable_linked_audit_intents",
                "durable_linked_outbox_intents",
            ):
                tx.execute(
                    f"UPDATE {table} SET request_id=? WHERE command_digest=?",
                    (other_request_id, digest),
                )
            tx.commit()
        with pytest.raises(DurableWorkTicketUnavailable):
            uow.enqueue(command=_command(scenario.request_id))
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# S4.5a P2-3 이월(필수) — commit-time compare_and_set_question_request False
# 분기 red
# ---------------------------------------------------------------------------


class _ForcedCasFalseTransaction:
    """CAS 호출 자체만 결정론적으로 가로채 False를 강제하는 delegate(모듈
    docstring 참조 — SQLite ``BEGIN IMMEDIATE`` 하에서 real race로는 이 분기를
    만들 수 없다는 증명). 그 외 read/write/commit/rollback은 전부 실
    ``SqliteCompletionTransaction``에 위임한다."""

    def __init__(self, real: SqliteCompletionTransaction) -> None:
        self._real = real
        self.cas_calls = 0

    def scope(self) -> AbstractContextManager[None]:
        return self._real.scope()

    def validate_component(self, validator: Callable[[sqlite3.Connection], None]) -> None:
        self._real.validate_component(validator)

    def validate_component_in_transaction(
        self, validator: Callable[[sqlite3.Connection], None]
    ) -> None:
        self._real.validate_component_in_transaction(validator)

    def begin_immediate(self) -> None:
        self._real.begin_immediate()

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        return self._real.execute(sql, parameters)

    def select_question_request(self, request_id: str) -> QuestionRequest | None:
        return self._real.select_question_request(request_id)

    def compare_and_set_question_request(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        self.cas_calls += 1
        return False

    def commit(self) -> None:
        self._real.commit()

    def rollback(self) -> None:
        self._real.rollback()

    @property
    def in_transaction(self) -> bool:
        return self._real.in_transaction


class _ForcedCasFalseCompletion:
    """``DurableWorkTicketEnqueueUnitOfWork``는 생성 시 ``completion.durable_transaction()``
    한 번만 호출해 저장하므로, 이 duck-typed delegate는 그 메서드만 노출하면 된다."""

    def __init__(self, forced: _ForcedCasFalseTransaction) -> None:
        self._forced = forced

    def durable_transaction(self) -> _ForcedCasFalseTransaction:
        return self._forced


def test_commit_time_cas_false는_conflict로_닫히고_write_0이며_이후_실경쟁은_오염되지_않는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cas-false.sqlite"
    scenario = _scenario("cas-false")
    _seed_scenarios(path, [scenario])
    registry = _Registry(owner=scenario.owner)
    ticket_ids = _IdFactory("ticket")
    receipt_ids = _IdFactory("receipt")
    command = _command(scenario.request_id)

    real_completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record-forced",
        clock=lambda: NOW,
        timeout=10.0,
    )
    forced_tx = _ForcedCasFalseTransaction(real_completion.durable_transaction())
    forced_uow = DurableWorkTicketEnqueueUnitOfWork(
        completion=_ForcedCasFalseCompletion(forced_tx),
        registry=registry,
        clock=lambda: NOW,
        ticket_id_factory=ticket_ids,
        receipt_id_factory=receipt_ids,
    )
    try:
        with pytest.raises(DurableWorkTicketConflict):
            forced_uow.enqueue(command=command)
    finally:
        real_completion.close()
    assert forced_tx.cas_calls == 1
    assert all(count == 0 for count in _row_counts_for(path, scenario.request_id).values())
    request = _final_request(path, scenario.request_id)
    assert isinstance(request.state, ReadyToDispatch)
    assert request.revision == 1

    # 이어서 real 다중 커넥션 경쟁이 정상 수렴함을 확인 — forced-false 롤백이
    # DB를 오염시키지 않았다는 증거.
    workers = 8
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableWorkTicketEnqueueUnitOfWork] = []
    for index in range(workers):
        completion, uow = _open_uow(
            path, index=index, registry=registry, ticket_ids=ticket_ids, receipt_ids=receipt_ids
        )
        completions.append(completion)
        uows.append(uow)
    barrier = threading.Barrier(workers)

    def run(uow: DurableWorkTicketEnqueueUnitOfWork) -> object:
        barrier.wait()
        return uow.enqueue(command=command)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            race_results = list(pool.map(run, uows))
    finally:
        for completion in completions:
            completion.close()

    assert len(race_results) == workers
    assert all(isinstance(result, DurableWorkTicketEnqueued) for result in race_results)
    assert len(set(race_results)) == 1
    final_counts = _row_counts_for(path, scenario.request_id)
    assert all(count == 1 for count in final_counts.values())
    final_request = _final_request(path, scenario.request_id)
    assert isinstance(final_request.state, AwaitingAnswer)
    assert final_request.revision == 2
    _assert_schema_capable(path, scenario.org_id)
