"""P17.9 S5.2 durable dispatch lease UoW 테스트(ADR 0042 §9 ④⑤).

claim(fresh+reclaim 통합)·renew·release의 full-row CAS와, 호출자가 만료를
주장할 수 없다는 API 계약(`now`·`lease_epoch` 인자 부재)·token 부재를
검증한다. `renew_in_transaction`/`release_in_transaction`은 S5.3이 소비할
좁은 seam이라 이 파일에서 직접 검증한다(begin/commit 미소유·`tx.in_transaction`
False면 Unavailable).
"""

from __future__ import annotations
# pyright: reportArgumentType=false

import hashlib
import inspect
import sqlite3
from collections.abc import Callable
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
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
    DurableDispatchLeaseBusy,
    DurableDispatchLeaseConflict,
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
REQUEST_ID = _ref("request", "request-1")
_TICKET_OWNER = _ref("subject", "owner-a")


class _Registry:
    """owner 주소 해석 Fake — 고정 owner 하나만 지원한다(S4.5 seed 전용)."""

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
    request_id: str = REQUEST_ID,
    org_id: str = ORG_ID,
    agent_id: str = "card-a",
    attempt: int = 1,
    ticket_label: str = "ticket-1",
    owner: str | None = _TICKET_OWNER,
) -> str:
    """ReadyToDispatch → S4.5 enqueue로 claim 가능한 pending WorkTicket을 만든다."""
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
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=RouteTarget(intent="refund", agent_id=agent_id, requires_approval=False),
            attempt=attempt,
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
        clock=lambda: NOW,
        ticket_id_factory=lambda: ticket_label,
        receipt_id_factory=lambda: f"receipt-{ticket_label}",
    )
    enqueued = enqueue_uow.enqueue(
        command=DurableWorkTicketEnqueueCommand(request_id, 1, attempt)
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


def _lease_row(completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str) -> sqlite3.Row | None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        row = tx.execute(
            "SELECT * FROM durable_dispatch_leases WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        tx.commit()
        return row


def _lease_count(completion: SqliteQuestionCompletionUnitOfWork) -> int:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        count = tx.execute("SELECT count(*) FROM durable_dispatch_leases").fetchone()[0]
        tx.commit()
        return count


def _insert_ticket_row(
    tx: SqliteCompletionTransaction,
    *,
    ticket_id: str,
    org_id: str,
    request_id: str,
    attempt: int,
    awaiting_revision: int,
    owner: str = _TICKET_OWNER,
    status: str = "pending",
) -> None:
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


# ---------------------------------------------------------------------------
# 추가 — 생성자 검증(mutation probe에서 발견한 gap): holder_id 형식·capability
# 부재는 각각 독립적으로 Unavailable을 내야 한다(ttl 경계는 red 11에서 검증).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("holder_id", ["", "   "])
def test_holder_id가_공백이면_unavailable이다(tmp_path: Path, holder_id: str) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        with pytest.raises(DurableDispatchLeaseUnavailable):
            _lease_uow(completion, holder_id=holder_id)
    finally:
        completion.close()


def test_dispatch_delivery_schema_미설치_db는_생성_시점에_unavailable이다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    # dispatch delivery(S5.1) migration을 의도적으로 건너뛴다.
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(DurableDispatchLeaseUnavailable):
            _lease_uow(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 1 — 첫 claim: epoch1·leased·expires=now+ttl·holder_ref=subject:<sha>
# ---------------------------------------------------------------------------


def test_첫_claim은_epoch1_leased_lease를_생성한다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, holder_id="dispatcher-a").claim(ticket_id=ticket_id)
        assert lease.ticket_id == ticket_id
        assert lease.org_id == ORG_ID
        assert lease.request_id == REQUEST_ID
        assert lease.lease_epoch == 1
        assert lease.holder_ref == _ref("subject", "dispatcher-a")
        assert lease.expires_at == NOW + _TTL
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["state"] == "leased"
        assert row["lease_epoch"] == 1
        assert row["holder_ref"] == lease.holder_ref
        assert row["expires_at"] == _canonical(NOW + _TTL)
        assert row["acquired_at"] == _canonical(NOW)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 2 — 활성 lease 재claim(다른 holder) → Conflict·행 무변경
# ---------------------------------------------------------------------------


def test_활성_lease_재claim은_conflict이고_행이_불변이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        _lease_uow(completion, holder_id="a").claim(ticket_id=ticket_id)
        before = _lease_row(completion, ticket_id)
        assert before is not None
        with pytest.raises(DurableDispatchLeaseConflict):
            _lease_uow(completion, holder_id="b").claim(ticket_id=ticket_id)
        after = _lease_row(completion, ticket_id)
        assert after is not None
        assert dict(after) == dict(before)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 3 — 만료 뒤 재claim → epoch2·holder 교체·acquired_at 갱신
# ---------------------------------------------------------------------------


def test_만료_뒤_재claim은_epoch2이고_holder가_교체된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        first = _lease_uow(completion, holder_id="a", clock=lambda: NOW).claim(ticket_id=ticket_id)
        later = NOW + timedelta(minutes=10)  # ttl(5분) 경과
        second = _lease_uow(completion, holder_id="b", clock=lambda: later).claim(
            ticket_id=ticket_id
        )
        assert second.lease_epoch == first.lease_epoch + 1 == 2
        assert second.holder_ref == _ref("subject", "b")
        assert second.expires_at == later + _TTL
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["acquired_at"] == _canonical(later)
        assert row["holder_ref"] == second.holder_ref
    finally:
        completion.close()


def test_만료시각_정각은_active가_아니고_reclaim된다(tmp_path: Path) -> None:
    # active 판정은 `expires_at > now`(strict)다 — `>=`로 완화되면 정확히
    # 경계 시각의 reclaim이 부당하게 거부된다(review-s52 P2-3 교정).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        _lease_uow(completion, holder_id="a", clock=lambda: NOW).claim(ticket_id=ticket_id)
        boundary = NOW + _TTL  # 정확히 expires_at
        second = _lease_uow(completion, holder_id="b", clock=lambda: boundary).claim(
            ticket_id=ticket_id
        )
        assert second.lease_epoch == 2
        assert second.holder_ref == _ref("subject", "b")
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 4 — release 뒤 즉시 재claim → epoch2
# ---------------------------------------------------------------------------


def test_release_뒤_즉시_재claim은_epoch2이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow_a = _lease_uow(completion, holder_id="a")
        first = uow_a.claim(ticket_id=ticket_id)
        uow_a.release(lease=first)
        second = _lease_uow(completion, holder_id="b").claim(ticket_id=ticket_id)
        assert second.lease_epoch == 2
        assert second.holder_ref == _ref("subject", "b")
    finally:
        completion.close()


def test_released_상태의_lease는_expires_at이_미래여도_active로_취급하지_않는다(
    tmp_path: Path,
) -> None:
    # "active" 판정은 state=='leased' AND expires_at>now 둘 다 필요하다(§9 ④).
    # `_release_cas`가 release 시점에 expires_at을 현재 now로 덮어써서, 정상
    # release→재claim 경로만으로는 state 조건이 가려진다 — release를 거치지
    # 않고 lease 행을 직접 `released`·미래 expires_at으로 시딩해 state 조건
    # 자체를 단독으로 겨냥한다.
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "INSERT INTO durable_dispatch_leases VALUES(?,?,?,?,?,?,?,?)",
                (
                    ticket_id,
                    ORG_ID,
                    REQUEST_ID,
                    1,
                    _ref("subject", "a"),
                    "released",
                    _canonical(NOW + timedelta(days=1)),  # 먼 미래 — expires_at만으로는 active
                    _canonical(NOW - timedelta(minutes=1)),
                ),
            )
            tx.commit()
        second = _lease_uow(completion, holder_id="b", clock=lambda: NOW).claim(
            ticket_id=ticket_id
        )
        assert second.lease_epoch == 2
        assert second.holder_ref == _ref("subject", "b")
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 5 — renew: epoch 불변·expires만 연장
# ---------------------------------------------------------------------------


def test_renew는_epoch_불변이고_expires만_연장한다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, holder_id="a", clock=lambda: NOW).claim(ticket_id=ticket_id)
        later = NOW + timedelta(minutes=1)
        renewed = _lease_uow(completion, holder_id="a", clock=lambda: later).renew(lease=lease)
        assert renewed.lease_epoch == lease.lease_epoch
        assert renewed.ticket_id == lease.ticket_id
        assert renewed.holder_ref == lease.holder_ref
        assert renewed.expires_at == later + _TTL
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["lease_epoch"] == lease.lease_epoch
        assert row["expires_at"] == _canonical(later + _TTL)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 6 — 다른 holder의 renew/release → Conflict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["renew", "release"])
