# ADR 0049 — Production bootstrap과 readiness-only 조립 경계

- 상태: 채택(Accepted)
- 날짜: 2026-07-14
- 계보: ADR 0043의 Request-first production 경계, ADR 0044의 Completion 저장 capability, ADR 0048의 Approval 운영 경계를 실제 환경 조립 전에 검증하는 P17.7 시작 관문으로 구체화한다.
- 적용 범위: P17.7 production bootstrap 계약, production-style composition 증명, 자원 소유권과 readiness-only 결과.
- 제외 범위: P17.8의 중앙 Authority/RBAC·조직 격리, P17.9의 durable linked workflow·공유 transaction·transactional outbox·lease·재시작 복구, P17.13의 암호화·보존 실행·운영 관측성.
- 구현 상태: P17.7 구현과 최종 독립 리뷰를 마쳤다. 재리뷰 결과는 **APPROVE(P0/P1/P2 0)**다. 집중 회귀 **136 passed**, 인접 parent 회귀 **301 passed**, 전체 Python **4,624 passed, 7 warnings**, Pyright 0 errors, Ruff clean을 확인했다.

## 맥락

질문 표면은 Request-first Application, Approval, Completion, SSE와 사용자 채널을 한 `QuestionSurfaceComposition`으로 묶는다. 기존 웹 진입점은 `build_demo`와 하드코딩 사용자, `RuleBasedClassifier`, InMemory workflow, `StubRuntime`, 로컬 CLI fallback을 함께 조립한다. `web.py`와 `server.py`도 시작 과정에서 demo seed와 legacy 표면을 만든다. 이 경로에 환경변수 검사만 덧붙이면 demo 의존성을 제거했다는 보장이 없다.

저장 capability도 범위가 나뉘어 있다. SQLite Completion은 한 Question Request의 terminal 결과를 원자적으로 확정하지만 Request·Conflict·Manager·Approval 전체를 한 durable transaction으로 묶지는 않는다. Approval 사건 journal·알림 dedup·복구 queue도 process-local이다. `workflow_durability="durable"` marker는 재시작 뒤 자기 데이터를 읽을 수 있다는 선언이다. 공유 transaction, outbox, lease나 다중 인스턴스 조정의 증거는 아니다.

P17.7에서 FastAPI 앱을 먼저 만들면 설정과 일부 저장소만 갖춘 상태를 실행 가능한 production으로 오해하기 쉽다. 반대로 시작 계약이 없으면 demo fallback이 production 경로에 다시 섞인다. 이번 단계는 서버를 여는 일이 아니라 production 조립이 통과해야 할 조건과 실패 의미를 코드로 고정한다.

## 결정

### 1. P17.7은 readiness-only 경계다

production bootstrap은 FastAPI 앱이나 포트를 만들지 않는다. module-level `app`도 두지 않는다. 공개 결과는 다음 둘 중 하나다.

```text
ProductionBootstrapHandle
ProductionBootstrapFailure
```

성공 handle의 범위는 `composition_contract_only`다. 주입된 durable fake로 설정·조립·소유권 계약을 통과했다는 뜻이며, HTTP 서버 실행이나 기업 파일럿 준비 완료를 뜻하지 않는다.

production bootstrap은 다음 모듈과 FastAPI를 import하지 않는다.

```text
fastapi
agent_org_network.web
agent_org_network.server
agent_org_network.demo
agent_org_network.demo_question_surfaces
agent_org_network.runtime_select
```

`build_demo`, demo seed와 runtime selector의 재노출도 금지한다. 하드코딩 사용자, `RuleBasedClassifier`, InMemory 필수 workflow, `StubRuntime`, 로컬 CLI fallback은 기본값이 될 수 없다.

P17.8·P17.9가 끝난 뒤에도 기존 demo 앱을 production mode로 바꾸지 않는다. 실제 앱이 필요해지면 별도 production 경계가 성공 handle을 받아 native 표면만 명시적으로 장착한다. legacy `/ask`, 무비밀번호 로그인, demo 콘솔과 worker WebSocket은 자동으로 따라오지 않는다.

### 2. 설정을 factory 부수 효과보다 먼저 검증한다

`ProductionBootstrapConfig`는 다음 아홉 값을 frozen·extra-forbid 모델로 읽는다.

```text
org_id
database_dsn                 # secret, network PostgreSQL
oidc_issuer                  # HTTPS
oidc_client_id
oidc_client_secret           # secret
session_secret               # secret
authority_policy_ref
provider
provider_credential          # secret
```

