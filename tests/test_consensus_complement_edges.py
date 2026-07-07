"""ADR 0038 슬라이스 A — `ConsensusService` 합의-소싱 `ComplementEdge` 방출 [순수·결정론].

핵심 급소(결정 3): 진 후보 owner의 concede stance가 신호다 — 기본 `withdraw`(엣지
없음·안전 기본)이고, 명시 `keep_as_complement`일 때만 `Agreed` 분기에서
`primary→supporting` 엣지가 방출된다. `edge_store=None`(기본)이면 방출 0(회귀 0).
"""

from datetime import datetime, timezone

from agent_org_network.complement import InMemoryEdgeStore
from agent_org_network.conflict import (
    Agreed,
    Candidate,
    ConcurOnPrimary,
    ConflictCase,
    ConsensusService,
    InMemoryConflictCaseStore,
    InMemoryPrecedentStore,
)


def fixed_clock() -> datetime:
    return datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def _case(
    case_id: str = "case-001",
    intent: str = "보상",
    owners: list[str] | None = None,
) -> ConflictCase:
    if owners is None:
        owners = ["owner_CS", "owner_Finance"]
    candidates = tuple(
        Candidate(agent_id=f"agent_{o.split('_')[-1].lower()}", owner=o) for o in owners
    )
    return ConflictCase(
        intent=intent,
        question="보상 되나요?",
        candidates=candidates,
        opened_at=fixed_clock(),
        case_id=case_id,
    )


def _service(
    case: ConflictCase | None = None, edge_store: InMemoryEdgeStore | None = None
) -> tuple[ConsensusService, InMemoryConflictCaseStore, InMemoryPrecedentStore]:
    store = InMemoryConflictCaseStore()
    precedents = InMemoryPrecedentStore(clock=fixed_clock)
    if case is not None:
        store.open_case(case)
    svc = ConsensusService(case_store=store, precedents=precedents, edge_store=edge_store)
    return svc, store, precedents


# ── 회귀 0 — edge_store 미주입 ────────────────────────────────────────────


def test_edge_store_미주입이면_keep_as_complement이어도_방출_0() -> None:
    case = _case()
    svc, _, _ = _service(case)  # edge_store=None(기본)
    svc.concur(
        "case-001",
        ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs"),
    )
    outcome = svc.concur(
        "case-001",
        ConcurOnPrimary(
            by_owner="owner_Finance", on_agent="agent_cs", stance="keep_as_complement"
        ),
    )
    assert isinstance(outcome, Agreed)
    # edge_store가 없으니 방출할 곳도 없다 — 예외 없이 그냥 방출 0(회귀 0).


def test_stance_기본값은_withdraw이다() -> None:
    vote = ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs")
    assert vote.stance == "withdraw"


# ── 기본(withdraw) → 엣지 0 ────────────────────────────────────────────────


def test_기본_withdraw_stance면_Agreed여도_엣지_0() -> None:
    case = _case()
    edge_store = InMemoryEdgeStore()
    svc, _, _ = _service(case, edge_store=edge_store)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs"))
    outcome = svc.concur("case-001", ConcurOnPrimary(by_owner="owner_Finance", on_agent="agent_cs"))
    assert isinstance(outcome, Agreed)
    assert edge_store.neighbors("보상", "agent_cs") == ()


# ── keep_as_complement → 엣지 방출(급소) ──────────────────────────────────


def test_keep_as_complement_stance면_진_후보_카드로_엣지가_방출된다() -> None:
    case = _case()
    edge_store = InMemoryEdgeStore()
    svc, _, _ = _service(case, edge_store=edge_store)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs"))
    outcome = svc.concur(
        "case-001",
        ConcurOnPrimary(
            by_owner="owner_Finance", on_agent="agent_cs", stance="keep_as_complement"
        ),
    )
    assert isinstance(outcome, Agreed)
    assert outcome.resolution.primary == "agent_cs"
    assert edge_store.neighbors("보상", "agent_cs") == ("agent_finance",)


def test_엣지는_primary로는_안_간다() -> None:
    """primary(agent_cs)는 자기 자신에게 엣지를 안 받는다 — neighbors("보상", "agent_finance")는 비어야."""
    case = _case()
    edge_store = InMemoryEdgeStore()
    svc, _, _ = _service(case, edge_store=edge_store)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs"))
    svc.concur(
        "case-001",
        ConcurOnPrimary(
            by_owner="owner_Finance", on_agent="agent_cs", stance="keep_as_complement"
        ),
    )
    assert edge_store.neighbors("보상", "agent_finance") == ()


def test_3인_케이스에서_keep인_owner만_엣지_방출된다() -> None:
    """owner_Finance=keep_as_complement, owner_Legal=withdraw(기본) → Finance만 엣지."""
    case = _case(owners=["owner_CS", "owner_Finance", "owner_Legal"])
    edge_store = InMemoryEdgeStore()
    svc, _, _ = _service(case, edge_store=edge_store)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs"))
    svc.concur(
        "case-001",
        ConcurOnPrimary(
            by_owner="owner_Finance", on_agent="agent_cs", stance="keep_as_complement"
        ),
    )
    outcome = svc.concur(
        "case-001", ConcurOnPrimary(by_owner="owner_Legal", on_agent="agent_cs")
    )
    assert isinstance(outcome, Agreed)
    assert edge_store.neighbors("보상", "agent_cs") == ("agent_finance",)


# ── owner 다중 카드 경계(경계 동작 고정 — code-reviewer Low 1) ─────────────


