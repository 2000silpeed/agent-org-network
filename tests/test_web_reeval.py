"""/inbox 재평가(reeval) 탭 web 배선 — Owner 처리함 세 번째 탭(둘째 탭 미러).

ADR 0019 결정 5(Owner Inbox 세 번째 탭). reeval 도메인(`reeval.py`)은 완성돼 있고,
여기선 web 라우트(GET `/inbox/reeval`·POST `/reeval/{item_id}/review`)·`serialize_reeval_item`·
데모 시드를 *둘째 탭 `BackupReviewStore`* 배선과 동형으로 검증한다.

결정론: TestClient 세션(쿠키 유지)·고정 session_secret·고정 clock·시드 주입. 실 LLM·
실 OKF 커밋·StalenessPropagator 자동 적재 0(시드로 가시성 확보 — 실 자동 경로는 이미 설계됨).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from starlette.testclient import WebSocketTestSession

from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.knowledge_index import Concept, KnowledgeIndex
from agent_org_network.reeval import (
    AnswerSubject,
    InMemoryReevalStore,
    KeepPrecedent,
    PrecedentSubject,
    ReevalItem,
    ReevalService,
)
from agent_org_network.registry import Registry
from agent_org_network.runtime import StubRuntime
from agent_org_network.transport import PublishIndex
from agent_org_network.web import create_app, serialize_reeval_item

_SECRET = "test-secret"
_NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
_DATE = date(2026, 6, 20)


def _fixed_clock() -> datetime:
    return _NOW


# ── serialize_reeval_item 단위(두 subject 모양) ──────────────────────────────


def _precedent_item(owner_id: str = "cs_lead") -> ReevalItem:
    return ReevalItem(
        subject=PrecedentSubject(intent="환불"),
        owner_id=owner_id,
        agent_id="cs_ops",
        trigger_sha="abcdef0123456789aaaa",
        flagged_at=_NOW,
        item_id="reeval-precedent-001",
    )


def _answer_item(owner_id: str = "cs_lead", audit_index: int = 0) -> ReevalItem:
    return ReevalItem(
        subject=AnswerSubject(audit_index=audit_index),
        owner_id=owner_id,
        agent_id="cs_ops",
        trigger_sha="abcdef0123456789bbbb",
        flagged_at=_NOW,
        item_id="reeval-answer-001",
    )


def test_serialize_reeval_item_precedent_subject_모양() -> None:
    d = serialize_reeval_item(_precedent_item(), Registry(), None)

    assert d["item_id"] == "reeval-precedent-001"
    assert d["owner_id"] == "cs_lead"
    assert d["agent_id"] == "cs_ops"
    assert d["subject_kind"] == "precedent"
    assert d["subject_ref"] == "환불"
    assert d["trigger_sha"] == "abcdef012345"  # 앞 12자
    assert d["flagged_at"] == _NOW.isoformat()
    assert d["status"] == "pending_review"
    assert "환불" in d["question"]
    assert "cs_ops" in d["reason"]
    assert d["review"] is None


def test_serialize_reeval_item_answer_subject_모양() -> None:
    # audit_reader가 None인 폴백 라벨을 검증한다(record_at 범위 밖 안전 폴백).
    d = serialize_reeval_item(_answer_item(audit_index=0), Registry(), None)

    assert d["item_id"] == "reeval-answer-001"
    assert d["subject_kind"] == "answer"
    assert d["subject_ref"] == "0"
    assert d["status"] == "pending_review"
    # audit_reader None → 폴백 라벨.
    assert "0" in d["question"]
    assert d["review"] is None


def test_serialize_reeval_item_audit_reader로_question_파생() -> None:
    from agent_org_network.agent_card import AgentCard
    from agent_org_network.audit import AuditEntry
    from agent_org_network.router import Routed

    card = AgentCard(
        agent_id="cs_ops",
        owner="cs_lead",
        team="cs",
        summary="환불 처리",
        domains=["환불"],
        last_reviewed_at=_DATE,
    )
    audit = InMemoryAuditLog()
    audit.record(
        AuditEntry(
            timestamp=_NOW,
            user_id="web_guest",
            question="환불 기간이 어떻게 되나요?",
            intent="환불",
            decision=Routed(
                primary=card,
                confidence=0.9,
                reason="r",
            ),
        )
    )
    d = serialize_reeval_item(_answer_item(audit_index=0), Registry(), audit)

    assert d["question"] == "환불 기간이 어떻게 되나요?"


def test_serialize_reeval_item_reviewed면_review_직렬화() -> None:
    item = _precedent_item().review_with(KeepPrecedent(by_owner="cs_lead", rationale="유지"))
    d = serialize_reeval_item(item, Registry(), None)

    assert d["status"] == "reviewed"
    assert d["review"] is not None
    assert d["review"]["kind"] == "keep"
    assert d["review"]["by_owner"] == "cs_lead"
    assert d["review"]["rationale"] == "유지"


# ── 앱 팩토리(둘째 탭 미러) ───────────────────────────────────────────────────


def _auth_app_with_reeval() -> tuple[FastAPI, InMemoryReevalStore]:
    """ReevalStore가 있는 인증 앱(둘째 탭 `_auth_app_with_review` 미러)."""
    reeval_store = InMemoryReevalStore()
    reeval_svc = ReevalService(reeval_store, clock=_fixed_clock)
    app = create_app(
        runtime=StubRuntime(),
        session_secret=_SECRET,
        reeval_store=reeval_store,
        reeval_service=reeval_svc,
    )
    return app, reeval_store


def _result(res: Response) -> tuple[int, Any]:
    return res.status_code, res.json()


def _get(client: TestClient, url: str) -> tuple[int, Any]:
    http: Any = client
    return _result(cast(Response, http.get(url)))


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    http: Any = client
    return _result(cast(Response, http.post(url, json=payload)))


def _login(client: TestClient, user_id: str) -> None:
    http: Any = client
    http.post("/login", json={"user_id": user_id})


# ── GET /inbox/reeval (둘째 탭 GET 미러) ─────────────────────────────────────


def test_미로그인_inbox_reeval은_401() -> None:
    app, _ = _auth_app_with_reeval()
    client = TestClient(app)
    assert _get(client, "/inbox/reeval")[0] == 401


def test_세션_owner의_시드_항목_반환() -> None:
    app, store = _auth_app_with_reeval()
    store.add(_precedent_item(owner_id="cs_lead"))
    client = TestClient(app)
    _login(client, "cs_lead")

    status, body = _get(client, "/inbox/reeval")
    assert status == 200
    assert len(body) == 1
    assert body[0]["owner_id"] == "cs_lead"
    assert body[0]["subject_kind"] == "precedent"
    assert body[0]["subject_ref"] == "환불"


def test_다른_owner는_빈_목록() -> None:
    app, store = _auth_app_with_reeval()
    store.add(_precedent_item(owner_id="cs_lead"))
    client = TestClient(app)
    _login(client, "legal_lead")

    status, body = _get(client, "/inbox/reeval")
    assert status == 200
    assert body == []


def test_store_None이면_빈_목록() -> None:
    # reeval_store 미주입이면 GET은 빈 목록(둘째 탭 review_store None 미러).
    app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
    client = TestClient(app)
    _login(client, "cs_lead")
    status, body = _get(client, "/inbox/reeval")
    assert status == 200
    assert body == []


# ── POST /reeval/{item_id}/review (둘째 탭 POST 미러) ─────────────────────────


def test_review_전이_pending에서_reviewed() -> None:
    app, store = _auth_app_with_reeval()
    store.add(_precedent_item(owner_id="cs_lead"))
    client = TestClient(app)
    _login(client, "cs_lead")

    status, body = _post(
        client,
        "/reeval/reeval-precedent-001/review",
        {"kind": "keep", "rationale": "유지"},
    )
    assert status == 200
    assert body["status"] == "reviewed"
    assert body["review"]["kind"] == "keep"

    # 재조회 시 목록에서 빠진다(pending만).
    assert _get(client, "/inbox/reeval")[1] == []


def test_review_supersede는_new_primary_필수() -> None:
    app, store = _auth_app_with_reeval()
    store.add(_precedent_item(owner_id="cs_lead"))
    client = TestClient(app)
    _login(client, "cs_lead")

    # new_primary 없으면 400.
    bad = _post(client, "/reeval/reeval-precedent-001/review", {"kind": "supersede"})
    assert bad[0] == 400

    status, body = _post(
        client,
        "/reeval/reeval-precedent-001/review",
        {"kind": "supersede", "new_primary": "finance_ops", "rationale": "갈음"},
    )
    assert status == 200
    assert body["review"]["kind"] == "supersede"
    assert body["review"]["new_primary"] == "finance_ops"


def test_review_answer_축_outcome() -> None:
    app, store = _auth_app_with_reeval()
    store.add(_answer_item(owner_id="cs_lead", audit_index=0))
    client = TestClient(app)
    _login(client, "cs_lead")

    status, body = _post(
        client,
        "/reeval/reeval-answer-001/review",
        {"kind": "reanswer", "rationale": "다시"},
    )
    assert status == 200
    assert body["review"]["kind"] == "reanswer"


def test_스코프_위반_403() -> None:
    app, store = _auth_app_with_reeval()
    store.add(_precedent_item(owner_id="cs_lead"))
    client = TestClient(app)
    _login(client, "legal_lead")  # 남의 항목

    status, _ = _post(
        client, "/reeval/reeval-precedent-001/review", {"kind": "keep"}
    )
    assert status == 403


def test_미존재_item_404() -> None:
    app, _ = _auth_app_with_reeval()
    client = TestClient(app)
    _login(client, "cs_lead")

    status, _ = _post(client, "/reeval/nonexistent/review", {"kind": "keep"})
    assert status == 404


def test_store_None이면_review_404() -> None:
    app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
    client = TestClient(app)
    _login(client, "cs_lead")
    status, _ = _post(client, "/reeval/any/review", {"kind": "keep"})
    assert status == 404


def test_미로그인_review는_401() -> None:
    app, store = _auth_app_with_reeval()
    store.add(_precedent_item(owner_id="cs_lead"))
    client = TestClient(app)
    status, _ = _post(
        client, "/reeval/reeval-precedent-001/review", {"kind": "keep"}
    )
    assert status == 401


# ── 데모 시드(다툼 케이스 시드 메커니즘 미러 — build_demo 번들) ────────────────


def test_build_demo가_reeval_store를_담는다() -> None:
    from agent_org_network.demo import build_demo

    bundle = build_demo(runtime=StubRuntime(), audit_log=InMemoryAuditLog())
    assert bundle.reeval_store is not None
    assert bundle.reeval_service is not None


def test_인증_OFF_레거시_path_reeval() -> None:
    """인증 OFF(데모 모드)면 `/inbox/{owner_id}/reeval` 레거시 path로 조회(둘째 탭 미러)."""
    reeval_store = InMemoryReevalStore()
    reeval_svc = ReevalService(reeval_store, clock=_fixed_clock)
    reeval_store.add(_precedent_item(owner_id="cs_lead"))
    app = create_app(
        runtime=StubRuntime(),
        reeval_store=reeval_store,
        reeval_service=reeval_svc,
    )  # session_secret 미주입 = 인증 OFF
    client = TestClient(app)

    status, body = _get(client, "/inbox/cs_lead/reeval")
    assert status == 200
    assert len(body) == 1
    assert body[0]["owner_id"] == "cs_lead"
    # 다른 owner는 빈 목록.
    assert _get(client, "/inbox/legal_lead/reeval")[1] == []


def test_seed_demo_reeval_items가_cs_lead_항목을_만든다() -> None:
    """데모 시드 헬퍼가 cs_lead 처리함 재평가 탭 항목(precedent·answer)을 만든다.

    다툼 케이스가 데모에서 '질문 흐름'으로 생기듯, reeval은 StalenessPropagator 자동
    경로가 데모 흐름에 없으므로 명시 시드한다(propagator가 만들 shape와 동형). owner-스코프라
    cs_lead 것만.
    """
    from agent_org_network.demo import seed_demo_reeval_items

    store = InMemoryReevalStore()
    seed_demo_reeval_items(store, clock=_fixed_clock)

    cs_items = store.pending_for_owner("cs_lead")
    assert len(cs_items) >= 1
    kinds = {type(it.subject).__name__ for it in cs_items}
    assert "PrecedentSubject" in kinds
    # 다른 owner는 시드 없음(owner-스코프 가시성).
    assert store.pending_for_owner("legal_lead") == []


def test_create_central_app이_reeval_시드를_세션_owner에_노출() -> None:
    """create_central_app(데모 진입점·인증 ON)이 cs_lead 처리함 재평가 탭에 시드를 노출한다."""
    from agent_org_network.server import create_central_app

    app = create_central_app(session_secret=_SECRET)
    client = TestClient(app)
    _login(client, "cs_lead")

    status, body = _get(client, "/inbox/reeval")
    assert status == 200
    assert len(body) >= 1
    assert all(it["owner_id"] == "cs_lead" for it in body)


# ── T11.7e E1 — 실 WS publish 경로가 reeval을 실제로 채운다(라이브 배선 통합) ────


def _recv(conn: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], conn.receive_json())


def test_create_central_app_실_WS_publish가_reeval에_실제로_적재된다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실 `/worker` WS 경로로 두 차례 publish → 두 번째가 첫 답을 stale로 걸어 reeval 적재.

    T11.7e E1 배선(WebSocketDispatcher.accept_index → propagator 전달, create_central_app이
    만든 실 StalenessPropagator 주입)이 없으면 dispatcher._propagator가 None이라 발화 자체가
    0회 — 이 테스트는 red(reeval 항목 0개)로 그 결함을 드러낸다.

    시나리오: register → 1차 publish(라우팅 시드) → /ask로 답 확보(audit에 routed 기록 생성,
    snapshot_sha=None) → 2차 publish(더 새 generated_at, 같은 agent_id) → Answer 축 과검출로
    그 답이 stale 판정 → reeval_store에 AnswerSubject 적재 → cs_lead 세션의 /inbox/reeval에서
    실제로 조회된다.
    """
    from agent_org_network.server import create_central_app

    monkeypatch.setenv("AON_ROUTER", "index")
    app = create_central_app(session_secret=_SECRET)
    client = TestClient(app)
    http: Any = client

    t1 = datetime(2026, 6, 29, 0, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 29, 1, 0, 0, tzinfo=timezone.utc)  # t1보다 더 새 것

    with http.websocket_connect("/worker") as conn:
        ws = cast(WebSocketTestSession, conn)
        ws.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        assert _recv(ws)["type"] == "welcome"

        # 1차 publish — 라우팅용 시드(첫 수용이라 발화는 하지만 아직 답이 없어 Answer 축 무영향).
        first = KnowledgeIndex(
            agent_id="cs_ops",
            version="okf-1",
            generated_at=t1,
            concepts=(
                Concept(id="refund", label="환불 규정", core_question="환불 규정 안내", domain="환불"),
            ),
        )
        ws.send_json(PublishIndex(index=first).model_dump(mode="json"))
        ws.send_json({"type": "heartbeat"})  # 송신/수신 루프 왕복 펜스(응답 없음, echo 안 함)

        # 질문 → cs_ops로 라우팅 → 워커가 답 회신(snapshot_sha 미실음 → None).
        r = http.post("/ask", json={"question": "환불 규정 알려줘"})
        assert r.status_code == 200
        tracking = r.json()["tracking"]

        push = _recv(ws)
        assert push["type"] == "push_work"
        ticket_id = push["ticket"]["ticket_id"]
        assert push["ticket"]["agent_id"] == "cs_ops"

        ws.send_json(
            {
                "type": "submit_answer",
                "ticket_id": ticket_id,
                "answer": {"text": "환불은 7일 이내", "sources": [], "mode": "full"},
            }
        )
        ws.send_json({"type": "heartbeat"})  # 펜스(응답 없음)

        answered = http.get(f"/ask/{tracking}").json()
        assert answered["type"] == "answered"

        # 2차 publish(더 새 generated_at) — 이 수용이 방금 확정된 답을 stale로 건다.
        second = KnowledgeIndex(
            agent_id="cs_ops",
            version="okf-2",
            generated_at=t2,
            concepts=(
                Concept(id="refund-v2", label="환불 규정 v2", core_question="환불 규정 v2", domain="환불"),
            ),
        )
        ws.send_json(PublishIndex(index=second).model_dump(mode="json"))
        ws.send_json({"type": "heartbeat"})  # 펜스(응답 없음) — 2차 publish 처리 완료 보장

    _login(client, "cs_lead")
    status, body = _get(client, "/inbox/reeval")
    assert status == 200
    answer_items = [it for it in body if it["subject_kind"] == "answer"]
    assert len(answer_items) >= 1
    assert all(it["owner_id"] == "cs_lead" for it in answer_items)


