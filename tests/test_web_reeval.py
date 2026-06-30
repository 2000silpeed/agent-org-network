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

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.audit import InMemoryAuditLog
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
