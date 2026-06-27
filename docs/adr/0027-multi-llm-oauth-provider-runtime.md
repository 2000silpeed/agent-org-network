# 멀티-LLM OAuth 공급자 API 런타임 — 답 실행을 owner OAuth 인프로세스 스트리밍으로 되돌린다(0010 supersede·0017 결정 2 재정의)

상태: accepted (2026-06-26) · **Phase 9의 ADR-D — 0010 supersede·0017 결정 2 재정의의 본체** · **ADR 0010 supersede**(0010이 0017로 한 번 재정의된 것을 *다시* 재정의 — 헤더에 포인터) · **ADR 0017 결정 2 재정의**(owner측 실행 부활 — 결정 2에 인라인 주석) · ADR 0011·0012 WS 전송을 *기본 대화 경로*로 재부상(0017이 "사설 데이터 옵션"으로 강등한 것을 1급으로) · ADR 0007(AgentRuntime 포트)·ADR 0013(OKF 지식 출처)와 정합 · CONTEXT(Agent Runtime 절 재정의·신규 Provider Runtime 용어)·PRD §3·§5·§6·TRD §2·§4 갱신

## 맥락 — 답 실행 위치를 정직하게 다시 뒤집는다

이 ADR은 되돌리기 어려운 결정을 *다시 뒤집는다*. 정직하게 다뤄야 하므로 역사부터 적는다.

- **ADR 0010 (2026-06-20)**: "답변 주체 = 각 Owner의 Claude Code". 최종 그림을 *각 owner PC의 분산 Claude Code 실행*(T6.3)으로 그렸다. 유효 핵심 = "중앙 API 키 LLM RAG 회피"(`claude -p` 로컬 인증·키/비용/모델 운영 회피).
- **ADR 0017 (2026-06-21)**: 0010의 "owner가 *실행*해야 한다"를 끊었다. "owner가 *관리*한다 ≠ owner가 *실행*한다" — 일곱 거버넌스 능력은 중앙 실행과 양립한다고 논증하고, **답 실행 = 중앙 `claude -p`가 owner OKF 최신 커밋 스냅샷을 cwd로 읽기**(결정 2)로 못박았다. owner측 분산 실행은 *사설 데이터 커넥터 옵션 B*로 강등(0011·0012 재포지셔닝). 0017이 owner측 실행에 든 반대 근거: **가용성**(owner PC는 잠든다)·**신선도**·**UX 악화** + 백업 워커의 자기모순(가용하려면 중앙 호스팅).

**Phase 9는 owner측 실행을 부활시킨다.** 이건 0017이 강하게 반대한 방향이라 그 추론을 가볍게 무시할 수 없다. 그래서 *왜 다시 뒤집는가*·*0017의 반대 근거가 어떻게 흡수되는가*·*무엇이 보존되는가*를 명문화한다.

### 무엇이 보존되는가 (0017의 추론은 역사로 남긴다)

- **0017의 논증을 삭제·왜곡하지 않는다.** 0017 본문은 그대로 두고(0010이 0017 포인터를 단 것처럼) 결정 2에 인라인 재정의 주석만 단다. 0017의 "관리 ≠ 실행" 통찰·일곱 거버넌스 능력 표는 *여전히 유효*하다 — owner 주권은 정의·범위·승인·검토·소유·라이프사이클을 쥠에서 나오지 런타임 호스팅에서 나오지 않는다(ADR-A 세션·ADR-B HITL이 그 거버넌스를 더 강화한다 — 검토·승인·맥락 통제).
- **"중앙 키/토큰 0"은 보존·강화된다.** 0010의 유효 핵심(중앙 API 키 LLM RAG 회피)이 *그대로*다. 자격증명이 **owner측 OAuth 구독 토큰**(API 키 아님·확정 결정 4)이라 중앙은 여전히 모델 키/토큰을 안 든다 — 오히려 더 엄격(중앙 토큰 0 강화). 바뀌는 건 *실행 위치·실행 방식*뿐이지 "중앙 무지식·중앙 키 0"은 깨지 않는다.

### 무엇이 바뀌는가·왜 다시 owner측 실행인가

owner측 실행을 부활시키는 근거 셋(재논쟁 아님 — grill-me 14개 결정으로 사용자가 명시 선택):

1. **사용자의 명시적 제품 비전** — Hermes/opencode 모델: 각 에이전트 전용 채팅 클라이언트가 중앙과만 연결, 답 생성 = owner OAuth 멀티-LLM(claude·codex·gemini 등). 이건 *제품 정의*지 기술 트레이드오프로 뒤집을 사항이 아니다. 멀티 공급자는 *본질상 owner 자격증명*을 요구한다(owner의 claude/codex/gemini 구독).
2. **속도** — `claude -p` 프로세스 스폰은 매 답마다 CLI 프로세스 기동·하네스(Claude Code 전체) 오버헤드를 진다. Phase 9는 **인프로세스 OAuth + 공급자 API 직접 스트리밍**(opencode/Hermes가 codex·claude·antigravity를 OAuth로 붙이는 방식)으로 그 오버헤드를 없앤다 — 토큰을 직접 스트리밍해 대화 응답이 빠르다.
3. **owner OAuth가 곧 owner 자격증명** — 멀티 공급자를 owner OAuth 구독으로 붙이면 owner의 *자기 구독·자기 모델 선택·자기 자격*으로 답한다. 이건 0017이 "지식 출처가 owner"라 한 것을 *실행·자격증명까지* owner로 끌어올린다(제품 비전상 owner 주권의 확장).

