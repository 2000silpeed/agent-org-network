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
Agent Card를 구동해 질문에 답하는 실행 주체. 라우터가 호출한다. 답변 주체는 각 Owner의 Claude Code다 — 중앙 API 키 LLM이 아니라(ADR 0010). 포트로 분리 — `StubRuntime`(canned, 테스트·스켈레톤) → `ClaudeCodeRuntime`(`claude -p` 헤드리스, T6.1 임시·중앙 1회성·모든 카드가 로컬 claude로 답) → owner별 분산 Claude Code(MCP/A2A, T6.3 — 그때 owner별 지식 격리 성립). `knowledge_sources`는 현재 `Answer.sources` 출처 레이블이며 진짜 문서 RAG는 후속. (ADR 0007·0010)
_Avoid_: Agent(단독), Bot

**Answer**:
Agent Runtime이 한 질문에 산출한 답. `text`(답 본문) · `sources`(근거 출처) · `mode`를 가진다. `mode`는 신뢰 상태 — `full`(그대로 사용자에게)과 `draft_only`(Approval 게이트가 걸려 사람 승인 전까지는 초안). 즉 Routed에 Approval이 붙으면 Runtime은 `draft_only`로 답한다(승인 ≠ 라우팅, 게이트만 걸림). "답에는 항상 담당·신뢰 상태(승인/초안/출처)가 붙는다"는 불변식의 그 답.
_Avoid_: Response, Result, Reply

### Distributed transport (분산 전송)

owner PC는 서버를 노출하지 않는다(NAT/방화벽 뒤·고정 IP 없음·상시 가동 X). 그래서 owner PC의 워커가 중앙에 *역방향 아웃바운드*로 연결해 작업을 가져가고, 중앙은 owner별 작업 큐에 적재해 비동기로 답을 수집한다. 논리적 호출 방향(질문 중앙→owner)과 물리적 연결 방향(소켓은 owner→중앙)은 분리된다. (ADR 0011 — Agent Runtime의 도달 방식, 답변 주체=owner Claude Code는 ADR 0010 그대로.)

**Owner Worker (오너 워커)**:
각 Owner PC에서 도는 작은 실행 주체. 중앙에 *아웃바운드*로 연결(폴링 또는 WS/SSE)해 자기 owner 작업 큐의 작업을 가져가(`claim`), 로컬 Claude Code(`ClaudeCodeRuntime` 재사용)로 답을 만들어 중앙에 회신(`submit`)한다. 중앙이 owner PC로 인바운드 연결을 *걸지 않는다* — 연결을 거는 쪽은 항상 워커다. 진짜 그 owner인지는 인증으로 강제한다(ADR 0009 연결점). 선례: Claude Code Remote Control(owner claude가 claude.ai에 아웃바운드 연결).
_Avoid_: Agent(단독), Client(단독 — 논리 호출 방향과 혼동), Node

**Work Queue (작업 큐)**:
중앙이 owner별로 질문 작업을 적재하는 큐. owner Worker가 연결돼 있을 때 가져가고, owner 부재/PC 꺼짐이면 작업은 *대기*한다(비동기 — 답은 즉시 보장되지 않는다). 미해소 작업의 도메인 보관소지 절차 로그가 아니다(전이 ≠ 기록 — AuditLog와 별개). 큐에 든 작업은 회신되거나 escalation되거나 둘 중 하나로 반드시 종착한다(미아 없음).
_Avoid_: Inbox(이건 Owner 처리함 — 합의 다툼, 작업 큐와 구분), Manager 큐(사람 위계 escalation), Mailbox

**RuntimeDispatcher (디스패처)**:
중앙이 owner별로 작업을 라우팅하고 답을 수집하는 포트 — `AuditLog`·`PrecedentStore`·`ConflictCaseStore`와 같은 패턴(Protocol + `InMemoryWorkQueueDispatcher`). 중앙측 `dispatch(question, card) -> WorkTicket`(작업 큐 적재·추적표 즉시 반환) · `poll(ticket) -> DispatchOutcome`(회신·대기·escalation 조회), 워커측 `claim(owner_id) -> WorkTicket | None` · `submit(ticket_id, answer)`. 동기 포트 `AgentRuntime.answer`는 폐기하지 않고 어댑터 `DispatchingRuntime`이 디스패처 위에 얹어 보존한다(dispatch→블로킹 poll). Authority·지식은 owner 환경에 있고 디스패처는 *운반·수집*만 — 답을 만들지 않는다. (ADR 0011)
_Avoid_: Router(이건 담당 결정 — 디스패처는 작업 운반), Broker, Queue(단독 — Work Queue는 데이터, 디스패처는 그 위 포트)

