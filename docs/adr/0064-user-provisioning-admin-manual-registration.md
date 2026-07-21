# 사용자 프로비저닝 — 관리자 수동 User 등록(register-only) — 실 사용자를 관리자가 `POST /admin/users`로 한 명씩 admission→라이브 Registry에 등록하고, User admission 불변식(nonblank id·email 전역 유일·manager 실재)을 카드 admission과 같은 결로 강제하며, 등록을 durable 저널로 영속화한다

상태: accepted (2026-07-21) · **U1 사용자 프로비저닝 슬라이스 ADR** · **ADR 0034의 형제**(카드 라이브 등록 관문 `admit_card`/`AdminRegistryService.register_card`를 User 축으로 미러 — 새 축이 아니라 admission-service 패턴의 새 인스턴스) · **ADR 0023 계승**(admission 관문이 라이브 등록 경로에서도 동일 — 우회 등록 API 금지) · **ADR 0004 계승**(Authority 중앙 유지 — 프로비저닝은 권한을 새로 선언하지 않음) · **ADR 0005 계승**(User/Agent Card 2노드 그래프 — Owner는 `owns` 엣지 파생·정의 불변) · **ADR 0021 계승**(User.email = SSO 신원 매핑 SSOT·`resolve_identity`의 email 유일성 근거 → email 전역 유일 admission으로 앞단 보강) · **ADR 0050/0051 계승**(중앙 Authority action manifest에 `user.register` 신설·operational authorization 경계 재사용) · **ADR 0044 계승**(Depth B = 중앙 모드 R1 UoW 승격 지점) · **ADR 0016 계승**(Depth A = 운영 면 세션 신원 게이트) · CONTEXT(신규 User Provisioning·User Admission·UserRegistered·UserCandidate 용어 등재)·TRD §4(프로비저닝 경계·admission 불변식·인가 계약)·tasks U1 슬라이스 갱신 대상 · 출처: planner 확정 계획(§5 9개 결정, 2026-07-21)

## 맥락 — "실 사용자를 어떻게 시스템에 넣나"가 admission 경계를 되묻는다

지금까지 User는 `registry/users.yaml` 시드로만 들어왔다(부팅 시 로드·`Registry.load`). 실 서비스화에는 **관리자가 실제 사람(id·email·manager)을 등록하는 경로**가 필요하다. 이건 표면상 "유저 CRUD" 요청이지만, 카드 라이브 등록(ADR 0034)이 강제했던 것과 같은 되돌리기 어려운 결정들을 강제한다 — (1) *프로비저닝 모델이 무엇인가*(관리자 수동 vs SSO JIT vs CSV 임포트), (2) *어떤 User admission 불변식을 강제하나*(id·email·manager), (3) *등록을 어떻게 영속화하나*(재기동 시 소멸 방지). 카드가 이미 이 셋을 `admit_card`/`AdminRegistryService`/`SqliteRegistryJournal`로 풀었으므로, User 축은 **그 형제 패턴을 미러**한다(새 기계 최소화).

### 실확인한 현재 지형 (근거 — 전부 코드 읽기로 확인)

