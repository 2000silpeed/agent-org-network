"""담당자 모니터링 + 질문자 정정 배지 웹 라우트 결정론 테스트 (Phase 12 (A)(B)·ADR 0033 결정 4).

실 uvicorn·실 WS 없이 인프로세스 ASGI TestClient로 잠근다:
  - (적재 배선) `/ask`가 답을 낼 때 중앙 `AnswerRecordStore`에 `AnswerRecord`가 실린다.
  - (A) 담당자 모니터링 — `GET /supervision/answers`(자기 에이전트 Q&A·검토 필요 필터)·
    `POST /supervision/answers/{record_id}/correct`(정정 제출·owner 스코핑)·
    `GET /supervision/presence/{agent_id}`(프레즌스 배지).
  - (B) 질문자 정정 배지 — `GET /answer/{record_id}/correction`(원문+정정본·풀 방식).

StubRuntime만 써 실 LLM 0·결정론. 정정 통지 채널(push)은 게이트 밖이라 여기 없다.
"""

from collections.abc import Callable
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.answer_record import (
    CorrectionStore,
    InMemoryAnswerRecordStore,
    InMemoryCorrectionStore,
    monitoring_for_owner,
)
from agent_org_network.presence import PresenceStatus
from agent_org_network.runtime import Answer, StubRuntime
from agent_org_network.transport import WebSocketDispatcher
from agent_org_network.web import create_app


def _fixed_presence(status: PresenceStatus) -> Callable[[str], PresenceStatus]:
    """그 status를 항상 반환하는 프레즌스 조회 콜백(타입 명시 — pyright strict)."""

    def _lookup(_agent_id: str) -> PresenceStatus:
        return status

    return _lookup


# pyright strict: starlette TestClient는 httpx 반환을 Unknown으로 노출한다(test_web.py와 동형).
# 호출부로 unknown이 새지 않게 client를 Any로 좁혀 호출하고 Response로 cast한다.


def _get(client: TestClient, url: str, **kwargs: Any) -> Response:
    http: Any = client
    return cast(Response, http.get(url, **kwargs))


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> Response:
    http: Any = client
    return cast(Response, http.post(url, json=payload))


def _json(res: Response) -> dict[str, Any]:
    return cast(dict[str, Any], res.json())


def _list(res: Response) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], res.json())


def _client(
    *,
    answer_record_store: InMemoryAnswerRecordStore | None = None,
    correction_store: CorrectionStore | None = None,
    presence_of: Any = None,
) -> tuple[TestClient, InMemoryAnswerRecordStore, CorrectionStore]:
    ars = answer_record_store if answer_record_store is not None else InMemoryAnswerRecordStore()
    cs = correction_store if correction_store is not None else InMemoryCorrectionStore()
    app = create_app(
        runtime=StubRuntime(),
        answer_record_store=ars,
        correction_store=cs,
        presence_of=presence_of,
    )
    return TestClient(app), ars, cs


def _seed_answer(client: TestClient) -> dict[str, Any]:
    return _json(_post(client, "/ask", {"question": "이 계약 조건 바꿔도 돼?"}))


# ── 적재 배선: /ask가 AnswerRecord를 적재한다 ────────────────────────────


def test_ask가_답을_내면_answer_record가_적재된다() -> None:
    client, ars, _ = _client()
    body = _seed_answer(client)
    assert body["type"] == "answered"
    # 답변 페이지 정정 조회용 불투명 손잡이가 사용자向 응답에 실린다.
    record_id = body["record_id"]
    assert isinstance(record_id, str) and record_id
    # 중앙 스토어에 그 답이 감사 단위로 append됐다.
    rec = ars.get(record_id)
    assert rec is not None
    assert rec.answer_text == body["text"]
    assert rec.agent_id == body["answered_by"]["agent_id"]


def test_오프라인_담당자_자동발신은_검토필요로_적재된다() -> None:
    # presence_of가 그 에이전트를 offline으로 보고하고 mode=full이면 검토 필요 표식.
    client, ars, _ = _client(presence_of=_fixed_presence("offline"))
    body = _seed_answer(client)
    rec = ars.get(body["record_id"])
    assert rec is not None
    assert rec.needs_correction_review is True


