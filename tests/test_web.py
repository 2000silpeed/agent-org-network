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
from agent_org_network.agent_card import AgentCard
from agent_org_network.demo import build_demo_ask_org
from agent_org_network.registry import Registry
from agent_org_network.runtime import Answer, StubRuntime, StubStreamingRuntime
from agent_org_network.session import InMemorySessionStore
from agent_org_network.transport import WebSocketDispatcher
from agent_org_network.user import User
from agent_org_network.knowledge_index import Concept, KnowledgeIndex
from agent_org_network.two_stage_router import InMemoryPublishedIndexStore
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


def test_backup_mode가_사용자에게_노출되고_내부값은_새지_않는다():
    """ADR 0012 결정 4 — mode=backup은 사용자가 알아야 할 신뢰값이라 노출 OK(불변식 무관).

    백업 답은 "owner 본인 실시간 답이 아니다"라는 신뢰 정보라 mode 축에 그대로 실린다.
    _LEAKY_KEYS(조직 내부 구조)와는 무관 — mode는 본디 노출하는 신뢰 상태값이다.
    """
    reply = Answered(
        text="담당 부재 중 백업 답변입니다",
        answered_by=("alice", "cs_ops"),
        mode="backup",
        sources=("위키/환불정책",),
    )
    body = serialize_reply(reply)

    assert body["type"] == "answered"
    assert body["mode"] == "backup"  # 신뢰 하향이 사용자에게 노출된다.
    # 담당·책임 귀속은 owner 불변(결정 5) — 백업이 답해도 answered_by는 그 owner.
    assert body["answered_by"] == {"owner": "alice", "agent_id": "cs_ops"}
    # 조직 내부값은 여전히 안 샌다.
    assert _LEAKY_KEYS.isdisjoint(set(body.keys()))
    for value in body.values():
        if isinstance(value, dict):
            nested: set[str] = set(value.keys())  # pyright: ignore[reportUnknownArgumentType]
            assert _LEAKY_KEYS.isdisjoint(nested)


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


def _sample_registry() -> Registry:
    """serialize_case 테스트용 최소 Registry — cs_ops·finance_ops 카드 포함."""
    from datetime import date

    reg = Registry()
    reg.register_user(User(id="cs_lead"))
    reg.register_user(User(id="finance_lead"))
    reg.register(
        AgentCard(
            agent_id="cs_ops",
            owner="cs_lead",
            team="cs",
            summary="고객 환불·보상 처리",
            domains=["환불", "보상"],
            knowledge_sources=["cs_wiki", "환불정책"],
            last_reviewed_at=date(2026, 1, 1),
        )
    )
    reg.register(
        AgentCard(
            agent_id="finance_ops",
            owner="finance_lead",
            team="finance",
            summary="가격·보상 정책 관리",
            domains=["가격", "보상"],
            knowledge_sources=["재무규정"],
            last_reviewed_at=date(2026, 1, 1),
        )
    )
    return reg


def test_serialize_case는_intent_question_후보를_담는다():
    body = serialize_case(_sample_case(), _sample_registry())

    assert body["case_id"] == "case-xyz"
    assert body["intent"] == "보상"
    assert body["question"] == "보상 기준?"
    assert body["candidates"] == [
        {
            "agent_id": "cs_ops",
            "owner": "cs_lead",
            "summary": "고객 환불·보상 처리",
            "domains": ["환불", "보상"],
            "knowledge_sources": ["cs_wiki", "환불정책"],
        },
        {
            "agent_id": "finance_ops",
            "owner": "finance_lead",
            "summary": "가격·보상 정책 관리",
            "domains": ["가격", "보상"],
            "knowledge_sources": ["재무규정"],
        },
    ]


def test_serialize_case_미등록_agent_id는_커버리지_필드_생략():
    """registry에 없는 agent_id를 가진 후보는 agent_id·owner만 담고 예외 없음."""
    case = ConflictCase(
        intent="테스트",
        question="질문",
        candidates=(Candidate(agent_id="unknown_ops", owner="some_lead"),),
        opened_at=_fixed_clock(),
        case_id="case-unknown",
    )
    reg = Registry()

    body = serialize_case(case, reg)

    assert body["case_id"] == "case-unknown"
    cand = body["candidates"][0]
    assert cand["agent_id"] == "unknown_ops"
    assert cand["owner"] == "some_lead"
    assert "summary" not in cand
    assert "domains" not in cand
    assert "knowledge_sources" not in cand


