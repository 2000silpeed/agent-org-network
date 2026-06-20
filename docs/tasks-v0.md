# Agent Org Network — Tasks v0

작성일: 2026-06-20 · rev2(walking skeleton 로드맵) · 근거: [prd-v0.md](prd-v0.md), [trd-v0.md](trd-v0.md). 수직 슬라이스, TDD(red→green→refactor), 한 번에 하나씩.

## Phase 1 — 등록 창구 (쓰기)

- [x] **T0.1** 스캐폴드(uv·pytest·pydantic·ruff·pyright)
- [x] **T1.1** `AgentCard` + `Registry.register/get`
- [x] **T1.2** 참조 무결성(User/Agent 그래프, `validate`)
- [ ] **T1.3** `Registry.load(dir)` YAML 로더 + `validate` CLI

## Phase 2 — 라우팅 코어 (읽기)

- [x] **T2.1** `Classifier` 포트 + `RuleBased` + `Fake`
- [x] **T2.2** 단일 매칭 → `Routed(primary)`
- [x] **T2.3** 0 매칭 → `Unowned(루트 User)` (불변식)
- [x] **T2.4** ≥2 매칭 → `Contested`
- [ ] **T2.5** `Routed`에 Approval·Collaborator 부착

## Phase 3 — Walking skeleton (end-to-end 한 바퀴 보이기)

- [x] **T3.1** `AgentRuntime` 포트 + `StubRuntime`(canned 답)
- [ ] **T3.2** MCP 서버 `ask_org(question, user)` — Router → (Routed면) StubRuntime → `Answer` 반환
- [x] **T3.3** 실 사용자 채팅(웹) 최소 — 질문 → 답(담당·승인·출처) 화면에 표시
  - 데모 조립 팩토리 `demo.py`(`build_demo_ask_org`, 하드코딩 카드 3종·유저 4명) + 웹 어댑터 `web.py`(`POST /ask`·정적 `web/index.html` 서빙) + plain HTML/JS 채팅 UI. 스택 FastAPI+uvicorn. `serialize_reply`가 `Answered/Pending`만 직렬화(내부값 미포함). 실행: `uv run uvicorn agent_org_network.web:app`. (YAML 로더 T1.3·샘플 T6.4 전이라 카드는 인라인)
- [x] **T3.4** append-only 감사 로그(모든 절차 기록)

## Phase 4 — 판례 + 후보 합의

- [ ] **T4.1** Resolution → Precedent 기록 + 라우터 참조(자동 라우팅)
- [ ] **T4.2** Owner 처리함 + 후보 합의(1인칭) — Contested를 합의로 해소 → Precedent

## Phase 5 — 나머지 면

- [ ] **T5.1** 운영 모니터링 로그 + 상세 보기
- [ ] **T5.2** Manager 큐(승인·escalation·합의 실패)
- [ ] **T5.3** Org 그래프 · Agent 빌더

## Phase 6 — 깊게 (실서비스화)

- [ ] **T6.1** `LlmRuntime`(owner `knowledge_sources` RAG) — StubRuntime 교체
- [ ] **T6.2** `LlmClassifier` + 골든셋 eval 러너(정확도 임계값)
- [ ] **T6.3** 분산 전송(각 Agent MCP/A2A 등록·호출, 로컬 PC 도달)
- [ ] **T6.4** 샘플 카드 5개 + 질문 30개 골든셋
