"""ADR 0038 슬라이스 C — `AskOrg` Routed arm co-grounding 배선 [게이트 내·결정론].

결정론: FakeClassifier→(판례 단축경로로)Routed·StubRuntime/StubStreamingRuntime·
`EdgeGroundingSelector`+`ChainGroundingSelector`(주입 `InMemoryEdgeStore`)·fake
resolver(dict lookup)만 사용. 실 LLM/실 KnowledgeStore 0.

Contested arm과의 핵심 차이(ADR 0038 결정 4): Routed 질문에 합의-소싱 `ComplementEdge`
이웃이 있으면 co-grounded 답을 내되 **ConflictCase는 열지 않는다**(다툼이 아니라 이미
라우팅된 질문). Approval 게이트·`_record_answer`·audit은 일반 Routed와 동일 적용된다.

하위호환 게이트(핵심 리스크): 프로덕션 기본 주입은 `ContestedGroundingSelector`뿐이라
Routed에 대해 selector가 항상 `None`을 반환한다 — 이 슬라이스 후에도 프로덕션 Routed
행동은 100% 그대로다(`TestBackwardCompatibilityProductionSelector`가 그 증거).
"""

from datetime import date, datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_record import InMemoryAnswerRecordStore
from agent_org_network.ask_org import (
    AskOrg,
    Answered,
    DoneEvent,
    MetaEvent,
    PendingEvent,
    TokenEvent,
)
from agent_org_network.audit import AuditEntry, InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.complement import ComplementEdge, InMemoryEdgeStore
from agent_org_network.conflict import InMemoryConflictCaseStore, InMemoryPrecedentStore, Resolution
from agent_org_network.dispatch import LocalRuntimeDispatcher, LocalStreamingDispatcher
from agent_org_network.grounding import (
    ChainGroundingSelector,
    ContestedGroundingSelector,
    EdgeGroundingSelector,
    first_by_agent_id,
)
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime, StubStreamingRuntime
from agent_org_network.user import User


def fixed_clock() -> datetime:
    return datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def card(
    agent_id: str,
    owner: str,
    domains: list[str],
    knowledge_sources: list[str] | None = None,
    approval_when: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary=f"{agent_id} 요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=knowledge_sources or [f"위키/{agent_id}"],
        approval_when=approval_when or [],
    )


def _fake_resolver(agent_id: str) -> str:
    return {"cs_ops": "고객지원 관점", "finance_ops": "회계·정산 관점"}.get(agent_id, "")


CS = card("cs_ops", "owner_CS", ["보상"])
FINANCE = card("finance_ops", "owner_Finance", ["보상"])


def _build(
    *,
    with_edge: bool = True,
    case_store: InMemoryConflictCaseStore | None = None,
    answer_record_store: InMemoryAnswerRecordStore | None = None,
    grounding_selector: object | None = "chain",
    streaming: bool = False,
    approval_when: list[str] | None = None,
) -> tuple[AskOrg, InMemoryAuditLog]:
    """Routed(cs_ops 단독)를 판례 단축경로로 강제하고 co-grounding을 배선한다.

    Registry엔 cs_ops·finance_ops 둘 다 "보상" domain을 갖는다(EdgeGroundingSelector의
    선택시점 재검증 `intent ∈ card.domains`을 만족해야 하므로) — 그러나 `Router`가 분류기
    후보 카운트로 라우팅하면 이는 Contested(후보 2건)가 된다. ADR 0038의 실 시나리오
    ("판례가 쌓이면 다음 질문은 Routed 단독")를 재현하려 `InMemoryPrecedentStore`에
    intent "보상" → primary "cs_ops" 판례를 미리 심어 Router의 판례 단축경로로 Routed를
    강제한다(router.py: precedent가 있으면 candidates 카운트를 보지 않고 즉시 Routed).
    """
    primary_card = card("cs_ops", "owner_CS", ["보상"], approval_when=approval_when or [])
    finance_card = FINANCE
    registry = Registry()
    registry.register(primary_card)
    registry.register(finance_card)

    precedents = InMemoryPrecedentStore(clock=fixed_clock)
    precedents.record(Resolution(intent="보상", primary="cs_ops"))

    classifier = FakeClassifier("보상")
    router = Router(registry, classifier, root_user="root", precedents=precedents)
    audit = InMemoryAuditLog()
    dispatcher = (
        LocalStreamingDispatcher(StubStreamingRuntime(deltas=("공동", "접지", "답")))
        if streaming
        else LocalRuntimeDispatcher(StubRuntime())
    )

    edge_store = InMemoryEdgeStore()
    if with_edge:
        edge_store.record(
            ComplementEdge(intent="보상", primary_id="cs_ops", supporting_id="finance_ops")
        )

    def _lookup(agent_id: str) -> AgentCard | None:
        try:
            return registry.get(agent_id)
        except KeyError:
            return None

    kwargs: dict[str, object] = {}
    if grounding_selector == "chain":
        kwargs["grounding_selector"] = ChainGroundingSelector(
            (
                EdgeGroundingSelector(edge_store=edge_store, card_lookup=_lookup),
                ContestedGroundingSelector(tie_break=first_by_agent_id),
            )
        )
        kwargs["grounding_resolver"] = _fake_resolver
    elif grounding_selector == "contested_only":
        # 프로덕션 현재 주입(ADR 0037) — Routed엔 항상 None을 돌린다(하위호환 회귀 0).
        kwargs["grounding_selector"] = ContestedGroundingSelector(tie_break=first_by_agent_id)
        kwargs["grounding_resolver"] = _fake_resolver
    # grounding_selector is None → 아예 미주입(기존 동작).

    ask = AskOrg(
        router=router,
        dispatcher=dispatcher,
        audit_log=audit,
        clock=fixed_clock,
        case_store=case_store,
        answer_record_store=answer_record_store,
        **kwargs,  # type: ignore[arg-type]
    )
    return ask, audit


