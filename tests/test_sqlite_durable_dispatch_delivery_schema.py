"""P17.9 S5.1 durable dispatch delivery schema capability 테스트(ADR 0042 §9 ①).

S5.1은 schema/reconciliation 경계만 연다 — lease claim/renew, 실 delivery,
답 수신 UoW는 S5.2 이후다. 여기서는 세 소유 테이블(lease·delivery attempt·
answer receipt)의 DDL·manifest·row 정합만 검증한다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_dispatch_delivery import (
    SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,
    SQLITE_DURABLE_DISPATCH_DELIVERY_MIGRATION_FAULT_POINTS,
    SQLITE_DURABLE_DISPATCH_DELIVERY_SCHEMA_VERSION,
    SqliteDurableDispatchDeliverySchemaError,
    migrate_sqlite_durable_dispatch_delivery_schema,
    open_sqlite_durable_dispatch_delivery_connection,
    reconcile_sqlite_durable_dispatch_delivery_schema,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    SqliteDurableLinkedAggregatesSchemaError,
    migrate_sqlite_durable_linked_aggregates_schema,
    reconcile_sqlite_durable_linked_aggregates_schema,
)

_T0 = "2026-01-01T00:00:00.000000+00:00"
_T1 = "2026-01-01T00:05:00.000000+00:00"
_LEASES_TABLE = "durable_dispatch_leases"
_ATTEMPTS_TABLE = "durable_dispatch_delivery_attempts"
_ANSWERS_TABLE = "durable_dispatch_answer_receipts"
_OWNED_TABLES = (_LEASES_TABLE, _ATTEMPTS_TABLE, _ANSWERS_TABLE)


def _ref(kind: str, label: str) -> str:
    return f"{kind}:{hashlib.sha256(label.encode()).hexdigest()}"


def _parent(path: Path) -> None:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)


def _request(connection: sqlite3.Connection, request_id: str, org_id: str) -> None:
    connection.execute(
        "INSERT INTO question_requests(request_id,org_id,requester_id,session_id,question,"
        "context_snapshot,intent,initial_disposition,state_kind,state_json,"
        "state_schema_version,revision,created_at,updated_at) VALUES(?,?, 'user',NULL,'q',"
        "NULL,NULL,NULL,'received','{}',1,0,'2026-01-01T00:00:00+00:00',"
        "'2026-01-01T00:00:00+00:00')",
        (request_id, org_id),
    )


def _ticket(
    connection: sqlite3.Connection, *, ticket_id: str, org_id: str, request_id: str
) -> None:
    connection.execute(
        "INSERT INTO durable_linked_work_tickets VALUES(?,?,?,?,?,?,?,?,?)",
        (
            ticket_id,
            org_id,
            request_id,
            1,
            0,
            "a" * 64,
            _ref("subject", f"owner-of-{ticket_id}"),
            "pending",
            "2026-01-01T00:00:00+00:00",
        ),
    )


def _current_value(
    path: Path, *, table: str, pk_column: str, pk_value: str, column: str
) -> object:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            f"SELECT {column} FROM {table} WHERE {pk_column}=?", (pk_value,)
        ).fetchone()[0]
    finally:
        connection.close()


def _setup(tmp_path: Path) -> tuple[Path, str, str, str]:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    org_id, request_id, ticket_id = _ref("org", "o1"), _ref("request", "r1"), _ref("ticket", "t1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_id, org_id)
    _ticket(connection, ticket_id=ticket_id, org_id=org_id, request_id=request_id)
    connection.commit()
    connection.close()
    return path, org_id, request_id, ticket_id


def _lease(
    connection: sqlite3.Connection,
    *,
    ticket_id: str,
    org_id: str,
    request_id: str,
    epoch: int = 1,
    state: str = "leased",
    holder: str = "subject:" + "1" * 64,
    expires_at: str = _T1,
    acquired_at: str = _T0,
) -> None:
    connection.execute(
        "INSERT INTO durable_dispatch_leases VALUES(?,?,?,?,?,?,?,?)",
        (ticket_id, org_id, request_id, epoch, holder, state, expires_at, acquired_at),
    )


def _attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    ticket_id: str,
    org_id: str,
    request_id: str,
    epoch: int = 1,
    holder: str = "subject:" + "1" * 64,
    outcome: str = "delivered",
    reason_code: str = "ok",
    target: str = "subject:" + "2" * 64,
    created_at: str = _T0,
) -> None:
    connection.execute(
        "INSERT INTO durable_dispatch_delivery_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            org_id,
            ticket_id,
            request_id,
            epoch,
            holder,
            outcome,
            reason_code,
            target,
            created_at,
        ),
    )


def _answer(
    connection: sqlite3.Connection,
    *,
    receipt_id: str,
    ticket_id: str,
    org_id: str,
    request_id: str,
    command_digest: str,
    principal: str = "subject:" + "3" * 64,
    action: str = "work_ticket.complete",
    revision: int = 1,
    answer_sha256: str = "b" * 64,
    created_at: str = _T0,
) -> None:
    connection.execute(
        "INSERT INTO durable_dispatch_answer_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_id,
            org_id,
            ticket_id,
            request_id,
            command_digest,
            principal,
            action,
            revision,
            answer_sha256,
            created_at,
        ),
    )


# 1. migrate -> validate green; manifest_json/sha·catalog·FK exact.


def test_migrates_after_linked_aggregates_parent_with_typed_only_tables(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    connection = open_sqlite_durable_dispatch_delivery_connection(path)
    try:
        row = connection.execute(
            "SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests "
            "WHERE component_id=?",
            (SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,),
        ).fetchone()
        assert row is not None
        assert row[0] == SQLITE_DURABLE_DISPATCH_DELIVERY_SCHEMA_VERSION
        assert row[2] == hashlib.sha256(row[1].encode("utf-8")).hexdigest()
        columns = {
            col[1].casefold()
            for table in (
                "durable_dispatch_leases",
                "durable_dispatch_delivery_attempts",
                "durable_dispatch_answer_receipts",
            )
            for col in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        }
        # exact-match만으로는 answer_body·question_text 같은 합성 컬럼명을 못 잡는다
        # (그런 컬럼명 자체가 forbidden 정확 문자열이 아니므로) — 부분 문자열로 강화.
        # "answer"는 제외한다 — answer_sha256(typed digest)은 정당한 컬럼이다.
        forbidden_substrings = (
            "question",
            "rationale",
            "secret",
            "token",
            "control_handle",
            "claim",
            "_body",
            "_text",
            "payload",
        )
        for column in columns:
            for forbidden in forbidden_substrings:
                assert forbidden not in column, (column, forbidden)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_s4_1_parent_remains_capable_after_s5_1_migrate(tmp_path: Path) -> None:
    # S5.1은 S4.1 DDL을 바꾸지 않는다(ADR 0042 §9 ①) — migrate 후에도 S4.1 자기
    # capability는 그대로 capable이어야 한다.
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    assert reconcile_sqlite_durable_linked_aggregates_schema(path).capable


# 2. 두 번째 migrate 멱등(no-op·행 1개 유지).


def test_second_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    connection = sqlite3.connect(path)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM schema_component_manifests WHERE component_id=?",
            (SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,),
        ).fetchone()[0]
        assert count == 1
    finally:
        connection.close()


# 3. 5개 fault point 각각 -> 소유 테이블 0개·manifest 0행(부분 스키마 0).


@pytest.mark.parametrize("point", SQLITE_DURABLE_DISPATCH_DELIVERY_MIGRATION_FAULT_POINTS)
def test_fault_atomic_migration_leaves_no_owned_schema(tmp_path: Path, point: str) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_durable_dispatch_delivery_schema(
            path,
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_dispatch_%'"
            ).fetchall()
            == []
        )
        assert (
            connection.execute(
                "SELECT 1 FROM schema_component_manifests WHERE component_id=?",
                (SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,),
            ).fetchone()
            is None
        )
    finally:
        connection.close()


# 4. manifest 없이 테이블만 있는 DB -> migrate 거부.


def test_manifest_missing_but_owned_tables_present_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "DELETE FROM schema_component_manifests WHERE component_id=?",
        (SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        migrate_sqlite_durable_dispatch_delivery_schema(path)
    # 거부는 그 자리에서 멈출 뿐 복구하지 않는다 — 소유 테이블 3개는 그대로
    # 잔류(DROP되지 않음)하고 manifest는 계속 0행(재삽입되지 않음).
    connection = sqlite3.connect(path)
    try:
        for table in _OWNED_TABLES:
            assert (
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
                ).fetchone()
                is not None
            ), table
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_component_manifests WHERE component_id=?",
                (SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_manifest_self_consistent_but_wrong_pair_is_rejected(tmp_path: Path) -> None:
    # manifest_json·manifest_sha256을 함께(자기정합되게) 위조해도 canonical
    # _expected_manifest()과의 대조가 잡아야 한다 — sha256 자기정합만 확인하는
    # mutant는 이 red를 죽이지 못한다.
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    forged_json = json.dumps(
        {"component_id": "forged", "component_schema_version": 1, "tables": []},
        sort_keys=True,
        separators=(",", ":"),
    )
    forged_sha256 = hashlib.sha256(forged_json.encode("utf-8")).hexdigest()
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE schema_component_manifests SET manifest_json=?,manifest_sha256=? WHERE component_id=?",
        (forged_json, forged_sha256, SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID),
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path)


# 5. S4.1 미설치 DB에서 migrate -> parent 부재로 거부.


def test_no_linked_aggregates_parent_no_capability(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        migrate_sqlite_durable_dispatch_delivery_schema(path)


def test_completion_only_without_linked_aggregates_parent_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        migrate_sqlite_durable_dispatch_delivery_schema(path)


# 6. 위조 row red 각 1건.


def test_lease_holder_rejects_non_typed_reference(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _lease(connection, ticket_id=ticket_id, org_id=org_id, request_id=request_id, holder="plain-id")
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)
    # validate는 복구하지 않는다 — 위조 값이 그대로 남아 있어야 한다.
    assert (
        _current_value(
            path, table=_LEASES_TABLE, pk_column="ticket_id", pk_value=ticket_id, column="holder_ref"
        )
        == "plain-id"
    )


def test_lease_state_rejects_disallowed_enum(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _lease(connection, ticket_id=ticket_id, org_id=org_id, request_id=request_id, state="expired")
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)
    assert (
        _current_value(
            path, table=_LEASES_TABLE, pk_column="ticket_id", pk_value=ticket_id, column="state"
        )
        == "expired"
    )


def test_lease_epoch_zero_is_rejected(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _lease(connection, ticket_id=ticket_id, org_id=org_id, request_id=request_id, epoch=0)
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)
    assert (
        _current_value(
            path, table=_LEASES_TABLE, pk_column="ticket_id", pk_value=ticket_id, column="lease_epoch"
        )
        == 0
    )


def test_lease_timestamp_rejects_variable_offset(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _lease(
        connection,
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        acquired_at="2026-01-01T00:00:00.000000+09:00",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)
    assert (
        _current_value(
            path, table=_LEASES_TABLE, pk_column="ticket_id", pk_value=ticket_id, column="acquired_at"
        )
        == "2026-01-01T00:00:00.000000+09:00"
    )


def test_lease_timestamp_rejects_second_precision(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _lease(
        connection,
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        expires_at="2026-01-01T00:05:00+00:00",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)
    assert (
        _current_value(
            path, table=_LEASES_TABLE, pk_column="ticket_id", pk_value=ticket_id, column="expires_at"
        )
        == "2026-01-01T00:05:00+00:00"
    )


def test_attempt_delivered_outcome_requires_ok_reason(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    attempt_id = _ref("receipt", "attempt-1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _attempt(
        connection,
        attempt_id=attempt_id,
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        outcome="delivered",
        reason_code="channel_error",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)
    assert (
        _current_value(
            path,
            table=_ATTEMPTS_TABLE,
            pk_column="attempt_id",
            pk_value=attempt_id,
            column="reason_code",
        )
        == "channel_error"
    )


def test_lease_without_matching_ticket_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    org_id, request_id = _ref("org", "o1"), _ref("request", "r1")
    orphan_ticket_id = _ref("ticket", "never-created")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_id, org_id)
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    _lease(connection, ticket_id=orphan_ticket_id, org_id=org_id, request_id=request_id)
    connection.commit()
    connection.close()
    report = reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id)
    assert not report.capable
    assert report.detail == "dispatch delivery에는 capable linked aggregate parent가 필요합니다."
    with pytest.raises(
        SqliteDurableDispatchDeliverySchemaError,
        match="capable linked aggregate parent가 필요합니다",
    ) as excinfo:
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)
    # 이 orphan은 우리 모듈의 parent-ticket lineage 체크가 아니라, S4.1을 감싼
    # Completion의 전역 PRAGMA foreign_key_check가 잡는다(Completion -> S4.1 ->
    # S5.1 순으로 wrap) — 어느 층이 실제로 fail-closed하는지 명시한다.
    cause = excinfo.value.__cause__
    assert isinstance(cause, SqliteDurableLinkedAggregatesSchemaError)
    assert "Question Completion parent" in str(cause)
    # 여전히 복구되지 않는다 — orphan lease 행 자체가 그대로 남아 있다.
    assert (
        _current_value(
            path, table=_LEASES_TABLE, pk_column="ticket_id", pk_value=orphan_ticket_id, column="ticket_id"
        )
        == orphan_ticket_id
    )


def test_lease_org_mismatch_with_real_ticket_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    org_a, org_b = _ref("org", "a"), _ref("org", "b")
    request_a, request_b = _ref("request", "a"), _ref("request", "b")
    ticket_a = _ref("ticket", "a")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_a, org_a)
    _request(connection, request_b, org_b)
    _ticket(connection, ticket_id=ticket_a, org_id=org_a, request_id=request_a)
    connection.commit()
    # lease references the real ticket_a/request_a FK targets but claims org_b lineage.
    _lease(connection, ticket_id=ticket_a, org_id=org_b, request_id=request_a)
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_b).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_b)
    assert (
        _current_value(
            path, table=_LEASES_TABLE, pk_column="ticket_id", pk_value=ticket_a, column="org_id"
        )
        == org_b
    )


def test_attempt_epoch_ahead_of_current_lease_epoch_is_rejected(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    attempt_id = _ref("receipt", "attempt-1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _lease(connection, ticket_id=ticket_id, org_id=org_id, request_id=request_id, epoch=1)
    _attempt(
        connection,
        attempt_id=attempt_id,
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        epoch=2,
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)
    assert (
        _current_value(
            path,
            table=_ATTEMPTS_TABLE,
            pk_column="attempt_id",
            pk_value=attempt_id,
            column="lease_epoch",
        )
        == 2
    )


# 7. UNIQUE 3종 위반.


def test_unique_ticket_lease_epoch_rejects_duplicate_attempt(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _attempt(
        connection,
        attempt_id=_ref("receipt", "attempt-1"),
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        epoch=1,
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(
            connection,
            attempt_id=_ref("receipt", "attempt-2"),
            ticket_id=ticket_id,
            org_id=org_id,
            request_id=request_id,
            epoch=1,
        )
    connection.close()


def test_unique_ticket_id_rejects_second_answer_receipt(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _answer(
        connection,
        receipt_id=_ref("receipt", "answer-1"),
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        command_digest="c" * 64,
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _answer(
            connection,
            receipt_id=_ref("receipt", "answer-2"),
            ticket_id=ticket_id,
            org_id=org_id,
            request_id=request_id,
            command_digest="d" * 64,
        )
    connection.close()


def test_unique_command_digest_rejects_duplicate_answer_receipt(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    org_id = _ref("org", "o1")
    request_1, request_2 = _ref("request", "r1"), _ref("request", "r2")
    ticket_1, ticket_2 = _ref("ticket", "t1"), _ref("ticket", "t2")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_1, org_id)
    _request(connection, request_2, org_id)
    _ticket(connection, ticket_id=ticket_1, org_id=org_id, request_id=request_1)
    _ticket(connection, ticket_id=ticket_2, org_id=org_id, request_id=request_2)
    connection.commit()
    _answer(
        connection,
        receipt_id=_ref("receipt", "answer-1"),
        ticket_id=ticket_1,
        org_id=org_id,
        request_id=request_1,
        command_digest="e" * 64,
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _answer(
            connection,
            receipt_id=_ref("receipt", "answer-2"),
            ticket_id=ticket_2,
            org_id=org_id,
            request_id=request_2,
            command_digest="e" * 64,
        )
    connection.close()


# 8. FK RESTRICT: ticket 행 DELETE 시도 거부.


def test_fk_restrict_blocks_ticket_delete_while_lease_references_it(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _lease(connection, ticket_id=ticket_id, org_id=org_id, request_id=request_id)
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM durable_linked_work_tickets WHERE ticket_id=?", (ticket_id,)
        )
    connection.close()


# 9. org 필터 reconcile / 전역 catalog 손상.


def test_row_reconciliation_is_org_scoped_but_other_org_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    org_a, org_b = _ref("org", "a"), _ref("org", "b")
    request_a, request_b = _ref("request", "a"), _ref("request", "b")
    ticket_a, ticket_b = _ref("ticket", "a"), _ref("ticket", "b")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_a, org_a)
    _request(connection, request_b, org_b)
    _ticket(connection, ticket_id=ticket_a, org_id=org_a, request_id=request_a)
    _ticket(connection, ticket_id=ticket_b, org_id=org_b, request_id=request_b)
    _lease(connection, ticket_id=ticket_a, org_id=org_a, request_id=request_a)
    connection.commit()
    _lease(connection, ticket_id=ticket_b, org_id=org_b, request_id=request_b, state="not-a-state")
    connection.commit()
    connection.close()
    assert reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_a).capable
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_b).capable


def test_dropped_owned_table_fails_closed_regardless_of_org_scope(tmp_path: Path) -> None:
    path, org_id, _request_id, _ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE durable_dispatch_answer_receipts")
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=_ref("org", "x")).capable
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path)
    assert (
        sqlite3.connect(path)
        .execute("SELECT 1 FROM sqlite_schema WHERE name='durable_dispatch_answer_receipts'")
        .fetchone()
        is None
    )


# 10. open_...은 :memory:/빈 경로 거부.


def test_open_rejects_memory_and_empty_path() -> None:
    # match= 없이는 가드가 죽어도(예: `if False:`) 우연한 OperationalError로
    # 통과할 수 있다 — 실제 fail-closed 가드 메시지를 pin한다.
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError, match="기존 SQLite 파일만"):
        open_sqlite_durable_dispatch_delivery_connection(":memory:")
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError, match="기존 SQLite 파일만"):
        open_sqlite_durable_dispatch_delivery_connection("")


# P1/P2 mutation 사멸 보강 — 리뷰(review-s51) 지적 반영. answer receipt 블록이
# 26개 기존 테스트에서 0회 실행되었다는 프로브 실측(LEASE 16·ATTEMPT 4·ANSWER 0)에
# 대응해, 세 테이블을 한 parametrize 배터리로 유효 seed 행 하나씩을 UPDATE로
# 손상시켜 reconcile/open을 직접 호출한다(S4.1 선례
# test_sqlite_durable_linked_aggregates_schema.py:169-216 판). 손상된 값이
# validate 후에도 그대로 남는지까지 확인해 "복구하지 않는다"를 같이 pin한다.


def _seed_dispatch_rows(tmp_path: Path) -> tuple[Path, str, str, str, str, str]:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    attempt_id = _ref("receipt", "attempt-seed")
    receipt_id = _ref("receipt", "answer-seed")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _lease(connection, ticket_id=ticket_id, org_id=org_id, request_id=request_id)
    _attempt(
        connection, attempt_id=attempt_id, ticket_id=ticket_id, org_id=org_id, request_id=request_id
    )
    _answer(
        connection,
        receipt_id=receipt_id,
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        command_digest="c" * 64,
    )
    connection.commit()
    connection.close()
    return path, org_id, request_id, ticket_id, attempt_id, receipt_id


@pytest.mark.parametrize(
    ("table", "pk_column", "pk_key", "column", "value"),
    (
        # -- lease: P2-1(expires<acquired) + P2-3(ref kind: pure-hex-no-prefix·wrong-kind) --
        (_LEASES_TABLE, "ticket_id", "ticket_id", "expires_at", "2025-12-31T23:59:59.000000+00:00"),
        (_LEASES_TABLE, "ticket_id", "ticket_id", "holder_ref", "1" * 64),
        # org_id/request_id 자리의 wrong-kind는 lineage 대조가 이차 방어로도 잡아
        # 순수 kind 판별을 못 죽인다 — lineage 백업이 없는 holder_ref로 고른다.
        (_LEASES_TABLE, "ticket_id", "ticket_id", "holder_ref", "receipt:" + "2" * 64),
        # -- attempt: P2-2(lease_epoch=0·org 불일치·비-typed target·created_at) --
        # outcome enum은 여기서 못 잡는다 — 단일 컬럼 손상이라 seed의 reason_code="ok"와
        # 짝이 되어 (outcome=="delivered")False != (reason=="ok")True 상관 검사가 먼저
        # raise한다. enum 절 자체는 아래 전용 테스트가 pin한다(reason_code와 대칭).
        (_ATTEMPTS_TABLE, "attempt_id", "attempt_id", "lease_epoch", 0),
        (_ATTEMPTS_TABLE, "attempt_id", "attempt_id", "org_id", _ref("org", "not-the-ticket-org")),
        (_ATTEMPTS_TABLE, "attempt_id", "attempt_id", "target_subject_ref", "not-typed"),
        (_ATTEMPTS_TABLE, "attempt_id", "attempt_id", "created_at", "2026-01-01T00:00:00+00:00"),
        # -- answer receipt: P1(action enum·비-hex digest·revision=0·org 불일치) + sha256 대구 --
        (_ANSWERS_TABLE, "receipt_id", "receipt_id", "action", "work_ticket.reassign"),
        (_ANSWERS_TABLE, "receipt_id", "receipt_id", "command_digest", "z" * 64),
        (_ANSWERS_TABLE, "receipt_id", "receipt_id", "expected_request_revision", 0),
        (_ANSWERS_TABLE, "receipt_id", "receipt_id", "org_id", _ref("org", "not-the-ticket-org")),
        (_ANSWERS_TABLE, "receipt_id", "receipt_id", "answer_sha256", "z" * 64),
        # principal_ref는 lineage 이차 방어가 없는 필드라 ref 판별 자체를 pin한다(P2-3 판 대구).
        (_ANSWERS_TABLE, "receipt_id", "receipt_id", "principal_ref", "1" * 64),
        (_ANSWERS_TABLE, "receipt_id", "receipt_id", "created_at", "2026-01-01T00:00:00+00:00"),
    ),
)
def test_row_scalar_corruption_fails_closed_without_repair(
    tmp_path: Path, table: str, pk_column: str, pk_key: str, column: str, value: object
) -> None:
    path, _org_id, _request_id, ticket_id, attempt_id, receipt_id = _seed_dispatch_rows(tmp_path)
    pk_value = {
        "ticket_id": ticket_id,
        "attempt_id": attempt_id,
        "receipt_id": receipt_id,
    }[pk_key]
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(f"UPDATE {table} SET {column}=? WHERE {pk_column}=?", (value, pk_value))
    connection.commit()
    connection.close()

    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path)

    assert _current_value(path, table=table, pk_column=pk_column, pk_value=pk_value, column=column) == value


def test_attempt_unauthorized_outcome_survives_delivered_ok_correlation_check_alone(
    tmp_path: Path,
) -> None:
    # reason_code 케이스의 대칭 — outcome="queued"와 reason_code="channel_error"를 함께 두면
    # (outcome=="delivered")False == (reason=="ok")False라 상관 검사를 통과한다.
    # outcome enum 검사 자체가 이걸 잡아야 한다.
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    attempt_id = _ref("receipt", "attempt-1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _attempt(
        connection,
        attempt_id=attempt_id,
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        outcome="queued",
        reason_code="channel_error",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)


def test_attempt_unauthorized_reason_survives_delivered_ok_correlation_check_alone(
    tmp_path: Path,
) -> None:
    # (outcome, reason_code) 상관 검사만으로는 undeliverable+임의 문장을 못 잡는다
    # — outcome=="delivered"(False)와 reason_code=="ok"(False)가 우연히 같아
    # 상관 검사를 통과한다. reason_code enum 검사 자체가 이걸 잡아야 한다.
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    attempt_id = _ref("receipt", "attempt-1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _attempt(
        connection,
        attempt_id=attempt_id,
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        outcome="undeliverable",
        reason_code="worker was on vacation",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_delivery_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchDeliverySchemaError):
        open_sqlite_durable_dispatch_delivery_connection(path, org_id=org_id)
