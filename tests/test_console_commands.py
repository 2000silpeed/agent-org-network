"""T9.2(b)·T9.3(b)·T9.5(c) — 운영자 콘솔 POST 명령 라우트 회귀.

각 명령은 도메인 서비스(SessionStore.end·HitlToggleMap.set·TokenStore)를 부르는 얇은
어댑터다(web.py `/console/*`). 검증 축:
  - 운영자 인증(인증 활성 시 미로그인 401·로그인 후 통과 — ADR 0016 결정 5 결).
  - 각 명령 성공·미존재 404.
  - 토큰 발급 응답에 평문 1회, GET /console/tokens엔 해시·평문 미노출.
  - HITL 토글 → /ask 다음 답 mode 반영 결정론(TestClient e2e).
  - 미주입(token_store/hitl_toggles 둘 다 None) 하위호환 — 기존 데모 앱 무회귀.

결정론: `StubRuntime` + 고정 `session_secret`. 실 LLM·실 네트워크 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.hitl import HitlToggleMap
from agent_org_network.runtime import StubRuntime
from agent_org_network.session import InMemorySessionStore
from agent_org_network.token import InMemoryTokenStore
from agent_org_network.web import create_app

_SECRET = "test-secret"


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


def _login(client: TestClient, user_id: str) -> HttpResult:
    return _post(client, "/login", {"user_id": user_id})


# ── 앱 팩토리 ────────────────────────────────────────────────────────────


def _console_app(
    *, auth: bool = True
) -> tuple[FastAPI, InMemoryTokenStore, HitlToggleMap, InMemorySessionStore]:
    token_store = InMemoryTokenStore()
    hitl_toggles = HitlToggleMap()
    session_store = InMemorySessionStore()
    app = create_app(
        runtime=StubRuntime(),
        session_secret=_SECRET if auth else None,
        token_store=token_store,
        hitl_toggles=hitl_toggles,
        session_store=session_store,
    )
    return app, token_store, hitl_toggles, session_store


# ════════════════════════════════════════════════════════════════════════════
# 세션 종료 명령 (T9.2(b) — ADR 0024 결정 4)
# ════════════════════════════════════════════════════════════════════════════


class Test콘솔_세션종료:
    def test_미로그인이면_401(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        r = _post(client, "/console/sessions/does-not-exist/end", {})
        assert r.status == 401

    def test_로그인_후_미존재_세션은_404(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(client, "/console/sessions/does-not-exist/end", {})
        assert r.status == 404

    def test_로그인_후_실재_세션_종료_200(self) -> None:
        app, _, _, session_store = _console_app()
        session = session_store.open_or_get("web_user_1")
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(client, f"/console/sessions/{session.session_id}/end", {})
        assert r.status == 200
        assert r.body["status"] == "ended"

    def test_종료_후_store에_반영된다(self) -> None:
        app, _, _, session_store = _console_app()
        session = session_store.open_or_get("web_user_2")
        client = TestClient(app)
        _login(client, "root_manager")
        _post(client, f"/console/sessions/{session.session_id}/end", {})
        after = session_store.get(session.session_id)
        assert after is not None
        assert after.status == "ended"

    def test_인증_OFF면_로그인없이_통과(self) -> None:
        app, _, _, session_store = _console_app(auth=False)
        session = session_store.open_or_get("web_user_3")
        client = TestClient(app)
        r = _post(client, f"/console/sessions/{session.session_id}/end", {})
        assert r.status == 200


# ════════════════════════════════════════════════════════════════════════════
# HITL 토글 명령 (T9.2(b)·T9.3(b) — ADR 0025)
# ════════════════════════════════════════════════════════════════════════════


class Test콘솔_HITL토글:
    def test_미로그인이면_401(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        r = _post(client, "/console/hitl/contract_ops", {"on": True})
        assert r.status == 401

    def test_미존재_카드는_404(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(client, "/console/hitl/no_such_agent", {"on": True})
        assert r.status == 404

    def test_로그인_후_set_on_200(self) -> None:
        app, _, toggles, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(client, "/console/hitl/contract_ops", {"on": True})
        assert r.status == 200
        assert r.body["on"] is True
        assert toggles.is_on("contract_ops") is True

    def test_get_현재상태_조회(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        _post(client, "/console/hitl/contract_ops", {"on": True})
        r = _get(client, "/console/hitl/contract_ops")
        assert r.status == 200
        assert r.body["on"] is True

    def test_get_미존재_카드는_404(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _get(client, "/console/hitl/no_such_agent")
        assert r.status == 404

    def test_get_명시_set_없으면_카드_시드값(self) -> None:
        """approval_when이 있는 hr_ops는 명시 set 전엔 시드값(True)을 반영한다."""
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _get(client, "/console/hitl/hr_ops")
        assert r.status == 200
        assert r.body["on"] is True

    def test_get_명시_set_없고_approval_when_없으면_False(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _get(client, "/console/hitl/contract_ops")
        assert r.status == 200
        assert r.body["on"] is False


# ════════════════════════════════════════════════════════════════════════════
# HITL 토글과 P17 사용자 경로 분리(P17.6b 전 legacy side effect 없음)
# ════════════════════════════════════════════════════════════════════════════


class Test토글_P17질문경로_분리:
    def test_토글_on이어도_다음_ask는_legacy_draft_only가_아니다(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        _post(client, "/console/hitl/contract_ops", {"on": True})
        r = _post(client, "/ask", {"question": "계약서 검토해줘"})
        assert r.status == 200
        assert r.body["mode"] == "full"
        assert r.body["review_status"] == "not_required"
        assert r.body["request_id"]
        assert r.body["record_id"]

    def test_토글_off_다음_ask_full(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        _post(client, "/console/hitl/contract_ops", {"on": False})
        r = _post(client, "/ask", {"question": "계약서 검토해줘"})
        assert r.status == 200
        assert r.body["mode"] == "full"

    def test_토글_변경은_P17_ask_mode를_바꾸지_않는다(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")

        _post(client, "/console/hitl/contract_ops", {"on": True})
        r1 = _post(client, "/ask", {"question": "계약서 검토해줘"})
        assert r1.body["mode"] == "full"
        assert r1.body["review_status"] == "not_required"

        _post(client, "/console/hitl/contract_ops", {"on": False})
        r2 = _post(client, "/ask", {"question": "계약서 검토해줘"})
        assert r2.body["mode"] == "full"
        assert r2.body["review_status"] == "not_required"


# ════════════════════════════════════════════════════════════════════════════
# 워커 토큰 명령 (T9.2(b)·T9.5(c) — ADR 0026)
# ════════════════════════════════════════════════════════════════════════════


class Test콘솔_토큰:
    def test_발급_미로그인이면_401(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        r = _post(client, "/console/tokens", {"owner_id": "legal_lead", "role": "primary"})
        assert r.status == 401

    def test_발급_미존재_owner는_404(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(client, "/console/tokens", {"owner_id": "no_such_owner", "role": "primary"})
        assert r.status == 404

    def test_발급_성공시_평문_token_포함(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(client, "/console/tokens", {"owner_id": "legal_lead", "role": "primary"})
        assert r.status == 200
        assert isinstance(r.body["token"], str)
        assert len(r.body["token"]) > 0
        assert r.body["owner_id"] == "legal_lead"
        assert r.body["role"] == "primary"

    def test_발급_응답에_해시_없음(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(client, "/console/tokens", {"owner_id": "legal_lead", "role": "primary"})
        assert "token_hash" not in r.body

    def test_목록_미로그인이면_401(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        r = _get(client, "/console/tokens")
        assert r.status == 401

    def test_목록_평문_미노출(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        issued = _post(client, "/console/tokens", {"owner_id": "legal_lead", "role": "primary"})
        raw_token = issued.body["token"]

        r = _get(client, "/console/tokens")
        assert r.status == 200
        body_text = str(r.body)
        assert raw_token not in body_text
        assert "token_hash" not in body_text
        assert "token" not in r.body[0]  # 평문 필드 자체가 없어야 함

    def test_목록에_발급된_토큰이_보인다(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        issued = _post(client, "/console/tokens", {"owner_id": "legal_lead", "role": "primary"})
        token_id = issued.body["token_id"]

        r = _get(client, "/console/tokens")
        ids = [t["token_id"] for t in r.body]
        assert token_id in ids

    def test_revoke_미로그인이면_401(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        r = _post(client, "/console/tokens/some-id/revoke", {})
        assert r.status == 401

    def test_revoke_미존재_토큰_404(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(client, "/console/tokens/no-such-token/revoke", {})
        assert r.status == 404

    def test_revoke_성공시_목록에서_사라진다(self) -> None:
        app, _, _, _ = _console_app()
        client = TestClient(app)
        _login(client, "root_manager")
        issued = _post(client, "/console/tokens", {"owner_id": "legal_lead", "role": "primary"})
        token_id = issued.body["token_id"]

        r = _post(client, f"/console/tokens/{token_id}/revoke", {})
        assert r.status == 200
        assert r.body["revoked"] is True

        listing = _get(client, "/console/tokens")
        ids = [t["token_id"] for t in listing.body]
        assert token_id not in ids

    def test_인증_OFF면_로그인없이_발급_통과(self) -> None:
        app, _, _, _ = _console_app(auth=False)
        client = TestClient(app)
        r = _post(client, "/console/tokens", {"owner_id": "legal_lead", "role": "primary"})
        assert r.status == 200


# ════════════════════════════════════════════════════════════════════════════
# 하위호환 — token_store·hitl_toggles 둘 다 미주입(기존 데모 앱 무회귀)
# ════════════════════════════════════════════════════════════════════════════


class Test하위호환_미주입:
    def test_token_store_hitl_toggles_미주입이어도_앱_생성_성공(self) -> None:
        app = create_app(runtime=StubRuntime())
        client = TestClient(app)
        r = _post(client, "/ask", {"question": "계약서 검토해줘"})
        assert r.status == 200
        assert r.body["mode"] == "full"

    def test_미주입_인증OFF_콘솔_토큰_발급_동작(self) -> None:
        app = create_app(runtime=StubRuntime())
        client = TestClient(app)
        r = _post(client, "/console/tokens", {"owner_id": "legal_lead", "role": "primary"})
        assert r.status == 200
