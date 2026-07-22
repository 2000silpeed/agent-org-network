from __future__ import annotations

import ast
import hashlib
import sqlite3
from pathlib import Path

import pytest

from agent_org_network.conflict_escalation_approval_evidence import (
    escalation_cause_digest as c1_escalation_cause_digest,
)
from agent_org_network.durable_conflict_escalation_evidence import (
    CandidateRegistryChanged,
    DivergentVotes,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    migrate_sqlite_durable_conflict_escalation_baseline_schema,
)
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_COMPONENT_ID,
    SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_MIGRATION_FAULT_POINTS,
    SqliteDurableConflictEscalationReceiptsSchemaError,
    migrate_sqlite_durable_conflict_escalation_receipts_schema,
    open_sqlite_durable_conflict_escalation_receipts_connection,
    reconcile_sqlite_durable_conflict_escalation_receipts_schema,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)

_CREATED = "2026-01-01T00:00:00+00:00"


def _ref(kind: str, label: str) -> str:
    return f"{kind}:{hashlib.sha256(label.encode()).hexdigest()}"


def _parent(path: Path) -> None:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)


def _request(connection: sqlite3.Connection, request_id: str, org_id: str) -> None:
    connection.execute(
        "INSERT INTO question_requests(request_id,org_id,requester_id,session_id,question,context_snapshot,intent,initial_disposition,state_kind,state_json,state_schema_version,revision,created_at,updated_at) VALUES(?,?, 'user',NULL,'q',NULL,NULL,NULL,'received','{}',1,0,?,?)",
        (request_id, org_id, _CREATED, _CREATED),
    )


def _insert_valid_receipt_graph(
    connection: sqlite3.Connection,
    *,
    org_id: str,
    label: str,
    cause_kind: str = "DivergentVotes",
    result_kind: str = "escalated_to_manager",
) -> tuple[str, str, str]:
    request_id, conflict_id, receipt_id, evidence_id = (
        _ref(kind, label) for kind in ("request", "conflict", "receipt", "evidence")
    )
    actor_ref = _ref("subject", f"{label}-actor")
    manager_ref = _ref("subject", f"{label}-manager")
    root_ref = _ref("subject", f"{label}-root")
    awaiting_revision = 0
    concurrence_round = 1
    candidate_snapshot = "a" * 64
    baseline_sha = "b" * 64
    candidate_claim_sha = "c" * 64
    vote_set_sha = "d" * 64
    candidate_owner_count = 2 if cause_kind == "DivergentVotes" else None
    current_candidate_snapshot = None if cause_kind == "DivergentVotes" else "e" * 64

    _request(connection, request_id, org_id)
    connection.execute(
        "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,?, 'open', ?, ?)",
        (conflict_id, org_id, request_id, awaiting_revision, candidate_snapshot, _CREATED),
    )

    cause: DivergentVotes | CandidateRegistryChanged
    if cause_kind == "DivergentVotes":
        assert candidate_owner_count is not None
        cause = DivergentVotes(
            org_ref=org_id,
            conflict_ref=conflict_id,
            request_ref=request_id,
            awaiting_revision=awaiting_revision,
            concurrence_round=concurrence_round,
            candidate_snapshot_sha256=candidate_snapshot,
            candidate_owner_count=candidate_owner_count,
            baseline_sha256=baseline_sha,
            candidate_claim_sha256=candidate_claim_sha,
            vote_set_sha256=vote_set_sha,
            evaluated_at=_CREATED,
        )
    else:
        assert current_candidate_snapshot is not None
        cause = CandidateRegistryChanged(
            org_ref=org_id,
            conflict_ref=conflict_id,
            request_ref=request_id,
            awaiting_revision=awaiting_revision,
            concurrence_round=concurrence_round,
            candidate_snapshot_sha256=candidate_snapshot,
            current_candidate_snapshot_sha256=current_candidate_snapshot,
            baseline_sha256=baseline_sha,
            candidate_claim_sha256=candidate_claim_sha,
            vote_set_sha256=vote_set_sha,
            evaluated_at=_CREATED,
        )
    cause_digest = c1_escalation_cause_digest(cause)
    graph_selection_digest = "f" * 64
    command_digest = "1" * 64
    resource_fingerprint = "2" * 64

    connection.execute(
        "INSERT INTO durable_conflict_escalation_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_id,
            org_id,
            conflict_id,
            request_id,
            awaiting_revision,
            actor_ref,
            "conflict.escalate",
            command_digest,
            resource_fingerprint,
            evidence_id,
            cause_digest,
            graph_selection_digest,
            _CREATED,
        ),
    )
    target_ref, evidence_manager_ref = (
        (manager_ref, manager_ref) if result_kind == "escalated_to_manager" else (root_ref, None)
    )
    connection.execute(
        "INSERT INTO durable_conflict_escalation_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_id,
            org_id,
            conflict_id,
            request_id,
            cause_kind,
            awaiting_revision,
            concurrence_round,
            candidate_snapshot,
            baseline_sha,
            candidate_claim_sha,
            vote_set_sha,
            candidate_owner_count,
            current_candidate_snapshot,
            cause_digest,
            graph_selection_digest,
            evidence_manager_ref,
            root_ref,
            _CREATED,
        ),
    )
    connection.execute(
        "INSERT INTO durable_conflict_escalation_result_projections VALUES(?,?,?,?,?,?,?)",
        (receipt_id, org_id, conflict_id, result_kind, "deadlock", target_ref, _CREATED),
    )
    connection.execute(
        "INSERT INTO durable_conflict_escalation_audit_intents VALUES(?,?,?,?,?)",
        (receipt_id, org_id, "conflict.escalate", command_digest, _CREATED),
    )
    connection.execute(
        "INSERT INTO durable_conflict_escalation_outbox_intents VALUES(?,?,?,?,?)",
        (receipt_id, org_id, "conflict.escalate", command_digest, _CREATED),
    )
    return request_id, conflict_id, receipt_id


