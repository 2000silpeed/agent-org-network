# ADR 0045 — Request-aware Unowned Manager 처분과 같은 질문 재개

- 상태: 채택(Accepted)
- 날짜: 2026-07-13
- 계보: ADR 0004(Authority 중앙)·0014(Manager 큐)·0042(Question Request 종결성)·0043(Request-first Application)를 정밀화한다. ADR 0014의 `ManagerQueueService`와 전역 Precedent 기록은 legacy 경로로 한정한다.
- 구현 상태: P17.4 S0~S5 구현과 독립 리뷰 완료.
- 구현 범위: request-aware `FromUnowned`의 Assign/Dismiss, generation-bound claim, request-scoped Authority grant, 같은 Request 재개·종결, Question Surface 조립과 웹·GET·SSE·MCP 결과 동등성을 단일 프로세스 InMemory 경계에서 검증했다.
- 검증·한계: P17.4 집중 회귀 49 passed, 전체 회귀 3,812 passed, pyright 0, Ruff·변경 파일 format·diff-check를 통과했다. durable claim/grant와 다중 인스턴스 lease·outbox는 P17.8~P17.9 전까지 보장하지 않는다.

## 맥락

P17.2c-1은 Unowned 질문을 먼저 Question Request로 저장하고, 같은 `request_id`를 가진 `ManagerItem`을 만든 뒤 Request를 `AwaitingManager(public_kind="unowned")`로 옮긴다. P17.4 착수 전 Manager 처분 엔드포인트는 모든 항목을 legacy `ManagerQueueService`로 보냈다. 이 서비스는 큐 항목만 `resolved`로 만들며 원 Question Request를 재개하거나 `DeclinedRequest`로 닫지 않았다.

legacy 서비스의 다른 책임도 P17.4와 맞지 않는다.

- `AssignOwner`가 intent를 찾으면 전역 `Precedent`를 만들 수 있다. P17.4의 목표는 질문 한 건의 책임 공백을 닫는 것이지, 그 결정을 모든 같은 intent에 적용하는 것이 아니다.
- `_AtomicManagerQueueStore.resolve_if_open`의 progress ledger는 `precedent | conflict_case` 두 효과만 알고, Request CAS·Authority write receipt·실행 재개를 표현하지 못한다. 항목을 닫을 때 ledger도 지우므로 부분 성공 뒤 재시작 가능한 증거가 되지 못한다.
- 카드 문자열을 지정하는 것만으로는 이 Question Request에 대한 중앙 Authority grant가 생겼다는 증거가 없다. 현재 Registry 카드와 Owner User, 카드 under-claim, `cannot_answer`, `approval_when`을 다시 읽고 request-scoped grant의 쓰기 결과와 읽기 결과를 exact-link해야 한다. 이 grant는 조직 전체 라우팅 규칙을 편집하거나 같은 intent의 다른 Request에 영향을 주지 않는다.
- request-aware `FromUnowned`의 intent 단일 원천은 `ManagerItem.source.decision.intent`다. 별도 `ManagerItem.intent`를 추가하면 둘이 갈릴 수 있다.

P17.4는 request-aware `FromUnowned`만 다룬다. `FromDeadlock`은 P17.5, `FromDispatch` 재배정은 P17.9 이후 범위다.

## 결정

### 1. 범위와 핵심 불변식

새 `P17ManagerDispositionApplication`은 다음 항목만 받는다.

```text
ManagerItem.request_id is nonblank
ManagerItem.source is FromUnowned
QuestionRequest.state is AwaitingManager(public_kind="unowned")
QuestionRequest.state.item_id == ManagerItem.item_id
QuestionRequest.request_id == ManagerItem.request_id
```

추가 exact-link 조건은 다음과 같다.

- `source.question == request.question`
- `source.decision.escalated_to == item.manager_id`
- 공백 intent를 `None`으로 정규화했을 때 `source.decision.intent == request.intent`
- 새 처분이면 Item은 `open`, `resolution is None`이어야 한다. 재시도면 저장된 action claim·resolved Item·Request 결과가 모두 같은 처분을 증명해야 한다.
- 처분 주체의 `org_id`는 Request와 같고 `subject_id`는 `item.manager_id`와 같아야 한다.

다음 불변식을 지킨다.

1. **같은 질문 보존** — 새 Request를 만들지 않고 기존 Request의 revision CAS만 사용한다.
2. **intent 단일 원천** — `FromUnowned.decision.intent`만 읽는다. Router를 다시 호출하거나 Manager 입력으로 intent를 새로 받지 않는다.
3. **분류되지 않은 공백은 배정 금지** — 정규화된 intent가 `None`이면 `AssignOwner`를 fail-closed한다. `Dismiss`는 허용해 질문을 명시적으로 닫는다.
4. **중앙 Authority 선행** — `AssignOwner`는 Registry·under-claim 검증과 request-scoped 중앙 Authority grant의 write/read exact 검증을 통과한 뒤에만 `ReadyToDispatch`로 간다.
5. **전역 학습 금지** — P17.4 application은 `PrecedentStore`와 Router에 의존하지 않고 둘을 호출하지 않는다. contextual Precedent는 P17.10이 별도로 결정한다.
6. **한 처분만 수용** — 같은 Item의 첫 유효 action claim만 이긴다. 같은 action은 멱등 수렴하고 다른 action은 명시적 conflict다.
7. **전이와 기록 구분** — Request·ManagerItem 전이는 도메인 상태다. request-scoped grant receipt와 action claim은 멱등·권한 증거이며 전역 라우팅 규칙이나 판례가 아니다.

