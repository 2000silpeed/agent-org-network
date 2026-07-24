"""P17.9 S4.6 linked command-receipt reconciliation gate (ADR 0065 §12).

committed ``durable_linked_command_receipts``(S4.4 ``manager.assign_owner``/
``manager.dismiss``·S4.5 ``work_ticket.create``가 쓰는 유일한 command receipt
테이블)와 target aggregate·Request 상태의 cross-aggregate 정합을
명령·transaction·repair 없이 **read-only**로 증명하는 게이트다. §10
escalation reconciliation(별 component)과 대칭이며, 그 "ManagerItem.status
판별자로 downstream 진행을 손상과 구분한다"는 판을 command receipt 전반으로
일반화한다.

capability(S4.1 linked aggregates·Completion 두 validator)가 서지 않으면
어떤 row도 신뢰하지 않고 즉시 ``linked_capability_uncertain`` 단일
violation으로 닫는다(row 열거 0). capability가 서면 org-필터 row를 첫
위반에서 멈추지 않고 전수 열거한다.

forward(receipt-anchored)는 action으로 대상 aggregate를 결박하고(직접
target은 exact·monotone), 그 aggregate가 가리키는 Request는 tolerant다 —
판별자는 **revision-anchored**다: ``request.revision ==
receipt.expected_request_revision + 1``이면 그 revision을 만든 주인이
유일하게 이 receipt로 결정되므로 exact shape 전부를 요구하고, 그 너머로
진행했으면(revision floor는 이미 확인됨) org만 재확인하는 tolerant다.
Request.state의 kind만으로 판별하지 않는다 — WorkTicket은
``UNIQUE(request_id, attempt)``라 재시도로 같은 request에 여러
ticket/receipt가 정상 공존할 수 있고(dispatched AwaitingManager→
ReadyToDispatch(attempt+1)→AwaitingAnswer 경로), kind만 보면 옛 receipt를
현재 다른 ticket을 가리키는 resting state와 오탐 결박한다. revision은
전이마다 정확히 1씩만 오르므로 이 결정을 유일하게 만든다.
backward(aggregate-anchored)는 처분된 ManagerItem·발급된 WorkTicket이
반드시 command receipt를 가져야 함을 검증한다(open Item은 제외 — 생성은
escalation/ingress 소관).

c.2 escalation receipt graph(§10 소관)·S4.2b concurrence receipt·
ConflictCase는 대상이 아니다. 이 모듈은 repair·write·전이·cursor·scheduler·
lease 표면을 열지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_org_network.question_request import (
    AwaitingAnswer,
    DeclinedRequest,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.sqlite_completion import validate_sqlite_completion_connection
from agent_org_network.sqlite_durable_linked_aggregates import (
    SQLITE_DURABLE_LINKED_AGGREGATES_COMPONENT_ID,
    validate_sqlite_durable_linked_aggregates_connection,
)
from agent_org_network.sqlite_stores import (
    _select_question_request_no_commit,  # pyright: ignore[reportPrivateUsage]
)


class DurableLinkedReconciliationError(RuntimeError):
    """read-only linked reconciliation gate가 안전하게 진행할 수 없습니다."""


type _ViolationKind = Literal[
    "manager_disposition_receipt_mismatch",
    "work_ticket_receipt_mismatch",
    "request_state_inconsistent",
    "unbindable_command_receipt",
    "disposed_item_without_receipt",
    "work_ticket_without_receipt",
    "linked_capability_uncertain",
]

_ASSIGN = "manager.assign_owner"
_DISMISS = "manager.dismiss"
_TICKET = "work_ticket.create"

# (action, target_kind) → 결박 종류. 그 밖의 조합은 이 테이블에 실 writer가
# 없으므로 fail-closed unbindable이다(ADR 0065 §12 Q1① 마지막 bullet).
_BINDABLE: dict[tuple[str, str], Literal["assign", "dismiss", "ticket"]] = {
    (_ASSIGN, "manager_item"): "assign",
    (_DISMISS, "manager_item"): "dismiss",
    (_TICKET, "work_ticket"): "ticket",
}


@dataclass(frozen=True)
class DurableLinkedReconciliationViolation:
    anchor_ref: str
    kind: _ViolationKind
    detail: str


@dataclass(frozen=True)
class DurableLinkedReconciliationReport:
    capable: bool
    detail: str
    linked_aggregates_manifest_present: bool
    violations: tuple[DurableLinkedReconciliationViolation, ...]


def _open(path: str | Path) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise DurableLinkedReconciliationError(
            "linked reconciliation gate는 기존 SQLite 파일만 엽니다."
        )
    try:
        return sqlite3.connect(
            f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as error:
        raise DurableLinkedReconciliationError(
            "linked reconciliation gate SQLite DB를 열 수 없습니다."
        ) from error


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
            (SQLITE_DURABLE_LINKED_AGGREGATES_COMPONENT_ID,),
        ).fetchone()
        is not None
    )


def _org_clause(org_id: str | None) -> tuple[str, tuple[str, ...]]:
    if org_id is None:
        return "", ()
    return "org_id COLLATE BINARY=?", (org_id,)


def _violation(
    anchor_ref: str, kind: _ViolationKind, detail: str
) -> DurableLinkedReconciliationViolation:
    return DurableLinkedReconciliationViolation(anchor_ref, kind, detail)


def _route_sha256(route: RouteTarget) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "agent_id": route.agent_id,
                "authority_version": route.authority_version,
                "intent": route.intent,
                "requires_approval": route.requires_approval,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _request_floor_ok(request: QuestionRequest | None, receipt: sqlite3.Row) -> bool:
    return (
        request is not None
        and request.org_id == receipt["org_id"]
        and request.revision >= receipt["expected_request_revision"] + 1
    )


def _check_assign_request(
    request: QuestionRequest,
    receipt: sqlite3.Row,
    violations: list[DurableLinkedReconciliationViolation],
) -> None:
    # 판별자 = revision-anchored(kind-only 아님). WorkTicket은
    # UNIQUE(request_id, attempt)라 재시도로 같은 request에 여러 ticket이
    # 정상 공존할 수 있고, dispatched AwaitingManager→ReadyToDispatch(attempt+1)
    # 경로로 Request state kind가 ready_to_dispatch로 다시 돌아올 수도 있다 —
    # kind만으로는 그 재시도 사이클을 오탐(false positive)한다. revision은
    # 전이마다 정확히 1씩만 오르므로, "resting revision"(expected+1)이 현재
    # revision과 exact 일치할 때만 그 revision을 만든 주인이 바로 이
    # receipt임이 유일하게 결정된다 — 그때만 exact shape를 요구하고, 그
    # 너머로 진행했으면(revision >, floor는 이미 확인됨) tolerant다.
    if request.revision != receipt["expected_request_revision"] + 1:
        return
    ok = (
        isinstance(request.state, ReadyToDispatch)
        and request.state.trigger_key == receipt["receipt_id"]
        and request.state.attempt == 1
        and request.state.handling.kind == "system"
        and request.state.handling.ref == receipt["receipt_id"]
    )
    if not ok:
        violations.append(
            _violation(
                receipt["receipt_id"],
                "request_state_inconsistent",
                "미진행 ReadyToDispatch Request가 assign receipt의 정확한 resting shape가 아닙니다.",
            )
        )


def _check_dismiss_request(
    request: QuestionRequest,
    receipt: sqlite3.Row,
    violations: list[DurableLinkedReconciliationViolation],
) -> None:
    # dismiss는 terminal이라 tolerance가 없다 — 항상 exact 결박.
    ok = (
        isinstance(request.state, DeclinedRequest)
        and request.state.reason_code == "manager_declined"
        and request.revision == receipt["expected_request_revision"] + 1
    )
    if not ok:
        violations.append(
            _violation(
                receipt["receipt_id"],
                "request_state_inconsistent",
                "dismiss receipt의 Request가 정확한 DeclinedRequest(manager_declined)가 아닙니다.",
            )
        )


def _check_ticket_request(
    request: QuestionRequest,
    receipt: sqlite3.Row,
    ticket: sqlite3.Row,
    violations: list[DurableLinkedReconciliationViolation],
) -> None:
    # 판별자 = revision-anchored(_check_assign_request와 동형 — WorkTicket의
    # UNIQUE(request_id, attempt) 재시도가 같은 request에 여러 ticket을
    # 정상 공존시키므로, kind만으로는 오탐한다).
    if request.revision != receipt["expected_request_revision"] + 1:
        return
    ok = (
        isinstance(request.state, AwaitingAnswer)
        and request.state.ticket_id == receipt["target_ref"]
        and request.state.attempt == ticket["attempt"]
        and _route_sha256(request.state.route) == ticket["route_sha256"]
        and request.state.handling.kind == "runtime_ticket"
        and request.state.handling.ref == receipt["target_ref"]
    )
    if not ok:
        violations.append(
            _violation(
                receipt["receipt_id"],
                "request_state_inconsistent",
                "미진행 AwaitingAnswer Request가 work_ticket receipt의 정확한 resting shape가 아닙니다.",
            )
        )


def _forward_assign_or_dismiss(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    *,
    expected_status: Literal["resolved", "dismissed"],
    violations: list[DurableLinkedReconciliationViolation],
) -> None:
    item = connection.execute(
        "SELECT * FROM durable_linked_manager_items WHERE manager_item_id COLLATE BINARY=?",
        (receipt["target_ref"],),
    ).fetchone()
    ok = (
        item is not None
        and item["org_id"] == receipt["org_id"]
        and item["request_id"] == receipt["request_id"]
        and item["status"] == expected_status
    )
    if not ok:
        violations.append(
            _violation(
                receipt["receipt_id"],
                "manager_disposition_receipt_mismatch",
                "ManagerItem이 command receipt와 결박되지 않습니다.",
            )
        )
        return
    request = _select_question_request_no_commit(connection, receipt["request_id"])
    if not _request_floor_ok(request, receipt):
        violations.append(
            _violation(
                receipt["receipt_id"],
                "request_state_inconsistent",
                "Request가 없거나 org/revision floor를 만족하지 않습니다.",
            )
        )
        return
    assert request is not None
    if expected_status == "resolved":
        _check_assign_request(request, receipt, violations)
    else:
        _check_dismiss_request(request, receipt, violations)


def _forward_ticket(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    violations: list[DurableLinkedReconciliationViolation],
) -> None:
    ticket = connection.execute(
        "SELECT * FROM durable_linked_work_tickets WHERE ticket_id COLLATE BINARY=?",
        (receipt["target_ref"],),
    ).fetchone()
    ok = (
        ticket is not None
        and ticket["org_id"] == receipt["org_id"]
        and ticket["request_id"] == receipt["request_id"]
        and ticket["awaiting_revision"] == receipt["expected_request_revision"]
        and ticket["status"] in ("pending", "completed", "escalated")
    )
    if not ok:
        violations.append(
            _violation(
                receipt["receipt_id"],
                "work_ticket_receipt_mismatch",
                "WorkTicket이 command receipt와 결박되지 않습니다.",
            )
        )
        return
    request = _select_question_request_no_commit(connection, receipt["request_id"])
    if not _request_floor_ok(request, receipt):
        violations.append(
            _violation(
                receipt["receipt_id"],
                "request_state_inconsistent",
                "Request가 없거나 org/revision floor를 만족하지 않습니다.",
            )
        )
        return
    assert request is not None
    assert ticket is not None
    _check_ticket_request(request, receipt, ticket, violations)


def _forward_sweep(
    connection: sqlite3.Connection,
    *,
    org_id: str | None,
    violations: list[DurableLinkedReconciliationViolation],
) -> None:
    """receipt-anchored — action별 대상 결박(ADR 0065 §12 Q1①)."""
    clause, args = _org_clause(org_id)
    where = f" WHERE {clause}" if clause else ""
    receipts = connection.execute(
        f"SELECT * FROM durable_linked_command_receipts{where} ORDER BY receipt_id COLLATE BINARY",
        args,
    ).fetchall()
    for receipt in receipts:
        bind = _BINDABLE.get((receipt["action"], receipt["target_kind"]))
        if bind is None:
            violations.append(
                _violation(
                    receipt["receipt_id"],
                    "unbindable_command_receipt",
                    "command receipt의 action/target_kind 조합을 결박할 수 없습니다.",
                )
            )
            continue
        if bind == "assign":
            _forward_assign_or_dismiss(
                connection, receipt, expected_status="resolved", violations=violations
            )
        elif bind == "dismiss":
            _forward_assign_or_dismiss(
                connection, receipt, expected_status="dismissed", violations=violations
            )
        else:
            _forward_ticket(connection, receipt, violations)


def _backward_sweep(
    connection: sqlite3.Connection,
    *,
    org_id: str | None,
    violations: list[DurableLinkedReconciliationViolation],
) -> None:
    """aggregate-anchored — 처분/발급의 receipt 필수(ADR 0065 §12 Q1②)."""
    clause, args = _org_clause(org_id)

    item_where = " WHERE status IN ('resolved','dismissed')" + (f" AND {clause}" if clause else "")
    items = connection.execute(
        f"SELECT manager_item_id,org_id,request_id,status FROM durable_linked_manager_items{item_where} "
        "ORDER BY manager_item_id COLLATE BINARY",
        args,
    ).fetchall()
    for item in items:
        expected_action = _ASSIGN if item["status"] == "resolved" else _DISMISS
        receipt = connection.execute(
            "SELECT 1 FROM durable_linked_command_receipts "
            "WHERE target_ref COLLATE BINARY=? AND action COLLATE BINARY=? "
            "AND org_id COLLATE BINARY=? AND request_id COLLATE BINARY=?",
            (item["manager_item_id"], expected_action, item["org_id"], item["request_id"]),
        ).fetchone()
        if receipt is None:
            violations.append(
                _violation(
                    item["manager_item_id"],
                    "disposed_item_without_receipt",
                    "처분된 ManagerItem에 대응하는 command receipt가 없습니다.",
                )
            )

    ticket_where = f" WHERE {clause}" if clause else ""
    tickets = connection.execute(
        f"SELECT ticket_id,org_id,request_id FROM durable_linked_work_tickets{ticket_where} "
        "ORDER BY ticket_id COLLATE BINARY",
        args,
    ).fetchall()
    for ticket in tickets:
        receipt = connection.execute(
            "SELECT 1 FROM durable_linked_command_receipts "
            "WHERE target_ref COLLATE BINARY=? AND action COLLATE BINARY=? "
            "AND org_id COLLATE BINARY=? AND request_id COLLATE BINARY=?",
            (ticket["ticket_id"], _TICKET, ticket["org_id"], ticket["request_id"]),
        ).fetchone()
        if receipt is None:
            violations.append(
                _violation(
                    ticket["ticket_id"],
                    "work_ticket_without_receipt",
                    "WorkTicket에 대응하는 command receipt가 없습니다.",
                )
            )


def _sweep(
    connection: sqlite3.Connection, *, org_id: str | None
) -> tuple[DurableLinkedReconciliationViolation, ...]:
    # capability가 서지 않으면 어떤 row도 신뢰할 수 없다 — 예외는 그대로 올려
    # 호출자가 단일 linked_capability_uncertain·row 열거 0으로 닫는다.
    validate_sqlite_durable_linked_aggregates_connection(connection, org_id=org_id)
    validate_sqlite_completion_connection(connection)

    violations: list[DurableLinkedReconciliationViolation] = []
    _forward_sweep(connection, org_id=org_id, violations=violations)
    _backward_sweep(connection, org_id=org_id, violations=violations)
    return tuple(violations)


def _uncertain_report(detail: str, *, present: bool) -> DurableLinkedReconciliationReport:
    violation = _violation("", "linked_capability_uncertain", detail)
    return DurableLinkedReconciliationReport(False, detail, present, (violation,))


def reconcile_sqlite_durable_linked_gate(
    db_path: str | Path, *, org_id: str | None = None
) -> DurableLinkedReconciliationReport:
    """committed command receipt와 target aggregate·Request의 cross-aggregate
    정합을 read-only로 증명한다. 명령·write·repair·cursor·scheduler·lease는 없다.
    """
    try:
        connection = _open(db_path)
    except DurableLinkedReconciliationError as error:
        return _uncertain_report(str(error), present=False)
    connection.row_factory = sqlite3.Row
    present = False
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _manifest_present(connection)
        # cross-aggregate 정합은 서로 다른 시점을 보면 안 되므로 한 deferred
        # read transaction으로 receipt·ManagerItem·WorkTicket·Request 전
        # sweep을 묶는다.
        connection.execute("BEGIN")
        try:
            violations = _sweep(connection, org_id=org_id)
        finally:
            connection.execute("COMMIT")
        if violations:
            return DurableLinkedReconciliationReport(False, violations[0].detail, present, violations)
        return DurableLinkedReconciliationReport(True, "capable_v1", present, ())
    except Exception as error:
        return _uncertain_report(str(error), present=present)
    finally:
        connection.close()
