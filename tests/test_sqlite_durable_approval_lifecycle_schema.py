from __future__ import annotations

import sqlite3
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from agent_org_network.sqlite_approval_assignments_v2 import (
    decode_approval_assignment_v2,
    encode_approval_assignment_v2,
    migrate_sqlite_approval_assignments_v2_schema,
)
from agent_org_network.approval import (
    AnswerCandidate,
    ApprovalDraft,
    ApprovalItem,
    ApprovalRequired,
    ApprovalSupersession,
)
from agent_org_network.question_request import AwaitingApproval, HandlingAssignment, RouteTarget
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_approval_lifecycle import (
    SQLITE_DURABLE_APPROVAL_LIFECYCLE_COMPONENT_ID,
    SQLITE_DURABLE_APPROVAL_LIFECYCLE_MIGRATION_FAULT_POINTS,
    DatabaseClock,
    SqliteDurableApprovalLifecycleSchemaError,
    migrate_sqlite_durable_approval_lifecycle_schema,
    open_sqlite_durable_approval_lifecycle_connection,
    reconcile_sqlite_durable_approval_lifecycle_schema,
)


def _parent(path: Path) -> None:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_approval_assignments_v2_schema(path)


def test_lifecycle_component_migrates_only_after_capable_v2_parent(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)

    migrate_sqlite_durable_approval_lifecycle_schema(path)

    connection = open_sqlite_durable_approval_lifecycle_connection(path)
    try:
        marker = connection.execute(
            "SELECT component_id FROM schema_component_manifests WHERE component_id = ?",
            (SQLITE_DURABLE_APPROVAL_LIFECYCLE_COMPONENT_ID,),
        ).fetchone()
        assert marker is not None
        assert marker[0] == SQLITE_DURABLE_APPROVAL_LIFECYCLE_COMPONENT_ID
        receipt = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'durable_approval_lifecycle_receipts'"
        ).fetchone()
        assert receipt is not None
        assert "UNIQUE" in receipt[0]
    finally:
        connection.close()


