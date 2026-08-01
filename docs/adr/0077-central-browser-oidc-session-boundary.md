# ADR 0077 — Central browser OIDC와 digest-only 세션 경계

- 상태: Implemented (RB3.2b.2 A–E; actual IdP/TLS Manual·support 승격·Full Gate는 RB3.7/8 미완료)
- 날짜: 2026-07-31
- 관련: ADR 0004 (중앙 Authority), ADR 0021 (OIDC identity binding), ADR 0024 (stateful session),
  ADR 0064 (Registry User admission), ADR 0075 (세 installation artifact), ADR 0076 (bootstrap)

## 맥락

RB3.2b.1은 Central Next artifact와 child-process 경계만 제공한다. 기존 개발 화면의 identity
picker, passwordless session, localStorage identity 또는 request의 User/org/role/token 자기보고를
Central Server browser 신원으로 해석하면 OIDC, Registry User, Authority의 순서가 무너진다.

반대로 최초 root User admission을 일반 browser login에 섞으면 one-time device authorization과
Bootstrap Admin Attestation의 재현·감사 경계가 사라진다. 추가 Registry User admission은 아직
RB3.2b.3의 별 command이므로, browser SSO는 이미 등록된 Registry User의 세션만 만들 수 있어야
한다.

## 결정

RB3.2b.2는 authorization-code + PKCE browser transaction과 opaque Central session을 Central
API의 별 bounded context로 추가한다. Central Next는 public same-origin BFF, redirect와 cookie
relay만 담당한다. issuer discovery, authorization URL 구성, PKCE, code exchange, token/claim
검증, Registry User resolution, Authority 재인가, session persistence는 모두 Central API가
소유한다. Central Next는 issuer 또는 OIDC token endpoint에 직접 연결하지 않는다.

### 정확한 browser/BFF route 네 개

Central Next와 Central API 사이에는 다음 네 route만 존재한다. generic `/api/[...path]`,
Developer API route, Owner API, raw/draft/A2A route는 이 slice의 BFF fallback이 아니다.

| browser → Central Next | fixed Central API upstream | method | 의미 |
|---|---|---|---|
| `/api/auth/login/start` | `/v1/browser-auth/login/start` | `POST` | same-origin login form을 검증하고 issuer authorization endpoint로 `303` redirect한다. |
| `/api/auth/callback` | `/v1/browser-auth/callback` | `GET` | IdP callback을 transaction cookie와 state에 결박해 처리하고 Central relative location으로만 `303`한다. |
| `/api/auth/session` | `/v1/browser-auth/session` | `GET` | current Central browser session의 safe projection을 읽는다. |
| `/api/auth/logout` | `/v1/browser-auth/logout` | `POST` | 현재 browser session의 local 종료를 단조롭게 수행한다. |

Next는 fixed `http://127.0.0.1:8010`에만 위 method/path/cookie와 아래 허용 request header를
forward한다: `Cookie`, `Origin`, `Sec-Fetch-Site`, `Sec-Fetch-Mode`, `Sec-Fetch-Dest`,
`X-AON-CSRF`. `Authorization`, forwarded-host/proto, user/org/role/permission/token claim header와
임의 request header는 forward하지 않는다. 응답은 `Location`, `Set-Cookie`, `Cache-Control`,
`Content-Type`만 relay하며 upstream error body를 새 shape로 바꾸지 않는다. callback의 redirect
목적지는 Central public origin의 relative path만 허용하고, 외부 absolute URL·request supplied
host·`return_to`는 허용하지 않는다.

### Central profile과 redirect URI

strict Central profile은 기존 identity verification field와 `central_public_origin`에 더해 정확히
아래 browser OIDC field 네 개를 가진다.

| field | 제약 |
|---|---|
| `browser_oidc_authorization_url` | absolute HTTPS URL, configured issuer와 canonical origin/path exact match |
| `browser_oidc_token_url` | absolute HTTPS URL, configured issuer와 canonical origin/path exact match |
| `browser_oidc_client_id` | non-empty public-client identifier; client secret field는 없다 |
| `browser_oidc_scope` | exact space-separated `openid email`; caller override·additional scope는 없다 |

