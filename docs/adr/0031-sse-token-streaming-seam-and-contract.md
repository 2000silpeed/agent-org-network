# SSE 토큰 스트리밍 — `/ask` 답 점진 렌더의 도메인 seam·이벤트 계약·audit-once 지점

상태: accepted (2026-06-30) · **ADR 0027의 후속(addendum 아님 — 신규 ADR)** · ADR 0027이 "실 스트리밍(T9.6)"을 게이트 밖 후속으로만 명시하고 *seam을 비워둔* 것을 채운다(supersede 아님 — 0027 위에 점진 전달 층을 더하는 refine) · ADR 0011(분산 전송·Pending(dispatched))·ADR 0024(세션·`SessionAskOrg`)·ADR 0012(mode 강제·노출 불변식)·ADR 0007(AgentRuntime 포트)와 정합 · CONTEXT(Provider Runtime·Answer·Agent Runtime 절에 스트리밍 seam 주석)·PRD §3·TRD §4 갱신

## 맥락 — 28초 블로킹을 점진 렌더로

운영 면 프론트엔드(`frontend/`)에서 `/ask`가 실 라우팅 + 실 LLM으로 동작하지만, 답이 ~28초 블로킹돼 "멈춘 듯" 보인다. 추적한 블로킹 경로:

```
POST /ask (web.py:746)
  → SessionAskOrg.handle(question, User(uid))           # 세션·맥락 조립
    → AskOrg.handle(question, user, context=...)        # 라우팅 1회 + audit 1회 (ask_org.py:479)
      → LocalRuntimeDispatcher.dispatch(question, card, context)  # dispatch.py:471
        → self._runtime.answer(question, card, context)  # ★ 블로킹 단일 호출
          → ClaudeCodeRuntime: subprocess.run("claude -p", timeout=120)  # ~28초의 출처
  → serialize_reply(reply)                              # 노출 투영 → 단일 JSON
```

핵심 관찰: **인프로세스 스트리밍 인프라는 *이미 있다*** — `ProviderTransport.__call__(request) -> Iterable[str]`(provider_runtime.py:59)가 청크를 yield하고 `assemble_stream(chunks)`이 조립한다. 그러나 그 청크가 **사용자에게 점진 전달되지 않는다** — `ProviderApiRuntime.answer`가 `assemble_stream`으로 *전부 모은 뒤* 단일 `Answer`를 반환한다(provider_runtime.py:227-232). 토큰은 이미 스트리밍되는데, 우리가 한 곳에서 막아 모은다.

따라서 이 ADR은 **새 LLM 능력을 만들지 않는다** — 이미 흐르는 청크를 코어 포트 무변경으로 *사용자 경계까지 흘려보내는 seam과 SSE 계약·audit-once 지점*을 잠근다.

### 보존해야 할 불변식 (이 ADR이 깨면 안 됨)

- **노출 불변식** — 사용자는 담당(owner/agent_id)·신뢰(mode)·출처(sources)·답(text)만 본다. 라우팅 점수·후보·내부값 0. *스트리밍 meta/done 이벤트도 `serialize_reply`와 같은 투영을 거친다.*
- **전이≠기록(audit-once)** — 감사 로그는 정확히 1회. 스트리밍이 라우팅을 두 번 돌리거나 audit를 두 번 찍으면 안 된다.
- **미아 없음 / Pending 비스트림** — unowned·contested·dispatched(매니저 에스컬레이션·다툼·대기)는 *스트리밍할 답이 없다* → 단일 pending 이벤트 후 종료. 0 매칭이면 루트 User 에스컬레이션 보존.
- **공급자 중립** — claude·codex·gemini 어느 것도 1급 아님. 스트리밍 seam도 공급자 중립.
- **중앙 토큰 0·비소유·owner OAuth** — OKF·답 생성은 owner측. 중앙은 목차만(0027·0010 보존).
- **게이트 결정론** — 단위 테스트는 stub 스트리밍 transport/runner 주입으로 고정 청크 시퀀스 → 결정 가능한 SSE 이벤트열. 실 `claude -p` stdout 스트리밍·실 SDK 스트리밍은 게이트 밖.

