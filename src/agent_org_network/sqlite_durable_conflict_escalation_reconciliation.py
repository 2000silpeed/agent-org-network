"""P17.9 S4.3d escalation reconciliation gate(ADR 0065 §10).

c.2(receipt graph)·c.3(escalation UoW)가 committed한 결과가 실제로
cross-aggregate 정합함을 명령·transaction·repair 없이 **read-only**로
증명하는 게이트다. 검증할 불변식(`receipt(org,conflict_id) 존재 ⟺ Case
escalated`·세 aggregate 결박)은 §8/§9가 이미 선언했고, 이 모듈은 그것을
읽어 대조할 뿐이다(자기 ADR 없이 이 보강으로 처리 — S4.2 reconciliation·
S3.2d one-shot reconciliation 선례와 같다).

capability(c.2 receipt graph·S4.1 linked aggregates·Completion 세
validator)가 서지 않으면 어떤 row도 신뢰하지 않고 즉시
`escalation_capability_uncertain` 단일 violation으로 닫는다(row 열거
0). capability가 서면 org-필터 row를 첫 위반에서 멈추지 않고 전수
열거한다. downstream 진행(S4.4 처분 이후 ManagerItem status/Request
revision)은 ManagerItem.status 판별자로 정상 통과시킨다(오탐 0) —
경계는 ADR 0065 §10 Q1③을 따른다.

이 모듈은 repair·write·전이·cursor·scheduler·lease 표면을 열지 않는다.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_org_network.question_request import AwaitingManager, QuestionRequest
from agent_org_network.sqlite_completion import validate_sqlite_completion_connection
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_COMPONENT_ID,
    validate_sqlite_durable_conflict_escalation_receipts_connection,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    validate_sqlite_durable_linked_aggregates_connection,
)
from agent_org_network.sqlite_stores import (
    _select_question_request_no_commit,  # pyright: ignore[reportPrivateUsage]
)


class DurableConflictEscalationReconciliationError(RuntimeError):
    """read-only reconciliation gate가 안전하게 진행할 수 없습니다."""


type _ViolationKind = Literal[
    "receipt_without_escalated_case",
    "escalated_case_without_receipt",
    "manager_item_missing_or_mismatched",
    "request_state_inconsistent",
    "escalation_capability_uncertain",
]


@dataclass(frozen=True)
class DurableConflictEscalationReconciliationViolation:
    conflict_id: str
    kind: _ViolationKind
    detail: str


@dataclass(frozen=True)
class DurableConflictEscalationReconciliationReport:
    capable: bool
    detail: str
    escalation_receipts_manifest_present: bool
    violations: tuple[DurableConflictEscalationReconciliationViolation, ...]


def _source_ref(conflict_id: str) -> str:
    return f"source:{hashlib.sha256(conflict_id.encode('utf-8')).hexdigest()}"


def _open(path: str | Path) -> sqlite3.Connection:
    raw = str(path)
    if raw in {"", ":memory:"}:
        raise DurableConflictEscalationReconciliationError(
            "escalation reconciliation gate는 기존 SQLite 파일만 엽니다."
        )
    try:
        return sqlite3.connect(
            f"{Path(raw).expanduser().resolve(strict=False).as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as error:
        raise DurableConflictEscalationReconciliationError(
            "escalation reconciliation gate SQLite DB를 열 수 없습니다."
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
            (SQLITE_DURABLE_CONFLICT_ESCALATION_RECEIPTS_COMPONENT_ID,),
        ).fetchone()
        is not None
    )


def _org_clause(org_id: str | None) -> tuple[str, tuple[str, ...]]:
    if org_id is None:
        return "", ()
    return "org_id COLLATE BINARY=?", (org_id,)


def _violation(
    conflict_id: str, kind: _ViolationKind, detail: str
) -> DurableConflictEscalationReconciliationViolation:
    return DurableConflictEscalationReconciliationViolation(conflict_id, kind, detail)


def _is_escalation_resting_shape(
    request: QuestionRequest,
    manager_item: sqlite3.Row,
    case: sqlite3.Row,
    *,
    require_revision: bool,
) -> bool:
    state = request.state
    if not isinstance(state, AwaitingManager):
        return False
    ok = (
        state.item_id == manager_item["manager_item_id"]
        and state.public_kind == "contested"
        and state.route is None
        and state.attempt is None
        and state.handling.kind == "manager_item"
        and state.handling.ref == manager_item["manager_item_id"]
    )
    if require_revision:
        ok = ok and request.revision == case["awaiting_revision"] + 2
    return ok


def _check_request_state(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    case: sqlite3.Row,
    manager_item: sqlite3.Row,
    violations: list[DurableConflictEscalationReconciliationViolation],
) -> None:
    request = _select_question_request_no_commit(connection, receipt["request_id"])
    if (
        request is None
        or request.org_id != receipt["org_id"]
        or request.revision < case["awaiting_revision"] + 2
    ):
        violations.append(
            _violation(
                receipt["conflict_id"],
                "request_state_inconsistent",
                "Request가 없거나 org/escalation revision floor를 만족하지 않습니다.",
            )
        )
        return
    status = manager_item["status"]
    if status == "open":
        # 미처분 — Request는 정확한 escalation resting shape여야 한다.
        if not _is_escalation_resting_shape(request, manager_item, case, require_revision=True):
            violations.append(
                _violation(
                    receipt["conflict_id"],
                    "request_state_inconsistent",
                    "미처분 ManagerItem의 Request가 정확한 escalation resting shape가 아닙니다.",
                )
            )
    elif status in ("resolved", "dismissed"):
        # S4.4 처분됨 — Request가 아직 AwaitingManager로 남아 있으면 결박이 성립해야
        # 하지만, 그 너머로 진행했다면(revision floor는 이미 통과) 손상이 아니다.
        if isinstance(request.state, AwaitingManager) and not _is_escalation_resting_shape(
            request, manager_item, case, require_revision=False
        ):
            violations.append(
                _violation(
                    receipt["conflict_id"],
                    "request_state_inconsistent",
                    "처분된 ManagerItem인데 남은 AwaitingManager Request가 escalation 결박과 다릅니다.",
                )
            )
    else:
        violations.append(
            _violation(
                receipt["conflict_id"],
                "request_state_inconsistent",
                "ManagerItem status가 알려진 값이 아닙니다.",
            )
        )


def _forward_sweep(
    connection: sqlite3.Connection,
    *,
    org_id: str | None,
    violations: list[DurableConflictEscalationReconciliationViolation],
) -> None:
    """receipt-anchored — receipt ⟶ Case/ManagerItem/Request(ADR 0065 §10 A)."""
    clause, args = _org_clause(org_id)
    where = f" WHERE {clause}" if clause else ""
    receipts = connection.execute(
        f"SELECT * FROM durable_conflict_escalation_receipts{where} ORDER BY receipt_id COLLATE BINARY",
        args,
    ).fetchall()
    for receipt in receipts:
        case = connection.execute(
            "SELECT awaiting_revision, status FROM durable_linked_conflict_cases "
            "WHERE conflict_id COLLATE BINARY=?",
            (receipt["conflict_id"],),
        ).fetchone()
        if case is None or case["status"] != "escalated":
            violations.append(
                _violation(
                    receipt["conflict_id"],
                    "receipt_without_escalated_case",
                    "receipt에 대응하는 escalated Conflict Case가 없습니다.",
                )
            )

        manager_item = connection.execute(
            "SELECT * FROM durable_linked_manager_items WHERE request_id COLLATE BINARY=?",
            (receipt["request_id"],),
        ).fetchone()
        projection = connection.execute(
            "SELECT target_subject_ref FROM durable_conflict_escalation_result_projections "
            "WHERE receipt_id COLLATE BINARY=?",
            (receipt["receipt_id"],),
        ).fetchone()
        manager_item_ok = (
            manager_item is not None
            and projection is not None
            and manager_item["org_id"] == receipt["org_id"]
            and manager_item["request_id"] == receipt["request_id"]
            and manager_item["awaiting_revision"] == receipt["awaiting_revision"]
            and manager_item["source_kind"] == "deadlock"
            and manager_item["source_ref"] == _source_ref(receipt["conflict_id"])
            and manager_item["manager_subject_id"] == projection["target_subject_ref"]
        )
        if not manager_item_ok:
            violations.append(
                _violation(
                    receipt["conflict_id"],
                    "manager_item_missing_or_mismatched",
                    "FromDeadlock ManagerItem이 receipt graph와 결박되지 않습니다.",
                )
            )

        if case is not None and manager_item is not None:
            _check_request_state(connection, receipt, case, manager_item, violations)


def _backward_sweep(
    connection: sqlite3.Connection,
    *,
    org_id: str | None,
    violations: list[DurableConflictEscalationReconciliationViolation],
) -> None:
    """Case-anchored — escalated Case ⟶ receipt(ADR 0065 §10 B)."""
    clause, args = _org_clause(org_id)
    where = " WHERE status='escalated'" + (f" AND {clause}" if clause else "")
    cases = connection.execute(
        f"SELECT conflict_id, org_id FROM durable_linked_conflict_cases{where} "
        "ORDER BY conflict_id COLLATE BINARY",
        args,
    ).fetchall()
    for case in cases:
        receipt = connection.execute(
            "SELECT 1 FROM durable_conflict_escalation_receipts "
            "WHERE org_id COLLATE BINARY=? AND conflict_id COLLATE BINARY=?",
            (case["org_id"], case["conflict_id"]),
        ).fetchone()
        if receipt is None:
            violations.append(
                _violation(
                    case["conflict_id"],
                    "escalated_case_without_receipt",
                    "escalated Conflict Case에 대응하는 receipt가 없습니다.",
                )
            )


def _sweep(
    connection: sqlite3.Connection, *, org_id: str | None
) -> tuple[DurableConflictEscalationReconciliationViolation, ...]:
    # capability가 서지 않으면 어떤 row도 신뢰할 수 없다 — 예외는 그대로 올려
    # 호출자가 단일 escalation_capability_uncertain·row 열거 0으로 닫는다.
    validate_sqlite_durable_conflict_escalation_receipts_connection(connection, org_id=org_id)
    validate_sqlite_durable_linked_aggregates_connection(connection, org_id=org_id)
    validate_sqlite_completion_connection(connection)

    violations: list[DurableConflictEscalationReconciliationViolation] = []
    _forward_sweep(connection, org_id=org_id, violations=violations)
    _backward_sweep(connection, org_id=org_id, violations=violations)
    return tuple(violations)


def _uncertain_report(detail: str, *, present: bool) -> DurableConflictEscalationReconciliationReport:
    violation = _violation("", "escalation_capability_uncertain", detail)
    return DurableConflictEscalationReconciliationReport(False, detail, present, (violation,))


def reconcile_sqlite_durable_conflict_escalation_gate(
    db_path: str | Path, *, org_id: str | None = None
) -> DurableConflictEscalationReconciliationReport:
    """committed escalation receipt graph와 세 aggregate의 cross-aggregate 정합을
    read-only로 증명한다. 명령·write·repair·cursor·scheduler·lease는 없다.
    """
    try:
        connection = _open(db_path)
    except DurableConflictEscalationReconciliationError as error:
        return _uncertain_report(str(error), present=False)
    connection.row_factory = sqlite3.Row
    present = False
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _manifest_present(connection)
        # cross-aggregate 정합은 서로 다른 시점을 보면 안 되므로 한 deferred read
        # transaction으로 receipt·Case·ManagerItem·Request 전 sweep을 묶는다.
        connection.execute("BEGIN")
        try:
            violations = _sweep(connection, org_id=org_id)
        finally:
            connection.execute("COMMIT")
        if violations:
            return DurableConflictEscalationReconciliationReport(
                False, violations[0].detail, present, violations
            )
        return DurableConflictEscalationReconciliationReport(True, "capable_v1", present, ())
    except Exception as error:
        return _uncertain_report(str(error), present=present)
    finally:
        connection.close()
