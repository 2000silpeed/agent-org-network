# ADR 0048 — Approval 운영, 새 세대 재지정, 초안 보존 경계

- 상태: 채택(Accepted)
- 날짜: 2026-07-14
- 계보: ADR 0042의 Question Request 사용자 결과 의미와 ADR 0043의 Request-first Approval·Finalization 순서를 운영 가능한 승인 수명주기로 구체화한다.
- 적용 범위: P17.6b의 단일 애플리케이션 프로세스 Approval 운영과 전 채널 의미 동등성.
- 제외 범위: ApprovalItem·Question Request·Finalization·Audit·알림의 durable 단일 transaction, 재시작 복구, 다중 인스턴스 lease·fencing, transactional outbox는 P17.9가 맡는다. production 암호화·실제 삭제·legal hold·DSR·backup 삭제 증명과 구체 보존기간은 P17.13이 맡는다.

## 맥락

P17.6a는 `ApprovalDraft`·`ApprovalItem`과 `Approve | ApproveWithEdit | Reject`를 만들고, 승인 전 후보를 본문 없는 `AwaitingApproval`로 보존했다. P17.3은 승인된 후보를 공통 Finalization으로 확정할 수 있다. P17.6b S1은 승인 배정 세대와 안전한 처리함·상세 조회를 구현했고, S2는 `ApprovalBoundary.decide()`에서 Finalization·terminal 전달까지 잇는 운영 명령을 완성했다. S3는 중앙 authorizer를 거치는 수동 재지정, 주입 expiry 정책, unavailable 종결과 process-local 복구 scan을 구현했다. S4는 본문 없는 Approval 사건 journal과 알림 dedup, exact-terminal 보존 판정, shared recorder/read-only handle 조립을 구현했다. S5는 세션 principal 기반 웹 어댑터, 조건부 MCP 승인 도구, Next·정적 Approval 탭과 blocking·GET·SSE reconnect·MCP 의미 동등성을 연결했다. S6은 32-way 경쟁, 단계별 장애·변조, 전체 회귀와 독립 리뷰까지 마쳐 P17.6b의 단일 프로세스 운영 계약을 닫았다.

기존 Store의 `(request_id, attempt)` 단일 Item 색인과 `open | resolved` 상태만으로는 같은 후보 답의 승인 담당 변경을 표현할 수 없다. 기존 Item의 승인자나 기한을 덮어쓰면 이전 승인자의 늦은 명령을 새 assignment와 구분할 causal token이 사라지고, 원 정책과 SLA 이력도 잃는다.

Question Request의 일반 `transition()`은 same-state 덮어쓰기를 의도적으로 금지한다. 이 규칙을 느슨하게 풀면 다른 상태까지 임의 갱신할 수 있다. Approval 재지정은 전용 도메인 전이로만 열어야 한다.

## 결정

### 1. 만료는 사람의 거절이 아니다

`DeclinedRequest`는 인증된 사람이 `Reject`로 답하지 않겠다고 명시한 결과다. 단순 시간 초과를 `Declined`로 바꾸지 않는다. timeout 자체도 복구 불가능한 실패가 아니므로 곧바로 `Failed`로 바꾸지 않는다.

`now >= due_at`이면 아직 open인 Item의 새 승인·수정승인·반려 처분을 받지 않는다. 중앙 `ApprovalExpiryPolicy`는 닫힌 결과 중 하나만 낸다.

```text
ReassignExpiredApproval
  → 유효한 다음 승인자 또는 fallback으로 새 Item 세대 생성
  → Request는 AwaitingApproval을 유지

ApprovalUnavailable
  → 유효한 승인자와 fallback이 영구히 없다는 정책 증거
  → FailedRequest(error_code="approval_unavailable")
```

`ApprovalUnavailable`은 timeout의 별칭이 아니다. 정책·조직 구성상 복구 가능한 경로가 없다는 sealed 결과여야 한다. 자동 승인과 timeout 기반 자동 거절은 허용하지 않는다. 기간·fallback·최대 재지정 횟수·고위험 규칙은 코드 상수가 아니라 주입 정책이 결정한다.

