"""P17.9 S5.3 durable dispatch outbox consumer + delivery attempt runner(ADR 0042 §9 ③⑥⑦⑫).

§9 ③이 이미 확정한 대로, S4.1 ``durable_linked_outbox_intents``는 receipt와
1:1로 봉인된 기록 mirror라 작업 큐가 아니다. **실제 작업 큐는
``durable_linked_work_tickets``의 ``status='pending'`` 행 자체**다 — S4.5가
Request 전이와 한 transaction으로 이미 써 둔 그 행이 "보낼 의도"의 durable
기록이므로 이 모듈은 두 번째 진실 원천을 만들지 않는다.

한 ticket의 처리는 3-phase다.

    TX-A(``lease_uow.claim``) → 전송(외부, transaction 밖) → TX-B(``_record``)

BUSY(락 경합·모름)와 Conflict(도메인 경쟁 패배·정상 관찰)는 분리해 흡수한다
(§9 ⑫). **비-Busy ``DurableDispatchLeaseUnavailable``은 절대 흡수하지 않는다**
— 잡으면 손상된 capability를 삼킨 채 남은 ticket을 조용히 헛돌게 하는 최악의
실패 모드가 되므로, run 전체를 중단하고 호출자에게 그대로 전파한다.

owner 결박(§9 ⑦)은 fence이지 주소가 아니다. enqueue 시점에 저장된
``owner_subject_id``와, 전달 시점에 현재 Registry로 다시 해석한 Owner의
subject ref가 다르면 카드 소유권이 바뀐 것이므로 **전송 호출 자체를 하지
않고** ``owner_drift``로 fail-closed 처리한다.

**러너 자기 오류와 lease 오류는 다른 계층이다(ADR 0042 §9 ⑭·review-s53
P2-7).** ``_candidates``(phase 0 후보 조회)는 아직 어떤 lease도 쥐지 않은
채 자기 connection을 직접 읽는다 — 이 read의 BUSY를 ``DurableDispatchLease*``
로 분류하면 "아직 존재하지 않는 lease"에 대한 판정처럼 읽혀 소유권이
흐려진다. ``batch_limit`` 형식·``_instant`` tz-naive·claim 뒤 ticket 소실도
같은 이유로 러너 자신의 ``DurableDispatchRun*`` 계층을 쓴다. lease 계열
예외(``DurableDispatchLeaseBusy``/``Conflict``/``Unavailable``)는 그
어디서도 ``DurableDispatchRun*``으로 wrap하지 않고 원본 그대로 통과시킨다
— 이 구분이 신설의 목적이다. 반대로 ``_prepare``(claim 이후 read)의
BUSY는 **이미 쥐고 있는 lease의 운명**에 관한 것이라 계속
``DurableDispatchLeaseBusy``로 남는다(``contended``로 흡수 — lease를
풀려면 또 write가 필요해 그것도 BUSY일 수 있으므로 풀지 않는다).

**판별 기준은 "lease 객체가 있나"가 아니라 "무엇이 실패했나"다.** 생성자의
open-time capability validate(``validate_sqlite_durable_dispatch_delivery_connection``)
도 아직 어떤 lease가 없는 시점에 도는 러너 자신의 호출이지만, 검증
대상은 **S5.1 저장소 capability**(스키마 상태)이지 러너 설정이 아니므로
계속 lease 계열로 남는다(team-lead 2026-07-25 재확정) — 같은 capability
손상이 생성자와 ``run_once`` 어느 진입점으로 발화하든 항상
``DurableDispatchLeaseUnavailable``/``Busy``로 관측돼야 한다(진입점에
따라 다른 타입이 되면 안 된다). Run* 계층은 오직 **러너 자신의 형식·
설정** 오류(``batch_limit``·``_instant``·claim 뒤 ticket 소실)와
**러너 자신의 read**(``_candidates``)만 쓴다.

**TTL 초과로 lease가 탈취되는 경우(§9 ⑭)** — 전송(외부, transaction 밖)
도중 lease가 만료돼 다른 인스턴스가 먼저 reclaim(epoch+1)하면, TX-B의
renew/release CAS가 stale로 실패해 ``DurableDispatchLeaseConflict``를
낸다. 이건 TX-A Conflict(가져오지 못함 — 외부 효과 0)와도, BUSY(lock
압력 — 모름)와도 다른 세 번째 사실이다: **보냈는데 기록하지 못했다**(외부
효과 1). 이 경우 시도 행은 없다(트랜잭션 전체 롤백) — 워커는 이미
받았을 수 있지만 그 사실은 durable하게 남지 않는다(lease 만료 뒤
새 epoch로 재전달돼 최종적으로는 at-least-once 봉투 안에 있다). 이
카운터(``preempted``)는 ``skipped``·``contended`` 어디와도 합치지
않는다 — 각각 다른 운영 손잡이(전자는 없음·후자는 timeout/batch_limit)
와 이어지고, ``preempted``는 반대로 ``lease_ttl``을 늘려야 하는 신호다.

``owner_drift``는 **중앙이 내리는 fail-closed 판정이지 채널이 관측한
사실이 아니다** — 그래서 ``DispatchChannel.deliver``의 반환 타입
``Undeliverable.reason_code``는 채널이 실제로 말할 수 있는 두 값
(``no_connected_worker``·``channel_error``)으로 좁힌다. 시도 행에
기록되는 값의 집합(``ok``·``no_connected_worker``·``owner_drift``·
``channel_error``, S5.1 ``_DELIVERY_REASON``과 동형)은 내부
``_AttemptRecord``만 나른다 — 이 경계로 채널 어댑터가 중앙 fence
판정을 자기보고할 수 없다(domain-architect 2026-07-25 정정). plain
dataclass는 ``Literal``을 런타임에 강제하지 않으므로, 실제 방어선은
``_record_of``의 화이트리스트 membership 검사다.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Protocol, TypeAlias

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.question_request import AwaitingAnswer
from agent_org_network.sqlite_durable_dispatch_delivery import (
    validate_sqlite_durable_dispatch_delivery_connection,
)
from agent_org_network.sqlite_durable_dispatch_lease_uow import (
    DispatchLease,
    DurableDispatchLeaseBusy,
    DurableDispatchLeaseConflict,
    DurableDispatchLeaseUnavailable,
    DurableDispatchLeaseUnitOfWork,
)


class DurableDispatchRunError(RuntimeError):
    """러너 자신의 오류 계층 — lease 계열(``DurableDispatchLeaseError``)과 분리된다."""


class DurableDispatchRunUnavailable(DurableDispatchRunError):
    """러너 자신의 형식·capability 오류(batch_limit·canonical instant·claim 뒤 ticket 소실)."""


class DurableDispatchRunBusy(DurableDispatchRunUnavailable):
    """``_candidates``(아직 어떤 lease도 쥐지 않은 phase 0 read)의 lock 경합."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, raw: str) -> str:
    return f"{kind}:{_sha(raw)}"


