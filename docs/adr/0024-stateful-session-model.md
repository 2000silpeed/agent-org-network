# 상태 세션 — 중앙이 사용자 단위 세션·맥락·수명을 관리한다(무상태 1회성 → 상태 세션)

상태: accepted (2026-06-26) · **Phase 9(Hermes 콘솔 + 상태 세션 + 멀티-LLM)의 ADR-A** · ADR 0006(중앙 MCP `ask_org`)·ADR 0011(작업 큐 디스패치)·ADR 0014(미아 없음 종착) 위에 *세션 층만* 더한다(라우팅 도메인 무변경) · `BackupReviewStore`·`ConflictCaseStore`·`ReevalStore`·`PrecedentStore`(포트+InMemory store 패턴)의 *N번째 인스턴스* — 새 메커니즘 0 · CONTEXT(신규 Session·Console 용어)·PRD §3·§5·§6·TRD §2·§4·§5 갱신

## 맥락 — 무상태 1회성을 상태 세션으로

Phase 1~8은 *무상태 1회성 질의응답*을 닫았다 — 질문 1개 → `Router`가 라우팅 → 답 생성 → 끝. 한 질문과 다음 질문 사이에 맥락이 없다. `ask_org(question)`도, `POST /ask`도 매 호출이 독립이다. 사용자가 "그럼 그 다음은?"이라고 물으면 그 "그"가 무엇인지 시스템은 모른다.

Phase 9의 제품 비전(Hermes/opencode 모델 — 각 에이전트 전용 채팅 + 사용자가 같은 맥락에서 대화)은 *상태*를 요구한다. 같은 사용자의 연속 발화가 한 맥락으로 묶이고, 라우팅된 담당(Agent Runtime)이 그 맥락을 보고 답한다. 중앙이 그 세션을 *소유*한다 — 시작·맥락 보존·종료가 전부 중앙 결정이다(opencode 모델의 "세션 관리는 중앙"·확정 결정 2).

핵심은 **라우팅 도메인을 다시 짜지 않는다**는 것이다. 각 사용자 메시지는 *여전히 기존 `Router`로* 라우팅된다(Routed→담당·Contested→ConflictCase·Unowned→Manager escalation·Precedent 자동 적용·Approval=draft_only). 세션 층은 그 위에 *맥락·트랜스크립트·수명*만 보탠다 — 기존 라우팅·노출 불변식·미아 없음을 *감싸기*지 *변경*이 아니다.

설계 제약 둘이 범위를 좁힌다:

1. **결정론 게이트.** 세션 store·맥락 조립·수명 판정은 게이트 내 결정론(주입 clock·`InMemorySessionStore`). 실 SSE 브라우저 푸시·실 멀티-머신·SQLite durable 어댑터의 실 IO는 게이트 밖(수동 시연·tmp-file 통합).
2. **증분.** 메시지당 담당 1명(fan-out은 후속 Phase로 명시 연기 — tasks line 313). 한 사용자 한 세션. SQLite durable은 InMemory 그린 뒤(T9.8).

## 결정

### 1. `SessionStore` 포트 — 기존 store 패턴의 N번째 인스턴스

세션 보관·조회·수명은 `BackupReviewStore`·`ConflictCaseStore`·`ReevalStore`·`PrecedentStore`와 **같은 포트 패턴**(Protocol + `InMemorySessionStore` + 후속 `SqliteSessionStore`)으로 격리한다 — 새 메커니즘이 아니라 검증된 모양의 또 한 인스턴스다.

```python
class SessionStore(Protocol):
    def open_or_get(self, user_id: str) -> Session: ...   # 암묵 시작(첫 메시지) 또는 활성 세션 반환
    def get(self, session_id: str) -> Session | None: ...
    def append_turn(self, session_id: str, turn: SessionTurn) -> Session: ...  # 트랜스크립트 적재
    def end(self, session_id: str) -> Session | None: ...  # 운영자 종료·맥락 비움
    def active_for_user(self, user_id: str) -> Session | None: ...
```

