"""P17.9 S5.6c FromDispatch Manager 처분 Unit of Work(ADR 0066 §5.2·ADR 0050 §12).

FromDispatch ManagerItem(S5.6a/b가 만든 ``durable_dispatch_manager_items``)의
Reroute/Dismiss를 원자 처분한다. **S4.4(``sqlite_durable_manager_disposition_uow``)
확장이 아니라 S5 소유 신 UoW다**(ADR 0066 §5.2) — 이유 둘.

1. S4.4는 처분 receipt를 S4.1 ``durable_linked_command_receipts``에
   ``target_ref = manager_item_id``로 쓰는데, FromDispatch Item은 S5 표에
   있어 그 join이 성립하지 않는다. S4.4로 확장하면 S4.6 forward sweep이
   ``manager_disposition_receipt_mismatch``로 정상 처분을 오탐한다.
2. S4가 S5를 알게 되는 계층 역전이 생긴다(S5는 S4.1 위에 선다).

S4.4의 ``source_kind == "dispatch" → Unavailable`` 거부는 그대로 유지된다
(정의역이 다르다는 사실의 표현 — 이 모듈이 그 정의역을 대신 연다).

**중앙 권한은 새 action을 만들지 않는다.** FromDispatch 처분도
``manager.act``(ADR 0050 §12·role hard-limit·``manager_item`` resource·1인칭
귀속)이고, 이 UoW는 S4.4와 동형으로 ``CentralAuthorizer``를 그대로 호출한다.
``ManagerActItemResolver``에 S5 표를 읽는 구현을 주는 것은 실 authorizer
배선(후속 슬라이스) 몫이다 — 이 UoW 자신은 그 resolver를 알지 못한다.

**receipt 표는 S5.6a가 이미 3-action 공용으로 설계해 뒀다.** ``durable_
dispatch_escalation_receipts``의 ``action`` enum은 처음부터
``{"work_ticket.escalate", "manager.reroute", "manager.dismiss"}``였다(S5.6a
schema docstring) — 이 UoW는 새 표를 만들지 않고 그 표에 나머지 두 action을
쓴다. **이 표에서는 ``resolved ⟺ manager.reroute``**이고 S4.1에서는
``resolved ⟺ manager.assign_owner``다(S5.6a 선언).

**digest는 command-local만**(S4.4 교정 계승) — item/request 읽기·중앙
authorize 어느 것도 선행하지 않는다. ``item_ref = command.item_id``를 그대로
쓴다(재hash 금지, S4.4 관례).

**reroute의 attempt은 ``item.attempt + 1``**(하드코딩 금지) — 도메인 전이표
(``question_request.py`` ``AwaitingManager(dispatched) → ReadyToDispatch``)가
``expected_attempt = current.attempt + 1``을 이미 강제하므로, 어긋나면
``QuestionRequestTransitionError``가 터진다.

**reroute의 RouteTarget — 새 대상 카드에서 ``requires_approval``·eligibility를
재도출한다(2026-07-27 team-lead 판정. 최초 설계안이었던 "기존 route 보존"은
승인 우회였다).** ``requires_approval``은 intent 단독 속성이 아니라
**(intent, 대상 카드)** 쌍에서 나온다 — 저장소의 기존 세 처분 경로가 모두
그렇게 재도출한다(``p17_manager_disposition.py:1540``·
``p17_deadlock_manager_disposition.py:755``·``p17_conflict_disposition.py:2385``,
전부 ``intent in card.approval_when``형). reroute는 대상 카드를 바꾸는
조작이므로, 옛 카드의 route를 그대로 베끼면 옛 카드에서는 승인이 필요 없던
intent가 새 카드에서는 승인이 필요한데도 ``requires_approval=False``가 그대로
실려 **답이 승인 게이트를 건너뛴다**. 이건 S4.4 assign만의 사정("Unowned라
신규 결선이 필요해서")이 아니라 **대상을 바꾸는 모든 처분의 공통 규율**이다.
``manager.act`` 중앙 재인가는 "이 Manager가 재지정할 권한이 있는가"만
답하지 "그 답이 누구의 승인 정책을 따르는가"는 답하지 않는다 — 두 질문은
별개다. 따라서 이 UoW는 S4.4의 ``DurableManagerRegistry``/
``DurableManagerAssignTarget`` 포트를 그대로 계승해 생성자에 주입받는다.
**intent는 기존 route에서 가져온다**(reroute는 같은 질문의 재지정이므로
intent는 불변 — command는 intent를 받지도 실어 보내지도 않는다). 바뀌는
것은 **대상 카드와 그로부터 재도출되는 ``requires_approval``·eligibility·
``authority_version``**이다. ``resolve_assign_target``이 ``None``이면
fail-closed(write 0·Item ``open``·Request ``AwaitingManager`` 잔류 — 미아
없음).

**dismiss → ``DeclinedRequest(reason_code="manager_declined")``**(S4.1 dismiss와
동일 종착). ``rationale``은 지속하지 않는다(노출 불변식) — digest의
``rationale_sha256``에만 결박된다.

**⑯ 남의 행 없음.** 이 UoW는 S5 자기 두 표(``durable_dispatch_manager_
items``·``durable_dispatch_escalation_receipts``)와 Completion이 소유한
``question_requests``(``compare_and_set_question_request`` seam)만 건드린다.
``durable_linked_work_tickets``는 읽지도 쓰지도 않는다 — 그 ticket은 이미
S5.6b에서 ``escalated``로 종결됐고, 새 attempt의 새 ticket은 이 UoW 이후
recovery runner가 별도로 enqueue한다(ADR 0066 §4).

**capability는 하나뿐이다.** ``validate_sqlite_durable_dispatch_escalation_
connection``(S5.6a) 하나만 검증한다 — 그 validator 자신이 이미 S4.1 linked
aggregates·Completion parent를 전이 요구하므로(S5.6b와 동형) 별도로
재검증하지 않는다. 이 capability의 손상은 **S5.6b가 이미 첫 소비자로 확립한
``DurableDispatchEscalationUnavailable``/``Busy``로 wrap 없이 관측된다** —
이 모듈은 그 capability의 owner가 아니다. 이 모듈 자신의 write 경계
(``begin_immediate``~``commit``)에서 난 순수 lock 경합(BUSY/LOCKED)만 자기
``DurableDispatchDispositionBusy``로 분류한다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, NoReturn

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
    QuestionRequestTransitionError,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_durable_dispatch_escalation import (
    validate_sqlite_durable_dispatch_escalation_connection,
)
from agent_org_network.sqlite_durable_dispatch_escalation_uow import (
    DurableDispatchEscalationBusy,
    DurableDispatchEscalationError,
    DurableDispatchEscalationUnavailable,
)
from agent_org_network.sqlite_durable_manager_disposition_uow import (
    DurableManagerAssignTarget,
    DurableManagerRegistry,
)


class DurableDispatchDispositionError(RuntimeError):
    """Base error deliberately free of authority/registry internals."""


class DurableDispatchDispositionConflict(DurableDispatchDispositionError):
    """정상 경쟁·stale CAS·중앙 재인가 거부·1인칭 fence 위반 — write 0으로 fail-closed."""


class DurableDispatchDispositionUnavailable(DurableDispatchDispositionError):
    """이 UoW 자신의 명령 형식·replay·write 오류 — 재시도로 해소되지 않는다."""


class DurableDispatchDispositionBusy(DurableDispatchDispositionUnavailable):
    """이 UoW 자신의 write 경계에서 난 lock 경합(SQLITE_BUSY/LOCKED)."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, raw: str) -> str:
    return f"{kind}:{_sha(raw)}"


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


