"""T7.1 슬라이스 3 — POST /login/sso + SSO 모드 가드 + 신원 출처 불변 테스트.

결정론: TestClient(쿠키 유지) + FakeOidcProvider(in-memory 토큰→claims) + 고정 session_secret.
실 IdP·네트워크·JWKS 0.

커버 범위:
  SSO 모드 (oidc_provider 주입):
    - 유효 id_token → 200·세션 박힘·user_id 일치.
    - 위조 토큰 → 401.
    - 매핑 실패(email_verified=False) 토큰 → 401.
    - 매핑 실패(email 0매칭) 토큰 → 401.
    - 무비밀번호 /login → 403(SSO 모드 가드).
    - SSO 로그인 후 /inbox/cases 200(신원 출처 불변 — ADR 0021 결정 5).

  SSO 비활성 (oidc_provider 미주입):
    - POST /login/sso → 404.
    - 무비밀번호 POST /login은 기존대로 200(하위호환 회귀).
"""

from __future__ import annotations

from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.oidc import FakeOidcProvider, OidcClaims
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app

_SECRET = "test-sso-secret"

# 데모 cs_lead 신원 — demo.py의 User email과 일치
_CLAIMS_CS = OidcClaims(
    sub="sub-cs-001",
    email="cs.lead@example.com",
    email_verified=True,
    iss="https://idp.example.com",
    aud="agent-org-app",
)

_CLAIMS_UNVERIFIED = OidcClaims(
    sub="sub-cs-002",
    email="cs.lead@example.com",
    email_verified=False,  # 미검증
    iss="https://idp.example.com",
    aud="agent-org-app",
)

_CLAIMS_NO_MATCH = OidcClaims(
    sub="sub-outsider",
    email="outsider@other.com",  # registry에 없는 email
    email_verified=True,
    iss="https://idp.example.com",
    aud="agent-org-app",
)


def _result(res: Response) -> tuple[int, Any]:
    return res.status_code, res.json()


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    http: Any = client
    return _result(cast(Response, http.post(url, json=payload)))


def _get(client: TestClient, url: str) -> tuple[int, Any]:
    http: Any = client
    return _result(cast(Response, http.get(url)))


def _sso_app(extra_tokens: dict[str, OidcClaims] | None = None) -> TestClient:
    """SSO 모드 앱 — FakeOidcProvider + session_secret 주입."""
    tokens: dict[str, OidcClaims] = {
        "valid-cs-token": _CLAIMS_CS,
        "unverified-token": _CLAIMS_UNVERIFIED,
        "no-match-token": _CLAIMS_NO_MATCH,
    }
    if extra_tokens:
        tokens.update(extra_tokens)
    provider = FakeOidcProvider(tokens=tokens)
    app = create_app(
        runtime=StubRuntime(),
        session_secret=_SECRET,
        oidc_provider=provider,
    )
    return TestClient(app)


def _plain_auth_app() -> TestClient:
    """무비밀번호 모드 앱 — oidc_provider 미주입(SSO 비활성)."""
    app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
    return TestClient(app)


# ════════════════════════════════════════════════════════════════════════════
# SSO 모드 — /login/sso 흐름
# ════════════════════════════════════════════════════════════════════════════


class TestSsoLogin_정상흐름:
    def test_유효_token_200_user_id_반환(self) -> None:
        client = _sso_app()
        status, body = _post(client, "/login/sso", {"id_token": "valid-cs-token"})
        assert status == 200
        assert body["ok"] is True
        assert body["user_id"] == "cs_lead"

    def test_유효_token_로그인_후_세션에_박힘(self) -> None:
        """SSO 로그인 후 쿠키 세션이 유지돼 운영 엔드포인트가 200."""
        client = _sso_app()
        status, _ = _post(client, "/login/sso", {"id_token": "valid-cs-token"})
        assert status == 200
        # 세션 쿠키가 살아 있으므로 /inbox/cases가 200이어야 한다.
        status2, _ = _get(client, "/inbox/cases")
        assert status2 == 200

    def test_SSO_로그인_후_inbox_cases_user_id_일치(self) -> None:
        """신원 출처 불변(ADR 0021 결정 5) — SSO user_id가 세션에 박히면 운영 엔드포인트 무변경."""
        client = _sso_app()
        # cs_lead로 SSO 로그인
        _post(client, "/login/sso", {"id_token": "valid-cs-token"})
        # cs_lead 세션으로 /inbox/cases 조회 — 200이어야 한다(user_id 세션 재사용)
        status, _ = _get(client, "/inbox/cases")
        assert status == 200