### 2. sealed command·result·error

애플리케이션 경계의 새 DTO는 pydantic v2 `frozen=True`, `extra="forbid"`, `strict=True`를 사용한다. 기존 `ManagerActionRequest`는 웹 DTO로 남고 아래 command로 변환한다.

```python
class ManagerPrincipal(FrozenDto):
    org_id: str
    subject_id: str

class AssignUnownedOwner(FrozenDto):
    kind: Literal["assign_unowned_owner"]
    principal: ManagerPrincipal
    item_id: str
    agent_id: str
    rationale: str = ""

class DismissUnowned(FrozenDto):
    kind: Literal["dismiss_unowned"]
    principal: ManagerPrincipal
    item_id: str
    rationale: str = ""

P17ManagerDispositionCommand = AssignUnownedOwner | DismissUnowned
```

`Reroute`는 P17.4 command에 없다. request-aware `FromUnowned`에 들어오면 지원하지 않는 조합으로 거부한다.

action claim도 frozen sealed sum으로 둔다. reserved→sealed는 같은 값을 mutation하는 것이 아니라 새 값과 새 control handle로 교체하는 전이다. 모든 reservation은 Item별로 유일한 `generation`을 가진다.

```python
class ReservedAssignOwnerClaim(FrozenDto):
    kind: Literal["reserved_assign_owner"]
    generation: str
    idempotency_key: str
    request_id: str
    item_id: str
    org_id: str
    by_manager: str
    intent: str
    agent_id: str
    requires_approval: bool
    rationale: str

class SealedAssignOwnerClaim(FrozenDto):
    kind: Literal["sealed_assign_owner"]
    generation: str
    # ReservedAssignOwnerClaim과 같은 canonical action payload

class ReservedDismissClaim(FrozenDto):
    kind: Literal["reserved_dismiss"]
    generation: str
    # SealedDismissClaim과 같은 canonical action payload

class SealedDismissClaim(FrozenDto):
    kind: Literal["sealed_dismiss"]
    generation: str
    idempotency_key: str
    request_id: str
    item_id: str
    org_id: str
    by_manager: str
    rationale: str
    reason_code: Literal["manager_declined"]

ManagerDispositionClaim = (
    ReservedAssignOwnerClaim
    | SealedAssignOwnerClaim
    | ReservedDismissClaim
    | SealedDismissClaim
)

class ReservationControlToken(FrozenDto):
    generation: str
    token: str

class SealedClaimHandle(FrozenDto):
    generation: str
    forward_token: str

class ClaimAcquired(FrozenDto):
    claim: ReservedAssignOwnerClaim | ReservedDismissClaim
    control_token: ReservationControlToken

class ClaimInProgress(FrozenDto):
    kind: Literal["in_progress"]
    retryable: Literal[True] = True

class SealedClaimAvailable(FrozenDto):
    claim: SealedAssignOwnerClaim | SealedDismissClaim
    handle: SealedClaimHandle

class ClaimConflict(FrozenDto):
    kind: Literal["conflict"]

ClaimAttempt = ClaimAcquired | ClaimInProgress | SealedClaimAvailable | ClaimConflict
```

```python
class UnownedOwnerAssigned(FrozenDto):
    kind: Literal["owner_assigned"]
    request_id: str
    item_id: str
    route: RouteTarget
    wake: ExecutionWake

class UnownedDismissed(FrozenDto):
    kind: Literal["dismissed"]
    request_id: str
    item_id: str
    reason_code: Literal["manager_declined"]
    delivery: TerminalDelivery

P17ManagerDispositionResult = UnownedOwnerAssigned | UnownedDismissed
```

`ExecutionWake`는 `Started | AlreadyRunning | NotNeeded | Deferred`의 sealed sum이다. `Deferred`는 scheduler closed·capacity·submission failure처럼 Request가 `ReadyToDispatch`에 남아 재시도할 수 있는 경우다. `TerminalDelivery`는 `Published | AlreadyPublished | Deferred`다. 전달 실패가 terminal 전이를 되돌리지는 않으며 canonical GET과 늦은 SSE 구독은 저장된 Request를 다시 읽는다. durable 전달 outbox와 lease는 P17.9에서 닫는다.

오류도 호출자가 문자열을 파싱하지 않도록 닫힌 종류로 둔다.

```text
ManagerDispositionNotFound       # 404, 미존재와 스코프 밖 상세를 과도하게 반사하지 않음
ManagerDispositionForbidden      # 403, 현재 Manager 1인칭 위반
ManagerDispositionInvalid        # 400, 출처/intent/카드/행위 조합이 유효하지 않음
ManagerDispositionInProgress     # 409/Retry-After, 같은 command의 reserved winner가 진행 중
ManagerDispositionConflict       # 409, 다른 claim 또는 다른 Request winner
ManagerDispositionDependency     # 503, Registry/Authority/Store/scheduler의 재시도 가능 장애
ManagerDispositionIntegrity      # 500, 저장된 exact-link·receipt·version 손상
```

각 오류는 고정 `code`와 `retryable`을 갖는다. 외부 입력인 item ID·Agent Card ID·intent를 오류 본문에 그대로 반사하지 않는다.

### 3. action claim은 generation token을 가진 P17 전용 Store seam이다

기존 `_AtomicManagerQueueStore.resolve_if_open`는 재사용하지 않는다. legacy side effect 두 종류에 맞춘 프로세스 내 최적화라 P17의 Request/Authority 수명을 담을 수 없기 때문이다.

