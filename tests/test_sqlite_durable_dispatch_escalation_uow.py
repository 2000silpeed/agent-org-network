"""P17.9 S5.6b dispatch timeout escalation UoW 테스트(ADR 0066 §5.3·ADR 0042 §9 ⑯⑰).

timeout escalation은 사람 명령이 아니라 system 전이다 — 스캔(S5.5)이 준 후보
목록·due_at은 write 근거가 아니며, escalate()는 begin_immediate() 안에서
ticket·Request를 재조회해 SLA를 다시 판정한다. 이 파일은 그 재판정·⑯ 남의 행
CAS 가드·replay·fault 원자성·에러 계열 분담을 red로 고정한다.
"""

from __future__ import annotations

import inspect
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.approval import (
    AnswerCandidate,
    FinalizationCandidate,
    InMemoryApprovalStore,
    NoApprovalRequired,
)
from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingManager,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_answer_ingestion_uow import (
    DurableAnswerIngestionConflict,
    DurableAnswerIngestionUnitOfWork,
    DurableWorkerAnswerCommand,
)
from agent_org_network.sqlite_durable_dispatch_delivery import (
    migrate_sqlite_durable_dispatch_delivery_schema,
)
from agent_org_network.sqlite_durable_dispatch_escalation import (
    migrate_sqlite_durable_dispatch_escalation_schema,
)
from agent_org_network.sqlite_durable_dispatch_escalation_uow import (
    SYSTEM_SUBJECT_REF,
    DurableDispatchEscalated,
    DurableDispatchEscalationBusy,
    DurableDispatchEscalationConflict,
    DurableDispatchEscalationError,
    DurableDispatchEscalationUnavailable,
    DurableDispatchEscalationUnitOfWork,
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
from agent_org_network.sqlite_stores import CorruptQuestionRequestError
from agent_org_network.worker_authorization import WorkerConnectionPrincipal

# ---------------------------------------------------------------------------
# 시간축 — 모든 clock을 이 상수들로 고정해 전이마다 요구되는 단조성
# (updated_at 역행 금지·target.handling.due_at >= 전이 시각)을 한눈에 보이게 한다.
# ---------------------------------------------------------------------------

T0 = datetime(2026, 7, 25, tzinfo=UTC)
DUE_AT = T0 + timedelta(minutes=30)  # AwaitingAnswer의 SLA
NOT_YET_DUE_NOW = T0 + timedelta(minutes=10)  # < DUE_AT
OVERDUE_NOW = T0 + timedelta(hours=2)  # > DUE_AT
DUE_AT_2 = OVERDUE_NOW + timedelta(minutes=30)  # attempt 2의 SLA
OVERDUE_NOW_2 = OVERDUE_NOW + timedelta(hours=2)  # > DUE_AT_2

_SLA = timedelta(hours=6)
_LEASE_TTL = timedelta(minutes=5)


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


ORG_ID = _ref("org", "org-1")
_TICKET_OWNER = _ref("subject", "owner-a")
_MANAGER_REF = _ref("subject", "manager-a")


class _Policy:
    def __init__(self, result: NoApprovalRequired) -> None:
        self.result = result

    def evaluate(self, org_id: str, route: RouteTarget, candidate_mode: str) -> NoApprovalRequired:
        return self.result


class _Resolver:
    def resolve(self, *, org_id: str, route: RouteTarget) -> None:
        return None


class _Registry:
    """WorkTicket enqueue(S4.5)용 owner 주소 해석 Fake — seed 전용."""

    def __init__(self, *, owner: str | None = _TICKET_OWNER) -> None:
        self._owner = owner

    def resolve_owner_subject(self, *, org_id: str, agent_id: str) -> str | None:
        return self._owner


class _Directory:
    """`DispatchEscalationTargetDirectory` Fake — manager 해석 결과·예외를 주입한다."""

    def __init__(self, manager: str | None = _MANAGER_REF, *, raises: bool = False) -> None:
        self._manager = manager
        self._raises = raises
        self.calls = 0

    def resolve_manager(self, *, org_id: str, owner_subject_ref: str) -> str | None:
        self.calls += 1
        if self._raises:
            raise RuntimeError("directory 조회 실패")
        return self._manager


def _open_all(path: Path, *, timeout: float = 5.0) -> SqliteQuestionCompletionUnitOfWork:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    return SqliteQuestionCompletionUnitOfWork(
        path,
        policy=_Policy(NoApprovalRequired(policy_version="v1")),
        approvals=InMemoryApprovalStore(),
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: uuid.uuid4().hex,
        clock=lambda: T0,
        timeout=timeout,
    )


def _seed_awaiting_answer(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    request_id: str,
    org_id: str = ORG_ID,
    agent_id: str = "card-a",
    ticket_label: str,
    owner: str | None = _TICKET_OWNER,
    due_at: datetime = DUE_AT,
    requires_approval: bool = False,
) -> tuple[str, RouteTarget, int]:
    """Received → ReadyToDispatch → S4.5 enqueue로 attempt=1 AwaitingAnswer를 만든다.

    (ticket_id, route, expected_request_revision)을 돌려준다 — expected_request_
    revision은 ticket.awaiting_revision + 1(= 현재 AwaitingAnswer의 Request revision)이다.
    """
    received = QuestionRequest.receive(
        org_id=org_id,
        requester_id="user",
        question="환불 절차가 궁금합니다",
        request_id_factory=lambda: request_id,
        clock=lambda: T0 - timedelta(minutes=20),
        due_at=due_at,
    )
    completion.create(received)
    trigger_ref = _ref("trigger", request_id)
    route = RouteTarget(intent="refund", agent_id=agent_id, requires_approval=requires_approval)
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=route, attempt=1, trigger_key=trigger_ref,
            handling=HandlingAssignment(kind="system", ref=trigger_ref, due_at=due_at),
        ),
        clock=lambda: T0 - timedelta(minutes=10),
    )
    assert completion.compare_and_set(request_id, 0, received, ready)

    enqueue_uow = DurableWorkTicketEnqueueUnitOfWork(
        completion=completion,
        registry=_Registry(owner=owner),
        clock=lambda: T0,
        ticket_id_factory=lambda: ticket_label,
        receipt_id_factory=lambda: f"receipt-{ticket_label}",
    )
    enqueued = enqueue_uow.enqueue(command=DurableWorkTicketEnqueueCommand(request_id, 1, 1))
    return enqueued.ticket_id, route, enqueued.request_revision


