# 지식 신선도·변경 전파 — OKF 커밋을 변경 이벤트로 받아 그 정책에 기댄 과거 Precedent·답을 stale 플래그하고 owner 재평가 큐로 보낸다

상태: accepted (2026-06-22) · **구현 완료(tdd-engineer red→green 슬라이스 1~7 + adversarial 리뷰 수정 2건 — 707 passed/pyright 0/ruff 0)** · **ADR 0017 결정 3②·6의 본체**("정책이 바뀌면 그 정책에 기댄 *과거 Precedent·답*을 자동 재검토/무효화 플래그" — ADR 0017:45·79가 "변경 전파의 정확한 알고리즘 미해결"로 남긴 것을 이 ADR이 닫는다) · **ADR 0018 결정 6의 구체화**(커밋 SHA ↔ 신선도, "OKF 커밋 = 변경 이벤트 소스"의 자리를 본체로 채움) · ADR 0008(ConflictCase 처리함 패턴)·ADR 0012 결정 7(BackupReview 복귀 검토 루프)·ADR 0014(Manager 큐 수렴)와 정합

## 맥락 — 끊어진 고리

ADR 0017 결정 3이 "살아있는 지식"의 심장을 "신선도·변경 전파"로 못박았다. 그 전파는 두 갈래다:

> ① *미래 답* 전파는 쉽다 — 다음 답은 어차피 OKF 최신 커밋 스냅샷을 읽으므로(ADR 0018 결정 4) 자동으로 새 정책을 반영한다.
> ② *과거 Precedent·답* 전파가 어렵고 핵심이다 — 정책(OKF)이 바뀌면 그 정책에 *기대어 이미 내려진* 판례·답은 옛 지식에 묶인 채 남는다.

①은 이미 풀려 있다(ADR 0018 — HEAD 스냅샷). ②가 비어 있었다. ADR 0017:79가 "변경 전파의 정확한 알고리즘(어느 판례를 무효화하나)은 후속"으로 미뤘고, ADR 0018 결정 6이 "빌더 OKF 커밋이 변경 이벤트 *소스*"라는 자리만 열고 본체를 T7.3으로 넘겼다. **이 ADR이 그 본체 = ②를 도메인·아키텍처로 확정한다.**

핵심 제약 셋이 설계를 좁힌다:

1. **미아 없음을 깰 수 없다.** Router(`router.py:21-49`)는 `precedents.lookup(intent)` → `registry.get(primary)` 폴백으로 라우팅하고 `needs_review`를 *전혀 보지 않는다*. stale 표식이 라우팅을 바꾸면 0매칭/공백이 생겨 미아가 될 위험이 있다 — 그래서 stale은 **플래그**이지 **무효화**가 아니어야 한다(결정 6).
2. **전이 ≠ 기록.** 재평가 상태는 도메인 보관소(전이)에 두고, 재검토 행위 사실은 audit(기록)에 둔다. 둘을 섞지 않는다.
3. **Authority는 중앙.** stale 플래그·재평가는 owner 자기보고가 아니라 *중앙(커밋 이벤트→전파기)*이 단다. 무효화 결정만 owner 1인칭(처리함 처분)이다.

## 결정

### 1. 변경 이벤트 모델 = OKF 커밋이 곧 `OkfChangeEvent`, 단일 발화 지점은 `commit_okf_bundle`

OKF 커밋이 *그 번들이 방금 바뀌었다*는 변경 사건의 진실 원천이다(ADR 0018 결정 6). 발화 지점을 **`commit_okf_bundle`**(`git_gateway.py:205`) 한 곳으로 못 박는다 — 빌더 OKF 커밋의 *유일한 오케스트레이션 닫힌 루프*이고, web 핸들러(`web.py:872`)·demo가 모두 이 함수를 거친다.

