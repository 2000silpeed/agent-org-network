"""ADR 0037 슬라이스 C — Contested arm 답+합의 병행(co-grounding) [게이트 내·결정론].

결정론: FakeClassifier→Contested·StubRuntime/StubStreamingRuntime·
ContestedGroundingSelector(주입 tie_break)·fake resolver(dict lookup)만 사용.
실 LLM/실 KnowledgeStore 0.

하위호환 게이트(핵심 리스크): `AskOrg`/`SessionAskOrg`가 `grounding_selector`+
`grounding_resolver`를 *둘 다* 주입받았을 때만 Contested가 co-grounded 답을
낸다. 미주입이면 기존 Pending 동작 100% 유지(회귀 0) — 이 파일의
`TestBackwardCompatibilityGateOff`가 그 증거.
"""

from datetime import date, datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_record import InMemoryAnswerRecordStore
from agent_org_network.ask_org import (
    AskOrg,
    Answered,
    DoneEvent,
    MetaEvent,
    Pending,
    PendingEvent,
    TokenEvent,
)
from agent_org_network.audit import AuditEntry, InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.conflict import InMemoryConflictCaseStore
from agent_org_network.dispatch import LocalRuntimeDispatcher, LocalStreamingDispatcher
from agent_org_network.grounding import ContestedGroundingSelector, first_by_agent_id
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime, StubStreamingRuntime
from agent_org_network.user import User


def fixed_clock() -> datetime:
    return datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def card(
    agent_id: str,
    owner: str,
    domains: list[str],
    knowledge_sources: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary=f"{agent_id} 요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=knowledge_sources or [f"위키/{agent_id}"],
    )


def _fake_resolver(agent_id: str) -> str:
    return {"cs_ops": "고객지원 지식", "sales_ops": "영업 지식"}.get(agent_id, "")


def build_contested_ask_org(
    *,
    case_store: InMemoryConflictCaseStore | None = None,
    answer_record_store: InMemoryAnswerRecordStore | None = None,
    with_co_grounding: bool = True,
    streaming: bool = False,
) -> tuple[AskOrg, InMemoryAuditLog]:
    cs = card("cs_ops", "owner_CS", ["환불"])
    sales = card("sales_ops", "owner_Sales", ["환불"])
    registry = Registry()
    registry.register(cs)
    registry.register(sales)
    classifier = FakeClassifier("환불")
    router = Router(registry, classifier, root_user="root")
    audit = InMemoryAuditLog()
    dispatcher = (
        LocalStreamingDispatcher(StubStreamingRuntime(deltas=("공동", "접지", "답")))
        if streaming
        else LocalRuntimeDispatcher(StubRuntime())
    )
    kwargs: dict[str, object] = {}
    if with_co_grounding:
        kwargs["grounding_selector"] = ContestedGroundingSelector(tie_break=first_by_agent_id)
        kwargs["grounding_resolver"] = _fake_resolver
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


# ── 1. Contested→Answered+ConflictCase 동시 ─────────────────────────────────


def test_Contested_질문은_co_grounded_Answered와_ConflictCase를_동시에_낸다() -> None:
    case_store = InMemoryConflictCaseStore()
    ask, _ = build_contested_ask_org(case_store=case_store)
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Answered)
    cases_cs = case_store.open_for_owner("owner_CS")
    cases_sales = case_store.open_for_owner("owner_Sales")
    assert len(cases_cs) == 1
    assert len(cases_sales) == 1
    assert cases_cs[0].case_id == cases_sales[0].case_id


# ── 2. primary = tie-break ──────────────────────────────────────────────────


def test_Answered_answered_by는_tie_break이_고른_primary() -> None:
    ask, _ = build_contested_ask_org()
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Answered)
    # first_by_agent_id: agent_id 사전순 최소 = "cs_ops" < "sales_ops"
    assert reply.answered_by == ("owner_CS", "cs_ops")


# ── 2-b. sources = primary+supporting 병합 (ADR 0037 결정 2·6·code-reviewer Minor 1) ──


def test_co_grounded_Answered_sources는_primary와_supporting_knowledge_sources_병합이다() -> None:
    """cs_ops(primary)=위키/cs_ops·sales_ops(supporting)=위키/sales_ops → 둘 다 sources에."""
    ask, _ = build_contested_ask_org()
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Answered)
    assert reply.sources == ("위키/cs_ops", "위키/sales_ops")


def test_co_grounded_sources_순서는_primary_먼저_supporting_순서대로_결정론이다() -> None:
    """agent_ids() 순서(primary, *supporting)와 같은 순서로 sources가 병합된다."""
    ask, _ = build_contested_ask_org()
    user = User(id="u1")

    reply1 = ask.handle("환불 되나요?", user)
    reply2 = ask.handle("환불 정책이 어떻게 되나요?", user)

    assert isinstance(reply1, Answered)
    assert isinstance(reply2, Answered)
    assert reply1.sources == reply2.sources == ("위키/cs_ops", "위키/sales_ops")


