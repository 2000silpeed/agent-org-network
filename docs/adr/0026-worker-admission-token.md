# 워커 인증/admission — `TokenStore` 포트로 등록 토큰을 발급·검증·만료·revoke하고 `_authenticate` stub을 실 교체한다

상태: accepted (2026-06-26) · **Phase 9의 ADR-C** · **ADR 0011 결정 6-5·ADR 0012 결정 5가 "실 토큰 검증은 T6.5/후속"으로 *예고한 자리 채움*(충돌 아님)** · `transport.py:522`의 `_authenticate` stub("token 있으면 통과")을 실 검증으로 교체 · `BackupReviewStore`·`SessionStore`(포트+InMemory store 패턴)의 *N번째 인스턴스* — 새 메커니즘 0 · ADR 0009(진짜 그 owner인가)·ADR 0016(운영 면 세션 인증, *다른 축*)와 정합 · CONTEXT(신규 Worker Admission Token·TokenStore 용어)·PRD §6·TRD §4·§5 갱신

## 맥락 — 예고된 자리를 채운다

owner 워커는 중앙에 *아웃바운드 WebSocket*으로 연결해 `RegisterWorker{owner_id, token?}`로 자기 신원을 선언한다(ADR 0011 결정 6-3). 그런데 지금 `WebSocketDispatcher._authenticate`는 stub이다:

```python
def _authenticate(self, frame: RegisterWorker) -> bool:
    # 실 토큰 검증은 T6.5 몫 — 지금은 *거부 지점만* 둔다.
    return bool(frame.owner_id)   # 빈 owner_id만 거부
```

`token` 필드는 있으나(`transport.py:113`) 검증되지 않는다 — 누구든 `owner_id`만 채우면 그 owner인 척 연결해 `SubmitAnswer`로 그 owner 이름의 답을 회신할 수 있다. ADR 0011 결정 6-5·ADR 0012 결정 5가 "실 토큰 검증은 T6.5/후속"·"거부 hook만"으로 *명시적으로 예고*한 자리다. 이건 **충돌이 아니라 예고된 자리 채움**(tasks line 219·275).

Phase 9에서 owner 워커는 *기본 대화 경로*로 다시 1급이 된다(ADR-D — owner OAuth 멀티-LLM 워커 ↔ 중앙). 그래서 "진짜 그 owner의 워커인가"를 검증하는 토큰 admission이 *지금 필요*하다 — 운영자가 콘솔에서 워커 등록 토큰을 발급·승인·취소(revoke)하고, 워커가 `--token`으로 연결하면 중앙이 검증한다(확정 결정 7).

설계 제약:

1. **결정론 게이트.** 토큰 발급·검증·만료·revoke의 *결정 로직*은 게이트 내(주입 clock·`InMemoryTokenStore`). 실 `--token` 연결 시연은 게이트 밖(실 워커·실 WS·실 네트워크·T9.5 (d)).
2. **결정 로직 vs 실 형식 분리.** *어떻게 발급/검증/만료하나*(로직)는 게이트 내. *실 토큰 형식·서명*은 외부 결정(아래 결정 대기).

## 결정

### 1. `TokenStore` 포트 — 기존 store 패턴의 N번째 인스턴스

등록 토큰의 발급·검증·만료·revoke는 `BackupReviewStore`·`SessionStore`·`ConflictCaseStore`와 **같은 포트 패턴**(Protocol + `InMemoryTokenStore` + 후속 `SqliteTokenStore`)으로 격리한다.

```python
class TokenStore(Protocol):
    def issue(self, owner_id: str, role: WorkerRole, *, now: datetime) -> AdmissionToken: ...
    def verify(self, raw_token: str, *, now: datetime) -> AdmissionToken | None: ...  # 유효(미만료·미revoke)면 토큰, 아니면 None
    def revoke(self, token_id: str) -> AdmissionToken | None: ...
    def list_active(self) -> list[AdmissionToken]: ...   # 콘솔 연결/대기 워커 토큰 목록

@dataclass(frozen=True)
class AdmissionToken:
    token_id: str            # 안정 식별자(콘솔 revoke 대상·로그)
    owner_id: str            # 이 토큰이 귀속된 owner(WorkTicket.owner_id와 합류)
    role: WorkerRole         # primary | backup(ADR 0012 — 그 owner 안 우선순위)
    token_hash: str          # 저장은 해시(평문 토큰은 발급 시 1회만 반환)
    issued_at: datetime
    expires_at: datetime | None
    revoked: bool = False     # append-only 정신(삭제 X) — Precedent.invalidated 패턴
    revoked_at: datetime | None = None
```

