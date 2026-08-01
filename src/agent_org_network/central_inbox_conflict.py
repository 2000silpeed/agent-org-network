"""Durable Central ConflictCase metadata/control half (RB3.2b.5-B, ADR 0080).

The v15 catalog is a companion to the v14 lifecycle catalog.  It preserves the
request-unique v14 ConflictCase row as immutable lineage and adds the mutable
sealed aggregate, immutable concurrence evidence, receipts/audits, metadata-only
evidence grants, and deadlock ManagerItem reverse link.
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
    AuthorityPolicySnapshot,
    AuthorizationGrant,
    CentralAuthorizer,
    ManagerActItemSnapshot,
    ResourceRef,
    SnapshotCentralAuthorizer,
    load_authority_policy_yaml,
)
from agent_org_network.central_operational_evidence import (
    ConflictChange,
    ManagerItemChange,
    QuestionChange,
    RequestState,
    SafeResourceRef,
    SourceReceiptProvenance,
    append_committed_source_evidence_if_v19,
    canonical_v19_file_authority,
    source_receipt_digest,
)
from agent_org_network.central_question_lifecycle import (
    CardBinding,
    canonical_lifecycle_json,
    central_question_lifecycle_schema_ready,
    compare_and_set_lifecycle_request,
    read_legacy_conflict_candidates,
    read_lifecycle_request,
)
from agent_org_network.question_request import (
    AwaitingConflict,
    AwaitingManager,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)


ConflictCaseState = Literal["open", "resolved", "escalated"]
ConflictOutcome = Literal["still_open", "agreed", "deadlocked", "route_rejected"]
ConflictTerminalOutcome = Literal["agreed", "deadlocked", "route_rejected"]
ConcurrenceStance = Literal["keep_as_complement", "withdraw"]

_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT_NAME = "central-inbox-conflict"
_COMPONENT_VERSION = 15

_TABLES = (
    """CREATE TABLE central_inbox_conflict_cases (
       case_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL UNIQUE, org_id TEXT NOT NULL,
       intent TEXT NOT NULL, candidate_snapshot_json TEXT NOT NULL,
       candidate_snapshot_digest TEXT NOT NULL CHECK(length(candidate_snapshot_digest)=64),
       authority_binding_digest TEXT NOT NULL CHECK(length(authority_binding_digest)=64),
       opened_request_revision INTEGER NOT NULL CHECK(opened_request_revision>0),
       state TEXT NOT NULL CHECK(state IN ('open','resolved','escalated')),
       round INTEGER NOT NULL CHECK(round>0), revision INTEGER NOT NULL CHECK(revision>0),
       resolved_outcome TEXT CHECK(resolved_outcome IN ('agreed','deadlocked','route_rejected')),
       resolution_receipt_id TEXT UNIQUE, escalated_receipt_id TEXT UNIQUE,
       opened_at TEXT NOT NULL, updated_at TEXT NOT NULL,
       FOREIGN KEY(case_id) REFERENCES central_question_conflict_cases(case_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(resolution_receipt_id) REFERENCES central_inbox_conflict_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       CHECK((state='open' AND resolved_outcome IS NULL AND resolution_receipt_id IS NULL AND escalated_receipt_id IS NULL)
          OR (state='resolved' AND resolved_outcome IS NOT NULL AND resolution_receipt_id IS NOT NULL AND escalated_receipt_id IS NULL)
          OR (state='escalated' AND resolved_outcome IS NULL AND resolution_receipt_id IS NULL AND escalated_receipt_id IS NOT NULL))
    )""",
    """CREATE TABLE central_inbox_conflict_concurrences (
       case_id TEXT NOT NULL, round INTEGER NOT NULL CHECK(round>0), actor_id TEXT NOT NULL,
       on_candidate_card_id TEXT NOT NULL,
       stance TEXT NOT NULL CHECK(stance IN ('keep_as_complement','withdraw')),
       rationale TEXT NOT NULL, idempotency_key TEXT NOT NULL,
       command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
       case_revision_after INTEGER NOT NULL CHECK(case_revision_after>1), created_at TEXT NOT NULL,
       PRIMARY KEY(case_id,round,actor_id),
       UNIQUE(case_id,actor_id,idempotency_key),
       UNIQUE(case_id,case_revision_after),
       FOREIGN KEY(case_id) REFERENCES central_inbox_conflict_cases(case_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_inbox_conflict_receipts (
       receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, case_id TEXT NOT NULL,
       request_id TEXT NOT NULL, actor_id TEXT NOT NULL, identity_session_id TEXT NOT NULL,
       round INTEGER NOT NULL CHECK(round>0), idempotency_key TEXT NOT NULL,
       command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
       expected_case_revision INTEGER NOT NULL CHECK(expected_case_revision>0),
       expected_request_revision INTEGER NOT NULL CHECK(expected_request_revision>=0),
       expected_round INTEGER NOT NULL CHECK(expected_round>0),
       resulting_case_revision INTEGER NOT NULL CHECK(resulting_case_revision>1),
       resulting_request_revision INTEGER NOT NULL CHECK(resulting_request_revision>=0),
       state TEXT NOT NULL CHECK(state IN ('open','resolved')),
       outcome TEXT NOT NULL CHECK(outcome IN ('still_open','agreed','deadlocked','route_rejected')),
       route_target_json TEXT, manager_item_id TEXT, created_at TEXT NOT NULL,
       authority_policy_revision_id TEXT NOT NULL, authority_policy_epoch INTEGER NOT NULL CHECK(authority_policy_epoch>0),
       authority_policy_digest TEXT NOT NULL CHECK(length(authority_policy_digest)=64),
       UNIQUE(case_id,actor_id,idempotency_key),
       UNIQUE(case_id,resulting_case_revision),
       FOREIGN KEY(case_id) REFERENCES central_inbox_conflict_cases(case_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_inbox_conflict_audits (
       receipt_id TEXT PRIMARY KEY NOT NULL, case_id TEXT NOT NULL,
       request_id TEXT NOT NULL, actor_id TEXT NOT NULL, round INTEGER NOT NULL,
       command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
       resulting_case_revision INTEGER NOT NULL, resulting_request_revision INTEGER NOT NULL,
       outcome TEXT NOT NULL CHECK(outcome IN ('still_open','agreed','deadlocked','route_rejected')),
       created_at TEXT NOT NULL,
       FOREIGN KEY(receipt_id) REFERENCES central_inbox_conflict_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(case_id) REFERENCES central_inbox_conflict_cases(case_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_inbox_conflict_evidence_grants (
       grant_id TEXT PRIMARY KEY NOT NULL, case_id TEXT NOT NULL,
       candidate_card_id TEXT NOT NULL, candidate_card_revision INTEGER NOT NULL CHECK(candidate_card_revision>0),
       concept_ref TEXT NOT NULL, expires_at TEXT NOT NULL,
       single_use INTEGER NOT NULL CHECK(single_use=1),
       status TEXT NOT NULL CHECK(status IN ('available','consumed','expired')),
       UNIQUE(case_id,candidate_card_id),
       FOREIGN KEY(case_id) REFERENCES central_inbox_conflict_cases(case_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_inbox_conflict_deadlock_manager_links (
       case_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL UNIQUE,
       manager_item_id TEXT NOT NULL UNIQUE, manager_id TEXT NOT NULL,
       resolution_receipt_id TEXT NOT NULL UNIQUE,
       candidate_snapshot_digest TEXT NOT NULL CHECK(length(candidate_snapshot_digest)=64),
       created_at TEXT NOT NULL,
       FOREIGN KEY(case_id) REFERENCES central_inbox_conflict_cases(case_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(request_id) REFERENCES question_requests(request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(manager_item_id) REFERENCES central_question_manager_items(item_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
       FOREIGN KEY(resolution_receipt_id) REFERENCES central_inbox_conflict_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    """CREATE TABLE central_inbox_component_schema (
       name TEXT PRIMARY KEY NOT NULL CHECK(name='central-inbox-conflict'),
       version INTEGER NOT NULL CHECK(version=15)
    )""",
)

_OWNED = (
    "central_inbox_component_schema",
    "central_inbox_conflict_audits",
    "central_inbox_conflict_cases",
    "central_inbox_conflict_concurrences",
    "central_inbox_conflict_deadlock_manager_links",
    "central_inbox_conflict_evidence_grants",
    "central_inbox_conflict_receipts",
)

_TRIGGERS = (
    "CREATE TRIGGER central_inbox_conflict_cases_immutable_identity BEFORE UPDATE ON central_inbox_conflict_cases "
    "WHEN OLD.case_id!=NEW.case_id OR OLD.request_id!=NEW.request_id OR OLD.org_id!=NEW.org_id "
    "OR OLD.intent!=NEW.intent OR OLD.candidate_snapshot_json!=NEW.candidate_snapshot_json "
    "OR OLD.candidate_snapshot_digest!=NEW.candidate_snapshot_digest "
    "OR OLD.authority_binding_digest!=NEW.authority_binding_digest "
    "OR OLD.opened_request_revision!=NEW.opened_request_revision OR OLD.round!=NEW.round "
    "OR OLD.opened_at!=NEW.opened_at BEGIN SELECT RAISE(ABORT,'immutable conflict identity'); END",
    "CREATE TRIGGER central_inbox_conflict_cases_no_delete BEFORE DELETE ON central_inbox_conflict_cases "
    "BEGIN SELECT RAISE(ABORT,'immutable conflict case'); END",
    "CREATE TRIGGER central_inbox_conflict_cases_sealed_transition BEFORE UPDATE ON central_inbox_conflict_cases "
    "WHEN OLD.state!='open' OR NEW.revision!=OLD.revision+1 "
    "BEGIN SELECT RAISE(ABORT,'sealed conflict transition'); END",
    "CREATE TRIGGER central_inbox_conflict_concurrences_no_update BEFORE UPDATE ON central_inbox_conflict_concurrences "
    "BEGIN SELECT RAISE(ABORT,'immutable concurrence'); END",
    "CREATE TRIGGER central_inbox_conflict_concurrences_no_delete BEFORE DELETE ON central_inbox_conflict_concurrences "
    "BEGIN SELECT RAISE(ABORT,'immutable concurrence'); END",
    "CREATE TRIGGER central_inbox_conflict_receipts_no_update BEFORE UPDATE ON central_inbox_conflict_receipts "
    "BEGIN SELECT RAISE(ABORT,'immutable conflict receipt'); END",
    "CREATE TRIGGER central_inbox_conflict_receipts_no_delete BEFORE DELETE ON central_inbox_conflict_receipts "
    "BEGIN SELECT RAISE(ABORT,'immutable conflict receipt'); END",
    "CREATE TRIGGER central_inbox_conflict_audits_no_update BEFORE UPDATE ON central_inbox_conflict_audits "
    "BEGIN SELECT RAISE(ABORT,'immutable conflict audit'); END",
    "CREATE TRIGGER central_inbox_conflict_audits_no_delete BEFORE DELETE ON central_inbox_conflict_audits "
    "BEGIN SELECT RAISE(ABORT,'immutable conflict audit'); END",
    "CREATE TRIGGER central_inbox_conflict_evidence_grants_no_update BEFORE UPDATE ON central_inbox_conflict_evidence_grants "
    "BEGIN SELECT RAISE(ABORT,'immutable conflict evidence grant'); END",
    "CREATE TRIGGER central_inbox_conflict_evidence_grants_no_delete BEFORE DELETE ON central_inbox_conflict_evidence_grants "
    "BEGIN SELECT RAISE(ABORT,'immutable conflict evidence grant'); END",
    "CREATE TRIGGER central_inbox_conflict_deadlock_links_no_update BEFORE UPDATE ON central_inbox_conflict_deadlock_manager_links "
    "BEGIN SELECT RAISE(ABORT,'immutable deadlock manager link'); END",
    "CREATE TRIGGER central_inbox_conflict_deadlock_links_no_delete BEFORE DELETE ON central_inbox_conflict_deadlock_manager_links "
    "BEGIN SELECT RAISE(ABORT,'immutable deadlock manager link'); END",
)


class ConflictUnavailable(RuntimeError):
    """Current session, policy, binding, or durable evidence is unavailable."""


class ConflictSessionUnauthenticated(ConflictUnavailable):
    """Browser Session is absent, ended, expired, or no longer current."""


class ConflictNotFound(RuntimeError):
    """Case is missing, foreign, or hidden from the current participant."""


class ConflictStaleOrConflict(RuntimeError):
    """Expected revisions or idempotent command identity no longer match."""


@dataclass(frozen=True, slots=True)
class ConflictCandidateSnapshot:
    card_id: str
    card_revision: int
    card_digest: str
    owner_user_id: str
    concept_ref: str
    coverage_digest: str


@dataclass(frozen=True, slots=True)
class ConflictEvidenceGrant:
    grant_id: str
    candidate_card_id: str
    candidate_card_revision: int
    concept_ref: str
    expires_at: datetime
    single_use: bool
    status: Literal["available", "consumed", "expired"]


@dataclass(frozen=True, slots=True)
class ConflictConcurrence:
    on_candidate_card_id: str
    stance: ConcurrenceStance
    rationale: str
    round: int


@dataclass(frozen=True, slots=True)
class ConflictCaseSummary:
    case_id: str
    request_id: str
    request_revision: int
    state: ConflictCaseState
    round: int
    revision: int
    candidate_card_ids: tuple[str, ...]
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class ConflictCaseDetail:
    case_id: str
    request_id: str
    request_revision: int
    state: ConflictCaseState
    round: int
    revision: int
    candidate_card_ids: tuple[str, ...]
    opened_at: datetime
    expected_case_revision: int
    expected_request_revision: int
    expected_round: int
    question: str
    candidates: tuple[ConflictCandidateSnapshot, ...]
    own_concurrence: ConflictConcurrence | None
    evidence_grants: tuple[ConflictEvidenceGrant, ...]


@dataclass(frozen=True, slots=True)
class ConflictReadCommand:
    identity_session_id: str
    expected_org_id: str
    expected_actor_id: str


@dataclass(frozen=True, slots=True)
class ConflictConcurrenceCommand:
    case_id: str
    identity_session_id: str
    expected_org_id: str
    expected_actor_id: str
    on_candidate_card_id: str
    stance: ConcurrenceStance
    rationale: str
    expected_case_revision: int
    expected_request_revision: int
    expected_round: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ConflictAuthorizationProof:
    principal: AuthenticatedPrincipal
    session_grant: AuthorizationGrant
    action_grant: AuthorizationGrant


@dataclass(frozen=True, slots=True)
class ConflictRouteAuthorization:
    kind: Literal["allowed", "rejected"]
    route: RouteTarget | None


@dataclass(frozen=True, slots=True)
class ConflictConcurrenceResult:
    receipt_id: str
    concurrence_command_digest: str
    case_id: str
    case_revision: int
    request_id: str
    request_revision: int
    state: Literal["open", "resolved"]
    outcome: ConflictOutcome
    replayed: bool


class ConflictAuthority(Protocol):
    def authorize_read(
        self, command: ConflictReadCommand, resource: ResourceRef, transaction: sqlite3.Connection
    ) -> ConflictAuthorizationProof: ...

    def issue_concurrence_proof(
        self,
        command: ConflictConcurrenceCommand,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ConflictAuthorizationProof: ...

    def verify_concurrence_proof(
        self,
        proof: ConflictAuthorizationProof,
        command: ConflictConcurrenceCommand,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> bool: ...

    def current_candidate_bindings(
        self,
        org_id: str,
        candidates: tuple[ConflictCandidateSnapshot, ...],
        transaction: sqlite3.Connection,
    ) -> bool: ...

    def authorize_route_target(
        self,
        org_id: str,
        intent: str,
        primary_card_id: str,
        complement_card_ids: tuple[str, ...],
        transaction: sqlite3.Connection,
    ) -> ConflictRouteAuthorization: ...

    def resolve_authorized_deadlock_manager(
        self,
        org_id: str,
        owner_user_ids: tuple[str, ...],
        item_id: str,
        transaction: sqlite3.Connection,
    ) -> str: ...

    def verify_deadlock_manager(
        self,
        org_id: str,
        manager_id: str,
        item_id: str,
        transaction: sqlite3.Connection,
    ) -> bool: ...


class FileReloadingConflictAuthority:
    """Production current-session/Card/Registry/Authority adapter.

    Every call reloads the central policy.  Browser Session, Registry User,
    production Card and Manager graph reads all use the caller's transaction.
    """

    def __init__(
        self,
        *,
        authority_policy_path: Path,
        configured_org_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        if not authority_policy_path.is_absolute() or not configured_org_id:
            raise ValueError("conflict authority configuration required")
        self._path = authority_policy_path
        self._org_id = configured_org_id
        self._clock = clock

    def authorize_read(
        self,
        command: ConflictReadCommand,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ConflictAuthorizationProof:
        principal = self._current_principal(command, transaction)
        authorizer = self._authorizer()
        return self._proof(authorizer, principal, "conflict.list", resource)

    def issue_concurrence_proof(
        self,
        command: ConflictConcurrenceCommand,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ConflictAuthorizationProof:
        principal = self._current_principal(command, transaction)
        authorizer = self._authorizer()
        return self._proof(authorizer, principal, "conflict.concur", resource)

    def verify_concurrence_proof(
        self,
        proof: ConflictAuthorizationProof,
        command: ConflictConcurrenceCommand,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> bool:
        try:
            current = self.issue_concurrence_proof(command, resource, transaction)
            return (
                current.principal == proof.principal
                and current.session_grant.model_dump(mode="json")
                == proof.session_grant.model_dump(mode="json")
                and current.action_grant.model_dump(mode="json")
                == proof.action_grant.model_dump(mode="json")
            )
        except Exception:
            return False

    def current_candidate_bindings(
        self,
        org_id: str,
        candidates: tuple[ConflictCandidateSnapshot, ...],
        transaction: sqlite3.Connection,
    ) -> bool:
        if org_id != self._org_id:
            return False
        from agent_org_network.sqlite_production_agent_cards import (
            validate_production_agent_card_rows,
        )

        try:
            validate_production_agent_card_rows(transaction, org_id)
        except Exception as error:
            raise ConflictUnavailable() from error
        for candidate in candidates:
            row = transaction.execute(
                "SELECT owner_id,card_digest,revision FROM production_agent_cards "
                "WHERE org_id=? AND agent_id=?",
                (org_id, candidate.card_id),
            ).fetchone()
            if (
                row is None
                or row["owner_id"] != candidate.owner_user_id
                or row["card_digest"] != candidate.card_digest
                or int(row["revision"]) != candidate.card_revision
            ):
                return False
        return True

    def authorize_route_target(
        self,
        org_id: str,
        intent: str,
        primary_card_id: str,
        complement_card_ids: tuple[str, ...],
        transaction: sqlite3.Connection,
    ) -> ConflictRouteAuthorization:
        if org_id != self._org_id:
            raise ConflictUnavailable()
        from agent_org_network.agent_card import AgentCard
        from agent_org_network.sqlite_production_agent_cards import (
            validate_production_agent_card_rows,
        )

        try:
            snapshot = self._snapshot()
            validate_production_agent_card_rows(transaction, org_id)
            ids = (primary_card_id,) + complement_card_ids
            if len(set(ids)) != len(ids):
                raise ConflictUnavailable()
            cards: dict[str, AgentCard] = {}
            for card_id in ids:
                row = transaction.execute(
                    "SELECT card_json FROM production_agent_cards WHERE org_id=? AND agent_id=?",
                    (org_id, card_id),
                ).fetchone()
                if row is None:
                    raise ConflictUnavailable()
                cards[card_id] = AgentCard.model_validate_json(str(row["card_json"]), strict=True)
            allowed = {
                rule.agent_card_id
                for rule in snapshot.route_rules
                if rule.intent == intent
            }
            if any(card_id not in allowed for card_id in ids):
                return ConflictRouteAuthorization(kind="rejected", route=None)
            primary = cards[primary_card_id]
            return ConflictRouteAuthorization(
                kind="allowed",
                route=RouteTarget(
                    intent=intent,
                    agent_id=primary_card_id,
                    requires_approval=intent in primary.approval_when,
                    authority_version=snapshot.policy_version,
                ),
            )
        except ConflictUnavailable:
            raise
        except Exception as error:
            raise ConflictUnavailable() from error

    def resolve_authorized_deadlock_manager(
        self,
        org_id: str,
        owner_user_ids: tuple[str, ...],
        item_id: str,
        transaction: sqlite3.Connection,
    ) -> str:
        if org_id != self._org_id or not owner_user_ids or not item_id:
            raise ConflictUnavailable()
        from agent_org_network.sqlite_production_registry_users import (
            validate_production_registry_user_connection,
        )

        try:
            validate_production_registry_user_connection(transaction)
            rows = tuple(
                transaction.execute(
                    "SELECT user_id,manager_id FROM production_registry_users "
                    "WHERE org_id=? ORDER BY user_id",
                    (org_id,),
                )
            )
            managers = {
                binding.subject_id
                for binding in self._snapshot().subject_roles
                if "manager" in binding.roles
            }
            parent = {
                str(row["user_id"]): (
                    None if row["manager_id"] is None else str(row["manager_id"])
                )
                for row in rows
            }
            if any(owner not in parent for owner in owner_user_ids):
                raise ConflictUnavailable()
            paths = tuple(_manager_path(owner, parent) for owner in owner_user_ids)
            common = set(paths[0])
            for path in paths[1:]:
                common.intersection_update(path)
            candidates = common & managers
            if candidates:
                scores = {
                    candidate: (
                        max(path.index(candidate) + 1 for path in paths),
                        sum(path.index(candidate) + 1 for path in paths),
                    )
                    for candidate in candidates
                }
                best = min(scores.values())
                selected = sorted(
                    candidate for candidate, score in scores.items() if score == best
                )
                if len(selected) != 1:
                    raise ConflictUnavailable()
                return selected[0]
            roots = sorted(user_id for user_id, manager_id in parent.items() if manager_id is None)
            if len(roots) != 1 or any(roots[0] not in path for path in paths):
                raise ConflictUnavailable()
            return roots[0]
        except ConflictUnavailable:
            raise
        except Exception as error:
            raise ConflictUnavailable() from error

    def verify_deadlock_manager(
        self,
        org_id: str,
        manager_id: str,
        item_id: str,
        transaction: sqlite3.Connection,
    ) -> bool:
        if org_id != self._org_id:
            return False
        try:
            resolver = _TransactionManagerActResolver(transaction, org_id, manager_id)
            authorizer = SnapshotCentralAuthorizer(
                self._snapshot(), manager_act_item_resolver=resolver
            )
            principal = AuthenticatedPrincipal(
                org_id=org_id,
                subject_id=manager_id,
                identity_provider="conflict-deadlock-manager",
                identity_session_id=sha256(
                    f"{org_id}:{manager_id}:{item_id}".encode()
                ).hexdigest(),
            )
            resource = ResourceRef(
                org_id=org_id,
                kind="manager_item",
                resource_id=item_id,
                owner_subject_id=manager_id,
            )
            grant = authorizer.authorize(principal, "manager.act", resource)
            return type(grant) is AuthorizationGrant and authorizer.verify(
                grant, principal, "manager.act", resource
            )
        except Exception:
            return False

    def _proof(
        self,
        authorizer: CentralAuthorizer,
        principal: AuthenticatedPrincipal,
        action: Literal["conflict.list", "conflict.concur"],
        resource: ResourceRef,
    ) -> ConflictAuthorizationProof:
        session = ResourceRef(
            org_id=principal.org_id,
            kind="browser_session",
            resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        session_grant = authorizer.authorize(principal, "session.read", session)
        action_grant = authorizer.authorize(principal, action, resource)
        if (
            type(session_grant) is not AuthorizationGrant
            or not authorizer.verify(session_grant, principal, "session.read", session)
            or type(action_grant) is not AuthorizationGrant
            or not authorizer.verify(action_grant, principal, action, resource)
        ):
            raise ConflictNotFound()
        return ConflictAuthorizationProof(principal, session_grant, action_grant)

    def _current_principal(
        self,
        command: ConflictReadCommand | ConflictConcurrenceCommand,
        transaction: sqlite3.Connection,
    ) -> AuthenticatedPrincipal:
        if command.expected_org_id != self._org_id:
            raise ConflictNotFound()
        from agent_org_network.central_browser_auth_sqlite import (
            read_current_browser_session_connection,
        )

        try:
            session = read_current_browser_session_connection(
                transaction, command.identity_session_id, now=self._clock()
            )
        except Exception as error:
            raise ConflictUnavailable() from error
        if session is None:
            raise ConflictSessionUnauthenticated()
        if (
            session.org_id != command.expected_org_id
            or session.registry_user_id != command.expected_actor_id
        ):
            raise ConflictNotFound()
        return AuthenticatedPrincipal(
            org_id=session.org_id,
            subject_id=session.registry_user_id,
            identity_provider="browser-session",
            identity_session_id=session.session_digest,
        )

    def _snapshot(self) -> AuthorityPolicySnapshot:
        return load_authority_policy_yaml(
            self._path.read_text(encoding="utf-8"), expected_org_id=self._org_id
        )

    def _authorizer(self) -> SnapshotCentralAuthorizer:
        try:
            return SnapshotCentralAuthorizer(self._snapshot())
        except ConflictUnavailable:
            raise
        except Exception as error:
            raise ConflictUnavailable() from error


class _TransactionManagerActResolver:
    def __init__(
        self, transaction: sqlite3.Connection, org_id: str, manager_id: str
    ) -> None:
        self._transaction = transaction
        self._org_id = org_id
        self._manager_id = manager_id

    def resolve_manager_act_item(
        self, *, item_id: str
    ) -> ManagerActItemSnapshot | None:
        row = self._transaction.execute(
            "SELECT m.org_id,m.item_id,m.manager_id,q.state_kind "
            "FROM central_question_manager_items m "
            "JOIN question_requests q ON q.request_id=m.request_id "
            "WHERE m.item_id=?",
            (item_id,),
        ).fetchone()
        if (
            row is None
            or row["org_id"] != self._org_id
            or row["manager_id"] != self._manager_id
            or row["state_kind"] != "awaiting_manager"
        ):
            return None
        return ManagerActItemSnapshot(
            org_id=self._org_id,
            item_id=item_id,
            manager_subject_ref="subject:"
            + sha256(self._manager_id.encode()).hexdigest(),
            state_kind="open",
            request_state_kind="awaiting_manager",
        )


def _manager_path(
    owner_id: str, parent: dict[str, str | None]
) -> tuple[str, ...]:
    path: list[str] = []
    seen = {owner_id}
    current = parent[owner_id]
    while current is not None:
        if current in seen or current not in parent:
            raise ConflictUnavailable()
        path.append(current)
        seen.add(current)
        current = parent[current]
    if not path:
        path.append(owner_id)
    return tuple(path)


def migrate_central_inbox_conflict_schema(
    path: Path, *, fault_injector: Callable[[str], None] = lambda _point: None
) -> None:
    """Forward-only v14→v15 companion migration, marker written last."""
    if not central_question_lifecycle_schema_ready(path):
        raise ConflictUnavailable()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        existing = _existing_owned(connection)
        if existing:
            if existing != set(_OWNED):
                raise ConflictUnavailable()
            _validate_catalog(connection)
            return
        legacy_rows = tuple(connection.execute("SELECT * FROM central_question_conflict_cases"))
        connection.execute("BEGIN IMMEDIATE")
        try:
            for ddl in _TABLES[:-1]:
                connection.execute(ddl)
            for ddl in _TRIGGERS:
                connection.execute(ddl)
            fault_injector("v14-to-v15-after-schema")
            for legacy in legacy_rows:
                request = read_lifecycle_request(connection, str(legacy["request_id"]))
                if request is None or not isinstance(request.state, AwaitingConflict):
                    raise ConflictUnavailable()
                candidates = _legacy_candidate_snapshots(connection, legacy)
                _insert_case_and_grants(
                    connection,
                    case_id=str(legacy["case_id"]),
                    request=request,
                    intent=str(legacy["intent"]),
                    candidates=candidates,
                    opened_at=_timestamp(str(legacy["created_at"])),
                )
            fault_injector("v14-to-v15-after-copy")
            connection.execute(_TABLES[-1])
            fault_injector("v14-to-v15-before-marker")
            connection.execute(
                "INSERT INTO central_inbox_component_schema(name,version) VALUES (?,?)",
                (_COMPONENT_NAME, _COMPONENT_VERSION),
            )
            _validate_catalog(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except ConflictUnavailable:
        raise
    except Exception as error:
        raise ConflictUnavailable() from error
    finally:
        connection.close()


def central_inbox_conflict_schema_ready(path: Path) -> bool:
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


def insert_initial_conflict_companion(
    transaction: sqlite3.Connection,
    *,
    request: QuestionRequest,
    case_id: str,
    intent: str,
    bindings: tuple[CardBinding, ...],
) -> None:
    """Append a v15 companion in the existing initial-conflict transaction."""
    marker = transaction.execute(
        "SELECT version FROM central_inbox_component_schema WHERE name=?",
        (_COMPONENT_NAME,),
    ).fetchone() if _table_exists(transaction, "central_inbox_component_schema") else None
    if marker is None:
        return
    if int(marker[0]) != _COMPONENT_VERSION or not isinstance(request.state, AwaitingConflict):
        raise ConflictUnavailable()
    candidates = tuple(_snapshot_from_binding(intent, binding) for binding in bindings)
    _insert_case_and_grants(
        transaction,
        case_id=case_id,
        request=request,
        intent=intent,
        candidates=candidates,
        opened_at=request.updated_at,
    )


class ConflictInboxApplication:
    def __init__(self, *, database_path: Path, authority: ConflictAuthority) -> None:
        self._path = database_path
        self._authority = authority

    def list(self, command: ConflictReadCommand) -> tuple[ConflictCaseSummary, ...]:
        _validate_read(command)
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            _validate_catalog(connection)
            scope = ResourceRef(
                org_id=command.expected_org_id,
                kind="conflict_inbox",
                resource_id=command.expected_actor_id,
                owner_subject_id=command.expected_actor_id,
            )
            proof = self._authority.authorize_read(command, scope, connection)
            _require_proof(proof, command, "conflict.list", scope)
            rows = tuple(
                connection.execute(
                    "SELECT c.*,q.revision AS request_revision FROM central_inbox_conflict_cases c "
                    "JOIN question_requests q ON q.request_id=c.request_id "
                    "WHERE c.org_id=? AND c.state='open' ORDER BY c.opened_at,c.case_id",
                    (proof.principal.org_id,),
                )
            )
            result = tuple(
                _summary(row)
                for row in rows
                if (
                    proof.principal.subject_id
                    in _candidate_owners(
                        candidates := _snapshots(str(row["candidate_snapshot_json"]))
                    )
                    and self._authority.current_candidate_bindings(
                        proof.principal.org_id, candidates, connection
                    )
                )
            )
            connection.commit()
            return result
        except (ConflictUnavailable, ConflictNotFound, ConflictStaleOrConflict):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ConflictUnavailable() from error
        finally:
            connection.close()

    def detail(self, command: ConflictReadCommand, case_id: str) -> ConflictCaseDetail | None:
        _validate_read(command)
        if not _valid_reference(case_id):
            return None
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            _validate_catalog(connection)
            row = _case_row(connection, case_id)
            if row is None or row["org_id"] != command.expected_org_id:
                connection.commit()
                return None
            resource = ResourceRef(
                org_id=command.expected_org_id,
                kind="conflict_case",
                resource_id=case_id,
                owner_subject_id=command.expected_actor_id,
            )
            proof = self._authority.authorize_read(command, resource, connection)
            _require_proof(proof, command, "conflict.list", resource)
            candidates = _snapshots(str(row["candidate_snapshot_json"]))
            if proof.principal.subject_id not in _candidate_owners(candidates):
                connection.commit()
                return None
            if not self._authority.current_candidate_bindings(
                proof.principal.org_id, candidates, connection
            ):
                connection.commit()
                return None
            detail = _detail(connection, row, proof.principal.subject_id, candidates)
            connection.commit()
            return detail
        except (ConflictUnavailable, ConflictNotFound, ConflictStaleOrConflict):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ConflictUnavailable() from error
        finally:
            connection.close()

    def _connection(self) -> sqlite3.Connection:
        if not central_inbox_conflict_schema_ready(self._path):
            raise ConflictUnavailable()
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class ConflictConcurrenceApplication:
    def __init__(
        self,
        *,
        database_path: Path,
        authority: ConflictAuthority,
        receipt_id_factory: Callable[[], str] = lambda: uuid4().hex,
        manager_item_id_factory: Callable[[], str] = lambda: uuid4().hex,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        fault_injector: Callable[[str], None] = lambda _point: None,
    ) -> None:
        self._path = database_path
        self._authority = authority
        self._receipt_id_factory = receipt_id_factory
        self._manager_item_id_factory = manager_item_id_factory
        self._clock = clock
        self._fault = fault_injector

    def concur(self, command: ConflictConcurrenceCommand) -> ConflictConcurrenceResult:
        _validate_concurrence_command(command)
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_catalog(connection)
            row = _case_row(connection, command.case_id)
            if row is None or row["org_id"] != command.expected_org_id:
                raise ConflictNotFound()
            candidates = _snapshots(str(row["candidate_snapshot_json"]))
            if command.expected_actor_id not in _candidate_owners(candidates):
                raise ConflictNotFound()
            if command.on_candidate_card_id not in {candidate.card_id for candidate in candidates}:
                raise ConflictStaleOrConflict()
            resource = ResourceRef(
                org_id=command.expected_org_id,
                kind="conflict_case",
                resource_id=command.case_id,
                owner_subject_id=command.expected_actor_id,
            )
            proof = self._authority.issue_concurrence_proof(command, resource, connection)
            _require_proof(proof, command, "conflict.concur", resource)
            if not self._authority.current_candidate_bindings(
                command.expected_org_id, candidates, connection
            ):
                raise ConflictUnavailable()
            digest = _command_digest(command)
            receipt = connection.execute(
                "SELECT * FROM central_inbox_conflict_receipts "
                "WHERE case_id=? AND actor_id=? AND idempotency_key=?",
                (command.case_id, proof.principal.subject_id, command.idempotency_key),
            ).fetchone()
            if receipt is not None:
                if receipt["command_digest"] != digest:
                    raise ConflictStaleOrConflict()
                if not self._authority.verify_concurrence_proof(
                    proof, command, resource, connection
                ):
                    raise ConflictUnavailable()
                result = _result_from_receipt(connection, receipt, replayed=True)
                _append_replayed_conflict_evidence(
                    connection, receipt=receipt,
                    current_policy_digest=proof.action_grant.policy_digest,
                )
                connection.commit()
                return result
            request = read_lifecycle_request(connection, str(row["request_id"]))
            if (
                request is None
                or not isinstance(request.state, AwaitingConflict)
                or request.state.case_id != command.case_id
                or row["state"] != "open"
                or int(row["revision"]) != command.expected_case_revision
                or request.revision != command.expected_request_revision
                or int(row["round"]) != command.expected_round
            ):
                raise ConflictStaleOrConflict()
            existing_vote = connection.execute(
                "SELECT 1 FROM central_inbox_conflict_concurrences "
                "WHERE case_id=? AND round=? AND actor_id=?",
                (command.case_id, command.expected_round, proof.principal.subject_id),
            ).fetchone()
            if existing_vote is not None:
                raise ConflictStaleOrConflict()
            at = self._clock()
            if at.tzinfo is None or at.utcoffset() is None or at < request.updated_at:
                raise ConflictUnavailable()
            next_case_revision = int(row["revision"]) + 1
            connection.execute(
                "INSERT INTO central_inbox_conflict_concurrences"
                "(case_id,round,actor_id,on_candidate_card_id,stance,rationale,idempotency_key,"
                "command_digest,case_revision_after,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    command.case_id,
                    command.expected_round,
                    proof.principal.subject_id,
                    command.on_candidate_card_id,
                    command.stance,
                    command.rationale,
                    command.idempotency_key,
                    digest,
                    next_case_revision,
                    at.isoformat(),
                ),
            )
            self._fault("after-concurrence-before-reducer")
            votes = tuple(
                connection.execute(
                    "SELECT * FROM central_inbox_conflict_concurrences "
                    "WHERE case_id=? AND round=? ORDER BY actor_id",
                    (command.case_id, command.expected_round),
                )
            )
            outcome, primary_card_id, complements = _reduce(candidates, votes)
            route: RouteTarget | None = None
            route_authorization: ConflictRouteAuthorization | None = None
            manager_item_id: str | None = None
            manager_id: str | None = None
            updated_request = request
            if outcome != "still_open":
                if outcome == "agreed":
                    assert primary_card_id is not None
                    route_authorization = self._authority.authorize_route_target(
                        request.org_id,
                        cast(str, row["intent"]),
                        primary_card_id,
                        complements,
                        connection,
                    )
                    if route_authorization.kind == "rejected":
                        outcome = "route_rejected"
                    elif route_authorization.kind == "allowed" and route_authorization.route is not None:
                        route = route_authorization.route
                    else:
                        raise ConflictUnavailable()
                if outcome == "agreed":
                    assert route is not None
                    updated_request = request.transition(
                        ReadyToDispatch(
                            route=route,
                            attempt=1,
                            trigger_key=f"conflict:{command.case_id}:{command.expected_round}",
                            handling=HandlingAssignment(
                                kind="system",
                                ref=f"conflict:{command.case_id}:{command.expected_round}",
                                due_at=request.state.handling.due_at,
                            ),
                        ),
                        clock=lambda: at,
                    )
                    compare_and_set_lifecycle_request(connection, request, updated_request)
                elif outcome == "route_rejected":
                    updated_request = request.transition(
                        DeclinedRequest(reason_code="route_rejected"), clock=lambda: at
                    )
                    compare_and_set_lifecycle_request(connection, request, updated_request)
                elif outcome == "deadlocked":
                    manager_item_id = self._manager_item_id_factory()
                    if not _valid_reference(manager_item_id):
                        raise ConflictUnavailable()
                    owners = _candidate_owners(candidates)
                    manager_id = self._authority.resolve_authorized_deadlock_manager(
                        request.org_id, owners, manager_item_id, connection
                    )
                    if not _valid_reference(manager_id):
                        raise ConflictUnavailable()
                    connection.execute(
                        "INSERT INTO central_question_manager_items"
                        "(request_id,org_id,item_id,manager_id,intent,created_at) VALUES (?,?,?,?,?,?)",
                        (
                            request.request_id,
                            request.org_id,
                            manager_item_id,
                            manager_id,
                            request.intent,
                            at.isoformat(),
                        ),
                    )
                    updated_request = request.transition(
                        AwaitingManager(
                            item_id=manager_item_id,
                            public_kind="contested",
                            handling=HandlingAssignment(
                                kind="manager_item",
                                ref=manager_item_id,
                                due_at=request.state.handling.due_at,
                            ),
                        ),
                        clock=lambda: at,
                    )
                    compare_and_set_lifecycle_request(connection, request, updated_request)
                    if not self._authority.verify_deadlock_manager(
                        request.org_id, manager_id, manager_item_id, connection
                    ):
                        raise ConflictUnavailable()
                else:
                    raise ConflictUnavailable()
                self._fault("after-conflict-request-cas")
            receipt_id = self._receipt_id_factory()
            if not _valid_reference(receipt_id):
                raise ConflictUnavailable()
            state: Literal["open", "resolved"] = (
                "open" if outcome == "still_open" else "resolved"
            )
            route_json = (
                None
                if route is None
                else canonical_lifecycle_json(
                    {
                        "primary": route.model_dump(mode="json"),
                        "complement_card_ids": list(complements),
                    }
                )
            )
            audit_authority = canonical_v19_file_authority(
                source_policy_digest=proof.action_grant.policy_digest,
                current_snapshot_digest=proof.action_grant.policy_digest,
            )
            connection.execute(
                "INSERT INTO central_inbox_conflict_receipts"
                "(receipt_id,org_id,case_id,request_id,actor_id,identity_session_id,round,"
                "idempotency_key,command_digest,expected_case_revision,expected_request_revision,"
                "expected_round,resulting_case_revision,resulting_request_revision,state,outcome,"
                "route_target_json,manager_item_id,created_at,authority_policy_revision_id,"
                "authority_policy_epoch,authority_policy_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    request.org_id,
                    command.case_id,
                    request.request_id,
                    proof.principal.subject_id,
                    proof.principal.identity_session_id,
                    command.expected_round,
                    command.idempotency_key,
                    digest,
                    command.expected_case_revision,
                    command.expected_request_revision,
                    command.expected_round,
                    next_case_revision,
                    updated_request.revision,
                    state,
                    outcome,
                    route_json,
                    manager_item_id,
                    at.isoformat(),
                    audit_authority.policy_revision_id,
                    audit_authority.policy_epoch,
                    audit_authority.policy_digest,
                ),
            )
            if state == "open":
                result = connection.execute(
                    "UPDATE central_inbox_conflict_cases SET revision=?,updated_at=? "
                    "WHERE case_id=? AND state='open' AND revision=? AND round=?",
                    (
                        next_case_revision,
                        at.isoformat(),
                        command.case_id,
                        command.expected_case_revision,
                        command.expected_round,
                    ),
                )
            else:
                result = connection.execute(
                    "UPDATE central_inbox_conflict_cases SET state='resolved',revision=?,"
                    "resolved_outcome=?,resolution_receipt_id=?,updated_at=? "
                    "WHERE case_id=? AND state='open' AND revision=? AND round=?",
                    (
                        next_case_revision,
                        outcome,
                        receipt_id,
                        at.isoformat(),
                        command.case_id,
                        command.expected_case_revision,
                        command.expected_round,
                    ),
                )
            if result.rowcount != 1:
                raise ConflictStaleOrConflict()
            connection.execute(
                "INSERT INTO central_inbox_conflict_audits"
                "(receipt_id,case_id,request_id,actor_id,round,command_digest,"
                "resulting_case_revision,resulting_request_revision,outcome,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    command.case_id,
                    request.request_id,
                    proof.principal.subject_id,
                    command.expected_round,
                    digest,
                    next_case_revision,
                    updated_request.revision,
                    outcome,
                    at.isoformat(),
                ),
            )
            append_committed_source_evidence_if_v19(
                connection, org_id=request.org_id, receipt_id=receipt_id,
                command_digest=digest, event_type="conflict_concurrence_recorded",
                action="conflict.concur",
                resource=SafeResourceRef(kind="conflict_case", resource_id=command.case_id),
                change=ConflictChange(case_id=command.case_id, request_id=request.request_id),
                actor_user_id=proof.principal.subject_id, occurred_at=at.isoformat(),
                policy_revision_id=audit_authority.policy_revision_id,
                policy_epoch=audit_authority.policy_epoch,
                policy_digest=audit_authority.policy_digest,
                source=SourceReceiptProvenance(
                    kind="conflict_concurrence",
                    receipt_key=receipt_id,
                    receipt_digest=source_receipt_digest(
                        connection,
                        "conflict_concurrence",
                        request.org_id,
                        receipt_id,
                    ),
                ),
            )
            if outcome != "still_open":
                append_committed_source_evidence_if_v19(
                    connection, org_id=request.org_id,
                    receipt_id=f"conflict-state:{receipt_id}",
                    command_digest=digest, event_type="request_state_changed",
                    action="conflict.resolve",
                    resource=SafeResourceRef(
                        kind="question_request", resource_id=request.request_id
                    ),
                    change=QuestionChange(
                        request_id=request.request_id,
                        from_state=request.state.kind,
                        to_state=cast(RequestState, updated_request.state.kind),
                    ),
                    actor_user_id=proof.principal.subject_id,
                    occurred_at=at.isoformat(),
                    policy_revision_id=audit_authority.policy_revision_id,
                    policy_epoch=audit_authority.policy_epoch,
                    policy_digest=audit_authority.policy_digest,
                    source=SourceReceiptProvenance(
                        kind="conflict_state", receipt_key=receipt_id,
                        receipt_digest=source_receipt_digest(
                            connection, "conflict_state", request.org_id,
                            receipt_id,
                        ),
                    ),
                )
            if outcome == "deadlocked":
                assert manager_item_id is not None and manager_id is not None
                append_committed_source_evidence_if_v19(
                    connection, org_id=request.org_id,
                    receipt_id=f"manager-deadlock:{receipt_id}",
                    command_digest=digest, event_type="manager_item_changed",
                    action="manager_item.create",
                    resource=SafeResourceRef(
                        kind="manager_item", resource_id=manager_item_id
                    ),
                    change=ManagerItemChange(
                        manager_item_id=manager_item_id,
                        request_id=request.request_id,
                    ),
                    actor_user_id=proof.principal.subject_id,
                    occurred_at=at.isoformat(),
                    policy_revision_id=audit_authority.policy_revision_id,
                    policy_epoch=audit_authority.policy_epoch,
                    policy_digest=audit_authority.policy_digest,
                    source=SourceReceiptProvenance(
                        kind="manager_deadlock", receipt_key=receipt_id,
                        receipt_digest=source_receipt_digest(
                            connection, "manager_deadlock", request.org_id,
                            receipt_id,
                        ),
                    ),
                )
                connection.execute(
                    "INSERT INTO central_inbox_conflict_deadlock_manager_links"
                    "(case_id,request_id,manager_item_id,manager_id,resolution_receipt_id,"
                    "candidate_snapshot_digest,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        command.case_id,
                        request.request_id,
                        manager_item_id,
                        manager_id,
                        receipt_id,
                        row["candidate_snapshot_digest"],
                        at.isoformat(),
                    ),
                )
            self._fault("before-conflict-receipt-commit")
            if not self._authority.verify_concurrence_proof(
                proof, command, resource, connection
            ) or not self._authority.current_candidate_bindings(
                request.org_id, candidates, connection
            ):
                raise ConflictUnavailable()
            if outcome in {"agreed", "route_rejected"}:
                if primary_card_id is None or route_authorization is None:
                    raise ConflictUnavailable()
                current_route_authorization = self._authority.authorize_route_target(
                    request.org_id,
                    cast(str, row["intent"]),
                    primary_card_id,
                    complements,
                    connection,
                )
                if current_route_authorization != route_authorization:
                    raise ConflictUnavailable()
            elif outcome == "deadlocked":
                if manager_item_id is None or manager_id is None:
                    raise ConflictUnavailable()
                current_manager_id = (
                    self._authority.resolve_authorized_deadlock_manager(
                        request.org_id,
                        _candidate_owners(candidates),
                        manager_item_id,
                        connection,
                    )
                )
                if current_manager_id != manager_id or not self._authority.verify_deadlock_manager(
                    request.org_id, manager_id, manager_item_id, connection
                ):
                    raise ConflictUnavailable()
            _validate_catalog(connection)
            connection.commit()
            return ConflictConcurrenceResult(
                receipt_id=receipt_id,
                concurrence_command_digest=digest,
                case_id=command.case_id,
                case_revision=next_case_revision,
                request_id=request.request_id,
                request_revision=updated_request.revision,
                state=state,
                outcome=outcome,
                replayed=False,
            )
        except (ConflictUnavailable, ConflictNotFound, ConflictStaleOrConflict):
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise ConflictUnavailable() from error
        finally:
            connection.close()


def _validate_read(command: ConflictReadCommand) -> None:
    if (
        type(command) is not ConflictReadCommand
        or _SHA256.fullmatch(command.identity_session_id) is None
        or not _valid_reference(command.expected_org_id)
        or not _valid_reference(command.expected_actor_id)
    ):
        raise ConflictUnavailable()


def _validate_concurrence_command(command: ConflictConcurrenceCommand) -> None:
    if (
        type(command) is not ConflictConcurrenceCommand
        or not _valid_reference(command.case_id)
        or _SHA256.fullmatch(command.identity_session_id) is None
        or not _valid_reference(command.expected_org_id)
        or not _valid_reference(command.expected_actor_id)
        or not _valid_reference(command.on_candidate_card_id)
        or command.stance not in {"keep_as_complement", "withdraw"}
        or type(command.rationale) is not str
        or command.rationale == ""
        or command.expected_case_revision < 1
        or command.expected_request_revision < 0
        or command.expected_round < 1
        or not _valid_reference(command.idempotency_key)
    ):
        raise ConflictUnavailable()
    try:
        if len(command.rationale.encode("utf-8")) > 4096:
            raise ConflictUnavailable()
    except UnicodeError as error:
        raise ConflictUnavailable() from error


def _require_proof(
    proof: ConflictAuthorizationProof,
    command: ConflictReadCommand | ConflictConcurrenceCommand,
    action: Literal["conflict.list", "conflict.concur"],
    resource: ResourceRef,
) -> None:
    if type(proof) is not ConflictAuthorizationProof:
        raise ConflictUnavailable()
    principal = proof.principal
    session = ResourceRef(
        org_id=command.expected_org_id,
        kind="browser_session",
        resource_id=command.identity_session_id,
        owner_subject_id=command.expected_actor_id,
    )
    if (
        type(principal) is not AuthenticatedPrincipal
        or principal.org_id != command.expected_org_id
        or principal.subject_id != command.expected_actor_id
        or principal.identity_session_id != command.identity_session_id
        or proof.session_grant.org_id != principal.org_id
        or proof.session_grant.subject_id != principal.subject_id
        or proof.session_grant.action != "session.read"
        or proof.session_grant.resource != session
        or proof.action_grant.org_id != principal.org_id
        or proof.action_grant.subject_id != principal.subject_id
        or proof.action_grant.action != action
        or proof.action_grant.resource != resource
    ):
        raise ConflictUnavailable()


def _command_digest(command: ConflictConcurrenceCommand) -> str:
    return sha256(
        canonical_lifecycle_json(
            {
                "case_id": command.case_id,
                "actor_id": command.expected_actor_id,
                "idempotency_key": command.idempotency_key,
                "on_candidate_card_id": command.on_candidate_card_id,
                "stance": command.stance,
                "rationale": command.rationale,
                "expected_case_revision": command.expected_case_revision,
                "expected_request_revision": command.expected_request_revision,
                "expected_round": command.expected_round,
            }
        ).encode("utf-8")
    ).hexdigest()


def _stored_command_digest(vote: sqlite3.Row, receipt: sqlite3.Row) -> str:
    return sha256(
        canonical_lifecycle_json(
            {
                "case_id": receipt["case_id"],
                "actor_id": vote["actor_id"],
                "idempotency_key": vote["idempotency_key"],
                "on_candidate_card_id": vote["on_candidate_card_id"],
                "stance": vote["stance"],
                "rationale": vote["rationale"],
                "expected_case_revision": receipt["expected_case_revision"],
                "expected_request_revision": receipt["expected_request_revision"],
                "expected_round": receipt["expected_round"],
            }
        ).encode("utf-8")
    ).hexdigest()


def _route_binding(raw: str) -> tuple[RouteTarget, tuple[str, ...]]:
    try:
        value_object: object = json.loads(raw)
        if type(value_object) is not dict:
            raise ValueError
        value = cast(dict[object, object], value_object)
        if set(value) != {"primary", "complement_card_ids"}:
            raise ValueError
        primary_object = value["primary"]
        complement_object = value["complement_card_ids"]
        if type(primary_object) is not dict or type(complement_object) is not list:
            raise ValueError
        primary = RouteTarget.model_validate(primary_object, strict=True)
        complement_values = cast(list[object], complement_object)
        if any(type(item) is not str for item in complement_values):
            raise ValueError
        complements = cast(tuple[str, ...], tuple(complement_values))
        if (
            complements != tuple(sorted(set(complements)))
            or primary.agent_id in complements
            or canonical_lifecycle_json(
                {
                    "primary": primary.model_dump(mode="json"),
                    "complement_card_ids": list(complements),
                }
            )
            != raw
        ):
            raise ValueError
        return primary, complements
    except Exception as error:
        raise ConflictUnavailable() from error


def _reduce(
    candidates: tuple[ConflictCandidateSnapshot, ...], votes: tuple[sqlite3.Row, ...]
) -> tuple[ConflictOutcome, str | None, tuple[str, ...]]:
    participants = _candidate_owners(candidates)
    by_actor = {str(vote["actor_id"]): vote for vote in votes}
    if tuple(sorted(by_actor)) != participants:
        return "still_open", None, ()
    selected = {str(vote["on_candidate_card_id"]) for vote in votes}
    if len(selected) != 1:
        return "deadlocked", None, ()
    primary = next(iter(selected))
    complements = tuple(
        sorted(
            candidate.card_id
            for candidate in candidates
            if candidate.card_id != primary
            and str(by_actor[candidate.owner_user_id]["stance"]) == "keep_as_complement"
        )
    )
    return "agreed", primary, complements


def _result_from_receipt(
    connection: sqlite3.Connection, receipt: sqlite3.Row, *, replayed: bool
) -> ConflictConcurrenceResult:
    case = connection.execute(
        "SELECT * FROM central_inbox_conflict_cases WHERE case_id=?", (receipt["case_id"],)
    ).fetchone()
    request = read_lifecycle_request(connection, str(receipt["request_id"]))
    audit = connection.execute(
        "SELECT * FROM central_inbox_conflict_audits WHERE receipt_id=?", (receipt["receipt_id"],)
    ).fetchone()
    if case is None or request is None or audit is None:
        raise ConflictStaleOrConflict()
    outcome = cast(ConflictOutcome, str(receipt["outcome"]))
    if (
        receipt["org_id"] != case["org_id"]
        or receipt["request_id"] != case["request_id"]
        or audit["case_id"] != receipt["case_id"]
        or audit["request_id"] != receipt["request_id"]
        or audit["actor_id"] != receipt["actor_id"]
        or audit["command_digest"] != receipt["command_digest"]
        or int(audit["resulting_case_revision"]) != int(receipt["resulting_case_revision"])
        or int(audit["resulting_request_revision"]) != int(receipt["resulting_request_revision"])
        or audit["outcome"] != outcome
        or int(case["revision"]) < int(receipt["resulting_case_revision"])
    ):
        raise ConflictStaleOrConflict()
    if outcome == "still_open":
        if int(request.revision) < int(receipt["resulting_request_revision"]):
            raise ConflictStaleOrConflict()
    elif (
        case["state"] != "resolved"
        or case["resolved_outcome"] != outcome
        or case["resolution_receipt_id"] != receipt["receipt_id"]
        or int(request.revision) != int(receipt["resulting_request_revision"])
    ):
        raise ConflictStaleOrConflict()
    return ConflictConcurrenceResult(
        receipt_id=str(receipt["receipt_id"]),
        concurrence_command_digest=str(receipt["command_digest"]),
        case_id=str(receipt["case_id"]),
        case_revision=int(receipt["resulting_case_revision"]),
        request_id=str(receipt["request_id"]),
        request_revision=int(receipt["resulting_request_revision"]),
        state=cast(Literal["open", "resolved"], str(receipt["state"])),
        outcome=outcome,
        replayed=replayed,
    )


def _append_replayed_conflict_evidence(
    connection: sqlite3.Connection, *, receipt: sqlite3.Row,
    current_policy_digest: str,
) -> None:
    org_id = str(receipt["org_id"])
    receipt_id = str(receipt["receipt_id"])
    request_id = str(receipt["request_id"])
    command_digest = str(receipt["command_digest"])
    case_id = str(receipt["case_id"])
    actor_id = str(receipt["actor_id"])
    occurred_at = str(receipt["created_at"])
    authority = canonical_v19_file_authority(
        source_policy_digest=current_policy_digest,
        current_snapshot_digest=current_policy_digest,
    )
    append_committed_source_evidence_if_v19(
        connection, org_id=org_id, receipt_id=receipt_id,
        command_digest=command_digest,
        event_type="conflict_concurrence_recorded", action="conflict.concur",
        resource=SafeResourceRef(kind="conflict_case", resource_id=case_id),
        change=ConflictChange(case_id=case_id, request_id=request_id),
        actor_user_id=actor_id, occurred_at=occurred_at,
        policy_revision_id=authority.policy_revision_id,
        policy_epoch=authority.policy_epoch,
        policy_digest=authority.policy_digest,
        source=SourceReceiptProvenance(
            kind="conflict_concurrence", receipt_key=receipt_id,
            receipt_digest=source_receipt_digest(
                connection, "conflict_concurrence", org_id, receipt_id
            ),
        ),
    )
    outcome = str(receipt["outcome"])
    if outcome == "still_open":
        return
    to_state: RequestState = {
        "agreed": "ready_to_dispatch",
        "route_rejected": "declined",
        "deadlocked": "awaiting_manager",
    }[outcome]  # type: ignore[assignment]
    append_committed_source_evidence_if_v19(
        connection, org_id=org_id,
        receipt_id=f"conflict-state:{receipt_id}",
        command_digest=command_digest, event_type="request_state_changed",
        action="conflict.resolve",
        resource=SafeResourceRef(
            kind="question_request", resource_id=request_id
        ),
        change=QuestionChange(
            request_id=request_id, from_state="awaiting_conflict",
            to_state=to_state,
        ),
        actor_user_id=actor_id, occurred_at=occurred_at,
        policy_revision_id=authority.policy_revision_id,
        policy_epoch=authority.policy_epoch,
        policy_digest=authority.policy_digest,
        source=SourceReceiptProvenance(
            kind="conflict_state", receipt_key=receipt_id,
            receipt_digest=source_receipt_digest(
                connection, "conflict_state", org_id, receipt_id
            ),
        ),
    )
    if outcome == "deadlocked":
        manager_item_id = str(receipt["manager_item_id"])
        append_committed_source_evidence_if_v19(
            connection, org_id=org_id,
            receipt_id=f"manager-deadlock:{receipt_id}",
            command_digest=command_digest,
            event_type="manager_item_changed", action="manager_item.create",
            resource=SafeResourceRef(
                kind="manager_item", resource_id=manager_item_id
            ),
            change=ManagerItemChange(
                manager_item_id=manager_item_id, request_id=request_id
            ),
            actor_user_id=actor_id, occurred_at=occurred_at,
            policy_revision_id=authority.policy_revision_id,
            policy_epoch=authority.policy_epoch,
            policy_digest=authority.policy_digest,
            source=SourceReceiptProvenance(
                kind="manager_deadlock", receipt_key=receipt_id,
                receipt_digest=source_receipt_digest(
                    connection, "manager_deadlock", org_id, receipt_id
                ),
            ),
        )


def _summary(row: sqlite3.Row) -> ConflictCaseSummary:
    candidates = _snapshots(str(row["candidate_snapshot_json"]))
    return ConflictCaseSummary(
        case_id=str(row["case_id"]),
        request_id=str(row["request_id"]),
        request_revision=int(row["request_revision"]),
        state=cast(ConflictCaseState, str(row["state"])),
        round=int(row["round"]),
        revision=int(row["revision"]),
        candidate_card_ids=tuple(candidate.card_id for candidate in candidates),
        opened_at=_timestamp(str(row["opened_at"])),
    )


def _detail(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    actor_id: str,
    candidates: tuple[ConflictCandidateSnapshot, ...],
) -> ConflictCaseDetail:
    request = read_lifecycle_request(connection, str(row["request_id"]))
    if request is None:
        raise ConflictUnavailable()
    vote = connection.execute(
        "SELECT * FROM central_inbox_conflict_concurrences "
        "WHERE case_id=? AND round=? AND actor_id=?",
        (row["case_id"], row["round"], actor_id),
    ).fetchone()
    own = (
        None
        if vote is None
        else ConflictConcurrence(
            on_candidate_card_id=str(vote["on_candidate_card_id"]),
            stance=cast(ConcurrenceStance, str(vote["stance"])),
            rationale=str(vote["rationale"]),
            round=int(vote["round"]),
        )
    )
    grants = tuple(
        ConflictEvidenceGrant(
            grant_id=str(grant["grant_id"]),
            candidate_card_id=str(grant["candidate_card_id"]),
            candidate_card_revision=int(grant["candidate_card_revision"]),
            concept_ref=str(grant["concept_ref"]),
            expires_at=_timestamp(str(grant["expires_at"])),
            single_use=bool(grant["single_use"]),
            status=cast(
                Literal["available", "consumed", "expired"], str(grant["status"])
            ),
        )
        for grant in connection.execute(
            "SELECT * FROM central_inbox_conflict_evidence_grants "
            "WHERE case_id=? ORDER BY candidate_card_id",
            (row["case_id"],),
        )
    )
    return ConflictCaseDetail(
        case_id=str(row["case_id"]),
        request_id=request.request_id,
        request_revision=request.revision,
        state=cast(ConflictCaseState, str(row["state"])),
        round=int(row["round"]),
        revision=int(row["revision"]),
        candidate_card_ids=tuple(candidate.card_id for candidate in candidates),
        opened_at=_timestamp(str(row["opened_at"])),
        expected_case_revision=int(row["revision"]),
        expected_request_revision=request.revision,
        expected_round=int(row["round"]),
        question=request.question,
        candidates=candidates,
        own_concurrence=own,
        evidence_grants=grants,
    )


def _case_row(connection: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT c.*,q.revision AS request_revision FROM central_inbox_conflict_cases c "
        "JOIN question_requests q ON q.request_id=c.request_id WHERE c.case_id=?",
        (case_id,),
    ).fetchone()


def _legacy_candidate_snapshots(
    connection: sqlite3.Connection, legacy: sqlite3.Row
) -> tuple[ConflictCandidateSnapshot, ...]:
    from agent_org_network.sqlite_production_agent_cards import (
        validate_production_agent_card_rows,
    )

    if not _table_exists(connection, "production_agent_cards"):
        raise ConflictUnavailable()
    try:
        validate_production_agent_card_rows(connection, str(legacy["org_id"]))
    except Exception as error:
        raise ConflictUnavailable() from error
    values = read_legacy_conflict_candidates(str(legacy["candidates_json"]))
    result: list[ConflictCandidateSnapshot] = []
    for value in values:
        card_id = value["agent_id"]
        owner_id = value["owner_id"]
        row = connection.execute(
            "SELECT owner_id,card_digest,revision FROM production_agent_cards "
            "WHERE org_id=? AND agent_id=?",
            (legacy["org_id"], card_id),
        ).fetchone()
        if (
            row is None
            or row["owner_id"] != owner_id
            or int(row["revision"]) < 1
            or _SHA256.fullmatch(str(row["card_digest"])) is None
        ):
            raise ConflictUnavailable()
        binding = CardBinding(
            agent_id=card_id,
            owner_id=owner_id,
            revision=int(row["revision"]),
            card_digest=str(row["card_digest"]),
            concept_ref=str(legacy["intent"]),
            coverage_digest=_coverage_digest(
                str(legacy["intent"]), card_id, str(row["card_digest"])
            ),
        )
        result.append(_snapshot_from_binding(str(legacy["intent"]), binding))
    return tuple(result)


def _snapshot_from_binding(intent: str, binding: CardBinding) -> ConflictCandidateSnapshot:
    digest = binding.card_digest
    concept = binding.concept_ref or intent
    coverage = binding.coverage_digest
    if _SHA256.fullmatch(digest) is None or _SHA256.fullmatch(coverage) is None:
        raise ConflictUnavailable()
    return ConflictCandidateSnapshot(
        card_id=binding.agent_id,
        card_revision=binding.revision,
        card_digest=digest,
        owner_user_id=binding.owner_id,
        concept_ref=concept,
        coverage_digest=coverage,
    )


def _insert_case_and_grants(
    connection: sqlite3.Connection,
    *,
    case_id: str,
    request: QuestionRequest,
    intent: str,
    candidates: tuple[ConflictCandidateSnapshot, ...],
    opened_at: datetime,
) -> None:
    if (
        not isinstance(request.state, AwaitingConflict)
        or request.state.case_id != case_id
        or request.intent != intent
        or len(candidates) < 2
        or len({candidate.card_id for candidate in candidates}) != len(candidates)
        or any(candidate.owner_user_id == "" for candidate in candidates)
    ):
        raise ConflictUnavailable()
    snapshot_json = _snapshot_json(candidates)
    snapshot_digest = sha256(snapshot_json.encode()).hexdigest()
    binding_digest = sha256(
        canonical_lifecycle_json(
            {
                "org_id": request.org_id,
                "request_id": request.request_id,
                "case_id": case_id,
                "intent": intent,
                "candidate_snapshot_digest": snapshot_digest,
            }
        ).encode()
    ).hexdigest()
    connection.execute(
        "INSERT INTO central_inbox_conflict_cases"
        "(case_id,request_id,org_id,intent,candidate_snapshot_json,candidate_snapshot_digest,"
        "authority_binding_digest,opened_request_revision,state,round,revision,resolved_outcome,"
        "resolution_receipt_id,escalated_receipt_id,opened_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            case_id,
            request.request_id,
            request.org_id,
            intent,
            snapshot_json,
            snapshot_digest,
            binding_digest,
            request.revision,
            "open",
            1,
            1,
            None,
            None,
            None,
            opened_at.isoformat(),
            opened_at.isoformat(),
        ),
    )
    for candidate in candidates:
        connection.execute(
            "INSERT INTO central_inbox_conflict_evidence_grants"
            "(grant_id,case_id,candidate_card_id,candidate_card_revision,concept_ref,"
            "expires_at,single_use,status) VALUES (?,?,?,?,?,?,1,'available')",
            (
                _grant_id(case_id, candidate),
                case_id,
                candidate.card_id,
                candidate.card_revision,
                candidate.concept_ref,
                request.state.handling.due_at.isoformat(),
            ),
        )


def _snapshot_json(candidates: tuple[ConflictCandidateSnapshot, ...]) -> str:
    return canonical_lifecycle_json(
        [
            {
                "card_id": candidate.card_id,
                "card_revision": candidate.card_revision,
                "card_digest": candidate.card_digest,
                "owner_user_id": candidate.owner_user_id,
                "concept_ref": candidate.concept_ref,
                "coverage_digest": candidate.coverage_digest,
            }
            for candidate in candidates
        ]
    )


def _snapshots(raw: str) -> tuple[ConflictCandidateSnapshot, ...]:
    try:
        parsed_object: object = json.loads(raw)
        if type(parsed_object) is not list:
            raise ValueError
        parsed = cast(list[object], parsed_object)
        result = tuple(_snapshot_from_object(value) for value in parsed)
        if (
            len(result) != len(parsed)
            or len(result) < 2
            or len({candidate.card_id for candidate in result}) != len(result)
            or any(
                not _valid_reference(candidate.card_id)
                or candidate.card_revision < 1
                or _SHA256.fullmatch(candidate.card_digest) is None
                or not _valid_reference(candidate.owner_user_id)
                or not candidate.concept_ref
                or _SHA256.fullmatch(candidate.coverage_digest) is None
                for candidate in result
            )
            or _snapshot_json(result) != raw
        ):
            raise ValueError
        return result
    except Exception as error:
        raise ConflictUnavailable() from error


def _snapshot_from_object(value: object) -> ConflictCandidateSnapshot:
    if type(value) is not dict:
        raise ValueError
    mapping = cast(dict[object, object], value)
    card_id = mapping.get("card_id")
    card_revision = mapping.get("card_revision")
    card_digest = mapping.get("card_digest")
    owner_user_id = mapping.get("owner_user_id")
    concept_ref = mapping.get("concept_ref")
    coverage_digest = mapping.get("coverage_digest")
    if (
        type(card_id) is not str
        or type(card_revision) is not int
        or type(card_digest) is not str
        or type(owner_user_id) is not str
        or type(concept_ref) is not str
        or type(coverage_digest) is not str
        or set(mapping)
        != {
            "card_id",
            "card_revision",
            "card_digest",
            "owner_user_id",
            "concept_ref",
            "coverage_digest",
        }
    ):
        raise ValueError
    return ConflictCandidateSnapshot(
        card_id=card_id,
        card_revision=card_revision,
        card_digest=card_digest,
        owner_user_id=owner_user_id,
        concept_ref=concept_ref,
        coverage_digest=coverage_digest,
    )


def _candidate_owners(
    candidates: tuple[ConflictCandidateSnapshot, ...],
) -> tuple[str, ...]:
    return tuple(sorted({candidate.owner_user_id for candidate in candidates}))


def _coverage_digest(intent: str, card_id: str, card_digest: str) -> str:
    return sha256(
        canonical_lifecycle_json(
            {"concept_ref": intent, "candidate_card_id": card_id, "card_digest": card_digest}
        ).encode()
    ).hexdigest()


def _grant_id(case_id: str, candidate: ConflictCandidateSnapshot) -> str:
    return "conflict-grant:" + sha256(
        canonical_lifecycle_json(
            {
                "case_id": case_id,
                "candidate_card_id": candidate.card_id,
                "candidate_card_revision": candidate.card_revision,
                "concept_ref": candidate.concept_ref,
            }
        ).encode()
    ).hexdigest()


def _validate_catalog(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ConflictUnavailable()
    if _existing_owned(connection) != set(_OWNED):
        raise ConflictUnavailable()
    if _catalog_signature(connection) != _EXPECTED_CATALOG:
        raise ConflictUnavailable()
    marker = connection.execute(
        "SELECT name,version FROM central_inbox_component_schema"
    ).fetchall()
    if [tuple(row) for row in marker] != [(_COMPONENT_NAME, _COMPONENT_VERSION)]:
        raise ConflictUnavailable()
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ConflictUnavailable()
    for case in connection.execute("SELECT * FROM central_inbox_conflict_cases"):
        legacy = connection.execute(
            "SELECT * FROM central_question_conflict_cases WHERE case_id=?", (case["case_id"],)
        ).fetchone()
        request = read_lifecycle_request(connection, str(case["request_id"]))
        candidates = _snapshots(str(case["candidate_snapshot_json"]))
        if (
            legacy is None
            or request is None
            or legacy["request_id"] != case["request_id"]
            or legacy["org_id"] != case["org_id"] != request.org_id
            or legacy["intent"] != case["intent"] != request.intent
            or sha256(str(case["candidate_snapshot_json"]).encode()).hexdigest()
            != case["candidate_snapshot_digest"]
            or case["authority_binding_digest"]
            != sha256(
                canonical_lifecycle_json(
                    {
                        "org_id": request.org_id,
                        "request_id": request.request_id,
                        "case_id": case["case_id"],
                        "intent": case["intent"],
                        "candidate_snapshot_digest": case["candidate_snapshot_digest"],
                    }
                ).encode()
            ).hexdigest()
            or int(case["opened_request_revision"]) != 1
            or int(case["round"]) != 1
        ):
            raise ConflictUnavailable()
        grants = tuple(
            connection.execute(
                "SELECT * FROM central_inbox_conflict_evidence_grants "
                "WHERE case_id=? ORDER BY candidate_card_id",
                (case["case_id"],),
            )
        )
        by_card = {str(grant["candidate_card_id"]): grant for grant in grants}
        if set(by_card) != {candidate.card_id for candidate in candidates}:
            raise ConflictUnavailable()
        for candidate in candidates:
            grant = by_card[candidate.card_id]
            if (
                grant["grant_id"] != _grant_id(str(case["case_id"]), candidate)
                or int(grant["candidate_card_revision"]) != candidate.card_revision
                or grant["concept_ref"] != candidate.concept_ref
                or int(grant["single_use"]) != 1
                or grant["status"] not in {"available", "consumed", "expired"}
            ):
                raise ConflictUnavailable()
        votes = tuple(
            connection.execute(
                "SELECT * FROM central_inbox_conflict_concurrences "
                "WHERE case_id=? ORDER BY case_revision_after",
                (case["case_id"],),
            )
        )
        if int(case["revision"]) != 1 + len(votes):
            raise ConflictUnavailable()
        for offset, vote in enumerate(votes, start=2):
            receipt = connection.execute(
                "SELECT * FROM central_inbox_conflict_receipts "
                "WHERE case_id=? AND resulting_case_revision=?",
                (case["case_id"], offset),
            ).fetchone()
            audit = (
                None
                if receipt is None
                else connection.execute(
                    "SELECT * FROM central_inbox_conflict_audits WHERE receipt_id=?",
                    (receipt["receipt_id"],),
                ).fetchone()
            )
            if (
                int(vote["case_revision_after"]) != offset
                or vote["actor_id"] not in _candidate_owners(candidates)
                or vote["on_candidate_card_id"]
                not in {candidate.card_id for candidate in candidates}
                or receipt is None
                or audit is None
                or receipt["actor_id"] != vote["actor_id"]
                or receipt["round"] != vote["round"]
                or receipt["idempotency_key"] != vote["idempotency_key"]
                or receipt["command_digest"] != vote["command_digest"]
                or int(receipt["expected_case_revision"]) != offset - 1
                or int(receipt["expected_round"]) != int(case["round"])
                or int(receipt["resulting_case_revision"]) != offset
                or receipt["command_digest"] != _stored_command_digest(vote, receipt)
                or audit["case_id"] != case["case_id"]
                or audit["request_id"] != case["request_id"]
                or audit["actor_id"] != vote["actor_id"]
                or audit["command_digest"] != vote["command_digest"]
                or int(audit["resulting_case_revision"]) != offset
                or int(audit["resulting_request_revision"])
                != int(receipt["resulting_request_revision"])
                or audit["outcome"] != receipt["outcome"]
                or audit["created_at"] != receipt["created_at"]
            ):
                raise ConflictUnavailable()
            reduced, _primary, _complements = _reduce(candidates, votes[: offset - 1])
            receipt_outcome = str(receipt["outcome"])
            if reduced == "agreed":
                expected_outcomes = {"agreed", "route_rejected"}
            else:
                expected_outcomes = {reduced}
            terminal = receipt_outcome != "still_open"
            if (
                receipt_outcome not in expected_outcomes
                or receipt["state"] != ("resolved" if terminal else "open")
                or int(receipt["resulting_request_revision"])
                != int(receipt["expected_request_revision"]) + (1 if terminal else 0)
                or (receipt["route_target_json"] is not None) != (receipt_outcome == "agreed")
                or (receipt["manager_item_id"] is not None) != (receipt_outcome == "deadlocked")
            ):
                raise ConflictUnavailable()
        if case["state"] == "open":
            if (
                not isinstance(request.state, AwaitingConflict)
                or request.state.case_id != case["case_id"]
                or case["resolved_outcome"] is not None
                or case["resolution_receipt_id"] is not None
                or connection.execute(
                    "SELECT 1 FROM central_inbox_conflict_deadlock_manager_links "
                    "WHERE case_id=?",
                    (case["case_id"],),
                ).fetchone()
                is not None
            ):
                raise ConflictUnavailable()
        elif case["state"] == "resolved":
            receipt = connection.execute(
                "SELECT * FROM central_inbox_conflict_receipts WHERE receipt_id=?",
                (case["resolution_receipt_id"],),
            ).fetchone()
            if (
                receipt is None
                or receipt["outcome"] != case["resolved_outcome"]
                or receipt["case_id"] != case["case_id"]
                or receipt["request_id"] != request.request_id
                or int(receipt["resulting_case_revision"]) != int(case["revision"])
                or int(receipt["resulting_request_revision"]) != request.revision
                or receipt["created_at"] != case["updated_at"]
            ):
                raise ConflictUnavailable()
            outcome = str(case["resolved_outcome"])
            if outcome == "agreed":
                route_binding = _route_binding(str(receipt["route_target_json"]))
                valid = (
                    isinstance(request.state, ReadyToDispatch)
                    and request.state.route == route_binding[0]
                    and request.state.attempt == 1
                    and request.state.trigger_key
                    == f"conflict:{case['case_id']}:{case['round']}"
                    and receipt["manager_item_id"] is None
                )
            elif outcome == "route_rejected":
                valid = (
                    isinstance(request.state, DeclinedRequest)
                    and request.state.reason_code == "route_rejected"
                    and receipt["route_target_json"] is None
                    and receipt["manager_item_id"] is None
                )
            elif outcome == "deadlocked":
                deadlock_state = request.state
                link = connection.execute(
                    "SELECT * FROM central_inbox_conflict_deadlock_manager_links WHERE case_id=?",
                    (case["case_id"],),
                ).fetchone()
                manager_item = (
                    None
                    if link is None
                    else connection.execute(
                        "SELECT request_id,org_id,item_id,manager_id,intent "
                        "FROM central_question_manager_items WHERE item_id=?",
                        (link["manager_item_id"],),
                    ).fetchone()
                )
                valid = (
                    isinstance(deadlock_state, AwaitingManager)
                    and deadlock_state.public_kind == "contested"
                    and link is not None
                    and manager_item is not None
                    and link["request_id"] == request.request_id
                    and link["manager_item_id"] == deadlock_state.item_id
                    and link["manager_item_id"] == receipt["manager_item_id"]
                    and link["manager_id"] == manager_item["manager_id"]
                    and manager_item["request_id"] == request.request_id
                    and manager_item["org_id"] == request.org_id == case["org_id"]
                    and manager_item["item_id"] == deadlock_state.item_id
                    and manager_item["intent"] == request.intent == case["intent"]
                    and link["resolution_receipt_id"] == case["resolution_receipt_id"]
                    and link["candidate_snapshot_digest"] == case["candidate_snapshot_digest"]
                )
            else:
                valid = False
            if not valid:
                raise ConflictUnavailable()
            link_count = int(
                connection.execute(
                    "SELECT count(*) FROM central_inbox_conflict_deadlock_manager_links "
                    "WHERE case_id=?",
                    (case["case_id"],),
                ).fetchone()[0]
            )
            if link_count != (1 if outcome == "deadlocked" else 0):
                raise ConflictUnavailable()
        else:
            raise ConflictUnavailable()
    legacy_count = int(
        connection.execute("SELECT count(*) FROM central_question_conflict_cases").fetchone()[0]
    )
    case_count = int(
        connection.execute("SELECT count(*) FROM central_inbox_conflict_cases").fetchone()[0]
    )
    if legacy_count != case_count:
        raise ConflictUnavailable()
    receipt_orphan = connection.execute(
        "SELECT r.receipt_id FROM central_inbox_conflict_receipts r "
        "LEFT JOIN central_inbox_conflict_concurrences v "
        "ON v.case_id=r.case_id AND v.round=r.round AND v.actor_id=r.actor_id "
        "WHERE v.case_id IS NULL OR v.command_digest!=r.command_digest "
        "OR v.idempotency_key!=r.idempotency_key"
    ).fetchone()
    audit_orphan = connection.execute(
        "SELECT a.receipt_id FROM central_inbox_conflict_audits a "
        "LEFT JOIN central_inbox_conflict_receipts r ON r.receipt_id=a.receipt_id "
        "WHERE r.receipt_id IS NULL"
    ).fetchone()
    if receipt_orphan is not None or audit_orphan is not None:
        raise ConflictUnavailable()


def _existing_owned(connection: sqlite3.Connection) -> set[str]:
    placeholders = ",".join("?" for _ in _OWNED)
    return {
        str(row[0])
        for row in connection.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
            _OWNED,
        )
    }


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _catalog_signature(connection: sqlite3.Connection) -> tuple[object, ...]:
    tables = tuple(
        (
            name,
            " ".join(
                str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
                    ).fetchone()[0]
                ).split()
            ),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info({name})")),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({name})")),
            tuple(tuple(row) for row in connection.execute(f"PRAGMA index_list({name})")),
        )
        for name in sorted(_OWNED)
    )
    placeholders = ",".join("?" for _ in _OWNED)
    triggers = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type='trigger' "
            f"AND tbl_name IN ({placeholders}) ORDER BY type,name",
            _OWNED,
        )
    )
    return tables, triggers


def _expected_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE question_requests(request_id TEXT PRIMARY KEY NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE central_question_conflict_cases("
            "case_id TEXT UNIQUE,request_id TEXT UNIQUE,org_id TEXT,intent TEXT)"
        )
        connection.execute(
            "CREATE TABLE central_question_manager_items("
            "request_id TEXT UNIQUE,item_id TEXT UNIQUE,manager_id TEXT)"
        )
        for ddl in _TABLES:
            connection.execute(ddl)
        for ddl in _TRIGGERS:
            connection.execute(ddl)
        return _catalog_signature(connection)
    finally:
        connection.close()


_EXPECTED_CATALOG = _expected_catalog()


def _timestamp(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConflictUnavailable()
    return value


def _valid_reference(value: object) -> bool:
    return type(value) is str and _REFERENCE.fullmatch(value) is not None


__all__ = [
    "ConflictAuthorizationProof",
    "ConflictAuthority",
    "ConflictCandidateSnapshot",
    "ConflictCaseDetail",
    "ConflictCaseSummary",
    "ConflictConcurrence",
    "ConflictConcurrenceApplication",
    "ConflictConcurrenceCommand",
    "ConflictConcurrenceResult",
    "ConflictEvidenceGrant",
    "ConflictInboxApplication",
    "ConflictNotFound",
    "ConflictReadCommand",
    "ConflictRouteAuthorization",
    "ConflictSessionUnauthenticated",
    "ConflictStaleOrConflict",
    "ConflictUnavailable",
    "FileReloadingConflictAuthority",
    "central_inbox_conflict_schema_ready",
    "insert_initial_conflict_companion",
    "migrate_central_inbox_conflict_schema",
]
