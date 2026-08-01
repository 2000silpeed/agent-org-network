# Agent Org Network TRD v0

- 상태: 실제 실행 기준선
- 기준일: 2026-07-30
- 제품 요구: [`prd-v0.md`](prd-v0.md)
- 실행 지원 SSOT: [`support-contract.json`](support-contract.json)
- 실행 작업: [`tasks-v0.md`](tasks-v0.md)
- Windows/Docker 없는 기본 실행: [ADR 0082](adr/0082-windows-without-docker-runtime-baseline.md)

## 1. 설계 원칙

1. 지원 상태는 코드의 존재가 아니라 사용자가 실행할 수 있는 artifact와 검증된 조건으로
   판정한다.
2. `Central Server`는 목표 제품에만 쓰는 이름이다. 현재 Next와 `web:app`은
   Browser Frontend + Developer API reference이며 `server:central_app`은 Legacy Fixture다.
3. `Agent Card`는 능력과 under-claim을 설명하고, Authority는 중앙 정책만 선언한다.
4. Question Request 전이와 audit/outbox 기록을 분리한다.
5. 원문·전체 초안·Owner Runtime 비밀은 `Card Owner` 경계에 남긴다.
6. production 의존성이 없으면 자동 fallback하지 않고 unavailable로 닫는다.

지원 상태 vocabulary는 다음 다섯 값으로 고정합니다.

- `runnable_developer_reference`
- `runnable_legacy_fixture`
- `installable_dependent_client`
- `tested_component_factory`
- `product_target_not_available`

## 2. 현재 실행 가능한 조립

### 2.1 Developer Reference

| artifact | 상태 | 실행 |
|---|---|---|
| Developer API | `runnable_developer_reference` | `uv run uvicorn agent_org_network.web:app --host 127.0.0.1 --port 8011` |
| Browser Frontend | `runnable_developer_reference` | Next build 뒤 `AON_FRONTEND_MODE=development AON_BACKEND_URL=http://127.0.0.1:8011 pnpm start` |

`web:app`은 질문, blocking/retrieve/SSE와 운영 API를 조립하지만 HTML을 제공하지
않습니다. Browser Frontend는 Next standalone 서버가 소유합니다. 두 artifact는 개발
Registry/설정을 쓰는 reference이며 실제 조직 Authority나 production Central Server를
뜻하지 않습니다.

### 2.2 Legacy Fixture

| 표면 | 상태 | 진입점 |
|---|---|---|
| legacy 중앙 수동 시연 | `runnable_legacy_fixture` | `server:central_app`, `scripts/run_central.sh 8000 127.0.0.1` |
| legacy Owner Worker 수동 시연 | `runnable_legacy_fixture` | `scripts/run_worker.sh cs_lead primary 8000 127.0.0.1` |
| 인프로세스 MCP | `runnable_legacy_fixture` | `mcp_server`, `scripts/run_mcp.sh` |

`server:central_app`은 legacy WebSocket dispatcher와 개발 웹을 함께 조립합니다.
`mcp_server`는 중앙 코드를 인프로세스로 호출하는 개발 fixture입니다. 두 경로 모두 아래의
목표 3-install 배포 경계나 thin remote Question User MCP를 증명하지 않습니다.
Owner Worker는 Owner ID가 필수이며 기본 Runtime을 쓸 때 로컬 `claude` 로그인이
필요합니다. 선택 Runtime은 해당 provider extra와 credential을 별도로 요구합니다.

### 2.3 Installable Dependent Client

| 항목 | 값 |
|---|---|
| 상태 | `installable_dependent_client` |
| 패키지 script | `aon-mcp = agent_org_network.question_user_mcp:main` |
| subcommand | `pair`, `serve-stdio` |
| tool manifest | `ask_org`, `get_question` |
| 외부 조건 | 실제 OIDC, HTTPS question gateway, OS keychain, Registry User/Authority |

`aon-mcp`는 패키지에 존재하고 로컬 help/stdio 진입점이 실행됩니다. 그러나 필요한
production gateway는 이 저장소의 runnable server로 제공되지 않으므로 독립 제품이나
end-to-end 지원 경로가 아닙니다.

## 3. 현재 Question Request 구조

```text
Question User input
  → Question Request 생성
  → exact-only non-actionable intake
       └─ 일치: Declined(non_actionable_conversation)
  → Router
       ├─ Routed → Runtime → Approval boundary → Answer Finalization
       ├─ Contested → Owner 합의 / Manager 처분 → 같은 Request 재개
       └─ Unowned → root User / Manager 처분 → 같은 Request 재개 또는 Declined
```

핵심 application은 저장된 Question Request와 RouteTarget을 기준으로 진행합니다.
blocking, retrieve, SSE, MCP projection은 같은 terminal 의미를 사용해야 합니다.
승인 전 candidate token은 최종 답이 아니며, Answer Finalization commit 뒤의 canonical
결과만 사용자 답으로 간주합니다.

`Non-actionable Conversational Intake`는 Router 이전의 좁은 결정론 규칙입니다. NFKC,
casefold, 공백 정규화 뒤 전체 입력 allowlist만 비교합니다. 접두사·부분 문자열·LLM 의미
판정은 사용하지 않으므로 업무 질문과 실제 no-match의 escalation을 보존합니다.

## 4. 테스트된 구성요소 팩토리

다음 코드는 `tested_component_factory`입니다.

| 모듈 | 팩토리/경계 | 현재 의미 |
|---|---|---|
| `production_onboarding_web.py` | `create_production_onboarding_app` | 주입된 capability로 온보딩 계약 검증 |
| `central_owner_pairing_web.py` | `create_production_central_authoring_pairing_app` | 중앙 저작·pairing route 조립 계약 |
| `central_question_gateway.py` | `CentralQuestionGatewayRoutes`, `create_https_question_gateway` | thin client용 gateway 경계 |
| `owner_authoring_web.py` | `create_owner_authoring_app` | production wiring이 완성될 때까지 의도적으로 unavailable |
| `a2a_remote_runtime.py`, `a2a_sdk_adapter.py` | strict A2A 1.0 `HTTP+JSON` outbound runtime와 공식 SDK adapter | owner-local tested component; `aon-owner` artifact 아님 |
| production User/Card/Authoring/Index 모듈 | SQLite UoW와 sealed DTO | durable 구성요소의 결정론 증거 |

팩토리는 테스트가 의존성을 명시적으로 주입해 fail-closed 동작과 불변식을 검증하는
단위입니다. 모듈 수준 `app`, console script, 배포 설정, migration/doctor가 없으므로
production server로 분류하지 않습니다.

## 5. Browser Frontend와 BFF 경계

```text
Browser
  → HTTPS reverse proxy / managed TLS
    → Next standalone server
      ├─ pages and assets
      ├─ allowlisted /api/* BFF
      ├─ /healthz
      └─ /readyz
        → private Developer API(JSON/SSE/WS)
```

- Next는 지원되는 유일 browser server입니다.
- `AON_FRONTEND_MODE=development|production`을 명시합니다.
- development만 loopback HTTP backend 기본값을 허용합니다.
- production은 `AON_BACKEND_URL`과 HTTPS `AON_PUBLIC_ORIGIN`을 필수로 검증합니다.
- BFF는 method/path/header/body-size allowlist를 사용하고 임의 upstream,
  `x-forwarded-*`, caller supplied host를 전달하지 않습니다.
- `/healthz`는 Next liveness, `/readyz`는 runtime config와 Developer API readiness입니다.
- Central mode에는 `/owner-api/*`가 없습니다. raw source/full draft/Owner credential은
  Card Owner Installation 경계에 남습니다.
- FastAPI의 기존 `web/*.html` browser runtime은 retire합니다. supervision, scorecard, audit
  detail, token/session control, owner transfer를 포함한 아홉 legacy 화면 기능은 Central Next
  또는 Owner-local Next에 이관해야 하며 HTML fallback을 열지 않습니다.
- HTML별 destination과 success/error/authority/API contract는
  `docs/frontend-runtime-parity.md`에서 추적합니다.

Next standalone 또는 선택적 Docker packaging이 실행된다는 사실은 실제 SSO, production Central Server,
Card Owner 또는 3-install 완성을 뜻하지 않습니다.

Windows native 실행은 PowerShell `.ps1` scripts와 Node standalone을 사용하며 Docker daemon,
WSL, Git Bash를 요구하지 않습니다. Dockerfile과 Docker smoke는 선택적 packaging 증거이고
Windows Fast/Contract gate의 prerequisite가 아닙니다.

## 6. Product Target: 3-install

3-install은 현재 `product_target_not_available`이지만, ADR 0075가 구현할 P0 composition,
protocol, port와 parity acceptance를 고정합니다. 구현 전에는 이 section의 command를 현재
지원 명령으로 읽지 않습니다.

RB3.8의 설치 artifact는 한 source repository를 쓰더라도 `agent-org-central`, `agent-org-owner`,
`agent-org-mcp-client`의 **독립 install bundle/image**여야 한다. 하나의 monolithic wheel에
`aon-central`·`aon-owner`·`aon-mcp` entrypoint만 함께 들어 있는 것은 개발 단계의 component
delivery일 뿐 3-install acceptance가 아니다. 각 bundle/image는 자기 module/tool/route allowlist와
다른 installation의 forbidden surface 부재를 설치 후 검사해야 한다. 이 요구는 ADR 0067 결정 1의
production artifact 해석을 local-reference RB3.8에도 적용한 것이다.

```text
┌────────────────────┐       HTTPS/OIDC       ┌────────────────────┐
│ Question User MCP  │ ─────────────────────→ │   Central Server   │
│ own questions only │                        │ Registry/Authority │
└────────────────────┘                        │ workflow/index     │
                                              └─────────┬──────────┘
                                                        │ paired commands,
                                                        │ receipt/read-back
                                              ┌─────────▼──────────┐
                                              │     Card Owner     │
                                              │ raw/draft/runtime  │
                                              │ review/publish     │
                                              └────────────────────┘
```

### Central Server 목표 경계

- Registry User, Agent Card, 조직 graph와 중앙 Authority
- OIDC callback/session, durable Question/Approval/Conflict/Manager workflow
- 승인된 지식 인덱스, AnswerRecord, safe audit/outbox
- 본문 없는 AuthoringRun control metadata와 digest
- Central Next(public browser/BFF, local reference `127.0.0.1:3000`)와 Central API(private
  JSON/SSE/WS, local reference `127.0.0.1:8010`)는 한 Central Installation의 별 process다.
  Central API composition은 demo/fixture/fallback을 import하지 않고, Central Next는 고정된
  private API origin만 BFF upstream으로 쓴다.