### 0017의 반대 근거는 어떻게 흡수되는가

0017이 owner측 실행에 든 반대(가용성·신선도·UX)는 Phase 9 아키텍처에서 *흡수*된다:

- **가용성** — 0017의 핵심 반대였다("owner PC는 잠든다"). Phase 9에서 가용성은 세 층으로 흡수된다: ① **기존 백업 워커**(ADR 0012 — owner 위임 격리 인스턴스가 owner PC 부재 시 답) ② **Manager escalation**(ADR 0014 — 워커도 백업도 부재면 timeout→Manager 큐 종착, 미아 없음) ③ **HITL 토글**(ADR-B — owner 부재 시 자동/검토 정책을 운영자가 조정). 즉 owner측 실행이 *기본*이되 부재는 기존 폴백 사슬(primary→backup→Manager)이 받는다. 0017이 "백업의 자기모순"이라 한 부분("가용하려면 중앙 호스팅")은 Phase 9에선 *모순이 아니라 설계*다 — 백업은 owner 위임 격리 인스턴스로 *의도된* 폴백이고, 기본 가용성은 owner 워커 + 폴백이 함께 떠받친다.
- **신선도** — owner OKF/지식은 owner 환경에 있어 항상 최신(owner가 자기 환경에서 실행하므로 중앙 캐시 staleness 문제가 *기본 경로엔* 없다). 0017이 중앙 실행으로 신선도를 풀었다면, owner 실행은 *지식이 있는 곳에서 답*해 신선도가 자연.
- **UX** — 인프로세스 스트리밍(속도 근거 2)이 `claude -p` 프로세스 스폰보다 빨라 UX가 *개선*된다(0017의 UX 우려가 인프로세스 방식으로 역전).

### ADR 0011·0012 WS 전송의 재부상

0017이 "사설 데이터 옵션 B"로 강등한 분산 WS 전송(owner 워커 ↔ 중앙 아웃바운드 WS·작업 큐)이 Phase 9에서 **기본 대화 경로로 다시 1급**이 된다. owner 워커가 owner OAuth 멀티-LLM으로 답을 만들어 중앙에 회신하는 경로가 *기본*이다(옵션 B 특수 케이스가 아니라). ADR 0011 결정 6(WS 채널)·실패 모드(끊김 re-queue·멱등·heartbeat)·ADR 0012 등급(primary/backup)·ADR 0026 토큰 admission이 그 재부상한 1급 경로를 떠받친다. 이 재포지셔닝을 여기 명문화한다.

설계 제약:

1. **결정론 게이트.** 공급자 어댑터 shape·요청/응답 매핑·스트리밍 조립의 *결정 로직*은 게이트 내(주입 transport Stub). 실 OAuth 흐름·실 공급자 API 스트리밍·실 토큰은 게이트 밖(T9.6 수동).
2. **증분.** 한 공급자부터(권장 Claude). 다중 공급자 동시 지원은 후속 명시 연기. 다만 포트는 공급자 중립(어댑터 N개 자리).
3. **잔존 구분.** 분류기·배치 경로의 `claude -p`는 *잔존 가능* — *대화 답변 경로만* 교체한다(명시 구분).

## 결정

### 1. 공급자별 `AgentRuntime` 어댑터 — 기존 포트의 공급자별 구현

owner OAuth 멀티-LLM을 *기존 `AgentRuntime` 포트의 공급자별 어댑터*로 추상한다 — `StubRuntime`·`ClaudeCodeRuntime`과 **같은 포트**(`answer(question, card) -> Answer`). 포트는 *무변경*이다.

```python
class ClaudeApiRuntime:   # AgentRuntime 구현 — Anthropic API + owner OAuth 구독
    def __init__(self, transport: ProviderTransport, ...):  # 주입 transport(결정론 경계)
        ...
    def answer(self, question: str, card: AgentCard) -> Answer: ...
# 후속: CodexApiRuntime, GeminiApiRuntime(같은 포트·다른 transport)
```

- **공급자 중립** — `AgentRuntime` 포트는 공급자를 모른다(`StubRuntime`·`ClaudeCodeRuntime`과 한 포트). 공급자별 차이(API 모양·OAuth·스트리밍 프로토콜)는 각 어댑터 안에 가둔다. 어떤 공급자도 1급이 아니다(`NotificationChannel`이 채널 중립인 정신).
- **주입 transport로 결정론** — 답 생성에 주입 transport(`ClaudeRunner`가 `_run_claude_headless`를 주입받는 정신)를 쓴다. Stub transport 주입 → 요청/응답 매핑·스트리밍 토큰 조립의 *결정 로직*만 게이트 내(`FakeGitGateway`·`FakeOidcProvider`와 같은 결).

### 2. 인프로세스 OAuth + 공급자 API 직접 스트리밍 (속도 근거)

- **`claude -p` 프로세스 스폰 회피** — 대화 답변 경로는 더 이상 매 답마다 CLI 프로세스를 기동하지 않는다. 인프로세스에서 공급자 API를 직접 호출·토큰 스트리밍한다(opencode/Hermes 방식). 속도·하네스 오버헤드 회피가 핵심 근거.
- **owner OAuth 구독 토큰(API 키 아님)** — 자격증명은 owner측 OAuth 구독 토큰이다(확정 결정 4). 중앙은 모델 토큰을 *0개* 보관(0010 "중앙 키 0" 보존·강화). owner 워커가 owner OAuth 토큰을 쥐고 API를 직접 부른다.