def test_다른_holder의_renew_release는_conflict이다(tmp_path: Path, operation: str) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, holder_id="a").claim(ticket_id=ticket_id)
        other = _lease_uow(completion, holder_id="b")
        with pytest.raises(DurableDispatchLeaseConflict):
            if operation == "renew":
                other.renew(lease=lease)
            else:
                other.release(lease=lease)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["state"] == "leased"
        assert row["holder_ref"] == lease.holder_ref
        assert row["lease_epoch"] == lease.lease_epoch
    finally:
        completion.close()


def test_renew은_holder_ref를_위조된_lease_인자가_아니라_자기_identity로_돌려준다(
    tmp_path: Path,
) -> None:
    # CAS는 self._holder_ref로 대조하므로, holder_ref만 위조된 lease로도
    # (ticket_id/epoch가 실제 행과 맞으면) CAS 자체는 통과한다 — 이때 반환값의
    # holder_ref가 위조된 lease.holder_ref를 그대로 echo하면 안 된다
    # (review-s52 mutation M40 교정).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        real = uow.claim(ticket_id=ticket_id)
        forged = DispatchLease(
            ticket_id=real.ticket_id,
            org_id=real.org_id,
            request_id=real.request_id,
            lease_epoch=real.lease_epoch,
            holder_ref=_ref("subject", "somebody-else"),
            expires_at=real.expires_at,
        )
        renewed = uow.renew(lease=forged)
        assert renewed.holder_ref == real.holder_ref
        assert renewed.holder_ref == _ref("subject", "a")
    finally:
        completion.close()


