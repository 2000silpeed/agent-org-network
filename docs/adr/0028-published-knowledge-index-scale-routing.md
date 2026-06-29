# 스케일 라우팅 — published 지식 인덱스 + 2단 라우팅(중앙=목차·owner=내용)

상태: accepted (2026-06-27) · **§13 T10.3 통합 shape 확정(2026-06-27 — 결정 A~E)** · **§14 T10.4 publish 경로 shape 확정(2026-06-28 — 결정 A~F·`PublishIndex` 프레임·워커-소유자 스코핑·`generated_at` staleness·와이어 포맷 변경=되돌리기 어려움)** · **§15 on-demand 문서 fetch shape 확정(2026-06-28 — 결정 A~F·`FetchDocument`/`DocumentContent` 프레임 2개·동기 대기 correlation·요청 owner 자기 케이스 스코핑·워커 자기 카드만 읽기·중앙 중계만 저장 0·ADR 0017 결정 4 옵션 B-1 실체·양 union 동시 진화=되돌리기 어려움)** · **ADR 0029(OKF 자동 저작)가 앞단으로 추가됨(2026-06-29 — 이 ADR이 *소비*하는 OKF/`KnowledgeIndex`를 *생성*하는 저작 파이프라인·`build_knowledge_index_from_okf`[T10.1]·okf_index·`ConceptEdge`·`PublishIndex`[§14]를 재사용·이 ADR 결정 전부 무변경)** · **ADR 0030(owner측 저작 토폴로지·크로스머신 fan-out)이 §14 `accept_published_index`에 reeval 트리거 훅 추가(2026-06-30 — supersede 아님·확장)**: 더 새 `generated_at` 수용 시 `StalenessPropagator`(ADR 0019) 옵셔널 발화 → 그 agent_id 과거 판례 reeval. §14 결정 C(staleness·더 새 것만·동률/역행 거부) 무변경·*수용 성공*에 훅만 건다. §14 결정 E "OKF 변경 재배포 트리거(`OkfChangeEvent` 연동·후속)"를 크로스머신 전파 사슬(owner commit→reindex→publish→accept→reeval·OKF git owner-로컬·중앙 목차만)로 닫음. · 사용자 grill 합의 7개 결정 명문화 · **현 라우팅(Classifier→intent 1라벨→`card.domains` 정확매칭→Routed/Contested/Unowned)을 *refine***(대체 아님 — `RoutingDecision` sealed sum·Authority 중앙·Precedent·Contested 폴백은 그대로, 후보 *제안* 메커니즘만 인덱스 기반으로 정밀화) · **ADR 0017 결정 3②("실시간 충돌 자동해소")의 실체** · **PRD §5 "LLM 분류기·임베딩 유사도 정교화 — 포트만 두고 후순위"를 현금화** · ADR 0006(중앙 MCP)·0010/0027(owner측 실행·중앙 토큰 0)·0013(OKF)·0011/0012(WS 전송)·0019(staleness 패턴)·0004(Authority 중앙)·0015(intent 단일 출처)와 정합 · CONTEXT(신규 KnowledgeIndex·Concept·KnowledgeIndexMatcher·published index·stage-1/stage-2 용어)·TRD §2·§4·PRD §5 갱신 대상

## 맥락 — 현 라우팅의 스케일 결함

현 라우팅은 *질문 → intent 라벨 1개 → `card.domains` 정확매칭*이다(`router.py`·`classifier.py`):

1. `LlmClassifier.classify(question)`가 **전 카드 `domains`의 합집합**을 후보로 LLM 프롬프트에 싣고(`build_prompt`) 하나를 고른다(또는 어휘 밖이면 `""`).
2. `Router.route`가 그 intent를 가진 카드를 `intent in c.domains`로 정확매칭한다 → 0이면 Unowned, 1이면 Routed, ≥2면 Contested.

이 설계는 *에이전트 수 × 각 에이전트 지식 깊이*가 커지면 두 군데서 깨진다:

- **결함 ① 후보 폭발·정밀 붕괴(에이전트↑).** `build_prompt`가 *전 카드 domains 합집합*을 평평한 라벨 리스트로 LLM에 먹인다. 에이전트가 수백이면 후보 라벨이 수백 개 평평하게 펼쳐져 프롬프트가 폭발하고(토큰·비용), LLM이 그 중 하나를 정확히 고르는 정밀도가 무너진다. "전 개념을 LLM에 한 번에 먹이기"는 본질상 O(전 조직 지식)이라 스케일에 역행한다.
- **결함 ② 라벨이 답가능성을 표현 못 함(지식 깊이↑).** `domains`는 owner가 자기보고하는 *거친 주제 라벨*(예: `"가격"`·`"환불"`)이다. 한 에이전트의 지식이 깊어지면 그 안에 수십 개 세부 주제가 생기는데, 라벨 하나는 "이 에이전트가 *이 구체적 질문*에 답할 수 있나"를 표현하지 못한다. 라벨은 *담당 영역*은 가리키지만 *답가능성·세부 주제*는 못 가린다.

핵심 통찰: **라우팅 정밀도는 "에이전트가 *무엇을 아는가*"의 표현력에 달렸는데, 현 메커니즘은 그 표현을 owner 자기보고 라벨 1줄로 압축한다.** 표현력을 올리면서(지식 인덱스) 중앙 부담은 올리지 않는(목차만 보유) 구조가 필요하다.

이 ADR은 사용자 grill로 *이미 확정*(2026-06-27)한 7개 결정을 명문화한다. 재논쟁이 아니라 충실한 화해·shape·SSOT 정합이다.

---

## 결정 (사용자 grill 확정 2026-06-27)

### 1. published-index 라우팅 — 중앙=목차, owner=내용

각 에이전트(owner 환경)가 자기 지식의 **경량 인덱스**(*목차*·"내가 무엇을 아는가" — 내용 자체가 아니라 *목차*)를 생성해 **중앙에 자동 배포(publish)**한다. 중앙은 배포된 인덱스들의 합집합으로 라우팅한다 — 매 질문마다 전 owner에게 fan-out해 "너 이거 알아?"를 묻지 않는다(그건 O(에이전트 수) 왕복).

**핵심 불변 보존**: 중앙은 **목차(메타)만** 보유한다 — 지식 *내용*은 0이다. 그러므로 중앙 토큰 0·중앙 무지식·비소유(ADR 0006·0010·0017)는 **그대로 보존**된다(아래 §SSOT 화해 (a)). published 인덱스는 "이 에이전트가 *어떤 종류의 질문*에 답하는가"의 목록이지, 그 답의 *내용*이 아니다.

이는 현 `card.domains`(owner 자기보고 라벨 1줄)의 *자연스러운 확장*이다 — 라벨 1줄 → 개념 목차. 카드는 그대로 라우팅 *권한*메타로 남고(admission·Authority), 인덱스는 그 위에 *커버리지 신호*를 더한다(§5).

### 2. owner측 지식 엔진 = semantic-os(ontology + RAG), 레퍼런스 어댑터

실제 지식 생성·발췌·인덱스 도출은 **owner 환경**에서 일어난다 — 중앙이 아니다. 레퍼런스 구현은 **semantic-os**(`~/ai-projects/semantic-os` — RDF/OWL 다중도메인 온톨로지)다: 에이전트당 Layer-3 도메인 온톨로지(Named Graph) + 내용 RAG. semantic-os 개념 노드에서 인덱스(§4 distill 스키마)를 도출하고, stage-2(§6)의 깊은 RAG 신뢰도를 그 온톨로지·RAG로 접지한다.

**semantic-os는 *레퍼런스 어댑터*이지 코어 의존이 아니다.** 다른 owner는 더 가벼운 어댑터를 쓸 수 있다 — 예: **OKF 태그-인덱스 어댑터**(OKF 프론트매터 `type`·`title`·`description`·`tags`에서 곧장 인덱스 도출, 온톨로지·임베딩 없이). 코어가 RDF/OWL/임베딩에 묶이지 않게 하는 게 §3 포트의 목적이다.

### 3. 포트 신설 — `KnowledgeIndex` + `KnowledgeIndexMatcher`

`AgentRuntime`·`ProviderTransport`·`Classifier`·`OidcProvider`·`NotificationChannel`과 **같은 포트+어댑터 패턴**을 둔다. 코어는 RDF·임베딩·벡터 인프라에 *결합하지 않는다*. 중앙은 어떤 인덱스 빌더·어떤 매처에도 중립이다.

- **`KnowledgeIndex`**(값 객체) — owner가 배포하는 인덱스. distill 스키마(§4).
- **`KnowledgeIndexMatcher`**(Protocol) — 질문 + 배포된 인덱스들 → 후보 agent_id들. v1은 결정론 개념/키워드 오버랩, 스케일 어댑터는 로컬 임베딩 ANN(§7).
- **stage-2 신뢰도 포트** — 모호(≥2 후보)일 때 각 후보 owner가 *접지된 신뢰도*를 자기평가(§6).

`FakeMatcher`·`FakeIndex` 주입으로 결정론 경계를 둔다(`FakeClassifier`·`StubRuntime` 정신).

### 4. distill 스키마 — 배포되는 인덱스의 모양

라우팅 키는 semantic-os 개념 노드의 **`core_question` 필드**다 — "이 개념이 *어떤 질문에 답하는가*"(예: `core_question: "어떤 컴포넌트가 사용자 입력값을 캡처하나?"`). `domains` 라벨이 *주제*를 가리켰다면, `core_question`은 *답가능성*을 가리킨다(결함 ② 해소).

```
AgentKnowledgeIndex {
    agent_id:      str                     # 어느 카드의 인덱스(Registry 카드와 admission 대조)
    version:       str | int               # 또는 generated_at — staleness 판정(ADR 0012·0019 패턴)
    generated_at:  datetime
    concepts: [
        Concept { id, label, core_question, type? }   # 내용 0 · 목차만
    ]
    edges?: [ ConceptEdge ]                # 개념 관계 — 계층 좁히기·후속(MVP는 죽은 필드 허용)
}
```

중앙 stage-1 라우팅 = **질문 ↔ 전 에이전트 `concepts[].core_question` 합집합 매칭**. `concepts`는 *목차*다 — 각 항목은 "이런 질문에 답함"이라는 한 줄이지 그 답의 본문이 아니다(중앙 내용 0 보존). `edges`는 개념 계층을 좁히는 후속 자리(MVP는 싣되 매칭에 안 씀 — `OkfChangeEvent.changed_paths`가 죽은 필드인 정신, ADR 0019).

### 5. 불변식 화해 — 인덱스 = *신호*, Authority는 *여전히* 중앙

published 인덱스는 *지식 커버리지 신호*지 *권한 선언이 아니다*. 핵심 불변식 "권한(Authority)은 중앙(`routing_rules`)만 선언·카드 자기보고 금지"(ADR 0004)와 정면으로 화해한다:

- **인덱스 매칭은 후보를 *제안*만 한다.** Authority(`routing_rules`)·Contested·Precedent가 여전히 **게이트**다. 인덱스가 "이 에이전트가 안다"고 신호해도, 권한·다툼·판례가 최종 처분을 정한다.
- **admission-유사 재검증**: 에이전트가 publish한 개념은 *자기 권한(중앙이 선언한 owned domains) 안의 것만* 중앙이 수용한다. 권한 밖 개념을 publish해도 **라우팅되지 않는다**. 이는 카드 admission("유효하지 않은 카드는 등록되지 않는다")의 인덱스판 — 자기보고가 권한을 *넓힐* 수 없다(under-claim만 자기보고, ADR 0004).

즉 인덱스는 "내가 무엇을 *아는가*"(커버리지)이고, `routing_rules`는 "누가 무엇을 *맡는가*"(권한)다. 전자는 후자 안에서만 효력을 갖는다.

### 6. 2단 라우팅 + 모호 시 자동해소

**stage-1(중앙·인덱스 매칭)** — 질문 → 후보 agent_id들:

- **1 후보 → Routed**(현 단일 매칭 자리).
- **0 후보 → Unowned/escalation**(루트 User로 — 미아 없음 보존, 현 0매칭 자리).
- **≥2 후보 → 모호 → stage-2**(현 Contested 직행 대신 자동해소를 한 번 더 시도).

**stage-2(owner측 깊은 RAG·*모호한 ≥2 후보에게만*)** — 각 후보가 *접지된 신뢰도*(RAG 검색 점수 등 — 자유 자기주장이 아니라 owner RAG로 접지된 수치)로 self-assess → 중앙이 **최고 신뢰도로 자동 라우팅**. 그래도 동률·전부 낮음이면 기존 **Contested(사람 1인칭 합의) + Precedent** 폴백으로 떨어진다(미아 없음·기존 종착 보존).

**stage-2는 *권한 있는 후보들 사이의 tie-break*일 뿐**이다 — 권한을 새로 만들지 않는다(stage-1이 이미 §5 admission 재검증을 통과한 후보만 stage-2에 들어간다). 이것이 ADR 0017 결정 3②가 비전으로 둔 *"실시간 충돌 자동해소"*의 실체다 — Contested를 *항상* 사람에게 올리던 것을, 접지된 신뢰도로 *먼저* 자동해소하고 그래도 안 되면 사람에게.

stage-2는 fan-out이지만 *전 owner가 아니라 ≥2 모호 후보에게만*이라 O(모호 후보 수)로 묶인다(결정 ①이 매 질문 전 owner fan-out을 막은 것과 정합). owner측 깊은 RAG·신뢰도는 owner OAuth 멀티-LLM(ADR 0027) 워커가 자기 환경에서 수행한다(중앙 토큰 0 보존).

### 7. stage-1 매처 = 포트 + 계층

stage-1 매처는 §3 포트이고 *계층*(점진 정교화)을 갖는다:

- **v1 = 결정론 개념/키워드 오버랩.** 질문 토큰 ↔ `core_question`·개념 태그 토큰의 오버랩 매칭. **토큰 0**(LLM 호출 없음)·**게이트 결정론**(FakeMatcher 없이도 실 v1이 결정론)·**벡터 인프라 0**. 현 `intent in c.domains` 정확매칭의 자연스러운 후계(라벨 1개 정확매칭 → 개념 다수 오버랩).
- **스케일 어댑터 = 로컬 임베딩 ANN.** owner가 publish할 때 *자기 개념 벡터까지* 함께 배포 → 중앙은 *쿼리(질문)만* 로컬 모델로 임베딩해 ANN으로 top-K를 찾는다. **중앙 토큰 0**(로컬 임베딩 모델·외부 API 아님)·개념 벡터는 owner가 만들어 보냄(중앙은 인덱싱만). 필요 시 top-K에 한해 LLM 리랭크(top-K만이라 결함 ① 재발 안 함).
- **"전 개념을 LLM에 먹이기"는 기각.** 현 `build_prompt`가 전 domains 합집합을 LLM에 싣는 그 패턴이 결함 ①의 원인이라, 스케일 어댑터에서 *되살리지 않는다*. LLM은 (있더라도) top-K 리랭크에만 쓴다.

---

## SSOT 화해 (규칙 1 — 가장 중요)

TRD(`docs/trd-v0.md`)·CONTEXT가 강하게 못박은 두 명제와 이 ADR의 관계를 명시 화해한다. **둘 다 보존된다 — 깨지 않는다.**

### (a) "중앙은 RAG 인덱스로 안 든다 · 중앙 토큰 0 · 지식 비소유" — *보존*

> TRD §2·§4·CONTEXT(Agent Runtime·Knowledge Bundle 절): *"중앙은 지식의 소유자·진실 원천이 아니라 답변 시 최신을 읽을 뿐이고 RAG 인덱스로 안 든다·중앙 키/토큰 0."*

이 ADR은 이를 **보존**한다. 논거:

1. **중앙은 *목차(메타)*만 보유한다.** published `KnowledgeIndex`는 `concepts[].core_question`(한 줄짜리 "이런 질문에 답함") 목록이지 지식 *내용*이 아니다. "RAG 인덱스"가 금지된 것은 *내용을 모아 임베딩·검색하는 지식 코퍼스*인데, 우리 중앙이 드는 건 *목차*다 — 답의 본문은 0이다. 그러므로 "지식 RAG 인덱스 비보유·중앙 무지식·비소유"는 그대로다.
2. **stage-1 매칭은 로컬/결정론이다.** v1은 결정론 토큰 오버랩(토큰 0), 스케일 어댑터는 *로컬* 임베딩(쿼리만 임베딩·외부 모델 API 0). 중앙은 모델 토큰을 *0개* 보관한다(ADR 0010/0027 "중앙 키 0" 보존·강화).
3. **내용 RAG는 owner 환경에만 있다.** stage-2 깊은 RAG·신뢰도는 owner 워커가 자기 온톨로지·RAG로 수행한다(§2·§6). 중앙은 그 *수치*만 받지 코퍼스를 안 든다.

### (b) "벡터DB·RAG 0(답변 경로는 claude가 파일 읽음)" — *답변 경로*였고, 이 ADR은 *라우팅 경로*에 예외를 명시 선언

> TRD §4·CONTEXT(OKF·Agent Runtime 절): *"벡터DB·RAG 인프라 0 — Claude Code가 파일 읽는 에이전트라 cwd 주입+읽기 도구면 성립."*

이 "RAG 0" 명제는 **답변 경로**(answer path — `ClaudeCodeRuntime`/`ClaudeApiRuntime`이 OKF 번들을 *읽어* 답을 만드는 경로)에 관한 것이었다. 답을 만들 때 중앙이 벡터DB·임베딩을 안 든다는 뜻이지, *라우팅*(어느 에이전트로 보낼지)에 관한 게 아니었다.

이 ADR은 **라우팅 경로**(routing path — 어느 에이전트로 분기할지)에:
- owner측 ontology/RAG(stage-2 신뢰도 접지·§6), 그리고
- 중앙 로컬-임베딩 매처(stage-1 스케일 어댑터·§7)

를 도입한다. 그래서 **"라우팅용 RAG/임베딩은 예외"임을 명시적으로 선언한다** — "RAG 0"는 *답변 경로 한정* 명제이고, 라우팅 경로는 (a)의 제약(중앙은 목차만·내용 0·로컬 임베딩) 안에서 임베딩 매칭을 *옵션 어댑터로* 쓸 수 있다. 답변 경로의 "RAG 0"는 여전히 유효하다(이 ADR이 안 건드림 — 답은 여전히 claude가 OKF 파일을 읽어 만든다).

요약: **답변 경로 RAG 0(불변) · 라우팅 경로 RAG 예외(이 ADR이 명시 도입·단 중앙은 목차/로컬만).**

### 어느 기존 ADR을 refine/supersede 하는가

- **현 라우팅(Classifier→intent→`card.domains`)을 *refine*한다(supersede 아님).** `RoutingDecision` sealed sum(Routed/Contested/Unowned)·Authority 중앙·Precedent·Contested 사람 폴백은 **그대로 재사용**한다. 바뀌는 건 *후보 제안 메커니즘*뿐 — `intent in c.domains` 정확매칭 → `KnowledgeIndexMatcher` 개념 오버랩(stage-1) + 모호 시 stage-2 자동해소. 종착(0→Unowned·1→Routed·동률/실패→Contested)은 불변. **이 ADR을 어느 ADR의 supersede로 헤더에 박지 않는다** — 기존 라우팅 ADR을 *대체*하는 게 아니라 *위에 인덱스 층을 더하는* refine이다.
- **ADR 0015(intent 단일 출처) 정합.** 현 `RoutingDecision.intent`는 보존된다. 인덱스 매칭이 도입돼도 결정에 실리는 단일 라우팅 키는 여전히 하나다(매처가 고른 대표 개념/intent를 `intent` 자리에 실어 Precedent·ConflictCase·audit 색인을 그대로 쓴다 — 후속 정밀화 자리).
- **ADR 0017 결정 3②의 실체.** "정책 변경 시 자동 재검토"(0019)와 짝을 이루는 "실시간 충돌 자동해소"가 stage-2(§6)로 실체화된다 — 0017을 *재정의*가 아니라 *실현*한다.
- **ADR 0010/0027 정합(보존).** 중앙 토큰 0·owner측 실행은 위 (a)(b)로 보존. stage-2 owner RAG·인덱스 빌드는 owner OAuth 워커(0027)·OKF(0013) 위에서 돈다.

---

## 기존 도메인 재사용 (신규는 최소)

**재사용(무변경):**
- `RoutingDecision` sealed sum — Routed/Contested/Unowned. stage-1/stage-2 결과를 이 세 처분으로 투영(1→Routed·0→Unowned·동률/실패→Contested).
- `Precedent`·`PrecedentStore` — stage-2 자동해소 결과·사람 합의 결과를 판례로 학습(현 그대로). 인덱스가 stale일 때의 판례 staleness는 ADR 0019 패턴 재사용.
- `ConflictCase`·`ConsensusService` — stage-2가 자동해소 못 한 모호는 기존 Contested→ConflictCase→1인칭 합의로 떨어진다.
- `Authority`(`routing_rules`)·`Registry`·`AgentCard` — 권한 선언·admission·라우팅 메타. 인덱스 권한 재검증(§5)이 이 admission 정신 재사용.
- worker WS 전송(`transport.py`·`server.py`·`worker.py`·ADR 0011/0012/0026) — 인덱스 publish 프레임을 *이 채널 위에* 얹는다(새 채널 0).
- `RuntimeDispatcher`·owner 워커(ADR 0027) — stage-2 신뢰도 self-assess를 owner 워커가 수행.

**신규(이 ADR이 추가하는 것 전부):**
- `KnowledgeIndex`/`Concept` 값 객체(§4).
- `KnowledgeIndexMatcher` 포트 + stage-2 신뢰도 포트(§3·§7).
- 중앙 published-index 스토어(에이전트별 최신 인덱스 보관 — `PrecedentStore`·`SessionStore` 패턴 N번째).
- 인덱스 publish 프레임(WS 재사용 — 새 `Transport Frame` 변이 `PublishIndex`).
- stage-2 owner RAG 신뢰도(owner 워커 수행).

