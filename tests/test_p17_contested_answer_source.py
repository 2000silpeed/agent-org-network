"""P17.5 S4 contested completed-inline Answer Source 계약."""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.conflict import Candidate, ConflictCase
from agent_org_network.grounding import assemble_grounding_knowledge_text
from agent_org_network.grounding_terminal_failure import (
    GroundingTerminalFailureCode,
    GroundingTerminalFailureRequested,
)
from agent_org_network.knowledge_store import (
    GroundingKnowledgeFound,
    GroundingKnowledgeInvalid,
    GroundingKnowledgeMissing,
    GroundingKnowledgeReader,
)
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc
from agent_org_network.p17_conflict_disposition import (
    ConflictResolutionEvidence,
    ConflictResolutionEvidenceReader,
    FromDirectConsensus,
    FromManagerMediation,
    OwnerConcurrenceEvidence,
    SupportingKnowledgeEvidence,
)
from agent_org_network.question_answer_source import (
    QuestionAnswerSourceError,
    RegistryRuntimeQuestionAnswerSource,
)
from agent_org_network.question_request import (
    AwaitingConflict,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.question_resolution import AuthorityGrant
from agent_org_network.question_stream_execution import BufferedAnswer
from agent_org_network.registry import Registry
from agent_org_network.runtime import Answer
from agent_org_network.user import User

NOW = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)


def _card(
    agent_id: str,
    owner: str,
    *,
    summary: str | None = None,
    domains: tuple[str, ...] = ("refund",),
    approval_when: tuple[str, ...] = (),
    knowledge_sources: tuple[str, ...] = (),
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="support",
        summary=agent_id if summary is None else summary,
        domains=list(domains),
        approval_when=list(approval_when),
        knowledge_sources=list(knowledge_sources),
        last_reviewed_at=date(2026, 7, 1),
    )


def _registry(*, primary_requires_approval: bool = False) -> Registry:
    registry = Registry()
    registry.register_user(User(id="owner-a"))
    registry.register_user(User(id="owner-b"))
    registry.register(
        _card(
            "refund-card",
            "owner-a",
            approval_when=("refund",) if primary_requires_approval else (),
            knowledge_sources=("shared.md", "refund-source.md"),
        )
    )
    registry.register(
        _card(
            "finance-card",
            "owner-b",
            knowledge_sources=("shared.md", "finance-source.md"),
        )
    )
    return registry


def _registry_with_supporting(card: AgentCard) -> Registry:
    registry = _registry()
    registry.replace_card(card)
    return registry


def _route(*, requires_approval: bool = False) -> RouteTarget:
    return RouteTarget(
        intent="refund",
        agent_id="refund-card",
        requires_approval=requires_approval,
        authority_version="request-grant-v1",
    )


