# Agent Org Network Tasks v0

- 상태: 활성 backlog
- 기준일: 2026-07-30
- 제품 요구: [`prd-v0.md`](prd-v0.md)
- 기술 설계: [`trd-v0.md`](trd-v0.md)
- 실행 지원 SSOT: [`support-contract.json`](support-contract.json)
- Windows/Docker 없는 기본 실행 기준: [ADR 0082](adr/0082-windows-without-docker-runtime-baseline.md)

이 문서는 앞으로 수행할 일과 제품 acceptance만 관리합니다. 과거의 세부 완료 로그는 Git
이력과 `docs/adr/`에 보존합니다. 구성요소 테스트 완료를 제품 완료 체크로 승격하지 않습니다.

표기:

- `[x]` 완료 및 현재 gate 통과
- `[~]` 구현 중이거나 최종 gate 전
- `[ ]` 미착수/미완료

## RB — 실제 실행 기준선 재정비

### RB0 실행 사실 동결

- [x] 패키지 console script가 `aon-mcp` 하나임을 확인
- [x] Developer Reference, Legacy Fixture, dependent client, component factory,
  Product Target을 분리
- [x] `aon-central`, `aon-owner`가 현재 존재하지 않음을 명시
- [x] 3-install 상태를 `product_target_not_available`로 동결
- [x] ADR 0072에 ADR 0067의 현재 해석을 기록

### RB1 SSOT 재작성

- [x] README를 실제 설치·실행 명령과 비지원 경계 중심으로 재작성
- [x] PRD를 제품 목표·현재 artifact 경계·acceptance gate 중심으로 재작성
- [x] TRD를 실제 entrypoint·조립·trust boundary 중심으로 재작성
- [x] TASK를 활성 backlog와 component-evidence index로 축소
- [x] CONTEXT를 유비쿼터스 언어와 핵심 불변식 중심으로 축소
- [x] frontend README에 당시 Visual Prototype 경계를 명시
- [x] `Non-actionable Conversational Intake` 변경을 SSOT에 통합

### RB2 지원 계약과 드리프트 gate

- [x] `docs/support-contract.json` 추가
- [x] package script와 지원 계약의 일치 테스트
- [x] demo/legacy entrypoint와 script 존재 테스트
- [x] `aon-central`/`aon-owner` 부재와 target 상태 테스트
- [x] `aon-mcp` subcommand·tool manifest exact 테스트
- [x] root 문서의 계약 링크·상태 표기 테스트
- [x] 전체 Python/frontend gate
- [x] 독립 리뷰
- [x] Windows PowerShell native 실행 스크립트와 Docker 없는 Fast/Contract 경로를 지원 계약에 추가

RB0–RB2 종료 조건:

- 모든 문서가 같은 지원 상태를 사용한다.
- README의 실행 가능 주장은 실제 명령 또는 계약 테스트에 대응한다.
- Python test, Pyright, Ruff, diff-check와 frontend gate가 통과한다.
- 독립 리뷰에서 지원 수준 과장이나 기존 Question Request 회귀가 없다.

## F — Next Browser Frontend와 검증 계층화

ADR 0073의 실행 단계입니다. Frontend Server를 배포 가능하게 만드는 단계이며 아직
production Central Server 또는 3-install 완료를 뜻하지 않습니다.

### F0 지원 계약·SSOT

- [x] Next를 유일 Browser Frontend로, FastAPI를 API-only Developer API로 결정
- [x] Central Browser Frontend의 Owner raw source/full draft 중계 금지
- [x] Fast/Contract/Full/Scale/Manual gate 책임과 CI 이벤트 결정
- [x] ADR 0073, PRD, TRD, TASK, CONTEXT와 support contract 선갱신
- [x] 당시 `docs/frontend-runtime-parity.md`에 HTML별 이관·retire·backlog를 기록
  (ADR 0075가 기능 backlog 허용을 대체했으며 현재 표는 아홉 화면 완전 이관 acceptance다)

### F1 HTML runtime 정리

- [x] `create_developer_api_app` API-only factory와 호환 alias
- [x] FastAPI HTML page route, framework docs UI, `FileResponse`,
  `web/*.html` runtime 참조 제거
- [x] Developer API `/healthz`, `/readyz`
- [x] `owner_web.py`의 drafts API만 보존하고 HTML page 제거
- [x] legacy HTML 전용 테스트를 API-only negative contract로 대체
- [ ] RB3 P0에서 supervision·scorecard·audit detail·token/session·Card Owner transfer를
  legacy 기능 보존 acceptance와 함께 Next로 이관

### F2 standalone Frontend Server

- [x] Next `output: standalone`
- [x] development/production runtime env 검증과 시작 전 fail-close
- [x] method/path/header/body-size BFF allowlist와 SSE passthrough
- [x] `/healthz` liveness와 `/readyz` backend readiness
- [x] Central `/owner-api/*` 제거와 raw/draft 중계 negative gate
- [x] Node 24 Dockerfile·dockerignore·standalone start 계약
- [x] 실제 Node standalone server와 Docker container smoke

### F3 빠른 기본 검증

- [x] `scripts/verify-fast.sh`: 451 Python + 32 frontend, 5.09초
- [x] `scripts/verify-contract.sh`: 58 contract, 1.24초
- [x] `scripts/verify-full.sh`: 전체 suite 보존
- [x] pull request=Fast+Contract, main/nightly/manual=Full로 CI 분리
- [x] frontend CI Node 24.14.1, pnpm 11.18.0, unit, TypeScript, lint, build 정합
- [ ] 후속 최적화 backlog: source-to-test affected map 고도화

### F4 종료 gate

- [x] Fast 5.09초, Contract 1.24초로 시간 예산 실측
- [x] Full: Python 7,067 passed(4분 42초), Pyright/Ruff, frontend
  unit/TypeScript/lint/build 통과
- [x] 독립 재리뷰 APPROVE — 남은 발견 없음
- [x] README 실행 절차와 실제 standalone/Docker smoke 정합:
  health 200, ready 200, `/ask` 관통, invalid production env exit 1

## RB3 — Local 3-process Reference

RB3부터는 새 기능 단계입니다. RB0–RB2 완료가 선행 조건이며, 완료 전까지 3-install은
`product_target_not_available`입니다.

RB3.0은 docs-first contract gate이고, 그 뒤 RB3.1–RB3.8의 여덟 구현 phase를 순서대로
완료합니다. 각 phase는 다음 phase의 fixture가 아닌 실제 target composition root를 추가하고,
legacy HTML fallback을 열지 않습니다.

### RB3.0 P0 계약 고정

