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

from collections.abc import Sequence
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

    def test_한국어_조사_정규화_자연어_질문_매칭(self) -> None:
        """조사 붙은 자연어 질문도 매칭 — '환불은'↔개념 '환불'(v1 한국어 대응·실측 회귀)."""
        c = _concept("c1", "환불", "환불 정책이 궁금합니다")
        idx = _index("cs_agent", c)
        assert len(self.matcher.match("환불은 어떻게 받을 수 있나요?", [idx])) == 1
        assert len(self.matcher.match("환불도 받을 수 있나요?", [idx])) == 1

    def test_조사_정규화_짧은_명사_오검출_방지(self) -> None:
        """'가'로 끝나는 짧은 명사(국가)는 조사로 오인하지 않는다(나머지 2글자+ 보존)."""
        c = _concept("c1", "국가", "국가 정책")
        idx = _index("agent_a", c)
        # '국가'(2)에서 '가'를 조사로 오인해 떼면 '국'만 남아 불일치했을 것 — 보존되어 매칭
        assert len(self.matcher.match("국가는 무엇인가요?", [idx])) == 1


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


# ════════════════════════════════════════════════════════════════════════════
# 4. EmbeddingAnnMatcher — 스케일 어댑터(FakeEmbedder 주입 결정론)
# ════════════════════════════════════════════════════════════════════════════

from agent_org_network.index_matcher import (  # noqa: E402
    DEFAULT_EMBED_TAU,
    EmbeddingAnnMatcher,
    select_matcher,
)


class _CountingEmbedder:
    """호출 수를 세는 Embedder 테스트 더블 — 고정 텍스트→벡터 dict + embed 콜/텍스트 카운터.

    FakeEmbedder(okf_dedup) 정신이나 캐시 적중을 단언하려 호출 계측을 얹었다. 사전에 없는
    텍스트는 KeyError로 fail-loud(테스트가 입력을 정확히 통제하게 강제).
    """

    def __init__(self, fixed: dict[str, tuple[float, ...]]) -> None:
        self._fixed = fixed
        self.embed_calls = 0
        self.embedded_texts = 0

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.embed_calls += 1
        text_list = list(texts)
        self.embedded_texts += len(text_list)
        return tuple(self._fixed[t] for t in text_list)


# 2차원 단위벡터 — cosine이 자명하게 계산되게(L2 정규화 가정 충족).
_V_A = (1.0, 0.0)  # 개념 A 방향
_V_B = (0.0, 1.0)  # 개념 B 방향(A와 직교 → cosine 0)
_V_Q_A = (1.0, 0.0)  # 질의 = A와 완전 일치(cosine 1.0)
_V_MID = (0.6, 0.8)  # A와 cosine 0.6, B와 cosine 0.8


def _doc(concept: Concept) -> str:
    return f"{concept.label} {concept.core_question} {concept.domain}"


