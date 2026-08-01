"""Fail-closed narrow SQLite contracts for Central Question Intake."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from agent_org_network.central_question_request_sqlite import (
    CentralQuestionRequestSqliteStore,
    CentralQuestionRequestSqliteUnavailable,
    central_question_request_schema_ready,
    migrate_central_question_request_schema,
)
from agent_org_network.question_request import (
    DuplicateQuestionRequestError,
    HandlingAssignment,
    QuestionRequest,
    Received,
)


NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)


def _received(**changes: object) -> QuestionRequest:
    request_id = changes.pop("request_id", "request-1")
    assert type(request_id) is str
    values: dict[str, object] = {
        "request_id": request_id,
        "org_id": "acme",
        "requester_id": "root",
        "session_id": None,
        "question": "question",
        "context_snapshot": None,
        "intent": None,
        "initial_disposition": None,
        "state": Received(
            handling=HandlingAssignment(
                kind="system",
                ref=f"question-intake:{request_id}",
                due_at=NOW + timedelta(minutes=5),
            )
        ),
        "revision": 0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return QuestionRequest.model_validate(values, strict=True)


@pytest.mark.parametrize(
    "mutation",
    (
        "CREATE UNIQUE INDEX unexpected_unique ON question_requests(question)",
        "CREATE TRIGGER unexpected_trigger BEFORE INSERT ON question_requests BEGIN SELECT 1; END",
        "CREATE TABLE replacement (request_id TEXT PRIMARY KEY); DROP TABLE question_requests; ALTER TABLE replacement RENAME TO question_requests",
    ),
)
def test_narrow_catalog_rejects_schema_mutation(tmp_path: Path, mutation: str) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_request_schema(database)
    with sqlite3.connect(database) as connection:
        connection.executescript(mutation)
    assert central_question_request_schema_ready(database) is False
    with pytest.raises(CentralQuestionRequestSqliteUnavailable):
        CentralQuestionRequestSqliteStore(database)


def test_create_maps_only_an_existing_exact_request_id_to_duplicate(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_request_schema(database)
    store = CentralQuestionRequestSqliteStore(database)
    store.create(_received())
    with pytest.raises(DuplicateQuestionRequestError):
        store.create(_received())

    # A different IntegrityError is a dependency failure, never a collision.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER fail_new_request BEFORE INSERT ON question_requests "
            "WHEN NEW.request_id='request-2' BEGIN SELECT RAISE(ABORT, 'no'); END"
        )
    with pytest.raises(CentralQuestionRequestSqliteUnavailable):
        store.create(_received(request_id="request-2"))


@pytest.mark.parametrize(
    "changes",
    (
        {"session_id": "caller-session"},
        {"context_snapshot": "caller-context"},
        {"revision": 1},
        {"updated_at": NOW + timedelta(seconds=1)},
        {
            "state": Received(
                handling=HandlingAssignment(
                    kind="system",
                    ref="wrong-ref",
                    due_at=NOW + timedelta(minutes=5),
                )
            )
        },
    ),
)
def test_create_rejects_any_non_rb3_1a_received_envelope(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_request_schema(database)
    store = CentralQuestionRequestSqliteStore(database)
    with pytest.raises(CentralQuestionRequestSqliteUnavailable):
        store.create(_received(**changes))


def test_read_revalidates_persisted_rb3_1a_received_envelope(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_request_schema(database)
    store = CentralQuestionRequestSqliteStore(database)
    store.create(_received())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE question_requests SET session_id='forged' WHERE request_id='request-1'"
        )
    with pytest.raises(CentralQuestionRequestSqliteUnavailable):
        store.get("request-1")


@pytest.mark.parametrize(
    "column,value",
    (
        ("revision", "1"),
        ("intent", "'forged-intent'"),
        ("initial_disposition", "'routed'"),
    ),
)
def test_read_decodes_persisted_envelope_fields_before_rb3_1a_validation(
    tmp_path: Path, column: str, value: str
) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_request_schema(database)
    store = CentralQuestionRequestSqliteStore(database)
    store.create(_received())
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"UPDATE question_requests SET {column}={value} WHERE request_id='request-1'"
        )
    with pytest.raises(CentralQuestionRequestSqliteUnavailable):
        store.get("request-1")


def test_migration_rejects_noncanonical_existing_catalog_without_writing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)"
        )
        before = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
        )
    with pytest.raises(CentralQuestionRequestSqliteUnavailable):
        migrate_central_question_request_schema(database)
    with sqlite3.connect(database) as connection:
        after = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
        )
    assert after == before
