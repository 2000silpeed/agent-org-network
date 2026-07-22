# ADR 0065 — Conflict escalation의 HITL 승인 증거 계약 (durable `conflict.escalate`)

- 상태: 채택(Accepted)
- 날짜: 2026-07-22
- 계보: ADR 0046(request-aware Contested 책임 결정·escalation 도메인)이 InMemory 단일 프로세스로 미룬 durable escalation 권한·사람 통제를 P17.9 S4.3c.1로 잇는다. ADR 0050(중앙 Authority·RBAC — §11의 `conflict.escalate` action/role/resource)·ADR 0051(운영 변경 승인 증거 — canonical command digest·resource fingerprint 1:1 결박)·ADR 0052(R5.4 `CredentialApprovalEvidence` 취득/재확인 패턴)·ADR 0044(SQLite Completion UoW)를 재사용하고, S4.3b `SealedEscalationEvidence`·S4.3c.0 `ConflictEscalationRegistrySnapshot`(graph_digest)를 소비 결박한다.
- 적용 범위: durable open ConflictCase를 Manager로 넘기는 `conflict.escalate` 명령의 사람 통제(HITL) 승인 증거 — 값 객체 shape, canonical command/cause digest, 취득 provider·재확인 resolver 포트, 순수 검증 계약, 승인자·만료·취소·재승인의 표현 위치.
- 제외 범위: start/write 직전 재인가·재확인과 Case/ManagerItem/Request 전이를 한 transaction에 쓰는 UoW(S4.3c.3), 동시성/장애 게이트(S4.3c.4), reconciliation 게이트(S4.3d). 파일럿 조직의 구체 승인자 신원·실 HITL UI·durable policy epoch·다중 인스턴스는 여전히 후속이다. escalation receipt graph 스키마의 지속 shape는 §8에서 보강했다(전이·write·transaction은 여전히 c.3 밖).

## 맥락 — escalation 자동 seal에서 durable 사람 통제 처분으로

ADR 0046(InMemory S1~S6)에서 escalation은 자동이었다. 마지막 상이 표결이 `DivergentVotes`/`CandidateRegistryChanged` claim을 seal하면 애플리케이션이 곧바로 FromDeadlock ManagerItem을 만들고 Case를 `escalated`로, Request를 `AwaitingManager`로 옮겼다. 그 ADR은 durable 내구성·권한·RBAC를 "P17.9 또는 후속 ADR"로 명시 이관했다.

P17.9 S4.3은 이 경계를 durable하게 다시 연다. S4.3a는 escalation baseline을, S4.3b는 표결 없는 read-only `SealedEscalationEvidence`(`DivergentVotes | CandidateRegistryChanged`)를, S4.3c.0은 graph-aware `ConflictEscalationRegistrySnapshot`(유일 nearest common Manager 또는 유일 root와 `graph_digest`)을 닫았다. 남은 것은 이 두 sealed 산물을 소비해 실제로 Case를 escalated로 전이하는 durable 명령이다.

그런데 durable 전이(Case `escalated`·FromDeadlock ManagerItem·Request `AwaitingManager`)는 되돌리기 어려운 운영 처분이다. ADR 0051이 `session.end`·`hitl.write`·`card.transfer_owner`에 "별도 운영 변경 승인 gate의 현재 허용·증적 참조"를 요구하고 "기존 HITL 토글을 이 승인으로 해석하지 않는다"고 못박은 것과 같은 결로, escalation도 중앙 grant만으로 열어선 안 된다. 사람 승인 증거가 필요하고, 그 증거는 이 명령·이 Case·이 escalation 원인·이 graph 선택에 exact 결박돼야 다른 명령·다른 Case·낡은 원인으로 재사용되지 않는다.

이 계약은 되돌리기 어려운 새 결정 — 승인자·만료·취소·재승인 lifecycle과 toggle 불인정 — 을 담으므로, action/role/resource만 다루는 ADR 0050 §11과 분리해 별 ADR로 둔다.

## 결정

### 1. ADR 경계 — ADR 0050 §11(action) + 신규 ADR 0065(evidence)

