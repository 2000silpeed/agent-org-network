# ADR 0081 — Central control plane의 durable PolicyRevision, Card Owner assignment와 redacted observability

- 상태: accepted (2026-07-31)
- 적용: RB3.2b.6-A 설계 계약. 구현, support 승격 또는 Full Gate 완료를 뜻하지 않는다.
- 계보: ADR 0004(Authority 중앙), ADR 0034(Card Owner transfer), ADR 0035(Scorecard), ADR
  0050(Central Authority), ADR 0051(운영 변경 승인·감사), ADR 0075(feature-preserving Next migration).
- 대체 범위: ADR 0050의 startup-immutable YAML runtime Authority와 ADR 0034의 in-process
  `card.owner` 교체+legacy token revoke를 RB3.2b.6 product path에서 아래 계약으로 대체한다.

## 맥락

RB3.2b.1–5는 Central Next, session-derived admission, Question lifecycle와 Inbox를 durable
SQLite control plane에 연결했다. `/console`과 `/admin`에는 아직 세 종류의 split-brain이 남아 있다.

1. 실행 중 Authority writer들은 `routing_rules.yaml`을 반복해서 읽지만, 운영 UI가 바꿀 canonical
   policy revision/epoch과 durable activation receipt가 없다.
2. Agent Card의 required `owner` 문자열만 바꾸면 current ownership generation, revoke와 stale Owner
   fence를 표현할 수 없다.
3. legacy feed/audit는 raw question/answer를 포함할 수 있는 서로 다른 in-memory/JSONL projection이며,
   durable cursor·retention·restart recovery가 없다.

RB3.2b.6은 이를 Central metadata/control plane으로 닫되 Owner Installation credential, pairing,
raw source와 full draft에는 손대지 않는다.

## 결정

아래 domain value/command/view/receipt는 pydantic v2 `ConfigDict(frozen=True, extra="forbid",
strict=True)`를 공통으로 쓰고 sealed sum은 `kind` discriminator와 exhaustive `match`로만 해석한다.
aggregate transition은 frozen next snapshot 교체이며 arbitrary mapping/downcast fallback은 없다.

### 1. OperationalEvent와 AuditRecord는 전이와 다른 safe evidence다

`OperationalEvent`는 다음 exact frozen schema다.

```text
OperationalEvent
  cursor: positive int
  event_id: opaque nonblank string
  org_id: nonblank string
  event_type:
    question_received | request_state_changed | answer_finalized | feedback_recorded
    | conflict_concurrence_recorded | manager_item_changed | approval_changed
    | backup_review_changed | reevaluation_changed | registry_user_registered
    | agent_card_registered | policy_activated | policy_rolled_back
    | card_owner_transferred | card_owner_revoked | command_attempt_recorded
  occurred_at: UTC RFC3339
  actor: SystemActor | UserActor(user_id)
  resource: SafeResourceRef(kind, resource_id)
  outcome: committed | denied | failed
  audit_id: opaque nonblank string
  receipt_id: opaque nonblank string
  policy_epoch: positive int
  policy_digest: lowercase SHA-256
  change: SafeChange
```

`SafeChange`는 event type별 sealed sum이며 다음 scalar/reference만 허용한다:
`from_state`, `to_state`, `reason_code`, `request_id`, `record_id`, `feedback_id`, `case_id`,
`manager_item_id`, `approval_item_id`, `review_id`, `reevaluation_id`, `card_id`,
`from_owner_user_id`, `to_owner_user_id`, `assignment_generation`, `policy_revision_id`,
`policy_epoch`, `policy_digest`. arbitrary mapping은 없다.

`AuditRecord`는 다음 exact frozen schema다.

```text
AuditRecord
  audit_id, org_id, occurred_at
  actor: SystemActor | UserActor(user_id)
  action, resource: SafeResourceRef
  outcome: committed | denied | failed
  command_digest: lowercase SHA-256
  receipt_id
  authority: {policy_revision_id, policy_epoch, policy_digest}
  approval_evidence: {evidence_id, evidence_digest} | null
  change: SafeChange
```

