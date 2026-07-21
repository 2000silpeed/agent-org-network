# ADR 0046 — Request-aware Contested 책임 결정과 primary 확정 후 다중 접지

- 상태: 채택(Accepted)
- 날짜: 2026-07-13
- 계보: ADR 0004(Authority 중앙)·0008(ConflictCase와 1인칭 합의)·0014(Manager 큐)·0037(다중 접지)·0038(합의 기반 상보 엣지)·0042(Question Request 수명주기)·0045(Request-aware Unowned 처분)를 정밀화한다.
- 대체 범위: ADR 0008의 intent 단위 Case 중복 제거, ADR 0037의 Contested 즉시 답변과 사전순 primary, ADR 0038의 합의 직후 전역 `Precedent`·`ComplementEdge` 방출은 **request-aware Contested 경로에서 적용하지 않는다**. `request_id is None`인 legacy 경로와 기존 회귀 자산은 유지한다. 조직·조건·유효기간을 갖춘 학습은 P17.10 contextual Precedent가 맡는다.
- 구현 상태: S0 설계 채택 뒤 S1~S6의 request-aware Case·concurrence claim·direct consensus 재개·Deadlock/Registry drift escalation·Manager mediation·typed post-primary grounding·저장된 Failed 종결·composition/웹/채널 연결·전체 경쟁/장애 검증을 구현했다.
- 설계 검증: 2026-07-13 독립 architecture review **APPROVE(P0/P1/P2 0)**, 구현 사전 점검 **IMPLEMENTABLE**. exact Store API·full handle 복구·Authority write-0·부분 실패 계약과 Markdown fence·diff-check를 확인했다.
- 구현 검증: 2026-07-13 S1~S2 독립 code review **APPROVE(P0/P1/P2 0)**. 핵심 직접 합의 회귀 68 passed, 전체 3,810 passed, Authority·application 32-way 핵심 경쟁 11개를 30회 반복해 통과했고 pyright 0 errors, ruff·변경 파일 format·diff-check도 green이었다.
- S3 구현 검증: 2026-07-13 독립 code re-review **APPROVE(P0/P1/P2 0)**. Deadlock Manager 처분 전용 89 passed, 관련 독립 묶음 259 passed, 전체 3,965 passed를 확인했다. Registry drift 원인은 후보 순서와 무관한 `candidate_missing > owner_missing > owner_changed > under_claim_changed` 우선순위로 고정했고, drift 호출은 새 vote를 쓰지 않되 같은 fingerprint의 과거 accepted vote를 보존한다. direct/P17.4/S3 void 포트는 exact `None`만 성공으로 인정한다. Ruff와 Pyright도 green이었다.
- S4 구현 검증: 2026-07-13 독립 code re-review **APPROVE(P0/P1/P2 0)**. S4 전용 80 passed, 독립 재리뷰 묶음 122 passed, 전체 4,045 passed를 확인했다. 첫 리뷰에서 발견한 손상 `ConflictCase` 불변식 우회와 supporting `authority=False | 0.0` 정규화 우회는 red 재현 뒤 fresh reconstruction과 exact int 검증으로 닫았다. Pyright 0 errors, Ruff·변경 파일 format·diff-check도 green이었다.
- S5 구현 검증: 2026-07-13 최초 독립 리뷰가 terminal grounding Failed POST 복구, legacy stance 무시, UI 의미, Pyright에서 P1 4건을 찾았다. 모두 red→green으로 닫아 S5 집중 125 passed, 전체 4,068 passed, Pyright 0 errors, Ruff lint·S5 변경 파일 format·diff-check, 프런트 계약 10 passed·tsc·lint·production build를 통과했다. resolved Case의 required-grounding Failed는 exact revision 3과 고정 error code 두 개에만 terminal concurrence 복구를 허용하고, open Case·다른 Failed·변조 revision은 fail-closed한다. 당시 새 reviewer 프로세스는 로컬 미지원 모델 설정으로 기동하지 못해 독립 리뷰를 S6 게이트로 넘겼고, S6에서 별 generic reviewer로 완료했다.
- S6 구현 검증: 2026-07-14 fault matrix가 direct forged claim/wrong secret, FromUnowned·FromDeadlock wrong secret, direct abandon·wake 반환 변조 네 P1을 찾았다. non-mutating reservation proof를 추가해 Authority write 전에 exact claim과 full token을 검증하고, abandon read-back과 wake closed union을 보강해 모두 red→green으로 닫았다. 최종 독립 리뷰가 추가로 찾은 Deadlock sealed Conflict proof의 Authority 선기록 P1과 direct seal 응답 유실 복구의 proof 우회 P2도 red→green으로 보수했다. 재리뷰 결과는 **APPROVE(P0/P1/P2 0)**다. S6 집중 752 passed, 핵심 32-way 행렬 15종의 보수 후 10회 반복 150/150, 전체 4,088 passed, Pyright 0 errors, Ruff lint·S6 변경 파일 format·diff-check를 확인했다. 프런트 계약 10 passed·tsc·lint·production build도 green이다.

> **S1~S6 구현 경계.** 현재 Store·claim·Authority와 control recovery는 단일 애플리케이션 프로세스의 InMemory 상태다. 같은 프로세스 안에서는 Authority 응답 유실, seal·abandon 응답 유실, evidence·Request CAS·Case 전이·wake 사이의 재시도를 보수하지만, 프로세스 재시작 뒤 control token·handle 복구는 보장하지 않는다. 이 내구성은 P17.9가 맡는다. Authority grant 뒤 Registry가 영구적으로 달라지면 direct claim은 sealed 상태를 유지하고 evidence·Request CAS·Case 전이·wake를 쓰지 않은 채 fail-closed한다. 이미 발행된 grant의 철회·대체·무효화 정책은 P17.9 또는 후속 ADR 범위다. reserve callback 검증과 Authority write 뒤 재검증은 각각 그 호출 내부에서만 짧은 Registry snapshot을 선형화 지점으로 쓰며, 애플리케이션 전체에 걸친 바깥 Registry lock은 잡지 않는다. reservation proof는 Store의 같은 lock에서 claim과 full control token/handle을 exact 비교하지만 상태·history를 바꾸지 않으며 외부 Authority까지 하나의 transaction으로 묶는 장치는 아니다. durable adapter는 같은 의미를 token hash와 조건부 CAS로 보존해야 한다. composition은 typed reader·Answer Source·terminal recorder·execution·Conflict/Manager 처분과 proof capability를 같은 객체 identity로 묶고 웹·MCP·두 UI에 연결한다. 이 조립은 demo/단일 프로세스 경계이며, durable linked workflow·다중 인스턴스 lease·production Authority/RBAC는 후속 단계다.

## 맥락

P17.2c-1은 모든 Contested 질문을 먼저 Question Request로 저장하고, Request마다 `ConflictCase` 한 건을 만든 뒤 Request를 `AwaitingConflict`로 옮긴다. 사용자는 책임이 정해질 때까지 본문 없는 Pending을 받는다. 이 경계는 ADR 0042가 정했다.

S0 채택 당시 Owner 처분 경로는 legacy `ConsensusService`를 사용했다. 이 서비스는 서비스 인스턴스 안의 dict에 표를 모으고 Case만 닫았다. 원 Question Request를 재개하지 않았으며, 합의 직후 intent 전역 `Precedent`와 `ComplementEdge`를 기록할 수 있었다. S1~S2가 direct consensus 코어를 대체한 뒤에도 request-aware Deadlock은 ManagerItem을 만들지 않아 Request가 `AwaitingConflict`에 머물렀다. S3는 sealed deadlock claim에서 request-aware `FromDeadlock` ManagerItem을 만들고, Assign/Dismiss가 같은 Request를 재개하거나 명시적으로 거절하도록 이 공백을 닫았다.

legacy 동작을 P17.5에 그대로 연결하면 다음 문제가 생긴다.

- 같은 intent를 가진 서로 다른 Request가 각자 다른 결론을 내릴 수 있는데도 전역 판례가 마지막 결과로 덮인다.
- `on_agent`가 원 Case 후보인지 확인하지 않아 후보 밖 카드를 실행 대상으로 넣을 수 있다.
- 책임자와 request-scoped Authority가 정해지기 전에 Runtime을 호출할 수 있다.
- 합의 뒤 Case만 닫히고 Request가 재개되지 않아 사용자 결과 기준 미아 없음이 깨진다.
- Deadlock 뒤에도 Case가 open이라 Owner 처리함에 남고 재투표를 받을 수 있다.
- Case·Request·ManagerItem·Authority grant·접지 근거가 부분 실패 뒤 서로 다른 결론을 가리킬 수 있다.

P17.5의 목표는 전역 학습이 아니다. 한 Contested Request의 책임을 정하고, 같은 질문을 실행하거나 명시적으로 거절하는 데 있다. 학습 범위를 정하지 않은 전역 `Precedent`와 `ComplementEdge`는 P17.10 전까지 쓰지 않는다.

## 결정

### 1. 범위와 불변식

P17.5는 세 경로를 닫는다.

```text
Owner 전원 일치
  → request-scoped Authority grant
  → 같은 Question Request 재개
  → primary 확정 뒤 다중 접지

Owner 의견 불일치
  → request-aware FromDeadlock ManagerItem
  → Manager Assign 또는 Dismiss
  → 같은 Question Request 재개 또는 Declined

후보 카드·Owner·under-claim 변경
  → 기존 표를 동의로 해석하지 않고 conflict escalation
  → Manager/root Assign 또는 Dismiss
  → 같은 Question Request 재개 또는 Declined
```

다음 불변식을 지킨다.

1. 한 Question Request에는 request-aware ConflictCase가 정확히 하나다.
2. 같은 intent의 다른 Request는 같은 Case로 합치지 않는다.
3. `on_agent`와 Manager Assign 대상은 원 Case 후보에 한정한다.
4. 현재 Registry의 카드·Owner User·under-claim·`cannot_answer`·`approval_when`을 실행 전까지 다시 확인한다.
5. direct consensus와 Manager mediation은 서로 다른 request-scoped Authority provenance를 남긴다.
6. `ConflictResolutionEvidence`가 저장되기 전에는 Request를 `ReadyToDispatch`로 옮기지 않는다.
7. primary가 정해지기 전에는 Runtime과 Answer Finalization을 호출하지 않는다.
8. supporting은 지식 출처일 뿐 책임자나 승인자가 아니다. Authority 값은 0이다.
9. Router를 다시 호출하지 않고 같은 Request를 `attempt=1`로 재개한다.
10. P17.5 경로는 전역 `Precedent`와 `ComplementEdge`를 기록하지 않는다.
11. 후보 Registry drift는 현재 invocation을 새 표로 추가하지 않은 채 claim-bound escalation으로 수렴한다. stale Case를 영구 open으로 방치하지 않는다.

### 2. request-aware Case와 legacy intent 중복 제거를 분리한다

`RequestAwareConflictCaseStore.create_or_get_for_request`가 request-aware Case의 유일한 생성 관문이다. semantic fingerprint는 `request_id`, intent, 질문 원문, 중복 없는 후보 `(agent_id, owner)` 튜플을 비교한다. 생성 ID·시각·진행 상태는 fingerprint에서 제외하되 후보 중복을 set 변환으로 숨기지 않는다.

저장소는 다음 경계를 강제한다.

- `_by_request`는 Case가 terminal이 된 뒤에도 최신본을 보존한다.
- `open_case`와 `mark_resolved` 같은 legacy mutator는 `request_id is not None`인 Case를 거부한다.
- `open_for_intent`는 `request_id is None`인 legacy Case만 찾는다.
- legacy `get(case_id)`는 기존처럼 open Case만 반환한다.
- `get_request_case(case_id)`는 request-aware Case의 최신 open·escalated·terminal 값을 case ID로 읽는다. legacy Case에는 `None`을 반환한다.
- request-aware Case는 claim-bound 전이 포트로만 바꾼다.
- 조회값과 history는 backing state와 분리된 canonical deep copy다.
- `case_id`와 `request_id`가 다른 Case에 재사용되면 integrity 오류로 멈춘다.

