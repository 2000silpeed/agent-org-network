# owner PC 오프라인 폴백은 owner가 위임한 백업 워커로 한다 — 중앙 공용 LLM이 아니라

상태: accepted (2026-06-21, 결정 1~6) · **보강 accepted (2026-06-21, 결정 7~10 — ① owner 복귀 검토 루프 ② timeout 예산 분배 ③ 동기화·staleness ④ cold start, 4축. 결정 1~6의 정밀화·확장이지 번복 아님)** · ADR 0010("답변 주체 = owner Claude Code")의 *답변 환경* 보강 · ADR 0011(분산 전송·디스패처·작업 큐)의 폴백 단계 확장 · **→ ADR 0017로 재포지셔닝(번복 아님)**: 백업 워커는 *기본 가용성 경로*에서 **사설 데이터 커넥터 옵션(옵션 B)의 하위 케이스**로 강등된다 — 중앙 실행이 기본(24/7·최신 읽기)이라 owner PC 부재 폴백이 1급 가용성 문제가 아니게 됐다. 단 백업의 *owner 위임·신뢰 하향·복귀 검토 루프*(`BackupReview`)는 ADR 0017이 거버넌스 능력("답 검토·정정")으로 **승격·보존**한다. 백업이 *가용하려면 중앙 호스팅*이라는 자기모순(결정 1~2)이 "중앙 실행이 기본"의 직접 근거가 됐다. · **구현 accepted (2026-06-21, T6.6 슬라이스 i~iv — mcp-runtime·tdd-engineer)**: 등급 라우팅(primary→backup 폴백 push·`mode=backup` 강제 하향, 슬라이스 i)·t1/t2 timeout 예산+staleness 거부(슬라이스 ii)·owner 복귀 검토 루프(`BackupReviewItem`·`BackupReview`[Approve/Correct/Dismiss]·`BackupReviewStore`, 슬라이스 iii)·백업 워커 실 배치 시연+검토 UI 와이어링(슬라이스 iv). 게이트 313 passed, pyright 0, ruff 0. **실 claude backup end-to-end 시연 성공** — primary 부재→backup 실 claude 답·`mode=backup`·owner 검토. *실 데이터 동기화·격리 인스턴스 배치·암호화·키 관리·cold 기동 오케스트레이션은 연결점/후속*(설계만, 손대지 않음).

## 맥락

PRD §1은 "A가 휴가면 누가 잇지"를 풀어야 할 문제로 든다. ADR 0011은 owner PC의 워커가 중앙에 아웃바운드 WS로 붙어 답을 만드는 분산 전송을 못박았고, owner 부재·timeout은 `DispatchOutcome.EscalatedToManager`(사람 그래프 상향)로 종착시켜 *미아 없음*을 지켰다. 그러나 현재 폴백은 **owner PC가 꺼지면 곧장 사람(Manager)으로 올린다** — 그 사이를 *자동으로* 메우는 단계가 없다. owner가 잠깐 자리를 비웠을 뿐인데 모든 질문이 Manager 큐로 쌓이면, "자기 지식으로 답한다"는 가치가 가용성 구멍에서 무너진다.

ADR 0010은 답변 주체를 "각 Owner의 Claude Code(owner PC)"로 못박았다. 가용성을 위해 *중앙 공용 LLM이 대신 답하게* 하면 이 결정과 PRD 핵심원칙(중앙 무지식·답은 담당이 한다)을 정면으로 깬다 — 중앙이 모르는 척 답해 버린다. 그래서 가용성 확장은 ADR 0010을 *깨지 않고 보강하는* 방향이어야 한다.

**확정 사실(사용자 결정):** owner PC 오프라인 시 답변 주체는 **owner가 위임한 백업 워커**다. 백업은 *owner가 명시적으로 위임한 자기 데이터·자기 신원으로 도는 격리 인스턴스*이며, 물리적으론 중앙 인프라에 호스팅되지만 논리적으론 *여전히 owner가 답한다*. 중앙은 여전히 무지식 — owner가 위임한 격리 스냅샷만 백업이 쓰고, 중앙이 owner 데이터를 임의로 보지 않는다. 백업 답은 owner 본인 실시간 답과 달리 **스냅샷 기반·owner 미검토**라 *신뢰 레이블을 하향*해야 한다.

이 ADR이 닫을 되돌리기 어려운 결정: (1) 폴백 단계·트리거, (2) 백업 워커의 도메인 표현, (3) 데이터 위임·동기화 모델, (4) 신뢰 레이블, (5) 격리·책임 연결점, (6) ADR 0010 보강 문구. **실 구현(데이터 동기화·격리 인스턴스 배치·암호화·키 관리)은 이 ADR 범위 밖** — shape와 연결점만 둔다.

**보강(결정 7~10, 2026-06-21).** 위 결정 1~6은 폴백 *사슬과 표현*을 닫았으나 네 군데가 비어 있었다 — 4축으로 보강한다. ① **owner 복귀 검토 루프(가장 큰 공백):** `mode=backup` 답은 *owner 미검토*인데 영영 미검토로 남으면 "책임은 owner인데 owner는 모르는" 답이 떠다닌다(결정 5의 "책임은 owner"가 명목에 그친다). owner 복귀 후 검토·정정·승격 루프가 없다. ② **timeout 예산 분배:** 결정 1은 "primary 미연결 즉시 백업 전환"으로 최소화하고 "느린 primary"를 후속으로 미뤘다 — 백업 단계가 끼면 단일 timeout을 "1차 대기→백업 전환→백업 대기→escalation"으로 나눠야 한다. ③ **동기화·staleness:** 결정 3의 `DelegationSnapshot.snapshot_at`은 *자리만* 잡고 동기화 트리거·staleness 정책·신뢰 차등을 미뤘다. ④ **cold start:** 결정 2는 warm을 택하고 cold를 후속으로 미뤘으나 cold의 *도메인 표현*(기동 트리거·대기·실패)을 안 뒀다. 이 넷도 **설계·shape·연결점만** — 실 구현(검토 UI·동기화 파이프라인·인스턴스 기동 오케스트레이션)은 후속.

## 결정

### 1. 폴백 단계 = owner PC 워커 → owner 백업 워커 → Manager escalation (3단, 미아 없음 종착)

기존 2단(owner PC 워커 → Manager escalation) 사이에 **owner 백업 워커**를 끼운다.

1. **owner PC 워커**(1차, ADR 0011) — owner PC가 연결돼 있으면 그 소켓으로 push. owner 본인 실시간 답.
2. **owner 백업 워커**(2차, 신규) — owner PC 워커가 미연결이거나 push 후 timeout이면, 같은 owner의 *백업 연결*로 push한다. owner가 위임한 격리 스냅샷으로 답(신뢰 하향).
3. **Manager escalation**(3차, ADR 0011 결정 3 그대로) — 백업도 미연결/실패/timeout이면 기존 `EscalatedToManager`로 사람 그래프 상향. 종착.

**미아 없음 보존:** 사슬은 반드시 3차(Manager escalation)에서 종착한다 — 백업이 없거나(opt-in 안 함) 백업도 부재면 곧장 기존 종착으로 떨어진다. 백업은 *새 종착이 아니라 종착 전 자동 회복 시도 1단*이다. 큐 도메인(`InMemoryWorkQueueDispatcher`)의 단조 종착·timeout escalation은 그대로 — 백업은 "어느 연결로 push하나"를 한 단계 늘릴 뿐, 큐 상태기계(queued↔claimed↔answered↔expired)를 바꾸지 않는다.

**ADR 0011 결정 3과의 정합:** 결정 3은 "owner 부재·timeout → 기존 Manager escalation 재사용"이다. 이 ADR은 그 *직전에* 백업 시도를 한 단계 삽입할 뿐 escalation 경로 자체는 건드리지 않는다. 백업이 답하면 escalation이 *안 일어나는* 것이고, 백업이 실패하면 결정 3이 그대로 발동한다.

**timeout 예산 분배(연결점, 실 튜닝은 후속):** 기존 단일 timeout(`InMemoryWorkQueueDispatcher.timeout`, 기본 120s)을 백업 단계가 끼면 "1차 대기 → 백업 전환 → 백업 대기 → escalation"으로 나눠야 한다. MVP shape은 *전환 트리거를 timeout 분할이 아니라 "1차 미연결 즉시 전환"으로 최소화*한다(아래 결정 2) — 1차 워커가 연결돼 있는데 느린 경우의 중간 timeout 분할은 후속 튜닝으로 미룬다.

### 2. 백업 워커 = `WorkerLogic` 재사용 + 디스패처 연결 레지스트리를 owner당 "우선 PC, 차선 백업"으로 확장 (새 워커 추상 없음)

