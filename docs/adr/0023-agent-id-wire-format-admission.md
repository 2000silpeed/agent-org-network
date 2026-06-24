# agent_id wire-format을 admission에서 근본 강제한다 — 어댑터 가드는 경로 안전 백스톱으로 유지

상태: accepted (2026-06-24) · **B1 후속(T8.1 (a) code-reviewer 보안 Blocker B1의 근본 강제)** · ADR 0018(`validate_agent_id` 어댑터 가드)의 *상류 보강* · "유효하지 않은 카드는 등록되지 않는다"(등록 무결성, PRD §8·§10) 불변식의 일부 · ADR 0004(권한 중앙·카드 자기보고)와 무관(형식 강제는 권한 선언이 아니라 식별자 위생).

## 맥락 — 어댑터가 막던 것을 admission으로 끌어올린다

T8.1 (a)에서 code-reviewer가 보안 Blocker B1을 잡았다. `SubprocessGitGateway`가 `okf_root / {agent_id}` 경로와 `{sha}:{agent_id}` archive tree-ish에 `agent_id`를 박는데, `agent_id`가 `../x`·`/etc`·`a/b` 같은 경로 탈출 형태면 번들 밖 쓰기·엉뚱한 트리 지목이 가능했다. 그 자리 처방은 `git_gateway.validate_agent_id`(어댑터 안전 경계 — `commit_bundle`·`extract_snapshot`·Fake 양쪽에서 빈/공백·`/`·`\`·`..`·절대경로 거부)였다.

하지만 이건 *어댑터 차단*이다. 유효하지 않은 `agent_id`는 **카드가 등록되는 순간(admission)에 이미 막혔어야** 한다 — 그래야 시스템에 애초에 못 들어온다. 현재 `AgentCard.agent_id: str`엔 형식 검증이 없어, 경로 탈출 형태의 `agent_id`로도 카드를 *구성*할 수 있다(어댑터에 닿기 전까진 통과). 게다가 빈 `agent_id` 거부는 `web.py`의 빌더 검증(`validate_card_for_builder`)에 *ad-hoc으로 산재*해(`if not req.agent_id ...`) 있을 뿐, 카드 자체엔 없다. admission이 형식의 단일 진실 원천이 아니다.

## 결정

### 1. 형식 = `^[A-Za-z0-9][A-Za-z0-9_-]*\Z`, 길이 ≤ 64 (대문자 수용 — 소문자 강제 기각)

`agent_id`는 **영숫자로 시작 + 영숫자/`_`/`-`로 구성**, 1~64자다. 상수로 박는다(`agent_card.py`):

```python
AGENT_ID_MAX_LENGTH = 64
AGENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*\Z"
```

- **경로 탈출을 *구조적으로* 차단한다.** 어떤 매칭 문자도 경로구분자(`/`·`\`)·상위참조(`..`의 `.`)·공백·절대경로 선두(`/`)가 될 수 없다. 어댑터가 *부정 패턴*(blocklist — `..`·`/` 등을 하나씩 거부)으로 막던 것을, admission은 *긍정 패턴*(allowlist — 허용 문자만)으로 막는다. allowlist가 더 강하다(미래의 새 탈출 형태도 자동 차단).
- **끝 앵커는 `$`가 아니라 `\Z`다(후행 개행 우회 차단).** Python `re`에서 `$`는 *문자열 끝의 후행 개행 직전*에도 매칭한다 — `re.match(r"...$", "cs_ops\n")`이 통과해버린다. wire-format admission에선 후행 개행이 든 식별자(`okf/cs_ops\n/` 같은 비정상 경로·로그/DB 키 오염)를 받으면 안 되므로 *문자열 절대 끝*만 매칭하는 `\Z`를 쓴다(B1 명시 테스트가 red로 잡아 `$`→`\Z`로 고침).
- **대문자 수용(소문자 강제 기각).** 기존 테스트 픽스처 `agent_X`·`agent_Y`(`test_consensus.py`)가 대문자를 쓴다. 소문자 강제는 더 깨끗한 명명 컨벤션이지만 *이 작업의 목표가 아니다* — 목표는 **보안 경화(경로 탈출 admission 차단)**지 명명 스타일 강제가 아니다. 최소 침습(기존 전부 수용)이 기본값이고(글로벌 과도 엔지니어링 금지), 소문자 강제는 픽스처 변경을 강요하는 스코프 크립이다. → **대문자 허용.**
- **길이 상한 64.** 무한 길이 식별자(파일시스템·로그·DB 키 압박)를 막는 가벼운 위생. 기존 최장값(`no_such_card` 12자)에 여유가 크다.
- **수용/거부 확인.** 수용: `cs_ops`·`finance_ops`·`hr_ops`·`it_ops`·`contract_ops`·`legal_ops`·`agent_a`·`agent_b`·`unknown_ops`·`no_such_card`·`agent_X`·`agent_Y`·`contract_ops2`·`sales_ops`·`new_ops`·`n` 등 기존 전부. 거부: `""`·`"   "`·`../x`·`/abs`·`a/b`·`a\b`·`..`·선행 `-`/`_`·내부 공백·`.`·비ASCII·65자 초과·**후행 개행(`"cs_ops\n"`)·내부 개행(`"a\nb"`)**. (`tests/test_agent_id_admission.py` 28개 명시 단언으로 고정 — 거부 16·수용 7·빌더/로더 경로 정합 5.)

### 2. 강제 지점 = `AgentCard`의 pydantic field validator (안 A — 가장 근본)

`@field_validator("agent_id")`로 카드 *구성 시점*에 강제한다. *모든* 카드 구성 경로 — YAML `model_validate`(`Registry.load`)·빌더 검증(`validate_card_for_builder`의 `AgentCard.model_validate`)·demo·테스트 — 가 이 경계를 지나므로, 유효하지 않은 `agent_id`를 **구성 불가**로 만든다. "유효하지 않은 카드는 등록되지 않는다"의 *최강 지점*이다(등록 시점만 막는 안 B = `Registry.register`는, 빌더 YAML 미리보기처럼 등록 *전*에 카드를 만드는 경로를 놓친다).

**에러 경로 정합(자연 매핑).** field validator가 `ValueError`를 던지면 pydantic이 `ValidationError`로 승격한다 — 그래서:
- **빌더**: `validate_card_for_builder`가 이미 `AgentCard.model_validate`를 `try/except ValidationError`로 감싸 `{ok: False, errors: [...]}`로 매핑한다. 형식 위반이 자동으로 `ok:False`가 된다. `web.py`의 빈 agent_id ad-hoc 블록(`if not req.agent_id`)은 *이제 이중 방어*(중복이지만 무해 — 같은 `ok:False`).
- **로더**: `Registry.load`가 `AgentCard.model_validate`를 `try/except Exception → RegistryError`로 감싼다. 형식 위반 YAML이 `RegistryError("카드 로드 실패 ...")`로 매핑된다.

별도 매핑 코드 추가 0 — 기존 에러 경로가 형식 검증을 *공짜로* 얻는다.

### 3. 어댑터 가드(`git_gateway.validate_agent_id`) = 유지 (심층 방어 · 역할 분리)

admission이 근본이지만 어댑터 가드는 **제거하지 않는다**(보안 백스톱). 보안 경계는 "상위에 검증이 있다"고 없애지 않는다(defense-in-depth — `commit_okf_bundle`이 카드를 안 거치는 경로로 직접 불릴 수 있고, `extract_snapshot`은 `agent_id`를 카드가 아니라 호출 인자로 받는다). 둘의 역할을 명문화한다:

| | admission(`AgentCard._validate_agent_id_format`) | 어댑터(`git_gateway.validate_agent_id`) |
|---|---|---|
| 책임 | **형식 권위**(positive format — wire-format 규칙) | **경로 안전 백스톱**(traversal block — okf_root 밖 쓰기·tree-ish 주입 차단) |
| 패턴 | allowlist(`^[A-Za-z0-9][A-Za-z0-9_-]*\Z`) | blocklist(빈·공백·`/`·`\`·`..`·절대) |
| 강도 | 빡세다(미래 형태도 차단·길이·`.` 거부) | 좁다(알려진 탈출 형태만 — 백스톱은 넓을 필요 없음) |
| 위치 | 카드 구성(저수준) | 어댑터 commit/extract 직전 |

어댑터를 admission만큼 빡세게 만들 필요는 없다(백스톱은 좁게 — 경로 안전만). 어댑터 가드를 *느슨하게* 만들지도 않는다(보안 회귀 금지). 이 ADR은 admission을 *추가*하는 것이지 어댑터를 *약화*하는 게 아니다.

### 4. 공유 위치 = `agent_card.py`(저수준 모듈), 어댑터와 독립

정규식 상수·validator는 `agent_card.py`에 둔다(카드가 형식의 본향 — 저수준 모듈, 순환 임포트 0). `git_gateway`는 이 정규식을 **재사용하지 않는다** — 책임이 다르다(어댑터는 경로 안전, admission은 형식 권위). 둘을 한 상수로 묶으면 "백스톱을 좁게 유지"라는 역할 분리가 깨진다. 독립 유지가 의도다.

## 기각안

- **소문자 강제(`^[a-z0-9][a-z0-9_-]*$`)** — 더 깨끗하지만 `agent_X`/`agent_Y` 픽스처 변경 강요(스코프 크립). 목표는 보안 경화지 명명 스타일이 아니다. → 기각.
- **강제 지점 = `Registry.register`(안 B)** — 등록 시점만 막아 빌더 YAML 미리보기 등 등록 전 카드 구성을 놓친다. admission의 근본은 *구성 불가*다. → 기각(안 A 채택).
- **어댑터 가드 제거** — admission이 근본이라고 어댑터를 없애면 심층 방어가 무너진다(`extract_snapshot`은 카드 외 인자 경로). → 기각(유지).
- **정규식을 한 상수로 공유(어댑터=admission 재사용)** — 역할이 다르다(백스톱은 좁게). 묶으면 분리가 깨진다. → 기각(독립).

## 결과

- `AgentCard` 구성이 유효하지 않은 `agent_id`(경로 탈출·빈·공백·비형식)를 `ValidationError`로 거부한다 — 빌더 `{ok:False}`·로더 `RegistryError`로 자연 매핑.
- 어댑터 가드는 백스톱으로 남아 카드를 안 거치는 경로도 막는다(이중 보장).
- 게이트 850 passed/pyright 0/ruff 0 — 기존 `agent_id` 전부 수용, 회귀 0. `tests/test_agent_id_admission.py` 명시 단언(거부·수용·빌더/로더 경로 정합 + 제어문자/유니코드 적대적 입력 안전망).
- **미해결로 남김**: agent_id의 *명명 컨벤션*(소문자·prefix 규약 등)은 강제하지 않는다(형식 위생만 — 스타일은 별 결정). 기존 OKF 번들 디렉터리(`okf/{agent_id}/`)는 이미 형식을 따른다(소급 마이그레이션 불요).
