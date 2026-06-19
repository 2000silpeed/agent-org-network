# Agent Org Network 초기 기획문서

작성일: 2026-06-20 08:21 KST  
프로젝트 경로: `/home/sung/project/agent-org-network`  
상태: 아이디어 정리 / 초기 제품 기획

## 1. 출발점

Sung이 제안한 아이디어는 다음과 같다.

> 각 사람이 자기만의 에이전트를 만든다.  
> 그 에이전트 안에 자기 지식, 담당 업무, 업무 페르소나, 자료를 넣는다.  
> 중앙 서버는 모든 지식을 갖는 게 아니라 “어떤 에이전트가 뭘 담당하는지”만 관리한다.  
> A에게 문의가 왔는데 A가 모르면, 중앙 서버가 담당 에이전트를 찾아 연결한다.  
> 여러 사람이 협업 가능한 구조를 만들고, 개인은 자기 에이전트만 깎으면 된다.

이 문서는 그 대화 전체를 바탕으로 만든 첫 기획 초안이다.

## 2. 핵심 아이디어

이 프로젝트는 “회사 지식봇”이 아니다. 더 정확히는 다음 구조다.

> 개인별 업무 에이전트 + 중앙 라우팅 브로커 + 권한/신뢰 레이어

중앙에 모든 지식을 몰아넣는 방식은 시간이 지나면 관리가 무거워진다. 문서가 오래되고, 책임자가 흐려지고, 누가 실제로 알고 있는지 알기 어렵다.

이 프로젝트는 반대로 간다.

- 각 개인/팀이 자기 업무 에이전트를 관리한다.
- 중앙은 지식을 직접 소유하지 않는다.
- 중앙은 누가 무엇을 담당하는지, 어떤 질문을 어디로 보내야 하는지, 어떤 권한이 필요한지만 관리한다.
- 어떤 에이전트가 모르면 아는 척하지 않고 중앙 라우터를 통해 다른 담당 에이전트로 넘긴다.

핵심 문장:

> 중앙 AI가 모든 걸 아는 구조가 아니라, 각 개인의 업무 AI가 자기 전문성을 갖고 중앙은 누가 뭘 아는지만 연결하는 구조.

## 3. 문제 정의

조직 안에서 업무 지식은 보통 문서보다 사람에게 있다.

실제 업무에서는 이런 문제가 자주 생긴다.

- “이건 누구한테 물어봐야 하지?”
- “이 고객 예외 케이스는 누가 기억하지?”
- “문서에는 없는데 실제 처리는 어떻게 하지?”
- “A가 휴가면 이 업무를 누가 이어받지?”
- “이 질문은 영업, 기술, 법무 중 어디로 보내야 하지?”
- “이 답변을 누가 책임질 수 있지?”

기존 중앙 RAG/사내 챗봇은 문서 검색에는 도움이 되지만, 담당자/권한/책임/핸드오프를 잘 다루지 못한다.

이 프로젝트의 문제 정의는 다음과 같다.

> 조직 내 지식과 책임이 사람 단위로 흩어져 있을 때, 사용자가 올바른 담당 에이전트에게 빠르게 연결되고, 각 에이전트가 자기 책임 범위 안에서 답하며, 모르는 영역은 안전하게 넘길 수 있게 한다.

## 4. 제품 관점에서의 포지셔닝

가능한 이름/표현:

- Agent Org Network
- Agent Org Chart
- Personal Agent Network
- AI Responsibility Graph
- Expert Router
- 업무 에이전트 조직도
- 개인 업무 AI 분신 네트워크
- 업무 책임 기반 멀티에이전트 협업 네트워크

현재 작업명은 `Agent Org Network`로 둔다.

제품 한 줄 설명:

> 조직 구성원이 자기 업무 에이전트를 관리하고, 중앙 라우터가 담당자/권한/신뢰도를 기준으로 질문과 일을 연결하는 협업형 AI 조직도.

## 5. 기본 구조

### 5.1 개인 에이전트

각 사람 또는 팀은 자기 업무 에이전트를 갖는다.

예시:

- 영업 담당 에이전트
- 기술지원 담당 에이전트
- 계약/법무 담당 에이전트
- 재무 담당 에이전트
- 운영 담당 에이전트
- 프로젝트 매니저 에이전트

