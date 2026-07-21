# ADR 0043 — Request-first Application Service와 Approval 선행

- 상태: 채택(Accepted)
- 날짜: 2026-07-12
- 계보: ADR 0042의 Question Request 수명주기와 공통 Answer Finalization을 구현 가능한 순서와 경계로 구체화한다. SQLite durable schema와 UoW 경계는 ADR 0044가 이어받는다.
- 구현 상태: P17.2c-1a·1b·1c, P17.6a, P17.3a·3b·3c, P17.2c-2, P17.4, P17.5 완료. 초기 Request-first 코어, 단일 프로세스 동시 advance·orphan 수렴·조립 gate, 최소 Approval 경계, InMemory/SQLite Answer Finalization, P17 SSE 생산자·구독, 웹·retrieve·MCP·UI 전환과 감독 read view, request-aware Unowned·Contested 재개/종결을 구현했다. online Owner 사전승인과 offline 자동발신 사후교정 evidence도 같은 Finalization에 결박했다. Approval 운영은 ADR 0048/P17.6b가 이어받는다. 승인 운영·durable linked workflow·lease/outbox 소비·production 인증/권한이 끝나기 전에는 production 경로가 아니다.

## 맥락

현재 `AskOrg`는 Router 호출, Runtime 실행, Approval 표시, AnswerRecord·Audit 기록, ConflictCase·ManagerItem 생성까지 한 객체에서 처리한다. blocking·SSE·retrieve도 서로 다른 부수효과 순서를 쓴다.

특히 `_apply_approval_gate`는 승인이 필요한 답을 `draft_only`로 바꾼 뒤 `Answered`와 AnswerRecord로 남긴다. ADR 0042의 계약은 다르다. 승인 전 초안은 `AwaitingApproval`이며, 승인이나 수정 승인이 끝나기 전에는 terminal 답이 아니다. 이 상태에서 Finalization만 먼저 공통화하면 잘못된 승인 의미가 원자적으로 고착된다.

SSE도 HTTP generator가 실행 수명을 소유한다. 클라이언트가 본문을 읽기 전에 끊으면 Request가 만들어지지 않거나 실행이 중단될 수 있다. 기존 `_tracking`은 Routed 비동기 질문만 다루고 requester 소유권도 확인하지 않는다.

## 결정

### 1. production 질문 경계는 Request-first Application Service다

새 `QuestionResolutionApplication`이 웹·SSE·retrieve·MCP의 공통 진입점이다. 질문을 `Received/revision 0`으로 저장한 뒤에만 Router를 호출한다. 기존 `AskOrg`는 개발·회귀용 legacy facade로 남기고 production 조립에서는 직접 호출하지 않는다.

Application Service의 최소 명령과 principal은 다음과 같다.

```text
RequesterPrincipal(org_id, subject_id)
AskQuestion(principal, question, session_id?, context_snapshot?)

ask(command) -> QuestionOutcome
retrieve(request_id, principal) -> QuestionLookupResult
advance(request_id, *, expected_revision) -> QuestionLookupResult

# P17.3b에서 추가
open_stream(command) -> OpenQuestionStream(request_id, events)
```

`retrieve`는 저장된 `org_id`와 `requester_id`를 현재 principal과 대조한다. 미존재와 소유권 위반은 같은 not-found 결과로 처리해 다른 사용자의 Request 존재를 드러내지 않는다.

### 2. Request 상태가 진행의 단일 출처다

진행 함수는 현재 상태만 보고 다음 작업을 정한다.

```text
Received          → Router를 한 번 호출
ReadyToDispatch   → 저장된 RouteTarget으로 실행
AwaitingAnswer    → 연결된 WorkTicket을 조회
AwaitingConflict  → Pending
AwaitingManager   → Pending
AwaitingApproval  → Pending
terminal          → 저장된 최종 결과를 투영
```

`ReadyToDispatch` 이후에는 Router를 다시 호출하지 않는다. 합의나 Manager 처분으로 확정된 RouteTarget을 그대로 쓴다. 단일 프로세스 파일럿에서는 request별 keyed lock으로 중복 초기 라우팅을 막는다. 다중 인스턴스 lease는 P17.9에서 구현한다.

### 3. 최소 Approval 경계를 Finalization보다 먼저 만든다

