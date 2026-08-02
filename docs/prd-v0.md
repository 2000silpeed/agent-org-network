# Agent Org Network PRD v0

- 상태: 재기초화된 제품 목표
- 기준일: 2026-07-30
- 실행 지원 SSOT: [`support-contract.json`](support-contract.json)
- 기술 설계: [`trd-v0.md`](trd-v0.md)
- 실행 작업: [`tasks-v0.md`](tasks-v0.md)
- Windows/Docker 없는 기본 실행: [ADR 0082](adr/0082-windows-without-docker-runtime-baseline.md)

## 1. 제품 목표

Agent Org Network의 목표는 조직의 질문을 책임 있는 사람과 공개된 조직 지식에 연결하고,
그 결정·승인·답변의 근거를 추적 가능하게 만드는 것입니다.

제품의 핵심 가치는 다음 세 가지입니다.

1. 어떤 업무 질문도 조용히 유실하지 않는다.
2. 담당·권한·승인을 추측하거나 `Agent Card`의 자기보고로 대체하지 않는다.
3. 원문과 전체 초안의 소유권은 `Card Owner`에 두면서, 중앙에는 검증 가능한 공개본과
   control evidence만 둔다.

## 2. 현재 제공 범위

현재 저장소는 production 제품이 아니라 개발·검증 기준선입니다. 현재 지원 수준은
[`support-contract.json`](support-contract.json)의 상태를 그대로 사용합니다.

- `runnable_developer_reference`: Next Browser Frontend와 API-only Developer API
- `runnable_legacy_fixture`: 중앙·Worker 및 인프로세스 MCP 수동 fixture
- `installable_dependent_client`: 별도 호환 gateway가 필요한 `aon-mcp`
- `tested_component_factory`: production 규칙을 테스트하는 온보딩·저작·pairing·gateway
  팩토리와 A2A Remote Runtime component
- `product_target_not_available`: 완성형 3-install 제품

현재 package에는 `aon-mcp`와 함께 `aon-central`·`aon-owner` fail-closed entrypoint가
있습니다. Central RB3.1a는 strict OIDC→Registry User→Authority 검증 뒤 durable `Received`
접수와 본인 조회를 조립했고, RB3.2a는 실제 RFC 8628 device authorization으로 one-time root
User bootstrap admission을 조립했습니다. RB3.2b.1은 prebuilt source 또는 wheel-bundled Central
Next를 `aon-central web serve --profile`의 별 child process로 실행하는 packaging/process
component를 조립했고 RB3.2b.2–3은 browser SSO와 session-derived Registry admission을 구현했습니다.
Central `/ask`의 durable lifecycle은 ADR 0079의 B1 receipt-first routing과 B2 sealed disposition·Answer
Finalization까지 구현·독립 review 승인되었습니다. B3 requester feedback evidence와 private Central lifecycle API는
구현·독립 review 승인되었고, Central Next lifecycle BFF는 구현되어 review 대기입니다. `/ask` UI와 cross-install answer는 아직 남아 있습니다. Owner
pair/workspace/worker/Next는 stable unavailable입니다. 이 명령과 팩토리·테스트·UI가 존재한다는
사실은 설치 가능한 전체 제품 완료를 뜻하지 않습니다.

### P0 방향 변경 — 설치 가능한 3-artifact와 기능 보존 이관

사용자의 최신 제품 방향은 Central Server, Card Owner Installation, Question User MCP를 각각
**독립 install bundle/image**로 설치·실행 가능하게 만들고, 과거 아홉 browser 화면의 기능을
Central Next 또는 Owner-local Next로 손실 없이 이관하는 것입니다. 같은 monolithic Python wheel에
세 console entrypoint가 있다는 사실만으로는 이 요구를 충족하지 않습니다. 각 bundle/image는
ADR 0067의 module/tool/route allowlist와 금지 surface를 독립적으로 증명해야 합니다. 이는 기존 “HTML retire 후 일부 기능을
backlog로 둔다”는 범위를 대체합니다. 상세 경계와 금지 흐름은
[ADR 0075](adr/0075-installable-three-artifact-and-feature-preserving-next-migration.md)를
따릅니다.

