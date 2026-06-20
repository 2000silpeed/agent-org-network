"""데모 팩토리 + 웹 직렬화 스모크 — end-to-end 한 바퀴를 결정론으로 고정.

StubRuntime만 쓰므로 실제 LLM 없이 항상 같은 결과. HTTP 왕복은 얇은 FastAPI
래핑이라, 핸들러 결과를 dict로 바꾸는 serialize_reply(불변식의 핵심)를 직접 검증한다.
"""

from typing import Any

from agent_org_network.ask_org import Answered, OrgReply, Pending
from agent_org_network.demo import build_demo_ask_org
from agent_org_network.user import User
from agent_org_network.web import create_app, serialize_reply

_USER = User(id="tester")

# 라우팅 내부값 — 사용자 응답에 절대 새면 안 되는 키들.
_LEAKY_KEYS = {"confidence", "candidates", "escalated_to", "reason", "primary", "intent"}


def _reply_to_json(question: str) -> dict[str, Any]:
    """데모 핸들러를 한 번 돌려 직렬화 dict까지 만든다(웹 경로와 동일)."""
    reply: OrgReply = build_demo_ask_org().handle(question, _USER)
    return serialize_reply(reply)


def test_데모_계약질문은_contract_ops가_답한다():
    reply = build_demo_ask_org().handle("이 계약 조건 바꿔도 돼?", _USER)

    assert isinstance(reply, Answered)
    assert reply.answered_by == ("legal_lead", "contract_ops")
    assert reply.mode == "full"
    assert "위키/계약가이드" in reply.sources


def test_데모_주차장질문은_unowned로_미아되지_않는다():
    reply = build_demo_ask_org().handle("주차장 정기권 어떻게 갱신해요?", _USER)

    assert isinstance(reply, Pending)
    assert reply.kind == "unowned"


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
    app = create_app()

    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/ask" in routes
    assert "/" in routes
