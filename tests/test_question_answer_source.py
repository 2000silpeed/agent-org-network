"""P17.2c-2 concrete completed-inline QuestionAnswerSource 계약."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import cast

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.dispatch import DispatchingRuntime, InMemoryWorkQueueDispatcher
from agent_org_network.question_answer_source import (
    QuestionAnswerSourceConfigurationError,
    QuestionAnswerSourceError,
    RegistryRuntimeQuestionAnswerSource,
)
from agent_org_network.question_request import (
    AwaitingManager,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.question_resolution import AuthorityGrant, RouteAuthority
from agent_org_network.question_stream_execution import BufferedAnswer
from agent_org_network.registry import Registry
from agent_org_network.runtime import AgentRuntime, Answer, AnswerMode
from agent_org_network.user import User

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _card(
    *,
    agent_id: str = "refund-card",
    owner: str = "owner-1",
    domains: list[str] | None = None,
    cannot_answer: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="support",
        summary="환불 담당",
        domains=["refund"] if domains is None else domains,
        cannot_answer=[] if cannot_answer is None else cannot_answer,
        last_reviewed_at=date(2026, 7, 1),
        knowledge_sources=["refund-policy.md"],
    )


def _registry(card: AgentCard | None = None, *, with_owner: bool = True) -> Registry:
    registry = Registry()
    if with_owner:
        registry.register_user(User(id="owner-1"))
    registry.register(_card() if card is None else card)
    return registry


def _ready_request(
    *,
    intent: str = "refund",
    agent_id: str = "refund-card",
    authority_version: str | None = "route-v1",
) -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="결제 취소는 언제 반영되나요?",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
        context_snapshot="이전 문의: 카드 결제",
    )
    return received.record_initial_routing(
        intent=intent,
        disposition="routed",
        target=ReadyToDispatch(
            route=RouteTarget(
                intent=intent,
                agent_id=agent_id,
                requires_approval=False,
                authority_version=authority_version,
            ),
            attempt=1,
            trigger_key="initial-route:request-1",
            handling=HandlingAssignment(
                kind="system",
                ref="initial-route:request-1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )


def _manager_resumed_request() -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="결제 취소는 언제 반영되나요?",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
        context_snapshot="이전 문의: 카드 결제",
    )
    awaiting = received.record_initial_routing(
        intent="refund",
        disposition="unowned",
        target=AwaitingManager(
            item_id="item-1",
            public_kind="unowned",
            handling=HandlingAssignment(
                kind="manager_item",
                ref="item-1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    return awaiting.transition(
        ReadyToDispatch(
            route=RouteTarget(
                intent="refund",
                agent_id="refund-card",
                requires_approval=False,
                authority_version="request-grant-v1",
            ),
            attempt=1,
            trigger_key="request-dispatch:request-1:1",
            handling=HandlingAssignment(
                kind="system",
                ref="request-dispatch:request-1:1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW + timedelta(seconds=2),
    )


class _Authority:
    def __init__(
        self,
        grant: object = AuthorityGrant(policy_version="route-v1"),
        *,
        error: Exception | None = None,
        callback: object | None = None,
    ) -> None:
        self.grant = grant
        self.error = error
        self.callback = callback
        self.calls: list[tuple[str, str, str]] = []

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        self.calls.append((org_id, intent, agent_id))
        if callable(self.callback):
            self.callback()
        if self.error is not None:
            raise self.error
        return cast(AuthorityGrant | None, self.grant)


class _RequestAuthority(_Authority):
    def __init__(self) -> None:
        super().__init__()
        self.request_calls: list[tuple[str, str, str, str]] = []

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None:
        self.request_calls.append((org_id, request_id, intent, agent_id))
        return AuthorityGrant(policy_version="request-grant-v1")


class _Runtime:
    def __init__(
        self,
        answer: object = Answer(
            text="영업일 기준 3일 이내입니다.",
            sources=("refund-policy.md",),
            mode="full",
            snapshot_sha="abc123",
        ),
        *,
        error: Exception | None = None,
        mutate_card: bool = False,
    ) -> None:
        self.value = answer
        self.error = error
        self.mutate_card = mutate_card
        self.calls: list[tuple[str, AgentCard, str | None, str | None]] = []

    def answer(
        self,
        question: str,
        card: AgentCard,
        context: str | None = None,
        grounding: str | None = None,
    ) -> Answer:
        self.calls.append((question, card, context, grounding))
        if self.mutate_card:
            card.domains.append("mutated-by-runtime")
        if self.error is not None:
            raise self.error
        return cast(Answer, self.value)


def test_completed_inline_source는_현재_evidence를_검증하고_완성_답_한_token을_돌려준다() -> None:
    registry = _registry()
    authority = _Authority()
    runtime = _Runtime()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=registry,
        route_authority=authority,
        runtime=runtime,
    )

    result = source.answer(_ready_request())

    assert isinstance(result, BufferedAnswer)
    assert source.question_answer_source_capability == "completed_inline_v1"
    assert result.candidate.text == "영업일 기준 3일 이내입니다."
    assert result.candidate.sources == ("refund-policy.md",)
    assert result.candidate.mode == "full"
    assert result.candidate.snapshot_sha == "abc123"
    assert result.tokens == ("영업일 기준 3일 이내입니다.",)
    assert authority.calls == [("org-1", "refund", "refund-card")]
    assert len(runtime.calls) == 1
    question, card, context, grounding = runtime.calls[0]
    assert question == "결제 취소는 언제 반영되나요?"
    assert card == registry.get("refund-card")
    assert card is not registry.get("refund-card")
    assert context == "이전 문의: 카드 결제"
    assert grounding is None


def test_completed_inline_source는_Registry와_Authority_identity를_검증할_수_있다() -> None:
    registry = _registry()
    authority = _Authority()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=registry,
        route_authority=authority,
        runtime=_Runtime(),
    )

    assert source.matches_question_answer_dependencies(
        registry=registry,
        route_authority=authority,
    )
    assert not source.matches_question_answer_dependencies(
        registry=_registry(),
        route_authority=authority,
    )
    assert not source.matches_question_answer_dependencies(
        registry=registry,
        route_authority=_Authority(),
    )


def test_Unowned에서_재개된_Request는_request_scoped_Authority만_읽는다() -> None:
    authority = _RequestAuthority()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(),
        route_authority=authority,
        runtime=_Runtime(),
    )

    source.answer(_manager_resumed_request())

    assert authority.request_calls == [("org-1", "request-1", "refund", "refund-card")]
    assert authority.calls == []


def test_Unowned_재개에_request_reader가_없으면_base_Authority로_fallback하지_않는다() -> None:
    authority = _Authority(grant=AuthorityGrant(policy_version="request-grant-v1"))
    source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(),
        route_authority=authority,
        runtime=_Runtime(),
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_manager_resumed_request())

    assert caught.value.code == "request_route_authority_unsupported"
    assert caught.value.retryable is False
    assert authority.calls == []


def test_Unowned_재개_route의_approval_snapshot이_현재_card와_다르면_실행하지_않는다() -> None:
    authority = _RequestAuthority()
    card = _card().model_copy(update={"approval_when": ["refund"]})
    runtime = _Runtime()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(card),
        route_authority=authority,
        runtime=runtime,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_manager_resumed_request())

    assert caught.value.code == "route_approval_snapshot_stale"
    assert authority.request_calls == []
    assert runtime.calls == []


def test_runtime에_넘긴_card는_Registry_backing_value와_alias되지_않는다() -> None:
    registry = _registry()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=registry,
        route_authority=_Authority(),
        runtime=_Runtime(mutate_card=True),
    )

    source.answer(_ready_request())

    assert registry.get("refund-card").domains == ["refund"]


def test_ReadyToDispatch_외_state는_Authority와_Runtime_전에_거부한다() -> None:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="질문",
        request_id_factory=lambda: "request-received",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )
    authority = _Authority()
    runtime = _Runtime()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(),
        route_authority=authority,
        runtime=runtime,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(received)

    assert caught.value.request_id == "request-received"
    assert caught.value.code == "invalid_request_state"
    assert caught.value.retryable is False
    assert authority.calls == []
    assert runtime.calls == []


def test_model_construct로_손상된_ReadyToDispatch는_외부_호출_전에_거부한다() -> None:
    valid = _ready_request()
    state = cast(ReadyToDispatch, valid.state)
    corrupt_state = ReadyToDispatch.model_construct(
        kind="ready_to_dispatch",
        route=RouteTarget.model_construct(
            intent=state.route.intent,
            agent_id=state.route.agent_id,
            requires_approval=False,
            authority_version=7,
        ),
        attempt=state.attempt,
        trigger_key=state.trigger_key,
        handling=state.handling,
    )
    corrupt = QuestionRequest.model_construct(
        **{name: getattr(valid, name) for name in QuestionRequest.model_fields if name != "state"},
        state=corrupt_state,
    )
    authority = _Authority()
    runtime = _Runtime()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(),
        route_authority=authority,
        runtime=runtime,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(corrupt)

    assert caught.value.code == "invalid_question_request"
    assert authority.calls == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("registry", "expected_code"),
    [
        (_registry(_card(agent_id="other-card")), "route_card_not_found"),
        (_registry(with_owner=False), "route_owner_not_found"),
    ],
)
def test_현재_Registry_card나_Owner_User가_없으면_fail_closed한다(
    registry: Registry,
    expected_code: str,
) -> None:
    source = RegistryRuntimeQuestionAnswerSource(
        registry=registry,
        route_authority=_Authority(),
        runtime=_Runtime(),
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_request())

    assert caught.value.code == expected_code
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    "card",
    [
        _card(domains=["billing"]),
        _card(cannot_answer=["refund"]),
    ],
    ids=["outside-domains", "cannot-answer"],
)
def test_card_under_claim이_현재_intent를_책임지지_않으면_Authority_전에_거부한다(
    card: AgentCard,
) -> None:
    authority = _Authority()
    runtime = _Runtime()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(card),
        route_authority=authority,
        runtime=runtime,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_request())

    assert caught.value.code == "route_under_claim_mismatch"
    assert authority.calls == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("grant", "expected_code"),
    [
        (None, "route_authority_denied"),
        (AuthorityGrant(policy_version="route-v2"), "route_authority_stale"),
        (AuthorityGrant.model_construct(policy_version=" "), "invalid_route_authority_grant"),
        (object(), "invalid_route_authority_grant"),
    ],
)
def test_현재_Authority_grant가_없거나_손상됐거나_version이_다르면_거부한다(
    grant: object,
    expected_code: str,
) -> None:
    runtime = _Runtime()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(),
        route_authority=_Authority(grant),
        runtime=runtime,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_request())

    assert caught.value.code == expected_code
    assert runtime.calls == []


def test_route_authority_version이_없는_저장_snapshot은_호출_전에_거부한다() -> None:
    authority = _Authority()
    source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(),
        route_authority=authority,
        runtime=_Runtime(),
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_request(authority_version=None))

    assert caught.value.code == "invalid_route_authority_evidence"
    assert authority.calls == []


def test_Authority_호출_중_card가_바뀌면_stale_card로_Runtime을_호출하지_않는다() -> None:
    registry = _registry()
    runtime = _Runtime()
    authority = _Authority(callback=lambda: registry.replace_card(_card(domains=["billing"])))
    source = RegistryRuntimeQuestionAnswerSource(
        registry=registry,
        route_authority=authority,
        runtime=runtime,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_request())

    assert caught.value.code == "route_registry_changed"
    assert runtime.calls == []


def test_Authority와_Runtime_예외는_내부_세부를_숨긴_구조화_오류다() -> None:
    authority_source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(),
        route_authority=_Authority(error=RuntimeError("secret-authority-token")),
        runtime=_Runtime(),
    )
    with pytest.raises(QuestionAnswerSourceError) as authority_error:
        authority_source.answer(_ready_request())
    assert authority_error.value.code == "route_authority_unavailable"
    assert authority_error.value.retryable is True
    assert "secret" not in str(authority_error.value)

    runtime_source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(),
        route_authority=_Authority(),
        runtime=_Runtime(error=RuntimeError("secret-runtime-token")),
    )
    with pytest.raises(QuestionAnswerSourceError) as runtime_error:
        runtime_source.answer(_ready_request())
    assert runtime_error.value.code == "answer_runtime_unavailable"
    assert runtime_error.value.retryable is True
    assert "secret" not in str(runtime_error.value)


@pytest.mark.parametrize(
    "answer",
    [
        object(),
        Answer(text=" "),
        Answer(text="답", sources=("",)),
        Answer(text="답", mode=cast(AnswerMode, "invalid")),
        Answer(text="답", snapshot_sha=" "),
    ],
    ids=["other-type", "blank-text", "blank-source", "invalid-mode", "blank-sha"],
)
def test_Runtime의_다른_타입_빈값_손상_Answer는_구조화_오류로_거부한다(
    answer: object,
) -> None:
    source = RegistryRuntimeQuestionAnswerSource(
        registry=_registry(),
        route_authority=_Authority(),
        runtime=_Runtime(answer),
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_request())

    assert caught.value.code == "invalid_runtime_answer"
    assert caught.value.retryable is False
    assert "validation" not in str(caught.value).lower()


@pytest.mark.parametrize("legacy_kind", ["dispatching-runtime", "raw-dispatcher"])
def test_pending을_답으로_바꾸는_legacy_async_adapter는_생성_때_명시_거부한다(
    legacy_kind: str,
) -> None:
    dispatcher = InMemoryWorkQueueDispatcher()
    runtime: object = (
        DispatchingRuntime(dispatcher) if legacy_kind == "dispatching-runtime" else dispatcher
    )

    with pytest.raises(QuestionAnswerSourceConfigurationError) as caught:
        RegistryRuntimeQuestionAnswerSource(
            registry=_registry(),
            route_authority=cast(RouteAuthority, _Authority()),
            runtime=cast(AgentRuntime, runtime),
        )

    assert caught.value.code == "legacy_async_runtime_not_supported"
