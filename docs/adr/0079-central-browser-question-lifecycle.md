# ADR 0079 — Central browser Question Request 수명주기와 전용 BFF

- 상태: Accepted (RB3.2b.4-A 설계 동결; 구현·support 승격·Full Gate 미완료)
- 날짜: 2026-07-31
- 관련: ADR 0004 (중앙 Authority), ADR 0031 (SSE), ADR 0042 (Question Request 수명),
  ADR 0043 (request-first/finalization), ADR 0071 (Non-actionable Conversational Intake),
  ADR 0075 (세 installation artifact), ADR 0077 (browser session), ADR 0078 (Central Next BFF)

## 맥락

Central Server는 RB3.1a에서 `Received` Question Request의 create/read-own만 조립했다. 반면
retire된 `web/index.html`을 옮기는 현재 `frontend/app/ask`는 generic `/api/[...path]`의
`/api/ask*`와 demo/legacy question surface 계약을 기대한다. 이 경로는 browser session-derived
principal, durable Router/Conflict/Manager/Approval 수명, requester-bound feedback과 Central
product composition의 fixture 금지 경계를 증명하지 않는다.

단순 인사까지 담당 지정이나 Manager 큐로 보내는 문제도 다시 열리지 않아야 한다. 실제 업무
질문의 0-match를 `Unowned`에서 끝내면 사용자 결과 기준의 미아 없음 불변식이 깨진다.

## 결정

### 1. 전용 browser → Next → private Central route

RB3.2b.4의 Central Next BFF와 private Central API mapping은 아래 **exact 네 route/method-path**로
고정한다. `{request_id}`는 URL-decoded 뒤 ASCII `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` 하나의 segment여야
한다. extra query, duplicate/encoded slash, trailing segment와 generic catch-all은 모두 거부한다.

| browser → Central Next | private Central API | method | current Authority | 의미 |
|---|---|---|---|---|
| `/api/questions` | `/v1/questions` | POST | `session.read`, `question.create` | Router 이전 durable Question Request 생성 |
| `/api/questions/{request_id}/stream` | `/v1/questions/{request_id}/stream` | GET | `session.read`, `question.read` | typed SSE 구독·재연결 |
| `/api/questions/{request_id}` | `/v1/questions/{request_id}` | GET | `session.read`, `question.read` | canonical safe lifecycle projection |
| `/api/questions/{request_id}/feedback` | `/v1/questions/{request_id}/feedback` | POST | `session.read`, `feedback.create` | requester-bound immutable feedback evidence |

`/api/ask`, `/api/ask/stream`, `/api/requests/*`, legacy `/ask*` compatibility URI와
`/api/[...path]` generic BFF는 Central product `/ask`에서 사용하거나 fallback하지 않는다. BFF는
각 행을 별 route handler로 구현하고 fixed private Central origin의 같은 method/path만 manual
redirect/no-store로 relay한다. request Host, URL, query 또는 header는 upstream/redirect를 결정하지
못한다. Central product root는 `server.py`, `web.py`, `demo_*`, fixture Owner Runtime을 import하거나
호출하지 않는다.

### 2. browser session·request envelope·오류

모든 route는 `__Host-aon-central-session`의 digest로만 Browser Session Principal을 해석한다.
Central은 active/expiry/end, existing Registry User/org binding, current `session.read`와 해당
`question.*` Authority를 every read/write에 다시 확인한다. requester/org/role/authority/session,
Owner 또는 Agent Card의 body/header/query self-claim은 principal·ResourceRef의 근거가 아니다.
requester가 아닌 Request/AnswerRecord는 존재 여부와 무관하게 `404 question_not_found`다.

