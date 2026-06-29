# 지식 신선도·변경 전파 — OKF 커밋을 변경 이벤트로 받아 그 정책에 기댄 과거 Precedent·답을 stale 플래그하고 owner 재평가 큐로 보낸다

상태: accepted (2026-06-22) · **구현 완료(tdd-engineer red→green 슬라이스 1~7 + adversarial 리뷰 수정 2건 — 707 passed/pyright 0/ruff 0)** · **T8.4(a) 정밀화(2026-06-24): 결정 7 — `ReevalSubject` sealed sum 분리(open question 해소)** · **T8.4(d) 보강(2026-06-24): 결정 6 — `InvalidatePrecedent` 실 라우팅 제외(open question 해소·append-only 무효 표식 + Router 제외 안 B)** · **ADR 0017 결정 3②·6의 본체**("정책이 바뀌면 그 정책에 기댄 *과거 Precedent·답*을 자동 재검토/무효화 플래그" — ADR 0017:45·79가 "변경 전파의 정확한 알고리즘 미해결"로 남긴 것을 이 ADR이 닫는다) · **ADR 0018 결정 6의 구체화**(커밋 SHA ↔ 신선도, "OKF 커밋 = 변경 이벤트 소스"의 자리를 본체로 채움) · ADR 0008(ConflictCase 처리함 패턴)·ADR 0012 결정 7(BackupReview 복귀 검토 루프)·ADR 0014(Manager 큐 수렴)와 정합 · **ADR 0030(owner측 저작 토폴로지·크로스머신 fan-out)이 이 ADR의 "발화 단일 지점"(`commit_okf_bundle`)을 *머신별 단일*로 재해석**(supersede 아님·확장): owner측=commit이 reindex 발화(이 ADR 결정 1 단일·owner 머신), 중앙측=index 수용(`accept_published_index`가 더 새 `generated_at` 수용)이 reeval 발화(중앙 머신·중앙이 owner commit·git을 못 봄=비소유). 크로스머신 이벤트 중복 0(한 변경이 두 번 reeval되거나 누락 안 됨). `StalenessPropagator`·`ReevalSubject`·reeval 큐·1인칭 처분은 무변경 — 발화 *지점*만 reeval 축에서 commit→index 수용으로 옮김(단일 머신 배포는 WS 루프백으로 동일 구조).

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

**DDD 정밀화 (T8.4(a)·2026-06-24 — 아래 open question "해소됨")**: MVP는 `subject_kind`(str 판별자) + untyped `subject_ref`(str) 두 필드였고, 이는 우리의 'sealed sum' 관용구(`RoutingDecision`·`EscalationSource` — 타입 자체가 판별자)에서 *약하게 이탈*했다. T8.4(a)가 이를 `ReevalSubject` **sealed sum**으로 분리해 닫는다 — `subject: ReevalSubject` 단일 필드, 타입이 곧 대상 판별자(`match`+`assert_never` 망라). 아래 [결정 7]을 보라.

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
- Precedent: `KeepPrecedent`(무관) / `InvalidatePrecedent`(무효 의사 — 라우팅에서 제외할 명시 의사·실 제외 메커니즘은 아래 T8.4(d)) / `SupersedePrecedent`(새 Resolution으로 갈음 — record). 셋 망라.
- Answer: `AcknowledgeAnswer`(옛 답 그대로 유효) / `ReAnswer`(재답변 필요 표식 — 실 재실행 후속). 둘 망라.

**`InvalidatePrecedent` 실 라우팅 제외 (T8.4(d)·2026-06-24 — open question "해소됨")**: MVP는 `InvalidatePrecedent`를 *무효 의사 표식*까지만 두고(ReevalStore 전이만·라우터는 그 판례를 계속 lookup) 실 제외를 후속으로 미뤘다. T8.4(d)가 그 길을 닫는다 — owner 명시 무효화 후 그 판례를 *실제로 라우팅에서 빼되 append-only*(store 삭제 X).

