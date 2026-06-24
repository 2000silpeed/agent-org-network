# 실시간 충돌 푸시 통지 — 처리함·큐에 항목이 *적재되는 사건*에서 채널 중립 push를 한 번 쏜다, pull은 그대로 남아 미아 없음을 이중 보장한다

상태: accepted (2026-06-23) · **구현 완료(tdd-engineer red→green — 발화 지점 4개 전부[ConflictCase·Reeval·Manager·BackupReview] + m1 subject_ref 네임스페이스 + code-reviewer 수정[M1 빈 귀속 가드·Minor1 clock 단일 시점] — 768 passed/pyright 0/ruff 0; 실 채널 어댑터·실 비동기 전달·동적 구독은 게이트 밖 후속)** · **T8.2 갱신(2026-06-24·결정 9·10): 첫 실 채널 = MCP 확정·`render_mcp_notification` 렌더 순수 함수(노출 불변식 게이트 내 본체)·`McpChannel` transport 주입 실 어댑터(stub 탈출)·fire-and-forget MVP 확정·Slack/Email 후속 stub. 렌더는 게이트 내·실 MCP wire-send는 게이트 밖(mcp-runtime-engineer/수동).** · **ADR 0017 결정 6④의 본체**("충돌·검토·escalation을 Slack/메일/MCP 알림으로 *push*[지금은 조회(pull)뿐]" — ADR 0017:62·76이 "실시간 충돌 푸시 통지"를 Phase 7 ④로 미룬 것을 이 ADR이 닫는다) · **ADR 0019 결정 5의 연장**("owner nudge = 처리함 pull 재사용·실시간 push는 T7.4" — ADR 0019:99·147이 자리만 연 push를 본체로 채움; `StalenessPropagator`의 `propagator=None` 옵셔널 주입이 이 ADR 발화의 본보기) · **ADR 0011·0012 인프라의 *패턴* 재활용**(멱등 `ticket_id`·at-least-once 정신만 — 코드 재사용 아님, 아래 결정 5) · ADR 0008(ConflictCase 처리함)·ADR 0014(Manager 큐 수렴)와 정합 · Phase 7 T7.4의 설계·shape를 닫는다.

## 맥락 — 끊어진 고리 하나

ADR 0017 결정 6은 "충돌이 실시간으로 owner에게 가고, 정리되면 자동 처리되는 중앙장치"를 1급으로 못박으며 두 갈래를 열었다 — ② 변경 시 재평가(T7.3·ADR 0019가 닫음)와 ④ **실시간 푸시 통지**. ④가 비어 있다. 지금 owner·manager는 처리함·큐를 *조회(pull)*해야만 새 일을 안다:

- owner는 `pending_for_owner`(ConflictCase·BackupReviewItem·ReevalItem 세 탭)를 *직접 열어봐야* 자기 다툼·검토할 백업 답·재평가할 stale 판례를 본다.
- manager는 `pending_for_manager`를 *직접 열어봐야* escalation을 본다.

새 일이 생긴 *그 순간* owner·manager에게 가닿는 채널이 없다. "실시간으로 owner에게 요청"(ADR 0017:62·PRD §6 ④)이 명목뿐이다.

**핵심: push는 pull을 대체하지 않고 *추가*한다.** T7.4는 "새 일이 생기면 *push*로도 알린다"를 더하는 것이지, 처리함 pull을 걷어내는 게 아니다. 통지 채널이 통째로 실패해도 처리함 pull은 그대로라 owner·manager는 여전히 조회로 일을 본다 — **가시성 이중 보장**(미아 없음은 push가 아니라 pull이 떠받친다, 결정 6). 발화는 *처리함/큐에 항목이 적재되는 사건*이다(다툼 open·백업 답 add·재평가 add·escalation enqueue).

설계 제약 셋이 범위를 좁힌다:

1. **결정론 게이트.** 실 전송(Slack API·SMTP·MCP 알림)은 비결정·외부 의존·새 무거운 의존성이라 게이트 밖 수동이다. 게이트 안 결정론은 `FakeChannel`(메모리 inbox·전달 로그) 주입까지 — `GitGateway`↔`FakeGitGateway`·`AgentRuntime`↔`StubRuntime`·`OidcProvider`↔`FakeOidcProvider`와 **같은 포트 패턴**.
2. **채널 중립.** 특정 벤더(Slack 등)가 1급이 되면 안 된다 — 전부 어댑터다. 첫 실 어댑터 후보는 MCP 알림(제품이 MCP 서버라 외부 의존 0)이나, 게이트 밖이라 지금 안 정한다(shape엔 Slack/Email/MCP 어댑터 자리만 `NotImplementedError`).
3. **노출 불변식.** 통지는 *운영 면*(owner/manager) 신호다 — "처리할 게 생겼다". 운영 내부값은 OK이되 **실 사용자 채팅엔 통지 0**(OrgReply 노출 불변식과 다른 면). 통지 본문에 사용자向 비밀이 새지 않게.

