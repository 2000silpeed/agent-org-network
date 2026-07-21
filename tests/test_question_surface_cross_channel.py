"""P17.2c-2 사용자 채널이 한 Request/Finalization을 공유하는지 검증한다."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event
from typing import Any, cast

import agent_org_network.question_stream_http as question_stream_http_module
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.mcp_server import create_question_mcp_server
from agent_org_network.conflict import Candidate, ConflictCase, DivergentVotes
from agent_org_network.decision import Unowned
from agent_org_network.demo import build_demo
from agent_org_network.demo_question_surfaces import build_demo_question_surface_composition
from agent_org_network.dispatch import EscalatedToManager, WorkTicket
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc
from agent_org_network.manager_queue import (
    FromDeadlock,
    FromDispatch,
    InMemoryManagerQueueStore,
    ManagerItem,
    ManagerQueueService,
)
from agent_org_network.p17_manager_disposition import (
    ManagerDispositionDependency,
    ManagerDispositionError,
    ManagerDispositionInProgress,
    ManagerDispositionInvalid,
)
from agent_org_network.question_resolution import RequesterPrincipal
from agent_org_network.question_stream import (
    PendingEvent,
    QuestionStreamEvent,
)
from agent_org_network.question_surface_composition import (
    QuestionSurfaceComposition,
    QuestionSurfaceCompositionError,
)
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import (
    _manager_disposition_http_error,  # pyright: ignore[reportPrivateUsage]
    create_app,
)


def _call_mcp(server: Any, tool: str, arguments: dict[str, object]) -> str:
    content, _ = asyncio.run(server.call_tool(tool, arguments))
    return content[0].text


def _cookie_value(response: Response) -> str:
    header = response.headers.get("set-cookie", "")
    pair = next(part.strip() for part in header.split(";") if part.strip().startswith("aon_uid="))
    return pair.split("=", 1)[1]


def _events(response: Response) -> list[tuple[str, dict[str, object]]]:
    parsed: list[tuple[str, dict[str, object]]] = []
    for frame in response.text.split("\n\n"):
        lines = [line for line in frame.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        name = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        raw = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        payload = json.loads(raw)
        assert isinstance(payload, dict)
        parsed.append((name, cast(dict[str, object], payload)))
    return parsed


def _mcp_for_same_requester(
    composition: QuestionSurfaceComposition,
    subject_id: str,
) -> Any:
    return create_question_mcp_server(
        application=composition.application,
        principal_provider=lambda: RequesterPrincipal(
            org_id="demo-org",
            subject_id=subject_id,
        ),
    )


def _grounded_contested_app() -> Any:
    knowledge = InMemoryKnowledgeStore()
    for agent_id in ("cs_ops", "finance_ops"):
        knowledge.put(
            KnowledgeBundleContent(
                agent_id=agent_id,
                documents=(
                    KnowledgeDoc(
                        path=f"{agent_id}.md",
                        body=f"{agent_id}의 보상 처리 지식입니다.",
                    ),
                ),
                version="v1",
                synced_at=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
            )
        )
    return create_app(runtime=StubRuntime(), knowledge_store=knowledge)


def _contested_case(client: TestClient, request_id: str) -> dict[str, object]:
    http: Any = client
    inbox = cast(Response, http.get("/inbox/cs_lead"))
    return cast(
        dict[str, object],
        next(case for case in inbox.json() if case.get("request_id") == request_id),
    )


def _p17_concur(
    client: TestClient,
    case_id: str,
    *,
    by_owner: str,
    on_agent: str,
) -> Response:
    http: Any = client
    return cast(
        Response,
        http.post(
            f"/cases/{case_id}/concur",
            json={
                "by_owner": by_owner,
                "on_agent": on_agent,
                "expected_round": 1,
                "stance": "withdraw",
            },
        ),
    )


def test_one_answered_request_is_identical_across_http_sse_and_mcp() -> None:
    app = create_app(runtime=StubRuntime())
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)

    with TestClient(app) as client:
        http: Any = client
        blocking = cast(
            Response,
            http.post("/ask", json={"question": "환불은 언제 되나요?"}),
        )
        answer = blocking.json()
        request_id = answer["request_id"]
        record_id = answer["record_id"]
        subject_id = _cookie_value(blocking)

        legacy = cast(Response, http.get(f"/ask/{request_id}"))
        canonical = cast(Response, http.get(f"/requests/{request_id}"))
        reconnected = cast(Response, http.get(f"/requests/{request_id}/stream"))
        mcp_text = _call_mcp(
            _mcp_for_same_requester(composition, subject_id),
            "get_question",
            {"request_id": request_id},
        )

    done = next(payload for name, payload in _events(reconnected) if name == "done")
    assert blocking.headers["x-request-id"] == request_id
    assert legacy.json() == answer
    assert canonical.json()["request_id"] == request_id
    assert canonical.json()["record_id"] == record_id
    assert reconnected.headers["x-request-id"] == request_id
    assert done["request_id"] == request_id
    assert done["record_id"] == record_id
    assert f"요청 ID: {request_id}" in mcp_text
    assert f"답변 기록: {record_id}" in mcp_text


@pytest.mark.parametrize(
    ("question", "native_kind", "legacy_kind", "state"),
    [
        ("평가 기준을 알려 주세요.", "routed", "dispatched", "awaiting_approval"),
        ("보상 기준은 무엇인가요?", "contested", "contested", "awaiting_conflict"),
        ("주차 등록은 어떻게 하나요?", "unowned", "unowned", "awaiting_manager"),
    ],
)
def test_pending_disposition_is_bodyless_and_correlated_across_channels(
    question: str,
    native_kind: str,
    legacy_kind: str,
    state: str,
) -> None:
    app = create_app(runtime=StubRuntime())
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)

    with TestClient(app) as client:
        http: Any = client
        blocking = cast(Response, http.post("/ask", json={"question": question}))
        legacy = blocking.json()
        request_id = legacy["request_id"]
        subject_id = _cookie_value(blocking)

        canonical = cast(Response, http.get(f"/requests/{request_id}"))
        reconnected = cast(Response, http.get(f"/requests/{request_id}/stream"))
        mcp_text = _call_mcp(
            _mcp_for_same_requester(composition, subject_id),
            "get_question",
            {"request_id": request_id},
        )

    native = canonical.json()
    pending = next(payload for name, payload in _events(reconnected) if name == "pending")
    assert legacy["tracking"] == request_id
    assert legacy["kind"] == legacy_kind
    assert legacy["state"] == state
    assert native["request_id"] == request_id
    assert native["kind"] == native_kind
    assert native["state"] == state
    assert pending["request_id"] == request_id
    assert pending["kind"] == native_kind
    assert pending["state"] == state
    assert f"요청 ID: {request_id}" in mcp_text
    assert f"처리 분류: {native_kind}" in mcp_text
    assert f"상태: {state}" in mcp_text
    for body in (legacy, native, pending):
        assert "text" not in body
        assert "answer_text" not in body
        assert "record_id" not in body


def test_direct_conflict_consensus_is_one_answer_across_http_sse_get_and_mcp() -> None:
    app = _grounded_contested_app()
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)

    with TestClient(app) as client:
        http: Any = client
        asked = cast(
            Response,
            http.post("/ask", json={"question": "보상 기준은 무엇인가요?"}),
        )
        request_id = cast(str, asked.json()["request_id"])
        subject_id = _cookie_value(asked)
        case_id = cast(str, _contested_case(client, request_id)["case_id"])
        pending = _p17_concur(
            client,
            case_id,
            by_owner="cs_lead",
            on_agent="cs_ops",
        )
        agreed = _p17_concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="cs_ops",
        )
        legacy = cast(Response, http.get(f"/ask/{request_id}"))
        canonical = cast(Response, http.get(f"/requests/{request_id}"))
        reconnected = cast(Response, http.get(f"/requests/{request_id}/stream"))
        mcp_text = _call_mcp(
            _mcp_for_same_requester(composition, subject_id),
            "get_question",
            {"request_id": request_id},
        )

    assert pending.json()["type"] == "still_open"
    assert agreed.json() == {
        "type": "agreed",
        "request_id": request_id,
        "case_id": case_id,
        "primary": "cs_ops",
        "intent": "보상",
    }
    canonical_body = canonical.json()
    legacy_body = legacy.json()
    done = next(payload for name, payload in _events(reconnected) if name == "done")
    assert canonical_body["request_id"] == request_id
    assert canonical_body["record_id"] == done["record_id"]
    assert legacy_body["record_id"] == done["record_id"]
    assert legacy_body["answered_by"]["agent_id"] == "cs_ops"
    assert f"요청 ID: {request_id}" in mcp_text
    assert f"답변 기록: {done['record_id']}" in mcp_text


def test_direct_conflict_grounding_failed_retry_is_same_result_across_http_sse_get_and_mcp() -> (
    None
):
    app = create_app(runtime=StubRuntime(), knowledge_store=InMemoryKnowledgeStore())
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)

    with TestClient(app) as client:
        http: Any = client
        asked = cast(
            Response,
            http.post("/ask", json={"question": "보상 기준은 무엇인가요?"}),
        )
        request_id = cast(str, asked.json()["request_id"])
        subject_id = _cookie_value(asked)
        case_id = cast(str, _contested_case(client, request_id)["case_id"])
        _p17_concur(client, case_id, by_owner="cs_lead", on_agent="cs_ops")
        agreed = _p17_concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="cs_ops",
        )
        watched = cast(Response, http.get(f"/requests/{request_id}/stream?watch=true"))
        retried = _p17_concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="cs_ops",
        )
        canonical = cast(Response, http.get(f"/requests/{request_id}"))
        reconnected = cast(Response, http.get(f"/requests/{request_id}/stream"))
        mcp_text = _call_mcp(
            _mcp_for_same_requester(composition, subject_id),
            "get_question",
            {"request_id": request_id},
        )

    expected_agreed = {
        "type": "agreed",
        "request_id": request_id,
        "case_id": case_id,
        "primary": "cs_ops",
        "intent": "보상",
    }
    assert agreed.json() == expected_agreed
    assert retried.status_code == 200
    assert retried.json() == expected_agreed
    expected_failure = "required_grounding_missing"
    assert canonical.json() == {
        "request_id": request_id,
        "error_code": expected_failure,
        "message": "질문을 처리하지 못했습니다.",
    }
    watched_failed = next(payload for name, payload in _events(watched) if name == "failed")
    late_failed = next(payload for name, payload in _events(reconnected) if name == "failed")
    assert watched_failed["request_id"] == request_id
    assert watched_failed["error_code"] == expected_failure
    assert late_failed["request_id"] == request_id
    assert late_failed["error_code"] == expected_failure
    assert f"요청 ID: {request_id}" in mcp_text
    assert f"오류 코드: {expected_failure}" in mcp_text


def test_deadlock_manager_assign_is_one_answer_across_http_sse_get_and_mcp() -> None:
    app = _grounded_contested_app()
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)

    with TestClient(app) as client:
        http: Any = client
        asked = cast(
            Response,
            http.post("/ask", json={"question": "보상 기준은 무엇인가요?"}),
        )
        request_id = cast(str, asked.json()["request_id"])
        subject_id = _cookie_value(asked)
        case_id = cast(str, _contested_case(client, request_id)["case_id"])
        _p17_concur(client, case_id, by_owner="cs_lead", on_agent="cs_ops")
        deadlocked = _p17_concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="finance_ops",
        )
        item_id = cast(str, deadlocked.json()["manager_item_id"])
        acted = cast(
            Response,
            http.post(
                f"/manager/items/{item_id}/act",
                json={
                    "type": "assign_owner",
                    "by_manager": "root_manager",
                    "primary": "cs_ops",
                },
            ),
        )
        canonical = cast(Response, http.get(f"/requests/{request_id}"))
        reconnected = cast(Response, http.get(f"/requests/{request_id}/stream"))
        mcp_text = _call_mcp(
            _mcp_for_same_requester(composition, subject_id),
            "get_question",
            {"request_id": request_id},
        )

    assert acted.status_code == 200
    assert acted.json()["request_outcome"] == "deadlock_owner_assigned"
    assert "continuation" not in acted.json()
    done = next(payload for name, payload in _events(reconnected) if name == "done")
    assert canonical.json()["request_id"] == request_id
    assert canonical.json()["record_id"] == done["record_id"]
    assert f"요청 ID: {request_id}" in mcp_text
    assert f"답변 기록: {done['record_id']}" in mcp_text


def test_deadlock_manager_dismiss_is_one_decline_across_http_sse_get_and_mcp() -> None:
    app = create_app(runtime=StubRuntime())
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)

    with TestClient(app) as client:
        http: Any = client
        asked = cast(
            Response,
            http.post("/ask", json={"question": "보상 기준은 무엇인가요?"}),
        )
        request_id = cast(str, asked.json()["request_id"])
        subject_id = _cookie_value(asked)
        case_id = cast(str, _contested_case(client, request_id)["case_id"])
        _p17_concur(client, case_id, by_owner="cs_lead", on_agent="cs_ops")
        deadlocked = _p17_concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="finance_ops",
        )
        item_id = cast(str, deadlocked.json()["manager_item_id"])
        acted = cast(
            Response,
            http.post(
                f"/manager/items/{item_id}/act",
                json={"type": "dismiss", "by_manager": "root_manager"},
            ),
        )
        canonical = cast(Response, http.get(f"/requests/{request_id}"))
        reconnected = cast(Response, http.get(f"/requests/{request_id}/stream"))
        mcp_text = _call_mcp(
            _mcp_for_same_requester(composition, subject_id),
            "get_question",
            {"request_id": request_id},
        )

    assert acted.status_code == 200
    assert acted.json()["request_outcome"] == "deadlock_dismissed"
    assert "continuation" not in acted.json()
    declined = next(payload for name, payload in _events(reconnected) if name == "declined")
    assert canonical.json()["request_id"] == request_id
    assert canonical.json()["reason_code"] == "manager_declined"
    assert declined["request_id"] == request_id
    assert declined["reason_code"] == "manager_declined"
    assert f"요청 ID: {request_id}" in mcp_text
    assert "거절" in mcp_text


def test_request_aware_unowned_dismiss_is_canonical_across_manager_http_sse_get_and_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(runtime=StubRuntime())
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)
    managers = cast(InMemoryManagerQueueStore, composition.manager_store)

    with TestClient(app) as client:
        http: Any = client
        asked = cast(
            Response,
            http.post("/ask", json={"question": "주차 등록은 어떻게 하나요?"}),
        )
        request_id = asked.json()["request_id"]
        subject_id = _cookie_value(asked)
        item = managers.get_by_request(request_id)
        assert item is not None
        pending_serialized = Event()
        original_serializer = question_stream_http_module.serialize_question_stream_sse

        def observe_pending(event: QuestionStreamEvent) -> str:
            frame = original_serializer(event)
            if isinstance(event, PendingEvent) and event.request_id == request_id:
                pending_serialized.set()
            return frame

        monkeypatch.setattr(
            question_stream_http_module,
            "serialize_question_stream_sse",
            observe_pending,
        )

        def watch_request() -> Response:
            return cast(
                Response,
                http.get(f"/requests/{request_id}/stream?watch=true"),
            )

        projected = cast(Response, http.get("/manager/root_manager")).json()
        visible = next(value for value in projected if value["item_id"] == item.item_id)
        with ThreadPoolExecutor(max_workers=1) as executor:
            watching = executor.submit(watch_request)
            saw_pending = pending_serialized.wait(timeout=3)
            acted = cast(
                Response,
                http.post(
                    f"/manager/items/{item.item_id}/act",
                    json={"type": "dismiss", "by_manager": "root_manager"},
                ),
            )
            watched = watching.result(timeout=5)
        canonical = cast(Response, http.get(f"/requests/{request_id}"))
        late = cast(Response, http.get(f"/requests/{request_id}/stream"))
        mcp_text = _call_mcp(
            _mcp_for_same_requester(composition, subject_id),
            "get_question",
            {"request_id": request_id},
        )

    assert visible["request_id"] == request_id
    assert "intent" in visible["source"]
    assert acted.status_code == 200
    assert acted.json()["request_outcome"] == "dismissed"
    assert "continuation" not in acted.json()
    assert saw_pending is True
    active_events = _events(watched)
    assert [name for name, _ in active_events] == ["pending", "declined"]
    assert active_events[0][1]["request_id"] == request_id
    assert active_events[1][1]["request_id"] == request_id
    assert active_events[1][1]["reason_code"] == "manager_declined"
    assert canonical.json()["request_id"] == request_id
    assert canonical.json()["reason_code"] == "manager_declined"
    declined = next(payload for name, payload in _events(late) if name == "declined")
    assert declined["request_id"] == request_id
    assert f"요청 ID: {request_id}" in mcp_text
    assert "거절" in mcp_text


def test_request_aware_unowned_wrong_manager_is_typed_403() -> None:
    app = create_app(runtime=StubRuntime())
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)
    managers = cast(InMemoryManagerQueueStore, composition.manager_store)

    with TestClient(app) as client:
        http: Any = client
        asked = cast(
            Response,
            http.post("/ask", json={"question": "주차 등록은 어떻게 하나요?"}),
        )
        request_id = cast(str, asked.json()["request_id"])
        item = managers.get_by_request(request_id)
        assert item is not None
        denied = cast(
            Response,
            http.post(
                f"/manager/items/{item.item_id}/act",
                json={"type": "dismiss", "by_manager": "other-manager"},
            ),
        )

    assert denied.status_code == 403
    assert denied.json()["detail"] == {
        "code": "manager_disposition_forbidden",
        "message": "이 Manager 처리 항목을 처분할 권한이 없습니다.",
        "retryable": False,
    }


def test_request_aware_unowned_reroute_fails_closed_without_legacy_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(runtime=StubRuntime())
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)
    managers = cast(InMemoryManagerQueueStore, composition.manager_store)

    with TestClient(app) as client:
        http: Any = client
        asked = cast(
            Response,
            http.post("/ask", json={"question": "주차 등록은 어떻게 하나요?"}),
        )
        item = managers.get_by_request(cast(str, asked.json()["request_id"]))
        assert item is not None

        def fail_if_legacy_called(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("request-aware 처분이 legacy service를 호출했습니다.")

        monkeypatch.setattr(ManagerQueueService, "act", fail_if_legacy_called)
        denied = cast(
            Response,
            http.post(
                f"/manager/items/{item.item_id}/act",
                json={
                    "type": "reroute",
                    "by_manager": "root_manager",
                    "to_agent": "cs_ops",
                },
            ),
        )

    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "unsupported_unowned_manager_action"
    assert managers.get(item.item_id) == item


def test_request_aware_action_without_manager_application_is_retryable_503() -> None:
    managers = InMemoryManagerQueueStore()
    composition = build_demo_question_surface_composition(
        build_demo(runtime=StubRuntime(), manager_queue_store=managers)
    )
    composition.manager_disposition = None
    app = create_app(
        runtime=StubRuntime(),
        manager_queue_store=managers,
        question_surface_composition=composition,
    )

    with TestClient(app) as client:
        http: Any = client
        asked = cast(
            Response,
            http.post("/ask", json={"question": "주차 등록은 어떻게 하나요?"}),
        )
        item = managers.get_by_request(cast(str, asked.json()["request_id"]))
        assert item is not None
        unavailable = cast(
            Response,
            http.post(
                f"/manager/items/{item.item_id}/act",
                json={"type": "dismiss", "by_manager": "root_manager"},
            ),
        )

    assert unavailable.status_code == 503
    assert unavailable.headers["retry-after"] == "1"
    assert unavailable.json()["detail"] == {
        "code": "manager_disposition_unavailable",
        "message": "Request-aware Manager 처분 기능을 사용할 수 없습니다.",
        "retryable": True,
    }
    assert managers.get(item.item_id) == item


class _AlwaysUnownedRefundRouter:
    def route(self, question: str) -> Unowned:
        return Unowned(
            escalated_to="root_manager",
            reason="테스트에서 담당 지정을 검증합니다.",
            intent="환불",
        )


def test_request_aware_unowned_assign_wakes_same_request_to_answered_across_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managers = InMemoryManagerQueueStore()
    bundle = replace(
        build_demo(runtime=StubRuntime(), manager_queue_store=managers),
        router=_AlwaysUnownedRefundRouter(),
    )
    composition = build_demo_question_surface_composition(bundle)
    app = create_app(
        runtime=StubRuntime(),
        manager_queue_store=managers,
        question_surface_composition=composition,
    )

    with TestClient(app) as client:
        http: Any = client
        asked = cast(
            Response,
            http.post("/ask", json={"question": "결제 취소를 도와주세요."}),
        )
        request_id = cast(str, asked.json()["request_id"])
        subject_id = _cookie_value(asked)
        item = managers.get_by_request(request_id)
        assert item is not None
        pending_serialized = Event()
        original_serializer = question_stream_http_module.serialize_question_stream_sse

        def observe_pending(event: QuestionStreamEvent) -> str:
            frame = original_serializer(event)
            if isinstance(event, PendingEvent) and event.request_id == request_id:
                pending_serialized.set()
            return frame

        monkeypatch.setattr(
            question_stream_http_module,
            "serialize_question_stream_sse",
            observe_pending,
        )

        def watch_request() -> Response:
            return cast(
                Response,
                http.get(f"/requests/{request_id}/stream?watch=true"),
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            watching = executor.submit(watch_request)
            saw_pending = pending_serialized.wait(timeout=3)
            acted = cast(
                Response,
                http.post(
                    f"/manager/items/{item.item_id}/act",
                    json={
                        "type": "assign_owner",
                        "by_manager": "root_manager",
                        "primary": "cs_ops",
                    },
                ),
            )
            watched = watching.result(timeout=5)
        canonical = cast(Response, http.get(f"/requests/{request_id}"))
        late = cast(Response, http.get(f"/requests/{request_id}/stream"))
        mcp_text = _call_mcp(
            _mcp_for_same_requester(composition, subject_id),
            "get_question",
            {"request_id": request_id},
        )

    assert acted.status_code == 200
    assert acted.json()["request_outcome"] == "owner_assigned"
    assert "continuation" not in acted.json()
    assert saw_pending is True
    active_events = _events(watched)
    assert active_events[0][0] == "pending"
    assert active_events[-1][0] == "done"
    done = active_events[-1][1]
    assert done["request_id"] == request_id
    assert canonical.json()["request_id"] == request_id
    assert canonical.json()["record_id"] == done["record_id"]
    late_done = next(payload for name, payload in _events(late) if name == "done")
    assert late_done["record_id"] == done["record_id"]
    assert f"요청 ID: {request_id}" in mcp_text
    assert f"답변 기록: {done['record_id']}" in mcp_text


def test_web_rejects_manager_store_identity_mismatch_and_closes_injected_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = InMemoryManagerQueueStore()
    composition = build_demo_question_surface_composition(
        build_demo(runtime=StubRuntime(), manager_queue_store=first)
    )
    close_calls: list[str] = []

    def close_scheduler(*, wait: bool = True) -> None:
        assert wait is True
        close_calls.append("scheduler")

    def close_storage() -> None:
        close_calls.append("storage")

    monkeypatch.setattr(composition.application, "shutdown", close_scheduler)
    monkeypatch.setattr(composition.storage, "close", close_storage, raising=False)

    with pytest.raises(QuestionSurfaceCompositionError):
        create_app(
            runtime=StubRuntime(),
            manager_queue_store=InMemoryManagerQueueStore(),
            question_surface_composition=composition,
        )

    assert close_calls == ["scheduler", "storage"]


@pytest.mark.parametrize(
    ("source_kind", "status", "code"),
    [
        ("deadlock", 500, "manager_disposition_integrity"),
        ("dispatch", 400, "unsupported_request_aware_manager_source"),
    ],
)
def test_unlinked_or_unsupported_request_aware_manager_sources_fail_closed(
    source_kind: str,
    status: int,
    code: str,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    if source_kind == "deadlock":
        source = FromDeadlock(
            case=ConflictCase.for_request(
                request_id="request-other-source",
                intent="환불",
                question="환불 문의",
                candidates=(Candidate(agent_id="cs_ops", owner="cs_lead"),),
                opened_at=now,
                case_id="case-other-source",
            ),
            reason="divergent_votes",
            cause=DivergentVotes(round=1),
        )
    else:
        source = FromDispatch(
            outcome=EscalatedToManager(
                ticket=WorkTicket.for_request(
                    request_id="request-other-source",
                    attempt=1,
                    owner_id="cs_lead",
                    agent_id="cs_ops",
                    question="환불 문의",
                    enqueued_at=now,
                    ticket_id="ticket-other-source",
                ),
                manager_id="root_manager",
                reason="담당자 부재",
            )
        )
    managers = InMemoryManagerQueueStore()
    item = ManagerItem(
        manager_id="root_manager",
        source=source,
        created_at=now,
        item_id=f"item-{source_kind}",
        request_id="request-other-source",
    )
    # 공개 관문을 건너뛴 손상/선행 데이터를 직접 구성한다. Deadlock은 연계 증거
    # 불일치로, FromDispatch는 미지원 출처로 닫히며 둘 다 legacy 처분으로 우회하지 않는다.
    unsafe_store = cast(Any, managers)
    with unsafe_store._lock:
        unsafe_store._enqueue_unlocked(item)
    app = create_app(runtime=StubRuntime(), manager_queue_store=managers)

    with TestClient(app) as client:
        http: Any = client
        response = cast(
            Response,
            http.post(
                f"/manager/items/{item.item_id}/act",
                json={"type": "dismiss", "by_manager": "root_manager"},
            ),
        )

    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert managers.get(item.item_id) == item


@pytest.mark.parametrize(
    ("error", "status", "retry_after"),
    [
        (ManagerDispositionInProgress(), 409, "1"),
        (ManagerDispositionDependency(), 503, "1"),
        (ManagerDispositionInvalid("외부 의존성 비밀"), 400, None),
    ],
)
def test_manager_disposition_http_mapping_is_typed_and_safe(
    error: ManagerDispositionError,
    status: int,
    retry_after: str | None,
) -> None:
    response = _manager_disposition_http_error(error)

    assert response.status_code == status
    assert (response.headers or {}).get("Retry-After") == retry_after
    assert "외부 의존성 비밀" not in json.dumps(response.detail, ensure_ascii=False)
