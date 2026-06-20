# Agent Org Network — TRD v0

작성일: 2026-06-20 · 근거: [CONTEXT.md](../CONTEXT.md), ADR 0001/0002/0004, [prd-v0.md](prd-v0.md)

## 1. 스택

Python 3.12 · pydantic v2 · pytest · ruff · pyright(strict). 근거: [ADR 0001](adr/0001-python-stack.md). 테스트는 `.venv` 가상환경에서 실행.

## 2. 바운디드 컨텍스트

단일 컨텍스트 **Routing**. 모듈: `registry / router / conflict / audit`. 같은 언어(Agent Card·Route)를 공유하므로 컨텍스트는 하나, 모듈만 분리.

## 3. 도메인 모델

- **User** — 사람 노드. `id` · `manager: UserId | None`(상위 조직장, 루트는 None). Agent를 owns, 다른 User를 manages.
- **AgentCard** — `frozen` 값 객체. 자기보고 필드만(ADR 0004).
  - `agent_id`(역할 정체성, 불변) · `owner: UserId`(실재 User 참조, 교체 가능) · `maintainer: UserId | None`(없으면 owner) · `team`
  - `summary` · `domains` · `can_answer` · `cannot_answer`
  - `approval_when` · `collaborate_when` · `knowledge_sources` · `trust_labels` · `last_reviewed_at`
- **Registry** — User·Agent 등록 + admission 불변식 강제. `agent_id`·user id 중복은 등록 시점 거부, 참조 무결성(`owner`·`manager`가 실재 User)은 `validate()`가 전체 검사. `register / register_user / get / load(dir) / validate`.
- **Query / Classifier 포트** — `Classifier.classify(question) -> intent`. 구현: `RuleBasedClassifier`(v0), `LlmClassifier`(후순위), `FakeClassifier`(테스트).
- **RoutingDecision** — sealed sum 타입 `Routed | Contested | Unowned`. 타입이 곧 상태.
  - `Routed(primary, collaborators[], approval | None, confidence, reason)`
  - `Contested(candidates[], reason)`
  - `Unowned(escalated_to, reason)`
- **Manager** — 다른 User를 `manages` 하는 User(조직장). Owner와 무관. Escalation은 사람 그래프(Agent→owner→manager→…→루트 User)를 타고 오른다. (ADR 0005)
- **Resolution / Precedent** — 합의 결론과 그 append-only 기록. 라우터가 참조.
- **Audit log** — append-only JSONL. *전이*가 아니라 *기록*을 담당.

## 4. 쓰기 / 읽기 분리

- **쓰기(등록 창구)**: `registry/agents/*.yaml` → git PR → `Registry.load` 검증 + `validate` CLI(CI). PR 리뷰가 Maintainer 편집 권한·이력을 대체.
- **읽기(라우팅)**: `router.route(question) -> RoutingDecision`.

## 5. 라우팅 알고리즘 v0 (규칙 기반, LLM 없이)

1. `classify(question) -> intent` (포트)
2. 일치하는 Precedent 있으면 그대로 적용 *(slice 3)*
3. `candidates = intent ∈ domains 이고 cannot_answer에 안 걸리는 카드`
4. 후보 수: 0 → `Unowned(루트 User)` / 1 → `Routed` / ≥2 → 중앙 Authority로 tie-break, 못 풀면 `Contested`
5. `Routed`면 `approval_when`·`collaborate_when` 평가해 Approval 게이트·Collaborator 부착
6. 결과를 audit log에 기록

## 6. 중앙 규칙

`routing_rules.yaml`: Authority/precedence, 루트 User 지정. **카드가 자기보고할 수 없는 권한만 여기**(ADR 0004).

## 7. 테스트 전략 (ADR 0003 후보)

- **단위(결정론)** — `FakeClassifier` 주입, router/registry 로직. 매 커밋.
- **eval(통계)** — 골든셋(= Precedent + 샘플 질문)에 대한 분류기·end-to-end 정확도 임계값. 분류기 변경/야간, 회귀 게이트.

## 8. 디렉터리

```text
agent-org-network/
  src/agent_org_network/
    user.py         agent_card.py   registry.py   classifier.py
    router.py       decision.py   conflict.py   audit.py
  registry/agents/*.yaml
  registry/routing_rules.yaml
  samples/questions.jsonl
  logs/audit.jsonl
  tests/
```

## 9. 핵심 불변식

- 어떤 질문도 미아로 남지 않는다(0 매칭 → 루트 User).
- 유효하지 않은 카드는 등록되지 않는다.
- 권한(Authority)은 중앙만 선언한다.
- 전이 ≠ 기록 — 전이는 도메인, 기록은 감사 로그.