### RB3.1a Central Question Intake 계약

RB3.1의 최소 수직 slice는 `CentralQuestionIntakeApplication`으로 durable `Received`만
create/read-own 한다. 기존 `QuestionResolutionApplication`은 initial routing과 durable
Conflict/Manager 의존성을 함께 요구하므로 여기서 사용하지 않는다. application은 기존
`QuestionRequest.receive`, `SqliteQuestionRequestStore`, `CentralAuthorizer`, request-id factory,
주입 clock만 조립한다. 새 domain state나 routing/answer/onboarding/pairing 기능은 범위 밖이다.

Central profile은 `org_id`, `oidc_provider_id`, `oidc_issuer`, `oidc_audience`, `oidc_jwks_url`,
`authority_snapshot_path`, `database_path`, `data_directory`, `bind_host`, `port`를 exact하게
가진다. local reference는 `127.0.0.1:8010`만 bind한다. OIDC 검증 → Registry User binding →
current Authority 순서가 POST/GET 모두의 유일한 principal/authorization 근거다.

route allowlist는 `GET /healthz`, `GET /readyz`, `POST /v1/questions`,
`GET /v1/questions/{request_id}` 네 개다. POST는 `{ "question": string }`만 받고 `201`, GET은
`200`과 `ReceivedQuestionProjection(request_id, state="received", created_at)`을 반환한다.
이는 existing `RequestPending`/`RequestNotFound`가 아니라 전용 최소 projection이다. 전자는
아직 없는 routing/terminal lifecycle을 wire contract로 약속하기 때문이다. unknown/other-owner/
denied GET은 모두 `404 question_not_found`; unauthenticated는 `401 oidc_unauthenticated`, Registry
User/current Authority 부재 또는 create denial은 `403 question_forbidden`, invalid input은
`422 invalid_question_request`, duplicate request id는 `409 question_request_conflict`, dependency
failure 또는 not-ready는 `503 central_intake_unavailable`이며 error body는 `{ "error": code }`다.

migration은 Question Request schema capability를 먼저 완료하고 Central schema marker를 마지막에
쓴다(marker-last). marker만 있고 table/index capability 또는 configured org의 Registry bootstrap/
Authority snapshot이 없으면 `/readyz`는 `503 central_intake_unavailable`이다. completion schema/UoW,
routing, durable Conflict/Manager store가 아직 없으므로 `Received` 뒤 전이와 terminal 결과는 이
slice가 제공하거나 주장하지 않는다. 이 slice의 구현·테스트가 있어도 support status는
`product_target_not_available`로 유지한다.

### RB3.2a Bootstrap Admin admission 계약

`aon-central bootstrap-admin --profile CENTRAL_PROFILE --attestation BOOTSTRAP_ADMIN_ATTESTATION`은
Central CLI의 non-browser one-time command다. `--profile`과 `--attestation` 외 CLI user, email,
OIDC subject, role, manager, token, secret argument는 허용하지 않는다. attestation exact field는
`schema_version`, `attestation_id`, `org_id`, `registry_user_id`, `oidc_provider_id`,
`oidc_issuer_digest`, `oidc_audience_digest`, `oidc_subject_digest`, `verified_email_digest`,
`device_authorization_ref`, `idempotency_key`, `expected_registry_revision=0`,
`authority_policy_digest`다. raw claim/email/token/device code/user code/client secret은 profile,
normal stdout/stderr, receipt/audit/outbox, HTTP response, support log에 쓰지 않는다.

`BootstrapOidcDeviceAuthorizer` port의 production adapter는 profile의
`bootstrap_oidc_device_authorization_url`, `bootstrap_oidc_device_client_id`,
`bootstrap_oidc_scope`로 device authorization grant를 수행한 뒤 `OidcProvider.verify`의 identity를
attestation digest와 exact match한다. fake adapter는 deterministic gate injection만 위한 것이다.
interactive URI/user code는 normal stdout/stderr가 아닌 invoking process의 `/dev/tty`에만 one-time
표시하며 non-TTY와 verifier/dependency failure는 fail-close한다. verified email은 existing
`ProductionRegistryUserCommand`을 만들 때 memory에서만 사용되고 Registry User internal durable
record 외에 egress하지 않는다.

`BootstrapAdminRegistrationAuthorizer`는 transaction에서 current Authority snapshot의
`user.register`, attestation/config/digest match, Registry revision `0` 또는 exact command replay를
검증하고 existing `SqliteProductionRegistryUsers.register`를 호출한다. 별
`central_bootstrap_admin_seals` schema는 immutable seal digest/reference만 기록한다. Registry
commit과 seal write 사이 crash는 same attestation replay가 existing receipt/audit/outbox를
read-back해 seal을 완성한다. Central marker/bootstrap schema capability가 없으면 `Unmigrated`,
seal이 없으면 `BootstrapPending`, matching immutable seal+receipt/audit/outbox가 있으면
`BootstrapSealed`다. unexpected unsealed mutation, drift, corrupt evidence는 fail-close한다.

stable CLI exit는 success/replay `0`, replay conflict/sealed-other `75`, attestation or Authority
denial `77`, configuration `78`, unavailable/fault/not-ready `69`다. same command concurrent calls는
one Registry User/receipt/audit/outbox/seal로 수렴한다. Central Next/Owner-local Next는 호출하지
않으며 browser route, session, JIT/invitation, extra User onboarding, Agent Card admission,
routing/answer는 이 command의 범위 밖이다. Central Next SSO 추가 User onboarding은 RB3.2b다.

### RB3.2b.1 Central Next packaging·process 계약

RB3.2b.1은 이미 있는 `frontend/`의 Central Next packaging/process component를 구현한 첫
slice다. source/installed artifact discovery, fixed child configuration과 lifecycle까지
검증됐지만 frontend의 일반 지원 수준과 전체 Central artifact를 승격하지 않는다. `web:app`,
legacy fixture, demo identity와 Developer API upstream을 import하거나 fallback으로 쓰지 않는다.
Owner-local Next는 별도의 미래 `owner-frontend/` artifact다.

구현 command는 정확히 `aon-central web serve --profile CENTRAL_PROFILE`이다. `--profile`은 existing
strict Central profile loader로 읽고, Central API origin은 `http://127.0.0.1:8010`, Next bind는
`127.0.0.1:3000`으로만 유도한다. profile에는 browser public HTTPS origin을 위한 exact
`central_public_origin`을 추가할 수 있지만 backend host/port 또는 arbitrary upstream field는 추가하지
않는다. caller `AON_BACKEND_URL`, `HOST`, `PORT`, proxy URL과 command option은 Central BFF upstream을
변경할 수 없다. CLI는 child에 검증·생성한 `AON_FRONTEND_MODE=central-local-reference`,
`AON_PUBLIC_ORIGIN`, `AON_BACKEND_URL=http://127.0.0.1:8010`, `HOSTNAME=127.0.0.1`, `PORT=3000`만
전달하고 caller environment를 정책 입력으로 재사용하지 않는다.

`web serve`는 Central API를 함께 기동하지 않는 별 parent/child process다. bind 전 profile,
`central_public_origin`, standalone artifact layout/manifest, Node executable과 child configuration을
모두 validate한다. source checkout에서는 이미 build된 `frontend/.next/standalone/server.js`만 허용하고,
installed package에서는 배포물에 포함된 같은 standalone artifact만 허용한다. command가 `pnpm build`,
`npx`, package download 또는 network fetch를 실행해 누락 artifact를 복구하지 않는다. child가
unexpectedly 종료하면 parent는 그 exit status를 반환한다. SIGINT/SIGTERM에는 child terminate, bounded
wait, 필요 시 kill을 수행해 orphan를 남기지 않고 signal/exit를 전파한다. API availability는 child
`/readyz`가 검사하되 artifact/process validation을 늦추는 기동 의존성으로 바꾸지 않는다.

이 slice의 Contract acceptance는 source/installed artifact discovery, fixed environment, literal
bind, parent/child lifecycle, caller-upstream override 거부와 fixture/Owner/raw/draft/credential/A2A
import·route·BFF allowlist 0을 검증한다. Central browser OIDC, extra Registry User onboarding,
Question lifecycle과 legacy route는 후속 RB3.2b slice다. 구현과 review가 끝나도 support status는
`product_target_not_available`로 유지한다.

완료 evidence는 Fast 486 Python + frontend 37, Contract 163, scoped Pyright/Ruff와 prebuilt
wheel을 새 environment에 설치한 뒤 bundled artifact validation 및 Node `/healthz` smoke다.
Full Gate는 RB3.8에서 한 번 실행한다.

### RB3.2b.2 Central browser OIDC/session 계약

RB3.2b.2는 ADR 0077의 Central API bounded context를 구현한다. Central Next는 전용 handler의
`POST /api/auth/login/start`, `GET /api/auth/callback`, `GET /api/auth/session`, `POST /api/auth/logout`
네 BFF route만 제공한다. 이는 fixed Central API의
`POST /v1/browser-auth/login/start`, `GET /v1/browser-auth/callback`, `GET /v1/browser-auth/session`,
`POST /v1/browser-auth/logout`에 각각 일대일 대응한다. generic `/api/[...path]`, Developer API,
Owner API 또는 OIDC issuer/token endpoint는 browser auth route가 아니다.

strict Central profile은 기존 OIDC verifier field와 `central_public_origin`에 더해
`browser_oidc_authorization_url`, `browser_oidc_token_url`, `browser_oidc_client_id`, exact string
`browser_oidc_scope`를 요구한다. browser redirect URI는 caller/profile override 없이 exact
`central_public_origin + "/api/auth/callback"`으로 유도한다. authorization URL과 token URL은 HTTPS,
client는 public PKCE client이고 client-secret field는 없다.

