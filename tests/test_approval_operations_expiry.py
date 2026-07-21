from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    InMemoryQuestionCompletionUnitOfWork,
    QuestionCompletionReader,
    QuestionCompletionUnitOfWork,
)

from agent_org_network.approval import (
    ApprovalAssignmentGeneration,
    ApprovalAuthorization,
    ApprovalBoundary,
    ApprovalConcurrencyError,
    ApprovalDraft,
    ApprovalExpiryResult,
    ApprovalItem,
    ApprovalReassignmentAuthorization,
    ApprovalReassignmentAuthorizationResult,
    ApprovalReassignmentDenied,
    ApprovalRequired,
    ApprovalSupersession,
    ApprovalUnavailable,
    ApprovalUnavailabilityEvidence,
    ApproverPrincipal,
    AnswerCandidate,
    InMemoryApprovalStore,
    ReassignExpiredApproval,
)
from agent_org_network.approval_operations import (
    ApprovalAnswered,
    ApprovalDeclined,
    ApprovalLifecycleFailure,
    ApprovalMadeUnavailable,
    ApprovalOperationsApplication,
    ApprovalOperationsConflict,
    ApprovalOperationsDependency,
    ApprovalOperationsError,
    ApprovalOperationsIntegrityError,
    ApprovalOperationsInvalid,
    ApprovalOperationsNotFoundOrDenied,
    ApprovalReassigned,
    ApproveIntent,
    ManualApprovalReassignmentTarget,
    RejectIntent,
    _ReassignmentWork,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.approval_evidence import (
    ApprovalEvent,
    ApprovalEventRecorder,
    InMemoryApprovalEventJournal,
)
from agent_org_network.notify import FakeChannel, Notification, NotificationChannel, Notifier
from agent_org_network.p17_manager_disposition import (
    TerminalDeferred,
    TerminalPublished,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingApproval,
    FailedRequest,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)


T0 = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=10)
T2 = T1 + timedelta(minutes=10)
T3 = T2 + timedelta(minutes=10)
ROUTE = RouteTarget(
    intent="refund",
    agent_id="refund-owner",
    requires_approval=True,
    authority_version="route-v1",
)


class _Reader:
    def __init__(self) -> None:
        self.calls = 0
        self.value: object | None = None

    def by_request(self, request_id: str) -> object | None:
        assert request_id == "request-1"
        self.calls += 1
        return self.value


class _Publisher:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once

    def publish_terminal(self, request_id: str) -> TerminalPublished:
        assert request_id == "request-1"
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("lost publisher response")
        return TerminalPublished()


class _ExpiryPolicy:
    def __init__(self) -> None:
        self.calls = 0
        self.result: ApprovalExpiryResult | object | None = None
        self.error: Exception | None = None

    def evaluate(
        self,
        *,
        assignment: ApprovalAssignmentGeneration,
        now: datetime,
    ) -> object:
        assert now >= assignment.due_at
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class _ReassignmentAuthorizer:
    def __init__(self) -> None:
        self.calls = 0
        self.result: ApprovalReassignmentAuthorizationResult | object | None = None
        self.error: Exception | None = None

    def authorize(
        self,
        *,
        assignment: ApprovalAssignmentGeneration,
        principal: ApproverPrincipal,
        target_approver_id: str,
        requested_at: datetime,
    ) -> object:
        assert assignment.item_id == "approval-1"
        assert principal == ApproverPrincipal(org_id="org-1", subject_id="operator-1")
        assert target_approver_id
        assert requested_at == T1
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class _DecisionPolicy:
    def evaluate(self, org_id: str, route: RouteTarget, candidate_mode: str) -> ApprovalRequired:
        del org_id, route, candidate_mode
        return ApprovalRequired(approver_id="alice", policy_version="approval-v1")


class _DecisionAuthorizer:
    def authorize(
        self,
        org_id: str,
        designated_approver_id: str,
        actor_id: str,
        action_kind: str,
        policy_version: str,
    ) -> ApprovalAuthorization | None:
        del action_kind
        if (
            org_id != "org-1"
            or designated_approver_id != actor_id
            or policy_version not in ("approval-v1", "approval-v2")
        ):
            return None
        return ApprovalAuthorization(policy_version=policy_version)


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        del org_id, state_kind
        return started_at + timedelta(hours=1)


class _ResponseLossApprovalStore(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.lose_supersede_once = True

    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]:
        result = super().supersede_and_create_if_open(
            item_id,
            supersession,
            successor,
            expected_generation=expected_generation,
        )
        if self.lose_supersede_once:
            self.lose_supersede_once = False
            raise RuntimeError("lost committed successor")
        return result


class _UnavailableResponseLossStore(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.lose_unavailable_once = True

    def close_unavailable_if_open(
        self,
        item_id: str,
        expected_generation: ApprovalAssignmentGeneration,
        evidence: ApprovalUnavailabilityEvidence,
    ) -> tuple[ApprovalItem, bool]:
        result = super().close_unavailable_if_open(
            item_id,
            expected_generation,
            evidence,
        )
        if self.lose_unavailable_once:
            self.lose_unavailable_once = False
            raise RuntimeError("lost committed unavailable close")
        return result


class _ResolveAfterDueScanStore(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.resolve_once = True

    def due_open(self, now: datetime, limit: int) -> list[ApprovalItem]:
        due = super().due_open(now, limit)
        if self.resolve_once and due:
            self.resolve_once = False
            item = due[0]
            self.close_unavailable_if_open(
                item.item_id,
                ApprovalAssignmentGeneration.from_item(item),
                ApprovalUnavailabilityEvidence(
                    decision=ApprovalUnavailable(
                        assignment_generation=ApprovalAssignmentGeneration.from_item(item),
                        policy_version="expiry-v1",
                        authority_version="org-policy-v1",
                        evidence_ref="concurrent-expiry",
                    ),
                    unavailable_at=now,
                ),
            )
        return due


class _SupersedeAfterDueScanStore(InMemoryApprovalStore):
    supersede_once = True

    def due_open(self, now: datetime, limit: int) -> list[ApprovalItem]:
        due = super().due_open(now, limit)
        if self.supersede_once and due:
            self.supersede_once = False
            item = due[0]
            successor = ApprovalItem(
                item_id="other-winner",
                org_id=item.org_id,
                request_id=item.request_id,
                awaiting_revision=item.awaiting_revision + 1,
                attempt=item.attempt,
                route=item.route,
                draft=item.draft,
                requirement=ApprovalRequired(
                    approver_id="other-fallback",
                    policy_version="approval-v2",
                ),
                created_at=now,
                due_at=T3,
                approval_round=item.approval_round + 1,
                supersedes_item_id=item.item_id,
            )
            self.supersede_and_create_if_open(
                item.item_id,
                ApprovalSupersession(
                    reason="expired",
                    successor_item_id=successor.item_id,
                    superseded_at=now,
                ),
                successor,
                expected_generation=ApprovalAssignmentGeneration.from_item(item),
            )
        return due


class _ForgedClosedGetStore(InMemoryApprovalStore):
    forge_closed_get = False

    def get(self, item_id: str) -> ApprovalItem | None:
        item = super().get(item_id)
        if not self.forge_closed_get or item is None or item.status != "open":
            return item
        assignment = ApprovalAssignmentGeneration.from_item(item)
        return item.close_unavailable(
            ApprovalUnavailabilityEvidence(
                decision=ApprovalUnavailable(
                    assignment_generation=assignment,
                    policy_version="expiry-v1",
                    authority_version="org-policy-v1",
                    evidence_ref="forged-get-only",
                ),
                unavailable_at=item.due_at,
            )
        )


class _ConflictThenRetirementReadLossStore(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.conflict_once = True
        self.lose_retirement_read_once = False

    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]:
        if self.conflict_once:
            self.conflict_once = False
            self.lose_retirement_read_once = True
            raise ApprovalConcurrencyError("simulated Store race")
        return super().supersede_and_create_if_open(
            item_id,
            supersession,
            successor,
            expected_generation=expected_generation,
        )

    def get(self, item_id: str) -> ApprovalItem | None:
        if self.lose_retirement_read_once:
            self.lose_retirement_read_once = False
            raise RuntimeError("retirement read temporarily unavailable")
        return super().get(item_id)


class _ConflictOnceWithoutMutationStore(InMemoryApprovalStore):
    conflict_once = True

    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]:
        if self.conflict_once:
            self.conflict_once = False
            raise ApprovalConcurrencyError("simulated transient conflict")
        return super().supersede_and_create_if_open(
            item_id,
            supersession,
            successor,
            expected_generation=expected_generation,
        )


class _ConflictTwiceWithoutMutationStore(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.remaining_conflicts = 2

    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]:
        if self.remaining_conflicts:
            self.remaining_conflicts -= 1
            raise ApprovalConcurrencyError("simulated repeated transient conflict")
        return super().supersede_and_create_if_open(
            item_id,
            supersession,
            successor,
            expected_generation=expected_generation,
        )


class _StaleCurrentReadStore(InMemoryApprovalStore):
    stale_item_id: str | None = None

    def get_by_request_attempt(self, request_id: str, attempt: int) -> ApprovalItem | None:
        if self.stale_item_id is not None:
            return super().get(self.stale_item_id)
        return super().get_by_request_attempt(request_id, attempt)


class _MissingDirectLifecycleIndexStore(InMemoryApprovalStore):
    """commit 응답 뒤 direct ID index만 유실하는 악성 Store."""

    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]:
        result = super().supersede_and_create_if_open(
            item_id,
            supersession,
            successor,
            expected_generation=expected_generation,
        )
        self._latest.pop(successor.item_id, None)
        return result

    def close_unavailable_if_open(
        self,
        item_id: str,
        expected_generation: ApprovalAssignmentGeneration,
        evidence: ApprovalUnavailabilityEvidence,
    ) -> tuple[ApprovalItem, bool]:
        result = super().close_unavailable_if_open(
            item_id,
            expected_generation,
            evidence,
        )
        self._latest.pop(item_id, None)
        return result


class _MissingPredecessorDirectLifecycleIndexStore(InMemoryApprovalStore):
    """commit 뒤 superseded predecessor의 direct ID index만 유실한다."""

    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]:
        result = super().supersede_and_create_if_open(
            item_id,
            supersession,
            successor,
            expected_generation=expected_generation,
        )
        self._latest.pop(item_id, None)
        return result


class _MalformedLifecycleWriteResultStore(InMemoryApprovalStore):
    def supersede_and_create_if_open(
        self,
        item_id: str,
        supersession: ApprovalSupersession,
        successor: ApprovalItem,
        *,
        expected_generation: ApprovalAssignmentGeneration | None = None,
    ) -> tuple[ApprovalItem, bool]:
        item, _ = super().supersede_and_create_if_open(
            item_id,
            supersession,
            successor,
            expected_generation=expected_generation,
        )
        return (item, "malicious-not-bool")  # type: ignore[return-value]

    def close_unavailable_if_open(
        self,
        item_id: str,
        expected_generation: ApprovalAssignmentGeneration,
        evidence: ApprovalUnavailabilityEvidence,
    ) -> tuple[ApprovalItem, bool]:
        item, _ = super().close_unavailable_if_open(
            item_id,
            expected_generation,
            evidence,
        )
        return (item, {"malicious": True})  # type: ignore[return-value]


class _DroppableDirectLifecycleIndexStore(InMemoryApprovalStore):
    def drop_direct(self, item_id: str) -> None:
        self._latest.pop(item_id, None)


class _TransientCompletedGetStore(InMemoryApprovalStore):
    fail_item_id: str | None = None

    def get(self, item_id: str) -> ApprovalItem | None:
        if self.fail_item_id == item_id:
            self.fail_item_id = None
            raise RuntimeError("completed item read temporarily unavailable")
        return super().get(item_id)


class _ForgedLaterGenerationStore(InMemoryApprovalStore):
    """저장된 첫 successor 뒤의 canonical lineage를 일관되게 위조한다."""

    forge_kind: str | None = None

    def _forged_generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        generations = super().generations(request_id, attempt)
        if self.forge_kind is None or len(generations) != 2:
            return generations
        predecessor, successor = generations
        later_item_id = predecessor.item_id if self.forge_kind == "item_id_reuse" else "approval-3"
        target_approver_id = "mallory" if self.forge_kind == "target_mismatch" else "carol"
        forged_successor = successor.supersede(
            ApprovalSupersession(
                reason="expired",
                successor_item_id=later_item_id,
                superseded_at=successor.due_at,
                policy_version="expiry-v2",
                authority_version="org-policy-v2",
                evidence_ref="forged-lineage",
                target_approver_id=target_approver_id,
            )
        )
        later = ApprovalItem(
            item_id=later_item_id,
            org_id=successor.org_id,
            request_id=successor.request_id,
            awaiting_revision=successor.awaiting_revision + 1,
            attempt=successor.attempt,
            route=successor.route,
            draft=successor.draft,
            requirement=ApprovalRequired(
                approver_id="carol",
                policy_version="approval-v3",
            ),
            created_at=successor.due_at,
            due_at=successor.due_at + timedelta(minutes=10),
            approval_round=successor.approval_round + 1,
            supersedes_item_id=successor.item_id,
        )
        return [predecessor, forged_successor, later]

    def get(self, item_id: str) -> ApprovalItem | None:
        if self.forge_kind is not None:
            generations = self._forged_generations("request-1", 1)
            if len(generations) == 3:
                matches = [item for item in generations if item.item_id == item_id]
                if len(matches) == 1:
                    return matches[0]
        return super().get(item_id)

    def get_by_request_attempt(self, request_id: str, attempt: int) -> ApprovalItem | None:
        generations = self._forged_generations(request_id, attempt)
        if self.forge_kind is not None and len(generations) == 3:
            return generations[-1]
        return super().get_by_request_attempt(request_id, attempt)

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        generations = self._forged_generations(request_id, attempt)
        if self.forge_kind is not None and len(generations) == 3:
            return next(
                (item for item in generations if item.approval_round == approval_round),
                None,
            )
        return super().get_by_request_attempt_round(request_id, attempt, approval_round)

    def generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        return self._forged_generations(request_id, attempt)