**백업 워커는 새 추상이 아니라 기존 `WorkerLogic`을 그대로 재사용한다.** 백업도 `claim`/`submit`(논리적으로) 워커이며 `PushWork`→`ClaudeCodeRuntime`→`SubmitAnswer`의 같은 프레임 흐름을 탄다. 다른 점은 *어디서 도는가*(owner PC가 아니라 중앙 호스팅 격리 인스턴스)와 *무엇을 들고 도는가*(owner PC의 실데이터가 아니라 위임 스냅샷)뿐 — 둘 다 `WorkerLogic`의 생성자 인자(`owner_id`·`cards`·`runtime`)로 흡수된다. 워커 코드(`worker.py`)는 **변경 불필요**.

**디스패처 확장이 본체다.** 현재 `WebSocketDispatcher._connections: dict[str, SendFrame]`는 owner당 연결 1개만 보관한다(`owner_id → send`). 이를 **owner당 워커 등급(role) 우선순위**로 확장한다:

```python
# 현재 (owner당 1 연결)
_connections: dict[str, SendFrame]

# 확장 (owner당 등급별 연결, 우선순위 push)
WorkerRole = Literal["primary", "backup"]   # primary=owner PC, backup=owner 위임 백업
_connections: dict[str, dict[WorkerRole, SendFrame]]
#   owner_id → {role → send}. push 대상 선택은 우선순위 — primary 있으면 primary, 없으면 backup.
```

- `RegisterWorker`에 `role: WorkerRole = "primary"` 필드를 더해 워커가 자기 등급을 선언한다(PC 워커는 기본 `primary`, 백업 인스턴스는 `backup`). 신원(어느 owner)은 그대로 `owner_id`, 등급은 그 owner 안에서의 우선순위.
- `_push_pending`(현재 `claim`→`PushWork`)의 *연결 선택*만 우선순위로 바꾼다: 그 owner의 `primary`가 연결돼 있으면 거기로, 없고 `backup`이 연결돼 있으면 거기로 push. 둘 다 없으면 큐 대기(기존 `AwaitingWorker` → timeout이면 `EscalatedToManager`). claim/submit/큐 도메인은 무변경.

**warm vs cold(백업 상시 대기 vs 오프라인 감지 시 기동):** MVP shape은 **warm(상시 연결)**을 택한다 — 백업 워커도 PC 워커처럼 중앙에 아웃바운드 WS를 *미리* 걸어두고 대기하다가, 디스패처가 primary 부재 시 그 연결로 push한다. 근거: (a) cold start(오프라인 감지→인스턴스 기동→연결)는 "인스턴스 라이프사이클 관리"라는 새 인프라 축을 부르는데, 이는 *실 배치* 영역이고 이 ADR의 도메인 shape 범위를 넘는다. (b) warm은 기존 WS 연결 레지스트리에 등급만 더하면 되어 자산 재사용이 깔끔하다. cold 기동(비용 절감)은 후속 인프라 결정으로 미룬다 — 도메인 모델(우선순위 push)은 warm/cold 어느 쪽이든 동일하게 작동한다(cold면 "backup 연결이 늦게 뜬다"일 뿐).

**왜 새 워커 타입이 아닌가:** 백업도 결국 "owner를 대리해 로컬(격리) claude로 답하는 워커"다 — `WorkerLogic`의 정의 그대로다. 새 타입을 만들면 프레임 핸들링·폴백 답·멱등을 두 곳에서 유지하게 된다. 차이(스냅샷 데이터·격리 환경)는 *주입 인자*와 *배치*의 차이지 *로직*의 차이가 아니다. 그래서 재사용 + 디스패처 라우팅 확장이 정합적이다.

### 3. 데이터 위임 = opt-in 위임 레코드(AgentCard 자기보고 필드 아님), 스냅샷 staleness가 신뢰에 반영

owner가 백업에 무엇을 위임하는가 — 현재 `knowledge_sources`(출처 레이블)와, 장래의 실 지식 인덱스(ADR 0010이 후속으로 둔 RAG)의 스냅샷.

**위임은 AgentCard 필드가 아니라 별도 위임 레코드(Delegation)로 표현한다.** 근거:
- **Authority 중앙 원칙(ADR 0004)과의 정합** — AgentCard는 *under-claim만 자기보고*한다. "이 카드를 백업에 위임한다"는 가용성·보안 정책이지 담당 영역 선언이 아니다. 카드에 박으면 카드의 책임이 비대해지고, 위임 staleness(언제 스냅샷 떴나)·격리 키 같은 *운영 상태*를 frozen 값 객체에 끼우게 된다(부적합).
- **opt-in의 명시성** — 위임은 owner의 *명시적* 행위여야 한다(중앙 무지식 보존의 핵심 — owner가 위임하지 않은 데이터는 백업도 못 본다). 별 레코드면 "위임함/안 함"이 1급으로 드러나고, 위임 없는 owner는 백업 단계를 건너뛰어 곧장 Manager escalation으로 떨어진다(결정 1의 사슬과 정합).

shape(코드 아님, 도메인 스케치):

```python
@dataclass(frozen=True)
class DelegationSnapshot:
    """owner가 백업 워커에 위임한 격리 스냅샷의 메타(실 데이터 본체는 격리 저장소).

    이 레코드 자체는 *위임 사실과 최신성*만 든다 — 실 지식 본체(문서·인덱스)는
    owner별 격리 저장소에 있고 백업 인스턴스만 접근한다(중앙 무지식 보존). 중앙/
    디스패처는 이 메타로 "백업이 답할 수 있는가·얼마나 최신인가"만 판단한다.
    """
    owner_id: str
    agent_ids: tuple[str, ...]      # 위임 대상 카드(어느 담당 영역을 백업이 답하나)
    snapshot_at: datetime           # 스냅샷을 뜬 시각 — staleness 판정·신뢰 레이블 기준
    # 실 데이터 위치·암호화 키 핸들은 *연결점만*(아래 결정 5, 실 구현 후속)
```

**staleness → 신뢰 반영:** `snapshot_at`이 오래될수록(예: 정책 임계 초과) 백업 답의 신뢰가 더 낮아진다. MVP shape은 "백업 답이면 일률 하향"(결정 4)으로 최소화하고, *staleness 등급에 따른 세분 하향*(fresh backup vs stale backup)은 후속으로 둔다 — `DelegationSnapshot.snapshot_at`이 그 자리를 예고한다.

**중앙 무지식 보존:** 중앙·디스패처는 `DelegationSnapshot` *메타*(누가·무엇을·언제)만 본다. 실 지식 본체는 owner별 격리 저장소에 있고 *백업 인스턴스만* 접근한다(owner 키로). 중앙이 owner 문서를 모아 보지 않는다 — ADR 0006·0010 그대로.

### 4. 신뢰 레이블 = `Answer.mode`에 신규 상태 `backup` 추가, OrgReply.Answered가 그대로 노출

백업 답을 어떻게 표시하나. 현재 `Answer.mode: Literal["full", "draft_only"]`(신뢰 상태)를 **`backup`을 더해 셋으로** 확장한다:

```python
AnswerMode = Literal["full", "draft_only", "backup"]
#   full:        owner 실시간 답, 그대로 사용자에게
#   draft_only:  Approval 게이트 — 사람 승인 전까지 초안
#   backup:      owner 위임 백업 워커의 스냅샷 기반 답(owner 미검토) — 신뢰 하향
```

근거(새 타입/필드가 아니라 mode 확장):
- **PRD §3 line 20 정합** — "답엔 담당·승인·출처(책임·신뢰)가 붙는다". `mode`가 바로 그 *신뢰 상태*다(CONTEXT Answer 절). 백업도 신뢰 상태의 한 값이므로 같은 축에 둔다. 별 필드(`is_backup: bool`)를 더하면 신뢰 상태가 두 축으로 흩어진다.
- **OrgReply.Answered가 이미 mode를 노출** — `Answered(text, answered_by, mode, sources)`가 mode를 사용자에게 투영한다(`full`/`draft_only`). `backup`이 그 자리에 흘러 사용자向 표현(예: "담당 부재 중 백업 답변 — 추후 담당 확인 가능")으로 노출된다. **노출 불변식과 양립** — `mode`는 *원래 노출하는 신뢰 상태값*이지 조직 내부 구조(manager_id·후보·ticket)가 아니다. `backup`은 사용자가 알아야 할 신뢰 정보다("이건 본인 실시간 답이 아니다").
- **`draft_only`와의 조합** — owner 백업 답인데 Approval 게이트도 걸린 경우는? MVP shape은 **`backup`이 `draft_only`를 우선**한다(백업 답은 어차피 owner 미검토라 더 강한 하향). 즉 둘이 겹치면 `backup`. 세분(backup+draft 별도 표시)은 후순위 — Approval 평가 자체가 T2.5 영역이라 이 ADR 범위 밖.