## 결정

### 1. 스트리밍 seam = 옵셔널 `answer_stream` 메서드 + `StreamingRuntime` 마커 Protocol — 코어 포트(`AgentRuntime.answer`)는 무변경

코어 포트 `AgentRuntime.answer(question, card, context) -> Answer`(router·MCP·테스트·디스패처가 의존)는 **무변경**으로 둔다. 스트리밍은 *옵셔널 능력 확장*으로 노출한다 — 권장안 **(A)**.

```python
# runtime.py — 기존 포트 무변경
class AgentRuntime(Protocol):
    def answer(self, question: str, card: AgentCard, context: str | None = None) -> Answer: ...

# runtime.py — 신규 *옵셔널* 스트리밍 능력 포트(별 Protocol·런타임이 선택 구현)
class StreamingRuntime(Protocol):
    """토큰 스트리밍을 지원하는 런타임의 *옵셔널* 능력.

    `answer`와 별개의 메서드 — `answer`를 구현한 런타임이 *추가로* 이 메서드를
    구현하면 점진 전달이 가능하고, 안 하면 블로킹 `answer`로 폴백한다.
    """
    def answer_stream(
        self, question: str, card: AgentCard, context: str | None = None
    ) -> Iterator["AnswerChunk"]: ...
```

- **`answer_stream`은 `AnswerChunk`를 yield하고, 마지막에 완성 `Answer`를 확정한다.** 텍스트 델타(`AnswerChunk.text_delta`)를 순서대로 흘리되, 스트림이 끝나면 *조립된 완성 `Answer`*(text·sources·mode·snapshot_sha)를 알 수 있어야 한다(audit·세션 적재·노출 투영이 그 완성 `Answer`를 본다). 모양은 결정 3 참조.
- **폴백 규약(필수)** — 런타임이 `answer_stream`을 구현하지 *않으면*(스트리밍 미지원 런타임 = `StubRuntime`·`ClaudeCodeRuntime`·`GeminiApiRuntime` 자리), 호출 측은 블로킹 `answer`를 부른 뒤 그 단일 `Answer`를 *한 덩어리 델타 + done*으로 투영한다. 즉 **모든 런타임이 SSE를 통해 동작**하되, 스트리밍 지원 런타임만 *여러 델타*를 내고 미지원은 *한 델타*를 낸다(미아 없음·하위호환 보존).
- **capability 감지 = `isinstance(runtime, StreamingRuntime)` (runtime_checkable Protocol)** — `hasattr` 문자열 검사가 아니라 `@runtime_checkable` Protocol로 타입 안전하게 감지한다(pyright 정합). `NotificationChannel`·`GitGateway` 포트 감지 정신.

**왜 (A)인가 — 기각한 대안:**

- **(B) `ProviderTransport.Iterable[str]`를 디스패처/ask_org까지 끌어올림** — 기각. `ProviderTransport`는 *공급자 어댑터 내부 seam*(요청→청크)이라 카드·맥락·노출 투영을 모른다. 이를 ask_org까지 올리면 (a) 공급자 중립 깨짐(transport는 `ProviderApiRuntime`의 사적 구현), (b) 노출 투영(sources·mode·answered_by)이 transport 층에 새어 노출 불변식 흔들림. 청크는 *런타임 경계*(`answer_stream`)에서 나와야 카드 투영을 거친다.
- **(C) `answer` 포트 자체를 `Iterator` 반환으로 진화** — 기각. router·MCP·`LocalRuntimeDispatcher`·`DispatchingRuntime`·전 테스트가 `answer -> Answer`에 의존한다. 포트를 깨면 (a) 비스트리밍 호출처가 전부 깨지고(되돌리기 매우 어려움), (b) Pending 경로(스트리밍 불가)도 Iterator를 강제받아 부자연. 옵셔널 능력이 **하위호환·미아 없음**을 모두 보존한다.

