# ADR 0066 — 한 Question Request의 반복 사람 처분과 dispatch timeout escalation

- 상태: **채택(Accepted, 2026-07-25 — 사용자 확인 완료)**. 확인 내용: "한 Request가 사람 처분 큐에 **실행 시도(attempt)마다 최대 한 번** 들어갈 수 있다"(결정 §1의 제안 답)와 그 저장 경계(FromDispatch Item은 S5 소유 `durable_dispatch_escalation_v1` component·`UNIQUE(request_id, attempt)`·S4.1 DDL 무변경)를 사용자가 승인했다. 기각 대안(평생 1회 유지 = 재실행 실패 시 사람에게 다시 묻지 않음)은 그 Request가 `AwaitingAnswer`에서 종착을 잃어 미아 없음 불변식을 깨므로 채택하지 않았다.
- 날짜: 2026-07-25
- 계보: ADR 0042(§2 전이표·§4 사람 처분·§9 S5 경계)·ADR 0014(Manager 큐)·ADR 0050 §12(`manager.act`)·ADR 0065 §11·§12(FromDeadlock 소비·linked command 정합)를 잇는다. ADR 0042 §9 S5.1~S5.5는 이 결정에 의존하지 않는다 — S5.6만 이 결정 위에 선다.
- 구현 상태: 미구현. 이 ADR은 P17.9 S5.6 착수 전에 사용자 확인을 받아야 하는 **제품 의미 결정**을 명시한다.

## 맥락 — 세 겹으로 얽힌 사실

dispatch timeout escalation(S5.6)은 “워커가 SLA 안에 답하지 않은 실행 시도를 사람에게 넘긴다”이다. ADR 0042 §2 전이표는 이 경로를 이미 모델링하고 있다.

```text
AwaitingAnswer ── AwaitingManager(public_kind="dispatched")
AwaitingManager ─ ReadyToDispatch(attempt + 1)
```

즉 **timeout → 사람 → 재실행**은 도메인이 이미 허용한 정상 순환이다. 그런데 구현 사실 셋이 이 순환을 durable하게 표현하지 못하게 막는다.

1. **`durable_linked_manager_items.request_id`가 UNIQUE다**(S4.1 DDL). 한 Question Request는 durable Manager 처분 큐에 **평생 한 번만** 들어갈 수 있다. Unowned로 시작해 배정된 Request가 나중에 dispatch timeout을 맞으면 두 번째 Item을 만들 수 없고, 재실행(attempt 2)이 다시 timeout을 맞아도 마찬가지다. S4.1 manifest는 DDL catalog를 exact 봉인하며 v1→v2 마이그레이션 경로가 없다.
2. **`sqlite_durable_linked_reconciliation.py`의 assign arm이 `request.state.attempt == 1`을 무조건 요구한다**(ADR 0065 §12 Q1① 문면도 같다). dispatched Manager 대기에서 재개하면 attempt는 `직전 + 1`(≥2)이므로, FromDispatch Item이 이 테이블에 들어오는 순간 정상 reroute가 `request_state_inconsistent`로 오탐된다. 현재는 durable reroute writer가 없어 발화하지 않은 **잠재 결함**이다.
3. **`manager_queue.py`의 request-aware fingerprint가 FromDispatch를 `ValueError`로 거부한다.** 이는 InMemory request-aware store의 중복 적재 방지 경로이며 durable 경로와는 별개다.

## 결정할 것 — “한 Request가 사람 처분 큐에 두 번 이상 들어갈 수 있는가”

**제안 답: 그렇다. 실행 시도(attempt)마다 최대 한 번이다.**

근거는 전이표 자체다. `AwaitingManager(dispatched) → ReadyToDispatch(attempt+1) → AwaitingAnswer → (다시 timeout) → AwaitingManager`는 도메인이 이미 허용한 순환이고, 두 번째 진입을 막으면 그 Request는 재실행 뒤 다시는 사람에게 닿지 못한다 — 즉 **사용자 결과 기준 미아**가 된다. 또한 “Unowned로 시작해 배정된 뒤 dispatch timeout”은 한 수명에 서로 다른 두 처분이 필요한 가장 흔한 경로다. 따라서 `UNIQUE(request_id)`는 ADR 0042 §2 전이표와 모순되는 **모델링 결함**이며, 올바른 grain은 WorkTicket과 같은 **`(request_id, attempt)`**다.

“평생 한 번”을 유지하는 선택지는 제품 의미상 “재실행은 실패해도 사람에게 다시 묻지 않는다”가 되므로 채택하지 않는다.

## 결정 — 저장 경계

### 1. FromDispatch Item은 S5 소유 신 component에 둔다 (권장)

S5.6은 `durable_dispatch_escalation_v1` component를 신설하고 `durable_dispatch_manager_items`(가칭)를 소유한다. 키는 **`UNIQUE(request_id, attempt)`**이며 `ticket_id`로 timeout된 WorkTicket에 FK 결박한다. S4.1 `durable_linked_manager_items`는 **escalation(FromDeadlock)·Unowned ingress가 만든 Item만** 계속 소유한다.

- **장점:** S4.1 DDL 무변경(봉인 유지)·S4.6 무변경·기존 행 마이그레이션 0. 두 테이블의 소유자가 각각 명확하다(“생성 주체가 소유한다”). ADR 0042 §9 ①의 component 분리 규율과 같은 결이다.
- **단점:** Manager 처분 큐가 두 테이블로 갈린다. 처분 UoW·resolver·화면 투영이 둘을 union해야 한다.
- **비용 실측(중요):** 오늘 durable ManagerItem을 읽는 사람 화면 투영은 **0건**이다(`durable_linked_manager_items`를 읽는 곳은 escalation UoW·escalation reconciliation·처분 UoW·S4.6 게이트뿐). 즉 “화면 union” 비용은 S5.6이 새로 만드는 부채가 아니라 S4.4 시점에 이미 존재하는 미완결(durable Manager 큐 투영 부재)이다. 실제 증분은 **처분 UoW 분기 하나와 S5.7 게이트의 reconciliation arm 하나**다.