**전송 보존:** `AnswerFrame.mode`(transport.py)도 같이 `backup`을 받게 확장한다 — 백업 워커가 `SubmitAnswer`로 회신할 때 mode를 실어 내려, `Delivered.answer.mode`→`Answered.mode`로 보존된다(ADR 0011 Approval 연결점과 같은 자리). 백업 워커가 `mode="backup"`을 *스스로 세팅*하는가, 디스패처가 *백업 연결로 push했으니* 회신 mode를 강제 하향하는가 — **디스패처가 강제 하향**을 택한다(아래 결정 5의 책임 정합 — 백업이라는 사실은 *연결 등급*이 진실이고, 워커 자기보고에 맡기면 누락·위조 여지). shape: `WebSocketDispatcher.submit`이 그 ticket이 backup 연결로 처리됐으면 `Answer.mode`를 `backup`으로 덮어(또는 합류) 큐에 회신.

**결정 4 정밀화(마지막으로 push된 등급이 진실):** "backup 연결로 push됐나" 판정은 *마지막 push 등급*을 기준으로 한다. 구체적으로: backup으로 push되면 `_backup_tickets`에 ticket_id를 추가하고, 이후 같은 ticket이 **primary로 재push되면 `_backup_tickets.discard(ticket_id)`**한다(경계 A — backup→disconnect→primary 재push 시 backup 표식 해제). 또한 **submit으로 종착된 후 `_backup_tickets.discard(ticket_id)`**해 무한 누적을 막는다(경계 B — 메모리 누수 제거). "한 번이라도 backup으로 push됐나"가 아니라 "가장 최근 push가 backup이었나"가 진실의 기준이다.

### 5. 격리·보안·책임 = owner별 격리 + 책임은 owner(opt-in 위임), 암호화·키는 연결점만

- **격리:** 백업 인스턴스는 owner별로 격리된다 — 데이터(위임 스냅샷)도 인스턴스도. 한 백업이 여러 owner 데이터를 섞어 보지 않는다(중앙 무지식의 인스턴스 레벨 강제). shape은 `DelegationSnapshot.owner_id` 귀속과 `RegisterWorker.owner_id`+`role=backup`로 *연결점만* 둔다.
- **암호화·owner 키:** 위임 스냅샷은 owner 키로 암호화돼 백업 인스턴스만 복호화한다 — *연결점만 명시*하고 실 키 관리·암호화는 후속(ADR 0009 인증 연계). `DelegationSnapshot`에 키 핸들 자리를 주석으로 예고.
- **책임 연결:** 백업 답도 **owner 책임**이다 — owner가 *명시적으로 opt-in 위임*했으므로(PRD "누가 책임지지"). `Answered.answered_by`는 그대로 `(owner, agent_id)` — 백업이 답해도 담당은 그 owner다(논리적으로 owner가 답함). `mode=backup`이 "본인 실시간 검토는 아님"을 신뢰 차원에서 덧붙일 뿐, 담당·책임 귀속은 owner 불변.
- **워커 인증(ADR 0009 → T6.5):** 백업 워커도 PC 워커처럼 `RegisterWorker.token`으로 owner 신원을 인증한다 — *진짜 그 owner가 위임한 백업인지* 검증할 자리(미인증 백업의 `SubmitAnswer` 거부). 기존 인증 거부 hook(`WebSocketDispatcher._authenticate`)에 등급 검증이 합류한다(실 토큰 검증은 T6.5).

### 6. ADR 0010 보강 = "owner Claude Code(owner PC)" → "owner가 위임·관리하는 환경(owner PC 또는 owner 백업 인스턴스)" (0010 직접 수정 않고 0012가 보강·참조)

ADR 0010은 "답변 주체 = 각 Owner의 Claude Code, owner PC"로 못박았다. 이 ADR은 그것을 **깨지 않고 보강**한다:

> 답변 주체는 *owner가 위임·관리하는 환경*에서 도는 Claude Code다 — 평상시엔 **owner PC**(ADR 0010 그대로), owner PC 부재 시엔 **owner가 명시적으로 위임한 백업 인스턴스**(owner 자기 데이터·자기 신원의 격리 스냅샷). 어느 쪽이든 *여전히 owner가 답한다* — 중앙 공용 LLM이 아니다. 중앙은 무지식을 유지한다(owner가 위임한 격리 스냅샷만 백업이 쓴다).

**0010을 직접 수정하지 않는다** — 0010은 "owner PC"를 1차 진실로 유지하고, 0012가 그 *가용성 확장*을 보강·참조한다(ADR 0011이 0010의 전송 계층을 보강한 것과 같은 패턴). 0010의 핵심("중앙 API 키 LLM 아님·답은 owner가")은 백업에서도 *그대로* 성립한다 — 백업은 owner의 위임 환경이지 중앙의 공용 추론이 아니다.

---

## 보강 결정 7~10 (4축, 2026-06-21 — 결정 1~6의 정밀화·확장, 번복 아님)

### 7. owner 복귀 검토 루프 = ConflictCase/Inbox 패턴 재사용한 `BackupReviewItem` + `BackupReviewStore`(Owner 처리함 확장), 검토 결과는 sealed sum `BackupReview` (가장 큰 공백을 닫음)

결정 5는 "백업 답도 owner 책임(opt-in 위임)"이라 못박았으나, `mode=backup` 답은 *owner가 본 적 없는* 답이다. owner가 돌아왔을 때 그 답들을 **보고·정정·승격**할 길이 없으면 책임은 명목에 그친다 — "owner 책임이라면서 owner는 그 답을 모른다". 이 루프를 닫는다.

#### 7-1. 검토 대상 보관 = `ConflictCase`/`Inbox`/`ConflictCaseStore` 패턴을 그대로 빌린 `BackupReviewItem` + `BackupReviewStore` (새 추상 최소화)

**기존 처리함(Inbox) 패턴이 정확히 맞는다.** ConflictCaseStore는 "Owner별 미해소 항목을 보관·조회하는 포트"(`open_for_owner`=처리함)이고, 검토 루프도 "Owner별 *미검토* 백업 답을 보관·조회"라 *구조가 동형*이다. 그래서 새 저장 메커니즘을 발명하지 않고 같은 정신(Protocol + InMemory, `open_for_owner` 색인, open→resolved 전이)을 빌린다.

- **보관 단위 = `BackupReviewItem`(frozen 값 객체).** 백업이 owner 이름으로 답한 한 건 — 검토 대기 상태. `ConflictCase`가 미해소 *다툼*을 담듯, 이건 미검토 *백업 답*을 담는다.

  shape(도메인 스케치, 코드 아님):
  ```python
  ReviewStatus = Literal["pending_review", "reviewed"]   # ConflictCase.status와 같은 결

  @dataclass(frozen=True)
  class BackupReviewItem:
      """백업이 owner 이름으로 답한 한 건의 검토 대기 항목.

      owner가 복귀해 보고·정정·승격할 대상. ConflictCase가 미해소 다툼을 담듯,
      이건 *미검토 백업 답*을 담는다 — Owner 처리함(Inbox)의 두 번째 면.
      답 본문(text)을 보관해 owner가 "백업이 내 이름으로 뭐라 답했나"를 보고
      정정 판단을 내린다(ConflictCase가 question 원문을 보관하는 것과 같은 정신).
      """
      owner_id: str                 # 누구 이름으로 답했나(처리함 귀속 키)
      agent_id: str                 # 어느 담당 영역(카드)의 답인가
      question: str                 # 원 질문(owner가 맥락 판단)
      backup_answer_text: str       # 백업이 낸 답 본문(owner가 정정 판단의 근거로 봄)
      ticket_id: str                # 어느 작업의 답인가 — audit·재노출(7-3) 연결 키
      snapshot_at: datetime         # 그 답이 쓴 위임 스냅샷 시각(staleness 맥락, 결정 9)
      answered_at: datetime         # 백업이 답한 시각(주입 clock 결정론)
      item_id: str                  # = ticket_id 재사용(별 ID 불요 — 1답 1검토)
      status: ReviewStatus = "pending_review"
      review: "BackupReview | None" = None   # reviewed일 때만(아래 7-2)

      def review_with(self, review: "BackupReview") -> "BackupReviewItem":
          """검토 결과를 안은 reviewed 항목을 새로 만든다(item_id 보존, 불변+새 인스턴스).
          ConflictCase.resolve()와 같은 전이 — 파괴적 변경 X."""
          ...
  ```

- **보관 포트 = `BackupReviewStore`(Protocol + `InMemoryBackupReviewStore`).** `ConflictCaseStore`와 *같은 메서드 모양*:
  ```python
  class BackupReviewStore(Protocol):
      def add(self, item: BackupReviewItem) -> None: ...           # open_case 대응
      def get(self, item_id: str) -> BackupReviewItem | None: ...
      def pending_for_owner(self, owner_id: str) -> list[BackupReviewItem]: ...  # open_for_owner=처리함
      def mark_reviewed(self, item: BackupReviewItem) -> None: ...  # mark_resolved 대응
  ```
  `pending_for_owner`가 **owner 복귀 시 "내가 검토할 백업 답들" 조회**다 — `open_for_owner`(다툼 처리함)와 한 면(Owner 처리함)의 두 탭.

