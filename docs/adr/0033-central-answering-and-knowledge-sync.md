# 중앙 답변 + 지식 동기화 실행 모델 — 답 실행을 중앙으로 재부활시키고 워커를 "지식 공급자"로 전환한다(0027 결정 2·4 재정의·0028/0030 "중앙 본문 0" 재정의·"중앙 토큰 0" 정직한 폐기)

상태: accepted (2026-07-04) · **Phase 12의 실행 모델 전환 ADR** · **ADR 0027 결정 2·4 재정의**(대화 답변 실행을 owner OAuth 인프로세스 → *중앙 런타임*으로 되돌림 — 헤더 포인터, 결정 1) · **ADR 0028/0030 "중앙은 목차만·본문 0" 부분 재정의**(명시 지정 지식은 *본문을 중앙에 담는다* — 라우팅 인덱스는 여전히 목차만, 결정 3) · **ADR 0017 근거 부활**("owner PC는 잠든다" 가용성 근거가 되살아남 — 계보 정리) · **ADR 0011·0012 WS 전송 재포지셔닝**(0027이 "기본 대화 경로"로 1급화한 것을 *지식 동기화 채널*로 재사용 — 답 운반은 아님) · **ADR 0012 `BackupReview` 일반화**(백업 답 검토 → *모든 자동 발신 답* 사후 교정, 결정 4) · **ADR 0019 reeval·ADR 0022 통지 재사용**(정정 이벤트→판례/지식 갱신·질문자 통지 — recipient 확장) · **ADR 0025 HITL 토글 입력 확장**(토글 입력에 프레즌스 결합·단조성 보존, 결정 5) · CONTEXT(신규 Knowledge Store·Knowledge Sync·Presence·Answer Record·Correction Event·Supervised Answering·Knowledge Provider 용어·Agent Runtime·Provider Runtime 절 재정의)·PRD §3·§5·§6·TRD §2·§4·§10 갱신 대상 · 출처: 상세 계획 [`docs/plan-central-answering.md`](../plan-central-answering.md)(planner·2026-07-04)

## 맥락 — 실행 위치를 *네 번째로* 다시 뒤집는다

이 ADR은 되돌리기 어려운 결정(답 실행 위치)을 *다시 뒤집는다*. 그리고 이번엔 **되돌리기 어려운 불변식 하나("중앙 토큰 0")를 정직하게 폐기**한다. 정직하게 다뤄야 하므로 계보부터 적는다 — 이건 *완전 번복*이 아니라 *부분 재정의 + 과거 조각의 재조합*이다.

### 실행 위치 계보 — 0010 → 0017 → 0027 → 이번

| 시기 | 답 실행 위치 | 핵심 근거 |
|---|---|---|
| **ADR 0010** (2026-06-20) | 각 owner PC의 분산 `claude -p` | "중앙 API 키 LLM RAG 회피"(로컬 인증·키/비용 회피) |
| **ADR 0017** (2026-06-21) | **중앙** `claude -p`(owner OKF 커밋 스냅샷 cwd 읽기) | "관리 ≠ 실행" — owner PC는 잠든다(가용성)·신선도·UX |
| **ADR 0027** (2026-06-26) | **owner 워커**(owner OAuth 멀티-LLM 인프로세스 스트리밍) | 제품 비전(Hermes/opencode)·속도·멀티 공급자는 owner 자격증명 필요·**중앙 토큰 0 강화** |
| **이번 (0033)** (2026-07-04) | **중앙 런타임**(지속 동기화된 *중앙 지식 저장소* 소비) | 가용성(owner PC 부재해도 답)·격리 백업 인스턴스 호스팅 복잡도 회피 |

### 각 근거의 부활·이동 — 정직한 정리

