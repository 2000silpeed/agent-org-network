"""P17.9 S5.3 durable dispatch outbox consumer + delivery runner 테스트(ADR 0042 §9 ③⑥⑦⑫).

3-phase(TX-A claim → 전송(외부) → TX-B `_record`)와 BUSY/Conflict 흡수 계약을
검증한다. `contended`(락 경합·모름)와 `skipped`(도메인 경쟁 패배·정상 관찰)는
서로 다른 카운터이고, 비-Busy `DurableDispatchLeaseUnavailable`은 절대 흡수되지
않고 `run_once` 전체를 중단해 호출자에게 전파돼야 한다.
"""

from __future__ import annotations
# pyright: reportPrivateUsage=false
# pyright: reportArgumentType=false

import hashlib
import sqlite3
import threading
import typing
import uuid
from collections.abc import Callable
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.durable_dispatch_delivery import (
    Delivered,
    DispatchChannel,
    DispatchFrame,
    DispatchOutcome,
    DispatchOwner,
    DispatchOwnerDirectory,
    DispatchRunReport,
    DurableDispatchRunBusy,
    DurableDispatchRunError,
    DurableDispatchRunUnavailable,
    DurableDispatchRunner,
    Undeliverable,
    _AttemptRecord,
)
from agent_org_network.question_request import (
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_dispatch_delivery import (
    migrate_sqlite_durable_dispatch_delivery_schema,
)
from agent_org_network.sqlite_durable_dispatch_lease_uow import (
    DispatchLease,
    DurableDispatchLeaseError,
    DurableDispatchLeaseUnavailable,
    DurableDispatchLeaseUnitOfWork,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)
from agent_org_network.sqlite_durable_work_ticket_uow import (
    DurableWorkTicketEnqueueCommand,
    DurableWorkTicketEnqueueUnitOfWork,
)

NOW = datetime(2026, 7, 25, tzinfo=UTC)
_TTL = timedelta(minutes=5)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


def _canonical(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


ORG_ID = _ref("org", "org-1")
_TICKET_OWNER = _ref("subject", "owner-a")


class _Registry:
    """WorkTicket enqueue(S4.5)용 owner 주소 해석 Fake — seed 전용."""

    def __init__(self, *, owner: str | None = _TICKET_OWNER) -> None:
        self._owner = owner

    def resolve_owner_subject(self, *, org_id: str, agent_id: str) -> str | None:
        return self._owner


def _open_all(path: Path, *, timeout: float = 5.0) -> SqliteQuestionCompletionUnitOfWork:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    return SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: NOW,
        timeout=timeout,
    )


def _seed_dispatchable_ticket(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    request_id: str,
    org_id: str = ORG_ID,
    agent_id: str = "card-a",
    ticket_label: str = "ticket-1",
    owner: str | None = _TICKET_OWNER,
    question: str = "refund question",
    context_snapshot: str | None = None,
    session_id: str | None = None,
    enqueue_clock: Callable[[], datetime] = lambda: NOW,
) -> str:
    """ReadyToDispatch → S4.5 enqueue로 claim 가능한 pending WorkTicket을 만든다."""
    received = QuestionRequest.receive(
        org_id=org_id,
        requester_id="user",
        question=question,
        request_id_factory=lambda: request_id,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id=session_id,
        context_snapshot=context_snapshot,
    )
    completion.create(received)
    trigger_ref = _ref("trigger", request_id)
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=RouteTarget(intent="refund", agent_id=agent_id, requires_approval=False),
            attempt=1,
            trigger_key=trigger_ref,
            handling=HandlingAssignment(
                kind="system", ref=trigger_ref, due_at=NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set(request_id, 0, received, ready)

    enqueue_uow = DurableWorkTicketEnqueueUnitOfWork(
        completion=completion,
        registry=_Registry(owner=owner),
        clock=enqueue_clock,
        ticket_id_factory=lambda: ticket_label,
        receipt_id_factory=lambda: f"receipt-{ticket_label}",
    )
    enqueued = enqueue_uow.enqueue(
        command=DurableWorkTicketEnqueueCommand(request_id, 1, 1)
    )
    return enqueued.ticket_id


def _lease_uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    holder_id: str = "dispatcher-a",
    clock: Callable[[], datetime] = lambda: NOW,
    ttl: timedelta = _TTL,
    fault_injector: Callable[[str], None] | None = None,
) -> DurableDispatchLeaseUnitOfWork:
    return DurableDispatchLeaseUnitOfWork(
        completion=completion,
        holder_id=holder_id,
        clock=clock,
        lease_ttl=ttl,
        fault_injector=fault_injector,
    )


def _runner(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    lease_uow: DurableDispatchLeaseUnitOfWork,
    channel: DispatchChannel,
    directory: DispatchOwnerDirectory,
    clock: Callable[[], datetime] = lambda: NOW,
    batch_limit: int = 32,
    fault_injector: Callable[[str], None] | None = None,
) -> DurableDispatchRunner:
    def attempt_id_factory() -> str:
        # 테스트마다 여러 runner 인스턴스를 만들 수 있으므로(재전달 시나리오),
        # 인스턴스-local 카운터가 아니라 전역적으로 유일한 값을 쓴다.
        return uuid.uuid4().hex

    return DurableDispatchRunner(
        completion=completion,
        lease_uow=lease_uow,
        channel=channel,
        directory=directory,
        clock=clock,
        attempt_id_factory=attempt_id_factory,
        batch_limit=batch_limit,
        fault_injector=fault_injector,
    )


def _lease_row(completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str) -> sqlite3.Row | None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        row = tx.execute(
            "SELECT * FROM durable_dispatch_leases WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        tx.commit()
        return row


def _attempt_rows(completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str) -> list[sqlite3.Row]:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        rows = tx.execute(
            "SELECT * FROM durable_dispatch_delivery_attempts WHERE ticket_id=? ORDER BY created_at",
            (ticket_id,),
        ).fetchall()
        tx.commit()
        return rows


def _attempt_count(completion: SqliteQuestionCompletionUnitOfWork) -> int:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        count = tx.execute("SELECT count(*) FROM durable_dispatch_delivery_attempts").fetchone()[0]
        tx.commit()
        return int(count)


def _ticket_row(completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str) -> sqlite3.Row:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        row = tx.execute(
            "SELECT * FROM durable_linked_work_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        tx.commit()
        assert row is not None
        return row


def _insert_ticket_row(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    ticket_id: str,
    org_id: str,
    request_id: str,
    attempt: int,
    awaiting_revision: int,
    owner: str = _TICKET_OWNER,
    status: str = "pending",
) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "INSERT INTO durable_linked_work_tickets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ticket_id,
                org_id,
                request_id,
                attempt,
                awaiting_revision,
                "a" * 64,
                owner,
                status,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        tx.commit()


class _FixedDirectory:
    """고정 owner를 돌려주는 Fake `DispatchOwnerDirectory`."""

    def __init__(self, owner: DispatchOwner | None) -> None:
        self._owner = owner

    def resolve_owner(self, *, org_id: str, agent_id: str) -> DispatchOwner | None:
        return self._owner


class _RaisingDirectory:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def resolve_owner(self, *, org_id: str, agent_id: str) -> DispatchOwner | None:
        raise self._error


class _FixedChannel:
    """고정 outcome을 돌려주고 전달된 frame을 spy로 기록하는 Fake `DispatchChannel`."""

    def __init__(self, outcome: DispatchOutcome) -> None:
        self._outcome = outcome
        self.calls: list[DispatchFrame] = []

    def deliver(self, frame: DispatchFrame) -> DispatchOutcome:
        self.calls.append(frame)
        return self._outcome


class _RaisingChannel:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls: list[DispatchFrame] = []

    def deliver(self, frame: DispatchFrame) -> DispatchOutcome:
        self.calls.append(frame)
        raise self._error


class _ForgedChannel:
    """`DispatchOutcome`이 아닌 값을 돌려주는 Fake — Protocol이 반환 타입을 강제하지 못한다."""

    def __init__(self, forged: object) -> None:
        self._forged = forged
        self.calls: list[DispatchFrame] = []

    def deliver(self, frame: DispatchFrame) -> DispatchOutcome:
        self.calls.append(frame)
        return self._forged  # type: ignore[return-value]


class _LockingChannel:
    """deliver() 시점에 별도 connection으로 write lock을 잡아 TX-B만 BUSY를 유도한다."""

    def __init__(self, db_path: Path, *, outcome: DispatchOutcome) -> None:
        self._db_path = db_path
        self._outcome = outcome
        self.calls: list[DispatchFrame] = []
        self.blocker: sqlite3.Connection | None = None

    def deliver(self, frame: DispatchFrame) -> DispatchOutcome:
        self.calls.append(frame)
        self.blocker = sqlite3.connect(str(self._db_path), timeout=1.0)
        self.blocker.execute("PRAGMA foreign_keys=ON")
        self.blocker.execute("BEGIN IMMEDIATE")
        return self._outcome

    def release(self) -> None:
        if self.blocker is not None:
            self.blocker.rollback()
            self.blocker.close()
            self.blocker = None


class _HijackingChannel:
    """지정된 ticket_id로 deliver()가 불리면, 그 시점에 다른 holder가 만료된
    lease를 reclaim(epoch+1)해 §9 ⑭ "TTL 초과+타 인스턴스 탈취"를 결정론으로
    재현한다. 지정되지 않은 ticket은 그대로 고정 outcome을 돌려준다."""

    def __init__(
        self,
        completion: SqliteQuestionCompletionUnitOfWork,
        *,
        hijack_ticket_id: str,
        later_clock: Callable[[], datetime],
        outcome: DispatchOutcome,
        thief_holder_id: str = "dispatcher-thief",
    ) -> None:
        self._completion = completion
        self._hijack_ticket_id = hijack_ticket_id
        self._later_clock = later_clock
        self._outcome = outcome
        self._thief_holder_id = thief_holder_id
        self.calls: list[DispatchFrame] = []

    def deliver(self, frame: DispatchFrame) -> DispatchOutcome:
        self.calls.append(frame)
        if frame.ticket_id == self._hijack_ticket_id:
            thief = DurableDispatchLeaseUnitOfWork(
                completion=self._completion,
                holder_id=self._thief_holder_id,
                clock=self._later_clock,
                lease_ttl=_TTL,
            )
            thief.claim(ticket_id=self._hijack_ticket_id)
        return self._outcome


def _owner_directory(owner_id: str = "owner-a", owner_ref: str = _TICKET_OWNER) -> _FixedDirectory:
    return _FixedDirectory(DispatchOwner(owner_id=owner_id, owner_subject_ref=owner_ref))


# ---------------------------------------------------------------------------
# 생성자 — capability 미설치 시 생성 시점에 unavailable이다(S4.5·S5.2 동형).
# ---------------------------------------------------------------------------


def test_capability_손상_후_runner_생성은_unavailable이다(tmp_path: Path) -> None:
    # lease_uow는 손상 전에 먼저 만들어 둔다 — lease_uow 자신의 constructor
    # validate가 아니라 runner 자신의 open-time validate를 단독으로 겨냥한다.
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-ctor")
        ticket_id = _seed_dispatchable_ticket(
            completion, request_id=request_id, ticket_label="ticket-ctor"
        )
        lease_uow = _lease_uow(completion, clock=lambda: NOW)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "INSERT INTO durable_dispatch_leases VALUES(?,?,?,?,?,?,?,?)",
                (
                    ticket_id,
                    ORG_ID,
                    request_id,
                    1,
                    _ref("subject", "x"),
                    "not-a-valid-state",
                    "2026-01-01T00:05:00.000000+00:00",
                    "2026-01-01T00:00:00.000000+00:00",
                ),
            )
            tx.commit()

        # 생성자의 open-time validate는 S5.1 저장소 capability(스키마 상태)를
        # 검증한다 — 러너 설정이 아니므로 lease 계열로 남는다(team-lead
        # 2026-07-25 재확정). red 30(run_once 경로의 같은 손상)과 정확히
        # 같은 타입이어야 한다 — 진입점에 따라 다른 타입이 되면 안 된다.
        with pytest.raises(DurableDispatchLeaseUnavailable) as excinfo:
            _runner(
                completion,
                lease_uow=lease_uow,
                channel=_FixedChannel(Delivered()),
                directory=_owner_directory(),
            )
        assert type(excinfo.value) is DurableDispatchLeaseUnavailable  # wrap 없이 그대로(Run* 아님)
    finally:
        completion.close()


@pytest.mark.parametrize("batch_limit", [0, -1])
def test_batch_limit이_1미만이면_run_unavailable이다(tmp_path: Path, batch_limit: int) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        lease_uow = _lease_uow(completion)
        with pytest.raises(DurableDispatchRunUnavailable) as excinfo:
            _runner(
                completion,
                lease_uow=lease_uow,
                channel=_FixedChannel(Delivered()),
                directory=_owner_directory(),
                batch_limit=batch_limit,
            )
        assert not isinstance(excinfo.value, DurableDispatchLeaseError)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 26 — DurableDispatchRunError 계열은 lease 계열과 완전히 분리된 계층이다
# (review-s53 P2-7 신설 확정).
# ---------------------------------------------------------------------------


def test_durabledispatchrunerror_계열은_lease_계열과_분리된_계층이다() -> None:
    assert issubclass(DurableDispatchRunBusy, DurableDispatchRunUnavailable)
    assert issubclass(DurableDispatchRunUnavailable, DurableDispatchRunError)
    assert not issubclass(DurableDispatchRunUnavailable, DurableDispatchLeaseError)
    assert not issubclass(DurableDispatchRunBusy, DurableDispatchLeaseError)
    assert not issubclass(DurableDispatchLeaseError, DurableDispatchRunError)


# ---------------------------------------------------------------------------
# 추가 — claim 이후(같은 TX-A commit 안) Request 상태가 바뀌면 재시도 대상으로
# 되돌아간다(3-phase 의사코드의 "claim 이후 대상 상태 변동" 분기 단독 격리).
# ---------------------------------------------------------------------------


def test_claim_이후_ticket_상태변동은_channel_error로_재시도_대상이_된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-state-drift")
        ticket_id = _seed_dispatchable_ticket(
            completion, request_id=request_id, ticket_label="ticket-state-drift"
        )
        tx = completion.durable_transaction()

        def corrupt_awaiting_revision(point: str) -> None:
            # lease insert와 같은 TX-A transaction 안에서 ticket.awaiting_revision을
            # 어긋나게 만든다 — claim() 자신은 그대로 성공하고(커밋), `_prepare`의
            # 재조회만 이 어긋남을 본다.
            if point == "after_lease_insert":
                tx.execute(
                    "UPDATE durable_linked_work_tickets SET awaiting_revision=99 WHERE ticket_id=?",
                    (ticket_id,),
                )

        lease_uow = _lease_uow(
            completion, clock=lambda: NOW, fault_injector=corrupt_awaiting_revision
        )
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert len(channel.calls) == 0  # 전송 시도 자체를 하지 않는다
        assert report.claimed == 1
        assert report.undeliverable == 1
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["reason_code"] == "channel_error"
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "released"  # 즉시 release되어 다음 run이 재시도한다
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 1 — 연결된 워커: 시도1 delivered/ok · lease renew(epoch 불변) · ticket pending 유지
# ---------------------------------------------------------------------------


def test_연결된_워커에게_전달하면_시도1이_delivered이고_lease가_renew된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r1")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id)
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.scanned == 1
        assert report.claimed == 1
        assert report.delivered == 1
        assert report.undeliverable == 0
        assert report.skipped == 0
        assert report.contended == 0

        assert len(channel.calls) == 1
        frame = channel.calls[0]
        assert frame.ticket_id == ticket_id
        assert frame.request_id == request_id
        assert frame.org_id == ORG_ID
        assert frame.attempt == 1
        assert frame.lease_epoch == 1
        assert frame.agent_id == "card-a"
        assert frame.owner_id == "owner-a"

        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == "delivered"
        assert attempts[0]["reason_code"] == "ok"
        assert attempts[0]["lease_epoch"] == 1
        assert attempts[0]["target_subject_ref"] == _TICKET_OWNER

        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"
        assert lease_row["lease_epoch"] == 1  # renew는 epoch 불변

        ticket_row = _ticket_row(completion, ticket_id)
        assert ticket_row["status"] == "pending"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 14 — DispatchFrame이 route/question/context/session을 digest 없이 그대로 나른다
# ---------------------------------------------------------------------------


def test_dispatchframe은_question_context_session을_digest_없이_그대로_나른다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-frame")
        ticket_id = _seed_dispatchable_ticket(
            completion,
            request_id=request_id,
            ticket_label="ticket-frame",
            question="환불 절차가 궁금합니다",
            context_snapshot="이전 대화 요약",
            session_id="session-abc",
        )
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        runner.run_once()

        assert len(channel.calls) == 1
        frame = channel.calls[0]
        assert frame.question == "환불 절차가 궁금합니다"
        assert frame.context_snapshot == "이전 대화 요약"
        assert frame.session_id == "session-abc"
        assert ticket_id  # 참조 유지용
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 2 — 미연결 undeliverable → release → 두 번째 run이 epoch2로 재전달한다
# ---------------------------------------------------------------------------


def test_미연결_undeliverable은_release되고_다음_run이_epoch2로_재전달한다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r2")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-2")
        channel = _FixedChannel(Undeliverable(reason_code="no_connected_worker"))
        directory = _owner_directory()
        first_lease_uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        runner = _runner(
            completion, lease_uow=first_lease_uow, channel=channel, directory=directory, clock=lambda: NOW
        )

        first = runner.run_once()
        assert first.delivered == 0
        assert first.undeliverable == 1

        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == "undeliverable"
        assert attempts[0]["reason_code"] == "no_connected_worker"
        assert attempts[0]["lease_epoch"] == 1

        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "released"

        later = NOW + timedelta(minutes=1)
        second_channel = _FixedChannel(Delivered())
        second_lease_uow = _lease_uow(completion, holder_id="b", clock=lambda: later)
        second_runner = _runner(
            completion,
            lease_uow=second_lease_uow,
            channel=second_channel,
            directory=directory,
            clock=lambda: later,
        )
        second = second_runner.run_once()
        assert second.claimed == 1
        assert second.delivered == 1

        attempts2 = _attempt_rows(completion, ticket_id)
        assert len(attempts2) == 2
        assert attempts2[1]["lease_epoch"] == 2

        # P1-2(review-s53): DispatchFrame.attempt(WorkTicket 실행 시도)와
        # lease_epoch(dispatch lease epoch)은 다른 축이다 — 재전달로 epoch만
        # 2로 올라가고 attempt는 그대로 1이어야 두 필드가 서로 뒤바뀌거나
        # 한쪽이 다른 쪽 값을 베끼는 혼용을 잡는다.
        assert len(second_channel.calls) == 1
        assert second_channel.calls[0].attempt == 1
        assert second_channel.calls[0].lease_epoch == 2
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 3·21 — owner drift: channel.deliver 호출 0(spy) · reason_code=owner_drift ·
# release. 이 쌍(호출 0 + owner_drift 기록)이 곧 red 21이다 — owner_drift 행은
# deliver를 호출하지 않은 경로에서만 생성됨을 증명한다.
# ---------------------------------------------------------------------------


def test_owner_drift는_전송_호출_없이_undeliverable로_release된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r3")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-3")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _FixedDirectory(
            DispatchOwner(owner_id="owner-b", owner_subject_ref=_ref("subject", "owner-b"))
        )
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert len(channel.calls) == 0  # 전송 호출 자체를 하지 않는다
        assert report.claimed == 1
        assert report.undeliverable == 1
        assert report.delivered == 0

        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == "undeliverable"
        assert attempts[0]["reason_code"] == "owner_drift"

        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "released"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 4 — directory 예외 → owner_drift와 같은 경로(전송 0)로 흡수된다