### 2. decide↔answer 분리 — 라우팅 1회로 결정 확정, 그 위에서 답만 스트리밍, audit는 스트림 완료 시 정확히 1회

스트리밍이 **라우팅을 두 번 돌리거나 audit를 두 번 찍으면 안 된다**(audit-once). 그래서 `AskOrg.handle`의 동기 흐름을 *분해*하되 재실행하지 않는다 — 새 메서드 `AskOrg.handle_stream`을 둔다.

```python
# ask_org.py — 신규 스트리밍 핸들(기존 handle은 무변경)
def handle_stream(
    self, question: str, user: User, *, context: str | None = None
) -> Iterator["AskEvent"]: ...
```

분리 명세(결정적 순서):

1. **decide (라우팅 1회)** — `decision = self._router.route(question)`. `handle`과 *동일하게 맨 질문만* 본다(맥락은 dispatch로만, 라우팅 정합 0027 결정 6 보존). 라우팅은 정확히 1회.
2. **분기 (sealed sum match)**:
   - `Contested` / `Unowned` → **스트리밍할 답 없음**. 기존 `handle`의 그 분기 부수효과를 *그대로* 수행하고(ConflictCase open·Manager escalation 적재·push 통지), **단일 `pending` 이벤트**를 yield한 뒤 종료. 즉 Pending은 비스트림(불변식). 부수효과는 한 번만 실행된다.
   - `Routed` → **스트림 가능 분기**. dispatch가 *스트리밍 디스패처*면(결정 4) 델타를 흘리고, 아니면 단일 `answer` → 한 덩어리 델타로 폴백.
3. **answer 스트리밍 (Routed·로컬 경로)** — `LocalStreamingDispatcher.dispatch_stream`(결정 4)이 카드 런타임을 `isinstance(runtime, StreamingRuntime)`로 감지해 델타를 yield. `meta` 이벤트(담당·mode·sources 투영) → `token` 이벤트들(델타) → 스트림 종료 시 *조립된 완성 `Answer`* 확정.
4. **Approval 게이트 적용** — 완성 `Answer`에 `_apply_approval_gate`(기존)를 적용해 `requires_approval`이면 `mode="draft_only"`로 격상(라우팅 결정이 강제·워커 자기보고 아님). 게이트는 *완성 답*에 적용하지 델타마다 적용하지 않는다(mode는 답 전체의 신뢰 상태).
5. **audit-once (스트림 완료 시 정확히 1회)** — **`self._audit.record(...)`는 `handle_stream`이 스트림을 *다 흘린 뒤* `done` 이벤트 직전에 정확히 1회 호출**한다. 기존 `handle`이 끝에서 1회 record하는 위치(ask_org.py:479)와 동형 — *지점만 스트림 종료로 옮긴다*. `dispatch_outcome`은 완성된 `Delivered(answer=...)`(또는 Pending 분기면 None). `handle`과 `handle_stream`은 **상호 배타**(한 질문은 둘 중 하나만 탄다 — web 엔드포인트가 가른다)라 이중 audit 위험이 구조적으로 없다.

**왜 ask_org에 두는가(디스패처 아님):** audit·Approval 게이트·세션 적재는 `AskOrg`/`SessionAskOrg` 책임이다(디스패처는 답 운반만). 스트리밍 변형도 *같은 층에 같은 책임*을 둬야 audit-once·게이트 적용 지점이 한 곳으로 모인다. `LocalStreamingDispatcher`는 *델타 운반*만 하고, audit·게이트·세션 적재 발화는 `AskOrg.handle_stream`/`SessionAskOrg.handle_stream`이 쥔다(기존 동기 경로와 대칭).

**세션 적재(전이≠기록 보존):** `SessionAskOrg.handle_stream`이 스트림을 다 흘린 뒤 *완성 답*으로 `append_turn`한다(기존 `handle`이 답 난 뒤 적재하는 위치와 동형 — `assemble_context`는 적재 전 호출이라 맥락 = 과거 턴만, 0027 결정 7 보존).