- **생성 트리거 = 백업 submit 시 자동 add(루프 진입점).** `BackupReviewItem`은 *백업 연결로 처리된 답이 회신될 때* 생긴다 — 결정 4의 "디스패처가 백업 연결 답에 `mode=backup` 강제 하향"이 일어나는 *바로 그 자리*에서, 디스패처가 `BackupReviewStore.add(item)`도 함께 호출한다(backup으로 답했다는 사실=연결 등급이 진실이므로 생성도 디스패처가 책임, 워커 자기보고 아님). 즉 "mode=backup 하향"과 "검토 항목 생성"은 한 사건의 두 면이다. Delivered(primary 답)·full 답엔 생성 안 함(검토 불요 — owner 실시간). 미아 없음과 정합: 백업 답은 큐에선 종착(answered)이되 *검토 차원에선* pending_review로 열려 owner를 기다린다(다툼이 큐와 별개로 Inbox에 열려 있듯).

- **새 추상이 최소인가(판단):** `ConflictCaseStore`를 *그대로 재사용*하지 않고 *별 포트*를 둔다. 근거: (a) 담는 값이 다르다(`ConflictCase`=다툼·후보 vs `BackupReviewItem`=백업 답·검토). 한 store에 두 타입을 섞으면 `open_for_owner`가 무엇을 돌려주는지 모호해지고 ConflictCase의 망라가 깨진다. (b) 그러나 *패턴은 100% 재사용* — Protocol + InMemory, owner 색인, 불변 전이. 새 메커니즘이 아니라 검증된 모양의 두 번째 인스턴스다(`AuditLog`·`PrecedentStore`·`ConflictCaseStore`가 같은 패턴의 다른 인스턴스이듯). 위치는 별 모듈 대신 `conflict.py`가 아니라 — 검토는 *다툼 도메인*이 아니라 *답 가용성 도메인*이라 — **`dispatch.py`나 신규 작은 모듈**(구현 단계 판단). 과도 분리 회피와 도메인 경계 사이는 구현자가 택한다(권장: 디스패치/가용성 인접이라 `dispatch.py` 또는 `review.py`).

#### 7-2. 검토 행위 = sealed sum `BackupReview`(Approve | Correct | Dismiss), ConsensusOutcome/Resolution 정신

owner의 검토 행위는 셋 중 하나다 — "타입이 곧 상태"(RoutingDecision·ConsensusOutcome·DispatchOutcome 정신). `ConsensusOutcome`이 합의 시도의 세 결말이듯, `BackupReview`는 검토의 세 결말이다.

```python
@dataclass(frozen=True)
class Approve:
    """owner가 백업 답을 승인 — '이대로 맞다'. mode=backup → full 승격 여부는 7-3."""
    by_owner: str
    rationale: str = ""

@dataclass(frozen=True)
class Correct:
    """owner가 정정 — 새 답을 발행한다. 기존 backup 답을 대체할 owner 실답."""
    by_owner: str
    corrected_text: str
    sources: tuple[str, ...] = ()
    rationale: str = ""

@dataclass(frozen=True)
class Dismiss:
    """owner가 무시 — '검토했고 따로 조치 안 함'(예: 사소·이미 사용자가 됐음).
    검토 *완료* 사실은 남는다(미검토와 구분) — 책임 실질화의 최소선."""
    by_owner: str
    rationale: str = ""

BackupReview = Approve | Correct | Dismiss
```

- **`ConcurOnPrimary`와 같은 1인칭 표 정신** — `by_owner`가 자기 책임 답을 1인칭으로 처분한다(검토는 *그 owner만* 할 수 있다 — 처리함이 owner 귀속). 검토 서비스가 `item.owner_id == review.by_owner`를 강제(ConsensusService가 후보 owner를 강제하듯).
- **셋으로 충분한가:** Approve(맞다)·Correct(틀려서 고침)·Dismiss(볼 것 없음)가 검토의 망라적 결말이다. "보류"(나중에)는 *status가 여전히 pending_review*인 것이라 별 결말이 아니다(StillOpen이 별 결말 아니라 미완 상태이듯 — 여기선 그냥 미검토 유지).

#### 7-3. 이미 나간 답의 사후 정정 = audit 사후 *전이* 기록 + 재노출은 노출 불변식 유지, 새 추적 토큰 재사용

사용자는 *이미* backup 답을 받았다(retrieve로). owner가 `Correct`하면 그 정정을 어떻게 이력화·전달하나.

- **검토는 사후 *전이*, 기록은 `BackupReviewStore.history`(전이 ≠ 기록 그대로).** 검토 결과(`BackupReview`)는 *도메인 전이*(BackupReviewItem: pending_review→reviewed)다. 그 전이의 *기록*은 `BackupReviewStore.history`(append-only 전이 보관소)가 담당한다 — `mark_reviewed`가 reviewed 항목을 `history`에 append한다(원 pending 항목과 reviewed 항목 모두 남음). **audit은 질문→라우팅→디스패치→답의 절차 전용**이라 검토 전이를 audit에 남기지 않는다(`record_review`는 전이 위임만 하고 `AuditEntry`를 기록하지 않는다 — 거짓 `decision=Unowned` 기록 방지, code-reviewer [Major 2] 확정). 원 백업 답 audit(결정 5의 `dispatch_outcome`)은 그대로 두고, 검토 전이는 store.history가 독립적으로 보관한다.
- **사용자向 재노출 = 조회 경로 재사용 + 검토 store 우선 조회(큐 종착은 안 덮음), 노출 불변식 유지.** `Correct`로 새 답이 나오면 사용자에게 닿아야 한다 — 그러나 푸시는 ADR 0011 결정 6-5가 *범위 밖*(조회로 한정)이라 **재노출도 조회 경로(`retrieve(tracking)`)를 재사용**한다. **중요(큐 단조성 보존):** 정정은 큐의 answered(첫 답 고정·단조 종착, 결정 6-4 멱등)를 *덮지 않는다* — 큐는 "백업이 낸 첫 답"을 그대로 들고, 검토 결과는 **`BackupReviewStore`(검토 store)에 따로** 산다. `retrieve(tracking)`이 그 ticket의 `BackupReviewItem`을 *먼저 조회*해 reviewed면 검토 결과를 투영하고(Correct→정정 text·`mode=full`, Approve→큐의 원 text·`mode=full`), pending_review거나 검토 항목 없으면 기존대로 `poll`을 투영한다(backup 답 그대로). 즉 재노출은 *큐 종착 위에 검토 store를 덧씌운 읽기*지 큐 상태 변경이 아니다 — 큐 도메인 무변경·멱등·단조성 모두 보존(결정 6-2 정신 그대로). `tracking→ticket` 매핑(ask_org `_tracking`)은 검토 시점까지 살아 있어야 하므로 MVP의 "프로세스 수명=토큰 수명"으로 충분하다(TTL/GC는 후속).
- **노출 불변식 그대로.** 재노출도 `Answered(text, answered_by, mode, sources)`만, 조직 내부(검토자·item_id·검토 store 존재)는 안 샌다. `answered_by`는 여전히 owner(정정도 owner가 함).
- **정정의 mode = `full`(owner 실답).** `Correct`는 owner가 *직접* 발행한 답이라 backup 하향이 풀린다 — `mode=full`. 즉 검토 정정은 "신뢰 복원" 경로이기도 하다(backup→full, owner 실시간 검토를 거쳤으므로). 이게 7-4의 "Approve→승격"과 다른 점: Approve는 *큐의 원 backup 답 text를 그대로 두고* mode만 full로, Correct는 *검토 store의 새 text로 대체*(둘 다 큐 자체는 불변, 차이는 검토 store가 text를 바꾸나 마나).

#### 7-4. Approve의 mode 승격 = MVP는 신뢰 *상태 갱신*만(재노출 시 full), 별 mode 신설 안 함

`Approve`(owner가 "백업 답 맞다")면 그 답은 더 이상 "미검토"가 아니다 — owner가 봤다. 그럼 `mode=backup`을 `full`로 승격하나?