이 규칙은 legacy intent 단위 Case와 P17 Request 단위 Case가 서로를 중복으로 오인하는 일을 막는다.

legacy 서비스도 fail-closed한다. `ConsensusService.concur`는 `case_store.get(case_id)` 직후, 후보 검증이나 `_votes` 접근보다 먼저 `case.request_id is not None`을 검사해 request-aware Case를 거부한다. 이 guard보다 앞이나 guard 실패 뒤에는 vote dict write, `PrecedentStore.record`, `EdgeStore.record`, `mark_resolved` 호출이 하나도 없어야 한다. 웹 분기만 믿지 않고 direct service 호출 spy로 네 side effect가 모두 0임을 검증한다.

### 3. ConflictCase는 네 상태를 가진다

`open | resolved`만으로는 Deadlock과 후보 Registry drift를 표현할 수 없다. Manager에게 넘긴 Case가 open으로 남으면 Owner 처리함과 concurrence API가 계속 받아들이기 때문이다. request-aware Case는 `concurrence_round=1`을 명시 필드로 가진 채 다음 네 상태를 쓴다.

```python
CaseStatus = Literal["open", "escalated", "resolved", "declined"]
```

상태별 값 조합은 다음과 같다.

| 상태 | `concurrence_round` | `resolution` | `manager_item_id` | `decline_reason` |
|---|---|---|---|---|
| `open` | 1 이상의 현재 round | `None` | `None` | `None` |
| `escalated` | escalation이 선점한 마지막 round | `None` | nonblank | `None` |
| `resolved` | 책임을 확정한 round | 필수 | direct면 `None`, mediation이면 nonblank | `None` |
| `declined` | escalation이 선점한 마지막 round | `None` | nonblank | `manager_declined` |

허용 전이는 다음 상태쌍뿐이다. `open → escalated`는 원인 타입에 따라 두 행으로 나눈다.

```text
open(round=n) ── Authority write0 ──> open(round=n+1)
open ── direct consensus ───────────> resolved
open ── DivergentVotes ─────────────> escalated
open ── CandidateRegistryChanged ───> escalated
escalated ── Manager Assign ─> resolved
escalated ── Manager Dismiss ─> declined
```

`open(round=n) → open(round=n+1)`은 Authority가 policy deny와 write 0을 typed result로 보증한 경우에만 claim-bound로 허용한다. active votes는 Case 필드가 아니라 별 backing과 append-only history에 두며, round를 올릴 때 현재 active votes만 폐기한다. 이전 round의 vote·claim history는 남는다.

terminal Case를 덮어쓰거나 `escalated → open`으로 되돌리지 않는다. `open_for_owner`는 `open`만 반환한다. escalation 뒤 Owner가 같은 Case에 다시 표를 내면 conflict다.

escalation 원인은 닫힌 타입으로 보존한다.

```python
class DivergentVotes(FrozenDto):
    kind: Literal["divergent_votes"]
    round: int

class CandidateRegistryChanged(FrozenDto):
    kind: Literal["candidate_registry_changed"]
    round: int
    reason_code: Literal[
        "candidate_missing",
        "owner_missing",
        "owner_changed",
        "under_claim_changed",
    ]

ConflictEscalationCause = Annotated[
    DivergentVotes | CandidateRegistryChanged,
    Field(discriminator="kind"),
]
```

저장 snapshot Owner 또는 원 후보 카드의 현재 Owner가 인증된 action을 보내면 Registry drift를 표보다 먼저 검사한다. 둘 중 어느 쪽과도 관계없는 principal은 Forbidden이다. drift가 확인되면 현재 invocation을 새 표로 저장하지 않고, 이전 표도 새 동의로 해석하지 않은 채 `CandidateRegistryChanged` claim을 선점해 Manager/root로 넘긴다. 자동 rebind는 하지 않는다. 사람이 action을 보내지 않은 Case를 찾는 system recovery scan은 P17.9 범위다.

한 action에서 drift 원인이 여러 개면 `candidate_missing → owner_missing → owner_changed → under_claim_changed` 순서로 첫 code를 택한다. 현재 카드가 intent를 더는 under-claim하지 않거나 `cannot_answer`로 바뀐 경우는 `under_claim_changed`다. 이 우선순위는 동시 검증과 재시도에서 cause fingerprint가 달라지지 않게 한다.

### 4. Owner 입력은 round에 묶인 1급 concurrence evidence다

애플리케이션 명령은 인증 계층이 확정한 Owner principal을 받는다.

```python
class OwnerPrincipal(FrozenDto):
    org_id: str
    subject_id: str

class ConcurOnConflict(FrozenDto):
    kind: Literal["concur_on_conflict"]
    principal: OwnerPrincipal
    case_id: str
    expected_round: int
    on_agent: str
    stance: Literal["withdraw", "keep_as_complement"] = "withdraw"
    rationale: str = ""
```

도메인 명령의 `expected_round`는 필수다. 웹 DTO `ConcurRequest`는 하위호환을 위해 `expected_round: int | None = None`과 `stance`를 additive 필드로 받는다. request-aware Case에는 1 이상의 `expected_round`가 반드시 있어야 하고, legacy Case는 두 필드를 모두 무시한다. `expected_round`는 Authority가 write 0으로 명시 거부해 새 round가 열린 뒤, 오래된 HTTP 재시도가 새 round의 표로 들어가는 ABA를 막는다.

표는 서비스 인스턴스 dict가 아니라 Conflict Store에 저장한다.

```python
class OwnerConcurrenceEvidence(FrozenDto):
    round: int
    owner_id: str
    on_agent: str
    stance: Literal["withdraw", "keep_as_complement"]
    rationale: str = ""
```

한 round의 규칙은 다음과 같다.

- principal의 조직은 Request 조직과 같아야 한다.
- normal vote는 Case snapshot Owner와 현재 카드 Owner가 같고 principal이 그 Owner일 때만 받는다.
- Case snapshot Owner와 현재 카드 Owner가 다르면, 인증된 저장 Owner 또는 현재 원 후보 Owner의 action만 drift trigger로 받는다. 현재 invocation은 새 표로 기록하지 않고 `CandidateRegistryChanged` escalation claim만 만든다. 같은 fingerprint의 과거 accepted vote는 history에 남을 수 있다.
- `on_agent`는 원 Case 후보여야 한다.
- 현재 후보 카드와 Owner User가 모두 존재하고, 후보 카드는 해당 intent를 under-claim하며 `cannot_answer`에 넣지 않아야 한다.
- 같은 Owner·round·같은 표는 멱등이다.
- 같은 Owner·round의 다른 표는 명시적 conflict다.
- 후보 Owner를 중복 제거한 전원이 표를 냈을 때만 round 결론을 만든다.
- 모든 `on_agent`가 같으면 direct consensus, 둘 이상이면 Deadlock이다.

request-aware Case projection은 `case_id`, `request_id`, 상태와 canonical `current_round`를 싣는다. Owner 처리함의 정적 UI와 Next.js UI는 이 projection을 별도 계산 없이 읽고, 같은 `current_round`를 `expected_round`로 POST한다.

기존 `ConsensusOutcome.Agreed`는 P17.5 결과로 쓰지 않는다. 이 타입은 `Precedent`를 필수로 안기 때문이다. P17.5의 애플리케이션 결과를 다음 닫힌 타입으로 고정한다.

```python
class ConcurrencePending(FrozenDto):
    kind: Literal["concurrence_pending"] = "concurrence_pending"
    request_id: str
    case_id: str
    current_round: int
    pending_owners: tuple[str, ...]

class ConsensusRouteRejected(FrozenDto):
    kind: Literal["consensus_route_rejected"] = "consensus_route_rejected"
    request_id: str
    case_id: str
    current_round: int
    next_round: int
    reason_code: str

class ConflictResolved(FrozenDto):
    kind: Literal["conflict_resolved"] = "conflict_resolved"
    request_id: str
    case_id: str
    route: RouteTarget
    wake: ExecutionWake

class ConflictEscalated(FrozenDto):
    kind: Literal["conflict_escalated"] = "conflict_escalated"
    request_id: str
    case_id: str
    cause: ConflictEscalationCause
    manager_item_id: str

P17DirectConcurrenceResult = Annotated[
    ConcurrencePending | ConsensusRouteRejected | ConflictResolved,
    Field(discriminator="kind"),
]

P17ConcurrenceResult = Annotated[
    ConcurrencePending
    | ConsensusRouteRejected
    | ConflictResolved
    | ConflictEscalated,
    Field(discriminator="kind"),
]

class P17DirectConflictDispositionApplication:
    def concur(
        self,
        command: ConcurOnConflict,
    ) -> P17DirectConcurrenceResult: ...

class P17ConflictDispositionApplication:
    def concur(
        self,
        command: ConcurOnConflict,
    ) -> P17ConcurrenceResult: ...
```

S2의 direct-only application은 Pending·Authority reject·direct resolve만 반환한다. sealed Deadlock/drift claim을 만나면 `ConflictDispositionInProgress`를 내고 `ConflictEscalated`로 가장하지 않는다. S3의 full application은 같은 direct 코어와 escalation application을 조합한다. `ConsensusRouteRejected.current_round`는 거부된 round, `next_round`는 Case에 commit된 새 round다. `ConflictEscalated`는 ManagerItem 생성, Case `escalated`, Request `AwaitingManager(contested)`가 모두 exact-link된 뒤에만 반환한다.

### 5. concurrence 결론은 generation-bound claim이다

마지막 표가 들어온 순간 Store가 round 결론을 선점한다. claim은 다음 sealed sum이다.

```text
ReservedConsensusClaim | SealedConsensusClaim
ReservedDeadlockClaim  | SealedDeadlockClaim
```

`ReservedDeadlockClaim`은 Store가 `SealedDeadlockClaim`을 만들기 직전 같은 임계구역 안에서만 쓰는 내부 값이다. Deadlock과 Registry drift에는 외부 Authority write가 없으므로 reserved 상태를 현재 claim으로 노출하거나 caller에게 반환하지 않는다. Store는 deadlock claim의 reserve와 seal을 한 임계구역에서 끝내 `SealedConflictClaimAvailable`을 돌려준다. direct consensus만 `ReservedConsensusClaim`과 control token을 caller에게 내보낸다.

공통 payload는 다음 값을 가진다.

```text
generation
idempotency_key = conflict-disposition:{case_id}:{round}
request_id / case_id / org_id / intent / round
candidate snapshot / canonical vote set
trigger action fingerprint
direct면 primary와 requires_approval
escalation이면 ConflictEscalationCause
```

필드 이름과 타입은 다음으로 고정한다.

```python
class ConcurrenceActionFingerprint(FrozenDto):
    case_id: str
    org_id: str
    owner_id: str
    expected_round: int
    on_agent: str
    stance: Literal["withdraw", "keep_as_complement"]
    rationale: str

class ReservedConsensusClaim(FrozenDto):
    kind: Literal["reserved_consensus"] = "reserved_consensus"
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    org_id: str
    intent: str
    round: int
    candidate_snapshot: tuple[Candidate, ...]
    votes: tuple[OwnerConcurrenceEvidence, ...]
    trigger: ConcurrenceActionFingerprint
    primary: str
    requires_approval: bool

class SealedConsensusClaim(FrozenDto):
    kind: Literal["sealed_consensus"] = "sealed_consensus"
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    org_id: str
    intent: str
    round: int
    candidate_snapshot: tuple[Candidate, ...]
    votes: tuple[OwnerConcurrenceEvidence, ...]
    trigger: ConcurrenceActionFingerprint
    primary: str
    requires_approval: bool

class ReservedDeadlockClaim(FrozenDto):
    kind: Literal["reserved_deadlock"] = "reserved_deadlock"
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    org_id: str
    intent: str
    round: int
    candidate_snapshot: tuple[Candidate, ...]
    votes: tuple[OwnerConcurrenceEvidence, ...]
    trigger: ConcurrenceActionFingerprint
    cause: ConflictEscalationCause

class SealedDeadlockClaim(FrozenDto):
    kind: Literal["sealed_deadlock"] = "sealed_deadlock"
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    org_id: str
    intent: str
    round: int
    candidate_snapshot: tuple[Candidate, ...]
    votes: tuple[OwnerConcurrenceEvidence, ...]
    trigger: ConcurrenceActionFingerprint
    cause: ConflictEscalationCause

ConflictDispositionClaim = Annotated[
    ReservedConsensusClaim
    | SealedConsensusClaim
    | ReservedDeadlockClaim
    | SealedDeadlockClaim,
    Field(discriminator="kind"),
]
```