audit collection은 projector가 paired OperationalEvent와 함께 만든 exact
`AuditRecordView {cursor, record: AuditRecord}`만 노출한다. list response는
`{items:[AuditRecordView],oldest_available_cursor,latest_cursor,next_before_cursor|null}`이고 detail은
exact 한 `AuditRecordView`다. cursor는 audit row의 domain identity가 아니라 paired feed projection의
ordering metadata다.

두 schema에는 question/answer/comment/rationale/edited text, raw source/full draft, source URI,
credential/token/hash, Browser Session handle/digest, OIDC claim, PKCE/code, approval evidence body,
Owner workspace ref가 없다. `reason_code`는 중앙 allowlist scalar이고 사람 자유서술이 아니다.

도메인 전이를 성공시킨 source writer는 **같은 SQLite transaction**에 immutable `AuditRecord`와
`OperationalEventOutboxIntent`를 쓴다. audit은 누가 어떤 명령을 내렸는지의 절차 기록이고,
OperationalEvent는 feed용 projection이며 둘 다 전이 자체가 아니다. source aggregate를 event로
재생해 state를 복원하거나 event append 실패를 성공한 전이로 추정하지 않는다.
deny/validation failure에는 Domain Transition이나 성공 receipt가 없다. durable audit dependency가
사용 가능하면 별 audit UoW에 attempt receipt, `denied|failed` AuditRecord와
`command_attempt_recorded` outbox intent를 함께 쓸 수 있지만, 이것은 성공이나 transition 증거로
사용할 수 없다.

intent ID는 `sha256("operational-event" || org_id || receipt_id || audit_id || command_digest)`다.
projector는 pending/expired intent를 lease claim하고, one transaction에서 next per-org cursor를 할당해
OperationalEvent와 delivered marker를 쓴다. `(org_id,cursor)`, `(org_id,event_id)`,
`(org_id,intent_id)`는 unique이고 sequence는 commit 순서로 strictly increasing하며 재사용하지 않는다.
claim/restart replay는 same event write 0이고 gap, duplicate, orphan 또는 changed payload는 unavailable이다.

#### bounded retention과 typed resync

local-reference 기본은 org별 최근 **10,000 events**다. Central profile의
`operational_event_retention_count`만 `1_000..1_000_000` 정수로 시작 시 고정할 수 있다. count-based
floor라 wall clock과 무관하게 결정론적이다. projector commit 뒤 old cursor를 event/audit pair로 prune하고
`OperationalRetentionReceipt(org_id, through_cursor, retained_from_cursor, policy_count, digest)`를 남긴다.
retained row는 update 불가이고 retention transaction만 whole-row delete를 허용한다. legal hold, export,
사용자별 기간, archive storage와 장기 감사 보존은 P1이다.

`GET .../feed` SSE는 decimal `Last-Event-ID`만 받는다. header가 없으면 connection 시점 latest 이후부터
보낸다. cursor가 retention floor보다 작거나 available sequence에 gap이 있으면
`event: resync_required`, data `{code:"resync_required",oldest_available_cursor,latest_cursor}`를 한 번
보내고 닫는다. audit list의 too-old/gap도 HTTP 409 같은 DTO다. client는 canonical audit/org/policy/
scorecard GET을 다시 읽은 뒤 latest cursor로 reconnect한다. volatile connection은 event durability의
근거가 아니다.

### 2. PolicyRevision이 runtime Authority의 유일한 canonical source다

`PolicyRevision`은 immutable SQLite aggregate다.

```text
PolicyRevision
  revision_id
  org_id
  epoch: positive, strictly monotonic
  policy_version
  policy_digest
  parent_revision_id | null
  parent_epoch | null
  parent_digest | null
  canonical_document
  validation_receipt_id
  created_by, created_at

ActivePolicyPointer
  org_id
  revision_id
  epoch
  policy_digest
  activated_at
```

`canonical_document`는 ADR 0050의 exact schema
`schema_version,org_id,policy_version,subject_roles,role_permissions,route_rules,worker_bindings`다.
client는 `content_sha256`을 보내지 않고 Central이 기존 strict validator/canonicalizer로 계산한다.
unknown key/action/role, duplicate, cross-org, invalid binding과 blank 값은 validation 실패다.

