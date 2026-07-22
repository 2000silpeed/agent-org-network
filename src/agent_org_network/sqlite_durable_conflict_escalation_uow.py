"""P17.9 S4.3c.3 escalation Unit of Work(ADR 0065 §9).

`conflict.escalate` 명령 하나를 한 SQLite transaction으로 세 aggregate(Case
escalated·FromDeadlock ManagerItem·Request AwaitingManager)와 c.2 receipt
graph(receipt·evidence·result projection·audit/outbox intent) 5행에 원자
결박한다. 권한 중앙·HITL 승인·toggle 불인정·receipt-parent shape는
ADR 0065 §1~§8이 이미 정했고, 이 모듈은 그 계약들의 조립·쓰기 순서·terminal
Case에서의 replay 의미만 확정한다(S4.2b `sqlite_durable_direct_conflict_concurrence`
동형).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.conflict_escalation_approval_evidence import (
    ESCALATE_ACTION,
    canonical_escalate_command_digest,
    escalation_cause_digest,
    escalation_resource_fingerprint,
)
from agent_org_network.conflict_escalation_approval_verifier import (
    CurrentEscalationApprovalEvidenceResolver,
    EscalationApprovalProvider,
    acquire_escalation_approval,
    reconfirm_escalation_approval,
)
from agent_org_network.conflict_escalation_registry_snapshot import (
    ConflictEscalationRegistrySnapshot,
    ConflictEscalationRegistrySnapshotReader,
)
from agent_org_network.conflict_open_contract import ConflictOpenCandidateClaim
from agent_org_network.durable_conflict_escalation_evidence import (
    CandidateRegistryChanged,
    DivergentVotes,
    SealedEscalationEvidence,
)
from agent_org_network.question_request import (
    AwaitingConflict,
    AwaitingManager,
    HandlingAssignment,
)
from agent_org_network.sqlite_completion import validate_sqlite_completion_connection
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    validate_sqlite_durable_conflict_escalation_receipts_connection,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    validate_sqlite_durable_linked_aggregates_connection,
)


class DurableConflictEscalationError(RuntimeError):
    """Base error deliberately free of Registry/authority/HITL internals."""


class DurableConflictEscalationConflict(DurableConflictEscalationError):
    pass


class DurableConflictEscalationUnavailable(DurableConflictEscalationError):
    pass


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, raw: str) -> str:
    return f"{kind}:{_sha(raw)}"


_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?[+-][0-9]{2}:[0-9]{2}\Z"
)


def _timestamp(value: datetime) -> str:
    rendered = value.isoformat(timespec="microseconds" if value.microsecond else "seconds")
    if _TIMESTAMP_RE.fullmatch(rendered) is None:
        raise DurableConflictEscalationConflict("canonical calendar command time이 필요합니다.")
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise DurableConflictEscalationConflict(
            "calendar command time이 올바르지 않습니다."
        ) from error
    precision = "microseconds" if parsed.microsecond else "seconds"
    if parsed.utcoffset() is None or parsed.isoformat(timespec=precision) != rendered:
        raise DurableConflictEscalationConflict("canonical command time이 필요합니다.")
    return rendered


class SealedEscalationCauseReader(Protocol):
    """S4.3b reader의 소비 포트. Pending/오류는 이 UoW가 Conflict/Unavailable로 닫는다."""

    def read_sealed(
        self, *, org_id: str, conflict_id: str, claims: Sequence[ConflictOpenCandidateClaim]
    ) -> SealedEscalationEvidence: ...


@dataclass(frozen=True)
class DurableConflictEscalateCommand:
    """Server-authenticated typed command, not an MCP payload DTO."""

    conflict_id: str
    request_id: str
    expected_request_revision: int
    claims: tuple[ConflictOpenCandidateClaim, ...]


@dataclass(frozen=True)
class DurableConflictEscalatedToManager:
    receipt_id: str
    conflict_id: str
    request_id: str
    manager_item_id: str
    request_revision: int
    manager_subject_ref: str


@dataclass(frozen=True)
class DurableConflictEscalatedToRoot:
    receipt_id: str
    conflict_id: str
    request_id: str
    manager_item_id: str
    request_revision: int
    root_subject_ref: str


DurableConflictEscalationResult = (
    DurableConflictEscalatedToManager | DurableConflictEscalatedToRoot
)


def _no_fault(_point: str) -> None:
    return None


class DurableConflictEscalationUnitOfWork:
    """한 durable open Conflict Case를 사람 승인 아래 Manager/root로 원자 escalate한다."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        cause_reader: SealedEscalationCauseReader,
        graph_reader: ConflictEscalationRegistrySnapshotReader,
        central_authorizer: CentralAuthorizer | None,
        approval_provider: EscalationApprovalProvider,
        approval_resolver: CurrentEscalationApprovalEvidenceResolver,
        clock: Callable[[], datetime],
        receipt_id_factory: Callable[[], str],
        manager_item_id_factory: Callable[[], str],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._completion = completion
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._cause_reader = cause_reader
        self._graph_reader = graph_reader
        self._authorizer = central_authorizer
        self._approval_provider = approval_provider
        self._approval_resolver = approval_resolver
        self._clock = clock
        self._receipt_id_factory = receipt_id_factory
        self._manager_item_id_factory = manager_item_id_factory
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        try:
            with self._tx.scope():
                self._tx.validate_component(
                    validate_sqlite_durable_conflict_escalation_receipts_connection
                )
                self._tx.validate_component(validate_sqlite_durable_linked_aggregates_connection)
                self._tx.validate_component(validate_sqlite_completion_connection)
        except Exception as error:
            raise DurableConflictEscalationUnavailable(
                "durable Conflict escalation capability를 열 수 없습니다."
            ) from error

    def escalate(
        self, *, principal: AuthenticatedPrincipal, command: DurableConflictEscalateCommand
    ) -> DurableConflictEscalationResult:
        if (
            type(principal) is not AuthenticatedPrincipal
            or type(command) is not DurableConflictEscalateCommand
        ):
            raise DurableConflictEscalationUnavailable(
                "서버 principal과 exact conflict.escalate command가 필요합니다."
            )
        self._valid_command(command)
        resource = ResourceRef(
            org_id=principal.org_id, kind="conflict_case", resource_id=command.conflict_id
        )
        canonical_command = {
            "conflict_id": command.conflict_id,
            "request_id": command.request_id,
            "expected_request_revision": command.expected_request_revision,
        }
        command_digest = canonical_escalate_command_digest(
            resource=resource, command=canonical_command
        )
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                # Replay is not a privilege escalation: every companion capability
                # is reread inside this write snapshot, never trusted from open time.
                try:
                    self._tx.validate_component_in_transaction(
                        validate_sqlite_durable_conflict_escalation_receipts_connection
                    )
                    self._tx.validate_component_in_transaction(
                        validate_sqlite_durable_linked_aggregates_connection
                    )
                    self._tx.validate_component_in_transaction(
                        validate_sqlite_completion_connection
                    )
                except DurableConflictEscalationError:
                    raise
                except Exception as error:
                    raise DurableConflictEscalationUnavailable(
                        "durable Conflict escalation capability가 command 중 검증되지 않았습니다."
                    ) from error

                case = self._case(command.conflict_id, principal.org_id)
                if case is None or case["request_id"] != command.request_id:
                    raise DurableConflictEscalationConflict("durable open Conflict Case가 없습니다.")
                receipt = self._receipt(principal.org_id, command.conflict_id)

                if receipt is not None:
                    if case["status"] != "escalated":
                        raise DurableConflictEscalationUnavailable(
                            "escalation receipt와 durable Conflict Case 상태가 어긋납니다."
                        )
                    if receipt["command_digest"] != command_digest:
                        raise DurableConflictEscalationConflict(
                            "이미 escalated된 Conflict Case의 다른 command는 replay할 수 없습니다."
                        )
                    result = self._stored_result(receipt, principal, command, case)
                    self._tx.commit()
                    return result

                # FRESH
                if case["status"] != "open":
                    raise DurableConflictEscalationConflict("current open Conflict Case가 아닙니다.")
                request = self._awaiting_conflict_request(principal, command, case)
                actor_ref = _ref("subject", principal.subject_id)
                cause0 = self._cause(
                    org_id=principal.org_id, conflict_id=command.conflict_id, claims=command.claims
                )
                graph0 = self._graph(principal.org_id, command.claims)
                self._authorize(principal, resource)
                evidence = acquire_escalation_approval(
                    principal,
                    ESCALATE_ACTION,
                    resource,
                    command_digest,
                    provider=self._approval_provider,
                    resolver=self._approval_resolver,
                    command=canonical_command,
                    cause=cause0,
                    graph_snapshot=graph0,
                )
                if evidence is None:
                    raise DurableConflictEscalationConflict("escalation 사람 승인 증거가 없습니다.")

                # PREWRITE — linearization point: current Case/Request/cause/graph
                # and central authority are all reread immediately before the
                # first write.
                case = self._current_open_case(command.conflict_id, case)
                request = self._awaiting_conflict_request(principal, command, case)
                cause1 = self._cause(
                    org_id=principal.org_id, conflict_id=command.conflict_id, claims=command.claims
                )
                if cause1 != cause0:
                    raise DurableConflictEscalationConflict(
                        "escalation 원인이 취득 이후 바뀌었습니다."
                    )
                try:
                    self._graph_reader.verify_current(graph0, claims=command.claims)
                except Exception as error:
                    raise DurableConflictEscalationConflict(
                        "current Conflict escalation Registry graph가 바뀌었습니다."
                    ) from error
                self._authorize(principal, resource)
                if not reconfirm_escalation_approval(
                    evidence,
                    resolver=self._approval_resolver,
                    org_id=principal.org_id,
                    resource=resource,
                    command=canonical_command,
                    cause=cause1,
                    graph_snapshot=graph0,
                ):
                    raise DurableConflictEscalationConflict(
                        "write 직전 escalation 사람 승인 재확인에 실패했습니다."
                    )

                if graph0.manager_subject_ref is not None:
                    target, result_kind = graph0.manager_subject_ref, "escalated_to_manager"
                else:
                    target, result_kind = graph0.root_subject_ref, "escalated_to_root"

                now = self._clock()
                created_at = _timestamp(now)
                receipt_id = self._receipt_id_factory()
                if not receipt_id.strip():
                    raise DurableConflictEscalationConflict("receipt identity가 올바르지 않습니다.")
                receipt_ref = _ref("receipt", receipt_id)
                manager_item_id = self._manager_item_id_factory()
                if not manager_item_id.strip():
                    raise DurableConflictEscalationConflict(
                        "manager item identity가 올바르지 않습니다."
                    )
                manager_item_ref = _ref("manager", manager_item_id)
                cause_digest = escalation_cause_digest(cause1)
                resource_fingerprint = escalation_resource_fingerprint(resource)
                cause_kind = type(cause1).__name__
                is_divergent_votes = isinstance(cause1, DivergentVotes)
                candidate_owner_count = cause1.candidate_owner_count if is_divergent_votes else None
                current_candidate_snapshot = (
                    None if is_divergent_votes else cause1.current_candidate_snapshot_sha256
                )

                self._tx.execute(
                    "INSERT INTO durable_conflict_escalation_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_ref,
                        principal.org_id,
                        command.conflict_id,
                        command.request_id,
                        case["awaiting_revision"],
                        actor_ref,
                        ESCALATE_ACTION,
                        command_digest,
                        resource_fingerprint,
                        _ref("evidence", evidence.evidence_id),
                        cause_digest,
                        graph0.graph_digest,
                        created_at,
                    ),
                )
                self._fault("after_receipt")
                self._tx.execute(
                    "INSERT INTO durable_conflict_escalation_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_ref,
                        principal.org_id,
                        command.conflict_id,
                        command.request_id,
                        cause_kind,
                        cause1.awaiting_revision,
                        cause1.concurrence_round,
                        cause1.candidate_snapshot_sha256,
                        cause1.baseline_sha256,
                        cause1.candidate_claim_sha256,
                        cause1.vote_set_sha256,
                        candidate_owner_count,
                        current_candidate_snapshot,
                        cause_digest,
                        graph0.graph_digest,
                        graph0.manager_subject_ref,
                        graph0.root_subject_ref,
                        created_at,
                    ),
                )
                self._fault("after_evidence")
                self._tx.execute(
                    "INSERT INTO durable_conflict_escalation_result_projections VALUES(?,?,?,?,?,?,?)",
                    (
                        receipt_ref,
                        principal.org_id,
                        command.conflict_id,
                        result_kind,
                        "deadlock",
                        target,
                        created_at,
                    ),
                )
                self._fault("after_result_projection")
                self._tx.execute(
                    "INSERT INTO durable_conflict_escalation_audit_intents VALUES(?,?,?,?,?)",
                    (receipt_ref, principal.org_id, ESCALATE_ACTION, command_digest, created_at),
                )
                self._fault("after_audit_intent")
                self._tx.execute(
                    "INSERT INTO durable_conflict_escalation_outbox_intents VALUES(?,?,?,?,?)",
                    (receipt_ref, principal.org_id, ESCALATE_ACTION, command_digest, created_at),
                )
                self._fault("after_outbox_intent")
                self._tx.execute(
                    "INSERT INTO durable_linked_manager_items VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        manager_item_ref,
                        principal.org_id,
                        command.request_id,
                        case["awaiting_revision"],
                        "deadlock",
                        _ref("source", command.conflict_id),
                        target,
                        "open",
                        created_at,
                    ),
                )
                self._fault("after_manager_item")
                if (
                    self._tx.execute(
                        "UPDATE durable_linked_conflict_cases SET status='escalated' WHERE conflict_id=? AND status='open' AND awaiting_revision=?",
                        (command.conflict_id, case["awaiting_revision"]),
                    ).rowcount
                    != 1
                ):
                    raise DurableConflictEscalationConflict(
                        "commit-time Conflict Case CAS에 실패했습니다."
                    )
                self._fault("after_case_escalated")
                assert isinstance(request.state, AwaitingConflict)
                awaiting_manager = AwaitingManager(
                    item_id=manager_item_ref,
                    public_kind="contested",
                    handling=HandlingAssignment(
                        kind="manager_item",
                        ref=manager_item_ref,
                        due_at=request.state.handling.due_at,
                    ),
                )
                updated = request.transition(awaiting_manager, clock=lambda: now)
                if not self._tx.compare_and_set_question_request(
                    request.request_id, request.revision, request, updated
                ):
                    raise DurableConflictEscalationConflict(
                        "commit-time Question Request CAS에 실패했습니다."
                    )
                self._fault("after_request_awaiting_manager")

                result: DurableConflictEscalationResult
                if result_kind == "escalated_to_manager":
                    result = DurableConflictEscalatedToManager(
                        receipt_ref,
                        command.conflict_id,
                        command.request_id,
                        manager_item_ref,
                        updated.revision,
                        target,
                    )
                else:
                    result = DurableConflictEscalatedToRoot(
                        receipt_ref,
                        command.conflict_id,
                        command.request_id,
                        manager_item_ref,
                        updated.revision,
                        target,
                    )
                self._tx.commit()
                return result
            except Exception:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise

    def _case(self, conflict_id: str, org_id: str) -> sqlite3.Row | None:
        return self._tx.execute(
            "SELECT * FROM durable_linked_conflict_cases WHERE conflict_id=? AND org_id=?",
            (conflict_id, org_id),
        ).fetchone()

    def _receipt(self, org_id: str, conflict_id: str) -> sqlite3.Row | None:
        return self._tx.execute(
            "SELECT * FROM durable_conflict_escalation_receipts WHERE org_id=? AND conflict_id=?",
            (org_id, conflict_id),
        ).fetchone()

    def _current_open_case(self, conflict_id: str, expected: sqlite3.Row) -> sqlite3.Row:
        current = self._case(conflict_id, expected["org_id"])
        if current is None or current != expected or current["status"] != "open":
            raise DurableConflictEscalationConflict("current open Conflict Case가 아닙니다.")
        return current

    def _awaiting_conflict_request(
        self,
        principal: AuthenticatedPrincipal,
        command: DurableConflictEscalateCommand,
        case: sqlite3.Row,
    ):
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or request.org_id != principal.org_id
            or not isinstance(request.state, AwaitingConflict)
            or request.state.case_id != command.conflict_id
            or request.revision != case["awaiting_revision"] + 1
            or request.revision != command.expected_request_revision
        ):
            raise DurableConflictEscalationConflict(
                "stale Conflict escalation/Request command를 거부합니다."
            )
        return request

    def _cause(
        self, *, org_id: str, conflict_id: str, claims: tuple[ConflictOpenCandidateClaim, ...]
    ) -> SealedEscalationEvidence:
        try:
            cause = self._cause_reader.read_sealed(
                org_id=org_id, conflict_id=conflict_id, claims=claims
            )
        except Exception as error:
            raise DurableConflictEscalationUnavailable(
                "sealed escalation 원인을 읽을 수 없습니다."
            ) from error
        if type(cause) not in (DivergentVotes, CandidateRegistryChanged):
            raise DurableConflictEscalationConflict("sealed escalation 원인이 아닙니다.")
        return cause

    def _graph(
        self, org_id: str, claims: tuple[ConflictOpenCandidateClaim, ...]
    ) -> ConflictEscalationRegistrySnapshot:
        try:
            graph = self._graph_reader.snapshot(org_id=org_id, claims=claims)
        except Exception as error:
            raise DurableConflictEscalationUnavailable(
                "current Conflict escalation Registry graph를 읽을 수 없습니다."
            ) from error
        if type(graph) is not ConflictEscalationRegistrySnapshot:
            raise DurableConflictEscalationConflict("current Conflict escalation graph가 없습니다.")
        return graph

    def _authorize(
        self, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> AuthorizationGrant:
        if self._authorizer is None:
            raise DurableConflictEscalationUnavailable("중앙 conflict.escalate 권한 원천이 없습니다.")
        try:
            grant = self._authorizer.authorize(principal, ESCALATE_ACTION, resource)
        except Exception as error:
            raise DurableConflictEscalationUnavailable(
                "중앙 권한 확인을 수행할 수 없습니다."
            ) from error
        if type(grant) is not AuthorizationGrant or not self._verify(grant, principal, resource):
            raise DurableConflictEscalationConflict("중앙 conflict.escalate 권한이 거부됐습니다.")
        return grant

    def _verify(
        self, grant: AuthorizationGrant, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> bool:
        assert self._authorizer is not None
        try:
            return self._authorizer.verify(grant, principal, ESCALATE_ACTION, resource)
        except Exception:
            return False

    def _stored_result(
        self,
        receipt: sqlite3.Row,
        principal: AuthenticatedPrincipal,
        command: DurableConflictEscalateCommand,
        case: sqlite3.Row,
    ) -> DurableConflictEscalationResult:
        evidence = self._tx.execute(
            "SELECT * FROM durable_conflict_escalation_evidence WHERE receipt_id=?",
            (receipt["receipt_id"],),
        ).fetchone()
        projection = self._tx.execute(
            "SELECT * FROM durable_conflict_escalation_result_projections WHERE receipt_id=?",
            (receipt["receipt_id"],),
        ).fetchone()
        manager_item = self._tx.execute(
            "SELECT * FROM durable_linked_manager_items WHERE request_id=?", (command.request_id,)
        ).fetchone()
        request = self._tx.select_question_request(command.request_id)
        if evidence is None or projection is None or manager_item is None or request is None:
            raise DurableConflictEscalationUnavailable(
                "immutable escalation receipt graph가 없습니다."
            )
        manager_item_ref = manager_item["manager_item_id"]
        if (
            manager_item["org_id"] != principal.org_id
            or manager_item["request_id"] != command.request_id
            or manager_item["awaiting_revision"] != case["awaiting_revision"]
            or manager_item["source_kind"] != "deadlock"
            or manager_item["source_ref"] != _ref("source", command.conflict_id)
            or manager_item["manager_subject_id"] != projection["target_subject_ref"]
        ):
            raise DurableConflictEscalationUnavailable(
                "저장된 FromDeadlock ManagerItem이 receipt graph와 다릅니다."
            )
        if (
            request.org_id != principal.org_id
            or not isinstance(request.state, AwaitingManager)
            or request.state.item_id != manager_item_ref
            or request.state.public_kind != "contested"
            or request.state.route is not None
            or request.state.attempt is not None
            or request.state.handling.kind != "manager_item"
            or request.state.handling.ref != manager_item_ref
            or request.revision != case["awaiting_revision"] + 2
        ):
            raise DurableConflictEscalationUnavailable(
                "저장된 AwaitingManager Request가 receipt graph와 다릅니다."
            )
        if projection["result_kind"] == "escalated_to_manager":
            if (
                evidence["manager_subject_ref"] is None
                or projection["target_subject_ref"] != evidence["manager_subject_ref"]
            ):
                raise DurableConflictEscalationUnavailable(
                    "저장된 escalated_to_manager result가 sealed manager와 다릅니다."
                )
            return DurableConflictEscalatedToManager(
                receipt["receipt_id"],
                command.conflict_id,
                command.request_id,
                manager_item_ref,
                request.revision,
                projection["target_subject_ref"],
            )
        if projection["result_kind"] == "escalated_to_root":
            if (
                evidence["manager_subject_ref"] is not None
                or projection["target_subject_ref"] != evidence["root_subject_ref"]
            ):
                raise DurableConflictEscalationUnavailable(
                    "저장된 escalated_to_root result가 sealed root와 다르거나 manager가 남아있습니다."
                )
            return DurableConflictEscalatedToRoot(
                receipt["receipt_id"],
                command.conflict_id,
                command.request_id,
                manager_item_ref,
                request.revision,
                projection["target_subject_ref"],
            )
        raise DurableConflictEscalationUnavailable("저장된 escalation result kind가 알 수 없습니다.")

    @staticmethod
    def _valid_command(command: DurableConflictEscalateCommand) -> None:
        if (
            any(not value for value in (command.conflict_id, command.request_id))
            or not command.claims
            or type(command.expected_request_revision) is not int
            or command.expected_request_revision < 0
        ):
            raise DurableConflictEscalationUnavailable(
                "typed conflict.escalate command 형식이 올바르지 않습니다."
            )