- **신규 frozen `OkfChangeEvent`**(`git_gateway.py`, `CommitResult` 인접):
  `agent_id` · `new_sha`(방금 만든 커밋) · `parent_sha: str | None`(커밋 *직전* HEAD — 최초 커밋이면 None) · `changed_paths: tuple[str, ...]`(`req.files`의 path) · `author` · `committed_at`(주입 clock — 결정론).
- **발화 시그니처**: `commit_okf_bundle(req, gateway, propagator: StalenessPropagator | None = None)`. propagator가 **옵셔널 주입**이라 *기존 호출은 무영향*(기본 None = 기존 동작 그대로·하위호환). None 아니면 커밋 성공 직후 `propagator.on_okf_committed(event)` 1회 호출.
- `parent_sha`는 커밋 *직전에* `gateway.head_sha(agent_id)`를 1회 읽어 얻는다(커밋 없으면 None). `commit_bundle`이 새 커밋을 만든 *뒤* head_sha를 읽으면 새 SHA가 나오므로, 반드시 커밋 전에 읽는다.
- **노출 불변식 보존**: `CommitResult`(`sha`·`agent_id`)와 web 응답(`{sha, agent_id}`)은 *불변*이다 — `OkfChangeEvent`는 propagator로만 흐르고 web 응답·CommitResult 직렬화 계약에 끼어들지 않는다.
- **죽은 필드 명시(MVP)**: `changed_paths`·`parent_sha`는 이벤트에 *싣되* MVP 영향 식별엔 **쓰지 않는다**(아래 결정 2 — agent_id 단위 거친 매칭). 미래 정밀화(파일 단위 교차 매칭·ordering)의 *자리*다. ADR에 박아 "왜 싣고 안 쓰나"의 의도를 못 박는다.

**기각**:
- web 핸들러에서 발화(`web.py`) — web 경계 의존이 끼면 결정론 단위 테스트(`commit_okf_bundle` 순수 함수)가 막힌다. 순수 오케스트레이션이 발화점이 맞다(`validate_card_for_builder` 경계 정신).
- `CommitResult`에 이벤트 필드 추가 — web 응답·audit 직렬화 계약을 흔든다(노출 불변식). 이벤트는 별 객체로 분리.
- `SubprocessGitGateway` 본체에 실 `git diff` — 이벤트는 `agent_id`만 영향 식별에 쓰므로 실 diff가 불요하다(MVP 거친 매칭).

### 2. 영향 식별 = agent_id 번들 단위 거친 매칭(과검출 허용·놓침 0)

"어느 Precedent·답이 이 OKF 변경에 영향받나"를 **agent_id 단위로 거칠게** 식별한다 — 정밀 교차 매칭을 *의도적으로 포기*한다. 근거: 놓치면 옛 정책에 묶인 판례가 조용히 산다(②의 실패), 과검출은 owner가 처리함에서 "이건 그대로 둠"으로 흡수한다(노이즈는 owner가 닫는다). **놓침 0 > 과검출 0**.

#### ① Precedent 축
- `PrecedentStore`에 **`find_by_primary(agent_id) -> list[Precedent]`**·**`list_all() -> list[Precedent]`** 신설. `InMemoryPrecedentStore`는 `record` 시점에 `_by_primary` 역색인을 채운다.
- `event.agent_id`를 `Resolution.primary`로 둔 판례가 영향 대상이다 — `Resolution.primary`는 *agent_id 문자열*이므로(`conflict.py:21`, ConsensusService가 record하는 Resolution도 primary=agent_id·`conflict.py:262`) 직접 매칭된다.
- 이미 `needs_review=True`인 판례는 다시 플래그하지 않는다(멱등 — 결정 6의 영구 잔류와 정합).

