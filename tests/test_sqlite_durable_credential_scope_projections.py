"""R5.3 committed Credential Scope Projection schema regressions."""

from __future__ import annotations

import hashlib
import json
import runpy
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from agent_org_network.sqlite_durable_credential_issue_targets import (
    _PARENT_DDLS,  # pyright: ignore[reportPrivateUsage]
    migrate_sqlite_durable_credential_issue_targets_schema,
)
from agent_org_network.sqlite_durable_credential_scope_bindings import (
    CredentialScopeSnapshot,
    migrate_sqlite_durable_credential_scope_bindings,
)
from agent_org_network.sqlite_durable_credential_scope_projections import (
    SqliteCredentialScopeProjectionError,
    migrate_sqlite_durable_credential_scope_projections,
    open_sqlite_durable_credential_scope_projections,
)


# R5.3 is the committed companion of the R5.2 scoped materialization path.
# Keep this matrix in its own test module while reusing the deterministic scoped
# composition fixture (there is deliberately no real delivery or LLM here).
_SCOPED = runpy.run_path("tests/test_credential_issue_scoped_operations.py")


def _committed(path: Path) -> tuple[Any, Any]:
    operations, source, _principal, _evidence, _delivery = _SCOPED["_operations"](path)
    reservation = _SCOPED["_reservation"]()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    return operations, source


def _projection_counts(connection: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "durable_credentials", "credential_command_receipts", "credential_audit_intents",
            "credential_outbox_intents", "durable_credential_issue_targets_v1",
            "credential_issue_stage_fences_v2", "durable_credential_scope_projections_v1",
        )
    )


def _raise_at_projection_fault(point: str) -> Any:
    def inject(actual: str) -> None:
        if actual == point:
            raise RuntimeError(actual)

    return inject


class _Source:
    def resolve_issue_scope(
        self, org_id: str, credential_id: str, agent_card_id: str
    ) -> CredentialScopeSnapshot | None:
        values = {
            "agent_card_id": agent_card_id,
            "card_resource_fingerprint": "c" * 64,
            "credential_id": credential_id,
            "credential_resource_fingerprint": "b" * 64,
            "org_id": org_id,
            "owner_resource_fingerprint": "d" * 64,
            "owner_subject_id": "owner",
            "source_instance_ref": "source",
            "source_kind": "registry",
            "scope_revision": 1,
        }
        return CredentialScopeSnapshot(
            org_id, credential_id, agent_card_id, "owner", "b" * 64, "c" * 64,
            "d" * 64, "registry", "source", 1,
            hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "2026-07-19T00:00:00.000Z",
        )


def _parent(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for ddl in _PARENT_DDLS.values():
            connection.execute(ddl)
    finally:
        connection.close()
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = sqlite3.connect(path)
    try:
        migrate_sqlite_durable_credential_scope_bindings(connection)
    finally:
        connection.close()


def test_projection_migration_is_idempotent_and_requires_full_canonical_catalog(tmp_path: Path) -> None:
    path = tmp_path / "projection.sqlite"
    _parent(path)
    connection = sqlite3.connect(path)
    try:
        migrate_sqlite_durable_credential_scope_projections(connection)
        migrate_sqlite_durable_credential_scope_projections(connection)
        assert connection.execute(
            "SELECT count(*) FROM schema_component_manifests "
            "WHERE component_id='durable_credential_scope_projections_v1'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND name LIKE "
            "'durable_credential_scope_projections_v1_no_%' ORDER BY name"
        ).fetchall() == [
            ("durable_credential_scope_projections_v1_no_delete",),
            ("durable_credential_scope_projections_v1_no_update",),
        ]
        connection.execute("DROP INDEX durable_credential_scope_projections_credential")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialScopeProjectionError):
        open_sqlite_durable_credential_scope_projections(path, source=_Source())


@pytest.mark.parametrize(
    "statement",
    (
        "DROP TRIGGER durable_credential_scope_projections_v1_no_update",
        "DROP TRIGGER durable_credential_scope_projections_v1_no_delete",
        "DELETE FROM schema_component_manifests WHERE component_id='durable_credential_scope_projections_v1'",
    ),
)
def test_projection_open_rejects_partial_catalog_instead_of_repairing(
    tmp_path: Path, statement: str
) -> None:
    path = tmp_path / "partial.sqlite"
    _parent(path)
    connection = sqlite3.connect(path)
    try:
        migrate_sqlite_durable_credential_scope_projections(connection)
        connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialScopeProjectionError):
        open_sqlite_durable_credential_scope_projections(path, source=_Source())