class _ForgedDueRetirementStore(_SupersedeAfterDueScanStore):
    """due snapshot loser의 full lineage를 canonical 값으로 위조한다."""

    def __init__(self, forge_kind: str) -> None:
        super().__init__()
        self.forge_kind = forge_kind

    def _forged_generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        generations = super().generations(request_id, attempt)
        if len(generations) != 2:
            return generations
        predecessor, successor = generations
        if self.forge_kind == "target_mismatch":
            assert predecessor.supersession is not None
            forged_predecessor = predecessor.model_copy(
                update={
                    "supersession": ApprovalSupersession(
                        reason="expired",
                        successor_item_id=successor.item_id,
                        superseded_at=predecessor.supersession.superseded_at,
                        policy_version="expiry-v1",
                        authority_version="org-policy-v1",
                        evidence_ref="forged-target",
                        target_approver_id="mallory",
                    )
                }
            )
            return [forged_predecessor, successor]
        forged_successor = successor.supersede(
            ApprovalSupersession(
                reason="expired",
                successor_item_id=predecessor.item_id,
                superseded_at=successor.due_at,
                policy_version="expiry-v2",
                authority_version="org-policy-v2",
                evidence_ref="forged-cycle",
                target_approver_id="carol",
            )
        )
        later = ApprovalItem(
            item_id=predecessor.item_id,
            org_id=successor.org_id,
            request_id=successor.request_id,
            awaiting_revision=successor.awaiting_revision + 1,
            attempt=successor.attempt,
            route=successor.route,
            draft=successor.draft,
            requirement=ApprovalRequired(
                approver_id="carol",
                policy_version="approval-v3",
            ),
            created_at=successor.due_at,
            due_at=successor.due_at + timedelta(minutes=10),
            approval_round=successor.approval_round + 1,
            supersedes_item_id=successor.item_id,
        )
        return [predecessor, forged_successor, later]

    def get(self, item_id: str) -> ApprovalItem | None:
        generations = self._forged_generations("request-1", 1)
        if len(generations) >= 2 and item_id == "approval-1":
            return generations[0]
        return super().get(item_id)

    def get_by_request_attempt(self, request_id: str, attempt: int) -> ApprovalItem | None:
        generations = self._forged_generations(request_id, attempt)
        if len(generations) >= 2:
            return generations[-1]
        return super().get_by_request_attempt(request_id, attempt)

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        generations = self._forged_generations(request_id, attempt)
        if len(generations) >= 2:
            return next(
                (item for item in generations if item.approval_round == approval_round),
                None,
            )
        return super().get_by_request_attempt_round(request_id, attempt, approval_round)

    def generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        return self._forged_generations(request_id, attempt)


class _ForgedEarlierLineageAfterDueScanStore(InMemoryApprovalStore):
    """round 2 due snapshot 뒤 외부 winner와 위조된 round 1 link를 함께 노출한다."""

    def __init__(self) -> None:
        super().__init__()
        self.race_once = True
        self.forge_lineage = False

    def due_open(self, now: datetime, limit: int) -> list[ApprovalItem]:
        due = super().due_open(now, limit)
        if self.race_once and due and due[0].approval_round == 2:
            self.race_once = False
            predecessor = due[0]
            successor = ApprovalItem(
                item_id="approval-3",
                org_id=predecessor.org_id,
                request_id=predecessor.request_id,
                awaiting_revision=predecessor.awaiting_revision + 1,
                attempt=predecessor.attempt,
                route=predecessor.route,
                draft=predecessor.draft,
                requirement=ApprovalRequired(
                    approver_id="carol",
                    policy_version="approval-v3",
                ),
                created_at=now,
                due_at=now + timedelta(minutes=10),
                approval_round=predecessor.approval_round + 1,
                supersedes_item_id=predecessor.item_id,
            )
            super().supersede_and_create_if_open(
                predecessor.item_id,
                ApprovalSupersession(
                    reason="expired",
                    successor_item_id=successor.item_id,
                    superseded_at=now,
                    policy_version="expiry-v2",
                    authority_version="org-policy-v2",
                    evidence_ref="external-winner",
                    target_approver_id="carol",
                ),
                successor,
                expected_generation=ApprovalAssignmentGeneration.from_item(predecessor),
            )
            self.forge_lineage = True
        return due

    def _forged_generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        generations = super().generations(request_id, attempt)
        if not self.forge_lineage or len(generations) != 3:
            return generations
        first = generations[0]
        assert first.supersession is not None
        forged_first = first.model_copy(
            update={
                "supersession": first.supersession.model_copy(
                    update={"target_approver_id": "mallory"}
                )
            }
        )
        return [forged_first, *generations[1:]]

    def get(self, item_id: str) -> ApprovalItem | None:
        generations = self._forged_generations("request-1", 1)
        if self.forge_lineage and len(generations) == 3:
            return next((item for item in generations if item.item_id == item_id), None)
        return super().get(item_id)

    def get_by_request_attempt(self, request_id: str, attempt: int) -> ApprovalItem | None:
        generations = self._forged_generations(request_id, attempt)
        if self.forge_lineage and len(generations) == 3:
            return generations[-1]
        return super().get_by_request_attempt(request_id, attempt)

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        generations = self._forged_generations(request_id, attempt)
        if self.forge_lineage and len(generations) == 3:
            return next(
                (item for item in generations if item.approval_round == approval_round),
                None,
            )
        return super().get_by_request_attempt_round(request_id, attempt, approval_round)

    def generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        return self._forged_generations(request_id, attempt)


class _MissingDueRetirementCurrentDirectStore(_SupersedeAfterDueScanStore):
    def due_open(self, now: datetime, limit: int) -> list[ApprovalItem]:
        due = super().due_open(now, limit)
        current = super().get_by_request_attempt("request-1", 1)
        assert current is not None and current.item_id == "other-winner"
        self._latest.pop(current.item_id, None)
        return due


class _StaleCompletedDueStore(InMemoryApprovalStore):
    return_stale = False
    stale_open: ApprovalItem | None = None

    def create_or_get(self, item: ApprovalItem) -> tuple[ApprovalItem, bool]:
        stored, created = super().create_or_get(item)
        if created and item.item_id == "approval-1":
            self.stale_open = stored
        return stored, created

    def due_open(self, now: datetime, limit: int) -> list[ApprovalItem]:
        if self.return_stale:
            assert self.stale_open is not None and limit > 0
            return [self.stale_open]
        return super().due_open(now, limit)


class _ForgedUnavailableLineageStore(InMemoryApprovalStore):
    forge_lineage = False

    def _forged_generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        generations = super().generations(request_id, attempt)
        if not self.forge_lineage or len(generations) != 2:
            return generations
        predecessor, current = generations
        assert predecessor.supersession is not None
        evidence = predecessor.supersession
        forged_predecessor = predecessor.model_copy(
            update={"supersession": evidence.model_copy(update={"target_approver_id": "mallory"})}
        )
        return [forged_predecessor, current]

    def get(self, item_id: str) -> ApprovalItem | None:
        generations = self._forged_generations("request-1", 1)
        if self.forge_lineage and len(generations) == 2 and item_id == "approval-1":
            return generations[0]
        return super().get(item_id)

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        generations = self._forged_generations(request_id, attempt)
        if self.forge_lineage and len(generations) == 2:
            return next(
                (item for item in generations if item.approval_round == approval_round),
                None,
            )
        return super().get_by_request_attempt_round(request_id, attempt, approval_round)

    def generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        return self._forged_generations(request_id, attempt)


class _DuplicateDueProjectionStore(InMemoryApprovalStore):
    def __init__(self, duplicate_kind: str) -> None:
        super().__init__()
        self.duplicate_kind = duplicate_kind

    def due_open(self, now: datetime, limit: int) -> list[ApprovalItem]:
        due = super().due_open(now, limit)
        if not due:
            return due
        actual = due[0]
        forged = actual.model_copy(
            update={
                "item_id": (
                    actual.item_id if self.duplicate_kind == "item_id" else "approval-forged"
                ),
                "created_at": T0,
                "due_at": T1,
            }
        )
        return [forged, actual]


class _ResponseLossRequestStore(InMemoryQuestionRequestStore):
    def __init__(self) -> None:
        super().__init__()
        self.lose_reassignment_once = True

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        result = super().compare_and_set(request_id, expected_revision, current, updated)
        if result and expected_revision == 2 and self.lose_reassignment_once:
            self.lose_reassignment_once = False
            raise RuntimeError("lost committed request transition")
        return result


class _LyingTrueRequestStore(InMemoryQuestionRequestStore):
    lie_on_reassignment = False

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        if self.lie_on_reassignment and request_id == "request-1" and expected_revision == 2:
            return True
        return super().compare_and_set(request_id, expected_revision, current, updated)


class _ToggleRequestFailureStore(InMemoryQuestionRequestStore):
    fail_reassignment = True

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        if self.fail_reassignment and request_id == "request-1" and expected_revision == 2:
            raise RuntimeError("request transition unavailable")
        return super().compare_and_set(request_id, expected_revision, current, updated)


class _CorruptPredecessorTimestampRequestStore(InMemoryQuestionRequestStore):
    corrupt_reads = False
    reassignment_cas_calls = 0

    def get(self, request_id: str) -> QuestionRequest | None:
        request = super().get(request_id)
        if not self.corrupt_reads or request is None or request.revision != 2:
            return request
        return request.model_copy(update={"updated_at": T0})

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        if request_id == "request-1" and expected_revision == 2:
            self.reassignment_cas_calls += 1
        return super().compare_and_set(request_id, expected_revision, current, updated)


class _PoisonFirstRequestStore(InMemoryQuestionRequestStore):
    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        if request_id == "request-1" and expected_revision == 2:
            raise RuntimeError("request-1 transition remains unavailable")
        return super().compare_and_set(request_id, expected_revision, current, updated)


class _InspectableApprovalOperationsApplication(ApprovalOperationsApplication):
    """Hostile cache tests expose internals through narrow, typed test-only hooks."""

    def pending_reassignment_work(self, item_id: str) -> _ReassignmentWork:
        work = self._lifecycle_work[item_id]
        if type(work) is not _ReassignmentWork:
            raise AssertionError("reassignment work expected")
        return work

    def replace_pending_reassignment_work(
        self,
        item_id: str,
        work: _ReassignmentWork,
    ) -> None:
        self._lifecycle_work[item_id] = work

    def pending_work_entry(self, item_id: str) -> object:
        return self._lifecycle_work[item_id]

    def drop_pending_work(self, item_id: str) -> None:
        self._lifecycle_work.pop(item_id)

    def cached_reassignment_entry(
        self,
        item_id: str,
    ) -> tuple[_ReassignmentWork, ApprovalReassigned]:
        work, outcome = self._lifecycle_results[item_id]
        if type(work) is not _ReassignmentWork or type(outcome) is not ApprovalReassigned:
            raise AssertionError("reassignment cache entry expected")
        return work, outcome

    def replace_cached_entry(self, item_id: str, entry: object) -> None:
        self._lifecycle_results[item_id] = cast(Any, entry)

    def has_cached_result(self, item_id: str) -> bool:
        return item_id in self._lifecycle_results


