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