## 결정

### 1. `NotificationChannel` 포트(Protocol) — 채널 중립, `send(notification)` 한 연산

실 전송은 비결정·외부 의존(Slack API·SMTP·MCP)이라 `GitGateway`·`OidcProvider`·`AgentRuntime`과 **같은 포트 패턴**으로 격리한다:

- **`NotificationChannel` 포트(Protocol)** — `send(notification: Notification) -> None`. 한 수신자에게 한 통지를 전달한다. 전달 결과를 반환하지 않는 fire-and-forget(전송 실패는 채널 내부 재시도/미전달 처리 자리 — 결정 5; 도메인은 "쐈다"까지만 본다). 실 전송은 *불투명한 외부 부작용*이라 포트로 가린다.
- **`FakeChannel`**(결정론·게이트 내) — 메모리 inbox(`recipient_id → list[Notification]`)에 쌓고 전달 로그(append-only)를 든다. 실 네트워크·시각·랜덤 0이라 단위 테스트가 결정론(`FakeGitGateway`의 결정 SHA, `FakeOidcProvider`의 in-memory 토큰 맵과 같은 결). 테스트가 "이 적재 사건이 이 수신자에게 이 통지를 쐈나"를 inbox 조회로 검증한다.
- **실 어댑터(`SlackChannel`·`EmailChannel`·`McpChannel` — 게이트 밖·`NotImplementedError`)** — 실 전송 본체. 새 무거운 의존성(Slack SDK·SMTP 라이브러리·MCP 알림 클라이언트)을 더할지는 **tdd-engineer/후속이 판단**한다(이 ADR은 자리만, shape는 `NotImplementedError`). `SubprocessGitGateway`·`HttpOidcProvider`가 게이트 밖 수동인 것과 동형.
- **채널 중립** — 어떤 채널도 1급이 아니다. 포트가 채널 어휘(Slack 채널 ID·이메일 주소·MCP 엔드포인트)를 *가정하지 않는다* — `Notification.recipient_id`(owner/manager User.id)를 받고, recipient → 실 주소 변환은 각 어댑터가 자기 안에서 한다. 그래서 어떤 통지 채널에도 붙는다.

### 2. `Notification` 값 객체(frozen) — 운영 면 신호, 사용자 채팅엔 0

통지 한 건의 도메인 값 객체:

- **`Notification`**(frozen pydantic) 필드:
  - `recipient_id: str` — owner 또는 manager의 User.id(귀속 키 — 누구에게 가는 통지인가).
  - `kind: NotificationKind` — 통지 종류(아래). 어느 처리함/큐 적재가 이 통지를 낳았나.
  - `subject_ref: str` — 어느 항목인가(case_id / item_id / intent 등 — 적재된 항목의 식별자). 멱등 키의 일부(결정 5)이자 수신자가 "무엇 때문에"를 잇는 손잡이.
  - `created_at: datetime` — 주입 clock(결정론 — `ReevalItem.flagged_at`·`ConflictCase.opened_at`과 같은 결).
- **`kind`는 `Literal`(`NotificationKind`)이 맞다 — sealed sum이 아니다.** 우리 관용구에서 sealed sum(`RoutingDecision`·`ConsensusOutcome`·`ReevalOutcome`)은 *각 변이가 자기 필드를 다르게 들 때* 쓴다(타입이 곧 상태·`match` 망라). `Notification`의 네 종류(`conflict_opened`·`backup_review_added`·`reeval_flagged`·`manager_escalated`)는 *필드 구조가 같다*(전부 recipient·subject_ref·created_at만 든다) — 종류는 분기 없는 *라벨*이라 `Literal`이 정확하다. 이는 `ReevalItem.subject_kind`(문자열 판별자)와 같은 판단이고, sealed sum은 *처분*(owner/manager가 *행위*를 고를 때 — `ReevalOutcome`·`ManagerAction`)에 둔다. 통지는 처분이 아니라 *사건 라벨*이다.
- **노출 불변식**: `Notification`은 *운영 면*(owner/manager) 신호다 — 운영 내부값(case_id·item_id·intent)을 들어도 OK(Inbox·Manager 큐가 내부값을 노출하는 것과 같은 면). 단 **실 사용자 채팅엔 통지 0**(OrgReply는 통지를 *모른다* — 통지는 `ask_org` 사용자向 경로에 끼어들지 않는다). 통지 본문(실 어댑터가 렌더할 때)에 사용자向 비밀(질문 원문의 민감 내용 등)이 새지 않게 — MVP `Notification`은 *식별자만* 들고 본문 렌더는 어댑터 책임으로 미룬다(`WorkTicket`이 카드 본문이 아니라 식별자만 드는 정신).