def test_installs_receipt_graph_schema_after_baseline_parent_and_remigrate_is_no_op(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)  # idempotent no-op
    connection = open_sqlite_durable_conflict_escalation_receipts_connection(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?",
            (SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_COMPONENT_ID,),
        ).fetchone()
        columns = {
            row[1].casefold()
            for table in (
                "durable_conflict_escalation_receipts",
                "durable_conflict_escalation_evidence",
                "durable_conflict_escalation_result_projections",
                "durable_conflict_escalation_audit_intents",
                "durable_conflict_escalation_outbox_intents",
            )
            for row in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        }
        for forbidden in ("question", "rationale", "secret", "token", "control", "raw_claim"):
            assert forbidden not in columns
    finally:
        connection.close()


def test_missing_baseline_parent_rejects_install(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    with pytest.raises(SqliteDurableConflictEscalationReceiptsSchemaError):
        migrate_sqlite_durable_conflict_escalation_receipts_schema(path)


@pytest.mark.parametrize(
    "point", SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_MIGRATION_FAULT_POINTS
)
def test_fault_atomic_migration_leaves_no_owned_schema(tmp_path: Path, point: str) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_durable_conflict_escalation_receipts_schema(
            path,
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_conflict_escalation_receipts%' OR name LIKE 'durable_conflict_escalation_evidence%' OR name LIKE 'durable_conflict_escalation_result_projections%' OR name LIKE 'durable_conflict_escalation_audit_intents%' OR name LIKE 'durable_conflict_escalation_outbox_intents%'"
            ).fetchall()
            == []
        )
        assert (
            connection.execute(
                "SELECT 1 FROM schema_component_manifests WHERE component_id=?",
                (SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_COMPONENT_ID,),
            ).fetchone()
            is None
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "child_table",
    (
        "durable_conflict_escalation_evidence",
        "durable_conflict_escalation_result_projections",
        "durable_conflict_escalation_audit_intents",
        "durable_conflict_escalation_outbox_intents",
    ),
)
def test_missing_one_to_one_child_fails_closed_without_repair(
    tmp_path: Path, child_table: str
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", f"missing-{child_table}")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _, _, receipt_id = _insert_valid_receipt_graph(
        connection, org_id=org_id, label=f"missing-{child_table}"
    )
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(f"DELETE FROM {child_table} WHERE receipt_id=?", (receipt_id,))
    connection.commit()
    connection.close()

    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_id
    ).capable
    with pytest.raises(SqliteDurableConflictEscalationReceiptsSchemaError):
        open_sqlite_durable_conflict_escalation_receipts_connection(path, org_id=org_id)
    verify = sqlite3.connect(path)
    assert (
        verify.execute(
            f"SELECT COUNT(*) FROM {child_table} WHERE receipt_id=?", (receipt_id,)
        ).fetchone()[0]
        == 0
    )
    verify.close()


