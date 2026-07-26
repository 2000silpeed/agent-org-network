"""P17.9 S5.5 dispatch timeout scan 테스트(ADR 0042 §9 ⑨).

read-only 스캐너 하나만 검증한다 — write·repair·lease·전이는 이 컴포넌트
어디에도 없다. 판별자는 SLA(``handling.due_at``)뿐이고 lease 시계(만료)는
정보 전용(``lease_active``)이다. ``capable``은 S4.6
(``sqlite_durable_linked_reconciliation``)의 "violation 0"과 달리 "스캔을
완주했다"는 뜻이다 — 후보가 있어도(심지어 여럿이어도) capable은 True다.
"""

from __future__ import annotations
# pyright: reportPrivateUsage=false
# pyright: reportArgumentType=false

import hashlib
import inspect
import sqlite3
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.durable_dispatch_timeout import (
    DispatchTimeoutCandidate,
    DispatchTimeoutScanReport,
    scan_sqlite_dispatch_timeouts,
)
from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingApproval,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_dispatch_delivery import (
    migrate_sqlite_durable_dispatch_delivery_schema,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)
from agent_org_network.sqlite_durable_work_ticket_uow import (
    DurableWorkTicketEnqueueCommand,
    DurableWorkTicketEnqueueUnitOfWork,
)
from agent_org_network.sqlite_stores import (
    _question_request_values,  # pyright: ignore[reportPrivateUsage]
)

SCAN_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


def _canonical_instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


ORG_ID = _ref("org", "org-1")
_TICKET_OWNER = _ref("subject", "owner-a")


class _Registry:
    """WorkTicket enqueue(S4.5)용 owner 주소 해석 Fake — seed 전용."""

    def __init__(self, *, owner: str | None = _TICKET_OWNER) -> None:
        self._owner = owner

    def resolve_owner_subject(self, *, org_id: str, agent_id: str) -> str | None:
        return self._owner


def _open_all(path: Path) -> SqliteQuestionCompletionUnitOfWork:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    return SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: SCAN_NOW,
        timeout=5.0,
    )


def _seed_awaiting_ticket(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    request_id: str,
    due_at: datetime,
    org_id: str = ORG_ID,
    agent_id: str = "card-a",
    attempt: int = 1,
    ticket_label: str = "ticket-1",
    owner: str | None = _TICKET_OWNER,
) -> tuple[str, int]:
    """Received → ReadyToDispatch → S4.5 enqueue로 SLA due_at을 통제 가능한
    AwaitingAnswer pending WorkTicket을 만든다. (ticket_id, request_revision)을 돌려준다.

    내부 clock들은 (호출자 SCAN_NOW가 아니라) ``due_at``을 기준으로 과거로
    역산한다 — due_at이 SCAN_NOW보다 과거(SLA 초과 후보)든 미래(비후보)든
    상관없이 ``updated_at``·``due_at`` 단조성(question_request.py의
    ``_transition_time``·``record_initial_routing``·``transition`` 가드)이
    항상 성립하게 하기 위함이다.
    """
    receive_at = due_at - timedelta(days=3)
    route_at = due_at - timedelta(days=2)
    enqueue_at = due_at - timedelta(days=1)
    received = QuestionRequest.receive(
        org_id=org_id,
        requester_id="user",
        question="refund question",
        request_id_factory=lambda: request_id,
        clock=lambda: receive_at,
        due_at=due_at,
    )
    completion.create(received)
    trigger_ref = _ref("trigger", f"{request_id}-{attempt}")
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=RouteTarget(intent="refund", agent_id=agent_id, requires_approval=False),
            attempt=attempt,
            trigger_key=trigger_ref,
            handling=HandlingAssignment(kind="system", ref=trigger_ref, due_at=due_at),
        ),
        clock=lambda: route_at,
    )
    assert completion.compare_and_set(request_id, 0, received, ready)

    enqueue_uow = DurableWorkTicketEnqueueUnitOfWork(
        completion=completion,
        registry=_Registry(owner=owner),
        clock=lambda: enqueue_at,
        ticket_id_factory=lambda: ticket_label,
        receipt_id_factory=lambda: f"receipt-{ticket_label}",
    )
    enqueued = enqueue_uow.enqueue(
        command=DurableWorkTicketEnqueueCommand(request_id, 1, attempt)
    )
    return enqueued.ticket_id, enqueued.request_revision


