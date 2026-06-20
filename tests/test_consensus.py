"""ConsensusService 단위 테스트 — FakeClassifier 패턴으로 결정론적으로."""
import pytest
from datetime import datetime, timezone

from agent_org_network.conflict import (
    Agreed,
    Candidate,
    ConcurOnPrimary,
    ConflictCase,
    ConsensusService,
    Deadlocked,
    InMemoryConflictCaseStore,
    InMemoryPrecedentStore,
    StillOpen,
)


def fixed_clock() -> datetime:
    return datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _case(
    case_id: str = "case-001",
    intent: str = "환불",
    owners: list[str] | None = None,
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


def _service(case: ConflictCase | None = None) -> tuple[ConsensusService, InMemoryConflictCaseStore, InMemoryPrecedentStore]:
    store = InMemoryConflictCaseStore()
    precedents = InMemoryPrecedentStore(clock=fixed_clock)
    if case is not None:
        store.open_case(case)
    svc = ConsensusService(case_store=store, precedents=precedents)
    return svc, store, precedents


# ── 에러 경로 ──────────────────────────────────────────────────────────


def test_미존재_case_id는_ValueError():
    svc, _, _ = _service()
    vote = ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A")
    with pytest.raises(ValueError, match="case"):
        svc.concur("없는케이스", vote)


def test_후보_아닌_Owner_표는_ValueError():
    case = _case(case_id="case-001")
    svc, _, _ = _service(case)
    vote = ConcurOnPrimary(by_owner="owner_X", on_agent="agent_owner_A")
    with pytest.raises(ValueError, match="owner"):
        svc.concur("case-001", vote)


# ── 정상 경로 ──────────────────────────────────────────────────────────


def test_한_표만_던지면_StillOpen이고_pending_정확():
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    svc, _, _ = _service(case)
    vote = ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A")
    outcome = svc.concur("case-001", vote)
    assert isinstance(outcome, StillOpen)
    assert "owner_B" in outcome.pending_owners
    assert "owner_A" not in outcome.pending_owners


def test_StillOpen_케이스_인스턴스가_원본_케이스():
    case = _case(case_id="case-001")
    svc, _, _ = _service(case)
    vote = ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A")
    outcome = svc.concur("case-001", vote)
    assert isinstance(outcome, StillOpen)
    assert outcome.case.case_id == "case-001"


def test_전원_같은_on_agent_지목시_Agreed():
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    svc, _, _ = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    outcome = svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_A"))
    assert isinstance(outcome, Agreed)
    assert outcome.resolution.primary == "agent_owner_A"
    assert outcome.resolution.intent == "환불"


def test_Agreed시_Precedent가_기록된다():
    case = _case(case_id="case-001", intent="환불", owners=["owner_A", "owner_B"])
    svc, _, precedents = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    outcome = svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_A"))
    assert isinstance(outcome, Agreed)
    p = precedents.lookup("환불")
    assert p is not None
    assert p.resolution.primary == "agent_owner_A"


def test_Agreed시_케이스가_store에서_빠진다():
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    svc, store, _ = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_A"))
    assert store.get("case-001") is None


def test_Agreed시_케이스가_history에는_남는다():
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    svc, store, _ = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_A"))
    assert any(c.case_id == "case-001" and c.status == "resolved" for c in store.history)


def test_표가_갈리면_Deadlocked():
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    svc, _, _ = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    outcome = svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_B"))
    assert isinstance(outcome, Deadlocked)
    assert outcome.case.case_id == "case-001"


def test_Deadlocked시_케이스는_store에_남는다():
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    svc, store, _ = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_B"))
    assert store.get("case-001") is not None


def test_같은_Owner가_다시_표_던지면_최신으로_덮어쓴다():
    """owner_A가 처음엔 agent_owner_B 지목, 다음엔 agent_owner_A로 바꾸면 최신이 반영."""
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    svc, _, _ = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_B"))
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    outcome = svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_A"))
    assert isinstance(outcome, Agreed)
    assert outcome.resolution.primary == "agent_owner_A"


def test_3인_케이스_두_표_이후_StillOpen():
    case = _case(case_id="case-001", owners=["owner_A", "owner_B", "owner_C"])
    svc, _, _ = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    outcome = svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_A"))
    assert isinstance(outcome, StillOpen)
    assert "owner_C" in outcome.pending_owners


def test_Agreed의_precedent_필드가_반환된_Precedent와_일치():
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    svc, _, precedents = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    outcome = svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_A"))
    assert isinstance(outcome, Agreed)
    assert outcome.precedent is precedents.lookup("환불")


def test_Deadlocked_후_재표로_Agreed_회복되고_Precedent는_1회만():
    case = _case(case_id="case-001", owners=["owner_A", "owner_B"])
    svc, store, precedents = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_owner_A"))
    deadlock = svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_B"))
    assert isinstance(deadlock, Deadlocked)

    # owner_B가 마음 바꿔 재표 → 전원 agent_owner_A 일치 → Agreed 회복
    recovered = svc.concur("case-001", ConcurOnPrimary(by_owner="owner_B", on_agent="agent_owner_A"))
    assert isinstance(recovered, Agreed)
    assert recovered.resolution.primary == "agent_owner_A"
    assert store.get("case-001") is None
    assert len(precedents.history) == 1  # 회복 시 record 1회만(이중 기록 없음)


def test_동일_owner_후보_2장이면_한_표로_Agreed():
    case = ConflictCase(
        intent="환불",
        question="환불 되나요?",
        candidates=(
            Candidate(agent_id="agent_X", owner="owner_A"),
            Candidate(agent_id="agent_Y", owner="owner_A"),
        ),
        opened_at=fixed_clock(),
        case_id="case-dup",
    )
    svc, store, _ = _service(case)
    outcome = svc.concur("case-dup", ConcurOnPrimary(by_owner="owner_A", on_agent="agent_X"))
    assert isinstance(outcome, Agreed)
    assert outcome.resolution.primary == "agent_X"
    assert store.get("case-dup") is None
