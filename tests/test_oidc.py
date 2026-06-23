"""T7.1 슬라이스 1·2 — FakeOidcProvider.verify + resolve_identity 결정론 테스트.

결정론: FakeOidcProvider(in-memory 토큰→claims 맵) + 테스트용 소형 Registry.
실 IdP·네트워크·JWKS 0.

커버 범위:
  슬라이스 1 (FakeOidcProvider.verify):
    - 등록 토큰 → OidcClaims 일치.
    - 미등록 토큰 → OidcVerificationError.
    - 빈 문자열 토큰 → OidcVerificationError.

  슬라이스 2 (resolve_identity):
    - verified email 1매칭 → user_id.
    - email_verified=False → OidcVerificationError(미검증 이메일 거부).
    - 0매칭(email 없는 registry) → OidcVerificationError.
    - 모호(같은 email User 복수) → OidcVerificationError.
    - email None User 제외 — claims.email이 빈 문자열이어도 None User와 오매칭 안 됨.
"""

from __future__ import annotations

import pytest

from agent_org_network.oidc import (
    FakeOidcProvider,
    OidcClaims,
    OidcVerificationError,
    resolve_identity,
)
from agent_org_network.registry import Registry
from agent_org_network.user import User

# ── 공통 픽스처 값 ───────────────────────────────────────────────────────────

_CLAIMS_CS = OidcClaims(
    sub="sub-cs-001",
    email="cs.lead@example.com",
    email_verified=True,
    iss="https://idp.example.com",
    aud="agent-org-app",
)

_CLAIMS_LEGAL = OidcClaims(
    sub="sub-legal-001",
    email="legal.lead@example.com",
    email_verified=True,
    iss="https://idp.example.com",
    aud="agent-org-app",
)


def _make_registry(*users: User) -> Registry:
    """테스트용 소형 Registry — 전달받은 User 목록만 등록."""
    reg = Registry()
    for u in users:
        reg.register_user(u)
    return reg


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 1 — FakeOidcProvider.verify
# ════════════════════════════════════════════════════════════════════════════


class TestFakeOidcProvider_Verify:
    def test_등록된_토큰은_claims를_돌려준다(self) -> None:
        provider = FakeOidcProvider(tokens={"valid-token-001": _CLAIMS_CS})
        result = provider.verify("valid-token-001")
        assert result == _CLAIMS_CS

    def test_다른_토큰도_각자_claims를_돌려준다(self) -> None:
        provider = FakeOidcProvider(
            tokens={
                "token-cs": _CLAIMS_CS,
                "token-legal": _CLAIMS_LEGAL,
            }
        )
        assert provider.verify("token-cs") == _CLAIMS_CS
        assert provider.verify("token-legal") == _CLAIMS_LEGAL

    def test_미등록_토큰은_OidcVerificationError(self) -> None:
        provider = FakeOidcProvider(tokens={"valid-token": _CLAIMS_CS})
        with pytest.raises(OidcVerificationError):
            provider.verify("위조된-토큰")

    def test_빈_토큰은_OidcVerificationError(self) -> None:
        provider = FakeOidcProvider(tokens={"valid-token": _CLAIMS_CS})
        with pytest.raises(OidcVerificationError):
            provider.verify("")

    def test_빈_맵에서_어떤_토큰도_OidcVerificationError(self) -> None:
        provider = FakeOidcProvider()
        with pytest.raises(OidcVerificationError):
            provider.verify("any-token")

    def test_맵_없이_생성해도_동작(self) -> None:
        """tokens=None 기본값으로 생성 — 빈 맵과 동등."""
        provider = FakeOidcProvider()
        with pytest.raises(OidcVerificationError):
            provider.verify("token")


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — resolve_identity (순수 함수)
# ════════════════════════════════════════════════════════════════════════════


