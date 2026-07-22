"""P17.9 S4.4 durable Manager 처분 Unit of Work (ADR 0050 §12·ADR 0065 §11).

FromUnowned(S4.4b/c)와 FromDeadlock(S4.4d) 두 source를 이 한 모듈이 ManagerItem의
``source_kind``로 분기해 처리한다. S4.2b(``sqlite_durable_direct_conflict_concurrence``)·
c.3(``sqlite_durable_conflict_escalation_uow``)와 동형이다.

Assign·Dismiss 둘 다 command_digest는 command+principal의 **로컬 값만**으로
계산한다(request 읽기·중앙 authorize·registry 호출 어느 것도 선행하지 않음) —
c.3 escalate digest·ADR 0045 InMemory fingerprint와 같은 결이다. Assign의
``requires_approval``·``authority_version``·``intent``는 grant/registry
파생값이라 digest에서 제외한다(2026-07-22 domain-architect 교정) — 넣으면
digest 계산이 request 읽기·중앙 authorize·registry 호출보다 먼저 와야 하는
§6 순서와, replay가 그 호출들을 재실행하지 않아야 하는 §8 요구가 동시에
성립할 수 없다. 그 값들은 fresh 경로에서 저장 ``ReadyToDispatch.route``에
결박되고, replay는 그 저장 route를 그대로 읽어 재구성한다(재계산·
재인가·registry 호출 0). intent 결박은 fresh 경로의
``registry.resolve_assign_target(request.intent, agent_id)``가 맡는다 —
request_id가 이미 digest에 있어 intent(같은 request면 불변)는 redundant다.

target_ref(receipt 컬럼)·digest의 ``item_ref``는 ``command.item_id``를 그대로
쓴다(재hash 금지) — item_id가 이미 durable ``manager_item_id`` PK와 같은
``manager:<sha>`` typed ref이기 때문이다. 다시 해시하면 어떤 manager_item_id와도
안 맞아 S4.6 receipt↔item reconciliation join이 깨진다.
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
from agent_org_network.question_request import (
    AwaitingManager,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
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
            self._valid_assign_command(command)
        else:
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
                result: DurableManagerDispositionResult
                if isinstance(command, DurableManagerAssignCommand):
                    result = self._assign(principal, command, item, source_kind, actor_ref)
                else:
                    result = self._dismiss(principal, command, item, source_kind, actor_ref)
                self._tx.commit()
                return result
            except Exception:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise

    def _dismiss(
        self,
        principal: AuthenticatedPrincipal,
        command: DurableManagerDismissCommand,
        item: sqlite3.Row,
        source_kind: str,
        actor_ref: str,
    ) -> DurableManagerDismissed:
        digest = self._dismiss_digest(command, principal, actor_ref, source_kind)
        receipt = self._tx.execute(
            "SELECT * FROM durable_linked_command_receipts WHERE command_digest=?",
            (digest,),
        ).fetchone()
        if receipt is not None:
            return self._stored_dismiss_result(receipt, command, item)

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
        receipt_ref = self._new_receipt_ref()
        target_ref = command.item_id

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
            raise DurableManagerDispositionConflict("commit-time ManagerItem CAS에 실패했습니다.")
        self._fault("after_manager_item")
        updated = request.transition(
            DeclinedRequest(reason_code="manager_declined"), clock=lambda: now
        )
        if not self._tx.compare_and_set_question_request(
            request.request_id, request.revision, request, updated
        ):
            raise DurableManagerDispositionConflict("commit-time Question Request CAS에 실패했습니다.")
        self._fault("after_request")
        return DurableManagerDismissed(
            receipt_ref, command.item_id, command.request_id, updated.revision
        )

    def _assign(
        self,
        principal: AuthenticatedPrincipal,
        command: DurableManagerAssignCommand,
        item: sqlite3.Row,
        source_kind: str,
        actor_ref: str,
    ) -> DurableManagerOwnerAssigned:
        digest = self._assign_digest(command, principal, actor_ref, source_kind)
        receipt = self._tx.execute(
            "SELECT * FROM durable_linked_command_receipts WHERE command_digest=?",
            (digest,),
        ).fetchone()
        if receipt is not None:
            return self._stored_assign_result(receipt, command, item)

        # FRESH
        if item["status"] != "open":
            raise DurableManagerDispositionConflict("current open ManagerItem이 아닙니다.")
        request = self._current_awaiting_manager_request(command, item, source_kind)
        assert isinstance(request.state, AwaitingManager)
        awaiting_manager_state = request.state
        intent = request.intent
        if not intent:
            raise DurableManagerDispositionConflict(
                "intent 없이는 manager.assign_owner를 배정할 수 없습니다(fail-closed)."
            )
        if source_kind == "deadlock":
            self._verify_deadlock_evidence(principal.org_id, command.request_id, item)
        resource = ResourceRef(
            org_id=principal.org_id,
            kind="manager_item",
            resource_id=command.item_id,
            owner_subject_id=principal.subject_id,
        )
        grant = self._authorize(principal, resource)
        target = self._registry.resolve_assign_target(
            org_id=principal.org_id, intent=intent, agent_id=command.agent_id
        )
        if target is None:
            raise DurableManagerDispositionConflict("Assign 대상 Registry 결선이 없습니다.")
        if target.agent_id != command.agent_id:
            raise DurableManagerDispositionConflict(
                "Registry가 command와 다른 Agent Card를 돌려줬습니다."
            )
        route = RouteTarget(
            intent=intent,
            agent_id=target.agent_id,
            requires_approval=target.requires_approval,
            authority_version=grant.policy_version,
        )

        now = self._clock()
        created_at = _timestamp(now)
        receipt_ref = self._new_receipt_ref()
        target_ref = command.item_id

        # WRITE 순서(FK receipt → child intents → aggregate 전이)
        self._tx.execute(
            "INSERT INTO durable_linked_command_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_ref,
                principal.org_id,
                command.request_id,
                digest,
                actor_ref,
                _ASSIGN,
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
                _ASSIGN,
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
                "UPDATE durable_linked_manager_items SET status='resolved' WHERE manager_item_id=? AND status='open'",
                (command.item_id,),
            ).rowcount
            != 1
        ):
            raise DurableManagerDispositionConflict("commit-time ManagerItem CAS에 실패했습니다.")
        self._fault("after_manager_item")
        updated = request.transition(
            ReadyToDispatch(
                route=route,
                attempt=1,
                trigger_key=receipt_ref,
                handling=HandlingAssignment(
                    kind="system", ref=receipt_ref, due_at=awaiting_manager_state.handling.due_at
                ),
            ),
            clock=lambda: now,
        )
        if not self._tx.compare_and_set_question_request(
            request.request_id, request.revision, request, updated
        ):
            raise DurableManagerDispositionConflict("commit-time Question Request CAS에 실패했습니다.")
        self._fault("after_request")
        return DurableManagerOwnerAssigned(
            receipt_ref, command.item_id, command.request_id, updated.revision, target.agent_id
        )

    def _new_receipt_ref(self) -> str:
        receipt_id = self._receipt_id_factory()
        if not receipt_id.strip():
            raise DurableManagerDispositionConflict("receipt identity가 올바르지 않습니다.")
        return _ref("receipt", receipt_id)

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
        command: DurableManagerDispositionCommand,
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
        if (
            receipt["action"] != _DISMISS
            or receipt["target_kind"] != "manager_item"
            or receipt["target_ref"] != command.item_id
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

    def _stored_assign_result(
        self,
        receipt: sqlite3.Row,
        command: DurableManagerAssignCommand,
        item: sqlite3.Row,
    ) -> DurableManagerOwnerAssigned:
        if (
            receipt["action"] != _ASSIGN
            or receipt["target_kind"] != "manager_item"
            or receipt["target_ref"] != command.item_id
            or receipt["request_id"] != command.request_id
            or receipt["expected_request_revision"] != command.expected_request_revision
            or item["status"] != "resolved"
        ):
            raise DurableManagerDispositionUnavailable(
                "immutable Assign receipt/ManagerItem이 서로 다릅니다."
            )
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or not isinstance(request.state, ReadyToDispatch)
            or request.state.attempt != 1
            or request.state.trigger_key != receipt["receipt_id"]
            or request.state.handling.kind != "system"
            or request.state.handling.ref != receipt["receipt_id"]
            or request.state.route.agent_id != command.agent_id
            or request.state.route.intent != request.intent
        ):
            raise DurableManagerDispositionUnavailable(
                "immutable Assign Request 결과가 receipt와 다릅니다."
            )
        # requires_approval·authority_version은 저장 route에서 읽기만 한다 —
        # registry·중앙 authorize 재호출 0(불변 receipt+저장 route가 proof).
        return DurableManagerOwnerAssigned(
            receipt["receipt_id"],
            command.item_id,
            command.request_id,
            request.revision,
            request.state.route.agent_id,
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
                    "item_ref": command.item_id,
                    "by_manager_ref": actor_ref,
                    "reason_code": "manager_declined",
                    "rationale_sha256": _sha(command.rationale),
                    "expected_request_revision": command.expected_request_revision,
                    "source_kind": source_kind,
                }
            )
        )

    def _assign_digest(
        self,
        command: DurableManagerAssignCommand,
        principal: AuthenticatedPrincipal,
        actor_ref: str,
        source_kind: str,
    ) -> str:
        # command+principal 로컬 값만(request 읽기·중앙 authorize·registry 호출
        # 어느 것도 선행하지 않는다 — 모듈 docstring 참고).
        return _sha(
            _json(
                {
                    "action": _ASSIGN,
                    "org_id": principal.org_id,
                    "request_id": command.request_id,
                    "item_ref": command.item_id,
                    "by_manager_ref": actor_ref,
                    "agent_card_ref": _ref("card", command.agent_id),
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

    @staticmethod
    def _valid_assign_command(command: DurableManagerAssignCommand) -> None:
        if (
            any(not value for value in (command.item_id, command.request_id, command.agent_id))
            or type(command.expected_request_revision) is not int
            or command.expected_request_revision < 0
            or type(command.rationale) is not str
        ):
            raise DurableManagerDispositionUnavailable(
                "typed manager.assign_owner command 형식이 올바르지 않습니다."
            )