#### ② Answer 축
- `AuditReader.records()`를 순회해 영향 답을 찾는다. **여기서 권고를 코드에 맞춰 바로잡는다**: `records()`는 `AuditEntry` 객체가 아니라 **직렬화된 dict**(`as_record()` 모양)를 돌려준다(`audit.py:172`·CONTEXT AuditReader 절). 따라서 객체 접근(`decision.disposition`·`answer.snapshot_sha`)이 아니라 **dict 접근**이다:
  - `rec["decision"]["disposition"] == "routed"` 그리고 `rec["decision"]["primary"] == event.agent_id`(`_decision_record`·`audit.py:78-86`).
  - 답 SHA는 `rec["answer"]`(없거나 None일 수 있음)의 `snapshot_sha` 키 — **`_answer_record`는 snapshot_sha가 None이면 키 자체를 넣지 않는다**(`audit.py:105-106`). 그래서 `(rec.get("answer") or {}).get("snapshot_sha")`로 안전 접근하고, 그 값이 *현 HEAD와 다르거나 None(키 부재 포함)* 이면 영향으로 본다.
- **`snapshot_sha`가 None/부재인 답도 보수적으로 포함**한다(working tree 직독·canned 경로의 답은 SHA가 없다 — `runtime.py:28`). 과검출이지만 미아·누락이 없다.
- **안정 식별자 = audit 기록순 인덱스**(0-based). audit은 append-only라 인덱스가 안정적이다(CONTEXT AuditReader 절·`audit.py:179`). `answer_id`(uuid) 신설은 안 한다(결정론·주입 부담, MVP 과함).
- **Answer 축도 멱등(커밋 반복)**: 같은 답(같은 `subject_ref`)이 이미 그 owner 처리함에 pending이면 다시 적재하지 않는다 — 같은 agent에 OKF 커밋이 연속돼도 같은 답이 처리함에 중복 쌓이지 않게(Precedent 축 `needs_review` 가드와 *동형 멱등*; `ReevalStore` 인터페이스 확장 없이 `pending_for_owner` 조회로 `(subject_kind="answer", subject_ref)` 존재 가드). 서로 다른 답(다른 `subject_ref`)은 각각 적재한다(과소검출 금지). *adversarial 리뷰가 잡은 비대칭(Precedent 축만 멱등이던 것)을 닫음.*

**MVP는 ordering 판정을 포기한다.** `snapshot_sha`는 불투명 해시라 "이 답의 SHA가 현 HEAD *보다 옛것인가*"를 부등호로 못 가린다(older-than 비교 불가). 그래서 `is_ancestor` 포트를 신설하지 *않고*, 순수 부등호 `snapshot_sha != 현 HEAD or None`만 쓴다 — 같은 HEAD로 만든 답은 영향에서 빠지고(이미 최신), 다른 SHA·SHA 없는 답은 보수적으로 영향에 든다. 정밀 ordering은 open question(아래).

**기각**:
- `snapshot_sha` 단독 매칭(agent_id 무시) — None 답을 누락하고 *어느 카드의 답인지*를 모른다(SHA만으론 agent_id를 못 역추적). decision.primary가 agent_id 매칭의 진짜 키다.
- `sources`(`Answer.sources`·자유 레이블 `list[str]`) ↔ `changed_paths`(파일 경로) 교차 매칭 — **타입 불일치로 과소검출**(자유 레이블과 파일 경로가 구조적으로 안 겹친다 → 매칭이 거의 0). source-citation 전략의 정밀 교차가 여기서 깨진다(아래 Considered Options). 그래서 `changed_paths`는 죽은 필드로만 싣는다(결정 1).
- `is_ancestor`/`rev-list` 포트 신설 — 불투명 SHA ordering은 실 git 의존이라 결정론 게이트를 깨고, MVP 과검출엔 불요(과도).
- Precedent에 "이 판례가 의존하는 OKF 파일" 역링크 — ADR 0017이 미해결로 남긴 영역(어느 *파일*이 어느 판례를 떠받치나)이고 범위 초과. agent_id 거친 매칭이 MVP 답.

### 3. 재평가 상태/큐 = `reeval.py` 신설 (BackupReview 처리함 패턴의 N번째 인스턴스)

