"""EmbeddingConfidenceAssessor (T10.5·ADR 0028 §17) red→green 테스트.

stage-2 실 어댑터 — 후보 카드의 okf_root/{agent_id}/*.md 개념 *body 전문*을
문서측으로, 질문을 질의측으로 임베딩해 최고 cosine을 confidence로 반환한다.

FakeEmbedder(고정 텍스트→벡터 dict)로 결정론 주입 — 실 fastembed 무접촉(게이트 내).

불변식 확인 대상:
  - 카드 body 최고 cosine 반환(항등 매핑).
  - 개념 0개 카드 → 저신뢰(confidence=0.0, embed 미호출).
  - (agent_id, generated_at) 캐시 적중 시 재질의해도 body 재임베딩 0.
  - 인덱스 갱신(다른 generated_at) → 캐시 무효화·재임베딩.
  - TwoStageRouter 통합: 자동해소/저신뢰 전부 Contested/격차 미달 Contested.
  - stage-1.5(δ)와 공존: 1.5 발동 시 stage-2 미호출, 미발동 시 2가 받음.
  - assess 예외 시 Contested 폴백(저신뢰 흡수).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.confidence_assessor import EmbeddingConfidenceAssessor
from agent_org_network.decision import Contested, Routed
from agent_org_network.index_matcher import FakeMatcher, IndexMatch
from agent_org_network.registry import Registry
from agent_org_network.two_stage_router import (
    GroundedConfidence,
    InMemoryPublishedIndexStore,
)
from agent_org_network.two_stage_router import TwoStageRouter

_NOW = datetime(2026, 7, 2, tzinfo=timezone.utc)
_LATER = datetime(2026, 7, 3, tzinfo=timezone.utc)
_REVIEWED = date(2026, 7, 2)

# 2차원 단위벡터 — cosine 자명 계산(L2 정규화 가정 충족).
_V_A = (1.0, 0.0)
_V_B = (0.0, 1.0)
_V_Q_A = (1.0, 0.0)  # A와 완전 일치(cosine 1.0)
_V_MID = (0.6, 0.8)  # A cosine 0.6, B cosine 0.8


class _CountingEmbedder:
    """Embedder 테스트 더블 — 고정 텍스트→벡터 dict + 호출 계측(EmbeddingAnnMatcher 대칭)."""

    def __init__(self, fixed: dict[str, tuple[float, ...]]) -> None:
        self._fixed = fixed
        self.embed_calls = 0
        self.embedded_texts = 0

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.embed_calls += 1
        text_list = list(texts)
        self.embedded_texts += len(text_list)
        return tuple(self._fixed[t] for t in text_list)


class _RaisingEmbedder:
    """embed 호출 시 항상 예외를 던지는 테스트 더블 — assess 예외 폴백 검증용."""

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        raise RuntimeError("임베딩 실패(테스트 주입)")


def _card(
    agent_id: str,
    owner: str = "alice",
    domains: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary=f"{agent_id} 요약",
        domains=domains or [],
        last_reviewed_at=_REVIEWED,
        cannot_answer=[],
        approval_when=[],
        collaborate_when=[],
    )


def _write_concept_doc(
    okf_root: Path,
    agent_id: str,
    filename: str,
    *,
    title: str,
    description: str,
    tags: list[str],
    body: str,
) -> None:
    agent_dir = okf_root / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    tags_yaml = "[" + ", ".join(tags) + "]"
    (agent_dir / filename).write_text(
        f"""---
type: concept
title: {title}
description: {description}
tags: {tags_yaml}
---