class _Harness:
    def __init__(
        self,
        *,
        approvals: InMemoryApprovalStore | None = None,
        requests: InMemoryQuestionRequestStore | None = None,
        publisher: _Publisher | None = None,
    ) -> None:
        self.approvals = approvals or InMemoryApprovalStore()
        self.requests = requests or InMemoryQuestionRequestStore()
        received = QuestionRequest.receive(
            org_id="org-1",
            requester_id="requester-1",
            question="환불해 주세요.",
            request_id_factory=lambda: "request-1",
            clock=lambda: T0,
            due_at=T1,
        )
        self.requests.create(received)
        ready = received.record_initial_routing(
            intent=ROUTE.intent,
            disposition="routed",
            target=ReadyToDispatch(
                route=ROUTE,
                attempt=1,
                trigger_key="request-dispatch:request-1:1",
                handling=HandlingAssignment(
                    kind="system",
                    ref="request-dispatch:request-1:1",
                    due_at=T1,
                ),
            ),
            clock=lambda: T0,
        )
        assert self.requests.compare_and_set("request-1", 0, received, ready)
        draft = ApprovalDraft(
            draft_id="draft-1",
            request_id="request-1",
            attempt=1,
            route=ROUTE,
            candidate=AnswerCandidate(
                text="환불할 수 있습니다.",
                sources=("refund.md",),
                mode="full",
                snapshot_sha="sha-1",
            ),
            created_at=T0,
        )
        self.item = ApprovalItem(
            item_id="approval-1",
            org_id="org-1",
            request_id="request-1",
            awaiting_revision=2,
            attempt=1,
            route=ROUTE,
            draft=draft,
            requirement=ApprovalRequired(
                approver_id="alice",
                policy_version="approval-v1",
            ),
            created_at=T1,
            due_at=T2,
        )
        self.approvals.create_or_get(self.item)
        awaiting = ready.transition(
            AwaitingApproval(
                route=ROUTE,
                attempt=1,
                draft_ref=self.item.item_id,
                handling=HandlingAssignment(
                    kind="approval_item",
                    ref=self.item.item_id,
                    due_at=self.item.due_at,
                ),
            ),
            clock=lambda: T1,
        )
        assert self.requests.compare_and_set("request-1", 1, ready, awaiting)
        self.expiry = _ExpiryPolicy()
        self.authorizer = _ReassignmentAuthorizer()
        self.reader = _Reader()
        self.publisher = publisher or _Publisher()
        self.id_calls = 0

        def item_id_factory() -> str:
            self.id_calls += 1
            return f"approval-{self.id_calls + 1}"

        self.item_id_factory = item_id_factory
        self.app = _InspectableApprovalOperationsApplication(
            requests=self.requests,
            approvals=self.approvals,
            boundary=cast(ApprovalBoundary, object()),  # lifecycle tests do not call decide
            completion=cast(QuestionCompletionUnitOfWork, object()),
            reader=cast(QuestionCompletionReader, self.reader),
            terminal_publisher=self.publisher,
            expiry_policy=cast(Any, self.expiry),
            reassignment_authorizer=cast(Any, self.authorizer),
            item_id_factory=self.item_id_factory,
            clock=lambda: T1,
        )

    @property
    def assignment(self) -> ApprovalAssignmentGeneration:
        return ApprovalAssignmentGeneration.from_item(self.item)

    def authorize(self, target: str = "bob") -> None:
        self.authorizer.result = ApprovalReassignmentAuthorization(
            assignment_generation=self.assignment,
            org_id="org-1",
            actor_id="operator-1",
            target_approver_id=target,
            requirement=ApprovalRequired(
                approver_id=target,
                policy_version="approval-v2",
            ),
            due_at=T3,
            policy_version="manual-reassignment-v1",
            authority_version="org-policy-v1",
            evidence_ref="manual-grant-1",
        )

    def expire_reassign(self) -> None:
        self.expiry.result = ReassignExpiredApproval(
            assignment_generation=self.assignment,
            requirement=ApprovalRequired(
                approver_id="fallback-1",
                policy_version="approval-v2",
            ),
            due_at=T3,
            policy_version="expiry-v1",
            authority_version="org-policy-v1",
            evidence_ref="fallback-rule-1",
        )

    def expire_unavailable(self) -> None:
        self.expiry.result = ApprovalUnavailable(
            assignment_generation=self.assignment,
            policy_version="expiry-v1",
            authority_version="org-policy-v1",
            evidence_ref="no-fallback-1",
        )

    def enable_decisions(self) -> None:
        boundary = ApprovalBoundary(
            requests=self.requests,
            approvals=self.approvals,
            policy=_DecisionPolicy(),
            authorizer=_DecisionAuthorizer(),
            deadline_policy=_Deadline(),
            draft_id_factory=lambda: "unused-draft",
            item_id_factory=lambda: "unused-item",
            clock=lambda: T1,
        )
        self.app = _InspectableApprovalOperationsApplication(
            requests=self.requests,
            approvals=self.approvals,
            boundary=boundary,
            completion=cast(QuestionCompletionUnitOfWork, object()),
            reader=cast(QuestionCompletionReader, self.reader),
            terminal_publisher=self.publisher,
            expiry_policy=cast(Any, self.expiry),
            reassignment_authorizer=cast(Any, self.authorizer),
            item_id_factory=self.item_id_factory,
            clock=lambda: T1,
        )


def _add_waiting_approval(
    harness: _Harness,
    *,
    request_id: str,
    item_id: str,
) -> ApprovalItem:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id=f"requester:{request_id}",
        question="두 번째 환불 요청입니다.",
        request_id_factory=lambda: request_id,
        clock=lambda: T0,
        due_at=T1,
    )
    harness.requests.create(received)
    ready = received.record_initial_routing(
        intent=ROUTE.intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=ROUTE,
            attempt=1,
            trigger_key=f"request-dispatch:{request_id}:1",
            handling=HandlingAssignment(
                kind="system",
                ref=f"request-dispatch:{request_id}:1",
                due_at=T1,
            ),
        ),
        clock=lambda: T0,
    )
    assert harness.requests.compare_and_set(request_id, 0, received, ready)
    draft = ApprovalDraft(
        draft_id=f"draft:{request_id}",
        request_id=request_id,
        attempt=1,
        route=ROUTE,
        candidate=AnswerCandidate(
            text="두 번째 환불 답변입니다.",
            sources=("refund.md",),
            mode="full",
            snapshot_sha=f"sha:{request_id}",
        ),
        created_at=T0,
    )
    item = ApprovalItem(
        item_id=item_id,
        org_id="org-1",
        request_id=request_id,
        awaiting_revision=2,
        attempt=1,
        route=ROUTE,
        draft=draft,
        requirement=ApprovalRequired(
            approver_id="alice",
            policy_version="approval-v1",
        ),
        created_at=T1,
        due_at=T2,
    )
    harness.approvals.create_or_get(item)
    awaiting = ready.transition(
        AwaitingApproval(
            route=ROUTE,
            attempt=1,
            draft_ref=item.item_id,
            handling=HandlingAssignment(
                kind="approval_item",
                ref=item.item_id,
                due_at=item.due_at,
            ),
        ),
        clock=lambda: T1,
    )
    assert harness.requests.compare_and_set(request_id, 1, ready, awaiting)
    return item


def _poisoned_pending_with_other_due() -> _Harness:
    harness = _Harness(requests=_PoisonFirstRequestStore())
    harness.authorize()
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )
    other = _add_waiting_approval(
        harness,
        request_id="request-2",
        item_id="approval-other-1",
    )
    harness.expiry.result = ReassignExpiredApproval(
        assignment_generation=ApprovalAssignmentGeneration.from_item(other),
        requirement=ApprovalRequired(
            approver_id="fallback-2",
            policy_version="approval-v2",
        ),
        due_at=T3,
        policy_version="expiry-v1",
        authority_version="org-policy-v1",
        evidence_ref="fallback-rule-2",
    )
    return harness


def test_manual_target_is_strict_actor_free() -> None:
    assert ManualApprovalReassignmentTarget(approver_id="bob").model_dump() == {
        "approver_id": "bob"
    }
    with pytest.raises(ValidationError):
        ManualApprovalReassignmentTarget.model_validate(
            {"approver_id": "bob", "actor_id": "forged"},
            strict=True,
        )


@pytest.mark.parametrize(
    "missing",
    [
        "expiry_policy",
        "reassignment_authorizer",
        "item_id_factory",
        "clock",
        "reader",
        "terminal_publisher",
    ],
)
def test_lifecycle_dependencies_are_all_or_none(missing: str) -> None:
    dependencies: dict[str, object | None] = {
        "boundary": object(),
        "completion": object(),
        "reader": _Reader(),
        "terminal_publisher": _Publisher(),
        "expiry_policy": _ExpiryPolicy(),
        "reassignment_authorizer": _ReassignmentAuthorizer(),
        "item_id_factory": lambda: "approval-1",
        "clock": lambda: T1,
    }
    dependencies[missing] = None

    with pytest.raises(ApprovalOperationsDependency):
        ApprovalOperationsApplication(
            requests=InMemoryQuestionRequestStore(),
            approvals=InMemoryApprovalStore(),
            **dependencies,  # type: ignore[arg-type]
        )


def test_manual_reassignment_uses_exact_authorization_and_same_retry() -> None:
    harness = _Harness()
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    first = harness.app.reassign("approval-1", principal, target)
    second = harness.app.reassign("approval-1", principal, target)

    assert (
        first
        == second
        == ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="reassigned",
        )
    )
    assert harness.authorizer.calls == 1
    assert harness.id_calls == 1
    old = harness.approvals.get("approval-1")
    successor = harness.approvals.get("approval-2")
    request = harness.requests.get("request-1")
    assert old is not None and old.status == "superseded"
    assert old.supersession is not None
    assert old.supersession.actor_id == "operator-1"
    assert old.supersession.target_approver_id == "bob"
    assert old.supersession.policy_version == "manual-reassignment-v1"
    assert old.supersession.authority_version == "org-policy-v1"
    assert old.supersession.evidence_ref == "manual-grant-1"
    assert successor is not None and successor.draft == harness.item.draft
    assert successor.requirement.approver_id == "bob"
    assert request is not None and request.revision == 3
    assert isinstance(request.state, AwaitingApproval)
    assert request.state.draft_ref == "approval-2"


def test_cached_manual_reassignment_revalidates_store_and_request_postcondition() -> None:
    approvals = _DroppableDirectLifecycleIndexStore()
    harness = _Harness(approvals=approvals)
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    harness.app.reassign("approval-1", principal, target)
    approvals.drop_direct("approval-2")

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign("approval-1", principal, target)


def test_dormant_completed_cache_read_failure_does_not_block_unrelated_due_item() -> None:
    approvals = _TransientCompletedGetStore()
    harness = _Harness(approvals=approvals)
    harness.authorize()
    harness.app.reassign(
        "approval-1",
        ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
        ManualApprovalReassignmentTarget(approver_id="bob"),
    )
    other = _add_waiting_approval(
        harness,
        request_id="request-2",
        item_id="approval-other-1",
    )
    harness.expiry.result = ReassignExpiredApproval(
        assignment_generation=ApprovalAssignmentGeneration.from_item(other),
        requirement=ApprovalRequired(
            approver_id="fallback-2",
            policy_version="approval-v2",
        ),
        due_at=T3,
        policy_version="expiry-v1",
        authority_version="org-policy-v1",
        evidence_ref="fallback-rule-2",
    )
    approvals.fail_item_id = "approval-2"

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalReassigned(
            predecessor_item_id="approval-other-1",
            successor_item_id="approval-3",
            request_id="request-2",
            approval_round=2,
            due_at=T3,
            reason="expired",
        )
    ]
    assert approvals.fail_item_id == "approval-2"


def test_manual_reassignment_rejects_same_item_authorizer_reentry_before_write() -> None:
    harness = _Harness()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    calls: list[str] = []

    def authorize(
        *,
        assignment: ApprovalAssignmentGeneration,
        principal: ApproverPrincipal,
        target_approver_id: str,
        requested_at: datetime,
    ) -> ApprovalReassignmentAuthorization:
        calls.append(target_approver_id)
        if len(calls) == 1:
            harness.app.reassign(
                "approval-1",
                principal,
                ManualApprovalReassignmentTarget(approver_id="mallory"),
            )
        return ApprovalReassignmentAuthorization(
            assignment_generation=assignment,
            org_id=principal.org_id,
            actor_id=principal.subject_id,
            target_approver_id=target_approver_id,
            requirement=ApprovalRequired(
                approver_id=target_approver_id,
                policy_version=f"approval-{target_approver_id}",
            ),
            due_at=T3,
            policy_version=f"manual-{target_approver_id}",
            authority_version="org-policy-v1",
            evidence_ref=f"grant-{target_approver_id}",
        )

    harness.authorizer.authorize = authorize  # type: ignore[method-assign]

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign(
            "approval-1",
            principal,
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )

    current = harness.approvals.get_by_request_attempt("request-1", 1)
    request = harness.requests.get("request-1")
    assert calls == ["bob"]
    assert current is not None and current.item_id == "approval-1" and current.status == "open"
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)


def test_manual_reassignment_requires_direct_successor_index_before_request_write() -> None:
    approvals = _MissingDirectLifecycleIndexStore()
    harness = _Harness(approvals=approvals)
    harness.authorize()

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )

    request = harness.requests.get("request-1")
    current = approvals.get_by_request_attempt("request-1", 1)
    assert approvals.get("approval-2") is None
    assert current is not None and current.item_id == "approval-2"
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)
    assert request.state.draft_ref == "approval-1"


def test_manual_reassignment_rejects_non_bool_store_write_flag_before_request_write() -> None:
    approvals = _MalformedLifecycleWriteResultStore()
    harness = _Harness(approvals=approvals)
    harness.authorize()

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )
    request = harness.requests.get("request-1")
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)


def test_manual_reassignment_requires_direct_predecessor_history_before_request_write() -> None:
    approvals = _MissingPredecessorDirectLifecycleIndexStore()
    harness = _Harness(approvals=approvals)
    harness.authorize()

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )

    request = harness.requests.get("request-1")
    current = approvals.get_by_request_attempt("request-1", 1)
    assert approvals.get("approval-1") is None
    assert current is not None and current.item_id == "approval-2"
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)
    assert request.state.draft_ref == "approval-1"


def test_manual_denial_exception_and_malformed_result_are_separate() -> None:
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    denied = _Harness()
    denied.authorizer.result = ApprovalReassignmentDenied(
        assignment_generation=denied.assignment,
        org_id="org-1",
        actor_id="operator-1",
        target_approver_id="bob",
    )
    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        denied.app.reassign("approval-1", principal, target)

    failed = _Harness()
    failed.authorizer.error = RuntimeError("directory unavailable")
    with pytest.raises(ApprovalOperationsDependency):
        failed.app.reassign("approval-1", principal, target)

    malformed = _Harness()
    malformed.authorizer.result = object()
    with pytest.raises(ApprovalOperationsIntegrityError):
        malformed.app.reassign("approval-1", principal, target)


def test_different_manual_command_conflicts_without_second_authorization() -> None:
    harness = _Harness()
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    harness.app.reassign(
        "approval-1",
        principal,
        ManualApprovalReassignmentTarget(approver_id="bob"),
    )

    with pytest.raises(ApprovalOperationsConflict):
        harness.app.reassign(
            "approval-1",
            principal,
            ManualApprovalReassignmentTarget(approver_id="carol"),
        )
    assert harness.authorizer.calls == 1