---

## 인덱스 갱신·배포 (결정 ①의 운영면)

- **워커 WS 채널 재사용**: owner 워커가 *연결 시* + *OKF/온톨로지 변경 시* 중앙으로 `PublishIndex` 프레임을 보낸다(워커→중앙 업스트림 — `RegisterWorker`·`SubmitAnswer`와 같은 봉투). OKF 커밋이 곧 변경 사건(ADR 0019 `OkfChangeEvent`)이라, 그 발화 지점에 인덱스 재배포를 옵셔널로 건다(`propagator`·`notifier` 옵셔널 주입 정신).
- **중앙은 에이전트별 최신만 보관**: published-index 스토어가 `agent_id → 최신 KnowledgeIndex`를 든다(`version`/`generated_at`로 더 새 것만 수용 — ADR 0012 staleness·ADR 0019 신선도 패턴). 옛 인덱스는 새 것으로 갈아끼운다(append-only history는 운영면 옵션).
- **publish 개념을 권한과 대조**(§5 admission 재검증): 수용 시 각 개념이 그 agent의 owned domains(중앙 선언) 안인지 검증 — 권한 밖 개념은 보관하되 라우팅 후보에서 제외(또는 거부). "유효하지 않은 카드는 등록되지 않는다"의 인덱스판.

---

## 포트 shape 제안 (코드 아님 · 미구현 통과 stub 수준 · 텍스트)

> tdd-engineer/mcp-runtime-engineer가 red→green으로 실체화한다. 아래는 *모양*이지 구현이 아니다.

**`Concept` / `KnowledgeIndex`**(frozen pydantic v2 값 객체 — `AgentCard` 정신):

```
class Concept(BaseModel, frozen=True):
    id: str
    label: str
    core_question: str            # 라우팅 키
    type: str | None = None       # OKF 프론트매터 type 등(선택)

class KnowledgeIndex(BaseModel, frozen=True):
    agent_id: str                 # Registry 카드와 admission 대조
    version: str                  # 또는 int — staleness 판정
    generated_at: datetime
    concepts: tuple[Concept, ...]
    edges: tuple[ConceptEdge, ...] = ()   # 죽은 필드 허용(후속 계층 좁히기)
```

**`KnowledgeIndexMatcher`**(Protocol — `Classifier`·`AgentRuntime` 포트 정신):

```
class IndexMatch(BaseModel, frozen=True):     # 또는 dataclass
    agent_id: str
    score: float                 # 결정론 오버랩 점수 또는 ANN 거리
    matched_concept_id: str      # 어느 개념이 걸렸나(intent 자리 후보)

class KnowledgeIndexMatcher(Protocol):
    def match(
        self, question: str, indexes: Sequence[KnowledgeIndex]
    ) -> tuple[IndexMatch, ...]: ...   # stage-1 후보(0·1·다수)
```

- **`ConceptOverlapMatcher`**(v1 결정론 어댑터) — 토큰 오버랩·LLM 0·벡터 0.
- **`EmbeddingAnnMatcher`**(스케일 어댑터·게이트 밖) — 로컬 임베딩 + ANN.
- **`FakeMatcher`**(테스트) — 고정 후보 반환(결정론 경계).

**stage-2 신뢰도 포트**(owner 워커 수행 — 접지된 self-assess):

```
class GroundedConfidence(BaseModel, frozen=True):
    agent_id: str
    confidence: float            # RAG 검색 점수 등으로 접지(자유 자기주장 아님)
    grounding: str = ""          # 근거 메모(운영면·노출 불변식상 사용자 미노출)

class ConfidenceAssessor(Protocol):    # owner측 — AgentRuntime 정신
    def assess(self, question: str, card: AgentCard) -> GroundedConfidence: ...
```

- **`FakeAssessor`**(테스트) — 고정 신뢰도(2단 라우팅 자동해소 로직을 결정론 단언).

**2단 라우팅 통합**(`Router` 또는 얇은 상위 — Fake 주입):

```
matches = matcher.match(question, store.all_indexes())     # stage-1
matches = [m for m in matches if authorized(m.agent_id, m.matched_concept_id)]  # §5 admission 재검증
if len(matches) == 0: -> Unowned(escalated_to=root)
elif len(matches) == 1: -> Routed(primary=...)             # _attach_gates 재사용
else:  # ≥2 모호 -> stage-2
    confs = [assessor.assess(question, card) for card in candidate_cards]
    winner = argmax(confs)
    if clear_winner(confs): -> Routed(primary=winner)      # 자동해소
    else: -> Contested(candidates=...)                     # 기존 사람 폴백
```

---

## 게이트 내/밖 경계

**게이트 내(결정론·`.venv` pytest로 잠금):**
- `KnowledgeIndex`·`Concept` 값 객체(frozen pydantic·admission 검증).
- `KnowledgeIndexMatcher` 포트 + `ConceptOverlapMatcher`(v1 결정론 토큰 오버랩) + `FakeMatcher`.
- 2단 라우팅 로직(stage-1 후보→admission 재검증→1/0/≥2 분기→stage-2 자동해소→RoutingDecision 투영) — `FakeMatcher`·`FakeAssessor` 주입 결정론.
- `PublishIndex` 프레임(pydantic DTO) + 중앙 published-index 스토어(InMemory) + 권한 대조 검증.
- `RoutingDecision` 통합(1→Routed·0→Unowned·동률/실패→Contested — 기존 sealed sum·`_attach_gates`·미아 없음 회귀).

**게이트 밖(수동·실 인프라·비결정):**
- 실 semantic-os 온톨로지 빌드·실 `core_question` distill(`SubprocessGitGateway`·실 OAuth 정신).
- 실 로컬 임베딩 ANN(`EmbeddingAnnMatcher` — 실 모델·새 의존성).
- 실 owner RAG 신뢰도(`ConfidenceAssessor` 실 구현 — owner 환경 RAG).
- 실 크로스머신 인덱스 배포(실 WS·실 워커 — `worker.py` 실 셸 정신).

---

## planner 넘김용 슬라이스 제안 (리스크 낮은 순)

> 상세 슬라이싱은 planner가 받는다. 게이트 내부터 의존성 순으로:

1. **`KnowledgeIndex`/`Concept` 값 객체** — frozen pydantic·admission 검증·`agent_id` 형식 재사용. self-contained 첫 진입.
2. **`KnowledgeIndexMatcher` 포트 + `ConceptOverlapMatcher`(오버랩) + `FakeMatcher`** — 결정론 매칭. (1) 위.
3. **2단 라우팅 통합** — stage-1→admission 재검증→1/0/≥2→stage-2(FakeAssessor)→RoutingDecision. (1)(2) 위·기존 `Router`/`RoutingDecision` 재사용·미아 없음 회귀.
4. **`PublishIndex` 프레임 + 중앙 스토어 + 권한 대조** — WS 봉투 재사용·InMemory 스토어·staleness 수용. (1) 위·기존 `transport.py` 재사용.
5. **`EmbeddingAnnMatcher`(스케일 어댑터)** — 게이트 밖(실 임베딩·새 의존성). (2) 위.

---

## 핵심 불변식 자체점검

- **미아 없음** — stage-1 0 후보 → Unowned/루트 escalation(현 0매칭 자리 보존). stage-2 자동해소 실패(동률·전부 낮음) → 기존 Contested→ConflictCase→1인칭 합의/Manager 큐(현 종착 보존). 어느 단계도 질문을 떨구지 않는다 — 모든 경로가 Routed·Unowned·Contested 중 하나로 종착.
- **Authority 중앙** — 인덱스는 *커버리지 신호*지 권한 선언이 아니다(§5). `routing_rules`·Contested·Precedent가 여전히 게이트. publish 개념은 owned domains(중앙 선언) 안의 것만 수용(admission 재검증) — 자기보고가 권한을 *넓힐* 수 없다(ADR 0004 보존). stage-2 신뢰도는 *권한 있는 후보 사이 tie-break*일 뿐 권한 생성 아님.
- **중앙 토큰 0 · 비소유** — 중앙은 *목차(메타)*만 보유(내용 0). stage-1은 로컬/결정론(v1 토큰 0·스케일 어댑터 로컬 임베딩·외부 모델 API 0). 내용 RAG·신뢰도는 owner 환경(§SSOT 화해 (a)). ADR 0010/0027 "중앙 키 0" 보존·강화.
- **전이 ≠ 기록** — 라우팅 결정(전이)은 `RoutingDecision` 도메인, 기록은 audit. 인덱스 publish(전이/배포)는 published-index 스토어 도메인이지 절차 로그가 아니다(`PrecedentStore`·`Work Queue`가 전이≠기록인 정신). 인덱스 스토어는 *최신 보관*이지 audit 아님.
- **노출 불변식** — stage-2 신뢰도·`grounding`·후보 목록·matched_concept은 *조직 내부값*이라 사용자向 `OrgReply`/`Answered`에 안 싣는다(audit·운영면만). 사용자는 담당·승인·출처만 본다(`intent`·confidence를 떨구는 현 투영 정신 그대로).
- **등록 무결성** — `KnowledgeIndex`의 `agent_id`는 Registry 카드와 대조(미등록 agent 인덱스 거부). 개념 권한 검증(§5)이 admission 정신. "유효하지 않은 인덱스는 라우팅에 들지 않는다"가 "유효하지 않은 카드는 등록되지 않는다"의 짝.

---

## 13. T10.3 통합 shape (domain-architect 확정 2026-06-27 — 결정 A~E)

> §6·§포트 shape가 *의사코드*로 열어둔 2개 도메인 결정(라우터 통합 전략·`authorized()` 권한 술어)을 닫는다. **구현 아님** — tdd-engineer 넘김용 *모양*. T10.2까지 green(게이트 1297 passed)인 코드를 읽고 확정. 핵심 사실: 현 코드에 `routing_rules.yaml` Authority 레이어는 *아직 없고*, 후보 게이트의 실질 권위는 admission 카드의 **`card.domains`**다(`router.route`가 `intent in c.domains and intent not in c.cannot_answer`로 후보 산출). 따라서 §5 "owned domains(중앙 선언) 안의 것만 수용"의 *현 구현체*는 `card.domains`다(ADR 0004상 `domains`는 under-claim 자기보고 — over-claim 차단 게이트로만 작동, 권한을 *넓힐* 수 없음).

### 결정 A — 새 `TwoStageRouter`를 기존 `Router`와 **공존**시킨다 (수정 아님)

기존 `Router`를 *수정하지 않고* 새 `TwoStageRouter`를 신설해 **공존**시킨다. 근거:

- **"refine(대체 아님)" 정합**(헤더·§어느 ADR refine/supersede). 기존 `Router.route`(classify→`intent in c.domains` 정확매칭→0/1/≥2)는 그대로 살아 있고, 인덱스 경로는 *별도 라우터*로 선택된다. 와이어 지점(`AskOrg`/`SessionAskOrg`)이 둘 중 하나를 주입받는다 — 어느 경로를 쓸지는 와이어 결정이지 라우터 내부 분기가 아니다.
- **기존 라우팅 테스트 무회귀가 제약**(불변식). `Router`를 *건드리지 않으면* 기존 테스트가 정의상 안 깨진다 — 가장 보수적. `Router`에 인덱스 분기를 끼워 넣으면 classify 경로·precedent 단축·`_attach_gates`가 모두 한 메서드에서 두 모드를 타게 돼 회귀 표면이 넓어진다.
- **재사용은 *위임*으로**. `TwoStageRouter`는 `RoutingDecision` sealed sum·`Unowned`/`Routed`/`Contested`·`Precedent` 단축경로·`_attach_gates`(approval_when/collaborate_when)를 **그대로 재사용**한다. `_attach_gates`·`_collaborators_for`는 현재 `Router`의 *private 메서드*라 — 두 라우터가 공유하려면 **모듈 수준 순수 함수로 추출**(`router.py` 내 `attach_gates(routed, intent, registry) -> Routed`)하고 `Router`는 그 함수에 위임한다. 이 추출은 `Router`의 *동작 무변경 리팩터*(같은 입력→같은 출력·기존 테스트 green 유지)다. 추출이 부담이면 대안으로 `TwoStageRouter`가 내부에 `Router` 인스턴스를 들고 게이트 부착만 위임받는다(둘 다 허용 — tdd-engineer가 red→green에서 더 작은 쪽 선택).
- **precedent 단축경로도 재사용**. `TwoStageRouter`도 `intent`(=대표 `concept.domain`, 결정 B/E)로 precedent를 lookup해 단축한다 — *단, intent는 stage-1 매칭 후에야 정해지므로* 순서가 다르다(현 `Router`는 classify→precedent, `TwoStageRouter`는 stage-1 매칭→대표 domain→precedent). precedent 단축은 stage-1 후보가 1+일 때만 의미 있다(0이면 Unowned 직행). MVP는 precedent 단축을 **stage-1 뒤·자동해소 앞**에 둔다(아래 의사코드).
  - **precedent primary도 `authorized()` 재검증 통과 필수 (code-review 보강·확정 2026-06-28).** 초기 구현은 `p.resolution.primary`를 권한 재검증 없이 곧장 Routed로 냈는데(레거시 `Router` 동작 상속), 이는 인덱스 경로의 "card.domains=권위·over-claim 차단" 명제를 precedent가 *우회*하게 한다(권한 박탈/over-claim 카드로 라우팅 가능). **확정**: 인덱스 경로에서 precedent 단축은 `대표 intent(concept.domain) ∈ primary_card.domains and ∉ cannot_answer`를 통과할 때만 발동하고, 미통과(또는 미등록)면 **단축을 건너뛰고 stage-1 권한통과 후보 투영으로 폴백**한다(미아 없음 보존 — authorized는 이미 1+). precedent 무효화(ADR 0019)는 *별개* 메커니즘이고, 권한 재검증은 그와 직교한 over-claim 차단이다. 레거시 `Router`는 이 강화를 *안* 받는다(별 경로·무수정) — over-claim 일관성은 인덱스 경로의 속성이다.

### 결정 B — `authorized()` = `concept.domain ∈ card.domains`, **`Concept.domain` 필드 추가**

§5 admission 재검증(over-claim 차단)을 구현하려면 *개념 → owned domains* 링크가 필요한데 현 `Concept`엔 domain 필드가 없다. **`Concept`에 `domain: str` 필드를 추가**한다(T10.1 소폭 수정). 권한 술어:

```
authorized(agent_id, concept) :=
    card = registry.get(agent_id)            # 미등록이면 제외(등록 무결성)
    concept.domain in card.domains           # over-claim 차단(권한 밖 개념 제외)
    and concept.domain not in card.cannot_answer   # 현 라우터 cannot_answer 정합
```

근거 — **`Concept.domain` 추가가 권한 술어와 intent 매핑을 *동시에* 푼다**:

- **권한(over-claim 차단)**: `concept.domain in card.domains`는 현 라우터의 `intent in c.domains` 권위 모델을 *그대로* 재사용한다. IT 에이전트가 "환불" 개념을 publish해도 `"환불" ∉ IT.domains`면 후보에서 빠진다 — 자기보고가 권한을 넓힐 수 없다(ADR 0004·§5 admission 재검증). `cannot_answer`도 현 라우터처럼 반영(`concept.domain in card.cannot_answer`면 제외).
- **intent 단일 출처(결정 E·ADR 0015)**: `RoutingDecision.intent = 매칭된 concept.domain`. precedent lookup(`intent → primary`)·`_attach_gates`(approval_when/collaborate_when가 *domain 단위*)·`ConflictCase.intent`·audit 색인이 *전부 domain 입도*라 그대로 동작한다. `matched_concept_id`는 인덱스마다 다르고 개념 단위라 너무 granular — precedent가 거의 재사용 안 되고 gate 매칭이 깨진다.
- **MVP-단순·정확**: 더 가벼운 매핑(개념 태그↔domains 오버랩·라벨 휴리스틱)은 *부정확*하다 — 어떤 태그가 어떤 domain에 속하는지 또 추론해야 하고 비결정 표면이 생긴다. `concept.domain`은 owner가 distill 시 **명시 선언**(개념을 어느 owned domain 아래 둘지)이라 결정론·정확하고, admission 재검증이 단순 집합 멤버십(`in`)으로 닫힌다. 추가 비용은 frozen 값객체 필드 1개(T10.1)뿐.

**`Concept.domain` 검증**: 빈 문자열/공백 거부(`id`·`core_question`과 동일 정신). domain 값 자체의 카드 owned-domains 일치 검증은 *publish 수용 시*(T10.4 권한 대조)·*라우팅 시*(T10.3 authorized)에 하지 admission(Concept 생성)에선 안 한다 — Concept은 카드를 모른다(값 객체 독립성).

### 결정 C — `PublishedIndexStore` 포트 (`all_indexes()`)

2단 라우터가 인덱스를 받는 출처는 `PublishedIndexStore` 포트다 — `PrecedentStore`·`SessionStore` 패턴 N번째(Protocol + InMemory).

```
class PublishedIndexStore(Protocol):
    def all_indexes(self) -> Sequence[KnowledgeIndex]: ...   # 에이전트별 최신 인덱스 합집합(stage-1 입력)
    def get(self, agent_id: str) -> KnowledgeIndex | None: ...  # 단건 조회(운영면·옵션)
    def put(self, index: KnowledgeIndex) -> None: ...           # 최신 수용(version/generated_at staleness — T10.4)
```

- T10.3은 `all_indexes()`만 *읽어* stage-1 매처에 먹인다(주입받음·구현 미가정). 실 InMemory 구현·`put` staleness 수용·권한 대조는 **T10.4** 책임(이 ADR §인덱스 갱신·배포). T10.3a는 포트의 `all_indexes()` 계약에만 의존하고 `FakePublishedIndexStore`(고정 인덱스 반환) 또는 인덱스 시퀀스 직접 주입으로 결정론 단언한다.
- `put`은 T10.3 미사용(read-only 경로)이지만 포트 shape를 한 번에 박아 T10.4가 같은 Protocol을 채우게 한다 — `PrecedentStore`가 record/lookup/invalidate를 한 Protocol에 둔 정신.

### 결정 D — stage-2 plug: `ConfidenceAssessor` 포트 + clear-winner 임계 *주입*

≥2 모호 후보 → stage-2 자동해소. 포트·값객체:

```
class GroundedConfidence(BaseModel, frozen=True):
    agent_id: str
    confidence: float            # owner RAG로 접지(자유 자기주장 아님)
    grounding: str = ""          # 근거 메모(노출 불변식상 사용자 미노출)

class ConfidenceAssessor(Protocol):    # owner측 — AgentRuntime 정신
    def assess(self, question: str, card: AgentCard) -> GroundedConfidence: ...

class FakeAssessor:                     # 테스트 더블 — agent_id→고정 confidence 주입
    ...
```

- **슬롯 위치**: stage-1이 ≥2 권한 통과 후보를 낸 *그 자리*(현 `Router`가 Contested를 직행하던 자리). T10.3a는 그 자리에서 곧장 Contested로 떨어뜨리고(자동해소 없음), T10.3b가 그 사이에 stage-2 자동해소를 *끼운다* — `TwoStageRouter`가 `assessor: ConfidenceAssessor | None = None`을 **옵셔널 주입**받아, None이면 T10.3a 동작(≥2→Contested), 주입되면 자동해소 시도. `precedents` 옵셔널 주입 정신 그대로(현 `Router.__init__`).
- **clear-winner 임계 주입**: 자동해소 vs Contested 폴백을 가르는 정책값은 `TwoStageRouter` 생성자에 **주입**(`clear_winner_margin: float` 또는 정책 함수). 카드/인덱스 자기보고가 아니라 중앙 라우터의 정책값(`DelegationSnapshot` staleness 임계 주입 정신). 게이트 내 결정론 단언은 임계를 주입해 검증(실 정책값은 ADR OQ ②·결정 대기).
- **자동해소 규칙(결정론)**: 후보들의 `assess` 결과 중 최고 confidence가 차순위와 `clear_winner_margin` *이상* 격차면 그 후보로 `Routed`(자동해소). 동률(격차 < margin)·전부 저신뢰(최고 confidence < 최소 임계 — 옵션 주입)면 기존 `Contested`. clear-winner 동률 tie-break는 `_collaborators_for` 정신(agent_id 오름차순)으로 결정론 고정.
- **불변식**: stage-2는 *권한 통과 후보 사이 tie-break*일 뿐 권한 생성 아님(§6). 신뢰도·grounding은 조직 내부값(노출 불변식). 중앙 토큰 0 — `assess`는 owner측(`FakeAssessor`는 게이트 내, 실 RAG는 T10.5 게이트 밖).

### 결정 E — `intent = 대표 concept.domain` (ADR 0015 정합 + 본문 정정)

`RoutingDecision.intent`에 싣는 대표 키는 **매칭된 `concept.domain`**이다(결정 B). ADR 0015 정합·정정:

- **정합**: ADR 0015가 보존하려던 *목적*은 "Precedent·ConflictCase·audit 색인이 그대로 동작"이다. `concept.domain`은 현 `intent`와 *같은 domain 입도*라 precedent lookup·`_attach_gates`·`ConflictCase`·audit이 무변경으로 돈다. `RoutingDecision`에 실리는 *단일 라우팅 키는 여전히 하나*(ADR 0015 핵심 명제)다.
- **본문 정정**: ADR 0015 본문/헤더가 예시로 든 "매처가 고른 대표 개념(`matched_concept_id`)을 `intent` 자리에"는 *후속 정밀화*로 열어둔 자리였다(ADR 0028 OQ ③ "대표 키 선정 규칙·MVP는 대표 1개·정밀화 후속"). T10.3에서 그 대표 키를 **`concept.domain`으로 확정**한다 — `matched_concept_id`보다 정합하다(domain 입도라 precedent 재사용·gate 매칭 보존). ADR 0015 헤더의 ADR 0028 정합 주석을 이 확정으로 갱신.
- **대표 선정 규칙**(다개념·다후보): stage-1이 후보당 1 `IndexMatch`(최고 점수 개념·T10.2 확정)를 내므로, *최종 처분 후보의* `matched_concept_id` → 그 concept의 `domain`이 대표 intent다. Routed면 primary 후보의 domain, Contested면 후보들의 대표(MVP: 최고 점수 후보의 domain·OQ ③ 후속 정밀화). precedent 단축경로는 stage-1 직후 대표 domain이 정해지면 그 domain으로 lookup.

