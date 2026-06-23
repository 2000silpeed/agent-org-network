# SSO/OIDC 신원 binding — 세션에 박기 전 "신원을 *선택*"하던 한 점을 "IdP가 *증명*한 신원만"으로 교체한다

상태: accepted (2026-06-23) · **구현 완료(tdd-engineer red→green 슬라이스 1~3 + code-reviewer 리뷰 수정[빈 email 가드 m1] — 734 passed/pyright 0/ruff 0; 실 `HttpOidcProvider`·JWKS·redirect는 게이트 밖 후속)** · **ADR 0016의 *위에 얹는* 결정(폐기 아님 — 무비밀번호는 OFF/하위호환 모드로 보존)** · **ADR 0017 결정 5의 *증명* 미싱피스를 채움**(owner 종속 = 선언[card.owner 중앙]·강제[편집 스코프 403]·증명[신원=SSO] 셋 중 *증명* — 지금 무비밀번호라 누구나 cs_lead로 들어옴) · ADR 0009(페르소나별 면 분리)·git_gateway.py `GitGateway`/`FakeGitGateway`(포트+Fake 패턴 본보기)와 정합 · Phase 7 T7.1의 설계·shape를 닫는다.

## 맥락 — 끊어진 고리 하나

ADR 0017 결정 5는 owner를 특정 에이전트에 묶는 사슬을 *선언·강제·증명* 셋으로 분해했다. 둘은 이미 산다 — **선언**은 `AgentCard.owner`를 레지스트리가 검증(Authority 중앙·불변식), **강제**는 빌더 편집 스코프(세션 신원 ≠ card.owner → 403, ADR 0016·0018 결정 5). 비어 있는 건 **증명**이다. 지금 운영 면 로그인은 무비밀번호(ADR 0016) — `POST /login`에 `user_id`만 주면 Registry에 실재하기만 하면 누구나 "cs_lead"로 들어온다. *신원 선택*이지 *신원 증명*이 아니다. 그래서 card.owner 종속은 명목이고, 빌더가 박는 커밋 author·답 귀속도 결국 "자칭"이다.

핵심 통찰은 ADR 0016이 이 고리를 *이미 좁혀 놨다*는 데 있다. ADR 0016 결정 3이 신원 출처를 path/body에서 **세션**으로 옮기며 `_session_identity(request)` 한 곳으로 격리했다. 그래서 SSO는 **"세션에 박기 전 검증" 단계 하나만 교체**하면 된다 — `_session_identity`·운영 스코프·concur/inbox/manager·빌더 OKF 커밋 author는 *전부 무변경*으로 재사용된다(세션 키에 들어간 `user_id`가 진짜 IdP가 증명한 신원이라는 것만 달라진다). SSO가 바꾸는 건 단 한 점: `POST /login`의 "아무 user_id나 *선택*"을 "IdP가 *증명*한 신원만 세션에 박기"로.

설계 제약 둘이 범위를 좁힌다:

1. **단일 회사 사내(single tenant).** 한 조직의 IdP, owner = 그 회사 직원. tenant 개념·tenant 격리·tenant 프리픽스·tenant 스코프 user_id는 *불필요*하다 — 만들지 않는다.
2. **결정론 게이트.** 실 IdP 연동(JWKS fetch·RS256 서명 검증·code/PKCE exchange·redirect·refresh)은 비결정·외부 의존·새 무거운 의존성이라 게이트 밖 수동이다. 게이트 안 결정론은 `FakeOidcProvider` 주입까지 — `GitGateway`↔`FakeGitGateway`·`AgentRuntime`↔`StubRuntime`과 **같은 포트 패턴**.

## 결정

### 1. `OidcProvider` 포트(Protocol) — 공급자 중립, 표준 claim만

신원 *검증*은 비결정·외부 의존(IdP·JWKS·서명)이라 `GitGateway`·`AgentRuntime`·`ClaudeRunner`와 **같은 포트 패턴**으로 격리한다:

- **`OidcProvider` 포트(Protocol)** — `verify(id_token: str) -> OidcClaims`. id_token의 서명·만료·audience를 검증하고, 통과하면 표준 claim을 담은 `OidcClaims`를, 실패(만료·잘못된 서명·aud 불일치·형식 오류)하면 `OidcVerificationError`를 올린다. 검증은 *불투명 토큰 → 신뢰할 수 있는 claim*의 변환이다.
- **`FakeOidcProvider`**(결정론·게이트 내) — in-memory 토큰→claims 맵을 들고, 등록된 토큰엔 그 claims를, 모르는/위조/만료 토큰엔 `OidcVerificationError`를 낸다. 실 서명·네트워크 0이라 단위 테스트가 결정론(`FakeGitGateway`의 결정 SHA와 같은 결).
- **`HttpOidcProvider`**(실 구현·게이트 밖) — JWKS fetch·RS256 서명 검증·iss/aud/exp 검증을 하는 실 구현. 새 무거운 의존성(`python-jose`·`authlib` 등)을 더할지는 **tdd-engineer/후속이 판단**한다(이 ADR은 자리만, shape는 `NotImplementedError`). `SubprocessGitGateway`가 게이트 밖 수동인 것과 동형.
- **공급자 중립** — 표준 OIDC claim(`sub`·`email`·`email_verified`·`iss`·`aud`)만 가정한다. 공급자별 특수 claim(Google `hd`·Microsoft `tid`)은 *가정하지 않는다*. 그래서 포트가 어떤 OIDC IdP에도 붙는다.

### 2. `OidcClaims` — frozen 값 객체(전송 DTO 아님)

검증을 통과한 신뢰할 수 있는 claim의 도메인 값 객체:

- 필드 = `sub`·`email`·`email_verified: bool`·`iss`·`aud`. 공급자별 특수 claim 없음(공급자 중립).
- **`exp`는 값 객체에 넣지 않는다.** 만료는 *검증의 책임*(`verify`가 통과시킬지 말지)이지 검증 통과 후의 도메인 값이 아니다 — `OidcClaims`는 "이미 유효성 검증을 통과한 신원"을 표상한다. 만료된 토큰은 `verify`에서 `OidcVerificationError`로 걸러져 애초에 `OidcClaims`가 안 나온다. (`Answer`가 `snapshot_sha`는 들되 검증용 메타는 안 드는 것과 같은 경계.)
- `frozen` pydantic 값 객체다 — 전송 와이어 DTO(`LoginRequest`·전송 프레임)가 아니라 *도메인이 다루는 신원 값*이다. 매핑(`resolve_identity`)이 이 값을 받아 registry user_id로 변환한다.

### 3. 신원 매핑 = verified email → registry User (`resolve_identity`, 순수 함수) — **baseline 선택**

검증된 claim을 registry user_id로 잇는 가장 단순한 닫힌 루프를 고른다. **채택 baseline = verified email 매핑**:

- **`User`에 `email: str | None = None` 추가**(frozen·하위호환 기본 None·admission 무관 — User는 카드가 아니다). 데모 User 6명에 회사 이메일을 부여한다(예: `cs_lead@example.com`). User email 기본이 None이라 *기존 User 생성·기존 테스트는 무영향*이고, 데모 데이터만 추가된다.
- **`resolve_identity(claims, registry) -> str`**(순수 함수·web 분리 경계 — `validate_card_for_builder`·`serialize_reply`와 같은 결): ① `claims.email_verified`가 True가 아니면 거부(미검증 이메일은 신원으로 못 쓴다) ② `claims.email == user.email`인 User를 registry에서 찾는다 ③ 정확히 1명이면 그 user_id, 0매칭이면 거부(인증 실패). 순수 함수라 결정론 단위 테스트.

**왜 이 baseline인가.** User.email이 *단일 진실 원천*이 된다 — 매핑 테이블을 따로 주입하면 SSOT가 이중화(User.email ↔ 별도 테이블)되고 드리프트가 생긴다. email 매핑은 표준 claim(`email`·`email_verified`)에 그대로 얹혀 공급자 중립과 정합한다.

#### Considered Options (매핑 baseline)
- **verified email → User.email 매핑(선택)** — User.email이 SSOT, 표준 claim에 정합, `email_verified` 가드가 자연스럽다. 데모 User에 회사 이메일만 부여하면 닫힌 루프.
- **email local-part == user_id(기각)** — `cs_lead@company.com`의 local-part `cs_lead`를 user_id로 본다. 더 짧지만 *회사 이메일 규칙에 종속*된다 — 실제 회사는 대개 `firstname.lastname@company.com` 꼴이라 local-part가 user_id(`cs_lead`)와 무관해 깨진다. 데모 데이터에만 맞는 우연한 단순함이라 견고하지 않다.
- **외부 매핑 테이블 주입(기각)** — `{email: user_id}` dict를 주입. User.email과 *별도의 신원 SSOT*가 생겨 이중화·드리프트. User에 email 한 필드를 더하는 쪽이 모델 한 곳에 신원을 모은다.

