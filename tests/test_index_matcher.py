"""KnowledgeIndexMatcher 포트 + ConceptOverlapMatcher(v1) + FakeMatcher — T10.2 red→green 테스트.

슬라이스:
  (a) IndexMatch 값 객체 + KnowledgeIndexMatcher Protocol
  (b) ConceptOverlapMatcher — 결정론 토큰 오버랩 매칭(LLM 0·벡터 0)
  (c) FakeMatcher — 고정 후보 반환 테스트 더블(FakeClassifier 정신)

불변식:
  - 중앙 토큰 0: LLM·외부 API·벡터 인프라 0(순수 토큰 오버랩).
  - 미아 없음: 0 후보를 정상 반환(떨구지 않음) — Unowned 투영은 T10.3a 책임.
  - Authority 중앙: 매처는 후보 제안만 — 권한·종착 판정 아님.
  - 결정론: 같은 질문+인덱스 → 항상 같은 순서·같은 결과.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_org_network.index_matcher import (
    ConceptOverlapMatcher,
    FakeMatcher,
    IndexMatch,
    KnowledgeIndexMatcher,
    relevant_concepts,
)
from agent_org_network.knowledge_index import Concept, KnowledgeIndex

# ── 헬퍼 ────────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 6, 27, 0, 0, 0, tzinfo=timezone.utc)


def _concept(
    cid: str,
    label: str,
    core_question: str,
    domain: str = "general",
    type_: str | None = None,
) -> Concept:
    return Concept(id=cid, label=label, core_question=core_question, domain=domain, type=type_)


def _index(agent_id: str, *concepts: Concept) -> KnowledgeIndex:
    return KnowledgeIndex(
        agent_id=agent_id,
        version="v1",
        generated_at=_NOW,
        concepts=concepts,
    )


# ── 공용 개념 픽스처 ─────────────────────────────────────────────────────────

_C_PRICE = _concept("c_price", "가격", "상품 가격이 얼마인가?", domain="가격", type_="pricing")
_C_REFUND = _concept("c_refund", "환불", "환불 정책이 어떻게 되나?", domain="환불", type_="policy")
_C_DELIVERY = _concept("c_delivery", "배송", "배송 기간이 얼마나 걸리나?", domain="배송", type_="logistics")
_C_UNRELATED = _concept("c_x", "기타", "전혀 관계없는 매우 특이한 주제다", domain="기타", type_=None)


# ════════════════════════════════════════════════════════════════════════════
# 1. IndexMatch 값 객체
# ════════════════════════════════════════════════════════════════════════════


class TestIndexMatch:
    """IndexMatch frozen pydantic 값 객체 기본 동작."""

    def test_유효_IndexMatch_생성_필드_보존(self) -> None:
        m = IndexMatch(agent_id="cs_ops", score=0.5, matched_concept_id="c1")
        assert m.agent_id == "cs_ops"
        assert m.score == 0.5
        assert m.matched_concept_id == "c1"

    def test_frozen_수정_불가(self) -> None:
        m = IndexMatch(agent_id="cs_ops", score=0.5, matched_concept_id="c1")
        with pytest.raises(Exception):
            m.agent_id = "other"  # type: ignore[misc]

    def test_score_float_보존(self) -> None:
        m = IndexMatch(agent_id="a", score=1.0, matched_concept_id="cx")
        assert isinstance(m.score, float)


# ════════════════════════════════════════════════════════════════════════════
# 2. KnowledgeIndexMatcher Protocol 구조 만족
# ════════════════════════════════════════════════════════════════════════════


class TestKnowledgeIndexMatcherProtocol:
    """ConceptOverlapMatcher·FakeMatcher가 KnowledgeIndexMatcher Protocol 구조 만족."""

    def test_ConceptOverlapMatcher_Protocol_만족(self) -> None:
        matcher: KnowledgeIndexMatcher = ConceptOverlapMatcher()
        result = matcher.match("가격", [])
        assert isinstance(result, tuple)

    def test_FakeMatcher_Protocol_만족(self) -> None:
        fake: KnowledgeIndexMatcher = FakeMatcher(())
        result = fake.match("아무 질문", [])
        assert isinstance(result, tuple)


# ════════════════════════════════════════════════════════════════════════════
# 3. ConceptOverlapMatcher — 기본 매칭 동작
# ════════════════════════════════════════════════════════════════════════════


class TestConceptOverlapMatcherBasic:
    """ConceptOverlapMatcher 기본 매칭 동작."""

    def setup_method(self) -> None:
        self.matcher = ConceptOverlapMatcher()

    # ── 미아 없음 — 빈 결과를 정상 반환 ─────────────────────────────────────

    def test_오버랩_없으면_후보_0_빈_튜플(self) -> None:
        """공유 토큰 0 → 후보 0(빈 튜플·정상 반환)."""
        idx = _index("agent_a", _C_UNRELATED)
        result = self.matcher.match("가격이 얼마인가요", [idx])
        assert result == ()

    def test_빈_인덱스_리스트_후보_0(self) -> None:
        """인덱스 없으면 후보 0."""
        result = self.matcher.match("가격", [])
        assert result == ()

    def test_빈_concepts_인덱스_후보_0(self) -> None:
        """concepts=() 인덱스 → 그 에이전트 후보 0."""
        idx = _index("agent_empty")
        result = self.matcher.match("가격", [idx])
        assert result == ()

    # ── 1개 매칭 ────────────────────────────────────────────────────────────

    def test_1개_매칭_후보_1_matched_concept_id_정확(self) -> None:
        """1개 후보·matched_concept_id가 정확히 매칭된 개념 id."""
        idx = _index("cs_ops", _C_PRICE)
        result = self.matcher.match("상품 가격이 얼마인가요?", [idx])
        assert len(result) == 1
        assert result[0].agent_id == "cs_ops"
        assert result[0].matched_concept_id == "c_price"
        assert result[0].score > 0

    def test_1개_매칭_에이전트별_1개_IndexMatch(self) -> None:
        """같은 에이전트에 여러 개념이 매칭돼도 IndexMatch는 1(최고 점수 개념)."""
        c_price2 = _concept("c_price2", "가격 조회", "상품 가격을 어떻게 확인하는가?")
        idx = _index("cs_ops", _C_PRICE, c_price2)
        result = self.matcher.match("상품 가격이 얼마인가요?", [idx])
        assert len(result) == 1
        assert result[0].agent_id == "cs_ops"

    # ── 다개념·다에이전트 ────────────────────────────────────────────────────

    def test_다에이전트_각_후보_1개(self) -> None:
        """에이전트 A·B 각각 1개씩 매칭 → 후보 2."""
        idx_a = _index("agent_a", _C_PRICE)
        idx_b = _index("agent_b", _C_PRICE)
        result = self.matcher.match("상품 가격이 얼마인가요?", [idx_a, idx_b])
        agent_ids = {m.agent_id for m in result}
        assert agent_ids == {"agent_a", "agent_b"}
        assert len(result) == 2

    def test_score_내림차순_정렬(self) -> None:
        """결과는 score 내림차순 정렬."""
        # agent_a: 여러 토큰 오버랩(높은 score), agent_b: 적은 오버랩(낮은 score)
        c_high = _concept("c_h", "가격", "상품 가격이 얼마인가?")
        c_low = _concept("c_l", "기타", "가격?")  # 토큰 수가 적어 점수 낮음
        idx_high = _index("agent_high", c_high)
        idx_low = _index("agent_low", c_low)
        result = self.matcher.match("상품 가격이 얼마인가요?", [idx_high, idx_low])
        scores = [m.score for m in result]
        assert scores == sorted(scores, reverse=True)

    def test_동점_agent_id_오름차순_정렬(self) -> None:
        """동점일 때 agent_id 오름차순 정렬(결정론 안정성)."""
        # 동일한 core_question → 동일 점수
        same_q = "가격이 얼마인가?"
        c_same = _concept("c1", "가격", same_q)
        idx_z = _index("zzz_agent", c_same)
        idx_a = _index("aaa_agent", c_same)
        idx_m = _index("mmm_agent", c_same)
        result = self.matcher.match(same_q, [idx_z, idx_a, idx_m])
        agent_ids = [m.agent_id for m in result]
        assert agent_ids == sorted(agent_ids)

    def test_동점_agent_id_정렬_반복_호출_안정(self) -> None:
        """같은 입력 반복 호출 → 동점 정렬이 안정."""
        same_q = "가격이 얼마인가?"
        c = _concept("c1", "가격", same_q)
        idx_z = _index("zzz_agent", c)
        idx_a = _index("aaa_agent", c)
        result1 = self.matcher.match(same_q, [idx_z, idx_a])
        result2 = self.matcher.match(same_q, [idx_z, idx_a])
        assert [m.agent_id for m in result1] == [m.agent_id for m in result2]


# ════════════════════════════════════════════════════════════════════════════
# 4. ConceptOverlapMatcher — 결정론 보장
# ════════════════════════════════════════════════════════════════════════════


class TestConceptOverlapMatcherDeterminism:
    """결정론: 같은 질문+인덱스 → 항상 같은 순서·같은 결과."""

    def setup_method(self) -> None:
        self.matcher = ConceptOverlapMatcher()

    def test_같은_입력_반복_호출_같은_출력(self) -> None:
        idx_a = _index("agent_a", _C_PRICE, _C_REFUND)
        idx_b = _index("agent_b", _C_DELIVERY)
        question = "상품 가격이 얼마인가요?"
        result1 = self.matcher.match(question, [idx_a, idx_b])
        result2 = self.matcher.match(question, [idx_a, idx_b])
        assert result1 == result2

    def test_결정론_다회_반복(self) -> None:
        idx = _index("agent_x", _C_PRICE, _C_REFUND, _C_DELIVERY)
        question = "환불 정책이 어떻게 되나?"
        results = [self.matcher.match(question, [idx]) for _ in range(10)]
        assert all(r == results[0] for r in results)


# ════════════════════════════════════════════════════════════════════════════
# 5. ConceptOverlapMatcher — 에이전트당 최고 개념 1개 불변식
# ════════════════════════════════════════════════════════════════════════════


class TestConceptOverlapMatcherPerAgentBest:
    """에이전트당 한 IndexMatch — 최고 점수 개념만 채택."""

    def setup_method(self) -> None:
        self.matcher = ConceptOverlapMatcher()

    def test_같은_에이전트_여러_개념_매칭시_IndexMatch_1개(self) -> None:
        c1 = _concept("c1", "가격", "상품 가격이 얼마인가?")
        c2 = _concept("c2", "가격 문의", "가격을 어떻게 확인하는가?")
        c3 = _concept("c3", "환불", "환불 정책이 어떻게 되나?")
        idx = _index("multi_agent", c1, c2, c3)
        result = self.matcher.match("상품 가격", [idx])
        assert len(result) == 1
        assert result[0].agent_id == "multi_agent"

    def test_최고_점수_개념이_matched_concept_id(self) -> None:
        """여러 개념 중 가장 높은 점수의 개념이 matched_concept_id."""
        # c_best: "가격이 얼마인가?" — 질문과 토큰 오버랩 많음
        c_best = _concept("c_best", "가격", "가격이 얼마인가?")
        # c_weak: "기타" — 오버랩 적음
        c_weak = _concept("c_weak", "기타", "다른 주제에 대한 질문이다")
        idx = _index("agent_a", c_best, c_weak)
        result = self.matcher.match("가격이 얼마인가?", [idx])
        assert len(result) == 1
        assert result[0].matched_concept_id == "c_best"


# ════════════════════════════════════════════════════════════════════════════
# 6. ConceptOverlapMatcher — 토큰화 & 오버랩 세부 동작
# ════════════════════════════════════════════════════════════════════════════


class TestConceptOverlapMatcherTokenization:
    """토큰화·오버랩 규칙 결정론 단언(소문자·공백/문장부호 분리·공유 토큰 기반)."""

    def setup_method(self) -> None:
        self.matcher = ConceptOverlapMatcher()

    def test_대소문자_무관_매칭(self) -> None:
        """소문자 정규화 — Price와 price는 동일 토큰."""
        c = _concept("c1", "Price", "What is the Price of the product?")
        idx = _index("eng_agent", c)
        # 대문자 포함 질문
        result_upper = self.matcher.match("What is the Price?", [idx])
        result_lower = self.matcher.match("what is the price?", [idx])
        # 둘 다 같은 agent_id 매칭
        assert {m.agent_id for m in result_upper} == {m.agent_id for m in result_lower}

    def test_문장부호_분리(self) -> None:
        """'가격?' '가격,' '가격' 모두 같은 토큰 '가격'으로 처리."""
        c = _concept("c1", "가격", "가격이 얼마인가?")
        idx = _index("agent_a", c)
        result = self.matcher.match("가격!", [idx])
        assert len(result) == 1

    def test_공유_토큰_0_후보_없음(self) -> None:
        """공유 토큰 0개 → 후보 0."""
        c = _concept("c1", "xyz", "xyz abc def")
        idx = _index("agent_a", c)
        result = self.matcher.match("가격 환불 배송", [idx])
        assert result == ()

    def test_공유_토큰_1개_이상_후보_있음(self) -> None:
        """공유 토큰 1개 이상 → 후보 포함(score > 0 임계)."""
        c = _concept("c1", "가격", "가격이 얼마인가")
        idx = _index("agent_a", c)
        result = self.matcher.match("가격", [idx])
        assert len(result) == 1
        assert result[0].score > 0

    def test_type_태그도_토큰_오버랩에_포함(self) -> None:
        """Concept.type도 토큰화 대상(type='pricing' → 질문에 pricing 있으면 매칭)."""
        c = _concept("c1", "가격", "얼마인가", type_="pricing")
        idx = _index("agent_a", c)
        # type 필드의 토큰 'pricing'과 질문의 'pricing' 매칭
        result = self.matcher.match("pricing policy", [idx])
        assert len(result) >= 1

    def test_label도_토큰_오버랩에_포함(self) -> None:
        """Concept.label도 토큰화 대상."""
        c = _concept("c1", "환불정책", "기타 내용입니다", type_=None)
        idx = _index("agent_a", c)
        result = self.matcher.match("환불정책", [idx])
        assert len(result) == 1


# ════════════════════════════════════════════════════════════════════════════
# 7. FakeMatcher — 테스트 더블
# ════════════════════════════════════════════════════════════════════════════


class TestFakeMatcher:
    """FakeMatcher: 주입 후보를 그대로 반환·Protocol 시그니처 준수."""

    def test_주입_후보_그대로_반환(self) -> None:
        fixed = (
            IndexMatch(agent_id="agent_a", score=0.9, matched_concept_id="c1"),
            IndexMatch(agent_id="agent_b", score=0.5, matched_concept_id="c2"),
        )
        fake = FakeMatcher(fixed)
        result = fake.match("아무 질문", [])
        assert result == fixed

    def test_빈_후보_주입(self) -> None:
        fake = FakeMatcher(())
        result = fake.match("질문", [])
        assert result == ()

    def test_indexes_무시_고정_반환(self) -> None:
        """FakeMatcher는 indexes 내용과 무관하게 고정 후보만 반환."""
        fixed = (IndexMatch(agent_id="x", score=1.0, matched_concept_id="cx"),)
        fake = FakeMatcher(fixed)
        idx = _index("y", _C_PRICE)
        result = fake.match("가격", [idx])
        assert result == fixed

    def test_match_시그니처_준수_반환_타입_tuple(self) -> None:
        """반환값이 tuple[IndexMatch, ...] 타입."""
        fake = FakeMatcher(())
        result = fake.match("q", [])
        assert isinstance(result, tuple)

    def test_Protocol_타입_어노테이션_만족(self) -> None:
        """FakeMatcher가 KnowledgeIndexMatcher로 타입 어노테이션 가능."""
        fake: KnowledgeIndexMatcher = FakeMatcher(())
        _ = fake.match("q", [])


# ════════════════════════════════════════════════════════════════════════════
# 8. relevant_concepts — 질문과 연관된 개념 추출 (슬라이스 1)
# ════════════════════════════════════════════════════════════════════════════


class TestRelevantConcepts:
    """relevant_concepts(question, index) → tuple[Concept, ...] 순수 헬퍼."""

    def test_오버랩_있는_개념만_반환(self) -> None:
        """질문과 오버랩 > 0인 개념만 포함한다."""
        idx = _index("agent_a", _C_PRICE, _C_UNRELATED)
        result = relevant_concepts("상품 가격이 얼마인가요?", idx)
        ids = {c.id for c in result}
        assert "c_price" in ids
        assert "c_x" not in ids

    def test_오버랩_0_개념은_제외(self) -> None:
        """공유 토큰이 0이면 결과에 포함하지 않는다."""
        idx = _index("agent_a", _C_UNRELATED)
        result = relevant_concepts("상품 가격이 얼마인가요?", idx)
        assert result == ()

    def test_빈_인덱스_빈_튜플(self) -> None:
        """concepts=() 인덱스 → 빈 튜플 반환."""
        idx = _index("agent_empty")
        result = relevant_concepts("가격", idx)
        assert result == ()

    def test_매칭_0_빈_튜플(self) -> None:
        """오버랩 있는 개념이 하나도 없으면 빈 튜플."""
        idx = _index("agent_a", _C_UNRELATED)
        result = relevant_concepts("xyz abc def unique_word_not_in_concepts", idx)
        assert result == ()

    def test_점수_내림차순_정렬(self) -> None:
        """오버랩 점수 내림차순 정렬 — 더 많은 토큰 오버랩 개념이 앞에 온다.

        c_high: "상품 가격 환불 정책" — 질문 "상품 가격 환불"과 3개 이상 공유
        c_low:  "가격 안내" — 1개(가격)만 공유
        높은 오버랩 개념이 먼저 와야 한다(c_h가 c_l보다 앞).
        """
        question = "상품 가격 환불"
        c_high = _concept("c_h", "가격", "상품 가격 환불 정책")
        c_low = _concept("c_l", "가격", "가격 안내")
        idx = _index("agent_a", c_low, c_high)
        result = relevant_concepts(question, idx)
        assert len(result) >= 2
        # c_high가 c_low보다 앞에 와야 한다
        ids = [c.id for c in result]
        assert ids.index("c_h") < ids.index("c_l")

    def test_동점_concept_id_오름차순(self) -> None:
        """동점일 때 concept.id 오름차순 — 결정론 안정성."""
        same_q = "가격이 얼마인가?"
        c_z = _concept("zzz", "가격", same_q)
        c_a = _concept("aaa", "가격", same_q)
        c_m = _concept("mmm", "가격", same_q)
        idx = _index("agent_a", c_z, c_a, c_m)
        result = relevant_concepts(same_q, idx)
        ids = [c.id for c in result]
        assert ids == sorted(ids)

    def test_반환_타입_tuple_of_Concept(self) -> None:
        """반환 타입이 tuple[Concept, ...]."""
        idx = _index("agent_a", _C_PRICE)
        result = relevant_concepts("가격", idx)
        assert isinstance(result, tuple)
        for item in result:
            assert isinstance(item, Concept)

    def test_여러_개념_모두_매칭_전부_반환(self) -> None:
        """오버랩 있는 개념이 여럿이면 전부 반환."""
        idx = _index("agent_a", _C_PRICE, _C_REFUND, _C_DELIVERY)
        result = relevant_concepts("가격 환불 배송", idx)
        ids = {c.id for c in result}
        assert "c_price" in ids
        assert "c_refund" in ids
        assert "c_delivery" in ids

    def test_결정론_같은_입력_같은_출력(self) -> None:
        """같은 질문+인덱스 반복 호출 → 같은 결과."""
        idx = _index("agent_a", _C_PRICE, _C_REFUND, _C_UNRELATED)
        question = "상품 가격 환불"
        r1 = relevant_concepts(question, idx)
        r2 = relevant_concepts(question, idx)
        assert r1 == r2