구현 상태(2026-07-31): B store foundation과 C code flow·atomic establish까지 완료했다. Central schema marker v4,
digest-only transaction/session, bounded process-memory verifier vault, fixed redirect와 strict
browser OIDC profile four field가 들어갔다. session establishment store는 shared SQLite
connection을 `RLock`으로 직렬화하고 `BEGIN IMMEDIATE` 안에서 transaction/Registry readback,
마지막 current-Authority callback, session insert와 transaction consume을 한 commit으로 수행한다.
vault after-reserve fault는 raw verifier를 즉시 제거·zeroize한다. affected 82, Contract 172,
scoped Pyright/Ruff 및 독립 review를 통과했다. C는 별 public-client exchange와
`FileReloadingBrowserSessionAuthority`, exact start/callback API를 조립한다. token POST redirect
follow 0·bounded response·redacted status 분류, start streaming body bound, standard IdP error의
one-way transaction cancel, durable TTL-cookie 정합, Uvicorn access log off와 composed
start→callback→session 1건을 검증했다(affected 16, Contract 175, scoped Pyright/Ruff).
D는 OIDC exchange와 독립된 browser-session capability로 current/logout을 조립했다. current는
same-DB Registry binding과 매 요청 policy reload `session.read`를 확인하고, logout은 exact
Origin/Fetch Metadata와 session-bound CSRF를 검증한 뒤 policy·Registry·IdP 상태와 무관하게
same-session local cleanup만 수행한다. Registry/policy drift·corruption, concurrent end,
clock rollback과 다른 session 보존을 포함해 affected 78, Contract 177, scoped Pyright/Ruff 및
독립 review를 통과했다. E는 Next dedicated auth handler 네 개, native login form/session/logout
UI, fixed loopback·route별 header allowlist·manual redirect·multiple Set-Cookie·bounded response를
구현했다. compiled standalone↔mock Central 관통과 legacy/demo/Owner/raw artifact negative scan을
포함해 Fast 486 Python + frontend 44, Contract 179 및 독립 review를 통과했다. 이로써
RB3.2b.2 A–E는 완료했지만 actual IdP/TLS browser Manual Acceptance와 support 승격·Full Gate는
RB3.7/8에 남아 있다.

`BrowserOidcTransaction`과 `BrowserSession` durable schema/repository/application DTO는
`transaction_digest`와 `session_digest`만 identifier로 가진다. `BrowserSessionPrincipal`도 digest
principal ID와 existing Registry User binding으로만 current identity를 전달한다. raw handle은 protected
cookie wire에만, code/token/claim은 OIDC exchange adapter process memory에만, PKCE verifier는
`BrowserPkceVerifierVault` process memory에만 있고 DB, port result,
audit/outbox, receipt, JSON, log에 없다. cookie는 `__Host-aon-central-oidc-tx`,
`__Host-aon-central-session`, `__Host-aon-central-csrf`로 Secure/Path=/no-Domain이며 session/transaction은
HttpOnly+Lax, CSRF cookie는 Strict다.

RB3.2b.2는 encrypted/stateless cookie codec, data-directory key file, caller environment secret 또는
key generation/rotation을 만들지 않는다. tx/session cookie에는 각각 256-bit random opaque handle만
있고 server는 digest lookup만 한다. raw PKCE verifier는 transaction digest로 index한
`BrowserPkceVerifierVault`의 API-process-local memory에만 둔다. vault는 1,024 pending entry와
transaction TTL 이하의 lifetime으로 bound하며 start capacity failure는 durable transaction write 전
`503`으로 닫는다. API restart/eviction/fault 뒤 callback은 verifier를 복구하지 않고 session/Registry/
audit/outbox write 0, tx cookie expiry, `503 browser_session_unavailable`으로 수렴한다. unconsumed
transaction row는 TTL expiry만 기다리며 browser는 새 login을 시작한다. encrypted cookie/key lifecycle과
multi-instance handoff는 P1이고 `aon-central migrate|doctor|api serve|web serve`가 key를 생성하거나
fallback하지 않는다.

callback은 transaction state/nonce/expiry/one-time consumption과 issuer/audience/signature/expiry/
email_verified OIDC proof를 검증한 뒤 existing Registry User만 resolve한다. first `session.establish`
Authority check보다 앞선 session write는 없고, Central transaction 안에서 Registry binding,
transaction state와 Authority epoch/action을 read-back하여 mutation 직전에 다시 authorize한다.
`session.read`는 every request current Registry binding과 Authority를 확인한다. `session.end`는
Origin/Fetch Metadata/session-CSRF을 요구하지만 current policy revoke/unavailable에도 자기 session
삭제와 cookie expiry만 수렴시키는 monotonic cleanup이다. remote global revoke/IdP logout은 범위 밖이다.

deterministic Contract는 profile/redirect, PKCE state/nonce/replay/expiry/API restart-vault loss,
no-JIT Registry denial,
establish pre-write/precommit revoke, per-read Authority, CSRF/origin/Fetch Metadata, logout cleanup,
four-route BFF/cookie relay, localStorage/raw-secret non-egress를 다룬다. real IdP+TLS browser redirect와
`__Host-` cookie는 RB3.7/8 Manual Acceptance다. 이 slice는 support status와 Full Gate를 승격하지 않는다.

### RB3.2b.3 session-derived Registry admission 계약

ADR 0078이 RB3.2b.3의 설계를 동결한다. Central Next는 auth BFF와 분리된 exact 다섯 handler
`GET /api/onboarding/status`, `GET|POST /api/admin/users`, `GET|POST /api/admin/agent-cards`만
제공하고, fixed private Central API의 동일 method/path `/onboarding/status`, `/admin/users`,
`/admin/agent-cards`로만 relay한다. generic `/api/[...path]` 또는 Owner/raw/draft/A2A fallback은
없다. `GET`은 session cookie-only이고 `status`는 current `session.read`, users는 current
`user.register`, cards는 current `card.register` Authority를 매 요청 재검증한다. `POST`는 exact
Origin, same-origin/cors/empty Fetch Metadata, session cookie, readable CSRF cookie와
`X-AON-CSRF`, bounded `Idempotency-Key`, JSON만 허용한다. actor/org/role/permission/session은
body/header/query의 입력이 아니며 unknown field/header/path는 fail-close한다.

`POST /admin/users` input은 exact object `{expected_revision, user_id, email, manager}`이고
`manager`만 null을 허용한다. `POST /admin/agent-cards` input은 exact object
`{expected_revision, agent_id, owner, team, summary, domains, maintainer, can_answer, cannot_answer,
approval_when, collaborate_when, knowledge_sources, trust_labels}`이며 `maintainer`만 null을
허용하고 `last_reviewed_at`은 Central clock이 정한다. list/status/result는 raw cookie/session,
OIDC claim, policy/evidence digest, receipt/audit/outbox internals 또는 Owner raw/OKF 본문을
포함하지 않는 safe projection만 반환한다. 정상 result는 current shared Registry revision과
`replayed`만 함께 돌려준다. HTTP error body는 `{ "error": code }`이고 `401
browser_session_unauthenticated`, `403 browser_session_forbidden|browser_csrf_forbidden|
registry_registration_forbidden`, `409 registry_revision_conflict|registry_registration_conflict`,
`422 invalid_registration_request`, `503 registry_registration_unavailable`로 원인을 안전하게
분류한다.

`GET /onboarding/status`의 exact top-level key는 `revision`, `card_capability`, `steps`, `cards`,
`card_owner_installation`이다. `users`는 포함하지 않으며 전체 User 목록은 current `user.register`를
요구하는 `GET /admin/users`에서만 읽는다. `steps`는 아래 순서와 shape를 바꾸지 않는다.

```json
[
  {"kind":"user","label":"Registry User","state":"complete"},
  {"kind":"card","label":"Agent Card","state":"complete|current|locked"},
  {"kind":"card_owner_installation","label":"Card Owner Installation","state":"current|locked"}
]
```

유효한 browser session 자체가 existing Registry User binding이므로 `user`는 항상 `complete`다.
`card`는 current User가 소유한 Card가 있으면 `complete`, 없고 capability가 `available`이면
`current`, capability가 `unavailable`이면 `locked`다. 세 번째 step은 소유 Card가 있을 때만
`current`, 아니면 `locked`이며 RB3.2b.3은 pairing/설치를 증명하지 않으므로 `complete`를 만들지
않는다. `cards`는 current User가 **owner**인 Card의 exact secret-free
`{agent_id, owner, team, summary}` 배열만 포함하고 maintainer-only 또는 다른 User의 Card는 넣지
않는다. `card_capability`는 `available|unavailable`, `card_owner_installation`은 exact
`{"artifact":"agent-org-owner","href":"/onboarding#card-owner-installation"}`이다. `href`는
Central Next 안의 고정 relative 안내 anchor이고 Owner endpoint, profile, pairing token 또는
credential URL이 아니다.

Central marker는 v4에서 v5로 승격하되 marker-last를 유지한다. 먼저 existing Registry User
canonical schema와 Agent Card canonical four table capability를 모두 migrate/read-back하고 그 뒤
marker를 쓴다. User와 Card는 하나의 `production_registry_revisions` sequence를 공유하므로 User
register가 N→N+1이면 뒤이은 Card register는 N+1→N+2만 허용한다. receipt replay/CAS/UoW/audit/outbox
는 각 existing store를 재사용한다. composition의 immutable global read-only registration authorizer는
계속 read/session/OIDC resolution에만 쓰고, request-scoped
`Session-Derived Registry Registration Application`만 transaction-current authorizer를 가진 scoped
store를 만든다. 그 authorizer는 `BEGIN IMMEDIATE` transaction 안에서 session digest의 active/expiry,
existing Registry binding/fingerprint/revision, reload한 current Authority `user.register` 또는
`card.register`를 `current()`과 `verify_precommit()` 양쪽에서 re-read한다. drift·revoke·unavailable은
write 0으로 닫는다.

RB3.2b.3-B는 이를 Central marker v5와 함께 구현했다. Agent Card four-table migration은 canonical
catalog가 이미 정확할 때만 restart-safe이며 partial/legacy/tampered catalog를 repair하지 않는다.
scoped factory는 raw cookie를 받지 않고 validated session digest만 닫아 둔 User/Card stores를 만들며,
Authority resource는 User 등록의 `(user,new_user_id)` 또는 Card 등록의
`(agent_card,agent_id,owner_subject_id)`다. immutable receipt/audit/outbox evidence는 raw session
material을 포함하지 않고, Card replay는 original companion 정합 및 current/precommit reauthorization을
모두 실행하므로 reload된 허용 policy가 과거 receipt digest와 달라도 replay를 막지 않는다. HTTP route,
DTO와 BFF는 이 seam 위의 C–E 범위다.

RB3.2b.3-C는 private Central에 `GET|POST /admin/users`, `GET /onboarding/status`만 추가했다.
request envelope은 query/self-claim/Origin/Fetch/session/CSRF/idempotency/body 순으로 fail-close하며,
POST는 active session과 current `user.register`를 body 전에 확인하고 UoW 안에서 다시 current/precommit
검증한다. `GET /admin/users`는 same SQLite read transaction 안에서 current `user.register`와 safe
projection을 읽고, status는 `session.read`와 같은 방식으로 safe Card Owner Installation handoff만
반환한다. 아직 Card route를 mount하지 않았으므로 status의 `card_capability`는 `unavailable`, `cards`는
빈 배열, card/installation step은 `locked`다. User receipt replay도 current/precommit authorization을
생략하지 않아 policy/session drift는 write 0으로 닫는다.