@pytest.mark.parametrize("pending", [False, True])
def test_existing_manual_work_never_leaks_cross_org_existence(pending: bool) -> None:
    approvals = _ResponseLossApprovalStore() if pending else None
    harness = _Harness(approvals=approvals)
    harness.authorize()
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    if pending:
        with pytest.raises(ApprovalOperationsDependency):
            harness.app.reassign(
                "approval-1",
                ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
                target,
            )
    else:
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            target,
        )

    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-2", subject_id="operator-1"),
            target,
        )
    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-2"),
            target,
        )


def test_manual_reassignment_hides_closed_item_like_missing_item_before_authorization() -> None:
    harness = _Harness()
    harness.enable_decisions()
    harness.app.decide(
        "approval-1",
        ApproverPrincipal(org_id="org-1", subject_id="alice"),
        RejectIntent(reason_code="unsupported"),
    )
    principal = ApproverPrincipal(org_id="org-1", subject_id="intruder")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        harness.app.reassign("approval-1", principal, target)
    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        harness.app.reassign("does-not-exist", principal, target)
    assert harness.authorizer.calls == 0


def test_expiry_work_never_becomes_a_manual_reassignment_oracle() -> None:
    harness = _Harness(approvals=_ResponseLossApprovalStore())
    harness.expire_reassign()
    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]

    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )


def test_same_manual_reassignment_32_way_materializes_once() -> None:
    harness = _Harness()
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    def reassign(_: int) -> ApprovalReassigned:
        return harness.app.reassign("approval-1", principal, target)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(reassign, range(32)))

    assert all(result == results[0] for result in results)
    assert harness.authorizer.calls == 1
    assert harness.id_calls == 1


@pytest.mark.parametrize("loss", ["store", "request"])
def test_manual_reassignment_response_loss_reuses_authorization_and_factories(
    loss: str,
) -> None:
    approvals = _ResponseLossApprovalStore() if loss == "store" else None
    requests = _ResponseLossRequestStore() if loss == "request" else None
    harness = _Harness(approvals=approvals, requests=requests)
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    result = harness.app.reassign("approval-1", principal, target)

    assert result.successor_item_id == "approval-2"
    assert harness.authorizer.calls == 1
    assert harness.id_calls == 1


def test_manual_transient_store_conflict_is_repaired_by_background_scan() -> None:
    harness = _Harness(approvals=_ConflictOnceWithoutMutationStore())
    harness.authorize()

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )
    assert harness.app.expire_due(T1, limit=1) == [
        ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="reassigned",
        )
    ]
    assert harness.authorizer.calls == 1
    assert harness.id_calls == 1


def test_same_manual_retry_keeps_repeated_open_store_conflicts_retryable() -> None:
    harness = _Harness(approvals=_ConflictTwiceWithoutMutationStore())
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    result = harness.app.reassign("approval-1", principal, target)

    assert result.successor_item_id == "approval-2"
    assert harness.authorizer.calls == 1
    assert harness.id_calls == 1


@pytest.mark.parametrize("loss", ["store", "request"])
def test_expiry_scan_repairs_pending_manual_reassignment_without_client_retry(
    loss: str,
) -> None:
    approvals = _ResponseLossApprovalStore() if loss == "store" else None
    requests = _ResponseLossRequestStore() if loss == "request" else None
    harness = _Harness(approvals=approvals, requests=requests)
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)

    assert harness.app.expire_due(T1, limit=10) == [
        ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="reassigned",
        )
    ]
    assert harness.authorizer.calls == 1
    assert harness.id_calls == 1


@pytest.mark.parametrize("orphan_work", [False, True])
def test_expiry_scan_rejects_unbound_cached_lifecycle_result(
    orphan_work: bool,
) -> None:
    requests = _ToggleRequestFailureStore()
    harness = _Harness(requests=requests)
    harness.authorize()
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )
    work = harness.app.pending_reassignment_work("approval-1")
    forged = ApprovalReassigned(
        predecessor_item_id="approval-1",
        successor_item_id="forged-successor",
        request_id="request-1",
        approval_round=2,
        due_at=T3,
        reason="reassigned",
    )
    harness.app.replace_cached_entry("approval-1", (work, forged))
    if orphan_work:
        harness.app.drop_pending_work("approval-1")

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.expire_due(T2, limit=1)

    request = harness.requests.get("request-1")
    current = harness.approvals.get_by_request_attempt("request-1", 1)
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)
    assert current is not None and current.item_id == "approval-2"


def test_expiry_scan_canonicalizes_malformed_cache_entry_as_integrity() -> None:
    harness = _Harness()
    harness.authorize()
    harness.app.reassign(
        "approval-1",
        ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
        ManualApprovalReassignmentTarget(approver_id="bob"),
    )
    harness.app.replace_cached_entry("approval-1", object())

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.expire_due(T2, limit=1)


@pytest.mark.parametrize("forged_part", ["work", "outcome"])
def test_manual_retry_strictly_revalidates_cached_models(forged_part: str) -> None:
    harness = _Harness()
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    harness.app.reassign("approval-1", principal, target)
    work, outcome = harness.app.cached_reassignment_entry("approval-1")
    if forged_part == "work":
        forged_predecessor = work.predecessor.model_copy(update={"approval_round": 1.0})
        work = work.model_copy(update={"predecessor": forged_predecessor})
    else:
        outcome = outcome.model_copy(update={"approval_round": 2.0})
    harness.app.replace_cached_entry("approval-1", (work, outcome))

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign("approval-1", principal, target)


def test_manual_retry_strictly_revalidates_pending_work_without_cache() -> None:
    harness = _Harness(approvals=_ConflictOnceWithoutMutationStore())
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    work = harness.app.pending_reassignment_work("approval-1")
    forged_predecessor = work.predecessor.model_copy(update={"approval_round": 1.0})
    harness.app.replace_pending_reassignment_work(
        "approval-1",
        work.model_copy(update={"predecessor": forged_predecessor}),
    )

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign("approval-1", principal, target)


def test_expiry_scan_rejects_exact_cached_result_without_request_postcondition() -> None:
    requests = _ToggleRequestFailureStore()
    harness = _Harness(requests=requests)
    harness.authorize()
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )
    work = harness.app.pending_reassignment_work("approval-1")
    exact_outcome = ApprovalReassigned(
        predecessor_item_id=work.predecessor.item_id,
        successor_item_id=work.successor.item_id,
        request_id=work.successor.request_id,
        approval_round=work.successor.approval_round,
        due_at=work.successor.due_at,
        reason=work.supersession.reason,
    )
    harness.app.replace_cached_entry("approval-1", (work, exact_outcome))

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.expire_due(T2, limit=1)


def test_expiry_scan_rejects_cached_item_returned_again_by_due_index() -> None:
    approvals = _StaleCompletedDueStore()
    harness = _Harness(approvals=approvals)
    harness.expire_reassign()
    first = harness.app.expire_due(T2, limit=1)
    assert len(first) == 1 and isinstance(first[0], ApprovalReassigned)
    approvals.return_stale = True

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.expire_due(T2, limit=1)


def test_expiry_scan_rejects_deferred_unavailable_outcome_in_completion_cache() -> None:
    publisher = _Publisher(fail_once=True)
    harness = _Harness(publisher=publisher)
    harness.expire_unavailable()

    first = harness.app.expire_due(T2, limit=1)
    assert len(first) == 1 and isinstance(first[0], ApprovalMadeUnavailable)
    assert isinstance(first[0].delivery, TerminalDeferred)
    work = harness.app.pending_work_entry("approval-1")
    harness.app.replace_cached_entry("approval-1", (work, first[0]))

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.expire_due(T2, limit=1)
    assert publisher.calls == 1


def test_expiry_scan_rejects_unknown_unavailable_delivery_in_completion_cache() -> None:
    publisher = _Publisher(fail_once=True)
    harness = _Harness(publisher=publisher)
    harness.expire_unavailable()
    first = harness.app.expire_due(T2, limit=1)
    assert len(first) == 1 and isinstance(first[0], ApprovalMadeUnavailable)
    work = harness.app.pending_work_entry("approval-1")
    forged = first[0].model_copy(update={"delivery": object()})
    harness.app.replace_cached_entry("approval-1", (work, forged))

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.expire_due(T2, limit=1)
    assert publisher.calls == 1


def test_reassignment_never_trusts_lying_true_request_cas() -> None:
    requests = _LyingTrueRequestStore()
    harness = _Harness(requests=requests)
    harness.authorize()
    requests.lie_on_reassignment = True

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
            ManualApprovalReassignmentTarget(approver_id="bob"),
        )

    request = requests.get("request-1")
    current = harness.approvals.get_by_request_attempt("request-1", 1)
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)
    assert request.state.draft_ref == "approval-1"
    assert current is not None and current.item_id == "approval-2"
    requests.lie_on_reassignment = False
    assert harness.app.expire_due(T1, limit=1) == [
        ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="reassigned",
        )
    ]


def test_unavailable_never_publishes_after_lying_true_request_cas() -> None:
    requests = _LyingTrueRequestStore()
    harness = _Harness(requests=requests)
    harness.expire_unavailable()
    requests.lie_on_reassignment = True

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]
    request = requests.get("request-1")
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)
    assert harness.publisher.calls == 0
    requests.lie_on_reassignment = False
    repaired = harness.app.expire_due(T2, limit=1)
    assert len(repaired) == 1 and isinstance(repaired[0], ApprovalMadeUnavailable)
    assert harness.publisher.calls == 1


def test_predecessor_repair_requires_exact_request_updated_at_before_cas() -> None:
    requests = _CorruptPredecessorTimestampRequestStore()
    harness = _Harness(
        approvals=_ResponseLossApprovalStore(),
        requests=requests,
    )
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    requests.corrupt_reads = True

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign("approval-1", principal, target)

    assert requests.reassignment_cas_calls == 0


def test_original_reassignment_retry_accepts_exact_successor_reject_terminal() -> None:
    harness = _Harness(requests=_ResponseLossRequestStore())
    harness.authorize()
    harness.enable_decisions()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    declined = harness.app.decide(
        "approval-2",
        ApproverPrincipal(org_id="org-1", subject_id="bob"),
        RejectIntent(reason_code="unsupported"),
    )
    repaired = harness.app.reassign("approval-1", principal, target)

    assert isinstance(declined, ApprovalDeclined)
    assert repaired == ApprovalReassigned(
        predecessor_item_id="approval-1",
        successor_item_id="approval-2",
        request_id="request-1",
        approval_round=2,
        due_at=T3,
        reason="reassigned",
    )
    assert harness.authorizer.calls == 1
    assert harness.id_calls == 1


def test_original_reassignment_retry_accepts_later_exact_assignment_generation() -> None:
    harness = _Harness(requests=_ResponseLossRequestStore())
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    successor = harness.approvals.get("approval-2")
    request = harness.requests.get("request-1")
    assert successor is not None and request is not None
    later = ApprovalItem(
        item_id="approval-3",
        org_id=successor.org_id,
        request_id=successor.request_id,
        awaiting_revision=successor.awaiting_revision + 1,
        attempt=successor.attempt,
        route=successor.route,
        draft=successor.draft,
        requirement=ApprovalRequired(
            approver_id="carol",
            policy_version="approval-v3",
        ),
        created_at=T2,
        due_at=T3,
        approval_round=successor.approval_round + 1,
        supersedes_item_id=successor.item_id,
    )
    supersession = ApprovalSupersession(
        reason="reassigned",
        successor_item_id=later.item_id,
        superseded_at=T2,
    )
    harness.approvals.supersede_and_create_if_open(
        successor.item_id,
        supersession,
        later,
        expected_generation=ApprovalAssignmentGeneration.from_item(successor),
    )
    updated = request.reassign_approval(
        previous_item_id=successor.item_id,
        successor_item_id=later.item_id,
        due_at=later.due_at,
        clock=lambda: T2,
    )
    assert harness.requests.compare_and_set(
        request.request_id,
        request.revision,
        request,
        updated,
    )

    repaired = harness.app.reassign("approval-1", principal, target)

    assert repaired.successor_item_id == "approval-2"
    assert harness.authorizer.calls == 1
    assert harness.id_calls == 1


def test_original_retry_requires_later_current_direct_index() -> None:
    approvals = _DroppableDirectLifecycleIndexStore()
    harness = _Harness(
        approvals=approvals,
        requests=_ResponseLossRequestStore(),
    )
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    successor = approvals.get("approval-2")
    request = harness.requests.get("request-1")
    assert successor is not None and request is not None
    later = ApprovalItem(
        item_id="approval-3",
        org_id=successor.org_id,
        request_id=successor.request_id,
        awaiting_revision=successor.awaiting_revision + 1,
        attempt=successor.attempt,
        route=successor.route,
        draft=successor.draft,
        requirement=ApprovalRequired(
            approver_id="carol",
            policy_version="approval-v3",
        ),
        created_at=T2,
        due_at=T3,
        approval_round=successor.approval_round + 1,
        supersedes_item_id=successor.item_id,
    )
    approvals.supersede_and_create_if_open(
        successor.item_id,
        ApprovalSupersession(
            reason="reassigned",
            successor_item_id=later.item_id,
            superseded_at=T2,
        ),
        later,
        expected_generation=ApprovalAssignmentGeneration.from_item(successor),
    )
    updated = request.reassign_approval(
        previous_item_id=successor.item_id,
        successor_item_id=later.item_id,
        due_at=later.due_at,
        clock=lambda: T2,
    )
    assert harness.requests.compare_and_set(
        request.request_id,
        request.revision,
        request,
        updated,
    )
    approvals.drop_direct(later.item_id)

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign("approval-1", principal, target)


