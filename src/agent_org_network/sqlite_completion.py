"""ADR 0044 SQLite completion component schema capability.

Migration은 명시적으로만 실행한다. Runtime open은 manifest와 실제 SQLite catalog를
재계산해 검증할 뿐 DDL을 만들거나 보정하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Final

SQLITE_COMPLETION_COMPONENT_ID: Final = "question_completion"
SQLITE_COMPLETION_SCHEMA_VERSION: Final = 1

SQLITE_COMPLETION_MIGRATION_FAULT_POINTS: Final = (
    "after_question_requests",
    "after_answer_records_v1",
    "after_answer_records_v2",
    "after_manifest_table",
    "after_terminal_answer_audits",
    "after_request_session_turns",
    "after_question_delivery_outbox",
    "after_question_completion_receipts",
    "before_manifest_insert",
    "after_manifest_insert",
)

type MigrationFaultInjector = Callable[[str], None]


class SqliteCompletionSchemaError(RuntimeError):
    """SQLite completion component가 ADR 0044 capability와 정확히 맞지 않음."""


_QUESTION_REQUEST_TABLE = """
CREATE TABLE question_requests (
    request_id            TEXT PRIMARY KEY NOT NULL,
    org_id                TEXT NOT NULL,
    requester_id          TEXT NOT NULL,
    session_id            TEXT,
    question              TEXT NOT NULL,
    context_snapshot      TEXT,
    intent                TEXT,
    initial_disposition   TEXT,
    state_kind            TEXT NOT NULL,
    state_json            TEXT NOT NULL,
    state_schema_version  INTEGER NOT NULL DEFAULT 1,
    revision              INTEGER NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
)
"""
_QUESTION_REQUEST_INDEXES = (
    "CREATE INDEX idx_question_requests_state_created_id "
    "ON question_requests(state_kind, created_at, request_id)",
    "CREATE INDEX idx_question_requests_org_created_id "
    "ON question_requests(org_id, created_at, request_id)",
)

_ANSWER_RECORD_TABLE_V1 = """
CREATE TABLE answer_records (
    record_id               TEXT PRIMARY KEY,
    question                TEXT NOT NULL,
    answer_text             TEXT NOT NULL,
    answered_by             TEXT NOT NULL,
    agent_id                 TEXT NOT NULL,
    mode                     TEXT NOT NULL,
    session_id               TEXT,
    answered_at              TEXT NOT NULL,
    needs_correction_review  INTEGER NOT NULL DEFAULT 0,
    request_id               TEXT
)
"""
_ANSWER_RECORD_AGENT_INDEX = "CREATE INDEX idx_answer_records_agent ON answer_records(agent_id)"
_ANSWER_RECORD_V2_DDL = (
    "ALTER TABLE answer_records ADD COLUMN sources_json TEXT",
    "ALTER TABLE answer_records ADD COLUMN snapshot_sha TEXT",
    "CREATE UNIQUE INDEX ux_answer_records_request_id_v2 "
    "ON answer_records(request_id COLLATE BINARY) WHERE request_id IS NOT NULL",
)

_MANIFEST_TABLE = """
CREATE TABLE schema_component_manifests (
    component_id      TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    schema_version    INTEGER NOT NULL,
    manifest_json     TEXT NOT NULL,
    manifest_sha256   TEXT NOT NULL
)
"""

_TERMINAL_ANSWER_AUDITS_TABLE = """
CREATE TABLE terminal_answer_audits (
    request_id            TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    record_id             TEXT NOT NULL UNIQUE COLLATE BINARY,
    org_id                TEXT NOT NULL,
    requester_id          TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    route_json            TEXT NOT NULL,
    responsibility_json   TEXT NOT NULL,
    candidate_mode        TEXT NOT NULL,
    final_mode            TEXT NOT NULL,
    approval_json         TEXT NOT NULL,
    completed_at          TEXT NOT NULL,
    audit_schema_version  INTEGER NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (record_id) REFERENCES answer_records(record_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""

_REQUEST_SESSION_TURNS_TABLE = """
CREATE TABLE request_session_turns (
    request_id    TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    record_id     TEXT NOT NULL UNIQUE COLLATE BINARY,
    session_id    TEXT NOT NULL COLLATE BINARY,
    question      TEXT NOT NULL,
    answer_text   TEXT NOT NULL,
    answered_by   TEXT NOT NULL,
    at            TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (record_id) REFERENCES answer_records(record_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_REQUEST_SESSION_TURNS_INDEX = (
    "CREATE INDEX idx_request_session_turns_session_at "
    "ON request_session_turns(session_id, at, request_id)"
)

_QUESTION_DELIVERY_OUTBOX_TABLE = """
CREATE TABLE question_delivery_outbox (
    request_id   TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    record_id    TEXT NOT NULL UNIQUE COLLATE BINARY,
    kind         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (record_id) REFERENCES answer_records(record_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_QUESTION_DELIVERY_OUTBOX_INDEX = (
    "CREATE INDEX idx_question_delivery_outbox_created "
    "ON question_delivery_outbox(created_at, request_id)"
)

_QUESTION_COMPLETION_RECEIPTS_TABLE = """
CREATE TABLE question_completion_receipts (
    request_id              TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    record_id               TEXT NOT NULL UNIQUE COLLATE BINARY,
    handoff_kind            TEXT NOT NULL,
    handoff_json            TEXT NOT NULL,
    handoff_sha256          TEXT NOT NULL,
    handoff_schema_version  INTEGER NOT NULL,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (record_id) REFERENCES answer_records(record_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""

_OWNED_TABLES: Final = (
    "answer_records",
    "question_completion_receipts",
    "question_delivery_outbox",
    "question_requests",
    "request_session_turns",
    "schema_component_manifests",
    "terminal_answer_audits",
)
_NATIVE_TABLES: Final = (
    "terminal_answer_audits",
    "request_session_turns",
    "question_delivery_outbox",
    "question_completion_receipts",
)


def _fault(injector: MigrationFaultInjector | None, point: str) -> None:
    if injector is not None:
        injector(point)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _type_affinity(declared_type: str) -> str:
    normalized = declared_type.upper()
    if "INT" in normalized:
        return "INTEGER"
    if any(token in normalized for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not normalized or "BLOB" in normalized:
        return "BLOB"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _normalize_default(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    return value.casefold()


def _normalize_partial_predicate(raw_sql: object) -> str | None:
    if not isinstance(raw_sql, str):
        return None
    match = re.search(r"\bWHERE\b(.*)\Z", raw_sql, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    return "|".join(_canonical_sql_tokens(match.group(1)))


def _canonical_sql_tokens(raw_sql: object) -> list[str]:
    """공백·주석·식별자 quote 차이를 제거한 SQLite DDL token sequence."""
    if not isinstance(raw_sql, str):
        raise SqliteCompletionSchemaError("SQLite table DDL을 확인할 수 없습니다.")
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(raw_sql):
        char = raw_sql[index]
        if char.isspace():
            index += 1
            continue
        if raw_sql.startswith("--", index):
            newline = raw_sql.find("\n", index + 2)
            index = len(raw_sql) if newline < 0 else newline + 1
            continue
        if raw_sql.startswith("/*", index):
            end = raw_sql.find("*/", index + 2)
            if end < 0:
                raise SqliteCompletionSchemaError("SQLite DDL 주석이 닫히지 않았습니다.")
            index = end + 2
            continue
        if char == "'":
            index += 1
            value: list[str] = []
            while index < len(raw_sql):
                if raw_sql[index] == "'":
                    if index + 1 < len(raw_sql) and raw_sql[index + 1] == "'":
                        value.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                value.append(raw_sql[index])
                index += 1
            else:
                raise SqliteCompletionSchemaError("SQLite DDL 문자열이 닫히지 않았습니다.")
            tokens.append(("string", "".join(value)))
            continue
        if char in {'"', "`", "["}:
            closing = "]" if char == "[" else char
            index += 1
            value = []
            while index < len(raw_sql):
                if raw_sql[index] == closing:
                    if index + 1 < len(raw_sql) and raw_sql[index + 1] == closing:
                        value.append(closing)
                        index += 2
                        continue
                    index += 1
                    break
                value.append(raw_sql[index])
                index += 1
            else:
                raise SqliteCompletionSchemaError("SQLite DDL 식별자가 닫히지 않았습니다.")
            tokens.append(("identifier", "".join(value).casefold()))
            continue
        if char.isalpha() or char == "_" or ord(char) >= 128:
            end = index + 1
            while end < len(raw_sql):
                candidate = raw_sql[end]
                if not (candidate.isalnum() or candidate in {"_", "$"} or ord(candidate) >= 128):
                    break
                end += 1
            tokens.append(("identifier", raw_sql[index:end].casefold()))
            index = end
            continue
        tokens.append(("symbol", char))
        index += 1

    # SQLite의 CREATE ... IF NOT EXISTS는 객체 의미가 아니라 실행 정책이다.
    if len(tokens) >= 5 and tokens[:5] == [
        ("identifier", "create"),
        ("identifier", "table"),
        ("identifier", "if"),
        ("identifier", "not"),
        ("identifier", "exists"),
    ]:
        del tokens[2:5]
    while tokens and tokens[-1] == ("symbol", ";"):
        tokens.pop()
    return [f"{kind}:{value}" for kind, value in tokens]


def _index_catalog(connection: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    indexes: list[dict[str, object]] = []
    rows = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
    for row in sorted(rows, key=lambda value: str(value["name"])):
        name = str(row["name"])
        escaped = name.replace('"', '""')
        keys = [
            {
                "column": value["name"],
                "collation": str(value["coll"]),
                "descending": bool(value["desc"]),
            }
            for value in connection.execute(f'PRAGMA index_xinfo("{escaped}")').fetchall()
            if bool(value["key"])
        ]
        schema_row = connection.execute(
            "SELECT sql FROM main.sqlite_schema WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        raw_sql = None if schema_row is None else schema_row["sql"]
        indexes.append(
            {
                "name": name,
                "origin": str(row["origin"]),
                "partial": bool(row["partial"]),
                "predicate": _normalize_partial_predicate(raw_sql),
                "unique": bool(row["unique"]),
                "keys": keys,
            }
        )
    return indexes


def _table_catalog(connection: sqlite3.Connection, table: str) -> dict[str, object]:
    escaped = table.replace('"', '""')
    columns = [
        {
            "name": str(row["name"]),
            "affinity": _type_affinity(str(row["type"])),
            "nullable": not bool(row["notnull"]),
            "pk_order": int(row["pk"]),
            "default": _normalize_default(row["dflt_value"]),
            "hidden": int(row["hidden"]),
        }
        for row in connection.execute(f'PRAGMA table_xinfo("{escaped}")').fetchall()
    ]
    foreign_keys = [
        {
            "id": int(row["id"]),
            "seq": int(row["seq"]),
            "table": str(row["table"]),
            "from": str(row["from"]),
            "to": str(row["to"]),
            "on_update": str(row["on_update"]).upper(),
            "on_delete": str(row["on_delete"]).upper(),
            "match": str(row["match"]).upper(),
        }
        for row in connection.execute(f'PRAGMA foreign_key_list("{escaped}")').fetchall()
    ]
    schema_row = connection.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    raw_table_sql = None if schema_row is None else schema_row["sql"]
    return {
        "columns": columns,
        "ddl_tokens": _canonical_sql_tokens(raw_table_sql),
        "foreign_keys": sorted(
            foreign_keys,
            key=lambda value: (int(value["id"]), int(value["seq"])),
        ),
        "indexes": _index_catalog(connection, table),
    }


def _persistent_auxiliary_catalog(
    connection: sqlite3.Connection,
) -> tuple[list[str], list[str]]:
    rows = connection.execute(
        "SELECT type, name FROM main.sqlite_schema "
        "WHERE type IN ('trigger', 'view') ORDER BY type, name"
    ).fetchall()
    triggers = [str(row["name"]) for row in rows if row["type"] == "trigger"]
    views = [str(row["name"]) for row in rows if row["type"] == "view"]
    return triggers, views


def _catalog_manifest(connection: sqlite3.Connection) -> dict[str, object]:
    missing = [table for table in _OWNED_TABLES if not _table_exists(connection, table)]
    if missing:
        raise SqliteCompletionSchemaError(
            "question_completion schema catalog table이 누락됐습니다: " + ", ".join(missing)
        )
    triggers, views = _persistent_auxiliary_catalog(connection)
    tables = {table: _table_catalog(connection, table) for table in _OWNED_TABLES}
    owned_indexes = sorted(
        str(row["name"])
        for table in _OWNED_TABLES
        for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall()
    )
    return {
        "component_id": SQLITE_COMPLETION_COMPONENT_ID,
        "component_schema_version": SQLITE_COMPLETION_SCHEMA_VERSION,
        "owned_tables": list(_OWNED_TABLES),
        "owned_indexes": owned_indexes,
        "tables": tables,
        "persistent_triggers": triggers,
        "persistent_views": views,
        "state_schemas": {
            "question_requests": 1,
            "answer_records": 2,
            "handoff_json": 1,
        },
    }


def _canonical_manifest_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _manifest_digest(manifest_json: str) -> str:
    return hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()


def _create_question_requests(connection: sqlite3.Connection) -> None:
    connection.execute(_QUESTION_REQUEST_TABLE)
    for statement in _QUESTION_REQUEST_INDEXES:
        connection.execute(statement)


def _create_answer_records_v1(connection: sqlite3.Connection) -> None:
    connection.execute(_ANSWER_RECORD_TABLE_V1)
    connection.execute(_ANSWER_RECORD_AGENT_INDEX)


def _upgrade_answer_records_v2(connection: sqlite3.Connection) -> None:
    for statement in _ANSWER_RECORD_V2_DDL:
        connection.execute(statement)


def _create_manifest_table(connection: sqlite3.Connection) -> None:
    connection.execute(_MANIFEST_TABLE)


def _create_native_tables(connection: sqlite3.Connection) -> None:
    connection.execute(_TERMINAL_ANSWER_AUDITS_TABLE)
    connection.execute(_REQUEST_SESSION_TURNS_TABLE)
    connection.execute(_REQUEST_SESSION_TURNS_INDEX)
    connection.execute(_QUESTION_DELIVERY_OUTBOX_TABLE)
    connection.execute(_QUESTION_DELIVERY_OUTBOX_INDEX)
    connection.execute(_QUESTION_COMPLETION_RECEIPTS_TABLE)


@lru_cache(maxsize=1)
def _expected_manifest_json() -> str:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _create_question_requests(connection)
        _create_answer_records_v1(connection)
        _upgrade_answer_records_v2(connection)
        _create_manifest_table(connection)
        _create_native_tables(connection)
        return _canonical_manifest_json(_catalog_manifest(connection))
    finally:
        connection.close()


@lru_cache(maxsize=1)
def _expected_legacy_catalogs() -> dict[str, dict[str, object]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        _create_question_requests(connection)
        _create_answer_records_v1(connection)
        return {
            "question_requests": _table_catalog(connection, "question_requests"),
            "answer_records": _table_catalog(connection, "answer_records"),
        }
    finally:
        connection.close()


def _expected_manifest_table_catalog() -> dict[str, object]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        _create_manifest_table(connection)
        return _table_catalog(connection, "schema_component_manifests")
    finally:
        connection.close()


def _require_foreign_keys(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA foreign_keys").fetchone()
    if row is None or row[0] != 1:
        raise SqliteCompletionSchemaError(
            "SQLite completion connection은 PRAGMA foreign_keys=ON이어야 합니다."
        )


def _manifest_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _table_exists(connection, "schema_component_manifests"):
        return None
    actual = _table_catalog(connection, "schema_component_manifests")
    if actual != _expected_manifest_table_catalog():
        raise SqliteCompletionSchemaError(
            "schema_component_manifests schema가 canonical catalog와 다릅니다."
        )
    return connection.execute(
        "SELECT component_id, schema_version, manifest_json, manifest_sha256 "
        "FROM schema_component_manifests WHERE component_id COLLATE BINARY = ?",
        (SQLITE_COMPLETION_COMPONENT_ID,),
    ).fetchone()


def has_sqlite_completion_manifest(connection: sqlite3.Connection) -> bool:
    """공유 manifest table을 검증하고 question_completion marker 존재를 확인한다."""
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        return _manifest_row(connection) is not None
    finally:
        connection.row_factory = previous_factory


def _validate_capable_connection(connection: sqlite3.Connection) -> None:
    _require_foreign_keys(connection)
    row = _manifest_row(connection)
    if row is None:
        raise SqliteCompletionSchemaError(
            "question_completion manifest가 없습니다. 명시 schema migration이 필요합니다."
        )
    if type(row["schema_version"]) is not int or row["schema_version"] != 1:
        raise SqliteCompletionSchemaError(
            "지원하지 않는 question_completion manifest version입니다."
        )

    expected_json = _expected_manifest_json()
    expected_digest = _manifest_digest(expected_json)
    if row["manifest_json"] != expected_json:
        raise SqliteCompletionSchemaError(
            "저장된 question_completion manifest JSON이 내장 기대값과 다릅니다."
        )
    if row["manifest_sha256"] != expected_digest:
        raise SqliteCompletionSchemaError(
            "question_completion manifest digest가 canonical JSON과 다릅니다."
        )

    actual_json = _canonical_manifest_json(_catalog_manifest(connection))
    if actual_json != expected_json:
        raise SqliteCompletionSchemaError(
            "SQLite question_completion catalog schema가 manifest와 다릅니다."
        )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise SqliteCompletionSchemaError(
            "SQLite completion foreign_key_check가 실패했습니다: "
            f"{[tuple(row) for row in violations]!r}"
        )


def validate_sqlite_completion_connection(connection: sqlite3.Connection) -> None:
    """이미 열린 connection을 DDL 없이 Capable v1인지 검증한다."""
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate_capable_connection(connection)
    finally:
        connection.row_factory = previous_factory


def validate_sqlite_completion_schema(db_path: str | Path) -> None:
    """DB 파일을 validate-only로 열어 component capability를 확인한다."""
    connection = _connect_existing_database(db_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _validate_capable_connection(connection)
    finally:
        connection.close()


def open_sqlite_completion_connection(
    db_path: str | Path,
    *,
    timeout: float = 5.0,
) -> sqlite3.Connection:
    """향후 SQLite Completion UoW가 사용할 validate-only runtime open seam."""
    connection = _connect_existing_database(
        db_path,
        timeout=timeout,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _validate_capable_connection(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _connect_existing_database(
    db_path: str | Path,
    *,
    timeout: float,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    raw_path = str(db_path)
    if raw_path in {"", ":memory:"}:
        raise SqliteCompletionSchemaError(
            "SQLite completion runtime은 기존 파일 경로만 열 수 있습니다."
        )
    resolved = Path(raw_path).expanduser().resolve(strict=False)
    uri = f"{resolved.as_uri()}?mode=rw"
    try:
        return sqlite3.connect(
            uri,
            uri=True,
            timeout=timeout,
            check_same_thread=check_same_thread,
        )
    except sqlite3.Error as error:
        raise SqliteCompletionSchemaError(
            f"SQLite completion runtime DB를 기존 파일로 열 수 없습니다: {resolved}"
        ) from error


def _validate_legacy_start(connection: sqlite3.Connection) -> None:
    triggers, views = _persistent_auxiliary_catalog(connection)
    if triggers or views:
        raise SqliteCompletionSchemaError(
            "legacy v1 persistent trigger/view allowlist는 empty입니다."
        )
    existing_native = [table for table in _NATIVE_TABLES if _table_exists(connection, table)]
    if existing_native:
        raise SqliteCompletionSchemaError(
            "manifest 없는 partial completion-native schema를 자동 복구하지 않습니다: "
            f"{existing_native!r}"
        )

    expected = _expected_legacy_catalogs()
    for table in ("question_requests", "answer_records"):
        if not _table_exists(connection, table):
            continue
        actual = _table_catalog(connection, table)
        if actual != expected[table]:
            raise SqliteCompletionSchemaError(
                f"{table} schema는 exact legacy v1이 아니며 partial/drift 상태입니다."
            )

    if _table_exists(connection, "question_requests"):
        answered = connection.execute(
            "SELECT COUNT(*) FROM question_requests WHERE state_kind = 'answered'"
        ).fetchone()
        if answered is not None and int(answered[0]) > 0:
            raise SqliteCompletionSchemaError(
                "receipt 없는 AnsweredRequest는 불완전 completion 상태입니다."
            )
    if _table_exists(connection, "answer_records"):
        duplicate = connection.execute(
            "SELECT request_id, COUNT(*) AS count FROM answer_records "
            "WHERE request_id IS NOT NULL GROUP BY request_id HAVING COUNT(*) > 1 "
            "LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise SqliteCompletionSchemaError(
                "answer_records에 non-null request_id 중복이 있어 migration할 수 "
                f"없습니다: {duplicate['request_id']!r} ({duplicate['count']})"
            )


def migrate_sqlite_completion_schema(
    db_path: str | Path,
    *,
    fault_injector: MigrationFaultInjector | None = None,
) -> None:
    """Fresh/exact legacy v1을 ADR 0044 Capable v1로 원자 migration한다."""
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _require_foreign_keys(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = _manifest_row(connection)
        if row is not None:
            _validate_capable_connection(connection)
            connection.commit()
            return

        _validate_legacy_start(connection)
        if not _table_exists(connection, "question_requests"):
            _create_question_requests(connection)
        _fault(fault_injector, "after_question_requests")

        if not _table_exists(connection, "answer_records"):
            _create_answer_records_v1(connection)
        _fault(fault_injector, "after_answer_records_v1")

        _upgrade_answer_records_v2(connection)
        _fault(fault_injector, "after_answer_records_v2")

        if not _table_exists(connection, "schema_component_manifests"):
            _create_manifest_table(connection)
        _fault(fault_injector, "after_manifest_table")

        connection.execute(_TERMINAL_ANSWER_AUDITS_TABLE)
        _fault(fault_injector, "after_terminal_answer_audits")
        connection.execute(_REQUEST_SESSION_TURNS_TABLE)
        connection.execute(_REQUEST_SESSION_TURNS_INDEX)
        _fault(fault_injector, "after_request_session_turns")
        connection.execute(_QUESTION_DELIVERY_OUTBOX_TABLE)
        connection.execute(_QUESTION_DELIVERY_OUTBOX_INDEX)
        _fault(fault_injector, "after_question_delivery_outbox")
        connection.execute(_QUESTION_COMPLETION_RECEIPTS_TABLE)
        _fault(fault_injector, "after_question_completion_receipts")

        expected_json = _expected_manifest_json()
        actual_json = _canonical_manifest_json(_catalog_manifest(connection))
        if actual_json != expected_json:
            raise SqliteCompletionSchemaError(
                "migration 결과 SQLite catalog가 canonical completion schema와 다릅니다."
            )
        _fault(fault_injector, "before_manifest_insert")
        connection.execute(
            "INSERT INTO schema_component_manifests "
            "(component_id, schema_version, manifest_json, manifest_sha256) "
            "VALUES (?, ?, ?, ?)",
            (
                SQLITE_COMPLETION_COMPONENT_ID,
                SQLITE_COMPLETION_SCHEMA_VERSION,
                expected_json,
                _manifest_digest(expected_json),
            ),
        )
        _fault(fault_injector, "after_manifest_insert")
        _validate_capable_connection(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