def test_serialize_case_커버리지는_카드에서_채운다():
    """registry 주입 시 각 후보에 summary·domains·knowledge_sources가 채워진다."""
    reg = _sample_registry()
    body = serialize_case(_sample_case(), reg)

    cs = body["candidates"][0]
    assert cs["summary"] == "고객 환불·보상 처리"
    assert cs["domains"] == ["환불", "보상"]
    assert cs["knowledge_sources"] == ["cs_wiki", "환불정책"]

    fin = body["candidates"][1]
    assert fin["summary"] == "가격·보상 정책 관리"
    assert fin["domains"] == ["가격", "보상"]
    assert fin["knowledge_sources"] == ["재무규정"]


# ── serialize_case + published_index_store 단위 ────────────────────────────


def _sample_published_store() -> InMemoryPublishedIndexStore:
    """cs_ops·finance_ops 인덱스를 담은 InMemoryPublishedIndexStore."""
    now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
    cs_idx = KnowledgeIndex(
        agent_id="cs_ops",
        version="v1",
        generated_at=now,
        concepts=(
            Concept(
                id="c_refund",
                label="환불",
                core_question="환불 정책이 어떻게 되나?",
                domain="환불",
            ),
            Concept(
                id="c_comp",
                label="보상",
                core_question="보상 기준이 무엇인가?",
                domain="보상",
            ),
            Concept(
                id="c_unrelated",
                label="기타",
                core_question="전혀 관계없는 매우 특이한 주제",
                domain="기타",
            ),
        ),
    )
    fin_idx = KnowledgeIndex(
        agent_id="finance_ops",
        version="v1",
        generated_at=now,
        concepts=(
            Concept(
                id="c_price",
                label="가격",
                core_question="가격 정책이 어떻게 되나?",
                domain="가격",
            ),
            Concept(
                id="c_comp2",
                label="보상",
                core_question="보상 기준이 어떻게 적용되나?",
                domain="보상",
            ),
        ),
    )
    store = InMemoryPublishedIndexStore()
    store.put(cs_idx)
    store.put(fin_idx)
    return store


def test_serialize_case_store_주입시_relevant_concepts_채운다():
    """published_index_store 주입 시 각 후보에 relevant_concepts 필드가 채워진다."""
    reg = _sample_registry()
    store = _sample_published_store()
    body = serialize_case(_sample_case(), reg, published_index_store=store)

    cs = body["candidates"][0]
    assert "relevant_concepts" in cs
    # 질문 "보상 기준?" → cs_ops의 c_comp(보상 기준이 무엇인가?)와 오버랩
    rc_ids = {r["id"] for r in cs["relevant_concepts"]}
    assert "c_comp" in rc_ids
    # c_unrelated는 오버랩 없으므로 제외
    assert "c_unrelated" not in rc_ids


def test_serialize_case_store_None이면_relevant_concepts_생략():
    """published_index_store=None이면 relevant_concepts 필드 없음 — 기존 동작 보존."""
    reg = _sample_registry()
    body = serialize_case(_sample_case(), reg)

    for cand in body["candidates"]:
        assert "relevant_concepts" not in cand


def test_serialize_case_store_None_기존_커버리지_보존():
    """store=None일 때 기존 커버리지 필드(summary·domains·knowledge_sources)는 유지."""
    reg = _sample_registry()
    body = serialize_case(_sample_case(), reg)

    cs = body["candidates"][0]
    assert cs["summary"] == "고객 환불·보상 처리"
    assert cs["domains"] == ["환불", "보상"]
    assert cs["knowledge_sources"] == ["cs_wiki", "환불정책"]


def test_serialize_case_relevant_concepts_필드_구조():
    """relevant_concepts 각 항목에 id·label·core_question이 있다."""
    reg = _sample_registry()
    store = _sample_published_store()
    body = serialize_case(_sample_case(), reg, published_index_store=store)

    for cand in body["candidates"]:
        for rc in cand.get("relevant_concepts", []):
            assert "id" in rc
            assert "label" in rc
            assert "core_question" in rc


