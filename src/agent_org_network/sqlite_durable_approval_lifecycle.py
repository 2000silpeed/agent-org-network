"""P17.9 S3.2a durable Approval lifecycle schema capability.

The component is deliberately validate-only.  It owns no operational command and
does not upgrade v1 Approval rows, v2 assignments, or S3.1 decision receipts into
authority.  A later lifecycle UoW can only use an already-capable database.
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
from typing import Final, Protocol, TypeVar, cast

from agent_org_network.sqlite_approval_assignments_v2 import (
    SqliteApprovalAssignmentsV2SchemaError,
    decode_approval_assignment_v2,
    validate_sqlite_approval_assignments_v2_connection,
)
from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalUnavailable,
    ReassignExpiredApproval,
)
from agent_org_network.question_request import AwaitingApproval, FailedRequest
from agent_org_network.sqlite_stores import _select_question_request_no_commit  # pyright: ignore[reportPrivateUsage]

SQLITE_DURABLE_APPROVAL_LIFECYCLE_COMPONENT_ID: Final = "durable_approval_lifecycle_v1"
SQLITE_DURABLE_APPROVAL_LIFECYCLE_SCHEMA_VERSION: Final = 1
SQLITE_DURABLE_APPROVAL_LIFECYCLE_MIGRATION_FAULT_POINTS: Final = (
    "after_receipts",
    "after_evidence",
    "after_results",
    "after_audit",
    "after_outbox",
    "before_manifest_insert",
    "after_manifest_insert",
)

type MigrationFaultInjector = Callable[[str], None]
_Transaction = TypeVar("_Transaction", contravariant=True)


class DatabaseClock(Protocol[_Transaction]):
    """One transaction-scoped, timezone-aware database instant.

    The port deliberately accepts the transaction, rather than a bare ``now()``
    callback, so expiry evaluation cannot accidentally compare multiple wall-clock
    reads in one lifecycle command.  The SQLite schema capability does not provide
    an implementation; its adapter is part of the later lifecycle UoW.
    """

    def now(self, transaction: _Transaction) -> datetime: ...


class SqliteDurableApprovalLifecycleSchemaError(RuntimeError):
    """Lifecycle component or its parent capability is not canonical."""


_MANIFEST_TABLE: Final = "schema_component_manifests"
_RECEIPTS: Final = "durable_approval_lifecycle_receipts"
_EVIDENCE: Final = "durable_approval_lifecycle_evidence"
_RESULTS: Final = "durable_approval_lifecycle_results"
_AUDIT: Final = "durable_approval_lifecycle_audit_intents"
_OUTBOX: Final = "durable_approval_lifecycle_outbox_intents"
_OWNED_TABLES: Final = (_RECEIPTS, _EVIDENCE, _RESULTS, _AUDIT, _OUTBOX)

_RECEIPTS_DDL: Final = """
CREATE TABLE durable_approval_lifecycle_receipts (
    receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    org_id TEXT NOT NULL COLLATE BINARY,
    predecessor_assignment_id TEXT NOT NULL UNIQUE COLLATE BINARY,
    request_id TEXT NOT NULL COLLATE BINARY,
    command_digest TEXT NOT NULL UNIQUE COLLATE BINARY,
    principal_id TEXT NOT NULL COLLATE BINARY,
    action TEXT NOT NULL COLLATE BINARY,
    expected_request_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (predecessor_assignment_id) REFERENCES durable_approval_assignments_v2(assignment_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_EVIDENCE_DDL: Final = """
CREATE TABLE durable_approval_lifecycle_evidence (
    receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    evidence_kind TEXT NOT NULL COLLATE BINARY,
    evidence_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL COLLATE BINARY,
    FOREIGN KEY (receipt_id) REFERENCES durable_approval_lifecycle_receipts(receipt_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_RESULTS_DDL: Final = """
CREATE TABLE durable_approval_lifecycle_results (
    receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    result_kind TEXT NOT NULL COLLATE BINARY,
    result_json TEXT NOT NULL,
    result_sha256 TEXT NOT NULL COLLATE BINARY,
    FOREIGN KEY (receipt_id) REFERENCES durable_approval_lifecycle_receipts(receipt_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_AUDIT_DDL: Final = """
CREATE TABLE durable_approval_lifecycle_audit_intents (
    receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    org_id TEXT NOT NULL COLLATE BINARY,
    action TEXT NOT NULL COLLATE BINARY,
    request_id TEXT NOT NULL COLLATE BINARY,
    predecessor_assignment_id TEXT NOT NULL COLLATE BINARY,
    command_digest TEXT NOT NULL COLLATE BINARY,
    created_at TEXT NOT NULL,
    FOREIGN KEY (receipt_id) REFERENCES durable_approval_lifecycle_receipts(receipt_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_OUTBOX_DDL: Final = """
CREATE TABLE durable_approval_lifecycle_outbox_intents (
    receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    org_id TEXT NOT NULL COLLATE BINARY,
    kind TEXT NOT NULL COLLATE BINARY,
    request_id TEXT NOT NULL COLLATE BINARY,
    predecessor_assignment_id TEXT NOT NULL COLLATE BINARY,
    command_digest TEXT NOT NULL COLLATE BINARY,
    created_at TEXT NOT NULL,
    FOREIGN KEY (receipt_id) REFERENCES durable_approval_lifecycle_receipts(receipt_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_CANONICAL_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{6})?[+-][0-9]{2}:[0-9]{2}\Z"
)
_SQLITE_INTEGER_MAX: Final = 2**63 - 1
_LIFECYCLE_ACTIONS: Final = frozenset({"approval.reassign", "approval.expire"})
_EVIDENCE_KINDS: Final = frozenset(
    {"manual_reassignment", "expiry_reassignment", "expiry_unavailable"}
)
_RESULT_KINDS: Final = frozenset({"reassigned", "unavailable"})
_OUTBOX_KINDS: Final = frozenset({"lifecycle_outbox"})
_FAILURE_CODES: Final = frozenset({"approval_unavailable"})
_BODY_BEARING_WORDS: Final = frozenset(
    {
        "answer",
        "body",
        "candidate",
        "content",
        "context",
        "draft",
        "edited",
        "prompt",
        "question",
        "text",
    }
)


def _nonblank(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name}은 nonblank string이어야 합니다."
        )
    return value


def _opaque_identifier(value: object, *, name: str) -> str:
    value = _nonblank(value, name=name)
    if _OPAQUE_IDENTIFIER_RE.fullmatch(value) is None:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name}은 canonical opaque identifier여야 합니다."
        )
    return value


def validate_lifecycle_opaque_identifier(value: object, *, name: str) -> str:
    """Public command boundary for lifecycle receipt and assignment identifiers."""
    return _opaque_identifier(value, name=name)


def _sha256(value: object, *, name: str) -> str:
    value = _nonblank(value, name=name)
    if _SHA256_RE.fullmatch(value) is None:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name}은 lowercase SHA-256이어야 합니다."
        )
    return value


