"""P17.9 S5.6a durable dispatch escalation schema capability(ADR 0066 §5.1).

S4.1 `durable_linked_manager_items.request_id`가 UNIQUE라 한 Question Request는
평생 한 번만 durable Manager 처분 큐에 들어갈 수 있다. dispatch timeout
escalation(사람 처분 뒤 재실행이 다시 timeout되는 순환, ADR 0042 §2)은 그
평생-1회 제약과 부딪히므로 ADR 0066은 S4.1 DDL을 바꾸지 않고(manifest가
catalog를 exact 봉인·v1→v2 경로 없음) 대신 S5 소유 신 component를 판다. 이
컴포넌트(`durable_dispatch_escalation_v1`)는 FromDispatch Item
(`durable_dispatch_manager_items`, 키 `UNIQUE(request_id, attempt)` — **한
Request가 실행 시도마다 최대 한 번** 사람 큐에 들어간다)과 그 처분·escalation
3종 action 공용 command receipt(`durable_dispatch_escalation_receipts`)만
소유한다. S4.1 `durable_linked_manager_items`는 escalation(FromDeadlock)·
Unowned ingress Item만 계속 소유하며 이 컴포넌트와 무관하다.

이 모듈은 schema와 reconciliation 경계만 연다 — escalation Unit of Work(system
전이·SLA 재판정)와 처분 Unit of Work는 S5.6 이후 슬라이스다.

**audit/outbox intent mirror를 두지 않는다.** S4.1이 receipt마다 붙이는 그 두
표는 receipt와 필드가 exact 일치해야 하는 순수 중복이고 소비자가 오늘
0이다(ADR 0042 §9 ③·⑥). S5.4(`durable_dispatch_answer_receipts`)가 같은
이유로 이미 mirror를 두지 않았고, 이 컴포넌트도 그 선례를 따른다. receipt
자체가 그 명령의 durable 기록이고, 운영 감사 로그로 내보내는 것은 소비자
관심사로 이월한다.

**status enum은 S4.1과 같은 값 집합 `{open, resolved, dismissed}`를 쓴다**
(S6 PostgreSQL 이관에서 두 Manager Item 표를 `(request_id, attempt)` 키의
한 표로 합치는 것을 기계적으로 만들기 위함, ADR 0066 결정 §2 (iii)). 그러나
의미는 다르다 — **이 표에서는 `resolved ⟺ manager.reroute`**이고 S4.1에서는
`resolved ⟺ manager.assign_owner`다. 이 상태-action 대응 자체를 이 스키마
계층이 교차 검증하지는 않는다(S5.1 판과 동형 — cross-aggregate 정합은
reconciliation 게이트 몫이며, 그 몫은 S5.7이다).

S5 자기 timestamp는 S4.1과 다른 canonical 문법(고정폭·고정 `+00:00` offset)을
쓴다 — `created_at`·`observed_due_at` 둘 다. Item은 S4.1 ticket에 FK로
붙지만, 경계를 넘는 값은 정수(`attempt`·`awaiting_revision`)와 typed ref뿐
이며 두 표의 timestamp를 문자열로 비교하는 코드는 이 모듈 어디에도 없다
(ADR 0066 §5.1). SLA 판정(`due_at <= now`)은 S5.6 UoW가 Python `datetime`
객체 사이에서 수행하고, 그 결과만 canonical 문자열로 정규화해 여기 저장한다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Final

from agent_org_network.sqlite_durable_linked_aggregates import (
    SqliteDurableLinkedAggregatesSchemaError,
    validate_sqlite_durable_linked_aggregates_connection,
)

SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID: Final = "durable_dispatch_escalation_v1"
SQLITE_DURABLE_DISPATCH_ESCALATION_SCHEMA_VERSION: Final = 1
SQLITE_DURABLE_DISPATCH_ESCALATION_MIGRATION_FAULT_POINTS: Final = (
    "after_manager_items",
    "after_escalation_receipts",
    "before_manifest_insert",
    "after_manifest_insert",
)
type MigrationFaultInjector = Callable[[str], None]


class SqliteDurableDispatchEscalationSchemaError(RuntimeError):
    """The S5.6a dispatch escalation capability is absent or non-canonical."""


_MANIFEST = "schema_component_manifests"
_ITEMS = "durable_dispatch_manager_items"
_RECEIPTS = "durable_dispatch_escalation_receipts"
_OWNED: Final = (_ITEMS, _RECEIPTS)

_ITEMS_DDL = """
CREATE TABLE durable_dispatch_manager_items (
 manager_item_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 ticket_id TEXT NOT NULL UNIQUE COLLATE BINARY,
 attempt INTEGER NOT NULL,
 awaiting_revision INTEGER NOT NULL,
 route_sha256 TEXT NOT NULL COLLATE BINARY,
 owner_subject_id TEXT NOT NULL COLLATE BINARY,
 manager_subject_id TEXT NOT NULL COLLATE BINARY,
 status TEXT NOT NULL COLLATE BINARY,
 observed_due_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(request_id, attempt),
 FOREIGN KEY(ticket_id) REFERENCES durable_linked_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_RECEIPTS_DDL = """
CREATE TABLE durable_dispatch_escalation_receipts (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 manager_item_id TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL UNIQUE COLLATE BINARY,
 principal_ref TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 expected_request_revision INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 FOREIGN KEY(manager_item_id) REFERENCES durable_dispatch_manager_items(manager_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_DDLS: Final = (_ITEMS_DDL, _RECEIPTS_DDL)

_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
# S4.1 _TIMESTAMP_RE와 달리 offset·소수점 자리를 고정한다(S5.1 판과 동형) —
# 문자열 사전순이 시간순과 같아지도록 하기 위함이다. S4.1 timestamp와는
# 문자열로 비교하지 않는다.
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00\Z"
)
_REF_KIND: Final = {
    "manager_item_id": "manager",
    "org_id": "org",
    "request_id": "request",
    "ticket_id": "ticket",
    "owner_subject_id": "subject",
    "manager_subject_id": "subject",
    "principal_ref": "subject",
    "receipt_id": "receipt",
}
# S4.1과 같은 값 집합(모듈 docstring 참조) — 단 이 표에서는
# resolved ⟺ manager.reroute다(S4.1은 resolved ⟺ manager.assign_owner).
_ITEM_STATUS: Final = frozenset({"open", "resolved", "dismissed"})
_ESCALATION_ACTION: Final = frozenset(
    {"work_ticket.escalate", "manager.reroute", "manager.dismiss"}
)
_INTEGER_MAX: Final = 2**63 - 1


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _tokens(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise SqliteDurableDispatchEscalationSchemaError("dispatch escalation DDL을 읽을 수 없습니다.")
    return " ".join(raw.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table in _OWNED:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise SqliteDurableDispatchEscalationSchemaError(
                "dispatch escalation canonical table이 없습니다."
            )
        tables.append(
            {
                "name": table,
                "ddl": _tokens(row[0]),
                "columns": [
                    tuple(column)
                    for column in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
                ],
                "foreign_keys": [
                    tuple(fk)
                    for fk in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
                ],
            }
        )
    return {
        "component_id": SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID,
        "component_schema_version": SQLITE_DURABLE_DISPATCH_ESCALATION_SCHEMA_VERSION,
        "tables": tables,
    }


@lru_cache(maxsize=1)
def _expected_manifest() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        connection.execute(
            "CREATE TABLE durable_linked_work_tickets (ticket_id TEXT PRIMARY KEY NOT NULL)"
        )
        for ddl in _DDLS:
            connection.execute(ddl)
        return _canonical(_catalog(connection))
    finally:
        connection.close()


def _manifest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _exists(connection, _MANIFEST):
        raise SqliteDurableDispatchEscalationSchemaError("공유 schema manifest table이 없습니다.")
    return connection.execute(
        "SELECT component_id,schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
        (SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID,),
    ).fetchone()


def _ref(value: object, *, field: str, name: str) -> str:
    kind = _REF_KIND[field]
    if (
        not isinstance(value, str)
        or not value.startswith(f"{kind}:")
        or _SHA256_RE.fullmatch(value.removeprefix(f"{kind}:")) is None
    ):
        raise SqliteDurableDispatchEscalationSchemaError(
            f"dispatch escalation {name}은 typed digest reference여야 합니다."
        )
    return value


def _sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SqliteDurableDispatchEscalationSchemaError(
            f"dispatch escalation {name}은 lowercase SHA-256이어야 합니다."
        )
    return value


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise SqliteDurableDispatchEscalationSchemaError(
            f"dispatch escalation {name}은 canonical UTC instant여야 합니다."
        )
    try:
        parsed = _dt(value)
    except ValueError as error:
        raise SqliteDurableDispatchEscalationSchemaError(
            f"dispatch escalation {name}은 실제 calendar timestamp여야 합니다."
        ) from error
    if parsed.utcoffset() is None or parsed.isoformat(timespec="microseconds") != value:
        raise SqliteDurableDispatchEscalationSchemaError(
            f"dispatch escalation {name}은 round-trip canonical UTC instant여야 합니다."
        )
    return value


def _integer(value: object, *, name: str, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > _INTEGER_MAX:
        raise SqliteDurableDispatchEscalationSchemaError(
            f"dispatch escalation {name}은 SQLite 범위의 정수여야 합니다."
        )
    return value


def _enum(value: object, *, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise SqliteDurableDispatchEscalationSchemaError(
            f"dispatch escalation {name} enum이 올바르지 않습니다."
        )
    return value


def _parent_ticket(
    connection: sqlite3.Connection, *, ticket_id: str, org_id: str, request_id: str, name: str
) -> None:
    ticket = connection.execute(
        "SELECT org_id,request_id FROM durable_linked_work_tickets WHERE ticket_id COLLATE BINARY=?",
        (ticket_id,),
    ).fetchone()
    if ticket is None or ticket["org_id"] != org_id or ticket["request_id"] != request_id:
        raise SqliteDurableDispatchEscalationSchemaError(f"{name} ticket lineage가 다릅니다.")


def _parent_item(
    connection: sqlite3.Connection, *, manager_item_id: str, org_id: str, request_id: str, name: str
) -> None:
    item = connection.execute(
        f"SELECT org_id,request_id FROM {_ITEMS} WHERE manager_item_id COLLATE BINARY=?",
        (manager_item_id,),
    ).fetchone()
    if item is None or item["org_id"] != org_id or item["request_id"] != request_id:
        raise SqliteDurableDispatchEscalationSchemaError(f"{name} manager item lineage가 다릅니다.")


def _validate_rows(connection: sqlite3.Connection, *, org_id: str | None) -> None:
    # 타 org row corruption이 이 org의 reconcile을 막지 않는다 — catalog/FK는
    # 전역, row 정합만 org 필터. cross-aggregate 정합(예: item.status와
    # receipt.action 대응)은 의도적으로 여기 없다 — S5.7 게이트 몫이다.
    where, args = ("", ()) if org_id is None else (" WHERE org_id COLLATE BINARY=?", (org_id,))

    for row in connection.execute(f"SELECT * FROM {_ITEMS}{where}", args).fetchall():
        for field in (
            "manager_item_id",
            "org_id",
            "request_id",
            "ticket_id",
            "owner_subject_id",
            "manager_subject_id",
        ):
            _ref(row[field], field=field, name=f"manager_item.{field}")
        _integer(row["attempt"], name="manager_item.attempt", positive=True)
        _integer(row["awaiting_revision"], name="manager_item.awaiting_revision")
        _sha256(row["route_sha256"], name="manager_item.route_sha256")
        _enum(row["status"], name="manager_item.status", allowed=_ITEM_STATUS)
        _timestamp(row["observed_due_at"], name="manager_item.observed_due_at")
        _timestamp(row["created_at"], name="manager_item.created_at")
        _parent_ticket(
            connection,
            ticket_id=row["ticket_id"],
            org_id=row["org_id"],
            request_id=row["request_id"],
            name="manager_item",
        )

    for row in connection.execute(f"SELECT * FROM {_RECEIPTS}{where}", args).fetchall():
        for field in ("receipt_id", "org_id", "request_id", "manager_item_id", "principal_ref"):
            _ref(row[field], field=field, name=f"escalation_receipt.{field}")
        _enum(row["action"], name="escalation_receipt.action", allowed=_ESCALATION_ACTION)
        _integer(
            row["expected_request_revision"],
            name="escalation_receipt.expected_request_revision",
            positive=True,
        )
        _sha256(row["command_digest"], name="escalation_receipt.command_digest")
        _timestamp(row["created_at"], name="escalation_receipt.created_at")
        _parent_item(
            connection,
            manager_item_id=row["manager_item_id"],
            org_id=row["org_id"],
            request_id=row["request_id"],
            name="escalation_receipt",
        )


def _validate(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SqliteDurableDispatchEscalationSchemaError("SQLite foreign_keys=ON이 필요합니다.")
    try:
        validate_sqlite_durable_linked_aggregates_connection(
            connection, org_id=org_id, reconcile_rows=reconcile_rows
        )
    except SqliteDurableLinkedAggregatesSchemaError as error:
        raise SqliteDurableDispatchEscalationSchemaError(
            "dispatch escalation에는 capable linked aggregate parent가 필요합니다."
        ) from error
    marker = _manifest(connection)
    expected = _expected_manifest()
    if (
        marker is None
        or marker["schema_version"] != SQLITE_DURABLE_DISPATCH_ESCALATION_SCHEMA_VERSION
        or marker["manifest_json"] != expected
        or marker["manifest_sha256"] != _digest(expected)
    ):
        raise SqliteDurableDispatchEscalationSchemaError(
            "dispatch escalation manifest가 canonical 기대값과 다릅니다."
        )
    if (
        _canonical(_catalog(connection)) != expected
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise SqliteDurableDispatchEscalationSchemaError(
            "dispatch escalation catalog 또는 foreign key가 canonical과 다릅니다."
        )
    if reconcile_rows:
        _validate_rows(connection, org_id=org_id)


def validate_sqlite_durable_dispatch_escalation_connection(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    finally:
        connection.row_factory = previous


def migrate_sqlite_durable_dispatch_escalation_schema(
    db_path: str | Path, *, fault_injector: MigrationFaultInjector | None = None
) -> None:
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_sqlite_durable_linked_aggregates_connection(connection)
        except SqliteDurableLinkedAggregatesSchemaError as error:
            raise SqliteDurableDispatchEscalationSchemaError(
                "dispatch escalation migration에는 capable linked aggregate parent가 필요합니다."
            ) from error
        if _manifest(connection) is not None:
            _validate(connection)
            connection.commit()
            return
        if any(_exists(connection, table) for table in _OWNED):
            raise SqliteDurableDispatchEscalationSchemaError(
                "manifest 없는 partial dispatch escalation schema는 복구하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteDurableDispatchEscalationSchemaError(
                "migration 전 foreign_key_check가 실패했습니다."
            )
        for ddl, point in zip(
            _DDLS, SQLITE_DURABLE_DISPATCH_ESCALATION_MIGRATION_FAULT_POINTS[:2], strict=True
        ):
            connection.execute(ddl)
            if fault_injector is not None:
                fault_injector(point)
        expected = _expected_manifest()
        if _canonical(_catalog(connection)) != expected:
            raise SqliteDurableDispatchEscalationSchemaError(
                "migration 결과 dispatch escalation catalog가 canonical과 다릅니다."
            )
        if fault_injector is not None:
            fault_injector("before_manifest_insert")
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES (?,?,?,?)",
            (
                SQLITE_DURABLE_DISPATCH_ESCALATION_COMPONENT_ID,
                SQLITE_DURABLE_DISPATCH_ESCALATION_SCHEMA_VERSION,
                expected,
                _digest(expected),
            ),
        )
        if fault_injector is not None:
            fault_injector("after_manifest_insert")
        _validate(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _open(path: str | Path, *, readonly: bool) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise SqliteDurableDispatchEscalationSchemaError(
            "dispatch escalation runtime은 기존 SQLite 파일만 엽니다."
        )
    try:
        return sqlite3.connect(
            f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode={'ro' if readonly else 'rw'}",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as error:
        raise SqliteDurableDispatchEscalationSchemaError(
            "dispatch escalation SQLite DB를 열 수 없습니다."
        ) from error


def open_sqlite_durable_dispatch_escalation_connection(
    db_path: str | Path, *, org_id: str | None = None
) -> sqlite3.Connection:
    connection = _open(db_path, readonly=False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _validate(connection, org_id=org_id)
    except Exception:
        connection.close()
        raise
    return connection


@dataclass(frozen=True)
class DurableDispatchEscalationSchemaReconciliationReport:
    capable: bool
    detail: str
    dispatch_escalation_manifest_present: bool


def reconcile_sqlite_durable_dispatch_escalation_schema(
    db_path: str | Path, *, org_id: str | None = None
) -> DurableDispatchEscalationSchemaReconciliationReport:
    present = False
    try:
        connection = _open(db_path, readonly=True)
    except SqliteDurableDispatchEscalationSchemaError as error:
        return DurableDispatchEscalationSchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _exists(connection, _MANIFEST) and _manifest(connection) is not None
        _validate(connection, org_id=org_id)
        return DurableDispatchEscalationSchemaReconciliationReport(True, "capable_v1", present)
    except Exception as error:
        return DurableDispatchEscalationSchemaReconciliationReport(False, str(error), present)
    finally:
        connection.close()
