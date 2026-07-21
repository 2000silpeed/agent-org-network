from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

from agent_org_network.sqlite_durable_credential_scope_bindings import (
    COMPONENT_ID,
    CredentialScopeSnapshot,
    SqliteCredentialScopeBindingError,
    migrate_sqlite_durable_credential_scope_bindings,
    open_sqlite_durable_credential_scope_bindings,
    reserve_sqlite_credential_scope_binding,
)
from agent_org_network.sqlite_durable_credential_issue_targets import (
    _PARENT_DDLS,  # pyright: ignore[reportPrivateUsage]
    DurableCredentialIssueTargetReservation,
    migrate_sqlite_durable_credential_issue_targets_schema,
)


def _parent(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "scope.sqlite"
    connection = sqlite3.connect(path)
    try:
        for ddl in _PARENT_DDLS.values():
            connection.execute(ddl)
    finally:
        connection.close()
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    return path


def _target(*, org_id: str = "org", target_id: str = "target", credential_id: str = "credential", owner: str = "owner", command_digest: str = "a" * 64) -> DurableCredentialIssueTargetReservation:
    return DurableCredentialIssueTargetReservation(
        org_id=org_id, target_id=target_id, credential_id=credential_id,
        command_digest=command_digest, principal_id="principal", owner_subject_id=owner,
        role="role", expires_at=None, resource_fingerprint="b" * 64,
        approval_evidence_id="evidence", approval_command_digest="c" * 64,
        approval_resource_fingerprint="d" * 64, target_generation=1,
        created_at="2026-07-19T00:00:00.000Z",
    )


def _snapshot(*, org_id: str = "org", credential_id: str = "credential", card: str = "card", owner: str = "owner", credential_fp: str = "b" * 64, card_fp: str = "e" * 64, owner_fp: str = "f" * 64, source_kind: str = "registry", source_ref: str = "source-1", revision: int = 1) -> CredentialScopeSnapshot:
    digest = hashlib.sha256(json.dumps({"agent_card_id": card, "card_resource_fingerprint": card_fp, "credential_id": credential_id, "credential_resource_fingerprint": credential_fp, "org_id": org_id, "owner_resource_fingerprint": owner_fp, "owner_subject_id": owner, "source_instance_ref": source_ref, "source_kind": source_kind, "scope_revision": revision}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return CredentialScopeSnapshot(org_id, credential_id, card, owner, credential_fp, card_fp, owner_fp, source_kind, source_ref, revision, digest, "2026-07-19T00:00:00.000Z")


class _Source:
    def __init__(self, snapshot: CredentialScopeSnapshot | None) -> None:
        self.snapshot = snapshot

    def resolve_issue_scope(self, org_id: str, credential_id: str, agent_card_id: str) -> CredentialScopeSnapshot | None:
        return self.snapshot


def _ready(tmp_path: Path) -> Path:
    path = _parent(tmp_path)
    connection = sqlite3.connect(path)
    try:
        migrate_sqlite_durable_credential_scope_bindings(connection)
    finally:
        connection.close()
    return path


def test_scope_binding_schema_is_additive_parent_bound_and_fault_atomic(tmp_path: Path) -> None:
    path = _parent(tmp_path)
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(RuntimeError, match="after_table"):
            migrate_sqlite_durable_credential_scope_bindings(
                connection,
                fault_injector=lambda point: (
                    (_ for _ in ()).throw(RuntimeError(point)) if point == "after_table" else None
                ),
            )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_credential_scope_binding%' "
            ).fetchall()
            == []
        )
        migrate_sqlite_durable_credential_scope_bindings(connection)
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone()
        open_sqlite_durable_credential_scope_bindings(path, source=_Source(_snapshot())).close()
    finally:
        connection.close()


def test_scope_binding_open_rejects_catalog_tamper(tmp_path: Path) -> None:
    path = _parent(tmp_path)
    connection = sqlite3.connect(path)
    try:
        migrate_sqlite_durable_credential_scope_bindings(connection)
        connection.execute("DROP TABLE durable_credential_scope_bindings_v1")
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialScopeBindingError):
        open_sqlite_durable_credential_scope_bindings(path, source=_Source(_snapshot()))