### 3. 요청/응답 매핑 + 스트리밍 조립 = 순수 함수 (SDK/IO 0)

공급자 API 요청 빌드·응답→`Answer` 매핑·스트리밍 토막 조립을 순수 함수로 격리한다(SDK·네트워크 0):

```python
def build_provider_request(question, card, context) -> ProviderRequest: ...  # 순수
def assemble_stream(chunks: Iterable[str]) -> str: ...                        # 순수(스트리밍 토막 조립)
def map_response_to_answer(resp, card) -> Answer: ...                         # 순수(노출 불변식)
```

- `serialize_reply`·`render_mcp_notification`·`assemble_context`(ADR-A)와 **같은 투영 경계** — 매핑이 도메인 값에서만 투영해 내부값·비밀이 안 샌다. 고정 응답 fixture → `Answer` 매핑 결정론 테스트.
- `Answer` 계약 보존 — `text`·`sources`·`mode`·`snapshot_sha`. 매핑이 새 필드를 안 만든다(노출 불변식).
- **`ProviderRequest`는 `model` + `system` + `messages`를 싣는다.** `system`은 *카드 페르소나 투영*(team·owner·summary·domains·can/cannot_answer·knowledge_sources)이고 `messages`는 (옵셔널 맥락 + 질문). `ClaudeCodeRuntime._build_persona_prompt`가 같은 카드 정보를 프롬프트에 싣는 것과 동형이되, Anthropic API가 system을 *top-level 파라미터*로 받으므로 `system`을 messages가 아닌 **별 필드**로 둔다(역할 혼동 방지). `build_provider_request`가 페르소나를 *반드시* 요청에 실어야 한다 — 빌드만 하고 버리면 담당자 맥락 없이 맨 질문만 가 답 품질이 깨진다(T9.4 code-reviewer M1).

### 4. `ClaudeCodeRuntime` 대화 경로 교체 — 분류기·배치 `claude -p`는 잔존

- **대화 답변 경로**만 공급자 어댑터로 교체한다. dispatcher/ask_org 주입 지점만 바꾸고 *라우팅·노출 불변식·미아 없음 회귀 0*(런타임 교체가 종착을 안 바꿈).
- **분류기·배치 경로의 `claude -p`는 잔존 가능**(명시 구분) — `LlmClassifier`(중앙 분류·`classifier.py`)·골든셋 eval·배치는 `claude -p`를 그대로 쓸 수 있다. 교체 대상은 *대화 답변 경로의 `ClaudeCodeRuntime`*뿐이다. 중앙 분류기 LLM 유지 여부는 ✅ 확정: **잔존**(아래 결정 3 — 대화 경로만 교체).

### 5. 한 공급자부터 증분 (권장 Claude)

- **첫 공급자 = Claude**(✅ 확정 — Anthropic 공식 SDK + owner OAuth 프로필 위임·결정 9; 기본 모델 `claude-opus-4-8`·결정 10) — 이미 배선됐고(claude 로컬 인증 입증) 스트리밍을 먼저 검증한다. Claude로 속도·스트리밍·매핑을 입증한 뒤 codex(openai SDK)·gemini(google SDK)를 *같은 포트·다른 transport·자기 SDK·자기 프로필 위임*으로 추가(후속).
- **다중 공급자 동시 지원은 후속 명시 연기**(과도 엔지니어링 회피·tasks line 214) — 지금은 한 공급자. 포트가 공급자 중립이라 추가는 어댑터 N번째일 뿐.

### 6. `AgentRuntime.answer` 포트 옵셔널 진화 — 멀티턴 맥락 스레딩 (T9.4(c)·ADR 0024 결정 3 연장)

T9.1(ADR 0024)이 `assemble_context(session, current_question) -> str`를 *순수 함수로* 닦았으나, 그 산출물이 **아무 데서도 답 생성에 안 흘러간다**(현재 답은 맥락 무관). 멀티턴이 실제로 동작하려면 맥락이 `AgentRuntime`까지 닿아야 하는데, 포트 `answer(question, card) -> Answer`엔 *맥락 자리가 없다*. 포트를 *옵셔널*로 진화시킨다 — 되돌리기 어려운 결정이라 여기 명문화한다.

```python
class AgentRuntime(Protocol):
    def answer(self, question: str, card: AgentCard, context: str | None = None) -> Answer: ...
```