`GET /policy`의 `PolicyRevisionView`는
`{revision_id,epoch,policy_version,policy_digest,canonical_document,activated_at}`다. raw credential은
document에 존재할 수 없고 worker binding에는 opaque credential ID/generation만 허용된다.

update body는 exact sealed sum이다. ADR 0051 operational approval reference는 header가 아니라
각 body의 required `approval` object에만 실린다. `evidence_id`는 opaque nonblank string,
`evidence_digest`는 lowercase SHA-256이며 unknown key는 없다.

```json
{"kind":"activate","expected_epoch":7,"expected_digest":"<sha256>","document":{...},
 "approval":{"evidence_id":"...","evidence_digest":"<sha256>"}}
{"kind":"rollback","expected_epoch":7,"expected_digest":"<sha256>",
 "target_revision_id":"...","target_digest":"<sha256>",
 "approval":{"evidence_id":"...","evidence_digest":"<sha256>"}}
{"kind":"import","expected_epoch":7,"expected_digest":"<sha256>","document":{...},
 "approval":{"evidence_id":"...","evidence_digest":"<sha256>"}}
```

POST header는 exact `Content-Type: application/json`, Origin, Fetch Metadata, CSRF cookie/header와
`Idempotency-Key`를 요구한다. approval ID/digest, actor/org/policy fingerprint는 별 header로 받지
않는다. session-derived actor와 current `policy.write` on
`ResourceRef(org,"authority_policy",org)`를 확인하고 다음 두 digest를 canonicalize한다.

- `command_digest = sha256(canonical(actor,action,ResourceRef,kind,expected_epoch,expected_digest,
  document 또는 target revision/digest))`. `approval`, CSRF와 idempotency header는 제외한다.
- `active_pointer_fingerprint = sha256(canonical(org_id,revision_id,epoch,policy_digest))`.

ADR 0051 approval record는 exact same org/actor/action/resource, `command_digest`,
`active_pointer_fingerprint`, body의 `evidence_id/evidence_digest`, unexpired/current/unrevoked/
unconsumed 상태여야 한다. application은 body decode/validation 뒤 write 직전 한 번, 한
`BEGIN IMMEDIATE`에서 pointer CAS와 domain write 직전 precommit에 한 번 더 approval record와 active
pointer를 재조회한다. evidence claim은 command/receipt unique라 다른 command가 재사용할 수 없다.
missing, stale/expired, foreign-org/actor/resource, digest/pointer/command mismatch, revoked/consumed/
already-claimed-by-other-command evidence는 typed conflict/denied이며 write 0이다.

activate와 import는 document validation 뒤 epoch `expected+1`의 immutable revision을 만든다.
`import`는 client가 읽은 document object를 가져오는 명시적 operation일 뿐 server file path/URI를
받거나 runtime YAML을 reload하지 않는다. rollback은 same-org retained historical target의 exact
document/digest를 읽어 **새 epoch `expected+1` revision**으로 복제·활성화한다. 세 operation 모두
pointer, evidence claim, command receipt, safe audit/outbox를 원자적으로 쓴다. 과거 pointer로
되돌리지 않으므로 같은 digest로 돌아와도 epoch가 증가해 ABA가 없다.

success response는 exact
`PolicyRevisionReceiptView {receipt_id,operation,revision_id,epoch,policy_digest,
previous_revision_id,previous_epoch,previous_digest,evidence_id,evidence_digest,replayed}`다.
`operation`은 `activated|rolled_back|imported` sealed literal이고 GET view와 receipt를 합쳐
추론하지 않는다. receipt/audit에는 approval 원문·rationale·actor claim·expiry body를 복사하지 않고
safe `evidence_id/evidence_digest`만 둔다.