POST는 ADR 0078과 같은 exact `Origin == central_public_origin`, `Sec-Fetch-Site: same-origin`,
`Sec-Fetch-Mode: cors`, `Sec-Fetch-Dest: empty`, readable `__Host-aon-central-csrf`와 exact
`X-AON-CSRF`, `Idempotency-Key` (`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`), `application/json`만 받는다.
query/body self claim, forwarded/`x-forwarded-*` caller provenance, malformed/oversize (64 KiB) body는
fail-close한다. GET은 body/query 없이 session cookie만 받으며 stream에 한해 bounded
`Last-Event-ID` (`[1-9][0-9]{0,18}`)와 `Accept: text/event-stream`만 추가로 허용한다. Next는
cookie와 allowlisted request facts만 전달하며 Central API가 만든 response의 safe headers,
`Set-Cookie`, `X-Request-ID`, SSE `Cache-Control: no-cache, no-transform`만 relay한다.

오류 body는 언제나 `{ "error": code }`다. session 부재·만료는 `401
browser_session_unauthenticated`, session/action deny는 `403 browser_session_forbidden` 또는
`question_forbidden`, CSRF는 `403 browser_csrf_forbidden`, foreign/missing은 `404
question_not_found`, semantic/idempotency conflict는 `409 question_request_conflict` 또는
`question_feedback_conflict`, invalid DTO/path/stream cursor는 `422 invalid_question_request`,
`invalid_question_feedback`, `invalid_question_stream_request`, dependency/schema/policy failure는
`503 question_lifecycle_unavailable`이다. BFF transport failure는 backend detail 없이 `502
central_question_unavailable`로 닫는다.

### 3. exact DTO와 idempotency

```json
POST /v1/questions
{"question":"환불 규정을 알려줘"}

201
{"request_id":"q_01", "state":"received", "created_at":"2026-07-31T00:00:00Z", "replayed":false}

POST /v1/questions/q_01/feedback
{"record_id":"a_01", "verdict":"good", "comment":"정확합니다"}

201
{"request_id":"q_01", "record_id":"a_01", "feedback_id":"f_01", "verdict":"good",
 "submitted_at":"2026-07-31T00:01:00Z", "replayed":false}
```

create input은 exact `question` 하나(비blank, 64 KiB 이하), feedback input은 exact
`record_id`, `verdict` (`good|bad`), `comment`만 가진다. `comment`는 항상 JSON string이며 `""`를
허용하고 최대 4096 UTF-8 bytes다; `null`·unknown field는 거부한다. leading/trailing text는 보존하고
canonical payload digest는 original JSON string의 UTF-8 bytes를 기준으로 한다. trim, Unicode normalization,
금지 문자 같은 추가 변환/검증은 만들지 않고 기존 JSON string validation을 사용한다. feedback의 `record_id`는
path Request에 finalization된 AnswerRecord이며 같은 session-derived requester가 소유해야 한다.

create command identity는 `(org_id, requester_id, question.create, Idempotency-Key, canonical body)`다.
feedback command identity는 `(org_id, requester_id, feedback.create, path request_id,
Idempotency-Key, canonical payload digest)`다. 같은 identity는 current session/Authority를 다시 확인한 뒤 원
safe result와 `replayed=true`만 돌려주며 write를 추가하지 않는다. 특히 feedback replay 전에도
requester-owned path Request와 finalized AnswerRecord의 `request_id` binding을 exact re-read한다.
같은 org/requester/action에서 Idempotency-Key를 다른 path 또는 canonical payload에 재사용하면
`question_feedback_conflict`다.

HTTP create는 current `session.read`와 `question.create` Authority 검증을 마친 뒤 one transaction으로
durable `Received`와 create receipt를 commit한다. 그 **별도 commit 뒤** Router/dispatch가 시작한다.
따라서 Router/Conflict/Manager/Approval/Runtime fault가 create를 없애지 않으며, Router disposition
transaction은 Received create transaction과 합쳐지지 않는다.

`QuestionFeedbackEvidence`의 durable `FeedbackRecord`는 generated `feedback_id`를 가진 append-only
immutable record다. 그것은 AnswerRecord의 text, source, attribution, mode, revision 또는 terminal
Question Request를 변경하지 않으며 requester binding·record/request binding·command receipt와 함께
저장된다. 익명 feedback, `record_id`만 아는 feedback, legacy upsert를 Central product path에 쓰지 않는다.

