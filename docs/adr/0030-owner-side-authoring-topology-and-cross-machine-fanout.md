# OKF 자동 저작의 owner측 토폴로지 — 실 어댑터·저작면·크로스머신 fan-out·reeval 인덱스-수용 훅을 게이트 밖에서 실체화한다

상태: accepted (2026-06-30) · **사용자 grill 확정 5개 결정 명문화**(① 포트 경계[공개 레포 `LlmAuthor`·`SemanticOsAuthor`는 owner 확장점] · ② 저작 전체 owner측[카드 빌더=중앙 / OKF 저작면=owner측] · ③ OKF git owner-로컬·크로스머신 변경 전파 · ④ 얇은 수직 슬라이스 먼저 · ⑤ T10.5 미룸) · **ADR 0029(OKF 자동 저작 게이트 경계·T11.7 슬라이스)가 *게이트 밖*으로 미룬 실 어댑터·실 인프라를 토폴로지로 실체화** — 0029가 *계약*(`OkfAuthor` 포트·`FakeAuthor`·staged·HITL·증분·admission)을 게이트 내로 잠갔고, 이 ADR은 그 게이트 밖(실 OAuth·실 git·실 WS·owner 검토 UI·크로스머신 전파)이 *어디서 어떻게* 도는지를 못박는다 · **확장/재해석이지 supersede 아님** — 0018(카드 빌더=중앙·OKF 커밋이 owner측으로 re-home·이 분할 명시)·0019(발화 단일 지점 → 머신별 단일로 재해석)·0027(owner OAuth 멀티-LLM의 *저작* 새 사용·LlmAuthor가 답변 런타임과 같은 인프라)·0028 §14(`PublishIndex`·`accept_published_index`에 reeval 트리거 훅 추가)·0029(게이트 밖 실체화)와 정합 · CONTEXT(신규 `LlmAuthor`·owner측 저작면·OKF git owner-로컬·머신별 단일 발화·reeval 인덱스-수용 훅·카드빌더 vs OKF저작면 분할)·tasks(T11.7 얇은 수직 슬라이스 재구성·T10.5 미룸 사유) 갱신 대상

---

## 맥락 — 게이트 밖이 *무엇인지*는 닫혔으나 *어디서 도는지*는 비어 있다

ADR 0029가 OKF 자동 저작("LLM wiki")을 도메인·아키텍처로 확정하고, 게이트 내(`OkfDraft`·`OkfDocumentDraft`·`RawSource`·`OkfAuthor` 포트·`FakeAuthor`·staged 오케스트레이션·HITL 상태기계·`admit_okf`·`build_index_from_admitted`·증분 diff·`Ingestor`/`TextIngestor`)를 T11.1~T11.6으로 *전부 green*으로 잠갔다(1577 passed). 남은 것은 **T11.7 — 게이트 밖 실 어댑터·실 인프라**다:

- `LlmAuthor`/`SemanticOsAuthor` 실 추출(실 owner OAuth)
- 실 인제스트 어댑터(PDF/docx/위키)
- owner 검토 UI(빌더 자동 초안 채우기 면·단계별 diff)
- 실 크로스머신 publish(저작→커밋→`PublishIndex` 실 WS)

ADR 0029는 이들을 *목록*으로만 뒀다(§게이트 밖·슬라이스 7). 그런데 이들을 실제로 배치하려면 **되돌리기 어려운 토폴로지 결정**이 필요하다 — 어느 코드가 *공개 레포(core)*에 들어가고 어느 게 *owner 확장점*인가, 저작 LLM·raw 자료·초안·검토 UI가 *중앙*에 닿는가 *owner측*에만 사는가, OKF git이 *중앙 모노repo*인가 *owner-로컬*인가, owner가 자기 OKF를 바꿨을 때 그 변화가 *크로스머신*으로 어떻게 전파되고 중앙 reeval(과거 판례 staleness)이 *무엇으로* 트리거되는가. 이 결정들은 한번 배포되면(워커 토폴로지·git repo 구조·WS 발화 지점) 호환을 깨지 않고는 바꾸기 어렵다 — 그래서 ADR이 필수다.

이 ADR은 사용자 grill로 *이미 확정*(2026-06-30)한 5개 결정을 명문화한다. 재논쟁이 아니라 충실한 화해·shape·SSOT 정합이다. 그리고 가장 중요한 규칙 1 검토 — **owner측 토폴로지가 중앙 토큰 0·비소유와 *정합*인가(충돌 아닌 게이트 밖 실체화인가)** — 를 명문화한다.

---

## 결정 (사용자 grill 확정 2026-06-30)

### 1. 포트 경계 — 공개 레포(core)는 `OkfAuthor` 포트 + `LlmAuthor`(기본) + `FakeAuthor`만, `SemanticOsAuthor`는 owner 확장점

**공급자 중립 불변식(ADR 0027 결정 11)의 저작판.** 공개 레포(core)에 무엇이 들어가는가를 못박는다:

- **core에 싣는 것**: `OkfAuthor` 포트(이미 T11.2 green) + **`LlmAuthor`(기본 어댑터)** + `FakeAuthor`(테스트 더블·이미 green).
  - **`LlmAuthor` = owner OAuth 멀티-LLM 직접 추출·semantic-os import 0.** owner 워커가 자기 OAuth 자격으로 공급자 API(claude·codex·gemini 등)를 직접 불러 raw→OKF 초안을 추출한다. **semantic-os를 import하지 않는다** — RDF/OWL·SPARQL 의존 0. 답변 런타임 `ClaudeApiRuntime`(ADR 0027)이 owner OAuth 인프로세스 스트리밍으로 *답*을 만드는 것과 *같은 인프라를 저작에 쓰는* 어댑터다(같은 owner 워커·같은 provider transport·같은 OAuth 세션 — 책임만 다름: 답 생성 ≠ 지식 저작이라 포트는 분리 `OkfAuthor` ≠ `AgentRuntime`).
