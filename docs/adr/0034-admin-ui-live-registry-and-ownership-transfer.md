# 관리 UI — 라이브 Registry 반영과 오너 변경 전이 — 카드 등록·오너 변경을 라이브 재-admission으로 즉시 반영하고, 오너 변경 전이는 구 owner 토큰 revoke를 같은 임계 구역에서 강제하며, 정정 권한 판정을 "현재 카드 owner" 기준으로 옮긴다

상태: accepted (2026-07-04) · **Phase 12 관리 UI 슬라이스 ADR** · **ADR 0018 재정의**(카드 편집 채널을 "YAML→git/PR 반영"에서 *라이브 Registry mutation + 감사 로그 + 영속 스토어*로 이동 — YAML은 초기 시드로 강등, 결정 1) · **ADR 0023 계승·강화**(agent_id 형식 admission이 UI 경로에서도 동일하게 관문 — 우회 API 금지, 결정 1) · **ADR 0004 계승**(카드 under-claim 자기보고·Authority 중앙 유지 — UI는 권한을 새로 선언하지 않음) · **ADR 0026 재사용**(오너 변경 전이가 구 owner 워커 토큰을 `revoke` — 스위치와 같은 임계 구역·결정 2) · **ADR 0033 정정 판정 재정의**(`CorrectionEvent` 판정 기준을 `answered_by` 동등 대조 → "현재 카드 owner" 대조로 교체 — 전이≠기록 유지·결정 3) · **ADR 0016 계승**(운영 면 세션 신원 요구·실 SSO 시 운영자 role 클레임 강화 지점·결정 4) · CONTEXT(신규 Ownership Transfer·Re-admission·OwnershipTransfer 감사 이벤트 용어 — 구현 라운드 등재)·TRD §4(오너 변경 전이 순서·정정 판정 변경·권한 경계)·tasks 관리 UI 슬라이스 갱신 대상 · 출처: 상세 계획 [`docs/plan-central-answering.md`](../plan-central-answering.md) §9(planner·2026-07-04)

## 맥락 — 쉬운 관리 UI가 "편집 채널의 진실 원천"을 되묻는다

사용자가 **신규 Agent Card 추가와 Owner 변경이 쉬운 관리 UI**를 요구했다. 이건 표면상 UI 요청이지만, 두 개의 되돌리기 어려운 결정을 강제한다 — (1) *등록의 진실 원천이 어디에 사나*(YAML 파일 vs 라이브 상태), (2) *오너가 바뀔 때 구 owner의 답변 권한이 어떻게 격리되나*(토큰 revoke의 원자성). 세 번째로 (3) 오너가 바뀐 뒤 *과거 답을 누가 정정할 수 있나*(정정 권한 판정 기준)가 따라온다. 이 세 결정은 코드 한 줄보다 먼저 확정해야 하는 계약이라 ADR로 못박는다.

### 실확인한 현재 지형 (근거 — 전부 코드 읽기로 확인)

- **admission 채널이 이미 존재한다** — `validate_card_for_builder(req, registry)`(`web.py:662`)가 카드 후보를 admission 규칙으로 검증한다: ① `AgentCard.model_validate`(필수 필드·타입·`agent_id` 형식 — `agent_card.py:118` field_validator·ADR 0023) ② 참조 무결성(`card.owner`가 Registry 실재 User·maintainer 실재 — `Registry.validate` 정신·`registry.py:61`). 통과 시 **라이브 등록 없이** `registry/agents/{agent_id}.yaml` 텍스트를 낸다. `web.py:640`에 "**라이브 레지스트리 mutation은 하지 않는다**"가 명시돼 있다 — 편집 채널이 git/PR(CONTEXT Maintainer·ADR 0018)이다.
- **owner 필드는 frozen 값** — `AgentCard.owner: str`(`agent_card.py:105`)는 User.id 참조. `frozen=True`라 "owner 변경"은 필드 mutation이 아니라 **새 카드 값으로의 교체(재-admission)**다.
- **정정 판정이 answered_by 동등을 요구** — `submit_correction`(`answer_record.py:207`)이 `by_owner != record.answered_by`면 거부한다. `AnswerRecord.answered_by`는 답 생성 시 owner_id로 채워진다(ADR 0033·`answer_record.py`). 오너 변경 후 새 owner의 정정을 막는다.
- **워커 토큰이 owner_id에 귀속** — `AdmissionToken.owner_id`(`token.py:64`)·`revoke(token_id)` append-only(`token.py:173`·ADR 0026). verify가 owner_id를 회신 출처로 강제한다(owner 격리 불변식).
- **PendingDraft는 구 owner 워커 로컬** — `worker.py:220`의 `ticket_id → PendingDraft`는 중앙 상태가 아니라 **구 owner 워커 프로세스 안**에 산다.
- **HitlToggleMap·Presence 키 = agent_id** — 둘 다 owner_id가 아니라 agent_id로 키잉된다(`hitl.py:70`·`presence.py:56`). 오너 변경 시 owner 무관하게 살아있다.

