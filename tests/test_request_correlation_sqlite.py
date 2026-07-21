"""P17.2c-1a — 기존 durable 기록의 nullable Request 상관키 마이그레이션."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

import pytest

import agent_org_network.sqlite_stores as sqlite_stores
from agent_org_network.answer_record import AnswerRecord
from agent_org_network.session import SessionTurn
from agent_org_network.sqlite_stores import (
    RequestCorrelationSchemaError,
    SqliteAnswerRecordStore,
    SqliteSessionStore,
    UnsupportedAnswerRecordEvidenceError,
)

T0 = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)


def _answer(*, record_id: str, request_id: str | None = None) -> AnswerRecord:
    if request_id is None:
        return AnswerRecord(
            record_id=record_id,
            question="환불은 언제 되나요?",
            answer_text="영업일 3일 이내입니다.",
            answered_by="owner-a",
            agent_id="card-a",
            mode="full",
            session_id="session-1",
            answered_at=T0,
        )
    return AnswerRecord.for_request(
        request_id=request_id,
        record_id=record_id,
        question="환불은 언제 되나요?",
        answer_text="영업일 3일 이내입니다.",
        answered_by="owner-a",
        agent_id="card-a",
        mode="full",
        session_id="session-1",
        answered_at=T0,
    )


def _turn(*, request_id: str | None = None, at: datetime = T0) -> SessionTurn:
    if request_id is None:
        return SessionTurn(
            question="환불은 언제 되나요?",
            answer_text="영업일 3일 이내입니다.",
            answered_by="owner-a",
            at=at,
        )
    return SessionTurn.for_request(
        request_id=request_id,
        question="환불은 언제 되나요?",
        answer_text="영업일 3일 이내입니다.",
        answered_by="owner-a",
        at=at,
    )


def _column(db: Path, table: str, column: str) -> sqlite3.Row:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    finally:
        connection.close()
    return next(row for row in rows if row["name"] == column)


def test_legacy_sqlite_answer_store_rejects_unpersisted_finalization_evidence(
    tmp_path: Path,
) -> None:
    store = SqliteAnswerRecordStore(tmp_path / "legacy-answer.db")
    record = AnswerRecord.for_request(
        request_id="request-1",
        record_id="record-1",
        question="환불은 언제 되나요?",
        answer_text="영업일 3일 이내입니다.",
        answered_by="owner-a",
        agent_id="card-a",
        mode="full",
        sources=("refund-policy.md",),
        snapshot_sha="sha-1",
        session_id="session-1",
        answered_at=T0,
    )
    try:
        with pytest.raises(
            UnsupportedAnswerRecordEvidenceError,
            match="schema v1",
        ):
            store.add(record)
        assert store.get("record-1") is None
    finally:
        store.close()


class _FalseySources(tuple[str, ...]):
    def __bool__(self) -> bool:
        return False


def test_legacy_sqlite_answer_store_canonicalizes_before_evidence_gate(
    tmp_path: Path,
) -> None:
    store = SqliteAnswerRecordStore(tmp_path / "legacy-answer-model-construct.db")
    record = AnswerRecord.model_construct(
        request_id="request-1",
        record_id="record-1",
        question="환불은 언제 되나요?",
        answer_text="영업일 3일 이내입니다.",
        answered_by="owner-a",
        agent_id="card-a",
        mode="full",
        sources=_FalseySources(("refund-policy.md",)),
        snapshot_sha=None,
        session_id="session-1",
        answered_at=T0,
        needs_correction_review=False,
    )

    try:
        with pytest.raises(
            UnsupportedAnswerRecordEvidenceError,
            match="schema v1",
        ):
            store.add(record)
        assert store.get("record-1") is None
    finally:
        store.close()


def test_새_DB의_answer_record와_session_turn이_request_id를_재시작_왕복한다(
    tmp_path: Path,
) -> None:
    db = tmp_path / "correlation.db"

    sessions = SqliteSessionStore(db, clock=lambda: T0)
    session = sessions.open_or_get("user-1")
    sessions.append_turn(session.session_id, _turn(request_id="request-1"))
    sessions.close()

    answers = SqliteAnswerRecordStore(db)
    answers.add(_answer(record_id="record-1", request_id="request-1"))
    answers.close()

    reopened_sessions = SqliteSessionStore(db, clock=lambda: T1)
    restored_session = reopened_sessions.get(session.session_id)
    assert restored_session is not None
    assert restored_session.transcript[0].request_id == "request-1"
    reopened_sessions.close()

    reopened_answers = SqliteAnswerRecordStore(db)
    restored_answer = reopened_answers.get("record-1")
    assert restored_answer is not None
    assert restored_answer.request_id == "request-1"
    reopened_answers.close()

    for table in ("session_turns", "answer_records"):
        column = _column(db, table, "request_id")
        assert column["type"].upper() == "TEXT"
        assert column["notnull"] == 0


def test_1a는_request_id_unique를_추가하지_않고_INSERT_OR_IGNORE_의미를_유지한다(
    tmp_path: Path,
) -> None:
    db = tmp_path / "legacy-insert-policy.db"

    sessions = SqliteSessionStore(db, clock=lambda: T0)
    session = sessions.open_or_get("user-1")
    sessions.append_turn(session.session_id, _turn(request_id="request-1", at=T0))
    sessions.append_turn(session.session_id, _turn(request_id="request-1", at=T1))
    restored_session = sessions.get(session.session_id)
    assert restored_session is not None
    assert [turn.request_id for turn in restored_session.transcript] == [
        "request-1",
        "request-1",
    ]
    sessions.close()

    answers = SqliteAnswerRecordStore(db)
    answers.add(_answer(record_id="record-1", request_id="request-1"))
    # 기존 정책: 같은 record_id는 INSERT OR IGNORE로 첫 행을 보존한다.
    answers.add(_answer(record_id="record-1", request_id="request-other"))
    # 이번 additive 슬라이스는 request당 유일 제약을 아직 두지 않는다.
    answers.add(_answer(record_id="record-2", request_id="request-1"))

    first = answers.get("record-1")
    assert first is not None
    assert first.request_id == "request-1"
    assert {record.record_id for record in answers.for_agent("card-a")} == {
        "record-1",
        "record-2",
    }
    answers.close()


def _create_legacy_tables(db: Path) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (
                session_id     TEXT PRIMARY KEY,
                user_id        TEXT NOT NULL,
                status         TEXT NOT NULL,
                started_at     TEXT NOT NULL,
                last_active_at TEXT NOT NULL
            );
            CREATE TABLE session_turns (
                session_id  TEXT NOT NULL,
                turn_index  INTEGER NOT NULL,
                question    TEXT NOT NULL,
                answer_text TEXT NOT NULL,
                answered_by TEXT NOT NULL,
                at          TEXT NOT NULL,
                PRIMARY KEY (session_id, turn_index)
            );
            CREATE TABLE answer_records (
                record_id               TEXT PRIMARY KEY,
                question                TEXT NOT NULL,
                answer_text             TEXT NOT NULL,
                answered_by             TEXT NOT NULL,
                agent_id                 TEXT NOT NULL,
                mode                     TEXT NOT NULL,
                session_id               TEXT,
                answered_at              TEXT NOT NULL,
                needs_correction_review  INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            ("session-legacy", "user-1", "active", T0.isoformat(), T0.isoformat()),
        )
        connection.execute(
            "INSERT INTO session_turns VALUES (?, ?, ?, ?, ?, ?)",
            (
                "session-legacy",
                0,
                "같은 질문",
                "같은 답",
                "owner-a",
                T0.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO answer_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "record-legacy",
                "같은 질문",
                "같은 답",
                "owner-a",
                "card-a",
                "full",
                "session-legacy",
                T0.isoformat(),
                0,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_legacy_테이블을_안전하게_확장하고_기존_행은_NULL로_남긴다(
    tmp_path: Path,
) -> None:
    db = tmp_path / "legacy.db"
    _create_legacy_tables(db)

    sessions = SqliteSessionStore(db, clock=lambda: T1)
    restored_session = sessions.get("session-legacy")
    assert restored_session is not None
    assert restored_session.transcript[0].request_id is None
    sessions.append_turn(
        "session-legacy",
        _turn(request_id="request-new", at=T1),
    )
    sessions.close()

    answers = SqliteAnswerRecordStore(db)
    restored_answer = answers.get("record-legacy")
    assert restored_answer is not None
    assert restored_answer.request_id is None
    answers.add(_answer(record_id="record-new", request_id="request-new"))
    answers.close()

    connection = sqlite3.connect(db)
    try:
        legacy_turn_id = connection.execute(
            "SELECT request_id FROM session_turns WHERE turn_index = 0"
        ).fetchone()[0]
        new_turn_id = connection.execute(
            "SELECT request_id FROM session_turns WHERE turn_index = 1"
        ).fetchone()[0]
        legacy_answer_id = connection.execute(
            "SELECT request_id FROM answer_records WHERE record_id = 'record-legacy'"
        ).fetchone()[0]
        new_answer_id = connection.execute(
            "SELECT request_id FROM answer_records WHERE record_id = 'record-new'"
        ).fetchone()[0]
    finally:
        connection.close()

    assert legacy_turn_id is None
    assert legacy_answer_id is None
    assert new_turn_id == "request-new"
    assert new_answer_id == "request-new"


@pytest.mark.parametrize(
    ("table", "request_column"),
    [
        ("session_turns", "request_id INTEGER"),
        ("session_turns", "request_id TEXT NOT NULL"),
        ("answer_records", "request_id INTEGER"),
        ("answer_records", "request_id TEXT NOT NULL"),
        ("answer_records", "request_id VARCHAR(36)"),
    ],
)
def test_기존_request_id_열의_affinity와_nullability가_다르면_fail_closed한다(
    tmp_path: Path,
    table: str,
    request_column: str,
) -> None:
    db = tmp_path / f"bad-{table}-{request_column.replace(' ', '-')}.db"
    connection = sqlite3.connect(db)
    try:
        if table == "session_turns":
            connection.executescript(
                f"""
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL
                );
                CREATE TABLE session_turns (
                    session_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    answered_by TEXT NOT NULL,
                    at TEXT NOT NULL,
                    {request_column},
                    PRIMARY KEY (session_id, turn_index)
                );
                """
            )
        else:
            connection.execute(
                f"""
                CREATE TABLE answer_records (
                    record_id TEXT PRIMARY KEY,
                    question TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    answered_by TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    session_id TEXT,
                    answered_at TEXT NOT NULL,
                    needs_correction_review INTEGER NOT NULL DEFAULT 0,
                    {request_column}
                )
                """
            )
        connection.commit()
    finally:
        connection.close()

    def store_factory() -> object:
        if table == "session_turns":
            return SqliteSessionStore(db, clock=lambda: T0)
        return SqliteAnswerRecordStore(db)

    with pytest.raises(RequestCorrelationSchemaError, match=f"{table}.*request_id"):
        store_factory()


@pytest.mark.parametrize(
    ("table", "request_column", "extra_schema"),
    [
        ("answer_records", "request_id TEXT UNIQUE", ""),
        ("session_turns", "request_id TEXT COLLATE NOCASE", ""),
        (
            "answer_records",
            "request_id TEXT",
            "CREATE INDEX idx_answer_request ON answer_records(request_id);",
        ),
        (
            "session_turns",
            "request_id TEXT CHECK (length(request_id) > 0) COLLATE NOCASE",
            "",
        ),
        (
            "answer_records",
            "request_id TEXT",
            "CREATE UNIQUE INDEX idx_answer_request_expr ON answer_records(lower(request_id));",
        ),
        (
            "answer_records",
            "request_id TEXT",
            "CREATE INDEX idx_answer_request_partial ON answer_records(agent_id) "
            "WHERE request_id IS NOT NULL;",
        ),
    ],
)
def test_1a보다_앞선_request_id_unique_index_collation은_fail_closed한다(
    tmp_path: Path,
    table: str,
    request_column: str,
    extra_schema: str,
) -> None:
    db = tmp_path / f"premature-{table}.db"
    connection = sqlite3.connect(db)
    try:
        if table == "session_turns":
            connection.executescript(
                f"""
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL
                );
                CREATE TABLE session_turns (
                    session_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    answered_by TEXT NOT NULL,
                    at TEXT NOT NULL,
                    {request_column},
                    PRIMARY KEY (session_id, turn_index)
                );
                {extra_schema}
                """
            )
        else:
            connection.executescript(
                f"""
                CREATE TABLE answer_records (
                    record_id TEXT PRIMARY KEY,
                    question TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    answered_by TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    session_id TEXT,
                    answered_at TEXT NOT NULL,
                    needs_correction_review INTEGER NOT NULL DEFAULT 0,
                    {request_column}
                );
                {extra_schema}
                """
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RequestCorrelationSchemaError, match=f"{table}.*request_id"):
        if table == "session_turns":
            SqliteSessionStore(db, clock=lambda: T0)
        else:
            SqliteAnswerRecordStore(db)


def test_request_id라는_index_이름과_주석_문자열은_column_참조로_오탐하지_않는다(
    tmp_path: Path,
) -> None:
    db = tmp_path / "index-name-is-not-column.db"
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            CREATE TABLE answer_records (
                record_id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer_text TEXT NOT NULL,
                answered_by TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                session_id TEXT,
                answered_at TEXT NOT NULL,
                needs_correction_review INTEGER NOT NULL DEFAULT 0,
                request_id TEXT
            );
            CREATE INDEX request_id ON answer_records(agent_id)
                WHERE agent_id <> 'request_id' /* request_id는 문자열/주석 */;
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = SqliteAnswerRecordStore(db)
    store.add(_answer(record_id="record-1", request_id="request-1"))
    restored = store.get("record-1")
    assert restored is not None
    assert restored.request_id == "request-1"
    store.close()


def test_migration된_DB도_persistent_trigger가_추가되면_reopen에서_fail_closed한다(
    tmp_path: Path,
) -> None:
    """v1 allowlist는 empty이며 향후 exact DDL/hash를 검토한 새 schema version만 연다."""
    db = tmp_path / "trigger-name-is-not-column.db"
    answers = SqliteAnswerRecordStore(db)
    answers.close()
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            """
            CREATE TRIGGER request_id AFTER INSERT ON answer_records
            BEGIN
                SELECT 'request_id' /* request_id는 문자열/주석 */;
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        RequestCorrelationSchemaError,
        match="answer_records.*trigger",
    ) as exc_info:
        SqliteAnswerRecordStore(db)
    message = str(exc_info.value)
    assert "name='request_id'" in message
    assert "tbl_name='answer_records'" in message


