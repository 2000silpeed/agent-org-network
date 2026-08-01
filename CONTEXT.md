# Agent Org Network — Ubiquitous Language

- 기준일: 2026-07-31
- 실행 지원 SSOT: [`docs/support-contract.json`](docs/support-contract.json)
- 제품 요구: [`docs/prd-v0.md`](docs/prd-v0.md)
- 기술 설계: [`docs/trd-v0.md`](docs/trd-v0.md)

이 문서는 코드·테스트·문서에서 공통으로 쓰는 도메인 용어집입니다. 구현 이력과 세부 결정은
`docs/adr/`에 둡니다.

## 언어 규칙

- 맨 단어 `Agent`는 쓰지 않습니다.
- 사람은 `Registry User`, 최상위 사람은 `root User`, 카드 소유자는 `Card Owner`입니다.
- 실행 주체는 `Agent Runtime`, 능력 설명은 `Agent Card`입니다.
- 목표 중앙 설치물만 `Central Server`라고 부릅니다.
- 현재 Next + `web:app`은 `Developer Reference`, `server:central_app`은
  `Legacy Fixture`입니다.
- 구성요소가 테스트된 것과 사용자가 설치 가능한 것을 구분합니다.
- 상태 전이와 기록을 같은 의미로 쓰지 않습니다.

## 사람과 조직

**Registry User**

조직 graph에 등록된 사람입니다. email, Manager 관계와 조직 소속을 가집니다. production
목표에서는 검증된 OIDC identity proof가 전역 유일 email과 일치할 때만 현재 사용자로
해석합니다.

**root User**

조직 graph의 최상위 Registry User입니다. 실제 업무 질문이 0매칭되어 `Unowned`가 되면
최종 escalation 대상입니다. root User는 모든 질문의 자동 담당자가 아니라 담당 공백을
처분할 사람입니다.

**Card Owner**

Agent Card와 로컬 지식 작업공간을 소유하는 Registry User입니다. 원문, 전체 초안,
Owner Runtime credential, 로컬 Git/index는 Card Owner 경계에 남습니다.

**Question User**

조직 질문을 만들고 자신의 Question Request를 조회하는 Registry User입니다.

**Manager**

Unowned 또는 Deadlock을 request 범위에서 Assign/Dismiss하는 권한 있는 Registry User입니다.
Manager 처분은 조직 전체 Authority 규칙을 자동 변경하지 않습니다.

## 카드·실행·지식

**Agent Card**

조직 내 역할, 처리 가능한 의도, 입력/출력 계약, Owner와 under-claim을 설명하는 등록
artifact입니다. Authority를 자기보고하지 않습니다. 유효성 검사를 통과하지 않은 카드는
Registry에 들어갈 수 없습니다.

**Agent Runtime**

저장된 RouteTarget과 grounding evidence를 받아 답 후보를 만드는 실행 경계입니다. Runtime의
출력은 승인과 Answer Finalization 전까지 최종 답이 아닙니다.

**A2A Remote Runtime**

Card Owner가 local profile로 명시 선택한 Agent Runtime입니다. 하나의 pinned HTTPS A2A
endpoint로만 outbound 호출하며, strict A2A 1.0 `HTTP+JSON` REST의 direct Message 또는
completed Task 단일 text-only Artifact만 답 후보로 만듭니다. Central Server는 이 호출의
proxy·inbound server·discovery registry가 아닙니다.

**A2A Remote Runtime Profile**

pair된 Card Owner Installation과 정확한 Agent Card revision/digest에 결박된 owner-local
encrypted profile입니다. pinned endpoint, Remote A2A Agent Card digest, opaque OOB credential
reference만 가지며 Authority나 credential 원문을 가지지 않습니다.

**Remote A2A Agent Card**

외부 A2A service의 통신 self-description입니다. endpoint·protocol·authentication 검증에만
쓰는 untrusted interoperability metadata이며, Registry의 Agent Card admission, Authority,
under-claim, Knowledge Index source evidence를 바꾸지 않습니다.

**A2A Completed Text**

strict A2A 1.0 `HTTP+JSON` REST 호출이 direct Message 또는 terminal completed Task의 단일
text-only Artifact로 돌려준 결과입니다. 이 값은 기존 AnswerCandidate 경계로만 들어가며,
그 자체가 AnswerRecord나 승인 증거는 아닙니다.

