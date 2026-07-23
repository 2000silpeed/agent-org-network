"""P17.9 S4.5a durable WorkTicket enqueue Unit of Work (ADR 0042 §5·§8 보강).

recover_ready(S5 runner)가 durable ``ReadyToDispatch``에서 합성한 커맨드 하나로
WorkTicket을 원자 발급하고 Question Request를 ``AwaitingAnswer``로 전이한다.
``work_ticket.create``는 AUTHORITY_ACTION_MANIFEST에 의도적으로 없다 — route가
봉인된 시점(``ReadyToDispatch`` 존재)이 이미 중앙 권한 행사의 증거이므로 이
UoW는 CentralAuthorizer·AuthenticatedPrincipal을 받지 않는 system 전이다(ADR
0042 §8 보강, 2026-07-23 domain-architect 확정). Registry(``resolve_owner_subject``)는
owner 주소 해석 전용이며 eligibility 재판정도 directory lookup도 아니다. None은
Conflict로 닫혀 Request가 ``ReadyToDispatch``에 그대로 남는다(미아 없음 —
recover_ready 재시도로 회복).

digest는 S4.4 교정을 그대로 계승해 command-local만 계산한다(request/Registry
읽기 어느 것도 선행하지 않는다). target_ref는 S4.1 receipt를 그대로 재사용하며
ticket_id를 재hash하지 않는다 — ticket_id가 이미 typed digest reference이기
때문이다(``sqlite_durable_manager_disposition_uow`` item_ref 관례와 동형).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.question_request import (
    AwaitingAnswer,
    HandlingAssignment,
    QuestionRequestTransitionError,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_completion import validate_sqlite_completion_connection
from agent_org_network.sqlite_durable_linked_aggregates import (
    validate_sqlite_durable_linked_aggregates_connection,
)


class DurableWorkTicketError(RuntimeError):
    """Base error deliberately free of Registry internals."""


class DurableWorkTicketConflict(DurableWorkTicketError):
    pass


class DurableWorkTicketUnavailable(DurableWorkTicketError):
    pass


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, raw: str) -> str:
    return f"{kind}:{_sha(raw)}"


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?[+-][0-9]{2}:[0-9]{2}\Z"
)


def _timestamp(value: datetime) -> str:
    rendered = value.isoformat(timespec="microseconds" if value.microsecond else "seconds")
    if _TIMESTAMP_RE.fullmatch(rendered) is None:
        raise DurableWorkTicketConflict("canonical calendar command time이 필요합니다.")
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise DurableWorkTicketConflict("calendar command time이 올바르지 않습니다.") from error
    precision = "microseconds" if parsed.microsecond else "seconds"
    if parsed.utcoffset() is None or parsed.isoformat(timespec=precision) != rendered:
        raise DurableWorkTicketConflict("canonical command time이 필요합니다.")
    return rendered


_ACTION: Final = "work_ticket.create"
_TARGET_KIND: Final = "work_ticket"
_OUTBOX_KIND: Final = "linked_aggregate_outbox"
_SYSTEM_SUBJECT_ID: Final = "system:work_ticket_enqueue"
SYSTEM_SUBJECT_REF: Final = _ref("subject", _SYSTEM_SUBJECT_ID)


@dataclass(frozen=True)
class DurableWorkTicketEnqueueCommand:
    """recover_ready(S5)가 durable ``ReadyToDispatch``에서 합성하는 typed command."""

    request_id: str
    expected_request_revision: int
    attempt: int


@dataclass(frozen=True)
class DurableWorkTicketEnqueued:
    receipt_id: str
    ticket_id: str
    request_id: str
    request_revision: int
    attempt: int


class DurableWorkTicketRegistry(Protocol):
    """Owner 주소 해석 전용 포트 — eligibility 재판정도 directory lookup도 아니다."""

    def resolve_owner_subject(self, *, org_id: str, agent_id: str) -> str | None: ...


def _no_fault(_point: str) -> None:
    return None


def _valid_command(command: DurableWorkTicketEnqueueCommand) -> None:
    if (
        not command.request_id.strip()
        or type(command.expected_request_revision) is not int
        or command.expected_request_revision < 1
        or type(command.attempt) is not int
        or command.attempt < 1
    ):
        raise DurableWorkTicketUnavailable(
            "typed work_ticket.create command 형식이 올바르지 않습니다."
        )


def _enqueue_digest(command: DurableWorkTicketEnqueueCommand) -> str:
    # command-local만(request 읽기·Registry 호출 어느 것도 선행하지 않는다) —
    # S4.4 교정 계승.
    return _sha(
        _json(
            {
                "action": _ACTION,
                "request_id": command.request_id,
                "expected_request_revision": command.expected_request_revision,
                "attempt": command.attempt,
                "by_system_ref": SYSTEM_SUBJECT_REF,
            }
        )
    )


def _route_sha256(route: RouteTarget) -> str:
    return _sha(
        _json(
            {
                "agent_id": route.agent_id,
                "authority_version": route.authority_version,
                "intent": route.intent,
                "requires_approval": route.requires_approval,
            }
        )
    )


class DurableWorkTicketEnqueueUnitOfWork:
    """한 durable ``ReadyToDispatch`` Request를 WorkTicket 발급으로 원자 전이한다."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        registry: DurableWorkTicketRegistry,
        clock: Callable[[], datetime],
        ticket_id_factory: Callable[[], str],
        receipt_id_factory: Callable[[], str],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._completion = completion
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._registry = registry
        self._clock = clock
        self._ticket_id_factory = ticket_id_factory
        self._receipt_id_factory = receipt_id_factory
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        try:
            with self._tx.scope():
                self._tx.validate_component(validate_sqlite_durable_linked_aggregates_connection)
                self._tx.validate_component(validate_sqlite_completion_connection)
        except Exception as error:
            raise DurableWorkTicketUnavailable(
                "durable WorkTicket enqueue capability를 열 수 없습니다."
            ) from error

    def enqueue(self, *, command: DurableWorkTicketEnqueueCommand) -> DurableWorkTicketEnqueued:
        if type(command) is not DurableWorkTicketEnqueueCommand:
            raise DurableWorkTicketUnavailable("typed work_ticket.create command가 필요합니다.")
        _valid_command(command)
        digest = _enqueue_digest(command)
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._tx.validate_component_in_transaction(
                    validate_sqlite_durable_linked_aggregates_connection
                )
                self._tx.validate_component_in_transaction(validate_sqlite_completion_connection)
                receipt = self._tx.execute(
                    "SELECT * FROM durable_linked_command_receipts WHERE command_digest=?",
                    (digest,),
                ).fetchone()
                result = (
                    self._stored_result(receipt, command)
                    if receipt is not None
                    else self._fresh(command, digest)
                )
                self._tx.commit()
                return result
            except Exception:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise

    def _fresh(
        self, command: DurableWorkTicketEnqueueCommand, digest: str
    ) -> DurableWorkTicketEnqueued:
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or not isinstance(request.state, ReadyToDispatch)
            or request.revision != command.expected_request_revision
            or request.state.attempt != command.attempt
        ):
            raise DurableWorkTicketConflict("stale WorkTicket enqueue command를 거부합니다.")
        route = request.state.route
        org_ref = request.org_id
        try:
            owner = self._registry.resolve_owner_subject(org_id=org_ref, agent_id=route.agent_id)
        except Exception as error:
            raise DurableWorkTicketUnavailable(
                "Owner 주소 Registry를 사용할 수 없습니다."
            ) from error
        if owner is None:
            raise DurableWorkTicketConflict("WorkTicket owner 주소를 해석할 수 없습니다.")

        now = self._clock()
        created_at = _timestamp(now)
        ticket_id = self._new_ticket_ref()
        receipt_ref = self._new_receipt_ref()
        route_sha = _route_sha256(route)

        # WRITE 순서(FK receipt → child intents → work_ticket → aggregate 전이)
        self._tx.execute(
            "INSERT INTO durable_linked_command_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_ref,
                org_ref,
                command.request_id,
                digest,
                SYSTEM_SUBJECT_REF,
                _ACTION,
                command.expected_request_revision,
                _TARGET_KIND,
                ticket_id,
                created_at,
            ),
        )
        self._fault("after_receipt")
        self._tx.execute(
            "INSERT INTO durable_linked_audit_intents VALUES(?,?,?,?,?,?)",
            (receipt_ref, org_ref, command.request_id, _ACTION, digest, created_at),
        )
        self._fault("after_audit_intent")
        self._tx.execute(
            "INSERT INTO durable_linked_outbox_intents VALUES(?,?,?,?,?,?)",
            (receipt_ref, org_ref, command.request_id, _OUTBOX_KIND, digest, created_at),
        )
        self._fault("after_outbox_intent")
        self._tx.execute(
            "INSERT INTO durable_linked_work_tickets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ticket_id,
                org_ref,
                command.request_id,
                command.attempt,
                command.expected_request_revision,
                route_sha,
                owner,
                "pending",
                created_at,
            ),
        )
        self._fault("after_work_ticket")

        try:
            updated = request.transition(
                AwaitingAnswer(
                    route=route,
                    attempt=command.attempt,
                    ticket_id=ticket_id,
                    handling=HandlingAssignment(
                        kind="runtime_ticket",
                        ref=ticket_id,
                        due_at=request.state.handling.due_at,
                    ),
                ),
                clock=lambda: now,
            )
        except QuestionRequestTransitionError as error:
            raise DurableWorkTicketConflict(
                "WorkTicket 발급 시점 Request 전이가 유효하지 않습니다(SLA 경과 등)."
            ) from error
        if not self._tx.compare_and_set_question_request(
            request.request_id, request.revision, request, updated
        ):
            raise DurableWorkTicketConflict("commit-time Question Request CAS에 실패했습니다.")
        self._fault("after_request")
        return DurableWorkTicketEnqueued(
            receipt_ref, ticket_id, command.request_id, updated.revision, command.attempt
        )

    def _stored_result(
        self, receipt: sqlite3.Row, command: DurableWorkTicketEnqueueCommand
    ) -> DurableWorkTicketEnqueued:
        if (
            receipt["action"] != _ACTION
            or receipt["target_kind"] != _TARGET_KIND
            or receipt["request_id"] != command.request_id
            or receipt["expected_request_revision"] != command.expected_request_revision
        ):
            raise DurableWorkTicketUnavailable("immutable WorkTicket receipt가 command와 다릅니다.")
        ticket_id = receipt["target_ref"]
        ticket = self._tx.execute(
            "SELECT * FROM durable_linked_work_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if (
            ticket is None
            or ticket["status"] != "pending"
            or ticket["ticket_id"] != receipt["target_ref"]
            or ticket["attempt"] != command.attempt
        ):
            raise DurableWorkTicketUnavailable(
                "immutable WorkTicket receipt/ticket이 서로 다릅니다."
            )
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or not isinstance(request.state, AwaitingAnswer)
            or request.state.ticket_id != receipt["target_ref"]
            or request.state.attempt != command.attempt
            or _route_sha256(request.state.route) != ticket["route_sha256"]
        ):
            raise DurableWorkTicketUnavailable(
                "immutable WorkTicket receipt/Request 결과가 서로 다릅니다."
            )
        return DurableWorkTicketEnqueued(
            receipt["receipt_id"],
            ticket_id,
            command.request_id,
            request.revision,
            command.attempt,
        )

    def _new_ticket_ref(self) -> str:
        ticket_id = self._ticket_id_factory()
        if not ticket_id.strip():
            raise DurableWorkTicketConflict("ticket identity가 올바르지 않습니다.")
        return _ref("ticket", ticket_id)

    def _new_receipt_ref(self) -> str:
        receipt_id = self._receipt_id_factory()
        if not receipt_id.strip():
            raise DurableWorkTicketConflict("receipt identity가 올바르지 않습니다.")
        return _ref("receipt", receipt_id)