def test_original_reassignment_retry_accepts_exact_later_store_partial_window() -> None:
    harness = _Harness(requests=_ResponseLossRequestStore())
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    successor = harness.approvals.get("approval-2")
    assert successor is not None
    later = ApprovalItem(
        item_id="approval-3",
        org_id=successor.org_id,
        request_id=successor.request_id,
        awaiting_revision=successor.awaiting_revision + 1,
        attempt=successor.attempt,
        route=successor.route,
        draft=successor.draft,
        requirement=ApprovalRequired(
            approver_id="carol",
            policy_version="approval-v3",
        ),
        created_at=T2,
        due_at=T3,
        approval_round=successor.approval_round + 1,
        supersedes_item_id=successor.item_id,
    )
    harness.approvals.supersede_and_create_if_open(
        successor.item_id,
        ApprovalSupersession(
            reason="reassigned",
            successor_item_id=later.item_id,
            superseded_at=T2,
        ),
        later,
        expected_generation=ApprovalAssignmentGeneration.from_item(successor),
    )

    repaired = harness.app.reassign("approval-1", principal, target)

    assert repaired.successor_item_id == "approval-2"
    request = harness.requests.get("request-1")
    assert request is not None and isinstance(request.state, AwaitingApproval)
    assert request.state.draft_ref == "approval-2"


def test_original_retry_rejects_stale_current_index_with_newer_generation_history() -> None:
    approvals = _StaleCurrentReadStore()
    harness = _Harness(
        approvals=approvals,
        requests=_ResponseLossRequestStore(),
    )
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    successor = approvals.get("approval-2")
    assert successor is not None
    later = ApprovalItem(
        item_id="approval-3",
        org_id=successor.org_id,
        request_id=successor.request_id,
        awaiting_revision=successor.awaiting_revision + 1,
        attempt=successor.attempt,
        route=successor.route,
        draft=successor.draft,
        requirement=ApprovalRequired(
            approver_id="carol",
            policy_version="approval-v3",
        ),
        created_at=T2,
        due_at=T3,
        approval_round=successor.approval_round + 1,
        supersedes_item_id=successor.item_id,
    )
    approvals.supersede_and_create_if_open(
        successor.item_id,
        ApprovalSupersession(
            reason="reassigned",
            successor_item_id=later.item_id,
            superseded_at=T2,
        ),
        later,
        expected_generation=ApprovalAssignmentGeneration.from_item(successor),
    )
    approvals.stale_item_id = "approval-2"

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign("approval-1", principal, target)


@pytest.mark.parametrize("forge_kind", ["target_mismatch", "item_id_reuse"])
def test_original_retry_rejects_forged_later_generation_lineage(
    forge_kind: str,
) -> None:
    approvals = _ForgedLaterGenerationStore()
    harness = _Harness(
        approvals=approvals,
        requests=_ResponseLossRequestStore(),
    )
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    approvals.forge_kind = forge_kind

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.reassign("approval-1", principal, target)


@pytest.mark.parametrize("loss", ["store", "request"])
def test_expiry_reassignment_response_loss_uses_pending_work_without_rerunning_policy(
    loss: str,
) -> None:
    approvals = _ResponseLossApprovalStore() if loss == "store" else None
    requests = _ResponseLossRequestStore() if loss == "request" else None
    harness = _Harness(approvals=approvals, requests=requests)
    harness.expire_reassign()

    first = harness.app.expire_due(T2, limit=10)
    result = harness.app.expire_due(T3, limit=10)

    assert first == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]
    assert result == [
        ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="expired",
        )
    ]
    assert harness.expiry.calls == 1
    assert harness.id_calls == 1


def test_unavailable_closes_item_fails_request_without_completion_and_publishes() -> None:
    harness = _Harness()
    harness.expire_unavailable()

    result = harness.app.expire_due(T2, limit=10)

    assert result == [
        ApprovalMadeUnavailable(
            item_id="approval-1",
            request_id="request-1",
            error_code="approval_unavailable",
            delivery=TerminalPublished(),
        )
    ]
    item = harness.approvals.get("approval-1")
    request = harness.requests.get("request-1")
    assert item is not None and item.status == "unavailable"
    assert item.resolution is None and item.unavailability is not None
    assert request is not None and request.revision == 3
    assert request.updated_at == T2
    assert isinstance(request.state, FailedRequest)
    assert request.state.error_code == "approval_unavailable"
    assert harness.reader.calls == 1
    assert harness.publisher.calls == 1


def test_unavailable_publish_failure_remains_pending_and_repairs_before_new_scan() -> None:
    publisher = _Publisher(fail_once=True)
    harness = _Harness(publisher=publisher)
    harness.expire_unavailable()

    first = harness.app.expire_due(T2, limit=10)
    second = harness.app.expire_due(T3, limit=10)

    assert len(first) == 1 and isinstance(first[0], ApprovalMadeUnavailable)
    assert first[0].delivery == TerminalDeferred(reason_code="publish_failed")
    assert second == [
        ApprovalMadeUnavailable(
            item_id="approval-1",
            request_id="request-1",
            error_code="approval_unavailable",
            delivery=TerminalPublished(),
        )
    ]
    assert harness.expiry.calls == 1
    assert harness.id_calls == 0
    assert publisher.calls == 2


@pytest.mark.parametrize("loss", ["store", "request"])
def test_unavailable_response_loss_repairs_from_pending_ledger(loss: str) -> None:
    approvals = _UnavailableResponseLossStore() if loss == "store" else None
    requests = _ResponseLossRequestStore() if loss == "request" else None
    harness = _Harness(approvals=approvals, requests=requests)
    harness.expire_unavailable()

    first = harness.app.expire_due(T2, limit=10)
    result = harness.app.expire_due(T3, limit=10)

    assert first == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]
    assert len(result) == 1 and isinstance(result[0], ApprovalMadeUnavailable)
    assert result[0].delivery == TerminalPublished()
    assert harness.expiry.calls == 1
    assert harness.publisher.calls == 1


def test_expiry_policy_exception_and_malformed_result_are_zero_write() -> None:
    failed = _Harness()
    failed.expiry.error = RuntimeError("policy unavailable")
    assert failed.app.expire_due(T2, limit=10) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]

    malformed = _Harness()
    malformed.expiry.result = object()
    assert malformed.app.expire_due(T2, limit=10) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]

    for harness in (failed, malformed):
        item = harness.approvals.get("approval-1")
        request = harness.requests.get("request-1")
        assert item is not None and item.status == "open"
        assert request is not None and isinstance(request.state, AwaitingApproval)
        assert harness.id_calls == 0
        assert harness.publisher.calls == 0


def test_expiry_scan_rejects_policy_reentry_before_nested_write() -> None:
    harness = _Harness()
    calls: list[str] = []

    def evaluate(
        *,
        assignment: ApprovalAssignmentGeneration,
        now: datetime,
    ) -> ReassignExpiredApproval:
        calls.append("outer" if not calls else "inner")
        if len(calls) == 1:
            harness.app.expire_due(T2, limit=1)
        target = "bob" if len(calls) == 1 else "mallory"
        return ReassignExpiredApproval(
            assignment_generation=assignment,
            requirement=ApprovalRequired(
                approver_id=target,
                policy_version=f"approval-{target}",
            ),
            due_at=T3,
            policy_version=f"expiry-{target}",
            authority_version="org-policy-v1",
            evidence_ref=f"fallback-{target}",
        )

    harness.expiry.evaluate = evaluate  # type: ignore[method-assign]

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]
    current = harness.approvals.get_by_request_attempt("request-1", 1)
    request = harness.requests.get("request-1")
    assert calls == ["outer"]
    assert current is not None and current.item_id == "approval-1" and current.status == "open"
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)


def test_cached_due_candidate_waits_when_later_scan_time_moves_before_due() -> None:
    harness = _Harness()
    harness.expiry.error = RuntimeError("policy temporarily unavailable")
    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]
    harness.expiry.error = None
    harness.expire_reassign()

    assert harness.app.expire_due(T1, limit=1) == []
    assert harness.expiry.calls == 1
    assert harness.id_calls == 0
    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="expired",
        )
    ]
    assert harness.expiry.calls == 2
    assert harness.id_calls == 1


def test_due_successor_waits_for_pending_predecessor_request_repair() -> None:
    requests = _ToggleRequestFailureStore()
    harness = _Harness(requests=requests)
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)
    successor = harness.approvals.get("approval-2")
    assert successor is not None
    next_due = T3 + timedelta(minutes=10)
    harness.expiry.result = ReassignExpiredApproval(
        assignment_generation=ApprovalAssignmentGeneration.from_item(successor),
        requirement=ApprovalRequired(
            approver_id="fallback-2",
            policy_version="approval-v3",
        ),
        due_at=next_due,
        policy_version="expiry-v1",
        authority_version="org-policy-v1",
        evidence_ref="fallback-rule-2",
    )

    first = harness.app.expire_due(T3, limit=1)
    second = harness.app.expire_due(T3, limit=1)
    requests.fail_reassignment = False
    repaired = harness.app.expire_due(T3, limit=1)
    expired = harness.app.expire_due(T3, limit=1)

    expected_failure = ApprovalLifecycleFailure(
        item_id="approval-1",
        request_id="request-1",
        error_code="dependency",
        retryable=True,
    )
    assert first == [expected_failure]
    assert second == [expected_failure]
    assert harness.expiry.calls == 1
    assert repaired == [
        ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="reassigned",
        )
    ]
    assert expired == [
        ApprovalReassigned(
            predecessor_item_id="approval-2",
            successor_item_id="approval-3",
            request_id="request-1",
            approval_round=3,
            due_at=next_due,
            reason="expired",
        )
    ]


def test_poisoned_pending_item_rotates_without_losing_unrelated_due_progress() -> None:
    harness = _poisoned_pending_with_other_due()

    first = harness.app.expire_due(T2, limit=1)
    second = harness.app.expire_due(T2, limit=1)

    assert first == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]
    assert second == [
        ApprovalReassigned(
            predecessor_item_id="approval-other-1",
            successor_item_id="approval-3",
            request_id="request-2",
            approval_round=2,
            due_at=T3,
            reason="expired",
        )
    ]
    request = harness.requests.get("request-2")
    assert request is not None and request.revision == 3
    assert isinstance(request.state, AwaitingApproval)
    assert request.state.draft_ref == "approval-3"
    assert harness.expiry.calls == 1


def test_batch_returns_partial_failure_and_success_without_losing_either() -> None:
    harness = _poisoned_pending_with_other_due()

    results = harness.app.expire_due(T2, limit=2)

    assert results == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        ),
        ApprovalReassigned(
            predecessor_item_id="approval-other-1",
            successor_item_id="approval-3",
            request_id="request-2",
            approval_round=2,
            due_at=T3,
            reason="expired",
        ),
    ]
    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]


def test_quarantined_due_item_does_not_hide_later_due_item_from_scan_window() -> None:
    harness = _Harness()
    other = _add_waiting_approval(
        harness,
        request_id="request-2",
        item_id="approval-other-1",
    )
    harness.expiry.result = object()

    first = harness.app.expire_due(T2, limit=1)
    harness.expiry.result = ReassignExpiredApproval(
        assignment_generation=ApprovalAssignmentGeneration.from_item(other),
        requirement=ApprovalRequired(
            approver_id="fallback-2",
            policy_version="approval-v2",
        ),
        due_at=T3,
        policy_version="expiry-v1",
        authority_version="org-policy-v1",
        evidence_ref="fallback-rule-2",
    )
    second = harness.app.expire_due(T2, limit=1)

    assert first == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]
    assert second == [
        ApprovalReassigned(
            predecessor_item_id="approval-other-1",
            successor_item_id="approval-2",
            request_id="request-2",
            approval_round=2,
            due_at=T3,
            reason="expired",
        )
    ]
    request = harness.requests.get("request-2")
    assert request is not None and request.revision == 3


def test_new_manual_work_clears_stale_quarantine_and_repairs_without_client_retry() -> None:
    harness = _Harness(approvals=_ResponseLossApprovalStore())
    harness.expiry.result = object()
    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]
    harness.authorize()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    with pytest.raises(ApprovalOperationsDependency):
        harness.app.reassign("approval-1", principal, target)

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="reassigned",
        )
    ]
    request = harness.requests.get("request-1")
    assert request is not None and request.revision == 3
    assert isinstance(request.state, AwaitingApproval)
    assert request.state.draft_ref == "approval-2"


@pytest.mark.parametrize("invalid_limit", [True, False, 1.5, "1", None])
def test_expiry_scan_rejects_non_exact_positive_integer_limit(
    invalid_limit: object,
) -> None:
    harness = _Harness()
    with pytest.raises(ApprovalOperationsInvalid):
        harness.app.expire_due(T2, invalid_limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("duplicate_kind", ["item_id", "assignment"])
def test_expiry_scan_rejects_duplicate_due_projection_before_policy(
    duplicate_kind: str,
) -> None:
    approvals = _DuplicateDueProjectionStore(duplicate_kind)
    harness = _Harness(approvals=approvals)
    harness.expire_unavailable()

    with pytest.raises(ApprovalOperationsIntegrityError):
        harness.app.expire_due(T2, limit=1)
    item = approvals.get_by_request_attempt("request-1", 1)
    request = harness.requests.get("request-1")
    assert item is not None and item.status == "open"
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)
    assert harness.expiry.calls == 0
    assert harness.publisher.calls == 0