모든 claim은 `idempotency_key == f"conflict-disposition:{case_id}:{round}"`여야 한다. `votes`의 모든 round는 claim round와 같고 Owner는 유일하며, 순서는 Case candidate에서 Owner ID를 처음 만난 순서다. `trigger`의 Case·조직·round는 claim과 같고, normal vote 결론이면 trigger가 canonical votes에 exact 포함돼야 한다. consensus의 `primary`는 모든 vote target과 같고 원 후보여야 한다. deadlock의 `DivergentVotes`는 둘 이상의 target과 trigger의 canonical vote 포함을 조합 validator로 강제한다. `CandidateRegistryChanged`는 현재 drift-triggering invocation을 새 vote로 append하지 않는다. 다만 fingerprint에 invocation ID가 없으므로 내용이 같은 과거 accepted vote는 canonical `votes`에 남을 수 있으며, trigger와 votes의 비포함 관계를 validator로 강제하지 않는다. sealed 값은 `kind` 외 모든 payload가 reserved와 exact equality다.

vote·claim progress는 `ConflictCase`와 분리된 Store backing과 history에 둔다. Case에는 현재 `concurrence_round`와 수명 상태만 남긴다. manager_id는 S1 claim이 임의로 정하지 않는다. S3가 cause, 현재 후보 Owner와 조직 그래프, root fallback을 함께 검증해 ManagerItem을 만들 때 확정한다.

애플리케이션의 Registry 검증과 Store write 사이에 다른 action이 끼지 않도록 검증 결과와 원자 반환 타입을 닫는다.

```python
class ValidatedOwnerVote(FrozenDto):
    kind: Literal["validated_owner_vote"]
    request_id: str
    case_id: str
    org_id: str
    intent: str
    candidate_snapshot: tuple[Candidate, ...]
    trigger: ConcurrenceActionFingerprint
    evidence: OwnerConcurrenceEvidence
    target_requires_approval: bool

class ValidatedRegistryEscalation(FrozenDto):
    kind: Literal["validated_registry_escalation"]
    request_id: str
    case_id: str
    org_id: str
    intent: str
    candidate_snapshot: tuple[Candidate, ...]
    trigger: ConcurrenceActionFingerprint
    cause: CandidateRegistryChanged

ValidatedConcurrence = Annotated[
    ValidatedOwnerVote | ValidatedRegistryEscalation,
    Field(discriminator="kind"),
]

class ConflictReservationControlToken(FrozenDto):
    generation: str
    token: str

class ConflictSealedClaimHandle(FrozenDto):
    generation: str
    forward_token: str

class ConcurrencePendingStored(FrozenDto):
    kind: Literal["pending"]
    current_round: int
    pending_owners: tuple[str, ...]

class ConflictClaimAcquired(FrozenDto):
    kind: Literal["acquired"]
    claim: ReservedConsensusClaim
    control_token: ConflictReservationControlToken

class ConflictClaimInProgress(FrozenDto):
    kind: Literal["in_progress"]
    retryable: Literal[True]

class SealedConflictClaimAvailable(FrozenDto):
    kind: Literal["sealed"]
    claim: SealedConsensusClaim | SealedDeadlockClaim
    handle: ConflictSealedClaimHandle

class ConflictClaimConflict(FrozenDto):
    kind: Literal["conflict"]

ConflictConcurrenceAttempt = Annotated[
    ConcurrencePendingStored
    | ConflictClaimAcquired
    | ConflictClaimInProgress
    | SealedConflictClaimAvailable
    | ConflictClaimConflict,
    Field(discriminator="kind"),
]
```

`ConflictClaimAcquired`는 claim과 control token의 generation이 같아야 하고, `SealedConflictClaimAvailable`은 claim과 full handle의 generation이 같아야 한다. 이 validator는 token 문자열과 forward token의 Store exact-read를 대신하지 않는다.

control token은 `generation`과 불투명 `token`, sealed handle은 같은 `generation`과 불투명 `forward_token`을 가진다. claim은 `ConcurrenceActionFingerprint`를 보존한다. 그래서 현재 drift invocation을 새 표로 append하지 않아도 같은 action 재시도와 다른 action을 구분할 수 있다.

Store는 `case_id`와 canonical command를 먼저 읽고 current Case가 request-aware `open`, `expected_round == concurrence_round`인지 확인한다. 그 뒤 같은 Case lock 안에서 `validate(canonical_case, canonical_command)`를 정확히 한 번 호출한다. callback 입력과 반환값은 deep copy하며, 반환값은 exact `ValidatedOwnerVote | ValidatedRegistryEscalation` 타입이어야 한다. request·Case·조직·intent·candidate snapshot·trigger·round·Owner·target·stance·rationale가 입력과 맞지 않으면 integrity 오류다. callback 예외·재진입·타입 치환·기존 generation 재사용은 vote·claim·progress history를 하나도 쓰지 않고 fail-closed한다.

`ValidatedOwnerVote`면 같은 Owner·round의 기존 evidence와 원자 비교한다. 같은 표는 멱등이고 다른 표는 `ConflictClaimConflict`다. 전원 표가 아니면 evidence를 한 번 기록하고 `ConcurrencePendingStored`를 반환한다. 전원 target이 같으면 `ReservedConsensusClaim`과 control token을 한 번만 저장해 `ConflictClaimAcquired`를 반환한다. target이 갈리면 마지막 evidence, `ReservedDeadlockClaim`, `SealedDeadlockClaim`, forward handle을 같은 임계구역에서 순서대로 기록하고 `SealedConflictClaimAvailable`을 반환한다. `ValidatedRegistryEscalation`은 현재 invocation을 새 vote로 append하지 않고 deadlock claim reserve·seal과 handle만 같은 임계구역에서 기록한다. 같은 fingerprint의 과거 accepted vote가 있으면 그 history와 canonical vote는 보존한다.

consensus·`DivergentVotes` claim에서는 canonical vote set에 exact 포함된 기존 표의 재시도를 같은 결론의 follower로 본다. direct claim의 reserved 구간이면 `ConflictClaimInProgress`, sealed 뒤면 같은 handle의 `SealedConflictClaimAvailable`을 돌려준다. 같은 Owner의 다른 표는 항상 `ConflictClaimConflict`다. `CandidateRegistryChanged`는 exact trigger fingerprint만 follower로 본다. trigger와 fingerprint가 같은 과거 accepted active vote는 claim의 canonical votes에 남을 수 있지만, 현재 drift invocation을 표로 다시 append하지 않는다. trigger와 다른 이전 active vote의 재시도는 escalation 동의로 해석하지 않고 conflict다. cause·generation이 다르거나 이 규칙에 들지 않는 action도 conflict다. 모든 반환값은 backing과 분리된 canonical copy다.

Case 전이 history와 vote·claim 진행 history는 섞지 않는다. 기존 `history`는 `ConflictCase` snapshot의 append-only deep copy다. 별도 진행 history는 다음 frozen entry union을 Case별 순서로 보존한다.

```python
class ConcurrenceVoteStored(FrozenDto):
    kind: Literal["vote_stored"]
    position: int
    case_id: str
    request_id: str
    round: int
    evidence: OwnerConcurrenceEvidence

class ConflictClaimReserved(FrozenDto):
    kind: Literal["claim_reserved"]
    position: int
    case_id: str
    request_id: str
    round: int
    claim: ReservedConsensusClaim | ReservedDeadlockClaim

class ConflictClaimSealed(FrozenDto):
    kind: Literal["claim_sealed"]
    position: int
    case_id: str
    request_id: str
    round: int
    claim: SealedConsensusClaim | SealedDeadlockClaim

class ConsensusRoundAbandoned(FrozenDto):
    kind: Literal["round_abandoned"]
    position: int
    case_id: str
    request_id: str
    from_round: int
    to_round: int
    generation: str
    reason_code: Literal["authority_rejected_write_zero"]

class ConflictResolutionEvidenceRecorded(FrozenDto):
    kind: Literal["resolution_evidence_recorded"]
    position: int
    case_id: str
    request_id: str
    round: int
    evidence: ConflictResolutionEvidence

ConflictProgressEntry = Annotated[
    ConcurrenceVoteStored
    | ConflictClaimReserved
    | ConflictClaimSealed
    | ConsensusRoundAbandoned
    | ConflictResolutionEvidenceRecorded,
    Field(discriminator="kind"),
]
```

`position`은 Case 안에서 1부터 단조 증가한다. `progress_history_for_case(case_id)`는 tuple deep snapshot을 돌려주며, terminal Case 뒤에도 이전 round를 포함한 전체 순서를 유지한다. append-only history는 감사 로그가 아니고 claim 복구·테스트 증거다.

Store seam은 P17.4의 검증된 generation 패턴을 따른다.

```python
class RequestAwareConflictDispositionStore(RequestAwareConflictCaseStore, Protocol):
    def reserve_validated_concurrence(
        self,
        case_id: str,
        command: ConcurOnConflict,
        *,
        validate: Callable[[ConflictCase, ConcurOnConflict], ValidatedConcurrence],
    ) -> ConflictConcurrenceAttempt: ...

    def claim_for_case(
        self,
        case_id: str,
    ) -> ReservedConsensusClaim | SealedConsensusClaim | SealedDeadlockClaim | None: ...

    def sealed_claim_for_case(
        self,
        case_id: str,
    ) -> SealedConflictClaimAvailable | None: ...

    def get_request_case(self, case_id: str) -> ConflictCase | None: ...

    def progress_history_for_case(
        self,
        case_id: str,
    ) -> tuple[ConflictProgressEntry, ...]: ...

    def seal_consensus_claim(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
    ) -> SealedConflictClaimAvailable: ...

    def abandon_unmutated_consensus_round(
        self,
        claim: ReservedConsensusClaim,
        *,
        control_token: ConflictReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> ConflictCase: ...

    def record_resolution_evidence(
        self,
        handle: ConflictSealedClaimHandle,
        evidence: ConflictResolutionEvidence,
    ) -> None: ...

    def resolution_evidence_for_request(
        self,
        request_id: str,
    ) -> ConflictResolutionEvidence | None: ...

    def transition_for_claim(
        self,
        handle: ConflictSealedClaimHandle,
        *,
        target: ConflictCase,
    ) -> ConflictCase: ...
```

`seal_consensus_claim`은 저장된 reserved direct claim과 control token 전체(`generation + token`)가 exact할 때만 같은 payload의 sealed claim과 handle을 만든다. `sealed_claim_for_case`는 현재 claim이 sealed이고 저장된 full handle이 있을 때 둘의 canonical deep copy를 함께 돌려준다. claim이 없거나 아직 reserved면 `None`, sealed claim은 있는데 handle이 없거나 generation이 다르면 integrity 오류다. `abandon_unmutated_consensus_round`도 같은 reserved claim·full token, claim idempotency key와 맞는 exact `RequestRouteGrantRejected(authority_write_applied=False, idempotency_write_applied=False)`, resolution evidence와 Case 전이 부재를 모두 확인할 때만 active votes와 current claim을 지운 뒤 round를 정확히 1 올린다. generation tombstone과 progress history는 지우지 않는다. `record_resolution_evidence`와 `transition_for_claim`은 저장된 sealed claim과 full handle(`generation + forward_token`)을 exact 비교한다. 같은 generation이라도 token 문자열이 다르거나 과거 generation의 handle이면 거부한다. `transition_for_claim`은 direct handle의 `open → resolved`, deadlock handle의 `open → escalated`만 허용한다. conflict handle만으로 사람이 내린 terminal 처분을 만들 수는 없다.