- **옵셔널 진화(하위호환)** — `context: str | None = None`. 미주입이면 *기존 무상태 동작 그대로*다(`notifier=None`·`propagator=None`·`build_provider_request(context=None)`과 동형 — 이미 자리만 둔 파라미터를 *켠다*). 기존 구현·기존 호출처(인자 미전달)는 시그니처상 무변경으로 산다.
- **구현별 소비**:
  - `StubRuntime` — `context`를 *받되 답에 안 싣는다*(canned 답 결정론 보존). 단 **게이트 내 관측 가능성**을 위해 받은 `context`를 마지막 인자로 기록하는 *관측 seam*(예: `last_context` 속성 또는 주입 spy)을 둬, "맥락이 런타임까지 닿았다"를 결정론으로 단언할 수 있게 한다(아래 결정 7·tdd 슬라이스).
  - `ClaudeApiRuntime` — `answer`가 `build_provider_request(question, card, context=context)`로 맥락을 *실제 소비*한다(이미 `context` 자리 존재·미사용 → 배선). 맥락은 `ProviderRequest.messages`의 선행 user 메시지로 들어가고(결정 3 — `messages=옵셔널 맥락+질문`), `system`(카드 페르소나)·라우팅엔 안 섞인다.
  - `ClaudeCodeRuntime` — `context`를 *받되 이번 증분에선 무시*(프롬프트 미주입·기존 동작 보존). `claude -p` 프롬프트에 맥락을 싣는 건 후속(이 ADR이 대화 경로를 `ClaudeApiRuntime`으로 교체하는 방향이라 `ClaudeCodeRuntime` 프롬프트 진화는 우선순위 밖 — 무시가 정직).
- **라우팅 정합(필수 분리)** — **분류기(`Router.route`)는 *맨 질문*만 본다.** 맥락+질문을 분류하면 과거 발화가 현재 의도를 흔들어 *오라우팅*한다("그럼 그 다음은?"이 과거 발화로 엉뚱한 담당에 가는 식). 그래서 맥락은 *런타임에만* 흐르고 `Router.route(question)`엔 절대 안 닿는다 — 이 분리는 **스레딩 경로의 구조가 보증**한다(결정 7): `AskOrg.handle`이 `route`엔 `question`만, `dispatch`에만 `context`를 넘긴다. 두 인자가 서로 다른 함수에 가므로 맥락이 분류에 새는 경로가 *타입·호출 구조상 없다*.

### 7. 스레딩 경로 — `SessionAskOrg`(세션 보유) → `AskOrg.handle(context=)` → `dispatch(context=)` → `runtime.answer(context=)`

맥락은 *세션을 보유한 곳*(`SessionAskOrg`)에서 조립돼 4계층을 관통해 런타임에 닿는다. ADR 0024 결정 5가 그린 와이어링(`context = assemble_context(...) → ask.handle(question, user, context=...)`)을 *실체화*한다.

```
SessionAskOrg.handle(question, user)            # 세션 보유
  session = store.open_or_get(user.id)
  context = assemble_context(session, question)  # 신규: 이전 턴 적재 전 조립(그 사용자 스레드만)
  reply   = ask.handle(question, user, context=context)   # AskOrg.handle 옵셔널 진화
     decision = router.route(question)            # 맥락 미투입 — 라우팅 정합(결정 6)
     ticket   = dispatcher.dispatch(question, card, context=context)  # dispatch 옵셔널 진화
        # 로컬: LocalRuntimeDispatcher → runtime.answer(question, card, context=context)
  store.append_turn(...)                          # 기존(이번 턴 적재 — 다음 턴 맥락이 됨)
```

- **`AskOrg.handle` 옵셔널 진화** — `handle(question, user, *, context: str | None = None)`. T9.1(d)는 "handle 무수정 위임"이었으나(맥락이 아직 안 흘러서), 이제 *맥락이 런타임까지 닿아야* 하므로 진화한다. ADR 0024 결정 5·Consequences가 *이미 예고한* 시그니처(`handle(question, user, *, context=None)`)라 새 결정이 아니라 *예고 실체화*다. 미주입이면 기존 동작(하위호환).
- **`RuntimeDispatcher.dispatch` 옵셔널 진화** — `dispatch(question, card, context: str | None = None)`. 맥락을 런타임까지 나르는 *유일한 추가 인자*. `RuntimeDispatcher` Protocol·`LocalRuntimeDispatcher`·`InMemoryWorkQueueDispatcher`·`WebSocketDispatcher`·`DispatchingRuntime`이 모두 이 옵셔널 인자를 받는다(미전달이면 기존 동작). **로컬 경로(`LocalRuntimeDispatcher`)만 맥락을 *런타임에 즉시 전달***한다(`runtime.answer(question, card, context=context)`) — 그 자리에서 동기 답 생성이라 와이어 직렬화를 안 거친다.
- **분산 WS 경로는 이번 증분에서 맥락을 *나르지 않는다*(후속 명시 연기)** — `InMemoryWorkQueueDispatcher`/`WebSocketDispatcher`는 `context` 인자를 *받되 큐·`WorkTicket`·`TicketFrame`에 싣지 않는다*(시그니처 정합용 흡수). 이유는 결정 8(게이트 경계).
- **전이≠기록 보존** — `append_turn`은 *답이 난 뒤* 그 턴을 적재한다(`SessionAskOrg`의 기존 위치). `assemble_context`는 *적재 전*에 부르므로 맥락 = *과거 턴만*(현재 질문·답 미포함). 현재 질문은 `messages`의 마지막 user 메시지로 호출자가 별도로 붙인다(결정 3·`assemble_context` docstring 정신).

### 8. 게이트 내/밖 경계 — 로컬 경로만 닫고, WS 프로토콜 맥락·실 런타임 교체는 후속 연기

가장 중요한 정직한 분리. 세 갈래로 가른다:

- **로컬 인프로세스 경로(web `/ask` → `SessionAskOrg` → `AskOrg.handle` → `LocalRuntimeDispatcher` → 인프로세스 런타임) = 게이트 내·이번 증분.** 맥락 스레딩을 *게이트 내 결정론*으로 닫는다 — `StubRuntime`이 `context`를 관측하는지(관측 seam) + `ClaudeApiRuntime`이 `build_provider_request(context=)`로 소비하는지(`StubProviderTransport` 주입) + 멀티턴이 로컬에서 관측되는지(턴 N의 맥락에 턴 N-1 발화 포함). **실 LLM·실 네트워크 0 — 전부 결정론.**
- **분산 WS 경로(중앙 → 워커 → 워커의 런타임) = 후속 연기(T9.7).** 맥락이 워커의 런타임까지 닿으려면 `WorkTicket`·`TicketFrame`(와이어 DTO)에 맥락 필드를 *추가*해야 한다 — **프로토콜 진화**다(`transport.py`·`worker.py`의 `handle_push_work`가 `ticket.question`만 받음). 이는 (a) 와이어 포맷 변경(되돌리기 어려움)·(b) *실 워커·실 owner OAuth 런타임*이 게이트 밖(T9.6)이라 *결정론 관측이 빈약*하다. 따라서 **WS 프레임 맥락 전파는 T9.7(owner 클라이언트)로 명시 연기** — 거기서 `ClaudeApiRuntime` 실 transport(T9.6)와 함께 와이어 진화를 한 슬라이스로 묶는다. 이번 증분은 dispatch 시그니처만 옵셔널 진화시켜 WS 디스패처가 *인자를 흡수*(미전파)하게 두고, 와이어 필드 추가는 후속.
- **런타임 교체(`ClaudeCodeRuntime` → `ClaudeApiRuntime`) 판단** — `ClaudeApiRuntime`은 *실 transport가 게이트 밖*(T9.6 — `StubProviderTransport`만 게이트 내)이라, 로컬 대화 경로를 프로덕션에서 실제로 `ClaudeApiRuntime`으로 *교체*하는 것은 T9.6/T9.7과 합류한다. **이번 증분은 "교체"가 아니라 "맥락 소비 능력 배선"**이다 — `ClaudeApiRuntime.answer`가 `context`를 `build_provider_request`로 흘리게 만들어, *주입되면 동작하는* 상태로 둔다(web 기본 런타임 교체는 후속). 즉 포트 진화 + 두 런타임(Stub·ClaudeApi)의 맥락 소비 배선까지가 게이트 내, web `/ask` 기본 런타임을 `ClaudeApiRuntime`으로 바꾸는 와이어링은 T9.6 실 transport와 합류(후속).

## 근거

- **포트 무변경·어댑터 추가** — `AgentRuntime`은 0007부터 포트였다. 0010(중앙 claude -p)·0017(owner OKF 읽는 중앙 claude -p)·0027(owner OAuth 인프로세스)은 *같은 포트의 다른 구현*이다. 포트가 바뀌지 않아 라우팅·dispatcher·노출 경계가 안 흔들린다(헥사고날 — 코어는 포트만 본다).
- **정직한 supersede** — 0017의 반대 근거(가용성·신선도·UX)를 무시하지 않고 *흡수 경로*(백업·escalation·HITL·인프로세스 속도)를 명시했다. "중앙 키 0"은 보존·강화. 역사(0017 논증)는 삭제 없이 인라인 주석으로 보존.
- **속도가 실질 근거** — 인프로세스 스트리밍 vs 프로세스 스폰은 측정 가능한 UX 차이다(0017 UX 우려의 역전).
- **WS 전송 재부상은 인프라 재사용** — 0011·0012의 디스패처·큐·재연결·멱등·등급이 그대로 재사용된다(코드 보존). owner 워커가 *기본 경로*로 돌아올 뿐 새 전송을 안 만든다.

## Consequences

