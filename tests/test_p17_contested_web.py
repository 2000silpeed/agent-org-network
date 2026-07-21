"""P17.5 request-aware 다툼·Manager HTTP 운영 표면 계약."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc
from agent_org_network.p17_conflict_disposition import (
    ConflictDispositionConflict,
    ConflictDispositionDependency,
    ConflictDispositionError,
    ConflictDispositionForbidden,
    ConflictDispositionInProgress,
    ConflictDispositionIntegrity,
    ConflictDispositionInvalid,
    ConflictDispositionNotFound,
    ConsensusRouteRejected,
)
from agent_org_network.question_surface_composition import QuestionSurfaceComposition
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import (
    _conflict_disposition_http_error,  # pyright: ignore[reportPrivateUsage]
    create_app,
    serialize_p17_concurrence,
)


def _response(client: TestClient, method: str, path: str, **kwargs: object) -> Response:
    http: Any = client
    return cast(Response, getattr(http, method)(path, **kwargs))


def _grounded_app() -> Any:
    store = InMemoryKnowledgeStore()
    for agent_id in ("cs_ops", "finance_ops"):
        store.put(
            KnowledgeBundleContent(
                agent_id=agent_id,
                documents=(
                    KnowledgeDoc(
                        path=f"{agent_id}.md",
                        body=f"{agent_id}의 보상 처리 기준입니다.",
                    ),
                ),
                version="v1",
                synced_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            )
        )
    return create_app(runtime=StubRuntime(), knowledge_store=store)


def _open_contested(client: TestClient) -> tuple[str, str, dict[str, object]]:
    asked = _response(
        client,
        "post",
        "/ask",
        json={"question": "보상 기준은 무엇인가요?"},
    )
    assert asked.status_code == 200
    request_id = cast(str, asked.json()["request_id"])
    inbox = _response(client, "get", "/inbox/cs_lead")
    case = next(item for item in inbox.json() if item.get("request_id") == request_id)
    assert case["status"] == "open"
    assert case["current_round"] == 1
    return request_id, cast(str, case["case_id"]), cast(dict[str, object], case)


def _concur(
    client: TestClient,
    case_id: str,
    *,
    by_owner: str,
    on_agent: str,
    stance: str = "withdraw",
) -> Response:
    return _response(
        client,
        "post",
        f"/cases/{case_id}/concur",
        json={
            "by_owner": by_owner,
            "on_agent": on_agent,
            "rationale": "운영 판단",
            "expected_round": 1,
            "stance": stance,
        },
    )


def test_request_aware_concur_requires_server_round_and_never_uses_legacy_service() -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        _request_id, case_id, _case = _open_contested(client)
        missing_round = _response(
            client,
            "post",
            f"/cases/{case_id}/concur",
            json={"by_owner": "cs_lead", "on_agent": "cs_ops"},
        )

    assert missing_round.status_code == 400
    assert missing_round.json()["detail"] == {
        "code": "conflict_disposition_invalid",
        "message": "다툼 합의 요청이 유효하지 않습니다.",
        "retryable": False,
    }


def test_request_aware_concur_returns_stable_result_and_terminal_retry_is_exact() -> None:
    app = _grounded_app()

    with TestClient(app) as client:
        request_id, case_id, _case = _open_contested(client)
        first = _concur(client, case_id, by_owner="cs_lead", on_agent="cs_ops")
        second = _concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="cs_ops",
            stance="keep_as_complement",
        )
        retried = _concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="cs_ops",
            stance="keep_as_complement",
        )

    assert first.status_code == 200
    assert first.json() == {
        "type": "still_open",
        "request_id": request_id,
        "case_id": case_id,
        "current_round": 1,
        "pending_owners": ["finance_lead"],
    }
    expected = {
        "type": "agreed",
        "request_id": request_id,
        "case_id": case_id,
        "primary": "cs_ops",
        "intent": "보상",
    }
    assert second.status_code == 200
    assert second.json() == expected
    assert retried.status_code == 200
    assert retried.json() == expected
    assert "wake" not in second.json()
    assert "continuation" not in second.json()


def test_final_concurrence의_첫_HTTP_response_loss는_same_POST로_terminal을_복구한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _grounded_app()
    original = serialize_p17_concurrence
    calls = 0

    def lose_first_response(outcome: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("response serialization lost after commit")
        return original(cast(Any, outcome))

    with TestClient(app, raise_server_exceptions=False) as client:
        request_id, case_id, _case = _open_contested(client)
        pending = _concur(client, case_id, by_owner="cs_lead", on_agent="cs_ops")
        assert pending.status_code == 200
        monkeypatch.setattr(
            "agent_org_network.web.serialize_p17_concurrence",
            lose_first_response,
        )
        first_final = _concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="cs_ops",
            stance="keep_as_complement",
        )
        retried = _concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="cs_ops",
            stance="keep_as_complement",
        )

    assert first_final.status_code == 500
    assert retried.status_code == 200
    assert retried.json() == {
        "type": "agreed",
        "request_id": request_id,
        "case_id": case_id,
        "primary": "cs_ops",
        "intent": "보상",
    }


def test_request_aware_conflict_error_is_typed_and_does_not_leak_domain_text() -> None:
    app = create_app(runtime=StubRuntime())

    with TestClient(app) as client:
        _request_id, case_id, _case = _open_contested(client)
        denied = _concur(client, case_id, by_owner="other_owner", on_agent="cs_ops")

    assert denied.status_code == 403
    assert denied.json()["detail"] == {
        "code": "conflict_disposition_forbidden",
        "message": "이 다툼 케이스에 합의할 권한이 없습니다.",
        "retryable": False,
    }


def test_deadlock_manager_assign_resumes_same_request_and_retry_body_is_stable() -> None:
    app = _grounded_app()
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)

    with TestClient(app) as client:
        request_id, case_id, _case = _open_contested(client)
        assert _concur(client, case_id, by_owner="cs_lead", on_agent="cs_ops").status_code == 200
        deadlocked = _concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="finance_ops",
        )
        deadlock_body = deadlocked.json()
        manager_item_id = cast(str, deadlock_body["manager_item_id"])
        action = {
            "type": "assign_owner",
            "by_manager": "root_manager",
            "primary": "cs_ops",
            "rationale": "고객 응대 기준으로 지정",
        }
        assigned = _response(
            client,
            "post",
            f"/manager/items/{manager_item_id}/act",
            json=action,
        )
        retried = _response(
            client,
            "post",
            f"/manager/items/{manager_item_id}/act",
            json=action,
        )

    assert deadlocked.status_code == 200
    assert deadlock_body == {
        "type": "deadlocked",
        "request_id": request_id,
        "case_id": case_id,
        "current_round": 1,
        "manager_item_id": manager_item_id,
    }
    assert assigned.status_code == 200
    assert assigned.json()["request_outcome"] == "deadlock_owner_assigned"
    assert "continuation" not in assigned.json()
    assert "wake" not in assigned.json()
    assert retried.status_code == 200
    assert retried.json() == assigned.json()

    conflict_store = cast(Any, composition.conflict_store)
    stored_case = conflict_store.get_request_case(case_id)
    assert stored_case is not None
    assert stored_case.status == "resolved"
    assert stored_case.resolution is not None
    assert stored_case.resolution.primary == "cs_ops"


def test_deadlock_manager_dismiss_declines_same_request_without_transient_delivery() -> None:
    app = create_app(runtime=StubRuntime())
    composition = cast(QuestionSurfaceComposition, app.state.question_surface_composition)

    with TestClient(app) as client:
        request_id, case_id, _case = _open_contested(client)
        _concur(client, case_id, by_owner="cs_lead", on_agent="cs_ops")
        deadlocked = _concur(
            client,
            case_id,
            by_owner="finance_lead",
            on_agent="finance_ops",
        )
        item_id = cast(str, deadlocked.json()["manager_item_id"])
        dismissed = _response(
            client,
            "post",
            f"/manager/items/{item_id}/act",
            json={
                "type": "dismiss",
                "by_manager": "root_manager",
                "rationale": "처리하지 않음",
            },
        )
        lookup = _response(client, "get", f"/requests/{request_id}")

    assert dismissed.status_code == 200
    assert dismissed.json()["request_outcome"] == "deadlock_dismissed"
    assert "continuation" not in dismissed.json()
    assert "delivery" not in dismissed.json()
    assert lookup.status_code == 200
    assert lookup.json()["reason_code"] == "manager_declined"

    stored_case = cast(Any, composition.conflict_store).get_request_case(case_id)
    assert stored_case is not None and stored_case.status == "declined"


def test_route_rejection_serializer_exposes_stable_round_contract_only() -> None:
    outcome = ConsensusRouteRejected(
        request_id="request-1",
        case_id="case-1",
        current_round=2,
        next_round=3,
        reason_code="policy_changed",
    )

    assert serialize_p17_concurrence(outcome) == {
        "type": "route_rejected",
        "request_id": "request-1",
        "case_id": "case-1",
        "current_round": 2,
        "next_round": 3,
        "reason_code": "policy_changed",
    }


def test_conflict_disposition_http_mapping_is_typed_and_never_leaks_error_text() -> None:
    cases: tuple[tuple[ConflictDispositionError, int, str | None], ...] = (
        (ConflictDispositionNotFound("secret-not-found"), 404, None),
        (ConflictDispositionForbidden("secret-forbidden"), 403, None),
        (ConflictDispositionInvalid("secret-invalid"), 400, None),
        (ConflictDispositionInProgress("secret-progress"), 409, "1"),
        (ConflictDispositionConflict("secret-conflict"), 409, None),
        (ConflictDispositionDependency("secret-dependency"), 503, "1"),
        (ConflictDispositionIntegrity("secret-integrity"), 500, None),
    )

    for error, expected_status, retry_after in cases:
        response = _conflict_disposition_http_error(error)
        assert response.status_code == expected_status
        assert (response.headers or {}).get("Retry-After") == retry_after
        assert isinstance(response.detail, dict)
        detail = cast(dict[str, object], response.detail)
        assert detail["code"] == error.code
        assert detail["retryable"] is error.retryable
        assert "secret-" not in str(detail)
