from __future__ import annotations
# pyright: reportArgumentType=false, reportUnknownMemberType=false, reportAttributeAccessIssue=false

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from threading import Event, Thread
from typing import Callable

import pytest

from agent_org_network.answer_finalization import AnswerResponsibilitySnapshot
from agent_org_network.answer_finalization import ReentrantCompletionMutationError
from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.approval import (
    ApprovalDraft,
    ApprovalItem,
    ApprovalRequired,
    AnswerCandidate,
    Approve,
    ApproveWithEdit,
    Reject,
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.question_request import (
    AwaitingApproval,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_approval_assignments_v2 import (
    encode_approval_assignment_v2,
    migrate_sqlite_approval_assignments_v2_schema,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_approval_decisions import (
    DurableApprovalDecisionConflict,
    DurableApprovalDecisionUnavailable,
    DurableApprovalDecisionUnitOfWork,
    migrate_sqlite_durable_approval_decisions_schema,
    reconcile_sqlite_durable_approval_decisions_schema,
)

NOW = datetime(2026, 7, 15, tzinfo=UTC)


class _Policy:
    def evaluate(self, *_: object) -> object:
        raise AssertionError("approval path must not re-evaluate policy")


class _Resolver:
    def resolve(self, *, org_id: str, route: RouteTarget) -> AnswerResponsibilitySnapshot:
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id="owner-1")


class _Authority:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow

    def authorize(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef
    ) -> AuthorizationGrant:
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,
            resource=resource,
            roles=("approver",),
            policy_version="v1",
            policy_digest="0" * 64,
        )  # type: ignore[arg-type]

    def verify(
        self,
        _grant: AuthorizationGrant,
        _principal: AuthenticatedPrincipal,
        _action: str,
        _resource: ResourceRef,
    ) -> bool:
        return self.allow


class _RevokedAtCommitAuthority(_Authority):
    """초기 허용 뒤 commit 직전 동일 ResourceRef 재검증을 거부한다."""

    def __init__(self) -> None:
        super().__init__()
        self.resources: list[ResourceRef] = []

    def verify(
        self,
        grant: AuthorizationGrant,
        _principal: AuthenticatedPrincipal,
        _action: str,
        resource: ResourceRef,
    ) -> bool:
        assert isinstance(grant, AuthorizationGrant)
        assert isinstance(resource, ResourceRef)
        self.resources.append(resource)
        return len(self.resources) == 1


def _prepared(
    tmp_path: Path,
    *,
    fault: str | None = None,
    fault_hook: Callable[[str], None] | None = None,
    authority: _Authority | None = None,
    receipt_id_factory: Callable[[], str] | None = None,
) -> tuple[SqliteQuestionCompletionUnitOfWork, DurableApprovalDecisionUnitOfWork]:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    migrate_sqlite_durable_approval_decisions_schema(db)
    completion = SqliteQuestionCompletionUnitOfWork(
        db,
        policy=_Policy(),
        approvals=object(),
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
        fault_injector=(
            fault_hook
            if fault_hook is not None
            else (
                lambda point: (_ for _ in ()).throw(RuntimeError(point)) if point == fault else None
            )
            if fault is not None
            else None
        ),
    )  # type: ignore[arg-type]
    route = RouteTarget(
        intent="refund", agent_id="refund-card", requires_approval=True, authority_version="v1"
    )
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="secret question",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(received)
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
            attempt=1,
            trigger_key="t",
            handling=HandlingAssignment(kind="system", ref="t", due_at=NOW + timedelta(hours=1)),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set("request-1", 0, received, ready)
    item = ApprovalItem(
        item_id="item-1",
        org_id="org-1",
        request_id="request-1",
        awaiting_revision=2,
        attempt=1,
        route=route,
        draft=ApprovalDraft(
            draft_id="draft",
            request_id="request-1",
            attempt=1,
            route=route,
            candidate=AnswerCandidate(text="secret draft"),
            created_at=NOW,
        ),
        requirement=ApprovalRequired(approver_id="approver-1", policy_version="v1"),
        created_at=NOW,
        due_at=NOW + timedelta(minutes=5),
    )
    awaiting = ready.transition(
        AwaitingApproval(
            route=route,
            attempt=1,
            draft_ref="item-1",
            handling=HandlingAssignment(kind="approval_item", ref="item-1", due_at=item.due_at),
        ),
        clock=lambda: NOW,
    )
    assert completion.compare_and_set("request-1", 1, ready, awaiting)
    body, digest = encode_approval_assignment_v2(item)
    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    connection.execute(
        "INSERT INTO durable_approval_assignments_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            item.item_id,
            item.org_id,
            item.request_id,
            item.awaiting_revision,
            item.attempt,
            item.approval_round,
            item.supersedes_item_id,
            item.status,
            body,
            digest,
            1,
        ),
    )
    connection.commit()
    return completion, DurableApprovalDecisionUnitOfWork(
        completion=completion,
        central_authorizer=authority or _Authority(),
        # Decision and finalization share a production clock.  A deterministic test
        # must preserve that invariant rather than making completion run before approval.
        clock=lambda: NOW,
        receipt_id_factory=receipt_id_factory or (lambda: "receipt-1"),
    )  # type: ignore[arg-type]


