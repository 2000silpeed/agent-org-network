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

import json as _json
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agent_org_network.agent_card import AgentCard
from agent_org_network.okf_authoring import (  # noqa: PLC2701
    _strip_code_fence,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.okf_dedup import Embedder
from agent_org_network.okf_index import parse_okf_document
from agent_org_network.provider_runtime import (
    ProviderRequest,
    ProviderTransport,
    assemble_stream,
)
from agent_org_network.two_stage_router import ConfidenceAssessor, GroundedConfidence

if TYPE_CHECKING:
    from agent_org_network.index_matcher import KnowledgeIndexMatcher


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


# ── select_assessor env 시임 (AON_ASSESSOR·author_select·select_matcher 대칭) ─────
#
# 현 demo 조립(demo.py `isinstance(matcher, EmbeddingAnnMatcher)`)은 매처 종류로
# assessor를 암묵 선택한다. (b) LlmConfidenceAssessor 추가로 이 암묵 결합을 명시
# 시임으로 승격한다(ADR 0028 §17-b 결정 K). 미설정=auto가 현 배선 100% 보존.

_ASSESS_TRANSPORT_CLAUDE_CODE_ALIASES = frozenset({"claude-code", "llm"})
_ASSESS_TRANSPORT_CLAUDE_API_ALIASES = frozenset({"claude-api", "anthropic"})


def _resolve_assess_model() -> str:
    return (os.environ.get("AON_ASSESSOR_MODEL") or "").strip() or DEFAULT_LLM_ASSESS_MODEL


def select_assess_transport() -> ProviderTransport:
    """LLM assessor의 transport를 env로 고른다 — author_select 선택 규약 대칭(§17-b 결정 K).

    `AON_ASSESSOR_PROVIDER`(소문자 trim·`AON_AUTHOR` 대칭):
      - `claude-code`/`llm`(**기본**) → ClaudeCodeTransport(owner Claude 구독 위임·이 머신
        실증 선례·중앙 토큰 0). 미설정 기본을 claude-code로 두는 건 이 머신이 구독 자격만
        있고 API 키는 없기 때문(author_select와 반대 기본 — assess 실 eval 대상이 이 머신).
      - `claude-api`/`anthropic` → AnthropicSdkTransport(API 키). 선택 extra·지연 import.
      - 알 수 없는 값 → SystemExit(조용한 폴백 없음·author_select 정신).

    실 transport는 각 분기에서만 지연 import(모듈 import 시 SDK·subprocess 무접촉).
    """
    flag = (os.environ.get("AON_ASSESSOR_PROVIDER") or "").strip().lower()
    model = _resolve_assess_model()
    if not flag or flag in _ASSESS_TRANSPORT_CLAUDE_CODE_ALIASES:
        from agent_org_network.provider_transport_claude_code import ClaudeCodeTransport

        return ClaudeCodeTransport(model=model)
    if flag in _ASSESS_TRANSPORT_CLAUDE_API_ALIASES:
        try:
            from agent_org_network.provider_transport_anthropic import (
                AnthropicSdkTransport,
            )
        except ImportError as exc:
            raise SystemExit(
                "LLM assessor(AON_ASSESSOR=llm)가 anthropic SDK transport를 쓰는데 SDK가 "
                "없습니다 — pip install 'agent-org-network[claude-api]'. Claude *구독* "
                "자격이면 AON_ASSESSOR_PROVIDER=claude-code(`claude -p` 위임·기본)를 쓰세요."
            ) from exc
        return AnthropicSdkTransport()
    raise SystemExit(
        f"알 수 없는 AON_ASSESSOR_PROVIDER={flag!r} — 지원: claude-code/llm(기본·`claude -p` "
        "위임), claude-api/anthropic(SDK)."
    )


def select_assessor(
    matcher: "KnowledgeIndexMatcher", okf_root: Path
) -> ConfidenceAssessor | None:
    """`AON_ASSESSOR` env로 stage-2 assessor를 고른다(§17-b 결정 K·author_select 대칭).

    `AON_ASSESSOR`(소문자 trim):
      - 미설정/`auto`(**기본**) → 현 배선 보존(하위호환): matcher가 EmbeddingAnnMatcher면
        같은 임베더 공유 EmbeddingConfidenceAssessor(모델 1회 로드)·아니면 None.
      - `embedding` → EmbeddingConfidenceAssessor(matcher.embedder 공유). matcher에 embedder가
        없으면(overlap 등) SystemExit(명시 오설정 — 조용한 폴백 없음).
      - `llm` → LlmConfidenceAssessor(select_assess_transport(), okf_root). matcher와 무관
        (transport 사용·임베더 공유 끊김) — assessor만 교체되는 경계가 명확해진다. 실 LLM은
        transport 지연 import라 게이트에서 미접촉.
      - `off` → None(assessor 미장착·≥2는 stage-1.5/Contested만).
      - 알 수 없는 값 → SystemExit(조용한 폴백 없음).
    """
    from agent_org_network.index_matcher import EmbeddingAnnMatcher

    flag = (os.environ.get("AON_ASSESSOR") or "").strip().lower()

    if flag in ("", "auto"):
        if isinstance(matcher, EmbeddingAnnMatcher):
            return EmbeddingConfidenceAssessor(
                matcher.embedder,
                okf_root,
                min_confidence=DEFAULT_STAGE2_MIN_CONFIDENCE,
            )
        return None
    if flag == "embedding":
        embedder = getattr(matcher, "embedder", None)
        if embedder is None:
            raise SystemExit(
                "AON_ASSESSOR=embedding인데 매처에 embedder가 없습니다 — "
                "AON_MATCHER=embedding과 짝지으세요(overlap 매처는 임베더 없음)."
            )
        return EmbeddingConfidenceAssessor(
            embedder, okf_root, min_confidence=DEFAULT_STAGE2_MIN_CONFIDENCE
        )
    if flag == "llm":
        max_body = int(
            os.environ.get("AON_ASSESSOR_MAX_BODY") or DEFAULT_LLM_ASSESS_MAX_BODY_CHARS
        )
        min_conf = float(os.environ.get("AON_ASSESSOR_MIN_CONFIDENCE") or 0.0)
        return LlmConfidenceAssessor(
            select_assess_transport(),
            okf_root,
            model=_resolve_assess_model(),
            max_body_chars=max_body,
            min_confidence=min_conf,
        )
    if flag == "off":
        return None
    raise SystemExit(
        f"알 수 없는 AON_ASSESSOR={flag!r} — 지원: auto(기본·현 배선 보존), "
        "embedding, llm, off."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# LlmConfidenceAssessor — stage-2 실 어댑터 2호 (ADR 0028 §17-b 결정 G~M)
# ═══════════════════════════════════════════════════════════════════════════════
#
# (a) EmbeddingConfidenceAssessor가 *질문↔body cosine*(표면 근접)이었다면, (b)는
# 후보 카드의 OKF 개념(core_question 전부 + body 발췌)을 컨텍스트로 실어 LLM에게
# "이 지식만으로 이 질문에 답할 수 있는가"를 confidence + 근거로 판정시킨다.
# 같은 `GroundedConfidence` 값객체·같은 `ConfidenceAssessor` Protocol을 만족 —
# 라우터·자동해소 규칙 무변경(어댑터만 교체·형제 어댑터).
#
# 프롬프트 빌드·응답 파싱은 순수함수로 분리한다(LlmAuthor의 build_*_request/
# parse_*_response 정신) — 게이트 내에서 transport 없이 결정론 단언한다.

# body 발췌 상한(개념당 char) — LLM 컨텍스트·비용이 body 전문 × 개념 수로 폭발하는 것을
# 막는다(§17-b 결정 I). read_okf_bundle의 100_000자 방어 상한과 같은 정신의 발췌.
# 실 스윕 몫(결정 M) — v1 기본은 발췌(비용 하한).
DEFAULT_LLM_ASSESS_MAX_BODY_CHARS = 800

# LLM 자기평가 기본 모델 — 빠른 것 우선(§실 eval: haiku 시도→실패 시 sonnet).
# author(sonnet)·분류(haiku)와 같은 *모듈 상수* 패턴 — 모델 교체는 이 한 줄.
DEFAULT_LLM_ASSESS_MODEL = "claude-haiku-4-5"

_ASSESS_SYSTEM = (
    "당신은 조직 라우팅 보조자입니다. 아래 [지식]은 한 담당자(에이전트)의 지식 목차와 "
    "본문 발췌입니다. 사용자 [질문]이 주어지면, **이 지식만으로 그 질문에 정확히 답할 수 "
    "있는지**를 0.0~1.0 사이 confidence로 판정하세요.\n"
    "- 1.0에 가까울수록: 이 지식이 질문을 직접·정확히 다룸(근거 조문·주제가 명확히 일치).\n"
    "- 0.0에 가까울수록: 이 지식은 질문과 무관하거나 인접 주제일 뿐 답의 근거가 없음.\n"
    "출력은 **JSON 객체 하나**만 내세요 — 코드펜스·산문·설명 금지:\n"
    '{"confidence": 0.0~1.0 사이 실수, "grounding": "판단 근거 한 줄"}'
)


@dataclass(frozen=True)
class AssessConcept:
    """assess 프롬프트에 싣는 개념 한 건 — core_question(전부) + body 발췌(상한).

    okf_root에서 추출(assess 호출부)해 build_assess_request에 넘긴다(순수 함수 경계 유지).
    """

    core_question: str
    body: str


def build_assess_request(
    question: str,
    concepts: Sequence[AssessConcept],
    *,
    model: str,
    max_body_chars: int = DEFAULT_LLM_ASSESS_MAX_BODY_CHARS,
) -> ProviderRequest:
    """질문 + 개념 목록 → 답가능성 판정 요청(순수·IO 0 — LlmAuthor build_*_request 정신).

    core_question은 전부 싣고(토큰 싸고 답가능성 신호 강함), body는 개념당 max_body_chars로
    발췌한다(§17-b 결정 I — LLM 컨텍스트·비용 폭발 방지). system이 JSON 강제·코드펜스 금지.
    """
    lines: list[str] = []
    for i, c in enumerate(concepts, start=1):
        body = c.body[:max_body_chars]
        lines.append(f"[개념 {i}] 핵심질문: {c.core_question}\n본문: {body}")
    knowledge = "\n\n".join(lines) if lines else "(등록된 지식 개념 없음)"
    user_content = f"[지식]\n{knowledge}\n\n[질문]\n{question}"
    return ProviderRequest(
        model=model,
        system=_ASSESS_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )


def parse_assess_response(text: str) -> tuple[float, str]:
    """assess 응답 JSON → (confidence[0,1], grounding)(순수·IO 0).

    _strip_code_fence(okf_authoring 재사용) → json.loads → 필드 추출.
    파싱 실패(JSONDecodeError·객체 아님·필드 부재·숫자 아님) → (0.0, "") 폴백.
    confidence는 [0,1]로 클램프한다(범위 밖은 클램프·NaN 등은 0.0). authoring은 실패를
    던지지만(파이프라인 중단 허용) assessor는 못 던진다 — 라우팅 경로는 항상 종착해야
    하므로 저신뢰(0.0) 흡수한다(미아 없음·§17-b 결정 H).
    """
    try:
        raw = _json.loads(_strip_code_fence(text))
    except (_json.JSONDecodeError, ValueError):
        return (0.0, "")
    if not isinstance(raw, dict):
        return (0.0, "")
    obj = cast("dict[str, object]", raw)
    conf_raw = obj.get("confidence")
    if not isinstance(conf_raw, (int, float)) or isinstance(conf_raw, bool):
        return (0.0, "")
    conf = float(conf_raw)
    if math.isnan(conf) or math.isinf(conf):
        return (0.0, "")
    conf = max(0.0, min(1.0, conf))
    grounding = obj.get("grounding")
    grounding_str = grounding if isinstance(grounding, str) else ""
    return (conf, grounding_str)


class LlmConfidenceAssessor:
    """ConfidenceAssessor Protocol 구현 — LLM 답가능성 자기평가 접지(ADR 0028 §17-b).

    (a) EmbeddingConfidenceAssessor와 형제 어댑터 — 접지 방식만 다르다(cosine vs LLM 추론).
    후보 카드의 okf_root/{agent_id}/*.md 개념(core_question 전부 + body 발췌)을 프롬프트에
    실어 transport로 LLM에게 confidence를 묻는다. okf_root 개념 추출은
    EmbeddingConfidenceAssessor와 *같은 경로·같은 파서*(parse_okf_document)를 쓴다.

    실행 위치·데이터 경계는 §17 결정 B와 동일 — 인프로세스 데모는 okf_root 디스크 직접
    읽기(ClaudeCodeRuntime cwd 접지 선례·워커=중앙 디제너레이트), 크로스머신은 §15
    FetchDocument 확장(후속). transport는 owner측(구독/API 키) — 중앙 토큰 0.

    캐시(옵셔널·결정 J): (question, agent_id, 파일 mtime 튜플) → GroundedConfidence.
    LLM 응답은 비결정이라 캐시는 *같은 응답 재현*이 아니라 *호출 절감*이 목적(첫 응답 고정).
    OKF 변경(mtime) 시 자연 무효화. 캐시는 어댑터 로컬(중앙 store 미탑재·비소유).

    불변식:
      - 미아 없음: 예외/타임아웃/파싱 실패/개념 0 → confidence=0.0(저신뢰) 흡수 — 예외를
        라우터로 던지지 않는다(어댑터 계약·§17-b 결정 H·L·EmbeddingConfidenceAssessor 대칭).
      - 중앙 토큰 0·비소유: body·프롬프트·응답은 어댑터 로컬 — 중앙엔 confidence 수치만.
    """

    def __init__(
        self,
        transport: ProviderTransport,
        okf_root: Path,
        *,
        model: str = DEFAULT_LLM_ASSESS_MODEL,
        max_body_chars: int = DEFAULT_LLM_ASSESS_MAX_BODY_CHARS,
        min_confidence: float = 0.0,
        cache: bool = True,
    ) -> None:
        self._transport = transport
        self._okf_root = okf_root
        self._model = model
        self._max_body_chars = max_body_chars
        self.min_confidence = min_confidence
        self._cache_enabled = cache
        # (question, agent_id, 파일별 mtime 튜플) → GroundedConfidence — 어댑터 로컬 캐시.
        self._cache: dict[tuple[str, str, tuple[float, ...]], GroundedConfidence] = {}

    def _concepts(self, agent_id: str) -> tuple[tuple[float, ...], tuple[AssessConcept, ...]]:
        """agent_id의 okf_root 개념(core_question + body)과 파일 mtime 튜플을 얻는다.

        EmbeddingConfidenceAssessor._body_vectors와 같은 경로·같은 파서(parse_okf_document).
        문서 0개면 ((), ()) — assess가 저신뢰(0.0)로 흡수.
        """
        agent_dir = self._okf_root / agent_id
        if not agent_dir.is_dir():
            return ((), ())
        md_paths = sorted(agent_dir.glob("*.md"), key=lambda p: p.name)
        if not md_paths:
            return ((), ())
        mtimes = tuple(p.stat().st_mtime for p in md_paths)
        concepts: list[AssessConcept] = []
        for path in md_paths:
            front, body = parse_okf_document(path.read_text(encoding="utf-8"))
            core_question = str(front.get("core_question") or front.get("title") or path.stem)
            concepts.append(AssessConcept(core_question=core_question, body=body))
        return (mtimes, tuple(concepts))

    def assess(self, question: str, card: AgentCard) -> GroundedConfidence:
        """question에 대해 card의 지식으로 답할 수 있는 confidence를 LLM에게 묻는다.

        예외/타임아웃/파싱 실패/개념 0/min_confidence 미만은 저신뢰(0.0)로 흡수한다 —
        예외를 라우터로 던지지 않는다(어댑터 계약·§17-b 결정 H·L·미아 없음 보존).
        """
        try:
            mtimes, concepts = self._concepts(card.agent_id)
            if not concepts:
                return GroundedConfidence(agent_id=card.agent_id, confidence=0.0)

            cache_key = (question, card.agent_id, mtimes)
            if self._cache_enabled:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    return cached

            request = build_assess_request(
                question,
                concepts,
                model=self._model,
                max_body_chars=self._max_body_chars,
            )
            text = assemble_stream(self._transport(request))
            confidence, grounding = parse_assess_response(text)
            if confidence < self.min_confidence:
                confidence = 0.0
                grounding = ""
            result = GroundedConfidence(
                agent_id=card.agent_id, confidence=confidence, grounding=grounding
            )
            if self._cache_enabled:
                self._cache[cache_key] = result
            return result
        except Exception:
            # 어댑터 계약: 예외를 라우터로 던지지 않고 저신뢰로 흡수(미아 없음 보존).
            return GroundedConfidence(agent_id=card.agent_id, confidence=0.0)
