# ADR 0067 — 제품 설치를 중앙 서버·Card Owner·질문 사용자 MCP의 세 신뢰 경계로 분리한다

- 상태: 채택(Accepted)
- 날짜: 2026-07-27
- 현재 artifact 해석: 이 ADR은 목표 trust boundary다. 현재 실행·지원 상태와 entrypoint
  존재 여부는 ADR 0072와 `docs/support-contract.json`이 우선한다.
- 계보: ADR 0009(페르소나별 surface), ADR 0021(verified-email OIDC 신원 매핑), ADR 0029·0030(Owner측 OKF 저작·크로스머신), ADR 0034·0064(Agent Card·Registry User 라이브 등록), ADR 0049(production bootstrap), ADR 0050(중앙 Authority/RBAC), ADR 0052(durable credential)를 잇는다.
- 적용 범위: 제품 설치 artifact, entrypoint, 설정·비밀·데이터 소유 경계, pairing/bootstrap, 제품형 온보딩과 기존 실행 진입점의 이행.
- 제외 범위: IdP 계정 생성·초대·JIT 프로비저닝, User edit/delete/re-parent, 다중 IdP, production 배포 완료 선언.

## 맥락과 충돌 검토

현재 구현에는 관리자 `POST /admin/users`, Agent Card 라이브 등록 서비스, validate-only `/builder`, Owner측 `/author/run`·`/author/publish`, `run_central`, `run_worker`, in-process `mcp_server`가 각각 존재한다. 그러나 설치 경계가 제품 계약으로 고정되지 않아 중앙 프로세스에 demo·Owner 저작·질문 MCP 조립이 함께 따라올 수 있고, `/author/run`은 transient라 새로고침·재시작 뒤 Owner 검토를 복원하지 못한다.

제품 요구는 다음 실제 동선을 하나로 잇는다.

```text
관리자 Registry User 등록
→ 기존 회사 SSO 신원 확인
→ Agent Card 라이브 등록
→ Owner 환경에서 문서 기반 OKF 초안 생성
→ durable Owner 검토
→ 승인된 OKF publish
→ 질문 사용자가 MCP로 질문·조회
```

이는 PRD/TRD의 기존 기능을 제품형 수직 흐름으로 잇는 확장이라 충돌하지 않는다. 다만 “SSO 연결”을 별도 `(issuer, sub) → User` 영속 row로 해석하면 ADR 0021 §3의 `User.email` 단일 매핑 SSOT와 정면 충돌한다. 따라서 이 ADR은 별 identity-binding row를 만들지 않고 verified email의 파생 결박을 유지한다.

## 결정 1 — 설치는 정확히 세 개다

공유 가능한 것은 frozen 계약·codec뿐이다. 한 설치가 다른 설치의 권한 있는 surface나 비밀·데이터 소유권을 함께 얻어서는 안 된다. 초기 개발에서는 한 저장소의 여러 entrypoint일 수 있으나 production artifact는 module/tool/route allowlist로 경계를 증명하고, 최종 배포는 공통 `agent-org-contracts`와 세 artifact/image로 분리한다.

### A. Central Server Installation

| 항목 | 계약 |
|---|---|
| artifact | `agent-org-central` |
| entrypoint | `aon-central serve`, `migrate`, `bootstrap-admin`, `reconcile` |
| 포함 기능 | Registry/조직 graph, 중앙 Authority/RBAC, routing, durable Question/Approval/Conflict/Manager/WorkTicket/Answer workflow, outbox/recovery, OIDC callback/session, admin/onboarding UI, 질문 API gateway |
| 설정 | `org_id`, public/base URL, OIDC issuer/audience/client/redirect, durable DB/outbox, 중앙 policy snapshot, TLS/proxy, retention/observability |
| 비밀 | OIDC client secret가 필요한 flow, session/signing key, DB/outbox credential, KMS/Vault reference, pairing signer |
| 권한·도구 | 중앙 manifest가 허용한 User/Card 등록, policy, approval, audit, recovery 도구. client가 보내는 role/권한 주장은 신뢰하지 않는다. |
| 데이터 소유 | canonical Registry User/Agent Card, Authority snapshot·receipt, durable workflow·AnswerRecord, safe audit/outbox, 승인된 KnowledgeIndex 목차, 저작 run의 본문 없는 control metadata·digest |
| UI | 관리자 온보딩, 운영·감사·처분 UI. Owner의 로컬 문서 편집기는 포함하지 않는다. |

**금지 surface:** raw 문서, staged/full OKF 본문, Owner OAuth/provider token, Owner git working tree와 private source credential, Owner draft runtime, demo seed·Fake source·무비밀번호 신원 선택·local LLM fallback을 production 기본으로 포함하지 않는다.