#### 3a. B3 requester feedback composition

B3는 private Central `POST /v1/questions/{request_id}/feedback`의 domain UoW만 조립한다. Central Next
BFF의 exact browser route와 `/ask` presentation은 후속 C/D/E이며, actual Card Owner answer의 cross-install
관통은 여전히 RB3.5/RB3.7의 독립 조건이다. command는 path `request_id`, current authenticated Browser
Session에서 유도한 Registry User/requester, `record_id`, `verdict`, `comment`, Idempotency-Key만 받는다.
`verdict`는 exact `good|bad`다. `comment`는 verdict-only feedback을 위한 `""`를 포함해 항상 JSON string,
최대 4096 UTF-8 bytes이며 leading/trailing을 보존한다. canonical payload digest는 그 original UTF-8 bytes를
쓴다; trim/Unicode normalization/새 금지 문자 규칙 없이 기존 JSON string validation만 적용하고 `null` 또는
unknown field는 `422 invalid_question_feedback`이다.

한 transaction은 current same-org Browser Session/Registry User와 `session.read`, `feedback.create`
Authority를 확인하고, path Request가 그 requester 소유의 `AnsweredRequest`인지, `record_id`가 그 Request에
finalization된 canonical AnswerRecord인지 exact-read한다. pending, Declined, Failed, foreign Request 또는
foreign/hidden/missing AnswerRecord에는 FeedbackRecord가 생기지 않으며 `question_not_found` 또는
fail-closed typed denial로 body/record detail을 노출하지 않는다. 이 precondition·FK/catalog/request↔record
reverse binding이 corrupt, missing 또는 tampered이면 restart/replay도 unavailable/write 0으로 닫는다.

fresh UoW는 FeedbackRecord, command receipt와 safe audit companion을 함께 append하고 exact-read한 뒤
commit한다; commit 전 fault는 세 record 모두 write 0으로 rollback한다. 이는 Request state transition이나
Answer Finalization이 아니며 audit/receipt가 state 또는 FeedbackRecord의 대체물이 아니다. unique receipt
identity가 concurrent identical command를 one
FeedbackRecord/receipt/audit으로 수렴시키고, exact replay는 current reauthorization 뒤 immutable prior safe
result만 write 0으로 돌려준다. feedback은 routing score·Authority·Card·Request·AnswerRecord를 바꾸거나
재작성하지 않는다. B2의 `AnsweredProjection`/SSE `done`에는 FeedbackRecord나 feedback payload를 추가하지
않고, terminal answer의 text/source/attribution confidentiality도 확장하지 않는다. feedback 목록·표시 UI는
후속 presentation contract에서만 연다.

### 4. lifecycle projection과 SSE reconnect

canonical `GET /v1/questions/{request_id}`과 SSE terminal/pending payload는 internal route,
candidate, policy, score, Manager/Owner credential, raw/full source를 포함하지 않는 sealed safe
projection이다.

| sealed domain state | exact wire `type` / `state` / `kind` / `retryable` | required safe fields |
|---|---|---|
| `Received` | `pending` / `received` / `routing` / `true` | `request_id`, `message` |
| `ReadyToDispatch` | `pending` / `ready_to_dispatch` / `routed` / `true` | `request_id`, `message` |
| `AwaitingAnswer` | `pending` / `awaiting_answer` / `routed` / `true` | `request_id`, `message` |
| `AwaitingApproval` | `pending` / `awaiting_approval` / `routed` / `false` | `request_id`, `message` |
| `AwaitingConflict` | `pending` / `awaiting_conflict` / `contested` / `false` | `request_id`, `message` |
| `AwaitingManager(public_kind=unowned)` | `pending` / `awaiting_manager` / `unowned` / `false` | `request_id`, `message` |
| `AwaitingManager(public_kind=contested)` | `pending` / `awaiting_manager` / `contested` / `false` | `request_id`, `message` |
| `AwaitingManager(public_kind=dispatched)` | `pending` / `awaiting_manager` / `routed` / `false` | `request_id`, `message` |
| `AnsweredRequest` | `answered` / `answered` / absent / `false` | exact `AnsweredProjection` below |
| `DeclinedRequest` | `declined` / `declined` / absent / `false` | `request_id`, `reason_code`, `message` |
| `FailedRequest` | `failed` / `failed` / absent / `false` | `request_id`, `error_code`, `message` |