Session row missing, ended 또는 expiry는 preflight와 transaction `current()`/`verify_precommit()` 모두에서
typed unauthenticated로 보존되어 `401 browser_session_unauthenticated`가 된다. Registry binding/Authority
deny는 `403`, policy/schema/dependency failure는 `503`으로 분리된다. 이 typed result는 Card authorizer
foundation에도 그대로 전파된다. POST envelope은 self-claim/Origin/Fetch까지만 먼저 확인하고 Content-Type,
Content-Length, streamed 64KiB body와 exact JSON DTO는 active session→CSRF→idempotency→current
`user.register` 뒤 검사하므로 세션 없는 malformed body는 401, invalid Origin은 403이다.

RB3.2b.3-D는 private Central에 `GET|POST /admin/agent-cards`를 추가했다. GET은 cookie-only
envelope 뒤 same SQLite transaction에서 current `card.register` Authority와 canonical Card catalog를
검증하고 full safe Agent Card array만 반환한다. POST는 C의 fail-close precedence를 그대로 따르며
session-derived principal/org와 exact `(agent_card, agent_id, owner_subject_id=owner)` ResourceRef만
사용한다. owner self는 별 action이 아니고, delegation도 body role이 아니라 current `card.register`
grant만이 근거다. Central clock이 `last_reviewed_at`을 설정하며 invalid DTO/admission/unknown owner or
maintainer는 `422 invalid_registration_request`, duplicate/key/semantic conflict는 409, policy/schema/
session capability failure는 503으로 분리한다. `GET /onboarding/status`는 `session.read`와 canonical
Card capability를 same transaction에서 검증해 `available`, current User가 owner인 exact Card summary와
User→Card→Card Owner Installation states를 반환한다; 다른 owner, maintainer-only Card, full Card와
knowledge body는 반환하지 않는다.

User와 Card의 replay 의미는 하나다. 같은 org·session-derived actor·`Idempotency-Key`와 canonical
command digest가 exact match할 때만 original safe result와 `replayed=true`를 반환하며 새 revision,
receipt, audit, outbox를 만들지 않는다. replay도 새 command와 똑같이 active session, Registry binding,
current action Authority를 transaction 안에서 재검증한다. 다른 digest/key reuse는 conflict, revoked/
unavailable session·Authority는 replay 여부와 관계없이 deny/unavailable이다. 따라서 stale
`expected_revision`은 semantic replay를 막는 우회 근거가 아니고, 새 command에는 shared revision CAS가
계속 적용된다.

`card.register`는 static central action이다. 현재 policy가 이를 허용한 actor는 existing Registry
User를 Card Owner로 지정할 수 있고 actor=self도 같은 규칙이다. manager 관계·client self-claim은
delegation 근거가 아니며, transfer/revoke/scorecard는 RB3.2b.6이다. `/onboarding`은 current
session의 User→Card→Card Owner Installation handoff만 투영하고 knowledge upload/OKF는 Owner-local
boundary에 남긴다. `/admin`은 ongoing register-only User/Card list/form이다. real IdP/TLS,
support 승격과 Full Gate는 이 slice 밖이다.

RB3.2b.3-E는 auth BFF와 별도인 Next exact admission handler 다섯 개를 구현했다. Browser
`GET /api/onboarding/status`, `GET|POST /api/admin/users`, `GET|POST /api/admin/agent-cards`는
고정 `http://127.0.0.1:8010`의 동일 private path에만 manual redirect/no-store로 relay한다.
generic BFF는 admission path를 계속 거부한다. GET upstream에는 Cookie만, POST upstream에는
Cookie·exact Origin/Fetch/CSRF·Idempotency-Key·Content-Type만 전달한다. caller Host는 fixed
upstream을 바꾸지 않으며 Node가 만드는 transport header도 upstream으로 전달하지 않는다.
Next 14가 route handler 전에 `x-forwarded-*`를 자동 합성하므로 standalone server entrypoint가
raw HTTP headers를 먼저 검사한다. caller가 보낸 `forwarded`, 모든 `x-forwarded-*` 또는 internal
provenance marker는 제거하고 caller-claimed marker를 붙여 BFF가 backend reach 0으로 닫는다. clean
marker 뒤 Next가 합성한 transport fact만 BFF가 허용한다. 값이 URL·Host·loopback과 exact match여도
caller provenance를 대신하지 않으며, unknown 일반 browser header는 upstream allowlist 밖에 남되
그 자체로 reject하지 않는다. standalone manifest와 runtime validation은 mutable `.next/cache/**`를
release file에서 제외하므로 image/cache write 뒤 restart preflight가 artifact tamper로 닫히지 않는다.
request 64KiB/response 1MiB bound와 safe 502 body를 적용했다. client는 readable CSRF cookie를
하나의 bounded opaque value로 strict parse하고, duplicate/malformed/decode failure 및 crypto
request-key 부재에서 fetch 전 fail-close한다. `/onboarding`은 실제 guided surface이고 `/admin`은
동일 component/API client의 independent register-only surface다. Owner raw/OKF body, pairing
credential, local identity/demo/passwordless는 이 bundle에 없다. compiled standalone↔mock Central
five-route integration과 route/query/header/method negative backend-reach-0를 검증했다.

### RB3.2b.4 Central browser Question lifecycle 계약

ADR 0079가 다음 구현의 contract를 동결한다. Central Next는 generic `/api/[...path]` 또는 legacy
`/api/ask*`를 `/ask`의 product path로 쓰지 않고 dedicated handler의 exact
`POST /api/questions`, `GET /api/questions/{request_id}/stream`, `GET /api/questions/{request_id}`,
`POST /api/questions/{request_id}/feedback`의 exact 네 browser method/path만 제공한다. 각각 fixed private Central
`POST /v1/questions`, `GET /v1/questions/{request_id}/stream`, `GET /v1/questions/{request_id}`,
`POST /v1/questions/{request_id}/feedback`의 same-semantic upstream 하나만 가진다.

RB3.2b.4-D는 이 네 dedicated route를 구현했다. standalone raw-header provenance guard가 clean으로
분류한 transport fact 외 caller `Forwarded`/`x-forwarded-*`/internal marker와 identity self-claim은
upstream 도달 전에 닫고, BFF는 session cookie와 route별 browser proof만 fixed loopback Central로
전달한다. write DTO는 exact key/UTF-8/lone-surrogate/64KiB를, finite Central response는 allowlisted
status·safe DTO/1MiB를 재검증하며 Set-Cookie·임의 header/body를 relay하지 않는다. SSE 200만 raw
ReadableStream으로 no-cache/no-transform 전달하고 browser abort/timeout은 safe unavailable로 닫는다.
generic `[...path]`는 Question path를 절대 fallback하지 않는다. independent review 전까지 support
status를 올리지 않는다.

RB3.2b.4-E의 Central Next `/ask`는 session 확인 뒤 exact create response의 `request_id` 하나로
EventSource를 열고, sealed pending/terminal event를 strict decoder로만 투영한다. `done`은 display
payload가 아니라 같은 request ID의 canonical GET으로 다시 읽어 `AnsweredProjection`을 확정한다.
native EventSource의 bounded `Last-Event-ID` reconnect는 유지하고 retryable interruption/transport
fault는 GET으로 수렴하며, deny/mismatch는 fail-close한다. feedback은 Answered에만 표시하고 comment
original whitespace를 보존한 4096 UTF-8 bytes client validation과 per-answer idempotency key를 쓴다.

모든 route는 digest-only Browser Session Principal → existing Registry User/org binding → current
`session.read`와 `question.create|read|feedback.create` Authority 순으로 확인한다. POST는 0078의 exact
Origin/Fetch Metadata/CSRF/64KiB/idempotency/provenance fail-close를 그대로 적용한다. GET은
body/query 없이 cookie만, stream은 bounded decimal `Last-Event-ID`와 `Accept: text/event-stream`만
추가 허용한다. create와 feedback DTO, own-read hiding, error body/code, BFF response/header bounds는
ADR 0079의 exact contract이고 owner/org/role/session self-claim, request Host upstream selection,
anonymous feedback, raw/full source, Owner credential, A2A proxy/inbound는 0이다.

Central marker v6 lifecycle component의 B1은 HTTP create의 current `session.read`/`question.create` 확인과
`Received` create receipt commit만 수행하고 original Received receipt를 반환한다. 별 recovery coordinator가
그 commit 뒤에만 Router를 시작한다. Non-actionable Conversational Intake는 그
commit 뒤 exact single greeting만 revision 1 `Declined(non_actionable_conversation)`로 만들며
routing/dispatch Authority·Router·ConflictCase·ManagerItem·Agent Runtime call 0이다. 업무 0-match는
`Unowned`에서 멈추지 않고 **Received create와 별도인 Router disposition transaction**이 root
User/Manager `ManagerItem`과 `AwaitingManager` durable assignment를 함께 만든다. B1 Routed는
`ReadyToDispatch`까지만 만들며 WorkTicket은 ADR 0042의 별 ReadyToDispatch→AwaitingAnswer UoW에서만
만든다. ConflictCase/Approval/Answer Finalization/feedback은 B2/B3에 남고, demo/fixture Owner Runtime
answer와 실제 Card Owner submit은 RB3.5/RB3.7에 남긴다.