- [x] ADR 0075, SSOT, support contract에 Central/Owner/User artifact, port, protocol, data
  ownership, forbidden flow와 9-screen parity acceptance를 기록
- [x] `aon-central`/`aon-owner` entrypoint skeleton을 `tested_component_factory`로만 기록하고
  3-install support matrix를 승격하지 않는 drift test
- [x] fixture/Developer Reference import가 product composition root에 들어가지 않는 negative test
- [x] installation value/manifest/entrypoint/support drift를 Contract Gate에 포함:
  95 passed, 0.74초

### RB3.1 Central durable API composition

- [~] `central_composition.py`, `central_api.py`, `central_cli.py`와
  `aon-central migrate|doctor|api serve`: RB3.1a intake 조립 완료, 전체 lifecycle 조립 미완료
- [x] RB3.1a Central Question Intake: `CentralQuestionIntakeApplication`으로 existing
  `QuestionRequest.receive` + `CentralQuestionRequestSqliteStore` durable `Received` create/read-own만
  조립. OIDC → Registry User → current Authority, request-id/clock injection, four-route allowlist,
  `ReceivedQuestionProjection`과 exact error contract를 구현
- [x] RB3.1a marker-last Question Request migration, configured org Registry bootstrap 및 Authority
  snapshot readiness. bootstrap/future completion schema가 없으면 ready fail-close; demo seed 금지
- [x] RB3.1a acceptance: restart read-own, duplicate ID, OIDC/Registry/Authority denial, hidden
  not-found, marker crash/retry와 no routing/answer/onboarding/pairing negative gate
- [x] RB3.1a SQLite canonical catalog/marker/Received tamper와 Registry dependency 오류를
  fail-close하고 코드리뷰 승인. Fast 467 + frontend 32, Contract 120 통과
- [x] RB3.1a 구현 뒤에도 support contract와 Central Server status를 승격하지 않음
- [ ] Registry/Authority/Question/Approval/Conflict/Manager/Answer/Index/audit/outbox를 하나의
  durable Central composition에 mount
- [ ] OIDC identity/session, policy snapshot, idempotent receipt/CAS/recovery를 fail-close로 조립
- [~] Central API private bind/default `127.0.0.1:8010`, health/readiness와 no-demo/no-fallback gate:
  bind·gate 완료, production dependency readiness 미완료

### RB3.2a one-time Bootstrap Admin admission

- [x] ADR 0076/Addendum B, PRD/TRD/TASK/CONTEXT에 non-browser 최초 root User OIDC admission,
  exact attestation digest/reference profile, state/error/replay/concurrency/no-egress contract를 고정
- [x] `aon-central bootstrap-admin --profile --attestation`과 실제 RFC 8628 device authorization
  adapter port 구현. raw user/email/claim/token CLI input, demo seed, raw SQL, Authority bypass를
  허용하지 않으며 verification URI/user code는 `/dev/tty`에만 one-time 표시
- [x] existing `SqliteProductionRegistryUsers.register` receipt/audit/outbox를 사용한 current
  `user.register` admission, 같은 Central SQLite DB의 immutable bootstrap seal, marker-last/read-back,
  fault/restart 및 same-command replay 구현
- [x] Unmigrated/BootstrapPending/BootstrapSealed, identity/policy/attestation/replay conflict,
  same-command concurrency와 raw claim/email/secret non-egress 결정론 계약 구현
- [x] bootstrap-admin/doctor의 stable exit와 bootstrap seal이 ready intake의 root User 증거임을
  component test로 확인. support contract/Central Server status/Developer Reference는 승격하지 않음
- [x] RB3.2a 구현 변경 독립 code review APPROVE(Blocker/Major 0), Fast 486 Python +
  frontend 32, Contract 139, scoped Pyright/Ruff 통과
- [ ] Full Gate는 작은 slice마다 반복하지 않고 RB3.8에서 1회 실행
- [ ] `/dev/tty` device-flow는 RB3.7/8의 clean-install test OIDC issuer Manual Acceptance에
  합쳐 한 번 실행한다. 이 항목은 RB3.2b 코드 착수 blocker나 Central/Owner/MCP 3-install
  acceptance 완료 주장이 아니다

### RB3.2b Central Next SSO 및 Central legacy 기능 parity

RB3.2b는 아래 일곱 유한 slice가 모두 끝나면 종료합니다. 이것은 Central browser artifact의
완결점이며 `runnable_local_reference`, 3-install, production/pilot 승격은 아닙니다. Full Gate는
RB3.8까지 보류하고 각 slice는 Fast·Contract·Affected gate와 독립 review를 따른다.

#### RB3.2b.1 Central Next packaging·process contract

- [x] `aon-central web serve --profile CENTRAL_PROFILE` exact command와 Central Next standalone
  child process contract: bind `127.0.0.1:3000`, private API `http://127.0.0.1:8010`은 같은 strict
  Central profile에서만 유도하며 caller environment/argument로 upstream을 바꾸지 않음
- [x] profile·frontend artifact·Node executable을 bind 전에 validate하고, source checkout은 이미
  build된 `frontend/.next/standalone`만, 설치 package는 bundled standalone artifact만 허용;
  `pnpm build`/network/download 자동 fallback 금지
- [x] parent/child exit·SIGTERM/SIGINT·unexpected child termination을 정확히 전파하고, Owner
  API/Owner-local Next/raw source/full draft/Owner credential/A2A import·route·BFF allowlist 0
- [x] source checkout과 installed package의 artifact discovery, fixed backend env, bind/lifecycle,
  forbidden-flow Contract acceptance 및 독립 review
- [x] 독립 review Major 1/2 runtime·packaging 수정과 Major 3 문서/support 동기화 완료.
  Fast 486 Python + frontend 37, Contract 163, scoped Pyright/Ruff, prebuilt wheel installed
  artifact validation과 Node `/healthz` smoke 통과
- [x] 이 완료는 Central Next packaging/process component만 뜻한다. support status와 3-install/
  전체 Central Server는 승격하지 않으며 browser SSO, Central 일곱 destination, Owner Next와
  독립 세 bundle은 후속 RB3.2b.2–RB3.8에 남김
- [ ] Full Gate는 RB3.8에서 정확히 1회 실행

#### RB3.2b.2 Central browser OIDC session

- [x] **A — 설계 계약:** ADR 0077, PRD/TRD/CONTEXT/parity에 authorization-code+PKCE,
  digest-only transaction/session/principal, exact profile four field/redirect, four dedicated BFF/API
  routes, cookie·CSRF·Authority·no-JIT/no-egress/Bootstrap separation을 고정. raw PKCE verifier는
  1,024-entry bounded process-memory vault만 사용하며 restart/eviction callback은 write 0+재로그인;
  새 cookie key/secret provisioning과 rotation은 P1이다. 구현·support 승격은 아님
