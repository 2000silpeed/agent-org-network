# End-to-end 수동 시연 — 중앙 ↔ owner 워커 ↔ 실 claude ↔ 답 회수

T6.3 슬라이스2b-ii. ADR 0011 결정 6. **게이트 밖 수동 시연**(실 WS·실 claude·별 프로세스는
비결정·느림). 끊김/재연결/중복 멱등은 2b-i가 결정론으로 닫았으므로, 여기선 실 전송이 진짜로
한 바퀴 도는지를 눈으로 확인한다.

## 전제

- 로컬 `claude` CLI 설치 + **로그인됨**(워커가 `claude -p`로 답을 만든다 — API 키 불요,
  로컬 claude 인증 사용). 확인: `claude -p "ping" --output-format text`
- `.venv` 준비(`uv sync`).

## 구성요소

한 프로세스 중앙(`agent_org_network.server:central_app`)이 **사용자 web 라우트**(`POST /ask`,
`GET /ask/{tracking}`)와 **owner 워커 WS**(`/worker`)를 *같은 `WebSocketDispatcher` 하나*로
잇는다. 그래서 사용자 질문이 만든 작업이 큐에 들어가 → 연결된 워커에게 push → 워커가 로컬
claude로 답해 회신 → 사용자가 추적 토큰으로 회수한다.

데모 카드 3장(owner):
| agent_id      | owner          | 도메인       |
|---------------|----------------|--------------|
| contract_ops  | `legal_lead`   | 계약 검토    |
| cs_ops        | `cs_lead`      | 환불         |
| finance_ops   | `finance_lead` | 가격, 보상   |

분류 키워드: `계약`→contract_ops, `환불`→cs_ops, `가격`→finance_ops.
(`보상`은 cs_ops·finance_ops가 겹쳐 Contested — 워커 데모엔 부적합, 단일 owner 키워드를 쓴다.)

---

## 터미널 A — 중앙 서버

```bash
scripts/run_central.sh
# 또는 직접:
uv run uvicorn agent_org_network.server:central_app --host 127.0.0.1 --port 8000
```

`http://127.0.0.1:8000` 에 뜬다. 워커는 `ws://127.0.0.1:8000/worker`, 사용자는 `/ask`.

## 터미널 B — owner 워커 (예: cs_lead)

```bash
scripts/run_worker.sh cs_lead
# 또는 직접:
uv run python -m agent_org_network.worker --owner cs_lead --url ws://127.0.0.1:8000/worker
```

`[worker:cs_lead] 중앙에 등록됨(...). 작업 대기.` 가 보이면 붙은 것이다.
다른 owner도 답하게 하려면 터미널을 더 열어 `scripts/run_worker.sh legal_lead` 식으로 띄운다
(한 owner = 한 워커 프로세스).

## 터미널 C — 질문 던지고 답 회수

### 1) 질문 → 추적 토큰 받기

```bash
curl -s -X POST http://127.0.0.1:8000/ask \
  -H 'content-type: application/json' \
  -d '{"question":"환불 규정 알려줘"}'
```

워커가 붙어 있어도 답 생성(실 claude)은 즉시 끝나지 않으므로 보통 `pending`이 온다:

```json
{"type":"pending","kind":"dispatched","message":"담당에게 질문을 전달했어요. 답변이 준비되면 알림드릴게요.","tracking":"<HEX>"}
```

`tracking` 값(불투명 토큰 — owner_id·ticket_id를 비추지 않음, ADR 0011 결정 6-5)을 복사한다.
터미널 B의 워커 로그에 `작업 수신 ... 로컬 claude 호출 중…` → `답 회신 ...` 이 찍힌다.

### 2) 답 회수(폴링)

```bash
TRACKING=<위에서 받은 HEX>
curl -s http://127.0.0.1:8000/ask/$TRACKING
```

워커가 회신하기 전이면 같은 `pending(dispatched)`, 회신 후엔 실 claude 답이 온다:

```json
{"type":"answered","text":"<실 claude가 생성한 답>","answered_by":{"owner":"cs_lead","agent_id":"cs_ops"},"mode":"full","sources":["위키/환불정책"]}
```

답이 올 때까지 한 줄로 폴링:

```bash
until curl -s http://127.0.0.1:8000/ask/$TRACKING | grep -q '"answered"'; do
  echo "...대기"; sleep 2
done
curl -s http://127.0.0.1:8000/ask/$TRACKING
```

이걸로 **중앙→워커→실 claude→답 회수** 한 바퀴가 닫힌다.

---

## 다른 기기에서 (같은 LAN — 윈도우/맥/리눅스에 각 담당자)

