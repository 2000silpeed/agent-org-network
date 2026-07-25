"""P17.9 S5.4 답 수신·ticket 종결 원자화 UoW 테스트(ADR 0042 §9 ⑦⑧⑬⑭).

한 transaction에 answer receipt·dispatch lease release·WorkTicket 종결·
Completion terminal(AnswerRecord·audit·SessionTurn·outbox·Request Answered)이
전부 묶임을 검증한다. dispatch lease epoch는 이 경로의 CAS 조건이 아니다
(stale submit은 ticket.status·Request 이동·owner fence 셋으로만 잡는다).
"""

from __future__ import annotations
# pyright: reportPrivateUsage=false

import dataclasses
import hashlib
import inspect
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pytest

from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    CompletionEvidenceError,
)
from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import (
    AnswerCandidate,
    ApprovalRequired,
    FinalizationCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.durable_dispatch_delivery import (
    Delivered,
    DispatchFrame,
    DispatchOutcome,
    DispatchOwner,
    DurableDispatchRunner,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.runtime import AnswerMode
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_answer_ingestion_uow import (
    DurableAnswerAccepted,
    DurableAnswerApprovalRequired,
    DurableAnswerIngestionBusy,
    DurableAnswerIngestionConflict,
    DurableAnswerIngestionError,
    DurableAnswerIngestionUnavailable,
    DurableAnswerIngestionUnitOfWork,
    DurableWorkerAnswerCommand,
    _answer_digest,  # pyright: ignore[reportPrivateUsage]
    _answer_sha256,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.sqlite_durable_dispatch_delivery import (
    migrate_sqlite_durable_dispatch_delivery_schema,
    reconcile_sqlite_durable_dispatch_delivery_schema,
)
from agent_org_network.sqlite_durable_dispatch_lease_uow import (
    DurableDispatchLeaseError,
    DurableDispatchLeaseUnavailable,
    DurableDispatchLeaseUnitOfWork,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)
from agent_org_network.sqlite_durable_linked_reconciliation import (
    reconcile_sqlite_durable_linked_gate,
)
from agent_org_network.sqlite_durable_work_ticket_uow import (
    DurableWorkTicketEnqueueCommand,
    DurableWorkTicketEnqueueUnitOfWork,
)
from agent_org_network.sqlite_stores import (
    _question_request_values,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.worker_authorization import WorkerConnectionPrincipal

NOW = datetime(2026, 7, 25, tzinfo=UTC)
_TTL = timedelta(minutes=5)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


ORG_ID = _ref("org", "org-1")
# WorkTicket owner(S4.5 enqueue가 저장하는 owner_subject_id)와 이 답을
# 제출하는 WorkerConnectionPrincipal.owner_id는 같은 identity를 가리켜야
# durable owner fence를 통과한다 — 둘 다 "owner-a"에서 파생한다.
_TICKET_OWNER = _ref("subject", "owner-a")


class _Policy:
    """정확히 한 결과만 돌려주는 Fake `ApprovalPolicy` — S5.4는 gate_candidate를
    거치지 않고 FinalizationCandidate를 직접 구성하므로, `complete_in_transaction`
    내부 재평가(`_validate_no_approval`)가 그 값과 정확히 일치해야 한다."""

    def __init__(self, result: NoApprovalRequired | ApprovalRequired) -> None:
        self.result = result
        self.calls = 0

    def evaluate(
        self, org_id: str, route: RouteTarget, candidate_mode: AnswerMode
    ) -> NoApprovalRequired | ApprovalRequired:
        self.calls += 1
        return self.result


class _Resolver:
    def __init__(self, *, owner_id: str = "worker-owner-a") -> None:
        self.calls = 0
        self._owner_id = owner_id

    def resolve(self, *, org_id: str, route: RouteTarget) -> AnswerResponsibilitySnapshot | None:
        self.calls += 1
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id=self._owner_id)


class _Registry:
    """WorkTicket enqueue(S4.5)용 owner 주소 해석 Fake — seed 전용."""

    def __init__(self, *, owner: str | None = _TICKET_OWNER) -> None:
        self._owner = owner

    def resolve_owner_subject(self, *, org_id: str, agent_id: str) -> str | None:
        return self._owner


def _open_all(
    path: Path,
    *,
    policy_version: str = "v1",
    policy: _Policy | None = None,
    resolver_owner_id: str = "worker-owner-a",
    timeout: float = 5.0,
) -> SqliteQuestionCompletionUnitOfWork:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    return SqliteQuestionCompletionUnitOfWork(
        path,
        policy=policy or _Policy(NoApprovalRequired(policy_version=policy_version)),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=_Resolver(owner_id=resolver_owner_id),
        record_id_factory=lambda: uuid.uuid4().hex,
        clock=lambda: NOW,
        timeout=timeout,
    )


def _seed_awaiting_answer(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    request_id: str,
    org_id: str = ORG_ID,
    agent_id: str = "card-a",
    ticket_label: str = "ticket-1",
    owner: str | None = _TICKET_OWNER,
    requires_approval: bool = False,
    session_id: str | None = None,
) -> tuple[str, RouteTarget, int]:
    """Received → ReadyToDispatch → S4.5 enqueue로 claim 가능한 AwaitingAnswer를 만든다.

    (ticket_id, route, expected_request_revision)을 돌려준다.
    """
    received = QuestionRequest.receive(
        org_id=org_id,
        requester_id="user",
        question="환불 절차가 궁금합니다",
        request_id_factory=lambda: request_id,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
        session_id=session_id,
        context_snapshot=None,
    )
    completion.create(received)
    trigger_ref = _ref("trigger", request_id)
    route = RouteTarget(intent="refund", agent_id=agent_id, requires_approval=requires_approval)
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
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
        clock=lambda: NOW,
        ticket_id_factory=lambda: ticket_label,
        receipt_id_factory=lambda: f"receipt-{ticket_label}",
    )
    enqueued = enqueue_uow.enqueue(command=DurableWorkTicketEnqueueCommand(request_id, 1, 1))
    return enqueued.ticket_id, route, enqueued.request_revision


def _handoff(
    *,
    request_id: str,
    route: RouteTarget,
    expected_revision: int,
    attempt: int = 1,
    text: str = "영업일 기준 3일 안에 처리됩니다.",
    sources: tuple[str, ...] = ("환불정책.md",),
    mode: AnswerMode = "full",
    policy_version: str = "v1",
    needs_correction_review: bool = False,
) -> FinalizationCandidate:
    return FinalizationCandidate(
        request_id=request_id,
        expected_revision=expected_revision,
        attempt=attempt,
        route=route,
        candidate=AnswerCandidate(text=text, sources=sources, mode=mode),
        approval_evaluation=NoApprovalRequired(
            policy_version=policy_version, needs_correction_review=needs_correction_review
        ),
    )


def _command(
    *, ticket_id: str, request_id: str, expected_request_revision: int, handoff: FinalizationCandidate
) -> DurableWorkerAnswerCommand:
    return DurableWorkerAnswerCommand(
        ticket_id=ticket_id,
        request_id=request_id,
        expected_request_revision=expected_request_revision,
        handoff=handoff,
    )


def _principal(
    *,
    org_id: str = ORG_ID,
    owner_id: str = "owner-a",
    credential_id: str = "cred-1",
    credential_generation: int = 1,
    role: str = "primary",
    connection_epoch: str = "epoch-1",
) -> WorkerConnectionPrincipal:
    return WorkerConnectionPrincipal(
        org_id=org_id,
        owner_id=owner_id,
        credential_id=credential_id,
        credential_generation=credential_generation,
        role=role,  # type: ignore[arg-type]
        connection_epoch=connection_epoch,
    )


def _uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
    receipt_id_factory: Callable[[], str] | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> DurableAnswerIngestionUnitOfWork:
    return DurableAnswerIngestionUnitOfWork(
        completion=completion,
        clock=clock,
        receipt_id_factory=receipt_id_factory or (lambda: uuid.uuid4().hex),
        fault_injector=fault_injector,
    )


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


def _lease_row(completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str) -> sqlite3.Row | None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        row = tx.execute(
            "SELECT * FROM durable_dispatch_leases WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        tx.commit()
        return row


def _count(completion: SqliteQuestionCompletionUnitOfWork, table: str) -> int:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        n = tx.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        tx.commit()
        return int(n)


_ARTIFACT_TABLES = (
    "durable_dispatch_answer_receipts",
    "answer_records",
    "terminal_answer_audits",
    "request_session_turns",
    "question_delivery_outbox",
    "question_completion_receipts",
)


def _artifact_counts(completion: SqliteQuestionCompletionUnitOfWork) -> dict[str, int]:
    return {table: _count(completion, table) for table in _ARTIFACT_TABLES}


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


def _corrupt_ticket_attempt(
    completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str, value: int
) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "UPDATE durable_linked_work_tickets SET attempt=? WHERE ticket_id=?",
            (value, ticket_id),
        )
        tx.commit()


def _corrupt_ticket_status(
    completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str, value: str
) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "UPDATE durable_linked_work_tickets SET status=? WHERE ticket_id=?",
            (value, ticket_id),
        )
        tx.commit()


class _FixedDirectory:
    def __init__(self, owner: DispatchOwner | None) -> None:
        self._owner = owner

    def resolve_owner(self, *, org_id: str, agent_id: str) -> DispatchOwner | None:
        return self._owner


class _FixedChannel:
    def __init__(self, outcome: DispatchOutcome) -> None:
        self._outcome = outcome
        self.calls: list[DispatchFrame] = []

    def deliver(self, frame: DispatchFrame) -> DispatchOutcome:
        self.calls.append(frame)
        return self._outcome


# ---------------------------------------------------------------------------
# red 1 — 승인 불필요 답: 한 transaction에 모든 completion artifact + ticket
# 종결 + lease release가 함께 커밋된다.
# ---------------------------------------------------------------------------


def test_승인_불필요_답은_한_transaction에_모든_artifact를_함께_확정한다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r1")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-1", session_id="session-1"
        )
        # S5.3 runner가 이미 dispatch lease를 쥔 상태를 재현한다(정상 흐름).
        lease_uow = DurableDispatchLeaseUnitOfWork(
            completion=completion, holder_id="dispatcher-a", clock=lambda: NOW, lease_ttl=_TTL
        )
        lease_uow.claim(ticket_id=ticket_id)

        handoff = _handoff(
            request_id=request_id, route=route, expected_revision=expected_revision
        )
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        accepted = uow.accept(submitter=_principal(), command=command)

        assert isinstance(accepted, DurableAnswerAccepted)
        assert accepted.ticket_id == ticket_id
        assert accepted.request_id == request_id
        assert accepted.request_revision == expected_revision + 1

        ticket_row = _ticket_row(completion, ticket_id)
        assert ticket_row["status"] == "completed"

        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "released"

        bundle = completion.by_request(request_id)
        assert bundle is not None
        assert bundle.completion.record_id == accepted.record_id
        assert bundle.session_turn is not None
        assert bundle.delivery.kind == "answer_ready"
        assert isinstance(bundle.request.state, AnsweredRequest)
        assert bundle.request.state.record_id == accepted.record_id
        assert bundle.request.revision == expected_revision + 1

        receipt_rows_tx = completion.durable_transaction()
        with receipt_rows_tx.scope():
            receipt_rows_tx.begin_immediate()
            receipt = receipt_rows_tx.execute(
                "SELECT * FROM durable_dispatch_answer_receipts WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            receipt_rows_tx.commit()
        assert receipt is not None
        assert receipt["action"] == "work_ticket.complete"
        assert receipt["org_id"] == ORG_ID
        assert receipt["request_id"] == request_id
        assert receipt["principal_ref"] == _TICKET_OWNER
        assert receipt["expected_request_revision"] == expected_revision
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 10 — 만료 lease가 있어도, 활성 lease가 있어도, lease 행이 아예 없어도
#          답은 수용된다(fence 없음 실증 — dispatch lease epoch는 이 경로의
#          CAS 조건이 아니다).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lease_kind", ["none", "active", "expired"])
def test_lease_상태와_무관하게_답이_수용된다(tmp_path: Path, lease_kind: str) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r10-{lease_kind}")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label=f"ticket-10-{lease_kind}"
        )
        if lease_kind == "active":
            lease_uow = DurableDispatchLeaseUnitOfWork(
                completion=completion, holder_id="dispatcher-a", clock=lambda: NOW, lease_ttl=_TTL
            )
            lease_uow.claim(ticket_id=ticket_id)
        elif lease_kind == "expired":
            earlier = NOW - timedelta(hours=1)
            lease_uow = DurableDispatchLeaseUnitOfWork(
                completion=completion, holder_id="dispatcher-a", clock=lambda: earlier, lease_ttl=_TTL
            )
            lease_uow.claim(ticket_id=ticket_id)  # expires_at은 NOW보다 훨씬 과거

        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        accepted = uow.accept(submitter=_principal(), command=command)

        assert accepted.request_revision == expected_revision + 1
        ticket_row = _ticket_row(completion, ticket_id)
        assert ticket_row["status"] == "completed"
        if lease_kind == "none":
            assert _lease_row(completion, ticket_id) is None
        else:
            lease_row = _lease_row(completion, ticket_id)
            assert lease_row is not None
            assert lease_row["state"] == "released"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# review-s54 P1 — 남의 테이블(durable_dispatch_leases)을 직접 쓰는 release