def _principal(*, org_id: str = "org-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=org_id, subject_id="approver-1", identity_provider="test", identity_session_id="s"
    )


def test_reject_is_atomic_and_body_free(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    migrate_sqlite_durable_approval_decisions_schema(db)
    completion = SqliteQuestionCompletionUnitOfWork(
        db,
        policy=_Policy(),
        approvals=object(),
        responsibility_resolver=_Resolver(),
        record_id_factory=lambda: "record-1",
        clock=lambda: NOW,
    )  # type: ignore[arg-type]
    route = RouteTarget(
        intent="refund", agent_id="refund-card", requires_approval=True, authority_version="v1"
    )
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="secret question",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(received)
    ready = received.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
            attempt=1,
            trigger_key="t",
            handling=HandlingAssignment(kind="system", ref="t", due_at=NOW + timedelta(hours=1)),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set("request-1", 0, received, ready)
    item = ApprovalItem(
        item_id="item-1",
        org_id="org-1",
        request_id="request-1",
        awaiting_revision=2,
        attempt=1,
        route=route,
        draft=ApprovalDraft(
            draft_id="draft",
            request_id="request-1",
            attempt=1,
            route=route,
            candidate=AnswerCandidate(text="secret draft"),
            created_at=NOW,
        ),
        requirement=ApprovalRequired(approver_id="approver-1", policy_version="v1"),
        created_at=NOW,
        due_at=NOW + timedelta(minutes=5),
    )
    awaiting = ready.transition(
        AwaitingApproval(
            route=route,
            attempt=1,
            draft_ref="item-1",
            handling=HandlingAssignment(kind="approval_item", ref="item-1", due_at=item.due_at),
        ),
        clock=lambda: NOW,
    )
    assert completion.compare_and_set("request-1", 1, ready, awaiting)
    body, digest = encode_approval_assignment_v2(item)
    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    connection.execute(
        "INSERT INTO durable_approval_assignments_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            item.item_id,
            item.org_id,
            item.request_id,
            item.awaiting_revision,
            item.attempt,
            item.approval_round,
            item.supersedes_item_id,
            item.status,
            body,
            digest,
            1,
        ),
    )
    connection.commit()
    uow = DurableApprovalDecisionUnitOfWork(
        completion=completion,
        central_authorizer=_Authority(),
        clock=lambda: NOW + timedelta(seconds=1),
        receipt_id_factory=lambda: "receipt-1",
    )  # type: ignore[arg-type]
    result = uow.decide(
        principal=AuthenticatedPrincipal(
            org_id="org-1",
            subject_id="approver-1",
            identity_provider="test",
            identity_session_id="s",
        ),
        item_id="item-1",
        action=Reject(by_approver="approver-1", reason_code="needs_revision"),
    )
    assert result.reason_code == "needs_revision"
    assert completion.get("request-1").state.kind == "declined"  # type: ignore[union-attr]
    raw = connection.execute(
        "SELECT result_json FROM durable_approval_decision_receipts"
    ).fetchone()[0]
    assert "secret" not in raw