`NotificationKind = Literal["conflict_opened", "backup_review_added", "reeval_flagged", "manager_escalated"]` — 네 발화 종류의 망라(새 종류는 여기 더하고 발화 지점에서 채운다).

### 3. 구독 = recipient → channel 매핑, MVP는 `Notifier`가 주입 맵을 든다

"누구에게 어느 채널로 보내나"는 구독(subscription)이다. single tenant·MVP라 *가장 단순한 닫힌 루프*를 고른다:

- **`Notifier`**(통지 서비스)가 `subscriptions: dict[str, NotificationChannel]`(recipient_id → 채널)을 주입받는다. 발화 지점은 `Notifier.notify(notification)`만 부르고, `Notifier`가 `notification.recipient_id`로 채널을 찾아 `channel.send`한다.
- **미구독 recipient는 skip**한다(그 recipient_id에 채널이 없으면 통지를 *조용히 건너뛴다*). 처리함 pull은 그대로라 미아가 없다 — 구독 안 한 owner도 조회로 일을 본다(push는 추가지 필수가 아니다, 결정 6). skip은 전달 로그에 남겨 운영이 "통지가 안 간 recipient"를 본다(자리만).
- **`Subscription`/`SubscriptionStore` 별 포트는 MVP에 두지 않는다** — 과하다. recipient → channel은 *주입 맵 하나*로 충분하고(데모는 모든 owner/manager를 한 `FakeChannel`에 매핑), 별 store는 구독 추가/삭제 라이프사이클(구독 관리 UI·동적 변경)이 생길 때 도입한다(open question). `ReevalStore`가 별 store인 건 *전이*(pending→reviewed)가 있어서지만, 구독은 전이 없는 정적 매핑이라 store가 불요하다. **DDD 주의(open question)**: 구독이 동적(런타임에 owner가 채널을 켜고 끔)이 되면 `SubscriptionStore` 포트(패턴 N번째)로 승격한다.

**기각**:
- `Subscription`/`SubscriptionStore` 포트를 MVP에 신설 — 정적 매핑엔 과도(전이 없는 데이터에 store 패턴은 무게만 는다). 동적 구독이 실제 요구가 될 때 도입.
- recipient별 채널 *리스트*(한 recipient에 여러 채널 fan-out) — MVP는 recipient당 채널 1개. 다중 채널 fan-out은 후속(맵 값을 list로 넓히면 됨).

### 4. 발화 지점 = 처리함/큐 적재 (단일 발화 추상 = `Notifier.notify` 한 인터페이스, 적재 지점마다 옵셔널 주입)

발화는 *처리함/큐에 항목이 적재되는 사건*이다. T7.3 `StalenessPropagator`가 `commit_okf_bundle(..., propagator=None)` 옵셔널 주입으로 발화한 것과 **동형**으로, 각 적재 지점에 `notifier: Notifier | None = None`을 옵셔널 주입한다 — `None`이면 기존 동작(하위호환·게이트 보존), 비`None`이면 적재 직후 `notifier.notify(notification)` 1회.

**단일 발화 추상을 어디에 두나 — 두 길의 갈림.** 네 적재 지점(ConflictCase open·BackupReview add·Reeval add·Manager enqueue)을 한 추상으로 묶는 길은 둘이다:

- (A) **store에 통지 책임을 넣는다** — 각 store의 `open_case`/`add`/`enqueue`가 내부에서 `notify`를 부른다.
- (B) **적재를 *오케스트레이션*하는 곳(서비스/핸들러)이 적재 직후 notify를 부른다** — store는 순수 보관, 발화는 호출자.