@pytest.mark.parametrize("operation", ["renew", "release"])
@pytest.mark.parametrize("forged_field", ["org_id", "request_id", "both"])
def test_위조된_org_또는_request_id로는_renew_release가_conflict이다(
    tmp_path: Path, operation: str, forged_field: str
) -> None:
    # org_id/request_id도 CAS의 WHERE에 있으므로, 이 필드가 위조되면 실제
    # 행과 매치되지 않아 CAS 자체가 stale로 실패해야 한다 — 위조값이 반환에
    # echo되는 경로(그 값이 향후 S5.3 attempts INSERT로 흘러들어 S5.1 lineage
    # 검증을 깨는 사슬)를 원천 차단한다(review-s52 P2-2 교정). org_id·
    # request_id를 함께 위조하면 한쪽 조건만 남아도 CAS가 막히므로, 두
    # 필드를 각각 단독으로도 위조해 서로 가리지 않는지 확인한다
    # (review-s52 후속 red 21: "틀린 org_id" 단독 격리).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        real = uow.claim(ticket_id=ticket_id)
        forged_org = _ref("org", "other-org") if forged_field in ("org_id", "both") else real.org_id
        forged_request = (
            _ref("request", "other-request")
            if forged_field in ("request_id", "both")
            else real.request_id
        )
        forged = DispatchLease(
            ticket_id=real.ticket_id,
            org_id=forged_org,
            request_id=forged_request,
            lease_epoch=real.lease_epoch,
            holder_ref=real.holder_ref,
            expires_at=real.expires_at,
        )
        with pytest.raises(DurableDispatchLeaseConflict):
            if operation == "renew":
                uow.renew(lease=forged)
            else:
                uow.release(lease=forged)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["state"] == "leased"
        assert row["org_id"] == ORG_ID
        assert row["request_id"] == REQUEST_ID
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 7 — stale epoch renew/release → Conflict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["renew", "release"])
def test_stale_epoch의_renew_release는_conflict이다(tmp_path: Path, operation: str) -> None:
    # 같은 holder("a")가 자기 만료 lease를 epoch2로 reclaim한 뒤, epoch1짜리
    # in-flight DispatchLease로 renew/release를 시도한다 — holder_ref는 양쪽
    # 다 "a"로 동일하므로, 이 red는 오직 lease_epoch 조건만으로 죽어야 한다
    # (다른 holder를 섞으면 holder_ref 조건이 먼저 막아 epoch 조건이 가려진다
    # — review-s52 P1-2 교정).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow_a = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        stale = uow_a.claim(ticket_id=ticket_id)
        later_uow = _lease_uow(
            completion, holder_id="a", clock=lambda: NOW + timedelta(minutes=10)
        )
        current = later_uow.claim(ticket_id=ticket_id)  # 자기 만료 lease를 epoch2로 reclaim
        assert current.lease_epoch == 2
        assert current.holder_ref == stale.holder_ref
        with pytest.raises(DurableDispatchLeaseConflict):
            if operation == "renew":
                later_uow.renew(lease=stale)
            else:
                later_uow.release(lease=stale)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["lease_epoch"] == current.lease_epoch
        assert row["holder_ref"] == current.holder_ref
        assert row["state"] == "leased"
    finally:
        completion.close()


def test_released_상태의_lease는_renew되지_않는다(tmp_path: Path) -> None:
    # renew CAS 자체의 state='leased' 조건을 겨냥한다(claim의 active 판정과는
    # 다른 코드 경로) — release는 expires_at을 now로 덮어써서 정상 경로만으론
    # 가려지므로, release를 거치지 않고 released+미래 expires_at을 직접
    # 시딩해 state 조건을 단독으로 격리한다(review-s52 P1-2 교정).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "INSERT INTO durable_dispatch_leases VALUES(?,?,?,?,?,?,?,?)",
                (
                    ticket_id,
                    ORG_ID,
                    REQUEST_ID,
                    1,
                    _ref("subject", "a"),
                    "released",
                    _canonical(NOW + timedelta(days=1)),
                    _canonical(NOW - timedelta(minutes=1)),
                ),
            )
            tx.commit()
        lease = DispatchLease(
            ticket_id=ticket_id,
            org_id=ORG_ID,
            request_id=REQUEST_ID,
            lease_epoch=1,
            holder_ref=_ref("subject", "a"),
            expires_at=NOW + timedelta(days=1),
        )
        with pytest.raises(DurableDispatchLeaseConflict):
            _lease_uow(completion, holder_id="a", clock=lambda: NOW).renew(lease=lease)
    finally:
        completion.close()


def test_이중_release는_conflict이다(tmp_path: Path) -> None:
    # release CAS 자체의 state='leased' 조건을 겨냥한다 — 첫 release가 이미
    # state를 'released'로 내려서, 두 번째 release는 오직 이 조건만으로
    # 죽어야 한다(review-s52 P1-2 교정).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)
        uow.release(lease=lease)
        with pytest.raises(DurableDispatchLeaseConflict):
            uow.release(lease=lease)
    finally:
        completion.close()


@pytest.mark.parametrize("operation", ["renew", "release"])
def test_typed_dispatchlease가_아니면_renew_release는_unavailable이다(
    tmp_path: Path, operation: str
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion)
        uow.claim(ticket_id=ticket_id)
        untyped = object()
        with pytest.raises(DurableDispatchLeaseUnavailable):
            if operation == "renew":
                uow.renew(lease=untyped)  # type: ignore[arg-type]
            else:
                uow.release(lease=untyped)  # type: ignore[arg-type]
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 8 — 만료 lease renew → Conflict
# ---------------------------------------------------------------------------


def test_만료된_lease의_renew는_conflict이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, holder_id="a", clock=lambda: NOW).claim(ticket_id=ticket_id)
        later = NOW + timedelta(minutes=10)  # ttl(5분) 경과
        with pytest.raises(DurableDispatchLeaseConflict):
            _lease_uow(completion, holder_id="a", clock=lambda: later).renew(lease=lease)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 9 — status != 'pending' claim → Conflict·write 0
# ---------------------------------------------------------------------------


