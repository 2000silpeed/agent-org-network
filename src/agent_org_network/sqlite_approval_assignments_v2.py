"""ADR 0052 attempt-aware durable Approval assignment v2 schema capability.

This is deliberately a separate, validate-only component.  It neither reads nor
migrates ``approval_items`` v1, and it does not activate an Approval operation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, cast

from agent_org_network.approval import ApprovalItem
from agent_org_network.sqlite_completion import (
    SqliteCompletionSchemaError,
    validate_sqlite_completion_connection,
)

SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID: Final = "durable_approval_assignments_v2"
SQLITE_APPROVAL_ASSIGNMENTS_V2_SCHEMA_VERSION: Final = 1
SQLITE_APPROVAL_ASSIGNMENTS_V2_MIGRATION_FAULT_POINTS: Final = (
    "after_assignments",
    "after_current_index",
    "before_manifest_insert",
    "after_manifest_insert",
)
type MigrationFaultInjector = Callable[[str], None]


class SqliteApprovalAssignmentsV2SchemaError(RuntimeError):
    """Attempt-aware durable Approval assignment capability가 canonical schema와 다름."""


_TABLE = """
CREATE TABLE durable_approval_assignments_v2 (
    assignment_id                 TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    org_id                        TEXT NOT NULL COLLATE BINARY,
    request_id                    TEXT NOT NULL COLLATE BINARY,
    awaiting_revision             INTEGER NOT NULL,
    attempt                       INTEGER NOT NULL,
    approval_round                INTEGER NOT NULL,
    supersedes_assignment_id      TEXT COLLATE BINARY,
    status                        TEXT NOT NULL COLLATE BINARY,
    assignment_json               TEXT NOT NULL,
    assignment_sha256             TEXT NOT NULL COLLATE BINARY,
    assignment_schema_version     INTEGER NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (supersedes_assignment_id)
        REFERENCES durable_approval_assignments_v2(assignment_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    UNIQUE (request_id, attempt, approval_round)
)
"""
_CURRENT_INDEX = """
CREATE UNIQUE INDEX ux_durable_approval_assignments_v2_current_open
ON durable_approval_assignments_v2(request_id COLLATE BINARY, attempt)
WHERE status = 'open'
"""
_MANIFEST_TABLE = "schema_component_manifests"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def encode_approval_assignment_v2(item: ApprovalItem) -> tuple[str, str]:
    if type(item) is not ApprovalItem:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "exact ApprovalItem만 v2 snapshot으로 인코드합니다."
        )
    encoded = _canonical_json(item.model_dump(mode="json"))
    return encoded, _digest(encoded)


def _strict_json_object(raw: object) -> dict[str, object]:
    if not isinstance(raw, str):
        raise SqliteApprovalAssignmentsV2SchemaError("v2 assignment JSON은 문자열이어야 합니다.")

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SqliteApprovalAssignmentsV2SchemaError(
                    "v2 assignment JSON에 중복 key가 있습니다."
                )
            result[key] = value
        return result

    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, SqliteApprovalAssignmentsV2SchemaError):
            raise
        raise SqliteApprovalAssignmentsV2SchemaError(
            "v2 assignment JSON을 해석할 수 없습니다."
        ) from error
    if not isinstance(decoded, dict):
        raise SqliteApprovalAssignmentsV2SchemaError("v2 assignment JSON root는 object여야 합니다.")
    return cast(dict[str, object], decoded)


def _decode(
    *, assignment_json: object, assignment_sha256: object, org_id: str, request_id: str
) -> ApprovalItem:
    if not isinstance(assignment_sha256, str) or len(assignment_sha256) != 64:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "v2 assignment digest 형식이 올바르지 않습니다."
        )
    if not isinstance(assignment_json, str) or _digest(assignment_json) != assignment_sha256:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "v2 assignment JSON digest가 일치하지 않습니다."
        )
    payload = _strict_json_object(assignment_json)
    if _canonical_json(payload) != assignment_json:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "v2 assignment JSON은 canonical encoding이어야 합니다."
        )
    try:
        item = ApprovalItem.model_validate_json(assignment_json, strict=True)
    except Exception as error:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "v2 assignment domain snapshot이 유효하지 않습니다."
        ) from error
    if item.org_id != org_id or item.request_id != request_id:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "v2 assignment parent scope가 indexed mirror와 다릅니다."
        )
    return item


def decode_approval_assignment_v2(
    *, assignment_json: object, assignment_sha256: object, org_id: str, request_id: str
) -> ApprovalItem:
    """다른 durable component가 v2 snapshot을 strict하게 읽는 public seam이다."""
    return _decode(
        assignment_json=assignment_json,
        assignment_sha256=assignment_sha256,
        org_id=org_id,
        request_id=request_id,
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _tokens(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise SqliteApprovalAssignmentsV2SchemaError("SQLite v2 DDL을 확인할 수 없습니다.")
    return " ".join(raw.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    if not _table_exists(connection, SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID):
        raise SqliteApprovalAssignmentsV2SchemaError(
            "durable_approval_assignments_v2 table이 없습니다."
        )
    table = connection.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type = 'table' AND name = ?",
        (SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID,),
    ).fetchone()
    index = connection.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type = 'index' AND name = 'ux_durable_approval_assignments_v2_current_open'"
    ).fetchone()
    if table is None or index is None:
        raise SqliteApprovalAssignmentsV2SchemaError("v2 canonical objects가 없습니다.")
    return {
        "component_id": SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID,
        "component_schema_version": SQLITE_APPROVAL_ASSIGNMENTS_V2_SCHEMA_VERSION,
        "table_ddl": _tokens(table[0]),
        "current_index_ddl": _tokens(index[0]),
        "columns": [
            (str(row["name"]), str(row["type"]), bool(row["notnull"]), int(row["pk"]))
            for row in connection.execute(
                'PRAGMA table_xinfo("durable_approval_assignments_v2")'
            ).fetchall()
        ],
        "foreign_keys": [
            tuple(row)
            for row in connection.execute(
                'PRAGMA foreign_key_list("durable_approval_assignments_v2")'
            ).fetchall()
        ],
    }


@lru_cache(maxsize=1)
def _expected_manifest_json() -> str:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        connection.execute(_TABLE)
        connection.execute(_CURRENT_INDEX)
        return _canonical_json(_catalog(connection))
    finally:
        connection.close()


def _require_foreign_keys(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "SQLite v2 connection은 PRAGMA foreign_keys=ON이어야 합니다."
        )


def _manifest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _table_exists(connection, _MANIFEST_TABLE):
        raise SqliteApprovalAssignmentsV2SchemaError(
            "공유 schema_component_manifests table이 없습니다."
        )
    return connection.execute(
        "SELECT component_id, schema_version, manifest_json, manifest_sha256 FROM schema_component_manifests WHERE component_id COLLATE BINARY = ?",
        (SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID,),
    ).fetchone()


def _reconcile_rows(connection: sqlite3.Connection, *, org_id: str | None = None) -> None:
    previous: dict[tuple[str, int], ApprovalItem] = {}
    query = (
        "SELECT assignment_id, org_id, request_id, awaiting_revision, attempt, approval_round, "
        "supersedes_assignment_id, status, assignment_json, assignment_sha256, "
        "assignment_schema_version FROM durable_approval_assignments_v2"
    )
    parameters: tuple[object, ...] = ()
    if org_id is not None:
        query += " WHERE org_id COLLATE BINARY=?"
        parameters = (org_id,)
    rows = connection.execute(
        query + " ORDER BY request_id COLLATE BINARY, attempt, approval_round", parameters
    ).fetchall()
    for row in rows:
        request_id, org_id, attempt = row["request_id"], row["org_id"], row["attempt"]
        if (
            not isinstance(request_id, str)
            or not isinstance(org_id, str)
            or type(attempt) is not int
        ):
            raise SqliteApprovalAssignmentsV2SchemaError(
                "v2 assignment parent/attempt type이 올바르지 않습니다."
            )
        parent = connection.execute(
            "SELECT org_id FROM question_requests WHERE request_id COLLATE BINARY = ?",
            (request_id,),
        ).fetchone()
        if parent is None or parent["org_id"] != org_id:
            raise SqliteApprovalAssignmentsV2SchemaError(
                "v2 assignment parent request/org가 현재 row와 다릅니다."
            )
        if (
            type(row["assignment_schema_version"]) is not int
            or row["assignment_schema_version"] != 1
        ):
            raise SqliteApprovalAssignmentsV2SchemaError(
                "지원하지 않는 v2 assignment payload schema version입니다."
            )
        item = _decode(
            assignment_json=row["assignment_json"],
            assignment_sha256=row["assignment_sha256"],
            org_id=org_id,
            request_id=request_id,
        )
        if not (
            item.item_id == row["assignment_id"]
            and item.awaiting_revision == row["awaiting_revision"]
            and item.attempt == attempt
            and item.approval_round == row["approval_round"]
            and item.supersedes_item_id == row["supersedes_assignment_id"]
            and item.status == row["status"]
        ):
            raise SqliteApprovalAssignmentsV2SchemaError(
                "v2 assignment indexed mirror와 canonical payload가 다릅니다."
            )
        key = (request_id, attempt)
        predecessor = previous.get(key)
        if predecessor is None:
            if item.approval_round != 1 or item.supersedes_item_id is not None:
                raise SqliteApprovalAssignmentsV2SchemaError(
                    "v2 first generation의 round/lineage가 올바르지 않습니다."
                )
        elif not (
            item.approval_round == predecessor.approval_round + 1
            and item.supersedes_item_id == predecessor.item_id
            and predecessor.status == "superseded"
            and predecessor.supersession is not None
            and predecessor.supersession.successor_item_id == item.item_id
        ):
            raise SqliteApprovalAssignmentsV2SchemaError(
                "v2 assignment supersession chain이 canonical lineage가 아닙니다."
            )
        previous[key] = item


def _validate(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    _require_foreign_keys(connection)
    try:
        validate_sqlite_completion_connection(connection)
    except SqliteCompletionSchemaError as error:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "v2 migration에는 capable Question Completion parent가 필요합니다."
        ) from error
    manifest = _manifest(connection)
    expected = _expected_manifest_json()
    if manifest is None:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "v2 manifest가 없습니다. 명시 migration이 필요합니다."
        )
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != SQLITE_APPROVAL_ASSIGNMENTS_V2_SCHEMA_VERSION
    ):
        raise SqliteApprovalAssignmentsV2SchemaError("지원하지 않는 v2 manifest version입니다.")
    if manifest["manifest_json"] != expected or manifest["manifest_sha256"] != _digest(expected):
        raise SqliteApprovalAssignmentsV2SchemaError(
            "저장된 v2 manifest가 canonical 기대값과 다릅니다."
        )
    if _canonical_json(_catalog(connection)) != expected:
        raise SqliteApprovalAssignmentsV2SchemaError("SQLite v2 catalog가 manifest와 다릅니다.")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SqliteApprovalAssignmentsV2SchemaError("SQLite v2 foreign_key_check가 실패했습니다.")
    if reconcile_rows:
        _reconcile_rows(connection, org_id=org_id)


def validate_sqlite_approval_assignments_v2_connection(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    finally:
        connection.row_factory = previous


def _connect_existing(path: str | Path, *, readonly: bool) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise SqliteApprovalAssignmentsV2SchemaError("SQLite v2 runtime은 기존 파일 경로만 엽니다.")
    try:
        resolved = Path(raw).expanduser().resolve(strict=False)
        return sqlite3.connect(
            f"{resolved.as_uri()}?mode={'ro' if readonly else 'rw'}", uri=True, timeout=5.0
        )
    except sqlite3.Error as error:
        raise SqliteApprovalAssignmentsV2SchemaError(
            "SQLite v2 DB를 기존 파일로 열 수 없습니다."
        ) from error


def open_sqlite_approval_assignments_v2_connection(db_path: str | Path) -> sqlite3.Connection:
    connection = _connect_existing(db_path, readonly=False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _validate(connection)
    except Exception:
        connection.close()
        raise
    return connection


def migrate_sqlite_approval_assignments_v2_schema(
    db_path: str | Path, *, fault_injector: MigrationFaultInjector | None = None
) -> None:
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_sqlite_completion_connection(connection)
        except SqliteCompletionSchemaError as error:
            raise SqliteApprovalAssignmentsV2SchemaError(
                "v2 migration에는 capable Question Completion parent가 필요합니다."
            ) from error
        existing = _manifest(connection)
        if existing is not None:
            _validate(connection)
            connection.commit()
            return
        if _table_exists(connection, SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID):
            raise SqliteApprovalAssignmentsV2SchemaError(
                "manifest 없는 v2 partial schema는 복구하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteApprovalAssignmentsV2SchemaError(
                "migration 전 foreign_key_check가 실패했습니다."
            )
        connection.execute(_TABLE)
        if fault_injector is not None:
            fault_injector("after_assignments")
        connection.execute(_CURRENT_INDEX)
        if fault_injector is not None:
            fault_injector("after_current_index")
        expected = _expected_manifest_json()
        if _canonical_json(_catalog(connection)) != expected:
            raise SqliteApprovalAssignmentsV2SchemaError(
                "migration 결과 v2 catalog가 canonical schema와 다릅니다."
            )
        if fault_injector is not None:
            fault_injector("before_manifest_insert")
        connection.execute(
            "INSERT INTO schema_component_manifests (component_id, schema_version, manifest_json, manifest_sha256) VALUES (?, ?, ?, ?)",
            (
                SQLITE_APPROVAL_ASSIGNMENTS_V2_COMPONENT_ID,
                SQLITE_APPROVAL_ASSIGNMENTS_V2_SCHEMA_VERSION,
                expected,
                _digest(expected),
            ),
        )
        if fault_injector is not None:
            fault_injector("after_manifest_insert")
        _validate(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


@dataclass(frozen=True)
class ApprovalAssignmentsV2SchemaReconciliationReport:
    capable: bool
    detail: str
    assignment_manifest_present: bool


def reconcile_sqlite_approval_assignments_v2_schema(
    db_path: str | Path,
) -> ApprovalAssignmentsV2SchemaReconciliationReport:
    present = False
    try:
        connection = _connect_existing(db_path, readonly=True)
    except SqliteApprovalAssignmentsV2SchemaError as error:
        return ApprovalAssignmentsV2SchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        present = _table_exists(connection, _MANIFEST_TABLE) and _manifest(connection) is not None
        _validate(connection)
        return ApprovalAssignmentsV2SchemaReconciliationReport(True, "capable_v2", present)
    except Exception as error:
        return ApprovalAssignmentsV2SchemaReconciliationReport(False, str(error), present)
    finally:
        connection.close()
