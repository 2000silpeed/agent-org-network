"""P17.9 S4.4 durable Manager 처분 Unit of Work (ADR 0050 §12·ADR 0065 §11).

FromUnowned(S4.4b/c)와 FromDeadlock(S4.4d) 두 source를 이 한 모듈이 ManagerItem의
``source_kind``로 분기해 처리한다. S4.2b(``sqlite_durable_direct_conflict_concurrence``)·
c.3(``sqlite_durable_conflict_escalation_uow``)와 동형이다.

S4.4a는 계약(error·값객체·포트·central_authority manager.act 배선)만 열고,
S4.4b는 Dismiss 경로만 완결한다. Assign(``manager.assign_owner``)은 S4.4c 전까지
이 UoW가 명시적으로 거부한다 — 아직 Registry 결선·digest·write 경로가 없다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

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
from agent_org_network.question_request import AwaitingManager, DeclinedRequest, QuestionRequest
from agent_org_network.sqlite_completion import validate_sqlite_completion_connection
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    validate_sqlite_durable_conflict_escalation_receipts_connection,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    validate_sqlite_durable_linked_aggregates_connection,
)


class DurableManagerDispositionError(RuntimeError):
    """Base error deliberately free of Registry/authority internals."""


class DurableManagerDispositionConflict(DurableManagerDispositionError):
    pass


class DurableManagerDispositionUnavailable(DurableManagerDispositionError):
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
        raise DurableManagerDispositionConflict("canonical calendar command time이 필요합니다.")
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise DurableManagerDispositionConflict(
            "calendar command time이 올바르지 않습니다."
        ) from error
    precision = "microseconds" if parsed.microsecond else "seconds"
    if parsed.utcoffset() is None or parsed.isoformat(timespec=precision) != rendered:
        raise DurableManagerDispositionConflict("canonical command time이 필요합니다.")
    return rendered


_ASSIGN: Final = "manager.assign_owner"
_DISMISS: Final = "manager.dismiss"
_ACTION: Final = "manager.act"


@dataclass(frozen=True)
class DurableManagerAssignCommand:
    """Server-authenticated typed command, not an MCP payload DTO."""

    item_id: str
    request_id: str
    agent_id: str
    expected_request_revision: int
    rationale: str = ""


@dataclass(frozen=True)
class DurableManagerDismissCommand:
    """Server-authenticated typed command, not an MCP payload DTO."""

    item_id: str
    request_id: str
    expected_request_revision: int
    rationale: str = ""


DurableManagerDispositionCommand = DurableManagerAssignCommand | DurableManagerDismissCommand


@dataclass(frozen=True)
class DurableManagerOwnerAssigned:
    receipt_id: str
    item_id: str
    request_id: str
    request_revision: int
    agent_id: str


@dataclass(frozen=True)
class DurableManagerDismissed:
    receipt_id: str
    item_id: str
    request_id: str
    request_revision: int
    reason_code: Literal["manager_declined"] = "manager_declined"


DurableManagerDispositionResult = DurableManagerOwnerAssigned | DurableManagerDismissed


@dataclass(frozen=True)
class DurableManagerAssignTarget:
    agent_id: str
    owner_subject_ref: str
    requires_approval: bool


class DurableManagerRegistry(Protocol):
    def resolve_assign_target(
        self, *, org_id: str, intent: str, agent_id: str
    ) -> DurableManagerAssignTarget | None: ...


def _no_fault(_point: str) -> None:
    return None


class DurableManagerDispositionUnitOfWork:
    """한 durable open ManagerItem을 Assign/Dismiss로 원자 처분한다."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        registry: DurableManagerRegistry,
        central_authorizer: CentralAuthorizer | None,
        clock: Callable[[], datetime],
        receipt_id_factory: Callable[[], str],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._completion = completion
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._registry = registry
        self._authorizer = central_authorizer
        self._clock = clock
        self._receipt_id_factory = receipt_id_factory
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        try:
            with self._tx.scope():
                self._tx.validate_component(validate_sqlite_durable_linked_aggregates_connection)
                self._tx.validate_component(validate_sqlite_completion_connection)
                self._tx.validate_component(
                    validate_sqlite_durable_conflict_escalation_receipts_connection
                )
        except Exception as error:
            raise DurableManagerDispositionUnavailable(
                "durable Manager 처분 capability를 열 수 없습니다."
            ) from error

    def act(
        self,
        *,
        principal: AuthenticatedPrincipal,
        command: DurableManagerDispositionCommand,
    ) -> DurableManagerDispositionResult:
        if type(principal) is not AuthenticatedPrincipal or type(command) not in (
            DurableManagerAssignCommand,
            DurableManagerDismissCommand,
        ):
            raise DurableManagerDispositionUnavailable(
                "서버 principal과 exact manager.act command가 필요합니다."
            )
        if isinstance(command, DurableManagerAssignCommand):
            # S4.4c 전까지 Assign은 명시 거부한다 — Registry 결선·digest·write 경로가 없다.
            raise DurableManagerDispositionUnavailable(
                "manager.assign_owner는 아직 열리지 않았습니다(S4.4c 전)."
            )
        self._valid_dismiss_command(command)
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                # Replay is not a privilege escalation: every companion capability
                # is reread inside this write snapshot, never trusted from open time.
                self._tx.validate_component_in_transaction(
                    validate_sqlite_durable_linked_aggregates_connection
                )
                self._tx.validate_component_in_transaction(validate_sqlite_completion_connection)
                self._tx.validate_component_in_transaction(
                    validate_sqlite_durable_conflict_escalation_receipts_connection
                )
                item = self._item(command.item_id, principal.org_id)
                source_kind = item["source_kind"]
                if source_kind == "dispatch":
                    raise DurableManagerDispositionUnavailable(
                        "FromDispatch ManagerItem은 아직 manager.act 대상이 아닙니다."
                    )
                if source_kind not in ("unowned", "deadlock"):
                    raise DurableManagerDispositionConflict(
                        "알 수 없는 ManagerItem source입니다."
                    )
                actor_ref = _ref("subject", principal.subject_id)
                if actor_ref != item["manager_subject_id"]:
                    raise DurableManagerDispositionConflict(
                        "principal이 durable ManagerItem의 Manager가 아닙니다."
                    )
                digest = self._dismiss_digest(command, principal, actor_ref, source_kind)
                receipt = self._tx.execute(
                    "SELECT * FROM durable_linked_command_receipts WHERE command_digest=?",
                    (digest,),
                ).fetchone()
                if receipt is not None:
                    result = self._stored_dismiss_result(receipt, command, item)
                    self._tx.commit()
                    return result

                # FRESH
                if item["status"] != "open":
                    raise DurableManagerDispositionConflict("current open ManagerItem이 아닙니다.")
                request = self._current_awaiting_manager_request(command, item, source_kind)
                if source_kind == "deadlock":
                    self._verify_deadlock_evidence(principal.org_id, command.request_id, item)
                resource = ResourceRef(
                    org_id=principal.org_id,
                    kind="manager_item",
                    resource_id=command.item_id,
                    owner_subject_id=principal.subject_id,
                )
                self._authorize(principal, resource)

                now = self._clock()
                created_at = _timestamp(now)
                receipt_id = self._receipt_id_factory()
                if not receipt_id.strip():
                    raise DurableManagerDispositionConflict("receipt identity가 올바르지 않습니다.")
                receipt_ref = _ref("receipt", receipt_id)
                target_ref = _ref("manager", command.item_id)

                # WRITE 순서(FK receipt → child intents → aggregate 전이)
                self._tx.execute(
                    "INSERT INTO durable_linked_command_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_ref,
                        principal.org_id,
                        command.request_id,
                        digest,
                        actor_ref,
                        _DISMISS,
                        command.expected_request_revision,
                        "manager_item",
                        target_ref,
                        created_at,
                    ),
                )
                self._fault("after_receipt")
                self._tx.execute(
                    "INSERT INTO durable_linked_audit_intents VALUES(?,?,?,?,?,?)",
                    (
                        receipt_ref,
                        principal.org_id,
                        command.request_id,
                        _DISMISS,
                        digest,
                        created_at,
                    ),
                )
                self._fault("after_audit_intent")
                self._tx.execute(
                    "INSERT INTO durable_linked_outbox_intents VALUES(?,?,?,?,?,?)",
                    (
                        receipt_ref,
                        principal.org_id,
                        command.request_id,
                        "linked_aggregate_outbox",
                        digest,
                        created_at,
                    ),
                )
                self._fault("after_outbox_intent")
                if (
                    self._tx.execute(
                        "UPDATE durable_linked_manager_items SET status='dismissed' WHERE manager_item_id=? AND status='open'",
                        (command.item_id,),
                    ).rowcount
                    != 1
                ):
                    raise DurableManagerDispositionConflict(
                        "commit-time ManagerItem CAS에 실패했습니다."
                    )
                self._fault("after_manager_item")
                updated = request.transition(
                    DeclinedRequest(reason_code="manager_declined"), clock=lambda: now
                )
                if not self._tx.compare_and_set_question_request(
                    request.request_id, request.revision, request, updated
                ):
                    raise DurableManagerDispositionConflict(
                        "commit-time Question Request CAS에 실패했습니다."
                    )
                self._fault("after_request")
                result: DurableManagerDispositionResult = DurableManagerDismissed(
                    receipt_ref, command.item_id, command.request_id, updated.revision
                )
                self._tx.commit()
                return result
            except Exception:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise

    def _item(self, item_id: str, org_id: str) -> sqlite3.Row:
        row = self._tx.execute(
            "SELECT * FROM durable_linked_manager_items WHERE manager_item_id=? AND org_id=?",
            (item_id, org_id),
        ).fetchone()
        if row is None:
            raise DurableManagerDispositionConflict("durable ManagerItem이 없습니다.")
        return row

    def _current_awaiting_manager_request(
        self,
        command: DurableManagerDismissCommand,
        item: sqlite3.Row,
        source_kind: str,
    ) -> QuestionRequest:
        expected_public_kind = "unowned" if source_kind == "unowned" else "contested"
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or request.org_id != item["org_id"]
            or not isinstance(request.state, AwaitingManager)
            or request.state.public_kind != expected_public_kind
            or request.state.item_id != command.item_id
            or request.revision != command.expected_request_revision
        ):
            raise DurableManagerDispositionConflict(
                "stale Manager 처분/Request command를 거부합니다."
            )
        return request

    def _verify_deadlock_evidence(self, org_id: str, request_id: str, item: sqlite3.Row) -> None:
        # ③ FromDeadlock evidence proof = committed escalation receipt(c.2)의
        # read-only 결박(새 HITL 0). Case는 읽지도 쓰지도 않는다(ADR 0065 §11).
        esc = self._tx.execute(
            "SELECT conflict_id,awaiting_revision FROM durable_conflict_escalation_receipts WHERE org_id=? AND request_id=?",
            (org_id, request_id),
        ).fetchone()
        if (
            esc is None
            or _ref("source", esc["conflict_id"]) != item["source_ref"]
            or esc["awaiting_revision"] != item["awaiting_revision"]
        ):
            raise DurableManagerDispositionConflict("FromDeadlock evidence 결박에 실패했습니다.")

    def _authorize(
        self, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> AuthorizationGrant:
        if self._authorizer is None:
            raise DurableManagerDispositionUnavailable("중앙 manager.act 권한 원천이 없습니다.")
        try:
            grant = self._authorizer.authorize(principal, _ACTION, resource)
        except Exception as error:
            raise DurableManagerDispositionUnavailable(
                "중앙 권한 확인을 수행할 수 없습니다."
            ) from error
        if type(grant) is not AuthorizationGrant or not self._verify(grant, principal, resource):
            raise DurableManagerDispositionConflict("중앙 manager.act 권한이 거부됐습니다.")
        return grant

    def _verify(
        self, grant: AuthorizationGrant, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> bool:
        assert self._authorizer is not None
        try:
            return self._authorizer.verify(grant, principal, _ACTION, resource)
        except Exception:
            return False

    def _stored_dismiss_result(
        self,
        receipt: sqlite3.Row,
        command: DurableManagerDismissCommand,
        item: sqlite3.Row,
    ) -> DurableManagerDismissed:
        expected_target_ref = _ref("manager", command.item_id)
        if (
            receipt["action"] != _DISMISS
            or receipt["target_kind"] != "manager_item"
            or receipt["target_ref"] != expected_target_ref
            or receipt["request_id"] != command.request_id
            or receipt["expected_request_revision"] != command.expected_request_revision
            or item["status"] != "dismissed"
        ):
            raise DurableManagerDispositionUnavailable(
                "immutable Dismiss receipt/ManagerItem이 서로 다릅니다."
            )
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or not isinstance(request.state, DeclinedRequest)
            or request.state.reason_code != "manager_declined"
        ):
            raise DurableManagerDispositionUnavailable(
                "immutable Dismiss Request 결과가 receipt와 다릅니다."
            )
        return DurableManagerDismissed(
            receipt["receipt_id"], command.item_id, command.request_id, request.revision
        )

    def _dismiss_digest(
        self,
        command: DurableManagerDismissCommand,
        principal: AuthenticatedPrincipal,
        actor_ref: str,
        source_kind: str,
    ) -> str:
        return _sha(
            _json(
                {
                    "action": _DISMISS,
                    "org_id": principal.org_id,
                    "request_id": command.request_id,
                    "item_ref": _ref("manager", command.item_id),
                    "by_manager_ref": actor_ref,
                    "reason_code": "manager_declined",
                    "rationale_sha256": _sha(command.rationale),
                    "expected_request_revision": command.expected_request_revision,
                    "source_kind": source_kind,
                }
            )
        )

    @staticmethod
    def _valid_dismiss_command(command: DurableManagerDismissCommand) -> None:
        if (
            any(not value for value in (command.item_id, command.request_id))
            or type(command.expected_request_revision) is not int
            or command.expected_request_revision < 0
            or type(command.rationale) is not str
        ):
            raise DurableManagerDispositionUnavailable(
                "typed manager.dismiss command 형식이 올바르지 않습니다."
            )