def test_legacy_target의_UPDATE_OF_request_id_trigger를_ALTER_전에_fail_closed한다(
    tmp_path: Path,
) -> None:
    """SQLite는 존재하지 않는 UPDATE OF 열도 받아 ALTER 뒤 trigger 의미가 달라질 수 있다."""
    db = tmp_path / "legacy-update-of-request-id-trigger.db"
    _create_legacy_tables(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            """
            CREATE TRIGGER trg_answer_request_update
            AFTER UPDATE OF request_id ON answer_records
            BEGIN
                SELECT '본문은 request_id를 읽거나 쓰지 않는다';
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RequestCorrelationSchemaError, match="answer_records.*trigger"):
        SqliteAnswerRecordStore(db)

    connection = sqlite3.connect(db)
    try:
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("answer_records")').fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "request_id" not in columns
    assert "idx_answer_records_agent" not in indexes


@pytest.mark.parametrize(
    "select_sql",
    [
        "SELECT * FROM answer_records",
        "SELECT answer_records.* FROM answer_records",
        "SELECT answer_alias.* FROM answer_records AS answer_alias",
        "SELECT answer_alias.* FROM main.answer_records AS answer_alias",
    ],
    ids=["star", "table-star", "alias-star", "schema-qualified-alias-star"],
)
def test_target_trigger의_wildcard_copy가_ALTER_뒤_첫_write를_깨기_전에_거부된다(
    tmp_path: Path,
    select_sql: str,
) -> None:
    """legacy와 열 수가 같은 shadow copy는 request_id ADD COLUMN 뒤 arity가 달라진다."""
    db = tmp_path / "legacy-target-wildcard-trigger.db"
    _create_legacy_tables(db)
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            f"""
            CREATE TABLE answer_shadow AS
                SELECT * FROM answer_records WHERE 0;
            CREATE TRIGGER trg_answer_shadow_copy
            AFTER INSERT ON answer_records
            BEGIN
                INSERT INTO answer_shadow {select_sql};
            END;
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RequestCorrelationSchemaError, match="answer_records.*trigger"):
        SqliteAnswerRecordStore(db)

    connection = sqlite3.connect(db)
    try:
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("answer_records")').fetchall()
        }
    finally:
        connection.close()
    assert "request_id" not in columns


