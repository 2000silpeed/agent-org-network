"""Durable BackupReview/Reevaluation producer foundation (RB3.2b.5-D1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from typing import Literal, Protocol, cast

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.central_operational_evidence import (
    BackupReviewChange,
    ReevaluationChange,
    SafeResourceRef,
    SourceReceiptProvenance,
    append_committed_source_evidence_if_v19,
    canonical_v19_file_authority,
    source_receipt_digest,
)
from agent_org_network.central_inbox_approval import (
    central_inbox_approval_schema_ready,
    validate_central_inbox_approval_connection,
)
from agent_org_network.central_question_lifecycle import (
    canonical_lifecycle_json,
    validate_central_question_lifecycle_connection,
)


ReviewSourceKind = Literal["backup_review", "reevaluation"]
ReviewAggregateState = Literal["open", "reviewed"]
ReviewReadAction = Literal[
    "backup_review.list",
    "backup_review.read",
    "backup_review.decide",
    "reevaluation.list",
    "reevaluation.read",
    "reevaluation.decide",
]
BackupReviewDispositionKind = Literal["approve", "dismiss", "correct"]
ReevaluationDispositionKind = Literal["acknowledge", "request_reanswer"]

_COMPONENT = "central-inbox-review"
_D1_VERSION = 17
_VERSION = 18
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

_INTENT_DDL = """CREATE TABLE central_inbox_review_outbox_intents (
 intent_id TEXT PRIMARY KEY NOT NULL,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('backup_review','reevaluation')),
 org_id TEXT NOT NULL, source_id TEXT NOT NULL, request_id TEXT NOT NULL,
 source_answer_record_id TEXT NOT NULL, producer_receipt_id TEXT NOT NULL,
 producer_receipt_digest TEXT NOT NULL CHECK(length(producer_receipt_digest)=64),
 frozen_request_revision INTEGER NOT NULL CHECK(frozen_request_revision>0),
 source_payload_digest TEXT NOT NULL CHECK(length(source_payload_digest)=64),
 source_timestamp TEXT NOT NULL, answering_card_id TEXT NOT NULL,
 answering_card_revision INTEGER NOT NULL CHECK(answering_card_revision>0),
 answering_card_digest TEXT NOT NULL CHECK(length(answering_card_digest)=64),
 owner_user_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','leased','delivered')),
 worker_id TEXT, lease_until TEXT, attempts INTEGER NOT NULL CHECK(attempts>=0),
 delivered_at TEXT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(source_answer_record_id) REFERENCES central_question_answer_records(record_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 UNIQUE(source_kind,source_id)
)"""

_BACKUP_DDL = """CREATE TABLE central_inbox_backup_reviews (
 review_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, request_id TEXT NOT NULL,
 source_answer_record_id TEXT NOT NULL UNIQUE, source_intent_id TEXT NOT NULL UNIQUE,
 producer_receipt_id TEXT NOT NULL, producer_receipt_digest TEXT NOT NULL CHECK(length(producer_receipt_digest)=64),
 answering_card_id TEXT NOT NULL, answering_card_revision INTEGER NOT NULL CHECK(answering_card_revision>0),
 answering_card_digest TEXT NOT NULL CHECK(length(answering_card_digest)=64),
 owner_user_id TEXT NOT NULL, source_answer_digest TEXT NOT NULL CHECK(length(source_answer_digest)=64),
 answered_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 state TEXT NOT NULL CHECK(state IN ('open','reviewed')), created_at TEXT NOT NULL,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(source_answer_record_id) REFERENCES central_question_answer_records(record_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(source_intent_id) REFERENCES central_inbox_review_outbox_intents(intent_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

_REEVALUATION_DDL = """CREATE TABLE central_inbox_reevaluations (
 reevaluation_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, request_id TEXT NOT NULL,
 feedback_id TEXT NOT NULL UNIQUE, source_answer_record_id TEXT NOT NULL,
 source_intent_id TEXT NOT NULL UNIQUE, producer_receipt_id TEXT NOT NULL,
 producer_receipt_digest TEXT NOT NULL CHECK(length(producer_receipt_digest)=64),
 answering_card_id TEXT NOT NULL, answering_card_revision INTEGER NOT NULL CHECK(answering_card_revision>0),
 answering_card_digest TEXT NOT NULL CHECK(length(answering_card_digest)=64),
 owner_user_id TEXT NOT NULL, feedback_payload_digest TEXT NOT NULL CHECK(length(feedback_payload_digest)=64),
 flagged_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 state TEXT NOT NULL CHECK(state IN ('open','reviewed')), created_at TEXT NOT NULL,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(feedback_id) REFERENCES central_question_feedback_records(feedback_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(source_answer_record_id) REFERENCES central_question_answer_records(record_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(source_intent_id) REFERENCES central_inbox_review_outbox_intents(intent_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

_PROJECTION_RECEIPT_DDL = """CREATE TABLE central_inbox_review_projection_receipts (
 projection_receipt_id TEXT PRIMARY KEY NOT NULL, intent_id TEXT NOT NULL UNIQUE,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('backup_review','reevaluation')),
 source_id TEXT NOT NULL, aggregate_id TEXT NOT NULL UNIQUE,
 producer_receipt_id TEXT NOT NULL, producer_receipt_digest TEXT NOT NULL CHECK(length(producer_receipt_digest)=64),
 projected_at TEXT NOT NULL,
 authority_policy_revision_id TEXT,
 authority_policy_epoch INTEGER,
 authority_policy_digest TEXT,
 FOREIGN KEY(intent_id) REFERENCES central_inbox_review_outbox_intents(intent_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 UNIQUE(source_kind,source_id)
)"""

_D1_MARKER_DDL = """CREATE TABLE central_inbox_review_component_schema (
 name TEXT PRIMARY KEY NOT NULL CHECK(name='central-inbox-review'),
 version INTEGER NOT NULL CHECK(version=17)
)"""

_TABLES = {
    "central_inbox_review_outbox_intents": _INTENT_DDL,
    "central_inbox_backup_reviews": _BACKUP_DDL,
    "central_inbox_reevaluations": _REEVALUATION_DDL,
    "central_inbox_review_projection_receipts": _PROJECTION_RECEIPT_DDL,
    "central_inbox_review_component_schema": _D1_MARKER_DDL,
}
_INDEXES = (
    "CREATE INDEX central_inbox_review_claimable ON "
    "central_inbox_review_outbox_intents(status,lease_until,source_timestamp,intent_id)",
    "CREATE INDEX central_inbox_backup_open_owner ON "
    "central_inbox_backup_reviews(org_id,owner_user_id,state,created_at,review_id)",
    "CREATE INDEX central_inbox_reevaluation_open_owner ON "
    "central_inbox_reevaluations(org_id,owner_user_id,state,created_at,reevaluation_id)",
)
_IMMUTABLE_TABLES = (
    "central_inbox_backup_reviews",
    "central_inbox_reevaluations",
    "central_inbox_review_projection_receipts",
)
_D1_TRIGGERS = tuple(
    statement
    for table in _IMMUTABLE_TABLES
    for statement in (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable inbox review row'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable inbox review row'); END",
    )
) + (
    """CREATE TRIGGER central_inbox_review_outbox_intents_frozen_fields
       BEFORE UPDATE ON central_inbox_review_outbox_intents
       WHEN NEW.intent_id!=OLD.intent_id OR NEW.source_kind!=OLD.source_kind
         OR NEW.org_id!=OLD.org_id OR NEW.source_id!=OLD.source_id
         OR NEW.request_id!=OLD.request_id
         OR NEW.source_answer_record_id!=OLD.source_answer_record_id
         OR NEW.producer_receipt_id!=OLD.producer_receipt_id
         OR NEW.producer_receipt_digest!=OLD.producer_receipt_digest
         OR NEW.frozen_request_revision!=OLD.frozen_request_revision
         OR NEW.source_payload_digest!=OLD.source_payload_digest
         OR NEW.source_timestamp!=OLD.source_timestamp
         OR NEW.answering_card_id!=OLD.answering_card_id
         OR NEW.answering_card_revision!=OLD.answering_card_revision
         OR NEW.answering_card_digest!=OLD.answering_card_digest
         OR NEW.owner_user_id!=OLD.owner_user_id
       BEGIN SELECT RAISE(ABORT,'immutable inbox review intent source'); END""",
    """CREATE TRIGGER central_inbox_review_outbox_intents_no_delete
       BEFORE DELETE ON central_inbox_review_outbox_intents
       BEGIN SELECT RAISE(ABORT,'immutable inbox review intent'); END""",
)

_D1_TABLES = dict(_TABLES)
_D1_INDEXES = _INDEXES
_D1_TABLES["central_inbox_review_projection_receipts"] = """CREATE TABLE central_inbox_review_projection_receipts (
 projection_receipt_id TEXT PRIMARY KEY NOT NULL, intent_id TEXT NOT NULL UNIQUE,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('backup_review','reevaluation')),
 source_id TEXT NOT NULL, aggregate_id TEXT NOT NULL UNIQUE,
 producer_receipt_id TEXT NOT NULL, producer_receipt_digest TEXT NOT NULL CHECK(length(producer_receipt_digest)=64),
 projected_at TEXT NOT NULL,
 FOREIGN KEY(intent_id) REFERENCES central_inbox_review_outbox_intents(intent_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 UNIQUE(source_kind,source_id)
)"""

_MARKER_DDL = """CREATE TABLE central_inbox_review_component_schema (
 name TEXT PRIMARY KEY NOT NULL CHECK(name='central-inbox-review'),
 version INTEGER NOT NULL CHECK(version=18)
)"""
_TABLES["central_inbox_review_component_schema"] = _MARKER_DDL

_BACKUP_HEAD_DDL = """CREATE TABLE central_inbox_backup_review_heads (
 review_id TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 state TEXT NOT NULL CHECK(state IN ('open','reviewed')),
 disposition_receipt_id TEXT UNIQUE,
 FOREIGN KEY(review_id) REFERENCES central_inbox_backup_reviews(review_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

_BACKUP_DISPOSITION_DDL = """CREATE TABLE central_inbox_backup_review_disposition_receipts (
 receipt_id TEXT PRIMARY KEY NOT NULL, review_id TEXT NOT NULL UNIQUE,
 org_id TEXT NOT NULL, actor_id TEXT NOT NULL, identity_session_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
 kind TEXT NOT NULL CHECK(kind IN ('approve','dismiss','correct')),
 rationale TEXT NOT NULL, corrected_text_digest TEXT,
 expected_revision INTEGER NOT NULL CHECK(expected_revision>0),
 resulting_revision INTEGER NOT NULL CHECK(resulting_revision>1),
 correction_record_id TEXT UNIQUE, answering_card_id TEXT NOT NULL,
 answering_card_revision INTEGER NOT NULL CHECK(answering_card_revision>0),
 answering_card_digest TEXT NOT NULL CHECK(length(answering_card_digest)=64),
 policy_version TEXT NOT NULL, policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),
 authority_policy_revision_id TEXT NOT NULL, authority_policy_epoch INTEGER NOT NULL CHECK(authority_policy_epoch>0),
 authority_policy_digest TEXT NOT NULL CHECK(length(authority_policy_digest)=64),
 created_at TEXT NOT NULL,
 FOREIGN KEY(review_id) REFERENCES central_inbox_backup_reviews(review_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 UNIQUE(org_id,actor_id,idempotency_key)
)"""

_BACKUP_AUDIT_DDL = """CREATE TABLE central_inbox_backup_review_disposition_audits (
 audit_id TEXT PRIMARY KEY NOT NULL, receipt_id TEXT NOT NULL UNIQUE,
 review_id TEXT NOT NULL UNIQUE, org_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
 kind TEXT NOT NULL CHECK(kind IN ('approve','dismiss','correct')),
 resulting_revision INTEGER NOT NULL CHECK(resulting_revision>1),
 correction_record_id TEXT UNIQUE, created_at TEXT NOT NULL,
 FOREIGN KEY(receipt_id) REFERENCES central_inbox_backup_review_disposition_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(review_id) REFERENCES central_inbox_backup_reviews(review_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

_CORRECTION_DDL = """CREATE TABLE central_inbox_answer_correction_records (
 correction_record_id TEXT PRIMARY KEY NOT NULL, review_id TEXT NOT NULL UNIQUE,
 request_id TEXT NOT NULL, supersedes_record_id TEXT NOT NULL UNIQUE,
 text TEXT NOT NULL, text_digest TEXT NOT NULL CHECK(length(text_digest)=64),
 mode TEXT NOT NULL CHECK(mode='full'), actor_id TEXT NOT NULL,
 answering_card_id TEXT NOT NULL,
 answering_card_revision INTEGER NOT NULL CHECK(answering_card_revision>0),
 answering_card_digest TEXT NOT NULL CHECK(length(answering_card_digest)=64),
 receipt_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
 FOREIGN KEY(review_id) REFERENCES central_inbox_backup_reviews(review_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(supersedes_record_id) REFERENCES central_question_answer_records(record_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(receipt_id) REFERENCES central_inbox_backup_review_disposition_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

_REEVALUATION_HEAD_DDL = """CREATE TABLE central_inbox_reevaluation_heads (
 reevaluation_id TEXT PRIMARY KEY NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0),
 state TEXT NOT NULL CHECK(state IN ('open','reviewed')),
 disposition_receipt_id TEXT UNIQUE,
 FOREIGN KEY(reevaluation_id) REFERENCES central_inbox_reevaluations(reevaluation_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

_REEVALUATION_DISPOSITION_DDL = """CREATE TABLE central_inbox_reevaluation_disposition_receipts (
 receipt_id TEXT PRIMARY KEY NOT NULL, reevaluation_id TEXT NOT NULL UNIQUE,
 org_id TEXT NOT NULL, actor_id TEXT NOT NULL, identity_session_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
 kind TEXT NOT NULL CHECK(kind IN ('acknowledge','request_reanswer')),
 rationale TEXT NOT NULL, expected_revision INTEGER NOT NULL CHECK(expected_revision>0),
 resulting_revision INTEGER NOT NULL CHECK(resulting_revision>1),
 reanswer_request_id TEXT UNIQUE, answering_card_id TEXT NOT NULL,
 answering_card_revision INTEGER NOT NULL CHECK(answering_card_revision>0),
 answering_card_digest TEXT NOT NULL CHECK(length(answering_card_digest)=64),
 policy_version TEXT NOT NULL, policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),
 authority_policy_revision_id TEXT NOT NULL, authority_policy_epoch INTEGER NOT NULL CHECK(authority_policy_epoch>0),
 authority_policy_digest TEXT NOT NULL CHECK(length(authority_policy_digest)=64),
 created_at TEXT NOT NULL,
 FOREIGN KEY(reevaluation_id) REFERENCES central_inbox_reevaluations(reevaluation_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 UNIQUE(org_id,actor_id,idempotency_key)
)"""

_REEVALUATION_AUDIT_DDL = """CREATE TABLE central_inbox_reevaluation_disposition_audits (
 audit_id TEXT PRIMARY KEY NOT NULL, receipt_id TEXT NOT NULL UNIQUE,
 reevaluation_id TEXT NOT NULL UNIQUE, org_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
 kind TEXT NOT NULL CHECK(kind IN ('acknowledge','request_reanswer')),
 resulting_revision INTEGER NOT NULL CHECK(resulting_revision>1),
 reanswer_request_id TEXT UNIQUE, created_at TEXT NOT NULL,
 FOREIGN KEY(receipt_id) REFERENCES central_inbox_reevaluation_disposition_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(reevaluation_id) REFERENCES central_inbox_reevaluations(reevaluation_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

_REANSWER_DDL = """CREATE TABLE central_inbox_reanswer_requested_records (
 reanswer_request_id TEXT PRIMARY KEY NOT NULL,
 reevaluation_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL,
 source_answer_record_id TEXT NOT NULL, feedback_id TEXT NOT NULL,
 rationale TEXT NOT NULL, actor_id TEXT NOT NULL,
 answering_card_id TEXT NOT NULL,
 answering_card_revision INTEGER NOT NULL CHECK(answering_card_revision>0),
 answering_card_digest TEXT NOT NULL CHECK(length(answering_card_digest)=64),
 receipt_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
 FOREIGN KEY(reevaluation_id) REFERENCES central_inbox_reevaluations(reevaluation_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(source_answer_record_id) REFERENCES central_question_answer_records(record_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(feedback_id) REFERENCES central_question_feedback_records(feedback_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 FOREIGN KEY(receipt_id) REFERENCES central_inbox_reevaluation_disposition_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

_TABLES.update(
    {
        "central_inbox_backup_review_heads": _BACKUP_HEAD_DDL,
        "central_inbox_backup_review_disposition_receipts": _BACKUP_DISPOSITION_DDL,
        "central_inbox_backup_review_disposition_audits": _BACKUP_AUDIT_DDL,
        "central_inbox_answer_correction_records": _CORRECTION_DDL,
        "central_inbox_reevaluation_heads": _REEVALUATION_HEAD_DDL,
        "central_inbox_reevaluation_disposition_receipts": _REEVALUATION_DISPOSITION_DDL,
        "central_inbox_reevaluation_disposition_audits": _REEVALUATION_AUDIT_DDL,
        "central_inbox_reanswer_requested_records": _REANSWER_DDL,
    }
)

_D2_IMMUTABLE_TABLES = (
    "central_inbox_backup_review_disposition_receipts",
    "central_inbox_backup_review_disposition_audits",
    "central_inbox_answer_correction_records",
    "central_inbox_reevaluation_disposition_receipts",
    "central_inbox_reevaluation_disposition_audits",
    "central_inbox_reanswer_requested_records",
)
_TRIGGERS = _D1_TRIGGERS + tuple(
    statement
    for table in _D2_IMMUTABLE_TABLES
    for statement in (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable inbox disposition row'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable inbox disposition row'); END",
    )
) + tuple(
    statement
    for table, key in (
        ("central_inbox_backup_review_heads", "review_id"),
        ("central_inbox_reevaluation_heads", "reevaluation_id"),
    )
    for statement in (
        f"""CREATE TRIGGER {table}_controlled_update BEFORE UPDATE ON {table}
        WHEN NEW.{key}!=OLD.{key} OR OLD.state!='open' OR NEW.state!='reviewed'
          OR NEW.revision!=OLD.revision+1 OR OLD.disposition_receipt_id IS NOT NULL
          OR NEW.disposition_receipt_id IS NULL
        BEGIN SELECT RAISE(ABORT,'invalid inbox disposition transition'); END""",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable inbox review head'); END",
    )
)


class ReviewInboxUnavailable(RuntimeError):
    pass


class ReviewInboxConflict(RuntimeError):
    pass


class ReviewSessionUnauthenticated(ReviewInboxUnavailable):
    pass


class ReviewInboxNotFound(RuntimeError):
    pass


class _ReviewBindingNotCurrent(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewReadCommand:
    identity_session_id: str
    expected_org_id: str
    expected_actor_id: str


@dataclass(frozen=True, slots=True)
class ReviewReadProof:
    principal: AuthenticatedPrincipal
    session_grant: AuthorizationGrant
    action_grant: AuthorizationGrant


@dataclass(frozen=True, slots=True)
class BackupReviewDispositionCommand:
    review_id: str
    identity_session_id: str
    expected_org_id: str
    expected_actor_id: str
    kind: BackupReviewDispositionKind
    rationale: str
    corrected_text: str | None
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class BackupReviewDispositionResult:
    receipt_id: str
    review_id: str
    revision: int
    state: Literal["reviewed"]
    correction_record_id: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReevaluationDispositionCommand:
    reevaluation_id: str
    identity_session_id: str
    expected_org_id: str
    expected_actor_id: str
    kind: ReevaluationDispositionKind
    rationale: str
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ReevaluationDispositionResult:
    receipt_id: str
    reevaluation_id: str
    revision: int
    state: Literal["reviewed"]
    reanswer_requested_id: str | None
    replayed: bool


class ReviewInboxAuthority(Protocol):
    def authorize_read(
        self,
        command: ReviewReadCommand,
        action: ReviewReadAction,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ReviewReadProof: ...

    def current_source_binding(
        self, row: sqlite3.Row, transaction: sqlite3.Connection
    ) -> bool: ...

    def authorize_disposition(
        self,
        command: BackupReviewDispositionCommand
        | ReevaluationDispositionCommand,
        action: Literal["backup_review.decide", "reevaluation.decide"],
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ReviewReadProof: ...

    def verify_disposition(
        self,
        proof: ReviewReadProof,
        command: BackupReviewDispositionCommand
        | ReevaluationDispositionCommand,
        action: Literal["backup_review.decide", "reevaluation.decide"],
        resource: ResourceRef,
        row: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class BackupReviewSummary:
    review_id: str
    request_id: str
    source_answer_record_id: str
    revision: int
    state: ReviewAggregateState
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BackupReviewDetail(BackupReviewSummary):
    question: str
    backup_answer_text: str
    answering_card_id: str
    answering_card_revision: int
    owner_user_id: str
    answered_at: datetime


@dataclass(frozen=True, slots=True)
class ReevaluationSummary:
    reevaluation_id: str
    request_id: str
    feedback_id: str
    source_answer_record_id: str
    revision: int
    state: ReviewAggregateState
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReevaluationDetail(ReevaluationSummary):
    question: str
    answer_text: str
    feedback_verdict: Literal["bad"]
    feedback_comment: str
    answering_card_id: str
    answering_card_revision: int
    owner_user_id: str
    flagged_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewOutboxClaim:
    intent_id: str
    source_kind: ReviewSourceKind
    source_id: str
    worker_id: str
    attempt: int
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class ReviewProjectionResult:
    aggregate_id: str
    source_kind: ReviewSourceKind
    source_id: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class _SourceEvidence:
    source_kind: ReviewSourceKind
    org_id: str
    source_id: str
    request_id: str
    source_answer_record_id: str
    producer_receipt_id: str
    producer_receipt_digest: str
    frozen_request_revision: int
    source_payload_digest: str
    source_timestamp: str
    answering_card_id: str
    answering_card_revision: int
    answering_card_digest: str
    owner_user_id: str

    @property
    def intent_id(self) -> str:
        return sha256(
            (
                f"{self.source_kind}|{self.org_id}|{self.source_id}|"
                f"{self.producer_receipt_digest}"
            ).encode()
        ).hexdigest()

    def values(self) -> tuple[object, ...]:
        return (
            self.intent_id,
            self.source_kind,
            self.org_id,
            self.source_id,
            self.request_id,
            self.source_answer_record_id,
            self.producer_receipt_id,
            self.producer_receipt_digest,
            self.frozen_request_revision,
            self.source_payload_digest,
            self.source_timestamp,
            self.answering_card_id,
            self.answering_card_revision,
            self.answering_card_digest,
            self.owner_user_id,
        )


def migrate_central_inbox_review_schema(
    path: Path,
    *,
    fault_injector: Callable[[str], None] = lambda _point: None,
) -> None:
    """Forward-only v16→v17 producer then v17→v18 disposition migration."""
    if not central_inbox_approval_schema_ready(path):
        raise ReviewInboxUnavailable()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        existing = _existing_owned(connection)
        if existing:
            if existing == set(_D1_TABLES):
                _upgrade_d1_companion_schema(
                    connection, fault_injector=fault_injector
                )
                _validate_catalog(connection)
                return
            if existing != set(_TABLES):
                raise ReviewInboxUnavailable()
            _validate_catalog(connection)
            return
        connection.execute("BEGIN IMMEDIATE")
        validate_central_question_lifecycle_connection(connection)
        validate_central_inbox_approval_connection(connection)
        for name, ddl in _D1_TABLES.items():
            if name != "central_inbox_review_component_schema":
                connection.execute(ddl)
        for ddl in _D1_INDEXES:
            connection.execute(ddl)
        for ddl in _D1_TRIGGERS:
            connection.execute(ddl)
        fault_injector("v16-to-v17-after-schema")
        for evidence in _all_eligible_evidence(connection):
            _insert_or_verify_intent(connection, evidence)
        fault_injector("v16-to-v17-after-backfill")
        connection.execute(
            _D1_TABLES["central_inbox_review_component_schema"]
        )
        fault_injector("v16-to-v17-before-marker")
        connection.execute(
            "INSERT INTO central_inbox_review_component_schema(name,version) "
            "VALUES (?,?)",
            (_COMPONENT, _D1_VERSION),
        )
        _validate_d1_catalog(connection)
        connection.commit()
        _upgrade_d1_companion_schema(
            connection, fault_injector=fault_injector
        )
        _validate_catalog(connection)
    except ReviewInboxUnavailable:
        connection.rollback()
        raise
    except Exception as error:
        connection.rollback()
        raise ReviewInboxUnavailable() from error
    finally:
        connection.close()


def _upgrade_d1_companion_schema(
    connection: sqlite3.Connection,
    *,
    fault_injector: Callable[[str], None],
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_d1_catalog(connection)
        for name, ddl in _TABLES.items():
            if name not in _D1_TABLES:
                connection.execute(ddl)
        projection_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(central_inbox_review_projection_receipts)"
            )
        }
        for name, ddl_type in (
            ("authority_policy_revision_id", "TEXT"),
            ("authority_policy_epoch", "INTEGER"),
            ("authority_policy_digest", "TEXT"),
        ):
            if name not in projection_columns:
                connection.execute(
                    "ALTER TABLE central_inbox_review_projection_receipts "
                    f"ADD COLUMN {name} {ddl_type}"
                )
        for ddl in _TRIGGERS[len(_D1_TRIGGERS) :]:
            connection.execute(ddl)
        connection.execute(
            "INSERT INTO central_inbox_backup_review_heads"
            "(review_id,revision,state,disposition_receipt_id) "
            "SELECT review_id,revision,state,NULL "
            "FROM central_inbox_backup_reviews"
        )
        connection.execute(
            "INSERT INTO central_inbox_reevaluation_heads"
            "(reevaluation_id,revision,state,disposition_receipt_id) "
            "SELECT reevaluation_id,revision,state,NULL "
            "FROM central_inbox_reevaluations"
        )
        fault_injector("v17-to-v18-before-marker")
        connection.execute(
            "DROP TABLE central_inbox_review_component_schema"
        )
        connection.execute(_MARKER_DDL)
        connection.execute(
            "INSERT INTO central_inbox_review_component_schema(name,version) "
            "VALUES (?,?)",
            (_COMPONENT, _VERSION),
        )
        fault_injector("v17-to-v18-after-marker")
        _validate_catalog(connection)
        connection.commit()
    except ReviewInboxUnavailable:
        connection.rollback()
        raise
    except Exception as error:
        connection.rollback()
        raise ReviewInboxUnavailable() from error


def central_inbox_review_schema_ready(path: Path) -> bool:
    if not path.is_file():
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


def validate_central_inbox_review_connection(
    connection: sqlite3.Connection,
) -> None:
    _validate_catalog(connection)


def append_backup_review_intent(
    transaction: sqlite3.Connection, record_id: str
) -> None:
    if not _installed(transaction):
        return
    _validate_owned_shape(transaction)
    evidence = _answer_evidence(transaction, record_id)
    if evidence is not None:
        _insert_or_verify_intent(transaction, evidence)


def verify_backup_review_intent(
    transaction: sqlite3.Connection, record_id: str
) -> None:
    if not _installed(transaction):
        return
    evidence = _answer_evidence(transaction, record_id)
    if evidence is not None:
        _require_exact_intent(transaction, evidence)


def append_reevaluation_intent(
    transaction: sqlite3.Connection, feedback_id: str
) -> None:
    if not _installed(transaction):
        return
    _validate_owned_shape(transaction)
    evidence = _feedback_evidence(transaction, feedback_id)
    if evidence is not None:
        _insert_or_verify_intent(transaction, evidence)


def verify_reevaluation_intent(
    transaction: sqlite3.Connection, feedback_id: str
) -> None:
    if not _installed(transaction):
        return
    evidence = _feedback_evidence(transaction, feedback_id)
    if evidence is not None:
        _require_exact_intent(transaction, evidence)


def _installed(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='central_inbox_review_component_schema'"
    ).fetchone() is not None


def _insert_or_verify_intent(
    connection: sqlite3.Connection, evidence: _SourceEvidence
) -> None:
    existing = connection.execute(
        "SELECT * FROM central_inbox_review_outbox_intents "
        "WHERE source_kind=? AND source_id=?",
        (evidence.source_kind, evidence.source_id),
    ).fetchone()
    if existing is not None:
        _require_exact_intent(connection, evidence)
        return
    connection.execute(
        "INSERT INTO central_inbox_review_outbox_intents"
        "(intent_id,source_kind,org_id,source_id,request_id,"
        "source_answer_record_id,producer_receipt_id,producer_receipt_digest,"
        "frozen_request_revision,source_payload_digest,source_timestamp,"
        "answering_card_id,answering_card_revision,answering_card_digest,"
        "owner_user_id,status,worker_id,lease_until,attempts,delivered_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,0,NULL)",
        evidence.values(),
    )


def _require_exact_intent(
    connection: sqlite3.Connection, evidence: _SourceEvidence
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM central_inbox_review_outbox_intents "
        "WHERE source_kind=? AND source_id=?",
        (evidence.source_kind, evidence.source_id),
    ).fetchone()
    if row is None or tuple(row)[:15] != evidence.values():
        raise ReviewInboxUnavailable()
    return row


def _all_eligible_evidence(
    connection: sqlite3.Connection,
) -> tuple[_SourceEvidence, ...]:
    evidence: list[_SourceEvidence] = []
    for row in connection.execute(
        "SELECT record_id FROM central_question_answer_records "
        "WHERE mode='backup' ORDER BY record_id"
    ):
        source = _answer_evidence(connection, str(row["record_id"]))
        if source is None:
            raise ReviewInboxUnavailable()
        evidence.append(source)
    for row in connection.execute(
        "SELECT feedback_id FROM central_question_feedback_records "
        "WHERE verdict='bad' ORDER BY feedback_id"
    ):
        source = _feedback_evidence(connection, str(row["feedback_id"]))
        if source is None:
            raise ReviewInboxUnavailable()
        evidence.append(source)
    return tuple(evidence)


def _answer_evidence(
    connection: sqlite3.Connection, record_id: str
) -> _SourceEvidence | None:
    record = connection.execute(
        "SELECT * FROM central_question_answer_records WHERE record_id=?",
        (record_id,),
    ).fetchone()
    if record is None:
        raise ReviewInboxUnavailable()
    if record["mode"] != "backup":
        return None
    return _answer_evidence_for_record(connection, record)


def _answer_evidence_for_record(
    connection: sqlite3.Connection,
    record: sqlite3.Row,
    *,
    frozen_card_digest: str | None = None,
) -> _SourceEvidence:
    ingest = tuple(
        connection.execute(
            "SELECT * FROM central_question_answer_ingest_receipts "
            "WHERE record_id=? AND result_kind='answered'",
            (record["record_id"],),
        )
    )
    disposition = tuple(
        connection.execute(
            "SELECT * FROM central_question_approval_disposition_receipts "
            "WHERE record_id=? AND terminal_kind='answered'",
            (record["record_id"],),
        )
    )
    if (len(ingest), len(disposition)) not in {(1, 0), (0, 1)}:
        raise ReviewInboxUnavailable()
    if ingest:
        receipt = ingest[0]
        audit = connection.execute(
            "SELECT * FROM central_question_answer_ingest_audits "
            "WHERE ticket_id=?",
            (receipt["ticket_id"],),
        ).fetchone()
        if (
            audit is None
            or audit["request_id"] != receipt["request_id"]
            or audit["receipt_ticket_id"] != receipt["ticket_id"]
            or audit["event_kind"] != "answered"
            or audit["created_at"] != receipt["created_at"]
        ):
            raise ReviewInboxUnavailable()
        producer_id = f"answer-ingest:{receipt['ticket_id']}"
        producer_digest = _evidence_digest("answer_ingest", receipt, audit)
        terminal_revision = int(receipt["expected_request_revision"]) + 1
        card_revision = int(receipt["binding_version"])
        timestamp = str(receipt["created_at"])
    else:
        receipt = disposition[0]
        audit = connection.execute(
            "SELECT * FROM central_question_approval_disposition_audits "
            "WHERE receipt_id=?",
            (receipt["receipt_id"],),
        ).fetchone()
        if (
            audit is None
            or audit["approval_item_id"] != receipt["approval_item_id"]
            or audit["request_id"] != receipt["request_id"]
            or audit["record_id"] != receipt["record_id"]
            or audit["created_at"] != receipt["created_at"]
        ):
            raise ReviewInboxUnavailable()
        producer_id = str(receipt["receipt_id"])
        producer_digest = _evidence_digest(
            "approval_disposition", receipt, audit
        )
        terminal_revision = int(receipt["terminal_request_revision"])
        card_revision = int(receipt["binding_version"])
        timestamp = str(receipt["created_at"])
    if (
        record["request_id"] != receipt["request_id"]
        or record["org_id"] != receipt["org_id"]
        or record["created_at"] != timestamp
        or card_revision < 1
    ):
        raise ReviewInboxUnavailable()
    frozen_digest = frozen_card_digest or _existing_card_digest(
        connection, "backup_review", str(record["record_id"])
    )
    card_digest = (
        frozen_digest
        if frozen_digest is not None
        else _current_card_digest(
            connection,
            str(record["org_id"]),
            str(record["agent_id"]),
            str(record["owner_id"]),
            card_revision,
        )
    )
    return _SourceEvidence(
        source_kind="backup_review",
        org_id=str(record["org_id"]),
        source_id=str(record["record_id"]),
        request_id=str(record["request_id"]),
        source_answer_record_id=str(record["record_id"]),
        producer_receipt_id=producer_id,
        producer_receipt_digest=producer_digest,
        frozen_request_revision=terminal_revision,
        source_payload_digest=_answer_payload_digest(record),
        source_timestamp=timestamp,
        answering_card_id=str(record["agent_id"]),
        answering_card_revision=card_revision,
        answering_card_digest=card_digest,
        owner_user_id=str(record["owner_id"]),
    )


def _feedback_evidence(
    connection: sqlite3.Connection, feedback_id: str
) -> _SourceEvidence | None:
    feedback = connection.execute(
        "SELECT * FROM central_question_feedback_records WHERE feedback_id=?",
        (feedback_id,),
    ).fetchone()
    if feedback is None:
        raise ReviewInboxUnavailable()
    if feedback["verdict"] != "bad":
        return None
    receipt = connection.execute(
        "SELECT * FROM central_question_feedback_receipts WHERE feedback_id=?",
        (feedback_id,),
    ).fetchone()
    audit = connection.execute(
        "SELECT * FROM central_question_feedback_audits WHERE feedback_id=?",
        (feedback_id,),
    ).fetchone()
    answer = connection.execute(
        "SELECT * FROM central_question_answer_records WHERE record_id=?",
        (feedback["record_id"],),
    ).fetchone()
    if (
        receipt is None
        or audit is None
        or answer is None
        or feedback["org_id"] != receipt["org_id"] != audit["org_id"]
        or feedback["request_id"] != receipt["request_id"] != audit["request_id"]
        or feedback["record_id"] != receipt["record_id"] != audit["record_id"]
        or feedback["payload_digest"]
        != receipt["payload_digest"]
        != audit["payload_digest"]
        or feedback["submitted_at"]
        != receipt["submitted_at"]
        != audit["submitted_at"]
        or receipt["receipt_id"] != audit["receipt_id"]
    ):
        raise ReviewInboxUnavailable()
    frozen_digest = _existing_card_digest(
        connection, "reevaluation", feedback_id
    )
    answer_source = _answer_evidence_for_record(
        connection,
        answer,
        frozen_card_digest=frozen_digest,
    )
    return _SourceEvidence(
        source_kind="reevaluation",
        org_id=str(feedback["org_id"]),
        source_id=feedback_id,
        request_id=str(feedback["request_id"]),
        source_answer_record_id=str(feedback["record_id"]),
        producer_receipt_id=str(receipt["receipt_id"]),
        producer_receipt_digest=_evidence_digest("feedback", receipt, audit),
        frozen_request_revision=answer_source.frozen_request_revision,
        source_payload_digest=str(feedback["payload_digest"]),
        source_timestamp=str(feedback["submitted_at"]),
        answering_card_id=answer_source.answering_card_id,
        answering_card_revision=answer_source.answering_card_revision,
        answering_card_digest=answer_source.answering_card_digest,
        owner_user_id=answer_source.owner_user_id,
    )


def _existing_card_digest(
    connection: sqlite3.Connection, source_kind: str, source_id: str
) -> str | None:
    if not _table_exists(connection, "central_inbox_review_outbox_intents"):
        return None
    row = connection.execute(
        "SELECT answering_card_digest FROM central_inbox_review_outbox_intents "
        "WHERE source_kind=? AND source_id=?",
        (source_kind, source_id),
    ).fetchone()
    if row is None:
        return None
    value = str(row[0])
    if _SHA256.fullmatch(value) is None:
        raise ReviewInboxUnavailable()
    return value


def _current_card_digest(
    connection: sqlite3.Connection,
    org_id: str,
    card_id: str,
    owner_id: str,
    revision: int,
) -> str:
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
        raise ReviewInboxUnavailable() from error
    if (
        row is None
        or row["owner_id"] != owner_id
        or int(row["revision"]) != revision
        or _SHA256.fullmatch(str(row["card_digest"])) is None
    ):
        raise _ReviewBindingNotCurrent()
    return str(row["card_digest"])


def _answer_payload_digest(record: sqlite3.Row) -> str:
    return sha256(
        canonical_lifecycle_json(
            {
                key: record[key]
                for key in (
                    "record_id",
                    "request_id",
                    "ticket_id",
                    "org_id",
                    "owner_id",
                    "agent_id",
                    "text",
                    "sources_json",
                    "mode",
                    "review_status",
                    "candidate_digest",
                    "created_at",
                )
            }
        ).encode()
    ).hexdigest()


def _evidence_digest(
    kind: str, receipt: sqlite3.Row, audit: sqlite3.Row
) -> str:
    return sha256(
        canonical_lifecycle_json(
            {
                "kind": kind,
                "receipt": {key: receipt[key] for key in receipt.keys()},
                "audit": {key: audit[key] for key in audit.keys()},
            }
        ).encode()
    ).hexdigest()


class ReviewOutboxProjector:
    def __init__(
        self,
        *,
        database_path: Path,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta,
        fault_injector: Callable[[str], None] = lambda _point: None,
    ) -> None:
        if not _valid_reference(worker_id) or lease_duration <= timedelta(0):
            raise ReviewInboxUnavailable()
        self._path = database_path
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._fault = fault_injector

    def claim_next(self) -> ReviewOutboxClaim | None:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_catalog(connection)
            now = self._now()
            row = connection.execute(
                "SELECT * FROM central_inbox_review_outbox_intents "
                "WHERE status='pending' OR "
                "(status='leased' AND lease_until<=?) "
                "ORDER BY source_timestamp,intent_id LIMIT 1",
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            lease_until = now + self._lease_duration
            changed = connection.execute(
                "UPDATE central_inbox_review_outbox_intents SET "
                "status='leased',worker_id=?,lease_until=?,attempts=attempts+1 "
                "WHERE intent_id=? AND (status='pending' OR "
                "(status='leased' AND lease_until<=?))",
                (
                    self._worker_id,
                    lease_until.isoformat(),
                    row["intent_id"],
                    now.isoformat(),
                ),
            )
            if changed.rowcount != 1:
                raise ReviewInboxUnavailable()
            claim = ReviewOutboxClaim(
                intent_id=str(row["intent_id"]),
                source_kind=cast(ReviewSourceKind, str(row["source_kind"])),
                source_id=str(row["source_id"]),
                worker_id=self._worker_id,
                attempt=int(row["attempts"]) + 1,
                lease_until=lease_until,
            )
            connection.commit()
            return claim
        except ReviewInboxUnavailable:
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ReviewInboxUnavailable() from error
        finally:
            connection.close()

    def has_claimable_intent(self) -> bool:
        connection = self._connection()
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            _validate_catalog(connection)
            row = connection.execute(
                "SELECT 1 FROM central_inbox_review_outbox_intents "
                "WHERE status='pending' OR "
                "(status='leased' AND lease_until<=?) LIMIT 1",
                (self._now().isoformat(),),
            ).fetchone()
            connection.commit()
            return row is not None
        except ReviewInboxUnavailable:
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ReviewInboxUnavailable() from error
        finally:
            connection.close()

    def project(self, claim: ReviewOutboxClaim) -> ReviewProjectionResult:
        if (
            type(claim) is not ReviewOutboxClaim
            or claim.worker_id != self._worker_id
        ):
            raise ReviewInboxUnavailable()
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_catalog(connection)
            now = self._now()
            intent = connection.execute(
                "SELECT * FROM central_inbox_review_outbox_intents "
                "WHERE intent_id=?",
                (claim.intent_id,),
            ).fetchone()
            if intent is None:
                raise ReviewInboxUnavailable()
            if intent["status"] == "delivered":
                evidence = _evidence_for_intent(connection, intent)
                _append_review_creation_evidence(
                    connection, evidence=evidence,
                    projection_receipt_id=_projection_receipt_id(
                        evidence.intent_id
                    ),
                )
                result = _projection_result(connection, intent, replayed=True)
                connection.commit()
                return result
            if (
                intent["status"] != "leased"
                or intent["worker_id"] != claim.worker_id
                or int(intent["attempts"]) != claim.attempt
                or intent["lease_until"] != claim.lease_until.isoformat()
                or claim.lease_until <= now
            ):
                raise ReviewInboxUnavailable()
            evidence = _evidence_for_intent(connection, intent)
            _require_exact_intent(connection, evidence)
            if _v19_enabled(connection):
                producer_operational_receipt = (
                    evidence.producer_receipt_id
                    if evidence.source_kind == "reevaluation"
                    or evidence.producer_receipt_id.startswith("answer-ingest:")
                    else f"answer-finalization:{evidence.producer_receipt_id}"
                )
                producer_audits = tuple(
                    connection.execute(
                        "SELECT policy_revision_id,policy_epoch,policy_digest "
                        "FROM central_operational_audit_records "
                        "WHERE org_id=? AND receipt_id=?",
                        (evidence.org_id, producer_operational_receipt),
                    )
                )
                if len(producer_audits) != 1:
                    raise ReviewInboxUnavailable()
                producer_authority = producer_audits[0]
            else:
                producer_authority = (None, None, None)
            aggregate_id = _aggregate_id(evidence)
            projection_receipt_id = _projection_receipt_id(evidence.intent_id)
            if evidence.source_kind == "backup_review":
                connection.execute(
                    "INSERT INTO central_inbox_backup_reviews"
                    "(review_id,org_id,request_id,source_answer_record_id,"
                    "source_intent_id,producer_receipt_id,producer_receipt_digest,"
                    "answering_card_id,answering_card_revision,"
                    "answering_card_digest,owner_user_id,source_answer_digest,"
                    "answered_at,revision,state,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,'open',?)",
                    (
                        aggregate_id,
                        evidence.org_id,
                        evidence.request_id,
                        evidence.source_answer_record_id,
                        evidence.intent_id,
                        evidence.producer_receipt_id,
                        evidence.producer_receipt_digest,
                        evidence.answering_card_id,
                        evidence.answering_card_revision,
                        evidence.answering_card_digest,
                        evidence.owner_user_id,
                        evidence.source_payload_digest,
                        evidence.source_timestamp,
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO central_inbox_backup_review_heads"
                    "(review_id,revision,state,disposition_receipt_id) "
                    "VALUES (?,1,'open',NULL)",
                    (aggregate_id,),
                )
            else:
                connection.execute(
                    "INSERT INTO central_inbox_reevaluations"
                    "(reevaluation_id,org_id,request_id,feedback_id,"
                    "source_answer_record_id,source_intent_id,"
                    "producer_receipt_id,producer_receipt_digest,"
                    "answering_card_id,answering_card_revision,"
                    "answering_card_digest,owner_user_id,"
                    "feedback_payload_digest,flagged_at,revision,state,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'open',?)",
                    (
                        aggregate_id,
                        evidence.org_id,
                        evidence.request_id,
                        evidence.source_id,
                        evidence.source_answer_record_id,
                        evidence.intent_id,
                        evidence.producer_receipt_id,
                        evidence.producer_receipt_digest,
                        evidence.answering_card_id,
                        evidence.answering_card_revision,
                        evidence.answering_card_digest,
                        evidence.owner_user_id,
                        evidence.source_payload_digest,
                        evidence.source_timestamp,
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO central_inbox_reevaluation_heads"
                    "(reevaluation_id,revision,state,disposition_receipt_id) "
                    "VALUES (?,1,'open',NULL)",
                    (aggregate_id,),
                )
            connection.execute(
                "INSERT INTO central_inbox_review_projection_receipts"
                "(projection_receipt_id,intent_id,source_kind,source_id,"
                "aggregate_id,producer_receipt_id,producer_receipt_digest,"
                "projected_at,authority_policy_revision_id,authority_policy_epoch,"
                "authority_policy_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    projection_receipt_id,
                    evidence.intent_id,
                    evidence.source_kind,
                    evidence.source_id,
                    aggregate_id,
                    evidence.producer_receipt_id,
                    evidence.producer_receipt_digest,
                    now.isoformat(),
                    (
                        str(producer_authority[0])
                        if producer_authority[0] is not None
                        else None
                    ),
                    (
                        int(producer_authority[1])
                        if producer_authority[1] is not None
                        else None
                    ),
                    (
                        str(producer_authority[2])
                        if producer_authority[2] is not None
                        else None
                    ),
                ),
            )
            _append_review_creation_evidence(
                connection, evidence=evidence,
                projection_receipt_id=projection_receipt_id,
            )
            changed = connection.execute(
                "UPDATE central_inbox_review_outbox_intents SET "
                "status='delivered',worker_id=NULL,lease_until=NULL,"
                "delivered_at=? WHERE intent_id=? AND status='leased' "
                "AND worker_id=? AND attempts=?",
                (
                    now.isoformat(),
                    evidence.intent_id,
                    claim.worker_id,
                    claim.attempt,
                ),
            )
            if changed.rowcount != 1:
                raise ReviewInboxUnavailable()
            self._fault("before-review-projection-commit")
            _validate_catalog(connection)
            connection.commit()
            return ReviewProjectionResult(
                aggregate_id=aggregate_id,
                source_kind=evidence.source_kind,
                source_id=evidence.source_id,
                replayed=False,
            )
        except ReviewInboxUnavailable:
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ReviewInboxUnavailable() from error
        finally:
            connection.close()

    def _connection(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self._path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except Exception as error:
            raise ReviewInboxUnavailable() from error

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ReviewInboxUnavailable()
        return value


class ReviewOutboxRecovery:
    def __init__(
        self, *, projector: ReviewOutboxProjector, maximum: int = 10000
    ) -> None:
        if maximum < 1:
            raise ReviewInboxUnavailable()
        self._projector = projector
        self._maximum = maximum

    def drain(self) -> int:
        projected = 0
        while projected < self._maximum:
            claim = self._projector.claim_next()
            if claim is None:
                return projected
            self._projector.project(claim)
            projected += 1
        if self._projector.has_claimable_intent():
            raise ReviewInboxUnavailable()
        return projected


def _aggregate_id(evidence: _SourceEvidence) -> str:
    prefix = (
        "backup-review"
        if evidence.source_kind == "backup_review"
        else "reevaluation"
    )
    return f"{prefix}:{sha256(evidence.intent_id.encode()).hexdigest()}"


def _projection_receipt_id(intent_id: str) -> str:
    return f"review-projection:{sha256(intent_id.encode()).hexdigest()}"


def _append_review_creation_evidence(
    connection: sqlite3.Connection, *, evidence: _SourceEvidence,
    projection_receipt_id: str,
) -> None:
    if not _v19_enabled(connection):
        return
    producer_operational_receipt = (
        evidence.producer_receipt_id
        if evidence.source_kind == "reevaluation"
        or evidence.producer_receipt_id.startswith("answer-ingest:")
        else f"answer-finalization:{evidence.producer_receipt_id}"
    )
    producer_audits = tuple(
        connection.execute(
            "SELECT policy_revision_id,policy_epoch,policy_digest "
            "FROM central_operational_audit_records "
            "WHERE org_id=? AND receipt_id=?",
            (evidence.org_id, producer_operational_receipt),
        )
    )
    if len(producer_audits) != 1:
        raise ReviewInboxUnavailable()
    producer_authority = producer_audits[0]
    aggregate_id = _aggregate_id(evidence)
    if evidence.source_kind == "backup_review":
        event_type = "backup_review_changed"
        action = "backup_review.create"
        resource = SafeResourceRef(
            kind="backup_review", resource_id=aggregate_id
        )
        change = BackupReviewChange(
            review_id=aggregate_id, request_id=evidence.request_id
        )
        source_kind = "backup_review_creation"
    else:
        event_type = "reevaluation_changed"
        action = "reevaluation.create"
        resource = SafeResourceRef(
            kind="reevaluation", resource_id=aggregate_id
        )
        change = ReevaluationChange(
            reevaluation_id=aggregate_id, request_id=evidence.request_id
        )
        source_kind = "reevaluation_creation"
    append_committed_source_evidence_if_v19(
        connection, org_id=evidence.org_id,
        receipt_id=projection_receipt_id,
        command_digest=evidence.producer_receipt_digest,
        event_type=event_type, action=action, resource=resource,
        change=change, actor_user_id=None,
        occurred_at=str(
            connection.execute(
                "SELECT projected_at FROM central_inbox_review_projection_receipts "
                "WHERE projection_receipt_id=?",
                (projection_receipt_id,),
            ).fetchone()[0]
        ),
        policy_revision_id=str(producer_authority[0]),
        policy_epoch=int(producer_authority[1]),
        policy_digest=str(producer_authority[2]),
        source=SourceReceiptProvenance(
            kind=source_kind, receipt_key=projection_receipt_id,
            receipt_digest=source_receipt_digest(
                connection, source_kind, evidence.org_id,
                projection_receipt_id,
            ),
        ),
    )


def _v19_enabled(connection: sqlite3.Connection) -> bool:
    marker = (
        connection.execute(
            "SELECT version FROM aon_installation_schema "
            "WHERE name='central-installation'"
        ).fetchone()
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='aon_installation_schema'"
        ).fetchone()
        else None
    )
    return marker is not None and int(marker[0]) >= 19


def _projection_result(
    connection: sqlite3.Connection,
    intent: sqlite3.Row,
    *,
    replayed: bool,
) -> ReviewProjectionResult:
    receipt = connection.execute(
        "SELECT aggregate_id FROM central_inbox_review_projection_receipts "
        "WHERE intent_id=?",
        (intent["intent_id"],),
    ).fetchone()
    if receipt is None:
        raise ReviewInboxUnavailable()
    return ReviewProjectionResult(
        aggregate_id=str(receipt["aggregate_id"]),
        source_kind=cast(ReviewSourceKind, str(intent["source_kind"])),
        source_id=str(intent["source_id"]),
        replayed=replayed,
    )


def _evidence_for_intent(
    connection: sqlite3.Connection, intent: sqlite3.Row
) -> _SourceEvidence:
    if intent["source_kind"] == "backup_review":
        evidence = _answer_evidence(
            connection, str(intent["source_answer_record_id"])
        )
    elif intent["source_kind"] == "reevaluation":
        evidence = _feedback_evidence(connection, str(intent["source_id"]))
    else:
        raise ReviewInboxUnavailable()
    if evidence is None:
        raise ReviewInboxUnavailable()
    return evidence


class ReviewInboxApplication:
    def __init__(
        self, *, database_path: Path, authority: ReviewInboxAuthority
    ) -> None:
        self._path = database_path
        self._authority = authority

    def list_backup_reviews(
        self, command: ReviewReadCommand
    ) -> tuple[BackupReviewSummary, ...]:
        rows = self._list(
            command,
            action="backup_review.list",
            resource_kind="backup_review_inbox",
            table="central_inbox_backup_reviews",
            head_table="central_inbox_backup_review_heads",
            id_column="review_id",
        )
        return tuple(_backup_summary(row) for row in rows)

    def backup_review_detail(
        self, command: ReviewReadCommand, review_id: str
    ) -> BackupReviewDetail | None:
        row = self._detail(
            command,
            aggregate_id=review_id,
            action="backup_review.read",
            resource_kind="backup_review",
            table="central_inbox_backup_reviews",
            id_column="review_id",
            head_table="central_inbox_backup_review_heads",
            include_feedback=False,
        )
        if row is None:
            return None
        summary = _backup_summary(row)
        return BackupReviewDetail(
            review_id=summary.review_id,
            request_id=summary.request_id,
            source_answer_record_id=summary.source_answer_record_id,
            revision=summary.revision,
            state=summary.state,
            created_at=summary.created_at,
            question=str(row["question"]),
            backup_answer_text=str(row["answer_text"]),
            answering_card_id=str(row["answering_card_id"]),
            answering_card_revision=int(row["answering_card_revision"]),
            owner_user_id=str(row["owner_user_id"]),
            answered_at=_timestamp(str(row["answered_at"])),
        )

    def list_reevaluations(
        self, command: ReviewReadCommand
    ) -> tuple[ReevaluationSummary, ...]:
        rows = self._list(
            command,
            action="reevaluation.list",
            resource_kind="reevaluation_inbox",
            table="central_inbox_reevaluations",
            head_table="central_inbox_reevaluation_heads",
            id_column="reevaluation_id",
        )
        return tuple(_reevaluation_summary(row) for row in rows)

    def reevaluation_detail(
        self, command: ReviewReadCommand, reevaluation_id: str
    ) -> ReevaluationDetail | None:
        row = self._detail(
            command,
            aggregate_id=reevaluation_id,
            action="reevaluation.read",
            resource_kind="reevaluation",
            table="central_inbox_reevaluations",
            id_column="reevaluation_id",
            head_table="central_inbox_reevaluation_heads",
            include_feedback=True,
        )
        if row is None:
            return None
        summary = _reevaluation_summary(row)
        return ReevaluationDetail(
            reevaluation_id=summary.reevaluation_id,
            request_id=summary.request_id,
            feedback_id=summary.feedback_id,
            source_answer_record_id=summary.source_answer_record_id,
            revision=summary.revision,
            state=summary.state,
            created_at=summary.created_at,
            question=str(row["question"]),
            answer_text=str(row["answer_text"]),
            feedback_verdict="bad",
            feedback_comment=str(row["feedback_comment"]),
            answering_card_id=str(row["answering_card_id"]),
            answering_card_revision=int(row["answering_card_revision"]),
            owner_user_id=str(row["owner_user_id"]),
            flagged_at=_timestamp(str(row["flagged_at"])),
        )

    def _list(
        self,
        command: ReviewReadCommand,
        *,
        action: Literal["backup_review.list", "reevaluation.list"],
        resource_kind: str,
        table: str,
        head_table: str,
        id_column: str,
    ) -> tuple[sqlite3.Row, ...]:
        _validate_read_command(command)
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            _validate_catalog(connection)
            resource = ResourceRef(
                org_id=command.expected_org_id,
                kind=resource_kind,
                resource_id=command.expected_actor_id,
                owner_subject_id=command.expected_actor_id,
            )
            proof = self._authority.authorize_read(
                command, action, resource, connection
            )
            _require_read_proof(proof, command, action, resource)
            rows = tuple(
                connection.execute(
                    f"SELECT a.*,h.revision AS current_revision,"
                    f"h.state AS current_state FROM {table} a "
                    f"JOIN {head_table} h ON h.{id_column}=a.{id_column} "
                    "WHERE a.org_id=? AND a.owner_user_id=? "
                    "AND h.state='open' ORDER BY a.created_at",
                    (proof.principal.org_id, proof.principal.subject_id),
                )
            )
            result = tuple(
                row
                for row in rows
                if self._authority.current_source_binding(row, connection)
            )
            connection.commit()
            return result
        except (ReviewInboxUnavailable, ReviewInboxNotFound):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ReviewInboxUnavailable() from error
        finally:
            connection.close()

    def _detail(
        self,
        command: ReviewReadCommand,
        *,
        aggregate_id: str,
        action: Literal["backup_review.read", "reevaluation.read"],
        resource_kind: str,
        table: str,
        id_column: str,
        head_table: str,
        include_feedback: bool,
    ) -> sqlite3.Row | None:
        _validate_read_command(command)
        if not _valid_reference(aggregate_id):
            return None
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            _validate_catalog(connection)
            feedback_join = (
                "LEFT JOIN central_question_feedback_records f "
                "ON f.feedback_id=a.feedback_id"
                if include_feedback
                else ""
            )
            feedback_column = (
                "f.comment AS feedback_comment"
                if include_feedback
                else "NULL AS feedback_comment"
            )
            row = connection.execute(
                f"SELECT a.*,h.revision AS current_revision,"
                f"h.state AS current_state,q.question,"
                f"r.text AS answer_text,{feedback_column} "
                f"FROM {table} a JOIN question_requests q USING(request_id) "
                f"JOIN {head_table} h ON h.{id_column}=a.{id_column} "
                "JOIN central_question_answer_records r "
                "ON r.record_id=a.source_answer_record_id "
                f"{feedback_join} WHERE a.{id_column}=?",
                (aggregate_id,),
            ).fetchone()
            if (
                row is None
                or row["org_id"] != command.expected_org_id
                or row["owner_user_id"] != command.expected_actor_id
                or row["current_state"] != "open"
            ):
                connection.commit()
                return None
            resource = ResourceRef(
                org_id=command.expected_org_id,
                kind=resource_kind,
                resource_id=aggregate_id,
                owner_subject_id=command.expected_actor_id,
            )
            proof = self._authority.authorize_read(
                command, action, resource, connection
            )
            _require_read_proof(proof, command, action, resource)
            if not self._authority.current_source_binding(row, connection):
                connection.commit()
                return None
            connection.commit()
            return row
        except (ReviewInboxUnavailable, ReviewInboxNotFound):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ReviewInboxUnavailable() from error
        finally:
            connection.close()

    def _connection(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                f"file:{self._path}?mode=rw", uri=True, timeout=5.0
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA query_only=ON")
            return connection
        except Exception as error:
            raise ReviewInboxUnavailable() from error


class BackupReviewDispositionApplication:
    def __init__(
        self,
        *,
        database_path: Path,
        authority: ReviewInboxAuthority,
        receipt_id_factory: Callable[[], str],
        correction_record_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
        fault_injector: Callable[[str], None] = lambda _point: None,
    ) -> None:
        self._path = database_path
        self._authority = authority
        self._receipt_id_factory = receipt_id_factory
        self._correction_id_factory = correction_record_id_factory
        self._clock = clock
        self._fault = fault_injector

    def dispose(
        self, command: BackupReviewDispositionCommand
    ) -> BackupReviewDispositionResult:
        _validate_backup_disposition(command)
        digest = _backup_disposition_digest(command)
        connection = _write_connection(self._path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_catalog(connection)
            row = connection.execute(
                "SELECT a.*,h.revision AS current_revision,"
                "h.state AS current_state,h.disposition_receipt_id "
                "FROM central_inbox_backup_reviews a "
                "JOIN central_inbox_backup_review_heads h USING(review_id) "
                "WHERE a.review_id=?",
                (command.review_id,),
            ).fetchone()
            if (
                row is None
                or row["org_id"] != command.expected_org_id
                or row["owner_user_id"] != command.expected_actor_id
            ):
                raise ReviewInboxNotFound()
            resource = ResourceRef(
                org_id=command.expected_org_id,
                kind="backup_review",
                resource_id=command.review_id,
                owner_subject_id=command.expected_actor_id,
            )
            proof = self._authority.authorize_disposition(
                command, "backup_review.decide", resource, connection
            )
            _require_read_proof(
                proof,
                _read_command(command),
                "backup_review.decide",
                resource,
            )
            if not self._authority.current_source_binding(row, connection):
                raise ReviewInboxNotFound()
            replay = connection.execute(
                "SELECT * FROM central_inbox_backup_review_disposition_receipts "
                "WHERE org_id=? AND actor_id=? AND idempotency_key=?",
                (
                    command.expected_org_id,
                    command.expected_actor_id,
                    command.idempotency_key,
                ),
            ).fetchone()
            if replay is not None:
                if (
                    replay["review_id"] != command.review_id
                    or replay["command_digest"] != digest
                ):
                    raise ReviewInboxConflict()
                _verify_disposition_authority(
                    self._authority,
                    proof,
                    command,
                    "backup_review.decide",
                    resource,
                    row,
                    connection,
                )
                replay_authority = canonical_v19_file_authority(
                    source_policy_digest=str(replay["authority_policy_digest"]),
                    current_snapshot_digest=proof.action_grant.policy_digest,
                )
                append_committed_source_evidence_if_v19(
                    connection, org_id=command.expected_org_id,
                    receipt_id=str(replay["receipt_id"]),
                    command_digest=str(replay["command_digest"]),
                    event_type="backup_review_changed",
                    action="backup_review.decide",
                    resource=SafeResourceRef(
                        kind="backup_review", resource_id=command.review_id
                    ),
                    change=BackupReviewChange(
                        review_id=command.review_id,
                        request_id=str(row["request_id"]),
                    ),
                    actor_user_id=command.expected_actor_id,
                    occurred_at=str(replay["created_at"]),
                    policy_revision_id=replay_authority.policy_revision_id,
                    policy_epoch=replay_authority.policy_epoch,
                    policy_digest=replay_authority.policy_digest,
                    source=SourceReceiptProvenance(
                        kind="backup_review_disposition",
                        receipt_key=str(replay["receipt_id"]),
                        receipt_digest=source_receipt_digest(
                            connection, "backup_review_disposition",
                            command.expected_org_id,
                            str(replay["receipt_id"]),
                        ),
                    ),
                )
                result = _backup_disposition_result(replay, replayed=True)
                connection.commit()
                return result
            if (
                row["current_state"] != "open"
                or int(row["current_revision"]) != command.expected_revision
            ):
                raise ReviewInboxConflict()
            receipt_id = self._receipt_id_factory()
            correction_id = (
                self._correction_id_factory()
                if command.kind == "correct"
                else None
            )
            if not _valid_reference(receipt_id) or (
                correction_id is not None and not _valid_reference(correction_id)
            ):
                raise ReviewInboxUnavailable()
            at = _aware_now(self._clock)
            audit_authority = canonical_v19_file_authority(
                source_policy_digest=proof.action_grant.policy_digest,
                current_snapshot_digest=proof.action_grant.policy_digest,
            )
            corrected_digest = (
                sha256(command.corrected_text.encode("utf-8")).hexdigest()
                if command.corrected_text is not None
                else None
            )
            connection.execute(
                "INSERT INTO central_inbox_backup_review_disposition_receipts"
                "(receipt_id,review_id,org_id,actor_id,identity_session_id,"
                "idempotency_key,command_digest,kind,rationale,"
                "corrected_text_digest,expected_revision,resulting_revision,"
                "correction_record_id,answering_card_id,"
                "answering_card_revision,answering_card_digest,"
                "policy_version,policy_digest,authority_policy_revision_id,"
                "authority_policy_epoch,authority_policy_digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    command.review_id,
                    command.expected_org_id,
                    command.expected_actor_id,
                    command.identity_session_id,
                    command.idempotency_key,
                    digest,
                    command.kind,
                    command.rationale,
                    corrected_digest,
                    command.expected_revision,
                    command.expected_revision + 1,
                    correction_id,
                    row["answering_card_id"],
                    row["answering_card_revision"],
                    row["answering_card_digest"],
                    proof.action_grant.policy_version,
                    proof.action_grant.policy_digest,
                    audit_authority.policy_revision_id,
                    audit_authority.policy_epoch,
                    audit_authority.policy_digest,
                    at,
                ),
            )
            if correction_id is not None:
                connection.execute(
                    "INSERT INTO central_inbox_answer_correction_records"
                    "(correction_record_id,review_id,request_id,"
                    "supersedes_record_id,text,text_digest,mode,actor_id,"
                    "answering_card_id,answering_card_revision,"
                    "answering_card_digest,receipt_id,created_at) "
                    "VALUES (?,?,?,?,?,?,'full',?,?,?,?,?,?)",
                    (
                        correction_id,
                        command.review_id,
                        row["request_id"],
                        row["source_answer_record_id"],
                        command.corrected_text,
                        corrected_digest,
                        command.expected_actor_id,
                        row["answering_card_id"],
                        row["answering_card_revision"],
                        row["answering_card_digest"],
                        receipt_id,
                        at,
                    ),
                )
            audit_id = _audit_id("backup-disposition", receipt_id)
            connection.execute(
                "INSERT INTO central_inbox_backup_review_disposition_audits"
                "(audit_id,receipt_id,review_id,org_id,actor_id,"
                "command_digest,kind,resulting_revision,"
                "correction_record_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    audit_id,
                    receipt_id,
                    command.review_id,
                    command.expected_org_id,
                    command.expected_actor_id,
                    digest,
                    command.kind,
                    command.expected_revision + 1,
                    correction_id,
                    at,
                ),
            )
            changed = connection.execute(
                "UPDATE central_inbox_backup_review_heads SET "
                "revision=?,state='reviewed',disposition_receipt_id=? "
                "WHERE review_id=? AND revision=? AND state='open' "
                "AND disposition_receipt_id IS NULL",
                (
                    command.expected_revision + 1,
                    receipt_id,
                    command.review_id,
                    command.expected_revision,
                ),
            )
            if changed.rowcount != 1:
                raise ReviewInboxConflict()
            audit_authority = canonical_v19_file_authority(
                source_policy_digest=proof.action_grant.policy_digest,
                current_snapshot_digest=proof.action_grant.policy_digest,
            )
            append_committed_source_evidence_if_v19(
                connection, org_id=command.expected_org_id, receipt_id=receipt_id,
                command_digest=digest, event_type="backup_review_changed",
                action="backup_review.decide",
                resource=SafeResourceRef(kind="backup_review", resource_id=command.review_id),
                change=BackupReviewChange(review_id=command.review_id, request_id=str(row["request_id"])),
                actor_user_id=command.expected_actor_id, occurred_at=at,
                policy_revision_id=audit_authority.policy_revision_id,
                policy_epoch=audit_authority.policy_epoch,
                policy_digest=audit_authority.policy_digest,
                source=SourceReceiptProvenance(
                    kind="backup_review_disposition",
                    receipt_key=receipt_id,
                    receipt_digest=source_receipt_digest(
                        connection,
                        "backup_review_disposition",
                        command.expected_org_id,
                        receipt_id,
                    ),
                ),
            )
            self._fault("before-backup-review-disposition-commit")
            _verify_disposition_authority(
                self._authority,
                proof,
                command,
                "backup_review.decide",
                resource,
                row,
                connection,
            )
            _validate_catalog(connection)
            connection.commit()
            return BackupReviewDispositionResult(
                receipt_id=receipt_id,
                review_id=command.review_id,
                revision=command.expected_revision + 1,
                state="reviewed",
                correction_record_id=correction_id,
                replayed=False,
            )
        except (ReviewInboxConflict, ReviewInboxNotFound, ReviewInboxUnavailable):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ReviewInboxUnavailable() from error
        finally:
            connection.close()


class ReevaluationDispositionApplication:
    def __init__(
        self,
        *,
        database_path: Path,
        authority: ReviewInboxAuthority,
        receipt_id_factory: Callable[[], str],
        reanswer_request_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
        fault_injector: Callable[[str], None] = lambda _point: None,
    ) -> None:
        self._path = database_path
        self._authority = authority
        self._receipt_id_factory = receipt_id_factory
        self._reanswer_id_factory = reanswer_request_id_factory
        self._clock = clock
        self._fault = fault_injector

    def dispose(
        self, command: ReevaluationDispositionCommand
    ) -> ReevaluationDispositionResult:
        _validate_reevaluation_disposition(command)
        digest = _reevaluation_disposition_digest(command)
        connection = _write_connection(self._path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_catalog(connection)
            row = connection.execute(
                "SELECT a.*,h.revision AS current_revision,"
                "h.state AS current_state,h.disposition_receipt_id "
                "FROM central_inbox_reevaluations a "
                "JOIN central_inbox_reevaluation_heads h "
                "USING(reevaluation_id) WHERE a.reevaluation_id=?",
                (command.reevaluation_id,),
            ).fetchone()
            if (
                row is None
                or row["org_id"] != command.expected_org_id
                or row["owner_user_id"] != command.expected_actor_id
            ):
                raise ReviewInboxNotFound()
            resource = ResourceRef(
                org_id=command.expected_org_id,
                kind="reevaluation",
                resource_id=command.reevaluation_id,
                owner_subject_id=command.expected_actor_id,
            )
            proof = self._authority.authorize_disposition(
                command, "reevaluation.decide", resource, connection
            )
            _require_read_proof(
                proof,
                _read_command(command),
                "reevaluation.decide",
                resource,
            )
            if not self._authority.current_source_binding(row, connection):
                raise ReviewInboxNotFound()
            replay = connection.execute(
                "SELECT * FROM central_inbox_reevaluation_disposition_receipts "
                "WHERE org_id=? AND actor_id=? AND idempotency_key=?",
                (
                    command.expected_org_id,
                    command.expected_actor_id,
                    command.idempotency_key,
                ),
            ).fetchone()
            if replay is not None:
                if (
                    replay["reevaluation_id"] != command.reevaluation_id
                    or replay["command_digest"] != digest
                ):
                    raise ReviewInboxConflict()
                _verify_disposition_authority(
                    self._authority,
                    proof,
                    command,
                    "reevaluation.decide",
                    resource,
                    row,
                    connection,
                )
                replay_authority = canonical_v19_file_authority(
                    source_policy_digest=str(replay["authority_policy_digest"]),
                    current_snapshot_digest=proof.action_grant.policy_digest,
                )
                append_committed_source_evidence_if_v19(
                    connection, org_id=command.expected_org_id,
                    receipt_id=str(replay["receipt_id"]),
                    command_digest=str(replay["command_digest"]),
                    event_type="reevaluation_changed",
                    action="reevaluation.decide",
                    resource=SafeResourceRef(
                        kind="reevaluation",
                        resource_id=command.reevaluation_id,
                    ),
                    change=ReevaluationChange(
                        reevaluation_id=command.reevaluation_id,
                        request_id=str(row["request_id"]),
                    ),
                    actor_user_id=command.expected_actor_id,
                    occurred_at=str(replay["created_at"]),
                    policy_revision_id=replay_authority.policy_revision_id,
                    policy_epoch=replay_authority.policy_epoch,
                    policy_digest=replay_authority.policy_digest,
                    source=SourceReceiptProvenance(
                        kind="reevaluation_disposition",
                        receipt_key=str(replay["receipt_id"]),
                        receipt_digest=source_receipt_digest(
                            connection, "reevaluation_disposition",
                            command.expected_org_id,
                            str(replay["receipt_id"]),
                        ),
                    ),
                )
                result = _reevaluation_disposition_result(
                    replay, replayed=True
                )
                connection.commit()
                return result
            if (
                row["current_state"] != "open"
                or int(row["current_revision"]) != command.expected_revision
            ):
                raise ReviewInboxConflict()
            receipt_id = self._receipt_id_factory()
            reanswer_id = (
                self._reanswer_id_factory()
                if command.kind == "request_reanswer"
                else None
            )
            if not _valid_reference(receipt_id) or (
                reanswer_id is not None and not _valid_reference(reanswer_id)
            ):
                raise ReviewInboxUnavailable()
            at = _aware_now(self._clock)
            audit_authority = canonical_v19_file_authority(
                source_policy_digest=proof.action_grant.policy_digest,
                current_snapshot_digest=proof.action_grant.policy_digest,
            )
            connection.execute(
                "INSERT INTO central_inbox_reevaluation_disposition_receipts"
                "(receipt_id,reevaluation_id,org_id,actor_id,"
                "identity_session_id,idempotency_key,command_digest,kind,"
                "rationale,expected_revision,resulting_revision,"
                "reanswer_request_id,answering_card_id,"
                "answering_card_revision,answering_card_digest,"
                "policy_version,policy_digest,authority_policy_revision_id,"
                "authority_policy_epoch,authority_policy_digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    command.reevaluation_id,
                    command.expected_org_id,
                    command.expected_actor_id,
                    command.identity_session_id,
                    command.idempotency_key,
                    digest,
                    command.kind,
                    command.rationale,
                    command.expected_revision,
                    command.expected_revision + 1,
                    reanswer_id,
                    row["answering_card_id"],
                    row["answering_card_revision"],
                    row["answering_card_digest"],
                    proof.action_grant.policy_version,
                    proof.action_grant.policy_digest,
                    audit_authority.policy_revision_id,
                    audit_authority.policy_epoch,
                    audit_authority.policy_digest,
                    at,
                ),
            )
            if reanswer_id is not None:
                connection.execute(
                    "INSERT INTO central_inbox_reanswer_requested_records"
                    "(reanswer_request_id,reevaluation_id,request_id,"
                    "source_answer_record_id,feedback_id,rationale,actor_id,"
                    "answering_card_id,answering_card_revision,"
                    "answering_card_digest,receipt_id,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        reanswer_id,
                        command.reevaluation_id,
                        row["request_id"],
                        row["source_answer_record_id"],
                        row["feedback_id"],
                        command.rationale,
                        command.expected_actor_id,
                        row["answering_card_id"],
                        row["answering_card_revision"],
                        row["answering_card_digest"],
                        receipt_id,
                        at,
                    ),
                )
            audit_id = _audit_id("reevaluation-disposition", receipt_id)
            connection.execute(
                "INSERT INTO central_inbox_reevaluation_disposition_audits"
                "(audit_id,receipt_id,reevaluation_id,org_id,actor_id,"
                "command_digest,kind,resulting_revision,"
                "reanswer_request_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    audit_id,
                    receipt_id,
                    command.reevaluation_id,
                    command.expected_org_id,
                    command.expected_actor_id,
                    digest,
                    command.kind,
                    command.expected_revision + 1,
                    reanswer_id,
                    at,
                ),
            )
            changed = connection.execute(
                "UPDATE central_inbox_reevaluation_heads SET "
                "revision=?,state='reviewed',disposition_receipt_id=? "
                "WHERE reevaluation_id=? AND revision=? AND state='open' "
                "AND disposition_receipt_id IS NULL",
                (
                    command.expected_revision + 1,
                    receipt_id,
                    command.reevaluation_id,
                    command.expected_revision,
                ),
            )
            if changed.rowcount != 1:
                raise ReviewInboxConflict()
            audit_authority = canonical_v19_file_authority(
                source_policy_digest=proof.action_grant.policy_digest,
                current_snapshot_digest=proof.action_grant.policy_digest,
            )
            append_committed_source_evidence_if_v19(
                connection, org_id=command.expected_org_id, receipt_id=receipt_id,
                command_digest=digest, event_type="reevaluation_changed",
                action="reevaluation.decide",
                resource=SafeResourceRef(kind="reevaluation", resource_id=command.reevaluation_id),
                change=ReevaluationChange(reevaluation_id=command.reevaluation_id, request_id=str(row["request_id"])),
                actor_user_id=command.expected_actor_id, occurred_at=at,
                policy_revision_id=audit_authority.policy_revision_id,
                policy_epoch=audit_authority.policy_epoch,
                policy_digest=audit_authority.policy_digest,
                source=SourceReceiptProvenance(
                    kind="reevaluation_disposition",
                    receipt_key=receipt_id,
                    receipt_digest=source_receipt_digest(
                        connection,
                        "reevaluation_disposition",
                        command.expected_org_id,
                        receipt_id,
                    ),
                ),
            )
            self._fault("before-reevaluation-disposition-commit")
            _verify_disposition_authority(
                self._authority,
                proof,
                command,
                "reevaluation.decide",
                resource,
                row,
                connection,
            )
            _validate_catalog(connection)
            connection.commit()
            return ReevaluationDispositionResult(
                receipt_id=receipt_id,
                reevaluation_id=command.reevaluation_id,
                revision=command.expected_revision + 1,
                state="reviewed",
                reanswer_requested_id=reanswer_id,
                replayed=False,
            )
        except (ReviewInboxConflict, ReviewInboxNotFound, ReviewInboxUnavailable):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ReviewInboxUnavailable() from error
        finally:
            connection.close()


def _validate_backup_disposition(
    command: BackupReviewDispositionCommand,
) -> None:
    if (
        type(command) is not BackupReviewDispositionCommand
        or not _valid_reference(command.review_id)
        or _SHA256.fullmatch(command.identity_session_id) is None
        or not _valid_reference(command.expected_org_id)
        or not _valid_reference(command.expected_actor_id)
        or command.kind not in ("approve", "dismiss", "correct")
        or not _valid_command_text(command.rationale, maximum=4096)
        or type(command.expected_revision) is not int
        or command.expected_revision < 1
        or not _valid_reference(command.idempotency_key)
        or (
            command.kind == "correct"
            and not _valid_command_text(
                command.corrected_text, maximum=65536
            )
        )
        or (
            command.kind != "correct"
            and command.corrected_text is not None
        )
    ):
        raise ReviewInboxUnavailable()


def _validate_reevaluation_disposition(
    command: ReevaluationDispositionCommand,
) -> None:
    if (
        type(command) is not ReevaluationDispositionCommand
        or not _valid_reference(command.reevaluation_id)
        or _SHA256.fullmatch(command.identity_session_id) is None
        or not _valid_reference(command.expected_org_id)
        or not _valid_reference(command.expected_actor_id)
        or command.kind not in ("acknowledge", "request_reanswer")
        or not _valid_command_text(command.rationale, maximum=4096)
        or type(command.expected_revision) is not int
        or command.expected_revision < 1
        or not _valid_reference(command.idempotency_key)
    ):
        raise ReviewInboxUnavailable()


def _valid_command_text(value: object, *, maximum: int) -> bool:
    if type(value) is not str or value == "":
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


def _backup_disposition_digest(
    command: BackupReviewDispositionCommand,
) -> str:
    return sha256(
        canonical_lifecycle_json(
            {
                "review_id": command.review_id,
                "identity_session_id": command.identity_session_id,
                "expected_org_id": command.expected_org_id,
                "expected_actor_id": command.expected_actor_id,
                "kind": command.kind,
                "rationale": command.rationale,
                "corrected_text": command.corrected_text,
                "expected_revision": command.expected_revision,
                "idempotency_key": command.idempotency_key,
            }
        ).encode()
    ).hexdigest()


def _reevaluation_disposition_digest(
    command: ReevaluationDispositionCommand,
) -> str:
    return sha256(
        canonical_lifecycle_json(
            {
                "reevaluation_id": command.reevaluation_id,
                "identity_session_id": command.identity_session_id,
                "expected_org_id": command.expected_org_id,
                "expected_actor_id": command.expected_actor_id,
                "kind": command.kind,
                "rationale": command.rationale,
                "expected_revision": command.expected_revision,
                "idempotency_key": command.idempotency_key,
            }
        ).encode()
    ).hexdigest()


def _read_command(
    command: BackupReviewDispositionCommand
    | ReevaluationDispositionCommand,
) -> ReviewReadCommand:
    return ReviewReadCommand(
        identity_session_id=command.identity_session_id,
        expected_org_id=command.expected_org_id,
        expected_actor_id=command.expected_actor_id,
    )


def _verify_disposition_authority(
    authority: ReviewInboxAuthority,
    proof: ReviewReadProof,
    command: BackupReviewDispositionCommand
    | ReevaluationDispositionCommand,
    action: Literal["backup_review.decide", "reevaluation.decide"],
    resource: ResourceRef,
    row: sqlite3.Row,
    transaction: sqlite3.Connection,
) -> None:
    if not authority.verify_disposition(
        proof, command, action, resource, row, transaction
    ):
        raise ReviewInboxNotFound()


def _backup_disposition_result(
    row: sqlite3.Row, *, replayed: bool
) -> BackupReviewDispositionResult:
    return BackupReviewDispositionResult(
        receipt_id=str(row["receipt_id"]),
        review_id=str(row["review_id"]),
        revision=int(row["resulting_revision"]),
        state="reviewed",
        correction_record_id=(
            str(row["correction_record_id"])
            if row["correction_record_id"] is not None
            else None
        ),
        replayed=replayed,
    )


def _reevaluation_disposition_result(
    row: sqlite3.Row, *, replayed: bool
) -> ReevaluationDispositionResult:
    return ReevaluationDispositionResult(
        receipt_id=str(row["receipt_id"]),
        reevaluation_id=str(row["reevaluation_id"]),
        revision=int(row["resulting_revision"]),
        state="reviewed",
        reanswer_requested_id=(
            str(row["reanswer_request_id"])
            if row["reanswer_request_id"] is not None
            else None
        ),
        replayed=replayed,
    )


def _audit_id(kind: str, receipt_id: str) -> str:
    return f"{kind}:{sha256(receipt_id.encode()).hexdigest()}"


def _aware_now(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReviewInboxUnavailable()
    return value.isoformat()


def _write_connection(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except Exception as error:
        raise ReviewInboxUnavailable() from error


def _backup_summary(row: sqlite3.Row) -> BackupReviewSummary:
    return BackupReviewSummary(
        review_id=str(row["review_id"]),
        request_id=str(row["request_id"]),
        source_answer_record_id=str(row["source_answer_record_id"]),
        revision=int(row["current_revision"]),
        state=cast(ReviewAggregateState, str(row["current_state"])),
        created_at=_timestamp(str(row["created_at"])),
    )


def _reevaluation_summary(row: sqlite3.Row) -> ReevaluationSummary:
    return ReevaluationSummary(
        reevaluation_id=str(row["reevaluation_id"]),
        request_id=str(row["request_id"]),
        feedback_id=str(row["feedback_id"]),
        source_answer_record_id=str(row["source_answer_record_id"]),
        revision=int(row["current_revision"]),
        state=cast(ReviewAggregateState, str(row["current_state"])),
        created_at=_timestamp(str(row["created_at"])),
    )


def _validate_read_command(command: ReviewReadCommand) -> None:
    if (
        type(command) is not ReviewReadCommand
        or _SHA256.fullmatch(command.identity_session_id) is None
        or not _valid_reference(command.expected_org_id)
        or not _valid_reference(command.expected_actor_id)
    ):
        raise ReviewInboxUnavailable()


def _require_read_proof(
    proof: ReviewReadProof,
    command: ReviewReadCommand,
    action: ReviewReadAction,
    resource: ResourceRef,
) -> None:
    session_resource = ResourceRef(
        org_id=command.expected_org_id,
        kind="browser_session",
        resource_id=command.identity_session_id,
        owner_subject_id=command.expected_actor_id,
    )
    if (
        type(proof) is not ReviewReadProof
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
        raise ReviewInboxUnavailable()


class FileReloadingReviewInboxAuthority:
    def __init__(
        self,
        *,
        authority_policy_path: Path,
        configured_org_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._path = authority_policy_path
        self._org_id = configured_org_id
        self._clock = clock

    def authorize_read(
        self,
        command: ReviewReadCommand,
        action: ReviewReadAction,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ReviewReadProof:
        principal = self._principal(command, transaction)
        authorizer = self._authorizer()
        session_resource = ResourceRef(
            org_id=principal.org_id,
            kind="browser_session",
            resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        session = authorizer.authorize(
            principal, "session.read", session_resource
        )
        grant = authorizer.authorize(principal, action, resource)
        if (
            type(session) is not AuthorizationGrant
            or type(grant) is not AuthorizationGrant
            or not authorizer.verify(
                session, principal, "session.read", session_resource
            )
            or not authorizer.verify(grant, principal, action, resource)
        ):
            raise ReviewInboxNotFound()
        return ReviewReadProof(principal, session, grant)

    def current_source_binding(
        self, row: sqlite3.Row, transaction: sqlite3.Connection
    ) -> bool:
        try:
            digest = _current_card_digest(
                transaction,
                str(row["org_id"]),
                str(row["answering_card_id"]),
                str(row["owner_user_id"]),
                int(row["answering_card_revision"]),
            )
            return digest == row["answering_card_digest"]
        except _ReviewBindingNotCurrent:
            return False

    def authorize_disposition(
        self,
        command: BackupReviewDispositionCommand
        | ReevaluationDispositionCommand,
        action: Literal["backup_review.decide", "reevaluation.decide"],
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ReviewReadProof:
        return self.authorize_read(
            ReviewReadCommand(
                identity_session_id=command.identity_session_id,
                expected_org_id=command.expected_org_id,
                expected_actor_id=command.expected_actor_id,
            ),
            action,
            resource,
            transaction,
        )

    def verify_disposition(
        self,
        proof: ReviewReadProof,
        command: BackupReviewDispositionCommand
        | ReevaluationDispositionCommand,
        action: Literal["backup_review.decide", "reevaluation.decide"],
        resource: ResourceRef,
        row: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        current = self.authorize_disposition(
            command, action, resource, transaction
        )
        return (
            current.principal == proof.principal
            and current.session_grant == proof.session_grant
            and current.action_grant == proof.action_grant
            and self.current_source_binding(row, transaction)
        )

    def _principal(
        self, command: ReviewReadCommand, transaction: sqlite3.Connection
    ) -> AuthenticatedPrincipal:
        if command.expected_org_id != self._org_id:
            raise ReviewInboxNotFound()
        from agent_org_network.central_browser_auth_sqlite import (
            read_current_browser_session_connection,
        )

        try:
            session = read_current_browser_session_connection(
                transaction, command.identity_session_id, now=self._clock()
            )
        except Exception as error:
            raise ReviewInboxUnavailable() from error
        if session is None:
            raise ReviewSessionUnauthenticated()
        if (
            session.org_id != command.expected_org_id
            or session.registry_user_id != command.expected_actor_id
        ):
            raise ReviewInboxNotFound()
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
            raise ReviewInboxUnavailable() from error


def _validate_catalog(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ReviewInboxUnavailable()
    validate_central_question_lifecycle_connection(connection)
    validate_central_inbox_approval_connection(connection)
    _validate_owned_shape(connection)
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ReviewInboxUnavailable()
    expected = {
        (evidence.source_kind, evidence.source_id): evidence
        for evidence in _all_eligible_evidence(connection)
    }
    rows = tuple(
        connection.execute(
            "SELECT * FROM central_inbox_review_outbox_intents"
        )
    )
    if {(str(row["source_kind"]), str(row["source_id"])) for row in rows} != set(
        expected
    ):
        raise ReviewInboxUnavailable()
    for row in rows:
        evidence = expected[(str(row["source_kind"]), str(row["source_id"]))]
        _require_exact_intent(connection, evidence)
        _validate_intent_state(connection, row, evidence)
    _validate_projection_reverse_links(connection)
    _validate_disposition_reverse_links(connection)


def _validate_owned_shape(connection: sqlite3.Connection) -> None:
    if _existing_owned(connection) != set(_TABLES):
        raise ReviewInboxUnavailable()
    if _catalog_signature(connection, _TABLES) != _EXPECTED_CATALOG:
        raise ReviewInboxUnavailable()
    marker = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT name,version FROM central_inbox_review_component_schema"
        )
    )
    if marker != ((_COMPONENT, _VERSION),):
        raise ReviewInboxUnavailable()


def _validate_d1_catalog(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ReviewInboxUnavailable()
    validate_central_question_lifecycle_connection(connection)
    validate_central_inbox_approval_connection(connection)
    if _existing_owned(connection) != set(_D1_TABLES):
        raise ReviewInboxUnavailable()
    if (
        _catalog_signature(connection, _D1_TABLES)
        != _D1_EXPECTED_CATALOG
    ):
        raise ReviewInboxUnavailable()
    marker = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT name,version FROM central_inbox_review_component_schema"
        )
    )
    if marker != ((_COMPONENT, _D1_VERSION),):
        raise ReviewInboxUnavailable()
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ReviewInboxUnavailable()
    expected = {
        (evidence.source_kind, evidence.source_id): evidence
        for evidence in _all_eligible_evidence(connection)
    }
    intents = tuple(
        connection.execute(
            "SELECT * FROM central_inbox_review_outbox_intents"
        )
    )
    if {
        (str(row["source_kind"]), str(row["source_id"])) for row in intents
    } != set(expected):
        raise ReviewInboxUnavailable()
    for row in intents:
        evidence = expected[
            (str(row["source_kind"]), str(row["source_id"]))
        ]
        _require_exact_intent(connection, evidence)
        _validate_intent_state(connection, row, evidence)
    _validate_projection_reverse_links(connection)


def _validate_intent_state(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    evidence: _SourceEvidence,
) -> None:
    status = row["status"]
    receipt_count = int(
        connection.execute(
            "SELECT count(*) FROM central_inbox_review_projection_receipts "
            "WHERE intent_id=?",
            (evidence.intent_id,),
        ).fetchone()[0]
    )
    if status == "pending":
        valid = (
            row["worker_id"] is None
            and row["lease_until"] is None
            and int(row["attempts"]) == 0
            and row["delivered_at"] is None
            and receipt_count == 0
        )
    elif status == "leased":
        valid = (
            _valid_reference(row["worker_id"])
            and row["lease_until"] is not None
            and int(row["attempts"]) > 0
            and row["delivered_at"] is None
            and receipt_count == 0
        )
        if valid:
            _timestamp(str(row["lease_until"]))
    elif status == "delivered":
        valid = (
            row["worker_id"] is None
            and row["lease_until"] is None
            and int(row["attempts"]) > 0
            and row["delivered_at"] is not None
            and receipt_count == 1
        )
        if valid:
            _timestamp(str(row["delivered_at"]))
    else:
        valid = False
    if not valid:
        raise ReviewInboxUnavailable()


def _validate_projection_reverse_links(connection: sqlite3.Connection) -> None:
    receipts = tuple(
        connection.execute(
            "SELECT * FROM central_inbox_review_projection_receipts"
        )
    )
    for receipt in receipts:
        intent = connection.execute(
            "SELECT * FROM central_inbox_review_outbox_intents "
            "WHERE intent_id=?",
            (receipt["intent_id"],),
        ).fetchone()
        if intent is None or intent["status"] != "delivered":
            raise ReviewInboxUnavailable()
        table, id_column = (
            ("central_inbox_backup_reviews", "review_id")
            if intent["source_kind"] == "backup_review"
            else ("central_inbox_reevaluations", "reevaluation_id")
        )
        aggregate = connection.execute(
            f"SELECT * FROM {table} WHERE {id_column}=?",
            (receipt["aggregate_id"],),
        ).fetchone()
        if (
            aggregate is None
            or aggregate["source_intent_id"] != intent["intent_id"]
            or aggregate["request_id"] != intent["request_id"]
            or aggregate["source_answer_record_id"]
            != intent["source_answer_record_id"]
            or aggregate["producer_receipt_id"]
            != intent["producer_receipt_id"]
            != receipt["producer_receipt_id"]
            or aggregate["producer_receipt_digest"]
            != intent["producer_receipt_digest"]
            != receipt["producer_receipt_digest"]
            or receipt["source_kind"] != intent["source_kind"]
            or receipt["source_id"] != intent["source_id"]
            or aggregate["state"] != "open"
            or int(aggregate["revision"]) != 1
        ):
            raise ReviewInboxUnavailable()
    aggregate_count = int(
        connection.execute(
            "SELECT (SELECT count(*) FROM central_inbox_backup_reviews)+"
            "(SELECT count(*) FROM central_inbox_reevaluations)"
        ).fetchone()[0]
    )
    if aggregate_count != len(receipts):
        raise ReviewInboxUnavailable()


def _validate_disposition_reverse_links(
    connection: sqlite3.Connection,
) -> None:
    _validate_disposition_kind(
        connection,
        aggregate_table="central_inbox_backup_reviews",
        head_table="central_inbox_backup_review_heads",
        receipt_table="central_inbox_backup_review_disposition_receipts",
        audit_table="central_inbox_backup_review_disposition_audits",
        id_column="review_id",
        followup_table="central_inbox_answer_correction_records",
        followup_column="correction_record_id",
        followup_kind="correct",
    )
    _validate_disposition_kind(
        connection,
        aggregate_table="central_inbox_reevaluations",
        head_table="central_inbox_reevaluation_heads",
        receipt_table="central_inbox_reevaluation_disposition_receipts",
        audit_table="central_inbox_reevaluation_disposition_audits",
        id_column="reevaluation_id",
        followup_table="central_inbox_reanswer_requested_records",
        followup_column="reanswer_request_id",
        followup_kind="request_reanswer",
    )
    for receipt in connection.execute(
        "SELECT * FROM central_inbox_backup_review_disposition_receipts"
    ):
        correction = connection.execute(
            "SELECT * FROM central_inbox_answer_correction_records "
            "WHERE receipt_id=?",
            (receipt["receipt_id"],),
        ).fetchone()
        aggregate = connection.execute(
            "SELECT * FROM central_inbox_backup_reviews WHERE review_id=?",
            (receipt["review_id"],),
        ).fetchone()
        if (
            aggregate is None
            or (receipt["kind"] == "correct") != (correction is not None)
            or (receipt["kind"] == "correct")
            != (receipt["corrected_text_digest"] is not None)
        ):
            raise ReviewInboxUnavailable()
        expected_digest = _backup_disposition_digest(
            BackupReviewDispositionCommand(
                review_id=str(receipt["review_id"]),
                identity_session_id=str(receipt["identity_session_id"]),
                expected_org_id=str(receipt["org_id"]),
                expected_actor_id=str(receipt["actor_id"]),
                kind=cast(
                    BackupReviewDispositionKind, str(receipt["kind"])
                ),
                rationale=str(receipt["rationale"]),
                corrected_text=(
                    str(correction["text"])
                    if correction is not None
                    else None
                ),
                expected_revision=int(receipt["expected_revision"]),
                idempotency_key=str(receipt["idempotency_key"]),
            )
        )
        if receipt["command_digest"] != expected_digest:
            raise ReviewInboxUnavailable()
        if correction is not None and (
            correction["correction_record_id"]
            != receipt["correction_record_id"]
            or correction["request_id"] != aggregate["request_id"]
            or correction["supersedes_record_id"]
            != aggregate["source_answer_record_id"]
            or correction["text_digest"]
            != sha256(str(correction["text"]).encode()).hexdigest()
            or correction["text_digest"] != receipt["corrected_text_digest"]
            or correction["mode"] != "full"
            or correction["actor_id"] != receipt["actor_id"]
            or correction["answering_card_id"]
            != receipt["answering_card_id"]
            != aggregate["answering_card_id"]
            or int(correction["answering_card_revision"])
            != int(receipt["answering_card_revision"])
            != int(aggregate["answering_card_revision"])
            or correction["answering_card_digest"]
            != receipt["answering_card_digest"]
            != aggregate["answering_card_digest"]
            or correction["created_at"] != receipt["created_at"]
        ):
            raise ReviewInboxUnavailable()
    for receipt in connection.execute(
        "SELECT * FROM central_inbox_reevaluation_disposition_receipts"
    ):
        followup = connection.execute(
            "SELECT * FROM central_inbox_reanswer_requested_records "
            "WHERE receipt_id=?",
            (receipt["receipt_id"],),
        ).fetchone()
        aggregate = connection.execute(
            "SELECT * FROM central_inbox_reevaluations "
            "WHERE reevaluation_id=?",
            (receipt["reevaluation_id"],),
        ).fetchone()
        if (
            aggregate is None
            or (receipt["kind"] == "request_reanswer")
            != (followup is not None)
        ):
            raise ReviewInboxUnavailable()
        expected_digest = _reevaluation_disposition_digest(
            ReevaluationDispositionCommand(
                reevaluation_id=str(receipt["reevaluation_id"]),
                identity_session_id=str(receipt["identity_session_id"]),
                expected_org_id=str(receipt["org_id"]),
                expected_actor_id=str(receipt["actor_id"]),
                kind=cast(
                    ReevaluationDispositionKind, str(receipt["kind"])
                ),
                rationale=str(receipt["rationale"]),
                expected_revision=int(receipt["expected_revision"]),
                idempotency_key=str(receipt["idempotency_key"]),
            )
        )
        if receipt["command_digest"] != expected_digest:
            raise ReviewInboxUnavailable()
        if followup is not None and (
            followup["reanswer_request_id"]
            != receipt["reanswer_request_id"]
            or followup["request_id"] != aggregate["request_id"]
            or followup["source_answer_record_id"]
            != aggregate["source_answer_record_id"]
            or followup["feedback_id"] != aggregate["feedback_id"]
            or followup["rationale"] != receipt["rationale"]
            or followup["actor_id"] != receipt["actor_id"]
            or followup["answering_card_id"]
            != receipt["answering_card_id"]
            != aggregate["answering_card_id"]
            or int(followup["answering_card_revision"])
            != int(receipt["answering_card_revision"])
            != int(aggregate["answering_card_revision"])
            or followup["answering_card_digest"]
            != receipt["answering_card_digest"]
            != aggregate["answering_card_digest"]
            or followup["created_at"] != receipt["created_at"]
        ):
            raise ReviewInboxUnavailable()


def _validate_disposition_kind(
    connection: sqlite3.Connection,
    *,
    aggregate_table: str,
    head_table: str,
    receipt_table: str,
    audit_table: str,
    id_column: str,
    followup_table: str,
    followup_column: str,
    followup_kind: str,
) -> None:
    aggregates = {
        str(row[0])
        for row in connection.execute(
            f"SELECT {id_column} FROM {aggregate_table}"
        )
    }
    heads = tuple(connection.execute(f"SELECT * FROM {head_table}"))
    if {str(row[id_column]) for row in heads} != aggregates:
        raise ReviewInboxUnavailable()
    receipt_count = 0
    for head in heads:
        if head["state"] == "open":
            if (
                int(head["revision"]) != 1
                or head["disposition_receipt_id"] is not None
            ):
                raise ReviewInboxUnavailable()
            continue
        receipt = connection.execute(
            f"SELECT * FROM {receipt_table} WHERE receipt_id=?",
            (head["disposition_receipt_id"],),
        ).fetchone()
        audit = connection.execute(
            f"SELECT * FROM {audit_table} WHERE receipt_id=?",
            (head["disposition_receipt_id"],),
        ).fetchone()
        if (
            receipt is None
            or audit is None
            or receipt[id_column] != head[id_column]
            or audit[id_column] != head[id_column]
            or int(receipt["resulting_revision"])
            != int(receipt["expected_revision"]) + 1
            or int(receipt["resulting_revision"]) != int(head["revision"])
            or int(audit["resulting_revision"]) != int(head["revision"])
            or audit["org_id"] != receipt["org_id"]
            or audit["actor_id"] != receipt["actor_id"]
            or audit["command_digest"] != receipt["command_digest"]
            or audit["kind"] != receipt["kind"]
            or audit[followup_column] != receipt[followup_column]
            or audit["created_at"] != receipt["created_at"]
        ):
            raise ReviewInboxUnavailable()
        followup = connection.execute(
            f"SELECT * FROM {followup_table} WHERE {followup_column}=?",
            (receipt[followup_column],),
        ).fetchone()
        if (receipt["kind"] == followup_kind) != (followup is not None):
            raise ReviewInboxUnavailable()
        if followup is not None and (
            followup[id_column] != head[id_column]
            or followup["receipt_id"] != receipt["receipt_id"]
        ):
            raise ReviewInboxUnavailable()
        receipt_count += 1
    if (
        int(
            connection.execute(
                f"SELECT count(*) FROM {receipt_table}"
            ).fetchone()[0]
        )
        != receipt_count
        or int(
            connection.execute(
                f"SELECT count(*) FROM {audit_table}"
            ).fetchone()[0]
        )
        != receipt_count
    ):
        raise ReviewInboxUnavailable()


def _existing_owned(connection: sqlite3.Connection) -> set[str]:
    placeholders = ",".join("?" for _ in _TABLES)
    return {
        str(row[0])
        for row in connection.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' "
            f"AND name IN ({placeholders})",
            tuple(_TABLES),
        )
    }


def _normalized_sql(value: object) -> str:
    return " ".join(str(value).split())


def _catalog_signature(
    connection: sqlite3.Connection,
    table_definitions: dict[str, str],
) -> tuple[object, ...]:
    owned = tuple(table_definitions)
    placeholders = ",".join("?" for _ in owned)
    objects = tuple(
        (
            row[0],
            row[1],
            row[2],
            _normalized_sql(row[3]),
        )
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE "
            f"(type='table' AND name IN ({placeholders})) OR "
            f"(type='trigger' AND tbl_name IN ({placeholders})) OR "
            "(type='index' AND name IN (?,?,?)) ORDER BY type,name",
            (
                *owned,
                *owned,
                "central_inbox_review_claimable",
                "central_inbox_backup_open_owner",
                "central_inbox_reevaluation_open_owner",
            ),
        )
    )
    tables = tuple(
        (
            name,
            tuple(
                tuple(row)
                for row in connection.execute(f"PRAGMA table_info({name})")
            ),
            tuple(
                tuple(row)
                for row in connection.execute(
                    f"PRAGMA foreign_key_list({name})"
                )
            ),
            tuple(
                tuple(row)
                for row in connection.execute(f"PRAGMA index_list({name})")
            ),
        )
        for name in sorted(table_definitions)
    )
    return objects, tables


def _expected_catalog(
    table_definitions: dict[str, str],
    indexes: tuple[str, ...],
    triggers: tuple[str, ...],
) -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE question_requests(request_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE central_question_answer_records(record_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE central_question_feedback_records(feedback_id TEXT PRIMARY KEY)"
        )
        for ddl in table_definitions.values():
            connection.execute(ddl)
        for ddl in indexes:
            connection.execute(ddl)
        for ddl in triggers:
            connection.execute(ddl)
        return _catalog_signature(connection, table_definitions)
    finally:
        connection.close()


_D1_EXPECTED_CATALOG = _expected_catalog(
    _D1_TABLES, _D1_INDEXES, _D1_TRIGGERS
)
_EXPECTED_CATALOG = _expected_catalog(_TABLES, _INDEXES, _TRIGGERS)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _timestamp(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReviewInboxUnavailable()
    return value


def _valid_reference(value: object) -> bool:
    return type(value) is str and _REFERENCE.fullmatch(value) is not None


__all__ = [
    "BackupReviewDispositionApplication",
    "BackupReviewDispositionCommand",
    "BackupReviewDispositionResult",
    "BackupReviewDetail",
    "BackupReviewSummary",
    "FileReloadingReviewInboxAuthority",
    "ReevaluationDetail",
    "ReevaluationDispositionApplication",
    "ReevaluationDispositionCommand",
    "ReevaluationDispositionResult",
    "ReevaluationSummary",
    "ReviewInboxApplication",
    "ReviewInboxAuthority",
    "ReviewInboxConflict",
    "ReviewInboxNotFound",
    "ReviewInboxUnavailable",
    "ReviewOutboxClaim",
    "ReviewOutboxProjector",
    "ReviewOutboxRecovery",
    "ReviewProjectionResult",
    "ReviewReadCommand",
    "ReviewReadProof",
    "ReviewSessionUnauthenticated",
    "append_backup_review_intent",
    "append_reevaluation_intent",
    "central_inbox_review_schema_ready",
    "migrate_central_inbox_review_schema",
    "validate_central_inbox_review_connection",
    "verify_backup_review_intent",
    "verify_reevaluation_intent",
]
