"""near-dup 분류 도메인 — Embedder 포트 + DedupCandidate + classify_dedup_candidates (ADR 0032 §C).

순수(IO 0·SDK 0)·`pydantic`만 임포트. `okf_authoring`을 역참조하지 않는다(입력은 이미
추출된 벡터와 식별자뿐 — `OkfDocumentDraft` 결합 0).

불변식:
  - 중앙 비소유: 이 모듈은 어떤 중앙 store도 모른다 — 호출자(라우트)가 owner측 텍스트를
    읽어 임베딩한 벡터만 받는다.
  - 결정론: 같은 벡터+임계 → 항상 같은 후보·같은 순서.
  - fail-loud: 0벡터·차원 불일치는 ValueError.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, field_validator


class Embedder(Protocol):
    """텍스트 → 임베딩 벡터 포트 — Classifier·KnowledgeIndexMatcher·OkfAuthor 포트 동일 패턴.

    구현: FakeEmbedder(테스트·고정 벡터 주입)·FastEmbedEmbedder(실·게이트 밖).
    벡터는 L2 정규화돼 있다고 *가정*(어댑터 책임) — 그래야 dot product가 곧 cosine.
    e5 prefix("query: ")·풀링·정규화는 실 어댑터 내부 책임이고, 이 포트 계약은 그걸 모른다.
    """

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """texts 각 원소를 임베딩 벡터로 변환. 입력 순서 보존·같은 차원.
        빈 시퀀스는 빈 튜플 반환. 차원 일관성(모든 벡터 같은 길이)은 구현 보장."""
        ...


class DedupCandidate(BaseModel, frozen=True):
    """near-dup 후보 한 쌍 — owner 처분(C3) 입력. 중앙 미도달(owner측 transient).

    new_concept_id: 이번 /author/run으로 새로 추출된(아직 미게시) 개념 id.
    existing_concept_id: 이미 게시된 라이브러리의 기존 개념 id.
    similarity: cosine 유사도(0.0~1.0). 범위 밖 거부.
    grade: 'auto_suggest'(sim≥τ_high·자동 병합 후보 제안)
           | 'similar'(τ_low≤sim<τ_high·"비슷한 개념" 표시만). C3 두 등급.
    """

    new_concept_id: str
    existing_concept_id: str
    similarity: float
    grade: Literal["auto_suggest", "similar"]

    @field_validator("new_concept_id", "existing_concept_id")
    @classmethod
    def _validate_ids(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("DedupCandidate concept_id는 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("similarity")
    @classmethod
    def _validate_similarity(cls, value: float) -> float:
        if not (0.0 <= value <= 1.0):
            raise ValueError(f"similarity는 0.0~1.0 범위여야 합니다: {value}")
        return value


def _cosine_similarity(u: tuple[float, ...], v: tuple[float, ...]) -> float:
    """cosine(u, v) = dot(u, v) / (||u|| * ||v||). 0벡터·차원 불일치는 ValueError(fail-loud)."""
    if len(u) != len(v):
        raise ValueError(f"벡터 차원 불일치: {len(u)} != {len(v)}")
    norm_u = math.sqrt(sum(x * x for x in u))
    norm_v = math.sqrt(sum(x * x for x in v))
    if norm_u == 0.0 or norm_v == 0.0:
        raise ValueError("0벡터의 cosine 유사도는 정의되지 않습니다.")
    dot = sum(x * y for x, y in zip(u, v))
    return dot / (norm_u * norm_v)


def classify_dedup_candidates(
    new_concepts: Sequence[tuple[str, tuple[float, ...]]],
    existing_concepts: Sequence[tuple[str, tuple[float, ...]]],
    *,
    tau_high: float,
    tau_low: float,
) -> tuple[DedupCandidate, ...]:
    """신규 추출분 × 기존 게시 라이브러리의 pairwise cosine으로 near-dup 후보 분류.

    순수·IO 0·결정론. *임베딩을 직접 계산하지 않는다* — 호출자가 Embedder.embed로 미리
    벡터를 낸 뒤 (concept_id, vector) 쌍으로 넘긴다. 이 함수는 cosine 계산·임계 분류만.

    입력 (concept_id, vector): new는 이번 /author/run 미게시 개념, existing은 게시 라이브러리.
    cosine(u, v) = dot(u, v) / (||u|| * ||v||). 0벡터·차원 불일치는 ValueError(fail-loud).
    같은 concept_id(new와 existing이 우연히 같은 id)는 자기쌍이라 후보에서 제외한다
    (결정 B2 결정론 키로 이미 같은 파일=멱등 덮어쓰기 경로라 dedup 대상 아님).

    분류:
      sim >= tau_high → DedupCandidate(grade='auto_suggest')
      tau_low <= sim < tau_high → DedupCandidate(grade='similar')
      sim < tau_low → 후보 아님(객체 생성 안 함).

    결과 정렬: similarity 내림차순, 동점은 (new_concept_id, existing_concept_id) 오름차순
    (결정론 — KnowledgeIndexMatcher.match 정렬 규약과 같은 결).

    tau_high·tau_low는 *주입 정책값*(결정 C3·OQ-5 — 하드코딩 금지·카드 자기보고 아님).
    호출자가 OQ-5 기본값(0.88·0.70)을 주입하되, 이 함수는 어떤 값이든 받는다.
    전제: tau_low <= tau_high(역전 시 'similar' 구간이 빔 — 호출자 책임, 함수는 거부 안 함).
    """
    candidates: list[DedupCandidate] = []

    for new_id, new_vec in new_concepts:
        for existing_id, existing_vec in existing_concepts:
            if new_id == existing_id:
                continue
            sim = _cosine_similarity(new_vec, existing_vec)
            if sim >= tau_high:
                grade: Literal["auto_suggest", "similar"] = "auto_suggest"
            elif sim >= tau_low:
                grade = "similar"
            else:
                continue
            candidates.append(
                DedupCandidate(
                    new_concept_id=new_id,
                    existing_concept_id=existing_id,
                    similarity=sim,
                    grade=grade,
                )
            )

    candidates.sort(key=lambda c: (-c.similarity, c.new_concept_id, c.existing_concept_id))
    return tuple(candidates)


class FakeEmbedder:
    """Embedder 테스트 더블 — FakeClassifier·FakeMatcher 정신.

    생성 시 주입한 텍스트→벡터 dict를 그대로 조회해 반환한다. 사전에 없는 텍스트는
    결정론을 깨는 임의 기본값을 만들지 않고 KeyError로 fail-loud(테스트가 입력을
    정확히 통제하게 강제).
    """

    def __init__(self, fixed: dict[str, tuple[float, ...]]) -> None:
        self._fixed = fixed

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._fixed[text] for text in texts)