redirect URI는 profile의 별 URL field가 아니다. Central API는 exact
`central_public_origin + "/api/auth/callback"`만 계산하고 authorization request와 token exchange에
같은 값을 쓴다. `central_public_origin`은 HTTPS scheme, host, optional non-default port만 가지며
path/query/fragment·HTTP·caller environment override를 허용하지 않는다. 이 URI는 IdP에 사전 등록해야
하지만 실제 test IdP/TLS browser 관통은 RB3.7/8 Manual Acceptance다.

### transaction과 session의 digest-only 모델

Central durable schema, application command/result, Repository port, audit/outbox/receipt projection에는
raw transaction handle, raw session identifier, authorization code, PKCE verifier, token, OIDC claim
원문을 저장하거나 반환하지 않는다. durable identifier와 authenticated principal key는 lower-case
SHA-256 `transaction_digest`/`session_digest`뿐이다.

`BrowserOidcTransaction`은 `transaction_digest`, configured provider/redirect digest,
`state_digest`, `nonce_digest`, `created_at`, `expires_at`, `consumed_at`과 terminal reason만 가진다.
`BrowserSession`은 `session_digest`, `registry_user_id`, `org_id`, OIDC identity binding digest,
`csrf_digest`, `established_at`, `expires_at`, `ended_at`와 local terminal reason만 가진다. session
principal은 raw cookie ID가 아니라 `BrowserSessionPrincipal(session_digest, registry_user_id, org_id)`로
application에 전달한다. Registry User ID는 application 내부 authorization resource이며 browser
safe projection이 raw OIDC identity를 대신하지 않는다.

HTTP cookie codec과 OIDC transport adapter만 raw material을 process memory에서 잠시 다룬다.
`__Host-aon-central-oidc-tx`에는 256-bit random opaque transaction handle만,
`__Host-aon-central-session`에는 별 256-bit random opaque session handle만 HttpOnly로 둔다. 이
handle은 signed/encrypted payload가 아니며 충분한 entropy와 server-side digest lookup으로만
해석된다. 각 DB row는 raw handle이 아니라 그 SHA-256 digest만 가진다. callback 뒤
`__Host-aon-central-oidc-tx`는 즉시 expire한다. `__Host-aon-central-csrf`는 session-bound CSRF
token만 담는 readable cookie이며 DB에는 그 digest만 남는다. 이 예외적인 browser cookie wire 외
raw value는 log, traceback, audit, outbox, receipt, JSON response 또는 Central Next
state/localStorage에 egress하지 않는다.

RB3.2b.2는 encrypted/stateless transaction cookie codec, cookie key file, environment secret,
key generation 또는 key rotation을 만들지 않는다. raw PKCE verifier는
`BrowserPkceVerifierVault`가 transaction digest로만 index한 bounded process-memory entry다.
vault는 Central API process당 최대 1,024 pending transaction, transaction TTL 이하의 expiry만
허용하며 callback consume/expiry/eviction 때 best-effort zeroize 후 제거한다. start는 vault capacity를
확보하기 **전에** durable transaction을 만들지 않으며, capacity가 없으면 `503
browser_session_unavailable`로 write 0이다. nonce는 durable `nonce_digest`와 verified OIDC claim의
digest compare로 검증하므로 vault에 저장하지 않는다.

Central API restart, vault eviction 또는 vault fault 뒤에는 durable unconsumed transaction과 tx
cookie가 남아도 verifier가 없으므로 callback은 session/Registry/audit/outbox write 0으로
`503 browser_session_unavailable`을 반환하고 tx cookie를 expire한다. orphan transaction은 TTL로만
만료하며 callback retry/replay가 session을 만들 수 없다; browser는 새 login을 시작해야 한다. 이는
local-reference single Central API process의 명시적 제한이며 real restart/retry 관통은 RB3.7/8
Manual Acceptance에서 다룬다. encrypted cookie/key lifecycle과 multi-instance transaction vault는 P1
후속 검토이고, 이 slice가 `data_directory`에 secret을 자동 생성하거나 읽는 경로는 없다.

