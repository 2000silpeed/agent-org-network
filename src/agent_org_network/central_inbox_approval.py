"""Durable Central Approval inbox/reassignment capability (RB3.2b.5-C).

v16 keeps the v12 ApprovalItem and disposition records canonical.  It widens
ApprovalItem cardinality for immutable assignment generations and adds only
assignment lineage plus reassignment receipts/audits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal, Protocol, cast
from uuid import uuid4

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.central_operational_evidence import (
    ApprovalChange,
    SafeResourceRef,
    SourceReceiptProvenance,
    append_committed_source_evidence_if_v19,
    canonical_v19_file_authority,
    source_receipt_digest,
)
from agent_org_network.central_inbox_conflict import (
    central_inbox_conflict_schema_ready,
)
from agent_org_network.central_question_lifecycle import (
    ApprovalDispositionApplication,
    ApprovalDispositionAuthorizationProof,
    ApprovalDispositionAuthority,
    ApprovalDispositionCommand,
    CENTRAL_QUESTION_APPROVAL_ITEMS_V16_DDL,
    CentralQuestionLifecycleConflict,
    CentralQuestionLifecycleUnavailable,
    canonical_lifecycle_json,
    central_question_lifecycle_schema_ready,
    compare_and_set_lifecycle_request,
    read_lifecycle_request,
    validate_central_question_lifecycle_connection,
)
from agent_org_network.question_request import AwaitingApproval, QuestionRequest


ApprovalItemState = Literal["open", "approved", "rejected", "superseded"]
ApprovalDispositionKind = Literal["approve", "approve_with_edit", "reject"]

_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT = "central-inbox-approval"
_VERSION = 16

_ASSIGNMENT_DDL = """CREATE TABLE central_inbox_approval_assignments (
 approval_item_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
 org_id TEXT NOT NULL, approval_round INTEGER NOT NULL CHECK(approval_round>0),
 predecessor_approval_item_id TEXT UNIQUE,
 assigned_approver_user_id TEXT NOT NULL, assigned_approval_card_id TEXT NOT NULL,
 assigned_card_revision INTEGER NOT NULL CHECK(assigned_card_revision>0),
 assigned_card_digest TEXT NOT NULL CHECK(length(assigned_card_digest)=64),
 assigned_at TEXT NOT NULL, due_at TEXT NOT NULL,
 FOREIGN KEY(approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(predecessor_approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(ticket_id) REFERENCES central_question_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 UNIQUE(request_id,approval_round)
)"""

_RECEIPT_DDL = """CREATE TABLE central_inbox_approval_reassignment_receipts (
 receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, request_id TEXT NOT NULL,
 superseded_approval_item_id TEXT NOT NULL UNIQUE,
 successor_approval_item_id TEXT NOT NULL UNIQUE,
 actor_id TEXT NOT NULL, identity_session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
 expected_approval_item_revision INTEGER NOT NULL CHECK(expected_approval_item_revision>0),
 expected_request_revision INTEGER NOT NULL CHECK(expected_request_revision>=0),
 target_approver_user_id TEXT NOT NULL, target_approval_card_id TEXT NOT NULL,
 target_card_revision INTEGER NOT NULL CHECK(target_card_revision>0),
 target_card_digest TEXT NOT NULL CHECK(length(target_card_digest)=64),
 resulting_superseded_revision INTEGER NOT NULL CHECK(resulting_superseded_revision>1),
 resulting_successor_revision INTEGER NOT NULL CHECK(resulting_successor_revision>0),
 resulting_request_revision INTEGER NOT NULL CHECK(resulting_request_revision>0),
 authority_policy_version TEXT NOT NULL, authority_policy_digest TEXT NOT NULL CHECK(length(authority_policy_digest)=64),
 authority_policy_revision_id TEXT NOT NULL, authority_policy_epoch INTEGER NOT NULL CHECK(authority_policy_epoch>0),
 created_at TEXT NOT NULL,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(superseded_approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(successor_approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 UNIQUE(org_id,actor_id,idempotency_key)
)"""

_AUDIT_DDL = """CREATE TABLE central_inbox_approval_reassignment_audits (
 receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, request_id TEXT NOT NULL,
 superseded_approval_item_id TEXT NOT NULL UNIQUE,
 successor_approval_item_id TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL,
 command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
 resulting_request_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(receipt_id) REFERENCES central_inbox_approval_reassignment_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(superseded_approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(successor_approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

_MARKER_DDL = """CREATE TABLE central_inbox_approval_component_schema (
 name TEXT PRIMARY KEY NOT NULL CHECK(name='central-inbox-approval'),
 version INTEGER NOT NULL CHECK(version=16)
)"""

_TABLES = {
    "central_inbox_approval_assignments": _ASSIGNMENT_DDL,
    "central_inbox_approval_reassignment_receipts": _RECEIPT_DDL,
    "central_inbox_approval_reassignment_audits": _AUDIT_DDL,
    "central_inbox_approval_component_schema": _MARKER_DDL,
}
_INDEXES = (
    "CREATE INDEX central_inbox_approval_open_by_assignee "
    "ON central_inbox_approval_assignments(org_id,assigned_approver_user_id,assigned_at,approval_item_id)",
    "CREATE INDEX central_inbox_approval_lineage "
    "ON central_inbox_approval_assignments(request_id,approval_round,approval_item_id)",
)
_TRIGGERS = tuple(
    statement
    for table, label in (
        ("central_inbox_approval_assignments", "assignment"),
        ("central_inbox_approval_reassignment_receipts", "reassignment receipt"),
        ("central_inbox_approval_reassignment_audits", "reassignment audit"),
    )
    for statement in (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT,'immutable approval {label}'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT,'immutable approval {label}'); END",
    )
)


class ApprovalInboxUnavailable(RuntimeError):
    pass


class ApprovalSessionUnauthenticated(ApprovalInboxUnavailable):
    pass


class ApprovalInboxNotFound(RuntimeError):
    pass


class ApprovalInboxStaleOrConflict(RuntimeError):
    pass


class _ApprovalBindingNotCurrent(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalReadCommand:
    identity_session_id: str
    expected_org_id: str
    expected_actor_id: str


@dataclass(frozen=True, slots=True)
class ApprovalItemSummary:
    approval_item_id: str
    request_id: str
    request_revision: int
    approval_round: int
    revision: int
    assigned_at: datetime
    due_at: datetime
    state: ApprovalItemState


@dataclass(frozen=True, slots=True)
class ApprovalItemDetail(ApprovalItemSummary):
    question: str
    candidate_text: str
    candidate_digest: str
    policy_digest: str
    binding_version: int
    assigned_approver_user_id: str
    assigned_approval_card_id: str


@dataclass(frozen=True, slots=True)
class ApprovalDispositionInboxCommand:
    approval_item_id: str
    identity_session_id: str
    expected_org_id: str
    expected_actor_id: str
    kind: ApprovalDispositionKind
    expected_approval_item_revision: int
    expected_request_revision: int
    idempotency_key: str
    edited_text: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalDispositionInboxResult:
    receipt_id: str
    approval_item_id: str
    approval_item_revision: int
    request_id: str
    request_revision: int
    state: Literal["approved", "rejected"]
    replayed: bool


@dataclass(frozen=True, slots=True)
class ApprovalReassignmentCommand:
    approval_item_id: str
    identity_session_id: str
    expected_org_id: str
    expected_actor_id: str
    target_approver_user_id: str
    target_approval_card_id: str
    expected_approval_item_revision: int
    expected_request_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ApprovalReassignmentResult:
    receipt_id: str
    superseded_approval_item_id: str
    successor_approval_item_id: str
    successor_approval_item_revision: int
    request_id: str
    request_revision: int
    state: Literal["open"]
    replayed: bool


@dataclass(frozen=True, slots=True)
class ApprovalReadProof:
    principal: AuthenticatedPrincipal
    session_grant: AuthorizationGrant
    action_grant: AuthorizationGrant


@dataclass(frozen=True, slots=True)
class ApprovalTargetAuthorization:
    target_approver_user_id: str
    target_approval_card_id: str
    target_card_revision: int
    target_card_digest: str
    policy_version: str
    policy_digest: str


class ApprovalInboxAuthority(Protocol):
    def authorize_read(
        self,
        command: ApprovalReadCommand,
        action: Literal["approval.list", "approval.read"],
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ApprovalReadProof: ...

    def issue_disposition_proof(
        self,
        principal: AuthenticatedPrincipal,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> ApprovalDispositionAuthorizationProof: ...

    def verify_disposition_proof(
        self,
        proof: ApprovalDispositionAuthorizationProof,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool: ...

    def issue_reassignment_proof(
        self,
        command: ApprovalReassignmentCommand,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> tuple[ApprovalReadProof, ApprovalTargetAuthorization]: ...

    def verify_reassignment_proof(
        self,
        proof: ApprovalReadProof,
        target: ApprovalTargetAuthorization,
        command: ApprovalReassignmentCommand,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool: ...

    def current_assignment_binding(
        self, assignment: sqlite3.Row, transaction: sqlite3.Connection
    ) -> bool: ...


def migrate_central_inbox_approval_schema(
    path: Path,
    *,
    fault_injector: Callable[[str], None] = lambda _point: None,
) -> None:
    """Forward-only v15→v16 migration; v16 marker is written last."""
    if not central_inbox_conflict_schema_ready(path):
        raise ApprovalInboxUnavailable()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        existing = _existing_owned(connection)
        if existing:
            if existing != set(_TABLES):
                raise ApprovalInboxUnavailable()
            try:
                _validate_catalog(connection)
                return
            except ApprovalInboxUnavailable:
                _repair_v15_approval_item_after_lifecycle_upgrade(
                    connection, fault_injector=fault_injector
                )
                _validate_catalog(connection)
                return
        items = tuple(connection.execute("SELECT * FROM central_question_approval_items"))
        current_bindings = {
            str(item["approval_item_id"]): _current_card_binding(
                connection,
                str(item["org_id"]),
                str(item["agent_id"]),
                str(item["owner_id"]),
            )
            for item in items
        }
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                CENTRAL_QUESTION_APPROVAL_ITEMS_V16_DDL.replace(
                    "central_question_approval_items",
                    "central_question_approval_items_v16",
                    1,
                )
            )
            connection.executemany(
                "INSERT INTO central_question_approval_items_v16"
                "(approval_item_id,request_id,ticket_id,org_id,owner_id,agent_id,route_json,"
                "attempt,candidate_json,candidate_digest,policy_digest,binding_version,status,"
                "revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tuple(item) for item in items),
            )
            connection.execute("DROP TABLE central_question_approval_items")
            connection.execute(
                "ALTER TABLE central_question_approval_items_v16 "
                "RENAME TO central_question_approval_items"
            )
            for name in (
                "central_inbox_approval_assignments",
                "central_inbox_approval_reassignment_receipts",
                "central_inbox_approval_reassignment_audits",
            ):
                connection.execute(_TABLES[name])
            for ddl in _INDEXES:
                connection.execute(ddl)
            for ddl in _TRIGGERS:
                connection.execute(ddl)
            fault_injector("v15-to-v16-after-schema")
            for item in items:
                _backfill_assignment(
                    connection,
                    item,
                    current_bindings[str(item["approval_item_id"])],
                )
            fault_injector("v15-to-v16-after-copy")
            connection.execute(_MARKER_DDL)
            fault_injector("v15-to-v16-before-marker")
            connection.execute(
                "INSERT INTO central_inbox_approval_component_schema(name,version) VALUES (?,?)",
                (_COMPONENT, _VERSION),
            )
            _validate_catalog(connection, require_foreign_keys=False)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        _validate_catalog(connection)
    except ApprovalInboxUnavailable:
        raise
    except Exception as error:
        raise ApprovalInboxUnavailable() from error
    finally:
        connection.close()


def central_inbox_approval_schema_ready(path: Path) -> bool:
    if not path.is_file() or not central_question_lifecycle_schema_ready(path):
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        _validate_catalog(connection)
        return True
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def validate_central_inbox_approval_connection(
    connection: sqlite3.Connection,
) -> None:
    """Validate the complete v16 Approval catalog in the caller's snapshot."""
    _validate_catalog(connection)


def _repair_v15_approval_item_after_lifecycle_upgrade(
    connection: sqlite3.Connection,
    *,
    fault_injector: Callable[[str], None],
) -> None:
    """Repair only the exact v15 base table under an otherwise exact v16 catalog."""
    marker = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT name,version FROM central_inbox_approval_component_schema"
        )
    )
    if (
        marker != ((_COMPONENT, _VERSION),)
        or _catalog_signature(connection) != _EXPECTED_CATALOG
        or _approval_item_sql(connection) != _EXPECTED_V15_APPROVAL_ITEM_SQL
    ):
        raise ApprovalInboxUnavailable()
    items = tuple(connection.execute("SELECT * FROM central_question_approval_items"))
    assigned_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT approval_item_id FROM central_inbox_approval_assignments"
        )
    }
    current_bindings = {
        str(item["approval_item_id"]): _current_card_binding(
            connection,
            str(item["org_id"]),
            str(item["agent_id"]),
            str(item["owner_id"]),
        )
        for item in items
        if str(item["approval_item_id"]) not in assigned_ids
    }
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            CENTRAL_QUESTION_APPROVAL_ITEMS_V16_DDL.replace(
                "central_question_approval_items",
                "central_question_approval_items_v16",
                1,
            )
        )
        connection.executemany(
            "INSERT INTO central_question_approval_items_v16"
            "(approval_item_id,request_id,ticket_id,org_id,owner_id,agent_id,"
            "route_json,attempt,candidate_json,candidate_digest,policy_digest,"
            "binding_version,status,revision,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tuple(item) for item in items),
        )
        connection.execute("DROP TABLE central_question_approval_items")
        connection.execute(
            "ALTER TABLE central_question_approval_items_v16 "
            "RENAME TO central_question_approval_items"
        )
        fault_injector("v15-repair-after-copy")
        for item in items:
            item_id = str(item["approval_item_id"])
            if item_id not in assigned_ids:
                _backfill_assignment(connection, item, current_bindings[item_id])
        _validate_catalog(connection, require_foreign_keys=False)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def insert_initial_approval_assignment(
    transaction: sqlite3.Connection,
    *,
    approval_item_id: str,
    request_id: str,
    ticket_id: str,
    org_id: str,
    assigned_approver_user_id: str,
    assigned_approval_card_id: str,
    assigned_card_revision: int,
    assigned_card_digest: str,
    assigned_at: datetime,
    due_at: datetime,
) -> None:
    """Attach a newly ingested ApprovalItem to an installed v16 inbox.

    Before v16 is installed this is deliberately a no-op.  Once the marker
    exists, the companion assignment is part of the lifecycle writer's same
    SQLite transaction and is therefore never published as an orphan.
    """
    if not transaction.in_transaction:
        raise ApprovalInboxUnavailable()
    marker_exists = transaction.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='central_inbox_approval_component_schema'"
    ).fetchone()
    if marker_exists is None:
        return
    marker = tuple(
        tuple(row)
        for row in transaction.execute(
            "SELECT name,version FROM central_inbox_approval_component_schema"
        )
    )
    if (
        marker != ((_COMPONENT, _VERSION),)
        or _existing_owned(transaction) != set(_TABLES)
        or not all(
            _valid_reference(value)
            for value in (
                approval_item_id,
                request_id,
                ticket_id,
                org_id,
                assigned_approver_user_id,
                assigned_approval_card_id,
            )
        )
        or assigned_card_revision < 1
        or _SHA256.fullmatch(assigned_card_digest) is None
        or assigned_at.tzinfo is None
        or due_at.tzinfo is None
        or due_at < assigned_at
    ):
        raise ApprovalInboxUnavailable()
    item = transaction.execute(
        "SELECT request_id,ticket_id,org_id,owner_id,agent_id,status,revision "
        "FROM central_question_approval_items WHERE approval_item_id=?",
        (approval_item_id,),
    ).fetchone()
    if item is None or tuple(item) != (
        request_id,
        ticket_id,
        org_id,
        assigned_approver_user_id,
        assigned_approval_card_id,
        "open",
        1,
    ):
        raise ApprovalInboxUnavailable()
    transaction.execute(
        "INSERT INTO central_inbox_approval_assignments"
        "(approval_item_id,request_id,ticket_id,org_id,approval_round,"
        "predecessor_approval_item_id,assigned_approver_user_id,"
        "assigned_approval_card_id,assigned_card_revision,assigned_card_digest,"
        "assigned_at,due_at) VALUES (?,?,?,?,1,NULL,?,?,?,?,?,?)",
        (
            approval_item_id,
            request_id,
            ticket_id,
            org_id,
            assigned_approver_user_id,
            assigned_approval_card_id,
            assigned_card_revision,
            assigned_card_digest,
            assigned_at.isoformat(),
            due_at.isoformat(),
        ),
    )


def _backfill_assignment(
    connection: sqlite3.Connection,
    item: sqlite3.Row,
    binding: tuple[int, str],
) -> None:
    request = read_lifecycle_request(connection, str(item["request_id"]))
    if request is None or request.org_id != item["org_id"]:
        raise ApprovalInboxUnavailable()
    if binding[0] != int(item["binding_version"]):
        raise ApprovalInboxUnavailable()
    assigned_at = _timestamp(str(item["created_at"]))
    if item["status"] == "open":
        if (
            not isinstance(request.state, AwaitingApproval)
            or request.state.draft_ref != item["approval_item_id"]
        ):
            raise ApprovalInboxUnavailable()
        due_at = request.state.handling.due_at
    else:
        receipt = connection.execute(
            "SELECT created_at FROM central_question_approval_disposition_receipts "
            "WHERE approval_item_id=?",
            (item["approval_item_id"],),
        ).fetchone()
        if receipt is None:
            raise ApprovalInboxUnavailable()
        due_at = _timestamp(str(receipt["created_at"]))
    connection.execute(
        "INSERT INTO central_inbox_approval_assignments"
        "(approval_item_id,request_id,ticket_id,org_id,approval_round,"
        "predecessor_approval_item_id,assigned_approver_user_id,"
        "assigned_approval_card_id,assigned_card_revision,assigned_card_digest,"
        "assigned_at,due_at) VALUES (?,?,?,?,1,NULL,?,?,?,?,?,?)",
        (
            item["approval_item_id"],
            item["request_id"],
            item["ticket_id"],
            item["org_id"],
            item["owner_id"],
            item["agent_id"],
            binding[0],
            binding[1],
            assigned_at.isoformat(),
            due_at.isoformat(),
        ),
    )


def _current_card_binding(
    connection: sqlite3.Connection, org_id: str, card_id: str, owner_id: str
) -> tuple[int, str]:
    from agent_org_network.sqlite_production_agent_cards import (
        validate_production_agent_card_rows,
    )

    try:
        validate_production_agent_card_rows(connection, org_id)
        row = connection.execute(
            "SELECT owner_id,revision,card_digest FROM production_agent_cards "
            "WHERE org_id=? AND agent_id=?",
            (org_id, card_id),
        ).fetchone()
    except Exception as error:
        raise ApprovalInboxUnavailable() from error
    if (
        row is None
        or row["owner_id"] != owner_id
        or int(row["revision"]) < 1
        or _SHA256.fullmatch(str(row["card_digest"])) is None
    ):
        raise _ApprovalBindingNotCurrent()
    return int(row["revision"]), str(row["card_digest"])


def _validate_catalog(
    connection: sqlite3.Connection, *, require_foreign_keys: bool = True
) -> None:
    if require_foreign_keys and connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ApprovalInboxUnavailable()
    if _existing_owned(connection) != set(_TABLES):
        raise ApprovalInboxUnavailable()
    if _catalog_signature(connection) != _EXPECTED_CATALOG:
        raise ApprovalInboxUnavailable()
    marker = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT name,version FROM central_inbox_approval_component_schema"
        )
    )
    if marker != ((_COMPONENT, _VERSION),) or connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall():
        raise ApprovalInboxUnavailable()
    if _normalized_sql(
        connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='central_question_approval_items'"
        ).fetchone()[0]
    ) != _EXPECTED_APPROVAL_ITEM_SQL:
        raise ApprovalInboxUnavailable()
    items = tuple(connection.execute("SELECT * FROM central_question_approval_items"))
    assignments = tuple(
        connection.execute("SELECT * FROM central_inbox_approval_assignments")
    )
    if {str(row["approval_item_id"]) for row in items} != {
        str(row["approval_item_id"]) for row in assignments
    }:
        raise ApprovalInboxUnavailable()
    item_by_id = {str(row["approval_item_id"]): row for row in items}
    assignment_by_id = {str(row["approval_item_id"]): row for row in assignments}
    for assignment in assignments:
        item = item_by_id[str(assignment["approval_item_id"])]
        request = read_lifecycle_request(connection, str(item["request_id"]))
        predecessor_id = assignment["predecessor_approval_item_id"]
        if (
            request is None
            or assignment["request_id"] != item["request_id"]
            or assignment["ticket_id"] != item["ticket_id"]
            or assignment["org_id"] != item["org_id"] != request.org_id
            or _timestamp(str(assignment["assigned_at"]))
            > _timestamp(str(assignment["due_at"]))
        ):
            raise ApprovalInboxUnavailable()
        if predecessor_id is None:
            if int(assignment["approval_round"]) != 1:
                raise ApprovalInboxUnavailable()
        else:
            predecessor = assignment_by_id.get(str(predecessor_id))
            if (
                predecessor is None
                or predecessor["request_id"] != assignment["request_id"]
                or predecessor["ticket_id"] != assignment["ticket_id"]
                or int(assignment["approval_round"])
                != int(predecessor["approval_round"]) + 1
            ):
                raise ApprovalInboxUnavailable()
            for field in (
                "request_id",
                "ticket_id",
                "org_id",
                "owner_id",
                "agent_id",
                "route_json",
                "attempt",
                "candidate_json",
                "candidate_digest",
                "policy_digest",
                "binding_version",
            ):
                if item[field] != item_by_id[str(predecessor_id)][field]:
                    raise ApprovalInboxUnavailable()
    for receipt in connection.execute(
        "SELECT * FROM central_inbox_approval_reassignment_receipts"
    ):
        _validate_reassignment_receipt(connection, receipt)
    for item in items:
        outgoing = int(
            connection.execute(
                "SELECT count(*) FROM central_inbox_approval_reassignment_receipts "
                "WHERE superseded_approval_item_id=?",
                (item["approval_item_id"],),
            ).fetchone()[0]
        )
        if (item["status"] == "superseded") != (outgoing == 1):
            raise ApprovalInboxUnavailable()
        if item["status"] == "open":
            request = read_lifecycle_request(connection, str(item["request_id"]))
            if (
                request is None
                or not isinstance(request.state, AwaitingApproval)
                or request.state.draft_ref != item["approval_item_id"]
            ):
                raise ApprovalInboxUnavailable()


def _validate_reassignment_receipt(
    connection: sqlite3.Connection, receipt: sqlite3.Row
) -> None:
    old = connection.execute(
        "SELECT * FROM central_question_approval_items WHERE approval_item_id=?",
        (receipt["superseded_approval_item_id"],),
    ).fetchone()
    successor = connection.execute(
        "SELECT * FROM central_question_approval_items WHERE approval_item_id=?",
        (receipt["successor_approval_item_id"],),
    ).fetchone()
    old_assignment = connection.execute(
        "SELECT * FROM central_inbox_approval_assignments WHERE approval_item_id=?",
        (receipt["superseded_approval_item_id"],),
    ).fetchone()
    successor_assignment = connection.execute(
        "SELECT * FROM central_inbox_approval_assignments WHERE approval_item_id=?",
        (receipt["successor_approval_item_id"],),
    ).fetchone()
    audit = connection.execute(
        "SELECT * FROM central_inbox_approval_reassignment_audits WHERE receipt_id=?",
        (receipt["receipt_id"],),
    ).fetchone()
    if (
        old is None
        or successor is None
        or old_assignment is None
        or successor_assignment is None
        or audit is None
        or old["status"] != "superseded"
        or int(old["revision"]) != int(receipt["resulting_superseded_revision"])
        or successor_assignment["predecessor_approval_item_id"]
        != old["approval_item_id"]
        or receipt["request_id"] != old["request_id"] != successor["request_id"]
        or receipt["org_id"] != old["org_id"] != successor["org_id"]
        or receipt["target_approver_user_id"]
        != successor_assignment["assigned_approver_user_id"]
        or receipt["target_approval_card_id"]
        != successor_assignment["assigned_approval_card_id"]
        or int(receipt["target_card_revision"])
        != int(successor_assignment["assigned_card_revision"])
        or receipt["target_card_digest"]
        != successor_assignment["assigned_card_digest"]
        or audit["org_id"] != receipt["org_id"]
        or audit["request_id"] != receipt["request_id"]
        or audit["superseded_approval_item_id"]
        != receipt["superseded_approval_item_id"]
        or audit["successor_approval_item_id"]
        != receipt["successor_approval_item_id"]
        or audit["actor_id"] != receipt["actor_id"]
        or audit["command_digest"] != receipt["command_digest"]
        or int(audit["resulting_request_revision"])
        != int(receipt["resulting_request_revision"])
        or int(receipt["resulting_superseded_revision"])
        != int(receipt["expected_approval_item_revision"]) + 1
        or int(receipt["resulting_successor_revision"]) != 1
        or int(receipt["resulting_request_revision"])
        != int(receipt["expected_request_revision"]) + 1
        or receipt["actor_id"]
        != old_assignment["assigned_approver_user_id"]
        or not _SHA256.fullmatch(str(receipt["identity_session_id"]))
        or not str(receipt["authority_policy_version"])
        or not _SHA256.fullmatch(str(receipt["authority_policy_digest"]))
        or audit["created_at"] != receipt["created_at"]
        or _reassignment_digest_from_receipt(receipt) != receipt["command_digest"]
    ):
        raise ApprovalInboxUnavailable()


def _existing_owned(connection: sqlite3.Connection) -> set[str]:
    placeholders = ",".join("?" for _ in _TABLES)
    return {
        str(row[0])
        for row in connection.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
            tuple(_TABLES),
        )
    }


def _catalog_signature(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE (type='table' AND name IN (?,?,?,?)) "
            "OR (type='trigger' AND tbl_name IN (?,?,?)) "
            "OR (type='index' AND name IN (?,?)) ORDER BY type,name",
            (
                *tuple(_TABLES),
                "central_inbox_approval_assignments",
                "central_inbox_approval_reassignment_receipts",
                "central_inbox_approval_reassignment_audits",
                "central_inbox_approval_open_by_assignee",
                "central_inbox_approval_lineage",
            ),
        )
    )
    tables = tuple(
        (
            name,
            tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info({name})")),
            tuple(
                tuple(row)
                for row in connection.execute(f"PRAGMA foreign_key_list({name})")
            ),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA index_list({name})")),
        )
        for name in sorted(_TABLES)
    )
    return objects, tables