같은 full handle·같은 `ConflictResolutionEvidence`의 재기록은 성공 no-op이며 `ConflictResolutionEvidenceRecorded`를 다시 append하지 않는다. 같은 Request에 다른 evidence가 있거나 같은 evidence를 다른 handle로 쓰면 integrity 오류이고 write는 0이다.

direct consensus의 첫 caller만 control token을 받는다. 같은 표의 follower는 reserved 동안 retryable in-progress, sealed 뒤에는 generation-bound forward handle을 받는다. Deadlock·Registry drift의 첫 caller는 같은 임계구역에서 만들어진 sealed claim과 handle을 바로 받는다. 다른 표나 다른 결론은 conflict다. generation이 바뀌면 과거 token과 handle은 효력을 잃는다.

Authority가 `RequestRouteGrantRejected`로 policy deny와 write 0을 보증할 때만 reserved consensus claim을 지우고 active votes를 비운 뒤 Case를 `open(round=n+1)`로 바꾼다. timeout이나 연결 단절은 write 0을 뜻하지 않는다. `RequestRouteGrantConflict`, 결과 불명, receipt 반환 뒤 오류는 claim을 abandon하거나 round를 reset하지 않는다. claim을 seal한 채 같은 action만 forward retry하거나 명시적 conflict로 끝낸다.

`DivergentVotes`와 `CandidateRegistryChanged`는 Authority write가 없으므로 escalation claim을 바로 seal한다. S1은 이 내부 claim까지만 만들 수 있다. S3가 ManagerItem 생성, Case escalation, Request 전이를 같은 claim으로 닫은 뒤에야 외부 `ConflictEscalated`를 반환한다.

### 6. request-scoped Authority provenance는 sealed sum이다

P17.4의 `item_id`와 `assigned_by`를 direct consensus에 가짜 값으로 넣지 않는다. request-scoped grant의 출처를 타입으로 구분한다.

```python
class FromUnownedManagerGrant(FrozenDto):
    kind: Literal["unowned_manager"]
    item_id: str
    by_manager: str

class FromOwnerConsensusGrant(FrozenDto):
    kind: Literal["owner_consensus"]
    case_id: str
    round: int

class FromDeadlockManagerGrant(FrozenDto):
    kind: Literal["deadlock_manager"]
    case_id: str
    item_id: str
    by_manager: str

RequestRouteGrantSource = Annotated[
    FromUnownedManagerGrant | FromOwnerConsensusGrant | FromDeadlockManagerGrant,
    Field(discriminator="kind"),
]
```

```python
class RequestRouteGrantAssignment(FrozenDto):
    org_id: str
    request_id: str
    intent: str
    agent_id: str
    source: RequestRouteGrantSource
    idempotency_key: str

class RequestRouteGrantReceipt(FrozenDto):
    kind: Literal["receipt"] = "receipt"
    assignment: RequestRouteGrantAssignment
    grant_version: str

class RequestRouteGrantRejected(FrozenDto):
    kind: Literal["rejected"] = "rejected"
    idempotency_key: str
    authority_write_applied: Literal[False] = False
    idempotency_write_applied: Literal[False] = False
    reason_code: str

class RequestRouteGrantConflict(FrozenDto):
    kind: Literal["conflict"] = "conflict"

RequestRouteGrantResult = Annotated[
    RequestRouteGrantReceipt | RequestRouteGrantRejected | RequestRouteGrantConflict,
    Field(discriminator="kind"),
]

class RequestRouteAuthority(RouteAuthority, Protocol):
    def grant_for_request(
        self,
        assignment: RequestRouteGrantAssignment,
    ) -> RequestRouteGrantResult: ...

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None: ...
```

`RequestRouteGrantRejected`는 요청한 idempotency key를 반사하되 policy deny로 Authority bytes, key row, receipt를 하나도 쓰지 않았다는 typed 보증이다. 반사된 key와 reserved claim key가 exact한 이 결과에서만 consensus claim abandon과 round 증가를 허용한다. `RequestRouteGrantConflict`는 `(org_id, request_id)` first-winner slot에 다른 assignment가 있거나 같은 idempotency key의 payload가 다른 경우다. conflict는 write 0 정책 거부가 아니므로 claim을 abandon하지 않고 seal/conflict로 남긴다. 예외도 write 0으로 해석하지 않는다.

Authority 저장소는 idempotency key뿐 아니라 `(org_id, request_id)`의 단일 first-winner slot을 지킨다. 같은 slot·같은 canonical assignment는 같은 receipt와 version을 돌려주고, target·provenance·key 중 하나라도 다르면 `RequestRouteGrantConflict`다. direct consensus key는 `conflict-disposition:{case_id}:{round}`, Manager key는 기존 `manager-disposition:{item_id}`를 쓴다.

receipt의 assignment와 입력은 exact equality여야 한다. writer 호출 뒤 같은 객체의 `authorize_for_request`를 읽어 `policy_version == receipt.grant_version`을 확인한다. request-scoped grant는 base policy나 다른 Request를 바꾸지 않는다.

P17.4의 `assign_owner(AuthorityAssignment)`는 같은 내부 `RequestRouteGrantAssignment(source=FromUnownedManagerGrant(...))`를 쓰는 compatibility facade다. 외부 계약은 그대로 보존한다.

- 성공 시 `AuthorityAssignmentReceipt.assignment == 호출자가 준 원 AuthorityAssignment`여야 한다. 내부 공통 assignment를 그대로 노출하지 않는다.
- policy deny는 기존 `AuthorityAssignmentRejected(authority_write_applied=False, idempotency_write_applied=False, reason_code)`로 투영한다.
- 내부 `RequestRouteGrantConflict`는 기존 `AuthorityAssignmentConflictError`로 투영한다.
- timeout·연결 단절·그 밖의 예외 계약과 `authorize_for_request` 결과는 P17.4와 같아야 한다.

이 투영과 exact equality는 P17.4 회귀 테스트로 고정한다. facade가 별도 grant map이나 version 계열을 만들면 안 된다.

### 7. direct consensus는 evidence를 먼저 남기고 같은 Request를 재개한다

처리 순서는 다음과 같다.

```text
Owner principal·Request·get_request_case Case exact-link
→ 현재 Registry에서 후보·Owner·under-claim drift 선검사
→ concurrence 저장과 consensus claim 선점
→ FromOwnerConsensusGrant write
→ receipt와 authorize_for_request read-back exact 검증
→ Registry 재검증
→ ConflictResolutionEvidence 저장
→ AwaitingConflict → ReadyToDispatch(attempt=1) CAS
→ Case open → resolved
→ execution starter
```

`RouteTarget`은 저장된 intent, 합의 primary, 현재 카드의 `approval_when`, request grant version으로 만든다. trigger key는 `request-dispatch:{request_id}:1`, Handling Assignment ref도 같은 값이다. `initial_disposition`은 `contested`로 남긴다.

`Resolution.rationale`은 vote dict 삽입 순서나 동시 도착 순서를 쓰지 않는다. Case의 candidate Owner order에서 Owner ID를 처음 만난 순서로 중복 제거하고, 그 순서대로 `owner→on_agent`를 조립한다. 같은 후보·표 집합이면 모든 실행에서 같은 rationale이 나온다.

`ConflictResolutionEvidence`를 Request CAS보다 먼저 저장한다. CAS 직후 recovery runner가 실행하더라도 Answer Source는 책임·접지 근거를 찾을 수 있다. evidence 저장 뒤 실패하면 같은 sealed handle이 Request CAS, Case terminal 전이, wake만 보수한다. 새 Router와 새 Request는 만들지 않는다.

같은 direct action의 재시도는 legacy open-only `get`이 아니라 `get_request_case(case_id)`로 terminal Case까지 읽는다. Case resolve 뒤 wake나 HTTP 응답이 실패해도 sealed claim·resolution evidence·Request 상태를 exact 비교해 repair한다. Request가 `AnsweredRequest`라면 같은 Completion Reader의 completion이 request ID·record ID·route·attempt·Authority version과 정확히 맞아야 같은 action의 terminal 성공으로 인정한다. Answered 상태만 보거나 다른 Reader를 쓰는 것은 integrity 오류다.

### 8. vote divergence와 후보 Registry drift는 request-aware FromDeadlock으로 넘긴다

`DivergentVotes` 또는 `CandidateRegistryChanged` escalation claim을 seal한 뒤 다음 순서로 전이한다.

```text
cause와 현재 조직 그래프로 manager_id 또는 root 확정
request-aware FromDeadlock ManagerItem create-or-get
→ Case open → escalated(manager_item_id)
→ Request AwaitingConflict → AwaitingManager(public_kind="contested") CAS
```

최초 Contested 라우팅은 `Received/revision 0 → AwaitingConflict/revision 1`이다. 따라서
위 escalation CAS는 정확히 `revision 1 → 2`이고, 뒤의 Manager Assign·Dismiss는
`AwaitingManager/revision 2 → ReadyToDispatch | DeclinedRequest/revision 3`이다. P17.4
Unowned의 `revision 1 → 2` 재개 증거를 Deadlock에 그대로 하드코딩하지 않는다. Deadlock
Assign의 `ResumeEvidence`는 `from_revision=2`, `to_revision=3`, `attempt=1`,
`trigger_key=request-dispatch:{request_id}:1`을 exact 보존한다. escalation과 Dismiss에는
실행 재개가 없으므로 `ResumeEvidence`를 기록하지 않는다.

ManagerItem은 기존 `FromDeadlock`을 쓰며 별도 `ManagerItem.intent`를 만들지 않는다. intent 단일 원천은 nested `FromDeadlock.case.intent`다. source는 `ConflictEscalationCause`를 보존하고, 사람용 `reason`이 필요하면 cause의 고정 code에서 투영한다. 외부 식별자를 나열한 자유 문자열은 fingerprint로 쓰지 않는다. request-aware create-or-get fingerprint는 다음 값만 순서대로 비교한다.

```text
request_id
manager_id
source.case.request_id / case_id / intent / question / candidates / concurrence_round
source.cause
```

`item_id`·`created_at`은 재시도마다 달라질 수 있으므로 제외하고, `reason`은 cause에서
결정론적으로 투영되므로 제외한다. request-aware `FromDeadlock` source는 Case가
`open`, `resolution=None`, `manager_item_id=None`, `decline_reason=None`인 escalation 직전
snapshot이어야 한다. `cause`는 필수이고 `cause.round == case.concurrence_round`여야 하며,
`reason`은 `DivergentVotes → "divergent_votes"`, `CandidateRegistryChanged →
"candidate_registry_changed:{reason_code}"`와 정확히 같아야 한다. `request_id is None`인
legacy source는 기존 `cause=None`·free-form reason 계약을 유지한다. 같은 Request에 이미
FromUnowned Item이 있거나 위 fingerprint가 다른 FromDeadlock Item이 있으면
`LinkedEntityMismatchError`로 멈춘다.

manager_id는 S3가 cause와 현재 Registry를 함께 읽어 정한다. candidate order로 원
`agent_id`를 순회해 현재 카드, 그 카드의 현재 Owner User, intent under-claim과
`cannot_answer`를 모두 통과한 첫 카드를 고른다. 그 현재 Owner User의 한 단계 Manager가
현재 Registry에 있으면 그 User ID를 쓰고, Manager가 없거나 조회할 수 없거나 유효한
후보 카드가 하나도 없으면 주입된 root User ID로 보정한다. snapshot Owner가 바뀐 경우에도
Case 후보를 자동 rebind하지 않으며 manager 선택에만 현재 카드 Owner를 읽는다. root도
현재 Registry에 존재하는 User여야 한다. LCA와 멀티홉은 이 ADR 범위가 아니다.