---

## 결정

### 1. Registry 반영 모델 = 라이브 즉시 반영 (ADR 0018 재정의·ADR 0023/0004 계승·사용자 확정 2026-07-04)

관리 UI 제출은 **admission 검증 통과 즉시 라이브 Registry에 반영**한다. YAML 파일은 진실 원천이 아니라 **초기 시드로 강등**한다.

- **흐름**: UI 제출 → admission 검증(§9.5·아래 우회 금지) → **즉시 라이브 Registry mutation** + **감사 로그 append** + (`AON_DB` 켜면) **SQLite 영속**. 등록 이력의 git 추적은 **감사 로그가 대신**한다(파일 diff에 의존하지 않는다).
- **진실 원천의 이동**: Registry의 진실 원천이 `registry/agents/*.yaml` 파일에서 **라이브 상태 + 감사 로그 + 영속 스토어**로 옮겨간다. YAML은 부팅 시 초기 시드 역할만 남는다(`registry.py:69` `load`는 시드 로더로 재포지셔닝). 파일과 라이브 상태가 갈릴 수 있음을 정면으로 받는다 — 그래서 감사 로그가 등록·전이의 진실 이력을 진다.
- **기각한 대안(정직한 기록)**:
  - **YAML 동기 기록** — UI 제출 시 파일을 동기로 다시 써서 파일 SSOT를 유지. 기각 근거: 파일 쓰기·동시성·부분 실패 복구 복잡도. UI 사용성 우선.
  - **UI는 제안만(git/PR 경유)** — 기존 ADR 0018 결(안전·즉시성 낮음). 기각 근거: "쉬운 관리 UI" 요구와 정면 충돌 — owner 변경 하나에 PR 왕복은 사용성 붕괴.
  - 사용자는 **라이브 즉시 반영**을 택했다 — 파일 순수성보다 *사용성*이 관리 UI의 실제 가치다.
- **"무효 카드 등록 금지" 불변식은 UI 경로에서도 동일하게 지킨다 (ADR 0023 계승·우회 API 금지)**:
  - **UI 전용 우회 등록 API를 만들지 않는다.** UI는 기존 admission 채널(`validate_card_for_builder` 정신)을 그대로 호출하는 **얇은 어댑터**다 — 폼→카드 후보 DTO 변환·표시만 담당한다.
  - 모든 신규 카드는 `AgentCard.model_validate`(형식·agent_id wire-format·ADR 0023) + 참조 무결성(owner·maintainer 실재)을 통과해야 라이브에 들어간다. UI가 이 두 관문을 건너뛰는 경로를 열면 "유효하지 않은 카드는 등록되지 않는다" 불변식이 깨진다 — **금지**.
- **Authority 중앙 유지 (ADR 0004 계승)**: 폼에서 권한류 필드(`can_answer` 등)를 받아도 그건 카드 under-claim 자기보고일 뿐(ADR 0004)이고, Authority SSOT는 여전히 중앙(`routing_rules.yaml`). UI가 권한을 새로 "선언"하지 않는다.

### 2. 오너 변경 전이 모델 — 재-admission + 같은 임계 구역에서 구 owner 토큰 revoke (ADR 0026 재사용·owner 격리 불변식)

오너 변경은 **agent_id는 불변**이고 그 카드의 `owner` User.id만 A→B로 바뀌는 **전이**다. frozen 값이라 교체(재-admission)로 실현한다. 이 전이의 핵심은 **구 owner의 답변 권한을 원자적으로 끊는 것**이다.

- **각 축의 운명 (§9.2 운명 표)**:

| 축 | 키 | 운명 | 이유 |
|---|---|---|---|
| Knowledge Store | agent_id | **유지** | 키가 agent_id — 지식은 카드에 붙지 사람에 안 붙음. |
| Precedent(판례) | agent_id | **유지** | 과거 판례는 카드 이력. |
| HitlToggleMap | agent_id | **유지(재확인 권고)** | 키가 agent_id라 owner 무관하게 살아있음. 토글은 신뢰 게이트라 새 owner가 재확인하는 게 안전 — 강제 리셋 없이 UI가 "새 owner 확인 요망" 표시. |
| PendingDraft(보류 초안) | 구 owner 워커 로컬 | **무효화(drain-or-drop)** | 구 owner 워커 프로세스 로컬(`worker.py:220`). 세션 무효화로 접근 끊김 → 중앙 재큐잉 또는 drop. 구 owner가 미제출 초안을 새 owner 권한으로 회신하면 owner 격리 위반. |
| Presence(프레즌스) | agent_id | **재관측까지 offline** | 구 owner 워커 WS 끊기면 offline. 새 owner 워커 재연결 시 재관측. 미관측=offline 안전 기본. |
| **Worker Token** | token_id(owner_id 귀속) | **revoke(필수·owner 단위 전부)** | **보안 핵심.** 구 owner 토큰의 owner_id가 여전히 구 A라 verify 통과 시 구 owner가 이 카드 회신을 계속 낼 수 있음 → owner 격리 붕괴. `AdmissionToken`이 카드별이 아니라 owner 단위라, A의 다른 카드용 토큰까지 함께 revoke된다(over-revoke·안전측). |
| AnswerRecord.answered_by | 과거 기록 | **불변** | 전이≠기록. 과거 답은 구 owner가 낸 게 사실 — 필드 절대 변경 X(append-only). |
| 정정 권한 | — | **새 owner로 이동** | 결정 3. |

- **전이 순서 (§9.3 — 전이 ≠ 기록)**:
  1. **제출** — 관리 UI가 대상 카드의 새 owner(B)로 갱신된 카드 후보를 제출한다. **전용 우회 API 없음** — 결정 1의 admission 채널을 그대로 호출.
  2. **admission 검증** — 새 카드 검증(agent_id 형식·새 owner B 실재·참조 무결성). **무효면 스위치 없음**("무효 카드 등록 안 됨" 불변식).
  3. **스위치(전이)** — 검증 통과 시 Registry의 그 카드 값을 새 owner 카드로 교체(frozen 값 교체). Authority는 여전히 중앙 선언.
  4. **구 세션/토큰 무효화 (스위치와 원자적)** — (a) 구 owner A의 **활성 토큰 전부** `revoke`(`token.py:173` — `AdmissionToken`이 owner 단위 귀속이라 카드별로 분리해 revoke할 수 없다. A가 카드를 여럿 갖고 있으면 그 전부의 토큰이 함께 revoke된다 — over-revoke는 owner 격리 관점에서 안전측이라 받아들인다. 향후 토큰에 `agent_id` 스코프를 도입해 카드별 분리 revoke를 하는 건 선택 백로그), (b) 구 owner 워커 WS 세션 끊기(presence offline로 귀결), (c) 구 owner 워커 로컬 PendingDraft 접근 불가(drop 또는 중앙 재큐).
  5. **감사 기록** — `OwnershipTransfer` 이벤트를 감사 로그에 append(who: 운영자, what: agent_id·from A·to B, when). `CorrectionEvent`·토큰 revoke처럼 append-only. **전이(3)와 기록(5)은 분리** — 3이 도메인, 5가 감사.

- **토큰 revoke 원자성 = owner 격리에 직결 (계약으로 명시)**: **스위치(3)와 revoke(4-a)는 같은 임계 구역에 둔다.** revoke가 스위치보다 늦으면 그 window 동안 구 owner A가 verify를 통과해 *새 owner의 카드로* 회신할 수 있다 — owner 격리 붕괴. 이건 편의가 아니라 보안 불변식 계약이다. 순서 역전·비원자 실행은 owner 격리 위반으로 간주한다.

### 3. 정정 권한 판정 = 현재 카드 owner 기준 (ADR 0033 재정의·전이 ≠ 기록 유지)

오너 변경 후 새 owner B가 구 owner A가 낸 과거 답을 정정하려면, 현재 판정(`by_owner != record.answered_by`·`answer_record.py:207`)이 거부한다. 판정 기준을 **"현재 카드 owner"로 옮긴다** — 과거 기록 필드는 건드리지 않는다.