# ── 1. Routed + 엣지 → co-grounded Answered, ConflictCase 없음 ──────────────


def test_Routed_질문에_엣지가_있으면_co_grounded_Answered를_낸다() -> None:
    case_store = InMemoryConflictCaseStore()
    ask, _ = _build(with_edge=True, case_store=case_store)
    user = User(id="u1")

    reply = ask.handle("보상 규정이 어떻게 되나요?", user)

    assert isinstance(reply, Answered)
    assert reply.answered_by == ("owner_CS", "cs_ops")
    assert reply.sources == ("위키/cs_ops", "위키/finance_ops")


def test_Routed_co_grounded_답은_ConflictCase를_열지_않는다() -> None:
    """다툼이 아니라 이미 라우팅된 질문이라 사람 합의 경로로 가지 않는다."""
    case_store = InMemoryConflictCaseStore()
    ask, _ = _build(with_edge=True, case_store=case_store)
    user = User(id="u1")

    reply = ask.handle("보상 규정이 어떻게 되나요?", user)

    assert isinstance(reply, Answered)
    assert case_store.open_for_owner("owner_CS") == []
    assert case_store.open_for_owner("owner_Finance") == []


def test_Routed_co_grounded_답도_AnswerRecord와_AuditEntry가_정상_적재된다() -> None:
    case_store = InMemoryConflictCaseStore()
    record_store = InMemoryAnswerRecordStore()
    ask, audit = _build(with_edge=True, case_store=case_store, answer_record_store=record_store)
    user = User(id="u1")

    reply = ask.handle("보상 규정이 어떻게 되나요?", user)

    assert isinstance(reply, Answered)
    assert reply.record_id is not None
    assert len(record_store.for_agent("cs_ops")) == 1
    assert len(audit.entries) == 1
    entry: AuditEntry = audit.entries[-1]
    assert entry.decision.__class__.__name__ == "Routed"


# ── 2. Routed + 엣지 없음 → 기존 단일 접지 Routed(회귀 0·스냅샷 비교) ────────


def test_Routed_엣지_없으면_기존_단일_접지_Routed와_동일하다() -> None:
    """co-grounding selector가 주입돼도(엣지가 없으니) None 폴백 — 미주입 baseline과 동일."""
    ask_with_grounding, _ = _build(with_edge=False, grounding_selector="chain")
    ask_baseline, _ = _build(with_edge=False, grounding_selector=None)
    user = User(id="u1")

    reply_grounding = ask_with_grounding.handle("보상 규정이 어떻게 되나요?", user)
    reply_baseline = ask_baseline.handle("보상 규정이 어떻게 되나요?", user)

    assert isinstance(reply_grounding, Answered)
    assert isinstance(reply_baseline, Answered)
    assert reply_grounding.text == reply_baseline.text
    assert reply_grounding.answered_by == reply_baseline.answered_by == ("owner_CS", "cs_ops")
    assert reply_grounding.sources == reply_baseline.sources == ("위키/cs_ops",)


# ── 3. 스트리밍 — meta→token*→done·PendingEvent 없음·ConflictCase 없음 ──────


def test_Routed_스트림_엣지있으면_meta_token_done만_방출한다() -> None:
    case_store = InMemoryConflictCaseStore()
    ask, _ = _build(with_edge=True, case_store=case_store, streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("보상 규정이 어떻게 되나요?", user))

    assert isinstance(events[0], MetaEvent)
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, PendingEvent) for e in events)
    assert any(isinstance(e, TokenEvent) for e in events)
    assert case_store.open_for_owner("owner_CS") == []


def test_Routed_스트림_meta_answered_by는_primary이고_sources는_병합이다() -> None:
    ask, _ = _build(with_edge=True, streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("보상 규정이 어떻게 되나요?", user))

    meta = next(e for e in events if isinstance(e, MetaEvent))
    assert meta.answered_by == ("owner_CS", "cs_ops")
    assert meta.sources == ("위키/cs_ops", "위키/finance_ops")

    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.sources == ("위키/cs_ops", "위키/finance_ops")


def test_Routed_스트림_엣지없으면_기존_동작() -> None:
    ask, _ = _build(with_edge=False, streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("보상 규정이 어떻게 되나요?", user))

    assert isinstance(events[0], MetaEvent)
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, PendingEvent) for e in events)