# UPDATE에도 그 테이블의 행 불변식(expires_at >= acquired_at)을 지키는
# 하한 가드가 있어야 한다. 이 UoW의 clock이 lease acquired_at보다 과거로
# skew된 채 호출돼도(예: NTP 역행) 그 불변식을 깨는 행을 커밋하지 않는다 —
# 답 자체는 여전히 수용되고(사용자 결과를 잃지 않는다), lease release만
# 건너뛴다(rowcount 0, lease는 leased로 남는다 — 완결된 ticket은 S5.3·S5.5가
# status='pending'만 스캔하므로 무해하다).
# ---------------------------------------------------------------------------


def test_clock이_lease_acquired_at보다_과거여도_답은_수용되고_행_불변식이_유지된다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    try:
        request_id = _ref("request", "r-p1-acquired-at-skew")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-p1-acquired-at-skew"
        )
        lease_uow = DurableDispatchLeaseUnitOfWork(
            completion=completion, holder_id="dispatcher-a", clock=lambda: NOW, lease_ttl=_TTL
        )
        lease_uow.claim(ticket_id=ticket_id)
        before_lease = _lease_row(completion, ticket_id)
        assert before_lease is not None

        skewed_now = NOW - timedelta(hours=1)  # acquired_at(NOW)보다 과거
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion, clock=lambda: skewed_now)
        accepted = uow.accept(submitter=_principal(), command=command)

        # 답은 여전히 수용된다 — 사용자 결과를 잃지 않는다.
        assert accepted.request_revision == expected_revision + 1
        assert _ticket_row(completion, ticket_id)["status"] == "completed"
        bundle = completion.by_request(request_id)
        assert bundle is not None
        assert bundle.completion.record_id == accepted.record_id

        # lease release는 건너뛴다 — 불변식(expires_at>=acquired_at)을 깨는
        # 행을 커밋하지 않는다(release CAS의 acquired_at<=? 하한 가드).
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"
        assert lease_row["expires_at"] == before_lease["expires_at"]
        assert lease_row["acquired_at"] == before_lease["acquired_at"]
        assert lease_row["expires_at"] >= lease_row["acquired_at"]

        report = reconcile_sqlite_durable_dispatch_delivery_schema(path)
        assert report.capable is True
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 2 — replay: 같은 command_digest는 write 0로 같은 결과를 돌려준다.
# ---------------------------------------------------------------------------


