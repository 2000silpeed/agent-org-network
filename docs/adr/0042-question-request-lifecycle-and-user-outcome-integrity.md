# ADR 0042 — Question Request 수명주기와 사용자 결과 기준 미아 없음

- 상태: 채택(Accepted)
- 날짜: 2026-07-12
- 계보: ADR 0008(ConflictCase)·0011(WorkTicket tracking)·0014(Manager 큐)·0024(Session)·0031(SSE)·0033(AnswerRecord)을 정밀화한다. ADR 0037의 **Contested 즉시 답+합의 병행**은 이 결정이 대체한다. ADR 0004의 중앙 Authority, 등록 무결성, 전이≠기록은 그대로 계승한다. 구현 경계와 Approval 선행 순서는 ADR 0043이 구체화한다.
- 구현 상태: P17.2a~b 도메인·SQLite Request Store, P17.2c-1 Request-first 초기 라우팅 코어, P17.6a 최소 Approval 경계, P17.3a~c InMemory/SQLite Answer Finalization·SSE 실행 분리, P17.2c-2 웹·SSE·retrieve·MCP 전 표면 전환, P17.4 Unowned와 P17.5 Contested의 같은 Request 재개·종결까지 구현·독립 리뷰를 마쳤다. online Owner 사전승인과 offline 자동발신 사후교정 증거도 공통 Finalization에 결박했다. Unowned·Contested 처분은 아직 단일 프로세스 InMemory claim과 demo request-scoped Authority 범위다. P17.6b Approval 운영, durable linked workflow·Approval transaction·lease/outbox 소비, production Authority/RBAC는 후속이다. Approval 만료·재지정 의미는 ADR 0048이 정밀화한다. P17 파일럿 진입 게이트를 통과하기 전에는 기업 실사용을 주장하지 않는다.

## 맥락

기업 준비도를 처음부터 다시 점검하면서, 기존의 “어떤 질문도 미아로 남지 않는다”가 사용자 결과를 보장하지 않는다는 사실이 확인됐다.

- 비동기 Routed만 프로세스 메모리의 `tracking`을 받는다. Contested와 Unowned는 추적값 없이 Pending으로 끝난다.
- `create_central_app()`은 Manager 큐를 기본 조립하지 않아, Unowned가 “매니저에게 전달했다”고 응답한 뒤 실제로는 아무 큐에도 남지 않았다. P17.1 hotfix는 이 배선 누락만 우선 복구한다.
- ConflictCase는 intent당 하나만 열어 두 번째 이후 질문 원문을 잃는다.
- 합의·Manager 처분은 Case/Item만 닫고 원 질문을 재실행하거나 질문자에게 결과를 돌려주지 않는다.
- blocking·SSE streaming·retrieve가 Approval·AnswerRecord·Audit·Session을 서로 다르게 처리한다. 일반 streaming 답은 AnswerRecord가 없고, retrieve는 Approval을 다시 적용하지 않는다.
- WorkTicket·ConflictCase·ManagerItem·AnswerRecord의 식별자는 각 책임에 맞게 설계됐지만, 질문 한 건의 접수부터 최종 결과까지를 대표하는 aggregate가 없다.

내부 큐 항목이 존재하는 것과 질문자가 최종 결과를 받는 것은 다르다. 기업용 제품의 불변식은 후자를 기준으로 다시 정의해야 한다.

## 결정

### 1. 모든 질문은 안정적인 Question Request다

질문을 라우팅하기 전에 `QuestionRequest`를 영속 저장하고 불투명 `request_id`를 발급한다. `request_id`는 질문 한 건의 전체 수명에 대한 상관키다.

- `WorkTicket`은 한 번의 실행 시도다.
- `ConflictCase`는 책임 다툼이다.
- `ManagerItem`은 사람 처분 큐다.
- `AnswerRecord`는 확정 답의 기록이다.

이 식별자들은 `request_id`를 참조하지만 서로를 대체하지 않는다. 기존 `tracking`을 이름만 바꾸는 방식은 쓰지 않는다.

Question Request는 최소한 `org_id`, `requester_id`, `session_id?`, 질문 원문, 필요한 경우의 입력 맥락 스냅샷, intent, 최초 disposition, 상태, revision, 생성·변경 시각을 가진다. 질문·맥락은 민감정보가 될 수 있으므로 production 도입 전 보존·삭제·접근 정책을 확정한다.