`conflict.escalate` action·role·resource mapping은 ADR 0050 §11 보강으로 충분하다. `conflict.open` 선례(a.0)가 이미 같은 층(중앙 action manifest·role hard-limit·resource kind·resolver re-read)에 있기 때문이다.

그러나 HITL 승인 증거 계약은 별 ADR(이 문서)로 둔다.

- **ADR 0046은 스스로 경계를 그었다.** durable·권한·RBAC를 후속 ADR로 명시 이관했으므로, 그 escalation 도메인 ADR(이미 1,400줄 넘음)을 다시 열어 durable HITL 증거를 끼우면 그 경계가 흐려진다.
- **승인자·lifecycle·toggle 불인정은 되돌리기 어려운 새 결정**이라 프로젝트 규율상 자기 ADR을 갖는다.
- **형제 배치가 일관된다.** ADR 0051(운영 승인 증거)·ADR 0052(R5.4 credential 승인 증거)는 각 명령 도메인의 승인 증거 계약을 자기 ADR로 둔다. escalation 승인 증거도 같은 자리에 둔다. RBAC(0050)와 evidence lifecycle(0065)은 다른 관심사다.

### 2. role — operator 명령 + 별도 HITL 승인자 (R5.4 동형 2층)

`conflict.escalate`는 R5.4(중앙 grant AND HITL)와 동형인 AND 2층이다.

- **1층(중앙 RBAC grant):** 명령 주체 role은 **operator**다(ADR 0050 §11에서 hard-limit). escalation은 owner concurrence(`conflict.concur`)도 Manager 처분(`manager.act`)도 아닌 운영 human-control 처분이다. Manager는 escalated Item을 *받아* `manager.act`로 처분하는 downstream 주체이지 자기 escalation을 개시하지 않는다.
- **2층(HITL 승인자):** 명령을 개시하는 operator와 별개로, 사람 승인자의 sealed 증거가 필요하다. 승인자의 구체 신원·조직 그래프 위치는 evidence store가 쥐고 resolver가 current로 증명하며, 중앙 RBAC layer에 승인자 role을 hard-code하지 않는다. 파일럿 조직의 구체 승인자 신원은 외부 결정(차단 아님)이고, 타입/정책 스냅샷 수준에서 승인자는 "사람 governance subject"다(ADR 0017 — owner/사람은 governance 주체). 두 층은 AND이므로, operator grant만으로도, 승인 증거만으로도 escalation은 열리지 않는다.

이 분리는 직무 분리(operator 개시 · 사람 승인 · Manager 처분)를 낳고, ADR 0050 §5의 "admin·operator·auditor는 합치지 않는다" 정신과 정합한다.

### 3. 만료·취소·재승인 — 단일 action + resolver current-snapshot (별 중앙 action 없음)

만료·취소·재승인을 위해 `conflict.escalate.expire` 같은 별 중앙 action을 만들지 않는다. 단일 `conflict.escalate` action + 승인 증거의 current-snapshot resolver로 표현한다.

- **R5.4 선례:** `CredentialApprovalEvidence`는 상태 필드 없이, `CurrentCredentialApprovalEvidenceResolver.resolve_credential_approval_evidence(org_id, evidence_id)`가 current 유효 증거 또는 `None`을 돌려주고, 취득·write 직전 모두 `current == evidence`로 same-evidence를 재확인한다. **만료·취소 = resolver가 `None`(또는 비일치)을 돌려줌**, **재승인 = 새 `evidence_id`가 옛 것을 대체**. 별 중앙 action은 없다.
- **ADR 0050 §6 정합:** 별 action은 manifest를 부풀리고 "한 escalation을 한 명령·한 authorizer 경계로"라는 규율을 깬다. 만료·취소·재승인은 *승인 증거의 lifecycle 상태*이지 별개 RBAC 명령이 아니다.
- **c.1은 shape만.** 실제 평가(취득·재확인)·transaction은 S4.3c.3이다. c.1은 증거 값 객체와 포트 시그니처, 순수 검증 계약(취득 1회·same-evidence 재확인·replay 재취득 0)까지만 못박는다.

### 4. canonical digest — conflict-escalation-domain-local 신설 (durable_credentials generic import 아님)

digest·fingerprint helper는 `durable_credentials`에서 import하지 않고 conflict-escalation 도메인에 신설한다.

