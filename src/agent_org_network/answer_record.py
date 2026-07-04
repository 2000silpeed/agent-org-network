"""담당자 모니터링 + 사후 교정 루프 — AnswerRecord + CorrectionEvent
(Phase 12 S5, ADR 0033 결정 4·S1 shape 절).

중앙이 낸 답의 감사 단위(`AnswerRecord`)와 그 사후 교정(`CorrectionEvent`)을 담는다.
`BackupReviewItem`(review.py)·`ReevalItem`(reeval.py) 정신의 N번째 포트+InMemory
인스턴스 — 새 메커니즘 0.

전이 ≠ 기록(핵심 불변식):
  - `AnswerRecord`는 나간 답의 append-only 기록이다. 정정은 이 레코드를 *수정하지
    않고* 새 `CorrectionEvent`를 append한다(`BackupReview`의 `CorrectBackup`이 원
    backup 답을 파괴하지 않고 새 인스턴스를 낳는 정신).
  - `CorrectionService.submit_correction`은 원 레코드를 그대로 둔 채 이벤트만
    적재하고, 그 정정된 지식을 `ReevalStore`(reeval.py, ADR 0019 재사용)에
    `AnswerSubject` 재평가 항목으로 얹는다 — 정정→판례/지식 갱신 고리.

owner 스코핑: 정정은 그 답을 낸 담당자(`AnswerRecord.answered_by`)만 할 수 있다
(`BackupReviewService`·`ReevalService`의 1인칭 강제 정신).

멱등: 같은 (record_id, by_owner, corrected_text) 재제출은 새 이벤트를 만들지
않고 기존 이벤트를 그대로 반환한다(`CorrectionEvent.event_id` 결정론 도출).

`needs_correction_review` 플래그 소비(S2/S4 배선 종단): `resolve_mode_with_presence`
(presence.py)가 오프라인 자동발신 시 낸 플래그를 `AnswerRecord`에 실어 두면,
`monitoring_for_owner`가 "검토 필요" 필터로 노출한다.

질문자 측 정정 배지(풀 방식, ADR 0033 결정 4): `view_answer_with_correction`이
원문과 최신 정정본을 함께 반환한다 — 둘 다 보존(질문자가 재접속 시 그 답변
페이지에서 정정 배지+정정본을 본다).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel

from agent_org_network.reeval import AnswerSubject, ReevalItem, ReevalStore
from agent_org_network.runtime import AnswerMode

Clock = Callable[[], datetime]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


# ── AnswerRecord — 중앙이 낸 답의 감사 단위 (ADR 0033 S1 shape 절) ────────


class AnswerRecord(BaseModel, frozen=True):
    """중앙이 낸 답의 감사 단위 — append-only(전이 ≠ 기록의 "기록" 축).

    담당자 모니터링(`monitoring_for_owner`)의 데이터 원천이자 정정 대상.
    `needs_correction_review`는 S2/S4가 배선한 오프라인 자동발신 사후교정
    플래그의 소비 지점(`resolve_mode_with_presence(..., return_flag=True)`가
    낸 값을 그대로 싣는다).
    """

    record_id: str
    question: str
    answer_text: str
    answered_by: str
    agent_id: str
    mode: AnswerMode
    session_id: str | None
    answered_at: datetime
    needs_correction_review: bool = False


class AnswerRecordStore(Protocol):
    """`AnswerRecord` 보관·조회 포트(`BackupReviewStore` 정신) — 담당자 모니터링 원천."""

    def add(self, rec: AnswerRecord) -> None: ...

    def get(self, record_id: str) -> AnswerRecord | None: ...

    def for_agent(self, agent_id: str) -> list[AnswerRecord]: ...


class InMemoryAnswerRecordStore:
    """in-memory `AnswerRecordStore` — 순수 보관(전이 아님)."""

    def __init__(self) -> None:
        self._by_id: dict[str, AnswerRecord] = {}
        self._lock = threading.Lock()

    def add(self, rec: AnswerRecord) -> None:
        with self._lock:
            self._by_id[rec.record_id] = rec

    def get(self, record_id: str) -> AnswerRecord | None:
        with self._lock:
            return self._by_id.get(record_id)

    def for_agent(self, agent_id: str) -> list[AnswerRecord]:
        with self._lock:
            return [rec for rec in self._by_id.values() if rec.agent_id == agent_id]


# ── CorrectionEvent — 원 레코드 수정 없이 append (전이 ≠ 기록) ────────────


class CorrectionEvent(BaseModel, frozen=True):
    """담당자 사후 교정을 원 `AnswerRecord` 수정 없이 쌓는 append-only 이벤트.

    `record_id`는 어느 답을 정정하나의 *참조*일 뿐(원 레코드는 절대 변경 X).
    """

    event_id: str
    record_id: str
    corrected_text: str
    by_owner: str
    rationale: str = ""
    corrected_at: datetime


class CorrectionStore(Protocol):
    """`CorrectionEvent` append-only 보관·조회 포트(`ReevalStore` 정신)."""

    def append(self, event: CorrectionEvent) -> None: ...

    def for_record(self, record_id: str) -> list[CorrectionEvent]: ...


class InMemoryCorrectionStore:
    """in-memory `CorrectionStore` — append-only, 삽입 순서 보존."""

    def __init__(self) -> None:
        self._by_record: dict[str, list[CorrectionEvent]] = {}
        self._by_id: dict[str, CorrectionEvent] = {}
        self._lock = threading.Lock()

    def append(self, event: CorrectionEvent) -> None:
        with self._lock:
            self._by_record.setdefault(event.record_id, []).append(event)
            self._by_id[event.event_id] = event

    def for_record(self, record_id: str) -> list[CorrectionEvent]:
        with self._lock:
            return list(self._by_record.get(record_id, []))

    def get(self, event_id: str) -> CorrectionEvent | None:
        with self._lock:
            return self._by_id.get(event_id)


def _correction_event_id(record_id: str, by_owner: str, corrected_text: str) -> str:
    """멱등 이벤트 식별자 — 같은 (record_id, by_owner, corrected_text)는 같은 event_id.

    같은 정정 재제출이 이벤트를 중복 적재하지 않도록 하는 멱등 키 도출(`ReevalItem.
    notification_ref` 정신 — 값에서 결정론적으로 식별자를 만든다).
    """
    digest = hashlib.sha256(
        f"{record_id}\x00{by_owner}\x00{corrected_text}".encode()
    ).hexdigest()[:16]
    return f"corr-{digest}"


# ── CorrectionService — 정정 상태기계 (전이 ≠ 기록·멱등·owner 스코핑) ─────


class CorrectionService:
    """정정 제출을 받아 `CorrectionEvent`를 append하고 reeval을 적재하는 서비스.

    상태기계 전이: 답변 발신(AnswerRecord.add, 호출자 책임) → (오프라인 자동
    발신이면 needs_correction_review=True 표식, S2/S4 배선) → 담당자 열람
    (monitoring_for_owner) → 정정 제출(submit_correction) → CorrectionEvent
    적재 → ReevalItem 적재(AnswerSubject, ADR 0019 재사용).

    owner 스코핑(1인칭 강제): `by_owner`가 `AnswerRecord.answered_by`와 달라야
    하면 ValueError(`BackupReviewService`·`ReevalService`의 1인칭 강제 정신 —
    담당자는 자기 에이전트 답만 정정 가능).

    멱등: 같은 (record_id, by_owner, corrected_text) 재제출은 새 이벤트를
    만들지 않고 기존 이벤트를 그대로 반환한다(reeval도 중복 적재 안 함).
    """

    def __init__(
        self,
        answer_store: AnswerRecordStore,
        correction_store: CorrectionStore,
        reeval_store: ReevalStore,
        clock: Clock = default_clock,
    ) -> None:
        self._answer_store = answer_store
        self._correction_store = correction_store
        self._reeval_store = reeval_store
        self._clock = clock

    def submit_correction(
        self,
        *,
        record_id: str,
        by_owner: str,
        corrected_text: str,
        rationale: str = "",
        at: datetime | None = None,
    ) -> CorrectionEvent:
        record = self._answer_store.get(record_id)
        if record is None:
            raise ValueError(f"미존재 answer record: {record_id!r}")
        if by_owner != record.answered_by:
            raise ValueError(
                f"정정자({by_owner!r})가 답변자({record.answered_by!r})와 다름 — "
                "자기 에이전트 답만 정정할 수 있다"
            )

        event_id = _correction_event_id(record_id, by_owner, corrected_text)
        existing_events = {e.event_id: e for e in self._correction_store.for_record(record_id)}
        if event_id in existing_events:
            return existing_events[event_id]

        corrected_at = at if at is not None else self._clock()
        event = CorrectionEvent(
            event_id=event_id,
            record_id=record_id,
            corrected_text=corrected_text,
            by_owner=by_owner,
            rationale=rationale,
            corrected_at=corrected_at,
        )
        self._correction_store.append(event)
        self._append_reeval(record, event)
        return event

    def _append_reeval(self, record: AnswerRecord, event: CorrectionEvent) -> None:
        """정정된 지식을 재평가 큐에 적재한다(ADR 0033 결정 4 — reeval 재사용).

        `record_id`를 안정 식별자로 삼는 `AnswerSubject` 변형이 없어(reeval.py의
        `AnswerSubject`는 `audit_index: int`), 이 슬라이스는 audit 인덱스 대신
        레코드 식별자 문자열을 해시해 정수 슬롯으로 매핑한다 — 두 축(정정 vs
        audit stale 전파)이 같은 `ReevalStore`를 공유하되 서로 다른 트리거에서
        독립적으로 적재하므로 충돌 걱정 없이 `AnswerSubject`를 재사용한다.
        """
        subject = AnswerSubject(audit_index=_record_id_to_slot(record.record_id))
        pending = self._reeval_store.pending_for_owner(record.answered_by)
        already_queued = any(
            isinstance(item.subject, AnswerSubject)
            and item.subject.audit_index == subject.audit_index
            for item in pending
        )
        if already_queued:
            return
        self._reeval_store.add(
            ReevalItem(
                subject=subject,
                owner_id=record.answered_by,
                agent_id=record.agent_id,
                trigger_sha=event.event_id,
                flagged_at=event.corrected_at,
            )
        )


def _record_id_to_slot(record_id: str) -> int:
    """`record_id`(문자열)를 `AnswerSubject.audit_index`(int) 슬롯으로 결정론 매핑."""
    digest = hashlib.sha256(record_id.encode()).hexdigest()
    return int(digest[:8], 16)


# ── 담당자 모니터링 조회 (owner 스코핑 — 자기 에이전트만) ─────────────────


@dataclass(frozen=True)
class MonitoringItem:
    """담당자 모니터링 한 건 — 답 레코드 + 검토 필요 표식 + 정정 이력 투영."""

    record: AnswerRecord
    corrections: list[CorrectionEvent]

    @property
    def needs_correction_review(self) -> bool:
        return self.record.needs_correction_review


def monitoring_for_owner(
    answer_store: AnswerRecordStore,
    correction_store: CorrectionStore,
    *,
    agent_id: str,
) -> list[MonitoringItem]:
    """담당자 모니터링 조회 — 자기 에이전트의 질문/답변 목록 + 검토 필요 표시 +
    정정 이력(ADR 0033 결정 4·계획 §3 S5).

    owner 스코핑: `agent_id`로만 스코핑한다(담당자는 자기 에이전트 것만 본다 —
    worker-소유자 스코핑 정신 재사용). 실 owner 인증/권한 대조는 호출측(웹/MCP
    어댑터) 몫 — 이 함수는 결정론 조회 코어만 담당한다.
    """
    return [
        MonitoringItem(record=rec, corrections=correction_store.for_record(rec.record_id))
        for rec in answer_store.for_agent(agent_id)
    ]


# ── 질문자 측 정정 배지 조회 (풀 방식 — 원문+정정본 보존) ─────────────────


@dataclass(frozen=True)
class AnswerCorrectionView:
    """질문자가 자기 답변 페이지에서 보는 투영 — 원문 + (있으면) 정정 배지·정정본.

    원문과 정정본이 둘 다 보존돼 반환된다(ADR 0033 결정 4 — 풀 방식 정정 표시).
    """

    record_id: str
    original_text: str
    has_correction: bool
    corrected_text: str | None
    corrected_at: datetime | None


def view_answer_with_correction(
    answer_store: AnswerRecordStore,
    correction_store: CorrectionStore,
    *,
    record_id: str,
) -> AnswerCorrectionView | None:
    """답변 조회 시 정정 이벤트가 있으면 정정 배지 + 정정본을 함께 반환한다(풀 방식).

    여러 정정이 쌓였으면 가장 최근 `corrected_at`의 정정본을 배지에 싣는다
    (`for_record`가 append 순서를 보존하므로 `max`로 최신을 고른다).
    """
    record = answer_store.get(record_id)
    if record is None:
        return None
    corrections = correction_store.for_record(record_id)
    if not corrections:
        return AnswerCorrectionView(
            record_id=record_id,
            original_text=record.answer_text,
            has_correction=False,
            corrected_text=None,
            corrected_at=None,
        )
    latest = max(corrections, key=lambda e: e.corrected_at)
    return AnswerCorrectionView(
        record_id=record_id,
        original_text=record.answer_text,
        has_correction=True,
        corrected_text=latest.corrected_text,
        corrected_at=latest.corrected_at,
    )


__all__ = [
    "AnswerRecord",
    "AnswerRecordStore",
    "InMemoryAnswerRecordStore",
    "CorrectionEvent",
    "CorrectionStore",
    "InMemoryCorrectionStore",
    "CorrectionService",
    "MonitoringItem",
    "monitoring_for_owner",
    "AnswerCorrectionView",
    "view_answer_with_correction",
]