def _uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    directory: _Directory | None = None,
    clock: object = lambda: OVERDUE_NOW,
    escalation_sla: timedelta = _SLA,
    item_id_factory: object = None,
    receipt_id_factory: object = None,
    fault_injector: object = None,
) -> DurableDispatchEscalationUnitOfWork:
    return DurableDispatchEscalationUnitOfWork(
        completion=completion,
        directory=directory or _Directory(),
        clock=clock,  # type: ignore[arg-type]
        escalation_sla=escalation_sla,
        item_id_factory=item_id_factory or (lambda: uuid.uuid4().hex),  # type: ignore[arg-type]
        receipt_id_factory=receipt_id_factory or (lambda: uuid.uuid4().hex),  # type: ignore[arg-type]
        fault_injector=fault_injector,  # type: ignore[arg-type]
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


def _item_row(completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str) -> sqlite3.Row | None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        row = tx.execute(
            "SELECT * FROM durable_dispatch_manager_items WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        tx.commit()
        return row


def _receipt_row(completion: SqliteQuestionCompletionUnitOfWork, ticket_id: str) -> sqlite3.Row | None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        row = tx.execute(
            "SELECT r.* FROM durable_dispatch_escalation_receipts r "
            "JOIN durable_dispatch_manager_items i ON i.manager_item_id=r.manager_item_id "
            "WHERE i.ticket_id=?",
            (ticket_id,),
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


_ARTIFACT_TABLES = ("durable_dispatch_manager_items", "durable_dispatch_escalation_receipts")


def _artifact_counts(completion: SqliteQuestionCompletionUnitOfWork) -> dict[str, int]:
    return {table: _count(completion, table) for table in _ARTIFACT_TABLES}


# ---------------------------------------------------------------------------
# ①②③ — 시그니처 red(S5.2/S5.4와 같은 계약). escalate()는 now·due_at·스캔
# 후보 객체 어느 것도 인자로 받지 않고, 생성자는 CentralAuthorizer를 받지 않는다.
# ---------------------------------------------------------------------------


def test_escalate_시그니처는_ticket_id_하나만_받는다() -> None:
    signature = inspect.signature(DurableDispatchEscalationUnitOfWork.escalate)
    assert list(signature.parameters) == ["self", "ticket_id"]
    assert signature.parameters["ticket_id"].kind is inspect.Parameter.KEYWORD_ONLY


def test_생성자에_centralauthorizer와_시간_인자가_없다() -> None:
    params = set(inspect.signature(DurableDispatchEscalationUnitOfWork.__init__).parameters)
    assert "central_authorizer" not in params
    assert "authorizer" not in params
    assert "now" not in params
    assert "due_at" not in params
    assert params == {
        "self",
        "completion",
        "directory",
        "clock",
        "escalation_sla",
        "item_id_factory",
        "receipt_id_factory",
        "fault_injector",
    }


# ---------------------------------------------------------------------------
# 해피 패스 — 한 transaction에 item·receipt·lease release·ticket 종결·Request
# 전이가 함께 커밋된다(⑤⑥ 값도 여기서 1차 고정한다).
# ---------------------------------------------------------------------------


def test_escalate는_한_transaction에_item_receipt_lease_release_ticket_request전이를_함께_커밋한다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-happy")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-happy"
        )
        lease_uow = DurableDispatchLeaseUnitOfWork(
            completion=completion, holder_id="dispatcher-a", clock=lambda: T0, lease_ttl=_LEASE_TTL
        )
        lease_uow.claim(ticket_id=ticket_id)

        directory = _Directory(_MANAGER_REF)
        uow = _uow(completion, directory=directory)
        escalated = uow.escalate(ticket_id=ticket_id)

        assert isinstance(escalated, DurableDispatchEscalated)
        assert escalated.ticket_id == ticket_id
        assert escalated.request_id == request_id
        assert escalated.manager_subject_id == _MANAGER_REF
        assert escalated.request_revision == expected_revision + 1
        assert directory.calls == 1

        ticket_row = _ticket_row(completion, ticket_id)
        assert ticket_row["status"] == "escalated"

        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "released"

        item = _item_row(completion, ticket_id)
        assert item is not None
        assert item["manager_item_id"] == escalated.manager_item_id
        assert item["org_id"] == ORG_ID
        assert item["request_id"] == request_id
        assert item["attempt"] == 1
        # ⑥ — Item awaiting_revision은 ticket 값 그대로다(+1 아님).
        assert item["awaiting_revision"] == ticket_row["awaiting_revision"]
        assert item["route_sha256"] == ticket_row["route_sha256"]
        assert item["owner_subject_id"] == ticket_row["owner_subject_id"]
        assert item["manager_subject_id"] == _MANAGER_REF
        assert item["status"] == "open"
        # ⑤ — observed_due_at은 재조회한 due_at, created_at은 자기 now다.
        assert item["observed_due_at"] == DUE_AT.isoformat(timespec="microseconds")
        assert item["created_at"] == OVERDUE_NOW.isoformat(timespec="microseconds")

        receipt = _receipt_row(completion, ticket_id)
        assert receipt is not None
        assert receipt["receipt_id"] == escalated.receipt_id
        assert receipt["org_id"] == ORG_ID
        assert receipt["request_id"] == request_id
        assert receipt["manager_item_id"] == item["manager_item_id"]
        assert receipt["principal_ref"] == SYSTEM_SUBJECT_REF
        assert receipt["action"] == "work_ticket.escalate"
        # ⑥ — receipt expected_request_revision만 ticket 값 + 1이다(전이 전
        # Request revision).
        assert receipt["expected_request_revision"] == ticket_row["awaiting_revision"] + 1
        assert receipt["expected_request_revision"] == expected_revision
        assert receipt["created_at"] == item["created_at"]

        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingManager)
        assert request.state.item_id == item["manager_item_id"]
        assert request.state.public_kind == "dispatched"
        assert request.state.route == route
        assert request.state.attempt == 1
        assert request.state.handling.kind == "manager_item"
        assert request.state.handling.ref == item["manager_item_id"]
        # 새 SLA는 이월이 아니라 now + escalation_sla다.
        assert request.state.handling.due_at == OVERDUE_NOW + _SLA
        assert request.revision == expected_revision + 1
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑥ — Item awaiting_revision(ticket 값 그대로) / receipt expected_request_
# revision(ticket 값 + 1) 쌍을 해피 패스와 독립적으로 다시 고정한다. 헷갈리기
# 쉬운 지점이고 S5.7이 이 쌍에 의존한다(2026-07-27 구현 교정).
# ---------------------------------------------------------------------------


def test_item_awaiting_revision은_ticket_값_그대로이고_receipt_expected_revision은_1_더한_값이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-revision-pair")
        ticket_id, _route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-revision-pair"
        )
        ticket_row = _ticket_row(completion, ticket_id)
        assert ticket_row["awaiting_revision"] == expected_revision - 1

        uow = _uow(completion)
        uow.escalate(ticket_id=ticket_id)

        item = _item_row(completion, ticket_id)
        receipt = _receipt_row(completion, ticket_id)
        assert item is not None
        assert receipt is not None
        assert item["awaiting_revision"] == ticket_row["awaiting_revision"]
        assert item["awaiting_revision"] != receipt["expected_request_revision"]
        assert receipt["expected_request_revision"] == ticket_row["awaiting_revision"] + 1
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ④/⑭ — 같은 계약의 양면(ADR 0066 §5.3 셋째 근거). SLA 재판정이 transaction
# 안에서 실제로 지켜지지 않으면 write 0이고(④), 재판정이 실제로 지켜졌을
# 때만 그 뒤 도착한 늦은 답을 S5.4가 거부하는 것이 정당하다(⑭).
# ---------------------------------------------------------------------------