def _corrupt_ticket_status(
    completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str, value: str
) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "UPDATE durable_linked_work_tickets SET status=? WHERE ticket_id=?", (value, ticket_id)
        )
        tx.commit()


def _corrupt_ticket_awaiting_revision(
    completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str, value: int
) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "UPDATE durable_linked_work_tickets SET awaiting_revision=? WHERE ticket_id=?",
            (value, ticket_id),
        )
        tx.commit()


def _drift_awaiting_answer_ticket_id(
    completion: SqliteQuestionCompletionUnitOfWork, *, request_id: str, other_ticket_id: str
) -> None:
    """다른 하위 시스템의 손상을 흉내낸다 — revision은 그대로 두고 Request의
    AwaitingAnswer.ticket_id만 존재하지 않는 다른 ticket을 가리키게 만든다
    (도메인 API로는 same-state 전이가 금지돼 raw row 덮어쓰기로만 표현 가능하다).
    """
    current = completion.get(request_id)
    assert current is not None
    assert isinstance(current.state, AwaitingAnswer)
    drifted_state = AwaitingAnswer(
        route=current.state.route,
        attempt=current.state.attempt,
        ticket_id=other_ticket_id,
        handling=HandlingAssignment(
            kind="runtime_ticket", ref=other_ticket_id, due_at=current.state.handling.due_at
        ),
    )
    drifted = current.model_copy(update={"state": drifted_state})
    values = _question_request_values(drifted)
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "UPDATE question_requests SET state_kind=?, state_json=? WHERE request_id=?",
            (values[8], values[9], request_id),
        )
        tx.commit()


def _advance_to_awaiting_approval(
    completion: SqliteQuestionCompletionUnitOfWork, *, request_id: str, after: datetime
) -> None:
    current = completion.get(request_id)
    assert current is not None
    assert isinstance(current.state, AwaitingAnswer)
    draft_ref = _ref("draft", f"{request_id}-approval")
    target = AwaitingApproval(
        route=current.state.route,
        attempt=current.state.attempt,
        draft_ref=draft_ref,
        handling=HandlingAssignment(
            kind="approval_item", ref=draft_ref, due_at=after + timedelta(hours=2)
        ),
    )
    approved = current.transition(target, clock=lambda: after)
    assert completion.compare_and_set(request_id, current.revision, current, approved)