이 문서 변경은 목표와 acceptance의 합의입니다. 현재 entrypoint skeleton과 health/readiness는
전체 Central API, Owner API/Next capability가 생겼다는 의미가 아니며 지원 상태는 구현 검증 전까지
`product_target_not_available`입니다.

## 3. Product Target

목표 사용 흐름은 다음과 같습니다.

1. 조직 관리자가 `Central Server`를 배포하고 실제 IdP와 Authority를 연결한다.
2. 사용자는 SSO로 `Registry User`에 결박된다.
3. `Card Owner`는 자신의 `Agent Card`를 등록한다.
4. 원문을 로컬에서 수집하고 `AuthoringRun`으로 초안을 만든다.
5. `Card Owner`가 검토·수정·거절하고, 승인된 revision만 공개를 요청한다.
6. 중앙은 `Published Index Acceptance Receipt`와 공개 인덱스를 원자적으로 확정한다.
7. `Question User`는 Question User MCP를 pair하고 조직 질문을 제출·조회한다.
8. 담당 공백, 다툼, 승인 필요, 장애는 숨기지 않고 명시 상태로 노출한다.

이 흐름은 제품 목표이며 현재 end-to-end 지원 상태가 아닙니다.

## 4. 목표 사용자

### Registry 관리자

- 실제 사람을 `Registry User`로 관리한다.
- 중앙 Authority와 조직 경계를 관리한다.
- 담당 공백과 교착 상태를 처분한다.

### Card Owner

- 자신의 `Agent Card`, 원문, 전체 초안과 로컬 Runtime을 소유한다.
- local profile로 선택한 `A2A Remote Runtime`은 pinned HTTPS endpoint에 outbound 호출만
  할 수 있으며, Remote A2A Agent Card를 권한이나 등록 근거로 해석하지 않는다.
- 공개 전에 초안을 검토한다.
- 공개된 지식의 출처와 revision을 추적한다.

### Question User

- 자신의 인증된 세션으로 조직 질문을 제출한다.
- 담당 확정 전 상태, 승인 대기, 명시적 거절, 장애를 구분해 본다.
- 같은 Question Request를 다시 조회한다.

## 5. 기능 요구사항

### PR-1 Question Request 우선

- 모든 업무 질문은 Router 전에 안정적인 Question Request ID를 얻어야 한다.
- 질문 상태는 조회·blocking·SSE·MCP에서 같은 의미여야 한다.
- terminal outcome은 `Answered`, `Declined`, `Failed` 중 하나로 명시되어야 한다.

### PR-2 책임 결정

- 단일 책임 후보는 중앙 Authority 검증 뒤에만 라우팅한다.
- 0매칭은 `Unowned`로 root User/Manager 처분에 연결한다.
- 복수 책임 후보는 `Contested`로 두고 합의 또는 Manager 처분 전 담당을 확정하지 않는다.
- `Agent Card`는 Authority를 선언할 수 없다.

### PR-3 비업무 대화

- exact-only 단독 인사는 `Non-actionable Conversational Intake`로 종결한다.
- 비업무 대화는 담당 큐, ConflictCase, ManagerItem, Runtime을 호출하지 않는다.
- 인사가 포함되어도 업무 질문이 있으면 정상 라우팅한다.
- 실제 0매칭 업무 질문은 비업무 대화로 오인하지 않는다.

### PR-4 승인과 최종화

- 승인 정책이 요구하는 후보 답은 승인 전 최종 답으로 노출하지 않는다.
- 수정승인은 새 후보 증거를 보존한다.
- 모든 사용자 표면은 같은 Answer Finalization 결과를 사용한다.
- 전이와 audit/outbox 기록을 구분한다.
- feedback은 requester-bound append-only evidence이며 immutable AnswerRecord나 terminal Question
  Request를 변경하지 않는다.

### PR-5 지식 소유권

- 원문과 전체 초안은 `Card Owner` 로컬 경계에 남는다.
- 중앙은 digest, 상태, receipt와 공개가 승인된 산출물만 다룬다.
- 공개 대상은 exact reviewed revision과 현재 Owner/Card/Authority에 결박되어야 한다.

### PR-5a A2A Remote Runtime 경계

- A2A Remote Runtime은 Card Owner local profile, OOB credential, pinned endpoint와 exact
  Remote A2A Agent Card digest를 통해서만 선택한다.