- **무효 표식 = append-only frozen 필드**(`Precedent`): `invalidated: bool = False`·`invalidated_at: datetime | None = None`·`invalidated_by: str | None = None`(`needs_review`·`last_flagged_at`가 추가된 패턴 그대로·하위호환 기본값). `PrecedentStore.invalidate(intent, by_owner, at) -> Precedent | None`이 이 표식을 단다 — `flag_stale`과 같은 형태(판례 없으면 None·이미 invalidated면 멱등·새 인스턴스 교체+history append+`_by_primary` 동기화). **store 삭제 금지** — `_precedents`/`_by_primary`에서 *제거하지 않으므로* `list_all`·`find_by_primary`(운영 면 열람·영향 식별)에 그대로 남는다.
- **`needs_review`(stale)와 `invalidated`(무효)는 독립 축.** stale은 *재검토 대상*이지 *무효*가 아니다(stale ≠ 무효 — 결정 6 본문). 둘은 서로 덮어쓰지 않는다(이미 stale이어도 invalidate는 invalidated만 켠다). Router는 `needs_review`는 *여전히 안 보고*(stale 판례는 계속 라우팅·미아 없음), `invalidated`만 본다.
- **제외 지점 = Router 단일 지점(안 B 채택, 안 A 기각).** 두 안을 검토했다.
  - 안 A — `lookup(intent)`이 invalidated 판례면 `None` 반환(모든 routing-via-lookup 호출자가 자동 제외).
  - 안 B — `lookup`은 기록된 판례를 그대로 반환하고, **Router가 `p.invalidated`면 판례 경로를 건너뛰고 분류기 경로로 폴백**(`needs_review`를 Router가 해석하는 것과 *대칭* — 판례 플래그를 라우팅에 어떻게 해석할지 결정하는 단일 지점).
  - **안 B 선택**: `lookup` 호출자를 grep으로 확인하니 *라우팅 경로는 Router 하나*이고(`router.py:24`) 나머지 9개 호출자는 전부 테스트의 "무엇이 기록됐나" 단언이다(`test_conflict.py`·`test_consensus.py`·`test_reeval.py`·`test_manager_queue.py`). 안 A는 `lookup`이 "기록 읽기"에서 "라우팅 가능 판례 읽기"로 의미가 바뀌어 그 단언들에 회귀를 낸다(무효화 후 `lookup`이 None을 돌려 기록 확인이 깨짐). 안 B는 `lookup`을 *순수 읽기*("이 intent에 무엇이 기록됐나" — `find_by_primary`/`list_all`과 일관)로 두고 Router가 "이 판례로 라우팅할까"를 결정한다(관심사 분리·`needs_review` 처리와 대칭). `StalenessPropagator`는 `find_by_primary`를·`ConsensusService`는 `record`를 쓰므로 lookup 회귀 영향 없음.