def test_status가_pending이_아닌_ticket의_claim은_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_work_tickets SET status='completed' WHERE ticket_id=?",
                (ticket_id,),
            )
            tx.commit()
        with pytest.raises(DurableDispatchLeaseConflict):
            _lease_uow(completion).claim(ticket_id=ticket_id)
        assert _lease_count(completion) == 0
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 10 — Request AwaitingAnswer 아님·ticket_id 불일치·revision 불일치
#          → 각 Conflict·write 0
# ---------------------------------------------------------------------------


def test_request가_awaitinganswer가_아니면_claim은_conflict이고_write0이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        # ReadyToDispatch 상태만 만들고, WorkTicket 행은 enqueue UoW를 거치지
        # 않고 직접 삽입한다 — Request 전이가 없는 상태 불일치 시나리오다.
        received = QuestionRequest.receive(
            org_id=ORG_ID,
            requester_id="user",
            question="q",
            request_id_factory=lambda: REQUEST_ID,
            clock=lambda: NOW - timedelta(minutes=2),
            due_at=NOW + timedelta(hours=1),
        )
        completion.create(received)
        trigger_ref = _ref("trigger", REQUEST_ID)
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
        assert completion.compare_and_set(REQUEST_ID, 0, received, ready)

        # awaiting_revision을 request.revision(1)-1로 맞춰 revision 검사(red 10c)와
        # 우연히 겹쳐 isinstance 검사(이 red)를 가리지 않게 한다 — 그래야 이
        # 테스트가 오직 "AwaitingAnswer가 아니다"만으로 Conflict를 낸다.
        ticket_id = _ref("ticket", "manual-1")
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            _insert_ticket_row(
                tx,
                ticket_id=ticket_id,
                org_id=ORG_ID,
                request_id=REQUEST_ID,
                attempt=1,
                awaiting_revision=0,
            )
            tx.commit()

        with pytest.raises(DurableDispatchLeaseConflict):
            _lease_uow(completion).claim(ticket_id=ticket_id)
        assert _lease_count(completion) == 0
    finally:
        completion.close()


def test_request_ticket_id_불일치는_claim_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        # 정상 seed로 Request.state.ticket_id는 real_ticket_id를 가리킨다. 같은
        # request_id·같은 awaiting_revision을 갖는 두 번째(attempt=2) WorkTicket
        # 행을 수동으로 만들어, 그 ticket_id로 claim을 시도하면 오직 ticket_id
        # 불일치만으로 Conflict가 나야 한다(다른 조건은 모두 정합).
        real_ticket_id = _seed_dispatchable_ticket(completion, ticket_label="ticket-real")
        other_ticket_id = _ref("ticket", "ticket-other")
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            _insert_ticket_row(
                tx,
                ticket_id=other_ticket_id,
                org_id=ORG_ID,
                request_id=REQUEST_ID,
                attempt=2,
                awaiting_revision=1,
            )
            tx.commit()
        assert real_ticket_id != other_ticket_id
        with pytest.raises(DurableDispatchLeaseConflict):
            _lease_uow(completion).claim(ticket_id=other_ticket_id)
        assert _lease_count(completion) == 0
    finally:
        completion.close()


def test_revision_불일치는_claim_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_work_tickets SET awaiting_revision=99 WHERE ticket_id=?",
                (ticket_id,),
            )
            tx.commit()
        with pytest.raises(DurableDispatchLeaseConflict):
            _lease_uow(completion).claim(ticket_id=ticket_id)
        assert _lease_count(completion) == 0
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 11 — ttl 경계(0·1일+1초 → Unavailable, 정확히 1일 허용)
# ---------------------------------------------------------------------------


def test_ttl_0은_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        with pytest.raises(DurableDispatchLeaseUnavailable):
            _lease_uow(completion, ttl=timedelta(0))
    finally:
        completion.close()


def test_ttl_1일_초과는_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        with pytest.raises(DurableDispatchLeaseUnavailable):
            _lease_uow(completion, ttl=timedelta(days=1, seconds=1))
    finally:
        completion.close()


def test_ttl_정확히_1일은_허용된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, ttl=timedelta(days=1)).claim(ticket_id=ticket_id)
        assert lease.expires_at == NOW + timedelta(days=1)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 12 — tz-naive clock → Unavailable(claim·renew·release 전부)
# ---------------------------------------------------------------------------


def test_tz_naive_clock은_claim에서_unavailable이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        naive_now = datetime(2026, 7, 25)
        with pytest.raises(DurableDispatchLeaseUnavailable):
            _lease_uow(completion, clock=lambda: naive_now).claim(ticket_id=ticket_id)
        assert _lease_count(completion) == 0
    finally:
        completion.close()


def test_tz_naive_clock은_renew_release에서도_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, clock=lambda: NOW).claim(ticket_id=ticket_id)
        naive_now = datetime(2026, 7, 25)
        with pytest.raises(DurableDispatchLeaseUnavailable):
            _lease_uow(completion, clock=lambda: naive_now).renew(lease=lease)
        with pytest.raises(DurableDispatchLeaseUnavailable):
            _lease_uow(completion, clock=lambda: naive_now).release(lease=lease)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["state"] == "leased"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 13 — 구조적으로 now·lease_epoch 인자가 없다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method_name", ["claim", "renew", "release"])
def test_claim_renew_release는_now_lease_epoch_인자가_없다(method_name: str) -> None:
    params = inspect.signature(getattr(DurableDispatchLeaseUnitOfWork, method_name)).parameters
    assert "now" not in params
    assert "lease_epoch" not in params


