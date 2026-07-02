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


# ════════════════════════════════════════════════════════════════════════════
# 7. LlmConfidenceAssessor — 프롬프트 빌드(순수)
# ════════════════════════════════════════════════════════════════════════════

from agent_org_network.confidence_assessor import (  # noqa: E402
    AssessConcept,
    LlmConfidenceAssessor,
    build_assess_request,
    parse_assess_response,
    select_assessor,
)


class TestBuildAssessRequest:
    def test_core_question_전부_실린다(self) -> None:
        concepts = [
            AssessConcept(core_question="Q1?", body="body1"),
            AssessConcept(core_question="Q2?", body="body2"),
            AssessConcept(core_question="Q3?", body="body3"),
        ]
        req = build_assess_request("질문입니다", concepts, model="m")
        user = req.messages[0]["content"]
        assert "Q1?" in user and "Q2?" in user and "Q3?" in user
        assert "질문입니다" in user
        assert req.model == "m"
        # system이 JSON 강제·코드펜스 금지.
        assert "JSON" in req.system
        assert "코드펜스" in req.system

    def test_body_발췌_상한_잘림(self) -> None:
        long_body = "가" * 5000
        concepts = [AssessConcept(core_question="Q?", body=long_body)]
        req = build_assess_request("질문", concepts, model="m", max_body_chars=100)
        user = req.messages[0]["content"]
        # body는 100자로 잘려 실린다 — core_question은 전부.
        assert user.count("가") == 100
        assert "Q?" in user

    def test_개념_0이면_안내_문구(self) -> None:
        req = build_assess_request("질문", [], model="m")
        assert "질문" in req.messages[0]["content"]


# ════════════════════════════════════════════════════════════════════════════
# 8. parse_assess_response — 4케이스(정상·코드펜스·깨진 JSON·범위 밖)
# ════════════════════════════════════════════════════════════════════════════


class TestParseAssessResponse:
    def test_정상_JSON(self) -> None:
        conf, grounding = parse_assess_response(
            '{"confidence": 0.87, "grounding": "제17조 일치"}'
        )
        assert conf == pytest.approx(0.87)
        assert grounding == "제17조 일치"

    def test_코드펜스_감싼_JSON(self) -> None:
        conf, grounding = parse_assess_response(
            '```json\n{"confidence": 0.5, "grounding": "메모"}\n```'
        )
        assert conf == pytest.approx(0.5)
        assert grounding == "메모"

    def test_깨진_JSON_0으로_흡수(self) -> None:
        conf, grounding = parse_assess_response("이건 JSON이 아니라 산문입니다")
        assert conf == 0.0
        assert grounding == ""

    def test_범위_밖_confidence_클램프(self) -> None:
        assert parse_assess_response('{"confidence": 1.5}')[0] == 1.0
        assert parse_assess_response('{"confidence": -0.3}')[0] == 0.0

    def test_confidence_필드_부재_0(self) -> None:
        assert parse_assess_response('{"grounding": "메모"}')[0] == 0.0

    def test_confidence_숫자_아님_0(self) -> None:
        assert parse_assess_response('{"confidence": "높음"}')[0] == 0.0

    def test_객체_아닌_배열_0(self) -> None:
        assert parse_assess_response("[1, 2, 3]") == (0.0, "")


# ════════════════════════════════════════════════════════════════════════════
# 9. LlmConfidenceAssessor.assess — StubProviderTransport 주입
# ════════════════════════════════════════════════════════════════════════════


def _write_okf_with_core_question(
    okf_root: Path, agent_id: str, filename: str, *, core_question: str, body: str
) -> None:
    agent_dir = okf_root / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / filename).write_text(
        f"""---
type: concept
title: {agent_id} 개념
core_question: {core_question}
---

{body}
""",
        encoding="utf-8",
    )


class _CountingTransport:
    """ProviderTransport 테스트 더블 — 고정 JSON 청크 반환 + 호출 계측."""

    def __init__(self, chunks: tuple[str, ...]) -> None:
        self._chunks = chunks
        self.calls = 0

    def __call__(self, request: object) -> tuple[str, ...]:
        self.calls += 1
        return self._chunks


