from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    CONFLICT_ESCALATION_MANAGER_SELECTION_AVAILABLE,
    CONFLICT_ESCALATION_UNDER_CLAIM_AVAILABLE,
    SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_COMPONENT_ID,
    SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_MIGRATION_FAULT_POINTS,
    SqliteDurableConflictEscalationBaselineSchemaError,
    migrate_sqlite_durable_conflict_escalation_baseline_schema,
    open_sqlite_durable_conflict_escalation_baseline_connection,
    reconcile_sqlite_durable_conflict_escalation_baseline_schema,
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


def _insert_valid(connection: sqlite3.Connection, org_id: str) -> str:
    request_id, conflict_id, baseline_id = (
        _ref(kind, org_id) for kind in ("request", "conflict", "baseline")
    )
    _request(connection, request_id, org_id)
    created = "2026-01-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,0,'open',?,?)",
        (conflict_id, org_id, request_id, "a" * 64, created),
    )
    candidates = [
        (1, _ref("card", "c1"), _ref("subject", "o1"), _ref("domain", "d1"), "b" * 64),
        (2, _ref("card", "c2"), _ref("subject", "o2"), _ref("domain", "d2"), "c" * 64),
    ]
    header = {
        "conflict_id": conflict_id,
        "org_id": org_id,
        "request_id": request_id,
        "awaiting_revision": 0,
        "candidate_set_sha256": "a" * 64,
        "candidate_count": 2,
        "created_at": created,
    }
    digest = hashlib.sha256(
        json.dumps(
            {
                "baseline": header,
                "candidates": [
                    {
                        "candidate_ordinal": ordinal,
                        "candidate_card_ref": card,
                        "candidate_owner_subject_ref": owner,
                        "candidate_domain_ref": domain,
                        "candidate_route_sha256": route,
                    }
                    for ordinal, card, owner, domain, route in candidates
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    connection.execute(
        "INSERT INTO durable_conflict_escalation_baselines VALUES(?,?,?,?,?,?,?,?,?)",
        (baseline_id, conflict_id, org_id, request_id, 0, "a" * 64, 2, digest, created),
    )
    connection.executemany(
        "INSERT INTO durable_conflict_escalation_baseline_candidates VALUES(?,?,?,?,?,?)",
        [(baseline_id, *candidate) for candidate in candidates],
    )
    return baseline_id


def _rewrite_baseline_digest(connection: sqlite3.Connection, baseline_id: str) -> None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM durable_conflict_escalation_baselines WHERE baseline_id=?", (baseline_id,)
    ).fetchone()
    assert row is not None
    candidates = connection.execute(
        "SELECT * FROM durable_conflict_escalation_baseline_candidates WHERE baseline_id=? ORDER BY candidate_ordinal",
        (baseline_id,),
    ).fetchall()
    fields = (
        "conflict_id",
        "org_id",
        "request_id",
        "awaiting_revision",
        "candidate_set_sha256",
        "candidate_count",
        "created_at",
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "baseline": {field: row[field] for field in fields},
                "candidates": [
                    {
                        field: candidate[field]
                        for field in (
                            "candidate_ordinal",
                            "candidate_card_ref",
                            "candidate_owner_subject_ref",
                            "candidate_domain_ref",
                            "candidate_route_sha256",
                        )
                    }
                    for candidate in candidates
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    connection.execute(
        "UPDATE durable_conflict_escalation_baselines SET baseline_sha256=? WHERE baseline_id=?",
        (digest, baseline_id),
    )
    connection.row_factory = None


def test_installs_candidate_only_validate_schema_after_parents(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    connection = open_sqlite_durable_conflict_escalation_baseline_connection(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?",
            (SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_COMPONENT_ID,),
        ).fetchone()
        columns = {
            row[1].casefold()
            for table in (
                "durable_conflict_escalation_baselines",
                "durable_conflict_escalation_baseline_candidates",
            )
            for row in connection.execute(f'PRAGMA table_xinfo("{table}")')
        }
        assert not (
            {
                "under_claim",
                "manager",
                "root",
                "selection",
                "question",
                "answer",
                "rationale",
                "secret",
                "token",
                "control",
            }
            & columns
        )
        assert (
            not CONFLICT_ESCALATION_UNDER_CLAIM_AVAILABLE
            and not CONFLICT_ESCALATION_MANAGER_SELECTION_AVAILABLE
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "point", SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_MIGRATION_FAULT_POINTS
)
def test_fault_atomic_migration_leaves_no_owned_schema(tmp_path: Path, point: str) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_durable_conflict_escalation_baseline_schema(
            path,
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_conflict_escalation_baseline%'"
            ).fetchall()
            == []
        )
    finally:
        connection.close()


def test_no_linked_parent_no_legacy_or_backfill_promotion(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    with pytest.raises(SqliteDurableConflictEscalationBaselineSchemaError):
        migrate_sqlite_durable_conflict_escalation_baseline_schema(path)


@pytest.mark.parametrize(
    ("statement", "params"),
    (
        (
            "UPDATE durable_conflict_escalation_baseline_candidates SET candidate_domain_ref=?",
            ("도메인 설명",),
        ),
        (
            "UPDATE durable_conflict_escalation_baseline_candidates SET candidate_card_ref=?",
            ("card:control-token",),
        ),
        ("UPDATE durable_conflict_escalation_baselines SET baseline_sha256=?", ("d" * 64,)),
    ),
)
def test_prose_secret_or_correct_hash_corruption_fails_closed(
    tmp_path: Path, statement: str, params: tuple[object, ...]
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    org_id = _ref("org", "scope")
    connection = sqlite3.connect(path)
    _insert_valid(connection, org_id)
    connection.execute(statement, params)
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_conflict_escalation_baseline_schema(
        path, org_id=org_id
    ).capable
    with pytest.raises(SqliteDurableConflictEscalationBaselineSchemaError):
        open_sqlite_durable_conflict_escalation_baseline_connection(path, org_id=org_id)


def test_correct_hash_late_baseline_is_not_a_legacy_backfill(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    org_id = _ref("org", "late")
    connection = sqlite3.connect(path)
    baseline_id = _insert_valid(connection, org_id)
    connection.execute(
        "UPDATE durable_conflict_escalation_baselines SET created_at='2026-01-02T00:00:00+00:00'"
    )
    _rewrite_baseline_digest(connection, baseline_id)
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_conflict_escalation_baseline_schema(
        path, org_id=org_id
    ).capable


def test_org_scoped_candidate_corruption_does_not_block_other_org(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    org_a, org_b = _ref("org", "a"), _ref("org", "b")
    connection = sqlite3.connect(path)
    _insert_valid(connection, org_a)
    b = _insert_valid(connection, org_b)
    connection.execute(
        "UPDATE durable_conflict_escalation_baseline_candidates SET candidate_card_ref=? WHERE baseline_id=?",
        ("prose", b),
    )
    connection.commit()
    connection.close()
    assert reconcile_sqlite_durable_conflict_escalation_baseline_schema(path, org_id=org_a).capable
    assert not reconcile_sqlite_durable_conflict_escalation_baseline_schema(
        path, org_id=org_b
    ).capable
