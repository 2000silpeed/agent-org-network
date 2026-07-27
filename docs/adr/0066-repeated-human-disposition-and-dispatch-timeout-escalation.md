# ADR 0066 — 한 Question Request의 반복 사람 처분과 dispatch timeout escalation

- 상태: **채택(Accepted, 2026-07-25 — 사용자 확인 완료)**. 확인 내용: "한 Request가 사람 처분 큐에 **실행 시도(attempt)마다 최대 한 번** 들어갈 수 있다"(결정 §1의 제안 답)와 그 저장 경계(FromDispatch Item은 S5 소유 `durable_dispatch_escalation_v1` component·`UNIQUE(request_id, attempt)`·S4.1 DDL 무변경)를 사용자가 승인했다. 기각 대안(평생 1회 유지 = 재실행 실패 시 사람에게 다시 묻지 않음)은 그 Request가 `AwaitingAnswer`에서 종착을 잃어 미아 없음 불변식을 깨므로 채택하지 않았다.
- 날짜: 2026-07-25
- 계보: ADR 0042(§2 전이표·§4 사람 처분·§9 S5 경계)·ADR 0014(Manager 큐)·ADR 0050 §12(`manager.act`)·ADR 0065 §11·§12(FromDeadlock 소비·linked command 정합)를 잇는다. ADR 0042 §9 S5.1~S5.5는 이 결정에 의존하지 않는다 — S5.6만 이 결정 위에 선다.
- 구현 상태: 미구현(결정만 확정). 사용자 확인은 2026-07-25에 완료돼 **S5.6 착수 차단은 해제**됐다. S5.6 상세 설계(component DDL·처분 UoW 분기·escalation UoW·S5.7 reconciliation arm·§3의 세 교정)는 S5.5 랜딩 뒤 domain-architect가 이어서 확정한다.

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

## 5. 상세 설계 (domain-architect·2026-07-26)

결정 §1~§4와 ADR 0042 §9 ⑯(교차 write)·⑰(시간 안전)이 제약을 이미 깔았으므로, 아래는 그 위에서 남은 선택만 확정한다.

### 5.1 component — 2테이블, audit/outbox intent mirror 없음

`durable_dispatch_escalation_v1`이 `durable_dispatch_manager_items`(FromDispatch Item)와 `durable_dispatch_escalation_receipts`(3 action 공용 command receipt)만 소유한다. **S4.1이 receipt마다 붙이는 audit/outbox intent mirror를 복제하지 않는다** — 그 두 테이블은 receipt와 필드가 exact 일치해야 하는 순수 중복이고 소비자가 오늘 0이며(§9 ③·⑥), S5.4가 같은 이유로 이미 mirror를 두지 않았다. receipt 자체가 그 명령의 durable 기록이고, 운영 감사 로그로 내보내는 것은 소비자 관심사로 이월한다. 이는 S4.1 shape에서 의도적으로 벗어난 지점이므로 리뷰가 반박할 수 있게 명시해 둔다.

**status enum은 S4.1과 같은 `{open, resolved, dismissed}`를 쓴다.** `rerouted`가 이 표에서 더 정직하지만, §2의 S6 테이블 통합을 기계적으로 만들기 위해 값 집합을 일치시킨다. 대신 **이 표에서는 `resolved ⟺ manager.reroute`**이고 S4.1에서는 `resolved ⟺ manager.assign_owner`라는 차이가 생기며, 그 차이를 눈에 보이게 만드는 것이 §3의 source 전제 명시다.

**시간 표현 경계 — S4.1 timestamp와 문자열 비교가 일어나는 지점은 0이다.** escalation 행의 `created_at`·`observed_due_at`은 S5 canonical instant(고정폭 UTC·microseconds)로 저장한다. Item은 S4.1 ticket에 FK로 붙지만 **두 표의 timestamp를 비교하는 코드는 없다** — 경계를 넘는 값은 정수(`attempt`·`awaiting_revision`)와 typed ref뿐이다. SLA 판정(`due_at <= now`)은 문자열이 아니라 **Python `datetime` 객체 사이에서** 수행하고, 그 결과를 저장할 때만 canonical 문자열로 정규화한다. 이 “정수·typed ref만 경계를 넘는다”가 red로 고정할 불변식이다.

