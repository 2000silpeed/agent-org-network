# 라우터 정확성은 TDD 단위 테스트, LLM 품질은 골든셋 eval로 나눠 검증

상태: accepted (2026-06-20)

정합화: ADR 0041·0047이 골든 라벨의 출처와 승격 평가를 구체화한다. 2026-07-13부터 이 ADR의 "골든셋"은 **사람이 정답과 기대 기준을 확정한 큐레이션 데이터**만 뜻한다.

"틀림"에 두 종류가 있어 검증 방법도 둘로 나눈다. 라우터·레지스트리 로직 오류는 결정론적이라 **단위 테스트(red-green, `FakeClassifier`·`StubRuntime` 주입)**로 매 커밋 잡는다. LLM 분류·답변 품질은 확률적이라 단위 테스트로 박을 수 없고, 사람이 확정한 라벨과 provenance를 가진 **골든셋**의 정확도/통과율 **임계값 eval**로 검증하며 회귀를 막는다(분류기·런타임 변경 시·야간). 한 예제가 틀리는 게 실패가 아니라, 임계값 미달이나 이전 대비 하락이 실패다.

## Consequences

- Precedent·Answer·Feedback·Correction 같은 운영 산출물은 큐레이션 후보일 뿐 eval 라벨이 아니다. 사람의 확정 없이 시스템 출력을 ground truth로 되먹이지 않는다.
- 실제 LLM은 단위 테스트에서 stub/fake로 대체하고, eval에서만 진짜를 쓴다.
- production 승격에는 일반 회귀 eval과 별도로 ADR 0047의 target별 immutable `EvaluationRequirementPolicy`, 독립 평가와 사람 승격 처분이 필요하다. 정책은 필수 축·비적용 축, `CandidateBaselineMetric | IntegrityCheck | SafetyInvariant`, runner/rubric, holdout 필요 여부와 scope를 고정한다. 필수 축의 `None | Skipped`는 실패고 `NotApplicable`은 policy가 미리 선언한 축에만 유효하다.
- Phase 18의 모든 proposal은 정책이 고른 `EvaluationDatasetReservation = HoldoutReservationReceipt | NoHoldoutRequiredReceipt`를 정확히 하나만 갖는다. classification·routing·answer-quality처럼 label-dependent 축이 있으면 holdout arm이 필수이며 `NoHoldoutRequiredReceipt`로 낮출 수 없다. holdout seal은 proposal 생성 전 별 append-only DB transaction에서 authoritative DB time·commit sequence로 먼저 확정하고, proposal UoW는 seal의 expected revision·unused와 strict prior sequence를 CAS한다. no-holdout arm은 current authority policy가 label-dependent metric이 없는 integrity/safety-only target에만 발행하며 독립 eval을 면제하지 않는다. 현재 30문항은 routing label의 인증 receipt와 답변 기대기준이 없어 coherence 자산일 뿐 어떤 target의 승격 근거도 아니다. 현재 eval 통과만으로 serving target state를 바꾸지 않는다.