class TestEmbeddingAnnMatcher:
    """EmbeddingAnnMatcher — 로컬 임베딩 cosine 매칭(FakeEmbedder 결정론 주입)."""

    def _concepts(self) -> tuple[Concept, Concept]:
        ca = _concept("c_a", "A라벨", "A질문", domain="A도메인")
        cb = _concept("c_b", "B라벨", "B질문", domain="B도메인")
        return ca, cb

    def _fixed_for(
        self, question: str, ca: Concept, cb: Concept, q_vec: tuple[float, ...]
    ) -> dict[str, tuple[float, ...]]:
        return {question: q_vec, _doc(ca): _V_A, _doc(cb): _V_B}

    def test_Protocol_만족(self) -> None:
        matcher: KnowledgeIndexMatcher = EmbeddingAnnMatcher(_CountingEmbedder({}))
        assert hasattr(matcher, "match")

    def test_cosine_후보_산출_최고개념_1건(self) -> None:
        ca, cb = self._concepts()
        q = "질의문"
        emb = _CountingEmbedder(self._fixed_for(q, ca, cb, _V_Q_A))
        m = EmbeddingAnnMatcher(emb, tau=0.5)
        idx = _index("agent_a", ca, cb)
        result = m.match(q, [idx])
        assert len(result) == 1
        assert result[0].agent_id == "agent_a"
        assert result[0].matched_concept_id == "c_a"  # cosine 1.0 > c_b cosine 0
        assert result[0].score == pytest.approx(1.0)

    def test_τ_경계_미만_제외_이상_채택(self) -> None:
        ca, cb = self._concepts()
        q = "질의문"
        # q=_V_MID: c_a cosine 0.6, c_b cosine 0.8.
        emb = _CountingEmbedder(self._fixed_for(q, ca, cb, _V_MID))
        idx = _index("agent_a", ca, cb)
        # τ=0.7 → c_b(0.8)만 채택, c_a(0.6) 제외.
        m_high = EmbeddingAnnMatcher(emb, tau=0.7)
        r_high = m_high.match(q, [idx])
        assert len(r_high) == 1
        assert r_high[0].matched_concept_id == "c_b"
        assert r_high[0].score == pytest.approx(0.8)
        # τ=0.9 → 둘 다 미만 → 0 후보(빈 튜플).
        m_none = EmbeddingAnnMatcher(_CountingEmbedder(self._fixed_for(q, ca, cb, _V_MID)), tau=0.9)
        assert m_none.match(q, [idx]) == ()

    def test_0후보_전부_τ_미만_빈목록(self) -> None:
        ca, cb = self._concepts()
        q = "질의문"
        emb = _CountingEmbedder(self._fixed_for(q, ca, cb, _V_MID))
        m = EmbeddingAnnMatcher(emb, tau=0.95)
        idx = _index("agent_a", ca, cb)
        assert m.match(q, [idx]) == ()

    def test_인덱스측_캐시_적중_재질의_재임베딩_0(self) -> None:
        ca, cb = self._concepts()
        q1, q2 = "질의1", "질의2"
        fixed = {_doc(ca): _V_A, _doc(cb): _V_B, q1: _V_Q_A, q2: _V_B}
        emb = _CountingEmbedder(fixed)
        m = EmbeddingAnnMatcher(emb, tau=0.5)
        idx = _index("agent_a", ca, cb)

        m.match(q1, [idx])
        # 첫 질의: 개념 2개(1콜) + 질의 1개(1콜) = embed 콜 2회·텍스트 3개.
        assert emb.embed_calls == 2
        assert emb.embedded_texts == 3

        m.match(q2, [idx])
        # 재질의: 인덱스 벡터 캐시 적중 → 질의 1개만 추가 임베딩.
        assert emb.embed_calls == 3
        assert emb.embedded_texts == 4  # 3 + 질의1개

    def test_인덱스_갱신_generated_at_변경_재임베딩(self) -> None:
        ca, cb = self._concepts()
        q = "질의1"
        fixed = {_doc(ca): _V_A, _doc(cb): _V_B, q: _V_Q_A}
        emb = _CountingEmbedder(fixed)
        m = EmbeddingAnnMatcher(emb, tau=0.5)

        idx_v1 = KnowledgeIndex(
            agent_id="agent_a", version="v1", generated_at=_NOW, concepts=(ca, cb)
        )
        m.match(q, [idx_v1])
        assert emb.embedded_texts == 3

        # generated_at 변경 → 캐시 키 달라짐 → 개념 재임베딩.
        newer = datetime(2026, 6, 28, tzinfo=timezone.utc)
        idx_v2 = KnowledgeIndex(
            agent_id="agent_a", version="v2", generated_at=newer, concepts=(ca, cb)
        )
        m.match(q, [idx_v2])
        # 질의(캐시 없음·매 호출 임베딩) 2개 + 개념 2 batch 2회 = 6.
        assert emb.embedded_texts == 6

    def test_개념_0개_인덱스_임베딩_0_후보_0(self) -> None:
        q = "질의문"
        emb = _CountingEmbedder({q: _V_Q_A})
        m = EmbeddingAnnMatcher(emb, tau=0.5)
        empty_idx = _index("agent_empty")  # concepts 없음
        result = m.match(q, [empty_idx])
        assert result == ()
        # 질의 1콜만 — 개념 임베딩 0.
        assert emb.embed_calls == 1
        assert emb.embedded_texts == 1

    def test_다중_에이전트_score_내림차순_동점_agent_id_오름차순(self) -> None:
        ca, _ = self._concepts()
        q = "질의문"
        # 같은 개념 c_a를 두 에이전트가 갖게 해 동점(cosine 1.0)을 만든다.
        fixed = {_doc(ca): _V_A, q: _V_Q_A}
        emb = _CountingEmbedder(fixed)
        m = EmbeddingAnnMatcher(emb, tau=0.5)
        idx_b = _index("z_agent", ca)
        idx_a = _index("a_agent", ca)
        result = m.match(q, [idx_b, idx_a])
        # 둘 다 cosine 1.0 동점 → agent_id 오름차순.
        assert [r.agent_id for r in result] == ["a_agent", "z_agent"]

    def test_결정론_같은_입력_같은_출력(self) -> None:
        ca, cb = self._concepts()
        q = "질의문"
        idx = _index("agent_a", ca, cb)
        m1 = EmbeddingAnnMatcher(_CountingEmbedder(self._fixed_for(q, ca, cb, _V_MID)), tau=0.5)
        m2 = EmbeddingAnnMatcher(_CountingEmbedder(self._fixed_for(q, ca, cb, _V_MID)), tau=0.5)
        assert m1.match(q, [idx]) == m2.match(q, [idx])

    def test_기본_τ_상수_노출(self) -> None:
        assert DEFAULT_EMBED_TAU == pytest.approx(0.85)