- **0017이 옳았던 근거가 부활한다.** 0017의 핵심 반대는 "owner PC는 잠든다(가용성)"였다. 0027은 이 반대를 *백업 워커(0012)·Manager escalation(0014)·HITL 토글(0025)*로 흡수한다고 논증했다. 사용자 판단은 그 흡수가 실제로는 무겁다는 것이다 — **격리 백업 워커라는 별도 인스턴스를 호스팅하는 것 자체가 복잡하고**, 더 단순한 해법이 "지식을 중앙에 계속 올려두고 중앙이 답"하는 것이다. 0017의 가용성 근거가 이번에 되살아난다. (단 0017로의 *단순 회귀는 아니다* — 아래.)

- **0017로의 단순 회귀가 아니다.** 0017은 "중앙 `claude -p`가 owner OKF 최신 *커밋 스냅샷을 cwd로 읽기*"였다. 이번은 owner git·커밋 스냅샷을 중앙이 *읽지 않는다* — owner 워커가 **지속 동기화한 중앙 지식 저장소**(`KnowledgeStore`)를 중앙 런타임이 소비한다. 저장소 모델이 다르다(cwd 스냅샷 읽기 → 동기화된 중앙 본체). 이건 0017의 "중앙이 owner 환경을 읽음"과도, 0030의 "중앙은 목차만"과도 다른 제3의 지점이다.

- **0027이 옳았던 근거는 어떻게 되나.**
  - **(a) "멀티-LLM OAuth = owner 자격증명 필요·중앙 토큰 0"** → 중앙이 답하면 자격증명이 중앙으로 갈 수밖에 없다. **"중앙 토큰 0" 불변식이 흔들린다 — 이게 이 전환의 가장 무거운, 되돌리기 어려운 트레이드오프다.** 아래 결정 2에서 *정직하게 폐기*한다.
  - **(b) "인프로세스 스트리밍 속도"** → 중앙도 인프로세스 스트리밍이 가능하다(런타임 위치만 이동·`AgentRuntime` 포트·`ProviderTransport` 무변경). 속도 근거는 위치와 무관하게 유지된다.
  - **(c) 제품 비전(멀티-LLM OAuth 전용 채팅)** → 답변 모델 선택의 자유는 유지된다. 다만 자격증명 주체가 owner(구독)에서 *중앙 조직 키*로 바뀐다(결정 2). 워커의 역할이 "답변 실행자"에서 "**지식 공급자**"로 이동한다.

### 워커 역할의 재정의 — "답변 실행자" → "지식 공급자"

0027에서 owner 워커는 owner OAuth로 답을 *만들어* 중앙에 회신했다. 이번엔 워커가 답을 *안 만들고* **자기 환경의 지식을 중앙 지식 저장소로 계속 자동 동기화**한다. 답은 중앙 런타임이 그 저장소를 소비해 만든다. WS 전송(0011·0012)은 답 운반이 아니라 *지식 동기화 채널*로 재사용된다(결정 3·아래).

---

## 결정

### 1. 답 실행 = 중앙 런타임이 중앙 지식 저장소를 소비 (ADR 0027 결정 2·4 재정의)

- **대화 답변 경로**의 실행을 owner 워커 인프로세스 → **중앙 런타임**으로 되돌린다. 라우팅 결과(`RoutingDecision`)를 소비하는 경로가 워커 dispatch에서 *중앙 인프로세스 소비*로 바뀐다.
- **`AgentRuntime` 포트는 무변경.** `answer(question, card, context) -> Answer`. 0010(중앙 claude -p)·0017(중앙 claude -p OKF 읽기)·0027(owner OAuth 인프로세스)·이번(중앙 지식 저장소 소비)은 *같은 포트의 다른 구현/위치*다. 라우팅·dispatcher·노출 경계가 안 흔들린다(헥사고날 — 코어는 포트만 본다).
- **소비 대상이 바뀐다** — 런타임은 이제 owner OKF cwd(0017)나 owner 워커 로컬 OKF(0027 결정 12)가 아니라 **`KnowledgeStore`(중앙 지식 저장소)에서 그 agent_id의 동기화된 본문**을 읽어 프롬프트에 접지한다. 순수 헬퍼 `read_okf_bundle`(0027 결정 12)의 *입력 원천*만 디스크→`KnowledgeStore`로 바뀌고 매핑 함수(`build_provider_request` 등)는 그대로다.
- **핵심 가치**: owner PC 가동 여부와 무관하게 답이 나온다(가용성). 라우팅 종착은 안 바뀐다(0매칭→Unowned/root·미아 없음 보존). 백업 워커·Manager escalation은 폴백으로 *잔존 가능*하되 기본 가용성은 중앙이 떠받친다.
- **분류기·배치 `claude -p`는 잔존**(0027 결정 4와 동일 — 대화 답변 경로만 이동).

