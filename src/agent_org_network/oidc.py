"""SSO/OIDC 신원 binding — OidcProvider 포트 + OidcClaims + resolve_identity (T7.1, ADR 0021).

**이 모듈은 shape(미구현 통과 stub)다 — tdd-engineer가 red→green으로 채운다.**

ADR 0016이 신원 출처를 *세션*으로 이미 격리했으므로(`_session_identity` 한 곳), SSO는
"세션에 박기 전 검증" 단계 하나만 교체한다 — `POST /login`의 "아무 user_id나 *선택*"을
"IdP가 *증명*한 신원만 세션에 박기"로(ADR 0021 결정 5). 이것이 owner 종속(card.owner)의
*증명* 미싱피스를 채워 owner 주권을 실재화한다(ADR 0017 결정 5).

ADR 0021:
- 결정 1: `OidcProvider` 포트(Protocol) — `verify(id_token) -> OidcClaims`(검증 실패=만료·
  잘못된 서명·aud 불일치 시 `OidcVerificationError`). 실 git·claude·GitGateway와 **같은 포트
  패턴**: 결정론 `FakeOidcProvider`(주입 토큰→claims·위조/만료 거부, 게이트 내) + 실
  `HttpOidcProvider`(JWKS·RS256·iss/aud/exp 검증 — 게이트 밖 수동·새 의존성은 후속 판단).
  공급자 중립 — 표준 claim(sub·email·email_verified·iss·aud)만 가정(Google hd·MS tid 안 씀).
- 결정 2: `OidcClaims`(frozen 값 객체) — sub·email·email_verified·iss·aud. `exp`는 *검증의
  책임*(verify가 만료를 걸러냄)이라 값 객체에 안 넣는다(검증 통과 신원만 표상). 전송 DTO 아님.
- 결정 3: 신원 매핑 baseline = **verified email → registry User**. `resolve_identity(claims,
  registry) -> user_id`(순수 함수·web 분리 경계 — `validate_card_for_builder`와 같은 결):
  email_verified 가드 → `claims.email == user.email`인 User → 0매칭/미검증/모호 거부.

결정론 경계(ADR 0003·0021 결정 1): `FakeOidcProvider`는 in-memory 토큰→claims 맵이라 게이트
에서 돈다(`FakeGitGateway`의 결정 SHA와 같은 결). `HttpOidcProvider`는 실 서명·네트워크라
게이트 밖(수동 시연). single tenant — tenant 개념·격리·프리픽스 없음(ADR 0021 맥락).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from agent_org_network.registry import Registry


class OidcVerificationError(Exception):
    """id_token 검증 실패 — 만료·잘못된 서명·aud 불일치·형식 오류(ADR 0021 결정 1).

    `verify`가 통과시킬 수 없는 토큰에 올린다. web 경계가 이를 401로 매핑한다
    (미검증 신원은 세션에 못 박는다 — `NotAuthenticatedError`와 다른 축: 그건 "세션 없음",
    이건 "증명 실패").
    """


class OidcClaims(BaseModel, frozen=True):
    """검증을 통과한 신뢰할 수 있는 OIDC claim의 도메인 값 객체(ADR 0021 결정 2).

    공급자 중립 — 표준 claim만 든다(공급자별 특수 claim 없음). `verify`가 서명·만료·aud를
    검증한 *뒤*의 신원이라, 이 값 객체가 존재한다는 것 자체가 "유효성 검증 통과"를 뜻한다.

    필드:
      - `sub` — IdP의 안정 주체 식별자(공급자 내 유일).
      - `email` — 회사 이메일(매핑 baseline의 키, 결정 3).
      - `email_verified` — IdP가 이 이메일을 검증했나(매핑 가드 — False면 거부).
      - `iss` — 발급자(IdP).
      - `aud` — 이 토큰이 향한 audience(우리 client).

    **`exp`는 일부러 안 든다** — 만료는 *검증의 책임*(verify가 만료 토큰을 OidcVerificationError로
    걸러냄)이지 검증 통과 후의 도메인 값이 아니다. `Answer`가 snapshot_sha는 들되 검증용 메타는
    안 드는 경계와 동형. frozen 값 객체이지 전송 와이어 DTO(`SsoLoginRequest`)가 아니다.
    """

    sub: str
    email: str
    email_verified: bool
    iss: str
    aud: str


class OidcProvider(Protocol):
    """id_token을 검증해 신뢰할 수 있는 claim으로 바꾸는 최소 포트(ADR 0021 결정 1).

    신원 *검증*은 비결정·외부 의존(IdP·JWKS·서명)이라 `GitGateway`·`AgentRuntime`·`ClaudeRunner`와
    **같은 포트 패턴**으로 격리한다 — 실 구현(`HttpOidcProvider`)은 JWKS fetch·RS256 서명 검증
    (부작용·게이트 밖), 단위 테스트는 `FakeOidcProvider`(in-memory 토큰→claims·결정론) 주입.
    공급자 중립 — 표준 claim만 가정(어떤 OIDC IdP에도 붙는다).
    """

    def verify(self, id_token: str) -> OidcClaims:
        """id_token의 서명·만료·audience를 검증하고 표준 claim을 돌려준다.

        통과하면 `OidcClaims`, 실패(만료·잘못된 서명·aud 불일치·형식 오류)하면
        `OidcVerificationError`를 올린다(ADR 0021 결정 1). 불투명 토큰 → 신뢰 claim의 변환.
        """
        ...


class FakeOidcProvider:
    """결정론 in-memory `OidcProvider` — 게이트(단위 테스트)에서 돈다(ADR 0021 결정 1).

    실 서명·네트워크 없이 토큰→claims 맵을 dict로 들고, 등록된 토큰엔 그 claims를, 모르는·
    위조·만료 토큰엔 `OidcVerificationError`를 낸다(만료/위조 시뮬). `FakeGitGateway`의
    결정 SHA와 같은 결 — 시각·랜덤·네트워크 0이라 결정론.
    """

    def __init__(self, tokens: dict[str, OidcClaims] | None = None) -> None:
        # id_token → 검증 통과 시 돌려줄 claims. 미등록 토큰은 verify에서 거부(위조 시뮬).
        self._tokens: dict[str, OidcClaims] = dict(tokens or {})

    def verify(self, id_token: str) -> OidcClaims:
        if id_token not in self._tokens:
            raise OidcVerificationError(f"미등록·위조·만료 토큰: {id_token!r}")
        return self._tokens[id_token]


class HttpOidcProvider:
    """실 JWKS·RS256 검증 `OidcProvider` — **게이트 밖 수동 시연**(ADR 0021 결정 1).

    IdP의 JWKS endpoint에서 공개키를 fetch해 id_token의 RS256 서명을 검증하고, iss/aud/exp를
    검증한다(실 네트워크·서명·비결정 — `SubprocessGitGateway`의 git subprocess와 같은 결).
    새 무거운 의존성(서명 검증 라이브러리)을 더할지는 **tdd-engineer/후속이 판단**한다 — 이
    shape는 자리만(NotImplementedError). 실 검증이라 단위 게이트에서 돌리지 않는다(수동 시연).

    **현재는 shape stub(미구현)** — 실 본문은 후속(mcp-runtime-engineer/수동·게이트 밖).
    """

    def __init__(self, issuer: str, audience: str, jwks_uri: str) -> None:
        self.issuer = issuer
        self.audience = audience
        self.jwks_uri = jwks_uri

    def verify(self, id_token: str) -> OidcClaims:
        raise NotImplementedError("실 OIDC 검증 — 게이트 밖 수동 시연(T7.1 후속)")


def resolve_identity(claims: OidcClaims, registry: Registry) -> str:
    """검증된 claim을 registry user_id로 매핑한다(ADR 0021 결정 3 — verified email baseline).

    절차(web과 분리한 순수 함수 — `validate_card_for_builder`·`serialize_reply`와 같은 경계,
    결정론 테스트 대상):
      ① `claims.email_verified`가 True가 아니거나 email이 비어 있으면 거부(미검증·빈 이메일은
         신원으로 못 씀 — 빈 email끼리 오매칭 차단).
      ② `claims.email == user.email`인 User를 registry에서 찾는다(User.email이 SSOT, 결정 3).
      ③ 정확히 1명이면 그 user_id, 0매칭/모호면 거부(인증 실패).

    거부는 `OidcVerificationError`로 표현한다(증명은 됐으나 우리 조직 신원으로 못 잇는 경우 —
    web이 401로 매핑). single tenant라 tenant 스코프 없음 — registry 전체에서 email로 찾는다.
    """
    if claims.email_verified is not True:
        raise OidcVerificationError("email_verified가 True가 아님 — 미검증 이메일 거부")
    if not claims.email:
        raise OidcVerificationError("email이 비어 있음 — 빈 이메일은 신원으로 못 씀")

    matched = [
        uid
        for uid in registry.user_ids()
        if registry.get_user(uid).email == claims.email
    ]

    if len(matched) == 1:
        return matched[0]
    if len(matched) == 0:
        raise OidcVerificationError(
            f"registry에서 email {claims.email!r}과 일치하는 User 없음"
        )
    raise OidcVerificationError(
        f"email {claims.email!r}이 복수 User와 매칭(모호) — 거부"
    )