def test_sla가_아직_지나지_않으면_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-not-yet-due")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-not-yet-due"
        )
        uow = _uow(completion, clock=lambda: NOT_YET_DUE_NOW)
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchEscalationConflict):
            uow.escalate(ticket_id=ticket_id)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
        assert _lease_row(completion, ticket_id) is None
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
    finally:
        completion.close()


def test_escalate_후_s4_6_게이트가_green이다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    try:
        request_id = _ref("request", "r-s4-6-gate")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-s4-6-gate"
        )
        uow = _uow(completion)
        uow.escalate(ticket_id=ticket_id)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path)
    assert report.capable is True
    assert report.violations == ()


def test_escalate_후_뒤늦은_답_제출은_conflict이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-late-answer")
        ticket_id, route, expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-late-answer"
        )
        uow = _uow(completion)
        uow.escalate(ticket_id=ticket_id)

        answer_uow = DurableAnswerIngestionUnitOfWork(
            completion=completion,
            clock=lambda: OVERDUE_NOW,
            receipt_id_factory=lambda: uuid.uuid4().hex,
        )
        handoff = FinalizationCandidate(
            request_id=request_id,
            expected_revision=expected_revision,
            attempt=1,
            route=route,
            candidate=AnswerCandidate(text="늦게 도착한 답", sources=("환불정책.md",), mode="full"),
            approval_evaluation=NoApprovalRequired(policy_version="v1"),
        )
        command = DurableWorkerAnswerCommand(
            ticket_id=ticket_id,
            request_id=request_id,
            expected_request_revision=expected_revision,
            handoff=handoff,
        )
        submitter = WorkerConnectionPrincipal(
            org_id=ORG_ID,
            owner_id="owner-a",
            credential_id="cred-1",
            credential_generation=1,
            role="primary",  # type: ignore[arg-type]
            connection_epoch="epoch-1",
        )

        with pytest.raises(DurableAnswerIngestionConflict):
            answer_uow.accept(submitter=submitter, command=command)

        assert _ticket_row(completion, ticket_id)["status"] == "escalated"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑧ — ticket이 이미 'pending'이 아니면(다른 하위 시스템이 독립적으로 종결)
