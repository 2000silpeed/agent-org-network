from __future__ import annotations
# pyright: reportArgumentType=false

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingManager,
    DeclinedRequest,
    FailedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    migrate_sqlite_durable_conflict_escalation_baseline_schema,
)
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    migrate_sqlite_durable_conflict_escalation_receipts_schema,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)
from agent_org_network.sqlite_durable_linked_reconciliation import (
    DurableLinkedReconciliationReport,
    reconcile_sqlite_durable_linked_gate,
)
from agent_org_network.sqlite_durable_manager_disposition_uow import (
    DurableManagerAssignCommand,
    DurableManagerAssignTarget,
    DurableManagerDismissCommand,
    DurableManagerDismissed,
    DurableManagerDispositionUnitOfWork,
    DurableManagerOwnerAssigned,
    DurableManagerRegistry,
)
from agent_org_network.sqlite_durable_work_ticket_uow import (
    SYSTEM_SUBJECT_REF,
    DurableWorkTicketEnqueueCommand,
    DurableWorkTicketEnqueued,
    DurableWorkTicketEnqueueUnitOfWork,
    DurableWorkTicketRegistry,
)

NOW = datetime(2026, 7, 24, tzinfo=UTC)
_TYPED_REF_RE = re.compile(r"^(receipt|manager|ticket):[0-9a-f]{64}$")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


