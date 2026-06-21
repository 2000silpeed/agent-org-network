# Routing

조직 구성원의 업무 에이전트 카드를 등록받아, 들어온 질문을 책임 있는 담당자에게 연결(라우팅)하는 컨텍스트. 중앙은 지식을 소유하지 않고 "누가 무엇을 담당하는가"와 그 연결·기록만 관리한다.

## Language

### Graph model

두 종류의 노드와 그 사이의 엣지로 본다. 역할(Owner/Manager/Maintainer)은 노드가 아니라 엣지에서 파생된다.

- **노드**: User(사람), Agent Card(업무 에이전트)
- **엣지**: `owns`(User→Agent, 그 User가 Owner) · `manages`(User→User, 윗 User가 Manager) · `maintains`(User→Agent, 그 User가 Maintainer)
- **카디널리티**: User의 manager는 0..1(루트는 0) · 한 User는 여러 Agent를 owns · 한 Agent의 Owner는 정확히 1명

### Core

**Agent Card**:
한 책임(역할/직무)의 담당 영역·답변 가능 범위·이관 규칙을 선언한 YAML 문서. 정체성(`agent_id`)은 역할에 묶여 담당자가 바뀌어도 불변. 레지스트리에 등록되는 단위이자 라우터가 평가하는 후보 단위 — 등록과 라우팅 양쪽에서 동일한 객체다.
_Avoid_: Agent(단독), Persona, Profile

**Owner**:
한 Agent를 `owns` 하는 User — 그 카드의 답변에 책임진다. 카드의 *정체성*이 아니라 *교체 가능한 관계(엣지)* — 담당자가 바뀌면 owns만 갈아끼운다.
_Avoid_: Author

**Maintainer**:
한 Agent를 `maintains` 하는 User — 카드 편집 권한자. MVP에선 Owner와 동일(maintains 엣지 없으면 owns가 대신). 편집 권한·이력은 git/PR이 강제·기록한다.
_Avoid_: Editor, Admin

**User**:
사람 노드. Agent를 owns 하거나 다른 User를 manages 할 수 있다. `id`와 상위 `manager`(0..1)를 가진다.
_Avoid_: Account, Member

**Agent Runtime**:
Agent Card를 구동해 질문에 답하는 실행 주체. 라우터가 호출한다. 답변 주체는 *owner가 위임·관리하는 환경*의 Claude Code다 — 평상시 owner PC, owner PC 부재 시 owner 위임 백업 인스턴스(ADR 0010+0012). 어느 쪽이든 owner가 답하며 중앙 API 키 LLM이 아니다(중앙 무지식 보존). 포트로 분리 — `StubRuntime`(canned, 테스트·스켈레톤) → `ClaudeCodeRuntime`(`claude -p` 헤드리스, T6.1 임시·중앙 1회성·모든 카드가 로컬 claude로 답) → owner별 분산 Claude Code(MCP/A2A, T6.3 — 그때 owner별 지식 격리 성립). `knowledge_sources`는 현재 `Answer.sources` 출처 레이블이며 진짜 문서 RAG는 후속. (ADR 0007·0010)
_Avoid_: Agent(단독), Bot

**Answer**:
Agent Runtime이 한 질문에 산출한 답. `text`(답 본문) · `sources`(근거 출처) · `mode`를 가진다. `mode`는 신뢰 상태 — `full`(owner 실시간 답, 그대로 사용자에게) · `draft_only`(Approval 게이트가 걸려 사람 승인 전까지는 초안) · `backup`(owner 위임 백업 워커의 스냅샷 기반 답, owner 미검토 — 신뢰 하향, ADR 0012). 즉 Routed에 Approval이 붙으면 Runtime은 `draft_only`로 답하고(승인 ≠ 라우팅, 게이트만 걸림), owner PC 부재로 백업이 답하면 `backup`으로 신뢰가 하향된다(둘이 겹치면 `backup`이 우선 — 백업은 어차피 owner 미검토라 더 강한 하향). **`backup`은 셋의 마지막 값이고 넷째 값은 없다** — staleness로 fresh/stale을 쪼개지 않는다(stale 백업은 답을 안 하고 거부→escalation이라 *답이 나가는* 백업은 다 fresh, ADR 0012 결정 9). owner가 복귀해 백업 답을 검토(`BackupReview`)하면 backup→`full`로 *신뢰 복원*(승인·정정 모두 — owner 검토를 거쳤으므로, 재노출 시 적용, ADR 0012 결정 7). "답에는 항상 담당·신뢰 상태(승인/초안/백업/출처)가 붙는다"는 불변식의 그 답. `mode`는 *원래 노출하는 신뢰 상태값*이라 사용자向 `Answered`에 그대로 실린다(조직 내부 구조가 아님 — 노출 불변식과 양립).
_Avoid_: Response, Result, Reply

### Distributed transport (분산 전송)

owner PC는 서버를 노출하지 않는다(NAT/방화벽 뒤·고정 IP 없음·상시 가동 X). 그래서 owner PC의 워커가 중앙에 *역방향 아웃바운드*로 연결해 작업을 가져가고, 중앙은 owner별 작업 큐에 적재해 비동기로 답을 수집한다. 논리적 호출 방향(질문 중앙→owner)과 물리적 연결 방향(소켓은 owner→중앙)은 분리된다. (ADR 0011 — Agent Runtime의 도달 방식, 답변 주체=owner Claude Code는 ADR 0010 그대로.)