def test_같은_command는_replay되고_write가_늘지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r2")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-2"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        submitter = _principal()
        first = uow.accept(submitter=submitter, command=command)
        before = _artifact_counts(completion)

        second = uow.accept(submitter=submitter, command=command)

        assert second == first
        assert _artifact_counts(completion) == before
        assert _count(completion, "durable_dispatch_answer_receipts") == 1
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 3 — 다른 Owner 제출 → Conflict(owner fence)·write0
# ---------------------------------------------------------------------------


def test_다른_owner가_제출하면_owner_fence로_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r3")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-3", owner=_TICKET_OWNER
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(owner_id="다른-worker"), command=command)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 4 — ticket 이미 completed → Conflict(중복 배달의 두 번째 답 흡수 실증)
# ---------------------------------------------------------------------------


def test_ticket이_이미_completed면_conflict이고_두번째_답은_흡수된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r4")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-4"
        )
        uow = _uow(completion)
        submitter = _principal()
        first_handoff = _handoff(
            request_id=request_id, route=route, expected_revision=expected_revision
        )
        first_command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=first_handoff,
        )
        first = uow.accept(submitter=submitter, command=first_command)

        # 중복 배달분(§9 ⑦) — 다른 워커/재시도가 다른 본문으로 같은 ticket을
        # 다시 제출한다(같은 digest가 아니므로 replay가 아니다).
        second_handoff = _handoff(
            request_id=request_id,
            route=route,
            expected_revision=expected_revision,
            text="다른 본문으로 재제출",
        )
        second_command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=second_handoff,
        )
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=submitter, command=second_command)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "completed"
        bundle = completion.by_request(request_id)
        assert bundle is not None
        assert bundle.completion.record_id == first.record_id  # 첫 답이 그대로 유지된다
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 5a — command.expected_request_revision이 실제 Request revision과
#          다르면 Conflict·write0이다.
# ---------------------------------------------------------------------------


