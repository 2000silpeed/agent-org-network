# Agent Org Network — TRD v0

작성일: 2026-06-20 · rev2(end-to-end 비전 반영) · 근거: [CONTEXT.md](../CONTEXT.md), ADR 0001~0007, [prd-v0.md](prd-v0.md)

## 1. 스택

백엔드 Python 3.12 · pydantic v2 · pytest · ruff · pyright(strict). 근거: [ADR 0001](adr/0001-python-stack.md). 테스트는 `.venv`에서 실행. 프론트(웹 UI)는 walking skeleton에서 **FastAPI+uvicorn 웹 어댑터 + 빌드 없는 순수 HTML/CSS/fetch**로 확정(T3.3). 백엔드 코어와 분리 — 어댑터는 `serialize_reply`로 `Answered/Pending`만 직렬화하고 라우팅 내부값은 노출하지 않는다.

## 2. 아키텍처 개요

```
사용자 ─(MCP 클라이언트 / 웹챗)─▶ 중앙 MCP 서버 ask_org ─▶ Router
                                                      ├─▶ Agent Runtime(담당 카드 구동) ─▶ 답
                                                      └─▶ Contested/Unowned → 사람(처리함/큐)
   답(담당·승인·출처) ◀──────────────────────────────────┘
운영·빌더·처리함·큐 화면 = 같은 백엔드 공유 · 모든 절차는 append-only 감사 로그에 기록
```

중앙은 지식을 소유하지 않고 *연결·호출·기록*만 한다(ADR 0006). 답은 Agent Runtime이 한다(ADR 0007).

## 3. 바운디드 컨텍스트

단일 컨텍스트 **Routing**. 모듈: `registry / router / runtime / conflict / audit / server(mcp) / web(api)`.

## 4. 도메인 모델 · 포트

- **User** — 사람 노드. `id` · `manager: UserId | None`. Agent를 owns, 다른 User를 manages. (ADR 0005)
- **AgentCard** — `frozen` 값 객체, 자기보고 필드만(ADR 0004): `agent_id` · `owner: UserId` · `maintainer: UserId | None` · `team` · `summary` · `domains` · `can_answer` · `cannot_answer` · `approval_when` · `collaborate_when` · `knowledge_sources` · `trust_labels` · `last_reviewed_at`
- **Registry** — User·Agent 등록 + admission 불변식. `register / register_user / get / load(dir) / validate`.
- **Classifier 포트** — `classify(question) -> intent`. `RuleBased`(v0) · `Llm`(후순위) · `Fake`(테스트).
- **RoutingDecision** — sealed sum `Routed | Contested | Unowned`. 타입이 곧 상태.
- **Agent Runtime 포트** — `answer(question, card) -> Answer`. `Answer(text, sources[], mode)`. 구현: `StubRuntime`(canned, 스켈레톤·테스트) → `LlmRuntime`(owner `knowledge_sources` RAG). *분류기와 같은 포트 패턴.*
- **Manager** — 다른 User를 `manages` 하는 User. Escalation은 사람 그래프를 타고 오른다.
- **Resolution / Precedent** — 합의 결론과 append-only 기록. 라우터가 참조.
- **Audit log 포트(`AuditLog`)** — `record(entry)`로 한 줄씩 append-only JSONL(`JsonlAuditLog`)·테스트용 `InMemoryAuditLog`. `AuditEntry`는 `RoutingDecision` 원형 + `Answer`를 안아 내부값(confidence·candidates·escalated_to·primary)까지 기록(OrgReply가 감춘 것). timestamp는 주입 clock으로 결정론. 전이가 아니라 기록.

## 5. 진입점 · 전송

- **MCP 서버 `ask_org(question, user)`** — 사용자 클라이언트가 붙는 1급 진입점. Router 호출 → `Routed`면 Agent Runtime 호출 → `Answer` 반환(담당·승인·출처 포함). (ADR 0006)
- **웹 백엔드 API** — 같은 코어를 채팅·운영·빌더·처리함·큐 화면에 제공.
- **분산 전송** — 중앙이 각 Agent를 호출하는 방식. *스켈레톤은 in-process stub.* 실제는 각 Agent가 MCP/A2A 엔드포인트로 중앙에 등록·연결, 중앙이 client로 호출 — 로컬 PC 도달은 후순위.

## 6. 라우팅 알고리즘 v0 (규칙 기반)

1. `classify(question) -> intent`
2. 일치 Precedent 있으면 적용
3. `candidates = intent ∈ domains 이고 cannot_answer 아닌 카드`
4. 0 → `Unowned(루트 User)` / 1 → `Routed` / ≥2 → 중앙 Authority tie-break, 못 풀면 `Contested`
5. `Routed`면 `approval_when`·`collaborate_when` 평가 → Approval·Collaborator 부착, Agent Runtime 호출
6. 모든 절차를 audit log에 기록

## 7. 테스트 전략 (ADR 0003)

- **단위(결정론)** — `FakeClassifier`·`StubRuntime` 주입, 코어 로직. 매 커밋.
- **eval(통계)** — 골든셋(= Precedent + 샘플 질문) 정확도/통과율 임계값. 분류기·런타임 변경 시·야간, 회귀 게이트.

## 8. 프론트 면 (5)

실 사용자 채팅 · Agent 빌더 · Owner 처리함(후보 합의 1인칭) · Manager 큐 · 운영 모니터링(로그·상세·Org 그래프). 모두 같은 백엔드 공유.

## 9. 디렉터리

```text
src/agent_org_network/
  user.py  agent_card.py  registry.py  classifier.py
  router.py  decision.py  runtime.py  conflict.py  audit.py  server.py
registry/agents/*.yaml   registry/routing_rules.yaml
samples/questions.jsonl  logs/audit.jsonl   web/   tests/
```

## 10. 핵심 불변식

- 어떤 질문도 미아로 남지 않는다(0 매칭 → 루트 User).
- 유효하지 않은 카드는 등록되지 않는다.
- 권한(Authority)은 중앙만 선언한다.
- 전이 ≠ 기록 — 전이는 도메인, 기록은 감사 로그.
- 사용자에게 가는 답에는 항상 담당·신뢰 상태(승인/초안/출처)가 붙는다.