class TestLlmAssessWithStubTransport:
    def test_stub_JSON_confidence_반환(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_okf_with_core_question(
            tmp_path, "cs_ops", "a.md", core_question="환불 되나요?", body="본문"
        )
        transport = _CountingTransport(('{"confidence": 0.9, "grounding": "일치"}',))
        assessor = LlmConfidenceAssessor(transport, tmp_path)  # type: ignore[arg-type]
        gc = assessor.assess("환불 문의", card)
        assert isinstance(gc, GroundedConfidence)
        assert gc.confidence == pytest.approx(0.9)
        assert gc.grounding == "일치"
        assert transport.calls == 1

    def test_개념_0_transport_미호출_저신뢰(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        transport = _CountingTransport(('{"confidence": 0.9}',))
        assessor = LlmConfidenceAssessor(transport, tmp_path)  # type: ignore[arg-type]
        gc = assessor.assess("질문", card)
        assert gc.confidence == 0.0
        assert transport.calls == 0

    def test_깨진_응답_저신뢰_흡수(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_okf_with_core_question(
            tmp_path, "cs_ops", "a.md", core_question="Q?", body="본문"
        )
        transport = _CountingTransport(("산문 응답",))
        assessor = LlmConfidenceAssessor(transport, tmp_path)  # type: ignore[arg-type]
        gc = assessor.assess("질문", card)
        assert gc.confidence == 0.0

    def test_min_confidence_미만_0으로_낙하(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_okf_with_core_question(
            tmp_path, "cs_ops", "a.md", core_question="Q?", body="본문"
        )
        transport = _CountingTransport(('{"confidence": 0.4}',))
        assessor = LlmConfidenceAssessor(
            transport,  # type: ignore[arg-type]
            tmp_path,
            min_confidence=0.5,
        )
        gc = assessor.assess("질문", card)
        assert gc.confidence == 0.0

    def test_transport_예외_저신뢰_흡수(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_okf_with_core_question(
            tmp_path, "cs_ops", "a.md", core_question="Q?", body="본문"
        )

        def _raising(request: object) -> tuple[str, ...]:
            raise RuntimeError("transport 실패")

        assessor = LlmConfidenceAssessor(_raising, tmp_path)  # type: ignore[arg-type]
        gc = assessor.assess("질문", card)
        assert gc.confidence == 0.0


# ════════════════════════════════════════════════════════════════════════════
# 10. LlmConfidenceAssessor 캐시 — 같은 (question, card) 재호출 절감
# ════════════════════════════════════════════════════════════════════════════


class TestLlmAssessCache:
    def test_같은_질문_같은_카드_캐시_적중_호출_1회(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_okf_with_core_question(
            tmp_path, "cs_ops", "a.md", core_question="Q?", body="본문"
        )
        transport = _CountingTransport(('{"confidence": 0.8}',))
        assessor = LlmConfidenceAssessor(transport, tmp_path)  # type: ignore[arg-type]
        assessor.assess("같은 질문", card)
        assessor.assess("같은 질문", card)
        assert transport.calls == 1

    def test_다른_질문_캐시_미스_재호출(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_okf_with_core_question(
            tmp_path, "cs_ops", "a.md", core_question="Q?", body="본문"
        )
        transport = _CountingTransport(('{"confidence": 0.8}',))
        assessor = LlmConfidenceAssessor(transport, tmp_path)  # type: ignore[arg-type]
        assessor.assess("질문 1", card)
        assessor.assess("질문 2", card)
        assert transport.calls == 2

    def test_캐시_비활성화시_매번_호출(self, tmp_path: Path) -> None:
        card = _card("cs_ops", domains=["환불"])
        _write_okf_with_core_question(
            tmp_path, "cs_ops", "a.md", core_question="Q?", body="본문"
        )
        transport = _CountingTransport(('{"confidence": 0.8}',))
        assessor = LlmConfidenceAssessor(
            transport,  # type: ignore[arg-type]
            tmp_path,
            cache=False,
        )
        assessor.assess("같은 질문", card)
        assessor.assess("같은 질문", card)
        assert transport.calls == 2


# ════════════════════════════════════════════════════════════════════════════
# 11. LlmConfidenceAssessor — TwoStageRouter 통합(자동해소/Contested)
# ════════════════════════════════════════════════════════════════════════════


class TestLlmRouterIntegration:
    def _setup(
        self, tmp_path: Path
    ) -> tuple[Registry, InMemoryPublishedIndexStore, FakeMatcher]:
        card_a = _card("cs_ops", domains=["환불"])
        card_b = _card("finance_ops", domains=["환불"])
        _write_okf_with_core_question(
            tmp_path, "cs_ops", "a.md", core_question="환불?", body="전자상거래법"
        )
        _write_okf_with_core_question(
            tmp_path, "finance_ops", "b.md", core_question="환불?", body="약관규제법"
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

    def test_자동해소_격차_충분_Routed(self, tmp_path: Path) -> None:
        reg, store, matcher = self._setup(tmp_path)

        class _PerCardTransport:
            def __call__(self, request: object) -> tuple[str, ...]:
                content = request.messages[0]["content"]  # type: ignore[attr-defined]
                if "전자상거래법" in content:
                    return ('{"confidence": 0.95}',)
                return ('{"confidence": 0.2}',)

        assessor = LlmConfidenceAssessor(_PerCardTransport(), tmp_path)  # type: ignore[arg-type]
        router = TwoStageRouter(
            reg, matcher, store, "root", assessor=assessor, clear_winner_margin=0.5
        )
        result = router.route("청약철회")
        assert isinstance(result, Routed)
        assert result.primary.agent_id == "cs_ops"

    def test_저신뢰_전부_깨진_JSON_Contested(self, tmp_path: Path) -> None:
        reg, store, matcher = self._setup(tmp_path)
        transport = _CountingTransport(("산문",))  # 둘 다 0.0
        assessor = LlmConfidenceAssessor(transport, tmp_path)  # type: ignore[arg-type]
        router = TwoStageRouter(
            reg, matcher, store, "root", assessor=assessor, clear_winner_margin=0.1
        )
        result = router.route("환불 질문")
        assert isinstance(result, Contested)


# ════════════════════════════════════════════════════════════════════════════
# 12. select_assessor 시임 — 5분기(auto/embedding/llm/off/미지)
# ════════════════════════════════════════════════════════════════════════════


class TestSelectAssessor:
    def test_auto_embedding_매처면_EmbeddingConfidenceAssessor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AON_ASSESSOR", raising=False)
        from agent_org_network.index_matcher import EmbeddingAnnMatcher

        # EmbeddingAnnMatcher 인스턴스 필요(isinstance) — 실 임베더 없이 더블 embedder 주입.
        matcher = EmbeddingAnnMatcher(_CountingEmbedder({}))  # type: ignore[arg-type]
        assessor = select_assessor(matcher, tmp_path)
        assert isinstance(assessor, EmbeddingConfidenceAssessor)

    def test_auto_overlap_매처면_None(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AON_ASSESSOR", raising=False)
        from agent_org_network.index_matcher import ConceptOverlapMatcher

        assert select_assessor(ConceptOverlapMatcher(), tmp_path) is None

    def test_embedding_명시_임베더_공유(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AON_ASSESSOR", "embedding")
        from agent_org_network.index_matcher import EmbeddingAnnMatcher

        matcher = EmbeddingAnnMatcher(_CountingEmbedder({}))  # type: ignore[arg-type]
        assessor = select_assessor(matcher, tmp_path)
        assert isinstance(assessor, EmbeddingConfidenceAssessor)

    def test_embedding_명시_overlap_매처면_SystemExit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AON_ASSESSOR", "embedding")
        from agent_org_network.index_matcher import ConceptOverlapMatcher

        with pytest.raises(SystemExit):
            select_assessor(ConceptOverlapMatcher(), tmp_path)

    def test_llm_명시_LlmConfidenceAssessor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AON_ASSESSOR", "llm")
        # claude-code transport는 지연 import·runner 미호출(생성만) — subprocess 무접촉.
        monkeypatch.setenv("AON_ASSESSOR_PROVIDER", "claude-code")
        from agent_org_network.index_matcher import ConceptOverlapMatcher

        assessor = select_assessor(ConceptOverlapMatcher(), tmp_path)
        assert isinstance(assessor, LlmConfidenceAssessor)

    def test_off_명시_None(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AON_ASSESSOR", "off")
        from agent_org_network.index_matcher import EmbeddingAnnMatcher

        matcher = EmbeddingAnnMatcher(_CountingEmbedder({}))  # type: ignore[arg-type]
        assert select_assessor(matcher, tmp_path) is None

    def test_미지값_SystemExit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AON_ASSESSOR", "bogus")
        from agent_org_network.index_matcher import ConceptOverlapMatcher

        with pytest.raises(SystemExit):
            select_assessor(ConceptOverlapMatcher(), tmp_path)


# ════════════════════════════════════════════════════════════════════════════
# 13. demo select_router 기본 경로 무회귀 — AON_ASSESSOR 미설정 시 현 배선 보존
# ════════════════════════════════════════════════════════════════════════════


class TestDemoDefaultPathNoRegression:
    def test_overlap_기본_assessor_None(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AON_MATCHER·AON_ASSESSOR 미설정 → index 라우터가 overlap 매처·assessor None."""
        monkeypatch.delenv("AON_MATCHER", raising=False)
        monkeypatch.delenv("AON_ASSESSOR", raising=False)
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.conflict import InMemoryPrecedentStore
        from agent_org_network.demo import select_router
        from agent_org_network.two_stage_router import TwoStageRouter

        reg = _registry(_card("cs_ops", domains=["환불"]))
        router = select_router(
            "index", reg, FakeClassifier("환불"), InMemoryPrecedentStore()
        )
        assert isinstance(router, TwoStageRouter)
        # 기본 overlap 경로는 assessor 미장착(현 배선 보존).
        assert router._assessor is None  # type: ignore[attr-defined]
