# 분산 전송은 owner 워커의 역방향 아웃바운드 연결 + 중앙 작업 큐로 한다 — owner PC는 서버를 노출하지 않는다

상태: accepted (2026-06-20, 결정 1~3) · 보강 accepted (2026-06-21, 결정 4 — ask_org 비동기화·manager_id 분리, 슬라이스2 진입 전) · 보강 accepted (2026-06-21, 결정 5 — escalation의 audit 기록, 슬라이스2b 진입 전 선결 ①) · ADR 0010의 *전송 계층* 보강(답변 주체=owner Claude Code는 그대로, "어떻게 그 환경에 도달하나"를 못박음)

ADR 0010은 Agent Runtime의 답변 주체를 각 Owner의 Claude Code로 못박았고, 최종(T6.3)은 "각 Owner PC의 Claude Code에 분산 연결(MCP/A2A 등록·호출)"이라 적었다. TRD §5도 "각 Owner Claude Code가 MCP/A2A로 중앙에 등록·연결, 중앙이 client로 호출, 로컬 PC 도달은 후순위"라 적었다. T6.3은 그 *전송 계층*을 채운다 — **중앙이 owner 환경에 실제로 어떻게 도달하는가**. 되돌리기 어려운 결정 두 가지: (1) 연결 방향(누가 누구에게 연결을 거는가), (2) 동기/비동기(중앙 핸들러가 답을 어떻게 기다리는가).

## 결정

### 1. 연결 방향 = owner 워커의 역방향 아웃바운드 연결 (owner PC는 서버를 노출하지 않는다)

owner PC는 **API를 제공(서버 노출)하지 않는다.** 실제 owner 환경(개인 PC)은 NAT/방화벽 뒤에 있고, 고정 IP가 없으며, 상시 가동을 보장하지 않는다. 중앙이 owner PC로 인바운드 연결을 거는 그림(중앙=client, owner=server)은 이 현실에서 깨진다 — 도달 불가·포트포워딩·인증서·상시 켜짐 가정이 모두 무너진다.

그래서 **방향을 뒤집는다.** owner PC에서 도는 작은 **Owner Worker**가 **중앙에 아웃바운드로 연결**한다(큐 폴링 또는 WebSocket/SSE). 아웃바운드라 방화벽·NAT를 그대로 통과한다. 중앙은 서버로서 연결을 *받기만* 하고, owner를 향해 먼저 소켓을 열지 않는다.

- 참고 선례: Claude Code의 Remote Control(owner의 claude가 claude.ai에 아웃바운드 연결을 걸어두고 원격 지시를 받는 패턴)이 정확히 이 모양이다. owner 쪽이 연결을 *건다*.
- TRD §5의 "중앙이 client로 호출"은 *논리적 호출 방향*(중앙이 질문을 보낸다)을 뜻하고, 이 ADR은 *물리적 연결 방향*(소켓을 거는 쪽은 owner)을 분리해 못박는다. 충돌이 아니라 구체화 — 질문은 여전히 중앙→owner로 흐르되, 그 흐름이 owner가 미리 열어둔 아웃바운드 채널을 *타고 내려간다*.

### 2. 동기/비동기 = 중앙은 owner별 작업 큐에 적재하고, 회신은 비동기 (포트는 동기를 유지하되 어댑터가 흡수)

owner 부재·PC 꺼짐이 정상 상태다. 따라서 답은 **즉시 보장되지 않는다.** 중앙은 질문을 **owner별 작업 큐(Work Queue)**에 넣고(`enqueue`), owner Worker가 연결돼 있을 때 가져가(`claim`/`poll`) 로컬 claude(ADR 0010의 `ClaudeCodeRuntime` 재사용)로 답을 만들어 중앙에 **회신**한다(`submit`). owner가 꺼져 있으면 작업은 큐에 **대기**한다.