**A2A Remote Failure**

endpoint/card/authentication/transport/protocol 또는 비완료 task의 구조화된 실패입니다.
SubmitAnswer나 AnswerCandidate가 아니며, Owner Worker는 이를 redacted local 결과로 처리하고
기존 dispatcher의 release/timeout/escalation 종착을 보존합니다.

**Authority**

어떤 principal이 어떤 action/resource를 수행할 수 있는지 판정하는 중앙 정책입니다.
RB3.2b.6 이전에는 `routing_rules.yaml` 또는 production Authority snapshot이 선언 원천입니다.
Central marker v20 이후에는 SQLite의 active `PolicyRevision` 하나만 runtime 선언 원천이며,
파일은 최초 bootstrap 또는 명시적 import input일 뿐 reload/fallback 원천이 아닙니다.

**PolicyRevision**

검증된 중앙 Authority 문서와 canonical digest를 strictly monotonic epoch에 결박한 immutable
SQLite aggregate입니다. `ActivePolicyPointer`는 조직마다 정확히 하나의 revision을 가리키며,
activate/rollback은 expected epoch+digest CAS로 새 epoch를 만들기 때문에 같은 digest로 돌아와도
ABA가 생기지 않습니다.

**Operational Change Approval Evidence**

ADR 0051의 운영 mutation 허용 증적입니다. PolicyRevision activate/rollback/import에서는 canonical
command digest와 write 직전 active pointer fingerprint에 exact 결박되고, write 직전과 transaction
precommit에 current validity를 재확인합니다. POST body에는 safe `evidence_id/evidence_digest`
reference만 들어가며 receipt/audit도 이 두 값만 보존합니다. missing/stale/foreign/mismatched/
consumed evidence는 fresh/replay 모두 write 0입니다.

**Windows Native Runtime Baseline**

회사 기본 운영 경계입니다. Python/`uv`, Node.js/Corepack, PowerShell과 SQLite만으로
Central API, Central Next, Card Owner process, Question User MCP를 실행할 수 있어야 하며
Docker Desktop·WSL·Git Bash는 prerequisite가 아닙니다. Dockerfile은 선택적 packaging
evidence이고 실행 지원 SSOT는 [`docs/support-contract.json`](docs/support-contract.json)입니다.

**Organizational Knowledge Format (OKF)**

Card Owner가 만든 조직 지식 단위와 관계의 형식입니다. 원문·전체 draft와 공개된 revision을
구분합니다.

**Knowledge Index**

승인·공개된 OKF에서 파생된 질문 grounding용 인덱스입니다. 로컬 draft index와 중앙에서
수용한 published index를 구분합니다.

## 실행 지원 상태

아래 vocabulary는 [`docs/support-contract.json`](docs/support-contract.json)의 exact
값입니다.

**Developer Reference — `runnable_developer_reference`**

Next Browser Frontend와 API-only Developer API로 재현 가능한 개발 기준입니다.
standalone frontend를 운영할 수 있지만 production 신원·Authority·Central Server 보장을
뜻하지 않습니다.

**Legacy Fixture — `runnable_legacy_fixture`**

과거 수동 시연 또는 인프로세스 테스트를 위해 남긴 실행 경로입니다. 목표 제품의
trust boundary를 증명하지 않습니다.

**Installable Dependent Client — `installable_dependent_client`**

로컬에 설치·실행할 수 있지만 외부 호환 서비스가 있어야 실제 기능이 성립하는 client입니다.
현재 `aon-mcp`가 여기에 해당합니다.

**Tested Component Factory — `tested_component_factory`**

의존성을 명시적으로 주입해 결정론 테스트로 검증하는 조립 팩토리입니다. console script,
모듈 수준 server app, 배포 설정이 없으면 사용자 제품으로 보지 않습니다.

**Product Target — `product_target_not_available`**

채택한 목표 아키텍처지만 현재 설치·실행 가능한 artifact가 없는 상태입니다. 현재
3-install이 여기에 해당합니다.

## 목표 설치물

**Central Server**

