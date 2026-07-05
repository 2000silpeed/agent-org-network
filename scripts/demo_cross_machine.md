# 물리 2대 크로스머신 시연 — 중앙(Mac) ↔ owner 워커(Linux)

T9.6/T9.7 잔여·ADR 0028 §14의 "단일 머신 루프백 과대주장 금지"를 해소하는 실 네트워크 시연.
전 사슬: 콘솔 토큰 발급 → 리눅스 워커 실 WS 등록(실 토큰 검증) → HITL 초안 보류 →
owner 검토·수정 전송 → 중앙 답 회수 → 콘솔 SSE 관전 → revoke 거부.

전제: 두 기기가 같은 LAN. `<MAC_IP>`는 중앙(Mac)의 LAN IP.

## 1. 중앙(Mac) 준비

```bash
cd ~/ai-projects/agent-org-network

# durable(SQLite) — 이걸 켜야 워커 실 토큰 검증도 함께 켜진다(storage_select 배선)
export AON_DB=$HOME/.aon/aon.db

# LAN에 연다(보안: 신뢰 LAN에서만·방화벽으로 8000 통제 — 시스템 설정에서 수신 허용)
scripts/run_central.sh 8000 0.0.0.0
```

다른 터미널에서:

```bash
# 내 LAN IP 확인 (예: 192.168.0.10)
ipconfig getifaddr en0

# 워커 admission 토큰 발급 — 평문 token은 이 응답 1회만 노출된다. 복사해 둘 것.
curl -s -X POST http://127.0.0.1:8000/console/tokens \
  -H 'Content-Type: application/json' \
  -d '{"owner_id":"cs_lead","role":"primary"}'

# (선택) HITL on — 워커가 자동 회신 대신 초안을 보류하고 owner 검토를 기다린다
curl -s -X POST http://127.0.0.1:8000/console/hitl/cs_ops \
  -H 'Content-Type: application/json' -d '{"on":true}'
```

브라우저로 **관전 화면**: `http://<MAC_IP>:8000/console/view`
(질문 인입·라우팅 결정·답 전송·워커 연결/해제가 실시간으로 흐른다)

## 2. owner 워커(Linux) 준비

```bash
# 의존 도구
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv
npm install -g @anthropic-ai/claude-code           # claude CLI (또는 공식 설치법)
claude                                             # 최초 1회 로그인(구독 인증) 후 종료

git clone https://github.com/2000silpeed/agent-org-network.git
cd agent-org-network
uv sync
```

워커 기동 — okf/ 번들은 레포에 포함돼 있어 clone만으로 cs_ops 지식을 읽는다:

```bash
TOKEN='<1단계에서 발급받은 평문 토큰>' \
AON_OWNER_UI_PORT=8790 \
scripts/run_worker.sh cs_lead primary 8000 <MAC_IP>
```

기대 출력: `중앙에 등록됨(ws://<MAC_IP>:8000/worker). 작업 대기.` + 콘솔 관전 화면에
`worker_connected` 배지. 등록 거부(AuthError)면 토큰 오타/만료/revoke 확인.

owner 검토면(HITL on일 때): 리눅스 로컬 브라우저에서 `http://127.0.0.1:8790/`
(bind 127.0.0.1 고정 — 외부 미도달. 원격에서 보려면 `ssh -L 8790:127.0.0.1:8790 <linux>`)

## 3. 시연 시나리오

```bash
# 아무 기기에서 — 질문 (브라우저 http://<MAC_IP>:8000/ 채팅도 동일)
curl -s -X POST http://<MAC_IP>:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"환불은 어떻게 받을 수 있나요?"}'
# → {"type":"pending","kind":"dispatched", "tracking":"<ID>"}
```

1. **관전**: 콘솔 화면에 question_received → routing_decision_recorded(cs_ops)가 뜬다.
2. **HITL off**: 리눅스 워커가 로컬 `claude -p`로 OKF 접지 답을 만들어 자동 회신 —
   `curl http://<MAC_IP>:8000/ask/<tracking>` 으로 답 회수(수십 초·LLM 시간).
3. **HITL on**: 워커가 초안을 **보류** — 리눅스 `:8790` 검토면에 초안이 뜬다.
   내용을 수정하고 "수정 전송" → 중앙에서 `/ask/<tracking>` 회수 시 **수정본 그대로** 도착.
4. **revoke 거부**: Mac에서 `curl -X POST http://127.0.0.1:8000/console/tokens/<token_id>/revoke`
   후 리눅스 워커를 재시작하면 `등록 거부(AuthError)`로 멈춘다(헛된 재연결 없음).
5. **durable**: 중앙을 재시작(같은 `AON_DB`)해도 토큰이 보존되고 워커가 자동 재연결한다.

## 관측 포인트 (기록용)

- 물리 2대 실 네트워크에서: 등록 왕복 시간·PushWork→회신 지연·재연결 백오프 동작.
- 단일 머신 루프백 시연(T11.7e·T9.7 S5)에서 못 본 것: 실 NAT/방화벽 통과·기기 간 시계 차이
  무영향(주입 clock은 중앙 기준)·LAN 단절 시 재연결.

## 알려진 주의점