def test_serialize_case_미등록_agent_id_방어():
    """store에 인덱스 없는 agent_id — relevant_concepts 필드 생략(방어적)."""
    case = ConflictCase(
        intent="테스트",
        question="보상 기준?",
        candidates=(Candidate(agent_id="unknown_ops", owner="some_lead"),),
        opened_at=_fixed_clock(),
        case_id="case-unknown2",
    )
    reg = Registry()
    store = _sample_published_store()  # unknown_ops 없음
    body = serialize_case(case, reg, published_index_store=store)

    cand = body["candidates"][0]
    assert "relevant_concepts" not in cand


def test_serialize_case_store_주입시_기존_커버리지_보존():
    """store 주입 시에도 기존 커버리지(summary·domains·knowledge_sources) 필드는 유지."""
    reg = _sample_registry()
    store = _sample_published_store()
    body = serialize_case(_sample_case(), reg, published_index_store=store)

    cs = body["candidates"][0]
    assert cs["summary"] == "고객 환불·보상 처리"
    assert cs["domains"] == ["환불", "보상"]
    assert cs["knowledge_sources"] == ["cs_wiki", "환불정책"]
    assert "relevant_concepts" in cs  # 둘 다 공존


def test_serialize_case_오버랩_0이면_relevant_concepts_빈_리스트():
    """질문과 오버랩이 전혀 없으면 relevant_concepts=[] (빈 리스트)."""
    now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
    # 질문 "보상 기준?"과 전혀 무관한 개념들만 가진 인덱스
    cs_idx = KnowledgeIndex(
        agent_id="cs_ops",
        version="v1",
        generated_at=now,
        concepts=(
            Concept(
                id="c_unrelated",
                label="기타",
                core_question="전혀 관계없는 매우 특이한 주제",
                domain="기타",
            ),
        ),
    )
    store = InMemoryPublishedIndexStore()
    store.put(cs_idx)
    reg = _sample_registry()
    body = serialize_case(_sample_case(), reg, published_index_store=store)

    cs = next(c for c in body["candidates"] if c["agent_id"] == "cs_ops")
    assert cs["relevant_concepts"] == []


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
    """채팅에 다툼 질문을 던져 ConflictCase를 열고 cs_lead 처리함의 case_id를 돌려준다.

    co-grounding 활성(ADR 0037 슬라이스 D) 이후 프로덕션 `/ask`의 다툼 질문은 "답+합의
    병행"으로 `answered`를 돌려주되 ConflictCase는 *여전히 그대로 열린다*(결정 5). 이 헬퍼는
    후자(케이스 개방)만 쓰므로 응답 타입은 answered로 단언한다.
    """
    contested = _post(client, "/ask", {"question": _CONTESTED_Q})
    assert contested.status == 200
    assert contested.body["type"] == "answered"
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


def test_concur_미존재_case_id는_404():
    """ADR 0016 결정 4: 대상 미존재 → 404 (기존 400은 틀린 기대였음)."""
    client = _client()

    res = _post(client, "/cases/없는케이스/concur", {"by_owner": "cs_lead", "on_agent": "cs_ops"})
    assert res.status == 404


def test_concur_비후보_Owner는_403():
    """비후보 owner의 concur — 스코프 위반이라 403(ADR 0016 결정 4 재배선)."""
    client = _client()
    case_id = _open_case_id(client)

    res = _post(client, f"/cases/{case_id}/concur", {"by_owner": "legal_lead", "on_agent": "cs_ops"})
    assert res.status == 403


# ── T6.3 슬라이스2b-i — 답 회수 조회(GET /ask/{tracking}) + 노출 불변식 ──────


def test_serialize_dispatched는_tracking을_싣되_내부값은_안샌다():
    """dispatched 직렬화에 불투명 tracking은 실리되 _LEAKY_KEYS는 안 샌다."""
    body = serialize_reply(
        Pending(kind="dispatched", message="전달했어요", tracking="opaque-abc123")
    )

    assert body["type"] == "pending"
    assert body["kind"] == "dispatched"
    assert body["tracking"] == "opaque-abc123"
    # 라우팅 내부값은 여전히 안 샌다(tracking은 불투명 ID 1개라 leaky가 아님).
    assert _LEAKY_KEYS.isdisjoint(set(body.keys()))


