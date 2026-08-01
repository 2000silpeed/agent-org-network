"""Production Agent Card authoritative SQLite component (P17.15 O3a)."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, field_validator

from agent_org_network.agent_card import AgentCard
from agent_org_network.central_operational_evidence import (
    AgentCardChange,
    SafeResourceRef,
    SourceReceiptProvenance,
    append_committed_source_evidence_if_v19,
    canonical_v19_file_authority,
    source_receipt_digest,
)
from agent_org_network.sqlite_production_registry_users import (
    ProductionRegistryUserUnavailable,
    validate_production_registry_user_connection,
)


class ProductionAgentCardError(Exception):
    pass


class ProductionAgentCardUnavailable(ProductionAgentCardError):
    pass


class ProductionAgentCardDenied(ProductionAgentCardError):
    pass


class ProductionAgentCardConflict(ProductionAgentCardError):
    pass


class ProductionAgentCardInvalid(ProductionAgentCardConflict):
    """The submitted Card cannot pass canonical Registry admission."""


class ProductionAgentCardRevisionConflict(ProductionAgentCardConflict):
    pass


_CARD_FIELDS = frozenset(AgentCard.model_fields)


class ProductionAgentCardCommand(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    org_id: str
    principal_id: str
    idempotency_key: str
    expected_revision: int
    card: AgentCard

    @field_validator("org_id", "principal_id", "idempotency_key")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("card", mode="before")
    @classmethod
    def _exact_card_contract(cls, value: object) -> object:
        if isinstance(value, dict):
            raw = cast(dict[str, object], value)
            if frozenset(raw) - _CARD_FIELDS:
                raise ValueError("Agent Card 권한 자기보고 또는 미지원 필드는 허용되지 않습니다")
            return raw
        return value


class ProductionAgentCardResult(BaseModel, frozen=True):
    revision: int
    card: AgentCard
    replayed: bool = False


class CurrentCardRegistrationAuthorization(BaseModel, frozen=True):
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
    def _digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("canonical digest required")
        return value


class TxCurrentCardRegistrationAuthorizer(Protocol):
    def current(
        self, command: ProductionAgentCardCommand, transaction: sqlite3.Connection
    ) -> CurrentCardRegistrationAuthorization: ...

    def verify_precommit(
        self,
        command: ProductionAgentCardCommand,
        evidence: CurrentCardRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool: ...


FaultInjector = Callable[[str], None]


def _no_fault(_point: str) -> None:
    return None


def _execute_schema(
    connection: sqlite3.Connection, schema: str, *, fault: FaultInjector
) -> None:
    """Run the Card capability DDL atomically while exposing a deterministic fault seam."""
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
        raise ProductionAgentCardUnavailable()


_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE production_agent_cards (
 org_id TEXT NOT NULL, agent_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 maintainer_id TEXT, card_json TEXT NOT NULL, card_digest TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision > 0),
 PRIMARY KEY(org_id,agent_id),
 FOREIGN KEY(org_id) REFERENCES production_registry_revisions(org_id),
 FOREIGN KEY(org_id,owner_id) REFERENCES production_registry_users(org_id,user_id),
 FOREIGN KEY(org_id,maintainer_id) REFERENCES production_registry_users(org_id,user_id)
);
CREATE TABLE production_agent_card_command_receipts (
 org_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, command_digest TEXT NOT NULL,
 result_agent_id TEXT NOT NULL, result_card_digest TEXT NOT NULL,
 result_revision INTEGER NOT NULL, authority_epoch INTEGER NOT NULL CHECK(authority_epoch >= 0),
 policy_digest TEXT NOT NULL, evidence_digest TEXT NOT NULL,
 resource_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
 principal_id TEXT NOT NULL, authority_policy_revision_id TEXT NOT NULL,
 authority_policy_epoch INTEGER NOT NULL CHECK(authority_policy_epoch > 0),
 authority_policy_digest TEXT NOT NULL CHECK(length(authority_policy_digest)=64),
 PRIMARY KEY(org_id,idempotency_key),
 FOREIGN KEY(org_id,result_agent_id) REFERENCES production_agent_cards(org_id,agent_id)
);
CREATE TABLE production_agent_card_audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action='AgentCardRegistered'), principal_id TEXT NOT NULL,
 subject_id TEXT NOT NULL, approval_evidence_digest TEXT NOT NULL,
 authority_epoch INTEGER NOT NULL CHECK(authority_epoch >= 0), policy_digest TEXT NOT NULL,
 resource_fingerprint TEXT NOT NULL, command_digest TEXT NOT NULL,
 card_digest TEXT NOT NULL, result_revision INTEGER NOT NULL,
 FOREIGN KEY(org_id,subject_id) REFERENCES production_agent_cards(org_id,agent_id)
);
CREATE TABLE production_agent_card_outbox (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind='registry.agent_card_registered'),
 subject_id TEXT NOT NULL, command_digest TEXT NOT NULL,
 card_digest TEXT NOT NULL, resource_fingerprint TEXT NOT NULL,
 result_revision INTEGER NOT NULL,
 delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered=0),
 FOREIGN KEY(org_id,subject_id) REFERENCES production_agent_cards(org_id,agent_id)
);
CREATE TRIGGER production_agent_card_receipts_immutable
BEFORE UPDATE ON production_agent_card_command_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_agent_card_receipts_no_delete
BEFORE DELETE ON production_agent_card_command_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_agent_card_audit_immutable
BEFORE UPDATE ON production_agent_card_audit
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_agent_card_audit_no_delete
BEFORE DELETE ON production_agent_card_audit
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_agent_card_outbox_immutable
BEFORE UPDATE ON production_agent_card_outbox
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_agent_card_outbox_no_delete
BEFORE DELETE ON production_agent_card_outbox
BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""

_TABLES = frozenset(
    {
        "production_agent_cards",
        "production_agent_card_command_receipts",
        "production_agent_card_audit",
        "production_agent_card_outbox",
    }
)
_TRIGGERS = frozenset(
    {
        "production_agent_card_receipts_immutable",
        "production_agent_card_receipts_no_delete",
        "production_agent_card_audit_immutable",
        "production_agent_card_audit_no_delete",
        "production_agent_card_outbox_immutable",
        "production_agent_card_outbox_no_delete",
    }
)


def _normalized_sql(value: str | None) -> str:
    return " ".join((value or "").split())


def _catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        (row[0], row[1], row[2], _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE 'production_agent_card%' ORDER BY type,name"
        )
    )
    details: list[object] = []
    for table in sorted(_TABLES):
        details.append(
            (
                table,
                tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info('{table}')")),
                tuple(
                    tuple(row)
                    for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")
                ),
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


def _canonical_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return _catalog(connection)
    finally:
        connection.close()


_CANONICAL_CATALOG = _canonical_catalog()


def validate_production_agent_card_connection(connection: sqlite3.Connection) -> None:
    """O3 parent capability가 O1/O2 exact canonical catalog인지 검증한다."""
    try:
        validate_production_registry_user_connection(connection)
    except ProductionRegistryUserUnavailable as error:
        raise ProductionAgentCardUnavailable() from error
    if _catalog(connection) != _CANONICAL_CATALOG:
        raise ProductionAgentCardUnavailable()


def validate_production_agent_card_rows(
    connection: sqlite3.Connection, org_id: str
) -> None:
    """Validate shared revision allocation and every stored Card companion."""
    before = connection.total_changes
    previous_row_factory = connection.row_factory
    try:
        connection.row_factory = sqlite3.Row
        validate_production_agent_card_connection(connection)
        revision_row = connection.execute(
            "SELECT revision FROM production_registry_revisions WHERE org_id=?",
            (org_id,),
        ).fetchone()
        if revision_row is None:
            raise ProductionAgentCardUnavailable()
        current = int(revision_row[0])
        user_revisions = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT revision FROM production_registry_users WHERE org_id=?",
                (org_id,),
            )
        )
        cards = connection.execute(
            "SELECT * FROM production_agent_cards WHERE org_id=? ORDER BY agent_id",
            (org_id,),
        ).fetchall()
        card_revisions = tuple(int(row["revision"]) for row in cards)
        allocated = user_revisions + card_revisions
        if (
            current < 0
            or len(set(allocated)) != len(allocated)
            or tuple(sorted(allocated)) != tuple(range(1, current + 1))
        ):
            raise ProductionAgentCardUnavailable()
        for row in cards:
            card = SqliteProductionAgentCards._decode_card(row)  # pyright: ignore[reportPrivateUsage]
            receipts = connection.execute(
                "SELECT * FROM production_agent_card_command_receipts "
                "WHERE org_id=? AND result_agent_id=?",
                (org_id, card.agent_id),
            ).fetchall()
            audits = connection.execute(
                "SELECT * FROM production_agent_card_audit "
                "WHERE org_id=? AND subject_id=?",
                (org_id, card.agent_id),
            ).fetchall()
            outbox = connection.execute(
                "SELECT * FROM production_agent_card_outbox "
                "WHERE org_id=? AND subject_id=?",
                (org_id, card.agent_id),
            ).fetchall()
            if len(receipts) != 1 or len(audits) != 1 or len(outbox) != 1:
                raise ProductionAgentCardUnavailable()
            receipt, audit, event = receipts[0], audits[0], outbox[0]
            command = ProductionAgentCardCommand(
                org_id=org_id,
                principal_id=str(audit["principal_id"]),
                idempotency_key=str(receipt["idempotency_key"]),
                expected_revision=int(row["revision"]) - 1,
                card=card,
            )
            command_digest = SqliteProductionAgentCards._command_digest(command)  # pyright: ignore[reportPrivateUsage]
            resource_fingerprint = SqliteProductionAgentCards._resource_fingerprint(  # pyright: ignore[reportPrivateUsage]
                command, str(row["card_digest"]), int(row["revision"])
            )
            common = (
                org_id,
                card.agent_id,
                row["card_digest"],
                row["revision"],
            )
            if (
                (
                    receipt["org_id"],
                    receipt["result_agent_id"],
                    receipt["result_card_digest"],
                    receipt["result_revision"],
                )
                != common
                or receipt["command_digest"] != command_digest
                or receipt["resource_fingerprint"] != resource_fingerprint
                or int(receipt["authority_epoch"]) < 0
                or re.fullmatch(r"[0-9a-f]{64}", str(receipt["policy_digest"])) is None
                or re.fullmatch(r"[0-9a-f]{64}", str(receipt["evidence_digest"])) is None
                or not str(receipt["created_at"]).strip()
                or audit["action"] != "AgentCardRegistered"
                or audit["principal_id"] != command.principal_id
                or audit["subject_id"] != card.agent_id
                or audit["approval_evidence_digest"] != receipt["evidence_digest"]
                or int(audit["authority_epoch"]) != int(receipt["authority_epoch"])
                or audit["policy_digest"] != receipt["policy_digest"]
                or audit["command_digest"] != command_digest
                or audit["card_digest"] != row["card_digest"]
                or int(audit["result_revision"]) != int(row["revision"])
                or audit["resource_fingerprint"] != resource_fingerprint
                or event["kind"] != "registry.agent_card_registered"
                or event["subject_id"] != card.agent_id
                or event["command_digest"] != command_digest
                or event["card_digest"] != row["card_digest"]
                or int(event["result_revision"]) != int(row["revision"])
                or event["resource_fingerprint"] != resource_fingerprint
                or int(event["delivered"]) != 0
            ):
                raise ProductionAgentCardUnavailable()
    except ProductionAgentCardUnavailable:
        raise
    except (TypeError, ValueError, sqlite3.Error) as error:
        raise ProductionAgentCardUnavailable() from error
    finally:
        connection.row_factory = previous_row_factory
        if connection.total_changes != before:
            raise ProductionAgentCardUnavailable()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class SqliteProductionAgentCards:
    @classmethod
    def migrate(
        cls,
        path: str | Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        """Mount only the exact canonical Card capability; never repair drift."""
        connection = sqlite3.connect(str(path))
        fault = fault_injector or _no_fault
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                validate_production_registry_user_connection(connection)
            except ProductionRegistryUserUnavailable as error:
                raise ProductionAgentCardUnavailable() from error
            catalog = _catalog(connection)
            if catalog == _CANONICAL_CATALOG:
                return
            if catalog[0]:
                raise ProductionAgentCardUnavailable()
            connection.execute("BEGIN EXCLUSIVE")
            locked_catalog = _catalog(connection)
            if locked_catalog == _CANONICAL_CATALOG:
                connection.commit()
                return
            if locked_catalog[0]:
                raise ProductionAgentCardUnavailable()
            _execute_schema(connection, _SCHEMA, fault=fault)
            fault("pre-readback")
            validate_production_agent_card_connection(connection)
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
        authorize: TxCurrentCardRegistrationAuthorizer,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        if not callable(getattr(authorize, "current", None)) or not callable(
            getattr(authorize, "verify_precommit", None)
        ):
            raise ValueError("transaction-current authorizer required")
        if not Path(path).is_file():
            raise ProductionAgentCardUnavailable()
        self._authorize = authorize
        self._fault = fault_injector or _no_fault
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._validate_manifest()

    def _validate_manifest(self) -> None:
        try:
            validate_production_registry_user_connection(self._connection)
        except ProductionRegistryUserUnavailable as error:
            raise ProductionAgentCardUnavailable() from error
        if _catalog(self._connection) != _CANONICAL_CATALOG:
            raise ProductionAgentCardUnavailable()

    def register(self, command: ProductionAgentCardCommand) -> ProductionAgentCardResult:
        if type(command) is not ProductionAgentCardCommand:
            raise ProductionAgentCardUnavailable()
        with self._lock:
            tx = self._connection
            try:
                tx.execute("BEGIN IMMEDIATE")
                self._validate_manifest()
                evidence = self._authorize.current(command, tx)
                if type(evidence) is not CurrentCardRegistrationAuthorization:
                    raise ProductionAgentCardDenied()
                command_digest = self._command_digest(command)
                receipt = tx.execute(
                    "SELECT * FROM production_agent_card_command_receipts "
                    "WHERE org_id=? AND idempotency_key=?",
                    (command.org_id, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    if receipt["command_digest"] != command_digest:
                        raise ProductionAgentCardConflict()
                    row = tx.execute(
                        "SELECT * FROM production_agent_cards WHERE org_id=? AND agent_id=?",
                        (command.org_id, receipt["result_agent_id"]),
                    ).fetchone()
                    if (
                        row is None
                        or row["card_digest"] != receipt["result_card_digest"]
                        or int(row["revision"]) != int(receipt["result_revision"])
                    ):
                        raise ProductionAgentCardUnavailable()
                    card = self._decode_card(row)
                    if self._semantic_card(card) != self._semantic_card(command.card):
                        raise ProductionAgentCardUnavailable()
                    self._verify_replay_companions(command, receipt)
                    # Replay still performs a second in-transaction current
                    # authorization read.  Its evidence may legitimately differ
                    # from the immutable original receipt after a policy reload.
                    if not self._authorize.verify_precommit(command, evidence, tx):
                        raise ProductionAgentCardDenied()
                    replay_authority = canonical_v19_file_authority(
                        source_policy_digest=str(receipt["authority_policy_digest"]),
                        current_snapshot_digest=str(receipt["authority_policy_digest"]),
                    )
                    append_committed_source_evidence_if_v19(
                        tx,
                        org_id=command.org_id,
                        receipt_id=f"agent-card:{command.idempotency_key}",
                        command_digest=command_digest,
                        event_type="agent_card_registered",
                        action="registry.agent_card.register",
                        resource=SafeResourceRef(
                            kind="agent_card", resource_id=card.agent_id
                        ),
                        change=AgentCardChange(card_id=card.agent_id),
                        actor_user_id=command.principal_id,
                        occurred_at=str(receipt["created_at"]),
                        policy_revision_id=replay_authority.policy_revision_id,
                        policy_epoch=replay_authority.policy_epoch,
                        policy_digest=replay_authority.policy_digest,
                        source=SourceReceiptProvenance(
                            kind="agent_card_registration",
                            receipt_key=command.idempotency_key,
                            receipt_digest=source_receipt_digest(
                                tx,
                                "agent_card_registration",
                                command.org_id,
                                command.idempotency_key,
                            ),
                        ),
                    )
                    tx.commit()
                    return ProductionAgentCardResult(
                        revision=int(row["revision"]), card=card, replayed=True
                    )

                revision_row = tx.execute(
                    "SELECT revision FROM production_registry_revisions WHERE org_id=?",
                    (command.org_id,),
                ).fetchone()
                current = 0 if revision_row is None else int(revision_row["revision"])
                if revision_row is None or current != command.expected_revision:
                    raise ProductionAgentCardRevisionConflict()
                card = self._admit(command, tx)
                card_json = _canonical_json(card.model_dump(mode="json"))
                card_digest = sha256(card_json.encode()).hexdigest()
                revision = current + 1
                resource_fingerprint = self._resource_fingerprint(
                    command, card_digest, revision
                )
                try:
                    tx.execute(
                        "INSERT INTO production_agent_cards"
                        "(org_id,agent_id,owner_id,maintainer_id,card_json,card_digest,revision)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (
                            command.org_id,
                            card.agent_id,
                            card.owner,
                            card.maintainer,
                            card_json,
                            card_digest,
                            revision,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise ProductionAgentCardConflict() from error
                self._fault("after_card")
                changed = tx.execute(
                    "UPDATE production_registry_revisions SET revision=? "
                    "WHERE org_id=? AND revision=?",
                    (revision, command.org_id, current),
                ).rowcount
                if changed != 1:
                    raise ProductionAgentCardRevisionConflict()
                self._fault("after_revision")
                if not self._authorize.verify_precommit(command, evidence, tx):
                    raise ProductionAgentCardDenied()
                created_at = str(
                    tx.execute(
                        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                    ).fetchone()[0]
                )
                audit_authority = canonical_v19_file_authority(
                    source_policy_digest=evidence.policy_digest,
                    current_snapshot_digest=evidence.policy_digest,
                )
                tx.execute(
                    "INSERT INTO production_agent_card_command_receipts "
                    "(org_id,idempotency_key,command_digest,result_agent_id,result_card_digest,"
                    "result_revision,authority_epoch,policy_digest,evidence_digest,resource_fingerprint,"
                    "created_at,principal_id,authority_policy_revision_id,authority_policy_epoch,"
                    "authority_policy_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        command.org_id,
                        command.idempotency_key,
                        command_digest,
                        card.agent_id,
                        card_digest,
                        revision,
                        evidence.authority_epoch,
                        evidence.policy_digest,
                        evidence.evidence_digest,
                        resource_fingerprint,
                        created_at,
                        command.principal_id,
                        audit_authority.policy_revision_id,
                        audit_authority.policy_epoch,
                        audit_authority.policy_digest,
                    ),
                )
                self._fault("after_receipt")
                append_committed_source_evidence_if_v19(
                    tx,
                    org_id=command.org_id,
                    receipt_id=f"agent-card:{command.idempotency_key}",
                    command_digest=command_digest,
                    event_type="agent_card_registered",
                    action="registry.agent_card.register",
                    resource=SafeResourceRef(
                        kind="agent_card", resource_id=card.agent_id
                    ),
                    change=AgentCardChange(card_id=card.agent_id),
                    actor_user_id=command.principal_id,
                    occurred_at=created_at,
                    policy_revision_id=audit_authority.policy_revision_id,
                    policy_epoch=audit_authority.policy_epoch,
                    policy_digest=audit_authority.policy_digest,
                    source=SourceReceiptProvenance(
                        kind="agent_card_registration",
                        receipt_key=command.idempotency_key,
                        receipt_digest=source_receipt_digest(
                            tx,
                            "agent_card_registration",
                            command.org_id,
                            command.idempotency_key,
                        ),
                    ),
                )
                tx.execute(
                    "INSERT INTO production_agent_card_audit"
                    "(org_id,action,principal_id,subject_id,approval_evidence_digest,"
                    "authority_epoch,policy_digest,resource_fingerprint,"
                    "command_digest,card_digest,result_revision)"
                    " VALUES (?,'AgentCardRegistered',?,?,?,?,?,?,?,?,?)",
                    (
                        command.org_id,
                        command.principal_id,
                        card.agent_id,
                        evidence.evidence_digest,
                        evidence.authority_epoch,
                        evidence.policy_digest,
                        resource_fingerprint,
                        command_digest,
                        card_digest,
                        revision,
                    ),
                )
                self._fault("after_audit")
                tx.execute(
                    "INSERT INTO production_agent_card_outbox"
                    "(org_id,kind,subject_id,command_digest,card_digest,"
                    "resource_fingerprint,result_revision)"
                    " VALUES (?,'registry.agent_card_registered',?,?,?,?,?)",
                    (
                        command.org_id,
                        card.agent_id,
                        command_digest,
                        card_digest,
                        resource_fingerprint,
                        revision,
                    ),
                )
                self._fault("after_outbox")
                self._fault("before_commit")
                tx.commit()
                return ProductionAgentCardResult(revision=revision, card=card)
            except Exception:
                tx.rollback()
                raise

    def _verify_replay_companions(
        self,
        command: ProductionAgentCardCommand,
        receipt: sqlite3.Row,
    ) -> None:
        expected_resource = self._resource_fingerprint(
            command, str(receipt["result_card_digest"]), int(receipt["result_revision"])
        )
        if (
            int(receipt["authority_epoch"]) < 0
            or re.fullmatch(r"[0-9a-f]{64}", str(receipt["policy_digest"])) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(receipt["evidence_digest"])) is None
            or not str(receipt["created_at"]).strip()
            or receipt["resource_fingerprint"] != expected_resource
        ):
            raise ProductionAgentCardUnavailable()
        expected_common = (
            command.org_id,
            receipt["result_agent_id"],
            receipt["command_digest"],
            receipt["result_card_digest"],
            receipt["result_revision"],
        )
        audits = self._connection.execute(
            "SELECT org_id,action,subject_id,command_digest,card_digest,result_revision,"
            "principal_id,approval_evidence_digest,authority_epoch,policy_digest,"
            "resource_fingerprint "
            "FROM production_agent_card_audit WHERE org_id=? AND command_digest=?",
            (command.org_id, receipt["command_digest"]),
        ).fetchall()
        outbox = self._connection.execute(
            "SELECT org_id,kind,subject_id,command_digest,card_digest,result_revision,"
            "resource_fingerprint "
            "FROM production_agent_card_outbox WHERE org_id=? AND command_digest=?",
            (command.org_id, receipt["command_digest"]),
        ).fetchall()
        if len(audits) != 1 or len(outbox) != 1:
            raise ProductionAgentCardUnavailable()
        audit = audits[0]
        if (
            tuple(audit[name] for name in (
                "org_id", "subject_id", "command_digest", "card_digest", "result_revision"
            ))
            != expected_common
            or audit["principal_id"] != command.principal_id
            or audit["action"] != "AgentCardRegistered"
            or audit["approval_evidence_digest"] != receipt["evidence_digest"]
            or int(audit["authority_epoch"]) != int(receipt["authority_epoch"])
            or audit["policy_digest"] != receipt["policy_digest"]
            or audit["resource_fingerprint"] != receipt["resource_fingerprint"]
        ):
            raise ProductionAgentCardUnavailable()
        event = outbox[0]
        if tuple(
            event[name]
            for name in ("org_id", "subject_id", "command_digest", "card_digest", "result_revision")
        ) != expected_common or event["kind"] != "registry.agent_card_registered" or event[
            "resource_fingerprint"
        ] != receipt["resource_fingerprint"]:
            raise ProductionAgentCardUnavailable()

    @staticmethod
    def _semantic_card(card: AgentCard) -> dict[str, object]:
        value = card.model_dump(mode="json")
        value.pop("last_reviewed_at")
        return value

    @classmethod
    def _command_digest(cls, command: ProductionAgentCardCommand) -> str:
        payload = command.model_dump(mode="json")
        payload["card"] = cls._semantic_card(command.card)
        return sha256(_canonical_json(payload).encode()).hexdigest()

    @staticmethod
    def _resource_fingerprint(
        command: ProductionAgentCardCommand, card_digest: str, revision: int
    ) -> str:
        return sha256(
            _canonical_json(
                {
                    "org_id": command.org_id,
                    "agent_id": command.card.agent_id,
                    "owner": command.card.owner,
                    "maintainer": command.card.maintainer,
                    "card_digest": card_digest,
                    "revision": revision,
                }
            ).encode()
        ).hexdigest()

    @staticmethod
    def _admit(
        command: ProductionAgentCardCommand, transaction: sqlite3.Connection
    ) -> AgentCard:
        rows = transaction.execute(
            "SELECT user_id FROM production_registry_users WHERE org_id=?",
            (command.org_id,),
        ).fetchall()
        user_ids = {str(row["user_id"]) for row in rows}
        errors: list[str] = []
        if command.card.owner not in user_ids:
            errors.append("owner is not an admitted Registry User")
        if command.card.maintainer is not None and command.card.maintainer not in user_ids:
            errors.append("maintainer is not an admitted Registry User")
        if errors:
            # A malformed Card or an unknown owner/maintainer is an admission
            # failure, not an idempotency/duplicate semantic conflict.  The
            # private Central boundary maps this typed distinction to 422
            # without exposing the individual reasons.
            raise ProductionAgentCardInvalid("; ".join(errors))
        # `ProductionAgentCardCommand.card` is itself the frozen canonical
        # Agent Card admission value.  Keeping the reference-integrity check
        # here avoids pulling the legacy Admin Registry service (and its
        # runtime/audit graph) into the sealed Central installation.
        return command.card

    @staticmethod
    def _decode_card(row: sqlite3.Row) -> AgentCard:
        try:
            raw: Any = json.loads(str(row["card_json"]))
            card = AgentCard.model_validate(raw)
        except Exception as error:
            raise ProductionAgentCardUnavailable() from error
        canonical = _canonical_json(card.model_dump(mode="json"))
        if canonical != row["card_json"] or sha256(canonical.encode()).hexdigest() != row[
            "card_digest"
        ]:
            raise ProductionAgentCardUnavailable()
        if card.agent_id != row["agent_id"] or card.owner != row["owner_id"]:
            raise ProductionAgentCardUnavailable()
        if card.maintainer != row["maintainer_id"]:
            raise ProductionAgentCardUnavailable()
        return card

    def revision(self, org_id: str) -> int:
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                self._validate_manifest()
                row = self._connection.execute(
                    "SELECT revision FROM production_registry_revisions WHERE org_id=?", (org_id,)
                ).fetchone()
                self._connection.commit()
                return 0 if row is None else int(row["revision"])
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        """Release a request-scoped Card store connection, best-effort/idempotently."""
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass

    def cards(self, org_id: str) -> tuple[AgentCard, ...]:
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                self._validate_manifest()
                rows = self._connection.execute(
                    "SELECT * FROM production_agent_cards WHERE org_id=? ORDER BY agent_id",
                    (org_id,),
                ).fetchall()
                cards = tuple(self._decode_card(row) for row in rows)
                self._connection.commit()
                return cards
            except Exception:
                self._connection.rollback()
                raise

    def counts(self, org_id: str) -> dict[str, int]:
        tables = {
            "receipts": "production_agent_card_command_receipts",
            "audit": "production_agent_card_audit",
            "outbox": "production_agent_card_outbox",
        }
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                self._validate_manifest()
                counts = {
                    key: int(
                        self._connection.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE org_id=?",  # noqa: S608
                            (org_id,),
                        ).fetchone()[0]
                    )
                    for key, table in tables.items()
                }
                self._connection.commit()
                return counts
            except Exception:
                self._connection.rollback()
                raise


__all__ = [
    "CurrentCardRegistrationAuthorization",
    "ProductionAgentCardCommand",
    "ProductionAgentCardConflict",
    "ProductionAgentCardDenied",
    "ProductionAgentCardError",
    "ProductionAgentCardInvalid",
    "ProductionAgentCardResult",
    "ProductionAgentCardRevisionConflict",
    "ProductionAgentCardUnavailable",
    "SqliteProductionAgentCards",
    "TxCurrentCardRegistrationAuthorizer",
    "validate_production_agent_card_connection",
    "validate_production_agent_card_rows",
]