**Owner Worker (오너 워커)**:
각 Owner PC에서 도는 작은 실행 주체. 중앙에 *아웃바운드 WebSocket*으로 연결(ADR 0011 결정 6 — 폴링이 아니라 WS로 확정)해 자기 owner 작업 큐의 작업을 받아(중앙이 그 소켓으로 `PushWork`), 로컬 Claude Code(`ClaudeCodeRuntime` 재사용)로 답을 만들어 중앙에 회신(`SubmitAnswer`)한다. 중앙이 owner PC로 인바운드 연결을 *걸지 않는다* — 연결을 거는 쪽은 항상 워커다. owner PC는 간헐 연결이 정상이라 끊김·재연결·중복 전달이 핵심 실패 모드다(ADR 0011 결정 6-4). 진짜 그 owner인지는 인증으로 강제한다(ADR 0009 연결점, 실 검증 T6.5). 선례: Claude Code Remote Control(owner claude가 claude.ai에 아웃바운드 연결). 구현(`worker.py`, 슬라이스2b-ii): 프레임 핸들링 결정론 코어(`WorkerLogic`=`PushWork`→`ClaudeCodeRuntime`→`SubmitAnswer`·`backoff_seconds`·`parse_central_frame`, 단위 테스트)와 실 아웃바운드 WS·재연결 셸(`run_worker`, 수동 시연)을 분리. 자기 owner 카드(`agent_id→AgentCard`)는 owner 환경에 들고(`PushWork`는 식별자만 싣고 워커가 카드 복원), 한 owner = 한 워커 프로세스.
_Avoid_: Agent(단독), Client(단독 — 논리 호출 방향과 혼동), Node

**Backup Worker (백업 워커)**:
owner PC 워커가 부재(미연결/timeout)일 때 그 owner를 *대신 답하게* 하는 워커 — 단, 중앙 공용 LLM이 아니라 **owner가 명시적으로 위임한 자기 데이터·자기 신원의 격리 인스턴스**다(ADR 0012). 물리적으론 중앙 인프라에 호스팅되지만 논리적으론 *여전히 owner가 답한다* — 그래서 ADR 0010("답변 주체 = owner Claude Code")을 깨지 않고 "owner가 위임·관리하는 환경(owner PC **또는** owner 백업 인스턴스)"으로 보강한다. 새 타입이 아니라 **`WorkerLogic` 재사용** — `PushWork`→`ClaudeCodeRuntime`→`SubmitAnswer`의 같은 프레임 흐름을 타고, 다른 건 *어디서 도는가*(중앙 호스팅 격리)와 *무엇을 들고 도는가*(owner PC 실데이터가 아니라 위임 스냅샷=`DelegationSnapshot`)뿐(둘 다 생성자 인자로 흡수, `worker.py` 무변경). 답은 owner 미검토·스냅샷 기반이라 **신뢰 하향**(`Answer.mode="backup"`). 책임은 여전히 owner(opt-in 위임이므로 — `answered_by`=owner 불변). 폴백 사슬: owner PC 워커 → (부재) Backup Worker → (백업도 부재/실패) Manager escalation(미아 없음 — 반드시 종착). 진짜 그 owner가 위임한 백업인지는 인증으로 강제(ADR 0009 → T6.5).
_Avoid_: Fallback LLM(중앙 공용 추론과 혼동), Agent(단독), Standby(상시/기동 무관)

**WorkerRole (워커 등급)**:
한 owner에 붙은 워커의 우선순위 등급 — `primary`(owner PC 워커, 1차)와 `backup`(owner 위임 백업 워커, 2차). 같은 owner 안에서 *어느 연결로 먼저 push하나*를 가른다(신원=`owner_id`는 같고, 등급은 그 owner 안의 우선순위). `RegisterWorker`에 실려 워커가 자기 등급을 선언하고(PC=기본 `primary`, 백업=`backup`), `WebSocketDispatcher`의 연결 레지스트리가 owner당 등급별 연결(`owner_id → {role → send}`)로 보관해 push 시 우선순위 선택(primary 있으면 primary, 없으면 backup, 둘 다 없으면 큐 대기→timeout escalation). (ADR 0012)
_Avoid_: WorkerType(타입이 아니라 우선순위), Priority(단독 — 디스패치 결정과 혼동)

**DelegationSnapshot (위임 스냅샷)**:
owner가 백업 워커에 *명시적으로 위임한* 격리 스냅샷의 메타 — `owner_id` · `agent_ids`(위임 대상 카드, 어느 담당 영역을 백업이 답하나) · `snapshot_at`(스냅샷 뜬 시각, staleness 판정 기준). **이 레코드는 위임 사실과 최신성만 든다** — 실 지식 본체(문서·인덱스)는 owner별 격리 저장소에 있고 *백업 인스턴스만* owner 키로 접근한다(중앙 무지식 보존 — 중앙·디스패처는 이 메타만 보고 실 데이터는 안 본다). AgentCard 자기보고 필드가 *아니라* 별 레코드인 이유: 위임은 가용성·보안 정책이지 담당 영역 선언이 아니고(Authority 중앙·ADR 0004 정합), opt-in이 1급으로 드러나야 위임 없는 owner가 백업 단계를 건너뛰어 곧장 escalation으로 떨어진다(폴백 사슬 정합). **staleness 정책(ADR 0012 결정 9)**: `snapshot_at`이 정책 임계를 넘으면 백업은 *신뢰를 더 낮춰 답하는 게 아니라 아예 거부*하고 escalation으로 떨어진다(너무 오래된 데이터로 답하느니 사람 — "모르면 안전하게 넘긴다"). 그래서 *답이 나가는* 백업은 모두 임계 내 fresh라 `mode`는 `backup` 하나로 충분하다(fresh/stale을 mode로 쪼개지 않음 — staleness는 *답 여부*를 가르는 메타이지 *나간 답의 mode*를 가르는 축이 아니다). `snapshot_at`은 검토 시 owner가 "얼마나 오래된 스냅샷으로 답했나" 참고하는 맥락 메타로도 `BackupReviewItem`에 실린다. 동기화 트리거는 owner 수동+주기+지식 변경 이벤트(정책, 실 파이프라인 후속). 암호화·owner 키 핸들은 *연결점만*(실 구현 후속, ADR 0009 연계). (ADR 0012)
_Avoid_: Backup(단독 — 워커와 혼동), Mirror, Replica

