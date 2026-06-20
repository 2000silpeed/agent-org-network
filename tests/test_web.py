"""데모 팩토리 + 웹 직렬화 스모크 — end-to-end 한 바퀴를 결정론으로 고정.

StubRuntime만 쓰므로 실제 LLM 없이 항상 같은 결과. HTTP 왕복은 얇은 FastAPI
래핑이라, 핸들러 결과를 dict로 바꾸는 serialize_reply(불변식의 핵심)를 직접 검증한다.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.ask_org import Answered, OrgReply, Pending
from agent_org_network.conflict import (
    Agreed,
    Candidate,
    ConflictCase,
    Deadlocked,
    Precedent,
    Resolution,
    StillOpen,
)
from agent_org_network.demo import build_demo_ask_org
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User
from agent_org_network.web import (
    create_app,
    serialize_case,
    serialize_outcome,
    serialize_reply,
)

_USER = User(id="tester")

# 데모서 "보상" domain을 공유해 다툼이 나는 질문(cs_ops·finance_ops).
_CONTESTED_Q = "보상 기준이 어떻게 되나요?"

# 라우팅 내부값 — 사용자 응답에 절대 새면 안 되는 키들.
_LEAKY_KEYS = {"confidence", "candidates", "escalated_to", "reason", "primary", "intent"}


def _reply_to_json(question: str) -> dict[str, Any]:
    """데모 핸들러를 한 번 돌려 직렬화 dict까지 만든다(웹 경로와 동일).

    StubRuntime 주입 — 실제 claude 호출 없이 결정론 유지.
    """
    reply: OrgReply = build_demo_ask_org(runtime=StubRuntime()).handle(question, _USER)
    return serialize_reply(reply)


def test_데모_계약질문은_contract_ops가_답한다():
    reply = build_demo_ask_org(runtime=StubRuntime()).handle("이 계약 조건 바꿔도 돼?", _USER)

    assert isinstance(reply, Answered)
    assert reply.answered_by == ("legal_lead", "contract_ops")
    assert reply.mode == "full"
    assert "위키/계약가이드" in reply.sources


def test_데모_주차장질문은_unowned로_미아되지_않는다():
    reply = build_demo_ask_org(runtime=StubRuntime()).handle("주차장 정기권 어떻게 갱신해요?", _USER)

    assert isinstance(reply, Pending)
    assert reply.kind == "unowned"


def test_데모_보상질문은_contested로_합의대기된다():
    reply = build_demo_ask_org(runtime=StubRuntime()).handle("보상 기준이 어떻게 되나요?", _USER)

    assert isinstance(reply, Pending)
    assert reply.kind == "contested"


def test_직렬화_계약은_answered_dict():
    body = _reply_to_json("계약서 검토해줄 수 있어?")

    assert body["type"] == "answered"
    assert body["answered_by"]["agent_id"] == "contract_ops"
    assert body["answered_by"]["owner"] == "legal_lead"
    assert body["mode"] == "full"
    assert "위키/계약가이드" in body["sources"]


def test_직렬화_주차장은_pending_dict():
    body = _reply_to_json("주차장 어디예요?")

    assert body["type"] == "pending"
    assert body["kind"] == "unowned"
    assert body["message"]


def test_직렬화_보상은_pending_contested_dict():
    body = _reply_to_json("보상 기준이 어떻게 되나요?")

    assert body["type"] == "pending"
    assert body["kind"] == "contested"
    assert body["message"]


def test_직렬화에_라우팅_내부값이_새지_않는다():
    for q in ("계약 검토 부탁해", "환불 되나요?", "주차장 어디예요?"):
        body = _reply_to_json(q)
        top_keys: set[str] = set(body.keys())
        assert _LEAKY_KEYS.isdisjoint(top_keys)
        # 중첩 dict(answered_by)에도 내부값이 없어야 한다.
        for value in body.values():
            if isinstance(value, dict):
                nested_keys: set[str] = set(value.keys())  # pyright: ignore[reportUnknownArgumentType]
                assert _LEAKY_KEYS.isdisjoint(nested_keys)


def test_create_app_은_조립된다():
    app = create_app(runtime=StubRuntime())

    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/ask" in routes
    assert "/" in routes
    assert "/inbox" in routes
    assert "/inbox/{owner_id}" in routes
    assert "/cases/{case_id}/concur" in routes


# ── serialize_case / serialize_outcome 단위 ────────────────────────────


def _fixed_clock() -> datetime:
    return datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _sample_case() -> ConflictCase:
    return ConflictCase(
        intent="보상",
        question="보상 기준?",
        candidates=(
            Candidate(agent_id="cs_ops", owner="cs_lead"),
            Candidate(agent_id="finance_ops", owner="finance_lead"),
        ),
        opened_at=_fixed_clock(),
        case_id="case-xyz",
    )


def test_serialize_case는_intent_question_후보를_담는다():
    body = serialize_case(_sample_case())

    assert body["case_id"] == "case-xyz"
    assert body["intent"] == "보상"
    assert body["question"] == "보상 기준?"
    assert body["candidates"] == [
        {"agent_id": "cs_ops", "owner": "cs_lead"},
        {"agent_id": "finance_ops", "owner": "finance_lead"},
    ]


def test_serialize_outcome_agreed():
    resolution = Resolution(intent="보상", primary="cs_ops", rationale="r")
    precedent = Precedent(resolution=resolution, recorded_at=_fixed_clock())
    body = serialize_outcome(Agreed(resolution=resolution, precedent=precedent))

    assert body == {"type": "agreed", "primary": "cs_ops", "intent": "보상"}


def test_serialize_outcome_still_open():
    body = serialize_outcome(
        StillOpen(case=_sample_case(), pending_owners=("finance_lead",))
    )

    assert body["type"] == "still_open"
    assert body["pending_owners"] == ["finance_lead"]


def test_serialize_outcome_deadlocked():
    body = serialize_outcome(Deadlocked(case=_sample_case(), reason="표 갈림"))

    assert body == {"type": "deadlocked"}


# ── 처리함 라우트 (HTTP 왕복) ──────────────────────────────────────────
#
# pyright strict: starlette TestClient는 httpx 메서드 반환을 Unknown으로 노출한다
# (httpx deprecation 스텁). 호출부로 unknown이 새지 않게 status·json을 명시 타입
# (HttpResult)으로 좁혀 받는다.


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: Any


def _result(res: Response) -> HttpResult:
    body: Any = res.json()
    return HttpResult(status=res.status_code, body=body)


def _get(client: TestClient, url: str) -> HttpResult:
    http: Any = client
    return _result(cast(Response, http.get(url)))


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> HttpResult:
    http: Any = client
    return _result(cast(Response, http.post(url, json=payload)))


def _client() -> TestClient:
    app: FastAPI = create_app(runtime=StubRuntime())
    return TestClient(app)


def _open_case_id(client: TestClient) -> str:
    """채팅에 다툼 질문을 던져 ConflictCase를 열고 cs_lead 처리함의 case_id를 돌려준다."""
    contested = _post(client, "/ask", {"question": _CONTESTED_Q})
    assert contested.status == 200
    assert contested.body["kind"] == "contested"
    cases: list[dict[str, Any]] = _get(client, "/inbox/cs_lead").body
    assert len(cases) == 1
    case_id: str = cases[0]["case_id"]
    return case_id


def test_inbox_후보_Owner_처리함에_케이스가_뜬다():
    client = _client()
    _open_case_id(client)

    res = _get(client, "/inbox/cs_lead")
    assert res.status == 200
    cases: list[dict[str, Any]] = res.body
    assert len(cases) == 1
    case = cases[0]
    assert case["intent"] == "보상"
    assert case["question"] == _CONTESTED_Q
    agent_ids = {c["agent_id"] for c in case["candidates"]}
    assert agent_ids == {"cs_ops", "finance_ops"}


def test_inbox_비후보_Owner는_빈_목록():
    client = _client()
    _open_case_id(client)

    res = _get(client, "/inbox/legal_lead")
    assert res.status == 200
    assert res.body == []


def test_concur_한_표는_still_open():
    client = _client()
    case_id = _open_case_id(client)

    res = _post(
        client,
        f"/cases/{case_id}/concur",
        {"by_owner": "cs_lead", "on_agent": "cs_ops", "rationale": "환불과 묶임"},
    )
    assert res.status == 200
    assert res.body["type"] == "still_open"
    assert "finance_lead" in res.body["pending_owners"]


def test_concur_양_Owner_일치하면_agreed_되고_이후_채팅이_자동라우팅된다():
    client = _client()
    case_id = _open_case_id(client)

    first = _post(client, f"/cases/{case_id}/concur", {"by_owner": "cs_lead", "on_agent": "cs_ops"})
    assert first.body["type"] == "still_open"

    second = _post(
        client, f"/cases/{case_id}/concur", {"by_owner": "finance_lead", "on_agent": "cs_ops"}
    )
    assert second.body["type"] == "agreed"
    assert second.body["primary"] == "cs_ops"
    assert second.body["intent"] == "보상"

    # 합의 후: 같은 다툼 질문이 이제 자동 Routed로 answered(판례 적용).
    after = _post(client, "/ask", {"question": _CONTESTED_Q})
    assert after.body["type"] == "answered"
    assert after.body["answered_by"]["agent_id"] == "cs_ops"

    # 합의된 케이스는 처리함 목록에서 사라진다.
    assert _get(client, "/inbox/cs_lead").body == []


def test_concur_표가_갈리면_deadlocked():
    client = _client()
    case_id = _open_case_id(client)

    _post(client, f"/cases/{case_id}/concur", {"by_owner": "cs_lead", "on_agent": "cs_ops"})
    res = _post(
        client, f"/cases/{case_id}/concur", {"by_owner": "finance_lead", "on_agent": "finance_ops"}
    )
    assert res.status == 200
    assert res.body["type"] == "deadlocked"


def test_concur_미존재_case_id는_400():
    client = _client()

    res = _post(client, "/cases/없는케이스/concur", {"by_owner": "cs_lead", "on_agent": "cs_ops"})
    assert res.status == 400


def test_concur_비후보_Owner는_400():
    client = _client()
    case_id = _open_case_id(client)

    res = _post(client, f"/cases/{case_id}/concur", {"by_owner": "legal_lead", "on_agent": "cs_ops"})
    assert res.status == 400