`QuestionRequestStore.create`는 새 수명의 진입점이다. `Received`, `revision=0`, `created_at=updated_at`인 Request만 받는다. 이미 라우팅됐거나 종결된 aggregate를 최초 행으로 넣어 전이 규칙을 건너뛸 수 없다. InMemory와 SQLite는 같은 생성 검증자를 사용하며, Unit of Work가 쓰는 내부 insert도 이 검증을 우회하지 않는다.

intent는 실제 분류 라벨이 있을 때만 저장한다. Routed와 Contested는 nonblank intent가 필수다. 분류 자체가 되지 않은 Unowned는 `intent=None`을 허용하되 빈 문자열을 저장하지 않는다. Router가 빈 문자열을 돌려주는 기존 경로는 Application Service 경계에서 `None`으로 정규화한다. `initial_disposition`은 최초 라우팅 결과이므로 Unowned에도 기록한다.

### 2. 상태가 곧 수명주기다

상태는 sealed sum으로 표현한다.

```text
Received
 ├─ ReadyToDispatch
 ├─ AwaitingConflict
 ├─ AwaitingManager
 └─ FailedRequest

ReadyToDispatch ── AwaitingAnswer | AwaitingApproval | AnsweredRequest | FailedRequest
AwaitingAnswer  ── AwaitingApproval | AnsweredRequest | AwaitingManager | FailedRequest
AwaitingConflict ─ ReadyToDispatch | AwaitingManager | DeclinedRequest | FailedRequest
AwaitingManager  ─ ReadyToDispatch | DeclinedRequest | FailedRequest
AwaitingApproval ─ AnsweredRequest | DeclinedRequest | FailedRequest
                 └ AwaitingApproval  # ADR 0048 전용 새 Item 재지정 전이만 허용
```

`AnsweredRequest | DeclinedRequest | FailedRequest`는 terminal이며 부활하지 않는다. `DeclinedRequest`는 사람이 확인한 뒤 사유를 남기고 답하지 않기로 한 명시적 종결이다. 일시 장애는 곧바로 Failed로 닫지 않고 재시도 가능한 상태에 둔다.

모든 비종결 Request는 정확히 한 `HandlingAssignment(kind, ref, due_at)`을 가져야 한다. `kind`는 `system | runtime_ticket | conflict_case | manager_item | approval_item`이다. `Received`·`ReadyToDispatch`처럼 프로세스 장애 뒤에도 남을 수 있는 시스템 작업도 명시적 처리 단위와 SLA를 가진다. 최초 `Received`의 system ref는 `question-intake:{request_id}`로 고정해 Request별 접수 작업을 식별한다. generic handler 이름이나 호출자 override로 바꿀 수 없다. 사람의 실제 신원은 runtime ticket의 RouteTarget, ConflictCase, ManagerItem, ApprovalItem을 현재 Registry·조직 그래프와 조인해 구한다. assignment의 `ref`는 해당 상태의 ticket/case/item/draft 참조와 같아야 하며, terminal 상태에는 assignment가 없다. terminal도 아니고 처리 단위·SLA가 없는 상태는 불법이다.

영속 행을 복원할 때도 도달 가능한 상태 조합을 검증한다. `AwaitingConflict`의 최초 disposition은 contested여야 한다. `AwaitingManager(public_kind="unowned")`는 unowned, `public_kind="contested"`는 contested에서만 올 수 있다. dispatched Manager 대기는 이전 실행의 RouteTarget을 보존하며 그 intent가 현재 Request intent와 충돌해서는 안 된다. 다만 Unowned로 시작해 Manager가 intent와 담당을 정한 경우처럼 최초 intent가 없던 수명은 RouteTarget의 nonblank intent를 현재 실행 기준으로 쓴다.

이 방식은 “후보 Owner 여러 명이 합의하는 Contested”를 억지로 한 사람에게 귀속하지 않으면서도, 사용자 질문이 어느 처리 단위에 있고 언제 SLA를 넘기는지 Request만으로 추적하게 한다. linked entity는 P17.4~P17.9에서 Request와 같은 durable 경계에 저장해 참조 무결성을 완성한다.

