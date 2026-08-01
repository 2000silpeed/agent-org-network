# ADR 0075 — 설치 가능한 세 artifact와 기능 보존 Next 이관

- 상태: Accepted (P0 target; RB3.1a/RB3.2a/RB3.2b.1 component 구현됨,
  독립 artifact acceptance·지원 수준 승격은 미완료)
- 날짜: 2026-07-30
- 대체/보강: ADR 0067의 artifact 이름을 실행 가능한 단계로 구체화하고, ADR 0072 §4와
  ADR 0073 §2의 “명령 부재/기능 backlog” 해석을 이 ADR의 P0 계획으로 대체한다.
- 관련: ADR 0004(중앙 Authority), ADR 0005(조직 graph), ADR 0067, ADR 0070, ADR 0074

## 결정

목표 제품은 정확히 세 설치 artifact다. 이 결정은 `aon-central`과 `aon-owner`가 이미
제공된다는 주장이 아니다. `docs/support-contract.json`의 현재 상태는 구현과 acceptance가
동시에 추가될 때까지 `product_target_not_available`로 유지한다.

| 설치 artifact | 실행 process | local reference 기본 port | 소유 경계 |
|---|---|---:|---|
| Central Server | Central Next, Central API | `127.0.0.1:3000`, `127.0.0.1:8010` | Registry, Authority, durable Question/Approval/Conflict/Manager workflow, AnswerRecord, published Knowledge Index, audit/outbox |
| Card Owner Installation | Owner-local Next, Owner API, 선택 Owner Worker | `127.0.0.1:3001`, `127.0.0.1:8012`, listener 없음 | raw source, full draft, local AuthoringRun/workspace/Git, provider·A2A credential |
| Question User MCP | stdio client | listener 없음 | OIDC client profile/keychain reference와 일시적 질문·표시 결과 |

Central Next와 Owner-local Next는 모두 standalone Node process지만 서로 다른 artifact, env,
BFF allowlist와 deployable unit이다. Central API는 public browser API가 아니라 Central
Next가 같은 설치의 private network에서 호출하는 JSON/SSE/WebSocket process다. Owner API는
기본 loopback만 bind하며 Owner-local Next만 호출한다. port는 local reference 기본값일 뿐,
production의 public origin이나 TLS 종료를 뜻하지 않는다.

### 명령과 composition root 목표 shape

다음은 구현할 명령 shape이며, 현재 실행 명령이 아니다.

```text
aon-central migrate | doctor | bootstrap-admin | api serve
aon-central web serve              # Central Next standalone child/configuration
aon-central reconcile

aon-owner pair | unpair | doctor
aon-owner api serve                # loopback Owner API
aon-owner workspace serve          # Owner-local Next standalone child/configuration
aon-owner worker --profile <paired-profile>

aon-mcp pair | serve-stdio         # 기존 thin client manifest 유지
```

`central_composition.py`는 one durable DB/Registry/Authority snapshot을 한 번만 조립하고
`central_api.py`에 Central API ASGI app을 노출한다. `central_cli.py`만 설정·migration·doctor를
읽는다. `owner_composition.py`는 paired local profile, encrypted workspace, local repository,
keychain credential provider와 optional `Agent Runtime`을 조립하고 `owner_api.py`와
`owner_cli.py`에 각각 ASGI/command root를 둔다. Central Next는 `frontend/`를 Central Next로
승격하거나 같은 artifact 안에서 명시 rename하고, Owner Next는 별 `owner-frontend/` package를
둔다. fixture `server.py`, `web.py`, `worker.py`, `mcp_server.py`, demo seed 또는 Fake
runtime은 어느 production composition root도 import하지 않는다.

### 연결·인증 계약

1. Question User MCP → Central Question Gateway는 HTTPS, OIDC PKCE/device pairing,
   `audience=mcp-client` credential로만 연결한다. 도구는 정확히 `ask_org`, `get_question`이다.
