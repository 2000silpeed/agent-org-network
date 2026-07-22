"""S4.3c.2 escalation receipt graph schema(ADR 0065 §8).

receipt가 parent 허브다(R1.0 판, S4.2a evidence-parent 판이 아니다). S4.3b·c.0은
read-only라 escalation 명령 전에는 어떤 durable row도 남기지 않으므로, sealed
cause·graph selection은 명령 시점에 c.3의 한 transaction으로 receipt와 함께 처음
쓰인다. child(sealed evidence·result projection·audit/outbox intent)는
`(org_id, receipt_id)` same-org composite FK로 receipt에 1:1 결박해
cross-org row stitching을 DB 수준(PRAGMA foreign_keys=ON)에서 막는다.

이 컴포넌트는 validate-only 경계다. write API(c.3)는 열지 않는다.
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

from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    SqliteDurableConflictEscalationBaselineSchemaError,
    validate_sqlite_durable_conflict_escalation_baseline_connection,
)

SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_COMPONENT_ID: Final = (
    "durable_conflict_escalation_receipts_v1"
)
SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_SCHEMA_VERSION: Final = 1
SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_MIGRATION_FAULT_POINTS: Final = (
    "after_receipts",
    "after_evidence",
    "after_result_projections",
    "after_audit_intents",
    "after_outbox_intents",
    "before_manifest_insert",
    "after_manifest_insert",
)
type MigrationFaultInjector = Callable[[str], None]


class SqliteDurableConflictEscalationReceiptsSchemaError(RuntimeError):
    """The receipt graph capability is absent, corrupt, or non-canonical."""


_MANIFEST = "schema_component_manifests"
_RECEIPTS = "durable_conflict_escalation_receipts"
_EVIDENCE = "durable_conflict_escalation_evidence"
_RESULTS = "durable_conflict_escalation_result_projections"
_AUDIT = "durable_conflict_escalation_audit_intents"
_OUTBOX = "durable_conflict_escalation_outbox_intents"
_OWNED: Final = (_RECEIPTS, _EVIDENCE, _RESULTS, _AUDIT, _OUTBOX)

_RECEIPTS_DDL = """
CREATE TABLE durable_conflict_escalation_receipts (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 conflict_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 awaiting_revision INTEGER NOT NULL,
 actor_subject_ref TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY,
 resource_fingerprint TEXT NOT NULL COLLATE BINARY,
 approval_evidence_id TEXT NOT NULL COLLATE BINARY,
 escalation_cause_digest TEXT NOT NULL COLLATE BINARY,
 graph_selection_digest TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 UNIQUE(org_id, receipt_id),
 UNIQUE(org_id, command_digest),
 UNIQUE(org_id, conflict_id),
 FOREIGN KEY(conflict_id) REFERENCES durable_linked_conflict_cases(conflict_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_EVIDENCE_DDL = """
CREATE TABLE durable_conflict_escalation_evidence (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 conflict_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 cause_kind TEXT NOT NULL COLLATE BINARY,
 awaiting_revision INTEGER NOT NULL,
 concurrence_round INTEGER NOT NULL,
 candidate_snapshot_sha256 TEXT NOT NULL COLLATE BINARY,
 baseline_sha256 TEXT NOT NULL COLLATE BINARY,
 candidate_claim_sha256 TEXT NOT NULL COLLATE BINARY,
 vote_set_sha256 TEXT NOT NULL COLLATE BINARY,
 candidate_owner_count INTEGER,
 current_candidate_snapshot_sha256 TEXT COLLATE BINARY,
 escalation_cause_digest TEXT NOT NULL COLLATE BINARY,
 graph_selection_digest TEXT NOT NULL COLLATE BINARY,
 manager_subject_ref TEXT COLLATE BINARY,
 root_subject_ref TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(org_id, receipt_id) REFERENCES durable_conflict_escalation_receipts(org_id, receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_RESULTS_DDL = """
CREATE TABLE durable_conflict_escalation_result_projections (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 conflict_id TEXT NOT NULL COLLATE BINARY,
 result_kind TEXT NOT NULL COLLATE BINARY,
 source_kind TEXT NOT NULL COLLATE BINARY,
 target_subject_ref TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(org_id, receipt_id) REFERENCES durable_conflict_escalation_receipts(org_id, receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_AUDIT_DDL = """
CREATE TABLE durable_conflict_escalation_audit_intents (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(org_id, receipt_id) REFERENCES durable_conflict_escalation_receipts(org_id, receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_OUTBOX_DDL = """
CREATE TABLE durable_conflict_escalation_outbox_intents (
 receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 action TEXT NOT NULL COLLATE BINARY,
 command_digest TEXT NOT NULL COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(org_id, receipt_id) REFERENCES durable_conflict_escalation_receipts(org_id, receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_DDLS: Final = (_RECEIPTS_DDL, _EVIDENCE_DDL, _RESULTS_DDL, _AUDIT_DDL, _OUTBOX_DDL)

_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?[+-][0-9]{2}:[0-9]{2}\Z"
)
_KINDS: Final = {
    "receipt_id": "receipt",
    "org_id": "org",
    "conflict_id": "conflict",
    "request_id": "request",
    "actor_subject_ref": "subject",
    "manager_subject_ref": "subject",
    "root_subject_ref": "subject",
    "target_subject_ref": "subject",
    "approval_evidence_id": "evidence",
}
_ACTION: Final = frozenset({"conflict.escalate"})
_CAUSE_KIND: Final = frozenset({"DivergentVotes", "CandidateRegistryChanged"})
_RESULT_KIND: Final = frozenset({"escalated_to_manager", "escalated_to_root"})
_SOURCE_KIND: Final = frozenset({"deadlock"})
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
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            "receipt graph DDL을 읽을 수 없습니다."
        )
    return " ".join(raw.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table in _OWNED:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "receipt graph canonical table이 없습니다."
            )
        tables.append(
            {
                "name": table,
                "ddl": _tokens(row[0]),
                "columns": [
                    tuple(v)
                    for v in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
                ],
                "foreign_keys": [
                    tuple(v)
                    for v in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
                ],
            }
        )
    return {
        "component_id": SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_COMPONENT_ID,
        "component_schema_version": SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_SCHEMA_VERSION,
        "tables": tables,
    }


@lru_cache(maxsize=1)
def _expected_manifest() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE durable_linked_conflict_cases ("
            "conflict_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, request_id TEXT NOT NULL,"
            " awaiting_revision INTEGER NOT NULL, candidate_set_sha256 TEXT NOT NULL)"
        )
        for ddl in _DDLS:
            connection.execute(ddl)
        return _canonical(_catalog(connection))
    finally:
        connection.close()


def _manifest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _exists(connection, _MANIFEST):
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            "공유 schema manifest table이 없습니다."
        )
    return connection.execute(
        "SELECT component_id,schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
        (SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_COMPONENT_ID,),
    ).fetchone()


def _ref(value: object, *, field: str, name: str) -> str:
    kind = _KINDS[field]
    if (
        not isinstance(value, str)
        or not value.startswith(f"{kind}:")
        or _SHA256_RE.fullmatch(value.removeprefix(f"{kind}:")) is None
    ):
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            f"receipt graph {name}은 typed digest reference여야 합니다."
        )
    return value


def _sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            f"receipt graph {name}은 lowercase SHA-256이어야 합니다."
        )
    return value


def _integer(value: object, *, name: str, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > _INTEGER_MAX:
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            f"receipt graph {name} 정수가 올바르지 않습니다."
        )
    return value


def _timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            f"receipt graph {name} timestamp가 올바르지 않습니다."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            f"receipt graph {name} calendar timestamp가 올바르지 않습니다."
        ) from error
    precision = "microseconds" if parsed.microsecond else "seconds"
    if parsed.utcoffset() is None or parsed.isoformat(timespec=precision) != value:
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            f"receipt graph {name} timestamp가 canonical이 아닙니다."
        )
    return value


