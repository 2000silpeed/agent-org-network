"""Phase 13 SC1 — Owner Scorecard 도메인 코어 (ADR 0035·TRD §4 스코어카드 shape 절).

기존 append-only 스토어(`AnswerRecordStore`·`FeedbackStore`·`CorrectionStore`·
`KnowledgeStore`·`PresenceLogStore`)를 주입 조인해 owner별 4축 관찰 지표
(`OwnerScorecard`)를 계산하는 순수 함수(`compute_owner_scorecard`) + 자기 추세
(`scorecard_trend`)를 검증한다.

핵심 불변식(Goodhart 방지, ADR 0035 결정 1): 정정 발생은 감독 성실도 축(가점)이지
품질 벌점이 아니다 — 정정만 많고 bad 피드백 0인 owner는 quality.bad_feedback_rate==0
AND supervision.handled_rate가 높다(정정이 품질을 깎지 않음을 값으로 고정).

전부 결정론: Fake 스토어(In-Memory 구현 재사용) + 주입 clock/window. 실 LLM 0.
"""

from datetime import datetime, timedelta, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_record import (
    AnswerFeedback,
    AnswerRecord,
    CorrectionEvent,
    InMemoryAnswerRecordStore,
    InMemoryCorrectionStore,
    InMemoryFeedbackStore,
)
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.knowledge_sync import KnowledgeBundleContent
from agent_org_network.presence import InMemoryPresenceLogStore, PresenceEvent
from agent_org_network.scorecard import (
    OwnerScorecard,
    ScorecardWindow,
    compute_owner_scorecard,
    scorecard_trend,
)

BASE = datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone.utc)


def _t(days: int = 0, seconds: int = 0) -> datetime:
    return BASE + timedelta(days=days, seconds=seconds)


def _card(agent_id: str, owner: str = "alice") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="고객 지원 담당",
        domains=["cs"],
        last_reviewed_at=BASE.date(),
    )


def _record(
    record_id: str,
    *,
    agent_id: str = "cs_ops",
    answered_at: datetime = BASE,
    mode: str = "full",
    needs_correction_review: bool = False,
) -> AnswerRecord:
    return AnswerRecord(
        record_id=record_id,
        question="환불 문의",
        answer_text="답변 본문",
        answered_by="alice",
        agent_id=agent_id,
        mode=mode,  # type: ignore[arg-type]
        session_id=None,
        answered_at=answered_at,
        needs_correction_review=needs_correction_review,
    )


def _window(since: datetime = BASE, until: datetime = _t(days=30)) -> ScorecardWindow:
    return ScorecardWindow(since=since, until=until)


# ── ScorecardWindow ─────────────────────────────────────────────────────


def test_scorecard_window는_frozen_값_객체다():
    window = _window()
    assert window.since == BASE
    assert window.until == _t(days=30)


# ── compute_owner_scorecard — 빈 상태(스토어 전부 비어 있음) ────────────


def test_빈_스토어면_품질_감독_신선도_전부_0():
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=InMemoryAnswerRecordStore(),
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.owner_id == "alice"
    assert scorecard.quality.total_answers == 0
    assert scorecard.quality.bad_feedback_answers == 0
    assert scorecard.quality.bad_feedback_rate == 0.0
    assert scorecard.supervision.needs_review_total == 0
    assert scorecard.supervision.corrected_count == 0
    assert scorecard.supervision.handled_rate == 0.0
    assert scorecard.supervision.median_handle_seconds is None
    assert scorecard.freshness.total_cards == 1
    assert scorecard.freshness.stale_cards == 1  # 본문 없는 카드 = stale 취급
    assert scorecard.freshness.stale_ratio == 1.0
    assert scorecard.freshness.oldest_synced_elapsed_seconds is None


def test_presence_log_none이면_가용성_온라인_비율은_None():
    """SC2 전 하위호환 — presence_log 미주입이면 availability.online_ratio=None."""
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=InMemoryAnswerRecordStore(),
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.availability.online_ratio is None
    assert scorecard.availability.presend_review_count == 0


# ── 품질 축 — bad 피드백률 (agent_id 축 합산) ──────────────────────────


def test_품질_bad_피드백_있는_답이_분자():
    answer_store = InMemoryAnswerRecordStore()
    feedback_store = InMemoryFeedbackStore()
    answer_store.add(_record("r1", answered_at=_t(days=1)))
    answer_store.add(_record("r2", answered_at=_t(days=2)))
    feedback_store.upsert(
        AnswerFeedback(record_id="r1", verdict="bad", submitted_by="q1", submitted_at=_t(days=1))
    )
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=answer_store,
        feedback_store=feedback_store,
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.quality.total_answers == 2
    assert scorecard.quality.bad_feedback_answers == 1
    assert scorecard.quality.bad_feedback_rate == 0.5