같은 Manager Store 객체가 아래 compound 포트를 구현한다.

```python
class RequestAwareManagerDispositionStore(
    RequestAwareManagerQueueStore,
    Protocol,
):
    def reserve_validated_action(
        self,
        item_id: str,
        command: P17ManagerDispositionCommand,
        validate: Callable[
            [ManagerItem],
            ReservedAssignOwnerClaim | ReservedDismissClaim,
        ],
    ) -> ClaimAttempt: ...

    def claim_for_item(self, item_id: str) -> ManagerDispositionClaim | None: ...

    def seal_claim(
        self,
        claim: ReservedAssignOwnerClaim | ReservedDismissClaim,
        *,
        control_token: ReservationControlToken,
    ) -> SealedClaimAvailable: ...

    def abandon_unmutated_claim(
        self,
        claim: ReservedAssignOwnerClaim,
        *,
        control_token: ReservationControlToken,
    ) -> None: ...

    def record_resume_evidence(
        self,
        handle: SealedClaimHandle,
        evidence: ResumeEvidence,
    ) -> None: ...

    def resume_evidence_for_claim(
        self,
        handle: SealedClaimHandle,
    ) -> ResumeEvidence | None: ...

    def resolve_for_claim(
        self,
        handle: SealedClaimHandle,
        resolved: ManagerItem,
    ) -> ManagerItem: ...
```

`reserve_validated_action`은 Store 임계구역에서 해당 Item을 잠정 선점하고, side effect가 없는 검증 callback을 실행한 뒤 성공한 reservation만 공개한다. callback이 Registry 부재·Owner 부재·under-claim 위반처럼 잘못된 대상을 발견해 예외를 내면 reservation을 남기지 않는다. 따라서 ADR 0042의 “검증 실패 시 Item을 열어 다른 대상을 고른다”는 규칙을 보존한다.

같은 Item의 validation callback이 `reserve_validated_action`에 재진입하면 별 reservation이나 token을 만들지 않고 fail-closed한다. `RLock`의 재진입 가능성을 중첩 claim 허용으로 해석하지 않는다.

첫 caller만 `ClaimAcquired.control_token`을 받는다. 같은 command의 follower가 reservation이 reserved인 동안 들어오면 token 없이 `ClaimInProgress`를 받고 재시도한다. 다른 command는 `ClaimConflict`다. reservation이 sealed된 뒤 같은 command가 오면 Store가 generation에 묶인 `SealedClaimHandle`을 발행해 forward retry를 허용한다.

`generation`은 매 reservation마다 새 값이다. generation 1을 abandon한 뒤 같은 Item에 generation 2가 생기면 generation 1의 control token과 forward handle은 `seal_claim`·`abandon_unmutated_claim`·`record_resume_evidence`·`resolve_for_claim` 어느 것도 조작하지 못한다. 공개 mutator는 전달된 token/handle의 generation과 secret를 stored generation에 exact 비교한다. claim 객체나 idempotency key만으로는 전이할 수 없다. 이 규칙이 abandon→re-reserve 사이의 ABA를 막는다.

reserved claim도 처리 중에는 다른 action을 막는다. `Dismiss`는 첫 caller의 control token으로 바로 sealed한다. `AssignOwner`는 아래 Authority 결과에 따라 상태를 정한다.

- Registry/under-claim 검증 실패: claim을 공개하지 않는다.
- Authority가 **정책 거부이며 write 0**임을 typed result로 보증: 첫 caller만 control token을 제시해 `abandon_unmutated_claim`으로 reserved claim을 지우고 다른 대상을 고를 수 있다.
- Authority receipt 반환: 첫 caller가 같은 control token으로 즉시 `seal_claim`한다.
- timeout·connection loss처럼 write 여부를 모르는 실패: 첫 caller가 보수적으로 `seal_claim`한다. 프로세스 중단 뒤에도 reserved가 남으면 같은 command follower는 함부로 seal하지 않고 운영 recovery가 reservation owner를 확인한다. durable owner/expiry recovery는 P17.9 범위다.

receipt 뒤 read-back mismatch나 손상에서는 claim을 절대 release하지 않는다. 중앙 grant가 이미 실행 경로에 보였을 수 있기 때문이다. 같은 command fingerprint는 sealed 뒤에만 forward handle을 받고, 다른 command는 `ManagerDispositionConflict`다. fingerprint에는 action 종류, 조직·Manager, item/request, intent, 대상 Agent Card, rationale, 고정 reason code가 들어간다. `claimed_at`, generation, control token은 semantic action 비교에서 제외한다.

claim idempotency key는 다음으로 고정한다.

```text
manager-disposition:{item_id}
```

Item 하나에 처분 하나만 허용하므로 action payload를 key에 넣지 않는다. 같은 key의 다른 payload는 Store와 request-scoped Authority 모두 충돌로 거부한다. claim generation, `ResumeEvidence`, resolved guard는 Item이 resolved된 뒤에도 남아 부분 성공 재시도의 증거가 된다. P17.4 구현은 단일 프로세스 InMemory 보장이고, durable schema·다중 인스턴스 transaction은 P17.9 책임이다.

```python
class ResumeEvidence(FrozenDto):
    request_id: str
    from_revision: int
    to_revision: int
    route: RouteTarget
    attempt: Literal[1]
    trigger_key: str
```