@pytest.mark.parametrize("point", SQLITE_DURABLE_APPROVAL_LIFECYCLE_MIGRATION_FAULT_POINTS)
def test_lifecycle_migration_fault_rolls_back_every_owned_object(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)

    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_durable_approval_lifecycle_schema(
            path,
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )

    connection = sqlite3.connect(path)
    try:
        for table in (
            "durable_approval_lifecycle_receipts",
            "durable_approval_lifecycle_evidence",
            "durable_approval_lifecycle_results",
            "durable_approval_lifecycle_audit_intents",
            "durable_approval_lifecycle_outbox_intents",
        ):
            assert (
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
                ).fetchone()
                is None
            )
        assert (
            connection.execute(
                "SELECT 1 FROM schema_component_manifests WHERE component_id = ?",
                (SQLITE_DURABLE_APPROVAL_LIFECYCLE_COMPONENT_ID,),
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_runtime_and_reconciliation_reject_catalog_drift_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_approval_lifecycle_schema(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE durable_approval_lifecycle_outbox_intents")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SqliteDurableApprovalLifecycleSchemaError):
        open_sqlite_durable_approval_lifecycle_connection(path)
    report = reconcile_sqlite_durable_approval_lifecycle_schema(path)
    assert not report.capable
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'durable_approval_lifecycle_outbox_intents'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_lifecycle_schema_requires_v2_and_does_not_create_it(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)

    with pytest.raises(SqliteDurableApprovalLifecycleSchemaError):
        migrate_sqlite_durable_approval_lifecycle_schema(path)
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'durable_approval_assignments_v2'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_database_clock_is_transaction_scoped_and_secret_free_contract() -> None:
    class Transaction:
        pass

    class Clock:
        def now(self, transaction: Transaction) -> datetime:
            assert transaction is not None
            return datetime(2026, 7, 16, tzinfo=UTC)

    clock: DatabaseClock[Transaction] = Clock()
    assert clock.now(Transaction()) == datetime(2026, 7, 16, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sealed(value: Mapping[str, object]) -> tuple[str, str]:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return raw, _sha(raw)


def _insert_parent_assignment(connection: sqlite3.Connection) -> None:
    at = datetime(2026, 7, 16, tzinfo=UTC)
    connection.execute(
        "INSERT INTO question_requests (request_id,org_id,requester_id,session_id,question,context_snapshot,intent,initial_disposition,state_kind,state_json,state_schema_version,revision,created_at,updated_at) "
        "VALUES ('request-1','org-1','user',NULL,'q',NULL,'refund','routed','received','{}',1,0,'2026-07-16T00:00:00+00:00','2026-07-16T00:00:00+00:00')"
    )
    route = RouteTarget(
        agent_id="card-1", intent="refund", requires_approval=True, authority_version="v1"
    )
    item = ApprovalItem(
        item_id="assignment-1",
        org_id="org-1",
        request_id="request-1",
        awaiting_revision=1,
        attempt=1,
        route=route,
        draft=ApprovalDraft(
            draft_id="d-1",
            request_id="request-1",
            attempt=1,
            route=route,
            candidate=AnswerCandidate(text="ok"),
            created_at=at,
        ),
        requirement=ApprovalRequired(approver_id="reviewer-1", policy_version="v1"),
        created_at=at,
        due_at=at + timedelta(minutes=5),
    )
    body, digest = encode_approval_assignment_v2(item)
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


def _insert_lifecycle_snapshot(
    path: Path,
    *,
    receipt_action: str,
    evidence_kind: str,
    evidence: Mapping[str, object],
    result_kind: str,
    result: Mapping[str, object],
) -> None:
    command_digest = "a" * 64
    evidence_json, evidence_sha = _sealed(evidence)
    result_json, result_sha = _sealed(result)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_parent_assignment(connection)
        if result_kind == "reassigned":
            predecessor_row = connection.execute(
                "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id='assignment-1'"
            ).fetchone()
            assert predecessor_row is not None
            predecessor = decode_approval_assignment_v2(
                assignment_json=predecessor_row["assignment_json"],
                assignment_sha256=predecessor_row["assignment_sha256"],
                org_id=predecessor_row["org_id"],
                request_id=predecessor_row["request_id"],
            )
            successor_id = str(result["successor_assignment_id"])
            successor_revision = result["successor_awaiting_revision"]
            successor_round = result["successor_approval_round"]
            assert type(successor_revision) is int and type(successor_round) is int
            successor = ApprovalItem(
                item_id=successor_id,
                org_id=predecessor.org_id,
                request_id=predecessor.request_id,
                awaiting_revision=successor_revision,
                attempt=predecessor.attempt,
                route=predecessor.route,
                draft=predecessor.draft,
                requirement=predecessor.requirement,
                created_at=predecessor.created_at,
                due_at=predecessor.due_at,
                approval_round=successor_round,
                supersedes_item_id=predecessor.item_id,
            )
            old = predecessor.supersede(
                ApprovalSupersession(
                    reason="reassigned",
                    successor_item_id=successor.item_id,
                    superseded_at=predecessor.created_at,
                )
            )
            old_json, old_sha = encode_approval_assignment_v2(old)
            successor_json, successor_sha = encode_approval_assignment_v2(successor)
            connection.execute(
                "UPDATE durable_approval_assignments_v2 SET status=?, assignment_json=?, assignment_sha256=? WHERE assignment_id=?",
                (old.status, old_json, old_sha, old.item_id),
            )
            connection.execute(
                "INSERT INTO durable_approval_assignments_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    successor.item_id,
                    successor.org_id,
                    successor.request_id,
                    successor.awaiting_revision,
                    successor.attempt,
                    successor.approval_round,
                    successor.supersedes_item_id,
                    successor.status,
                    successor_json,
                    successor_sha,
                    1,
                ),
            )
            current_state = AwaitingApproval(
                route=successor.route,
                attempt=successor.attempt,
                draft_ref=successor.item_id,
                handling=HandlingAssignment(
                    kind="approval_item",
                    ref=successor.item_id,
                    due_at=successor.due_at,
                ),
            )
            connection.execute(
                "UPDATE question_requests SET state_kind=?, state_json=?, revision=? WHERE request_id='request-1'",
                (
                    current_state.kind,
                    json.dumps(
                        current_state.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                    ),
                    2,
                ),
            )
        connection.execute(
            "INSERT INTO durable_approval_lifecycle_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "receipt-1",
                "org-1",
                "assignment-1",
                "request-1",
                command_digest,
                "operator-1",
                receipt_action,
                1,
                "2026-07-16T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO durable_approval_lifecycle_evidence VALUES (?,?,?,?)",
            ("receipt-1", evidence_kind, evidence_json, evidence_sha),
        )
        connection.execute(
            "INSERT INTO durable_approval_lifecycle_results VALUES (?,?,?,?)",
            ("receipt-1", result_kind, result_json, result_sha),
        )
        for table, kind in (
            ("durable_approval_lifecycle_audit_intents", "lifecycle_audit"),
            ("durable_approval_lifecycle_outbox_intents", "lifecycle_outbox"),
        ):
            connection.execute(
                f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
                (
                    "receipt-1",
                    "org-1",
                    kind if table.endswith("outbox_intents") else receipt_action,
                    "request-1",
                    "assignment-1",
                    command_digest,
                    "2026-07-16T00:00:00+00:00",
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _manual_evidence() -> dict[str, object]:
    return {
        "action": "approval.reassign",
        "authority_digest": "b" * 64,
        "authority_version_digest": "f" * 64,
        "due_at": "2026-07-16T00:05:00+00:00",
        "evidence_digest": "c" * 64,
        "expected_request_revision": 1,
        "org_id": "org-1",
        "policy_digest": "d" * 64,
        "predecessor_assignment_id": "assignment-1",
        "principal_id": "operator-1",
        "request_id": "request-1",
        "target_approver_id": "reviewer-2",
        "target_requirement_digest": "e" * 64,
    }


def _expiry_evidence() -> dict[str, object]:
    return {
        "action": "approval.expire",
        "authority_digest": "b" * 64,
        "database_time": "2026-07-16T00:05:00+00:00",
        "evidence_digest": "c" * 64,
        "expected_request_revision": 1,
        "expiry_policy_digest": "d" * 64,
        "org_id": "org-1",
        "predecessor_assignment_id": "assignment-1",
        "principal_id": "operator-1",
        "request_id": "request-1",
    }


def _reassigned_result(*, action: str) -> dict[str, object]:
    return {
        "action": action,
        "evidence_digest": "c" * 64,
        "expected_request_revision": 1,
        "org_id": "org-1",
        "predecessor_assignment_id": "assignment-1",
        "request_id": "request-1",
        "successor_assignment_id": "assignment-2",
        "successor_approval_round": 2,
        "successor_awaiting_revision": 2,
    }


@pytest.mark.parametrize(
    ("location", "field", "prose"),
    (
        ("manual", "target_approver_id", "reviewer needs human approval"),
        ("manual", "due_at", "승인 기한은 내일 오후입니다"),
        ("expiry", "database_time", "expiry check happened just now"),
        ("result", "successor_assignment_id", "다음 승인자에게 넘겨 주세요"),
    ),
    ids=("manual_target_english", "manual_due_korean", "expiry_time_english", "successor_korean"),
)
def test_lifecycle_correct_hash_prose_scalar_injection_fails_closed_without_repair(
    tmp_path: Path, location: str, field: str, prose: str
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_approval_lifecycle_schema(path)
    evidence_kind = "manual_reassignment" if location != "expiry" else "expiry_reassignment"
    receipt_action = "approval.reassign" if location != "expiry" else "approval.expire"
    evidence = _manual_evidence() if location != "expiry" else _expiry_evidence()
    result = _reassigned_result(action=receipt_action)
    if location in {"manual", "expiry"}:
        evidence[field] = prose
    else:
        result[field] = prose
    _insert_lifecycle_snapshot(
        path,
        receipt_action=receipt_action,
        evidence_kind=evidence_kind,
        evidence=evidence,
        result_kind="reassigned",
        result=result,
    )

    before = path.read_bytes()
    with pytest.raises(SqliteDurableApprovalLifecycleSchemaError):
        open_sqlite_durable_approval_lifecycle_connection(path)
    report = reconcile_sqlite_durable_approval_lifecycle_schema(path)
    assert not report.capable
    assert path.read_bytes() == before


def test_reassigned_result_successor_lineage_tamper_fails_closed_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_approval_lifecycle_schema(path)
    _insert_lifecycle_snapshot(
        path,
        receipt_action="approval.reassign",
        evidence_kind="manual_reassignment",
        evidence=_manual_evidence(),
        result_kind="reassigned",
        result=_reassigned_result(action="approval.reassign"),
    )
    connection = sqlite3.connect(path)
    try:
        result = _reassigned_result(action="approval.reassign")
        result["successor_approval_round"] = 3
        raw, digest = _sealed(result)
        connection.execute(
            "UPDATE durable_approval_lifecycle_results SET result_json=?, result_sha256=?",
            (raw, digest),
        )
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()
    with pytest.raises(SqliteDurableApprovalLifecycleSchemaError):
        open_sqlite_durable_approval_lifecycle_connection(path)
    assert not reconcile_sqlite_durable_approval_lifecycle_schema(path).capable
    assert path.read_bytes() == before


def test_reassigned_successor_route_and_draft_mutation_fails_closed_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_approval_lifecycle_schema(path)
    _insert_lifecycle_snapshot(
        path,
        receipt_action="approval.reassign",
        evidence_kind="manual_reassignment",
        evidence=_manual_evidence(),
        result_kind="reassigned",
        result=_reassigned_result(action="approval.reassign"),
    )
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id='assignment-2'"
        ).fetchone()
        assert row is not None
        successor = decode_approval_assignment_v2(
            assignment_json=row["assignment_json"],
            assignment_sha256=row["assignment_sha256"],
            org_id=row["org_id"],
            request_id=row["request_id"],
        )
        changed_route = RouteTarget(
            agent_id="other-card",
            intent=successor.route.intent,
            requires_approval=True,
            authority_version=successor.route.authority_version,
        )
        changed = successor.model_copy(
            update={
                "route": changed_route,
                "draft": successor.draft.model_copy(update={"route": changed_route}),
            }
        )
        body, digest = encode_approval_assignment_v2(changed)
        connection.execute(
            "UPDATE durable_approval_assignments_v2 SET assignment_json=?, assignment_sha256=? "
            "WHERE assignment_id='assignment-2'",
            (body, digest),
        )
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()
    with pytest.raises(SqliteDurableApprovalLifecycleSchemaError):
        open_sqlite_durable_approval_lifecycle_connection(path)
    assert not reconcile_sqlite_durable_approval_lifecycle_schema(path).capable
    assert path.read_bytes() == before


def test_receipt_and_sealed_revision_mirror_tamper_fails_actual_predecessor_check(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_approval_lifecycle_schema(path)
    evidence = _manual_evidence()
    result = _reassigned_result(action="approval.reassign")
    _insert_lifecycle_snapshot(
        path,
        receipt_action="approval.reassign",
        evidence_kind="manual_reassignment",
        evidence=evidence,
        result_kind="reassigned",
        result=result,
    )
    evidence["expected_request_revision"] = 0
    result["expected_request_revision"] = 0
    evidence_raw, evidence_digest = _sealed(evidence)
    result_raw, result_digest = _sealed(result)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE durable_approval_lifecycle_receipts SET expected_request_revision=0"
        )
        connection.execute(
            "UPDATE durable_approval_lifecycle_evidence SET evidence_json=?, evidence_sha256=?",
            (evidence_raw, evidence_digest),
        )
        connection.execute(
            "UPDATE durable_approval_lifecycle_results SET result_json=?, result_sha256=?",
            (result_raw, result_digest),
        )
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()
    with pytest.raises(SqliteDurableApprovalLifecycleSchemaError):
        open_sqlite_durable_approval_lifecycle_connection(path)
    assert not reconcile_sqlite_durable_approval_lifecycle_schema(path).capable
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "table",
    ("durable_approval_lifecycle_audit_intents", "durable_approval_lifecycle_outbox_intents"),
)
def test_valid_but_different_intent_timestamp_fails_closed_without_repair(
    tmp_path: Path, table: str
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_approval_lifecycle_schema(path)
    _insert_lifecycle_snapshot(
        path,
        receipt_action="approval.reassign",
        evidence_kind="manual_reassignment",
        evidence=_manual_evidence(),
        result_kind="reassigned",
        result=_reassigned_result(action="approval.reassign"),
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"UPDATE {table} SET created_at='2026-07-16T00:00:01+00:00'")
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()
    with pytest.raises(SqliteDurableApprovalLifecycleSchemaError):
        open_sqlite_durable_approval_lifecycle_connection(path)
    assert not reconcile_sqlite_durable_approval_lifecycle_schema(path).capable
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("evidence_kind", "evidence_mutation"),
    (
        ("manual_reassignment", {"question": "raw customer question"}),
        ("not_a_lifecycle_kind", {}),
        ("manual_reassignment", {"org_id": "org-2"}),
    ),
    ids=("raw_body", "arbitrary_kind", "identity_mismatch"),
)
def test_lifecycle_sealed_rows_fail_closed_without_repair(
    tmp_path: Path, evidence_kind: str, evidence_mutation: dict[str, object]
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_approval_lifecycle_schema(path)
    evidence: dict[str, object] = {
        "action": "approval.reassign",
        "authority_digest": "b" * 64,
        "due_at": "2026-07-16T00:05:00+00:00",
        "evidence_digest": "c" * 64,
        "expected_request_revision": 1,
        "org_id": "org-1",
        "policy_digest": "d" * 64,
        "predecessor_assignment_id": "assignment-1",
        "principal_id": "operator-1",
        "request_id": "request-1",
        "target_approver_id": "reviewer-2",
        "target_requirement_digest": "e" * 64,
    }
    evidence.update(evidence_mutation)
    result = {
        "action": "approval.reassign",
        "evidence_digest": "c" * 64,
        "expected_request_revision": 1,
        "org_id": "org-1",
        "predecessor_assignment_id": "assignment-1",
        "request_id": "request-1",
        "successor_assignment_id": "assignment-2",
        "successor_approval_round": 2,
        "successor_awaiting_revision": 2,
    }
    _insert_lifecycle_snapshot(
        path,
        receipt_action="approval.reassign",
        evidence_kind=evidence_kind,
        evidence=evidence,
        result_kind="reassigned",
        result=result,
    )
    before = path.read_bytes()
    with pytest.raises(SqliteDurableApprovalLifecycleSchemaError):
        open_sqlite_durable_approval_lifecycle_connection(path)
    report = reconcile_sqlite_durable_approval_lifecycle_schema(path)
    assert not report.capable
    assert path.read_bytes() == before
