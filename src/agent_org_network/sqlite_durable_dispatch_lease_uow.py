"""P17.9 S5.2 durable dispatch lease Unit of Work(ADR 0042 §9 ④⑤).

이 모듈은 어느 중앙 dispatcher 인스턴스가 WorkTicket 하나를 전달할 책임을
지는지 가르는 fence(`durable_dispatch_leases`)를 소유한다. lease는
`(ticket_id, lease_epoch, holder_ref)` 등호 + `state='leased'` +
`expires_at > now`의 full-row CAS로 관리하며, S5.1이 이미 확정한 대로 lease
token(비밀)도 tombstone 테이블도 두지 않는다 — 보유자는 항상 이 중앙
dispatcher 인스턴스 자신이고 외부 위임 대상이 0이기 때문이다(§9 ④). epoch
이력의 증거는 이 UoW가 아니라 append-only인 S5.3의 delivery attempt가 진다.

**API에 `now` 인자가 없는 것이 계약이다** — 호출자가 "만료됐다"를 주장할 수
없다. `claim`/`renew`/`release`는 각자 독립된 shared transaction을 열어
생성자에 1회 주입된 `clock`으로만 만료를 판정한다(§9 ⑤). `renew_in_transaction`/
`release_in_transaction`은 이미 write를 소유한 형제 컴포넌트(S5.3 runner)가
같은 transaction의 단일 시각을 공유하기 위한 좁은 seam이며, `now`는 그
형제가 넘겨준다(값의 출처는 여전히 같은 `clock`) — 여기서 자체 `clock()`을
다시 호출하지 않고, begin/commit도 소유하지 않는다(UPDATE만 수행한다).

`claim`은 fresh 발급과 reclaim(만료된 lease의 epoch+1 인계)을 한 분기로
통합한다 — reclaim은 별도 public 메서드가 아니다(호출자가 만료를 지시할 수
없다는 계약을 지키기 위함이다). S5 canonical instant(고정폭·고정 `+00:00`)는
S5.1의 문법을 이 모듈 로컬로 재현한다 — S4.1 timestamp와 문자열로 비교하지
않는다.

**BUSY taxonomy(ADR 0042 §9 ⑫).** 다른 connection과 lock 경합이 나면 무타입
`sqlite3.OperationalError`가 누수하지 않는다 — `DurableDispatchLeaseBusy`
(`DurableDispatchLeaseUnavailable`의 subclass, additive 변경)로 분류한다.
판별은 `answer_finalization_sqlite._is_storage_unavailable`과 같은 domain
(extended result code의 primary byte)이되, `SQLITE_BUSY`·`SQLITE_LOCKED`
**만** Busy로 좁힌다 — `IOERR`·`FULL`·`READONLY`·`CANTOPEN`·`PROTOCOL`·
`INTERRUPT`는 재시도로 풀리지 않으므로 일반 Unavailable로 남아 배치
runner(S5.3)를 멈춰야 한다. 이 모듈은 `PRAGMA busy_timeout`을 설정하지
않는다 — connection은 Completion 소유라 빌려 쓰는 것뿐이며, 대기 시간은
`SqliteQuestionCompletionUnitOfWork(timeout=...)` 생성자 인자가 유일한
손잡이다.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.question_request import AwaitingAnswer
from agent_org_network.sqlite_durable_dispatch_delivery import (
    validate_sqlite_durable_dispatch_delivery_connection,
)


class DurableDispatchLeaseError(RuntimeError):
    """Base error deliberately free of channel/registry internals."""


class DurableDispatchLeaseConflict(DurableDispatchLeaseError):
    """정상 경쟁·stale CAS — 다른 holder가 이미 가졌거나 대상 상태가 바뀌었다."""


class DurableDispatchLeaseUnavailable(DurableDispatchLeaseError):
    """capability·형식·주입 오류 — 재시도로 해소되지 않는다."""


class DurableDispatchLeaseBusy(DurableDispatchLeaseUnavailable):
    """lock 경합(SQLITE_BUSY/LOCKED) — 상태를 알지 못한 채 재시도 가능하다.

    Unavailable의 subclass다(기존 ``except ... Unavailable`` 호출자는 그대로
    동작한다). Conflict로 흡수하지 않는다 — Conflict는 "경쟁자가 이겼다"는
    도메인 사실을 안 것이고 Busy는 transaction이 상태를 읽지도 못한 모름이다.
    """


# extended result code의 primary byte만 본다(문자열 매칭 금지 — 로케일·버전
# 의존). BUSY/LOCKED만 좁힌다: IOERR·FULL·READONLY·CANTOPEN·PROTOCOL·
# INTERRUPT는 재시도로 풀리지 않으므로 일반 Unavailable로 남아야 한다.
_BUSY_SQLITE_CODES: Final = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def _is_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in _BUSY_SQLITE_CODES


def _reraise_capability(error: Exception, *, message: str) -> NoReturn:
    """capability·구조 전제 경계(생성자·`_validate_capability`·seam precondition)에서 재분류한다.

    이미 이 모듈의 typed error(Conflict·Unavailable·Busy)면 그대로 통과시킨다
    (이중 wrap 금지). ``sqlite3.Error``는 BUSY/LOCKED만 Busy로, 그 나머지는
    일반 Unavailable로 wrap한다. 그 어느 쪽도 아닌 예외(예: 공유 transaction
    seam의 재진입 오류)도 fail-closed로 일반 Unavailable로 wrap한다 — 이
    경계들은 애초에 capability·형식 오류를 구분 없이 Unavailable 하나로
    닫는 계약이었고, Busy만 그 안에서 별도로 좁혀 분류하는 additive
    변경이기 때문이다.
    """
    if isinstance(error, DurableDispatchLeaseError):
        raise error
    if isinstance(error, sqlite3.Error) and _is_busy(error):
        raise DurableDispatchLeaseBusy(message) from error
    raise DurableDispatchLeaseUnavailable(message) from error


def _reraise_write(error: Exception, *, message: str) -> NoReturn:
    """claim/renew/release 바깥 경계(begin_immediate~commit)에서 재분류한다.

    `_reraise_capability`와 달리 ``sqlite3.Error``도 ``DurableDispatchLeaseError``도
    아닌 예외는 **wrap하지 않고 원본 그대로** 올린다 — 이 경계는 fault
    injector가 던지는 임의 예외(테스트가 그 타입·메시지를 그대로 관찰해야
    한다)와, `answer_finalization_sqlite._raise_write_error`가 이미 확립한
    "모르는 실패는 삼키지 않는다" 원칙을 함께 지킨다.
    """
    if isinstance(error, DurableDispatchLeaseError):
        raise error
    if isinstance(error, sqlite3.Error):
        if _is_busy(error):
            raise DurableDispatchLeaseBusy(message) from error
        raise DurableDispatchLeaseUnavailable(message) from error
    raise error


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, raw: str) -> str:
    return f"{kind}:{_sha(raw)}"


# S5.1의 canonical UTC instant 문법을 이 모듈 로컬로 재현한다(고정폭·고정
# `+00:00` offset → 문자열 사전순이 시간순과 같아진다). S4.1 timestamp와
# 문자열로 비교하지 않는다.
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00\Z"
)


def _instant(value: datetime) -> str:
    if type(value) is not datetime or value.utcoffset() is None:
        raise DurableDispatchLeaseUnavailable(
            "dispatch lease canonical instant에는 tz-aware datetime이 필요합니다."
        )
    rendered = value.astimezone(UTC).isoformat(timespec="microseconds")
    if _TIMESTAMP_RE.fullmatch(rendered) is None:
        raise DurableDispatchLeaseUnavailable(
            "dispatch lease canonical instant 형식이 올바르지 않습니다."
        )
    return rendered


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _no_fault(_point: str) -> None:
    return None


@dataclass(frozen=True)
class DispatchLease:
    """한 WorkTicket을 전달할 책임을 지는 dispatcher 인스턴스의 fence."""

    ticket_id: str
    org_id: str
    request_id: str
    lease_epoch: int
    holder_ref: str
    expires_at: datetime


class DurableDispatchLeaseUnitOfWork:
    """`durable_dispatch_leases`에 대한 claim/renew/release UoW(ADR 0042 §9 ④⑤)."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        holder_id: str,
        clock: Callable[[], datetime],
        lease_ttl: timedelta,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if type(holder_id) is not str or not holder_id.strip():
            raise DurableDispatchLeaseUnavailable(
                "dispatch lease holder identity가 올바르지 않습니다."
            )
        if (
            type(lease_ttl) is not timedelta
            or lease_ttl <= timedelta(0)
            or lease_ttl > timedelta(days=1)
        ):
            raise DurableDispatchLeaseUnavailable(
                "dispatch lease_ttl은 0 초과 1일 이하여야 합니다."
            )
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._clock = clock
        self._lease_ttl = lease_ttl
        self._holder_ref = _ref("subject", holder_id)
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        try:
            with self._tx.scope():
                self._tx.validate_component(validate_sqlite_durable_dispatch_delivery_connection)
        except Exception as error:
            _reraise_capability(error, message="durable dispatch lease capability를 열 수 없습니다.")

    # ------------------------------------------------------------------
    # public API — `now`·`lease_epoch` 인자가 없다(호출자가 만료를 주장할 수
    # 없다는 계약). 각자 독립된 shared transaction을 연다.
    # ------------------------------------------------------------------

    def claim(self, *, ticket_id: str) -> DispatchLease:
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._validate_capability()
                now = self._clock()
                now_text = _instant(now)
                expires_text = _instant(now + self._lease_ttl)
                lease = self._claim_or_reclaim(
                    ticket_id=ticket_id, now_text=now_text, expires_text=expires_text
                )
                self._validate_capability()
                self._tx.commit()
                return lease
            except Exception as error:
                if self._tx.in_transaction:
                    self._tx.rollback()
                _reraise_write(error, message="durable dispatch lease claim이 실패했습니다.")

    def renew(self, *, lease: DispatchLease) -> DispatchLease:
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._validate_capability()
                now = self._clock()
                renewed = self._renew_cas(lease=lease, now=now)
                self._validate_capability()
                self._tx.commit()
                return renewed
            except Exception as error:
                if self._tx.in_transaction:
                    self._tx.rollback()
                _reraise_write(error, message="durable dispatch lease renew가 실패했습니다.")

    def release(self, *, lease: DispatchLease) -> None:
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._validate_capability()
                now = self._clock()
                self._release_cas(lease=lease, now=now)
                self._validate_capability()
                self._tx.commit()
            except Exception as error:
                if self._tx.in_transaction:
                    self._tx.rollback()
                _reraise_write(error, message="durable dispatch lease release가 실패했습니다.")

    # ------------------------------------------------------------------
    # in-transaction seam — 이미 write를 소유한 형제 컴포넌트(S5.3)가 같은
    # transaction의 단일 시각을 공유한다. begin/commit을 소유하지 않는다.
    # ------------------------------------------------------------------

    def renew_in_transaction(self, *, lease: DispatchLease, now: datetime) -> DispatchLease:
        self._require_open_shared_transaction()
        return self._renew_cas(lease=lease, now=now)

    def release_in_transaction(self, *, lease: DispatchLease, now: datetime) -> None:
        self._require_open_shared_transaction()
        self._release_cas(lease=lease, now=now)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_open_shared_transaction(self) -> None:
        try:
            in_transaction = self._tx.in_transaction
        except Exception as error:
            _reraise_capability(
                error,
                message="in-transaction dispatch lease 연산에는 열린 shared transaction이 필요합니다.",
            )
        if not in_transaction:
            raise DurableDispatchLeaseUnavailable(
                "in-transaction dispatch lease 연산에는 열린 shared transaction이 필요합니다."
            )

    def _validate_capability(self) -> None:
        try:
            self._tx.validate_component_in_transaction(
                validate_sqlite_durable_dispatch_delivery_connection
            )
        except Exception as error:
            _reraise_capability(
                error, message="durable dispatch delivery capability가 유효하지 않습니다."
            )

    def _claim_or_reclaim(
        self, *, ticket_id: str, now_text: str, expires_text: str
    ) -> DispatchLease:
        ticket = self._tx.execute(
            "SELECT * FROM durable_linked_work_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if ticket is None or ticket["status"] != "pending":
            raise DurableDispatchLeaseConflict(
                "dispatch lease 대상 WorkTicket이 유효하지 않습니다."
            )
        request = self._tx.select_question_request(ticket["request_id"])
        if (
            request is None
            or request.org_id != ticket["org_id"]
            or not isinstance(request.state, AwaitingAnswer)
            or request.state.ticket_id != ticket_id
            or request.revision != ticket["awaiting_revision"] + 1
        ):
            raise DurableDispatchLeaseConflict(
                "dispatch lease 대상 Question Request가 유효하지 않습니다."
            )
        row = self._tx.execute(
            "SELECT * FROM durable_dispatch_leases WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            epoch = 1
            self._tx.execute(
                "INSERT INTO durable_dispatch_leases VALUES(?,?,?,?,?,?,?,?)",
                (
                    ticket_id,
                    ticket["org_id"],
                    ticket["request_id"],
                    epoch,
                    self._holder_ref,
                    "leased",
                    expires_text,
                    now_text,
                ),
            )
            self._fault("after_lease_insert")
        else:
            if row["state"] == "leased" and row["expires_at"] > now_text:
                raise DurableDispatchLeaseConflict("active dispatch lease가 있습니다.")
            epoch = row["lease_epoch"] + 1
            cursor = self._tx.execute(
                "UPDATE durable_dispatch_leases SET lease_epoch=?,holder_ref=?,"
                "state='leased',expires_at=?,acquired_at=? "
                "WHERE ticket_id=? AND lease_epoch=? AND holder_ref=? AND state=? AND expires_at=?",
                (
                    epoch,
                    self._holder_ref,
                    expires_text,
                    now_text,
                    ticket_id,
                    row["lease_epoch"],
                    row["holder_ref"],
                    row["state"],
                    row["expires_at"],
                ),
            )
            if cursor.rowcount != 1:
                raise DurableDispatchLeaseConflict("lease CAS가 stale입니다.")
            self._fault("after_lease_cas")
        return DispatchLease(
            ticket_id=ticket_id,
            org_id=ticket["org_id"],
            request_id=ticket["request_id"],
            lease_epoch=epoch,
            holder_ref=self._holder_ref,
            expires_at=_parse_instant(expires_text),
        )

    def _renew_cas(self, *, lease: DispatchLease, now: datetime) -> DispatchLease:
        self._require_lease_shape(lease)
        now_text = _instant(now)
        expires_text = _instant(now + self._lease_ttl)
        # holder_ref는 인자로 받은 lease가 아니라 이 UoW 인스턴스 자신의
        # holder identity(`self._holder_ref`)로 대조한다 — 그래야 남의 lease를
        # 가리키는 DispatchLease를 그대로 되돌려줘도 진짜 보유자가 아니면
        # CAS가 stale로 실패한다(red 6: 다른 holder renew/release→Conflict).
        # org_id·request_id도 WHERE에 넣는다 — 그래야 호출자가 위조한 값이
        # CAS를 통과한 뒤 반환값에 그대로 echo되는 경로가 막힌다(그 위조값이
        # S5.3의 delivery attempt INSERT로 흘러가면 S5.1 lineage 검증을 깨
        # 전역 영구 Unavailable을 부를 수 있다 — P1-1과 같은 계열의 사고).
        # acquired_at<=?(새 expires_at 값)는 clock이 acquired_at보다 과거로
        # skew된 채 호출돼도 `expires_at < acquired_at`(S5.1 row invariant,
        # sqlite_durable_dispatch_delivery.py 참조)를 위반하는 행이 아예
        # 커밋되지 못하게 한다 — 이 seam은 begin/commit도 재검증도 소유하지
        # 않으므로(계약), 불변식 보호는 CAS의 WHERE 절 자체에 있어야 한다.
        cursor = self._tx.execute(
            "UPDATE durable_dispatch_leases SET expires_at=? "
            "WHERE ticket_id=? AND org_id=? AND request_id=? AND lease_epoch=? AND holder_ref=? "
            "AND state='leased' AND expires_at>? AND acquired_at<=?",
            (
                expires_text,
                lease.ticket_id,
                lease.org_id,
                lease.request_id,
                lease.lease_epoch,
                self._holder_ref,
                now_text,
                expires_text,
            ),
        )
        if cursor.rowcount != 1:
            raise DurableDispatchLeaseConflict("dispatch lease renew CAS가 stale입니다.")
        self._fault("after_renew_cas")
        return DispatchLease(
            ticket_id=lease.ticket_id,
            org_id=lease.org_id,
            request_id=lease.request_id,
            lease_epoch=lease.lease_epoch,
            holder_ref=self._holder_ref,
            expires_at=_parse_instant(expires_text),
        )

    def _release_cas(self, *, lease: DispatchLease, now: datetime) -> None:
        self._require_lease_shape(lease)
        now_text = _instant(now)
        # renew와 동일한 이유로 holder_ref는 self._holder_ref·org_id/request_id는
        # 위조 echo 차단·acquired_at<=?는 시계 역행이 invariant를 깨는 행을
        # 커밋하지 못하게 한다(위 _renew_cas 주석 참조).
        cursor = self._tx.execute(
            "UPDATE durable_dispatch_leases SET state='released',expires_at=? "
            "WHERE ticket_id=? AND org_id=? AND request_id=? AND lease_epoch=? AND holder_ref=? "
            "AND state='leased' AND acquired_at<=?",
            (
                now_text,
                lease.ticket_id,
                lease.org_id,
                lease.request_id,
                lease.lease_epoch,
                self._holder_ref,
                now_text,
            ),
        )
        if cursor.rowcount != 1:
            raise DurableDispatchLeaseConflict("dispatch lease release CAS가 stale입니다.")
        self._fault("after_release_cas")

    @staticmethod
    def _require_lease_shape(lease: DispatchLease) -> None:
        if type(lease) is not DispatchLease:
            raise DurableDispatchLeaseUnavailable("typed DispatchLease가 필요합니다.")
