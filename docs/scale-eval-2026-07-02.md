# 스케일 관측 리포트 — ConceptOverlapMatcher stage-1 한계 실측 (2026-07-02)

ADR 0030 결정 5("실 owner 골든셋 관측 전 임베딩 미룸")의 전제를 해소하는 실측. 시나리오·방법은 [tasks-v0.md](tasks-v0.md) 스케일 관측 절, 데이터는 `registry/scale/`(카드 10)·`okf_scale/`(개념 70)·`samples/scale_questions.jsonl`(72문항·tier 라벨), 러너는 `scale_eval.py`.

## 실측 조건

- 가상 조직 "공공기관 통합 민원 데스크" — 실 법령 조문(저작권법 §7 비보호·`samples/scale_sources/`) 근거로 결정론 직작성한 카드 10장·개념 70개. 의도적 그레이 쌍 4(연장근로수당·환불 불가 조항·개인정보 삭제·보험료 공제).
- 매처: `ConceptOverlapMatcher`(stage-1 토큰 오버랩·한국어 조사 정규화) 단독, `assessor=None`(stage-2 자동해소 없음 — 매처 원 한계 측정).
- 골든셋 72문항: easy 29(도메인 어휘 직접)·hard 25(구어·동의어·어휘 미등장)·ambiguous 18(그레이 쌍 정조준)·0매칭 2 포함.

## 결과 (6지표)

| 지표 | 전체 | easy | hard | ambiguous |
|---|---|---|---|---|
| top-1 정확도 | **20.8%** | 31.0% | **4.0%** | 27.8% |
| 오라우팅률(Routed인데 오답) | **15.3%** | 6.9% | 36.0% | 0% |
| 0-매칭 escalation률 | 13.9% | 13.8% | 24.0% | 0% |
| contested률 | **58.3%** | 51.7% | 36.0% | 100% |

실패 57/72. 실패 케이스 전수는 러너 출력(`run_scale_eval` failures) 참조.

## 실패 모드 3종 (개별 케이스 직접 검증)

1. **어휘 공백 → 0매칭(unowned)**: "올해 최저임금이 시간당 얼마인가요?" → 0 후보. 개념 core_question/label 토큰에 "최저임금"이 없으면(카드 domains에 있어도 stage-1은 개념 토큰만 봄) 표면 토큰 불일치 = 즉시 미아. 의미적 일반화("시간당 얼마"→임금) 불가.
2. **공통 토큰 노이즈 → 동점 contested·오라우팅**: "하루 근로시간이 8시간을 넘으면?" → labor_std 1.0 = social_insurance 1.0 동점(margin 0). 조사 정규화 후 남는 일반어 토큰이 교차 도메인에 걸려 점수 분리가 붕괴. contested 58.3%의 주원인.
3. **무의미 교차 매칭**: "알바비에서 3.3% 떼는 게 맞나요?" → oss_license 1.0(오답 단독). 숫자·기호·일반어 토큰이 라이선스 원문 어휘와 우연 일치 — hard tier 오라우팅 36%의 전형.

## 판정 (S6.5 게이트)

사전 합의 임계: **오라우팅 >15% 또는 hard top-1 <70%면 T10.5 진행.**

- 오라우팅 15.3% > 15% ✅
- hard top-1 4.0% < 70% ✅ (압도적 초과)

→ **압박 확인 — T10.5 `EmbeddingAnnMatcher` 진행 확정.** ADR 0030 결정 5의 보류 전제("압박이 실제 있을 때 당긴다")가 실측으로 해소됨. 임베딩 인프라는 ADR 0032 C2의 `Embedder` 포트·`FastEmbedEmbedder`(fastembed·e5-small·`dedup` extra)를 재사용하므로 새 의존성 0.

## A/B 기준선

이 리포트의 수치가 `ConceptOverlapMatcher` 기준선이다. S8에서 동일 골든셋·동일 조립으로 `EmbeddingAnnMatcher`를 실측해 tier별로 대조한다.

