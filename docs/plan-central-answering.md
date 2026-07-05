# 계획 — 실행 모델 전환: 중앙 답변 + 지식 동기화 + 담당자 감독

작성일: 2026-07-04 · 작성: planner · 상태: **계획(구현 착수 전)** · 대상 Phase: **Phase 12**

> 이 문서는 *상세 계획*이다 — 무엇을·어떤 순서로·어떻게 검증하나까지. 설계(타입·ADR·shape)는
> domain-architect, 구현(red→green)은 tdd-engineer, 실 어댑터(게이트 밖)는 mcp-runtime-engineer가
> 이어받는다. 이 문서 자체가 코드를 확정하지 않는다.

---

## 1. 목표 · 범위

### 목표

현재 실행 모델(**대화 답변 = owner 워커(PC)가 owner OAuth로 인프로세스 실행**, ADR 0027)을
다음으로 전환한다:

1. **지식 동기화** — 담당자(워커) 환경의 지식을 중앙 지식 저장소로 계속 자동 반영. 워커의 역할이
   "답변 실행자"에서 "**지식 공급자**"로 이동한다.
2. **중앙 답변** — 답은 중앙 런타임이 중앙 지식 저장소 기반으로 생성. 담당자 PC 가동 여부와 무관하게 가용.
3. **프레즌스** — 담당자 연결 상태(온라인/오프라인)를 항상 추적(워커 WS 연결 자체가 사실상 하트비트).
4. **모니터링 + 사후 교정** — 담당자가 자기 에이전트에 들어온 질문·나간 답을 열람하고, 잘못된 답을
   고칠 기회를 가진다. 정정 시 질문자에게 정정 통지 + 판례/지식 갱신.
5. **HITL 정책 변화** — 온라인이면 사전 검토(pre-send review), 오프라인이면 자동 발신 + 사후 교정.
   에이전트별 정책.

### 범위 (In)

- 라우팅 계층 **위에 얹는** 실행 계층 재구성: 지식 저장소·프레즌스·중앙 런타임 소비 경로·정정 이벤트·
  HITL 정책 분기·담당자 모니터링/교정 면.
- SSOT 갱신(PRD·TRD·CONTEXT·README·새 ADR 지시) — **구현보다 선행**.

### 범위 밖 (Out)

- **라우팅 계층 전부 무변경** — 매처 사슬·`RoutingDecision` sealed sum·판례·에스컬레이션·4대 불변식.
  바뀌는 것은 실행(런타임) 계층뿐.
- **지식 저장소의 "무엇을 올리나" 경계·민감정보 정책·LLM 비용 귀속·정정 통지 채널·stale 임계**는
  전부 *외부 결정 지점*(§5). 결정 전엔 그 슬라이스를 "결정 대기"로 둔다.
- fan-out·다중 공급자 동시·Postgres 등 Phase 9~11에서 이미 후속 연기한 것들은 그대로 연기.

---

## 2. PRD/TRD 충돌·확장 지점 (규칙 1 — 구현 전 문서 갱신 선행)

이 전환은 **한 축에서 정면 재정의**, 나머지는 확장이다. 정면 재정의 축은 되돌리기 어려운 결정이라
**domain-architect의 새 ADR 없이는 구현 착수 금지**다.

### [정면 재정의] 답 실행 위치 — ADR 0027(→0010→0017) 계보를 *네 번째로* 뒤집는다

- **현재 SSOT**: ADR 0027(Phase 9)이 "대화 답변 = owner 워커가 owner OAuth 구독 토큰으로 공급자 API를
  인프로세스 직접 스트리밍"으로 못박았다. CONTEXT `Agent Runtime`·`Provider Runtime` 절, PRD §3·§5·§6,
  TRD §2·§4가 전부 이 방향으로 서술됨. ADR 0011·0012 WS 전송이 "기본 대화 경로"로 *재부상*한 상태.