`ResumeEvidence`는 `AwaitingManager → ReadyToDispatch` CAS 성공 뒤 같은 sealed handle로 Store progress에 exact 기록한다. 같은 handle과 같은 evidence의 재기록은 멱등이고, 같은 generation의 다른 evidence는 conflict 또는 integrity 오류다. Store는 resolved 전이에도 `(generation, action fingerprint, ResumeEvidence | None, ManagerResolution)` guard를 보존한다. 같은 claim의 terminal retry는 이 guard와 terminal evidence가 일치할 때만 멱등 성공이다.

`resolve_for_claim`은 exact `SealedClaimHandle`만 받고 다음을 한 임계구역에서 확인한다.

- claim이 그 Item과 Request의 저장된 winner인가
- resolved Item이 원 `item_id`, `request_id`, `source`, `manager_id`, `created_at`을 보존하는가
- `ManagerResolution.action`이 claim의 command와 정확히 같은가
- 이미 resolved라면 같은 resolution만 멱등 반환하고 다른 resolution은 거부하는가

legacy 공개 `enqueue`, `mark_resolved`, `_AtomicManagerQueueStore.resolve_if_open`는 `request_id is not None`인 Item을 모두 거부한다. request-aware Item은 최초 `create_or_get_for_request`와 위 reservation/claim-bound 경로만 쓴다. 따라서 서비스 분기 하나가 빠져도 legacy 공개 write가 P17 Item을 닫지 못한다.

### 4. `Dismiss`는 같은 Request를 명시적으로 종결한다

`DismissUnowned`는 답을 만들거나 Authority를 바꾸지 않는다.

```text
exact Request/Item read
→ validated Dismiss reservation
→ first caller control token으로 claim seal
→ Request CAS:
   AwaitingManager(item_id) → DeclinedRequest(reason_code="manager_declined")
→ 같은 claim으로 ManagerItem resolved
→ 저장된 Request를 exact-read한 terminal SSE publish
```

Manager rationale는 운영 기록에 남길 수 있지만 사용자 결과에 그대로 내보내지 않는다. 사용자 표면은 고정 reason code와 중립 메시지를 투영한다. `DeclinedRequest`에는 Handling Assignment가 없고 terminal이라 다시 살아나지 않는다.

Request CAS 뒤 Item resolve가 실패하면 Request는 이미 정직한 terminal 결과다. 같은 action 재시도는 exact `DeclinedRequest`를 확인하고 Item만 같은 claim으로 닫는다. 다른 action은 sealed claim 때문에 거부된다.

### 5. `AssignOwner`는 Registry와 중앙 Authority를 차례로 검증한다

Registry 검증은 현재 snapshot에 대해 다음 순서로 한다.

1. `FromUnowned.decision.intent`를 공백 정규화한다. `None`이면 거부한다.
2. `registry.get(agent_id)`로 현재 Agent Card를 읽는다.
3. `registry.get_user(card.owner)`로 현재 Owner User의 존재를 확인한다.
4. `domain_authorized(intent, card)`를 적용한다. 즉 `intent in card.domains`이고 `intent not in card.cannot_answer`여야 한다.
5. `requires_approval = intent in card.approval_when`으로 계산한다. command나 Manager가 이 값을 지정하지 못한다.

위 card·Owner User 읽기는 `_validate_registry` 한 호출 안의 짧은 Registry snapshot으로 선형화한다. reservation callback과 Authority write 뒤 재검증은 이 규칙을 함께 쓴다. deadline policy와 Question Request Store의 `get`·CAS는 Registry snapshot 밖에서 호출해 Registry→Request/UoW lock 역순을 만들지 않는다. Request CAS 전·후에는 각각 새 snapshot으로 재검증한다. snapshot 전에 끝난 Registry 변경은 현재 값으로 검사해 fail-closed하고, snapshot 뒤 변경은 이후 변경으로 선형화한다.

`can_answer`는 Authority 필드가 아니라 설명적 under-claim이므로 새 권한을 만들지 않는다. P17.4의 실행 허용 판단은 `domains`·`cannot_answer`와 중앙 Authority가 맡는다.

Registry 검증이 만든 claim은 다음 RouteTarget 후보를 보존한다.

```text
intent             = FromUnowned.decision.intent
agent_id           = 현재 Agent Card.agent_id
requires_approval  = intent in current card.approval_when
authority_version  = request-scoped grant write/read exact 검증 뒤 채움
```

reserved/sealed claim은 검증 당시 snapshot이지 현재 Registry를 대신하지 않는다. 같은 action을 재시도할 때도 Request CAS 직전에 Agent Card와 현재 Owner User를 다시 읽고, `agent_id`·현재 Owner 존재·`domain_authorized`·`requires_approval` 계산이 claim과 정확히 같은지 확인한다. claim 뒤 Owner가 다른 유효 User로 transfer된 것은 허용한다. claim은 Owner ID를 권한 snapshot으로 고정하지 않으며, 실행·Finalization은 현재 Owner에게 책임을 귀속한다. 반면 카드 삭제, Owner User 부재, `cannot_answer`·`approval_when` 변화는 stale snapshot이므로 Request를 전이하지 않는다. Authority side effect 전이면 claim을 폐기할 수 있고, receipt 또는 결과 불명 상태 뒤라면 sealed claim을 유지한 채 integrity/dependency 오류로 멈춰 운영 복구 후 같은 action만 재시도한다.

### 6. Authority는 조직 규칙 편집이 아니라 request-scoped 중앙 grant다