def _action(value: object, *, name: str) -> str:
    value = _nonblank(value, name=name)
    if value not in _LIFECYCLE_ACTIONS:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name} action이 올바르지 않습니다."
        )
    return value


def _enum(value: object, *, name: str, allowed: frozenset[str]) -> str:
    value = _nonblank(value, name=name)
    if value not in allowed:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name} enum이 올바르지 않습니다."
        )
    return value


def _canonical_timestamp(value: object, *, name: str) -> str:
    value = _nonblank(value, name=name)
    if _CANONICAL_TIMESTAMP_RE.fullmatch(value) is None:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name}은 canonical timezone-aware ISO timestamp여야 합니다."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name} timestamp가 올바르지 않습니다."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name} timestamp가 canonical하지 않습니다."
        )
    return value


def validate_lifecycle_canonical_timestamp(value: object, *, name: str) -> str:
    """Public command boundary for timestamps that will enter sealed lifecycle rows."""
    return _canonical_timestamp(value, name=name)


def _sealed_body_free(value: object, *, location: str) -> None:
    """Reject body-shaped fields even when a row has a matching outer digest.

    Lifecycle snapshots are an evidence/result boundary, not a generic JSON bag.
    The exact-key validator below is the primary protection; this recursive pass
    additionally prevents a future nested extension from quietly carrying a raw
    question, draft, candidate, or edited text.
    """
    if isinstance(value, dict):
        object_value = cast(dict[object, object], value)
        for key, nested in object_value.items():
            if not isinstance(key, str):
                raise SqliteDurableApprovalLifecycleSchemaError(
                    f"lifecycle sealed {location} key type이 올바르지 않습니다."
                )
            words = {word for word in re.split(r"[^a-z0-9]+", key.casefold()) if word}
            if words & _BODY_BEARING_WORDS:
                raise SqliteDurableApprovalLifecycleSchemaError(
                    f"lifecycle sealed {location}에는 raw body-bearing key를 저장할 수 없습니다."
                )
            _sealed_body_free(nested, location=location)
        return
    if isinstance(value, (list, tuple)):
        sequence_value = cast(list[object] | tuple[object, ...], value)
        for nested in sequence_value:
            _sealed_body_free(nested, location=location)
        return
    if isinstance(value, str):
        # The contract has no free-form text fields.  Treat body-bearing words in
        # any nested value as corruption too, rather than trusting a matching hash.
        words = {word for word in re.split(r"[^a-z0-9]+", value.casefold()) if word}
        if words & _BODY_BEARING_WORDS:
            raise SqliteDurableApprovalLifecycleSchemaError(
                f"lifecycle sealed {location}에는 raw body-bearing value를 저장할 수 없습니다."
            )


