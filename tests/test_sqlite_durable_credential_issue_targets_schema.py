from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_org_network.sqlite_durable_credential_issue_targets import (
    SQLITE_DURABLE_CREDENTIAL_ISSUE_TARGETS_MIGRATION_FAULT_POINTS,
    DurableCredentialIssueTargetReservation,
    SqliteDurableCredentialIssueTargetsSchemaError,
    migrate_sqlite_durable_credential_issue_targets_schema,
    open_sqlite_durable_credential_issue_targets_connection,
    reconcile_sqlite_durable_credential_issue_targets_schema,
    reserve_sqlite_durable_credential_issue_target,
    validate_sqlite_durable_credential_issue_targets_connection,
)

_WRITE_TOKENS = frozenset({"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"})


def _parent(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE durable_credentials (
              credential_id TEXT NOT NULL, org_id TEXT NOT NULL, owner_subject_id TEXT NOT NULL, role TEXT NOT NULL,
              generation INTEGER NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, secret_hash TEXT NOT NULL,
              issued_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT, PRIMARY KEY (org_id, credential_id),
              CHECK (generation >= 1), CHECK (revision >= 1), CHECK (status IN ('active','revoked'))
            );
            CREATE TABLE credential_command_receipts (org_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL, command_digest TEXT NOT NULL, credential_id TEXT NOT NULL, result_revision INTEGER NOT NULL, result_json TEXT NOT NULL, delivery_ref TEXT, PRIMARY KEY (org_id, request_id, attempt));
            CREATE TABLE credential_audit_intents (id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, action TEXT NOT NULL, credential_id TEXT NOT NULL, principal_subject_id TEXT NOT NULL, evidence_id TEXT NOT NULL, detail_json TEXT NOT NULL);
            CREATE TABLE credential_outbox_intents (id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, kind TEXT NOT NULL, credential_id TEXT NOT NULL, payload_json TEXT NOT NULL);
            """
        )
        connection.commit()
    finally:
        connection.close()


def _reservation(
    *, target_id: str = "target", credential_id: str = "credential", command_digest: str = "a" * 64
) -> DurableCredentialIssueTargetReservation:
    return DurableCredentialIssueTargetReservation(
        org_id="org",
        target_id=target_id,
        credential_id=credential_id,
        command_digest=command_digest,
        principal_id="principal",
        owner_subject_id="owner",
        role="role",
        expires_at=None,
        resource_fingerprint="b" * 64,
        approval_evidence_id="evidence",
        approval_command_digest="c" * 64,
        approval_resource_fingerprint="d" * 64,
        target_generation=1,
        created_at="2026-07-19T00:00:00.000Z",
    )


def test_installs_canonical_target_and_target_fk_fence_without_v1_migration(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = open_sqlite_durable_credential_issue_targets_connection(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id='durable_credential_issue_targets_v1'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id='durable_credential_stage_fence_v2'"
        ).fetchone()
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='durable_credential_stage_fences'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "PRAGMA foreign_key_list(credential_issue_stage_fences_v2)"
            ).fetchone()[2]
            == "durable_credential_issue_targets_v1"
        )
    finally:
        connection.close()


@pytest.mark.parametrize("legacy", ("table", "marker"))
def test_v1_direct_fence_presence_makes_v2_unavailable_without_backfill(
    tmp_path: Path, legacy: str
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    connection = sqlite3.connect(path)
    if legacy == "table":
        connection.execute("CREATE TABLE durable_credential_stage_fences (bad TEXT)")
    else:
        connection.execute(
            "CREATE TABLE schema_component_manifests(component_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY, schema_version INTEGER NOT NULL, manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_component_manifests VALUES('durable_credential_stage_fence_v1',1,'{}','x')"
        )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialIssueTargetsSchemaError):
        migrate_sqlite_durable_credential_issue_targets_schema(path)
    with pytest.raises(SqliteDurableCredentialIssueTargetsSchemaError):
        open_sqlite_durable_credential_issue_targets_connection(path)
    assert not reconcile_sqlite_durable_credential_issue_targets_schema(path).capable
    check = sqlite3.connect(path)
    try:
        assert (
            check.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='durable_credential_issue_targets_v1'"
            ).fetchone()
            is None
        )
        assert (
            check.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='credential_issue_stage_fences_v2'"
            ).fetchone()
            is None
        )
    finally:
        check.close()


@pytest.mark.parametrize("point", SQLITE_DURABLE_CREDENTIAL_ISSUE_TARGETS_MIGRATION_FAULT_POINTS)
def test_migration_is_fault_atomic(tmp_path: Path, point: str) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_durable_credential_issue_targets_schema(
            path,
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='durable_credential_issue_targets_v1'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='schema_component_manifests'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_reservation_is_same_command_replay_but_blocks_actual_and_active_credential_collisions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = open_sqlite_durable_credential_issue_targets_connection(path)
    try:
        first = _reservation()
        assert (
            reserve_sqlite_durable_credential_issue_target(connection, first)["target_id"]
            == "target"
        )
        assert (
            reserve_sqlite_durable_credential_issue_target(connection, first)["target_id"]
            == "target"
        )
        with pytest.raises(SqliteDurableCredentialIssueTargetsSchemaError):
            reserve_sqlite_durable_credential_issue_target(
                connection, _reservation(credential_id="credential", command_digest="f" * 64)
            )
        assert not connection.in_transaction
        assert (
            reserve_sqlite_durable_credential_issue_target(
                connection,
                _reservation(
                    target_id="other-target", credential_id="other", command_digest="1" * 64
                ),
            )["target_id"]
            == "other-target"
        )
        connection.execute(
            "INSERT INTO durable_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "actual",
                "org",
                "owner",
                "role",
                1,
                1,
                "active",
                "a" * 64,
                "2026-07-19T00:00:00.000Z",
                None,
                None,
            ),
        )
        with pytest.raises(SqliteDurableCredentialIssueTargetsSchemaError):
            reserve_sqlite_durable_credential_issue_target(
                connection, _reservation(credential_id="actual", command_digest="e" * 64)
            )
    finally:
        connection.close()


def test_independent_connections_serialize_replay_and_actual_credential_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    barrier = threading.Barrier(8)

    def replay() -> tuple[str, str]:
        connection = open_sqlite_durable_credential_issue_targets_connection(path)
        try:
            barrier.wait()
            row = reserve_sqlite_durable_credential_issue_target(connection, _reservation())
            return row["target_id"], row["target_json"]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        replay_futures = [pool.submit(replay) for _ in range(8)]
        rows = [future.result() for future in replay_futures]
    assert len(set(rows)) == 1

    other = tmp_path / "actual-race.sqlite"
    _parent(other)
    migrate_sqlite_durable_credential_issue_targets_schema(other)
    race = threading.Barrier(2)

    def reserve() -> bool:
        connection = open_sqlite_durable_credential_issue_targets_connection(other)
        try:
            race.wait()
            try:
                reserve_sqlite_durable_credential_issue_target(connection, _reservation())
                return True
            except SqliteDurableCredentialIssueTargetsSchemaError:
                return False
        finally:
            connection.close()

    def insert_actual() -> bool:
        connection = sqlite3.connect(other, timeout=5.0)
        try:
            race.wait()
            try:
                connection.execute(
                    "INSERT INTO durable_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "credential",
                        "org",
                        "owner",
                        "role",
                        1,
                        1,
                        "active",
                        "a" * 64,
                        "2026-07-19T00:00:00.000Z",
                        None,
                        None,
                    ),
                )
                connection.commit()
                return True
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        target_future = pool.submit(reserve)
        actual_future = pool.submit(insert_actual)
        target_ok = target_future.result()
        actual_ok = actual_future.result()
    assert target_ok != actual_ok
    check = sqlite3.connect(other)
    try:
        assert (
            check.execute("SELECT count(*) FROM durable_credential_issue_targets_v1").fetchone()[0]
            + check.execute("SELECT count(*) FROM durable_credentials").fetchone()[0]
            == 1
        )
    finally:
        check.close()


def test_same_semantic_replay_returns_current_advanced_lifecycle_row(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = open_sqlite_durable_credential_issue_targets_connection(path)
    try:
        reservation = _reservation()
        reserve_sqlite_durable_credential_issue_target(connection, reservation)
        connection.execute(
            "INSERT INTO credential_issue_stage_fences_v2 VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "org",
                "target",
                "e" * 64,
                "f" * 64,
                None,
                1,
                "d" * 64,
                "ClaimedStage",
                "2026-07-19T00:00:00.000Z",
                "2026-07-19T00:00:01.000Z",
            ),
        )
        connection.execute(
            "UPDATE durable_credential_issue_targets_v1 SET state=?,updated_at=? WHERE org_id=? AND target_id=?",
            ("StageClaimed", "2026-07-19T00:00:01.000Z", "org", "target"),
        )
        connection.commit()
        replay = reserve_sqlite_durable_credential_issue_target(connection, reservation)
        assert replay["state"] == "StageClaimed"
        assert replay["updated_at"] == "2026-07-19T00:00:01.000Z"
    finally:
        connection.close()


def test_database_trigger_seals_immutable_target_snapshot_and_catalog_requires_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = open_sqlite_durable_credential_issue_targets_connection(path)
    try:
        reserve_sqlite_durable_credential_issue_target(connection, _reservation())
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE durable_credential_issue_targets_v1 SET role='other' WHERE org_id='org' AND target_id='target'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM durable_credential_issue_targets_v1 WHERE org_id='org' AND target_id='target'"
            )
        connection.rollback()
        connection.execute("DROP TRIGGER durable_credential_issue_targets_v1_immutable_snapshot")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteDurableCredentialIssueTargetsSchemaError):
        open_sqlite_durable_credential_issue_targets_connection(path)


def test_catalog_rejects_dropped_active_credential_index(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX durable_credential_issue_targets_active_credential")
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialIssueTargetsSchemaError):
        open_sqlite_durable_credential_issue_targets_connection(path)


@pytest.mark.parametrize("column,value", ((7, "x" * 24), (16, "2026-99-99T99:99:99.999Z")))
def test_invalid_timestamp_is_rejected_before_target_persistence(
    tmp_path: Path, column: int, value: str
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = open_sqlite_durable_credential_issue_targets_connection(path)
    try:
        values = list(_reservation().row())
        values[column] = value
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO durable_credential_issue_targets_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        connection.rollback()
        assert (
            connection.execute(
                "SELECT count(*) FROM durable_credential_issue_targets_v1"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_open_and_reconcile_reject_ddl_bypass_noncanonical_target_json_mirrors_and_fence_matrix(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    reservation = _reservation()
    target = reservation.target_json()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO durable_credential_issue_targets_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "target",
            "credential",
            "a" * 64,
            "principal",
            "owner",
            "role",
            None,
            "b" * 64,
            "evidence",
            "c" * 64,
            "d" * 64,
            "Reserved",
            1,
            target.replace("role", "Role", 1),
            hashlib.sha256(target.encode()).hexdigest(),
            "2026-07-19T00:00:00.000Z",
            "2026-07-19T00:00:00.000Z",
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialIssueTargetsSchemaError):
        open_sqlite_durable_credential_issue_targets_connection(path)
    assert not reconcile_sqlite_durable_credential_issue_targets_schema(path).capable


@pytest.mark.parametrize(
    "unsafe", ("raw secret", "grant:abc", "rationale-text", "body:content", "token:abc", "x" * 129)
)
def test_opaque_scalar_grammar_and_raw_content_are_rejected_by_ddl_and_open(
    tmp_path: Path, unsafe: str
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    reservation = _reservation()
    values = list(reservation.row())
    values[4] = unsafe
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO durable_credential_issue_targets_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO durable_credential_issue_targets_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialIssueTargetsSchemaError):
        open_sqlite_durable_credential_issue_targets_connection(path)


def test_open_wraps_standalone_validate_selects_in_one_snapshot_with_zero_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)

    statements: list[str] = []
    real_connect = sqlite3.connect

    def _trace(sql: str) -> None:
        statements.append(sql.strip().split()[0].upper())

    def _spy_connect(database: str, *, uri: bool = False, timeout: float = 5.0) -> sqlite3.Connection:
        connection = real_connect(database, uri=uri, timeout=timeout)
        if uri and "mode=rw" in database:
            connection.set_trace_callback(_trace)
        return connection

    monkeypatch.setattr(
        "agent_org_network.sqlite_durable_credential_issue_targets.sqlite3.connect",
        _spy_connect,
    )

    connection = open_sqlite_durable_credential_issue_targets_connection(path)
    try:
        begin_indices = [i for i, s in enumerate(statements) if s == "BEGIN"]
        commit_indices = [i for i, s in enumerate(statements) if s == "COMMIT"]
        assert len(begin_indices) == 1
        assert len(commit_indices) == 1
        begin_at, commit_at = begin_indices[0], commit_indices[0]
        assert begin_at < commit_at
        between = statements[begin_at + 1 : commit_at]
        assert len([s for s in between if s == "SELECT"]) >= 5
        assert not _WRITE_TOKENS.intersection(between)
    finally:
        connection.close()


def test_standalone_validate_connection_wraps_its_selects_in_one_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)

    connection = sqlite3.connect(path)
    statements: list[str] = []
    connection.set_trace_callback(lambda sql: statements.append(sql.strip().split()[0].upper()))
    try:
        assert connection.in_transaction is False
        validate_sqlite_durable_credential_issue_targets_connection(connection)
        begin_indices = [i for i, s in enumerate(statements) if s == "BEGIN"]
        commit_indices = [i for i, s in enumerate(statements) if s == "COMMIT"]
        assert len(begin_indices) == 1
        assert len(commit_indices) == 1
        begin_at, commit_at = begin_indices[0], commit_indices[0]
        assert begin_at < commit_at
        assert commit_at == len(statements) - 1
        between = statements[begin_at + 1 : commit_at]
        assert len([s for s in between if s == "SELECT"]) >= 5
        assert not _WRITE_TOKENS.intersection(between)
    finally:
        connection.close()


def test_validate_connection_reuses_callers_transaction_without_nested_begin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)

    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        statements: list[str] = []
        connection.set_trace_callback(
            lambda sql: statements.append(sql.strip().split()[0].upper())
        )
        validate_sqlite_durable_credential_issue_targets_connection(connection)
        assert connection.in_transaction is True
        assert "BEGIN" not in statements
        assert "COMMIT" not in statements
        assert "ROLLBACK" not in statements
        connection.commit()
    finally:
        connection.close()