**(B)를 택한다.** 근거:
- **전이 ≠ 기록 ≠ 통지의 분리.** store는 *전이의 보관소*다(ADR 0008·0012·0014·0019가 일관되게 "전이 ≠ 기록"으로 store를 절차 로그와 분리). 통지를 store에 넣으면 store가 보관 + 외부 부작용(전송)을 겸해 책임이 샌다 — `BackupReviewStore`가 audit을 안 남기고 호출자가 남기는 정신(ADR 0012 결정 7), `ReevalService`가 전이만 하고 재검토 기록은 호출자가 남기는 정신(ADR 0019 결정 3)과 같다. **통지도 호출자(오케스트레이션) 책임**이다.
- **결정론 단위 테스트.** store는 순수 in-memory로 남아 기존 store 테스트가 통지 주입 없이 그대로 산다(게이트 보존). 통지 발화는 오케스트레이션 함수/서비스에 `notifier` 주입으로 결정론 검증(`commit_okf_bundle`이 순수 함수라 `propagator` 주입으로 결정론 검증되는 것과 동형).
- **단일 발화 추상 = 인터페이스 하나(`Notifier.notify`)이지 함수 하나가 아니다.** 네 적재 지점이 *물리적으로 한 함수*를 거치지 않는다(ConflictCase는 `ask_org.handle`의 Contested arm, Reeval은 `StalenessPropagator.on_okf_committed`, Manager는 `ask_org._enqueue_*`/`ConsensusService`, BackupReview는 `WebSocketDispatcher.submit`로 *발화 맥락이 제각각*이다). 이들을 억지로 한 함수로 모으면(예: 모든 적재가 거치는 "처리함 적재 이벤트 버스") 과결합·과추상이 된다 — 분산 코드 재사용을 피하는 결정 5와 같은 판단. 대신 **발화 *인터페이스*를 하나(`Notifier.notify(notification)`)로 통일**하고, 각 적재 지점이 자기 맥락에서 그 인터페이스를 부른다. 이것이 "단일 발화 추상"의 우리 식 답 — 한 *함수*가 아니라 한 *포트 호출 모양*.

**MVP 슬라이스 = 최소 1~2개 발화 지점부터.** 네 지점을 한꺼번에 발화시키지 않고 **ConflictCase open·ReevalItem add 두 지점부터**(슬라이스 3, 아래) 같은 패턴으로 박고, BackupReview·Manager는 나머지 확장으로 가른다 — 슬라이스로 게이트 그린을 독립적으로 지킨다(`StalenessPropagator`가 Precedent·Answer 두 축을 점진 검증한 정신).

**기각**:
- (A) store가 통지 — 전이 보관과 외부 부작용을 겸해 책임이 샌다(전이 ≠ 기록 ≠ 통지). store 테스트가 통지 주입에 오염된다.
- 전역 "처리함 적재 이벤트 버스"(모든 적재가 거치는 단일 함수) — 발화 맥락이 제각각인 네 지점을 억지 통합하는 과추상. 인터페이스 통일(`Notifier.notify`)로 충분.

### 5. 전달 보장 = 멱등 + at-least-once (분산 인프라 *패턴* 재활용, 코드 재사용 아님)

같은 사건에 발화가 두 번 와도(같은 다툼에 두 번째 질문이 또 Contested를 내는 등) **같은 통지를 중복 발송하지 않는다**:

- **멱등 키 = `(recipient_id, kind, subject_ref)`.** `Notifier`가 이미 보낸 통지 키를 메모리 set에 들고, 같은 키면 `send`를 건너뛴다(이미 알린 일을 또 알리지 않는다). 같은 항목에 발화가 반복돼도 통지는 한 번 — `StalenessPropagator`의 `needs_review` 가드·answer dedup(같은 subject_ref 재적재 skip, ADR 0019 결정 2②)과 *동형 멱등*이고, `ManagerQueueStore.get_by_case`(같은 case 중복 enqueue 방지)와 같은 정신이다.
- **미전달(채널 실패)은 재시도/미전달 큐 자리**(MVP는 fire-and-forget — `send`가 던지면 `Notifier`가 삼키고 전달 로그에 실패로 남긴다·실 재시도/dead-letter는 게이트 밖). at-least-once(채널이 재시도로 최소 한 번 전달 보장)는 실 어댑터·실 비동기 전달의 몫(open question).

**분산 인프라를 코드로 재사용하지 않고 *패턴*만 따른다 — 이게 이 ADR의 핵심 판단 하나다.** ADR 0017:50·76이 "디스패처·작업 큐·재연결·멱등 인프라를 푸시 통지로 재활용"이라 했으나, *코드 직접 재사용*(`WebSocketDispatcher`·`InMemoryWorkQueueDispatcher`)은 **과결합**이다:

- 작업 디스패치는 **round-trip**이다 — 중앙이 owner 워커에 작업을 push하고 *답을 회수*한다(`dispatch→poll`, claim/submit, 단조 종착, re-queue). 큐 상태기계(queued↔claimed↔answered↔expired)가 본체다.
- 통지는 **fire-and-forget push**다 — "처리할 게 생겼다"를 한 번 쏘고 끝이다. 답을 회수하지 않고, claim/submit이 없고, 큐 상태기계가 없다.

