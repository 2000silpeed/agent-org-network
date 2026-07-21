from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_org_network.credential_mcp import DeliveryStage, StageMissing
from agent_org_network.sqlite_durable_credential_stage_fence import (
    SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_COMPONENT_ID,
    SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_MIGRATION_FAULT_POINTS,
    SqliteDurableCredentialStageFenceSchemaError,
    migrate_sqlite_durable_credential_stage_fence_schema,
    open_sqlite_durable_credential_stage_fence_connection,
    reconcile_sqlite_durable_credential_stage_fence_schema,
)


def _parent(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE durable_credentials (
              credential_id TEXT NOT NULL, org_id TEXT NOT NULL, owner_subject_id TEXT NOT NULL,
              role TEXT NOT NULL, generation INTEGER NOT NULL, revision INTEGER NOT NULL,
              status TEXT NOT NULL, secret_hash TEXT NOT NULL, issued_at TEXT NOT NULL,
              expires_at TEXT, revoked_at TEXT,
              PRIMARY KEY (org_id, credential_id),
              CHECK (generation >= 1), CHECK (revision >= 1), CHECK (status IN ('active','revoked'))
            );
            CREATE TABLE credential_command_receipts (
              org_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
              command_digest TEXT NOT NULL, credential_id TEXT NOT NULL, result_revision INTEGER NOT NULL,
              result_json TEXT NOT NULL, delivery_ref TEXT,
              PRIMARY KEY (org_id, request_id, attempt)
            );
            CREATE TABLE credential_audit_intents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, action TEXT NOT NULL,
              credential_id TEXT NOT NULL, principal_subject_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
              detail_json TEXT NOT NULL
            );
            CREATE TABLE credential_outbox_intents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, kind TEXT NOT NULL,
              credential_id TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()



def test_installs_canonical_fence_only_after_validate_only_credential_parent(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_stage_fence_schema(path)
    connection = open_sqlite_durable_credential_stage_fence_connection(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?",
            (SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_COMPONENT_ID,),
        ).fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(durable_credential_stage_fences)")}
        assert columns == {
            "org_id", "request_id", "attempt", "credential_id", "principal_subject_id",
            "action", "command_digest", "resource_fingerprint", "evidence_id",
            "stage_key", "secret_hash", "delivery_ref", "claim_generation", "claim_token_hash", "state",
        }
        assert not {"secret", "grant", "rationale", "body"} & columns
    finally:
        connection.close()


@pytest.mark.parametrize("point", SQLITE_DURABLE_CREDENTIAL_STAGE_FENCE_MIGRATION_FAULT_POINTS)
def test_migration_is_fault_atomic(tmp_path: Path, point: str) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_durable_credential_stage_fence_schema(
            path,
            fault_injector=lambda actual: (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None,
        )
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT name FROM sqlite_schema WHERE name='durable_credential_stage_fences'").fetchone() is None
        assert connection.execute("SELECT name FROM sqlite_schema WHERE name='schema_component_manifests'").fetchone() is None
    finally:
        connection.close()


def test_absent_or_drifted_parent_and_partial_component_fail_closed(tmp_path: Path) -> None:
    absent = tmp_path / "absent.sqlite"
    with pytest.raises(SqliteDurableCredentialStageFenceSchemaError):
        migrate_sqlite_durable_credential_stage_fence_schema(absent)

    drifted = tmp_path / "drifted.sqlite"
    _parent(drifted)
    connection = sqlite3.connect(drifted)
    connection.execute("ALTER TABLE durable_credentials ADD COLUMN raw_secret TEXT")
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialStageFenceSchemaError):
        migrate_sqlite_durable_credential_stage_fence_schema(drifted)

    partial = tmp_path / "partial.sqlite"
    _parent(partial)
    connection = sqlite3.connect(partial)
    connection.execute("CREATE TABLE durable_credential_stage_fences(bad TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialStageFenceSchemaError):
        migrate_sqlite_durable_credential_stage_fence_schema(partial)


def test_open_and_reconciliation_reject_corrupt_fence_rows_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_stage_fence_schema(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO durable_credential_stage_fences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("org", "request", 1, "credential", "principal", "worker_credential.issue", "a" * 64,
         "a" * 64, "evidence", "not-a-hash", "a" * 64, None, 0, None, "PendingStage"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialStageFenceSchemaError):
        open_sqlite_durable_credential_stage_fence_connection(path)
    report = reconcile_sqlite_durable_credential_stage_fence_schema(path)
    assert not report.capable
    assert "not-a-hash" in sqlite3.connect(path).execute(
        "SELECT stage_key FROM durable_credential_stage_fences"
    ).fetchone()[0]


@pytest.mark.parametrize(
    ("state", "generation", "token", "delivery_ref"),
    (
        ("PendingStage", 0, None, None),
        ("ClaimedStage", 1, "b" * 64, None),
        ("Staged", 1, "b" * 64, "delivery:v1:" + "1" * 64),
        ("Committing", 2, "c" * 64, "delivery:v1:" + "2" * 64),
        ("Committed", 2, "c" * 64, "delivery:v1:" + "2" * 64),
        ("CleanupPending", 2, "c" * 64, "delivery:v1:" + "2" * 64),
        ("Cleaned", 2, "c" * 64, "delivery:v1:" + "2" * 64),
    ),
)
def test_each_sealed_state_has_one_allowed_claim_material_shape(
    tmp_path: Path, state: str, generation: int, token: str | None, delivery_ref: str | None
) -> None:
    path = tmp_path / f"{state}.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_stage_fence_schema(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO durable_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("credential", "org", "owner", "role", 1, 1, "active", "a" * 64, "2026-01-01T00:00:00+00:00", None, None),
    )
    connection.execute(
        "INSERT INTO durable_credential_stage_fences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("org", "request", 1, "credential", "principal", "worker_credential.issue", "a" * 64,
         "a" * 64, "evidence", "d" * 64, "e" * 64, delivery_ref, generation, token, state),
    )
    connection.commit()
    connection.close()
    open_sqlite_durable_credential_stage_fence_connection(path).close()


@pytest.mark.parametrize(
    ("state", "generation", "token", "delivery_ref"),
    (
        ("PendingStage", 1, "b" * 64, None),
        ("PendingStage", 0, None, "delivery:v1:" + "1" * 64),
        ("ClaimedStage", 0, None, None),
        ("ClaimedStage", 1, "b" * 64, "delivery:v1:" + "1" * 64),
        ("Staged", 1, "b" * 64, None),
        ("Committing", 0, "b" * 64, "delivery:v1:" + "1" * 64),
        ("Committed", 1, None, "delivery:v1:" + "1" * 64),
        ("CleanupPending", 1, "b" * 64, None),
        ("Cleaned", 0, None, "delivery:v1:" + "1" * 64),
    ),
)
def test_open_and_reconcile_reject_every_invalid_sealed_state_material_shape(
    tmp_path: Path, state: str, generation: int, token: str | None, delivery_ref: str | None
) -> None:
    path = tmp_path / "invalid.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_stage_fence_schema(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO durable_credential_stage_fences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("org", "request", 1, "credential", "principal", "worker_credential.issue", "a" * 64,
         "a" * 64, "evidence", "d" * 64, "e" * 64, delivery_ref, generation, token, state),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialStageFenceSchemaError):
        open_sqlite_durable_credential_stage_fence_connection(path)
    assert not reconcile_sqlite_durable_credential_stage_fence_schema(path).capable


