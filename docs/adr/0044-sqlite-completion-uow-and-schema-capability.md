# ADR 0044 — SQLite Completion UoW와 component schema capability

- 상태: 채택(Accepted)
- 날짜: 2026-07-13
- 계보: ADR 0042의 terminalization 원자성과 ADR 0043의 공통 Answer Finalization을 SQLite 단일 인스턴스 파일럿 경계로 구체화한다.
- 구현 상태: P17.3c 구현 완료·독립 재리뷰 APPROVE(2026-07-13), P17.2c-2에서 전체 사용자 표면과 감독 read view 조립까지 연결했다. 통제된 단일 애플리케이션 SQLite 파일럿 범위이며, Approval transaction·durable linked workflow·outbox 소비/lease·다중 인스턴스 production은 포함하지 않는다.

## 맥락

P17.3a의 `InMemoryQuestionCompletionUnitOfWork`는 AnswerRecord, `AnsweredRequest`, Terminal Answer Audit, request-correlated SessionTurn, Delivery Outbox를 한 lock 아래 확정한다. 프로세스가 끝나면 결과도 사라진다. 기존 SQLite Store들을 차례로 호출해서 이 문제를 풀 수는 없다. 각 Store가 별도 연결에서 commit하므로 중간 장애 때 일부 행만 남고, 공개 Request CAS로 `AnsweredRequest`를 먼저 쓸 수도 있다.

기존 SQLite 스키마에도 그대로 재사용할 수 없는 지점이 있다.

- `answer_records`는 `sources`와 `snapshot_sha`를 저장하지 못한다. 새 테이블을 하나 더 만들면 AnswerRecord의 진실 원천이 둘이 된다.
- `session_turns`는 활성 대화의 투영이다. 세션 종료·유휴 만료 때 행을 지우는 것이 현재 계약이므로, 삭제할 수 없는 terminal 기록으로 쓸 수 없다.
- 각 Store 생성자가 스키마를 자동 변경한다. 애플리케이션 시작과 migration이 섞이면 일부 DDL만 적용된 파일을 정상 파일로 오인할 수 있다.
- SQLite의 전역 `PRAGMA user_version` 하나로는 여러 저장 component의 독립적인 스키마 capability를 표현할 수 없다.
- 동일 후보 재시도와 다른 후보 경쟁을 재시작 뒤에도 구분하려면 최종 AnswerRecord만으로는 부족하다. Finalization handoff의 exact equality를 복원할 증거가 필요하다.

P17.3c는 terminal completion만 내구화한다. ApprovalItem을 만들고 Request를 `AwaitingApproval`로 옮기는 pre-terminal transaction, ConflictCase·ManagerItem·WorkTicket, outbox 소비 lease는 P17.9 책임으로 남긴다.

## 결정

### 1. 한 SQLite 객체가 세 포트를 함께 구현한다

`SqliteQuestionCompletionUnitOfWork` 하나가 다음 포트를 동시에 구현한다.

```text
QuestionRequestStore
QuestionCompletionUnitOfWork
QuestionCompletionReader
```

객체는 private SQLite connection 하나와 `RLock` 하나를 소유한다. 연결은 `check_same_thread=False`, `row_factory=sqlite3.Row`, `PRAGMA foreign_keys=ON`으로 열고 외부에 노출하지 않는다. Request의 `create`, `compare_and_set`과 completion의 `complete`는 같은 connection을 쓴다. 공개 `compare_and_set`은 새 상태가 `AnsweredRequest`이면 SQL 실행 전에 `DirectAnsweredTransitionError`로 거부한다. Answered 전이는 `complete`만 쓸 수 있다.

`complete`는 `BEGIN IMMEDIATE`부터 commit 또는 rollback까지 한 connection에서 실행한다. 같은 객체의 스레드 경쟁은 `RLock`, 서로 다른 connection의 파일 경쟁은 SQLite write lock과 Request revision·request당 UNIQUE 제약으로 직렬화한다. 이 보장은 단일 SQLite 파일을 한 애플리케이션 인스턴스가 소유하는 통제 파일럿 범위다. 독립 connection 경쟁 테스트는 잠금·멱등성을 검증할 뿐 다중 인스턴스 production 승인을 뜻하지 않는다.