- **User 모델은 최소** — `User(BaseModel, frozen=True)`: `id: str`·`manager: str | None`·`email: str | None`(`user.py`). `email`은 SSO/OIDC 신원 매핑 키(ADR 0021 결정 3)로 하위호환 기본 `None`. admission 검증기는 아직 없다(카드와 달리 wire-format field validator 없음).
- **Registry.register_user는 id 중복만 막는다** — `register_user`(`registry.py:85`)가 `_lock`(RLock) 아래 id 중복이면 `RegistryError`. **email 유일성은 강제하지 않는다.** `Registry.validate`(`registry.py:121`)는 카드 owner 실재 + `user.manager` 실재만 본다.
- **email 유일성이 이미 신원 안전에 직결** — `resolve_identity`(`oidc.py:345`)가 verified email로 User를 찾을 때 **0매칭·복수매칭 둘 다 401**로 거부한다. 즉 email이 전역 유일하지 않으면 그 email을 쓰는 모든 사람이 로그인 불가가 된다 — email 전역 유일은 편의가 아니라 신원 무결성 요구다.
- **카드 admission 관문이 이미 존재** — `admit_card(candidate, registry)`(`admin_registry.py:70`)가 순수 함수로 `(AgentCard|None, errors)`를 낸다: ① nonblank id 조기 반환 ② `model_validate`(형식·타입) ③ 참조 무결성(owner/maintainer 실재). `AdminRegistryService.register_card`(`admin_registry.py:231`)가 그 위에 라이브 mutation + 감사 append + durable 저널을 얹는다. `SqliteRegistryJournal`(`sqlite_stores.py:2019`) + `replay_registry_journal`(`admin_registry.py:397`)이 재기동 복원을 진다.
- **저널·시드 부팅 순서** — `web.py:2050` 부트스트랩은 `registry.load(seed)`(users→cards) → `registry.validate()` → `replay_registry_journal(...)` 순. **라이브 카드의 owner가 라이브 User를 참조할 수 있으므로 User 복원이 카드 복원보다 앞서야 한다** — 이 순서가 U1의 저널 리플레이 계약을 결정한다.
- **중앙 인가 manifest** — `central_authority.py:29`의 `Action` TypeAlias + `AUTHORITY_ACTION_MANIFEST`에 `card.register`가 있다. `operational_authorization.py:23`의 `OperationalAction` + `OPERATIONAL_ACTION_MANIFEST`가 S4 경계를 진다. `card.register`는 **정적 role-gated**(DYNAMIC_SUBJECT_REQUIREMENTS·ACTION_RESOURCE_KIND_REQUIREMENTS에 없음) — 등록은 특정 주체 귀속이 아니라 역할로 판단.

---

## 결정

### ① 프로비저닝 모델 = 관리자 수동 등록 (사용자 확정 2026-07-21)

실 사용자는 **관리자가 각 사람을 손으로 등록**한다: `POST /admin/users`(+ `/admin` 사용자 탭)로 id·email·manager를 입력 → User admission → `Registry.register_user` → **즉시 라이브** + 감사(`UserRegistered`) + durable 저널. 카드 라이브 등록(ADR 0034 결정 1)의 User 판(版)이다.

- **기각한 대안(정직한 기록)**:
  - **SSO JIT(Just-In-Time) 프로비저닝** — 첫 로그인 시 IdP claim으로 User 자동 생성. 기각 근거: manager 그래프(`manages` 엣지)를 IdP claim만으로 못 채운다(조직 위계는 IdP에 없거나 신뢰 못 함) → 미아·잘못된 escalation 위험. 조직 그래프의 진실은 관리자가 쥔다(ADR 0005).
  - **CSV 임포트(대량)** — 한 번에 여러 명. 기각 근거: 부분 실패 복구·중복/충돌 처리 복잡도. MVP는 한 명씩 등록의 admission을 먼저 확립하고, 대량은 이 관문을 N번 호출하는 후속 어댑터로 남긴다(관문은 그대로).

### ② User id = 관리자 직접 입력 (admission = nonblank + 중복 거부만)

`user_id`는 **관리자가 직접 입력**한다(사람이 읽는 id). 기존 `users.yaml` 관례(`root_manager`·`legal_lead`)를 잇는다 — email 파생·서버 생성(UUID 등)은 기각.

- **기각 근거**: email 파생은 email이 바뀌면 id가 흔들리고(id는 안정 식별자여야 함), 서버 생성 UUID는 감사 로그·그래프 조회에서 사람이 못 읽는다. `card.owner`·`AnswerRecord.answered_by`·`user.manager`가 전부 이 id를 참조하므로 **읽는 id가 운영 가치**다.
- **admission이 id에 요구하는 것**: nonblank(공백 불가)만. 중복은 `Registry.register_user`가 이미 강제(id 중복 → `RegistryError`) → 서비스가 `DuplicateUserError`(409)로 매핑. 카드의 `agent_id` wire-format 강제(ADR 0023)와 달리 **User id에는 경로 안전 wire-format을 강제하지 않는다** — User id는 파일 경로·URL 세그먼트로 쓰이지 않는다(카드는 `registry/agents/{agent_id}.yaml`·라우팅 경로로 쓰여 경로 탈출 방어가 필요했다). User id 위생은 nonblank + 중복 거부로 충분. (후속에 필요가 확인되면 형식 강제를 얹는 건 열린 백로그.)

