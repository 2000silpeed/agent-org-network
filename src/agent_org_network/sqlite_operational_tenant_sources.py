"""ADR 0053 SQLite tenant operational-source schema capability (S2 only)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, cast

COMPONENT_ID: Final = "operational_tenant_sources_v1"
SCHEMA_VERSION: Final = 1
TABLES: Final = (
    "operational_registry_state",
    "operational_sessions",
    "operational_audit_records",
    "operational_hitl_toggles",
)
type FaultInjector = Callable[[str], None]
_CANONICAL_TIMESTAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class SqliteOperationalTenantSourcesError(RuntimeError):
    pass


_DDL: Final = (
    "CREATE TABLE operational_registry_state (org_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY, revision INTEGER NOT NULL CHECK(revision >= 0), payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL COLLATE BINARY, updated_at TEXT NOT NULL)",
    "CREATE TABLE operational_sessions (org_id TEXT NOT NULL COLLATE BINARY, session_id TEXT NOT NULL COLLATE BINARY, user_id TEXT NOT NULL COLLATE BINARY, status TEXT NOT NULL COLLATE BINARY, started_at TEXT NOT NULL, last_active_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 0), PRIMARY KEY(org_id, session_id))",
    "CREATE TABLE operational_audit_records (org_id TEXT NOT NULL COLLATE BINARY, seq INTEGER NOT NULL CHECK(seq >= 0), record_json TEXT NOT NULL, record_digest TEXT NOT NULL COLLATE BINARY, created_at TEXT NOT NULL, PRIMARY KEY(org_id, seq))",
    "CREATE TABLE operational_hitl_toggles (org_id TEXT NOT NULL COLLATE BINARY, agent_id TEXT NOT NULL COLLATE BINARY, \"on\" INTEGER NOT NULL CHECK(\"on\" IN (0,1)), explicit INTEGER NOT NULL CHECK(explicit IN (0,1)), revision INTEGER NOT NULL CHECK(revision >= 0), updated_at TEXT NOT NULL, PRIMARY KEY(org_id, agent_id))",
)
_MANIFEST_DDL: Final = """CREATE TABLE schema_component_manifests (
    component_id      TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    schema_version    INTEGER NOT NULL,
    manifest_json     TEXT NOT NULL,
    manifest_sha256   TEXT NOT NULL
)"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fault(injector: FaultInjector | None, point: str) -> None:
    if injector:
        injector(point)


def _same_ddl(actual: str, expected: str) -> bool:
    return " ".join(actual.split()) == " ".join(expected.split())


def _validate_manifest_catalog(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schema_component_manifests'"
    ).fetchone()
    if row is None or not _same_ddl(row[0], _MANIFEST_DDL):
        raise SqliteOperationalTenantSourcesError("공유 manifest catalog가 canonical하지 않습니다.")