- [x] **B — store foundation:** strict profile/redirect derivation, digest-only transaction/session
  schema·store, 1,024-entry verifier vault와 marker-last/fault/restart를 구현했다. `BEGIN IMMEDIATE`
  안에서 transaction replay/expiry와 current Registry fingerprint/revision을 재읽고 마지막
  Authority callback 뒤 session insert+transaction consume을 한 commit으로 묶었다. vault
  after-reserve fault는 entry 제거·zeroize, shared SQLite connection은 `RLock` 직렬화로 닫았으며
  concurrent establish는 정확히 1 success+1 replay invalid를 증명한다. code exchange/API route는
  아직 활성화하지 않았다. affected 82, Contract 172, scoped Pyright/Ruff 및 독립 review
  APPROVE(Blocker/Major/Minor 0); Full은 실행하지 않았다.
- [x] **C — code flow·atomic establish:** 별 fake/HTTP public-client code exchange와 PKCE
  start/callback application을 조립하고 verified OIDC를 existing Registry User로만 resolve한다.
  `session.establish` Authority는 policy file을 매 호출 다시 읽어 write 전과 Central transaction
  precommit에 재검증한다. unknown identity/org mismatch, Registry/policy drift, replay, expiry,
  vault loss와 IdP error callback은 session/Registry write 0으로 닫힌다. token POST redirect
  follow 0, 64KiB response bound, 4xx/5xx 분류, start body bound, callback exact query/cookie expiry,
  durable TTL-cookie 정합과 Uvicorn access log off를 검증했다. 실제 composed HTTP
  start→callback은 session 정확히 1개와 fixed `/ask` redirect를 만든다. affected 16,
  Contract 175, scoped Pyright/Ruff 및 독립 review 통과; Full은 실행하지 않았다.
- [x] **D — current/cleanup:** OIDC exchange와 독립된 `BrowserSessionApplication`을 조립했다.
  current는 cookie digest→active/expiry/end→same-DB Registry fingerprint/revision→매 요청
  `session.read` current Authority 순서로 재검증하고 401/403/503을 구분한다. logout은 exact
  Origin/Fetch Metadata/empty body·query/self-claim 차단과 session-bound CSRF cookie/header/
  durable digest를 검증한 뒤, policy·Registry·IdP deny/unavailable과 무관하게 같은 session
  digest만 단조 종료하고 세 cookie를 만료한다. Registry/policy drift·corruption, 다른 session
  보존, concurrent end와 clock rollback을 검증했다. affected 78, Contract 177, scoped
  Pyright/Ruff 및 독립 review APPROVE(Blocker/Major 0); remote global revoke와 Full은 실행하지
  않았다.
- [x] **E — BFF/review gate:** exact four Next↔Central handler/cookie relay, no generic auth
  fallback, no demo/passwordless/localStorage identity/self-claim/raw secret egress를 구현했다.
  login은 native same-origin POST form의 `navigate/document`, logout은 `cors/empty`+CSRF로
  분리하고 fixed loopback upstream, route별 request/response header, manual redirect,
  multiple Set-Cookie, bounded response와 safe Location을 강제한다. compiled standalone↔mock
  Central 관통에서 303/session/204, 세 cookie와 악성 query/header/path backend reach 0을
  검증했다. Fast 486 Python + frontend 44, Contract 179, targeted/compiled artifact 및 독립
  review APPROVE(Blocker/Major 0). RB3.2b.2 A–E는 완료했지만 test IdP/TLS browser는 RB3.7/8
  Manual, support 승격은 아니며 Full은 RB3.8이다.

#### RB3.2b.3 추가 Registry User onboarding

- [x] **A — admission boundary 동결:** ADR 0078로 browser→Next→private Central exact five-route,
  strict POST Origin/Fetch/CSRF/idempotency, GET cookie-only/action-current-Authority, safe DTO/error,
  policy-controlled Card Owner delegation, User→Card→Owner handoff와 v5 marker/공유 revision/UoW
  재사용을 확정했다. JIT/invitation, knowledge upload, revoke/transfer/scorecard, real SSO/Full은
  범위 밖이다.
- [x] **B — schema/session transaction seam:** Central marker v5를 marker-last로 쓰고 canonical Agent
  Card four-table capability를 exact catalog/fault-atomic/restart-safe로 mount했다. immutable global
  read-only Registry authorizer는 그대로 두고 request/session scoped registration application/factory가
  same `BEGIN IMMEDIATE` transaction에서 digest-only session row, Registry binding/revision, current
  file Authority와 command ResourceRef를 pre-write/precommit 재검증한다. policy reload 뒤 Card replay는
  original immutable companion 정합을 확인하면서 current grant를 다시 검증한다. precommit gap의 policy/
  Registry schema/browser-session capability unavailable은 typed `Unavailable`과 write 0으로 닫는다.
  scoped 44 tests,
  pyright/ruff, diff check green; API/BFF/UI는 C–E에 남는다.
- [x] **C — Registry User UoW/API:** existing User receipt replay/CAS/audit/outbox를 재사용해
  `GET|POST /admin/users`와 `GET /onboarding/status`를 현재 Central composition에 mount했다. global
  read-only Registry store는 유지하고 digest-only request factory가 active session/Registry binding과
  exact current Authority를 body 전, UoW current/precommit에 재검증한다. User replay도 original
  companion 검증 뒤 current/precommit을 매번 실행하며, safe status는 Card capability unavailable과
  Card Owner Installation handoff만 투영한다. ended/expired/deleted precommit session race는 typed 401과
  write 0으로, 32-way request-scoped concurrent exact replay는 fresh 1/replay 31와 reopen replay로,
  stale shared-revision CAS는 winner 1/conflict 31로 검증했다. scoped pytest 49, pyright/ruff green; Agent Card API와
  Next BFF/UI는 D/E에 남고 Full은 실행하지 않았다.
- [x] **D — Agent Card UoW/API:** existing Agent Card receipt replay/CAS/audit/outbox와 shared
  `production_registry_revisions`를 재사용해 private `GET|POST /admin/agent-cards`를 mount했다.
  GET은 same SQLite transaction의 current `card.register`와 canonical safe Card array만, POST는
  C와 같은 session→CSRF→idempotency→current/precommit precedence와 Central-clock review date를
  사용한다. invalid Card/owner/maintainer admission은 typed 422, duplicate/key conflict는 409으로
  분리했고, status는 `session.read`와 same transaction의 owner-only Card summary로 available/current
  handoff를 투영한다. scoped pytest 55, pyright/ruff green; Next BFF/UI와 Full은 E/RB3.8에 남는다.
