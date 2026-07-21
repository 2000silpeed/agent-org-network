"""P17.9 S4.1 durable linked-aggregate schema capability.

This is intentionally only a schema and reconciliation boundary.  It does not
promote legacy ConflictCase/ManagerItem/WorkTicket rows, activate a command, or
store question, answer, rationale, claim token, or control handle payloads.
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

from agent_org_network.sqlite_completion import (
    SqliteCompletionSchemaError,
    validate_sqlite_completion_connection,
)

SQLITE_DURABLE_LINKED_AGGREGATES_COMPONENT_ID: Final = "durable_linked_aggregates_v1"
SQLITE_DURABLE_LINKED_AGGREGATES_SCHEMA_VERSION: Final = 1
SQLITE_DURABLE_LINKED_AGGREGATES_MIGRATION_FAULT_POINTS: Final = (
    "after_conflicts",
    "after_manager_items",
    "after_work_tickets",
    "after_command_receipts",
    "after_audit_intents",
    "after_outbox_intents",
    "before_manifest_insert",
    "after_manifest_insert",
)
type MigrationFaultInjector = Callable[[str], None]


class SqliteDurableLinkedAggregatesSchemaError(RuntimeError):
    """The S4.1 linked-aggregate capability is absent or non-canonical."""


_MANIFEST = "schema_component_manifests"
_CONFLICTS = "durable_linked_conflict_cases"
_MANAGERS = "durable_linked_manager_items"
_TICKETS = "durable_linked_work_tickets"
_RECEIPTS = "durable_linked_command_receipts"
_AUDIT = "durable_linked_audit_intents"
_OUTBOX = "durable_linked_outbox_intents"
_OWNED: Final = (_CONFLICTS, _MANAGERS, _TICKETS, _RECEIPTS, _AUDIT, _OUTBOX)

_CONFLICT_DDL = """
CREATE TABLE durable_linked_conflict_cases (
 conflict_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL UNIQUE COLLATE BINARY,
 awaiting_revision INTEGER NOT NULL,
 status TEXT NOT NULL COLLATE BINARY,
 candidate_set_sha256 TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_MANAGER_DDL = """
CREATE TABLE durable_linked_manager_items (
 manager_item_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL UNIQUE COLLATE BINARY,
 awaiting_revision INTEGER NOT NULL,
 source_kind TEXT NOT NULL COLLATE BINARY,
 source_ref TEXT NOT NULL COLLATE BINARY,
 manager_subject_id TEXT NOT NULL COLLATE BINARY,
 status TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_TICKET_DDL = """
CREATE TABLE durable_linked_work_tickets (
 ticket_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 attempt INTEGER NOT NULL,
 awaiting_revision INTEGER NOT NULL,
 route_sha256 TEXT NOT NULL COLLATE BINARY,
 owner_subject_id TEXT NOT NULL COLLATE BINARY,
 status TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 UNIQUE(request_id, attempt),
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_RECEIPT_DDL = """
CREATE TABLE durable_linked_command_receipts (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL UNIQUE COLLATE BINARY,
 principal_id TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 expected_request_revision INTEGER NOT NULL,
 target_kind TEXT NOT NULL COLLATE BINARY,
 target_ref TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_AUDIT_DDL = """
CREATE TABLE durable_linked_audit_intents (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(receipt_id) REFERENCES durable_linked_command_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_OUTBOX_DDL = """
CREATE TABLE durable_linked_outbox_intents (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 kind TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(receipt_id) REFERENCES durable_linked_command_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_DDLS: Final = (_CONFLICT_DDL, _MANAGER_DDL, _TICKET_DDL, _RECEIPT_DDL, _AUDIT_DDL, _OUTBOX_DDL)
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{6})?[+-][0-9]{2}:[0-9]{2}\Z"
)
# This is a persistence boundary, not a generic identifier parser.  A generic
# slug admits prose (``refund-is-delayed``) and capability-shaped values
# (``grant-abc``).  Each persisted reference is therefore a typed digest:
# literal kind plus a 256-bit, lowercase digest.  No raw human or machine
# payload can be represented by this grammar.
_REF_KIND: Final = {
    "conflict_id": "conflict",
    "org_id": "org",
    "request_id": "request",
    "manager_item_id": "manager",
    "ticket_id": "ticket",
    "receipt_id": "receipt",
    "source_ref": "source",
    "manager_subject_id": "subject",
    "owner_subject_id": "subject",
    "principal_id": "subject",
}
_CONFLICT_STATUS: Final = frozenset({"open", "resolved", "escalated"})
_MANAGER_SOURCE_KIND: Final = frozenset({"conflict", "deadlock", "dispatch", "unowned"})
_MANAGER_STATUS: Final = frozenset({"open", "resolved", "dismissed"})
_TICKET_STATUS: Final = frozenset({"pending", "completed", "escalated"})
_COMMAND_ACTION: Final = frozenset(
    {
        "conflict.agree",
        "conflict.escalate",
        "manager.assign_owner",
        "manager.dismiss",
        "manager.reroute",
        "work_ticket.create",
    }
)
_TARGET_KIND: Final = frozenset({"conflict_case", "manager_item", "work_ticket"})
_OUTBOX_KIND: Final = frozenset({"linked_aggregate_outbox"})
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
        raise SqliteDurableLinkedAggregatesSchemaError("linked aggregate DDL을 읽을 수 없습니다.")
    return " ".join(raw.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table in _OWNED:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise SqliteDurableLinkedAggregatesSchemaError(
                "linked aggregate canonical table이 없습니다."
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
        "component_id": SQLITE_DURABLE_LINKED_AGGREGATES_COMPONENT_ID,
        "component_schema_version": SQLITE_DURABLE_LINKED_AGGREGATES_SCHEMA_VERSION,
        "tables": tables,
    }


@lru_cache(maxsize=1)
def _expected_manifest() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _DDLS:
            connection.execute(ddl)
        return _canonical(_catalog(connection))
    finally:
        connection.close()


def _manifest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _exists(connection, _MANIFEST):
        raise SqliteDurableLinkedAggregatesSchemaError("공유 schema manifest table이 없습니다.")
    return connection.execute(
        "SELECT component_id,schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
        (SQLITE_DURABLE_LINKED_AGGREGATES_COMPONENT_ID,),
    ).fetchone()


def _validate_rows(connection: sqlite3.Connection, *, org_id: str | None) -> None:
    # A principal may only reconcile its own linked rows.  Global schema/FK checks
    # stay outside this filter; foreign-org row corruption must not leak or block it.
    table_fields = (
        (_CONFLICTS, ("conflict_id", "org_id", "request_id")),
        (_MANAGERS, ("manager_item_id", "org_id", "request_id")),
        (_TICKETS, ("ticket_id", "org_id", "request_id")),
        (_RECEIPTS, ("receipt_id", "org_id", "request_id")),
    )
    for table, opaque_fields in table_fields:
        sql = f"SELECT * FROM {table}"
        args: tuple[object, ...] = ()
        if org_id is not None:
            sql += " WHERE org_id COLLATE BINARY=?"
            args = (org_id,)
        for row in connection.execute(sql, args).fetchall():
            for field in opaque_fields:
                _ref(row[field], field=field, name=f"{table}.{field}")
            parent = connection.execute(
                "SELECT org_id FROM question_requests WHERE request_id COLLATE BINARY=?",
                (row["request_id"],),
            ).fetchone()
            if parent is None or parent["org_id"] != row["org_id"]:
                raise SqliteDurableLinkedAggregatesSchemaError(
                    "linked aggregate org/request lineage가 다릅니다."
                )
            if table == _CONFLICTS:
                _integer(row["awaiting_revision"], name="conflict.awaiting_revision")
                _enum(row["status"], name="conflict.status", allowed=_CONFLICT_STATUS)
                _sha256(row["candidate_set_sha256"], name="conflict.candidate_set_sha256")
                _timestamp(row["created_at"], name="conflict.created_at")
            elif table == _MANAGERS:
                _integer(row["awaiting_revision"], name="manager.awaiting_revision")
                _enum(row["source_kind"], name="manager.source_kind", allowed=_MANAGER_SOURCE_KIND)
                _ref(row["source_ref"], field="source_ref", name="manager.source_ref")
                _ref(
                    row["manager_subject_id"],
                    field="manager_subject_id",
                    name="manager.manager_subject_id",
                )
                _enum(row["status"], name="manager.status", allowed=_MANAGER_STATUS)
                _timestamp(row["created_at"], name="manager.created_at")
            elif table == _TICKETS:
                _integer(row["attempt"], name="ticket.attempt")
                _integer(row["awaiting_revision"], name="ticket.awaiting_revision")
                _sha256(row["route_sha256"], name="ticket.route_sha256")
                _ref(row["owner_subject_id"], field="owner_subject_id", name="ticket.owner_subject_id")
                _enum(row["status"], name="ticket.status", allowed=_TICKET_STATUS)
                _timestamp(row["created_at"], name="ticket.created_at")
            else:
                _sha256(row["command_digest"], name="receipt.command_digest")
                _ref(row["principal_id"], field="principal_id", name="receipt.principal_id")
                _enum(row["action"], name="receipt.action", allowed=_COMMAND_ACTION)
                _integer(row["expected_request_revision"], name="receipt.expected_request_revision")
                _enum(row["target_kind"], name="receipt.target_kind", allowed=_TARGET_KIND)
                _target_ref(row["target_ref"], target_kind=row["target_kind"], name="receipt.target_ref")
                _timestamp(row["created_at"], name="receipt.created_at")
    # Intent rows must be 1:1 mirrors of a receipt and deliberately contain no body.
    sql = "SELECT r.receipt_id,r.org_id,r.request_id,r.action,r.command_digest,r.created_at,a.org_id AS audit_org,a.request_id AS audit_request,a.action AS audit_action,a.command_digest AS audit_digest,a.created_at AS audit_created,o.org_id AS outbox_org,o.request_id AS outbox_request,o.command_digest AS outbox_digest,o.created_at AS outbox_created FROM durable_linked_command_receipts r LEFT JOIN durable_linked_audit_intents a ON a.receipt_id=r.receipt_id LEFT JOIN durable_linked_outbox_intents o ON o.receipt_id=r.receipt_id"
    args = ()
    if org_id is not None:
        sql += " WHERE r.org_id COLLATE BINARY=?"
        args = (org_id,)
    for row in connection.execute(sql, args).fetchall():
        if any(row[key] is None for key in ("audit_org", "outbox_org")) or not (
            row["org_id"] == row["audit_org"] == row["outbox_org"]
            and row["request_id"] == row["audit_request"] == row["outbox_request"]
            and row["action"] == row["audit_action"]
            and row["command_digest"] == row["audit_digest"] == row["outbox_digest"]
            and row["created_at"] == row["audit_created"] == row["outbox_created"]
        ):
            raise SqliteDurableLinkedAggregatesSchemaError(
                "linked aggregate intent mirror가 receipt와 다릅니다."
            )
        _ref(row["receipt_id"], field="receipt_id", name="intent.receipt_id")
        _ref(row["org_id"], field="org_id", name="intent.org_id")
        _ref(row["request_id"], field="request_id", name="intent.request_id")
        _enum(row["action"], name="intent.action", allowed=_COMMAND_ACTION)
        _sha256(row["command_digest"], name="intent.command_digest")
        _timestamp(row["created_at"], name="intent.created_at")
        outbox = connection.execute(
            "SELECT kind FROM durable_linked_outbox_intents WHERE receipt_id COLLATE BINARY=?",
            (row["receipt_id"],),
        ).fetchone()
        if outbox is None:
            raise SqliteDurableLinkedAggregatesSchemaError(
                "linked aggregate outbox intent가 없습니다."
            )
        _enum(outbox["kind"], name="outbox.kind", allowed=_OUTBOX_KIND)


def _ref(value: object, *, field: str, name: str) -> str:
    kind = _REF_KIND[field]
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value.removeprefix(f"{kind}:")) is None:
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name}은 typed digest reference여야 합니다."
        )
    if not value.startswith(f"{kind}:"):
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name} reference kind가 올바르지 않습니다."
        )
    return value


def _target_ref(value: object, *, target_kind: object, name: str) -> str:
    if not isinstance(target_kind, str):
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name} target kind가 올바르지 않습니다."
        )
    expected = {
        "conflict_case": "conflict",
        "manager_item": "manager",
        "work_ticket": "ticket",
    }.get(target_kind)
    if expected is None:
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name} target kind가 올바르지 않습니다."
        )
    if not isinstance(value, str) or not value.startswith(f"{expected}:") or (
        _SHA256_RE.fullmatch(value.removeprefix(f"{expected}:")) is None
    ):
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name}은 target kind에 맞는 typed digest reference여야 합니다."
        )
    return value


def _sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name}은 lowercase SHA-256이어야 합니다."
        )
    return value


def _timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name}은 canonical timezone-aware timestamp여야 합니다."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name}은 실제 calendar timezone timestamp여야 합니다."
        ) from error
    precision = "microseconds" if parsed.microsecond else "seconds"
    if parsed.utcoffset() is None or parsed.isoformat(timespec=precision) != value:
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name}은 round-trip canonical timestamp여야 합니다."
        )
    return value


def _integer(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0 or value > _INTEGER_MAX:
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name}은 SQLite 범위의 nonnegative integer여야 합니다."
        )
    return value


def _enum(value: object, *, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise SqliteDurableLinkedAggregatesSchemaError(
            f"linked aggregate {name} enum이 올바르지 않습니다."
        )
    return value


def _validate(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SqliteDurableLinkedAggregatesSchemaError("SQLite foreign_keys=ON이 필요합니다.")
    try:
        validate_sqlite_completion_connection(connection)
    except SqliteCompletionSchemaError as error:
        raise SqliteDurableLinkedAggregatesSchemaError(
            "linked aggregate에는 capable Question Completion parent가 필요합니다."
        ) from error
    marker = _manifest(connection)
    expected = _expected_manifest()
    if (
        marker is None
        or marker["schema_version"] != SQLITE_DURABLE_LINKED_AGGREGATES_SCHEMA_VERSION
        or marker["manifest_json"] != expected
        or marker["manifest_sha256"] != _digest(expected)
    ):
        raise SqliteDurableLinkedAggregatesSchemaError(
            "linked aggregate manifest가 canonical 기대값과 다릅니다."
        )
    if (
        _canonical(_catalog(connection)) != expected
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise SqliteDurableLinkedAggregatesSchemaError(
            "linked aggregate catalog 또는 foreign key가 canonical과 다릅니다."
        )
    if reconcile_rows:
        _validate_rows(connection, org_id=org_id)


def validate_sqlite_durable_linked_aggregates_connection(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    finally:
        connection.row_factory = previous


def migrate_sqlite_durable_linked_aggregates_schema(
    db_path: str | Path, *, fault_injector: MigrationFaultInjector | None = None
) -> None:
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_sqlite_completion_connection(connection)
        except SqliteCompletionSchemaError as error:
            raise SqliteDurableLinkedAggregatesSchemaError(
                "linked aggregate migration에는 capable Question Completion parent가 필요합니다."
            ) from error
        if _manifest(connection) is not None:
            _validate(connection)
            connection.commit()
            return
        if any(_exists(connection, table) for table in _OWNED):
            raise SqliteDurableLinkedAggregatesSchemaError(
                "manifest 없는 partial linked aggregate schema는 복구하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteDurableLinkedAggregatesSchemaError(
                "migration 전 foreign_key_check가 실패했습니다."
            )
        for ddl, point in zip(
            _DDLS, SQLITE_DURABLE_LINKED_AGGREGATES_MIGRATION_FAULT_POINTS[:6], strict=True
        ):
            connection.execute(ddl)
            if fault_injector is not None:
                fault_injector(point)
        expected = _expected_manifest()
        if _canonical(_catalog(connection)) != expected:
            raise SqliteDurableLinkedAggregatesSchemaError(
                "migration 결과 linked aggregate catalog가 canonical과 다릅니다."
            )
        if fault_injector is not None:
            fault_injector("before_manifest_insert")
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES (?,?,?,?)",
            (
                SQLITE_DURABLE_LINKED_AGGREGATES_COMPONENT_ID,
                SQLITE_DURABLE_LINKED_AGGREGATES_SCHEMA_VERSION,
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
        raise SqliteDurableLinkedAggregatesSchemaError(
            "linked aggregate runtime은 기존 SQLite 파일만 엽니다."
        )
    try:
        return sqlite3.connect(
            f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode={'ro' if readonly else 'rw'}",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as error:
        raise SqliteDurableLinkedAggregatesSchemaError(
            "linked aggregate SQLite DB를 열 수 없습니다."
        ) from error


def open_sqlite_durable_linked_aggregates_connection(
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
class DurableLinkedAggregatesSchemaReconciliationReport:
    capable: bool
    detail: str
    linked_aggregate_manifest_present: bool


def reconcile_sqlite_durable_linked_aggregates_schema(
    db_path: str | Path, *, org_id: str | None = None
) -> DurableLinkedAggregatesSchemaReconciliationReport:
    present = False
    try:
        connection = _open(db_path, readonly=True)
    except SqliteDurableLinkedAggregatesSchemaError as error:
        return DurableLinkedAggregatesSchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _exists(connection, _MANIFEST) and _manifest(connection) is not None
        _validate(connection, org_id=org_id)
        return DurableLinkedAggregatesSchemaReconciliationReport(True, "capable_v1", present)
    except Exception as error:
        return DurableLinkedAggregatesSchemaReconciliationReport(False, str(error), present)
    finally:
        connection.close()