@pytest.mark.parametrize(
    ("store_kind", "table", "trigger_kind", "default_index"),
    [
        ("session", "session_turns", "wipe", "idx_sessions_active_user"),
        ("session", "session_turns", "unique", "idx_sessions_active_user"),
        ("answer", "answer_records", "wipe", "idx_answer_records_agent"),
        ("answer", "answer_records", "unique", "idx_answer_records_agent"),
    ],
)
def test_persistent_trigger의_request_id_참조를_거부하고_migration을_rollback한다(
    tmp_path: Path,
    store_kind: str,
    table: Literal["session_turns", "answer_records"],
    trigger_kind: str,
    default_index: str,
) -> None:
    db = tmp_path / f"trigger-{store_kind}-{trigger_kind}.db"
    _create_legacy_tables(db)
    row_match = (
        "session_id = NEW.session_id AND turn_index = NEW.turn_index"
        if table == "session_turns"
        else "record_id = NEW.record_id"
    )
    if trigger_kind == "wipe":
        trigger_sql = f"""
            CREATE TRIGGER trg_{store_kind}_wipe_request
            AFTER INSERT ON {table}
            BEGIN
                UPDATE {table}
                SET request_id = NULL
                WHERE {row_match};
            END
        """
    else:
        trigger_sql = f"""
            CREATE TRIGGER trg_{store_kind}_unique_request
            BEFORE INSERT ON {table}
            WHEN NEW.request_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM {table}
                  WHERE request_id = NEW.request_id
              )
            BEGIN
                SELECT RAISE(ABORT, 'duplicate request_id');
            END
        """

    connection = sqlite3.connect(db)
    try:
        connection.execute(trigger_sql)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RequestCorrelationSchemaError, match=f"{table}.*request_id"):
        if store_kind == "session":
            SqliteSessionStore(db, clock=lambda: T0)
        else:
            SqliteAnswerRecordStore(db)

    connection = sqlite3.connect(db)
    try:
        columns = {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        trigger_count = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (table,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert "request_id" not in columns
    assert default_index not in indexes
    assert trigger_count == 1


def test_다른_table_trigger가_answer_request_id를_변조해도_fail_closed한다(
    tmp_path: Path,
) -> None:
    db = tmp_path / "cross-table-trigger.db"
    _create_legacy_tables(db)
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            CREATE TABLE side_effects (event_id TEXT PRIMARY KEY);
            CREATE TRIGGER trg_side_effect_wipes_answer_request
            AFTER INSERT ON side_effects
            BEGIN
                UPDATE answer_records SET request_id = NULL;
            END;
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RequestCorrelationSchemaError, match="answer_records.*request_id"):
        SqliteAnswerRecordStore(db)

    connection = sqlite3.connect(db)
    try:
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("answer_records")').fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "request_id" not in columns
    assert "idx_answer_records_agent" not in indexes


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE answer_records SET question = question",
        'UPDATE OR IGNORE "answer_records" SET question = question',
        (
            "INSERT INTO answer_records "
            "(record_id, question, answer_text, answered_by, agent_id, mode, answered_at, "
            "needs_correction_review) "
            "VALUES ('copy', 'q', 'a', 'owner', 'card', 'full', '2026-07-12', 0)"
        ),
        (
            "REPLACE INTO [answer_records] "
            "(record_id, question, answer_text, answered_by, agent_id, mode, answered_at, "
            "needs_correction_review) "
            "VALUES ('copy', 'q', 'a', 'owner', 'card', 'full', '2026-07-12', 0)"
        ),
        "DELETE FROM answer_records",
        "SELECT * FROM answer_records",
        "SELECT answers.record_id FROM side_effects JOIN answer_records AS answers ON 1 = 1",
        "SELECT answers.* FROM main.answer_records AS answers",
        "SELECT * FROM (answer_records)",
        "SELECT answers.* FROM (main.answer_records AS answers)",
        "SELECT * FROM ((answer_records))",
        ("SELECT answers.record_id FROM side_effects JOIN (answer_records AS answers) ON 1 = 1"),
        "SELECT * FROM (SELECT * FROM answer_records)",
    ],
    ids=[
        "update",
        "update-or-ignore-quoted",
        "insert-into",
        "replace-into-quoted",
        "delete-from",
        "from",
        "join",
        "schema-from",
        "parenthesized-from",
        "parenthesized-schema-from",
        "nested-parenthesized-from",
        "parenthesized-join",
        "subquery-from",
    ],
)
def test_persistent_trigger_body_SQL_shape와_무관하게_v1_empty_allowlist로_거부한다(
    tmp_path: Path,
    statement: str,
) -> None:
    db = tmp_path / "cross-table-reference-trigger.db"
    _create_legacy_tables(db)
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            f"""
            CREATE TABLE side_effects (event_id TEXT PRIMARY KEY);
            CREATE TRIGGER trg_side_effect_reads_or_writes_answers
            AFTER INSERT ON side_effects
            BEGIN
                {statement};
            END;
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RequestCorrelationSchemaError, match="answer_records.*request_id"):
        SqliteAnswerRecordStore(db)


@pytest.mark.parametrize(
    "statement",
    [
        ("SELECT answers.record_id FROM side_effects AS effects, answer_records AS answers"),
        (
            "SELECT answers.record_id FROM side_effects AS effects, "
            '"main"."answer_records" AS answers'
        ),
    ],
    ids=["comma-join", "schema-qualified-comma-join"],
)
def test_persistent_trigger의_쉼표_schema_DDL도_v1_empty_allowlist로_거부한다(
    tmp_path: Path,
    statement: str,
) -> None:
    db = tmp_path / "cross-table-comma-reference-trigger.db"
    _create_legacy_tables(db)
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            f"""
            CREATE TABLE side_effects (event_id TEXT PRIMARY KEY);
            CREATE TRIGGER trg_side_effect_comma_reads_answers
            AFTER INSERT ON side_effects
            BEGIN
                {statement};
            END;
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RequestCorrelationSchemaError, match="answer_records.*request_id"):
        SqliteAnswerRecordStore(db)