`ConflictCaseStore`(다툼)·`BackupReviewStore`(검토)·`ManagerQueueStore`(escalation)와 **같은 처리함 포트 패턴**(Protocol + InMemory, owner 색인, 불변 전이)의 *네 번째 인스턴스*를 신규 `reeval.py`에 만든다 — 담는 값만 다르다(stale 표식 대상). CONTEXT "같은 포트 패턴"의 연장.

- **`ReevalItem`**(frozen) — 재평가 대기 한 건:
  `subject_kind: Literal["precedent", "answer"]` · `subject_ref: str`(precedent=intent 키, answer=audit 기록순 인덱스를 문자열로) · `owner_id`(Precedent=primary 카드의 owner·Answer=`rec["decision"]["owner"]`) · `agent_id` · `trigger_sha`(이 변경을 부른 커밋 = `event.new_sha`) · `flagged_at`(주입 clock) · `status: Literal["pending_review", "reviewed"]` · `review: ReevalOutcome | None` · `item_id`(default_factory uuid).
  `review_with(outcome)` — pending→reviewed 전이를 *item_id 보존한 새 인스턴스*로(`BackupReviewItem.review_with()` 동형, 파괴적 변경 X).
- **`ReevalOutcome`** sealed sum — '타입이 곧 상태'(`ConsensusOutcome`·`BackupReview` 정신):
  - Precedent 대상: **`KeepPrecedent`**(그대로 둠 — 변경이 이 판례엔 무관) | **`InvalidatePrecedent`**(이 판례를 무효로 — owner 명시 의사) | **`SupersedePrecedent`**(새 Resolution으로 갈음 — intent 키에 새 판례 record).
  - Answer 대상: **`AcknowledgeAnswer`**(옛 답이지만 그대로 유효 인정) | **`ReAnswer`**(다시 답해야 함 표식 — 실 재답변 실행은 후속).
  - 각 arm은 `by_owner`(1인칭 강제 키)를 든다.
- **`ReevalStore`**(Protocol) + **`InMemoryReevalStore`**: `add(item)` · `get(item_id)` · `pending_for_owner(owner_id)`(처리함 — `pending_for_owner`/`open_for_owner` 동형) · `mark_reviewed(item)` + append-only `history`. `BackupReviewStore`와 100% 동형.
- **`ReevalService`**: 1인칭 강제(`review.by_owner == item.owner_id` 아니면 ValueError — `BackupReviewService`·`ConsensusService` 정신). **검증 순서 = item None → 1인칭 → 멱등(reviewed면 반환)** (`BackupReviewService.review`와 동일 — *멱등을 1인칭보다 앞에 두면 이미 reviewed된 항목에 타인이 호출해도 무검증 통과해 권한이 누설된다*; adversarial 리뷰가 이 순서 역전을 잡았다). pending→reviewed 전이만 수행, audit 기록은 호출자(전이 ≠ 기록).
- **`StalenessPropagator`**(`on_okf_committed(event)`): 결정 2의 두 축(Precedent `find_by_primary` + Answer audit 순회)으로 영향을 식별해 `ReevalStore.add`로 적재한다. 새 통지 인프라 0 — 적재가 곧 처리함 nudge(결정 5).

**DDD 주의 (ADR에 명시·open question)**: `subject_kind` 문자열 판별자 + untyped `subject_ref`(str)는 우리의 'sealed sum' 관용구(`RoutingDecision`·`ConsensusOutcome` — 타입 자체가 판별자)에서 *약하게 이탈*한다. MVP는 허용하되(두 종류·단순 ref라 타입 분기 비용 > 이득), `ReevalSubject` sealed sum(`PrecedentSubject(intent)` | `AnswerSubject(audit_index)`)으로 분리하는 것을 open question으로 남긴다.