유일한 Answered wire DTO는 `AnsweredProjection`이다. canonical GET은 이를 그대로 반환하고 SSE `done`은
동일한 JSON object를 data로 쓴다(event name은 transport framing일 뿐이다).

```text
AnsweredProjection = {
  type: "answered", state: "answered", retryable: false,
  request_id: nonblank, record_id: nonblank, text: nonblank,
  answered_by: {owner: RegistryUserId, agent_id: AgentCardId},
  mode: "full" | "backup", sources: SafePublishedReference[],
  review_status: "not_required" | "approved"
}
```

```json
{"type":"answered", "request_id":"q_01", "state":"answered", "retryable":false,
 "record_id":"a_01", "text":"...", "answered_by":{"owner":"root", "agent_id":"legal"},
 "mode":"full", "sources":["published/legal/refund"], "review_status":"approved"}
```

`answered_by.owner`는 final AnswerRecord sender의 Registry User ID이고 `answered_by.agent_id`는 책임
Agent Card ID다. 둘은 attribution identifier일 뿐 route candidate, Authority evidence, Owner credential,
Owner Runtime 가용성 주장이 아니다. `CentralLifecycleDoneEvent`는 committed `CompletionReader` data로만
이 DTO를 만든다. legacy `DoneEvent` shape은 relay하지 않고 transient field에서 변환하지 않는다. adapter는
Central lifecycle seam에서 committed projection을 mapping한다.

SSE event name은 `accepted | token | pending | done | declined | failed | interrupted`만 허용한다.
모든 data는 path와 같은 `request_id`를 가져야 하고, `done`은 위 committed `AnsweredProjection`
그대로다. token은 finalized AnswerRecord를 exact-read한 뒤에만 best-effort로 보이며, approval
대기·conflict·failure에서 본문/source는 0이다. `id`는 stream-local positive decimal sequence다.

disconnect는 Request나 execution을 취소하지 않는다. browser는 동일 path에 bounded
`Last-Event-ID`로 재연결하고 Central은 durable current projection을 먼저 재구성한다. volatile token은
replay하지 않으며 terminal/pending은 한 번의 canonical projection으로 수렴한다. terminal 또는
stream interruption 뒤 browser는 항상 same `request_id`의 GET으로 닫는다. every emission과 every
reconnect는 current Browser Session과 `question.read` Authority를 다시 읽는다. 이미 열린 stream에서
revoke 또는 deny면 `interrupted(..., retryable=false, ...)`, dependency unavailable이면
`interrupted(..., retryable=true, ...)`만 내보내고 token, AnsweredProjection 또는 다른 body 누설 없이
닫는다. fresh reconnect도 이 재검증을 반복한다. requester ownership을 처음 세울 수 없는 fresh request는
normal 401/403/404/503 no-leak response를 쓴다. SSE transport 자체는 `FailedRequest`를 만들지 않는다.

### 5. Router 이전 intake와 실행 범위

HTTP create의 session/`question.create` Authority 검증과 durable `Received` commit **후** exact-only
Non-actionable Conversational Intake가 NFKC/casefold/공백 정규화한 전체 발화를 allowlist와 비교한다.
단일 인사만 일치하면 revision 1의 `Declined(reason_code="non_actionable_conversation")`로 전이하고
routing/dispatch Authority, Router, ConflictCase, ManagerItem, Agent Runtime 호출은 모두 0이다.
`안녕하세요, 환불 규정은?`처럼 업무가 섞인 발화는 정상 Router로 간다.