- **MVP: Approve된 항목의 *재노출*은 `mode=full`로 표시(승격), 단 새 mode 값은 안 만든다.** 승격은 `mode` 축의 *값 전이*(backup→full)이지 새 값이 아니다 — owner 검토를 거치면 "owner 실시간 답"과 신뢰가 같아진다(둘 다 owner가 책임지고 봤다). 별 `backup_approved` 같은 값을 만들면 mode가 비대해진다(결정 4의 "mode 최소화" 정신). 처음 받은 시점의 backup 표시는 *그때는 미검토였다는 사실*이라 사후에 거짓이 되지 않는다(audit엔 backup→approved 전이가 남음).
- **재노출 갱신과 일관:** Approve도 Correct처럼 `retrieve(tracking)`이 승격된 신뢰(full)를 돌려준다 — 차이는 Correct는 text까지 바뀌고 Approve는 text 그대로·mode만 full. 둘 다 "owner 검토 후 신뢰 복원"의 두 형태.

#### 7-5. Precedent와의 구분 = 검토 승인은 Precedent를 만들지 *않는다*

검토 승인이 Precedent를 만드나? **아니다 — 명확히 구분한다.**

- **Precedent는 *라우팅* 판례다**(어떤 intent의 다툼을 누가 맡기로 합의했나 — ConsensusOutcome.Agreed→Resolution→Precedent). 백업 답 검토는 *그 답이 맞나*의 판단이지 *누가 담당인가*의 판단이 아니다 — 담당은 이미 그 owner로 정해졌다(backup도 그 owner가 답한 것). 검토는 라우팅 결정을 바꾸지 않는다.
- **그래서 검토는 `PrecedentStore`에 안 들어간다.** 검토 기록은 `BackupReviewStore.history`(전이 보관소)에 남고, Precedent(라우터 색인)는 건드리지 않는다. 라우터가 다음 같은 질문에 참조할 건 여전히 *담당 판례*지 *답 검토 이력*이 아니다. (만약 owner가 Correct를 반복해 "백업 스냅샷이 이 영역을 자꾸 틀린다"가 드러나면, 그건 *동기화 backlog 신호*(결정 9)지 라우팅 판례가 아니다.)

#### 7-6. 책임 실질화 = 이 루프가 결정 5의 "owner 책임"을 명목에서 실질로, PRD "누가 책임지지" 완성

결정 5는 "백업 답도 owner 책임(opt-in 위임)"이라 선언만 했다. 검토 루프가 그 책임을 *행위 가능하게* 만든다 — owner가 복귀하면 자기 이름으로 나간 답을 *반드시 마주하고*(처리함 pending), 승인/정정/무시로 *처분*한다. PRD §1의 "이 답을 누가 책임지지"가 백업 경로에서도 닫힌다("owner가 위임했고, 복귀 후 검토한다"). **미검토 백업 답이 처리함에 쌓이는 것 자체가 "owner가 자리를 비운 동안 자기 이름으로 나간 답"의 가시화**다(다툼이 Inbox에 쌓이듯).

### 8. timeout 예산 분배 = primary 미연결은 즉시 백업(결정 1 유지), primary *연결됐는데 무응답*은 t1 후 백업 전환을 추가 — 백업은 primary 명백 부재 시만(중복 답 회피), 주입 clock 결정론

결정 1은 전환 트리거를 "primary 미연결 즉시"로 최소화하고 "느린 primary"를 후속으로 미뤘다. 백업 단계가 끼면 단일 timeout을 단계별로 나눠야 한다 — 이를 보강한다.

#### 8-1. 두 트리거 구분 = (a) primary 미연결(즉시 백업) vs (b) primary 연결됐으나 무응답(t1 후 백업)

- **(a) primary 미연결 = 즉시 백업(결정 1 그대로).** `_push_pending`이 primary 연결이 없으면 backup으로 push — 대기 없음. owner PC가 꺼져 있는 명백한 부재라 t1을 기다릴 이유가 없다.
- **(b) primary 연결됐으나 무응답 = t1 timeout 후 백업 전환(신규).** primary 워커가 연결돼 push를 받았는데(claimed) t1 안에 답이 안 오면, 그 작업을 backup으로 *재전환*한다. 결정 1의 "미연결 즉시"를 "느린 primary도 t1 후 전환"으로 확장.
- **timeout 예산 = t1(primary 대기) + t2(backup 대기), 합이 기존 단일 timeout 근처.** 현재 `InMemoryWorkQueueDispatcher.timeout`(120s 단일)을 **2단으로 쪼갠다** — `t1`(primary 무응답 한계, 예: 40s) 경과 시 backup 전환, `t2`(backup 무응답 한계, 예: 80s) 경과 시 escalation. 합(t1+t2)이 기존 escalation 한계 근처라 *전체 escalation까지의 시간*은 보존하면서 그 사이에 백업 시도 1단을 끼운다. (수치는 후속 튜닝 — shape은 "단일 timeout → t1/t2 2단 예산"이라는 *구조*만 못박는다.)

#### 8-2. 중복 답 방지 = 백업은 primary 명백 부재 시만 전환(t1 후 primary claim 해제 후 backup), ticket_id 멱등이 흡수

primary가 답 생성 중인데 backup도 시작하면 둘 다 답 → 자원 낭비(둘 다 claude 호출). 정책:

- **MVP: 백업은 primary가 *명백히 부재*할 때만 답한다(병렬 금지).** (a) primary 미연결이면 애초에 primary가 안 도니 충돌 없음. (b) primary 무응답 t1 경과 시엔 그 작업의 primary claim을 *해제*(release)한 뒤 backup으로 전환 — primary가 그 사이 답해도 ticket_id 멱등(첫 답 고정, 슬라이스2b 결정 6-4)이 흡수해 *두 답 중 먼저 온 것만* 채택된다. 즉 늦게 온 쪽(primary든 backup이든)의 답은 버려진다(자원은 일부 낭비되나 *답 일관성*은 보존 — 사용자는 한 답만 받는다).
- **t1 후 병렬 허용은 후속.** "primary도 계속 돌게 두고 backup도 시작해 먼저 오는 답 채택"(속도 우선)은 자원 낭비가 크고 정책이 복잡하다(어느 답을 audit에 남기나·mode 결정). MVP는 *순차 폴백*(release 후 전환)으로 단순화하고, 병렬은 후속 성능 결정으로 미룬다.
- **claim 해제 재사용:** t1 후 primary→backup 전환은 *기존 `release_claims` 정신*을 빌린다 — claimed(primary 진행 중)→queued로 되돌린 뒤 backup 연결로 재push(`_push_pending`이 이제 primary 부재라 backup 선택). 끊김 re-queue(결정 6-4)와 같은 메커니즘이 "느린 primary 회수"에도 쓰인다.

#### 8-3. 결정론 = t1/t2 분할도 주입 clock으로 결정론 테스트

기존 단일 timeout이 주입 clock으로 결정론이듯(`InMemoryWorkQueueDispatcher(clock=...)`), t1/t2 2단도 같은 주입 clock으로 결정론 테스트한다 — clock을 t1 너머로 진전시켜 backup 전환을, t1+t2 너머로 진전시켜 escalation을 결정론적으로 재현(Fake primary/backup 워커). 실 전송·실 타이머는 후속 수동 시연.

#### 8-4. 구현 정밀화(슬라이스 ii) = head-of-line 해소(`claimable`/`claim_ticket`) + primary 회수 표식 "1회 한정 제외"(primary 회복) + `t1 < timeout` 강제

슬라이스 ii 구현에서 결정 8의 t1 전환을 다중 ticket 환경에서 닫으며 세 가지를 정밀화한다(번복 아님 — 8-1·8-2 shape의 구현 보강):

- **head-of-line 해소.** `_push_pending`이 `claim`(FIFO 첫 queued 하나)만 쓰면, 큐 앞의 *거부* 작업(backup 부재·stale 위임·위임 대상외)이 뒤의 push 가능한 작업까지 막는다(거부 작업을 unclaim+return하면 그 자리에서 멈추고, 같은 작업을 재claim하면 무한 루프). 그래서 큐 도메인에 `claimable(owner)->list[WorkTicket]`(queued 후보 조회, 전이 없음)과 `claim_ticket(ticket_id)->bool`(FIFO 첫 작업이 아닌 *특정* 작업만 queued→claimed)을 추가하고, `_push_pending`이 후보를 보며 push 가능한 건 집어 claim+push, 거부는 claim하지 않고 queued로 남긴다(자기 timeout으로 escalation — 미아 없음·단조성 보존). 거부를 claim하지 않으므로 unclaim(슬라이스 i 임시 도입)은 불요가 되어 제거.
- **primary 회수 표식 = "이번 라우팅 1회 한정, 그 primary 제외"(primary 회복, 8-2 정밀화).** t1 회수분에 붙는 `_primary_exhausted` 표식이 *push 성공 시에만* 정리되면, backup 부재/거부로 push 못 한 작업은 표식이 escalation까지 잔존해 primary가 재연결돼도 영영 primary로 못 간다(회복 불가 — 8-2 "primary 회복" 사문화). 표식 의미를 "*현재 연결된 그 느린 primary*로는 안 보낸다"로 좁힌다: ① push 확정 시 소비(라우팅 확정), ② primary 끊김(`disconnect`) 시 만료(그 느린 primary가 사라졌으므로 → 재연결 시 다시 primary로 복귀), ③ 거부(보낼 곳 없음) 시엔 유지(소비하면 같은 느린 primary로 즉시 되돌아가 회수→primary→t1→회수 무한). 표식 만료의 단일 지점이 primary 연결 소멸이다(owner 격리 — 다른 owner 표식 불간섭).
- **`t1 < timeout` 강제.** t1≥timeout이면 t1 경과 전에 이미 전체 timeout으로 escalation돼 backup 단계가 *조용히 무력화*된다 — 가용성 목적을 죽이므로 생성자에서 ValueError로 거부(조용한 무력화보다 명시적 실패). t1=None(하위호환)은 분배 자체가 없어 무관.