- strict A2A 1.0 `HTTP+JSON` REST의 direct Message 또는 completed Task 단일 text-only
  Artifact만 기존 AnswerCandidate 경계로 보낸다.
- remote failure는 답으로 위장하지 않는다. Owner Worker는 SubmitAnswer 없이 기존
  dispatcher의 release/timeout/escalation으로 종착시킨다.
- Central Server는 A2A proxy, inbound server, discovery service 또는 자동 Registry admission
  surface를 제공하지 않는다.

### PR-6 인증과 권한

- production 목표에서는 실제 OIDC 검증 결과만 사용자 신원으로 받아들인다.
- 조직, User ID, 역할, 권한의 body/header/CLI 자기보고를 신뢰하지 않는다.
- 중앙 Authority는 every-read/every-write 시점의 현재 정책을 기준으로 fail-close한다.
- 최초 root User는 `aon-central bootstrap-admin`의 one-time OIDC device authorization과
  `Bootstrap Admin Attestation`으로만 admission한다. CLI는 raw user/email/claim/token/role 입력을
  받지 않으며 attestation·receipt·audit/outbox·출력에는 digest 또는 opaque reference만 남긴다.
- bootstrap은 current central `user.register` Authority와 immutable Registry receipt/audit/outbox를
  사용한다. `Unmigrated`/`BootstrapPending`/`BootstrapSealed`와 crash replay를 구분하며 demo seed,
  raw SQL, Authority bypass는 product path가 아니다.
- browser SSO는 Central API의 authorization-code+PKCE transaction과 digest-only opaque session만
  사용한다. verified OIDC identity는 existing Registry User로만 resolve하고, `session.establish`를
  write 전·transaction precommit에 재검증한다. 추가 Registry User admission만 별 RB3.2b.3
  범위이며, invitation과 JIT는 범위 밖이다.
- `session.read`는 매 요청 현재 Registry binding과 `session.read` Authority를 재검증한다.
  `session.end`는 revoke/unavailable에도 자기 local session을 없애는 monotonic cleanup이며 IdP/다른
  device의 global revoke를 뜻하지 않는다.

### PR-7 Browser Frontend

- Central Browser 화면은 Central Next standalone 서버가, Card Owner의 raw/draft 화면은 별
  Owner-local Next standalone 서버가 소유한다. 두 server/bundle/BFF allowlist를 합치지 않는다.
- FastAPI Developer API는 JSON, SSE, WebSocket만 제공하고 HTML을 반환하지 않는다.
- 브라우저는 same-origin BFF를 통해 private Developer API를 호출한다.
- production mode는 HTTPS public origin과 검증된 backend URL 없이는 시작하지 않는다.
- RB3.2b.1 Central Next packaging/process command는
  `aon-central web serve --profile CENTRAL_PROFILE`이며,
  same strict Central profile에서만 Central API origin을 유도한다. caller environment·argument로
  upstream을 바꾸지 않고 source/installed artifact를 자동 build·download하지 않는다.
- browser auth route는 exact four-route BFF뿐이다: `POST /api/auth/login/start`, `GET
  /api/auth/callback`, `GET /api/auth/session`, `POST /api/auth/logout`. 이는 fixed private Central
  API의 `/v1/browser-auth/login/start|callback|session|logout`에만 대응하며 generic BFF fallback은
  없다. Central Next는 OIDC issuer/token endpoint를 직접 호출하지 않는다.
- RB3.2b.3의 Registry admission BFF는 auth route와 별도의 exact 다섯 route뿐이다: `GET
  /api/onboarding/status`, `GET|POST /api/admin/users`, `GET|POST /api/admin/agent-cards`. 이들은
  fixed private Central API의 `/onboarding/status`, `/admin/users`, `/admin/agent-cards`에 같은
  method와 의미로만 대응한다. generic fallback·Owner API·raw/draft/A2A relay는 없다.
  `POST`는 exact Origin, `Sec-Fetch-Site=same-origin`, `Sec-Fetch-Mode=cors`,
  `Sec-Fetch-Dest=empty`, session cookie, session CSRF cookie/header와 `Idempotency-Key`를
  모두 요구하며, actor/org/role/session의 body·header 자기보고를 받지 않는다. `GET`은
  session cookie만 근거로 action별 현재 Authority를 재검증한다. `forwarded`와 모든
  `x-forwarded-*`의 caller-supplied presence는 standalone raw-request boundary에서 fail-close하며,
  URL/loopback과 값이 같다는 사실은 trusted provenance가 아니다. 일반 browser header는
  self-claim이 아닌 한 이 거부 기준으로 확장하지 않는다.
