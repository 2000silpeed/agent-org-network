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

from datetime import datetime, timedelta, timezone

import pytest

from agent_org_network.answer_record import (
    AnswerRecord,
    CorrectionEvent,
    CorrectionService,
    InMemoryAnswerRecordStore,
    InMemoryCorrectionStore,
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


def test_존재하지_않는_레코드_조회는_None():
    answer_store = InMemoryAnswerRecordStore()
    correction_store = InMemoryCorrectionStore()
    assert view_answer_with_correction(answer_store, correction_store, record_id="nope") is None