`RLock`의 재진입 허용을 쓰기 중첩 허용으로 해석하지 않는다. planner가 호출하는 policy, Approval Store, responsibility resolver, record ID factory, clock callback 안에서 같은 UoW의 `create`·`compare_and_set`·`complete`에 재진입하면 `ReentrantCompletionMutationError`로 거부한다. 조회는 허용하되 현재 transaction의 snapshot만 보게 한다.

production-style 조립은 Request Store, completion UoW, Completion Reader에 같은 객체 인스턴스를 넣어야 한다.

```text
requests is completion is reader
```

Question Resolution, ApprovalBoundary, stream execution, terminal broker도 이 인스턴스를 공유한다. 같은 경로 문자열로 연 서로 다른 Store는 통과하지 못한다. 이 identity gate는 별도 연결의 공개 CAS가 Answered 우회 경로로 남거나, writer와 reader가 서로 다른 schema capability를 보는 구성을 막는다.

P17.2c-2의 감독 조회는 `atomic_v1` marker를 넓히지 않는다. marker는 계속 `create/get/compare_and_set/nonterminal/complete/by_request/by_record` 일곱 callable만 뜻한다. `answer_record(record_id)`와 `answer_records_for_agent(agent_id)`는 별도 supervision read capability이며, completion artifact를 exact-read해 AnswerRecord만 투영한다. legacy AnswerRecord Store와의 합성 view는 쓰기 미러가 아니라 read-only adapter다.

### 2. 공통 Finalization planner를 먼저 분리한다

InMemory와 SQLite 구현이 승인·책임·시각·artifact 규칙을 각자 복제하지 않는다. `answer_finalization.py`에 다음 공통 경계를 둔다.

```text
canonical_completion_handoff(raw) -> CompletionHandoff

QuestionCompletionPlanner.plan(
    request,
    canonical_handoff,
) -> CompletionPlan(
    handoff,
    expected_request,
    bundle,
)
```

planner는 현재 `ApprovalPolicy`, resolved `ApprovalItem`, responsibility resolver, record ID factory, clock을 사용해 P17.3a와 같은 검증을 수행한다. 그 결과로만 `CompletionBundle`을 만든다. 두 UoW는 canonicalization과 planning 결과를 그대로 쓰고 persistence·동시성만 각각 맡는다.

이미 receipt가 있는 재시도는 planner보다 먼저 처리한다. 저장된 canonical handoff가 입력과 같으면 policy·clock·ID factory를 다시 호출하지 않고 기존 completion을 반환한다. 다르면 `CompletionConcurrencyError`다. 새 completion에만 planner를 호출한다. 이 순서가 재시작 뒤 멱등성과 P17.3a의 callback 의미를 함께 보존한다.

planner 호출은 SQLite `BEGIN IMMEDIATE` 안에서 최신 Request를 읽은 뒤 이뤄진다. 단일 인스턴스 파일럿에서는 짧은 policy·responsibility 조회 동안 write lock을 유지한다. 외부 네트워크를 호출하는 resolver는 이 경계에 넣지 않는다. 장시간·분산 검증과 lease는 P17.9에서 transaction 밖 snapshot과 재검증 단계로 다시 설계한다.

### 3. `answer_records`를 제자리에서 v2로 올린다

두 번째 answer 테이블을 만들지 않는다. 기존 `answer_records`에 아래 두 열을 순서대로 추가한다.

```sql
ALTER TABLE answer_records ADD COLUMN sources_json TEXT;
ALTER TABLE answer_records ADD COLUMN snapshot_sha TEXT;

CREATE UNIQUE INDEX ux_answer_records_request_id_v2
    ON answer_records(request_id COLLATE BINARY)
    WHERE request_id IS NOT NULL;
```

