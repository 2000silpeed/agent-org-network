from __future__ import annotations
# pyright: reportArgumentType=false

import hashlib
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    AuthorizationGrant,
    ManagerActItemSnapshot,
    ResourceRef,
    SnapshotCentralAuthorizer,
    canonical_policy_digest,
    load_authority_policy_yaml,
)
from agent_org_network.conflict_escalation_approval_evidence import (
    ESCALATE_ACTION,
    ConflictEscalationApprovalEvidence,
    canonical_escalate_command_digest,
    escalation_cause_digest,
    escalation_resource_fingerprint,
)
from agent_org_network.conflict_escalation_registry_snapshot import (
    ConflictEscalationOwnerPath,
    ConflictEscalationRegistrySnapshot,
    ConflictEscalationSnapshotCandidate,
)
from agent_org_network.conflict_open_contract import ConflictOpenCandidateClaim
from agent_org_network.durable_conflict_escalation_evidence import DivergentVotes
from agent_org_network.question_request import (
    AwaitingConflict,
    AwaitingManager,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    migrate_sqlite_durable_conflict_escalation_baseline_schema,
)
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    migrate_sqlite_durable_conflict_escalation_receipts_schema,
)
from agent_org_network.sqlite_durable_conflict_escalation_reconciliation import (
    reconcile_sqlite_durable_conflict_escalation_gate,
)
from agent_org_network.sqlite_durable_conflict_escalation_uow import (
    DurableConflictEscalateCommand,
    DurableConflictEscalatedToManager,
    DurableConflictEscalationResult,
    DurableConflictEscalationUnitOfWork,
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
    DurableManagerDispositionConflict,
    DurableManagerDispositionUnavailable,
    DurableManagerDispositionUnitOfWork,
    DurableManagerOwnerAssigned,
    DurableManagerRegistry,
)

NOW = datetime(2026, 7, 22, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


ORG_ID = _ref("org", "org-1")
REQUEST_ID = _ref("request", "request-1")
ITEM_REF = _ref("manager", "item-1")
MANAGER_SUBJECT_REF = _ref("subject", "manager-1")


class _Authority:
    def __init__(self, *, deny_at_call: int | None = None) -> None:
        self.calls = 0
        self._deny_at_call = deny_at_call

    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
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
        return self._deny_at_call != self.calls


class _Registry:
    """항상 None을 돌려주는 Registry — Dismiss(등록 결선 불요) 테스트 전용."""

    def __init__(self) -> None:
        self.calls = 0

    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None:
        self.calls += 1
        return None


class _AssignRegistry:
    """Assign happy-path용 Registry — 고정 target을 돌려준다."""

    def __init__(
        self,
        *,
        agent_id: str = "card-a",
        requires_approval: bool = False,
        target: DurableManagerAssignTarget | None = None,
    ) -> None:
        self.calls = 0
        self._target = target or DurableManagerAssignTarget(
            agent_id=agent_id,
            owner_subject_ref=_ref("subject", "owner-a"),
            requires_approval=requires_approval,
        )

    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None:
        self.calls += 1
        return self._target


def _principal(subject_id: str = "manager-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=ORG_ID, subject_id=subject_id, identity_provider="idp", identity_session_id="s1"
    )


def _command(
    *, item_id: str = ITEM_REF, expected_request_revision: int = 1, rationale: str = ""
) -> DurableManagerDismissCommand:
    return DurableManagerDismissCommand(item_id, REQUEST_ID, expected_request_revision, rationale)


def _assign_command(
    *,
    item_id: str = ITEM_REF,
    agent_id: str = "card-a",
    expected_request_revision: int = 1,
    rationale: str = "",
) -> DurableManagerAssignCommand:
    return DurableManagerAssignCommand(
        item_id, REQUEST_ID, agent_id, expected_request_revision, rationale
    )


def _prepared(
    tmp_path: Path, *, state_item_id: str = ITEM_REF, intent: str | None = "refund"
) -> SqliteQuestionCompletionUnitOfWork:
    """FromUnowned seed(스펙 §7): Received rev0 → AwaitingManager(unowned) rev1·item awaiting_revision=0."""
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: NOW,
    )
    received = QuestionRequest.receive(
        org_id=ORG_ID,
        requester_id="user",
        question="refund question",
        request_id_factory=lambda: REQUEST_ID,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(received)
    unowned_request = received.record_initial_routing(
        intent=intent,
        disposition="unowned",
        target=AwaitingManager(
            item_id=state_item_id,
            public_kind="unowned",
            handling=HandlingAssignment(
                kind="manager_item", ref=state_item_id, due_at=NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set(REQUEST_ID, 0, received, unowned_request)
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "INSERT INTO durable_linked_manager_items VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ITEM_REF,
                ORG_ID,
                REQUEST_ID,
                0,
                "unowned",
                _ref("source", REQUEST_ID),
                MANAGER_SUBJECT_REF,
                "open",
                NOW.isoformat(),
            ),
        )
        tx.commit()
    return completion


def _uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    authority: _Authority,
    registry: DurableManagerRegistry | None = None,
    fault_injector: Callable[[str], None] | None = None,
    receipt_id: str = "receipt-1",
) -> DurableManagerDispositionUnitOfWork:
    return DurableManagerDispositionUnitOfWork(
        completion=completion,
        registry=registry or _Registry(),
        central_authorizer=authority,
        clock=lambda: NOW,
        receipt_id_factory=lambda: receipt_id,
        fault_injector=fault_injector,
    )


def _row_counts(tx: SqliteCompletionTransaction) -> dict[str, int]:
    return {
        table: tx.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "durable_linked_command_receipts",
            "durable_linked_audit_intents",
            "durable_linked_outbox_intents",
        )
    }