목표 제품의 중앙 trust artifact입니다. Registry, Authority, Question Request workflow,
승인된 Knowledge Index, AnswerRecord, safe audit/outbox를 소유합니다. 현재 Developer
Reference나 Legacy Fixture를 Central Server라고 부르지 않습니다.

**Central Next**

Central Server Installation 안에서 page, asset, same-origin Central BFF, liveness/readiness를
소유하는 standalone Node process입니다. local reference 기본 bind는 `127.0.0.1:3000`이며,
Central API의 configured private origin만 호출합니다. Owner-local source/draft/API를 proxy하지
않습니다.

**Central Next Artifact**

RB3.2b.1에서 기존 `frontend/`의 demo/Developer Reference 전제를 제거한 뒤 승격하는 Central
standalone build입니다. source checkout은 build된 `.next/standalone`만, 설치 package는 bundled
standalone만 실행합니다. 자동 build/download fallback은 artifact가 아닙니다.

**Central Web Process**

`aon-central web serve --profile CENTRAL_PROFILE`가 parent로 관리하는 Central Next child process입니다.
같은 strict Central profile에서 public origin을 검증하고, `127.0.0.1:3000` bind 및
`http://127.0.0.1:8010` Central API origin을 고정합니다. caller environment나 argument는 BFF upstream을
정할 수 없으며 SIGINT/SIGTERM/child exit는 parent가 전파합니다.

**Central API**

Central Server Installation의 Registry, Authority, durable workflow와 Central gateway를 HTTP로
노출하는 API-only process입니다. local reference 기본 bind는 `127.0.0.1:8010`이며 browser
HTML을 제공하지 않습니다. 모든 read/write는 server-derived principal과 current Authority로
재인가합니다.

**Central Question Intake**

Central API가 `Question User`의 검증된 OIDC identity를 Registry User로 결박하고 current
Authority를 확인한 뒤 durable `Received` Question Request를 만들거나 자기 요청만 읽는 최소
application 경계입니다. Router, ConflictCase, ManagerItem, AnswerRecord, onboarding, pairing을
소유하지 않습니다.

**Received Question Projection**

Central Question Intake가 사용자에게 반환하는 최소 read model입니다. `request_id`, 고정
`state="received"`, `created_at`만 포함합니다. 전체 Question Request 수명주기의
`RequestPending`이나 terminal outcome을 대신하거나 미래 routing/answer를 약속하지 않습니다.

**Bootstrap Admin Attestation**

최초 root User admission을 위해 `aon-central bootstrap-admin`이 읽는 one-time profile입니다.
raw 사용자, email, OIDC claim 또는 credential이 아니라 configured 조직·provider·Authority에
결박된 opaque reference와 digest만 가집니다. 이 attestation은 Authority를 선언하거나
`user.register`를 우회하지 않습니다.

**Bootstrap OIDC Device Authorizer**

device authorization grant를 수행하고 existing OIDC verifier로 identity를 검증하는 Central
installation port입니다. 검증된 email과 claim은 admission command를 만드는 동안 process memory에만
있으며 CLI, attestation, receipt, audit/outbox 또는 log로 egress하지 않습니다. verification URI와
one-time user code만 invoking process의 `/dev/tty`에 one-time 표시하며 normal stdout/stderr에는
쓰지 않습니다.

**Bootstrap State**

Central의 one-time 최초 root User admission 상태입니다. `Unmigrated`는 required schema
capability가 없음을, `BootstrapPending`은 schema가 있으나 immutable bootstrap seal이 없음을,
`BootstrapSealed`는 matching Registry receipt/audit/outbox와 immutable seal이 read-back됨을 뜻합니다.
`BootstrapSealed` 뒤 새 최초 admission은 불가하고 동일 command의 idempotent replay만 허용합니다.

**Owner API**

Card Owner Installation의 encrypted workspace, local draft, local Git, keychain-backed
credential과 Owner Worker control을 Owner-local Next에만 제공하는 API-only process입니다.
기본 bind는 `127.0.0.1:8012`이고 Central Next의 BFF나 public ingress가 아닙니다.

**Conflict Evidence Grant**

Contested Question Request에서 source Card Owner가 자기 local evidence를 열 수 있게 하는
case/candidate/concept/Card revision/TTL/single-use 결박의 중앙 control metadata입니다. 이 grant는
raw source/full draft, raw location, body digest가 아니며 Central은 그러한 body를 relay·저장·감사하지
않습니다. RB3.2b.5는 metadata list/detail만 제공하고 Owner-local open/release와 release receipt는
RB3.5입니다.