def test_owner가_같은_다툼에서_primary_카드와_진_카드를_모두_소유하면_stance로_엣지방출된다() -> None:
    """`_emit_complement_edges`는 owner당 단일 stance(그 owner가 이 케이스에 던진
    표)를 그 owner의 모든 비-primary 카드에 적용한다 — claim/concede를 코드가
    구분해 가드하지 않는다(conflict.py `ConcessionStance`·`_emit_complement_edges`
    docstring 참고). owner O가 primary 카드(agent_P)와 진 카드(agent_L)를 둘 다
    소유하고 그 단일 표에 stance=keep_as_complement를 실으면, agent_P→agent_L
    엣지가 방출된다 — "패자 카드당 1엣지"(ADR 0038 결정 1) 정합·owner가 자기 두
    카드의 상보 관계를 스스로 선언하는 무해한 경계 동작(의도적으로 고정, 변경 아님).
    """
    case = ConflictCase(
        intent="보상",
        question="보상 되나요?",
        candidates=(
            Candidate(agent_id="agent_P", owner="owner_O"),
            Candidate(agent_id="agent_L", owner="owner_O"),
        ),
        opened_at=fixed_clock(),
        case_id="case-dual-owner",
    )
    edge_store = InMemoryEdgeStore()
    svc, store, _ = _service(case, edge_store=edge_store)

    outcome = svc.concur(
        "case-dual-owner",
        ConcurOnPrimary(by_owner="owner_O", on_agent="agent_P", stance="keep_as_complement"),
    )

    assert isinstance(outcome, Agreed)
    assert outcome.resolution.primary == "agent_P"
    assert edge_store.neighbors("보상", "agent_P") == ("agent_L",)
    assert store.get("case-dual-owner") is None


# ── 멱등(재합의) ───────────────────────────────────────────────────────────


def test_같은_intent_재합의는_엣지를_멱등_방출한다() -> None:
    """Deadlocked 후 재표로 Agreed 회복돼도 같은 엣지가 중복 기록되지 않는다."""
    case = _case()
    edge_store = InMemoryEdgeStore()
    svc, store, _ = _service(case, edge_store=edge_store)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs"))
    # 처음엔 갈림(교착) — Finance가 자기 카드를 지목.
    deadlock = svc.concur(
        "case-001",
        ConcurOnPrimary(
            by_owner="owner_Finance", on_agent="agent_finance", stance="keep_as_complement"
        ),
    )
    from agent_org_network.conflict import Deadlocked

    assert isinstance(deadlock, Deadlocked)
    assert edge_store.neighbors("보상", "agent_cs") == ()

    # Finance가 마음 바꿔 cs로 합의 + keep_as_complement 선언.
    recovered = svc.concur(
        "case-001",
        ConcurOnPrimary(
            by_owner="owner_Finance", on_agent="agent_cs", stance="keep_as_complement"
        ),
    )
    assert isinstance(recovered, Agreed)
    assert edge_store.neighbors("보상", "agent_cs") == ("agent_finance",)
    assert store.get("case-001") is None


# ── 오등록 구별(핵심 red 결정론 경계 4) ────────────────────────────────────


def test_같은_후보집합_같은_primary라도_stance만_다르면_엣지_방출이_갈린다() -> None:
    """case a(keep_as_complement)=엣지 방출 vs case b(withdraw)=엣지 없음 — 오등록 구별."""
    case_a = _case(case_id="case-a", intent="보상A")
    case_b = _case(case_id="case-b", intent="보상B")
    edge_store = InMemoryEdgeStore()
    store = InMemoryConflictCaseStore()
    store.open_case(case_a)
    store.open_case(case_b)
    precedents = InMemoryPrecedentStore(clock=fixed_clock)
    svc = ConsensusService(case_store=store, precedents=precedents, edge_store=edge_store)

    # case a: Finance가 진짜 걸침을 선언(keep_as_complement) → 엣지 방출.
    svc.concur("case-a", ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs"))
    outcome_a = svc.concur(
        "case-a",
        ConcurOnPrimary(
            by_owner="owner_Finance", on_agent="agent_cs", stance="keep_as_complement"
        ),
    )
    assert isinstance(outcome_a, Agreed)

    # case b: Finance가 오등록을 인정(기본 withdraw) → 엣지 없음.
    svc.concur("case-b", ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs"))
    outcome_b = svc.concur(
        "case-b", ConcurOnPrimary(by_owner="owner_Finance", on_agent="agent_cs")
    )
    assert isinstance(outcome_b, Agreed)

    assert edge_store.neighbors("보상A", "agent_cs") == ("agent_finance",)
    assert edge_store.neighbors("보상B", "agent_cs") == ()


# ── 회귀 0 — 기존 합의 로직 무변경 확인(핵심 케이스 재확인) ───────────────


def test_stance_인자_없이도_기존처럼_Agreed_생성된다() -> None:
    """stance 파라미터를 전혀 안 주는 기존 생성처가 100% 무영향(기본값 withdraw)."""
    case = _case()
    svc, _, precedents = _service(case)
    svc.concur("case-001", ConcurOnPrimary(by_owner="owner_CS", on_agent="agent_cs"))
    outcome = svc.concur(
        "case-001", ConcurOnPrimary(by_owner="owner_Finance", on_agent="agent_cs")
    )
    assert isinstance(outcome, Agreed)
    assert outcome.resolution.primary == "agent_cs"
    assert precedents.lookup("보상") is not None
