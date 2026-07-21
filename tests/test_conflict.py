from datetime import datetime, timezone
from dataclasses import replace

import pytest

from agent_org_network.conflict import (
    Candidate,
    ConcurOnPrimary,
    ConflictCase,
    ConsensusService,
    InMemoryConflictCaseStore,
    InMemoryPrecedentStore,
    Precedent,
    Resolution,
)


def fixed_clock() -> datetime:
    return datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _case(
    case_id: str = "case-001", intent: str = "환불", owners: list[str] | None = None
) -> ConflictCase:
    if owners is None:
        owners = ["owner_A", "owner_B"]
    candidates = tuple(Candidate(agent_id=f"agent_{o}", owner=o) for o in owners)
    return ConflictCase(
        intent=intent,
        question="환불 되나요?",
        candidates=candidates,
        opened_at=fixed_clock(),
        case_id=case_id,
    )


# ── ConflictCase 단위 ──────────────────────────────────────────────────


def test_candidate_ids가_agent_id_튜플을_반환한다():
    case = _case()
    assert case.candidate_ids() == ("agent_owner_A", "agent_owner_B")


def test_involves_owner가_후보_Owner면_True():
    case = _case()
    assert case.involves_owner("owner_A") is True
    assert case.involves_owner("owner_B") is True


def test_involves_owner가_후보_아닌_Owner면_False():
    case = _case()
    assert case.involves_owner("owner_X") is False


def test_resolve가_case_id_보존하고_status_resolved로():
    case = _case(case_id="case-abc")
    resolution = Resolution(intent="환불", primary="agent_owner_A", rationale="Owner_A 합의")
    resolved = case.resolve(resolution)
    assert resolved.case_id == "case-abc"
    assert resolved.status == "resolved"
    assert resolved.resolution == resolution


def test_resolve가_원본을_불변으로_남긴다():
    case = _case()
    resolution = Resolution(intent="환불", primary="agent_owner_A")
    _ = case.resolve(resolution)
    assert case.status == "open"
    assert case.resolution is None


def test_resolve된_케이스는_후보와_intent_보존():
    case = _case(intent="환불")
    resolution = Resolution(intent="환불", primary="agent_owner_A")
    resolved = case.resolve(resolution)
    assert resolved.intent == "환불"
    assert resolved.candidates == case.candidates


def test_request_aware_case는_round와_네상태_조합을_검증한다() -> None:
    case = ConflictCase.for_request(
        request_id="request-1",
        intent="refund",
        question="환불 기준은?",
        candidates=(Candidate(agent_id="refund-card", owner="owner-a"),),
        opened_at=fixed_clock(),
        case_id="case-request",
    )
    assert case.concurrence_round == 1

    with pytest.raises(ValueError, match="concurrence_round"):
        replace(case, concurrence_round=0)
    with pytest.raises(ValueError, match="manager_item_id"):
        ConflictCase(
            intent=case.intent,
            question=case.question,
            candidates=case.candidates,
            opened_at=case.opened_at,
            case_id=case.case_id,
            request_id=case.request_id,
            status="escalated",
            concurrence_round=1,
        )
    with pytest.raises(ValueError, match="알 수 없는"):
        replace(case, status="bogus")  # type: ignore[arg-type]


# ── InMemoryConflictCaseStore 단위 ─────────────────────────────────────


def test_open_case_후_get으로_조회된다():
    store = InMemoryConflictCaseStore()
    case = _case(case_id="case-001")
    store.open_case(case)
    assert store.get("case-001") == case
    assert store.get("case-001") is not case


def test_없는_case_id는_None():
    store = InMemoryConflictCaseStore()
    assert store.get("없는케이스") is None


def test_open_for_owner가_해당_Owner_케이스만_반환한다():
    store = InMemoryConflictCaseStore()
    case_ab = _case(case_id="case-ab", owners=["owner_A", "owner_B"])
    case_cd = _case(case_id="case-cd", owners=["owner_C", "owner_D"])
    store.open_case(case_ab)
    store.open_case(case_cd)
    result = store.open_for_owner("owner_A")
    assert len(result) == 1
    assert result[0].case_id == "case-ab"


def test_open_for_owner가_후보_아닌_Owner에_빈_목록():
    store = InMemoryConflictCaseStore()
    store.open_case(_case(case_id="case-001"))
    assert store.open_for_owner("owner_X") == []


def test_open_for_intent가_같은_intent_케이스_반환():
    store = InMemoryConflictCaseStore()
    case = _case(case_id="case-001", intent="환불")
    store.open_case(case)
    result = store.open_for_intent("환불")
    assert result == case
    assert result is not case


def test_open_for_intent가_없으면_None():
    store = InMemoryConflictCaseStore()
    assert store.open_for_intent("계약 검토") is None