# S5.1/S5.2의 canonical UTC instant 문법(고정폭·고정 `+00:00`)을 이 모듈
# 로컬로 재현한다 — S4.1 timestamp와 문자열로 비교하지 않는다.
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00\Z"
)


def _instant(value: datetime) -> str:
    # 이 clock은 runner 자신의 주입값이다(lease_uow의 clock과 별개) — 형식
    # 오류는 러너 자신의 DurableDispatchRunUnavailable이다.
    if type(value) is not datetime or value.utcoffset() is None:
        raise DurableDispatchRunUnavailable(
            "dispatch delivery record canonical instant에는 tz-aware datetime이 필요합니다."
        )
    rendered = value.astimezone(UTC).isoformat(timespec="microseconds")
    if _TIMESTAMP_RE.fullmatch(rendered) is None:
        raise DurableDispatchRunUnavailable(
            "dispatch delivery record canonical instant 형식이 올바르지 않습니다."
        )
    return rendered


# BUSY/LOCKED만 좁힌다(S5.2 `_is_busy`와 동형·모듈 로컬 재현) — IOERR·FULL·
# READONLY·CANTOPEN·PROTOCOL·INTERRUPT는 재시도로 풀리지 않으므로 일반
# Unavailable(여기서는 wrap하지 않고 그대로 전파)로 남아 run을 멈춰야 한다.
_BUSY_SQLITE_CODES: Final = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def _is_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in _BUSY_SQLITE_CODES


