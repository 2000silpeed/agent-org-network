# ADR 0038 — 합의-소싱 COMPLEMENTS 엣지 · `EdgeGroundingSelector`(Routed co-grounding) · concede stance로 상보/오등록 구별

- 상태: 채택(Accepted)
- 날짜: 2026-07-07 (사용자 승인 — 긴장 3 기본값 = 패자 명시 선언·기본 withdraw)
- 계보: ADR 0037(co-grounding·`EdgeGroundingSelector` 후속 자리·Authority=primary 단일)의 **후속 실체화**. ADR 0036 §9(북극성·에스컬레이션 사다리 ①·"COMPLEMENTS가 다중 접지를 구동하는 다리")의 **경량 룽 구체화**(무거운 북극성 연기 범위와 무충돌). ADR 0008/0014(1인칭 합의·`Precedent`)·ADR 0004(under-claim 자기보고)·ADR 0028(`ConceptEdge`·혼동 방지 대상). supersede 없음.
- 근거 본체: [`docs/trustgraph-eval-2026-07-06.md`](../trustgraph-eval-2026-07-06.md) §9-3(D1 단일 접지=절반 답 vs D2 다중 접지=완전한 답).

---

## 맥락 — 합의가 담당은 정했지만 상보 지식은 잃는다

co-grounding(ADR 0037)은 **Contested일 때만** 여러 담당 지식을 함께 접지한다. 그런데 사람 합의로 "보상 → cs_ops"가 정해지면 `Precedent`가 쌓여 **다음 보상 질문은 Routed(cs_ops 단독)** 이 된다. 다툼이 아니니 co-grounding이 꺼지고 → **다시 반쪽 답**(finance_ops의 보상 관점 누락). 합의가 *담당(front)* 은 정했지만 *상보 관계 지식* 은 잃은 것이다.

나이브한 처방("finance 지식을 cs_ops로 전이")은 틀렸다 — 지식은 owner가 소유·유지하고(복사본은 stale·이동은 finance가 잃음), 두 지식은 같은 사실이 아니라 **서로 다른 각도**(고객 응대 관점 vs 회계·정산 관점)라 전이하면 owner가 남의 전문성을 못 유지한다. **옮겨야 할 건 지식이 아니라 *관계*** 다.

이 ADR은 그 관계를 **합의가 남기는 `ComplementEdge`** 로 박고, `EdgeGroundingSelector`가 Routed에서도 그 엣지 이웃을 함께 접지하게 한다. **엣지의 출처가 사람 합의**(이미 후보 2명을 앎)지 LLM 추출이 아니므로, ADR 0036이 연기한 무거운 북극성(LLM 산문 추출·문서쌍 후보 생성·evidence span·검토 UX)을 **하나도** 짓지 않고 방금 만든 co-grounding 기계장치를 그대로 재사용하는 **경량 룽**이다.

---

## 결정

### 결정 1 — 합의는 `Precedent`와 대칭인 두 번째 학습 산물 `ComplementEdge`를 방출한다

**핵심 대칭: Precedent : 라우팅 :: ComplementEdge : 접지.** 판례가 "이 intent의 front는 누구인가"(미래 라우팅)를 바꾸듯, 상보 엣지는 "front를 누가 보완하나"(미래 접지)를 바꾼다. 둘 다 합의가 `Agreed`에서 방출하는 **도메인 상태**이고, 둘 다 미래 결정이 읽으며, 둘 다 AuditLog가 아니다.

- **`ComplementEdge`(frozen 값 객체)**: `intent`(관계가 성립하는 라우팅 라벨)·`primary_id`(라우팅된 front)·`supporting_id`(함께 접지할 이웃 카드). **유향**(primary → supporting) — front가 답하고 complement가 접지하는 관계는 한 방향이고, 역방향은 그 intent의 판례가 primary를 잠가 발생하지 않는다. N후보 → 1 primary + (N−1) 잠재 supporting이라 **패자 카드당 1엣지**(pairwise).
- **`ConceptEdge`(ADR 0028) 재사용 기각**: from/to가 개념-id인 목차 관계(라우팅 경로·`okf_authoring` LLM 소싱)라 agent_id를 넣으면 개념-노드 의미가 오염된다. `COMPLEMENTS`라는 *관계명*은 ADR 0036 §9 어휘로 공유하되 *운반체*는 별개(card-pair+intent·합의 소싱).
- **`Transfer(이관)`과 혼동 금지**: Transfer는 카드 *소유권* 재지정(A 빠지고 B 맡음)이고, 엣지는 두 카드의 *지식 관계*(둘 다 owner 유지).