둘은 *다른 도메인*이다. `WebSocketDispatcher`를 통지에 끌어쓰면 통지가 필요 없는 큐 상태기계·claim/submit·답 회수에 결합된다. 그래서 **재사용하는 건 *정신*뿐**: (a) **멱등 키**(ADR 0011 결정 6-4 `ticket_id` 멱등 → `(recipient, kind, subject_ref)` 멱등), (b) **at-least-once + dedup**(중복 전달을 멱등으로 흡수하는 정신), (c) **fire-and-forget push 채널이 owner 환경에 닿는다는 발상**(ADR 0011 결정 1 아웃바운드 정신 — 단 통지는 단방향이라 훨씬 가볍다). 코드는 새로 쓰되 검증된 *정신*을 따른다 — `reeval.py`가 BackupReview store 패턴을 *복제*(코드 상속 아님)한 것과 같은 판단.

### 6. pull 보존 · 미아 없음 (push는 추가지 대체가 아니다)

- **기존 처리함 pull은 *그대로*다.** `pending_for_owner`(ConflictCase·BackupReview·Reeval)·`pending_for_manager`(Manager 큐) 조회는 무변경. push는 그 위에 *얹는다*.
- **통지 채널 전체가 실패해도 처리함은 남는다 — 미아 없음은 pull이 떠받친다.** owner·manager가 통지를 못 받아도(채널 다운·미구독·발화 지점 미주입) 처리함 pull로 일을 본다. 통지는 *적시성*을 더할 뿐 *종착*을 책임지지 않는다 — 종착은 처리함 잔류(ADR 0019 결정 5 "처리함 영구 잔류로 가시성 보장")와 Manager 큐 수렴(ADR 0014 "미아 없음의 마지막 칸")이 이미 보장한다.
- **owner 비응답 종착과 정합.** owner가 통지를 받고도 응답 안 하는 ReevalItem은 ADR 0019 결정 5의 timeout→Manager escalation 자리로 흐른다 — 통지는 그 종착을 바꾸지 않는다(알림이지 처분이 아님).

### 7. 불변식 영향 없음

- **미아 없음** — push는 *알림*이지 라우팅·종착 도메인이 아니다(결정 6 — pull 보존). 0매칭→루트 escalation·처리함 잔류·Manager 큐 수렴 그대로.
- **Authority 중앙** — 통지는 "처리할 게 생겼다" 신호이지 *권한 선언*이 아니다(카드 자기보고 무관·`routing_rules.yaml` 무관).
- **전이 ≠ 기록** — 통지 발화는 *도메인 전이가 아니다*(store 상태를 바꾸지 않는다 — 적재가 전이, 통지는 그 *뒤*의 외부 부작용). 전달 로그(`FakeChannel`·`Notifier`의 send 기록)는 audit과 *별 축*이다 — audit은 질문→라우팅→디스패치→답 절차 기록이고, 전달 로그는 "통지가 나갔나"의 운영 신호(BackupReview 검토 기록을 audit에 안 남기는 정신과 같이, 통지도 audit에 안 남긴다).
- **노출 불변식** — 통지는 운영 면(owner/manager)만 간다. 실 사용자 채팅엔 통지 0(OrgReply는 통지를 모른다). `Notification`은 식별자만 들고 본문 렌더는 어댑터 책임(사용자向 비밀 누설 차단).
- **등록 무결성** — 통지는 카드 admission과 무관(Registry.validate 무관).

### 8. 게이트 밖 / open questions

- **실 Slack/메일/MCP 어댑터** — `SlackChannel`·`EmailChannel`·`McpChannel`의 실 전송 본체(새 무거운 의존성 추가 판단 포함). 첫 후보는 MCP 알림(외부 의존 0)이나 게이트 밖이라 지금 안 정함.
- **실 비동기 전달** — 워커·재연결·dead-letter 큐·at-least-once 보장(채널 재시도). MVP는 fire-and-forget(동기 `send`·실패는 로그).
- **구독 관리** — 동적 구독(owner가 채널을 켜고 끔)·`SubscriptionStore` 포트 승격·구독 관리 UI.
- **통지 빈도 제한** — rate limit·배치(한 owner에 짧은 시간 다발 통지를 묶기)·읽음 표시.
- **실시간 WS push** — 운영 면 브라우저에 실시간 푸시(SSE/WS)는 사용자向 푸시(ADR 0011 결정 6-5가 범위 밖으로 둔 것)와 같은 영역.
- **다중 채널 fan-out** — recipient당 채널 여러 개(맵 값을 list로).