- 중앙 `0.0.0.0` 바인드는 신뢰 LAN 전용. `AON_DB` 없이 띄우면 토큰 검증이 stub(통과)이다 —
  실 검증 시연이 목적이면 반드시 `AON_DB`를 켠다.
- 리눅스 워커의 답 생성은 로컬 `claude` 구독 인증을 쓴다(중앙 토큰 0 — owner측 자격).
  `AON_PROVIDER=claude-api`(anthropic SDK 인프로세스)를 쓰려면 `uv sync --extra claude-api`.
- HITL 토글은 인메모리 런타임 상태 — 중앙 재시작 시 off로 리셋된다.

## Phase 12 재시연 (중앙 답변 + 지식 동기화 + 담당자 감독, 2026-07-05)

Phase 12(ADR 0033·0034)의 5개 시나리오(S1~S5)를 물리 2대에서 재현하는 절차. 위 1·2단계
(중앙/워커 기동)는 그대로 전제. **검토 대기 120초 제약**: HITL 사전 검토(S1)는 owner가
검토를 마칠 시간이 필요하므로, 큐 타임아웃 기본값(120초)보다 여유 있게
`AON_QUEUE_TIMEOUT_SECONDS=900`(15분)으로 중앙을 띄우는 걸 권장(미조정 시 검토 중에
타임아웃→에스컬레이션되어 시연이 끊긴다).

```bash
# 중앙 기동 시(1단계 대체) — 큐 타임아웃을 넉넉히
export AON_DB=$HOME/.aon/aon.db
export AON_QUEUE_TIMEOUT_SECONDS=900
scripts/run_central.sh 8000 0.0.0.0
```

### S1 — 워커 온라인 + 사전 검토

1. 워커 기동(2단계) 후 HITL on(`/console/hitl/cs_ops` 토글) — 워커가 자동 회신 대신 초안을
   보류한다.
2. 질문 인입(`POST /ask`) → push → 워커가 `claude -p`로 초안 생성 → 프레즌스 온라인이라
   보류(need owner review).
3. 리눅스 로컬 `http://127.0.0.1:8790/`에서 초안 확인·승인 → 중앙 `/ask/<tracking>` 회수 성공.

### S2 — 중앙 폴백(워커 오프라인)

1. 워커 프로세스 종료(disconnect).
2. 같은 질문을 다시 `POST /ask` — 중앙이 (중앙 지식 저장소로) 즉답하고, 그 `AnswerRecord`는
   `needs_correction_review=True`로 적재된다(오프라인 자동발신 사후교정 표식).

### S3 — 사후 교정

1. `http://<MAC_IP>:8000/supervision`(감독 화면)에서 S2 답을 찾아 정정 제출.
2. 질문자가 자기 답변 페이지(`GET /answer/{record_id}/correction`)에서 정정 배지 확인 —
   원 답은 그대로 보존(전이 ≠ 기록), 정정본이 함께 노출(풀 방식).

### S4 — 피드백

1. 질문자 채팅 화면에서 임의 답에 싫어요 제출(`POST /answer/{record_id}/feedback`).
2. 감독 화면(`/supervision`) 목록에서 그 답이 "bad 피드백" 배지와 함께 검토 필요 축에
   표출됨을 확인.

### S5 — 관리 UI(`/admin`)

1. `http://<MAC_IP>:8000/admin`에서 신규 Agent Card(예: `legal_ops`) 라이브 등록.
   필수 필드(agent_id·owner·summary·domains·last_reviewed_at 등) 누락 시 422로 admission
   검증이 동작함을 먼저 확인.
2. 등록 완료 카드의 오너를 변경(예: `hr_lead`→`finance_lead`) — 감사 로그에 전이 기록.
3. 중앙 프로세스를 재기동(같은 `AON_DB`) — 저널 리플레이로 오너 변경 상태가 생존함을
   `/admin` 화면에서 재확인.

### 발견 결함 5건과 처리 (2026-07-05)

| # | 결함 | 처리 |
|---|------|------|
| 1 | registry 미바인딩 | aef95f1 수정 |
| 2 | 큐 타임아웃 120초 하드코딩 | `AON_QUEUE_TIMEOUT_SECONDS` env 시임 신설(c1677aa) — 위 권장값 참고 |
| 3 | 프레즌스 키 오탐(agent_id vs owner) | 조회 키를 owner로 통일(c1677aa) |
| 4 | no-auth 모드 `POST /login`이 500 | no-auth면 409로 안내(이번 라운드) |
| 5 | 감독 UI 정정이 no-auth 모드에서 403 | `current_owner` 노출 + UI no-auth 폴백(이번 라운드) |

### 백로그로 남은 마찰

**분류기 어휘가 라이브 등록을 못 따라간다.** S5에서 새 도메인 카드를 라이브 등록해도,
기본 키워드 분류기/`LlmClassifier`는 빌드 시점 스냅샷이라 그 도메인 관련 신규 질문이
0매칭→에스컬레이션된다(미아 없음 불변식은 지켜지나 즉시 라우팅은 안 됨). 분류기 intents의
라이브 갱신은 별도 도메인 설계가 필요(domain-architect 백로그).
