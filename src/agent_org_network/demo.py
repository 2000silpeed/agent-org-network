"""데모 조립 팩토리 — 하드코딩 샘플로 AskOrg를 한 개 만든다.

인라인 `_USERS`/`_CARDS`는 `registry/`의 YAML(T1.3 로더·T6.4 골든셋)과 *내용 같은 다른 출처*다 —
둘을 동기화해 데모와 골든셋이 같은 카드 셋을 본다. 눈으로 보는 end-to-end 한 바퀴(웹챗)용 조립이다.
"""

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from typing import TYPE_CHECKING

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg
from agent_org_network.audit import (
    AuditReader,
    InMemoryAuditLog,
    JsonlAuditLog,
)
from agent_org_network.classifier import Classifier, LlmClassifier, RuleBasedClassifier
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
from agent_org_network.index_matcher import ConceptOverlapMatcher
from agent_org_network.okf_index import build_knowledge_index_from_okf
from agent_org_network.registry import Registry
from agent_org_network.router import Router, RouterPort
from agent_org_network.runtime import AgentRuntime, ClaudeCodeRuntime
from agent_org_network.two_stage_router import (
    InMemoryPublishedIndexStore,
    TwoStageRouter,
)
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

# 감사 로그(T5.1) 기본 파일 경로. `build_demo(audit_log=None)`이 폴백하는 production
# 기본값을 모듈 상수로 노출해, 결정론 테스트가 conftest autouse fixture에서 tmp 경로로
# 치환(monkeypatch)해 실제 `logs/`를 더럽히지 않게 한다(TRD §7 격리). 모듈 전역 조회라
# 호출 시점에 치환이 반영된다.
_DEFAULT_AUDIT_LOG_PATH = Path("logs/audit.jsonl")

# 데모 User 6명. email은 SSO 매핑 baseline의 키(T7.1·ADR 0021 결정 3 — verified email →
# 이 User). single tenant 사내 가정이라 한 회사 도메인(example.com)을 쓴다. 매핑은 User.email이
# SSOT이고 resolve_identity가 verified email == user.email로 잇는다.
_USERS: tuple[User, ...] = (
    User(id=ROOT_USER, email="root.manager@example.com"),
    User(id="legal_lead", manager=ROOT_USER, email="legal.lead@example.com"),
    User(id="cs_lead", manager=ROOT_USER, email="cs.lead@example.com"),
    User(id="finance_lead", manager=ROOT_USER, email="finance.lead@example.com"),
    User(id="hr_lead", manager=ROOT_USER, email="hr.lead@example.com"),
    User(id="it_lead", manager=ROOT_USER, email="it.lead@example.com"),
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
    AgentCard(
        agent_id="hr_ops",
        owner="hr_lead",
        team="hr",
        summary="채용 절차, 휴가 정책, 직원 평가 기준을 안내합니다. 급여이체 실행은 담당하지 않습니다.",
        domains=["채용", "휴가", "평가", "급여이체"],
        cannot_answer=["급여이체"],
        approval_when=["평가"],
        last_reviewed_at=_REVIEWED,
        knowledge_sources=["위키/채용가이드", "Notion/휴가규정", "위키/평가기준"],
    ),
    AgentCard(
        agent_id="it_ops",
        owner="it_lead",
        team="it",
        summary="계정 관리, 접근 권한 요청, 보안 사고 대응을 안내합니다.",
        domains=["계정", "접근권한", "보안"],
        approval_when=["접근권한"],
        last_reviewed_at=_REVIEWED,
        knowledge_sources=["위키/계정정책", "Notion/접근권한가이드", "위키/보안대응절차"],
    ),
)