### 2. 재지정은 새 ApprovalItem 세대를 만든다

기존 Item의 `approver_id`나 `due_at`을 덮어쓰지 않는다.

- Request ID, 실행 `attempt`, `RouteTarget`, `ApprovalDraft` identity와 candidate digest는 유지한다.
- 새 Item은 필수 `org_id`·timezone-aware `due_at`, 새 `item_id`, `approval_round + 1`, `supersedes_item_id`, 새 `ApprovalRequired`, 새 assignment 시각을 가진다. `created_at`이 그 세대의 assignment 시각이며 `due_at >= created_at`이어야 한다.
- 기존 Item은 새 Item을 가리키는 immutable supersession evidence와 함께 닫힌다.
- Request는 전용 `reassign_approval(...)`로 `AwaitingApproval → AwaitingApproval`, revision `n → n+1`을 수행한다.
- 새 `AwaitingApproval.draft_ref`와 `HandlingAssignment.ref`는 새 Item ID이며 SLA도 새 assignment 기한으로 바뀐다.
- Runtime·Router를 다시 호출하지 않고 실행 attempt도 올리지 않는다.
- superseded Item의 늦은 처분은 fail-closed한다.

일반 `QuestionRequest.transition()`의 same-state 금지는 유지한다. 전용 전이는 현재 상태·revision·이전 Item ID·route·attempt가 모두 맞고, 새 Item ID·기한이 유효할 때만 revision을 정확히 한 칸 전진시킨다.

### 3. Store는 active index와 immutable history를 분리한다

`ApprovalStore`는 다음 의미를 제공한다.

- `(request_id, attempt)`의 현재 active Item 조회
- `(request_id, attempt, approval_round)`의 세대 이력
- `(org_id, approver_id)` 범위의 본문 없는 open Item 요약 목록
- 같은 Item lock 안에서 기존 open Item을 supersede하고 새 세대를 만드는 조건부 전이
- 같은 canonical 재지정 재시도는 같은 새 Item으로 수렴하고, 다른 대상·정책·기한은 명시적 conflict

Item ID 조회와 반환값은 canonical strict model로 다시 검증한다. Store가 subclass, 다른 org/request/revision/generation, 변조된 draft·route·정책을 반환하면 Request·Finalization·Audit·Authority write 전에 거부한다.

S1 구현은 다음 불변식을 구체화했다.

- 최초 Item 생성의 semantic fingerprint에는 조직·request·revision·attempt·route·candidate·요구사항·세대 계보를 포함하되, 재시도마다 달라질 수 있는 생성 ID·배정 시각·기한은 넣지 않는다. 같은 의미의 경쟁에서는 첫 저장 Item을 winner로 채택한다.
- Item 저장 뒤 Request CAS가 끊긴 재시도는 새 기한을 덮어쓰지 않고 첫 winner의 `created_at`·`due_at`으로 `AwaitingApproval`을 복구한다.
- successor 재지정의 full identity에는 조직·부모 세대·Draft와 새 `due_at`이 포함된다. 같은 canonical payload만 저장 successor로 수렴하고 다른 기한은 conflict다.
- Store의 queue는 조직과 지정 승인자로 범위를 좁힌 `ApprovalPendingSummary`만 반환한다. 질문·초안·수정 본문·source·snapshot은 싣지 않는다.
- 처분과 Finalization은 Item ID 조회, `(request, attempt)` current, `(request, attempt, round)` snapshot이 같은 concrete `ApprovalItem`인지 교차 검증한다. 조직·기한·색인이 어긋나면 Finalization write는 0이다.

### 4. 운영 application이 승인에서 terminal 전달까지 수렴시킨다

`ApprovalOperationsApplication`은 승인자 처리함의 단일 application service다.

```text
pending_for(principal)
detail(item_id, principal)
decide(item_id, principal, intent)
reassign(item_id, principal, target)
expire_due(now, limit)
retention_status(item_id, now)
```