- **판정 교체**: `submit_correction` 판정을 `record.answered_by == by_owner`에서 **`registry.get(record.agent_id).owner == by_owner`**("현재 그 카드의 owner인가")로 바꾼다. `AnswerRecord`가 `agent_id`도 갖고 있어(`answer_record.py`) 현재 카드 owner를 되짚을 수 있다.
- **과거 기록 필드는 불변 (전이 ≠ 기록)**: `record.answered_by`는 **절대 수정하지 않는다** — 누가 원래 답했나의 사실. `CorrectionEvent.by_owner`에는 **정정을 실제로 낸 owner(B)**를 그대로 기록한다 — 정정 이력의 진실.
- **멱등 자연 보존**: 멱등 event_id가 `(record_id, by_owner, corrected_text)` 해시(`answer_record.py`)라 by_owner가 B로 바뀌면 자연히 새 이벤트(구 A 정정과 구별).
- **하위호환**: agent_id 카드가 Registry에서 사라진 경우(카드 폐기)엔 판정 원천이 없다 — fallback으로 `answered_by` 동등 검사 유지 또는 "미존재 카드 정정 불가"로 명시(둘 다 불변식 안전). 구현자가 정한다.
- **불변식 문구 재해석**: 기존 "자기 에이전트 답만 정정"을 **"현재 카드 owner만 정정(과거 answered_by는 불변)"**으로 정정한다. 이건 owner 격리의 재해석이지 완화가 아니다 — 정정 권한이 *사람*이 아니라 *현재 카드 소유*를 따른다.

### 4. 권한 경계 = 세션 신원 + 로컬 관례 (현 단계)·실 SSO 시 운영자 role 강화 (ADR 0016 계승)

카드 등록·오너 변경은 파괴적이라 접근을 게이트한다.

- **현 단계(무비밀번호 데모·OIDC 코어 보류)**: 관리 UI는 **운영자 면**이다. 진입은 `_session_identity`(`web.py:180`)로 세션 신원(User.id)을 요구한다 — 무비밀번호 `/login`으로 신원을 선택해 세션 고정(per-request 가장 차단·ADR 0016). 역할(root/운영자) 구분은 아직 없다.
- **현실적 경계**: 로컬 바인드 관례(중앙 운영 면을 127.0.0.1 바인드 — `owner_web.py:14` 관례) + 로그인 세션으로 데모 단계는 충분.
- **실 SSO 강화 지점(명시)**: T7.x OIDC 활성 시 `resolve_identity`가 IdP 증명 신원을 세션에 박는다(`web.py:167`·ADR 0021). 그 위에 **운영자 role 클레임 검사**를 등록·오너 변경 엔드포인트에 얹는다 — 이게 role 게이트의 실 구현 지점이다.
- **후속 강화 지점(code-reviewer M-1 관측, 2026-07-05 기록)**: (1) `transfer_ownership`의 WS `disconnect_owner`는 임계 구역(`_lock`) *밖*에서 best-effort로 실행된다(admin_registry.py:245) — 전이·토큰 revoke는 이미 원자적으로 성립해 owner 격리 자체는 안전하지만, disconnect 실패 시 구 owner WS가 즉시 안 끊기는 좁은 창은 남는다. (2) `supervision_correct`(정정 제출) 경로는 `CorrectionService.submit_correction`의 `owner_of` 현재-owner 대조가 매 호출 재검증이라 이미 최신 owner 기준으로 판정하지만, 세션 신원 자체를 강제(웹 경로)하는 건 이번 M-1 수정으로 채워짐 — 두 지점 모두 골든셋/운영 관측으로 재확인할 백로그로 남긴다(`docs/tasks-v0.md` 참조).

---

## 근거

- **사용성 우선의 정직한 트레이드오프** — 결정 1은 파일 SSOT의 순수성(git diff 감사·재현성)을 포기하고 라이브 즉시 반영을 택한다. 그 반대급부로 **감사 로그**가 등록·전이 이력의 진실을 지도록 둔다("git 추적을 감사 로그가 대신"). 진실 원천은 옮겼으되 감사성은 유지한다.
- **admission은 옮기지 않는다** — 진실 원천은 파일→라이브로 옮겼지만 *관문*은 그대로다. ADR 0023의 agent_id 형식 강제·참조 무결성이 UI 경로에서도 같은 관문을 지킨다(우회 API 금지). 진실 원천 이동이 등록 무결성을 흔들지 않는다.
- **frozen 값 + 재-admission** — owner 변경을 필드 mutation이 아니라 값 교체로 실현해 불변 값 객체 정신을 지킨다(ADR 0005·0033의 값 객체 규율). "재-admission"은 신규 등록과 같은 관문을 재통과하는 것이라 admission 코드를 재사용한다(새 기계 0).
- **토큰 revoke 원자성 = 보안 계약** — 오너 변경의 진짜 위험은 UI가 아니라 *구 owner의 답변 권한이 살아있는 window*다. 스위치와 revoke를 같은 임계 구역에 묶는 계약이 owner 격리(ADR 0026)를 지킨다. ADR 0026의 append-only revoke를 재사용하되 *타이밍 계약*을 새로 못박는다.
- **전이 ≠ 기록 보존** — 오너 변경(전이)은 `OwnershipTransfer` 감사 이벤트로 따로 쌓고, 과거 `AnswerRecord.answered_by`는 불변으로 둔다. 정정 판정만 현재 owner로 옮기되 기록 필드는 안 건드린다. 4대 불변식 중 "전이≠기록"을 정면으로 지킨다.

