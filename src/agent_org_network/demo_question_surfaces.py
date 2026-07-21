"""개발·단일 프로세스 관통 검증용 P17 사용자 표면 조립.

하드코딩 데모 정책은 production Authority나 조직 인증이 아니다. 실제 설정을 강제하는
entry point는 P17.7~P17.8 범위이며, 여기서는 모든 사용자 채널이 같은 Request와
Finalization을 쓰도록 기존 데모 Registry·Router·중앙/로컬 Runtime을 연결한다.
사용자 지정 SQLite factory도 terminal completion만 선택적으로 내구화한다. Approval·
Conflict·Manager와 실행 lease는 P17.9 전까지 InMemory/단일 프로세스 범위다.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from threading import RLock
from typing import cast

from agent_org_network.agent_card import domain_authorized
from agent_org_network.answer_finalization import (
    AnswerResponsibilitySnapshot,
    InMemoryQuestionCompletionUnitOfWork,
    ResponsibilitySnapshotResolver,
)
from agent_org_network.approval import (
    ApprovalActionKind,
    ApprovalAssignmentGeneration,
    ApprovalAuthorization,
    ApprovalEvaluation,
    ApprovalReassignmentAuthorization,
    ApprovalReassignmentAuthorizationResult,
    ApprovalReassignmentDenied,
    ApprovalUnavailable,
    ApprovalPolicy,
    ApprovalRequired,
    ApprovalStore,
    ApproverPrincipal,
    InMemoryApprovalStore,
    NoApprovalRequired,
    ReassignExpiredApproval,
)
from agent_org_network.approval_evidence import InMemoryApprovalEventJournal
from agent_org_network.approval_retention import (
    ApprovalDraftRetentionDecision,
    ApprovalDraftTerminalEvidence,
)
from agent_org_network.demo import ROOT_USER, DemoBundle
from agent_org_network.knowledge_store import (
    InMemoryKnowledgeStore,
    KnowledgeStoreGroundingKnowledgeReader,
)
from agent_org_network.manager_queue import (
    InMemoryManagerQueueStore,
    RequestAwareManagerQueueStore,
)
from agent_org_network.presence import PresenceStatus
from agent_org_network.p17_manager_disposition import (
    AuthorityAssignment,
    AuthorityAssignmentConflictError,
    AuthorityAssignmentReceipt,
    AuthorityAssignmentRejected,
    RequestScopedRouteAuthority,
)
from agent_org_network.p17_conflict_disposition import InMemoryConflictDispositionStore
from agent_org_network.question_answer_source import RegistryRuntimeQuestionAnswerSource
from agent_org_network.question_request import RequestStateKind, RouteTarget
from agent_org_network.question_resolution import AuthorityGrant
from agent_org_network.request_route_authority import (
    FromUnownedManagerGrant,
    RequestRouteGrantAssignment,
    RequestRouteGrantConflict,
    RequestRouteGrantReceipt,
    RequestRouteGrantRejected,
    RequestRouteGrantResult,
)
from agent_org_network.question_surface_composition import (
    ApprovalEvidenceConfiguration,
    ApprovalLifecycleConfiguration,
    AtomicQuestionCompletionStorage,
    P17ContestedSurfaceConfiguration,
    QuestionCompletionStorageFactory,
    QuestionSurfaceComposition,
    build_question_surface_composition,
)
from agent_org_network.registry import Registry
from agent_org_network.runtime import AnswerMode


DEMO_ORG_ID = "demo-org"
_ROUTE_POLICY_VERSION = "demo-route-v1"
_APPROVAL_POLICY_VERSION = "demo-approval-v1"
_APPROVAL_LIFECYCLE_POLICY_VERSION = "demo-approval-lifecycle-v1"
_APPROVAL_LIFECYCLE_AUTHORITY_VERSION = "demo-registry-authority-v1"
_APPROVAL_RETENTION_POLICY_VERSION = "demo-approval-retention-v1"
Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
PresenceLookup = Callable[[str], PresenceStatus]


class DemoQuestionSurfaceConfigurationError(ValueError):
    """P17 데모 조립에 필요한 공유 구성요소가 빠졌음."""


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class DemoHandlingDeadlinePolicy:
    """개발 파일럿의 모든 nonterminal 처리에 같은 30분 SLA를 부여한다."""

    def deadline_for(
        self,
        org_id: str,
        state_kind: RequestStateKind | str,
        started_at: datetime,
    ) -> datetime:
        del org_id, state_kind
        return started_at + timedelta(minutes=30)


class DemoRouteAuthority:
    """현재 데모 Registry under-claim 안쪽만 허용하는 중앙 정책 stub."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._request_receipts: dict[str, RequestRouteGrantReceipt] = {}
        self._request_grants: dict[tuple[str, str], RequestRouteGrantReceipt] = {}
        self._grant_revision = 0
        self._lock = RLock()

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        if org_id != DEMO_ORG_ID:
            return None
        try:
            card = self._registry.get(agent_id)
            self._registry.get_user(card.owner)
        except KeyError:
            return None
        if not domain_authorized(intent, card):
            return None
        return AuthorityGrant(policy_version=_ROUTE_POLICY_VERSION)

    def assign_owner(
        self,
        assignment: AuthorityAssignment,
    ) -> AuthorityAssignmentReceipt | AuthorityAssignmentRejected:
        """조직 base rule을 바꾸지 않는 Request 한정 Manager grant를 기록한다."""
        canonical = AuthorityAssignment.model_validate(
            assignment.model_dump(mode="python", round_trip=True),
            strict=True,
        )
        common = RequestRouteGrantAssignment(
            org_id=canonical.org_id,
            request_id=canonical.request_id,
            intent=canonical.intent,
            agent_id=canonical.agent_id,
            source=FromUnownedManagerGrant(
                item_id=canonical.item_id,
                by_manager=canonical.assigned_by,
            ),
            idempotency_key=canonical.idempotency_key,
        )
        result = self.grant_for_request(common)
        if isinstance(result, RequestRouteGrantConflict):
            raise AuthorityAssignmentConflictError(
                "한 Request grant가 다른 assignment와 충돌합니다."
            )
        if isinstance(result, RequestRouteGrantRejected):
            return AuthorityAssignmentRejected(reason_code=result.reason_code)
        return AuthorityAssignmentReceipt(
            assignment=canonical,
            grant_version=result.grant_version,
        )

    def grant_for_request(
        self,
        assignment: RequestRouteGrantAssignment,
    ) -> RequestRouteGrantResult:
        """Request별 첫 canonical 책임 assignment 하나만 기록한다."""
        canonical = RequestRouteGrantAssignment.model_validate(
            assignment.model_dump(mode="python", round_trip=True),
            strict=True,
        )
        with self._registry.consistency_guard():
            return self._grant_for_request_under_registry_snapshot(canonical)

    def _grant_for_request_under_registry_snapshot(
        self,
        canonical: RequestRouteGrantAssignment,
    ) -> RequestRouteGrantResult:
        with self._lock:
            existing = self._request_receipts.get(canonical.idempotency_key)
            if existing is not None:
                if existing.assignment != canonical:
                    return RequestRouteGrantConflict()
                return RequestRouteGrantReceipt.model_validate(
                    existing.model_dump(mode="python", round_trip=True),
                    strict=True,
                )
            slot = (canonical.org_id, canonical.request_id)
            winner = self._request_grants.get(slot)
            if winner is not None:
                if winner.assignment != canonical:
                    return RequestRouteGrantConflict()
                return RequestRouteGrantReceipt.model_validate(
                    winner.model_dump(mode="python", round_trip=True),
                    strict=True,
                )
            try:
                card = self._registry.get(canonical.agent_id)
                self._registry.get_user(card.owner)
            except KeyError:
                return RequestRouteGrantRejected(
                    idempotency_key=canonical.idempotency_key,
                    reason_code="target_not_found",
                )
            if canonical.org_id != DEMO_ORG_ID or not domain_authorized(canonical.intent, card):
                return RequestRouteGrantRejected(
                    idempotency_key=canonical.idempotency_key,
                    reason_code="target_not_authorized",
                )
            self._grant_revision += 1
            receipt = RequestRouteGrantReceipt(
                assignment=canonical,
                grant_version=f"demo-request-grant-v{self._grant_revision}",
            )
            self._request_receipts[canonical.idempotency_key] = (
                RequestRouteGrantReceipt.model_validate(receipt.model_dump(), strict=True)
            )
            self._request_grants[slot] = RequestRouteGrantReceipt.model_validate(
                receipt.model_dump(), strict=True
            )
            return RequestRouteGrantReceipt.model_validate(receipt.model_dump(), strict=True)

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None:
        with self._lock:
            receipt = self._request_grants.get((org_id, request_id))
            if receipt is None:
                return None
            assignment = receipt.assignment
            if (
                assignment.org_id != org_id
                or assignment.request_id != request_id
                or assignment.intent != intent
                or assignment.agent_id != agent_id
            ):
                return None
            return AuthorityGrant(policy_version=receipt.grant_version)


