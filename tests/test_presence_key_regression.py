"""프레즌스 키 불일치 회귀 — 크로스머신 시연 실결함 4호.

트래커 등록(`WebSocketDispatcher.register/disconnect`)은 owner_id 키로 관측되는데
(`transport.py`), `AskOrg._record_answer`/`_record_answer_for_tracking`이 agent_id
키로 조회하면 트래커에 없는 키라 항상 offline로 폴백해 오탐이 났다(온라인 owner의
답도 needs_correction_review=True). 실 배선과 같은 키 체계(owner=cs_lead,
agent_id=cs_ops)로 재현·정정을 잠근다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.answer_record import (
    AnswerRecordReader,
    InMemoryAnswerRecordStore,
)
from agent_org_network.presence import InMemoryPresenceTracker, PresenceStatus
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app

_T0 = datetime(2026, 7, 5, 9, 0, 0, tzinfo=timezone.utc)

_QUESTION = "환불 절차 알려줘"  # cs_ops(agent_id)/cs_lead(owner) 카드로 라우팅.


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> Response:
    http: Any = client
    return cast(Response, http.post(url, json=payload))


def _get(client: TestClient, url: str) -> Response:
    http: Any = client
    return cast(Response, http.get(url))


def _json(res: Response) -> dict[str, Any]:
    return cast(dict[str, Any], res.json())


def _client(tracker: InMemoryPresenceTracker) -> tuple[TestClient, AnswerRecordReader]:
    ars = InMemoryAnswerRecordStore()
    app = create_app(
        runtime=StubRuntime(),
        answer_record_store=ars,
        presence_of=tracker.status,
    )
    return TestClient(app), cast(AnswerRecordReader, app.state.answer_record_view)


def test_owner가_online이면_실배선_키체계에서_검토필요_아님() -> None:
    """online Owner는 사전 검토 정책으로 bodyless Approval Pending이 된다."""
    tracker = InMemoryPresenceTracker()
    tracker.observe_connect("cs_lead", at=_T0)  # owner 키만 online.

    client, records = _client(tracker)
    body = _json(_post(client, "/ask", {"question": _QUESTION}))

    assert body["type"] == "pending"
    assert body["kind"] == "dispatched"
    assert body["state"] == "awaiting_approval"
    assert body["tracking"] == body["request_id"]
    assert "text" not in body
    assert "record_id" not in body
    assert records.for_agent("cs_ops") == []


def test_owner가_offline이면_즉시답과_사후교정_증거를_원자_보존한다() -> None:
    """offline은 승인 불필요로 답하되 AnswerRecord에 명시 검토 증거를 남긴다."""
    tracker = InMemoryPresenceTracker()
    tracker.observe_connect("cs_lead", at=_T0)
    tracker.observe_disconnect("cs_lead", at=_T0)

    client, records = _client(tracker)
    body = _json(_post(client, "/ask", {"question": _QUESTION}))

    assert body["type"] == "answered"
    rec = records.get(cast(str, body["record_id"]))
    assert rec is not None
    assert rec.mode == "full"
    assert rec.needs_correction_review is True

    items = _get(client, "/supervision/answers?agent_id=cs_ops&needs_review=true").json()
    assert len(items) == 1
    assert items[0]["record_id"] == body["record_id"]


def test_presence_원천은_Agent_Card가_아닌_Owner_키로만_조회한다() -> None:
    seen: list[str] = []

    def presence_of(owner_id: str) -> PresenceStatus:
        seen.append(owner_id)
        return "offline"

    app = create_app(runtime=StubRuntime(), presence_of=presence_of)
    with TestClient(app) as client:
        body = _json(_post(client, "/ask", {"question": _QUESTION}))

    assert body["type"] == "answered"
    assert seen
    assert set(seen) == {"cs_lead"}


def test_presence_미주입은_기존_즉시답과_false_기록을_보존한다() -> None:
    app = create_app(runtime=StubRuntime())
    records = cast(AnswerRecordReader, app.state.answer_record_view)

    with TestClient(app) as client:
        body = _json(_post(client, "/ask", {"question": _QUESTION}))

    assert body["type"] == "answered"
    rec = records.get(cast(str, body["record_id"]))
    assert rec is not None
    assert rec.needs_correction_review is False


def test_supervision_presence_라우트는_agent_id를_owner로_해석해_조회한다() -> None:
    """`GET /supervision/presence/{agent_id}` — 카드 owner를 registry로 해석해 owner 키로 조회.

    트래커엔 owner(cs_lead) 키만 online — agent_id(cs_ops)를 그대로 조회 키로 쓰면
    트래커에 없어 offline 오탐이 난다(수정 전 결함). agent_id→owner 해석 경유가 정답.
    """
    tracker = InMemoryPresenceTracker()
    tracker.observe_connect("cs_lead", at=_T0)

    client, _ = _client(tracker)
    body = _json(_get(client, "/supervision/presence/cs_ops"))

    assert body["agent_id"] == "cs_ops"
    assert body["status"] == "online"


def test_supervision_presence_미등록_agent_id는_offline_기본() -> None:
    tracker = InMemoryPresenceTracker()
    tracker.observe_connect("cs_lead", at=_T0)

    client, _ = _client(tracker)
    body = _json(_get(client, "/supervision/presence/no-such-agent"))

    assert body["status"] == "offline"