def _assert_no_writes(completion: SqliteQuestionCompletionUnitOfWork) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        assert all(count == 0 for count in _row_counts(tx).values())
        item = tx.execute(
            "SELECT status FROM durable_linked_manager_items WHERE manager_item_id=?", (ITEM_REF,)
        ).fetchone()
        assert item["status"] == "open"
        request = tx.select_question_request(REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, AwaitingManager) and request.revision == 1
        tx.commit()


# ---------------------------------------------------------------------------
# S4.4a — 계약(값객체·error·FromDispatch 가드)
# ---------------------------------------------------------------------------


def test_command_결과_값객체는_frozen이다() -> None:
    command = _command()
    with pytest.raises(FrozenInstanceError):
        command.item_id = "other"  # type: ignore[misc]
    result = DurableManagerDismissed("receipt:x", ITEM_REF, REQUEST_ID, 2)
    with pytest.raises(FrozenInstanceError):
        result.item_id = "other"  # type: ignore[misc]
    assign_command = DurableManagerAssignCommand(ITEM_REF, REQUEST_ID, "card-a", 1)
    with pytest.raises(FrozenInstanceError):
        assign_command.agent_id = "other"  # type: ignore[misc]
    assigned = DurableManagerOwnerAssigned("receipt:x", ITEM_REF, REQUEST_ID, 2, "card-a")
    with pytest.raises(FrozenInstanceError):
        assigned.agent_id = "other"  # type: ignore[misc]


def test_act는_exact하지_않은_command_타입을_unavailable로_거부한다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionUnavailable):
            uow.act(principal=_principal(), command=object())
        _assert_no_writes(completion)
        assert authority.calls == 0
    finally:
        completion.close()


def test_act는_registry가_assign_대상을_못_찾으면_conflict이고_write_0이다(tmp_path: Path) -> None:
    # registry.resolve_assign_target None은 카드 부재·Owner 부재·not
    # domain_authorized 세 이유를 모두 대표한다(스펙 §2) — UoW는 이유를 구분하지
    # 않고 전부 Conflict·write 0으로 닫는다.
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        registry = _Registry()
        uow = _uow(completion, authority=authority, registry=registry)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_assign_command())
        _assert_no_writes(completion)
        assert authority.calls == 1
        assert registry.calls == 1
    finally:
        completion.close()


def test_act는_fromdispatch_manageritem을_unavailable로_거부한다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_manager_items SET source_kind='dispatch' WHERE manager_item_id=?",
                (ITEM_REF,),
            )
            tx.commit()
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionUnavailable):
            uow.act(principal=_principal(), command=_command())
        assert authority.calls == 0
    finally:
        completion.close()


def test_존재하지_않는_manageritem은_conflict이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        command = _command(item_id=_ref("manager", "missing"))
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=command)
        assert authority.calls == 0
    finally:
        completion.close()


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
        with pytest.raises(DurableManagerDispositionUnavailable):
            DurableManagerDispositionUnitOfWork(
                completion=completion,
                registry=_Registry(),
                central_authorizer=_Authority(),
                clock=lambda: NOW,
                receipt_id_factory=lambda: "receipt-1",
            )
    finally:
        completion.close()


def test_act는_authorizer_미배선이면_unavailable이고_write_0이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        uow = DurableManagerDispositionUnitOfWork(
            completion=completion,
            registry=_Registry(),
            central_authorizer=None,
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt-1",
        )
        with pytest.raises(DurableManagerDispositionUnavailable):
            uow.act(principal=_principal(), command=_command())
        _assert_no_writes(completion)
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# S4.4b — FromUnowned Dismiss UoW
# ---------------------------------------------------------------------------


def test_fresh_dismiss_commits_item_request_and_receipt_graph_atomically(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        result = uow.act(principal=_principal(), command=_command())
        assert isinstance(result, DurableManagerDismissed)
        assert result.item_id == ITEM_REF
        assert result.request_id == REQUEST_ID
        assert result.request_revision == 2
        assert result.reason_code == "manager_declined"

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _row_counts(tx).values())
            item = tx.execute(
                "SELECT * FROM durable_linked_manager_items WHERE manager_item_id=?", (ITEM_REF,)
            ).fetchone()
            assert item["status"] == "dismissed"
            receipt = tx.execute(
                "SELECT * FROM durable_linked_command_receipts WHERE request_id=?", (REQUEST_ID,)
            ).fetchone()
            assert receipt["receipt_id"] == result.receipt_id
            assert receipt["action"] == "manager.dismiss"
            assert receipt["target_kind"] == "manager_item"
            assert receipt["target_ref"] == ITEM_REF
            assert receipt["principal_id"] == _ref("subject", "manager-1")
            assert receipt["expected_request_revision"] == 1
            audit = tx.execute(
                "SELECT * FROM durable_linked_audit_intents WHERE receipt_id=?",
                (receipt["receipt_id"],),
            ).fetchone()
            outbox = tx.execute(
                "SELECT * FROM durable_linked_outbox_intents WHERE receipt_id=?",
                (receipt["receipt_id"],),
            ).fetchone()
            assert audit["command_digest"] == receipt["command_digest"] == outbox["command_digest"]
            assert audit["action"] == receipt["action"] == "manager.dismiss"
            assert outbox["kind"] == "linked_aggregate_outbox"
            tx.commit()

        request = completion.get(REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, DeclinedRequest)
        assert request.state.reason_code == "manager_declined"
        assert request.revision == 2
        assert authority.calls == 1
    finally:
        completion.close()


def test_replay_returns_stored_result_without_central_reauthorization(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        command = _command()
        first = uow.act(principal=_principal(), command=command)
        calls_after_first = authority.calls
        second = uow.act(principal=_principal(), command=command)
        assert second == first
        assert authority.calls == calls_after_first

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _row_counts(tx).values())
            tx.commit()
    finally:
        completion.close()