2. Central Next → Central API는 static configured private origin만 쓴다. Central API는
   verified OIDC/session principal과 current central Authority를 every read/write에 확인한다.
3. Owner-local Next → Owner API는 loopback same-install BFF다. Owner API는 keychain profile의
   paired Owner/Card/device generation과 Central-issued binding을 확인한다.
4. Owner → Central control call은 HTTPS와 paired device credential/OIDC proof를 사용한다.
   command마다 org, Card revision/digest, idempotency key, expected revision과 current grant를
   대조한다. Central은 receipt/digest/status만 보관한다.
5. Owner Worker → Central은 paired worker credential으로 RouteTarget/grounding evidence를
   받고 AnswerCandidate를 submit한다. A2A Remote Runtime은 Owner → pinned external endpoint
   outbound only이며 Central은 proxy/inbound/discovery를 제공하지 않는다.

raw source, full draft, provider/OOB/git secret, Owner working tree는 Central DB, API wire,
Central BFF, audit/outbox에 들어가지 않는다. Central policy/Authority, Registry mutation,
Manager queue, 다른 Owner workspace, Central DB/migration은 Owner artifact에 들어가지 않는다.
MCP profile/argument/environment의 `user_id`, org, role, token 주장은 신원·권한 근거가 아니다.

### legacy HTML 기능의 완전 이관 계약

`web/*.html`은 다시 runtime fallback으로 열지 않는다. 아래는 각 기능의 destination,
성공/실패 투영, Authority와 aggregate/API 계약이다. “다음 backlog”는 허용되지 않는다.

| legacy | Next destination | aggregate/API와 성공·실패 계약 | Authority |
|---|---|---|---|
| `index.html` | Central Next `/ask` | `QuestionRequest`: create/stream/retrieve, canonical `AnswerRecord`, feedback. `Received/Routed/Contested/Unowned/AwaitingApproval/Answered/Declined/Failed`를 모두 투영하고 transport failure는 request ID 재조회로 닫는다. | create/read-own/feedback only |
| `inbox.html` | Central Next `/inbox` | `ConflictCase` concur, `BackupReview`, `Reevaluation`, `ApprovalItem` list/detail/decide/reassign. conflict stale revision·denied·unavailable를 명시 표시한다. | 후보 Owner 또는 승인자/Manager의 current request-scoped grant |
| `builder.html` | Owner Next `/card`와 `/workspace` | `CardCandidate` validate/preview, local OKF bundle edit/commit, `AuthoringRun` start/review/publish/recovery. body는 Owner API만 받으며 publish는 exact reviewed revision receipt만 Central에 보낸다. | paired current Card Owner; central admission/publish 재인가 |
| `admin.html` | Central Next `/onboarding` | `RegistryUser` admission, `AgentCard` live admission, ownership transfer/revoke, owner scorecard. idempotent receipt/replay와 CAS conflict를 보인다. | central `user.register`, `card.register`, transfer grant |
| `console-feed.html` | Central Next `/console/feed` | `OperationalFeed` SSE reconnect, session/credential control, failure/revocation state. | central operations grant |
| `monitor.html` | Central Next `/console/audit` | `AuditRecord` list와 detail drill-down; redacted safe audit only. | central audit read grant |
| `org.html` | Central Next `/console/org` | Registry graph (`owns`/`manages`/`maintains`) detail와 Authority policy read/edit. invalid mutation/epoch conflict를 명시한다. | central graph/policy grant |
| `owner-monitor.html` | Owner Next `/supervision` | 자기 Card의 `AnswerRecord` supervision, presence, correction, scorecard. 다른 Owner 비교/열람은 없다. | paired current Card Owner + central reauthorization |
| `owner-drafts.html` | Owner Next `/drafts` | local `WorkTicket` draft list, approve/edit submit, restart-safe draft recovery; unavailable/revoked binding은 local draft를 Central에 제출하지 않는다. | paired current Card Owner |