- `/onboarding`은 Registry User → Agent Card → Card Owner Installation handoff만 안내한다.
  knowledge upload/OKF 본문은 Card Owner 경계이며 이 화면 또는 Central BFF가 받지 않는다.
  status의 세 step kind는 exact `user`, `card`, `card_owner_installation`이고 세 번째 label은
  `Card Owner Installation`이다. status에는 전체 User 목록을 넣지 않고 current User가 소유한
  Card의 secret-free 요약, Card capability와 fixed relative installation handoff만 둔다.
  `/admin`은 ongoing User/Card 목록과 register-only form을 제공한다. revoke, transfer, scorecard는
  RB3.2b.6 범위다.
- RB3.2b.4 `/ask`은 legacy/generic BFF를 쓰지 않고 exact 전용 route만 쓴다: `POST
  /api/questions`, `GET /api/questions/{request_id}/stream`, `GET /api/questions/{request_id}`, `POST
  /api/questions/{request_id}/feedback`. 이는 exact 네 browser method/path이며 fixed private Central API의 `POST /v1/questions`,
  `GET /v1/questions/{request_id}/stream`, `GET /v1/questions/{request_id}`, `POST
  /v1/questions/{request_id}/feedback`에 같은 의미로만 대응한다. session-derived Question User,
  current `session.read`와 `question.create|read|feedback.create` Authority, POST CSRF/idempotency,
  requester-bound own-read/feedback, typed SSE reconnect와 canonical retrieve를 강제한다. create의
  current `session.read`/`question.create` 확인과 `Received` commit 뒤에만 exact-only 단독 인사의
  `non_actionable_conversation` Declined를 만들며, 이 경우 routing/dispatch Authority·Router·Case·
  Manager·Runtime은 0이다. 업무 0-match의 root User/Manager durable escalation은 Received create와
  분리된 Router disposition transaction에서 만든다. SSE emission/reconnect는 current session과
  `question.read`를 재검증하고 revoke/deny/unavailable이면 body 없이 typed `interrupted`로 닫는다.
  sealed 9-state mapping과 GET=`done` canonical Answered DTO는 ADR 0079를 따른다. Central Next
  presentation은 session unavailable을 로그인/안전 오류로 gate하고, create 후 exact EventSource를
  구독한다. token은 임시 진행 표시만이며 terminal answer는 canonical GET으로 확정한다. Answered에만
  requester feedback(`good|bad`, 빈 comment 허용, UTF-8 4096 bytes)을 표시한다.
