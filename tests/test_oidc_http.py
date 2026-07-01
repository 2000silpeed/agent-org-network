"""T8.3 — HttpOidcProvider 결정론 테스트(실 RS256 서명 검증·네트워크 0).

결정론 경계: cryptography로 테스트 내 RSA 키쌍을 만들고 PyJWT로 id_token을 서명한 뒤,
fixture fetcher(주입 seam)로 JWKS dict를 공급한다 — 실 네트워크·실 IdP 0. exp/nbf 검증은
주입 clock으로 고정한다. `FakeGitGateway`가 결정 SHA로 게이트에서 도는 정신.

이 스위트는 oidc extra(PyJWT+cryptography)가 있어야 돈다 — 게이트 환경엔 dev 그룹으로
항상 있으므로 skip은 예외적이다. 미설치 환경에선 importorskip이 조용히 건너뛴다(코어
무영향 — anthropic/fastembed extra 테스트와 같은 패턴).

커버 범위:
  - 정상 통과(서명·iss·aud·exp) → OidcClaims 매핑(email/email_verified 포함).
  - 서명 위조(다른 키로 서명) 거부.
  - 만료(주입 clock) 거부.
  - iss 불일치 거부.
  - aud 불일치 거부.
  - kid 미스 → 재fetch 후 통과(키 롤테이션).
  - JWKS TTL 캐시 — 만료 전엔 재fetch 안 함, 만료 후 재fetch.
  - JWKS fetch 실패 시 명확한 OidcVerificationError.
  - _urllib_jwks_fetcher가 비-JSON·객체 아님을 OidcVerificationError로 감쌈.
  - HttpOidcProvider가 email 없는 payload도 안전 기본값으로 떨어뜨림.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("jwt")
pytest.importorskip("cryptography")

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jwt.algorithms import RSAAlgorithm  # noqa: E402

from agent_org_network.oidc import (  # noqa: E402
    HttpOidcProvider,
    OidcVerificationError,
    _urllib_jwks_fetcher,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.runtime import StubRuntime  # noqa: E402
from agent_org_network.web import create_app  # noqa: E402

# ── 고정 구성값 ──────────────────────────────────────────────────────────────

_ISSUER = "https://idp.example.com"
_AUDIENCE = "agent-org-app"
_JWKS_URL = "https://idp.example.com/.well-known/jwks.json"

# 주입 clock의 "지금"(epoch 초). 토큰 exp/nbf를 이 값 기준으로 짠다.
_NOW = 1_700_000_000.0


class _Keypair:
    """테스트용 RSA 키쌍 + JWKS 항목 빌더."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict[str, Any]:
        raw = RSAAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        jwk: dict[str, Any] = {str(k): v for k, v in raw.items()}
        jwk["kid"] = self.kid
        jwk["alg"] = "RS256"
        jwk["use"] = "sig"
        return jwk

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": self.kid})


def _jwks(*pairs: _Keypair) -> dict[str, Any]:
    return {"keys": [p.jwk() for p in pairs]}


def _base_claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": "sub-cs-001",
        "email": "cs.lead@example.com",
        "email_verified": True,
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "iat": int(_NOW) - 10,
        "exp": int(_NOW) + 3600,
    }
    claims.update(overrides)
    return claims


