# ADR 0041 — 큐레이션 골든셋: 답변 품질 축 + 라벨 provenance(사람 확정만)

- 상태: 채택(Accepted, 2026-07-13) — 사용자가 큐레이션 골든셋과 사람 확정 라벨 원칙을 확정했다. · **보강(2026-07-22 — 갭 라벨 채점 보정·어휘 사영)**: unowned 갭 질문의 `expected_intent`는 사람 가독 라벨(주차 등·어휘 밖)을 그대로 유지하되, `run_eval(..., vocabulary=...)`이 분류 축 채점만 어휘 사영(`expected ∉ vocab → ""`)으로 보정한다 — `LlmClassifier` 계약(어휘 안 라벨 또는 `""`)과 라벨 provenance 보존을 양립시키는 채점 규약(사용자 확정·구현 tasks P17.11 기록).
- 계보: **ADR 0003**(라우터 정확성=TDD·LLM 품질=골든셋 eval)의 구체화 — 0003이 "분류·**답변 품질**은 골든셋 임계 eval"이라 이미 답변 품질을 eval 스코프에 명시했으나, `SampleQuestion`/`run_eval`은 분류·라우팅 축만 구현돼 있었다. 이 ADR이 미구현 축과 라벨 provenance를 채운다. **ADR 0047**은 이 정책을 승격용 독립 held-out 평가로 확장하고, Precedent·운영 결과의 자동 라벨화를 명시적으로 금지한다. **ADR 0035**(Owner Scorecard — Goodhart 방지·"지표가 목표가 되면 지표이기를 그친다")의 정신 인접 — 정답 라벨의 무결성을 코드가 굳기 전에 못박는다. **ADR 0039**(실조직 배포·채택 사활 기준)의 검증 축 — S7/S8·§8 채택 기준을 실측하려면 라우팅뿐 아니라 답변 품질까지 재야 한다. ADR 0004(Authority 중앙·under-claim만 자기보고) 정합.
- 구현 상태: `AnswerExpectation`·`AnswerGrader`·`FakeGrader`·`SubstringGrader`와 세 축 집계 구조는 구현됐다. 현재 시드 30문항에는 `answer_expectation`이 한 건도 없고 `LlmGrader`는 `NotImplementedError` 자리다. routing 기대 필드에는 별 `label_provenance`가 없으며, `CurationProvenance.curated_by`도 인증된 사람 receipt가 아닌 문자열이다. 따라서 현재 데이터는 coherence 확인용일 뿐 production 승격 게이트가 아니다.
- 성격: **도메인 모델 변경 + 되돌리기 어려운 검증 정책.** 정답 라벨의 무결성(누가 확정하나·무엇이 라벨이 될 수 있나)을 박는 foundational 결정이므로 정식 ADR감이다.

---

## 맥락 — 검증을 실 메일 재생이 아니라 큐레이션 골든셋으로 한다

Phase 15는 두 축이다: (A) 메일 질문 채널, (B) 검증 확장. 이 ADR은 **B축만** 다룬다.

이 결정을 내릴 당시 eval(ADR 0003·`eval.py`)은 **분류 정확도**와 **라우팅 정확도** 두 축만 쟀다. `run_eval`은 `classify`와 `route`만 재생했고 답변 경로(dispatch→runtime)를 타지 않았다. `SampleQuestion`에도 라우팅 기대치(`expected_intent`·`expected_disposition`·`expected_primary`…)만 있고 **답변이 좋은가를 판정할 기대 필드가 없었다**. ADR 0039가 사활 기준을 "기능이 도나"에서 "조직이 채택하나"로 옮긴 이상, "담당이 맞나(라우팅)"를 넘어 **"그 담당의 답이 실제로 쓸 만한가(답변 품질)"**를 재는 축이 필요했다.

사용자가 검증 방식을 확정했다: **실 메일 자동 재생이 아니라 큐레이션 골든셋**이다. 실 질문은 메일/Slack에서 길어오되, **정답(올바른 담당·기대 답 기준)은 사람이 확정**한다. 이 결정이 아래 정책을 낳는다.

## 결정

### 결정 1 — 답변 품질을 세 번째 eval 축으로 추가한다(0003 완성, 확장 아님)

`EvalReport`에 `answer_quality_accuracy`(+ 채점 대상 수)를 더한다. `run_eval`은 라우팅이 `Routed`로 떨어진 골든 질문에 대해 **답변 경로를 재생**(주입 런타임의 `answer(question, primary_card)`)하고, 그 답을 **`AnswerGrader` 포트**로 채점해 통과 비율을 낸다. 세 축(분류·라우팅·답변 품질)이 각자 임계와 비교되고, **한 예제 틀림이 아니라 집계 비율 vs 임계**가 통과를 가른다(0003 원칙 계승).

근거: ADR 0003이 애초에 "분류·답변 품질"을 골든셋 스코프로 명시했다. 이건 새 정책이 아니라 0003의 미구현 축을 채우는 것이다. 답변 품질을 별 축으로 분리해(분류·라우팅과 섞지 않고) 어느 층이 무너졌는지 진단 가능하게 둔다.

