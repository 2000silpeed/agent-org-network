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
  - 데모 조립 팩토리 `demo.py`(`build_demo_ask_org`, 하드코딩 카드 3종·유저 4명, cs_ops·finance_ops가 "보상" domain 공유 → "보상" 질문은 Contested 다툼 시연) + 웹 어댑터 `web.py`(`POST /ask`·정적 `web/index.html` 서빙) + plain HTML/JS 채팅 UI. 스택 FastAPI+uvicorn. `serialize_reply`가 `Answered/Pending`만 직렬화(내부값 미포함). 실행: `uv run uvicorn agent_org_network.web:app`. (YAML 로더 T1.3·샘플 T6.4 전이라 카드는 인라인)
- [x] **T3.4** append-only 감사 로그(모든 절차 기록)

## Phase 4 — 판례 + 후보 합의

- [x] **T4.1** Resolution → Precedent 기록 + 라우터 참조(자동 라우팅) — 불변식 방어 보강(M2 빈 intent 우회 차단, M3 stale 판례 폴백, M4 store 주입+판례 미존재 흐름 검증)
- [x] **T4.2** Owner 처리함 + 후보 합의(1인칭) — Contested를 합의로 해소 → Precedent
  - 슬라이스 i: 백엔드 도메인 + ConsensusService.
  - 슬라이스 ii: 웹 와이어링 — `demo.py` `build_demo()`가 공유 `InMemoryPrecedentStore`·`InMemoryConflictCaseStore`로 Router·AskOrg·ConsensusService를 한 상태로 묶은 `DemoBundle` 반환(`build_demo_ask_org`는 `.ask`만 돌려주는 하위호환). `web.py`에 `GET /inbox`(처리함 HTML)·`GET /inbox/{owner_id}`(open 케이스 JSON)·`POST /cases/{case_id}/concur`(`ConcurOnPrimary`→`ConsensusOutcome`, `ValueError`→400) + `serialize_case`/`serialize_outcome`. 처리함은 Owner向 운영 화면이라 내부값(후보·intent) 노출(채팅 OrgReply 불변식과 다른 면). `web/inbox.html` 1인칭 합의 폼. 채팅↔처리함이 한 store를 봐 합의 성립 시 같은 질문이 판례 자동 Routed로 전환.

## Phase 5 — 나머지 면

- [ ] **T5.1** 운영 모니터링 로그 + 상세 보기
- [ ] **T5.2** Manager 큐(승인·escalation·합의 실패)
- [ ] **T5.3** Org 그래프 · Agent 빌더

## Phase 6 — 깊게 (실서비스화)

- [ ] **T6.1** `ClaudeCodeRuntime`(`claude -p` 헤드리스 1회성, 임시·중앙 단일·모든 카드가 로컬 claude로 답) — StubRuntime 대체. 답변 주체 = Owner의 Claude Code(중앙 API 키 LLM 아님, ADR 0010). API 키 불필요·로컬 claude 인증 사용. 한계: owner별 지식 격리 없음(T6.3에서), `knowledge_sources`는 출처 레이블뿐.
- [ ] **T6.2** `LlmClassifier` + 골든셋 eval 러너(정확도 임계값)
  - 선행 주의: 현재 `ask_org`·`router`가 같은 질문을 각자 `classify`(결정론 분류기라 무해). 비결정 LLM 분류 도입 시 두 intent가 갈려 케이스 intent와 라우팅 intent가 어긋날 수 있음 → `RoutingDecision`에 intent를 실어 단일 출처화 선행 검토.
- [ ] **T6.3** 분산 전송 — **owner 워커의 역방향 아웃바운드 연결 + 중앙 작업 큐**(ADR 0011). owner PC는 서버를 노출하지 않고(NAT/방화벽·고정 IP 없음·상시 가동 X), owner PC의 **Owner Worker**가 중앙에 아웃바운드로 연결해 작업을 가져가 로컬 claude(T6.1 `ClaudeCodeRuntime` 재사용)로 답하고 회신. 중앙은 owner별 **Work Queue**에 적재·비동기 수집 → 답변 주체가 그 owner 환경(owner별 지식 격리 성립, ADR 0010). owner 부재·timeout → 기존 Manager escalation 재사용(미아·합의 실패와 같은 처분). `AgentRuntime.answer` 동기 포트는 보존, 호출 대상이 중앙 1회성 → owner별 분산.
  - **설계·shape(완료, 도메인)**: ADR 0011 + `dispatch.py`(`RuntimeDispatcher` Protocol·`WorkTicket`·`DispatchOutcome`=`Delivered`/`AwaitingWorker`/`EscalatedToManager`·`InMemoryWorkQueueDispatcher` stub·동기 어댑터 `DispatchingRuntime` stub). runtime.py·ask_org.py·tests 미변경(타입 shape만 새 모듈). TRD §5·§4·§9·CONTEXT 갱신.
  - [x] **슬라이스 1(in-process, 완료)**: `InMemoryWorkQueueDispatcher`·`DispatchingRuntime` red→green 구현 + Fake Worker(동기 회신). 결정론 테스트 20개(적재→claim→submit→Delivered / 미회신→AwaitingWorker / timeout→EscalatedToManager / owner별 큐 격리 / ticket_id 결정론). `WorkStatus`를 tuple→`Literal`로 수정. `DispatchingRuntime(dispatcher, worker=...)`에 동기 워커 주입. sleep/스레드/실 claude 0. 리뷰 후 보강: escalation·회신 **단조 종착**(한 번 Delivered/Escalated면 고정 — timeout 후 늦은 submit이 작업을 부활시키지 않음, 미아 불변식) + `match`+`assert_never` 망라. 게이트 126 passed, pyright 0, ruff 0.
  - **슬라이스 2(네트워크, 후속)**: owner Worker 별 프로세스 + 중앙 아웃바운드 채널(폴링 vs WS/SSE 택일) + 실 `claude` 회신. 연결 끊김·재연결·중복 전달 실패 모드.
    - **진입 전 도메인 결정(리뷰 발견)**: ① escalation/미회신을 동기 `Answer`로 가리지 말 것 — `ask_org` 비동기화(`Pending(kind="dispatched")`류)로 `DispatchOutcome`→`Pending` 매핑 강제(escalation은 사람에게 — `mode="full"` Answer 위장 금지). ② `EscalatedToManager.reason` 자연어 → `manager_id` 별 필드(T5.2 Manager 큐가 기계 소비).
  - **연결점(지금 X)**: 워커 신원 인증(ADR 0009 → T6.5), Approval 게이트(`Answer.mode` 보존 → T2.5), Manager 큐(EscalatedToManager → T5.2).
- [ ] **T6.4** 샘플 카드 5개 + 질문 30개 골든셋
- [ ] **T6.5** 페르소나별 인증 분리 — 실 사용자/운영 면 분리, Owner는 자기 처리함만, `inbox.html` owner 가장 드롭다운 제거(세션 `owner_id`로 대체). (ADR 0009 — 최종 완료 필수)
