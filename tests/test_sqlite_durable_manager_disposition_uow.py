from __future__ import annotations
# pyright: reportArgumentType=false

import hashlib
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.question_request import (
    AwaitingManager,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
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
    def __init__(self) -> None:
        self.calls = 0

    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None:
        self.calls += 1
        return None


def _principal(subject_id: str = "manager-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=ORG_ID, subject_id=subject_id, identity_provider="idp", identity_session_id="s1"
    )


def _command(
    *, item_id: str = ITEM_REF, expected_request_revision: int = 1, rationale: str = ""
) -> DurableManagerDismissCommand:
    return DurableManagerDismissCommand(item_id, REQUEST_ID, expected_request_revision, rationale)


def _prepared(
    tmp_path: Path, *, state_item_id: str = ITEM_REF
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
        intent="refund",
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
    registry: _Registry | None = None,
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


def test_act는_assign_command를_s4_4c_전까지_명시_거부한다(tmp_path: Path) -> None:
    completion = _prepared(tmp_path)
    try:
        authority = _Authority()
        registry = _Registry()
        uow = _uow(completion, authority=authority, registry=registry)
        command = DurableManagerAssignCommand(ITEM_REF, REQUEST_ID, "card-a", 1)
        with pytest.raises(DurableManagerDispositionUnavailable):
            uow.act(principal=_principal(), command=command)
        _assert_no_writes(completion)
        assert authority.calls == 0
        assert registry.calls == 0
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
            assert receipt["target_ref"] == _ref("manager", ITEM_REF)
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