### B. Card Owner Installation

| 항목 | 계약 |
|---|---|
| artifact | `agent-org-owner` |
| entrypoint | `aon-owner pair`, `workspace serve`, `worker`, `doctor`, `unpair` |
| 포함 기능 | 자기 Agent Card 후보/미리보기·라이브 등록 client, 문서 ingest, 실 `OkfAuthor`, local durable AuthoringRun/draft repository, Owner review/edit/reject, git commit, 승인 index publish, 선택 Owner Worker |
| 설정 | central URL, `org_id`, paired Owner/Card ref, local workspace/git path, 허용 source root/media type, provider/model, worker enable, loopback UI bind |
| 비밀 | Owner OIDC/device session, scoped pairing/worker credential, Owner LLM OAuth token, git/source connector credential. OS keychain/secret store에 두고 중앙·평문 config로 보내지 않는다. |
| 권한·도구 | 자기 Card admission/under-claim, 자기 authoring run/review/publish, 자기 worker connect/submit만. 모든 mutation은 현재 중앙 grant를 다시 확인한다. |
| 데이터 소유 | raw 문서·추출문, full staged OKF/edge, local run snapshot, Owner git bundle, 최소화된 provider transcript |
| UI | Card 설정과 문서→초안→검토→publish, 선택 worker 상태. 기본 bind는 loopback이다. |

**금지 surface:** 다른 User/Card 관리, 중앙 Authority/routing policy 편집, 조직 전체 audit/Manager queue, 중앙 migration/reconcile, 타 Owner 문서/index, 질문 사용자 사칭, raw 중앙 업로드, production 기본 FakeAuthor/FakeGitGateway를 포함하지 않는다.

### C. Question User MCP Installation

| 항목 | 계약 |
|---|---|
| artifact | `agent-org-mcp-client` |
| entrypoint | `aon-mcp pair`, `serve-stdio`, `doctor`, `unpair` |
| 포함 도구 | `ask_org`, 자기 Request status/retrieve, 선택적 자기 결과 feedback |
| 설정 | 중앙 MCP/HTTPS endpoint, `org_id`, client name/id, OIDC device/PKCE callback, timeout/TLS trust |
| 비밀 | 사용자 OIDC access/refresh token 또는 user-scoped credential만. OS keychain에 두며 worker/admin/LLM credential은 없다. |
| 권한 | `question.create`, `question.read_own`, `answer.retrieve_own`, 선택 `feedback.submit`만 |
| 데이터 소유 | MCP host가 입력 질문·표시 답을 일시 보유할 수 있다. durable Request/Answer 원장은 중앙이며 local log는 본문을 redact한다. |
| UI | 설치·SSO pairing·연결 상태만. admin/Card/authoring UI는 없다. |

**금지 surface:** Registry/Card mutation, authoring/publish, worker submit, Authority/approval/Manager 도구, 중앙 DB/import, demo identity selector, embedded central router/runtime을 포함하지 않는다. `user_id`는 MCP 도구 인자나 환경변수로 받지 않고 검증된 principal에서만 얻는다.

## 결정 2 — Registry User와 기존 회사 SSO의 연결은 파생 결박이다

용어를 분리한다.

- **Registry User**는 조직 graph의 내부 사람 노드다.
- **OIDC Identity Proof**는 실 provider가 검증한 `iss/sub/email/email_verified/aud` claim이다.
- **SSO Identity Link**는 `email_verified=True`이고 claim email과 전역 유일한 `Registry User.email`이 정확히 일치할 때만 성립하는 파생 관계다.

별 `(issuer, sub)` identity row, IdP 계정 생성·초대, 첫 로그인 자동 User 생성(JIT)은 만들지 않는다. 관리자가 먼저 Registry User를 등록하고 기존 회사 SSO 계정이 그 email을 증명한다. 0매칭·복수매칭은 인증 실패다. email 변경, 다중 IdP 또는 `sub` 고정 binding이 실제로 필요해지면 ADR 0021을 명시적으로 supersede·보강하는 별 ADR과 migration이 선행돼야 한다.

## 결정 3 — 온보딩 mutation은 durable receipt로 새로고침·재시도에 견딘다

제품 UI는 다음 작은 수직 슬라이스를 순서대로 연다.

1. 제품 shell과 capability/readiness/status
2. Registry User 등록과 verified-email SSO 확인
3. Agent Card live admission
4. durable AuthoringRun 생성
5. Owner review
6. exact reviewed revision publish
7. thin MCP client
8. 실제 세 프로세스 수동 관통