def test_unavailable_item_is_hidden_from_decision_surface() -> None:
    harness = _Harness()
    harness.expire_unavailable()
    assert len(harness.app.expire_due(T2, limit=10)) == 1

    with pytest.raises(ApprovalOperationsNotFoundOrDenied):
        harness.app.decide(
            "approval-1",
            ApproverPrincipal(org_id="org-1", subject_id="alice"),
            RejectIntent(reason_code="late"),
        )


def test_due_snapshot_loser_is_retired_instead_of_requeued_forever() -> None:
    harness = _Harness(approvals=_ResolveAfterDueScanStore())
    harness.expire_reassign()

    first = harness.app.expire_due(T2, limit=1)

    assert first == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="conflict",
            retryable=False,
        )
    ]
    assert harness.app.expire_due(T2, limit=1) == []
    assert harness.app.expire_due(T2, limit=1) == []


def test_due_snapshot_loser_accepts_exact_superseded_lineage() -> None:
    approvals = _SupersedeAfterDueScanStore()
    harness = _Harness(approvals=approvals)
    harness.expire_reassign()

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="conflict",
            retryable=False,
        )
    ]
    current = approvals.get_by_request_attempt("request-1", 1)
    assert current is not None and current.item_id == "other-winner"
    assert harness.expiry.calls == 0
    assert harness.app.expire_due(T2, limit=1) == []


def test_due_loser_retirement_requires_exact_get_current_round_snapshot() -> None:
    approvals = _ForgedClosedGetStore()
    harness = _Harness(approvals=approvals)
    harness.expire_reassign()
    approvals.forge_closed_get = True

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]
    actual = approvals.get_by_request_attempt("request-1", 1)
    assert actual is not None and actual.status == "open"
    assert harness.expiry.calls == 0


@pytest.mark.parametrize("forge_kind", ["target_mismatch", "item_id_reuse"])
def test_due_loser_retirement_rejects_forged_full_lineage(forge_kind: str) -> None:
    approvals = _ForgedDueRetirementStore(forge_kind)
    harness = _Harness(approvals=approvals)
    harness.expire_reassign()

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]
    assert harness.expiry.calls == 0


def test_due_loser_retirement_rejects_forged_line_before_expected_generation() -> None:
    approvals = _ForgedEarlierLineageAfterDueScanStore()
    harness = _Harness(approvals=approvals)
    harness.authorize()
    harness.app.reassign(
        "approval-1",
        ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
        ManualApprovalReassignmentTarget(approver_id="bob"),
    )
    round_two = approvals.get("approval-2")
    assert round_two is not None
    harness.expiry.result = ReassignExpiredApproval(
        assignment_generation=ApprovalAssignmentGeneration.from_item(round_two),
        requirement=ApprovalRequired(
            approver_id="fallback-2",
            policy_version="approval-v3",
        ),
        due_at=T3 + timedelta(minutes=10),
        policy_version="expiry-v2",
        authority_version="org-policy-v2",
        evidence_ref="fallback-rule-2",
    )

    assert harness.app.expire_due(T3, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-2",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]
    assert harness.expiry.calls == 0


def test_due_loser_retirement_requires_current_direct_index() -> None:
    approvals = _MissingDueRetirementCurrentDirectStore()
    harness = _Harness(approvals=approvals)
    harness.expire_reassign()

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]
    assert approvals.get("other-winner") is None
    assert harness.expiry.calls == 0


def test_unavailable_requires_direct_item_index_before_failed_request_and_publish() -> None:
    approvals = _MissingDirectLifecycleIndexStore()
    harness = _Harness(approvals=approvals)
    harness.expire_unavailable()

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]
    request = harness.requests.get("request-1")
    current = approvals.get_by_request_attempt("request-1", 1)
    assert approvals.get("approval-1") is None
    assert current is not None and current.status == "unavailable"
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)
    assert harness.publisher.calls == 0


def test_unavailable_rejects_non_bool_store_write_flag_before_failed_request() -> None:
    approvals = _MalformedLifecycleWriteResultStore()
    harness = _Harness(approvals=approvals)
    harness.expire_unavailable()

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]
    request = harness.requests.get("request-1")
    assert request is not None and request.revision == 2
    assert isinstance(request.state, AwaitingApproval)
    assert harness.publisher.calls == 0


def test_unavailable_rejects_forged_full_generation_lineage_before_failed_request() -> None:
    approvals = _ForgedUnavailableLineageStore()
    harness = _Harness(approvals=approvals)
    harness.authorize()
    harness.app.reassign(
        "approval-1",
        ApproverPrincipal(org_id="org-1", subject_id="operator-1"),
        ManualApprovalReassignmentTarget(approver_id="bob"),
    )
    successor = approvals.get("approval-2")
    assert successor is not None
    harness.expiry.result = ApprovalUnavailable(
        assignment_generation=ApprovalAssignmentGeneration.from_item(successor),
        policy_version="expiry-v2",
        authority_version="org-policy-v2",
        evidence_ref="no-fallback-v2",
    )
    approvals.forge_lineage = True

    assert harness.app.expire_due(T3, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-2",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]
    request = harness.requests.get("request-1")
    current = approvals.get_by_request_attempt("request-1", 1)
    assert request is not None and request.revision == 3
    assert isinstance(request.state, AwaitingApproval)
    assert request.state.draft_ref == "approval-2"
    assert current is not None and current.item_id == "approval-2"
    assert current.status == "open"
    assert harness.expiry.calls == 0
    assert harness.publisher.calls == 0


def test_transient_retirement_read_failure_stays_retryable() -> None:
    harness = _Harness(approvals=_ConflictThenRetirementReadLossStore())
    harness.expire_reassign()

    first = harness.app.expire_due(T2, limit=1)
    second = harness.app.expire_due(T2, limit=1)

    assert first == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]
    assert second == [
        ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="expired",
        )
    ]
    assert harness.expiry.calls == 1
    assert harness.id_calls == 1


def test_transient_store_conflict_with_open_item_retries_same_sealed_work() -> None:
    harness = _Harness(approvals=_ConflictOnceWithoutMutationStore())
    harness.expire_reassign()

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]
    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=T3,
            reason="expired",
        )
    ]
    assert harness.expiry.calls == 1
    assert harness.id_calls == 1


def test_duplicate_expiry_scan_32_way_has_one_generation_and_one_policy_call() -> None:
    harness = _Harness()
    harness.expire_reassign()

    def expire(
        _: int,
    ) -> list[ApprovalReassigned | ApprovalMadeUnavailable | ApprovalLifecycleFailure]:
        return harness.app.expire_due(T2, limit=10)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(expire, range(32)))

    assert sum(len(result) for result in results) == 1
    assert harness.expiry.calls == 1
    assert harness.id_calls == 1
    generations = harness.approvals.generations("request-1", 1)
    assert [item.approval_round for item in generations] == [1, 2]
    request = harness.requests.get("request-1")
    assert request is not None and request.revision == 3


def test_manual_and_expiry_race_has_one_successor_generation() -> None:
    harness = _Harness()
    harness.authorize()
    harness.expire_reassign()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    def manual() -> ApprovalReassigned | ApprovalOperationsConflict:
        try:
            return harness.app.reassign("approval-1", principal, target)
        except ApprovalOperationsConflict as error:
            return error

    def expiry() -> (
        list[ApprovalReassigned | ApprovalMadeUnavailable | ApprovalLifecycleFailure]
        | ApprovalOperationsConflict
    ):
        try:
            return harness.app.expire_due(T2, limit=10)
        except ApprovalOperationsConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        manual_future = pool.submit(manual)
        expiry_future = pool.submit(expiry)
        outcomes = [manual_future.result(), expiry_future.result()]

    assert len(harness.approvals.generations("request-1", 1)) == 2
    current = harness.approvals.get_by_request_attempt("request-1", 1)
    request = harness.requests.get("request-1")
    assert current is not None and current.approval_round == 2
    assert request is not None and request.revision == 3
    assert sum(isinstance(outcome, ApprovalOperationsConflict) for outcome in outcomes) <= 1


def test_reject_and_expiry_race_has_one_domain_winner() -> None:
    harness = _Harness()
    harness.expire_reassign()
    harness.enable_decisions()

    def reject() -> (
        ApprovalAnswered
        | ApprovalDeclined
        | ApprovalOperationsConflict
        | ApprovalOperationsNotFoundOrDenied
        | ApprovalOperationsIntegrityError
    ):
        try:
            return harness.app.decide(
                "approval-1",
                ApproverPrincipal(org_id="org-1", subject_id="alice"),
                RejectIntent(reason_code="unsupported"),
            )
        except (
            ApprovalOperationsConflict,
            ApprovalOperationsNotFoundOrDenied,
            ApprovalOperationsIntegrityError,
        ) as error:
            return error

    def expiry() -> (
        list[ApprovalReassigned | ApprovalMadeUnavailable | ApprovalLifecycleFailure]
        | ApprovalOperationsConflict
    ):
        try:
            return harness.app.expire_due(T2, limit=10)
        except ApprovalOperationsConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(reject), pool.submit(expiry)]
        results = [future.result() for future in outcomes]

    request = harness.requests.get("request-1")
    assert request is not None and request.revision == 3
    if isinstance(request.state, FailedRequest):
        pytest.fail("expiry reassign must not create FailedRequest")
    if isinstance(request.state, AwaitingApproval):
        assert request.state.draft_ref == "approval-2"
        assert any(isinstance(result, list) and result for result in results)
    else:
        assert any(isinstance(result, ApprovalDeclined) for result in results)
    assert len(harness.approvals.generations("request-1", 1)) in (1, 2)
    assert harness.app.expire_due(T2, limit=10) == []


def test_reject_and_unavailable_race_never_crosses_store_terminal_states() -> None:
    for _ in range(32):
        harness = _Harness()
        harness.expire_unavailable()
        harness.enable_decisions()

        def reject() -> object:
            try:
                return harness.app.decide(
                    "approval-1",
                    ApproverPrincipal(org_id="org-1", subject_id="alice"),
                    RejectIntent(reason_code="unsupported"),
                )
            except (
                ApprovalOperationsConflict,
                ApprovalOperationsNotFoundOrDenied,
                ApprovalOperationsIntegrityError,
            ) as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            reject_future = pool.submit(reject)
            expiry_future = pool.submit(harness.app.expire_due, T2, 1)
            reject_result = reject_future.result()
            expiry_result = expiry_future.result()

        item = harness.approvals.get("approval-1")
        request = harness.requests.get("request-1")
        assert item is not None and request is not None
        if item.status == "unavailable":
            assert isinstance(request.state, FailedRequest)
            assert not isinstance(reject_result, ApprovalDeclined)
            assert any(isinstance(result, ApprovalMadeUnavailable) for result in expiry_result)
        else:
            assert item.status == "resolved"
            assert isinstance(reject_result, ApprovalDeclined)
            assert not isinstance(request.state, FailedRequest)
            assert expiry_result in (
                [],
                [
                    ApprovalLifecycleFailure(
                        item_id="approval-1",
                        request_id="request-1",
                        error_code="conflict",
                        retryable=False,
                    )
                ],
            )


def test_expiry_result_for_other_generation_is_integrity_zero_write() -> None:
    harness = _Harness()
    other = harness.assignment.model_copy(update={"due_at": T3})
    harness.expiry.result = ApprovalUnavailable(
        assignment_generation=other,
        policy_version="expiry-v1",
        authority_version="org-policy-v1",
        evidence_ref="wrong-generation",
    )

    assert harness.app.expire_due(T2, limit=10) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="integrity",
            retryable=False,
        )
    ]

    item = harness.approvals.get("approval-1")
    request = harness.requests.get("request-1")
    assert item is not None and item.status == "open"
    assert request is not None and isinstance(request.state, AwaitingApproval)
    assert harness.publisher.calls == 0


class _RaisingNotificationChannel:
    def send(self, notification: Notification) -> None:
        del notification
        raise RuntimeError("push unavailable")


class _ToggleEventJournal:
    def __init__(self) -> None:
        self.target = InMemoryApprovalEventJournal()
        self.fail = True

    def append_batch_once(self, events: tuple[ApprovalEvent, ...]) -> tuple[ApprovalEvent, ...]:
        if self.fail:
            raise RuntimeError("journal unavailable")
        return self.target.append_batch_once(events)

    def append_once(self, event: ApprovalEvent) -> ApprovalEvent:
        return self.append_batch_once((event,))[0]

    def get(self, event_id: str) -> ApprovalEvent | None:
        return self.target.get(event_id)

    def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]:
        return self.target.for_request(org_id, request_id)


class _FirstRequestPoisonEventJournal(_ToggleEventJournal):
    def append_batch_once(self, events: tuple[ApprovalEvent, ...]) -> tuple[ApprovalEvent, ...]:
        if events and events[0].request_id == "request-1":
            raise RuntimeError("request-1 journal shard unavailable")
        return self.target.append_batch_once(events)


class _DynamicUnavailablePolicy:
    def evaluate(
        self,
        *,
        assignment: ApprovalAssignmentGeneration,
        now: datetime,
    ) -> ApprovalUnavailable:
        assert now >= assignment.due_at
        return ApprovalUnavailable(
            assignment_generation=assignment,
            policy_version="expiry-v1",
            authority_version="org-policy-v1",
            evidence_ref=f"unavailable:{assignment.item_id}",
        )


class _AnyRequestReader:
    def by_request(self, request_id: str) -> None:
        assert request_id in {"request-1", "request-2"}
        return None


class _AnyRequestPublisher:
    def publish_terminal(self, request_id: str) -> TerminalPublished:
        assert request_id in {"request-1", "request-2"}
        return TerminalPublished()