### ③ email = 전역 유일 필수 (제공 시 유일 + 실 등록 시 필수 — 두 관심사를 분리)

- **email 제공 시 전역 유일**: admission이 `email`이 이미 등록된 User와 겹치면 거부한다(신규). `resolve_identity`(ADR 0021)가 복수매칭을 401로 거부하는 것과 대칭 — email 유일성을 **등록 앞단에서** 강제해, IdP 로그인 시점에 모호가 애초에 생기지 않게 한다.
- **`email=None`은 복수 허용**: SSO 매핑에서 빠질 뿐(0매칭 → 그 사람만 로그인 못 함·미아 아님). 시드 루트 매니저·하위호환 User가 `email=None`으로 남는 걸 막지 않는다.
- **실 User 등록 시 email 필수(정책)**: `POST /admin/users`(실 프로비저닝)는 email을 요구한다. "email 제공 시 유일"(불변식)과 "실 등록 시 email 필수"(프로비저닝 정책)를 **shape에서 분리** — `admit_user(..., *, require_email: bool = True)`로, 실 등록 경로는 기본 `True`(email 없으면 admission 거부), 시드/테스트 경로는 `require_email=False`로 `email=None`을 허용한다. 불변식(유일)은 email이 있을 때 항상, 정책(필수)은 플래그로.
- **비교 기준**: 유일성은 `resolve_identity`가 쓰는 것과 **같은 정확 문자열 동등**으로 본다(그 매칭보다 느슨하면 안 됨 — 정확 동등이면 "유일 email → resolve_identity ≤1 매칭"이 보장된다). 대소문자 정규화(casefold) 일관성(admission ⇔ resolve_identity)은 IdP가 대소문자를 정규화할 때의 후속 강화로 남긴다(consequences).

### ④ manager = 기존 User ∪ {None(루트)} 실재 검사 (register-only라 순환·자기참조 원천 불가)

- `candidate.manager`가 `None`이 아니면 **기존 User 실재**를 검사한다(`manager ∈ registry.user_ids()`) — `Registry.validate`의 `user.manager` 실재 불변식(`registry.py:126`)을 admission 앞단에 미러. `None`이면 루트(0..1 manager·ADR 0005).
- **순환·자기참조는 register-only라 구조적으로 불가**: 신규 id는 아직 미등록이라 자기 자신을 manager로 못 잡고(id ∉ user_ids), 기존 User의 manager를 편집하지 않으므로(register-only) 기존 비순환 그래프에 새 잎(leaf)만 붙는다. 별도 순환 검사 기계 불요 — manager 실재 검사만으로 그래프 무결성 유지.

### ⑤ CRUD = register-only (edit/deactivate/delete/재-parent 기각·근거 기록)

MVP는 **등록만**. User edit(email/manager 변경)·비활성·삭제·재-parent(manager 교체)는 **후속으로 연기**한다.

- **기각 근거(참조 무결성·미아 없음 직결)**: User는 `card.owner`(owns)·타 User의 `manager`(manages)·`AnswerRecord.answered_by`가 참조한다. 삭제/비활성은 이 참조를 dangling으로 만들어 "미아 없음"(0 매칭 → 루트 escalation)·정정 권한 판정·escalation 경로를 깬다. 재-parent는 manages 서브트리 전체의 escalation을 바꾼다. 이건 카드 오너 변경(ADR 0034 결정 2)이나 승인 재배치(ADR 0048)급의 **별도 전이 설계**가 필요한 되돌리기 어려운 결정이라, 프로비저닝(register)과 분리해 후속 ADR로 남긴다. register-only는 그래프를 *채우기만* 하므로 어떤 기존 참조도 무효화하지 않는다.

