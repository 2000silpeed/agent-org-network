# Backup 워커 수동 시연 — primary 부재 시 backup이 실 claude로 답 + owner 검토

T6.6 슬라이스 iv. ADR 0012 결정 2·4·7·9. **게이트 밖 수동 시연**(실 WS·실 claude·별 프로세스는
비결정·느림). 등급 라우팅·mode=backup 강제·검토 생성/재노출은 슬라이스 i~iii가 결정론으로
닫았으므로, 여기선 실 전송이 진짜로 한 바퀴 도는지를 눈으로 확인한다.

사용자 원 요청 실증: **"primary(owner PC) 꺼지면 owner가 위임한 backup이 실 claude로 답한다"**
+ **owner가 복귀해 처리함에서 그 미검토 답을 검토(승인/정정/무시)한다.**

## 전제

- 로컬 `claude` CLI 설치 + **로그인됨**(워커가 `claude -p`로 답 생성 — API 키 불요).
  확인: `claude -p "ping" --output-format text`
- `.venv` 준비(`uv sync`).

## 한눈에 보는 구조

한 프로세스 중앙(`agent_org_network.server:central_app`)이 사용자 web(`/ask`, `/ask/{tracking}`,
`/inbox/...`)과 owner 워커 WS(`/worker`)를 *같은 `WebSocketDispatcher` 하나*로 잇는다.
`create_central_app`이 백업 검토를 위해 다음을 *같은 인스턴스*로 와이어링한다(슬라이스 iv):

- `BackupReviewStore`·`BackupReviewService` 1개씩 → 디스패처(생성 트리거)·web(검토 탭·retrieve
  덧씌움)에 주입.
- 데모 owner들의 위임 스냅샷(`demo_delegations`, staleness 30일 fresh) → `register_delegation`.

그래서 backup 답이 "답함 → 처리함에 미검토로 뜸 → owner 검토 → 재회수 반영"으로 닫힌다.

데모 카드(owner):
| agent_id      | owner          | 키워드  |
|---------------|----------------|---------|
| contract_ops  | `legal_lead`   | 계약    |
| cs_ops        | `cs_lead`      | 환불    |
| finance_ops   | `finance_lead` | 가격    |

---

## 시나리오 A — primary 없이 backup만(가장 단순)

primary를 안 띄우고 backup만 띄운다 → 질문이 곧장 backup으로 push된다.

### 터미널 A — 중앙 서버

```bash
scripts/run_central.sh
# = uv run uvicorn agent_org_network.server:central_app --host 127.0.0.1 --port 8000
```

### 터미널 B — cs_lead의 **backup** 워커

```bash
scripts/run_worker.sh cs_lead backup
# = uv run python -m agent_org_network.worker --owner cs_lead --role backup --url ws://127.0.0.1:8000/worker
```

`[worker:cs_lead|backup] 중앙에 등록됨(...). 작업 대기.` 가 보이면 붙은 것이다.

### 터미널 C — 질문 → backup 답 회수

```bash
# 1) 질문 → 추적 토큰
curl -s -X POST http://127.0.0.1:8000/ask \
  -H 'content-type: application/json' \
  -d '{"question":"환불 규정 알려줘"}'
# → {"type":"pending","kind":"dispatched", ... ,"tracking":"<HEX>"}
TRACKING=<위 HEX>
```

터미널 B에 `[worker:cs_lead|backup] 작업 수신 ... 로컬 claude 호출 중…` → `답 회신 ...` 이 찍힌다.

```bash
# 2) 답 회수 — 답이 올 때까지 폴링
until curl -s http://127.0.0.1:8000/ask/$TRACKING | grep -q '"answered"'; do echo "...대기"; sleep 2; done
curl -s http://127.0.0.1:8000/ask/$TRACKING
```

backup이 실 claude로 만든 답이 오되 **`mode`는 `backup`으로 하향**돼 있다(워커가 full로 보내도
디스패처가 *연결 등급*을 진실로 강제 — ADR 0012 결정 4). 담당은 여전히 owner다:

```json
{"type":"answered","text":"<실 claude 답>","answered_by":{"owner":"cs_lead","agent_id":"cs_ops"},"mode":"backup","sources":["위키/환불정책"]}
```

이걸로 **"primary 부재 → owner 위임 backup이 실 claude로 답"** 한 바퀴가 닫힌다.

---

## 시나리오 B — primary 띄웠다 끄면 backup으로 전환(원 요청 그대로)

primary와 backup을 둘 다 띄운 뒤 primary를 꺼서 "owner PC가 꺼지면 backup이 답"을 그대로 본다.

### 터미널 A — 중앙 서버

```bash
scripts/run_central.sh
```

### 터미널 B — cs_lead **primary** 워커

```bash
scripts/run_worker.sh cs_lead primary
```