`ApprovalDraft`는 Runtime이 만든 후보 답, `ApprovalItem`은 중앙 정책이 지정한 승인 처리 단위다. 둘 다 `request_id`, 실행 attempt, RouteTarget을 보존한다. 처분은 `Approve | ApproveWithEdit | Reject`의 sealed sum이다.

- `route.requires_approval=True`인 후보는 AnswerRecord를 만들지 않는다.
- 후보 mode가 `draft_only`여도 AnswerRecord를 만들지 않는다.
- ApprovalItem을 만들고 Request를 `AwaitingApproval`로 옮긴 뒤, 본문이 없는 Pending을 반환한다.
- 승인 또는 수정 승인은 `ApprovedCandidate`를 만들고, P17.3의 공통 Finalization만 이를 terminal 답으로 확정한다.
- 반려는 `DeclinedRequest`로 종결한다.

승인 주체는 카드 자기보고로 정하지 않는다. 중앙 `ApprovalPolicy`와 `ApprovalAuthorizer`가 결정하며 production 설정이 없으면 fail-closed한다. 기존 워커의 `PendingDraft`는 로컬 검토일 뿐 중앙 승인 증거로 간주하지 않는다.

P17.6a 구현은 정책·권한·Store 반환을 canonical strict model로 다시 검증한다. `ApprovalItem.awaiting_revision`은 AwaitingApproval Request revision과 승인된 Finalization 후보의 `expected_revision`을 결박하고, request/item/attempt/RouteTarget/draft/action도 exact-link한다. 승인자 명령은 인증된 `ApproverPrincipal(org_id, subject_id)`과 일치해야 한다. 같은 처분은 멱등 수렴하고 다른 처분은 명시적으로 충돌한다. 이 보장은 단일 프로세스 InMemory 경계이며 ApprovalItem과 Request를 한 durable transaction에 묶거나 terminal audit·delivery를 남긴다는 뜻은 아니다.

### 4. Finalization은 승인 완료 답만 원자적으로 종결한다

`QuestionCompletionUnitOfWork`가 AnswerRecord, Request terminal CAS, terminal audit, SessionTurn, delivery outbox를 한 transaction으로 확정한다. `AnswerRecord`와 `SessionTurn`에는 `request_id`를 추가하고 request당 유일 제약을 둔다. AnswerRecord는 재시작 뒤 같은 답을 복원할 수 있도록 mode와 sources도 보존한다.

동일 후보의 재호출은 기존 record를 읽어 같은 결과를 반환한다. 내용이 다른 후보의 경쟁은 한 건만 수용한다. `INSERT OR IGNORE`로 다른 payload의 충돌을 숨기지 않는다.

InMemory 구현도 독립 Store의 공개 메서드를 순서대로 호출하지 않는다. 하나의 lock과 공유 상태를 가진 UoW로 rollback 가능한 경계를 만든다. SQLite 구현은 한 연결에서 `BEGIN IMMEDIATE`를 열고 no-commit helper를 사용한다.

Finalization의 공개 입력은 `CompletionHandoff = FinalizationCandidate | ApprovedCandidate`로 제한한다. handoff 객체 자체를 승인 증거로 믿지 않는다. 승인 불필요 후보는 현재 중앙 ApprovalPolicy를 다시 평가해 `NoApprovalRequired`와 policy version이 같은지 확인하고, 승인 후보는 resolved ApprovalItem의 request/item/revision/attempt/RouteTarget/action/candidate와 exact 비교한다.

승인된 후보의 원 mode가 `draft_only`이면 최종 mode를 `full`로 올린다. 그 밖의 mode는 그대로 둔다. terminal audit에는 원 candidate mode와 final mode, Approval item·처분·승인자·승인 시각·policy version을 함께 남긴다. `AnswerRecord.agent_id`는 책임 Agent Card ID이고 `answered_by`는 최종 발신 시점의 Owner User ID다. 승인자와 Owner는 같은 사람이라고 가정하지 않으며, Finalization은 주입된 책임 snapshot resolver로 둘을 구분한다.