### 3. 책임자가 확정되기 전에는 최종 답을 보내지 않는다

Contested는 `AwaitingConflict`로 남고 Pending을 반환한다. 사전순 후보를 임의의 primary로 골라 `answered_by`를 붙이지 않는다. 기존 ADR 0037의 “답+합의 병행”은 이 결정으로 대체한다.

co-grounding과 ComplementEdge는 폐기하지 않는다. 합의나 Manager 중재로 primary가 확정된 뒤, 보조 지식을 넓히는 용도로만 사용한다.

첫 구현은 **Question Request당 ConflictCase 한 건**으로 한다. 기존 intent 단위 중복 제거는 원 질문을 잃고 판례 적용 범위를 과도하게 넓히므로 제거한다. 여러 요청을 한 Case에 묶는 최적화는 문맥 동등성과 원자적 request-link를 증명한 뒤 별도 결정으로 연다.

### 4. 사람 처분은 원 질문을 재개하거나 명시적으로 거절한다

ConflictCase와 ManagerItem은 `request_id`를 직접 참조한다.

- 합의 `Agreed` 또는 Manager `AssignOwner`는 현재 Registry와 중앙 Authority를 다시 검증한 뒤 같은 Question Request를 `ReadyToDispatch`로 옮긴다.
- dispatch 가용성 문제의 `Reroute`도 같은 Request의 attempt를 올려 재개한다.
- 답하지 않기로 한 처분은 모호한 Dismiss가 아니라 사용자에게 전달 가능한 `DeclinedRequest(reason_code)`로 닫는다.
- 출처에 맞지 않는 처분 조합은 거부한다.

Manager가 문자열로 Agent Card를 지목해 Authority를 우회할 수 없다. 대상은 현재 Registry에 존재하고, 허용 후보 조건을 만족하며, 카드가 intent를 under-claim하고, 중앙 AuthorityPolicy가 그 조합을 허용해야 한다. 검증 실패 시 Case/Item을 열린 채 유지해 다른 대상을 고르게 한다.

### 5. Resume Claim으로 한 번만 재개한다

`QuestionRequestStore`는 revision 기반 compare-and-set을 제공한다. Case/Item 해소자는 예상 대기 상태를 `ReadyToDispatch`로 바꾸는 CAS에 성공한 한 주체만 재디스패치할 수 있다.

목표 계약에서는 CAS 뒤 프로세스가 중단돼도 durable `ReadyToDispatch`를 startup reconciler나 background runner가 이어서 처리한다. P17.4의 현재 InMemory Unowned 경로는 같은 Manager action 재시도와 SSE reconnect가 기존 scheduler를 다시 깨운다. canonical GET은 저장 상태만 조회한다. durable startup recovery와 `(request_id, attempt)` lease는 P17.9에서 완성한다.

P17.9 S4.5는 이 durable `ReadyToDispatch` 소비를 실체화한다. WorkTicket enqueue는 사람 명령이 아니라 **system 전이**다 — `work_ticket.create`는 `AUTHORITY_ACTION_MANIFEST`에 의도적으로 없고(중앙 재인가 0·S4.4 `manager.act`와의 결정적 차이), background runner(`request_recovery.recover_ready`)가 이미 인가된 채로 봉인된 `ReadyToDispatch.route`를 소비할 뿐이다. 권한은 route를 봉인한 시점(최초 라우팅·합의·Manager 배정)에 이미 중앙에서 행사됐으므로 enqueue는 그 결정을 재판정하지 않는다. 따라서 enqueue는 frozen route를 현재 Registry로 **eligibility 재검증하지 않는다**(route는 `ReadyToDispatch`에 봉인·under-claim/route-rule drift는 S5 timeout/reroute 몫). 다만 `durable_linked_work_tickets.owner_subject_id`는 frozen `RouteTarget`에 없으므로(`agent_id`만 보유) enqueue 시점에 좁은 Registry 조회로 현재 Owner를 **주소 해석**만 하며(directory lookup — eligibility 판정 아님), 조회 실패는 fail-closed로 Request를 `ReadyToDispatch`에 남겨 미아를 막는다(recover_ready 재시도·영구 drift는 S5). 명령이 durable 상태에서 합성돼 수신 payload가 없으므로 멱등 앵커는 command-local `command_digest`(`request_id`·`expected_request_revision`·`attempt`)이며, `UNIQUE(request_id, attempt)`가 attempt당 WorkTicket 단일성을 보강한다.

