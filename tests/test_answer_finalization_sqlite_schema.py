"""ADR 0044 SQLite completion schema capability와 원자 migration 계약."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_org_network.answer_record import AnswerRecord
from agent_org_network.sqlite_completion import (
    SQLITE_COMPLETION_MIGRATION_FAULT_POINTS,
    SqliteCompletionSchemaError,
    migrate_sqlite_completion_schema,
    open_sqlite_completion_connection,
    validate_sqlite_completion_schema,
)
from agent_org_network.sqlite_stores import (
    SqliteAnswerRecordStore,
    UnsupportedAnswerRecordEvidenceError,
)

T0 = datetime(2026, 7, 13, 9, 8, 7, 654321, tzinfo=timezone.utc)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _create_legacy_question_requests(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
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
        );
        CREATE INDEX idx_question_requests_state_created_id
            ON question_requests(state_kind, created_at, request_id);
        CREATE INDEX idx_question_requests_org_created_id
            ON question_requests(org_id, created_at, request_id);
        """
    )


def _create_legacy_answer_records(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
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
        );
        CREATE INDEX idx_answer_records_agent ON answer_records(agent_id);
        """
    )


def _catalog_rows(path: Path) -> list[tuple[object, ...]]:
    with _connect(path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
            ).fetchall()
        ]


def _column_names(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row["name"]) for row in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
    ]


def _answer(
    *,
    record_id: str,
    request_id: str | None,
    sources: tuple[str, ...] = (),
    snapshot_sha: str | None = None,
) -> AnswerRecord:
    if request_id is None:
        return AnswerRecord(
            record_id=record_id,
            question="결제 취소는 언제 반영되나요? 💳",
            answer_text="영업일 기준 3일 이내입니다. ✅",
            answered_by="owner-한글",
            agent_id="card-한글",
            mode="full",
            sources=sources,
            snapshot_sha=snapshot_sha,
            session_id="session-한글",
            answered_at=T0,
        )
    return AnswerRecord.for_request(
        request_id=request_id,
        record_id=record_id,
        question="결제 취소는 언제 반영되나요? 💳",
        answer_text="영업일 기준 3일 이내입니다. ✅",
        answered_by="owner-한글",
        agent_id="card-한글",
        mode="full",
        sources=sources,
        snapshot_sha=snapshot_sha,
        session_id="session-한글",
        answered_at=T0,
    )


def test_fresh_db를_한번에_capable_v1로_migrate하고_user_version을_보존한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh.db"
    with _connect(path) as connection:
        connection.execute("PRAGMA user_version = 73")

    migrate_sqlite_completion_schema(path)
    validate_sqlite_completion_schema(path)

    with _connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 73
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        assert {
            "question_requests",
            "answer_records",
            "schema_component_manifests",
            "terminal_answer_audits",
            "request_session_turns",
            "question_delivery_outbox",
            "question_completion_receipts",
        } <= tables
        assert _column_names(connection, "answer_records")[-2:] == [
            "sources_json",
            "snapshot_sha",
        ]
        manifest = connection.execute(
            "SELECT * FROM schema_component_manifests WHERE component_id = 'question_completion'"
        ).fetchone()
        assert manifest is not None
        assert manifest["schema_version"] == 1
        assert (
            hashlib.sha256(manifest["manifest_json"].encode("utf-8")).hexdigest()
            == (manifest["manifest_sha256"])
        )
        payload = json.loads(manifest["manifest_json"])
        assert payload["component_id"] == "question_completion"
        assert payload["component_schema_version"] == 1
        assert payload["state_schemas"] == {
            "answer_records": 2,
            "handoff_json": 1,
            "question_requests": 1,
        }
        assert payload["persistent_triggers"] == []
        assert payload["persistent_views"] == []
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_native_table의_exact_columns_foreign_keys_indexes가_ADR0044와_같다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exact-native.db"
    migrate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        assert _column_names(connection, "terminal_answer_audits") == [
            "request_id",
            "record_id",
            "org_id",
            "requester_id",
            "attempt",
            "route_json",
            "responsibility_json",
            "candidate_mode",
            "final_mode",
            "approval_json",
            "completed_at",
            "audit_schema_version",
        ]
        assert _column_names(connection, "request_session_turns") == [
            "request_id",
            "record_id",
            "session_id",
            "question",
            "answer_text",
            "answered_by",
            "at",
        ]
        assert _column_names(connection, "question_delivery_outbox") == [
            "request_id",
            "record_id",
            "kind",
            "created_at",
        ]
        assert _column_names(connection, "question_completion_receipts") == [
            "request_id",
            "record_id",
            "handoff_kind",
            "handoff_json",
            "handoff_sha256",
            "handoff_schema_version",
            "created_at",
        ]
        for table in (
            "terminal_answer_audits",
            "request_session_turns",
            "question_delivery_outbox",
            "question_completion_receipts",
        ):
            foreign_keys = {
                (
                    str(row["from"]),
                    str(row["table"]),
                    str(row["to"]),
                    str(row["on_update"]),
                    str(row["on_delete"]),
                )
                for row in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            }
            assert foreign_keys == {
                (
                    "request_id",
                    "question_requests",
                    "request_id",
                    "RESTRICT",
                    "RESTRICT",
                ),
                (
                    "record_id",
                    "answer_records",
                    "record_id",
                    "RESTRICT",
                    "RESTRICT",
                ),
            }
        request_index = next(
            row
            for row in connection.execute('PRAGMA index_list("answer_records")').fetchall()
            if row["name"] == "ux_answer_records_request_id_v2"
        )
        assert bool(request_index["unique"])
        assert bool(request_index["partial"])
        index_key = next(
            row
            for row in connection.execute(
                'PRAGMA index_xinfo("ux_answer_records_request_id_v2")'
            ).fetchall()
            if bool(row["key"])
        )
        assert (index_key["name"], index_key["coll"], index_key["desc"]) == (
            "request_id",
            "BINARY",
            0,
        )
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'ux_answer_records_request_id_v2'"
        ).fetchone()[0]
        assert "WHERE request_id IS NOT NULL" in sql


def test_exact_legacy_v1을_in_place로_migrate하고_기존_값을_추정_backfill하지_않는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    with _connect(path) as connection:
        _create_legacy_question_requests(connection)
        _create_legacy_answer_records(connection)
        connection.execute(
            "INSERT INTO answer_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "record-기존",
                "기존 질문",
                "기존 답",
                "owner-a",
                "card-a",
                "full",
                "session-a",
                T0.isoformat(),
                1,
                None,
            ),
        )
        before = tuple(
            connection.execute(
                "SELECT * FROM answer_records WHERE record_id = 'record-기존'"
            ).fetchone()
        )

    migrate_sqlite_completion_schema(path)

    with _connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM answer_records WHERE record_id = 'record-기존'"
        ).fetchone()
        assert row is not None
        assert tuple(row)[: len(before)] == before
        assert row["sources_json"] is None
        assert row["snapshot_sha"] is None
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM answer_records "
                "WHERE sources_json IS NOT NULL OR snapshot_sha IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_migrated_request_aware_legacy_record는_sources_추정없이_계속_조회된다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-request-aware.db"
    with _connect(path) as connection:
        _create_legacy_question_requests(connection)
        _create_legacy_answer_records(connection)
        connection.execute(
            "INSERT INTO answer_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "record-legacy-request",
                "기존 질문",
                "기존 답",
                "owner-a",
                "card-a",
                "full",
                None,
                T0.isoformat(),
                0,
                "request-legacy",
            ),
        )

    migrate_sqlite_completion_schema(path)
    store = SqliteAnswerRecordStore(path)
    try:
        restored = store.get("record-legacy-request")
    finally:
        store.close()

    assert restored is not None
    assert restored.request_id == "request-legacy"
    assert restored.sources == ()
    assert restored.snapshot_sha is None


def test_capable_v1_migration은_idempotent_validate_only다(tmp_path: Path) -> None:
    path = tmp_path / "capable.db"
    migrate_sqlite_completion_schema(path)
    before = _catalog_rows(path)

    migrate_sqlite_completion_schema(path)

    assert _catalog_rows(path) == before


def test_partial_predicate의_공백_주석_identifier_quote는_같은_semantic_catalog다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "normalized-predicate.db"
    migrate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        connection.executescript(
            """
            DROP INDEX ux_answer_records_request_id_v2;
            CREATE UNIQUE INDEX ux_answer_records_request_id_v2
                ON answer_records(request_id COLLATE BINARY)
                WHERE /* formatting-only */ "request_id"   IS   NOT   NULL;
            """
        )

    validate_sqlite_completion_schema(path)


def test_runtime_validate_only는_manifest가_없는_DB에_DDL을_만들지_않는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "not-migrated.db"
    with _connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    before = _catalog_rows(path)

    with pytest.raises(SqliteCompletionSchemaError, match="manifest|migration"):
        validate_sqlite_completion_schema(path)

    assert _catalog_rows(path) == before


def test_runtime_validate_only는_미존재_경로_memory_빈경로를_생성하지_않는다(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "runtime.db"
    with pytest.raises(SqliteCompletionSchemaError, match="기존 파일|runtime"):
        validate_sqlite_completion_schema(missing)
    assert not missing.exists()
    assert not missing.parent.exists()

    with pytest.raises(SqliteCompletionSchemaError, match="기존 파일"):
        open_sqlite_completion_connection(":memory:")
    with pytest.raises(SqliteCompletionSchemaError, match="기존 파일"):
        validate_sqlite_completion_schema("")


def test_runtime_validate_only는_기존_빈파일도_변경하지_않는다(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    path.touch()
    assert path.stat().st_size == 0

    with pytest.raises(SqliteCompletionSchemaError, match="manifest|migration"):
        validate_sqlite_completion_schema(path)

    assert path.stat().st_size == 0


def test_runtime_open은_foreign_keys를_켜고_schema를_바꾸지_않는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    migrate_sqlite_completion_schema(path)
    before = _catalog_rows(path)

    connection = open_sqlite_completion_connection(path)
    try:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()

    assert _catalog_rows(path) == before


def test_nonnull_request_id_중복은_임의_병합없이_전체_rollback한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate.db"
    with _connect(path) as connection:
        _create_legacy_question_requests(connection)
        _create_legacy_answer_records(connection)
        values = (
            "질문",
            "답",
            "owner",
            "card",
            "full",
            None,
            T0.isoformat(),
            0,
            "request-1",
        )
        connection.execute(
            "INSERT INTO answer_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("record-1", *values),
        )
        connection.execute(
            "INSERT INTO answer_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("record-2", *values),
        )
    before = _catalog_rows(path)

    with pytest.raises(SqliteCompletionSchemaError, match="중복|duplicate"):
        migrate_sqlite_completion_schema(path)

    assert _catalog_rows(path) == before
    with _connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM answer_records").fetchone()[0] == 2


@pytest.mark.parametrize(
    "partial_sql",
    [
        "ALTER TABLE answer_records ADD COLUMN sources_json TEXT",
        "CREATE TABLE terminal_answer_audits (request_id TEXT)",
        "CREATE TABLE question_completion_receipts (request_id TEXT)",
    ],
)
def test_manifest_없는_partial_v2나_native_table은_fail_closed한다(
    tmp_path: Path,
    partial_sql: str,
) -> None:
    path = tmp_path / "partial.db"
    with _connect(path) as connection:
        _create_legacy_question_requests(connection)
        _create_legacy_answer_records(connection)
        connection.execute(partial_sql)
    before = _catalog_rows(path)

    with pytest.raises(SqliteCompletionSchemaError, match="부분|partial|manifest"):
        migrate_sqlite_completion_schema(path)

    assert _catalog_rows(path) == before


@pytest.mark.parametrize(
    "extra_column",
    [
        "extra TEXT NOT NULL DEFAULT 'x'",
        "extra TEXT GENERATED ALWAYS AS (question) VIRTUAL",
    ],
)
def test_legacy_unknown_notnull_generated_column은_fail_closed한다(
    tmp_path: Path,
    extra_column: str,
) -> None:
    path = tmp_path / "legacy-extra-column.db"
    with _connect(path) as connection:
        _create_legacy_question_requests(connection)
        _create_legacy_answer_records(connection)
        connection.execute(f"ALTER TABLE answer_records ADD COLUMN {extra_column}")
    before = _catalog_rows(path)

    with pytest.raises(SqliteCompletionSchemaError, match="exact legacy|drift"):
        migrate_sqlite_completion_schema(path)

    assert _catalog_rows(path) == before


@pytest.mark.parametrize(
    "answer_table_sql",
    [
        """
        CREATE TABLE answer_records (
            record_id TEXT PRIMARY KEY, question TEXT NOT NULL,
            answer_text TEXT NOT NULL, answered_by TEXT NOT NULL,
            agent_id TEXT NOT NULL, mode TEXT NOT NULL, session_id TEXT,
            answered_at TEXT NOT NULL,
            needs_correction_review INTEGER NOT NULL DEFAULT 0,
            request_id TEXT, CHECK(length(question) > 0)
        )
        """,
        """
        CREATE TABLE answer_records (
            record_id TEXT PRIMARY KEY, question TEXT NOT NULL COLLATE NOCASE,
            answer_text TEXT NOT NULL, answered_by TEXT NOT NULL,
            agent_id TEXT NOT NULL, mode TEXT NOT NULL, session_id TEXT,
            answered_at TEXT NOT NULL,
            needs_correction_review INTEGER NOT NULL DEFAULT 0,
            request_id TEXT
        )
        """,
        """
        CREATE TABLE answer_records (
            record_id TEXT PRIMARY KEY ON CONFLICT FAIL, question TEXT NOT NULL,
            answer_text TEXT NOT NULL, answered_by TEXT NOT NULL,
            agent_id TEXT NOT NULL, mode TEXT NOT NULL, session_id TEXT,
            answered_at TEXT NOT NULL,
            needs_correction_review INTEGER NOT NULL DEFAULT 0,
            request_id TEXT
        )
        """,
        """
        CREATE TABLE answer_records (
            record_id TEXT PRIMARY KEY, question TEXT NOT NULL,
            answer_text TEXT NOT NULL, answered_by TEXT NOT NULL,
            agent_id TEXT NOT NULL, mode TEXT NOT NULL, session_id TEXT,
            answered_at TEXT NOT NULL,
            needs_correction_review INTEGER NOT NULL DEFAULT 0,
            request_id TEXT
        ) STRICT
        """,
        """
        CREATE TABLE answer_records (
            record_id TEXT PRIMARY KEY, question TEXT NOT NULL,
            answer_text TEXT NOT NULL, answered_by TEXT NOT NULL,
            agent_id TEXT NOT NULL, mode TEXT NOT NULL, session_id TEXT,
            answered_at TEXT NOT NULL,
            needs_correction_review INTEGER NOT NULL DEFAULT 0,
            request_id TEXT
        ) WITHOUT ROWID
        """,
    ],
    ids=["check", "collate", "on-conflict", "strict", "without-rowid"],
)
def test_legacy_DDL_semantic_drift는_같은_PRAGMA_shape여도_fail_closed한다(
    tmp_path: Path,
    answer_table_sql: str,
) -> None:
    path = tmp_path / "legacy-ddl-drift.db"
    with _connect(path) as connection:
        _create_legacy_question_requests(connection)
        connection.execute(answer_table_sql)
        connection.execute("CREATE INDEX idx_answer_records_agent ON answer_records(agent_id)")
    before = _catalog_rows(path)

    with pytest.raises(SqliteCompletionSchemaError, match="exact legacy|drift"):
        migrate_sqlite_completion_schema(path)

    assert _catalog_rows(path) == before


@pytest.mark.parametrize("tamper", ["version", "digest", "json"])
def test_manifest_version_digest_json_변조를_모두_거부한다(
    tmp_path: Path,
    tamper: str,
) -> None:
    path = tmp_path / f"tamper-{tamper}.db"
    migrate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        if tamper == "version":
            connection.execute(
                "UPDATE schema_component_manifests SET schema_version = 2 "
                "WHERE component_id = 'question_completion'"
            )
        elif tamper == "digest":
            connection.execute(
                "UPDATE schema_component_manifests SET manifest_sha256 = ? "
                "WHERE component_id = 'question_completion'",
                ("0" * 64,),
            )
        else:
            connection.execute(
                "UPDATE schema_component_manifests SET manifest_json = '{}' "
                "WHERE component_id = 'question_completion'"
            )

    with pytest.raises(SqliteCompletionSchemaError, match="manifest|version|digest"):
        validate_sqlite_completion_schema(path)
    with pytest.raises(SqliteCompletionSchemaError):
        migrate_sqlite_completion_schema(path)


@pytest.mark.parametrize(
    "drift_sql",
    [
        "ALTER TABLE answer_records ADD COLUMN surprise TEXT",
        "CREATE UNIQUE INDEX ux_surprise ON answer_records(question)",
        "CREATE TRIGGER tr_surprise AFTER INSERT ON answer_records BEGIN SELECT 1; END",
        "CREATE VIEW v_surprise AS SELECT * FROM answer_records",
        "DROP INDEX ux_answer_records_request_id_v2; "
        "CREATE UNIQUE INDEX ux_answer_records_request_id_v2 "
        "ON answer_records(request_id COLLATE NOCASE) WHERE request_id IS NOT NULL",
        "DROP INDEX ux_answer_records_request_id_v2; "
        "CREATE UNIQUE INDEX ux_answer_records_request_id_v2 "
        "ON answer_records(request_id COLLATE BINARY) WHERE request_id IS NULL",
    ],
)
def test_capable_catalog_column_unique_trigger_view_index_drift를_거부한다(
    tmp_path: Path,
    drift_sql: str,
) -> None:
    path = tmp_path / "drift.db"
    migrate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        connection.executescript(drift_sql)

    with pytest.raises(SqliteCompletionSchemaError, match="catalog|schema|manifest"):
        validate_sqlite_completion_schema(path)


def test_capable_question_requests_STRICT_DDL_drift를_거부한다(tmp_path: Path) -> None:
    path = tmp_path / "capable-strict.db"
    migrate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP TABLE question_requests;
            CREATE TABLE question_requests (
                request_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL,
                requester_id TEXT NOT NULL, session_id TEXT, question TEXT NOT NULL,
                context_snapshot TEXT, intent TEXT, initial_disposition TEXT,
                state_kind TEXT NOT NULL, state_json TEXT NOT NULL,
                state_schema_version INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT;
            CREATE INDEX idx_question_requests_state_created_id
                ON question_requests(state_kind, created_at, request_id);
            CREATE INDEX idx_question_requests_org_created_id
                ON question_requests(org_id, created_at, request_id);
            """
        )

    with pytest.raises(SqliteCompletionSchemaError, match="catalog|schema|manifest"):
        validate_sqlite_completion_schema(path)