`inbox.html`의 “연관 문서 펼침”은 Central relay로 이관할 수 없다. Central `/inbox`에는
published Knowledge Index metadata와 `ConflictEvidenceGrant`만 보이고, raw/full evidence는
해당 Card Owner의 `/workspace/conflicts/{case_id}/evidence/{concept_id}`에서 local workspace로
직접 연다. 이 grant는 case/candidate/revision/TTL/single-use를 bind한다. 다른 Owner의 raw
evidence를 보려면 source Card Owner의 explicit release가 새 digest/receipt로 남아야 하며,
Central은 본문을 transport·저장·log하지 않는다. 이것은 legacy의 중앙 raw relay를 보안상
동일한 “근거 열람” 기능으로 보존하는 유일한 허용 형태다.

### 검증과 승격

local reference의 결정론 acceptance는 test OIDC issuer, loopback TLS CA, test keychain과
three separate processes를 사용한다. install/doctor, no fixture import, route/tool allowlist,
port/bind, receipt idempotency, transfer/revoke, raw/secret non-egress, 9 surface success/error
contract가 Fast/Contract/Affected gate에서 증명되어야 한다. 이 test tenant는 real IdP,
public TLS, OS keychain, production secret manager를 대신하지 않는다.

`runnable_local_reference`는 향후 support vocabulary에 추가할 수 있는 승격 후보일 뿐 현재
값이 아니다. 그 승격에는 위 local evidence와 설치 artifact가 필요하다. production/pilot
claim에는 별도로 real IdP discovery/JWKS/PKCE, HTTPS, OS keychain, distinct machine/process,
re-pair/revoke/failure recovery, backup/restore, observability와 security review의 Manual
Acceptance가 필요하다.

## Addendum A — RB3.1a Central Question Intake 최소 수직 슬라이스 (2026-07-31)

RB3.1의 첫 구현 단위는 routing, AnswerRecord, ConflictCase, ManagerItem, onboarding 또는
pairing을 조립하지 않는 **Central Question Intake**다. 이 slice는 durable `Received` 생성과
자기 요청 조회만 제공한다. 기존 `QuestionResolutionApplication`은 접수 뒤 initial routing을
진행하며 durable Conflict/Manager store를 요구하므로 이 slice에 재사용하지 않는다.

`CentralQuestionIntakeApplication`은 기존 `QuestionRequest.receive`,
`SqliteQuestionRequestStore`, `CentralAuthorizer`, request-id factory와 주입 clock만 소비한다.
새 Question Request domain state, 별 aggregate, routing/answer application은 만들지 않는다.
생성은 `Received`, revision `0`, `created_at == updated_at`의 기존 aggregate 불변식을 보존하고
SQLite에 durable create 한다. 조회는 저장소 read 뒤 org와 requester의 exact ownership 및
current Authority를 매번 재검증한다. OIDC claim은 issuer/audience/JWKS 검증을 거친 뒤에만
Registry User로 해석하며, Registry에 없는 identity·조직 불일치·현재 grant 부재는 principal이나
권한의 대체 근거가 아니다.

### 구성과 migration

local-reference Central profile은 다음 exact field를 요구한다:
`org_id`, `oidc_provider_id`, `oidc_issuer`, `oidc_audience`, `oidc_jwks_url`,
`authority_snapshot_path`, 기존 `database_path`, `data_directory`, `bind_host`, `port`.
`bind_host=127.0.0.1`, local API 기본 `port=8010`만 허용한다. provider/issuer/audience/JWKS와
Authority path는 빈 값·상대 path·caller supplied override를 허용하지 않는다. 이 설정은 실제
OIDC verifier와 Registry/Authority snapshot을 조립할 다음 구현의 입력이며, profile에 값이
있다는 사실만으로 production support를 주장하지 않는다.