### 불변식 보존 자체점검 (결정 A~E)

- **미아 없음**: stage-1 권한 통과 0 후보 → `Unowned(escalated_to=root)`. 자동해소 실패(동률·저신뢰)·assessor 미주입 ≥2 → `Contested`. 모든 경로가 Routed·Unowned·Contested 종착(기존 `Router` 종착 보존).
- **Authority 중앙**: 인덱스=신호(매처는 제안만)·권위는 `card.domains`(over-claim 차단)·stage-2는 tie-break(권한 생성 아님). 기존 `Router` 무수정이라 권위 모델 무변경.
- **중앙 토큰 0**: stage-1 결정론(`ConceptOverlapMatcher` 토큰 0)·stage-2는 owner측(`assess`). 라우터는 수치만 받음.
- **전이 ≠ 기록**: `TwoStageRouter`는 `RoutingDecision`(전이) 생성만·기록은 audit(ask_org). `PublishedIndexStore`는 최신 보관(전이≠기록).
- **노출 불변식**: `IndexMatch.score`·`matched_concept_id`·`GroundedConfidence.confidence`·`grounding`은 조직 내부값(사용자向 OrgReply 미노출). `RoutingDecision.intent`(=concept.domain)도 현 정신대로 미노출.
- **등록 무결성**: `authorized()`가 미등록 agent_id(`registry.get` KeyError) 제외·권한 밖 개념(`domain ∉ card.domains`) 제외. "유효하지 않은 인덱스는 라우팅에 안 든다".
- **기존 `Router` 무회귀**: `Router` 무수정(결정 A) — 기존 라우팅 테스트가 정의상 green. `_attach_gates` 추출은 동작 무변경 리팩터.

---

## 14. T10.4 publish 경로 shape (domain-architect 확정 2026-06-28 — 결정 A~F)

> §인덱스 갱신·배포(운영면)와 §13(라우팅 통합)이 *의사코드/연결점*으로 열어둔 **실 publish 경로**를 닫는다. **구현 아님** — mcp-runtime-engineer(전송·워커)·tdd-engineer(프레임 DTO·스토어 staleness·권한 결정론) 넘김용 *모양*. T10.3b까지 green인 코드(`transport.py`·`server.py`·`worker.py`·`two_stage_router.py`·`okf_index.py`)를 읽고 확정.
>
> **되돌리기 어려움(와이어 포맷 변경)**: 이 결정은 `WorkerFrame` sealed union을 진화시킨다(새 변이 `PublishIndex`). 한번 owner 워커들이 이 프레임을 송신하기 시작하면 *판별 필드·payload 모양*은 호환을 깨지 않고는 바꾸기 어렵다(배포된 워커 ↔ 중앙 무회귀). `_Frame`의 `extra="forbid"`가 미지 필드를 거부하므로 *추가*는 안전하나 *필드 제거/이름 변경*은 깨진다. ADR 0011/0012 와이어 프레임 진화와 같은 등급의 결정.
>
> **핵심 전환(비소유 강화)**: 현 라이브 슬라이스는 *중앙이 repo `okf/`를 직접 읽어* 인덱스를 시드한다(`demo.select_router`·데모 지름길). T10.4는 이를 **owner 워커가 자기 로컬 OKF에서 인덱스를 도출(`okf_index.build_knowledge_index_from_okf`)해 `PublishIndex`로 *배포*하고 중앙은 받아 보관만** 하는 경로로 바꾼다 — **중앙은 OKF 내용을 안 읽는다**. 이게 ADR 0006/0010 "중앙 비소유·목차만"을 *진짜로* 실현한다(데모 시드는 in-process 단축이었음).

### 결정 A — `PublishIndex` 프레임: `WorkerFrame` sealed union에 새 변이 추가

워커→중앙 업스트림 프레임 `WorkerFrame`(`transport.py:143`)에 **새 변이 `PublishIndex`**를 더한다. `RegisterWorker`/`SubmitAnswer`와 *같은 봉투 패턴*:

```
class PublishIndex(_Frame):
    type: Literal["publish_index"] = "publish_index"
    index: KnowledgeIndex          # frozen pydantic이라 중첩 직렬화 자연스러움

WorkerFrame = RegisterWorker | SubmitAnswer | Heartbeat | Ack | PublishIndex   # ← 변이 1개 추가
```

- **봉투 = `_Frame`(frozen·`extra="forbid"`) 상속·`type: Literal["publish_index"]` 판별.** `SubmitAnswer`가 `answer: AnswerFrame`(중첩 frozen DTO)을 싣듯, `PublishIndex`는 `index: KnowledgeIndex`(중첩 frozen 값객체)를 싣는다. `KnowledgeIndex`/`Concept`가 이미 frozen pydantic v2(`knowledge_index.py`)라 pydantic이 중첩 직렬화/역직렬화를 자동 처리한다 — 와이어 DTO를 따로 안 만든다(`AnswerFrame`처럼 도메인↔DTO 변환이 *불요*. 단 `generated_at: datetime`은 `model_dump(mode="json")`로 ISO 직렬화·`TicketFrame.enqueued_at` 정밀 동일).
  - **주의 — `_Frame` vs `KnowledgeIndex`의 `extra` 정책 차이**: `_Frame`은 `extra="forbid"`(미지 필드 거부)지만 `KnowledgeIndex`는 그 설정이 없다(`frozen=True`만). T10.4는 *프레임 봉투*만 `extra="forbid"`로 닫고, 중첩 `index`의 미지 필드 정책은 `KnowledgeIndex` 현 상태(허용)를 그대로 둔다 — 인덱스 스키마 진화(후속 `edges` 등 죽은 필드)에 여지를 남긴다. 봉투 무회귀만 보장하면 충분.
- **discriminated 직렬화/역직렬화 — 무회귀 끼움**: 중앙측 `server._parse_worker_frame`(`server.py:42`)·워커측 송신이 *추가 한 가지(elif 한 줄)*로 닫힌다. 현 파서는 `type` 문자열로 분기하는 *수동 판별*(pydantic `Field(discriminator=...)` 미사용·`if/elif` 체인):
  ```
  if frame_type == "register_worker": model = RegisterWorker
  elif frame_type == "submit_answer": model = SubmitAnswer
  elif frame_type == "publish_index": model = PublishIndex   # ← 추가 한 줄(무회귀)
  elif frame_type == "heartbeat":     model = Heartbeat
  elif frame_type == "ack":           model = Ack
  else: return None                                          # 미지는 그대로 None(와이어 안전)
  ```
  기존 5개 분기는 *문자열 키가 안 겹쳐* 무회귀다(`"publish_index"`는 새 키). `else: return None`(미지 프레임 무시)이 *구버전 중앙*에서도 미지 `publish_index`를 안전히 떨군다(전방 호환 — 워커가 먼저 신버전이어도 중앙이 깨지지 않음). 워커측은 `RegisterWorker`처럼 `model_dump_json()`으로 송신.
- **`Concept.domain`은 publish 와이어에 *반드시* 실린다**(결정 D 권한 대조의 키). T10.1에서 `Concept`에 `domain: str`이 이미 추가됐으므로(§13 결정 B·green) 별도 와이어 변경 없음 — `KnowledgeIndex`를 통째로 싣는 순간 `concept.domain`이 따라온다.

### 결정 B — 워커-소유자 스코핑(보안·핵심): 인증 owner ↔ index.agent_id의 card.owner 대조

**워커는 자기 인증된 owner가 *소유한* agent의 인덱스만 publish할 수 있다.** 다른 owner의 `agent_id`로 인덱스를 publish하는 사칭을 차단한다. 이게 "유효하지 않은 인덱스는 안 받는다"의 *publish 짝*(카드 admission의 인덱스판).

- **인증 owner의 출처 = 연결 세션**(`RegisterWorker`). `_handle_worker`(`server.py:110`)가 연결 직후 `owner_id = first.owner_id`로 *그 소켓의 인증 owner*를 잡는다(이미 존재). `PublishIndex`는 `SubmitAnswer`와 똑같이 그 *연결 귀속 owner*를 진실로 쓴다 — 프레임 안에 owner를 *다시 싣지 않는다*(소켓이 곧 그 owner, `TicketFrame.owner_id`를 생략하는 정신과 동일). 워커가 프레임에 owner를 자기보고할 여지를 안 준다.
- **스코핑 술어(중앙이 수용 전 검증)**:
  ```
  publishable(session_owner_id, index) :=
      card = registry.get(index.agent_id)        # 미등록 agent_id면 거부(등록 무결성)
      card.owner == session_owner_id             # 그 owner가 그 카드를 *소유*해야 함(사칭 차단)
  ```
  `card.owner`(중앙 선언·`registry`)와 *연결 세션의 인증 owner*가 일치할 때만 수용한다. 불일치(다른 owner agent)·미등록(`KeyError`)이면 **그 `PublishIndex`를 통째 거부**(보관 안 함). `RegisterWorker` admission(owner 신원 인증·6-5)이 *연결*을 닫고, 이 스코핑이 *그 연결이 무엇을 publish할 수 있나*를 닫는다 — 둘은 같은 owner 축의 두 게이트.
- **불변식(Authority 중앙)**: 어느 카드를 어느 owner가 소유하나는 *중앙(`registry`) 선언*이지 워커 자기보고가 아니다. 워커가 `index.agent_id`를 위조해도 `card.owner != session_owner`면 거부 — 자기보고가 소유 경계를 *넘을* 수 없다(ADR 0004·§5 admission 재검증의 owner 축).
- **한 owner가 여러 카드 소유**: owner는 자기 소유 카드 *여럿*에 대해 각각 `PublishIndex`를 보낼 수 있다(각 인덱스의 `agent_id`마다 `card.owner == session_owner` 검증). 한 연결로 여러 카드 인덱스를 배포하는 것은 허용 — 다른 owner 카드만 막는다.

### 결정 C — 스토어 `put` staleness: `generated_at` 키로 더 새 것만 수용

`InMemoryPublishedIndexStore.put`(`two_stage_router.py:114`, 현재 단순 덮어쓰기)을 **더 새 인덱스만 수용**하도록 닫는다. staleness 키 = **`generated_at`(datetime)** — `version`(str) 아님.

