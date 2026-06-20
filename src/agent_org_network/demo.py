"""데모 조립 팩토리 — 하드코딩 샘플로 AskOrg를 한 개 만든다.

T1.3 YAML 로더·T6.4 샘플 골든셋이 아직이라 인라인으로 카드/유저를 박는다.
눈으로 보는 end-to-end 한 바퀴(웹챗)를 돌리기 위한 walking-skeleton 조립이다.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg
from agent_org_network.audit import JsonlAuditLog
from agent_org_network.classifier import RuleBasedClassifier
from agent_org_network.conflict import (
    ConflictCaseStore,
    ConsensusService,
    InMemoryConflictCaseStore,
    InMemoryPrecedentStore,
    PrecedentStore,
)
from agent_org_network.dispatch import LocalRuntimeDispatcher
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import AgentRuntime, ClaudeCodeRuntime
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
        domains=["환불", "보상"],
        last_reviewed_at=_REVIEWED,
        knowledge_sources=["위키/환불정책"],
    ),
    AgentCard(
        agent_id="finance_ops",
        owner="finance_lead",
        team="finance",
        summary="가격 정책과 견적 기준을 안내합니다.",
        domains=["가격", "보상"],
        last_reviewed_at=_REVIEWED,
        knowledge_sources=["Notion/가격표", "위키/할인규정"],
    ),
)

_KEYWORD_INTENTS: dict[str, str] = {
    "계약": "계약 검토",
    "환불": "환불",
    "가격": "가격",
    "보상": "보상",
}


@dataclass(frozen=True)
class DemoBundle:
    """데모 한 벌을 한 상태로 묶는 컨테이너.

    채팅(`ask`)·처리함(`case_store`)·라우터 자동적용(`precedents`)·합의
    (`consensus`)가 모두 *같은* store 인스턴스를 공유해야 화면 간 합의가
    반영된다(처리함서 Agreed → 채팅서 자동 Routed). 웹이 이 한 벌을 받아
    모든 라우트가 같은 상태를 본다.
    """

    ask: AskOrg
    case_store: ConflictCaseStore
    precedents: PrecedentStore
    consensus: ConsensusService


def build_demo(runtime: AgentRuntime | None = None) -> DemoBundle:
    """하드코딩 샘플로 조립한 데모 한 벌(공유 store)을 돌려준다.

    카드 3종(contract_ops·cs_ops·finance_ops) + 루트 매니저 포함 유저 4명.
    분류기는 키워드 규칙(계약/환불/가격/보상).
    런타임은 기본 `ClaudeCodeRuntime`(웹에서 진짜 Claude 답) — 단 결정론이 필요한
    테스트는 `StubRuntime`(또는 FakeRunner 주입한 ClaudeCodeRuntime)을 넘긴다.
    cs_ops·finance_ops가 "보상" domain을 공유 → "보상" 질문은 Contested(다툼) 시연.

    `precedents`·`case_store`를 하나씩 만들어 Router·AskOrg·ConsensusService에
    같은 인스턴스로 주입한다 — 처리함 합의(Agreed→Precedent 기록)가 곧바로
    채팅 라우팅(판례 자동 적용)에 반영되도록.
    """
    registry = Registry()
    for user in _USERS:
        registry.register_user(user)
    for card in _CARDS:
        registry.register(card)
    registry.validate()

    classifier = RuleBasedClassifier(_KEYWORD_INTENTS)
    precedents = InMemoryPrecedentStore()
    case_store = InMemoryConflictCaseStore()
    router = Router(registry, classifier, root_user=ROOT_USER, precedents=precedents)
    # ask_org는 RuntimeDispatcher 경유로 답을 모은다(T6.3 슬라이스2). 데모/in-process는
    # 분산이 아니라 즉답이 필요하므로 동기 런타임을 LocalRuntimeDispatcher로 감싼다 —
    # dispatch가 곧 답(항상 Delivered). 실제 분산 워커·큐는 슬라이스2 네트워크 디스패처가
    # 이 자리를 대신한다.
    runtime_impl: AgentRuntime = runtime if runtime is not None else ClaudeCodeRuntime()
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(runtime_impl),
        audit_log=JsonlAuditLog(Path("logs/audit.jsonl")),
        classifier=classifier,
        case_store=case_store,
    )
    consensus = ConsensusService(case_store=case_store, precedents=precedents)
    return DemoBundle(
        ask=ask,
        case_store=case_store,
        precedents=precedents,
        consensus=consensus,
    )


def build_demo_ask_org(runtime: AgentRuntime | None = None) -> AskOrg:
    """하위호환 진입점 — `build_demo().ask`만 돌려준다.

    기존 호출처(채팅 단독 테스트 등)는 공유 store가 필요 없으므로 AskOrg만
    받는다. 처리함과 한 상태를 공유해야 하는 웹은 `build_demo()`를 쓴다.
    `runtime`은 `build_demo`로 그대로 전달한다(테스트는 StubRuntime 주입).
    """
    return build_demo(runtime=runtime).ask