`sources_json`은 legacy 행을 보존하려고 nullable로 둔다. 기존 행은 두 새 열 모두 `NULL`인 채로 남기며 질문·시각 유사도나 빈 배열을 추정해서 backfill하지 않는다. v2 UoW가 쓰는 request-aware 행은 출처가 없어도 canonical JSON `[]`을 반드시 저장한다. `snapshot_sha`는 도메인과 같이 nullable이다.

기존 `record_id TEXT PRIMARY KEY`도 table rebuild 없이 유지한다. SQLite rowid table의 이 선언은 raw SQL에서 `NULL`을 완전히 막지 못하므로 v2 writer와 reader가 nonblank exact ID를 별도로 검증한다. migration은 기존 행을 고쳐 이 차이를 감추지 않는다.

`request_id IS NOT NULL` partial UNIQUE가 legacy `NULL` 행을 그대로 두면서 새 경로의 request당 AnswerRecord 한 건을 보장한다. index를 만들기 전에 non-null request ID 중복을 검사한다. 중복이 하나라도 있으면 임의 행을 삭제·병합하지 않고 migration을 중단한다.

v2가 설치된 뒤 `SqliteAnswerRecordStore.add`는 `request_id`가 있는 직접 write를 명시적으로 거부한다. request-aware 기록은 `SqliteQuestionCompletionUnitOfWork.complete`만 쓴다. `request_id=None`인 legacy append는 sources가 비어 있고 snapshot SHA가 없을 때만 호환 경로로 허용한다. 기존 `INSERT OR IGNORE`를 request-aware 기록에 적용해 payload 충돌을 숨기는 일은 없다.

### 4. completion 기록은 활성 transcript와 분리한다

`session_turns`는 계속 활성 Session transcript의 투영으로 쓴다. P17.3c는 이 테이블에 쓰지 않는다. 대신 삭제하지 않는 `request_session_turns`를 만든다.

Question Request에 `session_id`가 없으면 행을 만들지 않는다. 값이 있으면 활성 Session 존재 여부와 무관하게 request당 한 행을 남긴다. `sessions`에는 foreign key를 걸지 않는다. 세션 종료 뒤에도 terminal 증거가 남아야 하기 때문이다. 활성 transcript 갱신은 후속 projector가 맡는다.

### 5. schema capability는 component별 manifest로 증명한다

전역 `PRAGMA user_version`은 읽지도 쓰지도 않는다. 공유 capability 테이블은 component별 행을 가진다.

```sql
CREATE TABLE schema_component_manifests (
    component_id      TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    schema_version    INTEGER NOT NULL,
    manifest_json     TEXT NOT NULL,
    manifest_sha256   TEXT NOT NULL
);
```

P17.3c component ID는 `question_completion`, component schema version은 `1`이다. `answer_records`의 논리 버전 `2`와 component version `1`은 서로 다른 번호다.

manifest JSON은 다음 내용을 key 정렬·공백 없는 UTF-8 JSON으로 저장한다.

- component ID와 version
- component가 소유·검증하는 table·index 이름
- 각 객체의 정규화된 column affinity, nullability, PK 순서, foreign key, index key·collation·partial predicate
- persistent trigger/view allowlist. v1은 empty다.
- `question_requests` state schema v1, `answer_records` v2, handoff JSON schema v1

`manifest_sha256`은 canonical `manifest_json` UTF-8 바이트의 소문자 SHA-256 hex다. runtime은 row의 digest만 믿지 않고 SQLite catalog에서 같은 정규 manifest를 다시 계산해 저장값·내장 기대값과 모두 비교한다. raw `CREATE TABLE` 문자열의 공백 차이를 hash하지 않는다. `PRAGMA table_xinfo`, `foreign_key_list`, `index_xinfo`와 정규화한 partial predicate를 사용해 in-place migration과 fresh create가 같은 capability를 내도록 한다.