각 에이전트는 다음 정보를 가진다.

- 담당 업무
- 자주 받는 질문
- 판단 기준
- 내부 문서 또는 참고 자료
- 업무 스타일
- 권한 범위
- 답하면 안 되는 영역
- 다른 에이전트로 넘겨야 하는 조건
- 사람 승인이 필요한 조건

중요한 점은 “페르소나”보다 “책임 범위”다.  
단순히 “나는 영업 담당자야”가 아니라, 무엇을 답할 수 있고 무엇을 답하면 안 되는지를 구조화해야 한다.

### 5.2 중앙 라우터

중앙 라우터는 모든 지식을 직접 보관하지 않는다.

중앙이 관리하는 것은 다음이다.

- 에이전트 목록
- 각 에이전트의 담당 영역
- capability card / agent card
- 권한 범위
- 라우팅 규칙
- handoff 규칙
- trust label / 신뢰도
- 감사 로그
- 실패/에스컬레이션 기록

중앙의 역할:

- 찾기
- 연결하기
- 권한 확인하기
- 기록하기
- 실패하면 사람에게 올리기

중앙이 직접 “정답을 만들어내는 만능 AI”가 되면 안 된다. 그 순간 병목이 되고, 기존 중앙 지식봇과 비슷해진다.

### 5.3 질문 흐름

예시 흐름:

1. 사용자가 A 에이전트에게 질문한다.  
   예: “이 고객 계약 조건 바꿔도 돼?”

2. A 에이전트가 자기 범위를 확인한다.  
   “이건 내가 답할 수 있는 영역이 아니다. 계약/법무 쪽이다.”

3. A 에이전트가 중앙 라우터에 묻는다.  
   “계약 조건 변경 담당 에이전트 누구야?”

4. 중앙 라우터가 D 에이전트를 찾는다.  
   “계약 검토는 D 에이전트 담당. 단, 금액 1억 이상은 사람 승인 필요.”

5. A 에이전트가 D 에이전트에게 넘긴다.  
   또는 사용자에게 “이건 D 담당이라 연결할게”라고 안내한다.

6. D 에이전트가 답하거나, 필요하면 실제 D 사람에게 에스컬레이션한다.

## 6. 왜 괜찮은 아이디어인가

### 6.1 지식 유지보수가 분산된다

중앙 지식베이스는 관리 책임이 애매하다.  
하지만 개인 에이전트 구조에서는 책임이 분명해진다.

- 영업 지식은 영업 담당자가 관리한다.
- 법무 지식은 법무 담당자가 관리한다.
- 운영 지식은 운영 담당자가 관리한다.

각자가 자기 에이전트만 잘 관리하면 전체 시스템이 살아있게 된다.

### 6.2 조직의 암묵지가 드러난다

회사에서 중요한 정보는 문서보다 이런 식으로 존재한다.

- “이건 김대리가 제일 잘 안다.”
- “이 고객사는 박팀장을 통해야 빠르다.”
- “이 이슈는 기술팀보다 CS팀이 먼저 봐야 한다.”
- “문서에는 없지만 예외 처리가 있다.”

개인 에이전트가 이런 정보를 흡수하면 조직의 암묵지가 구조화된다.

### 6.3 온보딩에 강하다

신입이나 다른 팀 사람이 들어와도 중앙 라우터가 알려줄 수 있다.

- 이 업무는 누구에게 물어봐야 하는지
- 어떤 에이전트와 먼저 대화해야 하는지
- 어떤 기준으로 사람 승인이 필요한지
- 관련 문서는 어디에 있는지

### 6.4 멀티에이전트 협업으로 확장된다

단순 Q&A를 넘어서 여러 에이전트가 함께 일할 수 있다.

예시 요청:

> “이번 고객 제안서 초안 만들어줘.”

중앙 라우터는 다음처럼 나눌 수 있다.

- 영업 에이전트: 고객 니즈 정리
- 기술 에이전트: 구현 가능성 확인
- 재무 에이전트: 가격 조건 검토
- 법무 에이전트: 위험 조항 체크
- PM 에이전트: 일정 산정

마지막에는 결과를 하나로 합친다.

## 7. 주의할 점

### 7.1 책임 범위가 없으면 실패한다