### 3. SSE 이벤트 계약 — 5종 이벤트, 노출 투영을 `serialize_reply`와 한 곳에서 공유

`AskEvent`는 sealed sum(타입이 곧 상태). 각 이벤트는 SSE `event:` + `data:`(JSON) 한 프레임으로 직렬화된다.

| 이벤트 | 발생 | 페이로드(노출 투영 후) |
|--------|------|------------------------|
| `meta` | Routed 스트림 시작 시 1회 | `{"answered_by": {"owner", "agent_id"}, "mode": <초기 추정 mode>, "sources": [...]}` |
| `token` | 델타마다 N회 | `{"text": <델타 텍스트>}` |
| `done` | Routed 스트림 종료 시 1회 | `{"mode": <최종 mode>, "sources": [...]}` (Approval 게이트 적용 후 최종 mode) |
| `pending` | Contested/Unowned/dispatched 1회 후 종료 | `{"kind": <PendingKind>, "message": <중립 안내>, "tracking"?: <불투명 토큰>}` |
| `error` | 런타임 실패·timeout 시 1회 후 종료 | `{"message": <중립 오류 안내>}` (내부 예외·스택 0) |

계약 규칙:

- **`meta`의 `mode`는 *초기 추정*, `done`의 `mode`가 *최종 권위*다.** Approval 게이트(`requires_approval`)·backup 하향은 *완성 답*에 적용되므로, `meta`는 런타임 초기 mode(보통 `full`)를, `done`은 게이트 적용 후 확정 mode(`draft_only`/`backup`/`full`)를 싣는다. 프론트는 `done`의 mode로 신뢰 배지를 확정한다. (mode가 답 전체에 붙는 신뢰 상태라 델타에는 안 싣는다.)
- **노출 투영 단일 출처(SSOT)** — `meta`·`done`·`pending`의 페이로드 투영은 **`serialize_reply`와 같은 투영 규칙을 공유**한다. 구현은 `serialize_reply`가 `Answered`/`Pending`을 dict로 투영하는 로직을 *순수 투영 헬퍼*로 추출해 양쪽(블로킹 `/ask`·스트리밍 SSE)이 재사용한다 — 두 경로가 노출 불변식을 *다르게* 흘릴 여지를 구조적으로 제거. `token`은 텍스트 델타만(answered_by·mode·sources 미포함 — 그건 meta/done이 한 번씩만).
- **`error`는 중립 안내만** — 런타임 timeout(`subprocess.TimeoutExpired`)·SDK 예외를 사용자에게 그대로 노출하지 않는다(`ClaudeCodeRuntime.answer`가 이미 예외를 중립 폴백 `Answer`로 감싸는 정신 — 스트리밍에선 `error` 이벤트로 투영). 내부 예외 메시지·스택은 절대 안 싣는다.
- **이벤트 순서 불변** — Routed 성공: `meta` → `token`* → `done`. Pending: `pending` 단독. 실패: (선택 `meta` 후) `error`. 한 스트림에 `done`과 `pending`이 함께 나오지 않는다(상호 배타).
- **`AskEvent` sealed sum + `match`/`assert_never` 망라** — `MetaEvent | TokenEvent | DoneEvent | PendingEvent | ErrorEvent`. SSE 직렬화 함수가 `match`로 망라해 새 이벤트 타입 추가 시 컴파일 강제(`serialize_reply`·`_project_outcome` 정신).

### 4. 스트리밍 디스패처 — `LocalStreamingDispatcher.dispatch_stream`(로컬 경로만), WS 분산 경로는 비스트림 폴백

로컬 인프로세스 경로(web `/ask` 데모 기본)만 스트리밍을 닫는다. 분산 WS 경로는 *비스트림 폴백*(후속).

