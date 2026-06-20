# Agent Org Network — 작업 규칙

이 저장소에서 작업할 때 항상 지킨다. **PRD/TRD/TASK가 단일 진실 원천(SSOT)** 이다.

## SSOT 문서

- 제품 요구사항: `docs/prd-v0.md`
- 기술 설계: `docs/trd-v0.md`
- 작업 목록: `docs/tasks-v0.md`
- 도메인 용어집: `CONTEXT.md`
- 아키텍처 결정: `docs/adr/`

## 규칙 1 — 새 요청은 기획 충돌부터 검토한다

새 요청이 들어오면 구현 전에 `docs/prd-v0.md`·`docs/trd-v0.md`와 충돌하는지 먼저 확인한다. 충돌하거나 스코프를 벗어나면 그냥 진행하지 말고 사용자에게 알리고 합의한다. 합의로 방향이 바뀌면 해당 문서를 먼저 갱신한 뒤 구현한다.

## 규칙 2 — 단계가 끝나면 문서를 갱신한다

작업 단계(Task)를 완료할 때마다 `docs/prd-v0.md`·`docs/trd-v0.md`·`docs/tasks-v0.md`를 다시 읽고, 구현으로 바뀐 점(스키마·결정·완료 체크 등)을 반영한다. 도메인 언어가 바뀌면 `CONTEXT.md`를, 되돌리기 어려운 결정은 `docs/adr/`에 ADR로 남긴다.

## 개발 방식

- **DDD**: `CONTEXT.md`의 용어를 코드·테스트·문서에서 그대로 쓴다. 맨 단어 "Agent" 단독 사용 금지(Owner/Agent Card/Agent Runtime로 한정).
- **TDD**: red → green → refactor. 단위 테스트는 결정론적으로(FakeClassifier 주입), LLM 분류 품질은 골든셋 eval로 검증한다.
- 모든 테스트는 `.venv` 가상환경에서 실행한다.

## 핵심 불변식 (깨지면 안 됨)

- 어떤 질문도 미아로 남지 않는다 — 0 매칭이면 루트 User로 Escalation.
- 유효하지 않은 카드는 등록되지 않는다.
- 권한(Authority)은 중앙(`routing_rules.yaml`)만 선언한다 — 카드 자기보고 금지.
- 전이 ≠ 기록 — 전이는 도메인, 기록은 감사 로그.

## 하네스: 개발 에이전트 팀

**목표:** Routing 도메인 구현을 전문 서브에이전트로 분담 — 설계·테스트우선구현·리뷰·MCP/런타임.

**트리거:** 구현 작업은 `.claude/agents/`의 적절한 서브에이전트에 위임한다 — 도메인 모델·타입 설계 → `domain-architect`, 테스트 우선 구현 → `tdd-engineer`, 변경 리뷰 → `code-reviewer`, MCP 서버·런타임·분류기·전송 → `mcp-runtime-engineer`. 단순 질문·자명한 편집은 직접 처리.

**변경 이력:**
| 날짜 | 변경 | 대상 | 사유 |
|------|------|------|------|
| 2026-06-20 | 초기 구성(서브에이전트 4종) | `.claude/agents/` | 빌드 착수 전 팀 셋업 |