# ---------------------------------------------------------------------------


def test_directory_예외는_owner_drift와_같은_경로로_흡수되고_전송0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r4")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-4")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _RaisingDirectory(RuntimeError("registry unavailable"))
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert len(channel.calls) == 0
        assert report.undeliverable == 1
        attempts = _attempt_rows(completion, ticket_id)
        assert attempts[0]["reason_code"] == "owner_drift"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 5 — deliver 예외·위조 반환은 channel_error로 흡수되고 부분 쓰기가 없다
# ---------------------------------------------------------------------------


def test_deliver_예외는_channel_error로_흡수된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r5a")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-5a")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _RaisingChannel(RuntimeError("channel down"))
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.undeliverable == 1
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["reason_code"] == "channel_error"
    finally:
        completion.close()


def test_deliver_위조_반환은_channel_error로_흡수된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r5b")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-5b")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _ForgedChannel(object())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.undeliverable == 1
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["reason_code"] == "channel_error"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 20 — 채널이 Undeliverable(reason_code="owner_drift")를 자기보고하면
#          channel_error로 강등 기록된다(architect-s5 2026-07-25 정정 —
#          owner_drift는 중앙만 내리는 판정이라 채널 포트 값이 아니다).
# ---------------------------------------------------------------------------


def test_채널이_owner_drift를_자기보고하면_channel_error로_강등된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r20")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-20")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        # Undeliverable.reason_code Literal은 2값이지만 plain dataclass는
        # 런타임에 강제하지 않는다 — 채널이 이렇게 위조해도 실제 방어선은
        # _record_of의 화이트리스트 검사다(reportArgumentType=false로 이
        # 파일에서는 타입체커가 이 위조를 막지 않는다).
        forged = Undeliverable(reason_code="owner_drift")
        channel = _FixedChannel(forged)
        directory = _owner_directory()  # owner는 일치 — 실제 owner_drift 경로가 아니다
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert len(channel.calls) == 1  # owner 일치라 전송은 실제로 호출된다
        assert report.undeliverable == 1
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["reason_code"] == "channel_error"  # 자기보고는 강등된다
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 21 — 채널이 허용 밖 임의 문자열 reason을 반환하면 전부 channel_error로
# 강등된다.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forged_reason", ["", "unknown", "delivered"])
def test_채널이_허용_밖_reason을_반환하면_channel_error로_강등된다(
    tmp_path: Path, forged_reason: str
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        label = forged_reason or "blank"
        request_id = _ref("request", f"r21-{label}")
        ticket_id = _seed_dispatchable_ticket(
            completion, request_id=request_id, ticket_label=f"ticket-21-{label}"
        )
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Undeliverable(reason_code=forged_reason))
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.undeliverable == 1
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["reason_code"] == "channel_error"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 22 — 포트 `Undeliverable.reason_code`의 Literal 인자에 "owner_drift"가
# 선언돼 있지 않다(타입 수준 분리를 구조적으로 고정 — plain dataclass가
# 런타임에 강제하지 못하는 부분을 이 red가 타입 선언 자체로 봉인한다).
# ---------------------------------------------------------------------------