한 번 request-aware ManagerItem이 생성되면 그 `manager_id`는 이 escalation의 Handling
Assignment winner다. Case·Request 전이 전 실패를 보수하는 재시도는 기존 Item의 full
fingerprint를 읽어 같은 Item을 이어가며, 그 사이 조직 그래프가 바뀌었다고 새 ManagerItem을
만들거나 기존 Item의 Manager를 자동 rebind하지 않는다. 재지정·stale Item scan은 P17.9
운영 복구 범위다.

CandidateRegistryChanged는 기존 표를 consensus나 supporting 동의로 바꾸지 않는다. Manager Assign 대상도 원 Case 후보 중 현재 Registry에 존재하고 Owner User·under-claim·`cannot_answer` 검증을 통과한 카드뿐이다. 유효한 원 후보가 하나도 없으면 Assign을 거부하고 Dismiss만 허용한다. Owner transfer를 Case snapshot에 자동 rebind하지 않는다.

### 9. FromDeadlock Manager 처분은 P17.4 claim 패턴만 재사용한다

request-aware `FromDeadlock`에는 별도 애플리케이션을 둔다.

```python
class AssignDeadlockedOwner(FrozenDto):
    kind: Literal["assign_deadlocked_owner"] = "assign_deadlocked_owner"
    principal: ManagerPrincipal
    item_id: str
    agent_id: str
    rationale: str = ""

class DismissDeadlocked(FrozenDto):
    kind: Literal["dismiss_deadlocked"] = "dismiss_deadlocked"
    principal: ManagerPrincipal
    item_id: str
    rationale: str = ""

DeadlockManagerDispositionCommand = Annotated[
    AssignDeadlockedOwner | DismissDeadlocked,
    Field(discriminator="kind"),
]
```

P17.4에서 재사용하는 것은 다음과 같다.

- generation-bound reservation
- control token과 forward handle
- 같은 command 멱등, 다른 command conflict
- `ResumeEvidence`
- execution starter와 terminal publisher
- 같은 Manager Store의 한 Item당 한 claim

다음 의미는 재사용하지 않는다.

- `ReservedAssignOwnerClaim`의 FromUnowned 전용 payload
- Unowned처럼 원 후보 밖 카드까지 고를 수 있는 target 규칙
- `AuthorityAssignment(item_id, assigned_by)`를 direct consensus에 대입하는 방식
- `ManagerResolution.resolution is None`이라는 P17.4 FromUnowned 가정

Manager claim union에는 다음 변이를 더한다.

```python
class ReservedDeadlockAssignClaim(FrozenDto):
    kind: Literal["reserved_deadlock_assign"] = "reserved_deadlock_assign"
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    item_id: str
    org_id: str
    by_manager: str
    intent: str
    round: int
    cause: ConflictEscalationCause
    agent_id: str
    requires_approval: bool
    rationale: str

class SealedDeadlockAssignClaim(FrozenDto):
    kind: Literal["sealed_deadlock_assign"] = "sealed_deadlock_assign"
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    item_id: str
    org_id: str
    by_manager: str
    intent: str
    round: int
    cause: ConflictEscalationCause
    agent_id: str
    requires_approval: bool
    rationale: str

class ReservedDeadlockDismissClaim(FrozenDto):
    kind: Literal["reserved_deadlock_dismiss"] = "reserved_deadlock_dismiss"
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    item_id: str
    org_id: str
    by_manager: str
    intent: str
    round: int
    cause: ConflictEscalationCause
    rationale: str
    reason_code: Literal["manager_declined"] = "manager_declined"

class SealedDeadlockDismissClaim(FrozenDto):
    kind: Literal["sealed_deadlock_dismiss"] = "sealed_deadlock_dismiss"
    generation: str
    idempotency_key: str
    request_id: str
    case_id: str
    item_id: str
    org_id: str
    by_manager: str
    intent: str
    round: int
    cause: ConflictEscalationCause
    rationale: str
    reason_code: Literal["manager_declined"] = "manager_declined"

DeadlockManagerDispositionClaim = Annotated[
    ReservedDeadlockAssignClaim
    | SealedDeadlockAssignClaim
    | ReservedDeadlockDismissClaim
    | SealedDeadlockDismissClaim,
    Field(discriminator="kind"),
]
```

sealed 두 타입은 `kind` 외 payload가 각 reserved 타입과 exact equality다. 네 타입 모두 `idempotency_key == f"manager-disposition:{item_id}"`이고 Request·Case·Item·조직·intent·round·cause를 request-aware `FromDeadlock` source와 exact-link한다. Store backing과 claim engine은 공유하되, `P17ManagerDispositionApplication`을 거대한 match로 만들지 않는다. `P17DeadlockManagerDispositionApplication`이 출처별 규칙을 소유한다.

`FromDeadlock`에는 additive `cause: ConflictEscalationCause | None = None`을 둔다. `ConflictEscalationCause`는 `conflict.py`의 Conflict 도메인 타입으로 두어 Manager 모듈이 P17 application을 역참조하지 않게 한다. legacy 생성은 `cause=None`과 기존 free-form `reason`을 그대로 보존한다. request-aware 생성은 cause가 필수다. 이때 사람용 `reason`은 `DivergentVotes → "divergent_votes"`, `CandidateRegistryChanged → f"candidate_registry_changed:{reason_code}"`로만 투영하고 fingerprint나 복구 근거로 쓰지 않는다.

공유한다는 뜻은 `InMemoryManagerQueueStore`의 `_disposition_claims`, control token,
forward handle, `ResumeEvidence`, used generation, validation 재진입 guard와 단일 Manager
Item `RLock` backing을 P17.4·S3가 함께 쓴다는 뜻이다. Item마다 두 출처를 통틀어 claim은
하나만 존재한다. 다만 공개 메서드와 DTO union은 출처별로 나눠 P17.4의
`reserve_validated_action`·`claim_for_item` 계약을 바꾸지 않고, S3는
`reserve_validated_deadlock_action`·`deadlock_claim_for_item`을 쓴다. 한 출처의 메서드가
다른 출처 claim을 만나면 follower로 가장하지 않고 conflict 또는 integrity로 거부한다.

Manager claim 획득·seal 타입과 Store seam도 닫는다.

```python
class DeadlockManagerReservationControlToken(FrozenDto):
    generation: str
    token: str

class DeadlockManagerSealedClaimHandle(FrozenDto):
    generation: str
    forward_token: str

class DeadlockManagerClaimAcquired(FrozenDto):
    kind: Literal["acquired"] = "acquired"
    claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim
    control_token: DeadlockManagerReservationControlToken

class DeadlockManagerClaimInProgress(FrozenDto):
    kind: Literal["in_progress"] = "in_progress"
    retryable: Literal[True] = True

class DeadlockManagerSealedClaimAvailable(FrozenDto):
    kind: Literal["sealed"] = "sealed"
    claim: SealedDeadlockAssignClaim | SealedDeadlockDismissClaim
    handle: DeadlockManagerSealedClaimHandle

class DeadlockManagerClaimConflict(FrozenDto):
    kind: Literal["conflict"] = "conflict"

DeadlockManagerClaimAttempt = Annotated[
    DeadlockManagerClaimAcquired
    | DeadlockManagerClaimInProgress
    | DeadlockManagerSealedClaimAvailable
    | DeadlockManagerClaimConflict,
    Field(discriminator="kind"),
]

class DeadlockOwnerAssigned(FrozenDto):
    kind: Literal["deadlock_owner_assigned"] = "deadlock_owner_assigned"
    request_id: str
    case_id: str
    item_id: str
    route: RouteTarget
    wake: ExecutionWake

class DeadlockDismissed(FrozenDto):
    kind: Literal["deadlock_dismissed"] = "deadlock_dismissed"
    request_id: str
    case_id: str
    item_id: str
    reason_code: Literal["manager_declined"] = "manager_declined"
    delivery: TerminalDelivery

P17DeadlockManagerDispositionResult = Annotated[
    DeadlockOwnerAssigned | DeadlockDismissed,
    Field(discriminator="kind"),
]

class RequestAwareDeadlockManagerDispositionStore(Protocol):
    def reserve_validated_deadlock_action(
        self,
        item_id: str,
        command: DeadlockManagerDispositionCommand,
        *,
        validate: Callable[
            [ManagerItem],
            ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        ],
    ) -> DeadlockManagerClaimAttempt: ...

    def deadlock_claim_for_item(
        self,
        item_id: str,
    ) -> DeadlockManagerDispositionClaim | None: ...

    def seal_deadlock_claim(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
    ) -> DeadlockManagerSealedClaimAvailable: ...

    def abandon_unmutated_deadlock_assign(
        self,
        claim: ReservedDeadlockAssignClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> None: ...

    def deadlock_claim_for_handle(
        self,
        handle: DeadlockManagerSealedClaimHandle,
    ) -> SealedDeadlockAssignClaim | SealedDeadlockDismissClaim: ...

    def record_resume_evidence(
        self,
        handle: DeadlockManagerSealedClaimHandle,
        evidence: ResumeEvidence,
    ) -> None: ...

    def resume_evidence_for_claim(
        self,
        handle: DeadlockManagerSealedClaimHandle,
    ) -> ResumeEvidence | None: ...

    def resolve_for_claim(
        self,
        handle: DeadlockManagerSealedClaimHandle,
        resolved: ManagerItem,
    ) -> ManagerItem: ...

class P17DeadlockManagerDispositionApplication:
    def act(
        self,
        command: DeadlockManagerDispositionCommand,
    ) -> P17DeadlockManagerDispositionResult: ...
```

acquired와 sealed result는 claim generation이 token·handle generation과 같아야 한다. Store는 P17.4와 같은 Manager Item lock 안에서 exact callback을 한 번 실행하고 callback 재진입·반환 치환·generation 재사용을 fail-closed한다. same command follower는 reserved 동안 in-progress, sealed 뒤 같은 full handle을 받고, 다른 command는 conflict다. `seal_deadlock_claim`은 full control token을 exact 비교하며 `deadlock_claim_for_handle`과 `resolve_for_claim`은 full sealed handle을 exact 비교한다.

`ResumeEvidence` 값 타입은 P17.4 것을 그대로 재사용한다. 실제
`InMemoryManagerQueueStore.record_resume_evidence`·`resume_evidence_for_claim` 구현은
P17.4 `SealedClaimHandle`과 S3 `DeadlockManagerSealedClaimHandle`의 closed union을 받되
저장된 exact handle type·generation·secret 전체로 claim을 찾는다. Unowned Assign은 기존
`revision 1 → 2`, Deadlock Assign은 `revision 2 → 3`만 허용한다. Dismiss claim, 다른
revision, 다른 route/Authority version, 같은 generation의 다른 secret은 write 0 integrity
오류다. 같은 handle·같은 evidence 재기록은 no-op이고 다른 evidence는 거부한다.

Assign 순서는 reserve → Registry·Request·Case 재검증 → `FromDeadlockManagerGrant` Authority write다. receipt 성공이면 claim을 seal하고 request grant를 read-back한다. Authority conflict·예외·결과 불명은 reserved claim을 seal한 뒤 conflict/dependency로 끝내 같은 command만 forward retry하게 한다. claim key와 같은 `RequestRouteGrantRejected`가 write 0을 보증할 때만 아직 reserved인 Assign claim을 `abandon_unmutated_deadlock_assign`으로 지우고 Invalid를 반환한다. Dismiss는 외부 Authority write가 없으므로 reserve 직후 재검증하고 seal한다. sealed Assign이 후속 재시도에서 policy reject를 받으면 임의로 풀지 않고 integrity로 멈춘다. durable 운영자 복구는 P17.9 범위다.

Manager 처분이 Case terminal 전이의 권한으로 쓰이기 전에 두 Store가 각자 소유한 handle을 검증하고, Conflict Store가 로컬 mediation proof를 seal한다.

