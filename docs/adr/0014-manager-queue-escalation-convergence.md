# Manager 큐 — 세 escalation 출처를 하나의 ManagerItem으로 수렴하고 manager_id로 귀속

상태: proposed (2026-06-21, 설계·shape — domain-architect) · 구현은 후속(tdd-engineer)

ADR 0008은 `ConsensusOutcome.Deadlocked`(합의 교착)를, ADR 0011은 `DispatchOutcome.EscalatedToManager`(owner 부재/timeout)를 각각 "자리만 남기고 **T5.2 Manager 큐로 미룬다**"고 명시했다. `RoutingDecision.Unowned`(미아)도 PRD §7 시나리오 4가 "Manager 큐로"라 적었다. 세 출처가 모두 *"담당/답을 사람(Manager)이 정해야 하는 미해소 escalation"*인데, 지금껏 처분 *상태*만 남기고 사람 손에 닿는 마지막 한 칸이 비어 있었다. T5.2가 그 칸을 채운다 — **Manager 큐로 수렴**.

되돌리기 어려운 결정 네 가지: (1) 세 출처를 하나로 담는가 출처별로 담는가, (2) 무엇으로 귀속(색인)하는가, (3) Manager 처리 행위를 어떻게 표현하고 어디까지 학습으로 흘리는가, (4) Approval(승인 게이트)이 이 큐에 드는가.

## 결정

### 1. 수렴 단위 = 하나의 `ManagerItem`, 출처는 sealed sum `EscalationSource`

세 출처(`Unowned`·`Deadlocked`·`EscalatedToManager`)를 **하나의 `ManagerItem`로 통합**한다. 출처의 차이는 `ManagerItem` 안의 sealed sum `EscalationSource = FromUnowned | FromDeadlock | FromDispatch`로 담아 망라성을 강제한다(`match`로 처리 행위를 가른다 — "타입이 곧 상태").

- **각 출처는 원형 처분을 그대로 안는다.** `FromUnowned(decision, question)`·`FromDeadlock(case, reason)`·`FromDispatch(outcome)` — audit이 `decision`·`dispatch_outcome` 원형을 안는 정신. Manager가 큐에서 "무엇을 두고 escalation됐나"의 전체 맥락(후보 목록·owner/agent_id·root)을 본다.
- **왜 통합인가 (ConflictCase·BackupReviewItem이 *별 store*인 것과 다른 판단).** `ConflictCaseStore`·`BackupReviewStore`는 별 store다 — *Owner* 처리함의 서로 다른 두 탭이고 담는 값(다툼·후보 vs 백업 답)이 근본적으로 다르다(CONTEXT BackupReviewStore _Avoid: "한 store에 두 타입을 섞으면 `open_for_owner`가 무엇을 돌려주는지 모호"). Manager 큐는 반대다 — 세 출처는 모두 *"사람이 담당/답을 정해야 하는 한 escalation"*이라는 **한 종류**고 Manager 화면에서 한 큐로 처리된다(PRD §4 "Manager 큐: 승인·escalation·합의 실패"). 셋을 별 store로 쪼개면 `pending_for_manager`가 셋으로 갈라지고 Manager 화면이 셋을 합쳐 봐야 한다 — owner 처리함이 두 탭으로 갈린 것과 *반대 방향*(거긴 정말 다른 일, 여긴 같은 일의 다른 출처). 그래서 보관 단위는 하나, 출처 차이만 sum으로.
- **`FromDispatch`의 결이 다름(명시).** `Unowned`·`Deadlocked`는 *담당 자체가 안 정해진* escalation이고, `EscalatedToManager`는 *담당은 정해졌으나 답이 안 나온*(가용성) escalation이다. 셋이 한 큐에 들되 처리 행위가 갈리는 근거(아래 결정 3).

### 2. 귀속 = `manager_id` 색인 (owner가 아니라 그 위 사람)

`ManagerQueueStore`는 `ConflictCaseStore`·`BackupReviewStore`와 *같은 포트 패턴*(Protocol + InMemory + 색인 조회)을 **세 번째 인스턴스**로 재사용하되, **색인 키가 `owner`가 아니라 `manager_id`**다 — escalation은 owner가 아니라 그 위 사람에게 귀속한다. `pending_for_manager(manager_id)`가 `open_for_owner`/`pending_for_owner` 동형 = Manager 처리함의 데이터 원천.