- **core에 *안* 싣는 것**: `SemanticOsAuthor`는 **owner측 확장점**이다 — 같은 `OkfAuthor` 포트를 구현하는 *별 패키지/플러그인*으로, semantic-os 깊이(RDF/OWL 온톨로지·seed_nodes/seed_edges·Named Graph)를 원하는 owner가 *자기 배포에서 주입*한다. **core는 semantic-os를 *모른다*** — import 0·의존 0. ADR 0029가 `SemanticOsAuthor`를 "옵션 백엔드"로 둔 것을 *패키징 축*에서 닫는다: core 레포는 그 어댑터를 *담지 않고*, owner가 자기 환경에서 끼운다.
- **`ClaudeApiRuntime`/`ClaudeCodeRuntime`이 같은 `AgentRuntime` 포트의 다른 어댑터인 정신.** `LlmAuthor`(core 기본)·`SemanticOsAuthor`(owner 확장)·`FakeAuthor`(테스트)는 같은 `OkfAuthor` 포트의 N개 어댑터다. 어떤 백엔드도 1급이 아니되, *core가 강제하는 의존*은 `LlmAuthor`의 owner OAuth(이미 ADR 0027 선택 extra)뿐 — RDF는 강제 0(`NotificationChannel` 채널 중립·`KnowledgeIndexMatcher`의 `ConceptOverlapMatcher`[core]/`EmbeddingAnnMatcher`[확장] 분할 정신).

### 2. 저작 전체 owner측 — 카드 빌더(중앙) / OKF 저작면(owner측)의 깔끔한 분할

**비소유·중앙 토큰 0의 실체.** 저작의 *모든 단계*가 owner측에서 돈다 — 중앙은 raw도·초안도·LLM 토큰도 0이다:

- **`LlmAuthor`는 owner 워커 프로세스에서 돈다** — 답변 런타임 `ClaudeApiRuntime`과 *같은 경계*(owner OAuth·provider transport 재사용·`worker.py`). 저작 LLM 호출이 owner OAuth 토큰으로 owner 환경에서 일어난다(중앙 토큰 0·ADR 0027 보존·강화).
- **raw 자료·단계별 초안은 중앙을 통과하지 않는다.** owner의 기존 문서·노트·위키(raw)는 owner 환경에 있고, staged 초안(②split·③derive·④link 산출)도 owner 환경(working tree·검토 면)에 머문다. **중앙 WS로 흘리면 비소유 위반** — 중앙은 *승인·publish된 목차*(`KnowledgeIndex`·`PublishIndex`)만 받는다(ADR 0028 §14·결정 3). raw·초안을 중앙 WS에 싣는 프레임은 *만들지 않는다*.
- **따라서 owner 검토 UI도 owner측**이다. 단계별 diff 검토(②③④ 산출을 diff로 보고 승인/수정/거부)는 owner 환경에서 도는 면이다 — 초안이 owner측에만 있으므로 그 초안을 보여주는 UI도 owner측일 수밖에 없다(중앙이 초안을 안 보므로 중앙이 검토 UI를 못 띄운다).
- **깔끔한 분할(되돌리기 어려운 경계)**:

  | 면 | 무엇 | 어디 | 코드 |
  |---|---|:---:|---|
  | **카드 빌더** | 라우팅 메타(domains·can/cannot_answer·admission 단위) | **중앙** | `web.py`(`/builder/validate`·`validate_card_for_builder`) 유지 |
  | **OKF 저작면** | raw→LLM 초안→단계 diff 검토→승인 | **owner측 신규** | owner 환경(빌더 패턴 `builder.html` 재사용·신규) |

  - **카드 빌더가 중앙인 이유**: 카드는 *admission 경계*(유효하지 않은 카드는 등록 안 됨·Authority 중앙·ADR 0004·0018 결정 1)다. 검증→YAML→수동 PR이 중앙 운영 면에 남는다(ADR 0018 결정 1·CONTEXT Card composer 절 무변경). 라우팅 메타·domains 선언은 중앙 권위라 중앙면이 자연.
  - **OKF 저작면이 owner측인 이유**: OKF 번들은 *답변 지식*이지 권한 선언이 아니고(ADR 0013 안 B), raw·초안·LLM 호출이 모두 owner측이라(결정 위) 그 저작·검토 면도 owner측에 있어야 비소유가 성립한다. **중앙 토큰 0·비소유 보존** — 이 분할이 그 보존의 구조적 보장이다.

### 3. OKF git owner-로컬 · 크로스머신 변경 전파

**OKF git이 owner-로컬임을 못박고, owner의 변화가 크로스머신으로 어떻게 전파되는지 닫는다.**

- **각 owner가 *자기* OKF git repo를 소유한다(중앙 모노repo 아님).** ADR 0018 결정 2가 MVP로 둔 "모노repo 하위폴더 `okf/{agent_id}/`"를 *owner-로컬 repo*로 진화시킨다(0018이 "owner별 repo는 후속 옵션"으로 예고한 자리). owner가 자기 환경에서 OKF를 로컬 git으로 버전 저장하고, `GitGateway` 포트(ADR 0018) 뒤에 가둔다(실 구현은 `SubprocessGitGateway`·owner 환경). **GitHub는 선택적 원격 백업**이지 핵심 경로가 아니다 — owner가 원하면 자기 OKF repo를 GitHub에 push해 백업하나, 시스템 동작은 그것에 의존하지 않는다(중앙이 그 repo를 *읽지 않으므로*).
- **중앙은 OKF·git을 안 가진다 — WS `PublishIndex`(목차·토큰 0)만.** 중앙은 owner OKF repo를 클론·pull·읽기 어느 것도 하지 않는다. owner→중앙으로 흐르는 것은 `PublishIndex`(`KnowledgeIndex` 목차·ADR 0028 §14)뿐이다(내용 0·비소유). 이게 ADR 0006/0010 "중앙 비소유·목차만"을 *진짜로* 실현한다(ADR 0028 §14 "데모 시드 지름길[중앙이 repo `okf/` 직접 읽기]은 in-process 단축"을 토폴로지에서 제거하는 방향).
- **크로스머신 변경 전파 사슬**:

  ```
  owner 커밋(로컬 git)
    → OkfChangeEvent(owner측 1회 발화)          ← commit_okf_bundle 단일 발화(ADR 0019 결정 1)
    → reindex 소비자(로컬·디스크 재도출·T11.7b)    ← worker.publish_frames (build_knowledge_index_from_okf)
    → PublishIndex(중앙 WS 송신·목차만)          ← 워커 publish(ADR 0028 §14 결정 E)
    → 중앙 accept_published_index(더 새 generated_at 수용)
    → StalenessPropagator 훅(과거 판례 reeval)    ← 이 ADR이 추가하는 reeval 인덱스-수용 훅
  ```

