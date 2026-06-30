"""okf_dedup 순수 도메인 테스트 — ADR 0032 §C (red→green→refactor).

Embedder 포트(FakeEmbedder)·DedupCandidate 값 객체·classify_dedup_candidates 순수 함수.
SDK 0·IO 0·결정론. 실 임베딩 모델 호출 없음(fastembed 어댑터는 다음 슬라이스).
"""

from __future__ import annotations

import pytest

from agent_org_network.okf_dedup import (
    DedupCandidate,
    FakeEmbedder,
    classify_dedup_candidates,
)


# ── classify_dedup_candidates ─────────────────────────────────────────────────


def test_동일_벡터는_auto_suggest로_분류된다():
    new_concepts = [("new-1", (1.0, 0.0, 0.0))]
    existing_concepts = [("existing-1", (1.0, 0.0, 0.0))]

    candidates = classify_dedup_candidates(
        new_concepts=new_concepts,
        existing_concepts=existing_concepts,
        tau_high=0.88,
        tau_low=0.70,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.new_concept_id == "new-1"
    assert candidate.existing_concept_id == "existing-1"
    assert candidate.similarity == pytest.approx(1.0)
    assert candidate.grade == "auto_suggest"


def test_중간_유사_벡터는_similar로_분류된다():
    # cosine([1,1],[1,0]) = 1/sqrt(2) ≈ 0.7071 — tau_low(0.70)~tau_high(0.88) 사이.
    new_concepts = [("new-1", (1.0, 1.0))]
    existing_concepts = [("existing-1", (1.0, 0.0))]

    candidates = classify_dedup_candidates(
        new_concepts=new_concepts,
        existing_concepts=existing_concepts,
        tau_high=0.88,
        tau_low=0.70,
    )

    assert len(candidates) == 1
    assert candidates[0].grade == "similar"
    assert candidates[0].similarity == pytest.approx(0.70710678, rel=1e-6)


def test_낮은_유사_벡터는_후보가_생성되지_않는다():
    new_concepts = [("new-1", (1.0, 0.0))]
    existing_concepts = [("existing-1", (0.0, 1.0))]  # 직교 — cosine 0.0

    candidates = classify_dedup_candidates(
        new_concepts=new_concepts,
        existing_concepts=existing_concepts,
        tau_high=0.88,
        tau_low=0.70,
    )

    assert candidates == ()


def test_같은_concept_id_자기쌍은_sim_1_0이어도_후보에서_제외된다():
    new_concepts = [("dup-id", (1.0, 0.0))]
    existing_concepts = [("dup-id", (1.0, 0.0))]

    candidates = classify_dedup_candidates(
        new_concepts=new_concepts,
        existing_concepts=existing_concepts,
        tau_high=0.88,
        tau_low=0.70,
    )

    assert candidates == ()


def test_정렬은_유사도_내림차순_동점은_id_오름차순이다():
    # 두 new × 두 existing 조합, 일부를 동률(similarity) 만들어 오름차순 타이브레이크 검증.
    new_concepts = [
        ("new-b", (1.0, 0.0)),
        ("new-a", (1.0, 0.0)),
    ]
    existing_concepts = [
        ("existing-z", (1.0, 0.0)),  # sim 1.0
        ("existing-a", (1.0, 0.0)),  # sim 1.0 (동률)
    ]

    candidates = classify_dedup_candidates(
        new_concepts=new_concepts,
        existing_concepts=existing_concepts,
        tau_high=0.88,
        tau_low=0.70,
    )

    # 4쌍 모두 sim=1.0(동률) → (new_concept_id, existing_concept_id) 오름차순
    assert [(c.new_concept_id, c.existing_concept_id) for c in candidates] == [
        ("new-a", "existing-a"),
        ("new-a", "existing-z"),
        ("new-b", "existing-a"),
        ("new-b", "existing-z"),
    ]


def test_정렬은_유사도_내림차순이_우선이다():
    new_concepts = [("new-1", (1.0, 0.0))]
    existing_concepts = [
        ("existing-low", (1.0, 1.0)),  # cosine ≈ 0.7071 → similar
        ("existing-high", (1.0, 0.0)),  # cosine 1.0 → auto_suggest
    ]

    candidates = classify_dedup_candidates(
        new_concepts=new_concepts,
        existing_concepts=existing_concepts,
        tau_high=0.88,
        tau_low=0.70,
    )

    assert [c.existing_concept_id for c in candidates] == ["existing-high", "existing-low"]


def test_0벡터는_ValueError():
    new_concepts = [("new-1", (0.0, 0.0))]
    existing_concepts = [("existing-1", (1.0, 0.0))]

    with pytest.raises(ValueError):
        classify_dedup_candidates(
            new_concepts=new_concepts,
            existing_concepts=existing_concepts,
            tau_high=0.88,
            tau_low=0.70,
        )


def test_차원_불일치는_ValueError():
    new_concepts = [("new-1", (1.0, 0.0, 0.0))]
    existing_concepts = [("existing-1", (1.0, 0.0))]

    with pytest.raises(ValueError):
        classify_dedup_candidates(
            new_concepts=new_concepts,
            existing_concepts=existing_concepts,
            tau_high=0.88,
            tau_low=0.70,
        )


def test_new_concepts_빈_입력은_빈_결과():
    candidates = classify_dedup_candidates(
        new_concepts=[],
        existing_concepts=[("existing-1", (1.0, 0.0))],
        tau_high=0.88,
        tau_low=0.70,
    )

    assert candidates == ()


def test_existing_concepts_빈_입력은_빈_결과():
    candidates = classify_dedup_candidates(
        new_concepts=[("new-1", (1.0, 0.0))],
        existing_concepts=[],
        tau_high=0.88,
        tau_low=0.70,
    )

    assert candidates == ()


def test_tau_low가_tau_high보다_크면_similar_구간이_비고_auto_suggest만_남는다():
    new_concepts = [("new-1", (1.0, 1.0))]  # vs (1,0): cosine ≈ 0.7071
    existing_concepts = [("existing-1", (1.0, 0.0))]

    candidates = classify_dedup_candidates(
        new_concepts=new_concepts,
        existing_concepts=existing_concepts,
        tau_high=0.5,
        tau_low=0.9,  # 역전: tau_low > tau_high
    )

    # sim(≈0.7071) >= tau_high(0.5) 이므로 auto_suggest로 분류(거부하지 않음)
    assert len(candidates) == 1
    assert candidates[0].grade == "auto_suggest"


# ── DedupCandidate validator ──────────────────────────────────────────────────


def test_DedupCandidate_similarity_범위_미만_거부():
    with pytest.raises(Exception):
        DedupCandidate(
            new_concept_id="new-1",
            existing_concept_id="existing-1",
            similarity=-0.1,
            grade="similar",
        )


def test_DedupCandidate_similarity_범위_초과_거부():
    with pytest.raises(Exception):
        DedupCandidate(
            new_concept_id="new-1",
            existing_concept_id="existing-1",
            similarity=1.1,
            grade="auto_suggest",
        )


def test_DedupCandidate_new_concept_id_빈_문자열_거부():
    with pytest.raises(Exception):
        DedupCandidate(
            new_concept_id="",
            existing_concept_id="existing-1",
            similarity=0.9,
            grade="auto_suggest",
        )


def test_DedupCandidate_existing_concept_id_빈_문자열_거부():
    with pytest.raises(Exception):
        DedupCandidate(
            new_concept_id="new-1",
            existing_concept_id="",
            similarity=0.9,
            grade="auto_suggest",
        )


def test_DedupCandidate_정상_생성_및_frozen():
    candidate = DedupCandidate(
        new_concept_id="new-1",
        existing_concept_id="existing-1",
        similarity=0.9,
        grade="auto_suggest",
    )
    assert candidate.new_concept_id == "new-1"
    with pytest.raises(Exception):
        candidate.similarity = 0.5  # type: ignore[misc]


# ── FakeEmbedder ───────────────────────────────────────────────────────────────


def test_FakeEmbedder는_주입한_텍스트를_고정_벡터로_돌려준다():
    embedder = FakeEmbedder(
        {
            "환불 정책": (1.0, 0.0),
            "배송 정책": (0.0, 1.0),
        }
    )

    vectors = embedder.embed(["환불 정책", "배송 정책"])

    assert vectors == ((1.0, 0.0), (0.0, 1.0))


def test_FakeEmbedder는_빈_입력에_빈_튜플을_돌려준다():
    embedder = FakeEmbedder({})

    assert embedder.embed([]) == ()


def test_FakeEmbedder는_미지_텍스트에_KeyError():
    embedder = FakeEmbedder({"알려진 텍스트": (1.0, 0.0)})

    with pytest.raises(KeyError):
        embedder.embed(["알려지지 않은 텍스트"])