위 흐름을 한 기기가 아니라 LAN의 여러 기기로 펼친다 — 한 OS = 한 담당자(owner) 워커. 중앙을
한 기기에 두고 LAN에 열면, 각 OS의 워커가 *그 기기의 로컬 claude*로 자기 담당 영역을 답한다
(분산 정신: Authority·지식·실행이 owner 환경에 있다 — ADR 0011·0017).

### 기기별 전제

각 워커 기기(윈도우/맥/리눅스)마다:
- 이 저장소 체크아웃 + `uv sync`(`.venv`) — `okf/` 번들이 함께 따라온다.
- 로컬 `claude` CLI 설치·로그인(답은 *그 기기의* claude가 만든다 — API 키 불요).
- 중앙 기기와 같은 네트워크, 중앙 포트(예 8000) 방화벽 허용.

### 중앙 기기 (IP 예: `192.168.0.10`)

```bash
scripts/run_central.sh 8000 0.0.0.0      # 0.0.0.0 = LAN에 노출(기본 127.0.0.1은 로컬 전용)
```

> 보안: 0.0.0.0 바인딩은 포트를 네트워크에 연다. 워커 등록 인증은 아직 stub(T6.5/SSO 전)이라
> 누구나 워커를 붙이거나 `/ask`를 칠 수 있다 — **신뢰된 LAN(집·테스트망)에서만** 쓰고 방화벽으로
> 통제한다. 공개망 노출 금지.

### 각 OS 기기 — 자기 담당자 워커

```bash
# 윈도우 (Git Bash/WSL 또는 PowerShell에서 동등 명령) — 예: cs_lead(환불·보상)
scripts/run_worker.sh cs_lead primary 8000 192.168.0.10

# 맥 — 예: legal_lead(계약 검토)
scripts/run_worker.sh legal_lead primary 8000 192.168.0.10

# 리눅스 — 예: finance_lead(가격)
scripts/run_worker.sh finance_lead primary 8000 192.168.0.10
```

각 워커 로그에 `중앙에 등록됨(ws://192.168.0.10:8000/worker). 작업 대기.`가 보이면 붙은 것이다.

### 질문 → 어느 기기가 답하나

중앙(또는 아무 기기)에서 질문하면 분류 키워드가 담당 워커로 라우팅된다:

```bash
curl -s -X POST http://192.168.0.10:8000/ask -H 'content-type: application/json' \
  -d '{"question":"계약서 표준 조항 알려줘"}'      # 계약 → legal_lead(맥)가 답
curl -s -X POST http://192.168.0.10:8000/ask -H 'content-type: application/json' \
  -d '{"question":"환불 규정 알려줘"}'             # 환불 → cs_lead(윈도우)가 답
curl -s -X POST http://192.168.0.10:8000/ask -H 'content-type: application/json' \
  -d '{"question":"Pro 요금제 가격 알려줘"}'        # 가격 → finance_lead(리눅스)가 답
```

각 답의 `answered_by`로 *어느 담당자(=어느 기기)*가 답했는지, 답 본문으로 *그 기기의 claude가
자기 OKF 번들을 읽어* 만든 내용임을 확인한다(`okf/cs_ops`·`okf/contract_ops`·`okf/finance_ops`
세 번들 모두 샘플 제공). owner를 안 띄운 도메인 질문은 `pending`이다가 timeout→escalation(미아 없음).

---

## 실패 모드 눈으로 보기(선택)

- **워커 끊김 → 재연결:** 터미널 B를 Ctrl-C로 끄고(작업 중이었다면 중앙이 `release_claims`로
  re-queue) 다시 `scripts/run_worker.sh cs_lead`로 띄우면, 미회신 작업이 다시 push돼 답이
  채워진다(미아 없음 — 2b-i 결정론으로 보장한 동작의 실 전송 확인).
- **워커 없이 질문 → timeout escalation:** 워커를 안 띄우고 질문하면 계속 `pending`이다가,
  중앙 큐 timeout(기본 120초)이 지나면 `poll`이 escalation으로 종착한다 — 단 사용자向
  투영은 여전히 `pending(dispatched)`(워커 미연결 vs Manager escalation은 감출 내부값, 결정 4).
- **중앙 먼저 끄고 워커만:** 워커는 연결 거부를 잡아 백오프(1→2→4…초, 최대 30초)로 재연결을
  반복한다(`[worker:...] N초 후 재연결`). 중앙을 다시 띄우면 자동으로 붙는다.

## 브라우저로 보기(선택)

중앙이 떠 있으면 `http://127.0.0.1:8000/` 채팅 UI에서도 같은 흐름을 본다(질문→pending,
워커 회신 후 폴링하면 답). 단 UI 폴링 구현 여부는 `web/index.html`에 달려 있다 — 위 curl이
가장 확실하다.