def test_겉보기_무해한_persistent_trigger도_v1_empty_allowlist로_거부한다(
    tmp_path: Path,
) -> None:
    """SQL 의미를 추측하지 않고 모든 main persistent auxiliary를 같은 정책으로 거부한다."""
    db = tmp_path / "cross-table-column-names-are-harmless.db"
    _create_legacy_tables(db)
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            CREATE TABLE side_effects (event_id TEXT PRIMARY KEY);
            CREATE TABLE harmless_log (answer_records TEXT, request_id TEXT);
            CREATE TRIGGER trg_side_effect_harmless_log
            AFTER INSERT ON side_effects
            BEGIN
                INSERT INTO harmless_log(answer_records, request_id)
                VALUES ('answer_records', 'request_id');
                SELECT coalesce(answer_records, request_id) FROM harmless_log;
                SELECT * FROM (
                    SELECT answer_records FROM harmless_log
                );
            END;
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        RequestCorrelationSchemaError,
        match="answer_records.*trigger",
    ) as exc_info:
        SqliteAnswerRecordStore(db)
    message = str(exc_info.value)
    assert "name='trg_side_effect_harmless_log'" in message
    assert "tbl_name='side_effects'" in message

    connection = sqlite3.connect(db)
    try:
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("answer_records")').fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "request_id" not in columns
    assert "idx_answer_records_agent" not in indexes