class TestResolveIdentity:
    def test_verified_email_1매칭_user_id_반환(self) -> None:
        registry = _make_registry(
            User(id="cs_lead", email="cs.lead@example.com"),
        )
        user_id = resolve_identity(_CLAIMS_CS, registry)
        assert user_id == "cs_lead"

    def test_email_verified_false는_OidcVerificationError(self) -> None:
        """미검증 이메일은 신원으로 쓸 수 없다."""
        claims = OidcClaims(
            sub="sub-001",
            email="cs.lead@example.com",
            email_verified=False,
            iss="https://idp.example.com",
            aud="agent-org-app",
        )
        registry = _make_registry(User(id="cs_lead", email="cs.lead@example.com"))
        with pytest.raises(OidcVerificationError):
            resolve_identity(claims, registry)

    def test_0매칭_email이면_OidcVerificationError(self) -> None:
        """registry에 email 일치 User가 없으면 거부."""
        registry = _make_registry(
            User(id="legal_lead", email="legal.lead@example.com"),
        )
        with pytest.raises(OidcVerificationError):
            resolve_identity(_CLAIMS_CS, registry)

    def test_모호_복수매칭은_OidcVerificationError(self) -> None:
        """같은 email을 가진 User가 2명이면 안전하게 거부."""
        registry = _make_registry(
            User(id="cs_lead_a", email="cs.lead@example.com"),
            User(id="cs_lead_b", email="cs.lead@example.com"),
        )
        with pytest.raises(OidcVerificationError):
            resolve_identity(_CLAIMS_CS, registry)

    def test_email_None_User는_매칭_대상_제외(self) -> None:
        """email=None인 User는 email 비교에서 빠진다 — claims.email과 오매칭 없음."""
        registry = _make_registry(
            User(id="no_email_user", email=None),
            User(id="cs_lead", email="cs.lead@example.com"),
        )
        user_id = resolve_identity(_CLAIMS_CS, registry)
        assert user_id == "cs_lead"

    def test_빈_문자열_claims_email은_None_User와_오매칭_안됨(self) -> None:
        """claims.email이 빈 문자열이어도 email=None User와 매칭되지 않는다."""
        claims_empty_email = OidcClaims(
            sub="sub-x",
            email="",
            email_verified=True,
            iss="https://idp.example.com",
            aud="agent-org-app",
        )
        registry = _make_registry(User(id="some_user", email=None))
        with pytest.raises(OidcVerificationError):
            resolve_identity(claims_empty_email, registry)

    def test_빈_문자열_email은_빈_email_User와도_오매칭_안됨(self) -> None:
        """claims.email이 빈 문자열이면 email='' User가 있어도 거부(빈 이메일 가드)."""
        claims_empty_email = OidcClaims(
            sub="sub-x",
            email="",
            email_verified=True,
            iss="https://idp.example.com",
            aud="agent-org-app",
        )
        registry = _make_registry(User(id="empty_email_user", email=""))
        with pytest.raises(OidcVerificationError):
            resolve_identity(claims_empty_email, registry)

    def test_registry_전원_email_없어도_0매칭_거부(self) -> None:
        """모든 User의 email이 None이면 0매칭 → 거부."""
        registry = _make_registry(
            User(id="user_a"),
            User(id="user_b"),
        )
        with pytest.raises(OidcVerificationError):
            resolve_identity(_CLAIMS_CS, registry)

    def test_데모_6명_registry로_cs_lead_매핑(self) -> None:
        """데모 User 6명(email 부여) 중에서 cs.lead@example.com → cs_lead."""
        registry = _make_registry(
            User(id="root_manager", email="root.manager@example.com"),
            User(id="legal_lead", email="legal.lead@example.com"),
            User(id="cs_lead", email="cs.lead@example.com"),
            User(id="finance_lead", email="finance.lead@example.com"),
            User(id="hr_lead", email="hr.lead@example.com"),
            User(id="it_lead", email="it.lead@example.com"),
        )
        user_id = resolve_identity(_CLAIMS_CS, registry)
        assert user_id == "cs_lead"