## Browser와 API 경계

**Browser Frontend**

지원되는 유일한 브라우저 화면 서버입니다. Next standalone Node process가 page, asset,
same-origin BFF, health/readiness를 소유합니다.

**Developer API**

현재 Question Request와 운영 기능을 JSON, SSE, WebSocket으로 제공하는 FastAPI 개발
조립입니다. HTML page와 browser redirect를 제공하지 않습니다.

**Backend-for-Frontend (BFF)**

Browser Frontend가 private Developer API에 전달하는 allowlisted same-origin 경계입니다.
method, path, header와 body size를 제한하며 요청이 upstream origin을 결정할 수 없습니다.

**Owner-local Browser Surface**

Card Owner 기기에서 raw source, full draft와 Owner credential을 다루는 별 browser
artifact입니다. Central Browser Frontend의 `/owner-api`나 upload proxy로 대체하지 않습니다.

**Owner-local Next**

Owner-local Browser Surface의 standalone Node process입니다. `Central Next`와 다른
artifact/env/BFF allowlist를 가지며 기본 loopback bind는 `127.0.0.1:3001`입니다. `/card`,
`/workspace`, `/drafts`, `/supervision`을 소유하고 Owner API만 호출합니다.

**Fast Gate**

모든 로컬 변경과 pull request에서 실행하는 90초 이하의 핵심 불변식·보안·frontend
검증입니다.

**Contract Gate**

API/BFF/runtime/standalone/Docker 경계를 확인하는 3분 이하의 계약 검증입니다.

**Full Gate**

전체 pytest, Pyright, Ruff와 frontend production build를 실행하는 main/nightly/release
검증입니다.

**Manual Acceptance**

Docker, HTTPS, 실제 IdP/keychain/별 process를 사람과 실제 환경으로 관통하는 검증입니다.
결정론 Fast/Contract/Full Gate와 섞어 완료 수치를 부풀리지 않습니다.

**Card Owner Installation**

목표 제품의 Owner trust artifact입니다. pair된 Card Owner가 로컬 source, draft,
Agent Runtime, review/publish 작업을 수행합니다. 현재 runnable production app은 없습니다.

**Question User MCP**

OIDC/PKCE로 pair한 Question User가 HTTPS gateway를 통해 `ask_org`와 `get_question`만
호출하는 thin client입니다. 현재 CLI는 Installable Dependent Client이며, 호환 Central
Server가 제공되지 않아 독립 end-to-end 제품은 아닙니다.

## Question Request 수명주기

**Question Request**

한 사용자 질문의 안정적인 aggregate입니다. Router보다 먼저 저장되며 request ID, requester,
원 질문, initial disposition, revision과 현재 상태를 가집니다.

**Received**

Question Request가 생성된 최초 상태입니다.

**Routed**

하나의 RouteTarget이 중앙 Authority로 확인된 실행 대기 상태입니다.

**Contested**

복수의 책임 후보가 있어 담당이 아직 확정되지 않은 상태입니다. 합의 또는 Manager 처분
전에는 임의 후보를 primary로 표시하지 않습니다.

**Unowned**

업무 질문이지만 책임 있는 Agent Card가 0개인 상태입니다. root User/Manager 처분으로
이어집니다. 단독 인사를 Unowned로 보내지 않습니다.

**AwaitingApproval**

답 후보가 중앙 ApprovalPolicy에 따라 사람 승인을 기다리는 상태입니다. 승인 전 후보
본문은 최종 답이 아닙니다.

**Answered**

Answer Finalization이 canonical AnswerRecord와 terminal evidence를 확정한 상태입니다.

**Declined**

사람 처분, 승인 반려 또는 non-actionable intake가 질문을 명시적으로 종결한 상태입니다.
reason code로 원인을 구분합니다.

**Failed**

처리 장애가 terminal outcome으로 기록된 상태입니다. pending이나 transport 끊김을 임의로
Failed로 바꾸지 않습니다.

**Central Browser Question Projection**

