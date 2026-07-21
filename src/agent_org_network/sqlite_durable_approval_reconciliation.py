"""One-shot durable expiry reconciliation (P17.9 S3.2d).

This module is intentionally *not* a scheduler, queue, lease, or recovery
worker.  It takes one read snapshot of due open v2 assignments and invokes the
already-authoritative expiry command once for each member of that snapshot.
There is no persisted cursor, scan authority, completion cache, or attempt to
infer a command that did not leave a lifecycle receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import ApprovalItem
from agent_org_network.central_authority import AuthenticatedPrincipal, CentralAuthorizer
from agent_org_network.sqlite_approval_assignments_v2 import decode_approval_assignment_v2
from agent_org_network.sqlite_durable_approval_expiry import (
    ApprovalExpiryPolicy,
    DurableApprovalExpiryConflict,
    DurableApprovalExpiryResult,
    DurableApprovalExpiryUnavailable,
    DurableApprovalExpiryUnitOfWork,
)
from agent_org_network.sqlite_durable_approval_lifecycle import (
    DatabaseClock,
    validate_lifecycle_canonical_timestamp,
    validate_lifecycle_opaque_identifier,
    validate_sqlite_durable_approval_lifecycle_connection,
)


class DurableApprovalExpiryReconciliationError(RuntimeError):
    """The one-shot reconciliation invocation cannot safely continue."""


@dataclass(frozen=True)
class DurableApprovalExpiryReconciliationOutcome:
    predecessor_item_id: str
    kind: Literal["reassigned", "unavailable", "conflict", "dependency"]
    result: DurableApprovalExpiryResult | None = None


@dataclass(frozen=True)
class DurableApprovalExpiryReconciliationReport:
    database_time: datetime
    outcomes: tuple[DurableApprovalExpiryReconciliationOutcome, ...]


class DurableApprovalExpiryReconciler:
    """Read a bounded, deterministic due snapshot and dispose it item by item.

    The snapshot transaction is read-only.  Every returned item is subsequently
    processed by ``DurableApprovalExpiryUnitOfWork.expire``, which owns a fresh
    atomic write transaction and repeats current-resource authorization.  New
    successors are deliberately absent from the immutable initial list.
    """

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        central_authorizer: CentralAuthorizer | None,
        expiry_policy: ApprovalExpiryPolicy | None,
        database_clock: DatabaseClock[SqliteCompletionTransaction] | None,
        receipt_id_factory: Callable[[], str],
        assignment_id_factory: Callable[[], str],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._tx = completion.durable_transaction()
        self._completion = completion
        self._central = central_authorizer
        self._policy = expiry_policy
        self._clock = database_clock
        self._receipt_id_factory = receipt_id_factory
        self._assignment_id_factory = assignment_id_factory
        self._fault = fault_injector
        # Principal scope is not known yet. Check shared schema only; row
        # validation is deferred until the caller supplies its organization.
        self._require_schema()

    def reconcile(
        self, *, principal: AuthenticatedPrincipal, limit: int
    ) -> DurableApprovalExpiryReconciliationReport:
        if type(principal) is not AuthenticatedPrincipal:
            raise DurableApprovalExpiryReconciliationError("exact service principal이 필요합니다.")
        self._principal_identifier(principal.org_id, "principal.org_id")
        self._principal_identifier(principal.subject_id, "principal.subject_id")
        if type(limit) is not int or limit <= 0:
            raise DurableApprovalExpiryReconciliationError("limit은 exact positive integer여야 합니다.")
        now, item_ids = self._due_snapshot(principal.org_id, limit)
        outcomes: list[DurableApprovalExpiryReconciliationOutcome] = []
        for item_id in item_ids:
            command = DurableApprovalExpiryUnitOfWork(
                completion=self._completion,
                central_authorizer=self._central,
                expiry_policy=self._policy,
                database_clock=self._clock,
                receipt_id_factory=self._receipt_id_factory,
                assignment_id_factory=self._assignment_id_factory,
                fault_injector=self._fault,
                validation_org_id=principal.org_id,
            )
            try:
                result = command.expire(principal=principal, predecessor_item_id=item_id)
            except DurableApprovalExpiryConflict:
                outcomes.append(DurableApprovalExpiryReconciliationOutcome(item_id, "conflict"))
            except DurableApprovalExpiryUnavailable as error:
                # A policy/authority/storage dependency affects this item only.
                # But a now-invalid component means this scan cannot distinguish
                # a partial/corrupt durable snapshot from a transient dependency.
                self._require_capability(principal.org_id, error)
                outcomes.append(DurableApprovalExpiryReconciliationOutcome(item_id, "dependency"))
            except Exception as error:
                # Policies and adapters are intentionally untrusted dependencies.
                # Revalidate before isolating their failure so a corruption that
                # surfaced through an unexpected exception is never skipped.
                self._require_capability(principal.org_id, error)
                outcomes.append(DurableApprovalExpiryReconciliationOutcome(item_id, "dependency"))
            else:
                kind: Literal["reassigned", "unavailable"] = (
                    "unavailable" if result.failure_code is not None else "reassigned"
                )
                outcomes.append(DurableApprovalExpiryReconciliationOutcome(item_id, kind, result))
        return DurableApprovalExpiryReconciliationReport(now, tuple(outcomes))

    def _due_snapshot(self, principal_org_id: str, limit: int) -> tuple[datetime, tuple[str, ...]]:
        if self._clock is None:
            raise DurableApprovalExpiryReconciliationError("DatabaseClock이 없습니다.")
        try:
            with self._tx.scope():
                with self._tx.read_scope():
                    self._tx.validate_component_in_transaction(
                        lambda connection: validate_sqlite_durable_approval_lifecycle_connection(
                            connection, org_id=principal_org_id
                        )
                    )
                    now = self._clock.now(self._tx)
                    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
                        raise DurableApprovalExpiryReconciliationError(
                            "DB time이 timezone-aware instant가 아닙니다."
                        )
                    validate_lifecycle_canonical_timestamp(now.isoformat(), name="database_time")
                    rows = self._tx.execute(
                        "SELECT assignment_id, assignment_json, assignment_sha256, org_id, request_id "
                        "FROM durable_approval_assignments_v2 "
                        "WHERE status='open' AND org_id COLLATE BINARY=? "
                        "ORDER BY assignment_id COLLATE BINARY"
                    , (principal_org_id,)).fetchall()
                    due: list[ApprovalItem] = []
                    for row in rows:
                        item = decode_approval_assignment_v2(
                            assignment_json=row["assignment_json"],
                            assignment_sha256=row["assignment_sha256"],
                            org_id=row["org_id"],
                            request_id=row["request_id"],
                        )
                        if item.status != "open":
                            raise DurableApprovalExpiryReconciliationError(
                                "open v2 projection이 canonical assignment와 다릅니다."
                            )
                        if item.due_at <= now:
                            due.append(item)
                    due.sort(key=lambda item: (item.due_at, item.item_id))
                    return now, tuple(item.item_id for item in due[:limit])
        except DurableApprovalExpiryReconciliationError:
            raise
        except Exception as error:
            raise DurableApprovalExpiryReconciliationError(
                "due snapshot/capability가 canonical하지 않습니다."
            ) from error

    @staticmethod
    def _principal_identifier(value: object, name: str) -> str:
        try:
            return validate_lifecycle_opaque_identifier(value, name=name)
        except Exception as error:
            raise DurableApprovalExpiryReconciliationError(
                f"{name}이 canonical opaque identifier가 아닙니다."
            ) from error

    def _require_schema(self) -> None:
        try:
            with self._tx.scope():
                self._tx.validate_component(
                    lambda connection: validate_sqlite_durable_approval_lifecycle_connection(
                        connection, reconcile_rows=False
                    )
                )
        except Exception as error:
            raise DurableApprovalExpiryReconciliationError(
                "durable lifecycle schema capability가 canonical하지 않습니다."
            ) from error

    def _require_capability(self, org_id: str, cause: Exception | None = None) -> None:
        try:
            with self._tx.scope():
                self._tx.validate_component(
                    lambda connection: validate_sqlite_durable_approval_lifecycle_connection(
                        connection, org_id=org_id
                    )
                )
        except Exception as error:
            message = "durable lifecycle capability가 canonical하지 않습니다."
            if cause is not None:
                raise DurableApprovalExpiryReconciliationError(message) from cause
            raise DurableApprovalExpiryReconciliationError(message) from error