def test_dismiss_커밋_결과는_s4_1_validate를_직접_통과한다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        uow = _uow(completion, authority=_Authority())
        uow.act(principal=_principal(), command=_command())
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.validate_component_in_transaction(
                lambda conn: validate_sqlite_durable_linked_aggregates_connection(
                    conn, org_id=ORG_ID
                )
            )
            tx.commit()
    finally:
        completion.close()


def test_알_수_없는_source_kind_conflict는_처분을_거부한다(tmp_path: Path) -> None:
    # _MANAGER_SOURCE_KIND에는 있으나 S4.4가 열지 않은 "conflict" source — 가드 회귀 앵커.
    completion = _prepared(tmp_path)
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_manager_items SET source_kind='conflict' WHERE manager_item_id=?",
                (ITEM_REF,),
            )
            tx.commit()
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_command())
        assert authority.calls == 0
    finally:
        completion.close()


def test_source_kind와_public_kind_불일치는_conflict이고_write_0이다(tmp_path: Path) -> None:
    # item을 deadlock으로 바꾸면 Request public_kind(unowned)와 어긋난다 — 결박 가드.
    completion = _prepared(tmp_path)
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_manager_items SET source_kind='deadlock' WHERE manager_item_id=?",
                (ITEM_REF,),
            )
            tx.commit()
        uow = _uow(completion, authority=_Authority())
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_command())
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 0 for count in _row_counts(tx).values())
            tx.commit()
    finally:
        completion.close()


def test_request의_item_id_결박이_어긋나면_conflict이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path, state_item_id=_ref("manager", "other-item"))
    try:
        uow = _uow(completion, authority=_Authority())
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_command())
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_replay는_저장_상태가_손상되면_unavailable이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        uow = _uow(completion, authority=_Authority())
        command = _command()
        uow.act(principal=_principal(), command=command)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_manager_items SET status='open' WHERE manager_item_id=?",
                (ITEM_REF,),
            )
            tx.commit()
        with pytest.raises(DurableManagerDispositionUnavailable):
            uow.act(principal=_principal(), command=command)
    finally:
        completion.close()


def test_dismissed_item에_다른_command는_conflict이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        uow.act(principal=_principal(), command=_command())
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_command(rationale="different rationale"))

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _row_counts(tx).values())
            tx.commit()
    finally:
        completion.close()


def test_1인칭_위반은_conflict이고_write_0이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal("someone-else"), command=_command())
        _assert_no_writes(completion)
        assert authority.calls == 0
    finally:
        completion.close()


@pytest.mark.parametrize("revision", [0, 2])
def test_stale_revision은_conflict이고_write_0이다(tmp_path: Path, revision: int) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(
                principal=_principal(), command=_command(expected_request_revision=revision)
            )
        _assert_no_writes(completion)
        assert authority.calls == 0
    finally:
        completion.close()


def test_rationale은_raw로_지속되지_않고_digest에만_반영된다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        secret_rationale = "internal-only-manager-note-zzz"
        authority = _Authority()
        uow = _uow(completion, authority=authority)
        uow.act(principal=_principal(), command=_command(rationale=secret_rationale))

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            for table in (
                "durable_linked_command_receipts",
                "durable_linked_audit_intents",
                "durable_linked_outbox_intents",
                "durable_linked_manager_items",
            ):
                for row in tx.execute(f"SELECT * FROM {table}").fetchall():
                    assert secret_rationale not in tuple(row)
            tx.commit()
    finally:
        completion.close()


@pytest.mark.parametrize("deny_at_call", [1])
def test_denied_central_authorization_conflicts_with_write_zero(
    tmp_path: Path, deny_at_call: int
) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority(deny_at_call=deny_at_call)
        uow = _uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_command())
        _assert_no_writes(completion)
    finally:
        completion.close()


_FAULT_POINTS = (
    "after_receipt",
    "after_audit_intent",
    "after_outbox_intent",
    "after_manager_item",
    "after_request",
)


@pytest.mark.parametrize("point", _FAULT_POINTS)
def test_각_fault_point는_전체_dismiss_artifact를_롤백한다(tmp_path: Path, point: str) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()

        def raise_at_point(injected: str) -> None:
            if injected == point:
                raise RuntimeError(injected)

        uow = _uow(completion, authority=authority, fault_injector=raise_at_point)
        with pytest.raises(RuntimeError, match=point):
            uow.act(principal=_principal(), command=_command())
        _assert_no_writes(completion)

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
# S4.4c — FromUnowned Assign UoW
# ---------------------------------------------------------------------------


def test_fresh_assign_commits_item_request_and_receipt_graph_atomically(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        registry = _AssignRegistry(agent_id="card-a", requires_approval=False)
        uow = _uow(completion, authority=authority, registry=registry)
        result = uow.act(principal=_principal(), command=_assign_command())
        assert isinstance(result, DurableManagerOwnerAssigned)
        assert result.item_id == ITEM_REF
        assert result.request_id == REQUEST_ID
        assert result.request_revision == 2
        assert result.agent_id == "card-a"

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _row_counts(tx).values())
            item = tx.execute(
                "SELECT * FROM durable_linked_manager_items WHERE manager_item_id=?", (ITEM_REF,)
            ).fetchone()
            assert item["status"] == "resolved"
            receipt = tx.execute(
                "SELECT * FROM durable_linked_command_receipts WHERE request_id=?", (REQUEST_ID,)
            ).fetchone()
            assert receipt["receipt_id"] == result.receipt_id
            assert receipt["action"] == "manager.assign_owner"
            assert receipt["target_kind"] == "manager_item"
            assert receipt["target_ref"] == ITEM_REF
            assert receipt["principal_id"] == _ref("subject", "manager-1")
            assert receipt["expected_request_revision"] == 1
            audit = tx.execute(
                "SELECT * FROM durable_linked_audit_intents WHERE receipt_id=?",
                (receipt["receipt_id"],),
            ).fetchone()
            outbox = tx.execute(
                "SELECT * FROM durable_linked_outbox_intents WHERE receipt_id=?",
                (receipt["receipt_id"],),
            ).fetchone()
            assert audit["command_digest"] == receipt["command_digest"] == outbox["command_digest"]
            assert audit["action"] == receipt["action"] == "manager.assign_owner"
            assert outbox["kind"] == "linked_aggregate_outbox"
            tx.commit()

        request = completion.get(REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, ReadyToDispatch)
        assert request.state.route.intent == "refund"
        assert request.state.route.agent_id == "card-a"
        assert request.state.route.requires_approval is False
        assert request.state.route.authority_version == "v1"
        assert request.state.attempt == 1
        assert request.state.trigger_key == result.receipt_id
        assert request.state.handling.kind == "system"
        assert request.state.handling.ref == result.receipt_id
        assert request.revision == 2
        assert authority.calls == 1
        assert registry.calls == 1
    finally:
        completion.close()