결정론은 `FakeChannel`+`Notifier` 도메인(멱등·구독 skip·발화 지점 옵셔널 주입)까지.

### 9. 페이로드 렌더 순수 함수 `render_mcp_notification` — 노출 불변식의 게이트 내 본체 (T8.2)

결정 1·2가 "본문 렌더는 어댑터 책임"으로 미룬 조각을 T8.2가 닫는다. **`Notification` → MCP 알림 페이로드(텍스트) 렌더를 순수 함수로 분리**한다:

- **`render_mcp_notification(notification: Notification) -> str`**(`notify.py`·`McpChannel` 바로 위) — `mcp_server.reply_to_mcp_text(reply: OrgReply) -> str`의 **직접 본보기**다. 같은 경계: 도메인 값(`reply_to_mcp_text`는 `OrgReply`, 이건 `Notification`)에서만 투영하므로 조직 내부값·사용자向 비밀이 *구조적으로* 새지 않는다(투영 원천에 그 필드 자체가 없다). 다른 점은 면(`reply_to_mcp_text`는 사용자向 답, 이건 운영 면 알림)과 종류 축(OrgReply sealed sum 대신 NotificationKind Literal).
- **위치 = `notify.py`**(`mcp_server.py` 아님). 렌더는 `Notification`·`NotificationKind` 도메인 값의 투영이라 같은 모듈이 응집도가 높고, `McpChannel.send`가 바로 호출한다. `reply_to_mcp_text`가 OrgReply 옆(`mcp_server.py`)에 사는 정신을 *축에 맞게* 적용 — 통지 렌더는 통지 도메인(notify.py) 옆.
- **노출 불변식(두 겹)**: ① **구조적** — `Notification`은 식별자만 든다(recipient_id·kind·subject_ref·created_at). 사용자向 질문 원문·카드 본문·조직 내부값(confidence·candidates·reason)은 *필드 자체가 없어* 실릴 수 없다(`reply_to_mcp_text`가 Answered/Pending에 그 필드가 없어 안전한 것과 동형). ② **렌더 규율** — kind별 *중립 안내 한 줄* + `subject_ref` *손잡이*만 낸다. `subject_ref`는 운영 면 식별자(case_id·item_id·intent — owner/manager가 처리함에서 "무엇 때문에"를 잇는 손잡이)라 운영 통지에 싣는 게 맞다(Inbox·Manager 큐가 내부 식별자를 노출하는 것과 같은 면). 사용자向 비밀은 애초에 `Notification`에 없어 실릴 수 없다(`reply_to_mcp_text` Pending 중립 안내 정신).
- **Literal 망라 = match+assert_never**(`reply_to_mcp_text`의 sealed sum 망라 정신을 Literal에 적용). `kind`가 Literal 4종이라 `match`로 전부 분기하고 `case _ as never: assert_never(never)`로 *빠짐을 타입 검사 시점에 막는다*. `NotificationKind`에 5번째 종류를 더하면 pyright가 이 match의 `assert_never`를 도달 가능으로 보아 에러를 낸다(검증함 — 새 종류가 렌더 누락 없이 강제). sealed sum match 망라와 같은 안전망을 Literal에서도 얻는다.
- **게이트 경계**: 렌더는 **게이트 내**(순수·SDK/IO 0·결정론 단위 테스트). 실 전송은 **게이트 밖**(결정 10 transport).

### 10. 첫 실 채널 = MCP · `McpChannel` transport 주입 실 어댑터 · Slack/Email 후속 stub (T8.2, 2026-06-24 외부 결정)

Open Questions가 "첫 후보 MCP(외부 의존 0)이나 게이트 밖이라 지금 안 정함"으로 남긴 것을 **2026-06-24 외부 결정 3건**으로 확정한다:

- **① 첫 실 채널 = MCP.** 제품이 MCP 서버라 외부 의존 0이 강점 — 먼저 닫는다. 채널 중립은 유지(MCP는 1급이 아니라 어댑터 중 *첫 번째*일 뿐 — 포트는 그대로 채널 어휘를 안 가정).
- **② fire-and-forget MVP 확정(가볍게).** `Notifier`의 현재 동작(동기 `send`·실패 삼킴, 결정 5)이 곧 fire-and-forget이라 **추가 인프라 0**. dead-letter·재시도·재연결은 결정 8이 "후속"으로 남긴 그대로 — 이번에 만들지 않는다(과도 엔지니어링·결정과 배치).
- **③ Slack/Email = 후속 stub.** "결정됨(2026-06-24): MCP 먼저·이 채널은 *후속 자리*"로 명문화(지금 "후속 판단"이 미정처럼 읽히던 걸 "결정 후 보류된 자리"로). 실 구현 0 — `NotImplementedError` 유지(채널 중립이라 같은 포트에 언제든 붙음).