**BackupReviewItem (백업 답 검토 항목)**:
백업이 owner 이름으로 답한 한 건의 *검토 대기* 항목 — owner가 복귀해 보고·정정·승격할 대상(ADR 0012 결정 7). `ConflictCase`가 미해소 *다툼*을 담듯 이건 미검토 *백업 답*을 담는다(Owner 처리함 Inbox의 두 번째 면). `owner_id`(처리함 귀속 키) · `agent_id`(어느 담당 영역) · `question`(원 질문) · `backup_answer_text`(백업이 낸 답 본문 — owner가 정정 판단 근거로 봄, ConflictCase가 question 원문 보관하는 정신) · `ticket_id`(어느 작업의 답인가 — audit·재노출 연결 키, `item_id`로 재사용) · `snapshot_at`(그 답이 쓴 위임 스냅샷 시각, staleness 맥락) · `answered_at`(주입 clock 결정론) · `status`(`pending_review`/`reviewed`, ConflictCase.status와 같은 결) · `review`(reviewed일 때만 — `BackupReview`). pending_review→reviewed 전이는 `review_with()`가 `item_id` 보존한 새 인스턴스를 돌려준다(파괴적 변경 X — `ConflictCase.resolve()`와 같은 정신). **생성 트리거**: 백업 연결 답이 회신될 때 디스패처가 `mode=backup` 강제 하향과 *함께* `BackupReviewStore.add`를 호출한다(backup이란 사실=연결 등급이 진실이라 생성도 디스패처 책임 — 한 사건의 두 면). primary·full 답엔 생성 안 함(검토 불요). **미검토 백업 답이 처리함에 쌓이는 것 자체가** "owner가 자리를 비운 동안 자기 이름으로 나간 답"의 가시화다(다툼이 Inbox에 쌓이듯). (ADR 0012 결정 7)
_Avoid_: BackupAnswer(단독 — Answer와 혼동), ReviewTask, PendingAnswer

**BackupReview (백업 답 검토 결과)**:
owner가 미검토 백업 답에 내리는 1인칭 처분 — 세 결말 중 하나의 sealed sum("타입이 곧 상태", RoutingDecision·ConsensusOutcome·DispatchOutcome 정신). **Approve**(승인 — "이대로 맞다", 재노출 시 `mode` backup→full 승격, text 그대로) · **Correct**(정정 — owner가 새 답 발행해 기존 backup 답 대체, `corrected_text`·`sources`, 재노출 시 `mode=full` owner 실답) · **Dismiss**(무시 — "검토했고 조치 안 함", 검토 *완료* 사실은 남아 미검토와 구분). 모두 `by_owner`가 자기 책임 답을 1인칭으로 처분(검토 서비스가 `item.owner_id == by_owner` 강제 — ConsensusService가 후보 owner 강제하듯). `ConcurOnPrimary`와 같은 1인칭 표 정신. "보류"는 별 결말이 아니라 status가 여전히 `pending_review`인 것(StillOpen이 별 결말 아니라 미완 상태이듯). **Approve·Correct 모두 backup→full 신뢰 복원**(owner 검토를 거쳤으므로) — 차이는 Correct는 text까지 바뀌고 Approve는 mode만. **Precedent를 만들지 *않는다*** — 검토는 *답이 맞나*의 판단이지 *누가 담당인가*(라우팅 판례)가 아니다(담당은 이미 그 owner). 검토 기록은 audit(절차 기록)에만, `PrecedentStore`엔 안 들어간다. (ADR 0012 결정 7)
_Avoid_: ReviewOutcome, Verdict, Resolution(이건 다툼 합의 결론 — 검토 결과와 구분)

**BackupReviewStore (검토 저장소)**:
미검토 `BackupReviewItem`을 보관·조회하는 포트 — `ConflictCaseStore`·`AuditLog`·`PrecedentStore`와 *같은 포트 패턴*(Protocol + `InMemoryBackupReviewStore`). `add(item)`(open_case 대응) · `get(item_id)` · `pending_for_owner(owner_id)`(=`open_for_owner`, 처리함 — owner 복귀 시 "내가 검토할 백업 답들" 조회) · `mark_reviewed(item)`(mark_resolved 대응). **`ConflictCaseStore`를 그대로 재사용하지 않고 별 포트**인 이유: 담는 값이 다르다(다툼·후보 vs 백업 답·검토) — 한 store에 두 타입을 섞으면 `open_for_owner`가 무엇을 돌려주는지 모호해지고 망라가 깨진다. 그러나 *패턴은 100% 재사용*(검증된 모양의 두 번째 인스턴스, 새 메커니즘 0). **전이 ≠ 기록** — 미검토 도메인 상태 보관이지 절차 로그(AuditLog)가 아니다. (ADR 0012 결정 7)
_Avoid_: ReviewDB, Repository(단독)

**Work Queue (작업 큐)**:
중앙이 owner별로 질문 작업을 적재하는 큐. owner Worker가 연결돼 있을 때 가져가고, owner 부재/PC 꺼짐이면 작업은 *대기*한다(비동기 — 답은 즉시 보장되지 않는다). 미해소 작업의 도메인 보관소지 절차 로그가 아니다(전이 ≠ 기록 — AuditLog와 별개). 큐에 든 작업은 회신되거나 escalation되거나 둘 중 하나로 반드시 종착한다(미아 없음).
_Avoid_: Inbox(이건 Owner 처리함 — 합의 다툼, 작업 큐와 구분), Manager 큐(사람 위계 escalation), Mailbox