### 결정 2 — `EdgeStore` 포트 (도메인 상태·전이≠기록)

`PrecedentStore`·`ConflictCaseStore`·`PublishedIndexStore` 패턴의 N번째 — `Protocol` + `InMemoryEdgeStore`. `record(edge)`·`neighbors(intent, primary_id) -> tuple[str, ...]`(그 intent에서 primary를 front로 둔 supporting agent_id들·멱등·순서 결정론).

- **저장 판정**: 엣지는 미래 접지 결정(`EdgeGroundingSelector`)이 읽는 **학습된 관계(도메인 상태)** 지 절차 로그(AuditLog)가 아니다 — `Precedent`와 동렬. **전이 ≠ 기록** 정합(합의 *사건* 자체는 audit이 따로 기록·무변경).
- **방출 지점**: `ConsensusService.concur`의 `Agreed` 분기(`self._precedents.record(resolution)` 바로 곁 — 판례와 엣지가 같은 합의 종결에서 함께 태어난다). `edge_store`는 `ConsensusService`에 **옵셔널 주입**(기본 `None` → 방출 0 · 회귀 0).

### 결정 3 — 상보 vs 오등록은 concede stance가 구별한다 (급소·사용자 승인)

합의 "보상→cs_ops"는 두 뜻일 수 있다: **(a) 진짜 걸침**("cs_ops가 front지만 finance 관점 계속 필요") → 엣지 방출 ✅ · **(b) 오등록**("보상은 사실 cs_ops 것·finance가 잘못 주장") → 엣지 방출 ❌(finance가 카드 domain에서 빼야). 매 합의에 자동 방출하면 (b)에서 잘못된 엣지가 생긴다.

- **신호 = 진 후보 owner의 concede stance**(per-vote). `ConcurOnPrimary.stance: Literal["withdraw", "keep_as_complement"]` — 양보(concede) 표에서만 의미. 지식 관련성의 판단 주체는 그 지식을 **소유한 owner**다(owner 주권·CONTEXT §Owner). primary도 중앙도 아니다.
- **기본값 = `withdraw` = 엣지 없음 (안전 기본·사용자 승인)**. 엣지는 **양성 신호를 요구**한다 — 진 owner가 명시적으로 `keep_as_complement`("내 관점은 계속 필요")를 선언할 때만 방출. 기본이 no-edge라 (b) 오등록에서 잘못된 엣지가 안 생기고, 기존 합의 생성처·테스트 100% 무영향.
- **패자 선언(Option A)** 채택 — 진 후보 owner 혼자 "나는 상보로 남는다"를 선언. 이긴 front의 수락까지 요구하는 **양자 합의(Option B)** 는 마찰↑라 *관측된 필요 전까지 연기*(과방출 노이즈가 실 운영에서 보이면 승격).
- **under-claim 정합**: complement 선언은 권한 주장이 아니라 *자기 지식의 관련성* 자기보고다(양보로 primary는 이미 넘김·접지를 더할 뿐 authority 0). ADR 0004 under-claim과 정합, Authority 중앙 무손상.
- **정직한 열린 질문**: 진 owner가 *자기 관련성을 오판*(진짜 오등록인데 keep 체크)하면 잘못된 엣지가 생긴다. 완화 3중 — ① 엣지는 접지일 뿐(소프트 실패·primary가 정정/기각 가능·불변식 안 깸) ② 생애주기 재검증으로 자연 소멸(결정 5) ③ 원치 않는 complement 접지 노이즈가 관측되면 양자 합의(Option B)로 승격. v0는 최소·정직 — 기능은 owner가 진짜 걸침을 인지할 때 *가용*하고, 신호 없으면 오늘 동작(Routed 단일 접지)으로 안전 폴백(은근한 과잉 도달 없음).

### 결정 4 — `EdgeGroundingSelector` + `ChainGroundingSelector` (단일 seam 합성)