- **도메인 결합 회피.** escalation 승인 증거가 `durable_credentials`(무관한 credential 도메인)에 import 의존하면 bounded context가 새어 든다. 헥사고날/DDD 상 두 컨텍스트를 분리한다.
- **신설 지점:** `canonical_escalate_command_digest(...)`(action·resource fingerprint·command에 결박)와 `escalation_cause_digest(...)`(S4.3b `SealedEscalationEvidence`를 canonical 결박)는 이 도메인이 소유한다. resource fingerprint는 `ResourceRef` 4필드만 hash하는 도메인 중립 계산이지만, cross-domain import를 피하려 같은 canonical 형태를 도메인-local helper로 재현한다(사소한 4필드 hash 중복이 import 결합보다 싸다).
- **graph selection은 재사용(신설 아님).** `graph_selection_digest`는 S4.3c.0 `ConflictEscalationRegistrySnapshot.graph_digest`를 그대로 결박한다(manager/root 선택이 이미 그 digest 안). 새 digest 함수가 아니라 field 결박이다.

### 5. sealed 증거 shape — granted 스냅샷 하나 (lifecycle union은 c.2/c.3로 이월)

c.1이 못박는 것은 **소비 가능한 granted 증거 값 객체 하나**다. 만료·취소·재승인 상태의 full union은 c.1이 만들지 않는다(소비 최소 결박).

- **`ConflictEscalationApprovalEvidence`(frozen 값 객체)**: `evidence_id`·`status: Literal["granted"]`·`action: Literal["conflict.escalate"]`·`command_digest`(64)·`resource_fingerprint`(64)·`escalation_cause_digest`(64)·`graph_selection_digest`(64). `CredentialApprovalEvidence`("이미 검증된 사람 승인 증거의 secret-free snapshot")의 escalation 판이다.
- **`status: Literal["granted"]`의 역할(§7 ①과 결합):** 답변 HITL toggle과 구분되는 자기 서술 타입이 되고, resolver가 잘못 낡은 증거를 돌려줘도 값 수준에서 걸러지는 방어선을 준다. c.1은 `granted` 변이만 정의한다. `expired`/`cancelled`/`superseded` 같은 terminal 상태·timestamp·supersession chain은 S4.3c.2 schema·S4.3c.3 UoW가 필요할 때 여는 확장이지 c.1이 미리 짓지 않는다.
- **3중 결박이 핵심 신설:** `command_digest`·`resource_fingerprint`(R5.4 재사용 결)에 더해 `escalation_cause_digest`·`graph_selection_digest`가 escalation 특유의 결박이다. 증거가 *이* 명령·*이* conflict_case·*이* escalation 원인(S4.3b sealed evidence)·*이* graph 선택(S4.3c.0 snapshot)에 exact 묶여, 낡은 원인·다른 Case·drift된 graph로 재사용되지 못한다.

### 6. 취득·재확인 — R5.4 패턴 재사용 (취득 1회·same-evidence 재확인·replay 재취득 0)

- **`EscalationApprovalProvider`(취득 포트):** `acquire_escalate_approval(principal, action, resource, command_digest) -> ConflictEscalationApprovalEvidence`. `CredentialRevokeApprovalProvider.acquire_revoke_approval`의 escalation 판. 취득은 명령 경로에서 **정확히 1회**다.
- **`CurrentEscalationApprovalEvidenceResolver`(재확인 포트):** `resolve_escalation_approval_evidence(*, org_id, evidence_id) -> ConflictEscalationApprovalEvidence | None`. `CurrentCredentialApprovalEvidenceResolver`의 escalation 판. 취득 직후와 write 직전 모두 same-evidence(`current == evidence`)를 재확인한다.
- **replay 재취득 0:** 같은 명령의 replay는 승인을 재취득하지 않고, current scope·중앙 authorization·same-evidence만 다시 검증한다(R5.4 `_current_access` 정신). 사람 승인은 한 번만 요구하고, 재시도가 새 사람 승인을 강요하지 않는다.

### 7. toggle 불인정 강제 — 타입 분리 + action 분리 + no-import 가드

