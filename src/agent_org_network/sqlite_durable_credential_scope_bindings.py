"""R5.1 immutable, source-backed Credential Scope Binding reservation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from agent_org_network.sqlite_durable_credential_issue_targets import (
    DurableCredentialIssueTargetReservation,
    open_sqlite_durable_credential_issue_targets_connection,
    validate_durable_credential_issue_target_reservation,
    validate_sqlite_durable_credential_issue_targets_connection,
)

COMPONENT_ID: Final = "durable_credential_scope_bindings_v1"
_TABLE: Final = "durable_credential_scope_bindings_v1"
_INDEX: Final = "durable_credential_scope_bindings_credential"
_UPDATE_TRIGGER: Final = "durable_credential_scope_bindings_v1_no_update"
_DELETE_TRIGGER: Final = "durable_credential_scope_bindings_v1_no_delete"
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
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


def _opaque_sql(field: str) -> str:
    return " AND ".join(
        (
            f"length({field}) BETWEEN 1 AND 128",
            f"{field} GLOB '[A-Za-z0-9]*'",
            f"{field} NOT GLOB '*[^A-Za-z0-9._:-]*'",
            *(f"instr(lower({field}), '{word}') = 0" for word in _FORBIDDEN),
        )
    )


def _hash_sql(field: str) -> str:
    return f"length({field})=64 AND {field} NOT GLOB '*[^0-9a-f]*'"


_DDL: Final = f"""CREATE TABLE {_TABLE} (
 org_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("org_id")}), target_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("target_id")}),
 credential_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("credential_id")}), agent_card_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("agent_card_id")}), owner_subject_id TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("owner_subject_id")}),
 credential_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("credential_resource_fingerprint")}), card_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("card_resource_fingerprint")}), owner_resource_fingerprint TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("owner_resource_fingerprint")}),
 source_kind TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("source_kind")}), source_instance_ref TEXT NOT NULL COLLATE BINARY CHECK({_opaque_sql("source_instance_ref")}), scope_revision INTEGER NOT NULL CHECK(scope_revision>=1), snapshot_digest TEXT NOT NULL COLLATE BINARY CHECK({_hash_sql("snapshot_digest")}),
 created_at TEXT NOT NULL COLLATE BINARY CHECK(length(created_at)=24 AND created_at GLOB '????-??-??T??:??:??.???Z' AND COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',created_at)=created_at,0)),
 PRIMARY KEY(org_id,target_id), FOREIGN KEY(org_id,target_id) REFERENCES durable_credential_issue_targets_v1(org_id,target_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""
_INDEX_DDL: Final = f"CREATE UNIQUE INDEX {_INDEX} ON {_TABLE}(org_id,credential_id)"
_UPDATE_TRIGGER_DDL: Final = f"""CREATE TRIGGER {_UPDATE_TRIGGER}
BEFORE UPDATE ON {_TABLE} FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'credential scope binding is immutable'); END"""
_DELETE_TRIGGER_DDL: Final = f"""CREATE TRIGGER {_DELETE_TRIGGER}
BEFORE DELETE ON {_TABLE} FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'credential scope binding deletion is forbidden'); END"""


class SqliteCredentialScopeBindingError(RuntimeError):
    """The R5.1 binding capability is absent, drifted, or has no exact source proof."""


def _timestamp(value: object) -> bool:
    if type(value) is not str or len(value) != 24:
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


def _safe(value: object) -> bool:
    return (
        type(value) is str
        and _OPAQUE.fullmatch(value) is not None
        and all(word not in value.lower() for word in _FORBIDDEN)
    )


def _sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


@dataclass(frozen=True)
class CredentialScopeSnapshot:
    org_id: str
    credential_id: str
    agent_card_id: str
    owner_subject_id: str
    credential_resource_fingerprint: str
    card_resource_fingerprint: str
    owner_resource_fingerprint: str
    source_kind: str
    source_instance_ref: str
    scope_revision: int
    snapshot_digest: str
    captured_at: str

    def __post_init__(self) -> None:
        if not _valid_snapshot(self):
            raise ValueError("credential scope snapshot이 strict하지 않습니다.")

    def binding_row(self, target_id: str, now: str) -> tuple[object, ...]:
        return (
            self.org_id,
            target_id,
            self.credential_id,
            self.agent_card_id,
            self.owner_subject_id,
            self.credential_resource_fingerprint,
            self.card_resource_fingerprint,
            self.owner_resource_fingerprint,
            self.source_kind,
            self.source_instance_ref,
            self.scope_revision,
            self.snapshot_digest,
            now,
        )


class CredentialScopeSource(Protocol):
    def resolve_issue_scope(
        self, org_id: str, credential_id: str, agent_card_id: str
    ) -> CredentialScopeSnapshot | None: ...


def _snapshot_digest(snapshot: CredentialScopeSnapshot) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "agent_card_id": snapshot.agent_card_id,
                "card_resource_fingerprint": snapshot.card_resource_fingerprint,
                "credential_id": snapshot.credential_id,
                "credential_resource_fingerprint": snapshot.credential_resource_fingerprint,
                "org_id": snapshot.org_id,
                "owner_resource_fingerprint": snapshot.owner_resource_fingerprint,
                "owner_subject_id": snapshot.owner_subject_id,
                "source_instance_ref": snapshot.source_instance_ref,
                "source_kind": snapshot.source_kind,
                "scope_revision": snapshot.scope_revision,
            }
        ).encode()
    ).hexdigest()