세 cookie는 모두 `Secure; Path=/`이고 `Domain`이 없어 `__Host-` prefix 조건을 만족한다. session과
transaction cookie는 `HttpOnly; SameSite=Lax`, CSRF cookie는 `SameSite=Strict`이며 session/transaction의
`Max-Age`는 Central policy의 bounded TTL을 넘지 않는다. HTTP/localhost에서 `__Host-` cookie를
느슨하게 대체하지 않는다. browser OIDC의 실제 loopback TLS path는 RB3.7/8에서만 manual로 증명한다.

### application과 port

Central composition은 다음의 narrow port를 둔다. session/transaction repository와 application
DTO는 digest-only다. `OidcAuthorizationCodeExchangePort`만 callback 순간의 code/verifier를 process
memory input으로 받고, verified result를 durable write 전에 verifier에 넘긴다. 그 port는 token,
claim, code 또는 verifier를 결과·exception text·telemetry로 egress하지 않는다.

1. `BeginBrowserOidcSession`은 in-memory PKCE verifier vault capacity를 먼저 확보하고 random
   transaction handle의 digest와 PKCE challenge/state/nonce를 발급한 뒤 unconsumed transaction을
   durable create한다. HTTP adapter만 opaque tx cookie와 issuer redirect를 만든다.
2. `CompleteBrowserOidcSession`은 callback cookie/state digest, unexpired/unconsumed transaction,
   PKCE/nonce 및 verified issuer/audience/signature/expiry/email_verified proof를 대조한다. 그 뒤
   verified identity를 **기존** Registry User에 exact resolve한다.
3. `ReadBrowserSession`은 cookie digest를 `BrowserSessionPrincipal`로 해석한 뒤 매 요청 현재
   Registry User binding과 `session.read` Authority를 다시 읽는다.
4. `EndBrowserSession`은 cookie digest와 session CSRF만으로 해당 local session/transaction cookie를
   종료한다. remote global logout 또는 IdP token revocation은 이 slice 범위 밖이다.

JIT provisioning, invitation, bootstrap substitute, unknown OIDC identity의 Registry write, body/header/
query self-claim은 없다. existing Registry User가 아니거나 org/binding이 달라지면 session은 만들지
않고 `403 registry_user_not_admitted`로 닫는다. 최초 root User는 계속 ADR 0076 device flow만 쓴다.

### Authority와 atomic establishment

OIDC verification이 곧 browser session authorization은 아니다. `CompleteBrowserOidcSession`은 다음
순서를 반드시 지킨다.

1. transaction의 digest/state/expiry/replay와 OIDC proof를 검증한다.
2. verified identity를 existing Registry User로 resolve하고 current Authority의 `session.establish`를
   확인한다. 이 확인 이전에는 session row를 write하지 않는다.
3. Central transaction을 시작해 Registry binding, transaction unconsumed 상태, Authority policy epoch와
   `session.establish`를 다시 읽는다.
4. mutation 직전 같은 current Authority가 다시 허용할 때만 session digest row를 create하고 transaction을
   consumed로 mark하여 함께 commit한다.

step 2와 step 4 사이 revoke, Registry binding change, policy drift, transaction replay/expiry,
unavailable read-back은 write 없이 deny/unavailable로 끝난다. transaction cookie만 존재해도 durable
session은 생기지 않는다. `ReadBrowserSession`은 every request에 current Registry binding과
`session.read` Authority를 재검증하며 deny/revoke/unavailable면 authenticated projection을 주지 않는다.