same actor/resource/key/payload replay는 current `policy.write` 재인가, stored
revision/pointer/receipt/audit/evidence claim 결박, 현재 approval record의 org/actor/action/resource/
command와 **stored previous active pointer fingerprint** 결박 및 unexpired/unrevoked/unconsumed
validity를 write 직전과 transaction precommit에 재검증한 뒤 prior result write 0이다. replay가
post-command live pointer를 pre-command evidence fingerprint로 오인해 비교하지는 않지만 stored
previous pointer/result pointer/receipt의 정·역방향 결박은 모두 exact해야 한다.
missing/stale/foreign/mismatched/consumed evidence는 replay도 write 0 오류다. missing/foreign/denied
evidence는 body-free 404, expired/stale/revoked/consumed/mismatch/other-command claim은
`409 policy_approval_conflict`, approval store/catalog unavailable은 `503 policy_approval_unavailable`이다.
same key의 다른 command, stale expected pointer와 target mismatch는 conflict다.
validation/evidence/receipt/audit/outbox fault는 pointer 변경 0이다. activation commit 뒤
시작하는 every Authority read는 DB active revision을 읽고, in-flight write는 precommit pointer를 다시
읽어 바뀌었으면 write 0으로 재시도한다. SSE emission도 current DB revision을 재인가한다.
process-local/file snapshot fallback은 없다.

#### YAML bootstrap/import와 split-brain 방지

v20 최초 migration은 public process를 열기 전 exclusive startup에서 configured
`routing_rules.yaml`(현재 profile의 `authority_snapshot_path`)을 기존 strict loader로 한 번 검증한다.
org/profile과 digest가 일치할 때 epoch 1 PolicyRevision, ActivePolicyPointer, bootstrap receipt/audit/
outbox를 한 transaction에 쓰고 **marker를 마지막에** v20으로 바꾼다. 실패하면 marker v19와 file-based
pre-v20 process가 그대로이고 partial DB policy는 없다. 이 epoch 1 system migration은 browser
`activate|import` command가 아니며 외부 approval reference를 합성하지 않는다.

marker v20 이후 file은 initial bootstrap 또는 명시적 policy document import input일 뿐 live canonical
source가 아니다. startup/runtime은 file 변경을 poll/reload하지 않고 DB pointer 손상·unavailable 시
file로 fallback하지 않는다. explicit import도 update command와 같은 validation/CAS/receipt를 거쳐 새
epoch를 만든다. rollback은 DB revision만 바꾸며 file을 rewrite하지 않는다. 이 marker cutover가 한
process 안에서 file/DB split-brain을 막는다.

### 3. Card Owner assignment는 generation이며 revoke는 Agent Card owner를 null로 만들지 않는다

`CardOwnerAssignment`은 durable generation aggregate다. assignment identity, Owner/Card binding과
generation은 생성 뒤 불변이고, `status/revision/closed_at/close_reason_code`만 expected revision CAS로
`active → revoked` 전이할 수 있다.

```text
CardOwnerAssignment
  assignment_id, org_id, card_id
  generation: positive
  revision: positive
  owner_user_id
  card_revision, card_digest
  predecessor_assignment_id | null
  status: active | revoked
  opened_at
  closed_at | null
  close_reason_code | null
```

각 Card에는 active assignment가 최대 하나다. required `AgentCard.owner`는 transfer 때 새 current Owner로
갱신하지만 revoke 때 null/delete하지 않고 마지막 recorded owner를 보존한다. routing, Answer ingest,
Inbox, authoring과 owner-scoped Central action은 current active assignment의 owner/generation/card
revision/digest까지 exact match해야 한다. active assignment가 없으면 card는 routing candidate와
Owner action에서 즉시 제외된다.

transfer body는
`{new_owner_user_id,expected_card_revision,expected_assignment_generation,expected_assignment_revision}`,
revoke body는
`{reason_code,expected_card_revision,expected_assignment_generation,expected_assignment_revision}`다.
`reason_code`는 `[a-z0-9_]{1,64}`이고 free text가 아니다. POST `Idempotency-Key`는 필수다.

transfer는 current `card.transfer_owner`, revoke는 신규 `card.revoke` action을 exact
`ResourceRef(org,"agent_card",card_id)`에 요구한다. ADR 0051의 current operational approval evidence도
command digest/current resource에 결박해 prewrite 재확인한다. 한 UoW가 current AgentCard/admission,
active assignment, Registry User target, expected revisions, Authority/policy pointer를 재검증한다.

