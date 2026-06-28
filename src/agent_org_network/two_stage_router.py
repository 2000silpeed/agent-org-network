"""TwoStageRouter — published 지식 인덱스 기반 2단 라우팅 (ADR 0028 §6·§13).

stage-1: matcher.match(question, store.all_indexes()) → 후보 IndexMatch들
         → authorized() admission 재검증 → 0/1/≥2 분기 → RoutingDecision 투영
stage-2(T10.3b): ≥2 모호 후보 → assessor.assess() → clear winner? → Routed/Contested

기존 Router와 *공존*(수정 아님·ADR 0028 §13 결정 A). 와이어 지점(AskOrg/SessionAskOrg)이
둘 중 하나를 주입받는다.

불변식:
  - 미아 없음: 권한통과 0 → Unowned(root escalation) · 1 → Routed · ≥2 → Contested.
  - Authority 중앙: authorized() = card.domains 기반 — 인덱스는 신호(제안)만.
  - 중앙 토큰 0: matcher는 결정론(LLM/외부 API 0) · assessor는 owner측(FakeAssessor 주입).
  - 기존 Router 무회귀: Router 코드 무수정(attach_gates 추출은 동작 보존 리팩터).
  - 노출 불변식: score·matched_concept_id·confidence·grounding은 RoutingDecision에 안 실림.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from agent_org_network.agent_card import AgentCard, domain_authorized
from agent_org_network.conflict import PrecedentStore
from agent_org_network.decision import Contested, RoutingDecision, Routed, Unowned
from agent_org_network.index_matcher import IndexMatch, KnowledgeIndexMatcher
from agent_org_network.knowledge_index import Concept, KnowledgeIndex
from agent_org_network.registry import Registry
from agent_org_network.router import attach_gates


# ── stage-2 값 객체 및 포트 ──────────────────────────────────────────────────


class GroundedConfidence(BaseModel, frozen=True):
    """owner RAG 검색 점수 등으로 접지된 신뢰도 값 객체(ADR 0028 §13 결정 D).

    confidence: owner RAG로 접지(자유 자기주장 아님).
    grounding: 근거 메모(조직 내부값 — 노출 불변식상 사용자 미노출).
    """

    agent_id: str
    confidence: float
    grounding: str = ""


class ConfidenceAssessor(Protocol):
    """owner측 신뢰도 자기평가 포트(ADR 0028 §13 결정 D·owner측 — AgentRuntime 정신).

    stage-2에서 각 권한통과 후보 카드에 대해 호출된다.
    실 구현은 T10.5(게이트 밖·owner 환경 RAG) — 게이트 내는 FakeAssessor 주입.
    """

    def assess(self, question: str, card: AgentCard) -> GroundedConfidence:
        """question에 대해 card(owner 에이전트)의 접지된 신뢰도를 반환한다."""
        ...


class FakeAssessor:
    """ConfidenceAssessor 테스트 더블 — 생성 시 agent_id→confidence 고정 주입.

    결정론 경계: FakeClassifier·StubRuntime 정신과 동일.
    없는 agent_id는 confidence=0.0 반환.
    """

    def __init__(self, confidences: dict[str, float]) -> None:
        self._confidences = confidences

    def assess(self, question: str, card: AgentCard) -> GroundedConfidence:
        confidence = self._confidences.get(card.agent_id, 0.0)
        return GroundedConfidence(agent_id=card.agent_id, confidence=confidence)


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

    `put`은 `generated_at`(datetime) 기준 *더 새 것만 수용*한다(ADR 0028 §14 결정 C) —
    첫 인덱스 무조건 수용·더 새면 교체·동률/역행은 거부(no-op·멱등). per-agent 격리
    (`_store` 키가 agent_id). `version: str`은 형식 자유라 순서를 정의 못 하므로 staleness
    판정에 쓰지 않는다(운영 메타로만). 스코핑·over-claim 권한 검증은 store 밖
    (`accept_published_index` 핸들러 함수)에서 하고 store는 staleness만 본다.
    """

    def __init__(self, indexes: Sequence[KnowledgeIndex] = ()) -> None:
        self._store: dict[str, KnowledgeIndex] = {idx.agent_id: idx for idx in indexes}

    def all_indexes(self) -> Sequence[KnowledgeIndex]:
        return list(self._store.values())

    def get(self, agent_id: str) -> KnowledgeIndex | None:
        return self._store.get(agent_id)

    def put(self, index: KnowledgeIndex) -> None:
        """더 새 인덱스만 수용한다(ADR 0028 §14 결정 C — generated_at staleness).

        첫 인덱스는 무조건 수용·`generated_at`이 더 새면 교체·동률/역행(`<=`)은 거부(no-op).
        역행 거부: 옛 인덱스가 재연결/재전송으로 늦게 도착해도 최신을 덮지 않는다. 동률
        거부(멱등): 같은 인덱스 재도착을 흡수한다(`SubmitAnswer` ticket_id 멱등 정신).
        결정론 `build_knowledge_index_from_okf`가 같은 OKF·같은 generated_at→같은 인덱스를
        보장하므로 동률 교체는 무의미하다. per-agent 격리: 한 agent 갱신이 다른 agent 무영향.
        """
        existing = self._store.get(index.agent_id)
        if existing is None:
            self._store[index.agent_id] = index
        elif index.generated_at > existing.generated_at:
            self._store[index.agent_id] = index
        # else: 동률·역행 → 거부(no-op·기존 보존)