- **중앙 reeval(과거 판례 staleness)은 *더 새 인덱스 도착*으로 트리거한다 — commit이 아니다.** 핵심 재해석:
  - owner측에선 **commit이 reindex 발화**다(`commit_okf_bundle`이 `OkfChangeEvent`를 발화 → 증분 재인덱싱). 이건 ADR 0019 발화 단일 지점이 *owner 머신에서* 도는 것이다.
  - 중앙측에선 **index 수용이 reeval 발화**다 — `accept_published_index`가 *더 새 `generated_at`을 수용하는 순간*이 "이 owner 지식이 바뀜"의 신호다(그 더 새 인덱스가 도착했다는 것 자체가 owner OKF가 변했다는 증거). 그 순간에 `StalenessPropagator` 훅(ADR 0019)이 그 agent_id에 기댄 과거 Precedent·답을 reeval 큐로 보낸다.
  - **왜 commit이 아니라 index 수용인가**: 중앙은 commit을 *볼 수 없다*(OKF git이 owner-로컬·중앙은 repo를 안 읽음). 중앙이 관측할 수 있는 유일한 "owner 지식이 바뀜" 신호는 *더 새 인덱스의 도착*이다. commit→reeval을 중앙에서 직접 걸려면 중앙이 owner git을 봐야 하는데 그건 비소유 위반이다. 그래서 reeval 트리거를 *index 수용*으로 옮긴다 — 비소유를 지키면서 staleness 전파를 산다.
- **ADR 0019 "발화 단일 지점"이 *머신별 단일*로 진화한다**:
  - **owner측 = commit이 reindex 발화**(`commit_okf_bundle` 단일·owner 머신).
  - **중앙측 = index 수용이 reeval 발화**(`accept_published_index`가 더 새 것 수용 시·중앙 머신).
  - **크로스머신 이벤트 중복 없음** — owner commit은 reindex만 트리거하고(과거 판례 reeval을 owner가 직접 안 함·중앙 책임), 중앙 index 수용은 reeval만 트리거한다(reindex를 중앙이 안 함·owner 책임). 두 발화가 서로 다른 머신·다른 책임이라 한 변경이 두 번 reeval되거나 누락되지 않는다.
  - **단일 머신 배포에서도 동일** — 워커=중앙 박스(`LocalRuntimeDispatcher` 디제너레이트 케이스·CONTEXT)면 WS 루프백으로 owner commit→reindex→PublishIndex→(루프백)→accept→reeval이 같은 프로세스에서 도되 *같은 발화 구조*를 탄다(commit이 reindex·수용이 reeval). 토폴로지가 분산이든 단일이든 발화 구조가 안 바뀐다.

### 4. 얇은 수직 슬라이스 먼저 · 넓게는 가산

**첫 산출물은 저작 루프를 *가장 가벼운 실 어댑터로* 한 번 관통한다**(과도 엔지니어링 회피·ADR 0029 증분 정신):

```
텍스트 인제스트(TextIngestor·기존 T11.6)
  → LlmAuthor(실 owner OAuth)
  → owner 로컬 웹 검토면(builder.html 패턴 재사용·단계 diff·승인)
  → 커밋(로컬 git·commit_okf_bundle 기존 T11.3 seam)
  → OkfChangeEvent(기존 발화 단일)
  → 재인덱싱(publish_frames 디스크 재도출·기존 worker.publish_frames)
  → PublishIndex 중앙 송신(기존 워커 publish·ADR 0028 §14)
  → 중앙 라우팅 반영(기존 TwoStageRouter)
```

- 이 슬라이스는 **기존 게이트 내 계약을 *전부 재사용***한다 — 신규는 (a) `LlmAuthor` 실 추출, (b) owner 로컬 웹 검토면, (c) reeval 인덱스-수용 훅 배선뿐이다. 나머지(인제스트·HITL·증분·커밋·publish·라우팅)는 T11.1~T11.6·ADR 0028이 이미 닫았다.
- **이번 미룸(후속 가산·포트 뒤·재작업 0)**: PDF/docx/위키 `Ingestor` 어댑터(T11.7(b))·`SemanticOsAuthor`(owner 확장점)·리치 UI·골든셋 eval 깊이. 모두 포트 뒤라 어댑터 추가가 재작업 0(`Ingestor` 포트·`OkfAuthor` 포트·`KnowledgeIndexMatcher` 포트가 자리를 이미 깔았다).

### 5. T10.5 미룸 — `ConceptOverlapMatcher`로 충분

**스케일 라우팅 게이트 밖(T10.5)의 임베딩·실 RAG는 *스케일/contested 볼륨이 실제 압박할 때* 당긴다.** 지금 당기지 않는다:

- **`ConceptOverlapMatcher`(core_question 토큰 오버랩·게이트 내 완성·T10.2)로 충분하다.** stage-1 후보 제안이 결정론 토큰 오버랩으로 닫혀 있고(토큰 0·벡터 0), 현 규모에서 정밀도가 압박되지 않는다.
- **미루는 것**:
  - `EmbeddingAnnMatcher`(T10.5(a))·**임베딩 모델 선택(되돌리기 어려운 새 의존성·벡터 라이브러리)** — 스케일이 stage-1 토큰 오버랩 정밀도를 실제로 무너뜨릴 때 당긴다. 임베딩 결정 *보류*(되돌리기 어려운 의존성을 압박 없이 박지 않는다).
  - 실 owner RAG `ConfidenceAssessor`(T10.5(c)) — contested 볼륨이 stage-2 자동해소를 실제로 요구할 때(≥2 모호 후보가 빈번해 사람 합의가 병목일 때) 당긴다. `FakeAssessor`(T10.3b)가 잠근 자동해소 로직은 이미 green이라 실 채움은 압박 시.
