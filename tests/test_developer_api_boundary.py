"""ADR 0073 — Developer API는 JSON/SSE/WebSocket만 제공한다."""

from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.owner_web import create_owner_app
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app, create_developer_api_app
from agent_org_network.worker import WorkerLogic


def _get(client: TestClient, path: str) -> Response:
    http: Any = client
    return cast(Response, http.get(path))


def test_명시적_Developer_API_팩토리와_호환_alias는_같은_조립이다() -> None:
    explicit = create_developer_api_app(runtime=StubRuntime())
    compatibility = create_app(runtime=StubRuntime())

    assert type(explicit) is type(compatibility)
    assert "/ask" in {getattr(route, "path", None) for route in explicit.routes}


def test_Developer_API는_HTML_페이지_경로를_제공하지_않는다() -> None:
    client = TestClient(create_developer_api_app(runtime=StubRuntime()))

    for path in (
        "/",
        "/inbox",
        "/builder",
        "/monitor/view",
        "/org/view",
        "/console/view",
        "/supervision",
        "/admin",
    ):
        response = _get(client, path)
        assert response.status_code == 404, path
        assert "text/html" not in response.headers.get("content-type", ""), path


def test_Developer_API는_framework_HTML_문서_UI를_제공하지_않는다() -> None:
    client = TestClient(create_developer_api_app(runtime=StubRuntime()))

    for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        response = _get(client, path)
        assert response.status_code == 404, path
        assert "text/html" not in response.headers.get("content-type", ""), path


def test_OpenAPI_JSON은_machine_contract로_유지한다() -> None:
    client = TestClient(create_developer_api_app(runtime=StubRuntime()))

    response = _get(client, "/openapi.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["openapi"]


def test_Developer_API_상태_엔드포인트는_JSON_준비상태를_반환한다() -> None:
    client = TestClient(create_developer_api_app(runtime=StubRuntime()))

    assert _get(client, "/healthz").json() == {"status": "ok"}
    assert _get(client, "/readyz").json() == {"status": "ready"}


def test_Owner_초안_API는_보존하고_HTML_루트는_제공하지_않는다() -> None:
    logic = WorkerLogic(owner_id="owner", cards={}, runtime=StubRuntime())
    client = TestClient(create_owner_app(logic, submit_sink=lambda _submit: None))

    assert _get(client, "/drafts").status_code == 200
    assert _get(client, "/").status_code == 404