업무 질문의 0-match는 `Unowned`에서 멈추지 않는다. **Received create와 분리된 Router disposition
transaction**이 root User/Manager disposition의 `ManagerItem`과 `AwaitingManager` handling assignment를
같이 만들고, 위 retrieve/SSE projection으로 requester가 조회한다. 이것은 자동 답변 또는 root User가
Runtime이라는 뜻이 아니다.

Routed는 Central이 durable WorkTicket 또는 `AwaitingApproval`까지만 만든다. 이 slice의 Central
product root는 demo/fixture Owner Runtime으로 답을 만들거나 owner endpoint/A2A proxy/inbound를
열지 않는다. 실제 Card Owner answer submit과 cross-install answer 관통은 RB3.5/RB3.7에서만
완료로 계상한다. raw/full source, Owner credential, A2A credential·proxy·inbound, anonymous feedback은
모두 범위 밖이다.

### 5a. B2 sealed disposition·answer finalization composition

B2는 B1의 `process_received`와 같은 Central SQLite UoW family에 아래 sealed transition만 더한다.
어느 transaction도 `Received` create receipt를 되돌리거나 B3 feedback을 쓰지 않는다.

| triggering sealed input | one transaction의 domain transition·linked aggregate | transaction에 없는 것 |
|---|---|---|
| `Routed(intent, primary, requires_approval)` | `Received → ReadyToDispatch(route, attempt=1)`와 route disposition receipt/audit | WorkTicket, ApprovalItem, Runtime/Owner call |
| `Contested(intent, candidates)` | `Received → AwaitingConflict(case_id)`와 request-unique immutable `ConflictCase` candidate snapshot | WorkTicket, ApprovalItem, Runtime/Owner call |
| `Unowned` | B1 그대로 transaction-current RootManagerResolver proof를 확인한 `Received → AwaitingManager(public_kind=unowned)`와 request-unique `ManagerItem` | ConflictCase, WorkTicket, ApprovalItem, Runtime/Owner call |
| system `ReadyToDispatch` recovery | ADR 0042의 별 UoW가 `WorkTicket(pending)`·`work_ticket.create` receipt/audit/outbox intent와 `ReadyToDispatch → AwaitingAnswer(ticket_id)`를 atomic commit | external delivery/send, AnswerCandidate, ApprovalItem |
| sealed answer candidate ingest + current approval evaluation | approval required이면 `ApprovalItem`/draft/evidence와 `AwaitingAnswer → AwaitingApproval`; approval not required이면 `work_ticket.complete` receipt, ticket completion, lease release와 Answer Finalization의 `AwaitingAnswer → AnsweredRequest` | B3 feedback, browser response send, external Owner/A2A call |
| approved/rejected ApprovalItem disposition | approved candidate는 Answer Finalization으로 `AwaitingApproval → AnsweredRequest`; reject는 same approval disposition UoW로 `AwaitingApproval → DeclinedRequest(reason_code="approval_rejected")` | AnswerRecord mutation after terminal, B3 feedback |
| sealed Central terminal-failure command | only a policy-defined non-retryable failure may `ReadyToDispatch|AwaitingAnswer|AwaitingApproval → FailedRequest` with terminal evidence | raw transport/A2A failure를 terminal로 위장 |

Router input is the sealed `RoutingDecision = Routed | Contested | Unowned`; an unrecognized value is
unavailable with write 0. Before the first three rows commit, Central re-reads the canonical Agent Card/
Registry binding and current central route Authority for each route/candidate. Card domains, candidates,
`requires_approval` or Owner names are conservative Card metadata, not Authority declarations. `Unowned`는
같은 transaction 안의 `RootManagerResolver`가 (a) request org의 root User가 정확히 하나이고, (b) 그
User가 현재 `manager.act` action을 수행할 Authority grant를 가지며, (c) Router의 `escalated_to`가 그 exact
root Manager ID와 일치함을 증명할 때만 ManagerItem을 만든다. drift, revocation, missing Registry
User/Card/root, policy unavailable 또는 failed precommit recheck writes no Case, ManagerItem, Request
transition, receipt/audit/outbox record.