migration은 Question Request SQLite table/index를 먼저 성공적으로 만들거나 검증하고, 그 뒤
`aon_installation_schema`의 Central marker를 마지막 write로 남긴다(marker-last). marker가
있어도 필요한 Question Request schema capability가 없거나 Registry bootstrap이 없으면
`/readyz`는 `503 central_intake_unavailable`이다. bootstrap은 적어도 configured `org_id`의
Registry User identity binding과 현재 Authority snapshot을 durable하게 제공해야 하며, 이
slice는 bootstrap-admin을 구현하거나 임의 demo user를 seed하지 않는다.

### HTTP 계약과 오류

Central API의 이 slice route는 정확히 네 개다: `GET /healthz`, `GET /readyz`,
`POST /v1/questions`, `GET /v1/questions/{request_id}`. browser HTML, stream, routing, answer,
onboarding, pairing, Manager/Conflict route는 추가하지 않는다. write/read 모두 verified OIDC →
Registry User → current Authority 순서를 따른다. POST body는 `{ "question": string }`이며
session/context client claim은 받지 않는다. 성공 POST는 `201`, 성공 GET은 `200`과 아래
`ReceivedQuestionProjection` JSON을 반환한다.

```json
{
  "request_id": "...",
  "state": "received",
  "created_at": "RFC 3339 UTC instant"
}
```

이는 기존 `RequestPending`/`RequestNotFound`를 재사용하지 않는 전용 최소 projection이다.
그 DTO들은 전체 resolution lifecycle의 pending/terminal 의미를 갖고 이 slice가 아직 소유하지
않은 routing/terminal 약속을 wire에 누출한다. GET의 미존재와 타 조직/타 requester/denied는
모두 `404 question_not_found`로 평탄화한다. 검증된 principal이 없으면 `401 oidc_unauthenticated`,
Registry User 또는 current Authority가 없거나 권한이 없으면 `403 question_forbidden`, 잘못된
body/request id면 `422 invalid_question_request`, request-id 충돌이면 `409 question_request_conflict`,
SQLite/Authority/OIDC 의존성 unavailable 또는 readiness 미충족이면
`503 central_intake_unavailable`을 반환한다. 오류 body는 정확히
`{ "error": "<code>" }`이며 내부 원인·다른 Question Request 존재를 노출하지 않는다.

### acceptance와 지원 경계

결정론 acceptance는 marker-last crash/retry, durable restart read-own, 동일 request-id conflict,
OIDC issuer/audience/JWKS failure, Registry identity/org mismatch, Authority revoke, denied/not-found
평탄화, ready-before-migrate 및 bootstrap-absent `503`, 그리고 네 route allowlist를 증명한다.
현재 support status는 계속 `product_target_not_available`이며 Central Server,
`runnable_local_reference`, Question User MCP end-to-end 또는 RB3.1 완료로 승격하지 않는다.
future completion schema가 아직 없으므로 이 slice는 `Received` 이후 전이를 만들거나 terminal
결과를 약속하지 않는다. completion schema/UoW와 routing·Conflict/Manager durable store가 같은
composition에 capability로 조립되고 별 acceptance를 통과하기 전까지 이 차단 조건을 해제하지
않는다.

## Addendum B — RB3.2a one-time Bootstrap Admin admission (2026-07-31)

Central Question Intake가 의존하는 첫 configured-org Registry User는 demo seed, raw SQLite,
CLI user/email 입력으로 만들지 않는다. [ADR 0076](0076-one-time-oidc-bootstrap-admin-admission.md)의
`aon-central bootstrap-admin --profile --attestation`만 one-time OIDC device authorization과
current central `user.register` Authority로 root User를 durable admission한다. attestation에는
raw email/claim/token이 아닌 digest와 opaque reference만 남기고, existing
`SqliteProductionRegistryUsers` receipt/audit/outbox를 재사용한다.

