# Agent Org Network — TRD v0

작성일: 2026-06-20 · rev3(분산 전송 §5 구체화) · 근거: [CONTEXT.md](../CONTEXT.md), ADR 0001~0011, [prd-v0.md](prd-v0.md)

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

중앙은 지식을 소유하지 않고 *연결·호출·기록*만 한다(ADR 0006). 답은 Agent Runtime이 한다(ADR 0007). 분산 배치에선 `Agent Runtime(담당 카드 구동)`이 각 Owner PC의 Claude Code이고, 중앙↔owner는 owner 워커가 거는 **아웃바운드 WebSocket**으로 잇는다(중앙=작업 큐 적재→소켓 push, 워커=로컬 claude 답→회신; ADR 0011 결정 6). 논리 호출 방향(질문 중앙→owner)과 물리 연결 방향(소켓 owner→중앙)은 분리.

## 3. 바운디드 컨텍스트

단일 컨텍스트 **Routing**. 모듈(현재): `registry · classifier · router · decision · conflict · runtime · ask_org · audit · demo · web · dispatch`(분산 전송 포트·타입, T6.3) · `transport`(WebSocket 전송층 — 프레임 DTO·`WebSocketDispatcher`, T6.3 슬라이스2b·ADR 0011 결정 6) · `server`(중앙 WS 핸들러 — 워커 아웃바운드 연결 수신 + `central_app` 통합 진입점, T6.3 슬라이스2b-i·2b-ii) · `worker`(owner 워커 프로세스 — 프레임 핸들링 결정론 코어 + 실 아웃바운드 WS·재연결, T6.3 슬라이스2b-ii).

## 4. 도메인 모델 · 포트

