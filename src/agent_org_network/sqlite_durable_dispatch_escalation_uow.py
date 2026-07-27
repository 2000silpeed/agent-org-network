"""P17.9 S5.6b dispatch timeout escalation Unit of Work(ADR 0066 §5.3·ADR 0042 §9 ⑯⑰).

timeout escalation은 사람 명령이 아니라 **system 전이**다(S4.5
``work_ticket.create``와 같은 결) — 중앙 재인가 0, `principal_ref`는 system
subject 상수, `CentralAuthorizer` 미주입. 권한 근거는 "SLA가 지났다"는 durable
사실 자체다.

**digest는 ticket 행 자체에서만 유도한다(request 현재 상태에 의존하지 않는다).**
S4.4/S4.5/S5.4 교정의 "digest는 command-local"을 이 UoW의 유일한 caller 입력인
``ticket_id``에 맞게 정밀화한 것이다 — ``expected_request_revision``은
``request.revision``이 아니라 **``ticket.awaiting_revision + 1``**(ticket
행에 고정된 불변 사실)에서 구한다. 그래야 이미 성공적으로 commit된 뒤의 재시도
(ticket.status가 이미 'escalated'·Request가 이미 AwaitingManager로 전이된 뒤)
에도 digest를 다시 계산하고 저장된 receipt를 찾아 **replay로 안전하게
수렴**할 수 있다(S5.4 ``_stored_result``가 ticket.status=='completed'인
POST-transition 상태를 replay 확인 기준으로 쓰는 것과 동형). ticket_id 자체가
이 escalation 사건의 유일한 identity이므로(그 ticket은 평생 최대 한 번만
escalate된다 — CAS `WHERE status='pending'`), 이 선택은 ADR 0066 §5.3의
"digest = command-local(action·org·ticket·request·expected_revision·system ref)"
의 문언을 반박하지 않는다 — 값 자체는 같고(ticket_id가 정하는 org/request/
expected_revision은 유일하다), **어느 시점에 읽어 계산하는가만** 다르다.

**⑰ 시간 안전 — `now`는 생성자 clock 1지점, SLA는 transaction 안에서 재조회한
`due_at`으로 다시 판정한다.** `escalate(*, ticket_id)`에 `now`·`due_at` 인자가
없다(`inspect` 시그니처 red — S5.2와 같은 계약). 스캔(S5.5)이 준 후보 목록·
`due_at`은 이 UoW의 어떤 판정에도 쓰이지 않는다 — 그 보고는 작업 목록이지
write 근거가 아니다.

**⑯ 남의 행 전수 대조 — 이 UoW가 직접 쓰는 남의 행은 둘뿐이다.**
- ``durable_linked_work_tickets.status``(`pending → escalated`): S4.1
  `_validate_rows`의 ticket 규칙(attempt·awaiting_revision 정수, route_sha256
  SHA, owner_subject_id typed ref, status enum, created_at timestamp, org/
  request lineage)을 전수 대조했다 — 이 write가 SET하는 컬럼은 **status
  하나뿐**이고 값은 리터럴 `'escalated'`(허용 집합 소속)라 문법 위반이 불가능
  하다. 단조 전방성은 S4.1이 검증하지 않으므로 `WHERE status='pending'`가
  유일한 집행 지점이고, 다른 컬럼은 SET에 넣지 않는다.
- ``durable_dispatch_leases``(release): S5.4가 밟은 함정과 같은 지점이다.
  S5.1의 행 불변식 `expires_at >= acquired_at`을 이 CAS가 지키도록
  `WHERE ticket_id=? AND state='leased' AND acquired_at<=?`를 S5.4와
  **동형으로** 쓴다. rowcount 0(만료·부재·clock skew)은 정상이다.

**늦은 답의 처분은 의도된 동작이다.** escalation이 ticket을 `pending`에서
떠나게 하면 그 뒤 도착한 워커의 답은 S5.4의 stale 판정(`status != 'pending'`)
으로 거부된다 — 재시도가 아니라 폐기가 계약이다. 그 거부가 정당한 유일한
근거가 이 UoW의 SLA 재판정(§9 ⑰(2))이므로, 그 재판정 자체가 write 0으로
끝나면(SLA 미도달·manager 미해석) 이 폐기 계약도 발동하지 않는다.

**CentralAuthorizer를 주입받지 않는다.** ``manager.act``(ADR 0050 §12) 재인가는
S5.6c 처분 UoW의 몫이다 — 이 UoW는 escalation Item·receipt를 만들 뿐 아무
사람 명령도 승인하지 않는다.

**오류 계열 규율(S5.3 §9 ⑭·S5.4 §9 ⑮ 계승).** 이 모듈 자신의 명령/replay/
도메인 검증 실패는 ``DurableDispatchEscalation{Error,Conflict,Unavailable,
Busy}``로만 신호한다. S5.1 dispatch delivery capability
(``validate_sqlite_durable_dispatch_delivery_connection``) 검증은 S5.2가 이미
확립한 타입(``DurableDispatchLeaseUnavailable``/``Busy``)으로 그대로
재확인한다(S5.4가 확립한 규율의 계승) — 같은 capability 손상이 어느 S5
모듈의 어느 진입점에서 관측되든 항상 같은 타입이어야 한다. 반대로 이 모듈
자신의 escalation schema capability
(``validate_sqlite_durable_dispatch_escalation_connection``) 검증 실패는
**새로 이 모듈이 소유하는** ``DurableDispatchEscalationUnavailable``/``Busy``
로 분류한다 — 이 UoW가 그 capability의 첫 소비자이기 때문이다(S5.2가
S5.1 capability의 첫 소비자였던 것과 같은 결).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn, Protocol

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingManager,
    HandlingAssignment,
    QuestionRequestTransitionError,
)
from agent_org_network.sqlite_durable_dispatch_delivery import (
    validate_sqlite_durable_dispatch_delivery_connection,
)
from agent_org_network.sqlite_durable_dispatch_escalation import (
    validate_sqlite_durable_dispatch_escalation_connection,
)
from agent_org_network.sqlite_durable_dispatch_lease_uow import (
    DurableDispatchLeaseBusy,
    DurableDispatchLeaseError,
    DurableDispatchLeaseUnavailable,
)


class DurableDispatchEscalationError(RuntimeError):
    """Base error deliberately free of directory/registry internals."""


class DurableDispatchEscalationConflict(DurableDispatchEscalationError):
    """정상 경쟁·stale CAS·SLA 미도달·manager 미해석 — write 0으로 fail-closed."""


class DurableDispatchEscalationUnavailable(DurableDispatchEscalationError):
    """이 UoW 자신의 capability·형식·replay·write 오류 — 재시도로 해소되지 않는다."""


class DurableDispatchEscalationBusy(DurableDispatchEscalationUnavailable):
    """이 UoW 자신의 capability 검증·write 경계에서 난 lock 경합(SQLITE_BUSY/LOCKED)."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, raw: str) -> str:
    return f"{kind}:{_sha(raw)}"


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