def _expected_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE question_requests(request_id TEXT PRIMARY KEY NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE central_question_work_tickets(ticket_id TEXT UNIQUE)"
        )
        connection.execute(
            "CREATE TABLE central_question_approval_items(approval_item_id TEXT UNIQUE)"
        )
        for ddl in _TABLES.values():
            connection.execute(ddl)
        for ddl in _INDEXES:
            connection.execute(ddl)
        for ddl in _TRIGGERS:
            connection.execute(ddl)
        return _catalog_signature(connection)
    finally:
        connection.close()


_EXPECTED_CATALOG = _expected_catalog()


def _normalized_sql(value: object) -> str:
    return " ".join(str(value).split())


def _approval_item_sql(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='central_question_approval_items'"
    ).fetchone()
    if row is None:
        raise ApprovalInboxUnavailable()
    return _normalized_sql(row[0])


def _expected_approval_item_sql() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE question_requests(request_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE central_question_work_tickets(ticket_id TEXT UNIQUE)"
        )
        connection.execute(
            CENTRAL_QUESTION_APPROVAL_ITEMS_V16_DDL.replace(
                "central_question_approval_items",
                "central_question_approval_items_v16",
                1,
            )
        )
        connection.execute(
            "ALTER TABLE central_question_approval_items_v16 "
            "RENAME TO central_question_approval_items"
        )
        return _normalized_sql(
            connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE name='central_question_approval_items'"
            ).fetchone()[0]
        )
    finally:
        connection.close()