```python
# dispatch.py — 신규 *옵셔널* 스트리밍 디스패처 능력(LocalRuntimeDispatcher 확장 또는 별 클래스)
class LocalStreamingDispatcher:  # RuntimeDispatcher + 스트리밍 능력
    def dispatch(self, question, card, context=None) -> WorkTicket: ...      # 기존(블로킹 폴백)
    def poll(self, ticket) -> DispatchOutcome: ...                            # 기존
    def dispatch_stream(
        self, question: str, card: AgentCard, context: str | None = None
    ) -> Iterator["AnswerChunk"]: ...  # 신규: 런타임이 StreamingRuntime이면 델타, 아니면 단일 answer 1델타
```

- **`dispatch_stream`은 카드 런타임을 `isinstance(runtime, StreamingRuntime)`로 감지** — 지원하면 `runtime.answer_stream(...)`의 델타를 그대로 흘리고, 미지원이면 `runtime.answer(...)`의 완성 텍스트를 *한 델타*로 yield한다(폴백 규약·결정 1). 어느 쪽이든 스트림 종료 시 *완성 `Answer`*를 확정해 audit·게이트가 본다.
- **로컬 경로만**(`LocalStreamingDispatcher` = `LocalRuntimeDispatcher`의 스트리밍 변형) — 그 자리에서 동기/스트리밍 답 생성이라 와이어 직렬화를 안 거친다(0027 결정 7·8 로컬 경로 정신).
- **분산 WS 경로는 이번 증분에서 비스트림** — `WebSocketDispatcher`/`InMemoryWorkQueueDispatcher`는 `dispatch_stream`을 *구현하지 않는다*. 그쪽으로 라우팅되면 `handle_stream`이 *블로킹 dispatch→poll → 단일 답 → 한 덩어리 델타 + done*으로 폴백한다(또는 미회신이면 `pending(dispatched)`). 와이어 프레임에 델타 스트리밍을 싣는 건 **프로토콜 진화**(`TicketFrame`에 델타 필드 추가)라 0027 결정 8의 "WS 와이어 진화는 후속" 정신으로 **명시 연기**.

### 5. 게이트 내/밖 경계

가장 중요한 정직한 분리.

**게이트 내(결정론·이번 증분):**
- `AnswerChunk`·`AskEvent` sealed sum 값 객체(frozen).
- `StreamingRuntime`·스트리밍 디스패처 Protocol shape.
- SSE 이벤트 *직렬화 순수 함수*(`AskEvent` → SSE 프레임 문자열·`serialize_reply` 공유 투영 헬퍼) — 고정 이벤트열 → 고정 SSE 문자열 결정론.
- `AskOrg.handle_stream`/`SessionAskOrg.handle_stream`의 *오케스트레이션 결정 로직* — **stub 스트리밍 런타임/transport 주입**으로 고정 청크 시퀀스 → 결정 가능한 SSE 이벤트열. 검증: decide 1회·audit 1회(스트림 완료 시)·meta 1회·token N회·done 1회·Pending 비스트림·게이트 적용·노출 불변식(`token`에 내부값 0).
- `StubStreamingRuntime`(고정 델타 시퀀스 yield·`StubProviderTransport` 정신) + `StubRuntime`(스트리밍 미지원→폴백 1델타) 양쪽 결정론.

**게이트 밖(수동 시연·T9.6 합류):**
- 실 `claude -p` stdout 토큰 스트리밍(`subprocess` PIPE 실시간 읽기) — 실 프로세스·비결정.
- 실 공급자 SDK 스트리밍(`client.messages.stream().text_stream`)이 `answer_stream` 델타로 실 흐름 — 실 네트워크·실 OAuth.
- 실 FastAPI `StreamingResponse`(또는 `EventSourceResponse`)의 브라우저 SSE 푸시 — 실 HTTP·실 브라우저 `EventSource`.
- web `/ask` 기본 런타임을 스트리밍 런타임으로 교체하는 와이어링(0027 결정 8 "기본 교체는 T9.6 합류" 정신).