def _valid_snapshot(snapshot: CredentialScopeSnapshot) -> bool:
    return (
        all(
            _safe(value)
            for value in (
                snapshot.org_id,
                snapshot.credential_id,
                snapshot.agent_card_id,
                snapshot.owner_subject_id,
                snapshot.source_kind,
                snapshot.source_instance_ref,
            )
        )
        and all(
            _sha256(value)
            for value in (
                snapshot.credential_resource_fingerprint,
                snapshot.card_resource_fingerprint,
                snapshot.owner_resource_fingerprint,
                snapshot.snapshot_digest,
            )
        )
        and type(snapshot.scope_revision) is int
        and snapshot.scope_revision >= 1
        and _timestamp(snapshot.captured_at)
        and snapshot.snapshot_digest == _snapshot_digest(snapshot)
    )


def _catalog_sql(connection: sqlite3.Connection, type_: str, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (type_, name)
    ).fetchone()
    return None if row is None else row[0]


def _same(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()).replace("( ", "(").replace(
        " )", ")"
    ) == " ".join(expected.split()).replace("( ", "(").replace(" )", ")")


def _catalog_expected() -> dict[str, object]:
    return {
        "tables": [{"name": _TABLE, "sql": " ".join(_DDL.split())}],
        "indexes": [{"name": _INDEX, "sql": " ".join(_INDEX_DDL.split())}],
        "triggers": [
            {"name": _UPDATE_TRIGGER, "sql": " ".join(_UPDATE_TRIGGER_DDL.split())},
            {"name": _DELETE_TRIGGER, "sql": " ".join(_DELETE_TRIGGER_DDL.split())},
        ],
    }


def _manifest() -> str:
    return _canonical(
        {"component_id": COMPONENT_ID, "schema_version": 1, "catalog": _catalog_expected()}
    )


def _validate_rows(
    connection: sqlite3.Connection, source: CredentialScopeSource | None = None
) -> None:
    for row in connection.execute(f"SELECT * FROM {_TABLE}"):
        data = dict(row)
        snapshot = CredentialScopeSnapshot(
            data["org_id"],
            data["credential_id"],
            data["agent_card_id"],
            data["owner_subject_id"],
            data["credential_resource_fingerprint"],
            data["card_resource_fingerprint"],
            data["owner_resource_fingerprint"],
            data["source_kind"],
            data["source_instance_ref"],
            data["scope_revision"],
            data["snapshot_digest"],
            data["created_at"],
        )
        if snapshot.binding_row(data["target_id"], data["created_at"])[1] != data["target_id"]:
            raise SqliteCredentialScopeBindingError("scope binding row가 canonical하지 않습니다.")
        target = connection.execute(
            "SELECT credential_id,owner_subject_id,resource_fingerprint FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
            (data["org_id"], data["target_id"]),
        ).fetchone()
        if target is None or tuple(target) != (
            data["credential_id"],
            data["owner_subject_id"],
            data["credential_resource_fingerprint"],
        ):
            raise SqliteCredentialScopeBindingError(
                "scope binding target source가 canonical하지 않습니다."
            )
        if source is not None:
            try:
                current = source.resolve_issue_scope(
                    data["org_id"], data["credential_id"], data["agent_card_id"]
                )
            except Exception as error:
                raise SqliteCredentialScopeBindingError(
                    "scope source가 unavailable입니다."
                ) from error
            if (
                type(current) is not CredentialScopeSnapshot
                or not _valid_snapshot(current)
                or current.binding_row(data["target_id"], data["created_at"])[:-1]
                != tuple(row)[:-1]
            ):
                raise SqliteCredentialScopeBindingError(
                    "current source scope가 persisted binding과 다릅니다."
                )