### 4. 인증 모드 3단 — 점진 전환·하위호환

`session_secret`·`oidc_provider` 주입 조합으로 모드가 갈린다:

| 모드 | 주입 | `POST /login` | `POST /login/sso` | 용도 |
|---|---|:---:|:---:|---|
| **① OFF** | (없음) | 인증 OFF(세션 미부착) | 404 | 데모·하위호환(ADR 0016 결정 OFF 모드) |
| **② 무비밀번호** | `session_secret`만 | ✅ 신원 *선택*(ADR 0016) | 404 | ADR 0016 기존 동작 보존(하위호환) |
| **③ SSO** | `session_secret` + `oidc_provider` | **거부**(403/404) | ✅ 신원 *증명* | T7.1 — owner 종속 실재화 |

- `create_app`·`create_central_app`에 **`oidc_provider: OidcProvider | None = None`** 주입 인자를 더한다(미주입이면 기존 동작 — 하위호환).
- **③ SSO 모드면 무비밀번호 `POST /login`을 거부**한다. SSO를 켰는데 무비밀번호 `/login`이 살아 있으면 "신원 *선택* 우회"로 SSO가 무의미해진다 — 증명 채널을 켜면 선택 채널을 닫아야 owner 종속이 실재한다. 거부 방식은 **`/login` 핸들러에서 oidc_provider 주입 시 403**(명시적 "SSO 모드에선 /login/sso를 쓰라" 신호 — 404로 "없는 엔드포인트"인 척하기보다, 라우트는 있으나 모드상 거부가 정직하다). ADR에 박는다.
- `POST /logout`은 세 모드 공통(세션 클리어 — IdP 로그아웃 연동은 후속).

### 5. 신원 출처 불변 — ADR 0016 재사용(왜 무변경인가)

SSO로 얻은 user_id를 **기존 세션 키 `_SESSION_USER_KEY`에 그대로 박는다.** 이후 `_session_identity`·운영 스코프·concur·inbox/manager·빌더 OKF 커밋 author는 *전부 무변경*이다. 가능한 이유는 ADR 0016 결정 3이 *신원 출처를 세션으로 이미 격리*했기 때문이다 — 운영 면은 신원을 항상 세션에서만 읽고(path/body 아님), 세션 키에 들어간 값이 "선택된 user_id"든 "증명된 user_id"든 *읽는 쪽은 똑같다*. SSO는 그 값을 *세션에 박기 직전의 검증*만 바꾼다. 헥사고날 정신 — `_session_identity`가 "세션 신원 1개"라는 추상만 보므로 그 아래 검증 메커니즘이 무비밀번호→SSO로 바뀌어도 엔드포인트가 안 흔들린다.

ADR 0018 결정 5("커밋 author = 세션 신원, T7.1 SSO 전")가 열어둔 자리가 정확히 여기서 채워진다 — 빌더 OKF 커밋 author는 코드 변경 0으로 *증명된* 회사 신원이 된다.

### 6. 불변식 영향 없음 — owner 종속은 *강화*

- **미아 없음** — SSO는 *접근 경계*(누가 로그인하나)이지 라우팅·답 도메인이 아니다. 0매칭→루트 escalation은 그대로.
- **Authority 중앙** — 권한은 여전히 `routing_rules.yaml`·card.owner 중앙 선언이다. SSO는 *신원 증명*이지 권한 선언이 아니다(카드 자기보고 금지 무관).
- **전이 ≠ 기록** — 로그인은 세션 set이지 도메인 전이가 아니다.
- **등록 무결성** — `User.email`은 카드 admission과 무관하다(User는 카드가 아니다 — `Registry.validate`는 card.owner 실재·manager 그래프만 검증). email 추가가 admission을 흔들지 않는다.
- **노출 불변식** — 인증은 접근 경계, 채팅 OrgReply 노출 불변식 무관. `OidcClaims`·`sub`·email은 *증명 내부값*이라 세션에 user_id만 박고 claims를 사용자向으로 흘리지 않는다.
- **owner 종속 강화** — 선언(card.owner)·강제(편집 스코프)는 그대로, *증명*(신원)이 선택→증명으로 채워져 종속이 실재화된다.
- **RBAC 아님** — ADR 0016 결정 5의 "역할은 그래프 파생·깊은 RBAC 없음"이 그대로다. SSO는 *신원 증명*이지 역할 부여가 아니다 — 증명된 user_id가 어떤 면을 보는지는 여전히 그래프(owns/manages)가 정한다.