# ── T11.7e minor-1 — Precedent 축 라이브 배선(precedents 공유·owner_of) ─────────


def test_create_central_app_실_판례_합의_후_재publish가_precedent_reeval에_적재된다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실 다툼→합의(`/cases/{case_id}/concur`)로 만든 판례가 재publish로 stale 표식된다.

    T11.7e minor-1이 닫는 두 결함을 이 한 테스트가 동시에 실증한다:
      ① precedents 공유 — `create_central_app`이 만들던 빈 새 `InMemoryPrecedentStore()`
         였다면 `/cases/.../concur`로 실제 `build_demo`의 `bundle.precedents`에 기록된
         판례를 `find_by_primary`가 *영원히 못 찾는다*(별개 인스턴스라 항상 빈 결과).
      ② owner_of 배선 — 없었다면 ReevalItem.owner_id가 빈 문자열("")로 적재돼 owner
         `/inbox/reeval`에 안 뜬다. 이 테스트는 owner_id가 실제 owner("cs_lead")로
         채워지는지(빈 문자열 아님) 직접 단언한다.

    시나리오: cs_lead·finance_lead WS 연결 각각 register → 같은 domain("보상") concept을
    가진 인덱스를 cs_ops·finance_ops로 publish(1차, 다툼 유도) → "보상" 질문 → Router가
    둘 다 매칭 → Contested(케이스 열림) → 두 owner 모두 concur(cs_ops 지목) → 전원 합의 →
    Agreed → 실 `precedents.record(Resolution(intent="보상", primary="cs_ops"))` →
    cs_ops 재publish(2차, 더 새 generated_at) → 그 판례가 stale 표식 + ReevalItem
    (subject=PrecedentSubject·owner_id="cs_lead") 적재 → cs_lead 세션의 `/inbox/reeval`에서
    실제로 조회된다.
    """
    from agent_org_network.server import create_central_app

    monkeypatch.setenv("AON_ROUTER", "index")
    app = create_central_app(session_secret=_SECRET)
    client = TestClient(app)
    http: Any = client

    t1 = datetime(2026, 6, 30, 0, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 30, 1, 0, 0, tzinfo=timezone.utc)  # t1보다 더 새 것

    with http.websocket_connect("/worker") as cs_conn, http.websocket_connect(
        "/worker"
    ) as finance_conn:
        cs_ws = cast(WebSocketTestSession, cs_conn)
        finance_ws = cast(WebSocketTestSession, finance_conn)

        cs_ws.send_json(
            {"type": "register_worker", "owner_id": "cs_lead", "role": "primary"}
        )
        assert _recv(cs_ws)["type"] == "welcome"
        finance_ws.send_json(
            {"type": "register_worker", "owner_id": "finance_lead", "role": "primary"}
        )
        assert _recv(finance_ws)["type"] == "welcome"

        # 같은 domain("보상")을 가진 인덱스를 각자 publish — 다툼(≥2 authorized) 유도.
        # domain_authorized는 카드의 domains 필드만 보므로(cs_ops·finance_ops 둘 다
        # "보상"을 domains에 가짐, demo.py) OKF 파일 내용과 무관하게 안전하게 통과한다.
        cs_index = KnowledgeIndex(
            agent_id="cs_ops",
            version="okf-1",
            generated_at=t1,
            concepts=(
                Concept(
                    id="compensation",
                    label="보상 기준",
                    core_question="보상 기준 안내",
                    domain="보상",
                ),
            ),
        )
        cs_ws.send_json(PublishIndex(index=cs_index).model_dump(mode="json"))
        cs_ws.send_json({"type": "heartbeat"})  # 펜스(응답 없음)

        finance_index = KnowledgeIndex(
            agent_id="finance_ops",
            version="okf-1",
            generated_at=t1,
            concepts=(
                Concept(
                    id="compensation-finance",
                    label="보상 규정(재무)",
                    core_question="보상 규정 안내",
                    domain="보상",
                ),
            ),
        )
        finance_ws.send_json(
            PublishIndex(index=finance_index).model_dump(mode="json")
        )
        finance_ws.send_json({"type": "heartbeat"})  # 펜스(응답 없음)

        # "보상" 질문 → cs_ops·finance_ops 둘 다 매칭 → Contested(케이스 열림, 판례 아직 없음).
        r = http.post("/ask", json={"question": "보상 기준이 어떻게 되나요?"})
        assert r.status_code == 200
        assert r.json()["type"] == "pending"
        assert r.json()["kind"] == "contested"

        # cs_lead·finance_lead 둘 다 cs_ops를 primary로 지목 → 전원 합의 → Agreed →
        # 실 precedents.record(Resolution(intent="보상", primary="cs_ops")).
        _login(client, "cs_lead")
        cases_status, cases_body = _get(client, "/inbox/cases")
        assert cases_status == 200
        assert len(cases_body) == 1
        case_id = cases_body[0]["case_id"]

        concur_status, concur_body = _post(
            client, f"/cases/{case_id}/concur", {"on_agent": "cs_ops"}
        )
        assert concur_status == 200
        assert concur_body["type"] == "still_open"

        _login(client, "finance_lead")
        concur_status2, concur_body2 = _post(
            client, f"/cases/{case_id}/concur", {"on_agent": "cs_ops"}
        )
        assert concur_status2 == 200
        assert concur_body2["type"] == "agreed"

        # cs_ops 재publish(더 새 generated_at) — 이 수용이 방금 record된 판례를 stale로 건다.
        cs_index_v2 = KnowledgeIndex(
            agent_id="cs_ops",
            version="okf-2",
            generated_at=t2,
            concepts=(
                Concept(
                    id="compensation-v2",
                    label="보상 기준 v2",
                    core_question="보상 기준 v2",
                    domain="보상",
                ),
            ),
        )
        cs_ws.send_json(PublishIndex(index=cs_index_v2).model_dump(mode="json"))
        cs_ws.send_json({"type": "heartbeat"})  # 펜스(응답 없음) — 2차 publish 처리 완료 보장

    _login(client, "cs_lead")
    status, body = _get(client, "/inbox/reeval")
    assert status == 200
    precedent_items = [it for it in body if it["subject_kind"] == "precedent"]
    assert len(precedent_items) >= 1
    matching = [it for it in precedent_items if it["subject_ref"] == "보상"]
    assert len(matching) == 1
    # 핵심 단언 — owner_id가 실제 owner로 채워지는지(빈 문자열 아님). owner_of 미배선이면
    # 이 항목의 owner_id가 ""라 위 pending_for_owner("cs_lead") 조회 자체에 안 잡혀 이
    # assert 이전에 이미 실패한다(빈 owner_id는 어느 owner 처리함에도 안 뜨므로).
    assert matching[0]["owner_id"] == "cs_lead"
    assert matching[0]["owner_id"] != ""