```python
class ValidatedMediationAssign(FrozenDto):
    kind: Literal["validated_mediation_assign"]
    conflict_claim: SealedDeadlockClaim
    conflict_handle: ConflictSealedClaimHandle
    manager_claim: SealedDeadlockAssignClaim
    manager_handle: DeadlockManagerSealedClaimHandle
    evidence: ConflictResolutionEvidence

class ValidatedMediationDismiss(FrozenDto):
    kind: Literal["validated_mediation_dismiss"]
    conflict_claim: SealedDeadlockClaim
    conflict_handle: ConflictSealedClaimHandle
    manager_claim: SealedDeadlockDismissClaim
    manager_handle: DeadlockManagerSealedClaimHandle
    reason_code: Literal["manager_declined"]

ValidatedManagerMediation = Annotated[
    ValidatedMediationAssign | ValidatedMediationDismiss,
    Field(discriminator="kind"),
]

class ConflictMediationHandle(FrozenDto):
    conflict_generation: str
    manager_generation: str
    forward_token: str

class SealedConflictMediationAvailable(FrozenDto):
    proof: ValidatedManagerMediation
    handle: ConflictMediationHandle

class ConflictMediationSealed(FrozenDto):
    kind: Literal["mediation_sealed"]
    position: int
    case_id: str
    request_id: str
    round: int
    item_id: str
    disposition: Literal["assign", "dismiss"]
    conflict_generation: str
    manager_generation: str

class RequestAwareConflictMediationStore(Protocol):
    def record_validated_mediation(
        self,
        conflict_handle: ConflictSealedClaimHandle,
        manager_handle: DeadlockManagerSealedClaimHandle,
        *,
        validate: Callable[
            [ConflictCase, SealedDeadlockClaim, DeadlockManagerSealedClaimHandle],
            ValidatedManagerMediation,
        ],
    ) -> SealedConflictMediationAvailable: ...

    def transition_for_mediation(
        self,
        handle: ConflictMediationHandle,
        *,
        target: ConflictCase,
    ) -> ConflictCase: ...
```

위 DTO에는 생성자 단계의 닫힌 조합 validator를 둔다.

- 네 Deadlock Manager claim은 idempotency key, `cause.round == round`, nonblank 공통 필드,
  Assign/Dismiss별 고정 `reason_code`를 검사한다. Item source·Case 후보·현재 Registry와의
  비교는 Store validation callback이 맡는다.
- acquired/sealed attempt는 claim과 control token/handle generation의 exact equality를
  검사한다. sealed 변이는 `kind` 외 reserved payload 전체가 같아야 한다.
- `ValidatedMediationAssign | ValidatedMediationDismiss`는 두 claim의
  Request·Case·Item·조직·intent·round·cause와 각 full handle generation을 exact-link한다.
  Assign은 `FromManagerMediation(item_id, by_manager)`, Manager claim과 같은 route
  primary·Approval, nonblank Authority version, `supporting=()`를 요구한다. Dismiss에는
  evidence 필드 자체가 없고 reason은 `manager_declined`만 허용한다.
- `SealedConflictMediationAvailable`은 proof의 두 generation과
  `ConflictMediationHandle.conflict_generation/manager_generation`을 exact 비교한다.
  progress projection은 두 generation과 disposition만 남기고 secret token을 싣지 않는다.

잠금 순서는 `Conflict Store → Manager Store → Question Request Store → Registry`로
고정하며 단계는 건너뛸 수 있다. 현재 direct callback은 `Conflict → Request → Registry`,
P17.4 Manager callback은 `Manager → Request → Registry`이며 S3도 같은 순서를 따른다.
외부 Authority·deadline policy·starter·publisher 호출
중에는 Conflict/Manager Store lock을 잡지 않는다. 특히 Manager reservation callback은
Conflict Store를 다시 읽지 않는다. S3 application이 `sealed_claim_for_case`로 원 claim과
full handle을 먼저 읽어 immutable snapshot으로 잡고, Manager lock 안의 callback은 그
snapshot과 현재 ManagerItem·Request를 exact 비교한 뒤 Registry를 읽는다. 반대 방향의 중첩은
`record_validated_mediation`이 Conflict lock 안에서 Manager Store의
`deadlock_claim_for_handle`을 읽는 경우 하나뿐이다. 이 Manager read는 Registry·Request나
Conflict Store를 호출하지 않는다. Registry 또는 Request lock을 잡은 상태에서
Conflict/Manager Store로 역진입하지 않으며, 따라서 `Manager → Conflict` 순환 잠금을
만들지 않는다.

S3 착수 전 P17.4의 예외도 같은 순서로 교정한다. 현재
`P17ManagerDispositionApplication._advance_assign`가 `Registry.consistency_guard()`를
잡은 채 deadline policy와 Request CAS를 호출하는 `Registry → Request/UoW` 순서는
Finalization의 `Request/UoW → Registry`와 ABBA가 될 수 있다. 장시간 outer guard를
제거하고, Registry 검증은 CAS 직전과 CAS winner read-back 뒤의 짧은 선형화 지점으로만
둔다. deadline policy와 `QuestionRequestStore.compare_and_set`을 호출할 때 Registry lock을
보유하지 않는다. CAS 사이 Registry mutation이 감지되면 ResumeEvidence·Item resolve·wake를
쓰지 않고 fail-closed하며, 같은 action은 Registry가 다시 exact해졌을 때만 forward
repair한다. blocking Request Store의 CAS 중 다른 스레드 Registry mutation이 막히지 않고,
post-CAS revalidation이 drift를 잡는 회귀 테스트를 P17.4에 추가한다.

`DeadlockManagerSealedClaimHandle`은 P17.4의 공통 Manager handle 구현을 재사용해도 되지만, Manager Store가 저장한 `generation + forward_token` 전체로만 읽고 resolve한다. 같은 generation의 다른 token도 integrity 오류다. S3 application은 `sealed_claim_for_case(case_id)`로 원 Conflict claim과 full handle을 함께 복구하고, 반드시 `SealedDeadlockClaim`인지와 `FromDeadlock`의 Request·Case·round·cause가 exact한지 확인한다. validation callback은 composition이 소유한 같은 Manager Store의 `deadlock_claim_for_handle`을 호출해야 하며, Conflict Store는 저장된 full conflict handle과 반환된 `conflict_claim`, 입력 full manager handle과 반환된 `manager_claim`을 각각 exact 비교한다. callback 재진입·예외·반환 치환은 mediation proof와 resolution evidence를 쓰지 않는다.

두 validated 변이는 Request·Case·ManagerItem·조직·intent·cause·round·두 generation을 exact-link한다. Assign evidence는 source가 exact `FromManagerMediation(item_id, by_manager)`, route primary가 Manager claim의 `agent_id`, approval flag가 claim과 같고 `supporting=()`여야 한다. Assign은 이 evidence를 mediation proof와 같은 임계구역에서 먼저 기록한다. Dismiss 타입에는 evidence 필드가 없으며 Conflict Store에 해당 Request의 resolution evidence가 있으면 거부한다. 같은 두 full handle·같은 proof의 재호출은 같은 `ConflictMediationHandle`을 돌려주고 progress를 다시 append하지 않는다. 다른 handle·처분·evidence는 conflict 또는 integrity 오류다. S3부터 `ConflictMediationSealed`를 Conflict progress entry union에 추가하되 secret forward token은 history projection에 싣지 않는다.

Assign은 원 Case 후보 중 하나만 받는다. 현재 Registry의 카드·Owner·under-claim·Approval을 확인하고 `FromDeadlockManagerGrant`를 write/read 검증한다. 그 다음 manager-sourced `ConflictResolutionEvidence`를 저장하고, 같은 Request를 Ready로 옮기고, Case와 ManagerItem을 같은 처분으로 닫은 뒤 execution starter를 부른다.

Dismiss는 Authority와 Runtime을 호출하지 않는다. Request를 `DeclinedRequest(reason_code="manager_declined")`, Case를 `declined`, ManagerItem을 resolved로 만든 뒤 terminal publisher를 부른다.

순서는 Assign에서 Manager claim reserve → Authority write → claim seal → request grant read-back → `record_validated_mediation`(resolution evidence 포함) → Request Ready CAS → Manager `record_resume_evidence` → `transition_for_mediation(resolved)` → Manager `resolve_for_claim` → wake다. CAS 응답 유실 뒤 Request가 exact Ready/revision 3인데 evidence만 없으면 같은 sealed handle로 `revision 2 → 3` evidence부터 보수한다. Dismiss는 Manager claim reserve → claim seal → `record_validated_mediation`(evidence 없음) → Request Declined CAS → `transition_for_mediation(declined)` → Manager `resolve_for_claim` → terminal publish다. `transition_for_mediation`은 저장된 local handle 전체를 검증하고 proof 변이에 맞는 target만 받는다. Assign은 같은 item을 가진 resolved Case와 evidence의 Resolution, Dismiss는 같은 item의 declined Case와 `manager_declined`를 요구한다.

Request CAS 뒤 Case나 Item 종결이 실패해도 proof와 두 원본 claim을 삭제하지 않는다. 같은 Manager action 재시도는 저장된 Request를 exact-read한 뒤 Case 전이와 Item resolve, wake/publish만 앞쪽부터 보수한다. `transition_for_mediation`과 Manager `resolve_for_claim`은 이미 저장된 terminal이 exact target이면 성공 no-op이고, 다른 terminal이면 integrity 오류다. Request가 다른 winner로 전이됐으면 Case·Item을 이 처분으로 닫지 않는다.

### 10. primary와 supporting 근거를 같은 resolution evidence에 묶는다

```python
class SupportingKnowledgeEvidence(FrozenDto):
    agent_id: str
    affirmed_by_owner: str
    authority: Literal[0] = 0

class FromDirectConsensus(FrozenDto):
    kind: Literal["direct_consensus"]
    round: int
    votes: tuple[OwnerConcurrenceEvidence, ...]

class FromManagerMediation(FrozenDto):
    kind: Literal["manager_mediation"]
    item_id: str
    by_manager: str

ConflictResolutionSource = Annotated[
    FromDirectConsensus | FromManagerMediation,
    Field(discriminator="kind"),
]

class ConflictResolutionEvidence(FrozenDto):
    request_id: str
    case_id: str
    org_id: str
    intent: str
    route: RouteTarget
    source: ConflictResolutionSource
    supporting: tuple[SupportingKnowledgeEvidence, ...] = ()
```

direct consensus에서 supporting은 primary가 아닌 원 후보 중, 그 후보의 저장된 Owner가 `keep_as_complement`를 명시한 카드만 포함한다. 기본 `withdraw`는 supporting을 만들지 않는다. 한 Owner가 여러 losing 카드를 소유하면 기존 ADR 0038 의미대로 그 Owner의 stance가 해당 카드들에 적용된다.

Manager mediation은 별도 supporting 선택 명령이 없으므로 `supporting=()`가 기본이다. Deadlock 표를 Manager 결론의 동의로 재해석하지 않는다.

supporting은 request-scoped Authority grant를 받지 않는다. `answered_by`, Approval 주체, 책임 snapshot에도 들어가지 않는다. 전역 `ComplementEdge`도 기록하지 않는다. supporting은 이 Request의 접지 입력과 sources 레이블을 넓히는 근거다.

### 11. Answer Source는 contested resolution evidence를 검증한다

P17 completed-inline Answer Source의 Authority reader 선택은 다음과 같다.

```text
initial_disposition == "routed"
  → authorize(org_id, intent, agent_id)

initial_disposition in {"unowned", "contested"}
  → authorize_for_request(org_id, request_id, intent, agent_id)
```

bare string resolver는 쓰지 않는다. 중앙 Knowledge Store 앞에 typed reader를 둔다.