- **미아 없음 보존(THE 핵심).** 무효화는 *판례 단축경로*만 끊는다 — 그 아래 분류기 경로(`router.py:39-49`)는 그대로 살아 항상 종착한다: 그 intent를 domains에 가진 카드가 1개면 `Routed`·≥2면 `Contested`·**0이면 `Unowned`(루트 User escalation)**. 어떤 경우에도 "라우팅이 아무것도 안 돌려주는" 미아 상태가 생기지 않는다.
- **와이어링 = `ReevalService`가 `InvalidatePrecedent`→`precedents.invalidate`(Consensus`Agreed`→`record`의 대칭).** `ReevalService.__init__`에 옵셔널 `precedents: PrecedentStore | None = None`·`clock: Clock = default_clock` 주입(미주입이면 기존 동작·하위호환·게이트 보존 — `commit_okf_bundle(propagator=None)` 정신). `review`는 전이 *뒤* outcome이 `InvalidatePrecedent`*이고* subject가 `PrecedentSubject`인 짝일 때만 `precedents.invalidate(subject.intent, by_owner, at=clock())` 호출. **subject↔outcome 축 정합**: 두 축은 독립(결정 7)이라 어긋난 짝(예: `AnswerSubject`에 `InvalidatePrecedent`)이 *타입상* 가능하다 — ReevalService는 어긋난 짝을 *무시*한다(에러 X). 근거: `review`의 1차 책임은 상태 전이(reviewed 종착)이고, 어긋난 짝에 에러를 던지면 정상 전이까지 막혀 그 항목이 처리함에서 영영 reviewed가 안 되는 미아 위험이 생긴다. 축 정합은 발화 지점(`StalenessPropagator`)이 `PrecedentSubject`엔 Precedent-축 표식만 적재하도록 구조적으로 보장하는 게 1차 방어선이고, ReevalService는 방어적으로 *맞는 짝만 처분*한다. `KeepPrecedent`·`SupersedePrecedent`·`AcknowledgeAnswer`·`ReAnswer`의 실 실행은 이번 범위 밖(d는 InvalidatePrecedent 실 제외만)이나 와이어링이 그들을 깨지 않는다(부작용 없이 전이만).
- **멱등·전이 ≠ 기록.** `invalidate`는 멱등(이미 invalidated면 그대로). 무효화는 ReevalStore 전이(이미)와 PrecedentStore 표식(신규)이지 *기록*(audit)이 아니다 — 재검토 행위의 audit 기록은 호출자 책임(기존 정신 보존).
- **propagator 재적재 가드(code-reviewer Minor 보강).** `StalenessPropagator.on_okf_committed`의 Precedent 축 가드를 `if precedent.needs_review:` → `if precedent.needs_review or precedent.invalidated:`로 넓힌다. owner가 무효화로 *라우팅에서 뺀* 판례를, 같은 agent_id OKF 커밋이 왔다고 다시 재평가 큐에 올리면 "owner가 일부러 닫은 걸 또 묻는" 처리함 노이즈다. `needs_review`(이미 stale 표식됨)와 `invalidated`(이미 무효 처분됨) 둘 다 "이미 처리됨"이라 재적재에서 뺀다(미아 위험 0 — 라우팅엔 영향 없고 재평가 nudge만 안 띄움).
- **Authority 중앙·노출 불변식.** 무효화는 owner 1인칭 처분(`by_owner` 강제는 ReevalService의 1인칭 검증이 이미)이지 카드 자기보고가 아니다. `invalidated`·`invalidated_at`·`invalidated_by`는 운영 면 메타라 사용자向 `Answered`엔 미노출(`needs_review`·`trigger_sha`와 동형 경계).

### 7. 재평가 대상 = `ReevalSubject` sealed sum (T8.4(a)·2026-06-24 — MVP 약한 이탈 정밀화)

MVP의 `subject_kind`(`Literal["precedent","answer"]`) + untyped `subject_ref: str`(precedent=intent / answer=`str(idx)`)를 **`ReevalSubject` sealed sum**으로 분리한다 — 타입이 곧 *대상* 판별자(`RoutingDecision`·`EscalationSource`·`ConsensusOutcome`과 같은 관용구). 결정 3의 DDD 주의가 남긴 open question을 닫는 *내부 값 객체 타입 정합*이다(되돌리기 어려운 신규 아키텍처가 아니라 기존 결정의 정밀화라 새 ADR 0023 대신 이 ADR을 갱신).

- **두 arm**(frozen dataclass):
  - **`PrecedentSubject(intent: str)`** — 대상 = 과거 Precedent. `intent`가 곧 `PrecedentStore` 키(StalenessPropagator ① 축).
  - **`AnswerSubject(audit_index: int)`** — 대상 = 과거 답. `audit_index`는 audit 기록순 0-based 인덱스. **int로 든다**(이전 MVP는 `str(idx)`였다 — index는 정수가 자연이고, dedup 가드가 `== idx` int 비교라 표현이 깔끔해진다).
  - `ReevalSubject = PrecedentSubject | AnswerSubject` union 별칭.