# S5.1/S5.2/S5.4/S5.6a/S5.6b의 canonical UTC instant 문법(고정폭·고정
# `+00:00`)을 이 모듈 로컬로 재현한다 — S4.1 timestamp와 문자열로 비교하지
# 않는다.
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00\Z"
)


def _instant(value: datetime) -> str:
    if type(value) is not datetime or value.utcoffset() is None:
        raise DurableDispatchDispositionUnavailable(
            "dispatch disposition canonical instant에는 tz-aware datetime이 필요합니다."
        )
    rendered = value.astimezone(UTC).isoformat(timespec="microseconds")
    if _TIMESTAMP_RE.fullmatch(rendered) is None:
        raise DurableDispatchDispositionUnavailable(
            "dispatch disposition canonical instant 형식이 올바르지 않습니다."
        )
    return rendered


# BUSY/LOCKED만 좁힌다(S5.2 `_is_busy`와 동형·모듈 로컬 재현).
_BUSY_SQLITE_CODES: Final = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def _is_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in _BUSY_SQLITE_CODES


def _no_fault(_point: str) -> None:
    return None


_ACTION: Final = "manager.act"
_REROUTE: Final = "manager.reroute"
_DISMISS: Final = "manager.dismiss"


@dataclass(frozen=True)
class DurableDispatchRerouteCommand:
    """Server-authenticated typed command, not an MCP payload DTO."""

    item_id: str
    request_id: str
    agent_id: str
    expected_request_revision: int
    rationale: str = ""


