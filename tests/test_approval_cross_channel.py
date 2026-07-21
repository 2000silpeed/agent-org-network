"""P17.6b S5.4 Approval 결과의 HTTP·SSE·MCP 의미 동등성."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.approval_operations import (
    ApprovalMadeUnavailable,
    ApprovalReassigned,
)
from agent_org_network.demo import build_demo
from agent_org_network.demo_question_surfaces import build_demo_question_surface_composition
from agent_org_network.mcp_server import create_question_mcp_server
from agent_org_network.question_resolution import RequesterPrincipal
from agent_org_network.question_surface_composition import QuestionSurfaceComposition
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app

QUESTION = "환불 기준을 알려 주세요."
EDITED_ANSWER = "수정된 최종 환불 답변입니다."
START = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


@dataclass
class _MutableClock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass(frozen=True)
class _ObservedChannels:
    legacy: dict[str, object]
    canonical: dict[str, object]
    events: tuple[tuple[str, dict[str, object]], ...]
    sse_text: str
    mcp_text: str


@dataclass(frozen=True)
class _PendingCase:
    request_id: str
    item_id: str
    requester_id: str
    candidate_text: str


def _response(client: TestClient, method: str, path: str, **kwargs: object) -> Response:
    http: Any = client
    return cast(Response, getattr(http, method)(path, **kwargs))


def _cookie_value(response: Response) -> str:
    header = response.headers.get("set-cookie", "")
    pair = next(part.strip() for part in header.split(";") if part.strip().startswith("aon_uid="))
    return pair.split("=", 1)[1]


def _events(response: Response) -> tuple[tuple[str, dict[str, object]], ...]:
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
    return tuple(parsed)


def _call_mcp(server: Any, request_id: str) -> str:
    content, _ = asyncio.run(server.call_tool("get_question", {"request_id": request_id}))
    return content[0].text


def _build_case() -> tuple[Any, QuestionSurfaceComposition, _MutableClock]:
    clock = _MutableClock()
    request_ids = count(1)
    record_ids = count(1)
    draft_ids = count(1)
    item_ids = count(1)
    bundle = build_demo(runtime=StubRuntime())
    composition = build_demo_question_surface_composition(
        bundle,
        presence_of=lambda _owner_id: "online",
        clock=clock,
        request_id_factory=lambda: f"request-{next(request_ids)}",
        record_id_factory=lambda: f"record-{next(record_ids)}",
        draft_id_factory=lambda: f"draft-{next(draft_ids)}",
        approval_item_id_factory=lambda: f"approval-{next(item_ids)}",
    )
    app = create_app(
        runtime=StubRuntime(),
        session_secret="approval-cross-channel-session-secret",
        presence_of=lambda _owner_id: "online",
        question_surface_composition=composition,
    )
    return app, composition, clock


def _open_pending(client: TestClient) -> _PendingCase:
    asked = _response(client, "post", "/ask", json={"question": QUESTION})
    assert asked.status_code == 200
    payload = cast(dict[str, object], asked.json())
    assert payload["type"] == "pending"
    assert payload["state"] == "awaiting_approval"
    request_id = cast(str, payload["request_id"])
    requester_id = _cookie_value(asked)

    logged_in = _response(client, "post", "/login", json={"user_id": "cs_lead"})
    assert logged_in.status_code == 200
    queue = _response(client, "get", "/inbox/approvals")
    assert queue.status_code == 200
    items = cast(list[dict[str, object]], queue.json())
    assert len(items) == 1
    item_id = cast(str, items[0]["item_id"])
    detail = _response(client, "get", f"/inbox/approvals/{item_id}")
    assert detail.status_code == 200
    detail_payload = cast(dict[str, object], detail.json())
    candidate = cast(dict[str, object], detail_payload["candidate"])
    return _PendingCase(
        request_id=request_id,
        item_id=item_id,
        requester_id=requester_id,
        candidate_text=cast(str, candidate["text"]),
    )


def _observe(
    client: TestClient,
    composition: QuestionSurfaceComposition,
    pending: _PendingCase,
) -> _ObservedChannels:
    legacy_response = _response(client, "get", f"/ask/{pending.request_id}")
    canonical_response = _response(client, "get", f"/requests/{pending.request_id}")
    sse_response = _response(client, "get", f"/requests/{pending.request_id}/stream")
    assert legacy_response.status_code == 200
    assert canonical_response.status_code == 200
    assert sse_response.status_code == 200
    mcp = create_question_mcp_server(
        application=composition.application,
        principal_provider=lambda: RequesterPrincipal(
            org_id="demo-org",
            subject_id=pending.requester_id,
        ),
    )
    return _ObservedChannels(
        legacy=cast(dict[str, object], legacy_response.json()),
        canonical=cast(dict[str, object], canonical_response.json()),
        events=_events(sse_response),
        sse_text=sse_response.text,
        mcp_text=_call_mcp(mcp, pending.request_id),
    )


def _assert_pending_is_bodyless(
    observed: _ObservedChannels,
    pending: _PendingCase,
    *,
    hidden_approvers: tuple[str, ...],
) -> None:
    pending_event = next(payload for name, payload in observed.events if name == "pending")
    assert observed.legacy["request_id"] == pending.request_id
    assert observed.canonical["request_id"] == pending.request_id
    assert pending_event["request_id"] == pending.request_id
    assert observed.legacy["state"] == "awaiting_approval"
    assert observed.canonical["state"] == "awaiting_approval"
    assert pending_event["state"] == "awaiting_approval"
    assert f"요청 ID: {pending.request_id}" in observed.mcp_text
    assert "상태: awaiting_approval" in observed.mcp_text

    rendered = (
        json.dumps(observed.legacy, ensure_ascii=False, sort_keys=True),
        json.dumps(observed.canonical, ensure_ascii=False, sort_keys=True),
        observed.sse_text,
        observed.mcp_text,
    )
    for value in rendered:
        assert QUESTION not in value
        assert pending.candidate_text not in value
        assert "draft-1" not in value
        assert "demo-approval-v1" not in value
        for forbidden_key in ("question", "candidate", "draft", "approver", "policy", "history"):
            assert f'"{forbidden_key}"' not in value
        for approver in hidden_approvers:
            assert approver not in value


def test_open_pending은_네_사용자_채널에서_같은_본문없는_상태다() -> None:
    app, composition, _clock = _build_case()

    with TestClient(app) as client:
        pending = _open_pending(client)
        observed = _observe(client, composition, pending)

    _assert_pending_is_bodyless(observed, pending, hidden_approvers=("cs_lead",))


def test_manual_reassigned도_같은_Request의_본문없는_pending으로_남는다() -> None:
    app, composition, _clock = _build_case()

    with TestClient(app) as client:
        pending = _open_pending(client)
        moved = _response(
            client,
            "post",
            f"/inbox/approvals/{pending.item_id}/reassign",
            json={"approver_id": "finance_lead"},
        )
        assert moved.status_code == 200
        moved_payload = cast(dict[str, object], moved.json())
        assert moved_payload["request_id"] == pending.request_id
        assert moved_payload["approval_round"] == 2
        assert (
            _response(
                client,
                "post",
                "/login",
                json={"user_id": "finance_lead"},
            ).status_code
            == 200
        )
        observed = _observe(client, composition, pending)

    _assert_pending_is_bodyless(
        observed,
        pending,
        hidden_approvers=("cs_lead", "finance_lead"),
    )


@pytest.mark.parametrize(
    ("decision", "expected_answer"),
    [
        ({"kind": "approve"}, None),
        ({"kind": "approve_with_edit", "edited_text": EDITED_ANSWER}, EDITED_ANSWER),
    ],
    ids=["approve", "approve-with-edit"],
)
def test_approve계열은_네_채널에서_같은_record와_answer_의미다(
    decision: dict[str, str],
    expected_answer: str | None,
) -> None:
    app, composition, _clock = _build_case()

    with TestClient(app) as client:
        pending = _open_pending(client)
        decided = _response(
            client,
            "post",
            f"/inbox/approvals/{pending.item_id}/decide",
            json=decision,
        )
        assert decided.status_code == 200
        decision_payload = cast(dict[str, object], decided.json())
        record_id = cast(str, decision_payload["record_id"])
        observed = _observe(client, composition, pending)

    answer_text = pending.candidate_text if expected_answer is None else expected_answer
    done = next(payload for name, payload in observed.events if name == "done")
    assert observed.legacy["type"] == "answered"
    assert observed.legacy["record_id"] == record_id
    assert observed.legacy["text"] == answer_text
    assert observed.canonical["record_id"] == record_id
    assert observed.canonical["answer_text"] == answer_text
    assert observed.canonical["review_status"] == "approved"
    assert done["request_id"] == pending.request_id
    assert done["record_id"] == record_id
    assert done["review_status"] == "approved"
    assert f"요청 ID: {pending.request_id}" in observed.mcp_text
    assert f"답변 기록: {record_id}" in observed.mcp_text
    assert answer_text in observed.mcp_text


def test_reject는_네_채널에서_같은_declined다() -> None:
    app, composition, _clock = _build_case()

    with TestClient(app) as client:
        pending = _open_pending(client)
        rejected = _response(
            client,
            "post",
            f"/inbox/approvals/{pending.item_id}/decide",
            json={"kind": "reject", "reason_code": "needs_revision"},
        )
        assert rejected.status_code == 200
        observed = _observe(client, composition, pending)

    declined = next(payload for name, payload in observed.events if name == "declined")
    assert observed.legacy["type"] == "declined"
    assert observed.legacy["request_id"] == pending.request_id
    assert observed.legacy["reason_code"] == "needs_revision"
    assert observed.canonical["request_id"] == pending.request_id
    assert observed.canonical["reason_code"] == "needs_revision"
    assert declined["request_id"] == pending.request_id
    assert declined["reason_code"] == "needs_revision"
    assert "질문 처리가 거절되었습니다." in observed.mcp_text
    assert f"요청 ID: {pending.request_id}" in observed.mcp_text


def test_lifecycle_unavailable은_네_채널에서_같은_failed_의미다() -> None:
    app, composition, clock = _build_case()

    with TestClient(app) as client:
        pending = _open_pending(client)
        clock.advance(timedelta(minutes=31))
        first = composition.approval_operations.expire_due(clock.now, 10)
        assert len(first) == 1
        assert isinstance(first[0], ApprovalReassigned)
        clock.advance(timedelta(minutes=31))
        second = composition.approval_operations.expire_due(clock.now, 10)
        assert len(second) == 1
        assert isinstance(second[0], ApprovalMadeUnavailable)
        observed = _observe(client, composition, pending)

    failed = next(payload for name, payload in observed.events if name == "failed")
    assert observed.legacy["type"] == "failed"
    assert observed.legacy["request_id"] == pending.request_id
    assert observed.legacy["error_code"] == "approval_unavailable"
    assert observed.canonical["request_id"] == pending.request_id
    assert observed.canonical["error_code"] == "approval_unavailable"
    assert failed["request_id"] == pending.request_id
    assert failed["error_code"] == "approval_unavailable"
    assert "질문을 처리하지 못했습니다." in observed.mcp_text
    assert "approval_unavailable" in observed.mcp_text
    assert f"요청 ID: {pending.request_id}" in observed.mcp_text