User/Card mutation은 기존 `admit_user`·`AdminUserService`·`SqliteUserJournal`, `admit_card`·`AdminRegistryService`·`SqliteRegistryJournal`을 재사용하되 production 중앙 UoW로 승격한다. command는 서버 principal의 `org_id`, stable idempotency key, canonical command digest, expected revision을 갖고 receipt·safe audit·outbox intent를 같은 transaction에 확정한다. 같은 key+payload는 저장 결과를 replay하고 다른 payload는 conflict다.

`AuthoringRun`은 frozen 참조와 sealed 상태를 갖는다.

```text
Extracting
→ AwaitingOwnerReview
→ Reviewed
→ Publishing
→ Published
```

실패는 retry 가능한 control 상태로 남기며 승인 전 index·serving write는 0이다. 중앙에는 run/card/Owner/stage/revision, source/draft/review/index digest, count와 timestamp만 저장한다. raw body·초안 본문·LLM token은 Owner 설치가 소유한다. Owner 로컬 payload가 사라졌다면 중앙이 복구한 척하지 않고 review/publish를 unavailable로 닫는다.

review는 `(run_id, expected_revision, concept_id, source_digest, draft_digest, Approved|Edited|Rejected)`를 current Card Owner·중앙 `author.publish` 권한과 함께 CAS한다. 현재 O4는 body-free singleton bundle review이므로 `concept_id="bundle"`, `source_digest=source_set_digest`, `draft_digest=admitted_bundle_digest`만 허용한다. `Edited`는 수정 본문·patch를 중앙에 보내지 않으며 수정본을 publish하려면 local에서 새 `AuthoringRun`을 admit/complete해야 한다. publish는 exact reviewed revision만 `commit_okf_bundle → build_knowledge_index_from_okf → central index`로 보낸다. git commit 뒤 ledger 실패 같은 dual-write는 durable outbox/saga와 stable semantic key `(org, card, run, review_revision)`로 reconcile한다. 기존 transient `/author/run`→본문 재전송 `/author/publish` 직행은 production 온보딩에서 사용하지 않는다.

O5a는 그 saga의 외부 효과 전 단계로, exact `Reviewed(2, Approved)`만 `Publishing(3)`으로 claim한다. claim UoW는 current `author.publish`와 O4 review receipt/audit/outbox anchor를 재검증하고 자체 receipt/audit/outbox를 한 transaction에 남긴다. `Publishing`은 git commit·index acceptance·`Published` 성공이 아니며, Owner-local commit과 중앙 index acceptance receipt가 같은 `(org, agent_id, run_id, review_revision=2)` key에서 확인되기 전 terminal로 바뀌지 않는다. Card transfer/revoke 뒤 이미 수용된 index acceptance의 terminalization 권한은 후속 reconciliation 설계에서 중앙 immutable receipt 소비로 명시한다.

## 결정 4 — pairing/bootstrap

### 중앙 bootstrap

schema migration과 durable capability를 검증하고, 최소 root Registry User를 admission으로 설치한 뒤 real OIDC discovery/JWKS/redirect를 검증한다. root admin이 SSO로 신원을 증명해야 중앙 production capability를 seal한다. demo fixture는 파일럿 Registry에 섞지 않는다.

### Card Owner pairing

1. Owner가 기존 회사 SSO device/code flow로 중앙에 로그인한다.
2. 중앙은 current Registry User와 Owner eligibility를 재확인한다.
3. 중앙이 짧은 TTL·single-use·`audience=owner-install` pairing intent를 발급한다.
4. Owner 설치의 device public key와 intent를 교환한다.
5. Durable Credential Registry가 `org/Owner/device/generation/scope`를 receipt·safe audit·outbox와 기록한다. raw secret은 저장하지 않는다.
6. Owner 설치가 Agent Card를 admission+중앙 authorization으로 live 등록하고 returned revision/fingerprint를 local binding에 저장한다.
7. Owner Worker가 필요하면 Card/Owner/generation에 결박된 별 worker credential을 발급한다.

재-pair는 generation을 올려 구 credential을 폐기한다. Card ownership transfer 뒤 old Owner binding은 즉시 deny한다.

### 질문 MCP pairing

질문 사용자는 OIDC device 또는 authorization-code+PKCE로 신원을 증명한다. 중앙은 verified email로 Registry User를 확인하고 `audience=mcp-client`와 own-question scope만 가진 session/credential을 발급한다. 공유 API token, 수동 `user_id`, Owner Worker credential 재사용은 금지한다.

pairing redeem은 stable idempotency key+digest를 사용한다. TTL, single-use, audience, org, device key, credential generation을 모두 대조하며 authorization code/token은 audit/outbox에 싣지 않는다.

