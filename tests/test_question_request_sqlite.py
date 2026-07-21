"""P17.2b QuestionRequest SQLite 영속 어댑터 통합 테스트.

도메인 포트와의 동치뿐 아니라 재시작, 독립 연결 CAS 경쟁, 스키마 검증,
손상 행 fail-closed를 검증한다. tmp-file DB만 사용해 프로세스 재기동 경계를 재현한다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network import sqlite_stores as sqlite_store_module
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingApproval,
    AwaitingConflict,
    AwaitingManager,
    DeclinedRequest,
    DuplicateQuestionRequestError,
    FailedRequest,
    HandlingAssignment,
    HandlingKind,
    InvalidNewQuestionRequestError,
    QuestionRequest,
    RequestStateKind,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_stores import (
    CorruptQuestionRequestError,
    QuestionRequestSchemaError,
    SqliteQuestionRequestStore,
    SqliteSessionStore,
)

_T0 = datetime(2026, 7, 12, 9, 0, tzinfo=timezone(timedelta(hours=9)))
_T1 = _T0 + timedelta(minutes=1)
_DUE = _T0 + timedelta(hours=2)


def _handling(kind: HandlingKind, ref: str) -> HandlingAssignment:
    return HandlingAssignment(kind=kind, ref=ref, due_at=_DUE)


def _route(
    agent_id: str = "인사-담당",
    *,
    requires_approval: bool = True,
) -> RouteTarget:
    return RouteTarget(
        intent="휴가 정책",
        agent_id=agent_id,
        requires_approval=requires_approval,
        authority_version="권한-v1",
    )


def _request(
    request_id: str = "요청-가",
    *,
    created_at: datetime = _T0,
) -> QuestionRequest:
    return QuestionRequest.receive(
        org_id="조직-서울",
        requester_id="사용자-김",
        session_id="세션-한글",
        question="육아휴직 뒤 연차는 어떻게 계산하나요?",
        context_snapshot="대한민국 지사 · 정규직",
        request_id_factory=lambda: request_id,
        clock=lambda: created_at,
        due_at=_DUE,
    )


_ALL_STATE_KINDS: tuple[RequestStateKind, ...] = (
    "received",
    "ready_to_dispatch",
    "awaiting_answer",
    "awaiting_conflict",
    "awaiting_manager",
    "awaiting_approval",
    "answered",
    "declined",
    "failed",
)


def _seed_state(
    store: SqliteQuestionRequestStore,
    request_id: str,
    kind: RequestStateKind,
    *,
    created_at: datetime = _T0,
) -> QuestionRequest:
    """공개 ingress와 합법 CAS 전이만으로 원하는 역사 상태를 만든다."""
    current = _request(request_id, created_at=created_at)
    store.create(current)
    if kind == "received":
        return current

    first_at = created_at + timedelta(minutes=1)
    second_at = created_at + timedelta(minutes=2)
    if kind == "failed":
        updated = current.transition(
            FailedRequest(error_code="영속-오류"),
            clock=lambda: first_at,
        )
        assert store.compare_and_set(request_id, 0, current, updated)
        return updated

    if kind in {"awaiting_conflict"}:
        updated = current.record_initial_routing(
            intent="휴가 정책",
            disposition="contested",
            target=AwaitingConflict(
                case_id="분쟁-한글",
                handling=_handling("conflict_case", "분쟁-한글"),
            ),
            clock=lambda: first_at,
        )
        assert store.compare_and_set(request_id, 0, current, updated)
        return updated

    if kind in {"awaiting_manager", "declined"}:
        manager = current.record_initial_routing(
            intent=None,
            disposition="unowned",
            target=AwaitingManager(
                item_id="관리-한글",
                public_kind="unowned",
                handling=_handling("manager_item", "관리-한글"),
            ),
            clock=lambda: first_at,
        )
        assert store.compare_and_set(request_id, 0, current, manager)
        if kind == "awaiting_manager":
            return manager
        declined = manager.transition(
            DeclinedRequest(reason_code="담당자-거절"),
            clock=lambda: second_at,
        )
        assert store.compare_and_set(request_id, 1, manager, declined)
        return declined

    route = _route(requires_approval=kind == "awaiting_approval")
    ready = current.record_initial_routing(
        intent="휴가 정책",
        disposition="routed",
        target=ReadyToDispatch(
            route=route,
            attempt=1,
            trigger_key="재시도-키",
            handling=_handling("system", "재시도-키"),
        ),
        clock=lambda: first_at,
    )
    assert store.compare_and_set(request_id, 0, current, ready)
    if kind == "ready_to_dispatch":
        return ready
    if kind == "awaiting_answer":
        target = AwaitingAnswer(
            route=route,
            attempt=1,
            ticket_id="작업표-한글",
            handling=_handling("runtime_ticket", "작업표-한글"),
        )
    elif kind == "awaiting_approval":
        target = AwaitingApproval(
            route=route,
            attempt=1,
            draft_ref="초안-한글",
            handling=_handling("approval_item", "초안-한글"),
        )
    else:
        assert kind == "answered"
        target = AnsweredRequest(record_id="답변-한글")
    updated = ready.transition(target, clock=lambda: second_at)
    assert store.compare_and_set(request_id, 1, ready, updated)
    return updated


def test_Unowned_intent_None은_SQLite재시작뒤에도_None이고_빈문자열은_fail_closed한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "unowned-intent.db"
    store = SqliteQuestionRequestStore(db_path)
    unowned = _seed_state(store, "요청-unowned", "awaiting_manager")
    assert unowned.intent is None
    store.close()

    reopened = SqliteQuestionRequestStore(db_path)
    assert reopened.get(unowned.request_id) == unowned
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE question_requests SET intent = '' WHERE request_id = ?",
        (unowned.request_id,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorruptQuestionRequestError, match="도메인 계약"):
        reopened.get(unowned.request_id)
    with pytest.raises(CorruptQuestionRequestError, match="도메인 계약"):
        reopened.nonterminal()
    reopened.close()


@pytest.mark.parametrize("state_kind", _ALL_STATE_KINDS)
def test_아홉상태와_한글_optional_timezone이_재시작뒤_그대로_왕복한다(
    tmp_path: Path,
    state_kind: RequestStateKind,
) -> None:
    db_path = tmp_path / "question.db"
    writer = SqliteQuestionRequestStore(db_path)
    if state_kind == "received":
        original = _request(f"요청-{state_kind}")
        payload = original.model_dump()
        payload["session_id"] = None
        payload["context_snapshot"] = None
        original = QuestionRequest.model_validate(payload)
        writer.create(original)
    else:
        original = _seed_state(writer, f"요청-{state_kind}", state_kind)

    writer.close()

    reader = SqliteQuestionRequestStore(db_path)
    assert reader.get(original.request_id) == original
    reader.close()


def test_v1_schema는_envelope_state_json_revision_timestamp와_필수_index를_가진다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "schema.db"
    store = SqliteQuestionRequestStore(db_path)
    request = _request()
    store.create(request)
    store.close()

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    table_info = {
        row["name"]: row for row in connection.execute("PRAGMA table_info(question_requests)")
    }
    columns = set(table_info)
    assert {
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
    }.issubset(columns)
    assert table_info["request_id"]["pk"] == 1
    assert table_info["request_id"]["notnull"] == 1
    row = connection.execute(
        "SELECT state_kind, state_json, state_schema_version FROM question_requests"
    ).fetchone()
    assert row is not None
    assert row["state_kind"] == "received"
    assert row["state_schema_version"] == 1
    assert row["state_json"] == json.dumps(
        request.state.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    indexes = {
        row["name"]: tuple(
            info["name"] for info in connection.execute(f"PRAGMA index_info({row['name']})")
        )
        for row in connection.execute("PRAGMA index_list(question_requests)")
        if not row["name"].startswith("sqlite_autoindex")
    }
    assert indexes["idx_question_requests_state_created_id"] == (
        "state_kind",
        "created_at",
        "request_id",
    )
    assert indexes["idx_question_requests_org_created_id"] == (
        "org_id",
        "created_at",
        "request_id",
    )
    connection.close()


def test_기존_incompatible_table은_PRAGMA_required_column검증에서_즉시_실패한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(QuestionRequestSchemaError, match="누락"):
        SqliteQuestionRequestStore(db_path)


def _create_full_legacy_table(
    db_path: Path,
    *,
    request_id_decl: str = "TEXT PRIMARY KEY NOT NULL",
    org_id_decl: str = "TEXT NOT NULL",
    revision_decl: str = "INTEGER NOT NULL",
    version_decl: str = "INTEGER NOT NULL DEFAULT 1",
    extra_columns: str = "",
    table_constraint: str = "",
) -> None:
    connection = sqlite3.connect(db_path)
    connection.execute(
        f"""
        CREATE TABLE question_requests (
            request_id {request_id_decl},
            org_id {org_id_decl},
            requester_id TEXT NOT NULL,
            session_id TEXT,
            question TEXT NOT NULL,
            context_snapshot TEXT,
            intent TEXT,
            initial_disposition TEXT,
            state_kind TEXT NOT NULL,
            state_json TEXT NOT NULL,
            state_schema_version {version_decl},
            revision {revision_decl},
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
            {extra_columns}
            {table_constraint}
        )
        """
    )
    connection.commit()
    connection.close()


def test_모든필수열이_있어도_request_id_PK가_아니면_init에서_거부한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "no-primary-key.db"
    _create_full_legacy_table(db_path, request_id_decl="TEXT")

    with pytest.raises(QuestionRequestSchemaError, match="PRIMARY KEY"):
        SqliteQuestionRequestStore(db_path)


def test_legacy_TEXT_PRIMARY_KEY는_NULL_ID_복수행을_허용하므로_init에서_거부한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nullable-text-primary-key.db"
    _create_full_legacy_table(db_path, request_id_decl="TEXT PRIMARY KEY")
    sample = _request()
    values = (
        None,
        sample.org_id,
        sample.requester_id,
        sample.session_id,
        sample.question,
        sample.context_snapshot,
        sample.intent,
        sample.initial_disposition,
        sample.state.kind,
        json.dumps(sample.state.model_dump(mode="json"), ensure_ascii=False),
        1,
        sample.revision,
        sample.created_at.isoformat(),
        sample.updated_at.isoformat(),
    )
    connection = sqlite3.connect(db_path)
    sql = (
        "INSERT INTO question_requests (request_id, org_id, requester_id, session_id, "
        "question, context_snapshot, intent, initial_disposition, state_kind, state_json, "
        "state_schema_version, revision, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    connection.execute(sql, values)
    connection.execute(sql, values)
    connection.commit()
    assert connection.execute(
        "SELECT COUNT(*) FROM question_requests WHERE request_id IS NULL"
    ).fetchone() == (2,)
    connection.close()

    with pytest.raises(QuestionRequestSchemaError, match="nullability"):
        SqliteQuestionRequestStore(db_path)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"org_id_decl": "TEXT"}, "nullability"),
        ({"revision_decl": "TEXT NOT NULL"}, "affinity"),
        ({"version_decl": "INTEGER NOT NULL"}, "DEFAULT 1"),
        ({"version_decl": "INTEGER NOT NULL DEFAULT 2"}, "DEFAULT 1"),
        ({"extra_columns": ", extra_required TEXT NOT NULL"}, "추가 열"),
        (
            {"extra_columns": ", extra_required TEXT NOT NULL DEFAULT 'v1'"},
            "추가 열",
        ),
        (
            {"extra_columns": ", extra_required TEXT NOT NULL DEFAULT NULL"},
            "추가 열",
        ),
        (
            {
                "request_id_decl": "TEXT",
                "extra_columns": ", shard_id TEXT NOT NULL",
                "table_constraint": ", PRIMARY KEY (request_id, shard_id)",
            },
            "단일 PRIMARY KEY",
        ),
    ],
)
def test_legacy_schema의_affinity_nullability_default_extra_PK위반을_거부한다(
    tmp_path: Path,
    kwargs: dict[str, str],
    message: str,
) -> None:
    db_path = tmp_path / f"bad-shape-{message}.db"
    _create_full_legacy_table(db_path, **kwargs)

    with pytest.raises(QuestionRequestSchemaError, match=message):
        SqliteQuestionRequestStore(db_path)


def test_nullable_일반열과_generated열만_forward_compatibility로_허용한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "compatible-extra-columns.db"
    _create_full_legacy_table(
        db_path,
        extra_columns=(
            ", optional_future TEXT, computed_future TEXT "
            "GENERATED ALWAYS AS (org_id || '-future') VIRTUAL"
        ),
    )

    store = SqliteQuestionRequestStore(db_path)

    request = _request()
    assert store.create(request) == request
    assert store.get(request.request_id) == request
    store.close()


def test_unknown_nonunique_index는_허용하지만_unknown_UNIQUE_index는_거부한다(
    tmp_path: Path,
) -> None:
    allowed_path = tmp_path / "allowed-nonunique-index.db"
    allowed = SqliteQuestionRequestStore(allowed_path)
    allowed.close()
    connection = sqlite3.connect(allowed_path)
    connection.execute("CREATE INDEX legacy_nonunique_org ON question_requests(org_id)")
    connection.commit()
    connection.close()
    reopened = SqliteQuestionRequestStore(allowed_path)
    reopened.create(_request("요청-index-a"))
    reopened.create(_request("요청-index-b"))
    reopened.close()

    rejected_path = tmp_path / "rejected-unique-index.db"
    rejected = SqliteQuestionRequestStore(rejected_path)
    rejected.close()
    connection = sqlite3.connect(rejected_path)
    connection.execute("CREATE UNIQUE INDEX legacy_unique_org ON question_requests(org_id)")
    connection.commit()
    connection.close()

    with pytest.raises(QuestionRequestSchemaError, match="UNIQUE"):
        SqliteQuestionRequestStore(rejected_path)


def test_store_open뒤_추가된_UNIQUE가_insert를_막아도_duplicate_ID로_오분류하지_않는다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "late-unique-index.db"
    store = SqliteQuestionRequestStore(db_path)
    store.create(_request("요청-first"))
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE UNIQUE INDEX late_unique_org ON question_requests(org_id)")
    connection.commit()
    connection.close()

    with pytest.raises(QuestionRequestSchemaError, match="UNIQUE"):
        store.create(_request("요청-second"))

    assert store.get("요청-second") is None
    store.close()


def test_question_requests_trigger와_foreign_key는_startup에서_거부한다(
    tmp_path: Path,
) -> None:
    trigger_path = tmp_path / "trigger.db"
    store = SqliteQuestionRequestStore(trigger_path)
    store.close()
    connection = sqlite3.connect(trigger_path)
    connection.executescript(
        """
        CREATE TRIGGER mutate_question_request
        BEFORE INSERT ON question_requests
        BEGIN
            SELECT RAISE(ABORT, 'mutated');
        END;
        """
    )
    connection.commit()
    connection.close()
    with pytest.raises(QuestionRequestSchemaError, match="trigger"):
        SqliteQuestionRequestStore(trigger_path)

    foreign_key_path = tmp_path / "foreign-key.db"
    _create_full_legacy_table(
        foreign_key_path,
        table_constraint=", FOREIGN KEY (org_id) REFERENCES organizations(id)",
    )
    with pytest.raises(QuestionRequestSchemaError, match="foreign key"):
        SqliteQuestionRequestStore(foreign_key_path)


def test_extra_CHECK_constraint는_nullable_extra를_강제할수있어_startup에서_거부한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "extra-check.db"
    _create_full_legacy_table(
        db_path,
        table_constraint=", CHECK (question <> '육아휴직 뒤 연차는 어떻게 계산하나요?')",
    )
    with pytest.raises(QuestionRequestSchemaError, match="CHECK"):
        SqliteQuestionRequestStore(db_path)


def test_request_id_NOCASE_PK는_exact_ID를_깨므로_startup에서_거부한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nocase-primary-key.db"
    _create_full_legacy_table(
        db_path,
        request_id_decl="TEXT COLLATE NOCASE PRIMARY KEY NOT NULL",
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    sqlite_store_module._insert_question_request_no_commit(  # pyright: ignore[reportPrivateUsage]
        connection,
        _request("ID"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_store_module._insert_question_request_no_commit(  # pyright: ignore[reportPrivateUsage]
            connection,
            _request("id"),
        )
    connection.rollback()
    connection.close()

    with pytest.raises(QuestionRequestSchemaError, match="BINARY"):
        SqliteQuestionRequestStore(db_path)


@pytest.mark.parametrize("policy", ["IGNORE", "REPLACE"])
def test_legacy_PK_conflict_policy는_INSERT_OR_ABORT가_무시하고_duplicate를_보존한다(
    tmp_path: Path,
    policy: str,
) -> None:
    db_path = tmp_path / f"conflict-{policy}.db"
    _create_full_legacy_table(
        db_path,
        request_id_decl=f"TEXT PRIMARY KEY ON CONFLICT {policy} NOT NULL",
    )
    store = SqliteQuestionRequestStore(db_path)
    original = _request()
    replacement = QuestionRequest.receive(
        org_id=original.org_id,
        requester_id=original.requester_id,
        question="덮어쓰면 안 되는 질문",
        request_id_factory=lambda: original.request_id,
        clock=lambda: original.created_at,
        due_at=_DUE,
    )
    store.create(original)

    with pytest.raises(DuplicateQuestionRequestError):
        store.create(replacement)

    assert store.get(original.request_id) == original
    store.close()


@pytest.mark.parametrize(
    "replacement_sql",
    [
        "CREATE INDEX idx_question_requests_state_created_id "
        "ON question_requests(created_at, state_kind, request_id)",
        "CREATE UNIQUE INDEX idx_question_requests_state_created_id "
        "ON question_requests(state_kind, created_at, request_id)",
        "CREATE INDEX idx_question_requests_state_created_id "
        "ON question_requests(state_kind, created_at, request_id) "
        "WHERE state_kind = 'received'",
        "CREATE INDEX idx_question_requests_state_created_id "
        "ON question_requests(state_kind, created_at DESC, request_id)",
        "CREATE INDEX idx_question_requests_state_created_id "
        "ON question_requests(state_kind COLLATE NOCASE, created_at, request_id)",
    ],
)
def test_expected_index의_순서_unique_partial_shape가_다르면_init에서_거부한다(
    tmp_path: Path,
    replacement_sql: str,
) -> None:
    db_path = tmp_path / "bad-index.db"
    initial = SqliteQuestionRequestStore(db_path)
    initial.close()
    connection = sqlite3.connect(db_path)
    connection.execute("DROP INDEX idx_question_requests_state_created_id")
    connection.execute(replacement_sql)
    connection.commit()
    connection.close()

    with pytest.raises(QuestionRequestSchemaError, match="index"):
        SqliteQuestionRequestStore(db_path)


def test_create_get_missing_duplicate계약은_InMemory와_같다(tmp_path: Path) -> None:
    store = SqliteQuestionRequestStore(tmp_path / "question.db")
    request = _request()

    assert store.create(request) == request
    assert store.get(request.request_id) == request
    assert store.get("없는-요청") is None
    with pytest.raises(DuplicateQuestionRequestError):
        store.create(request)
    store.close()


def test_create는_Received_revision0_접수원형아닌_중간상태를_거부한다(
    tmp_path: Path,
) -> None:
    store = SqliteQuestionRequestStore(tmp_path / "ingress.db")
    received = _request()
    forged = received.record_initial_routing(
        intent="휴가 정책",
        disposition="routed",
        target=ReadyToDispatch(
            route=_route(),
            attempt=1,
            trigger_key="위조",
            handling=_handling("system", "위조"),
        ),
        clock=lambda: _T1,
    )

    with pytest.raises(InvalidNewQuestionRequestError):
        store.create(forged)

    assert store.get(forged.request_id) is None
    store.close()


def test_CAS는_exact_current와_id_revision_SQL조건을_모두_요구한다(tmp_path: Path) -> None:
    store = SqliteQuestionRequestStore(tmp_path / "question.db")
    current = _request()
    updated = current.record_initial_routing(
        intent="휴가 정책",
        disposition="routed",
        target=ReadyToDispatch(
            route=_route(),
            attempt=1,
            trigger_key="최초",
            handling=_handling("system", "최초"),
        ),
        clock=lambda: _T1,
    )
    store.create(current)

    forged_payload = current.model_dump()
    forged_payload["question"] = "같은 revision의 위조 질문"
    forged = QuestionRequest.model_validate(forged_payload)
    forged_updated = forged.record_initial_routing(
        intent="휴가 정책",
        disposition="routed",
        target=ReadyToDispatch(
            route=_route(),
            attempt=1,
            trigger_key="최초",
            handling=_handling("system", "최초"),
        ),
        clock=lambda: _T1,
    )
    assert store.compare_and_set(current.request_id, 0, forged, forged_updated) is False
    assert store.get(current.request_id) == current

    assert store.compare_and_set(current.request_id, 0, current, updated) is True
    assert store.compare_and_set(current.request_id, 0, current, updated) is False
    assert store.get(current.request_id) == updated
    store.close()


def test_CAS_SQL은_immutable_envelope열을_UPDATE목록에_넣지_않는다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "immutable-trigger.db"
    store = SqliteQuestionRequestStore(db_path)
    current = _request()
    store.create(current)
    updated = current.record_initial_routing(
        intent="휴가 정책",
        disposition="routed",
        target=ReadyToDispatch(
            route=_route(),
            attempt=1,
            trigger_key="최초",
            handling=_handling("system", "최초"),
        ),
        clock=lambda: _T1,
    )
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TRIGGER reject_question_request_envelope_update
        BEFORE UPDATE OF org_id, requester_id, session_id, question,
                         context_snapshot, created_at
        ON question_requests
        BEGIN
            SELECT RAISE(ABORT, 'immutable envelope was in UPDATE');
        END;
        """
    )
    connection.commit()
    connection.close()

    assert store.compare_and_set(current.request_id, 0, current, updated) is True
    assert store.get(current.request_id) == updated
    store.close()