## Consequences

- **관리 UI 어댑터 신설**(폼→카드 후보 DTO·admission 채널 호출·게이트 내 결정론). 실 UI 렌더·실 세션·크로스머신은 게이트 밖(mcp-runtime-engineer).
- **Registry 라이브 mutation 경로 신설** — 결정 1의 admission 통과 즉시 반영 + 감사 append + (`AON_DB`) 영속. `registry.py`의 `load`가 시드 로더로 재포지셔닝(파일 = 초기 시드).
- **`OwnershipTransfer` 감사 이벤트 신설**(who/what/when·append-only·`CorrectionEvent` 정신). 스위치와 원자적으로 구 owner 토큰 revoke + WS 세션 끊기 + PendingDraft drop/재큐.
- **`submit_correction` 판정 교체** — `answered_by` 동등 → 현재 카드 owner 대조(`answer_record.py:207`). 과거 기록 필드 불변·`CorrectionEvent.by_owner`에 실제 정정자(B) 기록.
- **4대 불변식 영향**:
  - **미아 없음** — 관리 UI는 라우팅 종착을 안 바꾼다(등록·전이는 카드 값 축·라우팅 무변경).
  - **유효하지 않은 카드는 등록되지 않는다** — admission 관문이 UI 경로에서도 동일(ADR 0023 계승·우회 API 금지). 진실 원천 이동이 관문을 흔들지 않음.
  - **Authority 중앙** — UI는 카드 under-claim만 받고 권한을 새로 선언하지 않음(ADR 0004 계승). Authority SSOT는 중앙 파일.
  - **전이 ≠ 기록** — 오너 변경(전이)은 `OwnershipTransfer`(기록)로 따로 남고, `answered_by`는 불변. 정정은 새 `CorrectionEvent`(원 `AnswerRecord` 불변).
  - **owner 격리(부수·보안)** — 오너 변경 시 구 owner 토큰을 스위치와 같은 임계 구역에서 revoke(ADR 0026)해 구 owner의 답변 권한을 원자적으로 끊음.
- **갱신 대상**: CONTEXT(신규 Ownership Transfer·Re-admission·OwnershipTransfer 감사 이벤트 용어 — 구현 라운드 등재)·TRD §4(오너 변경 전이 순서·정정 판정 변경·권한 경계)·tasks 관리 UI 슬라이스(신규 등록 어댑터·오너 변경 전이·토큰 revoke 배선·정정 판정 교체)·불변식 문구("자기 에이전트 답만 정정"→"현재 카드 owner만 정정"·"owner 격리"에 "오너 변경 시 구 토큰 revoke" 명문화).

## 결정 (사용자 확정 2026-07-04)

1. **Registry 반영 모델 — ✅ 라이브 즉시 반영**(admission 통과 즉시 반영 + 감사 로그 + `AON_DB` 영속·YAML은 초기 시드로 강등·git 추적은 감사 로그가 대신·"YAML 동기 기록"·"UI는 제안만"은 기각·결정 1).
2. **오너 변경 전이 — ✅ 재-admission + 구 owner 토큰 revoke 원자성**(agent_id 불변·frozen 값 교체·스위치와 revoke를 같은 임계 구역·owner 격리 보안 계약·결정 2).
3. **정정 권한 판정 — ✅ 현재 카드 owner 기준**(`answered_by` 동등 → `registry.get(agent_id).owner` 대조·과거 기록 필드 불변·`CorrectionEvent.by_owner`에 실제 정정자 기록·결정 3).
4. **권한 경계 — 현 단계 세션 신원 + 로컬 관례**(실 SSO 시 운영자 role 클레임 강화 지점 명시·ADR 0016/0021 계승·결정 4).
