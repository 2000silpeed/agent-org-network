"""ADR 0037 co-grounding 기계장치와 P17 책임 확정 경계 [게이트 내·결정론].

legacy co-grounding의 순수 resolver·디스패처 기계장치는 직접 검증하고, P17 사용자 표면은
책임 확정 전에 이를 실행하지 않는 경계를 잠근다:

  1. 실 `grounding_resolver`(`make_grounding_resolver`)가 중앙 `KnowledgeStore`만 읽어
     GroundingSet의 각 agent_id를 해소한다 — 단일 접지 `resolve_knowledge_text` 재사용.
  2. 프로덕션 `/ask`의 다툼 질문은 책임 확정 전 bodyless Pending + ConflictCase.
  3. owner 격리(ADR 0037 결정 3 B안 기각 실증): resolver는 오직 중앙 스토어만 읽고
     워커 디스크(`okf/`)로 새지 않는다(`okf_root=None` 고정).
  4. WS 오프라인 폴백이 grounding=str를 실제로 소비한다(code-reviewer A+B Minor 1 이월).

전부 결정론 — in-memory `KnowledgeStore`·`StubRuntime`만. 실 LLM/소켓 0.
"""

from datetime import date, datetime, timezone
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_org_network.agent_card import AgentCard
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc
from agent_org_network.provider_runtime import make_grounding_resolver
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app

_NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def _bundle(agent_id: str, body: str) -> KnowledgeBundleContent:
    """단일 문서 in-memory 지식 번들 — 신선(synced_at=_NOW)."""
    return KnowledgeBundleContent(
        agent_id=agent_id,
        documents=(KnowledgeDoc(path=f"{agent_id}/policy.md", body=body),),
        version=f"{agent_id}-v1",
        synced_at=_NOW,
    )


def _store_with_two_domains() -> InMemoryKnowledgeStore:
    """데모의 "보상" 다툼 후보(cs_ops·finance_ops)에 구별 가능한 본문을 주입한 중앙 스토어."""
    store = InMemoryKnowledgeStore()
    store.put(_bundle("cs_ops", "CS_보상본문: 환불 연계 보상은 14일 이내."))
    store.put(_bundle("finance_ops", "FIN_보상본문: 재무 보상 한도는 월 100만원."))
    return store


# ── 1. 프로덕션 앱 end-to-end co-grounding ──────────────────────────────────


def _post(client: TestClient, question: str) -> dict[str, Any]:
    http: Any = client
    return cast(dict[str, Any], http.post("/ask", json={"question": question}).json())


def test_프로덕션_앱_다툼질문은_책임확정전_bodyless_Pending이다() -> None:
    """중앙 지식이 있어도 책임 확정 전에는 본문 없는 Contested만 반환한다(P17.5)."""
    stub = StubRuntime()
    app: FastAPI = create_app(runtime=stub, knowledge_store=_store_with_two_domains())
    client = TestClient(app)

    body = _post(client, "보상 기준이 어떻게 되나요?")

    assert body["type"] == "pending"
    assert body["kind"] == "contested"
    assert body["state"] == "awaiting_conflict"
    assert body["request_id"]
    assert body["tracking"] == body["request_id"]
    assert "text" not in body
    assert "record_id" not in body


def test_프로덕션_다툼은_책임확정전에_Runtime과_co_grounding을_실행하지_않는다() -> None:
    """P17.5 전에는 ConflictCase만 열고 후보 본문을 Runtime에 넘기지 않는다."""
    stub = StubRuntime()
    app: FastAPI = create_app(runtime=stub, knowledge_store=_store_with_two_domains())
    client = TestClient(app)

    _post(client, "보상 기준이 어떻게 되나요?")

    assert stub.last_grounding is None


def test_프로덕션_bodyless_Pending에서도_ConflictCase는_열린다() -> None:
    """사용자 본문은 막되 사람 합의를 위한 ConflictCase 운영 기능은 그대로 연다."""
    stub = StubRuntime()
    app: FastAPI = create_app(runtime=stub, knowledge_store=_store_with_two_domains())
    client = TestClient(app)

    _post(client, "보상 기준이 어떻게 되나요?")

    # 인증 OFF 레거시 path 라우트로 cs_lead(cs_ops owner) 처리함 조회.
    http: Any = client
    cases: list[dict[str, Any]] = cast(list[dict[str, Any]], http.get("/inbox/cs_lead").json())
    assert len(cases) == 1
    assert cases[0]["intent"] == "보상"
    assert {c["agent_id"] for c in cases[0]["candidates"]} == {"cs_ops", "finance_ops"}


def test_프로덕션_다툼_Pending은_후보_sources를_노출하지_않는다() -> None:
    """책임 확정 전에는 primary/supporting 출처도 사용자 표면에 내보내지 않는다."""
    stub = StubRuntime()
    app: FastAPI = create_app(runtime=stub, knowledge_store=_store_with_two_domains())
    client = TestClient(app)

    body = _post(client, "보상 기준이 어떻게 되나요?")

    assert body["type"] == "pending"
    assert body["kind"] == "contested"
    assert "sources" not in body
    assert "answered_by" not in body


# ── 2. Routed/단일 경로 무영향(회귀 0) ──────────────────────────────────────


