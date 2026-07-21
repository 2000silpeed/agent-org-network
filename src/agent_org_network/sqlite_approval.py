"""ADR 0052의 Approval-first SQLite schema capability.

이 모듈은 Approval operation을 실행하거나 기존 in-memory item을 이관하지 않는다.
명시 migration은 이미 검증된 Question Completion component 위에 ``approval_items``
저장 계약만 추가한다. runtime open은 검증만 하며 DDL 또는 manifest 보정을 하지 않는다.
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

SQLITE_APPROVAL_COMPONENT_ID: Final = "approval_items"
SQLITE_APPROVAL_SCHEMA_VERSION: Final = 1
SQLITE_APPROVAL_MIGRATION_FAULT_POINTS: Final = (
    "after_approval_items",
    "after_current_index",
    "before_manifest_insert",
    "after_manifest_insert",
)

type MigrationFaultInjector = Callable[[str], None]


class SqliteApprovalSchemaError(RuntimeError):
    """Approval component schema가 canonical capability와 다름."""


_APPROVAL_ITEMS_TABLE = """
CREATE TABLE approval_items (
    item_id              TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    org_id               TEXT NOT NULL COLLATE BINARY,
    request_id           TEXT NOT NULL COLLATE BINARY,
    awaiting_revision    INTEGER NOT NULL,
    attempt              INTEGER NOT NULL,
    approval_round       INTEGER NOT NULL,
    supersedes_item_id   TEXT COLLATE BINARY,
    status               TEXT NOT NULL COLLATE BINARY,
    item_json            TEXT NOT NULL,
    item_sha256          TEXT NOT NULL COLLATE BINARY,
    item_schema_version  INTEGER NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (supersedes_item_id) REFERENCES approval_items(item_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    UNIQUE (request_id, approval_round)
)
"""
_APPROVAL_CURRENT_INDEX = """
CREATE UNIQUE INDEX ux_approval_items_current_open
ON approval_items(request_id COLLATE BINARY)
WHERE status = 'open'
"""
_MANIFEST_TABLE = "schema_component_manifests"


def _fault(injector: MigrationFaultInjector | None, point: str) -> None:
    if injector is not None:
        injector(point)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encode_approval_item(item: ApprovalItem) -> tuple[str, str]:
    """ApprovalItem의 JSON-mode snapshot과 digest를 canonical 형태로 만든다."""
    if type(item) is not ApprovalItem:
        raise SqliteApprovalSchemaError(
            "exact ApprovalItem만 SQLite approval schema로 인코드합니다."
        )
    encoded = _canonical_json(item.model_dump(mode="json"))
    return encoded, _digest(encoded)


def _strict_json_object(raw: object) -> dict[str, object]:
    if not isinstance(raw, str):
        raise SqliteApprovalSchemaError("Approval item JSON은 문자열이어야 합니다.")

    def reject_duplicate(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise SqliteApprovalSchemaError("Approval item JSON에 중복 key가 있습니다.")
            value[key] = item
        return value

    def reject_constant(value: str) -> object:
        raise SqliteApprovalSchemaError(
            f"Approval item JSON의 비표준 수는 허용하지 않습니다: {value}"
        )

    try:
        decoded = json.loads(
            raw, object_pairs_hook=reject_duplicate, parse_constant=reject_constant
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, SqliteApprovalSchemaError):
            raise
        raise SqliteApprovalSchemaError("Approval item JSON을 해석할 수 없습니다.") from error
    if not isinstance(decoded, dict):
        raise SqliteApprovalSchemaError("Approval item JSON root는 object여야 합니다.")
    return cast(dict[str, object], decoded)


def decode_approval_item(
    *,
    item_json: object,
    item_sha256: object,
    expected_org_id: str | None = None,
    expected_request_id: str | None = None,
) -> ApprovalItem:
    """canonical JSON/digest와 Pydantic domain invariant를 모두 다시 확인한다."""
    if not isinstance(item_sha256, str) or len(item_sha256) != 64:
        raise SqliteApprovalSchemaError("Approval item digest 형식이 올바르지 않습니다.")
    if not isinstance(item_json, str) or _digest(item_json) != item_sha256:
        raise SqliteApprovalSchemaError("Approval item JSON digest가 일치하지 않습니다.")
    payload = _strict_json_object(item_json)
    if _canonical_json(payload) != item_json:
        raise SqliteApprovalSchemaError("Approval item JSON은 canonical encoding이어야 합니다.")
    try:
        # JSON transport의 ISO timestamp/array representation은 허용하되, 중복 key와
        # 비정규 encoding은 위에서 이미 거부했다.
        item = ApprovalItem.model_validate_json(item_json, strict=True)
    except Exception as error:
        raise SqliteApprovalSchemaError(
            "Approval item domain snapshot이 유효하지 않습니다."
        ) from error
    if expected_org_id is not None and item.org_id != expected_org_id:
        raise SqliteApprovalSchemaError("Approval item의 parent org가 현재 read scope와 다릅니다.")
    if expected_request_id is not None and item.request_id != expected_request_id:
        raise SqliteApprovalSchemaError(
            "Approval item의 parent request가 현재 read scope와 다릅니다."
        )
    return item


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _canonical_tokens(raw_sql: object) -> list[str]:
    if not isinstance(raw_sql, str):
        raise SqliteApprovalSchemaError("SQLite table DDL을 확인할 수 없습니다.")
    # completion component의 catalog 비교와 같은 목표로, 공백/대소문자 차이만 제거한다.
    return " ".join(raw_sql.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    if not _table_exists(connection, "approval_items"):
        raise SqliteApprovalSchemaError("approval_items table이 없습니다.")
    table_sql = connection.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type = 'table' AND name = 'approval_items'"
    ).fetchone()
    index_sql = connection.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type = 'index' AND name = 'ux_approval_items_current_open'"
    ).fetchone()
    if table_sql is None or index_sql is None:
        raise SqliteApprovalSchemaError("approval_items canonical objects가 없습니다.")
    columns = [
        (str(row["name"]), str(row["type"]), bool(row["notnull"]), int(row["pk"]))
        for row in connection.execute('PRAGMA table_xinfo("approval_items")').fetchall()
    ]
    foreign_keys = [
        tuple(row)
        for row in connection.execute('PRAGMA foreign_key_list("approval_items")').fetchall()
    ]
    return {
        "component_id": SQLITE_APPROVAL_COMPONENT_ID,
        "component_schema_version": SQLITE_APPROVAL_SCHEMA_VERSION,
        "table_ddl": _canonical_tokens(table_sql[0]),
        "current_index_ddl": _canonical_tokens(index_sql[0]),
        "columns": columns,
        "foreign_keys": foreign_keys,
    }


@lru_cache(maxsize=1)
def _expected_manifest_json() -> str:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        # parent shape is intentionally minimal here; runtime validates real parent capability.
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        connection.execute(_APPROVAL_ITEMS_TABLE)
        connection.execute(_APPROVAL_CURRENT_INDEX)
        return _canonical_json(_catalog(connection))
    finally:
        connection.close()


def _manifest_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _table_exists(connection, _MANIFEST_TABLE):
        raise SqliteApprovalSchemaError("공유 schema_component_manifests table이 없습니다.")
    return connection.execute(
        "SELECT component_id, schema_version, manifest_json, manifest_sha256 "
        "FROM schema_component_manifests WHERE component_id COLLATE BINARY = ?",
        (SQLITE_APPROVAL_COMPONENT_ID,),
    ).fetchone()


def _require_foreign_keys(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SqliteApprovalSchemaError(
            "SQLite Approval connection은 PRAGMA foreign_keys=ON이어야 합니다."
        )


def _validate(connection: sqlite3.Connection) -> None:
    _require_foreign_keys(connection)
    try:
        validate_sqlite_completion_connection(connection)
    except SqliteCompletionSchemaError as error:
        raise SqliteApprovalSchemaError(
            "Approval parent Question Completion schema가 유효하지 않습니다."
        ) from error
    row = _manifest_row(connection)
    if row is None:
        raise SqliteApprovalSchemaError(
            "approval_items manifest가 없습니다. 명시 migration이 필요합니다."
        )
    expected_json = _expected_manifest_json()
    if (
        type(row["schema_version"]) is not int
        or row["schema_version"] != SQLITE_APPROVAL_SCHEMA_VERSION
    ):
        raise SqliteApprovalSchemaError("지원하지 않는 approval_items manifest version입니다.")
    if row["manifest_json"] != expected_json or row["manifest_sha256"] != _digest(expected_json):
        raise SqliteApprovalSchemaError(
            "저장된 approval_items manifest가 canonical 기대값과 다릅니다."
        )
    if _canonical_json(_catalog(connection)) != expected_json:
        raise SqliteApprovalSchemaError("SQLite approval_items catalog가 manifest와 다릅니다.")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SqliteApprovalSchemaError("SQLite approval_items foreign_key_check가 실패했습니다.")
    # Catalog/FK capability만으로는 parent org, indexed mirror, generation lineage
    # 같은 row-level invariant를 증명할 수 없다. Runtime open도 read-only로 이를
    # 검증해 손상된 snapshot을 가진 저장소를 operation에 넘기지 않는다.
    _reconcile_item_rows(connection)


def _reconcile_item_rows(connection: sqlite3.Connection) -> None:
    """저장 snapshot의 mirror·parent-org·세대 chain을 읽기 전용으로 대조한다."""
    previous_by_request: dict[str, ApprovalItem] = {}
    rows = connection.execute(
        "SELECT item_id, org_id, request_id, awaiting_revision, attempt, approval_round, "
        "supersedes_item_id, status, item_json, item_sha256, item_schema_version "
        "FROM approval_items ORDER BY request_id COLLATE BINARY, approval_round"
    ).fetchall()
    for row in rows:
        request_id = row["request_id"]
        org_id = row["org_id"]
        if not isinstance(request_id, str) or not isinstance(org_id, str):
            raise SqliteApprovalSchemaError("Approval item parent key type이 올바르지 않습니다.")
        parent = connection.execute(
            "SELECT org_id FROM question_requests WHERE request_id COLLATE BINARY = ?",
            (request_id,),
        ).fetchone()
        if parent is None or parent["org_id"] != org_id:
            raise SqliteApprovalSchemaError(
                "Approval item parent request/org가 현재 row와 다릅니다."
            )
        if type(row["item_schema_version"]) is not int or row["item_schema_version"] != 1:
            raise SqliteApprovalSchemaError(
                "지원하지 않는 Approval item payload schema version입니다."
            )
        item = decode_approval_item(
            item_json=row["item_json"],
            item_sha256=row["item_sha256"],
            expected_org_id=org_id,
            expected_request_id=request_id,
        )
        mirrored = (
            item.item_id == row["item_id"]
            and item.awaiting_revision == row["awaiting_revision"]
            and item.attempt == row["attempt"]
            and item.approval_round == row["approval_round"]
            and item.supersedes_item_id == row["supersedes_item_id"]
            and item.status == row["status"]
        )
        if not mirrored:
            raise SqliteApprovalSchemaError(
                "Approval item indexed mirror와 canonical payload가 다릅니다."
            )
        previous = previous_by_request.get(request_id)
        if previous is None:
            if item.approval_round != 1 or item.supersedes_item_id is not None:
                raise SqliteApprovalSchemaError(
                    "첫 Approval generation의 round/lineage가 올바르지 않습니다."
                )
        else:
            if (
                item.approval_round != previous.approval_round + 1
                or item.supersedes_item_id != previous.item_id
                or previous.status != "superseded"
                or previous.supersession is None
                or previous.supersession.successor_item_id != item.item_id
            ):
                raise SqliteApprovalSchemaError(
                    "Approval supersession chain이 canonical lineage가 아닙니다."
                )
        previous_by_request[request_id] = item


def validate_sqlite_approval_connection(connection: sqlite3.Connection) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection)
    finally:
        connection.row_factory = previous


def _connect_existing(path: str | Path, *, readonly: bool) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise SqliteApprovalSchemaError("SQLite approval runtime은 기존 파일 경로만 엽니다.")
    resolved = Path(raw).expanduser().resolve(strict=False)
    mode = "ro" if readonly else "rw"
    try:
        return sqlite3.connect(f"{resolved.as_uri()}?mode={mode}", uri=True, timeout=5.0)
    except sqlite3.Error as error:
        raise SqliteApprovalSchemaError(
            "SQLite approval DB를 기존 파일로 열 수 없습니다."
        ) from error


def open_sqlite_approval_connection(db_path: str | Path) -> sqlite3.Connection:
    """validate-only runtime open. 성공해도 Approval operation은 활성화하지 않는다."""
    connection = _connect_existing(db_path, readonly=False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _validate(connection)
    except Exception:
        connection.close()
        raise
    return connection


def migrate_sqlite_approval_schema(
    db_path: str | Path, *, fault_injector: MigrationFaultInjector | None = None
) -> None:
    """Capable Question Completion DB에 approval_items v1을 원자적으로 추가한다."""
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _require_foreign_keys(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_sqlite_completion_connection(connection)
        except SqliteCompletionSchemaError as error:
            raise SqliteApprovalSchemaError(
                "Approval migration에는 capable Question Completion parent가 필요합니다."
            ) from error
        row = _manifest_row(connection)
        if row is not None:
            _validate(connection)
            connection.commit()
            return
        if _table_exists(connection, "approval_items"):
            raise SqliteApprovalSchemaError(
                "manifest 없는 approval_items partial schema는 복구하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteApprovalSchemaError("migration 전 foreign_key_check가 실패했습니다.")
        connection.execute(_APPROVAL_ITEMS_TABLE)
        _fault(fault_injector, "after_approval_items")
        connection.execute(_APPROVAL_CURRENT_INDEX)
        _fault(fault_injector, "after_current_index")
        expected = _expected_manifest_json()
        if _canonical_json(_catalog(connection)) != expected:
            raise SqliteApprovalSchemaError(
                "migration 결과 approval_items catalog가 canonical schema와 다릅니다."
            )
        _fault(fault_injector, "before_manifest_insert")
        connection.execute(
            "INSERT INTO schema_component_manifests "
            "(component_id, schema_version, manifest_json, manifest_sha256) VALUES (?, ?, ?, ?)",
            (
                SQLITE_APPROVAL_COMPONENT_ID,
                SQLITE_APPROVAL_SCHEMA_VERSION,
                expected,
                _digest(expected),
            ),
        )
        _fault(fault_injector, "after_manifest_insert")
        _validate(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


@dataclass(frozen=True)
class ApprovalSchemaReconciliationReport:
    """읽기 전용 schema 상태 보고. 복구나 backfill을 수행하지 않는다."""

    capable: bool
    detail: str
    approval_manifest_present: bool


def reconcile_sqlite_approval_schema(db_path: str | Path) -> ApprovalSchemaReconciliationReport:
    """파일을 read-only로 열어 migration 필요 여부만 보고한다."""
    present = False
    try:
        connection = _connect_existing(db_path, readonly=True)
    except SqliteApprovalSchemaError as error:
        return ApprovalSchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        present = (
            _table_exists(connection, _MANIFEST_TABLE) and _manifest_row(connection) is not None
        )
        _validate(connection)
        _reconcile_item_rows(connection)
        return ApprovalSchemaReconciliationReport(True, "capable_v1", present)
    except Exception as error:
        return ApprovalSchemaReconciliationReport(False, str(error), present)
    finally:
        connection.close()