`EndBrowserSession`은 의도적으로 Authority read를 성공 조건으로 삼지 않는다. current policy가
revoke되었거나 unavailable인 경우에도 same session digest의 **삭제/종결만** atomic하게 진행하고 세
cookie를 expire한다. 종료는 active state를 되살리거나 다른 session을 건드리거나 권한을 넓힐 수 없는
monotonic cleanup이므로 `204`는 missing/expired/already-ended에도 동일하다. 이는 remote global
revoke, 다른 device session 종료, IdP logout endpoint 호출을 뜻하지 않는다.

### browser origin·Fetch Metadata·CSRF

`POST /api/auth/login/start`와 `POST /api/auth/logout`는 Central Next와 Central API 양쪽에서
`Origin == central_public_origin`, `Sec-Fetch-Site == same-origin`을 검증한다. login start는 외부 IdP의
`303`으로 브라우저 전체가 이동해야 하므로 JavaScript fetch가 아니라 native same-origin `POST` form의
top-level navigation만 허용하며 exact `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`를 요구한다.
logout은 same-origin fetch이므로 exact `cors`/`empty`를 유지한다. missing, `null`, malformed, cross-site,
same-site-but-not-same-origin 값은 fail-close한다.
`session.end`는 추가로 readable CSRF cookie의 raw value와 `X-AON-CSRF` header를 exact compare하고
stored `csrf_digest`까지 constant-time 검증한다. session cookie가 없거나 CSRF mismatch여도 error는
session existence를 노출하지 않는 `403 browser_csrf_forbidden`이다. OIDC callback만 IdP top-level
navigation을 허용하므로 Origin/Fetch Metadata 대신 transaction cookie, state digest, PKCE, nonce,
one-time consumption과 fixed redirect URI를 모두 요구한다.

`GET /api/auth/session` 응답은 `Cache-Control: no-store`이며 token, claim, email, subject, session handle,
transaction/state/nonce/CSRF raw value를 포함하지 않는다. active safe projection은 authenticated flag,
Registry User opaque reference와 Central action projection만 반환한다. invalid/expired/revoked session은
`401 browser_session_unauthenticated`, current Authority denial은 `403 browser_session_forbidden`,
dependency/policy unavailable은 `503 browser_session_unavailable`이다. callback transaction invalid/replay/
expiry는 `400 browser_oidc_callback_invalid`; OIDC proof invalid는 `401 browser_oidc_unauthenticated`;
internal exchange/store faults는 `503 browser_session_unavailable`이다. 상세 원인, raw OIDC material,
다른 Registry User/session 존재는 어느 error에도 없다.

## 검증과 범위

RB3.2b.2는 아래 유한 A–E로 종료한다.

| slice | 결정론 gate |
|---|---|
| A | digest-only value/port/schema와 strict profile four field·redirect derivation Contract |
| B | strict profile/redirect, digest-only transaction/session schema·store, bounded verifier vault, marker-last/fault/restart Contract |
| C | fake/HTTP OIDC code provider, PKCE start/callback application, existing Registry User resolution, Authority establish-before-write·transaction precommit recheck Contract |
| D | `session.read` every-request current Authority, `session.end` CSRF/origin/Fetch Metadata와 monotonic cleanup Contract |
| E | exact four-route Central BFF allowlist/cookie relay/no localStorage/demo/JIT/raw-secret egress, Fast+Contract+Affected and independent review |

test IdP with real browser redirect, public/loopback TLS and actual `__Host-` cookie behavior is Manual
Acceptance retained for RB3.7/8. RB3.2b.2 completion does not add an additional Registry User, Agent Card,
`/ask`, `/inbox`, `/admin`, `/console`, Owner endpoint, Question User MCP pairing or remote global revoke.
It does not promote `docs/support-contract.json`, 3-install status, `runnable_local_reference`, production/
pilot status or schedule a Full Gate before RB3.8.

## 결과

Central browser SSO gains one verifiable, server-derived session boundary without using the bootstrap device
flow as a web login or treating an IdP identity as automatic organization membership. The next RB3.2b.3 slice
may explicitly add session-derived Registry User admission, but cannot weaken this route, digest, Authority,
cookie or no-egress contract.
