"""RB3.2b.4-B1 durable Central Question Request disposition seam.

This module deliberately stops before HTTP, SSE, Approval and answer
finalization.  It provides the transactional boundary that those adapters use:
``Received + create receipt`` commits first; routing is a later transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Literal, Protocol, cast
from unicodedata import normalize
from uuid import uuid4

from pydantic import TypeAdapter

from agent_org_network.central_operational_evidence import (
    ApprovalChange,
    AnswerChange,
    FeedbackChange,
    ManagerItemChange,
    QuestionChange,
    SafeResourceRef,
    SourceReceiptProvenance,
    append_committed_source_evidence_if_v19,
    canonical_v19_file_authority,
    source_receipt_digest,
)


from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    AuthorizationResult,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.question_request import (
    AwaitingConflict,
    AwaitingAnswer,
    AwaitingApproval,
    AnsweredRequest,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
    QuestionRequestState,
    ReadyToDispatch,
    RouteTarget,
    AwaitingManager,
    Received,
    validate_compare_and_set_semantics,
)


_STATE: TypeAdapter[QuestionRequestState] = TypeAdapter(QuestionRequestState)
_CONFLICT_CANDIDATES: TypeAdapter[list[dict[str, str]]] = TypeAdapter(list[dict[str, str]])
_ROUTE: TypeAdapter[RouteTarget] = TypeAdapter(RouteTarget)
_SOURCES: TypeAdapter[tuple[str, ...]] = TypeAdapter(tuple[str, ...])
_TABLES = (
    """CREATE TABLE central_question_create_receipts (
       org_id TEXT NOT NULL, requester_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
       question_json TEXT NOT NULL, received_json TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
       command_digest TEXT, authority_policy_revision_id TEXT, authority_policy_epoch INTEGER,
       authority_policy_digest TEXT, created_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       PRIMARY KEY(org_id, requester_id, idempotency_key)
    )""",
    """CREATE TABLE central_question_manager_items (
       request_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, item_id TEXT NOT NULL UNIQUE,
       manager_id TEXT NOT NULL, intent TEXT, created_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_question_conflict_cases (
       request_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, case_id TEXT NOT NULL UNIQUE,
       intent TEXT NOT NULL, candidates_json TEXT NOT NULL, created_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_question_work_tickets (
       request_id TEXT NOT NULL, attempt INTEGER NOT NULL CHECK(attempt > 0), ticket_id TEXT NOT NULL UNIQUE,
       org_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, route_json TEXT NOT NULL,
       status TEXT NOT NULL CHECK(status IN ('pending','completed')), create_digest TEXT NOT NULL, created_at TEXT NOT NULL,
       PRIMARY KEY(request_id,attempt),
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_question_work_ticket_receipts (
       request_id TEXT NOT NULL, attempt INTEGER NOT NULL CHECK(attempt > 0), command_digest TEXT NOT NULL,
       ticket_id TEXT NOT NULL UNIQUE, authority_policy_revision_id TEXT,
       authority_policy_epoch INTEGER, authority_policy_digest TEXT, created_at TEXT NOT NULL,
       PRIMARY KEY(request_id,attempt),
       FOREIGN KEY(request_id,attempt) REFERENCES central_question_work_tickets(request_id,attempt) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_question_work_ticket_delivery_claims (
       ticket_id TEXT PRIMARY KEY NOT NULL, status TEXT NOT NULL CHECK(status IN ('leased','delivered')),
       worker_id TEXT NOT NULL, lease_until TEXT NOT NULL, delivery_attempt INTEGER NOT NULL CHECK(delivery_attempt>0),
       ack_receipt TEXT UNIQUE, acked_at TEXT,
       FOREIGN KEY(ticket_id) REFERENCES central_question_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_question_answer_ingest_receipts (
       org_id TEXT NOT NULL, delivery_subject TEXT NOT NULL, ticket_id TEXT NOT NULL UNIQUE,
       request_id TEXT NOT NULL, expected_request_revision INTEGER NOT NULL CHECK(expected_request_revision >= 0),
       attempt INTEGER NOT NULL CHECK(attempt > 0), route_json TEXT NOT NULL, candidate_json TEXT NOT NULL,
       candidate_digest TEXT NOT NULL, policy_kind TEXT NOT NULL CHECK(policy_kind IN ('no_approval','approval_required')),
       policy_digest TEXT NOT NULL, binding_version INTEGER NOT NULL CHECK(binding_version > 0),
       authority_version TEXT NOT NULL, authority_policy_revision_id TEXT,
       authority_policy_epoch INTEGER, authority_policy_digest TEXT,
       result_kind TEXT NOT NULL CHECK(result_kind IN ('answered','awaiting_approval')),
       record_id TEXT UNIQUE, approval_item_id TEXT UNIQUE, created_at TEXT NOT NULL,
       FOREIGN KEY(ticket_id) REFERENCES central_question_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       PRIMARY KEY(org_id,delivery_subject,ticket_id,request_id,expected_request_revision,candidate_digest)
    )""",
    """CREATE TABLE central_question_answer_records (
       record_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL UNIQUE, ticket_id TEXT NOT NULL UNIQUE,
       org_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, text TEXT NOT NULL,
       sources_json TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('full','backup')),
       review_status TEXT NOT NULL CHECK(review_status='not_required'), candidate_digest TEXT NOT NULL,
       created_at TEXT NOT NULL,
       FOREIGN KEY(ticket_id) REFERENCES central_question_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_question_approval_items (
       approval_item_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL UNIQUE, ticket_id TEXT NOT NULL UNIQUE,
       org_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, route_json TEXT NOT NULL,
       attempt INTEGER NOT NULL CHECK(attempt > 0), candidate_json TEXT NOT NULL, candidate_digest TEXT NOT NULL,
       policy_digest TEXT NOT NULL, binding_version INTEGER NOT NULL CHECK(binding_version > 0),
       status TEXT NOT NULL CHECK(status='open'), created_at TEXT NOT NULL,
       FOREIGN KEY(ticket_id) REFERENCES central_question_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_question_answer_ingest_audits (
       ticket_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL, receipt_ticket_id TEXT NOT NULL UNIQUE,
       event_kind TEXT NOT NULL CHECK(event_kind IN ('answered','awaiting_approval')), created_at TEXT NOT NULL,
       FOREIGN KEY(ticket_id) REFERENCES central_question_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(receipt_ticket_id) REFERENCES central_question_answer_ingest_receipts(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)
_B1_OWNED = (
    "central_question_create_receipts",
    "central_question_manager_items",
)
_B2A_OWNED = _B1_OWNED + (
    "central_question_conflict_cases", "central_question_work_tickets", "central_question_work_ticket_receipts",
)
_V7_OWNED = _B2A_OWNED + ("central_question_work_ticket_delivery_claims",)
_V7_TABLES = _TABLES[:3] + (
    _TABLES[3].replace("CHECK(status IN ('pending','completed'))", "CHECK(status='pending')"),
) + _TABLES[4:6]
_V8_TABLES = _TABLES
_OWNED = (
    "central_question_create_receipts",
    "central_question_manager_items",
    "central_question_conflict_cases",
    "central_question_work_tickets",
    "central_question_work_ticket_receipts",
    "central_question_work_ticket_delivery_claims",
    "central_question_answer_ingest_receipts",
    "central_question_answer_records",
    "central_question_approval_items",
    "central_question_answer_ingest_audits",
)
_V8_OWNED = _OWNED
_TABLES = _TABLES[:7] + (  # pyright: ignore[reportConstantRedefinition]
    """CREATE TABLE central_question_answer_records (
       record_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL UNIQUE, ticket_id TEXT NOT NULL UNIQUE,
       org_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, text TEXT NOT NULL,
       sources_json TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('full','backup')),
       review_status TEXT NOT NULL CHECK(review_status IN ('not_required','approved')), candidate_digest TEXT NOT NULL,
       created_at TEXT NOT NULL,
       FOREIGN KEY(ticket_id) REFERENCES central_question_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_question_approval_items (
       approval_item_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL UNIQUE, ticket_id TEXT NOT NULL UNIQUE,
       org_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, route_json TEXT NOT NULL,
       attempt INTEGER NOT NULL CHECK(attempt > 0), candidate_json TEXT NOT NULL, candidate_digest TEXT NOT NULL,
       policy_digest TEXT NOT NULL, binding_version INTEGER NOT NULL CHECK(binding_version > 0),
       status TEXT NOT NULL CHECK(status IN ('open','approved','rejected')), revision INTEGER NOT NULL CHECK(revision >= 1),
       created_at TEXT NOT NULL,
       FOREIGN KEY(ticket_id) REFERENCES central_question_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
) + _TABLES[9:] + (
    """CREATE TABLE central_question_approval_disposition_receipts (
       org_id TEXT NOT NULL, request_id TEXT NOT NULL, approval_item_id TEXT NOT NULL, actor_id TEXT NOT NULL,
       expected_approval_item_revision INTEGER NOT NULL, expected_request_revision INTEGER NOT NULL,
       decision_kind TEXT NOT NULL CHECK(decision_kind IN ('approve','approve_with_edit','reject')),
       decision_digest TEXT NOT NULL, edited_text TEXT, idempotency_key TEXT NOT NULL, candidate_digest TEXT NOT NULL,
       policy_digest TEXT NOT NULL, binding_version INTEGER NOT NULL, terminal_kind TEXT NOT NULL CHECK(terminal_kind IN ('answered','declined')),
       record_id TEXT UNIQUE, resolved_item_revision INTEGER NOT NULL, terminal_request_revision INTEGER NOT NULL,
       created_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       PRIMARY KEY(org_id,request_id,approval_item_id,actor_id,expected_approval_item_revision,expected_request_revision,decision_kind,decision_digest,idempotency_key)
    )""",
    """CREATE TABLE central_question_approval_disposition_audits (
       approval_item_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL, receipt_idempotency_key TEXT NOT NULL,
       event_kind TEXT NOT NULL CHECK(event_kind IN ('answered','declined')), created_at TEXT NOT NULL,
       FOREIGN KEY(approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)
_OWNED = _V8_OWNED + (  # pyright: ignore[reportConstantRedefinition]
    "central_question_approval_disposition_receipts",
    "central_question_approval_disposition_audits",
)
_V9_TABLES = _TABLES
_V9_OWNED = _OWNED
_TABLES = _TABLES[:10] + (  # pyright: ignore[reportConstantRedefinition]
    """CREATE TABLE central_question_approval_disposition_receipts (
       receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, request_id TEXT NOT NULL,
       approval_item_id TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL,
       expected_approval_item_revision INTEGER NOT NULL, expected_request_revision INTEGER NOT NULL,
       decision_kind TEXT NOT NULL CHECK(decision_kind IN ('approve','approve_with_edit','reject')),
       decision_digest TEXT NOT NULL, edited_text TEXT, idempotency_key TEXT NOT NULL, candidate_digest TEXT NOT NULL,
       policy_digest TEXT NOT NULL, binding_version INTEGER NOT NULL,
       terminal_kind TEXT NOT NULL CHECK(terminal_kind IN ('answered','declined')),
       record_id TEXT UNIQUE, resolved_item_revision INTEGER NOT NULL, terminal_request_revision INTEGER NOT NULL,
       created_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       UNIQUE(org_id,request_id,approval_item_id,actor_id,expected_approval_item_revision,expected_request_revision,decision_kind,decision_digest,idempotency_key)
    )""",
    """CREATE TABLE central_question_approval_disposition_audits (
       receipt_id TEXT PRIMARY KEY NOT NULL, approval_item_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL,
       actor_id TEXT NOT NULL, decision_kind TEXT NOT NULL, decision_digest TEXT NOT NULL,
       terminal_kind TEXT NOT NULL CHECK(terminal_kind IN ('answered','declined')), record_id TEXT UNIQUE,
       created_at TEXT NOT NULL,
       FOREIGN KEY(receipt_id) REFERENCES central_question_approval_disposition_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)
_OWNED = _V9_OWNED  # pyright: ignore[reportConstantRedefinition]
_V10_TABLES = _TABLES
_V10_OWNED = _OWNED
_TABLES = _TABLES[:10] + (  # pyright: ignore[reportConstantRedefinition]
    """CREATE TABLE central_question_approval_disposition_receipts (
       receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, request_id TEXT NOT NULL,
       approval_item_id TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL, identity_session_id TEXT NOT NULL,
       authority_policy_version TEXT NOT NULL, authority_policy_digest TEXT NOT NULL,
       expected_approval_item_revision INTEGER NOT NULL, expected_request_revision INTEGER NOT NULL,
       decision_kind TEXT NOT NULL CHECK(decision_kind IN ('approve','approve_with_edit','reject')),
       decision_digest TEXT NOT NULL, edited_text TEXT, idempotency_key TEXT NOT NULL, candidate_digest TEXT NOT NULL,
       policy_digest TEXT NOT NULL, binding_version INTEGER NOT NULL,
       terminal_kind TEXT NOT NULL CHECK(terminal_kind IN ('answered','declined')),
       record_id TEXT UNIQUE, resolved_item_revision INTEGER NOT NULL, terminal_request_revision INTEGER NOT NULL,
       created_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       UNIQUE(org_id,request_id,approval_item_id,actor_id,expected_approval_item_revision,expected_request_revision,decision_kind,decision_digest,idempotency_key)
    )""",
    """CREATE TABLE central_question_approval_disposition_audits (
       receipt_id TEXT PRIMARY KEY NOT NULL, approval_item_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL,
       actor_id TEXT NOT NULL, identity_session_id TEXT NOT NULL, authority_policy_version TEXT NOT NULL,
       authority_policy_digest TEXT NOT NULL, decision_kind TEXT NOT NULL, decision_digest TEXT NOT NULL,
       terminal_kind TEXT NOT NULL CHECK(terminal_kind IN ('answered','declined')), record_id TEXT UNIQUE,
       created_at TEXT NOT NULL,
       FOREIGN KEY(receipt_id) REFERENCES central_question_approval_disposition_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)
_OWNED = _V10_OWNED  # pyright: ignore[reportConstantRedefinition]
_V11_TABLES = _TABLES
_V11_OWNED = _OWNED
_TABLES = _TABLES[:10] + (  # pyright: ignore[reportConstantRedefinition]
    """CREATE TABLE central_question_approval_disposition_receipts (
       receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, request_id TEXT NOT NULL,
       approval_item_id TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL, identity_session_id TEXT NOT NULL,
       authority_policy_version TEXT NOT NULL, authority_policy_digest TEXT NOT NULL,
       authority_proof_digest TEXT NOT NULL CHECK(length(authority_proof_digest)=64),
       expected_approval_item_revision INTEGER NOT NULL, expected_request_revision INTEGER NOT NULL,
       decision_kind TEXT NOT NULL CHECK(decision_kind IN ('approve','approve_with_edit','reject')),
       decision_digest TEXT NOT NULL, edited_text TEXT, idempotency_key TEXT NOT NULL, candidate_digest TEXT NOT NULL,
       policy_digest TEXT NOT NULL, binding_version INTEGER NOT NULL,
       terminal_kind TEXT NOT NULL CHECK(terminal_kind IN ('answered','declined')),
       record_id TEXT UNIQUE, resolved_item_revision INTEGER NOT NULL, terminal_request_revision INTEGER NOT NULL,
       authority_policy_revision_id TEXT, authority_policy_epoch INTEGER,
       created_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       UNIQUE(org_id,request_id,approval_item_id,actor_id,expected_approval_item_revision,expected_request_revision,decision_kind,decision_digest,idempotency_key)
    )""",
    """CREATE TABLE central_question_approval_disposition_audits (
       receipt_id TEXT PRIMARY KEY NOT NULL, approval_item_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL,
       actor_id TEXT NOT NULL, identity_session_id TEXT NOT NULL, authority_policy_version TEXT NOT NULL,
       authority_policy_digest TEXT NOT NULL, authority_proof_digest TEXT NOT NULL CHECK(length(authority_proof_digest)=64),
       decision_kind TEXT NOT NULL, decision_digest TEXT NOT NULL,
       terminal_kind TEXT NOT NULL CHECK(terminal_kind IN ('answered','declined')), record_id TEXT UNIQUE,
       created_at TEXT NOT NULL,
       FOREIGN KEY(receipt_id) REFERENCES central_question_approval_disposition_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(approval_item_id) REFERENCES central_question_approval_items(approval_item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)
_OWNED = _V11_OWNED  # pyright: ignore[reportConstantRedefinition]
_V12_TABLES = _TABLES
_V12_OWNED = _OWNED
_TABLES = _TABLES + (  # pyright: ignore[reportConstantRedefinition]
    """CREATE TABLE central_question_feedback_records (
       feedback_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, request_id TEXT NOT NULL,
       record_id TEXT NOT NULL, requester_id TEXT NOT NULL,
       verdict TEXT NOT NULL CHECK(verdict IN ('good','bad')), comment TEXT NOT NULL,
       payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64), submitted_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(record_id) REFERENCES central_question_answer_records(record_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       UNIQUE(org_id,request_id,record_id,requester_id,feedback_id)
    )""",
    """CREATE TABLE central_question_feedback_receipts (
       receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, requester_id TEXT NOT NULL,
       action TEXT NOT NULL CHECK(action='feedback.create'), request_id TEXT NOT NULL, record_id TEXT NOT NULL,
       idempotency_key TEXT NOT NULL, payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
       feedback_id TEXT NOT NULL UNIQUE, authority_policy_revision_id TEXT,
       authority_policy_epoch INTEGER, authority_policy_digest TEXT,
       submitted_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(record_id) REFERENCES central_question_answer_records(record_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(feedback_id) REFERENCES central_question_feedback_records(feedback_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       UNIQUE(org_id,requester_id,action,idempotency_key)
    )""",
    """CREATE TABLE central_question_feedback_audits (
       receipt_id TEXT PRIMARY KEY NOT NULL, feedback_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL,
       record_id TEXT NOT NULL, requester_id TEXT NOT NULL, verdict TEXT NOT NULL CHECK(verdict IN ('good','bad')),
       payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64), submitted_at TEXT NOT NULL,
       FOREIGN KEY(receipt_id) REFERENCES central_question_feedback_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(feedback_id) REFERENCES central_question_feedback_records(feedback_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(record_id) REFERENCES central_question_answer_records(record_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)
_OWNED = _V12_OWNED + (  # pyright: ignore[reportConstantRedefinition]
    "central_question_feedback_records",
    "central_question_feedback_receipts",
    "central_question_feedback_audits",
)
_FEEDBACK_TRIGGERS = (
    "CREATE TRIGGER central_question_feedback_records_no_update BEFORE UPDATE ON central_question_feedback_records BEGIN SELECT RAISE(ABORT,'immutable feedback record'); END",
    "CREATE TRIGGER central_question_feedback_records_no_delete BEFORE DELETE ON central_question_feedback_records BEGIN SELECT RAISE(ABORT,'immutable feedback record'); END",
    "CREATE TRIGGER central_question_feedback_receipts_no_update BEFORE UPDATE ON central_question_feedback_receipts BEGIN SELECT RAISE(ABORT,'immutable feedback receipt'); END",
    "CREATE TRIGGER central_question_feedback_receipts_no_delete BEFORE DELETE ON central_question_feedback_receipts BEGIN SELECT RAISE(ABORT,'immutable feedback receipt'); END",
    "CREATE TRIGGER central_question_feedback_audits_no_update BEFORE UPDATE ON central_question_feedback_audits BEGIN SELECT RAISE(ABORT,'immutable feedback audit'); END",
    "CREATE TRIGGER central_question_feedback_audits_no_delete BEFORE DELETE ON central_question_feedback_audits BEGIN SELECT RAISE(ABORT,'immutable feedback audit'); END",
)
_V13_TABLES = _TABLES
_V13_OWNED = _OWNED
_V13_FEEDBACK_TRIGGERS = _FEEDBACK_TRIGGERS
_TABLES = _TABLES[:14] + (  # pyright: ignore[reportConstantRedefinition]
    """CREATE TABLE central_question_feedback_audits (
       receipt_id TEXT PRIMARY KEY NOT NULL, feedback_id TEXT NOT NULL UNIQUE, org_id TEXT NOT NULL,
       request_id TEXT NOT NULL, record_id TEXT NOT NULL, requester_id TEXT NOT NULL,
       verdict TEXT NOT NULL CHECK(verdict IN ('good','bad')),
       payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64), submitted_at TEXT NOT NULL,
       FOREIGN KEY(receipt_id) REFERENCES central_question_feedback_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(feedback_id) REFERENCES central_question_feedback_records(feedback_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(record_id) REFERENCES central_question_answer_records(record_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)
_OWNED = _V13_OWNED  # pyright: ignore[reportConstantRedefinition]
_V14_TABLES = _TABLES
_V14_OWNED = _OWNED
_TABLES = _TABLES + (  # pyright: ignore[reportConstantRedefinition]
    """CREATE TABLE central_question_initial_transition_receipts (
       receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL,
       request_id TEXT NOT NULL UNIQUE,
       command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
       from_state TEXT NOT NULL CHECK(from_state='received'),
       to_state TEXT NOT NULL CHECK(to_state IN ('awaiting_manager','awaiting_conflict','ready_to_dispatch','declined')),
       manager_item_id TEXT UNIQUE, policy_revision_id TEXT NOT NULL,
       policy_epoch INTEGER NOT NULL CHECK(policy_epoch>0),
       policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),
       authority_policy_revision_id TEXT NOT NULL,
       authority_policy_epoch INTEGER NOT NULL CHECK(authority_policy_epoch>0),
       authority_policy_digest TEXT NOT NULL CHECK(length(authority_policy_digest)=64),
       created_at TEXT NOT NULL,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)
_OWNED = _V14_OWNED + (  # pyright: ignore[reportConstantRedefinition]
    "central_question_initial_transition_receipts",
)
_INITIAL_TRANSITION_TRIGGERS = (
    "CREATE TRIGGER central_question_initial_transition_receipts_no_update BEFORE UPDATE ON central_question_initial_transition_receipts BEGIN SELECT RAISE(ABORT,'immutable initial transition receipt'); END",
    "CREATE TRIGGER central_question_initial_transition_receipts_no_delete BEFORE DELETE ON central_question_initial_transition_receipts BEGIN SELECT RAISE(ABORT,'immutable initial transition receipt'); END",
)
CENTRAL_QUESTION_APPROVAL_ITEMS_V16_DDL = """CREATE TABLE central_question_approval_items (
       approval_item_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
       org_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, route_json TEXT NOT NULL,
       attempt INTEGER NOT NULL CHECK(attempt > 0), candidate_json TEXT NOT NULL, candidate_digest TEXT NOT NULL,
       policy_digest TEXT NOT NULL, binding_version INTEGER NOT NULL CHECK(binding_version > 0),
       status TEXT NOT NULL CHECK(status IN ('open','approved','rejected','superseded')),
       revision INTEGER NOT NULL CHECK(revision >= 1), created_at TEXT NOT NULL,
       FOREIGN KEY(ticket_id) REFERENCES central_question_work_tickets(ticket_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )"""
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _no_fault(_point: str) -> None:
    return None


class CentralQuestionLifecycleUnavailable(RuntimeError):
    """A durable lifecycle dependency or its canonical state is unavailable."""


class CentralQuestionLifecycleConflict(RuntimeError):
    """An idempotency key was reused for a different durable command."""


class RouteAuthority(Protocol):
    def authorize_route(
        self, org_id: str, intent: str, agent_id: str, transaction: sqlite3.Connection | None
    ) -> RouteAuthorization | str | None: ...

    def authorize_manager(
        self, org_id: str, manager_id: str, transaction: sqlite3.Connection | None
    ) -> RouteAuthorization | str | None: ...


@dataclass(frozen=True, slots=True)
class RouteAuthorization:
    policy_revision_id: str
    policy_epoch: int
    policy_digest: str


@dataclass(frozen=True, slots=True)
class CardBinding:
    """Current Central Registry/Card binding needed for a sealed route candidate."""

    agent_id: str
    owner_id: str
    revision: int
    card_digest: str = "0" * 64
    concept_ref: str = ""
    coverage_digest: str = "0" * 64


class CardBindingResolver(Protocol):
    """Resolve a canonical Agent Card binding in the lifecycle transaction."""

    def resolve_card_binding(
        self, org_id: str, agent_id: str, transaction: sqlite3.Connection) -> CardBinding: ...


class SqliteProductionCardBindingResolver:
    """Resolve a current production Agent Card without opening another connection."""

    def resolve_card_binding(
        self, org_id: str, agent_id: str, transaction: sqlite3.Connection
    ) -> CardBinding:
        from agent_org_network.sqlite_production_agent_cards import (
            validate_production_agent_card_rows,
        )

        try:
            validate_production_agent_card_rows(transaction, org_id)
            row = transaction.execute(
                "SELECT agent_id,owner_id,revision,card_digest FROM production_agent_cards "
                "WHERE org_id=? AND agent_id=?", (org_id, agent_id)
            ).fetchone()
            if row is None:
                raise CentralQuestionLifecycleUnavailable("card binding unavailable")
            return CardBinding(
                agent_id=str(row["agent_id"]), owner_id=str(row["owner_id"]),
                revision=int(row["revision"]),
                card_digest=str(row["card_digest"]),
                coverage_digest=sha256(
                    _canonical_json(
                        {
                            "candidate_card_id": str(row["agent_id"]),
                            "card_digest": str(row["card_digest"]),
                        }
                    ).encode()
                ).hexdigest(),
            )
        except CentralQuestionLifecycleUnavailable:
            raise
        except Exception as error:
            raise CentralQuestionLifecycleUnavailable("card binding unavailable") from error


@dataclass(frozen=True, slots=True)
class CentralWorkTicket:
    ticket_id: str
    request_id: str
    org_id: str
    owner_id: str
    agent_id: str
    route: RouteTarget
    attempt: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CentralWorkTicketEnqueueResult:
    request: QuestionRequest
    ticket: CentralWorkTicket
    replayed: bool

@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    ticket: CentralWorkTicket
    worker_id: str
    attempt: int


class OwnerDeliveryPort(Protocol):
    """Post-commit notification seam; it cannot mutate the lifecycle UoW."""

    def deliver(self, ticket: CentralWorkTicket) -> None: ...


@dataclass(frozen=True, slots=True)
class OwnerAnswerCandidate:
    """Sealed Owner-side completed text handoff, never a browser DTO."""

    text: str
    sources: tuple[str, ...]
    mode: Literal["full", "backup"] = "full"


@dataclass(frozen=True, slots=True)
class OwnerAnswerIngest:
    """Exact durable WorkTicket-bound internal answer command."""

    ticket_id: str
    request_id: str
    expected_request_revision: int
    attempt: int
    route: RouteTarget
    candidate: OwnerAnswerCandidate
    delivery_subject: str


@dataclass(frozen=True, slots=True)
class ApprovalEvaluation:
    kind: Literal["no_approval", "approval_required"]
    policy_digest: str


class AnswerIngestApprovalPolicy(Protocol):
    def evaluate(
        self, org_id: str, route: RouteTarget, candidate: OwnerAnswerCandidate
    ) -> ApprovalEvaluation: ...


@dataclass(frozen=True, slots=True)
class AnswerIngestAuthorization:
    policy_revision_id: str
    policy_epoch: int
    policy_digest: str


class AnswerIngestAuthority(Protocol):
    def authorize_answer_ingest(
        self,
        org_id: str,
        delivery_subject: str,
        owner_id: str,
        agent_id: str,
        transaction: sqlite3.Connection,
    ) -> AnswerIngestAuthorization | str | None: ...


@dataclass(frozen=True, slots=True)
class OwnerAnswerIngestResult:
    request: QuestionRequest
    record_id: str | None
    approval_item_id: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class AnsweredProjection:
    type: Literal["answered"]
    request_id: str
    state: Literal["answered"]
    retryable: Literal[False]
    record_id: str
    text: str
    answered_by: dict[str, str]
    mode: Literal["full", "backup"]
    sources: tuple[str, ...]
    review_status: Literal["not_required", "approved"]


@dataclass(frozen=True, slots=True)
class ApprovalDispositionCommand:
    request_id: str
    approval_item_id: str
    expected_approval_item_revision: int
    expected_request_revision: int
    principal: AuthenticatedPrincipal
    decision: Literal["approve", "approve_with_edit", "reject"]
    edited_text: str | None
    idempotency_key: str


class ApprovalDispositionAuthority(Protocol):
    def issue_approval_disposition_proof(
        self, principal: AuthenticatedPrincipal, request: QuestionRequest, item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> ApprovalDispositionAuthorizationProof: ...

    def verify_approval_disposition_proof(
        self, proof: ApprovalDispositionAuthorizationProof, request: QuestionRequest, item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ApprovalDispositionAuthorizationProof:
    principal: AuthenticatedPrincipal
    grant: AuthorizationGrant


@dataclass(frozen=True, slots=True)
class QuestionCreateCommand:
    question: str
    idempotency_key: str
    identity_session_id: str
    expected_org_id: str
    expected_requester_id: str


@dataclass(frozen=True, slots=True)
class QuestionCreateAuthorizationProof:
    principal: AuthenticatedPrincipal
    session_grant: AuthorizationGrant
    create_grant: AuthorizationGrant


class QuestionCreateAuthority(Protocol):
    def issue_question_create_proof(
        self, command: QuestionCreateCommand, transaction: sqlite3.Connection,
    ) -> QuestionCreateAuthorizationProof: ...

    def verify_question_create_proof(
        self, proof: QuestionCreateAuthorizationProof, command: QuestionCreateCommand,
        transaction: sqlite3.Connection,
    ) -> bool: ...


class QuestionCreateForbidden(RuntimeError):
    """Current session or create authority cannot establish the caller identity."""


class QuestionCreateApplication:
    """Session-derived create/replay seam; HTTP preflight is deliberately outside this UoW."""

    def __init__(
        self, *, store: CentralQuestionLifecycleStore, authority: QuestionCreateAuthority,
        request_id_factory: Callable[[], str], clock: Callable[[], datetime],
        deadline: Callable[[str, str, datetime], datetime],
    ) -> None:
        self._store = store
        self._authority = authority
        self._request_id_factory = request_id_factory
        self._clock = clock
        self._deadline = deadline

    def create(self, command: QuestionCreateCommand) -> CentralLifecycleCreateResult:
        return self._store.create_from_current_session(
            command, authority=self._authority, request_id_factory=self._request_id_factory,
            clock=self._clock, deadline=self._deadline,
        )


@dataclass(frozen=True, slots=True)
class FeedbackCommand:
    request_id: str
    record_id: str
    principal: AuthenticatedPrincipal
    verdict: Literal["good", "bad"]
    comment: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class FeedbackAuthorizationProof:
    principal: AuthenticatedPrincipal
    session_grant: AuthorizationGrant
    feedback_grant: AuthorizationGrant


class QuestionFeedbackAuthority(Protocol):
    def issue_feedback_proof(
        self, principal: AuthenticatedPrincipal, request: QuestionRequest, record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> FeedbackAuthorizationProof: ...

    def verify_feedback_proof(
        self, proof: FeedbackAuthorizationProof, request: QuestionRequest, record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class QuestionFeedbackResult:
    request_id: str
    record_id: str
    feedback_id: str
    verdict: Literal["good", "bad"]
    submitted_at: datetime
    replayed: bool


class QuestionFeedbackInvalid(ValueError):
    """The internal typed feedback command is malformed."""


class QuestionFeedbackNotFound(RuntimeError):
    """The request or finalized record is missing, foreign, or hidden."""


class QuestionFeedbackConflict(RuntimeError):
    """The idempotency key belongs to a different immutable feedback command."""


class QuestionFeedbackApplication:
    """Private Central feedback UoW; HTTP/BFF presentation intentionally stays outside."""

    def __init__(
        self, *, store: CentralQuestionLifecycleStore, authority: QuestionFeedbackAuthority,
        feedback_id_factory: Callable[[], str] = lambda: str(uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._store = store
        self._authority = authority
        self._feedback_id_factory = feedback_id_factory
        self._clock = clock

    def submit(self, command: FeedbackCommand) -> QuestionFeedbackResult:
        return self._store.submit_feedback(
            command, authority=self._authority, feedback_id_factory=self._feedback_id_factory, clock=self._clock,
        )


class SqliteQuestionCreateAuthority:
    """Transaction-current Browser Session/Registry and `session.read`/`question.create` grants."""

    def __init__(self, *, authorizer: CentralAuthorizer, configured_org_id: str, clock: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self._authorizer = authorizer
        self._org_id = configured_org_id
        self._clock = clock

    def issue_question_create_proof(self, command: QuestionCreateCommand, transaction: sqlite3.Connection) -> QuestionCreateAuthorizationProof:
        principal = self._current_principal(command, transaction)
        session = ResourceRef(org_id=principal.org_id, kind="browser_session", resource_id=principal.identity_session_id, owner_subject_id=principal.subject_id)
        create = ResourceRef(org_id=principal.org_id, kind="question", owner_subject_id=principal.subject_id)
        session_grant = self._grant(principal, "session.read", session)
        create_grant = self._grant(principal, "question.create", create)
        return QuestionCreateAuthorizationProof(principal, session_grant, create_grant)

    def verify_question_create_proof(self, proof: QuestionCreateAuthorizationProof, command: QuestionCreateCommand, transaction: sqlite3.Connection) -> bool:
        try:
            principal = self._current_principal(command, transaction)
            session = ResourceRef(org_id=principal.org_id, kind="browser_session", resource_id=principal.identity_session_id, owner_subject_id=principal.subject_id)
            create = ResourceRef(org_id=principal.org_id, kind="question", owner_subject_id=principal.subject_id)
            return (
                proof.principal == principal
                and self._matches(proof.session_grant, principal, "session.read", session)
                and self._matches(proof.create_grant, principal, "question.create", create)
            )
        except Exception:
            return False

    def _current_principal(self, command: QuestionCreateCommand, transaction: sqlite3.Connection) -> AuthenticatedPrincipal:
        if command.expected_org_id != self._org_id:
            raise QuestionCreateForbidden()
        from agent_org_network.central_browser_auth_sqlite import read_current_browser_session_connection
        session = read_current_browser_session_connection(transaction, command.identity_session_id, now=self._clock())
        if session is None or session.org_id != command.expected_org_id or session.registry_user_id != command.expected_requester_id:
            raise QuestionCreateForbidden()
        return AuthenticatedPrincipal(org_id=session.org_id, subject_id=session.registry_user_id, identity_provider="browser-session", identity_session_id=session.session_digest)

    def _grant(self, principal: AuthenticatedPrincipal, action: Literal["session.read", "question.create"], resource: ResourceRef) -> AuthorizationGrant:
        outcome = self._authorizer.authorize(principal, action, resource)
        if type(outcome) is not AuthorizationGrant or not self._matches(outcome, principal, action, resource):
            raise QuestionCreateForbidden()
        return outcome

    def _matches(self, grant: AuthorizationGrant, principal: AuthenticatedPrincipal, action: Literal["session.read", "question.create"], resource: ResourceRef) -> bool:
        return grant.org_id == principal.org_id and grant.subject_id == principal.subject_id and grant.action == action and grant.resource == resource and self._authorizer.verify(grant, principal, action, resource)


class FileReloadingQuestionCreateAuthority:
    """Reload Central Authority policy for create/replay issue and precommit checks."""

    def __init__(
        self, *, authority_policy_path: Path, configured_org_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        if not authority_policy_path.is_absolute() or not configured_org_id:
            raise ValueError("question create authority configuration required")
        self._path = authority_policy_path
        self._org_id = configured_org_id
        self._clock = clock

    def issue_question_create_proof(
        self, command: QuestionCreateCommand, transaction: sqlite3.Connection,
    ) -> QuestionCreateAuthorizationProof:
        return self._adapter().issue_question_create_proof(command, transaction)

    def verify_question_create_proof(
        self, proof: QuestionCreateAuthorizationProof, command: QuestionCreateCommand,
        transaction: sqlite3.Connection,
    ) -> bool:
        try:
            adapter = self._adapter()
            current = adapter.issue_question_create_proof(command, transaction)
            return (
                current.principal == proof.principal
                and current.session_grant.model_dump(mode="json") == proof.session_grant.model_dump(mode="json")
                and current.create_grant.model_dump(mode="json") == proof.create_grant.model_dump(mode="json")
                and adapter.verify_question_create_proof(current, command, transaction)
            )
        except Exception:
            return False

    def _adapter(self) -> SqliteQuestionCreateAuthority:
        from agent_org_network.central_authority import SnapshotCentralAuthorizer, load_authority_policy_yaml

        snapshot = load_authority_policy_yaml(
            self._path.read_text(encoding="utf-8"), expected_org_id=self._org_id
        )
        return SqliteQuestionCreateAuthority(
            authorizer=SnapshotCentralAuthorizer(snapshot), configured_org_id=self._org_id, clock=self._clock,
        )


class SqliteApprovalDispositionAuthority:
    """Production adapter: current Registry User plus exact Authority grant in the caller's UoW."""

    def __init__(
        self, *, authorizer: CentralAuthorizer, configured_org_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._authorizer = authorizer
        self._org_id = configured_org_id
        self._clock = clock

    def issue_approval_disposition_proof(
        self, principal: AuthenticatedPrincipal, request: QuestionRequest, item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> ApprovalDispositionAuthorizationProof:
        if not self._current_registry_principal(principal, request.org_id, transaction):
            raise CentralQuestionLifecycleUnavailable("approval principal unavailable")
        resource = self._resource(principal, request, item)
        outcome: AuthorizationResult = self._authorizer.authorize(principal, "approval.decide", resource)
        if type(outcome) is not AuthorizationGrant or not self._authorizer.verify(
            outcome, principal, "approval.decide", resource
        ):
            raise CentralQuestionLifecycleUnavailable("approval authority denied")
        return ApprovalDispositionAuthorizationProof(principal=principal, grant=outcome)

    def verify_approval_disposition_proof(
        self, proof: ApprovalDispositionAuthorizationProof, request: QuestionRequest, item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        principal = proof.principal
        if not self._current_registry_principal(principal, request.org_id, transaction):
            return False
        resource = self._resource(principal, request, item)
        grant = proof.grant
        return (
            grant.org_id == request.org_id
            and grant.subject_id == principal.subject_id
            and grant.action == "approval.decide"
            and grant.resource == resource
            and self._authorizer.verify(grant, principal, "approval.decide", resource)
        )

    def _current_registry_principal(
        self, principal: AuthenticatedPrincipal, org_id: str, transaction: sqlite3.Connection
    ) -> bool:
        if type(principal) is not AuthenticatedPrincipal or org_id != self._org_id or principal.org_id != org_id:
            return False
        from agent_org_network.central_browser_auth_sqlite import (
            read_current_browser_session_connection,
        )

        try:
            session = read_current_browser_session_connection(
                transaction, principal.identity_session_id, now=self._clock()
            )
            return session is not None and (
                session.org_id == org_id and session.registry_user_id == principal.subject_id
            )
        except Exception:
            return False

    @staticmethod
    def _resource(principal: AuthenticatedPrincipal, request: QuestionRequest, item: sqlite3.Row) -> ResourceRef:
        return ResourceRef(
            org_id=request.org_id, kind="approval_item",
            resource_id=str(item["approval_item_id"]),
            owner_subject_id=principal.subject_id,
        )


class FileReloadingApprovalDispositionAuthority:
    """Reload the Central policy at every disposition proof issue and precommit verification."""

    def __init__(
        self, *, authority_policy_path: Path, configured_org_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        if not authority_policy_path.is_absolute() or not configured_org_id:
            raise ValueError("approval authority configuration required")
        self._path = authority_policy_path
        self._org_id = configured_org_id
        self._clock = clock

    def issue_approval_disposition_proof(
        self, principal: AuthenticatedPrincipal, request: QuestionRequest, item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> ApprovalDispositionAuthorizationProof:
        return self._adapter().issue_approval_disposition_proof(principal, request, item, transaction)

    def verify_approval_disposition_proof(
        self, proof: ApprovalDispositionAuthorizationProof, request: QuestionRequest, item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        try:
            return self._adapter().verify_approval_disposition_proof(proof, request, item, transaction)
        except Exception:
            return False

    def _adapter(self) -> SqliteApprovalDispositionAuthority:
        from agent_org_network.central_authority import (
            SnapshotCentralAuthorizer,
            load_authority_policy_yaml,
        )

        snapshot = load_authority_policy_yaml(
            self._path.read_text(encoding="utf-8"), expected_org_id=self._org_id
        )
        return SqliteApprovalDispositionAuthority(
            authorizer=SnapshotCentralAuthorizer(snapshot), configured_org_id=self._org_id, clock=self._clock
        )


class SqliteQuestionFeedbackAuthority:
    """Current Browser Session/Registry User plus both required Authority grants in one UoW."""

    def __init__(
        self, *, authorizer: CentralAuthorizer, configured_org_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._authorizer = authorizer
        self._org_id = configured_org_id
        self._clock = clock

    def issue_feedback_proof(
        self, principal: AuthenticatedPrincipal, request: QuestionRequest, record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> FeedbackAuthorizationProof:
        if not self._current(principal, request, transaction):
            raise QuestionFeedbackNotFound()
        session_resource = ResourceRef(
            org_id=request.org_id, kind="browser_session", resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        feedback_resource = self._feedback_resource(principal, request, record)
        session_grant = self._grant(principal, "session.read", session_resource)
        feedback_grant = self._grant(principal, "feedback.create", feedback_resource)
        return FeedbackAuthorizationProof(principal, session_grant, feedback_grant)

    def verify_feedback_proof(
        self, proof: FeedbackAuthorizationProof, request: QuestionRequest, record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        principal = proof.principal
        if not self._current(principal, request, transaction):
            return False
        session_resource = ResourceRef(
            org_id=request.org_id, kind="browser_session", resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        feedback_resource = self._feedback_resource(principal, request, record)
        return (
            self._matches(proof.session_grant, principal, "session.read", session_resource)
            and self._matches(proof.feedback_grant, principal, "feedback.create", feedback_resource)
        )

    def _current(self, principal: AuthenticatedPrincipal, request: QuestionRequest, transaction: sqlite3.Connection) -> bool:
        if type(principal) is not AuthenticatedPrincipal or request.org_id != self._org_id or principal.org_id != request.org_id:
            return False
        from agent_org_network.central_browser_auth_sqlite import read_current_browser_session_connection
        try:
            session = read_current_browser_session_connection(
                transaction, principal.identity_session_id, now=self._clock()
            )
            return session is not None and session.org_id == request.org_id and session.registry_user_id == principal.subject_id
        except Exception:
            return False

    def _grant(self, principal: AuthenticatedPrincipal, action: Literal["session.read", "feedback.create"], resource: ResourceRef) -> AuthorizationGrant:
        outcome = self._authorizer.authorize(principal, action, resource)
        if type(outcome) is not AuthorizationGrant or not self._matches(outcome, principal, action, resource):
            raise QuestionFeedbackNotFound()
        return outcome

    def _matches(self, grant: AuthorizationGrant, principal: AuthenticatedPrincipal, action: Literal["session.read", "feedback.create"], resource: ResourceRef) -> bool:
        return (
            grant.org_id == principal.org_id and grant.subject_id == principal.subject_id
            and grant.action == action and grant.resource == resource
            and self._authorizer.verify(grant, principal, action, resource)
        )

    @staticmethod
    def _feedback_resource(principal: AuthenticatedPrincipal, request: QuestionRequest, record: sqlite3.Row) -> ResourceRef:
        return ResourceRef(
            org_id=request.org_id, kind="question_feedback",
            resource_id=f"{request.request_id}:{record['record_id']}", owner_subject_id=principal.subject_id,
        )


class FileReloadingQuestionFeedbackAuthority:
    """Reload the Central Authority policy for issue and precommit feedback checks."""

    def __init__(
        self, *, authority_policy_path: Path, configured_org_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        if not authority_policy_path.is_absolute() or not configured_org_id:
            raise ValueError("feedback authority configuration required")
        self._path = authority_policy_path
        self._org_id = configured_org_id
        self._clock = clock

    def issue_feedback_proof(
        self, principal: AuthenticatedPrincipal, request: QuestionRequest, record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> FeedbackAuthorizationProof:
        return self._adapter().issue_feedback_proof(principal, request, record, transaction)

    def verify_feedback_proof(
        self, proof: FeedbackAuthorizationProof, request: QuestionRequest, record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        try:
            adapter = self._adapter()
            current = adapter.issue_feedback_proof(proof.principal, request, record, transaction)
            return (
                current.principal == proof.principal
                and current.session_grant.model_dump(mode="json") == proof.session_grant.model_dump(mode="json")
                and current.feedback_grant.model_dump(mode="json") == proof.feedback_grant.model_dump(mode="json")
                and adapter.verify_feedback_proof(current, request, record, transaction)
            )
        except Exception:
            return False

    def _adapter(self) -> SqliteQuestionFeedbackAuthority:
        from agent_org_network.central_authority import SnapshotCentralAuthorizer, load_authority_policy_yaml
        snapshot = load_authority_policy_yaml(
            self._path.read_text(encoding="utf-8"), expected_org_id=self._org_id
        )
        return SqliteQuestionFeedbackAuthority(
            authorizer=SnapshotCentralAuthorizer(snapshot), configured_org_id=self._org_id, clock=self._clock,
        )


@dataclass(frozen=True, slots=True)
class ApprovalDispositionResult:
    request: QuestionRequest
    record_id: str | None
    replayed: bool


class RootManagerResolver(Protocol):
    """Return the current same-org root User/last-resort Manager."""

    def resolve_root_manager(self, org_id: str, transaction: sqlite3.Connection) -> str: ...


class SqliteCentralRegistryRootManagerResolver:
    """Read the canonical root User from the production Registry graph."""

    def resolve_root_manager(self, org_id: str, transaction: sqlite3.Connection) -> str:
        from agent_org_network.sqlite_production_registry_users import (
            validate_production_registry_user_rows,
        )

        try:
            roots = tuple(
                str(row[0]) for row in transaction.execute(
                    "SELECT user_id FROM production_registry_users "
                    "WHERE org_id=? AND manager_id IS NULL ORDER BY user_id", (org_id,)
                )
            )
            if len(roots) != 1:
                raise CentralQuestionLifecycleUnavailable("canonical root unavailable")
            root = validate_production_registry_user_rows(transaction, org_id, roots[0])
            if root.org_id != org_id or root.manager_id is not None:
                raise CentralQuestionLifecycleUnavailable("canonical root unavailable")
            return root.user_id
        except CentralQuestionLifecycleUnavailable:
            raise
        except Exception as error:
            raise CentralQuestionLifecycleUnavailable("canonical root unavailable") from error


class LifecycleRouter(Protocol):
    """Central B1's narrow Router port; it does not import legacy composition."""

    def route(self, question: str) -> Routed | Unowned | Contested: ...


_NON_ACTIONABLE_EXACT_INPUTS = frozenset(
    {"안녕", "안녕하세요", "반가워", "반갑습니다", "hi", "hello", "hey"}
)


def _is_non_actionable_conversation(question: str) -> bool:
    return " ".join(normalize("NFKC", question).casefold().split()) in _NON_ACTIONABLE_EXACT_INPUTS


@dataclass(frozen=True, slots=True)
class CentralLifecycleCreateResult:
    request: QuestionRequest
    replayed: bool


def migrate_central_question_lifecycle_schema(
    path: Path, *, fault_injector: Callable[[str], None] = _no_fault,
) -> None:
    """Create the B1 owned catalog atomically; partial catalogs are never repaired."""
    # RB3.1a already owns the canonical QuestionRequest table.  Import lazily
    # to keep this product seam independent of legacy/demo composition roots.
    from agent_org_network.central_question_request_sqlite import (
        migrate_central_question_request_schema,
    )

    migrate_central_question_request_schema(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        existing = {
            str(row[0])
            for row in connection.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({','.join('?' for _ in _OWNED)})",
                _OWNED,
            )
        }
        has_initial_transition_receipts = (
            "central_question_initial_transition_receipts" in existing
        )
        legacy_existing = existing - {
            "central_question_initial_transition_receipts"
        }
        if not has_initial_transition_receipts and legacy_existing == set(_V14_OWNED):
            if (
                _catalog_signature_for(connection, _V14_OWNED)
                != _EXPECTED_V14_CATALOG
            ):
                raise CentralQuestionLifecycleUnavailable(
                    "v14 lifecycle catalog drift"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(_TABLES[-1])
                for ddl in _INITIAL_TRANSITION_TRIGGERS:
                    connection.execute(ddl)
                _validate_catalog(connection)
                connection.commit()
                return
            except Exception:
                connection.rollback()
                raise
        if has_initial_transition_receipts and legacy_existing == set(_V14_OWNED):
            signature = _catalog_signature(connection)
            if signature in {_EXPECTED_CATALOG, _EXPECTED_V16_APPROVAL_CATALOG}:
                _validate_catalog(connection)
                return
            if _catalog_signature_for(connection, _V13_OWNED) == _EXPECTED_V13_CATALOG:
                _migrate_v13_feedback_audit_org(connection, fault=fault_injector)
                return
            if (
                _catalog_signature_for(connection, _V13_OWNED)
                == _EXPECTED_V13_APPROVAL_V16_CATALOG
            ):
                _migrate_v13_feedback_audit_org(connection, fault=fault_injector)
                return
            if _catalog_signature_for(connection, _V11_OWNED) == _EXPECTED_V11_CATALOG:
                _migrate_v11_authority_proof_commitment(connection, fault=fault_injector)
                return
            if _catalog_signature_for(connection, _V10_OWNED) == _EXPECTED_V10_CATALOG:
                _migrate_v10_approval_authority_proof(connection, fault=fault_injector)
                return
            if _catalog_signature_for(connection, _V9_OWNED) == _EXPECTED_V9_CATALOG:
                _migrate_v9_approval_receipts(connection, fault=fault_injector)
                return
            raise CentralQuestionLifecycleUnavailable("lifecycle catalog drift")
        if legacy_existing == set(_V12_OWNED):
            if _catalog_signature_for(connection, _V12_OWNED) == _EXPECTED_V12_CATALOG:
                _migrate_v12_feedback_catalog(connection, fault=fault_injector)
                return
            if _catalog_signature_for(connection, _V11_OWNED) == _EXPECTED_V11_CATALOG:
                _migrate_v11_authority_proof_commitment(connection, fault=fault_injector)
                return
            if _catalog_signature_for(connection, _V10_OWNED) == _EXPECTED_V10_CATALOG:
                _migrate_v10_approval_authority_proof(connection, fault=fault_injector)
                return
            if _catalog_signature_for(connection, _V9_OWNED) == _EXPECTED_V9_CATALOG:
                _migrate_v9_approval_receipts(connection, fault=fault_injector)
                return
            raise CentralQuestionLifecycleUnavailable("v12 lifecycle catalog drift")
        if legacy_existing == set(_V7_OWNED):
            if _catalog_signature_for(connection, _V7_OWNED) != _EXPECTED_V7_CATALOG:
                raise CentralQuestionLifecycleUnavailable("v7 lifecycle catalog drift")
            _migrate_v7_delivery_catalog(connection)
            return
        if legacy_existing == set(_V8_OWNED):
            if _catalog_signature_for(connection, _V8_OWNED) != _EXPECTED_V8_CATALOG:
                raise CentralQuestionLifecycleUnavailable("v8 lifecycle catalog drift")
            _migrate_v8_approval_catalog(connection, fault=fault_injector)
            return
        if legacy_existing not in (set(), set(_B1_OWNED), set(_B2A_OWNED)):
            raise CentralQuestionLifecycleUnavailable("partial lifecycle catalog")
        connection.execute("BEGIN IMMEDIATE")
        try:
            ddls = (
                _V14_TABLES
                if not legacy_existing
                else _V14_TABLES[len(legacy_existing):]
            )
            for ddl in ddls:
                connection.execute(ddl)
            for ddl in _FEEDBACK_TRIGGERS:
                connection.execute(ddl)
            _ensure_initial_transition_catalog(connection)
            _validate_catalog(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except CentralQuestionLifecycleUnavailable:
        raise
    except Exception as error:
        raise CentralQuestionLifecycleUnavailable("lifecycle schema unavailable") from error
    finally:
        connection.close()


def _ensure_initial_transition_catalog(
    connection: sqlite3.Connection,
) -> None:
    if not _table_exists(
        connection, "central_question_initial_transition_receipts"
    ):
        connection.execute(_TABLES[-1])
        for ddl in _INITIAL_TRANSITION_TRIGGERS:
            connection.execute(ddl)


def _migrate_v7_delivery_catalog(connection: sqlite3.Connection) -> None:
    """Upgrade the exact v7 ticket catalog without losing pending delivery evidence."""
    ticket_rows = tuple(connection.execute("SELECT * FROM central_question_work_tickets"))
    receipt_rows = tuple(connection.execute("SELECT * FROM central_question_work_ticket_receipts"))
    claim_rows = tuple(connection.execute("SELECT * FROM central_question_work_ticket_delivery_claims"))
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE central_question_work_ticket_delivery_claims")
        connection.execute("DROP TABLE central_question_work_ticket_receipts")
        connection.execute("DROP TABLE central_question_work_tickets")
        for ddl in _V14_TABLES[3:]:
            connection.execute(ddl)
        for ddl in _FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        _ensure_initial_transition_catalog(connection)
        connection.executemany(
            "INSERT INTO central_question_work_tickets(request_id,attempt,ticket_id,org_id,owner_id,agent_id,route_json,status,create_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [tuple(row) for row in ticket_rows],
        )
        connection.executemany(
            "INSERT INTO central_question_work_ticket_receipts(request_id,attempt,command_digest,ticket_id,created_at) VALUES (?,?,?,?,?)",
            [tuple(row) for row in receipt_rows],
        )
        connection.executemany(
            "INSERT INTO central_question_work_ticket_delivery_claims(ticket_id,status,worker_id,lease_until,delivery_attempt,ack_receipt,acked_at) VALUES (?,?,?,?,?,?,?)",
            [tuple(row) for row in claim_rows],
        )
        _validate_catalog(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_v8_approval_catalog(
    connection: sqlite3.Connection, *, fault: Callable[[str], None]
) -> None:
    """Add approval resolution state without rewriting immutable ingest evidence."""
    records = tuple(connection.execute("SELECT * FROM central_question_answer_records"))
    items = tuple(connection.execute("SELECT * FROM central_question_approval_items"))
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE central_question_answer_records")
        connection.execute("DROP TABLE central_question_approval_items")
        current_tables = cast(tuple[str, ...], _V14_TABLES)
        connection.execute(current_tables[7])
        connection.execute(current_tables[8])
        connection.execute(current_tables[10])
        connection.execute(current_tables[11])
        for ddl in current_tables[12:]:
            connection.execute(ddl)
        for ddl in _FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        _ensure_initial_transition_catalog(connection)
        fault("v8-to-current-after-schema")
        connection.executemany(
            "INSERT INTO central_question_answer_records(record_id,request_id,ticket_id,org_id,owner_id,agent_id,text,sources_json,mode,review_status,candidate_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["record_id"], row["request_id"], row["ticket_id"], row["org_id"],
                    row["owner_id"], row["agent_id"], row["text"], row["sources_json"],
                    row["mode"], row["review_status"], row["candidate_digest"], row["created_at"],
                )
                for row in records
            ],
        )
        connection.executemany(
            "INSERT INTO central_question_approval_items(approval_item_id,request_id,ticket_id,org_id,owner_id,agent_id,route_json,attempt,candidate_json,candidate_digest,policy_digest,binding_version,status,revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["approval_item_id"], row["request_id"], row["ticket_id"], row["org_id"],
                    row["owner_id"], row["agent_id"], row["route_json"], row["attempt"],
                    row["candidate_json"], row["candidate_digest"], row["policy_digest"],
                    row["binding_version"], row["status"], 1, row["created_at"],
                )
                for row in items
            ],
        )
        fault("v8-to-current-after-copy")
        _validate_catalog(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_v9_approval_receipts(
    connection: sqlite3.Connection, *, fault: Callable[[str], None]
) -> None:
    """Rewrite the v9 receipt/audit pair only after proving one unambiguous successor per item."""
    receipts = tuple(connection.execute("SELECT * FROM central_question_approval_disposition_receipts"))
    audits = tuple(connection.execute("SELECT * FROM central_question_approval_disposition_audits"))
    if receipts or audits:
        # v9 never persisted session-bound Authority proof.  Inventing one
        # would turn a historical terminal decision into forged evidence.
        raise CentralQuestionLifecycleUnavailable("v9 approval authority proof unavailable")
    items = {
        str(row["approval_item_id"]): row
        for row in connection.execute("SELECT * FROM central_question_approval_items")
    }
    receipts_by_item: dict[str, list[sqlite3.Row]] = {}
    audits_by_item: dict[str, list[sqlite3.Row]] = {}
    for row in receipts:
        receipts_by_item.setdefault(str(row["approval_item_id"]), []).append(row)
    for row in audits:
        audits_by_item.setdefault(str(row["approval_item_id"]), []).append(row)
    for item_id, item in items.items():
        item_receipts = receipts_by_item.get(item_id, [])
        item_audits = audits_by_item.get(item_id, [])
        if item["status"] == "open":
            if item_receipts or item_audits:
                raise CentralQuestionLifecycleUnavailable("ambiguous v9 open approval evidence")
            continue
        if item["status"] not in {"approved", "rejected"} or len(item_receipts) != 1 or len(item_audits) != 1:
            raise CentralQuestionLifecycleUnavailable("ambiguous v9 resolved approval evidence")
        receipt, audit = item_receipts[0], item_audits[0]
        if (
            receipt["request_id"] != item["request_id"]
            or audit["request_id"] != receipt["request_id"]
            or audit["receipt_idempotency_key"] != receipt["idempotency_key"]
            or audit["event_kind"] != receipt["terminal_kind"]
        ):
            raise CentralQuestionLifecycleUnavailable("ambiguous v9 disposition audit")
    if set(receipts_by_item) != {item_id for item_id, item in items.items() if item["status"] != "open"}:
        raise CentralQuestionLifecycleUnavailable("orphan v9 disposition receipt")
    if set(audits_by_item) != {item_id for item_id, item in items.items() if item["status"] != "open"}:
        raise CentralQuestionLifecycleUnavailable("orphan v9 disposition audit")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE central_question_approval_disposition_audits")
        connection.execute("DROP TABLE central_question_approval_disposition_receipts")
        current_tables = cast(tuple[str, ...], _V14_TABLES)
        connection.execute(current_tables[10])
        connection.execute(current_tables[11])
        for ddl in current_tables[12:]:
            connection.execute(ddl)
        for ddl in _FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        _ensure_initial_transition_catalog(connection)
        fault("v9-to-current-after-schema")
        fault("v9-to-current-after-copy")
        _validate_catalog(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_v10_approval_authority_proof(
    connection: sqlite3.Connection, *, fault: Callable[[str], None]
) -> None:
    """v10 had no session-bound proof; only unresolved items are safely forward-migratable."""
    if connection.execute("SELECT 1 FROM central_question_approval_disposition_receipts LIMIT 1").fetchone():
        raise CentralQuestionLifecycleUnavailable("v10 approval authority proof unavailable")
    if connection.execute("SELECT 1 FROM central_question_approval_disposition_audits LIMIT 1").fetchone():
        raise CentralQuestionLifecycleUnavailable("v10 approval authority proof unavailable")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE central_question_approval_disposition_audits")
        connection.execute("DROP TABLE central_question_approval_disposition_receipts")
        current_tables = cast(tuple[str, ...], _V14_TABLES)
        connection.execute(current_tables[10])
        connection.execute(current_tables[11])
        for ddl in current_tables[12:]:
            connection.execute(ddl)
        for ddl in _FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        _ensure_initial_transition_catalog(connection)
        fault("v10-to-current-after-schema")
        _validate_catalog(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_v11_authority_proof_commitment(
    connection: sqlite3.Connection, *, fault: Callable[[str], None]
) -> None:
    receipts = tuple(connection.execute("SELECT * FROM central_question_approval_disposition_receipts"))
    audits = tuple(connection.execute("SELECT * FROM central_question_approval_disposition_audits"))
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE central_question_approval_disposition_audits")
        connection.execute("DROP TABLE central_question_approval_disposition_receipts")
        current_tables = cast(tuple[str, ...], _V14_TABLES)
        connection.execute(current_tables[10])
        connection.execute(current_tables[11])
        for ddl in current_tables[12:]:
            connection.execute(ddl)
        for ddl in _FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        _ensure_initial_transition_catalog(connection)
        fault("v11-to-current-after-schema")
        receipt_digests = {
            str(row["receipt_id"]): _authority_proof_digest_from_values(row)
            for row in receipts
        }
        connection.executemany(
            "INSERT INTO central_question_approval_disposition_receipts(receipt_id,org_id,request_id,approval_item_id,actor_id,identity_session_id,authority_policy_version,authority_policy_digest,authority_proof_digest,expected_approval_item_revision,expected_request_revision,decision_kind,decision_digest,edited_text,idempotency_key,candidate_digest,policy_digest,binding_version,terminal_kind,record_id,resolved_item_revision,terminal_request_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["receipt_id"], row["org_id"], row["request_id"], row["approval_item_id"],
                    row["actor_id"], row["identity_session_id"], row["authority_policy_version"],
                    row["authority_policy_digest"], receipt_digests[str(row["receipt_id"])],
                    row["expected_approval_item_revision"], row["expected_request_revision"],
                    row["decision_kind"], row["decision_digest"], row["edited_text"], row["idempotency_key"],
                    row["candidate_digest"], row["policy_digest"], row["binding_version"], row["terminal_kind"],
                    row["record_id"], row["resolved_item_revision"], row["terminal_request_revision"], row["created_at"],
                )
                for row in receipts
            ],
        )
        connection.executemany(
            "INSERT INTO central_question_approval_disposition_audits(receipt_id,approval_item_id,request_id,actor_id,identity_session_id,authority_policy_version,authority_policy_digest,authority_proof_digest,decision_kind,decision_digest,terminal_kind,record_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["receipt_id"], row["approval_item_id"], row["request_id"], row["actor_id"],
                    row["identity_session_id"], row["authority_policy_version"], row["authority_policy_digest"],
                    receipt_digests[str(row["receipt_id"])], row["decision_kind"], row["decision_digest"],
                    row["terminal_kind"], row["record_id"], row["created_at"],
                )
                for row in audits
            ],
        )
        fault("v11-to-current-after-copy")
        _validate_catalog(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_v12_feedback_catalog(
    connection: sqlite3.Connection, *, fault: Callable[[str], None]
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for ddl in _V14_TABLES[12:]:
            connection.execute(ddl)
        for ddl in _FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        _ensure_initial_transition_catalog(connection)
        fault("v12-to-current-after-schema")
        _validate_catalog(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_v13_feedback_audit_org(
    connection: sqlite3.Connection, *, fault: Callable[[str], None]
) -> None:
    audits = tuple(connection.execute("SELECT * FROM central_question_feedback_audits"))
    records = {
        str(row["feedback_id"]): row
        for row in connection.execute("SELECT feedback_id,org_id FROM central_question_feedback_records")
    }
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute(cast(tuple[str, ...], _TABLES)[14])
        connection.execute(_FEEDBACK_TRIGGERS[4])
        connection.execute(_FEEDBACK_TRIGGERS[5])
        _ensure_initial_transition_catalog(connection)
        fault("v13-to-current-after-schema")
        connection.executemany(
            "INSERT INTO central_question_feedback_audits(receipt_id,feedback_id,org_id,request_id,record_id,requester_id,verdict,payload_digest,submitted_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["receipt_id"], row["feedback_id"], records[str(row["feedback_id"])]["org_id"],
                    row["request_id"], row["record_id"], row["requester_id"], row["verdict"],
                    row["payload_digest"], row["submitted_at"],
                )
                for row in audits
            ],
        )
        fault("v13-to-current-after-copy")
        _validate_catalog(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def central_question_lifecycle_schema_ready(path: Path) -> bool:
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


def validate_central_question_lifecycle_connection(
    connection: sqlite3.Connection,
) -> None:
    """Validate the canonical lifecycle and every reverse link in one snapshot."""
    _validate_catalog(connection)


class CentralQuestionLifecycleStore:
    """Canonical QuestionRequest, WorkTicket, and at-least-once delivery persistence."""

    workflow_durability = "durable"

    def __init__(self, path: Path, *, fault_injector: Callable[[str], None] | None = None, root_manager_resolver: RootManagerResolver | None = None, card_binding_resolver: CardBindingResolver | None = None) -> None:
        if not central_question_lifecycle_schema_ready(path):
            raise CentralQuestionLifecycleUnavailable("lifecycle schema unavailable")
        self._connection = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._lock = RLock()
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        self._root_manager_resolver = root_manager_resolver
        self._card_binding_resolver = card_binding_resolver
        try:
            self._validate()
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_or_replay(self, request: QuestionRequest, *, idempotency_key: str) -> CentralLifecycleCreateResult:
        if type(request) is not QuestionRequest or not _valid_idempotency_key(idempotency_key):
            raise CentralQuestionLifecycleUnavailable("invalid lifecycle command")
        question_json = _canonical_json({"question": request.question})
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._validate()
                receipt = self._connection.execute(
                    "SELECT question_json,received_json,request_id FROM central_question_create_receipts "
                    "WHERE org_id=? AND requester_id=? AND idempotency_key=?",
                    (request.org_id, request.requester_id, idempotency_key),
                ).fetchone()
                if receipt is not None:
                    if receipt["question_json"] != question_json:
                        raise CentralQuestionLifecycleConflict()
                    existing = _receipt_received_request(cast(str, receipt["received_json"]))
                    current = _read_request(self._connection, str(receipt["request_id"]))
                    if current is None or current.request_id != existing.request_id:
                        raise CentralQuestionLifecycleUnavailable("receipt without request")
                    _append_question_received_evidence(
                        self._connection, existing, idempotency_key, grant=None
                    )
                    self._connection.commit()
                    return CentralLifecycleCreateResult(existing, True)
                _insert_request(self._connection, request)
                self._fault("after-received-before-create-receipt")
                self._connection.execute(
                    "INSERT INTO central_question_create_receipts "
                    "(org_id,requester_id,idempotency_key,question_json,received_json,request_id,"
                    "command_digest,authority_policy_revision_id,authority_policy_epoch,"
                    "authority_policy_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (request.org_id, request.requester_id, idempotency_key, question_json,
                     _canonical_json(request.model_dump(mode="json")), request.request_id,
                     _question_create_command_digest(request, idempotency_key),
                     None, None, None, request.created_at.isoformat()),
                )
                _append_question_received_evidence(
                    self._connection, request, idempotency_key, grant=None
                )
                self._connection.commit()
                return CentralLifecycleCreateResult(request, False)
            except CentralQuestionLifecycleConflict:
                self._connection.rollback()
                raise
            except Exception as error:
                self._connection.rollback()
                raise CentralQuestionLifecycleUnavailable("lifecycle create unavailable") from error

    def create_from_current_session(
        self, command: QuestionCreateCommand, *, authority: QuestionCreateAuthority,
        request_id_factory: Callable[[], str], clock: Callable[[], datetime],
        deadline: Callable[[str, str, datetime], datetime],
    ) -> CentralLifecycleCreateResult:
        _validate_question_create_command(command)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._validate()
                proof = authority.issue_question_create_proof(command, self._connection)
                _require_question_create_proof(proof, command)
                question_json = _canonical_json({"question": command.question})
                receipt = self._connection.execute(
                    "SELECT question_json,received_json,request_id FROM central_question_create_receipts "
                    "WHERE org_id=? AND requester_id=? AND idempotency_key=?",
                    (proof.principal.org_id, proof.principal.subject_id, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    if receipt["question_json"] != question_json:
                        raise CentralQuestionLifecycleConflict()
                    if not authority.verify_question_create_proof(proof, command, self._connection):
                        raise QuestionCreateForbidden()
                    existing = _receipt_received_request(cast(str, receipt["received_json"]))
                    current = _read_request(self._connection, str(receipt["request_id"]))
                    if current is None or current.request_id != existing.request_id:
                        raise CentralQuestionLifecycleUnavailable("receipt without current request")
                    _append_question_received_evidence(
                        self._connection, existing, command.idempotency_key,
                        grant=proof.create_grant,
                    )
                    self._connection.commit()
                    return CentralLifecycleCreateResult(existing, True)
                at = clock()
                if at.tzinfo is None:
                    raise CentralQuestionLifecycleUnavailable("question create clock unavailable")
                due = deadline(proof.principal.org_id, "received", at)
                if due.tzinfo is None or due < at:
                    raise CentralQuestionLifecycleUnavailable("question create deadline unavailable")
                request = QuestionRequest.receive(
                    org_id=proof.principal.org_id, requester_id=proof.principal.subject_id, question=command.question,
                    request_id_factory=request_id_factory, clock=lambda: at, due_at=due,
                    session_id=proof.principal.identity_session_id,
                )
                if not authority.verify_question_create_proof(proof, command, self._connection):
                    raise QuestionCreateForbidden()
                _insert_request(self._connection, request)
                self._fault("after-browser-received-before-create-receipt")
                audit_authority = canonical_v19_file_authority(
                    source_policy_digest=proof.create_grant.policy_digest,
                    current_snapshot_digest=proof.create_grant.policy_digest,
                )
                self._connection.execute(
                    "INSERT INTO central_question_create_receipts "
                    "(org_id,requester_id,idempotency_key,question_json,received_json,request_id,"
                    "command_digest,authority_policy_revision_id,authority_policy_epoch,"
                    "authority_policy_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (request.org_id, request.requester_id, command.idempotency_key, question_json,
                     _canonical_json(request.model_dump(mode="json")), request.request_id,
                     _question_create_command_digest(request, command.idempotency_key),
                     audit_authority.policy_revision_id, audit_authority.policy_epoch,
                     audit_authority.policy_digest, request.created_at.isoformat()),
                )
                _append_question_received_evidence(
                    self._connection, request, command.idempotency_key,
                    grant=proof.create_grant,
                )
                self._connection.commit()
                return CentralLifecycleCreateResult(request, False)
            except (QuestionCreateForbidden, CentralQuestionLifecycleConflict):
                self._connection.rollback()
                raise
            except Exception as error:
                self._connection.rollback()
                raise CentralQuestionLifecycleUnavailable("browser question create unavailable") from error

    def get(self, request_id: str) -> QuestionRequest | None:
        with self._lock:
            try:
                self._validate()
                return _read_request(self._connection, request_id)
            except CentralQuestionLifecycleUnavailable:
                raise
            except Exception as error:
                raise CentralQuestionLifecycleUnavailable("lifecycle read unavailable") from error

    def record_initial(self, current: QuestionRequest, updated: QuestionRequest, *, manager: tuple[str, str] | None = None, conflict: tuple[str, str, str] | None = None, authority: RouteAuthority | None = None) -> QuestionRequest:
        """Commit one canonical initial transition and its linked B1 aggregate together."""
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                # The canonical root must be read only after this write
                # transaction starts and through this same connection.  A
                # pre-read resolver answer could be stale by the time the
                # ManagerItem and AwaitingManager transition commit.
                self._validate()
                _validate_initial_aggregate_shape(current, updated, manager=manager, conflict=conflict)
                stored = _read_request(self._connection, current.request_id)
                if stored != current:
                    raise CentralQuestionLifecycleConflict()
                validate_compare_and_set_semantics(current.request_id, current.revision, current, updated)
                initial_authorization: RouteAuthorization | str | None = None
                if manager is not None:
                    item_id, router_manager_id = manager
                    resolver = self._root_manager_resolver
                    if resolver is None:
                        raise CentralQuestionLifecycleUnavailable("root manager resolver unavailable")
                    manager_id = resolver.resolve_root_manager(current.org_id, self._connection)
                    if router_manager_id != manager_id:
                        raise CentralQuestionLifecycleUnavailable("unowned root manager mismatch")
                    initial_authorization = (
                        None if authority is None else authority.authorize_manager(
                            current.org_id, manager_id, self._connection
                        )
                    )
                    if not initial_authorization:
                        raise CentralQuestionLifecycleUnavailable("manager authority denied")
                    self._connection.execute(
                        "INSERT INTO central_question_manager_items(request_id,org_id,item_id,manager_id,intent,created_at) VALUES (?,?,?,?,?,?)",
                        (current.request_id, current.org_id, item_id, manager_id, updated.intent, updated.updated_at.isoformat()),
                    )
                if conflict is not None:
                    case_id, intent, candidates_json = conflict
                    if not isinstance(updated.state, AwaitingConflict):
                        raise CentralQuestionLifecycleUnavailable("conflict/request binding mismatch")
                    resolver = self._card_binding_resolver
                    if resolver is None:
                        raise CentralQuestionLifecycleUnavailable("card binding resolver unavailable")
                    candidates = _conflict_candidates(candidates_json)
                    if not candidates or len({candidate["agent_id"] for candidate in candidates}) != len(candidates):
                        raise CentralQuestionLifecycleUnavailable("invalid conflict candidates")
                    conflict_bindings: list[CardBinding] = []
                    for candidate in candidates:
                        binding = resolver.resolve_card_binding(current.org_id, candidate["agent_id"], self._connection)
                        if (
                            binding.agent_id != candidate["agent_id"]
                            or binding.owner_id != candidate["owner_id"]
                            or binding.revision < 1
                        ):
                            raise CentralQuestionLifecycleUnavailable("conflict candidate binding mismatch")
                        candidate_authorization = (
                            None if authority is None else authority.authorize_route(
                                current.org_id, intent, binding.agent_id,
                                self._connection,
                            )
                        )
                        if not candidate_authorization:
                            raise CentralQuestionLifecycleUnavailable("route authority denied")
                        if initial_authorization is None:
                            initial_authorization = candidate_authorization
                        elif candidate_authorization != initial_authorization:
                            raise CentralQuestionLifecycleUnavailable(
                                "route Authority provenance drift"
                            )
                        conflict_bindings.append(binding)
                    self._connection.execute(
                        "INSERT INTO central_question_conflict_cases(request_id,org_id,case_id,intent,candidates_json,created_at) VALUES (?,?,?,?,?,?)",
                        (current.request_id, current.org_id, case_id, intent, candidates_json, updated.updated_at.isoformat()),
                    )
                    from agent_org_network.central_inbox_conflict import (
                        insert_initial_conflict_companion,
                    )

                    insert_initial_conflict_companion(
                        self._connection,
                        request=updated,
                        case_id=case_id,
                        intent=intent,
                        bindings=tuple(conflict_bindings),
                    )
                if isinstance(updated.state, ReadyToDispatch):
                    resolver = self._card_binding_resolver
                    if resolver is None:
                        raise CentralQuestionLifecycleUnavailable("card binding resolver unavailable")
                    binding = resolver.resolve_card_binding(
                        current.org_id, updated.state.route.agent_id, self._connection
                    )
                    if binding.agent_id != updated.state.route.agent_id or binding.revision < 1:
                        raise CentralQuestionLifecycleUnavailable("route card binding mismatch")
                    route_grant = None if authority is None else authority.authorize_route(
                        current.org_id, updated.state.route.intent, binding.agent_id, self._connection
                    )
                    route_version = (
                        route_grant.policy_revision_id
                        if type(route_grant) is RouteAuthorization
                        else route_grant
                    )
                    if route_version != updated.state.route.authority_version:
                        raise CentralQuestionLifecycleUnavailable("route authority denied")
                    initial_authorization = route_grant
                if initial_authorization is None and authority is not None:
                    current_authority = getattr(authority, "current_authority", None)
                    if callable(current_authority):
                        current_result = current_authority(self._connection)
                        if type(current_result) in {
                            RouteAuthorization, str
                        }:
                            initial_authorization = cast(
                                RouteAuthorization | str, current_result
                            )
                if manager is not None:
                    self._fault("before-unowned-commit")
                _append_initial_transition_evidence(
                    self._connection, current=current, updated=updated,
                    manager_item_id=None if manager is None else manager[0],
                    authorization=initial_authorization,
                )
                _cas_request(self._connection, current, updated)
                self._connection.commit()
                return updated
            except CentralQuestionLifecycleConflict:
                self._connection.rollback()
                raise
            except Exception as error:
                self._connection.rollback()
                raise CentralQuestionLifecycleUnavailable("lifecycle disposition unavailable") from error

    def manager_item_id(self, request_id: str) -> str | None:
        return self._linked_id("central_question_manager_items", "item_id", request_id)

    def conflict_case_id(self, request_id: str) -> str | None:
        return self._linked_id("central_question_conflict_cases", "case_id", request_id)

    def enqueue_ready_to_dispatch(
        self, current: QuestionRequest, *, ticket_id: str,
        authority: RouteAuthority | None = None,
    ) -> CentralWorkTicketEnqueueResult:
        """Atomically create the one pending WorkTicket for a frozen route attempt."""
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._validate()
                stored = _read_request(self._connection, current.request_id)
                if stored is None:
                    raise CentralQuestionLifecycleUnavailable("request unavailable")
                if isinstance(stored.state, AwaitingAnswer):
                    ticket = self._work_ticket(stored.request_id, stored.state.attempt)
                    if ticket is None:
                        raise CentralQuestionLifecycleUnavailable("awaiting answer without ticket")
                    dispatch_authorization = (
                        None if authority is None else authority.authorize_route(
                            stored.org_id, stored.state.route.intent,
                            stored.state.route.agent_id, self._connection,
                        )
                    )
                    _append_dispatch_state_evidence(
                        self._connection, ticket=ticket, request_id=stored.request_id,
                        authorization=dispatch_authorization,
                    )
                    self._connection.commit()
                    return CentralWorkTicketEnqueueResult(stored, ticket, True)
                if stored != current or not isinstance(current.state, ReadyToDispatch):
                    raise CentralQuestionLifecycleConflict()
                resolver = self._card_binding_resolver
                if resolver is None:
                    raise CentralQuestionLifecycleUnavailable("card binding resolver unavailable")
                binding = resolver.resolve_card_binding(
                    current.org_id, current.state.route.agent_id, self._connection
                )
                if binding.agent_id != current.state.route.agent_id or binding.revision < 1:
                    raise CentralQuestionLifecycleUnavailable("work ticket card binding mismatch")
                dispatch_authorization = (
                    None if authority is None else authority.authorize_route(
                        current.org_id, current.state.route.intent,
                        current.state.route.agent_id, self._connection,
                    )
                )
                dispatch_version = (
                    dispatch_authorization.policy_revision_id
                    if type(dispatch_authorization) is RouteAuthorization
                    else dispatch_authorization
                )
                if dispatch_version != current.state.route.authority_version:
                    raise CentralQuestionLifecycleUnavailable(
                        "work ticket route authority unavailable"
                    )
                at = current.updated_at
                updated = current.transition(
                    AwaitingAnswer(
                        route=current.state.route,
                        attempt=current.state.attempt,
                        ticket_id=ticket_id,
                        handling=HandlingAssignment(
                            kind="runtime_ticket", ref=ticket_id, due_at=current.state.handling.due_at
                        ),
                    ),
                    clock=lambda: at,
                )
                ticket = CentralWorkTicket(
                    ticket_id=ticket_id, request_id=current.request_id, org_id=current.org_id,
                    owner_id=binding.owner_id, agent_id=binding.agent_id, route=current.state.route,
                    attempt=current.state.attempt, created_at=updated.updated_at,
                )
                digest = _work_ticket_digest(ticket)
                dispatch_audit_authority = (
                    canonical_v19_file_authority(
                        source_policy_digest=dispatch_authorization.policy_digest,
                        current_snapshot_digest=dispatch_authorization.policy_digest,
                    )
                    if type(dispatch_authorization) is RouteAuthorization
                    else None
                )
                self._connection.execute(
                    "INSERT INTO central_question_work_tickets(request_id,attempt,ticket_id,org_id,owner_id,agent_id,route_json,status,create_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ticket.request_id, ticket.attempt, ticket.ticket_id, ticket.org_id, ticket.owner_id,
                     ticket.agent_id, _canonical_json(ticket.route.model_dump(mode="json")), "pending", digest,
                     ticket.created_at.isoformat()),
                )
                self._connection.execute(
                    "INSERT INTO central_question_work_ticket_receipts("
                    "request_id,attempt,command_digest,ticket_id,"
                    "authority_policy_revision_id,authority_policy_epoch,"
                    "authority_policy_digest,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        ticket.request_id, ticket.attempt, digest, ticket.ticket_id,
                        None if dispatch_audit_authority is None else dispatch_audit_authority.policy_revision_id,
                        None if dispatch_audit_authority is None else dispatch_audit_authority.policy_epoch,
                        None if dispatch_audit_authority is None else dispatch_audit_authority.policy_digest,
                        ticket.created_at.isoformat(),
                    ),
                )
                _append_dispatch_state_evidence(
                    self._connection, ticket=ticket,
                    request_id=current.request_id,
                    authorization=dispatch_authorization,
                )
                self._fault("before-work-ticket-commit")
                _cas_request(self._connection, current, updated)
                self._connection.commit()
                return CentralWorkTicketEnqueueResult(updated, ticket, False)
            except CentralQuestionLifecycleConflict:
                self._connection.rollback()
                raise
            except Exception as error:
                self._connection.rollback()
                raise CentralQuestionLifecycleUnavailable("work ticket enqueue unavailable") from error

    def _work_ticket(self, request_id: str, attempt: int) -> CentralWorkTicket | None:
        row = self._connection.execute(
            "SELECT * FROM central_question_work_tickets WHERE request_id=? AND attempt=?",
            (request_id, attempt),
        ).fetchone()
        return None if row is None else _row_work_ticket(row)

    def _work_ticket_by_id(self, ticket_id: str) -> CentralWorkTicket | None:
        row = self._connection.execute(
            "SELECT * FROM central_question_work_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        return None if row is None else _row_work_ticket(row)

    def claim_delivery(
        self, request_id: str, worker_id: str, now: datetime, lease_for: timedelta
    ) -> DeliveryClaim | None:
        """Claim one pending WorkTicket after its commit, or return no active claim.

        A lease is deliberately not an exactly-once promise: a caller that
        times out after delivery leaves the lease intact, and a later worker
        may redeliver the same stable ticket only after it expires.
        """
        if (
            type(request_id) is not str
            or not request_id.strip()
            or type(worker_id) is not str
            or not worker_id.strip()
            or now.tzinfo is None
            or lease_for <= timedelta(0)
        ):
            raise CentralQuestionLifecycleUnavailable("invalid delivery claim")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._validate()
                request = _read_request(self._connection, request_id)
                if request is None or not isinstance(request.state, AwaitingAnswer):
                    self._connection.commit()
                    return None
                ticket = self._work_ticket(request_id, request.state.attempt)
                if ticket is None:
                    raise CentralQuestionLifecycleUnavailable("awaiting answer without ticket")
                row = self._connection.execute(
                    "SELECT * FROM central_question_work_ticket_delivery_claims WHERE ticket_id=?",
                    (ticket.ticket_id,),
                ).fetchone()
                if row is not None and (
                    row["status"] == "delivered"
                    or _parse_timestamp(str(row["lease_until"])) > now
                ):
                    self._connection.commit()
                    return None
                attempt = 1 if row is None else int(row["delivery_attempt"]) + 1
                self._connection.execute(
                    "INSERT INTO central_question_work_ticket_delivery_claims("
                    "ticket_id,status,worker_id,lease_until,delivery_attempt,ack_receipt,acked_at) "
                    "VALUES (?,?,?,?,?,NULL,NULL) ON CONFLICT(ticket_id) DO UPDATE SET "
                    "status='leased',worker_id=excluded.worker_id,lease_until=excluded.lease_until,"
                    "delivery_attempt=excluded.delivery_attempt,ack_receipt=NULL,acked_at=NULL",
                    (ticket.ticket_id, "leased", worker_id, (now + lease_for).isoformat(), attempt),
                )
                self._connection.commit()
                return DeliveryClaim(ticket, worker_id, attempt)
            except Exception as error:
                self._connection.rollback()
                raise CentralQuestionLifecycleUnavailable("delivery claim unavailable") from error

    def acknowledge_delivery(self, claim: DeliveryClaim, now: datetime) -> None:
        if (
            type(claim) is not DeliveryClaim
            or now.tzinfo is None
            or not claim.worker_id.strip()
            or claim.attempt < 1
        ):
            raise CentralQuestionLifecycleUnavailable("invalid delivery acknowledgement")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._validate()
                cursor = self._connection.execute(
                    "UPDATE central_question_work_ticket_delivery_claims SET status='delivered',"
                    "ack_receipt=?,acked_at=? WHERE ticket_id=? AND status='leased' "
                    "AND worker_id=? AND delivery_attempt=? AND lease_until>?",
                    (
                        _delivery_ack_receipt(claim.ticket.ticket_id, claim.attempt), now.isoformat(),
                        claim.ticket.ticket_id, claim.worker_id, claim.attempt, now.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise CentralQuestionLifecycleUnavailable("delivery claim lost")
                self._connection.commit()
            except Exception as error:
                self._connection.rollback()
                raise CentralQuestionLifecycleUnavailable("delivery acknowledgement unavailable") from error

    def ingest_owner_answer(
        self,
        command: OwnerAnswerIngest,
        *,
        approval_policy: AnswerIngestApprovalPolicy,
        authority: AnswerIngestAuthority,
        record_id_factory: Callable[[], str],
        approval_item_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
        approval_deadline: Callable[[str, datetime], datetime],
    ) -> OwnerAnswerIngestResult:
        """Consume one verified delivery and atomically finalize or open approval."""
        _validate_owner_answer_ingest(command)
        candidate_json = _candidate_json(command.candidate)
        candidate_digest = sha256(candidate_json.encode()).hexdigest()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._validate()
                receipt = self._connection.execute(
                    "SELECT * FROM central_question_answer_ingest_receipts WHERE ticket_id=?",
                    (command.ticket_id,),
                ).fetchone()
                if receipt is not None:
                    result = self._replay_ingest(
                        receipt, command, candidate_json, candidate_digest, approval_policy, authority
                    )
                    self._connection.commit()
                    return result
                request = _read_request(self._connection, command.request_id)
                ticket = self._require_ingest_binding(command, request, authority)
                if request is None:
                    raise CentralQuestionLifecycleUnavailable("answer ingest request unavailable")
                binding = self._require_ingest_card_binding(ticket)
                evaluation = _validate_approval_evaluation(
                    approval_policy.evaluate(ticket.org_id, ticket.route, command.candidate)
                )
                authorization = authority.authorize_answer_ingest(
                    ticket.org_id, command.delivery_subject, ticket.owner_id, ticket.agent_id, self._connection
                )
                if type(authorization) is AnswerIngestAuthorization:
                    authority_version = authorization.policy_revision_id
                elif type(authorization) is str and authorization.strip():
                    authority_version = authorization
                else:
                    raise CentralQuestionLifecycleUnavailable("answer ingest authority denied")
                at = clock()
                if at.tzinfo is None:
                    raise CentralQuestionLifecycleUnavailable("answer ingest clock unavailable")
                if evaluation.kind == "no_approval":
                    record_id = record_id_factory()
                    updated = request.transition(AnsweredRequest(record_id=record_id), clock=lambda: at)
                    self._insert_answer_ingest_receipt(
                        ticket, command, candidate_json, candidate_digest, evaluation, binding.revision,
                        authority_version, authorization, "answered", record_id, None, at,
                    )
                    self._fault("after-answer-ingest-receipt")
                    self._connection.execute(
                        "INSERT INTO central_question_answer_records(record_id,request_id,ticket_id,org_id,owner_id,agent_id,text,sources_json,mode,review_status,candidate_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (record_id, request.request_id, ticket.ticket_id, ticket.org_id, ticket.owner_id,
                         ticket.agent_id, command.candidate.text, _canonical_json(list(command.candidate.sources)),
                         command.candidate.mode, "not_required", candidate_digest, at.isoformat()),
                    )
                    self._fault("after-answer-record")
                    self._insert_answer_ingest_audit(
                        ticket, request, command, authorization, "answered", at,
                        candidate_digest, record_id, None,
                    )
                    from agent_org_network.central_inbox_review import (
                        append_backup_review_intent,
                    )

                    append_backup_review_intent(self._connection, record_id)
                    self._complete_ticket_and_release_lease(ticket.ticket_id)
                    self._fault("before-answered-request-cas")
                    _cas_request(self._connection, request, updated)
                    self._connection.commit()
                    return OwnerAnswerIngestResult(updated, record_id, None, False)
                approval_item_id = approval_item_id_factory()
                due_at = approval_deadline(ticket.org_id, at)
                if due_at.tzinfo is None or due_at < at:
                    raise CentralQuestionLifecycleUnavailable("approval deadline unavailable")
                updated = request.transition(
                    AwaitingApproval(
                        route=ticket.route,
                        attempt=ticket.attempt,
                        draft_ref=approval_item_id,
                        handling=HandlingAssignment(
                            kind="approval_item", ref=approval_item_id, due_at=due_at
                        ),
                    ),
                    clock=lambda: at,
                )
                self._insert_answer_ingest_receipt(
                    ticket, command, candidate_json, candidate_digest, evaluation, binding.revision,
                    authority_version, authorization, "awaiting_approval", None, approval_item_id, at,
                )
                self._fault("after-answer-ingest-receipt")
                self._connection.execute(
                    "INSERT INTO central_question_approval_items(approval_item_id,request_id,ticket_id,org_id,owner_id,agent_id,route_json,attempt,candidate_json,candidate_digest,policy_digest,binding_version,status,revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (approval_item_id, request.request_id, ticket.ticket_id, ticket.org_id, ticket.owner_id,
                     ticket.agent_id, _canonical_json(ticket.route.model_dump(mode="json")), ticket.attempt,
                     candidate_json, candidate_digest, evaluation.policy_digest, binding.revision, "open", 1, at.isoformat()),
                )
                from agent_org_network.central_inbox_approval import (
                    ApprovalInboxUnavailable,
                    insert_initial_approval_assignment,
                )

                try:
                    insert_initial_approval_assignment(
                        self._connection,
                        approval_item_id=approval_item_id,
                        request_id=request.request_id,
                        ticket_id=ticket.ticket_id,
                        org_id=ticket.org_id,
                        assigned_approver_user_id=ticket.owner_id,
                        assigned_approval_card_id=ticket.agent_id,
                        assigned_card_revision=binding.revision,
                        assigned_card_digest=binding.card_digest,
                        assigned_at=at,
                        due_at=due_at,
                    )
                except ApprovalInboxUnavailable as error:
                    raise CentralQuestionLifecycleUnavailable(
                        "approval inbox assignment unavailable"
                    ) from error
                self._fault("after-approval-item")
                self._insert_answer_ingest_audit(
                    ticket, request, command, authorization, "awaiting_approval", at,
                    candidate_digest, None, approval_item_id,
                )
                self._complete_ticket_and_release_lease(ticket.ticket_id)
                self._fault("before-awaiting-approval-request-cas")
                _cas_request(self._connection, request, updated)
                self._connection.commit()
                return OwnerAnswerIngestResult(updated, None, approval_item_id, False)
            except CentralQuestionLifecycleConflict:
                self._connection.rollback()
                raise
            except Exception as error:
                self._connection.rollback()
                raise CentralQuestionLifecycleUnavailable("answer ingest unavailable") from error

    def answered_projection(self, request_id: str) -> AnsweredProjection | None:
        with self._lock:
            try:
                self._validate()
                request = _read_request(self._connection, request_id)
                if request is None:
                    return None
                if not isinstance(request.state, AnsweredRequest):
                    return None
                row = self._connection.execute(
                    "SELECT * FROM central_question_answer_records WHERE record_id=? AND request_id=?",
                    (request.state.record_id, request_id),
                ).fetchone()
                if row is None:
                    raise CentralQuestionLifecycleUnavailable("answered projection unavailable")
                correction = None
                if self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='central_inbox_answer_correction_records'"
                ).fetchone() is not None:
                    from agent_org_network.central_inbox_review import (
                        validate_central_inbox_review_connection,
                    )

                    validate_central_inbox_review_connection(self._connection)
                    correction = self._connection.execute(
                        "SELECT * FROM central_inbox_answer_correction_records "
                        "WHERE supersedes_record_id=? AND request_id=?",
                        (request.state.record_id, request_id),
                    ).fetchone()
                sources = _safe_sources(str(row["sources_json"]))
                return AnsweredProjection(
                    type="answered", request_id=request_id, state="answered", retryable=False,
                    record_id=(
                        str(correction["correction_record_id"])
                        if correction is not None
                        else str(row["record_id"])
                    ),
                    text=(
                        str(correction["text"])
                        if correction is not None
                        else str(row["text"])
                    ),
                    answered_by={"owner": str(row["owner_id"]), "agent_id": str(row["agent_id"])},
                    mode=(
                        "full"
                        if correction is not None
                        else cast(Literal["full", "backup"], row["mode"])
                    ),
                    sources=sources,
                    review_status=(
                        "approved"
                        if correction is not None
                        else cast(Literal["not_required", "approved"], row["review_status"])
                    ),
                )
            except CentralQuestionLifecycleUnavailable:
                raise
            except Exception as error:
                raise CentralQuestionLifecycleUnavailable("answered projection unavailable") from error

    def dispose_approval(
        self, command: ApprovalDispositionCommand, *, authority: ApprovalDispositionAuthority,
        record_id_factory: Callable[[], str], clock: Callable[[], datetime],
    ) -> ApprovalDispositionResult:
        _validate_approval_disposition(command)
        digest = _approval_decision_digest(command)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._validate()
                receipt = self._connection.execute(
                    "SELECT * FROM central_question_approval_disposition_receipts WHERE org_id=(SELECT org_id FROM question_requests WHERE request_id=?) AND request_id=? AND approval_item_id=? AND actor_id=? AND expected_approval_item_revision=? AND expected_request_revision=? AND decision_kind=? AND decision_digest=? AND idempotency_key=?",
                    (command.request_id, command.request_id, command.approval_item_id, command.principal.subject_id,
                     command.expected_approval_item_revision, command.expected_request_revision,
                     command.decision, digest, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    result = self._replay_approval_disposition(receipt, command, authority)
                    self._connection.commit()
                    return result
                request = _read_request(self._connection, command.request_id)
                item = self._connection.execute(
                    "SELECT * FROM central_question_approval_items WHERE approval_item_id=?", (command.approval_item_id,)
                ).fetchone()
                if request is None or item is None or not isinstance(request.state, AwaitingApproval):
                    raise CentralQuestionLifecycleConflict("approval disposition pre-state conflict")
                if (
                    request.org_id != item["org_id"]
                    or request.state.draft_ref != command.approval_item_id
                    or request.revision != command.expected_request_revision
                    or item["revision"] != command.expected_approval_item_revision
                    or item["status"] != "open"
                    or request.state.route != _ROUTE.validate_json(cast(str, item["route_json"]), strict=True)
                    or request.state.attempt != item["attempt"]
                ):
                    raise CentralQuestionLifecycleConflict("approval disposition snapshot conflict")
                proof = authority.issue_approval_disposition_proof(
                    command.principal, request, item, self._connection
                )
                _require_approval_disposition_proof(proof, command.principal, request, item)
                self._require_approval_card_binding(request, item)
                at = clock()
                if at.tzinfo is None:
                    raise CentralQuestionLifecycleUnavailable("approval clock unavailable")
                candidate = _candidate_json_from_raw(cast(str, item["candidate_json"]))
                if candidate is None:
                    raise CentralQuestionLifecycleUnavailable("approval candidate unavailable")
                terminal_kind: Literal["answered", "declined"] = "declined" if command.decision == "reject" else "answered"
                record_id: str | None = None
                if terminal_kind == "answered":
                    record_id = record_id_factory()
                    text = command.edited_text if command.decision == "approve_with_edit" else candidate.text
                    updated = request.transition(AnsweredRequest(record_id=record_id), clock=lambda: at)
                    self._connection.execute(
                        "INSERT INTO central_question_answer_records(record_id,request_id,ticket_id,org_id,owner_id,agent_id,text,sources_json,mode,review_status,candidate_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (record_id, request.request_id, item["ticket_id"], request.org_id, item["owner_id"], item["agent_id"],
                         text, _canonical_json(list(candidate.sources)), candidate.mode, "approved", item["candidate_digest"], at.isoformat()),
                    )
                    self._fault("after-approval-answer-record")
                else:
                    updated = request.transition(DeclinedRequest(reason_code="approval_rejected"), clock=lambda: at)
                changed = self._connection.execute(
                    "UPDATE central_question_approval_items SET status=?,revision=revision+1 WHERE approval_item_id=? AND status='open' AND revision=?",
                    ("rejected" if terminal_kind == "declined" else "approved", command.approval_item_id,
                     command.expected_approval_item_revision),
                )
                if changed.rowcount != 1:
                    raise CentralQuestionLifecycleConflict("approval item CAS conflict")
                self._fault("after-approval-item-resolve")
                receipt_id = _approval_disposition_receipt_id_from_command(command, digest, request.org_id)
                authority_proof_digest = _authority_proof_digest_from_proof(proof, request, item)
                audit_authority = canonical_v19_file_authority(
                    source_policy_digest=proof.grant.policy_digest,
                    current_snapshot_digest=proof.grant.policy_digest,
                )
                self._connection.execute(
                    "INSERT INTO central_question_approval_disposition_receipts(receipt_id,org_id,request_id,approval_item_id,actor_id,identity_session_id,authority_policy_version,authority_policy_digest,authority_proof_digest,expected_approval_item_revision,expected_request_revision,decision_kind,decision_digest,edited_text,idempotency_key,candidate_digest,policy_digest,binding_version,terminal_kind,record_id,resolved_item_revision,terminal_request_revision,authority_policy_revision_id,authority_policy_epoch,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, request.org_id, request.request_id, command.approval_item_id, proof.principal.subject_id,
                     proof.principal.identity_session_id, proof.grant.policy_version, proof.grant.policy_digest,
                     authority_proof_digest,
                     command.expected_approval_item_revision, command.expected_request_revision, command.decision,
                     digest, command.edited_text, command.idempotency_key, item["candidate_digest"], item["policy_digest"], item["binding_version"],
                     terminal_kind, record_id, command.expected_approval_item_revision + 1,
                     command.expected_request_revision + 1,
                     audit_authority.policy_revision_id, audit_authority.policy_epoch,
                     at.isoformat()),
                )
                self._fault("after-approval-disposition-receipt")
                self._connection.execute(
                    "INSERT INTO central_question_approval_disposition_audits(receipt_id,approval_item_id,request_id,actor_id,identity_session_id,authority_policy_version,authority_policy_digest,authority_proof_digest,decision_kind,decision_digest,terminal_kind,record_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, command.approval_item_id, request.request_id, proof.principal.subject_id,
                     proof.principal.identity_session_id, proof.grant.policy_version, proof.grant.policy_digest,
                     authority_proof_digest,
                     command.decision, digest, terminal_kind, record_id, at.isoformat()),
                )
                _append_approval_disposition_evidence(
                    self._connection, receipt_id=receipt_id,
                    request_id=request.request_id,
                    approval_item_id=command.approval_item_id,
                    actor_user_id=proof.principal.subject_id,
                    decision_digest=digest, terminal_kind=terminal_kind,
                    record_id=record_id, occurred_at=at.isoformat(),
                    grant=proof.grant, org_id=request.org_id,
                )
                if record_id is not None:
                    from agent_org_network.central_inbox_review import (
                        append_backup_review_intent,
                    )

                    append_backup_review_intent(self._connection, record_id)
                self._fault("before-approval-terminal-cas")
                if not authority.verify_approval_disposition_proof(proof, request, item, self._connection):
                    raise CentralQuestionLifecycleUnavailable("approval disposition proof stale")
                _cas_request(self._connection, request, updated)
                self._connection.commit()
                return ApprovalDispositionResult(updated, record_id, False)
            except CentralQuestionLifecycleConflict:
                self._connection.rollback()
                raise
            except Exception as error:
                self._connection.rollback()
                raise CentralQuestionLifecycleUnavailable("approval disposition unavailable") from error

    def submit_feedback(
        self, command: FeedbackCommand, *, authority: QuestionFeedbackAuthority,
        feedback_id_factory: Callable[[], str], clock: Callable[[], datetime],
    ) -> QuestionFeedbackResult:
        _validate_feedback_command(command)
        payload_digest = _feedback_payload_digest(command)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._validate()
                receipt = self._connection.execute(
                    "SELECT r.*,f.verdict FROM central_question_feedback_receipts r JOIN central_question_feedback_records f "
                    "ON f.feedback_id=r.feedback_id WHERE r.org_id=? AND r.requester_id=? "
                    "AND r.action='feedback.create' AND r.idempotency_key=?",
                    (command.principal.org_id, command.principal.subject_id, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["request_id"] != command.request_id or receipt["record_id"] != command.record_id
                        or receipt["payload_digest"] != payload_digest
                    ):
                        raise QuestionFeedbackConflict()
                    request, record = _feedback_request_record(
                        self._connection, command.request_id, command.record_id, command.principal
                    )
                    proof = authority.issue_feedback_proof(command.principal, request, record, self._connection)
                    _require_feedback_proof(proof, command.principal, request, record)
                    if not authority.verify_feedback_proof(proof, request, record, self._connection):
                        raise QuestionFeedbackNotFound()
                    from agent_org_network.central_inbox_review import (
                        verify_reevaluation_intent,
                    )

                    verify_reevaluation_intent(
                        self._connection, str(receipt["feedback_id"])
                    )
                    _append_feedback_evidence(
                        self._connection, request=request,
                        record_id=command.record_id,
                        feedback_id=str(receipt["feedback_id"]),
                        receipt_id=str(receipt["receipt_id"]),
                        payload_digest=payload_digest,
                        occurred_at=str(receipt["submitted_at"]),
                        grant=proof.feedback_grant,
                        actor_user_id=command.principal.subject_id,
                    )
                    result = _feedback_result_from_receipt(receipt)
                    self._connection.commit()
                    return QuestionFeedbackResult(
                        request_id=result.request_id, record_id=result.record_id, feedback_id=result.feedback_id,
                        verdict=result.verdict, submitted_at=result.submitted_at, replayed=True,
                    )
                request, record = _feedback_request_record(
                    self._connection, command.request_id, command.record_id, command.principal
                )
                proof = authority.issue_feedback_proof(command.principal, request, record, self._connection)
                _require_feedback_proof(proof, command.principal, request, record)
                submitted_at = clock()
                if submitted_at.tzinfo is None:
                    raise CentralQuestionLifecycleUnavailable("feedback clock unavailable")
                feedback_id = feedback_id_factory()
                if type(feedback_id) is not str or not feedback_id.strip():
                    raise CentralQuestionLifecycleUnavailable("feedback ID unavailable")
                receipt_id = _feedback_receipt_id(command, payload_digest)
                audit_authority = canonical_v19_file_authority(
                    source_policy_digest=proof.feedback_grant.policy_digest,
                    current_snapshot_digest=proof.feedback_grant.policy_digest,
                )
                self._connection.execute(
                    "INSERT INTO central_question_feedback_records(feedback_id,org_id,request_id,record_id,requester_id,verdict,comment,payload_digest,submitted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (feedback_id, request.org_id, request.request_id, command.record_id, command.principal.subject_id,
                     command.verdict, command.comment, payload_digest, submitted_at.isoformat()),
                )
                self._fault("after-feedback-record")
                self._connection.execute(
                    "INSERT INTO central_question_feedback_receipts(receipt_id,org_id,requester_id,action,request_id,record_id,idempotency_key,payload_digest,feedback_id,authority_policy_revision_id,authority_policy_epoch,authority_policy_digest,submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, request.org_id, command.principal.subject_id, "feedback.create", command.request_id,
                     command.record_id, command.idempotency_key, payload_digest, feedback_id,
                     audit_authority.policy_revision_id, audit_authority.policy_epoch,
                     audit_authority.policy_digest, submitted_at.isoformat()),
                )
                self._fault("after-feedback-receipt")
                self._connection.execute(
                    "INSERT INTO central_question_feedback_audits(receipt_id,feedback_id,org_id,request_id,record_id,requester_id,verdict,payload_digest,submitted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (receipt_id, feedback_id, request.org_id, command.request_id, command.record_id,
                     command.principal.subject_id, command.verdict, payload_digest, submitted_at.isoformat()),
                )
                _append_feedback_evidence(
                    self._connection, request=request,
                    record_id=command.record_id, feedback_id=feedback_id,
                    receipt_id=receipt_id, payload_digest=payload_digest,
                    occurred_at=submitted_at.isoformat(),
                    grant=proof.feedback_grant,
                    actor_user_id=command.principal.subject_id,
                )
                from agent_org_network.central_inbox_review import (
                    append_reevaluation_intent,
                )

                append_reevaluation_intent(self._connection, feedback_id)
                self._fault("before-feedback-precommit")
                if not authority.verify_feedback_proof(proof, request, record, self._connection):
                    raise QuestionFeedbackNotFound()
                self._connection.commit()
                return QuestionFeedbackResult(
                    request_id=command.request_id, record_id=command.record_id, feedback_id=feedback_id,
                    verdict=command.verdict, submitted_at=submitted_at, replayed=False,
                )
            except (QuestionFeedbackInvalid, QuestionFeedbackNotFound, QuestionFeedbackConflict):
                self._connection.rollback()
                raise
            except Exception as error:
                self._connection.rollback()
                raise CentralQuestionLifecycleUnavailable("feedback unavailable") from error

    def _replay_approval_disposition(
        self, receipt: sqlite3.Row, command: ApprovalDispositionCommand,
        authority: ApprovalDispositionAuthority,
    ) -> ApprovalDispositionResult:
        request = _read_request(self._connection, command.request_id)
        item = self._connection.execute(
            "SELECT * FROM central_question_approval_items WHERE approval_item_id=?", (command.approval_item_id,)
        ).fetchone()
        if request is None or item is None or request.org_id != receipt["org_id"]:
            raise CentralQuestionLifecycleUnavailable("approval replay unavailable")
        proof = authority.issue_approval_disposition_proof(command.principal, request, item, self._connection)
        _require_approval_disposition_proof(proof, command.principal, request, item)
        if (
            proof.principal.subject_id != receipt["actor_id"]
            or not authority.verify_approval_disposition_proof(proof, request, item, self._connection)
        ):
            raise CentralQuestionLifecycleUnavailable("approval replay forbidden")
        self._require_approval_card_binding(request, item)
        _validate_approval_disposition_successor(self._connection, receipt, request, item)
        _append_approval_disposition_evidence(
            self._connection, receipt_id=str(receipt["receipt_id"]),
            request_id=str(receipt["request_id"]),
            approval_item_id=str(receipt["approval_item_id"]),
            actor_user_id=str(receipt["actor_id"]),
            decision_digest=str(receipt["decision_digest"]),
            terminal_kind=cast(
                Literal["answered", "declined"], receipt["terminal_kind"]
            ),
            record_id=cast(str | None, receipt["record_id"]),
            occurred_at=str(receipt["created_at"]), grant=proof.grant,
            org_id=str(receipt["org_id"]),
        )
        if receipt["record_id"] is not None:
            from agent_org_network.central_inbox_review import (
                verify_backup_review_intent,
            )

            verify_backup_review_intent(
                self._connection, str(receipt["record_id"])
            )
        return ApprovalDispositionResult(
            request, cast(str | None, receipt["record_id"]), True
        )

    def _require_approval_card_binding(self, request: QuestionRequest, item: sqlite3.Row) -> None:
        resolver = self._card_binding_resolver
        if resolver is None:
            raise CentralQuestionLifecycleUnavailable("approval binding unavailable")
        binding = resolver.resolve_card_binding(request.org_id, str(item["agent_id"]), self._connection)
        if binding.owner_id != item["owner_id"] or binding.revision != item["binding_version"]:
            raise CentralQuestionLifecycleConflict("approval binding drift")

    def _require_ingest_binding(
        self, command: OwnerAnswerIngest, request: QuestionRequest | None, authority: AnswerIngestAuthority
    ) -> CentralWorkTicket:
        if (
            request is None
            or not isinstance(request.state, AwaitingAnswer)
            or request.revision != command.expected_request_revision
            or request.request_id != command.request_id
            or request.state.ticket_id != command.ticket_id
            or request.state.attempt != command.attempt
            or request.state.route != command.route
        ):
            raise CentralQuestionLifecycleConflict("answer ingest request binding conflict")
        ticket = self._work_ticket(request.request_id, request.state.attempt)
        if (
            ticket is None
            or ticket.ticket_id != command.ticket_id
            or ticket.route != command.route
            or ticket.attempt != command.attempt
        ):
            raise CentralQuestionLifecycleUnavailable("answer ingest ticket binding unavailable")
        row = self._connection.execute(
            "SELECT worker_id,status FROM central_question_work_ticket_delivery_claims WHERE ticket_id=?",
            (ticket.ticket_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] not in {"leased", "delivered"}
            or row["worker_id"] != command.delivery_subject
        ):
            raise CentralQuestionLifecycleUnavailable("answer ingest delivery binding unavailable")
        return ticket

    def _require_ingest_card_binding(self, ticket: CentralWorkTicket) -> CardBinding:
        resolver = self._card_binding_resolver
        if resolver is None:
            raise CentralQuestionLifecycleUnavailable("card binding resolver unavailable")
        binding = resolver.resolve_card_binding(ticket.org_id, ticket.agent_id, self._connection)
        if (
            binding.agent_id != ticket.agent_id
            or binding.owner_id != ticket.owner_id
            or binding.revision < 1
        ):
            raise CentralQuestionLifecycleUnavailable("answer ingest card binding unavailable")
        return binding

    def _insert_answer_ingest_receipt(
        self, ticket: CentralWorkTicket, command: OwnerAnswerIngest, candidate_json: str, candidate_digest: str,
        evaluation: ApprovalEvaluation, binding_version: int, authority_version: str,
        authorization: AnswerIngestAuthorization | str,
        result_kind: Literal["answered", "awaiting_approval"], record_id: str | None,
        approval_item_id: str | None, at: datetime,
    ) -> None:
        audit_authority = (
            canonical_v19_file_authority(
                source_policy_digest=authorization.policy_digest,
                current_snapshot_digest=authorization.policy_digest,
            )
            if type(authorization) is AnswerIngestAuthorization
            else None
        )
        self._connection.execute(
            "INSERT INTO central_question_answer_ingest_receipts(org_id,delivery_subject,ticket_id,request_id,expected_request_revision,attempt,route_json,candidate_json,candidate_digest,policy_kind,policy_digest,binding_version,authority_version,authority_policy_revision_id,authority_policy_epoch,authority_policy_digest,result_kind,record_id,approval_item_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket.org_id, command.delivery_subject,
             command.ticket_id, command.request_id, command.expected_request_revision, command.attempt,
             _canonical_json(command.route.model_dump(mode="json")), candidate_json, candidate_digest,
             evaluation.kind, evaluation.policy_digest, binding_version, authority_version,
             None if audit_authority is None else audit_authority.policy_revision_id,
             None if audit_authority is None else audit_authority.policy_epoch,
             None if audit_authority is None else audit_authority.policy_digest,
             result_kind, record_id, approval_item_id, at.isoformat()),
        )

    def _insert_answer_ingest_audit(
        self, ticket: CentralWorkTicket, request: QuestionRequest,
        command: OwnerAnswerIngest,
        authorization: AnswerIngestAuthorization | str,
        event_kind: Literal["answered", "awaiting_approval"], at: datetime,
        candidate_digest: str, record_id: str | None,
        approval_item_id: str | None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO central_question_answer_ingest_audits(ticket_id,request_id,receipt_ticket_id,event_kind,created_at) VALUES (?,?,?,?,?)",
            (ticket.ticket_id, request.request_id, ticket.ticket_id, event_kind, at.isoformat()),
        )
        _append_answer_ingest_evidence(
            self._connection, ticket=ticket, request=request, command=command,
            authorization=authorization, event_kind=event_kind,
            occurred_at=at.isoformat(), candidate_digest=candidate_digest,
            record_id=record_id, approval_item_id=approval_item_id,
        )
        self._fault("after-answer-ingest-audit")

    def _complete_ticket_and_release_lease(self, ticket_id: str) -> None:
        changed = self._connection.execute(
            "UPDATE central_question_work_tickets SET status='completed' WHERE ticket_id=? AND status='pending'",
            (ticket_id,),
        )
        if changed.rowcount != 1:
            raise CentralQuestionLifecycleConflict("work ticket completion conflict")
        self._fault("after-work-ticket-complete")
        self._connection.execute(
            "DELETE FROM central_question_work_ticket_delivery_claims WHERE ticket_id=?", (ticket_id,)
        )
        self._fault("after-delivery-lease-release")

    def _replay_ingest(
        self, receipt: sqlite3.Row, command: OwnerAnswerIngest, candidate_json: str,
        candidate_digest: str, approval_policy: AnswerIngestApprovalPolicy,
        authority: AnswerIngestAuthority,
    ) -> OwnerAnswerIngestResult:
        if (
            receipt["request_id"] != command.request_id
            or receipt["delivery_subject"] != command.delivery_subject
            or receipt["expected_request_revision"] != command.expected_request_revision
            or receipt["attempt"] != command.attempt
            or receipt["route_json"] != _canonical_json(command.route.model_dump(mode="json"))
            or receipt["candidate_json"] != candidate_json
            or receipt["candidate_digest"] != candidate_digest
        ):
            raise CentralQuestionLifecycleConflict("answer ingest replay conflict")
        ticket = self._work_ticket_by_id(command.ticket_id)
        if ticket is None:
            raise CentralQuestionLifecycleUnavailable("answer ingest replay ticket unavailable")
        binding = self._require_ingest_card_binding(ticket)
        authorization = authority.authorize_answer_ingest(
            ticket.org_id, command.delivery_subject, ticket.owner_id, ticket.agent_id, self._connection
        )
        authority_version = (
            authorization.policy_revision_id
            if type(authorization) is AnswerIngestAuthorization
            else authorization
        )
        if authority_version != receipt["authority_version"]:
            raise CentralQuestionLifecycleConflict("answer ingest authority drift")
        evaluation = _validate_approval_evaluation(
            approval_policy.evaluate(ticket.org_id, ticket.route, command.candidate)
        )
        if evaluation.kind != receipt["policy_kind"] or evaluation.policy_digest != receipt["policy_digest"]:
            raise CentralQuestionLifecycleConflict("answer ingest policy drift")
        if binding.revision != receipt["binding_version"]:
            raise CentralQuestionLifecycleConflict("answer ingest binding drift")
        request = _read_request(self._connection, command.request_id)
        if receipt["result_kind"] == "answered":
            if request is None or not isinstance(request.state, AnsweredRequest) or request.state.record_id != receipt["record_id"]:
                raise CentralQuestionLifecycleUnavailable("answer ingest replay terminal unavailable")
            if self._connection.execute(
                "SELECT 1 FROM central_question_answer_records WHERE record_id=? AND request_id=? AND ticket_id=?",
                (receipt["record_id"], request.request_id, ticket.ticket_id),
            ).fetchone() is None:
                raise CentralQuestionLifecycleUnavailable("answer ingest replay record unavailable")
            from agent_org_network.central_inbox_review import (
                verify_backup_review_intent,
            )

            verify_backup_review_intent(
                self._connection, str(receipt["record_id"])
            )
            _append_answer_ingest_evidence(
                self._connection, ticket=ticket, request=request, command=command,
                authorization=authorization, event_kind="answered",
                occurred_at=str(receipt["created_at"]),
                candidate_digest=candidate_digest,
                record_id=cast(str, receipt["record_id"]), approval_item_id=None,
            )
            return OwnerAnswerIngestResult(request, cast(str, receipt["record_id"]), None, True)
        if (
            request is None
            or not isinstance(request.state, AwaitingApproval)
            or request.state.draft_ref != receipt["approval_item_id"]
            or self._connection.execute(
                "SELECT 1 FROM central_question_approval_items WHERE approval_item_id=? AND request_id=? AND ticket_id=? AND status='open'",
                (receipt["approval_item_id"], request.request_id, ticket.ticket_id),
            ).fetchone() is None
        ):
            raise CentralQuestionLifecycleUnavailable("answer ingest replay approval unavailable")
        _append_answer_ingest_evidence(
            self._connection, ticket=ticket, request=request, command=command,
            authorization=authorization, event_kind="awaiting_approval",
            occurred_at=str(receipt["created_at"]),
            candidate_digest=candidate_digest, record_id=None,
            approval_item_id=cast(str, receipt["approval_item_id"]),
        )
        return OwnerAnswerIngestResult(request, None, cast(str, receipt["approval_item_id"]), True)

    def _linked_id(self, table: str, column: str, request_id: str) -> str | None:
        with self._lock:
            try:
                self._validate()
                row = self._connection.execute(
                    f"SELECT {column} FROM {table} WHERE request_id=?", (request_id,)
                ).fetchone()
                return None if row is None else cast(str, row[0])
            except Exception as error:
                raise CentralQuestionLifecycleUnavailable("lifecycle linked read unavailable") from error

    def _validate(self) -> None:
        _validate_catalog(self._connection, root_manager_resolver=self._root_manager_resolver, card_binding_resolver=self._card_binding_resolver)


class CentralQuestionLifecycleApplication:
    """B1: durable receipt first, then exact greeting or Router disposition."""

    def __init__(self, *, store: CentralQuestionLifecycleStore, router: LifecycleRouter, route_authority: RouteAuthority, request_id_factory: Callable[[], str], clock: Callable[[], datetime], deadline: Callable[[str, str, datetime], datetime], manager_item_id_factory: Callable[[], str], root_manager_resolver: RootManagerResolver, conflict_case_id_factory: Callable[[], str] = lambda: uuid4().hex, work_ticket_id_factory: Callable[[], str] = lambda: uuid4().hex, owner_delivery: OwnerDeliveryPort | None = None, delivery_worker_id: str = "central-lifecycle", delivery_lease_for: timedelta = timedelta(minutes=1)) -> None:
        self._store = store
        self._router = router
        self._route_authority = route_authority
        self._request_id_factory = request_id_factory
        self._clock = clock
        self._deadline = deadline
        self._manager_item_id_factory = manager_item_id_factory
        self._root_manager_resolver = root_manager_resolver
        self._conflict_case_id_factory = conflict_case_id_factory
        self._work_ticket_id_factory = work_ticket_id_factory
        self._owner_delivery = owner_delivery
        self._delivery_worker_id = delivery_worker_id
        self._delivery_lease_for = delivery_lease_for

    def create(self, *, question: str, org_id: str, requester_id: str, idempotency_key: str) -> CentralLifecycleCreateResult:
        if not all(type(value) is str and value.strip() for value in (question, org_id, requester_id)):
            raise CentralQuestionLifecycleUnavailable("invalid lifecycle input")
        try:
            started_at = self._clock()
            due_at = self._deadline(org_id, "received", started_at)
            request = QuestionRequest.receive(org_id=org_id, requester_id=requester_id, question=question,
                request_id_factory=self._request_id_factory, clock=lambda: started_at, due_at=due_at)
            created = self._store.create_or_replay(request, idempotency_key=idempotency_key)
            if created.replayed:
                return created
            return created
        except (CentralQuestionLifecycleConflict, CentralQuestionLifecycleUnavailable):
            raise
        except Exception as error:
            raise CentralQuestionLifecycleUnavailable("lifecycle application unavailable") from error

    def process_received(self, request_id: str) -> QuestionRequest:
        """Resume one committed Received Request after create has returned.

        It is deliberately recoverable: a Router/dependency fault leaves the
        durable Received and create receipt unchanged for a later process.
        """
        try:
            return self._process_received(request_id)
        except CentralQuestionLifecycleConflict:
            winner = self._store.get(request_id)
            if winner is not None and not isinstance(winner.state, Received):
                return winner
            raise CentralQuestionLifecycleUnavailable("lifecycle concurrent disposition") from None
        except CentralQuestionLifecycleUnavailable:
            raise
        except Exception as error:
            raise CentralQuestionLifecycleUnavailable("lifecycle routing unavailable") from error

    def _process_received(self, request_id: str) -> QuestionRequest:
        request = self._store.get(request_id)
        if request is None:
            raise CentralQuestionLifecycleUnavailable("request unavailable")
        if not isinstance(request.state, Received):
            return request
        if _is_non_actionable_conversation(request.question):
            updated = request.record_initial_routing(intent=None, disposition="non_actionable",
                target=DeclinedRequest(reason_code="non_actionable_conversation"), clock=self._clock)
            return self._store.record_initial(
                request, updated, authority=self._route_authority
            )
        decision = self._router.route(request.question)
        if isinstance(decision, Contested):
            if not decision.intent.strip() or len(decision.candidates) < 2:
                raise CentralQuestionLifecycleUnavailable("invalid contested decision")
            candidates_json = _candidate_snapshot(decision)
            at = self._clock()
            due = self._deadline(request.org_id, "awaiting_conflict", at)
            case_id = self._conflict_case_id_factory()
            updated = request.record_initial_routing(
                intent=decision.intent,
                disposition="contested",
                target=AwaitingConflict(
                    case_id=case_id,
                    handling=HandlingAssignment(kind="conflict_case", ref=case_id, due_at=due),
                ),
                clock=lambda: at,
            )
            return self._store.record_initial(
                request, updated, conflict=(case_id, decision.intent, candidates_json),
                authority=self._route_authority,
            )
        if isinstance(decision, Unowned):
            at = self._clock()
            due = self._deadline(request.org_id, "awaiting_manager", at)
            item_id = self._manager_item_id_factory()
            updated = request.record_initial_routing(intent=decision.intent or None, disposition="unowned",
                target=AwaitingManager(item_id=item_id, public_kind="unowned",
                    handling=HandlingAssignment(kind="manager_item", ref=item_id, due_at=due)), clock=lambda: at)
            return self._store.record_initial(
                request, updated, manager=(item_id, decision.escalated_to), authority=self._route_authority
            )
        intent = decision.intent
        grant = self._route_authority.authorize_route(
            request.org_id, intent, decision.primary.agent_id, None
        )
        if type(grant) is RouteAuthorization:
            route_authority_version = grant.policy_revision_id
        elif type(grant) is str and grant.strip():
            route_authority_version = grant
        else:
            raise CentralQuestionLifecycleUnavailable("route authority denied")
        at = self._clock()
        due = self._deadline(request.org_id, "ready_to_dispatch", at)
        trigger = f"request-dispatch:{request.request_id}:1"
        updated = request.record_initial_routing(intent=intent, disposition="routed",
            target=ReadyToDispatch(route=RouteTarget(intent=intent, agent_id=decision.primary.agent_id,
                requires_approval=decision.requires_approval, authority_version=route_authority_version), attempt=1,
                trigger_key=trigger, handling=HandlingAssignment(kind="system", ref=trigger, due_at=due)),
            clock=lambda: at)
        return self._store.record_initial(request, updated, authority=self._route_authority)

    def process_ready_to_dispatch(self, request_id: str) -> QuestionRequest:
        """Persist one pending WorkTicket, then (and only then) notify Owner delivery."""
        try:
            request = self._store.get(request_id)
            if request is None:
                raise CentralQuestionLifecycleUnavailable("request unavailable")
            if not isinstance(request.state, (ReadyToDispatch, AwaitingAnswer)):
                return request
            result = self._store.enqueue_ready_to_dispatch(
                request, ticket_id=self._work_ticket_id_factory(),
                authority=self._route_authority,
            )
            if self._owner_delivery is not None:
                # Claim commits before the port call.  An exception or an
                # acknowledgement failure intentionally leaves the lease for
                # expiry/recovery, so this remains at-least-once delivery.
                claim = self._store.claim_delivery(
                    result.ticket.request_id,
                    self._delivery_worker_id,
                    self._clock(),
                    self._delivery_lease_for,
                )
                if claim is not None:
                    self._owner_delivery.deliver(claim.ticket)
                    self._store.acknowledge_delivery(claim, self._clock())
            return result.request
        except CentralQuestionLifecycleUnavailable:
            raise
        except Exception as error:
            raise CentralQuestionLifecycleUnavailable("work ticket delivery unavailable") from error


class OwnerAnswerIngestApplication:
    """Typed internal Owner completion boundary; no HTTP/A2A transport is mounted here."""

    def __init__(
        self,
        *,
        store: CentralQuestionLifecycleStore,
        approval_policy: AnswerIngestApprovalPolicy,
        authority: AnswerIngestAuthority,
        record_id_factory: Callable[[], str],
        approval_item_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
        approval_deadline: Callable[[str, datetime], datetime],
    ) -> None:
        self._store = store
        self._approval_policy = approval_policy
        self._authority = authority
        self._record_id_factory = record_id_factory
        self._approval_item_id_factory = approval_item_id_factory
        self._clock = clock
        self._approval_deadline = approval_deadline

    def ingest(self, command: OwnerAnswerIngest) -> OwnerAnswerIngestResult:
        return self._store.ingest_owner_answer(
            command,
            approval_policy=self._approval_policy,
            authority=self._authority,
            record_id_factory=self._record_id_factory,
            approval_item_id_factory=self._approval_item_id_factory,
            clock=self._clock,
            approval_deadline=self._approval_deadline,
        )


class ApprovalDispositionApplication:
    """The sole typed writer for an open ApprovalItem disposition."""

    def __init__(self, *, store: CentralQuestionLifecycleStore, authority: ApprovalDispositionAuthority,
                 record_id_factory: Callable[[], str], clock: Callable[[], datetime]) -> None:
        self._store = store
        self._authority = authority
        self._record_id_factory = record_id_factory
        self._clock = clock

    def dispose(self, command: ApprovalDispositionCommand) -> ApprovalDispositionResult:
        return self._store.dispose_approval(
            command, authority=self._authority, record_id_factory=self._record_id_factory, clock=self._clock
        )


def _valid_idempotency_key(value: object) -> bool:
    return type(value) is str and len(value.encode("utf-8")) <= 128 and _IDEMPOTENCY_KEY.fullmatch(value) is not None


def _validate_approval_disposition(command: ApprovalDispositionCommand) -> None:
    if (
        type(command) is not ApprovalDispositionCommand
        or type(command.principal) is not AuthenticatedPrincipal
        or not all(value.strip() for value in (
            command.request_id, command.approval_item_id, command.principal.subject_id, command.idempotency_key
        ))
        or command.expected_approval_item_revision < 1
        or command.expected_request_revision < 0
        or command.decision not in {"approve", "approve_with_edit", "reject"}
        or not _valid_idempotency_key(command.idempotency_key)
    ):
        raise CentralQuestionLifecycleUnavailable("invalid approval disposition")
    if command.decision == "approve_with_edit":
        if type(command.edited_text) is not str or not command.edited_text.strip():
            raise CentralQuestionLifecycleUnavailable("invalid approval edit")
    elif command.decision == "reject":
        if command.edited_text is not None and (
            type(command.edited_text) is not str or command.edited_text == ""
        ):
            raise CentralQuestionLifecycleUnavailable("invalid approval rejection reason")
    elif command.edited_text is not None:
        raise CentralQuestionLifecycleUnavailable("unexpected approval edit")


def _approval_decision_digest(command: ApprovalDispositionCommand) -> str:
    return _approval_decision_digest_values(command.decision, command.edited_text)


def _require_approval_disposition_proof(
    proof: ApprovalDispositionAuthorizationProof, principal: AuthenticatedPrincipal,
    request: QuestionRequest, item: sqlite3.Row,
) -> None:
    if type(proof) is not ApprovalDispositionAuthorizationProof or proof.principal != principal:
        raise CentralQuestionLifecycleUnavailable("approval proof unavailable")
    grant = proof.grant
    expected_resource = ResourceRef(
        org_id=request.org_id, kind="approval_item",
        resource_id=str(item["approval_item_id"]), owner_subject_id=principal.subject_id,
    )
    if (
        type(grant) is not AuthorizationGrant
        or grant.org_id != request.org_id
        or grant.subject_id != principal.subject_id
        or grant.action != "approval.decide"
        or grant.resource != expected_resource
        or not grant.policy_version.strip()
        or len(grant.policy_digest) != 64
    ):
        raise CentralQuestionLifecycleUnavailable("approval proof binding mismatch")


def _approval_decision_digest_values(decision: str, edited_text: str | None) -> str:
    return sha256(_canonical_json({"decision": decision, "edited_text": edited_text}).encode()).hexdigest()


def _approval_disposition_receipt_id(values: sqlite3.Row) -> str:
    """Deterministic immutable identity for an approval disposition receipt."""
    payload = {
        key: values[key]
        for key in (
            "org_id", "request_id", "approval_item_id", "actor_id",
            "expected_approval_item_revision", "expected_request_revision", "decision_kind",
            "decision_digest", "idempotency_key",
        )
    }
    return "approval-disposition:" + sha256(_canonical_json(payload).encode()).hexdigest()


def _approval_disposition_receipt_id_from_command(
    command: ApprovalDispositionCommand, digest: str, org_id: str
) -> str:
    return "approval-disposition:" + sha256(_canonical_json({
        "org_id": org_id,
        "request_id": command.request_id,
        "approval_item_id": command.approval_item_id,
        "actor_id": command.principal.subject_id,
        "expected_approval_item_revision": command.expected_approval_item_revision,
        "expected_request_revision": command.expected_request_revision,
        "decision_kind": command.decision,
        "decision_digest": digest,
        "idempotency_key": command.idempotency_key,
    }).encode()).hexdigest()


def _authority_proof_digest_from_proof(
    proof: ApprovalDispositionAuthorizationProof, request: QuestionRequest, item: sqlite3.Row,
) -> str:
    return _authority_proof_digest(
        org_id=request.org_id,
        request_id=request.request_id,
        approval_item_id=str(item["approval_item_id"]),
        actor_id=proof.principal.subject_id,
        identity_session_id=proof.principal.identity_session_id,
        policy_version=proof.grant.policy_version,
        policy_digest=proof.grant.policy_digest,
    )


def _authority_proof_digest_from_values(values: sqlite3.Row) -> str:
    # v11 did not store grant roles.  Its persisted grant commitment is the
    # exact historical identity/action/resource/policy shape it did retain.
    return _authority_proof_digest(
        org_id=str(values["org_id"]), request_id=str(values["request_id"]),
        approval_item_id=str(values["approval_item_id"]), actor_id=str(values["actor_id"]),
        identity_session_id=str(values["identity_session_id"]),
        policy_version=str(values["authority_policy_version"]),
        policy_digest=str(values["authority_policy_digest"]),
    )


def _authority_proof_digest(
    *, org_id: str, request_id: str, approval_item_id: str, actor_id: str,
    identity_session_id: str, policy_version: str, policy_digest: str,
) -> str:
    return sha256(_canonical_json({
        "principal": {
            "org_id": org_id, "subject_id": actor_id, "identity_session_id": identity_session_id,
        },
        "action": "approval.decide",
        "resource": {
            "org_id": org_id, "kind": "approval_item",
            "resource_id": approval_item_id, "owner_subject_id": actor_id,
        },
        "grant": {
            "policy_version": policy_version, "policy_digest": policy_digest,
        },
    }).encode()).hexdigest()


def _is_lowercase_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _validate_question_create_command(command: QuestionCreateCommand) -> None:
    if (
        type(command) is not QuestionCreateCommand
        or type(command.question) is not str
        or not command.question.strip()
        or not _valid_idempotency_key(command.idempotency_key)
        or not _is_lowercase_sha256(command.identity_session_id)
        or type(command.expected_org_id) is not str
        or not command.expected_org_id.strip()
        or type(command.expected_requester_id) is not str
        or not command.expected_requester_id.strip()
    ):
        raise QuestionCreateForbidden()


def _require_question_create_proof(
    proof: QuestionCreateAuthorizationProof, command: QuestionCreateCommand,
) -> None:
    if type(proof) is not QuestionCreateAuthorizationProof or type(proof.principal) is not AuthenticatedPrincipal:
        raise QuestionCreateForbidden()
    principal = proof.principal
    session_resource = ResourceRef(
        org_id=command.expected_org_id, kind="browser_session", resource_id=command.identity_session_id,
        owner_subject_id=command.expected_requester_id,
    )
    create_resource = ResourceRef(
        org_id=command.expected_org_id, kind="question", owner_subject_id=command.expected_requester_id,
    )
    if (
        principal.org_id != command.expected_org_id
        or principal.subject_id != command.expected_requester_id
        or principal.identity_session_id != command.identity_session_id
        or type(proof.session_grant) is not AuthorizationGrant
        or type(proof.create_grant) is not AuthorizationGrant
        or proof.session_grant.org_id != principal.org_id
        or proof.session_grant.subject_id != principal.subject_id
        or proof.session_grant.action != "session.read"
        or proof.session_grant.resource != session_resource
        or proof.create_grant.org_id != principal.org_id
        or proof.create_grant.subject_id != principal.subject_id
        or proof.create_grant.action != "question.create"
        or proof.create_grant.resource != create_resource
    ):
        raise QuestionCreateForbidden()


def _validate_feedback_command(command: FeedbackCommand) -> None:
    if (
        type(command) is not FeedbackCommand
        or type(command.principal) is not AuthenticatedPrincipal
        or not _valid_idempotency_key(command.idempotency_key)
        or not _valid_feedback_reference(command.request_id)
        or not _valid_feedback_reference(command.record_id)
        or command.verdict not in {"good", "bad"}
        or type(command.comment) is not str
    ):
        raise QuestionFeedbackInvalid()
    try:
        if len(command.comment.encode("utf-8")) > 4096:
            raise QuestionFeedbackInvalid()
    except UnicodeError as error:
        raise QuestionFeedbackInvalid() from error


def _valid_feedback_reference(value: object) -> bool:
    return type(value) is str and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value))


def _feedback_payload_digest(command: FeedbackCommand) -> str:
    return sha256(_canonical_json({
        "record_id": command.record_id, "verdict": command.verdict, "comment": command.comment,
    }).encode("utf-8")).hexdigest()


def _feedback_payload_digest_from_values(values: sqlite3.Row) -> str:
    return sha256(_canonical_json({
        "record_id": values["record_id"], "verdict": values["verdict"], "comment": values["comment"],
    }).encode("utf-8")).hexdigest()


def _feedback_receipt_id(command: FeedbackCommand, payload_digest: str) -> str:
    return "question-feedback:" + sha256(_canonical_json({
        "org_id": command.principal.org_id, "requester_id": command.principal.subject_id,
        "action": "feedback.create", "request_id": command.request_id,
        "idempotency_key": command.idempotency_key, "payload_digest": payload_digest,
    }).encode()).hexdigest()


def _feedback_receipt_id_from_values(values: sqlite3.Row) -> str:
    return "question-feedback:" + sha256(_canonical_json({
        "org_id": values["org_id"], "requester_id": values["requester_id"],
        "action": values["action"], "request_id": values["request_id"],
        "idempotency_key": values["idempotency_key"], "payload_digest": values["payload_digest"],
    }).encode()).hexdigest()


def _feedback_request_record(
    connection: sqlite3.Connection, request_id: str, record_id: str, principal: AuthenticatedPrincipal,
) -> tuple[QuestionRequest, sqlite3.Row]:
    request = _read_request(connection, request_id)
    record = connection.execute(
        "SELECT * FROM central_question_answer_records WHERE record_id=?", (record_id,)
    ).fetchone()
    if (
        request is None or record is None or not isinstance(request.state, AnsweredRequest)
        or request.org_id != principal.org_id or request.requester_id != principal.subject_id
        or request.state.record_id != record_id or record["request_id"] != request_id
        or record["org_id"] != request.org_id
    ):
        raise QuestionFeedbackNotFound()
    return request, record


def _require_feedback_proof(
    proof: FeedbackAuthorizationProof, principal: AuthenticatedPrincipal, request: QuestionRequest, record: sqlite3.Row,
) -> None:
    session_resource = ResourceRef(
        org_id=request.org_id, kind="browser_session", resource_id=principal.identity_session_id,
        owner_subject_id=principal.subject_id,
    )
    feedback_resource = ResourceRef(
        org_id=request.org_id, kind="question_feedback", resource_id=f"{request.request_id}:{record['record_id']}",
        owner_subject_id=principal.subject_id,
    )
    if (
        type(proof) is not FeedbackAuthorizationProof or proof.principal != principal
        or proof.session_grant.org_id != request.org_id or proof.session_grant.subject_id != principal.subject_id
        or proof.session_grant.action != "session.read" or proof.session_grant.resource != session_resource
        or proof.feedback_grant.org_id != request.org_id or proof.feedback_grant.subject_id != principal.subject_id
        or proof.feedback_grant.action != "feedback.create" or proof.feedback_grant.resource != feedback_resource
    ):
        raise QuestionFeedbackNotFound()


def _feedback_result_from_receipt(receipt: sqlite3.Row) -> QuestionFeedbackResult:
    try:
        verdict = cast(Literal["good", "bad"], receipt["verdict"])
        if verdict not in {"good", "bad"}:
            raise ValueError
        return QuestionFeedbackResult(
            request_id=str(receipt["request_id"]), record_id=str(receipt["record_id"]),
            feedback_id=str(receipt["feedback_id"]), verdict=verdict,
            submitted_at=_parse_timestamp(str(receipt["submitted_at"])), replayed=False,
        )
    except Exception as error:
        raise CentralQuestionLifecycleUnavailable("feedback receipt unavailable") from error


def _validate_owner_answer_ingest(command: OwnerAnswerIngest) -> None:
    if type(command) is not OwnerAnswerIngest:
        raise CentralQuestionLifecycleUnavailable("invalid answer ingest")
    if (
        not command.ticket_id.strip()
        or not command.request_id.strip()
        or not command.delivery_subject.strip()
        or command.expected_request_revision < 0
        or command.attempt < 1
        or type(command.route) is not RouteTarget
        or type(command.candidate) is not OwnerAnswerCandidate
    ):
        raise CentralQuestionLifecycleUnavailable("invalid answer ingest")
    _candidate_json(command.candidate)


def _candidate_json(candidate: OwnerAnswerCandidate) -> str:
    if (
        type(candidate) is not OwnerAnswerCandidate
        or not candidate.text.strip()
        or candidate.mode not in {"full", "backup"}
        or any(type(source) is not str or not source.strip() for source in candidate.sources)
    ):
        raise CentralQuestionLifecycleUnavailable("invalid answer candidate")
    return _canonical_json({"text": candidate.text, "sources": list(candidate.sources), "mode": candidate.mode})


def _candidate_json_from_raw(raw: str) -> OwnerAnswerCandidate | None:
    try:
        value = json.loads(raw)
        if set(value) != {"text", "sources", "mode"} or type(value["sources"]) is not list:
            return None
        candidate = OwnerAnswerCandidate(
            text=value["text"], sources=tuple(value["sources"]), mode=value["mode"]
        )
        return candidate if _candidate_json(candidate) == raw else None
    except Exception:
        return None


def _validate_approval_evaluation(value: object) -> ApprovalEvaluation:
    if (
        type(value) is not ApprovalEvaluation
        or value.kind not in {"no_approval", "approval_required"}
        or not value.policy_digest.strip()
    ):
        raise CentralQuestionLifecycleUnavailable("approval policy unavailable")
    return value


def _safe_sources(raw: str) -> tuple[str, ...]:
    try:
        values = _SOURCES.validate_json(raw, strict=True)
        if any(not source.strip() for source in values) or _canonical_json(list(values)) != raw:
            raise ValueError
        return values
    except Exception as error:
        raise CentralQuestionLifecycleUnavailable("corrupt answer sources") from error


def _validate_initial_aggregate_shape(
    current: QuestionRequest,
    updated: QuestionRequest,
    *,
    manager: tuple[str, str] | None,
    conflict: tuple[str, str, str] | None,
) -> None:
    """Reject direct callers that could split an initial linked aggregate."""
    if not isinstance(current.state, Received):
        raise CentralQuestionLifecycleUnavailable("invalid initial aggregate source")
    if manager is not None and conflict is not None:
        raise CentralQuestionLifecycleUnavailable("mutually exclusive initial aggregates")
    manager_state = isinstance(updated.state, AwaitingManager)
    conflict_state = isinstance(updated.state, AwaitingConflict)
    if manager_state != (manager is not None) or conflict_state != (conflict is not None):
        raise CentralQuestionLifecycleUnavailable("linked initial aggregate missing or mismatched")
    # WorkTicket has its own ReadyToDispatch recovery UoW.  `record_initial`
    # must never manufacture AwaitingAnswer without its ticket/receipt.
    if isinstance(updated.state, AwaitingAnswer):
        raise CentralQuestionLifecycleUnavailable("work ticket aggregate requires enqueue UoW")
    if manager is not None:
        if not isinstance(updated.state, AwaitingManager):
            raise CentralQuestionLifecycleUnavailable("manager aggregate binding mismatch")
        item_id, router_manager_id = manager
        if (
            not item_id.strip()
            or not router_manager_id.strip()
            or updated.state.item_id != item_id
            or updated.state.public_kind != "unowned"
        ):
            raise CentralQuestionLifecycleUnavailable("manager aggregate binding mismatch")
    if conflict is not None:
        if not isinstance(updated.state, AwaitingConflict):
            raise CentralQuestionLifecycleUnavailable("conflict aggregate binding mismatch")
        case_id, intent, _candidates_json = conflict
        if (
            not case_id.strip()
            or not intent.strip()
            or updated.state.case_id != case_id
            or updated.intent != intent
        ):
            raise CentralQuestionLifecycleUnavailable("conflict aggregate binding mismatch")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_lifecycle_json(value: object) -> str:
    """Canonical JSON shared by same-database durable lifecycle companions."""
    return _canonical_json(value)


def read_lifecycle_request(
    connection: sqlite3.Connection, request_id: str
) -> QuestionRequest | None:
    """Read one canonical Request through an existing transaction."""
    return _read_request(connection, request_id)


def compare_and_set_lifecycle_request(
    connection: sqlite3.Connection,
    current: QuestionRequest,
    updated: QuestionRequest,
) -> None:
    """Apply the canonical Request CAS through a companion's transaction."""
    _cas_request(connection, current, updated)


def read_legacy_conflict_candidates(raw: str) -> tuple[dict[str, str], ...]:
    """Decode the immutable v14 Conflict candidate identity/Owner snapshot."""
    return _conflict_candidates(raw)


def _candidate_snapshot(decision: Contested) -> str:
    """Persist only the immutable candidate identity/owner snapshot, never Card bodies."""
    return _canonical_json([
        {"agent_id": candidate.agent_id, "owner_id": candidate.owner}
        for candidate in decision.candidates
    ])


def _work_ticket_digest(ticket: CentralWorkTicket) -> str:
    return sha256(_canonical_json({
        "request_id": ticket.request_id, "attempt": ticket.attempt, "ticket_id": ticket.ticket_id,
        "org_id": ticket.org_id, "owner_id": ticket.owner_id, "agent_id": ticket.agent_id,
        "route": ticket.route.model_dump(mode="json"), "created_at": ticket.created_at.isoformat(),
    }).encode()).hexdigest()


def _delivery_ack_receipt(ticket_id: str, delivery_attempt: int) -> str:
    return f"delivery:{ticket_id}:{delivery_attempt}"


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be offset-aware")
    return parsed


def _row_work_ticket(row: sqlite3.Row) -> CentralWorkTicket:
    try:
        route = _ROUTE.validate_json(cast(str, row["route_json"]), strict=True)
        ticket = CentralWorkTicket(
            ticket_id=str(row["ticket_id"]), request_id=str(row["request_id"]), org_id=str(row["org_id"]),
            owner_id=str(row["owner_id"]), agent_id=str(row["agent_id"]), route=route,
            attempt=int(row["attempt"]), created_at=_parse_timestamp(str(row["created_at"])),
        )
        if row["status"] not in {"pending", "completed"} or _work_ticket_digest(ticket) != row["create_digest"]:
            raise ValueError
        return ticket
    except Exception as error:
        raise CentralQuestionLifecycleUnavailable("corrupt work ticket") from error


def _conflict_candidates(raw: str) -> tuple[dict[str, str], ...]:
    try:
        value = _CONFLICT_CANDIDATES.validate_json(raw, strict=True)
        candidates: list[dict[str, str]] = []
        for candidate in value:
            if (
                set(candidate) != {"agent_id", "owner_id"}
            ):
                raise ValueError
            agent_id = candidate["agent_id"]
            owner_id = candidate["owner_id"]
            if (
                not agent_id.strip()
                or not owner_id.strip()
            ):
                raise ValueError
            candidates.append({"agent_id": agent_id, "owner_id": owner_id})
        if _canonical_json(candidates) != raw:
            raise ValueError
        return tuple(candidates)
    except Exception as error:
        raise CentralQuestionLifecycleUnavailable("corrupt conflict candidates") from error


def _request_values(request: QuestionRequest) -> tuple[object, ...]:
    return (request.request_id, request.org_id, request.requester_id, request.session_id, request.question,
        request.context_snapshot, request.intent, request.initial_disposition, request.state.kind,
        _canonical_json(request.state.model_dump(mode="json")), 1, request.revision,
        request.created_at.isoformat(), request.updated_at.isoformat())


def _insert_request(connection: sqlite3.Connection, request: QuestionRequest) -> None:
    connection.execute("INSERT INTO question_requests(request_id,org_id,requester_id,session_id,question,context_snapshot,intent,initial_disposition,state_kind,state_json,state_schema_version,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _request_values(request))


def _read_request(connection: sqlite3.Connection, request_id: str) -> QuestionRequest | None:
    row = connection.execute("SELECT * FROM question_requests WHERE request_id COLLATE BINARY=?", (request_id,)).fetchone()
    if row is None:
        return None
    return _row_request(row)


def _row_request(row: sqlite3.Row) -> QuestionRequest:
    try:
        state: QuestionRequestState = _STATE.validate_json(
            cast(str, row["state_json"]), strict=True
        )
        if state.kind != row["state_kind"] or row["state_schema_version"] != 1:
            raise ValueError("state mismatch")
        return QuestionRequest.model_validate({"request_id": row["request_id"], "org_id": row["org_id"],
            "requester_id": row["requester_id"], "session_id": row["session_id"], "question": row["question"],
            "context_snapshot": row["context_snapshot"], "intent": row["intent"], "initial_disposition": row["initial_disposition"],
            "state": state, "revision": row["revision"], "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"])}, strict=True)
    except Exception as error:
        raise CentralQuestionLifecycleUnavailable("corrupt question request") from error


def _receipt_received_request(raw: str) -> QuestionRequest:
    try:
        request = QuestionRequest.model_validate_json(raw, strict=True)
        if not isinstance(request.state, Received) or request.revision != 0:
            raise ValueError("receipt must retain original Received")
        return request
    except Exception as error:
        raise CentralQuestionLifecycleUnavailable("corrupt create receipt") from error


def _cas_request(connection: sqlite3.Connection, current: QuestionRequest, updated: QuestionRequest) -> None:
    cursor = connection.execute("UPDATE question_requests SET intent=?,initial_disposition=?,state_kind=?,state_json=?,state_schema_version=?,revision=?,updated_at=? WHERE request_id=? AND revision=?", (
        updated.intent, updated.initial_disposition, updated.state.kind, _canonical_json(updated.state.model_dump(mode="json")),
        1, updated.revision, updated.updated_at.isoformat(), current.request_id, current.revision))
    if cursor.rowcount != 1:
        raise CentralQuestionLifecycleConflict()


def _validate_approval_disposition_successor(
    connection: sqlite3.Connection, receipt: sqlite3.Row, request: QuestionRequest, item: sqlite3.Row,
) -> None:
    """Prove a resolved ApprovalItem has precisely its receipt-bound terminal successor."""
    decision = cast(str, receipt["decision_kind"])
    edited_text = cast(str | None, receipt["edited_text"])
    terminal_kind = "declined" if decision == "reject" else "answered"
    expected_item_status = "rejected" if terminal_kind == "declined" else "approved"
    try:
        candidate = _candidate_json_from_raw(cast(str, item["candidate_json"]))
        if candidate is None:
            raise ValueError
        if (
            receipt["org_id"] != request.org_id
            or item["request_id"] != request.request_id
            or item["org_id"] != request.org_id
            or item["candidate_digest"] != receipt["candidate_digest"]
            or item["policy_digest"] != receipt["policy_digest"]
            or item["binding_version"] != receipt["binding_version"]
            or int(item["revision"]) != int(receipt["resolved_item_revision"])
            or int(receipt["resolved_item_revision"]) != int(receipt["expected_approval_item_revision"]) + 1
            or int(request.revision) != int(receipt["terminal_request_revision"])
            or int(receipt["terminal_request_revision"]) != int(receipt["expected_request_revision"]) + 1
            or item["status"] != expected_item_status
            or receipt["terminal_kind"] != terminal_kind
            or receipt["receipt_id"] != _approval_disposition_receipt_id(receipt)
            or receipt["decision_digest"] != _approval_decision_digest_values(decision, edited_text)
            or not _valid_idempotency_key(receipt["idempotency_key"])
            or not str(receipt["actor_id"]).strip()
            or not str(receipt["identity_session_id"]).strip()
            or not str(receipt["authority_policy_version"]).strip()
            or len(str(receipt["authority_policy_digest"])) != 64
            or not _is_lowercase_sha256(receipt["authority_proof_digest"])
            or receipt["authority_proof_digest"] != _authority_proof_digest_from_values(receipt)
        ):
            raise ValueError
        if decision == "approve":
            if edited_text is not None:
                raise ValueError
        elif decision == "approve_with_edit":
            if type(edited_text) is not str or not edited_text.strip():
                raise ValueError
        elif decision == "reject":
            if edited_text is not None and (
                type(edited_text) is not str or edited_text == ""
            ):
                raise ValueError
        else:
            raise ValueError
        audit = connection.execute(
            "SELECT * FROM central_question_approval_disposition_audits WHERE approval_item_id=?",
            (receipt["approval_item_id"],),
        ).fetchone()
        if (
            audit is None
            or audit["receipt_id"] != receipt["receipt_id"]
            or audit["request_id"] != request.request_id
            or audit["approval_item_id"] != receipt["approval_item_id"]
            or audit["actor_id"] != receipt["actor_id"]
            or audit["identity_session_id"] != receipt["identity_session_id"]
            or audit["authority_policy_version"] != receipt["authority_policy_version"]
            or audit["authority_policy_digest"] != receipt["authority_policy_digest"]
            or not _is_lowercase_sha256(audit["authority_proof_digest"])
            or audit["authority_proof_digest"] != receipt["authority_proof_digest"]
            or audit["decision_kind"] != decision
            or audit["decision_digest"] != receipt["decision_digest"]
            or audit["terminal_kind"] != terminal_kind
            or audit["record_id"] != receipt["record_id"]
        ):
            raise ValueError
        if terminal_kind == "declined":
            if (
                receipt["record_id"] is not None
                or not isinstance(request.state, DeclinedRequest)
                or request.state.reason_code != "approval_rejected"
            ):
                raise ValueError
            return
        if not isinstance(request.state, AnsweredRequest) or request.state.record_id != receipt["record_id"]:
            raise ValueError
        record = connection.execute(
            "SELECT * FROM central_question_answer_records WHERE record_id=?", (receipt["record_id"],)
        ).fetchone()
        text = edited_text if decision == "approve_with_edit" else candidate.text
        if (
            record is None
            or record["request_id"] != request.request_id
            or record["ticket_id"] != item["ticket_id"]
            or record["org_id"] != request.org_id
            or record["owner_id"] != item["owner_id"]
            or record["agent_id"] != item["agent_id"]
            or record["text"] != text
            or record["sources_json"] != _canonical_json(list(candidate.sources))
            or record["mode"] != candidate.mode
            or record["review_status"] != "approved"
            or record["candidate_digest"] != item["candidate_digest"]
        ):
            raise ValueError
    except Exception as error:
        raise CentralQuestionLifecycleUnavailable("approval disposition successor binding mismatch") from error


def _validate_catalog(
    connection: sqlite3.Connection,
    *,
    root_manager_resolver: RootManagerResolver | None = None,
    card_binding_resolver: CardBindingResolver | None = None,
) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise CentralQuestionLifecycleUnavailable("foreign_keys disabled")
    placeholders = ",".join("?" for _ in _OWNED)
    names = tuple(str(row[0]) for row in connection.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders}) ORDER BY name", _OWNED
    ))
    if names != tuple(sorted(_OWNED)):
        raise CentralQuestionLifecycleUnavailable("lifecycle catalog unavailable")
    lifecycle_signature = _catalog_signature(connection)
    if lifecycle_signature not in {
        _EXPECTED_CATALOG,
        _EXPECTED_V16_APPROVAL_CATALOG,
    }:
        raise CentralQuestionLifecycleUnavailable("lifecycle catalog drift")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise CentralQuestionLifecycleUnavailable("lifecycle foreign key drift")
    for row in connection.execute(
        "SELECT r.org_id,r.requester_id,r.request_id,r.received_json,r.created_at,q.* "
        "FROM central_question_create_receipts r JOIN question_requests q ON q.request_id=r.request_id"
    ):
        original = _receipt_received_request(cast(str, row["received_json"]))
        current = _row_request(row)
        if (
            original.request_id != row["request_id"]
            or original.org_id != row["org_id"] != current.org_id
            or original.requester_id != row["requester_id"] != current.requester_id
            or original.question != current.question
            or original.created_at.isoformat() != row["created_at"]
        ):
            raise CentralQuestionLifecycleUnavailable("receipt/request binding mismatch")
    for row in connection.execute(
        "SELECT m.request_id AS manager_request_id,m.org_id AS manager_org,m.item_id,m.manager_id,q.* "
        "FROM central_question_manager_items m LEFT JOIN question_requests q ON q.request_id=m.request_id"
    ):
        if row["request_id"] is None:
            raise CentralQuestionLifecycleUnavailable("orphan manager item")
        request = _row_request(row)
        if (
            row["manager_request_id"] != request.request_id
            or row["manager_org"] != request.org_id
            or not isinstance(request.state, AwaitingManager)
            or request.state.item_id != row["item_id"]
        ):
            raise CentralQuestionLifecycleUnavailable("manager/request binding mismatch")
        if request.state.public_kind == "unowned":
            if root_manager_resolver is not None and row["manager_id"] != root_manager_resolver.resolve_root_manager(request.org_id, connection):
                raise CentralQuestionLifecycleUnavailable("manager root binding mismatch")
        elif request.state.public_kind == "contested":
            if not _table_exists(connection, "central_inbox_conflict_deadlock_manager_links"):
                raise CentralQuestionLifecycleUnavailable("deadlock manager link unavailable")
            link = connection.execute(
                "SELECT request_id,manager_item_id,manager_id FROM "
                "central_inbox_conflict_deadlock_manager_links WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            if (
                link is None
                or link["manager_item_id"] != row["item_id"]
                or link["manager_id"] != row["manager_id"]
            ):
                raise CentralQuestionLifecycleUnavailable("deadlock manager link mismatch")
        else:
            raise CentralQuestionLifecycleUnavailable("manager/request binding mismatch")
    for row in connection.execute(
        "SELECT c.request_id AS conflict_request_id,c.org_id AS conflict_org,c.case_id,c.intent,c.candidates_json,q.* "
        "FROM central_question_conflict_cases c LEFT JOIN question_requests q ON q.request_id=c.request_id"
    ):
        if row["request_id"] is None:
            raise CentralQuestionLifecycleUnavailable("orphan conflict case")
        request = _row_request(row)
        candidates = _conflict_candidates(cast(str, row["candidates_json"]))
        durable = (
            connection.execute(
                "SELECT state,resolved_outcome FROM central_inbox_conflict_cases WHERE case_id=?",
                (row["case_id"],),
            ).fetchone()
            if _table_exists(connection, "central_inbox_conflict_cases")
            else None
        )
        if durable is None or durable["state"] == "open":
            state_matches = (
                isinstance(request.state, AwaitingConflict)
                and request.state.case_id == row["case_id"]
            )
        elif durable["state"] == "resolved" and durable["resolved_outcome"] == "agreed":
            state_matches = isinstance(request.state, ReadyToDispatch)
        elif durable["state"] == "resolved" and durable["resolved_outcome"] == "deadlocked":
            state_matches = (
                isinstance(request.state, AwaitingManager)
                and request.state.public_kind == "contested"
            )
        elif durable["state"] == "resolved" and durable["resolved_outcome"] == "route_rejected":
            state_matches = (
                isinstance(request.state, DeclinedRequest)
                and request.state.reason_code == "route_rejected"
            )
        else:
            state_matches = False
        if (
            row["conflict_request_id"] != request.request_id
            or row["conflict_org"] != request.org_id
            or not state_matches
            or request.initial_disposition != "contested"
            or request.intent != row["intent"]
            or len(candidates) < 2
            or len({candidate["agent_id"] for candidate in candidates}) != len(candidates)
        ):
            raise CentralQuestionLifecycleUnavailable("conflict/request binding mismatch")
        if card_binding_resolver is not None and (durable is None or durable["state"] == "open"):
            for candidate in candidates:
                binding = card_binding_resolver.resolve_card_binding(
                    request.org_id, candidate["agent_id"], connection
                )
                if binding.owner_id != candidate["owner_id"] or binding.agent_id != candidate["agent_id"]:
                    raise CentralQuestionLifecycleUnavailable("conflict candidate binding mismatch")
    for row in connection.execute(
        "SELECT t.request_id AS ticket_request_id,t.attempt AS ticket_attempt,r.command_digest,r.ticket_id AS receipt_ticket_id,q.*,t.* "
        "FROM central_question_work_tickets t "
        "LEFT JOIN central_question_work_ticket_receipts r ON r.request_id=t.request_id AND r.attempt=t.attempt "
        "LEFT JOIN question_requests q ON q.request_id=t.request_id"
    ):
        if row["request_id"] is None or row["command_digest"] is None:
            raise CentralQuestionLifecycleUnavailable("orphan work ticket")
        request = _row_request(row)
        ticket = _row_work_ticket(row)
        pending_binding = (
            row["ticket_request_id"] != request.request_id
            or row["ticket_attempt"] != ticket.attempt
            or row["receipt_ticket_id"] != ticket.ticket_id
            or row["command_digest"] != _work_ticket_digest(ticket)
            or request.org_id != ticket.org_id
        )
        if pending_binding:
            raise CentralQuestionLifecycleUnavailable("work ticket/request binding mismatch")
        if row["status"] == "pending":
            if (
                not isinstance(request.state, AwaitingAnswer)
                or request.state.ticket_id != ticket.ticket_id
                or request.state.attempt != ticket.attempt
                or request.state.route != ticket.route
            ):
                raise CentralQuestionLifecycleUnavailable("pending work ticket/request binding mismatch")
        elif row["status"] != "completed":
            raise CentralQuestionLifecycleUnavailable("work ticket status unavailable")
    for row in connection.execute(
        "SELECT c.*,t.ticket_id AS joined_ticket_id FROM central_question_work_ticket_delivery_claims c "
        "LEFT JOIN central_question_work_tickets t ON t.ticket_id=c.ticket_id"
    ):
        try:
            ticket_id = str(row["ticket_id"])
            if row["joined_ticket_id"] != ticket_id or not str(row["worker_id"]).strip():
                raise ValueError
            ticket_status = connection.execute(
                "SELECT status FROM central_question_work_tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if ticket_status is None or ticket_status[0] != "pending":
                raise ValueError
            _parse_timestamp(str(row["lease_until"]))
            attempt = int(row["delivery_attempt"])
            if attempt < 1:
                raise ValueError
            if row["status"] == "leased":
                if row["ack_receipt"] is not None or row["acked_at"] is not None:
                    raise ValueError
            elif row["status"] == "delivered":
                if (
                    row["ack_receipt"] != _delivery_ack_receipt(ticket_id, attempt)
                    or row["acked_at"] is None
                ):
                    raise ValueError
                _parse_timestamp(str(row["acked_at"]))
            else:
                raise ValueError
        except Exception as error:
            raise CentralQuestionLifecycleUnavailable("delivery claim binding mismatch") from error
    for row in connection.execute(
        "SELECT i.*,t.request_id AS ticket_request_id,t.attempt AS ticket_attempt,t.org_id AS ticket_org,"
        "t.owner_id,t.agent_id,t.route_json AS ticket_route,t.status AS ticket_status,q.* "
        "FROM central_question_answer_ingest_receipts i "
        "LEFT JOIN central_question_work_tickets t ON t.ticket_id=i.ticket_id "
        "LEFT JOIN question_requests q ON q.request_id=i.request_id"
    ):
        if row["ticket_org"] is None or row["request_id"] is None:
            raise CentralQuestionLifecycleUnavailable("orphan answer ingest receipt")
        request = _row_request(row)
        route = _ROUTE.validate_json(cast(str, row["route_json"]), strict=True)
        candidate = _candidate_json_from_raw(cast(str, row["candidate_json"]))
        if (
            row["org_id"] != row["ticket_org"] != request.org_id
            or row["request_id"] != row["ticket_request_id"] != request.request_id
            or row["attempt"] != row["ticket_attempt"]
            or route != _ROUTE.validate_json(cast(str, row["ticket_route"]), strict=True)
            or row["ticket_status"] != "completed"
            or candidate is None
            or sha256(cast(str, row["candidate_json"]).encode()).hexdigest() != row["candidate_digest"]
        ):
            raise CentralQuestionLifecycleUnavailable("answer ingest receipt binding mismatch")
        if row["result_kind"] == "answered":
            if not isinstance(request.state, AnsweredRequest) or request.state.record_id != row["record_id"]:
                raise CentralQuestionLifecycleUnavailable("answered ingest/request binding mismatch")
            record = connection.execute(
                "SELECT * FROM central_question_answer_records WHERE record_id=?", (row["record_id"],)
            ).fetchone()
            if (
                record is None
                or record["request_id"] != request.request_id
                or record["ticket_id"] != row["ticket_id"]
                or record["org_id"] != request.org_id
                or record["owner_id"] != row["owner_id"]
                or record["agent_id"] != row["agent_id"]
                or record["text"] != candidate.text
                or record["candidate_digest"] != row["candidate_digest"]
                or record["mode"] != candidate.mode
                or _safe_sources(cast(str, record["sources_json"]))
                != candidate.sources
            ):
                raise CentralQuestionLifecycleUnavailable("answer record/receipt binding mismatch")
        elif row["result_kind"] == "awaiting_approval":
            item = connection.execute(
                "SELECT * FROM central_question_approval_items WHERE approval_item_id=?", (row["approval_item_id"],)
            ).fetchone()
            if (
                item is None
                or item["request_id"] != request.request_id
                or item["ticket_id"] != row["ticket_id"]
                or item["org_id"] != request.org_id
                or item["owner_id"] != row["owner_id"]
                or item["agent_id"] != row["agent_id"]
                or item["route_json"] != row["route_json"]
                or item["attempt"] != row["attempt"]
                or item["candidate_json"] != row["candidate_json"]
                or item["candidate_digest"] != row["candidate_digest"]
                or item["policy_digest"] != row["policy_digest"]
                or item["binding_version"] != row["binding_version"]
            ):
                raise CentralQuestionLifecycleUnavailable("approval item/receipt binding mismatch")
            reassignment_count = (
                int(
                    connection.execute(
                        "SELECT count(*) FROM central_inbox_approval_reassignment_receipts "
                        "WHERE request_id=?",
                        (request.request_id,),
                    ).fetchone()[0]
                )
                if _table_exists(
                    connection, "central_inbox_approval_reassignment_receipts"
                )
                else 0
            )
            active_item = (
                item
                if not isinstance(request.state, AwaitingApproval)
                or request.state.draft_ref == row["approval_item_id"]
                else connection.execute(
                    "SELECT * FROM central_question_approval_items "
                    "WHERE approval_item_id=? AND request_id=?",
                    (request.state.draft_ref, request.request_id),
                ).fetchone()
            )
            if isinstance(request.state, AwaitingApproval):
                if (
                    active_item is None
                    or active_item["status"] != "open"
                    or (
                        reassignment_count == 0
                        and request.state.draft_ref != row["approval_item_id"]
                    )
                    or (
                        reassignment_count > 0
                        and item["status"] != "superseded"
                    )
                ):
                    raise CentralQuestionLifecycleUnavailable("open approval ingest/request binding mismatch")
            elif (
                reassignment_count == 0
                and item["status"] not in {"approved", "rejected"}
            ) or (
                reassignment_count > 0
                and item["status"] != "superseded"
            ):
                raise CentralQuestionLifecycleUnavailable("resolved approval item unavailable")
        else:
            raise CentralQuestionLifecycleUnavailable("answer ingest result unavailable")
        approval_reassignments = (
            int(
                connection.execute(
                    "SELECT count(*) FROM central_inbox_approval_reassignment_receipts "
                    "WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()[0]
            )
            if row["result_kind"] == "awaiting_approval"
            and _table_exists(
                connection, "central_inbox_approval_reassignment_receipts"
            )
            else 0
        )
        expected_revision = int(row["expected_request_revision"]) + (
            1
            if row["result_kind"] == "answered"
            or isinstance(request.state, AwaitingApproval)
            else 2
        ) + approval_reassignments
        if request.revision != expected_revision:
            raise CentralQuestionLifecycleUnavailable("answer ingest predecessor revision mismatch")
        audit = connection.execute(
            "SELECT request_id,receipt_ticket_id,event_kind FROM central_question_answer_ingest_audits WHERE ticket_id=?",
            (row["ticket_id"],),
        ).fetchone()
        if (
            audit is None
            or audit["request_id"] != request.request_id
            or audit["receipt_ticket_id"] != row["ticket_id"]
            or audit["event_kind"] != row["result_kind"]
        ):
            raise CentralQuestionLifecycleUnavailable("answer ingest audit binding mismatch")
    for row in connection.execute(
        "SELECT a.*,i.ticket_id AS receipt_ticket_id,i.request_id AS receipt_request_id,i.result_kind,"
        "d.request_id AS disposition_request_id,d.terminal_kind "
        "FROM central_question_answer_records a LEFT JOIN central_question_answer_ingest_receipts i "
        "ON i.record_id=a.record_id AND i.result_kind='answered' LEFT JOIN central_question_approval_disposition_receipts d "
        "ON d.record_id=a.record_id AND d.terminal_kind='answered'"
    ):
        ingest_link = row["receipt_ticket_id"] is not None and row["ticket_id"] == row["receipt_ticket_id"] and row["request_id"] == row["receipt_request_id"]
        disposition_link = row["disposition_request_id"] is not None and row["request_id"] == row["disposition_request_id"]
        if not ingest_link and not disposition_link:
            raise CentralQuestionLifecycleUnavailable("receipt-less answer record")
    for row in connection.execute(
        "SELECT a.*,i.ticket_id AS receipt_ticket_id,i.request_id AS receipt_request_id,i.result_kind "
        "FROM central_question_approval_items a LEFT JOIN central_question_answer_ingest_receipts i "
        "ON i.approval_item_id=a.approval_item_id AND i.result_kind='awaiting_approval'"
    ):
        successor = (
            _table_exists(connection, "central_inbox_approval_assignments")
            and connection.execute(
                "SELECT 1 FROM central_inbox_approval_assignments a "
                "JOIN central_inbox_approval_reassignment_receipts r "
                "ON r.successor_approval_item_id=a.approval_item_id "
                "WHERE a.approval_item_id=? AND a.predecessor_approval_item_id "
                "=r.superseded_approval_item_id AND r.request_id=a.request_id",
                (row["approval_item_id"],),
            ).fetchone()
            is not None
        )
        if not successor and (
            row["receipt_ticket_id"] is None
            or row["ticket_id"] != row["receipt_ticket_id"]
            or row["request_id"] != row["receipt_request_id"]
        ):
            raise CentralQuestionLifecycleUnavailable("receipt-less approval item")
    for row in connection.execute(
        "SELECT a.*,i.ticket_id AS expected_ticket_id,i.request_id AS expected_request_id,i.result_kind "
        "FROM central_question_answer_ingest_audits a LEFT JOIN central_question_answer_ingest_receipts i "
        "ON i.ticket_id=a.receipt_ticket_id"
    ):
        if (
            row["expected_ticket_id"] is None
            or row["ticket_id"] != row["expected_ticket_id"]
            or row["request_id"] != row["expected_request_id"]
            or row["event_kind"] != row["result_kind"]
        ):
            raise CentralQuestionLifecycleUnavailable("receipt-less answer ingest audit")
    for receipt in connection.execute(
        "SELECT * FROM central_question_approval_disposition_receipts"
    ):
        request = _read_request(connection, cast(str, receipt["request_id"]))
        item = connection.execute(
            "SELECT * FROM central_question_approval_items WHERE approval_item_id=?",
            (receipt["approval_item_id"],),
        ).fetchone()
        if request is None or item is None:
            raise CentralQuestionLifecycleUnavailable("orphan approval disposition receipt")
        _validate_approval_disposition_successor(connection, receipt, request, item)
    for item in connection.execute("SELECT approval_item_id,status FROM central_question_approval_items"):
        receipt_count = int(connection.execute(
            "SELECT COUNT(*) FROM central_question_approval_disposition_receipts WHERE approval_item_id=?",
            (item["approval_item_id"],),
        ).fetchone()[0])
        audit_count = int(connection.execute(
            "SELECT COUNT(*) FROM central_question_approval_disposition_audits WHERE approval_item_id=?",
            (item["approval_item_id"],),
        ).fetchone()[0])
        expected_count = 1 if item["status"] in {"approved", "rejected"} else 0
        if item["status"] not in {"open", "approved", "rejected", "superseded"} or (
            receipt_count, audit_count
        ) != (expected_count, expected_count):
            raise CentralQuestionLifecycleUnavailable("approval item disposition cardinality mismatch")
    orphan_audit = connection.execute(
        "SELECT a.receipt_id FROM central_question_approval_disposition_audits a LEFT JOIN "
        "central_question_approval_disposition_receipts r ON r.receipt_id=a.receipt_id "
        "WHERE r.receipt_id IS NULL"
    ).fetchone()
    if orphan_audit is not None:
        raise CentralQuestionLifecycleUnavailable("orphan approval disposition audit")
    for record in connection.execute("SELECT * FROM central_question_feedback_records"):
        request = _read_request(connection, str(record["request_id"]))
        answer = connection.execute(
            "SELECT * FROM central_question_answer_records WHERE record_id=?", (record["record_id"],)
        ).fetchone()
        receipt = connection.execute(
            "SELECT * FROM central_question_feedback_receipts WHERE feedback_id=?", (record["feedback_id"],)
        ).fetchone()
        audit = connection.execute(
            "SELECT * FROM central_question_feedback_audits WHERE feedback_id=?", (record["feedback_id"],)
        ).fetchone()
        if (
            request is None or answer is None or receipt is None or audit is None
            or not isinstance(request.state, AnsweredRequest) or request.state.record_id != record["record_id"]
            or record["org_id"] != request.org_id
            or answer["org_id"] != request.org_id
            or receipt["org_id"] != request.org_id
            or audit["org_id"] != request.org_id
            or record["request_id"] != request.request_id
            or answer["request_id"] != request.request_id
            or record["verdict"] not in {"good", "bad"}
            or not _is_lowercase_sha256(record["payload_digest"])
            or record["payload_digest"] != _feedback_payload_digest_from_values(record)
            or type(record["comment"]) is not str or len(str(record["comment"]).encode("utf-8")) > 4096
            or receipt["request_id"] != request.request_id
            or receipt["record_id"] != record["record_id"]
            or record["requester_id"] != request.requester_id
            or receipt["requester_id"] != request.requester_id
            or receipt["payload_digest"] != record["payload_digest"] or receipt["submitted_at"] != record["submitted_at"]
            or receipt["action"] != "feedback.create" or not _valid_idempotency_key(receipt["idempotency_key"])
            or receipt["receipt_id"] != _feedback_receipt_id_from_values(receipt)
            or audit["receipt_id"] != receipt["receipt_id"]
            or audit["request_id"] != request.request_id
            or audit["record_id"] != record["record_id"]
            or audit["requester_id"] != request.requester_id
            or audit["verdict"] != record["verdict"] or audit["payload_digest"] != record["payload_digest"]
            or audit["submitted_at"] != record["submitted_at"]
        ):
            raise CentralQuestionLifecycleUnavailable("feedback evidence binding mismatch")
    feedback_orphan = connection.execute(
        "SELECT r.feedback_id FROM central_question_feedback_receipts r LEFT JOIN central_question_feedback_records f "
        "ON f.feedback_id=r.feedback_id WHERE f.feedback_id IS NULL"
    ).fetchone()
    feedback_audit_orphan = connection.execute(
        "SELECT a.feedback_id FROM central_question_feedback_audits a LEFT JOIN central_question_feedback_records f "
        "ON f.feedback_id=a.feedback_id WHERE f.feedback_id IS NULL"
    ).fetchone()
    if feedback_orphan is not None or feedback_audit_orphan is not None:
        raise CentralQuestionLifecycleUnavailable("orphan feedback evidence")
    missing_conflicts = connection.execute(
        "SELECT request_id FROM question_requests WHERE state_kind='awaiting_conflict' AND 1 != "
        "(SELECT COUNT(*) FROM central_question_conflict_cases c WHERE c.request_id=question_requests.request_id)"
    ).fetchone()
    missing_tickets = connection.execute(
        "SELECT request_id FROM question_requests WHERE state_kind='awaiting_answer' AND 1 != "
        "(SELECT COUNT(*) FROM central_question_work_tickets t WHERE t.request_id=question_requests.request_id)"
    ).fetchone()
    orphan_receipt = connection.execute(
        "SELECT r.ticket_id FROM central_question_work_ticket_receipts r LEFT JOIN central_question_work_tickets t "
        "ON t.request_id=r.request_id AND t.attempt=r.attempt "
        "WHERE t.ticket_id IS NULL OR t.ticket_id != r.ticket_id"
    ).fetchone()
    missing_ingest = connection.execute(
        "SELECT t.ticket_id FROM central_question_work_tickets t WHERE t.status='completed' AND 1 != "
        "(SELECT COUNT(*) FROM central_question_answer_ingest_receipts i WHERE i.ticket_id=t.ticket_id)"
    ).fetchone()
    pending_ingest = connection.execute(
        "SELECT t.ticket_id FROM central_question_work_tickets t WHERE t.status='pending' AND EXISTS "
        "(SELECT 1 FROM central_question_answer_ingest_receipts i WHERE i.ticket_id=t.ticket_id)"
    ).fetchone()
    missing_manager_items = connection.execute(
        "SELECT q.request_id FROM question_requests q WHERE q.state_kind='awaiting_manager' "
        "AND 1 != (SELECT COUNT(*) FROM central_question_manager_items m "
        "WHERE m.request_id=q.request_id AND m.org_id=q.org_id)"
    ).fetchone()
    if (
        missing_conflicts is not None
        or missing_manager_items is not None
        or missing_tickets is not None
        or orphan_receipt is not None
        or missing_ingest is not None
        or pending_ingest is not None
    ):
        raise CentralQuestionLifecycleUnavailable("lifecycle reverse aggregate binding mismatch")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _append_question_received_evidence(
    connection: sqlite3.Connection, request: QuestionRequest, idempotency_key: str,
    *, grant: AuthorizationGrant | None,
) -> None:
    """The v19 evidence pair shares this lifecycle create UoW, never its text."""
    if grant is None:
        if _v19_enabled(connection):
            raise CentralQuestionLifecycleUnavailable(
                "question create Authority provenance unavailable"
            )
        return
    digest = _question_create_command_digest(request, idempotency_key)
    audit_authority = canonical_v19_file_authority(
        source_policy_digest=grant.policy_digest,
        current_snapshot_digest=grant.policy_digest,
    )
    append_committed_source_evidence_if_v19(
        connection, org_id=request.org_id,
        receipt_id=f"question-create:{request.request_id}", command_digest=digest,
        event_type="question_received", action="question.create",
        resource=SafeResourceRef(kind="question_request", resource_id=request.request_id),
        change=QuestionChange(request_id=request.request_id, to_state="received"),
        actor_user_id=request.requester_id, occurred_at=request.created_at.isoformat(),
        policy_revision_id=audit_authority.policy_revision_id,
        policy_epoch=audit_authority.policy_epoch,
        policy_digest=audit_authority.policy_digest,
        source=SourceReceiptProvenance(
            kind="question_create", receipt_key=request.request_id,
            receipt_digest=source_receipt_digest(
                connection, "question_create", request.org_id, request.request_id
            ),
        ),
    )


def _question_create_command_digest(
    request: QuestionRequest, idempotency_key: str
) -> str:
    return sha256(
        _canonical_json({
            "org_id": request.org_id, "requester_id": request.requester_id,
            "request_id": request.request_id, "idempotency_key": idempotency_key,
            "question": request.question,
        }).encode()
    ).hexdigest()


def _append_answer_ingest_evidence(
    connection: sqlite3.Connection, *, ticket: CentralWorkTicket,
    request: QuestionRequest, command: OwnerAnswerIngest,
    authorization: AnswerIngestAuthorization | str | None,
    event_kind: Literal["answered", "awaiting_approval"], occurred_at: str,
    candidate_digest: str, record_id: str | None,
    approval_item_id: str | None,
) -> None:
    if type(authorization) is not AnswerIngestAuthorization:
        if _v19_enabled(connection):
            raise CentralQuestionLifecycleUnavailable(
                "answer ingest Authority provenance unavailable"
            )
        return
    audit_authority = canonical_v19_file_authority(
        source_policy_digest=authorization.policy_digest,
        current_snapshot_digest=authorization.policy_digest,
    )
    if event_kind == "answered":
        event_type = "answer_finalized"
        action = "answer.ingest"
        resource = SafeResourceRef(
            kind="question_request", resource_id=request.request_id
        )
        change = AnswerChange(
            request_id=request.request_id, record_id=record_id
        )
        source_kind = "answer_ingest"
    else:
        if approval_item_id is None:
            raise CentralQuestionLifecycleUnavailable(
                "approval creation evidence unavailable"
            )
        event_type = "approval_changed"
        action = "approval.create"
        resource = SafeResourceRef(
            kind="approval_item", resource_id=approval_item_id
        )
        change = ApprovalChange(
            approval_item_id=approval_item_id, request_id=request.request_id
        )
        source_kind = "approval_creation"
    append_committed_source_evidence_if_v19(
        connection, org_id=ticket.org_id,
        receipt_id=f"answer-ingest:{ticket.ticket_id}",
        command_digest=candidate_digest, event_type=event_type, action=action,
        resource=resource, change=change,
        actor_user_id=command.delivery_subject, occurred_at=occurred_at,
        policy_revision_id=audit_authority.policy_revision_id,
        policy_epoch=audit_authority.policy_epoch,
        policy_digest=audit_authority.policy_digest,
        source=SourceReceiptProvenance(
            kind=source_kind, receipt_key=ticket.ticket_id,
            receipt_digest=source_receipt_digest(
                connection, source_kind, ticket.org_id, ticket.ticket_id
            ),
        ),
    )


def _append_dispatch_state_evidence(
    connection: sqlite3.Connection, *, ticket: CentralWorkTicket,
    request_id: str,
    authorization: RouteAuthorization | str | None,
) -> None:
    if type(authorization) is not RouteAuthorization:
        if _v19_enabled(connection):
            raise CentralQuestionLifecycleUnavailable(
                "dispatch Authority provenance unavailable"
            )
        return
    audit_authority = canonical_v19_file_authority(
        source_policy_digest=authorization.policy_digest,
        current_snapshot_digest=authorization.policy_digest,
    )
    command_digest = _work_ticket_digest(ticket)
    append_committed_source_evidence_if_v19(
        connection, org_id=ticket.org_id,
        receipt_id=f"question-dispatch:{ticket.ticket_id}",
        command_digest=command_digest, event_type="request_state_changed",
        action="question.dispatch",
        resource=SafeResourceRef(
            kind="question_request", resource_id=request_id
        ),
        change=QuestionChange(
            request_id=request_id, from_state="ready_to_dispatch",
            to_state="awaiting_answer",
        ),
        actor_user_id=None, occurred_at=ticket.created_at.isoformat(),
        policy_revision_id=audit_authority.policy_revision_id,
        policy_epoch=audit_authority.policy_epoch,
        policy_digest=audit_authority.policy_digest,
        source=SourceReceiptProvenance(
            kind="question_dispatch_state", receipt_key=ticket.ticket_id,
            receipt_digest=source_receipt_digest(
                connection, "question_dispatch_state", ticket.org_id,
                ticket.ticket_id,
            ),
        ),
    )


def _append_initial_transition_evidence(
    connection: sqlite3.Connection, *, current: QuestionRequest,
    updated: QuestionRequest, manager_item_id: str | None,
    authorization: RouteAuthorization | str | None,
) -> None:
    if type(authorization) is not RouteAuthorization:
        if _v19_enabled(connection):
            raise CentralQuestionLifecycleUnavailable(
                "initial transition Authority provenance unavailable"
            )
        return
    authority = canonical_v19_file_authority(
        source_policy_digest=authorization.policy_digest,
        current_snapshot_digest=authorization.policy_digest,
    )
    receipt_id = f"initial-transition:{current.request_id}"
    command_digest = sha256(
        _canonical_json(
            {
                "org_id": current.org_id,
                "request_id": current.request_id,
                "from_state": current.state.kind,
                "to_state": updated.state.kind,
                "manager_item_id": manager_item_id,
                "policy_revision_id": authority.policy_revision_id,
                "policy_epoch": authority.policy_epoch,
                "policy_digest": authority.policy_digest,
                "created_at": updated.updated_at.isoformat(),
            }
        ).encode()
    ).hexdigest()
    connection.execute(
        "INSERT INTO central_question_initial_transition_receipts"
        "(receipt_id,org_id,request_id,command_digest,from_state,to_state,"
        "manager_item_id,policy_revision_id,policy_epoch,policy_digest,"
        "authority_policy_revision_id,authority_policy_epoch,authority_policy_digest,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_id, current.org_id, current.request_id, command_digest,
            current.state.kind, updated.state.kind, manager_item_id,
            authority.policy_revision_id, authority.policy_epoch,
            authority.policy_digest, authority.policy_revision_id,
            authority.policy_epoch, authority.policy_digest,
            updated.updated_at.isoformat(),
        ),
    )
    append_committed_source_evidence_if_v19(
        connection, org_id=current.org_id,
        receipt_id=f"question-initial:{current.request_id}",
        command_digest=command_digest,
        event_type="request_state_changed",
        action="question.initial_transition",
        resource=SafeResourceRef(
            kind="question_request", resource_id=current.request_id
        ),
        change=QuestionChange(
            request_id=current.request_id,
            from_state=cast(
                Literal["received"], current.state.kind
            ),
            to_state=cast(
                Literal[
                    "awaiting_manager", "awaiting_conflict",
                    "ready_to_dispatch", "declined",
                ],
                updated.state.kind,
            ),
        ),
        actor_user_id=None, occurred_at=updated.updated_at.isoformat(),
        policy_revision_id=authority.policy_revision_id,
        policy_epoch=authority.policy_epoch,
        policy_digest=authority.policy_digest,
        source=SourceReceiptProvenance(
            kind="question_initial_state", receipt_key=receipt_id,
            receipt_digest=source_receipt_digest(
                connection, "question_initial_state", current.org_id,
                receipt_id,
            ),
        ),
    )
    if manager_item_id is not None:
        append_committed_source_evidence_if_v19(
            connection, org_id=current.org_id,
            receipt_id=f"manager-initial:{current.request_id}",
            command_digest=command_digest,
            event_type="manager_item_changed", action="manager_item.create",
            resource=SafeResourceRef(
                kind="manager_item", resource_id=manager_item_id
            ),
            change=ManagerItemChange(
                manager_item_id=manager_item_id,
                request_id=current.request_id,
            ),
            actor_user_id=None, occurred_at=updated.updated_at.isoformat(),
            policy_revision_id=authority.policy_revision_id,
            policy_epoch=authority.policy_epoch,
            policy_digest=authority.policy_digest,
            source=SourceReceiptProvenance(
                kind="manager_initial", receipt_key=receipt_id,
                receipt_digest=source_receipt_digest(
                    connection, "manager_initial", current.org_id,
                    receipt_id,
                ),
            ),
        )


def _append_feedback_evidence(
    connection: sqlite3.Connection, *, request: QuestionRequest,
    record_id: str, feedback_id: str, receipt_id: str,
    payload_digest: str, occurred_at: str, grant: AuthorizationGrant,
    actor_user_id: str,
) -> None:
    audit_authority = canonical_v19_file_authority(
        source_policy_digest=grant.policy_digest,
        current_snapshot_digest=grant.policy_digest,
    )
    append_committed_source_evidence_if_v19(
        connection, org_id=request.org_id, receipt_id=receipt_id,
        command_digest=payload_digest, event_type="feedback_recorded",
        action="question.feedback",
        resource=SafeResourceRef(
            kind="question_request", resource_id=request.request_id
        ),
        change=FeedbackChange(
            request_id=request.request_id, record_id=record_id,
            feedback_id=feedback_id,
        ),
        actor_user_id=actor_user_id, occurred_at=occurred_at,
        policy_revision_id=audit_authority.policy_revision_id,
        policy_epoch=audit_authority.policy_epoch,
        policy_digest=audit_authority.policy_digest,
        source=SourceReceiptProvenance(
            kind="feedback", receipt_key=receipt_id,
            receipt_digest=source_receipt_digest(
                connection, "feedback", request.org_id, receipt_id
            ),
        ),
    )


def _append_approval_disposition_evidence(
    connection: sqlite3.Connection, *, receipt_id: str, request_id: str,
    approval_item_id: str, actor_user_id: str, decision_digest: str,
    terminal_kind: Literal["answered", "declined"], record_id: str | None,
    occurred_at: str, grant: AuthorizationGrant, org_id: str,
) -> None:
    audit_authority = canonical_v19_file_authority(
        source_policy_digest=grant.policy_digest,
        current_snapshot_digest=grant.policy_digest,
    )
    source = SourceReceiptProvenance(
        kind="approval_disposition", receipt_key=receipt_id,
        receipt_digest=source_receipt_digest(
            connection, "approval_disposition", org_id, receipt_id
        ),
    )
    append_committed_source_evidence_if_v19(
        connection, org_id=org_id,
        receipt_id=f"approval-disposition:{receipt_id}",
        command_digest=decision_digest, event_type="approval_changed",
        action="approval.decide",
        resource=SafeResourceRef(
            kind="approval_item", resource_id=approval_item_id
        ),
        change=ApprovalChange(
            approval_item_id=approval_item_id, request_id=request_id
        ),
        actor_user_id=actor_user_id, occurred_at=occurred_at,
        policy_revision_id=audit_authority.policy_revision_id,
        policy_epoch=audit_authority.policy_epoch,
        policy_digest=audit_authority.policy_digest, source=source,
    )
    if terminal_kind == "answered":
        append_committed_source_evidence_if_v19(
            connection, org_id=org_id,
            receipt_id=f"answer-finalization:{receipt_id}",
            command_digest=decision_digest, event_type="answer_finalized",
            action="answer.finalize",
            resource=SafeResourceRef(
                kind="question_request", resource_id=request_id
            ),
            change=AnswerChange(request_id=request_id, record_id=record_id),
            actor_user_id=actor_user_id, occurred_at=occurred_at,
            policy_revision_id=audit_authority.policy_revision_id,
            policy_epoch=audit_authority.policy_epoch,
            policy_digest=audit_authority.policy_digest,
            source=SourceReceiptProvenance(
                kind="answer_finalization", receipt_key=receipt_id,
                receipt_digest=source.receipt_digest,
            ),
        )


def _v19_enabled(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "aon_installation_schema"):
        return False
    marker = connection.execute(
        "SELECT version FROM aon_installation_schema "
        "WHERE name='central-installation'"
    ).fetchone()
    return marker is not None and int(marker[0]) >= 19


def _catalog_signature_for(
    connection: sqlite3.Connection, tables_to_check: tuple[str, ...]
) -> tuple[object, ...]:
    tables = tuple(
        (
            table,
            " ".join(str(connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]).split()),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({table})")),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA index_list({table})")),
        )
        for table in sorted(tables_to_check)
    )
    placeholders = ",".join("?" for _ in tables_to_check)
    triggers = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type='trigger' "
            f"AND tbl_name IN ({placeholders}) ORDER BY type,name", tables_to_check
        )
    )
    return tables, triggers


def _catalog_signature(connection: sqlite3.Connection) -> tuple[object, ...]:
    return _catalog_signature_for(connection, _OWNED)


def _expected_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _TABLES:
            connection.execute(ddl)
        for ddl in _FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        for ddl in _INITIAL_TRANSITION_TRIGGERS:
            connection.execute(ddl)
        return _catalog_signature(connection)
    finally:
        connection.close()


def _expected_v14_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)"
        )
        for ddl in _V14_TABLES:
            connection.execute(ddl)
        for ddl in _FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        return _catalog_signature_for(connection, _V14_OWNED)
    finally:
        connection.close()


_EXPECTED_V14_CATALOG = _expected_v14_catalog()
_EXPECTED_CATALOG = _expected_catalog()


def _expected_v16_approval_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _TABLES[:8]:
            connection.execute(ddl)
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
        for ddl in _TABLES[9:]:
            connection.execute(ddl)
        for ddl in _FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        for ddl in _INITIAL_TRANSITION_TRIGGERS:
            connection.execute(ddl)
        return _catalog_signature(connection)
    finally:
        connection.close()


_EXPECTED_V16_APPROVAL_CATALOG = _expected_v16_approval_catalog()


def _expected_v13_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _V13_TABLES:
            connection.execute(ddl)
        for ddl in _V13_FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        return _catalog_signature_for(connection, _V13_OWNED)
    finally:
        connection.close()


_EXPECTED_V13_CATALOG = _expected_v13_catalog()


def _expected_v13_approval_v16_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)"
        )
        for ddl in _V13_TABLES[:8]:
            connection.execute(ddl)
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
        for ddl in _V13_TABLES[9:]:
            connection.execute(ddl)
        for ddl in _V13_FEEDBACK_TRIGGERS:
            connection.execute(ddl)
        return _catalog_signature_for(connection, _V13_OWNED)
    finally:
        connection.close()


_EXPECTED_V13_APPROVAL_V16_CATALOG = _expected_v13_approval_v16_catalog()


def _expected_v12_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _V12_TABLES:
            connection.execute(ddl)
        return _catalog_signature_for(connection, _V12_OWNED)
    finally:
        connection.close()


_EXPECTED_V12_CATALOG = _expected_v12_catalog()


def _expected_v11_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _V11_TABLES:
            connection.execute(ddl)
        return _catalog_signature_for(connection, _V11_OWNED)
    finally:
        connection.close()


_EXPECTED_V11_CATALOG = _expected_v11_catalog()


def _expected_v10_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _V10_TABLES:
            connection.execute(ddl)
        return _catalog_signature_for(connection, _V10_OWNED)
    finally:
        connection.close()


_EXPECTED_V10_CATALOG = _expected_v10_catalog()


def _expected_v9_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _V9_TABLES:
            connection.execute(ddl)
        return _catalog_signature_for(connection, _V9_OWNED)
    finally:
        connection.close()


_EXPECTED_V9_CATALOG = _expected_v9_catalog()


def _expected_v7_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _V7_TABLES:
            connection.execute(ddl)
        return _catalog_signature_for(connection, _V7_OWNED)
    finally:
        connection.close()


_EXPECTED_V7_CATALOG = _expected_v7_catalog()


def _expected_v8_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        for ddl in _V8_TABLES:
            connection.execute(ddl)
        return _catalog_signature_for(connection, _V8_OWNED)
    finally:
        connection.close()


_EXPECTED_V8_CATALOG = _expected_v8_catalog()


__all__ = [
    "AnsweredProjection",
    "AnswerIngestApprovalPolicy",
    "AnswerIngestAuthority",
    "ApprovalEvaluation",
    "ApprovalDispositionApplication",
    "ApprovalDispositionAuthorizationProof",
    "ApprovalDispositionCommand",
    "CardBinding",
    "CardBindingResolver",
    "CentralLifecycleCreateResult",
    "CentralWorkTicket",
    "CentralWorkTicketEnqueueResult",
    "CentralQuestionLifecycleApplication",
    "CentralQuestionLifecycleConflict",
    "CentralQuestionLifecycleStore",
    "CentralQuestionLifecycleUnavailable",
    "OwnerAnswerCandidate",
    "OwnerAnswerIngest",
    "OwnerAnswerIngestApplication",
    "OwnerAnswerIngestResult",
    "RootManagerResolver",
    "SqliteCentralRegistryRootManagerResolver",
    "SqliteProductionCardBindingResolver",
    "SqliteApprovalDispositionAuthority",
    "FileReloadingApprovalDispositionAuthority",
    "FeedbackAuthorizationProof",
    "FeedbackCommand",
    "FileReloadingQuestionCreateAuthority",
    "FileReloadingQuestionFeedbackAuthority",
    "QuestionCreateApplication",
    "QuestionCreateAuthority",
    "QuestionCreateAuthorizationProof",
    "QuestionCreateCommand",
    "QuestionCreateForbidden",
    "QuestionFeedbackApplication",
    "QuestionFeedbackAuthority",
    "QuestionFeedbackConflict",
    "QuestionFeedbackInvalid",
    "QuestionFeedbackNotFound",
    "QuestionFeedbackResult",
    "SqliteQuestionFeedbackAuthority",
    "SqliteQuestionCreateAuthority",
    "OwnerDeliveryPort",
    "canonical_lifecycle_json",
    "central_question_lifecycle_schema_ready",
    "compare_and_set_lifecycle_request",
    "migrate_central_question_lifecycle_schema",
    "read_legacy_conflict_candidates",
    "read_lifecycle_request",
    "validate_central_question_lifecycle_connection",
]