def test_scope_snapshot_is_frozen_and_rejects_secret_or_noncanonical_values() -> None:
    snapshot = _snapshot(credential_fp="a" * 64, card_fp="b" * 64, owner_fp="c" * 64)
    with pytest.raises((AttributeError, TypeError)):
        snapshot.org_id = "other"  # type: ignore[misc]
    with pytest.raises(ValueError):
        CredentialScopeSnapshot(
            "org",
            "credential",
            "card",
            "owner",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "grant=raw-secret",
            "source",
            0,
            "d" * 64,
            "bad",
        )
    for unsafe in ("raw-secret", "grant:abc", "body:content"):
        with pytest.raises(ValueError):
            _snapshot(source_ref=unsafe)


@pytest.mark.parametrize(
    "snapshot",
    (
        None,
        _snapshot(org_id="other"),
        _snapshot(credential_id="other"),
        _snapshot(card="other"),
        _snapshot(owner="other"),
        _snapshot(credential_fp="a" * 64),
    ),
)
def test_scope_binding_rejects_absent_or_mismatched_current_source_without_write(
    tmp_path: Path, snapshot: CredentialScopeSnapshot | None
) -> None:
    path = _ready(tmp_path)
    with pytest.raises(SqliteCredentialScopeBindingError):
        reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(snapshot))
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM durable_credential_issue_targets_v1").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM durable_credential_scope_bindings_v1").fetchone() == (0,)
    finally:
        connection.close()


def test_scope_binding_is_immutable_and_rejects_update_delete_and_check_bypass(tmp_path: Path) -> None:
    path = _ready(tmp_path)
    reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(_snapshot()))
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE durable_credential_scope_bindings_v1 SET source_kind='other'")
        connection.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM durable_credential_scope_bindings_v1")
        connection.rollback()
        connection.execute(f"INSERT INTO durable_credential_issue_targets_v1 VALUES ({','.join('?' for _ in _target(org_id='other', target_id='other-target', credential_id='other-credential', owner='other-owner', command_digest='1' * 64).row())})", _target(org_id="other", target_id="other-target", credential_id="other-credential", owner="other-owner", command_digest="1" * 64).row())
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("INSERT INTO durable_credential_scope_bindings_v1 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("other", "other-target", "other-credential", "other-card", "other-owner", "b" * 64, "e" * 64, "f" * 64, "registry", "source-1", 1, "bad", "2026-07-19T00:00:00.000Z"))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialScopeBindingError):
        open_sqlite_durable_credential_scope_bindings(path, source=_Source(_snapshot()))


def test_scope_binding_open_rejects_index_trigger_and_fk_drift(tmp_path: Path) -> None:
    for index, statement in enumerate((
        "DROP INDEX durable_credential_scope_bindings_credential",
        "DROP TRIGGER durable_credential_scope_bindings_v1_no_update",
        "PRAGMA foreign_keys=OFF; CREATE TABLE copied AS SELECT * FROM durable_credential_scope_bindings_v1; DROP TABLE durable_credential_scope_bindings_v1; ALTER TABLE copied RENAME TO durable_credential_scope_bindings_v1",
    )):
        path = _ready(tmp_path / str(index))
        connection = sqlite3.connect(path)
        try:
            connection.executescript(statement)
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(SqliteCredentialScopeBindingError):
            open_sqlite_durable_credential_scope_bindings(path, source=_Source(_snapshot()))


def test_scope_binding_same_semantic_32_way_converges_but_divergent_card_or_owner_rejects(tmp_path: Path) -> None:
    path = _ready(tmp_path)
    barrier = threading.Barrier(32)

    def reserve(index: int) -> None:
        barrier.wait()
        reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now=f"2026-07-19T00:00:00.{index:03}Z", source=_Source(_snapshot()))

    with ThreadPoolExecutor(max_workers=32) as pool:
        [future.result() for future in [pool.submit(reserve, index) for index in range(32)]]
    with pytest.raises(SqliteCredentialScopeBindingError):
        reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="different-card", now="2026-07-19T00:00:00.000Z", source=_Source(_snapshot(card="different-card")))
    with pytest.raises(SqliteCredentialScopeBindingError):
        reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(_snapshot(owner="different-owner")))
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT agent_card_id,owner_subject_id FROM durable_credential_scope_bindings_v1").fetchone() == ("card", "owner")
    finally:
        connection.close()