def test_assign은_requires_approval을_registry_target대로_route에_반영한다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _AssignRegistry(agent_id="card-a", requires_approval=True)
        uow = _uow(completion, authority=_Authority(), registry=registry)
        uow.act(principal=_principal(), command=_assign_command())
        request = completion.get(REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, ReadyToDispatch)
        assert request.state.route.requires_approval is True
    finally:
        completion.close()


def test_registry가_command과_다른_agent_id를_돌려주면_conflict이고_write_0이다(
    tmp_path: Path,
) -> None:
    # Registry 계약 위반(command.agent_id와 다른 target)을 fail-closed로 막는다
    # — fresh/replay가 서로 다른 agent_id로 발산하는 것을 방지.
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        registry = _AssignRegistry(agent_id="card-different")
        uow = _uow(completion, authority=authority, registry=registry)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_assign_command(agent_id="card-a"))
        _assert_no_writes(completion)
        assert authority.calls == 1
        assert registry.calls == 1
    finally:
        completion.close()


def test_intent이_없으면_assign은_conflict이고_dismiss는_허용된다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path, intent=None)
    try:
        authority = _Authority()
        registry = _AssignRegistry()
        uow = _uow(completion, authority=authority, registry=registry)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_assign_command())
        assert authority.calls == 0
        assert registry.calls == 0
        _assert_no_writes(completion)

        dismiss_uow = _uow(completion, authority=_Authority())
        outcome = dismiss_uow.act(principal=_principal(), command=_command())
        assert isinstance(outcome, DurableManagerDismissed)
    finally:
        completion.close()


def test_replay_assign_returns_stored_result_without_central_reauthorization(
    tmp_path: Path,
) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        registry = _AssignRegistry(agent_id="card-a")
        uow = _uow(completion, authority=authority, registry=registry)
        command = _assign_command()
        first = uow.act(principal=_principal(), command=command)
        assert authority.calls == 1
        assert registry.calls == 1
        second = uow.act(principal=_principal(), command=command)
        assert second == first
        assert authority.calls == 1
        assert registry.calls == 1

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _row_counts(tx).values())
            tx.commit()
    finally:
        completion.close()


def test_resolved_item에_다른_assign_command는_conflict이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _AssignRegistry(agent_id="card-a")
        uow = _uow(completion, authority=_Authority(), registry=registry)
        uow.act(principal=_principal(), command=_assign_command())
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_assign_command(agent_id="card-b"))

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _row_counts(tx).values())
            tx.commit()
    finally:
        completion.close()


def test_resolved_item에_rationale만_다른_assign_command는_conflict이다(tmp_path: Path) -> None:
    # digest는 command-local(rationale_sha256 포함)이라 rationale만 달라도 다른
    # command로 잡힌다 — Dismiss의 동형 회귀 앵커(rationale mismatch 결박).
    completion = _prepared(tmp_path)
    try:
        registry = _AssignRegistry(agent_id="card-a")
        uow = _uow(completion, authority=_Authority(), registry=registry)
        uow.act(principal=_principal(), command=_assign_command())
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(
                principal=_principal(),
                command=_assign_command(rationale="different rationale"),
            )
    finally:
        completion.close()


def test_resolved_item에_dismiss_command는_conflict이다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        registry = _AssignRegistry(agent_id="card-a")
        uow = _uow(completion, authority=_Authority(), registry=registry)
        uow.act(principal=_principal(), command=_assign_command())
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_command())
    finally:
        completion.close()


@pytest.mark.parametrize("deny_at_call", [1])
def test_assign_denied_central_authorization_conflicts_with_write_zero(
    tmp_path: Path, deny_at_call: int
) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority(deny_at_call=deny_at_call)
        registry = _AssignRegistry()
        uow = _uow(completion, authority=authority, registry=registry)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_assign_command())
        _assert_no_writes(completion)
        assert registry.calls == 0
    finally:
        completion.close()


@pytest.mark.parametrize("point", _FAULT_POINTS)
def test_각_fault_point는_전체_assign_artifact를_롤백한다(tmp_path: Path, point: str) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        registry = _AssignRegistry(agent_id="card-a")

        def raise_at_point(injected: str) -> None:
            if injected == point:
                raise RuntimeError(injected)

        uow = _uow(
            completion, authority=authority, registry=registry, fault_injector=raise_at_point
        )
        with pytest.raises(RuntimeError, match=point):
            uow.act(principal=_principal(), command=_assign_command())
        _assert_no_writes(completion)

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