S6까지 `pending_for`·`detail`·`decide`·`reassign`·`expire_due`·`retention_status`와 웹·MCP·두 UI·전 채널 연결, 경쟁·장애·최종 리뷰 게이트를 구현했다.

- 목록에는 질문·초안·수정 본문을 싣지 않는다.
- 상세는 현재 active Item의 지정 승인자와 같은 조직에만 질문·초안 후보를 제공한다.
- 미존재, 다른 조직, 다른 승인자, 새 조회·새 명령이 가리킨 superseded Item은 같은 not-found-or-denied 결과로 숨긴다. 같은 프로세스가 보유한 exact same-command 수동 재지정 재시도만 sealed work와 저장 결과를 검증해 기존 outcome으로 수렴한다.
- HTTP·MCP body에서 actor ID를 받지 않는다. 인증 adapter가 만든 `ApproverPrincipal`만 사용해 기존 `ApprovalBoundary` action을 구성한다.
- `Approve | ApproveWithEdit`는 `ApprovalBoundary.decide()`의 exact `ApprovedCandidate`를 같은 composition의 `QuestionCompletionUnitOfWork.complete()`에 넘긴다. `CompletionReader.by_request()`를 exact-read한 뒤 저장된 terminal만 publish한다.
- `Reject`는 저장된 `DeclinedRequest`를 exact-read한 뒤 같은 terminal publisher를 쓴다.
- resolved Item + AwaitingApproval, terminal commit + 응답 유실, terminal publish 유실은 같은 명령 재시도로 forward repair한다.
- 다른 처분 경쟁은 한 winner만 남기고 loser는 명시적 conflict다.
- S2의 `ApprovalAssignmentGeneration`은 Item의 조직·request·revision·attempt·route·draft·요구사항·배정 시각·기한·round·predecessor를 한 immutable snapshot으로 묶는다. 이 snapshot은 Boundary의 조건부 resolve, Finalization precommit, Completion exact-read, publish 직전 Item 재검증까지 이어진다.
- 신규 SQLite completion handoff는 schema v2와 필수 assignment generation을 저장한다. generation이 없던 v1 승인 receipt는 이미 확정된 terminal의 strict read에만 쓰고 새 Finalization replay나 승인 권한 증거로 승격하지 않는다. v1 승인 receipt를 durable 명령 복구로 바꾸는 일은 P17.9 범위다.
- S3의 수동 `reassign`은 actor가 없는 target과 인증 principal을 중앙 `ApprovalReassignmentAuthorizer`에 넘기고, exact assignment·actor·target·정책·authority·evidence가 결박된 sealed 허가만 받는다.
- `expire_due`는 due open projection을 정렬·중복 검증한 뒤 주입 `ApprovalExpiryPolicy`의 sealed `ReassignExpiredApproval | ApprovalUnavailable`만 수용한다. 성공과 field-free `conflict | dependency | integrity` 실패를 같은 batch에서 잃지 않는다.
- 재지정은 old supersede+new create Store 결과와 Request revision CAS를 각각 exact reread한다. 전체 generation의 ID·round·direct/current index, 인접 successor/predecessor, target approver, route·draft·revision·시각을 write 전후 모두 검증한다.
- unavailable은 사람 Reject가 아닌 별 Item 상태와 정책 증거다. 결정 표면에서는 닫힌 Item으로 숨기고, exact `FailedRequest(error_code="approval_unavailable")`와 terminal 전달로만 수렴한다.
- process-local fair queue·quarantine·완료 cache는 poison Item이 뒤의 due 진행을 막지 않게 하고 응답 유실을 같은 sealed work로 복구한다. 같은 Request의 여러 generation을 한 scan에서 연쇄 처리하지 않으며, 동기 재진입을 막고 cached/pending model을 strict 재수화한다.

이 수렴은 단일 프로세스 멱등 재시도 계약이다. queue·quarantine·cache는 process-local이며, Approval resolve·재지정·Request·Finalization을 하나의 durable transaction으로 보장한다는 뜻은 아니다.