## 결정 5 — 기존 진입점 이행

- `scripts/run_central.sh`는 `aon-central serve` 경고 wrapper로 이행한다. production profile은 demo import/fallback, legacy source, 무비밀번호 로그인을 hard fail한다.
- `run_worker`/`scripts/run_worker.sh`는 `aon-owner worker --profile <paired-profile>`로 이행한다. CLI의 owner/role/token은 권위가 아니며 pairing receipt와 current 중앙 binding에서 결정한다. 검증 불가능한 legacy token은 자동 승격하지 않고 재-pair한다.
- `mcp_server.py`의 in-process `AskOrg` 조립은 중앙 gateway 또는 테스트 fixture에 남기고, 사용자 artifact는 authenticated remote thin client가 된다. `run_mcp.sh`는 `aon-mcp serve-stdio --profile` wrapper가 된다. production에서 환경변수 `user_id`는 hard error다.

한 릴리스 동안 개발용 wrapper는 가능하지만 wrapper가 production capability를 우회하거나 금지 surface를 다시 포함해서는 안 된다.

## Production gate

결정론 게이트:

- artifact별 module/route/tool allowlist와 forbidden-surface negative test
- 중앙 OIDC/durable source/Authority 미완전 조립의 시작 전 fail-closed
- User/Card admission, org 격리, 중앙 재인가, receipt/audit/outbox transaction
- 같은 pairing/command의 32-way 단일 winner, replay, different-payload conflict
- issue/delivery/receipt 각 fault point, revoke/re-pair, Card transfer stale binding
- raw·draft body와 OIDC/provider secret이 중앙 DB·wire·audit·outbox에 없다는 전수 검사
- reload/restart AuthoringRun 복원, Owner drift·Card revision drift, publish crash reconciliation
- MCP tool manifest가 own-question read/write 외 기능을 노출하지 않는다는 negative gate

게이트 밖 실제 관통:

```text
central admin Registry User 등록
→ real SSO
→ Owner 설치 pair
→ Agent Card live register
→ local 문서 ingest/LLM draft
→ Owner review/publish
→ 질문 MCP pair
→ ask/retrieve
```

이를 중앙·Owner·질문 MCP의 세 별도 프로세스, 가능하면 세 머신에서 수동 시연한다. 실제 OIDC discovery/JWKS/PKCE, TLS, OS keychain, owner-side LLM/git/index를 사용한다. 이 시연은 결정론 gate 통과로 계상하지 않는다. PostgreSQL 다중 인스턴스·P17.13의 CSRF/rate-limit/비밀관리/backup/관측성/접근성 전에는 production 또는 파일럿 준비 완료를 주장하지 않는다.

## 결과

설치 경계가 데이터와 권한 경계가 된다. 중앙은 책임·권한·durable workflow와 승인된 목차를 소유하고, Card Owner는 raw 문서·초안·OKF 본문과 저작 비밀을 소유하며, 질문 MCP는 자기 질문 surface만 갖는다. UI를 한 화면으로 이어도 세 artifact의 trust boundary를 합치지 않는다.

## 2026-07-27 O1 구현 정밀화

Registry User의 canonical email 원문은 authoritative User row 한 곳에만 저장한다. identity proof·server-side session·command receipt·audit·outbox는 email/issuer/provider/token/`sub`/claims 원문을 저장하지 않고 digest와 opaque reference만 사용한다. production 브라우저 session은 CSPRNG opaque ID이고, OIDC authorization-code+PKCE transaction은 state·nonce·browser binding·TTL·single-use를 모두 검증한다. verified email은 전역 유일 User와 정확히 일치해야 하며 0매칭은 JIT 생성 없이 거부한다. 실제 회사 IdP·HTTPS redirect/TLS 수동 관통은 O7 전까지 별 게이트다.

## 2026-07-27 O2 구현 정밀화

Agent Card live registration의 권위는 org-scoped authoritative SQLite row와 receipt다. validate/YAML preview는 완료 증거가 아니다. O1 User component의 Owner/Maintainer와 shared registry revision을 같은 transaction에서 확인하고, transaction-current `card.register` authorization, card row, receipt, safe audit/outbox를 함께 확정한다. 카드의 domains·can/cannot-answer·approval metadata는 under-claim이며 중앙 role/action grant로 해석하지 않는다. 기본 current User는 자기 소유 Card만 등록할 수 있고 타 Owner 등록은 별 exact delegation이 필요하다. 이 단계는 register-only이며 ownership transfer/update/deactivate와 credential lifecycle은 포함하지 않는다.