### 2. "중앙 토큰 0" 불변식의 정직한 폐기 — 중앙 조직 API 키 1개 (외부 결정 ③·사용자 확정 2026-07-04)

**"중앙 토큰 0" 불변식(ADR 0010·0017·0027이 보존·강화해 온 것)을 폐기한다.** 은폐하지 않는다 — 중앙 답변이면 자격증명이 중앙으로 갈 수밖에 없다는 구조적 사실을 정면으로 받는다.

- **결정(사용자 2026-07-04)**: LLM 비용/토큰 귀속 = **중앙 조직 API 키 1개**. 중앙 런타임이 하나의 중앙 조직 API 키로 공급자 API를 부르고, 비용은 중앙 조직에 과금된다. 담당자별 비용 구분은 *태깅/로그*로 한다(키를 담당자별로 쪼개지 않는다 — MVP 단순).
- **선택지 비교(정직한 기록)**: ① 중앙 조직 키 1개(**채택** — 중앙 과금·운영 단순·owner 재설정 0) ② owner OAuth를 중앙이 위임 보관(자격 위임·보안 리스크·rotation 지옥 — 0027 결정 9 실측이 이미 "인프로세스 구독 OAuth는 viable하지 않다"고 닫음) ③ 하이브리드(온라인 owner 자격·오프라인 중앙 자격 — 두 경로 유지 복잡). 사용자는 ①을 택했다 — "중앙 토큰 0"이라는 순수성보다 *가용성과 운영 단순성*이 이 제품의 실제 가치다.
- **대체 안전장치(폐기의 반대급부로 반드시 둔다)**:
  - **키 보관** — 중앙 조직 API 키는 환경변수/시크릿 매니저에만 두고 코드·저장소·와이어에 싣지 않는다(`ANTHROPIC_API_KEY` 등 기존 env 해석 정신). 게이트 내는 `StubProviderTransport` 주입이라 실 키 0.
  - **로그에 키 미노출** — audit·트랜스크립트·통지·로그 어디에도 API 키 원문을 안 싣는다(노출 불변식의 확장 — `Answer`·`Notification`이 식별자만 담는 정신). 비용 태깅은 `agent_id`·`answered_by` 같은 *식별자*로 하지 키로 하지 않는다.
  - **비용 귀속 = 태깅** — 담당자별 비용은 답 생성 시 `agent_id` 태그를 로그/메트릭에 남겨 사후 집계한다(도메인 값 아님·운영 메타·`AnswerRecord`의 audit 축에 붙을 수 있음).
- **폐기가 흔들지 *않는* 것**: 이 폐기는 *LLM 자격증명 위치* 축만 바꾼다. 나머지 4대 불변식(미아 없음·무효 카드 금지·Authority 중앙·전이≠기록)은 그대로다(아래 §불변식). "중앙 토큰 0"은 4대 불변식이 *아니었다* — 0027의 강화된 부수 속성이었다. 4대는 유지된다.

### 3. "중앙은 목차만·본문 0"의 부분 재정의 — 명시 지정 지식은 본문을 중앙에 담는다 (ADR 0028/0030 재정의·외부 결정 ①②)

중앙이 *답하려면* 목차(0028 `KnowledgeIndex`·`core_question`)가 아니라 **지식 본문**이 필요하다. 0028/0030의 "중앙은 목차만·본문 0" 불변식을 **부분 재정의**한다 — 완전 폐기가 아니다.