`ConflictCase` and `ManagerItem` use `UNIQUE(request_id)` and their stored immutable request/intent/
candidate-or-escalation snapshot must exactly equal the target `QuestionRequest` state. Concurrent same
disposition has one winner; an exact reread is convergence, a different decision/snapshot is conflict, and
fault/restart leaves either the old `Received` or the fully linked target—never an orphan linked aggregate.
Every `state_kind=awaiting_manager` Request has exactly one same-org ManagerItem and state `item_id` equals
that row's exact `item_id`; missing, extra, cross-org, or mismatch fails closed on reopen/readiness. The public
initial-transition writer rejects unlinked AwaitingManager/AwaitingConflict, both linked outputs together, and
AwaitingAnswer (which only the WorkTicket UoW may write).
`WorkTicket` is likewise unique by `(request_id, attempt)` and its create command digest. Enqueue consumes
the already authorized frozen `ReadyToDispatch.route`; per ADR 0042 it resolves the current Owner only as a
directory address and does not re-authorize route eligibility. Missing directory data leaves the Request
`ReadyToDispatch` for recovery rather than creating a partial ticket.

#### Owner delivery and answer ingress boundary

The committed pending WorkTicket is the single durable delivery intent; B2 creates no second queue and calls
no Owner Runtime during its UoW. Lease/acquire/send happens only after commit through an injected
`OwnerDeliveryPort`. B2 has no product implementation of that port and no Central HTTP endpoint for an
Owner, A2A proxy, inbound A2A server, Remote A2A Agent Card discovery, credential or raw/full-source relay.
Deterministic tests may inject a sealed delivery result but Central product composition must not import/call a
demo/fixture Owner Runtime.

`ticket_id` is the stable delivery identity. A worker acquires it in a separate `BEGIN IMMEDIATE` UoW only
from no-claim or an expired `leased` claim, persisting `worker_id`, `lease_until`, and an incremented
`delivery_attempt`; an active lease or `delivered` row causes no port call. Only that post-commit claimant
calls `OwnerDeliveryPort`. A successful call CASes its exact claim to `delivered` and writes the exact durable
receipt `delivery:{ticket_id}:{delivery_attempt}`. A timeout, exception, or ack failure deliberately leaves
the lease in place: before expiry no worker may call, while after expiry/restart one worker may redeliver the
same ticket. This is at-least-once delivery, explicitly not exactly-once. Claim/ack row, ticket/request reverse
aggregate binding, foreign keys and the owned catalog/trigger set are read back at startup and before claim/ack;
tamper or missing/wrong state fails closed.

The subsequent `OwnerAnswerIngest` is an internal typed handoff, not a browser or anonymous payload:

```text
OwnerAnswerIngest(
  ticket_id, request_id, expected_request_revision, attempt, route, candidate
)
```

All IDs/route/attempt are exact-linked to the pending WorkTicket and `AwaitingAnswer`; owner/org/Agent Card
are derived from the verified delivery binding, not supplied by the handoff. Fresh ingest re-reads the
ticket's pending status, Request revision/state/ticket/route/attempt, durable owner fence and current
binding proof before write. Central then evaluates the current ApprovalPolicy: it creates an immutable
`FinalizationCandidate` only for `NoApprovalRequired`, otherwise creates the matching ApprovalItem/draft.
For an approval-required candidate, one transaction writes the answer-ingest receipt (command identity and
candidate digest), freezes the policy decision/digest and delivery-binding version, consumes/completes the
pending WorkTicket, releases its lease, persists the immutable candidate as the Approval draft/evidence,
creates the ApprovalItem, and CASes `AwaitingAnswer → AwaitingApproval`. Partial approval pending state is
invalid. Approval reject therefore starts only after that WorkTicket is terminal; it resolves the ApprovalItem
and CASes `AwaitingApproval → DeclinedRequest(reason_code="approval_rejected")` without re-consuming a
ticket.