def _exact_keys(payload: dict[str, object], expected: frozenset[str], *, name: str) -> None:
    if frozenset(payload) != expected:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name} key set이 canonical contract와 다릅니다."
        )


def _integer(value: object, *, name: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _SQLITE_INTEGER_MAX:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle sealed {name} type/range이 올바르지 않습니다."
        )
    return value


def _sealed_evidence(
    *, payload: dict[str, object], evidence_kind: object, receipt: sqlite3.Row
) -> tuple[str, str]:
    kind = _nonblank(evidence_kind, name="evidence_kind")
    common = frozenset(
        {
            "action",
            "authority_digest",
            "evidence_digest",
            "expected_request_revision",
            "org_id",
            "predecessor_assignment_id",
            "principal_id",
            "request_id",
        }
    )
    extras: dict[str, frozenset[str]] = {
        "manual_reassignment": frozenset(
            {
                "authority_version_digest",
                "due_at",
                "policy_digest",
                "target_approver_id",
                "target_requirement_digest",
            }
        ),
        "expiry_reassignment": frozenset({"database_time", "expiry_policy_digest"}),
        "expiry_unavailable": frozenset({"database_time", "expiry_policy_digest"}),
    }
    if kind not in _EVIDENCE_KINDS or kind not in extras:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "허용되지 않은 lifecycle evidence kind입니다."
        )
    _sealed_body_free(payload, location="evidence")
    _exact_keys(payload, common | extras[kind], name="evidence")
    for field in ("org_id", "request_id", "predecessor_assignment_id", "principal_id"):
        if _opaque_identifier(payload[field], name=f"evidence.{field}") != receipt[field]:
            raise SqliteDurableApprovalLifecycleSchemaError(
                "lifecycle sealed evidence identity mirror가 receipt와 다릅니다."
            )
    if _action(payload["action"], name="evidence.action") != receipt["action"]:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle sealed evidence identity mirror가 receipt와 다릅니다."
        )
    if (
        _integer(payload["expected_request_revision"], name="evidence.expected_request_revision")
        != receipt["expected_request_revision"]
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle sealed evidence revision mirror가 receipt와 다릅니다."
        )
    _sha256(payload["authority_digest"], name="evidence.authority_digest")
    evidence_digest = _sha256(payload["evidence_digest"], name="evidence.evidence_digest")
    if kind == "manual_reassignment":
        if receipt["action"] != "approval.reassign":
            raise SqliteDurableApprovalLifecycleSchemaError(
                "manual lifecycle action이 올바르지 않습니다."
            )
        _canonical_timestamp(payload["due_at"], name="evidence.due_at")
        _opaque_identifier(payload["target_approver_id"], name="evidence.target_approver_id")
        for field in (
            "authority_version_digest",
            "policy_digest",
            "target_requirement_digest",
        ):
            _sha256(payload[field], name=f"evidence.{field}")
    else:
        if receipt["action"] != "approval.expire":
            raise SqliteDurableApprovalLifecycleSchemaError(
                "expiry lifecycle action이 올바르지 않습니다."
            )
        _canonical_timestamp(payload["database_time"], name="evidence.database_time")
        _sha256(payload["expiry_policy_digest"], name="evidence.expiry_policy_digest")
    return kind, evidence_digest