def test_duplicate_evidence_child_fails_closed_without_repair(tmp_path: Path) -> None:
    """A pre-release schema downgrade cannot smuggle in a second evidence row.

    Foreign keys are disabled solely to replace the table with one that has the
    same columns but no canonical PK, so a duplicate can be inserted at all.
    """
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "duplicate-evidence")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _, _, receipt_id = _insert_valid_receipt_graph(
        connection, org_id=org_id, label="duplicate-evidence"
    )
    evidence = connection.execute(
        "SELECT * FROM durable_conflict_escalation_evidence WHERE receipt_id=?", (receipt_id,)
    ).fetchone()
    assert evidence is not None
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "ALTER TABLE durable_conflict_escalation_evidence RENAME TO duplicate_source"
    )
    connection.execute(
        "CREATE TABLE durable_conflict_escalation_evidence AS SELECT * FROM duplicate_source WHERE 0"
    )
    placeholders = ",".join("?" for _ in evidence)
    connection.execute(
        f"INSERT INTO durable_conflict_escalation_evidence VALUES({placeholders})", evidence
    )
    connection.execute(
        f"INSERT INTO durable_conflict_escalation_evidence VALUES({placeholders})", evidence
    )
    connection.execute("DROP TABLE duplicate_source")
    connection.commit()
    before = connection.execute(
        "SELECT COUNT(*) FROM durable_conflict_escalation_evidence"
    ).fetchone()[0]
    connection.close()

    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_id
    ).capable
    with pytest.raises(SqliteDurableConflictEscalationReceiptsSchemaError):
        open_sqlite_durable_conflict_escalation_receipts_connection(path, org_id=org_id)
    assert (
        sqlite3.connect(path)
        .execute("SELECT COUNT(*) FROM durable_conflict_escalation_evidence")
        .fetchone()[0]
        == before
    )


def test_cross_org_child_composite_fk_rejects_other_org_receipt(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_a, org_b = _ref("org", "cross-a"), _ref("org", "cross-b")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _, _, receipt_id = _insert_valid_receipt_graph(connection, org_id=org_a, label="cross-a")
    other_request_id, other_conflict_id = _ref("request", "cross-b"), _ref("conflict", "cross-b")
    _request(connection, other_request_id, org_b)
    connection.execute(
        "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,0, 'open', ?, ?)",
        (other_conflict_id, org_b, other_request_id, "a" * 64, _CREATED),
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO durable_conflict_escalation_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                org_b,
                other_conflict_id,
                other_request_id,
                "DivergentVotes",
                0,
                1,
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                1,
                None,
                "e" * 64,
                "f" * 64,
                None,
                _ref("subject", "cross-root"),
                _CREATED,
            ),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO durable_conflict_escalation_result_projections VALUES(?,?,?,?,?,?,?)",
            (
                receipt_id,
                org_b,
                other_conflict_id,
                "escalated_to_root",
                "deadlock",
                _ref("subject", "cross-root"),
                _CREATED,
            ),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO durable_conflict_escalation_audit_intents VALUES(?,?,?,?,?)",
            (receipt_id, org_b, "conflict.escalate", "1" * 64, _CREATED),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO durable_conflict_escalation_outbox_intents VALUES(?,?,?,?,?)",
            (receipt_id, org_b, "conflict.escalate", "1" * 64, _CREATED),
        )
    connection.close()


