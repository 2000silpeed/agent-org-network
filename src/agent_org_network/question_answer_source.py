"""P17-native completed-inline QuestionAnswerSource.

저장된 ``ReadyToDispatch`` snapshot을 현재 Registry·Route Authority에 다시 대조한
뒤 동기 AgentRuntime의 완성 ``Answer``만 Finalization 후보로 변환한다. pending을
답 문자열로 위장하는 legacy async adapter와 streaming delta 재생은 이 경계에서
허용하지 않는다.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Final, Literal, TypeAlias, final

from pydantic import ValidationError

from agent_org_network.agent_card import AgentCard, domain_authorized
from agent_org_network.approval import AnswerCandidate
from agent_org_network.conflict import Candidate, ConflictCase, Resolution
from agent_org_network.dispatch import DispatchingRuntime
from agent_org_network.grounding import assemble_grounding_knowledge_text
from agent_org_network.grounding_terminal_failure import GroundingTerminalFailureRequested
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
from agent_org_network.question_request import (
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    RouteTarget,
)
from agent_org_network.question_resolution import AuthorityGrant, RouteAuthority
from agent_org_network.question_stream_execution import BufferedAnswer
from agent_org_network.registry import Registry
from agent_org_network.runtime import AgentRuntime, Answer
from agent_org_network.user import User

_INVALID_REQUEST_MESSAGE: Final = "저장된 질문 실행 상태가 유효하지 않습니다."
_REGISTRY_MESSAGE: Final = "현재 책임 정보를 확인할 수 없습니다."
_AUTHORITY_MESSAGE: Final = "현재 실행 권한을 확인할 수 없습니다."
_RUNTIME_MESSAGE: Final = "답변 실행을 완료하지 못했습니다."
_INVALID_ANSWER_MESSAGE: Final = "완성된 답변 형식이 유효하지 않습니다."
_CONFLICT_EVIDENCE_MESSAGE: Final = "확정된 책임 근거를 확인할 수 없습니다."
_GROUNDING_MESSAGE: Final = "필수 답변 근거를 확인할 수 없습니다."

_RegistrySubjects: TypeAlias = tuple[tuple[str, str], ...]
_RegistrySnapshot: TypeAlias = tuple[tuple[AgentCard, User], ...]


class QuestionAnswerSourceError(RuntimeError):
    """내부 의존성 세부를 노출하지 않는 구조화 source 실패."""

    def __init__(
        self,
        *,
        request_id: str | None,
        code: str,
        retryable: bool,
        message: str,
    ) -> None:
        self.request_id = (
            request_id if isinstance(request_id, str) and bool(request_id.strip()) else None
        )
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class QuestionAnswerSourceConfigurationError(ValueError):
    """completed-inline source에 안전하지 않은 Runtime 조립을 거부함."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@final
