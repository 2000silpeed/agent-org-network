from datetime import date, datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.classifier import FakeClassifier
from agent_org_network.conflict import InMemoryPrecedentStore, Resolution
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.registry import Registry
from agent_org_network.router import Router


def fixed_clock() -> datetime:
    return datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def card(agent_id: str, domains: list[str]) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="D",
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
    )


def router_with(
    cards: list[AgentCard],
    intent: str,
    precedents: InMemoryPrecedentStore | None = None,
) -> Router:
    registry = Registry()
    for c in cards:
        registry.register(c)
    return Router(registry, FakeClassifier(intent), root_user="root", precedents=precedents)


def test_단일_매칭이면_Routed로_primary가_정해진다():
    router = router_with([card("contract_ops", ["계약 검토"]), card("sales_ops", ["영업"])], "계약 검토")
    decision = router.route("이 계약 조건 바꿔도 돼?")
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "contract_ops"


def test_매칭_0건이면_Unowned로_루트User에_올린다():
    router = router_with([card("contract_ops", ["계약 검토"])], "주차장")
    decision = router.route("주차장 정기권 어떻게 갱신해요?")
    assert isinstance(decision, Unowned)
    assert decision.escalated_to == "root"


def test_매칭_2건이상이면_Contested가_된다():
    router = router_with([card("cs_ops", ["환불"]), card("sales_ops", ["환불"])], "환불")
    decision = router.route("환불 되나요?")
    assert isinstance(decision, Contested)
    assert len(decision.candidates) == 2


def test_판례_있으면_자동으로_Routed가_된다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    store.record(Resolution(intent="계약 검토", primary="contract_ops"))
    router = router_with([card("contract_ops", ["계약 검토"])], "계약 검토", precedents=store)
    decision = router.route("이 계약 조건 바꿔도 돼?")
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "contract_ops"
    assert "판례" in decision.reason


def test_Contested였을_intent가_판례로_Routed가_된다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    store.record(Resolution(intent="환불", primary="cs_ops", rationale="CS팀이 주담당"))
    router = router_with(
        [card("cs_ops", ["환불"]), card("sales_ops", ["환불"])],
        "환불",
        precedents=store,
    )
    decision = router.route("환불 되나요?")
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "cs_ops"


def test_판례_없으면_기존_라우팅_동작_유지():
    router = router_with(
        [card("contract_ops", ["계약 검토"]), card("sales_ops", ["영업"])],
        "계약 검토",
        precedents=None,
    )
    decision = router.route("이 계약 조건 바꿔도 돼?")
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "contract_ops"


# M2: 빈 intent 판례가 미아 폴백을 우회하지 못해야 한다
def test_빈_intent_판례가_등록돼도_미분류_질문은_Unowned가_된다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    store.record(Resolution(intent="", primary="contract_ops"))
    router = router_with([], "", precedents=store)
    decision = router.route("???")
    assert isinstance(decision, Unowned)


# M3: 판례 primary가 미등록 카드면 crash 대신 폴백
def test_판례가_미등록_카드를_가리키면_Unowned로_폴백된다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    store.record(Resolution(intent="계약 검토", primary="ghost_ops"))
    router = router_with([], "계약 검토", precedents=store)
    decision = router.route("계약 확인해줘")
    assert isinstance(decision, Unowned)


def test_판례_primary_미등록이어도_일반_후보가_있으면_Routed로_폴백된다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    store.record(Resolution(intent="계약 검토", primary="ghost_ops"))
    router = router_with([card("contract_ops", ["계약 검토"])], "계약 검토", precedents=store)
    decision = router.route("계약 확인해줘")
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "contract_ops"


# M4: store 주입됐지만 해당 intent 판례 없으면 기존 라우팅 동작
def test_store_주입됐지만_해당_intent_판례_없으면_Contested가_된다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    store.record(Resolution(intent="환불", primary="cs_ops"))
    router = router_with(
        [card("cs_ops", ["계약 검토"]), card("sales_ops", ["계약 검토"])],
        "계약 검토",
        precedents=store,
    )
    decision = router.route("계약서 검토 부탁드려요")
    assert isinstance(decision, Contested)
    assert len(decision.candidates) == 2


def test_store_주입됐지만_해당_intent_판례_없으면_단일_후보는_Routed가_된다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    store.record(Resolution(intent="환불", primary="cs_ops"))
    router = router_with([card("contract_ops", ["계약 검토"])], "계약 검토", precedents=store)
    decision = router.route("계약 검토 부탁해요")
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "contract_ops"


def test_store_주입됐지만_해당_intent_판례_없으면_0건은_Unowned가_된다():
    store = InMemoryPrecedentStore(clock=fixed_clock)
    store.record(Resolution(intent="환불", primary="cs_ops"))
    router = router_with([], "계약 검토", precedents=store)
    decision = router.route("계약 검토 부탁해요")
    assert isinstance(decision, Unowned)


def test_intent가_domains에도_cannot_answer에도_든_카드는_후보에서_제외된다():
    """cannot_answer 차감: domains에 있어도 cannot_answer에도 있으면 후보 제외 → Unowned."""
    domain_card = AgentCard(
        agent_id="hr_ops",
        owner="D",
        team="hr",
        summary="HR",
        domains=["급여이체"],
        cannot_answer=["급여이체"],
        last_reviewed_at=date(2026, 6, 20),
    )
    registry = Registry()
    registry.register(domain_card)
    router = Router(registry, FakeClassifier("급여이체"), root_user="root")
    decision = router.route("급여이체 처리해줘")
    assert isinstance(decision, Unowned)