Answer-ingest receipt identity is `(org_id, verified delivery subject/session, ticket_id, request_id,
expected_request_revision, candidate_digest)`. It stores the exact current policy decision/digest and
binding version used for the first commit. An exact replay first reauthenticates the current delivery
session/Authority plus Request, owner/Card and WorkTicket binding. It returns the immutable internal result
only when the candidate, frozen policy decision/digest and binding version all still match. Different command
or candidate is `answer_ingest_conflict`; current authorization/binding loss is
`answer_ingest_binding_forbidden`; policy digest/decision drift is `answer_ingest_policy_conflict`; a
dependency cannot be read is `answer_ingest_unavailable`. These failures make no new transition, draft,
receipt, AnswerRecord or terminal state. Internal ingest receipts never disclose a terminal AnsweredProjection;
the requester receives terminal data only through separately reauthenticated own-read/stream routes. The later
RB3.5 paired Owner Installation is responsible for making that binding a real cross-install authentication
flow.
Google A2A's actual SDK/profile/pinned outbound adapter remains Card Owner-local RB3.3/3.4 work under ADR
0074; its completed text can only become this candidate after the Owner boundary and can never bypass
Approval or Finalization.

#### AwaitingApproval disposition writer

B2 owns the domain transition writer `ApprovalDispositionApplication`; RB3.2b.5 may later expose its
authorized list/detail/UI API but may not write around this boundary. Its only input is a typed command:

```text
ApprovalDispositionCommand(
  request_id, approval_item_id, expected_approval_item_revision,
  expected_request_revision, actor=CurrentRegistryUserSession,
  decision=approve | approve_with_edit(edited_text) | reject(reason_code), idempotency_key
)
```

The **first-disposition path** has no matching receipt. Before any receipt or state write, one transaction
resolves the actor's current Registry User session/identity, same org/request scope, and current central
`approval.decide` Authority. It exact-reads the *open* ApprovalItem and `AwaitingApproval` Question Request
and requires: same request/org/route/attempt; the command's expected ApprovalItem and Request revisions as
the pre-state CAS; the ApprovalItem's frozen candidate digest, policy decision/digest and binding version; and
the Request's `draft_ref == approval_item_id`. It then writes the receipt/resolved ApprovalItem and the exact
terminal successor in that one UoW. The WorkTicket was consumed/terminal in the earlier approval-required
ingest UoW, so this disposition cannot consume, reopen or otherwise change it. Foreign, revoked, stale or
no-longer-authorized actors cannot terminalize a Request.

The append-only approval disposition receipt identity is exactly `(org_id, request_id, approval_item_id,
actor_registry_user_id, expected_approval_item_revision, expected_request_revision, decision_kind,
canonical_decision_payload_digest, Idempotency-Key)`. `approve` or `approve_with_edit` atomically resolves the
ApprovalItem, writes this receipt, and invokes Answer Finalization to write AnswerRecord, `AnsweredRequest`,
terminal audit, applicable SessionTurn and delivery outbox. `reject` atomically resolves the ApprovalItem,
writes this receipt, CASes `AwaitingApproval → DeclinedRequest(reason_code="approval_rejected")`, and writes
the terminal audit; it does not create an AnswerRecord. In either case a receipt/audit is a record, not a
substitute for the state transition.

The **replay path** finds the receipt identity before attempting the first-disposition CAS, then repeats current
session/identity, `approval.decide` Authority and org/request scope. It exact-reads the receipt-bound resolved
ApprovalItem revision/state and its exact terminal successor: for approve/edit, Request/AnswerRecord/audit IDs,
digests and decision; for reject, Request/Declined/audit IDs, digests and decision. Only that immutable prior
result is returned, with write 0. The first success's now-terminal Request/ApprovalItem and their later
revisions are therefore normal replay evidence, not a conflict. Once a receipt is found, conflict is limited to
a receipt with a different successor, decision, candidate, policy, binding or actor identity, or to a missing or
tampered receipt-bound terminal row; current identity/policy/storage unavailability or authorization denial is
fail-closed with write 0. Replay never manufactures a second terminal state or reveals terminal data to an actor
that fails current authorization. Requester-facing terminal projection remains the separately reauthenticated
own-read/stream path.