def test_approve_and_exact_replay_complete_one_atomic_workflow(tmp_path: Path) -> None:
    completion, uow = _prepared(tmp_path)
    action = Approve(by_approver="approver-1")

    first = uow.decide(principal=_principal(), item_id="item-1", action=action)
    replay = uow.decide(principal=_principal(), item_id="item-1", action=action)

    assert first == replay
    assert first.record_id == "record-1"
    assert completion.get("request-1").state.kind == "answered"  # type: ignore[union-attr]
    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    assert (
        connection.execute("SELECT count(*) FROM question_completion_receipts").fetchone()[0] == 1
    )
    assert (
        connection.execute("SELECT count(*) FROM durable_approval_decision_receipts").fetchone()[0]
        == 1
    )


def test_approve_with_edit_persists_only_edited_answer_not_command_body_in_intents(
    tmp_path: Path,
) -> None:
    completion, uow = _prepared(tmp_path)
    result = uow.decide(
        principal=_principal(),
        item_id="item-1",
        action=ApproveWithEdit(by_approver="approver-1", edited_text="edited secret answer"),
    )

    assert completion.answer_record(result.record_id).answer_text == "edited secret answer"  # type: ignore[union-attr]
    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    rows = connection.execute(
        "SELECT result_json FROM durable_approval_decision_receipts "
        "UNION ALL SELECT command_digest FROM durable_approval_audit_intents "
        "UNION ALL SELECT command_digest FROM durable_approval_outbox_intents"
    ).fetchall()
    assert all("edited secret answer" not in row[0] for row in rows)


def test_public_durable_decision_read_apis_use_shared_read_scope(tmp_path: Path) -> None:
    _, uow = _prepared(tmp_path)

    item = uow.get("item-1")
    by_attempt = uow.get_by_request_attempt("request-1", 1)
    by_round = uow.get_by_request_attempt_round("request-1", 1, 1)

    assert item is not None
    assert by_attempt == item
    assert by_round == item


def test_different_semantic_command_cannot_reuse_item_receipt(tmp_path: Path) -> None:
    _, uow = _prepared(tmp_path)
    uow.decide(principal=_principal(), item_id="item-1", action=Approve(by_approver="approver-1"))

    with pytest.raises(DurableApprovalDecisionConflict):
        uow.decide(
            principal=_principal(),
            item_id="item-1",
            action=ApproveWithEdit(by_approver="approver-1", edited_text="other"),
        )


def test_authority_is_rechecked_before_exact_receipt_replay(tmp_path: Path) -> None:
    authority = _Authority()
    _, uow = _prepared(tmp_path, authority=authority)
    action = Approve(by_approver="approver-1")
    uow.decide(principal=_principal(), item_id="item-1", action=action)
    authority.allow = False

    with pytest.raises(DurableApprovalDecisionConflict):
        uow.decide(principal=_principal(), item_id="item-1", action=action)


@pytest.mark.parametrize("failure", ["stale", "cross_org", "central_deny"])
def test_stale_scope_or_central_deny_has_no_write(tmp_path: Path, failure: str) -> None:
    authority = _Authority(allow=failure != "central_deny")
    completion, uow = _prepared(tmp_path, authority=authority)
    if failure == "stale":
        current = completion.get("request-1")
        assert current is not None and isinstance(current.state, AwaitingApproval)
        successor = current.reassign_approval(
            previous_item_id="item-1",
            successor_item_id="item-2",
            due_at=NOW + timedelta(minutes=6),
            clock=lambda: NOW + timedelta(seconds=1),
        )
        assert completion.compare_and_set("request-1", current.revision, current, successor)
    principal = _principal(org_id="org-2") if failure == "cross_org" else _principal()

    with pytest.raises(DurableApprovalDecisionConflict):
        uow.decide(principal=principal, item_id="item-1", action=Approve(by_approver="approver-1"))

    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    assert (
        connection.execute("SELECT count(*) FROM durable_approval_decision_receipts").fetchone()[0]
        == 0
    )
    assert (
        connection.execute("SELECT status FROM durable_approval_assignments_v2").fetchone()[0]
        == "open"
    )


def test_completion_write_fault_rolls_back_decision_request_and_all_artifacts(
    tmp_path: Path,
) -> None:
    completion, uow = _prepared(tmp_path, fault="after_answer_record")

    with pytest.raises(RuntimeError, match="after_answer_record"):
        uow.decide(
            principal=_principal(), item_id="item-1", action=Approve(by_approver="approver-1")
        )

    assert completion.get("request-1").state.kind == "awaiting_approval"  # type: ignore[union-attr]
    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    for table in (
        "answer_records",
        "question_completion_receipts",
        "terminal_answer_audits",
        "question_delivery_outbox",
        "durable_approval_decision_receipts",
        "durable_approval_audit_intents",
        "durable_approval_outbox_intents",
    ):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert (
        connection.execute("SELECT status FROM durable_approval_assignments_v2").fetchone()[0]
        == "open"
    )


