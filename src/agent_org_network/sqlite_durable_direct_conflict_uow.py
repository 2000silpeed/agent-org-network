"""P17.9 S4.2a durable direct-Conflict evidence schema.

This is a validate-only schema capability.  It deliberately persists neither
legacy claim/control handles nor raw candidate, Owner, Agent Card, rationale,
question, grant, or secret material; S4.2b owns the command UoW.
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

SQLITE_DURABLE_DIRECT_CONFLICT_UOW_COMPONENT_ID: Final = "durable_direct_conflict_uow_v1"
SQLITE_DURABLE_DIRECT_CONFLICT_UOW_SCHEMA_VERSION: Final = 1
SQLITE_DURABLE_DIRECT_CONFLICT_UOW_MIGRATION_FAULT_POINTS: Final = (
    "after_votes",
    "after_receipts",
    "after_audit_intents",
    "after_outbox_intents",
    "after_result_projections",
    "before_manifest_insert",
    "after_manifest_insert",
)
type MigrationFaultInjector = Callable[[str], None]


class SqliteDurableDirectConflictUowSchemaError(RuntimeError):
    """The direct Conflict evidence capability is absent or non-canonical."""


_MANIFEST = "schema_component_manifests"
_VOTES = "durable_direct_conflict_votes"
_RECEIPTS = "durable_direct_conflict_receipts"
_AUDIT = "durable_direct_conflict_audit_intents"
_OUTBOX = "durable_direct_conflict_outbox_intents"
_RESULTS = "durable_direct_conflict_result_projections"
_OWNED: Final = (_VOTES, _RECEIPTS, _AUDIT, _OUTBOX, _RESULTS)

_VOTES_DDL = """
CREATE TABLE durable_direct_conflict_votes (
 conflict_id TEXT NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 concurrence_round INTEGER NOT NULL,
 owner_subject_ref TEXT NOT NULL COLLATE BINARY,
 target_card_ref TEXT NOT NULL COLLATE BINARY,
 candidate_set_sha256 TEXT NOT NULL COLLATE BINARY,
 candidate_owner_count INTEGER NOT NULL,
 vote_receipt_id TEXT NOT NULL UNIQUE COLLATE BINARY,
 created_at TEXT NOT NULL,
 PRIMARY KEY(conflict_id, owner_subject_ref, concurrence_round),
 FOREIGN KEY(conflict_id) REFERENCES durable_linked_conflict_cases(conflict_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_RECEIPTS_DDL = """
CREATE TABLE durable_direct_conflict_receipts (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 conflict_id TEXT NOT NULL COLLATE BINARY,
 concurrence_round INTEGER NOT NULL,
 command_digest TEXT NOT NULL UNIQUE COLLATE BINARY,
 actor_subject_ref TEXT NOT NULL COLLATE BINARY,
 owner_subject_ref TEXT NOT NULL COLLATE BINARY,
 target_card_ref TEXT NOT NULL COLLATE BINARY,
 candidate_set_sha256 TEXT NOT NULL COLLATE BINARY,
 candidate_owner_count INTEGER NOT NULL,
 action TEXT NOT NULL COLLATE BINARY,
 expected_request_revision INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(conflict_id, owner_subject_ref, concurrence_round),
 FOREIGN KEY(conflict_id) REFERENCES durable_linked_conflict_cases(conflict_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(receipt_id) REFERENCES durable_direct_conflict_votes(vote_receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(conflict_id,owner_subject_ref,concurrence_round) REFERENCES durable_direct_conflict_votes(conflict_id,owner_subject_ref,concurrence_round) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_AUDIT_DDL = """
CREATE TABLE durable_direct_conflict_audit_intents (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(receipt_id) REFERENCES durable_direct_conflict_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_OUTBOX_DDL = """
CREATE TABLE durable_direct_conflict_outbox_intents (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(receipt_id) REFERENCES durable_direct_conflict_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_RESULTS_DDL = """
CREATE TABLE durable_direct_conflict_result_projections (
 -- S4.2a has not been released: this is the v1 canonical shape, not a
 -- migration of a previously supported evidence format.  A receipt has one
 -- and only one immutable result projection.
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 conflict_id TEXT NOT NULL COLLATE BINARY,
 concurrence_round INTEGER NOT NULL,
 result_kind TEXT NOT NULL COLLATE BINARY,
 owner_subject_ref TEXT NOT NULL COLLATE BINARY,
 target_card_ref TEXT NOT NULL COLLATE BINARY,
 candidate_set_sha256 TEXT NOT NULL COLLATE BINARY,
 candidate_owner_count INTEGER NOT NULL,
 accepted_vote_count INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 FOREIGN KEY(receipt_id) REFERENCES durable_direct_conflict_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(conflict_id) REFERENCES durable_linked_conflict_cases(conflict_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_DDLS: Final = (_VOTES_DDL, _RECEIPTS_DDL, _AUDIT_DDL, _OUTBOX_DDL, _RESULTS_DDL)

_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?[+-][0-9]{2}:[0-9]{2}\Z"
)
_REF_KINDS: Final = {
    "conflict_id": "conflict",
    "org_id": "org",
    "request_id": "request",
    "receipt_id": "receipt",
    "owner_subject_ref": "subject",
    "actor_subject_ref": "subject",
    "target_card_ref": "card",
}
_ACTION: Final = frozenset({"conflict.concur"})
_RESULT_KIND: Final = frozenset({"vote_recorded", "consensus_ready"})
_INTEGER_MAX: Final = 2**63 - 1


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _tokens(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise SqliteDurableDirectConflictUowSchemaError("direct Conflict DDL을 읽을 수 없습니다.")
    return " ".join(raw.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table in _OWNED:
        row = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None:
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict canonical table이 없습니다.")
        tables.append({"name": table, "ddl": _tokens(row[0]), "columns": [tuple(v) for v in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()], "foreign_keys": [tuple(v) for v in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()]})
    return {"component_id": SQLITE_DURABLE_DIRECT_CONFLICT_UOW_COMPONENT_ID, "component_schema_version": SQLITE_DURABLE_DIRECT_CONFLICT_UOW_SCHEMA_VERSION, "tables": tables}


@lru_cache(maxsize=1)
def _expected_manifest() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL)")
        connection.execute("CREATE TABLE durable_linked_conflict_cases (conflict_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _DDLS:
            connection.execute(ddl)
        return _canonical(_catalog(connection))
    finally:
        connection.close()


def _manifest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _exists(connection, _MANIFEST):
        raise SqliteDurableDirectConflictUowSchemaError("공유 schema manifest table이 없습니다.")
    return connection.execute("SELECT component_id,schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id COLLATE BINARY=?", (SQLITE_DURABLE_DIRECT_CONFLICT_UOW_COMPONENT_ID,)).fetchone()


def _ref(value: object, *, field: str, name: str) -> str:
    kind = _REF_KINDS[field]
    if not isinstance(value, str) or not value.startswith(f"{kind}:") or _SHA256_RE.fullmatch(value.removeprefix(f"{kind}:")) is None:
        raise SqliteDurableDirectConflictUowSchemaError(f"direct Conflict {name}은 typed digest reference여야 합니다.")
    return value


def _sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SqliteDurableDirectConflictUowSchemaError(f"direct Conflict {name}은 lowercase SHA-256이어야 합니다.")
    return value


def _integer(value: object, *, name: str, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > _INTEGER_MAX:
        raise SqliteDurableDirectConflictUowSchemaError(f"direct Conflict {name}은 SQLite 범위 정수여야 합니다.")
    return value


def _timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise SqliteDurableDirectConflictUowSchemaError(f"direct Conflict {name} timestamp가 올바르지 않습니다.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SqliteDurableDirectConflictUowSchemaError(f"direct Conflict {name} calendar timestamp가 올바르지 않습니다.") from error
    precision = "microseconds" if parsed.microsecond else "seconds"
    if parsed.utcoffset() is None or parsed.isoformat(timespec=precision) != value:
        raise SqliteDurableDirectConflictUowSchemaError(f"direct Conflict {name} timestamp가 canonical이 아닙니다.")
    return value


def _enum(value: object, *, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise SqliteDurableDirectConflictUowSchemaError(f"direct Conflict {name} enum이 올바르지 않습니다.")
    return value


def _lineage(connection: sqlite3.Connection, row: sqlite3.Row, *, prefix: str) -> None:
    parent = connection.execute("SELECT org_id,request_id FROM durable_linked_conflict_cases WHERE conflict_id COLLATE BINARY=?", (row["conflict_id"],)).fetchone()
    if parent is None or parent["org_id"] != row["org_id"] or parent["request_id"] != row["request_id"]:
        raise SqliteDurableDirectConflictUowSchemaError(f"direct Conflict {prefix} lineage가 다릅니다.")


def _command_digest_for(receipt: sqlite3.Row) -> str:
    """Return the idempotency identity of a concurrence command.

    Receipt ids are intentionally absent: a retry must have the same digest,
    while any change to its authority/revision/candidate target is a different
    command and cannot be replayed as the original receipt.
    """
    fields = (
        "org_id",
        "request_id",
        "conflict_id",
        "concurrence_round",
        "actor_subject_ref",
        "owner_subject_ref",
        "target_card_ref",
        "candidate_set_sha256",
        "candidate_owner_count",
        "action",
        "expected_request_revision",
    )
    return _digest(_canonical({field: receipt[field] for field in fields}))


def _validate_rows(connection: sqlite3.Connection, *, org_id: str | None) -> None:
    where, args = ("", ()) if org_id is None else (" WHERE org_id COLLATE BINARY=?", (org_id,))
    votes = connection.execute(f"SELECT * FROM {_VOTES}{where}", args).fetchall()
    vote_by_receipt: dict[str, sqlite3.Row] = {}
    vote_groups: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for row in votes:
        for field in ("conflict_id", "org_id", "request_id", "owner_subject_ref", "target_card_ref"):
            _ref(row[field], field=field, name=f"vote.{field}")
        _integer(row["concurrence_round"], name="vote.concurrence_round", positive=True)
        _sha256(row["candidate_set_sha256"], name="vote.candidate_set_sha256")
        _integer(row["candidate_owner_count"], name="vote.candidate_owner_count", positive=True)
        _ref(row["vote_receipt_id"], field="receipt_id", name="vote.vote_receipt_id")
        _timestamp(row["created_at"], name="vote.created_at")
        _lineage(connection, row, prefix="vote")
        parent = connection.execute(
            "SELECT candidate_set_sha256 FROM durable_linked_conflict_cases WHERE conflict_id COLLATE BINARY=?",
            (row["conflict_id"],),
        ).fetchone()
        if parent is None or parent["candidate_set_sha256"] != row["candidate_set_sha256"]:
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict vote candidate set이 parent Case와 다릅니다.")
        if row["vote_receipt_id"] in vote_by_receipt:
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict vote receipt identity가 중복됩니다.")
        vote_by_receipt[row["vote_receipt_id"]] = row
        vote_groups.setdefault((row["conflict_id"], row["concurrence_round"]), []).append(row)
    receipts = connection.execute(f"SELECT * FROM {_RECEIPTS}{where}", args).fetchall()
    if len(receipts) != len(vote_by_receipt):
        raise SqliteDurableDirectConflictUowSchemaError("direct Conflict vote와 receipt는 정확히 1:1이어야 합니다.")
    receipts_by_id = {row["receipt_id"]: row for row in receipts}
    for row in receipts:
        for field in ("receipt_id", "org_id", "request_id", "conflict_id", "actor_subject_ref", "owner_subject_ref", "target_card_ref"):
            _ref(row[field], field=field, name=f"receipt.{field}")
        _integer(row["concurrence_round"], name="receipt.concurrence_round", positive=True)
        _sha256(row["command_digest"], name="receipt.command_digest")
        _sha256(row["candidate_set_sha256"], name="receipt.candidate_set_sha256")
        _integer(row["candidate_owner_count"], name="receipt.candidate_owner_count", positive=True)
        _enum(row["action"], name="receipt.action", allowed=_ACTION)
        _integer(row["expected_request_revision"], name="receipt.expected_request_revision")
        _timestamp(row["created_at"], name="receipt.created_at")
        _lineage(connection, row, prefix="receipt")
        vote = vote_by_receipt.get(row["receipt_id"])
        vote_fields = (
            "org_id", "request_id", "conflict_id", "concurrence_round",
            "owner_subject_ref", "target_card_ref", "candidate_set_sha256",
            "candidate_owner_count", "created_at",
        )
        if vote is None or any(vote[field] != row[field] for field in vote_fields):
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict receipt가 정확히 하나의 vote와 결속되지 않았습니다.")
        if row["actor_subject_ref"] != row["owner_subject_ref"]:
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict concurrence actor와 Owner가 다릅니다.")
        if row["command_digest"] != _command_digest_for(row):
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict command digest가 canonical command과 다릅니다.")
        mirrors = connection.execute(f"SELECT a.org_id audit_org,a.request_id audit_request,a.action audit_action,a.command_digest audit_digest,a.created_at audit_created,o.org_id outbox_org,o.request_id outbox_request,o.action outbox_action,o.command_digest outbox_digest,o.created_at outbox_created FROM {_AUDIT} a JOIN {_OUTBOX} o ON o.receipt_id=a.receipt_id WHERE a.receipt_id COLLATE BINARY=?", (row["receipt_id"],)).fetchone()
        mirror_fields = {
            "org": "org_id",
            "request": "request_id",
            "action": "action",
            "digest": "command_digest",
            "created": "created_at",
        }
        if mirrors is None or any(
            mirrors[f"{prefix}_{suffix}"] != row[field]
            for prefix in ("audit", "outbox")
            for suffix, field in mirror_fields.items()
        ):
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict receipt intent mirror가 다릅니다.")
        projections = connection.execute(
            f"SELECT * FROM {_RESULTS} WHERE receipt_id COLLATE BINARY=?",
            (row["receipt_id"],),
        ).fetchall()
        if len(projections) != 1:
            raise SqliteDurableDirectConflictUowSchemaError(
                "direct Conflict receipt는 정확히 하나의 result projection을 가져야 합니다."
            )
        projection = projections[0]
        if any(projection[key] != row[key] for key in ("org_id", "request_id", "conflict_id", "concurrence_round", "owner_subject_ref", "target_card_ref", "candidate_set_sha256", "candidate_owner_count", "created_at")):
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict result projection이 receipt와 다릅니다.")
        for field in ("receipt_id", "org_id", "request_id", "conflict_id", "owner_subject_ref", "target_card_ref"):
            _ref(projection[field], field=field, name=f"projection.{field}")
        _enum(projection["result_kind"], name="projection.result_kind", allowed=_RESULT_KIND)
        _sha256(projection["candidate_set_sha256"], name="projection.candidate_set_sha256")
        _integer(projection["candidate_owner_count"], name="projection.candidate_owner_count", positive=True)
        _integer(projection["accepted_vote_count"], name="projection.accepted_vote_count", positive=True)
        _timestamp(projection["created_at"], name="projection.created_at")
    if set(receipts_by_id) != set(vote_by_receipt):
        raise SqliteDurableDirectConflictUowSchemaError("direct Conflict receipt가 없는 vote 또는 vote 없는 receipt가 있습니다.")
    projections = connection.execute(f"SELECT * FROM {_RESULTS}{where}", args).fetchall()
    projection_receipt_ids = [projection["receipt_id"] for projection in projections]
    if (
        len(projection_receipt_ids) != len(set(projection_receipt_ids))
        or len(projections) != len(receipts_by_id)
        or set(projection_receipt_ids) != set(receipts_by_id)
    ):
        raise SqliteDurableDirectConflictUowSchemaError(
            "direct Conflict result projection과 receipt는 정확히 1:1이어야 합니다."
        )
    for (conflict_id, round_), group in vote_groups.items():
        first = group[0]
        if any(
            row["candidate_set_sha256"] != first["candidate_set_sha256"]
            or row["candidate_owner_count"] != first["candidate_owner_count"]
            for row in group
        ) or len(group) > first["candidate_owner_count"]:
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict vote 후보 cardinality가 다릅니다.")
        consensus_receipts = [
            receipt_id for receipt_id, receipt in receipts_by_id.items()
            if receipt["conflict_id"] == conflict_id
            and receipt["concurrence_round"] == round_
            and connection.execute(
                f"SELECT result_kind FROM {_RESULTS} WHERE receipt_id COLLATE BINARY=?", (receipt_id,)
            ).fetchone()["result_kind"] == "consensus_ready"
        ]
        if len(consensus_receipts) > 1:
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict round에는 consensus result가 하나여야 합니다.")
        for receipt_id, receipt in receipts_by_id.items():
            if receipt["conflict_id"] != conflict_id or receipt["concurrence_round"] != round_:
                continue
            projection = connection.execute(f"SELECT * FROM {_RESULTS} WHERE receipt_id COLLATE BINARY=?", (receipt_id,)).fetchone()
            assert projection is not None
            if projection["result_kind"] == "vote_recorded":
                if projection["accepted_vote_count"] != 1:
                    raise SqliteDurableDirectConflictUowSchemaError("direct Conflict vote_recorded count는 1이어야 합니다.")
            else:
                agreeing = sum(1 for vote in group if vote["target_card_ref"] == receipt["target_card_ref"])
                if (
                    len(group) != receipt["candidate_owner_count"]
                    or agreeing != receipt["candidate_owner_count"]
                    or projection["accepted_vote_count"] != receipt["candidate_owner_count"]
                ):
                    raise SqliteDurableDirectConflictUowSchemaError("direct Conflict consensus result의 target/count가 vote 집합과 다릅니다.")


def _validate(connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SqliteDurableDirectConflictUowSchemaError("SQLite foreign_keys=ON이 필요합니다.")
    try:
        validate_sqlite_durable_linked_aggregates_connection(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    except SqliteDurableLinkedAggregatesSchemaError as error:
        raise SqliteDurableDirectConflictUowSchemaError("capable linked aggregate parent가 필요합니다.") from error
    marker, expected = _manifest(connection), _expected_manifest()
    if marker is None or marker["schema_version"] != SQLITE_DURABLE_DIRECT_CONFLICT_UOW_SCHEMA_VERSION or marker["manifest_json"] != expected or marker["manifest_sha256"] != _digest(expected):
        raise SqliteDurableDirectConflictUowSchemaError("direct Conflict manifest가 canonical 기대값과 다릅니다.")
    if _canonical(_catalog(connection)) != expected or connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SqliteDurableDirectConflictUowSchemaError("direct Conflict catalog 또는 foreign key가 canonical과 다릅니다.")
    if reconcile_rows:
        _validate_rows(connection, org_id=org_id)


def validate_sqlite_durable_direct_conflict_uow_connection(connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    finally:
        connection.row_factory = previous


def migrate_sqlite_durable_direct_conflict_uow_schema(db_path: str | Path, *, fault_injector: MigrationFaultInjector | None = None) -> None:
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_sqlite_durable_linked_aggregates_connection(connection)
        except SqliteDurableLinkedAggregatesSchemaError as error:
            raise SqliteDurableDirectConflictUowSchemaError("direct Conflict migration에는 capable linked aggregate parent가 필요합니다.") from error
        if _manifest(connection) is not None:
            _validate(connection)
            connection.commit()
            return
        if any(_exists(connection, table) for table in _OWNED):
            raise SqliteDurableDirectConflictUowSchemaError("manifest 없는 partial direct Conflict schema는 복구하지 않습니다.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteDurableDirectConflictUowSchemaError("migration 전 foreign_key_check가 실패했습니다.")
        for ddl, point in zip(_DDLS, SQLITE_DURABLE_DIRECT_CONFLICT_UOW_MIGRATION_FAULT_POINTS[:5], strict=True):
            connection.execute(ddl)
            if fault_injector is not None:
                fault_injector(point)
        expected = _expected_manifest()
        if _canonical(_catalog(connection)) != expected:
            raise SqliteDurableDirectConflictUowSchemaError("migration 결과 direct Conflict catalog가 canonical과 다릅니다.")
        if fault_injector is not None:
            fault_injector("before_manifest_insert")
        connection.execute("INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES (?,?,?,?)", (SQLITE_DURABLE_DIRECT_CONFLICT_UOW_COMPONENT_ID, SQLITE_DURABLE_DIRECT_CONFLICT_UOW_SCHEMA_VERSION, expected, _digest(expected)))
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
        raise SqliteDurableDirectConflictUowSchemaError("direct Conflict runtime은 기존 SQLite 파일만 엽니다.")
    try:
        return sqlite3.connect(f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode={'ro' if readonly else 'rw'}", uri=True, timeout=5.0)
    except sqlite3.Error as error:
        raise SqliteDurableDirectConflictUowSchemaError("direct Conflict SQLite DB를 열 수 없습니다.") from error


def open_sqlite_durable_direct_conflict_uow_connection(db_path: str | Path, *, org_id: str | None = None) -> sqlite3.Connection:
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
class DurableDirectConflictUowSchemaReconciliationReport:
    capable: bool
    detail: str
    direct_conflict_manifest_present: bool


def reconcile_sqlite_durable_direct_conflict_uow_schema(db_path: str | Path, *, org_id: str | None = None) -> DurableDirectConflictUowSchemaReconciliationReport:
    present = False
    try:
        connection = _open(db_path, readonly=True)
    except SqliteDurableDirectConflictUowSchemaError as error:
        return DurableDirectConflictUowSchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _exists(connection, _MANIFEST) and _manifest(connection) is not None
        _validate(connection, org_id=org_id)
        return DurableDirectConflictUowSchemaReconciliationReport(True, "capable_v1", present)
    except Exception as error:
        return DurableDirectConflictUowSchemaReconciliationReport(False, str(error), present)
    finally:
        connection.close()