**기각**:
- `PrecedentStore`에 `invalidate`/`delete` 자동 신설 — 커밋이 판례를 *자동 무효화*하면 미아 위험(라우팅 0매칭). 무효화는 owner 명시(`InvalidatePrecedent`) 후만(결정 6).
- audit 되쓰기(답에 stale 마킹) — audit은 append-only(`audit.py` JSONL append)다. 옛-SHA=stale은 *audit에 쓰지 않고* `ReevalItem`이 든다.
- 답마다 별 store — 과도. 한 `ReevalStore`가 두 subject_kind를 든다(처리함 한 면).
- `ManagerQueueStore` 통합 — 재평가는 owner 1인칭 대상(자기 판례·자기 답)이라 처리함(owner 귀속)이 맞다. Manager 큐는 owner *위* 사람 escalation이라 색인 키가 다르다.

### 4. 신선도 신호의 위치·노출 = AgentCard.last_reviewed_at는 SSOT 그대로, Precedent에 stale 2필드 신설

- **`AgentCard.last_reviewed_at`**(`agent_card.py:12`·`date`·필수)는 *그대로* — OKF 커밋이 이 카드 필드를 *자동 갱신하지 않는다*(카드는 admission 경계·PR 채널·ADR 0018 결정 1). 커밋된 `agent_id`는 stale nudge의 *키*로만 쓰인다.
- **`Precedent`에 `needs_review: bool = False`·`last_flagged_at: datetime | None = None` 신설**(`conflict.py`·frozen·하위호환 기본값). 변경 전파기가 이 두 필드로 판례에 stale을 표식한다. `recorded_at`은 불변.
- **`status: Literal["valid", ...]`는 안 쓴다** — 'valid'가 admission 어휘("유효하지 않은 카드는 등록되지 않는다")와 충돌·자기모순이다. stale은 *재검토 대상*이지 *무효*가 아니므로 boolean `needs_review`가 정확하다.
- **`Answer`에 새 필드 0** — audit append-only 불변(`audit.py`)이라 답에 stale 마킹을 못 박는다. "옛 SHA = stale"의 표현은 `ReevalItem`이 든다(audit은 불변).
- **노출 경계**: `needs_review`·`last_flagged_at`·`ReevalItem`·`trigger_sha`는 *운영 면만*(처리함·모니터링·audit) 노출한다. 사용자向 `Answered`(`ask_org.py:26-31` — text/answered_by/mode/sources만)엔 **미노출** — `snapshot_sha`조차 안 싣는 경계와 동형(노출 불변식, ADR 0018 결정 4·0011).

### 5. owner nudge = 처리함 pull 재사용 (Owner Inbox 세 번째 탭)

새 통지 인프라를 *만들지 않는다* — owner 처리함 pull을 재사용한다. `pending_for_owner(owner_id)`가 곧 nudge다: 처리함에 ①open ConflictCase(다툼 합의) ②BackupReviewItem(백업 답 검토)와 *나란히* ③재평가 대기(`ReevalItem`)를 둔다. **Owner Inbox가 두 면 → 세 면(탭)으로 확장**된다(CONTEXT Inbox 절 갱신).

- 재검토 *행위 사실*은 호출자가 `AuditLog.record`로 남긴다(전이 ≠ 기록 — `record_review`가 audit에 아무것도 안 남기는 `BackupReview`와 달리, 재평가는 호출자 판단; MVP는 전이만이라 audit 자리만).
- **실시간 push**(Slack/메일/MCP 알림)는 ADR 0017 결정 6④·T7.4 영역 — 여기선 pull(조회)만. push는 분산 인프라 재사용으로 후속.
- **owner 비응답 ReevalItem의 종착**: 영영 처리 안 되면 미아처럼 떠돈다 → timeout → Manager escalation으로 종착(미아 없음). MVP는 *자리만*(timeout 임계·실 escalation은 후속) — `ReevalItem`이 처리함에 영구 잔류해 가시성은 보장된다.

### 6. 무효화 vs 플래그 = 플래그(needs_review) 채택, 자동 무효화 기각