# S5.1/S5.2/S5.4/S5.6a의 canonical UTC instant 문법(고정폭·고정 `+00:00`)을 이
# 모듈 로컬로 재현한다 — S4.1 timestamp와 문자열로 비교하지 않는다.
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00\Z"
)


def _instant(value: datetime) -> str:
    if type(value) is not datetime or value.utcoffset() is None:
        raise DurableDispatchEscalationUnavailable(
            "dispatch escalation canonical instant에는 tz-aware datetime이 필요합니다."
        )
    rendered = value.astimezone(UTC).isoformat(timespec="microseconds")
    if _TIMESTAMP_RE.fullmatch(rendered) is None:
        raise DurableDispatchEscalationUnavailable(
            "dispatch escalation canonical instant 형식이 올바르지 않습니다."
        )
    return rendered


# BUSY/LOCKED만 좁힌다(S5.2 `_is_busy`와 동형·모듈 로컬 재현).
_BUSY_SQLITE_CODES: Final = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def _is_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in _BUSY_SQLITE_CODES


def _no_fault(_point: str) -> None:
    return None


_ACTION: Final = "work_ticket.escalate"
_SYSTEM_SUBJECT_ID: Final = "system:dispatch_timeout_escalation"
SYSTEM_SUBJECT_REF: Final = _ref("subject", _SYSTEM_SUBJECT_ID)