**`McpChannel` = transport 주입 실 어댑터(stub 탈출).** 렌더(결정 9·게이트 내)와 전송(게이트 밖)을 transport 주입으로 가른다 — `GitGateway`↔`FakeGitGateway`·`ClaudeRunner` 주입과 **동형**(transport를 별 포트로 격리하지 않고 `send_fn` 함수 주입으로 가볍게 — recipient_id·payload 둘만 받는 좁은 시그니처라 포트 무게가 불요):

- **`McpChannel(send_fn: Callable[[str, str], None] | None = None)`** — `send`는 ① `payload = render_mcp_notification(notification)`(렌더·게이트 내) ② `self._send_fn(notification.recipient_id, payload)`(전송).
- **Fake send_fn 주입 → 게이트 내** — 렌더 결과·호출 인자(recipient_id·payload)를 결정론으로 검증(`FakeChannel`이 메모리 inbox로 검증되는 것과 같은 결).
- **실 MCP transport 주입 → 게이트 밖 수동**(mcp-runtime-engineer). recipient_id(User.id) → MCP 세션/엔드포인트 변환은 *transport 안에서*(채널 중립 — `McpChannel`도 MCP 세션 어휘를 안 가정).
- **send_fn 미주입(None) → `NotImplementedError`**("실 MCP transport 필요" 명시). 자리만인 Slack/Email stub과 달리, McpChannel은 *결정된 실 채널*이라 transport만 주면 실제로 돈다(no-op 아닌 명시 실패 — 미주입을 조용히 삼켜 통지가 사라지면 운영이 모른다).
- **fire-and-forget 계층**: `send_fn`이 던지면 `McpChannel.send`는 안 삼킨다 — `Notifier`가 fire-and-forget로 삼킨다(결정 5·계층 책임 분리). `Notifier`는 무변경(추가 인프라 0).

**기각**:
- transport를 별 포트(Protocol)로 격리 — recipient_id·payload 둘만 받는 좁은 시그니처라 포트 무게가 불요(`send_fn` 함수 주입으로 충분 — `commit_okf_bundle(..., propagator=...)`이 별 포트 없이 함수 주입인 정신).
- send_fn 미주입 시 no-op — 통지가 조용히 사라져 운영이 모른다. NotImplementedError로 "실 transport 필요"를 명시.
- Slack/Email까지 이번에 구현(다중 채널 fan-out) — MVP는 MCP 한 채널부터(결정 8 fan-out open question·과도 엔지니어링 회피).

## Considered Options

### 발화 지점 단일 추상 (결정 4)
- **(B) 오케스트레이션이 적재 직후 notify·인터페이스 통일(선택)** — store는 순수 보관(전이 ≠ 기록 ≠ 통지), 발화는 호출자. 단일 추상 = `Notifier.notify` 포트 호출 모양 하나(한 함수 아님). `propagator=None` 옵셔널 주입과 동형·결정론.
- **(A) store가 통지(기각)** — store가 전이 보관 + 외부 부작용을 겸해 책임이 샌다. store 테스트 오염.
- **전역 처리함 적재 이벤트 버스(기각)** — 발화 맥락이 제각각인 네 지점을 억지 통합하는 과추상. 인터페이스 통일로 충분.

### 분산 인프라 재사용 (결정 5)
- **패턴만 재사용(멱등 키·at-least-once 정신)(선택)** — 통지는 fire-and-forget push라 작업 디스패치(round-trip·claim/submit·큐 상태기계)와 다른 도메인. 코드 직접 재사용은 과결합. 검증된 *정신*을 새 코드로 따른다(reeval.py가 BackupReview 패턴 복제한 정신).
- **`WebSocketDispatcher` 코드 직접 재사용(기각)** — 통지가 필요 없는 claim/submit·답 회수·큐 단조 종착에 결합. 도메인이 다르다.

### `Notification.kind` 표현 (결정 2)
- **`Literal`(선택)** — 네 종류가 필드 구조 동일·분기 없는 라벨이라 `Literal`이 정확(`ReevalItem.subject_kind`와 같은 판단). sealed sum은 *처분*(행위 선택)에 둔다.
- **sealed sum(기각)** — 각 변이가 같은 필드를 들어 sealed sum의 이득(타입별 다른 필드·match 분기)이 0. 무게만 는다.