이 비동기 본질을 표현하려고 **새 포트 `RuntimeDispatcher`**(작업 큐 디스패처)를 도입한다 — `dispatch(question, card) -> WorkTicket`(작업을 owner 큐에 넣고 추적표를 즉시 반환) + `poll(ticket) -> DispatchOutcome`(회신·대기·timeout escalation 중 하나). `AgentRuntime`(동기 `answer`)은 **폐기하지 않는다.** 디스패처 위에 얇은 어댑터(`DispatchingRuntime`)를 얹어 `answer()`를 유지한다 — 어댑터가 내부에서 `dispatch` 후 회신이 올 때까지 *블로킹 대기*하거나 timeout이면 escalation을 표면화한다.

근거(왜 포트를 둘 다 두는가):
- **포트 안정성** — `ask_org`·`router`는 `AgentRuntime.answer(question, card) -> Answer` 동기 계약에 묶여 있다(ADR 0007·0010이 "포트는 안 바뀐다"고 못박음). 이 계약을 깨면 핸들러·테스트·web 어댑터가 연쇄로 흔들린다. 어댑터로 흡수해 **기존 동기 진입점을 보존**한다.
- **비동기를 거짓말하지 않음** — 그러나 분산의 본질은 비동기다. `RuntimeDispatcher`/`WorkTicket`/`DispatchOutcome`을 *1급 도메인 타입*으로 둬, 비동기(대기·timeout·escalation)를 동기 `Answer` 뒤에 숨기지 않고 명시적으로 모델링한다. ask_org를 진짜 비동기(Pending류 회신 후 나중 알림)로 끌어올릴 때 이 타입들이 그대로 쓰인다 — 어댑터는 *그때까지의* 다리일 뿐이다.
- **기존 포트 패턴 정합** — `AuditLog`·`PrecedentStore`·`ConflictCaseStore`와 같은 `Protocol + InMemory` 패턴(ADR 0008). `RuntimeDispatcher` Protocol + `InMemoryWorkQueueDispatcher`(결정론 테스트용 in-process 큐).

### 3. owner 부재·timeout → 기존 Manager escalation 재사용

작업이 큐에서 너무 오래(주입 정책) 회수되지 않거나 회신이 timeout이면, 이는 **미아·합의 실패와 같은 처분 = Escalation**이다(CONTEXT: Escalation은 Contested/Unowned의 종착). 새 escalation 경로를 만들지 않고 **사람 그래프를 타고 Owner의 Manager로 올라가는 기존 경로를 재사용**한다. `DispatchOutcome`의 한 갈래 `EscalatedToManager`로 표면화하고, 실제 Manager 큐 연결은 T5.2(Manager 큐)에 위임한다 — ADR 0008이 `Deadlocked`를 "자리만 남기고 T5.2로" 미룬 것과 동일한 처분 정합.

### 4. ask_org 비동기화 — DispatchOutcome → OrgReply.Pending 매핑 (슬라이스2 진입 전 확정)

슬라이스1은 동기 어댑터 `DispatchingRuntime`의 블로킹 poll로 *기존 동기 ask_org를 보존*했고, 아래 Consequences는 "진짜 비동기 전환은 후속 결정으로 남긴다"고 적었다. **그 후속 결정을 여기서 확정한다**(슬라이스2 진입 전, 리뷰 발견 ①②).

- **답 획득 경로를 디스패처로 일원화.** `ask_org`는 더 이상 동기 `AgentRuntime.answer`를 직접 부르지 않는다. `Routed`면 `RuntimeDispatcher.dispatch(question, card)` → `poll(ticket)`로 `DispatchOutcome`을 얻고, 그 결말을 `OrgReply`로 *투영*한다(도메인 디스패치 ↔ 표현 OrgReply의 경계, ADR 0006의 RoutingDecision→OrgReply 투영과 동형). `AskOrg.__init__`은 `runtime: AgentRuntime` 대신 `dispatcher: RuntimeDispatcher`를 받는다.