def _ready_contested_request(*, requires_approval: bool = False) -> QuestionRequest:
    received = QuestionRequest.receive(
        org_id="org-1",
        requester_id="requester-1",
        question="환불 정산 기준은?",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
        context_snapshot="기존 문의 맥락",
    )
    awaiting = received.record_initial_routing(
        intent="refund",
        disposition="contested",
        target=AwaitingConflict(
            case_id="case-1",
            handling=HandlingAssignment(
                kind="conflict_case",
                ref="case-1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    return awaiting.transition(
        ReadyToDispatch(
            route=_route(requires_approval=requires_approval),
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


def _open_case() -> ConflictCase:
    return ConflictCase.for_request(
        request_id="request-1",
        intent="refund",
        question="환불 정산 기준은?",
        candidates=(
            Candidate(agent_id="refund-card", owner="owner-a"),
            Candidate(agent_id="finance-card", owner="owner-b"),
        ),
        opened_at=NOW,
        case_id="case-1",
    )


def _votes() -> tuple[OwnerConcurrenceEvidence, ...]:
    return (
        OwnerConcurrenceEvidence(
            round=1,
            owner_id="owner-a",
            on_agent="refund-card",
            stance="withdraw",
            rationale="A",
        ),
        OwnerConcurrenceEvidence(
            round=1,
            owner_id="owner-b",
            on_agent="refund-card",
            stance="keep_as_complement",
            rationale="B",
        ),
    )


def _direct_case() -> ConflictCase:
    return _open_case().resolve_for_request(
        "refund-card",
        rationale="owner-a→refund-card; owner-b→refund-card",
    )


def _direct_evidence(*, requires_approval: bool = False) -> ConflictResolutionEvidence:
    return ConflictResolutionEvidence(
        request_id="request-1",
        case_id="case-1",
        org_id="org-1",
        intent="refund",
        route=_route(requires_approval=requires_approval),
        source=FromDirectConsensus(round=1, votes=_votes()),
        supporting=(
            SupportingKnowledgeEvidence(
                agent_id="finance-card",
                affirmed_by_owner="owner-b",
            ),
        ),
    )


def _manager_case() -> ConflictCase:
    return (
        _open_case()
        .escalate("item-1")
        .resolve_for_request(
            "refund-card",
            rationale="Manager가 환불 담당으로 중재",
        )
    )


def _manager_evidence() -> ConflictResolutionEvidence:
    return ConflictResolutionEvidence(
        request_id="request-1",
        case_id="case-1",
        org_id="org-1",
        intent="refund",
        route=_route(),
        source=FromManagerMediation(item_id="item-1", by_manager="manager-a"),
        supporting=(),
    )


def _content(agent_id: str, path: str, body: str) -> KnowledgeBundleContent:
    return KnowledgeBundleContent(
        agent_id=agent_id,
        documents=(KnowledgeDoc(path=path, body=body),),
        version="v1",
        synced_at=NOW,
    )


def _found(agent_id: str, path: str, body: str) -> GroundingKnowledgeFound:
    return GroundingKnowledgeFound(
        agent_id=agent_id,
        content=_content(agent_id, path, body),
    )


class _EvidenceReader:
    def __init__(
        self,
        evidence: object,
        case: object,
        *,
        error: Exception | None = None,
    ) -> None:
        self.evidence = evidence
        self.case = case
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def resolution_evidence_for_request(self, request_id: str) -> ConflictResolutionEvidence | None:
        self.calls.append(("evidence", request_id))
        if self.error is not None:
            raise self.error
        return cast(ConflictResolutionEvidence | None, self.evidence)

    def get_request_case(self, case_id: str) -> ConflictCase | None:
        self.calls.append(("case", case_id))
        if self.error is not None:
            raise self.error
        return cast(ConflictCase | None, self.case)


class _GroundingReader:
    def __init__(
        self,
        results: dict[str, object],
        *,
        error: Exception | None = None,
        after_read: Callable[[str, int], None] | None = None,
    ) -> None:
        self.results = results
        self.error = error
        self.after_read = after_read
        self.calls: list[str] = []

    def read(self, agent_id: str) -> object:
        self.calls.append(agent_id)
        if self.error is not None:
            raise self.error
        result = self.results[agent_id]
        if self.after_read is not None:
            self.after_read(agent_id, len(self.calls))
        return result


class _RequestAuthority:
    def __init__(self, *, callback: Callable[[], None] | None = None) -> None:
        self.callback = callback
        self.base_calls: list[tuple[str, str, str]] = []
        self.request_calls: list[tuple[str, str, str, str]] = []

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant:
        self.base_calls.append((org_id, intent, agent_id))
        return AuthorityGrant(policy_version="request-grant-v1")

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant:
        self.request_calls.append((org_id, request_id, intent, agent_id))
        if self.callback is not None:
            self.callback()
        return AuthorityGrant(policy_version="request-grant-v1")


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, AgentCard, str | None, str | None]] = []

    def answer(
        self,
        question: str,
        card: AgentCard,
        context: str | None = None,
        grounding: str | None = None,
    ) -> Answer:
        self.calls.append((question, card, context, grounding))
        return Answer(
            text="환불과 정산 기준을 함께 확인했습니다.",
            sources=("runtime-claimed-source.md",),
            mode="full",
            snapshot_sha="sha-1",
        )


def _source(
    *,
    registry: Registry | None = None,
    evidence_reader: object | None = None,
    grounding_reader: object | None = None,
    authority: _RequestAuthority | None = None,
    runtime: _Runtime | None = None,
) -> tuple[
    RegistryRuntimeQuestionAnswerSource,
    Registry,
    _RequestAuthority,
    _Runtime,
]:
    selected_registry = _registry() if registry is None else registry
    selected_authority = _RequestAuthority() if authority is None else authority
    selected_runtime = _Runtime() if runtime is None else runtime
    return (
        RegistryRuntimeQuestionAnswerSource(
            registry=selected_registry,
            route_authority=selected_authority,
            runtime=selected_runtime,
            conflict_resolution_evidence_reader=cast(
                ConflictResolutionEvidenceReader | None,
                evidence_reader,
            ),
            grounding_knowledge_reader=cast(
                GroundingKnowledgeReader | None,
                grounding_reader,
            ),
        ),
        selected_registry,
        selected_authority,
        selected_runtime,
    )


def _valid_grounding_reader() -> _GroundingReader:
    return _GroundingReader(
        {
            "refund-card": _found("refund-card", "refund.md", "환불은 7일 이내입니다."),
            "finance-card": _found("finance-card", "finance.md", "정산은 익월에 처리합니다."),
        }
    )


def test_direct_Contested는_evidence를_검증한뒤_primary와_positive_supporting만_접지한다() -> None:
    evidence_reader = _EvidenceReader(_direct_evidence(), _direct_case())
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, authority, runtime = _source(
        evidence_reader=evidence_reader,
        grounding_reader=grounding_reader,
    )

    result = source.answer(_ready_contested_request())

    assert isinstance(result, BufferedAnswer)
    assert evidence_reader.calls == [
        ("evidence", "request-1"),
        ("case", "case-1"),
    ]
    assert authority.request_calls == [("org-1", "request-1", "refund", "refund-card")]
    assert authority.base_calls == []
    assert grounding_reader.calls == ["refund-card", "finance-card"]
    assert len(runtime.calls) == 1
    question, runtime_card, context, grounding = runtime.calls[0]
    assert question == "환불 정산 기준은?"
    assert runtime_card.agent_id == "refund-card"
    assert context == "기존 문의 맥락"
    assert grounding == assemble_grounding_knowledge_text(
        (
            cast(GroundingKnowledgeFound, grounding_reader.results["refund-card"]),
            cast(GroundingKnowledgeFound, grounding_reader.results["finance-card"]),
        )
    )
    assert result.candidate.sources == (
        "shared.md",
        "refund-source.md",
        "finance-source.md",
    )
    assert "runtime-claimed-source.md" not in result.candidate.sources


def test_Manager_mediation은_supporting을_만들지않고_primary만_접지한다() -> None:
    evidence_reader = _EvidenceReader(_manager_evidence(), _manager_case())
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, authority, runtime = _source(
        evidence_reader=evidence_reader,
        grounding_reader=grounding_reader,
    )

    result = source.answer(_ready_contested_request())

    assert isinstance(result, BufferedAnswer)
    assert grounding_reader.calls == ["refund-card"]
    assert authority.request_calls == [("org-1", "request-1", "refund", "refund-card")]
    assert len(runtime.calls) == 1
    assert result.candidate.sources == ("shared.md", "refund-source.md")


@pytest.mark.parametrize(
    ("evidence_reader", "grounding_reader"),
    [
        (None, _valid_grounding_reader()),
        (_EvidenceReader(_direct_evidence(), _direct_case()), None),
    ],
    ids=["evidence-reader-missing", "grounding-reader-missing"],
)
def test_Contested_dependency가_누락되면_Authority와_Runtime_전에_fail_closed한다(
    evidence_reader: object | None,
    grounding_reader: object | None,
) -> None:
    source, _registry_value, authority, runtime = _source(
        evidence_reader=evidence_reader,
        grounding_reader=grounding_reader,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "contested_grounding_dependencies_missing"
    assert caught.value.retryable is False
    assert authority.request_calls == []
    assert authority.base_calls == []
    assert runtime.calls == []


def test_evidence_reader_예외는_retryable이고_Authority_grounding_Runtime이_0이다() -> None:
    evidence_reader = _EvidenceReader(
        _direct_evidence(),
        _direct_case(),
        error=RuntimeError("secret evidence backend"),
    )
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, authority, runtime = _source(
        evidence_reader=evidence_reader,
        grounding_reader=grounding_reader,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "conflict_resolution_evidence_unavailable"
    assert caught.value.retryable is True
    assert "secret" not in str(caught.value)
    assert authority.request_calls == []
    assert grounding_reader.calls == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("evidence", "case"),
    [
        (None, _direct_case()),
        (_direct_evidence(), None),
        (
            _direct_evidence().model_copy(
                update={"request_id": "other-request"},
            ),
            _direct_case(),
        ),
        (
            _direct_evidence().model_copy(
                update={
                    "route": RouteTarget(
                        intent="refund",
                        agent_id="finance-card",
                        requires_approval=False,
                        authority_version="request-grant-v1",
                    )
                }
            ),
            _direct_case(),
        ),
        (_direct_evidence(), _open_case()),
        (
            _direct_evidence(),
            _open_case().resolve_for_request(
                "finance-card",
                rationale="owner-a→finance-card; owner-b→finance-card",
            ),
        ),
        (
            _direct_evidence().model_copy(
                update={
                    "source": FromDirectConsensus(
                        round=1,
                        votes=tuple(reversed(_votes())),
                    )
                },
            ),
            _direct_case(),
        ),
        (
            _direct_evidence().model_copy(update={"supporting": ()}),
            _direct_case(),
        ),
        (_manager_evidence(), _direct_case()),
    ],
    ids=[
        "evidence-missing",
        "case-missing",
        "request-link-mismatch",
        "evidence-route-mismatch",
        "case-not-resolved",
        "resolution-primary-mismatch",
        "owner-vote-order-mismatch",
        "supporting-mismatch",
        "manager-source-with-direct-case",
    ],
)
def test_evidence_Case_source_supporting이_exact하지않으면_Authority전에_거부한다(
    evidence: object,
    case: object,
) -> None:
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, authority, runtime = _source(
        evidence_reader=_EvidenceReader(evidence, case),
        grounding_reader=grounding_reader,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "invalid_conflict_resolution_evidence"
    assert caught.value.retryable is False
    assert authority.request_calls == []
    assert grounding_reader.calls == []
    assert runtime.calls == []


def test_변조된_resolved_Case의_decline_reason은_Authority전에_거부한다() -> None:
    case = _direct_case()
    object.__setattr__(case, "decline_reason", "manager_declined")
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, authority, runtime = _source(
        evidence_reader=_EvidenceReader(_direct_evidence(), case),
        grounding_reader=grounding_reader,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "invalid_conflict_resolution_evidence"
    assert caught.value.retryable is False
    assert authority.request_calls == []
    assert grounding_reader.calls == []
    assert runtime.calls == []


def test_변조된_Manager_resolved_Case의_0_round는_Authority전에_거부한다() -> None:
    case = _manager_case()
    object.__setattr__(case, "concurrence_round", 0)
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, authority, runtime = _source(
        evidence_reader=_EvidenceReader(_manager_evidence(), case),
        grounding_reader=grounding_reader,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "invalid_conflict_resolution_evidence"
    assert caught.value.retryable is False
    assert authority.request_calls == []
    assert grounding_reader.calls == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    "forged_authority",
    [False, 0.0],
    ids=["bool-false", "float-zero"],
)
def test_supporting_authority의_0_유사값은_Authority전에_거부한다(
    forged_authority: object,
) -> None:
    evidence = _direct_evidence()
    object.__setattr__(evidence.supporting[0], "authority", forged_authority)
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, authority, runtime = _source(
        evidence_reader=_EvidenceReader(evidence, _direct_case()),
        grounding_reader=grounding_reader,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "invalid_conflict_resolution_evidence"
    assert caught.value.retryable is False
    assert authority.request_calls == []
    assert grounding_reader.calls == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("terminal_agent_id", "terminal_result", "expected_code", "expected_calls"),
    [
        (
            "refund-card",
            GroundingKnowledgeMissing(agent_id="refund-card"),
            "required_grounding_missing",
            ["refund-card"],
        ),
        (
            "refund-card",
            GroundingKnowledgeInvalid(
                agent_id="refund-card",
                reason_code="empty_documents",
            ),
            "required_grounding_invalid",
            ["refund-card"],
        ),
        (
            "finance-card",
            GroundingKnowledgeMissing(agent_id="finance-card"),
            "required_grounding_missing",
            ["refund-card", "finance-card"],
        ),
        (
            "finance-card",
            GroundingKnowledgeInvalid(
                agent_id="finance-card",
                reason_code="invalid_document",
            ),
            "required_grounding_invalid",
            ["refund-card", "finance-card"],
        ),
    ],
)
def test_primary_supporting_Missing_Invalid는_Runtime없이_same_revision_terminal_명령을_돌린다(
    terminal_agent_id: str,
    terminal_result: object,
    expected_code: GroundingTerminalFailureCode,
    expected_calls: list[str],
) -> None:
    results: dict[str, object] = {
        "refund-card": _found("refund-card", "refund.md", "환불 본문"),
        "finance-card": _found("finance-card", "finance.md", "정산 본문"),
    }
    results[terminal_agent_id] = terminal_result
    grounding_reader = _GroundingReader(results)
    source, _registry_value, authority, runtime = _source(
        evidence_reader=_EvidenceReader(_direct_evidence(), _direct_case()),
        grounding_reader=grounding_reader,
    )
    request = _ready_contested_request()

    result = source.answer(request)

    assert result == GroundingTerminalFailureRequested(
        request_id="request-1",
        expected_revision=request.revision,
        error_code=expected_code,
    )
    assert grounding_reader.calls == expected_calls
    assert authority.request_calls == [("org-1", "request-1", "refund", "refund-card")]
    assert runtime.calls == []


def test_grounding_reader_예외는_Ready를_바꾸지않는_retryable_interrupted다() -> None:
    grounding_reader = _GroundingReader({}, error=RuntimeError("secret knowledge backend"))
    source, _registry_value, _authority, runtime = _source(
        evidence_reader=_EvidenceReader(_direct_evidence(), _direct_case()),
        grounding_reader=grounding_reader,
    )
    request = _ready_contested_request()

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(request)

    assert caught.value.code == "grounding_read_interrupted"
    assert caught.value.retryable is True
    assert "secret" not in str(caught.value)
    assert isinstance(request.state, ReadyToDispatch)
    assert request.revision == 2
    assert runtime.calls == []


def test_transient_grounding_read뒤_same_Request_retry는_한번만_Runtime을_호출한다() -> None:
    valid = _valid_grounding_reader()
    grounding_reader = _GroundingReader(
        valid.results,
        error=RuntimeError("temporary knowledge backend"),
    )
    source, _registry_value, authority, runtime = _source(
        evidence_reader=_EvidenceReader(_direct_evidence(), _direct_case()),
        grounding_reader=grounding_reader,
    )
    request = _ready_contested_request()

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(request)
    assert caught.value.code == "grounding_read_interrupted"
    assert runtime.calls == []

    grounding_reader.error = None
    result = source.answer(request)

    assert isinstance(result, BufferedAnswer)
    assert isinstance(request.state, ReadyToDispatch)
    assert request.revision == 2
    assert grounding_reader.calls == ["refund-card", "refund-card", "finance-card"]
    assert authority.request_calls == [
        ("org-1", "request-1", "refund", "refund-card"),
        ("org-1", "request-1", "refund", "refund-card"),
    ]
    assert len(runtime.calls) == 1


@pytest.mark.parametrize(
    "invalid_result",
    [
        object(),
        GroundingKnowledgeMissing(agent_id="finance-card"),
        GroundingKnowledgeFound(
            agent_id="finance-card",
            content=_content("finance-card", "finance.md", "정산 본문"),
        ),
        GroundingKnowledgeFound(
            agent_id="refund-card",
            content=_content("finance-card", "finance.md", "정산 본문"),
        ),
    ],
    ids=[
        "unknown-result-type",
        "missing-agent-id-mismatch",
        "found-result-agent-id-mismatch",
        "found-content-agent-id-mismatch",
    ],
)
def test_grounding_reader_포트위반은_nonretryable이고_terminal로_낮추지않는다(
    invalid_result: object,
) -> None:
    grounding_reader = _GroundingReader(
        {
            "refund-card": invalid_result,
            "finance-card": _found("finance-card", "finance.md", "정산 본문"),
        }
    )
    source, _registry_value, _authority, runtime = _source(
        evidence_reader=_EvidenceReader(_direct_evidence(), _direct_case()),
        grounding_reader=grounding_reader,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "grounding_reader_protocol_invalid"
    assert caught.value.retryable is False
    assert runtime.calls == []


def test_Authority_중_supporting_card가_바뀌면_grounding_Runtime을_호출하지않는다() -> None:
    registry = _registry()
    authority = _RequestAuthority(
        callback=lambda: registry.replace_card(
            _card(
                "finance-card",
                "owner-b",
                summary="changed-after-authority",
                knowledge_sources=("shared.md", "finance-source.md"),
            )
        )
    )
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, _authority, runtime = _source(
        registry=registry,
        evidence_reader=_EvidenceReader(_direct_evidence(), _direct_case()),
        grounding_reader=grounding_reader,
        authority=authority,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "route_registry_changed"
    assert grounding_reader.calls == []
    assert runtime.calls == []


def test_grounding_조회중_card가_바뀌면_Runtime을_호출하지않는다() -> None:
    registry = _registry()

    def mutate_after_supporting(_agent_id: str, count: int) -> None:
        if count == 2:
            registry.replace_card(
                _card(
                    "finance-card",
                    "owner-b",
                    summary="changed-after-grounding",
                    knowledge_sources=("shared.md", "finance-source.md"),
                )
            )

    grounding_reader = _GroundingReader(
        _valid_grounding_reader().results,
        after_read=mutate_after_supporting,
    )
    source, _registry_value, _authority, runtime = _source(
        registry=registry,
        evidence_reader=_EvidenceReader(_direct_evidence(), _direct_case()),
        grounding_reader=grounding_reader,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "route_registry_changed"
    assert grounding_reader.calls == ["refund-card", "finance-card"]
    assert runtime.calls == []


def test_Contested_approval_snapshot은_Authority전에_현재_primary와_exact해야한다() -> None:
    registry = _registry(primary_requires_approval=True)
    source, _registry_value, authority, runtime = _source(
        registry=registry,
        evidence_reader=_EvidenceReader(_direct_evidence(), _direct_case()),
        grounding_reader=_valid_grounding_reader(),
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == "route_approval_snapshot_stale"
    assert authority.request_calls == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("registry", "expected_code"),
    [
        (
            _registry_with_supporting(
                _card(
                    "finance-card",
                    "owner-a",
                    knowledge_sources=("finance-source.md",),
                )
            ),
            "route_registry_changed",
        ),
        (
            _registry_with_supporting(
                _card(
                    "finance-card",
                    "owner-b",
                    domains=("billing",),
                    knowledge_sources=("finance-source.md",),
                )
            ),
            "route_under_claim_mismatch",
        ),
        (
            _registry_with_supporting(
                _card(
                    "finance-card",
                    "owner-b",
                    knowledge_sources=(" ",),
                )
            ),
            "invalid_registry_card",
        ),
    ],
    ids=[
        "supporting-owner-changed",
        "supporting-under-claim-changed",
        "supporting-blank-knowledge-source",
    ],
)
def test_supporting의_현재_Owner_under_claim_source도_Authority전에_검증한다(
    registry: Registry,
    expected_code: str,
) -> None:
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, authority, runtime = _source(
        registry=registry,
        evidence_reader=_EvidenceReader(_direct_evidence(), _direct_case()),
        grounding_reader=grounding_reader,
    )

    with pytest.raises(QuestionAnswerSourceError) as caught:
        source.answer(_ready_contested_request())

    assert caught.value.code == expected_code
    assert authority.request_calls == []
    assert grounding_reader.calls == []
    assert runtime.calls == []


def test_withdraw_losing_candidate는_typed_grounding_reader를_조회하지않는다() -> None:
    registry = _registry()
    registry.register_user(User(id="owner-c"))
    registry.register(
        _card(
            "legal-card",
            "owner-c",
            knowledge_sources=("legal-source.md",),
        )
    )
    votes = (
        *_votes(),
        OwnerConcurrenceEvidence(
            round=1,
            owner_id="owner-c",
            on_agent="refund-card",
            stance="withdraw",
            rationale="C",
        ),
    )
    case = ConflictCase.for_request(
        request_id="request-1",
        intent="refund",
        question="환불 정산 기준은?",
        candidates=(
            Candidate(agent_id="refund-card", owner="owner-a"),
            Candidate(agent_id="finance-card", owner="owner-b"),
            Candidate(agent_id="legal-card", owner="owner-c"),
        ),
        opened_at=NOW,
        case_id="case-1",
    ).resolve_for_request(
        "refund-card",
        rationale="owner-a→refund-card; owner-b→refund-card; owner-c→refund-card",
    )
    evidence = _direct_evidence().model_copy(
        update={"source": FromDirectConsensus(round=1, votes=votes)}
    )
    grounding_reader = _valid_grounding_reader()
    source, _registry_value, _authority, runtime = _source(
        registry=registry,
        evidence_reader=_EvidenceReader(evidence, case),
        grounding_reader=grounding_reader,
    )

    result = source.answer(_ready_contested_request())

    assert isinstance(result, BufferedAnswer)
    assert grounding_reader.calls == ["refund-card", "finance-card"]
    assert len(runtime.calls) == 1


def test_Contested_Answer_Source는_Precedent나_ComplementEdge를_import하지않는다() -> None:
    path = Path(__file__).parents[1] / "src" / "agent_org_network" / "question_answer_source.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert imported.isdisjoint({"precedent", "complement"})
