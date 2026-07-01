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

import json
import time
import urllib.request
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_org_network.registry import Registry

# JWKS TTL 캐시 기본 수명(초). 실 IdP가 Cache-Control로 더 짧게 권할 수 있으나 과설계 없이
# 고정 기본 + kid-미스 재fetch(키 롤테이션 대응) 두 규칙만 둔다(ADR 0021 결정 1 정신).
_DEFAULT_JWKS_TTL_SECONDS = 3600.0


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


def _urllib_jwks_fetcher(jwks_uri: str) -> dict[str, Any]:
    """기본 JWKS fetcher — stdlib urllib로 JWKS JSON을 GET한다(새 네트워크 의존성 0).

    실 네트워크·비결정(게이트 밖). 테스트는 fixture fetcher를 주입해 이 함수를 우회한다
    (`jwks_fetcher` seam — `SubprocessGitGateway`가 subprocess를 주입 seam으로 격리한 정신).
    fetch·파싱 실패는 `OidcVerificationError`로 감싸 web 401 매핑을 보존한다.
    """
    try:
        with urllib.request.urlopen(jwks_uri) as resp:  # noqa: S310 — jwks_uri는 신뢰 구성값
            raw = resp.read()
        parsed: Any = json.loads(raw)
    except Exception as exc:  # 네트워크·타임아웃·JSON 파싱 실패 등
        raise OidcVerificationError(f"JWKS fetch 실패({jwks_uri!r}): {exc}") from exc
    if not isinstance(parsed, dict):
        raise OidcVerificationError(f"JWKS 응답이 JSON 객체가 아님: {jwks_uri!r}")
    return _as_str_dict(cast("dict[object, object]", parsed))