def test_co_grounded_sources는_중복_레이블을_제거한다() -> None:
    """primary·supporting이 같은 출처 레이블을 공유해도 sources에 한 번만 실린다."""
    cs = card("cs_ops", "owner_CS", ["환불"], knowledge_sources=["위키/공통정책", "위키/cs_ops"])
    sales = card(
        "sales_ops", "owner_Sales", ["환불"], knowledge_sources=["위키/공통정책", "위키/sales_ops"]
    )
    registry = Registry()
    registry.register(cs)
    registry.register(sales)
    router = Router(registry, FakeClassifier("환불"), root_user="root")
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        clock=fixed_clock,
        grounding_selector=ContestedGroundingSelector(tie_break=first_by_agent_id),
        grounding_resolver=_fake_resolver,
    )

    reply = ask.handle("환불 되나요?", User(id="u1"))

    assert isinstance(reply, Answered)
    assert reply.sources == ("위키/공통정책", "위키/cs_ops", "위키/sales_ops")


def test_co_grounded_스트림_MetaEvent_sources도_병합이다() -> None:
    ask, _ = build_contested_ask_org(streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("환불 되나요?", user))

    meta = next(e for e in events if isinstance(e, MetaEvent))
    assert meta.sources == ("위키/cs_ops", "위키/sales_ops")


def test_co_grounded_스트림_DoneEvent_sources도_병합이다() -> None:
    ask, _ = build_contested_ask_org(streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("환불 되나요?", user))

    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.sources == ("위키/cs_ops", "위키/sales_ops")


def test_비_co_grounding_경로의_sources는_기존과_동일하다() -> None:
    """회귀 0 — selector/resolver 미주입이면 Pending이라 sources 자체가 없다(변경 대상 아님).

    Routed(비 Contested) 경로의 sources는 primary 카드 단일 파생으로 무변경이다
    (test_ask_org.py 등 기존 Routed 테스트가 이미 이 계약을 잠근다 — 여기선 co-grounding
    미주입 시 Contested가 여전히 Pending임을 재확인해 "sources 병합 로직이 미주입
    경로에 새어들지 않는다"를 보인다).
    """
    ask, _ = build_contested_ask_org(with_co_grounding=False)
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Pending)  # sources 필드 자체가 없는 타입 — 무변경.


# ── 3. 스트리밍 배타 ─────────────────────────────────────────────────────────


def test_Contested_스트림은_meta_token_done만_방출하고_pending_없음() -> None:
    case_store = InMemoryConflictCaseStore()
    ask, _ = build_contested_ask_org(case_store=case_store, streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("환불 되나요?", user))

    assert isinstance(events[0], MetaEvent)
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, PendingEvent) for e in events)
    assert any(isinstance(e, TokenEvent) for e in events)
    # side-effect로 ConflictCase는 그대로 열린다.
    assert len(case_store.open_for_owner("owner_CS")) == 1


def test_Contested_스트림_meta_answered_by는_primary() -> None:
    ask, _ = build_contested_ask_org(streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("환불 되나요?", user))

    meta = next(e for e in events if isinstance(e, MetaEvent))
    assert meta.answered_by == ("owner_CS", "cs_ops")


# ── 4. 하위호환(회귀 0) ──────────────────────────────────────────────────────


class TestBackwardCompatibilityGateOff:
    """selector/resolver 미주입이면 Contested → 기존 Pending(kind="contested")."""

    def test_미주입이면_handle은_기존_Pending을_반환한다(self) -> None:
        ask, _ = build_contested_ask_org(with_co_grounding=False)
        user = User(id="u1")

        reply = ask.handle("환불 되나요?", user)

        assert isinstance(reply, Pending)
        assert reply.kind == "contested"

    def test_미주입이면_handle_stream은_기존_PendingEvent_단독이다(self) -> None:
        ask, _ = build_contested_ask_org(with_co_grounding=False, streaming=True)
        user = User(id="u1")

        events = list(ask.handle_stream("환불 되나요?", user))

        assert len(events) == 1
        assert isinstance(events[0], PendingEvent)
        assert events[0].kind == "contested"

    def test_selector만_주입되고_resolver_미주입이면_기존_Pending(self) -> None:
        cs = card("cs_ops", "owner_CS", ["환불"])
        sales = card("sales_ops", "owner_Sales", ["환불"])
        registry = Registry()
        registry.register(cs)
        registry.register(sales)
        router = Router(registry, FakeClassifier("환불"), root_user="root")
        ask = AskOrg(
            router=router,
            dispatcher=LocalRuntimeDispatcher(StubRuntime()),
            audit_log=InMemoryAuditLog(),
            clock=fixed_clock,
            grounding_selector=ContestedGroundingSelector(tie_break=first_by_agent_id),
        )
        reply = ask.handle("환불 되나요?", User(id="u1"))

        assert isinstance(reply, Pending)
        assert reply.kind == "contested"


# ── 5. 이중 기록 아님 ────────────────────────────────────────────────────────