- **키 선택 근거(`generated_at` 우선)**: `version: str`은 *형식 자유*(예 `"okf-1"`·`"v2.3"`·커밋 SHA)라 *순서를 정의하지 못한다*(문자열 비교는 신선도와 무관). `generated_at: datetime`은 *자연 전순서*가 있어 "더 최신"이 명확하다(ADR 0012 `snapshot_at`·ADR 0019 신선도가 모두 datetime을 신선도 기준으로 쓰는 패턴 재사용). `version`은 운영면 식별·디스플레이 메타로 남기되 staleness 판정엔 안 쓴다.
- **수용 규칙(동률·역행 처리)**:
  ```
  put(index):
      existing = self._store.get(index.agent_id)
      if existing is None: store it                       # 첫 인덱스는 무조건 수용
      elif index.generated_at > existing.generated_at: replace   # 더 새 것만 교체
      else: reject (no-op)                                # 동률·역행은 거부(기존 보존)
  ```
  - **역행(`generated_at < existing`) → 거부**(no-op): 옛 인덱스가 늦게 도착(재연결·재전송)해도 최신을 덮지 않는다. ADR 0019 "옛 SHA = stale"·ADR 0012 "stale 거부" 정신.
  - **동률(`generated_at == existing`) → 거부**(no-op·멱등): 같은 인덱스 재도착(재연결 시 워커가 또 publish)을 흡수한다 — `SubmitAnswer` `ticket_id` 멱등·`PrecedentStore.invalidate` 멱등 정신. 동률을 *교체*로 두면 같은 인덱스를 무의미하게 다시 쓰고, *거부*로 두면 멱등이 명확하다. (같은 `generated_at`인데 내용이 다른 두 인덱스는 owner 도출 결정론 위반이라 가정 밖 — 결정론 `build_knowledge_index_from_okf`가 같은 OKF·같은 `generated_at`→같은 인덱스 보장.)
  - **per-agent 격리**: staleness는 *`agent_id`별 독립*이다(한 agent의 새 인덱스가 다른 agent 인덱스에 영향 0). `_store: dict[str, KnowledgeIndex]` 키가 `agent_id`라 자연 격리.
- **불변식(전이≠기록)**: 스토어는 *agent_id별 최신 보관*이지 audit이 아니다(append-only history는 운영면 옵션·MVP 미포함). 옛 인덱스는 갈아끼우고 버린다 — `PrecedentStore`/`Work Queue`가 "최신 상태 보관 ≠ 절차 로그"인 정신.

### 결정 D — 수용 시 권한 검증: over-claim concept 필터(저장 단계 admission)

`PublishIndex` 수용 시 각 concept의 `domain ∈ card.domains`(중앙 선언)인 것만 보관한다(over-claim concept 거부/필터). §5 admission 재검증의 *저장 단계 적용*.

- **필터 규칙(권장: publish에서 over-claim 필터)**:
  ```
  authorized_concepts := [
      c for c in index.concepts
      if c.domain in card.domains and c.domain not in card.cannot_answer
  ]
  ```
  over-claim concept(`domain ∉ card.domains`)·`cannot_answer` concept을 *떨궈낸* 인덱스를 보관한다. **전부 떨궈지면**(authorized 0개) 그래도 *빈 concepts 인덱스로 보관*한다(0 concept → 라우팅 0 후보로 자연 처리·미아 없음과 무관). 인덱스 자체를 거부하진 않는다 — *concept 단위* 필터(인덱스 단위 거부는 결정 B의 owner 사칭만).
  - **§13 `authorized()`와 *같은 규칙* 공유**: 라우팅 시 `TwoStageRouter.route`의 권한 재검증(`two_stage_router.py:202`)과 *동일 술어*(`domain in card.domains and domain not in card.cannot_answer`). 한 함수로 추출해 publish·라우팅 양쪽이 공유하게 한다(중복 정의 금지·단일 권위) — `attach_gates`를 모듈 함수로 뽑은 정신(§13 결정 A).
- **이중 게이트 — publish에서 걸러도 라우팅 authorized는 *방어적 잔존***: publish 시 over-claim을 거른 인덱스만 보관하지만, `TwoStageRouter.route`의 권한 재검증은 *그대로 둔다*. 근거 — ① 카드의 `domains`가 publish 이후 *축소*되면(owner under-claim 갱신·중앙 권한 변경) 보관된 인덱스에 이제 over-claim이 된 concept이 남을 수 있다(저장은 과거 카드 기준). ② `FakePublishedIndexStore`(권한 미검증 직접 주입) 테스트 경로가 라우팅에 들어올 수 있다. ③ 방어적 잔존 비용이 거의 0(이미 §13에서 green). **결론: publish가 1차 admission(저장 단계)·라우팅이 2차 방어(처분 단계). 둘 다 같은 술어를 공유**하므로 모순 0·이중 비용 미미.
- **불변식(Authority 중앙)**: `card.domains`(중앙 선언)가 권위. owner가 OKF에서 over-claim domain의 concept을 도출해 보내도 중앙이 저장 단계에서 떨군다 — 자기보고가 권한을 *넓힐* 수 없다(ADR 0004). publish는 under-claim(권한 안쪽 concept만)이 자연 통과·over-claim은 필터.

### 결정 E — 워커 publish 트리거: RegisterWorker 직후 + OKF 변경 재배포(후속)

워커가 `RegisterWorker`(연결·인증) *직후* 자기 OKF에서 인덱스를 빌드해 `PublishIndex`를 송신한다.

- **트리거 자리(연결 시)**: `WorkerLogic.register_frame`(`worker.py:155`)이 `RegisterWorker`를 만든 직후, 워커가 자기 소유 카드(`self._cards`)마다 `build_knowledge_index_from_okf(card, okf_root, generated_at=now)`로 인덱스를 도출해 `PublishIndex`로 송신한다. 실 송신 자리는 `run_worker`의 register 직후(`worker.py:226`·`Welcome` 수신 후)·게이트 밖(실 WS). 결정론 단위는 `WorkerLogic`에 `publish_frames() -> list[PublishIndex]`(자기 카드들→인덱스→프레임) 순수 메서드를 둬 FakeClock·고정 OKF로 단언(`handle_push_work` 정신).
  - **`generated_at` 출처**: 워커가 publish 시점의 시각(또는 OKF 최종 변경 시각)을 싣는다 — 결정 C staleness 키. 결정론 테스트는 주입 clock으로 고정.
- **OKF 변경 시 재배포(후속·게이트 밖)**: OKF 커밋이 곧 변경 사건(ADR 0019 `OkfChangeEvent`)이라, 그 발화 지점에 인덱스 재배포를 *옵셔널*로 건다(`propagator`/`notifier` 옵셔널 주입 정신). MVP/T10.4는 **연결 시 publish만** 닫고, OKF 변경 재배포는 *자리만*(후속). 재연결마다 다시 publish하므로 staleness(결정 C)가 중복을 멱등 흡수한다.
- **데모 시드 지름길 처분(`select_router`의 중앙 OKF 읽기)**: **정상 경로는 워커 publish**다. `demo.select_router`(`demo.py:196`)의 중앙 OKF 직접 읽기(`build_knowledge_index_from_okf`를 중앙에서 호출)는 ① **"워커 미연결 테스트용/라이브 시드"로 명시 격리**하거나 ② 워커 publish 실연이 닫히면 제거한다. **권장: 격리(즉시 제거 안 함)** — 게이트 내 결정론 테스트(`test_demo_router_flag`)와 워커 없는 라이브 데모가 빈 스토어로 깨지지 않게 *명시적으로 "시드(테스트/데모용)·실 경로는 워커 publish"* 주석을 박아 남긴다. `okf_index` 모듈 docstring의 "데모 지름길" 경계 명시를 그대로 잇는다. 실 크로스머신 워커 publish가 게이트 밖에서 닫히면 그때 시드 제거를 재검토(되돌리기 쉬운 후속 결정).

### 결정 F — 게이트 경계

**게이트 내(결정론·`.venv` pytest로 잠금):**
- `PublishIndex` 프레임 DTO 직렬화/역직렬화 왕복(`model_dump_json`↔`model_validate`·중첩 `KnowledgeIndex`·`generated_at` ISO 정밀).
- `WorkerFrame` union 파싱 무회귀(`_parse_worker_frame`이 기존 4종 + `publish_index`를 정확 판별·미지는 None).
- `InMemoryPublishedIndexStore.put` staleness(첫 수용·더 새 것 교체·동률 거부·역행 거부·per-agent 격리).
- 워커-소유자 스코핑 수용 검증(`card.owner == session_owner` 통과·불일치 거부·미등록 거부 — 결정 B).
- 수용 시 권한 검증(over-claim concept 필터·`cannot_answer` 제외·전부 떨궈지면 빈 concepts 보관 — 결정 D).
- `WorkerLogic.publish_frames()`(자기 카드들→인덱스→프레임·고정 OKF·주입 clock).

**게이트 밖(수동·실 인프라·비결정):**
- 실 WS 크로스머신 publish(실 owner 워커가 실 소켓으로 `PublishIndex` 송신·`run_worker`).
- 워커 connect→register→publish 실연(실 OKF·실 네트워크).
- OKF 변경 시 재배포(`OkfChangeEvent` 발화 연동·후속).

### 불변식 보존 자체점검 (결정 A~F)

- **중앙 토큰 0 · 비소유(강화)**: 중앙은 *목차(메타)*만 받아 보관(내용 0)·**이제 OKF를 안 읽는다**(워커가 도출·publish). 데모 시드(중앙 OKF 읽기)는 테스트/라이브 단축으로 격리. ADR 0006/0010 "중앙 비소유"를 진짜로 실현.
- **Authority 중앙**: `card.domains`·`card.owner`(중앙 `registry` 선언)가 권위. publish 수용 시 over-claim concept 필터(결정 D)·워커-소유자 스코핑(결정 B)이 자기보고가 권한·소유를 넓히는 것을 차단. 자기보고는 under-claim(권한 안쪽 concept)만 통과(ADR 0004).
- **등록 무결성**: 미등록 `agent_id` 인덱스 거부(결정 B `registry.get` KeyError)·타 owner agent 인덱스 거부(`card.owner != session_owner`). "유효하지 않은 인덱스는 안 받는다" = 카드 admission의 publish 짝.
- **미아 없음**: publish는 *라우팅 종착과 무관*(스토어 적재일 뿐). 빈 concepts·미수용 인덱스여도 라우팅은 stage-1 0 후보→Unowned(root)로 종착(§13 미아 없음 보존). publish 거부가 질문을 떨구지 않는다.
- **전이 ≠ 기록**: 스토어는 *agent_id별 최신 보관*(전이/배포 결과)이지 audit이 아니다. 옛 인덱스는 갈아끼우고 버린다(append-only history는 운영면 옵션).
- **노출 불변식**: `KnowledgeIndex`·`Concept`·`grounding`은 조직 내부값(사용자向 `OrgReply` 미노출). publish는 owner↔중앙 운영 채널이지 사용자 경로가 아니다.
- **기존 `WorkerFrame` 파싱 무회귀(제약)**: `PublishIndex`는 *추가 변이*(새 `type` 키)·기존 4종 분기 무변경·`else: return None`이 미지 프레임을 안전히 떨군다(전방 호환). `_Frame extra="forbid"`로 *추가*는 안전·*제거/이름변경*만 깨짐(되돌리기 어려움 명시).