### 터미널 C — cs_lead **backup** 워커

```bash
scripts/run_worker.sh cs_lead backup
```

이제 같은 owner 아래 primary·backup이 둘 다 등록됐다. 디스패처는 **primary 우선** push한다.

### primary를 끈다

터미널 B를 **Ctrl-C**. (작업 중이었다면 중앙이 `release_claims`로 re-queue → backup으로 재push.)
`[worker:cs_lead|primary] 종료.`

### 터미널 D — 질문 → 이제 backup이 답

```bash
curl -s -X POST http://127.0.0.1:8000/ask -H 'content-type: application/json' -d '{"question":"환불 규정 알려줘"}'
TRACKING=<HEX>
until curl -s http://127.0.0.1:8000/ask/$TRACKING | grep -q '"answered"'; do echo "...대기"; sleep 2; done
curl -s http://127.0.0.1:8000/ask/$TRACKING
```

primary가 없으니 터미널 C(backup)에 작업 수신 로그가 찍히고, 회수하면 `mode":"backup"` 답이 온다.

> 참고("느린 primary" 회수, 결정 8): primary를 *끄지 않고* 둔 채 답을 안 하게 하면, 기본
> timeout 안에서는 primary claim이 유지된다(데모 디스패처는 t1 분할을 따로 설정하지 않는다 —
> t1/t2 결정론은 슬라이스 ii 테스트가 닫았다). 가장 확실한 backup 시연은 위처럼 primary를 끄는
> 것이다.

---

## owner 복귀 검토 — 처리함 백업 검토 탭

backup이 답한 뒤(시나리오 A 또는 B), owner가 돌아와 자기 이름으로 나간 미검토 답을 검토한다.

### 브라우저

`http://127.0.0.1:8000/inbox` 접속 → 상단 담당자에서 **cs_lead** 선택 → **"백업 검토"** 탭.
backup이 답한 항목이 빨간 배지(미검토 백업 답)와 함께 뜬다 — 원문 질문·백업이 낸 답·스냅샷 시각.

- **승인**: 답이 맞다 → 검토 완료. 재회수 시 `mode=full`로 승격(신뢰 복원), text 그대로.
- **정정**: "정정" 버튼 → 입력칸이 펼쳐진다 → 정정 답 입력 후 다시 "정정" → 새 답 발행.
  재회수 시 정정 text + `mode=full`.
- **무시**: 검토만 하고 조치 안 함. 재회수 시 큐의 원 backup 답 그대로(`mode=backup` 유지).

검토를 마치면 그 항목은 "백업 검토" 탭(pending)에서 사라진다.

### curl로도 검토

```bash
# pending 목록 → item_id 확인
curl -s http://127.0.0.1:8000/inbox/cs_lead/backup-reviews
ITEM=<위 item_id>

# 정정(1인칭 — by_owner는 반드시 그 owner. 타인이면 400)
curl -s -X POST http://127.0.0.1:8000/backup-reviews/$ITEM \
  -H 'content-type: application/json' \
  -d '{"type":"correct","by_owner":"cs_lead","corrected_text":"정정된 환불 안내입니다."}'

# 같은 tracking으로 재회수 → 정정 반영(mode=full)
curl -s http://127.0.0.1:8000/ask/$TRACKING
```

```json
{"type":"answered","text":"정정된 환불 안내입니다.","answered_by":{"owner":"cs_lead","agent_id":"cs_ops"},"mode":"full","sources":[]}
```

이걸로 **"backup 답 → 처리함 미검토 → owner 정정 → 사용자 재회수에 반영"** 의 책임 루프가 닫힌다
(ADR 0012 결정 7 — 책임이 명목에서 실질로).

---

## 손대지 않은 것(연결점만 — 후속, ADR 0012)

- **실 데이터 동기화 파이프라인**: 데모 backup은 primary와 *같은 카드 데이터*로 답한다. owner의
  실 지식 스냅샷을 격리 저장소로 동기화하는 실 파이프라인은 후속(`DelegationSnapshot.snapshot_at`
  은 위임 *메타*만, 실 데이터 본체·암호화/키는 연결점). staleness 거부를 보려면
  `demo_delegations(snapshot_at=<과거>)`로 stale을 만들면 backup이 거부되고 escalation으로 종착한다
  (결정 9 — "모르면 넘김").
- **cold start 실 기동**: 평소 꺼둔 backup 인스턴스를 깨우는 오케스트레이션(`wake_backup` hook)은
  연결점만(결정 10). 데모는 backup을 미리 띄워 둔 warm 시연이다.
- **자동 푸시 통지**: 정정/escalation의 사용자 통지는 조회(`retrieve`)로 대신한다(푸시는 ADR 0011
  결정 6-5 범위 밖).