- **manager_id 결정 = 사람 그래프 상향(적재자 책임).** owner→manager(ADR 0005 `manages`)를 타고 한 단계 오른다 — 디스패처가 `manager_of` 콜백을 주입받듯(dispatch.py `_manager_of`), 큐 적재자도 그래프 조회(`ManagerOf = Callable[[str], str | None]`, `Registry.get_user(owner).manager`)를 주입받는다(Registry 직접 결합 회피).
  - `FromUnowned` → root(`decision.escalated_to` — 미아엔 owner가 없어 사람 그래프 시작점이 없다, 곧장 꼭대기).
  - `FromDeadlock` → 후보 Owner들의 manager(없으면/엇갈리면 root).
  - `FromDispatch` → `outcome.manager_id`(이미 owner의 manages 상위로 채워짐 — ADR 0011 결정 4; None이면 root 보정).
- **미아 없음 — manager_id는 끝내 None이 되지 않는다.** 적재자가 root로 보정해 escalation은 반드시 *누군가의* 큐에 닿는다(루트 User가 마지막 수신자). 큐 적재 = escalation의 최종 종착.
- **단일 Manager 수렴(과도 확장 금지).** 멀티홉(manager의 manager까지 등반)·LCA(여러 후보 owner의 공통 상위)는 **PRD §6 후순위로 명시**(범위 밖). 여기선 *한 단계 위 + root 폴백*만. `FromDeadlock`에서 후보들의 manager가 엇갈리면 LCA를 계산하지 않고 첫 후보 owner의 manager 또는 root로 떨어뜨린다.

### 3. Manager 처리 행위 = sealed sum `ManagerAction`, AssignOwner만 Precedent로 흘림

Manager가 한 항목에 내리는 처분은 sealed sum `ManagerAction = AssignOwner | Reroute | Dismiss`(1인칭 `by_manager` — 처리 서비스가 `item.manager_id == by_manager` 강제, ConsensusService·BackupReviewService 정신).

- **`AssignOwner(primary)` — 담당 지정(미아·교착의 사람 종결).** intent가 있으면(Unowned·Deadlock 모두 분류 라벨 보유) `Resolution(intent→primary)`으로 떨어져 **Precedent로 학습**된다 — CONTEXT Conflict "두 경로(Overlap/Gap) 모두 결론은 Resolution → Precedent". 이게 Gap/Overlap의 사람 해소가 라우터 학습으로 닫히는 지점(`ConsensusOutcome.Agreed`가 표로 닫는 것과 대칭). `FromDeadlock`을 AssignOwner로 닫으면 *그 ConflictCase도* resolved시킨다(`case_store` 주입 — 합의 실패의 사람 종결이 케이스를 닫는다, Agreed가 닫듯).
- **`Reroute(to_agent)` — 재지정(owner 부재의 운영 판단).** 담당은 있었으나 답이 안 나온 case라 *Transfer*(CONTEXT — 배정된 primary를 사후에 바꿈)에 해당. **Precedent를 만들지 않는다**(일회 가용성 사건이지 담당 규칙 변경이 아니다 — BackupReview가 Precedent를 안 만드는 정신).
- **`Dismiss` — 종결("확인했고 추가 조치 안 함").** 처리 완료 사실은 남긴다(미해소와 구분 — BackupReview.Dismiss 정신). Precedent X.
- **처리 결론 = `ManagerResolution`(conflict.Resolution과 구분).** `ManagerResolution(action, resolution?)` — 큐 항목의 결말(어떤 행위로 종결)이고, AssignOwner면 그 안에 conflict의 `Resolution`(Precedent로 흐른 결론)을 담는다. Reroute/Dismiss는 `resolution=None`. `ManagerItem.resolve(manager_resolution)`이 불변+새 인스턴스로 전이(ConflictCase.resolve·BackupReviewItem.review_with와 같은 정신).
- **T5.2 범위 = 자리 + 기본 처리.** 세 행위 전이·1인칭 강제·Precedent 흘림·case 종결까지. **후속(범위 밖):** 출처-행위 적합성 강제(미아에 Reroute 거부 등), 멀티홉, 처리 시 사용자 자동 통지(MVP는 retrieve 조회 갱신 — ADR 0011 결정 6-5 정합), 신규 카드 생성 흐름(AssignOwner는 *기존 카드* 지정만; "담당 공백 backlog"는 PRD §7 시나리오 4 후속).