stale은 **플래그**(`needs_review=True`)이지 **무효화가 아니다**. 핵심:

- **stale ≠ 무효화.** stale 판례는 store에 *그대로 남고*, Router lookup이 `needs_review`를 *보지 않으므로*(`router.py:23-38`) **계속 라우팅된다** — 0매칭/공백이 0(미아 없음 보존). "옛 정책 기준이라도 담당은 그 사람"이 맞다.
- **무효화는 owner 명시 후만.** owner가 처리함에서 `InvalidatePrecedent`를 명시 처분한 *뒤에만* 무효화한다 — 그것도 "store에서 삭제"가 아니라 append-only로 표현한다.
- **append-only 무효화 표현**: `needs_review=True`는 영구 잔류한다(플래그는 안 지운다). `SupersedePrecedent` 시 새 `Resolution`을 `record`하면 intent 키가 새 판례로 덮인다(`InMemoryPrecedentStore.record`의 기존 동작 — `_precedents[intent]` 갱신·`history` append). 즉 무효화·갱신 모두 *삭제 없이* 표현된다.

**ReevalOutcome arm 명명 확정**(권고가 "InvalidatePrecedent vs SupersedePrecedent / ReAnswer vs AcknowledgeAnswer"를 못박으라 했다):
- Precedent: `KeepPrecedent`(무관) / `InvalidatePrecedent`(무효 의사 — 라우팅에서 제외할 명시 의사·실 제외 메커니즘은 후속) / `SupersedePrecedent`(새 Resolution으로 갈음 — record). 셋 망라.
- Answer: `AcknowledgeAnswer`(옛 답 그대로 유효) / `ReAnswer`(재답변 필요 표식 — 실 재실행 후속). 둘 망라.

## Considered Options

설계 리서치가 3전략을 냈다. 채택은 **conservative-stale-flag 베이스 + agent-id-match의 역색인 흡수**다.

### 영향 식별 전략 (결정 2)
- **conservative-stale-flag(보수적 stale 플래그) — 베이스(선택)**: agent_id 거친 매칭 + 과거 답 audit 순회 + None 답 보수 포함. 놓침 0, 과검출은 owner가 처리함에서 닫는다. 미아 없음·전이≠기록과 정합.
- **agent-id-match(역색인) — 흡수(선택)**: `find_by_primary` 역색인을 베이스에 *흡수*한다(record 시점 `_by_primary`). Precedent 축 식별의 O(1) 조회. 베이스와 충돌 없이 합쳐진다.
- **source-citation(출처 인용 정밀 교차) — 기각**: `sources`(자유 레이블 `list[str]`) ↔ `changed_paths`(파일 경로) 교차로 *파일 단위 정밀* 영향 식별. **타입 불일치로 과소검출** — 자유 레이블과 파일 경로가 구조적으로 안 겹쳐 매칭이 거의 0이 된다(놓침 폭증 = ②의 실패). `changed_paths`·`parent_sha`는 이벤트에 *싣되* MVP 매칭엔 안 쓰는 미래 자리로만 보존한다.

### 무효화 표현 (결정 6)
- **플래그(needs_review) + owner 명시 후 무효화(선택)**: 미아 없음 보존(라우터가 플래그를 안 봐 계속 라우팅), 무효화는 1인칭 후만·append-only.
- **커밋 시 자동 무효화/삭제(기각)**: 라우팅 0매칭/공백 → 미아 위험. Authority 중앙이되 *무효화 판단*까지 중앙 자동화하면 owner 거버넌스(ADR 0017 — 담당 결정은 owner)를 침범.

### 재평가 보관 위치 (결정 3)
- **`reeval.py` 신규 + ReevalStore 별 포트(선택)**: BackupReview 패턴 N번째 인스턴스. 담는 값이 다르면 별 store(ConflictCaseStore가 BackupReviewStore와 갈리는 판단과 동형).
- **ManagerQueueStore 통합(기각)**: 재평가는 owner 1인칭 대상이라 처리함(owner 귀속)이 맞다 — Manager 큐(색인 키 manager_id)와 귀속 주체가 다르다.