def _insert_lease(
    path: Path,
    *,
    ticket_id: str,
    org_id: str,
    request_id: str,
    expires_at: datetime,
    acquired_at: datetime,
    epoch: int = 1,
    holder: str | None = None,
    state: str = "leased",
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO durable_dispatch_leases VALUES(?,?,?,?,?,?,?,?)",
            (
                ticket_id,
                org_id,
                request_id,
                epoch,
                holder or _ref("subject", "holder-a"),
                state,
                _canonical_instant(expires_at),
                _canonical_instant(acquired_at),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_attempt(
    path: Path,
    *,
    attempt_id: str,
    ticket_id: str,
    org_id: str,
    request_id: str,
    created_at: datetime,
    epoch: int = 1,
    holder: str | None = None,
    outcome: str = "delivered",
    reason_code: str = "ok",
    target: str | None = None,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO durable_dispatch_delivery_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                org_id,
                ticket_id,
                request_id,
                epoch,
                holder or _ref("subject", "holder-a"),
                outcome,
                reason_code,
                target or _ref("subject", "target-a"),
                _canonical_instant(created_at),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _dump_all_tables(path: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        return {
            table: connection.execute(f'SELECT rowid, * FROM "{table}" ORDER BY rowid').fetchall()
            for table in tables
        }
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# red 1~3 — SLA 경계(due_at vs now, 경계 포함)
# ---------------------------------------------------------------------------


def test_due_at이_now보다_미래면_후보가_없다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    try:
        _seed_awaiting_ticket(
            completion,
            request_id=_ref("request", "r1"),
            due_at=SCAN_NOW + timedelta(hours=1),
            ticket_label="ticket-1",
        )
    finally:
        completion.close()

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert report.candidates == ()


def test_due_at이_now와_정확히_같으면_경계_포함으로_후보에_들어간다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    try:
        ticket_id, _ = _seed_awaiting_ticket(
            completion,
            request_id=_ref("request", "r2"),
            due_at=SCAN_NOW,
            ticket_label="ticket-2",
        )
    finally:
        completion.close()

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert [c.ticket_id for c in report.candidates] == [ticket_id]


def test_due_at이_now보다_과거면_후보1이고_전_필드가_정확하다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "r3")
    try:
        ticket_id, request_revision = _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-3"
        )
    finally:
        completion.close()

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert len(report.candidates) == 1
    candidate = report.candidates[0]
    assert candidate.ticket_id == ticket_id
    assert candidate.org_id == ORG_ID
    assert candidate.request_id == request_id
    assert candidate.attempt == 1
    assert candidate.owner_subject_id == _TICKET_OWNER
    assert candidate.awaiting_revision == 1
    assert candidate.request_revision == request_revision
    assert candidate.due_at == due_at
    assert candidate.lease_active is False
    assert candidate.delivery_attempts == 0


# ---------------------------------------------------------------------------
# red 4~5 — lease_active는 정보 전용이고 SLA 초과를 은폐하지 않는다
# ---------------------------------------------------------------------------


def test_활성_lease가_있어도_후보에_포함되고_lease_active는_true다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "r4")
    try:
        ticket_id, _ = _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-4"
        )
    finally:
        completion.close()
    _insert_lease(
        path,
        ticket_id=ticket_id,
        org_id=ORG_ID,
        request_id=request_id,
        expires_at=SCAN_NOW + timedelta(minutes=5),
        acquired_at=SCAN_NOW - timedelta(minutes=5),
    )

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert len(report.candidates) == 1
    assert report.candidates[0].lease_active is True


@pytest.mark.parametrize(
    "insert_lease_row,lease_state,expires_at_offset",
    [
        (False, "leased", timedelta(minutes=5)),  # lease 행 자체가 없음
        (True, "leased", -timedelta(minutes=1)),  # leased인데 만료됨
        # released면 만료 전이어도(=아직 문자열상 expires_at>now) inactive다 —
        # `state=='leased'`를 빼면 이 케이스만 살아남는 mutant를 잡는다.
        (True, "released", timedelta(minutes=5)),
        # S5.2 claim의 "active" 판정과 동형인 엄격 부등호(>) 경계 — expires_at이
        # now와 정확히 같으면 만료로 본다(>=로 완화하는 mutant를 잡는다).
        (True, "leased", timedelta(0)),
    ],
    ids=[
        "no_lease_row",
        "expired_leased_row",
        "released_but_not_yet_expired",
        "expires_at_equals_now_boundary",
    ],
)
def test_lease가_만료되었거나_없거나_released면_lease_active는_false다(
    tmp_path: Path, insert_lease_row: bool, lease_state: str, expires_at_offset: timedelta
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "r5")
    try:
        ticket_id, _ = _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-5"
        )
    finally:
        completion.close()
    if insert_lease_row:
        _insert_lease(
            path,
            ticket_id=ticket_id,
            org_id=ORG_ID,
            request_id=request_id,
            state=lease_state,
            expires_at=SCAN_NOW + expires_at_offset,
            acquired_at=SCAN_NOW - timedelta(minutes=10),
        )

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert len(report.candidates) == 1
    assert report.candidates[0].lease_active is False