- RB3.2b.5 `/inbox`은 dedicated Central BFF의 exact conflict, BackupReview, Reevaluation, ApprovalItem
  list/detail/disposition routes만 쓴다. 모든 행위는 session-derived principal과 current
  `session.read`·action Authority, CSRF/idempotency, expected aggregate/Request revision CAS 및
  receipt replay를 요구한다. ConflictCase concurrence는 frozen 후보의 서로 다른 Card Owner 한 표씩의
  sealed reducer와 expected Case/Request revision으로 `still_open|agreed|deadlocked|route_rejected`의
  exact same-UoW Request 전이만 만들고 직접 Owner API/Agent Runtime을 부르지 않는다. deadlock은 current
  Registry nearest common Manager 또는 canonical root User와 ManagerItem을 원자적으로 만든다. Approval reassign은
  별 command로 successor ApprovalItem을 열어 `AwaitingApproval`을 계속 유지한다.
  Approval list/detail은 current Item과 AwaitingApproval Request를 같은 snapshot에서 검증하고
  `request_revision`을 반환하며 UI는 이를 expected revision으로 그대로 사용한다.
  BackupReview는
  terminal backup AnswerRecord, Reevaluation은 append-only bad FeedbackRecord만 durable producer로
  삼으며 각 source writer는 같은 transaction의 source-bound outbox intent를 append한다. v17은 v14–v16의
  eligible immutable source를 receipt digest 기반으로 결정론 backfill하여 누락 없는 처리함을 만든다.
  correction/reanswer는 immutable successor/follow-up record를 append할 뿐 기존
  AnswerRecord·FeedbackRecord·routing score를 고치지 않는다. raw/full evidence는 metadata-only
  `ConflictEvidenceGrant`를 넘어 Central API/BFF에 들어오지 않으며 open/release는 RB3.5다.
  E2 구현 기준 Central Next `/inbox`는 exact four-tab list와 lazy detail을 제공하고 13개 dedicated
  BFF route만 호출한다. action은 DTO의 current expected revision, CSRF와 stable replay key만 보내며
  caller identity를 self-claim하지 않는다. 409/503 canonical reload에도 입력을 보존하고 404는 stale
  detail을 숨기며, raw/full evidence link나 body는 렌더링하지 않는다. 독립 review와 실제 브라우저
  확인 전에는 E 또는 `/inbox` parity 완료로 계상하지 않는다.
  B 구현 기준 Central installation marker v15는 v14 Case 원형을 보존한 companion aggregate,
  metadata-only evidence grant, immutable Concurrence receipt/audit와 deadlock Manager reverse link까지
  제공한다. 후보 Card transfer/revoke/revision·digest drift 뒤에는 old Owner 목록·상세가 0건이어야 하고,
  migration은 exact production Card binding 없이는 synthetic 후보 snapshot을 만들지 않는다. terminal
  write는 commit 직전 current route Authority 또는 current Manager graph+`manager.act`가 최초 결정과
  exact 일치해야 하며 deadlock link는 실제 ManagerItem/Request와 일치해야 한다. Session 비인증은
  dependency unavailable 및 hidden not-found와 typed하게 분리한다. HTTP/BFF 노출은 후속 E이며 독립
  review 전 제품 완료로 계상하지 않는다.
  C 구현 기준 Central installation marker v16은 기존 ApprovalItem·ApprovalDisposition receipt를
  보존하고 immutable assignment lineage와 reassignment receipt/audit만 추가한다. list/lazy detail은
  current designated approver의 active same-org Card/Owner/Authority binding이 정확한 open Item만 safe
  DTO로 투영한다. approve/edit/reject는 기존 `ApprovalDispositionApplication`만 쓰며, reassign은 old
  Item supersede + open successor + `AwaitingApproval` revision CAS를 한 transaction에서 기록하고
  WorkTicket·AnswerRecord·Owner 호출은 만들지 않는다. HTTP/BFF 노출은 후속 E이며 독립 review 전
  완료로 계상하지 않는다.
  D1 구현 기준 Central installation marker v17은 historical terminal backup AnswerRecord와 bad
  FeedbackRecord를 immutable source receipt/audit에 결박된 deterministic outbox intent로 backfill한다.
  v17 이후 no-approval/approval finalization과 bad feedback source writer는 같은 transaction에 intent를
  쓰고 replay에서 exact intent를 재검증한다. startup recovery는 durable lease로 intent를 claim한 뒤
  aggregate·projection receipt·delivered marker를 원자적으로 기록한다. 현재 slice는 metadata-only
  BackupReview/Reevaluation list/detail Authority seam까지이며 disposition·correction·reanswer와
  HTTP/BFF/UI는 후속 D2/E 전까지 완료로 계상하지 않는다.
  D2 구현 기준 승인된 v17 producer catalog는 변경하지 않고 marker v18 forward migration이
  disposition head/receipt/audit와 immutable AnswerCorrectionRecord/ReanswerRequested companion을 추가한다.
  correction은 original AnswerRecord를 그대로 두고 canonical AnsweredProjection만 full successor를 읽으며,
  reanswer 요청은 기존 Request/Answer/Feedback/score/dispatch/runtime을 변경하지 않는다. HTTP/BFF/UI와
  독립 review는 후속 E 전까지 제품 완료로 계상하지 않는다.