## S8 — A/B 실측 결과 (EmbeddingAnnMatcher·실 fastembed e5-small·동일 72문항)

**전체 파이프라인**(assessor=None·τ=0.85):

| 구성 | overall top-1 | easy | hard | ambiguous | 오라우팅 | contested | unowned |
|---|---|---|---|---|---|---|---|
| ConceptOverlap (기준선) | 20.8% | 31.0% | 4.0% | 27.8% | 15.3% | 58.3% | 13.9% |
| **Embedding τ=0.85** | **38.9%** | **62.1%** | 20.0% | 27.8% | **1.4%** | 45.8% | 23.6% |

**매처 순수 top-K 랭킹**(routed 기대 65문항 — 라우터의 contested 접힘을 벗긴 매처 본연 품질):

| 매처 | top-1 | top-2 | top-3 |
|---|---|---|---|
| ConceptOverlap | 36.9% | 50.8% | 53.8% |
| **Embedding** | **67.7%** | **83.1%** | **86.2%** |

**판정·해석**
- 임베딩이 stage-1 랭킹 품질을 약 2배로 올리고(36.9%→67.7%), 오라우팅(가장 해로운 실패)을 15.3%→1.4%로 줄인다. **EmbeddingAnnMatcher 채택.**
- τ 스윕(0.75/0.80/0.82/0.85): e5-small cosine top-1 분포가 [0.814, 0.918]로 압축돼 0.85 미만은 전 후보 통과(contested 붕괴). **τ=0.85를 기본 정책값으로 확정**(`DEFAULT_EMBED_TAU`·주석에 본 리포트 근거).
- 잔존 contested 45.8%는 매처 결함이 아니라 **압축된 cosine 필드에서 절대-τ 단일 게이트가 단일 승자를 못 고르는 구조적 한계** — 정확히 ADR 0028 §6 stage-2(`ConfidenceAssessor`) 자동해소가 풀 몫. 후속 지렛대: stage-2 도입 또는 margin 기반 clear-winner 룰(top1-top2 격차 임계).
- e5 prefix 비대칭(passage:/query:)은 대칭 대비 실질 동등(top-1 +1.5%p·top-2/3 열위) — 기존 `FastEmbedEmbedder`(대칭·dedup 공유) 그대로 재사용, 별도 어댑터 불추가.
- 비용: 모델 로드 ~580ms(1회)·72문항 전체 ~410ms(인덱스 임베딩 캐시 적중 후). 기준선 ~165ms — 규모 70개념에서 수용 가능.

**배선**: `select_matcher()`(`AON_MATCHER` env 시임 — 미설정/`overlap`=기존 기본 무변경·`embedding`/`fastembed`=EmbeddingAnnMatcher+FastEmbedEmbedder 지연 import) — index 모드 라우터 조립(demo.py)이 소비. 스케일 러너는 `build_scale_router(matcher=)` 주입.

## S9 — stage-1.5 clear-winner(margin) 룰 실측 (ADR 0028 §16)

δ 스윕(오라우팅 상한 가드 = embedding 기준선 1.4%+1%p):

- **embedding: δ=0.03 채택** — contested 45.8%→41.7%·easy top-1 62.1%→**72.4%**·overall 38.9%→**43.1%**·오라우팅 1.4% 무악화. δ≤0.02는 contested를 크게 줄이나(최저 12.5%) 오라우팅 6.9~12.5%로 폭증(가드 위반), δ=0.05는 off와 동일(압축 cosine 범위상 발동 0).
- **overlap: 비채택(None=off)** — δ=1은 오라우팅 15.3%→18.1% 악화(가드 위반), δ≥2는 정수 이산 score라 발동 0(off와 동일). overlap 단독 사용 시 stage-1.5 비권장.
- hard/ambiguous tier 무영향 — e5-small이 그레이 쌍·구어에서 margin을 못 벌리는 구조적 한계(설계 의도대로 "명백한 승자"만 값싸게 회수). 잔여 contested 41.7%의 다음 지렛대는 owner측 `ConfidenceAssessor`(ADR 0028 §6·stage-2 본체) 또는 상위 임베딩 모델.