- **매핑(DispatchOutcome → OrgReply).**
  - `Delivered(answer)` → `Answered`(`mode` 보존 — Approval 게이트면 `draft_only`가 그대로 사용자에게).
  - `AwaitingWorker(waited)` → `Pending(kind="dispatched")`.
  - `EscalatedToManager(manager_id, reason)` → `Pending(kind="dispatched")`.
  - 신규 `Pending` kind는 **`dispatched` 하나로 최소화**한다. `AwaitingWorker`(미회신)와 `EscalatedToManager`(timeout/owner 부재)는 *도메인에선 다른 처분*이지만 *사용자 관점에선 동일*하다 — "담당에게 보냈는데 아직 답이 없다(사람 손으로 넘어가는 중)". 워커 미연결인지 Manager escalation인지는 **감춰야 할 내부값**이라(노출 불변식), kind를 `dispatched`/`escalated` 둘로 쪼개면 그 내부 구분이 사용자에게 새어나간다. 그래서 두 결말을 한 kind로 모은다.

- **escalation의 Answer 위장 금지 (리뷰 발견 ①).** 슬라이스1의 `DispatchingRuntime`은 `EscalatedToManager`/`AwaitingWorker`를 `mode="full"` Answer(텍스트 `[escalated]`/`[awaiting]`)로 표면화했는데, 이는 *escalation이라는 처분을 동기 Answer 뒤에 숨기는 것*이다("답에는 항상 담당·신뢰 상태가 붙는다" 불변식을 가짜 답으로 우회). `ask_org`의 사용자向 경로에선 이를 **금지**하고 `Pending`으로 표면화한다. `DispatchingRuntime`은 폐기하지 않되(아래) "동기 answer 계약을 요구하는 비-ask_org 호출처"의 호환 어댑터로 *재포지셔닝*하며, 그 폴백 Answer는 어댑터 한정 동작임을 docstring에 명시한다.

- **`EscalatedToManager.manager_id` 1급 분리 (리뷰 발견 ②).** 기존엔 매니저 정보가 `reason` 자연어에만 녹아 있었다. T5.2 Manager 큐가 *기계로 소비*(어느 큐에 적재할지)할 수 있게 `manager_id: str | None`을 1급 필드로 분리한다(owner의 `manages` 상위 User.id, 루트면 `None`). `reason`은 *사람용* 자연어 근거로 유지한다(`ConsensusOutcome.Deadlocked.reason`이 사람용이듯). 노출 불변식: `manager_id`·`reason` 모두 기계/운영자용 내부값이라 사용자向 `Pending`에 싣지 않는다 — `Pending`은 `kind`+`message`만.

- **`LocalRuntimeDispatcher` 도입(즉답 호환 다리).** ask_org가 디스패처만 보게 되면, 분산이 아닌 in-process 데모/단위 테스트(`test_ask_org`·`test_web`)도 디스패처를 거쳐야 한다 — 그러나 이들은 *즉답*이 필요하다(Routed→Answered가 한 호출에 끝나야 그린). 그래서 동기 `AgentRuntime`(StubRuntime·ClaudeCodeRuntime)을 `RuntimeDispatcher`로 감싸 **항상 즉시 `Delivered`를 돌려주는** `LocalRuntimeDispatcher`를 둔다(dispatch가 그 자리에서 `runtime.answer` 호출→회신 완료, poll은 곧장 Delivered). 이는 분산의 *디제너레이트 케이스*(워커=중앙, 큐 길이 0, 미회신·timeout 구조적 불가)이지 별 경로가 아니다. 실제 분산(워커·큐·대기)이 필요한 슬라이스2 네트워크 디스패처가 이 자리를 대신하고, 그때 비로소 `Pending(dispatched)` 분기가 살아난다.

