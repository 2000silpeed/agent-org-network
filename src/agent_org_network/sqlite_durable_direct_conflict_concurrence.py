"""P17.9 S4.2b durable direct Conflict concurrence transaction.

The evidence schema is intentionally owned by ``sqlite_durable_direct_conflict_uow``.
This module only activates its one command, through Completion's shared SQLite
transaction.  Raw Registry values exist only long enough to verify the current
candidate set and calculate its digest; no raw Card, Owner, domain, question or
authority material is written to SQLite.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, cast

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
from agent_org_network.question_request import (
    AwaitingConflict,
    HandlingAssignment,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_durable_direct_conflict_uow import (
    validate_sqlite_durable_direct_conflict_uow_connection,
)


class DurableDirectConflictConcurrenceError(RuntimeError):
    """Base error deliberately free of Registry/authority internals."""


class DurableDirectConflictConcurrenceConflict(DurableDirectConflictConcurrenceError):
    pass


class DurableDirectConflictConcurrenceUnavailable(DurableDirectConflictConcurrenceError):
    pass


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, raw: str) -> str:
    return f"{kind}:{_sha(raw)}"


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _timestamp(value: datetime) -> str:
    rendered = value.isoformat(timespec="microseconds" if value.microsecond else "seconds")
    if _TIMESTAMP_RE.fullmatch(rendered) is None:
        raise DurableDirectConflictConcurrenceConflict(
            "canonical calendar command time이 필요합니다."
        )
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise DurableDirectConflictConcurrenceConflict(
            "calendar command time이 올바르지 않습니다."
        ) from error
    precision = "microseconds" if parsed.microsecond else "seconds"
    if parsed.utcoffset() is None or parsed.isoformat(timespec=precision) != rendered:
        raise DurableDirectConflictConcurrenceConflict("canonical command time이 필요합니다.")
    return rendered


_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?[+-][0-9]{2}:[0-9]{2}\Z"
)


@dataclass(frozen=True)
class DurableConflictCandidate:
    """Current Registry-only candidate; none of these raw values are persisted."""

    card_id: str
    owner_subject_id: str
    domain: str
    route: RouteTarget


class DurableConflictRegistry(Protocol):
    def candidates(
        self, *, org_id: str, conflict_id: str
    ) -> Sequence[DurableConflictCandidate]: ...


@dataclass(frozen=True)
class DurableConflictConcurCommand:
    """Server-authenticated typed command, not an MCP payload DTO."""

    conflict_id: str
    request_id: str
    selected_card_id: str
    expected_request_revision: int


@dataclass(frozen=True)
class DurableConflictConcurrencePending:
    receipt_id: str
    conflict_id: str
    request_id: str
    accepted_vote_count: int


@dataclass(frozen=True)
class DurableConflictConcurrenceResolved:
    receipt_id: str
    conflict_id: str
    request_id: str
    request_revision: int
    selected_card_id: str


DurableConflictConcurrenceResult = (
    DurableConflictConcurrencePending | DurableConflictConcurrenceResolved
)


_ACTION: Final = "conflict.concur"


def _no_fault(_point: str) -> None:
    return None


class DurableDirectConflictConcurrenceUnitOfWork:
    """One Owner vote and, only when unanimous, the Case/Request transition."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        registry: DurableConflictRegistry,
        central_authorizer: CentralAuthorizer | None,
        clock: Callable[[], datetime],
        receipt_id_factory: Callable[[], str],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._completion = completion
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._registry, self._authorizer = registry, central_authorizer
        self._clock, self._receipt_id_factory = clock, receipt_id_factory
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        try:
            with self._tx.scope():
                self._tx.validate_component(validate_sqlite_durable_direct_conflict_uow_connection)
        except Exception as error:
            raise DurableDirectConflictConcurrenceUnavailable(
                "durable direct Conflict capability를 열 수 없습니다."
            ) from error

    def concur(
        self, *, principal: AuthenticatedPrincipal, command: DurableConflictConcurCommand
    ) -> DurableConflictConcurrenceResult:
        if (
            type(principal) is not AuthenticatedPrincipal
            or type(command) is not DurableConflictConcurCommand
        ):
            raise DurableDirectConflictConcurrenceUnavailable(
                "서버 principal과 exact conflict.concur command가 필요합니다."
            )
        self._valid_command(command)
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                # Replay is not a privilege escalation: validate every companion
                # graph, live Registry relation and current central authority first.
                self._tx.validate_component_in_transaction(
                    validate_sqlite_durable_direct_conflict_uow_connection
                )
                case = self._case(command.conflict_id)
                self._same_scope(principal, command, case)
                candidates, candidate_hash = self._current_candidates(principal, command, case)
                resource = ResourceRef(
                    org_id=principal.org_id,
                    kind="conflict_case",
                    resource_id=command.conflict_id,
                    owner_subject_id=principal.subject_id,
                )
                self._authorize(principal, resource)
                actor_ref, target_ref = (
                    _ref("subject", principal.subject_id),
                    _ref("card", command.selected_card_id),
                )
                receipt = self._receipt_for_owner(
                    command.conflict_id, actor_ref, case["awaiting_revision"]
                )
                digest = self._digest(
                    command, principal, actor_ref, target_ref, candidate_hash, len(candidates)
                )
                if receipt is not None:
                    if receipt["command_digest"] != digest:
                        raise DurableDirectConflictConcurrenceConflict(
                            "같은 Owner의 다른 concurrence command는 replay할 수 없습니다."
                        )
                    result = self._stored_result(receipt, command, case, candidates)
                    self._tx.commit()
                    return result
                request = self._current_request(command, case)
                # Linearization point: current Case/request/Registry/authority are
                # all reread immediately before the first write.
                case = self._current_open_case(command.conflict_id, case)
                candidates, candidate_hash = self._current_candidates(principal, command, case)
                request = self._current_request(command, case)
                self._authorize(principal, resource)
                digest = self._digest(
                    command, principal, actor_ref, target_ref, candidate_hash, len(candidates)
                )
                now = self._clock()
                created_at = _timestamp(now)
                receipt_id = self._receipt_id_factory()
                if not receipt_id.strip():
                    raise DurableDirectConflictConcurrenceConflict(
                        "receipt identity가 올바르지 않습니다."
                    )
                receipt_ref = _ref("receipt", receipt_id)
                votes = self._tx.execute(
                    "SELECT owner_subject_ref,target_card_ref FROM durable_direct_conflict_votes WHERE conflict_id=? AND concurrence_round=?",
                    (command.conflict_id, case["awaiting_revision"] + 1),
                ).fetchall()
                # The S4.1 Case stores request Awaiting revision; direct vote round
                # starts at one and follows that snapshot without mutable legacy state.
                round_ = case["awaiting_revision"] + 1
                if any(row["owner_subject_ref"] == actor_ref for row in votes):
                    raise DurableDirectConflictConcurrenceConflict(
                        "같은 Owner vote가 이미 있습니다."
                    )
                all_votes = [
                    *votes,
                    {"owner_subject_ref": actor_ref, "target_card_ref": target_ref},
                ]
                owners = {_ref("subject", candidate.owner_subject_id) for candidate in candidates}
                if {cast(str, row["owner_subject_ref"]) for row in all_votes} - owners:
                    raise DurableDirectConflictConcurrenceConflict(
                        "현재 후보 밖 Owner vote가 있습니다."
                    )
                unanimous = len(all_votes) == len(owners) and all(
                    cast(str, row["target_card_ref"]) == target_ref for row in all_votes
                )
                result_kind = "consensus_ready" if unanimous else "vote_recorded"
                self._tx.execute(
                    "INSERT INTO durable_direct_conflict_votes VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        command.conflict_id,
                        principal.org_id,
                        command.request_id,
                        round_,
                        actor_ref,
                        target_ref,
                        candidate_hash,
                        len(candidates),
                        receipt_ref,
                        created_at,
                    ),
                )
                self._fault("after_vote")
                self._tx.execute(
                    "INSERT INTO durable_direct_conflict_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_ref,
                        principal.org_id,
                        command.request_id,
                        command.conflict_id,
                        round_,
                        digest,
                        actor_ref,
                        actor_ref,
                        target_ref,
                        candidate_hash,
                        len(candidates),
                        _ACTION,
                        command.expected_request_revision,
                        created_at,
                    ),
                )
                for table in (
                    "durable_direct_conflict_audit_intents",
                    "durable_direct_conflict_outbox_intents",
                ):
                    self._tx.execute(
                        f"INSERT INTO {table} VALUES(?,?,?,?,?,?)",
                        (
                            receipt_ref,
                            principal.org_id,
                            command.request_id,
                            _ACTION,
                            digest,
                            created_at,
                        ),
                    )
                self._tx.execute(
                    "INSERT INTO durable_direct_conflict_result_projections VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_ref,
                        principal.org_id,
                        command.request_id,
                        command.conflict_id,
                        round_,
                        result_kind,
                        actor_ref,
                        target_ref,
                        candidate_hash,
                        len(candidates),
                        len(all_votes) if unanimous else 1,
                        created_at,
                    ),
                )
                self._fault("after_receipt_graph")
                if unanimous:
                    if (
                        self._tx.execute(
                            "UPDATE durable_linked_conflict_cases SET status='resolved' WHERE conflict_id=? AND status='open' AND awaiting_revision=?",
                            (command.conflict_id, case["awaiting_revision"]),
                        ).rowcount
                        != 1
                    ):
                        raise DurableDirectConflictConcurrenceConflict(
                            "commit-time Conflict Case CAS에 실패했습니다."
                        )
                    selected = next(
                        candidate
                        for candidate in candidates
                        if candidate.card_id == command.selected_card_id
                    )
                    assert isinstance(request.state, AwaitingConflict)
                    ready = ReadyToDispatch(
                        route=selected.route,
                        attempt=1,
                        trigger_key=receipt_ref,
                        handling=HandlingAssignment(
                            kind="system", ref=receipt_ref, due_at=request.state.handling.due_at
                        ),
                    )
                    updated = request.transition(ready, clock=lambda: now)
                    if not self._tx.compare_and_set_question_request(
                        request.request_id, request.revision, request, updated
                    ):
                        raise DurableDirectConflictConcurrenceConflict(
                            "commit-time Question Request CAS에 실패했습니다."
                        )
                    self._fault("after_case_request")
                    result: DurableConflictConcurrenceResult = DurableConflictConcurrenceResolved(
                        receipt_ref,
                        command.conflict_id,
                        command.request_id,
                        updated.revision,
                        command.selected_card_id,
                    )
                else:
                    result = DurableConflictConcurrencePending(
                        receipt_ref, command.conflict_id, command.request_id, 1
                    )
                self._tx.commit()
                return result
            except Exception:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise

    def _case(self, conflict_id: str) -> sqlite3.Row:
        row = self._tx.execute(
            "SELECT * FROM durable_linked_conflict_cases WHERE conflict_id=?", (conflict_id,)
        ).fetchone()
        if row is None:
            raise DurableDirectConflictConcurrenceConflict("durable Conflict Case가 없습니다.")
        return row

    def _same_scope(
        self,
        principal: AuthenticatedPrincipal,
        command: DurableConflictConcurCommand,
        case: sqlite3.Row,
    ) -> None:
        if case["org_id"] != principal.org_id or case["request_id"] != command.request_id:
            raise DurableDirectConflictConcurrenceConflict(
                "principal/command/Conflict Case 조직 또는 request가 다릅니다."
            )

    def _current_open_case(self, conflict_id: str, expected: sqlite3.Row) -> sqlite3.Row:
        current = self._case(conflict_id)
        if current != expected or current["status"] != "open":
            raise DurableDirectConflictConcurrenceConflict("current open Conflict Case가 아닙니다.")
        return current

    def _current_request(self, command: DurableConflictConcurCommand, case: sqlite3.Row):
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or request.org_id != case["org_id"]
            or not isinstance(request.state, AwaitingConflict)
        ):
            raise DurableDirectConflictConcurrenceConflict(
                "current AwaitingConflict Request가 아닙니다."
            )
        if (
            request.state.case_id != command.conflict_id
            or request.revision != command.expected_request_revision
            or request.revision != case["awaiting_revision"]
        ):
            raise DurableDirectConflictConcurrenceConflict(
                "stale Conflict/Request command를 거부합니다."
            )
        return request

    def _current_candidates(
        self,
        principal: AuthenticatedPrincipal,
        command: DurableConflictConcurCommand,
        case: sqlite3.Row,
    ) -> tuple[tuple[DurableConflictCandidate, ...], str]:
        try:
            candidates = tuple(
                self._registry.candidates(org_id=principal.org_id, conflict_id=command.conflict_id)
            )
        except Exception as error:
            raise DurableDirectConflictConcurrenceUnavailable(
                "현재 Conflict Registry를 읽을 수 없습니다."
            ) from error
        if not candidates or any(
            type(candidate) is not DurableConflictCandidate for candidate in candidates
        ):
            raise DurableDirectConflictConcurrenceConflict(
                "current ordered Conflict 후보가 필요합니다."
            )
        if len({candidate.card_id for candidate in candidates}) != len(candidates) or len(
            {candidate.owner_subject_id for candidate in candidates}
        ) != len(candidates):
            raise DurableDirectConflictConcurrenceConflict(
                "현재 후보 Card/Owner는 각각 유일해야 합니다."
            )
        if command.selected_card_id not in {
            candidate.card_id for candidate in candidates
        } or principal.subject_id not in {candidate.owner_subject_id for candidate in candidates}:
            raise DurableDirectConflictConcurrenceConflict(
                "actor 또는 selected Card가 현재 후보에 없습니다."
            )
        candidate_hash = _sha(
            _json(
                [
                    {
                        "card": _ref("card", c.card_id),
                        "owner": _ref("subject", c.owner_subject_id),
                        "domain": _sha(c.domain),
                        "route": c.route.model_dump(mode="json"),
                    }
                    for c in candidates
                ]
            )
        )
        if candidate_hash != case["candidate_set_sha256"]:
            raise DurableDirectConflictConcurrenceConflict(
                "current ordered candidate set이 durable Conflict Case와 다릅니다."
            )
        return candidates, candidate_hash

    def _authorize(
        self, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> AuthorizationGrant:
        if self._authorizer is None:
            raise DurableDirectConflictConcurrenceUnavailable(
                "중앙 conflict.concur 권한 원천이 없습니다."
            )
        try:
            grant = self._authorizer.authorize(principal, _ACTION, resource)
        except Exception as error:
            raise DurableDirectConflictConcurrenceUnavailable(
                "중앙 권한 확인을 수행할 수 없습니다."
            ) from error
        if type(grant) is not AuthorizationGrant or not self._verify(grant, principal, resource):
            raise DurableDirectConflictConcurrenceConflict(
                "중앙 conflict.concur 권한이 거부됐습니다."
            )
        return grant

    def _verify(
        self, grant: AuthorizationGrant, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> bool:
        assert self._authorizer is not None
        try:
            return self._authorizer.verify(grant, principal, _ACTION, resource)
        except Exception:
            return False

    def _receipt_for_owner(
        self, conflict_id: str, owner_ref: str, round_: int
    ) -> sqlite3.Row | None:
        return self._tx.execute(
            "SELECT * FROM durable_direct_conflict_receipts WHERE conflict_id=? AND owner_subject_ref=? AND concurrence_round=?",
            (conflict_id, owner_ref, round_ + 1),
        ).fetchone()

    def _stored_result(
        self,
        row: sqlite3.Row,
        command: DurableConflictConcurCommand,
        case: sqlite3.Row,
        candidates: tuple[DurableConflictCandidate, ...],
    ) -> DurableConflictConcurrenceResult:
        projection = self._tx.execute(
            "SELECT * FROM durable_direct_conflict_result_projections WHERE receipt_id=?",
            (row["receipt_id"],),
        ).fetchone()
        if projection is None:
            raise DurableDirectConflictConcurrenceUnavailable(
                "immutable concurrence result가 없습니다."
            )
        if projection["result_kind"] == "consensus_ready":
            current = self._tx.select_question_request(command.request_id)
            selected = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.card_id == command.selected_card_id
                ),
                None,
            )
            if (
                selected is None
                or case["status"] != "resolved"
                or row["concurrence_round"] != case["awaiting_revision"] + 1
                or row["expected_request_revision"] != case["awaiting_revision"]
                or projection["target_card_ref"] != _ref("card", selected.card_id)
                or current is None
                or current.org_id != case["org_id"]
                or current.revision != case["awaiting_revision"] + 1
                or not isinstance(current.state, ReadyToDispatch)
                or current.state.route != selected.route
                or current.state.attempt != 1
                or current.state.trigger_key != row["receipt_id"]
                or current.state.handling.kind != "system"
                or current.state.handling.ref != row["receipt_id"]
            ):
                raise DurableDirectConflictConcurrenceUnavailable(
                    "resolved receipt/Conflict Case/current Registry/Request 인과 상태가 다릅니다."
                )
            return DurableConflictConcurrenceResolved(
                row["receipt_id"],
                command.conflict_id,
                command.request_id,
                current.revision,
                command.selected_card_id,
            )
        return DurableConflictConcurrencePending(
            row["receipt_id"],
            command.conflict_id,
            command.request_id,
            projection["accepted_vote_count"],
        )

    def _digest(
        self,
        command: DurableConflictConcurCommand,
        principal: AuthenticatedPrincipal,
        actor_ref: str,
        target_ref: str,
        candidate_hash: str,
        count: int,
    ) -> str:
        return _sha(
            _json(
                {
                    "org_id": principal.org_id,
                    "request_id": command.request_id,
                    "conflict_id": command.conflict_id,
                    "concurrence_round": command.expected_request_revision + 1,
                    "actor_subject_ref": actor_ref,
                    "owner_subject_ref": actor_ref,
                    "target_card_ref": target_ref,
                    "candidate_set_sha256": candidate_hash,
                    "candidate_owner_count": count,
                    "action": _ACTION,
                    "expected_request_revision": command.expected_request_revision,
                }
            )
        )

    @staticmethod
    def _valid_command(command: DurableConflictConcurCommand) -> None:
        if (
            any(
                not value
                for value in (command.conflict_id, command.request_id, command.selected_card_id)
            )
            or type(command.expected_request_revision) is not int
            or command.expected_request_revision < 0
        ):
            raise DurableDirectConflictConcurrenceConflict(
                "typed concurrence command 형식이 올바르지 않습니다."
            )