def test_assign_rationale은_raw로_지속되지_않고_digest에만_반영된다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        secret_rationale = "internal-only-assign-note-zzz"
        registry = _AssignRegistry(agent_id="card-a")
        uow = _uow(completion, authority=_Authority(), registry=registry)
        uow.act(
            principal=_principal(), command=_assign_command(rationale=secret_rationale)
        )

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            for table in (
                "durable_linked_command_receipts",
                "durable_linked_audit_intents",
                "durable_linked_outbox_intents",
                "durable_linked_manager_items",
            ):
                for row in tx.execute(f"SELECT * FROM {table}").fetchall():
                    assert secret_rationale not in tuple(row)
            tx.commit()
    finally:
        completion.close()


# ---------------------------------------------------------------------------
# S4.4d — FromDeadlock Dismiss UoW (ADR 0065 §11): 실 c.3 baseline·evidence
# 결박·Case 불변·S4.3d reconciliation
# ---------------------------------------------------------------------------

_DEADLOCK_CAUSE_EVALUATED_AT = "2026-07-22T00:00:00+00:00"
_DEADLOCK_SNAPSHOT_SHA = "1" * 64
_DEADLOCK_BASELINE_SHA = "2" * 64
_DEADLOCK_CLAIM_SHA = "3" * 64
_DEADLOCK_VOTE_SHA = "4" * 64
_DEADLOCK_CANDIDATE_DIGEST = "5" * 64
_DEADLOCK_GRAPH_DIGEST = "6" * 64

DEADLOCK_ORG_ID = _ref("org", "org-2")
DEADLOCK_REQUEST_ID = _ref("request", "request-2")
DEADLOCK_CONFLICT_ID = _ref("conflict", "case-2")
DEADLOCK_MANAGER_SUBJECT_REF = _ref("subject", "manager-2")
DEADLOCK_ROOT_SUBJECT_REF = _ref("subject", "root-2")


class _EscalationCauseReader:
    def __init__(self, cause: DivergentVotes) -> None:
        self._cause = cause

    def read_sealed(self, *, org_id: str, conflict_id: str, claims: object) -> DivergentVotes:
        return self._cause


class _EscalationGraphReader:
    def __init__(self, snapshot: ConflictEscalationRegistrySnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self, *, org_id: str, claims: object) -> ConflictEscalationRegistrySnapshot:
        return self._snapshot

    def verify_current(
        self, snapshot: ConflictEscalationRegistrySnapshot, *, claims: object
    ) -> None:
        return None


class _EscalationAuthority:
    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,
            resource=resource,
            roles=("operator",),
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


class _EscalationApprovalProvider:
    def __init__(self, evidence: ConflictEscalationApprovalEvidence) -> None:
        self._evidence = evidence

    def acquire_escalate_approval(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        resource: ResourceRef,
        command_digest: str,
    ) -> ConflictEscalationApprovalEvidence:
        return self._evidence


class _EscalationApprovalResolver:
    def __init__(self, evidence: ConflictEscalationApprovalEvidence) -> None:
        self._evidence = evidence

    def resolve_escalation_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> ConflictEscalationApprovalEvidence | None:
        return self._evidence


def _deadlock_graph() -> ConflictEscalationRegistrySnapshot:
    candidate = ConflictEscalationSnapshotCandidate(
        ordinal=1,
        card_ref=_ref("card", "card-x"),
        owner_subject_ref=_ref("subject", "owner-x"),
        domain_ref=_ref("domain", "refund"),
        route_sha256="8" * 64,
        under_claim=True,
    )
    path = ConflictEscalationOwnerPath(
        owner_subject_ref=_ref("subject", "owner-x"),
        path_subject_refs=(DEADLOCK_MANAGER_SUBJECT_REF, DEADLOCK_ROOT_SUBJECT_REF),
    )
    return ConflictEscalationRegistrySnapshot(
        org_id=DEADLOCK_ORG_ID,
        candidates=(candidate,),
        owner_paths=(path,),
        manager_subject_ref=DEADLOCK_MANAGER_SUBJECT_REF,
        root_subject_ref=DEADLOCK_ROOT_SUBJECT_REF,
        candidate_digest=_DEADLOCK_CANDIDATE_DIGEST,
        claim_digest=_DEADLOCK_CLAIM_SHA,
        graph_digest=_DEADLOCK_GRAPH_DIGEST,
    )


def _deadlock_cause() -> DivergentVotes:
    return DivergentVotes(
        org_ref=DEADLOCK_ORG_ID,
        conflict_ref=DEADLOCK_CONFLICT_ID,
        request_ref=DEADLOCK_REQUEST_ID,
        awaiting_revision=0,
        concurrence_round=1,
        candidate_snapshot_sha256=_DEADLOCK_SNAPSHOT_SHA,
        candidate_owner_count=2,
        baseline_sha256=_DEADLOCK_BASELINE_SHA,
        candidate_claim_sha256=_DEADLOCK_CLAIM_SHA,
        vote_set_sha256=_DEADLOCK_VOTE_SHA,
        evaluated_at=_DEADLOCK_CAUSE_EVALUATED_AT,
    )


def _deadlock_completion(
    tmp_path: Path, *, name: str = "deadlock.sqlite"
) -> tuple[Path, SqliteQuestionCompletionUnitOfWork]:
    path = tmp_path / name
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: NOW,
    )
    return path, completion


