"""TwoStageRouter (T10.3a) red→green 테스트.

stage-1 2단 라우팅 통합:
  - FakeMatcher·InMemoryPublishedIndexStore 주입 결정론.
  - 권한 재검증(authorized): concept.domain in card.domains and ∉ cannot_answer.
  - 미등록 agent_id 제외.
  - 투영: 0→Unowned·1→Routed(intent=concept.domain·attach_gates)·≥2→Contested.
  - Precedent 단축경로 보존.
  - 기존 Router 무회귀(attach_gates 추출 동작 무변경).

불변식:
  - 미아 없음: 모든 경로가 Routed/Unowned/Contested 종착.
  - Authority 중앙: authorized()=card.domains 게이트.
  - 중앙 토큰 0: FakeMatcher 결정론(LLM 0).
  - 노출 불변식: score·matched_concept_id RoutingDecision 미포함.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.conflict import InMemoryPrecedentStore, Resolution
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.index_matcher import FakeMatcher, IndexMatch
from agent_org_network.knowledge_index import Concept, KnowledgeIndex
from agent_org_network.registry import Registry
from agent_org_network.two_stage_router import (
    InMemoryPublishedIndexStore,
    PublishedIndexStore,
    TwoStageRouter,
)

# ── 헬퍼 픽스처 ──────────────────────────────────────────────────────────────

_NOW = datetime(2026, 6, 27, 0, 0, 0, tzinfo=timezone.utc)
_REVIEWED = date(2026, 6, 27)


def _card(
    agent_id: str,
    owner: str = "alice",
    domains: list[str] | None = None,
    cannot_answer: list[str] | None = None,
    approval_when: list[str] | None = None,
    collaborate_when: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary=f"{agent_id} 요약",
        domains=domains or [],
        last_reviewed_at=_REVIEWED,
        cannot_answer=cannot_answer or [],
        approval_when=approval_when or [],
        collaborate_when=collaborate_when or [],
    )


def _concept(cid: str, label: str, core_question: str, domain: str) -> Concept:
    return Concept(id=cid, label=label, core_question=core_question, domain=domain)


def _index(agent_id: str, *concepts: Concept, version: str = "v1") -> KnowledgeIndex:
    return KnowledgeIndex(
        agent_id=agent_id,
        version=version,
        generated_at=_NOW,
        concepts=concepts,
    )


def _match(agent_id: str, score: float, concept_id: str) -> IndexMatch:
    return IndexMatch(agent_id=agent_id, score=score, matched_concept_id=concept_id)


def _registry(*cards: AgentCard) -> Registry:
    reg = Registry()
    for card in cards:
        reg.register(card)
    return reg


def _router(
    registry: Registry,
    matcher: FakeMatcher,
    store: PublishedIndexStore,
    root_user: str = "root",
    precedents: InMemoryPrecedentStore | None = None,
) -> TwoStageRouter:
    return TwoStageRouter(
        registry=registry,
        matcher=matcher,
        store=store,
        root_user=root_user,
        precedents=precedents,
    )


# ════════════════════════════════════════════════════════════════════════════
# 1. Concept.domain 필수·빈값 거부 (ADR 0028 §13 결정 B)
# ════════════════════════════════════════════════════════════════════════════


class TestConceptDomainValidation:
    """Concept.domain 필수 필드 검증 — id·core_question 검증과 동형."""

    def test_domain_필드_보존(self) -> None:
        c = _concept("c1", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        assert c.domain == "환불"

    def test_빈_domain_거부(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Concept(id="c1", label="라벨", core_question="질문", domain="")

    def test_공백만_domain_거부(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Concept(id="c1", label="라벨", core_question="질문", domain="   ")


# ════════════════════════════════════════════════════════════════════════════
# 2. PublishedIndexStore — InMemoryPublishedIndexStore
# ════════════════════════════════════════════════════════════════════════════


class TestInMemoryPublishedIndexStore:
    """InMemoryPublishedIndexStore 기본 동작."""

    def test_all_indexes_빈_초기화(self) -> None:
        store = InMemoryPublishedIndexStore()
        assert list(store.all_indexes()) == []

    def test_초기_인덱스_주입(self) -> None:
        c = _concept("c1", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        indexes = list(store.all_indexes())
        assert len(indexes) == 1
        assert indexes[0].agent_id == "cs_ops"

    def test_put_get(self) -> None:
        store = InMemoryPublishedIndexStore()
        c = _concept("c1", "가격", "가격이 얼마인가?", domain="가격")
        idx = _index("price_ops", c)
        store.put(idx)
        result = store.get("price_ops")
        assert result is not None
        assert result.agent_id == "price_ops"

    def test_get_없으면_None(self) -> None:
        store = InMemoryPublishedIndexStore()
        assert store.get("없는_에이전트") is None

    def test_put_갱신(self) -> None:
        store = InMemoryPublishedIndexStore()
        c = _concept("c1", "가격", "가격이 얼마인가?", domain="가격")
        idx_v1 = _index("agent_a", c, version="v1")
        idx_v2 = _index("agent_a", c, version="v2")
        store.put(idx_v1)
        store.put(idx_v2)
        result = store.get("agent_a")
        assert result is not None
        assert result.version == "v2"


# ════════════════════════════════════════════════════════════════════════════
# 3. 권한 재검증(authorized) — over-claim 차단
# ════════════════════════════════════════════════════════════════════════════


class TestAuthorizedFilter:
    """authorized(): concept.domain in card.domains and ∉ cannot_answer."""

    def test_domain_in_card_domains_통과(self) -> None:
        """concept.domain이 card.domains에 있으면 권한 통과."""
        card = _card("cs_ops", domains=["환불", "가격"])
        c_refund = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c_refund)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 정책 질문")
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "cs_ops"

    def test_domain_not_in_card_domains_제외(self) -> None:
        """concept.domain이 card.domains에 없으면(over-claim) 후보 탈락 → Unowned."""
        card = _card("it_ops", domains=["IT지원"])  # "환불" 없음
        c_refund = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("it_ops", c_refund)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("it_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 정책 질문")
        assert isinstance(result, Unowned)
        assert result.escalated_to == "root"

    def test_domain_in_cannot_answer_제외(self) -> None:
        """concept.domain이 cannot_answer에 있으면 후보 탈락."""
        card = _card("cs_ops", domains=["환불"], cannot_answer=["환불"])
        c_refund = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c_refund)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 정책 질문")
        assert isinstance(result, Unowned)

    def test_미등록_agent_id_제외(self) -> None:
        """Registry에 없는 agent_id 후보는 제외된다(등록 무결성)."""
        # card 등록 안 함, 인덱스에는 agent_id 있음
        c = _concept("c1", "가격", "가격이 얼마인가?", domain="가격")
        idx = _index("unknown_agent", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry()  # 아무것도 등록 안 함
        matcher = FakeMatcher((_match("unknown_agent", 1.0, "c1"),))
        router = _router(reg, matcher, store)

        result = router.route("가격 질문")
        assert isinstance(result, Unowned)
        assert result.escalated_to == "root"


# ════════════════════════════════════════════════════════════════════════════
# 4. 투영 — 0/1/≥2 분기
# ════════════════════════════════════════════════════════════════════════════


class TestProjection:
    """권한 통과 후보 수에 따른 RoutingDecision 투영."""

    def test_권한통과_0_Unowned_미아_없음(self) -> None:
        """권한 통과 0 → Unowned(루트) — 미아 없음 불변식."""
        reg = _registry()
        store = InMemoryPublishedIndexStore()
        matcher = FakeMatcher(())
        router = _router(reg, matcher, store)

        result = router.route("아무 질문")
        assert isinstance(result, Unowned)
        assert result.escalated_to == "root"

    def test_권한통과_1_Routed_primary_정확(self) -> None:
        """권한 통과 1 → Routed(primary=해당 카드)."""
        card = _card("cs_ops", domains=["환불"])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "cs_ops"

    def test_권한통과_1_intent_concept_domain(self) -> None:
        """Routed.intent = 매칭 concept.domain(결정 E·ADR 0015)."""
        card = _card("cs_ops", domains=["환불"])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert result.intent == "환불"

    def test_권한통과_2이상_Contested(self) -> None:
        """권한 통과 ≥2 → Contested(candidates·이번엔 stage-2 직행 아님)."""
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        c_a = _concept("c_a", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        c_b = _concept("c_b", "환불", "환불 처리는 어떻게 하나?", domain="환불")
        idx_a = _index("cs_ops", c_a)
        idx_b = _index("finance_ops", c_b)
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        reg = _registry(card_a, card_b)
        matcher = FakeMatcher((
            _match("cs_ops", 1.0, "c_a"),
            _match("finance_ops", 0.8, "c_b"),
        ))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Contested)
        candidate_ids = {c.agent_id for c in result.candidates}
        assert candidate_ids == {"cs_ops", "finance_ops"}

    def test_Contested_intent_대표_domain(self) -> None:
        """Contested.intent = 최고 점수 후보의 concept.domain."""
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        c_a = _concept("c_a", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        c_b = _concept("c_b", "환불", "환불 처리는 어떻게 하나?", domain="환불")
        idx_a = _index("cs_ops", c_a)
        idx_b = _index("finance_ops", c_b)
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        reg = _registry(card_a, card_b)
        matcher = FakeMatcher((
            _match("cs_ops", 1.0, "c_a"),
            _match("finance_ops", 0.8, "c_b"),
        ))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Contested)
        assert result.intent == "환불"

    def test_인덱스_없으면_Unowned(self) -> None:
        """인덱스가 없으면 matcher가 빈 튜플 → Unowned."""
        card = _card("cs_ops", domains=["환불"])
        store = InMemoryPublishedIndexStore()
        reg = _registry(card)
        matcher = FakeMatcher(())
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Unowned)


# ════════════════════════════════════════════════════════════════════════════
# 5. attach_gates 적용(Approval·Collaborator)
# ════════════════════════════════════════════════════════════════════════════


class TestAttachGates:
    """1→Routed 시 attach_gates(approval_when/collaborate_when) 적용 확인."""

    def test_approval_when_domain_포함_requires_approval_True(self) -> None:
        """approval_when에 concept.domain이 있으면 requires_approval=True."""
        card = _card(
            "cs_ops",
            domains=["환불"],
            approval_when=["환불"],
        )
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert result.requires_approval is True

    def test_approval_when_없으면_requires_approval_False(self) -> None:
        """approval_when 미포함이면 requires_approval=False."""
        card = _card("cs_ops", domains=["환불"], approval_when=[])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert result.requires_approval is False

    def test_collaborate_when_collaborator_부착(self) -> None:
        """collaborate_when에 domain이 있으면 collaborator 카드 부착."""
        card_primary = _card("cs_ops", domains=["환불"], collaborate_when=["환불"])
        card_collab = _card("finance_ops", domains=["환불"])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card_primary, card_collab)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert len(result.collaborators) == 1
        assert result.collaborators[0].agent_id == "finance_ops"


# ════════════════════════════════════════════════════════════════════════════
# 6. Precedent 단축경로 보존
# ════════════════════════════════════════════════════════════════════════════


class TestPrecedentShortcut:
    """판례 주입 시 stage-1 직후 단축경로 동작."""

    def test_판례_있으면_Routed_단축(self) -> None:
        """판례가 있으면 stage-1 후보 무관하게 판례 Routed 반환."""
        card = _card("cs_ops", domains=["환불"])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))

        precedent_store = InMemoryPrecedentStore()
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))

        router = _router(reg, matcher, store, precedents=precedent_store)
        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "cs_ops"
        assert "판례" in result.reason

    def test_invalidated_판례는_폴백(self) -> None:
        """무효화된 판례는 건너뛰고 stage-1 결과로 폴백."""
        card = _card("cs_ops", domains=["환불"])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))

        precedent_store = InMemoryPrecedentStore()
        precedent_store.record(Resolution(intent="환불", primary="cs_ops"))
        precedent_store.invalidate("환불", by_owner="alice", at=_NOW)

        router = _router(reg, matcher, store, precedents=precedent_store)
        result = router.route("환불 질문")
        # 무효화 폴백 → stage-1 결과(1 후보 Routed, 판례 아님)
        assert isinstance(result, Routed)
        assert "판례" not in result.reason

    def test_판례_없으면_stage1_결과(self) -> None:
        """판례 미주입 시 stage-1 결과 그대로."""
        card = _card("cs_ops", domains=["환불"])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)  # precedents=None

        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert "판례" not in result.reason


# ════════════════════════════════════════════════════════════════════════════
# 7. 결정론 — 같은 입력·같은 결과
# ════════════════════════════════════════════════════════════════════════════


class TestDeterminism:
    """같은 질문+인덱스+store → 같은 RoutingDecision(결정론 불변식)."""

    def test_같은_입력_반복_같은_결과(self) -> None:
        card = _card("cs_ops", domains=["환불"])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = _router(reg, matcher, store)

        results = [router.route("환불 질문") for _ in range(5)]
        assert all(isinstance(r, Routed) for r in results)
        assert all(r.primary.agent_id == "cs_ops" for r in results)  # type: ignore[union-attr]

    def test_다수_후보_순서_결정론(self) -> None:
        """≥2 후보 Contested의 candidates 순서가 반복 호출에서 일정."""
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        c_a = _concept("c_a", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        c_b = _concept("c_b", "환불", "환불 처리는 어떻게 하나?", domain="환불")
        idx_a = _index("cs_ops", c_a)
        idx_b = _index("finance_ops", c_b)
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        reg = _registry(card_a, card_b)
        matcher = FakeMatcher((
            _match("cs_ops", 1.0, "c_a"),
            _match("finance_ops", 0.8, "c_b"),
        ))
        router = _router(reg, matcher, store)

        results = [router.route("환불 질문") for _ in range(5)]
        assert all(isinstance(r, Contested) for r in results)
        first_ids = [c.agent_id for c in results[0].candidates]  # type: ignore[union-attr]
        for r in results[1:]:
            assert [c.agent_id for c in r.candidates] == first_ids  # type: ignore[union-attr]


# ════════════════════════════════════════════════════════════════════════════
# 8. 노출 불변식 — score·matched_concept_id RoutingDecision 미포함
# ════════════════════════════════════════════════════════════════════════════


class TestExposureInvariant:
    """score·matched_concept_id는 RoutingDecision/Routed/Contested에 안 실림."""

    def test_Routed에_score_없음(self) -> None:
        card = _card("cs_ops", domains=["환불"])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 0.9, "c_r"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert not hasattr(result, "score")
        assert not hasattr(result, "matched_concept_id")

    def test_Contested에_score_없음(self) -> None:
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        c_a = _concept("c_a", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        c_b = _concept("c_b", "환불", "환불 처리는 어떻게 하나?", domain="환불")
        idx_a = _index("cs_ops", c_a)
        idx_b = _index("finance_ops", c_b)
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        reg = _registry(card_a, card_b)
        matcher = FakeMatcher((
            _match("cs_ops", 1.0, "c_a"),
            _match("finance_ops", 0.8, "c_b"),
        ))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Contested)
        assert not hasattr(result, "score")
        assert not hasattr(result, "matched_concept_id")


# ════════════════════════════════════════════════════════════════════════════
# 9. 복합 시나리오
# ════════════════════════════════════════════════════════════════════════════


class TestComplexScenarios:
    """복합 시나리오 — 권한 필터가 후보를 줄여 0/1/≥2 전환."""

    def test_2후보_중_1개_권한_통과_Routed(self) -> None:
        """2 매처 후보 중 1개만 권한 통과 → Routed."""
        card_cs = _card("cs_ops", domains=["환불"])  # 환불 권한 있음
        card_it = _card("it_ops", domains=["IT지원"])  # 환불 권한 없음
        c_cs = _concept("c_cs", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        c_it = _concept("c_it", "환불", "환불 처리 IT 절차", domain="환불")  # domain="환불"이지만 card.domains에 없음
        idx_cs = _index("cs_ops", c_cs)
        idx_it = _index("it_ops", c_it)
        store = InMemoryPublishedIndexStore([idx_cs, idx_it])
        reg = _registry(card_cs, card_it)
        matcher = FakeMatcher((
            _match("cs_ops", 1.0, "c_cs"),
            _match("it_ops", 0.9, "c_it"),
        ))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "cs_ops"

    def test_2후보_모두_권한_탈락_Unowned(self) -> None:
        """2 매처 후보 모두 권한 탈락 → Unowned."""
        card_a = _card("agent_a", domains=["가격"])  # 환불 없음
        card_b = _card("agent_b", domains=["배송"])  # 환불 없음
        c_a = _concept("c_a", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        c_b = _concept("c_b", "환불", "환불 처리", domain="환불")
        idx_a = _index("agent_a", c_a)
        idx_b = _index("agent_b", c_b)
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        reg = _registry(card_a, card_b)
        matcher = FakeMatcher((
            _match("agent_a", 1.0, "c_a"),
            _match("agent_b", 0.9, "c_b"),
        ))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Unowned)
        assert result.escalated_to == "root"

    def test_cannot_answer_하나_탈락_나머지_Routed(self) -> None:
        """2후보 중 1개가 cannot_answer로 탈락 → 나머지 1개 Routed."""
        card_a = _card("cs_ops", domains=["환불"], cannot_answer=["환불"])  # cannot_answer
        card_b = _card("finance_ops", domains=["환불"])
        c_a = _concept("c_a", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        c_b = _concept("c_b", "환불", "환불 처리", domain="환불")
        idx_a = _index("cs_ops", c_a)
        idx_b = _index("finance_ops", c_b)
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        reg = _registry(card_a, card_b)
        matcher = FakeMatcher((
            _match("cs_ops", 1.0, "c_a"),
            _match("finance_ops", 0.9, "c_b"),
        ))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "finance_ops"

    def test_root_user_커스텀(self) -> None:
        """root_user 파라미터가 Unowned.escalated_to에 반영된다."""
        store = InMemoryPublishedIndexStore()
        reg = _registry()
        matcher = FakeMatcher(())
        router = TwoStageRouter(
            registry=reg,
            matcher=matcher,
            store=store,
            root_user="custom_root",
        )

        result = router.route("질문")
        assert isinstance(result, Unowned)
        assert result.escalated_to == "custom_root"


# ════════════════════════════════════════════════════════════════════════════
# 10. Precedent 단축경로 권한 재검증 (T10.3a code-reviewer 지적)
# ════════════════════════════════════════════════════════════════════════════


class TestPrecedentReauthorization:
    """precedent primary 카드도 권한 재검증 — over-claim precedent 무시."""

    def test_precedent_권한_우회_차단(self) -> None:
        """precedent.primary 카드가 해당 intent에 권한 없으면 단축경로 건너뜀 → stage-1 결과.

        over-claim precedent(it_ops.domains에 "환불" 없음)가 있어도
        stage-1 권한 통과 후보(cs_ops)로 Routed 돼야 한다.
        """
        card_cs = _card("cs_ops", domains=["환불"])      # 환불 권한 있음
        card_it = _card("it_ops", domains=["IT지원"])    # 환불 권한 없음
        c_cs = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx_cs = _index("cs_ops", c_cs)
        store = InMemoryPublishedIndexStore([idx_cs])
        reg = _registry(card_cs, card_it)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))

        # it_ops가 "환불" intent의 primary로 기록된 판례 — it_ops엔 "환불" 권한 없음
        precedent_store = InMemoryPrecedentStore()
        precedent_store.record(Resolution(intent="환불", primary="it_ops"))

        router = _router(reg, matcher, store, precedents=precedent_store)
        result = router.route("환불 질문")

        # over-claim precedent는 무시되고 stage-1 결과(cs_ops)로 라우팅돼야 함
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "cs_ops"
        assert "판례" not in result.reason

    def test_store_get_None_보수적_탈락(self) -> None:
        """all_indexes()에 후보가 있으나 get()이 None 반환하면 해당 후보 탈락 → Unowned.

        all_indexes()와 get()이 불일치할 때(동시 put·실 store 경쟁) 권한 넓히는
        위험 없음 — 보수적 탈락으로 미아 없음 보존.
        """
        from collections.abc import Sequence

        card = _card("cs_ops", domains=["환불"])
        c = _concept("c_r", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)

        class StubStoreGetNone:
            """all_indexes()는 후보 반환, get()은 항상 None."""
            def all_indexes(self) -> Sequence[KnowledgeIndex]:
                return [idx]
            def get(self, agent_id: str) -> KnowledgeIndex | None:
                return None
            def put(self, index: KnowledgeIndex) -> None:
                pass

        store = StubStoreGetNone()
        reg = _registry(card)
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_r"),))
        router = TwoStageRouter(
            registry=reg, matcher=matcher, store=store, root_user="root"
        )

        result = router.route("환불 질문")
        # store.get() 불일치 → 보수적 탈락 → 다른 후보 없음 → Unowned
        assert isinstance(result, Unowned)
        assert result.escalated_to == "root"

    def test_matched_concept_id_부재_폴백(self) -> None:
        """IndexMatch.matched_concept_id가 인덱스 concepts에 없으면 후보 탈락 → Unowned."""
        card = _card("cs_ops", domains=["환불"])
        c = _concept("c_real", "환불", "환불 정책이 어떻게 되나?", domain="환불")
        idx = _index("cs_ops", c)
        store = InMemoryPublishedIndexStore([idx])
        reg = _registry(card)
        # matched_concept_id="c_ghost" — 인덱스에 없는 concept id
        matcher = FakeMatcher((_match("cs_ops", 1.0, "c_ghost"),))
        router = _router(reg, matcher, store)

        result = router.route("환불 질문")
        # concept 못 찾음 → domain="" → 권한 필터 탈락 → Unowned
        assert isinstance(result, Unowned)
        assert result.escalated_to == "root"