# ── 4. Contested arm 무변경(ADR 0037 회귀) ──────────────────────────────────


def test_Contested_arm은_이_슬라이스로_무변경이다() -> None:
    """Routed 배선(슬라이스 C)이 Contested의 답+합의 병행 계약을 건드리지 않는다."""
    from agent_org_network.decision import Contested

    cs = card("cs_ops", "owner_CS", ["환불"])
    sales = card("sales_ops", "owner_Sales", ["환불"])
    registry = Registry()
    registry.register(cs)
    registry.register(sales)
    router = Router(registry, FakeClassifier("환불"), root_user="root")
    case_store = InMemoryConflictCaseStore()
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        clock=fixed_clock,
        case_store=case_store,
        grounding_selector=ContestedGroundingSelector(tie_break=first_by_agent_id),
        grounding_resolver=_fake_resolver,
    )
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Answered)
    assert len(case_store.open_for_owner("owner_CS")) == 1  # Contested는 여전히 ConflictCase open.
    assert isinstance(router.route("환불 되나요?"), Contested)


# ── 5. 하위호환(프로덕션 회귀 0) ─────────────────────────────────────────────


class TestBackwardCompatibilityProductionSelector:
    """프로덕션 현재 주입(`ContestedGroundingSelector`만)이면 Routed는 항상 None 폴백."""

    def test_ContestedGroundingSelector만_주입되면_Routed는_기존_단일_접지다(self) -> None:
        ask_prod, _ = _build(with_edge=True, grounding_selector="contested_only")
        ask_none, _ = _build(with_edge=True, grounding_selector=None)
        user = User(id="u1")

        reply_prod = ask_prod.handle("보상 규정이 어떻게 되나요?", user)
        reply_none = ask_none.handle("보상 규정이 어떻게 되나요?", user)

        assert isinstance(reply_prod, Answered)
        assert isinstance(reply_none, Answered)
        assert reply_prod.text == reply_none.text
        assert reply_prod.sources == reply_none.sources == ("위키/cs_ops",)

    def test_selector_미주입이면_Routed는_기존_동작이다(self) -> None:
        ask, _ = _build(with_edge=True, grounding_selector=None)
        user = User(id="u1")

        reply = ask.handle("보상 규정이 어떻게 되나요?", user)

        assert isinstance(reply, Answered)
        assert reply.sources == ("위키/cs_ops",)


# ── 6. Authority/노출 불변식 ─────────────────────────────────────────────────


_LEAKY_KEYS = {
    "confidence",
    "candidates",
    "escalated_to",
    "reason",
    "primary",
    "intent",
    "grounding",
    "grounding_set",
    "supporting",
    "contested",
    "edge",
    "edges",
    "complement",
}


def test_Routed_co_grounded_project는_내부값을_싣지_않는다() -> None:
    from agent_org_network.ask_org import project_answered

    ask, _ = _build(with_edge=True)
    user = User(id="u1")

    reply = ask.handle("보상 규정이 어떻게 되나요?", user)
    assert isinstance(reply, Answered)
    body = project_answered(reply)

    assert _LEAKY_KEYS.isdisjoint(set(body.keys()))
    for value in body.values():
        if isinstance(value, dict):
            assert _LEAKY_KEYS.isdisjoint(set(value.keys()))  # pyright: ignore[reportUnknownArgumentType]


def test_Routed_co_grounded_answered_by는_primary_단일이다() -> None:
    """엣지 supporting(finance_ops)의 owner는 answered_by에 절대 안 실린다(Authority=primary)."""
    ask, _ = _build(with_edge=True)
    user = User(id="u1")

    reply = ask.handle("보상 규정이 어떻게 되나요?", user)

    assert isinstance(reply, Answered)
    assert reply.answered_by == ("owner_CS", "cs_ops")
    assert "owner_Finance" not in reply.answered_by
    assert "finance_ops" not in reply.answered_by


def test_Routed_스트림_meta_done_페이로드에_내부값_0() -> None:
    from agent_org_network.ask_org import serialize_sse_event

    ask, _ = _build(with_edge=True, streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("보상 규정이 어떻게 되나요?", user))
    for e in events:
        frame = serialize_sse_event(e)
        for leaky in _LEAKY_KEYS:
            assert f'"{leaky}"' not in frame


# ── 7. Approval 게이트 정합 ──────────────────────────────────────────────────


def test_Routed_co_grounded가_requires_approval이면_draft_only로_하향된다() -> None:
    """co-grounding이 Approval 게이트를 우회하지 않는다(카드 approval_when="보상")."""
    ask, _ = _build(with_edge=True, approval_when=["보상"])
    user = User(id="u1")

    reply = ask.handle("보상 규정이 어떻게 되나요?", user)

    assert isinstance(reply, Answered)
    assert reply.mode == "draft_only"


def test_Routed_co_grounded_스트림도_Approval_게이트가_적용된다() -> None:
    ask, _ = _build(with_edge=True, approval_when=["보상"], streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("보상 규정이 어떻게 되나요?", user))

    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.mode == "draft_only"