def test_escalation_cause_digest_matches_c1_conflict_escalation_approval_evidence_recompute(
    tmp_path: Path,
) -> None:
    """S4.3c.2의 domain-local recompute가 c.1의 `escalation_cause_digest`와 exact 일치해야 한다."""
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "cause-digest-c1-parity")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(connection, org_id=org_id, label="cause-digest-c1-parity")
    connection.commit()
    connection.close()

    expected = c1_escalation_cause_digest(
        DivergentVotes(
            org_ref=org_id,
            conflict_ref=_ref("conflict", "cause-digest-c1-parity"),
            request_ref=_ref("request", "cause-digest-c1-parity"),
            awaiting_revision=0,
            concurrence_round=1,
            candidate_snapshot_sha256="a" * 64,
            candidate_owner_count=2,
            baseline_sha256="b" * 64,
            candidate_claim_sha256="c" * 64,
            vote_set_sha256="d" * 64,
            evaluated_at=_CREATED,
        )
    )
    stored = (
        sqlite3.connect(path)
        .execute(
            "SELECT escalation_cause_digest FROM durable_conflict_escalation_receipts WHERE org_id=?",
            (org_id,),
        )
        .fetchone()[0]
    )
    assert stored == expected
    assert reconcile_sqlite_durable_conflict_escalation_receipts_schema(path, org_id=org_id).capable


@pytest.mark.parametrize(
    ("statement", "params"),
    (
        (
            "UPDATE durable_conflict_escalation_evidence SET candidate_snapshot_sha256=?",
            ("9" * 64,),
        ),
        ("UPDATE durable_conflict_escalation_evidence SET awaiting_revision=1", ()),
        ("UPDATE durable_conflict_escalation_evidence SET escalation_cause_digest=?", ("8" * 64,)),
    ),
)
def test_evidence_field_tamper_or_digest_mismatch_fails_closed(
    tmp_path: Path, statement: str, params: tuple[object, ...]
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "tamper")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(connection, org_id=org_id, label="tamper")
    connection.execute(statement, params)
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_id
    ).capable
    with pytest.raises(SqliteDurableConflictEscalationReceiptsSchemaError):
        open_sqlite_durable_conflict_escalation_receipts_connection(path, org_id=org_id)


@pytest.mark.parametrize(
    ("statement", "params"),
    (
        (
            "UPDATE durable_conflict_escalation_evidence SET current_candidate_snapshot_sha256=?",
            ("7" * 64,),
        ),
        ("UPDATE durable_conflict_escalation_evidence SET candidate_owner_count=NULL", ()),
    ),
)
def test_divergent_votes_variant_misplacement_fails_closed(
    tmp_path: Path, statement: str, params: tuple[object, ...]
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "variant-misplace")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(
        connection, org_id=org_id, label="variant-misplace", cause_kind="DivergentVotes"
    )
    connection.execute(statement, params)
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_id
    ).capable


def test_candidate_registry_changed_variant_installs_and_reconciles(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "registry-changed")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(
        connection,
        org_id=org_id,
        label="registry-changed",
        cause_kind="CandidateRegistryChanged",
        result_kind="escalated_to_root",
    )
    connection.commit()
    connection.close()
    assert reconcile_sqlite_durable_conflict_escalation_receipts_schema(path, org_id=org_id).capable


@pytest.mark.parametrize(
    ("statement", "params"),
    (
        ("UPDATE durable_conflict_escalation_evidence SET candidate_owner_count=2", ()),
        (
            "UPDATE durable_conflict_escalation_evidence SET current_candidate_snapshot_sha256=NULL",
            (),
        ),
    ),
)
def test_candidate_registry_changed_variant_misplacement_fails_closed(
    tmp_path: Path, statement: str, params: tuple[object, ...]
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "crc-misplace")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(
        connection,
        org_id=org_id,
        label="crc-misplace",
        cause_kind="CandidateRegistryChanged",
        result_kind="escalated_to_root",
    )
    connection.execute(statement, params)
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_id
    ).capable


def test_receipt_row_cause_digest_tamper_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "receipt-tamper")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(connection, org_id=org_id, label="receipt-tamper")
    # evidence 행은 그대로 두고 receipt 쪽 digest만 직접 변조 — mirror 불일치 fail-closed.
    connection.execute(
        "UPDATE durable_conflict_escalation_receipts SET escalation_cause_digest=?",
        ("a1" * 32,),
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_id
    ).capable
    with pytest.raises(SqliteDurableConflictEscalationReceiptsSchemaError):
        open_sqlite_durable_conflict_escalation_receipts_connection(path, org_id=org_id)


