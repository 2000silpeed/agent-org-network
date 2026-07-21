from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_linked_aggregates import (
    SQLITE_DURABLE_LINKED_AGGREGATES_COMPONENT_ID,
    SQLITE_DURABLE_LINKED_AGGREGATES_MIGRATION_FAULT_POINTS,
    SqliteDurableLinkedAggregatesSchemaError,
    migrate_sqlite_durable_linked_aggregates_schema,
    open_sqlite_durable_linked_aggregates_connection,
    reconcile_sqlite_durable_linked_aggregates_schema,
)


def _parent(path: Path) -> None:
    migrate_sqlite_completion_schema(path)


def _ref(kind: str, label: str) -> str:
    return f"{kind}:{hashlib.sha256(label.encode()).hexdigest()}"


def test_migrates_after_completion_parent_with_secret_free_linked_tables(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    connection = open_sqlite_durable_linked_aggregates_connection(path)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM schema_component_manifests WHERE component_id=?",
                (SQLITE_DURABLE_LINKED_AGGREGATES_COMPONENT_ID,),
            ).fetchone()
            is not None
        )
        columns = {
            row[1].casefold()
            for table in (
                "durable_linked_conflict_cases",
                "durable_linked_manager_items",
                "durable_linked_work_tickets",
                "durable_linked_command_receipts",
                "durable_linked_audit_intents",
                "durable_linked_outbox_intents",
            )
            for row in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        }
        # request_id is an intentional correlation FK, not a raw question body.
        for forbidden in (
            "question",
            "answer",
            "rationale",
            "secret",
            "token",
            "control_handle",
            "claim",
        ):
            assert forbidden not in columns
    finally:
        connection.close()


@pytest.mark.parametrize("point", SQLITE_DURABLE_LINKED_AGGREGATES_MIGRATION_FAULT_POINTS)
def test_fault_atomic_migration_leaves_no_owned_schema(tmp_path: Path, point: str) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_durable_linked_aggregates_schema(
            path,
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_linked_%'"
            ).fetchall()
            == []
        )
        assert (
            connection.execute(
                "SELECT 1 FROM schema_component_manifests WHERE component_id=?",
                (SQLITE_DURABLE_LINKED_AGGREGATES_COMPONENT_ID,),
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_validate_only_does_not_repair_catalog_drift(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE durable_linked_outbox_intents")
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableLinkedAggregatesSchemaError):
        open_sqlite_durable_linked_aggregates_connection(path)
    assert not reconcile_sqlite_durable_linked_aggregates_schema(path).capable
    assert (
        sqlite3.connect(path)
        .execute("SELECT 1 FROM sqlite_schema WHERE name='durable_linked_outbox_intents'")
        .fetchone()
        is None
    )


def _request(connection: sqlite3.Connection, request_id: str, org_id: str) -> None:
    connection.execute(
        "INSERT INTO question_requests(request_id,org_id,requester_id,session_id,question,context_snapshot,intent,initial_disposition,state_kind,state_json,state_schema_version,revision,created_at,updated_at) VALUES(?,?, 'user',NULL,'q',NULL,NULL,NULL,'received','{}',1,0,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
        (request_id, org_id),
    )


def test_row_reconciliation_is_org_scoped_but_current_org_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    connection = sqlite3.connect(path)
    org_a, org_b = _ref("org", "a"), _ref("org", "b")
    request_a, request_b, request_c = (_ref("request", value) for value in ("a", "b", "c"))
    _request(connection, request_a, org_a)
    _request(connection, request_b, org_b)
    _request(connection, request_c, org_a)
    connection.execute(
        "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,0,'open',?,?)",
        (_ref("conflict", "a"), org_a, request_a, "a" * 64, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,0,'open',?,?)",
        (_ref("conflict", "b"), org_b, request_b, "b" * 64, "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "UPDATE durable_linked_conflict_cases SET request_id=? WHERE conflict_id=?",
        (request_c, _ref("conflict", "b")),
    )
    connection.commit()
    connection.close()
    assert reconcile_sqlite_durable_linked_aggregates_schema(path, org_id=org_a).capable
    assert not reconcile_sqlite_durable_linked_aggregates_schema(path, org_id=org_b).capable
    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT request_id FROM durable_linked_conflict_cases WHERE conflict_id=?", (_ref("conflict", "b"),)
        ).fetchone()[0] == request_c
    )
    connection.close()


def test_no_parent_no_legacy_promotion(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    with pytest.raises(SqliteDurableLinkedAggregatesSchemaError):
        migrate_sqlite_durable_linked_aggregates_schema(path)


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("conflict_id", "refund-is-delayed"),
        ("org_id", "grant-abc"),
        ("candidate_set_sha256", "x" * 64),
        ("created_at", "2026-01-01 00:00:00"),
        ("created_at", "2026-99-99T99:99:99+99:99"),
        ("awaiting_revision", -1),
    ),
)
def test_correct_hash_or_prose_scalar_corruption_fails_closed_without_repair(
    tmp_path: Path, column: str, value: object
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    connection = sqlite3.connect(path)
    request_id, org_id, conflict_id = _ref("request", "1"), _ref("org", "1"), _ref("conflict", "1")
    _request(connection, request_id, org_id)
    connection.execute(
        "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,0,'open',?,?)",
        (conflict_id, org_id, request_id, "a" * 64, "2026-01-01T00:00:00+00:00"),
    )
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        f"UPDATE durable_linked_conflict_cases SET {column}=? WHERE conflict_id=?",
        (value, conflict_id),
    )
    connection.commit()
    before = connection.execute(
        f"SELECT {column} FROM durable_linked_conflict_cases WHERE rowid=1"
    ).fetchone()[0]
    connection.close()

    scope = None if column in {"conflict_id", "org_id"} else org_id
    report = reconcile_sqlite_durable_linked_aggregates_schema(path, org_id=scope)
    assert not report.capable
    with pytest.raises(SqliteDurableLinkedAggregatesSchemaError):
        open_sqlite_durable_linked_aggregates_connection(path, org_id=scope)
    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            f"SELECT {column} FROM durable_linked_conflict_cases WHERE rowid=1"
        ).fetchone()[0]
        == before
    )
    connection.close()


@pytest.mark.parametrize("column", ("source_ref", "manager_subject_id"))
def test_manager_reference_rejects_secret_or_control_handle(tmp_path: Path, column: str) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    connection = sqlite3.connect(path)
    request_id, org_id, manager_id = _ref("request", "1"), _ref("org", "1"), _ref("manager", "1")
    _request(connection, request_id, org_id)
    connection.execute(
        "INSERT INTO durable_linked_manager_items VALUES(?,?,?,0,'unowned',?,?,'open','2026-01-01T00:00:00+00:00')",
        (manager_id, org_id, request_id, _ref("source", "root"), _ref("subject", "manager")),
    )
    connection.execute(
        f"UPDATE durable_linked_manager_items SET {column}='control-token-abc' WHERE manager_item_id=?",
        (manager_id,),
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_linked_aggregates_schema(path, org_id=org_id).capable
