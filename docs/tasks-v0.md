# Agent Org Network — Tasks v0

근거: [prd-v0.md](prd-v0.md), [trd-v0.md](trd-v0.md). 수직 슬라이스 단위, TDD(red→green→refactor)로 진행. 한 번에 하나씩.

## Slice 0 — 스캐폴드

- [x] **T0.1** `.venv` + `pyproject.toml`(pytest·pydantic·ruff·pyright) + `src/agent_org_network/` + `tests/` 레이아웃, 빈 pytest 통과 확인

## Slice 1 — 등록 창구 (쓰기)

- [x] **T1.1** (red→green) `AgentCard` 값 객체 + `Registry.register/get` — "유효한 카드 등록 → 조회됨"
- [x] **T1.2** (red→green) 참조 무결성 — "`agent_id`·user id 중복 → 등록 거부, `owner`·`manager`가 실재 User 아니면 `validate()` 실패" *(User/Agent 그래프 — ADR 0005)*
- [ ] **T1.3** `Registry.load(dir)` YAML 로더 + `validate` CLI(CI용)

## Slice 2 — 라우팅 (읽기)

- [ ] **T2.1** `Classifier` 포트 + `RuleBasedClassifier` + `FakeClassifier`
- [ ] **T2.2** (red→green) 테스트 A — "단일 매칭 → `Routed(primary)`"
- [ ] **T2.3** (red→green) 테스트 B(불변식) — "0 매칭 → `Unowned(루트 Manager)`, null 아님"
- [ ] **T2.4** (red→green) "≥2 매칭 + Authority 없음 → `Contested`"
- [ ] **T2.5** (red→green) `Routed`에 Approval 게이트·Collaborator 부착

## Slice 3 — 판례 + 감사

- [ ] **T3.1** append-only JSONL 감사 로그(라우팅 결과 기록)
- [ ] **T3.2** Resolution → Precedent 기록 + 라우터 참조("해소된 질문은 다음에 자동 라우팅")

## Slice 4 — 샘플 + 검증

- [ ] **T4.1** 샘플 카드 5개(영업·기술·법무·재무·운영) + 루트 Manager
- [ ] **T4.2** 질문 30개 골든셋 + eval 러너(정확도 임계값)
- [ ] **T4.3** ADR-0003(테스트 전략) 기록