### 결정 2 — 정답 라벨은 사람이 확정한 것만 유효하다(라벨 provenance·핵심 불변식)

답변 기대기준(무엇이 좋은 답인가)과 `expected_intent`·disposition·primary/candidates·approval/collaborators를 포함한 routing 기대값은 **사람이 확정한 라벨만** 유효하다. **실 메일/Slack의 Answer가 자동으로 라벨이 되는 코드 경로는 존재하지 않는다.** 현재 타입이 강제하는 범위와 Phase 18 승격 요건은 구분한다:

- 현재 `AnswerExpectation`은 `CurationProvenance`를 필수로 안지만, 값은 아직 인증 receipt가 아닌 문자열이다. routing 기대 필드에는 그에 대응하는 provenance 필드가 없다. 그러므로 현 30문항은 사람이 손으로 만든 coherence seed라는 운영 가정까지만 성립하고, 타입 수준의 인증된 ground truth라고 주장하지 않는다.
- Phase 18에서 classification·routing·answer-quality를 요구하는 target의 promotion evaluation은 위 모든 기대 필드와 nonempty answer criteria를 함께 덮는 인증된 사람 `label_provenance`를 가진 `HoldoutReservationReceipt`를 사용한다. 이 세 축은 label-dependent이므로 `NoHoldoutRequiredReceipt`로 낮출 수 없다. holdout의 `HoldoutSealReceipt`는 proposal 생성 전 별 append-only DB transaction에서 authoritative DB time·commit sequence로 먼저 확정하고, proposal UoW는 expected seal revision·unused와 `seal.commit_sequence < proposal_creation_sequence`를 CAS한다. seal·proposal 동일 transaction, caller time backdate, retry lineage의 dataset/split digest 재사용은 write 0이다. proposal은 policy-selected `EvaluationDatasetReservation = HoldoutReservationReceipt | NoHoldoutRequiredReceipt`를 정확히 하나만 갖고, no-holdout arm은 label-dependent metric이 없는 integrity/safety-only target에서도 독립 eval을 면제하지 않는다. target별 immutable `EvaluationRequirementPolicy`가 요구하지 않는 축은 policy만 `NotApplicable`로 선언할 수 있고, 필수 축의 `None | Skipped`는 실패다. `EvalCaseAddition`은 이 세 축의 candidate/baseline 비교로 자기 자신을 통과시키지 않고 provenance·schema·duplicate·leakage·split integrity를 검사한다. 이 요건을 충족하지 않은 기존 sample은 migration 전까지 승격 평가에서 제외한다.
- 골든 라벨을 `Answer`로부터 파생하는 함수·로더는 **두지 않는다**. `Answer`는 채점에서 *판정 대상*으로 흘러 들어갈 뿐, 라벨로 *흘러 나오지* 않는다(방향 단언 — 구조 가드).

근거: 정직한 제약 1 — **메일 답변자 ≠ 올바른 담당.** 메일에 누가 답했든 그게 "올바른 담당"이라는 근거가 못 된다. 자동 라벨링을 허용하면 시스템이 자기 출력을 정답으로 삼는 순환(자기충족 eval)이 생겨 검증이 무의미해진다. Authority가 중앙에서만 선언되듯(ADR 0004), **정답도 사람이 중앙에서 확정**하고 자동 산출물이 권한/정답을 창설하지 못한다.

### 결정 3 — 기존 `SampleQuestion`을 확장한다(신규 병렬 타입 아님)

답변 기대기준은 `SampleQuestion`에 **옵셔널 필드**로 얹는다(신규 `CuratedGoldenQuestion` 병렬 타입을 만들지 않는다). 필드가 없으면 라우팅만 채점(기존 30개 시드 하위호환). `samples/questions.jsonl` 형식·`load_golden`·`_routing_matches`를 그대로 재사용한다.

근거: 시드 질문은 기존 분류·라우팅 coherence를 확인하는 동일 행 타입이고, 답변 기대기준은 그 위의 추가 층이다. 병렬 타입·병렬 로더는 중복·유지부담이다. 다만 `AnswerExpectation` 안의 현재 provenance만으로 routing 라벨까지 인증되지는 않는다. Phase 18에서 이 세 축을 요구하는 target은 `SampleQuestion` 전체 기대값을 덮는 인증된 `label_provenance`와 dataset version 영수증을 추가해야 한다.

### 결정 4 — 게이트 내는 결정론, 실 grader·실 큐레이션은 게이트 밖(0003 경계 계승)

- **게이트 내(결정론)**: `run_eval`이 골든셋을 받아 세 축을 집계하고 임계와 비교하는 *구조*를 `FakeClassifier`+`StubRuntime`+`FakeGrader` 주입으로 검증한다(러너 로직은 분류기·런타임·grader와 무관하게 결정론). `AnswerGrader` 포트 + `FakeGrader`(고정 판정)까지가 게이트 내.
- **게이트 밖(실 LLM·수동/야간)**: 실 `LlmGrader`(LLM-as-judge)·실 큐레이션(사람이 실 메일/Slack 질문에서 정답담당+기대기준 확정)·실 Router+실 Runtime로 도는 정확도 실측. 채점 전략(기준 substring vs LLM-as-judge)·임계값 확정은 게이트 밖 외부 결정.