@pytest.mark.parametrize(
    "unsafe",
    ("raw secret", "grant:abc", "rationale-text", "body:content", "password:abc", "token:abc", "key:abc", "x" * 129),
)
@pytest.mark.parametrize("field", ("org_id", "request_id", "credential_id", "principal_subject_id", "evidence_id", "delivery_ref"))
def test_opaque_fields_reject_raw_or_unbounded_content_in_ddl_and_read_validation(
    tmp_path: Path, field: str, unsafe: str
) -> None:
    path = tmp_path / "opaque.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_stage_fence_schema(path)
    values: dict[str, object] = {
        "org_id": "org", "request_id": "request", "attempt": 1, "credential_id": "credential",
        "principal_subject_id": "principal", "action": "worker_credential.issue", "command_digest": "a" * 64,
        "resource_fingerprint": "b" * 64, "evidence_id": "evidence", "stage_key": "d" * 64,
        "secret_hash": "e" * 64, "delivery_ref": None, "claim_generation": 0,
        "claim_token_hash": None, "state": "PendingStage",
    }
    values[field] = unsafe
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO durable_credential_stage_fences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(values.values()),
        )
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO durable_credential_stage_fences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(values.values()),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialStageFenceSchemaError):
        open_sqlite_durable_credential_stage_fence_connection(path)
    assert not reconcile_sqlite_durable_credential_stage_fence_schema(path).capable


@pytest.mark.parametrize(
    "field",
    ("command_digest", "resource_fingerprint", "stage_key", "secret_hash", "claim_token_hash"),
)
def test_hash_fields_reject_64_character_non_sha256_values_in_ddl_and_read_validation(
    tmp_path: Path, field: str
) -> None:
    path = tmp_path / "hash.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_stage_fence_schema(path)
    values: dict[str, object] = {
        "org_id": "org", "request_id": "request", "attempt": 1, "credential_id": "credential",
        "principal_subject_id": "principal", "action": "worker_credential.issue", "command_digest": "a" * 64,
        "resource_fingerprint": "b" * 64, "evidence_id": "evidence", "stage_key": "d" * 64,
        "secret_hash": "e" * 64, "delivery_ref": "delivery:v1:" + "1" * 64, "claim_generation": 1,
        "claim_token_hash": "f" * 64, "state": "Staged",
    }
    values[field] = "P" * 64
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO durable_credential_stage_fences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(values.values()),
        )
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO durable_credential_stage_fences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(values.values()),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialStageFenceSchemaError):
        open_sqlite_durable_credential_stage_fence_connection(path)
    assert not reconcile_sqlite_durable_credential_stage_fence_schema(path).capable


@pytest.mark.parametrize("unsafe", ("sk_live_abc", "AKIA1234567890ABCDEF", "delivery:one", "delivery:v1:" + "P" * 64))
def test_delivery_ref_accepts_only_canonical_secret_free_external_reference(
    tmp_path: Path, unsafe: str
) -> None:
    path = tmp_path / "delivery-ref.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_stage_fence_schema(path)
    values = (
        "org", "request", 1, "credential", "principal", "worker_credential.issue", "a" * 64,
        "b" * 64, "evidence", "d" * 64, "e" * 64, unsafe, 1, "f" * 64, "Staged",
    )
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO durable_credential_stage_fences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("INSERT INTO durable_credential_stage_fences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableCredentialStageFenceSchemaError):
        open_sqlite_durable_credential_stage_fence_connection(path)
    assert not reconcile_sqlite_durable_credential_stage_fence_schema(path).capable


def test_recovery_delivery_stage_contract_binds_canonical_stage_key_and_reference() -> None:
    stage = DeliveryStage("a" * 64, "delivery:v1:" + "b" * 64)
    assert stage.stage_key == "a" * 64
    assert isinstance(StageMissing(), StageMissing)
    with pytest.raises(ValueError):
        DeliveryStage("not-a-stage-key", "delivery:v1:" + "b" * 64)
    with pytest.raises(ValueError):
        DeliveryStage("a" * 64, "sk_live_abc")