# Request는 여전히 정상 AwaitingAnswer라도 Conflict·write0이다.
# ---------------------------------------------------------------------------


def test_ticket_status가_pending이_아니면_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-ticket-status")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-status-only"
        )
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_work_tickets SET status='completed' WHERE ticket_id=?",
                (ticket_id,),
            )
            tx.commit()

        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchEscalationConflict):
            uow.escalate(ticket_id=ticket_id)

        assert _artifact_counts(completion) == before
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
    finally:
        completion.close()


def test_존재하지_않는_ticket_id는_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        uow = _uow(completion)
        before = _artifact_counts(completion)
        with pytest.raises(DurableDispatchEscalationConflict):
            uow.escalate(ticket_id=_ref("ticket", "does-not-exist"))
        assert _artifact_counts(completion) == before
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑨ — lease release rowcount 0은 정상이다: lease 부재·활성·만료·clock skew
# 전부에서 escalate는 성공하고, clock skew일 때는 release를 건너뛰어 lease
# 행 불변식(expires_at >= acquired_at)을 지킨다(S5.4와 동형 CAS 가드).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lease_kind", ["none", "active", "expired"])
def test_lease_상태와_무관하게_escalate가_성공한다(tmp_path: Path, lease_kind: str) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r-lease-{lease_kind}")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label=f"ticket-lease-{lease_kind}"
        )
        if lease_kind == "active":
            lease_uow = DurableDispatchLeaseUnitOfWork(
                completion=completion, holder_id="dispatcher-a", clock=lambda: T0, lease_ttl=_LEASE_TTL
            )
            lease_uow.claim(ticket_id=ticket_id)
        elif lease_kind == "expired":
            earlier = T0 - timedelta(hours=1)
            lease_uow = DurableDispatchLeaseUnitOfWork(
                completion=completion, holder_id="dispatcher-a", clock=lambda: earlier, lease_ttl=_LEASE_TTL
            )
            lease_uow.claim(ticket_id=ticket_id)

        uow = _uow(completion)
        escalated = uow.escalate(ticket_id=ticket_id)

        assert escalated.ticket_id == ticket_id
        assert _ticket_row(completion, ticket_id)["status"] == "escalated"
        if lease_kind == "none":
            assert _lease_row(completion, ticket_id) is None
        else:
            lease_row = _lease_row(completion, ticket_id)
            assert lease_row is not None
            assert lease_row["state"] == "released"
    finally:
        completion.close()


