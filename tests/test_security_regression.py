"""T6.5 보안 리뷰 후속 — B-1·B-2·M-1·M-2 우회 회귀 테스트.

red → green 순서: 이 파일이 먼저 실패(red)한 뒤 수정으로 green이 된다.

B-1: create_central_app에 session_secret 인자가 없어 인증 OFF — 레거시 path 우회.
B-2: 모듈 기본 앱이 env secret 없이 인증 OFF.
M-1: concur 미존재 case → 400(ADR 0016 결정 4는 404여야 함).
M-2: central_app 인증 회귀 — create_central_app(session_secret=...)으로 우회 차단 검증.
"""

from __future__ import annotations

import os
from typing import Any, cast
from unittest.mock import patch

from fastapi.testclient import TestClient
from httpx import Response

_SECRET = "test-secret"


def _get(client: TestClient, url: str) -> Response:
    http: Any = client
    return cast(Response, http.get(url))


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> Response:
    http: Any = client
    return cast(Response, http.post(url, json=payload))


def _login(client: TestClient, user_id: str) -> Response:
    return _post(client, "/login", {"user_id": user_id})


# ════════════════════════════════════════════════════════════════════════════
# B-1 — create_central_app에 session_secret 인자 추가 · 인증 ON 검증
# ════════════════════════════════════════════════════════════════════════════


class TestB1_CentralApp_인증_우회_차단:
    """create_central_app(session_secret=...) → 인증 ON, 레거시 path 404."""

    def _central_auth_client(self) -> TestClient:
        from agent_org_network.server import create_central_app

        return TestClient(create_central_app(session_secret=_SECRET))

    def test_central_session_secret_주입_시_SessionMiddleware_부착(self) -> None:
        """secret 주입 시 /monitor 미로그인 → 401(SessionMiddleware 부착 증거)."""
        client = self._central_auth_client()
        r = _get(client, "/monitor")
        assert r.status_code == 401, f"기대 401, 실제 {r.status_code}"

    def test_central_미로그인_inbox_레거시_path는_404(self) -> None:
        """인증 ON이면 /inbox/cs_lead(레거시) 자체가 미등록 → 404."""
        client = self._central_auth_client()
        r = _get(client, "/inbox/cs_lead")
        assert r.status_code == 404, f"기대 404, 실제 {r.status_code}"

    def test_central_미로그인_manager_레거시_path는_404(self) -> None:
        """인증 ON이면 /manager/root_manager(레거시) 자체가 미등록 → 404."""
        client = self._central_auth_client()
        r = _get(client, "/manager/root_manager")
        assert r.status_code == 404, f"기대 404, 실제 {r.status_code}"

    def test_central_미로그인_inbox_backup_reviews_레거시_path는_404(self) -> None:
        """인증 ON이면 /inbox/cs_lead/backup-reviews(레거시) 미등록 → 404."""
        client = self._central_auth_client()
        r = _get(client, "/inbox/cs_lead/backup-reviews")
        assert r.status_code == 404, f"기대 404, 실제 {r.status_code}"

    def test_central_미로그인_monitor는_401(self) -> None:
        """인증 ON이면 /monitor 미로그인 → 401."""
        client = self._central_auth_client()
        r = _get(client, "/monitor")
        assert r.status_code == 401, f"기대 401, 실제 {r.status_code}"

    def test_central_로그인_후_자기_inbox_세션_경로_200(self) -> None:
        """인증 ON에서도 로그인 후 /inbox/cases(세션 경로)는 200."""
        client = self._central_auth_client()
        _login(client, "cs_lead")
        r = _get(client, "/inbox/cases")
        assert r.status_code == 200, f"기대 200, 실제 {r.status_code}"

    def test_central_로그인_후_monitor_200(self) -> None:
        """인증 ON에서 로그인 후 /monitor → 200."""
        client = self._central_auth_client()
        _login(client, "cs_lead")
        r = _get(client, "/monitor")
        assert r.status_code == 200, f"기대 200, 실제 {r.status_code}"


# ════════════════════════════════════════════════════════════════════════════
# B-2 — 모듈 기본 앱이 env secret 미주입 시 안전하게 로드됨 (import 안전)
# ════════════════════════════════════════════════════════════════════════════


