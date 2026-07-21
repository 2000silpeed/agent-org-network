"""SQLite durable 어댑터 — SqliteSessionStore·SqliteTokenStore (T9.8(a), ADR 0024·0026)
+ Phase 12 확장(SqliteAnswerRecordStore·SqliteCorrectionStore·SqliteKnowledgeStore·
SqliteRegistryJournal, ADR 0033·0034) + QuestionRequest(P17.2b, ADR 0042).

InMemory(`session.py`·`token.py`·`answer_record.py`·`knowledge_store.py`)와 동작
동치인 영속 구현. 프로세스 재시작에도 상태가 보존된다(재오픈→조회 durable).
stdlib `sqlite3`만 쓴다(새 의존성 0).

`SubprocessGitGateway`의 tmp repo 통합 테스트 정신 — tmp-file DB로 통합 검증한다.

────────────────────────────────────────────────────────────────────────────
Phase 12 확장 스키마 개요(각 클래스 docstring에 상세):

  answer_records   ← AnswerRecordStore 포트(append-only — add만, UPDATE 없음)
  correction_events← CorrectionStore 포트(append-only — append만, 삽입 순서 보존)
  answer_feedback  ← FeedbackStore 포트(upsert 최신 판정 + 이력 전량 보존 — 두 테이블)
  knowledge_bundles← KnowledgeStore 포트(upsert — put은 최신 version만 수용)
  registry_journal ← 카드 라이브 등록·오너 변경의 durable 저널(append-only).
                     Registry 자체는 SQLite화하지 않는다(YAML 시드 + InMemory
                     라이브 구조 유지) — 대신 mutation(register/transfer)을
                     저널로 남기고 재기동 시 `admin_registry.replay_registry_journal`
                     이 YAML 시드 Registry 위에 저널을 순서대로 재생한다(admission
                     경유 — 무효 카드는 복원되지 않는다 불변식 보존).

────────────────────────────────────────────────────────────────────────────
스키마 확정 (docs/tasks-v0.md T9.8(b) "SQLite 스키마 확정" 결정을 이 구현이 닫는다).

frozen 값 객체에서 도출:

  Session(session_id, user_id, status, transcript, started_at, last_active_at)
  SessionTurn(question, answer_text, answered_by, at)   ← Session 하위 컬렉션
  AdmissionToken(token_id, owner_id, role, token_hash, issued_at, expires_at,
                 revoked, revoked_at)

  ┌── sessions ────────────────────────────────────────────────────────────┐
  │ session_id     TEXT PRIMARY KEY   ← Session.session_id (get 색인)        │
  │ user_id        TEXT NOT NULL      ← Session.user_id                      │
  │ status         TEXT NOT NULL      ← "active" | "ended"                   │
  │ started_at     TEXT NOT NULL      ← ISO8601(tz-aware)                    │
  │ last_active_at TEXT NOT NULL      ← ISO8601 (idle 슬라이딩 비교 원천)     │
  └────────────────────────────────────────────────────────────────────────┘
    INDEX(user_id) WHERE status='active'  ← active_for_user 색인
      (InMemory 의 _active_by_user 에 해당 — user_id 당 active 1개 불변식은
       open_or_get 이 열기 전 기존 active 를 재사용/자동종료해 보장)

  ┌── session_turns ───────────────────────────────────────────────────────┐
  │ session_id  TEXT NOT NULL  ← FK sessions.session_id                     │
  │ turn_index  INTEGER NOT NULL  ← transcript 튜플 순서 보존(0..N-1)        │
  │ question    TEXT NOT NULL  ← SessionTurn.question                       │
  │ answer_text TEXT NOT NULL  ← SessionTurn.answer_text                    │
  │ answered_by TEXT NOT NULL  ← SessionTurn.answered_by                    │
  │ at          TEXT NOT NULL  ← SessionTurn.at ISO8601                     │
  │ PRIMARY KEY(session_id, turn_index)                                    │
  └────────────────────────────────────────────────────────────────────────┘
    transcript 는 tuple[SessionTurn,...] 이므로 별 테이블에 순서(turn_index)로
    적재한다. end/auto_end 시 transcript 를 비우는 의미는 이 테이블의 해당
    session_id 행 전삭제로 재현한다(end 후 맥락 비워짐 불변식·노출 표면 축소).

  ┌── tokens ──────────────────────────────────────────────────────────────┐
  │ token_id    TEXT PRIMARY KEY  ← AdmissionToken.token_id (revoke 색인)   │
  │ owner_id    TEXT NOT NULL     ← owner 귀속                              │
  │ role        TEXT NOT NULL     ← "primary" | "backup"                    │
  │ token_hash  TEXT NOT NULL     ← 해시만 저장(평문 미저장 불변식)          │
  │ issued_at   TEXT NOT NULL     ← ISO8601                                 │
  │ expires_at  TEXT              ← ISO8601 | NULL(만료 없음)               │
  │ revoked     INTEGER NOT NULL  ← 0 | 1 (append-only 표식·삭제 X)          │
  │ revoked_at  TEXT              ← ISO8601 | NULL                          │
  └────────────────────────────────────────────────────────────────────────┘
    UNIQUE INDEX(token_hash)  ← verify 해시 색인(InMemory _by_hash 대응)

도출 근거:
  - 모든 datetime 은 tz-aware ISO8601 TEXT 로 왕복(파싱 시 datetime.fromisoformat).
    값 객체가 timezone.utc aware 를 유지하므로 naive 로 떨어지지 않는다.
  - append-only revoke 는 행 삭제가 아니라 revoked 플래그 UPDATE(Precedent.invalidated
    패턴 — token.py 불변식과 동일).
  - 전이≠기록: durable 보관도 도메인 보관소지 절차(audit) 로그가 아니다.

동시성 (InMemory 에 방금 들어간 RLock 결정과 정합):
  web.py 엔드포인트가 def(비 async)라 스레드풀에서 병렬 실행된다. sqlite3 연결은
  스레드 안전하지 않으므로 `check_same_thread=False` 단일 연결 + `threading.RLock`
  으로 모든 접근을 직렬화한다(InMemory 의 _lock 결정과 동형). open_or_get 의
  idle 체크→auto_end→새 세션 생성, append_turn 의 get→update 사이 TOCTOU 를
  락으로 막는다. 공개 시그니처·반환값·예외는 InMemory 와 불변.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

from pydantic import TypeAdapter, ValidationError

from agent_org_network.answer_record import (
    AnswerFeedback,
    AnswerRecord,
    CorrectionEvent,
    FeedbackVerdict,
)
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc
from agent_org_network.question_request import (
    DuplicateQuestionRequestError,
    InitialDisposition,
    QuestionRequest,
    QuestionRequestState,
    Received,
    validate_compare_and_set_semantics,
    validate_new_question_request_semantics,
)
from agent_org_network.runtime import AnswerMode
from agent_org_network.session import (
    Session,
    SessionTurn,
)
from agent_org_network.sqlite_completion import (
    has_sqlite_completion_manifest,
    validate_sqlite_completion_connection,
)
from agent_org_network.token import AdmissionToken, WorkerRole


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _new_token_id() -> str:
    return uuid.uuid4().hex


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_opt(value: str | None) -> datetime | None:
    return _parse(value) if value is not None else None


class RequestCorrelationSchemaError(RuntimeError):
    """기존 durable 기록 테이블의 request_id 열이 안전한 additive 계약과 다름."""


type _SqlDdlToken = tuple[Literal["identifier", "string", "symbol"], str]


def _sqlite_ddl_tokens(sql: str) -> tuple[_SqlDdlToken, ...]:
    """sqlite_master DDL을 문자열 리터럴·주석과 식별자를 구분해 토큰화한다."""
    tokens: list[_SqlDdlToken] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise RequestCorrelationSchemaError(
                    "request_id table DDL에 닫히지 않은 주석이 있습니다."
                )
            index = end + 2
            continue
        if char == "'":
            index += 1
            value: list[str] = []
            while index < len(sql):
                if sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        value.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                value.append(sql[index])
                index += 1
            else:
                raise RequestCorrelationSchemaError(
                    "request_id table DDL에 닫히지 않은 문자열이 있습니다."
                )
            tokens.append(("string", "".join(value)))
            continue
        if char in {'"', "`", "["}:
            closing = "]" if char == "[" else char
            index += 1
            value = []
            while index < len(sql):
                if sql[index] == closing:
                    if index + 1 < len(sql) and sql[index + 1] == closing:
                        value.append(closing)
                        index += 2
                        continue
                    index += 1
                    break
                value.append(sql[index])
                index += 1
            else:
                raise RequestCorrelationSchemaError(
                    "request_id table DDL에 닫히지 않은 식별자가 있습니다."
                )
            tokens.append(("identifier", "".join(value)))
            continue
        if char.isalpha() or char == "_" or ord(char) >= 128:
            end = index + 1
            while end < len(sql):
                candidate = sql[end]
                if not (candidate.isalnum() or candidate in {"_", "$"} or ord(candidate) >= 128):
                    break
                end += 1
            tokens.append(("identifier", sql[index:end]))
            index = end
            continue
        tokens.append(("symbol", char))
        index += 1
    return tuple(tokens)


def _sqlite_table_items(table_sql: str) -> tuple[tuple[_SqlDdlToken, ...], ...]:
    """CREATE TABLE의 최상위 column/table-constraint 항목을 괄호 깊이로 분리한다."""
    tokens = _sqlite_ddl_tokens(table_sql)
    opening = next(
        (index for index, token in enumerate(tokens) if token == ("symbol", "(")),
        None,
    )
    if opening is None:
        raise RequestCorrelationSchemaError("request_id table DDL의 여는 괄호를 찾을 수 없습니다.")

    items: list[tuple[_SqlDdlToken, ...]] = []
    current: list[_SqlDdlToken] = []
    depth = 0
    for token in tokens[opening + 1 :]:
        if token == ("symbol", "("):
            depth += 1
            current.append(token)
            continue
        if token == ("symbol", ")"):
            if depth == 0:
                if current:
                    items.append(tuple(current))
                return tuple(items)
            depth -= 1
            current.append(token)
            continue
        if token == ("symbol", ",") and depth == 0:
            if current:
                items.append(tuple(current))
                current = []
            continue
        current.append(token)
    raise RequestCorrelationSchemaError("request_id table DDL의 닫는 괄호를 찾을 수 없습니다.")


def _ddl_identifier_values(tokens: tuple[_SqlDdlToken, ...]) -> tuple[str, ...]:
    return tuple(value.casefold() for kind, value in tokens if kind == "identifier")


def _sqlite_index_scope_mentions_request_id(index_sql: str) -> bool:
    """index 이름/대상 table을 제외한 expression·partial predicate만 검사한다."""
    tokens = _sqlite_ddl_tokens(index_sql)
    expression_start = next(
        (index for index, token in enumerate(tokens) if token == ("symbol", "(")),
        None,
    )
    if expression_start is None:
        raise RequestCorrelationSchemaError(
            "request_id index DDL의 expression 괄호를 찾을 수 없습니다."
        )
    return "request_id" in _ddl_identifier_values(tokens[expression_start:])


def _validate_empty_correlation_auxiliary_schema(
    connection: sqlite3.Connection,
    table: Literal["session_turns", "answer_records"],
) -> None:
    """v1의 persistent trigger/view allowlist가 empty인지 fail-closed 확인한다.

    SQLite DDL 텍스트를 손수 해석해 ADD COLUMN 전후 의미 보존을 증명할 수 없다.
    따라서 현재 schema version은 ``main.sqlite_schema``의 persistent trigger와 view를
    하나도 허용하지 않는다. 영구 금지가 아니라, 실제 요구가 생기면 검토된 exact DDL/hash
    allowlist를 가진 새 schema version과 명시 migration으로만 연다.

    TEMP auxiliary는 생성 connection에만 귀속되므로 main schema 조회에서 제외된다.
    """
    rows = connection.execute(
        "SELECT type, name, tbl_name FROM main.sqlite_schema "
        "WHERE type IN ('trigger', 'view') ORDER BY type, name"
    ).fetchall()
    if not rows:
        return
    details = ", ".join(
        f"type={str(row['type'])!r} name={str(row['name'])!r} tbl_name={str(row['tbl_name'])!r}"
        for row in rows
    )
    raise RequestCorrelationSchemaError(
        f"{table} request_id schema v1의 persistent trigger/view allowlist는 empty입니다. "
        "검토된 exact DDL/hash allowlist를 가진 새 schema version migration이 "
        f"필요합니다: {details}"
    )


def _validate_plain_request_id_ddl(table: str, table_sql: str) -> None:
    """request_id를 제약 없는 ``TEXT`` 한 열로만 선언했는지 검증한다."""
    request_definition: tuple[_SqlDdlToken, ...] | None = None
    for item in _sqlite_table_items(table_sql):
        identifiers = _ddl_identifier_values(item)
        if not identifiers:
            continue
        if identifiers[0] == "request_id":
            if request_definition is not None:
                raise RequestCorrelationSchemaError(
                    f"{table}.request_id column 정의가 중복됐습니다."
                )
            request_definition = item
            continue
        if "request_id" in identifiers:
            raise RequestCorrelationSchemaError(
                f"{table}.request_id에는 table-level constraint를 둘 수 없습니다."
            )

    expected: tuple[_SqlDdlToken, ...] = (
        ("identifier", "request_id"),
        ("identifier", "TEXT"),
    )
    if request_definition is None or tuple(
        (kind, value.casefold() if kind == "identifier" else value)
        for kind, value in request_definition
    ) != tuple(
        (kind, value.casefold() if kind == "identifier" else value) for kind, value in expected
    ):
        raise RequestCorrelationSchemaError(
            f"{table}.request_id는 제약 없는 plain nullable TEXT여야 합니다."
        )


def _ensure_nullable_request_id_column(
    connection: sqlite3.Connection,
    table: Literal["session_turns", "answer_records"],
) -> None:
    """legacy 테이블에 nullable TEXT 상관키를 추가하고 기존 정의를 fail-closed 검증한다.

    새 열에는 DEFAULT를 두지 않아 기존 행이 반드시 NULL로 남는다. 이미 열이 있다면
    선언 타입/nullability/default/PK/generated/constraint shape를 확인할 뿐 값을 추정하거나
    고치지 않는다.
    """
    rows = connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
    by_name = {str(row["name"]): row for row in rows}
    if "request_id" not in by_name:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN request_id TEXT')
        rows = connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        by_name = {str(row["name"]): row for row in rows}

    row = by_name["request_id"]
    if str(row["type"]).strip().upper() != "TEXT":
        raise RequestCorrelationSchemaError(
            f"{table}.request_id 선언 타입은 정확히 TEXT여야 합니다."
        )
    if bool(row["notnull"]):
        raise RequestCorrelationSchemaError(f"{table}.request_id는 nullable이어야 합니다.")
    if int(row["pk"]) != 0:
        raise RequestCorrelationSchemaError(
            f"{table}.request_id는 PRIMARY KEY에 참여할 수 없습니다."
        )
    if int(row["hidden"]) != 0:
        raise RequestCorrelationSchemaError(
            f"{table}.request_id는 generated/hidden 열일 수 없습니다."
        )
    if row["dflt_value"] is not None:
        raise RequestCorrelationSchemaError(f"{table}.request_id에는 DEFAULT를 둘 수 없습니다.")

    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    table_sql = None if table_row is None else table_row["sql"]
    if not isinstance(table_sql, str):
        raise RequestCorrelationSchemaError(f"{table}.request_id DDL을 확인할 수 없습니다.")
    _validate_plain_request_id_ddl(table, table_sql)

    # 1a는 plain nullable 보관만 추가한다. 미리 설치된 UNIQUE/일반 index는 후속
    # request당 유일성·조회 의미를 몰래 앞당길 수 있으므로 versioned UoW 전까지 거부한다.
    for index_row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        index_name = str(index_row["name"])
        quoted_index = index_name.replace('"', '""')
        indexed_columns = connection.execute(f'PRAGMA index_info("{quoted_index}")').fetchall()
        index_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        index_sql = None if index_sql_row is None else index_sql_row["sql"]
        direct_request_column = any(
            str(indexed["name"]).casefold() == "request_id" for indexed in indexed_columns
        )
        expression_mentions_request = isinstance(
            index_sql, str
        ) and _sqlite_index_scope_mentions_request_id(index_sql)
        if direct_request_column or expression_mentions_request:
            raise RequestCorrelationSchemaError(
                f"{table}.request_id에는 1a 범위의 index를 둘 수 없습니다: {index_name!r}"
            )

    for foreign_key in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
        if str(foreign_key["from"]) == "request_id":
            raise RequestCorrelationSchemaError(
                f"{table}.request_id에는 1a 범위의 foreign key를 둘 수 없습니다."
            )


def _initialize_correlation_schema(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
    table: Literal["session_turns", "answer_records"],
) -> None:
    """v1 auxiliary gate·schema 생성·legacy ALTER를 한 transaction으로 묶는다.

    persistent trigger/view empty allowlist는 BEGIN IMMEDIATE 바로 뒤, 기본 table/index
    statement와 ALTER보다 먼저 검사한다. 이후 정책 확장은 SQL 추측이 아니라 검토된 exact
    DDL/hash allowlist를 가진 새 schema version으로만 가능하다.
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_empty_correlation_auxiliary_schema(connection, table)
        for statement in statements:
            connection.execute(statement)
        _ensure_nullable_request_id_column(connection, table)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