def _validate(connection: sqlite3.Connection, source: CredentialScopeSource | None = None) -> None:
    try:
        validate_sqlite_durable_credential_issue_targets_connection(connection)
    except Exception as error:
        raise SqliteCredentialScopeBindingError("R5.1 parent가 unavailable입니다.") from error
    marker = connection.execute(
        "SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id=?",
        (COMPONENT_ID,),
    ).fetchone()
    manifest = _manifest()
    foreign_keys = [tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({_TABLE})")]
    if (
        marker is None
        or tuple(marker) != (1, manifest, hashlib.sha256(manifest.encode()).hexdigest())
        or not _same(_catalog_sql(connection, "table", _TABLE), _DDL)
        or not _same(_catalog_sql(connection, "index", _INDEX), _INDEX_DDL)
        or not _same(_catalog_sql(connection, "trigger", _UPDATE_TRIGGER), _UPDATE_TRIGGER_DDL)
        or not _same(_catalog_sql(connection, "trigger", _DELETE_TRIGGER), _DELETE_TRIGGER_DDL)
        or foreign_keys
        != [
            (
                0,
                0,
                "durable_credential_issue_targets_v1",
                "org_id",
                "org_id",
                "RESTRICT",
                "RESTRICT",
                "NONE",
            ),
            (
                0,
                1,
                "durable_credential_issue_targets_v1",
                "target_id",
                "target_id",
                "RESTRICT",
                "RESTRICT",
                "NONE",
            ),
        ]
    ):
        raise SqliteCredentialScopeBindingError("R5.1 catalog가 canonical하지 않습니다.")
    try:
        _validate_rows(connection, source)
    except (KeyError, TypeError, ValueError) as error:
        raise SqliteCredentialScopeBindingError(
            "scope binding row가 canonical하지 않습니다."
        ) from error


def validate_sqlite_durable_credential_scope_bindings_connection(
    connection: sqlite3.Connection, *, source: CredentialScopeSource
) -> None:
    """Validate the complete R5.1 binding capability on an already-owned transaction."""
    if not callable(getattr(source, "resolve_issue_scope", None)):
        raise SqliteCredentialScopeBindingError("trusted scope source가 필요합니다.")
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, source)
    finally:
        connection.row_factory = previous