def _lifecycle_app_with_evidence(
    harness: _Harness,
    recorder: ApprovalEventRecorder,
    *,
    notifier: Notifier | None = None,
) -> _InspectableApprovalOperationsApplication:
    return _InspectableApprovalOperationsApplication(
        requests=harness.requests,
        approvals=harness.approvals,
        boundary=cast(ApprovalBoundary, object()),
        completion=cast(QuestionCompletionUnitOfWork, object()),
        reader=cast(QuestionCompletionReader, harness.reader),
        terminal_publisher=harness.publisher,
        expiry_policy=cast(Any, harness.expiry),
        reassignment_authorizer=cast(Any, harness.authorizer),
        item_id_factory=harness.item_id_factory,
        clock=lambda: T1,
        evidence_recorder=recorder,
        notifier=notifier,
    )


class _RaceResponsibilityResolver:
    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot:
        assert org_id == "org-1"
        return AnswerResponsibilitySnapshot(agent_id=route.agent_id, owner_id="owner-1")


class _RaceDeadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        assert (org_id, state_kind, started_at) == ("org-1", "awaiting_approval", T1)
        return T2


class _TargetReassignmentAuthorizer:
    def __init__(self) -> None:
        self.calls = 0

    def authorize(
        self,
        *,
        assignment: ApprovalAssignmentGeneration,
        principal: ApproverPrincipal,
        target_approver_id: str,
        requested_at: datetime,
    ) -> ApprovalReassignmentAuthorization:
        assert assignment.item_id == "approval-1"
        assert principal == ApproverPrincipal(org_id="org-1", subject_id="operator-1")
        assert target_approver_id in {"bob", "carol"}
        assert requested_at == T1
        self.calls += 1
        return ApprovalReassignmentAuthorization(
            assignment_generation=assignment,
            org_id="org-1",
            actor_id="operator-1",
            target_approver_id=target_approver_id,
            requirement=ApprovalRequired(
                approver_id=target_approver_id,
                policy_version="approval-v2",
            ),
            due_at=T3,
            policy_version="manual-reassignment-v1",
            authority_version="org-policy-v1",
            evidence_ref=f"manual-grant:{target_approver_id}",
        )


class _PauseOpenDecisionGetStore(InMemoryApprovalStore):
    """처분의 open snapshot 뒤 lifecycle winner가 끝나도록 한 번만 멈춘다."""

    def __init__(self) -> None:
        super().__init__()
        self._pause_once = False
        self.open_snapshot_read = Event()
        self.release_decision = Event()

    def arm_decision_pause(self) -> None:
        self._pause_once = True
        self.open_snapshot_read.clear()
        self.release_decision.clear()

    def get(self, item_id: str) -> ApprovalItem | None:
        item = super().get(item_id)
        if self._pause_once and item_id == "approval-1" and item is not None:
            assert item.status == "open"
            self._pause_once = False
            self.open_snapshot_read.set()
            assert self.release_decision.wait(timeout=5)
        return item


class _TamperedLifecycleConflictStore(_PauseOpenDecisionGetStore):
    def __init__(self) -> None:
        super().__init__()
        self.tamper: str | None = None
        self.stale_open: ApprovalItem | None = None

    def get(self, item_id: str) -> ApprovalItem | None:
        was_paused_decision_read = self._pause_once
        item = super().get(item_id)
        if self.open_snapshot_read.is_set() and self.stale_open is None and item is not None:
            self.stale_open = item
        if (
            self.tamper == "direct"
            and not was_paused_decision_read
            and item_id == "approval-1"
            and item is not None
        ):
            return item.model_copy(update={"org_id": "forged-org"})
        return item

    def get_by_request_attempt(self, request_id: str, attempt: int) -> ApprovalItem | None:
        if self.tamper == "current":
            return InMemoryApprovalStore.get(self, "approval-1")
        return super().get_by_request_attempt(request_id, attempt)

    def get_by_request_attempt_round(
        self,
        request_id: str,
        attempt: int,
        approval_round: int,
    ) -> ApprovalItem | None:
        if self.tamper == "round" and approval_round == 1:
            assert self.stale_open is not None
            return self.stale_open
        return super().get_by_request_attempt_round(request_id, attempt, approval_round)

    def generations(self, request_id: str, attempt: int) -> list[ApprovalItem]:
        if self.tamper == "generations":
            predecessor = InMemoryApprovalStore.get(self, "approval-1")
            assert predecessor is not None
            return [predecessor]
        return super().generations(request_id, attempt)


class _LifecycleRaceHarness:
    """실 Completion UoW·Boundary·evidence·Notifier를 공유하는 경쟁 조립."""

    def __init__(self, *, approvals: InMemoryApprovalStore | None = None) -> None:
        self.approvals = approvals or InMemoryApprovalStore()
        self.policy = _DecisionPolicy()
        self.record_id_calls = 0

        def record_id_factory() -> str:
            self.record_id_calls += 1
            return "record-1"

        self.uow = InMemoryQuestionCompletionUnitOfWork(
            policy=self.policy,
            approvals=self.approvals,
            responsibility_resolver=_RaceResponsibilityResolver(),
            record_id_factory=record_id_factory,
            clock=lambda: T1,
        )
        self.journal = InMemoryApprovalEventJournal()
        self.recorder = ApprovalEventRecorder(self.journal)
        self.channels = {
            recipient: FakeChannel() for recipient in ("alice", "bob", "carol", "fallback-1")
        }
        self.notifier = Notifier(cast(dict[str, NotificationChannel], self.channels))
        self.boundary = ApprovalBoundary(
            requests=self.uow,
            approvals=self.approvals,
            policy=self.policy,
            authorizer=_DecisionAuthorizer(),
            deadline_policy=_RaceDeadline(),
            draft_id_factory=lambda: "draft-1",
            item_id_factory=lambda: "approval-1",
            clock=lambda: T1,
            production_style=True,
            evidence_recorder=self.recorder,
            notifier=self.notifier,
        )
        received = QuestionRequest.receive(
            org_id="org-1",
            requester_id="requester-1",
            question="환불해 주세요.",
            request_id_factory=lambda: "request-1",
            clock=lambda: T0,
            due_at=T1,
        )
        self.uow.create(received)
        ready = received.record_initial_routing(
            intent=ROUTE.intent,
            disposition="routed",
            target=ReadyToDispatch(
                route=ROUTE,
                attempt=1,
                trigger_key="request-dispatch:request-1:1",
                handling=HandlingAssignment(
                    kind="system",
                    ref="request-dispatch:request-1:1",
                    due_at=T1,
                ),
            ),
            clock=lambda: T0,
        )
        assert self.uow.compare_and_set("request-1", 0, received, ready)
        self.boundary.gate_candidate(
            "request-1",
            expected_revision=1,
            candidate=AnswerCandidate(
                text="환불할 수 있습니다.",
                sources=("refund.md",),
                mode="full",
                snapshot_sha="sha-1",
            ),
        )
        item = self.approvals.get("approval-1")
        assert item is not None
        self.item = item
        self.expiry = _ExpiryPolicy()
        self.authorizer: _ReassignmentAuthorizer | _TargetReassignmentAuthorizer = (
            _ReassignmentAuthorizer()
        )
        self.publisher = _Publisher()
        self.id_calls = 0

        def item_id_factory() -> str:
            self.id_calls += 1
            return f"approval-{self.id_calls + 1}"

        self.item_id_factory = item_id_factory
        self._build_app()

    def _build_app(self) -> None:
        self.app = ApprovalOperationsApplication(
            requests=self.uow,
            approvals=self.approvals,
            boundary=self.boundary,
            completion=self.uow,
            reader=self.uow,
            terminal_publisher=self.publisher,
            expiry_policy=cast(Any, self.expiry),
            reassignment_authorizer=cast(Any, self.authorizer),
            item_id_factory=self.item_id_factory,
            clock=lambda: T1,
            evidence_recorder=self.recorder,
            notifier=self.notifier,
        )

    @property
    def assignment(self) -> ApprovalAssignmentGeneration:
        return ApprovalAssignmentGeneration.from_item(self.item)

    def expire_reassign(self) -> None:
        self.expiry.result = ReassignExpiredApproval(
            assignment_generation=self.assignment,
            requirement=ApprovalRequired(
                approver_id="fallback-1",
                policy_version="approval-v2",
            ),
            due_at=T3,
            policy_version="expiry-v1",
            authority_version="org-policy-v1",
            evidence_ref="fallback-rule-1",
        )

    def expire_unavailable(self) -> None:
        self.expiry.result = ApprovalUnavailable(
            assignment_generation=self.assignment,
            policy_version="expiry-v1",
            authority_version="org-policy-v1",
            evidence_ref="no-fallback-1",
        )

    def use_target_authorizer(self) -> _TargetReassignmentAuthorizer:
        authorizer = _TargetReassignmentAuthorizer()
        self.authorizer = authorizer
        self._build_app()
        return authorizer


def test_manual_reassignment_records_once_then_best_effort_notifies() -> None:
    harness = _Harness()
    harness.authorize()
    journal = InMemoryApprovalEventJournal()
    channel = FakeChannel()
    harness.app = _lifecycle_app_with_evidence(
        harness,
        ApprovalEventRecorder(journal),
        notifier=Notifier({"bob": channel}),
    )
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    first = harness.app.reassign("approval-1", principal, target)
    second = harness.app.reassign("approval-1", principal, target)

    assert second == first
    events = journal.for_request("org-1", "request-1")
    assert [event.kind for event in events] == ["reassigned"]
    assert events[0].item_id == first.successor_item_id
    assert events[0].subject.kind == "human"
    assert [notification.subject_ref for notification in channel.delivered] == [
        first.successor_item_id
    ]


@pytest.mark.parametrize(
    ("mode", "expected_kinds"),
    [
        ("reassign", ["expired", "reassigned"]),
        ("unavailable", ["expired", "unavailable"]),
    ],
)
def test_expiry_records_atomic_order_before_success_cache(
    mode: str,
    expected_kinds: list[str],
) -> None:
    harness = _Harness()
    if mode == "reassign":
        harness.expire_reassign()
    else:
        harness.expire_unavailable()
    journal = InMemoryApprovalEventJournal()
    harness.app = _lifecycle_app_with_evidence(
        harness,
        ApprovalEventRecorder(journal),
    )

    first = harness.app.expire_due(T2, limit=1)
    second = harness.app.expire_due(T2, limit=1)

    assert len(first) == 1
    assert second == []
    events = journal.for_request("org-1", "request-1")
    assert [event.kind for event in events] == expected_kinds
    payload = str([event.model_dump(mode="json") for event in events])
    assert "환불할 수 있습니다." not in payload
    assert "fallback-rule-1" not in payload
    assert "no-fallback-1" not in payload


def test_reassignment_notification_failure_never_changes_domain_result() -> None:
    harness = _Harness()
    harness.expire_reassign()
    journal = InMemoryApprovalEventJournal()
    harness.app = _lifecycle_app_with_evidence(
        harness,
        ApprovalEventRecorder(journal),
        notifier=Notifier({"fallback-1": _RaisingNotificationChannel()}),
    )

    result = harness.app.expire_due(T2, limit=1)

    assert len(result) == 1 and isinstance(result[0], ApprovalReassigned)
    assert [event.kind for event in journal.for_request("org-1", "request-1")] == [
        "expired",
        "reassigned",
    ]


def test_evidence_recorder_identity_matcher_is_exact_and_optional() -> None:
    harness = _Harness()
    journal = InMemoryApprovalEventJournal()
    recorder = ApprovalEventRecorder(journal)
    notifier = Notifier()
    harness.app = _lifecycle_app_with_evidence(
        harness,
        recorder,
        notifier=notifier,
    )

    assert harness.app.matches_evidence_dependencies(
        evidence_recorder=recorder,
        notifier=notifier,
    )
    assert not harness.app.matches_evidence_dependencies(
        evidence_recorder=ApprovalEventRecorder(journal),
        notifier=notifier,
    )


def test_32_way_manual_reassignment_records_and_notifies_once() -> None:
    harness = _Harness()
    harness.authorize()
    journal = InMemoryApprovalEventJournal()
    channel = FakeChannel()
    harness.app = _lifecycle_app_with_evidence(
        harness,
        ApprovalEventRecorder(journal),
        notifier=Notifier({"bob": channel}),
    )
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")

    def reassign(_: int) -> ApprovalReassigned:
        return harness.app.reassign("approval-1", principal, target)

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(reassign, range(32)))

    assert len({outcome.successor_item_id for outcome in outcomes}) == 1
    assert [event.kind for event in journal.for_request("org-1", "request-1")] == ["reassigned"]
    assert len(channel.delivered) == 1


def test_expiry_cache_cannot_hide_missing_evidence_and_retry_repairs_it() -> None:
    harness = _Harness()
    harness.expire_reassign()
    journal = _ToggleEventJournal()
    harness.app = _lifecycle_app_with_evidence(
        harness,
        ApprovalEventRecorder(journal),
    )

    assert harness.app.expire_due(T2, limit=1) == [
        ApprovalLifecycleFailure(
            item_id="approval-1",
            request_id="request-1",
            error_code="dependency",
            retryable=True,
        )
    ]
    assert journal.for_request("org-1", "request-1") == ()
    assert not harness.app.has_cached_result("approval-1")

    journal.fail = False
    repaired = harness.app.expire_due(T2, limit=1)
    assert len(repaired) == 1 and isinstance(repaired[0], ApprovalReassigned)
    assert harness.app.has_cached_result("approval-1")
    assert [event.kind for event in journal.for_request("org-1", "request-1")] == [
        "expired",
        "reassigned",
    ]