근거: ADR 0003의 결정론 vs 실 LLM 경계를 그대로 답변 축에 적용한다. `FakeClassifier`/`StubRuntime`이 분류·라우팅 축에서 러너 구조만 결정론으로 잠그듯, `FakeGrader`가 답변 축에서 같은 역할을 한다. 실 품질 신호는 게이트 밖 실 LLM에서만 나온다(단위 테스트로 확률적 품질을 박을 수 없음).

## 대안(기각)

- **실 메일 답을 골든 라벨로 자동 재생/자동 라벨링** — 기각(결정 2 근거·자기충족 eval·메일 답변자≠담당).
- **신규 `CuratedGoldenQuestion` 병렬 타입+로더** — 기각(결정 3 근거·중복·과설계). 기존 `SampleQuestion`을 확장하되, Phase 18 승격용 인증 provenance는 별 migration으로 보강한다.
- **답변 품질을 분류/라우팅 축에 합산(단일 통과 지표)** — 기각. 어느 층이 무너졌는지 진단 불가(결정 1 근거·축 분리).
- **`AnswerExpectation`에 provenance를 옵셔널로** — 기각. 현재 문자열 표식도 최소한 비어 있는 출처를 막기 위해 필수다. 다만 이것만으로 인증된 사람 receipt가 되지는 않으며 Phase 18 요건은 결정 2를 따른다.
- **게이트 내에서 실 LLM grader 실행** — 기각(비결정·느림·ADR 0003 경계). `FakeGrader`로 구조만 잠그고 실 grader는 게이트 밖.

## 결과

- `SampleQuestion`에 옵셔널 답변 기대기준(provenance 필수) 필드 추가·`load_golden` 하위호환.
- `EvalReport`에 `answer_quality_accuracy`(+채점 대상 수) 추가·`run_eval`이 `Routed` 답변 경로 재생·`AnswerGrader` 포트+`FakeGrader` 주입.
- `Answer`→라벨 파생 함수 부재를 구조 가드 테스트로 고정(방향 단언).
- 실 `LlmGrader`·실 큐레이션·채점 전략·임계값은 게이트 밖 외부 결정으로 명시 연기.
- Precedent·운영 결과는 큐레이션 후보일 뿐 자동 라벨이 아니다. ADR 0047의 개선 후보가 만든 eval case도 같은 후보의 held-out 평가에는 넣지 않는다.
- Phase 18에서 세 품질 축이 적용되는 target의 승격용 reservation은 전체 기대값을 덮는 인증된 사람 `label_provenance`, proposal 생성 전 별 transaction으로 선행 commit된 seal, 독립 split을 결박한 `HoldoutReservationReceipt`여야 한다. `NoHoldoutRequiredReceipt`는 세 label-dependent 축에 쓸 수 없다. target별 Evaluation Requirement Policy가 필수 축과 `NotApplicable` 축을 고정한다. 현재 30문항은 migration 전 coherence-only다.

현재 기본 시드에서는 답변 채점 대상이 0건이라 `answer_quality_accuracy=None`이다. 따라서 분류·라우팅 결과만으로 이 ADR의 답변 품질 목표를 달성했다고 보거나, Phase 18의 승격 근거로 사용할 수 없다.

## 불변식 영향(4대 + 고유 자체점검)

- **미아 없음**: 무접촉. eval은 사후 관측이라 라우팅·에스컬레이션·발신을 차단·변경하지 않는다(0매칭→root 경로 무손상).
- **무효 카드 거부**: 무관(카드 admission 무변경). 단, 정신 계승 — 무결성 없는 라벨(빈 확정자)은 거부된다(카드 검증이 무효 카드를 거부하듯 골든 로더가 무효 라벨을 거부).
- **Authority 중앙**: 강화. 정답 라벨은 사람이 중앙에서 확정하고, 자동 산출물(메일 Answer)이 정답/담당을 창설하지 못한다(결정 2). Authority가 중앙 선언이듯 ground-truth도 중앙 큐레이션.
- **전이 ≠ 기록**: 무관. eval은 **읽기 파생**(검증은 라우팅/실행을 무변경·관측만) — 새 전이 0. 골든셋은 append-only 라벨 데이터이지 도메인 전이가 아니다.
- **고유 불변식 = 라벨 무결성**(Goodhart 인접·ADR 0035 정신): `Answer`→라벨 파생 경로가 없고, Phase 18에서 해당 축을 요구하는 승격용 데이터는 모든 기대값을 덮는 인증된 사람 provenance 없이는 부적격이다. 현재 문자열 `CurationProvenance`와 provenance 없는 routing 기대값은 이 불변식을 아직 production 수준으로 충족하지 못한다.