기존 답변 HITL toggle(ADR 0025 `HitlToggleMap`)은 이 escalation 승인 증거가 **아니다**. ADR 0051이 운영 승인에서 toggle을 배제한 것과 같다. 세 겹으로 강제한다.

1. **타입 분리:** `ConflictEscalationApprovalEvidence`는 `HitlToggleMap`·`Answer.mode`와 공유 base 없는 독립 frozen 타입이다. `status: Literal["granted"]` 판별자가 답변 mode toggle과의 혼동을 막는다.
2. **action 분리:** `conflict.escalate`는 답변 흐름 toggle(`hitl.read`/`hitl.write`)과 다른 중앙 action이다. escalation 승인 증거는 `hitl.write` 운영 승인도 아니다.
3. **no-import 가드 테스트:** 신규 escalation 증거 모듈이 `hitl`(toggle) 모듈을 import하지 않음을 AST로 단언한다. `test_p17_conflict_module은_Router_Precedent_ComplementEdge를_import하지않는다`의 기존 패턴을 재사용한다.

의미 backing: provider·resolver 포트는 HITL toggle을 승인 원천으로 받지도 조회하지도 않는다. resolver는 escalation 승인 증거 store만 읽는다.

### 8. c.2 escalation receipt graph 스키마 — receipt-parent 허브 + sealed 증거 recompute (2026-07-22 보강)

c.1이 값 객체·포트·순수 검증(shape)을 닫았으므로, c.2는 그 계약이 durable하게 어떻게 기록되는지를 별 versioned component `durable_conflict_escalation_receipts_v1`로 설치한다. 새 되돌리기 어려운 도메인 결정은 없고(권한·lifecycle·toggle 불인정은 §1~§7이 이미 정함), c.2는 §5 4중 결박·S4.3b sealed cause·S4.3c.0 graph selection의 지속 shape만 못박으므로 자기 ADR 없이 이 ADR 보강으로 둔다(S4.2a schema가 자기 ADR 없이 ADR 0046/0051 계보로 처리된 선례와 같다).

- **receipt가 parent다(R1.0 판, S4.2a 판 아님).** S4.3b·c.0은 read-only라 escalation 명령 전에 어떤 durable row도 남기지 않는다. sealed cause·graph selection은 명령 시점에 계산·봉인돼 c.3의 한 transaction으로 receipt와 *함께* 처음 쓰인다. S4.2a votes처럼 명령 이전에 누적되는 독립 lifecycle이 없으므로, evidence를 parent로 두면 실제 생성 순서를 뒤집는다. escalation 하나 = receipt 하나이고 sealed 산물은 그 명령의 속성이다 — 이는 receipt가 source-scope proof digest를 자기 컬럼으로 지니는 R1.0 판과 동형이다. child(sealed evidence·result projection·audit·outbox intent)는 R1.0의 `(org_id, receipt_id)` same-org composite FK로 receipt에 1:1 결박해 cross-org row stitching을 DB 수준(PRAGMA foreign_keys=ON)에서 막는다.
- **secret-free 컬럼만.** opaque typed ref(`kind:<lowercase SHA-256>`)·64-hex digest·canonical UTC timestamp·범위 정수만 저장한다(S4.1 규율). raw claim·사유 원문·secret은 없다. c.0 `manager_subject_ref`(nullable)·`root_subject_ref`는 *digest가 아닌 typed subject ref*로 저장한다 — baseline이 `candidate_owner_subject_ref`를 저장하는 것과 같은 secret-free 문법이고, c.3가 선택 target을 FromDeadlock ManagerItem `manager_subject_id`에 mirror하려면 봉인된 선택 대상이 durable해야 하기 때문이다. raw User ID는 여전히 저장하지 않는다.
- **`escalation_cause_digest`만 recompute(자기 정합), 나머지 digest는 store+mirror.** sealed cause 필드(kind·org/conflict/request ref·revision·round·candidate_snapshot/baseline/candidate_claim/vote_set sha256·variant)를 evidence row에 저장하고 c.1과 같은 canonical 필드 순서로 `escalation_cause_digest`를 재계산해 receipt 값과 일치를 강제한다(S4.2a `command_digest`·baseline `baseline_sha256` recompute 규율). `graph_selection_digest`(c.0 소유)·`command_digest`·`resource_fingerprint`(c.1/c.3 소유, 원본 command·ResourceRef 미저장)는 재계산하지 않고 64-hex·receipt↔evidence mirror만 강제한다. `durable_credentials`·c.1 domain 값 객체를 import하지 않고 canonical hash를 도메인-local로 재현한다(§4 no-import 규율).
- **validate-only·write API 0.** open/reconcile은 검증만 하고 repair 0이다(S4.1 판). c.3가 소비할 쓰기 표면은 c.2가 예비하지 않는다 — S4.1/S4.2a/S4.3a 전 선례가 schema는 validate/migrate/open/reconcile만 노출하고 write는 UoW 모듈(S4.2b 동형)이 소유하며 schema validate를 호출할 뿐이다(inactive port 아님, 스키마만). fault-atomic migration·manifest 없는 partial/DDL drift fail-closed·S4.1/S4.2/S4.3a DDL 무변경은 전 선례 그대로다.

