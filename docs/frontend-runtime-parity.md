# Browser Frontend 기능 보존 이관표

이 문서는 ADR 0075의 P0 계약입니다. `web/*.html`은 retire된 runtime이며 fallback으로 다시
열지 않는다. 다만 retire는 기능 제거가 아니다. 아래 아홉 화면의 성공·실패·권한 계약이
Central Next 또는 별 Card Owner-local Next에 모두 구현되기 전에는 RB3를 완료하지 않는다.
현재 표는 목표 destination이며, 현재 지원 상태는 여전히 `product_target_not_available`이다.

| 과거 HTML | Next destination | action-level aggregate/API contract | authority·success·error contract |
|---|---|---|---|
| `web/index.html` | Central Next `/ask` (구현) | exact 네 method/path: dedicated `POST /api/questions` create → `GET /api/questions/{request_id}/stream` typed SSE → canonical `GET /api/questions/{request_id}` retrieve와 `POST /api/questions/{request_id}/feedback` durable evidence. session gate 뒤 strict sealed decoder로 pending/terminal을 보이며, native cursor reconnect/interruption은 같은 id GET으로 수렴한다. `done` payload는 canonical `AnsweredProjection`의 대체가 아니고, feedback은 Answered에만 보인다. | session-derived Question User의 current `session.read` + create/read-own/own-feedback만. POST CSRF/idempotency, feedback path binding/replay recheck와 401/403/404/409/422/503/network interruption을 UI type으로 보인다. greeting은 Authority-checked Received commit 뒤 Router/Case/Manager/Runtime 0으로 Declined하고 0-match는 별 Router disposition transaction의 ManagerItem으로 남는다. generic `/api/ask*`·anonymous feedback은 쓰지 않는다. |
| `web/inbox.html` | Central Next `/inbox` | ADR 0080 exact dedicated BFF/private Central list/detail/concurrence/disposition/reassignment routes. `ConflictCase` concurrence는 one-vote-per-current-candidate-Card-Owner reducer + Case/Request CAS로 `still_open/agreed/deadlocked/route_rejected` Request 전이에만 수렴하고 deadlock ManagerItem은 same-UoW다. `BackupReview`는 terminal backup AnswerRecord writer의 same-transaction outbox producer approve/correct/dismiss, `Reevaluation`은 bad FeedbackRecord writer의 same-transaction outbox producer acknowledge/request_reanswer이다. v17은 v14–v16 eligible immutable sources도 receipt-digest deterministic intent로 backfill한다. `ApprovalItem` list/detail은 same-read current AwaitingApproval `request_revision`을 반환하고 UI가 expected revision으로 그대로 쓰며, approve/edit/reject와 별 reassign(open successor)을 제공한다. Central half는 `ConflictEvidenceGrant` metadata만 반환한다. E2 React UI는 네 접근 가능한 탭, lazy detail, abort/epoch stale 억제, form-preserving canonical reload를 구현했으며 독립 review와 browser 확인 전 `[~]`다. | exact session.read plus eight list/detail action/ResourceRef grants, session-derived candidate Card Owner/approver/Manager와 current write action, CSRF/idempotency/CAS/receipt replay. stale/hidden/deny/unavailable, required corrected/edit text·reject reason·target Card/Owner binding을 구별한다. correction/reanswer는 immutable append-only이며 raw/full evidence body는 Central API/BFF가 반환하지 않는다. |
| `web/builder.html` | Owner Next `/card`, `/workspace` | Card candidate validate/preview와 Central live admission을 구별한다. Owner API에서 local OKF file path/body edit·validate·Git commit(author/commit SHA), `AuthoringRun` create/review/publish/recovery를 수행한다. | paired current Card Owner만. local path/Git/restart 오류, Central receipt replay/CAS, transfer/revoke/unavailable와 exact reviewed-revision publish result를 표시한다. Central Next에는 builder/author route·BFF가 없다. |
| `web/admin.html` | Central Next `/onboarding`, `/admin` | 기존 User/Card list+admission에 exact `GET /api/admin/policy`, `POST /api/admin/policy/revisions`, `POST /api/admin/agent-cards/{card_id}/owner-transfers`, `POST /api/admin/agent-cards/{card_id}/revocations`, `GET /api/admin/scorecard`를 더한다. PolicyRevision activate/rollback/import는 expected epoch+digest CAS와 새 epoch receipt를 쓰며 policy POST body의 exact `approval:{evidence_id,evidence_digest}`를 canonical command digest+current active pointer fingerprint에 결박한다. transfer/revoke는 expected Card/assignment revisions와 generation receipt, organization scorecard는 당시 assignment generation에 귀속한 four axes만 표시한다. | session-derived `policy.read`, `policy.write`, `card.transfer_owner`, `card.revoke`, `scorecard.organization.read`의 exact resource grant. POST는 Content-Type/CSRF/idempotency/operational approval evidence를 요구하되 policy approval은 header가 아니라 strict JSON body에만 둔다. prewrite/precommit과 replay에서 current `policy.write`·approval validity를 재검증하며 missing/stale/foreign/mismatched/consumed evidence는 write 0이다. 401/403/hidden 404/409/422/503을 safe DTO로 표시한다. revoke는 required Agent Card owner를 null로 만들지 않으며 credential/pairing invalidation 완료는 RB3.5다. |
| `web/console-feed.html` | Central Next `/console/feed` | exact `GET /api/console/feed` redacted typed `OperationalEvent` SSE. 조직별 strictly monotonic decimal cursor, bounded `Last-Event-ID`, 기본 최근 10,000건 count retention과 explicit `resync_required` 뒤 canonical read/reconnect를 제공한다. | current `session.read+monitor.read`; 401/403/409/503와 malformed/interrupted SSE를 안전하게 표시한다. event에는 question/answer/comment/rationale/raw source/full draft/URI/credential/session/claim이 없다. |
| `web/monitor.html` | Central Next `/console/audit` | exact `GET /api/console/audit` summary와 `GET /api/console/audit/{audit_id}` detail. immutable safe `AuditRecord`를 cursor page로 읽고 too-old/gap은 typed resync 후 canonical reload한다. | current `session.read+audit.read`; raw source/full draft/provider·OOB secret/session/claim은 어떤 field에도 없고 401/403/hidden 404/409/503을 명시한다. Audit Record와 Operational Event는 Domain Transition 자체가 아니다. |
| `web/org.html` | Central Next `/console/org` | exact `GET /api/console/org`은 current Registry/assignment graph의 safe User/Card node와 active `owns`, `manages`, `maintains` edge만 읽는다. Authority policy read/edit는 `/admin`의 별 exact command이며 이 화면은 policy document를 받지 않는다. | current `session.read+org_graph.read`; Registry revision과 active policy epoch/digest/source digest를 same snapshot에서 확인한다. invalid graph, denial/hidden/unavailable를 구별하고 graph read를 policy edit 권한의 암묵 승인으로 해석하지 않는다. |
| `web/owner-monitor.html` | Owner Next `/supervision` | Owner API → versioned Central owner-scoped control API의 self `AnswerRecord` list (`needs_review` filter), presence, immutable correction+rationale/history, feedback view, own scorecard 기간 조회. | paired current Card Owner와 Central every-read/every-write reauthorization. 자기 Card 외 answer/presence/scorecard는 403이며 body의 owner/role 자기보고는 0이다. transfer/revoke/unavailable이면 correction submit 0이다. Central 전체 scorecard는 별 `/admin`에 남는다. |
| `web/owner-drafts.html` | Owner Next `/drafts` | Owner-local durable `WorkTicket` draft list/detail, approve submit(`edited_text=null`), explicit edit submit, unsaved edit 보호와 restart recovery. | paired current Card Owner. draft body는 Owner workspace에만 있으며 local/Central failure를 구별한다. revoke/unavailable이면 Central submit 0이다. |

