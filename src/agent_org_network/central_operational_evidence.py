"""RB3.2b.6-B durable, redacted Central operational evidence.

This is deliberately a *projection boundary*: a lifecycle/Inbox/Registry
transition remains the source of truth.  The source tables insert a small,
safe audit and an outbox intent through SQLite triggers in the same UoW; a
separate projector assigns the observable per-organization cursor.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


SCHEMA_VERSION: Final = 19
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_EVENT_TYPES = frozenset({
    "question_received", "request_state_changed", "answer_finalized",
    "feedback_recorded", "conflict_concurrence_recorded", "manager_item_changed",
    "approval_changed", "backup_review_changed", "reevaluation_changed",
    "registry_user_registered", "agent_card_registered", "policy_activated",
    "policy_rolled_back", "card_owner_transferred", "card_owner_revoked",
    "command_attempt_recorded",
})
_SAFE_CHANGE_FIELDS = frozenset({
    "from_state", "to_state", "reason_code", "request_id", "record_id",
    "feedback_id", "case_id", "manager_item_id", "approval_item_id", "review_id",
    "reevaluation_id", "card_id", "from_owner_user_id", "to_owner_user_id",
    "assignment_generation", "policy_revision_id", "policy_epoch", "policy_digest",
})


class OperationalEvidenceUnavailable(RuntimeError):
    """Catalog, source reconciliation, or immutable delivery evidence drifted."""


class OperationalEvidenceResyncRequired(OperationalEvidenceUnavailable):
    def __init__(self, oldest_available_cursor: int, latest_cursor: int) -> None:
        super().__init__("resync_required")
        self.oldest_available_cursor = oldest_available_cursor
        self.latest_cursor = latest_cursor


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SystemActor(_StrictFrozen):
    kind: Literal["system"] = "system"


class UserActor(_StrictFrozen):
    kind: Literal["user"] = "user"
    user_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class SafeResourceRef(_StrictFrozen):
    kind: Literal[
        "question_request", "answer_record", "question_feedback", "conflict_case",
        "manager_item", "approval_item", "backup_review", "reevaluation",
        "registry_user", "agent_card", "authority_policy", "receipt",
    ]
    resource_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


SafeReference: TypeAlias = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
]
RequestState: TypeAlias = Literal[
    "received", "awaiting_manager", "awaiting_conflict", "ready_to_dispatch",
    "awaiting_answer", "awaiting_approval", "answered", "declined",
]


class _SafeChange(_StrictFrozen):
    """Closed safe scalars only; free text and source locations cannot enter."""
    def model_post_init(self, __context: object) -> None:
        for key, value in self.__dict__.items():
            if key not in _SAFE_CHANGE_FIELDS | {"kind"}:
                raise ValueError("unsafe change field")
            if isinstance(value, str) and (not value or len(value) > 128):
                raise ValueError("unsafe change scalar")
            if isinstance(value, str) and (
                "://" in value or "/" in value or "@" in value
                or any(character.isspace() for character in value)
            ):
                raise ValueError("raw-like change scalar")


class QuestionChange(_SafeChange):
    kind: Literal["question"] = "question"
    request_id: SafeReference
    from_state: RequestState | None = None
    to_state: RequestState | None = None


class AnswerChange(_SafeChange):
    kind: Literal["answer"] = "answer"
    request_id: SafeReference
    record_id: SafeReference | None = None


class FeedbackChange(_SafeChange):
    kind: Literal["feedback"] = "feedback"
    request_id: SafeReference
    record_id: SafeReference
    feedback_id: SafeReference


class ConflictChange(_SafeChange):
    kind: Literal["conflict"] = "conflict"
    case_id: SafeReference
    request_id: SafeReference


class ManagerItemChange(_SafeChange):
    kind: Literal["manager_item"] = "manager_item"
    manager_item_id: SafeReference
    request_id: SafeReference


class ApprovalChange(_SafeChange):
    kind: Literal["approval"] = "approval"
    approval_item_id: SafeReference
    request_id: SafeReference


class BackupReviewChange(_SafeChange):
    kind: Literal["backup_review"] = "backup_review"
    review_id: SafeReference
    request_id: SafeReference


class ReevaluationChange(_SafeChange):
    kind: Literal["reevaluation"] = "reevaluation"
    reevaluation_id: SafeReference
    request_id: SafeReference


class RegistryChange(_SafeChange):
    kind: Literal["registry"] = "registry"


class AgentCardChange(_SafeChange):
    kind: Literal["agent_card"] = "agent_card"
    card_id: SafeReference


class PolicyChange(_SafeChange):
    kind: Literal["policy"] = "policy"
    policy_revision_id: SafeReference
    policy_epoch: Annotated[int, Field(gt=0)]
    policy_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CardOwnerChange(_SafeChange):
    kind: Literal["card_owner"] = "card_owner"
    card_id: SafeReference
    assignment_generation: Annotated[int, Field(gt=0)]
    from_owner_user_id: SafeReference | None = None
    to_owner_user_id: SafeReference | None = None
    reason_code: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")] | None = None


class CommandAttemptChange(_SafeChange):
    kind: Literal["command_attempt"] = "command_attempt"


_SafeChangeUnion: TypeAlias = (
    QuestionChange | AnswerChange | FeedbackChange | ConflictChange | ManagerItemChange
    | ApprovalChange | BackupReviewChange | ReevaluationChange | RegistryChange
    | AgentCardChange | PolicyChange | CardOwnerChange | CommandAttemptChange
)
SafeChange: TypeAlias = Annotated[_SafeChangeUnion, Field(discriminator="kind")]
_SAFE_CHANGE_ADAPTER: Final[TypeAdapter[_SafeChangeUnion]] = TypeAdapter[_SafeChangeUnion](SafeChange)
_EVENT_CONTRACTS: Final[dict[str, tuple[str, str, frozenset[str]]]] = {
    "question_received": ("question", "question_request", frozenset({"user"})),
    "request_state_changed": ("question", "question_request", frozenset({"system", "user"})),
    "answer_finalized": ("answer", "question_request", frozenset({"system"})),
    "feedback_recorded": ("feedback", "question_request", frozenset({"user"})),
    "conflict_concurrence_recorded": ("conflict", "conflict_case", frozenset({"user"})),
    "manager_item_changed": ("manager_item", "manager_item", frozenset({"system", "user"})),
    "approval_changed": ("approval", "approval_item", frozenset({"system", "user"})),
    "backup_review_changed": ("backup_review", "backup_review", frozenset({"system", "user"})),
    "reevaluation_changed": ("reevaluation", "reevaluation", frozenset({"system", "user"})),
    "registry_user_registered": ("registry", "registry_user", frozenset({"user"})),
    "agent_card_registered": ("agent_card", "agent_card", frozenset({"user"})),
    "policy_activated": ("policy", "authority_policy", frozenset({"user"})),
    "policy_rolled_back": ("policy", "authority_policy", frozenset({"user"})),
    "card_owner_transferred": ("card_owner", "agent_card", frozenset({"user"})),
    "card_owner_revoked": ("card_owner", "agent_card", frozenset({"user"})),
    "command_attempt_recorded": ("command_attempt", "receipt", frozenset({"system", "user"})),
}
if frozenset(_EVENT_CONTRACTS) != _EVENT_TYPES:
    raise RuntimeError("OperationalEvent contract is not exhaustive")


class OperationalEvent(_StrictFrozen):
    cursor: Annotated[int, Field(gt=0)]
    event_id: SafeReference
    org_id: SafeReference
    event_type: str
    occurred_at: datetime
    actor: SystemActor | UserActor
    resource: SafeResourceRef
    outcome: Literal["committed", "denied", "failed"]
    audit_id: SafeReference
    receipt_id: SafeReference
    policy_epoch: Annotated[int, Field(gt=0)]
    policy_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    change: SafeChange

    def model_post_init(self, __context: object) -> None:
        if self.event_type not in _EVENT_TYPES:
            raise ValueError("unknown operational event type")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() != timedelta(0):
            raise ValueError("occurred_at must be UTC")
        change_kind, resource_kind, actor_kinds = _EVENT_CONTRACTS[self.event_type]
        match (self.change.kind, self.resource.kind, self.actor.kind):
            case (actual_change, actual_resource, actual_actor) if (
                actual_change == change_kind
                and actual_resource == resource_kind
                and actual_actor in actor_kinds
            ):
                pass
            case _:
                raise ValueError("event/change/resource/actor contract mismatch")


class AuditAuthority(_StrictFrozen):
    policy_revision_id: SafeReference
    policy_epoch: Annotated[int, Field(gt=0)]
    policy_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def canonical_v19_file_authority(
    *, source_policy_digest: str, current_snapshot_digest: str,
) -> AuditAuthority:
    """Bind a pre-v20 writer to the exact current YAML Authority snapshot."""
    if (
        _SHA256.fullmatch(source_policy_digest) is None
        or _SHA256.fullmatch(current_snapshot_digest) is None
        or source_policy_digest != current_snapshot_digest
    ):
        raise OperationalEvidenceUnavailable("current YAML Authority provenance unavailable")
    return AuditAuthority(
        policy_revision_id=f"yaml:{current_snapshot_digest}",
        policy_epoch=1,
        policy_digest=current_snapshot_digest,
    )


class ApprovalEvidence(_StrictFrozen):
    evidence_id: SafeReference
    evidence_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AuditRecord(_StrictFrozen):
    audit_id: SafeReference
    org_id: SafeReference
    occurred_at: datetime
    actor: SystemActor | UserActor
    action: Annotated[str, Field(pattern=r"^[a-z][a-z0-9._-]{0,127}$")]
    resource: SafeResourceRef
    outcome: Literal["committed", "denied", "failed"]
    command_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    receipt_id: SafeReference
    authority: AuditAuthority
    approval_evidence: ApprovalEvidence | None = None
    change: SafeChange


class AuditRecordView(_StrictFrozen):
    cursor: Annotated[int, Field(gt=0)]
    record: AuditRecord


SourceReceiptKind: TypeAlias = Literal[
    "question_create", "question_initial_state", "manager_initial",
    "question_dispatch_state", "answer_ingest",
    "approval_creation", "approval_disposition", "answer_finalization", "feedback",
    "conflict_concurrence", "conflict_state", "manager_deadlock",
    "approval_reassignment", "backup_review_creation", "reevaluation_creation",
    "backup_review_disposition", "reevaluation_disposition",
    "registry_user_registration", "agent_card_registration",
]


class SourceReceiptProvenance(_StrictFrozen):
    kind: SourceReceiptKind
    receipt_key: SafeReference
    receipt_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _SourceReceiptManifest(_StrictFrozen):
    kind: SourceReceiptKind
    table: str
    key_column: str
    event_type: str
    change_kind: str
    org_column: str | None = "org_id"
    org_join_table: str | None = None
    source_join_column: str | None = None
    join_key_column: str | None = None
    exact_columns: tuple[str, ...] | None = None
    required_foreign_key: tuple[str, str, str] | None = None


_SOURCE_MANIFESTS: Final[dict[SourceReceiptKind, _SourceReceiptManifest]] = {
    manifest.kind: manifest
    for manifest in (
        _SourceReceiptManifest(kind="question_create", table="central_question_create_receipts", key_column="request_id", event_type="question_received", change_kind="question"),
        _SourceReceiptManifest(
            kind="question_initial_state", table="central_question_initial_transition_receipts",
            key_column="receipt_id", event_type="request_state_changed", change_kind="question",
            exact_columns=(
                "receipt_id", "org_id", "request_id", "command_digest", "from_state",
                "to_state", "manager_item_id", "policy_revision_id", "policy_epoch",
                "policy_digest", "authority_policy_revision_id",
                "authority_policy_epoch", "authority_policy_digest", "created_at",
            ),
            required_foreign_key=("request_id", "question_requests", "request_id"),
        ),
        _SourceReceiptManifest(
            kind="manager_initial", table="central_question_initial_transition_receipts",
            key_column="receipt_id", event_type="manager_item_changed", change_kind="manager_item",
            exact_columns=(
                "receipt_id", "org_id", "request_id", "command_digest", "from_state",
                "to_state", "manager_item_id", "policy_revision_id", "policy_epoch",
                "policy_digest", "authority_policy_revision_id",
                "authority_policy_epoch", "authority_policy_digest", "created_at",
            ),
            required_foreign_key=("request_id", "question_requests", "request_id"),
        ),
        _SourceReceiptManifest(kind="question_dispatch_state", table="central_question_work_ticket_receipts", key_column="ticket_id", event_type="request_state_changed", change_kind="question", org_column=None, org_join_table="central_question_work_tickets", source_join_column="ticket_id", join_key_column="ticket_id"),
        _SourceReceiptManifest(kind="answer_ingest", table="central_question_answer_ingest_receipts", key_column="ticket_id", event_type="answer_finalized", change_kind="answer"),
        _SourceReceiptManifest(kind="approval_creation", table="central_question_answer_ingest_receipts", key_column="ticket_id", event_type="approval_changed", change_kind="approval"),
        _SourceReceiptManifest(kind="approval_disposition", table="central_question_approval_disposition_receipts", key_column="receipt_id", event_type="approval_changed", change_kind="approval"),
        _SourceReceiptManifest(kind="answer_finalization", table="central_question_approval_disposition_receipts", key_column="receipt_id", event_type="answer_finalized", change_kind="answer"),
        _SourceReceiptManifest(kind="feedback", table="central_question_feedback_receipts", key_column="receipt_id", event_type="feedback_recorded", change_kind="feedback"),
        _SourceReceiptManifest(kind="conflict_concurrence", table="central_inbox_conflict_receipts", key_column="receipt_id", event_type="conflict_concurrence_recorded", change_kind="conflict"),
        _SourceReceiptManifest(kind="conflict_state", table="central_inbox_conflict_receipts", key_column="receipt_id", event_type="request_state_changed", change_kind="question"),
        _SourceReceiptManifest(kind="manager_deadlock", table="central_inbox_conflict_receipts", key_column="receipt_id", event_type="manager_item_changed", change_kind="manager_item"),
        _SourceReceiptManifest(kind="approval_reassignment", table="central_inbox_approval_reassignment_receipts", key_column="receipt_id", event_type="approval_changed", change_kind="approval"),
        _SourceReceiptManifest(kind="backup_review_creation", table="central_inbox_review_projection_receipts", key_column="projection_receipt_id", event_type="backup_review_changed", change_kind="backup_review", org_column=None, org_join_table="central_inbox_review_outbox_intents", source_join_column="intent_id", join_key_column="intent_id"),
        _SourceReceiptManifest(kind="reevaluation_creation", table="central_inbox_review_projection_receipts", key_column="projection_receipt_id", event_type="reevaluation_changed", change_kind="reevaluation", org_column=None, org_join_table="central_inbox_review_outbox_intents", source_join_column="intent_id", join_key_column="intent_id"),
        _SourceReceiptManifest(kind="backup_review_disposition", table="central_inbox_backup_review_disposition_receipts", key_column="receipt_id", event_type="backup_review_changed", change_kind="backup_review"),
        _SourceReceiptManifest(kind="reevaluation_disposition", table="central_inbox_reevaluation_disposition_receipts", key_column="receipt_id", event_type="reevaluation_changed", change_kind="reevaluation"),
        _SourceReceiptManifest(kind="registry_user_registration", table="production_registry_user_command_receipts", key_column="idempotency_key", event_type="registry_user_registered", change_kind="registry"),
        _SourceReceiptManifest(kind="agent_card_registration", table="production_agent_card_command_receipts", key_column="idempotency_key", event_type="agent_card_registered", change_kind="agent_card"),
    )
}
_SOURCE_AUTHORITY_COLUMNS: Final = frozenset({
    "authority_policy_revision_id",
    "authority_policy_epoch",
    "authority_policy_digest",
})


def _source_receipt_row(
    connection: sqlite3.Connection, kind: SourceReceiptKind, org_id: str,
    receipt_key: str,
) -> sqlite3.Row:
    manifest = _SOURCE_MANIFESTS[kind]
    table_info = tuple(connection.execute(f"PRAGMA table_info({manifest.table})"))
    actual_column_order = tuple(str(row[1]) for row in table_info)
    columns = {str(row[1]) for row in table_info}
    required_columns = {manifest.key_column} | _SOURCE_AUTHORITY_COLUMNS
    if manifest.org_column is not None:
        required_columns.add(manifest.org_column)
    if manifest.source_join_column is not None:
        required_columns.add(manifest.source_join_column)
    if not table_info or not required_columns <= columns:
        raise OperationalEvidenceUnavailable("source receipt manifest unavailable")
    if manifest.exact_columns is not None and actual_column_order != manifest.exact_columns:
        raise OperationalEvidenceUnavailable("source receipt exact columns unavailable")
    if manifest.required_foreign_key is not None:
        source_column, target_table, target_column = manifest.required_foreign_key
        foreign_keys = tuple(connection.execute(
            f"PRAGMA foreign_key_list({manifest.table})"
        ))
        if not any(
            str(row[3]) == source_column
            and str(row[2]) == target_table
            and str(row[4]) == target_column
            for row in foreign_keys
        ):
            raise OperationalEvidenceUnavailable("source receipt foreign key unavailable")
    prior_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        if manifest.org_column is not None:
            rows = tuple(connection.execute(
                f"SELECT * FROM {manifest.table} WHERE {manifest.org_column}=? AND {manifest.key_column}=?",
                (org_id, receipt_key),
            ))
        else:
            rows = tuple(connection.execute(
                f"SELECT * FROM {manifest.table} WHERE {manifest.key_column}=?",
                (receipt_key,),
            ))
    finally:
        connection.row_factory = prior_factory
    if len(rows) != 1:
        raise OperationalEvidenceUnavailable("source receipt provenance unavailable")
    if (
        kind in {"question_initial_state", "manager_initial"}
        and rows[0]["receipt_id"] != f"initial-transition:{rows[0]['request_id']}"
    ):
        raise OperationalEvidenceUnavailable(
            "initial transition receipt identity unavailable"
        )
    if manifest.org_column is None:
        if (
            manifest.org_join_table is None or manifest.source_join_column is None
            or manifest.join_key_column is None
        ):
            raise OperationalEvidenceUnavailable("source receipt org binding unavailable")
        join_value = rows[0][manifest.source_join_column]
        org_rows = tuple(connection.execute(
            f"SELECT org_id FROM {manifest.org_join_table} WHERE {manifest.join_key_column}=?",
            (join_value,),
        ))
        if len(org_rows) != 1 or str(org_rows[0][0]) != org_id:
            raise OperationalEvidenceUnavailable("source receipt org binding unavailable")
    return rows[0]


def source_receipt_digest(
    connection: sqlite3.Connection, kind: SourceReceiptKind, org_id: str,
    receipt_key: str,
) -> str:
    """Digest the exact committed source receipt row without copying raw fields."""
    if not _REFERENCE.fullmatch(org_id) or not _REFERENCE.fullmatch(receipt_key):
        raise OperationalEvidenceUnavailable("invalid source receipt reference")
    row = _source_receipt_row(connection, kind, org_id, receipt_key)
    return _digest({key: row[key] for key in row.keys()})


def _validate_source_provenance(
    connection: sqlite3.Connection, provenance: SourceReceiptProvenance,
    org_id: str, event_type: str, change: SafeChange,
    resource: SafeResourceRef, actor_user_id: str | None,
    command_digest: str, occurred_at: str, policy_revision_id: str,
    policy_epoch: int, policy_digest: str,
) -> None:
    manifest = _SOURCE_MANIFESTS[provenance.kind]
    if manifest.event_type != event_type or manifest.change_kind != change.kind:
        raise OperationalEvidenceUnavailable("source/event manifest mismatch")
    row = _source_receipt_row(connection, provenance.kind, org_id, provenance.receipt_key)
    actual = _digest({key: row[key] for key in row.keys()})
    if actual != provenance.receipt_digest:
        raise OperationalEvidenceUnavailable("source receipt digest mismatch")
    if (
        row["authority_policy_revision_id"] != policy_revision_id
        or int(row["authority_policy_epoch"]) != policy_epoch
        or row["authority_policy_digest"] != policy_digest
    ):
        raise OperationalEvidenceUnavailable("source receipt Authority binding mismatch")
    match provenance.kind:
        case "question_create":
            if not (
                isinstance(change, QuestionChange)
                and change.request_id == row["request_id"]
                and change.from_state is None
                and change.to_state == "received"
                and resource.kind == "question_request"
                and resource.resource_id == row["request_id"]
                and actor_user_id == row["requester_id"]
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "question create source binding mismatch"
                )
        case "question_initial_state":
            if not (
                isinstance(change, QuestionChange)
                and change.request_id == row["request_id"]
                and change.from_state == row["from_state"]
                and change.to_state == row["to_state"]
                and resource.kind == "question_request"
                and resource.resource_id == row["request_id"]
                and actor_user_id is None
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable("initial state source binding mismatch")
        case "manager_initial":
            if not (
                isinstance(change, ManagerItemChange)
                and row["manager_item_id"] is not None
                and change.manager_item_id == row["manager_item_id"]
                and change.request_id == row["request_id"]
                and resource.kind == "manager_item"
                and resource.resource_id == row["manager_item_id"]
                and actor_user_id is None
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable("initial manager source binding mismatch")
        case "question_dispatch_state":
            if not (
                isinstance(change, QuestionChange)
                and change.request_id == row["request_id"]
                and change.from_state == "ready_to_dispatch"
                and change.to_state == "awaiting_answer"
                and resource.kind == "question_request"
                and resource.resource_id == row["request_id"]
                and actor_user_id is None
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "question dispatch source binding mismatch"
                )
        case "answer_ingest":
            if not (
                isinstance(change, AnswerChange)
                and row["result_kind"] == "answered"
                and row["record_id"] is not None
                and change.request_id == row["request_id"]
                and change.record_id == row["record_id"]
                and resource.kind == "question_request"
                and resource.resource_id == row["request_id"]
                and actor_user_id == row["delivery_subject"]
                and command_digest == row["candidate_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "answer ingest source binding mismatch"
                )
        case "approval_creation":
            if not (
                isinstance(change, ApprovalChange)
                and row["result_kind"] == "awaiting_approval"
                and row["approval_item_id"] is not None
                and change.request_id == row["request_id"]
                and change.approval_item_id == row["approval_item_id"]
                and resource.kind == "approval_item"
                and resource.resource_id == row["approval_item_id"]
                and actor_user_id == row["delivery_subject"]
                and command_digest == row["candidate_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "approval creation source binding mismatch"
                )
        case "approval_disposition":
            if not (
                isinstance(change, ApprovalChange)
                and change.request_id == row["request_id"]
                and change.approval_item_id == row["approval_item_id"]
                and resource.kind == "approval_item"
                and resource.resource_id == row["approval_item_id"]
                and actor_user_id == row["actor_id"]
                and command_digest == row["decision_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "approval disposition source binding mismatch"
                )
        case "answer_finalization":
            if not (
                isinstance(change, AnswerChange)
                and row["terminal_kind"] == "answered"
                and row["record_id"] is not None
                and change.request_id == row["request_id"]
                and change.record_id == row["record_id"]
                and resource.kind == "question_request"
                and resource.resource_id == row["request_id"]
                and actor_user_id == row["actor_id"]
                and command_digest == row["decision_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "answer finalization source binding mismatch"
                )
        case "feedback":
            if not (
                isinstance(change, FeedbackChange)
                and change.request_id == row["request_id"]
                and change.record_id == row["record_id"]
                and change.feedback_id == row["feedback_id"]
                and resource.kind == "question_request"
                and resource.resource_id == row["request_id"]
                and actor_user_id == row["requester_id"]
                and command_digest == row["payload_digest"]
                and occurred_at == row["submitted_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "feedback source binding mismatch"
                )
        case "conflict_concurrence":
            if not (
                isinstance(change, ConflictChange)
                and change.case_id == row["case_id"]
                and change.request_id == row["request_id"]
                and resource.kind == "conflict_case"
                and resource.resource_id == row["case_id"]
                and actor_user_id == row["actor_id"]
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "conflict concurrence source binding mismatch"
                )
        case "conflict_state":
            expected_to_state: RequestState | None = {
                "agreed": "ready_to_dispatch",
                "route_rejected": "declined",
                "deadlocked": "awaiting_manager",
            }.get(str(row["outcome"]))  # type: ignore[assignment]
            if not (
                isinstance(change, QuestionChange)
                and expected_to_state is not None
                and change.request_id == row["request_id"]
                and change.from_state == "awaiting_conflict"
                and change.to_state == expected_to_state
                and resource.kind == "question_request"
                and resource.resource_id == row["request_id"]
                and actor_user_id == row["actor_id"]
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "conflict state source binding mismatch"
                )
        case "manager_deadlock":
            if not (
                isinstance(change, ManagerItemChange)
                and row["outcome"] == "deadlocked"
                and row["manager_item_id"] is not None
                and change.manager_item_id == row["manager_item_id"]
                and change.request_id == row["request_id"]
                and resource.kind == "manager_item"
                and resource.resource_id == row["manager_item_id"]
                and actor_user_id == row["actor_id"]
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "manager deadlock source binding mismatch"
                )
        case "approval_reassignment":
            if not (
                isinstance(change, ApprovalChange)
                and change.request_id == row["request_id"]
                and change.approval_item_id
                == row["successor_approval_item_id"]
                and resource.kind == "approval_item"
                and resource.resource_id
                == row["successor_approval_item_id"]
                and actor_user_id == row["actor_id"]
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "approval reassignment source binding mismatch"
                )
        case "backup_review_creation":
            intent = _source_companion_row(
                connection,
                "SELECT source_kind,request_id,producer_receipt_digest "
                "FROM central_inbox_review_outbox_intents WHERE intent_id=?",
                str(row["intent_id"]),
            )
            if not (
                isinstance(change, BackupReviewChange)
                and row["source_kind"] == "backup_review"
                and intent["source_kind"] == "backup_review"
                and change.review_id == row["aggregate_id"]
                and change.request_id == intent["request_id"]
                and resource.kind == "backup_review"
                and resource.resource_id == row["aggregate_id"]
                and actor_user_id is None
                and command_digest == intent["producer_receipt_digest"]
                and occurred_at == row["projected_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "backup review creation source binding mismatch"
                )
        case "reevaluation_creation":
            intent = _source_companion_row(
                connection,
                "SELECT source_kind,request_id,producer_receipt_digest "
                "FROM central_inbox_review_outbox_intents WHERE intent_id=?",
                str(row["intent_id"]),
            )
            if not (
                isinstance(change, ReevaluationChange)
                and row["source_kind"] == "reevaluation"
                and intent["source_kind"] == "reevaluation"
                and change.reevaluation_id == row["aggregate_id"]
                and change.request_id == intent["request_id"]
                and resource.kind == "reevaluation"
                and resource.resource_id == row["aggregate_id"]
                and actor_user_id is None
                and command_digest == intent["producer_receipt_digest"]
                and occurred_at == row["projected_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "reevaluation creation source binding mismatch"
                )
        case "backup_review_disposition":
            aggregate = _source_companion_row(
                connection,
                "SELECT request_id FROM central_inbox_backup_reviews "
                "WHERE review_id=?",
                str(row["review_id"]),
            )
            if not (
                isinstance(change, BackupReviewChange)
                and change.review_id == row["review_id"]
                and change.request_id == aggregate["request_id"]
                and resource.kind == "backup_review"
                and resource.resource_id == row["review_id"]
                and actor_user_id == row["actor_id"]
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "backup review disposition source binding mismatch"
                )
        case "reevaluation_disposition":
            aggregate = _source_companion_row(
                connection,
                "SELECT request_id FROM central_inbox_reevaluations "
                "WHERE reevaluation_id=?",
                str(row["reevaluation_id"]),
            )
            if not (
                isinstance(change, ReevaluationChange)
                and change.reevaluation_id == row["reevaluation_id"]
                and change.request_id == aggregate["request_id"]
                and resource.kind == "reevaluation"
                and resource.resource_id == row["reevaluation_id"]
                and actor_user_id == row["actor_id"]
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "reevaluation disposition source binding mismatch"
                )
        case "registry_user_registration":
            if not (
                isinstance(change, RegistryChange)
                and resource.kind == "registry_user"
                and resource.resource_id == row["result_user_id"]
                and actor_user_id == row["principal_id"]
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "Registry User source binding mismatch"
                )
        case "agent_card_registration":
            if not (
                isinstance(change, AgentCardChange)
                and change.card_id == row["result_agent_id"]
                and resource.kind == "agent_card"
                and resource.resource_id == row["result_agent_id"]
                and actor_user_id == row["principal_id"]
                and command_digest == row["command_digest"]
                and occurred_at == row["created_at"]
            ):
                raise OperationalEvidenceUnavailable(
                    "Agent Card source binding mismatch"
                )


def _source_companion_row(
    connection: sqlite3.Connection, statement: str, key: str,
) -> sqlite3.Row:
    prior_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        rows = tuple(connection.execute(statement, (key,)))
    finally:
        connection.row_factory = prior_factory
    if len(rows) != 1:
        raise OperationalEvidenceUnavailable("source companion binding unavailable")
    return rows[0]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode()).hexdigest()


def _now_text(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OperationalEvidenceUnavailable("UTC clock required")
    return value.isoformat().replace("+00:00", "Z")


_TABLE_DDLS: tuple[str, ...] = (
    """CREATE TABLE central_operational_audit_records (
       audit_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
       actor_kind TEXT NOT NULL CHECK(actor_kind IN ('system','user')), actor_user_id TEXT,
       action TEXT NOT NULL, resource_kind TEXT NOT NULL, resource_id TEXT NOT NULL,
       outcome TEXT NOT NULL CHECK(outcome IN ('committed','denied','failed')),
       command_digest TEXT NOT NULL CHECK(length(command_digest)=64), receipt_id TEXT NOT NULL UNIQUE,
       policy_revision_id TEXT NOT NULL, policy_epoch INTEGER NOT NULL CHECK(policy_epoch>0),
       policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64), approval_evidence_id TEXT,
       approval_evidence_digest TEXT, change_kind TEXT NOT NULL, change_json TEXT NOT NULL,
       source_kind TEXT NOT NULL, source_receipt_key TEXT NOT NULL,
       source_receipt_digest TEXT NOT NULL CHECK(length(source_receipt_digest)=64),
       CHECK((actor_kind='system' AND actor_user_id IS NULL) OR (actor_kind='user' AND actor_user_id IS NOT NULL)),
       CHECK((approval_evidence_id IS NULL AND approval_evidence_digest IS NULL) OR (approval_evidence_id IS NOT NULL AND approval_evidence_digest IS NOT NULL))
    )""",
    """CREATE TABLE central_operational_event_intents (
       intent_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, event_id TEXT NOT NULL,
       event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, audit_id TEXT NOT NULL UNIQUE,
       receipt_id TEXT NOT NULL UNIQUE, payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
       payload_json TEXT NOT NULL,
       status TEXT NOT NULL CHECK(status IN ('pending','leased','delivered')), worker_id TEXT,
       lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0), delivered_at TEXT,
       FOREIGN KEY(audit_id) REFERENCES central_operational_audit_records(audit_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       UNIQUE(org_id,event_id), UNIQUE(org_id,intent_id)
    )""",
    """CREATE TABLE central_operational_events (
       org_id TEXT NOT NULL, cursor INTEGER NOT NULL CHECK(cursor>0), event_id TEXT NOT NULL,
       event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, audit_id TEXT NOT NULL UNIQUE,
       receipt_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
       intent_id TEXT NOT NULL UNIQUE,
       PRIMARY KEY(org_id,cursor), UNIQUE(org_id,event_id),
       FOREIGN KEY(audit_id) REFERENCES central_operational_audit_records(audit_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(intent_id) REFERENCES central_operational_event_intents(intent_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_operational_cursor_heads (
       org_id TEXT PRIMARY KEY NOT NULL, latest_cursor INTEGER NOT NULL CHECK(latest_cursor>=0)
    )""",
    """CREATE TABLE central_operational_projection_receipts (
       intent_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, cursor INTEGER NOT NULL CHECK(cursor>0),
       event_id TEXT NOT NULL, payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64), projected_at TEXT NOT NULL,
       FOREIGN KEY(intent_id) REFERENCES central_operational_event_intents(intent_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       UNIQUE(org_id,cursor), UNIQUE(org_id,event_id)
    )""",
    """CREATE TABLE central_operational_retention_receipts (
       receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, through_cursor INTEGER NOT NULL CHECK(through_cursor>=0),
       retained_from_cursor INTEGER NOT NULL CHECK(retained_from_cursor>=1), policy_count INTEGER NOT NULL CHECK(policy_count BETWEEN 1000 AND 1000000),
       digest TEXT NOT NULL CHECK(length(digest)=64), created_at TEXT NOT NULL, UNIQUE(org_id,through_cursor)
    )""",
    """CREATE TABLE central_operational_retention_authorizations (
       org_id TEXT PRIMARY KEY NOT NULL, through_cursor INTEGER NOT NULL CHECK(through_cursor>=0)
    )""",
    """CREATE TABLE central_operational_retention_members (
       org_id TEXT NOT NULL, audit_id TEXT NOT NULL, intent_id TEXT NOT NULL,
       PRIMARY KEY(org_id,audit_id), UNIQUE(intent_id)
    )""",
)
_INDEX_DDLS = (
    "CREATE INDEX central_operational_event_intents_claimable ON central_operational_event_intents(status,lease_until,occurred_at,intent_id)",
    "CREATE INDEX central_operational_audit_org_time ON central_operational_audit_records(org_id,occurred_at,audit_id)",
)
_IMMUTABLE_TRIGGERS = (
    "CREATE TRIGGER central_operational_audit_records_no_update BEFORE UPDATE ON central_operational_audit_records BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END",
    "CREATE TRIGGER central_operational_audit_records_no_delete BEFORE DELETE ON central_operational_audit_records WHEN NOT EXISTS (SELECT 1 FROM central_operational_retention_members m WHERE m.org_id=OLD.org_id AND m.audit_id=OLD.audit_id) BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END",
    "CREATE TRIGGER central_operational_events_no_update BEFORE UPDATE ON central_operational_events BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END",
    "CREATE TRIGGER central_operational_events_no_delete BEFORE DELETE ON central_operational_events WHEN NOT EXISTS (SELECT 1 FROM central_operational_retention_authorizations a WHERE a.org_id=OLD.org_id AND OLD.cursor<=a.through_cursor) BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END",
    "CREATE TRIGGER central_operational_projection_receipts_no_update BEFORE UPDATE ON central_operational_projection_receipts BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END",
    "CREATE TRIGGER central_operational_projection_receipts_no_delete BEFORE DELETE ON central_operational_projection_receipts WHEN NOT EXISTS (SELECT 1 FROM central_operational_retention_members m WHERE m.org_id=OLD.org_id AND m.intent_id=OLD.intent_id) BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END",
    "CREATE TRIGGER central_operational_retention_receipts_no_update BEFORE UPDATE ON central_operational_retention_receipts BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END",
    "CREATE TRIGGER central_operational_retention_receipts_no_delete BEFORE DELETE ON central_operational_retention_receipts BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END",
)
_OUTBOX_TRIGGERS = (
    """CREATE TRIGGER central_operational_event_intents_frozen BEFORE UPDATE ON central_operational_event_intents
       WHEN NEW.intent_id!=OLD.intent_id OR NEW.org_id!=OLD.org_id OR NEW.event_id!=OLD.event_id OR NEW.event_type!=OLD.event_type OR NEW.occurred_at!=OLD.occurred_at OR NEW.audit_id!=OLD.audit_id OR NEW.receipt_id!=OLD.receipt_id OR NEW.payload_digest!=OLD.payload_digest OR NEW.payload_json!=OLD.payload_json
       BEGIN SELECT RAISE(ABORT,'immutable operational intent'); END""",
    "CREATE TRIGGER central_operational_event_intents_no_delete BEFORE DELETE ON central_operational_event_intents WHEN NOT EXISTS (SELECT 1 FROM central_operational_retention_members m WHERE m.org_id=OLD.org_id AND m.intent_id=OLD.intent_id) BEGIN SELECT RAISE(ABORT,'immutable operational intent'); END",
)
_OWNED_TABLES: Final = tuple(statement.split()[2] for statement in _TABLE_DDLS)


def _normalized_sql(value: object) -> str:
    return " ".join(str(value or "").split())


def _owned_catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    tables = tuple(
        (
            table,
            _normalized_sql(connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({table})")),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA index_list({table})")),
        )
        for table in _OWNED_TABLES
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None
    )
    placeholders = ",".join("?" for _ in _OWNED_TABLES)
    objects = tuple(
        (row[0], row[1], row[2], _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            f"WHERE tbl_name IN ({placeholders}) AND type IN ('index','trigger') ORDER BY type,name",
            _OWNED_TABLES,
        )
    )
    return tables, objects


def _expected_owned_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in _TABLE_DDLS + _INDEX_DDLS + _IMMUTABLE_TRIGGERS + _OUTBOX_TRIGGERS:
            connection.execute(statement)
        return _owned_catalog(connection)
    finally:
        connection.close()


_EXPECTED_OWNED_CATALOG: Final = _expected_owned_catalog()


def _validate_owned_catalog(connection: sqlite3.Connection) -> None:
    if _owned_catalog(connection) != _EXPECTED_OWNED_CATALOG:
        raise OperationalEvidenceUnavailable("operational evidence catalog drift")


def _validate_v19_read_connection(connection: sqlite3.Connection) -> None:
    marker = tuple(
        tuple(row) for row in connection.execute(
            "SELECT name,version FROM aon_installation_schema ORDER BY name"
        )
    )
    if (
        len(marker) != 1
        or marker[0][0] != "central-installation"
        or type(marker[0][1]) is not int
        or int(marker[0][1]) < SCHEMA_VERSION
    ):
        raise OperationalEvidenceUnavailable("operational evidence marker unavailable")
    _validate_operational_uow_connection(connection)


def _validate_operational_uow_connection(
    connection: sqlite3.Connection, *, deep: bool = True,
) -> None:
    _validate_owned_catalog(connection)
    prior_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _reconcile(connection, deep=deep)
    finally:
        connection.row_factory = prior_factory


def migrate_central_operational_evidence_schema(path: Path, *, fault_injector: Callable[[str], None] | None = None) -> None:
    """Atomically migrate only Central marker v18 to v19, marker last."""
    def _no_fault(_point: str) -> None: return None
    fault: Callable[[str], None] = fault_injector or _no_fault
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        marker = connection.execute("SELECT version FROM aon_installation_schema WHERE name='central-installation'").fetchone()
        if marker is None or marker[0] != 18:
            if marker is not None and int(marker[0]) >= SCHEMA_VERSION and central_operational_evidence_schema_ready(path):
                return
            raise OperationalEvidenceUnavailable("operational evidence requires exactly v18")
        # Older component-recovery tests may deliberately rewind only the
        # installation marker.  A complete, reconciled v19 catalog is already
        # immutable evidence; advance the marker rather than attempting a
        # destructive/recreating migration.  Any partial catalog remains a
        # fail-closed error below.
        existing_tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        expected_tables = set(_OWNED_TABLES)
        if expected_tables <= existing_tables:
            _validate_owned_catalog(connection)
            _reconcile(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("UPDATE aon_installation_schema SET version=19 WHERE name='central-installation'")
                connection.commit()
                return
            except Exception:
                connection.rollback()
                raise
        if existing_tables & expected_tables:
            raise OperationalEvidenceUnavailable("partial operational evidence catalog")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _TABLE_DDLS:
                connection.execute(statement)
            for statement in _INDEX_DDLS + _IMMUTABLE_TRIGGERS + _OUTBOX_TRIGGERS:
                connection.execute(statement)
            _backfill_safe_sources(connection)
            _reconcile(connection)
            fault("before-v19-marker")
            connection.execute("UPDATE aon_installation_schema SET version=19 WHERE name='central-installation'")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except OperationalEvidenceUnavailable:
        raise
    except Exception as error:
        raise OperationalEvidenceUnavailable("operational evidence migration unavailable") from error
    finally:
        connection.close()


def _backfill_safe_sources(connection: sqlite3.Connection) -> None:
    """Reject unverifiable history; never synthesize timestamps or Authority.

    Existing v18 writers did not persist one common exact Authority/source
    companion capable of reconstructing the sealed event variants.  Therefore
    a non-empty eligible producer catalog is a migration dependency, not a cue
    to manufacture ``1970`` command-attempt rows.  Producer migrations must
    first add an exact safe companion and then this manifest seam can admit it.
    Raw legacy/demo audit tables are intentionally not inspected.
    """
    for manifest in _SOURCE_MANIFESTS.values():
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (manifest.table,),
        ).fetchone() is None:
            continue
        count = int(connection.execute(
            f"SELECT count(*) FROM {manifest.table}"
        ).fetchone()[0])
        if count:
            raise OperationalEvidenceUnavailable(
                f"v18 {manifest.kind} history lacks exact safe Authority companion"
            )


def _reconcile(connection: sqlite3.Connection, *, deep: bool = True) -> None:
    for table in (
        "central_operational_audit_records", "central_operational_event_intents",
        "central_operational_events", "central_operational_cursor_heads",
        "central_operational_projection_receipts",
    ):
        row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        if row is None or row[0] < 0:
            raise OperationalEvidenceUnavailable("operational evidence reconciliation failed")
    checks = (
        "SELECT count(*) FROM central_operational_event_intents i "
        "LEFT JOIN central_operational_audit_records a ON a.audit_id=i.audit_id "
        "WHERE a.audit_id IS NULL OR a.org_id!=i.org_id OR a.receipt_id!=i.receipt_id",
        "SELECT count(*) FROM central_operational_audit_records a "
        "LEFT JOIN central_operational_event_intents i ON i.audit_id=a.audit_id "
        "WHERE i.audit_id IS NULL",
        "SELECT count(*) FROM central_operational_events e "
        "LEFT JOIN central_operational_event_intents i ON i.intent_id=e.intent_id "
        "WHERE i.intent_id IS NULL OR i.org_id!=e.org_id OR i.event_id!=e.event_id "
        "OR i.audit_id!=e.audit_id OR i.receipt_id!=e.receipt_id",
        "SELECT count(*) FROM central_operational_projection_receipts p "
        "LEFT JOIN central_operational_events e ON e.intent_id=p.intent_id "
        "WHERE e.intent_id IS NULL OR e.org_id!=p.org_id OR e.cursor!=p.cursor "
        "OR e.event_id!=p.event_id OR e.payload_digest!=p.payload_digest",
        "SELECT count(*) FROM central_operational_cursor_heads h "
        "WHERE h.latest_cursor != COALESCE((SELECT max(e.cursor) FROM central_operational_events e WHERE e.org_id=h.org_id),0)",
        "SELECT count(*) FROM central_operational_retention_authorizations",
        "SELECT count(*) FROM central_operational_retention_members",
    )
    if any(connection.execute(statement).fetchone()[0] for statement in checks):
        raise OperationalEvidenceUnavailable("operational evidence reverse binding drift")
    if not deep:
        return
    for intent in connection.execute(
        "SELECT * FROM central_operational_event_intents ORDER BY org_id,intent_id"
    ):
        audit = connection.execute(
            "SELECT * FROM central_operational_audit_records WHERE audit_id=?",
            (intent["audit_id"],),
        ).fetchone()
        if audit is None:
            raise OperationalEvidenceUnavailable("orphan operational intent")
        _validate_intent_binding(connection, intent, audit)


def _validate_intent_binding(
    connection: sqlite3.Connection, intent: sqlite3.Row, audit: sqlite3.Row,
) -> None:
    source = SourceReceiptProvenance(
        kind=audit["source_kind"], receipt_key=audit["source_receipt_key"],
        receipt_digest=audit["source_receipt_digest"],
    )
    change = _SAFE_CHANGE_ADAPTER.validate_json(audit["change_json"])
    resource = SafeResourceRef(
        kind=audit["resource_kind"], resource_id=audit["resource_id"]
    )
    _validate_source_provenance(
        connection, source, str(audit["org_id"]), str(intent["event_type"]), change,
        resource, audit["actor_user_id"], str(audit["command_digest"]),
        str(audit["occurred_at"]), str(audit["policy_revision_id"]),
        int(audit["policy_epoch"]), str(audit["policy_digest"]),
    )
    command_digest = str(audit["command_digest"])
    org_id = str(audit["org_id"])
    receipt_id = str(audit["receipt_id"])
    audit_id = sha256(
        ("operational-audit" + org_id + receipt_id + command_digest).encode()
    ).hexdigest()
    event_id = sha256(
        ("operational-event-id" + org_id + receipt_id + command_digest).encode()
    ).hexdigest()
    intent_id = sha256(
        ("operational-event" + org_id + receipt_id + audit_id + command_digest).encode()
    ).hexdigest()
    if (
        audit["audit_id"] != audit_id or intent["audit_id"] != audit_id
        or intent["event_id"] != event_id or intent["intent_id"] != intent_id
        or intent["receipt_id"] != receipt_id or intent["org_id"] != org_id
        or intent["occurred_at"] != audit["occurred_at"]
    ):
        raise OperationalEvidenceUnavailable("deterministic operational ID drift")
    payload = _safe_intent_payload(
        org_id=org_id, event_id=event_id, event_type=str(intent["event_type"]),
        occurred_at=str(intent["occurred_at"]), actor=audit["actor_user_id"],
        action=str(audit["action"]), resource=resource, audit_id=audit_id,
        receipt_id=receipt_id, command_digest=command_digest,
        policy_revision_id=str(audit["policy_revision_id"]),
        policy_epoch=int(audit["policy_epoch"]),
        policy_digest=str(audit["policy_digest"]), change=change, source=source,
    )
    payload_json = _canonical(payload)
    if intent["payload_json"] != payload_json or intent["payload_digest"] != _digest(payload):
        raise OperationalEvidenceUnavailable("operational intent payload drift")
    event = connection.execute(
        "SELECT * FROM central_operational_events WHERE intent_id=?", (intent_id,)
    ).fetchone()
    if event is not None:
        expected_event = _event_from_rows(int(event["cursor"]), intent, audit)
        event_json = _canonical(expected_event.model_dump(mode="json"))
        if (
            event["payload_json"] != event_json
            or event["payload_digest"] != _digest(expected_event.model_dump(mode="json"))
        ):
            raise OperationalEvidenceUnavailable("projected operational event drift")


def _safe_intent_payload(
    *, org_id: str, event_id: str, event_type: str, occurred_at: str,
    actor: str | None, action: str, resource: SafeResourceRef, audit_id: str,
    receipt_id: str, command_digest: str, policy_revision_id: str,
    policy_epoch: int, policy_digest: str, change: SafeChange,
    source: SourceReceiptProvenance,
) -> dict[str, object]:
    actor_model: SystemActor | UserActor = (
        SystemActor() if actor is None else UserActor(user_id=actor)
    )
    occurred = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    # Constructing the domain event is the single exhaustive mapping gate.
    OperationalEvent(
        cursor=1, event_id=event_id, org_id=org_id, event_type=event_type,
        occurred_at=occurred, actor=actor_model, resource=resource,
        outcome="committed", audit_id=audit_id, receipt_id=receipt_id,
        policy_epoch=policy_epoch, policy_digest=policy_digest, change=change,
    )
    return {
        "org_id": org_id, "event_id": event_id, "event_type": event_type,
        "occurred_at": occurred.isoformat(), "actor": actor_model.model_dump(mode="json"),
        "action": action, "resource": resource.model_dump(mode="json"),
        "outcome": "committed", "audit_id": audit_id, "receipt_id": receipt_id,
        "command_digest": command_digest,
        "authority": {
            "policy_revision_id": policy_revision_id, "policy_epoch": policy_epoch,
            "policy_digest": policy_digest,
        },
        "change": change.model_dump(mode="json", exclude_none=True),
        "source": source.model_dump(mode="json"),
    }


def append_committed_source_evidence(
    connection: sqlite3.Connection,
    *,
    org_id: str,
    receipt_id: str,
    command_digest: str,
    event_type: str,
    action: str,
    resource: SafeResourceRef,
    change: SafeChange,
    actor_user_id: str | None,
    occurred_at: str,
    policy_revision_id: SafeReference,
    policy_epoch: Annotated[int, Field(gt=0)],
    policy_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    source: SourceReceiptProvenance,
) -> None:
    """Append safe audit plus outbox intent in an already-open source UoW.

    Callers use this immediately after their successful source receipt.  It
    deliberately accepts no question, answer, rationale, source, credential,
    browser session or identity claim material.
    """
    _validate_operational_uow_connection(connection, deep=False)
    _validate_source_provenance(
        connection, source, org_id, event_type, change, resource, actor_user_id,
        command_digest, occurred_at, policy_revision_id, policy_epoch, policy_digest,
    )
    _append_committed_source_evidence(
        connection, org_id=org_id, receipt_id=receipt_id,
        command_digest=command_digest, event_type=event_type, action=action,
        resource=resource, change=change, actor=actor_user_id,
        occurred_at=occurred_at, policy_revision_id=policy_revision_id,
        policy_epoch=policy_epoch, policy_digest=policy_digest, source=source,
    )


def append_committed_source_evidence_if_v19(
    connection: sqlite3.Connection, **kwargs: object,
) -> None:
    """No-op only before the v19 cutover; v19 missing catalog is unavailable."""
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aon_installation_schema'"
    ).fetchone() is None:
        return
    marker = connection.execute(
        "SELECT version FROM aon_installation_schema WHERE name='central-installation'"
    ).fetchone()
    if marker is None or int(marker[0]) < SCHEMA_VERSION:
        return
    if int(marker[0]) < SCHEMA_VERSION:
        return
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='central_operational_audit_records'"
    ).fetchone() is None:
        raise OperationalEvidenceUnavailable("v19 source evidence unavailable")
    append_committed_source_evidence(connection, **kwargs)  # type: ignore[arg-type]


def _append_committed_source_evidence(
    connection: sqlite3.Connection, *, org_id: str, receipt_id: str,
    command_digest: str, event_type: str, action: str, resource: SafeResourceRef,
    change: SafeChange, actor: str | None, occurred_at: str,
    policy_revision_id: str, policy_epoch: int, policy_digest: str,
    source: SourceReceiptProvenance,
) -> None:
    if not _REFERENCE.fullmatch(org_id) or not receipt_id or _SHA256.fullmatch(command_digest) is None:
        raise OperationalEvidenceUnavailable("invalid safe source evidence binding")
    if event_type not in _EVENT_TYPES:
        raise OperationalEvidenceUnavailable("unknown source event")
    if (
        not _REFERENCE.fullmatch(policy_revision_id)
        or type(policy_epoch) is not int or policy_epoch < 1
        or _SHA256.fullmatch(policy_digest) is None
    ):
        raise OperationalEvidenceUnavailable("actual source Authority required")
    audit_id = sha256(("operational-audit" + org_id + receipt_id + command_digest).encode()).hexdigest()
    event_id = sha256(("operational-event-id" + org_id + receipt_id + command_digest).encode()).hexdigest()
    intent_id = sha256(("operational-event" + org_id + receipt_id + audit_id + command_digest).encode()).hexdigest()
    change_json = _canonical(change.model_dump(mode="json", exclude_none=True))
    audit_values = (audit_id, org_id, occurred_at, "system" if actor is None else "user", actor, action,
                    resource.kind, resource.resource_id, "committed", command_digest, receipt_id,
                    policy_revision_id, policy_epoch, policy_digest, None, None, change.kind, change_json,
                    source.kind, source.receipt_key, source.receipt_digest)
    _insert_exact_or_fail(connection, "central_operational_audit_records", audit_values)
    payload = _safe_intent_payload(
        org_id=org_id, event_id=event_id, event_type=event_type,
        occurred_at=occurred_at, actor=actor, action=action, resource=resource,
        audit_id=audit_id, receipt_id=receipt_id, command_digest=command_digest,
        policy_revision_id=policy_revision_id, policy_epoch=policy_epoch,
        policy_digest=policy_digest, change=change, source=source,
    )
    payload_json = _canonical(payload)
    payload_digest = _digest(payload)
    intent_values = (intent_id, org_id, event_id, event_type, occurred_at, audit_id, receipt_id, payload_digest, payload_json, "pending", None, None, 0, None)
    _insert_exact_or_fail(connection, "central_operational_event_intents", intent_values)


def _insert_exact_or_fail(connection: sqlite3.Connection, table: str, values: tuple[object, ...]) -> None:
    columns = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
    if len(columns) != len(values):
        raise OperationalEvidenceUnavailable("operational evidence catalog drift")
    placeholders = ",".join("?" for _ in values)
    try:
        connection.execute(f"INSERT INTO {table} VALUES ({placeholders})", values)
        return
    except sqlite3.IntegrityError:
        primary = columns[0]
        row = connection.execute(f"SELECT * FROM {table} WHERE {primary}=?", (values[0],)).fetchone()
        if row is None or tuple(row) != values:
            raise OperationalEvidenceUnavailable("changed operational evidence replay")


def central_operational_evidence_schema_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        _validate_v19_read_connection(connection)
        return True
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


class OperationalEvidenceProjector:
    def __init__(self, database_path: Path, *, worker_id: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC), lease_duration: timedelta = timedelta(seconds=30), retention_count: int = 10_000) -> None:
        if not _REFERENCE.fullmatch(worker_id) or not 1000 <= retention_count <= 1_000_000:
            raise ValueError("invalid projector configuration")
        self._path, self._worker_id, self._clock, self._lease, self._retention = database_path, worker_id, clock, lease_duration, retention_count

    def drain(self) -> int:
        delivered = 0
        while self.project_one():
            delivered += 1
        return delivered

    def project_one(self) -> bool:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            now = _now_text(self._clock)
            until = (self._clock() + self._lease).isoformat().replace("+00:00", "Z")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM central_operational_event_intents WHERE status='pending' OR (status='leased' AND lease_until<?) ORDER BY occurred_at,intent_id LIMIT 1", (now,)).fetchone()
            if row is None:
                connection.commit()
                return False
            prior = connection.execute("SELECT cursor,payload_digest FROM central_operational_projection_receipts WHERE intent_id=?", (row["intent_id"],)).fetchone()
            if prior is not None:
                event = connection.execute("SELECT payload_digest FROM central_operational_events WHERE intent_id=?", (row["intent_id"],)).fetchone()
                if event is None or event["payload_digest"] != prior["payload_digest"]:
                    raise OperationalEvidenceUnavailable("changed projected intent")
                connection.execute("UPDATE central_operational_event_intents SET status='delivered',worker_id=?,lease_until=?,attempts=attempts+1,delivered_at=? WHERE intent_id=?", (self._worker_id, until, now, row["intent_id"]))
                connection.commit()
                return True
            connection.execute("UPDATE central_operational_event_intents SET status='leased',worker_id=?,lease_until=?,attempts=attempts+1 WHERE intent_id=?", (self._worker_id, until, row["intent_id"]))
            audit = connection.execute("SELECT * FROM central_operational_audit_records WHERE audit_id=?", (row["audit_id"],)).fetchone()
            if audit is None:
                raise OperationalEvidenceUnavailable("orphan operational intent")
            _validate_intent_binding(connection, row, audit)
            head = connection.execute("SELECT latest_cursor FROM central_operational_cursor_heads WHERE org_id=?", (row["org_id"],)).fetchone()
            cursor = 1 if head is None else int(head["latest_cursor"]) + 1
            payload = _event_from_rows(cursor, row, audit).model_dump(mode="json")
            payload_json = _canonical(payload)
            digest = _digest(payload)
            connection.execute("INSERT INTO central_operational_events VALUES (?,?,?,?,?,?,?,?,?,?)", (row["org_id"], cursor, row["event_id"], row["event_type"], row["occurred_at"], row["audit_id"], row["receipt_id"], payload_json, digest, row["intent_id"]))
            connection.execute("INSERT INTO central_operational_projection_receipts VALUES (?,?,?,?,?,?)", (row["intent_id"], row["org_id"], cursor, row["event_id"], digest, now))
            connection.execute("INSERT INTO central_operational_cursor_heads(org_id,latest_cursor) VALUES (?,?) ON CONFLICT(org_id) DO UPDATE SET latest_cursor=excluded.latest_cursor", (row["org_id"], cursor))
            connection.execute("UPDATE central_operational_event_intents SET status='delivered',delivered_at=? WHERE intent_id=?", (now, row["intent_id"]))
            self._prune(connection, row["org_id"], cursor, now)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _prune(self, connection: sqlite3.Connection, org_id: str, latest: int, now: str) -> None:
        floor = max(1, latest - self._retention + 1)
        old = connection.execute("SELECT count(*) FROM central_operational_events WHERE org_id=? AND cursor<?", (org_id, floor)).fetchone()[0]
        if not old:
            return
        through = floor - 1
        # Materialize the exact immutable pairs before deleting anything.  The
        # member table is both the retention authorization and the proof that
        # an audit, intent, receipt and event are pruned together.
        connection.execute(
            "INSERT INTO central_operational_retention_members(org_id,audit_id,intent_id) "
            "SELECT org_id,audit_id,intent_id FROM central_operational_events WHERE org_id=? AND cursor<=?",
            (org_id, through),
        )
        connection.execute("INSERT INTO central_operational_retention_authorizations VALUES (?,?)", (org_id, through))
        connection.execute("DELETE FROM central_operational_events WHERE org_id=? AND cursor<=?", (org_id, through))
        connection.execute("DELETE FROM central_operational_projection_receipts WHERE org_id=? AND intent_id IN (SELECT intent_id FROM central_operational_retention_members WHERE org_id=?)", (org_id, org_id))
        connection.execute("DELETE FROM central_operational_event_intents WHERE org_id=? AND intent_id IN (SELECT intent_id FROM central_operational_retention_members WHERE org_id=?)", (org_id, org_id))
        connection.execute("DELETE FROM central_operational_audit_records WHERE org_id=? AND audit_id IN (SELECT audit_id FROM central_operational_retention_members WHERE org_id=?)", (org_id, org_id))
        connection.execute("DELETE FROM central_operational_retention_members WHERE org_id=?", (org_id,))
        connection.execute("DELETE FROM central_operational_retention_authorizations WHERE org_id=?", (org_id,))
        digest = _digest({"org_id": org_id, "through_cursor": through, "retained_from_cursor": floor, "policy_count": self._retention})
        _insert_exact_or_fail(connection, "central_operational_retention_receipts", (f"retention:{org_id}:{through}", org_id, through, floor, self._retention, digest, now))


def _event_from_rows(cursor: int, intent: sqlite3.Row, audit: sqlite3.Row) -> OperationalEvent:
    actor: SystemActor | UserActor = SystemActor() if audit["actor_kind"] == "system" else UserActor(user_id=audit["actor_user_id"])
    return OperationalEvent(
        cursor=cursor, event_id=intent["event_id"], org_id=intent["org_id"], event_type=intent["event_type"],
        occurred_at=datetime.fromisoformat(intent["occurred_at"].replace("Z", "+00:00")), actor=actor,
        resource=SafeResourceRef(kind=audit["resource_kind"], resource_id=audit["resource_id"]), outcome=audit["outcome"],
        audit_id=audit["audit_id"], receipt_id=audit["receipt_id"], policy_epoch=audit["policy_epoch"], policy_digest=audit["policy_digest"],
        change=_SAFE_CHANGE_ADAPTER.validate_json(audit["change_json"]),
    )


class OperationalEvidenceReader:
    def __init__(self, database_path: Path) -> None: self._path = database_path

    def feed(self, org_id: str, last_event_id: int | None = None) -> tuple[OperationalEvent, ...]:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            _validate_v19_read_connection(connection)
            bounds = _bounds(connection, org_id)
            if last_event_id is not None and (last_event_id < bounds[0] - 1 or last_event_id > bounds[1]):
                raise OperationalEvidenceResyncRequired(*bounds)
            since_cursor = last_event_id if last_event_id is not None else bounds[1]
            rows = tuple(connection.execute("SELECT e.*,a.* FROM central_operational_events e JOIN central_operational_audit_records a ON a.audit_id=e.audit_id WHERE e.org_id=? AND e.cursor>? ORDER BY e.cursor", (org_id, since_cursor)))
            return tuple(_event_from_rows(row["cursor"], row, row) for row in rows)
        finally:
            connection.close()

    def audit_list(self, org_id: str, *, before_cursor: int | None = None, limit: int = 50) -> tuple[tuple[AuditRecordView, ...], int, int, int | None]:
        if not 1 <= limit <= 100:
            raise ValueError("invalid audit limit")
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            _validate_v19_read_connection(connection)
            oldest, latest = _bounds(connection, org_id)
            if before_cursor is not None and before_cursor < oldest:
                raise OperationalEvidenceResyncRequired(oldest, latest)
            upper = before_cursor if before_cursor is not None else latest + 1
            rows = tuple(connection.execute("SELECT e.cursor,a.* FROM central_operational_events e JOIN central_operational_audit_records a ON a.audit_id=e.audit_id WHERE e.org_id=? AND e.cursor<? ORDER BY e.cursor DESC LIMIT ?", (org_id, upper, limit)))
            views = tuple(AuditRecordView(cursor=row["cursor"], record=_audit_from_row(row)) for row in rows)
            next_before = views[-1].cursor if len(views) == limit else None
            return views, oldest, latest, next_before
        finally:
            connection.close()

    def audit_detail(self, org_id: str, audit_id: str) -> AuditRecordView | None:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            _validate_v19_read_connection(connection)
            row = connection.execute("SELECT e.cursor,a.* FROM central_operational_events e JOIN central_operational_audit_records a ON a.audit_id=e.audit_id WHERE e.org_id=? AND a.audit_id=?", (org_id, audit_id)).fetchone()
            return None if row is None else AuditRecordView(cursor=row["cursor"], record=_audit_from_row(row))
        finally:
            connection.close()


def _audit_from_row(row: sqlite3.Row) -> AuditRecord:
    actor: SystemActor | UserActor = SystemActor() if row["actor_kind"] == "system" else UserActor(user_id=row["actor_user_id"])
    evidence = None if row["approval_evidence_id"] is None else ApprovalEvidence(evidence_id=row["approval_evidence_id"], evidence_digest=row["approval_evidence_digest"])
    return AuditRecord(audit_id=row["audit_id"], org_id=row["org_id"], occurred_at=datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")), actor=actor, action=row["action"], resource=SafeResourceRef(kind=row["resource_kind"], resource_id=row["resource_id"]), outcome=row["outcome"], command_digest=row["command_digest"], receipt_id=row["receipt_id"], authority=AuditAuthority(policy_revision_id=row["policy_revision_id"], policy_epoch=row["policy_epoch"], policy_digest=row["policy_digest"]), approval_evidence=evidence, change=_SAFE_CHANGE_ADAPTER.validate_json(row["change_json"]))


def _bounds(connection: sqlite3.Connection, org_id: str) -> tuple[int, int]:
    row = connection.execute("SELECT min(cursor),max(cursor) FROM central_operational_events WHERE org_id=?", (org_id,)).fetchone()
    head = connection.execute("SELECT latest_cursor FROM central_operational_cursor_heads WHERE org_id=?", (org_id,)).fetchone()
    if row is None or row[1] is None:
        if head is not None and int(head["latest_cursor"]) > 0:
            raise OperationalEvidenceResyncRequired(1, int(head["latest_cursor"]))
        return 1, 0
    oldest, latest = int(row[0]), int(row[1])
    count = connection.execute("SELECT count(*) FROM central_operational_events WHERE org_id=?", (org_id,)).fetchone()[0]
    if latest - oldest + 1 != count:
        raise OperationalEvidenceResyncRequired(oldest, latest)
    return oldest, latest