- transfer: old active generation을 revoked로 닫고 generation+1 active assignment와 owner가 바뀐
  next AgentCard revision, shared Registry revision, receipt/audit/outbox를 atomic commit한다.
- revoke: active generation을 revoked로 닫고 AgentCard required owner 값은 유지한 채 Card/Registry
  revision을 올리고 receipt/audit/outbox를 atomic commit한다. successor assignment는 없다.

success response는 exact sealed receipt view다.

```text
CardOwnerTransferReceiptView
  receipt_id, card_id, card_revision, registry_revision
  closed_assignment_id, new_assignment_id
  new_generation, new_assignment_revision
  from_owner_user_id, to_owner_user_id
  policy_epoch, policy_digest, replayed

CardOwnerRevokeReceiptView
  receipt_id, card_id, card_revision, registry_revision
  closed_assignment_id, closed_generation, closed_assignment_revision
  recorded_owner_user_id, reason_code
  policy_epoch, policy_digest, replayed
```

same command replay만 stored outcome write 0이다. target missing/cross-org, self-no-op transfer,
inactive/stale assignment, invalid card, deny/evidence drift는 write 0이다. transfer/revoke transaction은
Owner API, Owner Worker, WebSocket, external credential/pairing service를 호출하지 않는다. Central은
active assignment fence로 old Owner와 revoked Card를 즉시 deny한다. Owner Installation credential,
pairing generation의 실제 revoke/re-pair/unpair와 crash reconciliation은 RB3.5가 맡는다.

v21 migration은 every valid current production AgentCard/registration receipt/audit binding을 검증해
required existing owner로 deterministic generation 1 active assignment를 하나 backfill한다. missing,
ambiguous, invalid Owner/Card binding은 migration fail-close이며 marker v20을 유지한다. legacy fixture
transfer/token history는 production assignment로 승격하지 않는다.

이 결정은 ADR 0034의 product-path transfer를 대체한다. legacy in-process token revoke/disconnect는
Central assignment transition의 원자 효과가 아니며 RB3.2b.6에서 호출하지 않는다. 과거 AnswerRecord의
answered_by와 prior assignment generation은 불변이다.

### 4. Organization graph와 Organization Scorecard는 safe read projection이다

`OrganizationGraphProjection`은
`{registry_revision,policy_epoch,policy_digest,nodes,edges,source_digest}`다.

- User node: `{kind:"user",user_id,manager_user_id|null}`.
- Card node: `{kind:"agent_card",card_id,card_revision,team,assignment_status,
  assignment_generation|null,current_owner_user_id|null,recorded_owner_user_id}`.
- Edge: `{kind:"manages"|"owns"|"maintains",source_id,target_id}`. `owns`는 active assignment에만 존재한다.

email/OIDC claim, question/answer, source/knowledge body/URI, credential, session, policy internals는 없다.
same read transaction에서 Registry User/Card/current assignment FK와 active policy pointer를 검증한다.

Organization Scorecard는 Owner self `scorecard.read`와 다른
`scorecard.organization.read` action/ResourceRef `(org,"organization_scorecard",org)`를 쓴다.
`OrganizationScorecardProjection`은
`{window:{since,until},source_digest,owners:[OrganizationOwnerScorecard]}`다. owners는 `owner_user_id`
오름차순일 뿐 rank/percentile/grade가 없다. window 안 historical evidence가 있거나 snapshot에 active
assignment가 있는 Owner만 정확히 한 item을 가지며, 각 item은 ADR 0035의
quality/supervision/availability/freshness four axes와 `weak_identity_note`만 가진다.

quality/supervision/availability의 historical count는 AnswerRecord, FeedbackRecord, correction/review/
WorkTicket receipt에 저장된 **당시 assignment generation**으로 귀속하고 transfer 뒤 current Owner에게
과거 기록을 이동시키지 않는다. freshness/current card count는 read snapshot의 active assignments와
published index acceptance timestamp를 쓴다. 모든 source는 same SQLite read transaction의 exact
catalog/FK/receipt reconciliation을 통과해야 하고 `source_digest`는 source high-water marks와 window의
canonical digest다. restart 뒤 같은 DB snapshot/window는 같은 projection이다. partial source,
process-local Presence만 있는 axis는 값을 추정하지 않고 `null`/typed unavailable로 표현한다.
Organization Scorecard는 관찰 도구이며 Authority, assignment, routing이나 appraisal을 바꾸지 않는다.

