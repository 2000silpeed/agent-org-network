# OKF 자동 저작("LLM wiki") — owner raw 자료를 staged 파이프라인으로 OKF 번들·인덱스로 자동 변환하되 owner 검증 게이트를 거친다

상태: accepted (2026-06-29) · **사용자 grill 확정 4개 결정 명문화**(① semantic-os 저작 방법론 재사용·OKF가 시스템 계약 · ② owner 검증 필수[변환 자동·publish 승인 게이트] · ③ staged 파이프라인 · ④ 증분 재인덱싱) · **ADR 0028(published 인덱스·publish 경로)이 *소비*한 OKF/KnowledgeIndex를 *생성*하는 앞단** — 기존 okf_index→`build_knowledge_index_from_okf`→`PublishIndex`(T10.4)에 재료를 공급 · **ADR 0013(OKF·카드/번들 분리) 확장**(저작 측 — OKF를 사람이 손으로 쓰던 것을 LLM 초안으로) · **ADR 0018(빌더 UI·OKF git 커밋) 위에 "자동 초안 채우기"를 얹음**(새 커밋 메커니즘 0·`GitGateway`·`commit_okf_bundle` 재사용) · **ADR 0025(HITL 초안→검토→확정) 정신 재사용**(자동 산출은 초안이고 owner 검토를 거쳐야 진실이 됨) · **ADR 0027(owner OAuth 멀티-LLM) 새 사용**(0027은 *답변* 경로였고 이 ADR은 그 owner LLM을 *저작*에 씀) · ADR 0019(`OkfChangeEvent`·변경 전파·증분의 짝)·ADR 0004(Authority 중앙·under-claim 자기보고)와 정합 · **현 라우팅·publish·fetch를 *대체하지 않고 앞단을 더하는 확장*(충돌 아님)** · CONTEXT(신규 OKF 자동 저작·`OkfAuthor` 포트·staged 변환·증분 재인덱싱 용어)·tasks(새 Phase 11 블록) 갱신 대상

---

## 맥락 — OKF를 *소비*는 하는데 *생성*은 사람 손이다

지금까지의 시스템은 owner의 OKF 번들을 *소비*한다:

- **답변 경로**: `ClaudeApiRuntime`/`ClaudeCodeRuntime`이 OKF 번들을 cwd로 읽어 답을 만든다(ADR 0013·0027).
- **라우팅 경로**: `build_knowledge_index_from_okf`가 OKF에서 `KnowledgeIndex`(개념·`core_question`)를 도출하고, 워커가 `PublishIndex`로 중앙에 배포한다(ADR 0028 §14·T10.4).
- **on-demand fetch**: 인박스 클릭 시 owner 워커가 OKF 문서 본문을 회신한다(ADR 0028 §15·T10.4.6).

이 세 경로가 모두 *이미 잘 구조화된 OKF*(마크다운+프론트매터·`type`·개념별 파일·`core_question`이 추출 가능한 형태)를 전제한다. 그런데 그 OKF를 **어떻게 만드나**는 비어 있다. ADR 0018의 빌더 UI는 owner가 *마크다운을 직접 써서* 커밋하는 경로만 닫았다 — owner가 가진 *기존 raw 자료*(문서·노트·위키·정책 PDF)를 OKF 번들로 *변환*하는 앞단이 없다.

이 ADR이 그 앞단 = **OKF 자동 저작("LLM wiki")**을 도메인·아키텍처로 확정한다. owner가 자기 raw 자료를 넣으면 → **LLM이 개념 분할·`core_question` 도출·개념 연결(edges)·인덱싱을 staged로 수행해 OKF 번들 초안 + KnowledgeIndex를 자동 생성** → owner가 검토·승인 → publish. 즉 ADR 0028이 *소비*한 OKF/인덱스를 *생성*해 그 파이프라인에 재료를 댄다.

이 ADR은 사용자 grill로 *이미 확정*(2026-06-29)한 4개 결정을 명문화한다. 재논쟁이 아니라 충실한 화해·shape·SSOT 정합이다.

---

## 결정 (사용자 grill 확정 2026-06-29)

### 1. semantic-os 저작 방법론 재사용 → OKF 생성, OKF가 시스템 계약