# ---------------------------------------------------------------------------
# red 14 — 구조적으로 token이 없다
# ---------------------------------------------------------------------------


def test_구조적으로_token이_없다() -> None:
    init_params = inspect.signature(DurableDispatchLeaseUnitOfWork.__init__).parameters
    assert "token_key" not in init_params
    field_names = {field.name for field in fields(DispatchLease)}
    assert not any("token" in name for name in field_names)


# ---------------------------------------------------------------------------
# red 15 — fault 4지점: 각 부분 쓰기 0(직전 상태로 롤백)
# ---------------------------------------------------------------------------


def test_after_lease_insert_fault는_lease_삽입을_롤백한다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)

        def raise_at(point: str) -> None:
            if point == "after_lease_insert":
                raise RuntimeError(point)

        uow = _lease_uow(completion, fault_injector=raise_at)
        with pytest.raises(RuntimeError, match="after_lease_insert"):
            uow.claim(ticket_id=ticket_id)
        assert _lease_count(completion) == 0
    finally:
        completion.close()


def test_after_lease_cas_fault는_reclaim을_롤백한다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        first = _lease_uow(completion, holder_id="a", clock=lambda: NOW).claim(ticket_id=ticket_id)
        later = NOW + timedelta(minutes=10)

        def raise_at(point: str) -> None:
            if point == "after_lease_cas":
                raise RuntimeError(point)

        uow = _lease_uow(completion, holder_id="b", clock=lambda: later, fault_injector=raise_at)
        with pytest.raises(RuntimeError, match="after_lease_cas"):
            uow.claim(ticket_id=ticket_id)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["lease_epoch"] == first.lease_epoch
        assert row["holder_ref"] == first.holder_ref
        assert row["state"] == "leased"
    finally:
        completion.close()


def test_after_renew_cas_fault는_renew를_롤백한다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, clock=lambda: NOW).claim(ticket_id=ticket_id)

        def raise_at(point: str) -> None:
            if point == "after_renew_cas":
                raise RuntimeError(point)

        later = NOW + timedelta(minutes=1)
        uow = _lease_uow(completion, clock=lambda: later, fault_injector=raise_at)
        with pytest.raises(RuntimeError, match="after_renew_cas"):
            uow.renew(lease=lease)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["expires_at"] == _canonical(lease.expires_at)
    finally:
        completion.close()


def test_after_release_cas_fault는_release를_롤백한다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, clock=lambda: NOW).claim(ticket_id=ticket_id)

        def raise_at(point: str) -> None:
            if point == "after_release_cas":
                raise RuntimeError(point)

        uow = _lease_uow(completion, clock=lambda: NOW, fault_injector=raise_at)
        with pytest.raises(RuntimeError, match="after_release_cas"):
            uow.release(lease=lease)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["state"] == "leased"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 16 — capability 손상 뒤 claim → Unavailable(행 무변경)
# ---------------------------------------------------------------------------


def test_capability_손상_뒤_uow_생성은_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        # claim은 org_id 없이 전역 검증한다 — 무관한 다른 request/ticket의
        # lease 행 하나를 위조해도 새 UoW 생성이 fail-closed해야 한다.
        other_request_id = _ref("request", "request-2")
        other_ticket_id = _seed_dispatchable_ticket(
            completion, request_id=other_request_id, ticket_label="ticket-2"
        )
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "INSERT INTO durable_dispatch_leases VALUES(?,?,?,?,?,?,?,?)",
                (
                    other_ticket_id,
                    ORG_ID,
                    other_request_id,
                    1,
                    _ref("subject", "someone"),
                    "not-a-valid-state",
                    "2026-01-01T00:05:00.000000+00:00",
                    "2026-01-01T00:00:00.000000+00:00",
                ),
            )
            tx.commit()
        with pytest.raises(DurableDispatchLeaseUnavailable):
            _lease_uow(completion)  # 생성자의 open-time validate가 이미 여기서 막는다
        assert _lease_count(completion) == 1  # 위조된 행 하나만 — 대상 ticket은 write0
    finally:
        completion.close()


def test_uow_생성_후_손상되면_claim의_1차_validate가_unavailable로_막는다(
    tmp_path: Path,
) -> None:
    # 위 red와 달리 UoW를 capable 상태에서 먼저 만들고, 그 뒤에 손상시킨다 —
    # 그래야 생성자 validate가 아니라 claim() 자신의 1차 validate(clock 호출
    # 전, begin_immediate 직후)가 실제로 겨냥된다(review-s52 P1-3 교정).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion)  # capable 상태에서 먼저 생성
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_work_tickets SET org_id=? WHERE ticket_id=?",
                (_ref("org", "org-2"), ticket_id),
            )
            tx.commit()
        with pytest.raises(DurableDispatchLeaseUnavailable):
            uow.claim(ticket_id=ticket_id)
        assert _lease_count(completion) == 0
    finally:
        completion.close()