def test_cached_reassignment_fast_path_repairs_evicted_event_before_return() -> None:
    harness = _Harness()
    harness.authorize()
    journal = _ToggleEventJournal()
    journal.fail = False
    harness.app = _lifecycle_app_with_evidence(
        harness,
        ApprovalEventRecorder(journal),
    )
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    first = harness.app.reassign("approval-1", principal, target)
    assert harness.app.has_cached_result("approval-1")

    journal.target = InMemoryApprovalEventJournal()
    repaired = harness.app.reassign("approval-1", principal, target)

    assert repaired == first
    assert [event.kind for event in journal.for_request("org-1", "request-1")] == ["reassigned"]


def test_one_expiry_evidence_failure_does_not_block_later_due_item() -> None:
    harness = _Harness()
    _add_waiting_approval(harness, request_id="request-2", item_id="approval-other-1")
    journal = _FirstRequestPoisonEventJournal()
    harness.app = _InspectableApprovalOperationsApplication(
        requests=harness.requests,
        approvals=harness.approvals,
        boundary=cast(ApprovalBoundary, object()),
        completion=cast(QuestionCompletionUnitOfWork, object()),
        reader=cast(QuestionCompletionReader, _AnyRequestReader()),
        terminal_publisher=_AnyRequestPublisher(),
        expiry_policy=_DynamicUnavailablePolicy(),
        reassignment_authorizer=cast(Any, harness.authorizer),
        item_id_factory=harness.item_id_factory,
        clock=lambda: T1,
        evidence_recorder=ApprovalEventRecorder(journal),
    )

    results = harness.app.expire_due(T2, limit=2)

    assert results[0] == ApprovalLifecycleFailure(
        item_id="approval-1",
        request_id="request-1",
        error_code="dependency",
        retryable=True,
    )
    assert isinstance(results[1], ApprovalMadeUnavailable)
    assert [event.kind for event in journal.for_request("org-1", "request-2")] == [
        "expired",
        "unavailable",
    ]


@pytest.mark.parametrize("lifecycle_kind", ["expiry_reassign", "unavailable", "manual"])
def test_lifecycle_winner_makes_stale_open_decision_a_field_free_conflict(
    lifecycle_kind: str,
) -> None:
    approvals = _PauseOpenDecisionGetStore()
    harness = _LifecycleRaceHarness(approvals=approvals)
    if lifecycle_kind == "expiry_reassign":
        harness.expire_reassign()
    elif lifecycle_kind == "unavailable":
        harness.expire_unavailable()
    else:
        harness.use_target_authorizer()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    approvals.arm_decision_pause()

    def approve() -> ApprovalAnswered | ApprovalDeclined | ApprovalOperationsError:
        try:
            return harness.app.decide(
                "approval-1",
                ApproverPrincipal(org_id="org-1", subject_id="alice"),
                ApproveIntent(),
            )
        except ApprovalOperationsError as error:
            return error

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approve)
        assert approvals.open_snapshot_read.wait(timeout=5)
        if lifecycle_kind == "manual":
            lifecycle_result: object = harness.app.reassign(
                "approval-1",
                principal,
                target,
            )
            assert type(lifecycle_result) is ApprovalReassigned
        else:
            lifecycle_result = harness.app.expire_due(T2, limit=1)
            assert type(lifecycle_result) is list and len(lifecycle_result) == 1
        approvals.release_decision.set()
        loser = future.result(timeout=5)

    assert type(loser) is ApprovalOperationsConflict
    assert loser.args == () and loser.__dict__ == {}


@pytest.mark.parametrize("tamper", ["direct", "current", "round", "generations"])
def test_stale_open_lifecycle_conflict_recovery_rejects_index_tampering(
    tamper: str,
) -> None:
    approvals = _TamperedLifecycleConflictStore()
    harness = _LifecycleRaceHarness(approvals=approvals)
    harness.expire_reassign()
    approvals.arm_decision_pause()

    def approve() -> ApprovalAnswered | ApprovalDeclined | ApprovalOperationsError:
        try:
            return harness.app.decide(
                "approval-1",
                ApproverPrincipal(org_id="org-1", subject_id="alice"),
                ApproveIntent(),
            )
        except ApprovalOperationsError as error:
            return error

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approve)
        assert approvals.open_snapshot_read.wait(timeout=5)
        lifecycle_result = harness.app.expire_due(T2, limit=1)
        assert len(lifecycle_result) == 1
        approvals.tamper = tamper
        approvals.release_decision.set()
        loser = future.result(timeout=5)

    assert type(loser) is ApprovalOperationsIntegrityError
    assert loser.args == () and loser.__dict__ == {}


def test_stale_open_lifecycle_winner_does_not_reveal_conflict_to_other_principal() -> None:
    approvals = _PauseOpenDecisionGetStore()
    harness = _LifecycleRaceHarness(approvals=approvals)
    harness.expire_reassign()
    approvals.arm_decision_pause()

    def unauthorized_decide() -> ApprovalAnswered | ApprovalDeclined | ApprovalOperationsError:
        try:
            return harness.app.decide(
                "approval-1",
                ApproverPrincipal(org_id="org-1", subject_id="mallory"),
                ApproveIntent(),
            )
        except ApprovalOperationsError as error:
            return error

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(unauthorized_decide)
        assert approvals.open_snapshot_read.wait(timeout=5)
        assert len(harness.app.expire_due(T2, limit=1)) == 1
        approvals.release_decision.set()
        hidden = future.result(timeout=5)

    assert type(hidden) is ApprovalOperationsNotFoundOrDenied
    assert hidden.args == () and hidden.__dict__ == {}


@pytest.mark.parametrize("lifecycle_kind", ["expiry_reassign", "unavailable", "manual"])
def test_32_way_approve_vs_lifecycle_has_one_consistent_domain_arm(
    lifecycle_kind: str,
) -> None:
    harness = _LifecycleRaceHarness()
    if lifecycle_kind == "expiry_reassign":
        harness.expire_reassign()
    elif lifecycle_kind == "unavailable":
        harness.expire_unavailable()
    else:
        harness.use_target_authorizer()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    target = ManualApprovalReassignmentTarget(approver_id="bob")
    start = Barrier(32)

    def race(index: int) -> object:
        start.wait()
        if index < 16:
            try:
                return harness.app.decide(
                    "approval-1",
                    ApproverPrincipal(org_id="org-1", subject_id="alice"),
                    ApproveIntent(),
                )
            except (
                ApprovalOperationsConflict,
                ApprovalOperationsNotFoundOrDenied,
            ) as error:
                return error
        if lifecycle_kind == "manual":
            try:
                return harness.app.reassign("approval-1", principal, target)
            except (
                ApprovalOperationsConflict,
                ApprovalOperationsNotFoundOrDenied,
            ) as error:
                return error
        return harness.app.expire_due(T2, limit=1)

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(race, range(32)))

    assert len(outcomes) == 32
    public_losers = [
        outcome for outcome in outcomes if isinstance(outcome, ApprovalOperationsError)
    ]
    assert all(
        type(error) in (ApprovalOperationsConflict, ApprovalOperationsNotFoundOrDenied)
        and error.args == ()
        and error.__dict__ == {}
        for error in public_losers
    )
    predecessor = harness.approvals.get("approval-1")
    current = harness.approvals.get_by_request_attempt("request-1", 1)
    request = harness.uow.get("request-1")
    bundle = harness.uow.by_request("request-1")
    assert predecessor is not None and current is not None and request is not None
    assert request.revision == 3
    event_kinds = [event.kind for event in harness.journal.for_request("org-1", "request-1")]
    delivered = [
        notification for channel in harness.channels.values() for notification in channel.delivered
    ]

    if isinstance(request.state, AnsweredRequest):
        assert predecessor.status == "resolved" and current == predecessor
        assert bundle is not None and bundle.completion.record_id == "record-1"
        assert harness.record_id_calls == 1
        assert event_kinds == ["requested", "approved"]
        assert [(notice.recipient_id, notice.subject_ref) for notice in delivered] == [
            ("alice", "approval-1")
        ]
    elif isinstance(request.state, AwaitingApproval):
        assert lifecycle_kind in {"expiry_reassign", "manual"}
        assert predecessor.status == "superseded"
        assert current.item_id == "approval-2" and current.status == "open"
        assert current.approval_round == 2 and request.state.draft_ref == current.item_id
        assert bundle is None and harness.record_id_calls == 0
        expected_events = (
            ["requested", "expired", "reassigned"]
            if lifecycle_kind == "expiry_reassign"
            else ["requested", "reassigned"]
        )
        expected_recipient = "fallback-1" if lifecycle_kind == "expiry_reassign" else "bob"
        assert event_kinds == expected_events
        assert [(notice.recipient_id, notice.subject_ref) for notice in delivered] == [
            ("alice", "approval-1"),
            (expected_recipient, "approval-2"),
        ]
    else:
        assert lifecycle_kind == "unavailable"
        assert isinstance(request.state, FailedRequest)
        assert request.state.error_code == "approval_unavailable"
        assert predecessor.status == "unavailable" and current == predecessor
        assert bundle is None and harness.record_id_calls == 0
        assert event_kinds == ["requested", "expired", "unavailable"]
        assert [(notice.recipient_id, notice.subject_ref) for notice in delivered] == [
            ("alice", "approval-1")
        ]

    evidence_payload = str(
        [
            event.model_dump(mode="json")
            for event in harness.journal.for_request("org-1", "request-1")
        ]
    )
    notification_payload = str([notification.model_dump(mode="json") for notification in delivered])
    for secret in (
        "환불해 주세요.",
        "환불할 수 있습니다.",
        "refund.md",
        "fallback-rule-1",
        "no-fallback-1",
        "manual-grant:bob",
    ):
        assert secret not in evidence_payload
        assert secret not in notification_payload


def test_32_way_different_manual_targets_have_one_successor_and_exact_retry() -> None:
    harness = _LifecycleRaceHarness()
    authorizer = harness.use_target_authorizer()
    principal = ApproverPrincipal(org_id="org-1", subject_id="operator-1")
    targets = (
        ManualApprovalReassignmentTarget(approver_id="bob"),
        ManualApprovalReassignmentTarget(approver_id="carol"),
    )
    start = Barrier(32)

    def reassign(
        index: int,
    ) -> tuple[
        ManualApprovalReassignmentTarget,
        ApprovalReassigned | ApprovalOperationsConflict,
    ]:
        target = targets[index % 2]
        start.wait()
        try:
            return target, harness.app.reassign("approval-1", principal, target)
        except ApprovalOperationsConflict as error:
            return target, error

    with ThreadPoolExecutor(max_workers=32) as pool:
        observed = list(pool.map(reassign, range(32)))

    successes = [
        (target, outcome) for target, outcome in observed if type(outcome) is ApprovalReassigned
    ]
    conflicts = [outcome for _, outcome in observed if type(outcome) is ApprovalOperationsConflict]
    assert successes and conflicts
    winning_target = successes[0][0]
    winning_outcome = successes[0][1]
    assert type(winning_outcome) is ApprovalReassigned
    assert all(
        target == winning_target and outcome == winning_outcome for target, outcome in successes
    )
    assert all(error.args == () and error.__dict__ == {} for error in conflicts)
    assert authorizer.calls == 1 and harness.id_calls == 1

    generations = harness.approvals.generations("request-1", 1)
    request = harness.uow.get("request-1")
    assert [item.approval_round for item in generations] == [1, 2]
    assert generations[1].requirement.approver_id == winning_target.approver_id
    assert request is not None and request.revision == 3
    assert isinstance(request.state, AwaitingApproval)
    assert request.state.draft_ref == generations[1].item_id

    assert harness.app.reassign("approval-1", principal, winning_target) == winning_outcome
    losing_target = targets[1] if winning_target == targets[0] else targets[0]
    with pytest.raises(ApprovalOperationsConflict) as caught:
        harness.app.reassign("approval-1", principal, losing_target)
    assert caught.value.args == () and caught.value.__dict__ == {}
    assert authorizer.calls == 1 and harness.id_calls == 1
    assert [event.kind for event in harness.journal.for_request("org-1", "request-1")] == [
        "requested",
        "reassigned",
    ]
    assert len(harness.channels[winning_target.approver_id].delivered) == 1
    assert len(harness.channels[losing_target.approver_id].delivered) == 0


def test_32_way_duplicate_expiry_scan_has_one_atomic_evidence_and_notification() -> None:
    harness = _LifecycleRaceHarness()
    harness.expire_reassign()
    start = Barrier(32)

    def expire(
        _: int,
    ) -> list[ApprovalReassigned | ApprovalMadeUnavailable | ApprovalLifecycleFailure]:
        start.wait()
        return harness.app.expire_due(T2, limit=1)

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(expire, range(32)))

    assert sum(len(outcome) for outcome in outcomes) == 1
    assert harness.expiry.calls == 1 and harness.id_calls == 1
    generations = harness.approvals.generations("request-1", 1)
    assert [item.approval_round for item in generations] == [1, 2]
    request = harness.uow.get("request-1")
    assert request is not None and request.revision == 3
    assert isinstance(request.state, AwaitingApproval)
    events = harness.journal.for_request("org-1", "request-1")
    assert [event.kind for event in events] == ["requested", "expired", "reassigned"]
    assert len(harness.channels["fallback-1"].delivered) == 1
    payload = str(
        [event.model_dump(mode="json") for event in events]
        + [
            notification.model_dump(mode="json")
            for notification in harness.channels["fallback-1"].delivered
        ]
    )
    for secret in ("환불해 주세요.", "환불할 수 있습니다.", "refund.md", "fallback-rule-1"):
        assert secret not in payload