이 slice는 실제 Central component 구현이다. configured issuer의 RFC 8628 device authorization
grant를 수행하고 existing OIDC verifier로 다시 검증하며, 같은 Central SQLite DB에 최초 root
User의 Registry receipt/audit/outbox와 immutable bootstrap seal을 결박한다. 같은 command의
restart replay만 read-back으로 수렴하고, 서로 다른 attestation·identity·policy·command는
deny 또는 conflict로 닫는다. verification URI와 one-time user code는 normal stdout/stderr가
아닌 invoking process의 `/dev/tty`에만 표시한다. non-TTY와 dependency failure는 write 없이
unavailable로 종료한다.

이 CLI slice는 browser UI를 열지 않는다. Central Next와 Owner-local Next는 서로 다른
frontend/backend installation artifact다. Central bootstrap command는 Owner API, Owner-local Next,
raw source, full draft, Owner credential 또는 A2A 경로를 호출하거나 중계하지 않는다. 반대로
Card Owner Installation은 Central Registry/Authority mutation이나 Central DB migration을 갖지
않는다. `frontend/`는 RB3.2b.1이 demo/Developer Reference 전제를 제거하고 packaging/process
contract를 통과할 때에만 Central 제품 artifact로 승격할 수 있으며, Central Next SSO의 추가
Registry User onboarding은 RB3.2b.2–.3으로 분리한다.

`aon-central bootstrap-admin`은 현재 `tested_component_factory` evidence다. 이 addendum은
`product_target_not_available`인 3-install 상태, production/pilot 상태, clean-install local
reference acceptance 또는 Central Server 전체 workflow 완성을 바꾸지 않는다. RB3.2a의 code
review·gate와 test OIDC Manual Acceptance는 TASK에서 별도로 닫는다.

## Addendum C — RB3.2b.1 Central Next packaging·process contract (2026-07-31)

RB3.2b.1은 existing `frontend/`를 duplicate `central-frontend/` 없이 Central Next로 package/run하는
단일 component slice이며 구현·검증됐다. `frontend/`의 일반 지원 상태는 계속 Developer Reference이고,
Owner-local Next는 독립 `owner-frontend/` artifact다. 이 결정은 Central/Owner browser process를
섞지 않으며 `web/*.html`, demo identity, Developer API, fixture import를 Central product fallback으로
허용하지 않는다.

exact command는 `aon-central web serve --profile CENTRAL_PROFILE`이다. profile은 Central CLI의
strict loader만 사용하고 source/installed artifact 위치, Node executable, profile의 required
HTTPS `central_public_origin`을 bind 전에 검사한다. Central child env는 CLI가 새로 만든
`AON_FRONTEND_MODE=central-local-reference`, `AON_PUBLIC_ORIGIN`,
`AON_BACKEND_URL=http://127.0.0.1:8010`, `HOSTNAME=127.0.0.1`, `PORT=3000`으로 고정한다. caller
environment, command argument 또는 profile field가 backend host/port/upstream을 바꾸는 경우는
configuration denial이다. `central_public_origin`은 public browser origin만 나타내며 path, HTTP,
arbitrary backend URL을 허용하지 않는다.

source checkout은 이미 build된 `frontend/.next/standalone/server.js`와 필요한 static/public payload를,
installed package는 bundled 같은 standalone layout과 manifest를 요구한다. command는 `pnpm build`,
`npx`, artifact download, network fetch를 실행하지 않는다. 누락·corrupt artifact 또는 unavailable
Node는 bind 전에 configuration exit로 닫는다. parent command는 child only를 관리하며 Central API를
함께 시작하지 않는다. child unexpected exit status를 반환하고 SIGINT/SIGTERM에는 terminate → bounded
wait → kill 순으로 orphan 없이 종료한다. `/healthz`는 child liveness, `/readyz`는 fixed Central API
readiness를 표현한다.