B1의 `Unowned.escalated_to`는 Router 자기보고가 아니라 `BEGIN IMMEDIATE` 뒤 같은 SQLite UoW에서
same-org Production Registry graph로 해석한 정확히 하나의 root User/last-resort Manager와 일치해야 한다.
create receipt·ManagerItem은
Question Request와 foreign-key 및 reverse binding을 매번 검증하고, owned SQLite catalog의 trigger set은
empty exact set이다. 모든 `awaiting_manager` Request에는 same-org ManagerItem이 정확히 하나여야 하고 state
`item_id`와 정확히 결박된다; 삭제·extra·cross-org·mismatch는 reopen/compose readiness에서 fail-close한다.
public initial-transition UoW는 unlinked Manager/Conflict, 둘의 동시 출력, ticket 없는 AwaitingAnswer를
거부한다. 재시작 시에도 Manager binding을 다시 검증한다. 다른 store instance가 initial
disposition CAS에서 지면 current committed winner를 재조회해 수렴하며, orphan/row/tamper/resolver failure는
Received를 고치지 않고 fail-close한다. B2-A는 valid v6 B1 snapshot을 migration input으로만 받아
marker-last Central v7 catalog로 승격한다. v7의 Contested는 immutable candidate snapshot `ConflictCase`와
`AwaitingConflict`를 same UoW로 만들고, frozen `ReadyToDispatch`는 pending WorkTicket/create receipt와
`AwaitingAnswer`를 same UoW로 만든다. Owner delivery는 committed WorkTicket을 읽는 post-commit injected
seam만 가진다. `ticket_id`는 재전달에도 변하지 않는 delivery identity이고, `BEGIN IMMEDIATE` claim은
`pending|expired leased → leased(worker_id, lease_until, delivery_attempt)`를 atomic으로 바꾼 뒤에만 port를
호출한다. 성공은 같은 claim의 CAS로 exact `delivery:{ticket_id}:{delivery_attempt}` acknowledgement receipt를
`delivered`로 기록한다. timeout/exception/ack fault는 active lease를 남긴다. 따라서 lease 만료 전에는 call
0이고, 만료 뒤 다른 worker/restart가 **같은** ticket을 중복 재전달할 수 있는 at-least-once 계약이며
exactly-once를 주장하지 않는다. claim/ack와 ticket/request reverse binding, FK, owned-catalog/trigger set
drift는 startup와 claim/ack 모두 fail-close한다.

B2-B1은 v7 delivery catalog를 forward migration input으로 받아 marker-last Central v8에 typed
`OwnerAnswerIngest` receipt·immutable candidate digest·safe ingest audit와 AnswerRecord 또는 open ApprovalItem
aggregate를 더한다. Ingest는 browser/A2A HTTP payload가 아니라 ticket/request expected revision/attempt/route와
delivery subject를 가진 internal command다. 한 `BEGIN IMMEDIATE` UoW는 current AwaitingAnswer/pending ticket,
lease delivery subject, current Card owner/revision 및 `answer.ingest` Authority를 re-read한다. current policy가
no-approval이면 ticket complete/lease release, receipt/audit, AnswerRecord, AnsweredRequest CAS를 atomic으로
commit하고 canonical `AnsweredProjection(mode=full|backup, review_status=not_required)`은 committed record만
읽는다. approval-required면 AnswerRecord 없이 동일 UoW에 complete/release, immutable candidate/policy/binding
snapshot, open ApprovalItem, AwaitingApproval CAS를 쓴다. replay는 current reauthorization 뒤 frozen
candidate/policy/binding/authority exact match만 write 0으로 반환하고 drift/tamper는 fail-close한다.

B2-B2는 valid v8 Approval catalog만 migration input으로 받아 marker-last Central v12로 승격한다.
v8→v12은 positional tuple append를 쓰지 않고 ApprovalItem의 명시 열 매핑으로 `created_at`을 보존하고
revision=1만 추가한다. ApprovalItem은 `open|approved|rejected`와 monotonic revision을 가지며, resolved
item마다 one-to-one immutable receipt와 receipt-ID foreign-key audit가 반드시 하나씩 존재한다. receipt/audit는
actor의 session-bound principal, exact `approval.decide` ResourceRef/Authority policy version·digest와 canonical
`authority_proof_digest`, 두 expected
revision, decision payload digest와 edit 원문, frozen candidate/policy/binding, resolved item과 terminal Request
revision, exact record ID를 함께 보존한다. 이 evidence는 resolved item과 terminal AnswerRecord 또는
`approval_rejected` Declined/audit를 양방향 검증하므로 FK만 유효한 바꿔치기도 reopen과 replay에서 fail-close한다.
`authority_proof_digest`는 identity session, actor/org, action, exact request/item ResourceRef, Authority policy
version/digest를 lowercase SHA-256으로 봉인하고 receipt/audit에 동일하게 보존한다. historical receipt의 session/policy
proof는 과거 처분 무결성용이며 replay의 로그인 세션과 같을 필요는 없다.
대신 같은 actor Registry User가 새 current Browser Session으로 exact resource의 현재 `approval.decide`를
다시 얻고 precommit에도 유지해야 한다. Browser Session·Registry User·policy는 모두 같은 disposition UoW
connection에서 canonical catalog/fingerprint/currentness 검증과 policy file reload를 거친다.

SSE의 sealed event는 `accepted|token|pending|done|declined|failed|interrupted`이며 every payload의
request ID는 path와 exact match한다. every emission/reconnect는 current Browser Session과
`question.read` Authority를 재검증하고 revoke/deny는 retryable=false, unavailable은 retryable=true의
body-free `interrupted` 후 close한다. disconnect는 execution을 취소하지 않고 stream-local decimal
cursor로 reconnect한 뒤 durable projection을 재구성한다; token은 volatile이라 replay하지 않고 terminal/
pending은 canonical GET으로 수렴한다. `Received`, `ReadyToDispatch`, `AwaitingAnswer`,
`AwaitingApproval`, `AwaitingConflict`, 세 `AwaitingManager.public_kind`, terminal 세 상태의 exact
`type/state/kind/retryable` mapping 및 GET=`done` canonical `AnsweredProjection`은 ADR 0079 표를
사용한다. feedback은 `(org, requester, feedback.create, path request_id, Idempotency-Key, canonical payload
digest)` identity의 requester-bound append-only `QuestionFeedbackEvidence`/immutable `FeedbackRecord`와
idempotent receipt다. current same-org session/`session.read`/`feedback.create`, own `AnsweredRequest`,
finalized AnswerRecord path binding을 fresh/replay 모두 재검증하며, identical replay는 write 0, same key의
other path/payload는 conflict다. `comment`는 `""` 허용·최대 4096 UTF-8 bytes의 JSON string이고 digest는
leading/trailing을 보존한 original UTF-8 bytes를 쓴다; trim/normalization/새 문자 규칙은 없다.
Central v12→v13은 feedback evidence table·immutable update/delete trigger를 marker-last로 추가하며 기존
v12 lifecycle evidence를 보존한다. Central v13→v14은 audit에 org binding을 명시적으로 보강한다.
따라서 B3 hardening 뒤 current Central installation marker는 v14이며 component migration failure는 marker를
v13에 남긴 채 audit/catalog을 rollback한다.
FeedbackRecord·receipt·safe audit은 one transaction으로 append할 뿐
Request, AnswerRecord, Card, Authority나 routing score를 바꾸지 않고 GET/SSE `AnsweredProjection`에 feedback을
넣지 않는다. pending/Declined/Failed/foreign/hidden record, FK/catalog tamper와 dependency failure는
body-free deny/unavailable이며 BFF/UI와 cross-install answer는 후속 slice다.

#### RB3.2b.4-B2 sealed disposition·finalization shape

ADR 0079 §5a가 B2의 composition contract다. `Received` recovery의 `Routed`는 current central
route Authority·canonical Card/Registry binding precommit 재검증 뒤 `ReadyToDispatch`만, `Contested`는
request-unique immutable `ConflictCase`와 `AwaitingConflict`를 같은 transaction으로 만든다.
`Unowned`는 같은 transaction의 RootManagerResolver가 same-org single root User와 current
`manager.act` grant를 증명하고 Router `escalated_to`와 exact match할 때만 ManagerItem과 Request를
atomic으로 만든다. WorkTicket은 이미 authorized frozen route를 소비하는 별
`ReadyToDispatch → AwaitingAnswer` UoW이며 external send를 같은 transaction에 넣지 않는다.

pending ticket의 answer는 browser/anonymous DTO가 아닌 typed `OwnerAnswerIngest`로만 Central에 들어와
ticket·request revision/state/route/attempt·durable owner fence·binding proof를 재검증한다. current
ApprovalPolicy가 required면 one UoW가 candidate digest/policy decision·digest/binding version의 ingest
receipt, pending ticket completion, lease release, immutable candidate draft/evidence, ApprovalItem 및
`AwaitingAnswer → AwaitingApproval` CAS를 함께 commit한다. reject는 이미 terminal ticket을 전제로
ApprovalItem과 Request만 `approval_rejected` Declined로 닫는다. not-required면 FinalizationCandidate와
atomic AnswerRecord/terminal Request/audit/SessionTurn/outbox로 간다. replay는 current delivery
session/Authority·Request·Owner/Card/WorkTicket binding을 다시 확인하고 frozen candidate/policy/binding
exact match일 때만 immutable internal result로 수렴한다; policy drift는 typed conflict, binding/Authority
drift는 fail-close이며 새 state를 만들지 않는다. requester terminal projection은 별 own-read
reauthorization만 사용한다. raw delivery/A2A failure는 Failed로 위장하지 않는다. `AnsweredProjection`
mode=`full|backup`, review_status=`not_required|approved`은 GET과 SSE `done`에 동일하다. Owner
delivery/A2A actual transport는 commit 뒤 injected seam이며 actual paired Owner/A2A adapter는 RB3.3–5에
남는다.

`AwaitingApproval` 처분 writer는 B2 `ApprovalDispositionApplication` 하나다. typed command는 current
Registry User session/identity, same org/request, current `approval.decide`, frozen candidate/policy/
binding version, expected ApprovalItem+QuestionRequest revision을 첫 처분 UoW의 open pre-state CAS로
one transaction에서 재검증한다. append-only disposition receipt는 request/item/actor/revisions/decision
payload digest/idempotency key에 결박한다. approve/edit는 ApprovalItem resolve+receipt+Answer
Finalization(AnswerRecord/Answered/audit), reject는 resolve+receipt+`approval_rejected` Declined+audit을
atomic으로 쓴다. ticket은 earlier ingest에서 이미 terminal이며 여기서 변경 0이다. replay는 먼저 receipt를
찾고 current reauth 뒤 receipt-bound resolved ApprovalItem revision/state와 exact terminal successor
(Request/AnswerRecord 또는 Declined/audit IDs·digests·decision)를 read해 immutable prior result만 write 0으로
돌려준다. 첫 성공 뒤 terminal revision/state는 정상 replay evidence이며, receipt와 다른 successor/decision/
candidate/policy/binding/actor 또는 receipt-bound terminal row missing/tamper만 conflict다; deny/unavailable은
fail-close write 0이다. RB3.2b.5는 이 writer를 우회하지 않는 UI/API만 추가한다.

### RB3.2b.5 Central browser Inbox metadata/control 계약