def test_expected_request_revision_불일치는_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r5a")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-5a"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision + 5,  # 어긋난 revision
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 5b — Request가 (retry 등으로) 다른 ticket_id를 가리키면 Conflict·write0.
# ---------------------------------------------------------------------------


def test_request가_다른_ticket_id를_가리키면_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r5b")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-5b"
        )
        current = completion.get(request_id)
        assert current is not None
        assert isinstance(current.state, AwaitingAnswer)
        other_ticket_id = _ref("ticket", "other-ticket")
        drifted_state = AwaitingAnswer(
            route=current.state.route,
            attempt=current.state.attempt,
            ticket_id=other_ticket_id,
            handling=HandlingAssignment(
                kind="runtime_ticket", ref=other_ticket_id, due_at=current.state.handling.due_at
            ),
        )
        # 도메인 API로는 AwaitingAnswer→AwaitingAnswer(same-state) 전이가
        # 금지된다(question_request._transition_violation) — 이 시나리오는
        # 다른 하위 시스템의 손상을 흉내내는 것이므로 row를 직접 덮어써
        # revision은 그대로 두고 state만 drift시킨다(격리: revision 검사가
        # 아니라 ticket_id 검사 단독을 겨냥한다).
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

        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 5c — Request가 AwaitingAnswer가 아니면 Conflict·write0이다.
# ---------------------------------------------------------------------------


def test_request가_awaitinganswer가_아니면_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r5c")
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
        route = RouteTarget(intent="refund", agent_id="card-a", requires_approval=False)
        ready = received.record_initial_routing(
            intent="refund",
            disposition="routed",
            target=ReadyToDispatch(
                route=route,
                attempt=1,
                trigger_key=trigger_ref,
                handling=HandlingAssignment(
                    kind="system", ref=trigger_ref, due_at=NOW + timedelta(hours=1)
                ),
            ),
            clock=lambda: NOW - timedelta(minutes=1),
        )
        assert completion.compare_and_set(request_id, 0, received, ready)

        # WorkTicket 행을 enqueue UoW 없이 직접 삽입한다(S5.3 red 11과 동형) —
        # Request는 여전히 ReadyToDispatch다.
        ticket_id = _ref("ticket", "manual-5c")
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "INSERT INTO durable_linked_work_tickets VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    ticket_id,
                    ORG_ID,
                    request_id,
                    1,
                    0,
                    "a" * 64,
                    _TICKET_OWNER,
                    "pending",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            tx.commit()

        handoff = _handoff(request_id=request_id, route=route, expected_revision=1)
        command = _command(
            ticket_id=ticket_id, request_id=request_id, expected_request_revision=1, handoff=handoff
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
    finally:
        completion.close()


def test_ticket_status만_손상되면_conflict이다(tmp_path: Path) -> None:
    """Request는 여전히 정상 AwaitingAnswer지만 ticket.status만 'pending'이
    아닌 경우(예: 다른 하위 시스템이 독립적으로 escalate)를 단독으로
    겨냥한다 — red 4(정상 accept 뒤 재제출)와 달리 Request state는 손상시키지
    않는다."""
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-ticket-status-only")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-status-only"
        )
        _corrupt_ticket_status(completion, ticket_id, "escalated")

        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)  # Request는 손상되지 않았다
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 6 — requires_approval=True → DurableAnswerApprovalRequired·전 테이블
#         write0·ticket pending·Request AwaitingAnswer 유지.
# ---------------------------------------------------------------------------


def test_route_requires_approval이면_approval_required이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r6")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-6", requires_approval=True
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerApprovalRequired):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
        assert _lease_row(completion, ticket_id) is None
        ticket_row = _ticket_row(completion, ticket_id)
        assert ticket_row["status"] == "pending"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 7 — mode=="draft_only" → 동일(ApprovalRequired·write0).
# ---------------------------------------------------------------------------