`Answer Finalization` remains the only writer of AnswerRecord and terminal `AnsweredRequest`. Its same
SQLite transaction writes the immutable AnswerRecord, terminal Request CAS, terminal audit, SessionTurn
when applicable and delivery outbox; ticket completion/answer receipt/lease release join that transaction
for an answer ingress. These are distinct concepts even when atomic: an audit/outbox/receipt never stands in
for a state transition. Duplicate/restart/concurrent completion returns the one exact completion bundle or
conflict; it cannot create a second record or terminal audit.

`AnsweredProjection` is derived only by exact-read of that committed bundle and is the one DTO for canonical
GET and SSE `done`. Its sealed enum is `mode = full|backup`, `review_status = not_required|approved`:

```json
{"type":"answered", "request_id":"q_01", "state":"answered", "retryable":false,
 "record_id":"a_01", "text":"...", "answered_by":{"owner":"registry-user-id", "agent_id":"card-id"},
 "mode":"full", "sources":["published-index-safe-reference"], "review_status":"approved"}
```

`text` and `sources` come from the immutable AnswerRecord/CompletionBundle; sources are safe published
references, never raw/full source. `owner` is the final responsible Registry User and `agent_id` is the
responsible Agent Card, neither an availability claim nor credential. A nonterminal or corrupt/mismatched
bundle cannot be converted into `done`; it is unavailable with body 0. B1's greeting remains terminal
Declined and never enters any B2 Router, Case, Manager, WorkTicket, Approval or answer path. B3 alone adds
requester feedback evidence.

The B2 deterministic seams are `FakeRouter` for all three sealed decisions, reloading central
Authority/Registry binding fakes, SQLite transaction/fault/restart injection, a no-network
`OwnerDeliveryPort`, and a typed answer-ingest fixture outside the product root. Tests cover each table row,
CAS race winner/convergence, divergent replay conflict, binding/policy drift before commit, no fixture/A2A
import, AnsweredProjection exact-read and atomic rollback. Fast/Contract/Affected gates apply; Full Gate is
forbidden until RB3.8.

### 6. 구현 순서와 검증

| slice | 책임 | 결정론 acceptance |
|---|---|---|
| A | 이 ADR 및 SSOT contract | route/DTO/state/forbidden-flow 정합 검사 |
| B | v6 marker-last durable lifecycle schema·Router/Conflict/Manager/Approval/Answer/feedback composition | Received-before-Router, greeting 0-call, 0-match ManagerItem, immutable feedback/replay/fault/restart |
| C | private Central API | exact auth/Authority/error/DTO/SSE/reconnect/own-read Contract |
| D | dedicated Central Next BFF | no generic route, cookie/CSRF/idempotency/provenance, SSE passthrough/reconnect, backend-reach-0 negatives |
| E | `/ask` React client | create→stream→retrieve, terminal/pending/feedback/reconnect UX and compiled standalone integration |

각 slice는 Fast·Contract·Affected 및 independent review를 실행한다. Full Gate, real IdP/TLS browser,
three-install support 승격은 RB3.7/8에 남긴다.

## 결과

Central Next `/ask`는 legacy HTML 또는 generic proxy가 아니라 authenticated requester가 durable
Question Request를 create/observe/feedback하는 전용 surface가 된다. 업무 질문은 durable escalation까지
남고, 단독 인사만 보수적으로 terminal Declined가 된다. 이 ADR은 Owner Runtime answer, remote A2A,
raw evidence, support 또는 production readiness를 구현·승격했다고 주장하지 않는다.