@pytest.mark.parametrize("point", ("after_projection_insert", "after_projection_readback"))
def test_projection_fault_rolls_back_every_materialization_companion(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / f"fault-{point}.sqlite"
    operations, _source, _principal, _evidence, _delivery = _SCOPED["_operations"](path)
    reservation = _SCOPED["_reservation"]()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    with pytest.raises(Exception):
        operations._materialize_for_test(  # pyright: ignore[reportPrivateUsage]
            path, "org", "target", "2026-07-19T00:00:01.000Z",
            fault_injector=_raise_at_projection_fault(point),
        )
    connection = sqlite3.connect(path)
    try:
        assert _projection_counts(connection) == (0, 0, 0, 0, 1, 1, 0)
        assert connection.execute("SELECT state FROM durable_credential_issue_targets_v1").fetchone() == ("Staged",)
        assert connection.execute("SELECT state FROM credential_issue_stage_fences_v2").fetchone() == ("Staged",)
    finally:
        connection.close()


def test_thirty_two_way_materialize_replay_has_one_committed_projection_and_ref(
    tmp_path: Path,
) -> None:
    path = tmp_path / "thirty-two.sqlite"
    operations, _source, _principal, _evidence, _delivery = _SCOPED["_operations"](path)
    reservation = _SCOPED["_reservation"]()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    def materialize_one(_: int) -> Any:
        return operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(materialize_one, range(32)))
    assert len(set(results)) == 1
    connection = sqlite3.connect(path)
    try:
        assert _projection_counts(connection) == (1, 1, 1, 1, 1, 1, 1)
        assert connection.execute(
            "SELECT count(*), min(committed_at), max(committed_at) "
            "FROM durable_credential_scope_projections_v1"
        ).fetchone() == (1, "2026-07-19T00:00:01.000Z", "2026-07-19T00:00:01.000Z")
    finally:
        connection.close()


def test_check_bypass_well_formed_forged_projection_is_unavailable_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged.sqlite"
    _operations, source = _committed(path)
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT * FROM durable_credential_scope_projections_v1"
        ).fetchone()
        assert row is not None
        forged = list(row)
        forged[1] = "orphan-target"
        forged[2] = "orphan-credential"
        # The row is deliberately CHECK-well-formed and has a valid self digest;
        # only its durable provenance is false.
        fields = (
            "org_id", "target_id", "credential_id", "agent_card_id", "owner_subject_id",
            "source_kind", "credential_resource_fingerprint", "card_resource_fingerprint",
            "owner_resource_fingerprint", "source_instance_ref", "scope_revision",
            "binding_created_at", "target_generation", "snapshot_digest", "committed_at",
        )
        forged[-1] = hashlib.sha256(json.dumps(
            dict(zip(fields, forged[:-1], strict=True)), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO durable_credential_scope_projections_v1 VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", forged
        )
        connection.commit()
        before = _projection_counts(connection)
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialScopeProjectionError):
        open_sqlite_durable_credential_scope_projections(path, source=source)
    connection = sqlite3.connect(path)
    try:
        assert _projection_counts(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize("drift", ("binding", "source", "target", "credential"))
def test_projection_provenance_drift_is_unavailable_without_mutation(
    tmp_path: Path, drift: str
) -> None:
    path = tmp_path / f"provenance-{drift}.sqlite"
    _operations, source = _committed(path)
    connection = sqlite3.connect(path)
    try:
        if drift == "binding":
            binding = connection.execute(
                "SELECT * FROM durable_credential_scope_bindings_v1"
            ).fetchone()
            binding_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' "
                "AND name='durable_credential_scope_bindings_v1'"
            ).fetchone()
            binding_index = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='index' "
                "AND name='durable_credential_scope_bindings_credential'"
            ).fetchone()
            binding_triggers = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' "
                "AND name LIKE 'durable_credential_scope_bindings_v1_no_%' ORDER BY name"
            ).fetchall()
            assert binding is not None and binding_sql is not None and binding_index is not None
            assert len(binding_triggers) == 2
            connection.execute("DROP TRIGGER durable_credential_scope_bindings_v1_no_update")
            connection.execute("DROP TRIGGER durable_credential_scope_bindings_v1_no_delete")
            connection.execute("DROP INDEX durable_credential_scope_bindings_credential")
            connection.execute("DROP TABLE durable_credential_scope_bindings_v1")
            connection.execute(str(binding_sql[0]))
            connection.execute(str(binding_index[0]))
            for trigger in binding_triggers:
                connection.execute(str(trigger[0]))
            forged_binding = list(binding)
            forged_binding[9] = "other-source"
            connection.execute(
                "INSERT INTO durable_credential_scope_bindings_v1 VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", forged_binding
            )
            connection.commit()
        elif drift == "source":
            source.available = False
        elif drift == "target":
            connection.execute(
                "UPDATE durable_credential_issue_targets_v1 SET state='CleanupPending' "
                "WHERE org_id='org' AND target_id='target'"
            )
            connection.commit()
        else:
            connection.execute(
                "UPDATE durable_credentials SET owner_subject_id='different-owner' "
                "WHERE org_id='org' AND credential_id='credential'"
            )
            connection.commit()
        before = _projection_counts(connection)
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialScopeProjectionError):
        open_sqlite_durable_credential_scope_projections(path, source=source)
    connection = sqlite3.connect(path)
    try:
        assert _projection_counts(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ("foreign", "mixed_org", "orphan"))