### 4. Approval(승인 게이트)은 이 큐에 넣지 않는다 (게이트 vs 종착 — 별개)

PRD §4는 Manager 큐를 "승인 요청·escalation·합의 실패"로 적어 셋을 한 화면에 묶었다. 그러나 **도메인 처분으로는 Approval과 escalation이 다른 결**이라(CONTEXT Approval _Avoid: "Escalation(이건 게이트가 아니다)"), T5.2 `ManagerItem`의 `EscalationSource`에 **Approval을 출처로 넣지 않는다**.

- **근거 — 게이트 vs 종착.** Escalation(Unowned/Deadlock/Dispatch)은 *담당/답을 사람이 정하는* **종착 처분**이다. Approval은 *담당은 정해졌고(Routed) 실행 전 사람 사인만 필요한* **게이트**다(CONTEXT). 둘을 한 sum에 섞으면 "담당 미정"과 "담당 정해짐+사인 대기"가 같은 타입이 돼 처리 행위(AssignOwner/Reroute vs 승인/반려)가 뒤섞인다.
- **승인자 ≠ Manager일 수 있음.** Approval은 *지정 승인자*(owner 또는 카드가 지목한 사인 권한자)의 게이트지 사람 위계 상위(manager) 전용이 아니다. manager_id 색인(결정 2)에 Approval을 끼우면 귀속 의미가 흐려진다.
- **현재 상태 — Approval 게이트는 이미 *표시*까지 구현됨.** `Routed.requires_approval` → `AskOrg._apply_approval_gate`가 `mode="draft_only"`로 답을 내려 "초안·승인 대기"를 표시한다(T2.5 완료, ADR 0012 mode 강제 패턴). 실 승인 행위(draft→full로 푸는 사람 사인)는 별 자리가 필요하다 — 그건 **별도 승인 큐/행위로 후속 분리**하고(PRD §4의 "승인 요청"은 그 자리), T5.2 escalation 수렴 범위에서는 **제외(자리만 명시)**. 같은 화면(Manager 큐 UI)에 *탭으로* 나란히 놓는 것은 표현 층 선택이지 도메인 통합이 아니다 — owner 처리함이 합의·검토 두 탭을 가지듯.
- **PRD 정합.** PRD §4 "Manager 큐: 승인·escalation·합의 실패"와 충돌하지 않는다 — escalation·합의 실패는 T5.2가 닫고, 승인은 *같은 면의 다른 탭/행위*로 후속. PRD 표현은 화면(surface) 단위 기술이고, 이 ADR은 그 안의 도메인 분리를 명문화한다.

## Consequences