- **T10.5(b) distill은 `LlmAuthor`가 흡수한다.** ADR 0028 OQ ①의 "실 `core_question` distill"(개념·core_question·edges 추출 = 인덱스 생성)은 *저작*과 같은 일이다 — `LlmAuthor`의 split/derive/link가 곧 distill이고, 그 산출 OKF를 `build_knowledge_index_from_okf`가 인덱스로 만든다(ADR 0029 5단계 재사용). 즉 T10.5(b)는 별도 어댑터가 아니라 *이 ADR의 `LlmAuthor`가 흡수*한다(저작이 distill을 겸함). semantic-os 깊이 distill은 `SemanticOsAuthor`(owner 확장점·결정 1)가 옵션으로.

---

## shape 결정 (grill에서 ADR로 위임)

> tdd-engineer/mcp-runtime-engineer가 실체화한다. 슬라이스 *세부* 분해는 planner. 아래는 *모양*이지 구현이 아니다.

### S1. `LlmAuthor` 어댑터 — `OkfAuthor` 포트 실 구현(owner OAuth·semantic-os import 0)

`OkfAuthor` 포트(T11.2 green — `split`/`derive_core_questions`/`link` 3메서드)의 실 구현. owner 워커가 자기 OAuth provider transport(ADR 0027)로 공급자 API를 직접 불러 추출한다.

```
class LlmAuthor:                          # OkfAuthor 구현 — owner OAuth 멀티-LLM
    def __init__(self, transport: ProviderTransport, ...):   # ADR 0027 transport 재사용
        ...
    def split(self, sources) -> tuple[OkfDocumentDraft, ...]: ...        # 2단계
    def derive_core_questions(self, drafts) -> tuple[OkfDocumentDraft, ...]: ...  # 3단계
    def link(self, drafts) -> tuple[ConceptEdge, ...]: ...               # 4단계
```

- **답변 런타임과 같은 인프라**: `LlmAuthor`는 `ClaudeApiRuntime`이 쥐는 그 provider transport(owner OAuth 인프로세스 스트리밍)를 재사용한다. 같은 owner 워커·같은 OAuth 세션·같은 SDK. 답 생성(`AgentRuntime.answer`)과 지식 저작(`OkfAuthor.split/derive/link`)이 책임만 달라 포트는 분리된다(ADR 0027 §(d) 정신 — `OkfAuthor` ≠ `AgentRuntime`).
- **semantic-os import 0**: `LlmAuthor`는 RDF/OWL·SPARQL을 import하지 않는다(core 어댑터). 깊은 온톨로지 distill은 `SemanticOsAuthor`(owner 확장점·별 패키지) 책임.
- **게이트 밖(실 인프라·비결정)**: 실 owner OAuth 호출·실 추출 품질(개념 분할·core_question·edges)은 골든셋 eval(ADR 0003)로 검증. 게이트 내는 `FakeAuthor`(T11.2 green)가 staged 오케스트레이션·HITL·증분의 *결정 로직 전부*를 이미 잠갔다 — `LlmAuthor`는 주입만 교체(`FakeAuthor`→`LlmAuthor`).

### S2. owner측 OKF 저작면 seam — builder.html 패턴 재사용·단계 diff 상태기계

owner 환경에서 도는 검토 면. raw 입력 → `LlmAuthor` staged 산출 → 단계별 diff → owner 처분(`set_disposition`·T11.3 green) → 승인분 `commit_okf_bundle`(T11.3 seam).

- **빌더 패턴 재사용**: ADR 0018 `builder.html`(카드 빌더·OKF 편집 면)의 폼/검증/커밋 패턴을 owner측에 재현한다. 단 *owner 환경*에 산다(중앙 `web.py`가 아니라 owner 워커 측 면 — 결정 2). 커밋 author=owner·스코프 card.owner(ADR 0018 결정 5)는 그대로.
- **단계 diff 상태기계(게이트 내 결정론·이미 green)**: `StageReview`·`StageDisposition`(`Approved`/`Edited`/`Rejected`)·`set_disposition`(T11.3)이 그 면의 *상태 전이 로직*이다. UI 조작(클릭·폼)은 게이트 밖(수동), 그 아래 상태기계는 게이트 내(결정론·T11.3 green).
- **게이트 밖**: 실 UI 면·실 raw 입력·단계 diff 시각화·owner 조작은 수동(ADR 0029 빌더 UI가 게이트 밖인 정신).

### S3. 크로스머신 fan-out seam — reindex 소비자·PublishIndex 워커 송신(Fake 리스너 결정론)

owner commit → `OkfChangeEvent` → 재인덱싱 → `PublishIndex` 송신의 *배선*. 발화·소비의 결정 로직은 게이트 내, 실 WS·실 워커는 게이트 밖.

- **발화 단일 보존**: `commit_okf_bundle(propagator=...)`(T11.3 seam·ADR 0018/0019)이 `OkfChangeEvent`를 발화하는 단일 지점을 owner 머신에서 유지한다. 새 발화점을 만들지 않는다(ADR 0019 결정 1·머신별 단일로 재해석).
- **reindex 소비자** *(정정 — T11.7b shape·2026-06-30)*: `OkfChangeEvent` 수신 → 워커 `publish_frames`(= `build_knowledge_index_from_okf` **디스크 재도출**·`worker.py:186` green·ADR 0028 §14) → `PublishIndex` 프레임 구성 → 송신. **`reindex_incrementally`(T11.5)를 부르지 *않는다***: ① `reindex_incrementally`는 `ReindexResult(AuthoredOkf)`를 내지 `PublishIndex`를 안 만든다(타입 불연속), ② `ReindexRequest`가 요구하는 `prior AuthoredOkf`·`changed_sources`(저작-시점 메모리)를 `OkfChangeEvent`가 *안 든다*(조달 불가). **커밋-직후 진실 원천은 디스크**(OKF가 owner working tree에 확정)이므로 `publish_frames`(디스크 재도출·prior 불요)가 정확하다. `reindex_incrementally`는 *저작-시점*(raw 편집→메모리 증분·commit 전·T11.7c/d 내부) 최적화로 남고, 크로스머신 fan-out의 "재인덱싱"은 **배포-시점 디스크 재도출(publish_frames)**을 가리킨다.
- **게이트 내 결정론(Fake 리스너)**: `ChangeEventListener`(`git_gateway.py` Protocol·green)를 구현하는 *Fake 리스너*를 주입해 "commit → reindex → publish 프레임 구성"의 fan-out 배선을 결정론으로 단언한다(`FakeGitGateway`·`StubRuntime` 정신). 실 WS 송신·실 워커는 게이트 밖.