- **`DispatchingRuntime`의 운명 = 유지(재포지셔닝).** Answer 위장을 ask_org에서 걷어내면 이 동기 어댑터는 *프로덕션 경로*에선 불필요해진다(ask_org가 디스패처를 직접 봄). 그러나 (a) ADR 0011 결정 2가 "동기 포트 `AgentRuntime.answer`를 어댑터로 보존"을 명시했고, (b) 슬라이스1이 단조성·망라까지 보강한 그린 자산이며(`test_dispatch`의 `DispatchingRuntime` 테스트군), (c) "동기 answer를 요구하는 비-ask_org 호출처"가 미래에 생길 수 있다. 그래서 **폐기하지 않고 "레거시/호환 어댑터"로 남긴다** — 단 docstring에 "ask_org는 이를 거치지 않으며, 폴백 Answer는 AgentRuntime 계약 유지용 어댑터 한정 동작"임을 못박아 *위장 금지 결정과 충돌하지 않음*을 명시한다. (ask_org의 즉답 다리 역할은 `LocalRuntimeDispatcher`가 맡는다 — 역할이 갈린다: `DispatchingRuntime`=디스패처→동기 answer, `LocalRuntimeDispatcher`=동기 runtime→디스패처.)

### 5. escalation의 audit 기록 — `AuditEntry`가 `DispatchOutcome`을 1급으로 안는다 (슬라이스2b 진입 전 선결 ①)

결정 4의 매핑은 디스패치 escalation(`EscalatedToManager`)을 `Pending(dispatched)`로 투영하며 `manager_id`·`reason`을 사용자向에서 떨군다 — 노출 불변식상 옳다. 그러나 그 escalation은 **audit엔 남아야** 한다(미아 없음의 *기록* 차원). 한 질문 처리는 두 절차다 — (1) 라우팅 → `RoutingDecision`, (2) 디스패치 → `DispatchOutcome`. 기존 `AuditEntry`는 (1)을 `decision`으로 1급 기록하면서 (2)는 `answer`(=`Delivered.answer`)로만 반쪽 기록해, `Routed`인데 escalation된 경우(`decision=Routed`·`answer=None`) escalation 대상이 통째로 소실됐다.

- **`AuditEntry.dispatch_outcome: DispatchOutcome | None` 1급 추가.** 디스패치 절차의 결말을 *원형 그대로* 안는다(`decision`이 라우팅 절차를 원형으로 안는 것과 대칭). Routed일 때만 채워지고, Contested/Unowned는 디스패치를 안 하므로 `None`.

- **`answer` 중복 제거 — 파생 프로퍼티로.** `Delivered.answer`가 이미 `dispatch_outcome`에 들어가므로 `answer`를 별도 생성자 필드로 두면 같은 답이 두 곳에 산다(SSOT 위반). 그래서 `answer`를 생성자 필드에서 빼고 `dispatch_outcome`에서 유도하는 `@property`로 둔다 — 진실은 `dispatch_outcome` 한 곳, `answer` 접근(`entry.answer`·JSONL `answer` 키)은 하위호환으로 그대로 산다. 대안(병존 = 중복, 대체 = 기존 `answer` 어서션·답 없는 처분 키 소실)보다 정합적이다.

- **직렬화 통일성 — escalation 키를 `Unowned.escalated_to`와 같은 결로.** `_dispatch_record`는 `EscalatedToManager`를 `{"disposition": "escalated_to_manager", "escalated_to": manager_id, "reason": …}`로 남긴다 — `Unowned`가 `{"disposition": "unowned", "escalated_to": …}`로 남기는 것과 *같은 모양*(둘 다 "escalation 대상" 개념이라 audit 독자가 한 눈에 같은 처분으로 읽는다). `AwaitingWorker`는 `waited_seconds`, `Delivered`는 처분 라벨만(답 본문은 상위 `answer` 키가 담아 중복 회피). `match`+`assert_never`로 `DispatchOutcome` 망라.