ADR 0080이 `/inbox`를 동결한다. Central Next와 private Central API는 generic fallback 없이 exact
`/api|/v1/inbox/{conflicts,backup-reviews,reevaluations,approvals}` list/detail 및 corresponding
`concurrences|dispositions|reassignments` POST route만 둔다. GET은 cookie-only, POST는 RB3.2b.4와 같은
Origin/Fetch Metadata/CSRF/Idempotency-Key/provenance guard와 finite DTO/size/error re-projection을 쓴다.
same-org hidden/denied/superseded resource는 `404 not_found_or_denied`로 숨기며 caller actor/card/owner/org
self-claim, upstream selection, raw/full evidence relay는 0이다.

E1 transport 구현은 13개 private `/v1/inbox/**` route와 동일한 13개 dedicated Next
`/api/inbox/**` route를 배선한다. private API는 opaque Browser Session에서 principal만 도출하고,
기존 Conflict/Approval/Review 애플리케이션이 transaction 안에서 `session.read`, named action
Authority와 current Registry/Card binding을 재검증하도록 한다. Next는 fixed loopback 외 upstream을
선택할 수 없고 exact request 64KiB/response 1MiB, UTF-8/lone-surrogate, safe success/error
projection을 적용한다. 공개 `next-standalone-clean` 문자열만으로 forwarding provenance를
신뢰하지 않으며, pre-Next raw-header wrapper가 caller forwarding/marker/proof를 제거·분류한 뒤
추가하는 process-secret companion proof가 일치할 때만 Next synthesized transport header를
허용한다. generic BFF는 `/inbox`를 계속 거부한다. UI/접근성/legacy action parity는
E2이며 E1은 E parent 또는 raw evidence completion을 뜻하지 않는다.

E2 UI 구현은 Central Session exact projection이 확인된 뒤 네 목록을 병렬로 읽고, 선택된 항목의
detail만 lazy load한다. tab/session/detail 전환은 AbortController와 monotonic epoch로 stale response를
버린다. action form은 각 payload 동안 stable idempotency key를 재사용하고 중복 submit을 막으며
DTO의 Case/Request/round 또는 Item/Request revision만 expected 값으로 보낸다. 401은 session state를
중지하고, 404는 stale detail을 숨긴 뒤 목록을 다시 읽으며, 409/503은 form local state를 유지한 채
선택 detail과 네 목록을 canonical GET으로 재조회한다. UI/client decoder는 exact safe projection만
받고 actor/org/owner self-claim, generic route, raw/full evidence link/body를 만들지 않는다.
독립 review와 compiled standalone 실제 브라우저 확인 전에는 E2 `[~]`, E parent `[ ]`다.

ConflictCase는 v15 request-unique `open|resolved|escalated` aggregate와 immutable candidate snapshot,
round/revision, participant Concurrence receipt/audit로 durable해진다. one-vote-per-distinct-Card-Owner sealed
reducer는 expected Case+Request revision, current Card/Owner/Authority binding을 re-read해 partial=
`still_open`, unanimous target=`agreed→ReadyToDispatch` 또는 valid route rejection=`route_rejected→Declined`,
full divergent=`deadlocked→AwaitingManager`만 만든다. deadlock은 current Registry graph nearest common Manager
(없으면 canonical root User)의 current `manager.act` proof, ManagerItem/Case/Request/receipt reverse binding을
같은 UoW에 써 orphan을 금지한다. WorkTicket과 direct Owner/API/Runtime call은 0이고 ADR 0065 escalated Case는 불변이다.

B 구현은 v14 `central_question_conflict_cases`를 삭제·재해석하지 않고 request/case unique FK로 결박한
v15 companion aggregate를 explicit-column marker-last migration한다. candidate snapshot에는 current
Card revision/digest, Owner User, concept/coverage digest만 저장하고 candidate별
`ConflictEvidenceGrant`는 grant/card revision/concept/expiry/single-use/status metadata만 둔다. concurrence,
receipt, audit, deadlock Manager link는 immutable trigger와 정·역방향 reconciliation으로 검증한다.
`ConflictInboxApplication`과 `ConflictConcurrenceApplication`은 HTTP와 분리된 typed seam이며 production
composition은 same-connection Browser Session/Registry fingerprint, production Card row, reload된 Authority
policy와 Registry Manager graph를 읽는 `FileReloadingConflictAuthority`를 배선한다. Central installation
marker는 모든 v15 component read-back 뒤 15로 기록했다. B operational evidence는 19종 source manifest
writer/catalog matrix와 focused gate를 통과했으며 independent review와 support 승격은 별도 게이트다. v14 backfill은 각
legacy 후보가 current canonical production Card의 exact org/Card/Owner/revision/digest로 해석될 때만
snapshot을 만들며 missing/transfer/revoke/catalog drift에는 synthetic fallback 없이 transaction 전체를
rollback한다. list/detail 역시 매 read에서 frozen 후보 전체의 같은 binding을 재검증해 old Owner에게
stale row를 숨긴다. terminal concurrence의 마지막 precommit은 receipt/audit/deadlock link와 fault hook
뒤 selected route Card+route Authority를 다시 확인하거나 nearest-common Manager/root와 `manager.act`를
다시 풀어 최초 결과와 exact 일치해야 commit한다. deadlock catalog reconciliation은 link뿐 아니라 실제
ManagerItem의 org/request/item/manager 및 `AwaitingManager(contested)` Request state까지 양방향으로 묶는다.
Browser Session 부재·종료·만료·Registry fingerprint drift는 typed unauthenticated, policy/Registry/Card
dependency failure는 unavailable, foreign/Authority denial은 hidden not-found다.

Approval은 existing B2-B2 `ApprovalDispositionApplication`의 projection/adaptor뿐이다. approve/edit/reject는
그 writer를 우회하지 않으며, reassign은 separate `approval.reassign` UoW가 current target approver Card/
Owner binding과 sealed authorizer를 확인한 후 old supersede + immutable open successor +
AwaitingApproval revision CAS + receipt/audit만 쓴다. WorkTicket과 terminal outcome은 재지정에서 변하지 않는다.
Approval list/detail은 current open Item과 exact `AwaitingApproval(item_id)` Request를 한 read snapshot에서
검증하고 Item DTO에 current `request_revision`을 반환한다. UI는 이를 disposition/reassign의
`expected_request_revision`으로 그대로 쓰며 다른 projection에서 추론하지 않는다.

C 구현의 v16 physical contract는 `central_question_approval_items`의 기존 payload와 disposition
receipt/audit를 보존하면서 request/ticket 다세대 cardinality와 `superseded` 상태를 허용하고,
`central_inbox_approval_assignments`에 round/predecessor/current designated approver User/Card/revision/
digest/assigned/due를 둔다. initial assignment는 v16 marker가 있으면 Owner answer ingest와 같은
transaction에 생성한다. 재지정은 old Item→reassignment receipt/audit→successor Item→Request의 정·역방향
결박을 exact catalog/FK/immutable trigger/reconciliation로 검증하며 orphan·extra·tamper를 readiness
failure로 닫는다. exact v15 base table이 lifecycle forward repair에서 다시 생긴 경우에만 payload를
explicit-column copy해 v16으로 원자 복구하고, 그 외 catalog drift에는 fail-close한다.

`ApprovalInboxApplication`의 list는 `(org,approval_inbox,subject)`에 `session.read+approval.list`,
detail은 `(org,approval_item,item_id)`에 `session.read+approval.read`를 요구하고 현재 지정자 이외에는
row 존재를 숨긴다. summary에는 Item/request ID와 같은 snapshot의 current AwaitingApproval
`request_revision`, round/Item revision/assigned/due/state만, lazy detail에는
question/candidate text와 digest/policy/binding/assigned User/Card만 추가하며 raw source/full draft는
노출하지 않는다. list/detail 모두 current index Item↔exact `AwaitingApproval(item_id)` reverse binding을
같은 read snapshot에서 검증하며 predecessor/candidate/별 Question GET으로 revision을 추론하지 않는다.
두 read는 pre-open readiness 결과를 권한·무결성 증거로 쓰지 않는다. file을
`mode=rw` query-only로 연 뒤 `BEGIN`한 같은 SQLite snapshot에서 parent lifecycle catalog와 모든
reverse link, v16 Approval catalog/FK/lineage를 먼저 검증하고 그 snapshot에서만 Authority와 projection을
수행한다. 따라서 readiness 뒤 read 전 catalog/lineage tamper도 DTO 없이 unavailable이다.
`ApprovalDispositionInboxApplication`은 typed command를 기존 writer로만 변환하고,
`InboxBoundApprovalDispositionAuthority`가 writer UoW 안에서 current assignment를 재확인한다.
`ApprovalReassignmentApplication`은 fresh/replay/precommit마다 actor session, exact Item resource,
current old assignment, target Card/Owner와 policy proof를 재검증한다. 동일 actor/no-op, changed key/payload,
stale revision, revoked/transferred binding은 hidden/conflict/unavailable로 write 0이며 terminal disposition
경쟁과 재지정 경쟁은 Item/Request CAS로 단 하나만 commit한다. production composition은
`FileReloadingApprovalInboxAuthority`와 SQLite production Registry/Card/Browser Session만 사용하고
legacy approval operation, demo/web/A2A/Owner Runtime은 호출하지 않는다. Central marker는 모든 v16
component read-back 뒤 16으로 기록한다.

v17 migration은 marker-last 전 valid v14–v16 catalog의 every eligible terminal `mode=backup` AnswerRecord와
every bad FeedbackRecord를 canonical source receipt/audit/Request binding으로 scan해 deterministic source-kind/
org/source-ID/receipt-digest outbox ID의 exact one pending intent로 backfill한다. missing/tampered/ambiguous
source는 rollback/fail-close로 marker를 v16에 남기며 noneligible source는 intent 0이다. 이후 v17 BackupReview는 every terminal `mode=backup` AnswerRecord writer(no-approval ingest와 matching approval
finalization)가 same transaction에 source-ID unique outbox intent를 append해 만들고, Reevaluation은 immutable
bad FeedbackRecord writer가 같은 transaction에 feedback-ID unique intent를 append해 만든다. source replay는
matching intent를 exact re-read하며 missing/different intent는 integrity unavailable이다. projector는
claim/lease/expiry retry 뒤 aggregate+producer receipt+delivered marker를 atomic하게 쓰며 startup catalog
reconciliation 뒤에만 재개한다. duplicate, orphan, tamper는 unavailable로 닫는다. backup correct는 immutable
mode=full superseding correction record라 새 backup intent 0이고, reevaluation request_reanswer는 immutable
follow-up만 append한다. feedback의 append-only B3 invariant와 AnswerRecord/Request/Authority/routing score
무변경을 유지한다.

