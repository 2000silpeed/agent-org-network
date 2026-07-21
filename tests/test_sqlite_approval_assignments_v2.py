from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.approval import (
    AnswerCandidate,
    ApprovalDraft,
    ApprovalItem,
    ApprovalRequired,
    ApprovalSupersession,
)
from agent_org_network.question_request import RouteTarget
from agent_org_network.sqlite_approval_assignments_v2 import (
    SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID,
    SqliteApprovalAssignmentsV2SchemaError,
    encode_approval_assignment_v2,
    migrate_sqlite_approval_assignments_v2_schema,
    open_sqlite_approval_assignments_v2_connection,
    reconcile_sqlite_approval_assignments_v2_schema,
)
from agent_org_network.sqlite_approval import migrate_sqlite_approval_schema
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema


def _item(
    *,
    item_id: str = "item-1",
    attempt: int = 1,
    round: int = 1,
    previous: str | None = None,
    status: str = "open",
    successor: str | None = None,
) -> ApprovalItem:
    at = datetime(2026, 7, 15, tzinfo=UTC)
    route = RouteTarget(
        agent_id="refund-card", intent="refund", requires_approval=True, authority_version="v1"
    )
    item = ApprovalItem(
        item_id=item_id,
        org_id="org-1",
        request_id="request-1",
        awaiting_revision=round,
        attempt=attempt,
        route=route,
        draft=ApprovalDraft(
            draft_id=f"draft-{item_id}",
            request_id="request-1",
            attempt=attempt,
            route=route,
            candidate=AnswerCandidate(text="ok"),
            created_at=at,
        ),
        requirement=ApprovalRequired(approver_id="reviewer-1", policy_version="v1"),
        created_at=at,
        due_at=at + timedelta(minutes=5),
        approval_round=round,
        supersedes_item_id=previous,
    )
    if status == "open":
        return item
    return item.supersede(
        ApprovalSupersession(
            successor_item_id=successor or "next",
            reason="reassigned",
            superseded_at=at + timedelta(minutes=1),
        )
    )


def _parent(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO question_requests (request_id, org_id, requester_id, session_id, question, context_snapshot, intent, initial_disposition, state_kind, state_json, state_schema_version, revision, created_at, updated_at) VALUES ('request-1','org-1','user',NULL,'q',NULL,NULL,NULL,'received','{}',1,0,'t','t')"
    )


def _insert(connection: sqlite3.Connection, item: ApprovalItem, **overrides: object) -> None:
    body, digest = encode_approval_assignment_v2(item)
    row: dict[str, object] = dict(
        assignment_id=item.item_id,
        org_id=item.org_id,
        request_id=item.request_id,
        awaiting_revision=item.awaiting_revision,
        attempt=item.attempt,
        approval_round=item.approval_round,
        supersedes_assignment_id=item.supersedes_item_id,
        status=item.status,
        assignment_json=body,
        assignment_sha256=digest,
        assignment_schema_version=1,
    )
    row.update(overrides)
    connection.execute(
        "INSERT INTO durable_approval_assignments_v2 (assignment_id,org_id,request_id,awaiting_revision,attempt,approval_round,supersedes_assignment_id,status,assignment_json,assignment_sha256,assignment_schema_version) VALUES (:assignment_id,:org_id,:request_id,:awaiting_revision,:attempt,:approval_round,:supersedes_assignment_id,:status,:assignment_json,:assignment_sha256,:assignment_schema_version)",
        row,
    )


def test_explicit_migration_leaves_v1_absent_and_installs_v2(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    connection = open_sqlite_approval_assignments_v2_connection(db)
    try:
        assert (
            connection.execute(
                "SELECT component_id FROM schema_component_manifests WHERE component_id = ?",
                (SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID,),
            ).fetchone()[0]
            == SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'approval_items'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "point",
    ["after_assignments", "after_current_index", "before_manifest_insert", "after_manifest_insert"],
)
def test_migration_fault_rolls_back_v2_only(tmp_path: Path, point: str) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_approval_assignments_v2_schema(
            db,
            fault_injector=lambda seen: (
                (_ for _ in ()).throw(RuntimeError(seen)) if seen == point else None
            ),
        )
    connection = sqlite3.connect(db)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'durable_approval_assignments_v2'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_identity_is_request_attempt_round_and_open_is_per_attempt(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _parent(connection)
        _insert(connection, _item(item_id="one", attempt=1))
        _insert(connection, _item(item_id="two", attempt=2))
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, _item(item_id="same", attempt=1))
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, _item(item_id="same-open", attempt=1, round=2, previous="one"))
    finally:
        connection.close()


@pytest.mark.parametrize("corruption", ["cross_org", "forged_mirror", "cross_attempt_lineage"])
def test_open_rejects_row_corruption_without_repair(tmp_path: Path, corruption: str) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _parent(connection)
        if corruption == "cross_org":
            _insert(connection, _item(), org_id="org-2")
        elif corruption == "forged_mirror":
            _insert(connection, _item(), status="resolved")
        else:
            first = _item(item_id="first", attempt=1, status="superseded", successor="second")
            _insert(connection, first)
            _insert(connection, _item(item_id="second", attempt=2, round=2, previous="first"))
        connection.commit()
    finally:
        connection.close()
    before = db.read_bytes()
    with pytest.raises(SqliteApprovalAssignmentsV2SchemaError):
        open_sqlite_approval_assignments_v2_connection(db)
    assert db.read_bytes() == before


def test_v1_presence_is_not_migration_source_or_authority(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("CREATE TABLE approval_items (item_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO approval_items VALUES ('legacy')")
        connection.commit()
    finally:
        connection.close()
    migrate_sqlite_approval_assignments_v2_schema(db)
    connection = sqlite3.connect(db)
    try:
        assert (
            connection.execute("SELECT count(*) FROM durable_approval_assignments_v2").fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT item_id FROM approval_items").fetchone()[0] == "legacy"
    finally:
        connection.close()


def test_existing_canonical_v1_stays_separate_from_v2(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    migrate_sqlite_approval_schema(db)
    migrate_sqlite_approval_assignments_v2_schema(db)
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT count(*) FROM approval_items").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM durable_approval_assignments_v2").fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_reconciliation_reports_missing_component_read_only(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(db)
    before = db.read_bytes()
    report = reconcile_sqlite_approval_assignments_v2_schema(db)
    assert not report.capable
    assert not report.assignment_manifest_present
    assert db.read_bytes() == before