**RuntimeDispatcher (디스패처)**:
중앙이 owner별로 작업을 라우팅하고 답을 수집하는 포트 — `AuditLog`·`PrecedentStore`·`ConflictCaseStore`와 같은 패턴(Protocol + 구현체들). 중앙측 `dispatch(question, card) -> WorkTicket`(작업 큐 적재·추적표 즉시 반환) · `poll(ticket) -> DispatchOutcome`(회신·대기·escalation 조회), 워커측 `claim(owner_id) -> WorkTicket | None` · `submit(ticket_id, answer)`. **`ask_org`의 답 획득 경로**다 — `ask_org`는 동기 `AgentRuntime.answer`를 직접 부르지 않고 `dispatch→poll`로 `DispatchOutcome`을 얻어 `OrgReply`로 투영한다(ADR 0011 결정 4). 구현체: `InMemoryWorkQueueDispatcher`(in-process 작업 큐, 결정론 테스트·슬라이스1) · `LocalRuntimeDispatcher`(동기 런타임을 즉시-Delivered로 감싸는 즉답 다리) · `WebSocketDispatcher`(WS 전송층, 슬라이스2b — 아래). Authority·지식은 owner 환경에 있고 디스패처는 *운반·수집*만 — 답을 만들지 않는다. (ADR 0011)
_Avoid_: Router(이건 담당 결정 — 디스패처는 작업 운반), Broker, Queue(단독 — Work Queue는 데이터, 디스패처는 그 위 포트)

**LocalRuntimeDispatcher (로컬 즉답 디스패처)**:
동기 `AgentRuntime`(StubRuntime·ClaudeCodeRuntime)을 `RuntimeDispatcher` 포트로 감싸 *항상 즉시 `Delivered`*를 돌려주는 in-process 어댑터. `dispatch`가 그 자리에서 `runtime.answer`를 호출해 답을 만들고 회신 완료 상태로 두며, `poll`은 곧장 `Delivered`(그 Answer)를 반환한다 — 네트워크·워커·대기 없음(미회신·timeout 구조적 불가). `ask_org`가 디스패처만 보게 된 뒤(ADR 0011 결정 4) in-process 데모/단위 테스트가 *즉답*을 받게 하는 다리다(Routed→Answered가 한 호출에). 분산의 *디제너레이트 케이스*(워커=중앙, 큐 길이 0)이지 별 경로가 아니며, 실제 분산이 필요한 슬라이스2b `WebSocketDispatcher`가 이 자리를 대신한다 — 그때 비로소 `Pending(dispatched)` 분기가 살아난다. (`DispatchingRuntime`과 방향이 반대: 이건 동기 runtime→디스패처, 저건 디스패처→동기 answer.)
_Avoid_: SyncDispatcher, InlineRuntime

**WebSocketDispatcher (WS 전송 디스패처)**:
`InMemoryWorkQueueDispatcher`(작업 큐 도메인)를 *합성해 재사용*하고 그 위에 WebSocket 전송만 얹는 `RuntimeDispatcher` 구현(ADR 0011 결정 6, 슬라이스2b). 새 큐 도메인이 아니다 — 큐 상태기계(queued↔claimed↔answered↔expired·단조 종착·timeout escalation·owner별 격리)는 합성한 in-memory 큐가 소유하고(미아 없음·idempotency 1차 보증), WS는 `claim`/`submit`을 *전송*으로 중계할 뿐. 포트 무변경: `claim(owner_id)`의 pull은 "중앙 핸들러가 워커 대신 claim해 `PushWork`로 push"로 의미 보존(워커가 직접 호출 안 함, 트리거 주체만 이동), `submit`은 "워커가 보낸 `SubmitAnswer`를 핸들러가 받아 내부 submit 호출"로. 연결 레지스트리(owner_id→소켓 send 콜백)를 들어 dispatch 시 연결된 워커에 push(미연결이면 큐 대기=기존 `AwaitingWorker`). 실패 모드(결정 6-4): 끊김 시 `release_claims`(미회신 claimed→queued re-queue, 단조성 보존), 중복은 `ticket_id` 멱등(answered 재submit 무시 — 큐가 보장), heartbeat 생존 판정, 워커 인증 거부 hook(ADR 0009 연결점). **가용성 확장(ADR 0012, 설계)**: 연결 레지스트리를 owner당 등급별(`owner_id → {WorkerRole → send}`)로 확장해 owner PC(`primary`) 부재 시 owner 위임 백업(`backup`)으로 push를 폴백한다 — push 대상 선택만 우선순위로 바뀌고(primary→backup→큐 대기→timeout escalation) claim/submit/큐 도메인은 무변경. 백업 연결로 처리된 답은 `submit` 시 `Answer.mode`를 `backup`으로 강제 하향(백업이라는 사실은 *연결 등급*이 진실 — 워커 자기보고에 맡기지 않음). **timeout 예산(결정 8)**: 단일 timeout을 t1(primary 대기)+t2(backup 대기) 2단으로 — primary 미연결은 *즉시* 백업, primary 연결됐으나 무응답은 t1 후 claim 해제(`release_claims` 재사용)→backup 전환(중복 답은 `ticket_id` 멱등이 흡수, 먼저 온 답 채택·MVP 순차). **staleness(결정 9)**: backup push 전 `DelegationSnapshot.snapshot_at`이 임계 초과면 push 안 하고 큐 대기→escalation(stale 거부). **cold 연결점(결정 10)**: warm이 MVP(백업 미리 연결)이고, cold는 디스패처 *기동 요청 hook*(`wake_backup: Callable[[str], None] | None` 주입, `manager_of` 정신)만 연결점으로 둔다 — cold의 "기동 대기"는 기존 큐 대기(`AwaitingWorker`)가 표현(새 상태 0), 기동 실패는 기존 timeout escalation 흡수. `transport.py`. **중앙 WS 핸들러**(`server.py`의 `create_worker_app(dispatcher)` + `@app.websocket("/worker")`)가 워커 아웃바운드 연결을 받아 이 디스패처로 프레임을 중계한다 — 채팅·처리함 어댑터(`web.py`)와 책임 분리. (ADR 0011 결정 6, 구현 슬라이스2b-i)
_Avoid_: Broker, WsServer(단독 — 디스패처는 포트 구현, WS 서버 핸들러는 그 위 어댑터)

