from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.sqlite_approval import (
    SQLITE_APPROVAL_COMPONENT_ID,
    SqliteApprovalSchemaError,
    decode_approval_item,
    encode_approval_item,
    migrate_sqlite_approval_schema,
    open_sqlite_approval_connection,
    reconcile_sqlite_approval_schema,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.approval import (
    AnswerCandidate,
    ApprovalDraft,
    ApprovalItem,
    ApprovalRequired,
    ApprovalSupersession,
)
from agent_org_network.question_request import RouteTarget


def _capable_parent(path: Path) -> None:
    migrate_sqlite_completion_schema(path)


def _approval_item(
    *,
    item_id: str = "approval-1",
    org_id: str = "org-1",
    request_id: str = "request-1",
    approval_round: int = 1,
    supersedes_item_id: str | None = None,
    status: str = "open",
    successor_item_id: str | None = None,
) -> ApprovalItem:
    at = datetime(2026, 7, 15, tzinfo=UTC)
    route = RouteTarget(
        agent_id="refund-card",
        intent="refund",
        requires_approval=True,
        authority_version="route-v1",
    )
    item = ApprovalItem(
        item_id=item_id,
        org_id=org_id,
        request_id=request_id,
        awaiting_revision=approval_round,
        attempt=1,
        route=route,
        draft=ApprovalDraft(
            draft_id=f"draft-{item_id}",
            request_id=request_id,
            attempt=1,
            route=route,
            candidate=AnswerCandidate(text="가능합니다."),
            created_at=at,
        ),
        requirement=ApprovalRequired(approver_id="reviewer-1", policy_version="policy-1"),
        created_at=at,
        due_at=at + timedelta(minutes=10),
        approval_round=approval_round,
        supersedes_item_id=supersedes_item_id,
    )
    if status == "open":
        return item
    assert status == "superseded" and successor_item_id is not None
    return item.supersede(
        ApprovalSupersession(
            successor_item_id=successor_item_id,
            reason="reassigned",
            superseded_at=at + timedelta(minutes=1),
        )
    )


def _insert_parent_request(
    connection: sqlite3.Connection, *, request_id: str = "request-1"
) -> None:
    connection.execute(
        "INSERT INTO question_requests "
        "(request_id, org_id, requester_id, session_id, question, context_snapshot, intent, "
        "initial_disposition, state_kind, state_json, state_schema_version, revision, created_at, updated_at) "
        "VALUES (?, 'org-1', 'user-1', NULL, 'q', NULL, NULL, NULL, 'received', '{}', 1, 0, 't', 't')",
        (request_id,),
    )


def _insert_item(
    connection: sqlite3.Connection, item: ApprovalItem, **row_overrides: object
) -> None:
    item_json, item_sha256 = encode_approval_item(item)
    row: dict[str, object] = {
        "item_id": item.item_id,
        "org_id": item.org_id,
        "request_id": item.request_id,
        "awaiting_revision": item.awaiting_revision,
        "attempt": item.attempt,
        "approval_round": item.approval_round,
        "supersedes_item_id": item.supersedes_item_id,
        "status": item.status,
        "item_json": item_json,
        "item_sha256": item_sha256,
        "item_schema_version": 1,
    }
    row.update(row_overrides)
    connection.execute(
        "INSERT INTO approval_items "
        "(item_id, org_id, request_id, awaiting_revision, attempt, approval_round, "
        "supersedes_item_id, status, item_json, item_sha256, item_schema_version) "
        "VALUES (:item_id, :org_id, :request_id, :awaiting_revision, :attempt, :approval_round, "
        ":supersedes_item_id, :status, :item_json, :item_sha256, :item_schema_version)",
        row,
    )


def test_migration_adds_approval_component_to_capable_fresh_parent(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    _capable_parent(db)
    migrate_sqlite_approval_schema(db)
    connection = open_sqlite_approval_connection(db)
    try:
        marker = connection.execute(
            "SELECT component_id FROM schema_component_manifests WHERE component_id = ?",
            (SQLITE_APPROVAL_COMPONENT_ID,),
        ).fetchone()
        assert marker[0] == SQLITE_APPROVAL_COMPONENT_ID
    finally:
        connection.close()


@pytest.mark.parametrize(
    "point",
    [
        "after_approval_items",
        "after_current_index",
        "before_manifest_insert",
        "after_manifest_insert",
    ],
)
def test_fault_rolls_back_all_approval_schema_changes(tmp_path: Path, point: str) -> None:
    db = tmp_path / "workflow.sqlite"
    _capable_parent(db)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_approval_schema(
            db,
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )
    connection = sqlite3.connect(db)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'approval_items'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM schema_component_manifests WHERE component_id = ?",
                (SQLITE_APPROVAL_COMPONENT_ID,),
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_runtime_open_detects_manifest_and_catalog_drift_without_repair(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    _capable_parent(db)
    migrate_sqlite_approval_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("DROP INDEX ux_approval_items_current_open")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteApprovalSchemaError):
        open_sqlite_approval_connection(db)
    connection = sqlite3.connect(db)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'ux_approval_items_current_open'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


@pytest.mark.parametrize("corruption", ["cross_org_parent", "forged_mirror", "broken_lineage"])
def test_runtime_open_rejects_row_level_approval_corruption_without_repair(
    tmp_path: Path, corruption: str
) -> None:
    db = tmp_path / "workflow.sqlite"
    _capable_parent(db)
    migrate_sqlite_approval_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_parent_request(connection)
        if corruption == "cross_org_parent":
            # request_id FK는 유효하지만, row와 현재 parent의 org는 다르다.
            _insert_item(connection, _approval_item(org_id="org-2"))
        elif corruption == "forged_mirror":
            # Canonical payload는 open이지만, indexed mirror만 위조했다.
            _insert_item(connection, _approval_item(), status="resolved")
        else:
            predecessor = _approval_item(
                item_id="approval-1",
                status="superseded",
                successor_item_id="different-successor",
            )
            successor = _approval_item(
                item_id="approval-2",
                approval_round=2,
                supersedes_item_id=predecessor.item_id,
            )
            _insert_item(connection, predecessor)
            _insert_item(connection, successor)
        connection.commit()
    finally:
        connection.close()

    before = db.read_bytes()
    with pytest.raises(SqliteApprovalSchemaError):
        open_sqlite_approval_connection(db)
    assert db.read_bytes() == before


def test_unique_current_round_and_parent_fk_are_database_constraints(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    _capable_parent(db)
    migrate_sqlite_approval_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO approval_items VALUES ('i', 'o', 'missing', 1, 1, 1, NULL, 'open', '{}', 'x', 1)"
            )
        connection.execute(
            "INSERT INTO question_requests "
            "(request_id, org_id, requester_id, session_id, question, context_snapshot, intent, "
            "initial_disposition, state_kind, state_json, state_schema_version, revision, created_at, updated_at) "
            "VALUES ('request-1', 'org-1', 'user-1', NULL, 'q', NULL, NULL, NULL, 'received', '{}', 1, 0, 't', 't')"
        )
        connection.execute(
            "INSERT INTO approval_items VALUES "
            "('first', 'org-1', 'request-1', 1, 1, 1, NULL, 'resolved', '{}', ?, 1)",
            ("0" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO approval_items VALUES "
                "('same-round', 'org-1', 'request-1', 2, 1, 1, NULL, 'resolved', '{}', ?, 1)",
                ("1" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO approval_items VALUES "
                "('no-predecessor', 'org-1', 'request-1', 2, 1, 2, 'unknown', 'resolved', '{}', ?, 1)",
                ("2" * 64,),
            )
        connection.execute(
            "INSERT INTO approval_items VALUES "
            "('current', 'org-1', 'request-1', 2, 1, 2, 'first', 'open', '{}', ?, 1)",
            ("3" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO approval_items VALUES "
                "('second-current', 'org-1', 'request-1', 3, 1, 3, 'current', 'open', '{}', ?, 1)",
                ("4" * 64,),
            )
    finally:
        connection.close()


def test_reconciliation_is_read_only_and_reports_missing_component(tmp_path: Path) -> None:
    db = tmp_path / "workflow.sqlite"
    _capable_parent(db)
    report = reconcile_sqlite_approval_schema(db)
    assert not report.capable
    assert not report.approval_manifest_present
    connection = sqlite3.connect(db)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'approval_items'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_decoder_rejects_noncanonical_and_forged_payload() -> None:
    with pytest.raises(SqliteApprovalSchemaError):
        decode_approval_item(item_json='{"x":1, "x":1}', item_sha256="0" * 64)


def test_decoder_requires_canonical_digest_and_current_parent_scope() -> None:
    at = datetime(2026, 7, 15, tzinfo=UTC)
    route = RouteTarget(
        agent_id="refund-card",
        intent="refund",
        requires_approval=True,
        authority_version="route-v1",
    )
    item = ApprovalItem(
        item_id="approval-1",
        org_id="org-1",
        request_id="request-1",
        awaiting_revision=1,
        attempt=1,
        route=route,
        draft=ApprovalDraft(
            draft_id="draft-1",
            request_id="request-1",
            attempt=1,
            route=route,
            candidate=AnswerCandidate(text="가능합니다."),
            created_at=at,
        ),
        requirement=ApprovalRequired(approver_id="reviewer-1", policy_version="policy-1"),
        created_at=at,
        due_at=at + timedelta(minutes=10),
    )
    from agent_org_network.sqlite_approval import encode_approval_item

    item_json, digest = encode_approval_item(item)
    assert (
        decode_approval_item(
            item_json=item_json,
            item_sha256=digest,
            expected_org_id="org-1",
            expected_request_id="request-1",
        )
        == item
    )
    with pytest.raises(SqliteApprovalSchemaError, match="parent org"):
        decode_approval_item(item_json=item_json, item_sha256=digest, expected_org_id="other")
