"""P17.9 S5.6a durable dispatch escalation schema capability 테스트(ADR 0066 §5.1).

S5.6a는 schema/reconciliation 경계만 연다 — escalation Unit of Work(system
전이·SLA 재판정)와 처분 Unit of Work는 다음 슬라이스다. 여기서는 두 소유
테이블(FromDispatch manager item · escalation command receipt)의 DDL·
manifest·row 정합, 그리고 ADR 0066의 핵심 불변식인
``UNIQUE(request_id, attempt)``(실행 시도마다 최대 한 번)만 검증한다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_dispatch_escalation import (
    SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID,
    SQLITE_DURABLE_DISPATCH_ESCALATION_MIGRATION_FAULT_POINTS,
    SQLITE_DURABLE_DISPATCH_ESCALATION_SCHEMA_VERSION,
    SqliteDurableDispatchEscalationSchemaError,
    migrate_sqlite_durable_dispatch_escalation_schema,
    open_sqlite_durable_dispatch_escalation_connection,
    reconcile_sqlite_durable_dispatch_escalation_schema,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    SqliteDurableLinkedAggregatesSchemaError,
    migrate_sqlite_durable_linked_aggregates_schema,
    reconcile_sqlite_durable_linked_aggregates_schema,
)

_T0 = "2026-01-01T00:00:00.000000+00:00"
_T1 = "2026-01-01T00:05:00.000000+00:00"
_ITEMS_TABLE = "durable_dispatch_manager_items"
_RECEIPTS_TABLE = "durable_dispatch_escalation_receipts"
_OWNED_TABLES = (_ITEMS_TABLE, _RECEIPTS_TABLE)


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
    connection: sqlite3.Connection, *, ticket_id: str, org_id: str, request_id: str, attempt: int = 1
) -> None:
    connection.execute(
        "INSERT INTO durable_linked_work_tickets VALUES(?,?,?,?,?,?,?,?,?)",
        (
            ticket_id,
            org_id,
            request_id,
            attempt,
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
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    org_id, request_id, ticket_id = _ref("org", "o1"), _ref("request", "r1"), _ref("ticket", "t1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_id, org_id)
    _ticket(connection, ticket_id=ticket_id, org_id=org_id, request_id=request_id)
    connection.commit()
    connection.close()
    return path, org_id, request_id, ticket_id


def _item(
    connection: sqlite3.Connection,
    *,
    manager_item_id: str,
    ticket_id: str,
    org_id: str,
    request_id: str,
    attempt: int = 1,
    awaiting_revision: int = 0,
    route_sha256: str = "a" * 64,
    owner: str = "subject:" + "1" * 64,
    manager: str = "subject:" + "2" * 64,
    status: str = "open",
    observed_due_at: str = _T1,
    created_at: str = _T0,
) -> None:
    connection.execute(
        f"INSERT INTO {_ITEMS_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            manager_item_id,
            org_id,
            request_id,
            ticket_id,
            attempt,
            awaiting_revision,
            route_sha256,
            owner,
            manager,
            status,
            observed_due_at,
            created_at,
        ),
    )


def _receipt(
    connection: sqlite3.Connection,
    *,
    receipt_id: str,
    manager_item_id: str,
    org_id: str,
    request_id: str,
    command_digest: str,
    principal: str = "subject:" + "3" * 64,
    action: str = "work_ticket.escalate",
    revision: int = 1,
    created_at: str = _T0,
) -> None:
    connection.execute(
        f"INSERT INTO {_RECEIPTS_TABLE} VALUES(?,?,?,?,?,?,?,?,?)",
        (
            receipt_id,
            org_id,
            request_id,
            manager_item_id,
            command_digest,
            principal,
            action,
            revision,
            created_at,
        ),
    )


# 1. migrate -> validate green; manifest_json/sha·catalog·FK exact.


def test_migrates_after_linked_aggregates_parent_with_typed_only_tables(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    connection = open_sqlite_durable_dispatch_escalation_connection(path)
    try:
        row = connection.execute(
            "SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests "
            "WHERE component_id=?",
            (SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID,),
        ).fetchone()
        assert row is not None
        assert row[0] == SQLITE_DURABLE_DISPATCH_ESCALATION_SCHEMA_VERSION
        assert row[2] == hashlib.sha256(row[1].encode("utf-8")).hexdigest()
        columns = {
            col[1].casefold()
            for table in _OWNED_TABLES
            for col in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        }
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
            "answer",
        )
        for column in columns:
            for forbidden in forbidden_substrings:
                assert forbidden not in column, (column, forbidden)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_s4_1_parent_remains_capable_after_s5_6a_migrate(tmp_path: Path) -> None:
    # S5.6a는 S4.1 DDL을 바꾸지 않는다(ADR 0066 결정 §1) — migrate 후에도 S4.1
    # 자기 capability는 그대로 capable이어야 한다.
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    assert reconcile_sqlite_durable_linked_aggregates_schema(path).capable


# 2. 두 번째 migrate 멱등(no-op·행 1개 유지).


def test_second_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    connection = sqlite3.connect(path)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM schema_component_manifests WHERE component_id=?",
            (SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID,),
        ).fetchone()[0]
        assert count == 1
    finally:
        connection.close()


# 3. 4개 fault point 각각 -> 소유 테이블 0개·manifest 0행(부분 스키마 0).


@pytest.mark.parametrize("point", SQLITE_DURABLE_DISPATCH_ESCALATION_MIGRATION_FAULT_POINTS)
def test_fault_atomic_migration_leaves_no_owned_schema(tmp_path: Path, point: str) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    with pytest.raises(RuntimeError, match=point):
        migrate_sqlite_durable_dispatch_escalation_schema(
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
                (SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID,),
            ).fetchone()
            is None
        )
    finally:
        connection.close()


# 4. manifest 없이 테이블만 있는 DB -> migrate 거부.


def test_manifest_missing_but_owned_tables_present_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "DELETE FROM schema_component_manifests WHERE component_id=?",
        (SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        migrate_sqlite_durable_dispatch_escalation_schema(path)
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
                (SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_manifest_self_consistent_but_wrong_pair_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    forged_json = json.dumps(
        {"component_id": "forged", "component_schema_version": 1, "tables": []},
        sort_keys=True,
        separators=(",", ":"),
    )
    forged_sha256 = hashlib.sha256(forged_json.encode("utf-8")).hexdigest()
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE schema_component_manifests SET manifest_json=?,manifest_sha256=? WHERE component_id=?",
        (forged_json, forged_sha256, SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID),
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path)


# 5. S4.1 미설치 DB에서 migrate -> parent 부재로 거부.


def test_no_linked_aggregates_parent_no_capability(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        migrate_sqlite_durable_dispatch_escalation_schema(path)


def test_completion_only_without_linked_aggregates_parent_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        migrate_sqlite_durable_dispatch_escalation_schema(path)


# 6. 위조 row red — 대표 케이스(전수는 8번 배터리).


def test_item_owner_subject_rejects_non_typed_reference(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection,
        manager_item_id=_ref("manager", "m1"),
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        owner="plain-id",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_id)


def test_item_status_rejects_disallowed_enum(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    manager_item_id = _ref("manager", "m1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection,
        manager_item_id=manager_item_id,
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        status="escalated",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_id)
    assert (
        _current_value(
            path, table=_ITEMS_TABLE, pk_column="manager_item_id", pk_value=manager_item_id, column="status"
        )
        == "escalated"
    )


def test_item_attempt_zero_is_rejected(tmp_path: Path) -> None:
    # attempt는 domain의 `ge=1`을 그대로 반영한다(question_request.py) — Item은
    # 실제 실행 시도(attempt >= 1)가 timeout됐을 때만 만들어진다.
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    manager_item_id = _ref("manager", "m1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection,
        manager_item_id=manager_item_id,
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        attempt=0,
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_id)
    assert (
        _current_value(
            path, table=_ITEMS_TABLE, pk_column="manager_item_id", pk_value=manager_item_id, column="attempt"
        )
        == 0
    )


def test_item_observed_due_at_rejects_variable_offset(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection,
        manager_item_id=_ref("manager", "m1"),
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        observed_due_at="2026-01-01T00:05:00.000000+09:00",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_id)


def test_item_created_at_rejects_second_precision(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection,
        manager_item_id=_ref("manager", "m1"),
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
        created_at="2026-01-01T00:00:00+00:00",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_id)


def test_receipt_action_rejects_disallowed_enum(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    manager_item_id = _ref("manager", "m1")
    receipt_id = _ref("receipt", "r1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection, manager_item_id=manager_item_id, ticket_id=ticket_id, org_id=org_id, request_id=request_id
    )
    _receipt(
        connection,
        receipt_id=receipt_id,
        manager_item_id=manager_item_id,
        org_id=org_id,
        request_id=request_id,
        command_digest="c" * 64,
        action="work_ticket.complete",
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_id)


def test_receipt_expected_request_revision_zero_is_rejected(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    manager_item_id = _ref("manager", "m1")
    receipt_id = _ref("receipt", "r1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection, manager_item_id=manager_item_id, ticket_id=ticket_id, org_id=org_id, request_id=request_id
    )
    _receipt(
        connection,
        receipt_id=receipt_id,
        manager_item_id=manager_item_id,
        org_id=org_id,
        request_id=request_id,
        command_digest="c" * 64,
        revision=0,
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_id)


# 7. lineage — orphan(FK 대상 부재) vs org mismatch(FK 대상은 실재하나 lineage 다름).


def test_item_without_matching_ticket_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    org_id, request_id = _ref("org", "o1"), _ref("request", "r1")
    orphan_ticket_id = _ref("ticket", "never-created")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_id, org_id)
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    _item(
        connection,
        manager_item_id=_ref("manager", "m1"),
        ticket_id=orphan_ticket_id,
        org_id=org_id,
        request_id=request_id,
    )
    connection.commit()
    connection.close()
    report = reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id)
    assert not report.capable
    assert report.detail == "dispatch escalation에는 capable linked aggregate parent가 필요합니다."
    with pytest.raises(
        SqliteDurableDispatchEscalationSchemaError,
        match="capable linked aggregate parent가 필요합니다",
    ) as excinfo:
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_id)
    # S5.1과 동형: 이 orphan은 우리 모듈의 parent-ticket lineage 체크가 아니라
    # Completion의 전역 PRAGMA foreign_key_check가 (S4.1을 거쳐) 먼저 잡는다.
    cause = excinfo.value.__cause__
    assert isinstance(cause, SqliteDurableLinkedAggregatesSchemaError)
    assert "Question Completion parent" in str(cause)


def test_item_org_mismatch_with_real_ticket_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    org_a, org_b = _ref("org", "a"), _ref("org", "b")
    request_a = _ref("request", "a")
    ticket_a = _ref("ticket", "a")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_a, org_a)
    _ticket(connection, ticket_id=ticket_a, org_id=org_a, request_id=request_a)
    connection.commit()
    # item references the real ticket_a/request_a FK targets but claims org_b lineage.
    _item(
        connection,
        manager_item_id=_ref("manager", "m1"),
        ticket_id=ticket_a,
        org_id=org_b,
        request_id=request_a,
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_b).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_b)


def test_receipt_without_matching_manager_item_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    org_id, request_id = _ref("org", "o1"), _ref("request", "r1")
    orphan_manager_item_id = _ref("manager", "never-created")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_id, org_id)
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    _receipt(
        connection,
        receipt_id=_ref("receipt", "r1"),
        manager_item_id=orphan_manager_item_id,
        org_id=org_id,
        request_id=request_id,
        command_digest="c" * 64,
    )
    connection.commit()
    connection.close()
    report = reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id)
    assert not report.capable
    assert report.detail == "dispatch escalation에는 capable linked aggregate parent가 필요합니다."
    with pytest.raises(
        SqliteDurableDispatchEscalationSchemaError,
        match="capable linked aggregate parent가 필요합니다",
    ) as excinfo:
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_id)
    # 이 FK도 전역 PRAGMA foreign_key_check 범위이므로 같은 층(Completion)이
    # 우리 모듈 자기 catalog/FK 체크보다 먼저 잡는다.
    cause = excinfo.value.__cause__
    assert isinstance(cause, SqliteDurableLinkedAggregatesSchemaError)
    assert "Question Completion parent" in str(cause)


def test_receipt_org_mismatch_with_real_manager_item_is_rejected(tmp_path: Path) -> None:
    path, org_a, request_a, ticket_a = _setup(tmp_path)
    org_b = _ref("org", "b")
    manager_item_id = _ref("manager", "m1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection, manager_item_id=manager_item_id, ticket_id=ticket_a, org_id=org_a, request_id=request_a
    )
    connection.commit()
    # receipt references the real manager_item_id/request_a FK targets but claims org_b lineage.
    _receipt(
        connection,
        receipt_id=_ref("receipt", "r1"),
        manager_item_id=manager_item_id,
        org_id=org_b,
        request_id=request_a,
        command_digest="c" * 64,
    )
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_b).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path, org_id=org_b)


# 8. UNIQUE 3종 — 특히 UNIQUE(request_id, attempt)는 ADR 0066의 핵심(같은
# request의 다른 attempt는 "돼야 한다").


def test_unique_ticket_id_rejects_second_manager_item(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection,
        manager_item_id=_ref("manager", "m1"),
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _item(
            connection,
            manager_item_id=_ref("manager", "m2"),
            ticket_id=ticket_id,
            org_id=org_id,
            request_id=request_id,
        )
    connection.close()


def test_unique_request_attempt_rejects_duplicate_attempt_for_same_request(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_1 = _setup(tmp_path)
    ticket_2 = _ref("ticket", "t2")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _ticket(connection, ticket_id=ticket_2, org_id=org_id, request_id=request_id, attempt=2)
    _item(
        connection,
        manager_item_id=_ref("manager", "m1"),
        ticket_id=ticket_1,
        org_id=org_id,
        request_id=request_id,
        attempt=1,
    )
    connection.commit()
    # 같은 (request_id, attempt=1)로 두 번째 Item을 만들려는 시도는 거부돼야 한다
    # — ticket_id는 다르지만 (request_id, attempt) 짝이 같다.
    with pytest.raises(sqlite3.IntegrityError):
        _item(
            connection,
            manager_item_id=_ref("manager", "m2"),
            ticket_id=ticket_2,
            org_id=org_id,
            request_id=request_id,
            attempt=1,
        )
    connection.close()


def test_unique_request_attempt_allows_second_attempt_for_same_request(tmp_path: Path) -> None:
    # ADR 0066 결정 §1의 핵심 양성 단언 — "실행 시도마다 최대 한 번"이지 "평생
    # 한 번"이 아니다. 같은 request의 서로 다른 attempt는 반드시 허용돼야 한다.
    path, org_id, request_id, ticket_1 = _setup(tmp_path)
    ticket_2 = _ref("ticket", "t2")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _ticket(connection, ticket_id=ticket_2, org_id=org_id, request_id=request_id, attempt=2)
    _item(
        connection,
        manager_item_id=_ref("manager", "m1"),
        ticket_id=ticket_1,
        org_id=org_id,
        request_id=request_id,
        attempt=1,
    )
    connection.commit()
    _item(
        connection,
        manager_item_id=_ref("manager", "m2"),
        ticket_id=ticket_2,
        org_id=org_id,
        request_id=request_id,
        attempt=2,
    )
    connection.commit()
    count = connection.execute(
        f"SELECT COUNT(*) FROM {_ITEMS_TABLE} WHERE request_id=?", (request_id,)
    ).fetchone()[0]
    assert count == 2
    connection.close()


def test_unique_command_digest_rejects_duplicate_escalation_receipt(tmp_path: Path) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
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
    item_1, item_2 = _ref("manager", "m1"), _ref("manager", "m2")
    _item(connection, manager_item_id=item_1, ticket_id=ticket_1, org_id=org_id, request_id=request_1)
    _item(connection, manager_item_id=item_2, ticket_id=ticket_2, org_id=org_id, request_id=request_2)
    connection.commit()
    _receipt(
        connection,
        receipt_id=_ref("receipt", "answer-1"),
        manager_item_id=item_1,
        org_id=org_id,
        request_id=request_1,
        command_digest="e" * 64,
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _receipt(
            connection,
            receipt_id=_ref("receipt", "answer-2"),
            manager_item_id=item_2,
            org_id=org_id,
            request_id=request_2,
            command_digest="e" * 64,
        )
    connection.close()


# 9. FK RESTRICT: 부모 삭제 거부(ticket ← item, item ← receipt).


def test_fk_restrict_blocks_ticket_delete_while_item_references_it(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection,
        manager_item_id=_ref("manager", "m1"),
        ticket_id=ticket_id,
        org_id=org_id,
        request_id=request_id,
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM durable_linked_work_tickets WHERE ticket_id=?", (ticket_id,)
        )
    connection.close()


def test_fk_restrict_blocks_manager_item_delete_while_receipt_references_it(tmp_path: Path) -> None:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    manager_item_id = _ref("manager", "m1")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection, manager_item_id=manager_item_id, ticket_id=ticket_id, org_id=org_id, request_id=request_id
    )
    _receipt(
        connection,
        receipt_id=_ref("receipt", "r1"),
        manager_item_id=manager_item_id,
        org_id=org_id,
        request_id=request_id,
        command_digest="c" * 64,
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"DELETE FROM {_ITEMS_TABLE} WHERE manager_item_id=?", (manager_item_id,)
        )
    connection.close()


# 10. org 필터 reconcile / 전역 catalog 손상.


def test_row_reconciliation_is_org_scoped_but_other_org_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.sqlite"
    _parent(path)
    migrate_sqlite_durable_dispatch_escalation_schema(path)
    org_a, org_b = _ref("org", "a"), _ref("org", "b")
    request_a, request_b = _ref("request", "a"), _ref("request", "b")
    ticket_a, ticket_b = _ref("ticket", "a"), _ref("ticket", "b")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _request(connection, request_a, org_a)
    _request(connection, request_b, org_b)
    _ticket(connection, ticket_id=ticket_a, org_id=org_a, request_id=request_a)
    _ticket(connection, ticket_id=ticket_b, org_id=org_b, request_id=request_b)
    _item(connection, manager_item_id=_ref("manager", "a"), ticket_id=ticket_a, org_id=org_a, request_id=request_a)
    connection.commit()
    _item(
        connection,
        manager_item_id=_ref("manager", "b"),
        ticket_id=ticket_b,
        org_id=org_b,
        request_id=request_b,
        status="not-a-status",
    )
    connection.commit()
    connection.close()
    assert reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_a).capable
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_b).capable


def test_dropped_owned_table_fails_closed_regardless_of_org_scope(tmp_path: Path) -> None:
    path, org_id, _request_id, _ticket_id = _setup(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(f"DROP TABLE {_RECEIPTS_TABLE}")
    connection.commit()
    connection.close()
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=org_id).capable
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path, org_id=_ref("org", "x")).capable
    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path)
    assert (
        sqlite3.connect(path)
        .execute(f"SELECT 1 FROM sqlite_schema WHERE name='{_RECEIPTS_TABLE}'")
        .fetchone()
        is None
    )


# 11. open_...은 :memory:/빈 경로 거부.


def test_open_rejects_memory_and_empty_path() -> None:
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError, match="기존 SQLite 파일만"):
        open_sqlite_durable_dispatch_escalation_connection(":memory:")
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError, match="기존 SQLite 파일만"):
        open_sqlite_durable_dispatch_escalation_connection("")


# 12. mutation 사멸 보강 — S5.1 선례(test_sqlite_durable_dispatch_delivery_schema.py
# 769행)를 따라 두 테이블에 유효 seed 행 하나씩을 UPDATE로 손상시켜
# reconcile/open을 직접 호출한다. 손상된 값이 validate 후에도 그대로 남는지까지
# 확인해 "복구하지 않는다"를 같이 pin한다.


def _seed_escalation_rows(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    path, org_id, request_id, ticket_id = _setup(tmp_path)
    manager_item_id = _ref("manager", "item-seed")
    receipt_id = _ref("receipt", "receipt-seed")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _item(
        connection, manager_item_id=manager_item_id, ticket_id=ticket_id, org_id=org_id, request_id=request_id
    )
    _receipt(
        connection,
        receipt_id=receipt_id,
        manager_item_id=manager_item_id,
        org_id=org_id,
        request_id=request_id,
        command_digest="c" * 64,
    )
    connection.commit()
    connection.close()
    return path, org_id, request_id, manager_item_id, receipt_id


@pytest.mark.parametrize(
    ("table", "pk_column", "pk_key", "column", "value"),
    (
        # -- manager_item: ref kind(pure-hex-no-prefix·wrong-kind)·정수·sha256·timestamp --
        (_ITEMS_TABLE, "manager_item_id", "manager_item_id", "owner_subject_id", "1" * 64),
        (_ITEMS_TABLE, "manager_item_id", "manager_item_id", "owner_subject_id", "receipt:" + "2" * 64),
        (_ITEMS_TABLE, "manager_item_id", "manager_item_id", "org_id", "not-typed"),
        (_ITEMS_TABLE, "manager_item_id", "manager_item_id", "awaiting_revision", -1),
        (_ITEMS_TABLE, "manager_item_id", "manager_item_id", "route_sha256", "z" * 64),
        (_ITEMS_TABLE, "manager_item_id", "manager_item_id", "created_at", "2026-01-01T00:00:00+00:00"),
        # -- escalation receipt: action enum·비-hex digest·revision=0·org 불일치·principal ref --
        (_RECEIPTS_TABLE, "receipt_id", "receipt_id", "action", "manager.assign_owner"),
        (_RECEIPTS_TABLE, "receipt_id", "receipt_id", "command_digest", "g" * 64),
        (_RECEIPTS_TABLE, "receipt_id", "receipt_id", "org_id", "not-typed"),
        (_RECEIPTS_TABLE, "receipt_id", "receipt_id", "principal_ref", "1" * 64),
        (_RECEIPTS_TABLE, "receipt_id", "receipt_id", "created_at", "2026-01-01T00:00:00+00:00"),
    ),
)
def test_row_scalar_corruption_fails_closed_without_repair(
    tmp_path: Path, table: str, pk_column: str, pk_key: str, column: str, value: object
) -> None:
    path, _org_id, _request_id, manager_item_id, receipt_id = _seed_escalation_rows(tmp_path)
    pk_value = {"manager_item_id": manager_item_id, "receipt_id": receipt_id}[pk_key]
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(f"UPDATE {table} SET {column}=? WHERE {pk_column}=?", (value, pk_value))
    connection.commit()
    connection.close()

    assert not reconcile_sqlite_durable_dispatch_escalation_schema(path).capable
    with pytest.raises(SqliteDurableDispatchEscalationSchemaError):
        open_sqlite_durable_dispatch_escalation_connection(path)

    assert _current_value(path, table=table, pk_column=pk_column, pk_value=pk_value, column=column) == value
