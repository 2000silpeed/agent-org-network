"""P17.9 S5.7 read-only dispatch cross-aggregate reconciliation gate.

This gate owns no rows and offers no repair operation.  It opens SQLite with
``mode=ro``, starts a deferred read transaction for one coherent snapshot, and
validates component capabilities before interpreting any cross-component row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_org_network.question_request import AwaitingManager, RouteTarget
from agent_org_network.sqlite_durable_dispatch_delivery import (
    validate_sqlite_durable_dispatch_delivery_connection,
)
from agent_org_network.sqlite_durable_dispatch_escalation import (
    validate_sqlite_durable_dispatch_escalation_connection,
)
from agent_org_network.sqlite_stores import (
    _select_question_request_no_commit,  # pyright: ignore[reportPrivateUsage]
)

type _ViolationKind = Literal[
    "dispatch_capability_uncertain",
    "answer_ticket_mismatch",
    "terminal_outcome_mixed",
    "escalation_item_ticket_mismatch",
    "escalation_receipt_mismatch",
    "disposition_receipt_mismatch",
    "request_state_inconsistent",
]


@dataclass(frozen=True)
class DurableDispatchReconciliationViolation:
    anchor_ref: str
    kind: _ViolationKind
    detail: str


@dataclass(frozen=True)
class DurableDispatchReconciliationReport:
    capable: bool
    detail: str
    mode: Literal["ro+deferred"]
    violations: tuple[DurableDispatchReconciliationViolation, ...]


def _violation(
    anchor: str, kind: _ViolationKind, detail: str
) -> DurableDispatchReconciliationViolation:
    return DurableDispatchReconciliationViolation(anchor, kind, detail)


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
        ).encode()
    ).hexdigest()


def _open(path: str | Path) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise ValueError("dispatch reconciliation은 기존 SQLite 파일만 엽니다.")
    return sqlite3.connect(
        f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode=ro",
        uri=True,
        timeout=5.0,
    )


def _answer_arm(
    connection: sqlite3.Connection,
    violations: list[DurableDispatchReconciliationViolation],
    *,
    org_id: str | None,
) -> None:
    clause = "" if org_id is None else " WHERE t.org_id COLLATE BINARY=?"
    args: tuple[str, ...] = () if org_id is None else (org_id,)
    rows = connection.execute(
        "SELECT t.ticket_id,t.status,count(a.receipt_id) answer_count,"
        "count(i.manager_item_id) item_count "
        "FROM durable_linked_work_tickets t "
        "LEFT JOIN durable_dispatch_answer_receipts a ON a.ticket_id=t.ticket_id "
        "LEFT JOIN durable_dispatch_manager_items i ON i.ticket_id=t.ticket_id"
        f"{clause} GROUP BY t.ticket_id,t.status",
        args,
    ).fetchall()
    for row in rows:
        answer_count = int(row["answer_count"])
        item_count = int(row["item_count"])
        if (row["status"] == "completed") != (answer_count == 1):
            violations.append(
                _violation(
                    row["ticket_id"],
                    "answer_ticket_mismatch",
                    "answer receipt와 completed WorkTicket이 양방향 1:1이 아닙니다.",
                )
            )
        if answer_count and item_count:
            violations.append(
                _violation(
                    row["ticket_id"],
                    "terminal_outcome_mixed",
                    "한 WorkTicket에 answer와 escalation terminal 결과가 혼재합니다.",
                )
            )


def _request_arm(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    item: sqlite3.Row,
    ticket: sqlite3.Row,
    violations: list[DurableDispatchReconciliationViolation],
) -> None:
    request = _select_question_request_no_commit(connection, receipt["request_id"])
    floor = receipt["expected_request_revision"] + 1
    if request is None or request.org_id != receipt["org_id"] or request.revision < floor:
        violations.append(
            _violation(
                receipt["receipt_id"],
                "request_state_inconsistent",
                "Request org/revision floor가 escalation receipt와 결박되지 않습니다.",
            )
        )
        return
    if request.revision != floor:
        return
    state = request.state
    if not (
        isinstance(state, AwaitingManager)
        and state.public_kind == "dispatched"
        and state.item_id == item["manager_item_id"]
        and state.attempt == item["attempt"] == ticket["attempt"]
        and state.route is not None
        and _route_sha256(state.route) == item["route_sha256"]
        and state.handling.kind == "manager_item"
        and state.handling.ref == item["manager_item_id"]
    ):
        violations.append(
            _violation(
                receipt["receipt_id"],
                "request_state_inconsistent",
                "resting revision Request가 정확한 dispatched AwaitingManager shape가 아닙니다.",
            )
        )


def _escalation_arm(
    connection: sqlite3.Connection,
    violations: list[DurableDispatchReconciliationViolation],
    *,
    org_id: str | None,
) -> None:
    where = "" if org_id is None else " WHERE org_id COLLATE BINARY=?"
    args: tuple[str, ...] = () if org_id is None else (org_id,)
    tickets = connection.execute(
        f"SELECT * FROM durable_linked_work_tickets{where}", args
    ).fetchall()
    for ticket in tickets:
        items = connection.execute(
            "SELECT * FROM durable_dispatch_manager_items "
            "WHERE ticket_id COLLATE BINARY=?",
            (ticket["ticket_id"],),
        ).fetchall()
        if (ticket["status"] == "escalated") != (len(items) == 1):
            violations.append(
                _violation(
                    ticket["ticket_id"],
                    "escalation_item_ticket_mismatch",
                    "escalation Item과 escalated WorkTicket이 양방향 1:1이 아닙니다.",
                )
            )
        for item in items:
            if (
                item["org_id"] != ticket["org_id"]
                or item["request_id"] != ticket["request_id"]
                or item["attempt"] != ticket["attempt"]
                or item["awaiting_revision"] != ticket["awaiting_revision"]
                or item["route_sha256"] != ticket["route_sha256"]
                or item["owner_subject_id"] != ticket["owner_subject_id"]
            ):
                violations.append(
                    _violation(
                        item["manager_item_id"],
                        "escalation_item_ticket_mismatch",
                        "escalation Item이 WorkTicket lineage/value와 일치하지 않습니다.",
                    )
                )
            receipts = connection.execute(
                "SELECT * FROM durable_dispatch_escalation_receipts "
                "WHERE manager_item_id COLLATE BINARY=?",
                (item["manager_item_id"],),
            ).fetchall()
            escalate = [r for r in receipts if r["action"] == "work_ticket.escalate"]
            if len(escalate) != 1:
                violations.append(
                    _violation(
                        item["manager_item_id"],
                        "escalation_receipt_mismatch",
                        "Item에는 work_ticket.escalate receipt가 정확히 하나여야 합니다.",
                    )
                )
            else:
                receipt = escalate[0]
                if (
                    receipt["org_id"] != item["org_id"]
                    or receipt["request_id"] != item["request_id"]
                    or receipt["expected_request_revision"] != item["awaiting_revision"] + 1
                ):
                    violations.append(
                        _violation(
                            receipt["receipt_id"],
                            "escalation_receipt_mismatch",
                            "escalation receipt의 lineage/revision이 Item과 다릅니다.",
                        )
                    )
                else:
                    _request_arm(connection, receipt, item, ticket, violations)
            disposition = [r for r in receipts if r["action"] != "work_ticket.escalate"]
            expected = {
                "open": None,
                "resolved": "manager.reroute",
                "dismissed": "manager.dismiss",
            }[item["status"]]
            if expected is None:
                ok = not disposition
            else:
                ok = len(disposition) == 1 and disposition[0]["action"] == expected
            if not ok:
                violations.append(
                    _violation(
                        item["manager_item_id"],
                        "disposition_receipt_mismatch",
                        "Item status와 처분 receipt가 양방향 exact 대응하지 않습니다.",
                    )
                )
    # Reverse disposition/escalation direction, including receipts orphaned by
    # an unexpected ticket relationship (FK only proves an Item parent).
    receipts = connection.execute(
        f"SELECT * FROM durable_dispatch_escalation_receipts{where}", args
    ).fetchall()
    for receipt in receipts:
        item = connection.execute(
            "SELECT status FROM durable_dispatch_manager_items "
            "WHERE manager_item_id COLLATE BINARY=?",
            (receipt["manager_item_id"],),
        ).fetchone()
        expected_status = {
            "work_ticket.escalate": ("open", "resolved", "dismissed"),
            "manager.reroute": ("resolved",),
            "manager.dismiss": ("dismissed",),
        }[receipt["action"]]
        if item is None or item["status"] not in expected_status:
            kind: _ViolationKind = (
                "escalation_receipt_mismatch"
                if receipt["action"] == "work_ticket.escalate"
                else "disposition_receipt_mismatch"
            )
            violations.append(
                _violation(
                    receipt["receipt_id"],
                    kind,
                    "receipt의 reverse Item status 대응이 성립하지 않습니다.",
                )
            )


def reconcile_sqlite_durable_dispatch_gate(
    db_path: str | Path, *, org_id: str | None = None
) -> DurableDispatchReconciliationReport:
    violations: list[DurableDispatchReconciliationViolation] = []
    connection: sqlite3.Connection | None = None
    try:
        connection = _open(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN DEFERRED")
        # capability-first: row interpretation is forbidden before both validate.
        validate_sqlite_durable_dispatch_delivery_connection(connection, org_id=org_id)
        validate_sqlite_durable_dispatch_escalation_connection(connection, org_id=org_id)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        try:
            if connection is not None:
                connection.close()
        except sqlite3.Error:
            pass
        violation = _violation(
            "dispatch-capability",
            "dispatch_capability_uncertain",
            str(error),
        )
        return DurableDispatchReconciliationReport(
            False, "dispatch_capability_uncertain", "ro+deferred", (violation,)
        )
    try:
        _answer_arm(connection, violations, org_id=org_id)
        _escalation_arm(connection, violations, org_id=org_id)
        return DurableDispatchReconciliationReport(
            not violations,
            "capable_v1" if not violations else "cross_aggregate_violation",
            "ro+deferred",
            tuple(violations),
        )
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        violation = _violation(
            "dispatch-capability", "dispatch_capability_uncertain", str(error)
        )
        return DurableDispatchReconciliationReport(
            False, "dispatch_capability_uncertain", "ro+deferred", (violation,)
        )
    finally:
        connection.rollback()
        connection.close()
