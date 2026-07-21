from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import agent_org_network.web as web_module
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.ask_org import AskOrg
from agent_org_network.demo import DemoBundle, build_demo
from agent_org_network.demo_question_surfaces import build_demo_question_surface_composition
from agent_org_network.manager_queue import InMemoryManagerQueueStore
from agent_org_network.question_surface_composition import (
    QuestionSurfaceComposition,
    QuestionSurfaceCompositionError,
)
from agent_org_network.presence import PresenceStatus
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app


def _post(client: TestClient, question: str, *, cookies: dict[str, str] | None = None) -> Response:
    http: Any = client
    return cast(
        Response,
        http.post("/requests", json={"question": question}, cookies=cookies),
    )


def _get(
    client: TestClient,
    path: str,
    *,
    cookies: dict[str, str] | None = None,
) -> Response:
    http: Any = client
    return cast(Response, http.get(path, cookies=cookies))


def _sse_events(response: Response) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for frame in response.text.split("\n\n"):
        lines = [line for line in frame.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        raw = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        payload = json.loads(raw)
        assert isinstance(payload, dict)
        events.append((event, cast(dict[str, object], payload)))
    return events


def _cookie_value(response: Response) -> str:
    header = response.headers.get("set-cookie", "")
    pair = next(part.strip() for part in header.split(";") if part.strip().startswith("aon_uid="))
    return pair.split("=", 1)[1]


def test_create_app_registers_canonical_question_routes() -> None:
    app = create_app(runtime=StubRuntime())
    with TestClient(app):
        paths = set(app.openapi()["paths"])

    assert {"/requests", "/requests/{request_id}/stream", "/requests/{request_id}"} <= paths
    assert isinstance(app.state.question_surface_composition, QuestionSurfaceComposition)


def test_native_request_sse_and_lookup_share_request_and_record() -> None:
    app = create_app(runtime=StubRuntime())
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)

    with TestClient(app) as client:
        response = _post(client, "환불은 언제 되나요?")
        events = _sse_events(response)
        request_id = response.headers["x-request-id"]
        done = next(payload for event, payload in events if event == "done")

        assert response.status_code == 200
        assert done["request_id"] == request_id
        assert isinstance(done["record_id"], str)
        lookup = _get(client, f"/requests/{request_id}")
        assert lookup.status_code == 200
        assert lookup.headers["x-request-id"] == request_id
        assert lookup.json()["request_id"] == request_id
        assert lookup.json()["record_id"] == done["record_id"]
        assert composition.storage.by_request(request_id) is not None
        stored = composition.storage.get(request_id)
        assert stored is not None
        assert stored.org_id == "demo-org"


def test_anonymous_cookie_binds_same_owner_and_hides_request_from_other_cookie() -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        created = _post(client, "환불은 언제 되나요?")
        request_id = created.headers["x-request-id"]
        subject_id = _cookie_value(created)
        header = created.headers["set-cookie"]

        assert subject_id
        assert "demo-org" not in subject_id
        assert "HttpOnly" in header
        assert "SameSite=lax" in header
        assert "Path=/" in header
        assert _get(client, f"/requests/{request_id}").status_code == 200

        hidden = _get(
            client,
            f"/requests/{request_id}",
            cookies={"aon_uid": "other-browser-00002"},
        )
        assert hidden.status_code == 404
        assert hidden.json() == {"detail": "질문 요청을 찾을 수 없습니다."}


def test_unsafe_anonymous_cookie_is_replaced_before_principal_binding() -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        response = _post(
            client,
            "환불은 언제 되나요?",
            cookies={"aon_uid": "short"},
        )

        replacement = _cookie_value(response)
        assert response.status_code == 200
        assert replacement != "short"
        assert len(replacement) >= 16
        request_id = response.headers["x-request-id"]
        stored = app.state.question_surface_composition.storage.get(request_id)
        assert stored is not None
        assert stored.requester_id == replacement


@pytest.mark.parametrize(
    ("question", "expected_state"),
    [
        ("보상 기준은 무엇인가요?", "awaiting_conflict"),
        ("주차 등록은 어떻게 하나요?", "awaiting_manager"),
    ],
)
def test_contested_and_unowned_native_requests_are_bodyless_pending(
    question: str,
    expected_state: str,
) -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        response = _post(client, question)
        events = _sse_events(response)

        assert [event for event, _ in events] == ["accepted", "pending"]
        pending = events[-1][1]
        assert pending["state"] == expected_state
        assert "text" not in pending
        assert "answer_text" not in pending
        assert "record_id" not in pending
        request_id = cast(str, pending["request_id"])
        assert app.state.question_surface_composition.storage.by_request(request_id) is None


def test_injected_composition_is_owned_and_closed_once_on_app_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = build_demo_question_surface_composition(build_demo(runtime=StubRuntime()))
    original_close = composition.close
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        original_close()

    monkeypatch.setattr(composition, "close", close)
    app = create_app(
        runtime=StubRuntime(),
        question_surface_composition=composition,
    )

    with TestClient(app):
        assert app.state.question_surface_composition is composition

    assert close_calls == 1


