"""관리 UI 웹 라우트 — 카드 등록·오너 변경(Phase 12 3라운드·ADR 0034).

TestClient 결정론:
- 등록 성공/무효 거부(422)/중복(409)/미로그인(401).
- 오너 변경 성공/무효 거부(422)/미존재 카드(404)/토큰 revoke 확인/감사 기록/미로그인.
- 라이브 등록→라우팅 반영 종단(/ask로 새 담당이 잡히는지).
- 오너 변경 후 새 owner가 정정 가능·구 owner 거부(정정 판정 교체 종단).
"""

from __future__ import annotations

from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.answer_record import AnswerRecord, InMemoryAnswerRecordStore
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.runtime import StubRuntime
from agent_org_network.token import InMemoryTokenStore
from agent_org_network.web import create_app

_SECRET = "test-secret"
_DATE = "2026-06-20"


def _get(client: TestClient, url: str) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(Response, http.get(url))
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(Response, http.post(url, json=payload))
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _login(client: TestClient, user_id: str) -> None:
    status, _ = _post(client, "/login", {"user_id": user_id})
    assert status == 200, f"로그인 실패: {user_id}"


def _demo_owner(agent_id: str) -> str:
    from agent_org_network.demo import build_demo

    return build_demo(runtime=StubRuntime()).registry.get(agent_id).owner


def _register_payload(**kwargs: Any) -> dict[str, Any]:
    # 데모 유저 6명 중 cs_lead(=cs_ops owner)를 owner로 쓴다.
    defaults: dict[str, Any] = {
        "agent_id": "new_ops",
        "owner": "cs_lead",
        "team": "new",
        "summary": "새 담당",
        "domains": ["신규도메인"],
        "last_reviewed_at": _DATE,
    }
    defaults.update(kwargs)
    return defaults


# ── 신규 카드 등록 라우트 ──────────────────────────────────────────────────


