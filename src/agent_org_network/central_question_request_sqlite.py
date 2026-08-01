"""Narrow SQLite adapter for durable RB3.1a Received Question Requests."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

from agent_org_network.question_request import (
    DuplicateQuestionRequestError,
    QuestionRequest,
    Received,
    validate_new_question_request_semantics,
)


_TABLE = """
CREATE TABLE IF NOT EXISTS question_requests (
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
"""
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_question_requests_state_created_id
    ON question_requests(state_kind, created_at, request_id);
CREATE INDEX IF NOT EXISTS idx_question_requests_org_created_id
    ON question_requests(org_id, created_at, request_id);
"""
class CentralQuestionRequestSqliteUnavailable(RuntimeError):
    pass


def migrate_central_question_request_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        catalog = _question_request_catalog(connection)
        if catalog == _CANONICAL_CATALOG:
            return
        if catalog != _EMPTY_CATALOG:
            raise CentralQuestionRequestSqliteUnavailable()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(_TABLE)
            connection.execute(
                "CREATE INDEX idx_question_requests_state_created_id "
                "ON question_requests(state_kind, created_at, request_id)"
            )
            connection.execute(
                "CREATE INDEX idx_question_requests_org_created_id "
                "ON question_requests(org_id, created_at, request_id)"
            )
            _validate(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except Exception as error:
        raise CentralQuestionRequestSqliteUnavailable() from error
    finally:
        connection.close()


def central_question_request_schema_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        _validate(connection)
        return True
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


class CentralQuestionRequestSqliteStore:
    workflow_durability = "durable"

    def __init__(self, path: Path) -> None:
        if not central_question_request_schema_ready(path):
            raise CentralQuestionRequestSqliteUnavailable()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self._connection.close()

    def create(self, request: QuestionRequest) -> QuestionRequest:
        if type(request) is not QuestionRequest:
            raise CentralQuestionRequestSqliteUnavailable()
        try:
            _validate(self._connection)
            _validate_rb3_1a_received(request)
        except Exception as error:
            raise CentralQuestionRequestSqliteUnavailable() from error
        state_json = json.dumps(
            request.state.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO question_requests("
                    "request_id,org_id,requester_id,session_id,question,context_snapshot,"
                    "intent,initial_disposition,state_kind,state_json,state_schema_version,"
                    "revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.request_id,
                        request.org_id,
                        request.requester_id,
                        request.session_id,
                        request.question,
                        request.context_snapshot,
                        request.intent,
                        request.initial_disposition,
                        "received",
                        state_json,
                        1,
                        0,
                        request.created_at.isoformat(),
                        request.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            try:
                existing = self._connection.execute(
                    "SELECT COUNT(*) FROM question_requests "
                    "WHERE request_id COLLATE BINARY=?",
                    (request.request_id,),
                ).fetchone()
                if existing is not None and existing[0] == 1:
                    raise DuplicateQuestionRequestError() from error
            except DuplicateQuestionRequestError:
                raise
            except Exception as lookup_error:
                raise CentralQuestionRequestSqliteUnavailable() from lookup_error
            raise CentralQuestionRequestSqliteUnavailable() from error
        except Exception as error:
            raise CentralQuestionRequestSqliteUnavailable() from error
        return request

    def get(self, request_id: str) -> QuestionRequest | None:
        try:
            _validate(self._connection)
            row = self._connection.execute(
                "SELECT * FROM question_requests WHERE request_id COLLATE BINARY=?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            if row["state_kind"] != "received" or row["state_schema_version"] != 1:
                raise CentralQuestionRequestSqliteUnavailable()
            state = Received.model_validate_json(str(row["state_json"]), strict=True)
            request = QuestionRequest.model_validate(
                {
                    "request_id": str(row["request_id"]),
                    "org_id": str(row["org_id"]),
                    "requester_id": str(row["requester_id"]),
                    "session_id": row["session_id"],
                    "question": str(row["question"]),
                    "context_snapshot": row["context_snapshot"],
                    "intent": row["intent"],
                    "initial_disposition": row["initial_disposition"],
                    "state": state,
                    "revision": row["revision"],
                    "created_at": datetime.fromisoformat(str(row["created_at"])),
                    "updated_at": datetime.fromisoformat(str(row["updated_at"])),
                },
                strict=True,
            )
            _validate_rb3_1a_received(request)
            return request
        except CentralQuestionRequestSqliteUnavailable:
            raise
        except Exception as error:
            raise CentralQuestionRequestSqliteUnavailable() from error

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        _ = request_id, expected_revision, current, updated
        raise CentralQuestionRequestSqliteUnavailable()

    def nonterminal(self) -> list[QuestionRequest]:
        try:
            identifiers = tuple(
                str(row[0])
                for row in self._connection.execute(
                    "SELECT request_id FROM question_requests ORDER BY created_at,request_id"
                )
            )
            return [
                request
                for identifier in identifiers
                if (request := self.get(identifier)) is not None
            ]
        except CentralQuestionRequestSqliteUnavailable:
            raise
        except Exception as error:
            raise CentralQuestionRequestSqliteUnavailable() from error


def _validate(connection: sqlite3.Connection) -> None:
    if _question_request_catalog(connection) != _CANONICAL_CATALOG:
        raise CentralQuestionRequestSqliteUnavailable()


def _validate_rb3_1a_received(request: QuestionRequest) -> None:
    """RB3.1a accepts only the no-caller-metadata Received envelope."""
    validate_new_question_request_semantics(request)
    if request.session_id is not None or request.context_snapshot is not None:
        raise CentralQuestionRequestSqliteUnavailable()


def _normalized_sql(value: object) -> str:
    return " ".join(str(value or "").split())


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _question_request_catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    """The whole narrow-table catalog; unrelated Central components are ignored."""
    objects = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name='question_requests' OR tbl_name='question_requests' "
            "ORDER BY type,name"
        )
    )
    normalized_objects = tuple(
        (row[0], row[1], row[2], _normalized_sql(row[3])) for row in objects
    )
    table_info = tuple(
        tuple(row) for row in connection.execute("PRAGMA table_info(question_requests)")
    )
    foreign_keys = tuple(
        tuple(row) for row in connection.execute("PRAGMA foreign_key_list(question_requests)")
    )
    indexes = tuple(
        tuple(row) for row in connection.execute("PRAGMA index_list(question_requests)")
    )
    index_details = tuple(
        (
            str(row[1]),
            tuple(
                tuple(index_row)
                for index_row in connection.execute(
                    f"PRAGMA index_xinfo({_quoted_identifier(str(row[1]))})"
                )
            ),
        )
        for row in indexes
    )
    return normalized_objects, table_info, foreign_keys, indexes, index_details


def _canonical_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_TABLE)
        connection.executescript(_INDEXES)
        return _question_request_catalog(connection)
    finally:
        connection.close()


_CANONICAL_CATALOG = _canonical_catalog()


def _empty_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        return _question_request_catalog(connection)
    finally:
        connection.close()


_EMPTY_CATALOG = _empty_catalog()


__all__ = [
    "CentralQuestionRequestSqliteStore",
    "CentralQuestionRequestSqliteUnavailable",
    "central_question_request_schema_ready",
    "migrate_central_question_request_schema",
]