def test_serialize_contested는_tracking을_싣지_않는다():
    """contested/unowned는 tracking이 None이라 키 자체가 없다."""
    body = serialize_reply(Pending(kind="contested", message="확인 중"))

    assert "tracking" not in body
    assert _LEAKY_KEYS.isdisjoint(set(body.keys()))


def test_ask_get_라우트가_등록된다():
    app = create_app(runtime=StubRuntime())
    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/ask/{tracking}" in routes


def test_모르는_tracking_토큰은_404():
    client = _client()
    res = _get(client, "/ask/없는토큰xyz")
    assert res.status == 404


def _ws_demo_app() -> tuple[TestClient, WebSocketDispatcher]:
    """WebSocketDispatcher를 주입한 데모 앱 — 분산 회수(dispatched→retrieve) 검증용.

    워커 연결 없이 dispatch하면 AwaitingWorker(dispatched) → tracking 발급. 디스패처에서
    직접 claim/submit해 워커 회신을 시뮬한 뒤 GET /ask/{tracking}으로 answered를 받는다.
    """
    ws = WebSocketDispatcher(clock=_fixed_clock)
    app: FastAPI = create_app(runtime=StubRuntime(), dispatcher=ws)
    return TestClient(app), ws


def test_회수_dispatched_후_워커_회신하면_answered로_조회된다():
    client, ws = _ws_demo_app()

    # "환불" → cs_ops(owner cs_lead)로 Routed, 워커 미연결이라 dispatched(tracking).
    asked = _post(client, "/ask", {"question": "환불 되나요?"})
    assert asked.status == 200
    assert asked.body["type"] == "pending"
    assert asked.body["kind"] == "dispatched"
    tracking: str = asked.body["tracking"]
    assert tracking

    # 회신 전: 조회하면 아직 dispatched(같은 토큰).
    before = _get(client, f"/ask/{tracking}")
    assert before.status == 200
    assert before.body["type"] == "pending"
    assert before.body["kind"] == "dispatched"

    # 워커가 (claim 후) 회신 — 디스패처에서 직접 시뮬(2b-i: 실 워커 프로세스는 2b-ii).
    ticket = ws.claim("cs_lead")
    assert ticket is not None
    ws.submit(ticket.ticket_id, Answer(text="환불 가능합니다", sources=("위키/환불정책",), mode="full"))

    # 회신 후: 같은 토큰으로 조회하면 answered(담당·출처).
    after = _get(client, f"/ask/{tracking}")
    assert after.status == 200
    assert after.body["type"] == "answered"
    assert after.body["text"] == "환불 가능합니다"
    assert after.body["answered_by"]["agent_id"] == "cs_ops"
    assert "위키/환불정책" in after.body["sources"]


def test_회수_응답에_라우팅_내부값이_새지_않는다():
    client, ws = _ws_demo_app()

    asked = _post(client, "/ask", {"question": "환불 되나요?"})
    tracking: str = asked.body["tracking"]

    # dispatched 조회 응답: _LEAKY_KEYS 미노출.
    before: dict[str, Any] = _get(client, f"/ask/{tracking}").body
    assert _LEAKY_KEYS.isdisjoint(set(before.keys()))

    # 회신 후 answered 조회 응답: _LEAKY_KEYS 미노출(중첩 dict 포함).
    ticket = ws.claim("cs_lead")
    assert ticket is not None
    ws.submit(ticket.ticket_id, Answer(text="답", sources=(), mode="full"))
    after: dict[str, Any] = _get(client, f"/ask/{tracking}").body
    assert _LEAKY_KEYS.isdisjoint(set(after.keys()))
    for value in after.values():
        if isinstance(value, dict):
            nested: set[str] = set(value.keys())  # pyright: ignore[reportUnknownArgumentType]
            assert _LEAKY_KEYS.isdisjoint(nested)


def test_회수_tracking_토큰은_ticket_id를_노출하지_않는다():
    """불투명성: 사용자向 tracking 토큰에 내부 ticket_id가 인코딩돼 있지 않다."""
    client, ws = _ws_demo_app()

    asked = _post(client, "/ask", {"question": "환불 되나요?"})
    tracking: str = asked.body["tracking"]

    # 디스패처가 보관한 실제 ticket_id가 사용자 토큰에 들어 있지 않아야 한다.
    ticket = ws.claim("cs_lead")
    assert ticket is not None
    assert ticket.ticket_id not in tracking
    assert ticket.owner_id not in tracking