class DemoApprovalPolicy:
    """Route·mode 또는 온라인 Owner가 요구할 때 현재 Owner를 승인자로 지정한다."""

    def __init__(
        self,
        registry: Registry,
        presence_of: PresenceLookup | None = None,
    ) -> None:
        self._registry = registry
        self._presence_of = presence_of

    def evaluate(
        self,
        org_id: str,
        route: RouteTarget,
        candidate_mode: AnswerMode,
    ) -> ApprovalEvaluation:
        if org_id != DEMO_ORG_ID:
            raise ValueError("데모 조직 밖 Approval 정책 요청")
        card = self._registry.get(route.agent_id)
        owner = self._registry.get_user(card.owner)
        owner_is_online = False
        if self._presence_of is not None:
            status = self._presence_of(owner.id)
            if status not in ("online", "offline"):
                raise ValueError("Owner presence는 online 또는 offline이어야 합니다.")
            owner_is_online = status == "online"
        if not route.requires_approval and candidate_mode != "draft_only" and not owner_is_online:
            return NoApprovalRequired(
                policy_version=_APPROVAL_POLICY_VERSION,
                needs_correction_review=self._presence_of is not None,
            )
        return ApprovalRequired(
            approver_id=owner.id,
            policy_version=_APPROVAL_POLICY_VERSION,
        )