- RB3.2b.6 `/console`·`/admin`은 ADR 0081의 durable Central control-plane 계약을 따른다.
  marker v18에서 v19 OperationalEvent/AuditRecord/outbox/cursor를 추가하고 source transition과 같은
  SQLite UoW에 safe audit+event intent를 쓴다. 조직별 cursor는 strictly monotonic이며 기본 최근
  10,000건 count retention과 typed `resync_required`를 제공한다. raw question/answer/comment/rationale,
  source/full draft/URI, credential/session/claim은 audit/feed schema에 없다.
  v20부터 immutable `PolicyRevision`과 active pointer가 runtime Authority의 유일한 source다.
  현재 구현은 `central_policy_revision.py`의 strict bootstrap과 Central marker v20/component
  composition cutover, epoch CAS/receipt까지다. composition runtime Authority provider는 DB active
  revision만 읽고 SQLite approval-port가 exact command binding을 검증한다. Central private policy
  GET/revision POST와 Next 전용 BFF를 연결했고 bootstrap companion receipt/audit/outbox와
  approval precommit 재조회까지 같은 durable UoW 경계로 닫았다.
  `routing_rules.yaml`은 strict 최초 bootstrap/명시적 import 뒤 live reload/fallback하지 않으며,
  activate/rollback은 expected epoch+digest CAS, idempotency receipt, 새 monotonic epoch로 ABA를 막는다.
  activate/rollback/import foundation은 ADR 0051 approval-port의 safe evidence ID/digest를 body
  command에 결박하고 write 직전/precommit/replay 검증을 수행하며 receipt에 ID/digest만 남긴다.
  durable approval evidence 검증·claim 경계, audit/outbox와 private API wiring은 구현되어 있다.
  v21 `CardOwnerAssignment`은 transfer 때 old generation close+successor open, revoke 때 successor 없이
  close하며 required Agent Card owner는 null로 만들지 않는다. routing/owner action은 current active
  generation만 사용하고 Owner API/external credential service는 transaction에서 호출하지 않는다.
  Owner Installation credential/pairing invalidation은 RB3.5다.
  현재 v21 foundation은 production Card/Registry catalog와 immutable receipt를 검증하며, same-UoW
  Card/Registry mutation port가 없으면 transfer/revoke를 fail-closed한다. 이 foundation만으로
  production ownership cutover를 주장하지 않는다.
  Central Next는 exact nine dedicated BFF/private API pairs만 추가한다: console feed/audit
  list+detail/org, admin policy read+revision write/Card Owner transfer+revoke/organization scorecard.
  정책·소유권 POST는 CSRF/Idempotency-Key/CAS/operational approval evidence를 요구하며 조직 scorecard는 별
  `scorecard.organization.read`로 rank/grade 없이 당시 assignment generation에 귀속한다. `/console/org`는
  safe User/Card graph projection과 401/403/404/503 명시 상태를 표시하고, `/admin`은 기존 register-only
  admission과 함께 PolicyRevision/scorecard read-only panel을 표시한다. Owner transfer/revoke capability가
  없으면 UI와 BFF 모두 성공을 가장하지 않고 unavailable로 닫는다. 구현 B–E는 Fast+Contract만 실행하고
  Full Gate 또는 RB3.5 완료를 주장하지 않는다.
- RB3.3b는 ADR 0083에 따라 Owner self-supervision을 위한 별도 versioned `/v1/owner/*` 계약부터
  고정한다. 현재는 paired binding·strict safe projection·Authority action·read/write 재인가를
  검증하는 contract foundation만 제공하며, 기본 composition에는 실제 route/source/pairing capability를
  조립하지 않아 503으로 닫힌다. Central admin scorecard와 requester Question projection을 Owner
  self scorecard/AnswerRecord source로 재사용하지 않으며, Owner Next는 실제 route 조립 후에만 시작한다.
- Central profile의 browser OIDC field는 `browser_oidc_authorization_url`, `browser_oidc_token_url`,
  `browser_oidc_client_id`, exact `browser_oidc_scope`뿐이다. redirect URI는 profile 입력이 아니라
  HTTPS `central_public_origin + "/api/auth/callback"`으로 고정한다.
- browser session/transaction은 `__Host-` cookie와 server-side digest만 사용한다. Origin, Fetch
  Metadata와 session CSRF를 fail-close하고 raw session ID, token, code, verifier, claim은
  localStorage·JSON·audit/outbox·log로 내보내지 않는다.