def test_undeliverable_reason_code_literal에는_owner_drift가_선언되지_않는다() -> None:
    hints = typing.get_type_hints(Undeliverable, include_extras=True)
    reason_args = typing.get_args(hints["reason_code"])
    assert "owner_drift" not in reason_args
    assert set(reason_args) == {"no_connected_worker", "channel_error"}


# ---------------------------------------------------------------------------
# red 6 — before_send fault: 시도0 · lease leased 유지 → 만료 뒤 epoch2 재전달
#         (중복 수신 1회 실증 — 워커는 아직 아무것도 받지 않았다)
# ---------------------------------------------------------------------------


def test_before_send_fault는_시도0이고_lease_leased_유지_후_만료뒤_epoch2로_재전달된다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r6")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-6")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()

        def raise_at(point: str) -> None:
            if point == "before_send":
                raise RuntimeError(point)

        runner = _runner(
            completion, lease_uow=lease_uow, channel=channel, directory=directory,
            clock=lambda: NOW, fault_injector=raise_at,
        )
        with pytest.raises(RuntimeError, match="before_send"):
            runner.run_once()

        assert len(channel.calls) == 0  # 워커는 아직 받지 못했다
        assert _attempt_count(completion) == 0
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"
        assert lease_row["lease_epoch"] == 1
        assert lease_row["expires_at"] == _canonical(NOW + _TTL)

        later = NOW + timedelta(minutes=10)  # ttl(5분) 경과
        later_channel = _FixedChannel(Delivered())
        later_lease_uow = _lease_uow(completion, holder_id="b", clock=lambda: later)
        later_runner = _runner(
            completion, lease_uow=later_lease_uow, channel=later_channel, directory=directory,
            clock=lambda: later,
        )
        second = later_runner.run_once()
        assert second.delivered == 1
        assert len(later_channel.calls) == 1
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["lease_epoch"] == 2
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 7 — after_send fault: 워커는 받았는데 기록 없음(at-least-once 실증) →
#         lease leased 유지 → 만료 뒤 epoch2 재전달(중복 수신 실증)
# ---------------------------------------------------------------------------