P17.4는 기존 `RouteAuthority.authorize(org_id, intent, agent_id)`의 의미를 바꾸지 않는다. 이 메서드는 최초 `Routed`를 위한 base policy reader다. Manager가 Unowned 질문 하나에 담당을 지정하는 행위는 조직 전체 규칙을 편집하지 않고, 그 `request_id`에만 적용되는 중앙 grant를 만든다.

```python
class AuthorityAssignment(FrozenDto):
    org_id: str
    request_id: str
    item_id: str
    intent: str
    agent_id: str
    assigned_by: str
    idempotency_key: str

class AuthorityAssignmentReceipt(FrozenDto):
    assignment: AuthorityAssignment
    grant_version: str

class AuthorityAssignmentRejected(FrozenDto):
    kind: Literal["rejected"]
    authority_write_applied: Literal[False] = False
    idempotency_write_applied: Literal[False] = False
    reason_code: str

class RequestScopedRouteAuthority(RouteAuthority, Protocol):
    def assign_owner(
        self,
        assignment: AuthorityAssignment,
    ) -> AuthorityAssignmentReceipt | AuthorityAssignmentRejected: ...

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None: ...
```

버전과 멱등 의미는 다음과 같다.

- grant는 exact `(org_id, request_id, item_id, intent, agent_id, assigned_by)` provenance를 보존한다. `authorize_for_request`는 같은 조직·Request·intent·Agent Card 조합만 허용한다.
- `grant_version`은 그 request-scoped grant의 revision이다. `RouteTarget.authority_version`에는 이 값을 넣는다.
- 같은 `idempotency_key`와 같은 assignment는 같은 receipt와 같은 grant version을 돌려준다. 새 version을 만들지 않는다.
- 같은 key의 다른 assignment는 conflict다.
- 정책 거부는 `AuthorityAssignmentRejected(authority_write_applied=False, idempotency_write_applied=False)`로만 표현한다. 이 결과는 grant bytes뿐 아니라 idempotency key·receipt reservation도 전혀 쓰지 않았다는 포트 계약이다. 이 typed result에서만 reserved claim을 버리고 다른 대상을 선택할 수 있다.
- 예외는 write 0을 뜻하지 않는다. timeout·연결 단절·알 수 없는 오류는 성공 여부가 불명확하므로 claim을 sealed하고 같은 key로 재조회·재시도한다.
- writer가 성공한 뒤 같은 객체의 `authorize_for_request(org_id, request_id, intent, agent_id)`를 다시 호출한다. grant가 없거나 `grant.policy_version != receipt.grant_version`이면 Request를 전이하지 않는다.
- `AuthorityGrant.policy_version` 필드명은 기존 RouteTarget 호환 envelope를 유지한다. `authorize_for_request`가 반환할 때 그 값의 의미는 base policy version이 아니라 request grant version이다.
- `AuthorityAssignmentReceipt.assignment`도 입력과 exact equality여야 한다. writer가 대상이나 조직을 바꿔 돌려주면 integrity 오류다.
- receipt 뒤 authorize/version mismatch에서는 sealed claim을 풀지 않는다. 운영자가 Authority 일관성을 복구한 뒤 같은 action만 다시 진행한다.
- request-scoped grant는 base `RouteAuthority.authorize`의 policy bytes를 바꾸지 않고, 다른 Request의 grant나 이미 만들어진 `ReadyToDispatch.authority_version`에도 영향을 주지 않는다. 같은 `(org_id, intent, agent_id)`가 여러 Request에서 반복돼도 각 grant version은 서로 독립이다. reviewer가 지적한 “같은 triple의 version 변경이 기존 Ready route를 stale하게 만드는 문제”는 이 scope 분리로 제거된다.

`DemoRouteAuthority`는 base `authorize`, `assign_owner`, `authorize_for_request`를 한 객체에서 구현하는 InMemory adapter로 확장한다. base policy와 request grant map은 의미상 분리하되 같은 lock과 객체 identity 아래 둔다. 이 adapter는 개발 증거일 뿐 durable Authority나 production RBAC가 아니다. request grant의 durable 저장, 정책 관리자 권한, 조직 격리는 P17.8에서 교체한다.

grant write가 성공하고 Request CAS 전에 실패하면 그 Request의 중앙 grant는 남을 수 있다. 이를 보상 삭제하지 않는다. sealed claim과 Authority idempotency key로 같은 action을 재시도해 수렴한다. grant가 다른 Request나 base policy에 영향을 주지 않으므로 forward recovery의 경계도 해당 Request 안에 머문다.

P17 Answer Source는 Request 전체를 보고 Authority reader를 고른다.

```text
initial_disposition == "unowned"
  → authorize_for_request(org_id, request_id, route.intent, route.agent_id)
그 외 기존 Routed 경로
  → authorize(org_id, route.intent, route.agent_id)
```

Unowned에서 재개된 Request인데 Answer Source가 `authorize_for_request` capability를 갖지 않거나 base `authorize`로 폴백하면 조립 또는 실행을 fail-closed한다. 두 reader의 반환 version은 모두 저장된 `RouteTarget.authority_version`과 exact match해야 한다. P17.5의 Contested grant 적용은 그 슬라이스에서 별도로 확정한다.

### 7. 같은 Request를 Router 없이 `ReadyToDispatch`로 옮긴다

request-scoped grant exact 검증이 끝나면 현재 `AwaitingManager`에서 다음 값을 만든다. Request CAS 바로 전에는 section 5의 현재 Registry derived 검증도 다시 통과해야 한다.