def _sealed_result(
    *,
    payload: dict[str, object],
    result_kind: object,
    receipt: sqlite3.Row,
    evidence_kind: str,
    evidence_digest: str,
) -> None:
    kind = _nonblank(result_kind, name="result_kind")
    common = frozenset(
        {
            "action",
            "evidence_digest",
            "expected_request_revision",
            "org_id",
            "predecessor_assignment_id",
            "request_id",
        }
    )
    extras: dict[str, frozenset[str]] = {
        "reassigned": frozenset(
            {"successor_assignment_id", "successor_approval_round", "successor_awaiting_revision"}
        ),
        "unavailable": frozenset({"failure_code"}),
    }
    if kind not in _RESULT_KINDS or kind not in extras:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "허용되지 않은 lifecycle result kind입니다."
        )
    if (evidence_kind == "expiry_unavailable") != (kind == "unavailable"):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle evidence/result relation이 올바르지 않습니다."
        )
    _sealed_body_free(payload, location="result")
    _exact_keys(payload, common | extras[kind], name="result")
    for field in ("org_id", "request_id", "predecessor_assignment_id"):
        if _opaque_identifier(payload[field], name=f"result.{field}") != receipt[field]:
            raise SqliteDurableApprovalLifecycleSchemaError(
                "lifecycle sealed result identity mirror가 receipt와 다릅니다."
            )
    if _action(payload["action"], name="result.action") != receipt["action"]:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle sealed result identity mirror가 receipt와 다릅니다."
        )
    if (
        _integer(payload["expected_request_revision"], name="result.expected_request_revision")
        != receipt["expected_request_revision"]
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle sealed result revision mirror가 receipt와 다릅니다."
        )
    if _sha256(payload["evidence_digest"], name="result.evidence_digest") != evidence_digest:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle sealed result evidence digest가 sealed evidence와 다릅니다."
        )
    if kind == "reassigned":
        _opaque_identifier(
            payload["successor_assignment_id"], name="result.successor_assignment_id"
        )
        _integer(
            payload["successor_approval_round"], name="result.successor_approval_round", minimum=1
        )
        _integer(payload["successor_awaiting_revision"], name="result.successor_awaiting_revision")
    else:
        _enum(payload["failure_code"], name="result.failure_code", allowed=_FAILURE_CODES)