def test_clock이_lease_acquired_at보다_과거여도_escalate는_성공하고_lease_행_불변식이_유지된다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-lease-skew")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-lease-skew"
        )
        lease_uow = DurableDispatchLeaseUnitOfWork(
            completion=completion, holder_id="dispatcher-a", clock=lambda: OVERDUE_NOW, lease_ttl=_LEASE_TTL
        )
        lease_uow.claim(ticket_id=ticket_id)
        before_lease = _lease_row(completion, ticket_id)
        assert before_lease is not None

        skewed_now = OVERDUE_NOW - timedelta(minutes=30)  # acquired_at(OVERDUE_NOW)보다 과거
        assert skewed_now > DUE_AT  # 이 테스트가 겨냥하는 것은 SLA 재판정이 아니라 lease 하한 가드다
        uow = _uow(completion, clock=lambda: skewed_now)

        escalated = uow.escalate(ticket_id=ticket_id)

        assert escalated.ticket_id == ticket_id
        assert _ticket_row(completion, ticket_id)["status"] == "escalated"

        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"  # release가 건너뛰어졌다
        assert lease_row["expires_at"] == before_lease["expires_at"]
        assert lease_row["acquired_at"] == before_lease["acquired_at"]
        assert lease_row["expires_at"] >= lease_row["acquired_at"]
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑩ — manager 해석 None·예외는 둘 다 Conflict·write0이며 Request는
# AwaitingAnswer에 남는다(미아 없음 — 다음 run이 재시도).
# ---------------------------------------------------------------------------


def test_manager_해석이_none이면_conflict이고_write0이며_request는_그대로다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-no-manager")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-no-manager"
        )
        uow = _uow(completion, directory=_Directory(None))
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchEscalationConflict):
            uow.escalate(ticket_id=ticket_id)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
    finally:
        completion.close()