@dataclass(frozen=True)
class DurableDispatchDismissCommand:
    """Server-authenticated typed command, not an MCP payload DTO."""

    item_id: str
    request_id: str
    expected_request_revision: int
    rationale: str = ""


DurableDispatchDispositionCommand = DurableDispatchRerouteCommand | DurableDispatchDismissCommand


@dataclass(frozen=True)
class DurableDispatchRerouted:
    receipt_id: str
    item_id: str
    request_id: str
    request_revision: int
    agent_id: str
    attempt: int


@dataclass(frozen=True)
class DurableDispatchDismissed:
    receipt_id: str
    item_id: str
    request_id: str
    request_revision: int
    reason_code: Literal["manager_declined"] = "manager_declined"


DurableDispatchDispositionResult = DurableDispatchRerouted | DurableDispatchDismissed


def _reraise_capability(error: Exception, *, message: str) -> NoReturn:
    """S5.6a escalation schema capability는 S5.6b가 이미 첫 소비자다.

    같은 capability 손상은 이 모듈에서도 S5.6b가 확립한
    ``DurableDispatchEscalationUnavailable``/``Busy``로 그대로 관측돼야
    한다 — 이 모듈은 그 capability의 owner가 아니므로 자기 타입으로 새로
    감싸지 않는다.
    """
    if isinstance(error, DurableDispatchEscalationError):
        raise error
    if isinstance(error, sqlite3.Error) and _is_busy(error):
        raise DurableDispatchEscalationBusy(message) from error
    raise DurableDispatchEscalationUnavailable(message) from error


def _reraise_write(error: Exception, *, message: str) -> NoReturn:
    """act() 바깥 경계(begin_immediate~commit)에서 재분류한다.

    이미 typed인 예외(이 모듈 자신·S5.6a/b escalation capability 등)는 wrap
    하지 않고 그대로 통과시킨다. raw ``sqlite3.Error``만 BUSY/LOCKED 여부로
    이 모듈 자신의 Unavailable/Busy로 분류한다.
    """
    if isinstance(error, DurableDispatchDispositionError):
        raise error
    if isinstance(error, sqlite3.Error):
        if _is_busy(error):
            raise DurableDispatchDispositionBusy(message) from error
        raise DurableDispatchDispositionUnavailable(message) from error
    raise error


