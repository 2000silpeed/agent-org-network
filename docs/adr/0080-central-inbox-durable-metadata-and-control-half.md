# ADR 0080 — Central `/inbox` durable metadata/control half

- 상태: accepted (2026-07-31)
- 적용: RB3.2b.5-A 계약 및 B–E 구현 슬라이스. 구현 또는 Full Gate 완료를 뜻하지 않는다.
- 계보: ADR 0008(ConflictCase), ADR 0012(BackupReview), ADR 0019(Reevaluation), ADR 0042(Question Request), ADR 0048(Approval 재지정), ADR 0079(Central browser lifecycle), ADR 0075(legacy 기능 보존 Next 이관).

## 맥락

삭제된 `web/inbox.html`은 담당 합의, 백업 검토, 재평가, Approval의 실제 사용자 행위를 한 화면에 두었지만, demo identity와 in-memory store를 쓰고 candidate source body를 Central dispatcher가 transient relay했다. 이 경로를 Central Next에 그대로 이식하면 세션·Authority·내구성·Owner-local privacy 경계를 무너뜨린다. 반대로 탭을 read-only로 줄이면 legacy 행위를 잃는다.

RB3.2b.5는 네 탭의 **목록·상세·처분**을 Central durable model의 실제 command로 보존한다. 다만 `ConflictEvidenceGrant`는 control metadata일 뿐이고 raw source/full draft/evidence open·release는 paired Owner workspace가 필요한 RB3.5이다.

## 결정

### 1. 공통 browser/API 경계

Central Next는 generic BFF를 쓰지 않고 아래 exact same-origin route만 둔다. 각 route는 같은 method/path의 fixed private Central API `/v1` route 하나만 호출한다.

| Browser BFF | Private Central API | 용도 |
| --- | --- | --- |
| `GET /api/inbox/conflicts` | `GET /v1/inbox/conflicts` | 내가 참여자인 open ConflictCase 목록 |
| `GET /api/inbox/conflicts/{case_id}` | `GET /v1/inbox/conflicts/{case_id}` | Case detail + grant metadata |
| `POST /api/inbox/conflicts/{case_id}/concurrences` | `POST /v1/inbox/conflicts/{case_id}/concurrences` | 합의 stance 기록 |
| `GET /api/inbox/backup-reviews` | `GET /v1/inbox/backup-reviews` | 내 open BackupReview 목록 |
| `GET /api/inbox/backup-reviews/{review_id}` | `GET /v1/inbox/backup-reviews/{review_id}` | BackupReview detail |
| `POST /api/inbox/backup-reviews/{review_id}/dispositions` | `POST /v1/inbox/backup-reviews/{review_id}/dispositions` | approve/correct/dismiss |
| `GET /api/inbox/reevaluations` | `GET /v1/inbox/reevaluations` | 내 open Reevaluation 목록 |
| `GET /api/inbox/reevaluations/{reevaluation_id}` | `GET /v1/inbox/reevaluations/{reevaluation_id}` | Reevaluation detail |
| `POST /api/inbox/reevaluations/{reevaluation_id}/dispositions` | `POST /v1/inbox/reevaluations/{reevaluation_id}/dispositions` | reevaluation disposition |
| `GET /api/inbox/approvals` | `GET /v1/inbox/approvals` | 현재 지정 ApprovalItem 목록 |
| `GET /api/inbox/approvals/{approval_item_id}` | `GET /v1/inbox/approvals/{approval_item_id}` | lazy ApprovalItem detail |
| `POST /api/inbox/approvals/{approval_item_id}/dispositions` | `POST /v1/inbox/approvals/{approval_item_id}/dispositions` | approve/edit/reject |
| `POST /api/inbox/approvals/{approval_item_id}/reassignments` | `POST /v1/inbox/approvals/{approval_item_id}/reassignments` | 별 재지정 command |