runtime constructor는 validate-only다. manifest가 없다고 DDL을 만들거나 열을 보충하지 않는다. schema migration은 별도 명령 또는 함수가 명시적으로 수행한다. 애플리케이션이 시작된 뒤의 out-of-band DDL·직접 SQL write는 지원하지 않으며, 파일을 단일 애플리케이션이 소유한다는 파일럿 전제를 어기면 보장도 끝난다.

### 6. migration 상태를 좁게 인정한다

전용 migrator는 `PRAGMA foreign_keys=ON`을 확인한 뒤 `BEGIN IMMEDIATE` 안에서 schema 검사, DDL, manifest insert를 모두 수행한다. DDL도 같은 transaction에 있으므로 실패하면 원래 상태로 rollback한다.

인정하는 시작 상태는 세 가지뿐이다.

1. **Fresh** — `question_completion` manifest와 completion-native table이 없다. `question_requests`·`answer_records`는 둘 다 없거나, 있으면 각각 exact legacy v1 shape다. 없는 legacy table은 migrator가 만든다.
2. **Legacy v1** — manifest와 completion-native table이 없고, `question_requests` v1과 `answer_records` correlation v1이 exact shape다. `answer_records`에 새 열을 추가하고 v2 partial UNIQUE를 만든다. 기존 행 값은 바꾸지 않는다.
3. **Capable v1** — `question_completion/version 1` manifest, digest, catalog shape가 모두 일치한다. migrator는 DDL 없이 validate-only로 끝난다.

다음 상태는 자동 복구하지 않는다.

- manifest 없이 completion-native table이나 v2 열·index 일부만 존재함
- manifest version·digest·catalog shape가 다름
- legacy table에 알 수 없는 UNIQUE, trigger, view, foreign key, generated/NOT NULL 추가 열이 있음
- partial UNIQUE를 만들 수 없는 중복 request ID가 있음
- `AnsweredRequest`가 있지만 receipt와 terminal artifact가 완전하지 않음

부분 적용 흔적을 “거의 완료”로 채택하지 않는다. 운영자가 원인을 확인한 뒤 검증된 migration을 다시 수행해야 한다. 기존 `session_turns`와 그 행은 migration 대상이 아니다.

### 7. exact schema

#### `answer_records` v2

| 열 | 선언 | 의미 |
|---|---|---|
| `record_id` | `TEXT PRIMARY KEY` | 기존 PK, v2에서 nonblank를 추가 검증 |
| `question` | `TEXT NOT NULL` | 질문 원문 |
| `answer_text` | `TEXT NOT NULL` | 확정 답 |
| `answered_by` | `TEXT NOT NULL` | Owner User ID |
| `agent_id` | `TEXT NOT NULL` | 책임 Agent Card ID |
| `mode` | `TEXT NOT NULL` | 최종 AnswerMode |
| `session_id` | `TEXT` | 입력 session 상관키 |
| `answered_at` | `TEXT NOT NULL` | timezone-aware ISO8601 |
| `needs_correction_review` | `INTEGER NOT NULL DEFAULT 0` | 기존 사후교정 flag |
| `request_id` | `TEXT` | legacy nullable 상관키 |
| `sources_json` | `TEXT` | v2 request-aware 행은 canonical JSON array 필수 |
| `snapshot_sha` | `TEXT` | 선택적 지식 snapshot SHA |

허용 index는 기존 non-unique `idx_answer_records_agent(agent_id)`와 partial unique `ux_answer_records_request_id_v2(request_id COLLATE BINARY) WHERE request_id IS NOT NULL`이다. 이 테이블은 in-place 제약 때문에 `question_requests` foreign key를 추가하지 않는다. UoW와 receipt·reader가 exact-link를 검증한다.

#### `terminal_answer_audits`