## Considered Options (요약)

### 신원 공급자 (결정 1)
- **범용 OIDC(공급자 중립) 포트 + 표준 claim(선택)** — 표준 claim만 가정해 어떤 IdP에도 붙는다. `GitGateway`와 같은 Fake/실 분리.
- **특정 공급자 SDK 직접 의존(기각)** — Google/MS SDK에 묶이면 공급자 종속·테스트 비결정. 포트+Fake가 결정론·중립.
- **자작 JWT 검증(기각)** — 서명·JWKS 검증은 보안 민감 코드라 자작 회피(ADR 0016 결정 1-a "서명 코드 자작 회피"와 동형). 실 검증은 검증된 라이브러리(후속 판단)·게이트 밖.

### 매핑 baseline (결정 3) — 위 §3 표 참조.

### SSO 모드에서 무비밀번호 /login (결정 4)
- **403 거부(선택)** — SSO 모드면 선택 채널을 닫아야 종속이 실재. 라우트는 있되 모드상 거부가 정직.
- **/login 라우트 미등록(404)(기각)** — 가능하나 "없는 엔드포인트"인 척이라 디버깅이 헷갈린다. 403 "SSO 모드"가 명확.
- **/login 유지(기각)** — 선택 우회로 SSO가 무의미.

## Consequences

- **`oidc.py` 신규 모듈** — `OidcProvider`(Protocol)·`OidcClaims`(frozen)·`OidcVerificationError`·`FakeOidcProvider`(결정론·구현 완료)·`HttpOidcProvider`(실 검증 — 게이트 밖·`NotImplementedError`)·`resolve_identity`(순수 함수·구현 완료 — email_verified·빈 email·0매칭/모호 거부). `git_gateway.py` 구조를 본보기로.
- **`User`에 `email: str | None = None`** — frozen·하위호환 기본 None. 데모 6명에 회사 이메일 부여(매핑 baseline). 기존 User 생성·테스트 무영향(기본 None).
- **`web.py`에 `POST /login/sso`** — `SsoLoginRequest{id_token}` → `oidc_provider.verify` → `resolve_identity` → 세션에 user_id 박기. SSO 모드 가드(`/login` 403). `create_app`에 `oidc_provider` 인자. `create_central_app`도 인자 자리.
- **인증 모드 3단** — OFF(하위호환)·무비밀번호(ADR 0016 보존)·SSO(신규). 점진 전환·미주입이면 기존 동작.
- **불변식 영향 없음** — 위 결정 6.
- **갱신 대상**: CONTEXT(Authn & planes — `OidcProvider`·`OidcClaims`·`resolve_identity`·SSO 모드·User.email 신규 용어, Operator session 절에 "신원 선택→SSO 증명" 재정의 주석)·PRD §6(SSO 항목 T7.1 진행)·TRD §4(OidcProvider 포트·OidcClaims·resolve_identity·인증 모드 3단)·§5(`/login/sso`)·tasks T7.1(설계·shape + red→green 구현 완료 주석·T7.1 체크박스 [x]).

## Open Questions (게이트 밖·후속)

- **실 OIDC 본체** — JWKS fetch·RS256 서명 검증·iss/aud/exp 검증·authorization code/PKCE exchange·redirect/callback·refresh token·nonce/state CSRF 방어는 `HttpOidcProvider`(게이트 밖·수동). 새 의존성 추가 판단은 tdd/후속.
- **실 IdP 등록** — client_id/secret·redirect URI·discovery endpoint 운영 설정은 배포 영역(env·커밋 금지).
- **세션 만료/롤링** — SSO 세션의 만료 정책·롤링·IdP 토큰 만료와 세션 만료의 동기화는 후속(ADR 0016이 미룬 세션 만료 튜닝의 연장).
- **로그아웃 시 IdP 연동** — `POST /logout`의 IdP single-logout(SLO) 연동은 후속(MVP는 세션 클리어만).
- **이메일 없는 User 매핑 폴백** — `User.email`이 None인 User는 SSO로 매핑 불가(0매칭 거부). sub 기반 매핑·매핑 보강은 후속(데모는 6명 전부 email 부여).
- **다중 IdP** — single tenant 한 IdP가 MVP. 여러 IdP(공급자별 분기)는 범위 밖.
- **CSRF·rate limit** — ADR 0016이 미룬 그대로(state/nonce는 실 redirect 흐름에서·게이트 밖).
