"""R3.1 immutable Credential Issue Target and target-FK stage-fence schema."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

SQLITE_DURABLE_CREDENTIAL_ISSUE_TARGETS_MIGRATION_FAULT_POINTS: Final = (
    "after_manifest_table",
    "after_targets",
    "after_fences",
    "before_manifest_insert",
    "after_manifest_insert",
)
type MigrationFaultInjector = Callable[[str], None]


class SqliteDurableCredentialIssueTargetsSchemaError(RuntimeError):
    """The R3.1 reservation schema is absent, drifted, or unsafe."""


_MANIFEST = "schema_component_manifests"
_TARGET = "durable_credential_issue_targets_v1"
_FENCE = "credential_issue_stage_fences_v2"
_V1_FENCE = "durable_credential_stage_fences"
_TARGET_COMPONENT = "durable_credential_issue_targets_v1"
_FENCE_COMPONENT = "durable_credential_stage_fence_v2"
_V1_COMPONENT = "durable_credential_stage_fence_v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")
_DELIVERY = re.compile(r"delivery:v1:[0-9a-f]{64}\Z")
_FORBIDDEN: Final = (
    "secret",
    "grant",
    "rationale",
    "body",
    "password",
    "token",
    "key",
    "bearer",
    "authorization",
)
_TARGET_STATES: Final = frozenset(
    {"Reserved", "StageClaimed", "Staged", "Committing", "Committed", "CleanupPending", "Cleaned"}
)
_FENCE_STATES: Final = frozenset(
    {
        "PendingStage",
        "ClaimedStage",
        "Staged",
        "Committing",
        "Committed",
        "CleanupPending",
        "Cleaned",
    }
)

_MANIFEST_DDL: Final = """CREATE TABLE schema_component_manifests (
    component_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    schema_version INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL
)"""


def _opaque_sql(field: str, *, nullable: bool = False) -> str:
    p = " AND ".join(
        (
            f"length({field}) BETWEEN 1 AND 128",
            f"{field} GLOB '[A-Za-z0-9]*'",
            f"{field} NOT GLOB '*[^A-Za-z0-9._:-]*'",
            *(f"instr(lower({field}), '{x}') = 0" for x in _FORBIDDEN),
        )
    )
    return f"{field} IS NULL OR ({p})" if nullable else p


def _hash_sql(field: str, *, nullable: bool = False) -> str:
    p = f"length({field}) = 64 AND {field} NOT GLOB '*[^0-9a-f]*'"
    return f"{field} IS NULL OR ({p})" if nullable else p


_TARGET_DDL: Final = f"""CREATE TABLE durable_credential_issue_targets_v1 (
 org_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("org_id")}), target_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("target_id")}), credential_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("credential_id")}), command_digest TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("command_digest")}),
 principal_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("principal_id")}), owner_subject_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("owner_subject_id")}), role TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("role")}), expires_at TEXT COLLATE BINARY CHECK(expires_at IS NULL OR (length(expires_at)=24 AND expires_at GLOB '????-??-??T??:??:??.???Z' AND COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', expires_at)=expires_at,0))),
 resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("resource_fingerprint")}), approval_evidence_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("approval_evidence_id")}), approval_command_digest TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("approval_command_digest")}), approval_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("approval_resource_fingerprint")}),
 state TEXT NOT NULL COLLATE BINARY CHECK(state IN ('Reserved','StageClaimed','Staged','Committing','Committed','CleanupPending','Cleaned')), target_generation INTEGER NOT NULL CHECK(target_generation >= 1),
 target_json TEXT NOT NULL, target_sha256 TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("target_sha256")}), created_at TEXT NOT NULL COLLATE BINARY CHECK(length(created_at)=24 AND created_at GLOB '????-??-??T??:??:??.???Z' AND COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', created_at)=created_at,0)), updated_at TEXT NOT NULL COLLATE BINARY CHECK(length(updated_at)=24 AND updated_at GLOB '????-??-??T??:??:??.???Z' AND COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ', updated_at)=updated_at,0)),
 PRIMARY KEY(org_id,target_id), UNIQUE(org_id,command_digest)
)"""
_ACTIVE_INDEX: Final = "CREATE UNIQUE INDEX durable_credential_issue_targets_active_credential ON durable_credential_issue_targets_v1(org_id,credential_id) WHERE state IN ('Reserved','StageClaimed','Staged','Committing','CleanupPending')"
_IMMUTABLE_TRIGGER = "durable_credential_issue_targets_v1_immutable_snapshot"
_IMMUTABLE_TRIGGER_DDL: Final = f"""CREATE TRIGGER {_IMMUTABLE_TRIGGER}
BEFORE UPDATE ON {_TARGET} FOR EACH ROW
WHEN OLD.org_id IS NOT NEW.org_id OR OLD.target_id IS NOT NEW.target_id OR OLD.credential_id IS NOT NEW.credential_id OR OLD.command_digest IS NOT NEW.command_digest OR OLD.principal_id IS NOT NEW.principal_id OR OLD.owner_subject_id IS NOT NEW.owner_subject_id OR OLD.role IS NOT NEW.role OR OLD.expires_at IS NOT NEW.expires_at OR OLD.resource_fingerprint IS NOT NEW.resource_fingerprint OR OLD.approval_evidence_id IS NOT NEW.approval_evidence_id OR OLD.approval_command_digest IS NOT NEW.approval_command_digest OR OLD.approval_resource_fingerprint IS NOT NEW.approval_resource_fingerprint OR OLD.target_generation IS NOT NEW.target_generation OR OLD.target_json IS NOT NEW.target_json OR OLD.target_sha256 IS NOT NEW.target_sha256 OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'immutable credential issue target snapshot'); END"""
_DELETE_TRIGGER = "durable_credential_issue_targets_v1_no_delete"
_DELETE_TRIGGER_DDL: Final = f"""CREATE TRIGGER {_DELETE_TRIGGER}
BEFORE DELETE ON {_TARGET} FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'credential issue target deletion is forbidden'); END"""
_ACTUAL_CREDENTIAL_TRIGGER = "durable_credentials_no_active_issue_target"
_ACTUAL_CREDENTIAL_TRIGGER_DDL: Final = f"""CREATE TRIGGER {_ACTUAL_CREDENTIAL_TRIGGER}
BEFORE INSERT ON durable_credentials FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM {_TARGET} WHERE org_id=NEW.org_id AND credential_id=NEW.credential_id AND state IN ('Reserved','StageClaimed','Staged','Committing','CleanupPending'))
BEGIN SELECT RAISE(ABORT, 'actual credential conflicts with active credential issue target'); END"""
_FENCE_DDL: Final = f"""CREATE TABLE credential_issue_stage_fences_v2 (
 org_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("org_id")}), target_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("target_id")}), stage_key TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("stage_key")}), secret_hash TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("secret_hash")}),
 delivery_ref TEXT COLLATE BINARY CHECK(delivery_ref IS NULL OR (length(delivery_ref)=76 AND substr(delivery_ref,1,12)='delivery:v1:' AND substr(delivery_ref,13) NOT GLOB '*[^0-9a-f]*')), claim_generation INTEGER NOT NULL CHECK(claim_generation >= 0), claim_token_hash TEXT COLLATE BINARY CHECK({_hash_sql("claim_token_hash", nullable=True)}),
 state TEXT NOT NULL COLLATE BINARY CHECK(state IN ('PendingStage','ClaimedStage','Staged','Committing','Committed','CleanupPending','Cleaned')), created_at TEXT NOT NULL COLLATE BINARY CHECK(length(created_at)=24), updated_at TEXT NOT NULL COLLATE BINARY CHECK(length(updated_at)=24),
 PRIMARY KEY(org_id,target_id), UNIQUE(org_id,stage_key), FOREIGN KEY(org_id,target_id) REFERENCES durable_credential_issue_targets_v1(org_id,target_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 CHECK((state='PendingStage' AND claim_generation=0 AND claim_token_hash IS NULL AND delivery_ref IS NULL) OR (state='ClaimedStage' AND claim_generation>=1 AND claim_token_hash IS NOT NULL AND delivery_ref IS NULL) OR (state IN ('Staged','Committing','Committed','CleanupPending','Cleaned') AND claim_generation>=1 AND claim_token_hash IS NOT NULL AND delivery_ref IS NOT NULL))
)"""

_PARENT_DDLS: Final = {
    "durable_credentials": """CREATE TABLE durable_credentials (
              credential_id TEXT NOT NULL, org_id TEXT NOT NULL, owner_subject_id TEXT NOT NULL,
              role TEXT NOT NULL, generation INTEGER NOT NULL, revision INTEGER NOT NULL,
              status TEXT NOT NULL, secret_hash TEXT NOT NULL, issued_at TEXT NOT NULL,
              expires_at TEXT, revoked_at TEXT,
              PRIMARY KEY (org_id, credential_id),
              CHECK (generation >= 1), CHECK (revision >= 1), CHECK (status IN ('active','revoked'))
            )""",
    "credential_command_receipts": """CREATE TABLE credential_command_receipts (
              org_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
              command_digest TEXT NOT NULL, credential_id TEXT NOT NULL, result_revision INTEGER NOT NULL,
              result_json TEXT NOT NULL, delivery_ref TEXT,
              PRIMARY KEY (org_id, request_id, attempt)
            )""",
    "credential_audit_intents": """CREATE TABLE credential_audit_intents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, action TEXT NOT NULL,
              credential_id TEXT NOT NULL, principal_subject_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
              detail_json TEXT NOT NULL
            )""",
    "credential_outbox_intents": """CREATE TABLE credential_outbox_intents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, kind TEXT NOT NULL,
              credential_id TEXT NOT NULL, payload_json TEXT NOT NULL
            )""",
}


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sql(connection: sqlite3.Connection, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return None if row is None else row[0]


def _catalog_sql(connection: sqlite3.Connection, type_: str, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (type_, name)
    ).fetchone()
    return None if row is None else row[0]


def _same(actual: object, expected: str) -> bool:
    if not isinstance(actual, str):
        return False

    def normalized(value: str) -> str:
        return " ".join(value.split()).replace("( ", "(").replace(" )", ")")

    return normalized(actual) == normalized(expected)


def _safe(value: object) -> bool:
    return (
        isinstance(value, str)
        and _OPAQUE.fullmatch(value) is not None
        and all(x not in value.lower() for x in _FORBIDDEN)
    )


def _time(value: object) -> bool:
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        return False
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
            .replace(tzinfo=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
            == value
        )
    except ValueError:
        return False


def _parent(connection: sqlite3.Connection) -> None:
    if any(not _same(_sql(connection, name), ddl) for name, ddl in _PARENT_DDLS.items()):
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "durable credential parent가 필요합니다."
        )


def _v1_present(connection: sqlite3.Connection) -> bool:
    return _sql(connection, _V1_FENCE) is not None or (
        _sql(connection, _MANIFEST) is not None
        and connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (_V1_COMPONENT,)
        ).fetchone()
        is not None
    )


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    return {
        "tables": [
            {"name": n, "sql": " ".join((_sql(connection, n) or "").split())}
            for n in (_TARGET, _FENCE)
        ],
        "indexes": [
            {
                "name": "durable_credential_issue_targets_active_credential",
                "sql": " ".join(
                    (
                        _catalog_sql(
                            connection,
                            "index",
                            "durable_credential_issue_targets_active_credential",
                        )
                        or ""
                    ).split()
                ),
            }
        ],
        "triggers": [
            {
                "name": _IMMUTABLE_TRIGGER,
                "sql": " ".join(
                    (_catalog_sql(connection, "trigger", _IMMUTABLE_TRIGGER) or "").split()
                ),
            },
            {
                "name": _DELETE_TRIGGER,
                "sql": " ".join(
                    (_catalog_sql(connection, "trigger", _DELETE_TRIGGER) or "").split()
                ),
            },
            {
                "name": _ACTUAL_CREDENTIAL_TRIGGER,
                "sql": " ".join(
                    (_catalog_sql(connection, "trigger", _ACTUAL_CREDENTIAL_TRIGGER) or "").split()
                ),
            },
        ],
    }


def _manifest(component: str) -> str:
    return _canonical(
        {"component_id": component, "schema_version": 1, "catalog": _catalog_expected()}
    )


def _catalog_expected() -> dict[str, object]:
    return {
        "tables": [
            {"name": _TARGET, "sql": " ".join(_TARGET_DDL.split())},
            {"name": _FENCE, "sql": " ".join(_FENCE_DDL.split())},
        ],
        "indexes": [
            {
                "name": "durable_credential_issue_targets_active_credential",
                "sql": " ".join(_ACTIVE_INDEX.split()),
            }
        ],
        "triggers": [
            {"name": _IMMUTABLE_TRIGGER, "sql": " ".join(_IMMUTABLE_TRIGGER_DDL.split())},
            {"name": _DELETE_TRIGGER, "sql": " ".join(_DELETE_TRIGGER_DDL.split())},
            {
                "name": _ACTUAL_CREDENTIAL_TRIGGER,
                "sql": " ".join(_ACTUAL_CREDENTIAL_TRIGGER_DDL.split()),
            },
        ],
    }


@dataclass(frozen=True)
class DurableCredentialIssueTargetReservation:
    org_id: str
    target_id: str
    credential_id: str
    command_digest: str
    principal_id: str
    owner_subject_id: str
    role: str
    expires_at: str | None
    resource_fingerprint: str
    approval_evidence_id: str
    approval_command_digest: str
    approval_resource_fingerprint: str
    target_generation: int
    created_at: str

    def target_json(self) -> str:
        return _canonical(
            {
                "approval_command_digest": self.approval_command_digest,
                "approval_evidence_id": self.approval_evidence_id,
                "approval_resource_fingerprint": self.approval_resource_fingerprint,
                "command_digest": self.command_digest,
                "credential_id": self.credential_id,
                "expires_at": self.expires_at,
                "org_id": self.org_id,
                "owner_subject_id": self.owner_subject_id,
                "principal_id": self.principal_id,
                "resource_fingerprint": self.resource_fingerprint,
                "role": self.role,
                "target_generation": self.target_generation,
                "target_id": self.target_id,
            }
        )

    def row(self) -> tuple[object, ...]:
        target = self.target_json()
        return (
            self.org_id,
            self.target_id,
            self.credential_id,
            self.command_digest,
            self.principal_id,
            self.owner_subject_id,
            self.role,
            self.expires_at,
            self.resource_fingerprint,
            self.approval_evidence_id,
            self.approval_command_digest,
            self.approval_resource_fingerprint,
            "Reserved",
            self.target_generation,
            target,
            _digest(target),
            self.created_at,
            self.created_at,
        )


def _same_reservation(
    row: sqlite3.Row, reservation: DurableCredentialIssueTargetReservation
) -> bool:
    return row["target_json"] == reservation.target_json() and row["target_sha256"] == _digest(
        reservation.target_json()
    )


def _validate_reservation(reservation: DurableCredentialIssueTargetReservation) -> None:
    if (
        any(
            not _safe(value)
            for value in (
                reservation.org_id,
                reservation.target_id,
                reservation.credential_id,
                reservation.principal_id,
                reservation.owner_subject_id,
                reservation.role,
                reservation.approval_evidence_id,
            )
        )
        or any(
            _SHA256.fullmatch(value) is None
            for value in (
                reservation.command_digest,
                reservation.resource_fingerprint,
                reservation.approval_command_digest,
                reservation.approval_resource_fingerprint,
            )
        )
        or type(reservation.target_generation) is not int
        or reservation.target_generation < 1
        or not _time(reservation.created_at)
        or (reservation.expires_at is not None and not _time(reservation.expires_at))
    ):
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "credential issue target reservation이 canonical하지 않습니다."
        )


def validate_durable_credential_issue_target_reservation(
    reservation: DurableCredentialIssueTargetReservation,
) -> None:
    """Public command boundary for an immutable target reservation."""
    _validate_reservation(reservation)


def _validate_rows(connection: sqlite3.Connection) -> None:
    targets: dict[tuple[str, str], sqlite3.Row] = {}
    for row in connection.execute(f"SELECT * FROM {_TARGET}"):
        d = dict(row)
        targets[(d["org_id"], d["target_id"])] = row
        expected = _canonical(
            {
                k: d[k]
                for k in (
                    "approval_command_digest",
                    "approval_evidence_id",
                    "approval_resource_fingerprint",
                    "command_digest",
                    "credential_id",
                    "expires_at",
                    "org_id",
                    "owner_subject_id",
                    "principal_id",
                    "resource_fingerprint",
                    "role",
                    "target_generation",
                    "target_id",
                )
            }
        )
        if (
            any(
                not _safe(d[k])
                for k in (
                    "org_id",
                    "target_id",
                    "credential_id",
                    "principal_id",
                    "owner_subject_id",
                    "role",
                    "approval_evidence_id",
                )
            )
            or any(
                not isinstance(d[k], str) or _SHA256.fullmatch(d[k]) is None
                for k in (
                    "command_digest",
                    "resource_fingerprint",
                    "approval_command_digest",
                    "approval_resource_fingerprint",
                    "target_sha256",
                )
            )
            or type(d["target_generation"]) is not int
            or d["target_generation"] < 1
            or d["state"] not in _TARGET_STATES
            or not _time(d["created_at"])
            or not _time(d["updated_at"])
            or d["created_at"] > d["updated_at"]
            or (d["expires_at"] is not None and not _time(d["expires_at"]))
            or d["target_json"] != expected
            or d["target_sha256"] != _digest(expected)
        ):
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "credential issue target row가 canonical하지 않습니다."
            )
    seen: set[tuple[str, str]] = set()
    mapping = {
        "PendingStage": "Reserved",
        "ClaimedStage": "StageClaimed",
        "Staged": "Staged",
        "Committing": "Committing",
        "Committed": "Committed",
        "CleanupPending": "CleanupPending",
        "Cleaned": "Cleaned",
    }
    for row in connection.execute(f"SELECT * FROM {_FENCE}"):
        d = dict(row)
        key = (d["org_id"], d["target_id"])
        target = targets.get(key)
        good = (
            (
                d["state"] == "PendingStage"
                and d["claim_generation"] == 0
                and d["claim_token_hash"] is None
                and d["delivery_ref"] is None
            )
            or (
                d["state"] == "ClaimedStage"
                and type(d["claim_generation"]) is int
                and d["claim_generation"] >= 1
                and d["claim_token_hash"] is not None
                and d["delivery_ref"] is None
            )
            or (
                d["state"] in {"Staged", "Committing", "Committed", "CleanupPending", "Cleaned"}
                and type(d["claim_generation"]) is int
                and d["claim_generation"] >= 1
                and d["claim_token_hash"] is not None
                and d["delivery_ref"] is not None
            )
        )
        if (
            key in seen
            or target is None
            or target["state"] != mapping.get(d["state"])
            or any(not _safe(d[k]) for k in ("org_id", "target_id"))
            or any(
                not isinstance(d[k], str) or _SHA256.fullmatch(d[k]) is None
                for k in ("stage_key", "secret_hash")
            )
            or d["state"] not in _FENCE_STATES
            or (
                d["claim_token_hash"] is not None
                and (
                    not isinstance(d["claim_token_hash"], str)
                    or _SHA256.fullmatch(d["claim_token_hash"]) is None
                )
            )
            or (
                d["delivery_ref"] is not None
                and (
                    not isinstance(d["delivery_ref"], str)
                    or _DELIVERY.fullmatch(d["delivery_ref"]) is None
                )
            )
            or not _time(d["created_at"])
            or not _time(d["updated_at"])
            or d["created_at"] > d["updated_at"]
            or not good
        ):
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "credential issue stage fence row가 canonical하지 않습니다."
            )
        seen.add(key)
    if any(row["state"] != "Reserved" and key not in seen for key, row in targets.items()):
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "target state와 stage fence가 일치하지 않습니다."
        )


def _validate(connection: sqlite3.Connection) -> None:
    _parent(connection)
    if _v1_present(connection):
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "v1 direct-FK stage fence가 있어 v2는 unavailable입니다."
        )
    if not _same(_sql(connection, _MANIFEST), _MANIFEST_DDL):
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "공유 manifest catalog가 canonical하지 않습니다."
        )
    for component in (_TARGET_COMPONENT, _FENCE_COMPONENT):
        row = connection.execute(
            "SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id=?",
            (component,),
        ).fetchone()
        expected = _manifest(component)
        if row is None or row[0] != 1 or row[1] != expected or row[2] != _digest(expected):
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "v2 manifest가 canonical하지 않습니다."
            )
    if (
        not _same(_sql(connection, _TARGET), _TARGET_DDL)
        or not _same(_sql(connection, _FENCE), _FENCE_DDL)
        or _canonical(_catalog(connection)) != _canonical(_catalog_expected())
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "v2 catalog 또는 foreign key가 canonical하지 않습니다."
        )
    _validate_rows(connection)


def validate_sqlite_durable_credential_issue_targets_connection(
    connection: sqlite3.Connection,
) -> None:
    old = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection)
    finally:
        connection.row_factory = old


def migrate_sqlite_durable_credential_issue_targets_schema(
    path: str | Path, *, fault_injector: MigrationFaultInjector | None = None
) -> None:
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        _parent(connection)
        if _v1_present(connection):
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "v1 direct-FK stage fence는 migration하지 않습니다."
            )
        exists = _sql(connection, _MANIFEST) is not None
        if exists and not _same(_sql(connection, _MANIFEST), _MANIFEST_DDL):
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "공유 manifest catalog가 canonical하지 않습니다."
            )
        markers = [
            _
            for _ in (_TARGET_COMPONENT, _FENCE_COMPONENT)
            if exists
            and connection.execute(
                "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (_,)
            ).fetchone()
        ]
        if markers:
            _validate(connection)
            connection.commit()
            return
        if _sql(connection, _TARGET) is not None or _sql(connection, _FENCE) is not None:
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "partial v2 schema는 repair하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "migration 전 foreign key가 canonical하지 않습니다."
            )
        if not exists:
            connection.execute(_MANIFEST_DDL)
        if fault_injector:
            fault_injector("after_manifest_table")
        connection.execute(_TARGET_DDL)
        connection.execute(_IMMUTABLE_TRIGGER_DDL)
        connection.execute(_DELETE_TRIGGER_DDL)
        connection.execute(_ACTUAL_CREDENTIAL_TRIGGER_DDL)
        if fault_injector:
            fault_injector("after_targets")
        connection.execute(_ACTIVE_INDEX)
        connection.execute(_FENCE_DDL)
        if fault_injector:
            fault_injector("after_fences")
        if fault_injector:
            fault_injector("before_manifest_insert")
        for component in (_TARGET_COMPONENT, _FENCE_COMPONENT):
            value = _manifest(component)
            connection.execute(
                "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES(?,?,?,?)",
                (component, 1, value, _digest(value)),
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


def open_sqlite_durable_credential_issue_targets_connection(path: str | Path) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "v2 runtime은 existing SQLite file만 엽니다."
        )
    try:
        connection = sqlite3.connect(
            f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode=rw",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as error:
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "v2 SQLite DB를 열 수 없습니다."
        ) from error
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _validate(connection)
    except Exception:
        connection.close()
        raise
    return connection


def reserve_sqlite_durable_credential_issue_target(
    connection: sqlite3.Connection, reservation: DurableCredentialIssueTargetReservation
) -> sqlite3.Row:
    _validate_reservation(reservation)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate(connection)
        existing = connection.execute(
            f"SELECT * FROM {_TARGET} WHERE org_id=? AND command_digest=?",
            (reservation.org_id, reservation.command_digest),
        ).fetchone()
        if existing is not None:
            if _same_reservation(existing, reservation):
                connection.commit()
                return existing
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "same command digest의 target mirror가 다릅니다."
            )
        if (
            connection.execute(
                "SELECT 1 FROM durable_credentials WHERE org_id=? AND credential_id=?",
                (reservation.org_id, reservation.credential_id),
            ).fetchone()
            is not None
        ):
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "actual durable credential과 충돌합니다."
            )
        if (
            connection.execute(
                f"SELECT 1 FROM {_TARGET} WHERE org_id=? AND credential_id=? AND state IN ('Reserved','StageClaimed','Staged','Committing','CleanupPending')",
                (reservation.org_id, reservation.credential_id),
            ).fetchone()
            is not None
        ):
            raise SqliteDurableCredentialIssueTargetsSchemaError(
                "active credential issue target과 충돌합니다."
            )
        connection.execute(
            f"INSERT INTO {_TARGET} VALUES ({','.join('?' for _ in reservation.row())})",
            reservation.row(),
        )
        row = connection.execute(
            f"SELECT * FROM {_TARGET} WHERE org_id=? AND target_id=?",
            (reservation.org_id, reservation.target_id),
        ).fetchone()
        _validate_rows(connection)
        connection.commit()
    except SqliteDurableCredentialIssueTargetsSchemaError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.IntegrityError as error:
        if connection.in_transaction:
            connection.rollback()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate(connection)
            existing = connection.execute(
                f"SELECT * FROM {_TARGET} WHERE org_id=? AND command_digest=?",
                (reservation.org_id, reservation.command_digest),
            ).fetchone()
            if existing is not None and _same_reservation(existing, reservation):
                connection.commit()
                return existing
            connection.rollback()
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "credential issue target reservation이 충돌합니다."
        ) from error
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "credential issue target reservation이 충돌합니다."
        ) from error
    if row is None:
        raise SqliteDurableCredentialIssueTargetsSchemaError(
            "credential issue target reservation readback이 없습니다."
        )
    return row


@dataclass(frozen=True)
class DurableCredentialIssueTargetsSchemaReconciliationReport:
    capable: bool
    detail: str


def reconcile_sqlite_durable_credential_issue_targets_schema(
    path: str | Path,
) -> DurableCredentialIssueTargetsSchemaReconciliationReport:
    try:
        connection = open_sqlite_durable_credential_issue_targets_connection(path)
    except Exception as error:
        return DurableCredentialIssueTargetsSchemaReconciliationReport(False, str(error))
    connection.close()
    return DurableCredentialIssueTargetsSchemaReconciliationReport(True, "capable_v2")