{body}
""",
        encoding="utf-8",
    )


def _registry(*cards: AgentCard) -> Registry:
    reg = Registry()
    for card in cards:
        reg.register(card)
    return reg


def _match(agent_id: str, score: float, concept_id: str) -> IndexMatch:
    return IndexMatch(agent_id=agent_id, score=score, matched_concept_id=concept_id)


# ════════════════════════════════════════════════════════════════════════════
# 1. Protocol 만족 + body 최고 cosine → confidence(항등 매핑)
# ════════════════════════════════════════════════════════════════════════════


class TestAssessBodyMaxCosine:
    def test_Protocol_만족(self, tmp_path: Path) -> None:
        from agent_org_network.two_stage_router import ConfidenceAssessor

        assessor: ConfidenceAssessor = EmbeddingConfidenceAssessor(
            _CountingEmbedder({}), tmp_path
        )
        assert hasattr(assessor, "assess")

    def test_카드_개념_body_최고_cosine_반환(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 설명 A",
            tags=["환불"],
            body="바디 A 본문",
        )
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "b.md",
            title="환불 B",
            description="환불 설명 B",
            tags=["환불"],
            body="바디 B 본문",
        )
        q = "질문입니다"
        emb = _CountingEmbedder(
            {"바디 A 본문": _V_A, "바디 B 본문": _V_B, q: _V_Q_A}
        )
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path)
        gc = assessor.assess(q, card)
        assert isinstance(gc, GroundedConfidence)
        assert gc.agent_id == "cs_ops"
        # A cosine 1.0 > B cosine 0 → 최고 cosine 반환(항등 매핑).
        assert gc.confidence == pytest.approx(1.0)

    def test_min_confidence_이상은_항등_그대로_반환(self, tmp_path: Path) -> None:
        """cosine이 min_confidence 이상이면 항등(스케일 보정 없음)."""
        card = _card("cs_ops", domains=["환불"])
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 설명 A",
            tags=["환불"],
            body="바디 A",
        )
        q = "질문"
        emb = _CountingEmbedder({"바디 A": _V_MID, q: _V_A})
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path, min_confidence=0.5)
        gc = assessor.assess(q, card)
        assert gc.confidence == pytest.approx(0.6)

    def test_min_confidence_미만_저신뢰_0으로_클램프(self, tmp_path: Path) -> None:
        """cosine이 min_confidence 미만이면 저신뢰(0.0)로 취급한다(ADR 0028 §17 결정 C·D).

        어댑터가 흡수(라우터 무수정) — min_confidence 미만인 confidence는 격차 판정에서
        낮은 값이 되어 자연히 Contested로 낙하하게 된다.
        """
        card = _card("cs_ops", domains=["환불"])
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 설명 A",
            tags=["환불"],
            body="바디 A",
        )
        q = "질문"
        emb = _CountingEmbedder({"바디 A": _V_MID, q: _V_A})
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path, min_confidence=0.9)
        gc = assessor.assess(q, card)
        assert gc.confidence == 0.0


# ════════════════════════════════════════════════════════════════════════════
# 2. 개념 0 카드 → 저신뢰(0.0)
# ════════════════════════════════════════════════════════════════════════════


class TestNoConceptsLowConfidence:
    def test_okf_디렉터리_없음_저신뢰_0(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        emb = _CountingEmbedder({"질문": _V_Q_A})
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path)
        gc = assessor.assess("질문", card)
        assert gc.confidence == 0.0
        # 개념 0 → body 임베딩 호출 0(질의 임베딩도 스킵 — 비교 대상 없음).
        assert emb.embed_calls == 0

    def test_md_파일_없음_저신뢰_0(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        (tmp_path / "cs_ops").mkdir(parents=True)
        emb = _CountingEmbedder({"질문": _V_Q_A})
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path)
        gc = assessor.assess("질문", card)
        assert gc.confidence == 0.0


# ════════════════════════════════════════════════════════════════════════════
# 3. (agent_id, generated_at) 캐시 — 재질의 시 body 재임베딩 0 / 무효화
# ════════════════════════════════════════════════════════════════════════════


class TestBodyVectorCache:
    def test_캐시_적중_재질의_body_재임베딩_0(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 설명 A",
            tags=["환불"],
            body="바디 A",
        )
        q1, q2 = "질문1", "질문2"
        emb = _CountingEmbedder({"바디 A": _V_A, q1: _V_Q_A, q2: _V_B})
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path)

        assessor.assess(q1, card)
        # 첫 assess: body 1개(1콜) + 질의 1개(1콜) = 2콜·텍스트 2개.
        assert emb.embed_calls == 2
        assert emb.embedded_texts == 2

        assessor.assess(q2, card)
        # 재질의: body 벡터 캐시 적중 → 질의 1개만 추가 임베딩.
        assert emb.embed_calls == 3
        assert emb.embedded_texts == 3

    def test_mtime_변경_캐시_무효화_재임베딩(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 설명 A",
            tags=["환불"],
            body="바디 A",
        )
        q = "질문"
        emb = _CountingEmbedder({"바디 A": _V_A, "바디 A2": _V_B, q: _V_Q_A})
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path)
        assessor.assess(q, card)
        assert emb.embedded_texts == 2  # body 1 + 질의 1

        # 파일 내용 변경(mtime 갱신) → 캐시 키 무효화 → body 재임베딩.
        import os
        import time

        time.sleep(0.01)
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 설명 A",
            tags=["환불"],
            body="바디 A2",
        )
        # mtime이 확실히 바뀌도록 강제 갱신(파일시스템 해상도 방어).
        newer = os.stat(tmp_path / "cs_ops" / "a.md").st_mtime + 1
        os.utime(tmp_path / "cs_ops" / "a.md", (newer, newer))

        assessor.assess(q, card)
        # body 재임베딩(1) + 질의(1) 추가 — 누적 4.
        assert emb.embedded_texts == 4


# ════════════════════════════════════════════════════════════════════════════
# 4. TwoStageRouter 통합 — 자동해소/Contested
# ════════════════════════════════════════════════════════════════════════════


class TestTwoStageRouterIntegration:
    def _setup(
        self, tmp_path: Path
    ) -> tuple[Registry, InMemoryPublishedIndexStore, FakeMatcher]:
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 A 설명",
            tags=["환불"],
            body="전자상거래법 제17조 청약철회 통신판매업자",
        )
        _write_concept_doc(
            tmp_path,
            "finance_ops",
            "b.md",
            title="환불 B",
            description="환불 B 설명",
            tags=["환불"],
            body="약관규제법 제6조 불공정약관 무효 면책조항",
        )
        reg = _registry(card_a, card_b)
        from agent_org_network.knowledge_index import Concept, KnowledgeIndex

        idx_a = KnowledgeIndex(
            agent_id="cs_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="a", label="환불 A", core_question="환불?", domain="환불"),),
        )
        idx_b = KnowledgeIndex(
            agent_id="finance_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="b", label="환불 B", core_question="환불?", domain="환불"),),
        )
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        matcher = FakeMatcher(
            (_match("cs_ops", 1.0, "a"), _match("finance_ops", 1.0, "b"))
        )
        return reg, store, matcher

    def test_자동해소_최고_차순위_격차_충분_Routed(self, tmp_path: Path) -> None:
        reg, store, matcher = self._setup(tmp_path)
        q = "청약철회 하고 싶어요"
        emb = _CountingEmbedder(
            {
                "전자상거래법 제17조 청약철회 통신판매업자": _V_A,
                "약관규제법 제6조 불공정약관 무효 면책조항": _V_B,
                q: _V_A,
            }
        )
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path)
        router = TwoStageRouter(
            reg, matcher, store, "root", assessor=assessor, clear_winner_margin=0.5
        )
        result = router.route(q)
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "cs_ops"

    def test_저신뢰_전부_Contested(self, tmp_path: Path) -> None:
        """개념 0 카드 2건(둘 다 confidence=0.0) → 동점 → Contested."""
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        reg = _registry(card_a, card_b)
        from agent_org_network.knowledge_index import Concept, KnowledgeIndex

        idx_a = KnowledgeIndex(
            agent_id="cs_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="a", label="환불 A", core_question="환불?", domain="환불"),),
        )
        idx_b = KnowledgeIndex(
            agent_id="finance_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="b", label="환불 B", core_question="환불?", domain="환불"),),
        )
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        matcher = FakeMatcher(
            (_match("cs_ops", 1.0, "a"), _match("finance_ops", 1.0, "b"))
        )
        # okf_root에 개념 문서를 두지 않음 → 둘 다 confidence 0.0.
        emb = _CountingEmbedder({"환불 질문": _V_A})
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path)
        router = TwoStageRouter(
            reg, matcher, store, "root", assessor=assessor, clear_winner_margin=0.1
        )
        result = router.route("환불 질문")
        assert isinstance(result, Contested)

    def test_격차_미달_Contested(self, tmp_path: Path) -> None:
        reg, store, matcher = self._setup(tmp_path)
        q = "약간 애매한 질문"
        emb = _CountingEmbedder(
            {
                "전자상거래법 제17조 청약철회 통신판매업자": _V_A,
                "약관규제법 제6조 불공정약관 무효 면책조항": _V_B,
                q: _V_MID,  # A cosine 0.6, B cosine 0.8 — 격차 0.2
            }
        )
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path)
        router = TwoStageRouter(
            reg, matcher, store, "root", assessor=assessor, clear_winner_margin=0.5
        )
        result = router.route(q)
        assert isinstance(result, Contested)


# ════════════════════════════════════════════════════════════════════════════
# 5. stage-1.5(δ)와 공존 — 1.5 먼저·미발동 시 2가 받음
# ════════════════════════════════════════════════════════════════════════════


class TestStage1Point5Coexistence:
    def test_stage1_5_발동시_stage2_미호출(self, tmp_path: Path) -> None:
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        reg = _registry(card_a, card_b)
        from agent_org_network.knowledge_index import Concept, KnowledgeIndex

        idx_a = KnowledgeIndex(
            agent_id="cs_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="a", label="환불 A", core_question="환불?", domain="환불"),),
        )
        idx_b = KnowledgeIndex(
            agent_id="finance_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="b", label="환불 B", core_question="환불?", domain="환불"),),
        )
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        # top1-top2 = 1.0 (margin 큼) → stage-1.5(δ=0.5) 발동 → assessor 미호출.
        matcher = FakeMatcher(
            (_match("cs_ops", 2.0, "a"), _match("finance_ops", 1.0, "b"))
        )

        class _TrackingAssessor:
            def __init__(self) -> None:
                self.called = False

            def assess(self, question: str, card: AgentCard) -> GroundedConfidence:
                self.called = True
                return GroundedConfidence(agent_id=card.agent_id, confidence=1.0)

        tracker = _TrackingAssessor()
        router = TwoStageRouter(
            reg,
            matcher,
            store,
            "root",
            assessor=tracker,  # type: ignore[arg-type]
            clear_winner_margin=0.0,
            stage1_clear_winner_margin=0.5,
        )
        result = router.route("환불 질문")
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "cs_ops"
        assert tracker.called is False

    def test_stage1_5_미발동시_stage2가_받음(self, tmp_path: Path) -> None:
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        reg = _registry(card_a, card_b)
        from agent_org_network.knowledge_index import Concept, KnowledgeIndex

        idx_a = KnowledgeIndex(
            agent_id="cs_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="a", label="환불 A", core_question="환불?", domain="환불"),),
        )
        idx_b = KnowledgeIndex(
            agent_id="finance_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="b", label="환불 B", core_question="환불?", domain="환불"),),
        )
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        # top1-top2 margin 작음(0.1) → δ=0.5 미달 → stage-2로 낙하.
        matcher = FakeMatcher(
            (_match("cs_ops", 1.1, "a"), _match("finance_ops", 1.0, "b"))
        )
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 A 설명",
            tags=["환불"],
            body="바디 A",
        )
        _write_concept_doc(
            tmp_path,
            "finance_ops",
            "b.md",
            title="환불 B",
            description="환불 B 설명",
            tags=["환불"],
            body="바디 B",
        )
        q = "환불 질문"
        emb = _CountingEmbedder({"바디 A": _V_A, "바디 B": _V_B, q: _V_Q_A})
        assessor = EmbeddingConfidenceAssessor(emb, tmp_path)
        router = TwoStageRouter(
            reg,
            matcher,
            store,
            "root",
            assessor=assessor,
            clear_winner_margin=0.5,
            stage1_clear_winner_margin=0.5,
        )
        result = router.route(q)
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "cs_ops"


# ════════════════════════════════════════════════════════════════════════════
# 6. assessor 예외 시 Contested 폴백(저신뢰 흡수)
# ════════════════════════════════════════════════════════════════════════════


class TestExceptionFallback:
    def test_embed_예외시_저신뢰_흡수_Contested(self, tmp_path: Path) -> None:
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        reg = _registry(card_a, card_b)
        from agent_org_network.knowledge_index import Concept, KnowledgeIndex

        idx_a = KnowledgeIndex(
            agent_id="cs_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="a", label="환불 A", core_question="환불?", domain="환불"),),
        )
        idx_b = KnowledgeIndex(
            agent_id="finance_ops",
            version="v1",
            generated_at=_NOW,
            concepts=(Concept(id="b", label="환불 B", core_question="환불?", domain="환불"),),
        )
        store = InMemoryPublishedIndexStore([idx_a, idx_b])
        matcher = FakeMatcher(
            (_match("cs_ops", 1.0, "a"), _match("finance_ops", 1.0, "b"))
        )
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 A 설명",
            tags=["환불"],
            body="바디 A",
        )
        _write_concept_doc(
            tmp_path,
            "finance_ops",
            "b.md",
            title="환불 B",
            description="환불 B 설명",
            tags=["환불"],
            body="바디 B",
        )
        assessor = EmbeddingConfidenceAssessor(_RaisingEmbedder(), tmp_path)
        router = TwoStageRouter(
            reg, matcher, store, "root", assessor=assessor, clear_winner_margin=0.1
        )
        # 예외를 라우터로 던지지 않고 저신뢰 GroundedConfidence로 흡수 → 동점 → Contested.
        result = router.route("환불 질문")
        assert isinstance(result, Contested)

    def test_assess_직접호출시_예외_던지지_않고_저신뢰_반환(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_concept_doc(
            tmp_path,
            "cs_ops",
            "a.md",
            title="환불 A",
            description="환불 A 설명",
            tags=["환불"],
            body="바디 A",
        )
        assessor = EmbeddingConfidenceAssessor(_RaisingEmbedder(), tmp_path)
        gc = assessor.assess("환불 질문", card)
        assert gc.confidence == 0.0