## 근거

- **포트 무변경·능력 추가** — `AgentRuntime.answer`는 0007부터 포트다. 스트리밍은 *옵셔널 능력*(`StreamingRuntime`)이라 코어 포트·라우팅·dispatcher·노출 경계가 안 흔들린다(헥사고날). 0010·0017·0027이 *같은 포트의 다른 구현*이었듯, 스트리밍은 *같은 포트 + 옵셔널 확장*이다.
- **이미 흐르는 청크를 막지 않는다** — `ProviderTransport`가 이미 청크를 yield하는데 `assemble_stream`이 모은다. seam은 *모으기 전에 새는 곳*(`answer_stream`)을 여는 것이다 — 새 LLM 능력 0.
- **audit-once는 지점 이동이지 새 메커니즘 아님** — 기존 `handle`의 끝 1회 record를 `handle_stream`의 스트림 종료 1회로 옮긴다. `handle`/`handle_stream` 상호 배타라 이중 기록 구조적 불가(`retrieve`의 `_answered_recorded` 멱등 가드 정신).
- **노출 투영 SSOT** — `serialize_reply`의 투영을 SSE가 *재사용*해 두 경로가 노출 불변식을 다르게 흘릴 여지를 제거한다(`serialize_reply`·`render_mcp_notification`·`map_response_to_answer`가 같은 투영 경계인 정신).

## Consequences