- [x] **E — Next screens/BFF/review:** shared React/API client로 `/onboarding`의 guided
  Registry User→Agent Card→Card Owner Installation handoff와 `/admin`의 ongoing register-only
  User/Card list·form을 이관했다. Central auth와 분리한 exact five-route BFF는 fixed
  `127.0.0.1:8010` same-semantic API만 호출하고, GET cookie-only/POST Origin·Fetch·CSRF·idempotency
  allowlist, 64KiB request·1MiB response bound, caller self-claim/forwarded/path/query fail-close와
  502 safe body를 적용했다. standalone raw-header provenance guard는 caller supplied `forwarded`와
  `x-forwarded-*`를 exact-match/loopback 값까지 backend reach 0으로 닫고 Next 합성 값만 분리한다.
  local brand image는 native `sharp` 없이 direct serving하며 mutable `.next/cache/**`는 artifact
  manifest/restart validation에서 제외한다. strict client DTO/CSRF parser·retry key,
  standalone↔mock Central five-route compiled integration과 negative backend-reach-0, frontend 47
  tests/type/build, artifact contract 6을
  통과했다. real IdP/TLS, support 승격, Full Gate는 여전히 RB3.7/8 밖이다.

#### RB3.2b.4 full Question lifecycle `/ask`

- [x] **A — lifecycle contract:** ADR 0079와 SSOT에 exact 네 browser→Next→private Central
  create/stream/retrieve/feedback method/path, DTO/error/session/CSRF/idempotency/SSE reconnect를
  고정했다. HTTP create Authority 확인 뒤 durable Request, 그 commit 뒤 exact single greeting
  `Declined(non_actionable_conversation)`의 routing/dispatch Authority·Router·Case·Manager·Runtime 0,
  Received create와 분리한 0-match Router disposition/ManagerItem, 9-state exact wire mapping,
  GET=`done` canonical AnsweredProjection, path-bound feedback replay 재검증과 Central demo/fixture Owner
  Runtime 금지를 명시했다. 구현·support/Full 승격은 아니다.
- [x] **B — durable lifecycle composition:** v6 marker-last schema와 Router/Conflict/Manager/Approval/
  Answer Finalization/append-only feedback composition을 Central product root에 조립한다. create receipt
  commit 전에 Router를 호출하지 않으며 greeting 0-call, 0-match ManagerItem, restart/fault/replay와
  actual Owner answer 0을 Contract로 증명한다.
  - [x] **B1 — receipt-first routing seam:** Central marker v6와 exact SQLite catalog/FK로 canonical
    `QuestionRequest`·create receipt·Unowned `ManagerItem`을 durable 조립했다. create는 original
    `Received` receipt만 commit/return하고 Router 0이며, 별 `process_received` recovery가 restart 뒤
    exact greeting Declined 또는 Unowned→AwaitingManager/Routed→ReadyToDispatch를 처리한다. lifecycle
    trigger empty-catalog·foreign-key/orphan·receipt/original-Received·ManagerItem binding을 fail-close하고,
    Unowned Router root는 `BEGIN IMMEDIATE` 뒤 같은 UoW의 same-org Registry root resolver와 exact match해야
    하며 재시작 binding tamper도 fail-close한다. 다중 store CAS loser는 committed winner로 수렴한다.
    WorkTicket, ConflictCase, Approval, Answer Finalization, feedback은 만들지 않는다.
  - [x] **B2 — contested/approval/finalization composition:** ADR 0079 §5a의 sealed
    Routed/Contested/Unowned disposition, request-unique Case/Manager snapshot, separate
    ReadyToDispatch→AwaitingAnswer WorkTicket UoW, typed OwnerAnswerIngest→current ApprovalPolicy→
    ApprovalItem 또는 Answer Finalization과 canonical AnsweredProjection을 B1 recovery seam에
    연결한다. approval-required ingest는 ticket completion/lease release/receipt/draft/ApprovalItem/
    Request CAS를 one UoW로, ApprovalDispositionApplication은 current `approval.decide`/frozen snapshot/
    expected item+request CAS/append-only disposition receipt 뒤 Answered 또는 approval_rejected Declined를
    유일하게 쓴다. replay는 current reauth와 frozen policy/binding exact match로만 수렴한다. Owner
    delivery는 commit 뒤 injected seam만 두며 demo/fixture Owner Runtime·Central A2A ingress/proxy·B3
    feedback은 열지 않는다.
    - [x] **B2-A — Contested·WorkTicket persistence (구현·독립 review APPROVE):** B1 v6의 exact catalog를 migration input으로만
      받고 Central marker v7을 marker-last로 쓴다. `Contested → AwaitingConflict`와 request-unique
      immutable candidate snapshot, `ReadyToDispatch → AwaitingAnswer`와 pending WorkTicket/create receipt를
      각각 one UoW로 조립했다. card binding은 해당 transaction에서 재검증한다. Owner delivery는 commit 뒤
      injected seam의 durable lease다: stable ticket identity를 `BEGIN IMMEDIATE`로 pending/expired lease에서
      한 worker만 claim하고, port success만 exact ack receipt/delivered CAS로 남긴다. timeout/exception은
      active lease를 보존하므로 expiry 전 call 0, restart/expiry 뒤 같은 ticket의 중복 redelivery 가능이라는
      at-least-once(never exactly-once) contract다. claim/ack catalog/FK/reverse-binding tamper와 v6→v7
      marker-last migration을 fail-close test로 검증했다. AwaitingManager는 exact one same-org ManagerItem와
      state item_id reverse binding을 reopen/readiness에 확인하며 public initial transition은 unlinked 또는
      mutually-exclusive Manager/Conflict/ticket aggregate를 거부한다. typed answer ingest/Approval disposition/Answer
      Finalization은 아직 이 slice에 포함하지 않는다.
    - [x] **B2-B1 — typed OwnerAnswerIngest (구현·독립 review APPROVE):** v7 delivery catalog를 marker-last v8로 forward-migrate하고
      stable WorkTicket/request revision/attempt/route, delivery subject, current Card owner/revision 및 injected
      Central ingest Authority를 one `BEGIN IMMEDIATE` UoW에서 exact re-read한다. candidate bytes/digest,
      policy decision/digest, binding/Authority version receipt는 immutable이다. no-approval은 ticket completion·lease
      release·receipt/audit·AnswerRecord·AnsweredRequest를 atomic으로, approval-required는 AnswerRecord 없이
      completion/release·receipt/audit·candidate snapshot/open ApprovalItem·AwaitingApproval을 atomic으로 쓴다.
      current reauth 뒤 exact replay만 write 0; changed candidate/policy/binding/Authority와 catalog/reverse/FK
      tamper는 fail-close한다. ApprovalDisposition, feedback, actual Owner/A2A transport는 아직 포함하지 않는다.
    - [x] **B2-B2 — ApprovalDispositionApplication (구현·독립 review APPROVE):** v8 approval catalog를 marker-last
      Central v12로 forward-migrate한다. typed current actor와 `approval.decide` Authority를 같은
      `BEGIN IMMEDIATE` UoW에서 재확인하고, open ApprovalItem·AwaitingApproval Request·frozen
      candidate/policy/Card binding·expected item/request revision을 CAS한다. approve/edit는 immutable
      decision payload/receipt/audit와 `review_status=approved` AnswerRecord·AnsweredRequest를, reject는
      receipt/audit와 `DeclinedRequest(reason_code=approval_rejected)`를 atomic으로 기록한다. historical
      session/policy proof는 lowercase canonical `authority_proof_digest`로 receipt/audit에 함께 봉인한다. receipt-first
      replay는 current reauth와 exact resolved item/terminal successor/decision payload evidence가 모두
      일치할 때만 write 0으로 수렴하며, stale/foreign/binding drift/tamper/migration failure는 fail-close한다.
  - [x] **B3 — requester feedback composition (Central v13 구현·v14 hardening·독립 review APPROVE):** v12→v13은
    FeedbackRecord/receipt/audit와 immutable trigger를, v13→v14은 audit의 explicit org binding을 marker-last로
    forward-migrate하며 current Central installation marker는 v14다. private Central feedback UoW가 current same-org
    `session.read`/`feedback.create`, requester-owned `AnsweredRequest`와 finalized AnswerRecord path binding을
    exact-read한 뒤 immutable `QuestionFeedbackEvidence`/`FeedbackRecord`·receipt·safe audit만 append한다.
    identity는 path/requester/key/payload digest이며 exact replay는 reauth 뒤 write 0, changed path/payload는
    conflict다. pending/terminal non-Answered/foreign/hidden/tampered binding은 write 0이고, Request/
    AnswerRecord/Card/Authority/routing score와 GET/SSE projection을 바꾸지 않는다. BFF/UI와 actual
    cross-install answer는 열지 않는다.