def test_온라인_담당자는_검토필요_아님() -> None:
    client, ars, _ = _client(presence_of=_fixed_presence("online"))
    body = _seed_answer(client)
    rec = ars.get(body["record_id"])
    assert rec is not None
    assert rec.needs_correction_review is False


# ── (A) 담당자 모니터링 목록 ─────────────────────────────────────────────


def test_모니터링_목록은_자기_에이전트의_답을_보여준다() -> None:
    client, _, _ = _client()
    body = _seed_answer(client)
    agent_id = body["answered_by"]["agent_id"]
    items = _list(_get(client, "/supervision/answers", params={"agent_id": agent_id}))
    assert len(items) == 1
    assert items[0]["record_id"] == body["record_id"]
    assert items[0]["question"] == "이 계약 조건 바꿔도 돼?"
    assert items[0]["answer_text"] == body["text"]
    assert "needs_correction_review" in items[0]
    assert items[0]["corrections"] == []


def test_모니터링_검토필요_필터() -> None:
    client, _, _ = _client(presence_of=_fixed_presence("offline"))
    body = _seed_answer(client)
    agent_id = body["answered_by"]["agent_id"]
    items = _list(
        _get(
            client,
            "/supervision/answers",
            params={"agent_id": agent_id, "needs_review": "true"},
        )
    )
    assert len(items) == 1
    assert items[0]["needs_correction_review"] is True

    # 온라인 답은 검토 필요 필터에서 빠진다.
    client2, _, _ = _client(presence_of=_fixed_presence("online"))
    body2 = _seed_answer(client2)
    items2 = _list(
        _get(
            client2,
            "/supervision/answers",
            params={"agent_id": body2["answered_by"]["agent_id"], "needs_review": "true"},
        )
    )
    assert items2 == []


# ── (A) 정정 제출 ────────────────────────────────────────────────────────


def test_정정_제출하면_correction_event가_쌓이고_원레코드는_불변() -> None:
    client, ars, cs = _client()
    body = _seed_answer(client)
    record_id = body["record_id"]
    owner = body["answered_by"]["owner"]
    original_text = body["text"]

    res = _post(
        client,
        f"/supervision/answers/{record_id}/correct",
        {"by_owner": owner, "corrected_text": "정정된 답입니다.", "rationale": "오류 수정"},
    )
    assert res.status_code == 200
    assert _json(res)["submitted"] is True

    # 원 레코드는 그대로(전이 ≠ 기록).
    rec = ars.get(record_id)
    assert rec is not None and rec.answer_text == original_text
    # 정정 이벤트가 append됐다.
    events = cs.for_record(record_id)
    assert len(events) == 1
    assert events[0].corrected_text == "정정된 답입니다."


def test_남의_에이전트_정정은_거부된다() -> None:
    client, _, cs = _client()
    body = _seed_answer(client)
    record_id = body["record_id"]
    res = _post(
        client,
        f"/supervision/answers/{record_id}/correct",
        {"by_owner": "not_the_owner", "corrected_text": "몰래 고침"},
    )
    assert res.status_code == 403
    assert cs.for_record(record_id) == []


def test_미존재_레코드_정정은_404() -> None:
    client, _, _ = _client()
    res = _post(
        client,
        "/supervision/answers/nope/correct",
        {"by_owner": "someone", "corrected_text": "x"},
    )
    assert res.status_code == 404


def test_같은_정정_재제출은_멱등() -> None:
    client, _, cs = _client()
    body = _seed_answer(client)
    record_id = body["record_id"]
    owner = body["answered_by"]["owner"]
    payload = {"by_owner": owner, "corrected_text": "동일 정정"}
    _post(client, f"/supervision/answers/{record_id}/correct", payload)
    _post(client, f"/supervision/answers/{record_id}/correct", payload)
    assert len(cs.for_record(record_id)) == 1


# ── (A) 프레즌스 배지 ────────────────────────────────────────────────────


def test_프레즌스_배지_조회() -> None:
    client, _, _ = _client(presence_of=_fixed_presence("offline"))
    assert _json(_get(client, "/supervision/presence/contract_ops"))["status"] == "offline"

    client2, _, _ = _client(presence_of=_fixed_presence("online"))
    assert _json(_get(client2, "/supervision/presence/contract_ops"))["status"] == "online"


