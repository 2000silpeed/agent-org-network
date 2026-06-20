# 미해소 다툼을 ConflictCase로 저장하고, 1인칭 합의를 단일 축 표(ConcurOnPrimary)로 모델링

상태: accepted (2026-06-20)

ADR 0002는 충돌(Overlap/Gap)을 "사람이 합의 → append-only Precedent로 학습"하는 루프로 정했고, T4.1이 그 후반부(Resolution → Precedent → 라우터 자동 적용)를 구현했다. T4.2는 그 전반부 — **Contested가 어떻게 사람 합의에 도달하는가** — 를 채운다. 두 가지 되돌리기 어려운 결정이 필요하다: (1) 미해소 다툼을 무엇으로 저장하는가, (2) "1인칭 합의"를 어떤 입력으로 표현하는가.

## 결정

### 1. 미해소 다툼의 저장 단위 = `ConflictCase`

`Contested`(후보 ≥2)는 라우터의 *순간 판정*일 뿐 어디에도 머물지 않는다. 후보 Owner들이 처리함에서 1인칭으로 합의하려면 그 다툼이 **조회 가능한 상태로 저장**돼야 한다. 그래서 `ConflictCase`를 둔다 — `intent` + 후보들(`Candidate(agent_id, owner)`) + 원문 `question` + 상태(`open`/`resolved`) + 생성 시각(주입 clock).

- **후보는 `(agent_id, owner)` 식별자만** 보관한다. `Contested.candidates`는 `AgentCard` 객체지만, 케이스는 라우터向(intent 색인)·처리함向(owner 색인) 조회가 본질이고 카드 본문은 Registry가 출처다 — 카드 전체를 안으면 Owner 교체·카드 편집 시 stale. `owner`를 함께 드는 이유는 **Owner별 처리함 조회**(`open_for_owner`)가 핵심 데이터 원천이기 때문.
- **question 원문을 보관**한다. Owner가 처리함에서 "무엇을 두고 다투는지" 맥락을 봐야 1인칭 판단을 내린다. (audit이 질문을 기록하지만, 그건 운영자向 절차 기록이지 처리함 조회 키가 아니다 — 전이 ≠ 기록.)
- **상태 전이는 불변 + 새 인스턴스.** `case.resolve(resolution)`이 `case_id`·후보를 보존한 resolved 케이스를 새로 돌려준다. RoutingDecision·OrgReply의 "타입이 곧 상태" 정신과 정합 — 다만 open↔resolved는 같은 케이스의 *수명*이라 별 타입(sum)이 아니라 `status` + nullable `resolution` 필드로 둔다(case_id 동일성이 보존돼야 처리함에서 추적 가능).

### 2. 1인칭 합의 = 단일 축 표 `ConcurOnPrimary`

PRD §7.3은 "후보 Owner들이 각자 화면(1인칭)에서 합의"를 요구한다. MVP 최소 단순화안으로 **후보 중 한 명을 primary로 지목하는 한 표**(`ConcurOnPrimary(by_owner, on_agent, rationale)`)를 채택한다.

- claim("내가 맡는다" = 자기 카드 지목)과 concede("쟤가 맡아" = 남 지목)를 **같은 한 축**으로 환원한다. 둘 다 "primary는 누구"라는 질문의 답일 뿐이다. 찬반 2축·라운드·코멘트 스레드를 두지 않는다 — 지금 필요한 건 "**전원이 한 명을 가리켰나**"뿐.
- **전원 합의 = 모든 후보 Owner가 같은 `on_agent`를 지목**. 그러면 그 agent_id가 `Resolution.primary`가 된다. (양보로 후보가 1명으로 좁혀지는 변형안도 가능하나, "전원이 한 명 지목"이 더 단순하고 1인칭 정신에 직접적 — 모두가 명시적으로 자기 입장을 낸다.)

### 3. 합의 결과 = sealed sum `ConsensusOutcome`

표를 모아 합의를 시도한 결과는 세 결말이다(타입이 곧 상태):
- `Agreed(resolution, precedent)` — 전원 일치. Resolution 산출, `PrecedentStore.record`로 흘려 케이스 closed.
- `StillOpen(case, pending_owners)` — 표가 덜 모였다. 케이스 open 유지, 처리함에 남음.
- `Deadlocked(case, reason)` — 표가 갈렸다(교착). **합의 실패 자리만 남기고**, Manager escalation 처리는 T5.2(Manager 큐)로 미룬다.

### 4. 저장소 포트 = `ConflictCaseStore` (audit·precedent와 같은 패턴)

open ConflictCase 보관·조회를 `ConflictCaseStore` Protocol + `InMemoryConflictCaseStore`로 둔다 — `AuditLog`·`PrecedentStore`와 동일한 포트 패턴. `open_for_owner`(처리함)·`open_for_intent`(중복 open 방지)·`mark_resolved`(open에서 빼고 history에 append) 메서드. **별 모듈을 만들지 않고 `conflict.py`에** 둔다 — ConflictCase는 Conflict 도메인의 "미해소 상태"이고 Resolution·Precedent와 같은 바운디드 개념이라, 처리함 조회 메서드 하나 때문에 도메인을 두 파일로 흩지 않는다(과도한 분리 회피).

## Consequences

- **전이 ≠ 기록 유지.** Contested 발생 시 ConflictCase 생성은 *도메인 이벤트*(ConflictCaseStore)이고, audit 기록은 별개로 계속 흐른다. 케이스 저장소는 미해소 상태의 도메인 보관소지 절차 로그가 아니다.
- **중복 open 방지 정책.** 같은 intent로 이미 open된 케이스가 있으면 새로 만들지 않는다(`open_for_intent`로 선조회) — 같은 다툼이 질문마다 케이스를 양산하지 않게. 반복되는 같은 intent의 Contested는 *하나의* 케이스로 모인다.
- **Authority 중앙 원칙 불변.** 합의는 카드 자기보고가 아니라 후보 Owner들의 1인칭 표 → Resolution → Precedent. 권한 선언은 여전히 중앙(판례가 곧 중앙 누적 규칙)이다.
- **합의 성공이 T4.2의 핵심.** 실패(Deadlocked)→Manager는 도메인에 자리만 두고 T5.2로 넘긴다. 미아 없음 불변식은 유지 — open 케이스는 영영 사라지지 않고 처리함/이력에 남는다.
- ConflictCase·ConcurOnPrimary는 그대로 **결정론 테스트 케이스**가 된다(주입 clock·고정 case_id 시드). 합의→Precedent 루프는 라우팅 회귀 스위트로 이어진다(ADR 0002·0003 정합).