- [x] **C — private Central lifecycle API (구현·독립 review APPROVE):** exact `/v1/questions` create, stream, retrieve, feedback를
  session-derived Browser Session/Registry User와 current `session.read`·route action으로 mount했다.
  typed create UoW의 same-transaction current/precommit proof 뒤 durable `Received` receipt를 먼저
  반환하고, production Registry/Card·routing rule·Authority adapter의 post-commit/startup recovery가
  greeting/Unowned/Routed disposition만 재개한다(실제 API lifespan 재기동도 seeded `Received`의
  original create receipt를 보존한 채 exact 한 번 수렴하며 Owner Runtime/A2A/delivery 호출 0). strict
  JSON/CSRF/idempotency/path·own-read hiding, lone-surrogate 422, expiry/end/Registry drift 401,
  sealed pending/terminal projection, every-emission session/Authority SSE recheck와 B3 feedback UoW를
  deterministic test로 확인했고 독립 review APPROVE를 받았다.
- [x] **D — Central Next BFF (구현·독립 review APPROVE):** generic/legacy ask route 없이 dedicated exact four-method-path BFF을
  구현했다. fixed loopback Central lifecycle upstream만, cookie-derived session/CSRF/idempotency/
  standalone raw-provenance fail-close, exact DTO·64KiB request/1MiB finite response bound, safe Korean
  status/error DTO, Set-Cookie/header non-relay와 streaming SSE passthrough/abort를 둔다. malformed UTF-8
  ·lone surrogate·self-claim/Forwarded/internal marker·wrong method/path/backend unavailable은 backend
  reach 0 또는 safe unavailable로 닫고 generic `[...path]` Question fallback도 봉쇄했다.
- [x] **E — `/ask` React parity/review (구현·독립 review APPROVE):** Central Session gate 뒤 exact create→EventSource stream→canonical
  retrieve, sealed pending/terminal Korean projection, bounded native cursor reconnect/convergence, terminal
  Answered requester feedback(UTF-8 4096 bytes·replay key) UI와 client/standalone 검증을 구현했다.
  compiled standalone 실제 브라우저에서도 session-unavailable fail-close·responsive layout·asset loading을 확인했다.
- [x] 이 slice는 product composition에 demo/fixture Owner Runtime을 넣지 않는다. Owner가 실제
  answer를 submit하는 cross-install 관통은 RB3.5/RB3.7에서만 완료로 계상한다.

#### RB3.2b.5 `/inbox` Central metadata/control half

- [x] **A — durable 계약:** ADR 0080과 SSOT에 exact private API/BFF list/detail/write method-path,
  session/Authority/error hiding, DTO/CSRF/idempotency/CAS/replay, v15→v17 marker/recovery와 no-raw-relay
  boundary를 고정했다. 구현·Full·RB3.5 evidence completion은 아니다.
- [x] **B — Conflict (구현·독립 review APPROVE):** request-unique v15 ConflictCase snapshot/round/revision, Concurrence receipt/audit,
  expected Request revision/idempotency digest, one-vote sealed reducer, same-UoW nearest-common-Manager/root
  deadlock ManagerItem/Request transition과 deterministic race/fault/replay Contract를 구현했다. v14
  request-unique Case 원형은 보존한 채 v15 companion aggregate/metadata-only `ConflictEvidenceGrant`를
  marker-last backfill하고, list/detail은 참여 Owner만 projection한다. fresh/replay 모두 current Browser
  Session+Registry/Card+`session.read|conflict.concur`를 같은 transaction에서 재검증하며 partial/agreed/
  route_rejected/deadlocked, current route/Manager Authority, Request CAS, receipt/audit/deadlock reverse link를
  atomic하게 닫는다. production composition은 `FileReloadingConflictAuthority`만 배선하며 legacy
  `conflict.py`/demo/web/A2A/Owner Runtime import·call은 없다. Review Major 보완으로 list/detail도 frozen
  후보 전체의 current active same-org Card/Owner/revision/digest가 정확할 때만 보이고, v14 backfill은
  production Card exact binding 없이는 synthetic snapshot을 만들지 않고 전부 rollback한다. terminal
  precommit은 모든 write/fault 뒤 route Authority 또는 current Manager graph+`manager.act`를 다시 풀어
  최초 결과와 같음을 확인하며, deadlock link는 실제 ManagerItem/Request와 exact join한다. ended/expired/
  fingerprint-drift Session은 typed unauthenticated, dependency failure는 unavailable, denied/foreign은
  hidden not-found로 분리한다. B 완료 표시는 독립 review 뒤로 남긴다.