- **`ReevalItem`의 두 필드 → 단일 `subject: ReevalSubject`**. 대상 식별은 `match item.subject` / `isinstance`로 분기. `subject`는 기본값 없는 필드라 기존 위치(맨 앞) 유지·`item_id`/`status`/`review` 기본값과 정합.
- **dedup 가드 동치 변환**: Answer 축 멱등 가드(결정 2②)가 `p.subject_kind == "answer" and p.subject_ref == str(idx)`(문자열 비교) → **`isinstance(p.subject, AnswerSubject) and p.subject.audit_index == idx`**(타입+int 비교)로 정밀화. 의미 동일·표현 명료.
- **통지 멱등 키 도출 = `ReevalSubject.notification_ref() -> str`** (m1 우회 *구조적* 해소): 발화 지점(`_push_reeval_notification`)이 이전엔 `f"precedent:{intent}"`·`f"answer:{idx}"` prefix를 *손으로* 붙였다 — intent가 순수 숫자("0")면 Answer 인덱스(0)와 `Notification` 멱등 키가 우연 충돌할 수 있어 prefix가 그 *우회*였다(ADR 0022 m1). 이제 각 arm이 자기 prefix를 *타입에서 도출*한다: `PrecedentSubject.notification_ref() == "precedent:{intent}"`, `AnswerSubject.notification_ref() == "answer:{audit_index}"`. 발화 지점은 `subject.notification_ref()`만 부른다 — 우회가 손 코드가 아니라 타입 구조로 보장된다.
- **경계(스코프)**: `notify.py`의 `Notification.subject_ref`는 *건드리지 않는다* — 그건 reeval과 무관한 *통지 메시지 식별자*(Literal kind + str ref)이고 별개 개념이다. `notification_ref()`가 낳는 문자열은 *기존 prefix와 동일*("precedent:0"/"answer:0")이라 통지 계약·슬라이스 C 테스트는 무변경. `ReevalOutcome`(결과 축 — 결정 6)과 `ReevalSubject`(대상 축)는 **독립**이다(섞지 않는다).
- **불변식 영향 0**: 전이≠기록(ReevalStore 보관 그대로)·미아 없음(라우터는 stale 안 봄)·노출 불변식 모두 무변경. 어댑터/도메인 전이 추가 0 — 순수 타입 정합.

**기각**: `subject_ref` int 유지(Answer만 str→int) — 두 축이 다른 식별자를 *untyped 한 필드*에 욱여넣는 약한 이탈은 그대로다. 타입 분기를 1급으로 올려야 `match` 망라가 컴파일 타임에 누락을 잡는다(우리 관용구의 핵심 이득).

## Considered Options

설계 리서치가 3전략을 냈다. 채택은 **conservative-stale-flag 베이스 + agent-id-match의 역색인 흡수**다.

### 영향 식별 전략 (결정 2)
- **conservative-stale-flag(보수적 stale 플래그) — 베이스(선택)**: agent_id 거친 매칭 + 과거 답 audit 순회 + None 답 보수 포함. 놓침 0, 과검출은 owner가 처리함에서 닫는다. 미아 없음·전이≠기록과 정합.
- **agent-id-match(역색인) — 흡수(선택)**: `find_by_primary` 역색인을 베이스에 *흡수*한다(record 시점 `_by_primary`). Precedent 축 식별의 O(1) 조회. 베이스와 충돌 없이 합쳐진다.
- **source-citation(출처 인용 정밀 교차) — 기각**: `sources`(자유 레이블 `list[str]`) ↔ `changed_paths`(파일 경로) 교차로 *파일 단위 정밀* 영향 식별. **타입 불일치로 과소검출** — 자유 레이블과 파일 경로가 구조적으로 안 겹쳐 매칭이 거의 0이 된다(놓침 폭증 = ②의 실패). `changed_paths`·`parent_sha`는 이벤트에 *싣되* MVP 매칭엔 안 쓰는 미래 자리로만 보존한다.

### 무효화 표현 (결정 6)
- **플래그(needs_review) + owner 명시 후 무효화(선택)**: 미아 없음 보존(라우터가 플래그를 안 봐 계속 라우팅), 무효화는 1인칭 후만·append-only.
- **커밋 시 자동 무효화/삭제(기각)**: 라우팅 0매칭/공백 → 미아 위험. Authority 중앙이되 *무효화 판단*까지 중앙 자동화하면 owner 거버넌스(ADR 0017 — 담당 결정은 owner)를 침범.

