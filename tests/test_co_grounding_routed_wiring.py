"""ADR 0038 슬라이스 D — Routed co-grounding 프로덕션 실배선·활성화 [게이트 내·결정론].

결정론: 실 LLM 0. 프로덕션 조립(`build_demo`)을 `StubRuntime`+in-memory
`InMemoryKnowledgeStore`로 돌린다. 실 D1→D2(실 LLM 보상 질문 재현)는 게이트 밖 수동.

검증 대상:
  1. 공유 `EdgeStore` — 합의(`ConsensusService`)가 쓴 엣지를 라우팅
     (`EdgeGroundingSelector`)이 읽는다(별개 통 아님·ADR 0038 결정 2).
  2. `ChainGroundingSelector` 활성 — 합의(keep_as_complement) 후 다음 같은 intent
     Routed 질문이 이웃 지식을 함께 접지한다(반쪽 답 → 완전한 답).
  3. 회귀 0 — 엣지 없는 Routed는 기존 단일 접지. Contested arm(ADR 0037) 무변경.
  4. 분산 non-Delivered 방어 — 온라인 워커 async 수령이면 빈 답이 아니라
     tracking/Pending 폴백(code-reviewer 관찰 3).
"""

from datetime import date, datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg, Answered, Pending
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.complement import ComplementEdge, InMemoryEdgeStore
from agent_org_network.conflict import (
    Agreed,
    ConcurOnPrimary,
    InMemoryPrecedentStore,
    Resolution,
)
from agent_org_network.demo import build_demo
from agent_org_network.dispatch import InMemoryWorkQueueDispatcher
from agent_org_network.grounding import (
    ChainGroundingSelector,
    ContestedGroundingSelector,
    EdgeGroundingSelector,
)
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc
from agent_org_network.provider_runtime import make_grounding_resolver
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User


def _fresh() -> datetime:
    return datetime.now(timezone.utc)


def _seed_knowledge() -> InMemoryKnowledgeStore:
    """데모 cs_ops·finance_ops 본문을 담은 중앙 KnowledgeStore(결정론 접지원)."""
    ks = InMemoryKnowledgeStore()
    ks.put(
        KnowledgeBundleContent(
            agent_id="cs_ops",
            documents=(KnowledgeDoc(path="cs.md", body="환불은 7일 내 가능합니다"),),
            version="1",
            synced_at=_fresh(),
        )
    )
    ks.put(
        KnowledgeBundleContent(
            agent_id="finance_ops",
            documents=(KnowledgeDoc(path="fin.md", body="보상 정산은 익월에 처리합니다"),),
            version="1",
            synced_at=_fresh(),
        )
    )
    return ks


# ── 1. 프로덕션 앱 경로 end-to-end: 합의→엣지→다음 Routed가 co-ground ──────────


def test_합의_후_다음_보상_질문은_Routed지만_이웃_지식까지_접지한다() -> None:
    runtime = StubRuntime()
    ks = _seed_knowledge()
    bundle = build_demo(
        runtime=runtime, audit_log=InMemoryAuditLog(), knowledge_store=ks
    )
    user = User(id="u1")

    # D1 — 첫 "보상" 질문: Contested → co-grounded 답 + ConflictCase 개설(ADR 0037).
    first = bundle.ask.handle("보상 규정 알려줘", user)
    assert isinstance(first, Answered)
    case = bundle.case_store.open_for_intent("보상")
    assert case is not None

    # 합의: 두 owner가 cs_ops를 primary로 지목. 진 후보(finance_ops) owner가
    # keep_as_complement를 명시 선언 → ComplementEdge(보상, cs_ops→finance_ops) 방출.
    bundle.consensus.concur(
        case.case_id, ConcurOnPrimary(by_owner="cs_lead", on_agent="cs_ops")
    )
    outcome = bundle.consensus.concur(
        case.case_id,
        ConcurOnPrimary(
            by_owner="finance_lead", on_agent="cs_ops", stance="keep_as_complement"
        ),
    )
    assert isinstance(outcome, Agreed)

    # D2 — 합의 후 다음 "보상" 질문: 판례로 Routed(cs_ops)지만 엣지 이웃까지 co-ground.
    second = bundle.ask.handle("보상 얼마나 받나요", user)

    assert isinstance(second, Answered)
    # ConflictCase는 새로 열리지 않는다(다툼 아님·Routed).
    assert bundle.case_store.open_for_intent("보상") is None
    # answered_by는 라우팅 front(primary) 단일 — Authority 정합.
    assert second.answered_by == ("cs_lead", "cs_ops")
    # 접지 본문에 primary(cs_ops)와 이웃(finance_ops) 지식이 *둘 다* 병합됐다(완전한 답).
    assert runtime.last_grounding is not None
    assert "환불은 7일 내 가능합니다" in runtime.last_grounding
    assert "보상 정산은 익월에 처리합니다" in runtime.last_grounding
    # sources도 primary+supporting knowledge_sources 병합(cs_ops·finance_ops).
    assert "위키/환불정책" in second.sources
    assert "Notion/가격표" in second.sources