### 5. exact nine private API/BFF routes

Central Next는 generic proxy 없이 다음 exact nine routes만 새로 추가한다. 각 BFF는 같은 method/path의
fixed private Central `/v1` route 하나만 호출한다.

| Central Next BFF | Private Central API | action / ResourceRef |
| --- | --- | --- |
| `GET /api/console/feed` | `GET /v1/console/feed` | `monitor.read` / `(org,"operational_feed",org)` |
| `GET /api/console/audit` | `GET /v1/console/audit` | `audit.read` / `(org,"audit_collection",org)` |
| `GET /api/console/audit/{audit_id}` | `GET /v1/console/audit/{audit_id}` | `audit.read` / `(org,"audit_record",audit_id)` |
| `GET /api/console/org` | `GET /v1/console/org` | `org_graph.read` / `(org,"organization_graph",org)` |
| `GET /api/admin/policy` | `GET /v1/admin/policy` | `policy.read` / `(org,"authority_policy",org)` |
| `POST /api/admin/policy/revisions` | `POST /v1/admin/policy/revisions` | `policy.write` / same resource |
| `POST /api/admin/agent-cards/{card_id}/owner-transfers` | `POST /v1/admin/agent-cards/{card_id}/owner-transfers` | `card.transfer_owner` / `(org,"agent_card",card_id)` |
| `POST /api/admin/agent-cards/{card_id}/revocations` | `POST /v1/admin/agent-cards/{card_id}/revocations` | `card.revoke` / `(org,"agent_card",card_id)` |
| `GET /api/admin/scorecard` | `GET /v1/admin/scorecard` | `scorecard.organization.read` / Organization Scorecard |

`policy.read`, `policy.write`, `card.revoke`, `scorecard.organization.read`를 Authority manifest에 추가한다.
`policy.write`와 ownership mutations는 admin hard-limit, organization scorecard는 admin/operator
hard-limit이다. Owner self scorecard는 이 route/action을 사용하지 않는다.

모든 route는 digest-only Browser Session Principal → current Registry User → DB active PolicyRevision의
`session.read`와 route action 순으로 재인가한다. GET은 body 없이 cookie-only다. feed는
`Accept:text/event-stream`과 bounded decimal `Last-Event-ID`만, audit list는 optional
`before_cursor` positive int와 `limit` 1–100(default 50), scorecard는 exact UTC RFC3339 `since`,`until`
(둘 다 있거나 둘 다 없음; default rolling 30 days)만 허용한다. 다른 query와 route-specific protocol
header 값은 거부하되 일반 browser transport header까지 self-claim으로 취급하지 않는다.

POST는 exact Origin, Fetch Metadata, CSRF cookie/header, `Idempotency-Key`, 64KiB strict JSON,
unknown-key/lone-surrogate/raw-forwarded provenance rejection을 쓴다. finite response는 1MiB, SSE는
no-cache/no-transform이다. actor/org/role/session/policy digest self-claim, request-selected upstream,
Owner API, raw evidence relay는 없다.
`POST /api/admin/policy/revisions`는 `Content-Type: application/json`과 §2의 exact
activate/rollback/import body를 쓰며 approval ID/digest는 nested `approval` object에만 둔다. BFF가
approval header를 만들거나 body reference를 header/query로 옮기지 않는다.

unauthenticated/revoked session은 401, authenticated collection action deny는 403, object
missing/foreign/denied는 body-free 404, invalid DTO/policy/card는 422, CAS/idempotency/resync은 409,
dependency/catalog/policy failure는 503이다. BFF는 safe `{code,message}` 또는 typed resync만 반환하고
upstream arbitrary body/header/Set-Cookie를 relay하지 않는다.

Next destinations는 `/console/feed`, `/console/audit`, `/console/org`, `/admin`이다. 기존
`GET|POST /api/admin/agent-cards` admission route 수/의미는 바꾸지 않는다.

### 6. marker migration과 source writer integration

current valid Central marker v18에서만 시작한다.

