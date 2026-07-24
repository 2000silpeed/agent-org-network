"""P17.9 S5.1 durable dispatch delivery schema capability(ADR 0042 §9 ①).

S5는 S4.1 `durable_linked_aggregates_v1`의 DDL을 바꾸지 않는다 — manifest가
catalog를 exact 봉인하고 v1→v2 경로가 없기 때문이다. 이 컴포넌트는 자기
component(`durable_dispatch_delivery_v1`)를 신설해 lease·delivery 시도·답
receipt 세 테이블만 소유한다. 이 모듈은 schema와 reconciliation 경계만 연다 —
lease claim/renew/reclaim, 실 delivery, 답 수신 Unit of Work는 S5.2 이후다.

S5 자기 timestamp는 S4.1과 다른 canonical 문법(고정폭·고정 `+00:00` offset)을
쓴다 — 문자열 사전순이 곧 시간순이 되어 lease 만료 비교가 문자열 비교로
충분해진다. S4.1 timestamp(가변 offset·초 단위 생략 허용)와 문자열로 비교하지
않는다.
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

SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID: Final = "durable_dispatch_delivery_v1"
SQLITE_DURABLE_DISPATCH_DELIVERY_SCHEMA_VERSION: Final = 1
SQLITE_DURABLE_DISPATCH_DELIVERY_MIGRATION_FAULT_POINTS: Final = (
    "after_leases",
    "after_delivery_attempts",
    "after_answer_receipts",
    "before_manifest_insert",
    "after_manifest_insert",
)
type MigrationFaultInjector = Callable[[str], None]


class SqliteDurableDispatchDeliverySchemaError(RuntimeError):
    """The S5.1 dispatch delivery capability is absent or non-canonical."""


_MANIFEST = "schema_component_manifests"
_LEASES = "durable_dispatch_leases"
_ATTEMPTS = "durable_dispatch_delivery_attempts"
_ANSWERS = "durable_dispatch_answer_receipts"
_OWNED: Final = (_LEASES, _ATTEMPTS, _ANSWERS)

_LEASES_DDL = """
CREATE TABLE durable_dispatch_leases (
 ticket_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 lease_epoch INTEGER NOT NULL,
 holder_ref TEXT NOT NULL COLLATE BINARY,
 state TEXT NOT NULL COLLATE BINARY,
 expires_at TEXT NOT NULL,
 acquired_at TEXT NOT NULL,
 FOREIGN KEY(ticket_id) REFERENCES durable_linked_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_ATTEMPTS_DDL = """
CREATE TABLE durable_dispatch_delivery_attempts (
 attempt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 ticket_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 lease_epoch INTEGER NOT NULL,
 holder_ref TEXT NOT NULL COLLATE BINARY,
 outcome TEXT NOT NULL COLLATE BINARY,
 reason_code TEXT NOT NULL COLLATE BINARY,
 target_subject_ref TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 UNIQUE(ticket_id, lease_epoch),
 FOREIGN KEY(ticket_id) REFERENCES durable_linked_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_ANSWERS_DDL = """
CREATE TABLE durable_dispatch_answer_receipts (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 ticket_id TEXT NOT NULL UNIQUE COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL UNIQUE COLLATE BINARY,
 principal_ref TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 expected_request_revision INTEGER NOT NULL,
 answer_sha256 TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(ticket_id) REFERENCES durable_linked_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_DDLS: Final = (_LEASES_DDL, _ATTEMPTS_DDL, _ANSWERS_DDL)

_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
# S4.1 _TIMESTAMP_RE와 달리 offset·소수점 자리를 고정한다 — 문자열 사전순이
# 시간순과 같아지도록 하기 위함이다. S4.1 timestamp와는 문자열로 비교하지 않는다.
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00\Z"
)
_REF_KIND: Final = {
    "org_id": "org",
    "request_id": "request",
    "ticket_id": "ticket",
    "holder_ref": "subject",
    "principal_ref": "subject",
    "target_subject_ref": "subject",
    "attempt_id": "receipt",
    "receipt_id": "receipt",
}
_LEASE_STATE: Final = frozenset({"leased", "released"})
_DELIVERY_OUTCOME: Final = frozenset({"delivered", "undeliverable"})
_DELIVERY_REASON: Final = frozenset({"ok", "no_connected_worker", "owner_drift", "channel_error"})
_ANSWER_ACTION: Final = frozenset({"work_ticket.complete"})
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
        raise SqliteDurableDispatchDeliverySchemaError("dispatch delivery DDL을 읽을 수 없습니다.")
    return " ".join(raw.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table in _OWNED:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise SqliteDurableDispatchDeliverySchemaError(
                "dispatch delivery canonical table이 없습니다."
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
        "component_id": SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,
        "component_schema_version": SQLITE_DURABLE_DISPATCH_DELIVERY_SCHEMA_VERSION,
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
        raise SqliteDurableDispatchDeliverySchemaError("공유 schema manifest table이 없습니다.")
    return connection.execute(
        "SELECT component_id,schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
        (SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,),
    ).fetchone()


def _ref(value: object, *, field: str, name: str) -> str:
    kind = _REF_KIND[field]
    if (
        not isinstance(value, str)
        or not value.startswith(f"{kind}:")
        or _SHA256_RE.fullmatch(value.removeprefix(f"{kind}:")) is None
    ):
        raise SqliteDurableDispatchDeliverySchemaError(
            f"dispatch delivery {name}은 typed digest reference여야 합니다."
        )
    return value


def _sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SqliteDurableDispatchDeliverySchemaError(
            f"dispatch delivery {name}은 lowercase SHA-256이어야 합니다."
        )
    return value


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise SqliteDurableDispatchDeliverySchemaError(
            f"dispatch delivery {name}은 canonical UTC instant여야 합니다."
        )
    try:
        parsed = _dt(value)
    except ValueError as error:
        raise SqliteDurableDispatchDeliverySchemaError(
            f"dispatch delivery {name}은 실제 calendar timestamp여야 합니다."
        ) from error
    if parsed.utcoffset() is None or parsed.isoformat(timespec="microseconds") != value:
        raise SqliteDurableDispatchDeliverySchemaError(
            f"dispatch delivery {name}은 round-trip canonical UTC instant여야 합니다."
        )
    return value


def _integer(value: object, *, name: str, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > _INTEGER_MAX:
        raise SqliteDurableDispatchDeliverySchemaError(
            f"dispatch delivery {name}은 SQLite 범위의 정수여야 합니다."
        )
    return value


def _enum(value: object, *, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise SqliteDurableDispatchDeliverySchemaError(f"dispatch delivery {name} enum이 올바르지 않습니다.")
    return value


def _parent_ticket(
    connection: sqlite3.Connection, *, ticket_id: str, org_id: str, request_id: str, name: str
) -> None:
    ticket = connection.execute(
        "SELECT org_id,request_id FROM durable_linked_work_tickets WHERE ticket_id COLLATE BINARY=?",
        (ticket_id,),
    ).fetchone()
    if ticket is None or ticket["org_id"] != org_id or ticket["request_id"] != request_id:
        raise SqliteDurableDispatchDeliverySchemaError(f"{name} ticket lineage가 다릅니다.")


def _validate_rows(connection: sqlite3.Connection, *, org_id: str | None) -> None:
    # 타 org row corruption이 이 org의 reconcile을 막지 않는다 — catalog/FK는
    # 전역, row 정합만 org 필터. cross-aggregate 정합(예: answer receipt와
    # ticket.status 대조)은 의도적으로 여기 없다 — S5.7 게이트 몫이다.
    where, args = ("", ()) if org_id is None else (" WHERE org_id COLLATE BINARY=?", (org_id,))

    leases: dict[str, sqlite3.Row] = {}
    for row in connection.execute(f"SELECT * FROM {_LEASES}{where}", args).fetchall():
        for field in ("ticket_id", "org_id", "request_id", "holder_ref"):
            _ref(row[field], field=field, name=f"lease.{field}")
        _integer(row["lease_epoch"], name="lease.lease_epoch", positive=True)
        _enum(row["state"], name="lease.state", allowed=_LEASE_STATE)
        _timestamp(row["acquired_at"], name="lease.acquired_at")
        _timestamp(row["expires_at"], name="lease.expires_at")
        if row["expires_at"] < row["acquired_at"]:
            raise SqliteDurableDispatchDeliverySchemaError(
                "lease.expires_at가 lease.acquired_at보다 이릅니다."
            )
        _parent_ticket(
            connection,
            ticket_id=row["ticket_id"],
            org_id=row["org_id"],
            request_id=row["request_id"],
            name="lease",
        )
        leases[row["ticket_id"]] = row

    for row in connection.execute(f"SELECT * FROM {_ATTEMPTS}{where}", args).fetchall():
        for field in ("attempt_id", "org_id", "ticket_id", "request_id", "holder_ref", "target_subject_ref"):
            _ref(row[field], field=field, name=f"attempt.{field}")
        _integer(row["lease_epoch"], name="attempt.lease_epoch", positive=True)
        outcome = _enum(row["outcome"], name="attempt.outcome", allowed=_DELIVERY_OUTCOME)
        reason_code = _enum(row["reason_code"], name="attempt.reason_code", allowed=_DELIVERY_REASON)
        if (outcome == "delivered") != (reason_code == "ok"):
            raise SqliteDurableDispatchDeliverySchemaError(
                "attempt.outcome과 attempt.reason_code가 서로 모순됩니다."
            )
        _timestamp(row["created_at"], name="attempt.created_at")
        _parent_ticket(
            connection,
            ticket_id=row["ticket_id"],
            org_id=row["org_id"],
            request_id=row["request_id"],
            name="attempt",
        )
        lease = leases.get(row["ticket_id"])
        if lease is not None and row["lease_epoch"] > lease["lease_epoch"]:
            raise SqliteDurableDispatchDeliverySchemaError(
                "attempt.lease_epoch이 현재 lease.lease_epoch보다 앞섭니다."
            )

    for row in connection.execute(f"SELECT * FROM {_ANSWERS}{where}", args).fetchall():
        for field in ("receipt_id", "org_id", "ticket_id", "request_id", "principal_ref"):
            _ref(row[field], field=field, name=f"answer.{field}")
        _enum(row["action"], name="answer.action", allowed=_ANSWER_ACTION)
        _integer(row["expected_request_revision"], name="answer.expected_request_revision", positive=True)
        _sha256(row["command_digest"], name="answer.command_digest")
        _sha256(row["answer_sha256"], name="answer.answer_sha256")
        _timestamp(row["created_at"], name="answer.created_at")
        _parent_ticket(
            connection,
            ticket_id=row["ticket_id"],
            org_id=row["org_id"],
            request_id=row["request_id"],
            name="answer",
        )


def _validate(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SqliteDurableDispatchDeliverySchemaError("SQLite foreign_keys=ON이 필요합니다.")
    try:
        validate_sqlite_durable_linked_aggregates_connection(
            connection, org_id=org_id, reconcile_rows=reconcile_rows
        )
    except SqliteDurableLinkedAggregatesSchemaError as error:
        raise SqliteDurableDispatchDeliverySchemaError(
            "dispatch delivery에는 capable linked aggregate parent가 필요합니다."
        ) from error
    marker = _manifest(connection)
    expected = _expected_manifest()
    if (
        marker is None
        or marker["schema_version"] != SQLITE_DURABLE_DISPATCH_DELIVERY_SCHEMA_VERSION
        or marker["manifest_json"] != expected
        or marker["manifest_sha256"] != _digest(expected)
    ):
        raise SqliteDurableDispatchDeliverySchemaError(
            "dispatch delivery manifest가 canonical 기대값과 다릅니다."
        )
    if (
        _canonical(_catalog(connection)) != expected
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise SqliteDurableDispatchDeliverySchemaError(
            "dispatch delivery catalog 또는 foreign key가 canonical과 다릅니다."
        )
    if reconcile_rows:
        _validate_rows(connection, org_id=org_id)


def validate_sqlite_durable_dispatch_delivery_connection(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    finally:
        connection.row_factory = previous


def migrate_sqlite_durable_dispatch_delivery_schema(
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
            raise SqliteDurableDispatchDeliverySchemaError(
                "dispatch delivery migration에는 capable linked aggregate parent가 필요합니다."
            ) from error
        if _manifest(connection) is not None:
            _validate(connection)
            connection.commit()
            return
        if any(_exists(connection, table) for table in _OWNED):
            raise SqliteDurableDispatchDeliverySchemaError(
                "manifest 없는 partial dispatch delivery schema는 복구하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteDurableDispatchDeliverySchemaError(
                "migration 전 foreign_key_check가 실패했습니다."
            )
        for ddl, point in zip(
            _DDLS, SQLITE_DURABLE_DISPATCH_DELIVERY_MIGRATION_FAULT_POINTS[:3], strict=True
        ):
            connection.execute(ddl)
            if fault_injector is not None:
                fault_injector(point)
        expected = _expected_manifest()
        if _canonical(_catalog(connection)) != expected:
            raise SqliteDurableDispatchDeliverySchemaError(
                "migration 결과 dispatch delivery catalog가 canonical과 다릅니다."
            )
        if fault_injector is not None:
            fault_injector("before_manifest_insert")
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES (?,?,?,?)",
            (
                SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,
                SQLITE_DURABLE_DISPATCH_DELIVERY_SCHEMA_VERSION,
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
        raise SqliteDurableDispatchDeliverySchemaError(
            "dispatch delivery runtime은 기존 SQLite 파일만 엽니다."
        )
    try:
        return sqlite3.connect(
            f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode={'ro' if readonly else 'rw'}",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as error:
        raise SqliteDurableDispatchDeliverySchemaError(
            "dispatch delivery SQLite DB를 열 수 없습니다."
        ) from error


def open_sqlite_durable_dispatch_delivery_connection(
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
class DurableDispatchDeliverySchemaReconciliationReport:
    capable: bool
    detail: str
    dispatch_delivery_manifest_present: bool


def reconcile_sqlite_durable_dispatch_delivery_schema(
    db_path: str | Path, *, org_id: str | None = None
) -> DurableDispatchDeliverySchemaReconciliationReport:
    present = False
    try:
        connection = _open(db_path, readonly=True)
    except SqliteDurableDispatchDeliverySchemaError as error:
        return DurableDispatchDeliverySchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _exists(connection, _MANIFEST) and _manifest(connection) is not None
        _validate(connection, org_id=org_id)
        return DurableDispatchDeliverySchemaReconciliationReport(True, "capable_v1", present)
    except Exception as error:
        return DurableDispatchDeliverySchemaReconciliationReport(False, str(error), present)
    finally:
        connection.close()