저작의 *방법론·에이전트 패턴*은 **semantic-os**(`~/ai-projects/semantic-os` — RDF/OWL 다중도메인 온톨로지, `seed_nodes`/`seed_edges` YAML 저작 파이프라인, 개념 노드에 *이미 `core_question` 필드*가 있고 seed_edges로 개념을 연결)에서 가져온다. semantic-os의 저작 흐름은 *ontology-scout(자료 정찰·개념 후보 발굴) → card-smith(개념 카드 작성) → ontology-engineer(개념 간 edges 연결·빌드)* 패턴이고, 우리 staged 파이프라인(결정 3)이 그 패턴을 그대로 본뜬다.

**그러나 영속 산출물은 OKF 마크다운 번들 + `KnowledgeIndex`다 — RDF 영속화를 강제하지 않는다.**

- **OKF가 시스템 계약**: 우리 시스템의 모든 소비 경로(답변·라우팅·fetch)는 *OKF 번들*과 *그에서 도출한 `KnowledgeIndex`*만 본다(ADR 0013·0028). 저작이 무엇으로 *추론*하든(semantic-os 온톨로지든 더 가벼운 휴리스틱이든), 최종 산출은 **OKF 번들 마크다운 + KnowledgeIndex**여야 한다. 이게 ADR 0028의 distill 입력(`build_knowledge_index_from_okf`)과 *완전히 정합*한다 — 저작은 그 입력을 만드는 것이다.
- **semantic-os full RDF/SPARQL 스택 = 깊이 원하는 owner의 옵션 백엔드**: RDF/OWL 온톨로지·SPARQL 쿼리·Named Graph 같은 무거운 스택은 *그 깊이를 원하는 owner*가 고를 수 있는 백엔드 옵션이다(ADR 0028 결정 2의 "semantic-os는 레퍼런스 어댑터이지 코어 의존 아님"의 저작판). 기본 owner는 OKF 마크다운만으로 충분하다.
- **코어는 RDF에 결합하지 않는다 — 포트 뒤**: 변환 엔진은 `OkfAuthor` 포트(아래 shape) 뒤에 가둔다. 코어는 LLM·semantic-os·RDF 중 무엇도 직접 import하지 않는다(`Classifier`·`AgentRuntime`·`KnowledgeIndexMatcher` 포트 정신). semantic-os RDF 어댑터는 그 포트의 *한 실 구현*일 뿐이다.

### 2. owner 검증 필수 — 변환은 자동, publish는 승인 게이트

LLM이 OKF *초안*을 자동 생성하지만, **검증 안 된 LLM 산출이 곧장 라우팅·답변의 진실이 되면 안 된다**(환각·오구조화 차단). 그래서:

- **변환은 자동**: 인제스천→개념 분할→`core_question` 도출→edges 연결→인덱싱이 LLM으로 자동 진행된다.
- **publish는 승인 게이트**: owner가 미리보기/diff로 검토·수정·승인 → **승인분만 commit·publish**된다. 승인 전 초안은 *staged* 상태로 머문다 — 소비 경로(답변·라우팅·fetch)에 닿지 않는다.

**ADR 0018에 "자동 초안 채우기"를 얹는다 + ADR 0025(HITL) 정신 재사용 — 새 기계 최소**:

- ADR 0018은 *빌더 UI가 owner 대신 OKF를 커밋*한다(owner는 git 몰라도 됨)까지 닫았다. 이 ADR은 그 *편집 대상(마크다운 본문)을 owner가 손으로 쓰던 것을 LLM 초안으로 미리 채운다* — 빌더의 커밋 메커니즘(`GitGateway`·`commit_okf_bundle`·`Answer.snapshot_sha`/`OkfChangeEvent` 발화)은 *그대로*이고, *입력을 자동 생성*만 더한다. 커밋되는 본체는 여전히 OKF 번들 마크다운이지 카드 YAML이 아니다(ADR 0018 결정 1 — 카드는 admission 경계라 검증→PR 유지).
- ADR 0025는 "LLM 초안 → owner 검토·수정·전송 vs 자동"을 *기존 `draft_only`/`full` 두 신뢰 상태의 런타임 토글*로 풀었다(새 도메인 기계 0). 이 ADR의 저작 HITL도 그 *정신*을 재사용한다 — 자동 산출은 *초안*이고, owner 검토(승인/수정/거부)를 거쳐야 *확정*(commit→publish)된다. ADR 0025가 *답*에 건 "초안→검토→확정"을 이 ADR은 *지식 저작*에 건다.

### 3. staged 파이프라인 — one-shot 기각

변환은 **단계별 파이프라인**이다(semantic-os card-smith[개념]·ontology-engineer[edges] 패턴):