@pytest.mark.parametrize(
    ("case_id", "auxiliary_schema", "name", "tbl_name"),
    [
        (
            "single-quoted",
            """
            CREATE TABLE auxiliary_events (value TEXT);
            CREATE TRIGGER 'single quoted trigger'
            AFTER INSERT ON auxiliary_events BEGIN SELECT 'request_id'; END;
            """,
            "single quoted trigger",
            "auxiliary_events",
        ),
        (
            "quoted-keyword",
            """
            CREATE TABLE "select" (value TEXT);
            CREATE TRIGGER "trigger"
            AFTER INSERT ON "select" BEGIN SELECT 1; END;
            """,
            "trigger",
            "select",
        ),
        (
            "cte",
            """
            CREATE TABLE auxiliary_events (value TEXT);
            CREATE TABLE auxiliary_log (value TEXT);
            CREATE TRIGGER cte_trigger AFTER INSERT ON auxiliary_events
            BEGIN
                INSERT INTO auxiliary_log
                WITH payload(value) AS (SELECT NEW.value)
                SELECT value FROM payload;
            END;
            """,
            "cte_trigger",
            "auxiliary_events",
        ),
        (
            "unrelated",
            """
            CREATE TABLE unrelated_events (value TEXT);
            CREATE TRIGGER unrelated_trigger
            AFTER INSERT ON unrelated_events BEGIN SELECT 1; END;
            """,
            "unrelated_trigger",
            "unrelated_events",
        ),
    ],
)
def test_persistent_trigger_DDL형태와_대상에_무관하게_v1_empty_allowlist로_거부한다(
    tmp_path: Path,
    case_id: str,
    auxiliary_schema: str,
    name: str,
    tbl_name: str,
) -> None:
    db = tmp_path / f"empty-trigger-allowlist-{case_id}.db"
    _create_legacy_tables(db)
    connection = sqlite3.connect(db)
    try:
        connection.executescript(auxiliary_schema)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        RequestCorrelationSchemaError,
        match="answer_records.*trigger",
    ) as exc_info:
        SqliteAnswerRecordStore(db)
    message = str(exc_info.value)
    assert f"name={name!r}" in message
    assert f"tbl_name={tbl_name!r}" in message

    connection = sqlite3.connect(db)
    try:
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("answer_records")').fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "request_id" not in columns
    assert "idx_answer_records_agent" not in indexes