def _no_fault(_point: str) -> None:
    return None


@dataclass(frozen=True)
class DispatchFrame:
    """워커에게 실제로 보낼 전달 payload — route/question/context/session을 그대로 나른다."""

    ticket_id: str
    request_id: str
    org_id: str
    attempt: int
    lease_epoch: int
    agent_id: str
    owner_id: str
    question: str
    context_snapshot: str | None
    session_id: str | None


@dataclass(frozen=True)
class Delivered:
    kind: Literal["delivered"] = "delivered"


@dataclass(frozen=True)
class Undeliverable:
    # 채널이 실제로 말할 수 있는 값만(포트 반환 계약) — owner_drift는 중앙
    # 판정이라 이 타입으로 자기보고할 수 없다(모듈 docstring 참조).
    reason_code: Literal["no_connected_worker", "channel_error"]
    kind: Literal["undeliverable"] = "undeliverable"


DispatchOutcome: TypeAlias = Delivered | Undeliverable


class DispatchChannel(Protocol):
    """실 전달 채널 포트 — 예외를 던져도 runner가 ``channel_error``로 흡수한다."""

    def deliver(self, frame: DispatchFrame) -> DispatchOutcome: ...


# 채널이 말할 수 있는 값의 whitelist — Undeliverable.reason_code Literal은
# 타입체커만 강제하므로, 위조/미지 값을 실제로 걸러내는 런타임 fence는
# 이 frozenset membership 검사다(`_record_of` 참조).
_CHANNEL_REASONS: Final = frozenset({"no_connected_worker", "channel_error"})

# 시도 행에 실제로 기록되는 값의 집합 — S5.1 `_DELIVERY_REASON`과 동형.
_DeliveryReasonCode: TypeAlias = Literal["ok", "no_connected_worker", "owner_drift", "channel_error"]


@dataclass(frozen=True)
class _AttemptRecord:
    """TX-B가 실제로 쓰는 값 — 채널(포트) 타입과 분리된 내부 표현이다.

    ``owner_drift``는 owner fence 분기의 ``_OWNER_DRIFT`` 상수로만
    생성된다 — 채널의 ``Undeliverable``에는 없는 값이고, claim 이후 대상
    상태가 바뀐 stale-state 분기(``_prepare``)는 ``channel_error``다.
    (기록 alias — architect-s5 2026-07-25: stale-state는 owner drift와
    다른 사실[Request가 정상 진행했을 뿐]이라 정직한 라벨은 5번째
    reason_code[예: ``target_moved``]가 맞지만, S5.1 ``_DELIVERY_REASON``
    확장 비용이 지금 이득보다 커 보류한다 — S5.7에서 운영 신호 근거가
    나오면 재검토.)
    """

    outcome: Literal["delivered", "undeliverable"]
    reason_code: _DeliveryReasonCode


_OWNER_DRIFT: Final = _AttemptRecord(outcome="undeliverable", reason_code="owner_drift")


def _record_of(outcome: object) -> _AttemptRecord:
    """채널이 돌려준 값(신뢰 불가)을 시도 기록으로 변환하는 실제 fence.

    ``Delivered``만 성공으로 인정하고, ``Undeliverable``은 화이트리스트
    (``_CHANNEL_REASONS``) 안의 reason_code만 그대로 옮긴다. 그 밖의 모든
    값 — 위조 타입, 미지 reason_code, **``owner_drift`` 자기보고 시도** —
    은 ``channel_error``로 강등한다.
    """
    if type(outcome) is Delivered:
        return _AttemptRecord(outcome="delivered", reason_code="ok")
    if type(outcome) is Undeliverable and outcome.reason_code in _CHANNEL_REASONS:
        return _AttemptRecord(outcome="undeliverable", reason_code=outcome.reason_code)
    return _AttemptRecord(outcome="undeliverable", reason_code="channel_error")


@dataclass(frozen=True)
class DispatchOwner:
    owner_id: str
    owner_subject_ref: str


class DispatchOwnerDirectory(Protocol):
    """Owner 주소 해석 전용 포트 — eligibility 재판정도 directory lookup도 아니다."""

    def resolve_owner(self, *, org_id: str, agent_id: str) -> DispatchOwner | None: ...