Contract acceptance는 source/installed discovery, literal bind, fixed child env, upstream override
rejection, lifecycle forwarding 및 Central BFF의 Owner API/Owner-local Next/raw source/full draft/Owner
credential/A2A route·import·allowlist 0을 다룬다. browser OIDC/session, additional Registry User,
full Question lifecycle, inbox, console/admin parity와 7-route integration은 RB3.2b.2–.7에서만
추가한다. Fast 486 Python + frontend 37, Contract 163, scoped Pyright/Ruff와 prebuilt wheel
installed artifact/Node `/healthz` smoke를 완료 evidence로 남긴다. 이 addendum은 support status,
Full Gate 시점(RB3.8), clean-install local reference 또는 production/pilot claim을 승격하지 않는다.

## Addendum D — action-level parity, dependency order, independent installation acceptance (2026-07-31)

`docs/frontend-runtime-parity.md`는 이 ADR의 executable P0 acceptance matrix다. 각 legacy 행은
destination만이 아니라 action/API, canonical success projection, 401/403/404/409/422/503/network
failure, Authority actor와 negative test를 가져야 한다. read-only mock, route shell 또는 legacy
Developer API BFF 연결은 migration 완료가 아니다. Central destination은 정확히 일곱 개다:
`/ask`, `/inbox`, `/onboarding`, `/admin`, `/console/feed`, `/console/audit`, `/console/org`.
`builder`/`author`는 Owner `/card`/`/workspace`로만, `owner-drafts`/`owner-monitor`는 Owner
`/drafts`/`/supervision`으로만 간다. Central artifact는 demo identity/session, Owner builder/author,
raw evidence relay를 포함하거나 fallback으로 열지 않는다.

Central `/inbox`의 RB3.2b.5는 conflict/backup-review/reevaluation/approval **metadata/control half**다.
Central은 `ConflictEvidenceGrant` metadata만 반환한다. source Owner raw/full evidence를 열거나 다른
Owner에게 release하는 기능은 paired Owner workspace가 있어야 하므로 RB3.5에서 완료한다. grant는
case/candidate/revision/TTL/single-use에 결박하고 release는 source Owner, recipient와 digest receipt에
결박한다. Central은 어느 단계에도 evidence body를 proxy/store/audit/outbox 하지 않는다.

Owner dependency order는 RB3.3a paired workspace/API foundation → RB3.3b versioned Central
owner-scoped control API → RB3.4 Owner-local Next → RB3.5 lifecycle/evidence completion이다. RB3.3b는
self AnswerRecord/presence/correction/own scorecard를 every-read/every-write reauthorize하고 다른
Owner/Card 및 self-reported owner/role을 deny한다. 따라서 RB3.4가 쓰는 control API를 RB3.5까지
미루지 않는다. Central Question lifecycle UI/API는 RB3.2b.4에서 durable 상태를 완성할 수 있으나,
product composition에 Fake Owner Runtime을 넣어 cross-install answer completion을 주장하지 않는다.
실제 Owner answer/publish/evidence flow는 RB3.5와 RB3.7의 별 process acceptance에서 닫는다.

ADR 0067 결정 1에 따라 RB3.8의 “각각 설치”는 공통 계약 package를 제외한 세 독립
install bundle/image, 즉 Central, Card Owner, Question User MCP artifact를 뜻한다. 한 monolithic
wheel에 세 console entrypoint가 있는 것은 개발·component delivery에는 허용되지만 3-install
acceptance가 아니다. RB3.8은 각각 clean install한 bundle/image에서 module/tool/route allowlist와
상대 artifact의 forbidden module, frontend payload, route/tool, secret/data directory 부재를 검사한
뒤 세 process를 기동한다. 구현된 RB3.2b.1의 bundled Central standalone component는 이 최종 분리를
대체하지 않으며 support status는 계속 `product_target_not_available`이다.

Full Gate는 위 모든 RB3.2b–RB3.7 acceptance와 independent review 뒤 RB3.8에서 정확히 한 번
실행한다. 그 전 slice는 Fast, Contract와 affected deterministic gate만 실행한다.