각 에이전트에 단순 페르소나만 넣으면 약하다.  
반드시 다음이 있어야 한다.

- 내가 담당하는 것
- 내가 답하면 안 되는 것
- 내가 참조할 수 있는 자료
- 다른 에이전트에게 넘겨야 하는 조건
- 사람에게 물어봐야 하는 조건
- 답변의 신뢰도 기준

### 7.2 중앙 라우터가 너무 똑똑해지면 병목이 된다

중앙은 답변자가 아니라 연결자다.  
라우팅, 권한 확인, 감사 기록에 집중해야 한다.

### 7.3 권한/보안 설계가 필요하다

인사, 재무, 법무, 고객 정보가 들어오면 민감하다.

반드시 확인해야 할 것:

- 이 사용자가 이 정보를 봐도 되는가?
- 이 에이전트가 이 문서에 접근해도 되는가?
- 이 요청은 사람 승인이 필요한가?
- 답변과 handoff 기록이 남는가?
- 위임 체인을 검증할 수 있는가?

### 7.4 에이전트 품질 편차가 생긴다

어떤 사람은 자기 에이전트를 잘 만들고, 어떤 사람은 대충 만들 수 있다.  
그래서 최소 에이전트 카드 템플릿과 검증 체크리스트가 필요하다.

## 8. 에이전트 카드 초안

```yaml
agent_id: contract_ops_agent
owner: D
team: legal_ops
status: active

summary: 계약 조건, NDA, 표준 조항 검토를 돕는 업무 에이전트

domains:
  - 계약 검토
  - 거래 조건
  - NDA
  - 표준 조항

can_answer:
  - 표준 계약 조항 설명
  - 계약 변경 리스크 1차 분류
  - 법무 검토 필요 여부 판단
  - 과거 유사 케이스 검색

cannot_answer:
  - 최종 법률 자문
  - 소송 가능성 판단
  - 비표준 고위험 계약 승인
  - 외부 발송용 최종 문안 확정

handoff_when:
  - 금액 1억 이상이면 finance_agent와 human_legal_owner에게 넘김
  - 개인정보 처리 조항이 있으면 privacy_agent에게 넘김
  - 해외 법인 계약이면 global_legal_agent에게 넘김
  - 구현 가능성 관련 질문이면 tech_solution_agent에게 넘김

human_approval_required:
  - 금액 1억 이상
  - 면책/손해배상 조항 변경
  - 개인정보 처리 위탁 조항 포함
  - 비표준 계약서 사용

knowledge_sources:
  - docs/contracts/standard_terms.md
  - docs/contracts/nda_policy.md
  - docs/contracts/approval_matrix.md

trust_labels:
  - internal_only
  - needs_source_citation
  - human_review_for_external_answer

last_reviewed_at: 2026-06-20
```

## 9. 중앙 라우터 API 초안

초기 MVP는 단순한 HTTP API로 충분하다.

```http
GET /agents
```

등록된 에이전트 목록을 반환한다.

```http
GET /agents/find?intent=contract_review
```

특정 intent에 맞는 에이전트를 찾는다.

```http
POST /route
```

사용자 질문을 받아 담당 에이전트를 추천한다.

예시 요청:

```json
{
  "user_id": "user_123",
  "question": "이 고객 계약 조건 바꿔도 돼?",
  "context": {
    "customer_tier": "enterprise",
    "amount_krw": 120000000
  }
}
```

예시 응답:

```json
{
  "route_to": "contract_ops_agent",
  "confidence": 0.86,
  "reason": "계약 조건 변경 질문이며 금액이 1억 이상이라 법무/재무 검토가 필요함",
  "required_handoffs": ["finance_agent"],
  "human_approval_required": true,
  "allowed_answer_mode": "draft_only"
}
```

```http
POST /handoff
```

에이전트 간 업무 이관을 기록한다.

```http
POST /audit-log
```

중요 행동과 권한 판단을 감사 로그에 남긴다.

## 10. MVP 범위

처음부터 큰 시스템으로 만들지 않는다.

### 10.1 대상

3~5개 역할로 시작한다.

- 영업
- 기술/솔루션
- 계약/법무
- 재무
- 운영/CS

### 10.2 MVP 기능