- **`open_or_get(user_id)`**: 첫 메시지에 *암묵* 세션 시작. 같은 사용자의 활성 세션이 있으면 그것을 반환(맥락 보존). 별도 "세션 생성" API를 사용자에게 노출하지 않는다 — 세션은 메시지가 오면 *자연히* 생긴다(opencode 모델).
- **`append_turn`**: 한 턴(사용자 발화 + 그 답)을 트랜스크립트에 append. 전이(세션 상태)와 별개의 *기록* 적재다(아래 불변식 4).
- **`end`**: 운영자 명시 종료 또는 유휴 타임아웃이 부른다 — `status`를 `ended`로 전이하고 **그 세션의 맥락(트랜스크립트)을 비운다**(프로덕션 위생·결정 4).
- 색인은 `user_id`(active_for_user)와 `session_id`(get)를 둔다 — `BackupReviewStore`가 owner 색인과 item_id 색인을 둘 다 두는 정신.

### 2. `Session` 값 객체(frozen) — 사용자 단위, 트랜스크립트는 *사용자 발화 스레드만*

```python
@dataclass(frozen=True)
class Session:
    session_id: str
    user_id: str                       # 사용자 단위(채팅 신원) — 결정 6
    status: SessionStatus              # Literal["active", "ended"]
    transcript: tuple[SessionTurn, ...]  # 사용자 발화 + 그 답의 스레드(종료 시 빈 튜플)
    started_at: datetime               # 주입 clock 결정론
    last_active_at: datetime           # 유휴 타임아웃 판정 기준(주입 clock)

@dataclass(frozen=True)
class SessionTurn:
    question: str                      # 사용자 발화 원문
    answer_text: str                   # 이 사용자에게 나간 답 본문
    answered_by: str                   # 어느 담당(agent_id)이 답했나 — 맥락 라벨
    at: datetime
```

- **`status`는 `Literal`(sealed sum 아님)** — `active`/`ended` 두 상태가 *필드 구조가 같다*(둘 다 같은 필드를 든다). `Notification.kind`가 Literal인 것과 같은 판단(분기 없는 라벨은 Literal, 처분만 sealed sum). 종료된 세션은 status=ended·transcript=빈 튜플.
- **`transcript`는 *그 사용자의 발화 스레드만*** — 사용자가 무엇을 물었고 무엇을 답받았나. **다른 사용자의 세션·다른 owner의 내부 맥락·조직 내부 라우팅 구조는 안 든다**(노출 불변식·owner 격리·결정 3).
- `frozen` 값 객체라 전이는 새 인스턴스를 낳는다(`ConflictCase.resolve()`·`BackupReviewItem.review_with()` 정신) — store가 교체한다.

### 3. 맥락 조립 = 순수 함수 `assemble_context` — owner 격리·노출 불변식

라우팅된 담당(Agent Runtime)에 주는 맥락은 *그 사용자의 발화 스레드만*이다. 다른 에이전트가 답한 내용·다른 owner의 답·조직 내부 구조는 섞지 않는다.

```python
def assemble_context(session: Session, current_question: str) -> str:  # 또는 list[messages]
    """그 사용자의 발화 스레드를 멀티턴 맥락으로 조립(순수 함수·web 분리 경계)."""
```

- `serialize_reply`·`reply_to_mcp_text`·`render_mcp_notification`과 **같은 투영 경계** — 도메인 값(Session)에서만 투영해 내부값이 구조적으로 안 샌다. 순수 함수라 결정론 단위 테스트.
- **노출 불변식**: 맥락에 *다른 owner의 답이 섞이지 않는다*(적대 추적 테스트 — 사용자 A의 맥락에 사용자 B 발화·owner 내부 메모가 없음). 종료된 세션(transcript 빈 튜플)은 *빈 맥락*을 낳는다.
- **owner 격리**: 세션은 *사용자* 귀속이다(누가 물었나). 각 담당은 *그 사용자가 자기에게 한 발화*만 맥락으로 받지, 다른 담당이 받은 발화·다른 사용자 맥락을 못 본다. fan-out(한 메시지→여러 담당)이 들어오면 owner 간 맥락 노출 규율을 그때 설계한다(후속 Phase — 지금은 메시지당 1명이라 한 담당만 맥락을 받음).

### 4. 세션 수명 = 암묵 시작 + 운영자 종료 + 유휴 타임아웃