**Transport Frame (전송 프레임)**:
owner 워커↔중앙 WebSocket으로 오가는 와이어 메시지 — `type` 판별 필드를 가진 봉투(envelope), pydantic v2 DTO(`transport.py`). 도메인 값 객체(`WorkTicket`·`Answer`, frozen dataclass)가 *아니라* 전송 DTO라 분리하고 경계에서 변환한다(전이 ≠ 전송). 워커→중앙(업스트림): `RegisterWorker`(owner 신원 선언·인증 연결점) · `SubmitAnswer`(답 회신, `ticket_id` 멱등) · `Heartbeat`/`Ack`(연결 생존·수신 확인). 중앙→워커(다운스트림): `Welcome`/`AuthError`(등록 수락·거부) · `PushWork`(claim한 작업 전달) · `Ping`(생존 확인). (ADR 0011 결정 6-3)
_Avoid_: Message(단독), Event, Packet

**DispatchingRuntime (동기 호환 어댑터)**:
`RuntimeDispatcher` 위에 동기 `AgentRuntime.answer`를 얹는 어댑터(ADR 0011 슬라이스1 산물) — dispatch→블로킹 poll로 동기 계약을 보존한다. 단 `ask_org`는 이를 **거치지 않는다**(ask_org는 디스패처를 직접 보고 escalation/미회신을 `Pending`으로 표면화 — ADR 0011 결정 4, "Answer 위장 금지"). 이 어댑터의 `EscalatedToManager`/`AwaitingWorker` → 폴백 `Answer` 변환은 *AgentRuntime 계약(항상 Answer 반환)을 지키기 위한 어댑터 한정 동작*이지 도메인 처분을 답으로 뭉개도 된다는 뜻이 아니다 — "동기 answer를 꼭 요구하는 비-ask_org 호출처"의 호환 경로로만 남는다(현재 그런 호출처는 단위 테스트뿐, 프로덕션 경로 아님).
_Avoid_: (ask_org의 답 경로로 오인 금지 — 그건 LocalRuntimeDispatcher)

**WorkTicket (작업 추적표)**:
중앙이 작업을 owner 큐에 넣을 때 즉시 돌려받는 추적표 — 답이 아니라 비동기 손잡이. `owner_id`(어느 owner 큐) · `agent_id`(어느 카드의 답) · `question`(워커가 로컬 claude에 넘길 원문) · `enqueued_at`(주입 clock, timeout 판정 기준) · `ticket_id`. `owner_id` 귀속이 신원/책임 연결점(회신이 진짜 그 owner에게서 왔는지, ADR 0009). 카드 본문이 아니라 식별자만 든다(`Candidate`·`ConflictCase`와 같은 정신 — 카드 출처는 Registry).
_Avoid_: Job(단독), Task(단독), Receipt

**DispatchOutcome (디스패치 결과)**:
`poll(ticket)`이 돌려주는 결말. "타입이 곧 상태"(RoutingDecision·ConsensusOutcome 정신) — 세 결말 중 하나. **Delivered**(워커가 owner 환경에서 답을 만들어 회신 → `Answer` 도착, `mode` 보존 = Approval 게이트 합류 자리) · **AwaitingWorker**(아직 회신 없음 — 워커 미연결/생성 중 → 큐에 대기, `waited` 경과시간) · **EscalatedToManager**(timeout/owner 부재 → 미아·합의 실패와 같은 종착 처분 = Escalation. `manager_id`는 T5.2 Manager 큐가 *기계로 소비*할 1급 식별자 — owner의 `manages` 상위 User.id(루트면 `None`), 어느 큐에 적재할지. `reason`은 그 escalation의 *사람용* 자연어 근거. 둘을 분리: 큐 라우팅은 식별자로, 운영자 화면은 문장으로 — `ConsensusOutcome.Deadlocked.reason`이 사람용이듯). owner 부재·timeout escalation은 새 경로가 아니라 사람 그래프 상향(Owner→Manager) 재사용이며, 실제 Manager 큐 연결은 T5.2로 자리만 둔다(ConsensusOutcome.Deadlocked 정합). 사용자向 투영: `AwaitingWorker`·`EscalatedToManager` 둘 다 `OrgReply.Pending(kind="dispatched")`로 모인다(`manager_id`·`reason`은 사용자에게 감추는 내부값 — Pending에 싣지 않음). (ADR 0011)
_Avoid_: Result(단독 — Answer와 혼동), DeliveryStatus

**Registry**:
Agent Card를 등록·보관·조회하는 모듈. 카드의 출처(YAML 파일)다.
_Avoid_: Directory, Store, 중앙 서버

**Router**:
질문을 받아 등록된 Agent Card들과 대조해 담당 후보를 결정하는 모듈.
_Avoid_: Broker, Dispatcher, 중앙 서버

