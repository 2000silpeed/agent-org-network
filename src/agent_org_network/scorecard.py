"""Owner Scorecard — 담당자 관찰 지표 (Phase 13 SC1, ADR 0035·TRD §4 스코어카드 shape 절).

Phase 12 감독 루프(`AnswerRecord`·`CorrectionEvent`·`AnswerFeedback`·`Presence`·
`KnowledgeStore`) 위에 얹는 **읽기 파생** 지표다. 새 전이·기록 0(순수 함수 + 기존
스토어 포트 읽기만) — `OwnerScorecard`는 4축(quality·supervision·availability·
freshness) frozen 값 객체 조합이고, `compute_owner_scorecard`가 그 축을 채운다.

Goodhart 방지(ADR 0035 결정 1, 핵심 불변식): 정정 발생(`CorrectionEvent`)은
**감독 성실도 축**(가점)으로만 카운트되고, 품질 벌점 신호는 **`AnswerFeedback`의
bad 피드백률에서만** 읽는다. 두 신호는 타입 단위로 분리된 축(`QualityMetric` vs
`SupervisionMetric`)에 담겨, 정정이 아무리 많아도 품질 축을 깎지 않는다.

자기 추세(ADR 0035 결정 2): `scorecard_trend`는 두 기간을 **독립적으로 계산한**
`OwnerScorecard` 두 개를 받아 델타만 낸다 — 함수 내부에 절대 비교·순위 로직이
없다(오너 간 순위표는 코드 부재로 강제).
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from agent_org_network.agent_card import AgentCard
    from agent_org_network.answer_record import AnswerRecordStore, CorrectionStore, FeedbackStore
    from agent_org_network.knowledge_store import KnowledgeStore
    from agent_org_network.presence import PresenceLogStore


class ScorecardWindow(BaseModel, frozen=True):
    """지표 집계 기간 — rolling 30일 기본(호출측이 `AON_SCORECARD_WINDOW_DAYS`로 시임)."""

    since: datetime
    until: datetime


class QualityMetric(BaseModel, frozen=True):
    """품질 축 — bad 피드백률만 벌점 신호(정정률 미포함, ADR 0035 결정 1)."""

    total_answers: int
    bad_feedback_answers: int
    bad_feedback_rate: float


class SupervisionMetric(BaseModel, frozen=True):
    """감독 축 — 정정은 여기서만 카운트되는 가점 신호(ADR 0035 결정 1)."""

    needs_review_total: int
    corrected_count: int
    handled_rate: float
    median_handle_seconds: float | None


class AvailabilityMetric(BaseModel, frozen=True):
    """가용성 축 — 온라인 비율(SC2 소비, 미주입이면 None) + 사전 검토 응답 근사."""

    online_ratio: float | None
    presend_review_count: int


class FreshnessMetric(BaseModel, frozen=True):
    """신선도 축 — stale 카드 비율 + 최고참 synced_at 경과."""

    total_cards: int
    stale_cards: int
    stale_ratio: float
    oldest_synced_elapsed_seconds: float | None


class OwnerScorecard(BaseModel, frozen=True):
    """담당자별 4축 관찰 지표 — 정정 축(가점)과 품질 축(bad)이 타입 단위로 분리된다."""

    owner_id: str
    window: ScorecardWindow
    quality: QualityMetric
    supervision: SupervisionMetric
    availability: AvailabilityMetric
    freshness: FreshnessMetric
    weak_identity_note: bool = True


def compute_owner_scorecard(
    *,
    owner_id: str,
    cards: list["AgentCard"],
    answer_store: "AnswerRecordStore",
    feedback_store: "FeedbackStore",
    correction_store: "CorrectionStore",
    knowledge_store: "KnowledgeStore",
    presence_log: "PresenceLogStore | None" = None,
    window: ScorecardWindow,
    now: datetime,
) -> OwnerScorecard:
    """owner 소유 카드들(호출자가 이미 필터해 넘긴 `cards`)을 조인해 4축 지표를 낸다.

    품질·감독·신선도는 `cards`의 각 `agent_id` 축을 훑어 합산하고, 가용성은
    `owner_id` 직접(프레즌스는 owner PC 연결 — 카드 단위가 아니다).
    """
    since, until = window.since, window.until

    records = [
        rec
        for card in cards
        for rec in answer_store.for_agent(card.agent_id)
        if since <= rec.answered_at < until
    ]

    total_answers = len(records)
    bad_feedback_answers = 0
    presend_review_count = 0
    for rec in records:
        if rec.mode == "draft_only":
            presend_review_count += 1
        latest = feedback_store.latest_for_record(rec.record_id)
        if latest is not None and latest.verdict == "bad":
            bad_feedback_answers += 1
    bad_feedback_rate = bad_feedback_answers / total_answers if total_answers else 0.0

    quality = QualityMetric(
        total_answers=total_answers,
        bad_feedback_answers=bad_feedback_answers,
        bad_feedback_rate=bad_feedback_rate,
    )

    needs_review_records = [rec for rec in records if rec.needs_correction_review]
    needs_review_total = len(needs_review_records)
    corrected_count = 0
    handle_seconds: list[float] = []
    for rec in needs_review_records:
        events = [
            e
            for e in correction_store.for_record(rec.record_id)
            if since <= e.corrected_at < until
        ]
        if not events:
            continue
        corrected_count += 1
        earliest = min(events, key=lambda e: e.corrected_at)
        handle_seconds.append((earliest.corrected_at - rec.answered_at).total_seconds())
    handled_rate = corrected_count / needs_review_total if needs_review_total else 0.0
    median_handle_seconds = median(handle_seconds) if handle_seconds else None

    supervision = SupervisionMetric(
        needs_review_total=needs_review_total,
        corrected_count=corrected_count,
        handled_rate=handled_rate,
        median_handle_seconds=median_handle_seconds,
    )

    online_ratio: float | None = None
    if presence_log is not None:
        from agent_org_network.presence import online_ratio as _online_ratio

        events = presence_log.for_owner(owner_id)
        online_ratio = _online_ratio(events, since=since, until=until)

    availability = AvailabilityMetric(
        online_ratio=online_ratio,
        presend_review_count=presend_review_count,
    )

    total_cards = len(cards)
    stale_cards = 0
    oldest_elapsed: float | None = None
    threshold_s = _stale_threshold_s()
    for card in cards:
        content = knowledge_store.get(card.agent_id)
        if content is not None:
            elapsed = (now - content.synced_at).total_seconds()
            if oldest_elapsed is None or elapsed > oldest_elapsed:
                oldest_elapsed = elapsed
        if knowledge_store.is_stale(card.agent_id, now=now, threshold_s=threshold_s):
            stale_cards += 1
    stale_ratio = stale_cards / total_cards if total_cards else 0.0

    freshness = FreshnessMetric(
        total_cards=total_cards,
        stale_cards=stale_cards,
        stale_ratio=stale_ratio,
        oldest_synced_elapsed_seconds=oldest_elapsed,
    )

    return OwnerScorecard(
        owner_id=owner_id,
        window=window,
        quality=quality,
        supervision=supervision,
        availability=availability,
        freshness=freshness,
    )


def _stale_threshold_s() -> int:
    from agent_org_network.knowledge_store import knowledge_stale_seconds

    return knowledge_stale_seconds()


class ScorecardTrend(BaseModel, frozen=True):
    """두 기간(현재·직전) `OwnerScorecard`의 축별 델타 — 절대 순위 로직 없음(ADR 0035 결정 2)."""

    owner_id: str
    quality_total_answers_delta: int
    quality_bad_feedback_rate_delta: float
    supervision_handled_rate_delta: float
    freshness_stale_ratio_delta: float


def scorecard_trend(current: OwnerScorecard, previous: OwnerScorecard) -> ScorecardTrend:
    """두 독립 계산된 `OwnerScorecard`의 델타만 낸다 — 호출 조립, 절대 비교 로직 부재."""
    return ScorecardTrend(
        owner_id=current.owner_id,
        quality_total_answers_delta=(
            current.quality.total_answers - previous.quality.total_answers
        ),
        quality_bad_feedback_rate_delta=(
            current.quality.bad_feedback_rate - previous.quality.bad_feedback_rate
        ),
        supervision_handled_rate_delta=(
            current.supervision.handled_rate - previous.supervision.handled_rate
        ),
        freshness_stale_ratio_delta=(
            current.freshness.stale_ratio - previous.freshness.stale_ratio
        ),
    )


__all__ = [
    "ScorecardWindow",
    "QualityMetric",
    "SupervisionMetric",
    "AvailabilityMetric",
    "FreshnessMetric",
    "OwnerScorecard",
    "compute_owner_scorecard",
    "ScorecardTrend",
    "scorecard_trend",
]