### 5.2 처분 UoW — S4.4 확장이 아니라 S5 소유 신 UoW

§1의 “처분 UoW 분기”는 **S4.4 `sqlite_durable_manager_disposition_uow`에 `dispatch` 분기를 넣는 것이 아니다**. 두 이유로 불가능하다.

1. **S4.6이 깨진다.** S4.4는 처분을 S4.1 `durable_linked_command_receipts`에 `target_ref = manager_item_id`로 쓴다. FromDispatch Item은 S5 표에 있으므로 그 receipt의 `target_ref`는 S4.1 Item 표에서 join되지 않고, S4.6 forward sweep이 `manager_disposition_receipt_mismatch`로 잡는다. §1이 약속한 “S4.1 DDL 무변경·S4.6 무변경”과 정면으로 충돌한다.
2. **계층이 역전된다.** S4.4가 S5 component를 알게 되면 S4가 S5에 의존한다(S5는 S4.1 위에 서 있다).

따라서 FromDispatch 처분은 **S5 소유 UoW가 S5 receipt 표에 기록**하고, 분기는 UoW 안이 아니라 **어느 표에 그 Item이 있는지로 갈리는 application 진입점**에 둔다. S4.4의 `source_kind == "dispatch" → Unavailable` 거부는 그대로 유지한다(정의역이 다르다는 사실의 표현이다).

중앙 권한은 **새 action을 만들지 않는다** — FromDispatch 처분도 `manager.act`(ADR 0050 §12·role hard-limit·`manager_item` resource·1인칭 귀속)이며, `ManagerActItemResolver`에 S5 표를 읽는 구현을 주면 된다(`resource_id`는 S5 `manager_item_id`). ADR 0050 계약 변경은 없다.

### 5.3 escalation UoW — system 전이, SLA는 transaction 안에서 다시 판정

timeout escalation은 사람 명령이 아니라 **system 전이**다(S4.5 `work_ticket.create`와 같은 결) — 중앙 재인가 0, `principal_ref`는 system subject 상수, `CentralAuthorizer` 미주입. 권한 근거는 “SLA가 지났다”는 durable 사실 자체다.

⑰ (1)(2)(3)을 그대로 적용한다. 특히 **⑰(2)의 transaction 내 SLA 재판정에는 세 번째 독립 근거가 있다** — ① 시간 안전(호출자 `now` 비전이성) ② TOCTOU(스캔~write 사이 답 도착) ③ **S5.4 거부의 정당성**: escalation이 ticket을 `pending`에서 떠나게 하면 그 뒤 도착한 워커의 답은 S5.4의 stale 판정으로 거부된다. 그 거부가 정당한 유일한 근거는 “escalation 시점에 SLA가 실제로 지나 있었다”이고, 그것을 만드는 것이 재판정이다. 근거가 셋이므로 하나가 반박돼도 요구는 남는다.

**늦은 답의 처분은 의도된 동작이다.** SLA가 진짜 지나 사람이 개입한 뒤라면 늦은 답을 받아들이는 것이 오히려 위험하다(Manager의 처분과 경쟁해 두 종착 경로가 열린다). 워커 측 계약은 “재시도가 아니라 폐기”이며, 그 Request는 Manager의 reroute로 **새 attempt의 새 ticket**을 받는다(§4 ADR 0042).

**새 SLA가 필요하다 — 이월할 수 없다.** c.3 escalation UoW는 `AwaitingConflict`의 `due_at`을 그대로 이월했지만, S5.6은 **그 `due_at`이 지났기 때문에** 전이하므로 이월하면 `QuestionRequest`의 “비종결 상태의 `due_at`은 전이 시각보다 빠를 수 없다”를 즉시 위반한다. 따라서 `escalation_sla: timedelta`를 **생성자 1지점**에 주입해 `due_at = now + escalation_sla`로 만든다(⑤의 `lease_ttl`과 같은 판·`0 < sla <= 30일` 검증). 조직별 SLA가 필요해지면 그 인자 하나를 port로 교체하는 것이 swap point다.