외부 LLM 계산 자체를 물리적으로 exactly-once라고 주장하지 않는다. 공급자가 멱등키를 지원하지 않으면 중복 계산은 생길 수 있다. 우리가 보장하는 것은 사용자에게 수용되는 terminal 결과, AnswerRecord, terminal audit, SessionTurn이 request당 최대 한 번이라는 점이다.

### 6. 모든 입출력 방식은 Answer Finalization을 공유한다

blocking·SSE streaming·비동기 retrieve·MCP는 각자 답을 확정하지 않는다. 공통 Answer Finalization이 다음 순서를 소유한다.

```text
저장된 RouteTarget으로 Approval/HITL 판정
→ AnswerRecord(request_id, UNIQUE)
→ QuestionRequest.AnsweredRequest(record_id)
→ terminal audit
→ SessionTurn(request_id, UNIQUE)
→ delivery outbox
→ 사용자 투영
```

`DoneEvent.mode`, AnswerRecord.mode, retrieve의 Answered.mode는 같은 최종 Answered 값에서 파생한다. SSE `done`은 `request_id`와 `record_id`를 포함한다. 스트림이 중간에 끊겨 terminal 확정에 도달하지 못하면 Answered로 기록하지 않는다.

전이와 기록은 계속 분리한다. Question Request 상태 변경은 도메인 전이이고, AnswerRecord·Audit·SessionTurn은 기록이다. 다만 durable 구현은 한 terminalization 트랜잭션으로 원자성을 보장한다.

### 7. 사용자 표면은 수명주기를 정직하게 드러낸다

사용자 결과는 다음 네 종류다.

- `Answered(request_id, record_id, ...)`
- `Pending(request_id, kind, message)`
- `Declined(request_id, message)`
- `Failed(request_id, message)`

request ID의 불투명성만 권한으로 믿지 않는다. 조회 시 저장된 requester principal과 현재 신원을 대조한다. MCP와 다른 채널도 같은 중앙 Application Service를 사용해야 하며, 독립 데모 상태를 만들지 않는다.

### 8. 영속성 경계

결정론 테스트와 단일 프로세스 개발에는 InMemory Store를 허용한다. 단일 인스턴스 통제 파일럿은 SQLite와 terminalization Unit of Work를 사용할 수 있다. 다중 인스턴스 production은 Postgres·lease·outbox 소비자·복구 절차가 준비된 뒤 연다.

SQLite 최소 스키마는 Question Request, WorkTicket, ConflictCase, ManagerItem, AnswerRecord, request audit, SessionTurn, delivery outbox의 request 상관키와 유일 제약을 포함한다. 각 Store가 독립 연결에서 따로 commit하는 조합은 terminal 원자성을 보장하지 못하므로 전용 Unit of Work를 둔다.

P17.9 S4.5는 durable WorkTicket enqueue Unit of Work를 추가한다(`sqlite_durable_work_ticket_uow.py`·S4.1 schema 재사용·신 DDL 0). 한 `ReadyToDispatch`를 소비해 `durable_linked_work_tickets`(status=pending) 한 행·`work_ticket.create` receipt·본문 없는 audit/outbox intent를 쓰고 Request를 `ReadyToDispatch → AwaitingAnswer(ticket_id)`로 CAS 전이하는 것을 한 SQLite transaction으로 원자화한다. 전이와 기록은 개념상 분리하되 한 transaction으로 commit한다(§6과 같은 결). receipt의 `principal_id`는 사람이 아니라 system subject 상수, `target_ref`는 `ticket_id` 그대로다(재hash 금지 — S4.4 교정 계승). 같은 합성 명령 재시도는 저장 receipt로 멱등 반환하고, 임의 fault에서 세 write(ticket·receipt graph·Request)의 부분 쓰기가 0이다. lease/claim/send/timeout scheduler·external delivery·FromDispatch 처분은 S5까지 열지 않는다.