# ── PublishIndex 수용 경로(중앙 핸들러 처리 로직, ADR 0028 §14 결정 B·D·F) ─────
#
# owner 워커가 보낸 PublishIndex를 중앙이 수용할 때의 *결정론 처리 로직*. 순수 함수로
# 분리해 가짜 프레임·InMemory store·registry로 단위 테스트한다(실 WS 수신 루프 연결만
# 게이트 밖). 두 게이트를 순서대로 통과해야 보관된다:
#   B. 워커-소유자 스코핑(publishable): 연결 세션의 인증 owner == index.agent_id의 card.owner
#      (사칭/미등록 차단 — 인덱스 단위 거부).
#   D. over-claim concept 필터(filter_authorized_concepts): card 권한 안의 concept만 보관
#      (concept 단위 필터 — 전부 떨어지면 빈 concepts로 보관·인덱스 자체는 안 거부).


def publishable(session_owner_id: str, index: KnowledgeIndex, registry: Registry) -> bool:
    """워커-소유자 스코핑 술어 — 이 인덱스를 그 인증 owner가 publish할 수 있는가(결정 B).

    `index.agent_id`의 카드가 *연결 세션의 인증 owner*(`RegisterWorker.owner_id`) 소유여야
    한다(`card.owner == session_owner_id`). 미등록 agent_id(`registry.get` KeyError)·타 owner
    카드면 거부(다른 owner 사칭 차단). owner는 프레임에 다시 싣지 않는다 — 소켓이 곧 그
    owner(`SubmitAnswer`가 연결 owner로 회신 출처를 강제하는 정신). Authority 중앙: 어느
    카드를 어느 owner가 소유하나는 *중앙 registry 선언*이지 워커 자기보고가 아니다.
    """
    try:
        card = registry.get(index.agent_id)
    except KeyError:
        return False  # 미등록 agent_id — "유효하지 않은 인덱스는 안 받는다"(등록 무결성)
    return card.owner == session_owner_id


def filter_authorized_concepts(index: KnowledgeIndex, card: AgentCard) -> KnowledgeIndex:
    """over-claim concept을 떨궈낸 인덱스를 만든다(저장 단계 admission, 결정 D).

    `domain_authorized`(공유 권위 술어)를 통과한 concept만 보관한다 — over-claim
    (`domain ∉ card.domains`)·`cannot_answer` concept은 떨군다. 전부 떨어지면 *빈 concepts
    인덱스로 보관*한다(0 concept → 라우팅 0 후보로 자연 처리·미아 없음과 무관). 인덱스
    자체는 거부하지 않는다 — concept 단위 필터(인덱스 단위 거부는 스코핑[결정 B]의 owner
    사칭만). 라우팅 시 `TwoStageRouter.route`의 권한 재검증과 *같은* 함수를 공유한다(이중
    게이트·단일 권위). frozen이라 새 인덱스로 교체한다(version·generated_at·edges 보존).
    """
    kept: tuple[Concept, ...] = tuple(
        c for c in index.concepts if domain_authorized(c.domain, card)
    )
    if len(kept) == len(index.concepts):
        return index  # 전부 통과 — 새 객체 안 만들고 그대로(불변)
    return index.model_copy(update={"concepts": kept})