구현된 D1 foundation은 `central_inbox_review.py`의 v17 component marker, immutable outbox/aggregate/
projection receipt schema, source-bound append/verify 함수, lease projector와 startup recovery로 구성한다.
`central_question_lifecycle.py`의 no-approval/approval finalization 및 bad-feedback writer만 실제 producer로
연결하며 source와 intent는 동일 SQLite UoW에서 commit된다. `central_composition.py`는 모든 component
read-back 뒤 central marker 17을 기록하고 startup에서 reconciliation 후 recovery를 drain한다.
metadata list/detail은 같은 query-only transaction snapshot에서 lifecycle/approval/review catalog와
current Card/Owner/Authority binding을 검증한다. D1에는 disposition, correction/reanswer command,
BFF route와 UI를 포함하지 않는다.

승인된 D1 v17 catalog는 불변 입력으로 유지한다. D2는 marker v18 forward migration으로 one-to-one
BackupReview/Reevaluation head, immutable disposition receipt/audit, `AnswerCorrectionRecord`,
`ReanswerRequested` companion을 추가한다. review head만 open revision 1→reviewed revision 2 CAS를 허용하며
source aggregate는 v17 원형을 유지한다. approve/dismiss/acknowledge는 receipt/audit와 head만 쓰고,
correct/request_reanswer는 각각 immutable correction/follow-up을 같은 UoW에 append한다. correct 뒤 canonical
`AnsweredProjection`은 original AnswerRecord를 변경하지 않고 superseding full correction을 읽는다.
모든 fresh/replay/precommit은 current Browser Session, Registry User, source Card Owner binding과
`session.read`+exact decide Authority를 같은 transaction에서 재검증한다. v17→v18 fault는 exact v17로
rollback하며 fresh install도 v17 producer shape를 commit한 뒤 v18까지 전진한다.

B=v15 Conflict, C=v16 Approval projection/reassignment, D1=v17 BackupReview/Reevaluation deterministic backfill+
producers, D2=v18 dispositions/correction/reanswer, E=BFF/UI/review의 finite slices이며 각각 deterministic Fast + Contract Gate만 실행한다. marker-last
forward migration, backfill fault/retry, explicit-column copy, exact catalog/FK/reverse-link/receipt reconciliation은 ADR 0080을 따른다.
Full Gate와 Owner-local raw/full evidence open/release completion 주장은 RB3.5 전까지 금지다.

### RB3.2b.6 Central control-plane 계약

ADR 0081이 current v18 catalog를 세 번의 marker-last migration으로 확장한다. B의 v18→v19는
exact `AuditRecord`, typed `OperationalEvent`, source-bound outbox intent, per-org monotonic cursor와
count retention을 추가한다. 모든 domain source writer는 transition+safe audit+event intent를 같은
SQLite transaction에 쓰고 projector만 cursor를 할당한다. `(org,cursor|event_id|intent_id)` unique,
lease/restart exact replay와 catalog/reverse reconciliation을 요구한다. 기본 retention은 org별 최근
10,000건(`operational_event_retention_count` 1,000..1,000,000)이며 too-old/gap은 SSE 또는 HTTP
typed `resync_required`로 canonical read 뒤 reconnect하게 한다. raw body/URI/credential/session/claim은
audit/event 어디에도 저장하지 않는다.
v19의 pre-v20 Authority provenance는 current strict YAML snapshot과 source grant의 digest가 일치할 때만
`yaml:<digest>` revision, epoch 1, same digest로 canonicalize한다. org 기반 digest, epoch 0, 호출부 hardcode는
금지하며 mismatch는 source UoW 전체 write 0이다. actual source receipt timestamp/Authority companion이 없는
v18 history는 1970 또는 command-attempt로 합성하지 않고 migration unavailable이다.

C의 v19→v20은 `central_policy_revision.py` foundation을 기준으로 strict validated immutable
`PolicyRevision`과 `ActivePolicyPointer`를 추가한다. 현재 구현은 component marker 20, YAML
bootstrap, Central composition marker-last cutover, approval-port 결박, monotonic epoch CAS와
idempotent receipt와 SQLite approval-port를 제공하고 composition runtime Authority provider는 DB
active revision만 읽는다. Central private policy GET/revision POST와 Next 전용 BFF도 연결했으며
bootstrap companion receipt/audit/outbox와 approval precommit 재조회까지 durable UoW로 닫았다.
configured YAML은 exclusive startup에서 epoch 1로 한 번 bootstrap하고 marker v20 뒤에는 import
input일 뿐 poll/reload/fallback하지 않는다. activate와 rollback은
`expected_epoch+expected_digest+Idempotency-Key`로 current pointer를 `BEGIN IMMEDIATE` CAS하고,
import는 같은 route의 sealed third command로 document object만 받으며 rollback은 historical document를
새 epoch revision으로 복제한다. 세 command body는
`approval:{evidence_id,evidence_digest}`를 필수로 포함하고 별 approval header는 없다. evidence는
canonical command digest와 pre-write active pointer fingerprint에 결박되며 write 직전/precommit에
current unexpired/unrevoked/unconsumed same-org/actor/action/resource binding을 재검증한다.
foundation receipt에는 safe evidence ID/digest만 남긴다. durable approval evidence는 SQLite port가
검증하고 policy audit+outbox는 successful apply와 한 commit으로 기록한다. bootstrap companion과
approval precommit 재조회도 동일한 marker-last/transaction 경계에서 검증한다.
evidence/pointer drift는 foundation에서 write 0이므로 ABA와 file/DB split-brain을 막는다.
runtime Authority read는 DB active pointer만 사용한다.

D의 v20→v21은 `CardOwnerAssignment` generation을 backfill한다. transfer는 old active assignment를
CAS revoke하고 generation+1 successor와 next Agent Card/Registry revision을 한 UoW에 쓰며, revoke는
successor 없이 닫되 required owner 값을 마지막 recorded Owner로 유지한다. routing, Answer ingest,
Inbox, authoring과 owner-scoped Central action은 current active generation/Card revision+digest가
exact match할 때만 진행한다. transaction은 Owner API/Worker/WebSocket/credential service를 호출하지
않으며 physical credential/pairing invalidation은 RB3.5다. graph는 safe User/Card와
`owns|manages|maintains`만, organization scorecard는 당시 assignment generation에 귀속한 ADR 0035
four axes만 stable Owner ID 순서로 투영하고 rank/grade를 만들지 않는다.
현재 v21 foundation은 production Card/Registry catalog를 먼저 검증하고 assignment/graph/scorecard를
읽지만, immutable registration receipt를 깨뜨리지 않는 same-UoW Card/Registry mutation port가
주입되지 않으면 transfer/revoke를 503으로 fail-closed한다. 따라서 이 foundation만으로 v21
production cutover나 Owner API success를 주장하지 않으며, mutation port와 source audit/outbox를
연결하는 별도 D/E slice가 필요하다.

E는 schema marker 없이 다음 exact nine BFF→fixed private `/v1` pairs만 배선한다:
`GET /api/console/feed`, `GET /api/console/audit`,
`GET /api/console/audit/{audit_id}`, `GET /api/console/org`,
`GET /api/admin/policy`, `POST /api/admin/policy/revisions`,
`POST /api/admin/agent-cards/{card_id}/owner-transfers`,
`POST /api/admin/agent-cards/{card_id}/revocations`, `GET /api/admin/scorecard`.
각 route는 session-derived Registry User와 DB current `session.read` 뒤
`monitor.read`, `audit.read`, `org_graph.read`, `policy.read`, `policy.write`,
`card.transfer_owner`, `card.revoke`, `scorecard.organization.read`의 exact ResourceRef를 확인한다.
GET은 cookie-only와 exact query,
POST는 Content-Type/Origin/Fetch/CSRF/Idempotency-Key, 64KiB strict DTO와 operational approval
evidence를 요구한다. policy evidence ID/digest는 sealed JSON body의 `approval`에만 있고 header에는 없으며,
expected revisions를 요구한다. errors는 401/403/body-free hidden 404/409/422/503 safe DTO로
재투영하고 generic proxy·Owner API·raw evidence relay는 없다. B–E는 deterministic Fast+Contract만
실행하며 Full Gate와 RB3.5 completion을 주장하지 않는다. Next `/console/org`는 graph의 safe
metadata만 렌더링하고 401/403/404/503을 명시한다. `/admin`은 기존 admission console을 유지한
채 PolicyRevision/scorecard를 read-only로 표시하며, ownership capability가 없는 동안 transfer/revoke
호출을 만들지 않는다. 이 UI 연결은 same-UoW mutation seam이나 independent review 완료를 뜻하지 않는다.

### RB3.2b 이후 parity 의존성

Central browser OIDC/session → session-derived User/Card admission → durable Central Question
lifecycle → Central inbox metadata/control → Central console/admin은 RB3.2b의 순서다. Central
`/inbox`는 `ConflictEvidenceGrant` metadata까지만 독립적으로 완료할 수 있다. source Owner의
raw/full evidence open/release는 Owner workspace와 paired binding을 필요로 하므로 RB3.5에서
완료한다. 이 이전에는 Central raw relay route/BFF 또는 “evidence parity complete” 주장이 없다.

Owner 쪽 순서는 Owner paired workspace/API foundation(RB3.3a) → versioned Central owner-scoped
control API(RB3.3b) → Owner-local Next(RB3.4) → re-pair/transfer/revoke/publish receipt와 evidence
release completion(RB3.5)으로 고정한다. 따라서 RB3.4가 요구하는 self-supervision API를 RB3.5에
처음 만드는 의존성 역전은 허용하지 않는다. RB3.2b.4의 UI/API 상태 전이는 product root의 demo
Owner Runtime으로 완료하지 않으며 실제 Owner answer submit은 RB3.5/RB3.7 cross-install evidence로
닫는다.

### Card Owner 목표 경계

- 원문, 추출물, 전체 초안, 로컬 Git/index workspace
- Owner Runtime과 provider/source credential
- 선택 A2A Remote Runtime의 encrypted local profile, pinned HTTPS endpoint, Remote A2A Agent
  Card digest와 OOB credential reference. A2A 호출은 owner→remote outbound only다.
