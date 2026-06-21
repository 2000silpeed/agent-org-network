"""데모 조립 팩토리 — 하드코딩 샘플로 AskOrg를 한 개 만든다.

T1.3 YAML 로더·T6.4 샘플 골든셋이 아직이라 인라인으로 카드/유저를 박는다.
눈으로 보는 end-to-end 한 바퀴(웹챗)를 돌리기 위한 walking-skeleton 조립이다.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from typing import TYPE_CHECKING

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
from agent_org_network.dispatch import (
    DelegationSnapshot,
    LocalRuntimeDispatcher,
    RuntimeDispatcher,
)
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import AgentRuntime, ClaudeCodeRuntime
from agent_org_network.user import User

if TYPE_CHECKING:
    from agent_org_network.manager_queue import ManagerQueueStore
    from agent_org_network.review import BackupReviewStore

ROOT_USER = "root_manager"

_REVIEWED = date(2026, 6, 20)

# 데모 owner들의 OKF 번들 루트(ADR 0013, T6.7). repo 루트의 `okf/`를 owner 환경으로
# 간주한다 — 의미상 owner 소유(개인 PC·격리 저장소)지만 데모는 repo가 그 자리. 절대
# 경로로 잡아 워커를 *어디서 실행하든*(cwd 무관) 같은 번들을 cwd로 읽게 한다. 분산
# (T6.3)에선 각 owner 워커가 자기 환경의 루트를 주입한다(번들 cwd 격리).
DEMO_OKF_ROOT = Path(__file__).resolve().parent.parent.parent / "okf"

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

    `review_store`(ADR 0012 결정 7): `ask._review_store`와 같은 인스턴스를 담아
    웹 검토 라우트·create_app이 이 bundle에서 꺼내 쓸 수 있게 한다. 주입 시에만
    채워지고 미주입이면 None(하위호환 — 검토 루프 없이 동작).
    """

    ask: AskOrg
    case_store: ConflictCaseStore
    precedents: PrecedentStore
    consensus: ConsensusService
    review_store: "BackupReviewStore | None" = None