```sql
CREATE TABLE terminal_answer_audits (
    request_id            TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    record_id             TEXT NOT NULL UNIQUE COLLATE BINARY,
    org_id                TEXT NOT NULL,
    requester_id          TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    route_json            TEXT NOT NULL,
    responsibility_json   TEXT NOT NULL,
    candidate_mode        TEXT NOT NULL,
    final_mode            TEXT NOT NULL,
    approval_json         TEXT NOT NULL,
    completed_at          TEXT NOT NULL,
    audit_schema_version  INTEGER NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (record_id) REFERENCES answer_records(record_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
```

세 JSON 열은 각각 `RouteTarget`, `AnswerResponsibilitySnapshot`, `ApprovalEvidence`의 canonical schema v1 표현이다. request와 record가 모두 unique하므로 별도 조회 index를 만들지 않는다.

#### `request_session_turns`

```sql
CREATE TABLE request_session_turns (
    request_id    TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    record_id     TEXT NOT NULL UNIQUE COLLATE BINARY,
    session_id    TEXT NOT NULL COLLATE BINARY,
    question      TEXT NOT NULL,
    answer_text   TEXT NOT NULL,
    answered_by   TEXT NOT NULL,
    at            TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (record_id) REFERENCES answer_records(record_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE INDEX idx_request_session_turns_session_at
    ON request_session_turns(session_id, at, request_id);
```

`answered_by`는 현재 `SessionTurn` 계약대로 책임 Agent Card ID다. Owner User ID는 AnswerRecord와 Terminal Answer Audit에 따로 남는다.

#### `question_delivery_outbox`

```sql
CREATE TABLE question_delivery_outbox (
    request_id   TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    record_id    TEXT NOT NULL UNIQUE COLLATE BINARY,
    kind         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (record_id) REFERENCES answer_records(record_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE INDEX idx_question_delivery_outbox_created
    ON question_delivery_outbox(created_at, request_id);
```

P17.3c의 `kind`는 `answer_ready` 하나다. 이 표는 전달할 canonical 사건을 보존한다. publish 상태·attempt·lease·dead letter는 P17.9에서 별도 dispatcher state 또는 검토된 schema version으로 추가한다. 그 전에는 행을 소비 완료로 표시하거나 삭제하지 않는다.

#### `question_completion_receipts`

```sql
CREATE TABLE question_completion_receipts (
    request_id              TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
    record_id               TEXT NOT NULL UNIQUE COLLATE BINARY,
    handoff_kind            TEXT NOT NULL,
    handoff_json            TEXT NOT NULL,
    handoff_sha256          TEXT NOT NULL,
    handoff_schema_version  INTEGER NOT NULL,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES question_requests(request_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (record_id) REFERENCES answer_records(record_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
```

receipt에는 digest만 두지 않고 `FinalizationCandidate | ApprovedCandidate` 전체 canonical JSON과 SHA-256을 함께 둔다. 같은 후보인지 재시작 뒤 exact 비교하려면 원문이 필요하고, JSON과 digest를 함께 두면 우발적 손상을 읽을 때 바로 잡을 수 있다. handoff에는 후보 답이 들어가므로 AnswerRecord와 일부 바이트가 중복되지만, 멱등성 증거를 복원할 수 있다는 이점이 더 크다.

`handoff_kind`는 `finalization_candidate | approved_candidate`, `handoff_schema_version`은 `1`만 허용한다. `created_at`은 같은 completion의 `completed_at`과 같아야 한다.

receipt는 두 번째 AnswerRecord가 아니다. 사용자 답 투영과 CompletionBundle은 `answer_records` 및 나머지 정규 table에서 복원한다. receipt의 handoff는 멱등성·승인 증거 대조에만 쓰고, 둘이 다르면 저장된 답을 receipt 값으로 덮어쓰지 않고 손상으로 처리한다. SHA-256은 우발적 손상 탐지 수단이지 악의적인 DB writer에 대한 서명은 아니다.

### 8. transaction 순서

새 completion은 다음 순서로 처리한다.