- **User** — 사람 노드. `id` · `manager: UserId | None`. Agent를 owns, 다른 User를 manages. (ADR 0005)
- **AgentCard** — `frozen` 값 객체, 자기보고 필드만(ADR 0004): `agent_id` · `owner: UserId` · `maintainer: UserId | None` · `team` · `summary` · `domains` · `can_answer` · `cannot_answer` · `approval_when` · `collaborate_when` · `knowledge_sources` · `trust_labels` · `last_reviewed_at`
- **Registry** — User·Agent 등록 + admission 불변식. `register / register_user / get / load(dir) / validate`.
- **Classifier 포트** — `classify(question) -> intent`. `RuleBased`(v0) · `Llm`(후순위) · `Fake`(테스트).
- **RoutingDecision** — sealed sum `Routed | Contested | Unowned`. 타입이 곧 상태.
- **Agent Runtime 포트** — `answer(question, card) -> Answer`. `Answer(text, sources[], mode)`. 답변 주체는 각 Owner의 Claude Code(중앙 API 키 LLM 아님, ADR 0010). 구현: `StubRuntime`(canned, 스켈레톤·테스트) → `ClaudeCodeRuntime`(`claude -p` 헤드리스, T6.1 임시·중앙 1회성·모든 카드가 로컬 claude로 답) → owner별 분산(T6.3, 아래 `RuntimeDispatcher` 경유). *분류기와 같은 포트 패턴.*
- **RuntimeDispatcher 포트 / WorkTicket / DispatchOutcome** — 분산 전송(T6.3, `dispatch.py`, ADR 0011). owner별 작업 큐에 적재·비동기 회신 수집. `dispatch(question, card) -> WorkTicket`(즉시 추적표) · `poll(ticket) -> DispatchOutcome` · 워커측 `claim(owner_id)`/`submit(ticket_id, answer)`. **`ask_org`의 답 획득 경로** — `ask_org`는 동기 `AgentRuntime.answer`를 직접 부르지 않고 `dispatch→poll`로 `DispatchOutcome`을 얻어 `OrgReply`로 투영(ADR 0011 결정 4). `DispatchOutcome` sealed sum: `Delivered(answer)` / `AwaitingWorker(waited)` / `EscalatedToManager(manager_id, reason)`(timeout·owner 부재 → 기존 Manager escalation 재사용. `manager_id: str|None`은 T5.2 Manager 큐가 기계 소비할 1급 식별자, `reason`은 사람용 자연어 — 둘 분리). `WorkTicket(owner_id·agent_id·question·enqueued_at·ticket_id)` — owner_id 귀속이 신원(ADR 0009)·`Answer.mode`가 Approval 연결점. 구현체: `InMemoryWorkQueueDispatcher`(in-process 큐, 결정론 테스트·슬라이스1) · `LocalRuntimeDispatcher`(동기 런타임을 즉시-Delivered로 감싸는 즉답 다리 — ask_org가 디스패처만 보게 된 뒤 in-process 데모/테스트의 즉답 보장) · 슬라이스2 네트워크 디스패처. 동기 포트 `AgentRuntime.answer`는 어댑터 `DispatchingRuntime`이 디스패처 위에 얹어 보존(레거시/비-ask_org 호환 — ask_org는 거치지 않음). 포트 패턴은 `ConflictCaseStore`·`PrecedentStore`와 동일(Protocol + 구현체). **전이 ≠ 기록** — 작업 큐는 미해소 작업의 도메인 보관소지 절차 로그 아님. (ADR 0011)
- **Manager** — 다른 User를 `manages` 하는 User. Escalation은 사람 그래프를 타고 오른다.
- **Resolution / Precedent** — 합의 결론과 append-only 기록. 라우터가 참조.
- **ConflictCase / ConflictCaseStore 포트** — 미해소 Overlap 다툼의 저장 단위와 그 보관·조회 포트(`AuditLog`·`PrecedentStore`와 같은 패턴, `conflict.py`). `ConflictCase(intent·question·candidates[Candidate(agent_id,owner)]·status·opened_at·case_id·resolution?)`, open→resolved는 `resolve()`가 새 인스턴스. 포트 메서드 `open_case·get·open_for_owner(처리함)·open_for_intent(중복 open 방지)·mark_resolved`. 구현 `InMemoryConflictCaseStore`. **전이 ≠ 기록** — 미해소 도메인 상태 보관이지 절차 로그 아님. (ADR 0008)
- **ConcurOnPrimary / ConsensusOutcome** — 후보 Owner의 1인칭 합의 표(`by_owner→on_agent`, 단일 축)와 합의 시도 결과 sealed sum(`Agreed`→Resolution+Precedent / `StillOpen` / `Deadlocked`). Agreed가 T4.2 핵심, Deadlocked→Manager는 T5.2로 자리만. (ADR 0008)
- **Audit log 포트(`AuditLog`)** — `record(entry)`로 한 줄씩 append-only JSONL(`JsonlAuditLog`)·테스트용 `InMemoryAuditLog`. `AuditEntry`는 `RoutingDecision` 원형 + `DispatchOutcome` 원형(디스패치 결말, Routed만 채움; `answer`는 거기서 유도하는 파생 접근자)을 안아 내부값(confidence·candidates·escalated_to·primary·escalation의 manager_id·reason)까지 기록(OrgReply가 감춘 것). timestamp는 주입 clock으로 결정론. 전이가 아니라 기록. (ADR 0011 결정 5)

## 5. 진입점 · 전송