def _deadlock_baseline(
    completion: SqliteQuestionCompletionUnitOfWork,
) -> DurableConflictEscalationResult:
    """c.3 escalate UoW를 실제로 커밋해 FromDeadlock ManagerItem을 만든다(수동 조립 금지)."""
    received = QuestionRequest.receive(
        org_id=DEADLOCK_ORG_ID,
        requester_id="user",
        question="deadlock question",
        request_id_factory=lambda: DEADLOCK_REQUEST_ID,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(received)
    conflict_request = received.record_initial_routing(
        intent="refund",
        disposition="contested",
        target=AwaitingConflict(
            case_id=DEADLOCK_CONFLICT_ID,
            handling=HandlingAssignment(
                kind="conflict_case", ref=DEADLOCK_CONFLICT_ID, due_at=NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set(DEADLOCK_REQUEST_ID, 0, received, conflict_request)
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,?,?,?,?)",
            (
                DEADLOCK_CONFLICT_ID,
                DEADLOCK_ORG_ID,
                DEADLOCK_REQUEST_ID,
                0,
                "open",
                _DEADLOCK_SNAPSHOT_SHA,
                NOW.isoformat(),
            ),
        )
        tx.commit()

    claims = (
        ConflictOpenCandidateClaim(
            card_id="card-x",
            intent="refund",
            route=RouteTarget(intent="refund", agent_id="card-x", requires_approval=False),
        ),
    )
    command = DurableConflictEscalateCommand(DEADLOCK_CONFLICT_ID, DEADLOCK_REQUEST_ID, 1, claims)
    resource = ResourceRef(
        org_id=DEADLOCK_ORG_ID, kind="conflict_case", resource_id=DEADLOCK_CONFLICT_ID
    )
    canonical_command = {
        "conflict_id": DEADLOCK_CONFLICT_ID,
        "request_id": DEADLOCK_REQUEST_ID,
        "expected_request_revision": 1,
    }
    command_digest = canonical_escalate_command_digest(resource=resource, command=canonical_command)
    cause = _deadlock_cause()
    graph = _deadlock_graph()
    evidence = ConflictEscalationApprovalEvidence(
        evidence_id="deadlock-evidence-1",
        status="granted",
        action=ESCALATE_ACTION,
        command_digest=command_digest,
        resource_fingerprint=escalation_resource_fingerprint(resource),
        escalation_cause_digest=escalation_cause_digest(cause),
        graph_selection_digest=graph.graph_digest,
    )
    escalation_uow = DurableConflictEscalationUnitOfWork(
        completion=completion,
        cause_reader=_EscalationCauseReader(cause),
        graph_reader=_EscalationGraphReader(graph),
        central_authorizer=_EscalationAuthority(),
        approval_provider=_EscalationApprovalProvider(evidence),
        approval_resolver=_EscalationApprovalResolver(evidence),
        clock=lambda: NOW,
        receipt_id_factory=lambda: "deadlock-receipt-1",
        manager_item_id_factory=lambda: "deadlock-manager-item-1",
    )
    operator_principal = AuthenticatedPrincipal(
        org_id=DEADLOCK_ORG_ID,
        subject_id="operator-1",
        identity_provider="idp",
        identity_session_id="s1",
    )
    result = escalation_uow.escalate(principal=operator_principal, command=command)
    assert isinstance(result, DurableConflictEscalatedToManager)
    return result


def _deadlock_principal(subject_id: str = "manager-2") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=DEADLOCK_ORG_ID,
        subject_id=subject_id,
        identity_provider="idp",
        identity_session_id="s1",
    )


def _deadlock_dismiss_command(
    result: DurableConflictEscalationResult, *, expected_request_revision: int = 2
) -> DurableManagerDismissCommand:
    return DurableManagerDismissCommand(
        result.manager_item_id, DEADLOCK_REQUEST_ID, expected_request_revision
    )


def _deadlock_assign_command(
    result: DurableConflictEscalationResult,
    *,
    agent_id: str = "card-x",
    expected_request_revision: int = 2,
) -> DurableManagerAssignCommand:
    return DurableManagerAssignCommand(
        result.manager_item_id, DEADLOCK_REQUEST_ID, agent_id, expected_request_revision
    )


def _deadlock_uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    authority: _Authority,
    registry: DurableManagerRegistry | None = None,
    fault_injector: Callable[[str], None] | None = None,
    receipt_id: str = "disposition-receipt-1",
) -> DurableManagerDispositionUnitOfWork:
    return DurableManagerDispositionUnitOfWork(
        completion=completion,
        registry=registry or _Registry(),
        central_authorizer=authority,
        clock=lambda: NOW,
        receipt_id_factory=lambda: receipt_id,
        fault_injector=fault_injector,
    )


def _assert_no_deadlock_disposition_writes(
    completion: SqliteQuestionCompletionUnitOfWork, item_id: str
) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        assert all(count == 0 for count in _row_counts(tx).values())
        item = tx.execute(
            "SELECT status FROM durable_linked_manager_items WHERE manager_item_id=?", (item_id,)
        ).fetchone()
        assert item["status"] == "open"
        request = tx.select_question_request(DEADLOCK_REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, AwaitingManager) and request.revision == 2
        case = tx.execute(
            "SELECT status,awaiting_revision FROM durable_linked_conflict_cases WHERE conflict_id=?",
            (DEADLOCK_CONFLICT_ID,),
        ).fetchone()
        assert case["status"] == "escalated" and case["awaiting_revision"] == 0
        tx.commit()


def test_fromdeadlock_dismiss는_item과_request를_처분하고_case는_불변이다(tmp_path: Path) -> None:
    _, completion = _deadlock_completion(tmp_path)
    try:
        result = _deadlock_baseline(completion)
        authority = _Authority()
        uow = _deadlock_uow(completion, authority=authority)
        outcome = uow.act(
            principal=_deadlock_principal(), command=_deadlock_dismiss_command(result)
        )
        assert isinstance(outcome, DurableManagerDismissed)
        assert outcome.item_id == result.manager_item_id
        assert outcome.request_revision == 3
        assert authority.calls == 1

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            item = tx.execute(
                "SELECT status FROM durable_linked_manager_items WHERE manager_item_id=?",
                (result.manager_item_id,),
            ).fetchone()
            assert item["status"] == "dismissed"
            case = tx.execute(
                "SELECT status,awaiting_revision FROM durable_linked_conflict_cases WHERE conflict_id=?",
                (DEADLOCK_CONFLICT_ID,),
            ).fetchone()
            assert case["status"] == "escalated" and case["awaiting_revision"] == 0
            tx.commit()

        request = completion.get(DEADLOCK_REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, DeclinedRequest)
        assert request.state.reason_code == "manager_declined"
        assert request.revision == 3
    finally:
        completion.close()