```text
RouteTarget(
    intent=claim.intent,
    agent_id=claim.agent_id,
    requires_approval=claim.requires_approval,
    authority_version=receipt.grant_version,
)
attempt = 1
trigger_key = request-dispatch:{request_id}:1
HandlingAssignment(
    kind="system",
    ref=trigger_key,
    due_at=deadline_policy(..., "ready_to_dispatch", transition_time),
)
```

`QuestionRequest.transition`과 revision CAS를 사용한다. Router는 호출하지 않는다. `initial_disposition`은 최초 사실인 `unowned`로 남고, Request의 intent는 기존 nested intent와 같은 값으로 유지한다. 새 Request나 새 ManagerItem을 만들지 않는다.

CAS가 졌거나 같은 action을 재시도할 때는 상태별 증거를 다르게 요구한다.

- `ReadyToDispatch`: RouteTarget·attempt 1·trigger key·Handling Assignment가 proposed target과 exact여야 한다.
- `AwaitingAnswer | AwaitingApproval`: 저장된 route와 attempt가 proposed target과 exact여야 한다.
- `AnsweredRequest`: 같은 Completion Reader의 Terminal Answer Audit가 request ID·record ID·route·attempt·authority version을 proposed target과 exact-link해야 한다. Answered 상태만 보고 성공으로 간주하지 않는다.
- `FailedRequest`이면서 Item이 아직 open: 같은 claim generation의 `ResumeEvidence(from_revision, to_revision, route, attempt, trigger_key)`가 Store progress에 exact 기록된 경우에만 Item repair를 허용한다. Failed 상태만으로는 어떤 route를 거쳤는지 알 수 없으므로 evidence가 없으면 integrity 오류다.
- Item이 이미 resolved인 같은-claim terminal retry: Store의 resolved guard와 `ResumeEvidence`, terminal audit 또는 exact Declined가 모두 같은 action을 증명할 때만 멱등 결과다.
- `Dismiss`: `DeclinedRequest.reason_code == "manager_declined"`, 같은 sealed Dismiss claim, 같은 resolved guard가 모두 exact여야 한다.

다른 RouteTarget, 증거 없는 terminal, 다른 ManagerItem, 상관키 손상은 conflict 또는 integrity 오류다. P17 Manager application은 Finalization과 같은 Completion Reader identity를 받아 Answered terminal audit를 검증한다.

AssignOwner Request CAS가 성공하면 같은 sealed handle로 `ResumeEvidence`를 기록한 뒤 ManagerItem을 resolve한다. 그 다음 Question Surface가 소유한 execution starter의 `ensure_started(request_id)`를 호출한다. Manager application이 scheduler job callback이나 Runtime을 직접 받지 않게 해 다른 실행 경로를 만들지 않는다.

### 8. 부분 실패 수렴

| 마지막으로 성공한 단계 | 관측 상태 | 같은 action 재시도 | 다른 action |
|---|---|---|---|
| Registry 검증 전/검증 실패 | Item open, claim 없음, Request AwaitingManager | 새로 검증 | 허용 |
| Authority 명시 거부(grant/idempotency/receipt write 0) | Item open, reservation 폐기, Request AwaitingManager | 다른 대상 포함 새 generation으로 검증 | 허용 |
| claim reserved | Item open, Request AwaitingManager | 첫 caller만 control token으로 진행, follower는 retryable in-progress | conflict |
| claim sealed/Authority 결과 불명 | Item open, Request AwaitingManager | same-command follower가 generation-bound handle로 forward retry | conflict |
| request grant write | receipt 존재, Request AwaitingManager | 같은 key로 같은 receipt, request-aware read exact 검증 후 CAS | conflict |
| Request CAS: AssignOwner | Request ReadyToDispatch 또는 증명된 후손, Item open 가능 | 상태별 exact evidence 확인, ResumeEvidence 기록, Item resolve·wake | conflict |
| Request CAS: Dismiss | Request Declined, Item open 가능 | 같은 reason 확인 후 Item resolve·terminal publish | conflict |
| Item resolve | Request 전이+Item resolved, claim 보존 | 저장본 exact 확인 후 wake/publish만 재시도 | conflict |
| execution wake/publish | 실행 중·terminal 또는 broker sealed | `AlreadyRunning | NotNeeded | AlreadyPublished`로 멱등 수렴 | conflict |

어느 실패에서도 Router, global Precedent, org-wide Authority rule edit를 호출하지 않는다. `ReadyToDispatch`가 남으면 같은 Manager action 재시도와 SSE reconnect가 기존 scheduler를 다시 깨운다. canonical GET은 저장 상태만 조회한다. startup recovery와 durable wake/outbox는 P17.9 범위다. 외부 LLM 계산 exactly-once, 다중 인스턴스 action claim, durable request grant도 주장하지 않는다.

### 9. composition identity gate와 legacy 우회 차단

`QuestionSurfaceComposition`은 P17.4 뒤 다음 identity를 함께 소유한다.

```text
Request Store used by intake/finalization
    is Request Store used by P17ManagerDispositionApplication

Manager Store used by initial Unowned enqueue
    is RequestAwareManagerDispositionStore used by action claim/resolve

RouteAuthority used by initial routing and answer-source revalidation
    is RequestScopedRouteAuthority used by assign_owner,
       authorize_for_request and Answer Source read-back

Completion Reader used by Finalization and canonical lookup
    is Completion Reader used by disposition terminal audit validation
```

