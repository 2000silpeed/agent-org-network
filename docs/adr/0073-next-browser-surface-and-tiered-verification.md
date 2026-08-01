# ADR 0073 — Next-only Browser Surface와 계층형 검증

- 상태: 채택(Accepted)
- 날짜: 2026-07-30
- 관련: ADR 0067(3-install trust boundary), ADR 0072(실행 지원 기준선)

## 맥락

현재 저장소는 FastAPI가 `web/*.html` 아홉 개를 직접 반환하면서, 별도 Next 앱이 같은
질문·처리함·운영·온보딩 화면을 다시 제공한다. 일부 기능은 중복되고 일부는 한쪽에만
있어 사용자가 어느 화면을 실행해야 하는지 알기 어렵다.

Python 전체 suite는 6천 건 이상이며 최근 로컬 실행은 약 5분이었다. 핵심 Question
Request·보안·지원 계약 413건은 약 2.2초에 끝나지만 CI와 작업 절차는 둘을 구분하지
않는다. 그 결과 작은 변경에도 전체 suite와 frontend build를 반복하는 비효율이 생겼다.

## 결정

### 1. Browser Frontend는 Next 서버 하나다

지원하는 브라우저 진입점은 `frontend/`의 Next standalone Node 서버 하나로 한정한다.
페이지, 정적 asset, same-origin `/api/*` BFF, `/healthz`, `/readyz`를 이 서버가 소유한다.

FastAPI Developer API는 JSON, SSE와 필요한 WebSocket만 제공한다. `/`, `/inbox`,
`/builder`, `/monitor/view`, `/org/view`, `/console/view`, `/supervision`, `/admin`
같은 HTML page route를 제공하거나 Next로 redirect하지 않는다.

### 2. 기존 HTML UI는 명시적으로 retire한다

`web/*.html`의 browser runtime은 종료한다. 대응 API는 삭제하지 않는다.

- 질문, 처리함, 빌더, 조직/감사 요약, User/Card 온보딩은 현재 Next 화면을 사용한다.
- legacy HTML에만 있던 audit detail, scorecard, supervision, token/session control,
  Card owner transfer와 builder commit UI는 API를 유지하고 Next 이관 backlog로 남긴다.
- `owner-drafts.html`은 Central Next로 이관하지 않는다. 실제 Owner-local UI는 RB3.2의
  별 Card Owner Installation에서 구현한다.
- `docs/scenario.html`은 문서 artifact이며 runtime frontend가 아니다.

기능을 Next에 이관하지 않았다는 사실을 숨기지 않으며, retire된 HTML을 fallback으로
다시 열지 않는다. 세부 이관 상태는 `docs/frontend-runtime-parity.md`에서 추적한다.

### 3. Central Browser Frontend는 Owner 원문·초안을 중계하지 않는다

Central mode의 Next 서버에서 `/owner-api/*`를 제거한다. raw source, full draft,
Owner credential은 ADR 0067의 Card Owner 경계에 남는다. 같은 Next 기술을 Owner-local
artifact에 재사용할 수는 있지만 Central 배포와는 별 mode, 별 process, 별 trust artifact다.

### 4. 운영 가능한 standalone server 경계를 제공한다

- Next는 `output: "standalone"`으로 빌드한다.
- `AON_FRONTEND_MODE=development|production`을 명시한다.
- development만 loopback HTTP backend 기본값을 허용한다.
- production은 `AON_BACKEND_URL`과 HTTPS `AON_PUBLIC_ORIGIN`을 필수로 하고 부적합하면
  fail-close한다.
- BFF는 method/path/header/body size allowlist를 사용하며 요청이 upstream origin을
  결정할 수 없다.
- `/healthz`는 Node process liveness, `/readyz`는 runtime config와 private Developer
  API readiness를 확인한다.
- TLS와 HSTS는 reverse proxy/managed hosting이 종료한다.

이 서버가 운영 가능하다는 것은 browser artifact를 배포할 수 있다는 뜻이다. 아직
존재하지 않는 production Central Server, 실제 IdP, durable production DB 또는 3-install
완료를 뜻하지 않는다.

### 5. 테스트를 목적과 비용으로 계층화한다

| Gate | 책임 | 기본 실행 |
|---|---|---|
| Fast Gate | 핵심 불변식, 보안, 지원 계약, frontend unit/type/lint | 모든 로컬 변경과 PR |
| Contract Gate | API-only route, BFF allowlist, runtime env, standalone/Docker 계약 | 관련 PR |
| Affected Gate | 변경 경로에 대응하는 기존 상세 suite | 관련 코드 PR |
| Full Gate | 전체 pytest, 전체 Pyright/Ruff, frontend production build | main/nightly/release/manual |
| Scale Gate | 실제 scale dataset | schedule/manual |
| Manual Acceptance | Docker/HTTPS/실 IdP·keychain·3-process | RB3 이후 |

Fast Gate 목표는 90초 이하다. Contract Gate 목표는 3분 이하다. 기존 상세 테스트는
삭제하지 않고 Full/Affected Gate에 보존한다. Authority, identity, approval, persistence,
raw/draft 경계 변경은 Fast 통과만으로 merge 완료를 주장할 수 없는 고위험 변경이다.

### 6. CI 이벤트를 분리한다

- pull request: Fast + Contract
- main push: Fast + Contract + Full
- nightly/manual/release: Full, 필요 시 Scale/Docker smoke

frontend CI는 pnpm 11.18.0, unit test, TypeScript, lint, build를 모두 실행한다.

## 결과

- 사용자는 Next 서버 하나만 브라우저 frontend로 운영한다.
- Developer API는 browser HTML을 소유하지 않는다.
- Card Owner 데이터 경계가 Central BFF에 섞이지 않는다.
- 작은 변경은 수십 초 안에 피드백을 받고, 전체 회귀 증거는 main/nightly/release에서
  계속 유지한다.
- 미이관 UI 기능은 fallback HTML로 숨기지 않고 명시 backlog가 된다.