_KEYWORD_INTENTS: dict[str, str] = {
    "계약": "계약 검토",
    "환불": "환불",
    "가격": "가격",
    "보상": "보상",
    "채용": "채용",
    "입사": "채용",
    "휴가": "휴가",
    "연차": "휴가",
    "평가": "평가",
    "급여이체": "급여이체",
    "계정": "계정",
    "접근권한": "접근권한",
    "보안": "보안",
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

    `audit_reader`(T5.1 운영 모니터링): `ask`가 쓰는 *바로 그* `AuditLog`를
    읽기 포트(`AuditReader`)로도 노출하는 읽을 손잡이다. 두 구현체(JsonlAuditLog·
    InMemoryAuditLog)가 record(쓰기)+records/record_at(읽기)를 다 구현하므로,
    `ask`(쓰기)와 모니터링 면(읽기)이 *같은 인스턴스*를 본다. 그동안 build_demo가
    audit_log를 내부 하드코딩하고 노출하지 않아 읽을 손잡이가 없던 것을 연다.
    """

    ask: AskOrg
    case_store: ConflictCaseStore
    precedents: PrecedentStore
    consensus: ConsensusService
    registry: Registry
    review_store: "BackupReviewStore | None" = None
    audit_reader: AuditReader | None = None


# 데모 인덱스 라우팅 시드의 고정 generated_at — OKF→인덱스 도출이 결정론이 되도록
# now()가 아닌 고정 타임스탬프를 주입한다(staleness는 T10.4 책임·여기선 시드만).
_INDEX_SEED_AT = datetime(2026, 6, 28, 0, 0, 0, tzinfo=timezone.utc)


def select_router(
    flag: str,
    registry: Registry,
    classifier: Classifier,
    precedents: PrecedentStore,
) -> RouterPort:
    """`AON_ROUTER` 플래그로 라우터 구현을 *결정론*으로 고른다(게이트 내 테스트 가능).

    - flag == "index" → `TwoStageRouter`(published 지식 인덱스 기반 2단 라우팅).
        데모 지름길: 중앙이 repo `okf/`를 직접 읽어 owner별 KnowledgeIndex를 *시드*한다
        (실 경로는 owner publish — T10.4·okf_index 모듈 docstring 참조). 중앙은 여전히
        목차만 보유(내용 0). assessor=None이라 단일 담당은 Routed·≥2는 Contested(stage-2
        자동해소 없음 — T10.5 owner측 RAG 전).
    - 그 외(미설정·임의 문자열) → 기존 `Router`(분류기 기반·기본·무회귀).

    선택 함수로 분리해 와이어 분기를 게이트 내에서 단언한다(env 미설정 시 옛 Router 보존).
    """
    if flag == "index":
        store = InMemoryPublishedIndexStore(
            [
                build_knowledge_index_from_okf(
                    card, DEMO_OKF_ROOT, generated_at=_INDEX_SEED_AT
                )
                for card in registry.all_cards()
            ]
        )
        matcher = ConceptOverlapMatcher()
        return TwoStageRouter(
            registry,
            matcher,
            store,
            root_user=ROOT_USER,
            precedents=precedents,
            assessor=None,
        )
    return Router(registry, classifier, root_user=ROOT_USER, precedents=precedents)


def build_demo(
    runtime: AgentRuntime | None = None,
    dispatcher: RuntimeDispatcher | None = None,
    review_store: "BackupReviewStore | None" = None,
    manager_queue_store: "ManagerQueueStore | None" = None,
    audit_log: "JsonlAuditLog | InMemoryAuditLog | None" = None,
    classifier: Classifier | None = None,
) -> DemoBundle:
    """하드코딩 샘플로 조립한 데모 한 벌(공유 store)을 돌려준다.

    카드 5종(contract_ops·cs_ops·finance_ops·hr_ops·it_ops) + 루트 매니저 포함 유저 6명.
    분류기는 키워드 규칙(계약/환불/가격/보상/채용·휴가·평가·급여이체/계정·접근권한·보안).
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

    # 분류기 선택(주입 > env > 기본 키워드). 기본은 결정론 `RuleBasedClassifier`(게이트·
    # 테스트 안전 — env 미설정이면 기존 동작 그대로). `AON_CLASSIFIER=llm`이면 정교한
    # `LlmClassifier`(실 claude Haiku로 자연어 질문→intent, T6.2·ADR 0010 정신 중앙 키 0).
    # intent 어휘 = 레지스트리 도메인 합집합(라우터가 매칭하는 바로 그 라벨) — 미분류("")는
    # 0매칭→Unowned(미아 없음). 분류는 `router.route`에서 질문당 1회뿐(ADR 0015 단일 출처·
    # ask_org는 `decision.intent` 재사용·재분류 없음)이라 비결정 분류기 도입이 안전하다.
    if classifier is None:
        if os.environ.get("AON_CLASSIFIER", "").strip().lower() == "llm":
            intents = sorted({d for c in registry.all_cards() for d in c.domains})
            classifier = LlmClassifier(intents=intents)
        else:
            classifier = RuleBasedClassifier(_KEYWORD_INTENTS)
    precedents = InMemoryPrecedentStore()
    case_store = InMemoryConflictCaseStore()
    # 라우터 선택(AON_ROUTER 플래그·기본 무회귀). 미설정/기타 → 기존 Router(분류기 기반),
    # AON_ROUTER=index → TwoStageRouter(인덱스 기반·OKF 시드). 선택은 select_router가
    # 결정론으로 한다(게이트 내 테스트). 데모 지름길 주석은 select_router·okf_index 참조.
    router_flag = os.environ.get("AON_ROUTER", "").strip().lower()
    router = select_router(router_flag, registry, classifier, precedents)
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

    # 감사 로그(T5.1): 그동안 내부 하드코딩(JsonlAuditLog)이라 읽을 손잡이가 없었다.
    # 이제 한 인스턴스를 잡아 `ask`(쓰기)와 `DemoBundle.audit_reader`(읽기)에 *같이*
    # 넘긴다 — 모니터링 면이 ask가 쓴 바로 그 로그를 읽는다. 기본은 파일(JSONL),
    # 결정론 테스트는 InMemoryAuditLog를 주입(파일 IO 없는 모니터링 라운드).
    audit_impl: JsonlAuditLog | InMemoryAuditLog = (
        audit_log if audit_log is not None else JsonlAuditLog(_DEFAULT_AUDIT_LOG_PATH)
    )

    ask = AskOrg(
        router=router,
        dispatcher=dispatcher_impl,
        audit_log=audit_impl,
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
        registry=registry,
        review_store=review_store,
        audit_reader=audit_impl,
    )


def build_demo_ask_org(runtime: AgentRuntime | None = None) -> AskOrg:
    """하위호환 진입점 — `build_demo().ask`만 돌려준다.

    기존 호출처(채팅 단독 테스트 등)는 공유 store가 필요 없으므로 AskOrg만
    받는다. 처리함과 한 상태를 공유해야 하는 웹은 `build_demo()`를 쓴다.
    `runtime`은 `build_demo`로 그대로 전달한다(테스트는 StubRuntime 주입).
    분산 디스패처(WebSocketDispatcher 등)가 필요하면 `build_demo(dispatcher=...)`를 직접 쓴다.
    """
    return build_demo(runtime=runtime).ask


def demo_keyword_intents() -> dict[str, str]:
    """데모 키워드→intent 매핑의 복사본 — eval CLI의 `rule` 분류기 등 외부 소비용 공개 접근자.

    `_KEYWORD_INTENTS`는 모듈 내부 상수라, 모듈 밖(eval.py 등)에서는 이 함수로 *복사본*을
    받는다(private 직접 참조 회피·원본 불변 보존).
    """
    return dict(_KEYWORD_INTENTS)


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