P17.3a InMemory 구현은 Question Request·AnswerRecord·terminal audit·request-correlated SessionTurn·delivery outbox를 하나의 backing state와 `RLock` 아래 copy-on-write로 commit한다. 공개 Request CAS로 `AnsweredRequest`를 직접 쓰는 우회는 이 completion-backed Store에서 거부한다. 모든 저장 객체와 공개 반환값을 plain-data canonical 복사본으로 분리해 frozen 모델의 강제 변조가 backing Request·Approval·completion 증거로 번지지 않게 한다. completion callback과 같은 ApprovalItem resolve callback의 재진입도 명시적으로 거부한다. `session_id=None`이면 SessionTurn을 만들지 않고, 값이 있으면 활성 세션 여부와 무관하게 request-correlated turn을 남긴다. 활성 Session transcript 갱신은 별도 projector 책임이다. delivery outbox v1은 `answer_ready`와 request/record 참조만 저장한다. AnswerRecord는 sources·snapshot SHA까지 보존하지만 legacy SQLite v1은 이 필드를 저장할 수 없어 canonical validation 뒤 write를 거부한다.

이 InMemory 원자성은 ApprovalItem resolve와 completion을 한 transaction에 묶거나 SQLite 내구성, 활성 Session projection, outbox 소비·lease를 보장하지 않는다. 앞의 첫 항목은 재시도 가능한 resolved Item+AwaitingApproval로 남기고, 나머지는 P17.3c·P17.9에서 닫는다. P17.3c의 in-place AnswerRecord v2, 별도 request-correlated SessionTurn, component manifest, receipt 계약은 ADR 0044를 따른다.

### 5. SSE 구독과 실행 생산자를 분리한다

`open_stream`은 반환 전에 Request를 저장한다. HTTP 연결은 실행 결과를 구독할 뿐 실행 자체를 소유하지 않는다. 부분 token은 최종 답 기록이 아니며, `done(request_id, record_id)`은 Finalization commit 뒤에만 발행한다.

연결이 끊겨도 background 실행은 이어진다. commit 뒤 전송이 끊기면 사용자는 Request ID로 같은 AnswerRecord를 조회한다. 이 분리가 끝나기 전에는 SSE를 production profile에서 활성화하지 않는다.

P17.3b는 ADR 0031의 `AskOrg.handle_stream` 이벤트를 넓히지 않고 P17-native 이벤트와 broker를 별도로 둔다. legacy generator는 Router·Runtime·audit·Session 수명을 HTTP 소비에 묶고, 승인 판정 전에 token을 노출하며, `DoneEvent`에 request/record 상관키가 없어 production 계약으로 쓸 수 없다. 새 이벤트는 `accepted | token | pending | done | declined | failed | interrupted`의 sealed sum이다. `done`은 `request_id`·`record_id`뿐 아니라 ADR 0042의 채널 동등성을 확인할 `mode`와 사용자에게 필요한 `sources`·`review_status`·책임 Owner User/Agent Card를 `CompletionBundle`에서만 투영한다. 답 본문은 canonical record 조회가 맡는다.

첫 슬라이스에서는 Runtime token을 전부 비공개 buffer에 모은다. 안전한 순서는 `Runtime 완료 → ApprovalBoundary → Finalization commit → CompletionReader exact-read → token* best-effort 발행 → done`이다. Approval 대기, 정책 변경으로 인한 Finalization 거부, completion 손상에서는 buffer를 전부 버리고 본문·출처를 노출하지 않는다. 이 선택은 실시간 첫 token 지연을 감수하지만, Approval 판정과 Finalization 재검증 사이의 policy race로 미승인 본문이 새는 일을 막는다. 실시간 선공개는 별도의 token disclosure policy와 정책 snapshot을 설계한 뒤에만 허용한다.

broker는 전달 outbox나 감사 로그가 아닌 단일 프로세스 전송 장치다. 구독자별 queue는 bounded이고 publish는 non-blocking이다. 포화 시 token만 버리며 pending·done·declined·failed·interrupted 제어 이벤트는 token을 밀어내서라도 남긴다. token은 재생하지 않는다. 늦은 구독자는 먼저 등록한 뒤 Question Request와 CompletionReader를 다시 읽고, 현재 Pending 또는 terminal 결과를 재구성한다. 같은 terminal은 구독별 한 번으로 합치고 다른 record·terminal 충돌은 fail-closed한다. 구독 종료는 subscriber만 제거하며 producer를 취소하지 않는다.