@pytest.mark.parametrize(
    ("view_name", "view_sql"),
    [
        ("answer_projection", "SELECT * FROM answer_records"),
        ("unrelated_projection", "SELECT 1 AS value"),
    ],
    ids=["target-select-star", "unrelated"],
)
def test_persistent_view도_v1_empty_allowlist로_ALTER전에_거부한다(
    tmp_path: Path,
    view_name: str,
    view_sql: str,
) -> None:
    db = tmp_path / f"empty-view-allowlist-{view_name}.db"
    _create_legacy_tables(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(f'CREATE VIEW "{view_name}" AS {view_sql}')
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        RequestCorrelationSchemaError,
        match="answer_records.*view",
    ) as exc_info:
        SqliteAnswerRecordStore(db)
    message = str(exc_info.value)
    assert f"name={view_name!r}" in message
    assert f"tbl_name={view_name!r}" in message

    connection = sqlite3.connect(db)
    try:
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("answer_records")').fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "request_id" not in columns
    assert "idx_answer_records_agent" not in indexes


@pytest.mark.parametrize("store_kind", ["session", "answer"])
def test_다른_connection의_TEMP_trigger와_view는_Store_roundtrip에_영향이_없다(
    tmp_path: Path,
    store_kind: str,
) -> None:
    db = tmp_path / f"temp-trigger-{store_kind}.db"
    _create_legacy_tables(db)
    table = "session_turns" if store_kind == "session" else "answer_records"
    temp_connection = sqlite3.connect(db)
    temp_connection.execute(
        f"""
        CREATE TEMP TRIGGER temp_wipe_{store_kind}_request
        AFTER INSERT ON {table}
        BEGIN
            UPDATE {table} SET request_id = NULL;
        END
        """
    )
    temp_connection.execute(
        f"CREATE TEMP VIEW temp_{store_kind}_projection AS SELECT * FROM {table}"
    )

    try:
        if store_kind == "session":
            sessions = SqliteSessionStore(db, clock=lambda: T1)
            sessions.append_turn(
                "session-legacy",
                _turn(request_id="request-temp-safe", at=T1),
            )
            restored_session = sessions.get("session-legacy")
            assert restored_session is not None
            assert restored_session.transcript[-1].request_id == "request-temp-safe"
            sessions.close()
        else:
            answers = SqliteAnswerRecordStore(db)
            answers.add(_answer(record_id="record-temp-safe", request_id="request-temp-safe"))
            restored_answer = answers.get("record-temp-safe")
            assert restored_answer is not None
            assert restored_answer.request_id == "request-temp-safe"
            answers.close()
    finally:
        temp_connection.close()