## 근거 열람의 trust boundary

과거 `inbox.html`은 Central dispatcher가 다른 후보의 문서 본문을 transient relay했다. 이는
Central Browser/API가 raw source·full draft를 중계하지 않는 ADR 0067/0073 불변식과 양립할 수
없다. 기능(근거 열람)은 다음으로 보존한다.

- Central `/inbox`는 published Knowledge Index metadata와 case/candidate/revision/TTL/single-use
  `ConflictEvidenceGrant`만 반환한다.
- source Card Owner는 Owner Next `/workspace/conflicts/.../evidence/...`에서 자기 local raw/full
  evidence를 연다. Central에 본문을 upload, proxy, audit, outbox 기록하지 않는다.
- 다른 Card Owner가 raw evidence를 볼 필요가 있으면 source Card Owner의 명시 release가
  recipient/case/concept/digest에 결박된 새 receipt로 필요하다. Central은 release control
  metadata만 보관한다.

## 공통 runtime 원칙

- Central Next와 Owner-local Next는 별 standalone Node process/환경/BFF allowlist다.
- Central Next BFF는 Central API 고정 private origin만, Owner-local Next BFF는 loopback Owner
  API만 호출한다. caller가 upstream host/path를 정하지 못한다.
- RB3.2b.2 browser auth는 Central Next 전용 handler의 exact four route만 쓴다: `POST
  /api/auth/login/start`, `GET /api/auth/callback`, `GET /api/auth/session`, `POST /api/auth/logout`.
  각각 fixed Central API `/v1/browser-auth/login/start|callback|session|logout`에만 대응하며 generic
  BFF fallback·issuer/token endpoint direct call은 없다.
- Central API가 authorization-code+PKCE, OIDC verify, existing Registry User resolution, Authority,
  transaction/session을 소유한다. Next는 redirect·cookie relay만 하며 demo/passwordless/localStorage
  identity나 body/header/query User/org/role/token self-claim을 사용하지 않는다.
- auth cookie는 `__Host-` Secure host-only cookie이고 session/transaction durable record에는 digest만
  저장한다. session-mutating request는 exact public Origin, Fetch Metadata와 session CSRF를
  fail-close한다. `GET /api/auth/session`은 every-request current Authority를 확인하고 token/claim/
  raw handle을 투영하지 않는다.
- `POST /api/auth/logout`은 revoke/unavailable에도 자기 local session cookie만 없애는 monotonic
  cleanup이다. remote global revoke, IdP logout과 다른 device session은 이 행렬의 범위 밖이다.
- `/ask` BFF는 `/api/[...path]` generic proxy가 아니라 ADR 0079의 exact 네 method/path 전용
  handler다. POST는 auth/admission과 같은 Origin·Fetch Metadata·CSRF·idempotency/provenance 규칙을,
  stream은 bounded `Last-Event-ID`, every-emission/reconnect session·Authority recheck와 no-transform
  passthrough를 사용한다.
- FastAPI Developer API와 `web/*.html`은 Central/Owner product browser runtime이 아니다.
- 화면별 API는 success뿐 아니라 401/403/404/409/422/503, network interruption과 terminal
  Question Request 상태를 타입으로 투영한다.
- 이 표의 각 action은 route/API, success shape, error shape, Authority actor와 결정론 Contract
  test 이름을 `RB3.7` 전까지 대응시킨다. 행만 존재하거나 read-only mock UI가 있는 것은 parity
  완료가 아니다.
