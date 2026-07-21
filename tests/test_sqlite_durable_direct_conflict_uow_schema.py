from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_direct_conflict_uow import (
    SQLITE_DURABLE_DIRECT_CONFLICT_UOW_COMPONENT_ID,
    SQLITE_DURABLE_DIRECT_CONFLICT_UOW_MIGRATION_FAULT_POINTS,
    SqliteDurableDirectConflictUowSchemaError,
    migrate_sqlite_durable_direct_conflict_uow_schema,
    open_sqlite_durable_direct_conflict_uow_connection,
    reconcile_sqlite_durable_direct_conflict_uow_schema,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)


def _ref(kind: str, label: str) -> str:
    return f"{kind}:{hashlib.sha256(label.encode()).hexdigest()}"


def _parent(path: Path) -> None:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)


def _request(connection: sqlite3.Connection, request_id: str, org_id: str) -> None:
    connection.execute(
        "INSERT INTO question_requests(request_id,org_id,requester_id,session_id,question,context_snapshot,intent,initial_disposition,state_kind,state_json,state_schema_version,revision,created_at,updated_at) VALUES(?,?, 'user',NULL,'q',NULL,NULL,NULL,'received','{}',1,0,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
        (request_id, org_id),
    )


def test_installs_separate_secret_free_direct_conflict_schema_after_parents(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    connection = open_sqlite_durable_direct_conflict_uow_connection(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?",
            (SQLITE_DURABLE_DIRECT_CONFLICT_UOW_COMPONENT_ID,),
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id='durable_linked_aggregates_v1'"
        ).fetchone()
        columns = {
            row[1].casefold()
            for table in (
                "durable_direct_conflict_votes",
                "durable_direct_conflict_receipts",
                "durable_direct_conflict_audit_intents",
                "durable_direct_conflict_outbox_intents",
                "durable_direct_conflict_result_projections",
            )
            for row in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        }
        for forbidden in ("question", "rationale", "secret", "token", "control", "grant", "card_id", "owner_id"):
            assert forbidden not in columns
        result_columns = {
            row[1]: row
            for row in connection.execute(
                'PRAGMA table_xinfo("durable_direct_conflict_result_projections")'
            ).fetchall()
        }
        assert result_columns["receipt_id"][5] == 1  # exactly one projection per receipt
    finally:
        connection.close()


@pytest.mark.parametrize("point", SQLITE_DURABLE_DIRECT_CONFLICT_UOW_MIGRATION_FAULT_POINTS)
def test_fault_atomic_migration_leaves_no_direct_conflict_owned_schema(tmp_path: Path, point: str) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_durable_direct_conflict_uow_schema(
            path,
            fault_injector=lambda actual: (_ for _ in ()).throw(RuntimeError(actual))
            if actual == point
            else None,
        )
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_direct_conflict_%'"
        ).fetchall() == []
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?",
            (SQLITE_DURABLE_DIRECT_CONFLICT_UOW_COMPONENT_ID,),
        ).fetchone() is None
    finally:
        connection.close()


