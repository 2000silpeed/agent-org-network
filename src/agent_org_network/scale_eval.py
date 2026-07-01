"""스케일 관측 시나리오 격리 조립 모듈 + tier별 집계 러너.

`build_demo`(데모 시드)와 완전히 격리된 조립 경로 — 스케일 관측 전용 registry/OKF
디렉터리를 받아 Registry·InMemoryPublishedIndexStore·TwoStageRouter를 조립한다.
기존 seam(registry.py·okf_index.py·two_stage_router.py·index_matcher.py)을 그대로
재사용하는 얇은 조립 함수만 둔다 — 도메인 로직 신규 없음.

assessor=None 고정: stage-2 자동해소를 켜지 않는다 — ConceptOverlapMatcher(v1
결정론 토큰 오버랩) 단독의 현 한계(오라우팅·contested·unowned률)를 있는 그대로
실측하기 위함(S6.5 판정 근거).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agent_org_network.conflict import PrecedentStore
from agent_org_network.decision import Contested, Routed, RoutingDecision
from agent_org_network.golden import SampleQuestion
from agent_org_network.index_matcher import ConceptOverlapMatcher, KnowledgeIndexMatcher
from agent_org_network.okf_index import build_knowledge_index_from_okf
from agent_org_network.registry import Registry
from agent_org_network.two_stage_router import InMemoryPublishedIndexStore, TwoStageRouter

Tier = Literal["easy", "hard", "ambiguous"]
Disposition = Literal["routed", "contested", "unowned"]


# ── 격리 조립 3함수 ───────────────────────────────────────────────────────────


def build_scale_registry(scale_dir: Path) -> Registry:
    """scale_dir(users.yaml + agents/*.yaml)에서 Registry를 로드·검증해 반환한다.

    기존 Registry.load/validate 재사용(신규 로직 0) — build_demo·_USERS·_CARDS 무접촉.
    """
    registry = Registry()
    registry.load(scale_dir)
    registry.validate()
    return registry


def build_scale_index_store(
    registry: Registry,
    okf_root: Path,
    *,
    generated_at: datetime,
) -> InMemoryPublishedIndexStore:
    """registry의 각 카드에 대해 okf_root에서 KnowledgeIndex를 도출해 store에 담는다.

    build_knowledge_index_from_okf(기존 seam)를 카드마다 호출 → put. 같은
    generated_at을 전 카드에 적용(결정론·동일 배치 시드).
    """
    store = InMemoryPublishedIndexStore()
    for card in registry.all_cards():
        index = build_knowledge_index_from_okf(card, okf_root, generated_at=generated_at)
        store.put(index)
    return store


def build_scale_router(
    registry: Registry,
    index_store: InMemoryPublishedIndexStore,
    *,
    root_user: str = "desk_root",
    precedents: PrecedentStore | None = None,
    matcher: KnowledgeIndexMatcher | None = None,
) -> TwoStageRouter:
    """매처(기본 ConceptOverlapMatcher) + assessor=None인 TwoStageRouter를 조립한다.

    matcher 미주입이면 ConceptOverlapMatcher(v1 결정론·기존 테스트 무변경). S8 A/B는
    EmbeddingAnnMatcher를 주입해 같은 조립·같은 골든셋에 대조한다.

    assessor=None 고정 — stage-2 자동해소 없음(현 한계 실측 목적, 모듈 docstring 참조).
    """
    return TwoStageRouter(
        registry,
        matcher if matcher is not None else ConceptOverlapMatcher(),
        index_store,
        root_user,
        precedents=precedents,
        assessor=None,
    )


# ── tier별 집계 러너 ──────────────────────────────────────────────────────────


def _disposition_of(decision: RoutingDecision) -> Disposition:
    if isinstance(decision, Routed):
        return "routed"
    if isinstance(decision, Contested):
        return "contested"
    return "unowned"


class ScaleFailureCase(BaseModel, frozen=True):
    """실패 케이스 한 건 — 기대 라벨과 실제 disposition·primary가 어긋난 근거."""

    question: str
    tier: Tier
    expected_disposition: Disposition
    expected_primary: str | None
    actual_disposition: Disposition
    actual_primary: str | None


class TierStats(BaseModel, frozen=True):
    """tier 하나에 대한 집계 통계."""

    total: int
    top1_accuracy: float
    misrouted_rate: float
    unowned_rate: float
    contested_rate: float


class ScaleEvalReport(BaseModel, frozen=True):
    """run_scale_eval의 산출물 — 전체·tier별 집계 + 실패 케이스 목록."""

    total: int
    overall_top1_accuracy: float
    misrouted_count: int
    misrouted_rate: float
    unowned_rate: float
    contested_rate: float
    by_tier: dict[str, TierStats]
    failures: tuple[ScaleFailureCase, ...]


def _is_top1_correct(entry: SampleQuestion, decision: RoutingDecision) -> bool:
    """기대 disposition·primary와 실제 decision이 일치하면 True.

    routed 기대 → 실제도 Routed이고 primary 일치.
    contested 기대 → 실제도 Contested.
    unowned 기대 → 실제도 Unowned.
    """
    actual = _disposition_of(decision)
    if entry.expected_disposition != actual:
        return False
    if entry.expected_disposition == "routed":
        assert isinstance(decision, Routed)
        return decision.primary.agent_id == entry.expected_primary
    return True


def run_scale_eval(
    router: TwoStageRouter, samples: Sequence[SampleQuestion]
) -> ScaleEvalReport:
    """samples 각각을 router.route()로 실행해 tier별·전체 집계 리포트를 만든다.

    집계: top-1 정확도(기대 disposition·primary 일치) / 오라우팅률(Routed인데
    primary 오답) / 0-매칭 escalation률(Unowned 비중) / contested률 / 실패 케이스.
    """
    failures: list[ScaleFailureCase] = []
    tier_buckets: dict[str, list[SampleQuestion]] = {"easy": [], "hard": [], "ambiguous": []}
    tier_correct: dict[str, int] = {"easy": 0, "hard": 0, "ambiguous": 0}
    tier_misrouted: dict[str, int] = {"easy": 0, "hard": 0, "ambiguous": 0}
    tier_unowned: dict[str, int] = {"easy": 0, "hard": 0, "ambiguous": 0}
    tier_contested: dict[str, int] = {"easy": 0, "hard": 0, "ambiguous": 0}

    overall_correct = 0
    overall_misrouted = 0
    overall_unowned = 0
    overall_contested = 0

    for entry in samples:
        decision = router.route(entry.question)
        actual = _disposition_of(decision)
        actual_primary = decision.primary.agent_id if isinstance(decision, Routed) else None
        correct = _is_top1_correct(entry, decision)

        tier_buckets[entry.tier].append(entry)
        if correct:
            overall_correct += 1
            tier_correct[entry.tier] += 1

        is_misrouted = actual == "routed" and (
            entry.expected_disposition != "routed"
            or actual_primary != entry.expected_primary
        )
        if is_misrouted:
            overall_misrouted += 1
            tier_misrouted[entry.tier] += 1

        if actual == "unowned":
            overall_unowned += 1
            tier_unowned[entry.tier] += 1

        if actual == "contested":
            overall_contested += 1
            tier_contested[entry.tier] += 1

        if not correct:
            failures.append(
                ScaleFailureCase(
                    question=entry.question,
                    tier=entry.tier,
                    expected_disposition=entry.expected_disposition,  # type: ignore[arg-type]
                    expected_primary=entry.expected_primary,
                    actual_disposition=actual,
                    actual_primary=actual_primary,
                )
            )

    total = len(samples)
    by_tier: dict[str, TierStats] = {}
    for tier in ("easy", "hard", "ambiguous"):
        n = len(tier_buckets[tier])
        by_tier[tier] = TierStats(
            total=n,
            top1_accuracy=(tier_correct[tier] / n) if n else 0.0,
            misrouted_rate=(tier_misrouted[tier] / n) if n else 0.0,
            unowned_rate=(tier_unowned[tier] / n) if n else 0.0,
            contested_rate=(tier_contested[tier] / n) if n else 0.0,
        )

    return ScaleEvalReport(
        total=total,
        overall_top1_accuracy=(overall_correct / total) if total else 0.0,
        misrouted_count=overall_misrouted,
        misrouted_rate=(overall_misrouted / total) if total else 0.0,
        unowned_rate=(overall_unowned / total) if total else 0.0,
        contested_rate=(overall_contested / total) if total else 0.0,
        by_tier=by_tier,
        failures=tuple(failures),
    )


# ── stage-1 후보 점수 margin 보조 함수(읽기 전용 재사용) ──────────────────────


class CandidateScore(BaseModel, frozen=True):
    """stage-1 후보 한 건의 점수 — margin 분석용(ConceptOverlapMatcher 직접 호출)."""

    agent_id: str
    score: float


def stage1_top_margin(
    matcher: ConceptOverlapMatcher, question: str, index_store: InMemoryPublishedIndexStore
) -> float | None:
    """matcher.match() 결과에서 top1-top2 점수 margin을 계산한다(읽기 전용 재사용).

    ConceptOverlapMatcher.match()는 IndexMatch(score 포함) 시퀀스를 그대로 노출하므로
    (index_matcher.py 확인 — score가 이미 공개 필드) 도메인 코드 수정 없이 매처를 직접
    호출해 top1·top2 score 차이를 뽑는다. 후보 0건 또는 1건이면 None(margin 정의 불가).
    """
    matches = matcher.match(question, index_store.all_indexes())
    if len(matches) < 2:
        return None
    return matches[0].score - matches[1].score