def build_demo(
    runtime: AgentRuntime | None = None,
    dispatcher: RuntimeDispatcher | None = None,
    review_store: "BackupReviewStore | None" = None,
    manager_queue_store: "ManagerQueueStore | None" = None,
) -> DemoBundle:
    """하드코딩 샘플로 조립한 데모 한 벌(공유 store)을 돌려준다.

    카드 3종(contract_ops·cs_ops·finance_ops) + 루트 매니저 포함 유저 4명.
    분류기는 키워드 규칙(계약/환불/가격/보상).
    런타임은 기본 `ClaudeCodeRuntime`(웹에서 진짜 Claude 답) — 단 결정론이 필요한
    테스트는 `StubRuntime`(또는 FakeRunner 주입한 ClaudeCodeRuntime)을 넘긴다.
    cs_ops·finance_ops가 "보상" domain을 공유 → "보상" 질문은 Contested(다툼) 시연.

    `dispatcher`를 주입하면 그 디스패처를 쓴다(분산 회수 경로 테스트용 `WebSocketDispatcher`
    등). 미주입이면 기본 `LocalRuntimeDispatcher`(동기 즉답 — 데모/in-process 기본).
    주입 시 `runtime`은 무시된다(디스패처가 답 획득을 전담).

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
    # dispatch가 곧 답(항상 Delivered). 분산 회수 경로(2b-i)를 검증할 땐 WebSocketDispatcher
    # 를 주입해 dispatched→retrieve 흐름을 결정론으로 본다.
    runtime_impl: AgentRuntime = (
        runtime if runtime is not None else ClaudeCodeRuntime(okf_root=DEMO_OKF_ROOT)
    )
    dispatcher_impl: RuntimeDispatcher = (
        dispatcher if dispatcher is not None else LocalRuntimeDispatcher(runtime_impl)
    )
    def _manager_of(uid: str) -> str | None:
        return registry.get_user(uid).manager if uid in registry.user_ids() else None

    ask = AskOrg(
        router=router,
        dispatcher=dispatcher_impl,
        audit_log=JsonlAuditLog(Path("logs/audit.jsonl")),
        classifier=classifier,
        case_store=case_store,
        review_store=review_store,
        manager_queue_store=manager_queue_store,
        manager_of=_manager_of,
        manager_root=ROOT_USER,
    )
    consensus = ConsensusService(case_store=case_store, precedents=precedents)
    return DemoBundle(
        ask=ask,
        case_store=case_store,
        precedents=precedents,
        consensus=consensus,
        review_store=review_store,
    )


def build_demo_ask_org(runtime: AgentRuntime | None = None) -> AskOrg:
    """하위호환 진입점 — `build_demo().ask`만 돌려준다.

    기존 호출처(채팅 단독 테스트 등)는 공유 store가 필요 없으므로 AskOrg만
    받는다. 처리함과 한 상태를 공유해야 하는 웹은 `build_demo()`를 쓴다.
    `runtime`은 `build_demo`로 그대로 전달한다(테스트는 StubRuntime 주입).
    분산 디스패처(WebSocketDispatcher 등)가 필요하면 `build_demo(dispatcher=...)`를 직접 쓴다.
    """
    return build_demo(runtime=runtime).ask


def cards_for_owner(owner_id: str) -> dict[str, AgentCard]:
    """데모 샘플에서 그 owner가 owns 하는 카드들을 `agent_id → AgentCard`로 추린다.

    Owner Worker(T6.3 슬라이스2b-ii)가 자기 owner 환경의 카드(담당 영역·지식 출처)를
    들고 로컬 claude로 답하기 위한 출처다 — `PushWork`의 `TicketFrame`은 `agent_id`만
    싣고 카드 본문은 안 싣으므로(CONTEXT — 식별자만), 워커가 `agent_id`로 카드를
    되찾는다. 분산 정신상 카드는 owner 환경에 있는 게 맞다(ADR 0011). 데모는 인라인
    `_CARDS`가 출처지만, 실제로는 각 owner PC가 자기 카드 YAML을 들 자리(T1.3·T6.5).
    """
    return {card.agent_id: card for card in _CARDS if card.owner == owner_id}


def demo_delegations(
    snapshot_at: datetime | None = None,
) -> tuple[DelegationSnapshot, ...]:
    """데모 owner들의 위임 스냅샷 메타를 만든다(ADR 0012 결정 3·9, T6.6 슬라이스 iv).

    각 owner가 자기 카드(담당 영역)를 백업 워커에 *명시적으로 위임*한 것으로 본다 —
    backup 워커가 그 owner 이름으로 답하려면 디스패처에 이 위임이 등록돼 있어야 한다
    (opt-in 위임·Authority 중앙, 카드 자기보고 아님). `snapshot_at`을 fresh(기본 지금)로
    잡아 staleness 임계 내에 들게 한다 — 그래야 backup push가 허용된다(결정 9). 실
    데이터 스냅샷 본체·동기화는 후속(여기는 위임 *메타*만, 연결점). 임계 초과(stale)면
    backup 거부→escalation을 시연하려면 `snapshot_at`을 과거로 넘긴다.

    owner별로 그 owner가 owns 하는 카드들을 `agent_ids`로 묶는다(데모 카드는 owner당 1장).
    """
    now = snapshot_at if snapshot_at is not None else datetime.now(timezone.utc)
    by_owner: dict[str, list[str]] = {}
    for card in _CARDS:
        by_owner.setdefault(card.owner, []).append(card.agent_id)
    return tuple(
        DelegationSnapshot(
            owner_id=owner_id,
            agent_ids=tuple(agent_ids),
            snapshot_at=now,
        )
        for owner_id, agent_ids in by_owner.items()
    )