def test_mark_resolved_후_get에서_빠진다():
    store = InMemoryConflictCaseStore()
    case = _case(case_id="case-001")
    store.open_case(case)
    resolution = Resolution(intent="환불", primary="agent_owner_A")
    resolved = case.resolve(resolution)
    store.mark_resolved(resolved)
    assert store.get("case-001") is None


def test_mark_resolved_후_open_for_owner에서_빠진다():
    store = InMemoryConflictCaseStore()
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    store.open_case(case)
    resolution = Resolution(intent="환불", primary="agent_owner_A")
    resolved = case.resolve(resolution)
    store.mark_resolved(resolved)
    assert store.open_for_owner("owner_A") == []


def test_mark_resolved_후_history에는_남는다():
    store = InMemoryConflictCaseStore()
    case = _case(case_id="case-001")
    store.open_case(case)
    resolution = Resolution(intent="환불", primary="agent_owner_A")
    resolved = case.resolve(resolution)
    store.mark_resolved(resolved)
    assert any(c.case_id == "case-001" for c in store.history)


def test_request_aware_case는_legacy_mutator와_intent_dedup을_차단한다() -> None:
    store = InMemoryConflictCaseStore()
    case = ConflictCase.for_request(
        request_id="request-1",
        intent="환불",
        question="환불 되나요?",
        candidates=(Candidate(agent_id="refund-card", owner="owner-a"),),
        opened_at=fixed_clock(),
        case_id="case-request",
    )
    with pytest.raises(ValueError, match="request-aware"):
        store.open_case(case)
    store.create_or_get_for_request(case)
    assert store.open_for_intent("환불") is None
    with pytest.raises(ValueError, match="request-aware"):
        store.mark_resolved(case.resolve(Resolution(intent="환불", primary="refund-card")))


def test_legacy_consensus는_request_aware_case를_side_effect_0으로_조기거부한다() -> None:
    class SpyCaseStore(InMemoryConflictCaseStore):
        def __init__(self) -> None:
            super().__init__()
            self.mark_calls = 0

        def mark_resolved(self, case: ConflictCase) -> None:
            self.mark_calls += 1
            super().mark_resolved(case)

    class SpyPrecedents(InMemoryPrecedentStore):
        def __init__(self) -> None:
            super().__init__(clock=fixed_clock)
            self.record_calls = 0

        def record(self, resolution: Resolution) -> Precedent:
            self.record_calls += 1
            return super().record(resolution)

    class SpyConsensusService(ConsensusService):
        def has_votes(self) -> bool:
            return bool(self._votes)

    store = SpyCaseStore()
    case = ConflictCase.for_request(
        request_id="request-1",
        intent="환불",
        question="환불 되나요?",
        candidates=(Candidate(agent_id="refund-card", owner="owner-a"),),
        opened_at=fixed_clock(),
        case_id="case-request",
    )
    store.create_or_get_for_request(case)
    precedents = SpyPrecedents()
    service = SpyConsensusService(store, precedents)

    with pytest.raises(ValueError, match="request-aware"):
        service.concur(
            case.case_id,
            ConcurOnPrimary(by_owner="owner-a", on_agent="refund-card"),
        )

    assert service.has_votes() is False
    assert precedents.record_calls == 0
    assert store.mark_calls == 0


def test_record_후_lookup이_같은_intent의_Precedent를_반환한다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    resolution = Resolution(intent="환불", primary="cs_ops", rationale="CS팀 담당")
    p = store.record(resolution)
    result = store.lookup("환불")
    assert result is not None
    assert result is p
    assert result.resolution.intent == "환불"
    assert result.resolution.primary == "cs_ops"


def test_미존재_intent는_None을_반환한다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    result = store.lookup("계약 검토")
    assert result is None


def test_recorded_at이_주입된_clock_값을_반영한다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    resolution = Resolution(intent="환불", primary="cs_ops")
    p = store.record(resolution)
    assert p.recorded_at == datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_같은_intent_재record시_lookup은_최신을_가리키고_history_길이_증가():
    call_count = 0

    def counting_clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return datetime(2026, 6, 20, 12, call_count, 0, tzinfo=timezone.utc)

    store = InMemoryPrecedentStore(clock=counting_clock)
    r1 = Resolution(intent="환불", primary="cs_ops", rationale="1차")
    r2 = Resolution(intent="환불", primary="sales_ops", rationale="2차 덮어쓰기")

    store.record(r1)
    store.record(r2)

    assert len(store.history) == 2
    latest = store.lookup("환불")
    assert latest is not None
    assert latest.resolution.primary == "sales_ops"
    assert latest.recorded_at == datetime(2026, 6, 20, 12, 2, 0, tzinfo=timezone.utc)