class DemoApprovalAuthorizer:
    """지정된 데모 승인자 본인만 같은 policy version으로 처분하게 한다."""

    def authorize(
        self,
        org_id: str,
        designated_approver_id: str,
        actor_id: str,
        action_kind: ApprovalActionKind,
        policy_version: str,
    ) -> ApprovalAuthorization | None:
        del action_kind
        if (
            org_id != DEMO_ORG_ID
            or actor_id != designated_approver_id
            or policy_version != _APPROVAL_POLICY_VERSION
        ):
            return None
        return ApprovalAuthorization(policy_version=policy_version)


class DemoApprovalReassignmentAuthorizer:
    """단일 프로세스 데모 Registry 안에서만 manual 재지정을 허가한다.

    production Authority나 RBAC 구현이 아니다. 현재 지정 승인자 본인과 같은 데모
    조직만 명령할 수 있고, 새 승인자는 현재 Registry에 등록된 User여야 한다.
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def authorize(
        self,
        *,
        assignment: ApprovalAssignmentGeneration,
        principal: ApproverPrincipal,
        target_approver_id: str,
        requested_at: datetime,
    ) -> ApprovalReassignmentAuthorizationResult:
        with self._registry.consistency_guard():
            target_exists = True
            try:
                self._registry.get_user(target_approver_id)
            except KeyError:
                target_exists = False
            authorized = (
                assignment.org_id == DEMO_ORG_ID
                and principal.org_id == assignment.org_id
                and principal.subject_id == assignment.requirement.approver_id
                and assignment.requirement.policy_version == _APPROVAL_POLICY_VERSION
                and target_exists
            )
            if not authorized:
                return ApprovalReassignmentDenied(
                    assignment_generation=assignment,
                    org_id=principal.org_id,
                    actor_id=principal.subject_id,
                    target_approver_id=target_approver_id,
                )
            return ApprovalReassignmentAuthorization(
                assignment_generation=assignment,
                org_id=principal.org_id,
                actor_id=principal.subject_id,
                target_approver_id=target_approver_id,
                requirement=ApprovalRequired(
                    approver_id=target_approver_id,
                    policy_version=_APPROVAL_POLICY_VERSION,
                ),
                due_at=requested_at + timedelta(minutes=30),
                policy_version=_APPROVAL_LIFECYCLE_POLICY_VERSION,
                authority_version=_APPROVAL_LIFECYCLE_AUTHORITY_VERSION,
                evidence_ref=(
                    f"demo-reassignment:{assignment.item_id}:"
                    f"{assignment.approval_round}:{principal.subject_id}:"
                    f"{target_approver_id}"
                ),
            )


class DemoApprovalExpiryPolicy:
    """기한이 지난 데모 assignment를 root User 한 번으로만 넘긴다.

    root User가 없거나 이미 root User가 현재 승인자라면 영구 fallback 부재로 봉인한다.
    이 결정은 단일 프로세스 데모 정책이며 production 운영 정책이 아니다.
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def evaluate(
        self,
        *,
        assignment: ApprovalAssignmentGeneration,
        now: datetime,
    ) -> ReassignExpiredApproval | ApprovalUnavailable:
        with self._registry.consistency_guard():
            root_exists = True
            try:
                self._registry.get_user(ROOT_USER)
            except KeyError:
                root_exists = False
            evidence_ref = (
                f"demo-expiry:{assignment.item_id}:{assignment.approval_round}:"
                f"{ROOT_USER if root_exists else 'no-root'}"
            )
            if (
                assignment.org_id == DEMO_ORG_ID
                and root_exists
                and assignment.requirement.approver_id != ROOT_USER
            ):
                return ReassignExpiredApproval(
                    assignment_generation=assignment,
                    requirement=ApprovalRequired(
                        approver_id=ROOT_USER,
                        policy_version=_APPROVAL_POLICY_VERSION,
                    ),
                    due_at=now + timedelta(minutes=30),
                    policy_version=_APPROVAL_LIFECYCLE_POLICY_VERSION,
                    authority_version=_APPROVAL_LIFECYCLE_AUTHORITY_VERSION,
                    evidence_ref=evidence_ref,
                )
            return ApprovalUnavailable(
                assignment_generation=assignment,
                policy_version=_APPROVAL_LIFECYCLE_POLICY_VERSION,
                authority_version=_APPROVAL_LIFECYCLE_AUTHORITY_VERSION,
                evidence_ref=evidence_ref,
            )