def test_단일_담당_질문은_co_grounding에_영향받지_않는다() -> None:
    """Routed(담당 1명·계약) 질문은 여전히 단일 Answered — grounding 미주입(자기 해소)."""
    stub = StubRuntime()
    app: FastAPI = create_app(runtime=stub, knowledge_store=_store_with_two_domains())
    client = TestClient(app)

    body = _post(client, "이 계약 조건 바꿔도 돼?")

    assert body["type"] == "answered"
    assert body["answered_by"]["agent_id"] == "contract_ops"
    # 단일 경로는 co-ground 조립을 타지 않는다 — dispatch가 grounding=None으로 흘러
    # 런타임이 자기 접지(_resolve_okf)를 쓴다(다중 접지 문자열 미주입).
    assert stub.last_grounding is None


# ── 3. owner 격리 — resolver는 중앙 스토어만 읽는다(ADR 0037 결정 3 B안 기각 실증) ──


def test_resolver는_중앙_스토어_본문을_해소한다() -> None:
    store = InMemoryKnowledgeStore()
    store.put(_bundle("cs_ops", "중앙_스토어_전용_본문"))
    resolve = make_grounding_resolver(store)

    text = resolve("cs_ops")

    assert "중앙_스토어_전용_본문" in text


def test_resolver는_스토어_미보유_agent를_빈문자로_낸다_디스크_폴백_없음() -> None:
    """finance_ops는 `okf/finance_ops/`에 실 디스크 번들이 있지만, 스토어에 없으면 ""이다.

    `make_grounding_resolver`가 `okf_root=None`을 고정해 디스크 폴백(`read_okf_bundle`)을
    원천 차단하는 owner 격리의 실증 — resolver가 워커 로컬 디스크로 새지 않는다(크로스머신
    격리·ADR 0033·ADR 0037 결정 3 B안 기각).
    """
    store = InMemoryKnowledgeStore()
    store.put(_bundle("cs_ops", "cs 본문만 스토어에 있다"))
    resolve = make_grounding_resolver(store)

    # finance_ops는 스토어에 없다 — 디스크(okf/finance_ops/pricing.md)가 있어도 폴백 0.
    assert resolve("finance_ops") == ""


def test_프로덕션_다툼은_스토어_일부미보유여도_bodyless_Pending이다() -> None:
    """후보 지식 일부가 없어도 책임 확정 전에는 디스크 폴백이나 답 생성을 하지 않는다."""
    store = InMemoryKnowledgeStore()
    store.put(_bundle("cs_ops", "CS_보상본문만_존재"))
    stub = StubRuntime()
    app: FastAPI = create_app(runtime=stub, knowledge_store=store)
    client = TestClient(app)

    body = _post(client, "보상 기준이 어떻게 되나요?")

    assert body["type"] == "pending"
    assert body["kind"] == "contested"
    assert stub.last_grounding is None
    assert "text" not in body


# ── 4. 하위호환 — knowledge_store 미주입 build_demo는 co-grounding OFF ──────────


def test_build_demo_knowledge_store_미주입이면_다툼은_기존_Pending이다() -> None:
    """옵트인 스위치: `build_demo`에 knowledge_store를 안 넘기면 co-grounding OFF(회귀 0)."""
    from agent_org_network.ask_org import Pending
    from agent_org_network.demo import build_demo
    from agent_org_network.user import User

    ask = build_demo(runtime=StubRuntime()).ask  # knowledge_store 미주입.

    reply = ask.handle("보상 기준이 어떻게 되나요?", User(id="u1"))

    assert isinstance(reply, Pending)
    assert reply.kind == "contested"


# ── 5. WS 오프라인 폴백이 grounding=str를 소비한다(code-reviewer A+B Minor 1 이월) ──


def _card(agent_id: str, owner: str) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary=f"{agent_id} 요약",
        domains=["보상"],
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=[f"위키/{agent_id}"],
    )


def test_WS_오프라인_폴백은_grounding_문자열을_런타임까지_전달한다() -> None:
    """담당 워커 미연결 → 중앙 폴백 런타임이 답하되, dispatch가 받은 grounding=str를 소비한다.

    A+B에서 `WebSocketDispatcher.dispatch(grounding=)`가 오프라인 폴백원(`fallback_runtime`)
    으로 grounding을 전달하도록 배선했는데, 그 소비를 잠그는 테스트가 없던 이월(Minor 1).
    워커가 안 붙은 상태(연결 0)라 폴백이 발동하고, StubRuntime의 `last_grounding` 관측
    seam으로 접지 문자열이 실제로 런타임까지 닿았음을 단언한다.
    """
    from agent_org_network.transport import WebSocketDispatcher

    fallback = StubRuntime()
    dispatcher = WebSocketDispatcher(fallback_runtime=fallback)
    card = _card("cs_ops", "cs_lead")
    grounding_text = "### cs_ops\nCS본문\n\n### finance_ops\nFIN본문"

    ticket = dispatcher.dispatch("보상 기준?", card, grounding=grounding_text)
    outcome = dispatcher.poll(ticket)

    # 폴백이 답을 냈고(즉시 Delivered), 그 답 생성에 grounding이 소비됐다.
    from agent_org_network.dispatch import Delivered

    assert isinstance(outcome, Delivered)
    assert fallback.last_grounding == grounding_text


def test_WS_폴백_grounding_미전달이면_None_소비이다() -> None:
    """대칭 잠금 — grounding 인자를 안 주면 폴백 런타임도 grounding=None으로 자기 접지."""
    from agent_org_network.transport import WebSocketDispatcher

    fallback = StubRuntime()
    dispatcher = WebSocketDispatcher(fallback_runtime=fallback)
    card = _card("cs_ops", "cs_lead")

    dispatcher.dispatch("보상 기준?", card)

    assert fallback.last_grounding is None