### 구독 표현 (결정 3)
- **`Notifier`가 주입 맵(선택)** — 정적 매핑엔 store 불요. 데모는 한 `FakeChannel`에 전원 매핑. 동적 구독 생기면 store 승격.
- **`SubscriptionStore` 포트 신설(기각)** — 전이 없는 정적 데이터에 store 패턴은 과도. 동적 구독이 실제 요구일 때.

## Consequences

- **`notify.py` 신규 모듈** — `NotificationChannel`(Protocol)·`Notification`(frozen)·`NotificationKind`(Literal)·`FakeChannel`(결정론 — 메모리 inbox·전달 로그)·실 어댑터 자리(`SlackChannel`·`EmailChannel`·`McpChannel` — 게이트 밖·`NotImplementedError`)·`Notifier`(발화·구독 맵·멱등). `oidc.py`·`git_gateway.py`·`reeval.py` 구조를 본보기로.
- **발화 지점에 `notifier: Notifier | None = None` 옵셔널 주입** — MVP 슬라이스는 ConflictCase open·ReevalItem add 두 지점부터(나머지 확장). `None`이면 기존 동작(하위호환·게이트 보존), 비`None`이면 적재 직후 `notify` 1회. `commit_okf_bundle(..., propagator=None)`과 동형.
- **store·기존 처리함 pull 무변경** — push는 추가지 대체가 아니다(결정 6). 기존 store 테스트·`pending_for_owner`/`pending_for_manager` 그대로.
- **불변식 영향 없음** — 위 결정 7(미아 없음은 pull이 떠받침·Authority 중앙·전이≠기록·전달 로그는 audit과 별 축·노출 불변식·등록 무결성).
- **갱신 대상**: CONTEXT(Conflict & learning 또는 Authn & planes 인접에 `NotificationChannel`·`Notification`·`Notifier`·`FakeChannel`·구독 신규 용어·Inbox 절에 "pull에 push 추가" 주석)·PRD §6 ④(T7.4 설계·shape·ADR 0022)·TRD §4(포트)·§5(진입점)·§9(notify.py)·tasks T7.4(설계·shape 완료 주석·체크박스는 구현 전이라 [ ] 유지).

## Open Questions (게이트 밖·후속)

- **실 채널 어댑터 — MCP 부분 진행(2026-06-24·T8.2 결정 9·10)**: 첫 실 채널 = MCP 확정. `McpChannel`이 transport 주입 실 어댑터로 stub 탈출(렌더 순수 함수 `render_mcp_notification`은 게이트 내·결정론, 실 MCP wire-send는 `send_fn` 주입으로 게이트 밖). **남은 게이트 밖**: 실 MCP server-initiated notification transport 구현(recipient_id → MCP 세션/엔드포인트 변환·실 클라 연결·end-to-end 시연 — mcp-runtime-engineer/수동). **Slack/메일**은 "결정 후 보류된 자리"(후속 stub·외부 결정·새 의존성 판단 — 결정 10③).
- **실 비동기 전달** — 워커·재연결·dead-letter·at-least-once(채널 재시도). fire-and-forget MVP 확정(결정 10②) — 이번 범위 밖 그대로.
- **동적 구독** — `SubscriptionStore` 포트 승격·구독 관리 UI·다중 채널 fan-out.
- **rate limit·배치·읽음 표시** — 통지 빈도 제한·다발 묶기·읽음 상태.
- **실시간 WS/SSE push** — 운영 면 브라우저 실시간 푸시(사용자向 푸시와 같은 영역·ADR 0011 결정 6-5 범위 밖의 연장).
- **나머지 발화 지점 — 닫힘(2026-06-23)**: BackupReview add(`transport.py` backup 답 종착→owner에게 `backup_review_added`)·Manager enqueue(`ask_org.py` unowned·dispatch·deadlock 세 경로→manager에게 `manager_escalated`) 발화 구현 완료. 발화 지점 4개 전부(ConflictCase·Reeval·Manager·BackupReview)가 같은 `Notifier.notify` 인터페이스를 탄다.
- **reeval 두 축 subject_ref 멱등 충돌(code-reviewer m1) — 닫힘(2026-06-23)**: 통지 subject_ref에 축 네임스페이스(`precedent:{intent}`/`answer:{idx}`)를 붙여 멱등 키 충돌을 차단했다(intent="0"·idx=0이어도 `precedent:0`≠`answer:0`). `ReevalItem.subject_ref` 도메인 값은 무변경(통지 인자에만 prefix). kind 분리(대안)는 NotificationKind 4종을 유지하려 채택 안 함.