def test_품질_최신_verdict가_good이면_분자에서_빠진다():
    """같은 답에 bad 이후 good으로 최신 갱신되면(upsert) 그 답은 bad 분자에서 빠진다."""
    answer_store = InMemoryAnswerRecordStore()
    feedback_store = InMemoryFeedbackStore()
    answer_store.add(_record("r1", answered_at=_t(days=1)))
    feedback_store.upsert(
        AnswerFeedback(record_id="r1", verdict="bad", submitted_by="q1", submitted_at=_t(days=1))
    )
    feedback_store.upsert(
        AnswerFeedback(
            record_id="r1", verdict="good", submitted_by="q1", submitted_at=_t(days=1, seconds=10)
        )
    )
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=answer_store,
        feedback_store=feedback_store,
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.quality.bad_feedback_answers == 0
    assert scorecard.quality.bad_feedback_rate == 0.0


def test_품질_window_밖_answered_at은_제외된다():
    answer_store = InMemoryAnswerRecordStore()
    answer_store.add(_record("r1", answered_at=_t(days=1)))
    answer_store.add(_record("r2", answered_at=_t(days=-5)))  # window 밖(since 이전)
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=answer_store,
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.quality.total_answers == 1


# ── 감독 축 — 처리율·처리 소요시간 (가점 축, Goodhart) ─────────────────


def test_감독_정정된_검토필요_항목이_분자():
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    answer_store.add(
        _record("r1", answered_at=_t(days=1), needs_correction_review=True)
    )
    answer_store.add(
        _record("r2", answered_at=_t(days=1), needs_correction_review=True)
    )
    correction_store.append(
        CorrectionEvent(
            event_id="c1",
            record_id="r1",
            corrected_text="정정본",
            by_owner="alice",
            corrected_at=_t(days=1, seconds=3600),
        )
    )
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=answer_store,
        feedback_store=InMemoryFeedbackStore(),
        correction_store=correction_store,
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.supervision.needs_review_total == 2
    assert scorecard.supervision.corrected_count == 1
    assert scorecard.supervision.handled_rate == 0.5
    assert scorecard.supervision.median_handle_seconds == 3600.0


def test_감독_처리_소요시간_중앙값():
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    answer_store.add(_record("r1", answered_at=_t(days=1), needs_correction_review=True))
    answer_store.add(_record("r2", answered_at=_t(days=1), needs_correction_review=True))
    answer_store.add(_record("r3", answered_at=_t(days=1), needs_correction_review=True))
    correction_store.append(
        CorrectionEvent(
            event_id="c1", record_id="r1", corrected_text="x", by_owner="alice",
            corrected_at=_t(days=1, seconds=100),
        )
    )
    correction_store.append(
        CorrectionEvent(
            event_id="c2", record_id="r2", corrected_text="x", by_owner="alice",
            corrected_at=_t(days=1, seconds=200),
        )
    )
    correction_store.append(
        CorrectionEvent(
            event_id="c3", record_id="r3", corrected_text="x", by_owner="alice",
            corrected_at=_t(days=1, seconds=600),
        )
    )
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=answer_store,
        feedback_store=InMemoryFeedbackStore(),
        correction_store=correction_store,
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.supervision.median_handle_seconds == 200.0


# ── Goodhart 핵심 단언 — 정정만 많고 bad 피드백 0인 owner ──────────────


def test_goodhart_정정만_많고_bad_피드백_0이면_품질_안_나쁘고_감독_높다():
    """ADR 0035 결정 1 — 정정 발생률은 감독 성실도 축(가점)이지 품질 벌점이 아니다.

    이 owner는 검토 필요 항목을 전부 정정했지만(감독 성실) bad 피드백은 0건이다.
    두 축이 코드에서 분리돼 있어야 quality.bad_feedback_rate==0 AND
    supervision.handled_rate가 높게 나온다(정정이 품질을 깎지 않음을 값으로 고정).
    """
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    feedback_store = InMemoryFeedbackStore()
    for i in range(5):
        answer_store.add(
            _record(f"r{i}", answered_at=_t(days=1, seconds=i), needs_correction_review=True)
        )
        correction_store.append(
            CorrectionEvent(
                event_id=f"c{i}",
                record_id=f"r{i}",
                corrected_text="정정본",
                by_owner="alice",
                corrected_at=_t(days=1, seconds=i + 60),
            )
        )
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=answer_store,
        feedback_store=feedback_store,
        correction_store=correction_store,
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.quality.bad_feedback_rate == 0.0
    assert scorecard.supervision.handled_rate == 1.0


# ── 가용성 축 — 온라인 비율(SC2 소비) + 사전 검토 응답 수 ──────────────


def test_가용성_presence_log_있으면_online_ratio_계산():
    presence_log = InMemoryPresenceLogStore()
    presence_log.append(PresenceEvent(owner_id="alice", status="online", at=BASE))
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=InMemoryAnswerRecordStore(),
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        presence_log=presence_log,
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.availability.online_ratio == 1.0