action claim은 같은 Manager Store의 capability여야 한다. 같은 파일 경로나 equality 비교, 별도 proxy는 identity 증거가 아니다. 조립은 reservation generation/token, request grant reader/writer, Completion Reader 필수 callable과 `is` identity를 확인하고 하나라도 다르면 시작을 거부한다. Registry 기반 target validator와 Answer Source도 같은 composition에서 만들며, 실행 직전 Answer Source가 현재 Registry와 request-scoped grant를 다시 검증한다.

웹 `/manager/items/{item_id}/act`는 저장된 Item을 먼저 읽어 다음처럼 분기한다.

- request-aware `FromUnowned` → 오직 `P17ManagerDispositionApplication`
- `request_id is None`인 legacy 항목 → 기존 `ManagerQueueService`
- request-aware `FromDeadlock | FromDispatch` → 후속 슬라이스 전까지 fail-closed

방어를 한 겹 더 두기 위해 `ManagerQueueService.act`뿐 아니라 Store의 legacy 공개 `enqueue`, `mark_resolved`, `_AtomicManagerQueueStore.resolve_if_open`도 `request_id is not None`인 Item을 거부한다. request-aware Item은 `create_or_get_for_request`와 generation-bound claim 경로만 쓴다. 이로써 다른 어댑터가 웹 분기를 우회해 P17 Item을 큐만 닫거나 Precedent로 흘릴 수 없다.

Manager 운영 projection은 request-aware `FromUnowned`에 `request_id`와 nested `decision.intent`를 명시적으로 보여준다. 다만 P17.4의 action 조작은 API endpoint까지가 범위다. Manager 화면의 새 Assign/Dismiss control, confirmation UX, retry 상태 표시는 이 슬라이스에서 완성됐다고 주장하지 않는다.

native `GET /requests/{request_id}/stream?watch=true`는 이미 열린 구독에서 `Pending`을 종료 신호로 보지 않고 terminal·중단 또는 제한된 idle poll까지 기다린다. 이 opt-in 경로가 Manager Dismiss의 `pending → declined`와 AssignOwner의 `pending → done`을 같은 HTTP 응답에서 전달한다. query가 없거나 `watch=false`인 native reconnect, 최초 `POST /requests`, legacy `/ask/stream`은 기존 Pending one-shot 계약을 유지한다. `watch`는 단일 `true | false`만 받고 그 밖의 값과 중복 값을 거부한다. 모든 종료·disconnect·ASGI 실패 경로는 구독만 닫으며 application이 소유한 producer는 취소하지 않는다.

웹이 명시 주입된 `QuestionSurfaceComposition`의 수명을 인수한 뒤 Manager Store identity를 포함한 startup 조립 검증이 실패하면 scheduler와 storage를 순서대로 정리한다. cleanup 자체의 실패는 다음 리소스 정리를 막지 않으며 startup 오류에 note로 남는다.

### 10. 슬라이스와 TDD handoff

#### S0 — 계약 red

- sealed command/result/error와 `manager_declined` reason code
- request-aware FromUnowned 외 조합 거부
- nested intent 단일 원천, `intent=None` AssignOwner 거부·Dismiss 허용
- Router·Precedent import/call 0 구조 가드

#### S1 — exact request context와 legacy 차단

- request/item/source/question/manager/org/intent exact-link
- Item·Request·nested source tamper fail-closed
- `ManagerQueueService.act`와 legacy Store `enqueue/mark_resolved/resolve_if_open`의 request-aware 항목 거부
- legacy `request_id=None` 회귀 green
- Manager projection에 request ID·nested intent 노출, action UI는 API-only임을 회귀 고정

#### S2 — Dismiss 최소 종결 루프

- 같은 Request `AwaitingManager → DeclinedRequest(manager_declined)` CAS
- Item same-claim resolved, 사용자 canonical GET과 active/late SSE Declined
- Request CAS 성공 뒤 Item/publish 실패 재시도 수렴

#### S3 — AssignOwner Registry·Authority·재개

- 현재 Agent Card와 Owner User, `domains`, `cannot_answer`, `approval_when` 검증
- `DemoRouteAuthority` base reader와 request grant read/write의 single object identity
- `authorize_for_request` receipt/grant version exact-link와 tamper/deny/failure
- 같은 org/intent/Agent Card의 다른 Request grant와 기존 Ready route version 불변
- Unowned Answer Source의 request-aware reader 필수·base authorize fallback 금지
- 같은 Request `ReadyToDispatch`, attempt 1, exact trigger/ref, Router 0
- global Precedent 0, 실행 starter로 한 번만 wake

#### S4 — action claim과 경쟁·부분 실패

- 동일 action 32-way: 한 reservation control token, follower in-progress/sealed forward retry, grant write·Request CAS·Item resolve·wake 최대 한 winner
- AssignOwner 대 Dismiss 32-way: 한 winner, loser conflict, 혼합 terminal 0
- abandon→새 reservation ABA에서 stale generation/control/forward token 전부 거부
- Registry validation 실패와 typed grant reject(write 0)는 다른 대상 선택 가능
- Authority success/CAS fail, CAS success/Item fail, Item success/wake fail fault point별 같은 action 수렴
- claim 뒤 CAS 전 Registry 재검증, 유효 Owner transfer 허용, under-claim/approval 변경 거부
- Ready/Awaiting route exact, Answered terminal audit exact, Failed open-item ResumeEvidence 필수
- 역행 clock, Store 반환 치환, receipt/key/version/route/evidence 변조 fail-closed

#### S5 — composition·web·회귀