def test_capable_answer_records_CHECK_DDL_drift를_거부한다(tmp_path: Path) -> None:
    path = tmp_path / "capable-check.db"
    migrate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP TABLE answer_records;
            CREATE TABLE answer_records (
                record_id TEXT PRIMARY KEY, question TEXT NOT NULL,
                answer_text TEXT NOT NULL, answered_by TEXT NOT NULL,
                agent_id TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode = 'full'),
                session_id TEXT, answered_at TEXT NOT NULL,
                needs_correction_review INTEGER NOT NULL DEFAULT 0,
                request_id TEXT, sources_json TEXT, snapshot_sha TEXT
            );
            CREATE INDEX idx_answer_records_agent ON answer_records(agent_id);
            CREATE UNIQUE INDEX ux_answer_records_request_id_v2
                ON answer_records(request_id COLLATE BINARY)
                WHERE request_id IS NOT NULL;
            """
        )

    with pytest.raises(SqliteCompletionSchemaError, match="catalog|schema|manifest"):
        validate_sqlite_completion_schema(path)


def test_capable_native_FK_DEFERRABLE_DDL_drift를_거부한다(tmp_path: Path) -> None:
    path = tmp_path / "capable-deferrable.db"
    migrate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP TABLE terminal_answer_audits;
            CREATE TABLE terminal_answer_audits (
                request_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
                record_id TEXT NOT NULL UNIQUE COLLATE BINARY,
                org_id TEXT NOT NULL, requester_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, route_json TEXT NOT NULL,
                responsibility_json TEXT NOT NULL, candidate_mode TEXT NOT NULL,
                final_mode TEXT NOT NULL, approval_json TEXT NOT NULL,
                completed_at TEXT NOT NULL, audit_schema_version INTEGER NOT NULL,
                FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY (record_id) REFERENCES answer_records(record_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            );
            """
        )

    with pytest.raises(SqliteCompletionSchemaError, match="catalog|schema|manifest"):
        validate_sqlite_completion_schema(path)