def _route_sha256(route: object) -> str:
    """production ``_route_sha256``와 동일한 canonical hash(재시도 ticket-2
    row를 raw seed하려면 같은 결정론적 값이 필요하다)."""
    return hashlib.sha256(
        json.dumps(
            {
                "agent_id": route.agent_id,  # type: ignore[attr-defined]
                "authority_version": route.authority_version,  # type: ignore[attr-defined]
                "intent": route.intent,  # type: ignore[attr-defined]
                "requires_approval": route.requires_approval,  # type: ignore[attr-defined]
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


ORG_ID = _ref("org", "org-1")
REQUEST_ID = _ref("request", "request-1")
ITEM_REF = _ref("manager", "item-1")
MANAGER_SUBJECT_REF = _ref("subject", "manager-1")
_DEFAULT_OWNER = _ref("subject", "owner-a")


class _ManagerAuthority:
    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
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
        return True


class _NoneRegistry:
    """Dismiss 전용 — Assign 대상 결선 불요."""

    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None:
        return None


class _AssignRegistry:
    def __init__(self, *, agent_id: str) -> None:
        self._agent_id = agent_id

    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None:
        return DurableManagerAssignTarget(
            agent_id=self._agent_id,
            owner_subject_ref=_ref("subject", f"owner-{self._agent_id}"),
            requires_approval=False,
        )


class _OwnerRegistry:
    def __init__(self, *, owner: str = _DEFAULT_OWNER) -> None:
        self._owner = owner

    def resolve_owner_subject(self, *, org_id: str, agent_id: str) -> str | None:
        return self._owner


def _manager_principal(org_id: str = ORG_ID, subject_id: str = "manager-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=org_id, subject_id=subject_id, identity_provider="idp", identity_session_id="s1"
    )


def _open_completion(path: Path) -> SqliteQuestionCompletionUnitOfWork:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    # Disposition UoW 생성자가 방어적으로 escalation receipts capability도
    # 검증하므로(FromUnowned 경로에서도) 이 schema가 있어야 한다.
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    return SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: NOW,
    )


def _seed_unowned(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    item_id: str = ITEM_REF,
    request_id: str = REQUEST_ID,
    org_id: str = ORG_ID,
    manager_subject_ref: str = MANAGER_SUBJECT_REF,
    intent: str = "refund",
) -> None:
    """FromUnowned seed: Received rev0 → AwaitingManager(unowned) rev1·item awaiting_revision=0."""
    received = QuestionRequest.receive(
        org_id=org_id,
        requester_id="user",
        question="refund question",
        request_id_factory=lambda: request_id,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(received)
    unowned = received.record_initial_routing(
        intent=intent,
        disposition="unowned",
        target=AwaitingManager(
            item_id=item_id,
            public_kind="unowned",
            handling=HandlingAssignment(
                kind="manager_item", ref=item_id, due_at=NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set(request_id, 0, received, unowned)
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "INSERT INTO durable_linked_manager_items VALUES(?,?,?,?,?,?,?,?,?)",
            (
                item_id,
                org_id,
                request_id,
                0,
                "unowned",
                _ref("source", request_id),
                manager_subject_ref,
                "open",
                NOW.isoformat(),
            ),
        )
        tx.commit()


def _prepared(tmp_path: Path, *, name: str = "workflow.sqlite") -> tuple[Path, SqliteQuestionCompletionUnitOfWork]:
    path = tmp_path / name
    completion = _open_completion(path)
    _seed_unowned(completion)
    return path, completion


def _dismiss_uow(
    completion: SqliteQuestionCompletionUnitOfWork, *, receipt_id: str = "receipt-dismiss-1"
) -> DurableManagerDispositionUnitOfWork:
    return DurableManagerDispositionUnitOfWork(
        completion=completion,
        registry=_NoneRegistry(),
        central_authorizer=_ManagerAuthority(),
        clock=lambda: NOW,
        receipt_id_factory=lambda: receipt_id,
    )


def _assign_uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    agent_id: str = "card-a",
    receipt_id: str = "receipt-assign-1",
    registry: DurableManagerRegistry | None = None,
) -> DurableManagerDispositionUnitOfWork:
    return DurableManagerDispositionUnitOfWork(
        completion=completion,
        registry=registry or _AssignRegistry(agent_id=agent_id),
        central_authorizer=_ManagerAuthority(),
        clock=lambda: NOW,
        receipt_id_factory=lambda: receipt_id,
    )


def _enqueue_uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    ticket_id: str = "ticket-1",
    receipt_id: str = "receipt-ticket-1",
    registry: DurableWorkTicketRegistry | None = None,
) -> DurableWorkTicketEnqueueUnitOfWork:
    return DurableWorkTicketEnqueueUnitOfWork(
        completion=completion,
        registry=registry or _OwnerRegistry(),
        clock=lambda: NOW,
        ticket_id_factory=lambda: ticket_id,
        receipt_id_factory=lambda: receipt_id,
    )


def _raw_execute(
    completion: SqliteQuestionCompletionUnitOfWork, sql: str, params: tuple[object, ...]
) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(sql, params)
        tx.commit()


def _canonical_state_json(state: object) -> str:
    return json.dumps(
        state.model_dump(mode="json"),  # type: ignore[attr-defined]
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _force_request_state(
    completion: SqliteQuestionCompletionUnitOfWork,
    request_id: str,
    state: object,
    *,
    revision: int | None = None,
) -> None:
    """CAS를 우회한 raw UPDATE — read-only reconciliation 손상/진행 시나리오
    시뮬레이션 전용(정상 경로는 항상 ``QuestionRequest.transition``만 쓴다)."""
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        params: list[object] = [state.kind, _canonical_state_json(state)]  # type: ignore[attr-defined]
        sql = "UPDATE question_requests SET state_kind=?, state_json=?"
        if revision is not None:
            sql += ", revision=?"
            params.append(revision)
        sql += " WHERE request_id COLLATE BINARY=?"
        params.append(request_id)
        tx.execute(sql, tuple(params))
        tx.commit()


def _assign(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    item_id: str = ITEM_REF,
    request_id: str = REQUEST_ID,
    org_id: str = ORG_ID,
    subject_id: str = "manager-1",
    agent_id: str = "card-a",
    receipt_id: str = "receipt-assign-1",
) -> DurableManagerOwnerAssigned:
    outcome = _assign_uow(completion, agent_id=agent_id, receipt_id=receipt_id).act(
        principal=_manager_principal(org_id, subject_id),
        command=DurableManagerAssignCommand(item_id, request_id, agent_id, 1),
    )
    assert isinstance(outcome, DurableManagerOwnerAssigned)
    return outcome


def _dismiss(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    item_id: str = ITEM_REF,
    request_id: str = REQUEST_ID,
    org_id: str = ORG_ID,
    subject_id: str = "manager-1",
    receipt_id: str = "receipt-dismiss-1",
) -> DurableManagerDismissed:
    outcome = _dismiss_uow(completion, receipt_id=receipt_id).act(
        principal=_manager_principal(org_id, subject_id),
        command=DurableManagerDismissCommand(item_id, request_id, 1),
    )
    assert isinstance(outcome, DurableManagerDismissed)
    return outcome


def _enqueue(
    completion: SqliteQuestionCompletionUnitOfWork,
    assigned: DurableManagerOwnerAssigned,
    *,
    request_id: str = REQUEST_ID,
    ticket_id: str = "ticket-1",
    receipt_id: str = "receipt-ticket-1",
) -> DurableWorkTicketEnqueued:
    outcome = _enqueue_uow(completion, ticket_id=ticket_id, receipt_id=receipt_id).enqueue(
        command=DurableWorkTicketEnqueueCommand(request_id, assigned.request_revision, 1)
    )
    assert isinstance(outcome, DurableWorkTicketEnqueued)
    return outcome


# ---------------------------------------------------------------------------
# 1 — green baseline 3종(S4.4/S4.5 UoW 실 커밋)
# ---------------------------------------------------------------------------


def test_baseline_dismiss_커밋은_capable하고_violation이_없다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _dismiss(completion)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert isinstance(report, DurableLinkedReconciliationReport)
    assert report.capable is True
    assert report.detail == "capable_v1"
    assert report.linked_aggregates_manifest_present is True
    assert report.violations == ()


def test_baseline_assign_커밋은_capable하고_ready_to_dispatch_resting_shape가_정확하다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _assign(completion)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is True
    assert report.violations == ()


def test_baseline_assign_후_enqueue_커밋은_capable하고_두_receipt_모두_유효하다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        _enqueue(completion, assigned)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is True
    assert report.violations == ()


# ---------------------------------------------------------------------------
# 2 — forward 위반
# ---------------------------------------------------------------------------


def test_처분된_manageritem이_open으로_되돌아가면_manager_disposition_receipt_mismatch이다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _assign(completion)
        _raw_execute(
            completion,
            "UPDATE durable_linked_manager_items SET status='open' WHERE manager_item_id=?",
            (ITEM_REF,),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "manager_disposition_receipt_mismatch" for v in report.violations)


def test_처분된_manageritem이_삭제되면_manager_disposition_receipt_mismatch이다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _dismiss(completion)
        _raw_execute(
            completion,
            "DELETE FROM durable_linked_manager_items WHERE manager_item_id=?",
            (ITEM_REF,),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "manager_disposition_receipt_mismatch" for v in report.violations)


def test_ticket_awaiting_revision이_어긋나면_work_ticket_receipt_mismatch이다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        ticket = _enqueue(completion, assigned)
        _raw_execute(
            completion,
            "UPDATE durable_linked_work_tickets SET awaiting_revision=99 WHERE ticket_id=?",
            (ticket.ticket_id,),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "work_ticket_receipt_mismatch" for v in report.violations)


def test_ticket가_삭제되면_work_ticket_receipt_mismatch이다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        ticket = _enqueue(completion, assigned)
        _raw_execute(
            completion, "DELETE FROM durable_linked_work_tickets WHERE ticket_id=?", (ticket.ticket_id,)
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "work_ticket_receipt_mismatch" for v in report.violations)


def test_assign_이후_request의_trigger_key가_어긋나면_request_state_inconsistent이다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _assign(completion)
        request = completion.get(REQUEST_ID)
        assert request is not None and isinstance(request.state, ReadyToDispatch)
        drifted_trigger = _ref("receipt", "drifted-trigger")
        drifted = ReadyToDispatch(
            route=request.state.route,
            attempt=request.state.attempt,
            trigger_key=drifted_trigger,
            handling=HandlingAssignment(
                kind="system", ref=drifted_trigger, due_at=request.state.handling.due_at
            ),
        )
        _force_request_state(completion, REQUEST_ID, drifted)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "request_state_inconsistent" for v in report.violations)


def test_assign_이후_request의_attempt가_1이_아니면_request_state_inconsistent이다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _assign(completion)
        request = completion.get(REQUEST_ID)
        assert request is not None and isinstance(request.state, ReadyToDispatch)
        drifted = ReadyToDispatch(
            route=request.state.route,
            attempt=2,
            trigger_key=request.state.trigger_key,
            handling=request.state.handling,
        )
        _force_request_state(completion, REQUEST_ID, drifted)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "request_state_inconsistent" for v in report.violations)


def test_enqueue_이후_request의_ticket_id가_어긋나면_request_state_inconsistent이다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        _enqueue(completion, assigned)
        request = completion.get(REQUEST_ID)
        assert request is not None and isinstance(request.state, AwaitingAnswer)
        drifted_ticket = _ref("ticket", "drifted-ticket")
        drifted = AwaitingAnswer(
            route=request.state.route,
            attempt=request.state.attempt,
            ticket_id=drifted_ticket,
            handling=HandlingAssignment(
                kind="runtime_ticket", ref=drifted_ticket, due_at=request.state.handling.due_at
            ),
        )
        _force_request_state(completion, REQUEST_ID, drifted)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "request_state_inconsistent" for v in report.violations)


def test_dismiss_이후_request가_다른_terminal로_변조되면_request_state_inconsistent이다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _dismiss(completion)
        _force_request_state(completion, REQUEST_ID, FailedRequest(error_code="drift"))
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "request_state_inconsistent" for v in report.violations)