def migrate_sqlite_operational_tenant_sources(connection: sqlite3.Connection, *, fault_injector: FaultInjector | None = None) -> None:
    """명시 migration; 오류 시 owned DDL/manifest 모두 rollback한다."""
    existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "schema_component_manifests" in existing:
        _validate_manifest_catalog(connection)
    if set(TABLES) & existing:
        if "schema_component_manifests" not in existing:
            raise SqliteOperationalTenantSourcesError("manifest 없는 partial schema는 복구하지 않습니다.")
        marker = connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone()
        if marker is None:
            raise SqliteOperationalTenantSourcesError("manifest 없는 partial schema는 복구하지 않습니다.")
        _validate(connection)
        return
    try:
        connection.execute("BEGIN IMMEDIATE")
        if "schema_component_manifests" not in existing:
            connection.execute(_MANIFEST_DDL)
        for index, ddl in enumerate(_DDL):
            connection.execute(ddl)
            _fault(fault_injector, f"after_table_{index}")
        manifest = _json({"component_id": COMPONENT_ID, "version": SCHEMA_VERSION, "tables": TABLES})
        connection.execute("INSERT INTO schema_component_manifests VALUES (?, ?, ?, ?)", (COMPONENT_ID, SCHEMA_VERSION, manifest, _digest(manifest)))
        _fault(fault_injector, "after_manifest")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _validate(connection: sqlite3.Connection) -> None:
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(TABLES).issubset(names) or "schema_component_manifests" not in names:
        raise SqliteOperationalTenantSourcesError("tenant operational schema가 없습니다.")
    _validate_manifest_catalog(connection)
    row = connection.execute("SELECT schema_version, manifest_json, manifest_sha256 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)).fetchone()
    expected = _json({"component_id": COMPONENT_ID, "version": SCHEMA_VERSION, "tables": TABLES})
    if row is None or row[0] != SCHEMA_VERSION or row[1] != expected or row[2] != _digest(expected):
        raise SqliteOperationalTenantSourcesError("tenant operational manifest가 canonical하지 않습니다.")
    for table, ddl in zip(TABLES, _DDL, strict=True):
        actual = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if actual is None or " ".join(actual[0].split()) != " ".join(ddl.split()):
            raise SqliteOperationalTenantSourcesError("tenant operational catalog가 canonical하지 않습니다.")


@dataclass(frozen=True)
class SqliteOperationalTenantSourcesCapability:
    connection: sqlite3.Connection
    def validate_only(self) -> None: _validate(self.connection)

    def registry(self, org_id: str) -> "SqliteTenantRegistryRepository":
        return SqliteTenantRegistryRepository(self.connection, org_id)

    def sessions(self, org_id: str) -> "SqliteTenantSessionRepository":
        return SqliteTenantSessionRepository(self.connection, org_id)

    def audit(self, org_id: str) -> "SqliteTenantAuditRepository":
        return SqliteTenantAuditRepository(self.connection, org_id)

    def hitl(self, org_id: str) -> "SqliteTenantHitlRepository":
        return SqliteTenantHitlRepository(self.connection, org_id)


def open_sqlite_operational_tenant_sources(connection: sqlite3.Connection) -> SqliteOperationalTenantSourcesCapability:
    _validate(connection)
    return SqliteOperationalTenantSourcesCapability(connection)


def _org(org_id: str) -> str:
    if type(org_id) is not str or not org_id.strip():
        raise SqliteOperationalTenantSourcesError("tenant org가 유효하지 않습니다.")
    return org_id


def _timestamp(value: object) -> str:
    if type(value) is not str or _CANONICAL_TIMESTAMP.fullmatch(value) is None:
        raise SqliteOperationalTenantSourcesError("timestamp가 canonical하지 않습니다.")
    # strptime catches impossible dates; milliseconds are exact because regex permits three only.
    from datetime import UTC, datetime

    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise SqliteOperationalTenantSourcesError("timestamp가 canonical하지 않습니다.") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z" != value:
        raise SqliteOperationalTenantSourcesError("timestamp가 canonical하지 않습니다.")
    return value


def _revision(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise SqliteOperationalTenantSourcesError(f"{label} revision이 유효하지 않습니다.")
    return value


def _strict_registry_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise SqliteOperationalTenantSourcesError("registry payload가 strict graph가 아닙니다.")
    value = cast(dict[str, object], payload)
    if set(value) != {"users", "cards", "manager_refs"}:
        raise SqliteOperationalTenantSourcesError("registry payload가 strict graph가 아닙니다.")
    users, cards, refs = value["users"], value["cards"], value["manager_refs"]
    if not isinstance(users, list) or not isinstance(cards, dict) or not isinstance(refs, dict):
        raise SqliteOperationalTenantSourcesError("registry graph field가 유효하지 않습니다.")
    users = cast(list[object], users)
    cards = cast(dict[object, object], cards)
    refs = cast(dict[object, object], refs)
    user_ids = set(users)
    if any(type(value) is not str or not value for value in users):
        raise SqliteOperationalTenantSourcesError("registry user가 유효하지 않습니다.")
    for card_id, card in cards.items():
        if type(card_id) is not str or not isinstance(card, dict):
            raise SqliteOperationalTenantSourcesError("registry card가 strict admission을 만족하지 않습니다.")
        card_value = cast(dict[str, object], card)
        if set(card_value) != {"owner"}:
            raise SqliteOperationalTenantSourcesError("registry card가 strict admission을 만족하지 않습니다.")
        if card_value["owner"] not in user_ids:
            raise SqliteOperationalTenantSourcesError("registry card owner reference가 없습니다.")
    for manager, target in refs.items():
        if type(manager) is not str or type(target) is not str or manager == target or target not in cards:
            raise SqliteOperationalTenantSourcesError("registry manager reference가 유효하지 않습니다.")
    for start in refs:
        seen: set[object] = set()
        current: object = start
        while current in refs:
            if current in seen:
                raise SqliteOperationalTenantSourcesError("registry manager cycle이 허용되지 않습니다.")
            seen.add(current)
            current = refs[current]
    return value


class SqliteTenantRegistryRepository:
    def __init__(self, connection: sqlite3.Connection, org_id: str) -> None:
        self._connection = connection
        self._org_id = _org(org_id)

    def read(self) -> tuple[int, dict[str, object]] | None:
        _validate(self._connection)
        row = self._connection.execute(
            "SELECT revision, payload_json, payload_digest, updated_at FROM operational_registry_state WHERE org_id=?",
            (self._org_id,),
        ).fetchone()
        if row is None:
            return None
        if type(row[0]) is not int or row[0] < 0:
            raise SqliteOperationalTenantSourcesError("registry revision이 손상되었습니다.")
        if _digest(row[1]) != row[2]:
            raise SqliteOperationalTenantSourcesError("registry digest가 손상되었습니다.")
        _timestamp(row[3])
        try:
            payload = json.loads(row[1])
        except (TypeError, ValueError) as error:
            raise SqliteOperationalTenantSourcesError("registry JSON이 손상되었습니다.") from error
        return row[0], _strict_registry_payload(payload)

    def compare_and_set(self, expected_revision: int | None, payload: object, updated_at: str) -> bool:
        _validate(self._connection)
        if expected_revision is not None:
            _revision(expected_revision, "expected registry")
        _timestamp(updated_at)
        graph = _strict_registry_payload(payload)
        encoded = _json(graph)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.read()
            if (current is None and expected_revision is not None) or (
                current is not None and current[0] != expected_revision
            ):
                self._connection.rollback()
                return False
            revision = 0 if current is None else current[0] + 1
            self._connection.execute(
                "INSERT INTO operational_registry_state VALUES (?, ?, ?, ?, ?) ON CONFLICT(org_id) DO UPDATE SET revision=excluded.revision, payload_json=excluded.payload_json, payload_digest=excluded.payload_digest, updated_at=excluded.updated_at",
                (self._org_id, revision, encoded, _digest(encoded), updated_at),
            )
            self._connection.commit()
            return True
        except Exception:
            self._connection.rollback()
            raise


class SqliteTenantSessionRepository:
    def __init__(self, connection: sqlite3.Connection, org_id: str) -> None:
        self._connection, self._org_id = connection, _org(org_id)

    def get(self, session_id: str) -> sqlite3.Row | tuple[object, ...] | None:
        _validate(self._connection)
        row = self._connection.execute(
            "SELECT session_id, user_id, status, started_at, last_active_at, revision FROM operational_sessions WHERE org_id=? AND session_id=?",
            (self._org_id, session_id),
        ).fetchone()
        if row is None:
            return None
        if (
            any(type(row[index]) is not str or not row[index] for index in range(5))
            or row[2] not in {"active", "ended"}
            or type(row[5]) is not int
            or row[5] < 0
        ):
            raise SqliteOperationalTenantSourcesError("session row가 손상되었습니다.")
        return row

    def compare_and_set_end(self, session_id: str, expected_revision: int, ended_at: str) -> bool:
        _validate(self._connection)
        _revision(expected_revision, "expected session")
        _timestamp(ended_at)
        if type(session_id) is not str or not session_id:
            raise SqliteOperationalTenantSourcesError("session id가 유효하지 않습니다.")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            _validate(self._connection)
            cursor = self._connection.execute(
                "UPDATE operational_sessions SET status='ended', last_active_at=?, revision=revision+1 "
                "WHERE org_id=? AND session_id=? AND revision=? AND status='active'",
                (ended_at, self._org_id, session_id, expected_revision),
            )
            self._connection.commit()
            return cursor.rowcount == 1
        except Exception:
            self._connection.rollback()
            raise


class SqliteTenantAuditRepository:
    def __init__(self, connection: sqlite3.Connection, org_id: str) -> None:
        self._connection, self._org_id = connection, _org(org_id)

    def append(self, seq: int, record: dict[str, object], created_at: str) -> None:
        _validate(self._connection)
        _safe_audit_record(record)
        encoded = _json(record)
        self._connection.execute(
            "INSERT INTO operational_audit_records VALUES (?, ?, ?, ?, ?)",
            (self._org_id, seq, encoded, _digest(encoded), created_at),
        )

    def records(self) -> list[dict[str, object]]:
        _validate(self._connection)
        result: list[dict[str, object]] = []
        for raw, digest in self._connection.execute(
            "SELECT record_json, record_digest FROM operational_audit_records WHERE org_id=? ORDER BY seq",
            (self._org_id,),
        ):
            if _digest(raw) != digest:
                raise SqliteOperationalTenantSourcesError("audit digest가 손상되었습니다.")
            try:
                value: Any = json.loads(raw)
            except (TypeError, ValueError) as error:
                raise SqliteOperationalTenantSourcesError("audit JSON이 손상되었습니다.") from error
            if not isinstance(value, dict):
                raise SqliteOperationalTenantSourcesError("audit record가 safe object가 아닙니다.")
            record = cast(dict[str, object], value)
            _safe_audit_record(record)
            result.append(record)
        return result


class SqliteTenantHitlRepository:
    def __init__(self, connection: sqlite3.Connection, org_id: str) -> None:
        self._connection, self._org_id = connection, _org(org_id)

    def get(self, agent_id: str) -> tuple[bool, bool, int] | None:
        _validate(self._connection)
        row = self._connection.execute(
            "SELECT \"on\", explicit, revision, updated_at FROM operational_hitl_toggles WHERE org_id=? AND agent_id=?",
            (self._org_id, agent_id),
        ).fetchone()
        if row is None:
            return None
        if type(row[0]) is not int or row[0] not in (0, 1) or type(row[1]) is not int or row[1] not in (0, 1) or type(row[2]) is not int or row[2] < 0:
            raise SqliteOperationalTenantSourcesError("HITL row가 손상되었습니다.")
        _timestamp(row[3])
        return bool(row[0]), bool(row[1]), row[2]

    def compare_and_set(
        self, card_id: str, expected_revision: int | None, on: bool, explicit: bool, updated_at: str
    ) -> bool:
        _validate(self._connection)
        if type(card_id) is not str or not card_id:
            raise SqliteOperationalTenantSourcesError("card id가 유효하지 않습니다.")
        if expected_revision is not None:
            _revision(expected_revision, "expected HITL")
        if type(on) is not bool or type(explicit) is not bool:
            raise SqliteOperationalTenantSourcesError("HITL boolean이 유효하지 않습니다.")
        _timestamp(updated_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            _validate(self._connection)
            if expected_revision is None:
                try:
                    self._connection.execute(
                        "INSERT INTO operational_hitl_toggles VALUES (?, ?, ?, ?, 0, ?)",
                        (self._org_id, card_id, int(on), int(explicit), updated_at),
                    )
                except sqlite3.IntegrityError:
                    self._connection.rollback()
                    return False
            else:
                cursor = self._connection.execute(
                    "UPDATE operational_hitl_toggles SET \"on\"=?, explicit=?, revision=revision+1, updated_at=? "
                    "WHERE org_id=? AND agent_id=? AND revision=?",
                    (int(on), int(explicit), updated_at, self._org_id, card_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    self._connection.rollback()
                    return False
            self._connection.commit()
            return True
        except Exception:
            self._connection.rollback()
            raise


_AUDIT_FIELDS: Final = {"action", "subject_id", "by", "channel", "outcome", "approval_evidence_id", "approval_command_digest"}


def _safe_audit_record(record: dict[str, object]) -> None:
    if not record or not set(record).issubset(_AUDIT_FIELDS):
        raise SqliteOperationalTenantSourcesError("audit record field가 안전 allowlist가 아닙니다.")
    if any(type(value) is not str or not value or len(value) > 512 for value in record.values()):
        raise SqliteOperationalTenantSourcesError("audit record scalar가 안전하지 않습니다.")