def _validate_reassigned_successor_lineage(
    connection: sqlite3.Connection,
    *,
    receipt: sqlite3.Row,
    result: dict[str, object],
    evidence_kind: str,
    evidence: dict[str, object],
) -> None:
    """Bind a sealed reassignment result to the real v2 generation lineage.

    A later reassignment may legitimately supersede the successor, so this does
    not require its status to remain ``open``.  Its immutable predecessor link,
    request/attempt scope and generation mirrors must nevertheless remain exact.
    """
    predecessor = connection.execute(
        "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id COLLATE BINARY=?",
        (receipt["predecessor_assignment_id"],),
    ).fetchone()
    successor = connection.execute(
        "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id COLLATE BINARY=?",
        (result["successor_assignment_id"],),
    ).fetchone()
    if predecessor is None or successor is None:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle reassigned successor lineage가 없습니다."
        )
    try:
        predecessor_item = decode_approval_assignment_v2(
            assignment_json=predecessor["assignment_json"],
            assignment_sha256=predecessor["assignment_sha256"],
            org_id=predecessor["org_id"],
            request_id=predecessor["request_id"],
        )
        successor_item = decode_approval_assignment_v2(
            assignment_json=successor["assignment_json"],
            assignment_sha256=successor["assignment_sha256"],
            org_id=successor["org_id"],
            request_id=successor["request_id"],
        )
    except Exception as error:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle reassigned immutable assignment snapshot이 손상됐습니다."
        ) from error
    if (
        receipt["expected_request_revision"] != predecessor["awaiting_revision"]
        or successor["org_id"] != predecessor["org_id"] != receipt["org_id"]
        or successor["request_id"] != predecessor["request_id"] != receipt["request_id"]
        or successor["attempt"] != predecessor["attempt"]
        or successor["supersedes_assignment_id"] != receipt["predecessor_assignment_id"]
        or successor["approval_round"] != result["successor_approval_round"]
        or successor["awaiting_revision"] != result["successor_awaiting_revision"]
        or successor["approval_round"] != predecessor["approval_round"] + 1
        or successor["awaiting_revision"] != predecessor["awaiting_revision"] + 1
        or successor_item.route != predecessor_item.route
        or successor_item.draft != predecessor_item.draft
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle reassigned successor generation lineage가 다릅니다."
        )
    request = _select_question_request_no_commit(connection, receipt["request_id"])
    # The direct generation must match exactly while it is still current. A later
    # successor/terminal command intentionally advances the Request and is checked
    # by its own durable receipt rather than invalidating this historical receipt.
    if request is None or request.revision <= receipt["expected_request_revision"]:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle reassignment Request revision이 historical receipt보다 뒤여야 합니다."
        )
    if request.revision == receipt["expected_request_revision"] + 1 and (
        not isinstance(request.state, AwaitingApproval)
        or request.state.draft_ref != successor_item.item_id
        or request.state.route != successor_item.route
        or request.state.attempt != successor_item.attempt
        or request.state.handling.due_at != successor_item.due_at
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle reassignment Request postcondition이 다릅니다."
        )
    if receipt["action"] == "approval.reassign":
        supersession = predecessor_item.supersession
        if (
            evidence_kind != "manual_reassignment"
            or predecessor_item.status != "superseded"
            or supersession is None
            or supersession.reason != "reassigned"
            or supersession.successor_item_id != successor_item.item_id
            or supersession.actor_id != receipt["principal_id"]
            or supersession.target_approver_id != evidence["target_approver_id"]
            or supersession.policy_version is None
            or supersession.authority_version is None
            or supersession.evidence_ref is None
            or supersession.superseded_at.isoformat() != receipt["created_at"]
            or successor_item.created_at != supersession.superseded_at
            or successor_item.due_at.isoformat() != evidence["due_at"]
            or _digest(supersession.policy_version) != evidence["policy_digest"]
            or _digest(supersession.authority_version) != evidence["authority_version_digest"]
            or _digest(supersession.evidence_ref) != evidence["evidence_digest"]
            or _digest(_canonical_json(successor_item.requirement.model_dump(mode="json")))
            != evidence["target_requirement_digest"]
        ):
            raise SqliteDurableApprovalLifecycleSchemaError(
                "manual reassignment domain evidence가 sealed receipt와 다릅니다."
            )
        return
    if receipt["action"] != "approval.expire":
        return
    supersession = predecessor_item.supersession
    if (
        evidence_kind != "expiry_reassignment"
        or predecessor_item.status != "superseded"
        or supersession is None
        or supersession.reason != "expired"
        or supersession.actor_id is not None
        or supersession.successor_item_id != successor_item.item_id
        or supersession.target_approver_id != successor_item.requirement.approver_id
        or supersession.policy_version is None
        or supersession.authority_version is None
        or supersession.evidence_ref is None
        or supersession.superseded_at.isoformat() != evidence["database_time"]
        or receipt["created_at"] != evidence["database_time"]
        or successor_item.created_at != supersession.superseded_at
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "expiry reassignment domain evidence가 sealed receipt와 다릅니다."
        )
    try:
        decision = ReassignExpiredApproval(
            assignment_generation=ApprovalAssignmentGeneration.from_item(predecessor_item),
            requirement=successor_item.requirement,
            due_at=successor_item.due_at,
            policy_version=supersession.policy_version,
            authority_version=supersession.authority_version,
            evidence_ref=supersession.evidence_ref,
        )
    except Exception as error:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "expiry reassignment policy snapshot을 재구성할 수 없습니다."
        ) from error
    if (
        _digest(_canonical_json(decision.model_dump(mode="json")))
        != evidence["expiry_policy_digest"]
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "expiry reassignment policy digest가 domain snapshot과 다릅니다."
        )