환경변수는 `AON_PRODUCTION_` prefix만 쓴다. 기존 `AON_DB`·`AON_PROVIDER`에는 demo/runtime selector의 fallback 의미가 있으므로 production 설정으로 승격하지 않는다. 누락·공백·형식 오류를 모두 확인한 뒤에만 dependency factory를 호출한다. 설정 오류가 있으면 DB connection, 파일 생성, 네트워크 조회, scheduler thread와 FastAPI 객체 생성은 0회다.

실패 합은 네 변이로 닫는다.

```text
ProductionBootstrapFailure =
    MissingProductionConfiguration
  | InvalidProductionConfiguration
  | ProductionDependencyUnavailable
  | ProductionCompositionRejected
```

네 변이는 공통 `cleanup_pending`과 `close()`를 제공한다. 설정 누락·설정 오류·실 어댑터 부재는 자원을 인수하지 않아 `cleanup_pending=false`다. 조립 거부 뒤 정리가 끝나지 않은 `ProductionCompositionRejected`만 `true`가 될 수 있다. cleanup 상태는 실패 원인과 별개이므로 다섯 번째 실패 변이를 만들지 않는다.

공개 오류에는 허용된 설정 키, kind, code와 `cleanup_pending`만 남긴다. 환경변수 원문, DSN, token, secret, callback, dependency 객체와 하위 예외는 `repr`·`model_dump`에 나타나지 않는다. Pydantic 입력 오류도 원본 값을 숨긴다.

### 3. actual env는 실 어댑터가 생길 때까지 닫혀 있다

bootstrap은 `ProductionDependencyFactory`를 명시적으로 받는다. 기본 demo factory나 자동 fallback은 없다. 실제 환경 전용 factory는 유효한 설정을 확인한 뒤에도 외부 자원을 만들지 않고 다음 결과를 반환한다.

```text
ProductionDependencyUnavailable(
    code="production_adapters_unavailable",
    cleanup_pending=false,
)
```

DB DSN, OIDC, Authority 정책 참조와 provider credential이 문자열로 존재한다는 사실만으로 실 어댑터가 준비됐다고 판단하지 않는다. `check_production_readiness_from_env()`는 P17.8·P17.9 전까지 항상 이 fail-closed 의미를 유지한다.

테스트는 주입된 durable fake factory로 성공 경로를 검증한다. 이 성공은 demo import와 fallback이 없고 설정 선검증, dependency identity, durability gate와 정리 순서가 맞다는 뜻이다. 실제 OIDC 서명 검증, Authority/RBAC, provider 호출, DB schema, 공유 transaction, outbox, lease, 재시작 복구와 다중 인스턴스 안전성은 증명하지 않는다.

### 4. production-style 조립은 linked Store와 Approval·Completion을 함께 검사한다

`build_question_surface_composition(..., production_style=True)`는 storage를 열기 전에 Conflict Store, Manager Store와 Approval Store의 `workflow_durability`를 읽는다. marker가 없거나 알려지지 않았거나 하나라도 `ephemeral`이면 조립을 거부한다. `InMemoryApprovalStore`는 계속 `ephemeral`이다.

Completion storage를 만든 뒤에는 다음 계약을 확인한다.

- Request Store·Completion UoW·Completion Reader가 같은 객체인가
- `question_completion_storage_capability="atomic_v1"`과 필수 callable을 제공하는가
- completion storage의 `workflow_durability`가 `durable`인가
- Approval Policy·Store·책임 resolver identity가 storage와 같은가
- Resolution과 Execution이 모두 `production_style=True`로 생성됐는가
- Approval boundary와 operations가 같은 Request·Approval·Completion·terminal publisher를 보는가

조립 중 storage나 scheduler를 인수한 뒤 실패하면 canonical builder가 scheduler를 먼저, storage를 나중에 회수한다.

`durable` marker는 component별 재시작 보존 선언일 뿐이다. Request·Conflict·Manager·Approval 사이의 원자 commit, orphan 방지, outbox 전달, lease와 exactly-once를 뜻하지 않는다.

### 5. canonical builder만 single-use attestation을 발급한다

exact `build_question_surface_composition(production_style=True)`가 모든 gate와 최종 identity 조립을 통과했을 때만 private production contract attestation을 발급한다. default/false 조립, direct dataclass 생성과 subclass에는 발급하지 않는다.

attestation은 exact `QuestionSurfaceComposition`의 weakref와 다음 13개 dependency identity snapshot을 보존한다.

```text
application
storage
approval_operations
manager_store
manager_disposition
conflict_store
conflict_disposition
deadlock_manager_disposition
registry
route_authority
grounding_knowledge_reader
grounding_terminal_failure_recorder
approval_events
```

상태는 `issued | claimed | revoked`로 닫는다. bootstrap은 exact 타입과 attestation을 함께 확인하고 `issued → claimed`를 한 번만 허용한다. snapshot 조회가 실패하거나 identity가 달라졌거나 이미 닫힌 composition이면 attestation을 영구 revoke한다. 필드를 원래 값으로 돌려도 다시 claim할 수 없다. composition close도 실제 자원을 닫기 전에 revoke한다.