def test_foreign_mixed_org_and_orphan_projection_rows_are_unavailable(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / f"{kind}.sqlite"
    _operations, source = _committed(path)
    connection = sqlite3.connect(path)
    try:
        row = list(connection.execute("SELECT * FROM durable_credential_scope_projections_v1").fetchone())
        if kind == "foreign":
            row[0] = "foreign"
        elif kind == "mixed_org":
            row[0] = "other-org"
            row[1] = "other-target"
        else:
            row[1] = "orphan-target"
            row[2] = "orphan-credential"
        fields = (
            "org_id", "target_id", "credential_id", "agent_card_id", "owner_subject_id",
            "source_kind", "credential_resource_fingerprint", "card_resource_fingerprint",
            "owner_resource_fingerprint", "source_instance_ref", "scope_revision",
            "binding_created_at", "target_generation", "snapshot_digest", "committed_at",
        )
        row[-1] = hashlib.sha256(json.dumps(dict(zip(fields, row[:-1], strict=True)), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO durable_credential_scope_projections_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        connection.commit()
        before = _projection_counts(connection)
    finally:
        connection.close()

    with pytest.raises(SqliteCredentialScopeProjectionError):
        open_sqlite_durable_credential_scope_projections(path, source=source)
    connection = sqlite3.connect(path)
    try:
        assert _projection_counts(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize("drift", ("catalog", "foreign_key", "index", "trigger"))
def test_projection_migration_catalog_fk_index_and_trigger_drift_are_unavailable(
    tmp_path: Path, drift: str
) -> None:
    path = tmp_path / f"catalog-{drift}.sqlite"
    _parent(path)
    connection = sqlite3.connect(path)
    try:
        migrate_sqlite_durable_credential_scope_projections(connection)
        if drift == "catalog":
            connection.execute(
                "UPDATE schema_component_manifests SET manifest_sha256=? "
                "WHERE component_id='durable_credential_scope_projections_v1'"
                , ("0" * 64,)
            )
        elif drift == "index":
            connection.execute("DROP INDEX durable_credential_scope_projections_credential")
        elif drift == "trigger":
            connection.execute("DROP TRIGGER durable_credential_scope_projections_v1_no_delete")
        else:
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' "
                "AND name='durable_credential_scope_projections_v1'"
            ).fetchone()
            index_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='index' "
                "AND name='durable_credential_scope_projections_credential'"
            ).fetchone()
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' "
                "AND name LIKE 'durable_credential_scope_projections_v1_no_%' ORDER BY name"
            ).fetchall()
            assert table_sql is not None and index_sql is not None and len(trigger_sql) == 2
            connection.execute("DROP TRIGGER durable_credential_scope_projections_v1_no_update")
            connection.execute("DROP TRIGGER durable_credential_scope_projections_v1_no_delete")
            connection.execute("DROP INDEX durable_credential_scope_projections_credential")
            connection.execute("DROP TABLE durable_credential_scope_projections_v1")
            connection.execute(str(table_sql[0]).replace("ON UPDATE RESTRICT", "ON UPDATE CASCADE", 1))
            connection.execute(str(index_sql[0]))
            for trigger in trigger_sql:
                connection.execute(str(trigger[0]))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialScopeProjectionError):
        open_sqlite_durable_credential_scope_projections(path, source=_Source())


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE INDEX durable_credential_scope_projections_extra "
        "ON durable_credential_scope_projections_v1(target_id)",
        "CREATE TRIGGER durable_credential_scope_projections_v1_extra "
        "BEFORE INSERT ON durable_credential_scope_projections_v1 "
        "BEGIN SELECT 1; END",
    ),
)
def test_projection_extra_catalog_member_is_unavailable(
    tmp_path: Path, statement: str
) -> None:
    path = tmp_path / "extra-catalog.sqlite"
    _parent(path)
    connection = sqlite3.connect(path)
    try:
        migrate_sqlite_durable_credential_scope_projections(connection)
        connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialScopeProjectionError):
        open_sqlite_durable_credential_scope_projections(path, source=_Source())


def test_committed_read_rejects_missing_projection_after_catalog_bypass(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-projection.sqlite"
    operations, _source = _committed(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER durable_credential_scope_projections_v1_no_delete")
        connection.execute("DELETE FROM durable_credential_scope_projections_v1")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(Exception):
        operations.read_committed(path, "org", "target")


def test_already_committed_orchestration_never_releases_when_projection_is_missing(
    tmp_path: Path,
) -> None:
    fixture = runpy.run_path("tests/test_credential_issue_scoped_orchestration.py")
    path = tmp_path / "orchestration-projection.sqlite"
    bridge, _scoped, _source, _principal, _evidence, _provider, delivery, _secrets = fixture[
        "_bridge"
    ](path)
    command = fixture["_command"]()
    assert bridge.issue(command) == fixture["Issued"]("credential", fixture["REF"])
    assert delivery.releases == 1
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER durable_credential_scope_projections_v1_no_delete")
        connection.execute("DELETE FROM durable_credential_scope_projections_v1")
        connection.commit()
    finally:
        connection.close()
    assert bridge.issue(command) == fixture["Unavailable"]()
    assert delivery.releases == 1