Central Next `/ask`의 safe lifecycle wire model입니다. `Received`는
`pending/received/routing/retryable=true`, `ReadyToDispatch`는
`pending/ready_to_dispatch/routed/true`, `AwaitingAnswer`는
`pending/awaiting_answer/routed/true`, `AwaitingApproval`은
`pending/awaiting_approval/routed/false`, `AwaitingConflict`는
`pending/awaiting_conflict/contested/false`로 투영합니다. `AwaitingManager`는 public kind가
`unowned|contested|dispatched`일 때 각각 `unowned|contested|routed`, 모두 `retryable=false`입니다.
`AnsweredRequest|DeclinedRequest|FailedRequest`는 각각
`answered/answered`, `declined/declined`, `failed/failed` terminal wire이고 retryable은 false입니다.
`AnsweredProjection`은 canonical GET과 SSE `done`에 동일한 DTO로만 쓰며 `mode`는 exact
`full|backup`, `review_status`는 exact `not_required|approved`입니다. 세부 route/DTO/auth/reconnect
계약은 ADR 0079가 소유합니다.

## 라우팅과 처분

**RouteTarget**

Question Request가 실행할 책임 대상을 sealed type으로 표현한 값입니다. `Routed`,
`Contested`, `Unowned` 또는 허용된 초기 non-actionable 종결을 구분합니다.

**Request-scoped Authority Grant**

Owner 합의나 Manager Assign이 한 Question Request의 책임자를 확정했다는 중앙 증거입니다.
같은 intent의 조직 전체 정책을 변경하지 않습니다.

**ConflictCase**

Contested 질문의 후보·합의·교착 상태를 추적하는 request-linked durable entity입니다. immutable
candidate snapshot, round/revision, current Card/Authority binding과 participant Concurrence를 가지며
`open|resolved|escalated` sealed 상태입니다. `agreed`는 Request를 `ReadyToDispatch`로, `deadlocked`는
`AwaitingManager`, `route_rejected`는 Declined로만 전이합니다.

**Concurrence**

