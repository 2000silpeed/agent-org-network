"""S4.3a immutable Conflict escalation-baseline schema capability.

This component is intentionally a validate-only evidence boundary.  It neither
creates Conflict Cases nor authorizes/escalates them.  In particular the
Registry has no typed ``under_claim`` or manager/root relation snapshot
contract yet, so no inferred or raw selection material is persisted here.
Those future dimensions are explicitly unavailable.
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

SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_COMPONENT_ID: Final = (
    "durable_conflict_escalation_baseline_v1"
)
SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_SCHEMA_VERSION: Final = 1
SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_MIGRATION_FAULT_POINTS: Final = (
    "after_baselines",
    "after_candidates",
    "before_manifest_insert",
    "after_manifest_insert",
)
# Deliberate contract boundary, rather than a nullable/raw column.  A later
# snapshot contract may introduce a new approved component version.
CONFLICT_ESCALATION_UNDER_CLAIM_AVAILABLE: Final = False
CONFLICT_ESCALATION_MANAGER_SELECTION_AVAILABLE: Final = False
type MigrationFaultInjector = Callable[[str], None]


class SqliteDurableConflictEscalationBaselineSchemaError(RuntimeError):
    """The immutable baseline capability is absent, corrupt, or non-canonical."""


_MANIFEST = "schema_component_manifests"
_BASELINES = "durable_conflict_escalation_baselines"
_CANDIDATES = "durable_conflict_escalation_baseline_candidates"
_OWNED: Final = (_BASELINES, _CANDIDATES)
_BASELINES_DDL = """
CREATE TABLE durable_conflict_escalation_baselines (
 baseline_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
 conflict_id TEXT NOT NULL UNIQUE COLLATE BINARY,
 org_id TEXT NOT NULL COLLATE BINARY,
 request_id TEXT NOT NULL COLLATE BINARY,
 awaiting_revision INTEGER NOT NULL,
 candidate_set_sha256 TEXT NOT NULL COLLATE BINARY,
 candidate_count INTEGER NOT NULL,
 baseline_sha256 TEXT NOT NULL UNIQUE COLLATE BINARY,
 created_at TEXT NOT NULL,
 FOREIGN KEY(conflict_id) REFERENCES durable_linked_conflict_cases(conflict_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_CANDIDATES_DDL = """
CREATE TABLE durable_conflict_escalation_baseline_candidates (
 baseline_id TEXT NOT NULL COLLATE BINARY,
 candidate_ordinal INTEGER NOT NULL,
 candidate_card_ref TEXT NOT NULL COLLATE BINARY,
 candidate_owner_subject_ref TEXT NOT NULL COLLATE BINARY,
 candidate_domain_ref TEXT NOT NULL COLLATE BINARY,
 candidate_route_sha256 TEXT NOT NULL COLLATE BINARY,
 PRIMARY KEY(baseline_id,candidate_ordinal),
 FOREIGN KEY(baseline_id) REFERENCES durable_conflict_escalation_baselines(baseline_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_DDLS: Final = (_BASELINES_DDL, _CANDIDATES_DDL)
_SHA: Final = re.compile(r"[0-9a-f]{64}\Z")
_TIME: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?[+-][0-9]{2}:[0-9]{2}\Z"
)
_KINDS: Final = {
    "baseline_id": "baseline",
    "conflict_id": "conflict",
    "org_id": "org",
    "request_id": "request",
    "candidate_card_ref": "card",
    "candidate_owner_subject_ref": "subject",
    "candidate_domain_ref": "domain",
}
_MAX: Final = 2**63 - 1


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
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "escalation baseline DDL을 읽을 수 없습니다."
        )
    return " ".join(raw.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table in _OWNED:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise SqliteDurableConflictEscalationBaselineSchemaError(
                "escalation baseline canonical table이 없습니다."
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
        "component_id": SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_COMPONENT_ID,
        "component_schema_version": SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_SCHEMA_VERSION,
        "tables": tables,
    }


@lru_cache(maxsize=1)
def _expected_manifest() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        connection.execute(
            "CREATE TABLE durable_linked_conflict_cases (conflict_id TEXT PRIMARY KEY NOT NULL)"
        )
        for ddl in _DDLS:
            connection.execute(ddl)
        return _canonical(_catalog(connection))
    finally:
        connection.close()


def _manifest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _exists(connection, _MANIFEST):
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "공유 schema manifest table이 없습니다."
        )
    return connection.execute(
        "SELECT component_id,schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
        (SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_COMPONENT_ID,),
    ).fetchone()


def _ref(value: object, *, field: str) -> str:
    kind = _KINDS[field]
    if (
        not isinstance(value, str)
        or not value.startswith(f"{kind}:")
        or _SHA.fullmatch(value.removeprefix(f"{kind}:")) is None
    ):
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            f"baseline {field}은 typed digest reference여야 합니다."
        )
    return value


def _sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            f"baseline {field}은 lowercase SHA-256이어야 합니다."
        )
    return value


def _integer(value: object, *, field: str, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > _MAX:
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            f"baseline {field} 정수가 올바르지 않습니다."
        )
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "baseline created_at timestamp가 올바르지 않습니다."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "baseline created_at calendar timestamp가 올바르지 않습니다."
        ) from error
    precision = "microseconds" if parsed.microsecond else "seconds"
    if parsed.utcoffset() is None or parsed.isoformat(timespec=precision) != value:
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "baseline created_at timestamp가 canonical이 아닙니다."
        )
    return value


def _baseline_digest(row: sqlite3.Row, candidates: list[sqlite3.Row]) -> str:
    # No under_claim field appears in this identity; escalation using that
    # dimension is unavailable until its independently typed contract exists.
    fields = (
        "conflict_id",
        "org_id",
        "request_id",
        "awaiting_revision",
        "candidate_set_sha256",
        "candidate_count",
        "created_at",
    )
    return _digest(
        _canonical(
            {
                "baseline": {field: row[field] for field in fields},
                "candidates": [
                    {
                        field: candidate[field]
                        for field in (
                            "candidate_ordinal",
                            "candidate_card_ref",
                            "candidate_owner_subject_ref",
                            "candidate_domain_ref",
                            "candidate_route_sha256",
                        )
                    }
                    for candidate in candidates
                ],
            }
        )
    )


def _validate_rows(connection: sqlite3.Connection, *, org_id: str | None) -> None:
    where, args = ("", ()) if org_id is None else (" WHERE org_id COLLATE BINARY=?", (org_id,))
    baselines = connection.execute(f"SELECT * FROM {_BASELINES}{where}", args).fetchall()
    for row in baselines:
        for field in ("baseline_id", "conflict_id", "org_id", "request_id"):
            _ref(row[field], field=field)
        _integer(row["awaiting_revision"], field="awaiting_revision")
        _sha(row["candidate_set_sha256"], field="candidate_set_sha256")
        count = _integer(row["candidate_count"], field="candidate_count", positive=True)
        _sha(row["baseline_sha256"], field="baseline_sha256")
        _timestamp(row["created_at"])
        parent = connection.execute(
            "SELECT org_id,request_id,awaiting_revision,candidate_set_sha256,created_at FROM durable_linked_conflict_cases WHERE conflict_id COLLATE BINARY=?",
            (row["conflict_id"],),
        ).fetchone()
        request = connection.execute(
            "SELECT org_id FROM question_requests WHERE request_id COLLATE BINARY=?",
            (row["request_id"],),
        ).fetchone()
        if (
            parent is None
            or request is None
            or any(
                parent[field] != row[field]
                for field in ("org_id", "request_id", "awaiting_revision", "candidate_set_sha256")
            )
            or request["org_id"] != row["org_id"]
            or parent["created_at"] != row["created_at"]
        ):
            raise SqliteDurableConflictEscalationBaselineSchemaError(
                "baseline Conflict/Request lineage가 다릅니다."
            )
        candidates = connection.execute(
            f"SELECT * FROM {_CANDIDATES} WHERE baseline_id COLLATE BINARY=? ORDER BY candidate_ordinal",
            (row["baseline_id"],),
        ).fetchall()
        if len(candidates) != count:
            raise SqliteDurableConflictEscalationBaselineSchemaError(
                "baseline candidate cardinality가 다릅니다."
            )
        for ordinal, candidate in enumerate(candidates, start=1):
            _ref(candidate["baseline_id"], field="baseline_id")
            if (
                _integer(candidate["candidate_ordinal"], field="candidate_ordinal", positive=True)
                != ordinal
            ):
                raise SqliteDurableConflictEscalationBaselineSchemaError(
                    "baseline candidate 순서가 contiguous하지 않습니다."
                )
            for field in (
                "candidate_card_ref",
                "candidate_owner_subject_ref",
                "candidate_domain_ref",
            ):
                _ref(candidate[field], field=field)
            _sha(candidate["candidate_route_sha256"], field="candidate_route_sha256")
        if row["baseline_sha256"] != _baseline_digest(row, candidates):
            raise SqliteDurableConflictEscalationBaselineSchemaError(
                "baseline digest가 canonical snapshot과 다릅니다."
            )
    # A globally corrupt table/catalog is rejected above; unrelated organization
    # rows deliberately remain opaque during scoped row reconciliation.


def _validate(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "SQLite foreign_keys=ON이 필요합니다."
        )
    try:
        validate_sqlite_durable_linked_aggregates_connection(
            connection, org_id=org_id, reconcile_rows=reconcile_rows
        )
    except SqliteDurableLinkedAggregatesSchemaError as error:
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "baseline에는 capable Completion+linked aggregate parent가 필요합니다."
        ) from error
    marker, expected = _manifest(connection), _expected_manifest()
    if (
        marker is None
        or marker["schema_version"] != SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_SCHEMA_VERSION
        or marker["manifest_json"] != expected
        or marker["manifest_sha256"] != _digest(expected)
    ):
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "baseline manifest가 canonical 기대값과 다릅니다."
        )
    if (
        _canonical(_catalog(connection)) != expected
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "baseline catalog 또는 foreign key가 canonical과 다릅니다."
        )
    if reconcile_rows:
        _validate_rows(connection, org_id=org_id)


def validate_sqlite_durable_conflict_escalation_baseline_connection(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    finally:
        connection.row_factory = previous


def migrate_sqlite_durable_conflict_escalation_baseline_schema(
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
            raise SqliteDurableConflictEscalationBaselineSchemaError(
                "baseline migration에는 capable Completion+linked aggregate parent가 필요합니다."
            ) from error
        if _manifest(connection) is not None:
            _validate(connection)
            connection.commit()
            return
        if any(_exists(connection, table) for table in _OWNED):
            raise SqliteDurableConflictEscalationBaselineSchemaError(
                "manifest 없는 partial baseline schema는 복구하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteDurableConflictEscalationBaselineSchemaError(
                "migration 전 foreign_key_check가 실패했습니다."
            )
        for ddl, point in zip(
            _DDLS,
            SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_MIGRATION_FAULT_POINTS[:2],
            strict=True,
        ):
            connection.execute(ddl)
            if fault_injector:
                fault_injector(point)
        expected = _expected_manifest()
        if _canonical(_catalog(connection)) != expected:
            raise SqliteDurableConflictEscalationBaselineSchemaError(
                "migration 결과 baseline catalog가 canonical과 다릅니다."
            )
        if fault_injector:
            fault_injector("before_manifest_insert")
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES (?,?,?,?)",
            (
                SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_COMPONENT_ID,
                SQLITE_DURABLE_CONFLICT_ESCALATION_BASELINE_SCHEMA_VERSION,
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
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "baseline runtime은 기존 SQLite 파일만 엽니다."
        )
    try:
        return sqlite3.connect(
            f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode={'ro' if readonly else 'rw'}",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as error:
        raise SqliteDurableConflictEscalationBaselineSchemaError(
            "baseline SQLite DB를 열 수 없습니다."
        ) from error


def open_sqlite_durable_conflict_escalation_baseline_connection(
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
class DurableConflictEscalationBaselineSchemaReconciliationReport:
    capable: bool
    detail: str
    escalation_baseline_manifest_present: bool
    under_claim_drift_available: bool = False
    manager_selection_available: bool = False


def reconcile_sqlite_durable_conflict_escalation_baseline_schema(
    db_path: str | Path, *, org_id: str | None = None
) -> DurableConflictEscalationBaselineSchemaReconciliationReport:
    present = False
    try:
        connection = _open(db_path, readonly=True)
    except SqliteDurableConflictEscalationBaselineSchemaError as error:
        return DurableConflictEscalationBaselineSchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _exists(connection, _MANIFEST) and _manifest(connection) is not None
        _validate(connection, org_id=org_id)
        return DurableConflictEscalationBaselineSchemaReconciliationReport(
            True,
            "capable_v1_under_claim_and_manager_selection_unavailable",
            present,
        )
    except Exception as error:
        return DurableConflictEscalationBaselineSchemaReconciliationReport(
            False, str(error), present
        )
    finally:
        connection.close()