### ⑥ 인가 = 신규 action `user.register` (중앙만 선언·우회 API 금지)

프로비저닝은 파괴적(그래프 mutation)이라 게이트한다. **중앙 Authority manifest에 `user.register`를 신설**한다(카드가 `card.register`를 가진 것과 대칭 — 계약만 명시, 배선은 mcp-runtime-engineer).

- **action/resource/role 계약**:
  - action `user.register` — `central_authority.py`의 `Action` + `AUTHORITY_ACTION_MANIFEST`, `operational_authorization.py`의 `OperationalAction` + `OPERATIONAL_ACTION_MANIFEST`에 추가.
  - **정적 role-gated**(`card.register`와 동일 결) — `DYNAMIC_SUBJECT_REQUIREMENTS`·`ACTION_ALLOWED_ROLES`·`ACTION_RESOURCE_KIND_REQUIREMENTS`에 넣지 않는다(등록은 특정 주체 귀속이 아니라 역할로 판단·신규 User엔 "owner subject"가 없다).
  - 권장 role: `admin`/`operator`(이 role이 `routing_rules.yaml`의 `role_permissions`에 `user.register`를 선언해야 grant — Authority 중앙 SSOT).
  - `ResourceRef` 관례: `org_id=configured_org_id`·`kind="user"`·`resource_id=<신규 user_id>`·`owner_subject_id=None`(User엔 owner 개념 없음). `kind`는 관례(authorizer가 강제하지 않음·conflict.open만 kind 요구).
- **Depth A(현 단계·무비밀번호 데모)**: `_session_identity`(`web.py:293`) 게이트 — 세션 신원(User.id)을 요구(미로그인 401). 카드 관리 면과 같은 관문 재사용·역할 구분은 아직 없음.
- **Depth B(중앙 모드 R1 UoW 승격)**: 중앙 Authority `authorize(principal, "user.register", ResourceRef(kind="user", resource_id=신규 id))` + role(admin/operator) sealed grant를 `_authorize_operational_resource`로 대조(카드 경로 `card.register`와 같은 관문).
- **우회 API 금지**: 프로비저닝 전용 우회 등록 경로를 만들지 않는다 — 카드와 **같은 관문**(admission + 인가)을 재사용한다("유효하지 않은 User는 등록되지 않는다"·"Authority 중앙" 불변식의 U1 판).

### ⑦ 영속화 = 병렬 `SqliteUserJournal` + user→card 리플레이

등록은 durable 저널에 남기고 재기동 시 리플레이한다(안 하면 라이브 등록 User가 재기동에 소멸).

- **방향: 병렬 `SqliteUserJournal`(신규 `user_journal` 테이블)** — 기존 `registry_journal`에 user kind를 얹는 대안은 기각한다(그 테이블의 `candidate` 컬럼은 카드 필드 JSON·`RegistryJournalCandidate`가 카드 shape·`replay`가 `CardCandidate`를 만든다 → user 행을 섞으면 union candidate·kind 분기로 카드 저널이 지저분해진다). `SqliteRegistryJournal`을 1:1로 미러한 새 인스턴스(=`TokenStore`/`SessionStore`처럼 "같은 패턴의 N번째 인스턴스")가 더 깨끗하다. `UserJournalSink` 포트(`append_register`) + `SqliteUserJournal`(durable) + `replay_user_journal(journal, registry)`(복원). 저장·리플레이 구현은 tdd-engineer/mcp-runtime-engineer.
- **리플레이 순서 계약: user → card (하드)**. 부팅: `registry.load(seed)` → `registry.validate()` → **`replay_user_journal`(User 복원)** → `replay_registry_journal`(카드 복원). 라이브 카드의 owner가 라이브 등록 User를 참조할 수 있으므로 User가 먼저 복원돼야 카드 admission의 owner 실재가 통과한다. 리플레이도 **admission 경유**(무효 User·미등록 manager는 안전측 스킵 — 부팅 중단 없음, `replay_registry_journal`과 같은 결).