- 초안 검토·수정·거절 및 exact reviewed revision 공개 요청
- 선택적 Owner Worker
- Owner-local Next(`127.0.0.1:3001`)와 Owner API(loopback `127.0.0.1:8012`)는 별 process다.
  Owner API는 local encrypted workspace/keychain profile만 조립하고 Central Browser의 BFF가
  호출할 수 없다. self-supervision은 versioned Central owner-scoped control API를 통해
  every-read/every-write reauthorization한다. 현재 composition은 explicit read-only
  `OwnerPairingReadiness` seam이 active binding을 확인할 때만 paired/ready를 인정하며,
  profile의 pairing reference나 metadata-only workspace만으로 ready를 반환하지 않는다.

Remote A2A Agent Card는 remote 통신 설정용 untrusted metadata다. Central Authority, Registry
Agent Card admission, Knowledge Index 또는 Answer source evidence로 승격하지 않는다. Central
Server는 A2A inbound endpoint, discovery registry, automatic registration, federation proxy를
제공하지 않는다.

### Question User MCP 목표 경계

- OIDC/PKCE pairing과 OS keychain credential
- `ask_org`, `get_question`
- 자기 Question Request의 생성·조회만 허용

목표 경계의 상세 결정은
[ADR 0067](adr/0067-three-install-product-boundaries-and-pairing.md)을 따릅니다.
다만 ADR 0067의 artifact/entrypoint 이름은 목표 설계이며 현재 패키지 manifest가 아닙니다.
이 해석은 [ADR 0072](adr/0072-runtime-baseline-and-support-levels.md)가 명시합니다.
P0의 exact artifact command, direct protocol, raw evidence route와 forbidden flow는
[ADR 0075](adr/0075-installable-three-artifact-and-feature-preserving-next-migration.md)를
따릅니다.

## 7. 현재 packaging gap

현재 `pyproject.toml`에는 `aon-mcp`, `aon-central`, `aon-owner` console script가 있습니다.
뒤의 두 명령은 RB3의 fail-closed 단계별 진입점입니다. Central은 RB3.1a의 durable
`Received` 접수·본인 조회, RB3.2a bootstrap과 RB3.2b.1 Central Next packaging/process
component까지 조립됐고 Owner는 health/readiness skeleton 단계입니다.

이 단일 개발 wheel의 세 entrypoint는 **세 설치 artifact가 아니다**. RB3.8에서는 Central bundle/image,
Owner bundle/image, Question User MCP bundle/image를 각각 clean install하고, shared frozen contract만
공유하며 서로의 private module, frontend payload, route/tool, secret/data directory가 포함되지 않음을
검사한다. 구현된 RB3.2b.1의 Central standalone wheel payload는 이 최종 artifact 분리 acceptance를
충족하거나 support status를 승격하지 않는다.

존재하지 않는 현재 기능:

- 최초 bootstrap과 Received 이후 Registry·Authority·Question lifecycle을 모두 조립한
  runnable Central API
- pairing·workspace·authoring·worker를 조립한 runnable Owner API
- 세 설치물의 배포 artifact/image
- 실제 IdP/TLS/keychain을 이용한 end-to-end 환경

따라서 앞으로 entrypoint capability를 추가할 때는 코드만 추가하지 않고
[`support-contract.json`](support-contract.json), README, PRD, TRD, TASK와 acceptance
evidence를 같은 변경에서 갱신해야 합니다.

### P0 target file shape (구현 전 설계)

```text
src/agent_org_network/central_composition.py   # only durable Central dependencies
src/agent_org_network/central_api.py           # Central API ASGI root
src/agent_org_network/central_cli.py           # aon-central command root
frontend/                                      # Central Next artifact (or explicit central rename)

src/agent_org_network/owner_composition.py     # paired local workspace/keychain/runtime
src/agent_org_network/owner_api.py             # loopback Owner API ASGI root
src/agent_org_network/owner_cli.py             # aon-owner command root
owner-frontend/                                # independent Owner-local Next artifact
```

`server.py`, `web.py`, `worker.py`, `mcp_server.py`, demo seed와 Fake port는 test fixture 또는
Developer Reference에 남고 product root에 재사용하지 않는다. shared domain value object와
port만 두 installation이 함께 import할 수 있다.

## 8. 데이터·신뢰 경계

### 신원

- production 목표에서는 검증된 OIDC issuer/audience/signature/expiry/email_verified만
  신원 증거입니다.
- body/header/CLI의 org, User ID, role, permission 자기보고는 거부합니다.
- 현재 demo의 익명/개발 세션은 production identity로 승격하지 않습니다.

### Authority

- 권한은 중앙 정책에서만 나옵니다. marker v20 이전 구현은 validated `routing_rules.yaml`/
  production snapshot을, marker v20 이후 runtime은 DB active `PolicyRevision`만 사용합니다.
  YAML은 이후 bootstrap/import input이며 reload/fallback source가 아닙니다.
- current principal, org, action, resource와 policy epoch를 mutation 직전에 재검증합니다.
- `Agent Card`의 문구를 권한으로 해석하지 않습니다.

### 저장

- InMemory는 결정론 단위 테스트와 demo용입니다.
- named SQLite 구현은 단일 프로세스 durable component evidence가 될 수 있지만 그 자체로
  PostgreSQL, multi-instance, backup/restore 또는 production readiness는 아닙니다.
- receipt, audit, outbox, domain state는 서로 다른 의미를 가지며 exact link를 검증합니다.

### 비밀과 본문

- token, authorization code, verifier, provider credential은 로그·audit·outbox에 넣지 않습니다.
- A2A OOB credential은 Card Owner의 secret store에만 두며 profile에는 opaque reference만
  둔다. Remote A2A Agent Card·질문·중앙 control evidence·audit/outbox에 credential 원문을
  넣지 않는다.
- 원문과 전체 draft는 Card Owner가 소유합니다.
- 중앙에는 필요한 digest/control evidence와 승인된 공개 산출물만 둡니다.

### A2A Remote Runtime

`a2a_remote_runtime.py`는 `AgentRuntime`을 구현하고, `a2a_sdk_adapter.py`는 공식
`a2a-sdk==1.1.1`을 narrow `A2AInvocationPort` 뒤에만 둔다. SDK에는
`follow_redirects=False`, `trust_env=False`, TLS verification과 response/time limit을
강제한 `httpx.AsyncClient`를 사용한다. 현재 Contract Gate는 exact MockTransport만 감싸며
SDK의 0.3 compatibility는 선택하지 않는다.

후속 paired Owner profile loader는 active Owner Installation binding의 org, owner, Agent Card
ID/revision/digest와 local profile을 exact 비교해야 한다. endpoint는 profile로 직접 고정하고,
connect 직전 DNS 결과의
loopback/private/link-local/multicast/unspecified/reserved 주소를 거부한다. Remote A2A Agent
Card는 same-origin fixed card path에서 읽어 canonical digest와 selected `1.0`
`protocolBinding="HTTP+JSON"` interface가
profile과 정확히 맞을 때만 수용한다. redirect, card endpoint drift, protocol downgrade와
unexpected authentication scheme은 fail-closed다.

기본 실제-network transport는 검증한 DNS 주소와 실제 dial/TLS SNI를 결박할 구현이 없으므로
fail-closed unavailable이다. 결정론 Contract Gate의 `for_test` seam은 exact MockTransport만
받아 공식 SDK codec/client 경로를 검증하며, 실제 HTTPS egress는 DNS-pinning transport와
Manual Acceptance가 생기기 전까지 지원하지 않는다.

SDK adapter의 sealed outcome은 `A2ACompletedText | A2ARemoteRejected |
A2ARemoteUnavailable | A2AProtocolViolation`이다. completed text-only만 `Answer(text,
sources=(), mode="full")`로 투영한다. failure는 typed `A2ARemoteRuntimeFailure`가 되고
`WorkerLogic`은 `AnswerReady | AwaitingOwnerReview | RuntimeFailed` 중 `RuntimeFailed`로
처리한다. 수신 루프는 이를 redacted log로 기록하고 살아 있으며 SubmitAnswer를 보내지 않는다.
ticket의 release/timeout/escalation은 기존 dispatcher가 단일 진실로 유지한다.

## 9. 검증 전략

### Fast Gate — 모든 로컬 변경과 PR

목표 시간은 90초 이내입니다. 핵심 Question Request·보안·지원 계약, frontend unit,
TypeScript와 lint를 실행합니다. 2026-07-30 실측은 5.09초입니다.

```bash
scripts/verify-fast.sh
```

### Contract Gate — API/frontend/infra 변경

목표 시간은 3분 이내입니다. API-only negative route, BFF allowlist, runtime config,
standalone/Docker와 지원 계약을 검증합니다. 2026-07-30 실측은 1.24초입니다.

```bash
scripts/verify-contract.sh
```

A2A Remote Runtime Contract Gate는 official SDK client가 test-only exact MockTransport를
감싼 secured HTTP client로 strict v1 `HTTP+JSON` REST card fetch와 completed text-only
요청을 수행하는지 확인한다.
Fast Gate는
profile/value object, card digest/interface, no-redirect/SSRF/OOB secret leakage, sealed
WorkerLogic failure mapping을 결정론적으로 검증한다.

### Full Gate — main/nightly/release/manual

전체 pytest, Pyright, Ruff, frontend production build를 보존합니다.

```bash
scripts/verify-full.sh
```

Scale Gate는 기존 `scale` marker를 명시 실행하며, 실제 TLS/IdP/keychain/3-process는
Manual Acceptance입니다.

지원 계약 테스트는 다음 drift를 막습니다.

- package script가 문서보다 늘거나 줄어드는 변경
- demo/legacy entrypoint의 소실
- `aon-central`/`aon-owner`를 구현 없이 현재 지원으로 표현하는 변경
- root 문서가 지원 계약과 상태 표기를 잃는 변경
- Browser Frontend standalone 실행을 production Central Server 완성으로 잘못 승격하는 변경
- FastAPI HTML route 또는 Central `/owner-api`를 다시 여는 변경

### gate 밖 수동 검증

실제 3-install acceptance는 실제 IdP, HTTPS, OS keychain, 별 프로세스, 실제 Owner
workspace/Runtime을 사용한 관통 시연으로만 인정합니다. PostgreSQL, migration,
backup/restore, multi-instance, revoke/re-pair와 운영 관측성도 별 증거가 필요합니다.
A2A Remote Runtime의 수동 증거는 실제 HTTPS A2A 1.0 service와 OOB credential/keychain, pinned card
digest, failure 후 dispatcher escalation을 별 프로세스에서 관통한다. 이 증거 전에는 production
또는 product support claim을 올리지 않는다.