- **두 축의 분리(핵심)**:
  - **라우팅 인덱스(0028 `KnowledgeIndex`) = 여전히 목차만·본문 0.** "어느 에이전트로 보낼지"는 `core_question` 목차 매칭이 그대로 한다(2단 라우팅·admission·중앙 로컬 임베딩). 이 축은 안 바뀐다.
  - **답변 지식(`KnowledgeStore`) = 본문을 담는다.** "그 에이전트가 어떻게 답하는지"는 중앙이 동기화된 *본문*을 읽어 접지한다. 이 축이 0028/0030의 "본문 0"을 재정의한다.
- **지식 경계 = 명시 지정만 동기화 (외부 결정 ①·사용자 확정)**: 담당자가 *명시 지정한* 디렉터리/파일/문서만 워커가 자동 동기화한다. owner 환경 전체를 올리지 않는다 — 지정 단위(`KnowledgeSyncSpec`)를 owner가 선언하고, 그 지정분만 admission을 거쳐 `KnowledgeStore`에 들어간다. 이는 owner 통제·비소유 정신의 잔존(중앙이 owner 전체를 빨아들이지 않음).
- **민감정보 이중 방어 (외부 결정 ②·사용자 확정)**:
  - **패턴 필터 admission(1차)** — 동기화 수용 시 주민번호·API 키·비밀번호류 패턴을 자동 거부한다(over-claim 필터가 권한을 검사하듯, 민감 패턴 필터가 본문을 검사·`SensitivityFilter` 순수 함수). 패턴에 걸린 본문은 `KnowledgeStore`에 안 들어간다.
  - **지정 책임(2차)** — 그 외는 "지정한 담당자 책임" 원칙. owner가 무엇을 올릴지 *명시 지정*했으므로(경계 ①) 지정 자체가 책임의 귀속이다. 패턴 필터는 실수 방어망이지 완전 보장이 아니다(정직한 한계).
- **동기화 주기·stale 임계 (외부 결정 ⑤·보수적 기본값 제안·설정값)**: 커밋=이벤트(0019)로 **즉시 반영**을 기본으로 하고, `last_synced_at` 신선도 신호를 `KnowledgeStore`에 둔다. stale 임계 기본값 **30분**(0024 유휴 타임아웃 선례·`AON_KNOWLEDGE_STALE_SECONDS` 설정값·추후 조정). 임계 초과분은 "낡음" 표식(답 신뢰 하향 신호일 뿐 라우팅 배제 아님 — 미아 없음 보존).

### 4. 정정 = 답변 레코드 수정이 아니라 새 이벤트 (전이 ≠ 기록 유지·ADR 0012 일반화·외부 결정 ④)

담당자가 자기 에이전트에 들어온 질문·나간 답을 열람(모니터링)하고, 잘못된 답을 고칠 수 있다. 이건 `BackupReview`(백업 답 검토·0012)의 **일반화**다 — 백업 답만이 아니라 *모든 자동 발신 답*이 검토·정정 대상이다.

- **정정 이벤트 모델(전이 ≠ 기록 유지)**: 정정은 원 `AnswerRecord`를 *수정하지 않는다*. **새 `CorrectionEvent`를 append**한다(감사 추적 보존·append-only). `BackupReview`의 `CorrectBackup`이 원 backup 답을 파괴하지 않고 새 인스턴스를 낳는 정신, `action_record`(정비 라운드) 정신 그대로. 원 답이 "무엇이 나갔나", 정정 이벤트가 "무엇으로 고쳤나"를 각각 산다.
- **정정 통지 = 답변 페이지 정정 표시(풀 방식) (외부 결정 ④·사용자 확정)**: 질문자가 받은 답변 페이지에 **정정 배지 + 정정본**을 표시한다. 질문자가 그 페이지를 다시 볼 때 정정을 본다(풀). 푸시/메신저 연동은 실 사용자 단계로 연기. 이는 0022 통지 인프라의 recipient 확장 자리이되, MVP는 *페이지 표시*로 닫는다(실 push 채널 게이트 밖).
  - **`Notification` recipient 확장** — 현재 통지는 owner/manager User.id 귀속만이다. 질문자 통지는 *세션 기반 표시*로 하고(익명 세션도 자기 답변 페이지에서 정정 배지를 봄), 실 push recipient 확장은 후속.