@dataclass(frozen=True)
class DispatchRunReport:
    """노출 불변식 — 자유 문장·원문·예외 메시지를 두지 않는다(실패는 카운트로만).

    돌려야 할 손잡이가 다르면 다른 카운터다(§9 ⑫⑭).

    - ``skipped``   — 지금 대상 아님(정상 관찰). 손잡이 없음.
    - ``contended`` — lock 압력. Completion timeout·batch_limit을 줄인다.
    - ``preempted`` — TTL보다 전송이 느려 다른 인스턴스에게 lease를
      빼앗겼다(전송은 됐을 수 있으나 기록은 못 했다 — 시도 행 0). 반대
      방향으로 ``lease_ttl``을 **늘려야** 하는 신호다.

    ``skipped``는 외부 효과가 없고(가져오지 못함), ``preempted``는 있다
    (보냈는데 기록 못 함) — 같은 사실이 아니므로 합치지 않는다.
    ``preempted``는 ``delivered``/``undeliverable`` 어디에도 세지 않는다
    (시도 행 자체가 없다).
    """

    scanned: int
    claimed: int
    delivered: int
    undeliverable: int
    skipped: int
    contended: int
    preempted: int


class DurableDispatchRunner:
    """dispatch 작업 큐(``status='pending'`` ticket)를 소비하는 outbox 소비자."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        lease_uow: DurableDispatchLeaseUnitOfWork,
        channel: DispatchChannel,
        directory: DispatchOwnerDirectory,
        clock: Callable[[], datetime],
        attempt_id_factory: Callable[[], str],
        batch_limit: int = 32,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if type(batch_limit) is not int or batch_limit < 1:
            raise DurableDispatchRunUnavailable("dispatch runner batch_limit이 올바르지 않습니다.")
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._lease_uow = lease_uow
        self._channel = channel
        self._directory = directory
        self._clock = clock
        self._attempt_id_factory = attempt_id_factory
        self._batch_limit = batch_limit
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        try:
            with self._tx.scope():
                self._tx.validate_component(validate_sqlite_durable_dispatch_delivery_connection)
        except Exception as error:
            # 판별 기준은 "lease 객체가 있나"가 아니라 "무엇이 실패했나"다 —
            # 여기서 검증하는 validate_sqlite_durable_dispatch_delivery_connection은
            # S5.1 저장소 capability(스키마 상태)이지 러너 설정이 아니므로
            # lease 계열로 남는다(review-s53 team-lead 2026-07-25 재확정 —
            # red 30과의 일관성: 같은 capability 손상이 진입점[생성자·
            # run_once]에 따라 다른 타입이 되면 안 된다. batch_limit·
            # `_instant`·`_candidates`의 Run* 배치와는 다른 판정이다).
            if isinstance(error, sqlite3.Error) and _is_busy(error):
                raise DurableDispatchLeaseBusy(
                    "durable dispatch runner capability를 열 수 없습니다."
                ) from error
            raise DurableDispatchLeaseUnavailable(
                "durable dispatch runner capability를 열 수 없습니다."
            ) from error

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def run_once(self, *, org_id: str | None = None) -> DispatchRunReport:
        ticket_ids = self._candidates(org_id=org_id)
        scanned = len(ticket_ids)
        claimed = delivered = undeliverable = skipped = contended = preempted = 0
        for ticket_id in ticket_ids:
            try:
                lease = self._lease_uow.claim(ticket_id=ticket_id)
            except DurableDispatchLeaseBusy:
                # lock 경합(모름) — 다음 run이 재시도한다.
                contended += 1
                continue
            except DurableDispatchLeaseConflict:
                # 도메인 사실(경쟁 패배·지금 대상 아님) — 정상 관찰.
                skipped += 1
                continue
            # 비-Busy DurableDispatchLeaseUnavailable은 여기서 잡지 않는다 —
            # run 전체를 중단하고 호출자에게 그대로 전파한다(손상 스키마를
            # 삼킨 채 조용히 헛도는 최악의 실패 모드를 피한다).
            claimed += 1

            try:
                record, target_ref, frame = self._prepare(lease)
            except DurableDispatchLeaseBusy:
                # lock 경합(모름) — lease는 leased 유지이므로 crash point ②와
                # 관측이 같다(만료 뒤 epoch+1이 재전달한다).
                contended += 1
                continue
            self._fault("before_send")
            if record is None:
                assert frame is not None
                record = _record_of(self._deliver(frame))
            self._fault("after_send")

            try:
                self._record(lease=lease, record=record, target_ref=target_ref)
            except DurableDispatchLeaseBusy:
                # TX-B의 BUSY는 시도 행 미기록 + lease leased 유지이므로 crash
                # point ②와 관측이 같다 — 만료 뒤 epoch+1이 재전달한다.
                contended += 1
                continue
            except DurableDispatchLeaseConflict:
                # TTL 초과로 다른 인스턴스가 먼저 reclaim했다 — 보냈을 수는
                # 있지만 기록은 못 했다(시도 행 0, 트랜잭션 전체 롤백). skipped
                # (외부 효과 0)와도 contended(모름)와도 다른 세 번째 사실이다.
                preempted += 1
                continue
            self._fault("after_record")

            if record.outcome == "delivered":
                delivered += 1
            else:
                undeliverable += 1
        return DispatchRunReport(
            scanned=scanned,
            claimed=claimed,
            delivered=delivered,
            undeliverable=undeliverable,
            skipped=skipped,
            contended=contended,
            preempted=preempted,
        )

    # ------------------------------------------------------------------
    # phase 0 — 후보 조회(read only)
    # ------------------------------------------------------------------

    def _candidates(self, *, org_id: str | None) -> list[str]:
        try:
            with self._tx.scope():
                with self._tx.read_scope():
                    if org_id is None:
                        rows = self._tx.execute(
                            "SELECT ticket_id FROM durable_linked_work_tickets WHERE status='pending' "
                            "ORDER BY created_at, ticket_id COLLATE BINARY LIMIT ?",
                            (self._batch_limit,),
                        ).fetchall()
                    else:
                        rows = self._tx.execute(
                            "SELECT ticket_id FROM durable_linked_work_tickets "
                            "WHERE status='pending' AND org_id=? "
                            "ORDER BY created_at, ticket_id COLLATE BINARY LIMIT ?",
                            (org_id, self._batch_limit),
                        ).fetchall()
        except Exception as error:
            # 무타입 sqlite3.OperationalError가 누수하지 않게 BUSY/LOCKED만
            # 좁혀 승격한다 — 스캔 자체가 재시도 가능함을 호출자에게 알린다
            # (아직 ticket 루프 진입 전이라 contended로 흡수할 대상이 없다).
            # 아직 어떤 lease도 쥐지 않았으므로 Run* 계층이다(lease 계열이
            # 아니다 — 모듈 docstring 참조).
            if isinstance(error, sqlite3.Error) and _is_busy(error):
                raise DurableDispatchRunBusy(
                    "durable dispatch candidate 조회가 lock 경합으로 실패했습니다."
                ) from error
            raise
        return [row["ticket_id"] for row in rows]

    # ------------------------------------------------------------------
    # claim 이후 read — frame 조립 + owner fence(read only)
    # ------------------------------------------------------------------

    def _prepare(
        self, lease: DispatchLease
    ) -> tuple[_AttemptRecord | None, str, DispatchFrame | None]:
        try:
            with self._tx.scope():
                with self._tx.read_scope():
                    ticket = self._tx.execute(
                        "SELECT * FROM durable_linked_work_tickets WHERE ticket_id=?",
                        (lease.ticket_id,),
                    ).fetchone()
                    request = self._tx.select_question_request(lease.request_id)
        except Exception as error:
            # _candidates와 동형 — 이 read의 BUSY는 run_once가 그 ticket만
            # contended로 흡수한다(lease는 leased 유지 — TX-B Busy와 같은 논거).
            if isinstance(error, sqlite3.Error) and _is_busy(error):
                raise DurableDispatchLeaseBusy(
                    "durable dispatch prepare 조회가 lock 경합으로 실패했습니다."
                ) from error
            raise
        if ticket is None:
            # 정상 API로는 claim된 lease가 가리키는 ticket이 사라질 수
            # 없다(S4.1 FK RESTRICT) — 도달하면 러너 자신의 불변식 위반이다.
            raise DurableDispatchRunUnavailable(
                "claim된 dispatch lease의 WorkTicket을 찾을 수 없습니다."
            )
        target_ref: str = ticket["owner_subject_id"]
        if (
            request is None
            or not isinstance(request.state, AwaitingAnswer)
            or request.state.ticket_id != ticket["ticket_id"]
            or request.revision != ticket["awaiting_revision"] + 1
        ):
            # claim 이후 대상 상태가 바뀌었다 — 재시도 대상으로 되돌린다.
            return (
                _AttemptRecord(outcome="undeliverable", reason_code="channel_error"),
                target_ref,
                None,
            )
        try:
            owner = self._directory.resolve_owner(
                org_id=ticket["org_id"], agent_id=request.state.route.agent_id
            )
        except Exception:
            owner = None
        if owner is None or owner.owner_subject_ref != ticket["owner_subject_id"]:
            # enqueue 이후 카드 소유권이 바뀌었다 — 전송 호출 자체를 하지 않는다.
            # 이 판정은 중앙만 내린다(_OWNER_DRIFT — 채널의 Undeliverable에는
            # 없는 값이다).
            return _OWNER_DRIFT, target_ref, None
        frame = DispatchFrame(
            ticket_id=lease.ticket_id,
            request_id=lease.request_id,
            org_id=lease.org_id,
            attempt=ticket["attempt"],
            lease_epoch=lease.lease_epoch,
            agent_id=request.state.route.agent_id,
            owner_id=owner.owner_id,
            question=request.question,
            context_snapshot=request.context_snapshot,
            session_id=request.session_id,
        )
        return None, target_ref, frame

    def _deliver(self, frame: DispatchFrame) -> object:
        # 반환값은 신뢰하지 않는다 — 채널이 무엇을 돌려주든(위조 타입·미지
        # reason_code·owner_drift 자기보고 시도) `_record_of`가 유일한 fence다.
        try:
            return self._channel.deliver(frame)
        except Exception:
            return Undeliverable(reason_code="channel_error")

    # ------------------------------------------------------------------
    # TX-B — 시도 기록 + lease 갱신(같은 transaction)
    # ------------------------------------------------------------------

    def _record(self, *, lease: DispatchLease, record: _AttemptRecord, target_ref: str) -> None:
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._tx.validate_component_in_transaction(
                    validate_sqlite_durable_dispatch_delivery_connection
                )
                now = self._clock()
                created = _instant(now)
                delivered = record.outcome == "delivered"
                self._tx.execute(
                    "INSERT INTO durable_dispatch_delivery_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        _ref("receipt", self._attempt_id_factory()),
                        lease.org_id,
                        lease.ticket_id,
                        lease.request_id,
                        lease.lease_epoch,
                        lease.holder_ref,
                        record.outcome,
                        record.reason_code,
                        target_ref,
                        created,
                    ),
                )
                self._fault("after_attempt")
                if delivered:
                    self._lease_uow.renew_in_transaction(lease=lease, now=now)
                else:
                    self._lease_uow.release_in_transaction(lease=lease, now=now)
                self._tx.commit()
            except Exception as error:
                if self._tx.in_transaction:
                    self._tx.rollback()
                # BUSY/LOCKED만 좁혀 Busy로 분류한다 — 그 외 어떤 예외(fault
                # injector의 임의 예외·스키마 UNIQUE 제약 IntegrityError·
                # DurableDispatchLeaseConflict/Unavailable)도 wrap하지 않고
                # 그대로 올린다. "모르는 실패는 삼키지 않는다" 원칙이다.
                if isinstance(error, sqlite3.Error) and _is_busy(error):
                    raise DurableDispatchLeaseBusy(
                        "durable dispatch delivery 기록이 lock 경합으로 실패했습니다."
                    ) from error
                raise


__all__ = [
    "DispatchChannel",
    "DispatchFrame",
    "DispatchOutcome",
    "DispatchOwner",
    "DispatchOwnerDirectory",
    "DispatchRunReport",
    "Delivered",
    "DurableDispatchRunBusy",
    "DurableDispatchRunError",
    "DurableDispatchRunUnavailable",
    "DurableDispatchRunner",
    "Undeliverable",
]