def _enum(value: object, *, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            f"receipt graph {name} enum이 올바르지 않습니다."
        )
    return value


def _cause_digest_for(evidence: sqlite3.Row) -> str:
    """S4.3b sealed cause 필드로 `escalation_cause_digest`를 재계산한다(자기 정합).

    c.1 `conflict_escalation_approval_evidence.escalation_cause_digest`와 같은
    canonical 필드(키 순서는 sort_keys라 무관)로 도메인-local 재현한다 — c.1
    모듈을 import하지 않는다(ADR 0065 §4/§8 no-import 규율).
    """
    if evidence["cause_kind"] == "DivergentVotes":
        variant: dict[str, object] = {"candidate_owner_count": evidence["candidate_owner_count"]}
    else:
        variant = {
            "current_candidate_snapshot_sha256": evidence["current_candidate_snapshot_sha256"]
        }
    return _digest(
        _canonical(
            {
                "kind": evidence["cause_kind"],
                "org_ref": evidence["org_id"],
                "conflict_ref": evidence["conflict_id"],
                "request_ref": evidence["request_id"],
                "awaiting_revision": evidence["awaiting_revision"],
                "concurrence_round": evidence["concurrence_round"],
                "candidate_snapshot_sha256": evidence["candidate_snapshot_sha256"],
                "baseline_sha256": evidence["baseline_sha256"],
                "candidate_claim_sha256": evidence["candidate_claim_sha256"],
                "vote_set_sha256": evidence["vote_set_sha256"],
                **variant,
            }
        )
    )