- **판례/지식 갱신 = reeval 재사용(0019)**: 정정은 그 답의 근거 지식·판례를 재평가 큐(`ReevalStore`)에 적재할 수 있다(`ReevalOutcome` — Keep/Invalidate/Supersede·Acknowledge/ReAnswer 재사용). 정정 이벤트→reeval은 기존 경로 재사용이지 새 기계가 아니다.
- **멱등** — 중복 정정 통지 방지(같은 답·같은 정정은 한 번만 표시·0022 멱등 정신).

### 5. HITL 정책 분기 = 프레즌스 결합 (ADR 0025 토글 입력 확장·단조성 보존·외부 결정 ⑥)

HITL 토글(0025)의 *입력에 프레즌스를 더한다* — 새 기계가 아니라 토글 입력 소스 확장이다.

- **프레즌스 1급 개념**: 담당자 워커 연결 상태(온라인/오프라인)를 1급 개념(`Presence`)으로 승격한다. 워커 WS 연결이 사실상 하트비트다(0011·0012 `WebSocketDispatcher._connections` 재사용). 콘솔 SSE(0024)가 이미 "워커 연결/해제" 피드를 예고했으므로 확장이지 충돌 아님.
- **정책 분기(에이전트별)**:
  - **온라인** = 사전 검토(pre-send review) — 기존 `draft_only`(owner 검토·수정·전송). 담당자가 온라인이니 답 나가기 전에 검토할 수 있다.
  - **오프라인** = 자동 발신 + 사후 교정 — `full`로 자동 발신하되 담당자 복귀 후 검토·정정(결정 4). 담당자가 없으니 답을 붙잡을 수 없어 자동 발신하고, 나중에 고친다.
- **단조성 보존(0025 정신 유지)**: 카드 `approval_when`이 건 담당은 *오프라인이라도* `draft_only`를 못 푼다(under-claim 단조성 — 풀기는 카드 정책에 막히고 조이기는 런타임/프레즌스로 가능). 즉 프레즌스가 mode를 *조일* 수는 있어도(온라인→검토) 카드가 조인 것을 *풀* 수는 없다.
- **순수 함수**: `presence_to_hitl(presence, ...) -> bool`(프레즌스→HITL 판정) + 기존 `resolve_mode`(OR 결합·backup 우선순위) 재사용. 프레즌스는 토글의 *입력*이지 토글 진실 자체가 아니다(HITL 토글 진실은 여전히 중앙·0025 결정 5).
- **오프라인 판정 기준(외부 결정 ⑥·보수적 기본값 제안·설정값)**: 연결 끊김 **즉시 오프라인**(grace period 없음·MVP 단순)·에이전트별 정책 기본값은 **카드 approval_when 시드 유지**(0025 결정 1 정신). 추후 grace period는 `AON_PRESENCE_GRACE_SECONDS` 설정값으로 조정 가능(기본 0).

---

## S1 도메인 shape (설계 — 구현 아님·tdd-engineer/mcp-runtime-engineer 넘김용 *모양*)

> 아래는 값 객체·포트의 *모양*이지 구현이 아니다. 전부 `SessionStore`·`ReevalStore`·`TokenStore`·`BackupReviewStore`의 **포트(Protocol) + InMemory/Fake + 주입 결정론** 패턴 N번째다(새 메커니즘 0). pydantic v2 frozen 값 객체·sealed sum은 `RoutingDecision`·`ReevalOutcome`·`BackupReview` 정신.

### `KnowledgeStore` — 중앙 지식 저장소(본문 보관)

동기화된 owner 지식 *본문*을 agent_id별로 보관하는 포트. `PublishedIndexStore`(0028)가 목차를 담듯 이건 본문을 담는다 — 두 store가 나란히(라우팅 축 vs 답변 축).