# ── T9.1(d) 세션 와이어링 통합 (web 레벨) ────────────────────────────
#
# web.py 의 `/ask`가 `_session_ask.handle`로 교체됐음을 HTTP 왕복으로 직접 못 박는다.
# 래핑 전후 OrgReply 동일(노출 불변식)·세션값 미노출을 엔드포인트 레벨에서 검증.


def test_web_ask_세션_래퍼_통과_후_응답이_Answered_직렬화와_동일하다():
    """/ask 가 SessionAskOrg 를 거쳐도 응답 구조가 래핑 전후 동일하다(노출 불변식).

    - type="answered"·answered_by·mode·sources 직렬화 형태 보존.
    - session_id·transcript 등 세션 내부값이 응답에 없다.
    """
    client = _client()

    result = _post(client, "/ask", {"question": "계약서 검토해줄 수 있어?"})

    assert result.status == 200
    body: dict[str, Any] = result.body
    assert body["type"] == "answered"
    assert body["answered_by"]["agent_id"] == "contract_ops"
    assert body["mode"] == "full"
    # 세션 내부값이 응답에 새지 않는다.
    session_leaky = {"session_id", "transcript", "started_at", "last_active_at"}
    assert session_leaky.isdisjoint(set(body.keys()))


def test_web_ask_연속_요청이_모두_정상_응답을_반환한다():
    """같은 앱 인스턴스(= 같은 _session_store)로 2회 POST /ask 가 모두 200.

    세션 층이 두 번째 호출에도 기존 세션을 재사용하며 응답 구조를 망가뜨리지 않음을 확인.
    """
    client = _client()

    first = _post(client, "/ask", {"question": "계약서 검토해줄 수 있어?"})
    second = _post(client, "/ask", {"question": "계약 기간은 얼마나 되나요?"})

    assert first.status == 200
    assert second.status == 200
    assert first.body["type"] == "answered"
    assert second.body["type"] == "answered"
    # 두 응답 모두 세션 내부값 미노출.
    session_leaky = {"session_id", "transcript", "started_at", "last_active_at"}
    assert session_leaky.isdisjoint(set(first.body.keys()))
    assert session_leaky.isdisjoint(set(second.body.keys()))


# ── Phase 9 익명 세션 쿠키 user_id 와이어링 (ADR 0024 결정 A) ──────────────


def _cookie_client(store: InMemorySessionStore) -> TestClient:
    """주입된 store 를 관찰 seam으로 쓰는 TestClient."""
    app: FastAPI = create_app(runtime=StubRuntime(), session_store=store)
    return TestClient(app, raise_server_exceptions=True)


def _post_cookies(client: TestClient, url: str, payload: dict[str, Any]) -> Response:
    http: Any = client
    return cast(Response, http.post(url, json=payload))


def test_쿠키없는_첫요청에_Set_Cookie가_응답헤더에_온다():
    """쿠키 없이 POST /ask → 응답에 aon_uid Set-Cookie 헤더 존재."""
    store = InMemorySessionStore()
    client = _cookie_client(store)

    res = _post_cookies(client, "/ask", {"question": "계약서 검토해줄 수 있어?"})

    assert res.status_code == 200
    set_cookie_header: str = res.headers.get("set-cookie", "")
    assert "aon_uid=" in set_cookie_header


def test_쿠키없는_첫요청_후_store에_세션_1개_생성된다():
    """첫 POST /ask(쿠키 없음) → store.active_for_user(uid)가 None 이 아니다."""
    store = InMemorySessionStore()
    client = _cookie_client(store)

    res = _post_cookies(client, "/ask", {"question": "계약서 검토해줄 수 있어?"})

    assert res.status_code == 200
    # Set-Cookie 에서 uid 추출
    set_cookie_header: str = res.headers.get("set-cookie", "")
    uid_part = next(p for p in set_cookie_header.split(";") if "aon_uid=" in p)
    uid = uid_part.split("=", 1)[1].strip()

    session = store.active_for_user(uid)
    assert session is not None
    assert session.user_id == uid