def test_action이_결박_불가_조합이면_unbindable_command_receipt이다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        _raw_execute(
            completion,
            "UPDATE durable_linked_command_receipts SET action='manager.reroute' WHERE receipt_id=?",
            (assigned.receipt_id,),
        )
        _raw_execute(
            completion,
            "UPDATE durable_linked_audit_intents SET action='manager.reroute' WHERE receipt_id=?",
            (assigned.receipt_id,),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "unbindable_command_receipt" for v in report.violations)


# ---------------------------------------------------------------------------
# 3 — backward(aggregate-anchored) partial state
# ---------------------------------------------------------------------------


def test_처분된_manageritem에_receipt가_없으면_disposed_item_without_receipt이다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        for table in (
            "durable_linked_audit_intents",
            "durable_linked_outbox_intents",
            "durable_linked_command_receipts",
        ):
            _raw_execute(completion, f"DELETE FROM {table} WHERE receipt_id=?", (assigned.receipt_id,))
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "disposed_item_without_receipt" for v in report.violations)


def test_receipt_없는_workticket은_work_ticket_without_receipt이다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _assign(completion)
        ghost_ticket = _ref("ticket", "ghost-1")
        _raw_execute(
            completion,
            "INSERT INTO durable_linked_work_tickets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ghost_ticket,
                ORG_ID,
                REQUEST_ID,
                1,
                2,
                "1" * 64,
                _ref("subject", "owner-x"),
                "pending",
                NOW.isoformat(),
            ),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "work_ticket_without_receipt" for v in report.violations)