def test_Contested_질문후_AnswerRecord_1건_ConflictCase_1건_AuditEntry_1건() -> None:
    case_store = InMemoryConflictCaseStore()
    record_store = InMemoryAnswerRecordStore()
    ask, audit = build_contested_ask_org(case_store=case_store, answer_record_store=record_store)
    user = User(id="u1")

    ask.handle("환불 되나요?", user)

    assert len(record_store.for_agent("cs_ops")) == 1
    assert len(case_store.open_for_owner("owner_CS")) == 1
    assert len(audit.entries) == 1


def test_AuditEntry_decision은_여전히_Contested_원형이다() -> None:
    from agent_org_network.decision import Contested

    ask, audit = build_contested_ask_org()
    user = User(id="u1")

    ask.handle("환불 되나요?", user)

    entry: AuditEntry = audit.entries[-1]
    assert isinstance(entry.decision, Contested)


# ── 6. 소급 무효화 없음 ──────────────────────────────────────────────────────


def test_합의_종결후에도_기존_AnswerRecord는_불변이다() -> None:
    from agent_org_network.conflict import (
        Agreed,
        ConcurOnPrimary,
        ConsensusService,
        InMemoryPrecedentStore,
    )

    case_store = InMemoryConflictCaseStore()
    record_store = InMemoryAnswerRecordStore()
    ask, _ = build_contested_ask_org(case_store=case_store, answer_record_store=record_store)
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)
    assert isinstance(reply, Answered)
    assert reply.record_id is not None
    before = record_store.get(reply.record_id)
    assert before is not None
    assert before.answered_by == "owner_CS"

    open_cases = case_store.open_for_owner("owner_CS")
    case_id = open_cases[0].case_id
    svc = ConsensusService(case_store=case_store, precedents=InMemoryPrecedentStore(clock=fixed_clock))
    svc.concur(case_id, ConcurOnPrimary(by_owner="owner_CS", on_agent="sales_ops"))
    outcome = svc.concur(case_id, ConcurOnPrimary(by_owner="owner_Sales", on_agent="sales_ops"))
    assert isinstance(outcome, Agreed)

    after = record_store.get(reply.record_id)
    assert after is not None
    assert after.answered_by == "owner_CS"
    assert after.answer_text == before.answer_text


# ── 7. 노출 불변식 ───────────────────────────────────────────────────────────


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
}


def test_co_grounded_Answered_project는_내부값을_싣지_않는다() -> None:
    from agent_org_network.ask_org import project_answered

    ask, _ = build_contested_ask_org()
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)
    assert isinstance(reply, Answered)
    body = project_answered(reply)

    assert _LEAKY_KEYS.isdisjoint(set(body.keys()))
    for value in body.values():
        if isinstance(value, dict):
            assert _LEAKY_KEYS.isdisjoint(set(value.keys()))  # pyright: ignore[reportUnknownArgumentType]


def test_병합된_sources에_agent_id나_candidates가_섞이지_않는다() -> None:
    """sources는 출처 레이블(knowledge_sources)만 — agent_id 문자열 자체가 레이블이 아니다."""
    from agent_org_network.ask_org import project_answered

    ask, _ = build_contested_ask_org()
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)
    assert isinstance(reply, Answered)
    body = project_answered(reply)

    sources = body["sources"]
    assert isinstance(sources, list)
    sources_list: list[object] = sources  # pyright: ignore[reportUnknownVariableType]
    assert "cs_ops" not in sources_list
    assert "sales_ops" not in sources_list
    assert sources_list == ["위키/cs_ops", "위키/sales_ops"]


def test_co_grounded_스트림_meta_done_페이로드에_내부값_0() -> None:
    from agent_org_network.ask_org import serialize_sse_event

    ask, _ = build_contested_ask_org(streaming=True)
    user = User(id="u1")

    events = list(ask.handle_stream("환불 되나요?", user))
    for e in events:
        frame = serialize_sse_event(e)
        for leaky in _LEAKY_KEYS:
            assert f'"{leaky}"' not in frame


# ── 8. 미아 없음 보존 ────────────────────────────────────────────────────────


def test_co_grounding_활성이어도_ConflictCase는_여전히_열려_사람_합의로_간다() -> None:
    case_store = InMemoryConflictCaseStore()
    ask, _ = build_contested_ask_org(case_store=case_store)
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    assert isinstance(reply, Answered)  # 답은 나갔지만
    open_cases = case_store.open_for_owner("owner_CS")
    assert len(open_cases) == 1
    assert open_cases[0].status == "open"  # 안전망(사람 합의 경로)은 그대로.


# ── SessionAskOrg 대칭 ───────────────────────────────────────────────────────


def test_SessionAskOrg도_co_grounding_주입시_Answered를_반환한다() -> None:
    from agent_org_network.session import InMemorySessionStore, SessionAskOrg

    ask, _ = build_contested_ask_org()
    store = InMemorySessionStore(clock=fixed_clock)
    session_ask = SessionAskOrg(ask=ask, session_store=store, clock=fixed_clock)
    user = User(id="u1")

    reply = session_ask.handle("환불 되나요?", user)

    assert isinstance(reply, Answered)
    assert reply.answered_by == ("owner_CS", "cs_ops")