def test_module_private_insert_select_CAS_helper는_commit하지_않아_UoW가_rollback한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "uow-helper.db"
    schema_owner = SqliteQuestionRequestStore(db_path)
    schema_owner.close()
    current = _request()
    updated = current.record_initial_routing(
        intent="휴가 정책",
        disposition="routed",
        target=ReadyToDispatch(
            route=_route(),
            attempt=1,
            trigger_key="최초",
            handling=_handling("system", "최초"),
        ),
        clock=lambda: _T1,
    )

    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    writer.execute("BEGIN IMMEDIATE")
    sqlite_store_module._insert_question_request_no_commit(  # pyright: ignore[reportPrivateUsage]
        writer,
        current,
    )
    assert (
        sqlite_store_module._select_question_request_no_commit(  # pyright: ignore[reportPrivateUsage]
            writer,
            current.request_id,
        )
        == current
    )
    reader = sqlite3.connect(db_path)
    assert reader.execute("SELECT COUNT(*) FROM question_requests").fetchone() == (0,)
    reader.close()
    writer.rollback()
    writer.close()

    committed = SqliteQuestionRequestStore(db_path)
    committed.create(current)
    committed.close()
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    writer.execute("BEGIN IMMEDIATE")
    assert sqlite_store_module._compare_and_set_question_request_no_commit(  # pyright: ignore[reportPrivateUsage]
        writer,
        current.request_id,
        current.revision,
        current,
        updated,
    )
    assert (
        sqlite_store_module._select_question_request_no_commit(  # pyright: ignore[reportPrivateUsage]
            writer,
            current.request_id,
        )
        == updated
    )
    reader = sqlite3.connect(db_path)
    assert reader.execute(
        "SELECT revision FROM question_requests WHERE request_id = ?",
        (current.request_id,),
    ).fetchone() == (0,)
    reader.close()
    writer.rollback()
    writer.close()

    reopened = SqliteQuestionRequestStore(db_path)
    assert reopened.get(current.request_id) == current
    reopened.close()