- **신규 값 객체(`runtime.py` 또는 `ask_org.py`)** — `AnswerChunk`(`text_delta: str`) · `AskEvent` sealed sum(`MetaEvent`·`TokenEvent`·`DoneEvent`·`PendingEvent`·`ErrorEvent`, frozen). SSE 직렬화 순수 함수 1개.
- **신규 옵셔널 포트** — `StreamingRuntime` Protocol(`@runtime_checkable`·`answer_stream`) · 스트리밍 디스패처 능력(`dispatch_stream`). 미구현 런타임/디스패처는 블로킹 폴백(미아 없음·하위호환).
- **`AskOrg.handle_stream`·`SessionAskOrg.handle_stream`** — 기존 `handle`은 *무변경*(블로킹 `/ask`·MCP·테스트가 계속 의존). 스트리밍은 *형제 메서드*로 추가. audit·Approval 게이트·세션 적재는 스트림 종료 시 1회.
- **web SSE 엔드포인트(게이트 밖 와이어링)** — `POST /ask/stream`(또는 `GET` + `EventSource`)이 `SessionAskOrg.handle_stream`을 `StreamingResponse`로 흘린다. 엔드포인트 배선·실 SSE 푸시는 게이트 밖(mcp-runtime-engineer + 프론트엔드).
- **게이트 경계** — 값 객체·Protocol·SSE 직렬화·오케스트레이션 결정 로직(stub 스트림 주입)은 게이트 내 결정론. 실 stdout/SDK 스트리밍·실 SSE 브라우저 푸시·기본 런타임 교체는 게이트 밖(T9.6 합류).
- **✅ 게이트 내 구현 완료(2026-06-30)** — `AnswerChunk`·`StreamingRuntime`·`StubStreamingRuntime`(`runtime.py`)·`LocalStreamingDispatcher`/`StreamedAnswer`(`dispatch.py`)·`AskEvent` sealed sum·`serialize_sse_event`·`project_answered`/`project_pending` 공유 투영 헬퍼·`AskOrg.handle_stream`(`ask_org.py`)·`SessionAskOrg.handle_stream`(`session.py`)·`serialize_reply` 헬퍼 리팩터(`web.py`). 테스트: `tests/test_streaming_seam.py`(23)·`tests/test_handle_stream.py`(26). 코어 포트 `AgentRuntime.answer`·`AskOrg.handle` 무변경·SDK import 0(게이트 내). 게이트: pytest 1672·pyright 0·ruff 0. 게이트 밖(web `/ask/stream` 배선·실 subprocess/SDK 스트리밍·기본 런타임 교체·프론트)은 T9.9(b) 후속.
- **✅ 게이트 밖 백엔드 구현 완료(2026-06-30, T9.9(b) 백엔드)** — 결정 5의 게이트 밖 항목 중 백엔드를 게이트 내 위에 얹었다(재구현 0). ① `ClaudeCodeRuntime.answer_stream`(`runtime.py`) — `answer`와 같은 프롬프트·cwd 접지 재사용·텍스트 델타만 yield(완성 Answer 조립은 `StreamedAnswer` 책임). 실 스트리밍 방식 = `claude -p --output-format stream-json --include-partial-messages --verbose`를 `subprocess.Popen`으로 띄워 `stream_event`/`content_block_delta`/`delta.type=="text_delta"`만 추출(직접 검증 — `text` 포맷은 점진성 없음·`thinking_delta`는 자연 배제·노출 불변식). **스트리밍 러너 seam** `StreamingClaudeRunner` Protocol을 옵셔널 주입(기본=실 헬퍼·테스트=가짜 러너)해 오케스트레이션을 결정론 단위 검증. 에러/timeout은 폴백 없이 *그대로 전파*(상위가 ErrorEvent로 투영). 코어 포트 `answer` 무변경(형제 메서드). ② `web.py` `POST /ask/stream` — `/ask`와 동일 익명 세션 쿠키 패턴·`StreamingResponse(generate(), media_type="text/event-stream")`·`generate()`가 `handle_stream`을 순회해 `serialize_sse_event` yield·`try/except`로 예외·timeout 시 중립 `ErrorEvent` 1프레임만(내부 예외·스택 0)·헤더 `Cache-Control: no-cache`/`X-Accel-Buffering: no`/`Connection: keep-alive`. ③ `demo.py` 기본 디스패처 `LocalRuntimeDispatcher`→`LocalStreamingDispatcher`(블로킹 면 동형이라 비스트림 경로 행위 불변·`dispatch_stream` 능력만 더함). 신규 테스트 14(`test_claude_runtime.py` +8·`test_web.py` +6). 게이트 pytest **1686**(1672→+14·회귀 0)·pyright 0·ruff 0. **수동 SSE 시연**: 라이브 `uvicorn`에 `curl -N`로 "환불 규정?" → `meta` 즉시·`token` 14개 점진(14~19.75s·0.5s 간격·실 claude OKF 읽음)·`done`(mode=full·sources=["위키/환불정책"]) 관찰·다툼은 `pending` 단독. **이번 범위 아님(메인/후속)**: 프론트 `EventSource` 렌더·실 공급자 SDK `text_stream`(`ClaudeApiRuntime`).
- **✅ 실 공급자 SDK 스트리밍 합류 완료(2026-06-30, T9.6 합류)** — line 159가 "이번 범위 아님"으로 남긴 마지막 조각(실 공급자 SDK `text_stream`이 `/ask/stream` 델타로 흐름)을 채웠다. **두 조각만:** ① `ProviderApiRuntime.answer_stream`(`provider_runtime.py`) — `answer`의 형제(코어 `answer` 무변경). 같은 파이프라인(`read_okf_bundle`→`build_provider_request`→`transport`)이되 `assemble_stream`으로 모으지 *않고* transport 청크를 `AnswerChunk(text_delta=chunk)`로 그대로 yield(빈 청크 스킵). 이로써 `ProviderApiRuntime`(따라서 `ClaudeApiRuntime`·`CodexApiRuntime`)이 `StreamingRuntime`을 만족 → `StreamedAnswer.__iter__`의 `isinstance(runtime, StreamingRuntime)`가 True → 블로킹 1델타 폴백을 탈출하고 실 SDK 토큰 델타가 점진 흐른다(기존 근본 원인: `answer`가 한 곳에서 모음). 실 SDK transport(`AnthropicSdkTransport.__call__`→`client.messages.stream().text_stream` yield·owner OAuth·중앙 토큰 0)는 0027에서 이미 구현됨 — 재구현 0. ② web:app가 `AON_PROVIDER` 존중 — 공급자 레지스트리·선택 로직을 `worker.py`에서 신규 공유 모듈 `runtime_select.py`(`select_runtime`·`_make_*_runtime`·`_PROVIDER_*`)로 옮겨 worker(별도 프로세스)·web:app(인프로세스)이 *단일 출처*를 공유. `runtime_select`는 코어 `runtime`(`AgentRuntime`·`ClaudeCodeRuntime`)만 모듈 레벨 import하고 공급자 SDK 어댑터는 팩토리 안 지연 import라 순환 import·코어 SDK 의존 0. web.py 모듈레벨 `app = create_app(runtime=select_runtime(DEMO_OKF_ROOT), ...)`. **무회귀**: `AON_PROVIDER` 미설정 → `ClaudeCodeRuntime`(기존 build_demo 기본·게이트·데모 행위 불변). 설정 시 → 그 공급자 인프로세스 SDK 런타임. 신규 테스트 7(`test_provider_runtime.py` — `StreamingRuntime` 만족·다중 델타·빈 청크 스킵·`LocalStreamingDispatcher` 다중 델타+완성 텍스트 조립). 게이트 pytest **1712**·pyright 0·ruff 0. **수동 SSE 시연(자격 없음)**: `AON_PROVIDER=claude-api` web:app에 `/ask/stream` "환불 규정?" → `meta` 즉시(담당·승인·출처만) 흐른 뒤 owner OAuth 자격 부재(`ANTHROPIC_API_KEY`/`ant` 없음)로 SDK 인증 실패가 중립 `error` 1프레임으로 투영(내부 예외·스택 0) 확인. 자격이 있으면 `meta`→`token`*(실 SDK 델타)→`done` 경로(자격 가용 시 검증). **이번 범위 아님(메인/후속)**: 프론트 `EventSource` 렌더·codex 실 transport 시연·gemini.
- **불변식 영향 없음:**
  - **노출 불변식** — `meta`/`done`/`pending`이 `serialize_reply` 공유 투영을 거침. `token`은 텍스트 델타만. 내부값·예외·스택 0(`error`도 중립 안내).
  - **전이≠기록(audit-once)** — 스트림 종료 시 정확히 1회. `handle`/`handle_stream` 상호 배타로 이중 기록 불가.
  - **미아 없음 / Pending 비스트림** — Contested·Unowned·dispatched는 단일 `pending` 이벤트 후 종료. 라우팅 종착 무변경(0→Unowned·≥2→Contested·timeout→escalation). 0 매칭 루트 User 에스컬레이션 보존.
  - **공급자 중립** — `StreamingRuntime`·`answer_stream`은 공급자를 모른다(claude·codex·gemini가 같은 능력의 다른 구현). 미지원 공급자(`GeminiApiRuntime` 자리)는 블로킹 폴백.
  - **중앙 토큰 0·비소유** — 스트리밍은 *답 운반*이지 지식 소유가 아님. owner 워커가 owner OAuth로 델타를 만들고 중앙은 흘려보내기만(0027·0010 보존).
  - **Authority 중앙** — 스트리밍은 답 생성이지 권한 선언이 아님(누가 담당·누가 owner는 무변경).
  - **라우팅 정합** — `handle_stream`도 `route(question)`에 맨 질문만(맥락은 `dispatch_stream(context=)`로만). 두 인자가 다른 함수에 가 맥락이 분류에 새는 경로가 호출 구조상 없음(0027 결정 6 보존).
- **갱신 대상**: CONTEXT(Provider Runtime·Answer·Agent Runtime 절에 스트리밍 seam·`AnswerChunk`·`AskEvent`·`StreamingRuntime` 신규 용어)·PRD §3(점진 렌더 UX)·TRD §4(SSE 계약·게이트 경계)·tasks-v0(T9.6 스트리밍 슬라이스 또는 신규 Task).