- [x] **C — Approval (구현·독립 review APPROVE):** v15 ApprovalItem/disposition 원형을 보존한
  v16 assignment lineage와 immutable reassignment receipt/audit, current designated approver 전용
  list/lazy detail safe projection을 구현했다. list/detail은 같은 snapshot의 current
  `AwaitingApproval(item_id)` reverse binding과 `request_revision`을 반환하고 UI가 이를
  `expected_request_revision`으로 그대로 사용한다. approve/edit/reject는 existing
  `ApprovalDispositionApplication`만 호출하고, separate reassign은 current Browser Session+
  `approval.reassign`, old/target current same-org Card·Owner·Authority binding, expected Item/Request
  revision을 같은 UoW에서 확인해 old supersede + open successor + `AwaitingApproval` CAS만 쓴다.
  신규 ingest도 v16 설치 후 initial assignment를 lifecycle writer transaction에 함께 기록한다.
  exact v15→v16/repair migration, replay write 0, transfer/revoke hiding, race/fault/tamper/restart reverse
  reconciliation을 구현했다. Review Major 보완으로 list/detail은 별도 readiness connection을
  신뢰하지 않고 자체 read transaction snapshot에서 parent lifecycle reverse-link와 v16 Approval
  catalog를 먼저 검증한다. Fast+Contract+pyright+ruff는 GREEN이며 독립 review 뒤 `[x]`로 바꾼다.
- [x] **D — Backup/Reevaluation:** v17 terminal-backup AnswerRecord 및 bad FeedbackRecord producer outbox,
  source-writer same-UoW intent, claim/lease/retry aggregate+receipt+delivered marker, durable disposition,
  v14–v16 eligible source deterministic backfill(immutable receipt/audit/request binding, fault/retry,
  noneligible intent 0), append-only correction/reanswer and startup reconciliation을 구현한다.
  - [x] **D1 — producer/outbox foundation (구현·독립 review APPROVE):** v16→v17 marker-last deterministic backfill, 실제
    no-approval/approval-finalization/bad-feedback source writer의 same-UoW intent와 exact replay,
    durable lease/projector/startup recovery, metadata-only BackupReview/Reevaluation list/detail Authority
    seam을 구현했다. affected/Fast/Contract/pyright/ruff gate와 독립 review를 마친 뒤 `[x]`로 바꾼다.
    disposition·correction·reanswer와 BFF/UI는 D2/E에 남겨 D parent는 완료하지 않는다.
  - [x] **D2 — disposition/correction/reanswer (구현·독립 review APPROVE):** approved exact v17을 입력으로 marker-last v18
    companion migration을 추가했다. BackupReview approve/dismiss/correct와 Reevaluation
    acknowledge/request_reanswer는 current Session/Registry/Card Owner/Authority, idempotency, CAS,
    replay/precommit reauthorization을 요구한다. immutable receipt/audit, AnswerCorrectionRecord,
    ReanswerRequested와 canonical AnsweredProjection correction read를 구현했으며 affected/Fast/Contract/
    pyright/ruff 및 독립 review APPROVE를 받았다.
- [x] **E — BFF/UI/review:** exact `/inbox` route guards, safe error projection, four accessible tabs,
  legacy action parity 및 independent review를 Fast + Contract Gate로 확인한다. Full은 실행하지 않는다.
  - [x] **E1 — private API + dedicated BFF (구현·독립 review APPROVE):** ADR 0080의 13개
    `/v1/inbox/**` private route와 동일한 13개 `/api/inbox/**` Next fixed loopback route를
    추가한다. current Browser Session/Registry principal, `session.read`와 동작별 Authority,
    exact 64KiB UTF-8 DTO, CSRF/idempotency/revision, hidden 404와 safe error projection,
    1MiB response projection, generic fallback/backend-reach-0 및 compiled standalone artifact를
    focused backend/frontend + Fast/Contract/pyright/ruff로 검증한다. raw clean marker 값 자체는
    신뢰하지 않고 pre-Next wrapper의 process-secret companion proof가 있을 때만 Next synthesized
    forwarding을 허용한다. UI·접근성·legacy action
    parity는 E2에서 완료했다.
  - [x] **E2 — `/inbox` React UI (구현·독립 review APPROVE·standalone browser 확인):** Central Session gate 뒤
    ConflictCase·BackupReview·Reevaluation·ApprovalItem 네 접근 가능한 탭, lazy detail, abort/epoch
    stale-response 억제와 13개 dedicated BFF route 전용 strict client를 구현했다. concurrence,
    approve/correct/dismiss, acknowledge/request_reanswer, approve/edit/reject/reassign은
    CSRF·stable replay key·DTO가 제공한 expected revision만 보내며 actor/self-claim은 보내지 않는다.
    401은 session stop, 404는 상세 숨김, 409/503은 입력을 보존한 canonical 목록·상세 재조회로 처리하고
    metadata-only `ConflictEvidenceGrant`만 표시한다. focused frontend/Fast/Contract/pyright/ruff,
    compiled standalone build, 독립 review와 session-unavailable 실제 브라우저 확인을 완료했다.
- [x] raw/full evidence는 `ConflictEvidenceGrant` metadata만 Central에 둔다. Owner-local evidence
  open/release는 Owner workspace와 pairing이 필요한 RB3.5 completion으로 분리하며, 이 전에는 raw relay
  fallback·완료 주장을 하지 않는다.

#### RB3.2b.6 `/console`·`/admin` parity

- [x] **A — durable control-plane 설계:** ADR 0081과 SSOT/parity에 safe AuditRecord/OperationalEvent,
  same-UoW source outbox, per-org monotonic cursor, deterministic count retention/typed resync,
  immutable PolicyRevision+active pointer/YAML one-time bootstrap, epoch+digest CAS activate/rollback/replay/
  import/ABA와 canonical command+active-pointer-bound operational approval evidence의 prewrite/precommit/
  replay 재검증, Card Owner Assignment generation transfer/revoke, safe graph/organization scorecard와 exact nine
  private API/BFF routes를 고정했다. current base는 RB3.2b.5 D2의 v18이며 A는 구현·Full·RB3.5 credential/
  pairing completion을 뜻하지 않는다.
