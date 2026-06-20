# 분산 전송은 owner 워커의 역방향 아웃바운드 연결 + 중앙 작업 큐로 한다 — owner PC는 서버를 노출하지 않는다

상태: accepted (2026-06-20) · ADR 0010의 *전송 계층* 보강(답변 주체=owner Claude Code는 그대로, "어떻게 그 환경에 도달하나"를 못박음)

ADR 0010은 Agent Runtime의 답변 주체를 각 Owner의 Claude Code로 못박았고, 최종(T6.3)은 "각 Owner PC의 Claude Code에 분산 연결(MCP/A2A 등록·호출)"이라 적었다. TRD §5도 "각 Owner Claude Code가 MCP/A2A로 중앙에 등록·연결, 중앙이 client로 호출, 로컬 PC 도달은 후순위"라 적었다. T6.3은 그 *전송 계층*을 채운다 — **중앙이 owner 환경에 실제로 어떻게 도달하는가**. 되돌리기 어려운 결정 두 가지: (1) 연결 방향(누가 누구에게 연결을 거는가), (2) 동기/비동기(중앙 핸들러가 답을 어떻게 기다리는가).

## 결정

### 1. 연결 방향 = owner 워커의 역방향 아웃바운드 연결 (owner PC는 서버를 노출하지 않는다)

owner PC는 **API를 제공(서버 노출)하지 않는다.** 실제 owner 환경(개인 PC)은 NAT/방화벽 뒤에 있고, 고정 IP가 없으며, 상시 가동을 보장하지 않는다. 중앙이 owner PC로 인바운드 연결을 거는 그림(중앙=client, owner=server)은 이 현실에서 깨진다 — 도달 불가·포트포워딩·인증서·상시 켜짐 가정이 모두 무너진다.

그래서 **방향을 뒤집는다.** owner PC에서 도는 작은 **Owner Worker**가 **중앙에 아웃바운드로 연결**한다(큐 폴링 또는 WebSocket/SSE). 아웃바운드라 방화벽·NAT를 그대로 통과한다. 중앙은 서버로서 연결을 *받기만* 하고, owner를 향해 먼저 소켓을 열지 않는다.

- 참고 선례: Claude Code의 Remote Control(owner의 claude가 claude.ai에 아웃바운드 연결을 걸어두고 원격 지시를 받는 패턴)이 정확히 이 모양이다. owner 쪽이 연결을 *건다*.
- TRD §5의 "중앙이 client로 호출"은 *논리적 호출 방향*(중앙이 질문을 보낸다)을 뜻하고, 이 ADR은 *물리적 연결 방향*(소켓을 거는 쪽은 owner)을 분리해 못박는다. 충돌이 아니라 구체화 — 질문은 여전히 중앙→owner로 흐르되, 그 흐름이 owner가 미리 열어둔 아웃바운드 채널을 *타고 내려간다*.

### 2. 동기/비동기 = 중앙은 owner별 작업 큐에 적재하고, 회신은 비동기 (포트는 동기를 유지하되 어댑터가 흡수)

owner 부재·PC 꺼짐이 정상 상태다. 따라서 답은 **즉시 보장되지 않는다.** 중앙은 질문을 **owner별 작업 큐(Work Queue)**에 넣고(`enqueue`), owner Worker가 연결돼 있을 때 가져가(`claim`/`poll`) 로컬 claude(ADR 0010의 `ClaudeCodeRuntime` 재사용)로 답을 만들어 중앙에 **회신**한다(`submit`). owner가 꺼져 있으면 작업은 큐에 **대기**한다.

이 비동기 본질을 표현하려고 **새 포트 `RuntimeDispatcher`**(작업 큐 디스패처)를 도입한다 — `dispatch(question, card) -> WorkTicket`(작업을 owner 큐에 넣고 추적표를 즉시 반환) + `poll(ticket) -> DispatchOutcome`(회신·대기·timeout escalation 중 하나). `AgentRuntime`(동기 `answer`)은 **폐기하지 않는다.** 디스패처 위에 얇은 어댑터(`DispatchingRuntime`)를 얹어 `answer()`를 유지한다 — 어댑터가 내부에서 `dispatch` 후 회신이 올 때까지 *블로킹 대기*하거나 timeout이면 escalation을 표면화한다.

근거(왜 포트를 둘 다 두는가):
- **포트 안정성** — `ask_org`·`router`는 `AgentRuntime.answer(question, card) -> Answer` 동기 계약에 묶여 있다(ADR 0007·0010이 "포트는 안 바뀐다"고 못박음). 이 계약을 깨면 핸들러·테스트·web 어댑터가 연쇄로 흔들린다. 어댑터로 흡수해 **기존 동기 진입점을 보존**한다.
- **비동기를 거짓말하지 않음** — 그러나 분산의 본질은 비동기다. `RuntimeDispatcher`/`WorkTicket`/`DispatchOutcome`을 *1급 도메인 타입*으로 둬, 비동기(대기·timeout·escalation)를 동기 `Answer` 뒤에 숨기지 않고 명시적으로 모델링한다. ask_org를 진짜 비동기(Pending류 회신 후 나중 알림)로 끌어올릴 때 이 타입들이 그대로 쓰인다 — 어댑터는 *그때까지의* 다리일 뿐이다.
- **기존 포트 패턴 정합** — `AuditLog`·`PrecedentStore`·`ConflictCaseStore`와 같은 `Protocol + InMemory` 패턴(ADR 0008). `RuntimeDispatcher` Protocol + `InMemoryWorkQueueDispatcher`(결정론 테스트용 in-process 큐).