def test_open_manageritem은_receipt_없어도_통과한다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is True
    assert report.violations == ()


# ---------------------------------------------------------------------------
# 4 — downstream tolerance(핵심)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["completed", "escalated"])
def test_ticket_status가_진행해도_capable하다(tmp_path: Path, status: str) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        ticket = _enqueue(completion, assigned)
        _raw_execute(
            completion,
            "UPDATE durable_linked_work_tickets SET status=? WHERE ticket_id=?",
            (status, ticket.ticket_id),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is True
    assert report.violations == ()


def test_request가_answered로_진행해도_assign_ticket_receipt는_관용된다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        _enqueue(completion, assigned)
        # revision-anchored 판별자는 revision이 실제로 진행해야 tolerant다 —
        # kind만 바뀌고 revision이 그대로면 오히려 손상(다른 red가 이를 검증).
        _force_request_state(
            completion, REQUEST_ID, AnsweredRequest(record_id="record-answer-1"), revision=4
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is True
    assert report.violations == ()


# ---------------------------------------------------------------------------
# 5 — capability fail-closed(row 열거 0)
# ---------------------------------------------------------------------------


def test_manifest가_없으면_capability_uncertain으로_row_열거_0으로_닫는다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _assign(completion)
    finally:
        completion.close()

    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "DELETE FROM schema_component_manifests WHERE component_id='durable_linked_aggregates_v1'"
        )
        connection.commit()
    finally:
        connection.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert len(report.violations) == 1
    assert report.violations[0].kind == "linked_capability_uncertain"


# ---------------------------------------------------------------------------
# 6 — org-scope
# ---------------------------------------------------------------------------


def test_타org_손상은_격리되고_전역_catalog_손상만_org_무관하게_닫힌다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_completion(path)
    org_b = _ref("org", "org-2")
    request_b = _ref("request", "request-2")
    item_b = _ref("manager", "item-2")
    manager_b = _ref("subject", "manager-2")
    try:
        _seed_unowned(completion)
        _seed_unowned(
            completion,
            item_id=item_b,
            request_id=request_b,
            org_id=org_b,
            manager_subject_ref=manager_b,
        )
        _assign(completion)
        assigned_b = _assign(
            completion,
            item_id=item_b,
            request_id=request_b,
            org_id=org_b,
            subject_id="manager-2",
            agent_id="card-b",
            receipt_id="receipt-assign-b",
        )
        _raw_execute(
            completion,
            "UPDATE durable_linked_manager_items SET status='open' WHERE manager_item_id=?",
            (item_b,),
        )
        assert assigned_b.item_id == item_b
    finally:
        completion.close()

    scoped_a = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert scoped_a.capable is True
    assert scoped_a.violations == ()

    scoped_b = reconcile_sqlite_durable_linked_gate(path, org_id=org_b)
    assert scoped_b.capable is False
    assert any(v.kind == "manager_disposition_receipt_mismatch" for v in scoped_b.violations)

    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "DELETE FROM schema_component_manifests WHERE component_id='durable_linked_aggregates_v1'"
        )
        connection.commit()
    finally:
        connection.close()

    globally_closed = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert globally_closed.capable is False
    assert globally_closed.violations[0].kind == "linked_capability_uncertain"