---

## 15. on-demand 문서 fetch shape (domain-architect 확정 2026-06-28 — 결정 A~F)

> §14(publish 경로)가 *목차*(`KnowledgeIndex`)를 owner→중앙으로 흘려 중앙이 보관·라우팅하게 닫았다. 이 절은 그 **목차의 한 개념을 클릭하면 그 *내용*을 *그때* owner 워커에서 추출**하는 경로(on-demand 문서 fetch)를 닫는다. T10.4.5(슬라이스 1)가 처리함 다툼 케이스 후보별 *연관 개념*(`relevant_concepts` — core_question 목차)을 표시하고 각 항목에 `data-concept-id`·`data-agent-id`를 심어 *배선을 준비*해뒀다. 이 절은 그 클릭이 owner 워커에서 문서를 끌어와 인박스에 표시하는 **슬라이스 2**의 shape다. **구현 아님** — mcp-runtime-engineer(전송·워커·web 라우트)·tdd-engineer(프레임 DTO·correlation·워커 읽기·권한 결정론) 넘김용 *모양*. `transport.py`·`worker.py`·`server.py`·`web.py`·`okf_index.py`를 읽고 확정.
>
> **되돌리기 어려움(와이어 포맷 변경)**: 이 결정은 양방향 union을 *둘 다* 진화시킨다 — `CentralFrame`(중앙→워커)에 새 변이 `FetchDocument`, `WorkerFrame`(워커→중앙)에 새 변이 `DocumentContent`. §14 `PublishIndex`와 같은 등급의 결정 — 한번 배포되면 *판별 필드·payload 모양*은 호환을 깨지 않고는 바꾸기 어렵다(배포된 워커 ↔ 중앙 무회귀). `_Frame extra="forbid"`라 *필드 추가*는 안전·*제거/이름변경*은 깨진다. **두 union을 동시에 늘리는 첫 사례**(§14는 `WorkerFrame` 한쪽만)라 더 신중히 닫는다.
>
> **핵심 전환(비소유 보존·lazy)**: 중앙은 *목차만* 갖고(§14에서 확보), 내용은 **클릭 순간 owner에서 가져와 *중계*만**(저장 0). 슬라이스 1이 표시한 `core_question`은 목차(메타)이고, 이 절의 `DocumentContent.content`는 owner OKF 문서 본문이다 — **중앙은 그 본문을 *통과시킬* 뿐 보관하지 않는다**(목차만 유지). 이게 ADR 0017 **결정 4 옵션 B-1(분산 전송 = *사설 데이터 커넥터* 한정 옵션 — "그 담당이 데이터 *접근만* owner 쪽에 노출하고 중앙이 호출")** 의 실체에 가깝다. 차이: 0017 결정 4가 그린 옵션 B는 *사설 실시간 데이터·자격증명*(DB·메일·사내 API)이고, 여기 fetch는 *owner OKF 마크다운 문서*다 — 같은 "데이터 접근만 노출, 실행/조립은 중앙" 패턴이되 데이터 종류가 OKF(공유 가능 마크다운)라 옵션 B의 *경량판(B-1)*이다. **중앙 토큰 0 보존**: 이 경로는 LLM을 *안 부른다* — 워커가 파일을 읽어 반환하고 중앙은 중계만 한다(답 생성과 무관·순수 데이터 패스스루).

### 결정 A — 새 프레임 2개: `FetchDocument`(중앙→워커)·`DocumentContent`(워커→중앙)

양방향 union을 *각각* 한 변이씩 늘린다(§14 `PublishIndex` 봉투 패턴 재사용):

```
class FetchDocument(_Frame):                       # 중앙→워커 (CentralFrame 변이)
    type: Literal["fetch_document"] = "fetch_document"
    agent_id: str          # 어느 카드의 문서인가 (OKF 디렉터리 키)
    concept_id: str        # OKF 파일 stem (okf_index: concept.id = 파일 stem)
    request_id: str        # 요청/응답 correlation 키 (결정 B)

class DocumentContent(_Frame):                     # 워커→중앙 (WorkerFrame 변이)
    type: Literal["document_content"] = "document_content"
    request_id: str        # FetchDocument의 그 id (correlation)
    found: bool            # 파일이 있었나 (없으면 found=False·content="")
    content: str = ""      # OKF 문서 본문 (found=False면 빈 문자열)

CentralFrame = Welcome | AuthError | PushWork | Ping | FetchDocument   # ← 변이 1개 추가
WorkerFrame  = RegisterWorker | SubmitAnswer | PublishIndex | Heartbeat | Ack | DocumentContent  # ← 변이 1개 추가
```

- **봉투 = `_Frame`(frozen·`extra="forbid"`)·`type` 판별.** `FetchDocument`는 `PushWork`와 *같은 봉투*(중앙→워커·소켓이 곧 그 owner)·`DocumentContent`는 `SubmitAnswer`와 *같은 봉투*(워커→중앙·`request_id` 멱등 키 정신). `PublishIndex`처럼 중첩 값객체가 아니라 평평한 스칼라 필드(추가 DTO 불요).
- **`FetchDocument`에 `agent_id`를 *싣는다*(생략 안 함)**: `TicketFrame`이 `owner_id`를 생략하는 것(소켓이 곧 그 owner)과 *다르다* — 한 워커가 *여러 카드*를 소유할 수 있어(§14 결정 B "한 owner가 여러 카드 소유") 어느 카드의 문서인지를 워커가 알아야 `okf_root/{agent_id}/{concept_id}.md`로 읽는다. owner는 여전히 연결 귀속(프레임에 안 실음)·`agent_id`는 *그 owner 소유 카드 중 어느 것*인지의 선택자.
- **`request_id`는 correlation 키(결정 B)**: 중앙이 발급해 `FetchDocument`에 싣고 워커가 `DocumentContent`에 그대로 되싣는다(echo). 한 워커가 *연달아 여러 fetch*를 받을 수 있어(인박스에서 여러 개념 클릭) 응답을 요청과 짝지으려면 키가 필요하다 — `ticket_id`가 `PushWork`↔`SubmitAnswer`를 짝짓는 정신.
- **전방 호환(union 진화 무회귀)**: 두 파서(`parse_central_frame`[워커측]·`_parse_worker_frame`[중앙측])가 *각각 elif 한 줄* 추가로 닫힌다. 새 `type` 키(`"fetch_document"`·`"document_content"`)는 기존 키와 안 겹쳐 무회귀·`else: return None`이 *구버전*에서 미지 프레임을 안전히 떨군다(워커가 신버전인데 중앙이 구버전이어도, 그 역도 안 깨짐). **두 union 진화가 기존 프레임 파싱을 무회귀**임을 명시 — `Welcome`/`AuthError`/`PushWork`/`Ping` 분기·`RegisterWorker`/`SubmitAnswer`/`PublishIndex`/`Heartbeat`/`Ack` 분기 *전부 무변경*.

### 결정 B — 요청/응답 correlation: **동기 대기**(폴링 아님)

중앙이 `request_id`로 `FetchDocument`를 보내고 `DocumentContent`를 그 id로 매칭한다. web 요청이 결과를 받는 방식 = **동기 대기**(web 핸들러가 future/타임아웃까지 블록).

- **택1 근거(동기 vs async 폴링)**: `/ask`(`tracking` 발급 + `GET /ask/{tracking}` 폴 엔드포인트, ADR 0011 결정 6-5)는 **async 폴링**이다 — 답 생성이 LLM 호출이라 수십 초·작업 큐 도메인(단조 종착·timeout escalation)을 통과한다. **fetch는 다르다**: ① owner OKF *로컬 파일 1개 읽기*라 거의 즉시(LLM 호출 0·순수 데이터 패스스루) · ② 작업 큐 도메인과 *무관*(라우팅 종착·escalation 대상 아님 — 읽기 중계일 뿐) · ③ 클릭→내용 표시가 단일 UX 라운드라 폴 엔드포인트·tracking 발급의 비용이 이득 0. → **동기 대기 채택**(로컬 파일이라 빠름·UX·correlation 스토어 단순). async 폴링은 *명시 기각*(fetch는 큐 종착 무관·즉시성이라 폴링 인프라 과대).
- **correlation 스토어 shape**: `request_id → Future[DocumentContent]`(또는 동등한 1회용 슬롯). web 핸들러가 `request_id`를 발급하고 그 future를 등록한 뒤 `FetchDocument`를 워커로 디스패치하고 future를 await(타임아웃 동반). `recv_loop`가 `DocumentContent` 수신 시 `request_id`로 그 future를 *완료*시킨다(set_result). 디스패처가 *발급/대기/완료* 세 연산을 소유한다(`tracking → WorkTicket` 보관 정신의 N번째·단 이건 1회용·완료 후 정리). server.py의 send 콜백이 *동기*(`call_soon_threadsafe`)인 패턴과 정합 — fetch도 같은 outbound 큐로 `FetchDocument`를 내보내고 future로 응답을 기다린다.
- **타임아웃·응답 없음 처리(명시)**: future가 타임아웃 안에 완료 안 되면(워커 무응답·끊김) web는 **"추출 불가(담당 워커 응답 없음)"** 를 돌려준다(에러가 아니라 정상 degradation·결정 C와 같은 클래스). 타임아웃 슬롯은 정리(`request_id` 누수 방지·`poll`이 escalation 종착 시 라우팅 표식 정리하는 정신). 타임아웃 값은 정책(주입 — 로컬 파일이라 짧게·`staleness_threshold` 주입 정신).

### 결정 C — 워커 라우팅·오프라인: owner 워커에 push·미연결이면 우아한 degradation

중앙은 `FetchDocument`를 **`agent_id`의 owner에 연결된 워커**에게 보낸다(`WebSocketDispatcher`의 per-owner 연결 재사용).

- **라우팅 = card.owner → 연결**: 중앙이 `registry.get(agent_id).owner`로 그 카드의 owner를 찾고(중앙 선언·Authority 중앙), `_connections[owner_id]`의 연결(우선순위 primary→backup·`_select_connection` 정신 또는 단순 primary)로 `FetchDocument`를 push한다. fetch는 작업 큐 claim/submit과 무관(읽기 중계)이라 큐를 안 통과 — 연결 레지스트리만 재사용한다.
- **오프라인 = 우아한 degradation(에러 아님)**: 그 owner 워커가 **미연결이면** 중앙은 `FetchDocument`를 보낼 곳이 없다 → web가 **"추출 불가(담당 워커 미연결)"** 를 정상 응답한다(HTTP 200·degradation 메시지·예외 아님). 비소유의 자연스러운 결과 — *내용은 owner 환경에만* 있으므로 owner가 자리에 없으면 *지금* 못 가져온다(목차는 이미 published라 표시는 됨·내용만 lazy 실패). PRD §3 "모르면 안전하게 넘김"·결정 9 "위임 stale/부재면 거부"의 fetch판 — *조용히 깨지지 않고* 명시적 degradation 메시지로 표면화한다.

