# 다음 작업

이 문서는 `Agent Org Network` 초기 기획 이후 바로 이어서 할 일을 정리한다.

## 1. 샘플 에이전트 카드 만들기

우선 5개 역할로 시작한다.

- `sales_agent.yaml`
- `tech_agent.yaml`
- `legal_agent.yaml`
- `finance_agent.yaml`
- `ops_agent.yaml`

각 파일에는 다음을 넣는다.

- agent_id
- owner
- team
- summary
- domains
- can_answer
- cannot_answer
- handoff_when
- human_approval_required
- knowledge_sources
- trust_labels

## 2. 라우터 v0 스펙 확정

입력:

```json
{
  "user_id": "user_123",
  "question": "이 고객 계약 조건 바꿔도 돼?",
  "context": {}
}
```

출력:

```json
{
  "route_to": "legal_agent",
  "confidence": 0.82,
  "reason": "계약 조건 변경 질문으로 legal_agent의 담당 영역과 일치함",
  "required_handoffs": ["finance_agent"],
  "human_approval_required": true,
  "allowed_answer_mode": "draft_only"
}
```

## 3. 샘플 질문 30개 만들기

영업, 기술, 법무, 재무, 운영 질문을 섞는다.

테스트 기준:

- 담당 에이전트 추천이 납득되는가?
- 금지 영역에서는 답변하지 않는가?
- 사람 승인 필요 여부를 잘 표시하는가?
- handoff 이유가 설명되는가?

## 4. 참고 프로젝트 깊게 보기

우선순위:

1. https://github.com/clagentic/clagentic-directory
2. https://github.com/a2aproject/A2A
3. https://github.com/a2aproject/a2a-python
4. https://github.com/yanksyoon/superpilot
5. https://github.com/AnuragVikramSingh/multi-agent-registry-kit

확인할 것:

- agent card / registry 스키마
- 라우팅 API 구조
- discovery 방식
- handoff 방식
- 권한/신뢰/감사 기록 지원 여부
- 우리 MVP에 가져올 수 있는 부분

## 5. v0 구현 방향

처음에는 LLM 없이 규칙 기반으로 만든다.

- YAML agent card 로드
- question intent 간단 분류
- domains/can_answer/cannot_answer 매칭
- handoff_when 규칙 적용
- route result JSON 출력
- audit log JSONL 저장

그 다음 LLM intent classifier와 embedding search를 붙인다.

## 6. 프로젝트 폴더 다음 구조 제안

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
  samples/
    questions.jsonl
  logs/
    audit.jsonl
    handoffs.jsonl
  src/
    agent_org_network/
      registry.py
      router.py
      handoff.py
      audit.py
  tests/
    test_router.py
```

## 7. 다음 산출물 후보

- `docs/prd-v0.md`: 1페이지 PRD
- `docs/architecture-v0.md`: 초기 아키텍처
- `registry/agents/*.yaml`: 샘플 에이전트 카드
- `samples/questions.jsonl`: 라우팅 테스트 질문
- `src/agent_org_network/router.py`: 규칙 기반 라우터