```
1. 인제스천(Ingest)       raw 자료 수집·정규화(마크다운/텍스트 먼저·결정 "인제스천 형식")
2. 개념 분할(Split)        자료를 개념 단위로 쪼개 각 OKF 문서 초안 생성(card-smith)
3. core_question 도출      각 개념의 "이 개념이 어떤 질문에 답하나" 1줄(라우팅 키·ADR 0028 §4)
4. 연결(Link/edges)        개념 간 관계를 ConceptEdge로(ontology-engineer·전용 단계)
5. 인덱싱(Index)           OKF 번들 → KnowledgeIndex(okf_index 재사용·ADR 0028)
```

**one-shot(한 번의 LLM 호출로 raw→완성 OKF) 기각**:

- **컨텍스트 한계**: 큰 raw 자료를 한 프롬프트에 다 넣으면 토큰 폭발·후반부 누락.
- **단계별 검증 불가**: owner가 *개념 분할이 맞나*·*core_question이 정확한가*·*edges가 옳은가*를 따로 못 본다 — 한 덩어리로 나오면 어디가 틀렸는지 짚을 수 없다.
- **다개념 edges 오류**: 개념을 다 만들기 전에 edges를 같이 뽑으면 *아직 없는 개념을 가리키는* 엉뚱한 edge가 생긴다. edges는 개념이 다 선 *뒤* 전용 단계여야 한다(semantic-os가 seed_nodes 먼저·seed_edges 나중인 정신).

**단계별 owner 검토 가능**이 핵심 이득 — owner가 ②(개념 분할)·③(core_question)·④(edges) 각 단계의 diff를 따로 검토·수정·승인할 수 있다(결정 2의 HITL이 *단계마다* 건다·아래 shape "HITL/빌더 통합").

### 4. 증분 재인덱싱 — full 재빌드는 옵션

자료가 추가/변경되면 **그 부분만 2~5단계를 재처리**한다(full 재빌드는 옵션):

```
변경 감지 → 영향 개념만 재분할/재도출(2~3단계) → edges 재연결(4단계) → 재인덱싱(5단계)
```

- **변경 감지**: 어느 raw 자료가 추가/바뀌었나(파일 단위·`OkfChangeEvent.changed_paths` 정신·ADR 0019).
- **영향 개념 재처리**: 바뀐 자료에서 나온 개념만 2~3단계 재실행(전체 자료 재처리 안 함).
- **edges 재연결**: 재처리된 개념의 edges만 다시 잇는다(나머지 edges 보존).
- **재인덱싱**: 바뀐 개념의 인덱스 항목만 갱신 → `generated_at` 갱신 → `PublishIndex` 재배포(ADR 0028 §14 staleness가 더 새 것만 수용·동률/역행 거부로 멱등 흡수).
- **full 재빌드는 옵션**: owner가 "인덱싱 새로"를 요구하면 전 자료를 1~5단계 통째로 다시 돈다(증분이 기본·full은 명시 트리거).

증분은 ADR 0019의 변경 전파와 *짝*이다 — OKF 커밋(=`OkfChangeEvent`)이 ① 증분 재인덱싱(이 ADR·*미래* 인덱스 갱신)과 ② 그 지식에 기댄 *과거* 판례·답 재검토(ADR 0019)를 *동시에* 트리거한다.

---

## shape 결정 (grill에서 ADR로 위임)

> tdd-engineer/mcp-runtime-engineer가 red→green으로 실체화한다. 아래는 *모양*이지 구현이 아니다.

### S1. 포트 신설 — `OkfAuthor`(변환 엔진을 포트로)

변환 엔진을 `AgentRuntime`·`Classifier`·`KnowledgeIndexMatcher`와 **같은 포트+어댑터 패턴**으로 둔다. 코어는 LLM·semantic-os·RDF에 결합하지 않는다.