### 3. owner 부재·timeout → 기존 Manager escalation 재사용

작업이 큐에서 너무 오래(주입 정책) 회수되지 않거나 회신이 timeout이면, 이는 **미아·합의 실패와 같은 처분 = Escalation**이다(CONTEXT: Escalation은 Contested/Unowned의 종착). 새 escalation 경로를 만들지 않고 **사람 그래프를 타고 Owner의 Manager로 올라가는 기존 경로를 재사용**한다. `DispatchOutcome`의 한 갈래 `EscalatedToManager`로 표면화하고, 실제 Manager 큐 연결은 T5.2(Manager 큐)에 위임한다 — ADR 0008이 `Deadlocked`를 "자리만 남기고 T5.2로" 미룬 것과 동일한 처분 정합.

## Considered Options (연결 방향)

- **owner 워커의 역방향 아웃바운드 + 중앙 큐**(선택): 방화벽/NAT 통과, 상시 켜짐 불요, owner 부재가 큐 대기로 자연 흡수. 단 비동기·전달 보장·연결 수명 관리 비용.
- **중앙→owner 인바운드(owner=server)**(기각): owner PC 서버 노출·포트포워딩·고정 IP·상시 가동을 요구 — 개인 PC 현실에서 비현실적. ADR 0010의 "owner 환경에서 답" 비전을 운영적으로 깨뜨린다.
- **owner가 답을 중앙 큐에 push만(연결 유지 없음, 순수 폴링)**: 가장 단순하나 실시간성↓·폴링 비용. → walking skeleton 첫 슬라이스는 이 *논리*(큐 적재+회수)만 in-process로 보이고, 실제 폴링 vs WebSocket 선택은 네트워크 슬라이스로 미룬다(아래).

## Consequences

- **비동기 한계가 1급 시민이 된다.** 답이 즉시 오지 않는 게 정상이다. walking skeleton 첫 슬라이스는 어댑터의 블로킹 대기로 *기존 동기 ask_org를 보존*하지만, 실제 제품에선 ask_org가 `Pending(kind="dispatched")`류로 즉시 회신하고 회신은 나중 알림으로 가는 게 옳다(ADR 0006의 OrgReply Pending 정신과 정합). 이 전환은 후속 결정으로 남긴다 — 이 ADR은 *타입을 비동기로 준비*해 두되 진입점 전환은 강제하지 않는다.
- **escalation 재사용.** owner 부재/timeout은 Manager escalation으로 흐른다 — 미아·합의 실패와 동일 처분. 미아 없음 불변식 유지: 큐에 들어간 작업은 회신되거나 escalation되거나 둘 중 하나로 *반드시* 종착하며, 영영 사라지지 않는다.
- **신원·책임 연결점(지금 구현 X).** 워커가 진짜 그 owner인지는 ADR 0009(페르소나 인증)가 강제할 자리다 — 워커는 중앙 연결 시 owner 신원으로 인증해야 하며, 인증되지 않은 워커의 회신은 거부된다. `WorkTicket`/회신에 `owner_id` 귀속을 실어 *연결점만* 명시하고, 실제 인증은 T6.5에 위임한다.
- **Approval 게이트 연결점(지금 구현 X).** owner가 만든 답이 `draft_only`(Approval 게이트, CONTEXT: Answer.mode)면 회신은 사람 승인 전까지 초안이다. 회신 경로에 mode를 보존해 *Approval과 합류할 자리*를 남긴다 — Approval 평가 자체는 T2.5/Routed 영역이고 이 ADR 범위 밖.
- **전이 ≠ 기록 유지.** 작업 enqueue·claim·submit·timeout은 *도메인 전이*(디스패처/큐 상태)이고, audit 기록은 별개로 계속 흐른다(ADR 0008과 동일). 작업 큐는 미해소 작업의 도메인 보관소지 절차 로그가 아니다.
- **결정론 테스트는 in-process로.** 실제 네트워크 전송·워커 프로세스·연결 수명은 비결정이라 단위 테스트에 부적합하다. `InMemoryWorkQueueDispatcher` + Fake Worker(동기 회신)로 *큐 적재→회수→회신→escalation 분기*를 결정론으로 검증하고(ADR 0003), 실제 전송 품질은 데모/수동 시연으로 본다.
- **외부 의존 추가(다음 슬라이스).** 네트워크 슬라이스부터 owner Worker는 별 프로세스·아웃바운드 연결(폴링 또는 WS)·로컬 `claude` CLI에 의존한다. 연결 끊김·재연결·중복 전달은 그 슬라이스에서 다룰 실패 모드다.
- TRD §5 분산 전송·tasks T6.3, CONTEXT(Owner Worker·Work Queue·RuntimeDispatcher·WorkTicket·DispatchOutcome)를 이 방향으로 갱신한다.