class TestSsoLogin_거부:
    def test_위조_token_401(self) -> None:
        client = _sso_app()
        status, _ = _post(client, "/login/sso", {"id_token": "위조된-토큰-xyz"})
        assert status == 401

    def test_미등록_토큰_401(self) -> None:
        client = _sso_app()
        status, _ = _post(client, "/login/sso", {"id_token": "unknown-token"})
        assert status == 401

    def test_미검증_email_토큰_401(self) -> None:
        """email_verified=False claims를 가진 토큰 → resolve_identity가 거부 → 401."""
        client = _sso_app()
        status, _ = _post(client, "/login/sso", {"id_token": "unverified-token"})
        assert status == 401

    def test_0매칭_email_토큰_401(self) -> None:
        """registry에 없는 email → resolve_identity 0매칭 → 401."""
        client = _sso_app()
        status, _ = _post(client, "/login/sso", {"id_token": "no-match-token"})
        assert status == 401


# ════════════════════════════════════════════════════════════════════════════
# SSO 모드 가드 — 무비밀번호 /login 403
# ════════════════════════════════════════════════════════════════════════════


class TestSsoModeGard:
    def test_SSO_모드에서_무비밀번호_login은_403(self) -> None:
        """oidc_provider 주입 시 POST /login은 403(신원 선택 채널 차단)."""
        client = _sso_app()
        status, _ = _post(client, "/login", {"user_id": "cs_lead"})
        assert status == 403

    def test_SSO_모드에서_login_403_후_sso로_정상_로그인(self) -> None:
        """/login 403 거부 후 /login/sso로 올바르게 로그인할 수 있다."""
        client = _sso_app()
        # 무비밀번호 /login 시도 → 403
        status1, _ = _post(client, "/login", {"user_id": "cs_lead"})
        assert status1 == 403
        # SSO /login/sso로 정상 로그인
        status2, body2 = _post(client, "/login/sso", {"id_token": "valid-cs-token"})
        assert status2 == 200
        assert body2["user_id"] == "cs_lead"


# ════════════════════════════════════════════════════════════════════════════
# SSO 비활성(oidc_provider 미주입) — 하위호환 회귀
# ════════════════════════════════════════════════════════════════════════════


class TestSsoDisabled_하위호환:
    def test_SSO_비활성에서_login_sso는_404(self) -> None:
        """oidc_provider 미주입이면 /login/sso는 404(SSO 비활성)."""
        client = _plain_auth_app()
        status, _ = _post(client, "/login/sso", {"id_token": "any-token"})
        assert status == 404

    def test_SSO_비활성에서_무비밀번호_login은_기존대로_200(self) -> None:
        """SSO 비활성이면 기존 무비밀번호 /login이 그대로 동작(하위호환)."""
        client = _plain_auth_app()
        status, body = _post(client, "/login", {"user_id": "cs_lead"})
        assert status == 200
        assert body["ok"] is True
        assert body["user_id"] == "cs_lead"

    def test_SSO_비활성에서_무비밀번호_login_후_inbox_200(self) -> None:
        """SSO 비활성 — 무비밀번호 로그인 후 운영 엔드포인트 기존대로 동작."""
        client = _plain_auth_app()
        _post(client, "/login", {"user_id": "cs_lead"})
        status, _ = _get(client, "/inbox/cases")
        assert status == 200