shallow/deep copy는 같은 attestation 객체를 공유하지만 weakref가 원본을 가리켜 claim하지 못한다. `dataclasses.replace`는 아래 lifecycle owner 재결박 단계에서 생성 자체가 거부된다. 성공 handle은 여전히 `composition_contract_only`이며 attestation을 배포 readiness 증거로 공개하지 않는다.

### 6. production wiring은 발급 순간부터 영구 봉인한다

attestation 발급과 함께 위 13개 public dependency field를 같은 close lock 안에서 영구 봉인한다. 봉인 뒤 일반 attribute set·delete는 `production composition wiring is sealed` 고정 오류로 끝난다. claim 성공·실패, revoke, 정상 close와 부분 close 뒤에도 봉인을 풀지 않는다.

claim과 close, wiring 변경은 composition의 같은 close lock으로 선형화한다. claim은 봉인된 wiring의 현재 identity를 발급 snapshot과 다시 비교한다. 조회 예외는 외부로 반사하지 않고 attestation을 revoke한 뒤 조립 거부로 수렴한다.

direct/default composition은 기존 개발·테스트 호환성을 위해 봉인하지 않는다. 이 봉인은 일반 애플리케이션 코드의 조립 불변식이지 Python 내부를 적대적으로 격리하는 보안 장치가 아니다. `object.__setattr__`·`object.__delattr__`, private token 직접 주입 같은 의도적 우회는 trusted-process 계약 밖이다. claim 전 우회는 identity 재검증과 영구 revoke로 거부하지만, claim 뒤 hostile extension 격리까지 보장하지 않는다.

### 7. lifecycle owner가 original-only cleanup을 보장한다

모든 `QuestionSurfaceComposition`은 생성 시 private lifecycle owner token을 새로 받아 `__post_init__`에서 exact 원본에 한 번만 결박된다. production seal 여부와 무관하게 자원 수명은 최초 조립된 객체만 소유한다.

token은 원본 weakref, bound-once·closed 상태와 lock을 가진다. shallow/deep copy에는 같은 token을 넘기며 token 자체의 copy/deepcopy는 같은 객체를 반환한다. 복사본이 `close()`를 호출하면 `question surface lifecycle owner mismatch` 고정 오류가 나고 scheduler·storage write는 0이다. 원본 close만 attestation revoke와 scheduler·storage 정리를 진행한다. close가 실패하면 같은 원본이 재시도할 수 있고, 성공 뒤 반복 close는 no-op이다.

`_lifecycle_owner`는 private keyword-only init field다. `dataclasses.replace`가 원본 token을 새 객체에 넘기면 `__post_init__`의 두 번째 bind가 거부되어 replacement가 만들어지지 않는다. 원본이 GC되어 weakref가 사라져도 token을 다시 unbound로 돌리거나 copy로 ownership을 넘기지 않는다.

private init field가 Python 생성자 signature와 `dataclasses.fields()`에 보이는 점은 감수한다. `repr`과 equality, bootstrap 결과에는 나타나지 않는다. fresh private token을 직접 넘기거나 Python object 내부를 강제로 바꾸는 코드는 이 계약의 보호 대상이 아니다.

잠금 순서는 다음으로 고정한다.

```text
QuestionSurfaceComposition close lock
→ lifecycle owner lock
→ production contract attestation lock
```

역순 획득은 허용하지 않는다. alias close는 `_closed` 확인이나 attestation revoke보다 먼저 owner mismatch로 끝나므로 원본의 cleanup identity를 침범하지 않는다.

### 8. claim 전 candidate는 factory가 소유한다

composition factory가 객체를 반환했다는 사실만으로 소유권이 bootstrap에 넘어오지 않는다. attestation claim 성공만 composition ownership을 bootstrap으로 이전한다. claim 전 candidate와 factory의 부분 결과는 `ProductionDependencies`가 소유한다.

claim 실패 시 bootstrap은 candidate의 `close` descriptor를 읽거나 호출하지 않는다. 같은 attested 원본을 두 factory가 반환했거나 copy가 application·storage를 공유할 수 있기 때문이다. 두 번째 rejection이 첫 성공 handle의 자원을 닫아서는 안 된다. foreign candidate의 `close` descriptor가 예외를 내더라도 조회 자체를 하지 않으므로 raw 오류가 새지 않는다. 이 경로에서는 `ProductionDependencies.close()`만 실행한다.