- **의존 방향.** `audit.py`가 `dispatch.py`(`DispatchOutcome` sum)에 의존하게 된다. `dispatch.py`는 `audit`를 import하지 않으므로 순환 없음 — `audit.py`는 이미 `runtime.Answer`를 런타임 import하므로 `DispatchOutcome`도 직접 import한다(`TYPE_CHECKING` 불필요). `decision.py`도 `dispatch`에 의존하지 않아 무관.

- **노출 불변식과 무관.** audit는 내부값을 *다 기록하는 게* 목적이라(`manager_id`·`reason`을 *남긴다*) 노출 불변식의 대상이 아니다. OrgReply/`Pending`엔 여전히 안 샌다(결정 4 그대로). 전이 ≠ 기록도 유지 — 디스패처 큐 상태(queued↔claimed↔…)는 도메인 전이, `AuditEntry.dispatch_outcome`은 그 *결말의 기록*이다(둘은 별개).

- **결정론·테스트 경로.** `LocalRuntimeDispatcher`는 항상 `Delivered`라 지금은 escalation이 구조적으로 안 난다. escalation 경로의 audit 기록은 `InMemoryWorkQueueDispatcher`(timeout 주입 clock) 주입으로 만들어 검증한다(결정론, ADR 0003).

- **이번 범위 밖 — 두 경계(2a→2b 리뷰 Minor, 의도된 결정).** 이 결정은 *escalation*의 audit 기록만 닫는다. (a) **`Delivered`의 ticket 식별자 생략은 의도적이다** — `_dispatch_record`의 `Delivered`는 처분 라벨만 남기고 `ticket_id`를 싣지 않는다. 답 본문은 상위 `answer` 키가, 담당은 `decision.primary`가 이미 담아 정상 경로 추적엔 충분하다(`decision`도 Routed에서 필요한 식별자만 남기듯, "원형"이 곧 전 필드는 아니다). 큐 작업↔entry 연결(`ticket_id`)이 필요해지면 운영 모니터링(T5.1)에서 보강한다. (b) **`AwaitingWorker`의 audit은 poll 시점 *스냅샷*이다** — 현재 `ask_org`는 dispatch→poll 1회라, 미회신(`AwaitingWorker`)으로 찍힌 entry는 그 질문이 나중에 Delivered/Escalated로 종착해도 재기록되지 않는다. 종착 재기록은 폴링/콜백이 생기는 슬라이스2 네트워크 디스패처의 몫이며, 그때 "큐의 작업은 반드시 종착한다"가 기록 차원에서도 닫힌다.

## Considered Options (연결 방향)

- **owner 워커의 역방향 아웃바운드 + 중앙 큐**(선택): 방화벽/NAT 통과, 상시 켜짐 불요, owner 부재가 큐 대기로 자연 흡수. 단 비동기·전달 보장·연결 수명 관리 비용.
- **중앙→owner 인바운드(owner=server)**(기각): owner PC 서버 노출·포트포워딩·고정 IP·상시 가동을 요구 — 개인 PC 현실에서 비현실적. ADR 0010의 "owner 환경에서 답" 비전을 운영적으로 깨뜨린다.
- **owner가 답을 중앙 큐에 push만(연결 유지 없음, 순수 폴링)**: 가장 단순하나 실시간성↓·폴링 비용. → walking skeleton 첫 슬라이스는 이 *논리*(큐 적재+회수)만 in-process로 보이고, 실제 폴링 vs WebSocket 선택은 네트워크 슬라이스로 미룬다(아래).

## Consequences