def test_PK없는_손상table의_CAS가_두행을_UPDATE하면_error후_rollback한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "multi-row-cas.db"
    _create_full_legacy_table(db_path, request_id_decl="TEXT")
    current = _request()
    updated = current.record_initial_routing(
        intent="휴가 정책",
        disposition="routed",
        target=ReadyToDispatch(
            route=_route(),
            attempt=1,
            trigger_key="중복-CAS",
            handling=_handling("system", "중복-CAS"),
        ),
        clock=lambda: _T1,
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    sqlite_store_module._insert_question_request_no_commit(  # pyright: ignore[reportPrivateUsage]
        connection,
        current,
    )
    sqlite_store_module._insert_question_request_no_commit(  # pyright: ignore[reportPrivateUsage]
        connection,
        current,
    )
    connection.commit()

    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(CorruptQuestionRequestError, match="2개 행"):
        sqlite_store_module._compare_and_set_question_request_no_commit(  # pyright: ignore[reportPrivateUsage]
            connection,
            current.request_id,
            current.revision,
            current,
            updated,
        )
    connection.rollback()

    revisions = connection.execute(
        "SELECT revision FROM question_requests ORDER BY rowid"
    ).fetchall()
    assert [row["revision"] for row in revisions] == [0, 0]
    connection.close()


def test_독립_connection_CAS_경쟁자는_정확히_하나만_이긴다(tmp_path: Path) -> None:
    db_path = tmp_path / "race.db"
    first = SqliteQuestionRequestStore(db_path)
    second = SqliteQuestionRequestStore(db_path)
    current = _request()
    first.create(current)
    barrier = threading.Barrier(2)
    candidates = [
        current.record_initial_routing(
            intent="휴가 정책",
            disposition="routed",
            target=ReadyToDispatch(
                route=_route(f"담당-{index}"),
                attempt=1,
                trigger_key=f"경쟁-{index}",
                handling=_handling("system", f"경쟁-{index}"),
            ),
            clock=lambda: _T1,
        )
        for index in range(2)
    ]

    def compete(index: int) -> bool:
        barrier.wait(timeout=2)
        store = first if index == 0 else second
        return store.compare_and_set(current.request_id, 0, current, candidates[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, range(2)))

    assert sum(results) == 1
    assert first.get(current.request_id) == candidates[results.index(True)]
    first.close()
    second.close()


def test_nonterminal은_terminal제외_created_id순이며_다른_SQLite표와_공존한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "coexist.db"
    requests = SqliteQuestionRequestStore(db_path)
    sessions = SqliteSessionStore(db_path)
    sessions.open_or_get("사용자-김")
    _seed_state(requests, "요청-z", "answered")
    _seed_state(requests, "요청-c", "declined")
    _seed_state(requests, "요청-f", "failed")
    _seed_state(requests, "요청-b", "awaiting_conflict", created_at=_T1)
    _seed_state(requests, "요청-aa", "received")
    _seed_state(requests, "요청-a", "received")

    snapshot = requests.nonterminal()

    assert [request.request_id for request in snapshot] == ["요청-a", "요청-aa", "요청-b"]
    snapshot.clear()
    assert len(requests.nonterminal()) == 3
    assert sessions.active_for_user("사용자-김") is not None
    requests.close()
    sessions.close()


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("state_schema_version", 2, "version"),
        ("state_kind", "answered", "state_kind"),
        ("state_json", "{깨진-json", "JSON"),
        ("state_json", '{"attempt":0,"kind":"ready_to_dispatch"}', "state"),
        ("question", sqlite3.Binary(b"not-text"), "row"),
        ("created_at", "2026-07-12T09:00:00", "row"),
    ],
)
def test_손상행은_get과_nonterminal에서_fail_closed하고_절대_skip하지_않는다(
    tmp_path: Path,
    column: str,
    value: object,
    message: str,
) -> None:
    db_path = tmp_path / f"corrupt-{column}.db"
    store = SqliteQuestionRequestStore(db_path)
    store.create(_request())
    connection = sqlite3.connect(db_path)
    connection.execute(
        f"UPDATE question_requests SET {column} = ? WHERE request_id = ?",
        (value, "요청-가"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorruptQuestionRequestError, match=message):
        store.get("요청-가")
    with pytest.raises(CorruptQuestionRequestError, match=message):
        store.nonterminal()
    store.close()


def test_persisted_Received원형과_nonReceived최소revision_위반은_fail_closed한다(
    tmp_path: Path,
) -> None:
    received_revision_path = tmp_path / "received-revision.db"
    received_store = SqliteQuestionRequestStore(received_revision_path)
    received_store.create(_request("요청-revision"))
    connection = sqlite3.connect(received_revision_path)
    connection.execute(
        "UPDATE question_requests SET revision = 1 WHERE request_id = ?",
        ("요청-revision",),
    )
    connection.commit()
    connection.close()
    with pytest.raises(CorruptQuestionRequestError, match="접수 원형"):
        received_store.get("요청-revision")
    with pytest.raises(CorruptQuestionRequestError, match="접수 원형"):
        received_store.nonterminal()
    received_store.close()

    received_ref_path = tmp_path / "received-ref.db"
    ref_store = SqliteQuestionRequestStore(received_ref_path)
    received = _request("요청-ref")
    ref_store.create(received)
    state_payload = received.state.model_dump(mode="json")
    handling = state_payload["handling"]
    assert isinstance(handling, dict)
    handling["ref"] = "generic-system-handler"
    connection = sqlite3.connect(received_ref_path)
    connection.execute(
        "UPDATE question_requests SET state_json = ? WHERE request_id = ?",
        (
            json.dumps(
                state_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            received.request_id,
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(CorruptQuestionRequestError, match="접수 원형"):
        ref_store.get(received.request_id)
    with pytest.raises(CorruptQuestionRequestError, match="접수 원형"):
        ref_store.nonterminal()
    ref_store.close()

    ready_revision_path = tmp_path / "ready-revision.db"
    ready_store = SqliteQuestionRequestStore(ready_revision_path)
    ready = _seed_state(ready_store, "요청-ready", "ready_to_dispatch")
    connection = sqlite3.connect(ready_revision_path)
    connection.execute(
        "UPDATE question_requests SET revision = 0 WHERE request_id = ?",
        (ready.request_id,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(CorruptQuestionRequestError, match="revision은 1 이상"):
        ready_store.get(ready.request_id)
    with pytest.raises(CorruptQuestionRequestError, match="revision은 1 이상"):
        ready_store.nonterminal()
    ready_store.close()


def test_미지_state_kind_variant는_fail_closed하고_nonterminal에서도_skip하지_않는다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "unknown-kind.db"
    store = SqliteQuestionRequestStore(db_path)
    store.create(_request())
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE question_requests SET state_kind = ?, state_json = ? WHERE request_id = ?",
        ("미지-상태", '{"kind":"미지-상태"}', "요청-가"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorruptQuestionRequestError, match="state"):
        store.get("요청-가")
    with pytest.raises(CorruptQuestionRequestError, match="state"):
        store.nonterminal()
    store.close()


@pytest.mark.parametrize(
    "state_json",
    [
        (
            '{"kind":"received","kind":"received","handling":'
            '{"kind":"system","ref":"질문","due_at":"2026-07-12T11:00:00+09:00"}}'
        ),
        (
            '{"kind":"received","handling":{"kind":"system","ref":"질문",'
            '"ref":"변조","due_at":"2026-07-12T11:00:00+09:00"}}'
        ),
    ],
)
def test_state_JSON의_top_level과_nested_중복key는_fail_closed한다(
    tmp_path: Path,
    state_json: str,
) -> None:
    db_path = tmp_path / "duplicate-json-key.db"
    store = SqliteQuestionRequestStore(db_path)
    store.create(_request())
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE question_requests SET state_json = ? WHERE request_id = ?",
        (state_json, "요청-가"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorruptQuestionRequestError, match="중복 JSON key"):
        store.get("요청-가")
    with pytest.raises(CorruptQuestionRequestError, match="중복 JSON key"):
        store.nonterminal()
    store.close()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_state_JSON의_비표준_NaN_Infinity상수는_fail_closed한다(
    tmp_path: Path,
    constant: str,
) -> None:
    db_path = tmp_path / f"nonstandard-{constant}.db"
    store = SqliteQuestionRequestStore(db_path)
    store.create(_request())
    state_json = (
        '{"kind":"received","handling":{"kind":"system","ref":"질문",'
        '"due_at":"2026-07-12T11:00:00+09:00"},"extra":'
        f"{constant}}}"
    )
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE question_requests SET state_json = ? WHERE request_id = ?",
        (state_json, "요청-가"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorruptQuestionRequestError, match="비표준 JSON 상수"):
        store.get("요청-가")
    with pytest.raises(CorruptQuestionRequestError, match="비표준 JSON 상수"):
        store.nonterminal()
    store.close()


def test_valid_noncanonical_state_JSON은_decode하고_다음쓰기에서_canonical화한다(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "noncanonical.db"
    store = SqliteQuestionRequestStore(db_path)
    current = _request()
    store.create(current)
    connection = sqlite3.connect(db_path)
    noncanonical = json.dumps(
        current.state.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )
    connection.execute(
        "UPDATE question_requests SET state_json = ? WHERE request_id = ?",
        (noncanonical, "요청-가"),
    )
    connection.commit()
    connection.close()

    assert store.get(current.request_id) == current
    updated = current.record_initial_routing(
        intent="휴가 정책",
        disposition="routed",
        target=ReadyToDispatch(
            route=_route(),
            attempt=1,
            trigger_key="최초",
            handling=_handling("system", "최초"),
        ),
        clock=lambda: _T1,
    )
    assert store.compare_and_set(current.request_id, 0, current, updated) is True
    connection = sqlite3.connect(db_path)
    row = connection.execute(
        "SELECT state_json FROM question_requests WHERE request_id = ?",
        (current.request_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == json.dumps(
        updated.state.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    connection.close()
    store.close()