# ---------------------------------------------------------------------------
# red 6 — delivery_attempts는 해당 ticket의 시도 행 수 그대로다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attempt_count", [0, 1, 2])
def test_delivery_attempts_카운트가_정확하다(tmp_path: Path, attempt_count: int) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", f"r6-{attempt_count}")
    try:
        ticket_id, _ = _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label=f"ticket-6-{attempt_count}"
        )
    finally:
        completion.close()
    for epoch in range(1, attempt_count + 1):
        _insert_attempt(
            path,
            attempt_id=_ref("receipt", f"attempt-{attempt_count}-{epoch}"),
            ticket_id=ticket_id,
            org_id=ORG_ID,
            request_id=request_id,
            epoch=epoch,
            outcome="undeliverable",
            reason_code="no_connected_worker",
            created_at=SCAN_NOW - timedelta(minutes=30),
        )

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert len(report.candidates) == 1
    assert report.candidates[0].delivery_attempts == attempt_count


# ---------------------------------------------------------------------------
# red 7~10 — 후보 조건 각각을 단독으로 위반하면 제외된다(mutation 격리)
# ---------------------------------------------------------------------------


def test_request가_awaitingapproval로_진행하면_제외된다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "r7")
    try:
        _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-7"
        )
        _advance_to_awaiting_approval(completion, request_id=request_id, after=due_at + timedelta(minutes=1))
    finally:
        completion.close()

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert report.candidates == ()


def test_ticket이_completed면_제외된다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "r8")
    try:
        ticket_id, _ = _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-8"
        )
        _corrupt_ticket_status(completion, ticket_id, "completed")
    finally:
        completion.close()

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert report.candidates == ()


def test_revision이_awaiting_revision_plus_1이_아니면_제외된다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "r9")
    try:
        ticket_id, request_revision = _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-9"
        )
        _corrupt_ticket_awaiting_revision(completion, ticket_id, request_revision + 5)
    finally:
        completion.close()

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert report.candidates == ()


def test_다른_ticket을_가리키는_awaitinganswer는_제외된다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "r10")
    try:
        _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-10"
        )
        _drift_awaiting_answer_ticket_id(
            completion, request_id=request_id, other_ticket_id=_ref("ticket", "other-10")
        )
    finally:
        completion.close()

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert report.candidates == ()


# ---------------------------------------------------------------------------
# red 11 — write 0(SQL 추적·파일 byte·전 테이블 스냅샷 3중 실증)
# ---------------------------------------------------------------------------


def test_스캔은_write_0이고_한_read_transaction으로_묶인다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "r11")
    try:
        ticket_id, _ = _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-11"
        )
    finally:
        completion.close()
    _insert_lease(
        path,
        ticket_id=ticket_id,
        org_id=ORG_ID,
        request_id=request_id,
        expires_at=SCAN_NOW + timedelta(minutes=5),
        acquired_at=SCAN_NOW - timedelta(minutes=5),
    )

    statements: list[str] = []
    real_connect = sqlite3.connect

    def _trace(sql: str) -> None:
        statements.append(sql.strip().split()[0].upper())

    def _spy_connect(database: str, *, uri: bool = False, timeout: float = 5.0) -> sqlite3.Connection:
        connection = real_connect(database, uri=uri, timeout=timeout)
        if uri and "mode=ro" in database:
            connection.set_trace_callback(_trace)
        return connection

    monkeypatch.setattr(
        "agent_org_network.durable_dispatch_timeout.sqlite3.connect", _spy_connect
    )

    before_bytes = path.read_bytes()
    before_stat = path.stat()
    before_dump = _dump_all_tables(path)

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)

    after_bytes = path.read_bytes()
    after_stat = path.stat()
    after_dump = _dump_all_tables(path)

    assert report.capable is True
    assert len(report.candidates) == 1

    # 물리적 증거 — 파일 byte·크기·mtime이 스캔 전후 완전히 동일하다.
    assert after_bytes == before_bytes
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    # 논리적 증거 — 모든 테이블의 전 행이 스캔 전후 완전히 동일하다.
    assert after_dump == before_dump

    # 구조적 증거 — 실행된 SQL 중 write 계열이 0이고, 정확히 한 BEGIN..COMMIT.
    begin_indices = [i for i, s in enumerate(statements) if s == "BEGIN"]
    commit_indices = [i for i, s in enumerate(statements) if s == "COMMIT"]
    assert len(begin_indices) == 1
    assert len(commit_indices) == 1
    begin_at, commit_at = begin_indices[0], commit_indices[0]
    assert begin_at < commit_at
    assert commit_at == len(statements) - 1
    writes = {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}
    assert not writes.intersection(statements)