def _validate_expiry_unavailable_domain_semantics(
    connection: sqlite3.Connection,
    *,
    receipt: sqlite3.Row,
    evidence_kind: str,
    evidence: dict[str, object],
) -> None:
    predecessor = connection.execute(
        "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id COLLATE BINARY=?",
        (receipt["predecessor_assignment_id"],),
    ).fetchone()
    if predecessor is None:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "expiry unavailable predecessor가 없습니다."
        )
    try:
        item = decode_approval_assignment_v2(
            assignment_json=predecessor["assignment_json"],
            assignment_sha256=predecessor["assignment_sha256"],
            org_id=predecessor["org_id"],
            request_id=predecessor["request_id"],
        )
    except Exception as error:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "expiry unavailable immutable assignment snapshot이 손상됐습니다."
        ) from error
    unavailability = item.unavailability
    if (
        evidence_kind != "expiry_unavailable"
        or item.status != "unavailable"
        or unavailability is None
        or unavailability.unavailable_at.isoformat() != evidence["database_time"]
        or receipt["created_at"] != evidence["database_time"]
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "expiry unavailable domain evidence가 sealed receipt와 다릅니다."
        )
    decision: ApprovalUnavailable = unavailability.decision
    if (
        _digest(_canonical_json(decision.model_dump(mode="json")))
        != evidence["expiry_policy_digest"]
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "expiry unavailable policy digest가 domain snapshot과 다릅니다."
        )
    request = _select_question_request_no_commit(connection, receipt["request_id"])
    if (
        request is None
        or request.revision != receipt["expected_request_revision"] + 1
        or not isinstance(request.state, FailedRequest)
        or request.state.error_code != "approval_unavailable"
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "expiry unavailable Request postcondition이 다릅니다."
        )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _tokens(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise SqliteDurableApprovalLifecycleSchemaError("lifecycle SQLite DDL을 읽을 수 없습니다.")
    return " ".join(raw.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table in _OWNED_TABLES:
        row = connection.execute(
            "SELECT sql FROM main.sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise SqliteDurableApprovalLifecycleSchemaError("lifecycle canonical table이 없습니다.")
        tables.append(
            {
                "name": table,
                "ddl": _tokens(row[0]),
                "columns": [
                    (
                        str(column["name"]),
                        str(column["type"]),
                        bool(column["notnull"]),
                        int(column["pk"]),
                    )
                    for column in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
                ],
                "foreign_keys": [
                    tuple(foreign_key)
                    for foreign_key in connection.execute(
                        f'PRAGMA foreign_key_list("{table}")'
                    ).fetchall()
                ],
            }
        )
    return {
        "component_id": SQLITE_DURABLE_APPROVAL_LIFECYCLE_COMPONENT_ID,
        "component_schema_version": SQLITE_DURABLE_APPROVAL_LIFECYCLE_SCHEMA_VERSION,
        "tables": tables,
    }


@lru_cache(maxsize=1)
def _expected_manifest_json() -> str:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        connection.execute(
            "CREATE TABLE durable_approval_assignments_v2 (assignment_id TEXT PRIMARY KEY NOT NULL)"
        )
        for ddl in (_RECEIPTS_DDL, _EVIDENCE_DDL, _RESULTS_DDL, _AUDIT_DDL, _OUTBOX_DDL):
            connection.execute(ddl)
        return _canonical_json(_catalog(connection))
    finally:
        connection.close()


def _manifest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _table_exists(connection, _MANIFEST_TABLE):
        raise SqliteDurableApprovalLifecycleSchemaError("공유 schema manifest table이 없습니다.")
    return connection.execute(
        "SELECT component_id, schema_version, manifest_json, manifest_sha256 "
        "FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
        (SQLITE_DURABLE_APPROVAL_LIFECYCLE_COMPONENT_ID,),
    ).fetchone()


def _strict_canonical_snapshot(
    raw_json: object, raw_digest: object, *, name: str
) -> dict[str, object]:
    if (
        not isinstance(raw_json, str)
        or not isinstance(raw_digest, str)
        or _digest(raw_json) != raw_digest
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle {name} digest가 올바르지 않습니다."
        )

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise SqliteDurableApprovalLifecycleSchemaError(
                    f"lifecycle {name} JSON에 중복 key가 있습니다."
                )
            decoded[key] = value
        return decoded

    try:
        value = json.loads(
            raw_json,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle {name} JSON이 올바르지 않습니다."
        ) from error
    if not isinstance(value, dict) or _canonical_json(cast(dict[str, object], value)) != raw_json:
        raise SqliteDurableApprovalLifecycleSchemaError(
            f"lifecycle {name} JSON은 canonical object여야 합니다."
        )
    return cast(dict[str, object], value)


def _reconcile_rows(connection: sqlite3.Connection, *, org_id: str | None = None) -> None:
    query = (
        "SELECT r.receipt_id, r.org_id, r.predecessor_assignment_id, r.request_id, r.command_digest, "
        "r.principal_id, r.action, r.expected_request_revision, r.created_at, "
        "e.evidence_kind, e.evidence_json, e.evidence_sha256, "
        "s.result_kind, s.result_json, s.result_sha256, "
        "a.org_id AS audit_org_id, a.action AS audit_action, a.request_id AS audit_request_id, "
        "a.predecessor_assignment_id AS audit_predecessor_id, a.command_digest AS audit_digest, "
        "a.created_at AS audit_created_at, o.org_id AS outbox_org_id, o.kind AS outbox_kind, "
        "o.request_id AS outbox_request_id, "
        "o.predecessor_assignment_id AS outbox_predecessor_id, o.command_digest AS outbox_digest "
        ", o.created_at AS outbox_created_at "
        "FROM durable_approval_lifecycle_receipts r "
        "LEFT JOIN durable_approval_lifecycle_evidence e ON e.receipt_id=r.receipt_id "
        "LEFT JOIN durable_approval_lifecycle_results s ON s.receipt_id=r.receipt_id "
        "LEFT JOIN durable_approval_lifecycle_audit_intents a ON a.receipt_id=r.receipt_id "
        "LEFT JOIN durable_approval_lifecycle_outbox_intents o ON o.receipt_id=r.receipt_id"
    )
    parameters: tuple[object, ...] = ()
    if org_id is not None:
        query += " WHERE r.org_id COLLATE BINARY=?"
        parameters = (org_id,)
    rows = connection.execute(query, parameters).fetchall()
    for row in rows:
        for field in (
            "receipt_id",
            "org_id",
            "predecessor_assignment_id",
            "request_id",
            "principal_id",
        ):
            _opaque_identifier(row[field], name=f"receipt.{field}")
        _sha256(row["command_digest"], name="receipt.command_digest")
        _action(row["action"], name="receipt.action")
        _integer(row["expected_request_revision"], name="receipt.expected_request_revision")
        _canonical_timestamp(row["created_at"], name="receipt.created_at")
        predecessor = connection.execute(
            "SELECT org_id, request_id FROM durable_approval_assignments_v2 WHERE assignment_id COLLATE BINARY=?",
            (row["predecessor_assignment_id"],),
        ).fetchone()
        parent = connection.execute(
            "SELECT org_id FROM question_requests WHERE request_id COLLATE BINARY=?",
            (row["request_id"],),
        ).fetchone()
        if (
            predecessor is None
            or parent is None
            or predecessor["org_id"] != row["org_id"]
            or predecessor["request_id"] != row["request_id"]
            or parent["org_id"] != row["org_id"]
        ):
            raise SqliteDurableApprovalLifecycleSchemaError(
                "lifecycle receipt의 current org/request lineage가 다릅니다."
            )
        if any(
            row[key] is None
            for key in ("evidence_kind", "result_kind", "audit_org_id", "outbox_org_id")
        ):
            raise SqliteDurableApprovalLifecycleSchemaError(
                "lifecycle receipt의 sealed companion row가 없습니다."
            )
        evidence = _strict_canonical_snapshot(
            row["evidence_json"], row["evidence_sha256"], name="evidence"
        )
        result = _strict_canonical_snapshot(row["result_json"], row["result_sha256"], name="result")
        evidence_kind, evidence_digest = _sealed_evidence(
            payload=evidence, evidence_kind=row["evidence_kind"], receipt=row
        )
        _sealed_result(
            payload=result,
            result_kind=row["result_kind"],
            receipt=row,
            evidence_kind=evidence_kind,
            evidence_digest=evidence_digest,
        )
        if row["result_kind"] == "reassigned":
            _validate_reassigned_successor_lineage(
                connection,
                receipt=row,
                result=result,
                evidence_kind=evidence_kind,
                evidence=evidence,
            )
        elif row["action"] == "approval.expire":
            _validate_expiry_unavailable_domain_semantics(
                connection,
                receipt=row,
                evidence_kind=evidence_kind,
                evidence=evidence,
            )
        if not (
            row["audit_org_id"] == row["org_id"] == row["outbox_org_id"]
            and row["audit_action"] == row["action"]
            and row["audit_request_id"] == row["request_id"] == row["outbox_request_id"]
            and row["audit_predecessor_id"]
            == row["predecessor_assignment_id"]
            == row["outbox_predecessor_id"]
            and row["audit_digest"] == row["command_digest"] == row["outbox_digest"]
            and row["audit_created_at"] == row["created_at"] == row["outbox_created_at"]
        ):
            raise SqliteDurableApprovalLifecycleSchemaError(
                "lifecycle intent mirror가 receipt와 다릅니다."
            )
        _canonical_timestamp(row["audit_created_at"], name="audit.created_at")
        _canonical_timestamp(row["outbox_created_at"], name="outbox.created_at")
        _enum(row["outbox_kind"], name="outbox.kind", allowed=_OUTBOX_KINDS)


def _validate(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SqliteDurableApprovalLifecycleSchemaError("SQLite foreign_keys=ON이 필요합니다.")
    try:
        validate_sqlite_approval_assignments_v2_connection(
            connection, org_id=org_id, reconcile_rows=reconcile_rows
        )
    except SqliteApprovalAssignmentsV2SchemaError as error:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle에는 capable v2 assignment parent가 필요합니다."
        ) from error
    manifest = _manifest(connection)
    expected = _expected_manifest_json()
    if (
        manifest is None
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != SQLITE_DURABLE_APPROVAL_LIFECYCLE_SCHEMA_VERSION
    ):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle manifest version이 올바르지 않습니다."
        )
    if manifest["manifest_json"] != expected or manifest["manifest_sha256"] != _digest(expected):
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle manifest digest가 canonical 기대값과 다릅니다."
        )
    if _canonical_json(_catalog(connection)) != expected:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle SQLite catalog가 canonical schema와 다릅니다."
        )
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle foreign_key_check가 실패했습니다."
        )
    if reconcile_rows:
        _reconcile_rows(connection, org_id=org_id)