```python
# 한 저작 단계의 산출 = OKF 문서 초안 묶음(아직 staged·미커밋)
class OkfDraft(BaseModel, frozen=True):
    agent_id: str                       # 어느 카드의 번들인가(ADR 0018 okf/{agent_id}/)
    documents: tuple[OkfDocumentDraft, ...]   # 개념별 OKF 문서 초안
    edges: tuple[ConceptEdge, ...] = ()       # 4단계 산출(ADR 0028 ConceptEdge 재사용)

class OkfDocumentDraft(BaseModel, frozen=True):
    concept_id: str                     # OKF 파일 stem(okf_index 도출 규칙·ADR 0028 §15)
    title: str
    body: str                           # 마크다운 본문(프론트매터 제외 본체)
    core_question: str                  # 3단계 산출(라우팅 키·ADR 0028 §4)
    domain: str                         # 어느 owned domain 아래(ADR 0028 §13 결정 B Concept.domain)
    type: str | None = None             # OKF 프론트매터 type(자유·ADR 0013 — 어휘 미강제)

class OkfAuthor(Protocol):              # owner측 변환 엔진 — AgentRuntime 정신
    def split(self, sources: Sequence[RawSource]) -> tuple[OkfDocumentDraft, ...]: ...   # 2단계
    def derive_core_questions(self, drafts: tuple[OkfDocumentDraft, ...]) -> tuple[OkfDocumentDraft, ...]: ...  # 3단계
    def link(self, drafts: tuple[OkfDocumentDraft, ...]) -> tuple[ConceptEdge, ...]: ...  # 4단계
```

- **`FakeAuthor`(테스트) — 결정론 경계**: 고정 raw→고정 초안 매핑을 주입해 *staged 오케스트레이션·OKF admission 검증·인덱스 생성·HITL 상태기계·증분 diff*를 게이트 내 결정론으로 단언한다(`FakeClassifier`·`StubRuntime`·`FakeGitGateway` 정신). 실 LLM 추출은 게이트 밖.
- **실 어댑터(게이트 밖)**: `SemanticOsAuthor`(RDF/OWL 온톨로지 빌드·seed_nodes/seed_edges)·`LlmAuthor`(owner OAuth 멀티-LLM 직접 추출). 둘 다 같은 `OkfAuthor` 포트·다른 백엔드(`SubprocessGitGateway`·`HttpOidcProvider`가 같은 포트 실 어댑터인 정신).
- **단계가 메서드로 분리**(split/derive/link)된 이유: 결정 3의 staged 검토 — owner가 각 단계 산출을 따로 검토하려면 단계가 분리 호출 가능해야 한다(one-shot은 단일 메서드라 단계 검토 불가).

### S2. 비소유·중앙 토큰 0 (핵심 보존)

변환은 **owner 환경·owner LLM**에서 일어난다 — 중앙이 아니다.

- **owner LLM 새 사용(ADR 0027)**: 실 추출은 owner OAuth 멀티-LLM(ADR 0027)이 수행한다. **단 이건 *저작* 용도의 새 사용이다** — 0027은 *답변* 경로(대화 응답)에서 owner OAuth를 썼고, 이 ADR은 *같은 owner OAuth를 OKF 저작*에 쓴다(자격증명은 owner측·중앙 토큰 0 보존). owner 워커가 자기 OAuth 토큰으로 공급자 API를 불러 추출한다(ADR 0027 인프로세스 스트리밍 정신).
- **raw 자료·초안·LLM 호출이 모두 owner측**: owner의 기존 자료는 owner 환경에 있고, 변환 LLM 호출도 owner OAuth로 owner 환경에서 돌고, 산출 초안도 owner 환경(빌더 UI·git working tree)에 머문다. **중앙은 raw 자료를 보지 않는다.**
- **중앙은 published 인덱스만**(T10.4 보존·강화): owner가 검토·승인·커밋·publish한 *KnowledgeIndex 목차만* 중앙이 받는다(ADR 0028 §14 `PublishIndex`). 중앙은 raw 자료도·초안도·OKF 본문도·LLM 토큰도 0개 보관한다 — 이 ADR이 비소유·중앙 토큰 0을 *깨지 않고 강화*한다(저작 전체가 owner측이라 더 엄격).

### S3. HITL/빌더 통합 — 단계별 diff 검토

자동 초안 → owner 검토·승인 → git commit(ADR 0018) → publish(T10.4).

- **검토 단위 = 단계별 diff**(결정 3): owner가 ②(개념 분할)·③(core_question)·④(edges) 각 단계 산출을 *diff로* 검토한다(추가/수정/삭제된 개념·바뀐 core_question·새 edge). 단계마다 승인/수정/거부.
- **상태기계**(ADR 0025 정신·새 도메인 기계 최소): 각 단계 초안은 `staged`(미검토) → owner 처분(`approved`/`edited`/`rejected`). `approved`/`edited`분만 다음 단계로·최종 승인분만 `commit_okf_bundle`(ADR 0018)로 커밋되고 `OkfChangeEvent` 발화 → 증분 재인덱싱·`PublishIndex` 재배포. `rejected`는 버려진다(소비 경로에 안 닿음).
- **빌더 UI 통합점**: ADR 0018의 OKF 편집 면에 *"자동 초안 채우기"* 진입을 더한다 — owner가 raw 자료를 넣으면 staged 초안이 채워지고, owner가 단계별로 검토·수정해 커밋한다. 빌더의 커밋 author=owner 신원·스코프=card.owner(ADR 0018 결정 5)는 *그대로*.