# ---------------------------------------------------------------------------
# 7 — snapshot 원자성(한 deferred read transaction)
# ---------------------------------------------------------------------------


def test_전_sweep은_한_read_transaction으로_묶인다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        _enqueue(completion, assigned)
    finally:
        completion.close()

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
        "agent_org_network.sqlite_durable_linked_reconciliation.sqlite3.connect", _spy_connect
    )

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is True

    begin_indices = [i for i, s in enumerate(statements) if s == "BEGIN"]
    commit_indices = [i for i, s in enumerate(statements) if s == "COMMIT"]
    assert len(begin_indices) == 1
    assert len(commit_indices) == 1
    begin_at, commit_at = begin_indices[0], commit_indices[0]
    assert begin_at < commit_at
    assert commit_at == len(statements) - 1
    writes = {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}
    assert not writes.intersection(statements[begin_at + 1 : commit_at])


# ---------------------------------------------------------------------------
# 8 — 노출 불변식(raw claim·User·원문 0)
# ---------------------------------------------------------------------------


def test_violation은_typed_ref와_요약만_담고_raw_원문을_노출하지_않는다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _assign(completion)
        _raw_execute(
            completion,
            "UPDATE durable_linked_manager_items SET status='open' WHERE manager_item_id=?",
            (ITEM_REF,),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert report.violations != ()
    for violation in report.violations:
        assert violation.anchor_ref == "" or _TYPED_REF_RE.fullmatch(violation.anchor_ref)
        assert "card-a" not in violation.detail
        assert "manager-1" not in violation.detail
        assert "refund" not in violation.detail


# ---------------------------------------------------------------------------
# P1 회귀 앵커 — revision-anchored 판별자(kind-only는 WorkTicket 재시도를 오탐한다)
# ---------------------------------------------------------------------------


def test_workticket_재시도_시나리오에서_이전_ticket_receipt는_revision_기준으로_관용된다(
    tmp_path: Path,
) -> None:
    """durable_linked_work_tickets는 UNIQUE(request_id, attempt)라 재시도로
    같은 request에 ticket-1(attempt=1)·ticket-2(attempt=2)가 정상 공존할 수
    있다 — kind-only 판별자(Request.state가 아직 AwaitingAnswer인가)는 이때
    ticket-1의 옛 receipt를 현재 ticket-2를 가리키는 상태와 어긋난다고
    오탐한다. revision은 전이마다 정확히 1씩만 오르므로 "resting
    revision"(expected+1)이 현재 revision과 exact 일치할 때만 그 receipt가
    유일한 주인임이 결정된다 — 그 판별자라야 ticket-1은 관용되고 ticket-2만
    정확히 결박된다."""
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        _enqueue(completion, assigned, ticket_id="ticket-1", receipt_id="receipt-ticket-1")
        request = completion.get(REQUEST_ID)
        assert request is not None and isinstance(request.state, AwaitingAnswer)
        route = request.state.route
        route_sha = _route_sha256(route)
        ticket2_id = _ref("ticket", "ticket-2")
        receipt2_id = _ref("receipt", "receipt-ticket-2")
        # 중간 전이(dispatched AwaitingManager→ReadyToDispatch(attempt=2))는
        # 이 read-only 게이트의 관심사가 아니므로 재현하지 않고, 재시도가
        # 완주한 최종 상태(revision=6에서 ticket-2를 가리킴)만 직접 조립한다.
        retried = AwaitingAnswer(
            route=route,
            attempt=2,
            ticket_id=ticket2_id,
            handling=HandlingAssignment(
                kind="runtime_ticket", ref=ticket2_id, due_at=request.state.handling.due_at
            ),
        )
        _force_request_state(completion, REQUEST_ID, retried, revision=6)
        _raw_execute(
            completion,
            "INSERT INTO durable_linked_work_tickets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ticket2_id,
                ORG_ID,
                REQUEST_ID,
                2,
                5,
                route_sha,
                _ref("subject", "owner-a"),
                "pending",
                NOW.isoformat(),
            ),
        )
        digest2 = _sha("retry-ticket-2-command")
        _raw_execute(
            completion,
            "INSERT INTO durable_linked_command_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                receipt2_id,
                ORG_ID,
                REQUEST_ID,
                digest2,
                SYSTEM_SUBJECT_REF,
                "work_ticket.create",
                5,
                "work_ticket",
                ticket2_id,
                NOW.isoformat(),
            ),
        )
        _raw_execute(
            completion,
            "INSERT INTO durable_linked_audit_intents VALUES(?,?,?,?,?,?)",
            (receipt2_id, ORG_ID, REQUEST_ID, "work_ticket.create", digest2, NOW.isoformat()),
        )
        _raw_execute(
            completion,
            "INSERT INTO durable_linked_outbox_intents VALUES(?,?,?,?,?,?)",
            (receipt2_id, ORG_ID, REQUEST_ID, "linked_aggregate_outbox", digest2, NOW.isoformat()),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is True
    assert report.violations == ()


# ---------------------------------------------------------------------------
# P2 — mutation 갭 5건
# ---------------------------------------------------------------------------


def test_completion_capability가_단독으로_깨지면_row_열거_0으로_닫는다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _assign(completion)
    finally:
        completion.close()

    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "DELETE FROM schema_component_manifests WHERE component_id='question_completion'"
        )
        connection.commit()
    finally:
        connection.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert len(report.violations) == 1
    assert report.violations[0].kind == "linked_capability_uncertain"