# ── SqliteQuestionRequestStore — 질문 수명주기 durable aggregate (P17.2b) ──

_QUESTION_REQUEST_STATE_SCHEMA_VERSION = 1
_QUESTION_REQUEST_STATE_ADAPTER: TypeAdapter[QuestionRequestState] = TypeAdapter(
    QuestionRequestState
)

_QUESTION_REQUEST_TABLE_SCHEMA = """
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

_QUESTION_REQUEST_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_question_requests_state_created_id
    ON question_requests(state_kind, created_at, request_id);
CREATE INDEX IF NOT EXISTS idx_question_requests_org_created_id
    ON question_requests(org_id, created_at, request_id);
"""

_QUESTION_REQUEST_EXPECTED_INDEXES: dict[str, tuple[str, ...]] = {
    "idx_question_requests_state_created_id": (
        "state_kind",
        "created_at",
        "request_id",
    ),
    "idx_question_requests_org_created_id": (
        "org_id",
        "created_at",
        "request_id",
    ),
}

_QUESTION_REQUEST_REQUIRED_COLUMNS = frozenset(
    {
        "request_id",
        "org_id",
        "requester_id",
        "session_id",
        "question",
        "context_snapshot",
        "intent",
        "initial_disposition",
        "state_kind",
        "state_json",
        "state_schema_version",
        "revision",
        "created_at",
        "updated_at",
    }
)

