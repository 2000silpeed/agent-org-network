# ADR 0072 — 실행 기준선과 지원 수준

- 상태: 채택(Accepted)
- 날짜: 2026-07-30
- 관련: ADR 0067(3-install 목표 경계), ADR 0070(thin Question User MCP),
  ADR 0071(비업무 대화 초기 처분)
- browser/API 분리, 지원 상태 명칭 및 검증 계층 변경: ADR 0073

## 맥락

저장소에는 Question Request, 온보딩, Agent Card 등록, AuthoringRun, review/publish,
pairing과 gateway의 많은 구성요소와 테스트가 있다. 그러나 문서가 구성요소 완료를
설치 가능한 제품 완료처럼 이어 적으면서 실제 패키지 진입점과 지원 범위를 파악하기
어려워졌다.

실행 사실은 다음과 같다.

- `pyproject.toml`의 console script는 `aon-mcp` 하나다.
- `aon-central`, `aon-owner`는 존재하지 않는다.
- `web:app`은 실행 가능한 API-only Developer Reference다.
- `server:central_app`, `scripts/run_central.sh`, `scripts/run_worker.sh`,
  `mcp_server`, `scripts/run_mcp.sh`는 Legacy Fixture다.
- `aon-mcp`는 설치 가능하지만 실제 OIDC·HTTPS Central gateway·OS keychain에 의존한다.
- production 온보딩·저작·pairing·gateway는 테스트에서 주입하는 component factory다.
- 공개 Owner authoring production factory는 의도적으로 unavailable이다.
- Next frontend는 실행 가능한 Browser Frontend Developer Reference이며 완성형
  production Central backend를 제공하지 않는다.

## 결정

### 1. 지원 상태를 다섯 값으로 고정한다

- `runnable_developer_reference`
- `runnable_legacy_fixture`
- `installable_dependent_client`
- `tested_component_factory`
- `product_target_not_available`

기계 판독 단일 원천은 `docs/support-contract.json`이다. README, PRD, TRD, TASK,
CONTEXT와 frontend README는 이 계약을 참조한다. 최초 채택 당시의 Demo/Visual
Prototype 명칭은 ADR 0073의 단일 Browser Frontend 결정으로 대체됐다.

### 2. 3-install은 현재 제품이 아니라 Product Target이다

ADR 0067이 정한 Central Server / Card Owner / Question User MCP의 trust boundary는
채택한 목표 아키텍처로 유지한다. 다만 ADR 0067에 적힌 `aon-central`, `aon-owner`,
배포 artifact와 표준 production app은 현재 제공되는 패키지 manifest가 아니다.

현재 3-install의 지원 상태는 `product_target_not_available`이다. 이 결정은 ADR 0067의
경계 설계를 폐기하지 않고, 그 설계를 현재 artifact로 읽는 해석만 대체한다.

### 3. 실행 가능 주장은 검증 증거에 결박한다

현재 지원으로 문서화한 명령은 실제 package script, import 가능한 app 또는 repository
script여야 한다. component factory는 테스트 증거로만 분류한다. 앞으로 지원 상태를
바꾸려면 코드, support contract, root 문서와 acceptance evidence를 같은 변경에서
갱신한다.

### 4. 재기초화에서 기능 코드를 재배치하지 않는다

이번 결정은 사실을 분류하고 SSOT를 다시 세우는 작업이다. legacy 모듈을 삭제하거나
production처럼 보이는 빈 `aon-central`/`aon-owner` 명령을 만들지 않는다. 실제
composition root와 배포 artifact는 별 기능 단계에서 테스트 우선으로 구현한다.

## 결과

- 사용자는 README의 명령만으로 현재 가능한 개발 경계를 재현할 수 있다.
- 테스트된 구성요소 수와 제품 readiness를 분리해 판단한다.
- 과거 상세 완료 이력은 ADR과 Git에 남고, TASK는 앞으로 할 일과 acceptance를 관리한다.
- `Non-actionable Conversational Intake`를 포함한 현재 Question Request 동작은 보존한다.
- 다음 제품 단계는 실제 Central Server와 Card Owner artifact를 만드는 RB3이며,
  실제 IdP/TLS/keychain/별 프로세스 관통 전에는 제품 완료를 선언하지 않는다.