1. 에이전트 카드 YAML 등록
2. 중앙 라우터가 intent 기반으로 담당 에이전트 추천
3. `cannot_answer`와 `handoff_when` 규칙 적용
4. 간단한 handoff 로그 저장
5. 사람 승인 필요 여부 표시
6. 라우팅 결과에 이유와 신뢰도 표시

### 10.3 MVP에서 하지 않을 것

- 완전한 권한/인증 시스템
- 복잡한 멀티홉 위임
- 사내 SSO 연동
- 모든 문서 자동 인덱싱
- UI 대시보드
- 완전 자동 실행

초기에는 YAML + CLI/API + 로그 파일 정도로 충분하다.

## 11. GitHub 조사 결과

조사일: 2026-06-20 KST  
조사 방식: GitHub CLI와 GitHub Search를 사용해 `agent registry`, `agent capability registry`, `multi agent handoff`, `A2A agent card`, `agent directory routing` 계열 검색.

결론:

> 비슷한 프로젝트는 있지만, “개인별 업무 에이전트 + 중앙 담당자 라우터 + 조직 협업/권한 구조”를 완성형으로 밀고 있는 대표 오픈소스는 아직 뚜렷하지 않다. 조각은 이미 존재한다.

### 11.1 가장 가까운 프로젝트

#### clagentic/clagentic-directory

URL: https://github.com/clagentic/clagentic-directory  
상태: 2026-06-17 업데이트, Go, stars 0 기준 확인  
설명:

> A self-hosted agent capability registry. Declare what your agents can do — intents, conversation kinds, trust labels, sequencing — and answer routing queries over HTTP. Backed by local YAML files or a git repository.

Sung 아이디어와 가장 직접적으로 가깝다.

닿는 부분:

- 중앙 capability registry
- 에이전트가 뭘 할 수 있는지 선언
- intent 기반 라우팅 질의
- trust label, sequencing 개념
- YAML 또는 git repo 기반

차이:

- 아직 초기 프로젝트로 보인다.
- 조직 구성원의 개인 업무 에이전트/업무 페르소나/사람 승인 흐름까지 제품 철학으로 드러나지는 않는다.

#### a2aproject/A2A

URL: https://github.com/a2aproject/A2A  
확인 시점 기준 약 24k stars  
설명:

> Agent2Agent (A2A) is an open protocol enabling communication and interoperability between opaque agentic applications.

README에서 확인한 핵심:

- JSON-RPC 2.0 over HTTP(S)
- Agent Discovery via Agent Cards
- Agent Cards가 capabilities와 connection info를 담음
- synchronous, streaming, async push notification 지원
- security, authentication, observability 고려

Sung 아이디어에서의 의미:

- 개인 에이전트가 자기 “명함”을 제공하는 표준으로 쓸 수 있다.
- 중앙 라우터가 Agent Card를 읽고 담당자를 찾는 구조와 맞다.

차이:

- A2A는 프로토콜이다.
- 조직용 중앙 라우터/업무 책임 그래프 자체를 제공하는 제품은 아니다.

#### a2aproject/a2a-python

URL: https://github.com/a2aproject/a2a-python  
확인 시점 기준 약 2k stars  
설명:

> Official Python SDK for the Agent2Agent (A2A) Protocol

MVP에서 개인 에이전트 서버나 A2A 호환 실험을 만들 때 후보가 된다.

#### AnuragVikramSingh/multi-agent-registry-kit

URL: https://github.com/AnuragVikramSingh/multi-agent-registry-kit  
상태: 2025-05-13 업데이트, Python, stars 0 기준 확인  
설명:

> The Registry Pattern for AI Agents: Build, Connect, and Route Multi-Agent Systems with Ease.

README에서 확인한 내용:

- Router Agent for intelligent request routing
- Registry Pattern for agent discovery and plug-in architecture
- composable design
- LangChain / LangGraph 또는 standalone 지원
- specialized agents로 query routing

닿는 부분:

- 중앙 라우터
- 전문 에이전트 연결
- registry pattern

차이:

- 조직의 사람별 업무 에이전트/권한/책임 구조까지는 아님.
- 업데이트와 성숙도는 낮아 보인다.

#### yanksyoon/superpilot