모든 GET은 query/body 없이 session cookie만 사용한다. 모든 POST는 RB3.2b.4와 같은 exact Origin, Fetch Metadata, session CSRF cookie/header, `Idempotency-Key`, standalone raw provenance guard를 요구한다. session-derived principal, same-org binding, current `session.read`, action별 current Authority를 Central에서 목록·상세·write 전과 transaction precommit에 재검증한다. body/header/query의 actor, Owner, Card, org, role, session 자기보고와 caller-selected upstream은 없다.

GET의 Authority proof는 항상 먼저 `session.read` on `ResourceRef(org_id, "browser_session",
identity_session_id)`다. 이어지는 eight read의 exact action/ResourceRef와 visibility는 아래와 같다.

| Read | action | list scope ResourceRef | detail ResourceRef 및 visibility |
| --- | --- | --- | --- |
| Conflict list | `conflict.list` | `(org, "conflict_inbox", principal.subject_id)` | —; Case snapshot의 active candidate Card Owner인 행만 반환 |
| Conflict detail | `conflict.list` | — | `(org, "conflict_case", case_id)`; same active candidate Card Owner만, otherwise hidden |
| BackupReview list | `backup_review.list` | `(org, "backup_review_inbox", principal.subject_id)` | —; source snapshot Card Owner의 open 행만 반환 |
| BackupReview detail | `backup_review.read` | — | `(org, "backup_review", review_id)`; same source snapshot Card Owner만, otherwise hidden |
| Reevaluation list | `reevaluation.list` | `(org, "reevaluation_inbox", principal.subject_id)` | —; source answer snapshot Card Owner의 open 행만 반환 |
| Reevaluation detail | `reevaluation.read` | — | `(org, "reevaluation", reevaluation_id)`; same source snapshot Card Owner만, otherwise hidden |
| Approval list | `approval.list` | `(org, "approval_inbox", principal.subject_id)` | —; current designated approver의 open 행만 반환 |
| Approval detail | `approval.read` | — | `(org, "approval_item", approval_item_id)`; same current designated approver만, otherwise hidden |

`backup_review.list|read|decide`와 `reevaluation.list|read|decide`는 RB3.2b.5-Before-B에서
`routing_rules.yaml`/Authority action manifest에 새로 선언해야 하는 Central actions다. 기존 action을
임의 재사용하지 않는다. write action은 exact `conflict.concur`, `backup_review.decide`,
`reevaluation.decide`, `approval.decide`, `approval.reassign`이고 각각
`ResourceRef(org,"conflict_case",case_id)`, `ResourceRef(org,"backup_review",review_id)`,
`ResourceRef(org,"reevaluation",reevaluation_id)`, `ResourceRef(org,"approval_item",approval_item_id)`를
쓴다.
list scope는 조직 전체 browse 권한이 아니라 principal-bound inbox scope이며, detail의 ResourceRef grant만으로
membership check를 우회할 수 없다.

각 finite DTO는 exact JSON object, unknown key 거부, UTF-8/lone-surrogate 거부, request 64KiB, finite response 1MiB를 적용한다. `rationale`, `reason_code`, `edited_text`, `corrected_text`는 원 UTF-8 bytes를 digest에 쓰고 trim/정규화하지 않는다. 단, required 여부 판정은 empty string만 거부한다. list는 본문을 싣지 않고 detail만 해당 session principal에게 필요한 Central-held question, candidate 또는 answer text를 낸다. raw source/full draft/Owner credential은 어느 DTO에도 없다.

hidden foreign, no-longer-current, superseded, denied resource는 같은 body-free `404 not_found_or_denied`로 숨긴다. unauthenticated/expired/revoked session은 `401 session_unavailable`, Authority/service dependency는 `503 unavailable`, syntactically invalid body는 `422 invalid_input`, same resource의 다른 live writer 또는 expected revision 불일치는 `409 stale_or_conflict`다. BFF는 safe `{code, message}`만 재투영하고 upstream body/header/Set-Cookie를 relay하지 않는다.

각 success disposition DTO는 `{receipt_id, aggregate_id, aggregate_revision, request_id?, request_revision?, state, replayed}`이며 immutable receipt/audit의 ID와 committed projection만 가리킨다. identical `Idempotency-Key` + canonical payload digest + same actor/resource는 current reauthorization 뒤 write 0의 `replayed=true` 결과로 수렴한다. 키 재사용의 다른 payload/resource/actor 또는 receipt/aggregate cross-binding 손상은 `409 stale_or_conflict`; deny/unavailable/tamper는 write 0이다.