- **MCP 서버 `ask_org(question, user)`** — 사용자 클라이언트가 붙는 1급 진입점. Router 호출 → `Routed`면 `RuntimeDispatcher.dispatch→poll`로 답 수집 → `DispatchOutcome`을 `OrgReply`로 투영(`Delivered`→`Answered`, `AwaitingWorker`·`EscalatedToManager`→`Pending(kind="dispatched")`). 동기 `AgentRuntime.answer` 직접 호출 아님 — escalation/미회신을 Answer로 위장하지 않고 Pending으로 표면화(ADR 0011 결정 4). in-process 즉답은 `LocalRuntimeDispatcher`가 흡수. (ADR 0006·0011)
- **웹 백엔드 API** — 같은 코어를 채팅·운영·빌더·처리함·큐 화면에 제공. 채팅 `POST /ask`·`GET /`(`serialize_reply`, 내부값 미노출). 처리함 `GET /inbox`(HTML)·`GET /inbox/{owner_id}`(open 케이스 JSON)·`POST /cases/{case_id}/concur`(`ConcurOnPrimary`→`ConsensusOutcome`, `ValueError`→400; `serialize_case`/`serialize_outcome`) — Owner向 운영 화면이라 내부값(후보·intent) 노출(채팅과 다른 면). 채팅·처리함은 한 `DemoBundle`(공유 store)을 봐 합의 성립이 곧 채팅 자동 라우팅에 반영. (T4.2)
- **분산 전송** — 중앙이 각 Owner의 Claude Code를 호출하는 방식(ADR 0010). *스켈레톤은 in-process stub, T6.1 임시는 중앙 단일 `claude -p` 1회성.* 최종(T6.3)은 **owner 워커의 역방향 아웃바운드 연결 + 중앙 작업 큐**(ADR 0011) — owner PC는 서버를 노출하지 않고(NAT/방화벽·고정 IP 없음·상시 가동 X), owner PC의 **Owner Worker**가 중앙에 아웃바운드로 연결(폴링 또는 WS/SSE)해 작업을 가져가 로컬 claude(T6.1 `ClaudeCodeRuntime` 재사용)로 답하고 회신한다. 중앙은 질문을 **owner별 작업 큐(Work Queue)**에 적재하고 회신을 비동기 수집 → 답변 주체가 그 owner 환경(owner별 지식 격리 성립). 논리적 호출 방향(질문 중앙→owner)과 물리적 연결 방향(소켓은 owner→중앙)을 분리. 포트 **`RuntimeDispatcher`**(`dispatch(question,card)->WorkTicket` · `poll(ticket)->DispatchOutcome` · 워커측 `claim`/`submit`, `dispatch.py`) + `InMemoryWorkQueueDispatcher`(결정론 stub)·`LocalRuntimeDispatcher`(즉답 다리). **ask_org는 디스패처를 직접 본다**(슬라이스2 진입 전 확정, ADR 0011 결정 4) — `dispatch→poll`로 `DispatchOutcome`을 얻어 `OrgReply`로 투영(`Delivered`→`Answered`(mode 보존), `AwaitingWorker`·`EscalatedToManager`→`Pending(kind="dispatched")`). escalation/미회신을 동기 Answer로 위장하지 않는다. in-process 데모/테스트는 동기 런타임을 `LocalRuntimeDispatcher`로 감싸 즉답(항상 Delivered)을 받는다. 동기 포트 `AgentRuntime.answer`는 보존하되 어댑터 `DispatchingRuntime`(디스패처→블로킹 poll)은 *비-ask_org 호환 경로*로 남는다(ask_org는 거치지 않음). owner 부재·timeout → `DispatchOutcome.EscalatedToManager`(`manager_id` 1급 = T5.2 Manager 큐 기계 소비, `reason` 사람용)로 기존 Manager escalation 재사용(미아·합의 실패와 같은 처분, 실제 Manager 큐는 T5.2). 신원(워커가 진짜 owner인지, ADR 0009)·Approval 게이트(`Answer.mode` 보존)는 *연결점만* — 실 인증은 T6.5. 실제 네트워크 전송·다른 PC 도달·연결 유지는 in-process 슬라이스 다음의 네트워크 슬라이스로 분리.
- **전송 채널 = WebSocket (슬라이스2b, ADR 0011 결정 6).** 결정 1이 "폴링 또는 WS/SSE"로 열어둔 채널을 **WebSocket**으로 확정 — 실시간 비전(답 토큰 스트리밍·양방향·단일 영속 연결) 기준, long-poll은 그 위층 스트리밍을 못 줘 기각. 연결 방향은 그대로(owner 워커가 중앙에 아웃바운드 WS, 중앙은 `@app.websocket`로 받기만). **WS는 새 큐 도메인이 아니라 `InMemoryWorkQueueDispatcher`를 *합성해 재사용*하는 전송층**(`WebSocketDispatcher`, `transport.py`) — 큐 상태기계·단조 종착·timeout escalation은 합성한 큐가 소유하고(미아 없음·idempotency 1차 보증) WS는 claim/submit을 *전송*으로 중계. `RuntimeDispatcher` 포트 무변경(claim=pull은 "중앙 핸들러가 워커 대신 claim해 `PushWork`로 push"로 의미 보존) → 기존 in-process 구현·143 passed 그대로. **전송 프레임(Transport Frame)**: 워커→중앙 `RegisterWorker`/`SubmitAnswer`/`Heartbeat`/`Ack`, 중앙→워커 `Welcome`/`AuthError`/`PushWork`/`Ping`(pydantic DTO, `type` 판별 봉투). **실패 모드(본체, owner PC 간헐 연결)**: 끊김 시 in-flight `claimed`→`queued` re-queue(`release_claims`, 단조성 보존), 중복 전달은 `ticket_id` 멱등(answered 재submit 무시), heartbeat 생존 판정, 워커 인증 거부 hook(ADR 0009→T6.5). **사용자↔중앙 답 회수 = 조회(pull)로 한정**(푸시 범위 밖): 기존 `poll`을 `AskOrg.retrieve(tracking)` → web `GET /ask/{tracking}`로 재노출, `Pending(dispatched)`에 *불투명 추적 토큰*(`tracking`)을 더해 그것으로 조회(노출 불변식 정밀화 — 추적 ID 1개 OK, 조직 내부 구조 금지). **구현(2b-i)**: 서버(`AskOrg._tracking`)가 `tracking→WorkTicket`을 보관하고 토큰은 `ticket_id`와 분리된 별도 `uuid4().hex`(ticket_id조차 미노출 — 6-5의 "서버가 ticket 보관" 대안). 중앙 WS 핸들러는 `server.py`(`create_worker_app` + `@app.websocket("/worker")`)로 web과 분리. **테스트 경계**: 중앙 WS 핸들러·프로토콜·idempotency·재연결 상태기계·인증 hook는 `TestClient` WebSocket(Fake 워커)로 *결정론*(2b-i 완료, 188 passed), 실 워커 프로세스·실 `claude`·실 네트워크는 *수동 데모*(2b-ii). 구현은 2b-i(중앙 WS+프로토콜, 결정론)·2b-ii(워커 프로세스+실 claude, 수동)로 쪼갠다.

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