- **암묵 시작**: 첫 메시지에 `open_or_get`이 세션을 연다(사용자가 "세션 시작"을 안 누름).
- **운영자 종료**: 콘솔(ADR-A 연계 T9.2)에서 운영자가 `end(session_id)`를 명시 호출 — 중앙이 끊으면 세션이 끝난다(확정 결정 2). 종료 시 맥락을 비운다.
- **유휴 타임아웃**: `last_active_at`이 정책 임계를 넘으면 자동 종료(주입 clock으로 결정론). **확정: 30분**(마지막 활동 후 슬라이딩·설정값·아래 결정 B).
- 종료(둘 다)는 transcript를 빈 튜플로 만든다(프로덕션 위생 — 끝난 세션이 맥락을 들고 있을 이유 없음, 메모리·노출 표면 축소). 종료 후 같은 사용자의 새 메시지는 *새 세션*을 연다.

### 5. `AskOrg`/세션 층 와이어링 — 감싸기지 라우팅 변경 아님

각 사용자 메시지는 세션을 통과해 *기존* `AskOrg.handle`(Router·dispatcher)로 라우팅되고, 결과 턴이 트랜스크립트에 적재된다.

```
message(user_id, question)
  → session = store.open_or_get(user_id)          # 세션 층(신규)
  → context = assemble_context(session, question)  # 맥락 조립(신규·순수)
  → reply = ask.handle(question, user, context=...) # 기존 라우팅(무변경 코어)
  → store.append_turn(session_id, turn(question, reply))  # 트랜스크립트 적재(신규)
  → reply
```

- **기존 라우팅·노출 불변식·미아 없음 회귀 0이 1순위.** 0매칭→Unowned→escalation, Contested→합의, Precedent 자동 적용, Approval→draft_only가 *그대로* 종착한다 — 세션 층이 종착을 안 바꾼다.
- `AskOrg.handle`에 맥락 주입은 *옵셔널*로(미주입이면 기존 무상태 동작 — `notifier: Notifier | None = None`·`propagator=None`과 동형 하위호환). 맥락은 Agent Runtime 프롬프트 조립에만 흘러 들어가고 라우팅 판정(intent·candidates)은 *현재 질문*으로 한다(맥락이 라우팅을 흔들지 않음 — 증분·안전).

## 근거

- **store 패턴 재사용** — 세션은 "미해소/활성 도메인 상태 + 전이 + 색인 조회"라는, 이미 네 번 검증된 모양(ConflictCase·BackupReview·Reeval·Precedent)이다. 새 추상을 만들 이유가 없다.
- **라우팅 무변경** — Phase 9의 가치는 *세션화*지 라우팅 재설계가 아니다. 라우팅을 건드리면 미아 없음·노출 불변식 회귀 위험이 크다. 세션을 *감싸기* 층으로 두면 그 위험이 0이다.
- **맥락 = 순수 투영** — `serialize_reply`가 노출 불변식을 순수 함수 경계로 지킨 그 정신을 맥락 조립에 그대로 쓴다. owner 격리·다른 답 누설 0이 *구조적*으로(타입에서) 보장된다.

## Consequences

- **`session.py` 신규 모듈** — `Session`·`SessionTurn`·`SessionStatus`·`SessionStore`(Protocol)·`InMemorySessionStore`·`assemble_context`(순수 함수). `review.py`·`conflict.py` 구조를 본보기로.
- **`AskOrg`에 옵셔널 맥락 주입** — `handle(question, user, *, context=None)`(미주입이면 기존 동작·하위호환). 세션 층 와이어링은 web/콘솔 어댑터가 `open_or_get → assemble → handle → append_turn`을 묶는다.
- **불변식 영향 없음**:
  - **미아 없음** — 세션은 라우팅 종착을 안 바꾼다(0매칭→Unowned→escalation 그대로). 세션이 만료돼도 새 메시지는 새 세션으로 *여전히* 라우팅된다.
  - **Authority 중앙** — 세션은 권한을 만들지 않는다(누가 담당인지는 여전히 Router·routing_rules.yaml).
  - **전이 ≠ 기록** — 세션 상태 전이(active↔ended)는 *도메인*이고, 트랜스크립트 적재는 *기록*이다(별 축). audit log(질문→라우팅→디스패치 절차)는 *무변경* — 세션 트랜스크립트는 사용자 맥락 보존용이지 운영자向 절차 로그가 아니다.
  - **노출 불변식·owner 격리** — 맥락은 그 사용자 발화 스레드만(다른 owner 답 누설 0). 종료 세션은 빈 맥락.