# ── 2. 공유 인스턴스 실증: 합의가 쓴 엣지를 EdgeGroundingSelector가 읽는다 ────────


def test_ConsensusService가_쓴_엣지를_EdgeGroundingSelector가_같은_store로_읽는다() -> None:
    ks = _seed_knowledge()
    bundle = build_demo(
        runtime=StubRuntime(), audit_log=InMemoryAuditLog(), knowledge_store=ks
    )
    user = User(id="u1")

    bundle.ask.handle("보상 규정", user)
    case = bundle.case_store.open_for_intent("보상")
    assert case is not None
    bundle.consensus.concur(
        case.case_id, ConcurOnPrimary(by_owner="cs_lead", on_agent="cs_ops")
    )
    bundle.consensus.concur(
        case.case_id,
        ConcurOnPrimary(
            by_owner="finance_lead", on_agent="cs_ops", stance="keep_as_complement"
        ),
    )

    # DemoBundle이 노출한 *바로 그* edge_store에 합의가 방출한 엣지가 보인다 —
    # ask의 EdgeGroundingSelector도 이 인스턴스를 읽는다(별개 통이면 위 D2가 실패).
    assert bundle.edge_store is not None
    assert bundle.edge_store.neighbors("보상", "cs_ops") == ("finance_ops",)


def test_기본_withdraw_표는_엣지를_방출하지_않아_Routed는_단일_접지다() -> None:
    """진 owner가 keep_as_complement를 선언하지 않으면(기본 withdraw) 엣지 0 → 회귀 0."""
    runtime = StubRuntime()
    ks = _seed_knowledge()
    bundle = build_demo(
        runtime=runtime, audit_log=InMemoryAuditLog(), knowledge_store=ks
    )
    user = User(id="u1")

    bundle.ask.handle("보상 규정", user)
    case = bundle.case_store.open_for_intent("보상")
    assert case is not None
    bundle.consensus.concur(
        case.case_id, ConcurOnPrimary(by_owner="cs_lead", on_agent="cs_ops")
    )
    bundle.consensus.concur(
        case.case_id, ConcurOnPrimary(by_owner="finance_lead", on_agent="cs_ops")
    )  # 기본 withdraw

    assert bundle.edge_store is not None
    assert bundle.edge_store.neighbors("보상", "cs_ops") == ()

    second = bundle.ask.handle("보상 얼마", user)
    assert isinstance(second, Answered)
    # 이웃 지식 미접지 — finance_ops 본문이 grounding에 없다(단일 접지).
    assert runtime.last_grounding is None or "익월" not in (runtime.last_grounding or "")
    assert "Notion/가격표" not in second.sources


# ── 3. 회귀 0: 엣지 없는 Routed는 기존 단일 접지(프로덕션 스냅샷) ────────────────


def test_엣지_없는_단일담당_Routed는_co_grounding_없이_단일_접지다() -> None:
    runtime = StubRuntime()
    ks = _seed_knowledge()
    bundle = build_demo(
        runtime=runtime, audit_log=InMemoryAuditLog(), knowledge_store=ks
    )
    user = User(id="u1")

    # "계약"은 contract_ops 단독 담당 — 다툼도 엣지도 없다.
    reply = bundle.ask.handle("계약 검토 요청", user)

    assert isinstance(reply, Answered)
    assert reply.answered_by == ("legal_lead", "contract_ops")
    # 단일 접지 Routed는 co-grounding 경로를 안 타 grounding 인자 자체가 전달되지 않는다.
    assert runtime.last_grounding is None
    # sources는 primary(contract_ops) 단일 파생.
    assert set(reply.sources) == {"위키/계약가이드", "Notion/표준계약서"}