_EXPECTED_APPROVAL_ITEM_SQL = _expected_approval_item_sql()


def _expected_v15_approval_item_sql() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE question_requests(request_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE central_question_work_tickets(ticket_id TEXT UNIQUE)"
        )
        connection.execute(
            CENTRAL_QUESTION_APPROVAL_ITEMS_V16_DDL.replace(
                "request_id TEXT NOT NULL,",
                "request_id TEXT NOT NULL UNIQUE,",
                1,
            )
            .replace(
                "ticket_id TEXT NOT NULL,",
                "ticket_id TEXT NOT NULL UNIQUE,",
                1,
            )
            .replace(
                "CHECK(status IN ('open','approved','rejected','superseded'))",
                "CHECK(status IN ('open','approved','rejected'))",
                1,
            )
        )
        return _approval_item_sql(connection)
    finally:
        connection.close()


_EXPECTED_V15_APPROVAL_ITEM_SQL = _expected_v15_approval_item_sql()


def _timestamp(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalInboxUnavailable()
    return value


def _valid_reference(value: object) -> bool:
    return type(value) is str and _REFERENCE.fullmatch(value) is not None


def _reassignment_digest(command: ApprovalReassignmentCommand) -> str:
    return sha256(
        canonical_lifecycle_json(
            {
                "approval_item_id": command.approval_item_id,
                "actor_id": command.expected_actor_id,
                "target_approver_user_id": command.target_approver_user_id,
                "target_approval_card_id": command.target_approval_card_id,
                "expected_approval_item_revision": command.expected_approval_item_revision,
                "expected_request_revision": command.expected_request_revision,
                "idempotency_key": command.idempotency_key,
            }
        ).encode()
    ).hexdigest()


def _reassignment_digest_from_receipt(receipt: sqlite3.Row) -> str:
    return sha256(
        canonical_lifecycle_json(
            {
                "approval_item_id": receipt["superseded_approval_item_id"],
                "actor_id": receipt["actor_id"],
                "target_approver_user_id": receipt["target_approver_user_id"],
                "target_approval_card_id": receipt["target_approval_card_id"],
                "expected_approval_item_revision": receipt[
                    "expected_approval_item_revision"
                ],
                "expected_request_revision": receipt["expected_request_revision"],
                "idempotency_key": receipt["idempotency_key"],
            }
        ).encode()
    ).hexdigest()


class ApprovalInboxApplication:
    def __init__(
        self,
        *,
        database_path: Path,
        authority: ApprovalInboxAuthority,
        read_snapshot_hook: Callable[[str], None] = lambda _point: None,
    ) -> None:
        self._path = database_path
        self._authority = authority
        self._read_snapshot_hook = read_snapshot_hook

    def list(self, command: ApprovalReadCommand) -> tuple[ApprovalItemSummary, ...]:
        _validate_read_command(command)
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            self._read_snapshot_hook("list-before-snapshot-validation")
            _validate_read_snapshot(connection)
            scope = ResourceRef(
                org_id=command.expected_org_id,
                kind="approval_inbox",
                resource_id=command.expected_actor_id,
                owner_subject_id=command.expected_actor_id,
            )
            proof = self._authority.authorize_read(
                command, "approval.list", scope, connection
            )
            _require_read_proof(proof, command, "approval.list", scope)
            rows = tuple(
                connection.execute(
                    "SELECT i.*,a.* FROM central_question_approval_items i "
                    "JOIN central_inbox_approval_assignments a USING(approval_item_id) "
                    "WHERE i.status='open' AND a.org_id=? "
                    "AND a.assigned_approver_user_id=? ORDER BY a.assigned_at,i.approval_item_id",
                    (proof.principal.org_id, proof.principal.subject_id),
                )
            )
            projected: list[ApprovalItemSummary] = []
            for row in rows:
                if not self._authority.current_assignment_binding(row, connection):
                    continue
                request = read_lifecycle_request(connection, str(row["request_id"]))
                if (
                    request is None
                    or not isinstance(request.state, AwaitingApproval)
                    or request.state.draft_ref != str(row["approval_item_id"])
                ):
                    raise ApprovalInboxUnavailable()
                projected.append(_summary(row, request))
            result = tuple(projected)
            connection.commit()
            return result
        except (ApprovalInboxUnavailable, ApprovalInboxNotFound):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ApprovalInboxUnavailable() from error
        finally:
            connection.close()

    def detail(
        self, command: ApprovalReadCommand, approval_item_id: str
    ) -> ApprovalItemDetail | None:
        _validate_read_command(command)
        if not _valid_reference(approval_item_id):
            return None
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            self._read_snapshot_hook("detail-before-snapshot-validation")
            _validate_read_snapshot(connection)
            row = _approval_row(connection, approval_item_id)
            if (
                row is None
                or row["status"] != "open"
                or row["org_id"] != command.expected_org_id
                or row["assigned_approver_user_id"] != command.expected_actor_id
            ):
                connection.commit()
                return None
            resource = ResourceRef(
                org_id=command.expected_org_id,
                kind="approval_item",
                resource_id=approval_item_id,
                owner_subject_id=command.expected_actor_id,
            )
            proof = self._authority.authorize_read(
                command, "approval.read", resource, connection
            )
            _require_read_proof(proof, command, "approval.read", resource)
            if not self._authority.current_assignment_binding(row, connection):
                connection.commit()
                return None
            request = read_lifecycle_request(connection, str(row["request_id"]))
            candidate = _candidate(str(row["candidate_json"]))
            if (
                request is None
                or not isinstance(request.state, AwaitingApproval)
                or request.state.draft_ref != approval_item_id
            ):
                raise ApprovalInboxUnavailable()
            summary = _summary(row, request)
            result = ApprovalItemDetail(
                approval_item_id=summary.approval_item_id,
                request_id=summary.request_id,
                request_revision=summary.request_revision,
                approval_round=summary.approval_round,
                revision=summary.revision,
                assigned_at=summary.assigned_at,
                due_at=summary.due_at,
                state=summary.state,
                question=request.question,
                candidate_text=candidate,
                candidate_digest=str(row["candidate_digest"]),
                policy_digest=str(row["policy_digest"]),
                binding_version=int(row["binding_version"]),
                assigned_approver_user_id=str(row["assigned_approver_user_id"]),
                assigned_approval_card_id=str(row["assigned_approval_card_id"]),
            )
            connection.commit()
            return result
        except (ApprovalInboxUnavailable, ApprovalInboxNotFound):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ApprovalInboxUnavailable() from error
        finally:
            connection.close()

    def _connection(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"file:{self._path}?mode=rw",
                uri=True,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA query_only=ON")
            return connection
        except Exception as error:
            if connection is not None:
                connection.close()
            raise ApprovalInboxUnavailable() from error


def _validate_read_snapshot(connection: sqlite3.Connection) -> None:
    try:
        validate_central_question_lifecycle_connection(connection)
        _validate_catalog(connection)
    except ApprovalInboxUnavailable:
        raise
    except Exception as error:
        raise ApprovalInboxUnavailable() from error


def _summary(row: sqlite3.Row, request: QuestionRequest) -> ApprovalItemSummary:
    return ApprovalItemSummary(
        approval_item_id=str(row["approval_item_id"]),
        request_id=str(row["request_id"]),
        request_revision=request.revision,
        approval_round=int(row["approval_round"]),
        revision=int(row["revision"]),
        assigned_at=_timestamp(str(row["assigned_at"])),
        due_at=_timestamp(str(row["due_at"])),
        state=cast(ApprovalItemState, str(row["status"])),
    )


def _candidate(raw: str) -> str:
    try:
        value: object = json.loads(raw)
        if type(value) is not dict:
            raise ValueError
        mapping = cast(dict[object, object], value)
        text = mapping.get("text")
        if type(text) is not str:
            raise ValueError
        return text
    except Exception as error:
        raise ApprovalInboxUnavailable() from error


def _approval_row(
    connection: sqlite3.Connection, approval_item_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT i.*,a.approval_round,a.predecessor_approval_item_id,"
        "a.assigned_approver_user_id,a.assigned_approval_card_id,"
        "a.assigned_card_revision,a.assigned_card_digest,a.assigned_at,a.due_at "
        "FROM central_question_approval_items i "
        "JOIN central_inbox_approval_assignments a USING(approval_item_id) "
        "WHERE i.approval_item_id=?",
        (approval_item_id,),
    ).fetchone()


def _validate_read_command(command: ApprovalReadCommand) -> None:
    if (
        type(command) is not ApprovalReadCommand
        or _SHA256.fullmatch(command.identity_session_id) is None
        or not _valid_reference(command.expected_org_id)
        or not _valid_reference(command.expected_actor_id)
    ):
        raise ApprovalInboxUnavailable()


def _require_read_proof(
    proof: ApprovalReadProof,
    command: ApprovalReadCommand,
    action: Literal["approval.list", "approval.read", "approval.reassign"],
    resource: ResourceRef,
) -> None:
    session_resource = ResourceRef(
        org_id=command.expected_org_id,
        kind="browser_session",
        resource_id=command.identity_session_id,
        owner_subject_id=command.expected_actor_id,
    )
    if (
        type(proof) is not ApprovalReadProof
        or proof.principal.org_id != command.expected_org_id
        or proof.principal.subject_id != command.expected_actor_id
        or proof.principal.identity_session_id != command.identity_session_id
        or proof.session_grant.org_id != command.expected_org_id
        or proof.session_grant.subject_id != command.expected_actor_id
        or proof.session_grant.action != "session.read"
        or proof.session_grant.resource != session_resource
        or proof.action_grant.org_id != command.expected_org_id
        or proof.action_grant.subject_id != command.expected_actor_id
        or proof.action_grant.action != action
        or proof.action_grant.resource != resource
    ):
        raise ApprovalInboxUnavailable()


def _require_reassignment_proof(
    proof: ApprovalReadProof,
    target: ApprovalTargetAuthorization,
    command: ApprovalReassignmentCommand,
    request: QuestionRequest,
) -> None:
    resource = ResourceRef(
        org_id=request.org_id,
        kind="approval_item",
        resource_id=command.approval_item_id,
        owner_subject_id=command.expected_actor_id,
    )
    try:
        _require_read_proof(
            proof,
            ApprovalReadCommand(
                identity_session_id=command.identity_session_id,
                expected_org_id=command.expected_org_id,
                expected_actor_id=command.expected_actor_id,
            ),
            "approval.reassign",
            resource,
        )
    except Exception as error:
        raise ApprovalInboxUnavailable() from error
    if (
        type(target) is not ApprovalTargetAuthorization
        or target.target_approver_user_id != command.target_approver_user_id
        or target.target_approval_card_id != command.target_approval_card_id
        or target.target_card_revision < 1
        or _SHA256.fullmatch(target.target_card_digest) is None
        or not target.policy_version
        or _SHA256.fullmatch(target.policy_digest) is None
        or target.policy_version != proof.action_grant.policy_version
        or target.policy_digest != proof.action_grant.policy_digest
    ):
        raise ApprovalInboxUnavailable()


class ApprovalDispositionInboxApplication:
    def __init__(
        self,
        *,
        database_path: Path,
        disposition: ApprovalDispositionApplication,
    ) -> None:
        self._path = database_path
        self._disposition = disposition

    def dispose(
        self, command: ApprovalDispositionInboxCommand
    ) -> ApprovalDispositionInboxResult:
        _validate_disposition_command(command)
        row = self._read_open_item(command.approval_item_id)
        if row is None or row["org_id"] != command.expected_org_id:
            raise ApprovalInboxNotFound()
        principal = AuthenticatedPrincipal(
            org_id=command.expected_org_id,
            subject_id=command.expected_actor_id,
            identity_provider="browser-session",
            identity_session_id=command.identity_session_id,
        )
        edited = (
            command.edited_text
            if command.kind == "approve_with_edit"
            else command.reason_code if command.kind == "reject" else None
        )
        try:
            result = self._disposition.dispose(
                ApprovalDispositionCommand(
                    request_id=str(row["request_id"]),
                    approval_item_id=command.approval_item_id,
                    expected_approval_item_revision=command.expected_approval_item_revision,
                    expected_request_revision=command.expected_request_revision,
                    principal=principal,
                    decision=command.kind,
                    edited_text=edited,
                    idempotency_key=command.idempotency_key,
                )
            )
        except CentralQuestionLifecycleConflict as error:
            raise ApprovalInboxStaleOrConflict() from error
        except CentralQuestionLifecycleUnavailable as error:
            raise ApprovalInboxUnavailable() from error
        receipt = self._read_disposition_receipt(command, str(row["request_id"]))
        state: Literal["approved", "rejected"] = (
            "rejected" if command.kind == "reject" else "approved"
        )
        return ApprovalDispositionInboxResult(
            receipt_id=str(receipt["receipt_id"]),
            approval_item_id=command.approval_item_id,
            approval_item_revision=int(receipt["resolved_item_revision"]),
            request_id=str(row["request_id"]),
            request_revision=result.request.revision,
            state=state,
            replayed=result.replayed,
        )

    def _read_open_item(self, approval_item_id: str) -> sqlite3.Row | None:
        if not central_inbox_approval_schema_ready(self._path):
            raise ApprovalInboxUnavailable()
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            return _approval_row(connection, approval_item_id)
        finally:
            connection.close()

    def _read_disposition_receipt(
        self, command: ApprovalDispositionInboxCommand, request_id: str
    ) -> sqlite3.Row:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            receipt = connection.execute(
                "SELECT * FROM central_question_approval_disposition_receipts "
                "WHERE request_id=? AND approval_item_id=? AND actor_id=? "
                "AND idempotency_key=?",
                (
                    request_id,
                    command.approval_item_id,
                    command.expected_actor_id,
                    command.idempotency_key,
                ),
            ).fetchone()
            if receipt is None:
                raise ApprovalInboxUnavailable()
            return receipt
        finally:
            connection.close()


def _validate_disposition_command(command: ApprovalDispositionInboxCommand) -> None:
    if (
        type(command) is not ApprovalDispositionInboxCommand
        or not all(
            _valid_reference(value)
            for value in (
                command.approval_item_id,
                command.expected_org_id,
                command.expected_actor_id,
                command.idempotency_key,
            )
        )
        or _SHA256.fullmatch(command.identity_session_id) is None
        or command.expected_approval_item_revision < 1
        or command.expected_request_revision < 0
        or command.kind not in {"approve", "approve_with_edit", "reject"}
    ):
        raise ApprovalInboxUnavailable()
    if command.kind == "approve":
        valid = command.edited_text is None and command.reason_code is None
    elif command.kind == "approve_with_edit":
        valid = (
            type(command.edited_text) is str
            and command.edited_text != ""
            and command.reason_code is None
        )
    else:
        valid = (
            type(command.reason_code) is str
            and command.reason_code != ""
            and command.edited_text is None
        )
    if not valid:
        raise ApprovalInboxUnavailable()


class InboxBoundApprovalDispositionAuthority:
    """Same-UoW assignment guard around the existing sole disposition writer."""

    def __init__(self, authority: ApprovalInboxAuthority) -> None:
        self._authority = authority

    def issue_approval_disposition_proof(
        self,
        principal: AuthenticatedPrincipal,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> ApprovalDispositionAuthorizationProof:
        assignment = _approval_row(transaction, str(item["approval_item_id"]))
        if (
            assignment is None
            or assignment["assigned_approver_user_id"] != principal.subject_id
            or not self._authority.current_assignment_binding(assignment, transaction)
        ):
            raise CentralQuestionLifecycleUnavailable("approval assignment hidden")
        return self._authority.issue_disposition_proof(
            principal, request, item, transaction
        )

    def verify_approval_disposition_proof(
        self,
        proof: ApprovalDispositionAuthorizationProof,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        assignment = _approval_row(transaction, str(item["approval_item_id"]))
        return (
            assignment is not None
            and assignment["assigned_approver_user_id"]
            == proof.principal.subject_id
            and self._authority.current_assignment_binding(assignment, transaction)
            and self._authority.verify_disposition_proof(
                proof, request, item, transaction
            )
        )


class ApprovalReassignmentApplication:
    def __init__(
        self,
        *,
        database_path: Path,
        authority: ApprovalInboxAuthority,
        approval_item_id_factory: Callable[[], str] = lambda: uuid4().hex,
        receipt_id_factory: Callable[[], str] = lambda: uuid4().hex,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        deadline: Callable[[str, datetime], datetime] = lambda _org, at: at,
        fault_injector: Callable[[str], None] = lambda _point: None,
    ) -> None:
        self._path = database_path
        self._authority = authority
        self._item_id = approval_item_id_factory
        self._receipt_id = receipt_id_factory
        self._clock = clock
        self._deadline = deadline
        self._fault = fault_injector

    def reassign(
        self, command: ApprovalReassignmentCommand
    ) -> ApprovalReassignmentResult:
        _validate_reassignment_command(command)
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_catalog(connection)
            item = _approval_row(connection, command.approval_item_id)
            if item is None or item["org_id"] != command.expected_org_id:
                raise ApprovalInboxNotFound()
            if (
                item["assigned_approver_user_id"] != command.expected_actor_id
                or not self._authority.current_assignment_binding(item, connection)
            ):
                raise ApprovalInboxNotFound()
            digest = _reassignment_digest(command)
            receipt = connection.execute(
                "SELECT * FROM central_inbox_approval_reassignment_receipts "
                "WHERE org_id=? AND actor_id=? AND idempotency_key=?",
                (
                    command.expected_org_id,
                    command.expected_actor_id,
                    command.idempotency_key,
                ),
            ).fetchone()
            request = read_lifecycle_request(connection, str(item["request_id"]))
            if request is None:
                raise ApprovalInboxUnavailable()
            if receipt is not None:
                if (
                    receipt["command_digest"] != digest
                    or receipt["superseded_approval_item_id"]
                    != command.approval_item_id
                ):
                    raise ApprovalInboxStaleOrConflict()
                proof, target = self._authority.issue_reassignment_proof(
                    command, request, item, connection
                )
                _require_reassignment_proof(proof, target, command, request)
                if not self._authority.verify_reassignment_proof(
                    proof, target, command, request, item, connection
                ):
                    raise ApprovalInboxUnavailable()
                _validate_reassignment_receipt(connection, receipt)
                replay_authority = canonical_v19_file_authority(
                    source_policy_digest=str(
                        receipt["authority_policy_digest"]
                    ),
                    current_snapshot_digest=target.policy_digest,
                )
                append_committed_source_evidence_if_v19(
                    connection, org_id=str(receipt["org_id"]),
                    receipt_id=str(receipt["receipt_id"]),
                    command_digest=str(receipt["command_digest"]),
                    event_type="approval_changed",
                    action="approval.reassign",
                    resource=SafeResourceRef(
                        kind="approval_item",
                        resource_id=str(
                            receipt["successor_approval_item_id"]
                        ),
                    ),
                    change=ApprovalChange(
                        approval_item_id=str(
                            receipt["successor_approval_item_id"]
                        ),
                        request_id=str(receipt["request_id"]),
                    ),
                    actor_user_id=str(receipt["actor_id"]),
                    occurred_at=str(receipt["created_at"]),
                    policy_revision_id=replay_authority.policy_revision_id,
                    policy_epoch=replay_authority.policy_epoch,
                    policy_digest=replay_authority.policy_digest,
                    source=SourceReceiptProvenance(
                        kind="approval_reassignment",
                        receipt_key=str(receipt["receipt_id"]),
                        receipt_digest=source_receipt_digest(
                            connection, "approval_reassignment",
                            str(receipt["org_id"]),
                            str(receipt["receipt_id"]),
                        ),
                    ),
                )
                result = _result_from_reassignment_receipt(receipt, replayed=True)
                connection.commit()
                return result
            if (
                not isinstance(request.state, AwaitingApproval)
                or request.state.draft_ref != command.approval_item_id
                or item["status"] != "open"
                or int(item["revision"])
                != command.expected_approval_item_revision
                or request.revision != command.expected_request_revision
                or item["assigned_approver_user_id"]
                != command.expected_actor_id
                or command.target_approver_user_id
                == item["assigned_approver_user_id"]
                or command.target_approval_card_id
                == item["assigned_approval_card_id"]
            ):
                raise ApprovalInboxStaleOrConflict()
            proof, target = self._authority.issue_reassignment_proof(
                command, request, item, connection
            )
            _require_reassignment_proof(proof, target, command, request)
            successor_id = self._item_id()
            receipt_id = self._receipt_id()
            at = self._clock()
            due_at = self._deadline(request.org_id, at)
            if (
                not _valid_reference(successor_id)
                or not _valid_reference(receipt_id)
                or at.tzinfo is None
                or due_at.tzinfo is None
                or due_at < at
            ):
                raise ApprovalInboxUnavailable()
            successor = request.reassign_approval(
                previous_item_id=command.approval_item_id,
                successor_item_id=successor_id,
                due_at=due_at,
                clock=lambda: at,
            )
            changed = connection.execute(
                "UPDATE central_question_approval_items "
                "SET status='superseded',revision=revision+1 "
                "WHERE approval_item_id=? AND status='open' AND revision=?",
                (
                    command.approval_item_id,
                    command.expected_approval_item_revision,
                ),
            )
            if changed.rowcount != 1:
                raise ApprovalInboxStaleOrConflict()
            connection.execute(
                "INSERT INTO central_question_approval_items"
                "(approval_item_id,request_id,ticket_id,org_id,owner_id,agent_id,"
                "route_json,attempt,candidate_json,candidate_digest,policy_digest,"
                "binding_version,status,revision,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'open',1,?)",
                (
                    successor_id,
                    item["request_id"],
                    item["ticket_id"],
                    item["org_id"],
                    item["owner_id"],
                    item["agent_id"],
                    item["route_json"],
                    item["attempt"],
                    item["candidate_json"],
                    item["candidate_digest"],
                    item["policy_digest"],
                    item["binding_version"],
                    at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO central_inbox_approval_assignments"
                "(approval_item_id,request_id,ticket_id,org_id,approval_round,"
                "predecessor_approval_item_id,assigned_approver_user_id,"
                "assigned_approval_card_id,assigned_card_revision,"
                "assigned_card_digest,assigned_at,due_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    successor_id,
                    item["request_id"],
                    item["ticket_id"],
                    item["org_id"],
                    int(item["approval_round"]) + 1,
                    command.approval_item_id,
                    target.target_approver_user_id,
                    target.target_approval_card_id,
                    target.target_card_revision,
                    target.target_card_digest,
                    at.isoformat(),
                    due_at.isoformat(),
                ),
            )
            compare_and_set_lifecycle_request(connection, request, successor)
            self._fault("after-approval-reassignment-cas")
            audit_authority = canonical_v19_file_authority(
                source_policy_digest=target.policy_digest,
                current_snapshot_digest=target.policy_digest,
            )
            connection.execute(
                "INSERT INTO central_inbox_approval_reassignment_receipts"
                "(receipt_id,org_id,request_id,superseded_approval_item_id,"
                "successor_approval_item_id,actor_id,identity_session_id,"
                "idempotency_key,command_digest,expected_approval_item_revision,"
                "expected_request_revision,target_approver_user_id,"
                "target_approval_card_id,target_card_revision,target_card_digest,"
                "resulting_superseded_revision,resulting_successor_revision,"
                "resulting_request_revision,authority_policy_version,"
                "authority_policy_digest,authority_policy_revision_id,"
                "authority_policy_epoch,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    request.org_id,
                    request.request_id,
                    command.approval_item_id,
                    successor_id,
                    proof.principal.subject_id,
                    proof.principal.identity_session_id,
                    command.idempotency_key,
                    digest,
                    command.expected_approval_item_revision,
                    command.expected_request_revision,
                    target.target_approver_user_id,
                    target.target_approval_card_id,
                    target.target_card_revision,
                    target.target_card_digest,
                    command.expected_approval_item_revision + 1,
                    1,
                    successor.revision,
                    target.policy_version,
                    target.policy_digest,
                    audit_authority.policy_revision_id,
                    audit_authority.policy_epoch,
                    at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO central_inbox_approval_reassignment_audits"
                "(receipt_id,org_id,request_id,superseded_approval_item_id,"
                "successor_approval_item_id,actor_id,command_digest,"
                "resulting_request_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    request.org_id,
                    request.request_id,
                    command.approval_item_id,
                    successor_id,
                    proof.principal.subject_id,
                    digest,
                    successor.revision,
                    at.isoformat(),
                ),
            )
            append_committed_source_evidence_if_v19(
                connection, org_id=request.org_id, receipt_id=receipt_id,
                command_digest=digest, event_type="approval_changed",
                action="approval.reassign",
                resource=SafeResourceRef(kind="approval_item", resource_id=successor_id),
                change=ApprovalChange(approval_item_id=successor_id, request_id=request.request_id),
                actor_user_id=proof.principal.subject_id, occurred_at=at.isoformat(),
                policy_revision_id=audit_authority.policy_revision_id,
                policy_epoch=audit_authority.policy_epoch,
                policy_digest=audit_authority.policy_digest,
                source=SourceReceiptProvenance(
                    kind="approval_reassignment",
                    receipt_key=receipt_id,
                    receipt_digest=source_receipt_digest(
                        connection,
                        "approval_reassignment",
                        request.org_id,
                        receipt_id,
                    ),
                ),
            )
            self._fault("before-approval-reassignment-commit")
            if (
                not self._authority.current_assignment_binding(item, connection)
                or not self._authority.verify_reassignment_proof(
                    proof, target, command, request, item, connection
                )
            ):
                raise ApprovalInboxUnavailable()
            _validate_catalog(connection)
            connection.commit()
            return ApprovalReassignmentResult(
                receipt_id=receipt_id,
                superseded_approval_item_id=command.approval_item_id,
                successor_approval_item_id=successor_id,
                successor_approval_item_revision=1,
                request_id=request.request_id,
                request_revision=successor.revision,
                state="open",
                replayed=False,
            )
        except (
            ApprovalInboxUnavailable,
            ApprovalInboxNotFound,
            ApprovalInboxStaleOrConflict,
        ):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ApprovalInboxUnavailable() from error
        finally:
            connection.close()


def _validate_reassignment_command(command: ApprovalReassignmentCommand) -> None:
    if (
        type(command) is not ApprovalReassignmentCommand
        or not all(
            _valid_reference(value)
            for value in (
                command.approval_item_id,
                command.expected_org_id,
                command.expected_actor_id,
                command.target_approver_user_id,
                command.target_approval_card_id,
                command.idempotency_key,
            )
        )
        or _SHA256.fullmatch(command.identity_session_id) is None
        or command.expected_approval_item_revision < 1
        or command.expected_request_revision < 0
    ):
        raise ApprovalInboxUnavailable()


def _result_from_reassignment_receipt(
    receipt: sqlite3.Row, *, replayed: bool
) -> ApprovalReassignmentResult:
    return ApprovalReassignmentResult(
        receipt_id=str(receipt["receipt_id"]),
        superseded_approval_item_id=str(
            receipt["superseded_approval_item_id"]
        ),
        successor_approval_item_id=str(receipt["successor_approval_item_id"]),
        successor_approval_item_revision=int(
            receipt["resulting_successor_revision"]
        ),
        request_id=str(receipt["request_id"]),
        request_revision=int(receipt["resulting_request_revision"]),
        state="open",
        replayed=replayed,
    )


class FileReloadingApprovalInboxAuthority:
    """Production browser-session/Card/Registry/reloaded-Authority adapter."""

    def __init__(
        self,
        *,
        authority_policy_path: Path,
        configured_org_id: str,
        disposition_authority: ApprovalDispositionAuthority,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._path = authority_policy_path
        self._org_id = configured_org_id
        self._disposition = disposition_authority
        self._clock = clock

    def authorize_read(
        self,
        command: ApprovalReadCommand,
        action: Literal["approval.list", "approval.read"],
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ApprovalReadProof:
        principal = self._principal(command, transaction)
        authorizer = self._authorizer()
        session_resource = ResourceRef(
            org_id=principal.org_id,
            kind="browser_session",
            resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        session = authorizer.authorize(principal, "session.read", session_resource)
        grant = authorizer.authorize(principal, action, resource)
        if (
            type(session) is not AuthorizationGrant
            or type(grant) is not AuthorizationGrant
            or not authorizer.verify(
                session, principal, "session.read", session_resource
            )
            or not authorizer.verify(grant, principal, action, resource)
        ):
            raise ApprovalInboxNotFound()
        return ApprovalReadProof(principal, session, grant)

    def issue_disposition_proof(
        self,
        principal: AuthenticatedPrincipal,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> ApprovalDispositionAuthorizationProof:
        return self._disposition.issue_approval_disposition_proof(
            principal, request, item, transaction
        )

    def verify_disposition_proof(
        self,
        proof: ApprovalDispositionAuthorizationProof,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        return self._disposition.verify_approval_disposition_proof(
            proof, request, item, transaction
        )

    def issue_reassignment_proof(
        self,
        command: ApprovalReassignmentCommand,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> tuple[ApprovalReadProof, ApprovalTargetAuthorization]:
        principal = self._principal(
            ApprovalReadCommand(
                command.identity_session_id,
                command.expected_org_id,
                command.expected_actor_id,
            ),
            transaction,
        )
        resource = ResourceRef(
            org_id=request.org_id,
            kind="approval_item",
            resource_id=command.approval_item_id,
            owner_subject_id=principal.subject_id,
        )
        authorizer = self._authorizer()
        session_resource = ResourceRef(
            org_id=principal.org_id,
            kind="browser_session",
            resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        session = authorizer.authorize(principal, "session.read", session_resource)
        reassign = authorizer.authorize(principal, "approval.reassign", resource)
        if (
            type(session) is not AuthorizationGrant
            or type(reassign) is not AuthorizationGrant
            or not authorizer.verify(
                session, principal, "session.read", session_resource
            )
            or not authorizer.verify(
                reassign, principal, "approval.reassign", resource
            )
        ):
            raise ApprovalInboxNotFound()
        try:
            revision, digest = _current_card_binding(
                transaction,
                request.org_id,
                command.target_approval_card_id,
                command.target_approver_user_id,
            )
        except _ApprovalBindingNotCurrent as error:
            raise ApprovalInboxNotFound() from error
        target_principal = AuthenticatedPrincipal(
            org_id=request.org_id,
            subject_id=command.target_approver_user_id,
            identity_provider="approval-reassignment-target",
            identity_session_id=sha256(
                f"{request.org_id}:{command.target_approver_user_id}".encode()
            ).hexdigest(),
        )
        target_resource = ResourceRef(
            org_id=request.org_id,
            kind="approval_item",
            resource_id=command.approval_item_id,
            owner_subject_id=command.target_approver_user_id,
        )
        decide = authorizer.authorize(
            target_principal, "approval.decide", target_resource
        )
        if type(decide) is not AuthorizationGrant or not authorizer.verify(
            decide, target_principal, "approval.decide", target_resource
        ):
            raise ApprovalInboxNotFound()
        return (
            ApprovalReadProof(principal, session, reassign),
            ApprovalTargetAuthorization(
                command.target_approver_user_id,
                command.target_approval_card_id,
                revision,
                digest,
                decide.policy_version,
                decide.policy_digest,
            ),
        )

    def verify_reassignment_proof(
        self,
        proof: ApprovalReadProof,
        target: ApprovalTargetAuthorization,
        command: ApprovalReassignmentCommand,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        try:
            current_proof, current_target = self.issue_reassignment_proof(
                command, request, item, transaction
            )
            return current_proof == proof and current_target == target
        except Exception:
            return False

    def current_assignment_binding(
        self, assignment: sqlite3.Row, transaction: sqlite3.Connection
    ) -> bool:
        try:
            revision, digest = _current_card_binding(
                transaction,
                str(assignment["org_id"]),
                str(assignment["assigned_approval_card_id"]),
                str(assignment["assigned_approver_user_id"]),
            )
            return revision == int(
                assignment["assigned_card_revision"]
            ) and digest == assignment["assigned_card_digest"]
        except _ApprovalBindingNotCurrent:
            return False

    def _principal(
        self, command: ApprovalReadCommand, transaction: sqlite3.Connection
    ) -> AuthenticatedPrincipal:
        if command.expected_org_id != self._org_id:
            raise ApprovalInboxNotFound()
        from agent_org_network.central_browser_auth_sqlite import (
            read_current_browser_session_connection,
        )

        try:
            session = read_current_browser_session_connection(
                transaction, command.identity_session_id, now=self._clock()
            )
        except Exception as error:
            raise ApprovalInboxUnavailable() from error
        if session is None:
            raise ApprovalSessionUnauthenticated()
        if (
            session.org_id != command.expected_org_id
            or session.registry_user_id != command.expected_actor_id
        ):
            raise ApprovalInboxNotFound()
        return AuthenticatedPrincipal(
            org_id=session.org_id,
            subject_id=session.registry_user_id,
            identity_provider="browser-session",
            identity_session_id=session.session_digest,
        )

    def _authorizer(self):
        from agent_org_network.central_authority import (
            SnapshotCentralAuthorizer,
            load_authority_policy_yaml,
        )

        try:
            return SnapshotCentralAuthorizer(
                load_authority_policy_yaml(
                    self._path.read_text(encoding="utf-8"),
                    expected_org_id=self._org_id,
                )
            )
        except Exception as error:
            raise ApprovalInboxUnavailable() from error


__all__ = [
    "ApprovalDispositionInboxApplication",
    "ApprovalDispositionInboxCommand",
    "ApprovalDispositionInboxResult",
    "ApprovalInboxApplication",
    "ApprovalInboxAuthority",
    "ApprovalInboxNotFound",
    "ApprovalInboxStaleOrConflict",
    "ApprovalInboxUnavailable",
    "ApprovalItemDetail",
    "ApprovalItemSummary",
    "ApprovalReadCommand",
    "ApprovalReadProof",
    "ApprovalReassignmentApplication",
    "ApprovalReassignmentCommand",
    "ApprovalReassignmentResult",
    "ApprovalSessionUnauthenticated",
    "ApprovalTargetAuthorization",
    "FileReloadingApprovalInboxAuthority",
    "InboxBoundApprovalDispositionAuthority",
    "central_inbox_approval_schema_ready",
    "insert_initial_approval_assignment",
    "migrate_central_inbox_approval_schema",
    "validate_central_inbox_approval_connection",
]
