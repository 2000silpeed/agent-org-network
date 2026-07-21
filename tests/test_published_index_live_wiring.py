"""T10.4 라이브 publish 배선 통합 테스트 — code-review Blocker B1 회귀 차단.

결함(B1): `create_central_app`이 `WebSocketDispatcher`를 store/registry 미주입으로 만들어
`accept_index`가 무조건 no-op → 워커 PublishIndex가 조용히 버려짐. 또 `demo.select_router`가
시드 store를 라우터 내부에 가둬 디스패처 store와 연결 불가. 수정은 *배선만* — store를 라우터·
디스패처 양쪽에 *같은 인스턴스*로 꽂는다(ADR 0028 §14 결정 F).

여기서 잠그는 것(게이트 내·실 WS 없음 결정론):
  1. 배선 확인: build_demo(index 모드) bundle의 store·registry로 디스패처를 bind →
     accept_index가 *실제로 그 store를 채운다*(no-op 아님). 미바인딩(B1)이면 no-op임을 대비 단언.
  2. 시드+publish 공존: 시드 store에 더 새 generated_at publish → put이 시드 교체 → 그 store로
     라우팅하면 새 인덱스 반영(시드는 워커 미연결 fallback·publish가 덮음).
  3. accept→route end-to-end: PublishIndex 수용(스코핑·필터 통과) → 매칭 질문 route → Routed.
  4. 라이브 WS(create_central_app TestClient): index 모드에서 워커 register→publish_index→
     /ask 질문이 그 publish된 인덱스 경로로 답해진다(라이브 배선이 실제로 돈다).

불변식: 중앙 토큰 0·비소유(워커가 인덱스 도출·중앙은 보관만)·Authority 중앙(스코핑·필터
유지)·미아 없음(publish 거부가 라우팅 종착과 무관). 결정론 코어(accept_published_index·
publishable·filter·put)는 무변경 — 배선만 검증.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from agent_org_network.agent_card import AgentCard
from agent_org_network.decision import Routed
from agent_org_network.demo import (
    DEMO_OKF_ROOT,
    build_demo,
    seed_published_index_store,
)
from agent_org_network.knowledge_index import Concept, KnowledgeIndex
from agent_org_network.okf_index import build_knowledge_index_from_okf
from agent_org_network.runtime import StubRuntime
from agent_org_network.transport import PublishIndex, WebSocketDispatcher
from agent_org_network.two_stage_router import TwoStageRouter

# 워커 publish 시각(시드 _INDEX_SEED_AT=2026-06-28 00:00보다 *더 새* — put staleness 교체).
_PUBLISH_AT = datetime(2026, 6, 29, 0, 0, 0, tzinfo=timezone.utc)


def _index_bundle() -> Any:
    """AON_ROUTER=index로 조립한 DemoBundle을 만든다(StubRuntime·결정론)."""
    import os

    prev = os.environ.get("AON_ROUTER")
    os.environ["AON_ROUTER"] = "index"
    try:
        return build_demo(runtime=StubRuntime())
    finally:
        if prev is None:
            os.environ.pop("AON_ROUTER", None)
        else:
            os.environ["AON_ROUTER"] = prev


# ── 1. 배선 확인 — bind 후 accept_index가 실제로 store를 채운다 ────────────────


def test_bind된_디스패처의_accept_index가_store를_채운다() -> None:
    """build_demo(index) bundle의 store·registry로 bind → accept_index가 그 store에 put.

    B1 회귀 핵심: 미바인딩이면 accept_index가 무조건 no-op(False). bind 후에는 실제 put.
    """
    bundle = _index_bundle()
    store = bundle.published_index_store
    assert store is not None  # index 모드는 store를 노출

    dispatcher = WebSocketDispatcher()
    # 미바인딩 — accept_index는 no-op(B1 상태 재현).
    cs_card = bundle.registry.get("cs_ops")
    frame = _publish_frame_for(cs_card)
    assert dispatcher.accept_index("cs_lead", frame) is False
    # 미바인딩 디스패처는 그 자체 store가 없으므로 bundle store는 그대로(publish 미반영).

    # 이제 *같은* store·registry를 bind — accept_index가 실제로 store를 채운다.
    dispatcher.bind_published_index(bundle.registry, store)
    assert dispatcher.accept_index("cs_lead", frame) is True
    stored = store.get("cs_ops")
    assert stored is not None
    assert stored.generated_at == _PUBLISH_AT  # publish된 인덱스가 store에 도달


def test_생성자_주입과_bind가_동등하다() -> None:
    """생성자 published_index_store=·bind_published_index 둘 다 같은 수용 결과."""
    bundle = _index_bundle()
    store_a = bundle.published_index_store
    assert store_a is not None
    cs_card = bundle.registry.get("cs_ops")
    frame = _publish_frame_for(cs_card)

    via_ctor = WebSocketDispatcher(registry=bundle.registry, published_index_store=store_a)
    assert via_ctor.accept_index("cs_lead", frame) is True

    store_b = seed_published_index_store(bundle.registry)
    via_bind = WebSocketDispatcher()
    via_bind.bind_published_index(bundle.registry, store_b)
    assert via_bind.accept_index("cs_lead", frame) is True


# ── 2. 시드+publish 공존 — 더 새 publish가 시드를 교체 ────────────────────────


def test_워커_publish가_시드를_교체한다_put_staleness() -> None:
    """시드 store(generated_at=_INDEX_SEED_AT)에 더 새 publish → put이 시드를 교체.

    시드는 워커 미연결 fallback·워커 publish는 datetime.now(utc)(더 새)라 시드를 덮는다
    (의도대로 — ADR 0028 §14 결정 C/E). 같은 store 인스턴스에서 시드와 publish가 만난다.
    """
    bundle = _index_bundle()
    store = bundle.published_index_store
    assert store is not None

    # 시드가 먼저 들어 있다(중앙 OKF 직접 읽기·_INDEX_SEED_AT).
    seeded = store.get("cs_ops")
    assert seeded is not None
    seed_at = seeded.generated_at
    assert seed_at < _PUBLISH_AT  # 시드는 publish보다 옛 것

    # 워커가 더 새 인덱스를 publish — 단일 distinguishable concept을 실어 교체를 식별.
    dispatcher = WebSocketDispatcher(registry=bundle.registry, published_index_store=store)
    marker = KnowledgeIndex(
        agent_id="cs_ops",
        version="okf-published",
        generated_at=_PUBLISH_AT,
        concepts=(
            Concept(id="환불-published", label="환불", core_question="환불 규정", domain="환불"),
        ),
    )
    assert dispatcher.accept_index("cs_lead", PublishIndex(index=marker)) is True

    replaced = store.get("cs_ops")
    assert replaced is not None
    assert replaced.generated_at == _PUBLISH_AT  # 시드 교체됨
    assert any(c.id == "환불-published" for c in replaced.concepts)


def test_역행_publish는_시드를_안_덮는다() -> None:
    """publish의 generated_at이 시드보다 옛 것이면 put no-op(시드 보존·staleness 역행 거부)."""
    bundle = _index_bundle()
    store = bundle.published_index_store
    assert store is not None
    seeded = store.get("cs_ops")
    assert seeded is not None

    dispatcher = WebSocketDispatcher(registry=bundle.registry, published_index_store=store)
    stale = KnowledgeIndex(
        agent_id="cs_ops",
        version="okf-stale",
        generated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),  # 시드보다 옛
        concepts=(Concept(id="stale", label="x", core_question="x", domain="환불"),),
    )
    # 스코핑은 통과(True)하나 put은 역행이라 no-op — 시드 보존.
    assert dispatcher.accept_index("cs_lead", PublishIndex(index=stale)) is True
    kept = store.get("cs_ops")
    assert kept is not None
    assert kept.generated_at == seeded.generated_at  # 시드 그대로


# ── 3. accept→route end-to-end — publish가 라우팅에 반영 ──────────────────────


def test_publish_수용_후_그_store로_라우팅한다() -> None:
    """publish 수용된 인덱스가 *라우터가 보는 store*에 들어가 매칭 질문을 Routed로 낸다.

    라우터(TwoStageRouter)와 디스패처가 *같은 store 인스턴스*를 봐야 성립 — m2가 빠뜨린
    accept→route end-to-end 커버.
    """
    bundle = _index_bundle()
    store = bundle.published_index_store
    assert store is not None
    router = bundle.ask._router  # pyright: ignore[reportPrivateUsage]
    assert isinstance(router, TwoStageRouter)

    dispatcher = WebSocketDispatcher(registry=bundle.registry, published_index_store=store)
    # 워커가 "환불" concept을 더 새 generated_at으로 publish(시드 교체).
    published = KnowledgeIndex(
        agent_id="cs_ops",
        version="okf-published",
        generated_at=_PUBLISH_AT,
        concepts=(
            Concept(id="refund", label="환불 규정", core_question="환불 규정 안내", domain="환불"),
        ),
    )
    assert dispatcher.accept_index("cs_lead", PublishIndex(index=published)) is True

    # 라우터가 *같은 store*를 보므로 그 publish된 인덱스로 라우팅한다.
    decision = router.route("환불 규정 알려줘")
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "cs_ops"
    assert decision.intent == "환불"


def test_타_owner_사칭_publish는_라우팅에_안_샌다() -> None:
    """다른 owner가 cs_ops 인덱스를 publish하려 하면 스코핑 거부 — store 미오염·라우팅 무영향.

    Authority 중앙(스코핑) 보존: card.owner != session_owner면 거부. 거부는 라우팅 종착과
    무관(미아 없음) — 라우터는 시드 인덱스로 정상 라우팅한다.
    """
    bundle = _index_bundle()
    store = bundle.published_index_store
    assert store is not None
    seeded = store.get("cs_ops")
    assert seeded is not None

    dispatcher = WebSocketDispatcher(registry=bundle.registry, published_index_store=store)
    forged = KnowledgeIndex(
        agent_id="cs_ops",
        version="forged",
        generated_at=_PUBLISH_AT,
        concepts=(Concept(id="forged", label="x", core_question="x", domain="환불"),),
    )
    # mallory(cs_ops 비소유)가 사칭 — 거부.
    assert dispatcher.accept_index("mallory", PublishIndex(index=forged)) is False
    # store는 시드 그대로(사칭 미반영).
    kept = store.get("cs_ops")
    assert kept is not None
    assert kept.generated_at == seeded.generated_at
    assert not any(c.id == "forged" for c in kept.concepts)


# ── 4. 라이브 WS — create_central_app TestClient 경로 ──────────────────────────


def _recv(conn: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], conn.receive_json())


def test_create_central_app_index모드_워커_publish가_라우팅에_도달한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """라이브 WS 한 바퀴: index 모드에서 워커 publish_index가 P17 Router에 반영된다.

    배선이 실제로 돈다는 증거 — 워커가 보낸 PublishIndex가 recv_loop→accept_index→put으로
    라우터 store에 도달하고, /ask 질문이 그 인덱스 경로로 라우팅돼 답이 나온다(B1 회귀
    차단·실 WS in-process TestClient·실 claude 0).
    """
    from agent_org_network.server import create_central_app

    monkeypatch.setenv("AON_ROUTER", "index")

    def select_stub_runtime(*args: object, **kwargs: object) -> StubRuntime:
        del args, kwargs
        return StubRuntime()

    monkeypatch.setattr(
        "agent_org_network.runtime_select.select_runtime",
        select_stub_runtime,
    )
    client = TestClient(create_central_app())
    http: Any = client

    with http.websocket_connect("/worker") as conn:
        ws = cast(WebSocketTestSession, conn)
        # cs_lead 워커가 primary로 등록.
        ws.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        assert _recv(ws)["type"] == "welcome"

        # 워커가 자기 인덱스를 publish(더 새 generated_at으로 시드 교체).
        published = KnowledgeIndex(
            agent_id="cs_ops",
            version="okf-published",
            generated_at=_PUBLISH_AT,
            concepts=(
                Concept(
                    id="refund", label="환불 규정", core_question="환불 규정 안내", domain="환불"
                ),
            ),
        )
        ws.send_json(PublishIndex(index=published).model_dump(mode="json"))
        # heartbeat로 송신/수신 루프 왕복 보장(publish 처리 완료 펜스).
        ws.send_json({"type": "heartbeat"})

    # publish를 마친 워커가 연결된 동안에는 Owner 사전 승인이 필요하다. 연결을 닫아
    # offline 자동발신 조건으로 바꾼 뒤 질문해, 이 테스트의 본래 목적인 published index
    # → P17 Router → cs_ops 책임 경로를 검증한다.
    r = http.post("/ask", json={"question": "환불 규정 알려줘"})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "answered"
    assert body["answered_by"] == {"owner": "cs_lead", "agent_id": "cs_ops"}
    request_id = body["request_id"]
    record_id = body["record_id"]

    # 새 canonical 조회도 같은 Request와 Finalization 결과를 돌려준다.
    ans = http.get(f"/requests/{request_id}").json()
    assert ans["request_id"] == request_id
    assert ans["record_id"] == record_id
    assert ans["answered_by"] == "cs_lead"
    assert ans["agent_id"] == "cs_ops"


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────


def _publish_frame_for(card: AgentCard) -> PublishIndex:
    """데모 카드의 OKF에서 인덱스를 도출해 _PUBLISH_AT 시각의 PublishIndex로 만든다.

    워커측 도출(build_knowledge_index_from_okf)을 흉내 — 더 새 generated_at으로 시드 교체.
    """
    idx = build_knowledge_index_from_okf(card, DEMO_OKF_ROOT, generated_at=_PUBLISH_AT)
    return PublishIndex(index=idx)