### S4. 인제스천 형식 — MVP 범위

- **MVP = 마크다운/텍스트 먼저**: 가장 단순한 입력(마크다운·플레인 텍스트·붙여넣기)을 먼저 닫는다. 인제스천(1단계)은 raw 텍스트를 정규화해 `RawSource`로 만든다.
- **PDF/문서/위키는 후속**: PDF·docx·Confluence/Notion 같은 위키 추출은 *별도 인제스트 어댑터*(`Ingestor` 포트의 추가 구현)로 후속한다 — 인제스천을 포트로 두면 형식 추가가 어댑터 추가다(`NotificationChannel` 채널 중립 정신). MVP는 텍스트 어댑터 하나.

### S5. 증분 메커니즘 (결정 4의 shape)

- **변경 감지**: 어느 `RawSource`가 추가/바뀌었나를 식별(파일 단위·해시/타임스탬프·`OkfChangeEvent.changed_paths` 정신). 정밀 매칭은 후속(MVP는 거친 매칭 — ADR 0019 "놓침 0 > 과검출 0" 정신).
- **영향 개념 재처리**: 바뀐 source에서 나온 `OkfDocumentDraft`만 2~3단계 재실행.
- **edges 재연결**: 재처리된 개념의 edges만 다시 link(4단계). 나머지 edges 보존.
- **재인덱싱·staleness**: 바뀐 개념의 인덱스만 갱신 → `generated_at` 재사용(ADR 0028 §14 staleness 키·더 새 것만 수용·동률/역행 멱등 거부). full 재빌드는 옵션 트리거.

### S6. Authority 무변경

- 자동 생성 OKF도 **under-claim 자기보고**(ADR 0004)다 — OKF는 *답변 지식*이지 *권한 선언*이 아니다. owner가 어떤 OKF를 저작하든 권한은 중앙 선언(`card.domains`·routing_rules)이 정한다.
- **자동화가 권한을 넓히지 않는다**: LLM이 over-claim domain의 개념을 만들어도, publish 수용 시 `concept.domain ∈ card.domains` 검증(ADR 0028 §14 결정 D)이 떨군다. 저작 자동화는 *후보 생성*이지 *권한 생성*이 아니다 — stage-2가 "권한 통과 후보 사이 tie-break일 뿐"인 정신(ADR 0028 §6).

---

## SSOT 화해 (규칙 1 — 가장 중요)

이 ADR은 **저작 쪽 확장이지 충돌이 아니다 — 새 ADR감**이다. 기존 ADR들과의 관계를 명시한다.

### (a) ADR 0013(OKF·카드/번들 분리) — *확장*

0013은 OKF를 owner가 *손으로 쓰는* 마크다운 번들로 정의하고, 카드(라우팅 메타·중앙)와 번들(답변 지식·owner)을 분리했다. 이 ADR은 그 *번들을 LLM이 초안으로 채운다*는 저작 측을 더한다 — 카드/번들 분리·`type` 어휘 미강제(0013 결정 1)·번들이 owner 환경에 있음은 *그대로*다. 저작이 자동화돼도 산출은 여전히 OKF 마크다운 번들이고 카드는 흡수되지 않는다(0013 결정 2 보존).

### (b) ADR 0018(빌더 UI·OKF git 커밋) — *확장(위에 자동 초안을 얹음)*

0018은 빌더가 owner 대신 OKF를 커밋(`GitGateway`·`commit_okf_bundle`·author=owner)까지 닫았다. 이 ADR은 그 *편집 대상을 LLM 초안으로 미리 채운다* — 커밋 메커니즘은 무변경, 입력 자동 생성만 더한다. 커밋 본체가 OKF 번들 마크다운이지 카드 YAML이 아닌 것(0018 결정 1)도 보존. 0018 결정 6의 "커밋=`OkfChangeEvent` 발화"가 이 ADR 증분 재인덱싱의 트리거다.

### (c) ADR 0025(HITL 초안→검토→확정) — *정신 재사용*

0025는 "LLM 초안→owner 검토→확정"을 *기존 draft_only/full 토글*로 풀었다(답 경로·새 기계 0). 이 ADR은 그 *정신*을 저작에 재사용한다 — 자동 산출은 초안이고 owner 검토를 거쳐야 진실(커밋·publish)이 된다. 단 저작 HITL은 *답 mode 토글*과 다른 축이라(저작 단계별 staged 상태기계) 0025의 `hitl_to_mode`를 그대로 쓰진 않고 *패턴*을 본뜬다.