```python
class GroundingKnowledgeFound(FrozenDto):
    kind: Literal["found"] = "found"
    agent_id: str
    content: KnowledgeBundleContent

class GroundingKnowledgeMissing(FrozenDto):
    kind: Literal["missing"] = "missing"
    agent_id: str

class GroundingKnowledgeInvalid(FrozenDto):
    kind: Literal["invalid"] = "invalid"
    agent_id: str
    reason_code: Literal[
        "type_mismatch",
        "agent_id_mismatch",
        "empty_documents",
        "invalid_document",
    ]

GroundingKnowledgeResult = Annotated[
    GroundingKnowledgeFound | GroundingKnowledgeMissing | GroundingKnowledgeInvalid,
    Field(discriminator="kind"),
]

class GroundingKnowledgeReader(Protocol):
    def read(self, agent_id: str) -> GroundingKnowledgeResult: ...

class GroundingTerminalFailureRecorder(Protocol):
    def fail_if_ready(
        self,
        request_id: str,
        expected_revision: int,
        error_code: Literal[
            "required_grounding_missing",
            "required_grounding_invalid",
        ],
    ) -> QuestionRequest: ...
```

reader는 requested agent ID와 결과 `agent_id`, `KnowledgeBundleContent.agent_id`를 exact 비교한다. `Found`는 exact `KnowledgeBundleContent`와 `KnowledgeDoc` 타입만 받는다. documents는 하나 이상이어야 하고, 각 문서 path와 body는 nonblank이며 path는 중복될 수 없다. Store가 돌려준 객체를 그대로 노출하지 않고 strict canonical deep copy를 반환한다. `Missing | Invalid`도 requested agent ID와 같아야 한다. 결과 타입이나 ID가 다르면 `Invalid`로 낮춰 기록하거나 integrity 오류로 멈추며 `Found`로 통과시키지 않는다.

Contested 재개 Request는 다음 검증을 더 거친다.

1. 같은 Conflict Store에서 `resolution_evidence_for_request(request_id)`를 읽는다.
2. evidence의 request·case·intent·route와 저장된 Request·Case를 exact 비교한다.
3. Case가 `resolved`이고 같은 Resolution을 가리키는지 확인한다.
4. primary와 supporting 카드를 현재 Registry에서 다시 읽고 Owner User와 under-claim을 검증한다.
5. `route.requires_approval`이 현재 primary 카드의 `approval_when`과 같은지 확인한다.
6. `GroundingKnowledgeReader`에서 primary의 필수 본문을 읽는다.
7. positive `SupportingKnowledgeEvidence`가 있는 카드만 추가로 읽는다. evidence가 없는 후보는 조회하지 않는다.
8. 모두 `Found`면 `(primary, *supporting)` 순서의 canonical tuple을 `assemble_grounding_knowledge_text`에 넘겨 문서 path·body 본문을 조립한다.
9. Runtime은 primary 카드 하나로 호출한다.
10. sources는 primary와 supporting 카드의 현재 `knowledge_sources`를 순서 보존 dedup해 만든다.

reader가 예외를 던지면 transient dependency failure다. Answer Source는 `grounding_read_interrupted` 오류를 retryable로 반환하고 Runtime을 호출하지 않으며, Request는 같은 `ReadyToDispatch` revision에 남는다. blocking 표면은 `QuestionSurfaceInterruptedError`, stream 표면은 `InterruptedEvent`로 투영한다. broker는 저장되지 않은 terminal을 만들지 않는다.

primary 또는 positive supporting에서 `Missing`이 나오면 Runtime 호출 0을 확인한 뒤 `GroundingTerminalFailureRecorder`가 같은 Request를 `FailedRequest(error_code="required_grounding_missing")`로 CAS한다. `Invalid`도 같은 순서로 `required_grounding_invalid`를 기록한다. broker는 recorder가 commit한 저장본을 다시 읽어 Failed를 내보낸다. CAS 경쟁에서 이미 terminal이 있으면 그 저장 결과를 투영하고, 다른 nonterminal winner면 conflict로 멈춘다. 빈 문자열을 정상 grounding으로 넘기거나 Reader 예외를 terminal Missing으로 바꾸지 않는다.

`_require_request_approval_snapshot`은 Unowned뿐 아니라 Contested에도 적용한다. Runtime 호출, AnswerRecord, Approval, 책임 snapshot은 계속 primary 하나만 본다. supporting의 Authority 값 0은 지식 실패 처리에서도 바뀌지 않는다.

### 12. composition과 웹은 객체 identity로 경로를 묶는다

`QuestionSurfaceComposition`은 다음 구성요소를 추가로 소유한다.

```text
Conflict Store / Conflict Disposition
Deadlock Manager Disposition
Conflict Resolution Evidence Reader
GroundingKnowledgeReader
GroundingTerminalFailureRecorder
```

조립 시 다음 identity를 `is`로 확인한다.

```text
intake가 쓰는 Conflict Store
  is concurrence claim·evidence·Case 전이가 쓰는 Store
  is Answer Source evidence reader
  is 웹 Owner 처리함과 concur가 쓰는 Store

initial/deadlock enqueue Manager Store
  is Manager claim·resolve Store
  is 웹 Manager 큐 Store

초기 라우팅 Route Authority
  is request grant writer
  is authorize_for_request reader
  is Answer Source Authority

concurrence·mediation Registry
  is Answer Source Registry
  is responsibility resolver가 보는 Registry

Answer Source GroundingKnowledgeReader
  is composition이 소유한 typed Knowledge Store reader

GroundingTerminalFailureRecorder Request Store
  is execution·broker·Completion이 보는 Request Store

Request Store·Completion Reader·execution starter·terminal publisher
  are 기존 Question Surface가 소유한 같은 객체
```

경로 문자열, equality, 같은 파일을 연 별도 proxy는 identity 증거가 아니다. 조립 실패 시 scheduler와 storage를 기존 순서대로 정리한다.

웹은 저장된 객체를 먼저 읽고 분기한다.

```text
POST /cases/{case_id}/concur
  request_id is None     → legacy ConsensusService
  request_id is nonblank → P17ConflictDispositionApplication

POST /manager/items/{item_id}/act
  legacy request_id None       → ManagerQueueService
  request-aware FromUnowned    → P17.4 application
  request-aware FromDeadlock   → P17DeadlockManagerDispositionApplication
  request-aware FromDispatch   → fail-closed
```

`ConcurRequest`는 `expected_round: int | None = None`과 `stance`를 additive로 받는다. request-aware Case는 expected round가 없으면 400 Invalid, legacy Case는 두 값을 무시해 기존 동작을 보존한다. 인증이 켜져 있으면 `by_owner`는 계속 세션 신원만 사용한다. 오류 응답은 고정 code와 retryable만 내보내고 내부 ID·intent·rationale를 반사하지 않는다.

Owner 처리함 projection은 request-aware Case의 canonical `current_round`를 포함한다. 정적 UI와 Next.js UI는 이 값을 화면 로컬에서 증가시키거나 캐시로 추정하지 않고, 받은 값을 그대로 `expected_round`에 넣어 POST한다.

웹과 애플리케이션의 같은-action 재시도는 `get_request_case(case_id)`로 terminal Case를 읽는다. Case resolve 뒤 wake 또는 첫 HTTP 응답을 강제로 실패시킨 다음 같은 POST를 보내도 새 vote·grant·resolution을 만들지 않고, 저장된 claim·evidence·Request/Completion을 확인해 같은 응답으로 수렴해야 한다. Answered terminal이면 composition의 같은 Completion Reader exact evidence가 필수다.

### 13. 부분 실패는 같은 claim으로 앞으로 수렴한다

| 마지막 성공 단계 | 관측 상태 | 같은 action 재시도 | 다른 action |
|---|---|---|---|
| 일부 Owner 표 저장 | Case open, round 진행 중 | 같은 표 멱등, 남은 Owner 대기 | 같은 Owner의 다른 표는 conflict |
| Registry drift 검출 | 현재 invocation의 새 표 미수용, escalation cause claim | S3가 같은 cause로 Item·Case·Request 전이 | normal vote 금지 |
| consensus claim reserved | Case open, Request AwaitingConflict | 첫 caller만 control token으로 Authority 진행 | conflict |
| Authority typed reject(write 0) | 새 round, active votes 없음 | 새 round에서 다시 합의 | 허용 |
| Authority first-winner conflict | claim sealed, 기존 grant 보존 | 같은 assignment만 조회·검증 | abandon·round reset 금지 |
| claim sealed 또는 Authority 결과 불명 | Case open, Request AwaitingConflict | 같은 round·표만 forward retry | conflict |
| request grant write/read | grant 존재, Request AwaitingConflict | 같은 key·receipt로 evidence부터 이어감 | conflict |
| resolution evidence 저장 | Request AwaitingConflict | 같은 evidence 확인 뒤 CAS | conflict |
| direct Request CAS | Request Ready 또는 증명된 후손, Case open일 수 있음 | Case resolve와 wake 보수 | conflict |
| Deadlock ManagerItem 생성 | Item open, Case open일 수 있음 | Case escalation과 Request CAS 보수 | Owner 표·다른 Item 금지 |
| Case escalated | Request AwaitingConflict일 수 있음 | 같은 Item으로 AwaitingManager CAS | Owner 표 금지 |
| Manager Assign Request CAS | Request Ready 또는 후손, Case/Item 미종결 가능 | Case·Item resolve와 wake 보수 | conflict |
| Manager Dismiss Request CAS | Request Declined, Case/Item 미종결 가능 | Case declined·Item resolve·publish 보수 | conflict |
| Grounding reader 예외 | Request Ready, Runtime 호출 0 | retryable Interrupted 뒤 재실행 | terminal 위장 금지 |
| Grounding Missing/Invalid | Request Failed, Runtime 호출 0 | 저장된 Failed 재투영 | Runtime·AnswerRecord 금지 |
| wake 또는 publish 실패 | 저장 상태는 이미 전이됨 | `AlreadyRunning \| NotNeeded \| AlreadyPublished`로 수렴 | conflict |

부분 성공을 보상 삭제하지 않는다. receipt, sealed claim, evidence를 남겨 같은 action으로 이어간다. 외부 Runtime 계산의 물리적 exactly-once는 주장하지 않는다.

### 14. 닫힌 오류

외부 어댑터가 문자열을 파싱하지 않도록 오류를 닫힌 종류로 둔다.

```text
ConflictDispositionNotFound
ConflictDispositionForbidden
ConflictDispositionInvalid
ConflictDispositionInProgress
ConflictDispositionConflict
ConflictDispositionDependency
ConflictDispositionIntegrity
```

HTTP는 각각 404, 403, 400, 409, 409, 503, 500으로 매핑한다. retryable in-progress와 dependency에는 제한된 `Retry-After`를 줄 수 있다. 저장소 손상이나 receipt mismatch를 사용자 입력 오류로 낮춰 숨기지 않는다.

## 기각한 대안

- **legacy `ConsensusService`에 Request Store를 주입** — process-local vote, 전역 학습, Case-only 종결이 한 서비스에 남아 주입 누락 시 다시 fail-open한다.
- **Case 상태를 `open | resolved`로 유지** — Manager가 맡은 Case가 Owner 처리함과 concur API에 계속 보인다.
- **사전순 카드 ID로 primary 선택** — 책임 결정 전 단수 책임자를 만들고 Approval과 Authority를 우회한다.
- **합의 직후 전역 intent Precedent 기록** — 같은 intent의 Request들이 서로 다른 결론을 낼 수 있어 적용 범위가 과도하다.
- **합의 직후 전역 ComplementEdge 기록** — 한 Request의 supporting 선언을 모든 미래 질문에 확대한다. P17.10이 조건과 범위를 정하기 전에는 저장하지 않는다.
- **supporting을 공동 책임자나 승인자로 기록** — 지식 인용을 Authority로 바꾼다.
- **direct consensus에 `item_id=case_id`를 넣기** — 서로 다른 도메인 ID를 가짜로 같게 만든다.
- **direct consensus에 `assigned_by="consensus"`를 넣기** — 여러 Owner의 결론을 존재하지 않는 단수 주체로 위장한다.
- **Manager mediation에서 Deadlock 표를 supporting 동의로 재해석** — 서로 다른 primary를 지목한 표에 없던 합의를 만든다.
- **해소 뒤 Router 재호출** — 사람이 정한 primary를 잃고 다시 Contested나 Unowned가 될 수 있다.
- **Authority 오류마다 claim 삭제** — write 성공 여부가 불명확한 경우 다른 target grant가 같은 Request에 생길 수 있다.

