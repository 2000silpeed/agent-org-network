"""R5.3 immutable committed Credential Scope Projection capability."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from agent_org_network.sqlite_durable_credential_scope_bindings import (
    CredentialScopeSource,
    open_sqlite_durable_credential_scope_bindings,
    validate_sqlite_durable_credential_scope_bindings_connection,
)

COMPONENT_ID: Final = "durable_credential_scope_projections_v1"
_TABLE: Final = "durable_credential_scope_projections_v1"
_INDEX: Final = "durable_credential_scope_projections_credential"
_UPDATE: Final = "durable_credential_scope_projections_v1_no_update"
_DELETE: Final = "durable_credential_scope_projections_v1_no_delete"
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN: Final = (
    "secret", "grant", "rationale", "body", "password", "token", "key", "bearer", "authorization",
)


def _opaque_sql(field: str) -> str:
    return " AND ".join((
        f"length({field}) BETWEEN 1 AND 128", f"{field} GLOB '[A-Za-z0-9]*'",
        f"{field} NOT GLOB '*[^A-Za-z0-9._:-]*'",
        *(f"instr(lower({field}), '{word}') = 0" for word in _FORBIDDEN),
    ))


def _hash_sql(field: str) -> str:
    return f"length({field})=64 AND {field} NOT GLOB '*[^0-9a-f]*'"

_DDL: Final = f"""CREATE TABLE {_TABLE} (
 org_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("org_id")}), target_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("target_id")}),
 credential_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("credential_id")}), agent_card_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("agent_card_id")}),
 owner_subject_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("owner_subject_id")}), source_kind TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("source_kind")}),
 credential_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("credential_resource_fingerprint")}), card_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("card_resource_fingerprint")}), owner_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("owner_resource_fingerprint")}),
 source_instance_ref TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("source_instance_ref")}), scope_revision INTEGER NOT NULL CHECK(scope_revision>=1), binding_created_at TEXT NOT NULL COLLATE BINARY CHECK(length(binding_created_at)=24 AND binding_created_at GLOB '????-??-??T??:??:??.???Z' AND COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',binding_created_at)=binding_created_at,0)), target_generation INTEGER NOT NULL CHECK(target_generation>=1),
 snapshot_digest TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("snapshot_digest")} ),
 committed_at TEXT NOT NULL COLLATE BINARY CHECK(length(committed_at)=24 AND committed_at GLOB '????-??-??T??:??:??.???Z' AND COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',committed_at)=committed_at,0)), projection_digest TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("projection_digest")} ),
 PRIMARY KEY(org_id,target_id), UNIQUE(org_id,credential_id),
 FOREIGN KEY(org_id,target_id) REFERENCES durable_credential_scope_bindings_v1(org_id,target_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(org_id,credential_id) REFERENCES durable_credentials(org_id,credential_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_INDEX_DDL: Final = f"CREATE UNIQUE INDEX {_INDEX} ON {_TABLE}(org_id,credential_id)"
_UPDATE_DDL: Final = f"CREATE TRIGGER {_UPDATE} BEFORE UPDATE ON {_TABLE} FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'credential scope projection is immutable'); END"
_DELETE_DDL: Final = f"CREATE TRIGGER {_DELETE} BEFORE DELETE ON {_TABLE} FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'credential scope projection deletion is forbidden'); END"


class SqliteCredentialScopeProjectionError(RuntimeError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _manifest() -> str:
    return _canonical({
        "component_id": COMPONENT_ID, "schema_version": 1,
        "catalog": {
            "tables": [{"name": _TABLE, "sql": " ".join(_DDL.split())}],
            "indexes": [{"name": _INDEX, "sql": " ".join(_INDEX_DDL.split())}],
            "triggers": [
                {"name": _UPDATE, "sql": " ".join(_UPDATE_DDL.split())},
                {"name": _DELETE, "sql": " ".join(_DELETE_DDL.split())},
            ],
        },
    })


def _same(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()).replace("( ", "(").replace(" )", ")") == " ".join(expected.split()).replace("( ", "(").replace(" )", ")")


def _sql(connection: sqlite3.Connection, kind: str, name: str) -> object:
    row = connection.execute("SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind, name)).fetchone()
    return None if row is None else row[0]


def _timestamp(value: object) -> bool:
    if type(value) is not str or len(value) != 24:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z") == value
    except ValueError:
        return False


def _safe(value: object) -> bool:
    return type(value) is str and _OPAQUE.fullmatch(value) is not None and all(word not in value.lower() for word in _FORBIDDEN)


def _sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _projection_digest(row: tuple[object, ...]) -> str:
    """Digest every immutable projection fact except this digest itself."""
    fields = (
        "org_id", "target_id", "credential_id", "agent_card_id", "owner_subject_id",
        "source_kind", "credential_resource_fingerprint", "card_resource_fingerprint",
        "owner_resource_fingerprint", "source_instance_ref", "scope_revision",
        "binding_created_at", "target_generation", "snapshot_digest", "committed_at",
    )
    return hashlib.sha256(_canonical(dict(zip(fields, row, strict=True))).encode()).hexdigest()


def _validate(connection: sqlite3.Connection, source: CredentialScopeSource | None) -> None:
    if source is not None:
        try:
            validate_sqlite_durable_credential_scope_bindings_connection(connection, source=source)
        except Exception as error:
            raise SqliteCredentialScopeProjectionError("R5.1 binding parent가 unavailable입니다.") from error
    manifest = _manifest()
    marker = connection.execute("SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)).fetchone()
    fks = [tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({_TABLE})")]
    expected_fks = [
        (0, 0, "durable_credentials", "org_id", "org_id", "RESTRICT", "RESTRICT", "NONE"),
        (0, 1, "durable_credentials", "credential_id", "credential_id", "RESTRICT", "RESTRICT", "NONE"),
        (1, 0, "durable_credential_scope_bindings_v1", "org_id", "org_id", "RESTRICT", "RESTRICT", "NONE"),
        (1, 1, "durable_credential_scope_bindings_v1", "target_id", "target_id", "RESTRICT", "RESTRICT", "NONE"),
    ]
    catalog = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT type,name FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') "
            "AND sql IS NOT NULL",
            (_TABLE,),
        )
    }
    if (
        marker is None or tuple(marker) != (1, manifest, hashlib.sha256(manifest.encode()).hexdigest())
        or not _same(_sql(connection, "table", _TABLE), _DDL)
        or not _same(_sql(connection, "index", _INDEX), _INDEX_DDL)
        or not _same(_sql(connection, "trigger", _UPDATE), _UPDATE_DDL)
        or not _same(_sql(connection, "trigger", _DELETE), _DELETE_DDL)
        or fks != expected_fks
        or catalog != {("index", _INDEX), ("trigger", _UPDATE), ("trigger", _DELETE)}
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise SqliteCredentialScopeProjectionError("R5.3 projection catalog가 canonical하지 않습니다.")
    for row in connection.execute(f"SELECT * FROM {_TABLE}"):
        binding = connection.execute("SELECT * FROM durable_credential_scope_bindings_v1 WHERE org_id=? AND target_id=?", (row["org_id"], row["target_id"])).fetchone()
        target = connection.execute("SELECT credential_id,owner_subject_id,resource_fingerprint,target_generation,state FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?", (row["org_id"], row["target_id"])).fetchone()
        credential = connection.execute("SELECT credential_id,owner_subject_id,generation,revision,status,issued_at FROM durable_credentials WHERE org_id=? AND credential_id=?", (row["org_id"], row["credential_id"])).fetchone()
        fence = connection.execute("SELECT state,delivery_ref FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?", (row["org_id"], row["target_id"])).fetchone()
        receipt = connection.execute("SELECT credential_id,result_revision,delivery_ref FROM credential_command_receipts WHERE org_id=? AND request_id=? AND attempt=?", (row["org_id"], row["target_id"], row["target_generation"])).fetchone()
        if (
            not all(_safe(row[field]) for field in ("org_id", "target_id", "credential_id", "agent_card_id", "owner_subject_id", "source_kind", "source_instance_ref"))
            or type(row["scope_revision"]) is not int or row["scope_revision"] < 1
            or type(row["target_generation"]) is not int or row["target_generation"] < 1
            or not all(_sha256(row[field]) for field in ("credential_resource_fingerprint", "card_resource_fingerprint", "owner_resource_fingerprint", "snapshot_digest", "projection_digest"))
            or not _timestamp(row["binding_created_at"]) or not _timestamp(row["committed_at"])
            or binding is None or target is None or credential is None or fence is None or receipt is None
            or tuple(row[:13]) != (binding["org_id"], binding["target_id"], binding["credential_id"], binding["agent_card_id"], binding["owner_subject_id"], binding["source_kind"], binding["credential_resource_fingerprint"], binding["card_resource_fingerprint"], binding["owner_resource_fingerprint"], binding["source_instance_ref"], binding["scope_revision"], binding["created_at"], target["target_generation"])
            or row["snapshot_digest"] != binding["snapshot_digest"]
            or tuple(target) != (row["credential_id"], row["owner_subject_id"], row["credential_resource_fingerprint"], row["target_generation"], "Committed")
            # Projection is the immutable issuance anchor, not a frozen lifecycle
            # mirror: a same-generation credential may later be revoked/revised.
            or credential[0] != row["credential_id"]
            or credential[1] != row["owner_subject_id"]
            or credential[2] != row["target_generation"]
            or type(credential[3]) is not int or credential[3] < 1
            or credential[4] not in ("active", "revoked")
            or credential[5] != row["committed_at"]
            or tuple(fence) != ("Committed", receipt[2])
            or tuple(receipt) != (row["credential_id"], 1, fence["delivery_ref"])
            or row["projection_digest"] != _projection_digest(tuple(row[:-1]))
        ):
            raise SqliteCredentialScopeProjectionError("R5.3 projection row가 canonical하지 않습니다.")


def migrate_sqlite_durable_credential_scope_projections(connection: sqlite3.Connection, *, fault_injector: Callable[[str], None] | None = None) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        present = any(_sql(connection, kind, name) is not None for kind, name in (("table", _TABLE), ("index", _INDEX), ("trigger", _UPDATE), ("trigger", _DELETE))) or connection.execute("SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)).fetchone() is not None
        if present:
            _validate(connection, None)
        else:
            connection.execute(_DDL)
            connection.execute(_INDEX_DDL)
            connection.execute(_UPDATE_DDL)
            connection.execute(_DELETE_DDL)
            if fault_injector:
                fault_injector("after_projection")
            manifest = _manifest()
            connection.execute("INSERT INTO schema_component_manifests VALUES(?,?,?,?)", (COMPONENT_ID, 1, manifest, hashlib.sha256(manifest.encode()).hexdigest()))
            _validate(connection, None)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.row_factory = previous


def open_sqlite_durable_credential_scope_projections(path: str | Path, *, source: CredentialScopeSource) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = open_sqlite_durable_credential_scope_bindings(path, source=source)
        _validate(connection, source)
        return connection
    except Exception as error:
        if connection is not None:
            connection.close()
        if isinstance(error, SqliteCredentialScopeProjectionError):
            raise
        raise SqliteCredentialScopeProjectionError("R5.3 projection capability가 unavailable입니다.") from error


def open_sqlite_durable_credential_scope_projections_for_scoped_read(
    path: str | Path,
) -> sqlite3.Connection:
    """Open the immutable anchor for R5.4a; current source proof is a separate port."""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        _validate(connection, None)
        return connection
    except Exception as error:
        if connection is not None:
            connection.close()
        if isinstance(error, SqliteCredentialScopeProjectionError):
            raise
        raise SqliteCredentialScopeProjectionError("R5.3 scoped read anchor가 unavailable입니다.") from error


def insert_sqlite_durable_credential_scope_projection(connection: sqlite3.Connection, *, org_id: str, target_id: str, now: str, source: CredentialScopeSource) -> None:
    if not _safe(org_id) or not _safe(target_id) or not _timestamp(now):
        raise SqliteCredentialScopeProjectionError("projection input이 strict하지 않습니다.")
    _validate(connection, source)
    binding = connection.execute("SELECT * FROM durable_credential_scope_bindings_v1 WHERE org_id=? AND target_id=?", (org_id, target_id)).fetchone()
    if binding is None:
        raise SqliteCredentialScopeProjectionError("exact scope binding이 필요합니다.")
    target = connection.execute("SELECT target_generation,state FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?", (org_id, target_id)).fetchone()
    if target is None or target["state"] != "Committed":
        raise SqliteCredentialScopeProjectionError("committed target이 필요합니다.")
    facts = (binding["org_id"], binding["target_id"], binding["credential_id"], binding["agent_card_id"], binding["owner_subject_id"], binding["source_kind"], binding["credential_resource_fingerprint"], binding["card_resource_fingerprint"], binding["owner_resource_fingerprint"], binding["source_instance_ref"], binding["scope_revision"], binding["created_at"], target["target_generation"], binding["snapshot_digest"], now)
    row = (*facts, _projection_digest(facts))
    old = connection.execute(f"SELECT * FROM {_TABLE} WHERE org_id=? AND target_id=?", (org_id, target_id)).fetchone()
    if old is None:
        connection.execute(f"INSERT INTO {_TABLE} VALUES({','.join('?' for _ in row)})", row)
    elif tuple(old)[:-1] != row[:-1]:
        raise SqliteCredentialScopeProjectionError("projection replay가 exact하지 않습니다.")


def validate_sqlite_durable_credential_scope_projections_connection(
    connection: sqlite3.Connection, *, source: CredentialScopeSource
) -> None:
    """Validate R5.3 inside the materialization transaction; never repair it."""
    if not callable(getattr(source, "resolve_issue_scope", None)):
        raise SqliteCredentialScopeProjectionError("trusted scope source가 필요합니다.")
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, source)
    finally:
        connection.row_factory = previous
