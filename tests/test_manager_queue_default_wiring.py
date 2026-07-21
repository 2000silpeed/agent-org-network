"""P17.1 — 기본 앱 조립의 Manager 큐 공유 배선 회귀 테스트.

실 사용자 경로(`/ask/stream`, `/ask`)에서 생긴 Unowned가 운영 경로
(`root_manager` 로그인 → `GET /manager/queue`)에 그대로 보여야 한다.
별도 store를 주입하지 않는 `web.create_app`과 `server.create_central_app`의
기본 조립을 검증한다. 실 LLM·네트워크 호출은 없다.
"""

from __future__ import annotations

import json
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.manager_queue import InMemoryManagerQueueStore
from agent_org_network.runtime import StubRuntime
from agent_org_network.server import create_central_app
from agent_org_network.web import create_app

_SECRET = "p17-manager-queue-secret"


def _post(client: TestClient, path: str, payload: dict[str, Any]) -> Response:
    http: Any = client
    return cast(Response, http.post(path, json=payload))


def _get(client: TestClient, path: str) -> Response:
    http: Any = client
    return cast(Response, http.get(path))


def _login_as_root_manager(client: TestClient) -> None:
    response = _post(client, "/login", {"user_id": "root_manager"})
    assert response.status_code == 200


def _pending_sse_payloads(response: Response) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for block in response.text.split("\n\n"):
        lines = block.splitlines()
        if "event: pending" not in lines:
            continue
        data_line = next(line for line in lines if line.startswith("data: "))
        payload = json.loads(data_line.removeprefix("data: "))
        assert isinstance(payload, dict)
        payloads.append(cast(dict[str, Any], payload))
    return payloads


def _assert_unowned_in_manager_queue(client: TestClient, question: str) -> None:
    _login_as_root_manager(client)
    response = _get(client, "/manager/queue")

    assert response.status_code == 200
    items = cast(list[dict[str, Any]], response.json())
    matching = [item for item in items if item["source"].get("question") == question]
    assert len(matching) == 1
    assert matching[0]["manager_id"] == "root_manager"
    assert matching[0]["source"]["type"] == "from_unowned"


def test_create_app_기본조립_stream_Unowned가_root_manager_큐에_보인다(
    monkeypatch: Any,
) -> None:
    """`web:app`과 같은 create_app 기본 조립에서 stream 부수효과가 공유된다."""
    monkeypatch.delenv("AON_CLASSIFIER", raising=False)
    monkeypatch.delenv("AON_ROUTER", raising=False)
    app: FastAPI = create_app(
        runtime=StubRuntime(),
        session_secret=_SECRET,
        audit_log=InMemoryAuditLog(),
    )
    client = TestClient(app)
    question = "주차장 정기권은 어떻게 갱신하나요?"

    response = _post(client, "/ask/stream", {"question": question})

    assert response.status_code == 200
    pending = _pending_sse_payloads(response)
    assert len(pending) == 1
    assert pending[0]["kind"] == "unowned"
    _assert_unowned_in_manager_queue(client, question)


def test_create_central_app_기본조립_Ask_Unowned가_root_manager_큐에_보인다(
    monkeypatch: Any,
) -> None:
    """통합 중앙 앱도 별도 주입 없이 AskOrg와 Manager 라우트가 한 큐를 본다."""
    for name in ("AON_CLASSIFIER", "AON_ROUTER", "AON_PROVIDER", "AON_DB"):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(create_central_app(session_secret=_SECRET))
    question = "주차장 방문 차량 등록은 어디서 하나요?"

    response = _post(client, "/ask", {"question": question})

    assert response.status_code == 200
    assert response.json()["kind"] == "unowned"
    _assert_unowned_in_manager_queue(client, question)


def test_create_central_app은_주입된_Manager_큐를_우선한다(monkeypatch: Any) -> None:
    """명시 주입은 기본 InMemory 생성보다 우선하며 AskOrg가 그 인스턴스에 적재한다."""
    for name in ("AON_CLASSIFIER", "AON_ROUTER", "AON_PROVIDER", "AON_DB"):
        monkeypatch.delenv(name, raising=False)
    queue_store = InMemoryManagerQueueStore()
    client = TestClient(
        create_central_app(
            session_secret=_SECRET,
            manager_queue_store=queue_store,
        )
    )
    question = "사내 주차 규정은 누가 담당하나요?"

    response = _post(client, "/ask", {"question": question})

    assert response.status_code == 200
    assert response.json()["kind"] == "unowned"
    pending = queue_store.pending_for_manager("root_manager")
    assert len(pending) == 1
    assert pending[0].question() == question