@pytest.mark.parametrize(
    ("store_kind", "table", "default_index"),
    [
        ("session", "session_turns", "idx_sessions_active_user"),
        ("answer", "answer_records", "idx_answer_records_agent"),
    ],
)
def test_schema_migration_실패는_ALTER와_기본_index를_모두_rollback한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_kind: str,
    table: Literal["session_turns", "answer_records"],
    default_index: str,
) -> None:
    db = tmp_path / f"rollback-{store_kind}.db"
    _create_legacy_tables(db)
    original = cast(
        Callable[[sqlite3.Connection, Literal["session_turns", "answer_records"]], None],
        getattr(sqlite_stores, "_ensure_nullable_request_id_column"),
    )

    def fail_after_migration(
        connection: sqlite3.Connection,
        target: Literal["session_turns", "answer_records"],
    ) -> None:
        original(connection, target)
        raise RuntimeError("injected schema failure")

    monkeypatch.setattr(
        sqlite_stores,
        "_ensure_nullable_request_id_column",
        fail_after_migration,
    )

    with pytest.raises(RuntimeError, match="injected schema failure"):
        if store_kind == "session":
            SqliteSessionStore(db, clock=lambda: T0)
        else:
            SqliteAnswerRecordStore(db)

    connection = sqlite3.connect(db)
    try:
        columns = {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "request_id" not in columns
    assert default_index not in indexes


@pytest.mark.parametrize("store_kind", ["session", "answer"])
def test_legacy_schema를_여러_connection이_동시에_열어도_한번만_migration한다(
    tmp_path: Path,
    store_kind: str,
) -> None:
    for iteration in range(10):
        db = tmp_path / f"race-{store_kind}-{iteration}.db"
        _create_legacy_tables(db)
        worker_count = 6
        barrier = threading.Barrier(worker_count)

        def open_and_close() -> None:
            barrier.wait(timeout=10)
            if store_kind == "session":
                store = SqliteSessionStore(db, clock=lambda: T0)
            else:
                store = SqliteAnswerRecordStore(db)
            store.close()

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(open_and_close) for _ in range(worker_count)]
            for future in futures:
                future.result(timeout=10)

        table = "session_turns" if store_kind == "session" else "answer_records"
        connection = sqlite3.connect(db)
        try:
            request_columns = [
                row
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                if row[1] == "request_id"
            ]
            legacy_value = connection.execute(
                f'SELECT request_id FROM "{table}" LIMIT 1'
            ).fetchone()[0]
        finally:
            connection.close()
        assert len(request_columns) == 1
        assert legacy_value is None


def test_legacy_내용이_같아도_request_id를_추정_backfill하지_않는다(tmp_path: Path) -> None:
    db = tmp_path / "no-backfill.db"
    _create_legacy_tables(db)

    sessions = SqliteSessionStore(db, clock=lambda: T1)
    sessions.close()
    answers = SqliteAnswerRecordStore(db)
    answers.close()

    connection = sqlite3.connect(db)
    try:
        turn_request_id = connection.execute(
            "SELECT request_id FROM session_turns WHERE question = '같은 질문'"
        ).fetchone()[0]
        answer_request_id = connection.execute(
            "SELECT request_id FROM answer_records WHERE question = '같은 질문'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert turn_request_id is None
    assert answer_request_id is None