producer scheduler는 애플리케이션 수명이 소유하고 같은 프로세스에서 request당 실행 하나만 시작한다. 작업이 끝나면 claim을 풀어 일시 실패를 재시도할 수 있게 하되, 재실행은 먼저 Request와 completion을 읽어 이미 terminal이면 계산하지 않는다. Runtime·scheduler의 일시 오류는 Request를 임의로 `FailedRequest`로 만들지 않고 retryable `interrupted`로 연결만 닫는다. `failed`는 실제 Request가 `FailedRequest`일 때만 발행한다. 다중 프로세스 lease·재시작 실행 복구·commit 뒤 delivery 재시도는 P17.9 범위다.

P17.3b의 HTTP 어댑터는 `open_stream`을 `StreamingResponse` 생성 전에 호출하고 `X-Request-ID`를 돌려준다. response generator에는 subscription 순회·SSE 직렬화·`finally: close()`만 둔다. P17.2c-2에서 native `/requests`·재접속 SSE·canonical GET과 legacy `/ask/stream` 호환 URI를 기본 웹에 연결하고, blocking·retrieve·MCP도 같은 application 계약으로 통일했다. production-style 조립은 P17-native stream application 없이 legacy generator로 폴백할 수 없다.

P17.3b 구현은 terminal 발행 권한도 저장 증거에 묶는다. broker는 `request_id`를 받은 뒤 주입된 Question Request Store와 Completion Reader를 exact-read해 `done | declined | failed`를 만든다. 구조만 맞춘 미저장 bundle이나 Request로 terminal을 만들 수 없다. terminal topic에는 이후 nonterminal 이벤트가 들어가지 않고, 늦은 구독자도 같은 lock 안에서 즉시 terminal로 봉인된다. producer scheduler는 request당 단일 claim과 전체 inflight 상한을 두며, commit 뒤 첫 전송이 실패하면 token을 다시 내보내지 않고 저장된 completion으로 `done`만 재조정한다.

독립 HTTP router는 `POST /requests`, `GET /requests/{request_id}/stream`, `GET /requests/{request_id}`를 제공하고 P17.2c-2에서 기본 웹에 연결됐다. principal은 인증 resolver의 canonical `RequesterPrincipal`만 받으며 body·path의 신원 자기보고는 허용하지 않는다. 미존재와 소유권 위반은 같은 응답으로 숨기고, 조회 직전 completion snapshot의 org/requester를 다시 대조한다. HTTP 계층은 자체 `interrupted` 이벤트를 만들지 않으며, 유휴 종료·disconnect·직렬화 오류에는 구독만 닫는다. broker 포화가 Request 저장 뒤 발생하면 구조화된 오류와 `X-Request-ID`로 같은 Request를 다시 찾을 수 있게 한다.

### 6. 사용자 결과와 상관키를 통일한다

새 결과는 모두 non-null `request_id`를 가진다.

```text
Answered(request_id, record_id, ...)
Pending(request_id, kind, message)
Declined(request_id, reason_code, message)
Failed(request_id, error_code, message)
```

WorkTicket은 `(request_id, attempt)`, ConflictCase·ManagerItem·ApprovalItem은 `request_id`, terminal audit·delivery outbox는 request당 유일 키를 가진다. P17.2c-2의 기존 `tracking`은 별도 alias Store나 bearer가 아니라 `tracking == request_id`인 URI 호환 이름으로만 남긴다. 새 비동기 질문은 Request ID 자체를 canonical 조회 키로 쓴다.

WorkTicket의 내부 상관키를 기존 워커 wire에 바로 추가하지 않는다. 구버전 `TicketFrame`이 추가 필드를 거부하므로 중앙 매핑에 보존하고, 워커가 실제로 필요해질 때 protocol version과 선행 배포로 진화한다.

P17.2c-2는 `QuestionSurfaceComposition` 하나에 Resolution Application, Approval 경계, completed-inline Answer Source, stream scheduler/broker와 Completion UoW/Reader를 같은 dependency identity로 묶었다. legacy `/ask`·`/ask/stream`·`/ask/{tracking}`은 이 composition의 DTO를 투영하는 URI 호환 어댑터이며 `AskOrg`·legacy SessionStore·Audit·WebSocketDispatcher에 질문 부수효과를 이중 기록하지 않는다. native `/requests*`, MCP `ask_org/get_question`, Next.js와 정적 UI도 같은 Request ID와 canonical completion을 사용한다. 다만 웹과 MCP를 별 프로세스로 띄우면 같은 계약만 공유할 뿐 상태는 자동으로 공유하지 않는다.