## Consequences

- **`OkfChangeEvent` + `commit_okf_bundle(propagator=None)` 신설** — propagator 옵셔널 주입이라 기존 호출 무영향(632 게이트 보존). `CommitResult`·web 응답 불변(노출 불변식).
- **`Precedent`에 `needs_review`·`last_flagged_at` 추가** — frozen·하위호환 기본값(기존 record/lookup 무변경). Router lookup은 *이 필드를 안 본다*(미아 없음 회귀 0).
- **`PrecedentStore`에 `find_by_primary`·`list_all`·`flag_stale` + InMemory `_by_primary` 역색인** — 기존 `record`/`lookup` 계약 보존, 추가만.
- **`reeval.py` 신규 모듈** — `ReevalItem`·`ReevalOutcome`(sealed sum)·`ReevalStore`/`InMemoryReevalStore`·`ReevalService`·`StalenessPropagator`. BackupReview 패턴 복제(새 메커니즘 0).
- **Owner Inbox 두 면 → 세 면** — CONTEXT Inbox 절 갱신(①다툼 ②검토 ③재평가). web 처리함은 후속 슬라이스(이번은 store·서비스까지).
- **불변식 영향 없음** — 미아 없음(라우터가 stale 플래그를 안 봐 계속 라우팅)·Authority 중앙(stale은 중앙 전파기가 달고, 무효화 판단만 owner 1인칭)·전이≠기록(재평가는 ReevalStore 전이·재검토 행위는 audit)·등록 무결성(카드 무변경·`last_reviewed_at` SSOT 그대로)·노출 불변식(stale 필드는 운영 면만·Answered 미노출)은 그대로.
- **갱신 대상**: CONTEXT(Inbox 두면→세면·`OkfChangeEvent`·`StalenessPropagator`·`ReevalItem`/`ReevalStore`/`ReevalService`·`Precedent` needs_review 신규 용어), PRD §5(T7.3 설계·shape), TRD §4/§8(reeval 포트·OkfChangeEvent), tasks T7.3.

## Open Questions (후속)

- **ordering 정밀화** — 불투명 SHA의 older-than 비교가 불가해 MVP는 `!= HEAD or None` 부등호만. `is_ancestor`/`rev-list` 포트는 실 git 의존이라 결정론 게이트와 상충 → 후속(파일 단위 ordering·`parent_sha` 체인 활용).
- **`ReevalSubject` sealed sum 분리** — `subject_kind` 문자열 판별자 + untyped `subject_ref`는 'sealed sum' 관용구에서 약하게 이탈. `PrecedentSubject`/`AnswerSubject` 분리 검토.
- **과검출 노이즈** — agent_id 거친 매칭은 무관 판례·답까지 큐에 넣는다. `changed_paths` 정밀화(결정 1 죽은 필드)로 후속 좁힘. None 답(SHA 부재)이 많으면 큐 폭증 임계가 필요.
- **카드 노후 nudge** — `last_reviewed_at`가 오래된 카드를 owner에 갱신 nudge하는 별 신호(ADR 0017 결정 3 "stale 지식 갱신 nudge")는 자리만(이 ADR은 판례·답 전파에 집중).
- **실시간 push 통지** — 처리함 pull → Slack/메일/MCP push는 ADR 0017 결정 6④·T7.4.
- **owner 비응답 ReevalItem timeout → Manager escalation** — 종착 메커니즘은 자리만(임계·실 escalation 후속). MVP는 처리함 영구 잔류로 가시성만 보장.
- **`InvalidatePrecedent` 실 제외 메커니즘** — 무효 의사 표식 후 라우팅에서 *실제로 빼는* 길(append-only 표현)은 후속(이 ADR은 표식까지).