class DispatchEscalationTargetDirectory(Protocol):
    """escalation 대상 Manager 해석 전용 좁은 port(ADR 0066 §5.3).

    답하지 않은 Owner의 nearest manager, 없으면 유일 root User를 돌려준다
    (ADR 0065 §10 c.0 선택 규칙을 Owner 앵커로 적용). 반환값은 이미
    typed subject reference(``subject:<sha256>``)여야 한다(S4.5
    ``DurableWorkTicketRegistry.resolve_owner_subject``와 같은 계약 — 재hash
    하지 않는다).
    """

    def resolve_manager(self, *, org_id: str, owner_subject_ref: str) -> str | None: ...


@dataclass(frozen=True)
class DurableDispatchEscalated:
    receipt_id: str
    manager_item_id: str
    ticket_id: str
    request_id: str
    manager_subject_id: str
    request_revision: int


def _escalation_digest(
    *, org_id: str, ticket_id: str, request_id: str, expected_request_revision: int
) -> str:
    # ticket 행에서만 유도한다(모듈 docstring 참조) — request 현재 상태에
    # 의존하면 이미 성공한 뒤의 재시도가 replay를 찾지 못하고 대신 "request가
    # 더 이상 AwaitingAnswer가 아니다"는 Conflict로 막힌다.
    return _sha(
        _json(
            {
                "action": _ACTION,
                "org_id": org_id,
                "ticket_id": ticket_id,
                "request_id": request_id,
                "expected_request_revision": expected_request_revision,
                "by_system_ref": SYSTEM_SUBJECT_REF,
            }
        )
    )


def _reraise_capability(error: Exception, *, message: str) -> NoReturn:
    """이 모듈 자기 capability(escalation schema) 검증 실패를 재분류한다."""
    if isinstance(error, DurableDispatchEscalationError):
        raise error
    if isinstance(error, sqlite3.Error) and _is_busy(error):
        raise DurableDispatchEscalationBusy(message) from error
    raise DurableDispatchEscalationUnavailable(message) from error


def _reraise_dispatch_capability(error: Exception) -> NoReturn:
    """S5.1 dispatch delivery capability 예외를 S5.2가 이미 확립한 타입으로 재확인한다.

    같은 capability 손상은 S5.2·S5.3·S5.4·이 모듈 어디서 관측되든 항상
    ``DurableDispatchLeaseUnavailable``/``Busy``여야 한다(S5.4 모듈 docstring
    규율의 계승).
    """
    if isinstance(error, DurableDispatchLeaseError):
        raise error
    if isinstance(error, sqlite3.Error) and _is_busy(error):
        raise DurableDispatchLeaseBusy(
            "durable dispatch escalation capability를 열 수 없습니다."
        ) from error
    raise DurableDispatchLeaseUnavailable(
        "durable dispatch escalation capability를 열 수 없습니다."
    ) from error


def _reraise_write(error: Exception, *, message: str) -> NoReturn:
    """escalate() 바깥 경계(begin_immediate~commit)에서 재분류한다.

    이미 typed인 예외(이 모듈 자신·S5.1 lease capability 등)는 wrap하지 않고
    그대로 통과시킨다. raw ``sqlite3.Error``만 BUSY/LOCKED 여부로 이 모듈
    자신의 Unavailable/Busy로 분류한다.
    """
    if isinstance(error, DurableDispatchEscalationError):
        raise error
    if isinstance(error, sqlite3.Error):
        if _is_busy(error):
            raise DurableDispatchEscalationBusy(message) from error
        raise DurableDispatchEscalationUnavailable(message) from error
    raise error


