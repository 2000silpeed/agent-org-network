"""Phase 12 (B) 라이브 지식 동기화 배선 통합 테스트 — 실 WS loopback(TestClient).

배선이 실제로 돈다는 증거: 워커가 실 소켓으로 보낸 `SyncKnowledge`가 중앙
`_handle_worker.recv_loop`→`dispatcher.accept_knowledge_sync_frame`→
`accept_and_store_knowledge_sync`(M3 계약 — store.put 직접 호출 금지·판정과 보관 분리)로
중앙 `KnowledgeStore`에 반영되고, `KnowledgeSyncAck`가 워커에 회신된다.

여기서 잠그는 것(실 WS in-process TestClient·실 claude 0):
  1. 수용 왕복: register→SyncKnowledge(clean)→ack accepted → 그 store에 실제로 put.
  2. 거부 왕복: 민감정보(주민번호) 본문 → ack rejected(reason 담김)·store 미오염.
  3. 사칭 스코핑: 다른 owner가 남의 agent_id를 sync → 거부(Authority 중앙·워커-소유자 스코핑).
  4. M3 계약: 수신부가 `store.put`을 직접 호출하지 않고 조합 함수 경유(admission 우회 차단) —
     거부 케이스에서 store가 비어 있음으로 간접 확인(admission이 put을 막았다).

불변식: 명시 지정만 동기화·민감 필터·Authority 중앙(스코핑)·등록 무결성(미등록 agent_id 거부)·
전이≠기록(판정과 보관 분리). 결정론 코어(admit_knowledge·filter_sensitive·accept_and_store_
knowledge_sync)는 무변경 — 실 소켓 배선만 검증.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from agent_org_network.demo import build_demo
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc, SyncKnowledge
from agent_org_network.presence import InMemoryPresenceTracker
from agent_org_network.runtime import StubRuntime
from agent_org_network.server import create_worker_app
from agent_org_network.transport import WebSocketDispatcher

_SYNCED_AT = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


def _dispatcher_with_store() -> tuple[WebSocketDispatcher, InMemoryKnowledgeStore]:
    """실 registry(데모 카드)·knowledge_store를 물린 디스패처를 만든다(loopback용)."""
    bundle = build_demo(runtime=StubRuntime())
    store = InMemoryKnowledgeStore()
    dispatcher = WebSocketDispatcher(
        registry=bundle.registry,
        knowledge_store=store,
        presence_tracker=InMemoryPresenceTracker(),
    )
    return dispatcher, store


def _recv(conn: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], conn.receive_json())


def _sync_frame(agent_id: str, docs: tuple[KnowledgeDoc, ...]) -> dict[str, Any]:
    frame = SyncKnowledge(
        content=KnowledgeBundleContent(
            agent_id=agent_id, documents=docs, version="v1", synced_at=_SYNCED_AT
        )
    )
    return frame.model_dump(mode="json")


def test_지식_동기화_수용_왕복이_store에_반영된다() -> None:
    """register→SyncKnowledge(clean)→ack accepted → 중앙 store에 실제로 put(실 WS 한 바퀴)."""
    dispatcher, store = _dispatcher_with_store()
    client = TestClient(create_worker_app(dispatcher))
    http: Any = client

    with http.websocket_connect("/worker") as conn:
        ws = cast(WebSocketTestSession, conn)
        # cs_lead 워커 등록(cs_ops 카드 소유자).
        ws.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        assert _recv(ws)["type"] == "welcome"

        ws.send_json(
            _sync_frame(
                "cs_ops",
                (KnowledgeDoc(path="cs_ops/refund.md", body="환불은 구매 후 7일 이내 가능."),),
            )
        )
        ack = _recv(ws)
        assert ack["type"] == "knowledge_sync_ack"
        assert ack["accepted"] is True
        assert ack["agent_id"] == "cs_ops"

    # 중앙 store에 본문이 실제로 반영됐다(M3 계약 경유 put).
    content = store.get("cs_ops")
    assert content is not None
    assert content.documents[0].path == "cs_ops/refund.md"
    assert "7일 이내" in content.documents[0].body


def test_민감정보_본문은_거부되고_store가_오염되지_않는다() -> None:
    """주민번호 패턴 본문 → ack rejected(reason)·store 미반영(admission이 put을 막음, M3 계약)."""
    dispatcher, store = _dispatcher_with_store()
    client = TestClient(create_worker_app(dispatcher))
    http: Any = client

    with http.websocket_connect("/worker") as conn:
        ws = cast(WebSocketTestSession, conn)
        ws.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        assert _recv(ws)["type"] == "welcome"

        ws.send_json(
            _sync_frame(
                "cs_ops",
                (KnowledgeDoc(path="cs_ops/pii.md", body="고객 주민번호 900101-1234567 참고."),),
            )
        )
        ack = _recv(ws)
        assert ack["accepted"] is False
        assert ack["reason"]  # 거부 사유가 담긴다(재시도/수정 판단 가능)

    # store 미오염 — admission이 put을 막았다(전이≠기록·수용 관문 단일화).
    assert store.get("cs_ops") is None


def test_타_owner_사칭_동기화는_스코핑_거부된다() -> None:
    """다른 owner가 cs_ops를 sync → 워커-소유자 스코핑 거부(Authority 중앙)·store 미오염."""
    dispatcher, store = _dispatcher_with_store()
    client = TestClient(create_worker_app(dispatcher))
    http: Any = client

    with http.websocket_connect("/worker") as conn:
        ws = cast(WebSocketTestSession, conn)
        # mallory가 cs_ops(cs_lead 소유)를 sync 시도 — 사칭.
        ws.send_json({"type": "register_worker", "owner_id": "mallory", "role": "primary"})
        assert _recv(ws)["type"] == "welcome"

        ws.send_json(
            _sync_frame("cs_ops", (KnowledgeDoc(path="cs_ops/x.md", body="위조 본문"),))
        )
        ack = _recv(ws)
        assert ack["accepted"] is False
        assert "스코핑" in ack["reason"]

    assert store.get("cs_ops") is None


def test_미등록_agent_id_동기화는_거부된다() -> None:
    """미등록 agent_id는 admission이 card 없이 판정 불가 → 거부(등록 무결성)."""
    dispatcher, store = _dispatcher_with_store()
    client = TestClient(create_worker_app(dispatcher))
    http: Any = client

    with http.websocket_connect("/worker") as conn:
        ws = cast(WebSocketTestSession, conn)
        ws.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        assert _recv(ws)["type"] == "welcome"

        ws.send_json(
            _sync_frame("ghost_ops", (KnowledgeDoc(path="ghost_ops/x.md", body="본문"),))
        )
        ack = _recv(ws)
        assert ack["accepted"] is False
        assert "미등록" in ack["reason"]

    assert store.get("ghost_ops") is None


def test_통합_조립_기본_라우터_모드에서도_동기화가_수용된다() -> None:
    """`create_app` 통합 조립이 registry를 항상 bind한다 — index 모드 아니어도 수용.

    회귀 고정(2026-07-05 크로스머신 시연이 잡은 실결함): registry 바인딩이
    `bind_published_index`(index 모드 전용) 안에만 있어, 기본 라우터 모드의 실 조립은
    registry 미주입 → `accept_knowledge_sync_frame`이 침묵 no-op(ack 없음)이었다.
    `bind_registry`가 라우터 모드와 무관하게 항상 불리는지를 실 조립 경로로 잠근다.
    """
    from agent_org_network.web import create_app

    store = InMemoryKnowledgeStore()
    # 생성자에 registry를 *일부러 안 준다* — 실 조립(create_central_app→create_app)과
    # 같은 시점 문제(디스패처가 build_demo보다 먼저 생성됨)를 재현.
    dispatcher = WebSocketDispatcher(
        knowledge_store=store, presence_tracker=InMemoryPresenceTracker()
    )
    create_app(runtime=StubRuntime(), dispatcher=dispatcher)  # 여기서 bind_registry
    client = TestClient(create_worker_app(dispatcher))
    http: Any = client

    with http.websocket_connect("/worker") as conn:
        ws = cast(WebSocketTestSession, conn)
        ws.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        assert _recv(ws)["type"] == "welcome"
        ws.send_json(
            _sync_frame(
                "cs_ops",
                (KnowledgeDoc(path="cs_ops/refund.md", body="환불은 구매 후 7일 이내 가능."),),
            )
        )
        ack = _recv(ws)
        assert ack["type"] == "knowledge_sync_ack"
        assert ack["accepted"] is True

    assert store.get("cs_ops") is not None


def test_지식_동기화_미배선_디스패처는_회신하지_않는다() -> None:
    """store/registry 미주입 디스패처는 accept_knowledge_sync_frame이 None → 회신 없음(하위호환).

    미배선이면 SyncKnowledge를 받아도 no-op(ack 미회신) — 이후 heartbeat로 소켓이 살아 있음만
    확인(수신부가 프레임에 안 깨지고 조용히 흡수).
    """
    bundle = build_demo(runtime=StubRuntime())
    # knowledge_store 미주입(하위호환 경로).
    dispatcher = WebSocketDispatcher(registry=bundle.registry)
    client = TestClient(create_worker_app(dispatcher))
    http: Any = client

    with http.websocket_connect("/worker") as conn:
        ws = cast(WebSocketTestSession, conn)
        ws.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        assert _recv(ws)["type"] == "welcome"
        ws.send_json(
            _sync_frame("cs_ops", (KnowledgeDoc(path="cs_ops/x.md", body="본문"),))
        )
        # ack 미회신 — ping으로 소켓 생존만 확인(수신부가 안 깨졌다).
        ws.send_json({"type": "ping"})
        # ping에는 워커가 응답하지 않지만(중앙→워커 방향) 소켓은 살아 있어야 한다.
        # 여기선 예외 없이 with 블록을 빠져나가면 성공(수신부 흡수 확인).