### 2. 기각한 대안

- **(i) 기존 Item 행 재사용·재개(resolved → open 되돌리기).** ADR 0065 §12가 “처분은 terminal·영구”로 못박았고 S4.6 backward sweep이 `resolved ⟺ assign receipt`·`dismissed ⟺ dismiss receipt`를 요구한다. status를 되돌리면 그 결박이 깨지고 과거 처분 기록의 의미가 사라진다.
- **(iii) S4.1 v2 component 신설 + 기존 행 마이그레이션.** 장기적으로는 가장 깨끗하다(테이블 하나·키 하나). 그러나 v1→v2 경로가 없어 신 component·행 이관·모든 참조 갱신을 지금 만들어야 하고, 이는 “S4.1 DDL 무변경” 제약과 정면으로 부딪힌다. **PostgreSQL 이관(S6)이 어차피 스키마를 다시 그리는 시점이므로, 그때 두 테이블을 `(request_id, attempt)` 키의 한 테이블로 합치는 것을 이 ADR의 후속 의무로 기록한다.**
- **(iv) 사람 처분 없이 자동 reroute.** 전이표에 `AwaitingAnswer → ReadyToDispatch`가 없다(§2). 자동 재실행은 전이표 자체를 바꾸는 훨씬 큰 결정이며, “사람이 담당을 다시 정한다”는 §4의 판단을 우회한다.

### 3. `attempt == 1` 하드코딩의 교정 — 오탐 방지 fence를 명시로 바꾼다

결정 1을 채택하면 `durable_linked_manager_items`에는 `source_kind ∈ {unowned, deadlock}` Item만 존재하고, 그 둘은 전이표상 `ReadyToDispatch(attempt=1)`로만 재개하므로 **`attempt == 1`은 그 테이블 범위에서 참**이다. 문제는 지금 코드·ADR 문면이 이를 *무조건 참*으로 읽히게 한다는 점이다. 다음 셋을 함께 교정한다.

- **코드:** `sqlite_durable_linked_reconciliation.py`의 assign arm에 `item["source_kind"] IN ('unowned','deadlock')` 전제를 명시 검사로 추가하고, `dispatch` source Item에 결박된 assign receipt는 fail-closed violation으로 닫는다(미탐 0). 기대 attempt는 source에서 도출한다는 사실을 주석으로 남긴다.
- **ADR 0065 §12 Q1①:** “`attempt == 1`”을 “source-derived 기대 attempt(`unowned`/`deadlock` ⇒ 1)”로 정밀화하고, `durable_linked_manager_items`가 구성상 FromDispatch를 담지 않음을 명문화한다.
- **CONTEXT.md:** Linked Command Reconciliation Gate 항목에 같은 fence를 반영한다.

### 4. `manager_queue.py`의 FromDispatch 거부는 유지한다

`_manager_request_fingerprint`의 `ValueError`는 **InMemory request-aware store**의 중복 적재 방지 경로다. durable S5.6은 이 store를 거치지 않고 자기 테이블에 쓰므로 우회가 아니라 **정의역이 다르다**. InMemory 경로에 FromDispatch를 여는 것은 별 결정이며 이 ADR은 열지 않는다.

## 결과와 이행 순서

1. ~~**사용자 확인**~~ — **완료(2026-07-25)**. 두 항목(attempt마다 최대 1회·S5 소유 테이블) 모두 승인됐다. S5.6 착수 차단 해제.
2. 확인 뒤 S5.6 상세 설계(component DDL·처분 UoW 분기·escalation UoW·S5.7 reconciliation arm)를 domain-architect가 이어서 확정한다.
3. §3의 세 교정(코드·ADR 0065 §12·CONTEXT)은 S5.6과 같은 슬라이스에서 함께 랜딩한다.
4. S6 PostgreSQL 이관에서 두 Manager Item 테이블을 `(request_id, attempt)` 키의 한 테이블로 합친다.

## 불변식 자체점검

- **미아 없음 — 강화.** 재실행 뒤 다시 timeout된 Request가 사람에게 다시 닿는 경로가 생긴다. 이 결정 없이는 그 Request가 `AwaitingAnswer`에서 종착을 잃는다.
- **등록 무결성 — 보존.** 재배정 대상은 기존 처분과 같은 Registry admission·중앙 Authority를 거친다.
- **Authority 중앙 — 보존.** 새 처분 action을 만들지 않는다. FromDispatch 처분도 `manager.act`(ADR 0050 §12)이며 카드 자기보고는 없다.
- **전이 ≠ 기록 — 보존.** Item 상태·Request 전이는 도메인, receipt·audit는 기록이며 한 transaction으로 commit한다.
- **노출 불변식 — 보존.** Item에는 typed ref·enum·정수만 담고 원문·rationale은 담지 않는다.

## 갱신 대상

- CONTEXT.md: **Dispatch Timeout Escalation** 용어 등재(승인 시).
- ADR 0065 §12 Q1①: source-derived attempt 정밀화(§3).
- ADR 0042 §9: S5.6 경계 참조(이미 이 ADR을 가리킨다).
- docs/tasks-v0.md S5.6: 사용자 확인 결과와 설계 확정 기록.