### 5. 감사·알림은 본문 없는 식별 증거다

Approval 감사 사건은 requested, approved, approved_with_edit, rejected, reassigned, expired, unavailable, retention_eligible을 구분한다. 다음 값만 남긴다.

- org/request/item/draft ID
- approval round와 이전/새 Item 참조
- actor 또는 system 주체
- action/candidate digest
- policy version과 시각
- terminal record/reason/error 참조

S4의 `ApprovalEventJournal`은 deterministic event ID와 strict sealed 사건을 process-local append-once로 기록한다. 같은 ID·같은 payload는 no-op이고 다른 payload는 integrity 오류다. expiry 재지정의 expired+reassigned와 unavailable의 expired+unavailable은 한 atomic batch다. `ApprovalEventRecorder`는 append 응답 유실을 journal read-back으로 복구한다. `ApprovalEvidenceConfiguration`은 Boundary·Operations·Retention에 같은 recorder를 주입하며, composition 밖에는 journal의 read-only `ApprovalEventReader` handle만 제공한다.

질문, 초안, 수정 본문, source 본문은 감사와 알림에 싣지 않는다. push 알림은 pull 처리함을 대체하지 않으며, 알림 실패가 Item·Request 전이를 되돌리지 않는다. 같은 사건의 process-local 재시도는 audit와 notification을 한 번으로 합친다. 이 journal과 알림 dedup은 재시작 뒤 내구성을 보장하지 않는다. P17.9가 durable journal·outbox·안정된 멱등키·전달 receipt로 at-least-once와 의미 멱등성을 맡고, P17.13이 실 채널 장애·재전송을 검증한다. 외부 채널의 물리적 exactly-once는 주장하지 않는다.

### 6. 초안 보존은 정책 판정과 접근 통제까지만 닫는다

open·재지정 중인 Item과 resolved Item + AwaitingApproval 복구 구간에서는 초안 본문을 지우지 않는다. 보존 시계는 `Answered | Declined | Failed`의 exact terminal 증거가 생긴 뒤에만 시작한다.

S4의 `retention_status`는 조회한 Item의 current draft와 전체 assignment lineage를 먼저 검증한다. active 또는 Finalization 전 resolved Item은 `ApprovalDraftRetained`로 보존한다. exact `Answered | Declined | Failed` 증거가 있을 때만 `ApprovalDraftRetentionPolicy`가 terminal 상태·정책 버전·기준 시각을 받아 `retain_until`과 `purge_eligible`을 판정한다. `ApprovalDraftRetentionEvaluated`와 retention_eligible 사건은 terminal 종류·Request revision·terminal 시각·증거 digest를 결박한다. 같은 입력의 policy 결정은 process-local cache로 고정하고, eligible 사건은 shared recorder로 한 번만 기록한다. 목록·감사·알림은 정책과 무관하게 본문을 노출하지 않는다.

P17.6b는 물리 삭제 완료를 주장하지 않는다. P17.9는 durable retention intent/status와 재시작 뒤 reconciliation을 맡는다. 실제 payload redaction/delete 실행과 그 증명, 암호화, legal hold, DSR, backup 삭제 증명은 P17.13이 맡는다. Item·Draft ID, digest, assignment history, 사람 처분, 정책·시각 같은 감사 metadata는 별 보존 계약을 따른다.

### 7. 전 채널 의미를 통일한다

- 운영 명령은 웹 UI와, 신뢰 가능한 server-side approver principal provider가 있는 MCP만 같은 `ApprovalOperationsApplication`을 호출한다.
- principal provider가 없으면 MCP 승인 도구를 등록하지 않는다.
- 승인·수정승인 뒤 blocking·canonical GET·SSE reconnect·MCP `get_question`은 같은 AnswerRecord를 본다.
- Reject와 `approval_unavailable`도 네 사용자 채널에서 같은 terminal 의미를 본다.
- open·reassigned 상태에서는 모든 사용자 채널이 계속 본문 없는 `AwaitingApproval`만 투영한다.
- 사용자 질문 채널에는 approver ID, draft ref, policy version, 재지정 history를 노출하지 않는다.