### 9. 동기화·staleness = 동기화 트리거는 정책(owner 수동 + 주기 + 변경 이벤트, 실 파이프라인 후속), staleness 정책은 *임계 초과 시 백업 거부→escalation*(너무 오래되면 사람), mode는 `backup` 하나 유지(staleness는 메타로)

결정 3은 `DelegationSnapshot.snapshot_at`을 자리만 잡았다. 동기화 트리거·staleness 정책·신뢰 차등을 보강한다.

#### 9-1. 동기화 트리거 = owner 수동 + 주기 + 지식 변경 이벤트, 셋 다 정책으로 두되 실 파이프라인은 후속

위임 스냅샷을 언제 뜨나 — 세 트리거를 *정책으로* 둔다(실 파이프라인은 후속, 여기선 모델·정책만):

- **owner 수동** — owner가 "지금 백업 갱신" 명시 트리거(가장 단순·확실, opt-in 정신과 직결 — 결정 3).
- **주기** — 정해진 간격(예: 일 1회) 자동 스냅샷(owner 손 안 가는 신선도 유지).
- **지식 변경 이벤트** — owner 지식(문서·인덱스) 변경 감지 시 갱신(가장 신선하나 실 변경 감지 파이프라인 필요 — 후속).
- **모델 자리:** `DelegationSnapshot.snapshot_at`이 *마지막 동기화 시각*이고, 어느 트리거로 떴든 그 시각이 staleness 판정 기준이다(트리거 종류는 메타로 둘 수 있으나 MVP shape엔 불요 — snapshot_at 하나면 판정 충분).

#### 9-2. staleness 정책 = 임계 초과 시 백업 *거부* → escalation (너무 오래된 데이터로 답하느니 사람)

snapshot_at이 정책 임계를 넘으면 — (a) 신뢰 더 하향 vs (b) 백업 거부→escalation. **(b) 거부→escalation을 택한다.**

- **근거:** 너무 오래된 스냅샷으로 답하면 *틀린 답을 owner 이름으로* 낼 위험이 크다 — "모르면 아는 척하지 않고 안전하게 넘긴다"(PRD §3 핵심원칙)에 정면으로 닿는다. 신뢰만 더 낮춰 답하느니(여전히 답은 나감) 사람(Manager)에게 올리는 게 그 원칙과 정합이다. 즉 *오래된 백업은 백업 단계를 건너뛴다* — 위임 안 한 owner가 백업을 건너뛰는 것(결정 3)과 같은 모양으로, 사슬은 Manager escalation에서 종착(미아 없음 그대로).
- **shape:** 디스패처가 backup으로 push하기 전 `DelegationSnapshot.snapshot_at`을 정책 임계(주입)와 비교 — 초과면 backup push를 *하지 않고* 큐 대기 유지(→ timeout escalation, 결정 1의 사슬 그대로). 결정 8의 t1/t2와 합류: stale면 t1 후에도 backup 전환 안 함 → 곧장 t2 없이 escalation 경로. (임계 수치는 후속 튜닝 — shape은 "stale 백업은 거부하고 escalation으로"라는 *정책 방향*만.)
- **`agent_ids` 대조(구현 정밀화, 슬라이스 ii):** backup 허용 판정은 snapshot_at(최신성)만이 아니라 *위임 대상 영역*도 본다 — 그 ticket의 카드(`agent_id`)가 `DelegationSnapshot.agent_ids`에 들어야 backup이 그 영역을 답한다. owner가 *그 담당 영역을 백업에 위임했을 때만* 백업이 그 영역에 답하는 것이라(`agent_ids`의 정의 그대로), 위임 안 한 영역까지 backup이 owner 이름으로 답하면 "모르면 넘김"을 어긴다. 대상 외면 backup 거부 → escalation(stale 거부와 같은 사슬). 즉 backup 허용 = ① 위임 존재(opt-in) ∧ ② `agent_id ∈ agent_ids`(대상 영역) ∧ ③ snapshot_at fresh, 셋의 곱. (셋 다 통과해야 `mode=backup` 답이 나가므로 결정 9-3의 "답이 나가는 백업은 다 fresh"와 정합 — 거기에 "다 위임 대상"이 더해진다.)

#### 9-3. 신뢰 차등 = mode는 `backup` 하나 유지(결정 4 정신), staleness는 *메타*로만 (fresh/stale을 mode로 안 쪼갬)

fresh backup vs stale backup을 `mode`로 쪼개나(`backup` vs `backup_stale`)? **안 쪼갠다 — mode는 `backup` 하나 유지.**

- **근거(결정 4의 "mode 최소화"와 9-2의 합류):** 9-2가 *stale은 아예 거부*(escalation)라, 백업이 *실제로 답하는* 경우는 모두 "임계 내 fresh backup"이다 — 즉 답이 나가는 backup엔 staleness 등급 분기가 없다(다 임계 내). 그러니 mode를 fresh/stale로 쪼갤 *답 자체가 존재하지 않는다*. staleness는 답이 *나갈지 말지*(거부 임계)를 가르는 메타이지, *나간 답의 mode*를 가르는 축이 아니다.
- **결정 4 정밀화(번복 아님):** 결정 4는 "staleness 등급에 따른 세분 하향(fresh vs stale)은 후속"이라 *열어뒀는데*, 결정 9가 그 후속을 닫는다 — "세분 하향은 *하지 않는다*. stale은 하향이 아니라 거부다". mode는 `backup` 하나로 확정(결정 4의 셋 `full|draft_only|backup` 그대로, 넷째 값 없음). `snapshot_at`은 BackupReviewItem(결정 7)과 audit에 *맥락 메타*로 실려 owner가 "얼마나 오래된 스냅샷으로 답했나"를 검토 시 참고할 뿐이다.

### 10. cold start = MVP는 warm(결정 2 유지), cold의 도메인 표현은 디스패처 *기동 요청 hook* + 새 대기 상태 없이 기존 큐 대기 재사용, 실패는 escalation

결정 2는 warm을 택하고 cold를 후속으로 미뤘으나 cold의 *도메인 표현*을 안 뒀다 — 보강한다(여전히 MVP는 warm, cold는 표현·연결점만).

#### 10-1. cold 라이프사이클 = primary 부재 감지 → 백업 기동 요청 → 백업 연결 대기 → push, 기동 실패/지연은 escalation

cold(백업이 상시 연결 아니라 오프라인, 필요 시 기동)의 단계:

1. **primary 부재 감지** — 결정 8의 트리거(미연결 또는 t1 무응답) 그대로.
2. **백업 기동 요청** — 누가? **중앙 디스패처가 *기동 요청만* 발행**하고(누구에게 기동을 맡기나는 인프라 — owner 인프라 또는 중앙 호스팅 오케스트레이터, 실 구현 후속), 기동 주체는 백업 인스턴스를 띄운다.
3. **백업 연결 대기** — 기동된 백업이 warm처럼 아웃바운드 WS로 *붙으면* 그때부터 결정 2의 warm 경로와 동일(우선순위 push). cold의 추가분은 "붙기까지의 지연" 한 구간뿐.
4. **push** — 백업 연결 후 큐의 그 작업을 push(결정 1·8 그대로).
5. **기동 실패/지연 → escalation** — 백업이 임계 내에 안 붙으면 결정 1의 escalation 종착(미아 없음). 즉 cold는 "백업 연결이 늦게/안 뜬다"를 *기존 timeout escalation으로 흡수*한다 — 새 종착 없음.

#### 10-2. 도메인 표현 = 새 상태 추가 없이 기존 큐 대기(AwaitingWorker) 재사용 + 디스패처 *기동 요청 hook* 연결점

결정 2는 "도메인 모델(우선순위 push)은 warm/cold 무관"이라 했다 — 이를 *정밀화*한다(번복 아님):