def test_mode가_draft_only면_approval_required이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r7")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-7", requires_approval=False
        )
        handoff = _handoff(
            request_id=request_id, route=route, expected_revision=expected_revision, mode="draft_only"
        )
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerApprovalRequired):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
        ticket_row = _ticket_row(completion, ticket_id)
        assert ticket_row["status"] == "pending"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ADR 0042 §9 ⑮(신설) — 승인 축은 탐지 시점으로 나뉜다. route/mode 축(위
# red6/7)은 S5.4가 write 전에 typed `DurableAnswerApprovalRequired`로 담당
# 하지만, "gate 이후 중앙 정책이 바뀌었다"는 policy 축은 `complete_in_
# transaction`을 실제로 호출해야만 알 수 있어 Completion의
# `_validate_no_approval`이 commit 직전에 `CompletionEvidenceError`로
# 담당한다. 이 분담이 성립하려면 `_reraise_write`가 그 예외를 **wrap 없이
# 그대로 통과**시켜야 한다 — 자기 계열로 감싸면 policy 축이 저장소 오류와
# 섞여 호출자가 "다시 ApprovalBoundary로 gate하라"는 신호를 구분할 수 없다.
# ---------------------------------------------------------------------------


def test_gate_이후_정책이_바뀌면_completionevidenceerror가_wrap_없이_통과한다(
    tmp_path: Path,
) -> None:
    completion = _open_all(
        tmp_path / "workflow.sqlite",
        # gate 시점엔 "v1"(NoApprovalRequired)이었지만, 답 수신 시점엔 중앙
        # 정책이 사람 승인을 요구하도록 바뀌었다(policy drift 재현).
        policy=_Policy(ApprovalRequired(approver_id="approver-a", policy_version="v2")),
    )
    try:
        request_id = _ref("request", "r-policy-drift")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-policy-drift"
        )
        # route.requires_approval=False·mode="full"이라 S5.4 자신의
        # route/mode 축 사전검사는 통과한다 — policy 축만 단독으로 겨냥한다.
        handoff = _handoff(
            request_id=request_id, route=route, expected_revision=expected_revision,
            policy_version="v1",
        )
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(CompletionEvidenceError) as excinfo:
            uow.accept(submitter=_principal(), command=command)
        assert type(excinfo.value) is CompletionEvidenceError  # wrap 없이 그대로
        assert not isinstance(excinfo.value, DurableAnswerIngestionError)

        assert _artifact_counts(completion) == before  # 양쪽 축 다 write 0
        assert _lease_row(completion, ticket_id) is None
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 8 — handoff route/attempt/expected_revision 불일치 → Conflict(각각 격리).
# ---------------------------------------------------------------------------


def test_handoff_route가_다르면_conflict이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r8-route")
        ticket_id, _route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-8-route"
        )
        other_route = RouteTarget(intent="refund", agent_id="card-b", requires_approval=False)
        handoff = _handoff(
            request_id=request_id, route=other_route, expected_revision=expected_revision
        )
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_handoff_attempt이_다르면_conflict이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r8-attempt")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-8-attempt"
        )
        handoff = _handoff(
            request_id=request_id, route=route, expected_revision=expected_revision, attempt=2
        )
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_request_attempt만_손상되면_handoff_attempt_결박이_conflict이다(tmp_path: Path) -> None:
    """ticket.attempt와 handoff.attempt는 같지만 request.state.attempt만
    어긋나는 경우를 단독으로 겨냥한다(위 테스트와 반대쪽 조건)."""
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r8-request-attempt")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-8-request-attempt"
        )
        _corrupt_ticket_attempt(completion, ticket_id, 2)

        # handoff.attempt(2)는 손상된 ticket.attempt(2)와는 같지만 손상되지
        # 않은 request.state.attempt(1)와는 다르다.
        handoff = _handoff(
            request_id=request_id, route=route, expected_revision=expected_revision, attempt=2
        )
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_handoff_expected_revision이_다르면_conflict이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r8-revision")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-8-revision"
        )
        handoff = _handoff(
            request_id=request_id, route=route, expected_revision=expected_revision + 1
        )
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_handoff_request_id가_다르면_conflict이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r8-reqid")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-8-reqid"
        )
        handoff = _handoff(
            request_id=_ref("request", "other-request"),
            route=route,
            expected_revision=expected_revision,
        )
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_ticket_attempt이_손상되면_handoff_attempt_결박이_conflict이다(tmp_path: Path) -> None:
    """request.state.attempt와 handoff.attempt는 같지만 ticket.attempt만
    어긋나는 경우를 단독으로 겨냥한다 — 한 조건이 다른 조건에 가려지지 않게."""
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r8-ticket-attempt")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-8-ticket-attempt"
        )
        _corrupt_ticket_attempt(completion, ticket_id, 99)

        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# 추가 격리 — ticket 존재/lineage 각 OR-조건 단독 확인(mutation 대비).
# ---------------------------------------------------------------------------


def test_존재하지_않는_ticket_id는_conflict이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-no-ticket")
        _, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-no-ticket"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=_ref("ticket", "does-not-exist"),
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_ticket의_org가_submitter_org와_다르면_conflict이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-org-mismatch")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-org-mismatch"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(
                submitter=_principal(org_id=_ref("org", "other-org")), command=command
            )

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_ticket의_awaiting_revision이_손상되면_conflict이다(tmp_path: Path) -> None:
    """command.expected_request_revision·request.revision은 일치하지만
    ticket.awaiting_revision만 어긋나는 경우를 단독으로 겨냥한다."""
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-awaiting-revision-corrupt")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion,
            request_id=request_id,
            ticket_label="ticket-awaiting-revision-corrupt",
        )
        _corrupt_ticket_awaiting_revision(completion, ticket_id, 99)

        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionConflict):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 9 — fault 4지점: 각 지점에서 네 결과(receipt·lease·ticket·Completion)가