### S4. reeval 인덱스-수용 훅 — `accept_published_index`가 더 새 것 수용 시 StalenessPropagator 발화

중앙측 reeval 발화 지점. `accept_published_index`(`two_stage_router.py` green)가 *더 새 `generated_at`을 수용하는 순간* `StalenessPropagator`(reeval.py green)를 옵셔널로 부른다.

```
accept_published_index(session_owner_id, index, registry, store, propagator=None):
    if not publishable(...): return False
    filtered = filter_authorized_concepts(...)
    accepted_newer = store.put(filtered)        # 더 새 generated_at만 수용(staleness·결정 C)
    if propagator is not None and accepted_newer:
        propagator.on_okf_committed(event_from_index(filtered))   # ← reeval 인덱스-수용 훅(이 ADR 신규)
    return True
```

- **옵셔널 주입(하위호환)**: `propagator: StalenessPropagator | None = None`. 미주입이면 *기존 동작 그대로*(`commit_okf_bundle(propagator=None)`·ADR 0019 정신). 주입되면 *더 새 인덱스 수용 시에만* 발화(동률·역행 수용 거부는 발화 안 함 — staleness 멱등 흡수가 reeval 멱등도 보장).
- **`store.put` "수용 여부" 노출 진화(작은 게이트 내 변경)**: 현 `InMemoryPublishedIndexStore.put`은 `None` 반환(더 새면 교체·동률/역행 no-op·green). 이 훅은 *더 새 것 수용 시에만* 발화해야 하므로 `put`이 "수용했나"(bool)를 알려야 한다 — `put -> bool`로 좁히거나(되돌리기 쉬운 시그니처 진화·하위호환 흡수) 호출자가 `get` 전후 `generated_at` 비교로 판정한다(둘 다 게이트 내·tdd-engineer가 더 작은 쪽 선택). 의사코드의 `accepted_newer`가 그 진화 자리.
- **`StalenessPropagator` 재사용(새 메커니즘 0)**: ADR 0019의 `on_okf_committed(event)`가 그 agent_id에 기댄 과거 Precedent·답을 reeval 큐로 보낸다. 이 ADR은 그 발화 *지점*을 commit(owner 머신)에서 index 수용(중앙 머신)으로 옮길 뿐이다. propagator 본체·reeval 큐·1인칭 처분(ADR 0019)은 무변경.
- **`OkfChangeEvent` 구성(중앙측)** *(정정 — T11.7a code-review M1·2026-06-30)*: 중앙은 비소유라 owner의 git 커밋 SHA를 *모른다*(중앙은 owner repo를 안 읽음·결정 3). 그래서 `agent_id`(= index.agent_id)·`committed_at`(= index.generated_at)을 채우되, **SHA 필드는 *필드별로 다르게* 다룬다**:
  - **`parent_sha`·`changed_paths`(죽은 필드·진짜 무영향)**: reeval은 *agent_id 거친 매칭*(ADR 0019 결정 2·`changed_paths`는 죽은 필드)이라 `on_okf_committed`가 안 읽는다 → `None`/`()`로 둬도 무영향.
  - **`new_sha`(살아있는 필드 — 빈값 두지 말 것)**: `reeval.py`가 `new_sha`를 *읽는다* — `flag_stale(trigger_sha=event.new_sha)`(감사)·Answer 축 가지치기 `if snapshot_sha == event.new_sha: continue`(같은 커밋으로 만든 답은 fresh라 제외). 따라서 빈값이 아니라 **인덱스 파생 합성 토큰**(`f"index@{index.generated_at.isoformat()}"`)을 채운다. 이 토큰은 (1) 실 답 `snapshot_sha`(git SHA 또는 None)와 *절대 안 겹쳐* Answer 축이 그 agent의 routed 답을 *전부 보수적 재적재* — **중앙이 freshness를 알 수 없으니(SHA 모름·비소유) 전부 재평가가 *올바른* 동작**(놓침 0 > 과검출 0). (2) `trigger_sha` 감사에 "인덱스 수용발 reeval"임을 드러낸다. **즉 commit 경로(owner측·실 SHA로 정밀 가지치기)와 index-수용 경로(중앙측·합성 토큰으로 전체 재적재)는 의도적으로 다른 정밀도**다(중앙은 SHA를 못 보므로 더 보수적). 큐 볼륨이 commit 경로보다 큰 점은 실 인프라 관측 후 임계 튜닝(T11.7e·ADR 0019 "큐 폭증 임계").
- **게이트 내 결정론**: `FakeStalenessPropagator`(또는 spy) 주입으로 "더 새 인덱스 수용 → reeval 발화 1회 / 동률·역행 → 발화 0회"를 단언. 실 reeval 큐·실 워커는 무관(이미 ADR 0019 green).

### S5. Authority·노출 무변경

- 저작 자동화도 **under-claim 자기보고**(ADR 0004·0029 S6) — over-claim concept은 `admit_okf`(저작측·T11.3)·`filter_authorized_concepts`(중앙 publish 수용·ADR 0028 §14 결정 D)가 이중으로 떨군다. owner측 토폴로지가 Authority를 *넓히지 않는다*.
- 저작면·검토 UI는 *owner 운영 면*(자기 OKF 저작)이지 사용자 경로(`OrgReply`)가 아니다. raw·초안·diff는 owner↔owner 운영 채널·자기 카드 범위(빌더 스코프 card.owner·ADR 0018 결정 5).

---

## SSOT 화해 (규칙 1 — 가장 중요)

이 ADR은 **게이트 밖 실체화·토폴로지 결정이지 충돌이 아니다 — 새 ADR감**이다. 기존 ADR들과의 관계를 명시한다(supersede 아님·확장/재해석).

### (a) ADR 0018(카드 빌더·OKF git 커밋) — *재해석(OKF 커밋 owner측 re-home·카드 빌더 중앙 유지·이 분할 명시)*