Read DTO는 다음 exact key만 쓴다. list response는 `{items:[...]}`이고 pagination/filter/query는 없다. ID는 opaque non-empty string, revision/round는 positive integer, timestamp는 UTC RFC3339 string이다.

- `ConflictCaseSummary`: `{case_id, request_id, request_revision, state, round, revision, candidate_card_ids, opened_at}`. `ConflictCaseDetail`은 summary에 `{expected_case_revision,expected_request_revision,expected_round,question,candidates:[{card_id,card_revision,card_digest,owner_user_id,concept_ref,coverage_digest}],own_concurrence:null|{on_candidate_card_id,stance,rationale,round},evidence_grants:[{grant_id,candidate_card_id,candidate_card_revision,concept_ref,expires_at,single_use,status}]}`만 더한다. three `expected_*` values are exactly the committed values used by the next concurrence command.
- `BackupReviewSummary`: `{review_id, request_id, source_answer_record_id, revision, state, created_at}`. detail은 summary에 `{question, backup_answer_text, answering_card_id, answering_card_revision, owner_user_id, answered_at}`만 더한다.
- `ReevaluationSummary`: `{reevaluation_id, request_id, feedback_id, source_answer_record_id, revision, state, created_at}`. detail은 summary에 `{question, answer_text, feedback_verdict, feedback_comment, answering_card_id, answering_card_revision, owner_user_id, flagged_at}`만 더한다.
- `ApprovalItemSummary`: `{approval_item_id, request_id, request_revision, approval_round, revision, assigned_at, due_at, state}`. `request_revision`은 Item과 같은 read snapshot에서 exact-read한 current `AwaitingApproval` Question Request revision이다. detail은 summary에 `{question, candidate_text, candidate_digest, policy_digest, binding_version, assigned_approver_user_id, assigned_approval_card_id}`만 더하며 같은 `request_revision`을 그대로 보존한다.

`question`, `backup_answer_text`, `answer_text`, `candidate_text`, `feedback_comment`, `rationale`, `reason_code`, `edited_text`, `corrected_text` 외에는 request/response에 arbitrary body field가 없다. raw evidence, full draft, credential, actor/org/role/session self-claim, source URI/location과 internal Authority proof는 all DTO에서 금지다.

Action response는 Conflict에 `{receipt_id, concurrence_command_digest, case_id, case_revision, request_id, request_revision, state, outcome:"still_open"|"agreed"|"deadlocked"|"route_rejected", replayed}`, BackupReview/Reevaluation에 `{receipt_id, review_id|reevaluation_id, revision, state:"reviewed", replayed}`, Approval disposition에 `{receipt_id, approval_item_id, approval_item_revision, request_id, request_revision, state:"approved"|"rejected", replayed}`, Approval reassign에 `{receipt_id, superseded_approval_item_id, successor_approval_item_id, successor_approval_item_revision, request_id, request_revision, state:"open", replayed}`를 exact 반환한다. correction/reanswer record ID는 BackupReview/Reevaluation response에 각각 `correction_record_id|null`, `reanswer_requested_id|null`로만 추가한다.

### 2. ConflictCase: snapshot, concurrence, Request 전이

`ConflictCase`는 request-unique durable aggregate다. `open | resolved | escalated` sealed state, monotonic `revision`, monotonic `round`, immutable `candidate_snapshot`(Card ID, Card revision/digest, Card Owner ID, matched concept/coverage digest), 그리고 Case를 연 current Question Request revision과 Authority/Card binding digest를 가진다. `resolved`에는 `agreed | deadlocked | route_rejected` outcome과 receipt/audit reference를, `escalated`에는 ADR 0065 receipt graph reference를 가진다. candidate snapshot은 후속 Card 변경으로 바뀌지 않으며 current binding은 command마다 별도 재검증한다.