- **새 대기 상태를 만들지 않는다.** cold의 "기동 대기"는 큐의 *기존 `queued`/`AwaitingWorker`*가 그대로 표현한다 — 백업이 아직 안 붙었으면 작업은 큐에 대기(AwaitingWorker), 붙으면 push, 안 붙으면 timeout→escalation. cold냐 warm이냐로 *큐 상태기계가 갈리지 않는다*(결정 2 보존). 차이는 오직 "backup 연결이 *언제* 뜨나"이고, 그건 상태가 아니라 타이밍이다.
- **유일한 신규 연결점 = 디스패처의 *기동 요청 hook*.** warm은 백업이 미리 붙어 있으니 hook 불요. cold는 primary 부재 감지 시 디스패처가 "이 owner의 백업을 기동하라"는 *요청을 발행할 자리*가 필요하다 — 이를 **주입 콜백 hook**으로 둔다(`manager_of` 콜백을 주입으로 받아 결합을 피한 것과 같은 정신, dispatch.py). shape: `wake_backup: Callable[[str], None] | None`(owner_id → 기동 요청, 미주입이면 warm 전제로 no-op). 실 기동 오케스트레이션(인스턴스 부팅·키 주입)은 *후속 인프라*, 디스패처는 hook 호출만.
- **`mode=backup`은 warm/cold 동일.** cold로 기동된 백업도 결국 같은 위임 스냅샷으로 답하므로 신뢰는 동일(`mode=backup`) — cold라는 *기동 방식*은 신뢰에 영향 없다(결정 4·9 그대로).

#### 10-3. MVP 권장 = warm을 MVP로, cold는 hook 연결점만 두고 후속

- **MVP = warm(결정 2 그대로).** 상시 연결 비용은 들지만 인스턴스 라이프사이클 관리가 없어 *도메인이 가장 단순*하다(우선순위 push만). 결정 2의 택일 유지.
- **cold = hook 연결점만, 실 기동은 후속.** 위 `wake_backup` hook 자리만 shape으로 두고, 실 기동 오케스트레이션은 후속 인프라 슬라이스. 하이브리드(평소 cold·바쁜 owner만 warm)는 *그 다음* — hook이 있으면 warm/cold/하이브리드가 같은 우선순위 push 위에서 갈린다.
- **근거:** 결정 2가 든 이유(cold start는 인스턴스 라이프사이클이라는 새 인프라 축 — 도메인 shape 범위 밖) 그대로. 보강은 그 인프라를 *당기지 않고* 도메인 연결점(hook·큐 대기 재사용)만 명시해, 후속 cold 구현이 도메인을 *안 바꾸고* 붙을 수 있게 길을 터둔다.

## Considered Options

### 폴백 답변 주체 (확정 사실의 대안 검토)

- **owner 위임 백업 워커**(선택, 사용자 결정): owner 데이터·신원의 격리 인스턴스. 논리적으로 owner가 답 → ADR 0010·PRD 보강. 신뢰 하향. 단 데이터 위임·동기화·격리 인프라 비용.
- **중앙 공용 LLM 폴백**(기각): owner PC 부재 시 중앙 모델이 카드 메타로 답. 가장 단순하나 **PRD 핵심원칙 정면 위반** — 중앙이 모르는 척 답하고(중앙 무지식 깨짐), 답 책임 귀속이 모호(누가 책임지나). ADR 0010 위반.
- **백업 없이 곧장 Manager escalation**(현행, 부분 기각): 가장 단순·정합적이고 미아 없음도 지킨다. 그러나 PRD §1의 "A 휴가면 누가 잇지"를 *자동으로* 못 푼다 — owner가 잠깐 비워도 전부 사람 큐로. 백업은 이 가용성 구멍을 메우는 *종착 전 자동 회복 1단*이며, 백업 실패 시 이 현행 경로로 떨어진다(대체가 아니라 앞에 끼움).

### 백업 워커 표현

- **`WorkerLogic` 재사용 + 디스패처 등급 라우팅**(선택): 자산 재사용 최대, 워커 코드 무변경, 차이를 주입 인자로 흡수. 디스패처 `_connections`만 등급별로 확장.
- **새 `BackupWorker` 타입**(기각): 프레임 핸들링·폴백·멱등을 두 곳에서 유지. 차이(스냅샷·격리)는 로직이 아니라 주입·배치라 새 타입 불필요.
- **백업도 같은 owner_id 단일 연결로 PC 워커를 덮어씀**(기각): owner당 1 연결(`dict[str, SendFrame]`) 그대로면 PC 재연결 시 백업을 밀어내거나 그 반대 — 우선순위를 표현 못 해 폴백이 깨진다. 등급(role) 분리가 필요.

### 신뢰 레이블

- **`Answer.mode`에 `backup` 추가**(선택): 신뢰 상태를 한 축에 유지, OrgReply가 이미 노출, 노출 불변식 양립. mode가 PRD의 "신뢰 상태" 그 자리.
- **별 필드 `is_backup`/`Trust Label` 확장**(기각): 신뢰 상태가 두 축으로 흩어짐. `Trust Label`은 카드·답의 *취급 제약 태그*(internal_only 등)라 의미축이 다름(CONTEXT — Authority·Confidence와 구분).
- **`OrgReply`에 별 결과 타입(BackupAnswered)**(기각): Answered/Pending 2형 망라를 깨고, "백업이지만 답은 답"이라는 본질(여전히 owner 답)을 표현 타입에서 쪼갬. mode 값 하나가 더 정합적.

### owner 복귀 검토 루프 보관 (결정 7)

- **`BackupReviewItem` + `BackupReviewStore`(ConflictCase/Inbox 패턴 재사용)**(선택): 검증된 처리함 패턴(Protocol + InMemory, owner 색인, 불변 전이)의 두 번째 인스턴스. owner 처리함이 "다툼 합의"와 "백업 답 검토" 두 탭을 갖는 그림이 PRD 페르소나(개인 Owner — Owner 처리함)와 정합. 새 메커니즘 0.
- **`ConflictCaseStore`를 그대로 재사용(한 store에 두 타입)**(기각): 담는 값이 다르다(다툼 vs 백업 답). `open_for_owner`가 무엇을 돌려주는지 모호해지고 ConflictCase 망라가 깨진다. 패턴은 재사용하되 포트는 분리.
- **검토 안 함(백업 답을 영영 미검토로 둠)**(기각, 현 공백): 결정 5의 "owner 책임"이 명목에 그친다 — "책임은 owner인데 owner는 그 답을 모른다". PRD "누가 책임지지"가 백업 경로에서 안 닫힘. 검토 루프가 책임을 실질로 만든다.
- **검토 결과를 Precedent로 기록**(기각): Precedent는 *라우팅* 판례지 *답 검토*가 아니다(7-5). 담당은 이미 그 owner로 정해졌고 검토는 답의 옳고 그름이라 라우팅 색인에 안 들어간다. 검토는 audit에만.

### timeout 분배 (결정 8)

- **단일 timeout → t1(primary)/t2(backup) 2단 예산, 백업은 primary 명백 부재 시만(순차)**(선택): 전체 escalation까지 시간을 보존하면서 백업 1단을 끼움. 중복 답은 ticket_id 멱등이 흡수(먼저 온 답 채택), claim 해제는 기존 `release_claims` 재사용.
- **t1 후 primary·backup 병렬(먼저 오는 답 채택)**(후속): 속도엔 유리하나 자원 낭비(둘 다 claude)·정책 복잡(어느 답 audit·mode). MVP는 순차로 단순화.
- **백업도 즉시 병렬 시작(t1 없이)**(기각): primary가 멀쩡히 답할 때도 백업이 돌아 *상시* 자원 2배. 결정 1의 "primary 우선" 정신과 어긋남.

### staleness 정책 (결정 9)

- **임계 초과 stale 백업은 거부→escalation, mode는 `backup` 하나 유지**(선택): "모르면 안전하게 넘긴다"(PRD §3)와 정합 — 오래된 데이터로 답하느니 사람. stale은 답이 *안 나가므로* mode 세분이 불요(나가는 백업은 다 fresh).
- **stale도 답하되 신뢰만 더 하향(`backup_stale` mode 신설)**(기각): 여전히 오래된 데이터로 답이 나간다(원칙 위반 위험). mode가 비대(결정 4 "최소화" 위배). 결정 4가 열어둔 "세분 하향 후속"을 *하지 않는* 쪽으로 닫음.

### cold start 표현 (결정 10)

- **warm MVP 유지 + cold는 기동 요청 hook + 기존 큐 대기 재사용**(선택): 새 상태/큐 분기 없이 cold를 표현(결정 2 보존). hook은 `manager_of` 주입 정신과 동형. 후속 cold가 도메인 무변경으로 붙는다.
- **cold 전용 대기 상태 신설(`AwakeningBackup` 등)**(기각): cold의 "기동 대기"는 기존 `AwaitingWorker`/`queued`가 이미 표현(타이밍 차이지 상태 차이 아님). 새 상태는 큐 상태기계를 cold/warm으로 가른다(결정 2 "warm/cold 무관" 위배).
- **cold를 MVP로**(기각): 인스턴스 라이프사이클(부팅·키 주입·기동 실패)이라는 새 인프라 축을 당김 — 도메인 shape 범위 밖(결정 2 근거 그대로).