- **미아 없음 불변식 완성.** 세 출처가 지금껏 처분 상태만 남기고 사람 손에 닿지 않던 마지막 칸을 큐 적재로 채운다 — escalation은 반드시 `pending_for_manager`에 쌓여 Manager를 기다리고(영구 소실 0), root로 보정돼 *반드시 누군가의* 큐에 닿는다. "큐의 작업은 회신/escalation으로 종착"(ADR 0011)에 이어 "escalation은 Manager 큐 적재로 종착"이 닫힌다.
- **전이 ≠ 기록 유지.** `ManagerQueueStore`(미해소 escalation 도메인 보관, 전이) ↔ `AuditLog`(절차 기록). escalation은 *이미* audit에 남는다(`AuditEntry.decision`=Unowned / `dispatch_outcome`=EscalatedToManager, ADR 0011 결정 5) — 큐 적재는 그 기록과 별개의 전이다. Manager 처리도 큐 전이만, 기록은 audit이 별개로(append 자리는 적재/처리 흐름 슬라이스에서 판단 — 검토 루프가 audit에 *안* 남긴 것과 달리, escalation 처리는 운영 추적 대상이라 audit append를 검토).
- **Authority 중앙 유지.** AssignOwner → Resolution → Precedent는 사람(Manager)의 1인칭 처분이 중앙 누적 규칙(판례)이 되는 것 — 카드 자기보고가 아니다. Reroute/Dismiss는 판례를 만들지 않아 라우팅 규칙을 흔들지 않는다.
- **노출 불변식 — Manager 큐는 운영 면(노출 OK).** `ManagerItem`은 내부값(후보·manager_id·reason·owner)을 그대로 노출한다(ConflictCase가 처리함에 후보를 노출하듯 — 채팅 OrgReply 불변식과 다른 면). 사용자 채팅엔 여전히 `Pending` 안내만 간다(escalation의 `dispatched`/`unowned` 등 — 이미 구현). escalation→Manager 큐 적재는 *운영 면에서만* 보인다.
- **패턴 재사용(새 메커니즘 0).** `ManagerQueueStore`는 `ConflictCaseStore`·`BackupReviewStore`의 검증된 Protocol+InMemory+색인 패턴의 세 번째 인스턴스 — 색인 키만 owner→manager_id. `ManagerQueueService`는 ConsensusService·BackupReviewService의 1인칭 강제·전이 정신. 결정론 테스트(주입 clock·고정 item_id 시드).
- **결정론 vs 수동 경계.** 적재 흐름(escalation→큐)·처리 전이(ManagerAction→resolved·Precedent·case 종결)는 전부 *결정론*(FakeClassifier·주입 clock·in-memory store). 실 Manager 화면 조작·실 전송은 수동/web 시연. 미아 없음(세 출처 모두 큐 적재로 종착)은 결정론으로 닫는다.

## Considered Options

### 수렴 단위
- **하나의 `ManagerItem` + `EscalationSource` sum**(선택): Manager 화면 한 큐·한 조회. 출처 차이는 sum으로 망라. 세 출처가 같은 종류(사람이 담당/답 정함)라 정합.
- **출처별 3 store**(기각): ConflictCase/BackupReview 패턴을 셋으로 복제 — 그러나 `pending_for_manager`가 셋으로 갈라지고 Manager 화면이 합쳐 봐야 함. 같은 일의 다른 출처를 다른 일처럼 쪼개는 과분리.
- **ConflictCaseStore에 Deadlock·Unowned도 섞기**(기각): CONTEXT _Avoid가 명시한 "한 store 두 타입" 모호함. ConflictCase는 *Owner* 처리함 귀속(owner 색인)인데 Manager 큐는 manager_id 색인이라 색인 키부터 다르다.

### Approval 경계
- **별개(게이트 ≠ 종착, 후속 분리)**(선택): 도메인 처분이 근본적으로 달라(담당 정해짐+사인 vs 담당 미정) 섞으면 처리 행위가 뒤엉킨다. 승인자도 manager 전용이 아니다. 화면 탭으로 나란히 두되 도메인은 분리.
- **같은 `ManagerItem` sum에 `FromApproval` 추가**(기각): PRD §4 표현을 도메인까지 끌어와 게이트와 종착을 한 타입으로 — CONTEXT Approval/Escalation 구분을 무너뜨린다.

## 갱신 대상

- CONTEXT: **Manager 큐**(_Avoid: Inbox)·**ManagerItem**·**EscalationSource**·**ManagerAction**·**ManagerQueueStore**·**ManagerResolution** 용어 등재. Escalation 항목에 "Manager 큐 적재로 종착" 보강. Approval 항목에 "Manager 큐와 별 탭/행위(후속)" 보강.
- TRD §9 디렉터리에 `manager_queue.py` 추가, §3 모듈 목록 갱신. §6 라우팅 후단에 escalation→Manager 큐 수렴 한 줄.
- tasks T5.2 설계·shape 완료 체크 + 슬라이스 분해 기록.
- PRD §6에 "단일 Manager 수렴 — 멀티홉·LCA는 후순위" 명시(이미 §6에 LCA·멀티홉 후순위 있음 — 확인·정합).
