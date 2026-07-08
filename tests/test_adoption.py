"""S3 — 채택 성공기준 계측(`compute_adoption_metrics`) red→green 테스트.

ADR 0039 결정 5·PRD §8 채택 3축(owner 자발 유지·사람 개입 없는 종결·논쟁 판례 종결)
+ 선행 사망신호(R1)를 domain-architect shape(`adoption.py`)에 맞춰 잠근다.

전부 결정론: `InMemoryAuditLog`(raw record 직접 주입) + `InMemoryPrecedentStore`
(주입 clock) + `InMemoryConflictCaseStore` + 주입 `is_owner_active` 술어. 실 LLM 0.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent_org_network.adoption import AdoptionWindow, compute_adoption_metrics
from agent_org_network.audit import InMemoryAuditLog, action_record
from agent_org_network.conflict import (
    Candidate,
    ConflictCase,
    InMemoryConflictCaseStore,
    InMemoryPrecedentStore,
    Resolution,
)

BASE = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)


def _t(days: int = 0, seconds: int = 0) -> datetime:
    return BASE + timedelta(days=days, seconds=seconds)


def _window(since: datetime = BASE, until: datetime | None = None) -> AdoptionWindow:
    return AdoptionWindow(since=since, until=until if until is not None else _t(days=28))


def _audit_record(
    *,
    timestamp: datetime,
    intent: str = "refund",
    decision: dict[str, Any] | None,
    dispatch: dict[str, Any] | None = None,
    answer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp.isoformat(),
        "user_id": "u1",
        "question": "질문",
        "intent": intent,
        "decision": decision,
        "answer": answer,
        "dispatch": dispatch,
        "tracking": None,
    }


def _routed_decision() -> dict[str, Any]:
    return {
        "disposition": "routed",
        "primary": "cs_ops",
        "owner": "alice",
        "confidence": 1.0,
        "reason": "",
        "requires_approval": False,
        "collaborators": [],
    }


def _routed_full(timestamp: datetime, intent: str = "refund") -> dict[str, Any]:
    return _audit_record(
        timestamp=timestamp,
        intent=intent,
        decision=_routed_decision(),
        dispatch={"disposition": "delivered"},
        answer={"text": "답", "mode": "full", "sources": []},
    )


def _routed_draft_only(timestamp: datetime, intent: str = "refund") -> dict[str, Any]:
    return _audit_record(
        timestamp=timestamp,
        intent=intent,
        decision={**_routed_decision(), "requires_approval": True},
        dispatch={"disposition": "delivered"},
        answer={"text": "초안", "mode": "draft_only", "sources": []},
    )


def _routed_awaiting_worker(timestamp: datetime, intent: str = "refund") -> dict[str, Any]:
    return _audit_record(
        timestamp=timestamp,
        intent=intent,
        decision=_routed_decision(),
        dispatch={"disposition": "awaiting_worker", "waited_seconds": 5.0},
        answer=None,
    )


def _unowned(timestamp: datetime, intent: str = "refund") -> dict[str, Any]:
    return _audit_record(
        timestamp=timestamp,
        intent=intent,
        decision={"disposition": "unowned", "escalated_to": "root", "reason": "미아 없음 보존"},
        dispatch=None,
        answer=None,
    )


def _contested(timestamp: datetime, intent: str = "refund") -> dict[str, Any]:
    return _audit_record(
        timestamp=timestamp,
        intent=intent,
        decision={"disposition": "contested", "candidates": ["cs_a", "cs_b"], "reason": "동률"},
        dispatch=None,
        answer=None,
    )


def _weeks_predicate(active_weeks: dict[str, int]) -> Callable[[str, datetime, datetime], bool]:
    """owner별 "앞에서부터 N번째 주까지 활동"을 결정론으로 흉내내는 fake 술어.

    실제 주 경계(week_since/week_until)는 보지 않고 호출 순번만 센다 — 슬라이싱
    로직은 `compute_adoption_metrics`가 정하고, 이 fake는 "owner당 활동 주 수"만
    통제한다(테스트 목적에 필요한 만큼만).
    """
    calls: dict[str, int] = {}

    def predicate(owner_id: str, _week_since: datetime, _week_until: datetime) -> bool:
        calls[owner_id] = calls.get(owner_id, 0) + 1
        return calls[owner_id] <= active_weeks.get(owner_id, 0)

    return predicate


def _always_active(_owner_id: str, _week_since: datetime, _week_until: datetime) -> bool:
    return True


def _fixed_clock(times: list[datetime]) -> Callable[[], datetime]:
    it: Iterator[datetime] = iter(times)

    def clock() -> datetime:
        return next(it)

    return clock


# ── 축2 — 사람 개입 없는 종결 ─────────────────────────────────────────────


def test_축2_비율_기본_절대건수_병기() -> None:
    audit_log = InMemoryAuditLog()
    for i in range(3):
        audit_log.record_action(_routed_full(_t(days=1, seconds=i)))
    audit_log.record_action(_routed_draft_only(_t(days=1, seconds=10)))
    audit_log.record_action(_unowned(_t(days=1, seconds=20)))

    metrics = compute_adoption_metrics(
        owner_ids=["alice"],
        audit_reader=audit_log,
        precedent_store=InMemoryPrecedentStore(),
        conflict_store=InMemoryConflictCaseStore(),
        is_owner_active=_always_active,
        window=_window(),
        now=_t(days=28),
        threshold_days=7,
    )

    uc = metrics.unattended_closure
    assert uc.total_questions == 5
    assert uc.unattended_closed == 3
    assert uc.unattended_rate == 0.6
    assert uc.hitl_reviewed == 1
    assert uc.escalated == 1


def test_축2_goodhart_정정된_답도_unattended_closed_카운트() -> None:
    """정정 스토어를 아예 조인하지 않으므로(모듈 docstring) 정정 발생이 이 지표에
    영향을 줄 수 없다 — full 발신 + 종결 시점 개입 신호 0이면 정정 여부와 무관하게
    unattended_closed에 남는다(ADR 0035 결정 1 정신).

    이름이 오해되지 않게 명시: 이 테스트는 실제 `CorrectionEvent`를 만들어 주입하지
    않는다(`compute_adoption_metrics`가 `CorrectionStore`를 인자로도 받지 않는다).
    이 테스트가 잠그는 것은 "정정이 나도 카운트가 안 깎인다"는 동작이 아니라,
    **정정 스토어가 애초에 조인되지 않는 구조 자체**다 — 축2는 종결 시점 신호
    (`decision`/`dispatch`/`answer.mode`)만 읽고 정정을 전혀 읽지 않는다."""
    audit_log = InMemoryAuditLog()
    audit_log.record_action(_routed_full(_t(days=1)))

    metrics = compute_adoption_metrics(
        owner_ids=["alice"],
        audit_reader=audit_log,
        precedent_store=InMemoryPrecedentStore(),
        conflict_store=InMemoryConflictCaseStore(),
        is_owner_active=_always_active,
        window=_window(),
        now=_t(days=28),
        threshold_days=7,
    )

    assert metrics.unattended_closure.unattended_closed == 1
    assert metrics.unattended_closure.unattended_rate == 1.0


def test_축2_action_레코드와_awaiting은_분모_제외_in_flight로_별도_계상() -> None:
    """action 레코드는 애초에 질문이 아니라 분모 제외. awaiting(in-flight)도 아직
    종결되지 않았으므로 분모 제외하고 `in_flight`로 별도 계상한다(PRD §8 확정 —
    "종결된 질문 중" ≥70%. 리뷰 m2)."""
    audit_log = InMemoryAuditLog()
    audit_log.record_action(
        action_record(timestamp=_t(days=1), action="backup_review", subject_id="cs_ops", by="alice")
    )
    audit_log.record_action(_routed_awaiting_worker(_t(days=1, seconds=5)))

    metrics = compute_adoption_metrics(
        owner_ids=["alice"],
        audit_reader=audit_log,
        precedent_store=InMemoryPrecedentStore(),
        conflict_store=InMemoryConflictCaseStore(),
        is_owner_active=_always_active,
        window=_window(),
        now=_t(days=28),
        threshold_days=7,
    )

    uc = metrics.unattended_closure
    assert uc.total_questions == 0  # awaiting뿐 — 종결된 질문 0건이라 분모 0
    assert uc.awaiting == 1
    assert uc.in_flight == 1
    assert uc.unattended_closed == 0
    assert uc.unattended_rate == 0.0  # 분모 0 폴백


# ── 축3 — 논쟁 판례 종결 + 재논쟁 0 ──────────────────────────────────────


def test_축3_판례_3건_합의_manager_혼합_종결() -> None:
    precedent_store = InMemoryPrecedentStore(
        clock=_fixed_clock([_t(days=1), _t(days=2), _t(days=3)])
    )
    precedent_store.record(Resolution(intent="refund", primary="cs_ops"))
    precedent_store.record(Resolution(intent="billing", primary="cs_ops"))
    precedent_store.record(Resolution(intent="hr", primary="hr_bot"))

    metrics = compute_adoption_metrics(
        owner_ids=["alice"],
        audit_reader=InMemoryAuditLog(),
        precedent_store=precedent_store,
        conflict_store=InMemoryConflictCaseStore(),
        is_owner_active=_always_active,
        window=_window(),
        now=_t(days=28),
        threshold_days=7,
    )

    cr = metrics.contested_resolution
    assert cr.resolved_precedents == 3
    assert set(cr.resolved_intents) == {"refund", "billing", "hr"}
    assert cr.re_contest_free is True


def test_축3_판례_이후_재논쟁_감지되면_re_contest_free_false() -> None:
    precedent_store = InMemoryPrecedentStore(clock=_fixed_clock([_t(days=1)]))
    precedent_store.record(Resolution(intent="refund", primary="cs_ops"))

    audit_log = InMemoryAuditLog()
    audit_log.record_action(_contested(_t(days=5), intent="refund"))

    metrics = compute_adoption_metrics(
        owner_ids=["alice"],
        audit_reader=audit_log,
        precedent_store=precedent_store,
        conflict_store=InMemoryConflictCaseStore(),
        is_owner_active=_always_active,
        window=_window(),
        now=_t(days=28),
        threshold_days=7,
    )

    cr = metrics.contested_resolution
    assert cr.re_contested_intents == ("refund",)
    assert cr.re_contest_free is False


# ── 축1 — owner 자발 유지 ────────────────────────────────────────────────


def test_축1_최소_활동_주_충족_owner만_유지되고_removal_요청은_활동_무관_탈락() -> None:
    predicate = _weeks_predicate({"alice": 3, "bob": 2, "carol": 1, "dave": 4})

    metrics = compute_adoption_metrics(
        owner_ids=["alice", "bob", "carol", "dave"],
        audit_reader=InMemoryAuditLog(),
        precedent_store=InMemoryPrecedentStore(),
        conflict_store=InMemoryConflictCaseStore(),
        is_owner_active=predicate,
        removal_requested_owner_ids=frozenset({"dave"}),
        window=_window(),
        now=_t(days=28),
        threshold_days=7,
    )

    retention = metrics.retention
    assert retention.retained_owners == 2
    assert retention.retained_owner_ids == ("alice", "bob")
    assert "carol" not in retention.retained_owner_ids
    assert "dave" not in retention.retained_owner_ids


# ── 선행지표(R1) — N일 이상 안 닫힌 Contested ────────────────────────────


def test_축4_R1_임계_초과분만_aging_max_open_days는_최댓값() -> None:
    now = _t(days=28)
    conflict_store = InMemoryConflictCaseStore()
    conflict_store.open_case(
        ConflictCase(
            intent="refund",
            question="q1",
            candidates=(
                Candidate(agent_id="cs_a", owner="alice"),
                Candidate(agent_id="cs_b", owner="bob"),
            ),
            opened_at=now - timedelta(days=10),
        )
    )
    conflict_store.open_case(
        ConflictCase(
            intent="billing",
            question="q2",
            candidates=(
                Candidate(agent_id="cs_c", owner="carol"),
                Candidate(agent_id="cs_d", owner="dave"),
            ),
            opened_at=now - timedelta(days=2),
        )
    )

    metrics = compute_adoption_metrics(
        owner_ids=["alice", "bob", "carol", "dave"],
        audit_reader=InMemoryAuditLog(),
        precedent_store=InMemoryPrecedentStore(),
        conflict_store=conflict_store,
        is_owner_active=_always_active,
        window=_window(),
        now=now,
        threshold_days=7,
    )

    ds = metrics.death_signal
    assert ds.open_contested_total == 2
    assert len(ds.aging_contested) == 1
    assert ds.aging_contested[0].open_days == pytest.approx(10.0)
    assert ds.max_open_days == pytest.approx(10.0)


def test_축4_R1_open_0건이면_aging_공집합_max_open_days_none() -> None:
    metrics = compute_adoption_metrics(
        owner_ids=["alice"],
        audit_reader=InMemoryAuditLog(),
        precedent_store=InMemoryPrecedentStore(),
        conflict_store=InMemoryConflictCaseStore(),
        is_owner_active=_always_active,
        window=_window(),
        now=_t(days=28),
        threshold_days=7,
    )

    ds = metrics.death_signal
    assert ds.aging_contested == ()
    assert ds.max_open_days is None
    assert ds.open_contested_total == 0


# ── window 필터 ──────────────────────────────────────────────────────────


def test_window_밖_감사_레코드와_판례는_제외된다() -> None:
    audit_log = InMemoryAuditLog()
    audit_log.record_action(_routed_full(_t(days=-5)))  # window(0~28일) 밖
    audit_log.record_action(_routed_full(_t(days=1)))

    precedent_store = InMemoryPrecedentStore(clock=_fixed_clock([_t(days=-1)]))
    precedent_store.record(Resolution(intent="refund", primary="cs_ops"))

    metrics = compute_adoption_metrics(
        owner_ids=["alice"],
        audit_reader=audit_log,
        precedent_store=precedent_store,
        conflict_store=InMemoryConflictCaseStore(),
        is_owner_active=_always_active,
        window=_window(),
        now=_t(days=28),
        threshold_days=7,
    )

    assert metrics.unattended_closure.total_questions == 1
    assert metrics.contested_resolution.resolved_precedents == 0