```
class KnowledgeBundleContent(BaseModel, frozen=True):   # 동기화된 본문 단위
    agent_id: str                    # Registry 카드와 admission 대조
    documents: tuple[KnowledgeDoc, ...]   # 명시 지정된 파일들(path·body)
    version: str                     # staleness 판정(KnowledgeIndex.version 정신)
    synced_at: datetime              # last_synced_at 신선도 신호

class KnowledgeDoc(BaseModel, frozen=True):
    path: str                        # 지정 경로(agent_id 상대)
    body: str                        # 본문(admission·민감 필터 통과분)

class KnowledgeStore(Protocol):      # SessionStore·ReevalStore 정신
    def put(self, content: KnowledgeBundleContent) -> None: ...   # 더 새 version만 수용
    def get(self, agent_id: str) -> KnowledgeBundleContent | None: ...
    def is_stale(self, agent_id: str, *, now: datetime, threshold_s: int) -> bool: ...
# InMemoryKnowledgeStore(결정론)·실 크로스머신 동기 back-end는 게이트 밖
```

- **불변식**: 순수 보관(전이 아님)·더 새 version만 수용(0028 staleness 정신)·`agent_id` 미등록이면 거부(등록 무결성).

### `KnowledgeSync` admission — 명시 지정 + 민감 필터

워커→중앙 본문 동기화의 수용 관문. 0028 over-claim 필터(권한 대조)에 *민감 패턴 필터*를 더한다.

```
class KnowledgeSyncSpec(BaseModel, frozen=True):   # owner가 명시 지정하는 동기화 경계
    agent_id: str
    paths: tuple[str, ...]           # 지정 디렉터리/파일(이것만 동기화)

def filter_sensitive(body: str) -> SensitivityVerdict: ...   # 순수 — 주민번호·API키·비밀번호 패턴
def admit_knowledge(
    content: KnowledgeBundleContent, card: AgentCard, spec: KnowledgeSyncSpec
) -> AdmissionResult: ...             # 순수 — 지정 경계 + 민감 필터 + (권한 대조 재사용)
```

- **sealed sum 후보**: `AdmissionResult = Admitted | Rejected`(사유 담음 — `RoutingDecision` 정신). `SensitivityVerdict = Clean | Blocked(patterns)`.
- **불변식**: Authority 중앙(동기화가 권한 자기보고로 못 넓힘·0028 재사용)·등록 무결성(유효하지 않은/민감 본문 미반영).

### `Presence` — 담당자 연결 상태(1급)

```
PresenceStatus = Literal["online", "offline"]     # 단조 아님·연결에서 도출

class Presence(BaseModel, frozen=True):
    agent_id: str
    status: PresenceStatus
    since: datetime                  # 이 상태로 들어온 시각(주입 clock 결정론)

class PresenceTracker(Protocol):     # HitlToggleMap 정신(in-memory 상태 그릇)
    def observe_connect(self, agent_id: str, *, at: datetime) -> None: ...
    def observe_disconnect(self, agent_id: str, *, at: datetime) -> None: ...
    def status(self, agent_id: str) -> PresenceStatus: ...   # 미관측=offline 기본
```

- `Presence`는 WS 연결(`WebSocketDispatcher._connections`)에서 도출한다(실 연결→프레즌스는 게이트 밖). HITL 순수 함수 `presence_to_hitl(status) -> bool`의 입력.

### `AnswerRecord` / `CorrectionEvent` — 답변 감사 단위 + 정정 이벤트(전이 ≠ 기록)

