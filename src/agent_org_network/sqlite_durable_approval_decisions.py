"""P17.9 S3.1: v2 assignment의 durable Approval decision UoW.

이 capability는 v1/in-memory ApprovalStore를 authority로 사용하지 않는다.  승인
결의와 기존 SQLite Completion artifact를 *같은 connection의 한 transaction*에
기록한다. 외부 발행, lease, 재지정은 의도적으로 이 슬라이스 밖이다.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Final, Protocol

from agent_org_network.answer_finalization import AnswerCompletion
from agent_org_network.answer_finalization_sqlite import (
    CompletionTransactionContext,
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import (
    Approve,
    ApproveWithEdit,
    ApprovalAction,
    ApprovalItem,
    ApprovalRejected,
    Reject,
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.question_request import AwaitingApproval, DeclinedRequest, QuestionRequest
from agent_org_network.sqlite_approval_assignments_v2 import (
    SqliteApprovalAssignmentsV2SchemaError,
    decode_approval_assignment_v2,
    encode_approval_assignment_v2,
    validate_sqlite_approval_assignments_v2_connection,
)

_COMPONENT: Final = "durable_approval_decisions_v1"
_VERSION: Final = 1
_MANIFEST_TABLE: Final = "schema_component_manifests"
_RECEIPTS_TABLE: Final = "durable_approval_decision_receipts"
_AUDIT_TABLE: Final = "durable_approval_audit_intents"
_OUTBOX_TABLE: Final = "durable_approval_outbox_intents"
_OWNED_TABLES: Final = (_AUDIT_TABLE, _OUTBOX_TABLE, _RECEIPTS_TABLE)

_RECEIPTS_DDL: Final = """
CREATE TABLE durable_approval_decision_receipts (
    receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    org_id TEXT NOT NULL COLLATE BINARY,
    item_id TEXT NOT NULL UNIQUE COLLATE BINARY,
    request_id TEXT NOT NULL COLLATE BINARY,
    command_digest TEXT NOT NULL UNIQUE COLLATE BINARY,
    principal_id TEXT NOT NULL COLLATE BINARY,
    result_kind TEXT NOT NULL COLLATE BINARY,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES durable_approval_assignments_v2(assignment_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_AUDIT_DDL: Final = """
CREATE TABLE durable_approval_audit_intents (
    receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    org_id TEXT NOT NULL COLLATE BINARY,
    action TEXT NOT NULL COLLATE BINARY,
    request_id TEXT NOT NULL COLLATE BINARY,
    item_id TEXT NOT NULL COLLATE BINARY,
    command_digest TEXT NOT NULL COLLATE BINARY,
    created_at TEXT NOT NULL,
    FOREIGN KEY (receipt_id) REFERENCES durable_approval_decision_receipts(receipt_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_OUTBOX_DDL: Final = """
CREATE TABLE durable_approval_outbox_intents (
    receipt_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    org_id TEXT NOT NULL COLLATE BINARY,
    kind TEXT NOT NULL COLLATE BINARY,
    request_id TEXT NOT NULL COLLATE BINARY,
    item_id TEXT NOT NULL COLLATE BINARY,
    command_digest TEXT NOT NULL COLLATE BINARY,
    created_at TEXT NOT NULL,
    FOREIGN KEY (receipt_id) REFERENCES durable_approval_decision_receipts(receipt_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""


class DurableApprovalDecisionError(RuntimeError):
    pass


class DurableApprovalDecisionUnavailable(DurableApprovalDecisionError):
    pass


class DurableApprovalDecisionConflict(DurableApprovalDecisionError):
    pass


class DecisionAuthorizer(Protocol):
    def authorize(self, principal: object, action: object, resource: object) -> object: ...
    def verify(
        self, grant: object, principal: object, action: object, resource: object
    ) -> bool: ...


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _ddl_tokens(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise DurableApprovalDecisionUnavailable("durable decision SQLite DDL을 읽을 수 없습니다.")
    return " ".join(raw.replace("\n", " ").split()).casefold().rstrip(";").split(" ")


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table_name in _OWNED_TABLES:
        row = connection.execute(
            "SELECT sql FROM main.sqlite_schema WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        if row is None:
            raise DurableApprovalDecisionUnavailable("durable decision canonical table이 없습니다.")
        tables.append(
            {
                "name": table_name,
                "ddl": _ddl_tokens(row[0]),
                "columns": [
                    (
                        str(column["name"]),
                        str(column["type"]),
                        bool(column["notnull"]),
                        int(column["pk"]),
                    )
                    for column in connection.execute(
                        f'PRAGMA table_xinfo("{table_name}")'
                    ).fetchall()
                ],
                "foreign_keys": [
                    tuple(foreign_key)
                    for foreign_key in connection.execute(
                        f'PRAGMA foreign_key_list("{table_name}")'
                    ).fetchall()
                ],
            }
        )
    return {"component": _COMPONENT, "version": _VERSION, "tables": tables}


@lru_cache(maxsize=1)
def _expected_manifest_json() -> str:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE question_requests (request_id TEXT PRIMARY KEY NOT NULL)")
        connection.execute(
            "CREATE TABLE durable_approval_assignments_v2 (assignment_id TEXT PRIMARY KEY NOT NULL)"
        )
        connection.execute(_RECEIPTS_DDL)
        connection.execute(_AUDIT_DDL)
        connection.execute(_OUTBOX_DDL)
        return _json(_catalog(connection))
    finally:
        connection.close()


def _manifest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    if not _table_exists(connection, _MANIFEST_TABLE):
        raise DurableApprovalDecisionUnavailable("공유 schema manifest table이 없습니다.")
    return connection.execute(
        "SELECT component_id, schema_version, manifest_json, manifest_sha256 "
        "FROM schema_component_manifests WHERE component_id COLLATE BINARY=?",
        (_COMPONENT,),
    ).fetchone()


def _validate_schema(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise DurableApprovalDecisionUnavailable("SQLite foreign_keys=ON이 필요합니다.")
    validate_sqlite_approval_assignments_v2_connection(connection)
    manifest = _manifest(connection)
    expected = _expected_manifest_json()
    if manifest is None:
        raise DurableApprovalDecisionUnavailable("durable decision manifest가 없습니다.")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != _VERSION:
        raise DurableApprovalDecisionUnavailable("durable decision manifest version이 다릅니다.")
    if manifest["manifest_json"] != expected or manifest["manifest_sha256"] != _sha(expected):
        raise DurableApprovalDecisionUnavailable("durable decision manifest digest가 다릅니다.")
    if _json(_catalog(connection)) != expected:
        raise DurableApprovalDecisionUnavailable("durable decision SQLite catalog가 다릅니다.")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise DurableApprovalDecisionUnavailable(
            "durable decision foreign_key_check가 실패했습니다."
        )


def _command_digest(
    principal: AuthenticatedPrincipal, item: ApprovalItem, action: ApprovalAction
) -> str:
    # 본문/수정 본문은 receipt와 audit/outbox에 저장하지 않는다. digest만 command identity다.
    return _sha(
        _json(
            {
                "org": principal.org_id,
                "subject": principal.subject_id,
                "item": item.item_id,
                "generation": [item.awaiting_revision, item.attempt, item.approval_round],
                "action": action.kind,
                "edited_text_sha256": _sha(action.edited_text)
                if isinstance(action, ApproveWithEdit)
                else None,
                "reason": action.reason_code if isinstance(action, Reject) else None,
            }
        )
    )


def migrate_sqlite_durable_approval_decisions_schema(db_path: str | Path) -> None:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        validate_sqlite_approval_assignments_v2_connection(connection)
        existing = _manifest(connection)
        if existing is not None:
            _validate_schema(connection)
            connection.commit()
            return
        if any(_table_exists(connection, table) for table in _OWNED_TABLES):
            raise DurableApprovalDecisionUnavailable(
                "manifest 없는 partial durable decision schema는 복구하지 않습니다."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise DurableApprovalDecisionUnavailable(
                "migration 전 foreign_key_check가 실패했습니다."
            )
        connection.execute(_RECEIPTS_DDL)
        connection.execute(_AUDIT_DDL)
        connection.execute(_OUTBOX_DDL)
        expected = _expected_manifest_json()
        if _json(_catalog(connection)) != expected:
            raise DurableApprovalDecisionUnavailable(
                "migration 결과 catalog가 canonical schema와 다릅니다."
            )
        connection.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES (?,?,?,?)",
            (_COMPONENT, _VERSION, expected, _sha(expected)),
        )
        _validate_schema(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@dataclass(frozen=True)
class DurableApprovalDecisionSchemaReconciliationReport:
    capable: bool
    detail: str
    decision_manifest_present: bool


class _DecisionApprovalReader:
    """Completion planner에만 주는 typed same-transaction assignment reader.

    일반 ``DurableApprovalDecisionUnitOfWork.get``를 재사용하지 않는다. 그래서
    planner/clock/authority 같은 외부 callback이 public reader를 다시 불러도 아직
    commit되지 않은 assignment/Completion 상태를 볼 수 없다.
    """

    def __init__(
        self,
        owner: "DurableApprovalDecisionUnitOfWork",
        context: CompletionTransactionContext,
    ) -> None:
        self._owner = owner
        self._context = context

    def get(self, item_id: str) -> ApprovalItem | None:
        return self._owner._get_in_completion_context(self._context, item_id)

    def get_by_request_attempt(self, request_id: str, attempt: int) -> ApprovalItem | None:
        return self._owner._get_by_request_attempt_in_completion_context(
            self._context, request_id, attempt
        )

    def get_by_request_attempt_round(
        self, request_id: str, attempt: int, approval_round: int
    ) -> ApprovalItem | None:
        return self._owner._get_by_request_attempt_round_in_completion_context(
            self._context, request_id, attempt, approval_round
        )


def reconcile_sqlite_durable_approval_decisions_schema(
    db_path: str | Path,
) -> DurableApprovalDecisionSchemaReconciliationReport:
    """기존 DB를 read-only로 열어 decision capability만 판정한다.

    drift를 발견해도 manifest/catalog/row를 보정하지 않는다. 운영 진단이 권한 원천을
    변경하지 않는다는 계약을 테스트 가능한 API로 둔다.
    """
    raw = str(db_path)
    if raw in {"", ":memory:"}:
        return DurableApprovalDecisionSchemaReconciliationReport(
            False, "reconciliation은 기존 SQLite 파일 경로가 필요합니다.", False
        )
    try:
        path = Path(raw).expanduser().resolve(strict=False)
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
    except (OSError, sqlite3.Error) as error:
        return DurableApprovalDecisionSchemaReconciliationReport(False, str(error), False)
    connection.row_factory = sqlite3.Row
    present = False
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        present = _table_exists(connection, _MANIFEST_TABLE) and _manifest(connection) is not None
        _validate_schema(connection)
        return DurableApprovalDecisionSchemaReconciliationReport(True, "capable_v1", present)
    except Exception as error:
        return DurableApprovalDecisionSchemaReconciliationReport(False, str(error), present)
    finally:
        connection.close()


class DurableApprovalDecisionUnitOfWork:
    """서버가 인증한 principal만 받는 actor-free v2 decision boundary."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        central_authorizer: CentralAuthorizer | None,
        clock: Callable[[], datetime],
        receipt_id_factory: Callable[[], str],
    ) -> None:
        self._completion = completion
        self._transaction: SqliteCompletionTransaction = completion.durable_transaction()
        self._authorizer = central_authorizer
        self._clock = clock
        self._receipt_id_factory = receipt_id_factory
        try:
            # Completion runtime opens with a row factory and does no DDL. This validation
            # is also validate-only: a damaged manifest/catalog must never be repaired here.
            with self._transaction.scope():
                self._transaction.validate_component(_validate_schema)
        except (
            sqlite3.Error,
            SqliteApprovalAssignmentsV2SchemaError,
            DurableApprovalDecisionError,
        ) as error:
            raise DurableApprovalDecisionUnavailable(
                "v2 durable capability를 열 수 없습니다."
            ) from error

    def decide(
        self, *, principal: AuthenticatedPrincipal, item_id: str, action: ApprovalAction
    ) -> AnswerCompletion | ApprovalRejected:
        if type(principal) is not AuthenticatedPrincipal or type(action) not in {
            Approve,
            ApproveWithEdit,
            Reject,
        }:
            raise DurableApprovalDecisionUnavailable(
                "서버 principal과 exact Approval action이 필요합니다."
            )
        if action.by_approver != principal.subject_id:
            raise DurableApprovalDecisionConflict(
                "명령 actor는 서버 인증 principal과 달라질 수 없습니다."
            )
        # This is the Completion UoW's shared scope, not a decision-local lock:
        # its BEGIN through commit/rollback is serialised with every Completion
        # read and write on the check_same_thread=False connection.
        with self._transaction.scope():
            try:
                self._transaction.begin_immediate()
                # A resolved item is still needed to derive an exact replay digest.
                # Re-check authority before returning that receipt: revoked access may
                # never read a prior decision outcome.
                item = self._item(item_id)
                if principal.org_id != item.org_id:
                    raise DurableApprovalDecisionConflict(
                        "principal과 ApprovalItem 조직이 다릅니다."
                    )
                digest = _command_digest(principal, item, action)
                self._authorize(principal, item)
                replay = self._transaction.execute(
                    "SELECT * FROM durable_approval_decision_receipts WHERE item_id=?", (item_id,)
                ).fetchone()
                if replay is not None:
                    if (
                        replay["command_digest"] != digest
                        or replay["principal_id"] != principal.subject_id
                    ):
                        raise DurableApprovalDecisionConflict(
                            "같은 item의 다른 semantic command는 replay할 수 없습니다."
                        )
                    self._transaction.commit()
                    return self._replay(replay)
                if item.status != "open":
                    raise DurableApprovalDecisionConflict("현재 open v2 assignment가 아닙니다.")
                if action.by_approver != item.requirement.approver_id:
                    raise DurableApprovalDecisionConflict(
                        "명령 actor는 현재 assignment 승인자와 달라질 수 없습니다."
                    )
                request = self._transaction.select_question_request(item.request_id)
                if (
                    request is None
                    or request.org_id != item.org_id
                    or not isinstance(request.state, AwaitingApproval)
                ):
                    raise DurableApprovalDecisionConflict(
                        "현재 Question Request와 v2 assignment가 다릅니다."
                    )
                if not (
                    request.revision == item.awaiting_revision
                    and request.state.draft_ref == item.item_id
                    and request.state.attempt == item.attempt
                    and request.state.route == item.route
                ):
                    raise DurableApprovalDecisionConflict(
                        "stale request/generation decision을 거부합니다."
                    )
                # Re-read the exact live item/request and reauthorize the exact ResourceRef
                # immediately before any write. A revoked or reassigned authorization must
                # leave all decision artifacts at zero.
                item = self._current_open_item(item_id, expected=item)
                request = self._current_awaiting_request(item)
                self._authorize(principal, item)
                now = self._clock()
                if now.tzinfo is None or now >= item.due_at:
                    raise DurableApprovalDecisionConflict("만료된 assignment는 처분할 수 없습니다.")
                resolved = self._resolve(item, action, now)
                body, body_sha = encode_approval_assignment_v2(resolved)
                changed = self._transaction.execute(
                    "UPDATE durable_approval_assignments_v2 SET status=?, assignment_json=?, assignment_sha256=? WHERE assignment_id=? AND status='open'",
                    (resolved.status, body, body_sha, item.item_id),
                ).rowcount
                if changed != 1:
                    raise DurableApprovalDecisionConflict(
                        "commit-time assignment 재확인에 실패했습니다."
                    )
                if isinstance(action, Reject):
                    updated = request.transition(
                        DeclinedRequest(reason_code=action.reason_code), clock=lambda: now
                    )
                    if not self._transaction.compare_and_set_question_request(
                        request.request_id, request.revision, request, updated
                    ):
                        raise DurableApprovalDecisionConflict(
                            "commit-time request revision 재확인에 실패했습니다."
                        )
                    result: AnswerCompletion | ApprovalRejected = ApprovalRejected(
                        request_id=request.request_id, reason_code=action.reason_code
                    )
                else:
                    assert (
                        resolved.resolution is not None
                        and resolved.resolution.approved_candidate is not None
                    )
                    # Existing UoW writes answer/audit/outbox/receipt without starting or committing another transaction.
                    context = self._transaction.completion_context()
                    result = self._completion.complete_in_transaction(
                        resolved.resolution.approved_candidate,
                        transaction_context=context,
                        approval_reader=_DecisionApprovalReader(self, context),
                    )
                receipt_id = self._receipt_id_factory()
                result_json = _json(
                    {
                        "kind": "rejected",
                        "request_id": result.request_id,
                        "reason_code": result.reason_code,
                    }
                    if isinstance(result, ApprovalRejected)
                    else {
                        "kind": "completed",
                        "request_id": result.request_id,
                        "record_id": result.record_id,
                    }
                )
                self._transaction.execute(
                    "INSERT INTO durable_approval_decision_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_id,
                        item.org_id,
                        item.item_id,
                        item.request_id,
                        digest,
                        principal.subject_id,
                        "rejected" if isinstance(result, ApprovalRejected) else "completed",
                        result_json,
                        now.isoformat(),
                    ),
                )
                for table, kind in (
                    ("durable_approval_audit_intents", action.kind),
                    ("durable_approval_outbox_intents", "approval_decided"),
                ):
                    self._transaction.execute(
                        f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
                        (
                            receipt_id,
                            item.org_id,
                            kind,
                            item.request_id,
                            item.item_id,
                            digest,
                            now.isoformat(),
                        ),
                    )
                self._transaction.commit()
                return result
            except Exception:
                if self._transaction.in_transaction:
                    self._transaction.rollback()
                raise

    def _item(self, item_id: str) -> ApprovalItem:
        row = self._transaction.execute(
            "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise DurableApprovalDecisionConflict("v2 assignment가 없습니다.")
        item = decode_approval_assignment_v2(
            assignment_json=row["assignment_json"],
            assignment_sha256=row["assignment_sha256"],
            org_id=row["org_id"],
            request_id=row["request_id"],
        )
        if row["attempt"] != item.attempt or row["awaiting_revision"] != item.awaiting_revision:
            raise DurableApprovalDecisionConflict("v2 assignment indexed mirror가 다릅니다.")
        return item

    def _current_open_item(self, item_id: str, *, expected: ApprovalItem) -> ApprovalItem:
        current = self._item(item_id)
        if current != expected or current.status != "open":
            raise DurableApprovalDecisionConflict(
                "commit-time v2 assignment snapshot이 달라졌습니다."
            )
        return current

    def _current_awaiting_request(self, item: ApprovalItem) -> QuestionRequest:
        request = self._transaction.select_question_request(item.request_id)
        if (
            request is None
            or request.org_id != item.org_id
            or not isinstance(request.state, AwaitingApproval)
            or request.revision != item.awaiting_revision
            or request.state.draft_ref != item.item_id
            or request.state.attempt != item.attempt
            or request.state.route != item.route
        ):
            raise DurableApprovalDecisionConflict(
                "commit-time Question Request와 v2 assignment가 다릅니다."
            )
        return request

    def get(self, item_id: str) -> ApprovalItem | None:
        with self._transaction.scope(), self._transaction.read_scope():
            row = self._transaction.execute(
                "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id=?", (item_id,)
            ).fetchone()
        return (
            None
            if row is None
            else decode_approval_assignment_v2(
                assignment_json=row["assignment_json"],
                assignment_sha256=row["assignment_sha256"],
                org_id=row["org_id"],
                request_id=row["request_id"],
            )
        )

    def _get_in_completion_context(
        self, context: CompletionTransactionContext, item_id: str
    ) -> ApprovalItem | None:
        self._transaction.require_completion_context(context)
        row = self._transaction.execute(
            "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id=?", (item_id,)
        ).fetchone()
        return self._decode_item_or_none(row)

    def get_by_request_attempt(self, request_id: str, attempt: int) -> ApprovalItem | None:
        with self._transaction.scope(), self._transaction.read_scope():
            row = self._transaction.execute(
                "SELECT * FROM durable_approval_assignments_v2 WHERE request_id=? AND attempt=? ORDER BY approval_round DESC LIMIT 1",
                (request_id, attempt),
            ).fetchone()
        return (
            None
            if row is None
            else decode_approval_assignment_v2(
                assignment_json=row["assignment_json"],
                assignment_sha256=row["assignment_sha256"],
                org_id=row["org_id"],
                request_id=row["request_id"],
            )
        )

    def _get_by_request_attempt_in_completion_context(
        self, context: CompletionTransactionContext, request_id: str, attempt: int
    ) -> ApprovalItem | None:
        self._transaction.require_completion_context(context)
        row = self._transaction.execute(
            "SELECT * FROM durable_approval_assignments_v2 WHERE request_id=? AND attempt=? ORDER BY approval_round DESC LIMIT 1",
            (request_id, attempt),
        ).fetchone()
        return self._decode_item_or_none(row)

    def get_by_request_attempt_round(
        self, request_id: str, attempt: int, approval_round: int
    ) -> ApprovalItem | None:
        with self._transaction.scope(), self._transaction.read_scope():
            row = self._transaction.execute(
                "SELECT * FROM durable_approval_assignments_v2 WHERE request_id=? AND attempt=? AND approval_round=?",
                (request_id, attempt, approval_round),
            ).fetchone()
        return (
            None
            if row is None
            else decode_approval_assignment_v2(
                assignment_json=row["assignment_json"],
                assignment_sha256=row["assignment_sha256"],
                org_id=row["org_id"],
                request_id=row["request_id"],
            )
        )

    def _get_by_request_attempt_round_in_completion_context(
        self,
        context: CompletionTransactionContext,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        self._transaction.require_completion_context(context)
        row = self._transaction.execute(
            "SELECT * FROM durable_approval_assignments_v2 WHERE request_id=? AND attempt=? AND approval_round=?",
            (request_id, attempt, approval_round),
        ).fetchone()
        return self._decode_item_or_none(row)

    @staticmethod
    def _decode_item_or_none(row: sqlite3.Row | None) -> ApprovalItem | None:
        return (
            None
            if row is None
            else decode_approval_assignment_v2(
                assignment_json=row["assignment_json"],
                assignment_sha256=row["assignment_sha256"],
                org_id=row["org_id"],
                request_id=row["request_id"],
            )
        )

    def _authorize(self, principal: AuthenticatedPrincipal, item: ApprovalItem) -> None:
        authorizer = self._authorizer
        resource = ResourceRef(
            org_id=item.org_id,
            kind="approval_item",
            resource_id=item.item_id,
            owner_subject_id=item.requirement.approver_id,
        )
        if authorizer is None:
            raise DurableApprovalDecisionUnavailable("중앙 Authority가 없습니다.")
        grant = authorizer.authorize(principal, "approval.decide", resource)
        if type(grant) is not AuthorizationGrant or not authorizer.verify(
            grant, principal, "approval.decide", resource
        ):
            raise DurableApprovalDecisionConflict("commit-time 중앙 권한이 없습니다.")

    @staticmethod
    def _resolve(item: ApprovalItem, action: ApprovalAction, now: datetime) -> ApprovalItem:
        candidate = None
        if isinstance(action, (Approve, ApproveWithEdit)):
            from agent_org_network.approval import ApprovalAssignmentGeneration, ApprovedCandidate

            source = (
                item.draft.candidate
                if isinstance(action, Approve)
                else item.draft.candidate.model_copy(update={"text": action.edited_text})
            )
            candidate = ApprovedCandidate(
                request_id=item.request_id,
                item_id=item.item_id,
                expected_revision=item.awaiting_revision,
                attempt=item.attempt,
                route=item.route,
                candidate=source,
                approved_by=action.by_approver,
                approved_at=now,
                edited=isinstance(action, ApproveWithEdit),
                policy_version=item.requirement.policy_version,
                assignment_generation=ApprovalAssignmentGeneration.from_item(item),
            )
        return item.resolve(action=action, approved_candidate=candidate, resolved_at=now)

    def _replay(self, row: sqlite3.Row) -> AnswerCompletion | ApprovalRejected:
        payload = json.loads(row["result_json"])
        if payload.get("kind") == "rejected":
            return ApprovalRejected(
                request_id=payload["request_id"], reason_code=payload["reason_code"]
            )
        if payload.get("kind") != "completed":
            raise DurableApprovalDecisionUnavailable("durable decision receipt가 손상됐습니다.")
        request_id = payload.get("request_id")
        record_id = payload.get("record_id")
        if not isinstance(request_id, str) or not isinstance(record_id, str):
            raise DurableApprovalDecisionUnavailable("completed receipt가 손상됐습니다.")
        bundle = self._completion.by_request(request_id)
        if bundle is None or bundle.completion.record_id != record_id:
            raise DurableApprovalDecisionUnavailable(
                "completed receipt가 terminal artifact와 다릅니다."
            )
        return bundle.completion
