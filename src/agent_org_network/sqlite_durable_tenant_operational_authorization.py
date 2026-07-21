"""R1.2 approval-evidence companion for R1.0 operational receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Final

from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    open_sqlite_durable_tenant_operational_mutations,
)

COMPONENT_ID: Final = "durable_tenant_operational_authorization_v1"
SCHEMA_VERSION: Final = 2
TABLE: Final = "durable_tenant_operational_authorization_evidence"
type FaultInjector = Callable[[str], None]

_DDL: Final = f"""CREATE TABLE {TABLE} (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 principal_id TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY CHECK(length(command_digest)=64),
 pre_resource_json TEXT NOT NULL COLLATE BINARY,
 post_resource_json TEXT NOT NULL COLLATE BINARY,
 post_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK(length(post_resource_fingerprint)=64),
 pre_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK(length(pre_resource_fingerprint)=64),
 evidence_id TEXT NOT NULL COLLATE BINARY,
 approver_subject_id TEXT NOT NULL COLLATE BINARY,
 approval_command_digest TEXT NOT NULL COLLATE BINARY CHECK(length(approval_command_digest)=64),
 approval_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK(length(approval_resource_fingerprint)=64),
 approved_at TEXT NOT NULL COLLATE BINARY,
 FOREIGN KEY(org_id, receipt_id) REFERENCES durable_tenant_operational_mutation_receipts(org_id, receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""


class SqliteDurableTenantOperationalAuthorizationError(RuntimeError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manifest() -> str:
    return _canonical({"component_id": COMPONENT_ID, "version": SCHEMA_VERSION, "tables": [TABLE]})


def _same(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()) == " ".join(expected.split())


def _validate(connection: sqlite3.Connection) -> None:
    try:
        open_sqlite_durable_tenant_operational_mutations(connection).validate_only()
    except Exception as error:
        raise SqliteDurableTenantOperationalAuthorizationError(
            "R1.2에는 R1.0 parent가 필요합니다."
        ) from error
    marker = connection.execute(
        "SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id=?",
        (COMPONENT_ID,),
    ).fetchone()
    manifest = _manifest()
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    if (
        marker is None
        or tuple(marker) != (SCHEMA_VERSION, manifest, _digest(manifest))
        or row is None
        or not _same(row[0], _DDL)
    ):
        raise SqliteDurableTenantOperationalAuthorizationError(
            "R1.2 catalog가 canonical하지 않습니다."
        )
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SqliteDurableTenantOperationalAuthorizationError("R1.2 foreign key가 손상되었습니다.")


def migrate_sqlite_durable_tenant_operational_authorization(
    connection: sqlite3.Connection, *, fault_injector: FaultInjector | None = None
) -> None:
    open_sqlite_durable_tenant_operational_mutations(connection).validate_only()
    try:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone()
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        if exists is not None or table_exists is not None:
            _validate(connection)
            connection.commit()
            return
        connection.execute(_DDL)
        if fault_injector:
            fault_injector("after_evidence")
        if fault_injector:
            fault_injector("before_manifest")
        manifest = _manifest()
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES(?,?,?,?)",
            (COMPONENT_ID, SCHEMA_VERSION, manifest, _digest(manifest)),
        )
        if fault_injector:
            fault_injector("after_manifest")
        _validate(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def open_sqlite_durable_tenant_operational_authorization(connection: sqlite3.Connection) -> None:
    _validate(connection)