- Central Browser Frontend는 Card Owner의 raw source, full draft, Runtime credential을
  업로드하거나 중계하지 않는다.
- Central artifact에는 legacy/demo identity, `/builder`·`/author` Owner route, raw evidence relay가
  없고, Owner artifact에는 Central Authority/admin/audit/Manager route가 없다.
- liveness와 backend readiness를 별 endpoint로 구분한다.

### PR-8 검증 비용

- 모든 로컬 변경과 pull request는 90초 이내 Fast Gate를 기본으로 한다.
- API/BFF/runtime 계약 변경은 3분 이내 Contract Gate를 추가한다.
- 전체 결정론 suite는 main, nightly, release 또는 명시 수동 실행에서 보존한다.
- 실제 TLS, IdP, keychain과 3-process 관통은 Manual Acceptance로 분리한다.
- 테스트 수를 줄이기 위해 Authority, identity, approval, persistence 불변식을 삭제하지 않는다.

## 6. 명시적 비범위

다음은 현재 제공 범위가 아니며, 별 acceptance gate 없이는 지원한다고 주장하지 않습니다.

- 구현·검증 전의 `aon-central`, `aon-owner` 설치 명령 또는 3-install 지원 주장
- real IdP/TLS/keychain 없이 production으로 해석하는 SSO·pairing·3-process 실행
- IdP 계정 생성, 초대, JIT provisioning
- production PostgreSQL, migration, backup/restore, multi-instance 운영
- 인터넷 공개를 전제로 한 TLS 종료·secret rotation·운영 관측성
- 모델 가중치, Authority, RBAC, ApprovalPolicy 또는 production code의 자율 변경
- 외부 시스템에 대한 물리적 exactly-once 전달
- inbound A2A server, Remote A2A Agent Card discovery crawl, 자동 Registry admission 또는
  Central Server A2A proxy
- streaming/polling/push callback 또는 non-text artifact/file/multi-modal A2A task를
  completed text-only 범위로 보이게 하는 방식
- Central Browser Frontend를 통한 Card Owner raw source/full draft 전송
- retire된 FastAPI HTML을 production fallback UI로 사용하는 방식
- Central Next SSO 추가 Registry User onboarding 전의 browser onboarding 또는 JIT provisioning
- browser OIDC의 remote global logout/revoke, IdP token revocation 또는 다른 device session 종료
- Bootstrap Admin Attestation과 OIDC device authorization 없이 만드는 최초 Registry User

## 7. local reference와 production/pilot acceptance gate

RB3의 유한 종료점은 `runnable_local_reference`(향후 support vocabulary에 추가할 승격 상태)의
증명입니다. 이는 test tenant로 설치·실행 가능한 세 artifact를 뜻하며 production/pilot
준비 완료와 다릅니다. production/pilot claim은 RB3 뒤 P1의 별 gate를 모두 통과해야 합니다.

### A. local reference 배포 artifact

- Browser Frontend는 standalone Node/Docker artifact, health/readiness와 runtime env
  fail-close 계약을 가진다.
- Central Server, Card Owner, Question User MCP는 각각 clean install 가능한 독립 bundle/image다.
  공통 source repository 또는 단일 monolithic wheel의 `aon-central`/`aon-owner`/`aon-mcp`
  entrypoint만으로는 완료가 아니다. 각 artifact의 module/tool/route allowlist와 excluded
  forbidden surface가 package/image 검사로 증명된다.
- `Central Server`와 `Card Owner`에 실제 패키지 진입점이 있다.
- 각 설치물에 `serve`, `doctor`, migration/upgrade 절차가 있다.
- demo/legacy import나 자동 fallback 없이 기동한다.
- Central Next와 Central API, Owner-local Next와 Owner API/Worker의 process·BFF·port·health
  경계가 ADR 0075와 일치한다.
- legacy 아홉 화면의 success/error/authority/API 계약이 해당 Central 또는 Owner Next route에
  모두 존재하며 `web/*.html` fallback은 0개다.

### B. local reference 관통