def test_fromdeadlock_assign은_item과_request를_처분하고_case는_불변이며_reconciliation은_capable하다(
    tmp_path: Path,
) -> None:
    path, completion = _deadlock_completion(tmp_path)
    try:
        result = _deadlock_baseline(completion)
        authority = _Authority()
        registry = _AssignRegistry(agent_id="card-x", requires_approval=False)
        uow = _deadlock_uow(completion, authority=authority, registry=registry)
        outcome = uow.act(
            principal=_deadlock_principal(), command=_deadlock_assign_command(result)
        )
        assert isinstance(outcome, DurableManagerOwnerAssigned)
        assert outcome.item_id == result.manager_item_id
        assert outcome.agent_id == "card-x"
        assert outcome.request_revision == 3
        assert authority.calls == 1
        assert registry.calls == 1

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            item = tx.execute(
                "SELECT status FROM durable_linked_manager_items WHERE manager_item_id=?",
                (result.manager_item_id,),
            ).fetchone()
            assert item["status"] == "resolved"
            case = tx.execute(
                "SELECT status,awaiting_revision FROM durable_linked_conflict_cases WHERE conflict_id=?",
                (DEADLOCK_CONFLICT_ID,),
            ).fetchone()
            assert case["status"] == "escalated" and case["awaiting_revision"] == 0
            tx.commit()

        request = completion.get(DEADLOCK_REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, ReadyToDispatch)
        assert request.state.route.agent_id == "card-x"
        assert request.state.route.intent == "refund"
        assert request.state.route.requires_approval is False
        assert request.state.attempt == 1
        assert request.state.trigger_key == outcome.receipt_id
        assert request.revision == 3
    finally:
        completion.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=DEADLOCK_ORG_ID)
    assert report.capable is True
    assert report.violations == ()


def test_fromdeadlock_assign은_evidence_source_ref_위조시_conflict이고_write_0이다(
    tmp_path: Path,
) -> None:
    # deadlock evidence 결박(``_verify_deadlock_evidence``)이 Assign에서도
    # authorize/registry보다 먼저 실행됨을 확인하는 회귀 앵커.
    _, completion = _deadlock_completion(tmp_path)
    try:
        result = _deadlock_baseline(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_manager_items SET source_ref=? WHERE manager_item_id=?",
                (_ref("source", "forged-conflict"), result.manager_item_id),
            )
            tx.commit()
        authority = _Authority()
        registry = _AssignRegistry(agent_id="card-x")
        uow = _deadlock_uow(completion, authority=authority, registry=registry)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(
                principal=_deadlock_principal(), command=_deadlock_assign_command(result)
            )
        assert authority.calls == 0
        assert registry.calls == 0
        _assert_no_deadlock_disposition_writes(completion, result.manager_item_id)
    finally:
        completion.close()


def test_fromdeadlock_처분은_escalation_receipt가_없으면_conflict이고_case는_불변이다(
    tmp_path: Path,
) -> None:
    _, completion = _deadlock_completion(tmp_path)
    try:
        result = _deadlock_baseline(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            receipt = tx.execute(
                "SELECT receipt_id FROM durable_conflict_escalation_receipts WHERE org_id=? AND request_id=?",
                (DEADLOCK_ORG_ID, DEADLOCK_REQUEST_ID),
            ).fetchone()
            for table in (
                "durable_conflict_escalation_evidence",
                "durable_conflict_escalation_result_projections",
                "durable_conflict_escalation_audit_intents",
                "durable_conflict_escalation_outbox_intents",
                "durable_conflict_escalation_receipts",
            ):
                tx.execute(f"DELETE FROM {table} WHERE receipt_id=?", (receipt["receipt_id"],))
            tx.commit()
        authority = _Authority()
        uow = _deadlock_uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_deadlock_principal(), command=_deadlock_dismiss_command(result))
        assert authority.calls == 0
        _assert_no_deadlock_disposition_writes(completion, result.manager_item_id)
    finally:
        completion.close()


def test_fromdeadlock_처분은_item_source_ref가_escalation_receipt와_어긋나면_conflict이다(
    tmp_path: Path,
) -> None:
    _, completion = _deadlock_completion(tmp_path)
    try:
        result = _deadlock_baseline(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_manager_items SET source_ref=? WHERE manager_item_id=?",
                (_ref("source", "different-conflict"), result.manager_item_id),
            )
            tx.commit()
        authority = _Authority()
        uow = _deadlock_uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_deadlock_principal(), command=_deadlock_dismiss_command(result))
        assert authority.calls == 0
        _assert_no_deadlock_disposition_writes(completion, result.manager_item_id)
    finally:
        completion.close()


def test_fromdeadlock_처분은_escalation_receipt_awaiting_revision이_어긋나면_conflict이다(
    tmp_path: Path,
) -> None:
    _, completion = _deadlock_completion(tmp_path)
    try:
        result = _deadlock_baseline(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_manager_items SET awaiting_revision=1 WHERE manager_item_id=?",
                (result.manager_item_id,),
            )
            tx.commit()
        authority = _Authority()
        uow = _deadlock_uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_deadlock_principal(), command=_deadlock_dismiss_command(result))
        assert authority.calls == 0
        _assert_no_deadlock_disposition_writes(completion, result.manager_item_id)
    finally:
        completion.close()