#         함께 롤백된다. 특히 after_completion: ticket completed인데
#         Completion 미확정인 상태가 관측되지 않는다.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "point",
    ["after_answer_receipt", "after_lease_release", "after_work_ticket_status", "after_completion"],
)
def test_fault_지점마다_네_결과가_함께_롤백된다(tmp_path: Path, point: str) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r9-{point}")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label=f"ticket-9-{point}"
        )
        lease_uow = DurableDispatchLeaseUnitOfWork(
            completion=completion, holder_id="dispatcher-a", clock=lambda: NOW, lease_ttl=_TTL
        )
        lease_uow.claim(ticket_id=ticket_id)

        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )

        def raise_at(injected: str) -> None:
            if injected == point:
                raise RuntimeError(injected)

        uow = _uow(completion, fault_injector=raise_at)
        before = _artifact_counts(completion)

        with pytest.raises(RuntimeError, match=point):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before  # 넷 다 함께 롤백된다
        ticket_row = _ticket_row(completion, ticket_id)
        assert ticket_row["status"] == "pending"  # completed가 홀로 관측되지 않는다
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 11 — 답 확정 뒤 run_once → 그 ticket은 더 이상 후보가 아니다.
# ---------------------------------------------------------------------------


def test_답_확정_뒤_run_once는_그_ticket을_후보로_보지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r11")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-11"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        uow.accept(submitter=_principal(), command=command)

        lease_uow = DurableDispatchLeaseUnitOfWork(
            completion=completion, holder_id="dispatcher-b", clock=lambda: NOW, lease_ttl=_TTL
        )
        directory = _FixedDirectory(DispatchOwner(owner_id="owner-a", owner_subject_ref=_TICKET_OWNER))
        runner = DurableDispatchRunner(
            completion=completion,
            lease_uow=lease_uow,
            channel=_FixedChannel(Delivered()),
            directory=directory,
            clock=lambda: NOW,
            attempt_id_factory=lambda: uuid.uuid4().hex,
        )

        report = runner.run_once()

        assert report.scanned == 0
        assert report.claimed == 0
        assert ticket_id  # 참조 유지
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# red 12 — 답 확정 뒤 reconcile_sqlite_durable_linked_gate가 green이다(S4.6
#          교차 소유권 회귀 고정 — ticket.status='completed'는 이미 허용값).
# ---------------------------------------------------------------------------


def test_답_확정_뒤_s4_6_reconciliation_gate가_green이다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    try:
        request_id = _ref("request", "r12")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-12"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        uow.accept(submitter=_principal(), command=command)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path)
    assert report.capable is True
    assert report.violations == ()


# ---------------------------------------------------------------------------
# red 13 — 시그니처: command에 lease_epoch 없음·생성자에 CentralAuthorizer 없음.
# ---------------------------------------------------------------------------


def test_durableworkeranswercommand에는_lease_epoch_필드가_없다() -> None:
    field_names = {f.name for f in dataclasses.fields(DurableWorkerAnswerCommand)}
    assert "lease_epoch" not in field_names
    assert field_names == {"ticket_id", "request_id", "expected_request_revision", "handoff"}


def test_생성자에_centralauthorizer_인자가_없다() -> None:
    params = set(inspect.signature(DurableAnswerIngestionUnitOfWork.__init__).parameters)
    assert "central_authorizer" not in params
    assert "authorizer" not in params
    assert params == {"self", "completion", "clock", "receipt_id_factory", "fault_injector"}


# ---------------------------------------------------------------------------
# red 14 — capability 손상은 S5.2가 이미 확립한 Lease 계열로 wrap 없이
#          관측된다(ctor·accept() 양쪽 진입점 모두 같은 타입).
# ---------------------------------------------------------------------------


def test_capability_손상_후_생성자는_dispatchleaseunavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute("DROP TABLE durable_dispatch_answer_receipts")
            tx.commit()

        with pytest.raises(DurableDispatchLeaseUnavailable) as excinfo:
            _uow(completion)
        assert type(excinfo.value) is DurableDispatchLeaseUnavailable
        assert not isinstance(excinfo.value, DurableAnswerIngestionError)
    finally:
        completion.close()


def test_capability_손상_후_accept은_dispatchleaseunavailable이고_write0이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r14")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-14"
        )
        uow = _uow(completion)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_work_tickets SET org_id=? WHERE ticket_id=?",
                (_ref("org", "org-corrupted"), ticket_id),
            )
            tx.commit()

        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )

        with pytest.raises(DurableDispatchLeaseUnavailable) as excinfo:
            uow.accept(submitter=_principal(), command=command)
        assert type(excinfo.value) is DurableDispatchLeaseUnavailable
        assert _count(completion, "durable_dispatch_answer_receipts") == 0
    finally:
        completion.close()


def test_capability_손상_예외는_lease_계열이지_ingestion_계열이_아니다() -> None:
    assert issubclass(DurableDispatchLeaseUnavailable, DurableDispatchLeaseError)
    assert not issubclass(DurableDispatchLeaseUnavailable, DurableAnswerIngestionError)
    assert not issubclass(DurableAnswerIngestionError, DurableDispatchLeaseError)


# ---------------------------------------------------------------------------
# 추가 — accept() 자신의 write 경계에서 난 lock 경합은 이 모듈 자신의
# DurableAnswerIngestionBusy로 분류된다(S5.2·S5.3과 같은 규율 — 판단해
# 보고: additive Busy subclass 신설).
# ---------------------------------------------------------------------------