- test OIDC issuer, loopback TLS CA, test keychain을 명시적으로 사용한다.
- Central Server, Card Owner, Question User MCP를 clean install 후 별 process로 실행한다.
- SSO → Card 등록 → 로컬 저작 → Owner 검토 → 공개 → MCP 질문/조회가 관통한다.
- 재시작, 재-pair, revoke, Card transfer, 실패 복구를 검증한다.
- legacy 아홉 화면의 success/error/authority/API contract, fixture isolation과 raw/secret
  non-egress를 검증한다.

### C. production/pilot Manual Acceptance (P1)

- 실제 IdP discovery/JWKS/PKCE, public HTTPS, OS keychain과 Central/Owner/User의 분리 process,
  가능하면 분리 machine을 사용한다.
- production DB migration과 backup/restore가 검증된다.
- durable receipt, outbox, lease/recovery와 멱등성 경계가 명시된다.
- 조직 격리, 권한 회수, 감사, 보존 정책과 관측성이 검증된다.

### D. 공통 품질

- 결정론 unit/integration gate가 통과한다.
- A2A Remote Runtime을 채택하면 strict v1 REST, endpoint pinning/SSRF/redirect 차단, Remote
  A2A Agent Card digest, OOB credential 비노출과 failure no-SubmitAnswer를 검증한다.
- 분류 품질은 별 golden-set eval 기준을 통과한다.
- 독립 보안·회귀 리뷰에서 차단 문제가 없다.
- README의 모든 지원 주장은 실행 또는 계약 테스트에 대응한다.

현재는 이 gate를 통과하지 않았습니다.

Owner 설치는 재시작·중복 요청에서도 pair 상태를 추측하지 않아야 한다. 로컬 recovery store는
  Central issue/redeem 원장 anchor와 binding digest를 포함한 `intent_issued`,
  `redeem_submitted`, `credential_stored` snapshot을 제공하고, 다음 단계는 그 snapshot을
  기준으로만 CAS를 재개한다. snapshot 부재·손상·Central receipt 불일치는 paired가 아닌
  명시적 unavailable로 남긴다. 실제 Central receipt를 포함한 pair/redeem 관통은 아직 목표
  범위이며 현재 제공 범위로 주장하지 않는다.
  Owner orchestration의 request digest는 pairing code와 분리된 공개 projection으로 선행 CAS를
  가능하게 하며, Central의 code-verifier proof는 중앙 내부에만 남는다. 현재 이 orchestration은
  테스트된 component 경계이고 `aon-owner pair`/Owner API에 조립되기 전까지 설치 완료로
  표시하지 않는다.

### 7.1 회사 기본 운영 환경

회사 기본 운영 환경은 Windows이며 Docker Desktop은 사용할 수 없는 것으로 가정한다.
따라서 local reference의 설치·실행·Fast/Contract 검증은 PowerShell, Python 3.12+와 `uv`,
Node.js 24+/Corepack, SQLite만으로 가능해야 한다. WSL·Git Bash·Docker daemon은 지원
경로의 전제 조건이 아니며, Dockerfile은 선택적 Linux packaging 증거로만 취급한다.
Owner secret bundle은 Windows current-user DPAPI로 보호하고, macOS/Linux는 명시적 OS keychain을
사용한다. 어느 환경도 pairing credential/private key를 평문 파일로 fallback하지 않는다(ADR 0084).

## 8. 현재 개발 reference의 수용 동작

개발 기준선에서는 다음 동작을 회귀 방지 대상으로 둡니다.

- Next가 유일한 browser runtime이고 Developer API의 HTML route가 0개임
- BFF method/path/header allowlist와 Owner raw/draft 중계 금지
- standalone build, 선택적 Docker manifest, health/readiness 분리
- Question Request 생성과 명시 상태 조회
- Routed / Contested / Unowned 분리
- root User/Manager escalation
- 승인 전 답 비노출과 공통 Answer Finalization
- requester ownership 검증
- 단독 인사의 `non_actionable_conversation` 종결
- 인사+업무 질문 및 실제 no-match의 기존 라우팅 유지
- thin Question User MCP의 두 도구 manifest와 금지 도구 부재
- Fast/Contract/Full/Scale/Manual gate의 실행 시점 분리

이 목록은 production readiness가 아니라 현재 구현의 결정론 계약입니다.