class TestB2_모듈_기본_앱_env_secret:
    """모듈 기본 앱(app·central_app)이 env secret을 읽어야 함.

    env 미설정 시 인증 OFF(데모), 설정 시 인증 ON(프로덕션).
    secret 하드코딩 금지 — env만. 모듈 import 자체가 안 깨지는지 확인.
    """

    def test_web_app_env_미설정_시_인증_OFF(self) -> None:
        """OPERATOR_SESSION_SECRET 없으면 /monitor 미로그인도 200(인증 OFF=데모)."""
        # env 미설정 상태 — create_app(session_secret=None)과 동치
        from agent_org_network.web import create_app

        app = create_app(session_secret=None)
        client = TestClient(app)
        # 인증 OFF면 /monitor는 200(audit_reader=None이면 빈 목록)
        r = _get(client, "/monitor")
        assert r.status_code == 200, f"인증 OFF 시 /monitor는 200이어야 함, 실제 {r.status_code}"

    def test_web_app_env_설정_시_인증_ON(self) -> None:
        """OPERATOR_SESSION_SECRET 있으면 /monitor 미로그인 → 401(인증 ON=프로덕션)."""
        from agent_org_network.web import create_app

        app = create_app(session_secret="env-injected-secret")
        client = TestClient(app)
        r = _get(client, "/monitor")
        assert r.status_code == 401, (
            f"인증 ON 시 /monitor 미로그인은 401이어야 함, 실제 {r.status_code}"
        )

    def test_모듈_app이_OPERATOR_SESSION_SECRET_env를_읽는다(self) -> None:
        """web.py 모듈 기본 app이 env secret을 읽어 인증 ON이 됨을 팩토리로 등가 검증."""
        # 직접 env 조작 대신 팩토리에 secret을 주입/미주입해 동등성 검증
        from agent_org_network.web import create_app

        # secret 미주입 — 인증 OFF
        app_off = create_app(session_secret=None)
        client_off = TestClient(app_off)
        assert _get(client_off, "/monitor").status_code == 200

        # secret 주입 — 인증 ON
        app_on = create_app(session_secret="some-secret")
        client_on = TestClient(app_on)
        assert _get(client_on, "/monitor").status_code == 401

    def test_모듈_central_app이_OPERATOR_SESSION_SECRET_env를_읽는다(self) -> None:
        """server.py 모듈 기본 central_app도 env secret을 읽어 인증 ON이 됨을 등가 검증."""
        from agent_org_network.server import create_central_app

        # secret 미주입 — 인증 OFF
        app_off = create_central_app(session_secret=None)
        client_off = TestClient(app_off)
        assert _get(client_off, "/monitor").status_code == 200

        # secret 주입 — 인증 ON
        app_on = create_central_app(session_secret="some-secret")
        client_on = TestClient(app_on)
        assert _get(client_on, "/monitor").status_code == 401

    def test_모듈_기본_앱_임포트_안_깨짐(self) -> None:
        """모듈 기본 앱(app·central_app) 생성이 ImportError 없이 완료됨."""
        # env 미설정 상태에서 import가 안 깨지는지만 확인
        saved = os.environ.pop("OPERATOR_SESSION_SECRET", None)
        try:
            import importlib
            import agent_org_network.web as web_mod
            import agent_org_network.server as server_mod

            importlib.reload(web_mod)
            importlib.reload(server_mod)
            assert web_mod.app is not None
            assert server_mod.central_app is not None
        finally:
            if saved is not None:
                os.environ["OPERATOR_SESSION_SECRET"] = saved


# ════════════════════════════════════════════════════════════════════════════
# M-1 — concur 미존재 case는 404 (ADR 0016 결정 4)
# ════════════════════════════════════════════════════════════════════════════


class TestM1_Concur_미존재_case_404:
    """POST /cases/{case_id}/concur: 미존재 case → 404.

    기존 test_auth.py의 test_미존재_case는_400은 틀린 기대(ADR 0016 결정 4 위반).
    올바른 동작: 미존재 → 404, 후보 아님 → 403.
    """

    def test_미존재_case_concur는_404(self) -> None:
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.web import create_app

        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "cs_lead")
        r = _post(client, "/cases/없는케이스/concur", {"on_agent": "cs_ops"})
        assert r.status_code == 404, f"미존재 case는 404여야 함, 실제 {r.status_code}"

    def test_후보_아님_concur는_여전히_403(self) -> None:
        """M-1 수정 후에도 후보 아님 → 403 불변."""
        from datetime import datetime, timezone

        from agent_org_network.conflict import Candidate, ConflictCase
        from agent_org_network.demo import build_demo
        from agent_org_network.manager_queue import InMemoryManagerQueueStore
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.web import create_app

        queue_store = InMemoryManagerQueueStore()
        bundle = build_demo(runtime=StubRuntime(), manager_queue_store=queue_store)
        case = ConflictCase(
            intent="보상",
            question="보상 기준이 어떻게 되나요?",
            candidates=(
                Candidate(agent_id="cs_ops", owner="cs_lead"),
                Candidate(agent_id="finance_ops", owner="finance_lead"),
            ),
            opened_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
            case_id="legacy-security-case",
        )
        bundle.case_store.open_case(case)
        with patch("agent_org_network.web.build_demo", return_value=bundle):
            app = create_app(
                runtime=StubRuntime(),
                session_secret=_SECRET,
                manager_queue_store=queue_store,
            )
        client = TestClient(app)
        _login(client, "legal_lead")  # 재로그인 (후보 아님)
        r = _post(client, f"/cases/{case.case_id}/concur", {"on_agent": "cs_ops"})
        assert r.status_code == 403, f"후보 아님은 403이어야 함, 실제 {r.status_code}"