### 9. P17.9 S5 — durable dispatch lease·전달·답 수신 경계 (2026-07-25 보강)

§5가 “durable startup recovery와 `(request_id, attempt)` lease는 P17.9에서 완성한다”로, §8이 “lease/claim/send/timeout scheduler·external delivery·FromDispatch 처분은 S5까지 열지 않는다”로 이월한 것을 S5가 연다. 여기에는 되돌리기 어려운 새 제품 결정이 없다 — 아래는 전부 §5·§8이 이미 선언한 계약의 정밀화다. 유일한 새 제품 결정(**한 Question Request가 사람 처분 큐에 두 번 이상 들어갈 수 있는가** = S5.6 FromDispatch escalation)은 ADR 0066으로 분리했고, S5.1~S5.5는 그 결정에 의존하지 않는다.

- **① 소유 경계 — S5는 자기 component를 신설하고 S4.1 DDL을 바꾸지 않는다.** S4.1 `durable_linked_aggregates_v1`은 manifest가 DDL catalog(테이블·컬럼·FK)를 exact 봉인하고 v1→v2 경로가 없으므로, lease 컬럼 추가나 `durable_linked_outbox_intents`에 소비 표식을 다는 변경은 불가능하다. S5는 공유 `schema_component_manifests`에 **`durable_dispatch_delivery_v1`**(S5.1)을 등록하고 자기 테이블만 소유한다. **S5.6 escalation은 또 다른 component(`durable_dispatch_escalation_v1`)로 분리**한다 — 한 component의 manifest는 생성 시점에 봉인되므로, S5.6 테이블을 S5.1 manifest에 미리 넣으면 ADR 0066 판정 전에는 S5.1을 시작할 수 없기 때문이다. escalation 계열이 `..._baseline`/`..._receipts`로 나뉜 선례와 같다.
- **② S4.1과의 유일한 접점 — `durable_linked_work_tickets.status`의 두 번째 writer 계약.** S4.5는 이 테이블의 유일한 **INSERT**er(항상 `pending`)이고, S5는 유일한 **UPDATE**er다. S5가 바꾸는 컬럼은 `status` 하나뿐이며 전이는 `pending → completed`(S5.4 답 수신)와 `pending → escalated`(S5.6)만, 즉 단조·전방·terminal이다. 모든 status 갱신은 대응 S5 receipt·Request 전이와 **같은 transaction**에 있다. S4.1의 `_TICKET_STATUS`가 이미 세 값을 허용하고 S4.6 forward arm도 `status ∈ {pending, completed, escalated}`를 tolerant로 열어 뒀으므로 **S4.1·S4.6 소스 변경은 0**이다.
- **③ outbox 소관 — S4.1 outbox intent는 작업 큐가 아니다.** `durable_linked_outbox_intents`는 receipt와 1:1로 봉인된 **기록 mirror**이고(모든 필드가 receipt와 exact 일치해야 함) 소비 표식을 가질 DDL 여지가 없다. 따라서 S5는 그것을 소비하지 않고 `_OUTBOX_KIND`도 확장하지 않는다. **dispatch 작업 큐는 `durable_linked_work_tickets`의 `status='pending'` 행 자체**다 — S4.5가 Request 전이와 한 transaction에 쓴 그 행이 이미 “보낼 의도”의 durable 기록이므로 두 번째 진실 원천을 만들지 않는다. Completion이 쓰는 `question_delivery_outbox`(`answer_ready` 사용자 통지)는 S5 범위 밖이다.
- **④ lease 모델 — epoch 등호 fence, token 없음, tombstone 없음.** lease는 `durable_dispatch_leases`(ticket당 최대 1행)에 두고 fence는 `(ticket_id, lease_epoch, holder_ref)` 등호 + `state='leased'` + `expires_at > now`의 CAS다. **lease token(비밀)을 두지 않는다** — P18 reciprocal review lease가 token_hash를 둔 이유는 lease를 사람/AI 검토자라는 **외부** 보유자에게 넘기고 나중에 그 보유자가 스스로를 증명해야 하기 때문인데, dispatch lease의 보유자는 중앙 dispatcher 인스턴스 자신이고 외부 위임 대상이 0이다. token은 방어할 상대 없이 secret key 주입(`token_key`)만 부른다. **tombstone 테이블도 두지 않는다** — P18에서 tombstone은 만료 token의 epoch별 불변 기록이었고, token이 없으면 남길 것이 없다. epoch 이력은 append-only `durable_dispatch_delivery_attempts`(epoch당 최대 1행)가 대신 증명한다.
- **⑤ 만료 판정 주체와 TTL.** 만료는 **호출자가 인자로 주장할 수 없고** UoW가 `BEGIN IMMEDIATE` 스냅샷 안에서만 판정한다. 시간 원천은 UoW 생성자에 한 번 주입하는 `clock`이며(호출마다 시간 인자 0), 단일 인스턴스 SQLite에서 이는 P18 `DbTransactionTime`과 동치다. 다중 인스턴스(S6 Postgres)에서는 이 **생성자 한 지점**을 DB-time 어댑터로 교체하는 것으로 족하다. `lease_ttl`도 생성자 주입(운영 정책이지 명령 인자가 아니다)이고 `0 < ttl <= 1일`이다. TTL이 실제 답 지연보다 짧으면 중복 전달이 늘어날 뿐이며, 미아 없음·terminal 단일성 같은 정확성은 TTL 값에 의존하지 않는다.
- **⑥ 전달 의미론 — at-least-once, 물리적 exactly-once 불주장.** 외부 전송은 transaction 밖에서 일어나므로 순서는 **claim(TX-A) → 전송(외부) → 시도 기록+lease 갱신(TX-B)**이다. 전송 뒤 TX-B 전에 죽으면 시도 기록이 없고 lease 만료 뒤 `epoch+1`이 재전달한다 — 워커는 같은 `ticket_id`를 두 번 받을 수 있다. **중복 흡수 주체는 두 겹**이다: 워커 상태기계가 `ticket_id` 기준 멱등이어야 하고(계약), 중앙은 S5.4의 ticket status CAS(`pending`에서만 성공)와 Completion receipt 멱등이 두 번째 답을 흡수한다. §5의 “외부 LLM 계산 자체를 물리적으로 exactly-once라고 주장하지 않는다”를 전달 채널에 그대로 확장한다. 전달 멱등 앵커는 **`(ticket_id, lease_epoch)`**다 — epoch당 시도 행 하나. 전달 실패(`undeliverable`)는 같은 transaction에서 lease를 release해 다음 scan이 즉시 `epoch+1`로 재시도하게 한다.
- **⑦ owner 결박 — `owner_subject_id`는 주소가 아니라 fence다.** S4.5가 저장한 `owner_subject_id`는 `subject:<sha256>`이라 역산할 수 없으므로 워커 주소를 이 컬럼에서 “해석”할 수 없다. 실제 주소는 전달 시점에 Request의 `AwaitingAnswer.route.agent_id`를 현재 Registry로 조회해 얻고, 그 Owner의 subject ref가 저장값과 **같은지 대조**한다. 다르면 enqueue 이후 카드 소유권이 바뀐 것이므로 새 Owner에게 조용히 보내지 않고 `owner_drift`로 fail-closed 처리한다 — §8이 “under-claim/route-rule drift는 S5 timeout/reroute 몫”이라 한 그 루프를 여기서 닫는다.
- **⑧ 답 수신의 종착 매핑 — S5.4는 `Answered` 하나만 durable하게 닫는다.** `AwaitingAnswer`의 네 종착 중 **`AnsweredRequest`**만 S5.4가 소유한다(lease release + ticket `completed` + S5 receipt + `complete_in_transaction`을 한 transaction). **`AwaitingApproval`**은 `ApprovalBoundary`가 ApprovalItem 생성과 함께 소유하므로 S5가 그 전이를 만들지 않는다 — 승인이 필요한 후보는 S5.4가 typed conflict로 fail-closed 거부하고, 기존 in-process 승인 경로가 그대로 종착을 책임진다(회귀 0·ticket은 `pending`으로 남고 S4.6은 이를 tolerant로 통과). **`AwaitingManager`**는 S5.6이다. **`FailedRequest`는 S5의 어떤 경로도 쓰지 않는다** — §2의 “일시 장애는 곧바로 Failed로 닫지 않는다”를 채널 영구 실패까지 확장한다. 워커가 영구 미연결이거나 전달이 반복 실패해도 결과는 Failed가 아니라 **사람에게 닿는 escalation**이다. Failed는 도메인적으로 복구 불가이면서 사람 처분으로도 진행할 수 없을 때만 쓰며, S5 범위에는 그런 경우가 없다.
- **⑨ SLA와 lease는 다른 시계다.** lease 만료는 “이 전달을 맡은 중앙 인스턴스가 멈췄다 → 재전달”이고, `AwaitingAnswer.handling.due_at`(§2의 도메인 SLA)은 “이 실행 시도가 너무 오래 걸렸다 → 사람에게”다. S5.5 timeout scan의 판별자는 **후자**이며, 전자는 S5.2 reclaim이 처리한다. scan은 read-only(write·cursor·scheduler 0)이고 후보 조건은 `ticket.status='pending'` ∧ Request가 그 ticket에 결박된 `AwaitingAnswer` ∧ `revision == ticket.awaiting_revision + 1` ∧ `handling.due_at <= now`다.
- **⑩ 두 층의 claim은 통합하지 않는다.** `transport.py`의 process-local `queued↔claimed↔answered↔expired`는 **한 인스턴스 안에서 워커 소켓을 가르는 전송 detail**이고, durable dispatch lease는 **어느 중앙 인스턴스가 그 durable ticket을 전달하는가**를 가르는 fence다. 재전달 여부의 권위는 언제나 durable lease + ticket status이며, in-memory 큐의 claim/expire를 durable 상태로 읽지 않는다. 둘을 한 상태기계로 합치는 것(=in-memory 큐를 durable 큐로 교체)은 실 WS 전송 슬라이스(S5.8)와 S6의 몫이다.
- **⑪ recovery runner 소유권.** `request_recovery.RequestRecoveryRunner`는 `store.nonterminal()`을 훑어 `Received`·`ReadyToDispatch`만 재개하는 Request-anchored coordinator이고, 그 `recover_ready` hook이 S4.5 enqueue의 자리다. S5의 소비자는 스캔 정의역(`pending` ticket × lease 상태)과 저장 접근(durable SQL)이 모두 달라 **별 runner로 신설**한다. 둘은 상태 정의역이 겹치지 않는다(`ReadyToDispatch` vs `AwaitingAnswer`).

