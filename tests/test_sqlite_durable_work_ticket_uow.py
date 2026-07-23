from __future__ import annotations
# pyright: reportArgumentType=false

import hashlib
import inspect
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pytest

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingConflict,
    AwaitingManager,
    FailedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
    validate_sqlite_durable_linked_aggregates_connection,
)
from agent_org_network.sqlite_durable_work_ticket_uow import (
    SYSTEM_SUBJECT_REF,
    DurableWorkTicketConflict,
    DurableWorkTicketEnqueueCommand,
    DurableWorkTicketEnqueued,
    DurableWorkTicketEnqueueUnitOfWork,
    DurableWorkTicketRegistry,
    DurableWorkTicketUnavailable,
)

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


ORG_ID = _ref("org", "org-1")
REQUEST_ID = _ref("request", "request-1")
_DEFAULT_OWNER = _ref("subject", "owner-a")


class _Registry:
    """owner 주소 해석 Fake — 고정 owner·None·예외 세 모드를 지원한다."""

    def __init__(self, *, owner: str | None = _DEFAULT_OWNER, raises: bool = False) -> None:
        self.calls = 0
        self._owner = owner
        self._raises = raises

    def resolve_owner_subject(self, *, org_id: str, agent_id: str) -> str | None:
        self.calls += 1
        if self._raises:
            raise RuntimeError("registry unavailable")
        return self._owner


def _open_completion(path: Path) -> SqliteQuestionCompletionUnitOfWork:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    return SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: NOW,
    )


