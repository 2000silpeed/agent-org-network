"""P17.9 S3.2b durable manual Approval reassignment.

This deliberately composes the S3.2 lifecycle tables with the Completion typed
transaction seam; it never promotes the legacy in-memory Approval operations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import (
    ApprovalItem,
    ApprovalReassignmentAuthorization,
    ApprovalSupersession,
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.question_request import AwaitingApproval
from agent_org_network.sqlite_approval_assignments_v2 import (
    decode_approval_assignment_v2,
    encode_approval_assignment_v2,
)
from agent_org_network.sqlite_durable_approval_lifecycle import (
    SqliteDurableApprovalLifecycleSchemaError,
    validate_lifecycle_canonical_timestamp,
    validate_lifecycle_opaque_identifier,
    validate_sqlite_durable_approval_lifecycle_connection,
)


class DurableApprovalReassignmentError(RuntimeError):
    pass


class DurableApprovalReassignmentConflict(DurableApprovalReassignmentError):
    pass


class DurableApprovalReassignmentUnavailable(DurableApprovalReassignmentError):
    pass


@dataclass(frozen=True)
class DurableApprovalReassignmentResult:
    predecessor_item_id: str
    successor_item_id: str
    request_id: str
    request_revision: int
    receipt_id: str


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DurableApprovalReassignmentUnitOfWork:
    """One exact predecessor-to-successor reassignment command per lifecycle receipt."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        central_authorizer: CentralAuthorizer | None,
        reassignment_authorizer: Callable[[AuthenticatedPrincipal, ApprovalItem, str], object]
        | None,
        clock: Callable[[], datetime],
        receipt_id_factory: Callable[[], str],
        assignment_id_factory: Callable[[], str],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._central, self._reassignment_authorizer = central_authorizer, reassignment_authorizer
        self._clock, self._receipt_id_factory, self._assignment_id_factory = (
            clock,
            receipt_id_factory,
            assignment_id_factory,
        )
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        try:
            with self._tx.scope():
                self._tx.validate_component(validate_sqlite_durable_approval_lifecycle_connection)
        except Exception as error:
            raise DurableApprovalReassignmentUnavailable(
                "lifecycle durable capability를 열 수 없습니다."
            ) from error

    def reassign(
        self,
        *,
        principal: AuthenticatedPrincipal,
        predecessor_item_id: str,
        target_approver_id: str,
    ) -> DurableApprovalReassignmentResult:
        if type(principal) is not AuthenticatedPrincipal:
            raise DurableApprovalReassignmentUnavailable(
                "서버 principal과 exact reassignment 입력이 필요합니다."
            )
        self._validate_command_identifiers(
            principal=principal,
            predecessor_item_id=predecessor_item_id,
            target_approver_id=target_approver_id,
        )
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                # Open-time validation is insufficient: every command, including
                # replay, must fail closed on a newly-corrupt companion/receipt.
                self._tx.validate_component_in_transaction(
                    validate_sqlite_durable_approval_lifecycle_connection
                )
                item = self._item(predecessor_item_id)
                self._assert_scope(principal, item)
                self._validate_item_identifiers(item)
                authorization = self._authorization(principal, item, target_approver_id)
                self._assert_authorization(authorization, principal, item, target_approver_id)
                digest = self._digest(principal, item, authorization)
                self._central_authorize(principal, item)
                replay = self._tx.execute(
                    "SELECT * FROM durable_approval_lifecycle_receipts WHERE predecessor_assignment_id=?",
                    (item.item_id,),
                ).fetchone()
                if replay is not None:
                    if (
                        replay["command_digest"] != digest
                        or replay["principal_id"] != principal.subject_id
                        or replay["action"] != "approval.reassign"
                    ):
                        raise DurableApprovalReassignmentConflict(
                            "같은 predecessor의 다른 command는 replay할 수 없습니다."
                        )
                    result = self._result(replay)
                    self._tx.commit()
                    return result
                request = self._current_request(item)
                self._assert_authorization(authorization, principal, item, target_approver_id)
                # Commit-time reread/reauthorization is intentionally after all planning.
                item = self._current_item(item)
                request = self._current_request(item)
                final_grant = self._central_authorize(principal, item)
                authorization = self._authorization(principal, item, target_approver_id)
                self._assert_authorization(authorization, principal, item, target_approver_id)
                # The committed command identity must be the final authorization
                # snapshot, not a permissive planning-time authorization.
                digest = self._digest(principal, item, authorization)
                now = self._clock()
                if (
                    now.tzinfo is None
                    or authorization.due_at.tzinfo is None
                    or authorization.due_at < now
                ):
                    raise DurableApprovalReassignmentConflict(
                        "재지정 due_at이 현재 transaction 시각보다 빠릅니다."
                    )
                self._validate_timestamp(now, name="database_time")
                self._validate_timestamp(authorization.due_at, name="due_at")
                successor_id = self._assignment_id_factory()
                self._validate_identifier(successor_id, name="successor_assignment_id")
                receipt_id = self._receipt_id_factory()
                self._validate_identifier(receipt_id, name="receipt_id")
                if successor_id == item.item_id:
                    raise DurableApprovalReassignmentConflict(
                        "successor assignment ID가 올바르지 않습니다."
                    )
                old = item.supersede(
                    ApprovalSupersession(
                        reason="reassigned",
                        successor_item_id=successor_id,
                        superseded_at=now,
                        policy_version=authorization.policy_version,
                        authority_version=authorization.authority_version,
                        evidence_ref=authorization.evidence_ref,
                        actor_id=principal.subject_id,
                        target_approver_id=target_approver_id,
                    )
                )
                successor = ApprovalItem(
                    item_id=successor_id,
                    org_id=item.org_id,
                    request_id=item.request_id,
                    awaiting_revision=item.awaiting_revision + 1,
                    attempt=item.attempt,
                    route=item.route,
                    draft=item.draft,
                    requirement=authorization.requirement,
                    created_at=now,
                    due_at=authorization.due_at,
                    approval_round=item.approval_round + 1,
                    supersedes_item_id=item.item_id,
                )
                updated = request.reassign_approval(
                    previous_item_id=item.item_id,
                    successor_item_id=successor_id,
                    due_at=authorization.due_at,
                    clock=lambda: now,
                )
                old_json, old_sha = encode_approval_assignment_v2(old)
                if (
                    self._tx.execute(
                        "UPDATE durable_approval_assignments_v2 SET status=?, assignment_json=?, assignment_sha256=? WHERE assignment_id=? AND status='open'",
                        (old.status, old_json, old_sha, item.item_id),
                    ).rowcount
                    != 1
                ):
                    raise DurableApprovalReassignmentConflict("predecessor CAS가 실패했습니다.")
                body, body_sha = encode_approval_assignment_v2(successor)
                self._tx.execute(
                    "INSERT INTO durable_approval_assignments_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        successor.item_id,
                        successor.org_id,
                        successor.request_id,
                        successor.awaiting_revision,
                        successor.attempt,
                        successor.approval_round,
                        successor.supersedes_item_id,
                        successor.status,
                        body,
                        body_sha,
                        1,
                    ),
                )
                if not self._tx.compare_and_set_question_request(
                    request.request_id, request.revision, request, updated
                ):
                    raise DurableApprovalReassignmentConflict(
                        "Request reassignment CAS가 실패했습니다."
                    )
                self._fault("after_domain")
                evidence = {
                    "action": "approval.reassign",
                    "authority_digest": self._grant_digest(final_grant),
                    "authority_version_digest": _sha(authorization.authority_version),
                    "evidence_digest": _sha(authorization.evidence_ref),
                    "expected_request_revision": item.awaiting_revision,
                    "org_id": item.org_id,
                    "predecessor_assignment_id": item.item_id,
                    "principal_id": principal.subject_id,
                    "request_id": item.request_id,
                    "due_at": authorization.due_at.isoformat(),
                    "policy_digest": _sha(authorization.policy_version),
                    "target_approver_id": target_approver_id,
                    "target_requirement_digest": _sha(
                        _json(authorization.requirement.model_dump(mode="json"))
                    ),
                }
                evidence_json = _json(evidence)
                result = {
                    "action": "approval.reassign",
                    "evidence_digest": evidence["evidence_digest"],
                    "expected_request_revision": item.awaiting_revision,
                    "org_id": item.org_id,
                    "predecessor_assignment_id": item.item_id,
                    "request_id": item.request_id,
                    "successor_assignment_id": successor_id,
                    "successor_approval_round": successor.approval_round,
                    "successor_awaiting_revision": successor.awaiting_revision,
                }
                result_json = _json(result)
                self._tx.execute(
                    "INSERT INTO durable_approval_lifecycle_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_id,
                        item.org_id,
                        item.item_id,
                        item.request_id,
                        digest,
                        principal.subject_id,
                        "approval.reassign",
                        item.awaiting_revision,
                        now.isoformat(),
                    ),
                )
                self._tx.execute(
                    "INSERT INTO durable_approval_lifecycle_evidence VALUES (?,?,?,?)",
                    (receipt_id, "manual_reassignment", evidence_json, _sha(evidence_json)),
                )
                self._tx.execute(
                    "INSERT INTO durable_approval_lifecycle_results VALUES (?,?,?,?)",
                    (receipt_id, "reassigned", result_json, _sha(result_json)),
                )
                for table, kind in (
                    ("durable_approval_lifecycle_audit_intents", "approval.reassign"),
                    ("durable_approval_lifecycle_outbox_intents", "lifecycle_outbox"),
                ):
                    self._tx.execute(
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
                self._fault("before_commit")
                self._tx.commit()
                return DurableApprovalReassignmentResult(
                    item.item_id, successor_id, item.request_id, updated.revision, receipt_id
                )
            except SqliteDurableApprovalLifecycleSchemaError as error:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise DurableApprovalReassignmentUnavailable(
                    "lifecycle receipt/capability가 canonical하지 않습니다."
                ) from error
            except Exception:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise

    def _item(self, item_id: str) -> ApprovalItem:
        row = self._tx.execute(
            "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise DurableApprovalReassignmentConflict("v2 predecessor가 없습니다.")
        return decode_approval_assignment_v2(
            assignment_json=row["assignment_json"],
            assignment_sha256=row["assignment_sha256"],
            org_id=row["org_id"],
            request_id=row["request_id"],
        )

    def _current_item(self, expected: ApprovalItem) -> ApprovalItem:
        current = self._item(expected.item_id)
        if current != expected or current.status != "open":
            raise DurableApprovalReassignmentConflict("current predecessor가 달라졌습니다.")
        return current

    def _current_request(self, item: ApprovalItem):
        request = self._tx.select_question_request(item.request_id)
        if (
            request is None
            or request.org_id != item.org_id
            or not isinstance(request.state, AwaitingApproval)
            or request.revision != item.awaiting_revision
            or request.state.draft_ref != item.item_id
            or request.state.attempt != item.attempt
            or request.state.route != item.route
        ):
            raise DurableApprovalReassignmentConflict("현재 Request/generation이 다릅니다.")
        return request

    def _assert_scope(self, p: AuthenticatedPrincipal, i: ApprovalItem) -> None:
        if p.org_id != i.org_id:
            raise DurableApprovalReassignmentConflict("principal 조직이 predecessor와 다릅니다.")

    def _central_authorize(self, p: AuthenticatedPrincipal, i: ApprovalItem) -> AuthorizationGrant:
        if self._central is None:
            raise DurableApprovalReassignmentUnavailable("중앙 Authority가 없습니다.")
        r = ResourceRef(
            org_id=i.org_id,
            kind="approval_item",
            resource_id=i.item_id,
            owner_subject_id=i.requirement.approver_id,
        )
        g = self._central.authorize(p, "approval.reassign", r)
        if type(g) is not AuthorizationGrant or not self._central.verify(
            g, p, "approval.reassign", r
        ):
            raise DurableApprovalReassignmentConflict("commit-time 중앙 권한이 없습니다.")
        return g

    @staticmethod
    def _grant_digest(grant: AuthorizationGrant) -> str:
        return _sha(_json(grant.model_dump(mode="json")))

    @staticmethod
    def _validate_identifier(value: object, *, name: str) -> str:
        try:
            return validate_lifecycle_opaque_identifier(value, name=name)
        except SqliteDurableApprovalLifecycleSchemaError as error:
            raise DurableApprovalReassignmentConflict(
                f"lifecycle {name} identifier가 올바르지 않습니다."
            ) from error

    def _validate_command_identifiers(
        self,
        *,
        principal: AuthenticatedPrincipal,
        predecessor_item_id: str,
        target_approver_id: str,
    ) -> None:
        for name, value in (
            ("principal.org_id", principal.org_id),
            ("principal.subject_id", principal.subject_id),
            ("predecessor_assignment_id", predecessor_item_id),
            ("target_approver_id", target_approver_id),
        ):
            self._validate_identifier(value, name=name)

    def _validate_item_identifiers(self, item: ApprovalItem) -> None:
        for name, value in (
            ("item.org_id", item.org_id),
            ("item.request_id", item.request_id),
            ("item.item_id", item.item_id),
            ("item.owner_subject_id", item.requirement.approver_id),
        ):
            self._validate_identifier(value, name=name)

    @staticmethod
    def _validate_timestamp(value: datetime, *, name: str) -> str:
        try:
            return validate_lifecycle_canonical_timestamp(value.isoformat(), name=name)
        except SqliteDurableApprovalLifecycleSchemaError as error:
            raise DurableApprovalReassignmentConflict(
                f"lifecycle {name} timestamp가 canonical하지 않습니다."
            ) from error

    def _authorization(
        self, p: AuthenticatedPrincipal, i: ApprovalItem, target: str
    ) -> ApprovalReassignmentAuthorization:
        if self._reassignment_authorizer is None:
            raise DurableApprovalReassignmentUnavailable(
                "ApprovalReassignmentAuthorization authorizer가 없습니다."
            )
        value = self._reassignment_authorizer(p, i, target)
        if type(value) is not ApprovalReassignmentAuthorization:
            raise DurableApprovalReassignmentConflict("재지정 authorization이 없습니다.")
        return value

    def _assert_authorization(
        self,
        a: ApprovalReassignmentAuthorization,
        p: AuthenticatedPrincipal,
        i: ApprovalItem,
        target: str,
    ) -> None:
        if (
            a.actor_id != p.subject_id
            or a.org_id != i.org_id
            or a.target_approver_id != target
            or not i.matches_assignment_generation(a.assignment_generation)
        ):
            raise DurableApprovalReassignmentConflict(
                "재지정 authorization이 current generation과 다릅니다."
            )

    def _digest(
        self, p: AuthenticatedPrincipal, i: ApprovalItem, a: ApprovalReassignmentAuthorization
    ) -> str:
        return _sha(
            _json(
                {
                    "principal": p.subject_id,
                    "org": p.org_id,
                    "item": i.item_id,
                    "generation": [i.awaiting_revision, i.attempt, i.approval_round],
                    "target": a.target_approver_id,
                    "requirement": a.requirement.model_dump(mode="json"),
                    "due_at": a.due_at.isoformat(),
                    "policy": a.policy_version,
                    "authority": a.authority_version,
                    "evidence": a.evidence_ref,
                }
            )
        )

    def _result(self, row: sqlite3.Row) -> DurableApprovalReassignmentResult:
        evidence = self._tx.execute(
            "SELECT evidence_json FROM durable_approval_lifecycle_evidence WHERE receipt_id=?",
            (row["receipt_id"],),
        ).fetchone()
        result = self._tx.execute(
            "SELECT result_json FROM durable_approval_lifecycle_results WHERE receipt_id=?",
            (row["receipt_id"],),
        ).fetchone()
        if evidence is None or result is None:
            raise DurableApprovalReassignmentUnavailable(
                "partial lifecycle receipt를 복구할 수 없습니다."
            )
        payload = json.loads(result["result_json"])
        if not isinstance(payload, dict):
            raise DurableApprovalReassignmentUnavailable("lifecycle result가 손상됐습니다.")
        result_payload = cast(dict[str, object], payload)
        successor = result_payload.get("successor_assignment_id")
        revision = result_payload.get("successor_awaiting_revision")
        if not isinstance(successor, str) or type(revision) is not int:
            raise DurableApprovalReassignmentUnavailable("lifecycle result가 손상됐습니다.")
        return DurableApprovalReassignmentResult(
            row["predecessor_assignment_id"],
            successor,
            row["request_id"],
            revision,
            row["receipt_id"],
        )


def _no_fault(_point: str) -> None:
    return None