def test_after_send_fault는_워커전달후_기록없이_동일하게_재전달된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r7")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-7")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()

        def raise_at(point: str) -> None:
            if point == "after_send":
                raise RuntimeError(point)

        runner = _runner(
            completion, lease_uow=lease_uow, channel=channel, directory=directory,
            clock=lambda: NOW, fault_injector=raise_at,
        )
        with pytest.raises(RuntimeError, match="after_send"):
            runner.run_once()

        assert len(channel.calls) == 1  # 워커는 이미 받았다 — 기록만 없다
        assert _attempt_count(completion) == 0
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"
        assert lease_row["lease_epoch"] == 1

        later = NOW + timedelta(minutes=10)
        later_channel = _FixedChannel(Delivered())
        later_lease_uow = _lease_uow(completion, holder_id="b", clock=lambda: later)
        later_runner = _runner(
            completion, lease_uow=later_lease_uow, channel=later_channel, directory=directory,
            clock=lambda: later,
        )
        second = later_runner.run_once()
        assert second.delivered == 1  # 두 번째 전달 — 워커 입장에선 중복 수신
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["lease_epoch"] == 2
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 8 — after_attempt fault: 시도 행 삽입과 lease 갱신이 한 transaction으로
#         함께 롤백된다(lease는 claim 직후 상태 그대로 남는다)
# ---------------------------------------------------------------------------


def test_after_attempt_fault는_시도행과_lease_갱신을_함께_롤백한다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r8")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-8")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()

        def raise_at(point: str) -> None:
            if point == "after_attempt":
                raise RuntimeError(point)

        runner = _runner(
            completion, lease_uow=lease_uow, channel=channel, directory=directory,
            clock=lambda: NOW, fault_injector=raise_at,
        )
        with pytest.raises(RuntimeError, match="after_attempt"):
            runner.run_once()

        assert _attempt_count(completion) == 0  # INSERT가 롤백됐다
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"  # renew가 실행되지 않았다(롤백)
        assert lease_row["expires_at"] == _canonical(NOW + _TTL)
        assert lease_row["lease_epoch"] == 1
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red — after_record fault(4번째 fault point 배선 확인): TX-B는 이미 commit돼
#       DB에는 남지만, 이 ticket의 counting은 완료되지 않은 채 run 전체가 중단된다.
# ---------------------------------------------------------------------------