- **`EdgeGroundingSelector`**: `Routed(primary)`에서 `decision.intent`+`primary.agent_id`로 `EdgeStore.neighbors`를 조회 → 카드 해소 → `GroundingSet(primary, supporting)`. `Routed` 아님/이웃 0/유효 이웃 0 → `None`(단일 접지 폴백·회귀 0). `card_lookup`(Registry.get) 주입.
- **`ChainGroundingSelector`**: 여러 `GroundingSelector`를 순서대로 시도해 첫 non-None을 돌리는 합성 selector(자신도 `GroundingSelector`). `AskOrg`의 **단일** `grounding_selector` seam(ADR 0037 설계)을 보존하며 `ChainGroundingSelector((EdgeGroundingSelector(...), ContestedGroundingSelector()))`를 주입. Edge(Routed)·Contested(Contested)는 처분이 배타라 각자 non-matching엔 `None`을 돌리므로 순서 무관이되, 명시 순서로 결정론 보장.
- **`AskOrg` 배선**: `_select_grounding_set`의 파라미터를 `Contested` → `RoutingDecision`으로 넓히고 **Routed(Delivered) arm에서도 호출**해 GroundingSet이 오면 co-grounded dispatch로 흘린다(현재는 Contested arm만 호출). `handle`/`handle_stream` 대칭·approval gate·`_record_answer` 재적용 유지.
- **Authority 정합(중요)**: 엣지 케이스는 primary = **라우팅된 front라 명확**하다(판례/매처가 확정 — ADR 0037 `ContestedGroundingSelector`의 알파벳 tie-break 애매함이 여기선 **없다**). `answered_by` = primary 단일 불변. 엣지는 *지식 관계 신호지 권한 선언이 아니다* — supporting은 접지 출처일 뿐 authority 0(under-claim/자기보고로 권한을 넓히지 않음). Authority 중앙 보존.

### 결정 5 — 생애주기: 선택시점 재검증으로 stale 자연 소멸

- **선택시점 재검증(소멸 규칙)**: `EdgeGroundingSelector`가 이웃 카드 해소 후 `intent ∈ card.domains`일 때만 supporting에 포함. 이웃이 나중에 그 domain을 카드에서 빼면 → skip → **엣지 자연 중성화**(watcher·삭제 API 불요). publish 수용 시 `concept.domain ∈ card.domains` 재검증·판례 무효 skip 패턴과 동형.
- **소유권 이관**: 엣지는 agent_id(역할 불변·owner 교체해도 불변) 기반이라 이관 무영향 — 그리고 이게 옳다(상보 관계는 사람이 아니라 역할/지식 사이).
- **재-contest/primary 변경**: 재합의는 새 엣지 멱등 방출. primary가 바뀌면 옛 엣지는 옛 primary 키라 새 Routed(새 primary)가 조회 안 함 → 도달 불가 stale(양성·미선택). 명시 삭제 API는 **관측된 필요 전까지 연기**(과잉 설계 회피).

---

## 근거

- **측정된 통증의 두 번째 반쪽을 닫는다** — ADR 0037이 Contested에서 완전한 답을 냈지만 합의 후 Routed에서 반쪽으로 회귀했다. 이 엣지가 그 회귀를 막아 "완전한 답"을 판례 이후에도 유지한다.
- **가장 싼 출처로 seam을 먼저 채운다** — ADR 0036 §9의 "COMPLEMENTS가 다중 접지를 구동하는 다리"를 *무거운 LLM 추출 투자 전에* 사람 합의로 실현해 `EdgeGroundingSelector` 소비자 seam을 디리스크한다. 무거운 북극성(LLM-추출 엣지)은 같은 selector에 후속 additive.
- **owner 주권 + 안전 기본이 급소를 푼다** — 상보/오등록 구별을 그 지식 owner에게 맡기고(주권), 기본을 `withdraw`(no-edge)로 둬 오판 위험을 최소화한다. 남는 오판 위험은 소프트 실패·자연 소멸·Option B 승격 경로로 정직하게 관리.

## 계보 (전부 계승·supersede 없음)

- **ADR 0037 — 후속 실체화.** `EdgeGroundingSelector` 후속 자리를 채우고, Contested 전용이던 co-grounding을 Routed로 확장. Authority=primary 단일·노출 불변식·`grounding` 옵셔널 인자 재사용.
- **ADR 0036 §9 — 경량 룽 구체화.** 사다리 ①(co-grounding)을 non-contested로 완성. 연기된 룽③(GraphRAG)·무거운 추출이 아님을 명시.
- **ADR 0008/0014 — 합의·판례 확장.** `ConsensusService`가 `Agreed`에서 Precedent(라우팅)+ComplementEdge(접지) 대칭 방출. 1인칭 합의 종착 무변경.
- **ADR 0004 — under-claim.** concede-as-complement는 자기 지식 관련성 자기보고(권한 양보 후)라 권한 창설 0.