# ════════════════════════════════════════════════════════════════════════════
# 5. select_matcher — AON_MATCHER env 시임(select_embedder 대칭)
# ════════════════════════════════════════════════════════════════════════════


class TestSelectMatcher:
    """select_matcher env 분기 — 기본 overlap 무변경·embedding 지연 import·미지 SystemExit."""

    def test_미설정_ConceptOverlapMatcher_기본(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AON_MATCHER", raising=False)
        assert isinstance(select_matcher(), ConceptOverlapMatcher)

    def test_overlap_ConceptOverlapMatcher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AON_MATCHER", "overlap")
        assert isinstance(select_matcher(), ConceptOverlapMatcher)

    def test_빈문자열_ConceptOverlapMatcher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AON_MATCHER", "  ")
        assert isinstance(select_matcher(), ConceptOverlapMatcher)

    def test_미지값_SystemExit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AON_MATCHER", "nonsense")
        with pytest.raises(SystemExit):
            select_matcher()

    def test_embedding_지연import_시임(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """embedding 분기가 FastEmbedEmbedder를 지연 import해 EmbeddingAnnMatcher를 만든다.

        실 ONNX 로드를 피하려 provider_embed_fastembed.FastEmbedEmbedder를 스텁으로 갈아끼운다.
        """
        import agent_org_network.provider_embed_fastembed as pef

        class _StubFastEmbed:
            def __init__(self, model_name: str = "") -> None:  # select_matcher가 모델명 전달
                self.model_name = model_name

            def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
                return tuple((1.0, 0.0) for _ in texts)

        monkeypatch.setattr(pef, "FastEmbedEmbedder", _StubFastEmbed)
        monkeypatch.setenv("AON_MATCHER", "embedding")
        m = select_matcher()
        assert isinstance(m, EmbeddingAnnMatcher)

    def test_fastembed_별칭_지연import_시임(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_org_network.provider_embed_fastembed as pef

        class _StubFastEmbed:
            def __init__(self, model_name: str = "") -> None:  # select_matcher가 모델명 전달
                self.model_name = model_name

            def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
                return tuple((1.0, 0.0) for _ in texts)

        monkeypatch.setattr(pef, "FastEmbedEmbedder", _StubFastEmbed)
        monkeypatch.setenv("AON_MATCHER", "fastembed")
        assert isinstance(select_matcher(), EmbeddingAnnMatcher)