## Consequences

- **가용성이 1급 시민이 된다.** owner 부재가 곧 Manager 큐 폭주가 아니라, 위임한 owner면 백업이 자동으로 메운다. opt-in 안 한 owner는 기존대로 곧장 escalation(미아 없음 불변 — 사슬은 반드시 Manager에서 종착).
- **ADR 0010 보강(깨짐 아님).** "답은 owner가"가 백업에서도 성립 — 백업은 owner 위임 환경이지 중앙 공용 LLM이 아니다. 0010은 owner PC를 1차로 유지하고 0012가 가용성 확장을 보강·참조(0011이 전송을 보강한 패턴).
- **중앙 무지식 보존(PRD 핵심).** 중앙·디스패처는 `DelegationSnapshot` 메타만, 실 지식 본체는 owner별 격리 저장소·백업 인스턴스만(owner 키). 중앙이 owner 문서를 모아 보지 않는다.
- **신뢰 하향이 사용자에게 노출.** 백업 답은 `mode=backup`으로 "본인 실시간 답이 아님"이 사용자向에 표시(PRD line 20). 노출 불변식 양립 — mode는 원래 노출하는 신뢰 상태값이지 조직 내부 구조가 아니다.
- **책임은 여전히 owner.** opt-in 위임이므로 백업 답도 owner 책임(`answered_by`=owner 불변). `mode=backup`이 신뢰 차원만 덧붙임.
- **디스패처 확장이 본체, 워커·큐 도메인 무변경.** `WebSocketDispatcher._connections`를 등급별로(`dict[str, dict[WorkerRole, SendFrame]]`) 확장하고 `_push_pending`의 연결 선택만 우선순위로. `WorkerLogic`·큐 상태기계·`RuntimeDispatcher` 포트는 그대로 → 기존 206 passed 보존이 목표(폴백은 push 라우팅 추가지 큐 도메인 변경 아님).
- **timeout 예산 분배 → 결정 8에서 보강(정밀화).** MVP shape의 "primary 미연결 즉시 백업 전환"은 유지하되, primary 연결됐는데 느린 경우의 t1/t2 2단 예산을 **결정 8에서 닫았다**(번복 아님 — 미뤘던 후속을 당겨 설계). 실 수치 튜닝은 여전히 후속.
- **warm 백업 = 상시 연결 비용 / cold 표현 → 결정 10에서 보강.** 백업이 PC 워커처럼 미리 WS를 걸어두므로 *상시 인스턴스 비용*이 든다(MVP=warm 유지). cold의 *도메인 표현*(기동 요청 hook·기존 큐 대기 재사용)은 **결정 10에서 닫았다** — 도메인 모델(우선순위 push·큐 상태기계)은 warm/cold 무관(결정 2 보존), 실 기동 오케스트레이션만 후속 인프라.
- **실 구현은 후속 슬라이스(사용자 합의 후).** 데이터 동기화 파이프라인·격리 인스턴스 배치·암호화·키 관리는 이 ADR 범위 밖 — shape·연결점만.

### 보강 결정 7~10의 Consequences (4축)

- **책임이 명목에서 실질로(결정 7).** 백업 답이 owner 처리함(pending_review)에 쌓여 owner 복귀 시 *반드시 마주하고* 승인/정정/무시로 처분한다 — 결정 5의 "owner 책임"이 행위 가능해진다. PRD §1 "이 답을 누가 책임지지"가 백업 경로에서도 닫힌다. Owner 처리함(Inbox)이 "다툼 합의"와 "백업 답 검토" 두 면을 갖는다(PRD §4 — 개인 Owner). 보관은 ConflictCase/Inbox 패턴 재사용(`BackupReviewItem`+`BackupReviewStore`, 새 메커니즘 0).
- **검토는 한 질문의 세 번째 절차(결정 7-3).** 라우팅(`decision`)→디스패치(`dispatch_outcome`)→검토(사후 audit 줄)가 ticket_id로 이어진다. 전이 ≠ 기록 유지 — 검토 결과(`BackupReview`)는 도메인 전이(pending_review→reviewed), 그 기록은 audit(append-only 사후 줄). Precedent는 *건드리지 않는다*(검토는 답 옳고 그름이지 라우팅 판례 아님, 7-5).
- **정정·승격은 조회로 재노출(결정 7-3·7-4).** owner가 Correct/Approve하면 `retrieve(tracking)`이 갱신된 답(Correct=새 text·`mode=full`, Approve=기존 text·`mode=full`)을 돌려준다 — 푸시 없이 조회로(ADR 0011 결정 6-5 정합). 노출 불변식 유지(검토자·item_id 안 샘). backup→full은 "owner 검토 후 신뢰 복원". 자동 푸시 통지는 후속(Manager 큐 T5.2 통지와 같은 자리).
- **timeout이 단계 예산으로(결정 8).** 단일 timeout이 t1(primary)+t2(backup) 2단으로 — primary 미연결 즉시 백업(결정 1 유지), primary 무응답은 t1 후 전환(claim 해제=`release_claims` 재사용). 중복 답은 ticket_id 멱등이 흡수(먼저 온 답 채택, MVP 순차). 전체 escalation까지 시간 보존. 주입 clock 결정론.
- **stale 백업은 거부→escalation(결정 9).** snapshot_at 임계 초과면 백업이 *답하지 않고* escalation — "모르면 안전하게 넘긴다"(PRD §3)와 정합. 그래서 mode는 `backup` 하나 유지(답이 나가는 백업은 다 fresh — staleness는 *답 여부*를 가르는 메타지 mode 축 아님). 결정 4가 열어둔 "세분 하향 후속"을 *하지 않는* 쪽으로 닫음(미결 해소). 동기화 트리거는 owner 수동+주기+변경 이벤트 정책(실 파이프라인 후속). owner가 Correct를 반복하면 *동기화 backlog 신호*(라우팅 판례 아님).
- **cold는 도메인 무변경으로 후속에 붙는다(결정 10).** MVP=warm(결정 2 유지). cold의 "기동 대기"는 기존 큐 대기(`AwaitingWorker`/`queued`)가 표현(새 상태 0), 유일한 신규 연결점은 디스패처 *기동 요청 hook*(`wake_backup` 주입 콜백). 기동 실패/지연은 기존 timeout escalation 흡수(새 종착 0). `mode=backup`은 warm/cold 동일. 실 기동 오케스트레이션은 후속 인프라.
- **불변식 보존(4축 전부).** 미아 없음(stale 거부·cold 기동 실패·검토 미완 모두 Manager escalation 또는 처리함 보관으로 종착, 영구 소실 0) · Authority 중앙(검토는 답 처분이지 권한 선언 아님, 위임은 여전히 opt-in) · 전이 ≠ 기록(검토 전이 vs audit 기록) · 노출 불변식(검토 재노출도 Answered만) · 답엔 담당(정정도 `answered_by`=owner) · 신뢰 상태(backup·full·정정 승격이 mode로).

### 구현 분담(4축 포함, 후속·합의 후)

- **domain(타입)** — 결정 1~6의 `WorkerRole`·`DelegationSnapshot`·`mode=backup`에 더해: 결정 7의 `BackupReviewItem`·`BackupReview`(Approve|Correct|Dismiss) sealed sum·`BackupReviewStore` 포트 shape. (이 단계서 shape 제안 완료.)
- **mcp-runtime** — 디스패처 등급 라우팅(결정 2)에 더해: 결정 8의 t1/t2 2단 예산·claim 해제 후 backup 전환, 결정 9의 staleness 임계 거부·동기화 파이프라인, 결정 10의 `wake_backup` 기동 hook·cold 오케스트레이션(실 인프라), 결정 7의 검토 재노출 경로(`retrieve` 갱신).
- **tdd** — 결정론 폴백 라우팅(결정 1~2)에 더해: 결정 8의 t1/t2 분할 폴백(주입 clock), 결정 9의 stale 거부→escalation(주입 clock), 결정 7의 검토 전이(pending_review→reviewed·Approve/Correct/Dismiss 망라·재노출 mode 승격)를 결정론으로.

- PRD §3·§4·§6, TRD §2·§4·§5·§9, CONTEXT(Backup Worker·DelegationSnapshot·WorkerRole·Answer.mode·**BackupReviewItem·BackupReview·BackupReviewStore·Inbox 확장**), tasks(T6.6 4축 분담)를 이 방향으로 갱신한다(설계 단계 — 구현 체크는 후속).
