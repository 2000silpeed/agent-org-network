"""T9.2(b)·T9.5(c) — `create_central_app(token_store=...)` 콘솔 발급→워커 register 관통.

콘솔 `/console/tokens`가 발급한 *같은* `TokenStore` 인스턴스가 `WebSocketDispatcher`에도
물려 있어야, 콘솔이 발급한 토큰으로 워커가 실제 register(WS `RegisterWorker.token`)될 수
있다(ADR 0026 결정 2·T9.5(b) `_authenticate` 실 교체 위의 단일 원천 배선).

`create_central_app()`(무인자)는 기존처럼 `token_store=None`이라 인증 미검증(하위호환 —
test_central_app_backup_review.py가 이미 그 회귀를 지킨다). 여기선 `token_store` **명시
주입** 시의 새 동작만 검증한다.

결정론: FastAPI TestClient WS(실 네트워크 0). InMemoryTokenStore(결정론 clock 불요 — 만료
미지정 토큰은 영구 유효).
"""

from __future__ import annotations

from typing import Any, cast

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from agent_org_network.server import create_central_app
from agent_org_network.token import InMemoryTokenStore


def _ws(client: TestClient) -> Any:
    http: Any = client
    return http.websocket_connect("/worker")


def _recv(conn: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], conn.receive_json())


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> Any:
    http: Any = client
    return http.post(url, json=payload)


def test_token_store_주입시_콘솔_발급_토큰으로_register_통과() -> None:
    token_store = InMemoryTokenStore(token_factory=lambda: "console-issued-raw-token")
    app = create_central_app(token_store=token_store)
    client = TestClient(app)

    # 콘솔 발급(HTTP) — 실 라우트를 거쳐 같은 token_store에 적재.
    issued = _post(
        client, "/console/tokens", {"owner_id": "cs_lead", "role": "primary"}
    ).json()
    raw_token = issued["token"]

    with _ws(client) as conn:
        conn.send_json(
            {
                "type": "register_worker",
                "owner_id": "cs_lead",
                "role": "primary",
                "token": raw_token,
            }
        )
        reply = _recv(conn)
        assert reply["type"] == "welcome"


def test_token_store_주입시_미지_토큰은_거부된다() -> None:
    token_store = InMemoryTokenStore()
    app = create_central_app(token_store=token_store)
    client = TestClient(app)

    with _ws(client) as conn:
        conn.send_json(
            {
                "type": "register_worker",
                "owner_id": "cs_lead",
                "role": "primary",
                "token": "never-issued-token",
            }
        )
        reply = _recv(conn)
        assert reply["type"] == "auth_error"


def test_token_store_주입시_revoke된_토큰은_거부된다() -> None:
    token_store = InMemoryTokenStore(token_factory=lambda: "console-issued-raw-token-2")
    app = create_central_app(token_store=token_store)
    client = TestClient(app)

    issued = _post(
        client, "/console/tokens", {"owner_id": "cs_lead", "role": "primary"}
    ).json()
    raw_token = issued["token"]
    token_id = issued["token_id"]

    revoke_resp = _post(client, f"/console/tokens/{token_id}/revoke", {})
    assert revoke_resp.status_code == 200

    with _ws(client) as conn:
        conn.send_json(
            {
                "type": "register_worker",
                "owner_id": "cs_lead",
                "role": "primary",
                "token": raw_token,
            }
        )
        reply = _recv(conn)
        assert reply["type"] == "auth_error"


def test_token_store_주입시_다른_owner_토큰으로는_거부된다() -> None:
    """콘솔이 cs_lead용으로 발급한 토큰으로 legal_lead를 사칭 register 시도 → 거부."""
    token_store = InMemoryTokenStore(token_factory=lambda: "console-issued-raw-token-3")
    app = create_central_app(token_store=token_store)
    client = TestClient(app)

    issued = _post(
        client, "/console/tokens", {"owner_id": "cs_lead", "role": "primary"}
    ).json()
    raw_token = issued["token"]

    with _ws(client) as conn:
        conn.send_json(
            {
                "type": "register_worker",
                "owner_id": "legal_lead",
                "role": "primary",
                "token": raw_token,
            }
        )
        reply = _recv(conn)
        assert reply["type"] == "auth_error"


def test_token_store_미주입이면_기존_stub_동작_보존() -> None:
    """create_central_app() 무인자 — token=None이어도 register 통과(하위호환)."""
    app = create_central_app()
    client = TestClient(app)

    with _ws(client) as conn:
        conn.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        reply = _recv(conn)
        assert reply["type"] == "welcome"