- **영속(T9.8)** — `SqliteSessionStore`는 InMemory 그린 뒤 tmp-file 통합 테스트로(`SubprocessGitGateway` 정신·DB 없으면 skip). 스키마는 결정 대기.
- **갱신 대상**: CONTEXT(신규 Session·SessionTurn·SessionStore·assemble_context 용어, Console 절은 ADR-A·ADR-C 연계)·PRD §3·§5·§6·TRD §2·§4·§5.

## 결정 (사용자 확정 2026-06-27 — A·B 확정 / C는 T9.8 마지막)

### A. 채팅 사용자 신원(`Session.user_id`의 출처) — **✅ 확정: 익명 세션 쿠키**

- **권장: 익명 세션 쿠키.** 채팅은 ADR 0006·0009·0016·현 README상 *이미 익명*이다(운영 면만 인증, ADR 0016 결정 6 "실 사용자 채팅은 익명 유지"). Phase 9 세션도 이 경계를 유지 — 채팅 세션은 익명 쿠키(브라우저당 1 `user_id`)로 식별하고, SSO 연계는 후속.
- **근거**: 운영 면 SSO(ADR 0021)는 *operator/owner* 신원 축이고 채팅 사용자는 *다른 축*이다(ADR 0016 결정 6). 익명 쿠키는 외부 결정·새 의존성 0이고 ADR 0016 경계와 정합. `_session_identity` 헬퍼는 *운영* 세션용이므로 채팅 세션 식별은 별 헬퍼(혼입 방지).
- **대안(기각 권장)**: 기존 SSO 신원을 채팅에도 요구 — 채팅을 인증 뒤로 밀어 ADR 0016 "채팅 익명" 경계를 깨고, 사내 사용자만 묻게 제한한다. 사내 전용이 확정 요구가 되면 그때 SSO 채팅을 얹는다(세션 user_id 출처만 바꾸면 됨 — ADR 0021이 운영 면에 한 것과 동형).

### B. 유휴 타임아웃 기본값 — **✅ 확정: 30분**(마지막 활동 후 슬라이딩·설정값)

- **권장: 30분.** 대화 세션의 통상 유휴 임계(채팅 도구 관행)이고, 너무 짧으면(예: 5분) 잠깐 자리 비운 사용자의 맥락이 끊겨 재질문을 강요하고, 너무 길면(예: 24시간) 끝난 대화의 맥락·메모리가 오래 남아 위생·노출 표면이 커진다. 30분이 그 사이의 합리적 기본.
- **근거**: 주입 clock으로 결정론 테스트되는 *설정값*이라 운영 중 조정 가능. 기본만 박고 환경별 튜닝은 설정으로.
- **대안**: 무제한(운영자 종료만) — 메모리·노출 표면 누적, 비추천. 활동 기반 슬라이딩(`last_active_at` 갱신마다 리셋)은 채택 — 30분은 "마지막 활동 후 30분".

### C. SQLite 스키마(T9.8 영속·sessions 테이블) — **권장 초안만** *(확정 대기·T9.8 마지막)*

- **권장 초안**(sessions 테이블 — 확정 대기): `sessions(session_id TEXT PK, user_id TEXT, status TEXT, started_at TEXT, last_active_at TEXT)` + `session_turns(session_id TEXT FK, idx INTEGER, question TEXT, answer_text TEXT, answered_by TEXT, at TEXT, PRIMARY KEY(session_id, idx))`(트랜스크립트는 별 테이블·순서 idx). 시각은 ISO8601 TEXT(주입 clock 직렬화·`AuditEntry` timestamp 정신). `tokens` 테이블은 ADR 0026 참조.
- **근거**: InMemory 그린 뒤 tmp-file 통합 테스트로(`SubprocessGitGateway` tmp repo·DB 없으면 skip). 멀티 인스턴스 확장 시 Postgres는 *후속 명시 연기*(과도 엔지니어링 회피 — SQLite 한 바퀴부터). 실 스키마·인덱스·마이그레이션은 T9.8에서 확정.