def test_같은_쿠키_재요청은_같은_세션을_쓴다():
    """동일 aon_uid 쿠키로 2회 POST /ask → active_for_user 결과 session_id 동일."""
    store = InMemorySessionStore()
    client = _cookie_client(store)

    # 첫 요청 — 쿠키 없음, uid 발급
    res1 = _post_cookies(client, "/ask", {"question": "계약서 검토해줄 수 있어?"})
    set_cookie_header: str = res1.headers.get("set-cookie", "")
    uid_part = next(p for p in set_cookie_header.split(";") if "aon_uid=" in p)
    uid = uid_part.split("=", 1)[1].strip()

    session_after_first = store.active_for_user(uid)
    assert session_after_first is not None
    sid1 = session_after_first.session_id

    # 두 번째 요청 — 같은 uid 쿠키 전송
    http: Any = client
    res2 = cast(Response, http.post("/ask", json={"question": "계약 기간은?"}, cookies={"aon_uid": uid}))
    assert res2.status_code == 200

    session_after_second = store.active_for_user(uid)
    assert session_after_second is not None
    sid2 = session_after_second.session_id

    assert sid1 == sid2, "같은 쿠키 → 같은 세션이어야 한다"


def test_다른_쿠키_브라우저는_다른_세션을_만든다():
    """브라우저 A(uid_a)·브라우저 B(uid_b) → 각자 별개 세션(session_id 다름)."""
    store = InMemorySessionStore()
    client = _cookie_client(store)

    http: Any = client
    uid_a = "browser-a-uid-00001"
    uid_b = "browser-b-uid-00002"

    res_a = cast(Response, http.post("/ask", json={"question": "계약서 검토해줄 수 있어?"}, cookies={"aon_uid": uid_a}))
    res_b = cast(Response, http.post("/ask", json={"question": "환불 되나요?"}, cookies={"aon_uid": uid_b}))

    assert res_a.status_code == 200
    assert res_b.status_code == 200

    session_a = store.active_for_user(uid_a)
    session_b = store.active_for_user(uid_b)

    assert session_a is not None
    assert session_b is not None
    assert session_a.session_id != session_b.session_id, "다른 쿠키 → 다른 세션이어야 한다"
    assert session_a.user_id == uid_a
    assert session_b.user_id == uid_b


def test_쿠키값은_불투명_user_id만_담는다_라우팅구조_미노출():
    """Set-Cookie 값이 조직 내부값(agent_id·routing_rules·session_id 등)을 포함하지 않는다."""
    store = InMemorySessionStore()
    client = _cookie_client(store)

    res = _post_cookies(client, "/ask", {"question": "계약서 검토해줄 수 있어?"})

    set_cookie_header: str = res.headers.get("set-cookie", "")
    # 쿠키 값(불투명 uid)에 내부 식별자가 안 들어 있다
    internal_keys = ["contract_ops", "legal_lead", "session_id", "routing", "candidate"]
    for key in internal_keys:
        assert key not in set_cookie_header, f"{key!r}가 쿠키 헤더에 노출됨"


def test_ask_응답_body는_쿠키_이전과_동일하다_기존_회귀():
    """쿠키 도입 후에도 /ask 응답 body 구조가 무변경(기존 test 회귀 0 확인)."""
    store = InMemorySessionStore()
    client = _cookie_client(store)

    result = _post_cookies(client, "/ask", {"question": "계약서 검토해줄 수 있어?"})

    assert result.status_code == 200
    body: dict[str, Any] = result.json()
    assert body["type"] == "answered"
    assert body["answered_by"]["agent_id"] == "contract_ops"
    assert body["mode"] == "full"
    # 세션·쿠키 내부값이 응답 body에 없다
    leaky = {"session_id", "transcript", "aon_uid", "user_id", "cookie"}
    assert leaky.isdisjoint(set(body.keys()))


# ── POST /ask/stream — SSE 토큰 스트리밍 엔드포인트(ADR 0031 결정 2·3·5 게이트 밖 배선) ──
#
# StubStreamingRuntime 주입(결정론 델타열) + LocalStreamingDispatcher(데모 기본) → meta→token*→done
# SSE 프레임이 결정론으로 흐른다. 실 claude -p subprocess 스트리밍은 게이트 밖(수동 시연).