1. **v18→v19 Operational evidence:** event/audit/outbox/cursor/retention tables와 immutable triggers를
   만들고 existing valid receipt+safe audit pairs를 deterministic intent로 backfill한다. raw legacy
   AuditEntry/JSONL/demo row는 승격하지 않는다. current lifecycle, Inbox, Registry admission writer는
   transition+safe audit+event intent를 같은 UoW로 쓰도록 forward integration한다. tampered/ambiguous
   source는 rollback, marker v18 유지다.
   v19 source writer의 Authority provenance는 org/epoch 합성값이 아니다. strict loader가 읽은 current
   YAML snapshot digest와 source grant의 policy digest가 exact 일치할 때만
   `policy_revision_id="yaml:<digest>", policy_epoch=1, policy_digest=<digest>`로 기록한다. missing/mismatch는
   source transition write 0이며, v20 bootstrap은 같은 YAML digest를 epoch-1 DB revision으로 가져온다.
   이 provenance를 재구성할 receipt timestamp/source receipt가 없는 v18 history는 합성 backfill하지 않고
   marker v18에서 fail-close한다.
2. **v19→v20 Policy:** configured YAML을 strict validate/import하고 epoch 1 revision/pointer/bootstrap
   receipt/audit/outbox를 atomic commit한 뒤 marker를 마지막에 쓴다. product Authority adapters는 marker
   v20부터 DB pointer만 사용한다.
3. **v20→v21 Ownership:** current production Card/Owner binding을 generation 1 assignment로 backfill하고
   transfer/revoke receipt/audit/outbox와 exact indexes를 만든다. marker-last 뒤 routing/owner-scoped
   resolvers는 active assignment를 필수로 한다.
4. E는 schema marker를 추가하지 않는다.

각 migration은 `BEGIN IMMEDIATE`, explicit-column copy, exact owned catalog/FK/reverse binding,
update/delete trigger allowlist, reconciliation 뒤 marker-last다. failure/retry는 prior marker 또는 exact
same rows로 수렴하고 partial marker advance는 없다. startup은 catalog/reconciliation 뒤 event projector와
retention recovery를 drain한다. source writer integration 누락은 readiness unavailable이며 synthetic audit/
event/assignment/policy fallback을 만들지 않는다.

### 7. finite implementation slices와 gates

1. **B — Operational evidence (v19):** safe schemas, historical safe backfill, source-writer UoW,
   cursor/lease/retention/resync, feed/audit private API. deterministic fault/restart/gap tests, Fast+Contract.
2. **C — PolicyRevision (v20):** YAML cutover, DB current Authority provider,
   activate/rollback/import/replay/ABA, command+active-pointer-bound ADR 0051 evidence의
   prewrite/precommit/current-validity와 safe evidence receipt/audit, policy API. deterministic
   transaction/policy/evidence drift tests, Fast+Contract.
3. **D — Ownership/graph/scorecard (v21):** assignment migration, transfer/revoke, immediate central fences,
   graph/organization scorecard projections/API. deterministic concurrency/restart/source tests, Fast+Contract.
4. **E — BFF/UI/review:** exact nine BFF routes, SSE resync, `/console/*` and `/admin` action parity,
   compiled standalone, independent review. Fast+Contract only.

어느 slice도 Full Gate를 실행하거나 RB3.5 credential/pairing invalidation completion을 주장하지 않는다.
advanced retention/export/legal hold, multi-instance cursor allocation과 long-term archive는 P1이다.

## 결과

- Authority의 runtime SSOT는 active SQLite PolicyRevision 하나이고 YAML은 bootstrap/import input으로
  강등된다.
- Policy activate/rollback/import는 current `policy.write`와 command+active-pointer-bound ADR 0051
  evidence가 fresh/replay 모두 current할 때만 진행하고 receipt/audit에는 safe evidence ID/digest만 남긴다.
- revoke는 invalid AgentCard를 만들지 않으며 active assignment 부재로 즉시 Central deny한다.
- transition, audit, event, delivery가 서로 다른 evidence로 남는다.
- console/admin은 safe metadata만 보고 raw body/credential/session handle을 받지 않는다.
