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
- [x] **T6.3** 분산 전송 — **owner 워커의 역방향 아웃바운드 연결 + 중앙 작업 큐**(ADR 0011). *(슬라이스1·2a·2b-i·2b-ii 완료 — 실 claude end-to-end 시연 성공 2026-06-21: 질문→dispatched+tracking→cs_lead 워커 로컬 claude→submit→retrieve answered. 연결점(워커 인증 T6.5·Approval T2.5·Manager 큐 T5.2)은 별 태스크.)* owner PC는 서버를 노출하지 않고(NAT/방화벽·고정 IP 없음·상시 가동 X), owner PC의 **Owner Worker**가 중앙에 아웃바운드로 연결해 작업을 가져가 로컬 claude(T6.1 `ClaudeCodeRuntime` 재사용)로 답하고 회신. 중앙은 owner별 **Work Queue**에 적재·비동기 수집 → 답변 주체가 그 owner 환경(owner별 지식 격리 성립, ADR 0010). owner 부재·timeout → 기존 Manager escalation 재사용(미아·합의 실패와 같은 처분). `AgentRuntime.answer` 동기 포트는 보존, 호출 대상이 중앙 1회성 → owner별 분산.
  - **설계·shape(완료, 도메인)**: ADR 0011 + `dispatch.py`(`RuntimeDispatcher` Protocol·`WorkTicket`·`DispatchOutcome`=`Delivered`/`AwaitingWorker`/`EscalatedToManager`·`InMemoryWorkQueueDispatcher` stub·동기 어댑터 `DispatchingRuntime` stub). runtime.py·ask_org.py·tests 미변경(타입 shape만 새 모듈). TRD §5·§4·§9·CONTEXT 갱신.
  - [x] **슬라이스 1(in-process, 완료)**: `InMemoryWorkQueueDispatcher`·`DispatchingRuntime` red→green 구현 + Fake Worker(동기 회신). 결정론 테스트 20개(적재→claim→submit→Delivered / 미회신→AwaitingWorker / timeout→EscalatedToManager / owner별 큐 격리 / ticket_id 결정론). `WorkStatus`를 tuple→`Literal`로 수정. `DispatchingRuntime(dispatcher, worker=...)`에 동기 워커 주입. sleep/스레드/실 claude 0. 리뷰 후 보강: escalation·회신 **단조 종착**(한 번 Delivered/Escalated면 고정 — timeout 후 늦은 submit이 작업을 부활시키지 않음, 미아 불변식) + `match`+`assert_never` 망라. 게이트 126 passed, pyright 0, ruff 0.
  - **슬라이스 2b(네트워크, WebSocket)**: 전송 채널 = **WebSocket**으로 확정(ADR 0011 결정 6 — 실시간 비전·답 토큰 스트리밍·양방향, long-poll 기각). owner 워커가 중앙에 아웃바운드 WS 연결 + 실 `claude` 회신. owner PC 간헐 연결이라 **실패 모드(끊김·재연결·중복 전달)가 본체**.
    - **진입 전 도메인 결정(리뷰 발견 — 확정, 2026-06-21, ADR 0011 결정 4 / domain-architect)**: ① escalation/미회신을 동기 `Answer`로 가리지 말 것 — `ask_org` 비동기화로 `DispatchOutcome`→`OrgReply` 매핑 강제(`Delivered`→`Answered` mode 보존, `AwaitingWorker`·`EscalatedToManager`→`Pending(kind="dispatched")`, 신규 kind는 `dispatched` 하나로 최소화 — 둘은 사용자 관점 동일·내부 구분 감춤). `mode="full"` Answer 위장 금지. ② `EscalatedToManager.reason` 자연어 → `manager_id: str|None` 1급 필드 분리(T5.2 Manager 큐 기계 소비, reason은 사람용 유지). + `AskOrg`는 `runtime: AgentRuntime` 대신 `dispatcher: RuntimeDispatcher` 주입, in-process 즉답은 신규 `LocalRuntimeDispatcher`(동기 runtime→항상 Delivered)가 흡수, `DispatchingRuntime`은 폐기 않고 비-ask_org 호환 어댑터로 재포지셔닝.
      - **shape 완료(도메인, 미구현 stub)**: `dispatch.py`(`EscalatedToManager.manager_id` 추가·`_make_escalated`가 채움 / `LocalRuntimeDispatcher` 시그니처 stub·`DispatchingRuntime` docstring 재포지셔닝) · `ask_org.py`(`Pending.kind`에 `dispatched`·`PendingKind`, `AskOrg(dispatcher=...)`, `_project_outcome` match 골격 stub, `handle` Routed 분기 dispatch→poll) · `demo.py`(`LocalRuntimeDispatcher`로 감쌈). pyright 0·ruff 0·import OK. CONTEXT·TRD §4·§5·ADR 0011 갱신.
      - [x] **tdd-engineer 구현(red→green, 완료 2026-06-21)**: `LocalRuntimeDispatcher` 4메서드(dispatch→runtime.answer→Delivered, poll→항상 Delivered, claim→None, submit→no-op) + `AskOrg._project_outcome` 두 분기(Delivered→Answered mode 보존, AwaitingWorker|EscalatedToManager→Pending(dispatched) manager_id/reason 떨굼). `test_ask_org.py` ask_org_with `runtime=` → `dispatcher=LocalRuntimeDispatcher(StubRuntime())`, `test_audit.py` 동일. 신규 테스트 11개(test_dispatch: EscalatedToManager.manager_id 주입/미주입·LocalRuntimeDispatcher 즉시Delivered/answer동일/mode보존/claim=None/ticket_id상이/history; test_ask_org: AwaitingWorker→dispatched·EscalatedToManager→dispatched·노출불변식). 게이트 137 passed, pyright 0, ruff 0.
    - **2b 진입 전 추가 선결 ①(2a 리뷰 [Major], 2026-06-21) — 해소(설계 확정, ADR 0011 결정 5 / domain-architect)**: escalation(timeout/owner 부재)의 **audit 기록 공백**. `_project_outcome`이 `EscalatedToManager`를 `Pending(dispatched)`로 투영하며 `manager_id`·`reason`을 떨궈, `AuditEntry`(`decision`+`answer`만)엔 escalation 대상이 안 남던 비대칭(`Unowned.escalated_to`와 어긋남)을 메웠다. **해소 방식**: `AuditEntry`에 `dispatch_outcome: DispatchOutcome | None` 1급 추가(라우팅 `decision`과 대칭으로 디스패치 결말을 원형 기록, Routed일 때만·Contested/Unowned는 None) → `EscalatedToManager.manager_id`·`reason`을 audit에 전부 남긴다. `answer`는 중복 회피로 `dispatch_outcome`에서 유도하는 파생 프로퍼티화(SSOT=dispatch_outcome, 하위호환 접근 유지). 직렬화 `_dispatch_record`는 escalation을 `Unowned.escalated_to`와 같은 키 모양(`escalated_to`=manager_id)으로 통일. 노출 불변식 무관(audit는 내부값 기록이 목적, Pending엔 여전히 안 샘) · 전이≠기록 유지.
      - **shape 완료(도메인, 미구현 stub 없음 — 시그니처+직렬화 확정)**: `audit.py`(`AuditEntry.dispatch_outcome` 필드·`answer` @property·`_dispatch_record` match 직렬화, `dispatch` import) · `ask_org.py`(`_project_outcome` 반환을 `OrgReply`로 단순화·`handle`이 `dispatch_outcome=outcome`를 AuditEntry로 전달). 기존 test_audit·test_ask_org 그린 보존(137 passed) · pyright 0 · ruff 0. CONTEXT(AuditEntry·Audit log) · ADR 0011 결정 5 갱신.
      - [x] **tdd-engineer 구현(red→green, 완료 2026-06-21)**: 신규 테스트 6개(escalation→`dispatch_outcome` 보존·JSONL escalation 직렬화·Unowned `escalated_to` 통일·Delivered `answer` 파생 하위호환·Contested/Unowned `dispatch=None`·AwaitingWorker `waited_seconds`). 구현 버그 0. 게이트 143 passed, pyright 0, ruff 0. 리뷰(code-reviewer): Blocker/Major 0, Minor 2(Delivered `ticket_id` 생략·AwaitingWorker poll 스냅샷) → ADR 0011 결정 5에 의도된 경계로 박제.
    - **2b 본체 설계(완료, 도메인·shape — domain-architect, 2026-06-21, ADR 0011 결정 6)**: 전송 채널=**WebSocket** 확정(long-poll 기각 근거·트레이드오프 명문화). WS는 새 큐 도메인이 아니라 `InMemoryWorkQueueDispatcher`를 **합성해 재사용**하는 전송층(`WebSocketDispatcher`) — `RuntimeDispatcher` 포트 무변경(claim=pull은 "중앙 핸들러가 워커 대신 claim해 push"로 의미 보존). **전송 프레임(Transport Frame)** pydantic DTO 7종(`RegisterWorker`·`SubmitAnswer`·`Heartbeat`·`Ack`·`Welcome`/`AuthError`·`PushWork`·`Ping`). 실패 모드: 끊김 시 `release_claims`(claimed→queued re-queue) + `submit`의 answered 멱등 보강 + heartbeat 생존 판정 + 인증 거부 hook. 사용자 답 회수=조회(pull)로 한정(`poll` 재노출·`Pending(dispatched)`에 불투명 추적 토큰). **shape 완료(미구현 stub)**: `transport.py`(프레임 DTO 확정 + `WebSocketDispatcher` 합성 그릇·메서드 stub + 프레임↔도메인 변환 stub) · `dispatch.py`(`submit` answered 멱등 보강[동작, 단조성 강화] + `release_claims` stub). 게이트 **143 passed**, pyright 0, ruff 0(shape이 그린 유지). ADR 0011 결정 6 · TRD §2·§3·§5·§9 · CONTEXT(Owner Worker·RuntimeDispatcher·WebSocketDispatcher·Transport Frame·OrgReply·Pending) 갱신.
    - [x] **슬라이스 2b-i (중앙 WS + 프로토콜 + 실패 모드, 전부 결정론, 완료 2026-06-21)** — mcp-runtime-engineer:
      - [x] `transport.py` 동작 구현(red→green): 프레임↔도메인 변환 4종(`to/from_ticket_frame`·`to/from_answer_frame`) · `WebSocketDispatcher`(dispatch→큐 위임+연결 시 push, poll→`_queue.poll` 위임, claim/submit→큐 위임, `register`→인증 hook+레지스트리+재동기 push, `disconnect`→레지스트리 제거+`release_claims`, 내부 헬퍼 `_push_pending`/`_authenticate`).
      - [x] `dispatch.py` `release_claims` 구현(claimed→queued, answered/expired 불변 — 단조성, owner 격리).
      - [x] 중앙 `@app.websocket("/worker")` 핸들러(**신규 `server.py`** — 채팅·처리함 어댑터 web.py와 책임 분리): 워커 연결 수신→`RegisterWorker` 인증→송신/수신 루프(`PushWork` 내보내기·`SubmitAnswer`→`submit`·`Heartbeat`/`Ack` 생존). 끊김 시 `disconnect`로 re-queue. `create_worker_app(dispatcher)`로 디스패처 주입(결정론 테스트가 고정 clock·주입 큐 박은 디스패처 투입). 와이어 직렬화는 `model_dump(mode="json")`(datetime→ISO).
      - [x] 사용자 답 회수 조회 엔드포인트 `GET /ask/{tracking}`(`web.py`) + `AskOrg.retrieve(tracking)`(`ask_org.py`, `poll` 재노출) + `Pending`에 불투명 `tracking` 필드. **방침**: 서버(`AskOrg._tracking`)가 `tracking→WorkTicket` 매핑을 보관하고, 사용자엔 ticket_id와 *분리된* 별도 uuid4 hex 토큰만 노출(ticket_id조차 미노출 — ADR 6-5의 "서버가 ticket 보관" 대안 채택). `_LEAKY_KEYS`는 무변경(tracking은 불투명 ID 1개라 leaky 아님), tracking이 ticket_id·owner를 인코딩하지 않음을 테스트로 강제.
      - [x] **결정론 테스트(`TestClient` WebSocket, Fake 워커)**: `tests/test_transport.py`(변환·디스패처 합성 19) · `tests/test_server.py`(WS 핸들러 9) · `tests/test_dispatch.py`(release_claims +5) · `tests/test_ask_org.py`(retrieve·tracking +6) · `tests/test_web.py`(회수·노출 불변식 +8). 등록→push→submit→Delivered · 미연결 큐 대기 · 끊김→re-queue→재연결 재push · 중복 submit 멱등(첫 답 고정) · 인증 거부 · 회수 조회(미회신→pending, 회신 후→answered). 실 claude·실 네트워크·실 프로세스 **0**.
      - [x] 게이트 **188 passed**(기존 143 + 신규 45), pyright 0, ruff 0. 새 의존 추가 없음(FastAPI/starlette WebSocket 내장).
    - [x] **슬라이스 2b-ii (owner 워커 프로세스 + 실 claude, 수동 시연, 완료 2026-06-21)** — mcp-runtime-engineer:
      - [x] `worker.py`(파일명 `demo_worker.py` 대신 `worker.py`): **결정론 코어 분리** — `WorkerLogic`(프레임 핸들링: `PushWork`→`from_ticket_frame`로 `WorkTicket` 복원→`agent_id`로 자기 카드 조회→`ClaudeCodeRuntime`(T6.1 재사용, 재구현 X)→`SubmitAnswer` 생성·카드 없으면 폴백 답으로 미아 방지)·`backoff_seconds`(지수 백오프·cap·음수 방어, 순수)·`parse_central_frame`(중앙→워커 프레임 복원, `_parse_worker_frame` 대칭). **실 전송 셸**(게이트 밖) — `run_worker`(실 아웃바운드 WS·`RegisterWorker`→`Welcome`/`AuthError`·수신 루프 `PushWork`→답→`SubmitAnswer`·`Ping`→`Heartbeat`·끊김 시 `backoff_seconds` 재연결)·`main` CLI(`--owner`/`--url`/`--token`, env). 카드 출처: `demo.cards_for_owner`(owner별 `agent_id→AgentCard` — `TicketFrame`은 식별자만 싣고 워커가 카드 복원, 분산 정신상 카드는 owner 환경).
      - [x] **새 의존 1개**: `websockets>=16.0`(WS *클라이언트* — Python 표준에 없음). 선택 근거: 외부 의존성 **0개**(httpx-ws는 anyio·httpcore·httpx·wsproto 4개) + 동기 클라이언트(`websockets.sync.client.connect` — claude subprocess가 동기라 async 불요). 서버(starlette)는 WS 내장이라 무관, 클라이언트만 추가.
      - [x] **결정론 단위 테스트 `tests/test_worker.py`(18개, 실 claude·실 WS 0)**: `handle_push_work`(PushWork→FakeRunner 고정답→SubmitAnswer·ticket_id 일치·question을 claude에 전달·sources/mode 카드 보존·빈 답 폴백)·모르는 agent_id 폴백(미아 방지·claude 미호출)·`register_frame`(owner_id·token)·`backoff_seconds`(지수·cap·음수)·`parse_central_frame`(4프레임 복원·미지/불량/검증실패 None)·SubmitAnswer 와이어 왕복. FakeRunner 주입한 `ClaudeCodeRuntime`으로 실 claude 대역.
      - [x] **end-to-end 통합 진입점 + 데모 스크립트**: `server.create_central_app`/`central_app`(사용자 web 라우트 + `/worker` WS를 *같은 `WebSocketDispatcher` 하나*로 — dispatch한 작업이 워커에 push되고 워커 submit이 사용자 retrieve로 도달). `scripts/run_central.sh`·`scripts/run_worker.sh`·`scripts/demo_e2e.md`(중앙 띄우기→워커 붙이기→`POST /ask`(→pending+tracking)→`GET /ask/{tracking}`(→실 claude 답) 단계별 명령 + 끊김/재연결·timeout escalation 관찰법).
      - [x] **실 전송 스모크 확인**(FakeRunner 워커를 실 WS·별 프로세스로): 질문→pending+tracking→워커가 실 소켓으로 작업 받아 회신→`GET /ask/{tracking}`로 answered(owner·agent_id·sources 보존) 회수 한 바퀴. **미아 없음 실 확인**: 워커 죽인 동안 적재→대기 pending→워커 재기동(재연결)→대기 작업 자동 push→answered 회복.
      - [x] **게이트 밖 수동**(결정·비결정 혼재·느림 — 단위 테스트 아님). 끊김/재연결/중복은 2b-i가 결정론으로 닫았으므로 여기선 실 전송 동작 확인만. 게이트(`uv run pytest`/`pyright`/`ruff`) **206 passed**(188 + 워커 18), pyright 0, ruff 0 — 실 claude·실 WS는 게이트에 없음(`WorkerLogic`/`backoff_seconds`/`parse_central_frame`만 결정론).
  - **연결점(지금 X)**: 워커 신원 인증(ADR 0009 → T6.5 — 2b의 거부 hook에 실 토큰 검증), Approval 게이트(`Answer.mode` 보존 → T2.5), Manager 큐(EscalatedToManager → T5.2).
- [ ] **T6.4** 샘플 카드 5개 + 질문 30개 골든셋
- [ ] **T6.5** 페르소나별 인증 분리 — 실 사용자/운영 면 분리, Owner는 자기 처리함만, `inbox.html` owner 가장 드롭다운 제거(세션 `owner_id`로 대체). (ADR 0009 — 최종 완료 필수)