def test_다른_connection이_lock을_쥐면_accept은_durableansweringestionbusy이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path, timeout=0.2)
    try:
        request_id = _ref("request", "r-busy")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-busy"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)

        blocker = sqlite3.connect(str(path), timeout=1.0)
        blocker.execute("PRAGMA foreign_keys=ON")
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(DurableAnswerIngestionBusy) as excinfo:
                uow.accept(submitter=_principal(), command=command)
            assert type(excinfo.value) is DurableAnswerIngestionBusy
        finally:
            blocker.rollback()
            blocker.close()

        assert _count(completion, "durable_dispatch_answer_receipts") == 0
        assert _ticket_row(completion, ticket_id)["status"] == "pending"

        # lock 해제 뒤 재실행은 정상이다(영구 잠금이 아니다).
        accepted = uow.accept(submitter=_principal(), command=command)
        assert accepted.request_revision == expected_revision + 1
    finally:
        completion.close()


def test_durableansweringestionbusy는_unavailable의_subclass다() -> None:
    assert issubclass(DurableAnswerIngestionBusy, DurableAnswerIngestionUnavailable)
    assert issubclass(DurableAnswerIngestionUnavailable, DurableAnswerIngestionError)
    assert issubclass(DurableAnswerApprovalRequired, DurableAnswerIngestionError)
    assert not issubclass(DurableAnswerApprovalRequired, DurableAnswerIngestionUnavailable)
    assert not issubclass(DurableAnswerApprovalRequired, DurableAnswerIngestionConflict)


# ---------------------------------------------------------------------------
# 추가 — 타입 검사(exact type)·command 형식 검사.
# ---------------------------------------------------------------------------


def test_submitter_타입이_아니면_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-bad-submitter")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-bad-submitter"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)

        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=object(), command=command)  # type: ignore[arg-type]
    finally:
        completion.close()


def test_command_타입이_아니면_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        uow = _uow(completion)
        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=_principal(), command=object())  # type: ignore[arg-type]
    finally:
        completion.close()


def test_handoff_타입이_finalizationcandidate가_아니면_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-bad-handoff")
        ticket_id, _route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-bad-handoff"
        )
        command = DurableWorkerAnswerCommand(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=object(),  # type: ignore[arg-type]
        )
        uow = _uow(completion)

        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=_principal(), command=command)
    finally:
        completion.close()


@pytest.mark.parametrize("bad_revision", [0, -1])
def test_expected_request_revision이_1미만이면_unavailable이다(
    tmp_path: Path, bad_revision: int
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r-bad-rev-{bad_revision}")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label=f"ticket-bad-rev-{bad_revision}"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=bad_revision,
            handoff=handoff,
        )
        uow = _uow(completion)

        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=_principal(), command=command)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# review-s54 P2-5 — `_valid_command`의 blank·비-int 조건이 각각 typed
# Unavailable로 닫히고 raw TypeError가 typed 계열 밖으로 새지 않는다.
# ---------------------------------------------------------------------------


def test_ticket_id가_blank이면_unavailable이고_typeerror가_새지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-blank-ticket-id")
        _ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-blank-ticket-id"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = DurableWorkerAnswerCommand(
            ticket_id="   ",
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)

        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=_principal(), command=command)
    finally:
        completion.close()


def test_request_id가_blank이면_unavailable이고_typeerror가_새지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-blank-request-id")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-blank-request-id"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = DurableWorkerAnswerCommand(
            ticket_id=ticket_id,
            request_id="",
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)

        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=_principal(), command=command)
    finally:
        completion.close()


@pytest.mark.parametrize("bad_revision", [None, "1", 1.0, True])
def test_expected_request_revision이_비int이면_unavailable이고_typeerror가_새지_않는다(
    tmp_path: Path, bad_revision: object
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r-nonint-rev-{bad_revision!r}")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label=f"ticket-nonint-rev-{bad_revision!r}"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = DurableWorkerAnswerCommand(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=bad_revision,  # type: ignore[arg-type]
            handoff=handoff,
        )
        uow = _uow(completion)

        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=_principal(), command=command)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# review-s54 P2-3 — tz-naive clock은 accept()에서 Unavailable이고 write0이다
# (S5.2 red 12 판).
# ---------------------------------------------------------------------------


def test_tz_naive_clock은_accept에서_unavailable이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-tz-naive-clock")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-tz-naive-clock"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        naive_now = datetime(2026, 7, 25)
        uow = _uow(completion, clock=lambda: naive_now)
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# review-s54 P2-4 — receipt_id_factory가 blank·비-str을 돌려주면
# `_new_receipt_id`가 Unavailable로 닫고 write0이다.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_receipt_id", ["", "   ", None])
def test_receipt_id_factory가_blank_비str이면_unavailable이고_write0이다(
    tmp_path: Path, bad_receipt_id: object
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r-bad-receipt-id-{bad_receipt_id!r}")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion,
            request_id=request_id,
            ticket_label=f"ticket-bad-receipt-id-{bad_receipt_id!r}",
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion, receipt_id_factory=lambda: bad_receipt_id)  # type: ignore[arg-type,return-value]
        before = _artifact_counts(completion)

        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=_principal(), command=command)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# 추가 — `_stored_result` 손상 방어(mutation 대비, S4.5
