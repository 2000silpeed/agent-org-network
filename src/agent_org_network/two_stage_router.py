"""TwoStageRouter — published 지식 인덱스 기반 2단 라우팅 (ADR 0028 §6·§13).

stage-1: matcher.match(question, store.all_indexes()) → 후보 IndexMatch들
         → authorized() admission 재검증 → 0/1/≥2 분기 → RoutingDecision 투영
stage-2(T10.3b, 이번 범위 밖): ≥2 모호 후보 → assessor 자동해소 자리(옵셔널 주입)

기존 Router와 *공존*(수정 아님·ADR 0028 §13 결정 A). 와이어 지점(AskOrg/SessionAskOrg)이
둘 중 하나를 주입받는다.

불변식:
  - 미아 없음: 권한통과 0 → Unowned(root escalation) · 1 → Routed · ≥2 → Contested.
  - Authority 중앙: authorized() = card.domains 기반 — 인덱스는 신호(제안)만.
  - 중앙 토큰 0: matcher는 결정론(LLM/외부 API 0).
  - 기존 Router 무회귀: Router 코드 무수정(attach_gates 추출은 동작 보존 리팩터).
  - 노출 불변식: score·matched_concept_id는 RoutingDecision에 안 실림.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from agent_org_network.agent_card import AgentCard
from agent_org_network.conflict import PrecedentStore
from agent_org_network.decision import Contested, RoutingDecision, Routed, Unowned
from agent_org_network.index_matcher import IndexMatch, KnowledgeIndexMatcher
from agent_org_network.knowledge_index import KnowledgeIndex
from agent_org_network.registry import Registry
from agent_org_network.router import attach_gates


# ── PublishedIndexStore 포트 ─────────────────────────────────────────────────


class PublishedIndexStore(Protocol):
    """에이전트별 최신 KnowledgeIndex를 보관하는 포트(ADR 0028 §13 결정 C).

    T10.3은 all_indexes()만 read-only로 사용한다.
    put()·get()은 T10.4 책임(실 수용·staleness 대조·권한 검증).
    """

    def all_indexes(self) -> Sequence[KnowledgeIndex]:
        """에이전트별 최신 인덱스 합집합(stage-1 매처 입력)."""
        ...

    def get(self, agent_id: str) -> KnowledgeIndex | None:
        """단건 조회(운영면·옵션 — T10.4)."""
        ...

    def put(self, index: KnowledgeIndex) -> None:
        """최신 수용(version/generated_at staleness 대조 — T10.4)."""
        ...


class InMemoryPublishedIndexStore:
    """PublishedIndexStore 프로토콜의 in-memory 구현(테스트·데모용).

    T10.4에서 staleness 대조·권한 검증이 추가된다 — 지금은 단순 보관.
    """

    def __init__(self, indexes: Sequence[KnowledgeIndex] = ()) -> None:
        self._store: dict[str, KnowledgeIndex] = {idx.agent_id: idx for idx in indexes}

    def all_indexes(self) -> Sequence[KnowledgeIndex]:
        return list(self._store.values())

    def get(self, agent_id: str) -> KnowledgeIndex | None:
        return self._store.get(agent_id)

    def put(self, index: KnowledgeIndex) -> None:
        self._store[index.agent_id] = index


# ── TwoStageRouter ───────────────────────────────────────────────────────────


def _find_concept_by_id(index: KnowledgeIndex, concept_id: str) -> str:
    """index에서 concept_id에 해당하는 concept.domain을 반환한다.

    없으면 빈 문자열(안전 폴백 — 권한 검증에서 탈락하게 됨).
    """
    for concept in index.concepts:
        if concept.id == concept_id:
            return concept.domain
    return ""


class TwoStageRouter:
    """published 지식 인덱스 기반 2단 라우터 (ADR 0028 §13 결정 A~E).

    기존 Router와 *공존*(Router 코드 무수정). RoutingDecision sealed sum·Precedent
    단축경로·attach_gates를 그대로 재사용한다.

    생성자 파라미터:
      registry:    카드 admission·권한 검증 원천(card.domains·cannot_answer).
      matcher:     stage-1 KnowledgeIndexMatcher(FakeMatcher or ConceptOverlapMatcher).
      store:       PublishedIndexStore — all_indexes()로 stage-1 입력.
      root_user:   Unowned 시 escalated_to(미아 없음 보장).
      precedents:  PrecedentStore(옵셔널 — 주입 시 stage-1 직후 판례 단축경로).
      assessor:    ConfidenceAssessor(옵셔널 — T10.3b stage-2 자동해소 자리·이번 미사용).
      clear_winner_margin: stage-2 자동해소 임계(옵셔널·T10.3b 자리).
    """

    def __init__(
        self,
        registry: Registry,
        matcher: KnowledgeIndexMatcher,
        store: PublishedIndexStore,
        root_user: str,
        precedents: PrecedentStore | None = None,
        assessor: object | None = None,
        clear_winner_margin: float | None = None,
    ) -> None:
        self._registry = registry
        self._matcher = matcher
        self._store = store
        self._root_user = root_user
        self._precedents = precedents
        # T10.3b 자리만(이번 미사용)
        self._assessor = assessor
        self._clear_winner_margin = clear_winner_margin

    def route(self, question: str) -> RoutingDecision:
        """2단 라우팅 — stage-1 인덱스 매칭 + 권한 재검증 + RoutingDecision 투영.

        순서:
        1. stage-1: matcher.match(question, store.all_indexes()) → 후보 IndexMatch들.
        2. 권한 재검증: 각 match에 대해 concept.domain in card.domains and
           concept.domain not in card.cannot_answer인 후보만 남긴다(over-claim 차단).
           미등록 agent_id는 제외(등록 무결성).
        3. 대표 intent 결정: 처분 후보의 concept.domain(결정 E·ADR 0015).
        4. Precedent 단축경로(stage-1 직후·대표 intent 정해지면 lookup).
        5. 투영: 0→Unowned(root)·1→Routed(+attach_gates)·≥2→Contested.
        """
        # ── stage-1: 인덱스 매칭 ──────────────────────────────────────────────
        raw_matches = self._matcher.match(question, self._store.all_indexes())

        # ── 권한 재검증(admission 재검증, ADR 0028 §5·결정 B) ────────────────
        # 각 match: concept.domain ∈ card.domains AND ∉ card.cannot_answer
        # 미등록 agent_id는 KeyError → 제외.
        # all_indexes()→get() 불일치(실 store·동시 put) 시 후보 보수적 탈락 → 미아 없음 보존(권한 넓히는 위험 없음).
        authorized: list[tuple[IndexMatch, str]] = []
        for match in raw_matches:
            try:
                card = self._registry.get(match.agent_id)
            except KeyError:
                continue
            # matched_concept_id → concept → domain
            idx = self._store.get(match.agent_id)
            if idx is None:
                continue
            domain = _find_concept_by_id(idx, match.matched_concept_id)
            if not domain:
                continue
            if domain in card.domains and domain not in card.cannot_answer:
                authorized.append((match, domain))

        # ── 0 후보 → Unowned(미아 없음) ───────────────────────────────────────
        if not authorized:
            return Unowned(
                escalated_to=self._root_user,
                reason="담당 없음: 인덱스 매칭 후보 0건 또는 전부 권한 밖",
                intent="",
            )

        # ── 대표 intent = 최고 점수 후보의 concept.domain(결정 E) ─────────────
        # authorized는 raw_matches와 같은 score 내림차순 정렬에서 필터된 것.
        # 첫 번째(최고 score)의 domain을 대표 intent로.
        representative_intent = authorized[0][1]

        # ── Precedent 단축경로(stage-1 직후·대표 intent 정해지면 lookup) ──────
        # 매처 계약(index_matcher score 내림차순)에 의존 — authorized 필터가 순서 보존.
        if self._precedents is not None and representative_intent:
            p = self._precedents.lookup(representative_intent)
            if p is not None and not p.invalidated:
                try:
                    card = self._registry.get(p.resolution.primary)
                except KeyError:
                    pass
                else:
                    # 권한 재검증: precedent primary 카드도 card.domains 게이트 통과 필수.
                    # 미통과(over-claim) 또는 cannot_answer 등록 → 단축 건너뜀 → stage-1
                    # authorized 투영으로 폴백(미아 없음 보존 — authorized는 이미 1+).
                    primary_authorized = (
                        representative_intent in card.domains
                        and representative_intent not in card.cannot_answer
                    )
                    if primary_authorized:
                        return attach_gates(
                            Routed(
                                primary=card,
                                reason=f"판례 적용: intent '{representative_intent}' → {p.resolution.primary}",
                                intent=representative_intent,
                            ),
                            representative_intent,
                            self._registry,
                        )

        # ── 1 후보 → Routed ───────────────────────────────────────────────────
        if len(authorized) == 1:
            match, domain = authorized[0]
            try:
                card = self._registry.get(match.agent_id)
            except KeyError:
                return Unowned(
                    escalated_to=self._root_user,
                    reason="담당 없음: 권한 통과 후보 카드 조회 실패",
                    intent=domain,
                )
            return attach_gates(
                Routed(
                    primary=card,
                    reason=f"인덱스 매칭: intent '{domain}' → {match.agent_id}",
                    intent=domain,
                ),
                domain,
                self._registry,
            )

        # ── ≥2 후보 → Contested(T10.3a — stage-2 자동해소는 T10.3b) ─────────
        candidate_cards: list[AgentCard] = []
        for match, _domain in authorized:
            try:
                card = self._registry.get(match.agent_id)
                candidate_cards.append(card)
            except KeyError:
                continue

        return Contested(
            candidates=tuple(candidate_cards),
            reason=f"후보 {len(candidate_cards)}건, Authority 미정(stage-2 미통합)",
            intent=representative_intent,
        )