# ---------------------------------------------------------------------------
# red 12 — org 격리·org_id=None이면 전역
# ---------------------------------------------------------------------------


def test_org_필터는_격리되고_org_id_none이면_전역이다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    org_a, org_b = _ref("org", "org-a"), _ref("org", "org-b")
    due_at = SCAN_NOW - timedelta(hours=1)
    request_a, request_b = _ref("request", "r12a"), _ref("request", "r12b")
    try:
        ticket_a, _ = _seed_awaiting_ticket(
            completion, request_id=request_a, org_id=org_a, due_at=due_at, ticket_label="ticket-12a"
        )
        ticket_b, _ = _seed_awaiting_ticket(
            completion, request_id=request_b, org_id=org_b, due_at=due_at, ticket_label="ticket-12b"
        )
    finally:
        completion.close()

    only_a = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW, org_id=org_a)
    assert only_a.capable is True
    assert [c.ticket_id for c in only_a.candidates] == [ticket_a]

    only_b = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW, org_id=org_b)
    assert only_b.capable is True
    assert [c.ticket_id for c in only_b.candidates] == [ticket_b]

    everything = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW, org_id=None)
    assert everything.capable is True
    assert {c.ticket_id for c in everything.candidates} == {ticket_a, ticket_b}


# ---------------------------------------------------------------------------
# red 13 — 전역 catalog 손상은 org 범위와 무관하게 fail-closed
# ---------------------------------------------------------------------------


def test_전역_catalog_손상은_capable_false이고_candidates가_비어있다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "r13")
    try:
        _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-13"
        )
    finally:
        completion.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE durable_dispatch_answer_receipts")
    connection.commit()
    connection.close()

    for org_id in (None, ORG_ID, _ref("org", "other")):
        report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW, org_id=org_id)
        assert report.capable is False
        assert report.candidates == ()


# ---------------------------------------------------------------------------
# red 14 — manifest 부재는 present=False로 정직하게 보고된다
# ---------------------------------------------------------------------------


def test_dispatch_delivery_manifest_부재는_present_false로_보고된다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    # S5.1 dispatch delivery schema는 의도적으로 migrate하지 않는다.

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is False
    assert report.dispatch_delivery_manifest_present is False
    assert report.candidates == ()


def test_기존_sqlite_파일이_아니면_present_false로_열지_못한다() -> None:
    for path in (":memory:", ""):
        report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
        assert report.capable is False
        assert report.dispatch_delivery_manifest_present is False
        assert report.candidates == ()


# ---------------------------------------------------------------------------
# red 15 — 정렬은 (due_at, ticket_id COLLATE BINARY)로 결정론적이다
# ---------------------------------------------------------------------------


def test_후보_정렬은_due_at_다음_ticket_id로_결정론적이다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    labels = ["ticket-x", "ticket-y", "ticket-z"]
    due_ats = {
        "ticket-x": SCAN_NOW - timedelta(hours=1),
        "ticket-y": SCAN_NOW - timedelta(hours=3),
        "ticket-z": SCAN_NOW - timedelta(hours=3),
    }
    try:
        for label in labels:
            _seed_awaiting_ticket(
                completion,
                request_id=_ref("request", label),
                due_at=due_ats[label],
                ticket_label=label,
            )
    finally:
        completion.close()

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    expected_order = sorted(labels, key=lambda label: (due_ats[label], _ref("ticket", label)))
    assert [c.ticket_id for c in report.candidates] == [_ref("ticket", label) for label in expected_order]