0018은 OKF 번들을 *모노repo 하위폴더 `okf/{agent_id}/`*(MVP)에 두고 빌더가 owner 대신 커밋(`commit_okf_bundle`·author=owner·스코프 card.owner)까지 닫았다. 이 ADR은:
- **OKF git을 owner-로컬 repo로 re-home**(0018 결정 2가 "owner별 repo는 후속 옵션"으로 예고한 자리·실 데이터 격리가 진짜 요구일 때). 중앙 모노repo 가정을 owner-로컬로 진화.
- **카드 빌더는 중앙 유지**(0018 결정 1 — 카드는 admission 경계라 검증→YAML→PR·중앙면). 카드 빌더 vs OKF 저작면 분할을 *명시*한다(결정 2 표).
- **커밋 메커니즘 무변경**: `GitGateway`·`commit_okf_bundle`·author=owner·`OkfChangeEvent` 발화(0018 결정 6)는 그대로. 바뀌는 건 *repo가 owner-로컬*이고 *OKF 저작면이 owner측*인 것뿐(커밋 본체는 여전히 OKF 번들 마크다운·카드 YAML 아님).

### (b) ADR 0019(OkfChangeEvent 단일 발화) — *재해석(머신별 단일로)*

0019는 `commit_okf_bundle`을 *단일 발화 지점*으로 못박았다(빌더 커밋의 유일한 닫힌 루프). 이 ADR은 그 단일성을 *머신별 단일*로 진화시킨다:
- **owner측 = commit이 reindex 발화**(`commit_okf_bundle` 단일·owner 머신·증분 재인덱싱 트리거).
- **중앙측 = index 수용이 reeval 발화**(`accept_published_index`가 더 새 것 수용 시·중앙 머신·과거 판례 staleness 트리거).
- **0019 본체 무변경**: `StalenessPropagator`·`ReevalSubject`·`ReevalItem`·reeval 큐·1인칭 처분은 그대로. 이 ADR은 그 발화 *지점*을 reeval 축에서 commit→index 수용으로 옮길 뿐(중앙이 owner commit을 못 보므로 — 비소유). 0019가 한 머신을 전제했다면(중앙이 OKF git을 봄·데모 시드), 이 ADR이 *크로스머신 분리*(owner git·중앙 목차)에서 발화를 머신별로 가른다. **크로스머신 이벤트 중복 없음**이 0019 "발화 단일"의 정신을 분산에서 지킨다.

### (c) ADR 0027(owner OAuth 멀티-LLM) — *새 사용 강화(LlmAuthor가 답변 런타임과 같은 인프라)*

0027은 owner OAuth 멀티-LLM을 *답변* 경로(`ClaudeApiRuntime`)에 썼다. 0029가 그걸 *저작*에 쓰는 새 사용을 예고했고, 이 ADR은 그 실체를 닫는다:
- **`LlmAuthor`가 답변 런타임과 같은 인프라**(같은 owner 워커·같은 provider transport·같은 OAuth 세션·`worker.py`). 저작과 답변이 인프라를 공유하되 포트는 분리(`OkfAuthor` ≠ `AgentRuntime`·0027 §(d)).
- **공급자 중립(0027 결정 11) 보존·강화**: core는 `LlmAuthor`만 싣고 `SemanticOsAuthor`(RDF 의존)는 owner 확장점이라 *core에 RDF 의존 0*(0027 "코어는 어떤 공급자 SDK에도 의존 0"의 저작판). 중앙 토큰 0 보존(자격증명 owner OAuth).

### (d) ADR 0028 §14(PublishIndex·accept_published_index) — *훅 추가(reeval 트리거 지점)*

0028 §14는 `PublishIndex`(owner→중앙 목차 배포)·`accept_published_index`(스코핑→필터→staleness put)를 닫았다. 이 ADR은 그 *수용 지점에 reeval 트리거 훅을 더한다*:
- **`accept_published_index`에 `propagator` 옵셔널 주입**(S4) — 더 새 `generated_at` 수용 시 `StalenessPropagator` 발화. 0028 §14 결정 C(staleness·더 새 것만·동률/역행 거부)는 그대로이고, *수용 성공*에 reeval 훅을 건다.
- **0028 §14 결정 E "OKF 변경 재배포 트리거(OkfChangeEvent 연동·후속)"의 실체**: 0028이 "MVP는 연결 시 publish만·OKF 변경 재배포는 자리만"으로 둔 것을, 이 ADR이 *크로스머신 전파 사슬*(commit→reindex→publish→accept→reeval)로 닫는다. publish 경로·프레임·스토어는 무변경.

### (e) ADR 0029(OKF 자동 저작 게이트 경계·T11.7) — *게이트 밖 실체화*

0029는 게이트 내(T11.1~T11.6·계약)를 잠그고 게이트 밖(T11.7·실 어댑터)을 *목록*으로 뒀다. 이 ADR은 그 목록을 *토폴로지로 실체화*한다:
- **0029 §게이트 밖의 항목들을 어디서 어떻게 돌릴지 못박음**: `LlmAuthor`(owner 워커·결정 1·2)·owner 검토 UI(owner측·결정 2)·실 크로스머신 publish(owner git→PublishIndex·결정 3)·얇은 슬라이스(결정 4).
- **0029 결정·계약 무변경**: `OkfAuthor`/`Ingestor` 포트·`OkfDraft`/`AuthoredOkf` 값 객체·staged·HITL·증분·admission은 그대로(실 어댑터가 그 계약을 채움). 이 ADR은 0029를 *supersede 하지 않고* 그 게이트 밖을 실체화한다.

### 충돌인가 확장인가 — 판단

**확장/재해석이다(충돌 아님·supersede 아님).** 근거:
1. **소비 경로 무변경**: 답변·라우팅·fetch는 그대로. 이 ADR은 *저작 게이트 밖 토폴로지*만 닫는다.
2. **비소유·중앙 토큰 0 보존·강화**: 저작 전체가 owner측(raw·초안·LLM 토큰 0이 중앙 도달)·OKF git owner-로컬(중앙은 목차만)·reeval이 index 수용으로 트리거(commit을 중앙이 안 봄). owner측 토폴로지가 비소유를 *구조적으로 보장*한다(아래 §비소유 논거).
3. **Authority 중앙 무변경**: 자동 OKF도 under-claim·권한은 중앙(over-claim concept 이중 필터·결정 2·S5).
4. **발화 단일 정신 보존**: ADR 0019 단일 발화가 *머신별 단일*로 진화하되 크로스머신 이벤트 중복 0(정신 보존).
5. **기존 ADR을 supersede 하지 않음**: 0018(재해석)·0019(머신별 재해석)·0027(새 사용)·0028 §14(훅 추가)·0029(게이트 밖 실체화) 위에 *토폴로지 층*을 더하는 확장이라 헤더에 supersede 미표기(각 ADR 헤더엔 0030 포인터를 단다 — 확장/재해석 명시).