ConflictCase 참여 Card Owner가 frozen candidate 중 primary 하나와 `keep_as_complement|withdraw` stance,
rationale, expected Case/**Request** round/revision으로 남기는 idempotent/CAS 투표입니다. round의 서로
다른 후보 Card Owner는 각각 정확히 한 표를 남기며 전원 일치는 agreed, 전원 투표의 불일치는 same-UoW
ManagerItem/AwaitingManager deadlock, valid Route Authority 거절은 route_rejected입니다. 직접 Owner API
또는 Agent Runtime 호출이 아닙니다.

**ManagerItem**

Unowned 또는 Deadlock에 대한 사람 처분을 추적하는 request-linked entity입니다.

**Resume Claim**

사람 처분 뒤 같은 Question Request를 정확히 한 번 재개하기 위한 경쟁 제어 증거입니다.

## Non-actionable Conversational Intake

조직 업무나 지식 처리를 요구하지 않는 exact-only 단독 인사의 초기 처분입니다.

- Question Request는 먼저 `Received`로 저장합니다.
- NFKC, casefold, 공백 정규화 후 전체 발화 allowlist만 비교합니다.
- 일치하면 revision 1에서
  `Declined(reason_code="non_actionable_conversation")`로 종결합니다.
- `initial_disposition="non_actionable"`, `intent=None` 조합만 허용합니다.
- HTTP create의 current `session.read`·`question.create` Authority 검증과 durable `Received`
  commit 뒤에는 routing/dispatch Authority, Router, ConflictCase, ManagerItem, Agent Runtime을
  호출하지 않습니다.
- `안녕하세요, 환불 규정은?` 같은 업무 질문은 정상 라우팅합니다.
- 실제 no-match는 기존 `Unowned` escalation을 유지합니다.

상세 결정은 [ADR 0071](docs/adr/0071-non-actionable-conversational-intake.md)을 따릅니다.

## 승인과 답

**AnswerCandidate**

Agent Runtime이 만든 비terminal 답 후보입니다.

**ApprovalPolicy**

AnswerCandidate에 사람 승인이 필요한지 판정하는 중앙 정책입니다.

**ApprovalItem**

request ID, revision, candidate snapshot, route evidence와 중앙 요구사항을 보존하는 승인
처리 단위입니다. Inbox list/detail projection은 current open Item과 exact
`AwaitingApproval(item_id)` Request를 같은 read snapshot에서 결박하고 그 Request의 current
`request_revision`을 반환합니다. disposition/reassign UI는 이 값을 expected Request revision으로 그대로
사용하며 추론하지 않습니다.

**Approval Disposition Application**

`AwaitingApproval`을 Answered 또는 Declined로 바꾸는 유일한 Central domain transition writer입니다.
current Registry User session/identity와 `approval.decide` Authority, same org/request, frozen candidate/
policy/binding, ApprovalItem·QuestionRequest expected revision을 한 transaction에서 확인합니다. RB3.2b.5의
approval UI/API는 이 경계를 호출할 뿐 직접 Request·AnswerRecord를 쓰지 않습니다.

**BackupReview**

terminal `mode="backup"` AnswerRecord 하나에 source record ID로 정확히 하나 결박되는 Card Owner의
durable 사후 검토 aggregate입니다. `approve|correct|dismiss` disposition을 가지며 correct는 원
AnswerRecord를 고치지 않고 immutable superseding AnswerCorrectionRecord를 append합니다. demo seed나
UI 조회가 producer가 아니며 terminal writer가 같은 transaction에 source-bound outbox intent를 append하고
lease/projector가 생성합니다. v17은 existing eligible terminal record도 immutable source receipt digest로
결정론 backfill하여 정확히 하나의 pending intent를 만듭니다.

**Reevaluation**

append-only `FeedbackRecord(verdict="bad")` 하나에 feedback ID로 정확히 하나 결박되는 Card Owner의
durable 후속 검토 aggregate입니다. `acknowledge|request_reanswer` 처분은 FeedbackRecord, AnswerRecord,
Question Request, Authority 또는 routing score를 rewrite하지 않습니다. request_reanswer는 immutable
follow-up record만 append하며 실제 재질문/dispatch는 별 recovery concern입니다. bad feedback writer가
같은 transaction에 feedback-bound outbox intent를 append하고 lease/projector가 생성합니다.
v17은 existing eligible bad feedback도 immutable source receipt digest로 결정론 backfill합니다.
승인된 v17 producer catalog 뒤 v18은 disposition head/receipt/audit와 immutable
`AnswerCorrectionRecord`·`ReanswerRequested` companion을 추가합니다.

**ApprovedCandidate**

Approve 또는 ApproveWithEdit 뒤 Answer Finalization으로 넘기는 후보입니다. 자체로는
terminal 답이 아닙니다.

**Answer Finalization**

승인된 후보를 AnswerRecord, Question Request terminal 전이, terminal audit,
SessionTurn과 delivery outbox에 결박하는 application 경계입니다.

**Owner Answer Ingest**

pending WorkTicket의 verified delivery binding이 Central에 넘기는 typed
`ticket_id, request_id, expected_request_revision, attempt, route, AnswerCandidate` handoff입니다.
owner/org/Agent Card를 body로 자기보고하지 않으며 Central이 ticket·Request·durable owner fence와
current binding을 다시 확인한 뒤 ApprovalPolicy 또는 Answer Finalization으로 보냅니다. browser API,
anonymous payload, Central A2A ingress/proxy나 실제 Owner transport를 뜻하지 않습니다.

**AnswerRecord**

Question User에게 반환할 canonical 최종 답과 출처·담당·결정 증거를 연결한 기록입니다.

**Question Feedback Evidence (FeedbackRecord)**

session-derived Question User가 자신이 소유한 Answered Question Request의 canonical AnswerRecord에 남기는
append-only immutable durable 증거입니다. `feedback.create`는 current same-org session/Authority와 path
Question Request·finalized AnswerRecord binding을 fresh/replay에 재검증하고, receipt와 safe audit companion은
별 record로 남깁니다. AnswerRecord 본문·출처·담당·mode, terminal Question Request, Card, Authority 또는
routing score를 변경하지 않습니다. anonymous feedback·record ID만 아는 feedback·legacy upsert와 구분합니다.

## 저작과 공개

**Production Registry User**

production 규칙으로 durable receipt와 함께 등록되는 Registry User 구성요소 용어입니다.
현재 사용자에게 배포된 Central Server 기능을 뜻하지 않습니다.

**Production Identity Session**

검증된 OIDC proof를 **기존** Registry User와 결박한 opaque Central browser session 구성요소입니다.
durable ID와 application principal은 `session_digest`뿐이며 raw session handle, token, code, verifier,
claim은 DB·port result·audit/outbox·JSON·log에 넣지 않습니다. establish는 current `session.establish`
Authority를 write 전과 transaction precommit에 확인하고, read는 every request current
`session.read` Authority를 확인합니다.

**Browser OIDC Transaction**

Central API가 authorization-code+PKCE callback을 한 번만 완료하도록 만드는 ephemeral durable
구성요소입니다. `transaction_digest`, state/nonce digest, expiry/consumed state만 저장하며 raw
authorization code는 OIDC transport adapter의 process memory에만 둡니다. raw PKCE verifier는
transaction digest를 key로 하는 1,024-entry bounded `BrowserPkceVerifierVault`에만 두며 durable
record/cookie에는 넣지 않습니다. Central API restart 또는 vault eviction이면 callback은 write 0으로
닫히고 새 login이 필요합니다. encrypted cookie key, secret provisioning과 rotation은 P1 범위입니다.

**Browser PKCE Verifier Vault**

Central API 한 process 안에서 pending Browser OIDC Transaction의 raw PKCE verifier만 TTL 동안
보관하는 bounded memory 구성요소입니다. DB나 profile/data directory/환경변수 secret으로 복구하지
않으며, capacity 부족·restart·eviction·fault는 session을 만들지 않는 unavailable 결과입니다.

**Browser Session Principal**

raw cookie ID가 아닌 `session_digest`와 existing Registry User/org binding으로 표현한 Central API
principal입니다. body/header/query의 user/org/role/token 자기보고나 browser localStorage identity를
대체하지 않습니다.

**Session-Derived Registry Registration Application**

`BrowserSessionPrincipal`에서만 actor/org를 얻어 Registry User 또는 Agent Card register-only
command를 만드는 Central application 경계입니다. global Registry store의 read-only authorizer를
변경하지 않고 request마다 scoped store/factory를 만들며, 같은 SQLite transaction에서 active
session row, existing Registry User binding과 shared Registry revision, 현재 Authority의
`user.register` 또는 `card.register`를 write 전과 precommit에 다시 확인합니다. marker v20 이전
구현은 file snapshot, 이후 구현은 DB active PolicyRevision만 사용합니다. body/header/query의
actor·org·role·session self-claim, JIT/invitation, Authority bypass는 이 경계에 없습니다.

**Policy-Controlled Card Owner Delegation**

`card.register`를 현재 중앙 Authority가 허용한 actor가 existing Registry User를 Agent Card의
`owner`로 지정하는 register-only admission입니다. actor 자신을 owner로 지정하는 self-registration도
같은 action의 한 경우일 뿐 별도 권한이 아니며, manager 관계나 request body는 위임 근거가 아닙니다.
Card Owner transfer/revoke는 기존 card를 바꾸는 별 전이로서 이 용어와 RB3.2b.3 범위 밖입니다.

**Card Owner Assignment**

Agent Card와 Registry User인 Card Owner의 current binding을 generation으로 나타내는 durable
aggregate입니다. 한 Card에는 active generation이 최대 하나이고 transfer는 old generation을
revoked로 닫고 successor를 열며, revoke는 successor 없이 닫습니다. required `AgentCard.owner`는
revoke 때 null로 만들지 않고 마지막 recorded Owner를 보존합니다. routing과 owner-scoped action은
current active generation만 사용합니다. Owner Installation credential/pairing의 실제 invalidation은
RB3.5입니다.

**Monotonic Session Cleanup**

`session.end`가 same session digest의 local session을 active로 되돌릴 수 없는 terminal state로
만들고 관련 `__Host-` cookie를 expire하는 동작입니다. current Authority revoke/unavailable도 cleanup을
막지 않지만, IdP logout, remote global revoke 또는 다른 device session 종료를 뜻하지 않습니다.

**Production Agent Card**

production Authority와 current Registry revision을 transaction 안에서 재검증해 등록하는
Agent Card 구성요소입니다.

**AuthoringRun**

Card Owner의 로컬 source/draft 작업과 중앙의 본문 없는 control aggregate를 연결합니다.
목표 상태 흐름은 Extracting → AwaitingOwnerReview → Reviewed → Publishing → Published입니다.

**Published Index Acceptance Receipt**

exact reviewed revision의 인덱스를 중앙이 수용했다는 immutable evidence입니다. receipt
존재만으로 외부 release, 다중 인스턴스 운영 또는 전체 제품 관통을 주장하지 않습니다.

## 내구성과 증거

**Domain Transition**

aggregate의 유효한 상태 변경입니다.

**Audit Record**

누가 어떤 결정을 했는지 추적하는 safe metadata 기록입니다. actor/action/resource/outcome,
command digest, receipt, current Authority revision/epoch/digest와 typed `SafeChange`만 가지며
question/answer/comment/rationale/raw source/full draft/URI/credential/session/claim을 가지지
않습니다. 상태 전이 자체가 아닙니다.

**Operational Event**

safe 운영 feed를 위한 immutable projection입니다. source writer는 Domain Transition과 같은 SQLite
transaction에 Audit Record와 event outbox intent를 쓰고 projector가 조직별 strictly monotonic
cursor를 부여합니다. local-reference 기본 retention은 조직별 최근 10,000건이며 오래되었거나
cursor gap이 있으면 typed `resync_required`로 canonical read를 요구합니다.

**YAML Authority Provenance**

v19에서 strict current YAML digest와 source grant digest가 exact 일치할 때만 쓰는
`yaml:<digest>` revision, epoch 1, same digest 세트입니다. org 기반 합성 digest, epoch 0,
합성 timestamp는 Authority 또는 historical evidence가 아니며 unavailable로 닫습니다.

**Organization Scorecard**

조직의 Card Owner별 quality/supervision/availability/freshness를 stable Owner ID 순서로 관찰하는
safe projection입니다. `scorecard.organization.read`를 쓰며 Owner self `scorecard.read`와
분리됩니다. rank/percentile/grade를 만들거나 Authority, assignment, routing, 인사고과를 바꾸지
않고 historical evidence는 기록 당시 Card Owner Assignment generation에 귀속합니다.

**Outbox Intent**

commit 뒤 외부 전달할 효과를 durable하게 예약하는 기록입니다. 실제 전달 receipt와
구분합니다.

**Receipt**

명령의 semantic identity, 결과와 정책/권한 evidence를 재조회할 수 있게 결박한 불변
증거입니다.

**Durable**

명시된 저장소가 프로세스 재시작 뒤 상태를 복원한다는 제한된 속성입니다. SQLite durable
component가 곧 PostgreSQL, multi-instance, backup/restore 또는 production-ready라는 뜻은
아닙니다.

**Idempotent Replay**

같은 idempotency key와 같은 semantic command가 저장된 canonical 결과로 수렴하는
동작입니다. 같은 key의 다른 command는 conflict입니다.

## 핵심 불변식

1. 어떤 업무 질문도 미아로 남지 않습니다. 0매칭은 root User/Manager로 escalation합니다.
2. 유효하지 않은 Agent Card는 등록하지 않습니다.
3. Authority는 중앙에서만 선언합니다.
4. 담당 확정 전 후보를 최종 담당으로 표시하지 않습니다.
5. 승인 전 후보 답을 최종 답으로 내보내지 않습니다.
6. 모든 사용자 표면은 같은 Question Request와 Answer Finalization을 사용합니다.
7. 전이, audit, outbox, delivery receipt를 서로 대체하지 않습니다.
8. demo·fixture·팩토리 테스트를 production artifact로 표현하지 않습니다.
9. 원문·전체 draft·Owner credential을 중앙 control evidence에 섞지 않습니다.
10. 지원 상태 변경은 코드, [`docs/support-contract.json`](docs/support-contract.json),
    README와 SSOT를 함께 갱신합니다.
11. A2A Remote Runtime은 Card Owner local profile의 pinned outbound endpoint만 호출하며,
    Remote A2A Agent Card가 내부 Agent Card·Authority·Registry를 자동 변경하지 않습니다.
12. A2A Remote Failure는 AnswerCandidate·SubmitAnswer·AnswerRecord로 위장하지 않습니다.