이 §9은 SQLite 단일 인스턴스 범위다. PostgreSQL·실 다중 인스턴스·실 WS 전송은 계속 §8의 경계 밖(S5.8·S6)이며, **S5.5까지 랜딩한 시점에 “S5로 미아 없음이 닫혔다”고 주장하지 않는다** — owner 영구 부재 경로의 종착은 S5.6이 닫아야 완성된다.

## 기각한 대안

- **기존 `_tracking` dict를 세 분기에 확대** — 재시작·다중 인스턴스·사용자 소유권·terminal 원자성을 해결하지 못한다.
- **WorkTicket을 Question Request로 승격** — 재지정·재시도 한 질문에 여러 Ticket이 필요해 책임이 맞지 않는다.
- **해소 뒤 Router를 처음부터 다시 호출** — 사람의 확정 결정을 잃거나 다른 비결정 결과가 나올 수 있다. 검증된 RouteTarget으로 재개해야 한다.
- **intent당 ConflictCase 하나에 request_id 하나만 추가** — 두 번째 이후 원 질문 손실이 그대로다.
- **Contested 상태에서 사전순 primary로 즉시 답변** — 책임 확정 전 단수 책임자를 표시하고 Approval도 우회한다.
- **stream 경로에 `_record_answer()`만 추가** — 당장 한 누락은 고치지만 request 상관키·retrieve Approval·Session·terminal 멱등성 문제를 남긴다. 공통 finalizer로 수렴시킨다.
- **(S5) S4.1 `durable_linked_outbox_intents`를 dispatch 작업 큐로 소비** — 소비 표식을 달 DDL 여지가 없고(manifest exact 봉인), 그 행은 receipt와 필드가 exact 일치해야 하는 기록 mirror다. ticket 행 자체가 이미 원자적으로 쓰인 전달 의도이므로 두 번째 진실 원천을 만들지 않는다.
- **(S5) dispatch lease에 P18식 lease token·tombstone 도입** — 보유자가 중앙 인스턴스 자신이라 외부 위임 대상이 0이고, token은 방어 대상 없이 secret key 주입만 부른다. epoch 등호 fence + append-only 시도 기록으로 같은 성질을 얻는다.
- **(S5) `worker.claim` 중앙 action 신설** — dispatch는 사람 명령이 아니라 §5·§8의 system 전이이고, 신설하면 `AUTHORITY_ACTION_MANIFEST`·role 매핑·resolver가 연쇄로 열린다. 워커 admission은 ADR 0026/0050 §5가 이미 별 층에서 닫았다.
- **(S5) 답 수신 UoW가 `AwaitingApproval` 전이까지 소유** — ApprovalItem 생성은 ApprovalStore/ApprovalBoundary 소유라, UoW가 Item ID를 발명하면 `draft_ref`가 어떤 Item과도 맞지 않는다. 승인 필요 후보는 fail-closed 거부하고 기존 승인 경계에 맡긴다.
- **(S5) 채널 영구 실패를 `FailedRequest`로 닫기** — 사용자에게 “실패”만 남기고 재배정 가능성을 버린다. §2의 재시도 가능 상태 원칙에 따라 사람 처분(escalation)으로 보낸다.