- [x] **B — Operational evidence (v18→v19):** source transition과 safe audit+event intent를 같은
  transaction에 통합하고 deterministic backfill, cursor projector/lease/restart, org별 기본 10,000건
  retention, SSE/HTTP typed resync, feed/audit private API를 구현한다. pre-v20 Authority는 current YAML/source
  grant digest exact 일치의 `yaml:<digest>`/epoch 1만 허용하고, actual timestamp/Authority companion 없는 v18
  history는 합성 backfill하지 않는다. 19종 source manifest writer/catalog matrix와 lifecycle·Inbox·Registry·Card
  focused gate를 통과했다(`172 passed`); Fast+Contract만 실행하며 independent review와 support 승격은 별도
  게이트로 남긴다.
- [x] **C — PolicyRevision (v19→v20):** `central_policy_revision.py`의 strict YAML bootstrap, immutable
  revision/pointer, Central marker-last v20/component composition cutover, approval-port 결박,
  monotonic epoch CAS와 activate/import foundation을 추가했다. validated YAML epoch-1 bootstrap 뒤
  DB active revision만 runtime Authority로 읽고 activate/rollback/import의 validation, expected epoch+digest CAS,
  canonical command digest+active pointer fingerprint에 결박된 ADR 0051 evidence의 write 직전/precommit/
  replay current-validity 확인, safe evidence ID/digest receipt와 ABA/no-file-fallback foundation을
  구현한다. missing/stale/foreign/mismatched/consumed evidence는 write 0이다. Policy GET/revision
  POST와 Next 전용 BFF까지 연결했고 bootstrap companion receipt/audit/outbox와 approval precommit
  재조회까지 구현했다.
  PolicyRevision 회귀 9개와 v20 composition bootstrap 회귀를 추가했고 Fast+Contract만 실행한다.
  DB active revision 기반 runtime Authority composition adapter와 SQLite approval-port까지 반영했다.
- [~] **D — Ownership/graph/scorecard (v20→v21):** current Card/Owner binding을 generation 1로
  backfill하고 transfer/revoke receipt/CAS/immediate active-assignment fence, safe organization graph와
  assignment-generation-attributed organization scorecard를 구현한다. Owner API/external credential
  call과 Owner Installation credential/pairing invalidation은 RB3.5에 남긴다. v21 foundation은
  canonical production Card/Registry catalog 검증, FK/immutable assignment receipt, safe graph와
  generation fence까지 구현했지만, production Card/Registry next-revision same-UoW mutation seam이
  없으면 기본 transfer/revoke를 fail-closed한다. focused ownership 회귀 6개와 Fast+Contract를
  통과했으며, same-UoW mutation/audit-outbox 연결 전에는 D 완료로 승격하지 않는다.
- [~] **E — exact BFF/UI/review:** console feed/audit list+detail/org와 admin policy
  read+revision write/Card Owner transfer+revoke/organization scorecard의 exact nine BFF→private `/v1`
  pairs, CSRF/error/resync projection, legacy action parity와 independent review를 구현한다. Policy와
  graph/ownership/scorecard BFF 계약 및 Central private route의 capability-unavailable 경계를
  연결했고, `/console/org`의 safe graph projection과 `/admin`의 read-only PolicyRevision/scorecard
  패널을 Next에 연결했다. UI의 malformed/401/403/404/503 명시 상태, credentials-only 요청,
  owner mutation 미호출을 `central-admin-ui.test.mjs`와 standalone build에서 검증했다. same-UoW
  ownership seam과 independent review가 남았고 schema marker는 없으며 Fast+Contract만 실행하고
  Full Gate는 금지한다.

#### RB3.2b.7 Central integration acceptance

- [~] Central Next/API 별 process, fixed port/BFF allowlist, no fixture import/no Owner flow와 legacy
  7 destination(`/ask`, `/inbox`, `/onboarding`, `/admin`, `/console/feed`, `/console/audit`,
  `/console/org`)의 action-level success/error/authority parity runbook·Contract/Affected evidence.
  legacy `builder`/`author`, demo session, raw evidence relay는 Central artifact에서 negative test로
  금지한다. 현재 standalone route bundle에 `/console/org`, PolicyRevision/scorecard, ownership
  transfer/revoke 경로와 forbidden Owner surface 부재를 고정하고 Fast 487 Python+67 Next 및
  Contract 300을 통과했다. 별 process clean-install runbook, affected evidence와 test OIDC real
  manual은 RB3.7/8으로 보류한다.

### RB3.3a Owner paired workspace/API foundation

- [~] `owner_composition.py`, `owner_api.py`, `owner_cli.py`와
  `aon-owner api serve|workspace serve|doctor`: API skeleton 완료, workspace/worker는 unavailable.
  Owner profile의 `pairing_reference`만으로 paired를 주장하지 않도록 read-only
  `OwnerPairingReadiness` seam과 active bundle의 Central origin·device binding·credential
  generation/expiry·pending 상태를 검증하는 adapter를 추가했고, seam·workspace·schema marker가
  모두 없으면 doctor/readyz가 fail-closed한다. Windows current-user DPAPI secret-bundle backend와
  keyring/DPAPI 실패 시 평문 fallback 금지를 추가했지만, 실제 pair/redeem mutation·receipt anchor·
  recovery orchestration은 다음 bounded slice다(ADR 0084).
- [ ] Central-issued pair/redeem의 current generation을 exact compare하는 active Owner Installation
  binding, encrypted workspace/keychain profile, local durable AuthoringRun/draft/Git repository와
  default loopback Owner API `127.0.0.1:8012`를 조립
- [x] `server.py`/`web.py` transient authoring, demo source/Fake runtime의 product-root import 금지
- [ ] A2A Remote Runtime은 `a2a-sdk==1.1.1`의 strict v1 `HTTP+JSON` direct Message 또는
  completed Task 단일 text-only Artifact만 수용하고, profile/digest/interface/SSRF/redirect/OOB
  secret/no-SubmitAnswer gate와 dispatcher release/timeout/escalation을 보존
- [ ] 실제 HTTPS A2A 1.0, OOB credential/keychain, pinned card digest와 remote failure escalation의
  별 process Manual Acceptance

### RB3.3b Owner-scoped Central control API 선행

- [~] ADR 0083의 versioned `/v1/owner/*` contract foundation을 추가했다. strict paired binding
  (`org`/Owner/Card revision+digest/assignment·credential generation/device/origin), safe AnswerRecord/
  Presence/correction/scorecard DTO, `supervision.read|supervision.correct|scorecard.read` Authority
  actions, injected auth/read/write ports와 read serialization/write pre-commit reauthorization을
  고정했다. default composition에는 capability를 주입하지 않아 unavailable로 닫으며, 결정론 Fake
  contract 13개가 self-only filter, stale/foreign/unknown fail-close, secret non-egress와 write 0을
  검증한다. 실제 Central route·durable source adapter·Owner pairing/current fence 조립은 아직 남았다.
