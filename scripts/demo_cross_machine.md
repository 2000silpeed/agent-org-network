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