def test_clock_호출_중_다른_ticket_lease를_오염시키면_2차_validate가_잡는다(
    tmp_path: Path,
) -> None:
    # claim()의 2차 validate(clock 호출 뒤·commit 전)를 단독으로 겨냥한다.
    # 대상 ticket 자체를 건드리면 reclaim이 그 행을 덮어써 스스로 치유되므로,
    # clock 콜백 안에서 무관한 다른 ticket의 delivery attempt를 위조해
    # outcome/reason_code 상관검사를 깬다(review-s52 P1-3 교정).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        tx = completion.durable_transaction()

        def corrupting_clock() -> datetime:
            tx.execute(
                "INSERT INTO durable_dispatch_delivery_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    _ref("receipt", "a1"),
                    ORG_ID,
                    ticket_id,
                    REQUEST_ID,
                    99,
                    _ref("subject", "x"),
                    "delivered",
                    "channel_error",  # (outcome==delivered) != (reason==ok) 상관검사 위반
                    _TICKET_OWNER,
                    _canonical(NOW),
                ),
            )
            return NOW

        uow = _lease_uow(completion, clock=corrupting_clock)
        with pytest.raises(DurableDispatchLeaseUnavailable):
            uow.claim(ticket_id=ticket_id)
        assert _lease_row(completion, ticket_id) is None  # 오염·claim 시도 모두 롤백
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# 추가 — renew_in_transaction/release_in_transaction seam(팀리드 명시 요구):
# begin/commit 미소유·tx.in_transaction False면 Unavailable(S5.3이 소비).
# ---------------------------------------------------------------------------


def test_renew_in_transaction은_scope_밖에서_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)
        with pytest.raises(DurableDispatchLeaseUnavailable):
            uow.renew_in_transaction(lease=lease, now=NOW)
    finally:
        completion.close()


def test_renew_in_transaction은_scope_안이지만_begin_전에는_unavailable이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)
        tx = completion.durable_transaction()
        with tx.scope():
            with pytest.raises(DurableDispatchLeaseUnavailable):
                uow.renew_in_transaction(lease=lease, now=NOW)
    finally:
        completion.close()


def test_release_in_transaction은_scope_밖에서_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)
        with pytest.raises(DurableDispatchLeaseUnavailable):
            uow.release_in_transaction(lease=lease, now=NOW)
    finally:
        completion.close()


def test_renew_in_transaction은_caller가_소유한_transaction을_공유하고_begin_commit을_소유하지_않는다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)

        tx = completion.durable_transaction()
        later = NOW + timedelta(minutes=1)
        with tx.scope():
            tx.begin_immediate()
            renewed = uow.renew_in_transaction(lease=lease, now=later)
            assert tx.in_transaction  # caller가 여전히 commit을 소유한다
            tx.commit()
        assert renewed.expires_at == later + _TTL
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["expires_at"] == _canonical(later + _TTL)
        assert row["lease_epoch"] == lease.lease_epoch
    finally:
        completion.close()


def test_release_in_transaction은_caller가_소유한_transaction을_공유하고_begin_commit을_소유하지_않는다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            uow.release_in_transaction(lease=lease, now=NOW)
            assert tx.in_transaction  # caller가 여전히 commit을 소유한다
            tx.commit()
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["state"] == "released"
    finally:
        completion.close()


def test_release_in_transaction_실패는_caller가_직접_rollback해야_한다(tmp_path: Path) -> None:
    # in_transaction seam은 begin/commit/rollback을 소유하지 않으므로, CAS가
    # stale로 실패해도 이 UoW는 rollback하지 않는다 — caller(S5.3)의 몫이다.
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        stale = uow.claim(ticket_id=ticket_id)
        uow.release(lease=stale)
        _lease_uow(completion, holder_id="b").claim(ticket_id=ticket_id)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            with pytest.raises(DurableDispatchLeaseConflict):
                uow.release_in_transaction(lease=stale, now=NOW)
            assert tx.in_transaction  # rollback되지 않았다 — caller 몫
            tx.rollback()
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# 추가 — 시계가 acquired_at보다 과거로 역행하는 경우(review-s52 P1-1 교정):
# renew/release CAS에 `acquired_at<=?`(새 expires_at 값)가 없으면
# `expires_at < acquired_at`인 행(S5.1 row invariant 위반)이 커밋될 수 있고,
# 그 뒤로는 `_validate_capability`가 전역 스캔이라 org 불문 모든 claim/renew/
# release는 물론 새 UoW 생성 자체가 영구히 막힌다(복구 경로 없음). CAS의
# WHERE 절 자체가 그 행의 커밋을 막아야 한다 — seam은 쓰기 뒤 재검증을
# 소유하지 않기 때문이다(계약).
# ---------------------------------------------------------------------------


def test_시계가_acquired_at보다_과거면_공개_release는_conflict이고_행이_무손상이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, holder_id="a", clock=lambda: NOW).claim(ticket_id=ticket_id)
        skewed = _lease_uow(completion, holder_id="a", clock=lambda: NOW - timedelta(hours=1))
        with pytest.raises(DurableDispatchLeaseConflict):
            skewed.release(lease=lease)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["state"] == "leased"
        assert row["acquired_at"] == _canonical(NOW)
        assert row["expires_at"] == _canonical(NOW + _TTL)
    finally:
        completion.close()


def test_시계가_acquired_at보다_과거면_공개_renew는_conflict이고_행이_무손상이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        lease = _lease_uow(completion, holder_id="a", clock=lambda: NOW).claim(ticket_id=ticket_id)
        skewed = _lease_uow(completion, holder_id="a", clock=lambda: NOW - timedelta(hours=1))
        with pytest.raises(DurableDispatchLeaseConflict):
            skewed.renew(lease=lease)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["expires_at"] == _canonical(NOW + _TTL)
        assert row["acquired_at"] == _canonical(NOW)
    finally:
        completion.close()