def test_manager_해석이_예외를_던지면_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-manager-raises")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-manager-raises"
        )
        uow = _uow(completion, directory=_Directory(None, raises=True))
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchEscalationConflict):
            uow.escalate(ticket_id=ticket_id)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑪ — escalation_sla 경계: 0·30일+1초는 생성자에서 Unavailable, 30일 정확히는
# 허용되고 그 값이 그대로 due_at에 반영된다(이월이 아니다).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_sla", [timedelta(0), timedelta(days=30) + timedelta(seconds=1), timedelta(days=-1)]
)
def test_escalation_sla_경계_밖이면_생성자에서_unavailable이다(
    tmp_path: Path, bad_sla: timedelta
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        with pytest.raises(DurableDispatchEscalationUnavailable):
            _uow(completion, escalation_sla=bad_sla)
    finally:
        completion.close()


def test_escalation_sla_30일_정확히는_허용되고_due_at에_그대로_반영된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-sla-30")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-sla-30"
        )
        uow = _uow(completion, escalation_sla=timedelta(days=30))
        uow.escalate(ticket_id=ticket_id)

        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingManager)
        assert request.state.handling.due_at == OVERDUE_NOW + timedelta(days=30)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑫ — replay: 같은 ticket 재호출은 같은 결과를 write 0으로 돌려준다.
# ---------------------------------------------------------------------------


def test_같은_ticket_재호출은_replay되고_write가_늘지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-replay")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-replay"
        )
        uow = _uow(completion)
        first = uow.escalate(ticket_id=ticket_id)
        before = _artifact_counts(completion)

        second = uow.escalate(ticket_id=ticket_id)

        assert second == first
        assert _artifact_counts(completion) == before
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑬ — fault 5지점: 각 지점에서 item·receipt·lease release·ticket 종결·Request
# 전이가 전부 롤백된다(부분 쓰기 0).
# ---------------------------------------------------------------------------


_FAULT_POINTS = (
    "after_manager_item_insert",
    "after_escalation_receipt_insert",
    "after_lease_release",
    "after_ticket_escalated",
    "after_request_transition",
)


@pytest.mark.parametrize("point", _FAULT_POINTS)
def test_fault_지점마다_부분_쓰기가_0이다(tmp_path: Path, point: str) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r-fault-{point}")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label=f"ticket-fault-{point}"
        )
        lease_uow = DurableDispatchLeaseUnitOfWork(
            completion=completion, holder_id="dispatcher-a", clock=lambda: T0, lease_ttl=_LEASE_TTL
        )
        lease_uow.claim(ticket_id=ticket_id)

        def raise_at(injected: str) -> None:
            if injected == point:
                raise RuntimeError(injected)

        uow = _uow(completion, fault_injector=raise_at)
        before = _artifact_counts(completion)

        with pytest.raises(RuntimeError, match=point):
            uow.escalate(ticket_id=ticket_id)

        assert _artifact_counts(completion) == before
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
        lease_row = _lease_row(completion, ticket_id)
        assert lease_row is not None
        assert lease_row["state"] == "leased"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingAnswer)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑦ — ADR 0066 결정 §1의 핵심 양성 단언: "실행 시도마다 최대 한 번"이지
# "평생 한 번"이 아니다. attempt 1이 escalate된 뒤 처분(S5.6c, 이 슬라이스
# 밖)이 새 attempt를 열었다고 가정하고 Request를 직접 ReadyToDispatch
# (attempt=2)로 되돌려 다시 dispatch·timeout·escalate 한 바퀴를 돈다.
#
# 같은 (request_id, attempt) 쌍의 두 번째 진입 거부는 이 UoW의 escalate()
# 경로로는 재현할 수 없다 — Request가 AwaitingAnswer(ticket_id=<특정
# ticket>)로 정확히 한 ticket에만 결박되므로, 같은 attempt를 가리키는 두
# 번째 ticket이 이 경로에 들어오려면 그 결박 자체가 먼저 깨져야 한다(그러면
# escalate()는 UNIQUE 위반이 아니라 그보다 앞선 "Request가 유효하지 않다"
# Conflict로 먼저 닫힌다). 그 UNIQUE(request_id, attempt) 제약은 방어
# 깊이(defense-in-depth)이며, raw SQL로 직접 위반을 시도하는 형태는 S5.6a
# 스키마 테스트(test_unique_request_attempt_rejects_duplicate_attempt_for_
# same_request)가 이미 고정했다.
# ---------------------------------------------------------------------------