- **`issue`**: 운영자가 (owner/role)용 등록 토큰을 발급 — 평문 *raw_token*을 *그 자리에서 1회만* 반환하고, store엔 *해시*만 보관(아래 결정 대기 4). owner 귀속을 토큰에 박는다(워커 자기보고가 아니라 중앙 발급 — Authority 중앙).
- **`verify`**: 워커 `RegisterWorker.token`을 검증 — 미만료(주입 clock)·미revoke·해시 일치면 `AdmissionToken`, 아니면 None.
- **`revoke`**: 콘솔에서 취소 — append-only(삭제 X·`revoked=True` 표식, `Precedent.invalidate`·`BackupReviewItem` 정신). revoke된 토큰은 verify에서 None.
- **`list_active`**: 콘솔이 "연결/대기 워커" 목록을 그리는 원천.
- 색인은 `token_hash`(verify)·`token_id`(revoke)·전체(list)를 둔다.

### 2. `_authenticate` 실 교체 — stub → `TokenStore.verify`

`WebSocketDispatcher._authenticate`(`transport.py:522`)를 `TokenStore` 검증으로 교체한다(ADR 0011 결정 6-5 hook 실체화):

```python
def _authenticate(self, frame: RegisterWorker, *, now: datetime) -> bool:
    if not frame.owner_id:
        return False                               # 빈 신원 거부(기존)
    if frame.token is None:
        return False                               # 토큰 없으면 거부(stub의 "있으면 통과" 반전)
    tok = self._tokens.verify(frame.token, now=now)
    if tok is None:
        return False                               # 만료/revoke/위조 거부
    return tok.owner_id == frame.owner_id and tok.role == frame.role  # 귀속·등급 일치
```

- **등록 무결성**: 유효하지 않은 토큰(만료·revoke·위조·없음)은 admission 거부 → 레지스트리에 안 올라가 이후 `PushWork`·`SubmitAnswer`가 안 흐른다(`register`가 `AuthError` 반환·`transport.py:469`).
- **owner 격리**: 토큰의 `owner_id`가 `RegisterWorker.owner_id`와 일치해야 한다 — "그 ticket의 owner ≠ 연결 owner면 거부"(ADR 0011 결정 6-5)가 *발급 시점 귀속*으로 강제된다. 회신이 *진짜 그 owner에게서* 온다(owner 가장 차단).
- **등급 검증**: 토큰의 `role`(primary/backup)이 선언 role과 일치(ADR 0012 결정 2 등급 검증 합류 — backup 토큰으로 primary 등록 차단).
- `TokenStore`는 `WebSocketDispatcher` 생성자에 *주입*(미주입이면 기존 stub 동작 보존? — 아니다, 인증은 안전 경계라 *주입 필수가 프로덕션*이고 데모 OFF는 명시 플래그로. ADR 0016의 "env 설정=인증 ON·미설정=데모 OFF" fail-open 정신과 동형 — 단 워커 인증은 owner 가장이 곧 신뢰 위반이라 프로덕션 기본 ON 권장).

### 3. 콘솔 워커 명령 = 발급·목록·승인/취소 (T9.2 (b)·T9.5 (c))

콘솔 POST 명령이 도메인 서비스를 부르는 얇은 어댑터다:

- **발급**: `TokenStore.issue(owner_id, role)` → 평문 raw_token 1회 반환(운영자가 워커 설정에 전달).
- **목록**: `TokenStore.list_active()` + `WebSocketDispatcher`의 연결 레지스트리(`_connections`)를 합쳐 "발급됨/연결됨/대기" 상태 투영.
- **승인/취소**: revoke는 `TokenStore.revoke(token_id)`. "승인"은 *발급 = 승인*(발급된 토큰이 곧 admission 자격)이거나, 발급-then-pending-then-approve 2단 — **MVP는 발급=즉시 유효**(가장 단순한 닫힌 루프·SessionStore 암묵 시작 정신). pending-approve 2단은 *요구가 관측될 때* 당김(과도 엔지니어링 회피).
- 운영자 인증은 ADR 0016 운영 세션 재사용(콘솔은 운영 면 — 인증된 운영자만 발급/revoke).

## 근거

- **store 패턴 재사용** — 토큰은 "발급·검증·만료·revoke + 색인 조회"라는 store 모양이다. `BackupReviewStore`·`SessionStore`와 한 패턴(새 메커니즘 0).
- **예고된 자리 채움** — ADR 0011 결정 6-5·0012 결정 5가 명시적으로 hook만 두고 "실 검증은 후속"으로 미뤘다. 이 ADR은 그 hook을 실체화할 뿐 새 결정을 뒤집지 않는다(충돌 아님).
- **Authority 중앙 정합** — 토큰은 *중앙 발급*(콘솔 운영자)이지 워커 자기보고가 아니다. owner 귀속이 발급 시점에 박혀 카드 자기보고 금지(ADR 0004)와 같은 결 — 워커가 자기 owner를 자처 못 한다.
- **append-only revoke** — 삭제가 아니라 `revoked=True` 표식(`Precedent.invalidate`·`BackupReviewItem` 정신). 취소 이력이 남고 멱등.

## Consequences