class _CountingFetcher:
    """호출 횟수를 세는 fixture JWKS fetcher(재fetch·캐시 단언용)."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        self.jwks = jwks
        self.calls = 0

    def __call__(self, url: str) -> dict[str, Any]:
        self.calls += 1
        return self.jwks


def _provider(
    fetcher: Any,
    *,
    now: float = _NOW,
    ttl: float = 3600.0,
) -> HttpOidcProvider:
    return HttpOidcProvider(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        jwks_url=_JWKS_URL,
        jwks_fetcher=fetcher,
        clock=lambda: now,
        jwks_ttl_seconds=ttl,
    )


# ════════════════════════════════════════════════════════════════════════════
# 정상 통과 + claim 매핑
# ════════════════════════════════════════════════════════════════════════════


class TestHttpOidcProvider_정상:
    def test_유효_토큰_claims_매핑(self) -> None:
        kp = _Keypair("kid-1")
        provider = _provider(_CountingFetcher(_jwks(kp)))
        token = kp.sign(_base_claims())

        claims = provider.verify(token)

        assert claims.sub == "sub-cs-001"
        assert claims.email == "cs.lead@example.com"
        assert claims.email_verified is True
        assert claims.iss == _ISSUER
        assert claims.aud == _AUDIENCE

    def test_email_없는_payload는_안전_기본값(self) -> None:
        kp = _Keypair("kid-1")
        provider = _provider(_CountingFetcher(_jwks(kp)))
        claims_no_email = _base_claims()
        del claims_no_email["email"]
        del claims_no_email["email_verified"]
        token = kp.sign(claims_no_email)

        claims = provider.verify(token)

        assert claims.email == ""
        assert claims.email_verified is False

    def test_kid_없는_단일키_JWKS도_통과(self) -> None:
        """단일 키 IdP에서 kid 헤더가 없어도 유일 키로 검증한다."""
        kp = _Keypair("kid-1")
        provider = _provider(_CountingFetcher(_jwks(kp)))
        # kid 헤더 없이 서명
        token = jwt.encode(_base_claims(), kp.private, algorithm="RS256")

        claims = provider.verify(token)

        assert claims.sub == "sub-cs-001"


# ════════════════════════════════════════════════════════════════════════════
# 거부 — 서명·만료·iss·aud
# ════════════════════════════════════════════════════════════════════════════


class TestHttpOidcProvider_거부:
    def test_서명_위조_거부(self) -> None:
        """JWKS엔 kp_a 공개키만 있는데 토큰은 kp_b로 서명 → 서명 검증 실패."""
        kp_a = _Keypair("kid-1")
        kp_b = _Keypair("kid-1")  # 같은 kid로 위조 시도, 실제 키는 다름
        provider = _provider(_CountingFetcher(_jwks(kp_a)))
        token = kp_b.sign(_base_claims())

        with pytest.raises(OidcVerificationError):
            provider.verify(token)

    def test_만료_토큰_거부(self) -> None:
        kp = _Keypair("kid-1")
        provider = _provider(_CountingFetcher(_jwks(kp)))
        # exp가 now 이전
        token = kp.sign(_base_claims(exp=int(_NOW) - 1))

        with pytest.raises(OidcVerificationError):
            provider.verify(token)

    def test_아직_유효하지_않은_nbf_거부(self) -> None:
        kp = _Keypair("kid-1")
        provider = _provider(_CountingFetcher(_jwks(kp)))
        # nbf가 now 이후(아직 유효하지 않음)
        token = kp.sign(_base_claims(nbf=int(_NOW) + 100))

        with pytest.raises(OidcVerificationError):
            provider.verify(token)

    def test_iss_불일치_거부(self) -> None:
        kp = _Keypair("kid-1")
        provider = _provider(_CountingFetcher(_jwks(kp)))
        token = kp.sign(_base_claims(iss="https://evil.example.com"))

        with pytest.raises(OidcVerificationError):
            provider.verify(token)

    def test_aud_불일치_거부(self) -> None:
        kp = _Keypair("kid-1")
        provider = _provider(_CountingFetcher(_jwks(kp)))
        token = kp.sign(_base_claims(aud="other-app"))

        with pytest.raises(OidcVerificationError):
            provider.verify(token)

    def test_형식_오류_토큰_거부(self) -> None:
        kp = _Keypair("kid-1")
        provider = _provider(_CountingFetcher(_jwks(kp)))

        with pytest.raises(OidcVerificationError):
            provider.verify("not-a-jwt")


# ════════════════════════════════════════════════════════════════════════════
# JWKS 캐싱 — kid 미스 재fetch + TTL
# ════════════════════════════════════════════════════════════════════════════


class TestHttpOidcProvider_JWKS캐싱:
    def test_kid_미스_재fetch_후_통과(self) -> None:
        """캐시된 JWKS엔 옛 키만 있는데 새 kid 토큰 도착 → 1회 재fetch 후 통과(롤테이션)."""
        old_kp = _Keypair("kid-old")
        new_kp = _Keypair("kid-new")

        class RotatingFetcher:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, url: str) -> dict[str, Any]:
                self.calls += 1
                # 1차 fetch: 옛 키만. 2차(재fetch): 새 키 포함.
                if self.calls == 1:
                    return _jwks(old_kp)
                return _jwks(old_kp, new_kp)

        fetcher = RotatingFetcher()
        provider = _provider(fetcher)
        # 옛 키로 먼저 검증해 캐시를 채운다.
        provider.verify(old_kp.sign(_base_claims()))
        assert fetcher.calls == 1

        # 새 kid 토큰 → 캐시 미스 → 재fetch 후 통과.
        claims = provider.verify(new_kp.sign(_base_claims(sub="sub-new")))
        assert claims.sub == "sub-new"
        assert fetcher.calls == 2

    def test_kid_미스_재fetch_후에도_없으면_거부(self) -> None:
        kp = _Keypair("kid-1")
        unknown_kp = _Keypair("kid-unknown")
        fetcher = _CountingFetcher(_jwks(kp))
        provider = _provider(fetcher)

        with pytest.raises(OidcVerificationError):
            provider.verify(unknown_kp.sign(_base_claims()))
        # 최초 fetch + kid 미스 재fetch = 2회
        assert fetcher.calls == 2

    def test_TTL_내_재검증은_재fetch_안함(self) -> None:
        kp = _Keypair("kid-1")
        fetcher = _CountingFetcher(_jwks(kp))
        provider = _provider(fetcher, ttl=3600.0)

        provider.verify(kp.sign(_base_claims()))
        provider.verify(kp.sign(_base_claims(sub="sub-2")))
        # 같은 kid·TTL 내라 fetch는 1회뿐.
        assert fetcher.calls == 1

    def test_TTL_만료_후_재fetch(self) -> None:
        kp = _Keypair("kid-1")
        fetcher = _CountingFetcher(_jwks(kp))
        # clock을 리스트로 밀어 시간 전진 시뮬.
        clock_state = {"t": _NOW}
        provider = HttpOidcProvider(
            issuer=_ISSUER,
            audience=_AUDIENCE,
            jwks_url=_JWKS_URL,
            jwks_fetcher=fetcher,
            clock=lambda: clock_state["t"],
            jwks_ttl_seconds=100.0,
        )
        provider.verify(kp.sign(_base_claims()))
        assert fetcher.calls == 1
        # TTL(100s) 경과.
        clock_state["t"] = _NOW + 200.0
        provider.verify(kp.sign(_base_claims(sub="sub-2")))
        assert fetcher.calls == 2


# ════════════════════════════════════════════════════════════════════════════
# JWKS fetch 실패
# ════════════════════════════════════════════════════════════════════════════


class TestHttpOidcProvider_fetch실패:
    def test_fetcher_예외는_OidcVerificationError로_전파(self) -> None:
        def boom(url: str) -> dict[str, Any]:
            raise OidcVerificationError("JWKS fetch 실패(시뮬)")

        kp = _Keypair("kid-1")
        provider = _provider(boom)
        with pytest.raises(OidcVerificationError):
            provider.verify(kp.sign(_base_claims()))

    def test_keys_없는_JWKS는_kid_미스로_거부(self) -> None:
        kp = _Keypair("kid-1")
        provider = _provider(_CountingFetcher({"keys": []}))
        with pytest.raises(OidcVerificationError):
            provider.verify(kp.sign(_base_claims()))


# ════════════════════════════════════════════════════════════════════════════
# _urllib_jwks_fetcher — 파싱 가드(네트워크 0, urlopen 모킹)
# ════════════════════════════════════════════════════════════════════════════


class TestUrllibJwksFetcher:
    def test_JSON_객체가_아니면_거부(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeResp:
            def read(self) -> bytes:
                return b"[1, 2, 3]"  # JSON이지만 객체가 아님(list)

            def __enter__(self) -> FakeResp:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def fake_urlopen(url: str) -> FakeResp:
            return FakeResp()

        monkeypatch.setattr("agent_org_network.oidc.urllib.request.urlopen", fake_urlopen)
        with pytest.raises(OidcVerificationError):
            _urllib_jwks_fetcher("https://idp.example.com/jwks")

    def test_정상_JSON_객체는_dict_반환(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {"keys": [{"kid": "k1"}]}

        class FakeResp:
            def read(self) -> bytes:
                return json.dumps(payload).encode()

            def __enter__(self) -> FakeResp:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def fake_urlopen(url: str) -> FakeResp:
            return FakeResp()

        monkeypatch.setattr("agent_org_network.oidc.urllib.request.urlopen", fake_urlopen)
        assert _urllib_jwks_fetcher("https://idp.example.com/jwks") == payload

    def test_urlopen_예외는_OidcVerificationError로_감쌈(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(url: str) -> Any:
            raise OSError("네트워크 다운")

        monkeypatch.setattr(
            "agent_org_network.oidc.urllib.request.urlopen",
            boom,
        )
        with pytest.raises(OidcVerificationError):
            _urllib_jwks_fetcher("https://idp.example.com/jwks")


# ════════════════════════════════════════════════════════════════════════════
# POST /login/sso 통합 — HttpOidcProvider 주입(TestClient·fixture fetcher·네트워크 0)
# ════════════════════════════════════════════════════════════════════════════


class TestLoginSsoWithHttpProvider:
    def test_실_서명_토큰으로_login_sso_통과(self) -> None:
        """HttpOidcProvider(실 RS256 검증)를 /login/sso에 주입 — 실 서명 토큰이 세션에 박힌다.

        cs_lead 데모 신원 email로 서명한 id_token을 fixture fetcher가 검증할 JWKS로 통과시켜,
        resolve_identity가 cs_lead로 매핑하고 세션이 유지되는지(inbox 200) 라운드로 확인한다.
        """
        kp = _Keypair("kid-1")
        provider = HttpOidcProvider(
            issuer=_ISSUER,
            audience=_AUDIENCE,
            jwks_url=_JWKS_URL,
            jwks_fetcher=_CountingFetcher(_jwks(kp)),
            clock=lambda: _NOW,
        )
        app = create_app(
            runtime=StubRuntime(),
            session_secret="test-http-oidc-secret",
            oidc_provider=provider,
        )
        client = TestClient(app)

        token = kp.sign(_base_claims())
        http: Any = client
        res: Any = http.post("/login/sso", json={"id_token": token})
        assert res.status_code == 200
        body: Any = res.json()
        assert body["ok"] is True
        assert body["user_id"] == "cs_lead"

        # 세션 유지 라운드 — 운영 엔드포인트 200.
        res2: Any = http.get("/inbox/cases")
        assert res2.status_code == 200

    def test_위조_서명_토큰은_login_sso_401(self) -> None:
        kp_real = _Keypair("kid-1")
        kp_forged = _Keypair("kid-1")
        provider = HttpOidcProvider(
            issuer=_ISSUER,
            audience=_AUDIENCE,
            jwks_url=_JWKS_URL,
            jwks_fetcher=_CountingFetcher(_jwks(kp_real)),
            clock=lambda: _NOW,
        )
        app = create_app(
            runtime=StubRuntime(),
            session_secret="test-http-oidc-secret",
            oidc_provider=provider,
        )
        client = TestClient(app)

        http: Any = client
        res: Any = http.post("/login/sso", json={"id_token": kp_forged.sign(_base_claims())})
        assert res.status_code == 401