- **공급자 어댑터 모듈(`runtime.py` 확장 또는 신규)** — `ClaudeApiRuntime`(첫 공급자) + 순수 매핑 함수(`build_provider_request`·`assemble_stream`·`map_response_to_answer`) + 주입 `ProviderTransport`(Stub 결정론·실 게이트 밖). 후속 `CodexApiRuntime`·`GeminiApiRuntime` 자리(`NotImplementedError` — `HttpOidcProvider`·`SlackChannel` 정신).
- **`AgentRuntime` 포트 옵셔널 진화(T9.4(c)·결정 6)** — `answer(question, card, context: str|None=None)`. 되돌리기 어려운 포트 변경. 모든 구현(`StubRuntime`·`ClaudeCodeRuntime`·`ClaudeApiRuntime`·`Codex/GeminiApiRuntime` 자리)이 인자를 받는다. `StubRuntime`은 관측 seam으로 받되 답 무변경, `ClaudeApiRuntime`은 `build_provider_request(context=)`로 소비, `ClaudeCodeRuntime`은 이번 증분 무시(후속 프롬프트 진화).
- **`AskOrg.handle`·`RuntimeDispatcher.dispatch` 옵셔널 진화(결정 7)** — `handle(question, user, *, context=None)`(ADR 0024 결정 5 예고 실체화)·`dispatch(question, card, context=None)`. 미주입이면 기존 동작(하위호환). `LocalRuntimeDispatcher`만 맥락을 런타임에 즉시 전달, WS 디스패처는 인자 흡수(미전파·후속 T9.7).
- **게이트 경계(결정 8)** — 로컬 인프로세스 경로 맥락 스레딩은 게이트 내 결정론(`StubRuntime` 관측·`ClaudeApiRuntime` 맥락 소비·멀티턴 로컬 관측). 분산 WS 프레임 맥락 전파(`WorkTicket`·`TicketFrame` 와이어 진화)·web 기본 런타임을 `ClaudeApiRuntime`으로 교체는 **T9.6/T9.7로 명시 연기**.
- **`ClaudeCodeRuntime` 대화 경로 교체** — dispatcher/ask_org 주입을 공급자 어댑터로(분류기·배치 `claude -p` 잔존). 라우팅·노출·미아 없음 회귀 0.
- **WS 전송 1급 재부상** — owner 워커(ADR 0011) ↔ 중앙 아웃바운드 WS가 *기본 대화 경로*. ADR 0026 토큰 admission이 그 연결을 검증. `worker.py`의 `WorkerLogic`이 `ClaudeCodeRuntime` 대신 공급자 어댑터를 쥐고 돈다(T9.7 owner 클라이언트).
- **실 OAuth·실 스트리밍(T9.6·게이트 밖)** — 실 owner OAuth 프로필 재사용(owner CLI 위임·결정 2·9 확정)·실 anthropic SDK 인프로세스 스트리밍(결정 4·9 확정). 새 의존성 `anthropic` SDK는 *첫 공급자 슬라이스에서* `pyproject.toml`에 추가(되돌리기 어려움·결정 9); openai·google SDK는 후속 자리. 기본 모델 `claude-opus-4-8`(결정 10·override 가능).
- **불변식 영향 없음**:
  - **미아 없음** — 런타임 교체가 라우팅 종착을 안 바꾼다(0→Unowned·≥2→Contested·timeout→escalation 그대로). owner 워커 부재는 백업→Manager 폴백 사슬이 받음(ADR 0012·0014).
  - **Authority 중앙** — 런타임은 *답 생성*이지 *권한 선언*이 아니다. 누가 담당인지·누가 owner인지(routing_rules.yaml·card.owner)는 안 건드림. 멀티 공급자는 *답을 만드는 모델 선택*이지 권한이 아님.
  - **노출 불변식** — `Answer` 계약 보존(매핑이 내부값 안 실음). 멀티턴 맥락(ADR-A)은 그 사용자 발화 스레드만(owner 격리). `assemble_context`가 이미 구조적으로 보증(다른 owner 답·다른 사용자 발화 미혼입).
  - **라우팅 정합(맥락이 분류를 안 흔듦, 결정 6·7)** — `Router.route(question)`는 *맨 질문*만 본다. 맥락은 `dispatch(context=)`로만 흐르고 `route`엔 안 닿는다 — 두 인자가 서로 다른 함수에 가므로 맥락이 분류에 새는 경로가 호출 구조상 없다. 멀티턴이 라우팅 종착을 안 바꾼다(미아 없음 보존).
  - **전이 ≠ 기록** — 답 생성은 도메인 행위, audit 기록은 별 축(무변경).
  - **중앙 키/토큰 0(0010 보존·강화)** — 자격증명이 owner OAuth라 중앙은 모델 토큰을 0개 보관. 0010의 유효 핵심이 더 엄격해진다.
  - **owner 격리·등록 무결성** — owner 워커는 ADR 0026 토큰으로 admission(가장 차단), 맥락은 그 사용자 스레드만.
- **갱신 대상**: CONTEXT(Agent Runtime 절 재정의 — "중앙 claude -p 기본" → "owner OAuth 공급자 어댑터 대화 경로·중앙 claude -p는 분류기/배치 잔존", 신규 Provider Runtime 용어, Distributed transport 절 재부상 주석)·PRD §3·§5·§6·TRD §2·§4. **ADR 0010 헤더에 0027 supersede 포인터·ADR 0017 결정 2에 0027 재정의 인라인 주석.**

## 결정 (사용자 확정 2026-06-27)

### 1. 첫 공급자 — **✅ 확정: Claude(Anthropic API + OAuth 구독)**
- **근거**: 이미 배선됨(claude 로컬 인증·`ClaudeCodeRuntime` 입증)·스트리밍 먼저 검증. codex·gemini는 같은 포트·다른 transport로 후속. *인프로세스 스트리밍·어댑터 패턴을 known-good 공급자로 먼저 입증한 뒤 멀티 공급자로 확장*(리스크 최소).

### 2. OAuth 흐름 — owner CLI vs 직접 — **✅ 확정: owner CLI 위임(직접 PKCE 기각)** (2026-06-27)
- **확정**: owner의 **Anthropic OAuth 프로필**(`ant auth login` 또는 *기존 Claude Code 로그인* — 둘이 같은 프로필 resolution 공유)을 워커가 재사용한다. 워커의 `anthropic.Anthropic()`(인자 없이)가 그 프로필을 **자동 해석**한다(API 키 아님·OAuth 구독 토큰). 직접 PKCE 흐름(워커가 자체 code/redirect/refresh 구현)은 **기각** — 무거움·owner 마찰·재발명. 상세는 아래 **결정 9**.
- **공식 경로**(reverse-engineering 아님) — Claude Code/Agent SDK가 쓰는 그 인증. 자격증명 owner측·중앙 토큰 0(이 ADR 핵심 불변식 그대로). 현 분산 워커가 *이미 로컬 claude 구독 인증*을 쓰므로 owner 재설정 0.
- **남은 수동 시연 검증**: Claude Code `/login` 자격과 `ant` 프로필 충돌 가능성(claude-api 스킬이 경고) — T9.6 시연 때 확인.
- **trade-off(역사 기록 — 둘 다 마찰 적었으나 owner CLI 위임이 더 적음)**:
  - **owner CLI 위임**(채택) — 가장 마찰 적음(owner가 추가 OAuth 흐름을 안 거침)·opencode가 codex/claude를 붙이는 방식과 유사·이미 배선됨. 단 CLI/프로필 형식에 종속.
  - **직접 OAuth**(기각) — CLI 비종속·표준 OAuth. 단 워커에 OAuth 흐름·redirect·refresh를 구현(무거움)·owner가 흐름을 한 번 더 거쳐야 함.