class HttpOidcProvider:
    """실 JWKS·RS256 검증 `OidcProvider` — 표준 OIDC 제네릭(특정 IdP 가정 없음·ADR 0021 결정 1).

    IdP의 JWKS endpoint에서 공개키를 fetch해 id_token의 RS256 서명을 검증하고 iss/aud/exp/nbf를
    검증한다(실 네트워크·서명·비결정 — `SubprocessGitGateway`의 git subprocess와 같은 결).
    `jwks_url`·`issuer`·`audience`는 사내 IdP 미정이라 **생성자 주입**(특정 공급자 가정 0).

    JWT 검증 = **PyJWT + cryptography**(선택 extra `oidc`로 격리 — anthropic `claude-api`·
    fastembed `dedup` extra와 같은 패턴). 지연 import라 extra 미설치 코어는 안 깨지고, 미설치
    환경에서 이 provider를 *생성*하면 명확한 `SystemExit`으로 안내한다(`FastEmbedEmbedder` 정신).

    주입 seam(결정론 경계):
      - `jwks_fetcher(url) -> dict` — 실 기본은 `_urllib_jwks_fetcher`(stdlib·네트워크). 테스트는
        fixture fetcher를 주입해 네트워크 0으로 JWKS dict를 공급한다.
      - `clock() -> float` — epoch 초. exp/iat/nbf 검증을 결정론으로 만든다(기본 `time.time`).

    JWKS 캐싱(과설계 금지 — 이 두 규칙만):
      1. TTL 캐시 — `clock` 기준 `jwks_ttl_seconds` 동안 fetch 결과를 재사용.
      2. kid 미스 시 1회 재fetch — 키 롤테이션 대응. 재fetch 후에도 없으면 검증 실패.

    **실 IdP 연동(redirect/PKCE code flow·refresh)은 이번 슬라이스 범위 밖** — id_token *검증*까지가
    이번 슬라이스다(웹 로그인 flow 자체는 기존 `/login/sso`가 id_token을 받는 구조 그대로).
    code flow·redirect는 사내 IdP 결정 후 후속이다.
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        *,
        jwks_fetcher: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
        jwks_ttl_seconds: float = _DEFAULT_JWKS_TTL_SECONDS,
    ) -> None:
        # PyJWT+cryptography 지연 import(oidc extra) — 미설치면 명확한 SystemExit 안내.
        # 모듈 상단 import가 아니라 생성 시점 import라 extra 미설치 코어/게이트는 안 깨진다.
        try:
            import jwt

            _ = jwt.__version__  # 설치·로드 확인(실사용 API는 verify에서 지연 import)
        except ImportError as exc:
            raise SystemExit(
                "HttpOidcProvider엔 PyJWT+cryptography가 필요합니다 — oidc extra를 설치하세요: "
                "pip install 'agent-org-network[oidc]'  (uv: uv sync --extra oidc)"
            ) from exc

        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self._jwks_fetcher = jwks_fetcher or _urllib_jwks_fetcher
        self._clock = clock
        self._jwks_ttl = jwks_ttl_seconds
        # TTL 캐시 상태 — (fetched_at, JWKS dict). None이면 미fetch.
        self._cache: tuple[float, dict[str, Any]] | None = None

    # ── JWKS 캐시 ────────────────────────────────────────────────────────────

    def _fetch_and_cache(self) -> dict[str, Any]:
        jwks = self._jwks_fetcher(self.jwks_url)
        self._cache = (self._clock(), jwks)
        return jwks

    def _get_jwks(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """캐시된 JWKS를 돌려주되, 만료·강제 시 재fetch한다(규칙 1: TTL)."""
        if force_refresh or self._cache is None:
            return self._fetch_and_cache()
        fetched_at, jwks = self._cache
        if self._clock() - fetched_at >= self._jwks_ttl:
            return self._fetch_and_cache()
        return jwks

    def _find_signing_key(self, id_token: str) -> Any:
        """id_token의 kid에 맞는 서명 공개키를 찾는다(규칙 2: kid 미스 시 1회 재fetch).

        캐시된 JWKS에서 kid를 찾고, 없으면 키 롤테이션으로 보고 1회 재fetch 후 재시도한다.
        재fetch 후에도 없으면 `OidcVerificationError`.
        """
        import jwt

        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise OidcVerificationError(f"id_token 헤더 파싱 실패: {exc}") from exc
        kid_raw = header.get("kid")
        kid = kid_raw if isinstance(kid_raw, str) else None

        # 1차: 캐시(또는 최초 fetch)에서 kid 조회.
        jwks = self._get_jwks()
        key = self._select_key(jwks, kid)
        if key is not None:
            return key

        # kid 미스 → 키 롤테이션 대응 1회 재fetch.
        jwks = self._get_jwks(force_refresh=True)
        key = self._select_key(jwks, kid)
        if key is not None:
            return key

        raise OidcVerificationError(
            f"JWKS에서 kid {kid!r}에 맞는 서명 키를 찾지 못함(재fetch 후에도 미존재)"
        )

    @staticmethod
    def _select_key(jwks: dict[str, Any], kid: str | None) -> Any:
        """JWKS dict에서 kid에 맞는 JWK를 PyJWT 공개키 객체로 변환한다(없으면 None).

        kid가 없으면(단일 키 IdP) 첫 키를 쓴다 — 다중 키에서 kid 없으면 매칭 불가로 본다.
        반환은 PyJWT RSA 공개키(`decode`의 key 인자로 그대로 전달) — 포트 밖 내부 타입이라 Any.
        """
        from jwt.algorithms import RSAAlgorithm

        raw_keys: object = jwks.get("keys")
        if not isinstance(raw_keys, list):
            return None
        keys = cast("list[object]", raw_keys)
        chosen: dict[str, Any] | None = None
        for item in keys:
            if not isinstance(item, dict):
                continue
            jwk = _as_str_dict(cast("dict[object, object]", item))
            if kid is not None:
                if jwk.get("kid") == kid:
                    chosen = jwk
                    break
            elif len(keys) == 1:
                chosen = jwk
                break
        if chosen is None:
            return None
        return RSAAlgorithm.from_jwk(json.dumps(chosen))

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self, id_token: str) -> OidcClaims:
        """id_token의 RS256 서명·iss/aud/exp/nbf를 검증하고 `OidcClaims`로 매핑한다.

        절차: kid로 JWKS 서명 키 선택(캐시·롤테이션 재fetch) → PyJWT `decode`로 서명·iss·aud·
        exp·nbf 검증(`clock`으로 결정론) → 표준 claim을 `OidcClaims`로 투영. 실패(서명 위조·
        만료·iss/aud 불일치·형식 오류·키 미스)는 전부 `OidcVerificationError`(web 401 매핑 보존).
        """
        import jwt

        signing_key = self._find_signing_key(id_token)
        try:
            payload: dict[str, Any] = jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_signature": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    # exp/nbf/iat 시간 검증은 PyJWT(내부적으로 time.time 사용)에 맡기지 않고
                    # 주입 clock으로 직접 한다(_verify_time_claims) — 결정론 보장(clock seam).
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise OidcVerificationError(f"id_token 검증 실패: {exc}") from exc

        self._verify_time_claims(payload)
        return _claims_from_payload(payload)

    def _verify_time_claims(self, payload: dict[str, Any]) -> None:
        """exp/nbf를 주입 clock 기준으로 검증한다(결정론 — PyJWT의 time.time 우회).

        `require`가 exp 존재를 이미 강제하므로 exp는 있다고 본다. nbf는 옵셔널(있으면 검증).
        leeway 0 — 슬라이스 단순화(clock seam으로 테스트가 시각을 정확히 통제).
        """
        now = self._clock()
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and now >= exp:
            raise OidcVerificationError("id_token 검증 실패: 토큰 만료(exp)")
        nbf = payload.get("nbf")
        if isinstance(nbf, (int, float)) and now < nbf:
            raise OidcVerificationError("id_token 검증 실패: 아직 유효하지 않음(nbf)")


def _as_str_dict(item: dict[object, object]) -> dict[str, Any]:
    """JWK dict의 키를 str로 좁힌다(JSON 파싱 결과라 키는 사실상 str)."""
    return {str(k): v for k, v in item.items()}


def _claims_from_payload(payload: dict[str, Any]) -> OidcClaims:
    """검증 통과한 JWT payload를 `OidcClaims`로 매핑한다(표준 claim만·공급자 중립).

    `aud`는 표준상 str 또는 list일 수 있으나(PyJWT가 audience 일치를 이미 검증) 값 객체엔
    구성된 단일 audience 문자열이 들어가도록 정규화한다. `email`·`email_verified`는 IdP가
    안 보낼 수 있어 안전 기본값(빈 문자열·False)으로 떨어뜨린다 — resolve_identity가 거른다.
    """
    aud_raw: object = payload.get("aud", "")
    if isinstance(aud_raw, list):
        aud_list = cast("list[object]", aud_raw)
        aud = str(aud_list[0]) if aud_list else ""
    else:
        aud = str(aud_raw)
    return OidcClaims(
        sub=str(payload.get("sub", "")),
        email=str(payload.get("email", "")),
        email_verified=bool(payload.get("email_verified", False)),
        iss=str(payload.get("iss", "")),
        aud=aud,
    )


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