def _validate_rows(connection: sqlite3.Connection, *, org_id: str | None) -> None:
    where, args = ("", ()) if org_id is None else (" WHERE org_id COLLATE BINARY=?", (org_id,))
    receipts = connection.execute(f"SELECT * FROM {_RECEIPTS}{where}", args).fetchall()
    for row in receipts:
        for field in (
            "receipt_id",
            "org_id",
            "conflict_id",
            "request_id",
            "actor_subject_ref",
            "approval_evidence_id",
        ):
            _ref(row[field], field=field, name=f"receipt.{field}")
        _integer(row["awaiting_revision"], name="receipt.awaiting_revision")
        for field in (
            "command_digest",
            "resource_fingerprint",
            "escalation_cause_digest",
            "graph_selection_digest",
        ):
            _sha256(row[field], name=f"receipt.{field}")
        _enum(row["action"], name="receipt.action", allowed=_ACTION)
        _timestamp(row["created_at"], name="receipt.created_at")

        case = connection.execute(
            "SELECT org_id,request_id,awaiting_revision,candidate_set_sha256 FROM durable_linked_conflict_cases WHERE conflict_id COLLATE BINARY=?",
            (row["conflict_id"],),
        ).fetchone()
        request = connection.execute(
            "SELECT org_id FROM question_requests WHERE request_id COLLATE BINARY=?",
            (row["request_id"],),
        ).fetchone()
        if (
            case is None
            or request is None
            or case["org_id"] != row["org_id"]
            or case["request_id"] != row["request_id"]
            or case["awaiting_revision"] != row["awaiting_revision"]
            or request["org_id"] != row["org_id"]
        ):
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "receipt Conflict/Request lineage가 다릅니다."
            )

        evidences = connection.execute(
            f"SELECT * FROM {_EVIDENCE} WHERE receipt_id COLLATE BINARY=?", (row["receipt_id"],)
        ).fetchall()
        if len(evidences) != 1:
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "receipt에는 정확히 하나의 sealed evidence가 필요합니다."
            )
        evidence = evidences[0]
        for field in ("receipt_id", "org_id", "conflict_id", "request_id"):
            _ref(evidence[field], field=field, name=f"evidence.{field}")
        if (
            evidence["org_id"] != row["org_id"]
            or evidence["conflict_id"] != row["conflict_id"]
            or evidence["request_id"] != row["request_id"]
            or evidence["awaiting_revision"] != row["awaiting_revision"]
        ):
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "evidence가 receipt/Case lineage와 mirror되지 않습니다."
            )
        _integer(evidence["awaiting_revision"], name="evidence.awaiting_revision")
        _integer(evidence["concurrence_round"], name="evidence.concurrence_round", positive=True)
        if evidence["concurrence_round"] != evidence["awaiting_revision"] + 1:
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "evidence concurrence_round가 awaiting_revision과 다릅니다."
            )
        _sha256(evidence["candidate_snapshot_sha256"], name="evidence.candidate_snapshot_sha256")
        if evidence["candidate_snapshot_sha256"] != case["candidate_set_sha256"]:
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "evidence candidate snapshot이 Conflict Case와 다릅니다."
            )
        _sha256(evidence["baseline_sha256"], name="evidence.baseline_sha256")
        _sha256(evidence["candidate_claim_sha256"], name="evidence.candidate_claim_sha256")
        _sha256(evidence["vote_set_sha256"], name="evidence.vote_set_sha256")
        _enum(evidence["cause_kind"], name="evidence.cause_kind", allowed=_CAUSE_KIND)
        if evidence["cause_kind"] == "DivergentVotes":
            if evidence["current_candidate_snapshot_sha256"] is not None:
                raise SqliteDurableConflictEscalationReceiptsSchemaError(
                    "DivergentVotes evidence에는 current candidate snapshot이 없어야 합니다."
                )
            _integer(
                evidence["candidate_owner_count"],
                name="evidence.candidate_owner_count",
                positive=True,
            )
        else:
            if evidence["candidate_owner_count"] is not None:
                raise SqliteDurableConflictEscalationReceiptsSchemaError(
                    "CandidateRegistryChanged evidence에는 candidate_owner_count가 없어야 합니다."
                )
            _sha256(
                evidence["current_candidate_snapshot_sha256"],
                name="evidence.current_candidate_snapshot_sha256",
            )
        _sha256(evidence["escalation_cause_digest"], name="evidence.escalation_cause_digest")
        _sha256(evidence["graph_selection_digest"], name="evidence.graph_selection_digest")
        if evidence["graph_selection_digest"] != row["graph_selection_digest"]:
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "evidence graph_selection_digest가 receipt와 다릅니다."
            )
        computed_cause_digest = _cause_digest_for(evidence)
        if (
            evidence["escalation_cause_digest"] != computed_cause_digest
            or row["escalation_cause_digest"] != computed_cause_digest
        ):
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "escalation_cause_digest가 canonical sealed cause와 다릅니다."
            )
        root_ref = _ref(
            evidence["root_subject_ref"], field="root_subject_ref", name="evidence.root_subject_ref"
        )
        manager_ref: str | None = None
        if evidence["manager_subject_ref"] is not None:
            manager_ref = _ref(
                evidence["manager_subject_ref"],
                field="manager_subject_ref",
                name="evidence.manager_subject_ref",
            )
        _timestamp(evidence["created_at"], name="evidence.created_at")

        projections = connection.execute(
            f"SELECT * FROM {_RESULTS} WHERE receipt_id COLLATE BINARY=?", (row["receipt_id"],)
        ).fetchall()
        if len(projections) != 1:
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "receipt에는 정확히 하나의 result projection이 필요합니다."
            )
        projection = projections[0]
        _ref(projection["receipt_id"], field="receipt_id", name="projection.receipt_id")
        if projection["org_id"] != row["org_id"] or projection["conflict_id"] != row["conflict_id"]:
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "result projection이 receipt와 mirror되지 않습니다."
            )
        _enum(projection["source_kind"], name="projection.source_kind", allowed=_SOURCE_KIND)
        _enum(projection["result_kind"], name="projection.result_kind", allowed=_RESULT_KIND)
        target_ref = _ref(
            projection["target_subject_ref"],
            field="target_subject_ref",
            name="projection.target_subject_ref",
        )
        if projection["result_kind"] == "escalated_to_manager":
            if manager_ref is None or target_ref != manager_ref:
                raise SqliteDurableConflictEscalationReceiptsSchemaError(
                    "escalated_to_manager result target이 sealed manager와 다릅니다."
                )
        else:
            if manager_ref is not None or target_ref != root_ref:
                raise SqliteDurableConflictEscalationReceiptsSchemaError(
                    "escalated_to_root result target이 sealed root와 다르거나 manager가 남아있습니다."
                )
        _timestamp(projection["created_at"], name="projection.created_at")

        for table, label in ((_AUDIT, "audit"), (_OUTBOX, "outbox")):
            mirrors = connection.execute(
                f"SELECT * FROM {table} WHERE receipt_id COLLATE BINARY=?", (row["receipt_id"],)
            ).fetchall()
            if len(mirrors) != 1:
                raise SqliteDurableConflictEscalationReceiptsSchemaError(
                    f"receipt에는 정확히 하나의 {label} intent가 필요합니다."
                )
            mirror = mirrors[0]
            if (
                mirror["org_id"] != row["org_id"]
                or mirror["action"] != row["action"]
                or mirror["command_digest"] != row["command_digest"]
            ):
                raise SqliteDurableConflictEscalationReceiptsSchemaError(
                    f"{label} intent가 receipt와 mirror되지 않습니다."
                )
            _timestamp(mirror["created_at"], name=f"{label}.created_at")
    # A globally corrupt table/catalog is rejected above; unrelated organization
    # rows deliberately remain opaque during scoped row reconciliation.