### 결정 D — 워커 핸들러: 자기 소유 카드 문서만 읽어 회신(사칭 차단)

워커가 `FetchDocument` 수신 → `okf_root/{agent_id}/{concept_id}.md`를 읽어 `DocumentContent`로 회신한다.

- **읽기 경로(okf_index 규칙 재사용)**: `concept.id = OKF 파일 stem`(`okf_index.py` 도출 규칙)이라 `okf_root/{agent_id}/{concept_id}.md`가 그 개념의 원본 문서다. 파일이 있으면 `DocumentContent(request_id, found=True, content=<본문>)`·없으면 `DocumentContent(request_id, found=False, content="")`. `publish_frames`가 OKF를 *읽는 정신*(워커측 도출·중앙은 안 읽음)과 같은 자리 — 단 이번엔 *목차 도출*이 아니라 *본문 반환*.
- **자기 소유 카드만(사칭 차단)**: 워커는 `self._cards`에 그 `agent_id`가 *있어야* 읽는다(`handle_push_work`의 카드 조회 정신). 없으면 `found=False`(또는 명시적 거부) — *남의 owner 카드 문서*를 owner 사칭으로 끌어내지 못하게(§14 결정 B 워커-소유자 스코핑의 fetch판·워커측 1차 방어). 중앙측 권한 스코핑(결정 E)과 *이중 게이트* — 워커가 자기 카드만 읽고, 중앙이 요청 owner를 자기 케이스 범위로 제한.
- **⚠️ `concept_id`·`agent_id` 경로 sanitization 필수(보안 불변식·code-review 발견 2026-06-28).** `concept_id`/`agent_id`는 **HTTP body 신뢰 불가 입력**이고 워커가 `okf_root/{agent_id}/{concept_id}.md`로 파일을 읽는다 — sanitization이 없으면 `concept_id="../finance_ops/pricing"`·`"/abs/path"`로 **경로 traversal**(다른 owner OKF·okf_root 바깥 유출)이 가능하다(own-cards 게이트는 `agent_id`만 봐 `concept_id` 축으로 우회·web 케이스-후보 게이트도 `concept_id` 미검증). 정상 UI는 `concept_id=concept.id=파일 stem`이라 안전하지만 *"정상값 안전 ≠ 입력 안전"*. **워커가 최종 신뢰 경계**: ① 화이트리스트(순수 파일명 컴포넌트 — 구분자·`..`·절대경로·숨김 거부, `_is_safe_path_component`) ② resolve 후 `okf_root/{agent_id}` 하위 확인(`is_relative_to` — 심볼릭링크·잔여 traversal 방어). 거부 시 `found=False`(degradation 일관). web 라우트는 1차 형식 검증(이중 방어)이되 워커가 권위. traversal 음성 테스트(공격 A 형제 owner·B okf_root 탈출·C 절대경로·D agent_id·E 심볼릭) 필수.
- **`found=False` content content="" 규약**: 파일 없음·미소유는 *예외가 아니라* `found=False`로 정상 회신한다(요청이 미아로 떠 있지 않게·web가 "문서 없음" 표시). `handle_push_work`가 미등록 agent_id를 폴백 답으로 종착시키는 정신.

### 결정 E — 비소유·권한 스코핑: 중계만·저장 0·요청 owner 자기 케이스 범위

중앙은 내용을 *중계*만(저장 0·목차만 유지)·권한은 요청 owner를 *자기 케이스 후보 문서*로 제한한다.

- **비소유(중계만·저장 0)**: 중앙은 `DocumentContent.content`를 web 응답으로 *통과시킬* 뿐 **어디에도 보관하지 않는다**(스토어·캐시 0). 다음 클릭에 다시 fetch(lazy 일관). published 인덱스 스토어(§14)는 *목차*만 갖고 본문은 안 갖는다 — 이 절이 그 *비소유*를 본문 경로에서도 지킨다(중앙 = 목차·owner = 내용 항상 유지).
- **요청 owner 스코핑(권한)**: web 요청의 **세션 owner가 *자기가 후보로 걸린 다툼 케이스의 후보 문서만* fetch** 가능하다. 임의 owner가 남의 OKF를 무단 열람하는 걸 차단 — 스코프를 *그 케이스의 후보*로 제한한다. 검증: `case_store`에서 그 `case_id`(또는 question)의 케이스를 찾아 ① 세션 owner가 그 케이스 후보인지(`concur` 스코프 "세션 owner가 후보 아니면 403"의 fetch판) ② `agent_id`가 그 케이스 후보 중 하나인지. 둘 다 통과해야 `FetchDocument`를 보낸다(불통이면 403). 인박스가 *owner 운영 면*이라 운영값 노출은 OK이되(serialize_case 내부값 노출 OK 정신) *자기 케이스 범위로 한정*한다.
- **두 권한 축(요청 측·읽기 측)**: ① *요청* 측 = 세션 owner 자기 케이스 후보로 제한(중앙·web 핸들러) · ② *읽기* 측 = 워커가 자기 소유 카드만(결정 D·워커). 두 축이 각각 막아 "남의 OKF를 owner 사칭으로도·자기 케이스 밖으로도" 못 끌어낸다.

### 결정 F — 게이트 경계

**게이트 내(결정론·`.venv` pytest로 잠금):**
- `FetchDocument`·`DocumentContent` 프레임 DTO 직렬화/역직렬화 왕복(`model_dump_json`↔`model_validate`).
- 두 union 파싱 무회귀(`parse_central_frame`이 `fetch_document` 추가·기존 4종 무변경·`_parse_worker_frame`이 `document_content` 추가·기존 5종 무변경·미지는 None).
- correlation 로직(`request_id` 발급·future 등록·`DocumentContent` 도착 시 매칭 완료·타임아웃 시 degradation·완료 후 슬롯 정리·멱등).
- 워커 문서 읽기(`okf_root/{agent_id}/{concept_id}.md`·tmp OKF·found True/False·자기 소유 카드만 읽기·미소유 found=False).
- 권한 스코핑(세션 owner 자기 케이스 후보 제한·비후보 403·`agent_id`가 케이스 후보인지).
- web 핸들러 처리 로직(가짜 fetch 디스패처 주입으로 fetch 라우트의 동기 대기·degradation 분기 결정론).

**게이트 밖(수동·실 인프라·비결정):**
- 실 WS fetch 왕복(실 owner 워커가 실 소켓으로 `FetchDocument` 수신·`DocumentContent` 송신).
- 워커 오프라인 실연(워커 끊고 클릭 → "담당 워커 미연결" degradation).
- 클릭→내용 표시 end-to-end(브라우저 인박스에서 개념 클릭 → 실 워커 OKF 본문 → 인박스 표시).

### 불변식 보존 자체점검 (결정 A~F)

- **중앙 토큰 0**: fetch는 LLM을 안 부른다(워커가 파일 읽어 반환·중앙 중계). 답 생성과 무관한 순수 데이터 패스스루.
- **비소유 보존(핵심)**: 중앙은 본문을 *중계만*·저장 0·목차만 유지(스토어·캐시 0·lazy). 중앙=목차·owner=내용이 본문 경로에서도 지켜진다(ADR 0006/0010/0017 비소유의 fetch판).
- **Authority 중앙**: 워커는 *자기 소유 카드 문서만* 읽고(결정 D·사칭 차단), 요청 owner는 *자기 케이스 후보 문서만* 가져온다(결정 E). 두 축 다 자기보고가 경계를 넘지 못함(§14 결정 B 워커-소유자 스코핑·`concur` 후보 스코프 재사용).
- **등록 무결성**: `registry.get(agent_id)`로 owner 라우팅(미등록 agent_id면 보낼 곳 없음·degradation).
- **미아 없음**: fetch는 *라우팅 종착과 무관*(읽기 중계일 뿐·작업 큐 안 통과). fetch 실패(미연결·미소유·파일 없음)가 *질문을 떨구지 않는다* — 어떤 질문도 fetch 때문에 미아가 안 된다(라우팅은 이미 §13/§14가 종착).
- **전이 ≠ 기록**: fetch는 *조회*(읽기 중계)이지 전이도 기록도 아니다. correlation 스토어는 1회용 future(완료 후 정리)이지 audit 아님.
- **노출 불변식**: fetch는 *owner 운영 면*(처리함·다툼 케이스 합의 보조)이지 사용자 경로(`OrgReply`)가 아니다. 본문 노출은 owner↔owner 운영 채널이고 *자기 케이스 범위로 한정*(결정 E).
- **기존 `CentralFrame`/`WorkerFrame` 파싱 무회귀(제약)**: 양 union에 *추가 변이*(새 `type` 키)·기존 분기 전부 무변경·`else: return None` 전방 호환. `_Frame extra="forbid"`로 *추가*는 안전·*제거/이름변경*만 깨짐(되돌리기 어려움 — 와이어 포맷 변경·§14와 같은 등급).

---

## Open Questions / 게이트 밖

- **`core_question` distill의 품질** — semantic-os 개념 노드 → `core_question` 도출이 라우팅 정밀도를 좌우한다. 골든셋 eval(ADR 0003)로 검증·게이트 밖(실 LLM/실 온톨로지).
- **`intent` 단일 출처와 다개념 매칭의 정합(ADR 0015)** — ~~대표 키 선정 규칙~~ **결정 13 §E에서 `concept.domain`으로 확정**. 다후보 Contested 시 대표 domain 선정(최고 점수 후보)의 정밀화만 후속.
- **stage-2 "clear winner" 임계** — 자동해소 vs Contested 폴백을 가르는 신뢰도 격차 임계(`DelegationSnapshot` staleness 임계 주입 정신 — 정책값 주입). 게이트 내 결정론 단언은 임계를 주입해 검증(결정 13 §D — `clear_winner_margin` 주입 확정·실 정책값은 결정 대기).
- **인덱스 staleness 전파** — OKF 변경 시 인덱스 재배포와 *그 인덱스에 기댄 과거 판례* staleness(ADR 0019)의 짝. ~~`put` staleness 키~~ **§14 결정 C에서 `generated_at`(datetime·더 새 것만 수용·동률/역행 거부)으로 확정**. OKF 변경 시 재배포 트리거(`OkfChangeEvent` 연동)·그에 기댄 과거 판례 재검토 정밀화는 후속(게이트 밖).
- **이름 충돌**(CONTEXT 명문화) — semantic-os의 "card/node"는 우리 "Agent Card"(권한·라우팅 메타)와 *다르다*. "OKF"=지식 내용, "KnowledgeIndex"=OKF/온톨로지에서 도출한 *목차*.
- **fan-out과의 관계** — stage-2의 ≥2 후보 평가는 *tie-break*(담당 1명)이지 fan-out(여러 담당 동시 답)이 아니다. fan-out은 Phase 9에서 명시 연기됨(`Routed.collaborators` 씨앗) — 이 ADR도 메시지당 담당 1명을 유지한다.