### Routing outcomes

**RoutingDecision**:
라우터가 한 질문에 내리는 결과. 세 처분(disposition) 중 하나다 — **Routed / Contested / Unowned**. 인스턴스의 *타입*이 곧 상태이며, 각 타입은 자기 처분에 필요한 필드만 갖는다(속성값을 들여다보지 않고 타입으로 판별).

**Routed**:
담당(primary)이 정해진 결정. Collaborator·Approval을 동반할 수 있다.

**Contested**:
후보가 여럿이라 담당이 아직 안 정해진 결정. 후보 Owner 합의 또는 Manager로 간다.

**Unowned**:
후보가 0인 미아 결정. 루트 User로 Escalation된다.

**Route**:
질문을 primary Agent Card에 연결하는 진입 행위(= Routed를 만드는 것).

**Candidate**:
질문의 domains에 부분적으로 매칭되어 담당 가능성이 있는 Agent Card. 0·1·다수.

**Intent**:
질문을 분류해 얻은 주제 라벨. Router가 카드의 `domains`와 대조하는 키다. **Classifier** 포트가 질문에서 생성한다(v0 규칙 기반 → 후순위 LLM).
_Avoid_: Topic, Category

**Collaboration / Collaborator**:
primary는 그대로 두고 추가로 끌어들이는 협업. 끌려 들어온 Card가 Collaborator. (기획서 `required_handoffs` 필드 → `collaborators`로 개명.)

**Approval (승인 게이트)**:
Routed인데 실행 전 사람 사인이 필요한 상태. 라우팅은 됐고 게이트만 걸린다. (기획서 `human_approval_required`)
_Avoid_: Escalation(이건 게이트가 아니다)

**Escalation**:
담당 자체를 사람(Manager)이 정하도록 결정을 사람에게 넘기는 것 — Contested(합의 실패)·Unowned의 종착 처분. Approval(게이트)과 구분.

**Transfer (이관)**:
이미 배정된 primary를 다른 Card로 재지정하는 런타임 사건. A 빠지고 B 맡음. 전이이며, 기록은 감사 로그가 맡는다(전이 ≠ 기록).
_Avoid_: Handoff(모호 — 프레임워크 용어·기록 의미와 혼동), Delegate, Forward

> 한 **Routed** 안에서 Route(primary)·Collaboration(collaborators)·Approval(게이트)은 동시에 성립 가능한 독립 축이다. **Escalation**은 Contested/Unowned의 종착이고, **Transfer**는 배정된 primary를 사후에 바꾸는 런타임 사건이다.

### User-facing outcome (실 사용자向 투영)

라우팅 기계장치(RoutingDecision·Candidate·Confidence·trace)는 실 사용자에게 감춘다. 사용자는 *처분의 결과*만 사용자 말로 받는다. `ask_org` 핸들러가 `RoutingDecision`을 아래 결과 타입으로 투영한다 — 도메인(RoutingDecision)과 표현(OrgReply)의 경계.

**OrgReply**:
실 사용자가 `ask_org`에서 돌려받는 결과. `RoutingDecision`과 `DispatchOutcome`의 사용자向 투영이며, 두 형태로 모인다 — **Answered**(담당이 답함 = `Routed`→`Delivered`) · **Pending**(아직 답이 없음 — 사람 손으로 넘어갔거나 담당이 답을 만드는 중). 인스턴스의 *타입*이 곧 사용자가 받은 상태다. 노출 불변식: 담당·승인 상태·출처만 보이고, confidence·trace·후보·manager_id·reason 등 *조직 내부 구조*는 절대 싣지 않는다 — 단 답 회수를 위한 **불투명 추적 토큰 1개**는 예외(ADR 0011 결정 6-5, 슬라이스2b — 사용자가 그것으로 답을 조회. 토큰은 내부 구조를 비추지 않는 ID라 노출 불변식의 *정밀화*이지 완화가 아니다).
_Avoid_: Response, Result(단독 — Answer와 혼동)

**Answered**:
Routed가 투영된 결과. `text`(Answer 본문) · `answered_by`(primary의 owner·agent_id) · `mode`(`full`/`draft_only` — Approval 게이트면 draft) · `sources`를 노출. Approval이 붙은 Routed는 `mode=draft_only`로 "초안·승인 대기"를 표시한다.

**Pending**:
즉답이 없는 상태의 사용자向 투영 — 담당이 사람 손에 있거나(Contested·Unowned), 담당이 정해졌으나 분산 전송으로 답을 기다리는/사람으로 넘어가는 중(dispatched). `kind`(`contested`/`unowned`/`dispatched`)와 사용자向 안내 문구만 노출하고, 후보 목록·escalation 대상·`manager_id`·`reason`·`ticket_id` 같은 내부는 감춘다. Contested는 *다툼이라는 사실조차* 감추고 "담당을 확인하는 중" 류 중립 안내만(후보가 여럿이라는 내부를 비추지 않는다), Unowned는 "아직 담당이 없어 매니저에게 전달" 류. **dispatched**는 `DispatchOutcome`의 `AwaitingWorker`(미회신)와 `EscalatedToManager`(timeout/owner 부재)를 *함께* 투영한다 — 둘은 도메인에선 다른 처분이지만 사용자 관점에선 "담당에게 보냈는데 아직 답이 없다"로 동일하고, 워커 미연결인지 Manager escalation인지는 감춰야 할 내부값이라 한 kind로 모은다(쪼개면 내부 구분이 샌다). "담당에게 전달했고 답변이 준비되면 알림" 류 중립 안내만. 답 회수(ADR 0011 결정 6-5, 슬라이스2b-i 구현): `dispatched`는 *불투명 추적 토큰*(`tracking`)을 동반해(internal 구조 노출 없는 ID 1개) 사용자/데모 UI가 그 토큰으로 답을 *조회*(pull)한다 — 워커가 나중에 회신한 실 claude 답이 사용자에게 도달하는 경로. **구현 방침**: 서버(`AskOrg._tracking`)가 `tracking→WorkTicket` 매핑을 보관하고, 토큰은 `ticket_id`와 *분리된* 별도 `uuid4().hex`다(ticket_id조차 노출하지 않음 — 6-5의 "서버가 ticket 보관" 대안 채택). 조회는 `AskOrg.retrieve(tracking)`(`poll` 재노출) → web `GET /ask/{tracking}`. 사용자向 푸시(SSE/WS)는 범위 밖(조회로 한정).
_Avoid_: Escalation(이건 도메인 처분, Pending은 그 사용자向 표현)