class DemoApprovalDraftRetentionPolicy:
    """데모 Approval Draft를 exact terminal 뒤 30일 동안 보존한다.

    purge_eligible은 삭제 완료가 아니라 보존 기한이 지났다는 판정만 뜻한다.
    """

    def evaluate(
        self,
        *,
        terminal: ApprovalDraftTerminalEvidence,
        evaluated_at: datetime,
    ) -> ApprovalDraftRetentionDecision:
        retain_until = terminal.terminal_at.astimezone(UTC) + timedelta(days=30)
        return ApprovalDraftRetentionDecision(
            terminal=terminal,
            evaluated_at=evaluated_at,
            policy_version=_APPROVAL_RETENTION_POLICY_VERSION,
            retain_until=retain_until,
            purge_eligible=evaluated_at.astimezone(UTC) >= retain_until,
        )


class RegistryResponsibilitySnapshotResolver:
    """Finalization 시점의 현재 Agent Card와 Owner User를 Registry에서 확정한다."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def matches_registry(self, registry: Registry) -> bool:
        """P17.5 composition identity gate용 동일 Registry 확인."""
        return self._registry is registry

    def resolve(
        self,
        *,
        org_id: str,
        route: RouteTarget,
    ) -> AnswerResponsibilitySnapshot | None:
        if org_id != DEMO_ORG_ID:
            return None
        try:
            card = self._registry.get(route.agent_id)
            owner = self._registry.get_user(card.owner)
        except KeyError:
            return None
        if not domain_authorized(route.intent, card):
            return None
        return AnswerResponsibilitySnapshot(
            agent_id=card.agent_id,
            owner_id=owner.id,
        )


def _in_memory_storage_factory(
    *,
    record_id_factory: IdFactory,
    clock: Clock,
) -> QuestionCompletionStorageFactory:
    def create(
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> AtomicQuestionCompletionStorage:
        return InMemoryQuestionCompletionUnitOfWork(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
            record_id_factory=record_id_factory,
            clock=clock,
        )

    return create


def build_demo_question_surface_composition(
    bundle: DemoBundle,
    *,
    storage_factory: QuestionCompletionStorageFactory | None = None,
    presence_of: PresenceLookup | None = None,
    clock: Clock = _clock,
    request_id_factory: IdFactory = _new_id,
    record_id_factory: IdFactory = _new_id,
    draft_id_factory: IdFactory = _new_id,
    approval_item_id_factory: IdFactory = _new_id,
) -> QuestionSurfaceComposition:
    """legacy AskOrg를 호출하지 않는 개발 관통용 P17 사용자 표면을 만든다.

    SQLite storage factory를 넘겨도 terminal completion 파일럿일 뿐이며 linked workflow,
    Approval resolve, lease가 함께 durable해지는 것은 아니다.
    """

    if bundle.router is None or bundle.runtime is None:
        raise DemoQuestionSurfaceConfigurationError(
            "데모 Question Surface에는 공유 Router와 중앙/로컬 AgentRuntime이 필요합니다."
        )
    approvals = InMemoryApprovalStore()
    route_authority: RequestScopedRouteAuthority = DemoRouteAuthority(bundle.registry)
    approval_policy = DemoApprovalPolicy(bundle.registry, presence_of)
    responsibility_resolver = RegistryResponsibilitySnapshotResolver(bundle.registry)
    knowledge_store = (
        bundle.knowledge_store if bundle.knowledge_store is not None else InMemoryKnowledgeStore()
    )
    grounding_reader = KnowledgeStoreGroundingKnowledgeReader(knowledge_store)
    approval_events = InMemoryApprovalEventJournal()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=bundle.registry,
        route_authority=route_authority,
        runtime=bundle.runtime,
        conflict_resolution_evidence_reader=cast(
            InMemoryConflictDispositionStore, bundle.case_store
        ),
        grounding_knowledge_reader=grounding_reader,
    )
    managers = (
        bundle.manager_queue_store
        if bundle.manager_queue_store is not None
        else InMemoryManagerQueueStore()
    )
    chosen_storage_factory = (
        storage_factory
        if storage_factory is not None
        else _in_memory_storage_factory(
            record_id_factory=record_id_factory,
            clock=clock,
        )
    )
    return build_question_surface_composition(
        storage_factory=chosen_storage_factory,
        router=bundle.router,
        conflicts=cast(InMemoryConflictDispositionStore, bundle.case_store),
        managers=cast(RequestAwareManagerQueueStore, managers),
        route_authority=route_authority,
        handling_deadline_policy=DemoHandlingDeadlinePolicy(),
        approval_store=approvals,
        approval_policy=approval_policy,
        approval_authorizer=DemoApprovalAuthorizer(),
        approval_deadline_policy=DemoHandlingDeadlinePolicy(),
        responsibility_resolver=responsibility_resolver,
        answer_source=source,
        request_id_factory=request_id_factory,
        draft_id_factory=draft_id_factory,
        approval_item_id_factory=approval_item_id_factory,
        approval_lifecycle_configuration=ApprovalLifecycleConfiguration(
            expiry_policy=DemoApprovalExpiryPolicy(bundle.registry),
            reassignment_authorizer=DemoApprovalReassignmentAuthorizer(bundle.registry),
        ),
        approval_evidence_configuration=ApprovalEvidenceConfiguration(
            journal=approval_events,
            retention_policy=DemoApprovalDraftRetentionPolicy(),
            notifier=None,
        ),
        contested_configuration=P17ContestedSurfaceConfiguration(
            registry=bundle.registry,
            grounding_knowledge_reader=grounding_reader,
            root_user_id=ROOT_USER,
            manager_item_id_factory=_new_id,
            generation_factory=_new_id,
        ),
        clock=clock,
    )