def test_after_record_fault는_tx_b_commit_이후에_run을_중단시킨다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-after-record")
        ticket_id = _seed_dispatchable_ticket(
            completion, request_id=request_id, ticket_label="ticket-after-record"
        )
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()

        def raise_at(point: str) -> None:
            if point == "after_record":
                raise RuntimeError(point)

        runner = _runner(
            completion, lease_uow=lease_uow, channel=channel, directory=directory,
            clock=lambda: NOW, fault_injector=raise_at,
        )
        with pytest.raises(RuntimeError, match="after_record"):
            runner.run_once()

        # TX-B는 fault 이전에 이미 commit됐다 — 시도 행·renew는 남아 있다.
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == "delivered"
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 9 — 같은 epoch 재기록 → UNIQUE(ticket_id, lease_epoch) IntegrityError
#         (wrap되지 않고 그대로 관찰돼야 한다 — 스키마 멱등 앵커의 실 방어선)
# ---------------------------------------------------------------------------


def test_같은_epoch_재기록은_unique_integrityerror이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r9")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-9")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        lease = lease_uow.claim(ticket_id=ticket_id)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        delivered_record = _AttemptRecord(outcome="delivered", reason_code="ok")
        runner._record(lease=lease, record=delivered_record, target_ref=_TICKET_OWNER)
        with pytest.raises(sqlite3.IntegrityError):
            runner._record(lease=lease, record=delivered_record, target_ref=_TICKET_OWNER)

        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1  # 두 번째 시도는 커밋되지 않았다
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 10 — 활성 lease를 다른 holder가 쥔 ticket은 skip되고 write0이다
# ---------------------------------------------------------------------------


def test_활성_lease_ticket은_다른_runner에서_skip되고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r10")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-10")
        holder_a = _lease_uow(completion, holder_id="dispatcher-a", clock=lambda: NOW)
        holder_a.claim(ticket_id=ticket_id)  # 이미 활성 lease를 쥔 상태

        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        holder_b = _lease_uow(completion, holder_id="dispatcher-b", clock=lambda: NOW)
        runner = _runner(completion, lease_uow=holder_b, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.scanned == 1
        assert report.claimed == 0
        assert report.skipped == 1
        assert report.contended == 0
        assert len(channel.calls) == 0
        assert _attempt_count(completion) == 0
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 11 — Request가 AwaitingAnswer가 아니면 claim 단계에서 skip되고 write0이다
# ---------------------------------------------------------------------------


def test_request가_awaitinganswer가_아니면_claim_단계에서_skip되고_write0이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r11")
        received = QuestionRequest.receive(
            org_id=ORG_ID,
            requester_id="user",
            question="q",
            request_id_factory=lambda: request_id,
            clock=lambda: NOW - timedelta(minutes=2),
            due_at=NOW + timedelta(hours=1),
        )
        completion.create(received)
        trigger_ref = _ref("trigger", request_id)
        ready = received.record_initial_routing(
            intent="refund",
            disposition="routed",
            target=ReadyToDispatch(
                route=RouteTarget(intent="refund", agent_id="card-a", requires_approval=False),
                attempt=1,
                trigger_key=trigger_ref,
                handling=HandlingAssignment(
                    kind="system", ref=trigger_ref, due_at=NOW + timedelta(hours=1)
                ),
            ),
            clock=lambda: NOW - timedelta(minutes=1),
        )
        assert completion.compare_and_set(request_id, 0, received, ready)

        # WorkTicket 행을 enqueue UoW 없이 직접 삽입한다 — Request는 여전히
        # ReadyToDispatch라 claim()의 AwaitingAnswer 검사만 단독으로 겨냥한다.
        ticket_id = _ref("ticket", "manual-11")
        _insert_ticket_row(
            completion, ticket_id=ticket_id, org_id=ORG_ID, request_id=request_id,
            attempt=1, awaiting_revision=0,
        )

        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.scanned == 1
        assert report.claimed == 0
        assert report.skipped == 1
        assert len(channel.calls) == 0
        assert _attempt_count(completion) == 0
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 12 — batch_limit·생성순 정렬이 결정론적이다
# ---------------------------------------------------------------------------


def test_batch_limit과_생성순_정렬이_결정론적이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        directory = _owner_directory()
        specs = [
            ("r-late", "ticket-late", NOW + timedelta(minutes=2)),
            ("r-early", "ticket-early", NOW),
            ("r-mid", "ticket-mid", NOW + timedelta(minutes=1)),
        ]
        ticket_ids: dict[str, str] = {}
        for seed, label, created_at in specs:
            request_id = _ref("request", seed)
            ticket_ids[label] = _seed_dispatchable_ticket(
                completion,
                request_id=request_id,
                ticket_label=label,
                enqueue_clock=lambda created_at=created_at: created_at,
            )

        operating_now = NOW + timedelta(minutes=10)
        channel = _FixedChannel(Delivered())
        lease_uow = _lease_uow(completion, clock=lambda: operating_now)
        runner = _runner(
            completion, lease_uow=lease_uow, channel=channel, directory=directory,
            clock=lambda: operating_now, batch_limit=2,
        )

        report = runner.run_once()

        assert report.scanned == 2  # batch_limit이 후보 조회 자체를 제한한다
        assert report.claimed == 2
        assert len(channel.calls) == 2
        assert channel.calls[0].ticket_id == ticket_ids["ticket-early"]
        assert channel.calls[1].ticket_id == ticket_ids["ticket-mid"]
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 13 — DispatchRunReport에는 문자열 필드가 없다(자유 문장·원문·예외 메시지 금지)
# ---------------------------------------------------------------------------


def test_dispatchrunreport에는_문자열_필드가_없다() -> None:
    for field in fields(DispatchRunReport):
        assert field.type == "int", f"{field.name}은 int여야 합니다(자유 문장 노출 금지)."


# ---------------------------------------------------------------------------
# red 15·16 — 다른 connection이 lock을 쥐면 run_once는 죽지 않고 contended가
#             오르며 write0이다. lock 해제 뒤 재실행은 정상이다(영구 잠금 아님).
# ---------------------------------------------------------------------------


def test_다른_connection이_lock을_쥐면_run_once는_안죽고_contended가_오르며_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path, timeout=0.2)
    try:
        request_id = _ref("request", "r15")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-15")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        blocker = sqlite3.connect(str(path), timeout=1.0)
        blocker.execute("PRAGMA foreign_keys=ON")
        blocker.execute("BEGIN IMMEDIATE")
        try:
            report = runner.run_once()
        finally:
            blocker.rollback()
            blocker.close()

        assert report.scanned == 1
        assert report.contended == 1
        assert report.claimed == 0
        assert len(channel.calls) == 0
        assert _attempt_count(completion) == 0
        assert _lease_row(completion, ticket_id) is None

        # lock 해제 뒤 재실행은 정상이다(영구 잠금이 아니다).
        second = runner.run_once()
        assert second.delivered == 1
        assert len(channel.calls) == 1
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 17·30 — capability 손상 후 run_once는 Unavailable을 전파하고 run을
#             중단한다(비-Busy Unavailable을 삼키지 않는다는 실증). 이 손상은
#             claim() 자신의 in-transaction validate가 잡는 lease capability
#             오류라 `DurableDispatchLeaseUnavailable`이 **wrap 없이 그대로**
#             전파돼야 한다(`type(...) is`로 정확한 타입까지 단언 — Run*로
#             재포장되지 않았음을 확인).
# ---------------------------------------------------------------------------


