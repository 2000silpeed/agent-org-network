from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_org_network.demo import build_demo
from agent_org_network.demo_question_surfaces import build_demo_question_surface_composition
from agent_org_network.answer_finalization import (
    InMemoryQuestionCompletionUnitOfWork,
    ResponsibilitySnapshotResolver,
)
from agent_org_network.approval import ApprovalPolicy, ApprovalStore
from agent_org_network.manager_queue import InMemoryManagerQueueStore
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.p17_conflict_disposition import InMemoryConflictDispositionStore
from agent_org_network.presence import PresenceStatus
from agent_org_network.question_resolution import AskQuestion, RequesterPrincipal
from agent_org_network.question_stream_execution import (
    AnsweredQuestionLookup,
    PendingQuestionLookup,
    QuestionSurfaceInterruptedError,
)
from agent_org_network.runtime import StubRuntime


PRINCIPAL = RequesterPrincipal(org_id="demo-org", subject_id="browser-1")


def test_demo_bundle은_P17_conflict_store와_주입된_knowledge_store를_보존한다() -> None:
    knowledge = InMemoryKnowledgeStore()
    bundle = build_demo(runtime=StubRuntime(), knowledge_store=knowledge)

    assert isinstance(bundle.case_store, InMemoryConflictDispositionStore)
    assert bundle.knowledge_store is knowledge


def test_demo_surface는_legacy_knowledge_opt_in이_없어도_빈_P17_reader를_조립한다() -> None:
    bundle = build_demo(runtime=StubRuntime())
    composition = build_demo_question_surface_composition(bundle)
    try:
        assert bundle.knowledge_store is None
        assert composition.conflict_store is bundle.case_store
        assert composition.conflict_disposition is not None
        assert composition.deadlock_manager_disposition is not None
        assert composition.grounding_knowledge_reader is not None
        assert composition.grounding_terminal_failure_recorder is not None
    finally:
        composition.close()


def test_demo_surface가_Routed를_공통_Finalization으로_완료한다() -> None:
    managers = InMemoryManagerQueueStore()
    bundle = build_demo(
        runtime=StubRuntime(),
        manager_queue_store=managers,
    )
    composition = build_demo_question_surface_composition(bundle)
    try:
        result = composition.application.ask(
            AskQuestion(principal=PRINCIPAL, question="환불은 언제 되나요?")
        )

        assert isinstance(result, AnsweredQuestionLookup)
        assert result.request_id
        assert result.record_id
        assert result.agent_id == "cs_ops"
        assert result.answered_by == "cs_lead"
        assert composition.storage.by_request(result.request_id) is not None
        assert composition.manager_store is managers
        assert composition.manager_disposition is not None
    finally:
        composition.close()


def test_demo_surface가_offline_presence_증거를_한_completion에_보존한다() -> None:
    seen: list[str] = []

    def presence_of(owner_id: str) -> PresenceStatus:
        seen.append(owner_id)
        return "offline"

    composition = build_demo_question_surface_composition(
        build_demo(runtime=StubRuntime()),
        presence_of=presence_of,
    )
    try:
        result = composition.application.ask(
            AskQuestion(principal=PRINCIPAL, question="환불은 언제 되나요?")
        )

        assert isinstance(result, AnsweredQuestionLookup)
        stored = composition.storage.by_request(result.request_id)
        assert stored is not None
        assert stored.answer_record.needs_correction_review is True
        assert stored.terminal_audit.responsibility.needs_correction_review is True
        assert stored.terminal_audit.approval.kind == "not_required"
        assert stored.terminal_audit.approval.needs_correction_review is True
        assert seen == ["cs_lead", "cs_lead"]
    finally:
        composition.close()


def test_demo_surface가_online_owner를_본문없는_승인대기로_둔다() -> None:
    def presence_of(_owner_id: str) -> PresenceStatus:
        return "online"

    composition = build_demo_question_surface_composition(
        build_demo(runtime=StubRuntime()),
        presence_of=presence_of,
    )
    try:
        result = composition.application.ask(
            AskQuestion(principal=PRINCIPAL, question="환불은 언제 되나요?")
        )

        assert isinstance(result, PendingQuestionLookup)
        assert result.kind == "routed"
        assert result.state == "awaiting_approval"
        assert composition.storage.by_request(result.request_id) is None
    finally:
        composition.close()