- [ ] versioned Central owner-scoped control API를 Owner API가 호출하도록 조립: paired current
  binding으로 self AnswerRecord list/presence/correction/own scorecard를 매 read/write 재인가
- [ ] 다른 Card/Owner의 control read/write는 403, body/query owner·role 자기보고는 0, transfer/revoke/
  unavailable이면 local correction submit 0을 실제 route/adapter Contract로 검증
- [ ] 이 API와 Owner API가 준비되기 전에는 RB3.4 Owner Next를 start하지 않는다. Central 전체
  scorecard/Authority/admin scope는 RB3.2b.6 `/admin`에 남긴다.

### RB3.4 Owner Next 및 Owner legacy 기능 parity

- [ ] Owner-local Next standalone `owner-frontend/`, default `127.0.0.1:3001`, loopback-only BFF
- [ ] `/card`, `/workspace`, `/drafts`, `/supervision`의 builder/authoring/owner-draft/self-supervision
  success/error/recovery contract 구현
- [ ] Owner self-supervision은 versioned Central owner-scoped control API를 매 read/write 재인가해
  answer/presence/correction/scorecard를 제공; Central admin 전체 scorecard와 분리

### RB3.5 Central–Owner lifecycle 및 evidence completion

- [ ] re-pair/unpair, Card revision/transfer/revoke, worker credential generation과 exact reviewed
  revision publish receipt. RB3.3a paired binding의 restart/reconciliation과 stale generation도 닫음
- [ ] `ConflictEvidenceGrant`와 Owner-local evidence open/release를 완료: Central raw relay/storage/
  log 0, grant는 case/candidate/revision/TTL/single-use, source Owner release receipt와 recipient를
  exact bind. 이것이 RB3.2b.5 inbox evidence parity의 completion이다.
- [ ] receipt replay/different-payload conflict, restart/crash reconciliation과 body/secret non-egress tests

### RB3.6 Question User MCP 관통

- [ ] current `aon-mcp` fixed `{ask_org,get_question}` manifest로 Central HTTPS gateway/PKCE pair
- [ ] own Question Request create/retrieve, expiry/re-pair/revoke/restart와 client self-claim negative test

### RB3.7 Local reference acceptance

- [ ] test tenant OIDC issuer, loopback TLS CA, test keychain과 Central Next/API, Owner Next/API/Worker,
  MCP stdio의 별 process install/doctor/runbook
- [ ] legacy 9 route의 success/error/authority parity, port/bind/BFF allowlist, fixture isolation과
  raw/secret non-egress Contract/Affected gate

### RB3.8 Final local-reference acceptance 및 support claim

- [ ] clean install한 **독립 Central/Owner/User bundle/image**의 `doctor`/기동과 Central Next/API,
  Owner Next/API/Worker, MCP stdio 별 process를 runbook으로 재현. 한 monolithic wheel의 세
  entrypoint만으로는 통과하지 않으며 artifact별 module/tool/route allowlist와 상대 forbidden
  surface 부재를 설치 후 검사
- [ ] test OIDC issuer, loopback TLS CA, test keychain으로 pair → Card admission → local authoring/
  review/publish → MCP ask/retrieve를 관통
- [ ] legacy 9 screen parity의 success/error/authority, fixture isolation, raw/secret non-egress,
  restart/re-pair/revoke/transfer/remote-failure negative acceptance
- [ ] Full Gate 1회, targeted Contract/Affected gate, 독립 code review 후 support contract를
  `runnable_local_reference`로 승격할지 증거와 함께 결정

real enterprise IdP/public TLS/OS keychain/separate machine, backup/restore, multi-instance,
observability, secret rotation과 production/pilot security review는 RB3 종료 조건이 아니라
P1 Production 운영 gate의 별 Manual Acceptance다.

RB3는 로컬 reference까지만 목표로 합니다. 완료해도 곧바로 production/pilot-ready로
선언하지 않습니다.

## P1 — Production 운영 gate

- [ ] PostgreSQL schema/migration과 supported upgrade path
- [ ] multi-instance transaction, lease, outbox, recovery
- [ ] backup/restore와 재해 복구 연습
- [ ] secret/KMS, rotation, revoke와 keychain 지원 범위
- [ ] TLS 종료, rate limit, CSRF, secure cookie와 보안 헤더
- [ ] audit retention/export, metrics, tracing, alerting
- [ ] 조직 격리·권한 회수·침해 시나리오 보안 검토
- [ ] 접근성·브라우저·운영 UX 검증
- [ ] golden-set 분류/라우팅 품질 기준과 회귀 평가

## P2 — 통제된 지식 개선

- [ ] 사람 작성 revision의 AI 자문 검토
- [ ] AI/mixed revision의 사람 binding 검토
- [ ] finding → immutable candidate → target별 독립 eval → 사람 promotion 계보
- [ ] source/target exact binding과 kill/revoke
- [ ] serving promotion·rollback의 receipt/audit/outbox

Authority, RBAC, ApprovalPolicy, production code, secret 또는 모델 가중치의 자율 변경은
범위 밖입니다.

## 구성요소 증거 색인

아래 항목은 테스트된 설계·구성요소 증거입니다. 설치 가능한 제품 acceptance가 아닙니다.

| 영역 | 대표 ADR | 현재 분류 |
|---|---|---|
| Question Request·Answer Finalization | ADR 0042–0046 | Developer Reference + tested components |
| production bootstrap·Authority | ADR 0049–0050 | tested component factory |
| tenant/durable credential 경계 | ADR 0052–0053 | tested components |
| 검토·개선 계보 | ADR 0054–0063 | 설계 및 tested components |
| Registry User·사람 처분 | ADR 0064–0066 | tested components |
| 3-install 경계 | ADR 0067 | Product Target |
| Owner credential·published index | ADR 0068–0069 | tested components |
| thin Question User MCP | ADR 0070 | installable dependent client |
| 비업무 대화 종결 | ADR 0071 | 현재 Question Request 동작 |
| 실행 지원 재기초화 | ADR 0072 | 현재 지원 SSOT |
| Card Owner A2A Remote Runtime | ADR 0074 | tested component; Owner profile loader·실 egress·Manual Acceptance 미완료 |

세부 구현 순서와 변경 이유가 필요하면 해당 ADR과 Git 이력을 조회합니다. 이 색인의
“tested”는 실제 IdP/TLS/별 프로세스/PostgreSQL 운영 증거를 대신하지 않습니다.
