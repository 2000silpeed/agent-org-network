"""EmbeddingConfidenceAssessor — stage-2 실 어댑터 (ADR 0028 §17 결정 A~D).

`ConfidenceAssessor` Protocol(`two_stage_router.py`)의 실 구현. 후보 카드의
okf_root/{agent_id}/*.md 개념 **body 전문**을 문서측으로, 질문을 질의측으로
임베딩해 cosine 유사도를 계산한다. 카드 내 최고 cosine을 confidence로
반환한다(항등 매핑 — 절대 스케일 보정 없음, 자동해소는 후보 간 상대 격차로 판정).

stage-1(`_concept_doc_text` = label+core_question+domain, 목차)과 대비되는
접지 텍스트(body)를 써서 stage-1이 못 본 신호를 연다(§17 동기 — 그레이 쌍은
목차 어휘가 겹치지만 body의 근거 법령·조문·행위주체는 배타적으로 갈린다).

실행 위치: 인프로세스 데모(워커=중앙 박스) — okf_root 디스크 직접 읽기.
`ClaudeCodeRuntime`이 owner OKF 번들을 cwd로 두고 읽는 선례와 동형(비소유
위반 아님·§17 결정 B). 크로스머신은 §15 FetchDocument 확장(후속·게이트 밖).

body 추출은 `okf_index.parse_okf_document`(기존 파서)를 재사용한다 — 중복
파서 금지.

캐시: (agent_id, 파일별 mtime) 키로 body 벡터를 캐시한다. OKF 파일 변경(mtime
갱신) 시 캐시가 자동 무효화된다(`EmbeddingAnnMatcher` (agent_id, generated_at)
무효화 규약과 동형 — 이 어댑터는 KnowledgeIndex가 아닌 okf_root 디스크를
직접 읽으므로 파일 mtime이 그 자리를 대신한다).

불변식:
  - 중앙 토큰 0: Embedder는 owner측 로컬(FastEmbedEmbedder) — 외부 API·키 0.
  - 미아 없음: 예외/개념 0 → confidence=0.0(저신뢰)로 흡수 — 예외를 라우터로
    던지지 않는다(어댑터 계약, §17 결정 D).
  - 비소유: body·벡터는 어댑터 로컬 캐시에만 — 중앙 store(RoutingDecision)에
    안 실린다. 노출되는 건 GroundedConfidence.confidence(수치)뿐.
"""

from __future__ import annotations

import math
from pathlib import Path

from agent_org_network.agent_card import AgentCard
from agent_org_network.okf_dedup import Embedder
from agent_org_network.okf_index import parse_okf_document
from agent_org_network.two_stage_router import GroundedConfidence


def _cosine(u: tuple[float, ...], v: tuple[float, ...]) -> float:
    """cosine(u, v) = dot / (||u|| * ||v||). index_matcher._cosine·okf_dedup._cosine_similarity와 동일 계산.

    0벡터·차원 불일치는 ValueError(fail-loud) — 호출부(assess)가 감싸 저신뢰로 흡수한다.
    """
    if len(u) != len(v):
        raise ValueError(f"벡터 차원 불일치: {len(u)} != {len(v)}")
    norm_u = math.sqrt(sum(x * x for x in u))
    norm_v = math.sqrt(sum(x * x for x in v))
    if norm_u == 0.0 or norm_v == 0.0:
        raise ValueError("0벡터의 cosine 유사도는 정의되지 않습니다.")
    return sum(x * y for x, y in zip(u, v)) / (norm_u * norm_v)


# stage-2 정책값 실측 확정(2026-07-02·docs/scale-eval-2026-07-02.md S10·ADR 0028 §17):
# clear_winner_margin=0.02·min_confidence=0.75가 오라우팅 가드(기준선 1.4%+1%p) 통과 중
# contested 최소 조합. min_confidence는 관측 cosine 하한(0.838)보다 낮게 — 분포 중간대(0.85)를
# 침범하면 클램프가 격차 계산을 흔들어 오라우팅이 악화된다(스윕 실측).
DEFAULT_STAGE2_CLEAR_WINNER_MARGIN = 0.02
DEFAULT_STAGE2_MIN_CONFIDENCE = 0.75


class EmbeddingConfidenceAssessor:
    """ConfidenceAssessor Protocol 구현 — 카드 개념 body 전문 cosine 접지(ADR 0028 §17)."""

    def __init__(
        self,
        embedder: Embedder,
        okf_root: Path,
        *,
        min_confidence: float = 0.0,
    ) -> None:
        self._embedder = embedder
        self._okf_root = okf_root
        self.min_confidence = min_confidence
        # (agent_id, (파일별 mtime 튜플)) → ((path, body_vector), ...) — 어댑터 로컬 캐시.
        self._cache: dict[
            tuple[str, tuple[float, ...]], tuple[tuple[str, tuple[float, ...]], ...]
        ] = {}

    def _body_vectors(
        self, agent_id: str
    ) -> tuple[tuple[str, tuple[float, ...]], ...]:
        """agent_id의 okf_root 개념 body 벡터를 (파일별 mtime) 캐시로 얻는다.

        캐시 미스면 전 문서를 한 번에 임베딩해 채운다. 파일 변경(mtime 갱신)은
        새 키라 자동 무효화된다. 문서 0개면 빈 튜플(임베더 호출 0).
        """
        agent_dir = self._okf_root / agent_id
        if not agent_dir.is_dir():
            return ()
        md_paths = sorted(agent_dir.glob("*.md"), key=lambda p: p.name)
        if not md_paths:
            return ()

        mtimes = tuple(p.stat().st_mtime for p in md_paths)
        key = (agent_id, mtimes)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        bodies: list[str] = []
        for path in md_paths:
            text = path.read_text(encoding="utf-8")
            _front, body = parse_okf_document(text)
            bodies.append(body)

        vectors = self._embedder.embed(bodies)
        entry = tuple((str(p), vec) for p, vec in zip(md_paths, vectors))
        self._cache[key] = entry
        return entry

    def assess(self, question: str, card: AgentCard) -> GroundedConfidence:
        """question에 대해 card의 개념 body 최고 cosine을 confidence로 반환한다.

        cosine이 min_confidence 이상이면 항등(스케일 보정 없음, ADR 0028 §17 결정 C).
        min_confidence 미만·개념 0(문서 없음)·임베딩 실패는 저신뢰(confidence=0.0)로
        흡수한다 — 예외를 라우터로 던지지 않는다(어댑터 계약, §17 결정 D. 라우터는
        무수정 — 0.0 클램프가 격차 판정에서 자연히 Contested 낙하를 유도한다).
        """
        try:
            body_vectors = self._body_vectors(card.agent_id)
            if not body_vectors:
                return GroundedConfidence(agent_id=card.agent_id, confidence=0.0)

            query_vecs = self._embedder.embed([question])
            if not query_vecs:
                return GroundedConfidence(agent_id=card.agent_id, confidence=0.0)
            q_vec = query_vecs[0]

            best = max(_cosine(q_vec, vec) for _path, vec in body_vectors)
            if best < self.min_confidence:
                return GroundedConfidence(agent_id=card.agent_id, confidence=0.0)
            return GroundedConfidence(agent_id=card.agent_id, confidence=best)
        except Exception:
            # 어댑터 계약: 예외를 라우터로 던지지 않고 저신뢰로 흡수(미아 없음 보존).
            return GroundedConfidence(agent_id=card.agent_id, confidence=0.0)