### (d) ADR 0027(owner OAuth 멀티-LLM) — *새 사용*

0027은 owner OAuth를 *답변 경로*(대화 응답)에 썼다. 이 ADR은 *같은 owner OAuth 인프라를 OKF 저작*에 쓰는 새 사용이다 — 자격증명이 owner측 OAuth라 중앙 토큰 0 보존(S2). 답변 transport(`ProviderTransport`)와 저작 추출이 같은 owner OAuth 세션을 공유할 수 있으나, 포트는 분리(`OkfAuthor` ≠ `AgentRuntime` — 답 생성과 지식 저작은 다른 책임).

### (e) ADR 0028(published 인덱스·publish 경로) — *재료 공급(앞단)*

0028은 OKF→`KnowledgeIndex`→`PublishIndex`를 *소비*했다(이미 OKF가 있다고 전제). 이 ADR은 그 *OKF를 생성*해 0028의 distill 입력(`build_knowledge_index_from_okf`)을 만든다. 0028의 모든 결정(중앙=목차·owner=내용·`concept.domain` 권한 검증·staleness)은 *그대로*이고, 이 ADR은 그 앞단에 저작 단계를 붙일 뿐이다. 5단계(인덱싱)는 0028의 okf_index를 *재사용*한다 — 새 인덱싱 메커니즘 0.

### 충돌인가 확장인가 — 판단

**확장이다(충돌 아님).** 근거: ① 새 산출물(OKF 자동 저작)은 기존 소비 경로의 *입력을 만드는* 것이지 소비 경로를 바꾸지 않는다. ② 비소유·중앙 토큰 0은 *보존·강화*된다(저작 전체가 owner측). ③ Authority 중앙은 무변경(자동 OKF도 under-claim·권한은 중앙). ④ 기존 ADR을 *supersede 하지 않는다* — 0013/0018/0025/0027/0028 위에 *저작 앞단*을 더하는 확장이라 헤더에 supersede 미표기.

### 비소유·토큰 0 보존 논거 (명시)

owner LLM을 *저작*에 쓰는 새 사용에도 비소유·중앙 토큰 0이 보존된다:

1. **raw 자료가 owner측**: owner의 기존 문서·노트·위키는 owner 환경에 있고 중앙에 올라가지 않는다.
2. **변환 LLM이 owner OAuth**: 추출 호출이 owner OAuth 토큰으로 owner 환경에서 돈다 — 중앙은 모델 토큰 0(ADR 0027 보존·강화).
3. **초안이 owner측 staged**: 미승인 초안은 owner 빌더/working tree에 머문다 — 중앙은 초안을 안 본다.
4. **중앙은 published 목차만**: 승인·커밋·publish된 `KnowledgeIndex`(목차·내용 0)만 중앙이 받는다(ADR 0028 §14). 저작 전 과정에서 중앙은 raw·초안·본문·토큰 어느 것도 0개 보관 — 비소유가 *저작 경로에서도* 성립한다(on-demand fetch가 본문 비소유를 지킨 정신의 저작판).

---

## 기존 도메인 재사용 (신규는 최소)

**재사용(무변경):**
- `OkfDocumentDraft.concept_id`·`domain`·`core_question`·`type` — ADR 0028 `Concept`·okf_index 도출 규칙(파일 stem=concept.id)과 정합. 저작이 그 규칙을 따르는 OKF를 만든다.
- `ConceptEdge`(ADR 0028 T10.1) — 4단계 산출. 새 edge 타입 0(MVP는 죽은 필드였던 것을 저작이 채우기 시작).
- `build_knowledge_index_from_okf`·`KnowledgeIndex`·`PublishIndex`·`PublishedIndexStore`(ADR 0028 T10.1·T10.4) — 5단계 인덱싱·배포. 새 인덱싱 0.
- `GitGateway`·`commit_okf_bundle`·`OkfChangeEvent`(ADR 0018·0019) — 승인분 커밋·변경 발화·증분 트리거. 새 커밋 메커니즘 0.
- owner OAuth 멀티-LLM·owner 워커(ADR 0027) — 실 추출 실행. 새 실행 인프라 0(저작 용도 새 사용).
- `card.owner`·`card.domains`(ADR 0004·0028 §14 결정 D) — 저작 스코프·publish 권한 검증. Authority 무변경.

