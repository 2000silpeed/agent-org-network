"""데모 조립 팩토리 — 하드코딩 샘플로 AskOrg를 한 개 만든다.

T1.3 YAML 로더·T6.4 샘플 골든셋이 아직이라 인라인으로 카드/유저를 박는다.
눈으로 보는 end-to-end 한 바퀴(웹챗)를 돌리기 위한 walking-skeleton 조립이다.
"""

from datetime import date
from pathlib import Path

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg
from agent_org_network.audit import JsonlAuditLog
from agent_org_network.classifier import RuleBasedClassifier
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User

ROOT_USER = "root_manager"

_REVIEWED = date(2026, 6, 20)

_USERS: tuple[User, ...] = (
    User(id=ROOT_USER),
    User(id="legal_lead", manager=ROOT_USER),
    User(id="cs_lead", manager=ROOT_USER),
    User(id="finance_lead", manager=ROOT_USER),
)

_CARDS: tuple[AgentCard, ...] = (
    AgentCard(
        agent_id="contract_ops",
        owner="legal_lead",
        team="legal",
        summary="계약 검토와 조건 변경 가능 여부를 안내합니다.",
        domains=["계약 검토"],
        last_reviewed_at=_REVIEWED,
        knowledge_sources=["위키/계약가이드", "Notion/표준계약서"],
    ),
    AgentCard(
        agent_id="cs_ops",
        owner="cs_lead",
        team="cs",
        summary="환불 정책과 처리 절차를 안내합니다.",
        domains=["환불"],
        last_reviewed_at=_REVIEWED,
        knowledge_sources=["위키/환불정책"],
    ),
    AgentCard(
        agent_id="finance_ops",
        owner="finance_lead",
        team="finance",
        summary="가격 정책과 견적 기준을 안내합니다.",
        domains=["가격"],
        last_reviewed_at=_REVIEWED,
        knowledge_sources=["Notion/가격표", "위키/할인규정"],
    ),
)

_KEYWORD_INTENTS: dict[str, str] = {
    "계약": "계약 검토",
    "환불": "환불",
    "가격": "가격",
}


def build_demo_ask_org() -> AskOrg:
    """하드코딩 샘플로 조립한 AskOrg를 돌려준다.

    카드 3종(contract_ops·cs_ops·finance_ops) + 루트 매니저 포함 유저 4명.
    분류기는 키워드 규칙(계약/환불/가격), 런타임은 결정론 StubRuntime.
    """
    registry = Registry()
    for user in _USERS:
        registry.register_user(user)
    for card in _CARDS:
        registry.register(card)
    registry.validate()

    classifier = RuleBasedClassifier(_KEYWORD_INTENTS)
    router = Router(registry, classifier, root_user=ROOT_USER)
    return AskOrg(
        router=router,
        runtime=StubRuntime(),
        audit_log=JsonlAuditLog(Path("logs/audit.jsonl")),
        classifier=classifier,
    )