```text
canonical handoff
→ BEGIN IMMEDIATE
→ Question Request exact-read
→ 기존 receipt 확인과 idempotency 판정
→ 공통 QuestionCompletionPlanner
→ AnswerRecord v2 INSERT
→ Question Request revision CAS로 AnsweredRequest UPDATE
→ Terminal Answer Audit INSERT
→ request_session_turns INSERT (session_id가 있을 때만)
→ question_delivery_outbox INSERT
→ question_completion_receipts INSERT (commit marker)
→ 같은 transaction 안에서 CompletionBundle exact-read·plan과 비교
→ COMMIT
```

receipt는 마지막 insert다. 어느 단계에서든 오류가 나면 rollback하고 Request revision과 모든 artifact가 원래 상태여야 한다. Session이 없는 Request에서도 fault injection 위치는 유지하되 실제 turn 행은 만들지 않는다.

`INSERT OR IGNORE`와 `INSERT OR REPLACE`는 쓰지 않는다. 모든 insert는 충돌 원인을 읽어 exact하게 분류한다.

- 같은 request의 receipt handoff가 입력과 같음: 기존 completion 반환
- 같은 request의 receipt handoff가 다름: `CompletionConcurrencyError`
- 새 record ID가 다른 request 또는 legacy record와 충돌: `CompletionIdCollisionError`
- `AnsweredRequest`인데 receipt나 artifact가 없음: `IncompleteCompletionStateError`
- nonterminal Request에 completion-native 잔여 행이 있음: `IncompleteCompletionStateError`
- handoff와 현재 revision·route·attempt·Approval 증거가 다름: 기존 `CompletionEvidenceError`
- lock timeout·I/O 실패: domain 경쟁으로 오인하지 않고 storage unavailable 오류
- 알 수 없는 constraint·schema drift: rollback 뒤 capability/corruption 오류

### 9. reader는 모든 artifact를 함께 검증한다

`by_request`와 `by_record`는 한 read transaction에서 snapshot을 잡고 strict hydrate한다. 성공 조건은 다음과 같다.

- Request가 `AnsweredRequest`이고 state의 `record_id`가 receipt와 같다.
- request·record별 receipt, AnswerRecord, Terminal Answer Audit, Delivery Outbox가 각각 정확히 한 건이다.
- Request에 `session_id`가 있으면 request-correlated turn이 정확히 한 건이고 값이 같아야 한다. 없으면 turn도 없어야 한다.
- AnswerRecord의 `sources_json`은 canonical JSON array이며 빈 문자열 출처가 없다. request-aware v2 행에서 `NULL`은 손상이다.
- route·responsibility·approval JSON은 중복 key·NaN 같은 비표준 값을 거부하고 strict model로 hydrate한다.
- handoff digest를 재계산하고 `handoff_kind`, schema version, JSON discriminator, request/route/attempt/candidate·Approval evidence를 audit·record와 exact 비교한다.
- Request `updated_at`, AnswerRecord `answered_at`, audit `completed_at`, outbox·receipt `created_at`이 같은 timezone-aware 시각이어야 한다. mode, Owner User, Agent Card, question, answer, sources, snapshot SHA도 `CompletionBundle` 검증을 통과해야 한다.
- canonical JSON을 다시 직렬화한 바이트가 저장값과 같아야 한다.

receipt가 없고 Request가 nonterminal이며 completion-native artifact도 없으면 `by_request`는 `None`이다. `AnsweredRequest`인데 receipt가 없거나, nonterminal인데 artifact가 하나라도 있으면 불완전 상태로 실패한다. `by_record`는 receipt의 `record_id`에서 시작한다. receipt가 없는 legacy AnswerRecord는 completion으로 승격하지 않는다.

runtime open 시 manifest와 `PRAGMA foreign_key_check`를 validate-only로 확인한다. 실행 중 raw SQL로 바꾼 데이터까지 매 호출마다 전체 스캔하지는 않는다. 대신 조회한 completion은 매번 위 검증을 거친다.

### 10. Approval·outbox·배포 경계