# `test_stored_result_손상_방어`와 동형). S5.1 schema validator가 이미
# `_ref`/`_sha256`/`_enum`/`_parent_ticket`로 잡는 손상(예: action 유일값·
# org_id/ticket_id/request_id의 parent_ticket 교차)은 이 층에서 격리 불가능
# 하므로 다루지 않는다 — 여기서는 S5.1이 형식만 검증하고 의미는 검증하지
# 않는 필드(expected_request_revision·principal_ref·answer_sha256)와,
# S5.1이 아예 다루지 않는 필드(ticket.status)만 겨냥한다.
# ---------------------------------------------------------------------------


class _Corruption(Protocol):
    def __call__(
        self, tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str, request_id: str
    ) -> None: ...


def _corrupt_receipt_expected_revision(
    tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str, request_id: str
) -> None:
    tx.execute(
        "UPDATE durable_dispatch_answer_receipts SET expected_request_revision=? "
        "WHERE command_digest=?",
        (999, digest),
    )


def _corrupt_receipt_principal_ref(
    tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str, request_id: str
) -> None:
    tx.execute(
        "UPDATE durable_dispatch_answer_receipts SET principal_ref=? WHERE command_digest=?",
        (_ref("subject", "someone-else"), digest),
    )


def _corrupt_receipt_answer_sha256(
    tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str, request_id: str
) -> None:
    tx.execute(
        "UPDATE durable_dispatch_answer_receipts SET answer_sha256=? WHERE command_digest=?",
        ("9" * 64, digest),
    )


def _corrupt_receipt_ticket_id_to_sibling_ticket(
    tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str, request_id: str
) -> None:
    # 같은 request의 다른 attempt(재시도) ticket으로 바꿔치기한다 — org_id·
    # request_id는 그대로라 S5.1의 `_parent_ticket` 교차 검증은 통과하고,
    # sibling ticket도 status='completed'로 만들어 `_stored_result`의 ticket
    # status 검사를 함께 통과시킨다 — 그래야 `receipt["ticket_id"] !=
    # command.ticket_id`(자기 자신의 검사)만 단독으로 격리된다(다른 검사에
    # 가려지지 않게).
    sibling_ticket_id = _ref("ticket", f"{ticket_id}-sibling")
    tx.execute(
        "INSERT INTO durable_linked_work_tickets VALUES(?,?,?,?,?,?,?,?,?)",
        (
            sibling_ticket_id,
            ORG_ID,
            request_id,
            2,
            1,
            "b" * 64,
            _TICKET_OWNER,
            "completed",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    tx.execute(
        "UPDATE durable_dispatch_answer_receipts SET ticket_id=? WHERE command_digest=?",
        (sibling_ticket_id, digest),
    )


def _corrupt_ticket_status_back_to_pending(
    tx: SqliteCompletionTransaction, *, digest: str, ticket_id: str, request_id: str
) -> None:
    tx.execute(
        "UPDATE durable_linked_work_tickets SET status='pending' WHERE ticket_id=?",
        (ticket_id,),
    )


_STORED_CORRUPTIONS: tuple[tuple[str, _Corruption], ...] = (
    ("expected_request_revision", _corrupt_receipt_expected_revision),
    ("principal_ref", _corrupt_receipt_principal_ref),
    ("answer_sha256", _corrupt_receipt_answer_sha256),
    ("ticket_id_sibling", _corrupt_receipt_ticket_id_to_sibling_ticket),
    ("ticket_status", _corrupt_ticket_status_back_to_pending),
)


@pytest.mark.parametrize(
    "mutate",
    [mutate for _, mutate in _STORED_CORRUPTIONS],
    ids=[name for name, _ in _STORED_CORRUPTIONS],
)
def test_stored_result_손상_방어(tmp_path: Path, mutate: _Corruption) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-stored-corrupt")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-stored-corrupt"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        uow = _uow(completion)
        submitter = _principal()
        uow.accept(submitter=submitter, command=command)
        digest = _answer_digest(submitter, command)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            mutate(tx, digest=digest, ticket_id=ticket_id, request_id=request_id)
            tx.commit()

        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=submitter, command=command)
    finally:
        completion.close()


def test_stored_result_request가_answered가_아니면_unavailable이다(tmp_path: Path) -> None:
    """receipt·완결된 ticket은 있지만 Request가 아직 AnsweredRequest로
    전이되지 않은 손상(원자성이 깨진 것처럼 보이는 상황)을 직접 조립해
    `_stored_result`의 `not isinstance(request.state, AnsweredRequest)`
    검사를 단독으로 겨냥한다."""
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-stored-not-answered")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-stored-not-answered"
        )
        handoff = _handoff(request_id=request_id, route=route, expected_revision=expected_revision)
        command = _command(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        submitter = _principal()
        digest = _answer_digest(submitter, command)
        owner_ref = _ref("subject", submitter.owner_id)
        answer_sha = _answer_sha256(handoff.candidate)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_work_tickets SET status='completed' WHERE ticket_id=?",
                (ticket_id,),
            )
            tx.execute(
                "INSERT INTO durable_dispatch_answer_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    _ref("receipt", "fabricated"),
                    ORG_ID,
                    ticket_id,
                    request_id,
                    digest,
                    owner_ref,
                    "work_ticket.complete",
                    expected_revision,
                    answer_sha,
                    "2026-01-01T00:00:00.000000+00:00",
                ),
            )
            tx.commit()

        uow = _uow(completion)
        with pytest.raises(DurableAnswerIngestionUnavailable):
            uow.accept(submitter=submitter, command=command)
    finally:
        completion.close()