### ⑧ Depth A/B 단계화 (도메인 코어는 Depth 무관 동일)

**공유 코어를 한 번만 짓는다.** `UserCandidate`·`admit_user`·`AdminUserService.register_user`·`DuplicateUserError`·`UserRegistered` 감사·`UserJournalSink`는 Depth 무관 동일하다. Depth는 *인가 게이트와 UoW 경계*만 다르다.

- **Depth A 먼저(현 단계)**: 데모/무비밀번호 모드 — `_session_identity` 게이트 + 라이브 `AdminUserService.register_user`. `POST /admin/users` 어댑터가 세션 신원을 `by`로 넣어 감사·저널.
- **Depth B 승격(후속)**: 중앙 모드 R1 UoW(ADR 0044) — 중앙 Authority `user.register` grant + operational application이 UoW(원자 트랜잭션) 안에서 register_user. **코어(admission·서비스·저널·감사)는 재사용**, 인가·트랜잭션 경계만 승격.

### ⑨ 미-co-mingle (파일럿 registry ≠ 데모 픽스처)

되돌리기 어려운 운영 결정이라 **언급만** 한다: 파일럿 registry엔 **루트 매니저만 시드**하고 실 Owner는 `POST /admin/users`로 라이브 등록한다. 데모 픽스처(`@example.com` User·`agent_X`/`agent_Y` 등)는 실 파일럿 registry에 **co-mingle 금지** — 실 신원과 데모 더블이 같은 그래프에 섞이면 감사·escalation·신원 매핑이 오염된다(production-no-fakes 정신). 시드 마이그레이션·실 registry 조립은 배선(mcp-runtime-engineer)이 진다.

---

## 근거

- **형제 미러 = 새 기계 최소화** — U1은 카드 라이브 등록(ADR 0034)의 검증된 3층(순수 admission `admit_*` → 라이브 서비스 `register_*` → durable 저널 `Sqlite*Journal` + `replay_*`)을 User 축으로 1:1 복제한다. admission 관문·감사·저널·리플레이·인가 게이트의 *결*이 이미 있으므로 새로 발명하지 않는다("형제 패턴 미러·새 축 금지").
- **email 유일성은 신원 무결성** — email 전역 유일을 등록 앞단에서 강제하는 근거는 편의가 아니라 `resolve_identity`(ADR 0021)의 복수매칭 401이다. 앞단 거부가 없으면 email 충돌이 "그 email을 쓰는 모두의 로그인 불가"로 번진다. 그래서 email 유일성 검사는 admission의 email-uniqueness 읽기와 register_user 쓰기를 **같은 임계 구역**에 묶어(카드 register_card가 has_card+register를 lock에 묶는 것과 대칭·단 User는 Registry가 email 유일을 강제하지 않으므로 유일성 읽기까지 lock 안에서) 동시 같은-email 등록이 둘 다 통과하지 못하게 한다.
- **register-only의 정직한 경계** — CRUD를 register로 좁힌 건 게으름이 아니라 참조 무결성 존중이다. User는 owns·manages·answered_by가 참조하는 그래프 허브라, 삭제/비활성/재-parent는 "미아 없음"·정정 판정·escalation을 흔드는 별도 전이 설계(ADR 0034/0048급)를 요구한다. 등록만 먼저 확립하고 나머지는 근거와 함께 연기한다.
- **전이 ≠ 기록 보존** — 등록(전이)은 `Registry.register_user`(도메인)로, `UserRegistered`(감사 이벤트·append-only)는 기록으로 분리한다. 저널(기계 리플레이 원천)과 감사 로그(사람이 읽는 이력)는 카드와 같이 다른 축으로 유지한다.

## Consequences