현재 Answer Source는 중앙/로컬 Runtime이 완성 답을 즉시 돌려주는 completed-inline 모드만 허용한다. 분산 WorkTicket runtime은 P17.9의 durable AwaitingAnswer·lease·복구가 없으므로 production-style 조립에서 거부한다. P17 AnswerRecord는 legacy Store에 복제하지 않고 read-only composite view로 감독·피드백·정정·scorecard에 연결한다. 동일 record ID의 payload 충돌이나 completion 일부 손상은 legacy 값으로 숨기지 않고 fail-closed한다.

Phase 12의 presence 안전 규칙도 이 전환에서 복구했다. `DemoApprovalPolicy`가 Owner User ID로 presence를 평가해 online이면 `ApprovalRequired`로 올리고, offline 자동발신이면 `NoApprovalRequired.needs_correction_review=True`를 만든다. Finalization은 현재 policy를 다시 평가해 handoff와 exact 비교하고, 같은 bool을 `NoApprovalEvidence`·terminal 책임 snapshot·AnswerRecord에 결박한다. 책임 resolver는 Agent Card와 Owner User 귀속만 판정한다. 평가 사이에 presence가 바뀌면 승인이나 사후교정 표시를 추정하지 않고 fail-closed한다.

### 7. 구현 순서를 고정한다

1. P17.2c-1a — legacy-compatible Request 상관키와 새 경로의 nonblank 생성 관문
2. P17.2c-1b — Request-first Application Service intake·소유권 검증
3. P17.2c-1c — request별 동시 advance·linked entity 실패 수렴·조립 gate
4. P17.6a — 최소 ApprovalDraft·ApprovalItem·승인/수정승인/반려 경계
5. P17.3a — 승인 불필요 blocking·retrieve의 InMemory Finalization
6. P17.3b — SSE 실행 생산자 분리와 commit 뒤 done
7. P17.3c — SQLite Finalization UoW와 outbox
8. P17.2c-2 — blocking·SSE·retrieve·MCP 전 표면 전환과 tracking alias

## 결과

- 승인 전 초안과 최종 답을 구조적으로 구분한다.
- 모든 채널이 같은 Request와 최종 결과를 보게 된다.
- Application Service가 기존 `AskOrg`의 여러 책임을 한 번에 재사용하지 않으므로, migration 동안 legacy와 production 경로가 명확히 갈린다.
- 모델과 테이블에 request 상관키가 단계적으로 추가된다. 기존 AnswerRecord·SessionTurn은 신뢰할 상관키가 없으므로 억지로 backfill하지 않고 legacy null 행으로 남긴다. production requester 조회에서는 제외한다.
- P17.2b Recovery Runner는 현재 `Received`와 `ReadyToDispatch` hook만 다시 호출한다. durable WorkTicket·lease·outbox를 대신하지 않으며, 해당 범위는 P17.3·P17.9에서 닫는다.

## 기각한 대안

- **기존 AskOrg를 바깥에서 감싸기** — 내부에서 이미 Router·AnswerRecord·Audit·Session 부수효과를 실행해 공통 UoW와 중복된다.
- **Finalization부터 구현하기** — `draft_only`를 terminal로 남기는 현재 승인 오류를 원자적으로 고착한다.
- **HTTP generator가 실행 계속 소유** — disconnect와 사용자 질문 수명이 결합돼 Request 복구 계약을 지킬 수 없다.
- **tracking을 Request ID로 이름만 변경** — Contested·Unowned 수명, requester 권한, terminal 원자성을 해결하지 못한다.
- **legacy 기록의 request_id 추정 backfill** — 신뢰할 상관키가 없어 다른 질문과 답을 잘못 연결할 수 있다.

## 불변식 자체점검

- 사용자 결과 기준 미아 없음: Router 전에 Request를 저장하고 disconnect 뒤에도 수명을 남긴다.
- Authority 중앙: RouteTarget과 승인 주체를 중앙 정책으로 재검증한다.
- 등록 무결성: 실행 직전 현재 Registry의 Agent Card를 다시 해소한다.
- 전이 ≠ 기록: Request 전이와 AnswerRecord·Audit는 다른 개념이지만 terminalization transaction은 하나다.
- 책임 확정 전 답 금지: Contested와 승인 대기 초안은 Pending이며 AnswerRecord를 만들지 않는다.