def test_프레즌스_미배선이면_offline_기본() -> None:
    client, _, _ = _client()  # presence_of=None
    assert _json(_get(client, "/supervision/presence/contract_ops"))["status"] == "offline"


# ── (B) 질문자 정정 배지 ─────────────────────────────────────────────────


def test_정정_전에는_배지_없음() -> None:
    client, _, _ = _client()
    body = _seed_answer(client)
    record_id = body["record_id"]
    view = _json(_get(client, f"/answer/{record_id}/correction"))
    assert view["has_correction"] is False
    assert view["original_text"] == body["text"]
    assert view["corrected_text"] is None


def test_정정_후_배지와_정정본이_보인다() -> None:
    client, _, _ = _client()
    body = _seed_answer(client)
    record_id = body["record_id"]
    owner = body["answered_by"]["owner"]
    _post(
        client,
        f"/supervision/answers/{record_id}/correct",
        {"by_owner": owner, "corrected_text": "정정본입니다."},
    )
    view = _json(_get(client, f"/answer/{record_id}/correction"))
    assert view["has_correction"] is True
    assert view["corrected_text"] == "정정본입니다."
    # 원문도 보존돼 함께 반환된다(풀 방식).
    assert view["original_text"] == body["text"]


def test_미존재_레코드_배지_조회는_404() -> None:
    client, _, _ = _client()
    res = _get(client, "/answer/nope/correction")
    assert res.status_code == 404


# ── 페이지 라우트 + 분산 회신 경로 적재 ──────────────────────────────────


def test_supervision_페이지가_서빙된다() -> None:
    client, _, _ = _client()
    res = _get(client, "/supervision")
    assert res.status_code == 200
    assert "답변 감독" in res.text


def test_분산_회신_경로도_answer_record를_멱등_적재한다() -> None:
    ars = InMemoryAnswerRecordStore()
    cs = InMemoryCorrectionStore()
    ws = WebSocketDispatcher()
    app = create_app(
        runtime=StubRuntime(),
        dispatcher=ws,
        answer_record_store=ars,
        correction_store=cs,
    )
    client = TestClient(app)

    # 워커 미연결 → dispatched(tracking).
    asked = _json(_post(client, "/ask", {"question": "환불 되나요?"}))
    assert asked["kind"] == "dispatched"
    tracking = asked["tracking"]

    # 워커 회신 시뮬.
    ticket = ws.claim("cs_lead")
    assert ticket is not None
    ws.submit(ticket.ticket_id, Answer(text="환불 가능합니다", sources=(), mode="full"))

    # 첫 회수 → answered + record_id, 스토어에 적재.
    after = _json(_get(client, f"/ask/{tracking}"))
    assert after["type"] == "answered"
    record_id = after["record_id"]
    assert ars.get(record_id) is not None

    # 재회수 → 같은 record_id(멱등 — 새 레코드 안 만듦).
    again = _json(_get(client, f"/ask/{tracking}"))
    assert again["record_id"] == record_id
    assert len(ars.for_agent("cs_ops")) == 1


# ── 도메인 조회 코어 회귀(모니터링 투영이 정정 이력을 싣는다) ─────────────


def test_모니터링_목록에_정정_이력이_실린다() -> None:
    client, ars, cs = _client()
    body = _seed_answer(client)
    record_id = body["record_id"]
    owner = body["answered_by"]["owner"]
    agent_id = body["answered_by"]["agent_id"]
    _post(
        client,
        f"/supervision/answers/{record_id}/correct",
        {"by_owner": owner, "corrected_text": "고침"},
    )
    items = _list(_get(client, "/supervision/answers", params={"agent_id": agent_id}))
    assert len(items) == 1
    assert len(items[0]["corrections"]) == 1
    assert items[0]["corrections"][0]["corrected_text"] == "고침"
    # 도메인 코어와 일치(투영이 monitoring_for_owner를 소비한다).
    core = monitoring_for_owner(ars, cs, agent_id=agent_id)
    assert len(core) == 1 and len(core[0].corrections) == 1