## Consequences

- **불변식 영향 없음:**
  - **Authority 중앙** — 승인 증거는 사람 통제(HITL)이지 카드 자기보고 authority가 아니다. 명령 role(operator)·Manager/root 선택(c.0 graph)은 중앙 Registry/정책에서 온다. 어떤 카드도 escalation authority를 자기보고하지 않는다.
  - **전이 ≠ 기록** — c.1은 shape/계약만이며 전이도 write도 없다. Case escalated·ManagerItem·Request 전이(도메인)와 receipt/audit/outbox(기록)는 각각 S4.3c.3·S4.3c.2다.
  - **미아 없음** — escalation은 deadlock/drift된 Conflict를 사람(Manager)에게 durable하게 닿게 하는 경로다. 증거 계약은 미아를 만들지 않는다.
  - **노출 불변식** — 증거는 secret-free다. digest·fingerprint·evidence_id만 담고 raw claim·Manager/root User ID·원문은 담지 않는다(S4.3b·c.0 결과와 같은 결).
- **DB write 0·전이 0·실 store 0.** S4.3c.1은 read-only 계약·포트·순수 검증만 연다.
- **후속 결정 대기:** 파일럿 조직 승인자 구체 신원, 실 HITL 승인 UI, 만료 TTL·취소·재승인의 durable schema(c.2)·평가 transaction(c.3), durable policy epoch·다중 인스턴스 일관성.

## 갱신 대상

- CONTEXT.md: **Conflict Escalation Approval Evidence** 용어 등재(Conflict Escalation Registry Snapshot 다음).
- ADR 0050 §6 manifest·§5 operator·§11: `conflict.escalate` action/role/resource(이 ADR과 함께 보강 완료).
- docs/tasks-v0.md S4.3c.1: 설계·shape 완료 시 갱신(구현은 tdd-engineer).
- CONTEXT.md: **Conflict Escalation Receipt Graph** 용어 등재(§8 c.2 보강과 함께·Conflict Escalation Approval Evidence 다음).
- docs/tasks-v0.md S4.3c.2: c.2 schema 설계 확정 기록(구현은 tdd-engineer).

## S4.3c.1 RED 인수조건 (shape)

- `ConflictEscalationApprovalEvidence`는 frozen이고, `command_digest`·`resource_fingerprint`·`escalation_cause_digest`·`graph_selection_digest`가 각 64 hex가 아니거나 `evidence_id` blank거나 `action`/`status` 리터럴이 아니면 생성 실패다.
- `canonical_escalate_command_digest`는 같은 action·resource·command에 같은 digest를, 다르면 다른 digest를 낸다. `escalation_cause_digest`는 같은 S4.3b sealed evidence에 결정론 digest를 낸다.
- `graph_selection_digest`는 S4.3c.0 snapshot의 `graph_digest`와 exact 일치한다.
- 순수 검증: granted 증거의 4결박이 현재 명령·resource·cause·graph와 exact일 때만 유효, 하나라도 어긋나면 무효. 취득은 1회, same-evidence 재확인은 취득 직후·write 직전 2회 모두 통과해야 유효, replay는 승인 재취득 없이 current만 재검증.
- escalation 증거 모듈은 `hitl` toggle 모듈을 import하지 않는다.