def accept_published_index(
    session_owner_id: str,
    index: KnowledgeIndex,
    registry: Registry,
    store: PublishedIndexStore,
) -> bool:
    """중앙이 PublishIndex 한 건을 수용 처리한다 — 스코핑→필터→put(결정 F 통합).

    순서: ① 워커-소유자 스코핑(`publishable`, 결정 B) — 불통이면 거부(보관 안 함·False).
    ② over-claim concept 필터(`filter_authorized_concepts`, 결정 D) — 권한 안 concept만 남김.
    ③ `store.put`(staleness, 결정 C) — 더 새 것만 수용(동률/역행 거부). 이 *처리 로직*은
    결정론(실 WS 수신 루프 연결만 게이트 밖). 반환: 스코핑 통과로 store.put까지 갔으면
    True(staleness로 put이 no-op이어도 스코핑 자체는 통과했으므로 True)·스코핑 거부면 False.
    """
    if not publishable(session_owner_id, index, registry):
        return False
    card = registry.get(index.agent_id)  # publishable 통과 → 존재 보장
    filtered = filter_authorized_concepts(index, card)
    store.put(filtered)
    return True


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
      assessor:    ConfidenceAssessor(옵셔널 — None이면 ≥2→Contested T10.3a 동작,
                   주입 시 stage-2 자동해소 시도).
      clear_winner_margin: stage-2 clear winner 판정 임계(옵셔널 — None이면 기본 0.0).
                   assessor 주입 시 top.confidence - second.confidence >= margin이면
                   단독 최고 후보로 Routed 자동해소. None이면 0.0 적용.
    """

    def __init__(
        self,
        registry: Registry,
        matcher: KnowledgeIndexMatcher,
        store: PublishedIndexStore,
        root_user: str,
        precedents: PrecedentStore | None = None,
        assessor: ConfidenceAssessor | None = None,
        clear_winner_margin: float | None = None,
    ) -> None:
        self._registry = registry
        self._matcher = matcher
        self._store = store
        self._root_user = root_user
        self._precedents = precedents
        self._assessor = assessor
        # None이면 기본 0.0 — 단독 최고면 clear, 동점이면 Contested
        self._clear_winner_margin: float = clear_winner_margin if clear_winner_margin is not None else 0.0

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
            # 공유 권위 술어(agent_card.domain_authorized) — publish over-claim 필터(T10.4
            # 결정 D)와 *같은* 함수. 이중 게이트: publish가 1차 admission, 이 라우팅이 2차 방어.
            if domain_authorized(domain, card):
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
                    # 공유 권위 술어(publish over-claim 필터와 같은 함수).
                    if domain_authorized(representative_intent, card):
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

        # ── ≥2 후보 → stage-2 자동해소 시도(assessor 주입 시) or Contested ──────
        candidate_cards: list[AgentCard] = []
        for match, _domain in authorized:
            try:
                card = self._registry.get(match.agent_id)
                candidate_cards.append(card)
            except KeyError:
                continue

        # assessor 미주입 → T10.3a 동작(≥2→Contested) 무회귀
        if self._assessor is None:
            return Contested(
                candidates=tuple(candidate_cards),
                reason=f"후보 {len(candidate_cards)}건, Authority 미정(assessor 미주입)",
                intent=representative_intent,
            )

        # stage-2: 권한통과 후보에게만 assess 호출(권한 밖은 이미 제외)
        confidences: list[GroundedConfidence] = [
            self._assessor.assess(question, card) for card in candidate_cards
        ]

        # confidence 내림차순 정렬 — 동점 tie-break: agent_id 오름차순(결정론)
        sorted_confs = sorted(
            confidences, key=lambda gc: (-gc.confidence, gc.agent_id)
        )

        top = sorted_confs[0]
        second = sorted_confs[1]

        # 동점(top.confidence == second.confidence) → Contested
        if top.confidence == second.confidence:
            return Contested(
                candidates=tuple(candidate_cards),
                reason=f"후보 {len(candidate_cards)}건, stage-2 동점(confidence={top.confidence:.3f})",
                intent=representative_intent,
            )

        # clear winner: top - second >= margin → Routed 자동해소
        if top.confidence - second.confidence >= self._clear_winner_margin:
            try:
                winner_card = self._registry.get(top.agent_id)
            except KeyError:
                # 카드 조회 실패 → Contested 폴백(미아 없음 보존)
                return Contested(
                    candidates=tuple(candidate_cards),
                    reason=f"stage-2 winner 카드 조회 실패: {top.agent_id}",
                    intent=representative_intent,
                )
            # winner의 domain: authorized에서 agent_id로 domain 찾기
            winner_domain = representative_intent
            for match, domain in authorized:
                if match.agent_id == top.agent_id:
                    winner_domain = domain
                    break
            return attach_gates(
                Routed(
                    primary=winner_card,
                    reason=f"stage-2 자동해소: intent '{winner_domain}' → {top.agent_id} (confidence={top.confidence:.3f})",
                    intent=winner_domain,
                ),
                winner_domain,
                self._registry,
            )

        # 격차 < margin → Contested 폴백
        return Contested(
            candidates=tuple(candidate_cards),
            reason=f"후보 {len(candidate_cards)}건, stage-2 격차 부족(top={top.confidence:.3f}, second={second.confidence:.3f})",
            intent=representative_intent,
        )