**escalation 대상 Manager는 ticket Owner의 nearest manager, 없으면 유일 root User다.** ADR 0065 §10 c.0가 conflict escalation에 세운 선택 규칙을 그대로 쓰되, 앵커는 후보 집합이 아니라 **답하지 않은 Owner**다(답을 못 낸 주체의 상위가 처분해야 한다). c.0 snapshot reader는 conflict ingress claim에 강하게 결합돼 재사용할 수 없으므로 좁은 신 port(`DispatchEscalationTargetDirectory.resolve_manager(org_id, owner_subject_ref) -> subject_ref | None`)를 두고, `None`은 fail-closed(Request를 `AwaitingAnswer`에 남겨 다음 run이 재시도 — 미아 없음)다. 이 선택은 “누가 호출을 받는가”라는 사용자 가시 결정이지만 기존 선례를 그대로 적용한 것이므로 별 사용자 확인 없이 진행하고, 다른 큐로 보내고 싶다면 이 port 구현만 바꾸면 되도록(스키마 변경 0) 격리한다.

**digest 계산 시점 — 의사코드에서 벗어난 지점(2026-07-27·구현 판정).** 위 의사코드대로 `request`를 다 읽은 뒤 digest를 계산하면 **replay 분기가 도달 불가능한 죽은 코드가 된다**: escalate가 성공 commit되면 같은 transaction에서 ticket이 `escalated`로, Request가 `AwaitingManager`로 함께 바뀌므로, 다음 호출은 맨 앞 `status != 'pending' → Conflict`에서 걸려 receipt 조회에 닿지 못한다. S5.4는 이 문제가 없는데 그건 **caller가 넘긴 command 값만으로**(DB read 0) transaction 밖에서 digest를 먼저 계산하기 때문이다. escalate의 caller 입력은 `ticket_id` 하나뿐이라 그 방식을 쓸 수 없다. 구현은 `expected_request_revision`을 **`ticket.awaiting_revision + 1`**(ticket 행에 escalate 이전부터 고정된 불변값)로 유도해 ticket을 한 번 읽은 직후 digest를 계산하고 replay 조회부터 한 뒤, 그 다음에 신규 write 전제조건(`status='pending'`·SLA·manager)을 검사한다. **값 자체는 의사코드와 동일하고 읽는 시점만 다르다.** replay를 아예 없애고 재호출을 Conflict로 떨어뜨리는 선택지도 검토했으나(유일 caller가 스캔 구동 runner이고 스캔이 `status='pending'`만 내므로 실사용 재호출 경로가 없다), 동작하는 코드를 정확성 이득 없이 바꾸는 것이라 채택하지 않았다 — 랜딩 차단 사유(결과 손실·fail-open·불변식 위반·권한 우회) 어디에도 해당하지 않는다. 독립 리뷰가 재판정할 항목으로 남긴다.

**Item의 `awaiting_revision`은 부모 행 값을 그대로 복사한다(+1 아님·2026-07-27 결함 교정).** c.3 `sqlite_durable_conflict_escalation_uow.py`가 선례를 고정한다 — `manager_item.awaiting_revision == case.awaiting_revision`이고 전이 전 Request revision은 `command.expected_request_revision == case.awaiting_revision + 1`이 진다. 즉 **Item은 부모의 대기 revision을, receipt는 전이 전 Request revision을** 담는 쌍이다. 구현이 Item에 `+1` 값을 넣고 있어 교정했다 — §5.4의 item⟺ticket 교차검증이 c.3와 같은 모양이므로 그대로 두면 S5.7이 정상 행을 위반으로 잡는다. (S4.6 `:321`의 `ticket.awaiting_revision == receipt.expected_request_revision`은 **WorkTicket 표의 규칙**이라 이 쌍과 무관하다 — 혼동하지 말 것.)

**⑯ 전수 대조 결과.** S5.6이 쓰는 남의 행은 둘이다.
- **`durable_linked_work_tickets.status`(`pending → escalated`)** — S4.1 `_validate_rows`의 ticket 규칙은 `attempt`·`awaiting_revision` 정수, `route_sha256` SHA, `owner_subject_id` typed ref, `status` enum, `created_at` timestamp, 그리고 (ticket_id·org_id·request_id) typed ref + org/request lineage다. 이 write가 건드릴 수 있는 것은 **`status` enum 하나뿐**이고 값은 리터럴 `'escalated'`(허용 집합 소속)이므로 문법 위반이 불가능하다. 단조 전방성(§9 ②)은 S4.1이 검증하지 않으므로 **CAS `WHERE status='pending'`이 유일한 집행 지점**이다. 다른 컬럼은 SET에 넣지 않는다.
- **`durable_dispatch_leases`(release)** — S5.4가 밟은 함정과 **같은 지점**이다. S5.1의 행 불변식 `expires_at >= acquired_at`을 CAS가 지켜야 하므로 `WHERE ticket_id=? AND state='leased' AND acquired_at<=?`를 S5.4와 **동형으로** 쓴다. rowcount 0(만료·부재·clock skew)은 정상이며, 그때 `leased` lease가 비-`pending` ticket에 남는 것은 **의도적으로 관용되는 상태**다(아래 5.4).