**신규(이 ADR이 추가하는 것 전부):**
- `OkfAuthor` 포트 + `OkfDraft`/`OkfDocumentDraft`/`RawSource` 값 객체(S1).
- staged 오케스트레이션(1~5단계 순차·단계별 산출)·HITL 상태기계(staged→approved/edited/rejected·S3).
- `Ingestor` 포트(인제스천 형식·MVP 텍스트 어댑터·S4).
- 증분 diff 로직(변경 감지·영향 개념·edges 재연결·S5).
- `FakeAuthor`·`FakeIngestor`(테스트 더블·결정론 경계).

---

## 게이트 내/밖 경계

**게이트 내(결정론·`.venv` pytest로 잠금):**
- `OkfDraft`·`OkfDocumentDraft`·`RawSource` 값 객체(frozen pydantic·OKF admission 검증 — concept_id 파일 stem 규칙·core_question/domain 빈값 거부·ADR 0028 §15 경로 sanitization 정신).
- `OkfAuthor` 포트 + `FakeAuthor`(고정 매핑 주입).
- staged 오케스트레이션(1~5단계 순차·단계별 산출·`FakeAuthor` 주입 결정론).
- HITL 상태기계(staged→approved/edited/rejected 전이·승인분만 다음 단계·미아 없는 처분).
- OKF admission 검증(자동 산출 OKF가 okf_index 도출 규칙·publish 권한 검증 통과하는지).
- KnowledgeIndex 생성(저작 산출 OKF → `build_knowledge_index_from_okf` 재사용·ADR 0028).
- 증분 diff 로직(변경 감지·영향 개념 식별·edges 재연결·재인덱싱 트리거 — 결정론).

**게이트 밖(수동·실 인프라·비결정):**
- 실 LLM 추출(`LlmAuthor`/`SemanticOsAuthor` 실 owner OAuth 호출 — 비결정·골든셋 eval로 품질 검증·ADR 0003).
- 실 semantic-os 빌드(RDF/OWL 온톨로지·seed_nodes/seed_edges·SPARQL — 옵션 백엔드).
- 실 raw 자료(owner의 실 문서·PDF·위키 추출 — 인제스트 어댑터 실 본체).
- owner 검토 UI(빌더 UI의 자동 초안 채우기 면·단계별 diff 검토 조작 — ADR 0018 빌더 UI가 게이트 밖인 정신).
- 실 크로스머신 publish(저작→커밋→`PublishIndex` 실 WS — ADR 0028 §14 게이트 밖 정신).

---

## planner 넘김용 슬라이스 윤곽 (리스크 낮은 순)

> 상세 슬라이싱은 planner가 받는다. 게이트 내부터 의존성 순으로:

1. **`OkfDraft`/`OkfDocumentDraft`/`RawSource` 값 객체** — frozen pydantic·OKF admission 검증(concept_id 파일 stem·core_question/domain 빈값·경로 sanitization 재사용). self-contained 첫 진입·`Concept`(ADR 0028)와 정합 검증.
2. **`OkfAuthor` 포트 + `FakeAuthor` + staged 오케스트레이션(1~5단계)** — 단계별 산출·`FakeAuthor` 주입 결정론. (1) 위.
3. **HITL 상태기계 + OKF admission 검증** — staged→approved/edited/rejected·승인분만 커밋 경로(ADR 0018 `commit_okf_bundle` 재사용)·자동 OKF admission 통과 검증. (1)(2) 위·ADR 0018/0025 재사용.
4. **KnowledgeIndex 생성 합류** — 저작 산출 OKF → `build_knowledge_index_from_okf`(ADR 0028 재사용) → 승인분 `PublishIndex` 경로. (2)(3) 위.
5. **증분 diff 로직** — 변경 감지·영향 개념·edges 재연결·재인덱싱 트리거. (2)(4) 위·ADR 0019 정합.
6. **`Ingestor` 포트 + 텍스트 어댑터(MVP)** — 인제스천 형식·텍스트 먼저·PDF/위키 후속. (1) 위.
7. **실 어댑터(게이트 밖)** — `LlmAuthor`/`SemanticOsAuthor` 실 추출·실 인제스트·빌더 UI 자동 초안 면·실 크로스머신 publish. 마지막(실 인프라·비결정).

---

## 핵심 불변식 자체점검