_QUESTION_REQUEST_TEXT_COLUMNS = frozenset(
    {
        "request_id",
        "org_id",
        "requester_id",
        "session_id",
        "question",
        "context_snapshot",
        "intent",
        "initial_disposition",
        "state_kind",
        "state_json",
        "created_at",
        "updated_at",
    }
)
_QUESTION_REQUEST_INTEGER_COLUMNS = frozenset({"state_schema_version", "revision"})
_QUESTION_REQUEST_NULLABLE_COLUMNS = frozenset(
    {"session_id", "context_snapshot", "intent", "initial_disposition"}
)


class QuestionRequestSchemaError(RuntimeError):
    """기존 question_requests 테이블이 v1 필수 열 계약과 호환되지 않음."""


class CorruptQuestionRequestError(RuntimeError):
    """SQLite 행을 QuestionRequest로 엄격하고 안전하게 복원할 수 없음."""


class _QuestionRequestJsonIntegrityError(ValueError):
    """표준 JSON 문법만으로는 잡히지 않는 중복 key·비표준 상수."""


def _canonical_question_request_state_json(state: QuestionRequestState) -> str:
    """쓰기 경로의 결정론적 v1 JSON 표현(읽기는 유효한 비정규 JSON도 허용)."""
    return json.dumps(
        state.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _QuestionRequestJsonIntegrityError(
                f"QuestionRequest state JSON에 중복 JSON key가 있습니다: {key!r}"
            )
        result[key] = value
    return result


def _reject_nonstandard_json_constant(value: str) -> object:
    raise _QuestionRequestJsonIntegrityError(
        f"QuestionRequest state JSON에 비표준 JSON 상수가 있습니다: {value}"
    )


def _question_request_values(request: QuestionRequest) -> tuple[object, ...]:
    return (
        request.request_id,
        request.org_id,
        request.requester_id,
        request.session_id,
        request.question,
        request.context_snapshot,
        request.intent,
        request.initial_disposition,
        request.state.kind,
        _canonical_question_request_state_json(request.state),
        _QUESTION_REQUEST_STATE_SCHEMA_VERSION,
        request.revision,
        _iso(request.created_at),
        _iso(request.updated_at),
    )


def _validate_question_request_schema(connection: sqlite3.Connection) -> None:
    """v1 열의 affinity/nullability/default/단일 PK와 호환 추가 열을 검증한다."""
    rows = connection.execute("PRAGMA table_info(question_requests)").fetchall()
    extended_rows = connection.execute("PRAGMA table_xinfo(question_requests)").fetchall()
    by_name = {str(row["name"]): row for row in rows}
    extended_by_name = {str(row["name"]): row for row in extended_rows}
    columns = set(by_name)
    missing = sorted(_QUESTION_REQUEST_REQUIRED_COLUMNS - columns)
    if missing:
        raise QuestionRequestSchemaError(
            "question_requests v1 필수 열이 누락됐습니다: " + ", ".join(missing)
        )
    hidden_required = sorted(
        name
        for name in _QUESTION_REQUEST_REQUIRED_COLUMNS
        if int(extended_by_name[name]["hidden"]) != 0
    )
    if hidden_required:
        raise QuestionRequestSchemaError(
            f"question_requests 필수 열은 generated/hidden일 수 없습니다: {hidden_required!r}"
        )

    foreign_keys = connection.execute("PRAGMA foreign_key_list(question_requests)").fetchall()
    if foreign_keys:
        raise QuestionRequestSchemaError("question_requests v1에는 foreign key를 둘 수 없습니다.")
    triggers = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'question_requests'"
    ).fetchall()
    if triggers:
        trigger_names = sorted(str(row["name"]) for row in triggers)
        raise QuestionRequestSchemaError(
            f"question_requests v1에는 trigger를 둘 수 없습니다: {trigger_names!r}"
        )
    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'question_requests'"
    ).fetchone()
    table_sql = None if table_row is None else table_row["sql"]
    if not isinstance(table_sql, str):
        raise QuestionRequestSchemaError("question_requests table DDL을 확인할 수 없습니다.")
    if re.search(r"\bCHECK\b", table_sql, flags=re.IGNORECASE):
        raise QuestionRequestSchemaError(
            "question_requests v1에는 CHECK constraint를 둘 수 없습니다."
        )

    primary_key = sorted((int(row["pk"]), str(row["name"])) for row in rows if int(row["pk"]) > 0)
    if primary_key != [(1, "request_id")]:
        raise QuestionRequestSchemaError(
            f"question_requests는 request_id만 단일 PRIMARY KEY여야 합니다: {primary_key!r}"
        )

    for name in sorted(_QUESTION_REQUEST_REQUIRED_COLUMNS):
        row = by_name[name]
        actual_affinity = _sqlite_type_affinity(str(row["type"]))
        if name in _QUESTION_REQUEST_TEXT_COLUMNS:
            expected_affinity = "TEXT"
        elif name in _QUESTION_REQUEST_INTEGER_COLUMNS:
            expected_affinity = "INTEGER"
        else:  # pragma: no cover - module constant configuration guard
            raise RuntimeError(f"정의되지 않은 QuestionRequest column affinity: {name}")
        if actual_affinity != expected_affinity:
            raise QuestionRequestSchemaError(
                f"question_requests.{name} affinity는 {expected_affinity}여야 합니다: "
                f"{actual_affinity}"
            )

        # SQLite rowid table의 `TEXT PRIMARY KEY`만으로는 PRAGMA notnull=0이고
        # NULL PK 복수행도 가능하다. v1은 request_id까지 명시 NOT NULL(=1)을 요구한다.
        expected_not_null = name not in _QUESTION_REQUEST_NULLABLE_COLUMNS
        actual_not_null = bool(row["notnull"])
        if actual_not_null != expected_not_null:
            raise QuestionRequestSchemaError(
                f"question_requests.{name} nullability가 v1과 다릅니다."
            )

    if not _sqlite_default_is_one(by_name["state_schema_version"]["dflt_value"]):
        raise QuestionRequestSchemaError(
            "question_requests.state_schema_version에는 DEFAULT 1이 필요합니다."
        )

    for row in extended_rows:
        name = str(row["name"])
        if name in _QUESTION_REQUEST_REQUIRED_COLUMNS:
            continue
        if int(row["pk"]) > 0:
            raise QuestionRequestSchemaError(
                f"추가 열 {name!r}은 PRIMARY KEY에 참여할 수 없습니다."
            )
        if bool(row["notnull"]):
            raise QuestionRequestSchemaError(
                f"v1 writer가 안전하게 생략할 수 없는 NOT NULL 추가 열이 있습니다: {name!r}"
            )


def _sqlite_type_affinity(declared_type: str) -> str:
    """SQLite의 선언형 type→affinity 규칙을 필요한 수준에서 그대로 적용한다."""
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


def _sqlite_default_is_one(raw_default: object) -> bool:
    if not isinstance(raw_default, str):
        return False
    normalized = raw_default.strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    return normalized in {"1", "+1", "01"}


def _sqlite_index_key_shape(
    connection: sqlite3.Connection,
    name: str,
) -> tuple[tuple[str, str, int], ...]:
    quoted_name = name.replace('"', '""')
    return tuple(
        (
            str(row["name"]),
            str(row["coll"]),
            int(row["desc"]),
        )
        for row in connection.execute(f'PRAGMA index_xinfo("{quoted_name}")').fetchall()
        if bool(row["key"])
    )


def _validate_question_request_indexes(connection: sqlite3.Connection) -> None:
    """expected index 이름·열 순서·non-unique·non-partial shape를 검증한다."""
    listed = {
        str(row["name"]): row
        for row in connection.execute("PRAGMA index_list(question_requests)").fetchall()
    }
    for name, expected_columns in _QUESTION_REQUEST_EXPECTED_INDEXES.items():
        row = listed.get(name)
        if row is None:
            raise QuestionRequestSchemaError(
                f"question_requests expected index가 없습니다: {name!r}"
            )
        if bool(row["unique"]) or bool(row["partial"]):
            raise QuestionRequestSchemaError(
                f"question_requests index {name!r}는 non-unique/non-partial이어야 합니다."
            )
        actual_shape = _sqlite_index_key_shape(connection, name)
        expected_shape = tuple((column_name, "BINARY", 0) for column_name in expected_columns)
        if actual_shape != expected_shape:
            raise QuestionRequestSchemaError(
                f"question_requests index {name!r} 열/정렬/collation shape가 다릅니다: "
                f"{actual_shape!r}"
            )

    primary_key_indexes = 0
    for name, row in listed.items():
        if name in _QUESTION_REQUEST_EXPECTED_INDEXES or not bool(row["unique"]):
            continue
        key_shape = _sqlite_index_key_shape(connection, name)
        if str(row["origin"]) == "pk":
            primary_key_indexes += 1
            if key_shape != (("request_id", "BINARY", 0),):
                raise QuestionRequestSchemaError(
                    "question_requests request_id PRIMARY KEY index는 "
                    "BINARY collation과 ASC exact-ID 순서를 써야 합니다: "
                    f"{key_shape!r}"
                )
            continue
        raise QuestionRequestSchemaError(
            f"question_requests v1에 예상 밖 UNIQUE index가 있습니다: {name!r} {key_shape!r}"
        )
    if primary_key_indexes != 1:
        raise QuestionRequestSchemaError(
            "question_requests request_id PRIMARY KEY index는 정확히 하나여야 합니다."
        )