- Request/Manager/request grant/claim/Completion Reader identity gate
- 웹 request-aware 분기와 HTTP 400/403/404/409/503 안전 매핑
- blocking 질문자가 Manager 처분 뒤 같은 Request 답/거절을 조회
- active SSE와 늦은 SSE reconnect 모두 같은 Request terminal 수신
- MCP `get_question`도 같은 결과 조회
- 기존 legacy Manager queue·FromDeadlock·FromDispatch 회귀는 그대로 보존

#### 구현 검증(2026-07-13)

- S0~S4 코어는 전체 Request/Item/claim/grant/ResumeEvidence 경계의 복사·재검증, 32-way 경쟁, fault 수렴, legacy write 차단을 포함해 독립 승인받았다.
- S5는 실제 HTTP adapter를 통과하는 active watch와 late reconnect, Manager Store identity startup cleanup을 보완한 뒤 독립 재리뷰에서 P0/P1/P2 없이 승인받았다.
- 최종 게이트: P17.4 집중 `pytest` 49 passed, 전체 `pytest` 3,812 passed, pyright 0, Ruff 통과, P17.4 변경 파일 format 통과, `git diff --check` 통과.
- 이 수치는 개발 회귀 증거다. InMemory claim/grant, demo principal·Authority, process-local broker/scheduler 때문에 production 준비 완료를 뜻하지 않는다.

구현은 `tdd-engineer`가 red→green을 맡았고, 조립·웹·실행 starter는 `mcp-runtime-engineer`가 병행했다. 구현 뒤 `code-reviewer`가 Authority 중앙, legacy 우회, 경쟁·부분 실패, 전역 Precedent 0을 독립 검증했다.

## 기각한 대안

- **기존 `ManagerQueueService`에 Request Store를 선택 주입** — legacy Precedent와 request-aware 수명이 한 서비스에 섞이고, 주입 누락 때 다시 큐만 닫는 fail-open 경로가 생긴다.
- **`_AtomicManagerQueueStore.resolve_if_open` 확장** — 이름과 effect enum을 계속 넓혀도 Authority receipt와 Request CAS의 durable 순서를 표현하지 못한다. legacy 최적화와 P17 claim을 분리한다.
- **Registry 검증 전에 영구 claim** — Manager가 잘못 고른 카드 하나가 Item을 영구 잠가 ADR 0042의 “다른 대상 선택”을 깨뜨린다. Registry 검증까지는 무흔적 잠정 선점, 검증 뒤에는 reserved claim, Authority side effect가 확인됐거나 결과가 불명확해진 뒤에는 sealed claim으로 나눈다.
- **Authority write 실패면 항상 claim 삭제** — write 성공 여부를 모르는 timeout에서 다른 target grant가 같은 Request에 생길 수 있다. Authority bytes와 idempotency/receipt reservation 모두 write 0을 보증한 typed 정책 거부만 삭제하고, 그 밖에는 claim을 sealed해 같은 key로만 재시도한다.
- **request grant write를 보상 삭제** — 같은 Request 실행 경로가 이미 읽었을 수 있어 안전하지 않다. idempotent forward recovery를 택한다.
- **Manager 배정을 org-wide Authority rule edit로 기록** — 같은 `(org_id, intent, agent_id)`의 다른 Request와 기존 Ready route version을 뜻밖에 바꾼다. P17.4는 request/item provenance를 가진 request-scoped grant만 만든다.
- **해소 뒤 Router 재호출** — Manager의 확정 결정을 잃고 다시 Unowned·Contested가 될 수 있다. 저장된 target으로 직접 재개한다.
- **P17.4에서 global Precedent 생성** — 조직·조건·유효기간 없는 intent 전역 규칙이 된다. contextual Precedent는 P17.10으로 미룬다.
- **`ManagerItem.intent` 추가** — nested `FromUnowned.decision.intent`와 두 진실 원천이 된다.

## 남은 범위

- P17.5: request-aware Contested 합의·Manager 중재와 후보 검증
- P17.8: production base `routing_rules.yaml` 또는 동등 정책 저장소, durable request grant 저장, default-deny RBAC, org 격리, 정책 관리자 권한
- P17.9: ManagerItem·action claim·Authority/Request 전이의 durable transaction, 다중 인스턴스 lease, wake/delivery outbox 소비와 복구
- P17.10: 조직·조건·적용 범위·유효기간·근거·정책 버전을 가진 contextual Precedent
- P17.13: Manager 처분 감사·관측성·SLO·보존·운영 runbook

## 불변식 자체점검

- **사용자 결과 기준 미아 없음 — 강화.** Dismiss는 같은 Request를 Declined로 닫고, AssignOwner는 같은 Request를 실행 가능한 상태로 되돌린다.
- **등록 무결성 — 보존.** 현재 Registry에 있고 Owner User가 실재하는 Agent Card만 선택한다.
- **Authority 중앙 — 강화.** 카드 under-claim만으로 실행하지 않고 request/item provenance를 가진 중앙 grant receipt와 같은 request-aware reader 결과를 exact 검증한다. 다른 Request의 규칙은 바꾸지 않는다.
- **전이 ≠ 기록 — 보존.** Request/Item 전이, Authority receipt/action claim, 사용자 전달을 구분한다.
- **책임 확정 전 답 금지 — 보존.** Authority까지 검증된 RouteTarget이 생기기 전 Runtime과 Finalization을 호출하지 않는다.
- **노출 불변식 — 보존.** 사용자에게 Manager rationale·Authority 내부 key를 노출하지 않고 중립 Declined/진행 결과만 투영한다.