def test_scope_binding_fault_rolls_back_its_reservation_write(tmp_path: Path) -> None:
    path = _ready(tmp_path)
    with pytest.raises(RuntimeError, match="after_target"):
        reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(_snapshot()), fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(point)))
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM durable_credential_issue_targets_v1").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM durable_credential_scope_bindings_v1").fetchone() == (0,)
    finally:
        connection.close()


def test_scope_binding_replay_ignores_new_now_but_revalidates_full_current_source(tmp_path: Path) -> None:
    path = _ready(tmp_path)
    reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(_snapshot()))
    reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.999Z", source=_Source(_snapshot()))
    for snapshot in (_snapshot(card="other-card"), _snapshot(card_fp="9" * 64), _snapshot(source_ref="source-2", revision=2)):
        with pytest.raises(SqliteCredentialScopeBindingError):
            reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.999Z", source=_Source(snapshot))


def test_scope_binding_rejects_tampered_snapshot_digest_or_replay_source_reference(tmp_path: Path) -> None:
    path = _ready(tmp_path)
    forged = _snapshot()
    object.__setattr__(forged, "snapshot_digest", "0" * 64)
    with pytest.raises(SqliteCredentialScopeBindingError):
        reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(forged))
    reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(_snapshot()))
    with pytest.raises(SqliteCredentialScopeBindingError):
        reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(_snapshot(source_ref="source-2")))


@pytest.mark.parametrize("field", ("source_instance_ref", "snapshot_digest"))
def test_scope_binding_rejects_source_ref_or_digest_tamper_without_write(tmp_path: Path, field: str) -> None:
    path = _ready(tmp_path)
    forged = _snapshot()
    object.__setattr__(forged, field, "source-2" if field == "source_instance_ref" else "0" * 64)
    with pytest.raises(SqliteCredentialScopeBindingError):
        reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(forged))
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM durable_credential_scope_bindings_v1").fetchone() == (0,)
    finally:
        connection.close()


def test_scope_binding_persists_only_opaque_source_metadata_not_secret_body_or_grant(tmp_path: Path) -> None:
    path = _ready(tmp_path)
    for value in ("raw-secret", "grant:abc", "body:content"):
        with pytest.raises(SqliteCredentialScopeBindingError):
            reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id=value, now="2026-07-19T00:00:00.000Z", source=_Source(_snapshot()))
    connection = sqlite3.connect(path)
    try:
        names = {row[1] for row in connection.execute("PRAGMA table_info(durable_credential_scope_bindings_v1)")}
        assert not {"secret", "raw_secret", "body", "grant"} & names
        assert connection.execute("SELECT count(*) FROM durable_credential_scope_bindings_v1").fetchone() == (0,)
    finally:
        connection.close()


def test_open_requires_trusted_source_and_revalidates_each_persisted_binding(tmp_path: Path) -> None:
    path = _ready(tmp_path)
    reserve_sqlite_credential_scope_binding(path, reservation=_target(), agent_card_id="card", now="2026-07-19T00:00:00.000Z", source=_Source(_snapshot()))
    with pytest.raises(SqliteCredentialScopeBindingError):
        open_sqlite_durable_credential_scope_bindings(path, source=None)
    for snapshot in (_snapshot(card_fp="9" * 64), _snapshot(source_ref="source-2", revision=2)):
        with pytest.raises(SqliteCredentialScopeBindingError):
            open_sqlite_durable_credential_scope_bindings(path, source=_Source(snapshot))
    open_sqlite_durable_credential_scope_bindings(path, source=_Source(_snapshot())).close()


def test_open_rejects_check_bypassed_well_formed_forged_source_binding(tmp_path: Path) -> None:
    path = _ready(tmp_path)
    reservation = _target()
    forged = _snapshot(card_fp="9" * 64, owner_fp="8" * 64, source_ref="source-forged", revision=7)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f"INSERT INTO durable_credential_issue_targets_v1 VALUES ({','.join('?' for _ in reservation.row())})",
            reservation.row(),
        )
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "INSERT INTO durable_credential_scope_bindings_v1 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            forged.binding_row(reservation.target_id, "2026-07-19T00:00:00.000Z"),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialScopeBindingError):
        open_sqlite_durable_credential_scope_bindings(path, source=_Source(_snapshot()))