실 사용자 채팅 · Agent 빌더 · Owner 처리함(후보 합의 1인칭) · Manager 큐 · 운영 모니터링(로그·상세·Org 그래프). 모두 같은 백엔드 공유 — 단 **페르소나별 분리된 공간**이라 접근 주체는 인증으로 분리(최종 필수). walking skeleton 처리함의 owner 선택 드롭다운은 인증 전 시연 장치다(ADR 0009).

## 9. 디렉터리

```text
src/agent_org_network/
  user.py  agent_card.py  registry.py          # 등록 창구
  classifier.py  decision.py  router.py        # 라우팅 코어
  conflict.py                                  # 판례(Resolution·Precedent·PrecedentStore) + 다툼 케이스(ConflictCase·ConflictCaseStore·ConcurOnPrimary·ConsensusOutcome)
  runtime.py  ask_org.py  audit.py             # 런타임·핸들러·감사
  dispatch.py                                  # 분산 전송 포트·타입(RuntimeDispatcher·WorkTicket·DispatchOutcome·InMemoryWorkQueueDispatcher·release_claims, T6.3·ADR 0011)
  transport.py                                 # WebSocket 전송층(Transport Frame DTO·WebSocketDispatcher=큐 도메인 합성·프레임↔도메인 변환, T6.3 슬라이스2b·ADR 0011 결정 6)
  server.py                                    # 중앙 WS 핸들러(create_worker_app·@app.websocket("/worker") 워커 연결 수신 + create_central_app/central_app: web+워커WS를 한 dispatcher로, 수동 시연 진입점, T6.3 슬라이스2b-i·2b-ii)
  worker.py                                    # owner 워커 프로세스(WorkerLogic=프레임 핸들링 결정론 코어 + run_worker=실 아웃바운드 WS·재연결 + main CLI, T6.3 슬라이스2b-ii·ADR 0011 결정 6)
  demo.py  web.py                              # 데모 조립(cards_for_owner 포함) + 웹 어댑터(POST /ask·GET /ask/{tracking} 회수 조회)
web/index.html  web/inbox.html   logs/audit.jsonl   tests/
# 예정: registry/agents/*.yaml · routing_rules.yaml · samples/questions.jsonl
```

## 10. 핵심 불변식

- 어떤 질문도 미아로 남지 않는다(0 매칭 → 루트 User).
- 유효하지 않은 카드는 등록되지 않는다.
- 권한(Authority)은 중앙만 선언한다.
- 전이 ≠ 기록 — 전이는 도메인, 기록은 감사 로그.
- 사용자에게 가는 답에는 항상 담당·신뢰 상태(승인/초안/출처)가 붙는다.