class DurableDispatchEscalationUnitOfWork:
    """FromDispatch Item·escalation receipt를 system 전이로 원자 결박한다."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        directory: DispatchEscalationTargetDirectory,
        clock: Callable[[], datetime],
        escalation_sla: timedelta,
        item_id_factory: Callable[[], str],
        receipt_id_factory: Callable[[], str],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        # 이월 불가 — 새 SLA는 항상 `now + escalation_sla`로 만든다(ADR 0066
        # §5.3). `lease_ttl`과 같은 판(0 < sla <= 30일).
        if (
            type(escalation_sla) is not timedelta
            or escalation_sla <= timedelta(0)
            or escalation_sla > timedelta(days=30)
        ):
            raise DurableDispatchEscalationUnavailable(
                "dispatch escalation_sla는 0 초과 30일 이하여야 합니다."
            )
        self._completion = completion
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._directory = directory
        self._clock = clock
        self._escalation_sla = escalation_sla
        self._item_id_factory = item_id_factory
        self._receipt_id_factory = receipt_id_factory
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        try:
            with self._tx.scope():
                self._tx.validate_component(validate_sqlite_durable_dispatch_escalation_connection)
        except Exception as error:
            _reraise_capability(
                error, message="durable dispatch escalation capability를 열 수 없습니다."
            )
        try:
            with self._tx.scope():
                self._tx.validate_component(validate_sqlite_durable_dispatch_delivery_connection)
        except Exception as error:
            _reraise_dispatch_capability(error)

    def escalate(self, *, ticket_id: str) -> DurableDispatchEscalated:
        if type(ticket_id) is not str or not ticket_id.strip():
            raise DurableDispatchEscalationUnavailable(
                "typed dispatch escalation ticket_id가 필요합니다."
            )
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._validate_capability()
                now = self._clock()
                ticket = self._tx.execute(
                    "SELECT * FROM durable_linked_work_tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if ticket is None:
                    raise DurableDispatchEscalationConflict(
                        "dispatch escalation 대상 WorkTicket이 존재하지 않습니다."
                    )
                expected_revision = ticket["awaiting_revision"] + 1
                digest = _escalation_digest(
                    org_id=ticket["org_id"],
                    ticket_id=ticket["ticket_id"],
                    request_id=ticket["request_id"],
                    expected_request_revision=expected_revision,
                )
                receipt = self._tx.execute(
                    "SELECT * FROM durable_dispatch_escalation_receipts WHERE command_digest=?",
                    (digest,),
                ).fetchone()
                result = (
                    self._stored_result(receipt, ticket=ticket)
                    if receipt is not None
                    else self._fresh(
                        ticket=ticket, now=now, expected_revision=expected_revision, digest=digest
                    )
                )
                self._validate_capability()
                self._tx.commit()
                return result
            except Exception as error:
                if self._tx.in_transaction:
                    self._tx.rollback()
                _reraise_write(error, message="durable dispatch escalation이 실패했습니다.")

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
                error, message="durable dispatch escalation capability가 유효하지 않습니다."
            )
        try:
            self._tx.validate_component_in_transaction(
                validate_sqlite_durable_dispatch_delivery_connection
            )
        except Exception as error:
            _reraise_dispatch_capability(error)

    def _fresh(
        self,
        *,
        ticket: sqlite3.Row,
        now: datetime,
        expected_revision: int,
        digest: str,
    ) -> DurableDispatchEscalated:
        if ticket["status"] != "pending":
            raise DurableDispatchEscalationConflict(
                "dispatch escalation 대상 WorkTicket이 유효하지 않습니다."
            )
        request = self._tx.select_question_request(ticket["request_id"])
        if (
            request is None
            or request.org_id != ticket["org_id"]
            or not isinstance(request.state, AwaitingAnswer)
            or request.state.ticket_id != ticket["ticket_id"]
            or request.state.attempt != ticket["attempt"]
            or request.revision != expected_revision
        ):
            raise DurableDispatchEscalationConflict(
                "dispatch escalation 대상 Question Request가 유효하지 않습니다."
            )
        # ⑰(2) — 스캔이 준 due_at·후보 존재 자체를 근거로 쓰지 않는다. 재조회한
        # due_at을 자기 clock()과 다시 비교한다(TOCTOU·시간 안전 동시 대응).
        if request.state.handling.due_at > now:
            raise DurableDispatchEscalationConflict("SLA가 아직 지나지 않았습니다.")
        try:
            manager = self._directory.resolve_manager(
                org_id=ticket["org_id"], owner_subject_ref=ticket["owner_subject_id"]
            )
        except Exception as error:
            raise DurableDispatchEscalationConflict(
                "dispatch escalation manager 대상을 해석할 수 없습니다."
            ) from error
        if manager is None:
            raise DurableDispatchEscalationConflict(
                "dispatch escalation manager 대상을 해석할 수 없습니다."
            )

        created = _instant(now)
        item_ref = self._new_item_ref()
        receipt_ref = self._new_receipt_ref()

        # WRITE 순서: item → receipt → lease release → ticket status → Request 전이.
        self._tx.execute(
            "INSERT INTO durable_dispatch_manager_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item_ref,
                ticket["org_id"],
                ticket["request_id"],
                ticket["ticket_id"],
                ticket["attempt"],
                # c.3 선례(`sqlite_durable_conflict_escalation_uow.py`): Item의
                # awaiting_revision은 부모 행 값을 **그대로 복사**하고(+1 아님),
                # 전이 전 Request revision은 receipt의 expected_request_revision이
                # 진다. c.3 검증 두 줄이 그 쌍을 고정한다 —
                # `manager_item.awaiting_revision == case.awaiting_revision`,
                # `command.expected_request_revision == case.awaiting_revision + 1`.
                # S5.7의 item⟺ticket 교차검증이 같은 모양이므로 여기서 +1을 쓰면 깨진다.
                ticket["awaiting_revision"],
                ticket["route_sha256"],
                ticket["owner_subject_id"],
                manager,
                "open",
                _instant(request.state.handling.due_at),
                created,
            ),
        )
        self._fault("after_manager_item_insert")

        self._tx.execute(
            "INSERT INTO durable_dispatch_escalation_receipts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                receipt_ref,
                ticket["org_id"],
                ticket["request_id"],
                item_ref,
                digest,
                SYSTEM_SUBJECT_REF,
                _ACTION,
                expected_revision,
                created,
            ),
        )
        self._fault("after_escalation_receipt_insert")

        # ⑯ — 남의 테이블(S5.1 durable_dispatch_leases)을 직접 쓴다. 그 행
        # 불변식(expires_at >= acquired_at)을 CAS의 WHERE 절 자체가 지키도록
        # acquired_at<=?(이 UoW 자기 clock 값)를 S5.4와 동형으로 남긴다.
        # rowcount 0(만료·부재·clock skew)은 정상이다 — escalation이 lease를
        # 이긴다.
        self._tx.execute(
            "UPDATE durable_dispatch_leases SET state='released', expires_at=? "
            "WHERE ticket_id=? AND state='leased' AND acquired_at<=?",
            (created, ticket["ticket_id"], created),
        )
        self._fault("after_lease_release")

        # ⑯ — 남의 테이블(S4.1 durable_linked_work_tickets)의 status 컬럼
        # 하나만 SET한다(값은 허용 enum의 리터럴 'escalated'). 단조 전방성은
        # S4.1이 검증하지 않으므로 이 CAS `WHERE status='pending'`이 유일한
        # 집행 지점이다.
        cursor = self._tx.execute(
            "UPDATE durable_linked_work_tickets SET status='escalated' "
            "WHERE ticket_id=? AND status='pending'",
            (ticket["ticket_id"],),
        )
        if cursor.rowcount != 1:
            raise DurableDispatchEscalationConflict("WorkTicket escalate CAS가 stale입니다.")
        self._fault("after_ticket_escalated")

        try:
            updated = request.transition(
                AwaitingManager(
                    item_id=item_ref,
                    public_kind="dispatched",
                    route=request.state.route,
                    attempt=request.state.attempt,
                    handling=HandlingAssignment(
                        kind="manager_item", ref=item_ref, due_at=now + self._escalation_sla
                    ),
                ),
                clock=lambda: now,
            )
        except QuestionRequestTransitionError as error:
            raise DurableDispatchEscalationConflict(
                "dispatch escalation 시점 Request 전이가 유효하지 않습니다."
            ) from error
        if not self._tx.compare_and_set_question_request(
            request.request_id, request.revision, request, updated
        ):
            raise DurableDispatchEscalationConflict("commit-time Question Request CAS에 실패했습니다.")
        self._fault("after_request_transition")

        return DurableDispatchEscalated(
            receipt_id=receipt_ref,
            manager_item_id=item_ref,
            ticket_id=ticket["ticket_id"],
            request_id=ticket["request_id"],
            manager_subject_id=manager,
            request_revision=updated.revision,
        )

    def _stored_result(
        self, receipt: sqlite3.Row, *, ticket: sqlite3.Row
    ) -> DurableDispatchEscalated:
        if (
            receipt["action"] != _ACTION
            or receipt["org_id"] != ticket["org_id"]
            or receipt["request_id"] != ticket["request_id"]
        ):
            raise DurableDispatchEscalationUnavailable(
                "immutable dispatch escalation receipt가 command와 다릅니다."
            )
        item = self._tx.execute(
            "SELECT * FROM durable_dispatch_manager_items WHERE manager_item_id=?",
            (receipt["manager_item_id"],),
        ).fetchone()
        if (
            item is None
            or item["org_id"] != ticket["org_id"]
            or item["request_id"] != ticket["request_id"]
            or item["ticket_id"] != ticket["ticket_id"]
        ):
            raise DurableDispatchEscalationUnavailable(
                "immutable dispatch escalation receipt/item이 서로 다릅니다."
            )
        refreshed_ticket = self._tx.execute(
            "SELECT status FROM durable_linked_work_tickets WHERE ticket_id=?",
            (ticket["ticket_id"],),
        ).fetchone()
        if refreshed_ticket is None or refreshed_ticket["status"] != "escalated":
            raise DurableDispatchEscalationUnavailable(
                "immutable dispatch escalation receipt/ticket이 서로 다릅니다."
            )
        request = self._tx.select_question_request(ticket["request_id"])
        if (
            request is None
            or request.org_id != ticket["org_id"]
            or not isinstance(request.state, AwaitingManager)
            or request.state.item_id != item["manager_item_id"]
        ):
            raise DurableDispatchEscalationUnavailable(
                "immutable dispatch escalation receipt/Request 결과가 서로 다릅니다."
            )
        return DurableDispatchEscalated(
            receipt_id=receipt["receipt_id"],
            manager_item_id=item["manager_item_id"],
            ticket_id=ticket["ticket_id"],
            request_id=ticket["request_id"],
            manager_subject_id=item["manager_subject_id"],
            request_revision=request.revision,
        )

    def _new_item_ref(self) -> str:
        item_id = self._item_id_factory()
        if type(item_id) is not str or not item_id.strip():
            raise DurableDispatchEscalationUnavailable("manager item identity가 올바르지 않습니다.")
        return _ref("manager", item_id)

    def _new_receipt_ref(self) -> str:
        receipt_id = self._receipt_id_factory()
        if type(receipt_id) is not str or not receipt_id.strip():
            raise DurableDispatchEscalationUnavailable("receipt identity가 올바르지 않습니다.")
        return _ref("receipt", receipt_id)


__all__ = [
    "DispatchEscalationTargetDirectory",
    "DurableDispatchEscalated",
    "DurableDispatchEscalationBusy",
    "DurableDispatchEscalationConflict",
    "DurableDispatchEscalationError",
    "DurableDispatchEscalationUnavailable",
    "DurableDispatchEscalationUnitOfWork",
    "SYSTEM_SUBJECT_REF",
]
