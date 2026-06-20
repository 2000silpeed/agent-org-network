from datetime import date

from agent_org_network.agent_card import AgentCard
from agent_org_network.classifier import FakeClassifier
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.registry import Registry
from agent_org_network.router import Router


def card(agent_id: str, domains: list[str]) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="D",
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
    )


def router_with(cards: list[AgentCard], intent: str) -> Router:
    registry = Registry()
    for c in cards:
        registry.register(c)
    return Router(registry, FakeClassifier(intent), root_user="root")


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