def test_shared_completion_scope_blocks_reader_until_durable_decision_commits(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()

    def pause_before_decision_receipt() -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "receipt-1"

    completion, uow = _prepared(tmp_path, receipt_id_factory=pause_before_decision_receipt)
    decision_error: list[BaseException] = []
    observed: list[QuestionRequest | None] = []
    reader_started = Event()
    reader_done = Event()

    def decide() -> None:
        try:
            uow.decide(
                principal=_principal(), item_id="item-1", action=Approve(by_approver="approver-1")
            )
        except BaseException as error:  # pragma: no cover - asserted by the parent thread
            decision_error.append(error)

    def read() -> None:
        reader_started.set()
        observed.append(completion.get("request-1"))
        reader_done.set()

    decision_thread = Thread(target=decide)
    decision_thread.start()
    assert entered.wait(timeout=2)
    reader_thread = Thread(target=read)
    reader_thread.start()
    assert reader_started.wait(timeout=2)
    assert not reader_done.wait(timeout=0.1)

    release.set()
    decision_thread.join(timeout=2)
    reader_thread.join(timeout=2)

    assert not decision_thread.is_alive()
    assert not reader_thread.is_alive()
    assert decision_error == []
    assert len(observed) == 1
    assert observed[0] is not None
    assert observed[0].state.kind == "answered"


def test_shared_completion_scope_releases_reader_after_decision_rollback(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()

    def fail_after_partial_completion(point: str) -> None:
        if point == "after_answer_record":
            entered.set()
            assert release.wait(timeout=2)
            raise RuntimeError(point)

    completion, uow = _prepared(tmp_path, fault_hook=fail_after_partial_completion)
    decision_error: list[BaseException] = []
    observed: list[QuestionRequest | None] = []
    reader_started = Event()
    reader_done = Event()

    def decide() -> None:
        try:
            uow.decide(
                principal=_principal(), item_id="item-1", action=Approve(by_approver="approver-1")
            )
        except BaseException as error:
            decision_error.append(error)

    def read() -> None:
        reader_started.set()
        observed.append(completion.get("request-1"))
        reader_done.set()

    decision_thread = Thread(target=decide)
    decision_thread.start()
    assert entered.wait(timeout=2)
    reader_thread = Thread(target=read)
    reader_thread.start()
    assert reader_started.wait(timeout=2)
    assert not reader_done.wait(timeout=0.1)

    release.set()
    decision_thread.join(timeout=2)
    reader_thread.join(timeout=2)

    assert not decision_thread.is_alive()
    assert not reader_thread.is_alive()
    assert len(decision_error) == 1
    assert isinstance(decision_error[0], RuntimeError)
    assert len(observed) == 1
    assert observed[0] is not None
    assert observed[0].state.kind == "awaiting_approval"


def _schema_snapshot(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        return (
            [
                tuple(row)
                for row in connection.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
                )
            ],
            [
                tuple(row)
                for row in connection.execute(
                    "SELECT component_id,schema_version,manifest_json,manifest_sha256 "
                    "FROM schema_component_manifests ORDER BY component_id"
                )
            ],
        )
    finally:
        connection.close()


def test_durable_decision_migration_refuses_tampered_manifest_without_repair(
    tmp_path: Path,
) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    migrate_sqlite_durable_approval_decisions_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE schema_component_manifests SET manifest_json='{}' "
            "WHERE component_id='durable_approval_decisions_v1'"
        )
        connection.commit()
    finally:
        connection.close()
    before = _schema_snapshot(db)

    with pytest.raises(DurableApprovalDecisionUnavailable, match="manifest|catalog"):
        migrate_sqlite_durable_approval_decisions_schema(db)

    assert _schema_snapshot(db) == before


def test_durable_decision_migration_refuses_manifestless_partial_schema(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "CREATE TABLE durable_approval_decision_receipts (receipt_id TEXT PRIMARY KEY)"
        )
        connection.commit()
    finally:
        connection.close()
    before = _schema_snapshot(db)

    with pytest.raises(DurableApprovalDecisionUnavailable, match="partial"):
        migrate_sqlite_durable_approval_decisions_schema(db)

    assert _schema_snapshot(db) == before


def test_runtime_open_refuses_manifest_drift_without_write(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    connection.execute(
        "UPDATE schema_component_manifests SET schema_version=2 "
        "WHERE component_id='durable_approval_decisions_v1'"
    )
    connection.commit()
    before = _schema_snapshot(tmp_path / "workflow.sqlite")

    with pytest.raises(DurableApprovalDecisionUnavailable, match="capability"):
        DurableApprovalDecisionUnitOfWork(
            completion=completion,
            central_authorizer=_Authority(),
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt-2",
        )

    assert _schema_snapshot(tmp_path / "workflow.sqlite") == before


def test_readonly_reconciliation_refuses_manifest_drift_without_write(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    connection.execute(
        "UPDATE schema_component_manifests SET manifest_sha256='0' "
        "WHERE component_id='durable_approval_decisions_v1'"
    )
    connection.commit()
    path = tmp_path / "workflow.sqlite"
    before = _schema_snapshot(path)

    report = reconcile_sqlite_durable_approval_decisions_schema(path)

    assert report.capable is False
    assert report.decision_manifest_present is True
    assert _schema_snapshot(path) == before


def test_commit_time_reauthorization_revocation_leaves_zero_writes(tmp_path: Path) -> None:
    authority = _RevokedAtCommitAuthority()
    completion, uow = _prepared(tmp_path, authority=authority)

    with pytest.raises(DurableApprovalDecisionConflict, match="중앙 권한"):
        uow.decide(
            principal=_principal(), item_id="item-1", action=Approve(by_approver="approver-1")
        )

    assert len(authority.resources) == 2
    assert authority.resources[0] == authority.resources[1]
    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    assert (
        connection.execute("SELECT count(*) FROM durable_approval_decision_receipts").fetchone()[0]
        == 0
    )
    assert (
        connection.execute("SELECT status FROM durable_approval_assignments_v2").fetchone()[0]
        == "open"
    )
    assert completion.get("request-1").state.kind == "awaiting_approval"  # type: ignore[union-attr]


def test_receipt_callback_cannot_read_uncommitted_completion_and_rollback_hides_it(
    tmp_path: Path,
) -> None:
    holder: dict[str, SqliteQuestionCompletionUnitOfWork] = {}
    callback_entered = Event()

    def receipt_id() -> str:
        callback_entered.set()
        with pytest.raises(ReentrantCompletionMutationError, match="uncommitted"):
            holder["completion"].get("request-1")
        raise RuntimeError("receipt callback failure")

    completion, uow = _prepared(tmp_path, receipt_id_factory=receipt_id)
    holder["completion"] = completion

    with pytest.raises(RuntimeError, match="receipt callback failure"):
        uow.decide(
            principal=_principal(), item_id="item-1", action=Approve(by_approver="approver-1")
        )

    assert callback_entered.is_set()
    restored = completion.get("request-1")
    assert restored is not None and restored.state.kind == "awaiting_approval"
    connection = completion._connection  # pyright: ignore[reportPrivateUsage]
    for table in (
        "answer_records",
        "question_completion_receipts",
        "durable_approval_decision_receipts",
    ):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_completion_context_is_thread_bound_and_rejects_direct_cross_thread_interleave(
    tmp_path: Path,
) -> None:
    completion, _ = _prepared(tmp_path)
    transaction = completion.durable_transaction()
    entered = Event()
    attempted = Event()
    errors: list[BaseException] = []

    with transaction.scope():
        transaction.begin_immediate()
        context = transaction.completion_context()

        def direct_cross_thread_call() -> None:
            entered.wait(timeout=1)
            try:
                completion.complete_in_transaction(
                    object(),  # context validation must happen before handoff coercion.
                    transaction_context=context,
                )
            except BaseException as error:
                errors.append(error)
            finally:
                attempted.set()

        worker = Thread(target=direct_cross_thread_call)
        worker.start()
        entered.set()
        assert attempted.wait(timeout=1)
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ReentrantCompletionMutationError)
        transaction.rollback()

    request = completion.get("request-1")
    assert request is not None and request.state.kind == "awaiting_approval"