class DurableDispatchDispositionUnitOfWork:
    """FromDispatch ManagerItem을 Reroute/Dismiss로 원자 처분한다."""

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
                self._tx.validate_component(validate_sqlite_durable_dispatch_escalation_connection)
        except Exception as error:
            _reraise_capability(
                error, message="durable dispatch disposition capability를 열 수 없습니다."
            )

    def act(
        self,
        *,
        principal: AuthenticatedPrincipal,
        command: DurableDispatchDispositionCommand,
    ) -> DurableDispatchDispositionResult:
        if type(principal) is not AuthenticatedPrincipal or type(command) not in (
            DurableDispatchRerouteCommand,
            DurableDispatchDismissCommand,
        ):
            raise DurableDispatchDispositionUnavailable(
                "서버 principal과 exact manager.act command가 필요합니다."
            )
        if isinstance(command, DurableDispatchRerouteCommand):
            self._valid_reroute_command(command)
        else:
            self._valid_dismiss_command(command)
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._validate_capability()
                item = self._item(command.item_id, principal.org_id)
                actor_ref = _ref("subject", principal.subject_id)
                if actor_ref != item["manager_subject_id"]:
                    raise DurableDispatchDispositionConflict(
                        "principal이 durable dispatch ManagerItem의 Manager가 아닙니다."
                    )
                result: DurableDispatchDispositionResult
                if isinstance(command, DurableDispatchRerouteCommand):
                    result = self._reroute(principal, command, item, actor_ref)
                else:
                    result = self._dismiss(principal, command, item, actor_ref)
                self._validate_capability()
                self._tx.commit()
                return result
            except Exception as error:
                if self._tx.in_transaction:
                    self._tx.rollback()
                _reraise_write(error, message="durable dispatch disposition이 실패했습니다.")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _validate_capability(self) -> None:
        try:
            self._tx.validate_component_in_transaction(
                validate_sqlite_durable_dispatch_escalation_connection
            )
        except Exception as error:
            _reraise_capability(
                error, message="durable dispatch disposition capability가 유효하지 않습니다."
            )

    def _item(self, item_id: str, org_id: str) -> sqlite3.Row:
        row = self._tx.execute(
            "SELECT * FROM durable_dispatch_manager_items WHERE manager_item_id=? AND org_id=?",
            (item_id, org_id),
        ).fetchone()
        if row is None:
            raise DurableDispatchDispositionConflict("durable dispatch ManagerItem이 없습니다.")
        return row

    def _current_awaiting_manager_request(
        self, command: DurableDispatchDispositionCommand, item: sqlite3.Row
    ) -> QuestionRequest:
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or request.org_id != item["org_id"]
            or not isinstance(request.state, AwaitingManager)
            or request.state.public_kind != "dispatched"
            or request.state.item_id != command.item_id
            or request.state.route is None
            or request.state.attempt is None
            or request.revision != command.expected_request_revision
        ):
            raise DurableDispatchDispositionConflict(
                "stale dispatch 처분/Question Request command를 거부합니다."
            )
        return request

    def _resolve_target(
        self,
        principal: AuthenticatedPrincipal,
        intent: str,
        command: DurableDispatchRerouteCommand,
    ) -> DurableManagerAssignTarget:
        target = self._registry.resolve_assign_target(
            org_id=principal.org_id, intent=intent, agent_id=command.agent_id
        )
        if target is None:
            raise DurableDispatchDispositionConflict("Reroute 대상 Registry 결선이 없습니다.")
        if target.agent_id != command.agent_id:
            raise DurableDispatchDispositionConflict(
                "Registry가 command와 다른 Agent Card를 돌려줬습니다."
            )
        return target

    def _reroute(
        self,
        principal: AuthenticatedPrincipal,
        command: DurableDispatchRerouteCommand,
        item: sqlite3.Row,
        actor_ref: str,
    ) -> DurableDispatchRerouted:
        digest = self._reroute_digest(command, principal, actor_ref)
        receipt = self._tx.execute(
            "SELECT * FROM durable_dispatch_escalation_receipts WHERE command_digest=?", (digest,)
        ).fetchone()
        if receipt is not None:
            return self._stored_reroute_result(receipt, command, item)

        # FRESH
        if item["status"] != "open":
            raise DurableDispatchDispositionConflict("current open dispatch ManagerItem이 아닙니다.")
        request = self._current_awaiting_manager_request(command, item)
        assert isinstance(request.state, AwaitingManager)
        awaiting_manager_state = request.state
        previous_route = awaiting_manager_state.route
        assert previous_route is not None
        new_attempt = item["attempt"] + 1

        resource = ResourceRef(
            org_id=principal.org_id,
            kind="manager_item",
            resource_id=command.item_id,
            owner_subject_id=principal.subject_id,
        )
        grant = self._authorize(principal, resource)

        # intent는 기존 route에서 가져온다(reroute는 같은 질문의 재지정 —
        # command는 intent를 싣지 않는다). requires_approval·eligibility는
        # 새 대상 카드에서 재도출한다(모듈 docstring 2026-07-27 판정 —
        # 옛 route를 그대로 베끼면 승인 우회가 된다).
        intent = previous_route.intent
        target = self._resolve_target(principal, intent, command)

        route = RouteTarget(
            intent=intent,
            agent_id=target.agent_id,
            requires_approval=target.requires_approval,
            authority_version=grant.policy_version,
        )

        now = self._clock()
        created_at = _instant(now)
        receipt_ref = self._new_receipt_ref()

        # WRITE 순서(receipt → 자기 Item CAS → Request 전이). audit/outbox
        # intent mirror는 두지 않는다(S5.6a 설계 계승).
        self._tx.execute(
            "INSERT INTO durable_dispatch_escalation_receipts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                receipt_ref,
                principal.org_id,
                command.request_id,
                command.item_id,
                digest,
                actor_ref,
                _REROUTE,
                command.expected_request_revision,
                created_at,
            ),
        )
        self._fault("after_receipt_insert")

        cursor = self._tx.execute(
            "UPDATE durable_dispatch_manager_items SET status='resolved' "
            "WHERE manager_item_id=? AND status='open'",
            (command.item_id,),
        )
        if cursor.rowcount != 1:
            raise DurableDispatchDispositionConflict("commit-time dispatch ManagerItem CAS에 실패했습니다.")
        self._fault("after_item_disposed")

        try:
            updated = request.transition(
                ReadyToDispatch(
                    route=route,
                    attempt=new_attempt,
                    trigger_key=receipt_ref,
                    handling=HandlingAssignment(
                        kind="system", ref=receipt_ref, due_at=awaiting_manager_state.handling.due_at
                    ),
                ),
                clock=lambda: now,
            )
        except QuestionRequestTransitionError as error:
            raise DurableDispatchDispositionConflict(
                "dispatch reroute 시점 Request 전이가 유효하지 않습니다."
            ) from error
        if not self._tx.compare_and_set_question_request(
            request.request_id, request.revision, request, updated
        ):
            raise DurableDispatchDispositionConflict("commit-time Question Request CAS에 실패했습니다.")
        self._fault("after_request_transition")

        return DurableDispatchRerouted(
            receipt_ref, command.item_id, command.request_id, updated.revision, route.agent_id, new_attempt
        )

    def _dismiss(
        self,
        principal: AuthenticatedPrincipal,
        command: DurableDispatchDismissCommand,
        item: sqlite3.Row,
        actor_ref: str,
    ) -> DurableDispatchDismissed:
        digest = self._dismiss_digest(command, principal, actor_ref)
        receipt = self._tx.execute(
            "SELECT * FROM durable_dispatch_escalation_receipts WHERE command_digest=?", (digest,)
        ).fetchone()
        if receipt is not None:
            return self._stored_dismiss_result(receipt, command)

        # FRESH
        if item["status"] != "open":
            raise DurableDispatchDispositionConflict("current open dispatch ManagerItem이 아닙니다.")
        request = self._current_awaiting_manager_request(command, item)

        resource = ResourceRef(
            org_id=principal.org_id,
            kind="manager_item",
            resource_id=command.item_id,
            owner_subject_id=principal.subject_id,
        )
        self._authorize(principal, resource)

        now = self._clock()
        created_at = _instant(now)
        receipt_ref = self._new_receipt_ref()

        self._tx.execute(
            "INSERT INTO durable_dispatch_escalation_receipts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                receipt_ref,
                principal.org_id,
                command.request_id,
                command.item_id,
                digest,
                actor_ref,
                _DISMISS,
                command.expected_request_revision,
                created_at,
            ),
        )
        self._fault("after_receipt_insert")

        cursor = self._tx.execute(
            "UPDATE durable_dispatch_manager_items SET status='dismissed' "
            "WHERE manager_item_id=? AND status='open'",
            (command.item_id,),
        )
        if cursor.rowcount != 1:
            raise DurableDispatchDispositionConflict("commit-time dispatch ManagerItem CAS에 실패했습니다.")
        self._fault("after_item_disposed")

        try:
            updated = request.transition(
                DeclinedRequest(reason_code="manager_declined"), clock=lambda: now
            )
        except QuestionRequestTransitionError as error:
            raise DurableDispatchDispositionConflict(
                "dispatch dismiss 시점 Request 전이가 유효하지 않습니다."
            ) from error
        if not self._tx.compare_and_set_question_request(
            request.request_id, request.revision, request, updated
        ):
            raise DurableDispatchDispositionConflict("commit-time Question Request CAS에 실패했습니다.")
        self._fault("after_request_transition")

        return DurableDispatchDismissed(
            receipt_ref, command.item_id, command.request_id, updated.revision
        )

    def _authorize(
        self, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> AuthorizationGrant:
        if self._authorizer is None:
            raise DurableDispatchDispositionUnavailable("중앙 manager.act 권한 원천이 없습니다.")
        try:
            grant = self._authorizer.authorize(principal, _ACTION, resource)
        except Exception as error:
            raise DurableDispatchDispositionUnavailable(
                "중앙 권한 확인을 수행할 수 없습니다."
            ) from error
        if type(grant) is not AuthorizationGrant or not self._verify(grant, principal, resource):
            raise DurableDispatchDispositionConflict("중앙 manager.act 권한이 거부됐습니다.")
        return grant

    def _verify(
        self, grant: AuthorizationGrant, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> bool:
        assert self._authorizer is not None
        try:
            return self._authorizer.verify(grant, principal, _ACTION, resource)
        except Exception:
            return False

    def _stored_reroute_result(
        self, receipt: sqlite3.Row, command: DurableDispatchRerouteCommand, item: sqlite3.Row
    ) -> DurableDispatchRerouted:
        if (
            receipt["action"] != _REROUTE
            or receipt["manager_item_id"] != command.item_id
            or receipt["request_id"] != command.request_id
            or receipt["expected_request_revision"] != command.expected_request_revision
            or item["status"] != "resolved"
        ):
            raise DurableDispatchDispositionUnavailable(
                "immutable dispatch reroute receipt/ManagerItem이 서로 다릅니다."
            )
        expected_attempt = item["attempt"] + 1
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or not isinstance(request.state, ReadyToDispatch)
            or request.state.attempt != expected_attempt
            or request.state.trigger_key != receipt["receipt_id"]
            or request.state.handling.kind != "system"
            or request.state.handling.ref != receipt["receipt_id"]
            or request.state.route.agent_id != command.agent_id
        ):
            raise DurableDispatchDispositionUnavailable(
                "immutable dispatch reroute Request 결과가 receipt와 다릅니다."
            )
        return DurableDispatchRerouted(
            receipt["receipt_id"],
            command.item_id,
            command.request_id,
            request.revision,
            request.state.route.agent_id,
            request.state.attempt,
        )

    def _stored_dismiss_result(
        self, receipt: sqlite3.Row, command: DurableDispatchDismissCommand
    ) -> DurableDispatchDismissed:
        if (
            receipt["action"] != _DISMISS
            or receipt["manager_item_id"] != command.item_id
            or receipt["request_id"] != command.request_id
            or receipt["expected_request_revision"] != command.expected_request_revision
        ):
            raise DurableDispatchDispositionUnavailable(
                "immutable dispatch dismiss receipt가 command와 다릅니다."
            )
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or not isinstance(request.state, DeclinedRequest)
            or request.state.reason_code != "manager_declined"
        ):
            raise DurableDispatchDispositionUnavailable(
                "immutable dispatch dismiss Request 결과가 receipt와 다릅니다."
            )
        return DurableDispatchDismissed(
            receipt["receipt_id"], command.item_id, command.request_id, request.revision
        )

    def _reroute_digest(
        self,
        command: DurableDispatchRerouteCommand,
        principal: AuthenticatedPrincipal,
        actor_ref: str,
    ) -> str:
        # command+principal 로컬 값만(item/request 읽기·중앙 authorize 어느
        # 것도 선행하지 않는다 — 모듈 docstring 참고).
        return _sha(
            _json(
                {
                    "action": _REROUTE,
                    "org_id": principal.org_id,
                    "request_id": command.request_id,
                    "item_ref": command.item_id,
                    "by_manager_ref": actor_ref,
                    "agent_card_ref": _ref("card", command.agent_id),
                    "rationale_sha256": _sha(command.rationale),
                    "expected_request_revision": command.expected_request_revision,
                }
            )
        )

    def _dismiss_digest(
        self,
        command: DurableDispatchDismissCommand,
        principal: AuthenticatedPrincipal,
        actor_ref: str,
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
                }
            )
        )

    def _new_receipt_ref(self) -> str:
        receipt_id = self._receipt_id_factory()
        if type(receipt_id) is not str or not receipt_id.strip():
            raise DurableDispatchDispositionUnavailable("receipt identity가 올바르지 않습니다.")
        return _ref("receipt", receipt_id)

    @staticmethod
    def _valid_reroute_command(command: DurableDispatchRerouteCommand) -> None:
        if (
            any(
                type(value) is not str or not value.strip()
                for value in (command.item_id, command.request_id, command.agent_id)
            )
            or type(command.expected_request_revision) is not int
            or command.expected_request_revision < 0
            or type(command.rationale) is not str
        ):
            raise DurableDispatchDispositionUnavailable(
                "typed manager.reroute command 형식이 올바르지 않습니다."
            )

    @staticmethod
    def _valid_dismiss_command(command: DurableDispatchDismissCommand) -> None:
        if (
            any(
                type(value) is not str or not value.strip()
                for value in (command.item_id, command.request_id)
            )
            or type(command.expected_request_revision) is not int
            or command.expected_request_revision < 0
            or type(command.rationale) is not str
        ):
            raise DurableDispatchDispositionUnavailable(
                "typed manager.dismiss command 형식이 올바르지 않습니다."
            )


__all__ = [
    "DurableDispatchDismissCommand",
    "DurableDispatchDismissed",
    "DurableDispatchDispositionBusy",
    "DurableDispatchDispositionCommand",
    "DurableDispatchDispositionConflict",
    "DurableDispatchDispositionError",
    "DurableDispatchDispositionResult",
    "DurableDispatchDispositionUnavailable",
    "DurableDispatchDispositionUnitOfWork",
    "DurableDispatchRerouteCommand",
    "DurableDispatchRerouted",
]