def _parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE 응답 본문(`event: <type>\\ndata: <json>\\n\\n` 반복)을 (type, payload) 목록으로 파싱."""
    import json

    frames: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name = ""
        data_json = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_json = line[len("data: ") :]
        frames.append((event_name, json.loads(data_json)))
    return frames


def _stream_app(deltas: tuple[str, ...] | None = None) -> TestClient:
    runtime = StubStreamingRuntime(deltas=deltas)
    app: FastAPI = create_app(runtime=runtime)
    return TestClient(app, raise_server_exceptions=True)


def test_ask_stream_라우트가_등록된다():
    app = create_app(runtime=StubStreamingRuntime())
    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/ask/stream" in routes


def test_ask_stream_meta_token_done_순서로_흐른다():
    client = _stream_app(deltas=("환불은 ", "7일 이내 ", "가능합니다."))
    http: Any = client
    res = cast(Response, http.post("/ask/stream", json={"question": "환불 되나요?"}))

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(res.text)
    names = [name for name, _ in frames]

    assert names[0] == "meta"
    assert names[-1] == "done"
    token_texts = [payload["text"] for name, payload in frames if name == "token"]
    assert token_texts == ["환불은 ", "7일 이내 ", "가능합니다."]


def test_ask_stream_meta는_담당과_출처를_싣는다():
    client = _stream_app()
    http: Any = client
    res = cast(Response, http.post("/ask/stream", json={"question": "환불 되나요?"}))

    frames = _parse_sse(res.text)
    meta = next(payload for name, payload in frames if name == "meta")
    # 담당(owner·agent_id)·mode·sources — 노출 투영(내부값 0)
    assert set(meta["answered_by"].keys()) == {"owner", "agent_id"}
    assert "mode" in meta
    assert "sources" in meta


def test_ask_stream_token에_내부값이_새지_않는다():
    # 노출 불변식: token 프레임은 텍스트 델타만(answered_by·mode·sources·confidence 0).
    client = _stream_app()
    http: Any = client
    res = cast(Response, http.post("/ask/stream", json={"question": "환불 되나요?"}))

    frames = _parse_sse(res.text)
    for name, payload in frames:
        if name == "token":
            assert set(payload.keys()) == {"text"}


def test_ask_stream_쿠키없는_첫요청에_Set_Cookie가_온다():
    client = _stream_app()
    http: Any = client
    res = cast(Response, http.post("/ask/stream", json={"question": "환불 되나요?"}))

    set_cookie_header: str = res.headers.get("set-cookie", "")
    assert "aon_uid=" in set_cookie_header


def test_ask_stream_프록시_버퍼링_방지_헤더가_있다():
    client = _stream_app()
    http: Any = client
    res = cast(Response, http.post("/ask/stream", json={"question": "환불 되나요?"}))

    assert res.headers.get("cache-control") == "no-cache"
    assert res.headers.get("x-accel-buffering") == "no"


def test_ask_stream_런타임_예외는_error_프레임으로_투영된다():
    # 노출 불변식: 런타임 예외·스택은 절대 노출하지 않고 중립 error 프레임 1개만.
    class _BoomStreamingRuntime:
        def answer_stream(self, question: str, card: Any, context: str | None = None) -> Any:
            raise RuntimeError("내부 스택 절대 노출 금지 BOOM")
            yield  # pragma: no cover

        def answer(self, question: str, card: Any, context: str | None = None) -> Answer:
            raise RuntimeError("내부 스택 절대 노출 금지 BOOM")

    app: FastAPI = create_app(runtime=cast(Any, _BoomStreamingRuntime()))
    client = TestClient(app, raise_server_exceptions=True)
    http: Any = client
    res = cast(Response, http.post("/ask/stream", json={"question": "환불 되나요?"}))

    assert res.status_code == 200
    frames = _parse_sse(res.text)
    error_frames = [payload for name, payload in frames if name == "error"]
    assert len(error_frames) == 1
    # 중립 안내만 — 내부 예외 메시지·스택 0
    assert "BOOM" not in res.text
    assert "RuntimeError" not in res.text
    assert "Traceback" not in res.text