def _seed_ready(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    request_id: str = REQUEST_ID,
    org_id: str = ORG_ID,
    agent_id: str = "card-a",
    intent: str = "refund",
    attempt: int = 1,
    due_at: datetime | None = None,
) -> None:
    """record_initial_routing(routed)으로 Received rev0→ReadyToDispatch rev1 직행."""
    received = QuestionRequest.receive(
        org_id=org_id,
        requester_id="user",
        question="refund question",
        request_id_factory=lambda: request_id,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(received)
    trigger_ref = _ref("trigger", request_id)
    ready = received.record_initial_routing(
        intent=intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=RouteTarget(intent=intent, agent_id=agent_id, requires_approval=False),
            attempt=attempt,
            trigger_key=trigger_ref,
            handling=HandlingAssignment(
                kind="system", ref=trigger_ref, due_at=due_at or NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set(request_id, 0, received, ready)


def _prepared(
    tmp_path: Path,
    *,
    agent_id: str = "card-a",
    attempt: int = 1,
    due_at: datetime | None = None,
) -> SqliteQuestionCompletionUnitOfWork:
    completion = _open_completion(tmp_path / "workflow.sqlite")
    _seed_ready(completion, agent_id=agent_id, attempt=attempt, due_at=due_at)
    return completion


def _uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    registry: DurableWorkTicketRegistry | None = None,
    fault_injector: Callable[[str], None] | None = None,
    ticket_ids: Sequence[str] = ("ticket-1",),
    receipt_ids: Sequence[str] = ("receipt-1",),
) -> DurableWorkTicketEnqueueUnitOfWork:
    ticket_iter = iter(ticket_ids)
    receipt_iter = iter(receipt_ids)
    return DurableWorkTicketEnqueueUnitOfWork(
        completion=completion,
        registry=registry or _Registry(),
        clock=lambda: NOW,
        ticket_id_factory=lambda: next(ticket_iter),
        receipt_id_factory=lambda: next(receipt_iter),
        fault_injector=fault_injector,
    )


def _command(
    *, request_id: str = REQUEST_ID, expected_request_revision: int = 1, attempt: int = 1
) -> DurableWorkTicketEnqueueCommand:
    return DurableWorkTicketEnqueueCommand(request_id, expected_request_revision, attempt)


def _row_counts(tx: SqliteCompletionTransaction) -> dict[str, int]:
    return {
        table: tx.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "durable_linked_command_receipts",
            "durable_linked_audit_intents",
            "durable_linked_outbox_intents",
            "durable_linked_work_tickets",
        )
    }


def _assert_no_new_writes(completion: SqliteQuestionCompletionUnitOfWork) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        assert all(count == 0 for count in _row_counts(tx).values())
        tx.commit()


def _assert_ready_to_dispatch_remains(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    request_id: str = REQUEST_ID,
    revision: int = 1,
) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        assert all(count == 0 for count in _row_counts(tx).values())
        request = tx.select_question_request(request_id)
        assert request is not None
        assert isinstance(request.state, ReadyToDispatch)
        assert request.revision == revision
        tx.commit()


# ---------------------------------------------------------------------------
# red 1 — command 형식 위반 → Unavailable, Registry/state 미접촉
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_command",
    [
        lambda: object(),
        lambda: DurableWorkTicketEnqueueCommand("", 1, 1),
        lambda: DurableWorkTicketEnqueueCommand(REQUEST_ID, 0, 1),
        lambda: DurableWorkTicketEnqueueCommand(REQUEST_ID, 1, 0),
    ],
    ids=["wrong-type", "blank-request-id", "revision-lt-1", "attempt-lt-1"],
)
def test_command_형식_위반은_unavailable이고_registry_state_미접촉이다(
    tmp_path: Path, make_command: Callable[[], object]
) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _Registry()
        uow = _uow(completion, registry=registry)
        with pytest.raises(DurableWorkTicketUnavailable):
            uow.enqueue(command=make_command())
        assert registry.calls == 0
        _assert_ready_to_dispatch_remains(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 2 — 생성 validate 실패 → Unavailable
# ---------------------------------------------------------------------------


def test_migration_안된_db는_생성_시점에_unavailable이다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(DurableWorkTicketUnavailable):
            DurableWorkTicketEnqueueUnitOfWork(
                completion=completion,
                registry=_Registry(),
                clock=lambda: NOW,
                ticket_id_factory=lambda: "ticket-1",
                receipt_id_factory=lambda: "receipt-1",
            )
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 3 — fresh happy path(+15 target_ref 재hash 금지)
# ---------------------------------------------------------------------------


def test_fresh_enqueue는_ticket_receipt_audit_outbox를_원자_커밋하고_awaitinganswer로_전이한다(
    tmp_path: Path,
) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _Registry()
        uow = _uow(completion, registry=registry)
        result = uow.enqueue(command=_command())
        assert isinstance(result, DurableWorkTicketEnqueued)
        assert result.request_id == REQUEST_ID
        assert result.request_revision == 2
        assert result.attempt == 1

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _row_counts(tx).values())
            receipt = tx.execute(
                "SELECT * FROM durable_linked_command_receipts WHERE request_id=?", (REQUEST_ID,)
            ).fetchone()
            assert receipt["receipt_id"] == result.receipt_id
            assert receipt["action"] == "work_ticket.create"
            assert receipt["target_kind"] == "work_ticket"
            assert receipt["target_ref"] == result.ticket_id  # red 15: 재hash 금지
            assert receipt["principal_id"] == SYSTEM_SUBJECT_REF
            assert receipt["expected_request_revision"] == 1
            ticket = tx.execute(
                "SELECT * FROM durable_linked_work_tickets WHERE ticket_id=?", (result.ticket_id,)
            ).fetchone()
            assert ticket["org_id"] == ORG_ID
            assert ticket["request_id"] == REQUEST_ID
            assert ticket["attempt"] == 1
            assert ticket["awaiting_revision"] == 1
            assert ticket["owner_subject_id"] == _DEFAULT_OWNER
            assert ticket["status"] == "pending"
            audit = tx.execute(
                "SELECT * FROM durable_linked_audit_intents WHERE receipt_id=?",
                (receipt["receipt_id"],),
            ).fetchone()
            outbox = tx.execute(
                "SELECT * FROM durable_linked_outbox_intents WHERE receipt_id=?",
                (receipt["receipt_id"],),
            ).fetchone()
            assert audit["command_digest"] == receipt["command_digest"] == outbox["command_digest"]
            assert audit["action"] == receipt["action"] == "work_ticket.create"
            assert outbox["kind"] == "linked_aggregate_outbox"
            tx.validate_component_in_transaction(
                lambda conn: validate_sqlite_durable_linked_aggregates_connection(
                    conn, org_id=ORG_ID
                )
            )
            tx.commit()

        request = completion.get(REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
        assert request.state.route.agent_id == "card-a"
        assert request.state.route.intent == "refund"
        assert request.state.attempt == 1
        assert request.state.ticket_id == result.ticket_id
        assert request.state.handling.kind == "runtime_ticket"
        assert request.state.handling.ref == result.ticket_id
        assert request.revision == 2
        assert registry.calls == 1
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 4 — non-ReadyToDispatch 각 상태 → Conflict·write 0
# ---------------------------------------------------------------------------


def test_awaitingmanager_상태는_conflict이고_write_0이다(tmp_path: Path) -> None:
    completion = _open_completion(tmp_path / "workflow.sqlite")
    try:
        received = QuestionRequest.receive(
            org_id=ORG_ID,
            requester_id="user",
            question="q",
            request_id_factory=lambda: REQUEST_ID,
            clock=lambda: NOW - timedelta(minutes=2),
            due_at=NOW + timedelta(hours=1),
        )
        completion.create(received)
        item_ref = _ref("manager", "item-1")
        unowned = received.record_initial_routing(
            intent="refund",
            disposition="unowned",
            target=AwaitingManager(
                item_id=item_ref,
                public_kind="unowned",
                handling=HandlingAssignment(
                    kind="manager_item", ref=item_ref, due_at=NOW + timedelta(hours=1)
                ),
            ),
            clock=lambda: NOW - timedelta(minutes=1),
        )
        assert completion.compare_and_set(REQUEST_ID, 0, received, unowned)
        uow = _uow(completion)
        with pytest.raises(DurableWorkTicketConflict):
            uow.enqueue(command=_command(expected_request_revision=1))
        _assert_no_new_writes(completion)
    finally:
        completion.close()


def test_awaitingconflict_상태는_conflict이고_write_0이다(tmp_path: Path) -> None:
    completion = _open_completion(tmp_path / "workflow.sqlite")
    try:
        received = QuestionRequest.receive(
            org_id=ORG_ID,
            requester_id="user",
            question="q",
            request_id_factory=lambda: REQUEST_ID,
            clock=lambda: NOW - timedelta(minutes=2),
            due_at=NOW + timedelta(hours=1),
        )
        completion.create(received)
        case_ref = _ref("conflict", "case-1")
        contested = received.record_initial_routing(
            intent="refund",
            disposition="contested",
            target=AwaitingConflict(
                case_id=case_ref,
                handling=HandlingAssignment(
                    kind="conflict_case", ref=case_ref, due_at=NOW + timedelta(hours=1)
                ),
            ),
            clock=lambda: NOW - timedelta(minutes=1),
        )
        assert completion.compare_and_set(REQUEST_ID, 0, received, contested)
        uow = _uow(completion)
        with pytest.raises(DurableWorkTicketConflict):
            uow.enqueue(command=_command(expected_request_revision=1))
        _assert_no_new_writes(completion)
    finally:
        completion.close()


def test_이미_awaitinganswer인_상태는_conflict이고_write_0이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        ready_request = completion.get(REQUEST_ID)
        assert ready_request is not None and isinstance(ready_request.state, ReadyToDispatch)
        manual_ticket_ref = _ref("ticket", "manual-ticket")
        awaiting = ready_request.transition(
            AwaitingAnswer(
                route=ready_request.state.route,
                attempt=ready_request.state.attempt,
                ticket_id=manual_ticket_ref,
                handling=HandlingAssignment(
                    kind="runtime_ticket",
                    ref=manual_ticket_ref,
                    due_at=ready_request.state.handling.due_at,
                ),
            ),
            clock=lambda: NOW,
        )
        assert completion.compare_and_set(REQUEST_ID, 1, ready_request, awaiting)
        uow = _uow(completion)
        with pytest.raises(DurableWorkTicketConflict):
            uow.enqueue(command=_command(expected_request_revision=2))
        _assert_no_new_writes(completion)
    finally:
        completion.close()


def test_terminal_failedrequest_상태는_conflict이고_write_0이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        ready_request = completion.get(REQUEST_ID)
        assert ready_request is not None and isinstance(ready_request.state, ReadyToDispatch)
        failed = ready_request.transition(FailedRequest(error_code="boom"), clock=lambda: NOW)
        assert completion.compare_and_set(REQUEST_ID, 1, ready_request, failed)
        uow = _uow(completion)
        with pytest.raises(DurableWorkTicketConflict):
            uow.enqueue(command=_command(expected_request_revision=2))
        _assert_no_new_writes(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 5 — stale revision → Conflict, ReadyToDispatch 잔류
# ---------------------------------------------------------------------------


# revision 0은 expected_request_revision>=1 command 계약 위반(red 1의 Unavailable
# 영역)이라 여기서는 다루지 않는다 — actual=1 대비 "너무 큰" stale만 Conflict다.
@pytest.mark.parametrize("revision", [2, 3])
def test_stale_revision은_conflict이고_readytodispatch_잔류이다(
    tmp_path: Path, revision: int
) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _Registry()
        uow = _uow(completion, registry=registry)
        with pytest.raises(DurableWorkTicketConflict):
            uow.enqueue(command=_command(expected_request_revision=revision))
        assert registry.calls == 0
        _assert_ready_to_dispatch_remains(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 6 — attempt 불일치 → Conflict, ReadyToDispatch 잔류
# ---------------------------------------------------------------------------


def test_attempt_불일치는_conflict이고_readytodispatch_잔류이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path, attempt=1)
    try:
        registry = _Registry()
        uow = _uow(completion, registry=registry)
        with pytest.raises(DurableWorkTicketConflict):
            uow.enqueue(command=_command(expected_request_revision=1, attempt=2))
        assert registry.calls == 0
        _assert_ready_to_dispatch_remains(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 7 — request 미존재 → Conflict
# ---------------------------------------------------------------------------


def test_존재하지_않는_request는_conflict이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _Registry()
        uow = _uow(completion, registry=registry)
        missing_id = _ref("request", "missing")
        with pytest.raises(DurableWorkTicketConflict):
            uow.enqueue(command=_command(request_id=missing_id))
        assert registry.calls == 0
        _assert_no_new_writes(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 8 — resolve_owner None → Conflict·write 0·ReadyToDispatch 잔류(미아 없음)
# ---------------------------------------------------------------------------


def test_resolve_owner_none은_conflict이고_readytodispatch_잔류이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _Registry(owner=None)
        uow = _uow(completion, registry=registry)
        with pytest.raises(DurableWorkTicketConflict):
            uow.enqueue(command=_command())
        assert registry.calls == 1
        _assert_ready_to_dispatch_remains(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 9 — Registry 예외 → Unavailable, ReadyToDispatch 잔류
# ---------------------------------------------------------------------------


def test_registry_예외는_unavailable이고_readytodispatch_잔류이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _Registry(raises=True)
        uow = _uow(completion, registry=registry)
        with pytest.raises(DurableWorkTicketUnavailable):
            uow.enqueue(command=_command())
        assert registry.calls == 1
        _assert_ready_to_dispatch_remains(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 10·11 — replay는 Registry 미호출·같은 command 2회는 1 fresh(멱등)
# ---------------------------------------------------------------------------


def test_replay는_registry를_호출하지_않고_동일_결과를_돌려준다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _Registry()
        uow = _uow(completion, registry=registry)
        command = _command()
        first = uow.enqueue(command=command)
        assert registry.calls == 1
        second = uow.enqueue(command=command)
        assert second == first
        assert registry.calls == 1

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _row_counts(tx).values())
            tx.commit()
        request = completion.get(REQUEST_ID)
        assert request is not None
        assert request.revision == 2
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 12 — _stored_result 손상 방어
# ---------------------------------------------------------------------------


class _Corruption(Protocol):
    def __call__(
        self, tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str
    ) -> None: ...


def _corrupt_action(tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str) -> None:
    # S4.1 스키마의 intent mirror 불변(action == audit.action)까지 함께 맞춰야
    # _stored_result 자체의 action 검사(작업 수행 이후 두 번째 방어선)를 겨냥할 수 있다.
    tx.execute(
        "UPDATE durable_linked_command_receipts SET action='manager.assign_owner' "
        "WHERE command_digest=?",
        (digest,),
    )
    tx.execute(
        "UPDATE durable_linked_audit_intents SET action='manager.assign_owner' "
        "WHERE command_digest=?",
        (digest,),
    )


def _corrupt_missing_ticket(
    tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str
) -> None:
    tx.execute(
        "UPDATE durable_linked_command_receipts SET target_ref=? WHERE command_digest=?",
        (_ref("ticket", "missing-ticket"), digest),
    )


def _corrupt_ticket_status(
    tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str
) -> None:
    tx.execute(
        "UPDATE durable_linked_work_tickets SET status='completed' WHERE ticket_id=?",
        (ticket_id,),
    )


def _corrupt_route_sha256(
    tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str
) -> None:
    tx.execute(
        "UPDATE durable_linked_work_tickets SET route_sha256=? WHERE ticket_id=?",
        ("9" * 64, ticket_id),
    )


_CORRUPTIONS = (
    ("action", _corrupt_action),
    ("missing_ticket", _corrupt_missing_ticket),
    ("ticket_status", _corrupt_ticket_status),
    ("route_sha256", _corrupt_route_sha256),
)


@pytest.mark.parametrize(
    "mutate", [mutate for _, mutate in _CORRUPTIONS], ids=[name for name, _ in _CORRUPTIONS]
)
def test_stored_result_손상_방어(tmp_path: Path, mutate: _Corruption) -> None:
    completion = _prepared(tmp_path)
    try:
        uow = _uow(completion)
        first = uow.enqueue(command=_command())
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            receipt = tx.execute(
                "SELECT command_digest FROM durable_linked_command_receipts WHERE request_id=?",
                (REQUEST_ID,),
            ).fetchone()
            mutate(tx, digest=receipt["command_digest"], ticket_id=first.ticket_id)
            tx.commit()
        with pytest.raises(DurableWorkTicketUnavailable):
            uow.enqueue(command=_command())
    finally:
        completion.close()


def test_stored_result_ticket_id_교차_불일치는_unavailable이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        other_request_id = _ref("request", "request-2")
        _seed_ready(completion, request_id=other_request_id)
        uow = _uow(
            completion,
            ticket_ids=("ticket-a", "ticket-b"),
            receipt_ids=("receipt-a", "receipt-b"),
        )
        uow.enqueue(command=_command(request_id=REQUEST_ID))
        second = uow.enqueue(command=_command(request_id=other_request_id))

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_command_receipts SET target_ref=? WHERE request_id=?",
                (second.ticket_id, REQUEST_ID),
            )
            tx.commit()
        with pytest.raises(DurableWorkTicketUnavailable):
            uow.enqueue(command=_command(request_id=REQUEST_ID))
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 13 — UNIQUE(request_id, attempt) backstop
# ---------------------------------------------------------------------------


def test_unique_request_attempt_backstop은_ticket_중복_삽입을_막는다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "INSERT INTO durable_linked_work_tickets VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    _ref("ticket", "pre-existing"),
                    ORG_ID,
                    REQUEST_ID,
                    1,
                    1,
                    "0" * 64,
                    _DEFAULT_OWNER,
                    "pending",
                    NOW.isoformat(),
                ),
            )
            tx.commit()

        uow = _uow(completion)
        with pytest.raises(sqlite3.IntegrityError):
            uow.enqueue(command=_command())

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            counts = _row_counts(tx)
            assert counts["durable_linked_command_receipts"] == 0
            assert counts["durable_linked_audit_intents"] == 0
            assert counts["durable_linked_outbox_intents"] == 0
            assert counts["durable_linked_work_tickets"] == 1
            request = tx.select_question_request(REQUEST_ID)
            assert request is not None
            assert isinstance(request.state, ReadyToDispatch)
            assert request.revision == 1
            tx.commit()
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 14 — fault 5지점: 부분 쓰기 0·ReadyToDispatch 잔류·validate green
# ---------------------------------------------------------------------------

_FAULT_POINTS = (
    "after_receipt",
    "after_audit_intent",
    "after_outbox_intent",
    "after_work_ticket",
    "after_request",
)


@pytest.mark.parametrize("point", _FAULT_POINTS)
def test_각_fault_point는_전체_enqueue_artifact를_롤백한다(tmp_path: Path, point: str) -> None:
    completion = _prepared(tmp_path)
    try:

        def raise_at_point(injected: str) -> None:
            if injected == point:
                raise RuntimeError(injected)

        uow = _uow(completion, fault_injector=raise_at_point)
        with pytest.raises(RuntimeError, match=point):
            uow.enqueue(command=_command())
        _assert_ready_to_dispatch_remains(completion)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.validate_component_in_transaction(
                lambda connection: validate_sqlite_durable_linked_aggregates_connection(
                    connection, org_id=ORG_ID
                )
            )
            tx.commit()
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 16 — 구조적으로 중앙 재인가 인자가 없다(system 전이)
# ---------------------------------------------------------------------------


def test_구조적으로_중앙_재인가_인자가_없다() -> None:
    init_params = inspect.signature(DurableWorkTicketEnqueueUnitOfWork.__init__).parameters
    assert "central_authorizer" not in init_params
    assert "principal" not in init_params
    enqueue_params = inspect.signature(DurableWorkTicketEnqueueUnitOfWork.enqueue).parameters
    assert "principal" not in enqueue_params


# ---------------------------------------------------------------------------
# red 17 — SLA 경과(due_at<now) → Conflict·잔류
# ---------------------------------------------------------------------------


def test_sla_경과는_conflict이고_readytodispatch_잔류이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path, due_at=NOW - timedelta(seconds=30))
    try:
        registry = _Registry()
        uow = _uow(completion, registry=registry)
        with pytest.raises(DurableWorkTicketConflict):
            uow.enqueue(command=_command())
        _assert_ready_to_dispatch_remains(completion)
    finally:
        completion.close()