class TestAdminRegister:
    def test_미로그인_401(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        status, _ = _post(client, "/admin/cards", _register_payload())
        assert status == 401

    def test_등록_성공_200(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _post(client, "/admin/cards", _register_payload())
        assert status == 200, body
        assert body["registered"] is True
        assert body["agent_id"] == "new_ops"

    def test_무효_owner_422(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _post(client, "/admin/cards", _register_payload(owner="ghost"))
        assert status == 422
        assert "errors" in body["detail"]

    def test_중복_agent_id_409(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        # cs_ops는 데모에 이미 존재.
        status, _ = _post(client, "/admin/cards", _register_payload(agent_id="cs_ops"))
        assert status == 409

    def test_잘못된_agent_id_형식_422(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, _ = _post(client, "/admin/cards", _register_payload(agent_id="_bad id"))
        assert status == 422

    def test_카드_목록_조회(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _get(client, "/admin/cards")
        assert status == 200
        ids = {c["agent_id"] for c in body}
        assert "cs_ops" in ids


# ── 오너 변경 라우트 ───────────────────────────────────────────────────────


class TestAdminOwnerChange:
    def test_미로그인_401(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        status, _ = _post(client, "/admin/cards/cs_ops/owner", {"new_owner": "hr_lead"})
        assert status == 401

    def test_오너_변경_성공(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        old_owner = _demo_owner("cs_ops")
        status, body = _post(
            client, "/admin/cards/cs_ops/owner", {"new_owner": "hr_lead"}
        )
        assert status == 200, body
        assert body["transferred"] is True
        assert body["from_owner"] == old_owner
        assert body["to_owner"] == "hr_lead"

    def test_무효_새_owner_422_스위치_없음(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, _ = _post(
            client, "/admin/cards/cs_ops/owner", {"new_owner": "ghost"}
        )
        assert status == 422
        # 스위치 없음 — 목록에서 owner 불변 확인.
        _, cards = _get(client, "/admin/cards")
        cs = next(c for c in cards if c["agent_id"] == "cs_ops")
        assert cs["owner"] == _demo_owner("cs_ops")

    def test_미존재_카드_404(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, _ = _post(
            client, "/admin/cards/ghost_ops/owner", {"new_owner": "hr_lead"}
        )
        assert status == 404

    def test_구_owner_토큰_revoke(self) -> None:
        tokens = InMemoryTokenStore()
        app = create_app(
            runtime=StubRuntime(), session_secret=_SECRET, token_store=tokens
        )
        client = TestClient(app)
        _login(client, "root_manager")
        old_owner = _demo_owner("cs_ops")
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        raw_old, tok_old = tokens.issue(old_owner, "primary", now=now)
        status, body = _post(
            client, "/admin/cards/cs_ops/owner", {"new_owner": "hr_lead"}
        )
        assert status == 200
        assert tok_old.token_id in body["revoked_token_ids"]
        assert tokens.verify(raw_old, now=now) is None

    def test_감사_ownership_transfer_기록(self) -> None:
        audit = InMemoryAuditLog()
        app = create_app(
            runtime=StubRuntime(), session_secret=_SECRET, audit_log=audit
        )
        client = TestClient(app)
        _login(client, "root_manager")
        _post(client, "/admin/cards/cs_ops/owner", {"new_owner": "hr_lead"})
        transfers = [
            r for r in audit.records()
            if r.get("action") and r["action"]["kind"] == "OwnershipTransfer"
        ]
        assert len(transfers) == 1
        assert transfers[0]["action"]["to_owner"] == "hr_lead"


# ── 종단: 라이브 등록→라우팅 반영 ──────────────────────────────────────────


class TestLiveRegistrationRouting:
    def test_라이브_등록_카드가_라우팅에_즉시_잡힌다(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        # 등록 전: "환불" 질문은 cs_ops 단독 → Routed(담당 있음·답 나옴).
        _, before = _post(client, "/ask", {"question": "환불 문의"})
        assert before.get("answered_by") is not None
        # "환불" 도메인 카드를 하나 더 라이브 등록(같은 도메인 → 다툼 후보 추가).
        status, _ = _post(
            client,
            "/admin/cards",
            _register_payload(agent_id="refund_ops2", owner="hr_lead", domains=["환불"]),
        )
        assert status == 200
        # 등록 직후 같은 질문이 이제 후보 2건 → Contested(라이브 카드가 라우팅에 즉시 잡힘).
        _, after = _post(client, "/ask", {"question": "환불 문의"})
        assert after.get("kind") == "contested", after


# ── 종단: 오너 변경 후 정정 판정 교체 ──────────────────────────────────────


class TestOwnerChangeCorrection:
    def test_오너_변경_후_새_owner가_정정_가능_구_owner_거부(self) -> None:
        answer_store = InMemoryAnswerRecordStore()
        app = create_app(
            runtime=StubRuntime(),
            session_secret=_SECRET,
            answer_record_store=answer_store,
        )
        client = TestClient(app)
        _login(client, "root_manager")

        from datetime import UTC, datetime

        old_owner = _demo_owner("cs_ops")
        answer_store.add(
            AnswerRecord(
                record_id="rec-cs",
                question="환불?",
                answer_text="원문",
                answered_by=old_owner,
                agent_id="cs_ops",
                mode="full",
                session_id=None,
                answered_at=datetime.now(UTC),
            )
        )
        # 오너 변경 cs_ops → hr_lead.
        _post(client, "/admin/cards/cs_ops/owner", {"new_owner": "hr_lead"})

        # 새 owner(hr_lead) 세션으로 정정 가능(by_owner는 세션에서 취함 — body 무시,
        # code-reviewer M-1: 클라이언트 자기보고 by_owner 신뢰 금지).
        _login(client, "hr_lead")
        status_new, _ = _post(
            client,
            "/supervision/answers/rec-cs/correct",
            {"corrected_text": "정정본", "rationale": ""},
        )
        assert status_new == 200

        # 구 owner 세션으로 정정 거부(403) — 과거 답변자 신원을 body로 실어도(하드코딩
        # 흉내) 세션이 최종 신원이라 구 owner 세션 자체가 거부된다.
        _login(client, old_owner)
        status_old, _ = _post(
            client,
            "/supervision/answers/rec-cs/correct",
            {"by_owner": "hr_lead", "corrected_text": "구owner시도", "rationale": ""},
        )
        assert status_old == 403
