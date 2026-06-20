from datetime import date

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg, Answered, Pending
from agent_org_network.classifier import FakeClassifier
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User


def card(agent_id: str, domains: list[str], knowledge_sources: list[str] | None = None) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="D",
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=knowledge_sources or [],
    )


def ask_org_with(cards: list[AgentCard], intent: str) -> AskOrg:
    registry = Registry()
    for c in cards:
        registry.register(c)
    router = Router(registry, FakeClassifier(intent), root_user="root")
    return AskOrg(router=router, runtime=StubRuntime())


def test_Routed면_Answered로_담당과_출처가_붙는다():
    sources = ["위키/계약가이드", "Notion/FAQ"]
    c = card("contract_ops", ["계약 검토"], knowledge_sources=sources)
    ask = ask_org_with([c], "계약 검토")
    user = User(id="u1")

    reply = ask.handle("이 계약 조건 바꿔도 돼?", user)

    assert isinstance(reply, Answered)
    assert reply.answered_by[1] == "contract_ops"
    assert reply.sources == tuple(sources)
    assert reply.mode == "full"


def test_Unowned면_Pending_unowned로_안내만():
    c = card("contract_ops", ["계약 검토"])
    ask = ask_org_with([c], "주차장")
    user = User(id="u1")

    reply = ask.handle("주차장 정기권 어떻게 갱신해요?", user)

    assert isinstance(reply, Pending)
    assert reply.kind == "unowned"


def test_Contested면_Pending_contested():
    cards = [card("cs_ops", ["환불"]), card("sales_ops", ["환불"])]
    ask = ask_org_with(cards, "환불")
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Pending)
    assert reply.kind == "contested"
