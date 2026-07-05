"""담당자 모니터링 + 사후 교정 루프 (Phase 12 S5, ADR 0033 결정 4·S1 shape 절).

`AnswerRecord`/`CorrectionEvent` 값 객체·포트+InMemory + 정정 상태기계
(`CorrectionService`) + 담당자 모니터링 조회(`monitoring_for_owner`) + 질문자 측
정정 배지 조회(`view_answer_with_correction`) 결정론 코어.

범위: 결정론(주입 격리)만 — 실 UI·실 통지 채널·실 WS 프레즌스 배선은 밖
(mcp-runtime-engineer 몫).

불변식:
  - 전이 ≠ 기록 — 정정은 원 `AnswerRecord`를 수정하지 않고 새 `CorrectionEvent`를
    append한다(원 레코드 불변 단언 필수).
  - 멱등 — 같은 정정 재제출이 이벤트를 중복 적재하지 않는다.
  - owner 스코핑 — 담당자는 자기 에이전트 것만 본다(남의 에이전트 정정 시도 거부).
  - reeval 적재 — 정정된 지식이 판례/라우팅 재평가로 이어지는 고리(ADR 0033 결정 4).
  - `needs_correction_review` 플래그 소비 — S2/S4가 전달까지 배선해 둔 플래그를
    `AnswerRecord`에 기록하고 담당자 모니터링 조회에서 "검토 필요" 필터로 노출한다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from agent_org_network.answer_record import (
    AnswerFeedback,
    AnswerRecord,
    CorrectionEvent,
    CorrectionService,
    InMemoryAnswerRecordStore,
    InMemoryCorrectionStore,
    InMemoryFeedbackStore,
    monitoring_for_owner,
    view_answer_with_correction,
)
from agent_org_network.reeval import AnswerSubject, InMemoryReevalStore
from agent_org_network.runtime import AnswerMode

_T0 = datetime(2026, 7, 4, 9, 0, 0, tzinfo=timezone.utc)


def _record(
    *,
    record_id: str = "rec-1",
    question: str = "환불 정책이 뭔가요?",
    answer_text: str = "7일 이내 환불 가능합니다.",
    answered_by: str = "alice",
    agent_id: str = "refund-bot",
    mode: AnswerMode = "full",
    session_id: str | None = "session-1",
    answered_at: datetime = _T0,
    needs_correction_review: bool = False,
) -> AnswerRecord:
    return AnswerRecord(
        record_id=record_id,
        question=question,
        answer_text=answer_text,
        answered_by=answered_by,
        agent_id=agent_id,
        mode=mode,
        session_id=session_id,
        answered_at=answered_at,
        needs_correction_review=needs_correction_review,
    )


# ── AnswerRecord 값 객체 ──────────────────────────────────────────────────


def test_answer_record는_frozen_값_객체다():
    rec = _record()
    assert rec.record_id == "rec-1"
    assert rec.question == "환불 정책이 뭔가요?"
    assert rec.answer_text == "7일 이내 환불 가능합니다."
    assert rec.answered_by == "alice"
    assert rec.agent_id == "refund-bot"
    assert rec.mode == "full"
    assert rec.session_id == "session-1"
    assert rec.answered_at == _T0
    assert rec.needs_correction_review is False


def test_answer_record는_수정_불가():
    import pydantic

    rec = _record()
    with pytest.raises((pydantic.ValidationError, AttributeError, TypeError)):
        rec.answer_text = "변조 시도"  # type: ignore[misc]


def test_needs_correction_review_기본값은_False():
    rec = AnswerRecord(
        record_id="rec-2",
        question="q",
        answer_text="a",
        answered_by="alice",
        agent_id="refund-bot",
        mode="full",
        session_id=None,
        answered_at=_T0,
    )
    assert rec.needs_correction_review is False


# ── AnswerRecordStore ─────────────────────────────────────────────────────


def test_answer_record_store_add_get():
    store = InMemoryAnswerRecordStore()
    rec = _record()
    store.add(rec)
    assert store.get("rec-1") == rec


def test_answer_record_store_미존재는_None():
    store = InMemoryAnswerRecordStore()
    assert store.get("nope") is None


def test_answer_record_store_for_agent_그_에이전트만():
    store = InMemoryAnswerRecordStore()
    store.add(_record(record_id="rec-1", agent_id="refund-bot"))
    store.add(_record(record_id="rec-2", agent_id="billing-bot"))
    store.add(_record(record_id="rec-3", agent_id="refund-bot"))

    recs = store.for_agent("refund-bot")

    assert {r.record_id for r in recs} == {"rec-1", "rec-3"}


# ── CorrectionEvent 값 객체 ───────────────────────────────────────────────


def test_correction_event는_frozen_값_객체다():
    event = CorrectionEvent(
        event_id="evt-1",
        record_id="rec-1",
        corrected_text="14일 이내 환불 가능합니다.",
        by_owner="alice",
        rationale="정책 갱신 반영 누락",
        corrected_at=_T0,
    )
    assert event.event_id == "evt-1"
    assert event.record_id == "rec-1"
    assert event.corrected_text == "14일 이내 환불 가능합니다."
    assert event.by_owner == "alice"
    assert event.rationale == "정책 갱신 반영 누락"
    assert event.corrected_at == _T0


def test_correction_event는_수정_불가():
    import pydantic

    event = CorrectionEvent(
        event_id="evt-1",
        record_id="rec-1",
        corrected_text="14일 이내 환불 가능합니다.",
        by_owner="alice",
        corrected_at=_T0,
    )
    with pytest.raises((pydantic.ValidationError, AttributeError, TypeError)):
        event.corrected_text = "변조 시도"  # type: ignore[misc]


def test_correction_event_rationale_기본값은_빈문자열():
    event = CorrectionEvent(
        event_id="evt-1",
        record_id="rec-1",
        corrected_text="14일 이내 환불 가능합니다.",
        by_owner="alice",
        corrected_at=_T0,
    )
    assert event.rationale == ""


# ── CorrectionStore ───────────────────────────────────────────────────────


def test_correction_store_append_for_record():
    store = InMemoryCorrectionStore()
    event = CorrectionEvent(
        event_id="evt-1",
        record_id="rec-1",
        corrected_text="14일 이내 환불 가능합니다.",
        by_owner="alice",
        corrected_at=_T0,
    )
    store.append(event)
    assert store.for_record("rec-1") == [event]


def test_correction_store_미정정_레코드는_빈리스트():
    store = InMemoryCorrectionStore()
    assert store.for_record("nope") == []


def test_correction_store_같은_레코드_여러_정정_순서_보존():
    store = InMemoryCorrectionStore()
    event1 = CorrectionEvent(
        event_id="evt-1",
        record_id="rec-1",
        corrected_text="1차 정정",
        by_owner="alice",
        corrected_at=_T0,
    )
    event2 = CorrectionEvent(
        event_id="evt-2",
        record_id="rec-1",
        corrected_text="2차 정정",
        by_owner="alice",
        corrected_at=_T0 + timedelta(minutes=5),
    )
    store.append(event1)
    store.append(event2)
    assert [e.event_id for e in store.for_record("rec-1")] == ["evt-1", "evt-2"]


# ── CorrectionService — 정정 상태기계 (전이 ≠ 기록·멱등·owner 스코핑) ─────


def _service() -> tuple[
    CorrectionService, InMemoryAnswerRecordStore, InMemoryCorrectionStore, InMemoryReevalStore
]:
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    reeval_store = InMemoryReevalStore()
    service = CorrectionService(
        answer_store=answer_store,
        correction_store=correction_store,
        reeval_store=reeval_store,
    )
    return service, answer_store, correction_store, reeval_store


def test_정정_제출하면_새_이벤트가_append된다():
    service, answer_store, correction_store, _ = _service()
    answer_store.add(_record())

    event = service.submit_correction(
        record_id="rec-1",
        by_owner="alice",
        corrected_text="14일 이내 환불 가능합니다.",
        rationale="정책 갱신",
        at=_T0 + timedelta(minutes=5),
    )

    assert event.corrected_text == "14일 이내 환불 가능합니다."
    assert correction_store.for_record("rec-1") == [event]


def test_정정해도_원_answer_record는_불변이다():
    """전이 ≠ 기록 핵심 불변식 — 원 레코드 수정 금지."""
    service, answer_store, _, _ = _service()
    original = _record()
    answer_store.add(original)

    service.submit_correction(
        record_id="rec-1",
        by_owner="alice",
        corrected_text="14일 이내 환불 가능합니다.",
        at=_T0 + timedelta(minutes=5),
    )

    stored = answer_store.get("rec-1")
    assert stored == original
    assert stored is not None
    assert stored.answer_text == "7일 이내 환불 가능합니다."


def test_정정_제출은_reeval_아이템을_적재한다():
    """ADR 0033 결정 4 — 정정된 지식이 판례/라우팅 재평가로 이어지는 고리(reeval 재사용)."""
    service, answer_store, _, reeval_store = _service()
    answer_store.add(_record())

    service.submit_correction(
        record_id="rec-1",
        by_owner="alice",
        corrected_text="14일 이내 환불 가능합니다.",
        at=_T0 + timedelta(minutes=5),
    )

    pending = reeval_store.pending_for_owner("alice")
    assert len(pending) == 1
    assert isinstance(pending[0].subject, AnswerSubject)
    assert pending[0].agent_id == "refund-bot"


def test_존재하지_않는_레코드_정정은_ValueError():
    service, _, _, _ = _service()
    with pytest.raises(ValueError):
        service.submit_correction(
            record_id="nope",
            by_owner="alice",
            corrected_text="x",
            at=_T0,
        )


def test_남의_에이전트_정정_시도는_거부된다():
    """owner 스코핑 — 담당자는 자기 에이전트 것만 정정 가능."""
    service, answer_store, _, _ = _service()
    answer_store.add(_record(answered_by="alice"))

    with pytest.raises(ValueError):
        service.submit_correction(
            record_id="rec-1",
            by_owner="bob",
            corrected_text="가로채기 시도",
            at=_T0,
        )


def test_같은_정정_재제출은_이벤트를_중복_적재하지_않는다():
    """멱등 — 같은 정정(record_id+corrected_text+by_owner) 재제출이 중복 append 안 됨."""
    service, answer_store, correction_store, reeval_store = _service()
    answer_store.add(_record())

    event1 = service.submit_correction(
        record_id="rec-1",
        by_owner="alice",
        corrected_text="14일 이내 환불 가능합니다.",
        at=_T0 + timedelta(minutes=5),
    )
    event2 = service.submit_correction(
        record_id="rec-1",
        by_owner="alice",
        corrected_text="14일 이내 환불 가능합니다.",
        at=_T0 + timedelta(minutes=10),
    )

    assert event1.event_id == event2.event_id
    assert len(correction_store.for_record("rec-1")) == 1
    assert len(reeval_store.pending_for_owner("alice")) == 1


def test_다른_정정_내용이면_새_이벤트가_추가된다():
    service, answer_store, correction_store, _ = _service()
    answer_store.add(_record())

    service.submit_correction(
        record_id="rec-1", by_owner="alice", corrected_text="1차 정정", at=_T0
    )
    service.submit_correction(
        record_id="rec-1",
        by_owner="alice",
        corrected_text="2차 정정",
        at=_T0 + timedelta(minutes=5),
    )

    assert len(correction_store.for_record("rec-1")) == 2


# ── needs_correction_review 플래그 소비 ───────────────────────────────────


def test_needs_correction_review_True_레코드는_검토_필요_목록에_뜬다():
    store = InMemoryAnswerRecordStore()
    store.add(_record(record_id="rec-1", needs_correction_review=True))
    store.add(_record(record_id="rec-2", needs_correction_review=False))

    view = monitoring_for_owner(store, correction_store=InMemoryCorrectionStore(), agent_id="refund-bot")

    needing_review = [item for item in view if item.needs_correction_review]
    assert [item.record.record_id for item in needing_review] == ["rec-1"]


def test_오프라인_자동발신_플래그가_모니터링_검토_필요_목록에_등장한다():
    """S2/S4가 전달까지 배선한 needs_correction_review 플래그 소비 종단 확인."""
    store = InMemoryAnswerRecordStore()
    store.add(_record(record_id="rec-offline", needs_correction_review=True))

    view = monitoring_for_owner(store, correction_store=InMemoryCorrectionStore(), agent_id="refund-bot")

    assert any(item.record.record_id == "rec-offline" and item.needs_correction_review for item in view)


# ── 담당자 모니터링 조회 (owner 스코핑) ───────────────────────────────────


def test_모니터링은_그_에이전트_레코드만_반환한다():
    store = InMemoryAnswerRecordStore()
    store.add(_record(record_id="rec-1", agent_id="refund-bot"))
    store.add(_record(record_id="rec-2", agent_id="billing-bot"))

    view = monitoring_for_owner(store, correction_store=InMemoryCorrectionStore(), agent_id="refund-bot")

    assert [item.record.record_id for item in view] == ["rec-1"]


def test_모니터링은_정정_이력을_함께_투영한다():
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    answer_store.add(_record(record_id="rec-1"))
    event = CorrectionEvent(
        event_id="evt-1",
        record_id="rec-1",
        corrected_text="14일 이내 환불 가능합니다.",
        by_owner="alice",
        corrected_at=_T0 + timedelta(minutes=5),
    )
    correction_store.append(event)

    view = monitoring_for_owner(answer_store, correction_store=correction_store, agent_id="refund-bot")

    assert view[0].corrections == [event]


# ── 질문자 측 정정 배지 조회 (풀 방식, 원문+정정본 보존) ──────────────────


def test_정정_전에는_배지_없음_원문만():
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    answer_store.add(_record(record_id="rec-1", answer_text="7일 이내 환불 가능합니다."))

    view = view_answer_with_correction(answer_store, correction_store, record_id="rec-1")

    assert view is not None
    assert view.original_text == "7일 이내 환불 가능합니다."
    assert view.has_correction is False
    assert view.corrected_text is None


def test_정정_후에는_배지와_정정본이_함께_반환된다():
    """원문과 정정본이 둘 다 보존돼 반환되는지 단언(ADR 0033 결정 4 풀 방식)."""
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    answer_store.add(_record(record_id="rec-1", answer_text="7일 이내 환불 가능합니다."))
    correction_store.append(
        CorrectionEvent(
            event_id="evt-1",
            record_id="rec-1",
            corrected_text="14일 이내 환불 가능합니다.",
            by_owner="alice",
            corrected_at=_T0 + timedelta(minutes=5),
        )
    )

    view = view_answer_with_correction(answer_store, correction_store, record_id="rec-1")

    assert view is not None
    assert view.original_text == "7일 이내 환불 가능합니다."
    assert view.has_correction is True
    assert view.corrected_text == "14일 이내 환불 가능합니다."


def test_여러_정정_중_최신_정정본이_배지에_반영된다():
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    answer_store.add(_record(record_id="rec-1"))
    correction_store.append(
        CorrectionEvent(
            event_id="evt-1",
            record_id="rec-1",
            corrected_text="1차 정정",
            by_owner="alice",
            corrected_at=_T0,
        )
    )
    correction_store.append(
        CorrectionEvent(
            event_id="evt-2",
            record_id="rec-1",
            corrected_text="2차 정정(최신)",
            by_owner="alice",
            corrected_at=_T0 + timedelta(minutes=10),
        )
    )

    view = view_answer_with_correction(answer_store, correction_store, record_id="rec-1")

    assert view is not None
    assert view.corrected_text == "2차 정정(최신)"


# ── 정정 권한 판정 = 현재 카드 owner 기준 (ADR 0034 결정 3) ─────────────────


def _service_with_owner_of(
    owner_of: "Callable[[str], str | None]",
) -> tuple[CorrectionService, InMemoryAnswerRecordStore, InMemoryReevalStore]:
    answer_store = InMemoryAnswerRecordStore()
    reeval_store = InMemoryReevalStore()
    service = CorrectionService(
        answer_store=answer_store,
        correction_store=InMemoryCorrectionStore(),
        reeval_store=reeval_store,
        owner_of=owner_of,
    )
    return service, answer_store, reeval_store


def test_현재_카드_owner가_정정할_수_있다():
    """오너 변경 후 새 owner(현재 카드 owner)가 과거 답을 정정 가능(ADR 0034 결정 3)."""
    # 과거 답변자는 alice지만 현재 카드 owner는 bob(오너 변경됨).
    service, answer_store, _ = _service_with_owner_of(lambda agent_id: "bob")
    answer_store.add(_record(answered_by="alice", agent_id="refund-bot"))

    event = service.submit_correction(
        record_id="rec-1",
        by_owner="bob",  # 새 owner
        corrected_text="새 owner 정정",
        at=_T0,
    )
    assert event.by_owner == "bob"
    # 과거 기록(answered_by)은 불변(전이 ≠ 기록).
    stored = answer_store.get("rec-1")
    assert stored is not None and stored.answered_by == "alice"


def test_구_owner는_현재_카드_owner가_아니면_거부된다():
    """오너 변경 후 구 owner(과거 답변자)는 더 이상 정정 불가(ADR 0034 결정 3)."""
    service, answer_store, _ = _service_with_owner_of(lambda agent_id: "bob")
    answer_store.add(_record(answered_by="alice", agent_id="refund-bot"))

    with pytest.raises(ValueError):
        service.submit_correction(
            record_id="rec-1",
            by_owner="alice",  # 구 owner — 이제 카드 owner 아님
            corrected_text="구 owner 정정 시도",
            at=_T0,
        )


def test_카드가_사라지면_정정_불가():
    """owner_of가 None(카드 폐기)이면 판정 원천 부재 — 정정 불가(불변식 안전)."""
    service, answer_store, _ = _service_with_owner_of(lambda agent_id: None)
    answer_store.add(_record(answered_by="alice", agent_id="refund-bot"))

    with pytest.raises(ValueError):
        service.submit_correction(
            record_id="rec-1",
            by_owner="alice",
            corrected_text="x",
            at=_T0,
        )


def test_오너_변경_후_정정하면_reeval이_새_owner_처리함에_뜬다():
    """code-reviewer m-3: reeval 귀속은 정정자(event.by_owner) 기준이어야 한다.

    `record.answered_by`(구 owner)로 귀속하면 새 owner가 정정해도 reeval이 구 owner
    처리함에 떠(ADR 0034 결정 3의 "정정 권한=현재 owner" 취지가 reeval에서 무력화).
    """
    service, answer_store, reeval_store = _service_with_owner_of(lambda agent_id: "bob")
    answer_store.add(_record(answered_by="alice", agent_id="refund-bot"))

    service.submit_correction(
        record_id="rec-1",
        by_owner="bob",  # 새 owner가 정정
        corrected_text="새 owner 정정",
        at=_T0,
    )

    assert len(reeval_store.pending_for_owner("bob")) == 1
    assert reeval_store.pending_for_owner("alice") == []


def test_존재하지_않는_레코드_조회는_None():
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    assert view_answer_with_correction(answer_store, correction_store, record_id="nope") is None


# ── AnswerFeedback 값 객체 + FeedbackStore (plan §10.2) ──────────────────


def _feedback(
    *,
    record_id: str = "rec-1",
    verdict: str = "good",
    comment: str = "",
    submitted_by: str = "uid-1",
    submitted_at: datetime = _T0,
) -> AnswerFeedback:
    return AnswerFeedback(
        record_id=record_id,
        verdict=verdict,  # type: ignore[arg-type]
        comment=comment,
        submitted_by=submitted_by,
        submitted_at=submitted_at,
    )


def test_answer_feedback는_frozen_값_객체다():
    fb = _feedback(verdict="bad", comment="틀렸어요")
    assert fb.record_id == "rec-1"
    assert fb.verdict == "bad"
    assert fb.comment == "틀렸어요"
    assert fb.submitted_by == "uid-1"
    assert fb.submitted_at == _T0


def test_answer_feedback는_수정_불가():
    import pydantic

    fb = _feedback()
    with pytest.raises((pydantic.ValidationError, AttributeError, TypeError)):
        fb.verdict = "bad"  # type: ignore[misc]


def test_answer_feedback_comment_기본값은_빈문자열():
    fb = AnswerFeedback(
        record_id="rec-1",
        verdict="good",
        submitted_by="uid-1",
        submitted_at=_T0,
    )
    assert fb.comment == ""


def test_answer_feedback_verdict는_good_bad만_허용():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        AnswerFeedback(
            record_id="rec-1",
            verdict="great",  # type: ignore[arg-type]
            submitted_by="uid-1",
            submitted_at=_T0,
        )


def test_feedback_store_upsert_and_latest_for_record():
    store = InMemoryFeedbackStore()
    fb = _feedback(verdict="good")
    store.upsert(fb)
    assert store.latest_for_record("rec-1") == fb


def test_feedback_store_미존재_레코드는_latest_None():
    store = InMemoryFeedbackStore()
    assert store.latest_for_record("nope") is None


def test_feedback_store_같은_질문자_재제출은_최신으로_덮인다():
    """멱등 정책 — (record_id, submitted_by) 키 upsert, 최신 verdict/comment로 갱신."""
    store = InMemoryFeedbackStore()
    store.upsert(_feedback(verdict="good", submitted_at=_T0))
    store.upsert(_feedback(verdict="bad", comment="마음이 바뀜", submitted_at=_T0 + timedelta(minutes=5)))

    latest = store.latest_for_record("rec-1")
    assert latest is not None
    assert latest.verdict == "bad"
    assert latest.comment == "마음이 바뀜"


def test_feedback_store_이력은_전량_보존된다():
    """전이 ≠ 기록 — upsert는 최신 판정을 갱신하지만, for_record는 이력 전체를 돌려준다."""
    store = InMemoryFeedbackStore()
    first = _feedback(verdict="good", submitted_at=_T0)
    second = _feedback(verdict="bad", comment="마음이 바뀜", submitted_at=_T0 + timedelta(minutes=5))
    store.upsert(first)
    store.upsert(second)

    history = store.for_record("rec-1")
    assert history == [first, second]


def test_feedback_store_다른_질문자는_각각_별_행으로_쌓인다():
    store = InMemoryFeedbackStore()
    store.upsert(_feedback(record_id="rec-1", submitted_by="uid-1", verdict="good"))
    store.upsert(_feedback(record_id="rec-1", submitted_by="uid-2", verdict="bad"))

    history = store.for_record("rec-1")
    assert {fb.submitted_by for fb in history} == {"uid-1", "uid-2"}


def test_feedback_store_for_record_미존재는_빈리스트():
    store = InMemoryFeedbackStore()
    assert store.for_record("nope") == []


# ── monitoring_for_owner 조인 확장 — 두 축 OR(레코드 표식 OR bad 피드백) (§10.3) ──


def test_feedback_store_미배선이면_기존_판정_100프로_보존():
    """하위호환 핵심 — feedback_store=None이면 feedback=None·기존 판정 그대로."""
    store = InMemoryAnswerRecordStore()
    store.add(_record(record_id="rec-1", needs_correction_review=False))

    view = monitoring_for_owner(store, correction_store=InMemoryCorrectionStore(), agent_id="refund-bot")

    assert view[0].feedback is None
    assert view[0].needs_correction_review is False


def test_레코드_표식_없고_피드백_없으면_검토_불필요():
    answer_store = InMemoryAnswerRecordStore()
    answer_store.add(_record(record_id="rec-1", needs_correction_review=False))
    feedback_store = InMemoryFeedbackStore()

    view = monitoring_for_owner(
        answer_store,
        correction_store=InMemoryCorrectionStore(),
        agent_id="refund-bot",
        feedback_store=feedback_store,
    )

    assert view[0].needs_correction_review is False
    assert view[0].feedback is None


def test_레코드_표식_없고_bad_피드백_있으면_검토_필요():
    """싫음 피드백이 담당자 검토 필요 축에 단독으로 합류한다(§10.3 OR 조인)."""
    answer_store = InMemoryAnswerRecordStore()
    answer_store.add(_record(record_id="rec-1", needs_correction_review=False))
    feedback_store = InMemoryFeedbackStore()
    feedback_store.upsert(_feedback(record_id="rec-1", verdict="bad"))

    view = monitoring_for_owner(
        answer_store,
        correction_store=InMemoryCorrectionStore(),
        agent_id="refund-bot",
        feedback_store=feedback_store,
    )

    assert view[0].needs_correction_review is True
    assert view[0].feedback is not None
    assert view[0].feedback.verdict == "bad"


def test_레코드_표식_있고_피드백_없으면_여전히_검토_필요():
    answer_store = InMemoryAnswerRecordStore()
    answer_store.add(_record(record_id="rec-1", needs_correction_review=True))
    feedback_store = InMemoryFeedbackStore()

    view = monitoring_for_owner(
        answer_store,
        correction_store=InMemoryCorrectionStore(),
        agent_id="refund-bot",
        feedback_store=feedback_store,
    )

    assert view[0].needs_correction_review is True


def test_레코드_표식_있고_good_피드백이면_검토_필요_유지():
    """good 피드백이 레코드 표식을 지우지 않는다(OR 조인·good은 검토 불필요 축에 기여 안 함)."""
    answer_store = InMemoryAnswerRecordStore()
    answer_store.add(_record(record_id="rec-1", needs_correction_review=True))
    feedback_store = InMemoryFeedbackStore()
    feedback_store.upsert(_feedback(record_id="rec-1", verdict="good"))

    view = monitoring_for_owner(
        answer_store,
        correction_store=InMemoryCorrectionStore(),
        agent_id="refund-bot",
        feedback_store=feedback_store,
    )

    assert view[0].needs_correction_review is True
    assert view[0].feedback is not None
    assert view[0].feedback.verdict == "good"


def test_good_피드백만_있으면_검토_불필요():
    answer_store = InMemoryAnswerRecordStore()
    answer_store.add(_record(record_id="rec-1", needs_correction_review=False))
    feedback_store = InMemoryFeedbackStore()
    feedback_store.upsert(_feedback(record_id="rec-1", verdict="good"))

    view = monitoring_for_owner(
        answer_store,
        correction_store=InMemoryCorrectionStore(),
        agent_id="refund-bot",
        feedback_store=feedback_store,
    )

    assert view[0].needs_correction_review is False


def test_피드백은_최신값이_조인된다():
    """마음 바꿈(좋음→싫음)이 조인 판정에 반영된다 — latest_for_record 재사용."""
    answer_store = InMemoryAnswerRecordStore()
    answer_store.add(_record(record_id="rec-1", needs_correction_review=False))
    feedback_store = InMemoryFeedbackStore()
    feedback_store.upsert(_feedback(record_id="rec-1", verdict="good", submitted_at=_T0))
    feedback_store.upsert(
        _feedback(record_id="rec-1", verdict="bad", submitted_at=_T0 + timedelta(minutes=5))
    )

    view = monitoring_for_owner(
        answer_store,
        correction_store=InMemoryCorrectionStore(),
        agent_id="refund-bot",
        feedback_store=feedback_store,
    )

    assert view[0].needs_correction_review is True
    assert view[0].feedback is not None
    assert view[0].feedback.verdict == "bad"