def test_capability_손상_후_run_once는_unavailable을_전파하고_run을_중단한다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r17")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-17")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_work_tickets SET org_id=? WHERE ticket_id=?",
                (_ref("org", "org-corrupted"), ticket_id),
            )
            tx.commit()

        with pytest.raises(DurableDispatchLeaseUnavailable) as excinfo:
            runner.run_once()
        assert type(excinfo.value) is DurableDispatchLeaseUnavailable  # wrap 없이 그대로

        assert len(channel.calls) == 0  # 삼키지 않았다 — 전송 시도조차 하지 않았다
        assert _attempt_count(completion) == 0
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 18 — TX-B(_record)만 BUSY면 시도0·lease leased 유지 → 두 번째 run이
#          epoch2로 재전달한다(crash point ②와 관측이 같다)
# ---------------------------------------------------------------------------


def test_tx_b만_busy이면_시도0이고_lease_leased_유지_후_epoch2로_재전달된다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path, timeout=0.2)
    try:
        request_id = _ref("request", "r18")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-18")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _LockingChannel(path, outcome=Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        try:
            report = runner.run_once()
        finally:
            channel.release()

        assert report.claimed == 1
        assert report.contended == 1
        assert report.delivered == 0
        assert len(channel.calls) == 1  # 워커는 이미 받았다 — TX-B만 실패했다
        assert _attempt_count(completion) == 0
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"
        assert lease_row["lease_epoch"] == 1

        later = NOW + timedelta(minutes=10)
        later_channel = _FixedChannel(Delivered())
        later_lease_uow = _lease_uow(completion, holder_id="b", clock=lambda: later)
        later_runner = _runner(
            completion, lease_uow=later_lease_uow, channel=later_channel, directory=directory,
            clock=lambda: later,
        )
        second = later_runner.run_once()
        assert second.delivered == 1
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        assert attempts[0]["lease_epoch"] == 2
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 19 — contended와 skipped는 같은 run 안에서도 서로 다른 카운터로 관측된다
# ---------------------------------------------------------------------------


def test_contended와_skipped는_같은_run에서_서로_다른_카운터로_관측된다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_a = _seed_dispatchable_ticket(
            completion, request_id=_ref("request", "r19a"), ticket_label="ticket-19a",
            enqueue_clock=lambda: NOW,
        )
        ticket_b = _seed_dispatchable_ticket(
            completion, request_id=_ref("request", "r19b"), ticket_label="ticket-19b",
            enqueue_clock=lambda: NOW + timedelta(minutes=1),
        )
        # ticket_b는 다른 holder가 이미 활성 lease를 쥔 상태 → 그 claim은 Conflict다.
        other_holder = _lease_uow(completion, holder_id="dispatcher-other", clock=lambda: NOW)
        other_holder.claim(ticket_id=ticket_b)

        # ticket_a의 fresh claim(after_lease_insert)에서만 BUSY를 1회 주입한다 —
        # ticket_a가 정렬상 먼저 처리되므로(created_at 이르다) 정확히 그 claim만 겨냥한다.
        fired = {"count": 0}

        def raise_busy_once(point: str) -> None:
            if point == "after_lease_insert" and fired["count"] == 0:
                fired["count"] += 1
                error = sqlite3.OperationalError("simulated contention")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error

        lease_uow = _lease_uow(
            completion, holder_id="dispatcher-a", clock=lambda: NOW, fault_injector=raise_busy_once
        )
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.scanned == 2
        assert report.contended == 1
        assert report.skipped == 1
        assert report.claimed == 0
        assert ticket_a and ticket_b  # 참조 유지용
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 29(review-s53 P1-1·P2-7) — phase 0 후보 조회(`_candidates`)의 BUSY는
# `DurableDispatchRunBusy`로 승격된다(lease 계열이 아니다 — 아직 어떤 lease도
# 쥐지 않았다). `BEGIN EXCLUSIVE`는 reader까지 막으므로 TX-A(claim) 진입 전에
# 이미 BUSY를 만난다 — 무타입 sqlite3.OperationalError가 누수하면 ADR §9 ⑫를
# 어긴다. 아직 ticket 루프에 들어가지 않아 contended로 흡수할 대상이 없으므로
# run_once 밖으로 그대로 전파된다(스캔 자체가 재시도 가능함을 호출자에게
# 알린다) — 후보 열거 0·write 0.
# ---------------------------------------------------------------------------


def test_candidates_read의_busy는_run_busy로_승격돼_run_once_밖으로_전파된다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path, timeout=0.2)
    try:
        request_id = _ref("request", "rp1a")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-p1a")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        blocker = sqlite3.connect(str(path), timeout=1.0)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(DurableDispatchRunBusy) as excinfo:
                runner.run_once()
            assert not isinstance(excinfo.value, DurableDispatchLeaseError)
        finally:
            blocker.rollback()
            blocker.close()

        assert len(channel.calls) == 0  # 후보 열거 0
        assert _attempt_count(completion) == 0
        assert _lease_row(completion, ticket_id) is None  # TX-A 진입 전이라 write 0

        # lock 해제 뒤 재실행은 정상이다(영구 잠금이 아니다).
        second = runner.run_once()
        assert second.delivered == 1
        assert len(channel.calls) == 1
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# P1-1b(review-s53) — claim 이후 read(`_prepare`)의 BUSY는 그 ticket만
# contended로 흡수한다(lease는 leased 유지 — TX-B Busy·crash point ②와 같은
# 논거). `_candidates`(phase 0)와 달리 TX-A가 이미 성공했으므로 실제 lock
# 경합을 정확히 그 시점에 겨냥하려면 진짜 스레드 동기화가 필요하다 — claim이
# 끝나고 `_prepare`의 read가 시작되기 **직전**에만 EXCLUSIVE를 쥔다
# (S4.5b `paused`/`release` Event 패턴과 동형).
# ---------------------------------------------------------------------------