# ── 4. 분산 non-Delivered 방어(code-reviewer 관찰 3) ────────────────────────────


def _routed_edge_ask(dispatcher: InMemoryWorkQueueDispatcher) -> AskOrg:
    """판례로 Routed(cs_ops)·엣지(cs_ops→sales_ops) co-grounding이 켜진 AskOrg 조립."""
    cs = AgentCard(
        agent_id="cs_ops",
        owner="owner_CS",
        team="cs",
        summary="cs",
        domains=["환불"],
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=["위키/cs"],
    )
    sales = AgentCard(
        agent_id="sales_ops",
        owner="owner_Sales",
        team="sales",
        summary="sales",
        domains=["환불"],
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=["위키/sales"],
    )
    registry = Registry()
    registry.register(cs)
    registry.register(sales)
    precedents = InMemoryPrecedentStore()
    precedents.record(Resolution(intent="환불", primary="cs_ops"))
    router = Router(
        registry, FakeClassifier("환불"), root_user="root", precedents=precedents
    )
    edge_store = InMemoryEdgeStore()
    edge_store.record(
        ComplementEdge(intent="환불", primary_id="cs_ops", supporting_id="sales_ops")
    )

    def _lookup(agent_id: str) -> AgentCard | None:
        try:
            return registry.get(agent_id)
        except KeyError:
            return None

    selector = ChainGroundingSelector(
        (EdgeGroundingSelector(edge_store, _lookup), ContestedGroundingSelector())
    )
    return AskOrg(
        router=router,
        dispatcher=dispatcher,
        audit_log=InMemoryAuditLog(),
        grounding_selector=selector,
        grounding_resolver=make_grounding_resolver(InMemoryKnowledgeStore()),
    )


def test_co_grounded_Routed가_미회신이면_빈_답_대신_Pending_dispatched로_폴백한다() -> None:
    """온라인 워커 async 수령(non-Delivered) — 빈 co-grounded 답을 사용자에 내보내지 않는다."""
    dispatcher = InMemoryWorkQueueDispatcher()  # 워커 없음 → poll은 AwaitingWorker.
    ask = _routed_edge_ask(dispatcher)

    reply = ask.handle("환불 되나요?", User(id="u1"))

    assert isinstance(reply, Pending)
    assert reply.kind == "dispatched"
    assert reply.tracking is not None
    # 빈 Answered가 아니다 — 사용자는 회수 가능한 Pending을 받는다(미아 없음).


def test_미회신_폴백의_tracking으로_나중에_답을_회수할_수_있다() -> None:
    """같은 ticket 재사용 — 워커가 나중에 회신하면 retrieve로 (단일 접지) 답을 회수."""
    from agent_org_network.runtime import Answer

    dispatcher = InMemoryWorkQueueDispatcher()
    ask = _routed_edge_ask(dispatcher)

    reply = ask.handle("환불 되나요?", User(id="u1"))
    assert isinstance(reply, Pending)
    assert reply.tracking is not None

    # 워커가 그 ticket을 claim·submit(async 회신).
    ticket = dispatcher.claim("owner_CS")
    assert ticket is not None
    dispatcher.submit(ticket.ticket_id, Answer(text="환불 답", sources=(), mode="full"))

    recovered = ask.retrieve(reply.tracking)
    assert isinstance(recovered, Answered)
    assert recovered.text == "환불 답"
    assert recovered.answered_by == ("owner_CS", "cs_ops")


def test_co_grounded_Routed_스트림이_미회신이면_meta후_PendingEvent로_종착한다() -> None:
    """handle_stream 대칭 — 빈 token(text="") 대신 meta→pending(dispatched)으로 종착."""
    from agent_org_network.ask_org import MetaEvent, PendingEvent, TokenEvent

    dispatcher = InMemoryWorkQueueDispatcher()
    ask = _routed_edge_ask(dispatcher)

    events = list(ask.handle_stream("환불 되나요?", User(id="u1")))

    assert isinstance(events[0], MetaEvent)
    assert isinstance(events[-1], PendingEvent)
    assert events[-1].kind == "dispatched"
    assert events[-1].tracking is not None
    # 빈 token(co-grounded reply.text=="")은 새어나가지 않는다.
    assert not any(isinstance(e, TokenEvent) and e.text == "" for e in events)
