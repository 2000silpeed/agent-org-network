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

import re
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from agent_org_network.knowledge_index import Concept, KnowledgeIndex

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