def test_prepare_read의_busy는_그_ticket만_contended로_흡수한다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path, timeout=2.0)
    try:
        request_id = _ref("request", "rp1b")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-p1b")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        claimed = threading.Event()
        locked = threading.Event()
        original_prepare = runner._prepare

        def paced_prepare(
            lease: DispatchLease,
        ) -> tuple[_AttemptRecord | None, str, DispatchFrame | None]:
            claimed.set()
            assert locked.wait(timeout=5)
            return original_prepare(lease)

        runner._prepare = paced_prepare  # type: ignore[method-assign]

        blocker_box: dict[str, sqlite3.Connection] = {}

        def hold_exclusive_lock() -> None:
            assert claimed.wait(timeout=5)
            blocker = sqlite3.connect(str(path), timeout=1.0, check_same_thread=False)
            blocker.execute("BEGIN EXCLUSIVE")
            blocker_box["conn"] = blocker
            locked.set()

        holder = threading.Thread(target=hold_exclusive_lock)
        holder.start()
        try:
            report = runner.run_once()
        finally:
            holder.join(timeout=5)
            blocker = blocker_box.get("conn")
            if blocker is not None:
                blocker.rollback()
                blocker.close()

        assert report.scanned == 1
        assert report.claimed == 1  # TX-A(claim)는 blocker 이전에 이미 성공했다
        assert report.contended == 1
        assert len(channel.calls) == 0  # deliver까지 가지 못했다
        assert _attempt_count(completion) == 0
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"
        assert lease_row["lease_epoch"] == 1
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 27(review-s53) — TX-B의 tz-naive clock(`_instant`)은 러너 자신의
# `DurableDispatchRunUnavailable`이고, 흡수되지 않고 run_once 밖으로 그대로
# 전파된다. lease_uow의 clock은 tz-aware로 둬 TX-A는 정상 성공시키고,
# `_record`가 처음으로 `self._clock()`(runner 자신의 clock)을 부르는
# 지점만 단독으로 겨냥한다.
# ---------------------------------------------------------------------------


def test_tx_b의_tz_naive_clock은_run_unavailable이고_흡수되지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "rp21")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-p21")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)  # TX-A는 tz-aware로 정상
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        naive_now = datetime(2026, 7, 25)  # tz-naive — runner 자신의 clock
        runner = _runner(
            completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: naive_now
        )

        with pytest.raises(DurableDispatchRunUnavailable) as excinfo:
            runner.run_once()
        assert not isinstance(excinfo.value, DurableDispatchLeaseError)

        assert len(channel.calls) == 1  # deliver는 이미 호출됐다 — TX-B에서만 실패했다
        assert _attempt_count(completion) == 0
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"  # renew/release 모두 실행되지 않았다
        assert lease_row["lease_epoch"] == 1
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# P2-3(review-s53) — `run_once(org_id=...)` 필터는 다른 org의 pending
# ticket을 스캔 자체에서 제외한다(write 0).
# ---------------------------------------------------------------------------


def test_run_once_org_id_필터는_다른_org_ticket을_건드리지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        org_a = _ref("org", "org-a-p23")
        org_b = _ref("org", "org-b-p23")
        ticket_a = _seed_dispatchable_ticket(
            completion, request_id=_ref("request", "ra-p23"), org_id=org_a, ticket_label="ticket-a-p23"
        )
        ticket_b = _seed_dispatchable_ticket(
            completion, request_id=_ref("request", "rb-p23"), org_id=org_b, ticket_label="ticket-b-p23"
        )
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once(org_id=org_a)

        assert report.scanned == 1
        assert report.claimed == 1
        assert len(channel.calls) == 1
        assert channel.calls[0].ticket_id == ticket_a

        attempts_a = _attempt_rows(completion, ticket_a)
        attempts_b = _attempt_rows(completion, ticket_b)
        assert len(attempts_a) == 1
        assert len(attempts_b) == 0
        assert _lease_row(completion, ticket_b) is None  # 다른 org ticket은 건드리지 않았다
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# P2-4(review-s53) — `status != 'pending'` ticket은 후보에서 제외된다
# (S5.4/S5.6이 status를 completed/escalated로 옮긴 뒤 이 필터가 실제로
# 발화한다 — 지금은 조기 회귀 고정).
# ---------------------------------------------------------------------------


def test_status가_pending이_아닌_ticket은_후보에서_제외된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "rp24")
        received = QuestionRequest.receive(
            org_id=ORG_ID,
            requester_id="user",
            question="q",
            request_id_factory=lambda: request_id,
            clock=lambda: NOW - timedelta(minutes=2),
            due_at=NOW + timedelta(hours=1),
        )
        completion.create(received)
        trigger_ref = _ref("trigger", request_id)
        ready = received.record_initial_routing(
            intent="refund",
            disposition="routed",
            target=ReadyToDispatch(
                route=RouteTarget(intent="refund", agent_id="card-a", requires_approval=False),
                attempt=1,
                trigger_key=trigger_ref,
                handling=HandlingAssignment(
                    kind="system", ref=trigger_ref, due_at=NOW + timedelta(hours=1)
                ),
            ),
            clock=lambda: NOW - timedelta(minutes=1),
        )
        assert completion.compare_and_set(request_id, 0, received, ready)

        ticket_id = _ref("ticket", "manual-p24")
        _insert_ticket_row(
            completion,
            ticket_id=ticket_id,
            org_id=ORG_ID,
            request_id=request_id,
            attempt=1,
            awaiting_revision=0,
            status="completed",
        )

        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.scanned == 0
        assert report.claimed == 0
        assert len(channel.calls) == 0
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# P2-5(review-s53) — TX-B는 attempt.created_at과 lease 갱신의 `now`로 같은
# 시각 **한 번만** 재야 한다(§9 ⑬이 `*_in_transaction` seam을 허용한 유일한
# 근거). `self._clock()`을 호출마다 다른 값으로 만들어, `_record`가 두 번
# 부르면 `expires_at != created_at + ttl`로 어긋나게 한다.
# ---------------------------------------------------------------------------


def test_tx_b는_created_at과_lease_갱신에_같은_시각_한_번만_쓴다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "rp25")
        ticket_id = _seed_dispatchable_ticket(completion, request_id=request_id, ticket_label="ticket-p25")
        lease_uow = _lease_uow(completion, clock=lambda: NOW)  # TX-A는 고정 시각
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()

        counter = {"n": 0}

        def incrementing_clock() -> datetime:
            value = NOW + timedelta(seconds=counter["n"])
            counter["n"] += 1
            return value

        runner = _runner(
            completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=incrementing_clock
        )

        report = runner.run_once()

        assert report.delivered == 1
        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 1
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        created_at = datetime.fromisoformat(attempts[0]["created_at"])
        expires_at = datetime.fromisoformat(lease_row["expires_at"])
        assert expires_at == created_at + _TTL
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 23(review-s53 §9 ⑭) — 전송(외부) 도중 lease TTL이 초과돼 다른
# 인스턴스가 먼저 reclaim하면 TX-B의 renew CAS가 stale로 실패한다(§9 ⑭
# "TTL 초과+타 인스턴스 탈취"). run_once는 죽지 않고 정상 반환하며,
# `preempted==1`·시도 행 0·ticket은 pending 그대로 — **남은 ticket은
# 이어서 처리된다**(채널 호출 2/2).
# ---------------------------------------------------------------------------