```
class AnswerRecord(BaseModel, frozen=True):    # 중앙이 낸 답의 감사 단위
    record_id: str
    question: str
    answer_text: str
    answered_by: str                 # agent_id/owner 귀속
    agent_id: str
    mode: AnswerMode                 # full/draft_only/backup(재사용)
    session_id: str | None           # 질문자 세션(정정 페이지 표시 귀속)
    answered_at: datetime

class CorrectionEvent(BaseModel, frozen=True):   # 원 레코드 수정 없이 append
    event_id: str
    record_id: str                   # 어느 답을 정정하나(원 레코드 불변 — 참조만)
    corrected_text: str
    by_owner: str                    # 정정 주체(BackupReview by_owner 정신)
    rationale: str = ""
    corrected_at: datetime

class AnswerRecordStore(Protocol):   # BackupReviewStore 정신
    def add(self, rec: AnswerRecord) -> None: ...
    def get(self, record_id: str) -> AnswerRecord | None: ...
    def for_agent(self, agent_id: str) -> list[AnswerRecord]: ...   # 담당자 모니터링

class CorrectionStore(Protocol):
    def append(self, event: CorrectionEvent) -> None: ...           # append-only
    def for_record(self, record_id: str) -> list[CorrectionEvent]: ...
```

- **불변식(핵심)**: **전이 ≠ 기록** — 정정은 `AnswerRecord`를 *수정하지 않고* `CorrectionEvent`를 append. `for_record`가 원 답 + 정정 이력을 함께 투영해 답변 페이지 정정 배지를 만든다(풀 방식). reeval 적재는 별 축(0019 재사용).

### `HitlPolicy` — 에이전트별 HITL 정책 + 프레즌스 결합

새 타입을 최소화한다 — 기존 `HitlToggleMap`(0025)·`resolve_mode`·`seed_from_card`를 재사용하고, 프레즌스 입력만 더한다.

```
def presence_to_hitl(status: PresenceStatus) -> bool:   # 순수 — online→True(검토)·offline→False(자동)
    return status == "online"

# 최종 mode = resolve_mode(
#     requires_approval=card.approval_when 유래,   # under-claim 단조성(못 풂)
#     hitl_on=HitlToggleMap.is_on(agent_id) OR presence_to_hitl(presence),  # 입력 확장
#     current_mode=...,
# )
```

- **불변식**: 노출 불변식(mode는 노출값)·Authority 중앙(토글·프레즌스는 신뢰 게이트지 권한 아님)·under-claim 단조성 보존(카드가 조인 건 프레즌스로 못 풂).

### 기존 자산 재사용 요약

| 신규 | 재사용 패턴 | 근거 |
|---|---|---|
| `KnowledgeStore` | `PublishedIndexStore`·`SessionStore`(Protocol+InMemory·더 새 version 수용) | 목차 축 옆 본문 축 |
| `KnowledgeSync` admission | 0028 over-claim 필터 + `AgentCard` admission | Authority 중앙 재사용 |
| `Presence` | `HitlToggleMap`(in-memory 상태 그릇)·WS `_connections` | 연결→상태 도출 |
| `AnswerRecord`/`CorrectionEvent` | `BackupReviewItem`/`BackupReview`(불변+새 인스턴스)·`action_record`·audit | 전이≠기록 |
| `HitlPolicy` | `HitlToggleMap`·`resolve_mode`·`seed_from_card`(0025) | 새 기계 0·입력 확장 |
| 정정 통지 | `Notifier`·`Notification`(0022·멱등)·`ReevalStore`(0019) | recipient 확장 자리 |

---

## 근거

- **포트 무변경·위치 이동** — `AgentRuntime`은 0007부터 포트였다. 실행 위치 네 번의 이동이 전부 *같은 포트의 다른 구현*이라 라우팅·노출 경계가 안 흔들린다(헥사고날).
- **정직한 폐기** — "중앙 토큰 0"을 은폐하지 않고 명시 폐기하고 대체 안전장치(키 보관·로그 미노출·태깅)를 정의했다. 4대 불변식이 아니었던 부수 속성이라 폐기가 4대를 흔들지 않는다.
- **부분 재정의·조각 재조합** — 라우팅 목차(0028)는 목차만 유지, 답변 본문만 중앙에 담는다. WS 전송(0011·0012)은 답 운반이 아니라 지식 동기화 채널로 재사용. `BackupReview`(0012)·reeval(0019)·통지(0022)·HITL(0025)의 조각을 재조합한다 — 새 도메인 기계를 최소화했다.
- **전이 ≠ 기록 보존** — 정정을 답변 레코드 수정이 아니라 새 이벤트로 쌓아 감사 추적을 지킨다(0012·0019의 불변+새 인스턴스 정신).