### 무효 라우팅 제외 지점 (결정 6·T8.4(d))
- **Router가 `p.invalidated` 체크(안 B·선택)**: `lookup`은 순수 읽기로 기록된 판례를 그대로 반환하고 Router가 무효 판례면 판례 경로를 건너뛰고 분류기 폴백. `needs_review` 처리와 *대칭*(판례 플래그→라우팅 해석을 Router 단일 지점에 둠)·관심사 분리(`lookup`은 `find_by_primary`/`list_all`과 일관된 읽기). lookup 호출자가 라우팅 경로는 Router 하나·나머지는 테스트 기록 단언이라 회귀 없음.
- **`lookup`이 invalidated면 None 반환(안 A·기각)**: `lookup`이 "기록 읽기"에서 "라우팅 가능 판례 읽기"로 의미가 바뀌어 기록 확인 테스트 단언에 회귀를 내고, 운영 면 읽기(무엇이 기록됐나)와 라우팅 판단(이걸로 라우팅할까)을 한 메서드에 섞는다(관심사 혼재).

### 재평가 보관 위치 (결정 3)
- **`reeval.py` 신규 + ReevalStore 별 포트(선택)**: BackupReview 패턴 N번째 인스턴스. 담는 값이 다르면 별 store(ConflictCaseStore가 BackupReviewStore와 갈리는 판단과 동형).
- **ManagerQueueStore 통합(기각)**: 재평가는 owner 1인칭 대상이라 처리함(owner 귀속)이 맞다 — Manager 큐(색인 키 manager_id)와 귀속 주체가 다르다.

## Consequences

- **`OkfChangeEvent` + `commit_okf_bundle(propagator=None)` 신설** — propagator 옵셔널 주입이라 기존 호출 무영향(632 게이트 보존). `CommitResult`·web 응답 불변(노출 불변식).
- **`Precedent`에 `needs_review`·`last_flagged_at` 추가** — frozen·하위호환 기본값(기존 record/lookup 무변경). Router lookup은 *이 필드를 안 본다*(미아 없음 회귀 0).
- **`PrecedentStore`에 `find_by_primary`·`list_all`·`flag_stale` + InMemory `_by_primary` 역색인** — 기존 `record`/`lookup` 계약 보존, 추가만.
- **`reeval.py` 신규 모듈** — `ReevalSubject`(sealed sum — `PrecedentSubject`/`AnswerSubject`·T8.4(a))·`ReevalItem`(`subject: ReevalSubject` 단일 필드)·`ReevalOutcome`(sealed sum)·`ReevalStore`/`InMemoryReevalStore`·`ReevalService`·`StalenessPropagator`. BackupReview 패턴 복제(새 메커니즘 0).
- **`ReevalSubject` sealed sum(T8.4(a)·결정 7)** — `subject_kind`+untyped `subject_ref` 두 필드를 단일 `subject: ReevalSubject`로 교체(타입이 곧 대상 판별자). `AnswerSubject.audit_index`는 int·dedup 가드 isinstance 비교·통지 멱등 키는 `notification_ref()`가 타입에서 도출(m1 우회 구조적 해소). 순수 타입 정합(818 passed·pyright 0·ruff 0)·불변식 영향 0.
- **`InvalidatePrecedent` 실 제외(T8.4(d)·결정 6 보강)** — `Precedent`에 append-only 무효 3필드(`invalidated`·`invalidated_at`·`invalidated_by`·frozen·하위호환) + `PrecedentStore.invalidate`(flag_stale 동형·멱등·삭제 X·`_swap` 공통 헬퍼). Router가 `p.invalidated`면 판례 단축경로를 건너뛰고 분류기 폴백(제외 지점 안 B). `ReevalService`에 옵셔널 `precedents`·`clock` 주입, `InvalidatePrecedent`×`PrecedentSubject` 짝에서 invalidate(Consensus`Agreed`→record 대칭). 미아 없음 보존(분류기 폴백 항상 종착)·needs_review와 독립 축·전이≠기록·Authority 중앙 무변경. web 처리함(reeval inbox) 라우트는 후속.
- **Owner Inbox 두 면 → 세 면** — CONTEXT Inbox 절 갱신(①다툼 ②검토 ③재평가). web 처리함은 후속 슬라이스(이번은 store·서비스까지).
- **불변식 영향 없음** — 미아 없음(라우터가 stale 플래그를 안 봐 계속 라우팅)·Authority 중앙(stale은 중앙 전파기가 달고, 무효화 판단만 owner 1인칭)·전이≠기록(재평가는 ReevalStore 전이·재검토 행위는 audit)·등록 무결성(카드 무변경·`last_reviewed_at` SSOT 그대로)·노출 불변식(stale 필드는 운영 면만·Answered 미노출)은 그대로.
- **갱신 대상**: CONTEXT(Inbox 두면→세면·`OkfChangeEvent`·`StalenessPropagator`·`ReevalItem`/`ReevalStore`/`ReevalService`·`Precedent` needs_review 신규 용어), PRD §5(T7.3 설계·shape), TRD §4/§8(reeval 포트·OkfChangeEvent), tasks T7.3.

