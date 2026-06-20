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
Agent Card를 실제로 구동해 질문에 답하는 실행 주체(LLM 등). MVP 범위 밖 — 이름만 예약한다.
_Avoid_: Agent(단독), Bot

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
후보가 0인 미아 결정. 루트 Manager로 Escalation된다.

**Route**:
질문을 primary Agent Card에 연결하는 진입 행위(= Routed를 만드는 것).

**Candidate**:
질문의 domains에 부분적으로 매칭되어 담당 가능성이 있는 Agent Card. 0·1·다수.

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

### Conflict & learning

**Authority (관할)**:
다툼이 나는 영역의 최종 결정권. 중앙 라우팅 규칙에서 선언되며 카드가 자기보고할 수 없다.
_Avoid_: Trust, Priority(단독)

**Conflict**:
한 사안의 담당이 깨끗하게 정해지지 않는 상태. 두 극과 각각의 해소 경로 —
- **Overlap**(후보 여럿): 후보 Owner 전원을 호출해 당사자끼리 담당을 합의. 합의 실패 시 Manager로.
- **Gap**(후보 0, 미아 질문): Manager가 기존 Owner 지정 또는 신규 Agent Card 생성.
두 경로 모두 결론은 Resolution → Precedent로 떨어진다.

**Manager**:
다른 User를 `manages` 하는 User(조직장). 사람 위계의 상위 노드이며 Owner와 무관 — 에이전트를 소유할 수도, 안 할 수도 있다. 미해소 Conflict의 escalation은 카드가 아니라 사람 그래프를 타고 Owner의 manager로 올라가며, 꼭대기는 루트 User.
_Avoid_: Admin(단독), Triage, "Owner의 일종"

**Resolution**:
한 Conflict에 대해 관련 Owner들(필요시 사람 Authority)이 합의한 결론.

**Precedent (판례)**:
Resolution을 append-only로 남긴 기록. 라우터가 유사한 미래 케이스에 참조한다. 구조화된 조직 암묵지이자 곧 라우팅 회귀 테스트 케이스.
_Avoid_: Rule(단독), History

**Confidence**:
한 결정·답변의 확신도(0~1). 자기보고값이며 Authority와 무관.

**Trust Label**:
카드·답변의 취급 제약 태그(`internal_only` 등). Authority·Confidence와 구분.

## Flagged ambiguities

**"Agent" 단독 사용 금지**: 도메인 모델·코드·이 문서에서 맨 단어 "Agent"는 쓰지 않는다 — 항상 **Owner / Agent Card / Agent Runtime** 중 하나로 한정. (제품·마케팅 산문에서는 "에이전트" 자유 사용 OK.)

## Example dialogue

— "이 질문 누구한테 보내야 해?"
— "Router가 등록된 Agent Card들이랑 대조해서 담당 후보를 골라. 카드 자체는 Registry에 있고."
— "그 카드 누가 관리하는데?"
— "각 Owner가 자기 카드만 관리해. 중앙은 카드 내용을 직접 소유하지 않아."