### Conflict & learning

**Authority (관할)**:
다툼이 나는 영역의 최종 결정권. 중앙 라우팅 규칙에서 선언되며 카드가 자기보고할 수 없다.
_Avoid_: Trust, Priority(단독)

**Conflict**:
한 사안의 담당이 깨끗하게 정해지지 않는 상태. 두 극과 각각의 해소 경로 —
- **Overlap**(후보 여럿): 후보 Owner 전원을 호출해 당사자끼리 담당을 합의. 합의 실패 시 Manager로.
- **Gap**(후보 0, 미아 질문): Manager가 기존 Owner 지정 또는 신규 Agent Card 생성.
두 경로 모두 결론은 Resolution → Precedent로 떨어진다.

**ConflictCase (다툼 케이스)**:
미해소 Overlap 다툼을 *조회 가능한 상태*로 저장한 단위. `Contested`는 라우터의 순간 판정일 뿐이라 후보 Owner가 1인칭 합의하려면 그 다툼이 머물 곳이 필요하다 — 그게 ConflictCase. `intent`(어떤 분류 라벨의 다툼) · `question`(원문, Owner가 처리함에서 맥락 판단) · `candidates`(다툼에 걸린 **Candidate** = `agent_id`+`owner` 식별자만, 카드 본문은 Registry가 출처) · `status`(`open`/`resolved`) · `opened_at`(주입 clock 결정론) · `case_id` · `resolution`(resolved일 때만). open→resolved 전이는 `resolve()`가 `case_id` 보존한 새 인스턴스를 돌려준다(파괴적 변경 X). `Contested.candidates`(AgentCard 객체)와 달리 식별자만 들고, owner를 함께 드는 이유는 **Owner별 처리함 조회**가 본질이라서. (ADR 0008)
_Avoid_: Ticket, Dispute, Issue

**Inbox (처리함)**:
한 Owner에게 귀속된 미해소 항목들의 모음 — PRD 페르소나의 "Owner 처리함" 그 면이다. **두 면(탭)을 가진다**: ① 자기 카드가 후보로 걸린 open ConflictCase들(다툼 합의 — 데이터 원천 **ConflictCaseStore**.`open_for_owner`), ② owner 부재 중 백업이 자기 이름으로 답한 미검토 백업 답들(검토 — 데이터 원천 **BackupReviewStore**.`pending_for_owner`, ADR 0012 결정 7). 둘 다 "Owner별 미해소 항목 보관·조회"라는 한 패턴의 두 인스턴스이며, 별도 영속 상태가 아니라 각 store의 open/pending 투영이다. Owner는 ①에서 1인칭 합의 표(ConcurOnPrimary)를 던지고, ②에서 백업 답을 1인칭으로 처분(BackupReview — 승인/정정/무시)한다. ②가 결정 5의 "백업 답도 owner 책임"을 명목에서 실질로 만든다(owner가 복귀하면 자기 이름으로 나간 답을 반드시 마주한다).
_Avoid_: Queue(이건 Manager 큐 — 사람 위계 escalation, 처리함과 구분), Tasklist

**ConflictCaseStore (케이스 저장소)**:
open ConflictCase를 보관·조회하는 포트 — `AuditLog`·`PrecedentStore`와 같은 패턴(Protocol + `InMemoryConflictCaseStore`). `open_for_owner(owner)`(처리함) · `open_for_intent(intent)`(중복 open 방지 선조회) · `mark_resolved(case)`(open에서 빼 history에 append) · `get(case_id)` · `open_case(case)`. **전이 ≠ 기록** — 미해소 도메인 상태를 보관하는 곳이지 운영자向 절차 로그(AuditLog)가 아니다. 같은 intent로 이미 open된 케이스가 있으면 새로 만들지 않는다(같은 다툼이 질문마다 케이스를 양산하지 않게).
_Avoid_: CaseDB, Repository(단독)

**ConcurOnPrimary (합의 표)**:
후보 Owner 한 명의 *1인칭* 합의 입력. `by_owner`(표를 던진 Owner `User.id`)가 `on_agent`(primary로 지목한 카드 `agent_id`)를 담당으로 지목 + `rationale`(근거, 선택). claim("내가 맡는다"=자기 카드 지목)과 concede("쟤가 맡아"=남 지목)를 **같은 한 축**(primary는 누구)으로 환원한 단일 표 — 찬반 2축·라운드·코멘트 스레드는 후순위. 후보 Owner 전원이 같은 `on_agent`를 지목하면 합의 성립 → Resolution. (ADR 0008)
_Avoid_: Vote(단독 — 다수결 아님, 전원 일치), Claim/Concede(둘로 쪼개지 않고 한 축으로)