## Open Questions (후속)

- **ordering 정밀화** — 불투명 SHA의 older-than 비교가 불가해 MVP는 `!= HEAD or None` 부등호만. `is_ancestor`/`rev-list` 포트는 실 git 의존이라 결정론 게이트와 상충 → 후속(파일 단위 ordering·`parent_sha` 체인 활용).
- ~~**`ReevalSubject` sealed sum 분리**~~ — **해소됨(T8.4(a)·2026-06-24·결정 7)**. `subject_kind`+untyped `subject_ref` 두 필드를 `PrecedentSubject(intent)` | `AnswerSubject(audit_index: int)` sealed sum의 단일 `subject` 필드로 교체. dedup 가드 isinstance 비교·통지 멱등 키 `notification_ref()` 타입 도출(m1 우회 구조적 해소). 순수 타입 정합·불변식 영향 0.
- **과검출 노이즈** — agent_id 거친 매칭은 무관 판례·답까지 큐에 넣는다. `changed_paths` 정밀화(결정 1 죽은 필드)로 후속 좁힘. None 답(SHA 부재)이 많으면 큐 폭증 임계가 필요.
- **카드 노후 nudge** — `last_reviewed_at`가 오래된 카드를 owner에 갱신 nudge하는 별 신호(ADR 0017 결정 3 "stale 지식 갱신 nudge")는 자리만(이 ADR은 판례·답 전파에 집중).
- **실시간 push 통지** — 처리함 pull → Slack/메일/MCP push는 ADR 0017 결정 6④·T7.4.
- **owner 비응답 ReevalItem timeout → Manager escalation** — 종착 메커니즘은 자리만(임계·실 escalation 후속). MVP는 처리함 영구 잔류로 가시성만 보장.
- ~~**`InvalidatePrecedent` 실 제외 메커니즘**~~ — **해소됨(T8.4(d)·2026-06-24·결정 6 보강)**. `Precedent`에 append-only 무효 표식(`invalidated`·`invalidated_at`·`invalidated_by`) + `PrecedentStore.invalidate`(flag_stale 동형·멱등·삭제 X) 신설, Router가 `p.invalidated`면 판례 단축경로를 건너뛰고 분류기 경로로 폴백(제외 지점 안 B — lookup은 순수 읽기). `ReevalService`에 옵셔널 `precedents`·`clock` 주입, `InvalidatePrecedent`×`PrecedentSubject` 짝에서 invalidate 호출(Consensus`Agreed`→record 대칭·어긋난 짝 무시). 미아 없음 보존(판례 단축경로만 끊고 분류기 폴백은 항상 종착)·needs_review(stale)와 독립 축·전이≠기록·Authority 중앙 모두 무변경.
