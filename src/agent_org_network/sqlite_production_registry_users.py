"""Production Registry User authoritative SQLite component (P17.15 O2a)."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Protocol

from pydantic import BaseModel, field_validator

from agent_org_network.central_operational_evidence import (
    RegistryChange,
    SafeResourceRef,
    SourceReceiptProvenance,
    append_committed_source_evidence_if_v19,
    canonical_v19_file_authority,
    source_receipt_digest,
)



class ProductionRegistryUserError(Exception):
    pass


class ProductionRegistryUserUnavailable(ProductionRegistryUserError):
    pass


class ProductionRegistryUserDenied(ProductionRegistryUserError):
    pass


class ProductionRegistryUserConflict(ProductionRegistryUserError):
    pass


class ProductionRegistryUserRevisionConflict(ProductionRegistryUserConflict):
    pass


class LegacyProductionRegistryUsersUnverifiable(ProductionRegistryUserUnavailable):
    def __init__(self, *, counts: dict[str, int], reason: str) -> None:
        super().__init__(reason)
        self.counts = dict(counts)
        self.reason = reason


class ProductionRegistryUserCommand(BaseModel, frozen=True):
    org_id: str
    principal_id: str
    idempotency_key: str
    expected_revision: int
    user_id: str
    email: str
    manager_id: str | None = None

    @field_validator("org_id", "principal_id", "idempotency_key", "user_id", "manager_id")
    @classmethod
    def _opaque(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        if not value or value != value.strip() or "@" not in value:
            raise ValueError("canonical email required")
        return value


class ProductionRegistryUser(BaseModel, frozen=True):
    org_id: str
    user_id: str
    email: str
    manager_id: str | None
    revision: int


class ProductionRegistryUserResult(BaseModel, frozen=True):
    revision: int
    user: ProductionRegistryUser
    replayed: bool = False


class CurrentUserRegistrationAuthorization(BaseModel, frozen=True):
    authority_epoch: int
    policy_digest: str
    evidence_digest: str

    @field_validator("authority_epoch")
    @classmethod
    def _epoch(cls, value: int) -> int:
        if value < 0:
            raise ValueError("nonnegative authority epoch required")
        return value

    @field_validator("policy_digest", "evidence_digest")
    @classmethod
    def _evidence_digest(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("canonical digest required")
        return value


class TxCurrentUserRegistrationAuthorizer(Protocol):
    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection
    ) -> CurrentUserRegistrationAuthorization: ...

    def verify_precommit(
        self,
        command: ProductionRegistryUserCommand,
        evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool: ...
FaultInjector = Callable[[str], None]


def _no_fault(_point: str) -> None:
    return None

_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS production_registry_revisions (
 org_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision >= 0)
);
CREATE TABLE production_registry_users (
 org_id TEXT NOT NULL, user_id TEXT NOT NULL, email TEXT NOT NULL,
 manager_id TEXT, revision INTEGER NOT NULL CHECK(revision > 0),
 PRIMARY KEY(org_id,user_id), UNIQUE(email),
 FOREIGN KEY(org_id) REFERENCES production_registry_revisions(org_id),
 FOREIGN KEY(org_id,manager_id) REFERENCES production_registry_users(org_id,user_id)
);
CREATE TABLE production_registry_user_command_receipts (
 org_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 operation TEXT NOT NULL CHECK(operation='user.register'), principal_id TEXT NOT NULL,
 command_digest TEXT NOT NULL, result_user_id TEXT NOT NULL, result_revision INTEGER NOT NULL,
 email_digest TEXT NOT NULL, registry_fingerprint TEXT NOT NULL,
 resource_fingerprint TEXT NOT NULL, evidence_digest TEXT NOT NULL, created_at TEXT NOT NULL,
 authority_policy_revision_id TEXT NOT NULL, authority_policy_epoch INTEGER NOT NULL CHECK(authority_policy_epoch > 0),
 authority_policy_digest TEXT NOT NULL CHECK(length(authority_policy_digest)=64),
 PRIMARY KEY(org_id,idempotency_key),
 FOREIGN KEY(org_id) REFERENCES production_registry_revisions(org_id)
);
CREATE TABLE production_registry_user_audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action='UserRegistered'), principal_id TEXT NOT NULL,
 subject_id TEXT NOT NULL, approval_evidence_digest TEXT NOT NULL,
 command_digest TEXT NOT NULL, result_revision INTEGER NOT NULL,
 email_digest TEXT NOT NULL, registry_fingerprint TEXT NOT NULL,
 resource_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(org_id,subject_id) REFERENCES production_registry_users(org_id,user_id)
);
CREATE TABLE production_registry_user_outbox (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind='registry.user_registered'),
 subject_id TEXT NOT NULL, command_digest TEXT NOT NULL,
 result_revision INTEGER NOT NULL, email_digest TEXT NOT NULL,
 registry_fingerprint TEXT NOT NULL, resource_fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered=0),
 FOREIGN KEY(org_id,subject_id) REFERENCES production_registry_users(org_id,user_id)
);
CREATE TRIGGER production_registry_user_receipts_immutable
BEFORE UPDATE ON production_registry_user_command_receipts BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_registry_user_receipts_no_delete
BEFORE DELETE ON production_registry_user_command_receipts BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_registry_user_audit_immutable
BEFORE UPDATE ON production_registry_user_audit BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_registry_user_audit_no_delete
BEFORE DELETE ON production_registry_user_audit BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_registry_user_outbox_immutable
BEFORE UPDATE ON production_registry_user_outbox BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_registry_user_outbox_no_delete
BEFORE DELETE ON production_registry_user_outbox BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""
_LEGACY_V1_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS production_registry_revisions (
 org_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision >= 0)
);
CREATE TABLE production_registry_users (
 org_id TEXT NOT NULL, user_id TEXT NOT NULL, email TEXT NOT NULL,
 manager_id TEXT, revision INTEGER NOT NULL CHECK(revision > 0),
 PRIMARY KEY(org_id,user_id), UNIQUE(email),
 FOREIGN KEY(org_id) REFERENCES production_registry_revisions(org_id),
 FOREIGN KEY(org_id,manager_id) REFERENCES production_registry_users(org_id,user_id)
);
CREATE TABLE production_registry_user_command_receipts (
 org_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, command_digest TEXT NOT NULL,
 result_user_id TEXT NOT NULL, result_revision INTEGER NOT NULL,
 PRIMARY KEY(org_id,idempotency_key),
 FOREIGN KEY(org_id) REFERENCES production_registry_revisions(org_id)
);
CREATE TABLE production_registry_user_audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action='UserRegistered'), principal_id TEXT NOT NULL,
 subject_id TEXT NOT NULL, approval_evidence_digest TEXT NOT NULL,
 command_digest TEXT NOT NULL, result_revision INTEGER NOT NULL,
 FOREIGN KEY(org_id,subject_id) REFERENCES production_registry_users(org_id,user_id)
);
CREATE TABLE production_registry_user_outbox (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind='registry.user_registered'),
 subject_id TEXT NOT NULL, command_digest TEXT NOT NULL,
 result_revision INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered=0),
 FOREIGN KEY(org_id,subject_id) REFERENCES production_registry_users(org_id,user_id)
);
CREATE TRIGGER production_registry_user_receipts_immutable
BEFORE UPDATE ON production_registry_user_command_receipts BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_registry_user_receipts_no_delete
BEFORE DELETE ON production_registry_user_command_receipts BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""
_TABLES = frozenset(
    {
        "production_registry_revisions",
        "production_registry_users",
        "production_registry_user_command_receipts",
        "production_registry_user_audit",
        "production_registry_user_outbox",
    }
)


def _normalized_sql(value: str | None) -> str:
    return " ".join((value or "").split())


def _catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        (row[0], row[1], row[2], _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE 'production_registry_%' ORDER BY type,name"
        )
    )
    details: list[object] = []
    for table in sorted(_TABLES):
        details.append(
            (
                table,
                tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info('{table}')")),
                tuple(tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")),
                tuple(tuple(row) for row in connection.execute(f"PRAGMA index_list('{table}')")),
            )
        )
        for index in connection.execute(f"PRAGMA index_list('{table}')"):
            details.append(
                (
                    str(index[1]),
                    tuple(
                        tuple(row)
                        for row in connection.execute(f"PRAGMA index_info('{index[1]}')")
                    ),
                )
            )
    return objects, tuple(details)


def _canonical_catalog_for(schema: str) -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(schema)
        return _catalog(connection)
    finally:
        connection.close()


_CANONICAL_CATALOG = _canonical_catalog_for(_SCHEMA)
_LEGACY_V1_CANONICAL_CATALOG = _canonical_catalog_for(_LEGACY_V1_SCHEMA)


def validate_production_registry_user_connection(connection: sqlite3.Connection) -> None:
    """O2a parent capability가 exact canonical catalog인지 읽기 전용으로 검증한다."""
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ProductionRegistryUserUnavailable()
    if _catalog(connection) != _CANONICAL_CATALOG:
        raise ProductionRegistryUserUnavailable()


def production_registry_email_digest(email: str) -> str:
    return sha256(email.encode()).hexdigest()


def production_registry_user_command_digest(
    command: ProductionRegistryUserCommand,
) -> str:
    payload = command.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return sha256(encoded).hexdigest()


def production_registry_user_fingerprint(
    org_id: str, user_id: str, email: str, manager_id: str | None, revision: int
) -> str:
    return sha256(
        f"{org_id}\x00{user_id}\x00{email}\x00{manager_id or ''}\x00{revision}".encode()
    ).hexdigest()


def _production_registry_resource_fingerprint(
    org_id: str,
    user_id: str,
    revision: int,
    email_digest: str,
    registry_fingerprint: str,
) -> str:
    payload = {
        "org_id": org_id,
        "user_id": user_id,
        "revision": revision,
        "email_digest": email_digest,
        "registry_fingerprint": registry_fingerprint,
    }
    return sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def validate_production_registry_user_rows(
    connection: sqlite3.Connection, org_id: str, user_id: str
) -> ProductionRegistryUser:
    """Validate the current org graph and every registration companion without writes."""
    before = connection.total_changes
    try:
        validate_production_registry_user_connection(connection)
        revision_row = connection.execute(
            "SELECT revision FROM production_registry_revisions WHERE org_id=?",
            (org_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT org_id,user_id,email,manager_id,revision "
            "FROM production_registry_users WHERE org_id=? ORDER BY revision",
            (org_id,),
        ).fetchall()
        graph_revision = -1 if revision_row is None else int(revision_row[0])
        user_revisions = tuple(int(row[4]) for row in rows)
        if (
            revision_row is None
            or graph_revision < 0
            or any(revision < 1 or revision > graph_revision for revision in user_revisions)
            or len(set(user_revisions)) != len(user_revisions)
        ):
            raise ProductionRegistryUserUnavailable()
        known = {str(row[1]) for row in rows}
        if any(row[3] is not None and str(row[3]) not in known for row in rows):
            raise ProductionRegistryUserUnavailable()
        receipt_subjects = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT result_user_id FROM production_registry_user_command_receipts "
                "WHERE org_id=? ORDER BY result_user_id",
                (org_id,),
            )
        )
        audit_subjects = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT subject_id FROM production_registry_user_audit "
                "WHERE org_id=? ORDER BY subject_id",
                (org_id,),
            )
        )
        outbox_subjects = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT subject_id FROM production_registry_user_outbox "
                "WHERE org_id=? ORDER BY subject_id",
                (org_id,),
            )
        )
        expected_subjects = tuple(sorted(known))
        if (
            receipt_subjects != expected_subjects
            or audit_subjects != expected_subjects
            or outbox_subjects != expected_subjects
        ):
            raise ProductionRegistryUserUnavailable()
        managers = {
            str(row[1]): None if row[3] is None else str(row[3]) for row in rows
        }
        for subject in managers:
            visited: set[str] = set()
            cursor: str | None = subject
            while cursor is not None:
                if cursor in visited:
                    raise ProductionRegistryUserUnavailable()
                visited.add(cursor)
                cursor = managers[cursor]
        selected: ProductionRegistryUser | None = None
        for row in rows:
            current = ProductionRegistryUser(
                org_id=str(row[0]),
                user_id=str(row[1]),
                email=str(row[2]),
                manager_id=None if row[3] is None else str(row[3]),
                revision=int(row[4]),
            )
            _validate_production_registry_user_companions(connection, current)
            if current.user_id == user_id:
                selected = current
        if selected is None:
            raise ProductionRegistryUserUnavailable()
        return selected
    except ProductionRegistryUserUnavailable:
        raise
    except (ValueError, TypeError, sqlite3.Error) as error:
        raise ProductionRegistryUserUnavailable() from error
    finally:
        if connection.total_changes != before:
            raise ProductionRegistryUserUnavailable()


def _validate_production_registry_user_companions(
    connection: sqlite3.Connection, user: ProductionRegistryUser
) -> None:
    receipts = connection.execute(
        "SELECT * FROM production_registry_user_command_receipts "
        "WHERE org_id=? AND result_user_id=?",
        (user.org_id, user.user_id),
    ).fetchall()
    audits = connection.execute(
        "SELECT * FROM production_registry_user_audit "
        "WHERE org_id=? AND subject_id=?",
        (user.org_id, user.user_id),
    ).fetchall()
    outbox = connection.execute(
        "SELECT * FROM production_registry_user_outbox "
        "WHERE org_id=? AND subject_id=?",
        (user.org_id, user.user_id),
    ).fetchall()
    if len(receipts) != 1 or len(audits) != 1 or len(outbox) != 1:
        raise ProductionRegistryUserUnavailable()
    receipt, audit, event = receipts[0], audits[0], outbox[0]
    command = ProductionRegistryUserCommand(
        org_id=user.org_id,
        principal_id=str(receipt[3]),
        idempotency_key=str(receipt[1]),
        expected_revision=user.revision - 1,
        user_id=user.user_id,
        email=user.email,
        manager_id=user.manager_id,
    )
    command_digest = production_registry_user_command_digest(command)
    email_digest = production_registry_email_digest(user.email)
    registry_fingerprint = production_registry_user_fingerprint(
        user.org_id, user.user_id, user.email, user.manager_id, user.revision
    )
    resource_fingerprint = _production_registry_resource_fingerprint(
        user.org_id,
        user.user_id,
        user.revision,
        email_digest,
        registry_fingerprint,
    )
    common = (
        user.org_id,
        user.user_id,
        command_digest,
        user.revision,
        email_digest,
        registry_fingerprint,
        resource_fingerprint,
        receipt[11],
    )
    if (
        receipt[2] != "user.register"
        or receipt[4] != command_digest
        or int(receipt[6]) != user.revision
        or receipt[7] != email_digest
        or receipt[8] != registry_fingerprint
        or receipt[9] != resource_fingerprint
        or not _is_digest(str(receipt[10]))
        or (audit[1], audit[4], audit[6], audit[7], audit[8], audit[9], audit[10], audit[11])
        != common
        or audit[2] != "UserRegistered"
        or audit[3] != receipt[3]
        or audit[5] != receipt[10]
        or (event[1], event[3], event[4], event[5], event[6], event[7], event[8], event[9])
        != common
        or event[2] != "registry.user_registered"
        or int(event[10]) != 0
    ):
        raise ProductionRegistryUserUnavailable()


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _execute_schema(
    connection: sqlite3.Connection, schema: str, *, fault: FaultInjector
) -> None:
    statement = ""
    executed = 0
    for line in schema.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        statement = ""
        if not sql or sql.upper().startswith("PRAGMA "):
            continue
        connection.execute(sql)
        executed += 1
        if executed == 3:
            fault("mid-DDL")
    if statement.strip():
        raise ProductionRegistryUserUnavailable()


class SqliteProductionRegistryUsers:
    @classmethod
    def migrate(
        cls,
        path: str | Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        cls.migrate_v2(path, fault_injector=fault_injector)

    @classmethod
    def migrate_v2(
        cls,
        path: str | Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        fault = fault_injector or _no_fault
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            catalog = _catalog(connection)
            if catalog == _CANONICAL_CATALOG:
                return
            if not catalog[0]:
                connection.execute("BEGIN EXCLUSIVE")
                locked_catalog = _catalog(connection)
                if locked_catalog == _CANONICAL_CATALOG:
                    connection.commit()
                    return
                if locked_catalog[0]:
                    raise ProductionRegistryUserUnavailable()
                _execute_schema(connection, _SCHEMA, fault=fault)
                fault("pre-readback")
                if _catalog(connection) != _CANONICAL_CATALOG:
                    raise ProductionRegistryUserUnavailable()
                fault("precommit")
                connection.commit()
                return
            if catalog != _LEGACY_V1_CANONICAL_CATALOG:
                raise ProductionRegistryUserUnavailable()
            connection.execute("BEGIN EXCLUSIVE")
            locked_catalog = _catalog(connection)
            if locked_catalog == _CANONICAL_CATALOG:
                connection.commit()
                return
            if locked_catalog != _LEGACY_V1_CANONICAL_CATALOG:
                raise ProductionRegistryUserUnavailable()
            counts = {
                table: int(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                )
                for table in _TABLES
            }
            if any(counts.values()):
                raise LegacyProductionRegistryUsersUnverifiable(
                    counts=counts, reason="legacy_nonempty"
                )
            sequence_rows = tuple(
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT name,seq FROM sqlite_sequence "
                    "WHERE name LIKE 'production_registry_%' ORDER BY name"
                )
            )
            autoincrement_tables = {
                "production_registry_user_audit",
                "production_registry_user_outbox",
            }
            sequence_groups = {
                table: tuple(seq for name, seq in sequence_rows if name == table)
                for table in autoincrement_tables
            }
            unknown_sequence_rows = tuple(
                row for row in sequence_rows if row[0] not in autoincrement_tables
            )
            inconsistent_groups = tuple(
                sequences
                for sequences in sequence_groups.values()
                if len(sequences) > 1 or (sequences and sequences[0] != 0)
            )
            if unknown_sequence_rows or inconsistent_groups:
                raise LegacyProductionRegistryUsersUnverifiable(
                    counts={
                        **counts,
                        "autoincrement_history": len(unknown_sequence_rows)
                        + sum(len(sequences) for sequences in inconsistent_groups),
                    },
                    reason="legacy_history_unverifiable",
                )
            fault("pre-drop")
            for table in (
                "production_registry_user_outbox",
                "production_registry_user_audit",
                "production_registry_user_command_receipts",
                "production_registry_users",
                "production_registry_revisions",
            ):
                connection.execute(f"DROP TABLE {table}")
            fault("after-drop")
            _execute_schema(connection, _SCHEMA, fault=fault)
            fault("pre-readback")
            if _catalog(connection) != _CANONICAL_CATALOG:
                raise ProductionRegistryUserUnavailable()
            fault("precommit")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def __init__(
        self,
        path: str | Path,
        *,
        authorize: TxCurrentUserRegistrationAuthorizer,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        if not callable(getattr(authorize, "current", None)) or not callable(
            getattr(authorize, "verify_precommit", None)
        ):
            raise ValueError("transaction-current authorizer required")
        if not Path(path).is_file():
            raise ProductionRegistryUserUnavailable()
        self._authorize: TxCurrentUserRegistrationAuthorizer = authorize
        self._fault: FaultInjector = fault_injector or _no_fault
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._validate_manifest()

    def _validate_manifest(self) -> None:
        validate_production_registry_user_connection(self._connection)

    def register(self, command: ProductionRegistryUserCommand) -> ProductionRegistryUserResult:
        if type(command) is not ProductionRegistryUserCommand:
            raise ProductionRegistryUserUnavailable()
        with self._lock:
            tx = self._connection
            try:
                tx.execute("BEGIN IMMEDIATE")
                self._validate_manifest()
                evidence = self._current_authorization(command)
                command_digest = self._command_digest(command)
                receipt = tx.execute(
                    "SELECT command_digest,result_user_id,result_revision,created_at "
                    "FROM production_registry_user_command_receipts "
                    "WHERE org_id=? AND idempotency_key=?",
                    (command.org_id, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    if receipt["command_digest"] != command_digest:
                        raise ProductionRegistryUserConflict()
                    row = tx.execute(
                        "SELECT * FROM production_registry_users WHERE org_id=? AND user_id=?",
                        (command.org_id, receipt["result_user_id"]),
                    ).fetchone()
                    if row is None or int(row["revision"]) != int(receipt["result_revision"]):
                        raise ProductionRegistryUserUnavailable()
                    if (
                        row["org_id"] != command.org_id
                        or row["user_id"] != command.user_id
                        or row["email"] != command.email
                        or row["manager_id"] != command.manager_id
                    ):
                        raise ProductionRegistryUserUnavailable()
                    result = ProductionRegistryUserResult(
                        revision=int(row["revision"]),
                        user=ProductionRegistryUser.model_validate(dict(row)),
                    )
                    validate_production_registry_user_rows(
                        tx, command.org_id, command.user_id
                    )
                    # A receipt is an idempotency result, never a durable
                    # authorization grant.  In particular a new browser
                    # session may replay an exact command, but only after the
                    # request-scoped authorizer has re-read the *current*
                    # session, Registry binding and Authority at precommit.
                    # This deliberately happens after companion validation so
                    # a tampered historical receipt cannot be used as a
                    # policy probe.
                    if not self._authorize.verify_precommit(command, evidence, tx):
                        raise ProductionRegistryUserDenied()
                    replay_authority = canonical_v19_file_authority(
                        source_policy_digest=evidence.policy_digest,
                        current_snapshot_digest=evidence.policy_digest,
                    )
                    append_committed_source_evidence_if_v19(
                        tx, org_id=command.org_id,
                        receipt_id=f"registry:{command.idempotency_key}",
                        command_digest=str(receipt["command_digest"]),
                        event_type="registry_user_registered",
                        action="registry.user.register",
                        resource=SafeResourceRef(
                            kind="registry_user",
                            resource_id=str(receipt["result_user_id"]),
                        ),
                        change=RegistryChange(),
                        actor_user_id=command.principal_id,
                        occurred_at=str(receipt["created_at"]),
                        policy_revision_id=replay_authority.policy_revision_id,
                        policy_epoch=replay_authority.policy_epoch,
                        policy_digest=replay_authority.policy_digest,
                        source=SourceReceiptProvenance(
                            kind="registry_user_registration",
                            receipt_key=command.idempotency_key,
                            receipt_digest=source_receipt_digest(
                                tx, "registry_user_registration",
                                command.org_id, command.idempotency_key,
                            ),
                        ),
                    )
                    tx.commit()
                    return result.model_copy(update={"replayed": True})

                tx.execute(
                    "INSERT OR IGNORE INTO production_registry_revisions(org_id,revision) VALUES (?,0)",
                    (command.org_id,),
                )
                current = int(
                    tx.execute(
                        "SELECT revision FROM production_registry_revisions WHERE org_id=?",
                        (command.org_id,),
                    ).fetchone()["revision"]
                )
                if current != command.expected_revision:
                    raise ProductionRegistryUserRevisionConflict()
                if command.manager_id is not None:
                    manager = tx.execute(
                        "SELECT 1 FROM production_registry_users WHERE org_id=? AND user_id=?",
                        (command.org_id, command.manager_id),
                    ).fetchone()
                    if manager is None:
                        raise ProductionRegistryUserConflict()
                revision = current + 1
                try:
                    tx.execute(
                        "INSERT INTO production_registry_users"
                        "(org_id,user_id,email,manager_id,revision) VALUES (?,?,?,?,?)",
                        (
                            command.org_id,
                            command.user_id,
                            command.email,
                            command.manager_id,
                            revision,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise ProductionRegistryUserConflict() from error
                self._fault("after_user")
                tx.execute(
                    "UPDATE production_registry_revisions SET revision=? WHERE org_id=? AND revision=?",
                    (revision, command.org_id, current),
                )
                user = ProductionRegistryUser(
                    org_id=command.org_id,
                    user_id=command.user_id,
                    email=command.email,
                    manager_id=command.manager_id,
                    revision=revision,
                )
                result = ProductionRegistryUserResult(revision=revision, user=user)
                if not self._authorize.verify_precommit(command, evidence, tx):
                    raise ProductionRegistryUserDenied()
                created_at = str(
                    tx.execute(
                        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                    ).fetchone()[0]
                )
                email_digest = production_registry_email_digest(user.email)
                registry_fingerprint = production_registry_user_fingerprint(
                    user.org_id,
                    user.user_id,
                    user.email,
                    user.manager_id,
                    user.revision,
                )
                resource_fingerprint = _production_registry_resource_fingerprint(
                    user.org_id,
                    user.user_id,
                    user.revision,
                    email_digest,
                    registry_fingerprint,
                )
                audit_authority = canonical_v19_file_authority(
                    source_policy_digest=evidence.policy_digest,
                    current_snapshot_digest=evidence.policy_digest,
                )
                tx.execute(
                    "INSERT INTO production_registry_user_command_receipts "
                    "(org_id,idempotency_key,operation,principal_id,command_digest,"
                    "result_user_id,result_revision,email_digest,registry_fingerprint,"
                    "resource_fingerprint,evidence_digest,created_at,"
                    "authority_policy_revision_id,authority_policy_epoch,authority_policy_digest) "
                    "VALUES (?,?,'user.register',?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        command.org_id,
                        command.idempotency_key,
                        command.principal_id,
                        command_digest,
                        command.user_id,
                        revision,
                        email_digest,
                        registry_fingerprint,
                        resource_fingerprint,
                        evidence.evidence_digest,
                        created_at,
                        audit_authority.policy_revision_id,
                        audit_authority.policy_epoch,
                        audit_authority.policy_digest,
                    ),
                )
                append_committed_source_evidence_if_v19(
                    tx, org_id=command.org_id,
                    receipt_id=f"registry:{command.idempotency_key}",
                    command_digest=command_digest,
                    event_type="registry_user_registered", action="registry.user.register",
                    resource=SafeResourceRef(kind="registry_user", resource_id=command.user_id),
                    change=RegistryChange(), actor_user_id=command.principal_id,
                    occurred_at=created_at,
                    policy_revision_id=audit_authority.policy_revision_id,
                    policy_epoch=audit_authority.policy_epoch,
                    policy_digest=audit_authority.policy_digest,
                    source=SourceReceiptProvenance(
                        kind="registry_user_registration",
                        receipt_key=command.idempotency_key,
                        receipt_digest=source_receipt_digest(
                            tx,
                            "registry_user_registration",
                            command.org_id,
                            command.idempotency_key,
                        ),
                    ),
                )
                tx.execute(
                    "INSERT INTO production_registry_user_audit"
                    "(org_id,action,principal_id,subject_id,"
                    "approval_evidence_digest,command_digest,result_revision,"
                    "email_digest,registry_fingerprint,resource_fingerprint,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        command.org_id,
                        "UserRegistered",
                        command.principal_id,
                        command.user_id,
                        evidence.evidence_digest,
                        command_digest,
                        revision,
                        email_digest,
                        registry_fingerprint,
                        resource_fingerprint,
                        created_at,
                    ),
                )
                tx.execute(
                    "INSERT INTO production_registry_user_outbox"
                    "(org_id,kind,subject_id,command_digest,result_revision,"
                    "email_digest,registry_fingerprint,resource_fingerprint,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        command.org_id,
                        "registry.user_registered",
                        command.user_id,
                        command_digest,
                        revision,
                        email_digest,
                        registry_fingerprint,
                        resource_fingerprint,
                        created_at,
                    ),
                )
                validate_production_registry_user_rows(
                    tx, command.org_id, command.user_id
                )
                self._fault("before_commit")
                tx.commit()
                return result
            except Exception:
                tx.rollback()
                raise

    def _current_authorization(
        self, command: ProductionRegistryUserCommand
    ) -> CurrentUserRegistrationAuthorization:
        evidence = self._authorize.current(command, self._connection)
        if type(evidence) is not CurrentUserRegistrationAuthorization:
            raise ProductionRegistryUserDenied()
        return evidence

    @staticmethod
    def _command_digest(command: ProductionRegistryUserCommand) -> str:
        return production_registry_user_command_digest(command)

    def revision(self, org_id: str) -> int:
        row = self._connection.execute(
            "SELECT revision FROM production_registry_revisions WHERE org_id=?", (org_id,)
        ).fetchone()
        return 0 if row is None else int(row["revision"])

    def close(self) -> None:
        """Release the composition-owned SQLite connection."""
        self._connection.close()

    def users(self, org_id: str) -> tuple[ProductionRegistryUser, ...]:
        rows = self._connection.execute(
            "SELECT * FROM production_registry_users WHERE org_id=? ORDER BY user_id", (org_id,)
        ).fetchall()
        return tuple(ProductionRegistryUser.model_validate(dict(row)) for row in rows)

    def user_by_global_email(self, email: str) -> ProductionRegistryUser | None:
        row = self._connection.execute(
            "SELECT * FROM production_registry_users WHERE email=?", (email,)
        ).fetchone()
        return None if row is None else ProductionRegistryUser.model_validate(dict(row))

    def counts(self, org_id: str) -> dict[str, int]:
        names = {
            "receipts": "production_registry_user_command_receipts",
            "audit": "production_registry_user_audit",
            "outbox": "production_registry_user_outbox",
        }
        return {
            key: int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE org_id=?", (org_id,)  # noqa: S608
                ).fetchone()[0]
            )
            for key, table in names.items()
        }


__all__ = [
    "ProductionRegistryUser",
    "CurrentUserRegistrationAuthorization",
    "ProductionRegistryUserCommand",
    "ProductionRegistryUserConflict",
    "ProductionRegistryUserDenied",
    "ProductionRegistryUserError",
    "ProductionRegistryUserResult",
    "ProductionRegistryUserRevisionConflict",
    "ProductionRegistryUserUnavailable",
    "SqliteProductionRegistryUsers",
    "TxCurrentUserRegistrationAuthorizer",
    "production_registry_email_digest",
    "production_registry_user_command_digest",
    "production_registry_user_fingerprint",
    "validate_production_registry_user_connection",
    "validate_production_registry_user_rows",
]