def _require_row_str(row: sqlite3.Row, column: str) -> str:
    value = row[column]
    if not isinstance(value, str):
        raise CorruptQuestionRequestError(f"QuestionRequest row 열 {column!r}은 TEXT여야 합니다.")
    return value


def _require_row_optional_str(row: sqlite3.Row, column: str) -> str | None:
    value = row[column]
    if value is not None and not isinstance(value, str):
        raise CorruptQuestionRequestError(
            f"QuestionRequest row 열 {column!r}은 TEXT 또는 NULL이어야 합니다."
        )
    return value


def _decode_question_request_state(row: sqlite3.Row) -> QuestionRequestState:
    version = row["state_schema_version"]
    if type(version) is not int or version != _QUESTION_REQUEST_STATE_SCHEMA_VERSION:
        raise CorruptQuestionRequestError(
            f"지원하지 않는 QuestionRequest state schema version입니다: {version!r}"
        )

    state_kind = _require_row_str(row, "state_kind")
    raw_json = _require_row_str(row, "state_json")
    try:
        raw_payload: object = json.loads(
            raw_json,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except _QuestionRequestJsonIntegrityError as exc:
        raise CorruptQuestionRequestError(str(exc)) from exc
    except (json.JSONDecodeError, TypeError) as exc:
        raise CorruptQuestionRequestError(
            f"QuestionRequest state JSON을 해석할 수 없습니다: {exc}"
        ) from exc
    if not isinstance(raw_payload, dict):
        raise CorruptQuestionRequestError("QuestionRequest state JSON은 object여야 합니다.")
    payload = cast(dict[str, object], raw_payload)
    if payload.get("kind") != state_kind:
        raise CorruptQuestionRequestError(
            "QuestionRequest state_kind 열과 state JSON kind가 일치하지 않습니다."
        )
    try:
        # strict Python 경로는 JSON의 ISO datetime을 문자열로 보아 거부한다.
        # JSON strict 경로를 써야 due_at 같은 datetime은 JSON 표현으로만 coercion되고,
        # int→str 같은 느슨한 값 변환은 계속 차단된다.
        state = _QUESTION_REQUEST_STATE_ADAPTER.validate_json(raw_json, strict=True)
    except ValidationError as exc:
        raise CorruptQuestionRequestError(
            f"QuestionRequest state가 v1 도메인 계약에 맞지 않습니다: {exc}"
        ) from exc
    return state


def _question_request_from_row(row: sqlite3.Row) -> QuestionRequest:
    """한 행 전체를 엄격 decode한다. 하나라도 손상됐으면 부분 복구하지 않는다."""
    try:
        revision = row["revision"]
        if type(revision) is not int:
            raise CorruptQuestionRequestError(
                "QuestionRequest row 열 'revision'은 INTEGER여야 합니다."
            )
        raw_disposition = _require_row_optional_str(row, "initial_disposition")
        disposition = cast(InitialDisposition | None, raw_disposition)
        request = QuestionRequest.model_validate(
            {
                "request_id": _require_row_str(row, "request_id"),
                "org_id": _require_row_str(row, "org_id"),
                "requester_id": _require_row_str(row, "requester_id"),
                "session_id": _require_row_optional_str(row, "session_id"),
                "question": _require_row_str(row, "question"),
                "context_snapshot": _require_row_optional_str(row, "context_snapshot"),
                "intent": _require_row_optional_str(row, "intent"),
                "initial_disposition": disposition,
                "state": _decode_question_request_state(row),
                "revision": revision,
                "created_at": _parse(_require_row_str(row, "created_at")),
                "updated_at": _parse(_require_row_str(row, "updated_at")),
            },
            strict=True,
        )
        if isinstance(request.state, Received):
            validate_new_question_request_semantics(request)
        elif request.revision == 0:
            raise ValueError(
                "Received가 아닌 persisted QuestionRequest의 revision은 1 이상이어야 합니다."
            )
    except CorruptQuestionRequestError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        request_id = row["request_id"] if "request_id" in row.keys() else "<unknown>"
        raise CorruptQuestionRequestError(
            f"QuestionRequest row {request_id!r}가 도메인 계약에 맞지 않습니다: {exc}"
        ) from exc
    return request


def _insert_question_request_no_commit(
    connection: sqlite3.Connection,
    request: QuestionRequest,
) -> None:
    """QuestionRequest 한 행을 삽입한다. 트랜잭션 소유자가 commit/rollback한다."""
    validate_new_question_request_semantics(request)
    connection.execute(
        "INSERT OR ABORT INTO question_requests ("
        "request_id, org_id, requester_id, session_id, question, context_snapshot, "
        "intent, initial_disposition, state_kind, state_json, state_schema_version, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _question_request_values(request),
    )


def _select_question_request_no_commit(
    connection: sqlite3.Connection,
    request_id: str,
) -> QuestionRequest | None:
    """현재 QuestionRequest를 decode한다. 읽기만 하며 commit하지 않는다."""
    row = connection.execute(
        "SELECT * FROM question_requests WHERE request_id COLLATE BINARY = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    request = _question_request_from_row(row)
    if request.request_id != request_id:
        raise CorruptQuestionRequestError(
            "QuestionRequest request_id 조회가 exact match를 위반했습니다: "
            f"요청={request_id!r}, 저장={request.request_id!r}"
        )
    return request


def _compare_and_set_question_request_no_commit(
    connection: sqlite3.Connection,
    request_id: str,
    expected_revision: int,
    current: QuestionRequest,
    updated: QuestionRequest,
) -> bool:
    """정확 current 확인 뒤 id+revision 조건으로 교체한다. commit하지 않는다."""
    validate_compare_and_set_semantics(
        request_id,
        expected_revision,
        current,
        updated,
    )
    stored = _select_question_request_no_commit(connection, request_id)
    if stored is None:
        return False
    if stored.revision != expected_revision or stored != current:
        return False

    cursor = connection.execute(
        "UPDATE question_requests SET "
        "intent = ?, initial_disposition = ?, state_kind = ?, state_json = ?, "
        "state_schema_version = ?, revision = ?, updated_at = ? "
        "WHERE request_id COLLATE BINARY = ? AND revision = ?",
        (
            updated.intent,
            updated.initial_disposition,
            updated.state.kind,
            _canonical_question_request_state_json(updated.state),
            _QUESTION_REQUEST_STATE_SCHEMA_VERSION,
            updated.revision,
            _iso(updated.updated_at),
            request_id,
            expected_revision,
        ),
    )
    if cursor.rowcount not in {0, 1}:
        raise CorruptQuestionRequestError(
            "QuestionRequest CAS가 단일 aggregate가 아닌 "
            f"{cursor.rowcount}개 행을 변경했습니다: {request_id!r}"
        )
    return cursor.rowcount == 1


class SqliteQuestionRequestStore:
    """ADR 0042 QuestionRequestStore의 SQLite v1 durable 구현.

    각 공개 쓰기는 이 인스턴스의 RLock과 SQLite ``BEGIN IMMEDIATE``를 함께 써서
    같은 연결의 스레드 경쟁과 서로 다른 연결/프로세스의 revision 경쟁을 모두
    직렬화한다. 모듈 내부 ``*_no_commit`` 함수는 P17.3 Answer Finalization UoW가
    같은 트랜잭션에 AnswerRecord·감사·outbox를 묶을 수 있도록 commit하지 않는다.

    additive schema 호환은 nullable 추가 열(생성 열 포함)과 unknown non-unique
    index까지만 허용한다. NOT NULL 추가 열은 default 표현을 추론하지 않고 다음 schema
    version으로 올리게 한다. trigger·foreign key·CHECK·unknown UNIQUE는 시작 시
    거부한다. 시작 뒤 생긴 raw constraint 오류도 duplicate ID로 오분류하지 않는다.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.workflow_durability: Literal["ephemeral", "durable"] = (
            "ephemeral" if str(db_path) in {"", ":memory:"} else "durable"
        )
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.executescript(_QUESTION_REQUEST_TABLE_SCHEMA)
                _validate_question_request_schema(self._conn)
                self._conn.executescript(_QUESTION_REQUEST_INDEX_SCHEMA)
                _validate_question_request_indexes(self._conn)
                self._conn.commit()
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create(self, request: QuestionRequest) -> QuestionRequest:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                _insert_question_request_no_commit(self._conn, request)
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                if exc.sqlite_errorcode in {
                    sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
                    sqlite3.SQLITE_CONSTRAINT_UNIQUE,
                }:
                    row = self._conn.execute(
                        "SELECT COUNT(*) AS count FROM question_requests "
                        "WHERE request_id COLLATE BINARY = ?",
                        (request.request_id,),
                    ).fetchone()
                    exact_count = 0 if row is None else int(row["count"])
                    if exact_count == 1:
                        raise DuplicateQuestionRequestError(
                            f"이미 존재하는 Question Request: {request.request_id!r}"
                        ) from exc
                    if exact_count > 1:
                        raise CorruptQuestionRequestError(
                            "같은 exact request_id가 여러 행에 존재합니다: "
                            f"{request.request_id!r} ({exact_count})"
                        ) from exc
                # 열린 뒤 UNIQUE index/trigger가 추가된 경우도 duplicate ID로
                # 오분류하지 않는다. 알려진 v1 shape면 CHECK 등 raw constraint를
                # 원래 sqlite3.IntegrityError 그대로 올린다.
                _validate_question_request_schema(self._conn)
                _validate_question_request_indexes(self._conn)
                raise
            except Exception:
                self._conn.rollback()
                raise
        return request

    def get(self, request_id: str) -> QuestionRequest | None:
        with self._lock:
            return _select_question_request_no_commit(self._conn, request_id)

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                changed = _compare_and_set_question_request_no_commit(
                    self._conn,
                    request_id,
                    expected_revision,
                    current,
                    updated,
                )
                self._conn.commit()
                return changed
            except Exception:
                self._conn.rollback()
                raise

    def nonterminal(self) -> list[QuestionRequest]:
        """모든 행을 먼저 decode해 terminal처럼 위장한 손상 행도 절대 건너뛰지 않는다."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM question_requests").fetchall()
            decoded = [_question_request_from_row(row) for row in rows]
        return sorted(
            (request for request in decoded if not request.is_terminal),
            key=lambda request: (request.created_at, request.request_id),
        )


# ── SqliteSessionStore ──────────────────────────────────────────────────────

_SESSION_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id     TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL,
        status         TEXT NOT NULL,
        started_at     TEXT NOT NULL,
        last_active_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sessions_active_user
        ON sessions(user_id) WHERE status = 'active'
    """,
    """
    CREATE TABLE IF NOT EXISTS session_turns (
        session_id  TEXT NOT NULL,
        turn_index  INTEGER NOT NULL,
        question    TEXT NOT NULL,
        answer_text TEXT NOT NULL,
        answered_by TEXT NOT NULL,
        at          TEXT NOT NULL,
        request_id  TEXT,
        PRIMARY KEY (session_id, turn_index)
    )
    """,
)


class SqliteSessionStore:
    """durable SessionStore — SQLite 백엔드(SqliteSessionStore, ADR 0024).

    InMemorySessionStore 와 동작 동치. 색인: session_id(get)·user_id(active_for_user).
    상태 전이(active→ended)·트랜스크립트 순서·유휴 슬라이딩(30분 주입 clock)·end 후
    맥락 비움을 SQL 로 재현한다. 프로세스 재시작에도 보존(재오픈→조회 durable).

    동시성: check_same_thread=False 단일 연결 + RLock 직렬화(InMemory _lock 정합).

    P17.2c-1a schema v1은 이 SQLite 파일을 단일 애플리케이션이 소유하고,
    실행 중 out-of-band DDL·직접 SQL write가 없다는 전제의 startup/reopen
    capability gate다. main persistent trigger/view allowlist는 empty이며, 확장은
    검토된 exact DDL/hash를 가진 새 schema version migration으로만 연다.
    """

    IDLE_TIMEOUT_SECONDS: int = 30 * 60

    def __init__(
        self,
        db_path: str | Path,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                _initialize_correlation_schema(
                    self._conn,
                    _SESSION_SCHEMA_STATEMENTS,
                    "session_turns",
                )
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 공개 포트 ────────────────────────────────────────────────────────
    def open_or_get(self, user_id: str) -> Session:
        with self._lock:
            existing = self._active_row_for_user(user_id)
            if existing is not None:
                session = self._row_to_session(existing, load_transcript=True)
                if self._is_idle_expired(session):
                    self._auto_end(session)
                else:
                    return session

            now = self._clock()
            session = Session(
                session_id=_new_session_id(),
                user_id=user_id,
                status="active",
                transcript=(),
                started_at=now,
                last_active_at=now,
            )
            self._conn.execute(
                "INSERT INTO sessions"
                " (session_id, user_id, status, started_at, last_active_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.user_id,
                    session.status,
                    _iso(session.started_at),
                    _iso(session.last_active_at),
                ),
            )
            self._conn.commit()
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_session(row, load_transcript=True)

    def append_turn(self, session_id: str, turn: SessionTurn) -> Session:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"활성 세션 없음: {session_id!r}")

            next_index = self._conn.execute(
                "SELECT COALESCE(MAX(turn_index) + 1, 0) AS n"
                " FROM session_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()["n"]
            self._conn.execute(
                "INSERT INTO session_turns"
                " (session_id, turn_index, question, answer_text, answered_by, at, request_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    next_index,
                    turn.question,
                    turn.answer_text,
                    turn.answered_by,
                    _iso(turn.at),
                    turn.request_id,
                ),
            )
            new_last_active = self._clock()
            self._conn.execute(
                "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                (_iso(new_last_active), session_id),
            )
            self._conn.commit()
            return self._row_to_session(
                self._conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone(),
                load_transcript=True,
            )

    def end(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            self._clear_and_end(session_id)
            self._conn.commit()
            ended_row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self._row_to_session(ended_row, load_transcript=True)

    def active_for_user(self, user_id: str) -> Session | None:
        with self._lock:
            row = self._active_row_for_user(user_id)
            if row is None:
                return None
            return self._row_to_session(row, load_transcript=True)

    # ── 내부 헬퍼(락 보유 상태에서만 호출) ───────────────────────────────
    def _active_row_for_user(self, user_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND status = 'active'"
            " ORDER BY started_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    def _auto_end(self, session: Session) -> None:
        self._clear_and_end(session.session_id)
        self._conn.commit()

    def _clear_and_end(self, session_id: str) -> None:
        # end 후 맥락(트랜스크립트) 비워짐 불변식 — 턴 전삭제 + status='ended'.
        self._conn.execute("DELETE FROM session_turns WHERE session_id = ?", (session_id,))
        self._conn.execute(
            "UPDATE sessions SET status = 'ended' WHERE session_id = ?",
            (session_id,),
        )

    def _is_idle_expired(self, session: Session) -> bool:
        elapsed = (self._clock() - session.last_active_at).total_seconds()
        return elapsed >= self.IDLE_TIMEOUT_SECONDS

    def _row_to_session(self, row: sqlite3.Row, *, load_transcript: bool) -> Session:
        transcript: tuple[SessionTurn, ...] = ()
        if load_transcript and row["status"] == "active":
            turn_rows = self._conn.execute(
                "SELECT question, answer_text, answered_by, at, request_id"
                " FROM session_turns WHERE session_id = ? ORDER BY turn_index ASC",
                (row["session_id"],),
            ).fetchall()
            transcript = tuple(
                SessionTurn(
                    question=t["question"],
                    answer_text=t["answer_text"],
                    answered_by=t["answered_by"],
                    at=_parse(t["at"]),
                    request_id=t["request_id"],
                )
                for t in turn_rows
            )
        return Session(
            session_id=row["session_id"],
            user_id=row["user_id"],
            status=row["status"],
            transcript=transcript,
            started_at=_parse(row["started_at"]),
            last_active_at=_parse(row["last_active_at"]),
        )


# ── SqliteTokenStore ────────────────────────────────────────────────────────

_TOKEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    token_id    TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    role        TEXT NOT NULL,
    token_hash  TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    expires_at  TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0,
    revoked_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);
"""


def _default_token_factory() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _hash_token(raw: str) -> str:
    import hashlib

    return hashlib.sha256(raw.encode()).hexdigest()


class SqliteTokenStore:
    """durable TokenStore — SQLite 백엔드(SqliteTokenStore, ADR 0026).

    InMemoryTokenStore 와 동작 동치. 색인: token_hash(verify)·token_id(revoke).
    평문 미저장(해시만)·등록 무결성(만료/revoke/위조/없음→None)·append-only revoke·
    주입 clock/now seam 을 SQL 로 재현한다. 재시작에도 보존(재오픈→verify durable).

    동시성: check_same_thread=False 단일 연결 + RLock 직렬화(InMemory 정합).
    """

    def __init__(
        self,
        db_path: str | Path,
        token_factory: Callable[[], str] = _default_token_factory,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._token_factory = token_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_TOKEN_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def issue(
        self,
        owner_id: str,
        role: WorkerRole,
        *,
        now: datetime,
        expires_in: timedelta | None = None,
    ) -> tuple[str, AdmissionToken]:
        raw = self._token_factory()
        token_hash = _hash_token(raw)
        token_id = _new_token_id()
        expires_at = (now + expires_in) if expires_in is not None else None

        token = AdmissionToken(
            token_id=token_id,
            owner_id=owner_id,
            role=role,
            token_hash=token_hash,
            issued_at=now,
            expires_at=expires_at,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO tokens"
                " (token_id, owner_id, role, token_hash, issued_at,"
                "  expires_at, revoked, revoked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, NULL)",
                (
                    token.token_id,
                    token.owner_id,
                    token.role,
                    token.token_hash,
                    _iso(token.issued_at),
                    _iso(expires_at) if expires_at is not None else None,
                ),
            )
            self._conn.commit()
        return raw, token

    def verify(self, raw_token: str, *, now: datetime) -> AdmissionToken | None:
        token_hash = _hash_token(raw_token)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        if row is None:
            return None
        token = self._row_to_token(row)
        if token.revoked:
            return None
        if token.expires_at is not None and now >= token.expires_at:
            return None
        return token

    def revoke(self, token_id: str, *, now: datetime | None = None) -> AdmissionToken | None:
        """append-only revoke — 삭제 X·revoked=1 표식·멱등.

        now 주입 시 그 시각을 revoked_at 으로 찍는다(issue/verify seam 대칭·durable
        재현성). None이면 생성자 clock(InMemory 와 동형).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tokens WHERE token_id = ?", (token_id,)
            ).fetchone()
            if row is None:
                return None
            token = self._row_to_token(row)
            if token.revoked:
                return token

            revoked_at = now if now is not None else self._clock()
            self._conn.execute(
                "UPDATE tokens SET revoked = 1, revoked_at = ? WHERE token_id = ?",
                (_iso(revoked_at), token_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM tokens WHERE token_id = ?", (token_id,)
            ).fetchone()
            return self._row_to_token(updated)

    def list_active(self, now: datetime | None = None) -> list[AdmissionToken]:
        effective_now = now if now is not None else self._clock()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tokens WHERE revoked = 0").fetchall()
        result: list[AdmissionToken] = []
        for row in rows:
            token = self._row_to_token(row)
            if token.expires_at is not None and effective_now >= token.expires_at:
                continue
            result.append(token)
        return result

    def _row_to_token(self, row: sqlite3.Row) -> AdmissionToken:
        role: WorkerRole = "primary" if row["role"] == "primary" else "backup"
        return AdmissionToken(
            token_id=row["token_id"],
            owner_id=row["owner_id"],
            role=role,
            token_hash=row["token_hash"],
            issued_at=_parse(row["issued_at"]),
            expires_at=_parse_opt(row["expires_at"]),
            revoked=bool(row["revoked"]),
            revoked_at=_parse_opt(row["revoked_at"]),
        )


# ── SqliteAnswerRecordStore — 답변 감사 단위 (Phase 12, ADR 0033) ──────────────

_ANSWER_RECORD_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS answer_records (
        record_id               TEXT PRIMARY KEY,
        question                TEXT NOT NULL,
        answer_text              TEXT NOT NULL,
        answered_by              TEXT NOT NULL,
        agent_id                 TEXT NOT NULL,
        mode                      TEXT NOT NULL,
        session_id                TEXT,
        answered_at               TEXT NOT NULL,
        needs_correction_review  INTEGER NOT NULL DEFAULT 0,
        request_id                TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_answer_records_agent ON answer_records(agent_id)
    """,
)


class UnsupportedAnswerRecordEvidenceError(RuntimeError):
    """legacy SQLite schema가 sources/snapshot_sha를 조용히 유실하려 함."""


class SqliteAnswerRecordStore:
    """durable `AnswerRecordStore` — SQLite 백엔드(Phase 12, ADR 0033 결정 4).

    `InMemoryAnswerRecordStore`와 동작 동치. append-only 계약 유지 — `add`는
    새 레코드를 삽입할 뿐 기존 `record_id`의 필드를 UPDATE하지 않는다(전이 ≠
    기록 — 나간 답의 감사 단위는 한 번 적재되면 불변). 동시성은 다른 sqlite
    스토어와 동형(check_same_thread=False 단일 연결 + RLock).

    P17.2c-1a schema v1은 이 SQLite 파일을 단일 애플리케이션이 소유하고,
    실행 중 out-of-band DDL·직접 SQL write가 없다는 전제의 startup/reopen
    capability gate다. main persistent trigger/view allowlist는 empty이며, 확장은
    검토된 exact DDL/hash를 가진 새 schema version migration으로만 연다.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._completion_schema_v2 = False
        try:
            with self._lock:
                self._conn.execute("PRAGMA foreign_keys = ON")
                if has_sqlite_completion_manifest(self._conn):
                    # ADR 0044 runtime open은 validate-only다. capable DB를 기존
                    # correlation initializer로 열면 v2 partial UNIQUE를 v1 drift로
                    # 오인하고, 반대로 IF NOT EXISTS DDL이 drift를 숨길 수 있다.
                    validate_sqlite_completion_connection(self._conn)
                    self._completion_schema_v2 = True
                else:
                    _initialize_correlation_schema(
                        self._conn,
                        _ANSWER_RECORD_SCHEMA_STATEMENTS,
                        "answer_records",
                    )
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add(self, rec: AnswerRecord) -> None:
        try:
            rec = AnswerRecord.model_validate(
                {
                    "record_id": rec.record_id,
                    "question": rec.question,
                    "answer_text": rec.answer_text,
                    "answered_by": rec.answered_by,
                    "agent_id": rec.agent_id,
                    "mode": rec.mode,
                    # model_dump는 exclude_if를 적용하므로, model_construct로 만든
                    # falsey tuple subclass가 증거를 숨기지 못하게 명시적으로 싣는다.
                    "sources": rec.sources,
                    "snapshot_sha": rec.snapshot_sha,
                    "session_id": rec.session_id,
                    "answered_at": rec.answered_at,
                    "needs_correction_review": rec.needs_correction_review,
                    "request_id": rec.request_id,
                },
                strict=True,
            )
        except Exception as error:
            raise UnsupportedAnswerRecordEvidenceError(
                "SQLite AnswerRecord write는 canonical record만 허용합니다."
            ) from error
        if self._completion_schema_v2 and rec.request_id is not None:
            raise UnsupportedAnswerRecordEvidenceError(
                "SQLite AnswerRecord schema v2의 request-aware direct write는 "
                "금지됩니다. SqliteQuestionCompletionUnitOfWork를 사용해야 합니다."
            )
        if rec.sources or rec.snapshot_sha is not None:
            raise UnsupportedAnswerRecordEvidenceError(
                "SQLite AnswerRecord schema v1/legacy direct write는 "
                "sources/snapshot_sha evidence를 보존할 수 없습니다. "
                "Completion UoW를 사용해야 합니다."
            )
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO answer_records"
                " (record_id, question, answer_text, answered_by, agent_id,"
                "  mode, session_id, answered_at, needs_correction_review, request_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.record_id,
                    rec.question,
                    rec.answer_text,
                    rec.answered_by,
                    rec.agent_id,
                    rec.mode,
                    rec.session_id,
                    _iso(rec.answered_at),
                    int(rec.needs_correction_review),
                    rec.request_id,
                ),
            )
            self._conn.commit()

    def get(self, record_id: str) -> AnswerRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM answer_records WHERE record_id = ?", (record_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def for_agent(self, agent_id: str) -> list[AnswerRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM answer_records WHERE agent_id = ?", (agent_id,)
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _row_to_record(self, row: sqlite3.Row) -> AnswerRecord:
        mode: AnswerMode = row["mode"]
        sources: tuple[str, ...] = ()
        snapshot_sha: str | None = None
        if "sources_json" in row.keys():
            raw_sources = row["sources_json"]
            # in-place migration은 기존 request-aware legacy 행도 backfill하지 않는다.
            # 이 legacy Store는 NULL을 기존 계약의 sources=()/snapshot=None으로 읽고,
            # receipt-linked v2 행의 NULL 손상 판정은 strict Completion Reader가 맡는다.
            if isinstance(raw_sources, str):
                try:
                    payload: object = json.loads(
                        raw_sources,
                        parse_constant=_reject_nonstandard_json_constant,
                    )
                except (json.JSONDecodeError, TypeError, ValueError) as error:
                    raise UnsupportedAnswerRecordEvidenceError(
                        "schema v2 AnswerRecord sources_json이 유효한 JSON이 아닙니다."
                    ) from error
                source_values = cast(list[object], payload)
                if not isinstance(payload, list) or any(
                    not isinstance(source, str) or not source.strip() for source in source_values
                ):
                    raise UnsupportedAnswerRecordEvidenceError(
                        "schema v2 AnswerRecord sources_json은 nonblank 문자열 배열이어야 합니다."
                    )
                canonical = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                if canonical != raw_sources:
                    raise UnsupportedAnswerRecordEvidenceError(
                        "schema v2 AnswerRecord sources_json은 canonical JSON이어야 합니다."
                    )
                sources = tuple(cast(str, source) for source in source_values)
            elif raw_sources is not None:
                raise UnsupportedAnswerRecordEvidenceError(
                    "schema v2 AnswerRecord sources_json은 TEXT 또는 NULL이어야 합니다."
                )
            raw_snapshot = row["snapshot_sha"]
            if raw_snapshot is not None and not isinstance(raw_snapshot, str):
                raise UnsupportedAnswerRecordEvidenceError(
                    "schema v2 AnswerRecord snapshot_sha는 TEXT 또는 NULL이어야 합니다."
                )
            snapshot_sha = raw_snapshot
        return AnswerRecord(
            record_id=row["record_id"],
            question=row["question"],
            answer_text=row["answer_text"],
            answered_by=row["answered_by"],
            agent_id=row["agent_id"],
            mode=mode,
            sources=sources,
            snapshot_sha=snapshot_sha,
            session_id=row["session_id"],
            answered_at=_parse(row["answered_at"]),
            needs_correction_review=bool(row["needs_correction_review"]),
            request_id=row["request_id"],
        )


# ── SqliteCorrectionStore — 사후 정정 이벤트 (Phase 12, ADR 0033) ─────────────

_CORRECTION_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS correction_events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    record_id      TEXT NOT NULL,
    corrected_text TEXT NOT NULL,
    by_owner       TEXT NOT NULL,
    rationale      TEXT NOT NULL DEFAULT '',
    corrected_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_correction_events_record ON correction_events(record_id);
"""


class SqliteCorrectionStore:
    """durable `CorrectionStore` — SQLite 백엔드(Phase 12, ADR 0033 결정 4).

    `InMemoryCorrectionStore`와 동작 동치. append-only — 원 `AnswerRecord`를
    건드리지 않고 새 이벤트만 쌓는다(UPDATE 없음). `seq`(AUTOINCREMENT)로
    삽입 순서를 보존해 `for_record`가 append 순서 그대로 돌려준다(전이 ≠ 기록,
    `CorrectionEvent`는 새 인스턴스로만 증가한다 — 기존 이벤트 불변).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CORRECTION_EVENT_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append(self, event: CorrectionEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO correction_events"
                " (event_id, record_id, corrected_text, by_owner, rationale,"
                "  corrected_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.record_id,
                    event.corrected_text,
                    event.by_owner,
                    event.rationale,
                    _iso(event.corrected_at),
                ),
            )
            self._conn.commit()

    def for_record(self, record_id: str) -> list[CorrectionEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM correction_events WHERE record_id = ? ORDER BY seq ASC",
                (record_id,),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def get(self, event_id: str) -> CorrectionEvent | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM correction_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_event(row)

    def _row_to_event(self, row: sqlite3.Row) -> CorrectionEvent:
        return CorrectionEvent(
            event_id=row["event_id"],
            record_id=row["record_id"],
            corrected_text=row["corrected_text"],
            by_owner=row["by_owner"],
            rationale=row["rationale"],
            corrected_at=_parse(row["corrected_at"]),
        )


# ── SqliteFeedbackStore — 질문자 답변 피드백 (계획 §10, ADR 0033 계열) ────────

_ANSWER_FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_feedback_latest (
    record_id    TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    comment      TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (record_id, submitted_by)
);
CREATE TABLE IF NOT EXISTS answer_feedback_history (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id    TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    comment      TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_answer_feedback_history_record
    ON answer_feedback_history(record_id);
"""


class SqliteFeedbackStore:
    """durable `FeedbackStore` — SQLite 백엔드(계획 §10.2 "최신 우선(upsert), 이력 보존").

    `InMemoryFeedbackStore`와 동작 동치. 두 테이블로 멱등 정책을 재현한다:
      - `answer_feedback_latest` — `(record_id, submitted_by)` PK upsert. 같은
        질문자가 같은 답에 재제출하면 최신 verdict/comment로 *판정*을 덮는다
        (`latest_for_record`가 record별 최신을 고르는 원천).
      - `answer_feedback_history` — append-only(seq AUTOINCREMENT). 모든 제출을
        전량 보존한다(`for_record`가 append 순서 그대로 돌려줌 — 전이 ≠ 기록:
        판정은 최신, 기록은 전량).

    동시성은 다른 sqlite 스토어와 동형(check_same_thread=False 단일 연결 + RLock).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_ANSWER_FEEDBACK_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert(self, fb: AnswerFeedback) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO answer_feedback_latest"
                " (record_id, submitted_by, verdict, comment, submitted_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(record_id, submitted_by) DO UPDATE SET"
                "   verdict = excluded.verdict,"
                "   comment = excluded.comment,"
                "   submitted_at = excluded.submitted_at",
                (
                    fb.record_id,
                    fb.submitted_by,
                    fb.verdict,
                    fb.comment,
                    _iso(fb.submitted_at),
                ),
            )
            self._conn.execute(
                "INSERT INTO answer_feedback_history"
                " (record_id, submitted_by, verdict, comment, submitted_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    fb.record_id,
                    fb.submitted_by,
                    fb.verdict,
                    fb.comment,
                    _iso(fb.submitted_at),
                ),
            )
            self._conn.commit()

    def latest_for_record(self, record_id: str) -> AnswerFeedback | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM answer_feedback_latest WHERE record_id = ?",
                (record_id,),
            ).fetchall()
        if not rows:
            return None
        return max(
            (self._row_to_feedback(r) for r in rows),
            key=lambda fb: fb.submitted_at,
        )

    def for_record(self, record_id: str) -> list[AnswerFeedback]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM answer_feedback_history WHERE record_id = ? ORDER BY seq ASC",
                (record_id,),
            ).fetchall()
        return [self._row_to_feedback(r) for r in rows]

    def _row_to_feedback(self, row: sqlite3.Row) -> AnswerFeedback:
        verdict: FeedbackVerdict = "good" if row["verdict"] == "good" else "bad"
        return AnswerFeedback(
            record_id=row["record_id"],
            verdict=verdict,
            comment=row["comment"],
            submitted_by=row["submitted_by"],
            submitted_at=_parse(row["submitted_at"]),
        )


# ── SqliteKnowledgeStore — 중앙 지식 저장소 본문 (Phase 12, ADR 0033) ─────────

_KNOWLEDGE_BUNDLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_bundles (
    agent_id   TEXT PRIMARY KEY,
    documents  TEXT NOT NULL,
    version    TEXT NOT NULL,
    synced_at  TEXT NOT NULL
);
"""


class SqliteKnowledgeStore:
    """durable `KnowledgeStore` — SQLite 백엔드(Phase 12, ADR 0033 결정 1·3).

    `InMemoryKnowledgeStore`와 동작 동치. `put`은 순수 보관(전이 아님)이라
    *최신 version만 수용하는 upsert*가 정당하다(감사 로그가 아니라 "지금 이
    agent_id의 최신 본문" 하나만 보관하는 그릇 — append-only 계약 대상이 아님).
    `documents`(튜플)는 JSON 배열로 직렬화한다.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_KNOWLEDGE_BUNDLE_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def put(self, content: KnowledgeBundleContent) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, synced_at FROM knowledge_bundles WHERE agent_id = ?",
                (content.agent_id,),
            ).fetchone()
            if row is not None:
                if content.version == row["version"]:
                    return
                if content.synced_at < _parse(row["synced_at"]):
                    return
            docs_json = json.dumps(
                [{"path": d.path, "body": d.body} for d in content.documents],
                ensure_ascii=False,
            )
            self._conn.execute(
                "INSERT INTO knowledge_bundles (agent_id, documents, version, synced_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(agent_id) DO UPDATE SET"
                "   documents = excluded.documents,"
                "   version = excluded.version,"
                "   synced_at = excluded.synced_at",
                (
                    content.agent_id,
                    docs_json,
                    content.version,
                    _iso(content.synced_at),
                ),
            )
            self._conn.commit()

    def get(self, agent_id: str) -> KnowledgeBundleContent | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_bundles WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_content(row)

    def is_stale(self, agent_id: str, *, now: datetime, threshold_s: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT synced_at FROM knowledge_bundles WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None:
            return True
        elapsed = (now - _parse(row["synced_at"])).total_seconds()
        return elapsed > threshold_s

    def _row_to_content(self, row: sqlite3.Row) -> KnowledgeBundleContent:
        raw_docs: list[dict[str, str]] = json.loads(row["documents"])
        documents = tuple(KnowledgeDoc(path=d["path"], body=d["body"]) for d in raw_docs)
        return KnowledgeBundleContent(
            agent_id=row["agent_id"],
            documents=documents,
            version=row["version"],
            synced_at=_parse(row["synced_at"]),
        )


# ── SqliteRegistryJournal — 카드 라이브 등록·오너 변경 durable 저널 ───────────
# (Phase 12, ADR 0034 결정 1·2 — "AON_DB 켜면 SQLite 영속")

RegistryJournalKind = Literal["register", "transfer"]


@dataclass(frozen=True)
class RegistryJournalCandidate:
    """저널에 적재하는 카드 후보 값 — `admin_registry.CardCandidate`의 durable 투영.

    `sqlite_stores.py`는 `admin_registry.py`를 import하지 않는다(순환 회피 —
    `admin_registry.py`가 이 모듈의 `SqliteRegistryJournal`을 참조하는 방향
    하나로 의존을 고정한다). 그래서 `CardCandidate`와 같은 필드를 이 모듈
    안에서 독립적으로 들고, `admin_registry.replay_registry_journal`이
    `CardCandidate(**entry.candidate.__dict__)`로 변환해 admission에 태운다.
    """

    agent_id: str
    owner: str
    team: str
    summary: str
    domains: tuple[str, ...]
    last_reviewed_at: str
    maintainer: str | None = None
    can_answer: tuple[str, ...] = ()
    cannot_answer: tuple[str, ...] = ()
    approval_when: tuple[str, ...] = ()
    collaborate_when: tuple[str, ...] = ()
    knowledge_sources: tuple[str, ...] = ()
    trust_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegistryJournalEntry:
    """저널 한 줄 — `register`(신규 등록) | `transfer`(오너 변경) + 카드 후보 + 메타."""

    kind: RegistryJournalKind
    candidate: RegistryJournalCandidate
    by: str
    at: datetime


_REGISTRY_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    candidate  TEXT NOT NULL,
    by         TEXT NOT NULL,
    at         TEXT NOT NULL
);
"""


class SqliteRegistryJournal:
    """카드 라이브 등록·오너 변경의 durable 저널(append-only, ADR 0034 결정 1·2).

    Registry 자체를 SQLite화하지 않는다 — 기존 "InMemory Registry + YAML 시드"
    구조를 유지하되, 라이브 mutation(등록·오너 변경)만 이 저널에 순서대로 남긴다.
    중앙 기동 시 YAML 시드 로드 → `admin_registry.replay_registry_journal`이 이
    저널을 처음부터 순서대로 재생해 라이브 상태를 복원한다(admission 경유 —
    무효 카드/오너는 복원되지 않는다).

    `AdminRegistryService(journal_sink=...)`로 주입하면 `register_card`·
    `transfer_ownership` 성공 시 자동으로 이 저널에 append된다.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_REGISTRY_JOURNAL_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append_register(
        self,
        *,
        agent_id: str,
        owner: str,
        team: str,
        summary: str,
        domains: list[str] | tuple[str, ...],
        last_reviewed_at: str,
        by: str,
        at: datetime,
        maintainer: str | None = None,
        can_answer: list[str] | tuple[str, ...] = (),
        cannot_answer: list[str] | tuple[str, ...] = (),
        approval_when: list[str] | tuple[str, ...] = (),
        collaborate_when: list[str] | tuple[str, ...] = (),
        knowledge_sources: list[str] | tuple[str, ...] = (),
        trust_labels: list[str] | tuple[str, ...] = (),
    ) -> None:
        self._append(
            "register",
            agent_id=agent_id,
            owner=owner,
            team=team,
            summary=summary,
            domains=domains,
            last_reviewed_at=last_reviewed_at,
            by=by,
            at=at,
            maintainer=maintainer,
            can_answer=can_answer,
            cannot_answer=cannot_answer,
            approval_when=approval_when,
            collaborate_when=collaborate_when,
            knowledge_sources=knowledge_sources,
            trust_labels=trust_labels,
        )

    def append_transfer(
        self,
        *,
        agent_id: str,
        owner: str,
        team: str,
        summary: str,
        domains: list[str] | tuple[str, ...],
        last_reviewed_at: str,
        by: str,
        at: datetime,
        maintainer: str | None = None,
        can_answer: list[str] | tuple[str, ...] = (),
        cannot_answer: list[str] | tuple[str, ...] = (),
        approval_when: list[str] | tuple[str, ...] = (),
        collaborate_when: list[str] | tuple[str, ...] = (),
        knowledge_sources: list[str] | tuple[str, ...] = (),
        trust_labels: list[str] | tuple[str, ...] = (),
    ) -> None:
        self._append(
            "transfer",
            agent_id=agent_id,
            owner=owner,
            team=team,
            summary=summary,
            domains=domains,
            last_reviewed_at=last_reviewed_at,
            by=by,
            at=at,
            maintainer=maintainer,
            can_answer=can_answer,
            cannot_answer=cannot_answer,
            approval_when=approval_when,
            collaborate_when=collaborate_when,
            knowledge_sources=knowledge_sources,
            trust_labels=trust_labels,
        )

    def _append(
        self,
        kind: RegistryJournalKind,
        *,
        agent_id: str,
        owner: str,
        team: str,
        summary: str,
        domains: list[str] | tuple[str, ...],
        last_reviewed_at: str,
        by: str,
        at: datetime,
        maintainer: str | None,
        can_answer: list[str] | tuple[str, ...],
        cannot_answer: list[str] | tuple[str, ...],
        approval_when: list[str] | tuple[str, ...],
        collaborate_when: list[str] | tuple[str, ...],
        knowledge_sources: list[str] | tuple[str, ...],
        trust_labels: list[str] | tuple[str, ...],
    ) -> None:
        candidate_json = json.dumps(
            {
                "agent_id": agent_id,
                "owner": owner,
                "team": team,
                "summary": summary,
                "domains": list(domains),
                "last_reviewed_at": last_reviewed_at,
                "maintainer": maintainer,
                "can_answer": list(can_answer),
                "cannot_answer": list(cannot_answer),
                "approval_when": list(approval_when),
                "collaborate_when": list(collaborate_when),
                "knowledge_sources": list(knowledge_sources),
                "trust_labels": list(trust_labels),
            },
            ensure_ascii=False,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO registry_journal (kind, candidate, by, at) VALUES (?, ?, ?, ?)",
                (kind, candidate_json, by, _iso(at)),
            )
            self._conn.commit()

    def entries(self) -> list[RegistryJournalEntry]:
        """append 순서 그대로(seq ASC) 전 저널 항목을 돌려준다(리플레이 원천)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM registry_journal ORDER BY seq ASC").fetchall()
        result: list[RegistryJournalEntry] = []
        for row in rows:
            raw: dict[str, object] = json.loads(row["candidate"])
            kind: RegistryJournalKind = "register" if row["kind"] == "register" else "transfer"
            candidate = RegistryJournalCandidate(
                agent_id=str(raw["agent_id"]),
                owner=str(raw["owner"]),
                team=str(raw["team"]),
                summary=str(raw["summary"]),
                domains=tuple(raw.get("domains") or ()),  # type: ignore[arg-type]
                last_reviewed_at=str(raw["last_reviewed_at"]),
                maintainer=raw.get("maintainer"),  # type: ignore[arg-type]
                can_answer=tuple(raw.get("can_answer") or ()),  # type: ignore[arg-type]
                cannot_answer=tuple(raw.get("cannot_answer") or ()),  # type: ignore[arg-type]
                approval_when=tuple(raw.get("approval_when") or ()),  # type: ignore[arg-type]
                collaborate_when=tuple(raw.get("collaborate_when") or ()),  # type: ignore[arg-type]
                knowledge_sources=tuple(raw.get("knowledge_sources") or ()),  # type: ignore[arg-type]
                trust_labels=tuple(raw.get("trust_labels") or ()),  # type: ignore[arg-type]
            )
            result.append(
                RegistryJournalEntry(
                    kind=kind,
                    candidate=candidate,
                    by=str(row["by"]),
                    at=_parse(row["at"]),
                )
            )
        return result


# ── SqliteUserJournal — User 라이브 등록 durable 저널 ──────────────────────────
# (ADR 0064 결정 ⑦ — `SqliteRegistryJournal`의 User 축 형제. register-only라 kind는
#  "register" 하나뿐이고, 카드 저널의 `transfer`(오너 변경)에 대응하는 축이 없다.)


@dataclass(frozen=True)
class UserJournalCandidate:
    """저널에 적재하는 User 후보 값 — `admin_users.UserCandidate`의 durable 투영.

    `sqlite_stores.py`는 `admin_users.py`를 import하지 않는다(순환 회피 —
    `admin_users.replay_user_journal`이 이 모듈의 `SqliteUserJournal`을 참조하는 방향
    하나로 의존을 고정한다·`RegistryJournalCandidate`와 같은 결). 그래서 `UserCandidate`와
    같은 필드를 이 모듈 안에서 독립적으로 들고, 리플레이가 `UserCandidate(...)`로 변환해
    admission에 태운다.
    """

    user_id: str
    email: str | None = None
    manager: str | None = None


@dataclass(frozen=True)
class UserJournalEntry:
    """저널 한 줄 — `register`(신규 등록) + User 후보 + 메타(카드 저널 entry의 User 판)."""

    kind: Literal["register"]
    candidate: UserJournalCandidate
    by: str
    at: datetime


_USER_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    candidate  TEXT NOT NULL,
    by         TEXT NOT NULL,
    at         TEXT NOT NULL
);
"""


class SqliteUserJournal:
    """User 라이브 등록의 durable 저널(append-only, ADR 0064 결정 ⑦).

    `SqliteRegistryJournal`(카드)의 User 축 형제다 — 별도 테이블(`user_journal`)에
    User 등록만 순서대로 남긴다(그 테이블은 카드 필드 shape라 user kind를 얹지 않는다).
    Registry 자체를 SQLite화하지 않는다(YAML 시드 + InMemory 라이브 구조 유지) — 라이브
    mutation(register_user)만 이 저널에 append한다. 중앙 기동 시 YAML 시드 로드 →
    `admin_users.replay_user_journal`이 이 저널을 처음부터 순서대로 재생해 라이브 User를
    복원한다(admission 경유 — 무효 User·미등록 manager는 복원되지 않는다 불변식 보존).
    **리플레이 순서는 user → card**(라이브 카드 owner가 라이브 등록 User를 참조하므로
    User가 카드보다 먼저 복원돼야 한다).

    `AdminUserService(journal_sink=...)`로 주입하면 `register_user` 성공 시 자동으로 이
    저널에 append된다(`UserJournalSink.append_register` 포트 구현).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_USER_JOURNAL_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append_register(
        self,
        *,
        user_id: str,
        email: str | None,
        manager: str | None,
        by: str,
        at: datetime,
    ) -> None:
        candidate_json = json.dumps(
            {"user_id": user_id, "email": email, "manager": manager},
            ensure_ascii=False,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO user_journal (kind, candidate, by, at) VALUES (?, ?, ?, ?)",
                ("register", candidate_json, by, _iso(at)),
            )
            self._conn.commit()

    def entries(self) -> list[UserJournalEntry]:
        """append 순서 그대로(seq ASC) 전 저널 항목을 돌려준다(리플레이 원천)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM user_journal ORDER BY seq ASC").fetchall()
        result: list[UserJournalEntry] = []
        for row in rows:
            raw: dict[str, object] = json.loads(row["candidate"])
            email = raw.get("email")
            manager = raw.get("manager")
            candidate = UserJournalCandidate(
                user_id=str(raw["user_id"]),
                email=str(email) if email is not None else None,
                manager=str(manager) if manager is not None else None,
            )
            result.append(
                UserJournalEntry(
                    kind="register",
                    candidate=candidate,
                    by=str(row["by"]),
                    at=_parse(row["at"]),
                )
            )
        return result