`ConflictCaseDetail`은 case/request/round/revision/state, candidate snapshot, 현재 참여자의 own `Concurrence`와 metadata-only `ConflictEvidenceGrant[]`를 돌려준다. grant는 `grant_id, case_id, candidate_card_id, candidate_card_revision, concept_ref, expires_at, single_use, status`만 포함한다. raw location, source bytes, full draft, body digest를 반환하지 않는다.

`POST .../concurrences` DTO는 exact `{on_candidate_card_id, stance:"keep_as_complement"|"withdraw", rationale, expected_case_revision, expected_request_revision, expected_round}`다. `Idempotency-Key` header는 필수이며 caller가 digest를 보내지 않는다. Central은 `case_id`, session-derived actor, idempotency key와 DTO exact UTF-8 bytes의 canonical `concurrence_command_digest`를 계산해 receipt/audit과 response에 저장한다. 같은 actor/round의 same digest replay만 receipt read-back으로 성공한다. 다른 candidate/stance/rationale 또는 expected request revision은 새 key가 아니라 `409 stale_or_conflict`이고 last-write-wins가 아니다.

한 `BEGIN IMMEDIATE` UoW는 open Case, exact Request=`AwaitingConflict`, candidate snapshot, same-org participant, current Authority/Card binding, expected Case/**Request** revision을 재확인하고 Concurrence, Case revision, receipt/audit과 resulting Request/ManagerItem을 함께 쓴다. `still_open`은 same-state Request를 rewrite하지 않되 locked UoW 안에서 expected Request revision equality를 CAS assertion으로 확인한다. terminal branch는 `UPDATE question_requests … WHERE request_id=? AND revision=? AND state_kind="awaiting_conflict"` CAS가 winner일 때만 Case/receipt/ManagerItem을 commit한다. No receipt exists without that same-UoW Request CAS. 결과는 다음뿐이다.

- `still_open`: Case remains open, Request remains `AwaitingConflict`.
- `agreed`: request-scoped Authority Grant와 selected current RouteTarget을 만들고 Case=`resolved`; Request는 exact `AwaitingConflict → ReadyToDispatch` CAS이다. WorkTicket은 이 command가 만들지 않으며 기존 ReadyToDispatch recovery가 후속으로 만든다.
- `deadlocked`: Case=`resolved` 및 same-org ManagerItem을 원자적으로 만들고 Request는 `AwaitingConflict → AwaitingManager(public_kind="contested")` CAS이다.
- `route_rejected`: Case=`resolved`; Request는 `AwaitingConflict → Declined(reason_code="route_rejected")` CAS이다.

#### sealed concurrence reducer

Case의 active participant set `P`는 frozen candidate snapshot의 **서로 다른 Card Owner ID**들의 정렬 집합이다.
각 participant는 current round에 정확히 한 vote만 남긴다. 같은 participant가 소유한 여러 candidate Card는
한 vote의 primary 선택 대상일 뿐 participant를 늘리지 않는다. vote의 stance는 선택 primary가 아닌 그
participant 소유 candidate들을 `keep_as_complement`(보조 RouteTarget으로 유지) 또는 `withdraw`(이번
request-scoped RouteTarget에서 제외)할지 정한다. selected primary는 stance와 무관하게 유지된다.

reducer는 write 직후 `(P, votes_for_round)`만 읽어 다음 순서로 한 번 계산한다.

1. `votes.keys() != P`이면 `still_open`: Case/Request는 open/`AwaitingConflict`이고 round는 그대로다.
2. `votes.keys() == P`이며 모든 `on_candidate_card_id`가 같은 frozen candidate `C`이면 selected primary는
   `C`; 각 voter의 stance로 complement set을 계산한다. current `conflict.concur` proof와 existing
   Route Authority가 `C` 및 resulting complement set의 current binding을 accept하면 `agreed`; Route
   Authority가 validly rejects the target이면 `route_rejected`; proof infrastructure/binding failure는
   terminal outcome을 만들지 않고 `503 unavailable` write 0이다.
3. `votes.keys() == P`이며 선택 candidate가 둘 이상이면 `deadlocked`다.

따라서 complete round에서 `agreed|route_rejected|deadlocked`는 동시에 일어날 수 없고, partial round는
오직 `still_open`이다. terminal outcome 뒤 next round는 없다; stale/withdraw 변경을 위한 round rollover는
RB3.2b.5 범위 밖이다. 이 reducer가 ADR 0008의 `ConcurOnPrimary` 단일축·전원일치 원칙을 durable
request-unique Case와 stance/complement semantics로 **대체**한다.

`deadlocked` branch는 same UoW에서 active candidate Owner들의 current same-org Registry graph를 읽는다.
Manager 후보는 각 Owner의 `manages` ancestor intersection 중 Manager role인 Registry User이고, shortest
path의 max distance가 최소인 유일한 User다; same max distance tie는 sum distance가 최소여야 하며 여전히
tie/graph corruption이면 fail-close write 0이다. candidate가 없으면 same-org canonical root User 하나를
Manager로 쓴다; root가 없거나 여러 명이면 fail-close한다. chosen Manager의 current `manager.act` grant,
Router Contested snapshot digest=Case candidate snapshot digest, Case↔Request binding을 prewrite에 다시
검증한 뒤 ManagerItem(open, `FromDeadlock`, exact case ID)·Case resolved receipt/audit·Request
`AwaitingConflict → AwaitingManager(public_kind="contested", item_id)`를 원자적으로 쓴다. FK/reverse
binding은 case/request/item/receipt 네 방향을 보장하므로 orphan ManagerItem 또는 AwaitingManager가 없다.

직접 Card Owner/Owner API/Agent Runtime 호출은 0이다. `escalated` Case는 ADR 0065의 terminal invariant를 따르며 concurrence writer가 재개하거나 바꾸지 않는다.

### 3. Approval inbox는 기존 ApprovalItem의 projection이다

Approval list는 current designated approver만 보는 `ApprovalItemSummary`(`approval_item_id`, request ID,
same-snapshot current AwaitingApproval `request_revision`, round, Item revision, assigned/due timestamps, state)이고
question/candidate 본문은 없다. list와 detail은 current index의 open ApprovalItem과 exact same-org/request
`AwaitingApproval(item_id=approval_item_id)` reverse binding을 한 read transaction에서 검증한다. detail은
그 binding, current designated approver, same-org `approval.decide` grant가 모두 있을 때만 candidate text와
frozen policy/binding/revision metadata를 반환한다. Request가 missing/non-AwaitingApproval, Item ID/revision이
다르거나 current index가 다른 Item을 가리키면 hidden/unavailable로 닫으며 revision을 predecessor,
candidate 또는 별 GET에서 추론하지 않는다.

`POST .../dispositions` DTO는 sealed one-of:

```json
{"kind":"approve","expected_approval_item_revision":N,"expected_request_revision":M}
{"kind":"approve_with_edit","edited_text":"…","expected_approval_item_revision":N,"expected_request_revision":M}
{"kind":"reject","reason_code":"…","expected_approval_item_revision":N,"expected_request_revision":M}
```

edit text와 reject reason code는 required non-empty string이고 other command keys는 거부한다. 이 route는 `ApprovalDispositionApplication` 하나만 호출한다. approve/edit는 resolved ApprovalItem receipt/audit, AnswerRecord, `AnsweredRequest`를, reject는 resolved ApprovalItem receipt/audit과 `Declined(reason_code="approval_rejected")`를 기존 B2-B2 UoW로만 만든다. WorkTicket은 earlier ingest에서 이미 terminal이므로 write 0이다.

Central Next `/inbox` action UI는 selected summary/detail의 `revision`을
`expected_approval_item_revision`, same object의 `request_revision`을 `expected_request_revision`으로 그대로
보낸다. 다른 row/detail에서 revision을 합성하거나 action 직전 별 Question GET으로 보정하지 않는다.
stale snapshot은 writer의 `409 stale_or_conflict` 뒤 queue/detail 재조회로만 수렴한다.

재지정은 disposition이 아닌 별 command다. `POST .../reassignments` DTO는 exact `{target_approver_user_id, target_approval_card_id, expected_approval_item_revision, expected_request_revision}`다. writer는 actor의 current `approval.reassign` Authority와 `ApprovalReassignmentAuthorizer`의 sealed authorization을 확인하고 target Registry User가 same org의 current target Approval Card binding 및 policy상 eligible approver인지 재확인한다. 성공은 old open ApprovalItem=`superseded`, immutable successor ApprovalItem=`open` (new item ID, round+1, predecessor link), reassignment receipt/audit를 만들고 Request는 전용 `AwaitingApproval → AwaitingApproval` revision+1 CAS를 한다. successor가 열려 있으므로 approval은 **계속 open**이고 AnswerRecord/terminal Request/WorkTicket은 바뀌지 않는다. target ID만으로 current Card/Owner binding을 추측하거나 재지정을 self-claim으로 허용하지 않는다.

### 4. BackupReview: backup terminal AnswerRecord의 durable 사후 검토

`BackupReview`는 `open | reviewed` sealed aggregate이며 `review_id`, `revision`, immutable `source_answer_record_id`, request/ticket ID, answering Card/Owner snapshot, backup mode proof digest, created-at과 disposition/receipt/audit reference를 가진다. producer는 **terminal AnswerRecord whose canonical mode is `backup`** 하나당 정확히 하나다. demo seed, UI fetch, legacy dispatcher, policy guess는 producer가 아니다.

source writer is part of the contract: no-approval `OwnerAnswerIngest` and approval approve/edit finalization이
`mode=backup` terminal AnswerRecord를 처음 만들 때는 같은 transaction에 deterministic
`backup_review_outbox_intent`를 반드시 append한다. `outbox_intent_id`는
`sha256("backup_review" || org_id || source_answer_record_id || source_receipt_digest)`이고, row에는 unique
source ID, request ID, producer receipt ID/digest, frozen terminal Request revision, source answer digest,
source answered timestamp, `pending`을 함께 저장한다. correction은 owner-authorized `mode=full` superseding correction record만
만드므로 새 BackupReview intent를 만들지 않는다. 이러한 기존 terminal writer 통합은 RB3.2b.5-D의
forward migration work이며, AnswerRecord가 존재하는 replay도 exact matching intent 존재를 re-read해야 한다.
source AnswerRecord가 있고 matching intent가 없거나 different digest이면 replay는 repair create가 아니라
integrity `503 unavailable`, write 0이다.

projector는 pending intent를 `BEGIN IMMEDIATE`로 `leased(worker_id, lease_until, attempts)`로 claim한다.
lease 만료 전에는 다른 projector call 0이고, 만료 뒤 retry 가능하다. claim owner는 source AnswerRecord/
Request/Card Owner snapshot과 immutable source receipt를 exact re-read한 뒤 one transaction에서 open
BackupReview, aggregate↔producer receipt, delivered outbox marker를 write한다. `source_answer_record_id`
unique constraint와 receipt read-back은 duplicate claim/restart를 same aggregate/marker write 0으로 수렴시킨다.
mismatch/orphan/tamper는 unavailable로 fail-close한다. startup은 catalog reconciliation 뒤 expired/pending
intent만 재시도하므로 async crash가 duplicate open review나 AnswerRecord 재작성으로 이어지지 않는다.
bounded startup recovery는 최대 `N`개를 project한 뒤 동일 database/current clock의 read-only snapshot으로
claimable pending 또는 expired lease 존재 여부만 확인한다. backlog가 없으면 실제 project 수를 반환하고,
하나라도 남으면 unavailable로 닫되 `N+1` intent를 claim하거나 status/attempts/worker/lease를 변경하지
않는다. 따라서 다음 startup/retry는 untouched pending intent를 attempt 1부터 정상 drain한다.

detail은 source의 Central-held question/backup answer, record metadata와 `expected_revision`만 같은 snapshot Card Owner에게 준다. `POST .../dispositions` DTO는 exact `{kind:"approve"|"dismiss", rationale, expected_revision}` 또는 `{kind:"correct", corrected_text, rationale, expected_revision}`다. `corrected_text`는 correct에서만 required non-empty다. current owner binding + `backup_review.decide` Authority + CAS가 성립하면:

- `approve`와 `dismiss`는 original AnswerRecord/Question Request projection을 바꾸지 않고 review만 closed한다.
- `correct`는 original AnswerRecord를 update하지 않는다. immutable `AnswerCorrectionRecord` (new record ID, `supersedes_record_id`, corrected-text digest/body, actor/binding, receipt/audit)를 append하고 canonical answered projection은 superseding record를 읽는다.

모든 arm은 reviewed aggregate/receipt/audit을 같은 UoW로 append하고 동일 command replay만 write 0이다. Precedent/Authority/routing score는 바꾸지 않는다.

### 5. Reevaluation: bad FeedbackRecord의 append-only 후속 검토

RB3.2b.5의 `Reevaluation` producer는 explicit demo/manual seed가 아니라 immutable `FeedbackRecord(verdict="bad")`다. B3 feedback writer는 feedback record/receipt/audit과 **같은 transaction**에 deterministic `reevaluation_outbox_intent`를 append한다. `outbox_intent_id`는 `sha256("reevaluation" || org_id || feedback_id || source_receipt_digest)`이고 row에는 unique feedback ID, request/source AnswerRecord ID, producer receipt ID/digest, frozen source AnswerRecord terminal Request revision, feedback payload digest, feedback submitted timestamp, `pending`을 저장한다. good feedback은 intent 0이다. feedback replay는 exact matching intent 존재를 re-read하며 missing/different intent는 integrity `503 unavailable`, write 0이다. aggregate는 `reevaluation_id`, `revision`, immutable feedback/answer record/request IDs, feedback payload digest, answer Card/Owner snapshot, `open | reviewed` state, disposition/receipt/audit reference를 가진다. FeedbackRecord는 그대로이고 Request, AnswerRecord, Card, Authority, routing score를 producer가 바꾸지 않는다(B3 불변식).

detail은 same snapshot Card Owner에게 Central-held question, answer, feedback verdict/comment과 metadata를 주며 raw Owner-local evidence를 싣지 않는다. disposition DTO는 exact `{kind:"acknowledge"|"request_reanswer", rationale, expected_revision}`이다. `acknowledge`는 reviewed aggregate와 receipt/audit만 append한다. `request_reanswer`도 원 AnswerRecord/FeedbackRecord/QuestionRequest를 rewrite하지 않고 immutable `ReanswerRequested` follow-up record를 append한다; actual new Question Request, dispatch, runtime call은 이 command가 만들지 않는 후속 authorized recovery concern이다. 둘 다 `reevaluation.decide`, current snapshot Card Owner binding, expected revision CAS를 요구한다.

Reevaluation projector/recovery는 BackupReview와 같은 claim/lease/expiry retry를 쓴다. claimed intent는 one
transaction에서 feedback ID unique Reevaluation, aggregate↔producer receipt, delivered marker를 쓰며 replay는
write 0이다. startup은 catalog reconciliation 뒤 pending/expired intent만 재개한다. bad feedback 하나가
score/Authority/route를 자동 갱신하는 경로는 없다.

### 6. migration, recovery, and markers

RB3.2b.5 starts only from valid Central v14 lifecycle catalog. B adds v15 (`conflict_case` durable detail, concurrences, receipts/audits), C v16 (inbox projection indexes plus Approval reassignment BFF-facing receipt readback), D1 v17 (BackupReview/Reevaluation aggregates and source writer outbox intent/delivery marker/lease), D2 v18 (review heads, disposition receipt/audit, immutable correction/reanswer companion records), E adds no catalog shape. D1 v17은 독립 승인된 exact catalog이므로 D2가 이를 같은 marker 아래에서 silent mutation하지 않는다. v17→v18은 current v17 catalog와 reverse link를 먼저 검증하고 companion table/head를 채운 뒤 marker를 마지막에 18로 교체한다. Each migration is forward-only, `BEGIN IMMEDIATE`, explicit-column copy (never positional tuple append), foreign keys/immutable update-delete triggers, owned-table/trigger exact catalog check, reconciliation, then marker-last write. Failure rolls back data/catalog and leaves the prior marker.

**v17 deterministic backfill is mandatory.** Before writing marker v17, the migration scans every v14–v16
terminal `mode=backup` AnswerRecord and every immutable `FeedbackRecord(verdict="bad")`. For each source it
requires exactly one canonical source receipt, exactly one matching audit, and exact same-org Request/AnswerRecord
binding. For an AnswerRecord that canonical pair is its unique no-approval `OwnerAnswerIngest` receipt/audit **or**
its unique approval `ApprovalDisposition` receipt/audit, selected by the record lineage and never both; for a
FeedbackRecord it is its unique `feedback.create` receipt/audit. It derives the above ID only from
`{source_kind, org_id, source_id, source_receipt_digest}` and inserts
exactly one pending intent with the frozen source revision/digest/timestamp already described. Missing, tampered,
ambiguous or cross-bound source receipt/audit/binding fails closed: the whole migration rolls back and marker stays
v16. Noneligible historical source (`mode!=backup` or verdict!=bad) creates intent 0. A retried migration may read
the same deterministic row only when every frozen value matches; it never reseeds or reinterprets the source.
After marker v17 commits, startup projector recovery drains these pending historical intents by the normal lease
protocol. New source writers use the identical deterministic ID and same-transaction intent rule.

**v18 disposition hardening is a separate forward migration.** Exact approved v17 is the only input. It creates
one-to-one BackupReview/Reevaluation head rows from each immutable v17 aggregate, immutable disposition receipt/audit
tables, `AnswerCorrectionRecord`, and `ReanswerRequested` companion tables. A fault before or after marker replacement
rolls the transaction back to exact v17. Fresh installation therefore commits the v17 producer/backfill shape first
and immediately applies v17→v18; readiness and Central installation marker expose only the final v18 shape.

Before accepting inbox reads/writes and at startup, v15+ validates exact marker, FK/reverse links, unique request-to-ConflictCase, concurrence/receipt bindings, Approval successor lineage, immutable audit/receipt links and **exactly one** source writer outbox intent per all eligible historical and new source, with no intent for a noneligible source. Each intent/delivered marker/producer receipt must have exact source digest/revision/timestamp and reverse binding. Any orphan, extra, missing or mismatched row makes `/inbox` unavailable; it never repairs by creating a synthetic disposition. Async producer startup recovery is bounded/idempotent and runs after catalog validation; it invokes no Card Owner, Owner API, or Agent Runtime.

### 7. finite slices and deterministic gates

1. **B — Conflict:** v15 aggregate/concurrence UoW, projection/API, all four outcomes and exact Request transitions; deterministic fake Authority/Registry and concurrency/replay/fault tests; Fast + Contract Gate.
2. **C — Approval:** existing ApprovalItem projection and exact disposition/reassignment adapters; stale generation, target binding, successor/open invariant, edit/reject validation, receipt replay tests; Fast + Contract Gate.
3. **D — Backup/Reevaluation:** v17 aggregates, deterministic v14–v16 historical backfill, terminal-backup and bad-feedback outbox producers, correction/reanswer append-only records, migration fault/retry and projector restart/reconciliation tests; Fast + Contract Gate.
4. **E — BFF/UI/review:** exact routes and DTO guards, session/error hiding, no raw relay, accessible `/inbox` tabs, legacy action parity review; Fast + Contract Gate and independent code review.

No slice runs Full Gate or claims RB3.5 evidence completion. RB3.5 alone adds Owner-local raw/full evidence open/release and its source Owner release receipt.

## Consequences

- Lost inbox actions reappear as durable Central behavior, not a legacy fixture or a read-only mock.
- A correction supersedes immutable answer data; feedback and reevaluation never silently mutate answers or scores.
- Approval reassignment remains a distinct, auditable, still-open generation transition.
- Central never proxies or stores raw/full Owner-local evidence in RB3.2b.5.
