# pyright: reportUnknownParameterType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportGeneralTypeIssues=false, reportUnknownLambdaType=false
"""P17.9 S3.2c DB-time durable expiry lifecycle command.

This is intentionally a separate command surface from legacy Approval operations.
It composes only the v2 assignment, lifecycle capability, and Completion typed
transaction ownership seam.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import (
    ApprovalItem,
    ApprovalSupersession,
    ApprovalUnavailable,
    ApprovalUnavailabilityEvidence,
    ReassignExpiredApproval,
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.question_request import AwaitingApproval, FailedRequest
from agent_org_network.sqlite_approval_assignments_v2 import (
    decode_approval_assignment_v2,
    encode_approval_assignment_v2,
)
from agent_org_network.sqlite_durable_approval_lifecycle import (
    DatabaseClock,
    SqliteDurableApprovalLifecycleSchemaError,
    validate_lifecycle_canonical_timestamp,
    validate_lifecycle_opaque_identifier,
    validate_sqlite_durable_approval_lifecycle_connection,
)


class DurableApprovalExpiryError(RuntimeError):
    pass


class DurableApprovalExpiryConflict(DurableApprovalExpiryError):
    pass


class DurableApprovalExpiryUnavailable(DurableApprovalExpiryError):
    pass


class ApprovalExpiryPolicy(Protocol):
    def evaluate(
        self, *, assignment: ApprovalItem, now: datetime
    ) -> ReassignExpiredApproval | ApprovalUnavailable: ...


@dataclass(frozen=True)
class DurableApprovalExpiryResult:
    predecessor_item_id: str
    request_id: str
    request_revision: int
    receipt_id: str
    successor_item_id: str | None = None
    failure_code: str | None = None


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DurableApprovalExpiryUnitOfWork:
    """Evaluate one due v2 assignment at one transaction-scoped DB instant."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        central_authorizer: CentralAuthorizer | None,
        expiry_policy: ApprovalExpiryPolicy | None,
        database_clock: DatabaseClock[SqliteCompletionTransaction] | None,
        receipt_id_factory: callable,
        assignment_id_factory: callable,
        fault_injector: callable | None = None,
        validation_org_id: str | None = None,
    ) -> None:
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._central, self._policy, self._clock = central_authorizer, expiry_policy, database_clock
        self._receipt_id_factory, self._assignment_id_factory = (
            receipt_id_factory,
            assignment_id_factory,
        )
        self._fault = fault_injector or (lambda _p: None)
        self._validation_org_id = validation_org_id
        try:
            with self._tx.scope():
                self._tx.validate_component(self._validate_lifecycle)
        except Exception as error:
            raise DurableApprovalExpiryUnavailable(
                "lifecycle durable capability를 열 수 없습니다."
            ) from error

    def expire(
        self, *, principal: AuthenticatedPrincipal, predecessor_item_id: str
    ) -> DurableApprovalExpiryResult:
        if type(principal) is not AuthenticatedPrincipal:
            raise DurableApprovalExpiryUnavailable("exact service principal이 필요합니다.")
        self._identifier(principal.org_id, "principal.org_id")
        self._identifier(principal.subject_id, "principal.subject_id")
        self._identifier(predecessor_item_id, "predecessor_assignment_id")
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._tx.validate_component_in_transaction(
                    self._validate_lifecycle
                )
                item = self._item(predecessor_item_id)
                self._scope(principal, item)
                self._item_ids(item)
                self._central_authorize(principal, item)
                receipt = self._tx.execute(
                    "SELECT * FROM durable_approval_lifecycle_receipts WHERE predecessor_assignment_id=?",
                    (item.item_id,),
                ).fetchone()
                digest = self._digest(principal, item)
                if receipt is not None:
                    if (
                        receipt["command_digest"] != digest
                        or receipt["principal_id"] != principal.subject_id
                        or receipt["action"] != "approval.expire"
                    ):
                        raise DurableApprovalExpiryConflict(
                            "같은 predecessor의 다른 expiry command는 replay할 수 없습니다."
                        )
                    result = self._result(receipt)
                    self._assert_replay_semantics(item, result)
                    self._tx.commit()
                    return result
                now = self._now_once()
                self._due(item, now)
                request = self._request(item)
                decision = self._decision(item, now)
                # Recheck exact current resource and central authority immediately before writes.
                item = self._current_item(item)
                request = self._request(item)
                grant = self._central_authorize(principal, item)
                if isinstance(decision, ReassignExpiredApproval):
                    self._assert_reassign(decision, item, now)
                    result = self._write_reassign(
                        principal, item, request, decision, grant, now, digest
                    )
                else:
                    self._assert_unavailable(decision, item)
                    result = self._write_unavailable(
                        principal, item, request, decision, grant, now, digest
                    )
                self._fault("before_commit")
                self._tx.commit()
                return result
            except SqliteDurableApprovalLifecycleSchemaError as error:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise DurableApprovalExpiryUnavailable(
                    "lifecycle receipt/capability가 canonical하지 않습니다."
                ) from error
            except Exception:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise

    def _validate_lifecycle(self, connection) -> None:
        validate_sqlite_durable_approval_lifecycle_connection(
            connection, org_id=self._validation_org_id
        )

    def _now_once(self) -> datetime:
        if self._clock is None:
            raise DurableApprovalExpiryUnavailable("DatabaseClock이 없습니다.")
        now = self._clock.now(self._tx)
        if type(now) is not datetime or now.tzinfo is None:
            raise DurableApprovalExpiryConflict("DB time이 timezone-aware instant가 아닙니다.")
        self._timestamp(now, "database_time")
        return now

    def _item(self, item_id: str) -> ApprovalItem:
        row = self._tx.execute(
            "SELECT * FROM durable_approval_assignments_v2 WHERE assignment_id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise DurableApprovalExpiryConflict("v2 predecessor가 없습니다.")
        return decode_approval_assignment_v2(
            assignment_json=row["assignment_json"],
            assignment_sha256=row["assignment_sha256"],
            org_id=row["org_id"],
            request_id=row["request_id"],
        )

    def _current_item(self, expected: ApprovalItem) -> ApprovalItem:
        current = self._item(expected.item_id)
        if current != expected or current.status != "open":
            raise DurableApprovalExpiryConflict("current predecessor가 달라졌습니다.")
        return current

    def _request(self, item: ApprovalItem):
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
            raise DurableApprovalExpiryConflict("현재 Request/generation이 다릅니다.")
        return request

    @staticmethod
    def _scope(p: AuthenticatedPrincipal, i: ApprovalItem) -> None:
        if p.org_id != i.org_id:
            raise DurableApprovalExpiryConflict("principal 조직이 predecessor와 다릅니다.")

    def _central_authorize(self, p: AuthenticatedPrincipal, i: ApprovalItem) -> AuthorizationGrant:
        if self._central is None:
            raise DurableApprovalExpiryUnavailable("중앙 Authority가 없습니다.")
        resource = ResourceRef(
            org_id=i.org_id,
            kind="approval_item",
            resource_id=i.item_id,
            owner_subject_id=i.requirement.approver_id,
        )
        grant = self._central.authorize(p, "approval.expire", resource)
        if type(grant) is not AuthorizationGrant or not self._central.verify(
            grant, p, "approval.expire", resource
        ):
            raise DurableApprovalExpiryConflict("commit-time 중앙 권한이 없습니다.")
        return grant

    def _decision(
        self, item: ApprovalItem, now: datetime
    ) -> ReassignExpiredApproval | ApprovalUnavailable:
        if self._policy is None:
            raise DurableApprovalExpiryUnavailable("expiry policy가 없습니다.")
        raw = self._policy.evaluate(assignment=item, now=now)
        if type(raw) is ReassignExpiredApproval:
            return raw
        if type(raw) is ApprovalUnavailable:
            return raw
        raise DurableApprovalExpiryConflict("sealed expiry policy result가 아닙니다.")

    def _due(self, item: ApprovalItem, now: datetime) -> None:
        self._timestamp(item.due_at, "due_at")
        if item.status != "open" or item.due_at > now:
            raise DurableApprovalExpiryConflict("due assignment가 아닙니다.")

    def _assert_reassign(
        self, d: ReassignExpiredApproval, item: ApprovalItem, now: datetime
    ) -> None:
        if not item.matches_assignment_generation(d.assignment_generation) or d.due_at <= now:
            raise DurableApprovalExpiryConflict("expiry reassign policy generation/due가 다릅니다.")
        self._identifier(d.requirement.approver_id, "target_approver_id")
        self._timestamp(d.due_at, "due_at")
        for n, v in (
            ("policy_version", d.policy_version),
            ("authority_version", d.authority_version),
            ("evidence_ref", d.evidence_ref),
        ):
            self._identifier(v, n)

    def _assert_unavailable(self, d: ApprovalUnavailable, item: ApprovalItem) -> None:
        if not item.matches_assignment_generation(d.assignment_generation):
            raise DurableApprovalExpiryConflict("expiry unavailable policy generation이 다릅니다.")
        for n, v in (
            ("policy_version", d.policy_version),
            ("authority_version", d.authority_version),
            ("evidence_ref", d.evidence_ref),
        ):
            self._identifier(v, n)

    def _write_reassign(
        self, p, item, request, d, grant, now, digest
    ) -> DurableApprovalExpiryResult:
        successor_id = self._new_id(self._assignment_id_factory(), "successor_assignment_id")
        receipt_id = self._new_id(self._receipt_id_factory(), "receipt_id")
        if successor_id == item.item_id:
            raise DurableApprovalExpiryConflict("successor assignment ID가 올바르지 않습니다.")
        old = item.supersede(
            ApprovalSupersession(
                reason="expired",
                successor_item_id=successor_id,
                superseded_at=now,
                policy_version=d.policy_version,
                authority_version=d.authority_version,
                evidence_ref=d.evidence_ref,
                target_approver_id=d.requirement.approver_id,
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
            requirement=d.requirement,
            created_at=now,
            due_at=d.due_at,
            approval_round=item.approval_round + 1,
            supersedes_item_id=item.item_id,
        )
        updated = request.reassign_approval(
            previous_item_id=item.item_id,
            successor_item_id=successor_id,
            due_at=d.due_at,
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
            raise DurableApprovalExpiryConflict("predecessor CAS가 실패했습니다.")
        body, sha = encode_approval_assignment_v2(successor)
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
                sha,
                1,
            ),
        )
        if not self._tx.compare_and_set_question_request(
            request.request_id, request.revision, request, updated
        ):
            raise DurableApprovalExpiryConflict("Request CAS가 실패했습니다.")
        self._fault("after_domain")
        evidence = self._evidence(p, item, grant, d, now)
        result = {
            "action": "approval.expire",
            "evidence_digest": evidence["evidence_digest"],
            "expected_request_revision": item.awaiting_revision,
            "org_id": item.org_id,
            "predecessor_assignment_id": item.item_id,
            "request_id": item.request_id,
            "successor_assignment_id": successor_id,
            "successor_approval_round": successor.approval_round,
            "successor_awaiting_revision": successor.awaiting_revision,
        }
        self._receipt(
            receipt_id, item, p, digest, now, "expiry_reassignment", evidence, "reassigned", result
        )
        return DurableApprovalExpiryResult(
            item.item_id,
            item.request_id,
            updated.revision,
            receipt_id,
            successor_item_id=successor_id,
        )

    def _write_unavailable(
        self, p, item, request, d, grant, now, digest
    ) -> DurableApprovalExpiryResult:
        receipt_id = self._new_id(self._receipt_id_factory(), "receipt_id")
        old = item.close_unavailable(ApprovalUnavailabilityEvidence(decision=d, unavailable_at=now))
        updated = request.transition(
            FailedRequest(error_code="approval_unavailable"), clock=lambda: now
        )
        body, sha = encode_approval_assignment_v2(old)
        if (
            self._tx.execute(
                "UPDATE durable_approval_assignments_v2 SET status=?, assignment_json=?, assignment_sha256=? WHERE assignment_id=? AND status='open'",
                (old.status, body, sha, item.item_id),
            ).rowcount
            != 1
        ):
            raise DurableApprovalExpiryConflict("predecessor CAS가 실패했습니다.")
        if not self._tx.compare_and_set_question_request(
            request.request_id, request.revision, request, updated
        ):
            raise DurableApprovalExpiryConflict("Request CAS가 실패했습니다.")
        self._fault("after_domain")
        evidence = self._evidence(p, item, grant, d, now)
        result = {
            "action": "approval.expire",
            "evidence_digest": evidence["evidence_digest"],
            "expected_request_revision": item.awaiting_revision,
            "org_id": item.org_id,
            "predecessor_assignment_id": item.item_id,
            "request_id": item.request_id,
            "failure_code": "approval_unavailable",
        }
        self._receipt(
            receipt_id, item, p, digest, now, "expiry_unavailable", evidence, "unavailable", result
        )
        return DurableApprovalExpiryResult(
            item.item_id,
            item.request_id,
            updated.revision,
            receipt_id,
            failure_code="approval_unavailable",
        )

    def _evidence(self, p, item, grant, d, now):
        return {
            "action": "approval.expire",
            "authority_digest": _sha(_json(grant.model_dump(mode="json"))),
            "evidence_digest": _sha(d.evidence_ref),
            "expected_request_revision": item.awaiting_revision,
            "org_id": item.org_id,
            "predecessor_assignment_id": item.item_id,
            "principal_id": p.subject_id,
            "request_id": item.request_id,
            "database_time": now.isoformat(),
            "expiry_policy_digest": _sha(_json(d.model_dump(mode="json"))),
        }

    def _receipt(
        self, receipt_id, item, p, digest, now, evidence_kind, evidence, result_kind, result
    ):
        ej = _json(evidence)
        rj = _json(result)
        self._tx.execute(
            "INSERT INTO durable_approval_lifecycle_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                item.org_id,
                item.item_id,
                item.request_id,
                digest,
                p.subject_id,
                "approval.expire",
                item.awaiting_revision,
                now.isoformat(),
            ),
        )
        self._tx.execute(
            "INSERT INTO durable_approval_lifecycle_evidence VALUES (?,?,?,?)",
            (receipt_id, evidence_kind, ej, _sha(ej)),
        )
        self._tx.execute(
            "INSERT INTO durable_approval_lifecycle_results VALUES (?,?,?,?)",
            (receipt_id, result_kind, rj, _sha(rj)),
        )
        for table, kind in (
            ("durable_approval_lifecycle_audit_intents", "approval.expire"),
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

    @staticmethod
    def _digest(p, item):
        return _sha(
            _json(
                {
                    "principal": p.subject_id,
                    "org": p.org_id,
                    "item": item.item_id,
                    "generation": [item.awaiting_revision, item.attempt, item.approval_round],
                }
            )
        )

    @staticmethod
    def _identifier(value: object, name: str) -> str:
        try:
            return validate_lifecycle_opaque_identifier(value, name=name)
        except SqliteDurableApprovalLifecycleSchemaError as e:
            raise DurableApprovalExpiryConflict(f"{name}이 올바르지 않습니다.") from e

    @staticmethod
    def _timestamp(value: datetime, name: str) -> str:
        try:
            return validate_lifecycle_canonical_timestamp(value.isoformat(), name=name)
        except SqliteDurableApprovalLifecycleSchemaError as e:
            raise DurableApprovalExpiryConflict(
                f"{name} timestamp가 canonical하지 않습니다."
            ) from e

    def _item_ids(self, item):
        for n, v in (
            ("item.org_id", item.org_id),
            ("item.request_id", item.request_id),
            ("item.item_id", item.item_id),
            ("item.owner_subject_id", item.requirement.approver_id),
        ):
            self._identifier(v, n)

    def _new_id(self, value, name):
        return self._identifier(value, name)

    def _result(self, row) -> DurableApprovalExpiryResult:
        result = self._tx.execute(
            "SELECT result_json FROM durable_approval_lifecycle_results WHERE receipt_id=?",
            (row["receipt_id"],),
        ).fetchone()
        if result is None:
            raise DurableApprovalExpiryUnavailable(
                "partial lifecycle receipt를 복구할 수 없습니다."
            )
        try:
            payload = cast(dict[str, object], json.loads(result["result_json"]))
        except Exception as e:
            raise DurableApprovalExpiryUnavailable("lifecycle result가 손상됐습니다.") from e
        if payload.get("failure_code") == "approval_unavailable":
            return DurableApprovalExpiryResult(
                row["predecessor_assignment_id"],
                row["request_id"],
                row["expected_request_revision"] + 1,
                row["receipt_id"],
                failure_code="approval_unavailable",
            )
        successor = payload.get("successor_assignment_id")
        revision = payload.get("successor_awaiting_revision")
        if not isinstance(successor, str) or type(revision) is not int:
            raise DurableApprovalExpiryUnavailable("lifecycle result가 손상됐습니다.")
        return DurableApprovalExpiryResult(
            row["predecessor_assignment_id"],
            row["request_id"],
            revision,
            row["receipt_id"],
            successor_item_id=successor,
        )

    def _assert_replay_semantics(
        self, predecessor: ApprovalItem, result: DurableApprovalExpiryResult
    ) -> None:
        """A canonical receipt alone is not enough: bind it to current domain rows."""
        if result.failure_code == "approval_unavailable":
            request = self._tx.select_question_request(predecessor.request_id)
            if (
                predecessor.status != "unavailable"
                or request is None
                or not isinstance(request.state, FailedRequest)
                or request.state.error_code != "approval_unavailable"
                or request.revision != result.request_revision
            ):
                raise DurableApprovalExpiryUnavailable(
                    "expiry unavailable replay lineage가 손상됐습니다."
                )
            return
        if (
            predecessor.status != "superseded"
            or predecessor.supersession is None
            or predecessor.supersession.reason != "expired"
            or predecessor.supersession.successor_item_id != result.successor_item_id
        ):
            raise DurableApprovalExpiryUnavailable(
                "expiry reassignment replay lineage가 손상됐습니다."
            )