def test_같은_request의_다른_attempt는_각각_독립적으로_escalate된다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-attempt-2")
        ticket_1, route, _rev = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-attempt-2-a"
        )
        uow_1 = _uow(completion, clock=lambda: OVERDUE_NOW)
        escalated_1 = uow_1.escalate(ticket_id=ticket_1)

        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingManager)
        trigger_ref_2 = _ref("trigger", f"{request_id}-2")
        rerouted = request.transition(
            ReadyToDispatch(
                route=route,
                attempt=2,
                trigger_key=trigger_ref_2,
                handling=HandlingAssignment(kind="system", ref=trigger_ref_2, due_at=DUE_AT_2),
            ),
            clock=lambda: OVERDUE_NOW,
        )
        assert completion.compare_and_set(request_id, request.revision, request, rerouted)

        enqueue_uow = DurableWorkTicketEnqueueUnitOfWork(
            completion=completion,
            registry=_Registry(owner=_TICKET_OWNER),
            clock=lambda: OVERDUE_NOW,
            ticket_id_factory=lambda: "ticket-attempt-2-b",
            receipt_id_factory=lambda: "receipt-attempt-2-b",
        )
        enqueued = enqueue_uow.enqueue(
            command=DurableWorkTicketEnqueueCommand(request_id, rerouted.revision, 2)
        )

        uow_2 = _uow(completion, clock=lambda: OVERDUE_NOW_2)
        escalated_2 = uow_2.escalate(ticket_id=enqueued.ticket_id)

        assert escalated_2.request_id == request_id
        assert escalated_1.manager_item_id != escalated_2.manager_item_id
        item_1 = _item_row(completion, ticket_1)
        item_2 = _item_row(completion, enqueued.ticket_id)
        assert item_1 is not None
        assert item_2 is not None
        assert item_1["attempt"] == 1
        assert item_2["attempt"] == 2
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑯ — S4.1 `_validate_rows`의 ticket 규칙을 전수 대조한 결론: 이 write는
# `status` 컬럼 하나만 SET한다. 다른 컬럼이 조금이라도 바뀌면 이 테스트가
# 잡는다(문서 주장이 아니라 값으로 고정한 회귀).
# ---------------------------------------------------------------------------


def test_ticket_escalate_write는_status_컬럼만_바꾼다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-ticket-untouched")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-untouched"
        )
        before = _ticket_row(completion, ticket_id)

        uow = _uow(completion)
        uow.escalate(ticket_id=ticket_id)

        after = _ticket_row(completion, ticket_id)
        assert after["status"] == "escalated"
        for column in (
            "ticket_id",
            "org_id",
            "request_id",
            "attempt",
            "awaiting_revision",
            "route_sha256",
            "owner_subject_id",
            "created_at",
        ):
            assert after[column] == before[column], column
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# 타입 검사 — ticket_id 형식.
# ---------------------------------------------------------------------------


def test_ticket_id가_blank이면_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        uow = _uow(completion)
        with pytest.raises(DurableDispatchEscalationUnavailable):
            uow.escalate(ticket_id="   ")
    finally:
        completion.close()


def test_ticket_id가_str이_아니면_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        uow = _uow(completion)
        with pytest.raises(DurableDispatchEscalationUnavailable):
            uow.escalate(ticket_id=None)  # type: ignore[arg-type]
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# capability — 이 모듈 자기 escalation schema 손상은 자기 Unavailable/Busy로,
# S5.1 dispatch delivery capability 손상은 S5.2가 이미 확립한 Lease 계열로
# wrap 없이 관측된다(ctor·escalate() 양쪽 진입점 모두).
# ---------------------------------------------------------------------------


def test_escalation_schema_손상은_생성자에서_자기_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute("DROP TABLE durable_dispatch_escalation_receipts")
            tx.commit()

        with pytest.raises(DurableDispatchEscalationUnavailable) as excinfo:
            _uow(completion)
        assert type(excinfo.value) is DurableDispatchEscalationUnavailable
    finally:
        completion.close()


