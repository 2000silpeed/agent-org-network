"""KnowledgeIndexMatcher 포트 + ConceptOverlapMatcher(v1) + FakeMatcher (ADR 0028 §3·§7).

포트+어댑터 패턴: Classifier·AgentRuntime·ProviderTransport와 동일 정신.
코어는 RDF·임베딩·벡터 인프라에 결합하지 않는다.

불변식:
  - 중앙 토큰 0: LLM·외부 API·벡터 인프라 0(순수 토큰 오버랩, v1).
  - 미아 없음: 0 후보를 정상 반환 — Unowned 투영은 T10.3a 책임.
  - Authority 중앙: 매처는 후보 제안만 — 권한·종착 판정 아님.
  - 결정론: 같은 질문+인덱스 → 항상 같은 순서·같은 결과.
    score 내림차순, 동점은 agent_id 오름차순(_collaborators_for 정렬 정신).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from agent_org_network.knowledge_index import Concept, KnowledgeIndex

if TYPE_CHECKING:
    from agent_org_network.okf_dedup import Embedder

# ── 토큰화 ────────────────────────────────────────────────────────────────────
# 형태소 분석 없이 정규식으로 근사 — ADR 0028 §7 v1 정책:
# 소문자 + 공백/문장부호 분리(한국어·영어 공용).
# 정규식: 하나 이상의 알파벳·숫자·한글 연속 → 토큰.
# 문장부호·공백·특수문자는 구분자(버림).
_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣]+")


# 한국어 조사 접미사 — 토큰 끝에서 떼어 명사↔조사결합의 오버랩을 살린다("환불은"↔"환불").
# 긴 조사 우선(그리디)으로 "으로"를 "로"로 잘못 떼지 않게 한다. 조사를 뗀 나머지가
# 2글자 이상일 때만 제거해 짧은 명사("국가"·"휴가"의 "가")를 조사로 오인하지 않는다.
_JOSA: tuple[str, ...] = (
    "으로서", "으로써", "에서도", "에게서", "에서는", "에게는",
    "으로", "에서", "에게", "한테", "께서", "까지", "부터", "보다",
    "처럼", "마다", "조차", "밖에", "이나", "라도", "이란", "이라", "라는",
    "은", "는", "이", "가", "을", "를", "에", "의", "와", "과", "로", "도", "만", "나",
)


def _strip_josa(tok: str) -> str:
    """한글 토큰 끝의 조사를 제거한다(나머지 2글자+ 보존·긴 조사 우선·결정론)."""
    for j in _JOSA:
        if tok.endswith(j) and len(tok) - len(j) >= 2:
            return tok[: -len(j)]
    return tok


def _tokenize(text: str) -> frozenset[str]:
    """text를 소문자 토큰 집합으로 변환.

    - 소문자 정규화(영문·숫자는 lower, 한글은 그대로).
    - 알파벳·숫자·한글 연속 단위로 토큰 추출 — 문장부호·공백 버림.
    - 한글 포함 토큰은 조사 접미사를 정규화("환불은"→"환불") — 자연어 질문의 조사가
      개념 core_question 토큰과 오버랩되게 한다(v1 거친 매칭의 한국어 대응).
    - frozenset 반환 → 교집합·Jaccard 계산에 적합·결정론.
    """
    toks: set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        t = m.group().lower()
        if any("가" <= ch <= "힣" for ch in t):
            t = _strip_josa(t)
        toks.add(t)
    return frozenset(toks)


def _overlap_score(q_tokens: frozenset[str], concept_tokens: frozenset[str]) -> float:
    """질문 토큰 집합과 개념 토큰 집합의 오버랩 점수(공유 토큰 수).

    v1 = 공유 토큰 수(정수지만 float 반환 — 후속 Jaccard/TF-IDF 교체 자리).
    score > 0 이면 후보 채택 임계 충족(공유 토큰 1개 이상).
    결정론: 같은 입력 → 항상 같은 float.
    """
    if not q_tokens or not concept_tokens:
        return 0.0
    return float(len(q_tokens & concept_tokens))


# ── 값 객체 ───────────────────────────────────────────────────────────────────


class IndexMatch(BaseModel, frozen=True):
    """stage-1 매처가 반환하는 후보 한 건 — 조직 내부값(사용자向 투영 미노출).

    score·matched_concept_id는 노출 불변식상 사용자向 OrgReply/Answered에 싣지 않는다.
    T10.3a admission 재검증 및 T10.3b stage-2 자동해소의 입력으로만 쓰인다.
    """

    agent_id: str
    score: float
    matched_concept_id: str


# ── 포트(Protocol) ────────────────────────────────────────────────────────────


class KnowledgeIndexMatcher(Protocol):
    """stage-1 매처 포트 — Classifier·AgentRuntime 포트와 동일 패턴.

    구현: ConceptOverlapMatcher(v1 결정론)·EmbeddingAnnMatcher(스케일·게이트 밖)·FakeMatcher(테스트).
    """

    def match(
        self, question: str, indexes: Sequence[KnowledgeIndex]
    ) -> tuple[IndexMatch, ...]:
        """question과 각 KnowledgeIndex를 매칭해 후보 IndexMatch를 돌려준다.

        0 후보는 빈 튜플로 정상 반환(Unowned 투영은 호출자 책임 — 미아 없음 불변식).
        결과: score 내림차순, 동점은 agent_id 오름차순.
        """
        ...


# ── v1 결정론 어댑터 ───────────────────────────────────────────────────────────


class ConceptOverlapMatcher:
    """KnowledgeIndexMatcher Protocol의 v1 결정론 구현 — 토큰 오버랩 매칭.

    ADR 0028 §7 v1:
    - 질문과 각 Concept의 core_question·label·type을 토큰화(소문자·정규식 분리).
    - 개념별 오버랩 점수 = 공유 토큰 수(결정론·순수 함수).
    - 후보 채택 임계: score > 0(공유 토큰 1개 이상).
    - 에이전트당 한 IndexMatch(최고 점수 개념).
    - 결정론 정렬: score 내림차순, 동점은 agent_id 오름차순.

    LLM 0 · 외부 API 0 · 벡터 인프라 0 · 순수 함수.
    """

    def match(
        self, question: str, indexes: Sequence[KnowledgeIndex]
    ) -> tuple[IndexMatch, ...]:
        """질문 토큰 오버랩으로 후보 에이전트를 제안한다.

        각 KnowledgeIndex의 concepts를 순회해 에이전트당 최고 점수 개념을 뽑고,
        score > 0인 에이전트를 후보로 반환한다.

        반환: score 내림차순, 동점 agent_id 오름차순 결정론 정렬.
        """
        q_tokens = _tokenize(question)
        candidates: list[IndexMatch] = []

        for idx in indexes:
            best_score = 0.0
            best_concept_id = ""

            for concept in idx.concepts:
                # core_question·label·type 모두 토큰화 대상
                concept_text = concept.core_question + " " + concept.label
                if concept.type:
                    concept_text += " " + concept.type
                concept_tokens = _tokenize(concept_text)

                score = _overlap_score(q_tokens, concept_tokens)
                if score > best_score:
                    best_score = score
                    best_concept_id = concept.id

            if best_score > 0.0:
                candidates.append(
                    IndexMatch(
                        agent_id=idx.agent_id,
                        score=best_score,
                        matched_concept_id=best_concept_id,
                    )
                )

        # 결정론 정렬: score 내림차순, 동점 agent_id 오름차순
        candidates.sort(key=lambda m: (-m.score, m.agent_id))
        return tuple(candidates)


# ── relevant_concepts 순수 헬퍼 ──────────────────────────────────────────────


def relevant_concepts(question: str, index: KnowledgeIndex) -> tuple[Concept, ...]:
    """질문과 오버랩 > 0인 개념만 추려 반환한다.

    index.concepts 중 core_question + label + type 토큰이 질문 토큰과
    1개 이상 겹치는 개념만 포함한다(score > 0).
    정렬: 오버랩 점수 내림차순, 동점은 concept.id 오름차순(결정론).
    빈 인덱스·매칭 0 → 빈 튜플.

    중앙 토큰 0: LLM·외부 API·벡터 인프라 0 — 순수 토큰 오버랩.
    """
    q_tokens = _tokenize(question)
    scored: list[tuple[float, str, Concept]] = []

    for concept in index.concepts:
        concept_text = concept.core_question + " " + concept.label
        if concept.type:
            concept_text += " " + concept.type
        concept_tokens = _tokenize(concept_text)
        score = _overlap_score(q_tokens, concept_tokens)
        if score > 0.0:
            scored.append((score, concept.id, concept))

    # 점수 내림차순, 동점 concept.id 오름차순
    scored.sort(key=lambda t: (-t[0], t[1]))
    return tuple(c for _, _, c in scored)


# ── 테스트 더블 ───────────────────────────────────────────────────────────────


class FakeMatcher:
    """KnowledgeIndexMatcher 테스트 더블 — FakeClassifier·StubRuntime 정신.

    생성 시 주입한 고정 후보를 그대로 반환한다.
    T10.3 2단 라우팅 로직을 매처 구현과 독립으로 결정론 단언하기 위함.
    """

    def __init__(self, fixed: tuple[IndexMatch, ...]) -> None:
        self._fixed = fixed

    def match(
        self, question: str, indexes: Sequence[KnowledgeIndex]
    ) -> tuple[IndexMatch, ...]:
        """질문·인덱스 무관하게 고정 후보를 그대로 반환."""
        return self._fixed


# ── 스케일 어댑터: 로컬 임베딩 매처 (ADR 0028 §7·게이트 밖) ────────────────────

# 후보 채택 cosine 임계 τ 기본값 — e5-small 대칭 cosine 분포 기준. S8 A/B 스윕(0.75/
# 0.80/0.85) 실측으로 조정 가능하게 상수 한 곳에 둔다. τ 미만은 후보에서 제외(0 후보는
# 정상 반환 — Unowned 투영은 라우터 층 책임, 미아 없음 불변식은 매처 밖).
# 실측 근거(docs/scale-eval-2026-07-02.md A/B): e5-small cosine top-1 분포가 [0.814, 0.918]로
# 압축돼 0.80~0.82는 전 후보 통과(contested 붕괴), 0.85가 유일하게 분리를 시작한다.
# 남는 다후보 해소는 stage-2(ConfidenceAssessor·ADR 0028 §6) 몫 — τ를 더 올려 풀지 않는다.
DEFAULT_EMBED_TAU = 0.85


def _concept_doc_text(concept: Concept) -> str:
    """개념을 문서측 임베딩 텍스트로 결합 — label + core_question + domain.

    stage-1 토큰 오버랩(_tokenize)이 core_question·label·type을 봤듯, 임베딩 문서측은
    사람이 읽는 자연어 필드(label·core_question·domain)를 결합한다. type은 태그성이라
    제외(자연어 신호 희석 방지). 결합 순서·구분자는 결정론(같은 개념→같은 문서 텍스트).
    """
    return f"{concept.label} {concept.core_question} {concept.domain}"


class EmbeddingAnnMatcher:
    """KnowledgeIndexMatcher Protocol의 스케일 어댑터 — 로컬 임베딩 cosine 매칭 [게이트 밖].

    ADR 0028 §7 스케일 어댑터:
    - 개념 텍스트(label+core_question+domain)를 *문서측*으로 임베딩하고, 질의를 *질의측*으로
      임베딩해 cosine 유사도로 후보를 산출한다. 어휘 표면 불일치(조사·동의어·구어)를 의미
      공간에서 흡수 — ConceptOverlapMatcher 실패 모드(어휘 공백 0매칭·동점 노이즈·교차
      매칭)의 대안(docs/scale-eval-2026-07-02.md).
    - **인덱스측 임베딩 캐시**: (agent_id, generated_at) 키로 개념 벡터를 재사용한다. 질의마다
      전 개념을 재임베딩하지 않고, 인덱스가 갱신(generated_at 변경)되면 키가 달라져 자연
      무효화된다.
    - 후보 임계 τ(주입 정책값·기본 DEFAULT_EMBED_TAU): cosine ≥ τ 개념만 후보. 에이전트당
      최고 cosine 개념 1건. score = cosine 그대로.
    - 규모 70~수백은 브루트포스 cosine으로 충분 — ANN 라이브러리 0(과설계·후속).

    **중앙 토큰 0**: Embedder는 owner측 로컬 ONNX(FastEmbedEmbedder) — 외부 API·키 0.
    **결정론**: 같은 질문+같은 인덱스(같은 벡터)→같은 순서·같은 결과(score 내림차순, 동점
    agent_id 오름차순). 단, cosine 부동소수라 float 동일성은 임베더 결정성에 의존한다.
    벡터는 L2 정규화 가정(Embedder 어댑터 책임)이라 dot product가 곧 cosine.
    """

    def __init__(self, embedder: Embedder, *, tau: float = DEFAULT_EMBED_TAU) -> None:
        self._embedder = embedder
        self._tau = tau
        # (agent_id, generated_at) → ((concept_id, vector), ...) 인덱스측 임베딩 캐시.
        self._cache: dict[
            tuple[str, datetime], tuple[tuple[str, tuple[float, ...]], ...]
        ] = {}

    def _index_vectors(
        self, index: KnowledgeIndex
    ) -> tuple[tuple[str, tuple[float, ...]], ...]:
        """인덱스 개념 벡터를 (agent_id, generated_at) 캐시로 얻는다(적중 시 재임베딩 0).

        캐시 미스면 전 개념을 한 번 임베딩해 채운다. 인덱스 갱신(generated_at 변경)은
        새 키라 자동 무효화된다. 개념 0개면 빈 튜플(임베더 호출 0).
        """
        key = (index.agent_id, index.generated_at)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not index.concepts:
            self._cache[key] = ()
            return ()
        texts = [_concept_doc_text(c) for c in index.concepts]
        vectors = self._embedder.embed(texts)
        entry = tuple(
            (c.id, vec) for c, vec in zip(index.concepts, vectors)
        )
        self._cache[key] = entry
        return entry

    def match(
        self, question: str, indexes: Sequence[KnowledgeIndex]
    ) -> tuple[IndexMatch, ...]:
        """질의 임베딩과 각 인덱스 개념 벡터의 cosine으로 후보를 산출한다.

        질의는 질의측으로 1회 임베딩. 각 인덱스는 캐시된 개념 벡터와 cosine을 재고,
        cosine ≥ τ 중 최고 개념 1건을 에이전트 후보로 담는다. 전부 τ 미만이면 그
        에이전트는 후보에서 빠진다(0 후보는 빈 튜플로 정상 반환).

        반환: score(cosine) 내림차순, 동점 agent_id 오름차순 결정론 정렬.
        """
        query_vecs = self._embedder.embed([question])
        if not query_vecs:
            return ()
        q_vec = query_vecs[0]

        candidates: list[IndexMatch] = []
        for index in indexes:
            best_score: float | None = None
            best_concept_id = ""
            for concept_id, vec in self._index_vectors(index):
                sim = _cosine(q_vec, vec)
                if sim >= self._tau and (best_score is None or sim > best_score):
                    best_score = sim
                    best_concept_id = concept_id
            if best_score is not None:
                candidates.append(
                    IndexMatch(
                        agent_id=index.agent_id,
                        score=best_score,
                        matched_concept_id=best_concept_id,
                    )
                )

        candidates.sort(key=lambda m: (-m.score, m.agent_id))
        return tuple(candidates)


def _cosine(u: tuple[float, ...], v: tuple[float, ...]) -> float:
    """cosine(u, v) = dot / (||u|| * ||v||). 차원 불일치·0벡터는 ValueError(fail-loud).

    okf_dedup._cosine_similarity와 같은 계산 — 임베딩 도메인 결합을 피해 매처 모듈에
    작은 순수 함수로 둔다(포트 계약상 벡터는 L2 정규화 가정이나, 방어적으로 norm 나눔).
    """
    if len(u) != len(v):
        raise ValueError(f"벡터 차원 불일치: {len(u)} != {len(v)}")
    norm_u = math.sqrt(sum(x * x for x in u))
    norm_v = math.sqrt(sum(x * x for x in v))
    if norm_u == 0.0 or norm_v == 0.0:
        raise ValueError("0벡터의 cosine 유사도는 정의되지 않습니다.")
    return sum(x * y for x, y in zip(u, v)) / (norm_u * norm_v)


# ── select_matcher env 시임 (AON_MATCHER·embedder_select 대칭) ────────────────

_OVERLAP_ALIASES = frozenset({"", "overlap"})
_EMBEDDING_ALIASES = frozenset({"embedding", "fastembed"})


def select_matcher() -> KnowledgeIndexMatcher:
    """env 플래그로 stage-1 매처를 고른다 — select_embedder·select_runtime과 대칭.

    `AON_MATCHER`(소문자 trim):
      - 미설정/`overlap` → `ConceptOverlapMatcher()`(**기본·무변경**·게이트 결정론).
      - `embedding`/`fastembed` → `EmbeddingAnnMatcher(FastEmbedEmbedder())`(실 ONNX·게이트
        밖·dedup extra 필요). 실 어댑터는 이 분기에서만 지연 import(기본 경로 무접촉).
      - 알 수 없는 값 → 명시 실패(SystemExit — 조용히 기본으로 안 떨어진다).
    """
    import os

    flag = (os.environ.get("AON_MATCHER") or "").strip().lower()
    if flag in _OVERLAP_ALIASES:
        return ConceptOverlapMatcher()
    if flag in _EMBEDDING_ALIASES:
        # 실 어댑터는 이 분기에서만 지연 import(기본 overlap 경로는 fastembed 무접촉).
        from agent_org_network.provider_embed_fastembed import FastEmbedEmbedder

        print(
            f"[select_matcher] AON_MATCHER={flag} → EmbeddingAnnMatcher(FastEmbedEmbedder) "
            "— owner측 로컬 ONNX 임베딩·중앙 토큰 0(게이트 밖)."
        )
        return EmbeddingAnnMatcher(FastEmbedEmbedder())
    raise SystemExit(
        f"알 수 없는 AON_MATCHER={flag!r} — 지원: overlap(기본), embedding/fastembed"
    )