- **`token.py`(또는 `transport.py` 확장) 신규** — `AdmissionToken`(frozen)·`TokenStore`(Protocol)·`InMemoryTokenStore`·후속 `SqliteTokenStore`. `review.py` 구조 본보기.
- **`WebSocketDispatcher._authenticate` 실 교체** — `TokenStore` 주입·verify·owner/role 일치. `RegisterWorker.token` 실 검증. 미인증/revoke 토큰의 `SubmitAnswer` 거부(register가 AuthError 반환해 레지스트리에 안 올림).
- **콘솔 워커 명령(T9.2 (b))** — 발급·목록·revoke POST 라우트(운영 세션 인증).
- **실 `--token` 연결(T9.5 (d)·게이트 밖)** — 실 워커 프로세스가 발급 토큰으로 실 아웃바운드 WS 연결·revoke가 실 연결에 반영. 수동 시연(`worker.py`의 `run_worker`에 `--token` 인자·`RegisterWorker.token` 채움).
- **불변식 영향 없음**:
  - **등록 무결성** — 유효하지 않은 토큰은 admission 거부(워커 admission이 카드 admission과 같은 결 — "유효하지 않으면 안 들어온다").
  - **owner 격리** — 토큰 owner 귀속이 회신 출처를 그 owner로 강제(가장 차단).
  - **Authority 중앙** — 토큰은 중앙(콘솔 운영자) 발급·귀속 선언이지 워커 자기보고 아님.
  - **미아 없음** — 인증 거부는 *그 워커*가 안 붙을 뿐, 작업은 큐에 남아 timeout→escalation 종착(`disconnect`/큐 도메인 무변경). 인증이 미아를 만들지 않는다.
  - **전이 ≠ 기록** — 토큰 발급/revoke는 도메인 admission 상태지 절차 로그 아님(audit과 별 축).
- **운영 면 세션 인증(ADR 0016)과 다른 축** — ADR 0016은 *운영 웹 면* 신원(operator/owner 로그인)이고, 이 토큰은 *전송 채널* 워커 admission이다. 같은 "진짜 그 owner인가"를 다른 채널에서 강제(ADR 0016 결정 Consequences "워커 인증은 전송 인증이라 별 축이지만 같은 질문"). 워커 신원과 owner SSO 신원 연계는 결정 대기.
- **갱신 대상**: CONTEXT(신규 Worker Admission Token·AdmissionToken·TokenStore 용어, WebSocketDispatcher `_authenticate` 실 교체 주석)·PRD §6(워커 인증 항목)·TRD §4(TokenStore 포트)·§5(분산 전송 워커 인증 실 교체).

## 결정 대기 (사용자 확정)

### 토큰 형식/만료/refresh 정책 — **권장: 불투명 랜덤 토큰 + 만료(주입 clock) + 저장 시 해시 + revoke 목록** *(확정 대기)*

- **권장**: 평문은 *불투명 랜덤*(`secrets.token_urlsafe` 정도) — 구조 없는 ID 1개(노출 불투명·추측 불가). 저장은 *해시*(평문은 발급 시 1회만 반환·DB 유출 시 평문 미노출 — ADR 0016 결정 1-a "보안 코드 자작 회피"의 정신으로 표준 해시). 만료는 `expires_at`(주입 clock으로 결정론 verify). revoke는 append-only 표식 목록.
- **근거**: 불투명 랜덤 + 해시 저장은 v0 워커 admission에 충분(서명 토큰·JWT는 owner OAuth 자격증명이 아니라 *워커 연결 자격*이라 과함 — ADR 0021이 JWT 자작을 보안 민감으로 기각한 정신). 결정 *로직*(발급·verify·만료·revoke)은 게이트 내, 실 형식(토큰 길이·해시 알고리즘·서명 도입 여부)은 외부 결정.
- **대안**: 서명 토큰(JWT) — refresh·만료를 토큰 자체에 실으나 자작 서명은 보안 민감(ADR 0021 기각 정신). refresh token — MVP는 만료 시 재발급(콘솔)으로 충분, refresh 흐름은 후속.
- **워커 신원과 owner SSO 신원 연계 여부** *(확정 대기)* — 워커 토큰 owner_id를 SSO 검증 신원(ADR 0021)과 잇을지. 권장 보류(MVP는 콘솔 발급 owner_id가 진실 — SSO 연계는 운영 면 축이라 후속).

### SQLite 스키마(T9.8 영속·tokens 테이블) — **권장 초안만** *(확정 대기·T9.8 마지막)*
- **권장 초안**(tokens 테이블 — 확정 대기): `tokens(token_id TEXT PK, owner_id TEXT, role TEXT, token_hash TEXT, issued_at TEXT, expires_at TEXT, revoked INTEGER, revoked_at TEXT)` — token_hash로 verify 색인(인덱스), owner_id로 list 색인. 평문 토큰은 *저장 안 함*(해시만). 시각은 ISO8601 TEXT. `sessions` 테이블은 ADR 0024 참조.
- **근거**: InMemory 그린 뒤 tmp-file 통합 테스트(`SubprocessGitGateway` 정신·DB 없으면 skip). Postgres는 후속. 실 스키마·인덱스는 T9.8에서 확정.