### 3. 중앙 분류기 LLM 유지 여부 — **✅ 확정: 잔존**(대화 경로만 교체·결정 1 "Claude 먼저"와 정합)
- **근거**: 분류기(`LlmClassifier`·Haiku)·배치·골든셋 eval의 `claude -p`는 *중앙 운영*이지 owner 대화 답이 아니다. 대화 경로만 교체하면 분류기 잔존(명시 구분·tasks line 271). 확정 결정 1(첫 공급자 Claude·대화 경로만)에서 자연 도출.

### 4. 공급자 API SDK/라이브러리(새 의존성)·스트리밍 프로토콜 — **✅ 확정: 공급자별 공식 SDK + 인프로세스 스트리밍** (2026-06-27)
- **확정**: raw HTTP가 아니라 **각 공급자의 공식 SDK**를 쓴다 — anthropic SDK(Claude)·openai SDK(codex/GPT)·google genai(gemini). 단일 SDK 아님·공급자별. 인프로세스 스트리밍(`client.messages.stream().text_stream` → `Iterable[str]` 청크)이 *기존 `ProviderTransport` Protocol 시그니처를 그대로 만족*(`__call__(request) -> Iterable[str]`). 상세는 아래 **결정 9**.
- **새 의존성(되돌리기 어려움)**: **`anthropic` SDK 먼저**(첫 공급자 Claude·증분). openai·google SDK는 **후속 공급자 슬라이스에서 자리만**. 의존성 추가 시점은 T9.6 실 transport 슬라이스(게이트 밖) — 그때 `pyproject.toml`에 `anthropic` 추가.
- **근거**: claude-api 스킬이 Python 프로젝트는 raw HTTP 아닌 *공식 SDK* 사용을 명시 — 인증·재시도·에러·스트리밍 내장. OAuth 토큰은 `Authorization: Bearer` + `anthropic-beta: oauth-2025-04-20`을 SDK가 처리(워커가 헤더 수작업 0).

### 9. 공급자별 공식 SDK + OAuth 프로필 위임 — 어댑터마다 자기 SDK·자기 자격 위임을 가둔다 (✅ 확정 2026-06-27·되돌리기 어려움)

결정 2·4를 한 어댑터 경계로 묶는 본 결정. **포트는 무변경**(결정 1 — `AgentRuntime`은 공급자를 모름). 공급자별 차이(SDK·OAuth 프로필 해석·스트리밍 프로토콜)는 *각 어댑터 안*에만 산다.

- **공식 SDK(공급자별·결정 4)** — anthropic SDK(Claude)·openai SDK(codex/GPT)·google genai(gemini). raw HTTP 아님·단일 SDK 아님. 어댑터마다 *자기 공급자 SDK*를 가둔다(`ClaudeApiRuntime`→anthropic, `CodexApiRuntime`→openai, `GeminiApiRuntime`→google). 인프로세스 스트리밍이 `ProviderTransport.__call__(request) -> Iterable[str]`를 그대로 만족하므로 **포트·Protocol·게이트 내 매핑 함수 시그니처 무변경**(`build_provider_request`·`assemble_stream`·`map_response_to_answer`·`StubProviderTransport` 그대로).
- **OAuth 프로필 위임(owner CLI 위임·결정 2)** — 워커의 실 transport 구현이 인자 없는 `anthropic.Anthropic()`를 만들면 SDK가 owner의 Anthropic OAuth 프로필을 *자동 해석*한다(`ant auth login` 또는 기존 Claude Code 로그인이 심은 프로필 — 같은 resolution). API 키 환경변수·중앙 토큰 주입 0. 어댑터마다 *자기 공급자의 OAuth/프로필 위임*을 가둔다(공급자별 프로필 형식이 달라도 어댑터 경계가 흡수).
- **현 분산 워커 진화** — 워커는 *이미 로컬 claude 구독 인증*을 쓴다(`ClaudeCodeRuntime`이 `claude -p` 서브프로세스로). T9.6은 답 생성을 `claude -p` 서브프로세스 → *같은 프로필*의 인프로세스 SDK로 바꾸는 것뿐 — owner 재설정 0·프로세스 스폰 회피(속도 근거 2의 실현).
- **되돌리기 어려운 결정 명시** — (a) **새 의존성 `anthropic` SDK**(첫 공급자·증분; openai·google은 후속 자리), (b) **OAuth 위임 방식**(owner CLI 위임·인자 없는 클라이언트 자동 해석에 의존). 둘 다 게이트 밖 T9.6에서 `pyproject.toml`·실 transport에 박는다 — 결정론 게이트로 못 잠그는 외부 의존이라 ADR에 못박는다(`HttpOidcProvider`·`SubprocessGitGateway`가 실 본체를 게이트 밖으로 미룬 정신).
- **게이트 내/밖 경계 재확인** — *게이트 내*(이번까지 그린): 어댑터 shape(`ClaudeApiRuntime`)·순수 매핑 함수·`StubProviderTransport`·`ProviderTransport` Protocol. *게이트 밖*(T9.6 수동): 실 anthropic SDK transport(인자 없는 `Anthropic()` + `messages.stream`)·실 OAuth 프로필 해석·실 네트워크 스트리밍. 실 transport는 *주입만 교체*(Stub→실 SDK)이고 어댑터·매핑·포트는 그대로다 — 게이트 내 조각이 게이트 밖 본체의 *결정 로직 전부*를 이미 닫았다.