- **미아 없음 — 무관(보존)**: 저작은 *지식 생성*이지 *질문 라우팅*이 아니다. 저작 실패(추출 실패·검토 거부)는 그 owner의 OKF가 안 채워질 뿐, 라우팅 종착(Routed/Unowned/Contested)을 안 건드린다. 저작 안 된 영역의 질문은 여전히 stage-1 0 후보→Unowned(root escalation·ADR 0028 §13)로 종착한다 — 저작은 미아 없음과 직교한다.
- **Authority 중앙(보존)**: 자동 생성 OKF도 under-claim 자기보고(ADR 0004)·권한은 중앙 선언(`card.domains`·routing_rules). LLM이 over-claim 개념을 만들어도 publish 수용 시 `concept.domain ∈ card.domains` 검증(ADR 0028 §14 결정 D)이 떨군다 — 자동화가 권한을 *넓힐 수 없다*(S6).
- **중앙 토큰 0 / 비소유(보존·강화)**: raw 자료·변환 LLM 호출·초안이 모두 owner측(owner OAuth·owner 환경), 중앙은 published 목차만 받는다(S2). 저작 전 과정에서 중앙은 raw·초안·본문·모델 토큰 0개 보관 — 비소유가 저작 경로에서도 성립(on-demand fetch 비소유의 저작판). ADR 0027 "중앙 키 0"을 저작에서도 보존·강화.
- **노출 불변식(무관)**: 저작은 *owner 운영 면*(빌더 UI·자기 OKF 저작)이지 사용자 경로(`OrgReply`)가 아니다. 초안·diff·검토는 owner↔owner 운영 채널이고 자기 카드 범위로 한정(빌더 스코프 card.owner·ADR 0018 결정 5).
- **등록 무결성(보존) — 유효하지 않은 OKF는 publish 안 됨**: 자동 산출 OKF도 okf_index 도출 규칙(concept.id=파일 stem·경로 sanitization·ADR 0028 §15)·publish 권한 검증(`concept.domain ∈ card.domains`·ADR 0028 §14)을 통과해야 인덱스로 배포된다. owner 미승인 staged 초안은 소비 경로(답변·라우팅·fetch)에 *닿지 않는다*(결정 2). "유효하지 않은 OKF는 publish 안 됨" = "유효하지 않은 카드는 등록 안 됨"의 저작판.
- **전이 ≠ 기록(보존)**: 저작 단계 전이(staged→approved/edited/rejected)는 도메인 상태고, 커밋 사실은 git이 기록한다(ADR 0018 — author=owner·커밋 이력). 둘을 섞지 않는다.

---

## Open Questions / 게이트 밖

- **개념 분할 품질** — LLM이 raw 자료를 *어떤 입도로* 개념 쪼개나(너무 잘게/너무 거칠게)가 라우팅 정밀도를 좌우한다. 골든셋 eval(ADR 0003)·게이트 밖(실 LLM).
- **core_question 도출 정확도** — 3단계 산출이 ADR 0028 라우팅 정밀도의 핵심(같은 OQ가 0028에도 있음 — 거기선 *소비*, 여기선 *생성*). 저작 단계가 그 품질의 책임 지점이 된다.
- **edges 추출 정확도** — 4단계가 *없는 개념을 가리키는 edge*·*과/소 연결*을 만들 위험. semantic-os ontology-engineer 검증 패턴 참조·owner 검토가 폴백.
- **증분 변경 감지 정밀도** — 어느 raw 변경이 어느 개념에 영향 주나의 정밀 매칭(MVP는 거친 매칭·ADR 0019 "놓침 0 > 과검출 0" 정신). 정밀화 후속.
- **인제스트 형식 확장** — PDF·docx·Confluence/Notion 위키 추출 어댑터(MVP는 텍스트·`Ingestor` 포트로 후속 추가).
- **`OkfAuthor` vs `Ingestor` 경계** — ~~인제스천(1단계)을 `OkfAuthor`에 흡수할지 별 포트로 둘지는 구현 시 shape 판단~~ **해소(T11.6·2026-06-30): 별 포트 확정**(인제스트≠변환·`ingest(items)→tuple[RawSource,...]` 1:1·source_id 호출자 제공[증분 매칭 키 안정성]·MVP `TextIngestor`·PDF/위키는 후속 어댑터·형식 추가가 어댑터 추가). pipeline 자동 연결 안 함(호출자 조립 seam).
- **저작 HITL 상태 영속** — staged 초안 상태를 in-memory로 둘지 store로 둘지(ADR 0025 "in-memory 토글 맵 MVP·요구 시 store 승격" 정신). 요구 관측 시 당기는 후속.