factory가 dependency 묶음을 넘긴 뒤 composition 조립이나 claim이 실패하면 bootstrap은 dependency cleanup을 즉시 한 번 시도한다. 실패하면 같은 bundle과 단계 상태를 private cleanup sequence에 남기고 `ProductionCompositionRejected(cleanup_pending=true)`를 반환한다. 호출자는 같은 failure의 `close()`로 재시도한다.

성공 handle과 실패 결과는 같은 cleanup sequence 의미를 쓴다. composition과 external dependency 단계의 성공 여부를 따로 추적해 성공한 단계는 다시 실행하지 않는다. sequence의 shallow/deep copy는 같은 객체를 반환하므로 cleanup ownership도 갈라지지 않는다. 정리 오류는 하위 예외를 저장하거나 연결하지 않고 `production bootstrap cleanup failed` 고정 오류만 낸다. dependency bundle 내부 DB pool·OIDC client·provider client의 부분 성공은 factory가 제공한 close가 직접 추적한다.

## 구현 검증

구현은 다음 경계를 RED로 먼저 고정했다.

- 필수 설정 누락·오류에서 factory 부수 효과 0, secret-safe repr·serialization
- 금지 import와 FastAPI의 직접·간접 import 부재
- actual env의 `production_adapters_unavailable` 고정
- linked·Approval Store의 missing·unknown·ephemeral marker 선거부
- Completion identity·`atomic_v1`·durability와 Resolution·Execution production flag
- canonical durable fake builder만 성공하고 default/direct/subclass는 거부
- attestation single claim, 13-field identity drift·getter 장애·close 뒤 permanent revoke
- 13-field set/delete 영구 봉인과 claim·close 경쟁
- shallow/deep copy close 차단, replace rebind 거부, 원본 GC 뒤 ownership 비이전
- claim 실패 candidate descriptor 미조회와 external cleanup 지속
- cleanup 실패의 `cleanup_pending`, 같은 bundle 재시도, 성공 단계 비반복과 secret-safe 고정 오류

최종 독립 재리뷰는 P0/P1/P2 0으로 승인했다. 집중 136건과 인접 parent 301건, 전체 Python 4,624건이 통과했고 경고는 7건이었다. Pyright와 Ruff도 green이었다.

## 결과와 한계

demo와 production의 import root가 갈렸고 production 설정이 없으면 factory를 열지 않는다. 설정이 완전해도 검토된 실 어댑터가 없으면 typed failure로 멈춘다. canonical production-style composition만 identity-bound attestation을 한 번 claim할 수 있으며, wiring과 lifecycle ownership은 copy·replace·동시 close가 원본 자원을 침범하지 못하게 한다.

P17.7은 production으로 가는 시작 계약을 구현했지만 production 앱을 열지는 않는다. 중앙 Authority/RBAC와 default-deny 조직 격리는 P17.8, linked workflow의 공유 transaction·outbox·lease·재시작 복구는 P17.9가 닫는다. 그전에는 실제 FastAPI production 앱과 global `app`을 추가하지 않는다.

## 기각한 대안

- **기존 `web.create_app()`에 production flag 추가** — demo bundle과 legacy fallback을 함께 만들어 import 경계와 시작 부수 효과를 증명하기 어렵다.
- **설정 확인 뒤 FastAPI readiness endpoint 제공** — 서버가 뜬 사실을 실제 Authority와 durable workflow 준비로 오해하게 만든다.
- **누락 dependency를 InMemory나 Stub으로 대체** — 시작은 되지만 fail-closed와 durable 조립 계약을 깨뜨린다.
- **exact QSC 타입만 검사** — factory가 `production_style` 인자를 무시한 default 조립도 통과한다.
- **복사 가능한 bool marker 사용** — copy·replace가 production 계약을 승계하고 identity drift를 숨긴다.
- **claim 실패 candidate를 bootstrap이 닫기** — duplicate·copy·replace가 첫 성공 handle과 공유하는 scheduler·storage를 닫을 수 있다.
- **durability marker를 transaction 증거로 간주** — component별 재시작 보존과 여러 component의 원자 commit은 다른 보장이다.
- **원본 GC 뒤 copy에 lifecycle ownership 이전** — 어떤 alias가 원본인지 다시 증명할 수 없어 공유 자원의 늦은 종료를 허용한다.

## 불변식 자체점검

- 질문 미아 없음: P17.7은 질문을 접수하지 않는다. 실제 질문 표면은 durable workflow와 복구 계약이 생긴 뒤 연다.
- Authority 중앙: 설정 참조만으로 권한 집행을 가장하지 않고 P17.8 어댑터가 없으면 시작을 거부한다.
- 유효하지 않은 카드 등록 금지: bootstrap은 Registry admission을 우회하거나 demo seed를 등록하지 않는다.
- 전이 ≠ 기록: durability marker, attestation과 readiness 결과를 도메인 전이나 감사 기록으로 해석하지 않는다.