P17.3c는 이미 resolved된 ApprovalItem을 planner가 확인한 뒤 terminal completion을 commit할 수 있다. ApprovalItem 생성·resolve와 `AwaitingApproval` Request 전이는 아직 같은 SQLite transaction이 아니다. 프로세스가 그 사이에 끝나면 Approval 상태를 복원하지 못할 수 있다. 이 빈틈은 P17.9에서 durable Approval Store와 transaction 경계를 도입해 닫는다.

Delivery Outbox 행은 durable하지만 소비·lease·재시도는 아직 없다. P17.3b broker도 process-local 전송 장치다. 따라서 P17.3c가 끝나도 다중 인스턴스 production이나 exactly-once 외부 전달을 주장하지 않는다.

SQLite 파일은 한 애플리케이션 인스턴스가 소유하는 통제 파일럿까지만 허용한다. Postgres, workflow lease, backup/restore, outbox dispatcher, 관측성 게이트를 마치기 전에는 production profile을 열지 않는다.

## migration·구현 게이트

### schema와 migration

- fresh DB와 exact legacy v1 DB가 한 번의 명시 migration으로 같은 normalized manifest를 만든다.
- legacy `answer_records`의 모든 기존 열 값이 전후 동일하고, `sources_json`·`snapshot_sha`는 `NULL`이다. 추정 backfill은 0건이다.
- non-null request ID 중복, partial v2, unknown manifest version, digest 변조, column/index/FK/trigger/view drift를 모두 거부한다.
- DDL 각 단계와 manifest insert fault에서 전체 rollback된다. 재오픈하면 migration 전 상태이거나 Capable v1 둘 중 하나여야 한다.
- runtime constructor가 DDL을 실행하지 않고, manifest가 없으면 시작 실패한다.
- `PRAGMA user_version`을 migration 전후·runtime open에서 바꾸지 않는다.
- v2 이후 legacy AnswerRecord Store의 request-aware direct write는 거부되고 `request_id=None` 호환 write만 남는다.

### 공통 계약과 복원

- InMemory·SQLite가 같은 Finalization contract suite를 통과한다. 승인 불필요, 승인·수정승인, session 유무, sources 빈 값·복수 값, snapshot SHA를 모두 포함한다.
- close/reopen 뒤 `by_request`·`by_record`가 원래 `CompletionBundle`과 exact equality를 이룬다.
- 저장된 full handoff가 같은 재시도는 record ID·clock·policy callback을 다시 소비하지 않고 기존 completion을 돌려준다.
- AnswerRecord, Request CAS, audit, turn, outbox, receipt, pre-commit 각 fault point에서 부분 행과 Answered 상태가 남지 않는다.
- 각 artifact의 ID, JSON, digest, 시각, mode, attribution, sources를 하나씩 손상하거나 행을 누락·추가하면 reader가 fail-closed한다.
- planner callback에서 같은 UoW 쓰기에 재진입하면 부분 변경 없이 거부한다.

### 동시성·조립

- 서로 다른 connection 32개가 같은 handoff를 경쟁하면 record 한 건으로 수렴한다.
- 서로 다른 handoff 경쟁은 한 winner만 남고 나머지는 `CompletionConcurrencyError`다.
- record ID 충돌과 Request revision 경쟁을 일반 SQLite 오류로 노출하지 않고 도메인 오류로 분류한다.
- 동시성 시나리오를 반복 실행해 request당 AnswerRecord·audit·turn·outbox·receipt가 최대 한 건임을 확인한다.
- production-style identity gate가 같은 파일의 별도 Store 인스턴스, legacy `SqliteQuestionRequestStore`, 다른 Completion Reader 조합을 모두 거부한다.
- focused tests, 전체 `uv run pytest -q`, `uv run pyright`, `uv run ruff check .`, `git diff --check`가 통과하고 독립 code review 승인을 받아야 P17.3c를 완료로 표시한다.

## 구현 결과(2026-07-13)