def migrate_sqlite_durable_credential_scope_bindings(
    connection: sqlite3.Connection, *, fault_injector: Callable[[str], None] | None = None
) -> None:
    try:
        validate_sqlite_durable_credential_issue_targets_connection(connection)
        connection.execute("BEGIN IMMEDIATE")
        present = (
            any(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type=? AND name=?", (kind, name)
                ).fetchone()
                is not None
                for kind, name in (
                    ("table", _TABLE),
                    ("index", _INDEX),
                    ("trigger", _UPDATE_TRIGGER),
                    ("trigger", _DELETE_TRIGGER),
                )
            )
            or connection.execute(
                "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
            ).fetchone()
            is not None
        )
        if present:
            _validate(connection)
        else:
            connection.execute(_DDL)
            connection.execute(_INDEX_DDL)
            connection.execute(_UPDATE_TRIGGER_DDL)
            connection.execute(_DELETE_TRIGGER_DDL)
            if fault_injector is not None:
                fault_injector("after_table")
            manifest = _manifest()
            connection.execute(
                "INSERT INTO schema_component_manifests VALUES(?,?,?,?)",
                (COMPONENT_ID, 1, manifest, hashlib.sha256(manifest.encode()).hexdigest()),
            )
            _validate(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def open_sqlite_durable_credential_scope_bindings(
    path: str | Path, *, source: CredentialScopeSource | None
) -> sqlite3.Connection:
    if not callable(getattr(source, "resolve_issue_scope", None)):
        raise SqliteCredentialScopeBindingError("trusted scope source가 필요합니다.")
    connection: sqlite3.Connection | None = None
    try:
        connection = open_sqlite_durable_credential_issue_targets_connection(path)
        _validate(connection, source)
        return connection
    except Exception as error:
        if connection is not None:
            connection.close()
        if isinstance(error, SqliteCredentialScopeBindingError):
            raise
        raise SqliteCredentialScopeBindingError("R5.1 parent가 unavailable입니다.") from error


def reserve_sqlite_credential_scope_binding(
    path: str | Path,
    *,
    reservation: DurableCredentialIssueTargetReservation,
    agent_card_id: str,
    now: str,
    source: CredentialScopeSource,
    fault_injector: Callable[[str], None] | None = None,
) -> None:
    """Atomically reserve the immutable Issue Target and its source-backed scope binding."""
    try:
        validate_durable_credential_issue_target_reservation(reservation)
    except Exception as error:
        raise SqliteCredentialScopeBindingError(
            "scope binding target reservation이 strict하지 않습니다."
        ) from error
    if (
        not _safe(agent_card_id)
        or not _timestamp(now)
        or not callable(getattr(source, "resolve_issue_scope", None))
    ):
        raise SqliteCredentialScopeBindingError(
            "scope binding input 또는 source가 strict하지 않습니다."
        )
    connection = open_sqlite_durable_credential_scope_bindings(path, source=source)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_target = connection.execute(
            "SELECT target_json,target_sha256 FROM durable_credential_issue_targets_v1 WHERE org_id=? AND command_digest=?",
            (reservation.org_id, reservation.command_digest),
        ).fetchone()
        target_json = reservation.target_json()
        target_sha256 = hashlib.sha256(target_json.encode()).hexdigest()
        if existing_target is None:
            if (
                connection.execute(
                    "SELECT 1 FROM durable_credentials WHERE org_id=? AND credential_id=?",
                    (reservation.org_id, reservation.credential_id),
                ).fetchone()
                is not None
            ):
                raise SqliteCredentialScopeBindingError("actual credential과 target이 충돌합니다.")
            if (
                connection.execute(
                    "SELECT 1 FROM durable_credential_issue_targets_v1 WHERE org_id=? AND credential_id=? AND state IN ('Reserved','StageClaimed','Staged','Committing','CleanupPending')",
                    (reservation.org_id, reservation.credential_id),
                ).fetchone()
                is not None
            ):
                raise SqliteCredentialScopeBindingError("active target과 충돌합니다.")
            connection.execute(
                f"INSERT INTO durable_credential_issue_targets_v1 VALUES ({','.join('?' for _ in reservation.row())})",
                reservation.row(),
            )
            if fault_injector is not None:
                fault_injector("after_target")
        elif tuple(existing_target) != (target_json, target_sha256):
            raise SqliteCredentialScopeBindingError("target replay가 exact하지 않습니다.")
        try:
            snapshot = source.resolve_issue_scope(
                reservation.org_id, reservation.credential_id, agent_card_id
            )
        except Exception as error:
            raise SqliteCredentialScopeBindingError("scope source가 unavailable입니다.") from error
        if (
            type(snapshot) is not CredentialScopeSnapshot
            or not _valid_snapshot(snapshot)
            or (
                snapshot.org_id,
                snapshot.credential_id,
                snapshot.agent_card_id,
                snapshot.owner_subject_id,
                snapshot.credential_resource_fingerprint,
            )
            != (
                reservation.org_id,
                reservation.credential_id,
                agent_card_id,
                reservation.owner_subject_id,
                reservation.resource_fingerprint,
            )
        ):
            raise SqliteCredentialScopeBindingError("current source scope가 target과 다릅니다.")
        row = snapshot.binding_row(reservation.target_id, now)
        old = connection.execute(
            f"SELECT * FROM {_TABLE} WHERE org_id=? AND target_id=?",
            (reservation.org_id, reservation.target_id),
        ).fetchone()
        if old is None:
            connection.execute(f"INSERT INTO {_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            if fault_injector is not None:
                fault_injector("after_binding")
        elif tuple(old)[:-1] != row[:-1]:
            raise SqliteCredentialScopeBindingError("scope binding replay가 exact하지 않습니다.")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