**WorkTicket (작업 추적표)**:
중앙이 작업을 owner 큐에 넣을 때 즉시 돌려받는 추적표 — 답이 아니라 비동기 손잡이. `owner_id`(어느 owner 큐) · `agent_id`(어느 카드의 답) · `question`(워커가 로컬 claude에 넘길 원문) · `enqueued_at`(주입 clock, timeout 판정 기준) · `ticket_id`. `owner_id` 귀속이 신원/책임 연결점(회신이 진짜 그 owner에게서 왔는지, ADR 0009). 카드 본문이 아니라 식별자만 든다(`Candidate`·`ConflictCase`와 같은 정신 — 카드 출처는 Registry).
_Avoid_: Job(단독), Task(단독), Receipt

**DispatchOutcome (디스패치 결과)**:
`poll(ticket)`이 돌려주는 결말. "타입이 곧 상태"(RoutingDecision·ConsensusOutcome 정신) — 세 결말 중 하나. **Delivered**(워커가 owner 환경에서 답을 만들어 회신 → `Answer` 도착, `mode` 보존 = Approval 게이트 합류 자리) · **AwaitingWorker**(아직 회신 없음 — 워커 미연결/생성 중 → 큐에 대기, `waited` 경과시간) · **EscalatedToManager**(timeout/owner 부재 → 미아·합의 실패와 같은 종착 처분 = Escalation, `reason`은 T5.2 Manager 큐로 넘길 근거). owner 부재·timeout escalation은 새 경로가 아니라 사람 그래프 상향(Owner→Manager) 재사용이며, 실제 Manager 큐 연결은 T5.2로 자리만 둔다(ConsensusOutcome.Deadlocked 정합). (ADR 0011)
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
실 사용자가 `ask_org`에서 돌려받는 결과. `RoutingDecision`의 사용자向 투영이며, 세 처분이 두 형태로 모인다 — **Answered**(담당이 답함) · **Pending**(사람 손으로 넘어가 아직 답이 없음). 인스턴스의 *타입*이 곧 사용자가 받은 상태다. 노출 불변식: 담당·승인 상태·출처만 보이고, confidence·trace·후보 내부는 절대 싣지 않는다.
_Avoid_: Response, Result(단독 — Answer와 혼동)

**Answered**:
Routed가 투영된 결과. `text`(Answer 본문) · `answered_by`(primary의 owner·agent_id) · `mode`(`full`/`draft_only` — Approval 게이트면 draft) · `sources`를 노출. Approval이 붙은 Routed는 `mode=draft_only`로 "초안·승인 대기"를 표시한다.

**Pending**:
Contested·Unowned가 투영된 결과 — 담당이 사람 손에 있어 즉답이 없는 상태. `kind`(`contested`/`unowned`)와 사용자向 안내 문구만 노출하고, 후보 목록·escalation 대상 같은 내부는 감춘다. Contested는 *다툼이라는 사실조차* 감추고 "담당을 확인하는 중" 류 중립 안내만(후보가 여럿이라는 내부를 비추지 않는다), Unowned는 "아직 담당이 없어 매니저에게 전달" 류.
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
한 Owner에게 귀속된, 자기 카드가 후보로 걸린 open ConflictCase들의 모음. PRD 페르소나의 "Owner 처리함" 그 면이다. 데이터 원천은 **ConflictCaseStore**의 Owner별 조회(`open_for_owner`)이며, 별도 영속 상태가 아니라 open 케이스의 투영. Owner는 여기서 자기에게 온 다툼을 보고 1인칭 합의 표를 던진다.
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
운영자向 append-only JSONL 기록. 한 질문 처리의 전체 절차(질문·intent·처분·답)를 *내부값까지* 담는다 — OrgReply(사용자向)가 감춘 `confidence`·`candidates`·`escalated_to`·`primary`를 여기선 전부 기록. 미래 모니터링(질문→절차→답)의 데이터 원천. 전이가 아니라 기록(전이 ≠ 기록).
_Avoid_: Trace(단독 — 사용자에게 감추는 라우팅 내부와 혼동), Log(단독)

**AuditEntry**:
Audit log의 한 줄. 한 질문 처리 절차의 기록 단위 — `timestamp`·`user_id`·`question`·`intent`·`decision`(RoutingDecision 원형, 내부 상세 보존)·`answer`(Routed일 때만). OrgReply가 decision을 투영해 버리는 것과 달리 **decision 원형을 그대로 안는다**.

## Flagged ambiguities

**"Agent" 단독 사용 금지**: 도메인 모델·코드·이 문서에서 맨 단어 "Agent"는 쓰지 않는다 — 항상 **Owner / Agent Card / Agent Runtime** 중 하나로 한정. (제품·마케팅 산문에서는 "에이전트" 자유 사용 OK.)

## Example dialogue

— "이 질문 누구한테 보내야 해?"
— "Router가 등록된 Agent Card들이랑 대조해서 담당 후보를 골라. 카드 자체는 Registry에 있고."
— "그 카드 누가 관리하는데?"
— "각 Owner가 자기 카드만 관리해. 중앙은 카드 내용을 직접 소유하지 않아."