### 5.4 S5.7 arm — 그리고 절대 단언하면 안 되는 것

S5.7의 증분은 S4.6(S4.1 receipt ⟺ S4.1 aggregate ⟺ Request)·S5.5(read-only 스캔)·S5.1(행 문법+parent lineage)이 보지 않는 **S5 소유 cross-aggregate 정합**이다.

- **answer receipt ⟺ ticket `completed`**(양방향·terminal 영구).
- **escalation Item ⟺ ticket `escalated`**(양방향·1:1) + Item ⟺ receipt(`work_ticket.escalate` 정확히 하나) + 처분된 Item(`resolved`/`dismissed`) ⟺ 대응 처분 receipt.
- **Request 결박은 resting-revision 판별자**(S4.6 판 계승): `request.revision == receipt.expected_request_revision + 1`이면 정확한 shape(`AwaitingManager(public_kind="dispatched")`·`item_id`·route/attempt 보존·handling), 초과면 revision floor + org만.
- **`leased` lease ⇒ ticket `pending`을 단언하지 않는다.** S5.4의 release CAS가 `acquired_at<=?` 가드로 rowcount 0을 허용하므로 **`completed`/`escalated` ticket에 `leased` lease가 남는 것은 문서화된 안전 상태**다(그 lease는 `status='pending'`만 스캔하는 S5.3·S5.5에 무해하다). 이걸 위반으로 잡으면 의도적 관용 상태를 오탐한다. 단언 가능한 것은 `attempt.lease_epoch <= lease.lease_epoch`(S5.1 소관)와 “delivery attempt가 있으면 그 ticket에 lease 행이 있(었)다”뿐이다.
- **P2-4 표면화 위치는 S5.7 게이트가 아니라 S5.6 runner 리포트다.** S5.7은 S4.6 판 그대로 `capable ⟺ violation 0`이고, “관측이 손상으로 멈췄다”를 정상 유휴와 구분해야 하는 주체는 escalation runner다 → `DispatchEscalationRunReport(scanned, escalated, skipped, contended, scan_capable: bool, scan_detail: str)`.

### 5.5 §3 삼자 교정의 확정 형태

`durable_linked_manager_items`는 구성상 `source_kind ∈ {unowned, deadlock}`만 담는다(FromDispatch는 5.1의 S5 표). 따라서 S4.6 assign arm의 `attempt == 1`은 참이지만 **전제가 암묵적**이다. 교정은 (1) 코드 — assign arm에 `item["source_kind"] IN ('unowned','deadlock')`를 명시 검사로 추가하고 `dispatch` source에 결박된 assign receipt는 fail-closed violation, (2) ADR 0065 §12 Q1① 문면(완료), (3) CONTEXT(완료)다. S4.1 `_COMMAND_ACTION`의 미사용 `manager.reroute`는 **그대로 둔다** — S5는 자기 receipt 표의 자기 action enum을 쓰므로 S4.1 표에 그 값을 쓰는 writer는 계속 0이고, S4.6이 그것을 `unbindable_command_receipt`로 fail-closed하는 현행이 옳다.

## 결과와 이행 순서

1. ~~**사용자 확인**~~ — **완료(2026-07-25)**. 두 항목(attempt마다 최대 1회·S5 소유 테이블) 모두 승인됐다. S5.6 착수 차단 해제.
2. ~~확인 뒤 S5.6 상세 설계~~ — **완료(2026-07-26·§5)**. component 2테이블·S5 소유 처분 UoW·system 전이 escalation UoW·S5.7 arm·§3 교정 형태를 확정했다.
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