def test_foreign_key_shape와_foreign_key_check_위반을_거부한다(tmp_path: Path) -> None:
    path = tmp_path / "foreign-key.db"
    migrate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO terminal_answer_audits VALUES "
            "('missing-request', 'missing-record', 'org', 'user', 1, '{}', '{}', "
            "'full', 'full', '{}', ?, 1)",
            (T0.isoformat(),),
        )

    with pytest.raises(SqliteCompletionSchemaError, match="foreign key|foreign_key"):
        validate_sqlite_completion_schema(path)


@pytest.mark.parametrize("fault_point", SQLITE_COMPLETION_MIGRATION_FAULT_POINTS)
def test_각_DDL과_manifest_fault는_fresh_DB를_전체_rollback한다(
    tmp_path: Path,
    fault_point: str,
) -> None:
    path = tmp_path / f"fault-{fault_point}.db"
    with _connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    before = _catalog_rows(path)

    def fail(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(f"injected {point}")

    with pytest.raises(RuntimeError, match="injected"):
        migrate_sqlite_completion_schema(path, fault_injector=fail)

    assert _catalog_rows(path) == before


def test_동시_migrator들은_하나의_capable_manifest로_수렴한다(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"

    def migrate(_: int) -> None:
        migrate_sqlite_completion_schema(path)

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(migrate, range(32)))

    validate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_component_manifests "
                "WHERE component_id = 'question_completion'"
            ).fetchone()[0]
            == 1
        )