def test_presence가_policy_recheck_사이에_바뀌면_자동발신을_fail_closed한다() -> None:
    statuses: list[PresenceStatus] = ["offline", "online"]
    observed = iter(statuses)

    def presence_of(_owner_id: str) -> PresenceStatus:
        return next(observed)

    composition = build_demo_question_surface_composition(
        build_demo(runtime=StubRuntime()),
        presence_of=presence_of,
    )
    try:
        with pytest.raises(QuestionSurfaceInterruptedError) as error_info:
            composition.application.ask(
                AskQuestion(principal=PRINCIPAL, question="환불은 언제 되나요?")
            )

        assert error_info.value.code == "question_execution_interrupted"
        assert composition.storage.by_request(error_info.value.request_id) is None
    finally:
        composition.close()


def test_demo_surface가_Contested와_Unowned에서_Runtime을_실행하지_않는다() -> None:
    runtime = StubRuntime()
    bundle = build_demo(
        runtime=runtime,
        manager_queue_store=InMemoryManagerQueueStore(),
    )
    composition = build_demo_question_surface_composition(bundle)
    try:
        contested = composition.application.ask(
            AskQuestion(principal=PRINCIPAL, question="보상 기준은 무엇인가요?")
        )
        unowned = composition.application.ask(
            AskQuestion(principal=PRINCIPAL, question="주차 등록은 어떻게 하나요?")
        )

        assert isinstance(contested, PendingQuestionLookup)
        assert contested.kind == "contested"
        assert isinstance(unowned, PendingQuestionLookup)
        assert unowned.kind == "unowned"
        assert runtime.last_context is None
    finally:
        composition.close()


def test_demo_surface가_Approval_필요_Routed를_본문없는_Pending으로_둔다() -> None:
    bundle = build_demo(
        runtime=StubRuntime(),
        manager_queue_store=InMemoryManagerQueueStore(),
    )
    composition = build_demo_question_surface_composition(bundle)
    try:
        result = composition.application.ask(
            AskQuestion(principal=PRINCIPAL, question="평가 기준을 알려 주세요.")
        )

        assert isinstance(result, PendingQuestionLookup)
        assert result.kind == "routed"
        assert result.state == "awaiting_approval"
        assert composition.storage.by_request(result.request_id) is None
    finally:
        composition.close()


def test_demo_surface는_falsy_명시_Manager와_storage_factory를_그대로_쓴다() -> None:
    class _FalsyManagerStore(InMemoryManagerQueueStore):
        def __bool__(self) -> bool:
            return False

    class _FalsyStorageFactory:
        def __bool__(self) -> bool:
            return False

        def __call__(
            self,
            *,
            policy: ApprovalPolicy,
            approvals: ApprovalStore,
            responsibility_resolver: ResponsibilitySnapshotResolver,
        ) -> InMemoryQuestionCompletionUnitOfWork:
            return InMemoryQuestionCompletionUnitOfWork(
                policy=policy,
                approvals=approvals,
                responsibility_resolver=responsibility_resolver,
                record_id_factory=lambda: "falsy-record",
                clock=lambda: datetime.now(timezone.utc),
            )

    managers = _FalsyManagerStore()
    bundle = build_demo(runtime=StubRuntime(), manager_queue_store=managers)
    composition = build_demo_question_surface_composition(
        bundle,
        storage_factory=_FalsyStorageFactory(),
    )
    try:
        answered = composition.application.ask(
            AskQuestion(principal=PRINCIPAL, question="환불은 언제 되나요?")
        )
        unowned = composition.application.ask(
            AskQuestion(principal=PRINCIPAL, question="주차 등록은 어떻게 하나요?")
        )

        assert isinstance(answered, AnsweredQuestionLookup)
        assert answered.record_id == "falsy-record"
        assert isinstance(unowned, PendingQuestionLookup)
        assert managers.get_by_request(unowned.request_id) is not None
    finally:
        composition.close()