class RegistryRuntimeQuestionAnswerSource:
    """현재 중앙 증거를 재검증하고 완성 ``Answer`` 한 token을 반환한다."""

    question_answer_source_capability: Literal["completed_inline_v1"] = "completed_inline_v1"

    def __init__(
        self,
        *,
        registry: Registry,
        route_authority: RouteAuthority,
        runtime: AgentRuntime,
        conflict_resolution_evidence_reader: ConflictResolutionEvidenceReader | None = None,
        grounding_knowledge_reader: GroundingKnowledgeReader | None = None,
    ) -> None:
        if isinstance(runtime, DispatchingRuntime) or _looks_like_runtime_dispatcher(runtime):
            raise QuestionAnswerSourceConfigurationError(
                code="legacy_async_runtime_not_supported",
                message=(
                    "completed-inline source는 pending을 Answer로 바꾸는 "
                    "legacy async Runtime을 허용하지 않습니다."
                ),
            )
        if not callable(getattr(runtime, "answer", None)):
            raise QuestionAnswerSourceConfigurationError(
                code="completed_inline_runtime_required",
                message="completed-inline AgentRuntime.answer가 필요합니다.",
            )
        self._registry = registry
        self._route_authority = route_authority
        self._runtime = runtime
        self._conflict_resolution_evidence_reader = conflict_resolution_evidence_reader
        self._grounding_knowledge_reader = grounding_knowledge_reader

    def matches_question_answer_dependencies(
        self,
        *,
        registry: Registry,
        route_authority: RouteAuthority,
    ) -> bool:
        """조립 경계가 실행 source의 Registry·Authority 동일성을 검증한다."""
        return self._registry is registry and self._route_authority is route_authority

    def matches_contested_question_answer_dependencies(
        self,
        *,
        registry: Registry,
        route_authority: RouteAuthority,
        conflict_resolution_evidence_reader: ConflictResolutionEvidenceReader,
        grounding_knowledge_reader: GroundingKnowledgeReader,
    ) -> bool:
        """P17.5 composition의 네 상태 원천이 정확히 같은 객체인지 확인한다."""
        return (
            self._registry is registry
            and self._route_authority is route_authority
            and self._conflict_resolution_evidence_reader is conflict_resolution_evidence_reader
            and self._grounding_knowledge_reader is grounding_knowledge_reader
        )

    def answer(
        self, request: QuestionRequest
    ) -> BufferedAnswer | GroundingTerminalFailureRequested:
        request_id = _safe_request_id(request)
        canonical = _canonical_ready_request(request, request_id=request_id)
        state = canonical.state
        # _canonical_ready_request가 exact state type을 보장한다.
        assert isinstance(state, ReadyToDispatch)
        route = state.route

        if canonical.initial_disposition == "contested":
            return self._answer_contested(canonical, route)

        card = self._load_current_card(
            request_id=canonical.request_id,
            agent_id=route.agent_id,
        )
        self._require_under_claim(
            request_id=canonical.request_id,
            intent=route.intent,
            card=card,
        )
        self._require_request_approval_snapshot(canonical, route, card)
        if route.authority_version is None:
            raise _source_error(
                canonical.request_id,
                "invalid_route_authority_evidence",
                retryable=False,
                message=_AUTHORITY_MESSAGE,
            )

        grant = self._current_authority_grant(canonical, route)
        if grant.policy_version != route.authority_version:
            raise _source_error(
                canonical.request_id,
                "route_authority_stale",
                retryable=False,
                message=_AUTHORITY_MESSAGE,
            )

        # Authority callback 동안 Registry가 교체되면 stale card로 실행하지 않는다.
        try:
            current_card = self._load_current_card(
                request_id=canonical.request_id,
                agent_id=route.agent_id,
            )
        except QuestionAnswerSourceError as error:
            raise _source_error(
                canonical.request_id,
                "route_registry_changed",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            ) from error
        if current_card != card:
            raise _source_error(
                canonical.request_id,
                "route_registry_changed",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            )
        self._require_under_claim(
            request_id=canonical.request_id,
            intent=route.intent,
            card=current_card,
        )
        self._require_request_approval_snapshot(canonical, route, current_card)

        try:
            raw_answer = self._runtime.answer(
                canonical.question,
                current_card,
                context=canonical.context_snapshot,
                grounding=None,
            )
        except Exception as error:
            raise _source_error(
                canonical.request_id,
                "answer_runtime_unavailable",
                retryable=True,
                message=_RUNTIME_MESSAGE,
            ) from error
        candidate = _canonical_answer_candidate(
            raw_answer,
            request_id=canonical.request_id,
        )
        return BufferedAnswer(candidate=candidate, tokens=(candidate.text,))

    def _answer_contested(
        self,
        request: QuestionRequest,
        route: RouteTarget,
    ) -> BufferedAnswer | GroundingTerminalFailureRequested:
        evidence_reader, grounding_reader = self._require_contested_dependencies(request.request_id)
        subjects = self._load_contested_resolution(
            request=request,
            route=route,
            reader=evidence_reader,
        )

        first_snapshot = self._load_contested_registry_snapshot(
            request=request,
            route=route,
            subjects=subjects,
        )
        if route.authority_version is None:
            raise _source_error(
                request.request_id,
                "invalid_route_authority_evidence",
                retryable=False,
                message=_AUTHORITY_MESSAGE,
            )
        grant = self._current_authority_grant(request, route)
        if grant.policy_version != route.authority_version:
            raise _source_error(
                request.request_id,
                "route_authority_stale",
                retryable=False,
                message=_AUTHORITY_MESSAGE,
            )

        second_snapshot = self._reread_exact_contested_registry_snapshot(
            request=request,
            route=route,
            subjects=subjects,
            expected=first_snapshot,
        )
        grounding_result = self._read_contested_grounding(
            request=request,
            subjects=subjects,
            reader=grounding_reader,
        )
        third_snapshot = self._reread_exact_contested_registry_snapshot(
            request=request,
            route=route,
            subjects=subjects,
            expected=second_snapshot,
        )
        if isinstance(grounding_result, GroundingTerminalFailureRequested):
            return grounding_result

        try:
            grounding_text = assemble_grounding_knowledge_text(grounding_result)
        except Exception as error:
            raise _source_error(
                request.request_id,
                "grounding_reader_protocol_invalid",
                retryable=False,
                message=_GROUNDING_MESSAGE,
            ) from error

        primary_card = third_snapshot[0][0]
        sources = tuple(
            dict.fromkeys(
                source for card, _owner in third_snapshot for source in card.knowledge_sources
            )
        )
        try:
            raw_answer = self._runtime.answer(
                request.question,
                primary_card,
                context=request.context_snapshot,
                grounding=grounding_text,
            )
        except Exception as error:
            raise _source_error(
                request.request_id,
                "answer_runtime_unavailable",
                retryable=True,
                message=_RUNTIME_MESSAGE,
            ) from error
        candidate = _canonical_answer_candidate(
            raw_answer,
            request_id=request.request_id,
            sources_override=sources,
        )
        return BufferedAnswer(candidate=candidate, tokens=(candidate.text,))

    def _require_contested_dependencies(
        self,
        request_id: str,
    ) -> tuple[ConflictResolutionEvidenceReader, GroundingKnowledgeReader]:
        evidence_reader = self._conflict_resolution_evidence_reader
        grounding_reader = self._grounding_knowledge_reader
        if (
            evidence_reader is None
            or not callable(getattr(evidence_reader, "resolution_evidence_for_request", None))
            or not callable(getattr(evidence_reader, "get_request_case", None))
            or grounding_reader is None
            or not callable(getattr(grounding_reader, "read", None))
        ):
            raise _source_error(
                request_id,
                "contested_grounding_dependencies_missing",
                retryable=False,
                message=_CONFLICT_EVIDENCE_MESSAGE,
            )
        return evidence_reader, grounding_reader

    def _load_contested_resolution(
        self,
        *,
        request: QuestionRequest,
        route: RouteTarget,
        reader: ConflictResolutionEvidenceReader,
    ) -> _RegistrySubjects:
        try:
            raw_evidence = reader.resolution_evidence_for_request(request.request_id)
        except Exception as error:
            raise _source_error(
                request.request_id,
                "conflict_resolution_evidence_unavailable",
                retryable=True,
                message=_CONFLICT_EVIDENCE_MESSAGE,
            ) from error
        evidence = self._canonical_resolution_evidence(
            raw_evidence,
            request_id=request.request_id,
        )
        try:
            raw_case = reader.get_request_case(evidence.case_id)
        except Exception as error:
            raise _source_error(
                request.request_id,
                "conflict_resolution_evidence_unavailable",
                retryable=True,
                message=_CONFLICT_EVIDENCE_MESSAGE,
            ) from error
        case = self._canonical_conflict_case(raw_case, request_id=request.request_id)
        try:
            subjects = self._validate_contested_resolution_links(
                request=request,
                route=route,
                evidence=evidence,
                case=case,
            )
        except QuestionAnswerSourceError:
            raise
        except Exception as error:
            raise _source_error(
                request.request_id,
                "invalid_conflict_resolution_evidence",
                retryable=False,
                message=_CONFLICT_EVIDENCE_MESSAGE,
            ) from error
        return subjects

    @staticmethod
    def _canonical_resolution_evidence(
        raw_evidence: object,
        *,
        request_id: str,
    ) -> ConflictResolutionEvidence:
        try:
            if type(raw_evidence) is not ConflictResolutionEvidence:
                raise TypeError("ConflictResolutionEvidence exact type이 필요합니다.")
            if (
                type(raw_evidence.route) is not RouteTarget
                or type(raw_evidence.supporting) is not tuple
                or any(
                    type(support) is not SupportingKnowledgeEvidence
                    or type(support.authority) is not int
                    or support.authority != 0
                    for support in raw_evidence.supporting
                )
            ):
                raise TypeError("ConflictResolutionEvidence nested exact type이 필요합니다.")
            if type(raw_evidence.source) is FromDirectConsensus:
                if type(raw_evidence.source.votes) is not tuple or any(
                    type(vote) is not OwnerConcurrenceEvidence for vote in raw_evidence.source.votes
                ):
                    raise TypeError("direct consensus vote exact type이 필요합니다.")
            elif type(raw_evidence.source) is not FromManagerMediation:
                raise TypeError("ConflictResolutionSource exact type이 필요합니다.")
            return ConflictResolutionEvidence.model_validate(
                raw_evidence.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise _source_error(
                request_id,
                "invalid_conflict_resolution_evidence",
                retryable=False,
                message=_CONFLICT_EVIDENCE_MESSAGE,
            ) from error

    @staticmethod
    def _canonical_conflict_case(
        raw_case: object,
        *,
        request_id: str,
    ) -> ConflictCase:
        try:
            if type(raw_case) is not ConflictCase:
                raise TypeError("ConflictCase exact type이 필요합니다.")
            if (
                type(raw_case.intent) is not str
                or type(raw_case.question) is not str
                or type(raw_case.opened_at) is not datetime
                or type(raw_case.case_id) is not str
                or type(raw_case.status) is not str
                or (raw_case.request_id is not None and type(raw_case.request_id) is not str)
                or type(raw_case.concurrence_round) is not int
                or (
                    raw_case.manager_item_id is not None
                    and type(raw_case.manager_item_id) is not str
                )
                or (
                    raw_case.decline_reason is not None and type(raw_case.decline_reason) is not str
                )
            ):
                raise TypeError("ConflictCase scalar exact type이 필요합니다.")
            if (
                type(raw_case.candidates) is not tuple
                or len(raw_case.candidates) < 2
                or any(
                    type(candidate) is not Candidate
                    or type(candidate.agent_id) is not str
                    or not candidate.agent_id.strip()
                    or type(candidate.owner) is not str
                    or not candidate.owner.strip()
                    for candidate in raw_case.candidates
                )
            ):
                raise ValueError("ConflictCase candidate 원형이 유효하지 않습니다.")
            candidate_ids = tuple(candidate.agent_id for candidate in raw_case.candidates)
            if len(candidate_ids) != len(set(candidate_ids)):
                raise ValueError("ConflictCase candidate agent_id가 중복됩니다.")
            candidates = tuple(
                Candidate(
                    agent_id=deepcopy(candidate.agent_id),
                    owner=deepcopy(candidate.owner),
                )
                for candidate in raw_case.candidates
            )
            if raw_case.resolution is None:
                resolution = None
            else:
                if (
                    type(raw_case.resolution) is not Resolution
                    or type(raw_case.resolution.intent) is not str
                    or type(raw_case.resolution.primary) is not str
                    or type(raw_case.resolution.rationale) is not str
                ):
                    raise TypeError("ConflictCase Resolution exact type이 필요합니다.")
                resolution = Resolution(
                    intent=deepcopy(raw_case.resolution.intent),
                    primary=deepcopy(raw_case.resolution.primary),
                    rationale=deepcopy(raw_case.resolution.rationale),
                )
            return ConflictCase(
                intent=deepcopy(raw_case.intent),
                question=deepcopy(raw_case.question),
                candidates=candidates,
                opened_at=deepcopy(raw_case.opened_at),
                case_id=deepcopy(raw_case.case_id),
                status=raw_case.status,
                resolution=resolution,
                request_id=deepcopy(raw_case.request_id),
                concurrence_round=raw_case.concurrence_round,
                manager_item_id=deepcopy(raw_case.manager_item_id),
                decline_reason=raw_case.decline_reason,
            )
        except Exception as error:
            raise _source_error(
                request_id,
                "invalid_conflict_resolution_evidence",
                retryable=False,
                message=_CONFLICT_EVIDENCE_MESSAGE,
            ) from error

    @staticmethod
    def _validate_contested_resolution_links(
        *,
        request: QuestionRequest,
        route: RouteTarget,
        evidence: ConflictResolutionEvidence,
        case: ConflictCase,
    ) -> _RegistrySubjects:
        resolution = case.resolution
        if (
            request.initial_disposition != "contested"
            or request.intent is None
            or evidence.request_id != request.request_id
            or evidence.org_id != request.org_id
            or evidence.intent != request.intent
            or evidence.route != route
            or case.request_id != request.request_id
            or case.case_id != evidence.case_id
            or case.intent != request.intent
            or case.question != request.question
            or case.status != "resolved"
            or type(resolution) is not Resolution
            or resolution.intent != request.intent
            or resolution.primary != route.agent_id
        ):
            raise ValueError("Request, evidence, Case Resolution 링크가 다릅니다.")

        candidates = case.candidates
        candidate_owner = {candidate.agent_id: candidate.owner for candidate in candidates}
        primary_owner = candidate_owner.get(route.agent_id)
        if primary_owner is None:
            raise ValueError("Resolution primary가 원 후보가 아닙니다.")

        source = evidence.source
        if type(source) is FromDirectConsensus:
            if case.manager_item_id is not None:
                raise ValueError("direct consensus Case에 Manager item이 있습니다.")
            RegistryRuntimeQuestionAnswerSource._validate_direct_resolution(
                case=case,
                evidence=evidence,
                source=source,
            )
        elif type(source) is FromManagerMediation:
            if (
                case.manager_item_id is None
                or source.item_id != case.manager_item_id
                or evidence.supporting != ()
            ):
                raise ValueError("Manager mediation evidence가 Case와 다릅니다.")
        else:
            raise TypeError("Conflict resolution source exact type이 필요합니다.")

        return (
            (route.agent_id, primary_owner),
            *((support.agent_id, support.affirmed_by_owner) for support in evidence.supporting),
        )

    @staticmethod
    def _validate_direct_resolution(
        *,
        case: ConflictCase,
        evidence: ConflictResolutionEvidence,
        source: FromDirectConsensus,
    ) -> None:
        resolution = case.resolution
        assert type(resolution) is Resolution
        owner_order = tuple(dict.fromkeys(candidate.owner for candidate in case.candidates))
        votes = source.votes
        if (
            source.round != case.concurrence_round
            or len(votes) != len(owner_order)
            or any(type(vote) is not OwnerConcurrenceEvidence for vote in votes)
            or tuple(vote.owner_id for vote in votes) != owner_order
            or any(
                vote.round != source.round or vote.on_agent != resolution.primary for vote in votes
            )
        ):
            raise ValueError("direct consensus vote가 Case와 다릅니다.")
        rationale = "; ".join(f"{vote.owner_id}→{vote.on_agent}" for vote in votes)
        if resolution.rationale != rationale:
            raise ValueError("direct consensus rationale가 canonical votes와 다릅니다.")
        vote_by_owner = {vote.owner_id: vote for vote in votes}
        expected_supporting = tuple(
            SupportingKnowledgeEvidence(
                agent_id=candidate.agent_id,
                affirmed_by_owner=candidate.owner,
            )
            for candidate in case.candidates
            if candidate.agent_id != resolution.primary
            and vote_by_owner[candidate.owner].stance == "keep_as_complement"
        )
        if evidence.supporting != expected_supporting or any(
            type(support) is not SupportingKnowledgeEvidence or support.authority != 0
            for support in evidence.supporting
        ):
            raise ValueError("supporting evidence가 positive stance와 다릅니다.")

    def _load_contested_registry_snapshot(
        self,
        *,
        request: QuestionRequest,
        route: RouteTarget,
        subjects: _RegistrySubjects,
    ) -> _RegistrySnapshot:
        snapshot: list[tuple[AgentCard, User]] = []
        try:
            with self._registry.consistency_guard():
                for agent_id, expected_owner in subjects:
                    card, owner = self._load_current_card_and_owner(
                        request_id=request.request_id,
                        agent_id=agent_id,
                    )
                    if card.owner != expected_owner or owner.id != expected_owner:
                        raise _source_error(
                            request.request_id,
                            "route_registry_changed",
                            retryable=False,
                            message=_REGISTRY_MESSAGE,
                        )
                    self._require_under_claim(
                        request_id=request.request_id,
                        intent=route.intent,
                        card=card,
                    )
                    snapshot.append((card, owner))
        except QuestionAnswerSourceError:
            raise
        except Exception as error:
            raise _source_error(
                request.request_id,
                "registry_unavailable",
                retryable=True,
                message=_REGISTRY_MESSAGE,
            ) from error
        if not snapshot or snapshot[0][0].agent_id != route.agent_id:
            raise _source_error(
                request.request_id,
                "invalid_conflict_resolution_evidence",
                retryable=False,
                message=_CONFLICT_EVIDENCE_MESSAGE,
            )
        self._require_request_approval_snapshot(request, route, snapshot[0][0])
        return tuple(snapshot)

    def _reread_exact_contested_registry_snapshot(
        self,
        *,
        request: QuestionRequest,
        route: RouteTarget,
        subjects: _RegistrySubjects,
        expected: _RegistrySnapshot,
    ) -> _RegistrySnapshot:
        try:
            current = self._load_contested_registry_snapshot(
                request=request,
                route=route,
                subjects=subjects,
            )
        except QuestionAnswerSourceError as error:
            raise _source_error(
                request.request_id,
                "route_registry_changed",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            ) from error
        if current != expected:
            raise _source_error(
                request.request_id,
                "route_registry_changed",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            )
        return current

    @staticmethod
    def _read_contested_grounding(
        *,
        request: QuestionRequest,
        subjects: _RegistrySubjects,
        reader: GroundingKnowledgeReader,
    ) -> tuple[GroundingKnowledgeFound, ...] | GroundingTerminalFailureRequested:
        found: list[GroundingKnowledgeFound] = []
        for agent_id, _owner_id in subjects:
            try:
                raw = reader.read(agent_id)
            except Exception as error:
                raise _source_error(
                    request.request_id,
                    "grounding_read_interrupted",
                    retryable=True,
                    message=_GROUNDING_MESSAGE,
                ) from error
            if type(raw) is GroundingKnowledgeFound:
                found.append(
                    RegistryRuntimeQuestionAnswerSource._canonical_grounding_found(
                        raw,
                        requested_agent_id=agent_id,
                        request_id=request.request_id,
                    )
                )
                continue
            if type(raw) is GroundingKnowledgeMissing:
                RegistryRuntimeQuestionAnswerSource._require_grounding_result_agent_id(
                    raw,
                    requested_agent_id=agent_id,
                    request_id=request.request_id,
                )
                return GroundingTerminalFailureRequested(
                    request_id=request.request_id,
                    expected_revision=request.revision,
                    error_code="required_grounding_missing",
                )
            if type(raw) is GroundingKnowledgeInvalid:
                RegistryRuntimeQuestionAnswerSource._require_grounding_result_agent_id(
                    raw,
                    requested_agent_id=agent_id,
                    request_id=request.request_id,
                )
                try:
                    GroundingKnowledgeInvalid.model_validate(
                        raw.model_dump(mode="python", round_trip=True),
                        strict=True,
                    )
                except Exception as error:
                    raise _source_error(
                        request.request_id,
                        "grounding_reader_protocol_invalid",
                        retryable=False,
                        message=_GROUNDING_MESSAGE,
                    ) from error
                return GroundingTerminalFailureRequested(
                    request_id=request.request_id,
                    expected_revision=request.revision,
                    error_code="required_grounding_invalid",
                )
            raise _source_error(
                request.request_id,
                "grounding_reader_protocol_invalid",
                retryable=False,
                message=_GROUNDING_MESSAGE,
            )
        return tuple(found)

    @staticmethod
    def _canonical_grounding_found(
        raw: GroundingKnowledgeFound,
        *,
        requested_agent_id: str,
        request_id: str,
    ) -> GroundingKnowledgeFound:
        try:
            if (
                raw.agent_id != requested_agent_id
                or type(raw.content) is not KnowledgeBundleContent
                or raw.content.agent_id != requested_agent_id
                or type(raw.content.documents) is not tuple
                or not raw.content.documents
            ):
                raise ValueError("Grounding Knowledge identity가 다릅니다.")
            documents: list[KnowledgeDoc] = []
            seen_paths: set[str] = set()
            for document in raw.content.documents:
                if (
                    type(document) is not KnowledgeDoc
                    or type(document.path) is not str
                    or not document.path.strip()
                    or type(document.body) is not str
                    or not document.body.strip()
                    or document.path in seen_paths
                ):
                    raise ValueError("Grounding Knowledge document가 유효하지 않습니다.")
                seen_paths.add(document.path)
                documents.append(
                    KnowledgeDoc.model_validate(
                        {"path": document.path, "body": document.body},
                        strict=True,
                    )
                )
            content = KnowledgeBundleContent.model_validate(
                {
                    "agent_id": raw.content.agent_id,
                    "documents": tuple(documents),
                    "version": deepcopy(raw.content.version),
                    "synced_at": deepcopy(raw.content.synced_at),
                },
                strict=True,
            )
            return GroundingKnowledgeFound.model_validate(
                {
                    "agent_id": requested_agent_id,
                    "content": content,
                },
                strict=True,
            )
        except Exception as error:
            raise _source_error(
                request_id,
                "grounding_reader_protocol_invalid",
                retryable=False,
                message=_GROUNDING_MESSAGE,
            ) from error

    @staticmethod
    def _require_grounding_result_agent_id(
        raw: GroundingKnowledgeMissing | GroundingKnowledgeInvalid,
        *,
        requested_agent_id: str,
        request_id: str,
    ) -> None:
        try:
            if raw.agent_id != requested_agent_id:
                raise ValueError("Grounding Knowledge result agent_id가 다릅니다.")
            model = (
                GroundingKnowledgeMissing
                if type(raw) is GroundingKnowledgeMissing
                else GroundingKnowledgeInvalid
            )
            model.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise _source_error(
                request_id,
                "grounding_reader_protocol_invalid",
                retryable=False,
                message=_GROUNDING_MESSAGE,
            ) from error

    def _load_current_card(self, *, request_id: str, agent_id: str) -> AgentCard:
        card, _owner = self._load_current_card_and_owner(
            request_id=request_id,
            agent_id=agent_id,
        )
        return card

    def _load_current_card_and_owner(
        self,
        *,
        request_id: str,
        agent_id: str,
    ) -> tuple[AgentCard, User]:
        try:
            raw_card = self._registry.get(agent_id)
        except KeyError as error:
            raise _source_error(
                request_id,
                "route_card_not_found",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            ) from error
        except Exception as error:
            raise _source_error(
                request_id,
                "registry_unavailable",
                retryable=True,
                message=_REGISTRY_MESSAGE,
            ) from error

        try:
            if type(raw_card) is not AgentCard:
                raise TypeError("Registry card type mismatch")
            card = AgentCard.model_validate(
                {
                    "agent_id": raw_card.agent_id,
                    "owner": raw_card.owner,
                    "maintainer": raw_card.maintainer,
                    "team": raw_card.team,
                    "summary": raw_card.summary,
                    "domains": list(raw_card.domains),
                    "can_answer": list(raw_card.can_answer),
                    "cannot_answer": list(raw_card.cannot_answer),
                    "approval_when": list(raw_card.approval_when),
                    "collaborate_when": list(raw_card.collaborate_when),
                    "knowledge_sources": list(raw_card.knowledge_sources),
                    "trust_labels": list(raw_card.trust_labels),
                    "last_reviewed_at": raw_card.last_reviewed_at,
                },
                strict=True,
            )
            if card.agent_id != agent_id or not card.owner.strip():
                raise ValueError("card identity mismatch")
            if any(
                not value.strip()
                for value in (
                    *card.domains,
                    *card.cannot_answer,
                    *card.knowledge_sources,
                )
            ):
                raise ValueError("card responsibility or knowledge source contains blank value")
        except (AttributeError, TypeError, ValueError, ValidationError) as error:
            raise _source_error(
                request_id,
                "invalid_registry_card",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            ) from error

        try:
            raw_owner = self._registry.get_user(card.owner)
        except KeyError as error:
            raise _source_error(
                request_id,
                "route_owner_not_found",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            ) from error
        except Exception as error:
            raise _source_error(
                request_id,
                "registry_unavailable",
                retryable=True,
                message=_REGISTRY_MESSAGE,
            ) from error
        try:
            if type(raw_owner) is not User:
                raise TypeError("Registry owner type mismatch")
            owner = User.model_validate(
                {
                    "id": raw_owner.id,
                    "manager": raw_owner.manager,
                    "email": raw_owner.email,
                },
                strict=True,
            )
            if owner.id != card.owner or not owner.id.strip():
                raise ValueError("owner identity mismatch")
        except (AttributeError, TypeError, ValueError, ValidationError) as error:
            raise _source_error(
                request_id,
                "invalid_registry_owner",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            ) from error
        return card, owner

    @staticmethod
    def _require_under_claim(
        *,
        request_id: str,
        intent: str,
        card: AgentCard,
    ) -> None:
        if not domain_authorized(intent, card):
            raise _source_error(
                request_id,
                "route_under_claim_mismatch",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            )

    @staticmethod
    def _require_request_approval_snapshot(
        request: QuestionRequest,
        route: RouteTarget,
        card: AgentCard,
    ) -> None:
        if request.initial_disposition not in ("unowned", "contested"):
            return
        if route.requires_approval != (route.intent in card.approval_when):
            raise _source_error(
                request.request_id,
                "route_approval_snapshot_stale",
                retryable=False,
                message=_REGISTRY_MESSAGE,
            )

    def _current_authority_grant(
        self,
        request: QuestionRequest,
        route: RouteTarget,
    ) -> AuthorityGrant:
        if request.initial_disposition in ("unowned", "contested"):
            request_reader = getattr(self._route_authority, "authorize_for_request", None)
            if not callable(request_reader):
                raise _source_error(
                    request.request_id,
                    "request_route_authority_unsupported",
                    retryable=False,
                    message=_AUTHORITY_MESSAGE,
                )
            try:
                raw_grant = request_reader(
                    request.org_id,
                    request.request_id,
                    route.intent,
                    route.agent_id,
                )
            except Exception as error:
                raise _source_error(
                    request.request_id,
                    "route_authority_unavailable",
                    retryable=True,
                    message=_AUTHORITY_MESSAGE,
                ) from error
            return self._canonical_authority_grant(raw_grant, request.request_id)
        try:
            raw_grant = self._route_authority.authorize(
                request.org_id,
                route.intent,
                route.agent_id,
            )
        except Exception as error:
            raise _source_error(
                request.request_id,
                "route_authority_unavailable",
                retryable=True,
                message=_AUTHORITY_MESSAGE,
            ) from error
        return self._canonical_authority_grant(raw_grant, request.request_id)

    @staticmethod
    def _canonical_authority_grant(
        raw_grant: object,
        request_id: str,
    ) -> AuthorityGrant:
        if raw_grant is None:
            raise _source_error(
                request_id,
                "route_authority_denied",
                retryable=False,
                message=_AUTHORITY_MESSAGE,
            )
        try:
            if type(raw_grant) is not AuthorityGrant:
                raise TypeError("AuthorityGrant type mismatch")
            return AuthorityGrant.model_validate(
                {"policy_version": raw_grant.policy_version},
                strict=True,
            )
        except (AttributeError, TypeError, ValueError, ValidationError) as error:
            raise _source_error(
                request_id,
                "invalid_route_authority_grant",
                retryable=False,
                message=_AUTHORITY_MESSAGE,
            ) from error


def _looks_like_runtime_dispatcher(runtime: object) -> bool:
    return all(
        callable(getattr(runtime, method, None))
        for method in ("dispatch", "poll", "claim", "submit")
    )


def _safe_request_id(request: object) -> str | None:
    try:
        request_id: object = getattr(request, "request_id")
    except Exception:
        return None
    return request_id if isinstance(request_id, str) and request_id.strip() else None


def _canonical_ready_request(
    request: QuestionRequest,
    *,
    request_id: str | None,
) -> QuestionRequest:
    if type(request) is not QuestionRequest:
        raise _source_error(
            request_id,
            "invalid_question_request",
            retryable=False,
            message=_INVALID_REQUEST_MESSAGE,
        )
    raw_state = request.state
    if type(raw_state) is not ReadyToDispatch:
        raise _source_error(
            request_id,
            "invalid_request_state",
            retryable=False,
            message=_INVALID_REQUEST_MESSAGE,
        )
    try:
        route = RouteTarget.model_validate(
            {
                "intent": raw_state.route.intent,
                "agent_id": raw_state.route.agent_id,
                "requires_approval": raw_state.route.requires_approval,
                "authority_version": raw_state.route.authority_version,
            },
            strict=True,
        )
        handling = HandlingAssignment.model_validate(
            {
                "kind": raw_state.handling.kind,
                "ref": raw_state.handling.ref,
                "due_at": raw_state.handling.due_at,
            },
            strict=True,
        )
        state = ReadyToDispatch.model_validate(
            {
                "kind": raw_state.kind,
                "route": route,
                "attempt": raw_state.attempt,
                "trigger_key": raw_state.trigger_key,
                "handling": handling,
            },
            strict=True,
        )
        return QuestionRequest.model_validate(
            {
                "request_id": request.request_id,
                "org_id": request.org_id,
                "requester_id": request.requester_id,
                "session_id": request.session_id,
                "question": request.question,
                "context_snapshot": request.context_snapshot,
                "intent": request.intent,
                "initial_disposition": request.initial_disposition,
                "state": state,
                "revision": request.revision,
                "created_at": request.created_at,
                "updated_at": request.updated_at,
            },
            strict=True,
        )
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise _source_error(
            request_id,
            "invalid_question_request",
            retryable=False,
            message=_INVALID_REQUEST_MESSAGE,
        ) from error


def _canonical_answer_candidate(
    raw_answer: object,
    *,
    request_id: str,
    sources_override: tuple[str, ...] | None = None,
) -> AnswerCandidate:
    try:
        if type(raw_answer) is not Answer:
            raise TypeError("Runtime Answer type mismatch")
        return AnswerCandidate.model_validate(
            {
                "text": raw_answer.text,
                "sources": (raw_answer.sources if sources_override is None else sources_override),
                "mode": raw_answer.mode,
                "snapshot_sha": raw_answer.snapshot_sha,
            },
            strict=True,
        )
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise _source_error(
            request_id,
            "invalid_runtime_answer",
            retryable=False,
            message=_INVALID_ANSWER_MESSAGE,
        ) from error


def _source_error(
    request_id: str | None,
    code: str,
    *,
    retryable: bool,
    message: str,
) -> QuestionAnswerSourceError:
    return QuestionAnswerSourceError(
        request_id=request_id,
        code=code,
        retryable=retryable,
        message=message,
    )