- **이번 전환**: 답 실행을 다시 **중앙 런타임**으로 옮긴다. 다만 이건 ADR 0017(중앙 `claude -p`)로의
  단순 회귀가 *아니다* — 이번엔 중앙이 **지속 동기화된 중앙 지식 저장소**를 소비한다(0017의 "OKF 커밋
  스냅샷 cwd 읽기"와도 다른 저장소 모델). 워커는 답을 *안 만들고* 지식을 *올린다*.
- **계보의 역설 정리**(정직하게):
  - 0010 "owner Claude Code 실행" → 0017 "중앙 실행(백업 워커 자기모순 근거)" → 0027 "owner OAuth
    인프로세스 실행 부활(가용성은 백업/escalation/HITL이 흡수)" → **이번: 중앙 실행 재부활**.
  - **0017이 옳았던 근거가 이번에 되살아난다** — "owner PC는 잠든다(가용성)". 0027이 이 반대를
    "백업 워커·escalation·HITL로 흡수"한다고 했으나, 사용자 판단은 *백업 워커라는 격리 인스턴스
    호스팅 자체가 복잡하고*, 더 단순한 해법이 "지식을 중앙에 계속 올려두고 중앙이 답"하는 것이다.
  - **0027이 옳았던 근거는 어떻게 되나** — (a) "멀티-LLM OAuth = owner 자격증명 필요·중앙 토큰 0"
    → **외부 결정 §5-C(LLM 비용 귀속)로 이동**. 중앙 답변이면 자격증명이 중앙으로 갈 수밖에 없어
    "중앙 토큰 0" 불변식이 흔들린다 — 이게 이 전환의 *가장 무거운 되돌리기 어려운 트레이드오프*다.
    (b) "인프로세스 스트리밍 속도" → 중앙도 인프로세스 스트리밍 가능(런타임 위치만 이동).
- **domain-architect에 넘길 것**: 새 ADR(가칭 **"중앙 답변 + 지식 동기화 실행 모델"**)에서
  ① ADR 0027 결정 2·4(owner OAuth·중앙 토큰 0) 재정의 ② ADR 0017 계보와의 관계 명문화(0017 근거
  부활·0027 근거 이동) ③ "중앙 토큰 0" 불변식의 운명 결정(유지 불가면 명시 폐기·대안 정의).
  **이 ADR 확정 전엔 S2 이후 착수 금지.**

### [확장] 지식 동기화 — ADR 0013(OKF)·0018(git 저장)·0028(published 인덱스)의 재조합

- **현재 SSOT**: OKF 번들은 owner-로컬 git(ADR 0030)에 있고, 중앙은 답변 시 *읽거나*(0017 커밋 스냅샷)
  *목차만 받는다*(0028 `PublishIndex` — 본문 0·중앙 토큰 0). Phase 10이 이미 "owner→중앙 목차 배포"
  채널(`PublishIndex` WS 프레임)을 열었다.
- **이번 전환**: 중앙이 *답하려면 목차가 아니라 지식 본문*이 필요하다. 0028의 "중앙은 목차만·본문 0"
  불변식과 **정면으로 충돌**한다 — 중앙 지식 저장소는 *본문을 담는다*. 이건 0028·0030의 "비소유·중앙
  본문 0" 재정의다.
- **재사용 가능한 조각**: `PublishIndex` WS 채널(ADR 0028 §14)·`OkfChangeEvent`(0019 커밋=이벤트)·
  워커 publish 파이프라인(`publish_frames`·ADR 0030 S3)·`GitGateway` 포트(0018). "목차 배포"를
  "본문(또는 본문+목차) 동기화"로 확장하되 admission 검증(0028 권한 대조)은 유지.
- **domain-architect에 넘길 것**: 지식 저장소 도메인(`KnowledgeStore`?)·동기화 프로토콜·본문 담기의
  admission·"중앙 본문 0" 불변식 재정의(0028/0030과의 관계).

### [확장] 프레즌스 — ADR 0011 WS 연결·0022 통지 패턴의 승격

- **현재 SSOT**: 워커 WS 연결은 `WebSocketDispatcher._connections`에 owner당 role별로 보관(0012).
  연결/해제가 이미 관측되나 *프레즌스라는 1급 개념*은 없다(연결은 "push 대상 선택" 용도).
- **이번 전환**: 연결 상태를 **프레즌스(온라인/오프라인) 1급 개념**으로 승격. 콘솔 SSE(ADR 0024)가
  이미 "워커 연결/해제" 피드를 예고했으니(PRD §4 운영자 행) 확장이지 충돌 아님.
- **domain-architect에 넘길 것**: `Presence` 값/포트·WS 연결→프레즌스 도출·HITL 정책 분기의 입력.

### [확장] 사후 교정 — ADR 0012 `BackupReview`·0019 reeval·0022 통지의 재조합

- **현재 SSOT**: `BackupReview`(승인·정정·무시, 0012)는 *백업 워커가 낸 답*의 owner 복귀 검토다.
  `ReevalOutcome`(0019 — Keep/Invalidate/Supersede·Acknowledge/ReAnswer)은 지식 변경 시 과거 판례·답
  재평가다. `Notifier`(0022)는 처리함 적재 시 push.
- **이번 전환**: "담당자가 *중앙이 낸 답*을 사후 열람·교정"은 `BackupReview`의 *일반화*다(백업 답만이
  아니라 **모든 오프라인/자동 발신 답**이 검토 대상). 정정 시 **질문자에게 정정 통지**(현재 통지는
  owner/manager 운영 면만·질문자 통지는 신규) + **판례/지식 갱신**(reeval 경로 재사용).
- **불변식 유지**: **전이 ≠ 기록** — 정정은 답변 레코드 *수정*이 아니라 **새 정정 이벤트**로 쌓는다
  (감사 추적 보존). 이건 append-only audit·`action_record`(정비 라운드) 패턴 그대로.
- **domain-architect에 넘길 것**: 답변 레코드·정정 이벤트 도메인·`BackupReview` 일반화 vs 신규 타입
  판단·질문자 통지(0022 `Notification` recipient 확장 — 현재 owner/manager User.id만).

### [확장] HITL 정책 분기 — ADR 0025 HITL 토글의 프레즌스 연동

- **현재 SSOT**: HITL 토글(0025)은 `hitl_on → draft_only`(검토)·`off → full`(자동)이고 *에이전트별
  in-memory 토글 맵 + 콘솔 런타임 토글*이다(운영자가 수동 on/off).
- **이번 전환**: 토글의 *입력에 프레즌스를 더한다* — 온라인=검토(기존 draft_only)·오프라인=자동+사후교정.
  0025의 "안전 방향 단조성"(카드 approval_when은 못 풂)은 유지. 이건 0025 토글 *입력 소스 확장*이지
  새 기계 아님.
- **domain-architect에 넘길 것**: 프레즌스→HITL mode 매핑(순수 함수·`hitl_to_mode` 확장)·에이전트별
  정책과 프레즌스의 결합 규칙·단조성 보존.

### [부수 결정] 외부 배포 네트워킹 — Tailscale → 사내망 기본·Headscale 1순위

- **현재 SSOT**: tasks 정비 라운드 잔여에 "Tailscale 채택 후보(후순위)"로 기록됨(사용자당 과금은
  미언급). 이번에 사용자 판단으로 **사내망 배치 기본**(중앙을 사내망에 두면 외부 노출 0·비용 0),
  외부 접속 필요 시 **Headscale(자체 호스팅) 1순위**로 조정. tasks 항목 갱신(이 계획에 포함·직접 갱신).

---

## 3. 슬라이스 분해

각 슬라이스 = {무엇 · 게이트 내/밖 · 검증 방식 · 건드리는 불변식 · SSOT 영향}.

### S0 — SSOT 갱신 (문서 선행·게이트 밖·수동 검토)

- **무엇**: PRD §3·§5·§6·TRD §2·§4·CONTEXT(Agent Runtime·Provider Runtime·Knowledge Bundle 절)에
  "현행 구조 vs 전환 방향" 구분 서술 추가. README 아키텍처/실행 모델 절 갱신(현행 vs 전환 구분·
  구현된 것처럼 쓰지 말 것). tasks에 Phase 12 절 추가. **새 ADR 초안 작성 지시**(domain-architect).
  Tailscale 항목 갱신(사내망 기본·Headscale 1순위).
- **게이트**: 밖(문서). 코드 게이트 불필요.
- **검증**: 문서 형식·톤 정합 수동 검토(한국어·기존 tasks 표기 관례). ADR 계보 서술의 정직성
  (0017 근거 부활·0027 근거 이동을 왜곡 없이).
- **불변식**: 서술만 — 4대 불변식 문구·"중앙 토큰 0"의 운명을 ADR에서 명시적으로 다룰 것.
- **SSOT 영향**: PRD·TRD·CONTEXT·README·tasks·새 ADR(초안 지시).
- **넘김**: 이 계획이 tasks/README/Tailscale은 직접 처리. PRD/TRD/CONTEXT 본문 + 새 ADR은
  domain-architect(설계 확정 후 규칙 2로).

### S1 — 도메인 개념 추가 (설계·shape·게이트 내 shape / 확정은 domain-architect)

- **무엇**: 지식 저장소(중앙 본문 담는 `KnowledgeStore`?)·프레즌스(`Presence`)·답변 레코드/정정
  이벤트·에이전트별 HITL 정책(프레즌스 연동)의 타입·포트 설계. 용어 확정(§6 후보).
- **게이트**: 내(값 객체·포트 Protocol + Fake 구현·주입 결정론 shape). 실 저장소·실 동기화는 밖.
- **검증**: pydantic frozen 값 객체·포트+Fake·주입 spy로 결정론 단언(기존 `SessionStore`·
  `ReevalStore`·`TokenStore` 패턴 N번째).
- **불변식**: 전이≠기록(저장소는 순수 보관·정정은 새 이벤트)·노출 불변식(사용자 Answered 미노출값
  구분)·Authority 중앙(지식 동기화가 권한 자기보고로 새지 않게).
- **SSOT 영향**: CONTEXT 용어·TRD §4 도메인 모델·새 ADR.
- **넘김**: **domain-architect**(이 슬라이스가 설계의 8할 — 지식 경계·admission·불변식 재정의).

### S2 — 중앙 답변 경로 (게이트 내 / 실 런타임·자격증명은 밖)

- **무엇**: 중앙 런타임이 지식 저장소를 소비해 답 생성. 기존 라우팅 결과(`RoutingDecision`)를 소비하는
  경로를 워커 dispatch에서 *중앙 인프로세스 소비*로 전환. `AgentRuntime` 포트 재사용(위치만 중앙).
- **게이트**: 내 — 지식 저장소(Fake)·`StubRuntime`/`ClaudeApiRuntime`(주입 `ProviderTransport` Stub)
  로 결정론. **실 LLM 자격증명·실 스트리밍은 밖**(mcp-runtime-engineer·수동).
- **검증**: FakeKnowledgeStore + StubRuntime 주입 → 오프라인 owner라도 답 나오는지 결정론 단언
  (핵심 가치 검증 — PC 꺼져도 답). 라우팅 회귀 0(런타임 위치 이동이 종착 안 바꿈).
- **불변식**: 미아 없음(위치 이동이 종착 무변경)·노출 불변식(`Answer` 계약 보존).
  **중앙 토큰 0은 이 슬라이스가 건드린다** — ADR(§5-C) 결정 없이는 게이트 밖 실 배선 금지.
- **SSOT 영향**: TRD §2·§4.
- **넘김**: tdd-engineer(게이트 내)·mcp-runtime-engineer(실 자격증명·게이트 밖·**§5-C 결정 후**).
- **선행 의존**: **S0의 새 ADR 확정 + §5-C(LLM 비용 귀속) 결정**.

### S3 — 지식 동기화 채널 (게이트 내 프로토콜 / 실 WS·실 git은 밖)

- **무엇**: 워커→중앙 지식 업로드 프로토콜 + admission 검증. 0028 `PublishIndex` 채널을 "본문 동기화"로
  확장(또는 병행 신규 프레임). 커밋=이벤트(0019) 재사용해 자동 반영.
- **게이트**: 내 — WS 프레임 DTO·중앙 수용 로직·admission(권한 대조)은 Fake 리스너로 결정론.
  **실 소켓·실 git·실 크로스머신은 밖**(mcp-runtime-engineer·수동).
- **검증**: Fake 워커→프레임→중앙 저장소 반영 결정론 단언. admission(over-claim 본문 필터·0028 정신).
  동기화 신선도(stale 지식 표식).
- **불변식**: Authority 중앙(동기화가 권한 자기보고 못 넓힘·0028 over-claim 필터 재사용)·
  등록 무결성(유효하지 않은 지식은 반영 안 됨).
- **SSOT 영향**: TRD §2·§4·CONTEXT 동기화 용어.
- **넘김**: tdd-engineer(프레임·수용 로직)·mcp-runtime-engineer(실 WS/git).
- **선행 의존**: S1(지식 저장소 도메인).

### S4 — 프레즌스 + HITL 정책 분기 (게이트 내)

- **무엇**: 워커 WS 연결→프레즌스(온라인/오프라인) 도출. HITL 정책 분기(온라인=사전검토·
  오프라인=자동발신+사후교정). 0025 토글 입력에 프레즌스 결합(단조성 보존).
- **게이트**: 내 — 연결 상태 주입·`presence_to_hitl` 순수 함수·mode 매핑 결정론. 실 연결 이벤트는 밖.
- **검증**: 프레즌스 상태별 mode 분기 결정론(온라인→draft_only·오프라인→full+교정 플래그).
  단조성(카드 approval_when은 오프라인이라도 못 풂).
- **불변식**: 노출 불변식(mode는 노출값)·Authority 중앙(토글은 신뢰 게이트지 권한 아님)·전이≠기록.
- **SSOT 영향**: TRD §4(HITL 절·프레즌스 절)·CONTEXT.
- **넘김**: tdd-engineer(순수 함수·매핑)·mcp-runtime-engineer(실 연결 이벤트→프레즌스·게이트 밖).
- **선행 의존**: S1(프레즌스 도메인).

### S5 — 담당자 모니터링 + 사후 교정 루프 (게이트 내 상태기계 / 실 UI·실 통지는 밖)

- **무엇**: 담당자가 자기 에이전트 Q&A 열람(모니터링)·정정. 정정 이벤트→질문자 정정 통지 +
  판례/지식 갱신(reeval). `BackupReview` 일반화 또는 신규 타입.
- **게이트**: 내 — 정정 상태기계·정정 이벤트 append(전이≠기록)·통지 발화(FakeChannel)·reeval 연동은
  결정론. **실 통지 채널·실 UI·실 질문자 도달은 밖**.
- **검증**: 정정→새 이벤트 append(원 답 레코드 불변 단언)·질문자 통지 발화(FakeChannel inbox)·
  reeval 적재. 멱등(중복 정정 통지 방지).
- **불변식**: **전이 ≠ 기록**(정정=새 이벤트·원 레코드 수정 금지·감사 추적 보존)·노출 불변식
  (정정 통지는 질문자에게 담당·정정 사실만)·미아 없음(무관).
- **SSOT 영향**: TRD §4(답변 레코드·정정 이벤트)·CONTEXT·PRD §4(담당자 화면).
- **넘김**: tdd-engineer(상태기계·이벤트)·mcp-runtime-engineer(실 통지·실 UI·게이트 밖).
- **선행 의존**: S1(답변 레코드/정정 이벤트 도메인)·S2(중앙 답이 있어야 교정 대상 존재).

---

## 4. 의존성 · 권장 순서

```
S0 (SSOT+ADR 선행) ──┬──▶ S1 (도메인) ──┬──▶ S2 (중앙 답변)   ──┐
                     │                 ├──▶ S3 (동기화 채널)  │
                     │                 └──▶ S4 (프레즌스+HITL) │
                     └── ADR 확정이 S2 게이트 밖 실 배선을 막음 └──▶ S5 (교정 루프)
```

- **첫 타자 = S0 → S1**. S0(문서+ADR)이 정면 재정의 축을 닫아야 나머지가 착수 가능. S1(도메인)은
  나머지 전부의 타입 기반이라 그다음. 근거: 되돌리기 어려운 결정(중앙 토큰 0)이 S0 ADR에 걸려 있어
  리스크가 가장 높고, 그걸 먼저 닫아야 S2~S5의 방향이 확정된다.
- **S1 후 병렬 가능**: S3(동기화)·S4(프레즌스+HITL)는 서로 독립(지식 저장소 축 vs 연결 상태 축).
  self-contained·검증 쉬운 순으로는 **S4(순수 함수 매핑·가장 작음) → S3 → S2 → S5**.
- **S5는 마지막** — S2(중앙 답 존재)에 의존(교정 대상이 있어야 함).
- **게이트 밖 실 배선(S2·S3의 실 자격증명/실 WS)은 §5 외부 결정 확정 후**.

---

## 5. 외부 결정 지점 (사용자가 정해야 함 — 결정 전 해당 슬라이스 "결정 대기")

### A. 지식 경계 — "무엇을 올리고 무엇을 안 올리나" (S1·S3 차단)

- 중앙 지식 저장소에 담는 단위: OKF 번들 전체 본문? 일부? 목차(0028)+본문 온디맨드?
- 트레이드오프: 전체 본문 = 중앙 답변 완결·가용성 최대 / 비소유·중앙 저장 최소화 원칙과 충돌.
  목차만 = 0028 유지·중앙 토큰 0 유지 가능하나 *중앙이 본문 없이 어떻게 답하나* 미해결.
- **권장**: domain-architect가 "본문 담되 owner 통제·감사"로 shape 후 사용자 확정.

### B. 민감정보 거버넌스 (S1·S3 차단)

- owner 환경 지식 중 민감정보(개인정보·사내 비밀)를 중앙에 올릴 때 필터/마스킹/제외 정책.
- 트레이드오프: 강한 필터 = 안전하나 답 품질 저하 / 약한 필터 = 중앙 지식 저장소가 민감정보 집적소.
- **권장**: owner 명시 제외 목록 + admission 시 검증(0028 권한 대조 재사용). 사용자 확정.

### C. LLM 비용 귀속 — 담당자 로컬 → 중앙 과금 (S2 차단·가장 무거움)

- 현재 "중앙 토큰 0"(owner OAuth·ADR 0010·0027)이 전제. 중앙 답변이면 자격증명이 중앙으로 감 →
  **중앙 토큰 0 불변식이 깨진다**. 이건 이 전환의 *가장 되돌리기 어려운 트레이드오프*.
- 선택지: ① 중앙 API 키(중앙 과금·중앙 토큰 0 폐기) ② owner OAuth를 중앙이 위임 보관(자격 위임·
  보안 리스크) ③ 하이브리드(온라인 owner=owner 자격·오프라인=중앙 자격).
- **권장**: domain-architect가 "중앙 토큰 0"의 운명을 새 ADR에서 명시 결정(유지 불가면 정직하게
  폐기·대안 정의). **사용자 확정 없이는 S2 실 배선 착수 금지.**

### D. 정정 통지 채널 — 질문자 도달 방식 (S5 차단)

- 현재 통지(0022)는 owner/manager 운영 면(MCP 첫 채널). 질문자(익명 세션·MCP 클라이언트)에게 정정을
  어떻게 도달시키나 — 세션 재접속 시 표시? MCP push? 이메일?
- **권장**: 세션 기반 표시(재접속 시)부터 증분·실시간 push는 후속. 사용자 확정.

### E. 동기화 주기 · stale 임계 (S3·S4 차단)

- 지식 동기화 주기(커밋 시 즉시? 폴링? 배치)와 stale 지식 임계(중앙 지식이 얼마나 오래되면 "낡음"
  표식·답 신뢰 하향).
- **권장**: 커밋=이벤트(0019) 즉시 반영 + `last_synced_at` 신선도 신호. 임계값은 사용자 확정
  (0024 유휴 타임아웃 권장 30분 선례 참조).

### F. 프레즌스→HITL 정책 세부 (S4 차단·경미)

- 오프라인 판정 기준(연결 끊김 즉시? grace period?)·에이전트별 정책 기본값(카드 approval_when 시드
  유지?).
- **권장**: 연결 끊김 즉시 오프라인 + 카드 시드 유지(0025 정신). 사용자 확정.

---

## 6. 용어 후보 (CONTEXT.md 유비쿼터스 언어 — 확정은 domain-architect)

> 아래는 *후보*다. sealed sum/포트/불변식과의 정합·`_Avoid_` 목록은 domain-architect가 확정한다.

- **Knowledge Store (중앙 지식 저장소)** — 중앙이 답변에 소비하는, 동기화된 owner 지식 본체.
  기존 `Knowledge Bundle`(owner-로컬)과 구분. _후보 Avoid_: RAG corpus·Vector store·중앙 소유.
- **Knowledge Sync (지식 동기화)** — 워커→중앙 지식 반영. 0028 "목차 배포"의 본문 확장.
  _후보 Avoid_: 미러·복제(중앙 소유 함의 주의).
- **Presence (프레즌스)** — 담당자 워커 연결 상태(온라인/오프라인). WS 연결에서 도출.
  _후보 Avoid_: Heartbeat(단독·메커니즘)·Status(모호).
- **Answer Record (답변 레코드)** — 중앙이 낸 답의 감사 단위(전이≠기록). audit 기반.
- **Correction Event (정정 이벤트)** — 담당자 사후 교정을 원 레코드 수정 없이 쌓는 append-only 이벤트.
  `BackupReview`·`action_record` 정신. _후보 Avoid_: Answer 수정·Edit(원 레코드 변경 함의).
- **Supervised Answering / Post-hoc Correction (사후 교정)** — 오프라인 자동 발신 답을 담당자가
  복귀 후 검토·정정하는 루프. `BackupReview` 일반화.
- **Knowledge Provider (지식 공급자)** — 워커 역할의 재정의("답변 실행자"→"지식 공급자").

---

## 7. 넘김 표

| 슬라이스 | 게이트 내(구현) | 게이트 밖(실 어댑터) | 설계·ADR |
|---|---|---|---|
| S0 | — | — | planner(tasks/README/Tailscale 직접) · **domain-architect(PRD/TRD/CONTEXT·새 ADR)** |
| S1 | tdd-engineer(값 객체·포트+Fake) | — | **domain-architect(핵심 — 지식 경계·불변식 재정의)** |
| S2 | tdd-engineer(Fake 저장소+Stub 런타임) | mcp-runtime-engineer(실 자격증명·스트리밍·**§5-C 후**) | domain-architect |
| S3 | tdd-engineer(프레임·수용·admission) | mcp-runtime-engineer(실 WS·실 git·크로스머신) | domain-architect |
| S4 | tdd-engineer(순수 매핑·mode 분기) | mcp-runtime-engineer(실 연결→프레즌스) | domain-architect |
| S5 | tdd-engineer(정정 상태기계·이벤트·통지 발화) | mcp-runtime-engineer(실 통지·실 UI·질문자 도달) | domain-architect |

리뷰는 각 슬라이스 green 후 code-reviewer → 게이트 → 규칙 2 SSOT 갱신.

---

## 8. 자체 점검 — 핵심 불변식 (슬라이스별)

- **미아 없음**: 라우팅 무변경(S2 위치 이동이 종착 안 바꿈). 오프라인 owner라도 중앙 답 → 오히려
  가용성 *강화*(0매칭은 여전히 root escalation).
- **Authority 중앙**: 지식 동기화(S3)가 권한 자기보고로 새지 않게 — 0028 over-claim 필터 재사용.
  프레즌스·HITL(S4)은 신뢰 게이트지 권한 아님.
- **전이 ≠ 기록 ≠ 통지**: 정정(S5)은 새 이벤트(원 레코드 불변)·통지는 적재 뒤 부작용.
- **노출 불변식**: 사용자 Answered엔 담당·신뢰·출처·정정만(내부값·동기화 메타 미노출).
- **등록 무결성**: 유효하지 않은 지식은 동기화 반영 안 됨(admission).
- **⚠️ 중앙 토큰 0**: S2가 정면으로 건드림 — **이 불변식의 운명은 새 ADR(§5-C)이 명시 결정**.

---

## 9. 관리 UI — 카드 등록·오너 변경 (도메인 설계, domain-architect)

> 사용자 승인 요구: **신규 Agent Card 추가와 Owner 변경이 쉬운 관리 UI**. 이 절은 "무엇이
> 무엇인지"만 확정한다(구현은 3라운드). 되돌리기 어려운 결정(§9.6)만 ADR급 판정이 필요하다.
> 아래는 모두 **읽기로 실확인한 코드 근거**를 인용한다.

### 9.1 실확인한 현재 지형 (근거)

- **admission 채널이 이미 존재한다** — `validate_card_for_builder(req, registry)`
  (`web.py:662`)가 카드 후보를 admission 규칙으로 검증한다: ① `AgentCard.model_validate`
  (필수 필드·타입·`agent_id` 형식 — `agent_card.py:118` field_validator·ADR 0023) ②
  참조 무결성(`card.owner`가 Registry 실재 User·maintainer 실재, `Registry.validate`
  정신·`registry.py:61`). 통과 시 **라이브 등록 없이** `registry/agents/{agent_id}.yaml`
  텍스트를 낸다 — 편집 채널은 git/PR(CONTEXT Maintainer). "**라이브 레지스트리 mutation은
  하지 않는다**"(`web.py:640`)가 명시돼 있다.
- **Authority 중앙**: 카드는 under-claim만 자기보고(ADR 0004). 권한 선언의 SSOT는 중앙
  파일(`routing_rules.yaml`·`registry/agents/*.yaml`)이고 Registry는 파일에서 `load`된다
  (`registry.py:69`).
- **owner 필드**: `AgentCard.owner: str`(`agent_card.py:105`) — User.id 참조. `frozen=True`
  값 객체라 "owner 변경"은 필드 mutation이 아니라 **새 카드 값으로의 교체(재-admission)**다.
- **`AnswerRecord.answered_by`는 owner_id**로 채워진다(`ask_org.py:766`
  `answered_by=(card.owner, card.agent_id)` → 레코드는 owner 쪽 저장, `answer_record.py:67`).
- **`submit_correction` 판정**(`answer_record.py:207`): `by_owner != record.answered_by`면
  거부 — **오너 변경 후 새 owner의 정정을 막는다**(§9.4에서 해결).
- **TokenStore**: `AdmissionToken.owner_id` 귀속(`token.py:64`)·`revoke(token_id)`
  append-only(`token.py:173`). verify는 owner_id를 회신 출처로 강제(owner 격리 불변식).
- **HitlToggleMap 키 = agent_id**(`hitl.py:70` `is_on(agent_id)`) — owner_id 아님.
- **Presence 키 = agent_id**(`presence.py:56` `observe_connect(agent_id)`).
- **PendingDraft**: owner 워커 로컬 in-memory(`worker.py:220` `ticket_id → PendingDraft`) —
  중앙 상태가 아니라 **구 owner 워커 프로세스 안**에 있다.

### 9.2 오너 변경 시 각 축의 운명 표

전제: 오너 변경은 **agent_id는 그대로**, 그 카드의 `owner` User.id만 A→B로 바뀐다.

| 축 | 키 | 운명 | 근거·이유 |
|---|---|---|---|
| Knowledge Store | `agent_id` | **유지** | 키가 agent_id라 owner 무관. 지식은 카드에 붙지 사람에 붙지 않음. |
| Precedent(판례) | `agent_id` | **유지** | 동상. 과거 판례는 카드 이력. |
| HitlToggleMap | `agent_id` | **유지(정책 재검토 권고)** | 키가 agent_id(`hitl.py:70`)라 owner 무관하게 살아있음. 단 토글은 신뢰 게이트라 새 owner가 재확인하는 게 안전 — 강제 리셋은 하지 않되 UI가 "새 owner 확인 요망" 표시. |
| PendingDraft(보류 초안) | 구 owner 워커 로컬 | **무효화(drain-or-drop)** | 구 owner 워커 프로세스 로컬 상태(`worker.py:220`). 새 owner 워커엔 없음. 세션 무효화 시 접근 끊김 → **방치 보류의 종착은 중앙 재큐잉**(worker.py:83 주석 정신)이거나 drop. 구 owner가 미제출 초안을 새 owner 권한으로 회신하면 owner 격리 위반. |
| Presence(프레즌스) | `agent_id` | **재관측까지 offline** | 구 owner 워커 WS 끊기면 `observe_disconnect`(offline). 새 owner 워커가 재연결하면 재관측. 미관측=offline 안전 기본(`presence.py:11`). |
| Worker Token | `token_id`(owner_id 귀속) | **revoke(무효화 필수)** | **보안 핵심.** 구 owner 토큰의 `owner_id`가 여전히 구 A라 verify 통과 시 구 owner가 이 카드 회신을 계속 낼 수 있음 → owner 격리 붕괴. 재-admission 스위치 시 **해당 카드에 매인 구 owner 토큰을 revoke**(`token.py:173` append-only)하고 구 WS 세션을 끊는다. |
| AnswerRecord.answered_by | 과거 기록 | **불변(그대로)** | 전이≠기록. 과거 답은 구 owner가 낸 게 사실이므로 필드는 절대 변경 X(`answer_record.py:56` append-only). |
| 정정 권한 | — | **새 owner로 이동** | §9.4 — 판정 기준을 "현재 카드 owner"로. |

### 9.3 오너 변경의 전이 모델 (전이 ≠ 기록)

오너 변경은 **전이(도메인 상태 변화)**이고, 그 사실은 별도로 **감사 로그(기록)**에 남는다.
재-admission 흐름을 순서로 확정:

1. **제출** — 관리 UI가 대상 카드의 새 `owner`(B)로 갱신된 카드 후보를 제출한다.
   전용 우회 API 없음 — **기존 admission 채널**(§9.5)을 그대로 호출.
2. **admission 검증** — `validate_card_for_builder` 정신으로 새 카드를 검증(agent_id 형식·
   새 owner B 실재·참조 무결성). **무효면 스위치 없음**("무효 카드 등록 안 됨" 불변식).
3. **스위치(전이)** — 검증 통과 시 Registry의 그 카드 값을 새 owner 카드로 교체. frozen
   값이므로 교체(구 카드 값 → 새 카드 값). Authority는 여전히 중앙 파일 선언.
4. **구 세션/토큰 무효화** — 스위치와 **원자적으로**: (a) 구 owner A의 그 카드용 워커
   토큰 `revoke`(`token.py:173`), (b) 구 owner 워커 WS 세션 끊기(presence offline로 귀결),
   (c) 구 owner 워커 로컬 PendingDraft는 접근 불가(drop 또는 중앙 재큐). **순서 중요**:
   revoke가 스위치보다 늦으면 그 창(window) 동안 구 owner가 새 소유 카드로 회신 가능 →
   보안 구멍. 그래서 **revoke를 스위치와 같은 임계 구역**에 둔다.
5. **감사 기록** — `OwnershipTransfer` 이벤트를 감사 로그에 append(who: 운영자, what:
   agent_id·from A·to B, when). 이벤트는 `CorrectionEvent`·`AdmissionToken` revoke처럼
   append-only. **전이(3)와 기록(5)은 분리** — 3이 도메인, 5가 감사.

> ⚠️ 편집 채널 현실(§9.1): 현재 admission 채널은 라이브 mutation을 안 하고 **YAML→git/PR**로
> 반영한다. 그래서 실제 "스위치(3)"는 두 형태 중 하나다 — (i) **PR 경유**(기존 결·안전,
> 단 즉시성 낮음) 또는 (ii) **라이브 mutation 허용**(즉시·UI 친화, 단 Registry가 파일 SSOT를
> 벗어남). 이건 되돌리기 어려운 결정이라 §9.6 ADR 후보로 올린다.

### 9.4 정정 권한 판정 해법 (전이 후 새 owner가 과거 답을 정정)

**문제**: `submit_correction`이 `by_owner != record.answered_by`로 거부(`answer_record.py:207`).
오너 변경 후 새 owner B가 구 owner A가 낸 과거 답(record.answered_by=A)을 정정하려 하면 거부됨.

**해법(합의된 방향 채택)**: 정정 권한 판정을 **"현재 카드 owner" 기준**으로 바꾼다.
- 과거 기록 필드(`record.answered_by`)는 **불변 유지**(전이≠기록) — 누가 원래 답했나의 사실.
- 판정만 `record.answered_by == by_owner` → **`registry.get(record.agent_id).owner == by_owner`**
  (현재 그 카드의 owner인가)로 교체. record는 `agent_id`도 갖고 있어(`answer_record.py:68`)
  현재 카드 owner를 되짚을 수 있다.
- `CorrectionEvent.by_owner`는 **정정을 실제로 낸 owner(B)** 그대로 기록 — 정정 이력의 진실.
- 멱등 event_id는 `(record_id, by_owner, corrected_text)` 해시(`answer_record.py:152`)라
  by_owner가 B로 바뀌어도 자연히 새 이벤트(구 A 정정과 구별).
- **하위호환**: agent_id 카드가 Registry에서 사라진 경우(카드 폐기)엔 판정 원천이 없으므로,
  fallback으로 `answered_by` 동등 검사를 유지하거나 "미존재 카드 정정 불가"로 명시. 구현자가
  정한다(둘 다 불변식 안전).

### 9.5 신규 카드 등록 UI의 admission 경유 경계

- **원칙**: UI 전용 우회 등록 API를 **만들지 않는다**. UI는 기존 admission 채널
  (`validate_card_for_builder` 정신의 카드 제출 경로)을 그대로 호출한다. UI는 폼→카드 후보
  DTO 변환·표시만 담당하는 **얇은 어댑터**.
- **불변식 보존**: 모든 신규 카드는 `AgentCard.model_validate`(형식) + 참조 무결성(owner·
  maintainer 실재)을 통과해야 한다. UI가 이 두 관문을 건너뛰는 경로를 열면 "유효하지 않은
  카드는 등록되지 않는다" 불변식이 깨진다 — **금지**.
- **Authority 중앙 유지**: 폼에서 권한류 필드(`can_answer` 등)를 받아도 그건 카드 under-claim
  자기보고일 뿐(ADR 0004), Authority SSOT는 중앙 파일. UI가 권한을 새로 "선언"하지 않는다.

### 9.6 권한 경계 (누가 관리 UI를 쓰나)

- **현 단계(무비밀번호 데모·OIDC 코어 보류)**: 관리 UI는 **운영자 면**이다. 현재 운영 면
  진입은 `_session_identity`(`web.py:180`)로 세션 신원(User.id)을 요구한다 — 무비밀번호
  `/login`으로 신원을 *선택*해 세션 고정(per-request 가장 차단·ADR 0016). **역할(root/운영자)
  구분은 아직 없다** — 로그인된 신원이면 운영 면 진입.
- **현실적 경계 결정**: 카드 등록·오너 변경은 파괴적이므로 최소 두 겹 중 택1을 권고:
  (a) **로컬 바인드 관례**(중앙 운영 면을 127.0.0.1 바인드 — owner_web가 쓰는 관례,
  `owner_web.py:14`) + 로그인 세션, 또는 (b) **root/운영자 역할 게이트**(세션 신원이 지정된
  운영자 집합에 속할 때만 등록·전이 허용). 데모 단계는 (a)+로그인으로 충분, **실 SSO 연동
  시 (b)로 강화**.
- **실 SSO 강화 지점(명시)**: T7.x OIDC 활성 시 `resolve_identity`가 IdP 증명 신원을 세션에
  박는다(`web.py:167`). 그 위에 **운영자 role 클레임 검사**를 등록·오너 변경 엔드포인트에
  얹는다 — 이게 (b)의 실 구현 지점.

### 9.7 되돌리기 어려운 결정 → ADR 후보

> **✅ ADR 작성 완료 (2026-07-04·domain-architect)** — [`docs/adr/0034-admin-ui-live-registry-and-ownership-transfer.md`](adr/0034-admin-ui-live-registry-and-ownership-transfer.md).
> 사용자 확정: **라이브 즉시 반영**(YAML=시드 강등·감사 로그가 git 추적 대신·"YAML 동기 기록"·"UI는 제안만" 기각). 아래 3결정 전부 ADR 0034에 확정 기록됨.

1. **라이브 mutation vs PR 경유 스위치**(§9.3 주석) — Registry SSOT가 파일이냐 라이브냐를
   가른다. **→ ADR 0034 결정 1: 라이브 즉시 반영**(admission 통과 즉시 라이브 mutation + 감사
   로그 + `AON_DB` 영속·YAML은 초기 시드·ADR 0018 재정의).
2. **토큰 revoke 시점의 원자성 계약**(§9.2·§9.3-4) — 보안 불변식(owner 격리)에 직결. **→ ADR
   0034 결정 2: "스위치와 revoke는 같은 임계 구역"** 계약 명시(ADR 0026 재사용).
3. **정정 권한 판정 기준 변경**(§9.4) — `answered_by` 동등 → 현재 카드 owner. **→ ADR 0034
   결정 3**(과거 기록 필드 불변·`CorrectionEvent.by_owner`에 실제 정정자·불변식 문구 재해석·
   ADR 0033 재정의).

### 9.8 CONTEXT.md 용어 후보 (제안만 — 확정은 확정 시점에)

- **Ownership Transfer (오너 변경 / 소유권 이전)** — 카드 owner를 A→B로 바꾸는 **전이**.
  agent_id 불변·재-admission·구 세션/토큰 무효화·감사 기록을 함의. _후보 Avoid_: "Owner
  수정/Edit"(필드 mutation 함의 — frozen 값 교체가 진실), "재할당/Reassign"(사람 배치 뉘앙스).
- **Re-admission (재-admission / 재검증 등록)** — 갱신 카드가 기존 admission 관문을 다시
  통과하는 것. 신규 등록과 같은 관문. _후보 Avoid_: "재등록"(중복 register 오해 — 실제론 교체).
- **OwnershipTransfer (감사 이벤트)** — 전이 사실의 append-only 기록(who/what/when).
  `CorrectionEvent` 정신. _후보 Avoid_: 전이 자체와 이름 충돌 주의 — 감사 이벤트임을 문맥 표기.
- _Avoid(전역)_: 맨 단어 "Agent" 금지(Owner/AgentCard/AgentRuntime).

### 9.9 구현 시 반영할 SSOT 갱신 목록 (3라운드 구현자용)

> 다른 문서는 지금 다른 에이전트가 편집 중 — 여기 적어두면 구현자가 반영한다.

- **CONTEXT.md**: §9.8 용어(Ownership Transfer·Re-admission·OwnershipTransfer 이벤트) 확정 등재.
- **docs/adr/**: §9.7의 세 결정 중 확정분을 ADR로(특히 #1 라이브 vs PR, #2 토큰 revoke 원자성).
- **docs/tasks-v0.md**: 관리 UI 슬라이스(신규 등록 어댑터·오너 변경 전이·토큰 revoke 배선·
  정정 판정 교체) 태스크 추가.
- **docs/trd-v0.md**: 오너 변경 전이 순서(§9.3)·정정 판정 기준 변경(§9.4)·권한 경계(§9.6) 반영.
- **핵심 불변식 문구**: "자기 에이전트 답만 정정"을 "현재 카드 owner만 정정(과거 answered_by는
  불변)"으로 정정(§9.4). "owner 격리"에 "오너 변경 시 구 토큰 revoke" 명문화(§9.2).
  이번 전환에서 유지 불가할 수 있음(정직하게 다룰 것·domain-architect 몫).

---

## 10. 답변 피드백 — 질문자 좋음/싫음 → 담당자 표출 (도메인 설계, domain-architect)

> **✅ 코어+웹 구현 완료(tdd-engineer·2026-07-05)** — `AnswerFeedback`/`FeedbackVerdict`/
> `FeedbackStore`+`InMemoryFeedbackStore`(answer_record.py)·`monitoring_for_owner` 두 축
> OR 조인(`feedback_store=None` 하위호환)·`POST /answer/{record_id}/feedback`(web.py)·
> `serialize_monitoring_item` 피드백 블록·`owner-monitor.html`/`index.html` UI. 게이트
> `pytest` 2468 passed(2443+25)·`pyright` 0 errors·`ruff` clean. 아래 §10.1~§10.8은 이
> 구현이 그대로 따른 설계 shape다(그대로 유지 — 구현 기록은 `docs/tasks-v0.md` Phase 12
> 절 참조). **잔여(다음 라운드·mcp-runtime-engineer)**: §10.4 MCP `record_id` 노출+
> `submit_feedback` 도구·`SqliteFeedbackStore`(durable)·실 푸시 통지.

질문자가 받은 답에 좋음/싫음을 남기고, "싫음"이 담당자(owner) 감독 면의 "검토 필요"
축으로 표출돼 **정정(Correction)의 트리거**가 되게 한다. 새 정정 경로·새 통지 채널을
만들지 않는다 — 기존 감독 루프(`monitoring_for_owner` + `CorrectionService`)를 재사용한다.

### 10.1 실확인한 현재 지형 (근거)

- **질문자 신원 수준(정책의 뿌리)**: `/ask`·`/ask/stream`은 로그인 없이 익명 쿠키
  (`_COOKIE_NAME`)로 `uid`를 발급/재사용하고 `User(id=uid)`로 처리한다(web.py:1363-1373).
  이 `uid`가 `AnswerRecord.session_id`에 실린다(ask_org.py `_record_answer`). 즉 질문자는
  **쿠키 기반 약한 지속 신원**만 갖는다(운영 세션 `_SESSION_USER_KEY`와 별개 — 그건 owner/
  manager 로그인용). MCP 질문자는 `mcp_guest` 고정(mcp_server.py:33) — **개별 신원 없음**.
- **record_id 노출 현황**: 웹은 `Answered.record_id`를 `project_answered`가 실어 준다
  (ask_org.py:113·질문자가 정정 배지 조회에 쓰는 불투명 손잡이). **MCP는 `reply_to_mcp_text`가
  텍스트만 투영해 record_id를 노출하지 않는다**(mcp_server.py:54-59) — 피드백을 MCP에서 걸려면
  이 손잡이를 텍스트에 노출해야 한다(10.4).
- **감독 조인 지점**: `needs_correction_review`는 `AnswerRecord`의 **frozen 필드**라 답 발신
  후 변경 불가(answer_record.py:78). 피드백은 발신 *이후* 도착하므로 레코드에 되쓸 수 없다 —
  **별도 FeedbackStore를 `monitoring_for_owner`가 조인**하는 형태가 유일하게 자연스럽다(10.3).
- **재사용할 감독 루프**: `monitoring_for_owner`(agent_id 스코핑·검토 필요 필터·정정 이력 투영,
  answer_record.py:310)와 `MonitoringItem`(record + corrections, `needs_correction_review`
  프로퍼티). 웹은 `/supervision/answers?needs_review=` 필터로 소비(web.py:1448-1460).

### 10.2 AnswerFeedback 값 객체 + FeedbackStore 포트 (shape)

`AnswerRecord`/`CorrectionEvent`와 같은 정신 — frozen 값 객체 + Protocol 포트 + InMemory 구현.

```python
FeedbackVerdict = Literal["good", "bad"]  # sealed enum(문자열 2값·pydantic 검증)

class AnswerFeedback(BaseModel, frozen=True):
    """질문자가 한 답(record_id)에 남긴 좋음/싫음 — append-only(전이 ≠ 기록의 "기록").

    원 AnswerRecord를 절대 수정하지 않는다. record_id는 어느 답에 대한 피드백인가의
    *참조*일 뿐. submitted_by는 질문자 약한 신원(쿠키 uid 또는 mcp_guest) — 멱등 키.
    """
    record_id: str
    verdict: FeedbackVerdict
    comment: str = ""            # 선택 — "싫음" 사유(담당자가 정정에 참고). good도 허용.
    submitted_by: str            # 질문자 세션 uid(AnswerRecord.session_id와 같은 결) 또는 mcp_guest
    submitted_at: datetime

class FeedbackStore(Protocol):
    def upsert(self, fb: AnswerFeedback) -> None: ...          # 멱등(10.2 정책)
    def latest_for_record(self, record_id: str) -> AnswerFeedback | None: ...
    def for_record(self, record_id: str) -> list[AnswerFeedback]: ...  # 감사·이력용(전체 보존)
```

**멱등 · 중복 정책 — "최신 우선(upsert), 단 이력은 보존"**:
- 키는 `(record_id, submitted_by)`. 같은 질문자가 같은 답에 재제출하면 *최신 verdict/comment로
  덮되*, `for_record`는 append 이력 전체를 돌려준다(전이 ≠ 기록 — 판정은 최신, 기록은 전량).
- **근거(왜 1회 제한이 아니라 최신 우선인가)**: 질문자 신원이 쿠키 약신원이라 "1인 1표" 강제 자체가
  구조적으로 불가(쿠키 삭제 시 새 uid). 그래서 강한 중복 차단 대신, *같은 세션 내 마음 바꿈*을
  자연스럽게 흡수하는 upsert가 현실적이다("싫음 눌렀다가 좋음으로 정정"이 흔한 UX). 서로 다른
  uid의 피드백은 각각 별 행으로 쌓인다(집계 시 record별 여러 건 가능 — bad 존재 판정엔 OR면 충분).
- **event_id 결정론**: `CorrectionEvent`처럼 `(record_id, submitted_by)` 해시로 안정 id 도출
  가능(같은 질문자 재제출이 새 행을 만들지 않게) — 단 verdict/comment는 값이므로 id에 미포함
  (덮어쓰기가 목적). 이력 보존이 필요하면 InMemory가 upsert 시 직전 값을 history 리스트에 push.

**부수 결정 — verdict를 `good | bad` 2값 sealed enum으로 (Rating/점수 아님)**: 5점 척도·이모지·
자유 리액션은 지금 필요 없다(과설계). "싫음→정정 트리거"라는 단일 목적엔 이진 verdict면 충분하고,
"bad 존재"라는 감독 필터 판정이 명료해진다. 확장(척도)이 진짜 필요해지면 그때 값을 넓힌다.

### 10.3 "싫음" → 담당자 표출 경로 (monitoring 조인 — frozen 우회)

`needs_correction_review`가 frozen이라 되쓸 수 없으므로, **`monitoring_for_owner`가 FeedbackStore를
조인**해 "검토 필요" 판정을 *두 축의 OR*로 확장한다:

```python
@dataclass(frozen=True)
class MonitoringItem:
    record: AnswerRecord
    corrections: list[CorrectionEvent]
    feedback: AnswerFeedback | None = None       # 그 답의 최신 피드백(조인 결과·없으면 None)

    @property
    def needs_correction_review(self) -> bool:
        # 레코드 자체 표식(오프라인 자동발신 사후교정) OR 질문자 "싫음" 피드백.
        return self.record.needs_correction_review or self._has_bad_feedback

    @property
    def _has_bad_feedback(self) -> bool:
        return self.feedback is not None and self.feedback.verdict == "bad"

def monitoring_for_owner(
    answer_store, correction_store, *, agent_id, feedback_store=None,
) -> list[MonitoringItem]: ...
```

- **하위호환(핵심)**: `feedback_store=None`이면 `feedback=None`·기존 판정 100% 보존(현 배선·현
  테스트 무변경). 주입 시에만 bad 피드백이 검토 필요 축에 합류한다 — `notifier`·`presence_of`
  옵셔널 주입과 동형 패턴.
- **조인 위치는 코어(함수 내부)**: 웹/MCP 어댑터가 조인하면 두 표면이 판정을 다르게 흘릴 여지가
  생긴다. `monitoring_for_owner`가 record별로 `feedback_store.latest_for_record`를 당겨 조인하면
  **판정 SSOT가 코어 한 곳**(serialize 어댑터는 투영만). 웹 `/supervision/answers?needs_review=`
  필터는 그대로 `it.needs_correction_review`를 보므로 배선만 바뀌고 필터 로직 무변경.
- **MonitoringItem 노출 형태**: `serialize_monitoring_item`에 `feedback` 블록 추가 —
  `{verdict, comment, submitted_at}`. `submitted_by`(질문자 uid)는 owner 감독 면이라 노출해도
  leak 아님(감독 면은 내부값 노출이 원래 계약, web.py:342 주석). 단 채팅/질문자 표면엔 절대 안 실림.

### 10.4 MCP 표면 (record_id 노출 + submit_feedback 도구)

**선결 — `ask_org` 응답에 record_id 노출**: 현재 `reply_to_mcp_text`는 텍스트만 줘서 질문자가
피드백을 걸 손잡이가 없다. Answered 투영에 record_id 라인을 덧붙인다(불투명 uuid hex라 내부 구조
미인코딩 — `tracking` 토큰과 같은 결·leak 아님):

```
{답 본문}

담당: {owner}/{agent_id} · 신뢰: {mode} · 출처: {sources}
피드백 참조: {record_id}      # ← 추가(record_id is not None일 때만)
```

**새 도구 `submit_feedback`**:
```python
@mcp.tool(name="submit_feedback", description="받은 답에 좋음/싫음 피드백을 남깁니다. '싫음'은 담당자에게 전달돼 정정 기회가 됩니다.")
def submit_feedback(record_id: str, verdict: Literal["good", "bad"], comment: str = "") -> str:
    # user_id는 서버 설정값(mcp_guest) — submitted_by로 실린다(질문자 자기보고 금지·ADR 0009 정신).
    # 미존재 record_id면 거부 메시지(존재 검증은 서비스/스토어). 성공 시 접수 확인 텍스트.
```
- `verdict`는 `Literal["good","bad"]`이라 잘못된 값은 MCP 스키마 단에서 거부(FeedbackVerdict와 동일 SSOT).
- `submitted_by`는 도구 파라미터가 아니라 서버 설정 `user_id`(=mcp_guest) — `ask_org`가 신원을
  파라미터로 안 받는 것과 같은 규율(누구도 남을 가장 못 함). MCP 질문자는 전원 mcp_guest라 멱등
  키가 공유됨(같은 서버의 여러 MCP 질문자는 record별 최신 하나로 수렴) — v0 약신원 한계로 수용,
  실 인증(T6.5) 시 실 주체로 대체.

### 10.5 웹 표면 (POST /answer/{record_id}/feedback)

질문자 측 라우트 — `/answer/{record_id}/correction`(GET 정정 배지 조회)의 형제. **세션 신원 불요**
(질문자는 운영 로그인 안 함) — `submitted_by`는 `/ask`가 발급한 익명 쿠키(`_COOKIE_NAME`)에서 읽는다.

```python
class FeedbackRequest(BaseModel):
    verdict: Literal["good", "bad"]
    comment: str = ""

@app.post("/answer/{record_id}/feedback")
def answer_feedback(record_id: str, req: FeedbackRequest, request: Request) -> dict:
    # 검증: 존재하는 record인가(answer_store.get → 없으면 404). verdict enum은 pydantic이 422로 거부.
    # submitted_by: request.cookies[_COOKIE_NAME](없으면 새로 발급 — /ask와 동형) 또는 익명 폴백.
    # FeedbackStore.upsert(멱등). 응답: {"submitted": True, "record_id": ..., "verdict": ...}.
    # 미배선(feedback_store 미주입)이면 503(정정 서비스 미배선과 동형).
```
- **응답 노출 불변식**: 접수 확인(`submitted`·`record_id`·`verdict`)만. bad 피드백이 어느 owner/
  agent로 갔는지·내부 판정은 응답에 절대 싣지 않는다(질문자 표면). owner 표출은 별 면(supervision).
- **미아·흐름 무차단**: 피드백 제출은 답변 흐름과 완전 분리된 사후 액션 — 실패(미배선/404)해도 원
  답변·정정 배지 조회는 영향 없음. feedback_store 미주입 시 나머지 전부 정상 동작(옵셔널 배선).

### 10.6 불변식 자체 점검

- **전이 ≠ 기록**: `AnswerFeedback`는 append-only(upsert는 최신 판정 갱신, `for_record`가 이력 전량
  보존). 원 `AnswerRecord`·`CorrectionEvent` 불변 — 피드백은 참조(record_id)만 든다. ✅
- **미아 없음**: 피드백은 답변 라우팅·발신을 막지 않는 사후 액션. feedback_store 미주입/제출 실패가
  질문 흐름·정정 배지를 깨지 않는다(옵셔널 배선·흐름 분리). ✅
- **Authority 중앙**: 피드백은 Authority(누가 담당인가)를 건드리지 않는다 — 담당 판정은 여전히
  `routing_rules.yaml`. 피드백은 "이 답이 별로였다"는 질문자 신호일 뿐, 담당을 재선언하지 않는다. ✅
- **노출 불변식**: 질문자 표면(웹 POST 응답·MCP 도구 텍스트)은 접수 확인만 — bad가 어느 owner에
  갔는지·감독 내부값 미노출. 반대로 owner 감독 면(`serialize_monitoring_item`)은 피드백 상세를
  노출(원래 감독 면은 내부값 노출이 계약). 두 표면의 방향이 반대임을 코드로 강제(질문자↔owner). ✅
- **정정 트리거일 뿐, 정정 아님**: 피드백은 `needs_correction_review` 축을 켜는 *신호*지 정정
  자체가 아니다. 실제 정정은 owner가 `submit_correction`을 눌러야 발생(자동 정정 없음) — 기존
  정정 상태기계·owner 스코핑(현재 카드 owner만) 100% 재사용. ✅

### 10.7 CONTEXT.md 용어 후보 (제안만 — 확정은 domain-architect 확정 시점에)

- **Answer Feedback (답변 피드백)** — 질문자가 받은 답(AnswerRecord)에 남기는 좋음/싫음 신호.
  append-only·정정의 *트리거*(정정 자체 아님). _Avoid_: "Rating/평점"(척도 함의 — 이진임),
  "Like/Dislike·좋아요/싫어요"(SNS 반응 뉘앙스), "Reaction/리액션"(감정 표출로 목적 흐림),
  "Review/리뷰"(BackupReview와 이름 충돌).
- **Feedback Verdict (피드백 판정: good | bad)** — `AnswerFeedback`의 이진 값. `RoutingDecision`·
  `AnswerMode`처럼 sealed 값 축. _Avoid_: "Score/Vote"(집계 함의).
- **bad Feedback → Needs Review (싫음 → 검토 필요)** — bad 피드백이 감독 "검토 필요" 축에
  합류하는 조인 규칙(레코드 표식 OR bad 피드백). 새 상태 아님 — 기존 축의 원천 하나 추가.

### 10.8 구현 시 반영할 SSOT 갱신 목록 (구현자용)

> 다른 문서·코드는 지금 다른 에이전트가 편집 중 — 여기 적어두면 구현자가 반영한다. 이 절은
> 설계(shape)만이고, red→green 구현은 tdd-engineer, MCP 도구 배선은 mcp-runtime-engineer 몫.

- **CONTEXT.md**: §10.7 용어(Answer Feedback·Feedback Verdict·bad→Needs Review) 확정 등재.
  "Agent" 단독 금지·정정과의 관계(트리거) 명시.
- **docs/adr/**: 되돌리기 어려운 결정 후보 — (a) 피드백 멱등 정책(최신 우선 upsert·1회 제한 아님·
  약신원 근거), (b) MCP `ask_org` 응답에 record_id 노출(표면 변경). 경미하면 plan §10 참조로 갈음 가능.
- **docs/tasks-v0.md**: 피드백 슬라이스 태스크 — AnswerFeedback+FeedbackStore(코어)·monitoring 조인
  확장·웹 POST 라우트·MCP record_id 노출+submit_feedback 도구·serialize 투영. TDD 순서로 분해.
- **docs/trd-v0.md**: 감독 "검토 필요" 판정이 *두 축 OR*(레코드 표식 OR bad 피드백)로 확장됨 반영.
  `/answer/{record_id}/feedback`·MCP `submit_feedback` 표면 기술.
- **docs/prd-v0.md**: 질문자 답변 피드백 UX(좋음/싫음·선택 코멘트·웹+MCP) 요구 반영. "싫음→담당자
  정정 기회" 흐름 명문화.
- **외부 결정 연결**: owner 도달은 현 단계 **풀 방식**(supervision 검토 필요 축) — 실 push는 외부
  결정 ④(§5 D, 질문자↔owner 도달 채널)의 잔여로 실 사용자 단계 유지(새 통지 채널 미신설). plan §5
  D를 "정정 통지 + 피드백 표출 도달 채널"로 포괄 표기하는 게 정합적(구현자가 문구 조정).