URL: https://github.com/yanksyoon/superpilot  
상태: 2026-05-13 업데이트, TypeScript, stars 1 기준 확인  
설명:

> Multi-agent registry built on @github/copilot-sdk — single-process and distributed modes

README에서 확인한 내용:

- multiple named agents
- 각 agent가 responsibility를 가짐
- `send_to_agent` 도구로 다른 에이전트에게 메시지 전달
- LLM-driven delegation
- peer awareness
- per-agent FIFO queue

닿는 부분:

- A가 모르면 B에게 넘기는 handoff 구조와 비슷하다.
- 각 에이전트가 책임을 갖는 구조가 있다.

차이:

- GitHub Copilot SDK 기반 실험 성격이 강하다.
- 조직 지식망/업무 담당자 라우터보다는 named agent coordination에 가깝다.

#### invincible-jha/agent-marketplace

URL: https://github.com/invincible-jha/agent-marketplace  
상태: 2026-03-01 업데이트, Python, stars 0 기준 확인  
설명:

> Agent capability registry, discovery, and semantic matching

닿는 부분:

- capability registry
- discovery
- semantic matching

차이:

- 조직용 개인 업무 에이전트 네트워크보다는 에이전트 마켓플레이스/매칭 계열로 보인다.

#### Retsumdk/agent-capability-registry

URL: https://github.com/Retsumdk/agent-capability-registry  
상태: 2026-05-07 업데이트, TypeScript, stars 0 기준 확인  
설명:

> Dynamic registry for agent capabilities with version tracking, compatibility checking, and automatic capability discovery

닿는 부분:

- capability registry
- version tracking
- compatibility checking
- automatic discovery

차이:

- README가 짧고 성숙도 검증이 어렵다.

#### ibmlachezar/multi-agent-patterns

URL: https://github.com/ibmlachezar/multi-agent-patterns  
상태: 2026-05-27 업데이트, Python, stars 1 기준 확인  
설명:

> 5 multi-agent patterns using Google ADK and A2A protocol — Agent Card Discovery, Delegated Specialization, MCP Tool Bridge, Cross-Org Federation, and Ambient Event Mesh.

닿는 부분:

- Agent Card Discovery
- Delegated Specialization
- Cross-Org Federation

차이:

- 제품이라기보다 학습/패턴 모음에 가깝다.

### 11.2 큰 프레임워크

#### LangGraph

URL: https://github.com/langchain-ai/langgraph  
확인 시점 기준 약 35k stars  
설명: Build resilient agents.

쓸 수 있는 부분:

- multi-agent handoff
- supervisor/router pattern
- workflow graph
- stateful agent orchestration

한계:

- 조직 구성원별 개인 에이전트 디렉토리 제품은 아니다.

#### Microsoft AutoGen

URL: https://github.com/microsoft/autogen  
확인 시점 기준 약 59k stars  
설명: A programming framework for agentic AI

쓸 수 있는 부분:

- 멀티에이전트 대화
- 팀형 agent collaboration
- group chat / orchestration

한계:

- 담당자/권한/조직 라우팅 구조는 별도로 설계해야 한다.

#### CrewAI

URL: https://github.com/crewAIInc/crewAI  
확인 시점 기준 약 54k stars  
설명: Framework for orchestrating role-playing, autonomous AI agents.

쓸 수 있는 부분:

- 역할 기반 에이전트 구성
- task delegation
- crew 단위 협업

한계:

- 사람별 업무 에이전트 네트워크보다는 role-playing agent orchestration에 가깝다.

#### OpenAI Swarm

URL: https://github.com/openai/swarm  
확인 시점 기준 약 21k stars  
설명: Educational framework exploring ergonomic, lightweight multi-agent orchestration.

쓸 수 있는 부분:

- handoff 개념 이해
- 가벼운 멀티에이전트 실험

한계:

- 교육용/실험용 성격이 강하다.

## 12. 시장/오픈소스 관점 결론

이미 있는 조각:

- Agent discovery / Agent Card: A2A
- Agent capability registry: clagentic-directory, agent-capability-registry 계열
- Router agent / handoff: LangGraph, AutoGen, CrewAI, Swarm, multi-agent-registry-kit
- Semantic matching: agent-marketplace, vector search/RAG 조합

아직 비어 있는 영역:

> 회사 구성원마다 자기 업무 에이전트를 갖고, 중앙은 사람/업무/권한/담당 영역만 관리하고, 모르는 질문은 담당 에이전트로 자동 연결하는 조직형 에이전트 네트워크.

차별점:

- 중앙 지식봇이 아니다.
- 개인/팀별 에이전트가 자기 전문성과 책임을 가진다.
- 중앙은 지식 저장소가 아니라 책임/권한/라우팅 레이어다.
- 모르면 자동 handoff한다.
- 사람 승인 조건과 감사 기록을 갖는다.

## 13. 기술 아키텍처 초안

초기 실험은 다음 조합이 적절하다.

- Agent Card 개념: A2A 참고
- 중앙 디렉토리: YAML/Git 기반 registry
- 라우팅 엔진: 규칙 기반 + embedding similarity
- handoff 실행: 단순 HTTP 또는 CLI mock
- 감사 로그: append-only JSONL
- UI: 초기에는 생략. CLI/API로 검증

### 13.1 최소 디렉터리 구조

```text
agent-org-network/
  README.md
  docs/
    initial-planning.md
    next-actions.md
  registry/
    agents/
      sales_agent.yaml
      tech_agent.yaml
      legal_agent.yaml
      finance_agent.yaml
      ops_agent.yaml
    routing_rules.yaml
  logs/
    audit.jsonl
    handoffs.jsonl
  src/
    router/
    registry/
    handoff/
```

### 13.2 라우팅 알고리즘 v0

1. 질문을 입력받는다.
2. 질문 intent를 간단히 분류한다.
3. 각 에이전트 카드의 `domains`, `can_answer`, `cannot_answer`, `handoff_when`과 매칭한다.
4. 권한/민감도 규칙을 확인한다.
5. 후보 에이전트와 이유를 반환한다.
6. `human_approval_required` 여부를 표시한다.
7. 결과를 audit log에 남긴다.

v0는 LLM 없이도 작동하게 만든다.  
그 위에 LLM intent classifier와 vector search를 붙인다.

## 14. 보안/신뢰 설계 메모

나중에 실사용으로 가면 다음이 필요하다.

- 에이전트 신원 검증
- 에이전트별 권한 scope
- 사용자별 접근 권한
- 위임 체인 검증
- 만료/폐기 가능한 delegation
- append-only 감사 로그
- 답변 출처와 근거 기록
- 사람 승인 필요 조건

원칙:

- 자기 보고된 신원은 믿지 않는다.
- 자기 보고된 권한도 믿지 않는다.
- 검증 실패 시 허용하지 않는다.
- 증거를 남길 수 없으면 중요한 행동을 실행하지 않는다.

초기 MVP에서는 완전한 암호화 신원까지 가지 않더라도, 스키마와 로그 구조는 나중에 확장 가능하게 잡아야 한다.

## 15. 초기 성공 기준

MVP가 성공하려면 최소한 다음을 보여줘야 한다.

- 5개 에이전트 카드를 등록할 수 있다.
- 임의의 업무 질문 30개를 넣었을 때 담당 에이전트를 납득 가능하게 추천한다.
- 모르는 영역/금지 영역은 답하지 않고 handoff한다.
- 사람 승인 필요 여부를 구분한다.
- 왜 이 에이전트를 골랐는지 설명한다.
- 라우팅/핸드오프 이력이 남는다.
- 새 에이전트 추가가 코드 수정 없이 YAML 추가만으로 가능하다.

## 16. 바로 다음 할 일

1. 샘플 에이전트 5개 정의
2. agent card YAML 스키마 확정
3. 라우터 v0 입력/출력 JSON 확정
4. 샘플 질문 30개 작성
5. 규칙 기반 라우터 프로토타입 작성
6. GitHub에서 찾은 프로젝트 3개를 더 깊게 뜯어보기
   - clagentic-directory
   - A2A
   - superpilot
7. “조직형 개인 에이전트 네트워크”라는 포지셔닝으로 1페이지 PRD 작성

## 17. 현재 대화 원문 요약

### 17.1 Sung의 최초 아이디어

Sung:

> 이런 아이디어 어때??? 각 사람이 자기만의 에이전트를 만들고 거기에 지식을 만들고 내가 뭘 담당하는지를 정리해서 내가하는 업무의 페르소나를 셋팅하고 자료도 넣고 그리고 이것들을 중앙에서는 어떤 에이전트가 뭘 담당하는지만 관리하는거지.. 그리고 a에게 문의가 와.. a가 몰라 그러면 중앙서버에서 누가담당인지 알아서 찾아서 연결해주는 그런 그림이야 여러명이 협업가는한 구조를 만드는거야 개인은 자기 에이전트만 깍으면 되는거고

해석:

- 개인별 에이전트가 자기 업무 지식과 역할을 가진다.
- 중앙은 전체 지식을 소유하지 않고 담당 영역만 관리한다.
- 질문을 받은 에이전트가 모르면 중앙에서 담당 에이전트를 찾아 연결한다.
- 여러 명이 협업 가능한 구조다.
- 개인은 자기 에이전트만 개선하면 된다.

### 17.2 첫 분석 답변의 핵심

응답 요지:

- 이건 단순 회사 지식봇이 아니라 개인별 업무 에이전트 + 중앙 라우팅 브로커 + 권한/신뢰 레이어다.
- 중앙 지식봇은 시간이 지나면 망가지기 쉽다.
- 개인 에이전트 구조는 유지보수 책임이 분산되어 더 현실적이다.
- 중앙 서버는 지식 저장소가 아니라 담당자 디렉토리 역할을 해야 한다.
- 모르는 질문은 담당 에이전트로 handoff해야 한다.
- 성공하려면 페르소나보다 책임 범위, 권한, 감사 기록이 중요하다.

### 17.3 GitHub 조사 요청

Sung:

> 혹시 이런거 만든사람 있나? 깃허브 뒤져봐

조사 결과 요지:

- 비슷한 조각은 있다.
- A2A는 Agent Card와 agent discovery 프로토콜로 중요하다.
- clagentic-directory는 agent capability registry로 가장 직접적으로 가깝다.
- multi-agent-registry-kit, superpilot, agent-marketplace, agent-capability-registry 등 유사 실험이 있다.
- LangGraph, AutoGen, CrewAI, Swarm은 멀티에이전트 프레임워크로 쓸 수 있지만, Sung이 말한 조직형 개인 에이전트 네트워크를 제품 철학으로 제공하는 것은 아니다.

### 17.4 현재 문서 생성 요청

Sung:

> 일단 지금까지 대화내용을 도무 포함해서 초기 기획문서를 새 프로젝트에 저장해줘

처리 내용:

- 새 프로젝트 생성: `/home/sung/project/agent-org-network`
- README 작성
- 현재 문서 작성: `docs/initial-planning.md`
- 다음 작업 문서 작성 예정: `docs/next-actions.md`

## 18. 열린 질문

- 개인 에이전트는 실제로 어디서 실행될 것인가? 로컬, 서버, Slack/Telegram bot, 사내 웹?
- 중앙 라우터는 API 서버인가, MCP 서버인가, CLI인가?
- Agent Card는 A2A 표준을 그대로 따를 것인가, 내부 YAML로 시작할 것인가?
- 개인 에이전트의 자료는 어디에 저장할 것인가? Obsidian, Git, Google Drive, Notion, DB?
- 사람이 자기 에이전트를 쉽게 “깎는” UI는 무엇인가?
- 권한 모델은 사용자 중심인가, 팀 중심인가, 문서 중심인가?
- 답변 책임은 에이전트 owner에게 귀속되는가?
- 실사용 첫 팀은 어디가 적합한가?

## 19. 초기 판단

이 아이디어는 충분히 실험할 가치가 있다.

기존 프로젝트들이 보여주는 방향은 명확하다. Agent Card, registry, handoff, multi-agent orchestration은 이미 등장했다. 하지만 아직 대부분은 기술 프레임워크 중심이다.

Sung 아이디어의 강점은 기술이 아니라 제품 관점에 있다.

> AI 에이전트를 조직도처럼 다루고, 각 개인이 자기 업무 AI를 관리하며, 중앙이 담당자와 권한을 기준으로 연결한다.

이 관점으로 가면 “또 하나의 멀티에이전트 프레임워크”가 아니라 “AI 시대의 업무 담당자 라우터/조직도”가 될 수 있다.