## Consequences

- **`KnowledgeStore`·`KnowledgeSync`·`Presence`·`AnswerRecord`/`CorrectionEvent`·`HitlPolicy` 신설**(S1·게이트 내 값 객체·포트+Fake·주입 결정론). 실 크로스머신 동기화·실 통지·실 UI·실 중앙 조직 키/스트리밍은 게이트 밖(수동).
- **`AgentRuntime` 소비 원천 이동** — `read_okf_bundle` 입력이 디스크→`KnowledgeStore`. 매핑 함수(`build_provider_request` 등)·포트·`ProviderTransport`는 무변경(주입만 교체).
- **워커 역할 전환** — owner 워커가 답 생성 대신 지식 동기화(publish 파이프라인 재사용·본문으로 확장). `worker.py`가 답 런타임 대신 `KnowledgeSync` 발신을 쥔다(게이트 밖 실 배선).
- **4대 불변식 영향 없음 + "중앙 토큰 0" 폐기**:
  - **미아 없음** — 실행 위치 이동이 라우팅 종착을 안 바꾼다(0→Unowned·≥2→Contested·timeout→escalation 그대로). 오프라인 owner라도 중앙 답 → 가용성 *강화*.
  - **유효하지 않은 카드는 등록되지 않는다** — admission 무변경. 지식 동기화 admission이 그 정신을 본문으로 확장(유효하지 않은/민감 본문 미반영).
  - **Authority 중앙** — 지식 동기화가 권한 자기보고로 못 넓힌다(0028 over-claim 필터 재사용). 프레즌스·HITL은 신뢰 게이트지 권한 아님.
  - **전이 ≠ 기록** — 정정은 새 `CorrectionEvent`(원 `AnswerRecord` 불변). `KnowledgeStore`는 최신 보관이지 audit 아님.
  - **~~중앙 토큰 0~~ (폐기)** — 중앙 조직 API 키 1개로 중앙 과금. 대체 안전장치(키 보관·로그 미노출·태깅)로 보완. 이 폐기는 정직한 트레이드오프(가용성·운영 단순 > 순수성).
- **노출 불변식** — `Answer`·`AnswerRecord`·`CorrectionEvent`가 사용자 페이지에 담당·신뢰 상태·출처·정정만 싣는다(내부값·동기화 메타·API 키 미노출).
- **갱신 대상**: CONTEXT(신규 7용어·Agent Runtime·Provider Runtime 절 재정의)·PRD §3·§5·§6·TRD §2·§4·§10("중앙 토큰 0" 폐기 반영)·tasks Phase 12 S0.

## 결정 (사용자 확정 2026-07-04)

1. **LLM 비용/토큰 귀속 — ✅ 중앙 조직 API 키 1개**("중앙 토큰 0" 정직 폐기·담당자별 태깅/로그·결정 2).
2. **지식 경계 — ✅ 명시 지정만 동기화**(지정 디렉터리/파일/문서만·admission 결합·결정 3).
3. **민감정보 — ✅ 패턴 필터 + 지정 책임**(주민번호·API키·비밀번호 자동 거부 + 지정 담당자 책임·이중 방어·결정 3).
4. **정정 통지 — ✅ 답변 페이지 정정 표시(풀 방식)**(정정 배지 + 정정본·push/메신저는 실 사용자 단계 연기·결정 4).
5. **동기화 주기·stale 임계 — 보수적 기본값(domain-architect 제안)**: 커밋=이벤트 즉시 반영·stale 임계 **30분**(`AON_KNOWLEDGE_STALE_SECONDS` 설정값·조정 가능·결정 3).
6. **프레즌스→HITL 세부 — 보수적 기본값(domain-architect 제안)**: 연결 끊김 즉시 오프라인(grace 0·`AON_PRESENCE_GRACE_SECONDS` 설정값)·에이전트별 기본값 = 카드 approval_when 시드 유지(결정 5).