def test_release_in_transaction의_시계_역행은_conflict이고_커밋되지_않으며_영구잠금을_남기지_않는다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            with pytest.raises(DurableDispatchLeaseConflict):
                uow.release_in_transaction(lease=lease, now=NOW - timedelta(hours=1))
            tx.rollback()
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["state"] == "leased"
        assert row["acquired_at"] == _canonical(NOW)
        # 손상되지 않았으므로 이후 새 UoW 생성도 정상이다(영구 잠금 없음).
        _lease_uow(completion)
    finally:
        completion.close()


def test_renew_in_transaction의_시계_역행은_conflict이고_커밋되지_않으며_영구잠금을_남기지_않는다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            with pytest.raises(DurableDispatchLeaseConflict):
                uow.renew_in_transaction(lease=lease, now=NOW - timedelta(hours=1))
            tx.rollback()
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["expires_at"] == _canonical(NOW + _TTL)
        assert row["acquired_at"] == _canonical(NOW)
        _lease_uow(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# 추가 — release는 expires_at을 now로 덮어쓴다(review-s52 P2-4 교정).
# ---------------------------------------------------------------------------


def test_release는_expires_at을_now로_덮어쓴다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)
        assert lease.expires_at == NOW + _TTL
        uow.release(lease=lease)
        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["state"] == "released"
        assert row["expires_at"] == _canonical(NOW)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 17~20 — BUSY taxonomy(ADR 0042 §9 ⑫·team-lead 지시).
# ---------------------------------------------------------------------------


def test_다른_connection이_write_lock을_쥐면_claim은_busy이다(tmp_path: Path) -> None:
    # 무타입 sqlite3.OperationalError가 누수하지 않고 DurableDispatchLeaseBusy로
    # 분류돼야 한다. 짧은 timeout으로 테스트를 빠르게 유지한다.
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path, timeout=0.2)
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        blocker = sqlite3.connect(str(path), timeout=1.0)
        blocker.execute("PRAGMA foreign_keys=ON")
        blocker.execute("BEGIN IMMEDIATE")
        try:
            uow = _lease_uow(completion)
            with pytest.raises(DurableDispatchLeaseBusy):
                uow.claim(ticket_id=ticket_id)
        finally:
            blocker.rollback()
            blocker.close()
        # lock 경합이 해소된 뒤에는 정상 claim이 가능하다(영구 잠금이 아니다).
        assert _lease_uow(completion).claim(ticket_id=ticket_id) is not None
    finally:
        completion.close()


def test_busy는_unavailable의_subclass다() -> None:
    assert issubclass(DurableDispatchLeaseBusy, DurableDispatchLeaseUnavailable)


def test_생성자_capability_검증의_lock_경합도_busy로_분류한다(tmp_path: Path) -> None:
    # 생성자 open-time validate는 read지만, 다른 connection이 EXCLUSIVE를 쥐면
    # read도 막힌다. 이 경합이 일반 Unavailable로 오분류되면 S5.3 runner가
    # 재시도 가능한 경합을 부팅 실패로 오판한다(_reraise_capability의 Busy 분기).
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    try:
        _seed_dispatchable_ticket(completion)
        blocker = sqlite3.connect(path, timeout=0.2)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(DurableDispatchLeaseBusy):
                _lease_uow(completion)
        finally:
            blocker.rollback()
            blocker.close()
        # 경합 해소 뒤에는 생성이 정상이다(영구 실패가 아니다).
        assert _lease_uow(completion) is not None
    finally:
        completion.close()


@pytest.mark.parametrize(
    "code_name", ["SQLITE_BUSY_SNAPSHOT", "SQLITE_LOCKED_SHAREDCACHE", "SQLITE_BUSY_RECOVERY"]
)
def test_busy_extended_code도_primary_byte로_판별한다(tmp_path: Path, code_name: str) -> None:
    # `& 0xFF` masking이 load-bearing임을 고정한다 — 공유캐시 LOCKED(262)나 WAL
    # recovery BUSY(261)는 상수 집합에 없고 primary byte로만 BUSY/LOCKED가 된다.
    # masking을 지우면 이 셋이 일반 Unavailable로 오분류돼 runner가 재시도 가능한
    # 경합을 부팅 실패로 오판한다.
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        code = getattr(sqlite3, code_name)

        def raise_extended_busy(point: str) -> None:
            if point == "after_lease_insert":
                error = sqlite3.OperationalError(f"simulated {code_name}")
                error.sqlite_errorcode = code
                raise error

        uow = _lease_uow(completion, fault_injector=raise_extended_busy)
        with pytest.raises(DurableDispatchLeaseBusy):
            uow.claim(ticket_id=ticket_id)
    finally:
        completion.close()


@pytest.mark.parametrize(
    "code_name", ["SQLITE_FULL", "SQLITE_IOERR", "SQLITE_READONLY", "SQLITE_CANTOPEN"]
)
def test_busy가_아닌_sqlite_에러_계열은_일반_unavailable이다(
    tmp_path: Path, code_name: str
) -> None:
    # BUSY/LOCKED만 Busy로 좁힌다 — FULL/IOERR/READONLY/CANTOPEN 등은 재시도로
    # 풀리지 않으므로 runner를 멈춰야 하는 일반 Unavailable로 남아야 한다.
    # 실 디스크풀·I/O 실패는 재현이 어려우므로 fault_injector로 해당
    # extended code를 지닌 sqlite3.Error를 직접 주입한다.
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        code = getattr(sqlite3, code_name)

        def raise_non_busy(point: str) -> None:
            if point == "after_lease_insert":
                error = sqlite3.OperationalError(f"simulated {code_name}")
                error.sqlite_errorcode = code
                raise error

        uow = _lease_uow(completion, fault_injector=raise_non_busy)
        with pytest.raises(DurableDispatchLeaseUnavailable) as excinfo:
            uow.claim(ticket_id=ticket_id)
        assert not isinstance(excinfo.value, DurableDispatchLeaseBusy)
    finally:
        completion.close()