@pytest.mark.parametrize(
    ("statement", "params"),
    (
        (
            "UPDATE durable_conflict_escalation_result_projections SET target_subject_ref=?",
            (_ref("subject", "wrong-target"),),
        ),
        (
            "UPDATE durable_conflict_escalation_evidence SET manager_subject_ref=root_subject_ref",
            (),
        ),
    ),
)
def test_result_target_mismatch_or_manager_leak_on_root_fallback_fails_closed(
    tmp_path: Path, statement: str, params: tuple[object, ...]
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "target-mismatch")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(
        connection,
        org_id=org_id,
        label="target-mismatch",
        cause_kind="CandidateRegistryChanged",
        result_kind="escalated_to_root",
    )
    connection.execute(statement, params)
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_id
    ).capable


@pytest.mark.parametrize(
    ("table", "column", "value"),
    (
        ("durable_conflict_escalation_receipts", "actor_subject_ref", "user:control-token"),
        ("durable_conflict_escalation_receipts", "action", "conflict.approve"),
        ("durable_conflict_escalation_receipts", "created_at", "2026-01-01 00:00:00"),
        ("durable_conflict_escalation_evidence", "root_subject_ref", "루트 승인자 설명"),
        ("durable_conflict_escalation_result_projections", "source_kind", "vote"),
        ("durable_conflict_escalation_result_projections", "result_kind", "approved"),
    ),
)
def test_prose_raw_user_or_noncanonical_corruption_fails_closed_without_repair(
    tmp_path: Path, table: str, column: str, value: str
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", f"prose-{table}-{column}")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(connection, org_id=org_id, label=f"prose-{table}-{column}")
    connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    connection.commit()
    before = connection.execute(f"SELECT {column} FROM {table}").fetchone()[0]
    connection.close()

    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_id
    ).capable
    with pytest.raises(SqliteDurableConflictEscalationReceiptsSchemaError):
        open_sqlite_durable_conflict_escalation_receipts_connection(path, org_id=org_id)
    assert sqlite3.connect(path).execute(f"SELECT {column} FROM {table}").fetchone()[0] == before


def test_audit_or_outbox_mirror_drift_from_receipt_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "intent-drift")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(connection, org_id=org_id, label="intent-drift")
    connection.execute(
        "UPDATE durable_conflict_escalation_audit_intents SET command_digest=?", ("9" * 64,)
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_id
    ).capable


def test_row_reconciliation_is_org_scoped_but_catalog_and_fk_are_not(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_a, org_b = _ref("org", "scope-a"), _ref("org", "scope-b")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(connection, org_id=org_a, label="scope-a")
    _insert_valid_receipt_graph(connection, org_id=org_b, label="scope-b")
    connection.execute(
        "UPDATE durable_conflict_escalation_result_projections SET source_kind='not-deadlock' WHERE org_id=?",
        (org_b,),
    )
    connection.commit()
    connection.close()
    assert reconcile_sqlite_durable_conflict_escalation_receipts_schema(path, org_id=org_a).capable
    assert not reconcile_sqlite_durable_conflict_escalation_receipts_schema(
        path, org_id=org_b
    ).capable


def test_validate_only_never_repairs_and_write_apis_are_absent(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    org_id = _ref("org", "no-write-api")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_valid_receipt_graph(connection, org_id=org_id, label="no-write-api")
    connection.commit()
    connection.close()
    before = {
        table: sqlite3.connect(path).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "durable_conflict_escalation_receipts",
            "durable_conflict_escalation_evidence",
            "durable_conflict_escalation_result_projections",
            "durable_conflict_escalation_audit_intents",
            "durable_conflict_escalation_outbox_intents",
        )
    }

    assert reconcile_sqlite_durable_conflict_escalation_receipts_schema(path, org_id=org_id).capable
    connection = open_sqlite_durable_conflict_escalation_receipts_connection(path, org_id=org_id)
    connection.close()

    after = {
        table: sqlite3.connect(path).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert before == after


def test_receipts_module_does_not_import_c1_evidence_or_durable_credentials() -> None:
    path = (
        Path(__file__).parents[1]
        / "src"
        / "agent_org_network"
        / "sqlite_durable_conflict_escalation_receipts.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"conflict_escalation_approval_evidence", "durable_credentials"}
    imported = {
        alias.name.split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        (node.module or "").split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert imported.isdisjoint(forbidden)