### 비소유·중앙 토큰 0 정합 논거 (명시 — 규칙 1 핵심)

owner측 토폴로지가 중앙 토큰 0·비소유와 *정합*(충돌 아님·게이트 밖 실체화)임을 명문화한다:

1. **저작 LLM이 owner OAuth·owner 워커**: `LlmAuthor` 추출 호출이 owner OAuth 토큰으로 owner 환경에서 돈다 — 중앙은 모델 토큰 0(ADR 0027 보존·강화). core는 RDF 의존도 0(`SemanticOsAuthor` owner 확장점).
2. **raw·초안이 owner측에만**: owner의 raw 자료·staged 초안은 owner 환경·검토 면에 머문다 — 중앙 WS로 흘리지 않는다(흘리면 비소유 위반·결정 2). 중앙은 초안을 안 본다.
3. **OKF git owner-로컬·중앙은 목차만**: 중앙은 owner OKF repo를 클론·읽기 0 — `PublishIndex`(목차·내용 0)만 받는다(결정 3). 본문 비소유(on-demand fetch·ADR 0028 §15)와 정합 — 중앙은 목차·owner는 내용.
4. **reeval이 index 수용으로 트리거(commit을 안 봄)**: 중앙이 owner commit을 보려면 owner git을 읽어야 하는데 그건 비소유 위반이다. 그래서 reeval을 *index 수용*(중앙이 정당히 관측하는 신호)으로 옮겨 비소유를 지키며 staleness를 산다(결정 3).

저작 전 과정에서 중앙은 raw·초안·OKF 본문·git·모델 토큰 어느 것도 0개 보관 — 비소유가 *저작 토폴로지에서도* 구조적으로 성립한다(ADR 0029 §비소유 논거의 토폴로지판).

---

## 게이트 내/밖 경계

**게이트 내(결정론·`.venv` pytest로 잠금):**
- 크로스머신 fan-out 배선의 *결정 로직*(`OkfChangeEvent` → reindex 소비자 → `PublishIndex` 프레임 구성) — **Fake 리스너**(`ChangeEventListener` 구현 spy) 주입 결정론(S3).
- owner측 저작면 *상태기계*(`StageReview`·`set_disposition`·단계 diff 전이) — 이미 T11.3 green(S2).
- reeval 인덱스-수용 훅(`accept_published_index(propagator=...)`이 더 새 것 수용 시 발화 1회·동률/역행 시 0회) — **FakeStalenessPropagator/spy** 주입 결정론(S4).
- 발화 머신별 단일 보존(owner commit이 reindex만·중앙 수용이 reeval만·중복 0) — 주입 spy로 단언.

**게이트 밖(수동·실 인프라·비결정):**
- 실 owner OAuth(`LlmAuthor` 실 추출 — owner OAuth 멀티-LLM·비결정·골든셋 eval·ADR 0003·S1).
- 실 WS(저작→커밋→`PublishIndex` 실 크로스머신 송신·실 워커·실 소켓).
- 실 git(owner-로컬 OKF repo·실 `SubprocessGitGateway`·실 커밋·실 SHA).
- owner 검토 UI(빌더 자동 초안 채우기 면·단계별 diff 시각화·owner 조작 — 수동 시연·ADR 0029 빌더 UI 게이트 밖 정신).
- 얇은 수직 슬라이스 end-to-end 수동 시연(텍스트 인제스트→LlmAuthor→검토면→커밋→OkfChangeEvent→재인덱싱→PublishIndex→중앙 라우팅 반영·결정 4).

---

## planner 넘김용 윤곽 (얇은 수직 슬라이스 — 결정 4)

> 상세 슬라이싱은 planner가 받는다. 이 ADR은 결정·게이트 경계·갱신 대상까지. 얇은 수직 슬라이스를 *가장 가벼운 실 어댑터로 한 번 관통*하는 순서(의존성 순):

1. **reeval 인덱스-수용 훅** — `accept_published_index(propagator=)` 옵셔널 진화·더 새 것 수용 시 `StalenessPropagator` 발화·spy 결정론(S4). **게이트 내**(기존 스토어·propagator 재사용·되돌리기 쉬운 옵셔널 진화). 첫 진입(self-contained·실 인프라 무관).
2. **크로스머신 fan-out 배선(Fake 리스너)** — `OkfChangeEvent` → reindex 소비자 → `PublishIndex` 프레임 구성을 Fake 리스너로 결정론 단언(S3). **게이트 내**(발화 머신별 단일 보존 검증). (1) 정합.
3. **`LlmAuthor` 실 어댑터** — `OkfAuthor` 포트 실 구현·owner OAuth provider transport 재사용·semantic-os import 0(S1). **게이트 밖**(실 OAuth·비결정·`FakeAuthor`→`LlmAuthor` 주입 교체·eval).
4. **owner측 저작면** — builder.html 패턴 재사용·단계 diff 검토·승인·커밋(S2). **게이트 밖**(실 UI·수동·owner 환경). 상태기계는 게이트 내(T11.3 green).
5. **얇은 슬라이스 end-to-end 수동 시연** — 텍스트→LlmAuthor→검토면→커밋(owner git)→OkfChangeEvent→재인덱싱→PublishIndex→중앙 라우팅·reeval 반영(결정 4). **게이트 밖**(실 WS·실 워커·실 git·실 LLM).

**후속 가산(이번 미룸·포트 뒤·재작업 0)**: PDF/docx/위키 `Ingestor` 어댑터·`SemanticOsAuthor`(owner 확장점)·리치 UI·골든셋 eval 깊이(결정 4) · T10.5 `EmbeddingAnnMatcher`·임베딩 모델 선택·실 RAG `ConfidenceAssessor`(스케일/contested 볼륨 압박 시·결정 5).

---

## 핵심 불변식 자체점검