**배선**: `TwoStageRouter(stage1_clear_winner_margin=None)`(기본 off·하위호환) + `recommended_stage1_margin()`(`AON_MATCHER`와 같은 env에서 도출 — overlap=None·embedding=0.03·`DEFAULT_STAGE1_MARGIN_EMBEDDING` 단일 원천) — demo.py index 모드 조립이 소비.

## S10 — stage-2 `EmbeddingConfidenceAssessor` 실측 (ADR 0028 §17)

**설계 근거 실물 검증**: 그레이 쌍은 목차(core_question) 어휘가 안 갈리지만 **body는 근거 법령이 배타적으로 갈린다**(전자상거래법 §17 vs 약관법 §6 등) — stage-2가 body 전문을 접지하면 stage-1이 못 본 신호를 본다.

2축 스윕(clear_winner_margin × min_confidence·오라우팅 가드 2.4%): **margin=0.02·min_confidence=0.75 채택**. min_confidence는 관측 cosine 하한(0.838)보다 낮게 — 분포 중간대(0.85) 침범 시 클램프가 격차 계산을 흔들어 오라우팅 악화(실측).

| 지표 | 기준선(τ=0.85+stage-1.5) | **+ stage-2** |
|---|---|---|
| overall top-1 | 43.1% | **50.0%** |
| easy | 72.4% | 75.9% |
| hard | 20.0% | 24.0% |
| **ambiguous** | 27.8% | **44.4%** |
| 오라우팅 | 1.4% | **1.4% (무악화)** |
| contested | 41.7% | **34.7%** |

**body 분리 가설 검증됨** — ambiguous 자동해소 성공이 실제 그레이 쌍에서 정답을 골랐다(예: "환불 불가 조항이 있는 상품은 청약철회도 안 되는 건가요?" → consumer_protect 정답). 캐시는 (agent_id, 파일 mtime) 키 body 벡터·assess 예외는 confidence 0.0 흡수(미아 없음 — Contested 폴백).

**배선**: demo index 모드에서 embedding 매처 선택 시 **같은 임베더 인스턴스 공유**(`EmbeddingAnnMatcher.embedder` 접근자·모델 1회 로드)로 assessor 자동 장착·okf_root=DEMO_OKF_ROOT(인프로세스 디제너레이트·ClaudeCodeRuntime cwd 접지 선례). overlap 기본 경로는 assessor=None 무변경. 크로스머신은 §15 FetchDocument 확장으로 owner 워커가 confidence 수치만 회신(후속).

## 누적 궤적 (전체 스케일 트랙)

| 단계 | overall top-1 | 오라우팅 | contested |
|---|---|---|---|
| 토큰 오버랩 (시작점) | 20.8% | 15.3% | 58.3% |
| + EmbeddingAnnMatcher(τ=0.85) | 38.9% | 1.4% | 45.8% |
| + stage-1.5(δ=0.03) | 43.1% | 1.4% | 41.7% |
| **+ stage-2(margin 0.02·min_conf 0.75)** | **50.0%** | **1.4%** | **34.7%** |

## S11 — 커버리지 보강(음성 결과)·임베딩 모델 A/B (2026-07-02)

**① 개념 커버리지 보강 — 가설 기각(정직 기록).** "unowned의 태반은 지식 커버리지 문제"라는 가설로 실패 36건을 전수 판정한 결과, **커버리지 갭은 1건뿐**(wage_ops "최저임금" — 카드 domains엔 있는데 개념 부재). 나머지는 매처 한계(정답 개념이 있는데 표면 어휘 격차로 놓침) 또는 소스 발췌 부재(근거 없는 개념 추가 금지 원칙으로 보강 불가 — 실업급여·종합소득세 신고 등). 갭 1건을 보강(minimum-wage-effect.md·최저임금법 §1·§6 근거)해도 **수치 무변화**(top-1 50.0% 유지) — stage-1 margin 0.0093(δ 미달)·stage-2 body cosine도 labor_std 연차 개념과 0.0043 차로 분리 실패(압축 cosine 필드의 구조적 노이즈). 결론: 이 코퍼스에서 커버리지 보강은 지렛대가 아니다.