def test_busy_판별은_메시지_문자열이_아니라_extended_code로_한다(tmp_path: Path) -> None:
    # 문자열 매칭 금지를 직접 겨냥한다: 메시지 텍스트와 실제 BUSY 여부를
    # 서로 엇갈리게 만들어, code 기반 판별만이 두 케이스 모두를 올바르게
    # 가른다는 것을 증명한다("database is locked" 문자열 매칭으로 바꿔도
    # 우연히 통과하는 실 BUSY 시나리오 하나만으로는 이 오류를 못 잡는다).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)

        # (a) 메시지는 BUSY와 무관하지만 code는 진짜 SQLITE_BUSY다 → Busy여야 한다.
        def raise_busy_with_unrelated_message(point: str) -> None:
            if point == "after_lease_insert":
                error = sqlite3.OperationalError("something went wrong")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error

        uow_a = _lease_uow(completion, fault_injector=raise_busy_with_unrelated_message)
        with pytest.raises(DurableDispatchLeaseBusy):
            uow_a.claim(ticket_id=ticket_id)

        # (b) 메시지는 "locked"를 언급하지만 code는 SQLITE_FULL이다 → Busy가 아니어야 한다.
        def raise_full_with_deceptive_message(point: str) -> None:
            if point == "after_lease_insert":
                error = sqlite3.OperationalError("database is locked (실은 디스크가 가득 찼다)")
                error.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise error

        uow_b = _lease_uow(completion, fault_injector=raise_full_with_deceptive_message)
        with pytest.raises(DurableDispatchLeaseUnavailable) as excinfo:
            uow_b.claim(ticket_id=ticket_id)
        assert not isinstance(excinfo.value, DurableDispatchLeaseBusy)
    finally:
        completion.close()


def test_lease_uow는_busy_timeout_pragma를_바꾸지_않는다(tmp_path: Path) -> None:
    # connection은 Completion 소유라 빌려 쓰는 것뿐이다 — S5가 대기 시간을
    # 재설정하면 다른 component가 공유하는 자원의 성질을 바꾸는 소유권
    # 누수다. 대기 시간의 유일한 손잡이는 SqliteQuestionCompletionUnitOfWork
    # 생성자의 timeout이다.
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path, timeout=1.5)
    try:

        def read_busy_timeout() -> int:
            tx = completion.durable_transaction()
            with tx.scope():
                tx.begin_immediate()
                value = tx.execute("PRAGMA busy_timeout").fetchone()[0]
                tx.commit()
                return int(value)

        before = read_busy_timeout()
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)
        uow.renew(lease=lease)
        uow.release(lease=lease)
        after = read_busy_timeout()
        assert before == after
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 23 — `*_in_transaction`의 과거(단축) now는 성공하고 expires_at은
# acquired_at 이상을 유지한다(ADR 0042 §9 ⑬ 안전 논거 ③ — 단축은 결함이
# 아니라 at-least-once 봉투 안이다).
# ---------------------------------------------------------------------------


def test_renew_in_transaction의_단축된_now는_성공하고_expires_at은_acquired_at_이상이다(
    tmp_path: Path,
) -> None:
    # ttl이 고정이고 now>=acquired_at이 항상 강제되므로, 어떤 성공한 renewal도
    # "원래 claim의 expires_at"보다 짧아질 수는 없다(now>=acquired_at이면
    # now+ttl>=acquired_at+ttl=원래 expires_at). §9 ⑬이 말하는 "단축"은 그
    # 대신 "형제 컴포넌트가 공유하는 clock이 실제(정상적으로 한참 뒤)보다
    # 이른 값을 넘겨도 거부되지 않고, 그만큼 더 짧은 expires_at을 남긴다"는
    # 뜻이다 — acquired_at 바로 다음 순간(최단 성공)으로 renew해 거부되지
    # 않음을 보이고, "정상적으로 한참 뒤" 호출했을 가상의 expires_at과
    # 산술로 대비시킨다(두 번째 실제 renew는 하지 않는다 — 첫 renewal이
    # 만든 짧은 expires_at을 이미 지난 now로 다시 renew하면 red 8의 만료
    # 거부가 걸려 이 red의 취지와 다른 경로를 겨냥하게 된다).
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        ticket_id = _seed_dispatchable_ticket(completion)
        uow = _lease_uow(completion, holder_id="a", clock=lambda: NOW)
        lease = uow.claim(ticket_id=ticket_id)  # acquired_at=NOW, expires_at=NOW+TTL
        shortened_now = NOW + timedelta(microseconds=1)  # acquired_at 바로 다음 — 최단 성공
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            shortened = uow.renew_in_transaction(lease=lease, now=shortened_now)
            tx.commit()
        assert shortened.expires_at == shortened_now + _TTL
        assert shortened.expires_at >= NOW  # acquired_at 이상은 항상 유지된다

        hypothetical_later_expiry = (NOW + timedelta(minutes=10)) + _TTL
        assert shortened.expires_at < hypothetical_later_expiry  # 정상 호출보다 짧다 — 거부되지 않았다

        row = _lease_row(completion, ticket_id)
        assert row is not None
        assert row["expires_at"] >= row["acquired_at"]
    finally:
        completion.close()