- **User admission 코어 신설**(`admin_users.py` — `UserCandidate`·`admit_user`·`DuplicateUserError`·`UserJournalSink`·`AdminUserService`). `AdmissionError`·`AdminAuditSink`·`action_record`는 admin_registry/audit에서 재사용(제네릭). shape는 domain-architect, red→green 구현은 tdd-engineer.
- **`AdminUserService`는 `AdminRegistryService`의 형제(별도 서비스)** — 확장(register_user를 카드 서비스에 얹기) 대신 미러(별도 클래스·별도 lock). 근거: 카드 서비스는 오너 변경(토큰 revoke·WS 끊기·owner 격리 임계 구역)으로 무겁고, User 등록은 그 기계가 없다 → 섞으면 둘 다 흐려진다. 별도 서비스가 Depth A/B 배선(`_admin_user_service` 병렬 인스턴스)에서도 대칭. (확장도 가용했으나 기각 — 응집·대칭 우선.)
- **`SqliteUserJournal`(신규 `user_journal` 테이블) + `replay_user_journal`** 신설(sqlite_stores.py). 부팅 리플레이 순서에 User 단계를 카드 앞에 삽입(`web.py` 부트스트랩·mcp-runtime-engineer 배선).
- **인가 manifest 확장** — `central_authority.py`·`operational_authorization.py`에 `user.register`(정적 role-gated) 추가·`routing_rules.yaml`에 admin/operator role permission 선언(배선 mcp-runtime-engineer). `POST /admin/users` 어댑터는 카드 경로와 같은 인가 관문 재사용(web·mcp-runtime-engineer).
- **4대 불변식 영향**:
  - **미아 없음** — 프로비저닝은 manager 그래프를 *채우기만* 한다(register-only). 라우팅·escalation 종착 무변경.
  - **유효하지 않은 User는 등록되지 않는다** — `admit_user` 관문(nonblank id·email 형식/유일/필수·manager 실재)이 라이브·저널 리플레이 양쪽에서 동일(우회 API 금지). 카드 admission 불변식의 User 판.
  - **Authority 중앙** — 프로비저닝은 카드처럼 권한을 새로 선언하지 않는다. `user.register` grant는 중앙 `routing_rules.yaml`만 선언(ADR 0004 계승).
  - **전이 ≠ 기록** — 등록(전이)은 `register_user`, `UserRegistered`(감사)는 append-only 기록. 저널 ≠ 감사(카드와 같은 두 축).
- **후속·백로그(정직한 기록)**: (1) email 대소문자 정규화 일관성(admission casefold ⇔ `resolve_identity` casefold) — IdP가 대소문자를 정규화하면 강화. (2) User CRUD(edit/deactivate/delete/재-parent) — 참조 무결성 전이 설계 별도 ADR. (3) CSV 대량 임포트 — 관문 N회 호출 어댑터. (4) User id wire-format 강제 — 필요 확인 시.

## 결정 (planner 확정 계획 반영 2026-07-21)

1. **프로비저닝 모델 — ✅ 관리자 수동 등록**(`POST /admin/users`·register-only·SSO JIT·CSV 임포트 기각·결정 ①⑤).
2. **User id — ✅ 관리자 직접 입력**(nonblank + 중복 거부만·email 파생/서버 생성 기각·wire-format 비강제·결정 ②).
3. **email — ✅ 전역 유일 필수**(제공 시 유일 + 실 등록 시 필수 분리·`None` 복수 허용·정확 동등 비교·결정 ③).
4. **manager — ✅ 기존 User ∪ {None} 실재 검사**(register-only라 순환/자기참조 원천 불가·결정 ④).
5. **인가 — ✅ 신규 `user.register`**(중앙만 선언·정적 role-gated·admin/operator·Depth A 세션 게이트·Depth B 중앙 Authority·우회 API 금지·결정 ⑥).
6. **영속화 — ✅ 병렬 `SqliteUserJournal` + user→card 리플레이**(registry_journal 병합 기각·결정 ⑦).
7. **Depth A/B — ✅ 공유 코어 + Depth A 먼저**(도메인 코어 Depth 무관 동일·Depth B는 인가·UoW만 승격·결정 ⑧).
8. **미-co-mingle — ✅ 파일럿 루트 매니저만 시드**(데모 `@example.com` co-mingle 금지·결정 ⑨).