## 결과와 이행 순서

1. P17.1 — 기본 앱의 Manager 큐 공유 배선 복구. 이는 누락 hotfix이며 P0 완결이 아니다.
2. P17.2a~b — QuestionRequest 상태·Store와 SQLite 복구 경계.
3. P17.2c-1 — Request 상관키와 Request-first Application Service intake·소유권 검증.
4. P17.6a — 최소 ApprovalDraft·ApprovalItem·승인/수정 승인·반려 경계. 승인 대기 초안을 terminal로 기록하지 않도록 Finalization보다 먼저 구현한다.
5. P17.3a~c — blocking·retrieve·SSE의 공통 Finalization과 SQLite UoW·outbox.
6. P17.2c-2 — blocking·SSE·retrieve·MCP 전 표면 전환.
7. P17.4~P17.5 — Unowned·Contested 처분 뒤 원 질문 재개/Declined 수직 슬라이스. 두 단계 모두 완료했다.
8. P17.6b — ADR 0048의 승인 운영·새 ApprovalItem 세대 재지정·보존 eligibility를 전 채널에 연결한다.
9. production composition·Authority/RBAC·전체 durable workflow·contextual Precedent를 뒤따르게 한다.

## 불변식 자체점검

- **사용자 결과 기준 미아 없음 — 강화.** 큐 적재가 아니라 조회 가능한 request 수명과 terminal 결과로 정의한다.
- **등록 무결성 — 보존.** 재개 대상 Agent Card도 Registry admission을 통과한 현재 카드만 허용한다.
- **Authority 중앙 — 강화.** 합의·Manager 처분·재디스패치 모두 중앙 AuthorityPolicy를 다시 검증한다.
- **전이 ≠ 기록 — 보존.** Question Request 전이와 AnswerRecord/Audit를 개념상 분리하되 terminalization은 원자적으로 commit한다.
- **노출 불변식 — 보존.** 사용자에게 request_id·상태·담당·검토 상태·근거만 보이고 후보 점수·manager_id·내부 Ticket은 숨긴다.
- **(§9 S5) 미아 없음 — 보존, 아직 미완결.** 전달 실패·lease 만료·owner drift는 전부 재시도 가능한 상태로 수렴하고 어느 경로도 `FailedRequest`를 쓰지 않는다. 다만 owner 영구 부재의 종착은 S5.6이 닫아야 하므로 S5.5 시점에 “닫혔다”고 주장하지 않는다. **Authority 중앙 — 보존.** dispatch는 §5·§8의 system 전이라 중앙 재인가가 없고 route 봉인이 이미 인가 증거이며, 답 수신은 기존 `worker.submit` 결박과 durable `owner_subject_id` 대조를 함께 요구한다. **전이 ≠ 기록 — 보존.** Request 전이·ticket status는 도메인, delivery 시도·answer receipt는 기록이며 한 transaction으로 commit한다.
