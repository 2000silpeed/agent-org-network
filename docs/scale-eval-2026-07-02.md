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