### 10. 공급자별 권장 모델 기본값 — 어댑터 안의 설정 가능 기본값 (✅ 확정 2026-06-27)

- **확정**: 답변 모델은 *연결 서비스별 recommended 모델*을 어댑터 기본값으로 둔다. **Anthropic → `claude-opus-4-8`**(adaptive thinking·streaming·claude-api 스킬 기본 권장값). OpenAI·Google은 각 공급자 권장 모델(후속 슬라이스에서 그 어댑터가 결정).
- **설정값(override 가능)** — owner/운영자가 어댑터별로 override할 수 있다. 공급자 중립 포트라 **모델 선택은 각 어댑터 안**에 산다(포트는 모델을 모름). 현재 게이트 내 `build_provider_request`가 `ProviderRequest.model`에 박는 placeholder(`claude-3-5-haiku-20241022`)는 *게이트 내 결정론 매핑용 더미*였다 — 실 기본값(`claude-opus-4-8`)은 실 어댑터/transport 구성에서 적용한다(게이트 밖). 매핑 함수 자체는 모델 문자열에 무관해 게이트 내 테스트 무영향.
- **Authority와 무관** — 모델 선택은 *답을 만드는 모델*이지 *권한 선언*이 아니다(누가 담당·누가 owner는 `routing_rules.yaml`·`card.owner`가 쥠). 멀티 모델/멀티 공급자는 답 생성 축이지 Authority 축이 아니다(불변식 보존).

### 11. 공급자 중립 = 코어는 어떤 공급자 SDK에도 의존하지 않는다 (✅ 확정 2026-06-27·불변식)

**원칙(사용자 지시 2026-06-27)**: "claude에 종속되는 레포를 만들지 않는다 — owner별 구독 서비스가 다를 수 있으니 *전체를 다 받아들이는 구조*." 이는 결정 1("공급자 중립·어떤 공급자도 1급 아님")을 *의존성·패키징 축까지* 끌어올린 **불변식**이다.

- **코어 런타임 의존 = 공급자 SDK 0.** `pyproject.toml`의 `[project.dependencies]`는 어떤 공급자 SDK도 안 든다(`anthropic`·`openai`·`google-genai` 0). `pip install agent-org-network`(코어)는 공급자 SDK를 *안* 끌어온다. → owner가 claude를 안 써도 레포가 claude를 강제하지 않는다.
- **공급자 SDK = 선택 의존성(extra).** `[project.optional-dependencies]`에 공급자별 extra(`claude-api = ["anthropic"]`·후속 `codex = ["openai"]`·`gemini = ["google-genai"]`). **owner는 자기 구독 공급자만 설치**(`pip install agent-org-network[claude-api]`). 게이트/CI는 전 공급자 어댑터를 pyright로 타입검사하려 그 SDK들을 `[dependency-groups].dev`에 둔다 — *런타임 코어 의존이 아니라 검사용*(`uv sync --no-dev`엔 안 들어옴).
- **대칭 레지스트리.** 워커의 `_select_runtime`은 `AON_PROVIDER`(별칭)→공급자 어댑터 lazy 팩토리 *레지스트리*다(`worker.py`). 공급자 SDK는 *그 공급자를 고를 때만* import한다 — 다른 공급자 owner는 미설치 SDK를 안 건드린다. **claude 특권 없음**: 새 공급자(codex·gemini)는 레지스트리에 한 줄 + extra 한 줄이면 대칭으로 붙는다. 알 수 없는 공급자는 *명시 실패*(조용히 claude로 안 떨어짐 — owner 의도 보존).
- **레거시 기본의 위상.** `AON_PROVIDER` 미설정 시 워커 기본은 `ClaudeCodeRuntime`(`claude -p` CLI)인데, 이건 *레거시 기본*이지 코어의 claude **pip 의존**이 아니다(`claude` CLI는 owner가 고르는 런타임 도구). owner는 `AON_PROVIDER`로 자기 공급자를 고른다. (분류기 `LlmClassifier`의 `claude -p`도 *opt-in*이고 기본은 claude 무관한 `RuleBasedClassifier` — 코어는 분류에도 claude를 강제 안 함. 분류기까지 공급자 중립화는 *후속*으로 남긴다.)
- **불변식 보존** — 중앙 키/토큰 0(공급자 SDK가 코어에 없으니 강화)·Authority 중앙·노출 불변식·포트 무변경. 이 결정은 *패키징/배선*을 바꾸지 도메인을 안 바꾼다.