- **미아 없음 — 무관(보존)**: 저작 토폴로지는 *지식 생성*이지 *질문 라우팅*이 아니다(ADR 0029 정신). owner 워커 부재·저작 실패·crossmachine 전파 지연은 그 owner OKF 갱신이 늦을 뿐, 라우팅 종착(Routed/Unowned/Contested)을 안 건드린다. PublishIndex 미도착이면 stage-1이 옛 인덱스로 라우팅(또는 0 후보→Unowned·root escalation·ADR 0028 §13) — 어느 경우도 미아가 안 된다.
- **Authority 중앙(보존)**: 자동 생성 OKF도 under-claim·권한은 중앙 선언(`card.domains`·routing_rules·ADR 0004). over-claim concept은 `admit_okf`(저작측·T11.3)·`filter_authorized_concepts`(중앙 publish 수용·ADR 0028 §14 결정 D)가 이중 필터. owner측 토폴로지·저작 자동화가 권한을 *넓힐 수 없다*(S5).
- **중앙 토큰 0 / 비소유(보존·강화)**: 저작 LLM(owner OAuth·owner 워커)·raw·초안(owner측·중앙 WS 미도달)·OKF git(owner-로컬·중앙 클론 0)이 모두 owner측, 중앙은 published 목차만 받는다(§비소유 논거). reeval이 index 수용으로 트리거(중앙이 commit·git을 안 봄). ADR 0027 "중앙 키 0"을 저작 토폴로지에서도 보존·강화.
- **공급자 중립(보존·강화)**: core는 `LlmAuthor`(owner OAuth·결정 1)만·`SemanticOsAuthor`(RDF)는 owner 확장점이라 *core에 RDF 의존 0*(ADR 0027 결정 11의 저작판). 어떤 백엔드도 1급 아님(`OkfAuthor` 포트 N 어댑터).
- **발화 단일[머신별](보존·재해석)**: owner commit이 reindex만·중앙 index 수용이 reeval만 발화·크로스머신 이벤트 중복 0(결정 3). ADR 0019 "발화 단일 지점"이 머신별 단일로 진화하되 한 변경이 두 번 reeval되거나 누락되지 않는다. 단일 머신 배포에서도 WS 루프백으로 동일 구조.
- **노출 불변식(무관)**: 저작·검토는 *owner 운영 면*(자기 OKF 저작)이지 사용자 경로(`OrgReply`)가 아니다. raw·초안·diff는 owner↔owner 운영 채널·자기 카드 범위(빌더 스코프 card.owner·ADR 0018 결정 5).
- **등록 무결성(보존) — 유효하지 않은 OKF는 publish 안 됨**: 자동 산출 OKF도 okf_index 도출 규칙·publish 권한 검증(`concept.domain ∈ card.domains`·워커-소유자 스코핑·ADR 0028 §14)을 통과해야 인덱스로 배포된다. owner 미승인 staged 초안은 소비 경로(답변·라우팅·fetch)에 안 닿는다(ADR 0029 결정 2).
- **전이 ≠ 기록(보존)**: 저작 단계 전이(staged→approved/edited/rejected)·라우팅 결정·인덱스 수용은 도메인 상태고, 커밋 사실은 owner git이 기록(author=owner). reeval은 ReevalStore 전이(ADR 0019)이지 audit이 아니다. 셋을 안 섞는다.

---

## Open Questions / 게이트 밖

- **`LlmAuthor` 추출 품질** — 개념 분할 입도·core_question 정확도·edges 과/소 연결(ADR 0029 OQ ①②③ 연장). 골든셋 eval(ADR 0003)·게이트 밖. T10.5(b) distill을 흡수하므로 그 품질 책임 지점이 `LlmAuthor`로 합쳐진다.
- **owner-로컬 repo 운영** — owner별 OKF repo의 초기화·GitHub 선택 백업 동기화·다중 카드 한 owner의 repo 구조(ADR 0018 owner별 repo 후속의 운영면). 게이트 밖·수동.
- **크로스머신 전파 지연·중복** — owner commit ↔ 중앙 index 수용 사이 지연(워커 부재·WS 끊김)에서 staleness가 더 새 것 멱등 흡수(ADR 0028 §14 결정 C)로 중복을 닫으나, 실 크로스머신 ordering·재연결 reeval 폭주 임계는 실 인프라 관측 후(게이트 밖).
- **reeval 인덱스-수용 훅의 과검출** — index 수용마다 그 agent_id 과거 판례를 reeval 큐에 올리면(agent_id 거친 매칭·ADR 0019 결정 2), 잦은 재배포가 reeval 노이즈를 낸다. ADR 0019 "놓침 0 > 과검출 0"·owner가 처리함에서 흡수. 정밀화(changed_paths 활용)는 후속.
- **T10.5 당기는 트리거** — `EmbeddingAnnMatcher`·임베딩 모델 선택(되돌리기 어려운 의존성)·실 RAG `ConfidenceAssessor`는 *스케일이 stage-1 토큰 오버랩 정밀도를 무너뜨릴 때*·*contested 볼륨이 stage-2 자동해소를 요구할 때* 당긴다(결정 5). 압박 관측이 트리거.
- **`SemanticOsAuthor` 확장점 패키징** — owner가 끼우는 별 패키지/플러그인의 entry-point·주입 규약(`AON_PROVIDER` 레지스트리 정신·ADR 0027 결정 11)은 owner 확장이 실제 요구될 때 닫는다(게이트 밖·owner측).
- **공급자별 OAuth 위임 비대칭(ADR 0027 결정 9 정정 정합)** — `LlmAuthor`가 "답변 런타임과 같은 인프라"라 함은 *포트·provider transport·owner 워커 경계*의 공유다(공급자 중립). 단 공급자별 자격 해석은 어댑터에 갇히고 비대칭이다 — Claude 구독 답은 `claude -p`(`ClaudeCodeRuntime`)가 공식·robust 경로(인프로세스 구독 OAuth는 ToS·rotation 문제로 불가·ADR 0027 결정 9 실 시연 정정), codex는 자기 CLI 토큰 파일로 인프로세스 성립. `LlmAuthor`의 실 transport 선택(공급자별 인프로세스 SDK vs `claude -p` 위임)은 ADR 0027 결정 9를 그대로 따른다(이 ADR이 재정의 안 함·게이트 밖 demo로 실제 통하는 쪽 확정).