def test_ticket_route_sha256이_어긋나면_request_state_inconsistent이다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        ticket = _enqueue(completion, assigned)
        _raw_execute(
            completion,
            "UPDATE durable_linked_work_tickets SET route_sha256=? WHERE ticket_id=?",
            ("f" * 64, ticket.ticket_id),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "request_state_inconsistent" for v in report.violations)


def test_ticket_attempt이_어긋나면_request_state_inconsistent이다(tmp_path: Path) -> None:
    path, completion = _prepared(tmp_path)
    try:
        assigned = _assign(completion)
        ticket = _enqueue(completion, assigned)
        _raw_execute(
            completion,
            "UPDATE durable_linked_work_tickets SET attempt=2 WHERE ticket_id=?",
            (ticket.ticket_id,),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "request_state_inconsistent" for v in report.violations)


def test_assign_request_json의_handling_kind가_pydantic_불변식을_깨면_decode_단계에서_fail_closed이다(
    tmp_path: Path,
) -> None:
    # ReadyToDispatch 모델 validator가 handling.kind/ref를 trigger_key에
    # 항상 tie하므로(_require_handling), 순수 shape 위반으로는 handling.kind만
    # 독립적으로 어긋나는 값을 만들 수 없다 — 그 결합을 깨면 decode 자체가
    # ValidationError로 실패해 이 게이트는 row별 shape violation이 아니라
    # 상위 capability_uncertain으로 fail-closed한다(defense-in-depth).
    path, completion = _prepared(tmp_path)
    try:
        _assign(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            row = tx.execute(
                "SELECT state_json FROM question_requests WHERE request_id COLLATE BINARY=?",
                (REQUEST_ID,),
            ).fetchone()
            payload = json.loads(row["state_json"])
            payload["handling"]["kind"] = "runtime_ticket"
            tx.execute(
                "UPDATE question_requests SET state_json=? WHERE request_id COLLATE BINARY=?",
                (json.dumps(payload), REQUEST_ID),
            )
            tx.commit()
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert len(report.violations) == 1
    assert report.violations[0].kind == "linked_capability_uncertain"


def test_dismiss_이후_request의_reason_code만_어긋나면_request_state_inconsistent이다(
    tmp_path: Path,
) -> None:
    path, completion = _prepared(tmp_path)
    try:
        _dismiss(completion)
        _force_request_state(completion, REQUEST_ID, DeclinedRequest(reason_code="manager_disposed"))
    finally:
        completion.close()

    report = reconcile_sqlite_durable_linked_gate(path, org_id=ORG_ID)
    assert report.capable is False
    assert any(v.kind == "request_state_inconsistent" for v in report.violations)