def _validate(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            "SQLite foreign_keys=ON이 필요합니다."
        )
    try:
        validate_sqlite_durable_conflict_escalation_baseline_connection(
            connection, org_id=org_id, reconcile_rows=reconcile_rows
        )
    except SqliteDurableConflictEscalationBaselineSchemaError as error:
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            "receipt graph에는 capable escalation baseline parent가 필요합니다."
        ) from error
    marker, expected = _manifest(connection), _expected_manifest()
    if (
        marker is None
        or marker["schema_version"] != SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_SCHEMA_VERSION
        or marker["manifest_json"] != expected
        or marker["manifest_sha256"] != _digest(expected)
    ):
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            "receipt graph manifest가 canonical 기대값과 다릅니다."
        )
    if (
        _canonical(_catalog(connection)) != expected
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            "receipt graph catalog 또는 foreign key가 canonical과 다릅니다."
        )
    if reconcile_rows:
        _validate_rows(connection, org_id=org_id)


def validate_sqlite_durable_conflict_escalation_receipts_connection(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    finally:
        connection.row_factory = previous


def migrate_sqlite_durable_conflict_escalation_receipts_schema(
    db_path: str | Path, *, fault_injector: MigrationFaultInjector | None = None
) -> None:
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_sqlite_durable_conflict_escalation_baseline_connection(connection)
        except SqliteDurableConflictEscalationBaselineSchemaError as error:
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "receipt graph migration에는 capable escalation baseline parent가 필요합니다."
            ) from error
        if _manifest(connection) is not None:
            _validate(connection)
            connection.commit()
            return
        if any(_exists(connection, table) for table in _OWNED):
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "manifest 없는 partial receipt graph schema는 복구하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "migration 전 foreign_key_check가 실패했습니다."
            )
        for ddl, point in zip(
            _DDLS,
            SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_MIGRATION_FAULT_POINTS[:5],
            strict=True,
        ):
            connection.execute(ddl)
            if fault_injector:
                fault_injector(point)
        expected = _expected_manifest()
        if _canonical(_catalog(connection)) != expected:
            raise SqliteDurableConflictEscalationReceiptsSchemaError(
                "migration 결과 receipt graph catalog가 canonical과 다릅니다."
            )
        if fault_injector:
            fault_injector("before_manifest_insert")
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES (?,?,?,?)",
            (
                SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_COMPONENT_ID,
                SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_SCHEMA_VERSION,
                expected,
                _digest(expected),
            ),
        )
        if fault_injector:
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
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            "receipt graph runtime은 기존 SQLite 파일만 엽니다."
        )
    try:
        return sqlite3.connect(
            f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode={'ro' if readonly else 'rw'}",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as error:
        raise SqliteDurableConflictEscalationReceiptsSchemaError(
            "receipt graph SQLite DB를 열 수 없습니다."
        ) from error


def open_sqlite_durable_conflict_escalation_receipts_connection(
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
class DurableConflictEscalationReceiptsSchemaReconciliationReport:
    capable: bool
    detail: str
    escalation_receipts_manifest_present: bool


def reconcile_sqlite_durable_conflict_escalation_receipts_schema(
    db_path: str | Path, *, org_id: str | None = None
) -> DurableConflictEscalationReceiptsSchemaReconciliationReport:
    present = False
    try:
        connection = _open(db_path, readonly=True)
    except SqliteDurableConflictEscalationReceiptsSchemaError as error:
        return DurableConflictEscalationReceiptsSchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _exists(connection, _MANIFEST) and _manifest(connection) is not None
        _validate(connection, org_id=org_id)
        return DurableConflictEscalationReceiptsSchemaReconciliationReport(
            True, "capable_v1", present
        )
    except Exception as error:
        return DurableConflictEscalationReceiptsSchemaReconciliationReport(
            False, str(error), present
        )
    finally:
        connection.close()
