# 지식 그래프 종합 비교 — TrustGraph · EKOS · Agent Org Network 최종 결정 (2026-07-06)

대상: [trustgraph-ai/trustgraph](https://github.com/trustgraph-ai/trustgraph)(외부 OSS) + EKOS(`~/ai-projects/Enterprise-knowledge-Operating-System`·사용자 자산). 질문: 우리(Agent Org Network)에 **반영할지 / 별도로 갈지 / 미채택할지**. 사용자 맥락: (1) "RAG 없이는 완전한 라우팅이 안 된다"는 경험치, (2) 좋은 레포라 탐색, (3) **지식 대규모 확장 가정**, (4) EKOS도 온톨로지 그래프 사상이라 함께 비교 후 **최종 결정**.

> **rev2 (2026-07-06)**: rev1(TrustGraph 실측)에 EKOS 3자 비교 + 최종 결정(§8·§9) 추가. rev1에서 초안 가정("답변이 컨텍스트 초과로 깨진다")을 실측으로 정정하고 첫 액션을 `assemble_context`로 확정.

> **정식 승격 (2026-07-06)**: 이 문서의 종합 판정(§9)이 정식 ADR로 승격됐다 — [**ADR 0036 — 지식 계층 전략: TrustGraph/EKOS 미채택·이중 표현 원칙·북극성**](adr/0036-knowledge-layer-strategy-dual-representation-and-north-star.md). 이 문서는 그 ADR의 근거 본체다(결정 5건의 실측·스파이크 실증). 되돌리기 어려운 전략 방향이라 사용자 명시 승인으로 박음.

> **첫 액션 shape 확정 (2026-07-06)**: ADR 0036 결정 5의 첫 코드 액션(다중 에이전트 접지·§3·§9-3의 D1→D2 정조준)이 [**ADR 0037 — Co-grounding(다중 접지): Authority primary 단일 귀속·포트 최소 진화·답+합의 병행**](adr/0037-co-grounding-multi-agent-knowledge-answer-and-consensus-parallel.md)로 도메인 shape가 확정됐다(`GroundingSet`·`GroundingSelector`·`grounding` 옵셔널 인자·Contested 답+합의 병행). 구현은 후속(tdd·mcp-runtime).

## 최종 결정 한눈에 (§9 상세)

- **AON 지금 = `assemble_context`(T9.1b) 완성** — 측정된 통증(경계 넘는 관계형 답변)의 정답. 외부 아님.
- **TrustGraph = 미채택·최후** — 경량 불가·2인 코어. 필요 시 알고리즘만 차용(자체 구현).
- **EKOS = 별도 유지·"빌릴 자매"** — as-is 통합 ❌(SAP 타입 소스 전제·산문 안 맞음), 패턴 3종(evidence+confidence·bitemporal·닫힌상위+열린subtype)은 흡수.
- **북극성 = OKF 저작을 타입화 사실로 진화**시키면 그때 그래프가 저작에서 공짜로 나오고 EKOS 엔진이 진짜 재사용 가능(제품 피벗급·연기).

## 0. 결론 요약 (실측 확정)

- **지금 TrustGraph 미채택.** 라우팅 병목은 지식 깊이가 아니었고(S11 커버리지 가설 기각), 답변 통증의 정답도 TrustGraph가 아니라 **우리 자신의 미구현 `assemble_context`(T9.1b)** 다.
- **답변 통증은 실재하나 성격이 다르다** — 컨텍스트 초과가 아니라(30배 여유) **"라우팅×단일 에이전트 접지"의 경계**. 관계형 질문(근거가 두 도메인에 갈림)이 이미 구조적으로 절반만 답된다. 첫 처방 = 다중 에이전트 접지(`assemble_context`), retrieval도 GraphRAG도 아님.
- **TrustGraph는 경량 사이드카가 불가**(Pulsar 하드 의존·풀스택만). 그러나 **GraphRAG 알고리즘은 단순해 자체 구현이 사이드카보다 우세**. 쓴다면 아이디어 차용이지 플랫폼 도입/별도 레포가 아니다.
- **에스컬레이션 사다리**(각 단계는 앞이 부족할 때만): ① `assemble_context`(지금·우리 것) → ② `KnowledgeRetriever` 포트+청크 retrieval(단일 에이전트 지식 비대해질 때·30배 뒤) → ③ 자체 경량 GraphRAG(관계 추론 복잡해질 때·TrustGraph 알고리즘 차용) → ④ TrustGraph 사이드카(최후·자체 구축이 운영상 더 비쌀 때·비현실적).

---

## 1. 두 시스템의 성격 대조

| 축 | Agent Org Network (우리) | TrustGraph |
|---|---|---|
| 본질 | **조직 질문 라우팅** — 담당·권한·판례·책임 | **지식 인프라** — GraphRAG·온톨로지·설명가능성 |
| 1급 도메인 | RoutingDecision·Precedent·Escalation·4대 불변식 | Context Core·Holon·GraphRAG 파이프라인 |
| 답변 접지 | OKF 번들 **전량 주입**(단일 에이전트·retrieval 없음) | DocumentRAG·GraphRAG·OntologyRAG |
| 스택 | Python·FastAPI·SQLite·fastembed(단일 머신·경량) | Cassandra·Qdrant·Pulsar·Garage(전부 필수·수 GB RAM) |
| 모듈성 | 포트+어댑터(교체 자유) | `trustgraph-base`부터 메시징 하드 의존·Flow 런타임 강결합 |
| 성숙도/리스크 | v0 사내 skeleton | 2.3k★·활발하나 **2인 코어·잦은 patch 릴리스(breaking 위험)·상용 오픈코어 의도** |

**핵심**: 겹치는 건 "조직 지식으로 LLM이 답한다"는 목적뿐. 우리는 *누구에게 물을지*(라우팅), TrustGraph는 *어떻게 깊게 답할지*(답변). TrustGraph엔 담당·권한·판례·미아 없음 같은 우리 1급 도메인이 아예 없다.

---

## 2. 우리 실측 ① — "지식 부족"은 라우팅 병목이 아니었다

스케일 트랙([scale-eval-2026-07-02.md](scale-eval-2026-07-02.md)) 최종: top-1 20.8%→**54.2%**, 오라우팅 15.3%→**1.4%**, contested 58.3%→**30.6%**.

지렛대는 매처(EmbeddingAnnMatcher 20.8→38.9%)·body 접지 어세서(→50.0%)·하이브리드 LLM 리랭크(→54.2%)였다. **결정적 음성 결과(S11):** "unowned의 태반은 지식 커버리지 문제" 가설로 실패 36건 전수 판정 → **커버리지 갭 1건뿐, 보강해도 수치 무변화.** 이 코퍼스에서 지식을 늘리는 건 라우팅 지렛대가 아니다.

**실패 모드 A/B/C 분해:**

| 모드 | 정의 | 근거 | 처방 |
|---|---|---|---|
| **A. 관계 부재** | 평면 목록으론 못 가르나 관계(상위/소속)면 갈림 | "8시간 근로→labor_std=social_insurance 동점" | 개념 관계(계층 가지치기) — **우리 인덱스 확장** |
| **B. 매칭 한계** | 더 나은 임베더면 갈림 | e5-large가 unowned -6.9%p | 임베더/리랭커 교체(포트만) |
| **C. 진짜 모호** | 사람도 두 팀 다 가능 — contested가 정답 | ambiguous 18문항·골든셋이 "사람 합의" 라벨 | **손 안 댐**(판례 흡수) |

잔여 contested 30.6%의 상당 부분이 **C(설계상 옳음)**. TrustGraph가 겨냥할 A조차 우리 인덱스 확장으로 충분하고 GraphRAG-답변과 무관하다. **→ 라우팅 축에서 TrustGraph는 우리 실측이 반증한다.**

---

## 3. 우리 실측 ② — 답변 통증의 진짜 정체 (초안 정정)

**⚠️ 초안 정정:** "대규모에서 컨텍스트 초과로 답변이 깨진다"는 **틀렸다.** 실측:

- **컨텍스트 예산 압박 없음.** okf_scale 에이전트당 본문 ~6.4k 토큰, 200k 윈도까지 **~30배 여유**. `_OKF_MAX_CHARS=100k`자 컷이 먼저 걸리는데 그것도 30배 뒤. retrieval의 근거가 **아니다**.
- **진짜 통증 = "라우팅 × 단일 에이전트 접지"의 경계 (이미 존재·구조적).** `answer()`는 라우팅된 **한 에이전트**의 본문만 주입한다(`provider_runtime.py:351` `_resolve_okf(card.agent_id)`). 다중 에이전트 접지 경로 없음(`assemble_context`는 T9.1b로 **미구현·항상 None**).
- **골든셋 실물 사례:** "환불 불가 조항이 있으면 청약철회도 안 되나요?" — 완전한 답은 청약철회권(consumer_protect)+조항 무효 법리(standard_terms) 두 축 종합 필요. **둘이 다른 에이전트에 흩어져** 라우팅은 한쪽만 보내고 나머지 절반은 컨텍스트에 **들어오지도 않는다.** ambiguous 18·contested 5문항이 이 유형.

**함의:** 전량 주입은 *한 에이전트 안*에선 종합을 잘한다. *경계를 넘는* 관계형 질문이 문제고, 이건 스케일이 아니라 **지금 있는 구조적 경계**다.

---

## 4. 축별 처방 적합성 (실측 확정)

| 축 | 통증 상태 | 첫 처방 | TrustGraph 적합성 |
|---|---|---|---|
| **라우팅 변별** | 대규모에서 압축 cosine 심화 | 계층 가지치기(우리 인덱스) | ✗ 무관 |
| **답변 — 경계 넘는 관계형** | **이미 실재**(단일 에이전트 접지) | **`assemble_context`**(다중 에이전트 접지·우리 백로그) | △ 먼 후보(자체 GraphRAG가 먼저) |
| **답변 — 단일 에이전트 지식 비대** | 없음(30배 뒤) | `KnowledgeRetriever`+청크 retrieval | ✗ retrieval이 오히려 관계 종합 악화 |
| **인프라** | — | — | ✗ 경량 불가(Pulsar 하드 의존) |

**retrieval(청크 임베딩)은 이 통증의 정답이 아니다** — top-k 컷이 흩어진 근거의 한쪽을 떨어뜨려 관계형 사례를 **악화**시킬 수 있다. 얻는 건 토큰↓뿐인데 토큰은 안 아프다.

---

## 5. TrustGraph 실측 — 뜯어 쓸 수 있나 (모듈성)

- **라이브러리 분리 불가.** `trustgraph-base`의 pyproject가 `pulsar-client`·`pika`·`confluent-kafka`를 하드 의존. GraphRAG 프로세서(`graph_rag.py`)가 Flow 런타임(=Pulsar)에 묶여 함수 호출식 import 불가. 아키텍처 문서: *"Pulsar underpins **all** system communication."*
- **경량 프로파일 없음.** Cassandra+Qdrant+Garage+Pulsar 전부 필수. 단일 머신 docker-compose는 되나 "경량"이 아니라 **무거운 풀스택 1식**(JVM·Cassandra·Pulsar 상시·수 GB RAM 추정).
- **그러나 GraphRAG 알고리즘은 단순(재구현 가능).** 소스 직접 열람 결과 5단계: ① LLM으로 질의→개념 분해 ② 개념 임베딩→벡터로 seed 엔티티 ③ **hop-and-filter 2-hop 순회**(엣지를 cross-encoder로 스코어링·hop당 25개 컷) ④ 출처 추적 ⑤ 서브그래프→LLM 합성. 파라미터 기본값도 소스에 노출(entity_limit=50·max_path_length=2·edge_limit=25). **우리 fastembed로 2·3단계 재활용 가능.**
- **안정 REST API 존재** — `POST /api/v1/flow/{flow}/graph-rag` `{query}`→`{response}`. 블랙박스 사이드카는 기술적으로 가능.
- **라이선스 Apache 2.0** — 차용·참조·사이드카 모두 안전(고지 보존만).
- **리스크**: 2인 코어(버스팩터)·v2.6 patch 러시(breaking 위험)·상용 오픈코어 의도(핵심 폐쇄 가능성).

---

## 6. 세 경로 + 에스컬레이션 사다리

| 경로 | 판정 | 근거 |
|---|---|---|
| **A. 우리 레포 통합** | ✗ 기각 | 인프라 4종·Pulsar·원칙(단일머신·SQLite·경량) 붕괴·라우팅 도메인 중복 |
| **B. 별도 사이드카(TrustGraph 그대로)** | ✗ 사실상 최후 | 경량 불가·풀스택 운영·2인 버스팩터·breaking 추종. 자체 구축이 더 쌈 |
| **C. 아이디어 차용 자체 구현** | ○ **우세** | GraphRAG 5단계 단순·fastembed 보유·스택 일관·리스크 회피. 추정 2~4주(그래프 추출 파이프라인 제외 — 우리 OKF는 이미 구조화라 부담 작음) |

**"별도 레포냐"의 답**: TrustGraph를 *쓴다면* 별도(사이드카)가 맞지만, **쓸 필요가 없다.** 우리가 만들 GraphRAG는 별도 레포가 아니라 *우리 `KnowledgeRetriever` 포트 뒤의 자체 어댑터*다.

**에스컬레이션 사다리 (각 단계는 앞이 부족함이 실측될 때만):**

1. **지금 = `assemble_context`(T9.1b) 완성.** 관계형/contested 질문에서 인접 에이전트 문서도 함께 접지. **측정된 통증(단일 에이전트 접지)을 정조준하는 우리 백로그.** 이게 첫 액션. — domain-architect·tdd-engineer
2. **단일 에이전트 지식이 비대해지면(30배 뒤) = `KnowledgeRetriever` 포트 + 청크 retrieval**(fastembed 재사용·인프라 0). 지금은 불요.
3. **관계 추론이 나이브 다중 접지로 안 되면 = 자체 경량 GraphRAG.** TrustGraph 5단계 알고리즘 차용, 우리 포트 뒤 자체 구현(cross-encoder는 LLM 스코어링으로 대체 가능). 2~4주.
4. **최후 = TrustGraph 사이드카(HTTP 어댑터).** 자체 GraphRAG 구축이 그들 풀스택 운영보다 비쌀 때만 — 우리 규모엔 비현실적.

**공통 선결**: 어느 단계든 `KnowledgeRetriever`(또는 접지 입력) 포트 추상화를 먼저 세우면 1↔2↔3↔4 교체가 자유롭다. 단 **첫 실제 코드 액션은 포트가 아니라 `assemble_context`** — 그게 지금 아픈 곳이니까.

---

## 7. 라우팅 대규모 별개 트랙

답변과 독립적으로, 개념 100배 시 압축 cosine 필드에서 절대-τ 게이트가 단일 승자를 못 고르는 구조적 한계(S8)가 심해진다. 처방 = **KnowledgeIndex 관계 확장**(팀→도메인→에이전트 계층 가지치기). TrustGraph 아이디어 차용·인프라 0. 대규모 라우팅 신호 관측 시 착수.

## 8. EKOS 비교 — 사용자 자매 프로젝트 (2026-07-06 추가)

`~/ai-projects/Enterprise-knowledge-Operating-System/`. **사용자 소유·온톨로지 그래프 지식 구축.** TrustGraph와 근본적으로 다른 위치.

**EKOS 정체 (조사 근거):** SAP 엔터프라이즈 지식을 **결정론적·evidence-backed·bitemporal 그래프**로 변환. 닫힌 상위 온톨로지(9 노드/9 엣지 Core: Object·Action·Event…/HAS_PART·TRIGGERS·READS·WRITES…) + 열린 Pack subtype(OWL `subClassOf`·SHACL 검증). **LLM-free 그래프 구축**(정적 파싱+실행 관측·hallucination 0)·자연키 IRI 자동 dedup·모든 사실 ≥1 evidence+confidence+valid_from/to. Python 3.12·pytest·pyshacl·545 테스트·PoC(synthetic 벤치만).

### 3자 대조

| 축 | TrustGraph | **EKOS** | AON (우리) |
|---|---|---|---|
| 소유 | 외부 OSS | **사용자 자산** | 사용자 자산 |
| 그래프 구축 | LLM 추출(환각 위험) | **정적 파싱(결정론·환각 0)** | 없음(OKF 산문·cwd-read) |
| 기질(substrate) | 임의 텍스트 | **타입화 소스(SAP·코드·GUI)** | 조직 정책 산문 |
| 관계 종류 | 유동/커뮤니티 | **고정 9종·기술적**(READS/WRITES/TRIGGERS) | 없음(라우팅 메타만) |
| 스택 | Cassandra+Pulsar+K8s(무거움) | **Python+pyshacl(경량·우리와 한 가족)** | Python+SQLite+fastembed |
| evidence | 출처 포인터 | **필수+confidence+bitemporal** | Answer Record/origin |
| 성숙도 | 2.3k★·2인 코어 | 545 테스트·PoC·synthetic | v0 skeleton |
| 사상 정합 | 낮음 | **높음**(evidence-first·append-only·DDD+TDD·서브에이전트 팀) | — |

### 핵심 판정 — EKOS도 우리 통증을 as-is로 못 푼다 (표면 유사에 속지 말 것)

EKOS의 강점(결정론 그래프)은 **타입화 소스**(ABAP·DDIC·GUI 세션)에서 나온다. 파서가 코드/실행을 읽는다. **우리 지식은 OKF 마크다운 산문**(환불정책·근로 조문)이라:
1. EKOS 파서(ABAP 토크나이저·RFC·GUI)가 **적용 안 됨.** 산문에서 관계 그래프를 뽑으려면 결국 **LLM 추출이 필요한데, 그건 EKOS가 의도적으로 안 하는 것**(환각 회피가 EKOS의 존재 이유).
2. EKOS 관계 9종은 **기술적 인과**(READS/WRITES/TRIGGERS)다. 우리 관계 통증은 **법적/정책적**(이 조항은 저 법 위반으로 무효·이 환불정책↔저 청약철회권)이라 EKOS Core 어휘가 아니다. 새 Pack + 산문 추출이 필요 = 다시 LLM.
3. 즉 "EKOS를 우리 지식 계층으로" = SAP 모양 온톨로지를 조직 정책에 강제 + LLM 추출 재도입. **표면은 닮았지만 기질·관계·구축법이 다르다.**

**그러나 EKOS의 사상/패턴은 TrustGraph보다 훨씬 빌릴 가치가 있다** (우리 자산·한 가족):
- **evidence+confidence 필수** → 우리 Answer Record/출처·신뢰(책임)에 정확히 매핑. 강화 가치.
- **bitemporal(valid_from/to)** → 우리 Correction Event·staleness(ADR 0019 reeval)의 원리적 상위판.
- **닫힌 상위 온톨로지 + 열린 subtype** → 우리 KnowledgeIndex 관계 확장(라우팅 계층 가지치기 §7)의 정확한 설계 패턴.

---

## 9. 최종 결정

**AON의 측정된 통증(경계 넘는 관계형 답변)은 외부 플랫폼도 EKOS도 아닌 우리 자신의 `assemble_context`가 첫 정답이다.** 세 시스템의 관계를 이렇게 확정한다:

1. **AON 근시 액션 = `assemble_context`(T9.1b) 완성.** 변함없음. 측정된 통증 정조준. (§6 사다리 1단계)
2. **TrustGraph = 미채택·최후 수단.** 외부·무거움·경량 불가·2인 버스팩터. 필요해지면 플랫폼이 아니라 GraphRAG *알고리즘만* 차용해 우리 포트 뒤 자체 구현. (§6 사다리 3~4)
3. **EKOS = 별도 유지가 옳다. 단 "빌릴 자매"다.** as-is 통합 ❌(기질·관계·구축법 상이). 대신 **검증된 패턴 3종을 AON에 흡수**: (a) Answer Record에 evidence+confidence 강화, (b) reeval을 bitemporal 방향으로 상향, (c) KnowledgeIndex 관계에 닫힌-상위+열린-subtype 패턴 적용.
4. **북극성(전략 옵션·태스크 아님):** 지식 규모·관계형 답변 통증이 커지면 원리적 경로는 TrustGraph도 EKOS-import도 아니라 **OKF 저작을 "타입화 evidence-backed 사실"로 진화**(ADR 0029/0030 저작면 확장)시키는 것. 그러면 산문 대신 저작 단계에서 결정론 그래프가 나오고, 라우팅·답변이 공짜로 그래프를 얻으며, **그때 비로소 EKOS 엔진이 진짜 재사용 가능**해진다. 제품 피벗급 결정이라 지금은 연기하되, 그래프 지식이 중심이 되면 이게 방향이다.

### 9-1. 설계 원칙 — 하나의 지식, 두 얼굴 (사용자 확정)

**사람에겐 직관, 시스템엔 정확 — 산문과 타입화 그래프를 evidence로 결속한 하나의 지식.** 규칙:
- **산문 = 진실 원천**(사람 축·owner가 저작/검토). **타입화 그래프 = 파생**(시스템 축·독립 저작 금지·산문보다 많을 수 없음=안전 경계). **evidence 포인터 = 결속**(각 사실이 원문 span을 가리킴 — 감사+사람↔시스템 결속+환각 방어 삼역).
- owner는 구조화 사실을 직접 못 쓴다(현실) → **자동 변환(LLM 추출) 필수**. 나이브 추출은 TrustGraph의 환각. 해독제 = **owner 검토(HITL) + evidence 접지**(우리가 이미 가진 것·Phase 11 `LlmAuthor` 패턴의 출력 진화). 세 시스템 화해: TrustGraph(추출·근거X=환각)·EKOS(파서·산문X)·우리(추출+검토+근거=산문 가능+감사 가능).

### 9-2. 디리스크 스파이크 결과 (2026-07-06·실 claude·GO 조건부)

실 정책 산문 5문서 + cross-doc 2쌍을 실 LLM으로 타입화 사실+관계+span근거 추출(스크래치패드 산출·프로덕션 무변경). **판정: 북극성 최난 기술 내기 검증됨.**
- **근거 접지 100%**(span 46/46 원문 exact·환각 0·닫힌 어휘 위반 0) — "evidence 결속으로 결정론 대체" 원리 작동.
- **검토가능성** — 사실이 원문 충실 압축·근거 나란히 = *체크*지 *다시쓰기* 아님.
- **cross-doc COMPLEMENTS 포착**(핵심) — 환불 쌍 "청약철회권 관점 ↔ 조항무효 관점"을 양쪽 원문 span 접지로 하나의 엣지로. **측정된 통증(경계 넘는 관계형)을 그래프 엣지로 실물 포착.**
- **다리 발견**: 추출된 COMPLEMENTS 엣지가 곧 `assemble_context`의 "어느 인접 에이전트를 함께 접지할지"를 구동 — 북극성 추출과 단기 액션(§6-1)이 맞물림(순차 아님).
- **라운드1 미증명 4갭 → 라운드2에서 닫음(정직)**.

### 9-3. 검증 라운드2 — 4갭 폐쇄 + 다운스트림 보상 증명 (2026-07-06)

라운드1의 4갭을 실 claude(opus-4-8)로 검증(스크래치패드 `round2/`·프로덕션 무변경):
- **재현율**: 골든셋 대조 완전(원자 문서 3·4·4·5·3 일치)·**장문 재현율 누락 0**(환불 3문서를 다문단 하나로 이어붙여도 8사실 전부 재현). 정밀도뿐 아니라 재현율 확인.
- **강건성**: 3문서×3회 재현성 안정(분절 ±1뿐·내용 누락/추가 0)·**장문 span 접지 100%(14/14)·열화 0.** 최대 리스크였던 장문이 버팀.
- **어휘 정제**: COMPLEMENTS 5→2(60%↓)·문서내 순차를 `LEADS_TO`로 분리·**핵심 cross-doc 엣지 생존**·접지 100% 유지. (유보: "cross-doc=COMPLEMENTS" 단순 규칙은 불성립·정제 반복 필요·workable).
- **다운스트림 보상(크럭스)—증명됨**: "환불불가면 청약철회 안 되나?" 질문에 **D1(단일 접지)=절반**(전상법 청약철회권만)·**D2(이중 접지·COMPLEMENTS가 약관규제법 문서 견인)=완전**(청약철회권 + 조항 자체 무효 + 이중 요건 종합). D2의 약관규제법 축은 D1 컨텍스트에 물리적 부재 → 그래프 엣지가 견인해야만 나옴. **그래프 엣지 → 절반 답을 완전한 답으로 전환, 실 답변으로 실증.**

### 9-4. 스파이크 종합 판정 — 기술 실현성 GO·남은 미지는 제품/스케일

**북극성의 기술 핵심이 검증됐다**(추출 품질·근거 접지·재현성·장문 강건성·다운스트림 보상 전부). "evidence 결속 + owner 검토로 EKOS 파서 결정론을 대체"가 실제로 작동하고 견고하며, 그래프가 측정된 통증(경계 넘는 관계형)을 실 답변에서 푼다.

**남은 미지는 "될까?"가 아니라 "지속 가능한가?"** — 스파이크로 더 못 풀고 실 빌드가 답할 것: ① **실 owner 산문의 지저분함**(오탈자·불완전·모순·비격식 — 여기 코퍼스는 결정론 직작성 5문서로 깨끗) ② **문서 쌍 후보 생성**(스파이크는 우리가 두 문서를 손으로 짝지음 — 프로덕션은 "어느 문서를 짝지을지"가 별도 문제·contested 후보집합/임베딩 이웃이 후보이나 미설계) ③ **owner 검토 UX·부담**(문서당 수 사실 × 지식베이스 규모의 지속 검토) ④ **비용/지연**(문서당 LLM 호출 + 편집마다 재추출).

**추천: 스파이크 트랙 여기서 종료.** 기술 실현성은 스파이크가 유용하게 풀 수 있는 한계까지 검증됐다(강한 GO). 남은 미지는 실 빌드+실 사용자라야 답하므로 더 스파이크하지 말 것. 북극성은 이제 **검증된·준비된 옵션**이고, 트리거(관계형 통증이 assemble_context를 넘어섬/그래프 지식이 중심이 됨)가 오면 첫 설계 질문은 **②문서 쌍 후보 생성**이다. 단기 액션(assemble_context §6-1)은 불변 — 이제 COMPLEMENTS가 그것을 구동하는 다리로 입증됨.

**한 줄 결론:** 세 프로젝트는 경쟁이 아니라 **초점이 다른 자매**다 — AON=누가 담당·라우팅, EKOS=타입화 소스의 결정론 그래프, TrustGraph=산문 GraphRAG 참조 구현. AON은 지금 자기 `assemble_context`를 채우고, EKOS의 패턴을 빌리며, 두 외부 그래프는 각각의 트리거가 올 때만 소환한다.

---

## 10. 남은 미확인 (정직 표기)

- TrustGraph 최소 RAM/노드·REST 포트·msgpack standalone 리더 — 미명시(사이드카 미채택으로 추적 불요).
- 답변 품질 정성 채점은 실 claude 미실행(코퍼스 구조로 논증). 100배 라우팅 붕괴점은 외삽.
- EKOS 대규모(100K+ fragment) 스케일·산문 지식 적용 실증은 없음(SAP 타입 소스 전제). EKOS↔AON 저작 피벗(§9-4)의 실현성은 미검증 전략 가설.