def test_capable_v2에서_legacy_AnswerRecordStore는_null_request만_호환한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "answer-store-v2.db"
    migrate_sqlite_completion_schema(path)
    before = _catalog_rows(path)
    store = SqliteAnswerRecordStore(path)
    try:
        assert _catalog_rows(path) == before
        legacy = _answer(record_id="record-한글", request_id=None)
        store.add(legacy)
        assert store.get("record-한글") == legacy

        with pytest.raises(UnsupportedAnswerRecordEvidenceError, match="UoW|request"):
            store.add(_answer(record_id="record-request", request_id="request-1"))
        with pytest.raises(UnsupportedAnswerRecordEvidenceError, match="sources|evidence"):
            store.add(
                _answer(
                    record_id="record-evidence",
                    request_id=None,
                    sources=("정책.md",),
                )
            )
    finally:
        store.close()


def test_capable_v2_AnswerRecordStore는_sources와_snapshot을_유실없이_읽는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "answer-store-read-v2.db"
    migrate_sqlite_completion_schema(path)
    with _connect(path) as connection:
        connection.execute(
            "INSERT INTO answer_records "
            "(record_id, question, answer_text, answered_by, agent_id, mode, "
            "session_id, answered_at, needs_correction_review, request_id, "
            "sources_json, snapshot_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "record-v2",
                "질문 🌐",
                "답변",
                "owner-a",
                "card-a",
                "full",
                "session-a",
                T0.isoformat(),
                0,
                "request-v2",
                '["정책.md","FAQ.md"]',
                "snapshot-한글",
            ),
        )

    store = SqliteAnswerRecordStore(path)
    try:
        restored = store.get("record-v2")
    finally:
        store.close()
    assert restored is not None
    assert restored.sources == ("정책.md", "FAQ.md")
    assert restored.snapshot_sha == "snapshot-한글"
    assert restored.answered_at == T0