def test_전송중_ttl_초과_타인스턴스_reclaim은_preempted로_흡수되고_나머지는_처리된다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_a = _seed_dispatchable_ticket(
            completion, request_id=_ref("request", "r23a"), ticket_label="ticket-23a",
            enqueue_clock=lambda: NOW,
        )
        ticket_b = _seed_dispatchable_ticket(
            completion, request_id=_ref("request", "r23b"), ticket_label="ticket-23b",
            enqueue_clock=lambda: NOW + timedelta(minutes=1),
        )

        later = NOW + timedelta(minutes=10)  # ttl(5분) 경과 시점
        channel = _HijackingChannel(
            completion, hijack_ticket_id=ticket_a, later_clock=lambda: later, outcome=Delivered()
        )
        directory = _owner_directory()
        lease_uow = _lease_uow(completion, holder_id="dispatcher-a", clock=lambda: NOW)
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.scanned == 2
        assert report.claimed == 2  # ticket_a의 claim(TX-A) 자체는 성공했다
        assert report.preempted == 1
        assert report.delivered == 1  # ticket_b만 기록까지 성공했다
        assert len(channel.calls) == 2  # 둘 다 채널까지는 갔다

        attempts_a = _attempt_rows(completion, ticket_a)
        assert len(attempts_a) == 0  # 시도 행 없음(TX-B 전체 롤백)
        ticket_a_row = _ticket_row(completion, ticket_a)
        assert ticket_a_row["status"] == "pending"
        lease_a = _lease_row(completion, ticket_a)
        assert lease_a is not None
        assert lease_a["lease_epoch"] == 2  # thief가 가져갔다
        assert lease_a["holder_ref"] == _ref("subject", "dispatcher-thief")

        attempts_b = _attempt_rows(completion, ticket_b)
        assert len(attempts_b) == 1
        assert attempts_b[0]["outcome"] == "delivered"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 24(review-s53) — `preempted`는 `skipped`·`contended`와 섞이지 않는다
# (한 run 안에서 preempted 1건과 skipped 1건을 동시에 만들어 서로 독립임을
# 확인한다).
# ---------------------------------------------------------------------------


def test_preempted는_skipped_contended와_섞이지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_preempt = _seed_dispatchable_ticket(
            completion, request_id=_ref("request", "r24p"), ticket_label="ticket-24-preempt",
            enqueue_clock=lambda: NOW,
        )
        ticket_skip = _seed_dispatchable_ticket(
            completion, request_id=_ref("request", "r24s"), ticket_label="ticket-24-skip",
            enqueue_clock=lambda: NOW + timedelta(minutes=1),
        )
        # ticket_skip은 다른 holder가 이미 활성 lease를 쥔 상태 → TX-A Conflict.
        other_holder = _lease_uow(completion, holder_id="dispatcher-other", clock=lambda: NOW)
        other_holder.claim(ticket_id=ticket_skip)

        later = NOW + timedelta(minutes=10)
        channel = _HijackingChannel(
            completion, hijack_ticket_id=ticket_preempt, later_clock=lambda: later, outcome=Delivered()
        )
        directory = _owner_directory()
        lease_uow = _lease_uow(completion, holder_id="dispatcher-a", clock=lambda: NOW)
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.scanned == 2
        assert report.claimed == 1  # ticket_skip의 claim은 Conflict라 claimed에 안 들어간다
        assert report.skipped == 1
        assert report.preempted == 1
        assert report.contended == 0
        assert report.delivered == 0
        assert report.undeliverable == 0
        assert len(channel.calls) == 1  # skip된 ticket은 채널까지 가지 않는다
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 25(review-s53) — undeliverable 경로(release CAS)의 TTL 탈취도
# preempted로 흡수된다(renew·release 두 CAS가 대칭으로 보호된다).
# ---------------------------------------------------------------------------


def test_undeliverable_경로의_release_ttl_탈취도_preempted로_흡수된다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(
            completion, request_id=_ref("request", "r25"), ticket_label="ticket-25"
        )
        later = NOW + timedelta(minutes=10)
        channel = _HijackingChannel(
            completion,
            hijack_ticket_id=ticket_id,
            later_clock=lambda: later,
            outcome=Undeliverable(reason_code="no_connected_worker"),
        )
        directory = _owner_directory()
        lease_uow = _lease_uow(completion, holder_id="dispatcher-a", clock=lambda: NOW)
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        report = runner.run_once()

        assert report.claimed == 1
        assert report.preempted == 1
        assert report.delivered == 0
        assert report.undeliverable == 0  # 시도 행 자체가 없다
        assert len(channel.calls) == 1

        attempts = _attempt_rows(completion, ticket_id)
        assert len(attempts) == 0
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["lease_epoch"] == 2
        assert lease_row["holder_ref"] == _ref("subject", "dispatcher-thief")
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 28(review-s53) — claim 뒤 ticket이 사라진 상태는 정상 API로 도달할 수
# 없다(S4.1 FK RESTRICT) — `_prepare` 자신의 방어(불변식 위반 fail-closed)를
# 직접 겨냥한다. ticket_id가 애초에 존재한 적 없는 위조 lease로 같은 분기를
# 겨냥해 러너 자신의 DurableDispatchRunUnavailable을 확인한다.
# ---------------------------------------------------------------------------


def test_claim_이후_ticket_소실은_run_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        lease_uow = _lease_uow(completion, clock=lambda: NOW)
        channel = _FixedChannel(Delivered())
        directory = _owner_directory()
        runner = _runner(completion, lease_uow=lease_uow, channel=channel, directory=directory, clock=lambda: NOW)

        ghost_ticket_id = _ref("ticket", "ghost-p28")
        ghost_lease = DispatchLease(
            ticket_id=ghost_ticket_id,
            org_id=ORG_ID,
            request_id=_ref("request", "ghost-request-p28"),
            lease_epoch=1,
            holder_ref=_ref("subject", "dispatcher-a"),
            expires_at=NOW + _TTL,
        )

        with pytest.raises(DurableDispatchRunUnavailable) as excinfo:
            runner._prepare(ghost_lease)
        assert not isinstance(excinfo.value, DurableDispatchLeaseError)
    finally:
        completion.close()
