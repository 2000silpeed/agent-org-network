"""P17.9 S5.5 durable dispatch timeout scan(ADR 0042 §9 ⑨) — read-only.

`AwaitingAnswer.handling.due_at`(§2의 도메인 SLA)은 "이 실행 시도가 너무
오래 걸렸다 → 사람에게"다. 이는 lease 만료("이 전달을 맡은 중앙 인스턴스가
멈췄다 → 재전달", S5.2 reclaim 소관)와 **다른 시계**다(§9 ⑨). 이 모듈의
판별자는 전자(SLA)뿐이고, lease 시계는 정보로만 실어 나른다 —
``lease_active``가 True여도 그건 "지금 누가 재전송을 시도하고 있다"는
사실일 뿐 SLA 초과라는 사실을 은폐하지 않는다(활성 lease와 SLA 초과는
동시에 참일 수 있다).

**`now`가 파라미터인 것이 여기서는 옳다** — S5.2 lease UoW의 "호출자가
만료를 주장할 수 없다"는 계약은 write fence를 지키기 위한 write 경로
한정 규율이다(claim/renew/release CAS가 stale lease를 부활시키지 않도록).
이 스캐너는 어떤 write도 하지 않는 순수 관찰이므로 호출자 시각을 받아도
어떤 불변식도 위태롭게 하지 않고, 오히려 결정론 테스트를 가능하게 한다.

후보 조건(§9 ⑨ 그대로)은 ``ticket.status='pending'`` ∧ Request가 그
ticket에 결박된 ``AwaitingAnswer`` ∧ ``revision == ticket.awaiting_revision
+ 1`` ∧ ``handling.due_at <= now``(경계 포함)다. **여기서 후보는 손상이
아니라 정상 관찰 결과다** — S4.6
(``sqlite_durable_linked_reconciliation``)의 ``capable ⟺ violation 0``과
의미가 다르다: 이 모듈은 ``capable == True ⟺ 스캔을 완주했다``이고,
후보가 여럿이어도(심지어 전부 SLA를 넘겼어도) capable은 그대로 True다.
capability 자체가 서지 않을 때만(스키마 손상·BUSY·경로 오류)
``capable=False``·``candidates=()``로 fail-closed 닫는다 — 이 판을
one-shot read-only 게이트 골격(``sqlite_durable_linked_reconciliation``,
S4.6)에서 그대로 가져온다.

read-only 단언: ``mode=ro`` URI만 열어 write 시도 자체가 SQLite 레벨에서
불가능하고, ``PRAGMA foreign_keys=ON`` 뒤 단일 deferred read transaction
안에서 ticket·lease·attempt·Request를 같은 스냅샷으로 읽는다. 이 모듈은
자기 connection을 직접 여므로(S4.6과 동형) 자기 ``timeout``을 정한다 —
lock 경합(BUSY/LOCKED)도 다른 모든 capability 실패와 마찬가지로
capability-우선 fail-closed(``capable=False``·``candidates=()``)로
닫힌다. cursor·scheduler·wake·lease·repair·전이는 이 모듈 어디에도 없다.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from agent_org_network.question_request import AwaitingAnswer
from agent_org_network.sqlite_durable_dispatch_delivery import (
    SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,
    validate_sqlite_durable_dispatch_delivery_connection,
)
from agent_org_network.sqlite_stores import (
    _select_question_request_no_commit,  # pyright: ignore[reportPrivateUsage]
)


class DispatchTimeoutScanError(RuntimeError):
    """read-only dispatch timeout scan이 안전하게 진행할 수 없습니다."""


@dataclass(frozen=True)
class DispatchTimeoutCandidate:
    """SLA(``handling.due_at``)를 넘긴 것으로 관찰된 pending WorkTicket 한 건."""

    ticket_id: str
    org_id: str
    request_id: str
    attempt: int
    owner_subject_id: str
    awaiting_revision: int
    request_revision: int
    due_at: datetime
    lease_active: bool
    delivery_attempts: int


@dataclass(frozen=True)
class DispatchTimeoutScanReport:
    """``capable`` 값과 무관하게 ``scanned_at``이 이 보고의 관측 기준 시각이다.

    **write 근거로 쓸 수 없다** — 이 보고는 작업 목록(index)이지 evidence가
    아니다(§9 ⑨·⑰). ``scanned_at``은 호출자가 넘긴 ``now``를 그대로
    canonical UTC로 정규화한 값일 뿐, 이 모듈이 SLA를 다시 검증한다는
    뜻이 아니다 — 소비자(S5.6)는 자기 transaction 안에서 자기 `clock()`으로
    SLA를 다시 판정해야 하며 이 값이나 ``candidates``의 ``due_at``을 그
    판정의 근거로 삼지 않는다. ``now``가 애초에 무효(tz-naive 등)라
    canonical 값을 만들 수 없을 때만 ``None``이다 — 위조된 시각을 감사
    기록에 남기지 않기 위함이다.
    """

    capable: bool
    detail: str
    dispatch_delivery_manifest_present: bool
    scanned_at: datetime | None
    candidates: tuple[DispatchTimeoutCandidate, ...]


def _open(path: str | Path) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise DispatchTimeoutScanError("dispatch timeout scan은 기존 SQLite 파일만 엽니다.")
    try:
        return sqlite3.connect(
            f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
    # expanduser/resolve/as_uri는 sqlite3.Error가 아닌 ValueError(임베디드 NUL)·
    # RuntimeError(`~unknownuser` 확장 실패)·OSError도 던진다 — 이걸 놓치면
    # capability가 서지 않을 때 typed report로 fail-closed한다는 이 게이트의
    # 존재 이유가 그 입력에 대해 성립하지 않는다(ADR 0042 §9 ⑱, S4.6과 공통
    # 교정). Exception으로 넓히지 않는다 — 프로그래밍 오류는 삼키지 않는다.
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        raise DispatchTimeoutScanError("dispatch timeout scan SQLite DB를 열 수 없습니다.") from error


def _manifest_present(connection: sqlite3.Connection) -> bool:
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_component_manifests'"
        ).fetchone()
        is None
    ):
        return False
    return (
        connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
            (SQLITE_DURABLE_DISPATCH_DELIVERY_COMPONENT_ID,),
        ).fetchone()
        is not None
    )


def _org_clause(org_id: str | None) -> tuple[str, tuple[str, ...]]:
    if org_id is None:
        return "", ()
    return " AND org_id COLLATE BINARY=?", (org_id,)


def _lease_active(connection: sqlite3.Connection, *, ticket_id: str, now_text: str) -> bool:
    # 정보 전용 판별자다(§9 ⑨) — 활성 lease가 SLA 초과 사실을 은폐하지
    # 않는다. lease 만료 판정은 S5.2와 동형으로 고정폭 canonical instant의
    # 문자열 사전순 비교로 한다(S4.1 timestamp와는 비교하지 않는다).
    row = connection.execute(
        "SELECT state, expires_at FROM durable_dispatch_leases WHERE ticket_id COLLATE BINARY=?",
        (ticket_id,),
    ).fetchone()
    return row is not None and row["state"] == "leased" and row["expires_at"] > now_text


def _delivery_attempts(connection: sqlite3.Connection, *, ticket_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM durable_dispatch_delivery_attempts WHERE ticket_id COLLATE BINARY=?",
        (ticket_id,),
    ).fetchone()
    return int(row[0])


# S5.1/S5.2/S5.3의 canonical UTC instant 문법(고정폭·고정 `+00:00`)을 이
# 모듈 로컬로 재현한다(`sqlite_durable_dispatch_lease_uow.py`·
# `durable_dispatch_delivery.py`와 동형) — S4.1 timestamp와는 비교하지 않는다.
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00\Z"
)


def _instant(value: datetime) -> str:
    # lease.expires_at는 S5.1/S5.2가 쓴 fixed-width UTC canonical instant다
    # (문자열 사전순=시간순). `now`를 같은 문법(UTC·마이크로초 6자리·
    # `+00:00`)으로 렌더링해야만 그 컬럼과 문자열 비교가 시간 비교와
    # 같아진다 — 다른 오프셋으로 렌더링하면 사전순이 깨진다(review-s55b
    # P1-2: `astimezone(UTC)`가 정확성의 핵심이고 장식이 아니다 — tz-aware지만
    # 비-UTC offset인 `now`(예: `+09:00`)도 정당한 입력이므로 반드시 UTC로
    # 정규화한 뒤 렌더링해야 한다).
    if type(value) is not datetime or value.utcoffset() is None:
        raise DispatchTimeoutScanError("dispatch timeout scan now에는 tz-aware datetime이 필요합니다.")
    rendered = value.astimezone(UTC).isoformat(timespec="microseconds")
    if _TIMESTAMP_RE.fullmatch(rendered) is None:
        raise DispatchTimeoutScanError("dispatch timeout scan now canonical instant 형식이 올바르지 않습니다.")
    return rendered


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _candidates(
    connection: sqlite3.Connection, *, org_id: str | None, now: datetime, now_text: str
) -> tuple[DispatchTimeoutCandidate, ...]:
    clause, args = _org_clause(org_id)
    tickets = connection.execute(
        f"SELECT * FROM durable_linked_work_tickets WHERE status='pending'{clause}", args
    ).fetchall()

    found: list[DispatchTimeoutCandidate] = []
    for ticket in tickets:
        request = _select_question_request_no_commit(connection, ticket["request_id"])
        if (
            request is None
            or request.org_id != ticket["org_id"]
            or not isinstance(request.state, AwaitingAnswer)
            or request.state.ticket_id != ticket["ticket_id"]
            or request.revision != ticket["awaiting_revision"] + 1
        ):
            continue
        due_at = request.state.handling.due_at
        if due_at > now:
            continue
        found.append(
            DispatchTimeoutCandidate(
                ticket_id=ticket["ticket_id"],
                org_id=ticket["org_id"],
                request_id=ticket["request_id"],
                attempt=ticket["attempt"],
                owner_subject_id=ticket["owner_subject_id"],
                awaiting_revision=ticket["awaiting_revision"],
                request_revision=request.revision,
                due_at=due_at,
                lease_active=_lease_active(connection, ticket_id=ticket["ticket_id"], now_text=now_text),
                delivery_attempts=_delivery_attempts(connection, ticket_id=ticket["ticket_id"]),
            )
        )
    found.sort(key=lambda candidate: (candidate.due_at, candidate.ticket_id))
    return tuple(found)


def _uncertain_report(
    detail: str, *, present: bool, scanned_at: datetime | None
) -> DispatchTimeoutScanReport:
    return DispatchTimeoutScanReport(False, detail, present, scanned_at, ())


def scan_sqlite_dispatch_timeouts(
    db_path: str | Path, *, now: datetime, org_id: str | None = None
) -> DispatchTimeoutScanReport:
    """SLA(``handling.due_at``)를 넘긴 pending WorkTicket을 read-only로 관찰한다.

    write·repair·cursor·scheduler·lease·전이는 없다. capability가 서지
    않으면(스키마 손상·BUSY·경로 오류) ``capable=False``·``candidates=()``로
    fail-closed 닫는다 — 후보 존재는 손상이 아니라 정상 관찰이므로 이는
    S4.6의 ``capable ⟺ violation 0``과 다른 의미다(모듈 docstring 참조).

    ``now``를 canonical UTC로 정규화한 값을 ``scanned_at``에 그대로 싣는다
    — 두 번째 clock을 읽지 않는다(``DispatchTimeoutScanReport`` 참조). 이
    정규화 자체가 실패하면(tz-naive 등) capability도 서지 않으므로
    ``scanned_at=None``이다.
    """
    try:
        now_text = _instant(now)
    except DispatchTimeoutScanError as error:
        return _uncertain_report(str(error), present=False, scanned_at=None)
    scanned_at = _parse_instant(now_text)
    try:
        connection = _open(db_path)
    except DispatchTimeoutScanError as error:
        return _uncertain_report(str(error), present=False, scanned_at=scanned_at)
    connection.row_factory = sqlite3.Row
    present = False
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _manifest_present(connection)
        # SLA 판별과 lease_active·delivery_attempts는 서로 다른 시점을 보면
        # 안 되므로 한 deferred read transaction으로 ticket·lease·attempt·
        # Request 전체를 같은 스냅샷에서 묶는다(S4.6과 동형).
        connection.execute("BEGIN")
        try:
            validate_sqlite_durable_dispatch_delivery_connection(connection, org_id=org_id)
            candidates = _candidates(connection, org_id=org_id, now=now, now_text=now_text)
        finally:
            connection.execute("COMMIT")
        return DispatchTimeoutScanReport(True, "capable_v1", present, scanned_at, candidates)
    except Exception as error:
        return _uncertain_report(str(error), present=present, scanned_at=scanned_at)
    finally:
        connection.close()