**② e5-large A/B — 채택(opt-in).** `FastEmbedEmbedder(model_name=)` seam + `AON_EMBED_MODEL` env 신설 후 τ·δ·margin 재스윕(정책값이 모델 cosine 분포에 묶임을 재확인):

| 구성 | top-1 | 오라우팅 | contested | unowned |
|---|---|---|---|---|
| e5-small (τ0.85·δ0.03·m2 0.02) | 50.0% | 1.4% | 34.7% | 23.6% |
| **e5-large (τ0.83·δ0.02·m2 0.03)** | **54.2%** | 1.4% | 37.5% | **16.7%** |

+4.2%p·미아 -6.9%p·오라우팅 무악화. 비용: 모델 2.24GB(17배)·dim 1024. **판정: 기본은 e5-small 유지(경량), e5-large는 품질 우선 배포의 opt-in**(`AON_EMBEDDER=fastembed AON_EMBED_MODEL=intfloat/multilingual-e5-large`).

**모델별 정책 맵 단일 원천화**: `index_matcher._EMBED_POLICY_BY_MODEL`(model→τ·δ·m2)이 `select_matcher`(τ)·`recommended_stage1_margin`(δ)·`recommended_stage2_margin`(m2)의 공통 원천 — `AON_EMBED_MODEL`만 바꾸면 세 정책값이 같이 따라간다(짝 불일치 구조적 방지). 미지 모델은 e5-small 값 폴백(스윕 전 무보장 — 주석 계약).

## S12 — stage-2 (b) LLM 자기평가 실측: 기본 미채택 (ADR 0028 §17-b)

`LlmConfidenceAssessor`(claude-haiku·`claude -p`·67콜·평균 17.2s/콜) A/B — raw confidence 1회 수집 후 정책값은 표 위 오프라인 스윕(비결정 응답 재사용·스윕 공정성):

| 구성 | top-1 | 오라우팅 | contested | ambiguous top-1 |
|---|---|---|---|---|
| embedding assessor (기준선) | 50.0% | 1.4% | 34.7% | 44.4% |
| LLM 가드준수 (margin 0.6·minc 0.3) | 48.6% | 1.4% | 36.1% | 33.3% |
| LLM 최고품질 (margin 0.2) | 62.5% | **5.6% 가드 위반** | 15.3% | 66.7% |

**판정 — 기본 미채택(정직 기록).** LLM은 그레이 쌍에서 결정력이 매우 높으나(가드 해제 시 top-1 62.5%·contested 15.3%), 그 결정력의 상당 부분이 **골든셋이 "사람 합의(Contested)"로 라벨한 케이스를 한쪽으로 확신 라우팅**하는 형태라 오라우팅 가드(2.4%)와 정면 충돌한다. 가드를 지키려 margin을 0.6까지 올리면 결정력이 상쇄돼 기준선 이하(48.6%). 오라우팅이 가장 해로운 실패라는 시스템 원칙 하에서는 임베딩 기준선 유지가 옳다. 지연(후보당 ~17s)도 대화 경로에 부적합.

- 채택 상태: `AON_ASSESSOR` 미설정=auto(임베딩 배선 100% 보존·기본 무변경). `llm`은 opt-in으로 존재(가드준수값 margin 0.6·minc 0.3 — 단 이득 없음을 명기).
- **다음 지렛대 = (c) 하이브리드**(§17-b 결정 M 후속): embedding이 오라우팅을 방어하며 자동해소하고, *잔여 그레이 쌍만* LLM이 리랭크하는 구조 — LLM 결정력을 contested 한정 적용. 또한 "ambiguous 라벨 자체가 사람 합의를 요구하는가"라는 라벨링 철학 재검(LLM이 고른 쪽이 실무상 옳은 케이스 존재)도 병행 거리.