def test_가용성_사전_검토_응답_수는_draft_only_모드_근사():
    answer_store = InMemoryAnswerRecordStore()
    answer_store.add(_record("r1", answered_at=_t(days=1), mode="draft_only"))
    answer_store.add(_record("r2", answered_at=_t(days=1), mode="full"))
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=answer_store,
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.availability.presend_review_count == 1


# ── 신선도 축 — stale 비율·최고참 synced_at 경과 ────────────────────────


def test_신선도_본문_있으면_stale_아님():
    knowledge_store = InMemoryKnowledgeStore()
    knowledge_store.put(
        KnowledgeBundleContent(
            agent_id="cs_ops", documents=(), version="v1", synced_at=_t(days=29)
        )
    )
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=InMemoryAnswerRecordStore(),
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=knowledge_store,
        window=_window(),
        now=_t(days=29, seconds=10),
    )
    assert scorecard.freshness.stale_cards == 0
    assert scorecard.freshness.stale_ratio == 0.0
    assert scorecard.freshness.oldest_synced_elapsed_seconds == 10.0


def test_신선도_여러_카드_중_최고참_synced_at_경과():
    knowledge_store = InMemoryKnowledgeStore()
    knowledge_store.put(
        KnowledgeBundleContent(agent_id="cs_a", documents=(), version="v1", synced_at=_t(days=0))
    )
    knowledge_store.put(
        KnowledgeBundleContent(agent_id="cs_b", documents=(), version="v1", synced_at=_t(days=10))
    )
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_a"), _card("cs_b")],
        answer_store=InMemoryAnswerRecordStore(),
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=knowledge_store,
        window=_window(),
        now=_t(days=20),
    )
    # 최고참(가장 오래된) synced_at = day0 → now(day20)까지 20일 경과.
    assert scorecard.freshness.oldest_synced_elapsed_seconds == timedelta(days=20).total_seconds()


# ── 스코핑 — cards의 owner가 다르면 그 카드는 계산에서 제외되지 않는다
#    (호출자가 이미 필터해 넘긴다는 계약 — 여기선 카드 목록을 그대로 신뢰) ──


def test_cards가_여러_agent_id면_agent_id_축으로_합산된다():
    answer_store = InMemoryAnswerRecordStore()
    answer_store.add(_record("r1", agent_id="cs_a", answered_at=_t(days=1)))
    answer_store.add(_record("r2", agent_id="cs_b", answered_at=_t(days=1)))
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_a"), _card("cs_b")],
        answer_store=answer_store,
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert scorecard.quality.total_answers == 2


# ── 자기 추세 — scorecard_trend(current, previous) — 절대 비교 로직 부재 ──


def test_scorecard_trend는_두_기간_독립_계산의_델타만_낸다():
    answer_store_current = InMemoryAnswerRecordStore()
    answer_store_current.add(_record("r1", answered_at=_t(days=31)))
    answer_store_current.add(_record("r2", answered_at=_t(days=31)))
    current = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=answer_store_current,
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=ScorecardWindow(since=_t(days=30), until=_t(days=60)),
        now=_t(days=60),
    )

    answer_store_previous = InMemoryAnswerRecordStore()
    answer_store_previous.add(_record("r0", answered_at=_t(days=1)))
    previous = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=answer_store_previous,
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=ScorecardWindow(since=BASE, until=_t(days=30)),
        now=_t(days=30),
    )

    trend = scorecard_trend(current, previous)
    assert trend.quality_total_answers_delta == 1  # 2 - 1
    assert trend.owner_id == "alice"


def test_scorecard_trend에는_절대_순위_랭킹_필드가_없다():
    """ADR 0035 결정 2 — 절대 등수/랭킹 함수/필드가 코드에 존재하지 않는다."""
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=InMemoryAnswerRecordStore(),
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    trend = scorecard_trend(scorecard, scorecard)
    trend_fields = set(vars(trend).keys()) if hasattr(trend, "__dict__") else set(
        type(trend).model_fields.keys()
    )
    forbidden = {"rank", "ranking", "leaderboard", "position", "percentile"}
    assert not (trend_fields & forbidden)


# ── OwnerScorecard 자체 — 정정 축과 품질 축 타입 분리(ADR 0035 결정 1) ──


def test_ownerscorecard는_frozen_이고_4축이_별_타입이다():
    scorecard = compute_owner_scorecard(
        owner_id="alice",
        cards=[_card("cs_ops")],
        answer_store=InMemoryAnswerRecordStore(),
        feedback_store=InMemoryFeedbackStore(),
        correction_store=InMemoryCorrectionStore(),
        knowledge_store=InMemoryKnowledgeStore(),
        window=_window(),
        now=_t(days=30),
    )
    assert isinstance(scorecard, OwnerScorecard)
    assert type(scorecard.quality) is not type(scorecard.supervision)
    assert scorecard.weak_identity_note is True
    import pydantic

    try:
        scorecard.owner_id = "bob"  # type: ignore[misc]
    except (pydantic.ValidationError, AttributeError, TypeError):
        pass
    else:
        raise AssertionError("frozen 값 객체가 수정을 허용함")