- **비동기 한계가 1급 시민이 된다.** 답이 즉시 오지 않는 게 정상이다. walking skeleton 첫 슬라이스(1)는 어댑터의 블로킹 대기로 *기존 동기 ask_org를 보존*했지만, 실제 제품에선 ask_org가 `Pending(kind="dispatched")`로 즉시 회신하고 회신은 나중 알림으로 가는 게 옳다(ADR 0006의 OrgReply Pending 정신과 정합). **이 전환을 위 결정 4에서 확정했다**(슬라이스2 진입 전) — ask_org는 `RuntimeDispatcher.dispatch→poll`을 직접 보고 `DispatchOutcome`을 `OrgReply`로 투영하며(`Delivered`→`Answered`, `AwaitingWorker`·`EscalatedToManager`→`Pending(dispatched)`), escalation을 Answer로 위장하지 않는다. in-process 즉답은 `LocalRuntimeDispatcher`가 흡수한다.
- **escalation 재사용.** owner 부재/timeout은 Manager escalation으로 흐른다 — 미아·합의 실패와 동일 처분. 미아 없음 불변식 유지: 큐에 들어간 작업은 회신되거나 escalation되거나 둘 중 하나로 *반드시* 종착하며, 영영 사라지지 않는다.
- **신원·책임 연결점(지금 구현 X).** 워커가 진짜 그 owner인지는 ADR 0009(페르소나 인증)가 강제할 자리다 — 워커는 중앙 연결 시 owner 신원으로 인증해야 하며, 인증되지 않은 워커의 회신은 거부된다. `WorkTicket`/회신에 `owner_id` 귀속을 실어 *연결점만* 명시하고, 실제 인증은 T6.5에 위임한다.
- **Approval 게이트 연결점(지금 구현 X).** owner가 만든 답이 `draft_only`(Approval 게이트, CONTEXT: Answer.mode)면 회신은 사람 승인 전까지 초안이다. 회신 경로에 mode를 보존해 *Approval과 합류할 자리*를 남긴다 — Approval 평가 자체는 T2.5/Routed 영역이고 이 ADR 범위 밖.
- **전이 ≠ 기록 유지.** 작업 enqueue·claim·submit·timeout은 *도메인 전이*(디스패처/큐 상태)이고, audit 기록은 별개로 계속 흐른다(ADR 0008과 동일). 작업 큐는 미해소 작업의 도메인 보관소지 절차 로그가 아니다.
- **escalation의 audit 기록 공백 → 해소(2b 진입 전 선결 ①, 2026-06-21, 아래 결정 5).** 2a 리뷰 [Major]가 발견한 공백 — 결정 4의 매핑이 `EscalatedToManager`를 `Pending(dispatched)`로 투영하며 `manager_id`·`reason`을 사용자向에서 떨구는데, 당시 `AuditEntry`(`decision`+`answer`만)엔 그 escalation 대상을 실을 자리가 없어 `Unowned.escalated_to`와 **비대칭**이었다. **이 공백을 결정 5에서 메웠다** — `AuditEntry`에 `dispatch_outcome: DispatchOutcome | None`을 1급으로 추가해 `EscalatedToManager`의 `manager_id`·`reason`을 audit에 *전부* 남긴다(노출 불변식은 사용자向 `Pending`에만 적용, audit는 내부값 기록이 목적). 이로써 "큐의 작업은 회신되거나 escalation되거나 반드시 종착하며 영영 사라지지 않는다"가 *기록* 차원에서도 지켜진다.
- **결정론 테스트는 in-process로.** 실제 네트워크 전송·워커 프로세스·연결 수명은 비결정이라 단위 테스트에 부적합하다. `InMemoryWorkQueueDispatcher` + Fake Worker(동기 회신)로 *큐 적재→회수→회신→escalation 분기*를 결정론으로 검증하고(ADR 0003), 실제 전송 품질은 데모/수동 시연으로 본다.
- **외부 의존 추가(다음 슬라이스).** 네트워크 슬라이스부터 owner Worker는 별 프로세스·아웃바운드 연결(폴링 또는 WS)·로컬 `claude` CLI에 의존한다. 연결 끊김·재연결·중복 전달은 그 슬라이스에서 다룰 실패 모드다.
- TRD §5 분산 전송·tasks T6.3, CONTEXT(Owner Worker·Work Queue·RuntimeDispatcher·WorkTicket·DispatchOutcome)를 이 방향으로 갱신한다.