- `SqliteQuestionCompletionUnitOfWork`가 Request Store·Completion UoW·Reader를 한 connection과 `RLock`으로 구현했다. 공개 CAS의 `AnsweredRequest` 우회와 callback 쓰기 재진입을 거부한다.
- 명시 migration은 fresh·exact legacy v1만 Capable v1으로 올리고, runtime open은 manifest·catalog·foreign key를 validate-only로 확인한다. `PRAGMA user_version`은 쓰지 않는다.
- 새 completion은 AnswerRecord v2 → Request CAS → terminal audit → 선택적 request SessionTurn → delivery outbox → full handoff receipt → 같은 transaction의 exact-read 순으로 기록한다. 일곱 fault 지점과 commit 전 오류는 모두 rollback된다.
- reader는 canonical JSON·digest·schema version·ID·시각·mode·책임·Approval·intent 링크를 다시 확인한다. receipt 없는 request-aware v2 흔적은 불완전 상태로 거부하고, migration 전 `sources_json=NULL`·`snapshot_sha=NULL`인 legacy 행만 completion이 아닌 기록으로 남긴다.
- production-style execution gate는 세 포트의 객체 identity뿐 아니라 `atomic_v1` marker, 일곱 필수 callable, durable marker를 확인한다. legacy `SqliteQuestionRequestStore`, 별도 Reader proxy, 같은 파일의 다른 인스턴스는 통과하지 못한다.
- 독립 재리뷰에서 최초 P1 두 건(legacy Store의 marker-only 우회, receipt 없는 v2 행 은폐)과 링크·오류 분류 누락을 수정한 뒤 APPROVE를 받았다. 관련 197개, 전체 3,569개 테스트와 32-connection 경쟁 50회 반복, pyright·ruff·diff-check가 통과했다.

## 기각한 대안

- **`completion_answer_records`를 새로 만들기** — 기존 `answer_records`와 답의 진실 원천이 둘이 된다. in-place v2를 택한다.
- **기존 `session_turns`에 request당 UNIQUE 추가** — 세션 종료 시 삭제되는 활성 transcript와 불변 terminal 기록의 수명이 다르다.
- **receipt에 handoff digest만 저장하기** — 재시작 뒤 exact payload를 복원할 수 없어 같은 후보인지 판단하려면 다른 table을 추정 조인해야 한다.
- **receipt JSON을 답 투영의 fallback으로 사용하기** — 손상된 AnswerRecord를 숨기고 두 번째 진실 원천을 만든다. 불일치는 실패시킨다.
- **Store 생성자에서 자동 migration** — 애플리케이션 시작과 DDL 실패 경계가 섞인다. 별도 원자 migration 뒤 runtime은 validate-only로 연다.
- **전역 `user_version` 사용** — 다른 SQLite component의 version과 충돌하고 어느 capability가 준비됐는지 알 수 없다.
- **같은 DB 경로면 같은 UoW로 인정하기** — connection과 공개 API가 달라 Answered 우회·reader drift를 막지 못한다. 객체 identity를 확인한다.

## 불변식 자체점검

- **사용자 결과 기준 미아 없음 — 강화.** Answered 상태와 다섯 completion artifact가 한 transaction에서 생기거나 모두 생기지 않는다.
- **Authority 중앙 — 보존.** 공통 planner가 현재 Approval policy와 책임 snapshot을 다시 검증한다. SQLite adapter가 정책을 자체 선언하지 않는다.
- **등록 무결성 — 보존.** responsibility resolver가 돌려준 현재 Agent Card·Owner snapshot 검증을 공통 planner가 맡는다.
- **전이 ≠ 기록 — 보존.** Request terminal 전이, AnswerRecord, audit, SessionTurn, outbox, receipt는 별개 개념이다. 원자적 commit이 이 구분을 합치는 것은 아니다.
- **legacy 무추정 — 강화.** 기존 AnswerRecord에 sources·snapshot·request 상관을 추정 backfill하지 않는다.