def test_injected_composition_is_closed_once_when_late_router_assembly_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = build_demo_question_surface_composition(build_demo(runtime=StubRuntime()))
    original_close = composition.close
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        original_close()

    def fail_include_router(self: FastAPI, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise RuntimeError("late-router-assembly-failure")

    monkeypatch.setattr(composition, "close", close)
    monkeypatch.setattr(FastAPI, "include_router", fail_include_router)

    with pytest.raises(RuntimeError, match="late-router-assembly-failure"):
        create_app(
            runtime=StubRuntime(),
            question_surface_composition=composition,
        )

    assert close_calls == 1


def test_injected_composition_is_closed_once_when_intermediate_demo_assembly_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = build_demo_question_surface_composition(build_demo(runtime=StubRuntime()))
    original_close = composition.close
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        original_close()

    def fail_build_demo(*args: object, **kwargs: object) -> DemoBundle:
        del args, kwargs
        raise RuntimeError("intermediate-demo-assembly-failure")

    monkeypatch.setattr(composition, "close", close)
    monkeypatch.setattr(web_module, "build_demo", fail_build_demo)

    with pytest.raises(RuntimeError, match="intermediate-demo-assembly-failure"):
        create_app(
            runtime=StubRuntime(),
            question_surface_composition=composition,
        )

    assert close_calls == 1


def test_injected_composition_is_closed_once_when_manager_store_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = build_demo_question_surface_composition(build_demo(runtime=StubRuntime()))
    original_close = composition.close
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        original_close()

    monkeypatch.setattr(composition, "close", close)

    with pytest.raises(QuestionSurfaceCompositionError, match="Manager Store"):
        create_app(
            runtime=StubRuntime(),
            question_surface_composition=composition,
            manager_queue_store=InMemoryManagerQueueStore(),
        )

    assert close_calls == 1


def test_default_composition_is_closed_once_when_late_router_assembly_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_builder = web_module.build_demo_question_surface_composition
    built: QuestionSurfaceComposition | None = None
    close_calls = 0

    def build(
        bundle: DemoBundle,
        *,
        presence_of: Callable[[str], PresenceStatus] | None = None,
    ) -> QuestionSurfaceComposition:
        nonlocal built, close_calls
        composition = original_builder(bundle, presence_of=presence_of)
        original_close = composition.close

        def close() -> None:
            nonlocal close_calls
            close_calls += 1
            original_close()

        monkeypatch.setattr(composition, "close", close)
        built = composition
        return composition

    def fail_include_router(self: FastAPI, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise RuntimeError("late-router-assembly-failure")

    monkeypatch.setattr(web_module, "build_demo_question_surface_composition", build)
    monkeypatch.setattr(FastAPI, "include_router", fail_include_router)

    with pytest.raises(RuntimeError, match="late-router-assembly-failure"):
        create_app(runtime=StubRuntime())

    assert built is not None
    assert close_calls == 1


def test_injected_composition에는_create_app_presence를_재주입하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = build_demo_question_surface_composition(build_demo(runtime=StubRuntime()))

    def forbidden_builder(*args: object, **kwargs: object) -> QuestionSurfaceComposition:
        del args, kwargs
        raise AssertionError("명시 composition이 있으면 기본 builder를 호출하면 안 됩니다.")

    def online(_owner_id: str) -> PresenceStatus:
        return "online"

    monkeypatch.setattr(
        web_module,
        "build_demo_question_surface_composition",
        forbidden_builder,
    )
    app = create_app(
        runtime=StubRuntime(),
        presence_of=online,
        question_surface_composition=composition,
    )

    with TestClient(app) as client:
        response = _post(client, "환불은 언제 되나요?")

    assert any(event == "done" for event, _ in _sse_events(response))


def test_late_assembly_error_survives_first_cleanup_failure_and_cleanup_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = build_demo_question_surface_composition(build_demo(runtime=StubRuntime()))
    original_close = composition.close
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("first-cleanup-failure")
        original_close()

    def fail_include_router(self: FastAPI, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise ValueError("late-router-assembly-failure")

    monkeypatch.setattr(composition, "close", close)
    monkeypatch.setattr(FastAPI, "include_router", fail_include_router)

    with pytest.raises(ValueError, match="late-router-assembly-failure") as error_info:
        create_app(
            runtime=StubRuntime(),
            question_surface_composition=composition,
        )

    assert close_calls == 2
    assert any("RuntimeError" in note for note in error_info.value.__notes__)


def test_native_request_never_calls_legacy_ask_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_handle(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("native /requests가 legacy AskOrg를 호출했습니다.")

    monkeypatch.setattr(AskOrg, "handle", forbidden_handle)
    app: FastAPI = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        response = _post(client, "환불은 언제 되나요?")

    assert response.status_code == 200
    assert any(event == "done" for event, _ in _sse_events(response))


def test_native_request_body_cannot_self_report_principal() -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        http: Any = client
        response = cast(
            Response,
            http.post(
                "/requests",
                json={
                    "question": "환불은 언제 되나요?",
                    "org_id": "other-org",
                    "subject_id": "other-user",
                },
            ),
        )

    assert response.status_code == 422