def validate_sqlite_durable_approval_lifecycle_connection(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    finally:
        connection.row_factory = previous


def migrate_sqlite_durable_approval_lifecycle_schema(
    db_path: str | Path, *, fault_injector: MigrationFaultInjector | None = None
) -> None:
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_sqlite_approval_assignments_v2_connection(connection)
        except SqliteApprovalAssignmentsV2SchemaError as error:
            raise SqliteDurableApprovalLifecycleSchemaError(
                "lifecycle migration에는 capable v2 parent가 필요합니다."
            ) from error
        existing = _manifest(connection)
        if existing is not None:
            _validate(connection)
            connection.commit()
            return
        if any(_table_exists(connection, table) for table in _OWNED_TABLES):
            raise SqliteDurableApprovalLifecycleSchemaError(
                "manifest 없는 partial lifecycle schema는 복구하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SqliteDurableApprovalLifecycleSchemaError(
                "migration 전 foreign_key_check가 실패했습니다."
            )
        for ddl, point in zip(
            (_RECEIPTS_DDL, _EVIDENCE_DDL, _RESULTS_DDL, _AUDIT_DDL, _OUTBOX_DDL),
            SQLITE_DURABLE_APPROVAL_LIFECYCLE_MIGRATION_FAULT_POINTS[:5],
            strict=True,
        ):
            connection.execute(ddl)
            if fault_injector is not None:
                fault_injector(point)
        expected = _expected_manifest_json()
        if _canonical_json(_catalog(connection)) != expected:
            raise SqliteDurableApprovalLifecycleSchemaError(
                "migration 결과 lifecycle catalog가 canonical schema와 다릅니다."
            )
        if fault_injector is not None:
            fault_injector("before_manifest_insert")
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES (?,?,?,?)",
            (
                SQLITE_DURABLE_APPROVAL_LIFECYCLE_COMPONENT_ID,
                SQLITE_DURABLE_APPROVAL_LIFECYCLE_SCHEMA_VERSION,
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


def _open_existing(path: str | Path, *, readonly: bool) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle runtime은 기존 SQLite 파일 경로만 엽니다."
        )
    try:
        resolved = Path(raw).expanduser().resolve(strict=False)
        return sqlite3.connect(
            f"{resolved.as_uri()}?mode={'ro' if readonly else 'rw'}", uri=True, timeout=5.0
        )
    except sqlite3.Error as error:
        raise SqliteDurableApprovalLifecycleSchemaError(
            "lifecycle SQLite DB를 기존 파일로 열 수 없습니다."
        ) from error


def open_sqlite_durable_approval_lifecycle_connection(db_path: str | Path) -> sqlite3.Connection:
    connection = _open_existing(db_path, readonly=False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _validate(connection)
    except Exception:
        connection.close()
        raise
    return connection


@dataclass(frozen=True)
class DurableApprovalLifecycleSchemaReconciliationReport:
    capable: bool
    detail: str
    lifecycle_manifest_present: bool


def reconcile_sqlite_durable_approval_lifecycle_schema(
    db_path: str | Path,
) -> DurableApprovalLifecycleSchemaReconciliationReport:
    present = False
    try:
        connection = _open_existing(db_path, readonly=True)
    except SqliteDurableApprovalLifecycleSchemaError as error:
        return DurableApprovalLifecycleSchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _table_exists(connection, _MANIFEST_TABLE) and _manifest(connection) is not None
        _validate(connection)
        return DurableApprovalLifecycleSchemaReconciliationReport(True, "capable_v1", present)
    except Exception as error:
        return DurableApprovalLifecycleSchemaReconciliationReport(False, str(error), present)
    finally:
        connection.close()