# ---------------------------------------------------------------------------
# red 16 — 시그니처 red(반환 dataclass에 write/repair 메서드 없음)
# ---------------------------------------------------------------------------


def test_시그니처는_db_path_now_org_id_뿐이고_반환_dataclass에_write_메서드가_없다() -> None:
    params = list(inspect.signature(scan_sqlite_dispatch_timeouts).parameters)
    assert params == ["db_path", "now", "org_id"]

    report_fields = {f.name for f in fields(DispatchTimeoutScanReport)}
    assert report_fields == {
        "capable",
        "detail",
        "dispatch_delivery_manifest_present",
        "scanned_at",
        "candidates",
    }
    candidate_fields = {f.name for f in fields(DispatchTimeoutCandidate)}
    assert candidate_fields == {
        "ticket_id",
        "org_id",
        "request_id",
        "attempt",
        "owner_subject_id",
        "awaiting_revision",
        "request_revision",
        "due_at",
        "lease_active",
        "delivery_attempts",
    }

    for cls in (DispatchTimeoutScanReport, DispatchTimeoutCandidate):
        own_methods = {
            name for name, value in vars(cls).items() if callable(value) and not name.startswith("__")
        }
        assert own_methods == set()
        for forbidden in ("write", "repair", "commit", "claim", "renew", "release", "reclaim"):
            assert not hasattr(cls, forbidden)


# ---------------------------------------------------------------------------
# BUSY — S5.5 자기 mode=ro connection의 lock 경합은 capability-우선 fail-closed
# ---------------------------------------------------------------------------