Approval은 담당이 확정된 답의 발송 승인이다. 담당 미정인 Manager escalation과 같은 도메인 타입이나 큐로 합치지 않는다. 같은 화면에서 별 탭으로 나란히 보여도 Store와 처분은 분리한다.

## 구현 슬라이스

1. **S0 — ADR·SSOT(완료):** 이 결정과 경계를 PRD·TRD·TASK·CONTEXT에 반영했다.
2. **S1 — lifecycle·Store·조회(완료):** Item generation/supersession, active·round 이력, 전용 Request 재지정, 조직 범위의 본문 없는 queue와 현재 지정 승인자 detail을 TDD로 구현했다.
3. **S2 — decide→Finalization→terminal(완료):** actor-free 운영 application, assignment generation 결박, partial-failure forward repair와 terminal publish를 연결했다.
4. **S3 — expiry·reassignment(완료):** 주입 expiry/reassignment 정책, due-at 경계, full-lineage 검증, typed lossless batch, process-local 공정 복구와 forward repair를 구현했다.
5. **S4 — audit·notification·retention(완료):** ID-only process-local 사건 journal, shared recorder와 read-only handle, 배정 알림 dedup, exact-terminal 보존 eligibility를 구현했다.
6. **S5 — web·MCP·두 UI·전 채널(완료):** 인증 principal 기반 운영 어댑터, 조건부 MCP 승인 도구, 본문 없는 queue와 지연 상세를 갖춘 두 UI, open·reassigned·terminal 전 채널 동등성을 구현했다. 인증·세션·선택 세대가 바뀌면 이전 입력과 응답을 폐기하고, 주입한 웹 composition은 반환 전 조립 실패에서도 회수한다.
7. **S6 — fault/concurrency/review(완료):** same/different 처분, approve 대 만료 재지정·unavailable·수동 재지정, 서로 다른 재지정 target, 중복 expiry scan·감사·알림을 32-way로 검증했다. stale open snapshot의 정상 lifecycle loser는 exact generation·색인·계보·Request 재조회 뒤 field-free conflict로 수렴하고, resolved Item의 저장 winner와 terminal/partial Request·completion 인과는 incoming 처분보다 먼저 검증한다. 전체 Python 4,524건, Approval 475건, 핵심 경쟁 12종×30회, 프런트 16건과 정적·production build 게이트를 통과했고 독립 최종 재리뷰는 P0/P1/P2 0이었다.

## 결과와 한계

S6까지 승인 요청·처분·재지정·만료·unavailable·보존 eligibility를 같은 shared recorder와 본문 없는 사건 journal로 추적하고, 지정 승인자의 웹·MCP 처리함과 두 UI에서 같은 operations를 호출한다. exact terminal 전에는 초안을 보존하고, 그 뒤에도 정책 판정만 남길 뿐 물리 삭제를 실행하지 않는다. 경쟁과 장애 검증은 단일 프로세스의 의미 수렴을 증명하며, durable transaction이나 다중 인스턴스 조정을 뜻하지 않는다.

P17.6b가 끝나면 승인 대기가 실제 사람 처리함에 도달하고, 승인·수정승인·반려·만료 재지정이 같은 Question Request와 Finalization으로 수렴한다. 이전 assignment를 덮어쓰지 않아 늦은 승인과 정책 이력을 분리할 수 있고, 초안 본문은 필요한 승인자 상세 외 경로에서 노출되지 않는다.

그러나 P17.6b는 단일 프로세스 Approval 운영과 전 채널 의미 동등성만 완성한다. ApprovalItem·Request·Finalization·Audit·알림의 재시작 내구성, 다중 인스턴스 lease, transactional outbox와 production 보존 실행은 P17.9·P17.13 전까지 보장하지 않는다.