def test_missing_linked_parent_does_not_promote_legacy_conflict(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    with pytest.raises(SqliteDurableDirectConflictUowSchemaError):
        migrate_sqlite_durable_direct_conflict_uow_schema(path)


def _insert_valid_vote_graph(connection: sqlite3.Connection, *, org_id: str) -> tuple[str, str, str]:
    request_id, conflict_id, receipt_id = (
        _ref(kind, org_id) for kind in ("request", "conflict", "receipt")
    )
    _request(connection, request_id, org_id)
    connection.execute(
        "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,0,'open',?,?)",
        (conflict_id, org_id, request_id, "a" * 64, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO durable_direct_conflict_votes VALUES(?,?,?,?,?,?,?,?,?,?)",
        (conflict_id, org_id, request_id, 1, _ref("subject", "owner"), _ref("card", "target"), "a" * 64, 1, receipt_id, "2026-01-01T00:00:00+00:00"),
    )
    receipt = {
        "org_id": org_id,
        "request_id": request_id,
        "conflict_id": conflict_id,
        "concurrence_round": 1,
        "actor_subject_ref": _ref("subject", "owner"),
        "owner_subject_ref": _ref("subject", "owner"),
        "target_card_ref": _ref("card", "target"),
        "candidate_set_sha256": "a" * 64,
        "candidate_owner_count": 1,
        "action": "conflict.concur",
        "expected_request_revision": 0,
    }
    command_digest = hashlib.sha256(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    connection.execute(
        "INSERT INTO durable_direct_conflict_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (receipt_id, org_id, request_id, conflict_id, 1, command_digest, receipt["actor_subject_ref"], receipt["owner_subject_ref"], receipt["target_card_ref"], receipt["candidate_set_sha256"], receipt["candidate_owner_count"], "conflict.concur", 0, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO durable_direct_conflict_audit_intents VALUES(?,?,?,?,?,?)",
        (receipt_id, org_id, request_id, "conflict.concur", command_digest, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO durable_direct_conflict_outbox_intents VALUES(?,?,?,?,?,?)",
        (receipt_id, org_id, request_id, "conflict.concur", command_digest, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO durable_direct_conflict_result_projections VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (receipt_id, org_id, request_id, conflict_id, 1, "vote_recorded", receipt["owner_subject_ref"], receipt["target_card_ref"], receipt["candidate_set_sha256"], receipt["candidate_owner_count"], 1, "2026-01-01T00:00:00+00:00"),
    )
    return request_id, conflict_id, receipt_id


@pytest.mark.parametrize(
    ("statement", "params"),
    (
        ("UPDATE durable_direct_conflict_votes SET vote_receipt_id=?", ("receipt:" + "b" * 64,)),
        ("UPDATE durable_direct_conflict_receipts SET target_card_ref=?", (_ref("card", "other-target"),)),
        ("UPDATE durable_direct_conflict_receipts SET candidate_set_sha256=?", ("b" * 64,)),
        ("UPDATE durable_direct_conflict_result_projections SET target_card_ref=?", (_ref("card", "other-target"),)),
        ("UPDATE durable_direct_conflict_result_projections SET accepted_vote_count=2", ()),
    ),
)
def test_correct_looking_but_unbound_or_mismatched_vote_graph_fails_closed(
    tmp_path: Path, statement: str, params: tuple[object, ...]
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    org_id = _ref("org", "bound-graph")
    connection = sqlite3.connect(path)
    _insert_valid_vote_graph(connection, org_id=org_id)
    connection.execute(statement, params)
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_direct_conflict_uow_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDirectConflictUowSchemaError):
        open_sqlite_durable_direct_conflict_uow_connection(path, org_id=org_id)


def test_correct_hash_but_semantically_different_command_digest_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    org_id = _ref("org", "replay")
    connection = sqlite3.connect(path)
    _insert_valid_vote_graph(connection, org_id=org_id)
    # A digest-looking value is not sufficient: it must represent these exact command fields.
    connection.execute("UPDATE durable_direct_conflict_receipts SET command_digest=?", ("b" * 64,))
    connection.execute("UPDATE durable_direct_conflict_audit_intents SET command_digest=?", ("b" * 64,))
    connection.execute("UPDATE durable_direct_conflict_outbox_intents SET command_digest=?", ("b" * 64,))
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_direct_conflict_uow_schema(path, org_id=org_id).capable


@pytest.mark.parametrize("orphan_table", ("durable_direct_conflict_votes", "durable_direct_conflict_receipts"))
def test_vote_and_receipt_cannot_survive_without_their_one_to_one_counterpart(
    tmp_path: Path, orphan_table: str
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    org_id = _ref("org", f"orphan-{orphan_table}")
    connection = sqlite3.connect(path)
    _insert_valid_vote_graph(connection, org_id=org_id)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(f"DELETE FROM {orphan_table}")
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_direct_conflict_uow_schema(path, org_id=org_id).capable


@pytest.mark.parametrize(
    ("table", "column", "value"),
    (
        ("durable_direct_conflict_votes", "owner_subject_ref", "owner:control-token"),
        ("durable_direct_conflict_votes", "target_card_ref", "카드 선택 사유"),
        ("durable_direct_conflict_receipts", "action", "conflict.agree"),
        ("durable_direct_conflict_receipts", "command_digest", "x" * 64),
        ("durable_direct_conflict_result_projections", "result_kind", "approved"),
    ),
)
def test_prose_secret_and_alias_corruption_fail_closed_without_repair(
    tmp_path: Path, table: str, column: str, value: str
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    org_id = _ref("org", "1")
    connection = sqlite3.connect(path)
    _insert_valid_vote_graph(connection, org_id=org_id)
    connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    connection.commit()
    before = connection.execute(f"SELECT {column} FROM {table}").fetchone()[0]
    connection.close()
    assert not reconcile_sqlite_durable_direct_conflict_uow_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDirectConflictUowSchemaError):
        open_sqlite_durable_direct_conflict_uow_connection(path, org_id=org_id)
    assert sqlite3.connect(path).execute(f"SELECT {column} FROM {table}").fetchone()[0] == before


def test_row_reconciliation_is_org_scoped_but_catalog_and_current_row_are_not(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    org_a, org_b = _ref("org", "a"), _ref("org", "b")
    connection = sqlite3.connect(path)
    _insert_valid_vote_graph(connection, org_id=org_a)
    # A second independent graph is deliberately corrupt only for org_b.
    _insert_valid_vote_graph(connection, org_id=org_b)
    connection.execute("UPDATE durable_direct_conflict_votes SET target_card_ref='secret-value' WHERE org_id=?", (org_b,))
    connection.commit()
    connection.close()
    assert reconcile_sqlite_durable_direct_conflict_uow_schema(path, org_id=org_a).capable
    assert not reconcile_sqlite_durable_direct_conflict_uow_schema(path, org_id=org_b).capable


def test_validate_only_never_repairs_missing_projection(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE durable_direct_conflict_result_projections")
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_direct_conflict_uow_schema(path).capable
    assert sqlite3.connect(path).execute(
        "SELECT 1 FROM sqlite_schema WHERE name='durable_direct_conflict_result_projections'"
    ).fetchone() is None


def test_duplicate_valid_result_projection_fails_closed_without_repair(tmp_path: Path) -> None:
    """A malicious pre-release schema downgrade cannot create two projections.

    Foreign keys are disabled solely to inject a duplicate into a replacement
    table with the same columns but without the canonical PK.  Neither runtime
    opening nor reconciliation may repair, deduplicate, or activate it.
    """
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    org_id = _ref("org", "duplicate-projection")
    connection = sqlite3.connect(path)
    _insert_valid_vote_graph(connection, org_id=org_id)
    projection = connection.execute(
        "SELECT * FROM durable_direct_conflict_result_projections"
    ).fetchone()
    assert projection is not None
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("ALTER TABLE durable_direct_conflict_result_projections RENAME TO duplicate_source")
    connection.execute(
        "CREATE TABLE durable_direct_conflict_result_projections AS SELECT * FROM duplicate_source WHERE 0"
    )
    placeholders = ",".join("?" for _ in projection)
    connection.execute(
        f"INSERT INTO durable_direct_conflict_result_projections VALUES({placeholders})",
        projection,
    )
    connection.execute(
        f"INSERT INTO durable_direct_conflict_result_projections VALUES({placeholders})",
        projection,
    )
    connection.execute("DROP TABLE duplicate_source")
    connection.commit()
    before = connection.execute(
        "SELECT COUNT(*) FROM durable_direct_conflict_result_projections"
    ).fetchone()[0]
    connection.close()

    assert not reconcile_sqlite_durable_direct_conflict_uow_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDirectConflictUowSchemaError):
        open_sqlite_durable_direct_conflict_uow_connection(path, org_id=org_id)
    assert sqlite3.connect(path).execute(
        "SELECT COUNT(*) FROM durable_direct_conflict_result_projections"
    ).fetchone()[0] == before