**ConsensusOutcome (합의 결과)**:
후보 Owner들의 ConcurOnPrimary 표를 모아 합의를 시도한 결과. "타입이 곧 상태"(RoutingDecision·OrgReply 정신) — 세 결말 중 하나. **Agreed**(전원 일치 → `Resolution`+`Precedent` 산출, 케이스 closed) · **StillOpen**(표가 덜 모임 → 케이스 open 유지, `pending_owners` 남음) · **Deadlocked**(표가 갈림=교착 → 합의 실패 *자리만*, Manager escalation 처리는 T5.2 Manager 큐로 미룸). T4.2의 핵심은 Agreed→Precedent이며, Deadlocked는 도메인에 자리를 두되 처리는 미룬다.
_Avoid_: Result(단독 — Answer와 혼동)

**Manager**:
다른 User를 `manages` 하는 User(조직장). 사람 위계의 상위 노드이며 Owner와 무관 — 에이전트를 소유할 수도, 안 할 수도 있다. 미해소 Conflict의 escalation은 카드가 아니라 사람 그래프를 타고 Owner의 manager로 올라가며, 꼭대기는 루트 User.
_Avoid_: Admin(단독), Triage, "Owner의 일종"

**Resolution**:
한 Conflict에 대해 관련 Owner들(필요시 사람 Authority)이 합의한 결론. `frozen` 값 객체 — `intent`(어떤 분류 라벨의 다툼인지) · `primary`(합의로 정해진 담당 `agent_id`) · `rationale`(합의 근거, 선택). "타입이 곧 상태"라 이것 자체가 합의됐다는 사실이다. Overlap에선 ConflictCase의 후보 Owner들이 ConcurOnPrimary 표로 전원 일치할 때 산출되고(`ConsensusOutcome.Agreed`), 곧장 Precedent로 기록되며 그 ConflictCase는 resolved된다.

**Precedent (판례)**:
Resolution을 append-only로 남긴 기록. 라우터가 유사한 미래 케이스에 참조한다. 구조화된 조직 암묵지이자 곧 라우팅 회귀 테스트 케이스. `frozen` 값 객체 — `resolution`(무엇을 합의했나) · `recorded_at`(언제 기록됐나, 주입 clock 기반 결정론). **PrecedentStore** 포트로 보관한다 — `record(resolution) -> Precedent`(append) · `lookup(intent) -> Precedent | None`(라우터 조회). 감사 로그와 같은 append-only 메커니즘이되, 운영자向 절차 기록(AuditLog)과 달리 라우터向 intent-색인 조회 사실이라 포트를 분리한다(ADR 0002 보강).
_Avoid_: Rule(단독), History

**Confidence**:
한 결정·답변의 확신도(0~1). 자기보고값이며 Authority와 무관.

**Trust Label**:
카드·답변의 취급 제약 태그(`internal_only` 등). Authority·Confidence와 구분.

### Audit (운영자向 기록)

**Audit log (감사 로그)**:
운영자向 append-only JSONL 기록. 한 질문 처리의 전체 절차(질문·intent·라우팅 처분·디스패치 결말)를 *내부값까지* 담는다 — OrgReply(사용자向)가 감춘 `confidence`·`candidates`·`escalated_to`·`primary`, 그리고 `Pending(dispatched)`가 떨군 디스패치 escalation의 `manager_id`·`reason`까지 여기선 전부 기록. 백업 답을 owner가 사후 검토(`BackupReview`)하면 그 검토 *전이*도 같은 ticket_id를 키로 *사후 줄*로 append된다(라우팅→디스패치→검토, 한 질문의 세 번째 절차 — 원 답 줄은 append-only라 불변, ADR 0012 결정 7-3). 미래 모니터링(질문→절차→답)의 데이터 원천. 전이가 아니라 기록(전이 ≠ 기록).
_Avoid_: Trace(단독 — 사용자에게 감추는 라우팅 내부와 혼동), Log(단독)

**AuditEntry**:
Audit log의 한 줄. 한 질문 처리 절차의 기록 단위 — `timestamp`·`user_id`·`question`·`intent`·`decision`(RoutingDecision 원형, 내부 상세 보존)·`dispatch_outcome`(DispatchOutcome 원형, Routed일 때만; Contested/Unowned는 디스패치를 안 하므로 `None`). OrgReply가 decision·outcome을 투영해 버리는 것과 달리 **둘 다 원형을 그대로 안는다** — 한 질문 처리의 두 절차(라우팅→`decision`, 디스패치→`dispatch_outcome`)를 1급으로 기록. `EscalatedToManager`의 `manager_id`·`reason`은 사용자向 `Pending`에선 떨궈지지만 여기선 전부 남아, `Unowned.escalated_to`를 남기는 것과 *대칭*을 이룬다(둘 다 "escalation 대상" — 같은 처분이 기록 차원에서 같은 모양). `answer`는 별도 필드가 아니라 `dispatch_outcome`에서 유도하는 파생 접근자다(`Delivered.answer`만 답을 가짐 — 같은 답을 두 곳에 두지 않기 위함, SSOT는 `dispatch_outcome`).

## Flagged ambiguities

**"Agent" 단독 사용 금지**: 도메인 모델·코드·이 문서에서 맨 단어 "Agent"는 쓰지 않는다 — 항상 **Owner / Agent Card / Agent Runtime** 중 하나로 한정. (제품·마케팅 산문에서는 "에이전트" 자유 사용 OK.)

## Example dialogue

— "이 질문 누구한테 보내야 해?"
— "Router가 등록된 Agent Card들이랑 대조해서 담당 후보를 골라. 카드 자체는 Registry에 있고."
— "그 카드 누가 관리하는데?"
— "각 Owner가 자기 카드만 관리해. 중앙은 카드 내용을 직접 소유하지 않아."
