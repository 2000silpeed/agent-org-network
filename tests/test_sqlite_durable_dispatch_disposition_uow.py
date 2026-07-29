"""P17.9 S5.6c FromDispatch Manager 처분 UoW 테스트(ADR 0066 §5.2·ADR 0050 §12).

FromDispatch ManagerItem(S5.6b escalate가 만든 open Item)의 Reroute/Dismiss가
한 transaction에 receipt·Item CAS·Request 전이를 함께 커밋함을 검증한다.
S4.4(``sqlite_durable_manager_disposition_uow``)의 동형 골격을 따르되, 중앙
receipt 표는 새로 만들지 않고 S5.6a가 이미 3-action 공용으로 설계해 둔
``durable_dispatch_escalation_receipts``를 재사용한다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.approval import InMemoryApprovalStore, NoApprovalRequired
from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.question_request import (
    AwaitingManager,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_dispatch_delivery import (
    migrate_sqlite_durable_dispatch_delivery_schema,
)
from agent_org_network.sqlite_durable_dispatch_disposition_uow import (
    DurableDispatchDismissCommand,
    DurableDispatchDismissed,
    DurableDispatchDispositionBusy,
    DurableDispatchDispositionConflict,
    DurableDispatchDispositionError,
    DurableDispatchDispositionUnavailable,
    DurableDispatchDispositionUnitOfWork,
    DurableDispatchRerouteCommand,
    DurableDispatchRerouted,
)
from agent_org_network.sqlite_durable_dispatch_escalation import (
    migrate_sqlite_durable_dispatch_escalation_schema,
    reconcile_sqlite_durable_dispatch_escalation_schema,
)
from agent_org_network.sqlite_durable_dispatch_escalation_uow import (
    DurableDispatchEscalated,
    DurableDispatchEscalationError,
    DurableDispatchEscalationUnitOfWork,
)
from agent_org_network.sqlite_durable_dispatch_reconciliation import (
    reconcile_sqlite_durable_dispatch_gate,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)
from agent_org_network.sqlite_durable_linked_reconciliation import (
    reconcile_sqlite_durable_linked_gate,
)
from agent_org_network.sqlite_durable_manager_disposition_uow import (
    DurableManagerAssignTarget,
    DurableManagerRegistry,
)
from agent_org_network.sqlite_durable_work_ticket_uow import (
    DurableWorkTicketEnqueueCommand,
    DurableWorkTicketEnqueueUnitOfWork,
)

# ---------------------------------------------------------------------------
# 시간축 — S5.6b red와 같은 방식. attempt 2 사이클을 위해 escalate 시각을
# 두 벌(1차/2차) 둔다.
# ---------------------------------------------------------------------------

T0 = datetime(2026, 7, 25, tzinfo=UTC)
DUE_AT = T0 + timedelta(minutes=30)
OVERDUE_NOW = T0 + timedelta(hours=2)  # attempt 1 escalate 시각
_ESCALATION_SLA = timedelta(hours=6)  # AwaitingManager.due_at = OVERDUE_NOW + 6h = T0+8h
DISPOSITION_NOW = OVERDUE_NOW + timedelta(hours=1)  # manager act 시각(T0+3h)
# reroute의 새 ReadyToDispatch.handling.due_at은 AwaitingManager의 due_at을
# 그대로 이월한다(S4.4 assign과 동형·모듈 docstring 참조) — 그 값이 그대로
# enqueue를 거쳐 새 AwaitingAnswer의 SLA가 되므로, attempt 2의 escalate
# 시각은 그 이월된 due_at(T0+8h)보다 뒤여야 한다.
OVERDUE_NOW_2 = T0 + timedelta(hours=9)  # attempt 2 escalate 시각(> T0+8h)
DISPOSITION_NOW_2 = OVERDUE_NOW_2 + timedelta(hours=1)  # attempt 2 처분 시각

_LEASE_TTL = timedelta(minutes=5)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


ORG_ID = _ref("org", "org-1")
_TICKET_OWNER = _ref("subject", "owner-a")
_MANAGER_SUBJECT_ID = "manager-a"
_MANAGER_REF = _ref("subject", _MANAGER_SUBJECT_ID)


class _Policy:
    def __init__(self, result: NoApprovalRequired) -> None:
        self.result = result

    def evaluate(self, org_id: str, route: RouteTarget, candidate_mode: str) -> NoApprovalRequired:
        return self.result


class _Resolver:
    def resolve(self, *, org_id: str, route: RouteTarget) -> None:
        return None


class _Registry:
    def __init__(self, *, owner: str | None = _TICKET_OWNER) -> None:
        self._owner = owner

    def resolve_owner_subject(self, *, org_id: str, agent_id: str) -> str | None:
        return self._owner


class _Directory:
    """`DispatchEscalationTargetDirectory` Fake — S5.6b escalate 준비용."""

    def __init__(self, manager: str | None = _MANAGER_REF) -> None:
        self._manager = manager

    def resolve_manager(self, *, org_id: str, owner_subject_ref: str) -> str | None:
        return self._manager


class _RerouteRegistry:
    """`DurableManagerRegistry` Fake — 기본은 요청된 agent_id를 그대로
    eligible로 승인하고 ``requires_approval``은 고정값을 돌려준다."""

    def __init__(self, *, requires_approval: bool = False, target_agent_id: str | None = None) -> None:
        self._requires_approval = requires_approval
        self._target_agent_id = target_agent_id

    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None:
        resolved = self._target_agent_id if self._target_agent_id is not None else agent_id
        return DurableManagerAssignTarget(
            agent_id=resolved,
            owner_subject_ref=_ref("subject", f"owner-{resolved}"),
            requires_approval=self._requires_approval,
        )


class _NoneRerouteRegistry:
    """Reroute 대상 결선이 없는 Fake(⑬) — dismiss 전용 테스트에도 안전히 쓸 수 있다."""

    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None:
        return None


class _ManagerAuthority:
    """항상 grant하는 Fake `CentralAuthorizer`."""

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


class _DenyAuthority:
    """중앙 정책이 거부하는 Fake — `AuthorizationDenied`를 돌려준다."""

    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationDenied:
        return AuthorizationDenied(kind="not_found_or_denied")

    def verify(self, *args: object, **kwargs: object) -> bool:
        return False


class _RaisingAuthority:
    """중앙 정책 조회 자체가 실패하는 Fake."""

    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
        raise RuntimeError("policy backend 조회 실패")

    def verify(self, *args: object, **kwargs: object) -> bool:
        return False


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


def _manager_principal(
    org_id: str = ORG_ID, subject_id: str = _MANAGER_SUBJECT_ID
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=org_id, subject_id=subject_id, identity_provider="idp", identity_session_id="s1"
    )


def _escalation_uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    clock: object,
    directory: _Directory | None = None,
    escalation_sla: timedelta = _ESCALATION_SLA,
) -> DurableDispatchEscalationUnitOfWork:
    return DurableDispatchEscalationUnitOfWork(
        completion=completion,
        directory=directory or _Directory(),
        clock=clock,  # type: ignore[arg-type]
        escalation_sla=escalation_sla,
        item_id_factory=lambda: uuid.uuid4().hex,
        receipt_id_factory=lambda: uuid.uuid4().hex,
    )


def _seed_open_item(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    request_id: str,
    ticket_label: str,
    org_id: str = ORG_ID,
    agent_id: str = "card-a",
    owner: str | None = _TICKET_OWNER,
    manager: str | None = _MANAGER_REF,
    due_at: datetime = DUE_AT,
    escalate_now: datetime = OVERDUE_NOW,
    requires_approval: bool = False,
) -> DurableDispatchEscalated:
    """Received → ReadyToDispatch → AwaitingAnswer → escalate까지 한 번에 만든다."""
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

    escalation = _escalation_uow(completion, clock=lambda: escalate_now, directory=_Directory(manager))
    return escalation.escalate(ticket_id=enqueued.ticket_id)


_UNSET = object()


def _uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    authorizer: object = _UNSET,
    registry: DurableManagerRegistry | None = None,
    clock: object = lambda: DISPOSITION_NOW,
    receipt_id_factory: object = None,
    fault_injector: object = None,
) -> DurableDispatchDispositionUnitOfWork:
    return DurableDispatchDispositionUnitOfWork(
        completion=completion,
        registry=registry or _RerouteRegistry(),
        central_authorizer=(
            _ManagerAuthority() if authorizer is _UNSET else authorizer
        ),  # type: ignore[arg-type]
        clock=clock,  # type: ignore[arg-type]
        receipt_id_factory=receipt_id_factory or (lambda: uuid.uuid4().hex),  # type: ignore[arg-type]
        fault_injector=fault_injector,  # type: ignore[arg-type]
    )


def _item_row(completion: SqliteQuestionCompletionUnitOfWork, item_id: str) -> sqlite3.Row | None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        row = tx.execute(
            "SELECT * FROM durable_dispatch_manager_items WHERE manager_item_id=?", (item_id,)
        ).fetchone()
        tx.commit()
        return row


def _receipt_row_by_digest_count(completion: SqliteQuestionCompletionUnitOfWork) -> int:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        n = tx.execute("SELECT count(*) FROM durable_dispatch_escalation_receipts").fetchone()[0]
        tx.commit()
        return int(n)


def _item_count(completion: SqliteQuestionCompletionUnitOfWork) -> int:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        n = tx.execute("SELECT count(*) FROM durable_dispatch_manager_items").fetchone()[0]
        tx.commit()
        return int(n)


def _artifact_counts(completion: SqliteQuestionCompletionUnitOfWork) -> tuple[int, int]:
    return (_receipt_row_by_digest_count(completion), _item_count(completion))


# ---------------------------------------------------------------------------
# 시그니처 — ctor·act() 파라미터가 스펙과 정확히 같다.
# ---------------------------------------------------------------------------


def test_생성자_파라미터가_스펙과_정확히_같다() -> None:
    params = set(inspect.signature(DurableDispatchDispositionUnitOfWork.__init__).parameters)
    assert params == {
        "self",
        "completion",
        "central_authorizer",
        # reroute는 대상 카드를 바꾸므로 requires_approval·eligibility를 새 대상에서
        # 재도출해야 한다(기존 route 보존은 승인 우회다 — ADR 0066 §5.2).
        "registry",
        "clock",
        "receipt_id_factory",
        "fault_injector",
    }


def test_act_시그니처는_principal과_command만_받는다() -> None:
    params = set(inspect.signature(DurableDispatchDispositionUnitOfWork.act).parameters)
    assert params == {"self", "principal", "command"}


def test_에러_계층이_올바르다() -> None:
    assert issubclass(DurableDispatchDispositionBusy, DurableDispatchDispositionUnavailable)
    assert issubclass(DurableDispatchDispositionUnavailable, DurableDispatchDispositionError)
    assert issubclass(DurableDispatchDispositionConflict, DurableDispatchDispositionError)
    assert not issubclass(DurableDispatchDispositionConflict, DurableDispatchDispositionUnavailable)
    assert not issubclass(DurableDispatchEscalationError, DurableDispatchDispositionError)
    assert not issubclass(DurableDispatchDispositionError, DurableDispatchEscalationError)


# ---------------------------------------------------------------------------
# 해피 패스 — reroute는 한 transaction에 receipt·Item CAS·Request 전이를
# 함께 커밋한다. ① reroute attempt == item.attempt + 1의 1차 확인(1→2).
# ---------------------------------------------------------------------------


def test_reroute는_한_transaction에_receipt_item_request전이를_함께_커밋한다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-reroute-happy")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-reroute-happy"
        )
        item_before = _item_row(completion, escalated.manager_item_id)
        assert item_before is not None
        assert item_before["attempt"] == 1

        uow = _uow(completion)
        command = DurableDispatchRerouteCommand(
            item_id=escalated.manager_item_id,
            request_id=request_id,
            agent_id="card-b",
            expected_request_revision=escalated.request_revision,
            rationale="다른 담당자에게 재배정",
        )
        rerouted = uow.act(principal=_manager_principal(), command=command)

        assert isinstance(rerouted, DurableDispatchRerouted)
        assert rerouted.item_id == escalated.manager_item_id
        assert rerouted.request_id == request_id
        assert rerouted.agent_id == "card-b"
        assert rerouted.attempt == 2  # ① item.attempt(1) + 1

        item_after = _item_row(completion, escalated.manager_item_id)
        assert item_after is not None
        assert item_after["status"] == "resolved"
        assert item_after["attempt"] == 1  # item 자기 행의 attempt는 불변이다

        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, ReadyToDispatch)
        assert request.state.attempt == 2
        assert request.state.route.agent_id == "card-b"
        assert request.state.route.intent == "refund"  # 기존 route에서 보존
        assert request.state.route.requires_approval is False  # 기존 route에서 보존
        assert request.state.trigger_key == rerouted.receipt_id
        assert request.state.handling.kind == "system"
        assert request.state.handling.ref == rerouted.receipt_id
        assert request.revision == rerouted.request_revision

        receipt_tx = completion.durable_transaction()
        with receipt_tx.scope():
            receipt_tx.begin_immediate()
            receipt = receipt_tx.execute(
                "SELECT * FROM durable_dispatch_escalation_receipts WHERE receipt_id=?",
                (rerouted.receipt_id,),
            ).fetchone()
            receipt_tx.commit()
        assert receipt is not None
        assert receipt["action"] == "manager.reroute"
        assert receipt["manager_item_id"] == escalated.manager_item_id
        assert receipt["principal_ref"] == _MANAGER_REF
        assert receipt["expected_request_revision"] == escalated.request_revision
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ② dismiss → DeclinedRequest(manager_declined)·rationale 미지속.
# ---------------------------------------------------------------------------


def test_dismiss는_declinedrequest로_전이하고_rationale은_지속하지_않는다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-dismiss-happy")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-dismiss-happy"
        )
        uow = _uow(completion)
        secret_rationale = "고객이 이미 환불 완료됨 — 내부 계정 정보 xyz"
        command = DurableDispatchDismissCommand(
            item_id=escalated.manager_item_id,
            request_id=request_id,
            expected_request_revision=escalated.request_revision,
            rationale=secret_rationale,
        )
        dismissed = uow.act(principal=_manager_principal(), command=command)

        assert isinstance(dismissed, DurableDispatchDismissed)
        assert dismissed.reason_code == "manager_declined"

        item_after = _item_row(completion, escalated.manager_item_id)
        assert item_after is not None
        assert item_after["status"] == "dismissed"

        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, DeclinedRequest)
        assert request.state.reason_code == "manager_declined"
        assert request.revision == dismissed.request_revision

        # rationale은 어떤 durable 열에도 원문으로 남지 않는다(노출 불변식).
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            rows = tx.execute(
                "SELECT * FROM durable_dispatch_escalation_receipts WHERE receipt_id=?",
                (dismissed.receipt_id,),
            ).fetchall()
            tx.commit()
        assert len(rows) == 1
        for row in rows:
            for key in row.keys():
                assert secret_rationale not in str(row[key])
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ③ Item open→resolved|dismissed CAS — 이미 처분된 Item 재처분은 거부된다.
# ---------------------------------------------------------------------------


def test_이미_resolved인_item에_다시_reroute하면_conflict이고_write0이다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-double-reroute")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-double-reroute"
        )
        first_uow = _uow(completion, receipt_id_factory=lambda: "receipt-first")
        first_uow.act(
            principal=_manager_principal(),
            command=DurableDispatchRerouteCommand(
                escalated.manager_item_id, request_id, "card-b", escalated.request_revision
            ),
        )
        before = _artifact_counts(completion)

        second_uow = _uow(
            completion, clock=lambda: DISPOSITION_NOW + timedelta(minutes=1),
            receipt_id_factory=lambda: "receipt-second",
        )
        with pytest.raises(DurableDispatchDispositionConflict):
            second_uow.act(
                principal=_manager_principal(),
                command=DurableDispatchRerouteCommand(
                    escalated.manager_item_id, request_id, "card-c", escalated.request_revision + 1
                ),
            )
        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_이미_dismissed인_item에_dismiss하면_replay가_아니라_conflict이다(
    tmp_path: Path,
) -> None:
    """같은 command(같은 digest)의 재호출이 아니라 **다른** command(다른
    receipt_id_factory 호출 결과와 무관 — digest는 command 값에서만 나온다)
    가 이미 처분된 Item을 다시 겨냥하는 경우를 겨냥한다. 여기서는 rationale을
    다르게 줘 digest 자체를 다르게 만든다."""
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-double-dismiss")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-double-dismiss"
        )
        uow = _uow(completion)
        uow.act(
            principal=_manager_principal(),
            command=DurableDispatchDismissCommand(
                escalated.manager_item_id, request_id, escalated.request_revision, rationale="a"
            ),
        )
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchDispositionConflict):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchDismissCommand(
                    escalated.manager_item_id, request_id, escalated.request_revision, rationale="b"
                ),
            )
        assert _artifact_counts(completion) == before
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑤ 중앙 manager.act 재인가 실패 → write 0.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authorizer", [None, _DenyAuthority(), _RaisingAuthority()], ids=["none", "deny", "raises"]
)
def test_중앙_재인가가_실패하면_write0이다(tmp_path: Path, authorizer: object) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-auth-fail")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-auth-fail"
        )
        uow = _uow(completion, authorizer=authorizer)
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchDispositionError):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchRerouteCommand(
                    escalated.manager_item_id, request_id, "card-b", escalated.request_revision
                ),
            )

        assert _artifact_counts(completion) == before
        item = _item_row(completion, escalated.manager_item_id)
        assert item is not None
        assert item["status"] == "open"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingManager)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑥ fault 3지점 — reroute·dismiss 각각에서 부분 쓰기가 0이다.
# ---------------------------------------------------------------------------


_FAULT_POINTS = ("after_receipt_insert", "after_item_disposed", "after_request_transition")


@pytest.mark.parametrize("point", _FAULT_POINTS)
def test_reroute_fault_지점마다_부분_쓰기가_0이다(tmp_path: Path, point: str) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r-reroute-fault-{point}")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label=f"ticket-reroute-fault-{point}"
        )

        def raise_at(injected: str) -> None:
            if injected == point:
                raise RuntimeError(injected)

        uow = _uow(completion, fault_injector=raise_at)
        before = _artifact_counts(completion)

        with pytest.raises(RuntimeError, match=point):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchRerouteCommand(
                    escalated.manager_item_id, request_id, "card-b", escalated.request_revision
                ),
            )

        assert _artifact_counts(completion) == before
        item = _item_row(completion, escalated.manager_item_id)
        assert item is not None
        assert item["status"] == "open"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingManager)
    finally:
        completion.close()


@pytest.mark.parametrize("point", _FAULT_POINTS)
def test_dismiss_fault_지점마다_부분_쓰기가_0이다(tmp_path: Path, point: str) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r-dismiss-fault-{point}")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label=f"ticket-dismiss-fault-{point}"
        )

        def raise_at(injected: str) -> None:
            if injected == point:
                raise RuntimeError(injected)

        uow = _uow(completion, fault_injector=raise_at)
        before = _artifact_counts(completion)

        with pytest.raises(RuntimeError, match=point):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchDismissCommand(
                    escalated.manager_item_id, request_id, escalated.request_revision
                ),
            )

        assert _artifact_counts(completion) == before
        item = _item_row(completion, escalated.manager_item_id)
        assert item is not None
        assert item["status"] == "open"
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingManager)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑦ replay — 같은 command는 write 0로 같은 결과를 돌려준다.
# ---------------------------------------------------------------------------


def test_같은_reroute_command는_replay되고_write가_늘지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-reroute-replay")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-reroute-replay"
        )
        uow = _uow(completion)
        command = DurableDispatchRerouteCommand(
            escalated.manager_item_id, request_id, "card-b", escalated.request_revision
        )
        first = uow.act(principal=_manager_principal(), command=command)
        before = _artifact_counts(completion)

        second = uow.act(principal=_manager_principal(), command=command)

        assert second == first
        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_같은_dismiss_command는_replay되고_write가_늘지_않는다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-dismiss-replay")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-dismiss-replay"
        )
        uow = _uow(completion)
        command = DurableDispatchDismissCommand(
            escalated.manager_item_id, request_id, escalated.request_revision
        )
        first = uow.act(principal=_manager_principal(), command=command)
        before = _artifact_counts(completion)

        second = uow.act(principal=_manager_principal(), command=command)

        assert second == first
        assert _artifact_counts(completion) == before
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑨ — 처분 뒤 S4.6·S5.6a 게이트가 green이다.
# ---------------------------------------------------------------------------


def test_reroute_후_s4_6_및_s5_6a_게이트가_green이다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    try:
        request_id = _ref("request", "r-reroute-gate")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-reroute-gate"
        )
        uow = _uow(completion)
        uow.act(
            principal=_manager_principal(),
            command=DurableDispatchRerouteCommand(
                escalated.manager_item_id, request_id, "card-b", escalated.request_revision
            ),
        )
    finally:
        completion.close()

    linked_report = reconcile_sqlite_durable_linked_gate(path)
    assert linked_report.capable is True
    assert linked_report.violations == ()
    escalation_report = reconcile_sqlite_durable_dispatch_escalation_schema(path)
    assert escalation_report.capable is True
    s5_report = reconcile_sqlite_durable_dispatch_gate(path)
    assert s5_report.capable is True
    assert s5_report.violations == ()


def test_dismiss_후_s4_6_및_s5_6a_게이트가_green이다(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path)
    try:
        request_id = _ref("request", "r-dismiss-gate")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-dismiss-gate"
        )
        uow = _uow(completion)
        uow.act(
            principal=_manager_principal(),
            command=DurableDispatchDismissCommand(
                escalated.manager_item_id, request_id, escalated.request_revision
            ),
        )
    finally:
        completion.close()

    linked_report = reconcile_sqlite_durable_linked_gate(path)
    assert linked_report.capable is True
    assert linked_report.violations == ()
    escalation_report = reconcile_sqlite_durable_dispatch_escalation_schema(path)
    assert escalation_report.capable is True
    s5_report = reconcile_sqlite_durable_dispatch_gate(path)
    assert s5_report.capable is True
    assert s5_report.violations == ()


# ---------------------------------------------------------------------------
# ①/⑪ — 미아 없음 왕복: escalate → reroute(attempt 1→2) → 새 attempt로
# 재-dispatch → 그 시도도 timeout → 다시 escalate 성공(item.attempt==2) →
# 그 attempt 2 Item에서 다시 reroute하면 3이 된다(① "≠1 실증"의 정확한 형태).
# 사용자가 승인한 ADR 0066 결정 §1("실행 시도마다 최대 한 번")이 처분
# UoW까지 실제로 도는지 왕복으로 증명하는, 이 슬라이스의 존재 이유다.
# ---------------------------------------------------------------------------


def test_미아_없음_왕복_escalate_reroute_재dispatch_timeout_재escalate_재reroute(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-round-trip")
        escalated_1 = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-round-trip-1"
        )
        assert escalated_1.request_revision  # attempt 1 escalate 완료(AwaitingManager)

        # 1) reroute: attempt 1 → 2, card-a → card-b.
        disposition_uow_1 = _uow(completion, clock=lambda: DISPOSITION_NOW)
        rerouted_1 = disposition_uow_1.act(
            principal=_manager_principal(),
            command=DurableDispatchRerouteCommand(
                escalated_1.manager_item_id, request_id, "card-b", escalated_1.request_revision
            ),
        )
        assert isinstance(rerouted_1, DurableDispatchRerouted)
        assert rerouted_1.attempt == 2

        # 2) 새 attempt로 재-dispatch(enqueue) — Request는 ReadyToDispatch(attempt=2).
        enqueue_uow_2 = DurableWorkTicketEnqueueUnitOfWork(
            completion=completion,
            registry=_Registry(owner=_TICKET_OWNER),
            clock=lambda: DISPOSITION_NOW,
            ticket_id_factory=lambda: "ticket-round-trip-2",
            receipt_id_factory=lambda: "receipt-ticket-round-trip-2",
        )
        enqueued_2 = enqueue_uow_2.enqueue(
            command=DurableWorkTicketEnqueueCommand(request_id, rerouted_1.request_revision, 2)
        )

        # 3) 그 attempt 2 실행도 timeout(SLA 경과) → 다시 escalate 성공.
        escalation_uow_2 = _escalation_uow(completion, clock=lambda: OVERDUE_NOW_2)
        escalated_2 = escalation_uow_2.escalate(ticket_id=enqueued_2.ticket_id)
        assert escalated_2.manager_item_id != escalated_1.manager_item_id
        item_2 = _item_row(completion, escalated_2.manager_item_id)
        assert item_2 is not None
        assert item_2["attempt"] == 2  # 미아 없음 — 재실행 실패가 다시 사람에게 닿았다

        # 4) attempt 2 Item에서 다시 reroute하면 3이 된다(① "≠1 실증").
        disposition_uow_2 = _uow(completion, clock=lambda: DISPOSITION_NOW_2)
        rerouted_2 = disposition_uow_2.act(
            principal=_manager_principal(),
            command=DurableDispatchRerouteCommand(
                escalated_2.manager_item_id, request_id, "card-c", escalated_2.request_revision
            ),
        )
        assert isinstance(rerouted_2, DurableDispatchRerouted)
        assert rerouted_2.attempt == 3

        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, ReadyToDispatch)
        assert request.state.attempt == 3
        assert request.state.route.agent_id == "card-c"
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# 1인칭 귀속 — 다른 Manager의 command는 Conflict.
# ---------------------------------------------------------------------------


def test_다른_manager가_처분하면_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-wrong-manager")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-wrong-manager"
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchDispositionConflict):
            uow.act(
                principal=_manager_principal(subject_id="다른-manager"),
                command=DurableDispatchRerouteCommand(
                    escalated.manager_item_id, request_id, "card-b", escalated.request_revision
                ),
            )
        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_존재하지_않는_item_id는_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        uow = _uow(completion)
        with pytest.raises(DurableDispatchDispositionConflict):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchDismissCommand(
                    _ref("manager", "does-not-exist"), _ref("request", "r-no-item"), 1
                ),
            )
    finally:
        completion.close()


def test_expected_request_revision이_어긋나면_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-bad-revision")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-bad-revision"
        )
        uow = _uow(completion)
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchDispositionConflict):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchDismissCommand(
                    escalated.manager_item_id, request_id, escalated.request_revision + 5
                ),
            )
        assert _artifact_counts(completion) == before
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# capability — S5.6a escalation schema 손상은 S5.6b가 이미 확립한 타입으로
# wrap 없이 관측된다(ctor·act() 양쪽 진입점 모두). 이 모듈 자기 타입으로
# 감싸지 않는다 — 이 모듈은 그 capability의 owner가 아니다.
# ---------------------------------------------------------------------------


def test_escalation_schema_손상은_생성자에서_남의_타입으로_관측되고_wrap되지_않는다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute("DROP TABLE durable_dispatch_escalation_receipts")
            tx.commit()

        with pytest.raises(DurableDispatchEscalationError) as excinfo:
            _uow(completion)
        assert not isinstance(excinfo.value, DurableDispatchDispositionError)
    finally:
        completion.close()


def test_escalation_schema_손상_후_act호출은_남의_타입이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-cap-corrupt")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-cap-corrupt"
        )
        uow = _uow(completion)

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute("DROP TABLE durable_dispatch_escalation_receipts")
            tx.commit()

        with pytest.raises(DurableDispatchEscalationError) as excinfo:
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchDismissCommand(
                    escalated.manager_item_id, request_id, escalated.request_revision
                ),
            )
        assert not isinstance(excinfo.value, DurableDispatchDispositionError)
    finally:
        completion.close()


def test_다른_connection이_lock을_쥐면_act은_durabledispatchdispositionbusy이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    completion = _open_all(path, timeout=0.2)
    try:
        request_id = _ref("request", "r-busy")
        escalated = _seed_open_item(completion, request_id=request_id, ticket_label="ticket-busy")
        # _seed_open_item의 escalate() 자체가 이미 receipt 1개(work_ticket.
        # escalate)를 남긴다 — 공용 receipt 표이므로 절대값이 아니라 이
        # snapshot 대비 증가분 0을 확인한다.
        before = _artifact_counts(completion)
        uow = _uow(completion)

        blocker = sqlite3.connect(str(path), timeout=1.0)
        blocker.execute("PRAGMA foreign_keys=ON")
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(DurableDispatchDispositionBusy) as excinfo:
                uow.act(
                    principal=_manager_principal(),
                    command=DurableDispatchDismissCommand(
                        escalated.manager_item_id, request_id, escalated.request_revision
                    ),
                )
            assert type(excinfo.value) is DurableDispatchDispositionBusy
        finally:
            blocker.rollback()
            blocker.close()

        assert _artifact_counts(completion) == before

        # lock 해제 뒤 재실행은 정상이다.
        dismissed = uow.act(
            principal=_manager_principal(),
            command=DurableDispatchDismissCommand(
                escalated.manager_item_id, request_id, escalated.request_revision
            ),
        )
        assert dismissed.item_id == escalated.manager_item_id
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# 타입 검사 — command 형식.
# ---------------------------------------------------------------------------


def test_command_타입이_아니면_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        uow = _uow(completion)
        with pytest.raises(DurableDispatchDispositionUnavailable):
            uow.act(principal=_manager_principal(), command=object())  # type: ignore[arg-type]
    finally:
        completion.close()


def test_principal_타입이_아니면_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-bad-principal")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-bad-principal"
        )
        uow = _uow(completion)
        with pytest.raises(DurableDispatchDispositionUnavailable):
            uow.act(
                principal=object(),  # type: ignore[arg-type]
                command=DurableDispatchDismissCommand(
                    escalated.manager_item_id, request_id, escalated.request_revision
                ),
            )
    finally:
        completion.close()


def test_agent_id가_blank이면_unavailable이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-blank-agent")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-blank-agent"
        )
        uow = _uow(completion)
        with pytest.raises(DurableDispatchDispositionUnavailable):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchRerouteCommand(
                    escalated.manager_item_id, request_id, "   ", escalated.request_revision
                ),
            )
    finally:
        completion.close()


@pytest.mark.parametrize("bad_revision", [-1, "1", None, 1.0, True])
def test_expected_request_revision이_비정상이면_unavailable이다(
    tmp_path: Path, bad_revision: object
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", f"r-bad-rev-{bad_revision!r}")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label=f"ticket-bad-rev-{bad_revision!r}"
        )
        uow = _uow(completion)
        with pytest.raises(DurableDispatchDispositionUnavailable):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchDismissCommand(
                    escalated.manager_item_id, request_id, bad_revision  # type: ignore[arg-type]
                ),
            )
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# ⑫⑬⑭ — 2026-07-27 team-lead 판정: reroute는 새 대상 카드에서
# requires_approval·eligibility를 재도출해야 한다(옛 route를 그대로 베끼면
# 승인 우회). ⑫가 이 판정의 핵심 실증이다 — route 보존 mutant를 걸면 반드시
# 죽어야 한다.
# ---------------------------------------------------------------------------


def test_옛_카드는_승인_불필요_새_카드는_승인_필요면_requires_approval이_true로_재도출된다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-approval-bypass-guard")
        # _seed_open_item의 route는 requires_approval=False로 시작한다.
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-approval-bypass-guard"
        )
        uow = _uow(
            completion,
            registry=_RerouteRegistry(requires_approval=True, target_agent_id="card-strict"),
        )
        rerouted = uow.act(
            principal=_manager_principal(),
            command=DurableDispatchRerouteCommand(
                escalated.manager_item_id, request_id, "card-strict", escalated.request_revision
            ),
        )
        assert isinstance(rerouted, DurableDispatchRerouted)

        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, ReadyToDispatch)
        assert request.state.route.requires_approval is True  # 새 카드에서 재도출 — 승인 우회 없음
        assert request.state.route.agent_id == "card-strict"
        assert request.state.route.intent == "refund"  # intent는 불변(⑭)
    finally:
        completion.close()


def test_옛_카드는_승인_필요_새_카드는_승인_불필요면_requires_approval이_false로_재도출된다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-approval-relax")
        escalated = _seed_open_item(
            completion,
            request_id=request_id,
            ticket_label="ticket-approval-relax",
            requires_approval=True,
        )
        uow = _uow(
            completion,
            registry=_RerouteRegistry(requires_approval=False, target_agent_id="card-relaxed"),
        )
        rerouted = uow.act(
            principal=_manager_principal(),
            command=DurableDispatchRerouteCommand(
                escalated.manager_item_id, request_id, "card-relaxed", escalated.request_revision
            ),
        )
        assert isinstance(rerouted, DurableDispatchRerouted)

        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, ReadyToDispatch)
        assert request.state.route.requires_approval is False  # 새 카드에서 재도출
        assert request.state.route.agent_id == "card-relaxed"
    finally:
        completion.close()


def test_registry가_대상을_결선하지_못하면_conflict이고_write0이며_item은_open으로_남는다(
    tmp_path: Path,
) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-no-registry-target")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-no-registry-target"
        )
        uow = _uow(completion, registry=_NoneRerouteRegistry())
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchDispositionConflict):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchRerouteCommand(
                    escalated.manager_item_id, request_id, "card-b", escalated.request_revision
                ),
            )

        assert _artifact_counts(completion) == before
        item = _item_row(completion, escalated.manager_item_id)
        assert item is not None
        assert item["status"] == "open"  # 미아 없음 — 다음 재시도로 회복
        request = completion.get(request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingManager)
    finally:
        completion.close()


def test_registry가_다른_agent_id를_돌려주면_conflict이고_write0이다(tmp_path: Path) -> None:
    completion = _open_all(tmp_path / "workflow.sqlite")
    try:
        request_id = _ref("request", "r-registry-mismatch")
        escalated = _seed_open_item(
            completion, request_id=request_id, ticket_label="ticket-registry-mismatch"
        )
        uow = _uow(completion, registry=_RerouteRegistry(target_agent_id="card-different"))
        before = _artifact_counts(completion)

        with pytest.raises(DurableDispatchDispositionConflict):
            uow.act(
                principal=_manager_principal(),
                command=DurableDispatchRerouteCommand(
                    escalated.manager_item_id, request_id, "card-b", escalated.request_revision
                ),
            )

        assert _artifact_counts(completion) == before
    finally:
        completion.close()


def test_reroute_command에는_intent_필드가_없다() -> None:
    # ⑭ intent 불변 — command 자체가 intent를 실어 보낼 수 없으므로 항상
    # 기존 route에서만 가져온다.
    field_names = {f.name for f in dataclasses.fields(DurableDispatchRerouteCommand)}
    assert "intent" not in field_names
    assert field_names == {"item_id", "request_id", "agent_id", "expected_request_revision", "rationale"}


def test_생성자에_registry_인자가_있다() -> None:
    params = set(inspect.signature(DurableDispatchDispositionUnitOfWork.__init__).parameters)
    assert "registry" in params
    assert params == {
        "self",
        "completion",
        "registry",
        "central_authorizer",
        "clock",
        "receipt_id_factory",
        "fault_injector",
    }