## 4대 + 노출 불변식 자체점검

- **미아 없음 — 보존.** 엣지는 이미 Routed된 결정의 접지 *품질*만 바꾼다. 라우팅 종착 무변경·selector `None`→단일 접지 폴백·0매칭→Unowned 무관.
- **유효하지 않은 카드는 등록되지 않는다 — 무관(보존).** admission 무변경. 엣지는 등록분 카드의 agent_id만 참조·선택시점 카드 해소는 Registry(등록분)로.
- **Authority 중앙 — 보존(급소).** `answered_by`=primary(라우팅 front) 불변. 엣지=접지 관계 신호·**권한 선언 아님**. concede-as-complement는 under-claim 자기보고(권한 양보 후)라 권한 창설 0. supporting authority 0.
- **전이 ≠ 기록 — 보존.** 엣지=학습 도메인 상태(Precedent 동렬·EdgeStore), audit 아님. 합의 *사건*은 audit이 따로.
- **노출 불변식 — 보존.** 엣지·supporting agent_id·이웃 카드 사용자 미노출(ADR 0037 결정 6 재사용·새 노출 필드 0). Routed co-grounded 답도 `answered_by`=primary + 병합 sources(레이블)만. 사용자는 정상 답 하나(이제 *완전한* 답).

## 결과

- **v0 shape 확정**: `ComplementEdge`·`EdgeStore`(포트)·`InMemoryEdgeStore`·`ConcurOnPrimary.stance`(concede stance)·`ConsensusService` 방출·`EdgeGroundingSelector`·`ChainGroundingSelector`. `AskOrg` Routed arm co-grounding 활성화.
- **슬라이스**: A(방출·저장)·B(selector)·C(AskOrg Routed arm 배선)는 게이트 내 결정론(tdd-engineer), D(프로덕션 EdgeStore 공유 배선·실 LLM Routed D1→D2 재현)는 mcp-runtime-engineer·일부 게이트 밖 수동. **A+B+C green(2026-07-07). D green(2026-07-07·게이트 내)**: (1) `build_demo`가 `InMemoryEdgeStore` 하나를 `ConsensusService`(쓰기)와 `EdgeGroundingSelector`(읽기)에 공유 주입(`DemoBundle.edge_store` 노출), (2) `knowledge_store` 주입 시 selector를 `ChainGroundingSelector((EdgeGroundingSelector, ContestedGroundingSelector))`로 교체(활성화·ON), (3) **분산 non-Delivered 처리(code-reviewer 관찰 3)**: 온라인 워커 async 수령이면 빈 co-grounded 답 대신 tracking/Pending(dispatched) 폴백(같은 ticket 재사용·미아 없음). **정직한 범위**: co-grounding은 중앙/로컬 즉답·WS 오프라인 폴백(`fallback_runtime`)에서만 실효 — **온라인 워커 async 경로는 grounding이 WS 프레임으로 전송되지 않아(결정 3 유예) 단일 접지로 폴백**(범위 밖·명시). 게이트: `pytest 2666 passed·pyright 0·ruff clean`(`tests/test_co_grounding_routed_wiring.py`). **D 게이트 밖(미실행·수동)**: 실 owner OAuth/LLM Routed D1→D2 재현(보상 합의 후 완전한 답)은 실 자격/LLM 부재로 미실행(production-no-fakes).
- **CONTEXT 용어 추가**: `ComplementEdge`(상보 엣지)·`EdgeStore`·`EdgeGroundingSelector`·`ChainGroundingSelector`·`ConcurOnPrimary.stance`.
- **북극성과의 관계**: 이 합의-소싱 엣지는 LLM-추출 엣지(무거운 북극성)와 같은 `EdgeGroundingSelector`를 먹이는 다른(더 싼) 출처다. 무거운 빌드는 여전히 트리거 대기(ADR 0036 결정 4).
- **역참조**: `docs/tasks-v0.md`·`docs/adr/0037`에 이 ADR 번호(0038) 링크.