def test_다른_connection이_exclusive_lock을_쥐면_scan은_capable_false로_닫힌다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    try:
        _seed_awaiting_ticket(
            completion, request_id=_ref("request", "busy"), due_at=due_at, ticket_label="ticket-busy"
        )
    finally:
        completion.close()

    real_connect = sqlite3.connect

    # S5.5는 자기 timeout(=5.0, S4.6 판)을 정한다 — 테스트를 빠르게 유지하려고
    # 실제 연결의 대기시간만 짧게 오버라이드한다(BUSY 발생 원리는 그대로다).
    def _fast_timeout_connect(
        database: str, *, uri: bool = False, timeout: float = 5.0
    ) -> sqlite3.Connection:
        return real_connect(database, uri=uri, timeout=0.2)

    monkeypatch.setattr(
        "agent_org_network.durable_dispatch_timeout.sqlite3.connect", _fast_timeout_connect
    )

    blocker = real_connect(str(path), timeout=1.0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
        assert report.capable is False
        assert report.candidates == ()
    finally:
        blocker.rollback()
        blocker.close()

    # lock 경합이 해소된 뒤에는 정상 스캔이 가능하다(영구 손상이 아니다).
    recovered = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert recovered.capable is True
    assert len(recovered.candidates) == 1


# ---------------------------------------------------------------------------
# review-s55b P1-2 — `now` 형식 계약: UTC 정규화·tz-naive fail-closed
# ---------------------------------------------------------------------------


def test_now이_utc가_아닌_tz_aware여도_동일_순간이면_lease_active가_같다(tmp_path: Path) -> None:
    # `_instant`가 `astimezone(UTC)` 없이 그대로 렌더링하면 비-UTC offset
    # `now`의 canonical 문자열이 `+00:00` 문법을 벗어나 lease.expires_at과의
    # 사전순 비교가 붕괴한다 — 시그니처는 tz-aware만 요구하므로 KST 같은
    # 비-UTC(but 동일 순간) clock도 정당한 입력이고, 결과가 UTC와 같아야 한다.
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    due_at = SCAN_NOW - timedelta(hours=1)
    request_id = _ref("request", "p12a")
    try:
        ticket_id, _ = _seed_awaiting_ticket(
            completion, request_id=request_id, due_at=due_at, ticket_label="ticket-p12a"
        )
    finally:
        completion.close()
    _insert_lease(
        path,
        ticket_id=ticket_id,
        org_id=ORG_ID,
        request_id=request_id,
        expires_at=SCAN_NOW + timedelta(minutes=5),
        acquired_at=SCAN_NOW - timedelta(minutes=5),
    )

    kst_now = SCAN_NOW.astimezone(timezone(timedelta(hours=9)))
    assert kst_now.utcoffset() == timedelta(hours=9)
    assert kst_now == SCAN_NOW  # 같은 순간(instant)이지 다른 시각이 아니다

    utc_report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    kst_report = scan_sqlite_dispatch_timeouts(path, now=kst_now)

    assert utc_report.capable is True
    assert kst_report.capable is True
    assert len(utc_report.candidates) == 1
    assert len(kst_report.candidates) == 1
    assert utc_report.candidates[0].lease_active is True
    assert kst_report.candidates[0].lease_active is True


def test_tz_naive_now은_pending_ticket이_0건이어도_capable_false다(tmp_path: Path) -> None:
    # `_instant`는 후보를 순회하기 전 무조건 한 번 불린다 — tz-naive now의
    # 가드를 없애면 `datetime.astimezone()`이 naive datetime을 시스템
    # 로컬시간으로 조용히 해석해 예외를 던지지 않고, pending ticket이 0건인
    # DB에서는 그 렌더값이 어디에도 쓰이지 않아 "SLA 초과 없음"을 잘못
    # 확정하는 fail-open이 된다. ticket이 하나도 없어도 capability는 닫혀야
    # 한다.
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    completion.close()

    naive_now = datetime(2026, 7, 26, 12, 0, 0)
    assert naive_now.utcoffset() is None

    report = scan_sqlite_dispatch_timeouts(path, now=naive_now)
    assert report.capable is False
    assert report.candidates == ()


# ---------------------------------------------------------------------------
# review-s55b P1-3 — lease_active·delivery_attempts의 per-ticket 결박
# ---------------------------------------------------------------------------


def test_lease_active와_delivery_attempts는_ticket별로_정확히_결박된다(tmp_path: Path) -> None:
    # 두 후보 중 하나(due_at이 더 이른 쪽)에만 활성 lease·시도 1건을 붙인다.
    # `WHERE ticket_id=?`를 무력화하는 mutant는 이 두 값이 후보 사이에서
    # 뒤섞이거나(둘 다 True/1) COUNT가 전역 합계로 새는 것으로 드러난다.
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    request_1, request_2 = _ref("request", "p13-1"), _ref("request", "p13-2")
    due_at_1 = SCAN_NOW - timedelta(hours=3)
    due_at_2 = SCAN_NOW - timedelta(hours=1)
    try:
        ticket_1, _ = _seed_awaiting_ticket(
            completion, request_id=request_1, due_at=due_at_1, ticket_label="ticket-p13-1"
        )
        ticket_2, _ = _seed_awaiting_ticket(
            completion, request_id=request_2, due_at=due_at_2, ticket_label="ticket-p13-2"
        )
    finally:
        completion.close()
    _insert_lease(
        path,
        ticket_id=ticket_2,
        org_id=ORG_ID,
        request_id=request_2,
        expires_at=SCAN_NOW + timedelta(minutes=5),
        acquired_at=SCAN_NOW - timedelta(minutes=5),
    )
    _insert_attempt(
        path,
        attempt_id=_ref("receipt", "p13-attempt"),
        ticket_id=ticket_2,
        org_id=ORG_ID,
        request_id=request_2,
        created_at=SCAN_NOW - timedelta(minutes=10),
    )

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is True
    assert [c.ticket_id for c in report.candidates] == [ticket_1, ticket_2]
    first, second = report.candidates
    assert first.lease_active is False
    assert first.delivery_attempts == 0
    assert second.lease_active is True
    assert second.delivery_attempts == 1


# ---------------------------------------------------------------------------
# review-s55b P2-3 — scanned_at: 작업 목록 표지(감사용·write 근거 불가)
# ---------------------------------------------------------------------------


def test_scanned_at은_호출자_now를_canonical_utc로_그대로_반영한다(tmp_path: Path) -> None:
    # 두 번째 clock 읽기가 없다는 것을 실측한다 — UTC로 넘긴 now와 같은
    # 순간을 가리키는 KST(+09:00) now 둘 다 scanned_at이 동일한 UTC
    # canonical 값이어야 한다(원본 offset을 보존하는 mutant를 잡는다).
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    completion.close()

    kst_now = SCAN_NOW.astimezone(timezone(timedelta(hours=9)))
    assert kst_now == SCAN_NOW

    utc_report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    kst_report = scan_sqlite_dispatch_timeouts(path, now=kst_now)

    assert utc_report.capable is True
    assert kst_report.capable is True
    assert utc_report.scanned_at == SCAN_NOW
    assert kst_report.scanned_at == SCAN_NOW
    assert utc_report.scanned_at is not None
    assert kst_report.scanned_at is not None
    assert utc_report.scanned_at.utcoffset() == timedelta(0)
    assert kst_report.scanned_at.utcoffset() == timedelta(0)


def test_scanned_at은_capable_false_스키마_손상_경로에서도_채워진다(tmp_path: Path) -> None:
    # capable=False라도 "언제 관측을 시도했나"는 감사 대상이다 — 스캔이
    # 손상을 만난 시각도 scanned_at에 남아야 한다(None으로 흘리는 mutant를
    # 잡는다).
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    completion.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE durable_dispatch_answer_receipts")
    connection.commit()
    connection.close()

    report = scan_sqlite_dispatch_timeouts(path, now=SCAN_NOW)
    assert report.capable is False
    assert report.scanned_at == SCAN_NOW


def test_scanned_at은_tz_naive_now에서는_none이다(tmp_path: Path) -> None:
    # tz-naive now는 애초에 유효한 관측 시각이 아니다 — `_instant`가 이미
    # capable=False로 닫으므로, scanned_at도 위조된 canonical 값을 만들어
    # 내지 않고 정직하게 None으로 남는다(로컬 시스템 시각으로 조용히
    # 해석해 값을 채워 넣는 mutant를 잡는다).
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    completion.close()

    naive_now = datetime(2026, 7, 26, 12, 0, 0)
    report = scan_sqlite_dispatch_timeouts(path, now=naive_now)
    assert report.capable is False
    assert report.scanned_at is None


# ---------------------------------------------------------------------------
# review-s55b P2-2 — 경로 오류(ValueError·RuntimeError)도 typed report로 닫힌다
# (ADR 0042 §9 ⑱ S4.6 공통 교정)
# ---------------------------------------------------------------------------


def test_경로에_임베디드_nul이_있으면_valueerror가_typed_report로_닫힌다() -> None:
    # Path(...).resolve()가 embedded NUL에 ValueError를 던진다 — sqlite3.Error만
    # 잡으면 이 예외가 raw로 누수해 호출자가 capable=False 대신 예외로 중단된다.
    report = scan_sqlite_dispatch_timeouts("\x00abc", now=SCAN_NOW)
    assert report.capable is False
    assert report.dispatch_delivery_manifest_present is False
    assert report.candidates == ()


def test_expanduser가_실패하면_runtimeerror가_typed_report로_닫힌다() -> None:
    # 존재하지 않는 사용자의 `~user` 확장은 RuntimeError다.
    report = scan_sqlite_dispatch_timeouts("~nosuchuser1234/x.sqlite", now=SCAN_NOW)
    assert report.capable is False
    assert report.dispatch_delivery_manifest_present is False
    assert report.candidates == ()