## 정직한 한계

- 첫 구현은 단일 프로세스 InMemory claim·evidence·request grant를 사용한다.
- process restart, 다중 인스턴스, durable transaction, lease, outbox는 P17.9가 맡는다.
- production Authority, OIDC/RBAC, org 격리, 정책 관리자 권한은 P17.8 범위다.
- 첫 후보 Owner의 Manager를 택하는 현재 규칙을 유지한다. LCA와 멀티홉은 후속이다.
- Case 진행 중 카드·Owner·under-claim이 바뀌면 관련 Owner의 다음 authenticated action이 `CandidateRegistryChanged` escalation으로 수렴한다. 자동 rebind는 하지 않는다. 사람이 action을 보내지 않은 stale Case를 찾아 깨우는 system recovery scan은 P17.9 범위다.
- typed grounding reader의 transient 장애는 Request를 Ready에 남긴다. 필수 grounding Missing/Invalid만 고정 error code의 Failed terminal로 닫는다.
- Manager가 supporting을 따로 선택하는 기능은 없다. mediation 결과의 supporting은 비어 있다.
- Owner·Manager 조작 UI, 만료, 재지정, 감사·알림 운영은 P17.6b·P17.13에서 보강한다.
- contextual Precedent와 장기 상보 관계 학습은 P17.10 전에는 없다.

## S0~S6 TDD handoff

### S0 — ADR·유비쿼터스 언어

- ADR 0046과 CONTEXT의 request-aware 용어를 맞춘다.
- ADR 0008의 intent dedup, ADR 0037의 즉시 답, ADR 0038의 전역 Edge 방출을 legacy 범위로 표시한다.
- 새 코드가 Router·Precedent·Edge를 import하지 않는 구조 가드를 먼저 red로 둔다.

### S1 — Case·Store·concurrence claim

- Case 네 상태와 허용 전이를 red→green으로 구현한다.
- request-aware Case의 `concurrence_round=1`, claim-bound open→open round 증가, active vote 폐기와 history 보존을 검증한다.
- request별 Case 유일성, legacy mutator 차단, deep copy와 history 보호, legacy open-only `get`과 terminal `get_request_case` 분리를 검증한다.
- `expected_round`, 같은 표 멱등, 다른 표 conflict, stale round ABA 차단을 구현한다.
- exact validation callback과 `Pending | Acquired | InProgress | Sealed | Conflict` 원자 결과, callback 재진입·반환 치환·generation 재사용 fail-closed를 구현한다.
- claim member·validator와 canonical vote/drift follower 규칙, 같은 generation의 다른 secret token/handle 및 stale handle 거부를 구현한다.
- Case history와 분리된 frozen progress history가 vote·reserve·seal·round abandon·resolution evidence를 순서 보존 deep snapshot으로 내는지, 동일 evidence 재시도가 no-op이고 history를 중복 append하지 않는지 검증한다.
- `DivergentVotes | CandidateRegistryChanged` cause claim을 구현하고, 현재 drift invocation이 새 표로 저장되지 않되 같은 fingerprint의 과거 accepted vote는 보존됨을 확인한다.
- S1의 escalation claim은 내부 결과일 뿐이며 S3 전까지 외부 `ConflictEscalated`로 투영하지 않는다.
- `ConsensusService.concur` request-aware 조기 guard와 vote dict·Precedent·Edge·mark_resolved spy 0을 검증한다.

### S2 — direct consensus

- 현재 Registry와 후보 Owner·under-claim·Approval 검증을 구현한다.
- direct consensus provenance의 request grant write/read exact 검증을 구현한다.
- `RequestRouteGrantRejected`와 `RequestRouteGrantConflict`를 분리하고, conflict에서 claim abandon·round reset이 0인지 확인한다.
- `(org_id, request_id)` first-winner slot과 P17.4 facade receipt/rejected/conflict/exception 투영을 회귀 검증한다.
- evidence-before-CAS, 같은 Request attempt 1, Case resolve, wake 순서를 고정한다.
- Authority typed reject, 결과 불명, evidence/CAS/Case/wake fault point를 각각 재시도한다.
- Resolution rationale가 candidate Owner order로 canonical한지 검증한다.
- Case resolve 뒤 wake/응답 실패 재시도는 `get_request_case`와 Completion Reader exact evidence로 수렴시킨다.
- 동일·상충 표 32-way 경쟁을 반복 검증한다.

### S3 — Deadlock과 Manager mediation

- 선행 교정으로 P17.4 Assign의 Registry outer guard를 제거하고 Request CAS 전후 짧은
  재검증과 blocking Request Store 동시성 회귀를 추가해 `Request/UoW → Registry` 순서를
  거스르는 ABBA 가능성을 없앤다.
- cause와 현재 Registry를 함께 읽어 manager_id/root를 확정하고, request-aware `FromDeadlock` ManagerItem create-or-get과 Case escalation을 구현한다.
- Request를 `AwaitingManager(public_kind="contested")`로 옮긴다.
- Deadlock command·claim attempt·result union과 reserve/seal/abandon/read-handle Store API를 P17.4 공통 engine에 추가하되 출처별 application은 분리한다. Assign은 reserve→Authority→seal, Dismiss는 reserve→seal 순서를 지킨다.
- Manager Store가 full sealed handle로 claim을 읽고, Conflict Store가 두 full handle과 변이별 proof를 seal한 뒤에만 `escalated → resolved | declined`를 허용한다. Assign evidence 필수·Dismiss evidence 금지, terminal same-action no-op과 부분 실패 보수를 검증한다.
- 별도 Manager action 요청에서도 `sealed_claim_for_case`가 원 deadlock claim과 full Conflict handle을 복구하고, 누락·generation 불일치를 fail-closed하는지 검증한다.
- Assign target을 현재 유효한 원 후보로 제한한다. 유효 후보가 없을 때 Assign은 거부하고 Dismiss는 Request·Case·Item을 정확히 닫아야 한다.

### S4 — primary 확정 후 다중 접지

- `ConflictResolutionEvidenceReader`와 Answer Source를 연결한다.
- Contested가 반드시 `authorize_for_request`를 쓰도록 한다.
- positive stance만 supporting evidence가 되고 `authority=0`인지 검증한다.
- bare string resolver를 제거하고 typed `GroundingKnowledgeReader`의 `Found | Missing | Invalid` canonical 결과를 구현한다.
- exact agent ID, `KnowledgeBundleContent`·`KnowledgeDoc`, nonblank·중복 없는 path/body, deep copy를 검증한다.
- reader 예외는 Request Ready와 retryable Interrupted를 유지하고 Runtime·terminal publish가 0인지 확인한다.
- Missing/Invalid은 `GroundingTerminalFailureRecorder`가 `required_grounding_missing | required_grounding_invalid` Failed를 CAS하고, broker가 저장된 Failed를 내는지 검증한다.
- primary는 항상 읽고, supporting은 positive evidence가 있을 때만 읽는다. Runtime·Approval·책임은 primary 하나다.
- **구현 완료(2026-07-13):** typed Knowledge Store reader와 canonical assembler, contested evidence/Case/grant strict 검증, request-scoped Authority, execution-owned terminal command/recorder를 구현했다. Missing/Invalid는 저장된 Failed, reader 예외는 Ready+retryable Interrupted로 수렴한다. 독립 재리뷰와 전체 4,045 테스트를 통과했으며, S5에서 composition·웹·채널 identity 연결까지 마쳤다.

### S5 — composition·웹·채널

- Request·Conflict·Manager·Registry·Authority·Completion·typed Knowledge reader·terminal failure recorder identity gate를 구현한다.
- request-aware와 legacy `/cases`·`/manager/items` 분기를 검증한다.
- request-aware Case projection의 `current_round`를 정적 UI와 Next.js UI가 그대로 `expected_round`로 POST하는지 검증한다. legacy는 optional round·stance를 무시한다.
- Case resolve 뒤 wake/첫 HTTP 응답 실패를 주입하고 같은 POST가 새 vote·grant 없이 terminal Case와 Completion evidence로 같은 응답을 repair하는지 확인한다.
- blocking·canonical GET·native SSE watch·MCP가 같은 Request의 Pending·Answered·Declined·Failed를 보는지 확인한다.
- P17.4 FromUnowned와 legacy Consensus/Manager 회귀를 보존한다.
- **구현 완료(2026-07-13):** all-or-none contested surface config와 exact identity gate, shared legacy/request-aware Conflict Store, request-aware concurrence 및 FromDeadlock Manager 분기, 안정된 wire 결과, 서버 round·stance UI 전송을 구현했다. legacy Case는 additive round·stance를 무시한다. direct·Deadlock Assign/Dismiss·grounding Failed가 blocking·GET·SSE·MCP에서 같은 Request를 보며, raw 오류 문자열은 노출하지 않고 공개 가능한 grounding error code만 고정 allowlist로 낸다. Case resolve 뒤 같은 concurrence POST는 새 vote·grant 없이 저장된 claim·evidence·Request/Completion으로 수렴한다. grounding Failed 복구는 resolved Case·revision 3·`required_grounding_missing | required_grounding_invalid`로 제한한다. 최초 독립 리뷰 P1 4건 보수 뒤 S5 집중 125 passed, 전체 4,068 passed, 정적·프런트 게이트를 통과했다.

### S6 — 경쟁·장애·리뷰와 SSOT

- final vote, Registry drift, direct와 Deadlock, Manager Assign과 Dismiss, Authority first-winner, resolution evidence, Request CAS, Case·Item terminal, grounding terminal recorder, wake를 32-way로 반복한다.
- 각 저장 단계 사이 fault injection과 반환값 변조, 같은 generation의 다른 secret 및 stale token/handle, 동일 evidence 재생, transient grounding read, Missing/Invalid terminal 기록을 검증한다.
- Ruff, Pyright, 전체 Pytest, diff-check를 통과한다.
- code-reviewer가 Authority 우회, 조기 Runtime, 전역 학습, Store capability 우회를 독립 확인한다.
- 승인 뒤 PRD·TRD·TASK·CONTEXT와 ADR 상태를 구현 결과에 맞춰 갱신한다.
- **구현 완료(2026-07-14):** final vote·Deadlock/Registry drift·Manager Assign/Dismiss·Authority·mediation evidence·grounding terminal·wake의 32-way 경쟁을 full application 수준에서 반복했다. reserve·sealed proof를 별 non-mutating preflight로 만들고 composition 필수 capability로 고정했다. claim/token/handle·Authority read·abandon·wake·seal 응답·HTTP 응답·grounding read의 변조와 유실을 주입해 Authority·Runtime·terminal write 순서를 검증했다. 독립 fault review의 P1 5건과 P2 1건을 red→green으로 닫은 뒤 재리뷰 P0/P1/P2 0, 집중 752 passed, 32-way 150/150, 전체 4,088 passed와 정적·프런트 게이트 green을 확인했다.

## 불변식 자체점검

- **사용자 결과 기준 미아 없음 — 강화.** direct consensus와 Manager mediation 모두 같은 Request를 재개하거나 Declined로 닫는다.
- **등록 무결성 — 보존.** 원 후보라도 현재 Registry 카드와 Owner User가 유효하지 않으면 실행하지 않는다.
- **Authority 중앙 — 강화.** Owner 합의와 Manager 중재를 구분한 request-scoped grant receipt와 read-back이 있어야 실행한다.
- **전이 ≠ 기록 — 보존.** Case·Request·Item 전이, claim·Authority·resolution evidence, AnswerRecord·Audit를 구분한다.
- **책임 확정 전 답 금지 — 강화.** resolution evidence와 request grant가 없으면 Answer Source가 Runtime을 호출하지 않는다.
- **노출 불변식 — 보존.** 사용자는 Pending·Answered·Declined·Failed만 보고, 후보 표·Manager·grant provenance·supporting 내부 ID는 보지 않는다.