def test_dispatch_delivery_schema_손상은_생성자에서_남의_lease_unavailable이고_wrap되지_않는다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute("DROP TABLE durable_dispatch_leases")
            tx.commit()

        with pytest.raises(DurableDispatchLeaseUnavailable) as excinfo:
            _uow(completion)
        assert type(excinfo.value) is DurableDispatchLeaseUnavailable
        assert not isinstance(excinfo.value, DurableDispatchEscalationError)
    finally:
        completion.close()


def test_escalation_schema_손상_후_escalate호출은_자기_unavailable이고_write0이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-cap-own")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-cap-own"
        )
        uow = _uow(completion)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute("DROP TABLE durable_dispatch_escalation_receipts")
            tx.commit()

        with pytest.raises(DurableDispatchEscalationUnavailable):
            uow.escalate(ticket_id=ticket_id)
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
    finally:
        completion.close()


def test_dispatch_delivery_schema_손상_후_escalate호출은_남의_lease_unavailable이고_write0이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-cap-dispatch")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-cap-dispatch"
        )
        uow = _uow(completion)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute("DROP TABLE durable_dispatch_leases")
            tx.commit()

        with pytest.raises(DurableDispatchLeaseUnavailable) as excinfo:
            uow.escalate(ticket_id=ticket_id)
        assert not isinstance(excinfo.value, DurableDispatchEscalationError)
        assert _ticket_row(completion, ticket_id)["status"] == "pending"
    finally:
        completion.close()


def test_다른_connection이_lock을_쥐면_escalate는_durabledispatchescalationbusy이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path, timeout=0.2)
    try:
        request_id = _ref("request", "r-busy")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-busy"
        )
        uow = _uow(completion)

        blocker = sqlite3.connect(str(path), timeout=1.0)
        blocker.execute("PRAGMA foreign_keys=ON")
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(DurableDispatchEscalationBusy) as excinfo:
                uow.escalate(ticket_id=ticket_id)
            assert type(excinfo.value) is DurableDispatchEscalationBusy
        finally:
            blocker.rollback()
            blocker.close()

        assert _artifact_counts(completion) == {table: 0 for table in _ARTIFACT_TABLES}
        assert _ticket_row(completion, ticket_id)["status"] == "pending"

        # lock 해제 뒤 재실행은 정상이다(영구 잠금이 아니다).
        escalated = uow.escalate(ticket_id=ticket_id)
        assert escalated.ticket_id == ticket_id
    finally:
        completion.close()


def test_에러_계층이_올바르다() -> None:
    assert issubclass(DurableDispatchEscalationBusy, DurableDispatchEscalationUnavailable)
    assert issubclass(DurableDispatchEscalationUnavailable, DurableDispatchEscalationError)
    assert issubclass(DurableDispatchEscalationConflict, DurableDispatchEscalationError)
    assert not issubclass(DurableDispatchEscalationConflict, DurableDispatchEscalationUnavailable)
    assert not issubclass(DurableDispatchLeaseUnavailable, DurableDispatchEscalationError)
    assert not issubclass(DurableDispatchEscalationError, DurableDispatchLeaseError)


# ---------------------------------------------------------------------------
# 남의 계열(§9 ⑮와 같은 규율의 확장) — 이 모듈이 알지 못하는 저장소 손상
# (여기서는 `sqlite_stores`의 QuestionRequest row 손상)은 자기 계열로 감싸지
# 않고 그대로 통과한다. `_reraise_write`의 마지막 `else: raise error`가
# 겨냥하는 지점이다.
# ---------------------------------------------------------------------------


def test_request_state_json이_손상되면_남의_계열이_wrap_없이_통과한다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-corrupt-state")
        ticket_id, _route, _expected_revision = _seed_awaiting_answer(
            completion, request_id=request_id, ticket_label="ticket-corrupt-state"
        )
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE question_requests SET state_json='not-json' WHERE request_id=?",
                (request_id,),
            )
            tx.commit()

        uow = _uow(completion)
        with pytest.raises(CorruptQuestionRequestError) as excinfo:
            uow.escalate(ticket_id=ticket_id)
        assert not isinstance(excinfo.value, DurableDispatchEscalationError)
    finally:
        completion.close()