def test_fromdeadlock_item의_source_kind가_unowned로_바뀌면_public_kind_불일치로_conflict이다(
    tmp_path: Path,
) -> None:
    # b의 unowned→deadlock 결박 회귀 앵커와 대칭: deadlock item이 unowned로
    # 위조되면 contested Request와 어긋나 write 0으로 거부돼야 한다.
    _, completion = _deadlock_completion(tmp_path)
    try:
        result = _deadlock_baseline(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_linked_manager_items SET source_kind='unowned' WHERE manager_item_id=?",
                (result.manager_item_id,),
            )
            tx.commit()
        authority = _Authority()
        uow = _deadlock_uow(completion, authority=authority)
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_deadlock_principal(), command=_deadlock_dismiss_command(result))
        assert authority.calls == 0
    finally:
        completion.close()


def test_fromdeadlock_dismiss_처분_후_s4_3d_reconciliation은_capable하다(tmp_path: Path) -> None:
    path, completion = _deadlock_completion(tmp_path)
    try:
        result = _deadlock_baseline(completion)
        uow = _deadlock_uow(completion, authority=_Authority())
        uow.act(principal=_deadlock_principal(), command=_deadlock_dismiss_command(result))
    finally:
        completion.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=DEADLOCK_ORG_ID)
    assert report.capable is True
    assert report.violations == ()


# ---------------------------------------------------------------------------
# P2-5 — 실 SnapshotCentralAuthorizer + 실(durable row 재조회) ManagerActItemResolver
# 배선 통합 테스트(Fake authorizer 아님) — happy·deny 각 1건
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DurableManagerActItemResolver:
    """durable ``manager_items``·Question Request를 그때그때 재조회하는 실 resolver."""

    completion: SqliteQuestionCompletionUnitOfWork
    org_id: str

    def resolve_manager_act_item(self, *, item_id: str) -> ManagerActItemSnapshot | None:
        # 호출자(manager.act authorize)가 이미 durable write transaction 안에
        # 있으므로(``begin_immediate`` 기존 소유), 여기서는 새 transaction을 열지
        # 않고 같은 shared scope에 재진입해 그 transaction을 그대로 재사용한다.
        tx = self.completion.durable_transaction()
        with tx.scope():
            row = tx.execute(
                "SELECT * FROM durable_linked_manager_items WHERE manager_item_id=? AND org_id=?",
                (item_id, self.org_id),
            ).fetchone()
            request = None if row is None else tx.select_question_request(row["request_id"])
        if (
            row is None
            or row["status"] != "open"
            or request is None
            or not isinstance(request.state, AwaitingManager)
        ):
            return None
        return ManagerActItemSnapshot(
            org_id=self.org_id,
            item_id=item_id,
            manager_subject_ref=row["manager_subject_id"],
            state_kind="open",
            request_state_kind="awaiting_manager",
        )


def _manager_act_policy_snapshot(
    *, org_id: str, subject_id: str, grant: bool
) -> AuthorityPolicySnapshot:
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": org_id,
        "policy_version": "policy-manager-act-v1",
        "content_sha256": "pending",
        "subject_roles": (
            [{"org_id": org_id, "subject_id": subject_id, "roles": ["manager"]}] if grant else []
        ),
        "role_permissions": [{"role": "manager", "actions": ["manager.act"]}],
        "route_rules": [],
        "worker_bindings": [],
    }
    document["content_sha256"] = canonical_policy_digest(document)
    return load_authority_policy_yaml(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), expected_org_id=org_id
    )


def test_real_snapshotcentralauthorizer와_real_resolver로_manager_act가_승인된다(
    tmp_path: Path,
) -> None:
    completion = _prepared(tmp_path)
    try:
        resolver = _DurableManagerActItemResolver(completion, ORG_ID)
        snapshot = _manager_act_policy_snapshot(org_id=ORG_ID, subject_id="manager-1", grant=True)
        authorizer = SnapshotCentralAuthorizer(snapshot, manager_act_item_resolver=resolver)
        uow = DurableManagerDispositionUnitOfWork(
            completion=completion,
            registry=_Registry(),
            central_authorizer=authorizer,
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt-real-1",
        )
        result = uow.act(principal=_principal(), command=_command())
        assert isinstance(result, DurableManagerDismissed)
    finally:
        completion.close()


def test_real_snapshotcentralauthorizer와_real_resolver로_manager_act_assign이_승인된다(
    tmp_path: Path,
) -> None:
    completion = _prepared(tmp_path)
    try:
        resolver = _DurableManagerActItemResolver(completion, ORG_ID)
        snapshot = _manager_act_policy_snapshot(org_id=ORG_ID, subject_id="manager-1", grant=True)
        authorizer = SnapshotCentralAuthorizer(snapshot, manager_act_item_resolver=resolver)
        registry = _AssignRegistry(agent_id="card-a")
        uow = DurableManagerDispositionUnitOfWork(
            completion=completion,
            registry=registry,
            central_authorizer=authorizer,
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt-real-assign-1",
        )
        result = uow.act(principal=_principal(), command=_assign_command())
        assert isinstance(result, DurableManagerOwnerAssigned)
        assert result.agent_id == "card-a"
    finally:
        completion.close()


def test_real_snapshotcentralauthorizer와_real_resolver로_manager_act가_거부된다(
    tmp_path: Path,
) -> None:
    completion = _prepared(tmp_path)
    try:
        resolver = _DurableManagerActItemResolver(completion, ORG_ID)
        snapshot = _manager_act_policy_snapshot(org_id=ORG_ID, subject_id="manager-1", grant=False)
        authorizer = SnapshotCentralAuthorizer(snapshot, manager_act_item_resolver=resolver)
        uow = DurableManagerDispositionUnitOfWork(
            completion=completion,
            registry=_Registry(),
            central_authorizer=authorizer,
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt-real-2",
        )
        with pytest.raises(DurableManagerDispositionConflict):
            uow.act(principal=_principal(), command=_command())
        _assert_no_writes(completion)
    finally:
        completion.close()
