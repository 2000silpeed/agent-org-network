# Agent Org Network Frontend

이 Next.js 앱은 브라우저가 사용하는 유일한 frontend server입니다. 현재 지원 수준은
`runnable_developer_reference`이며, Windows native standalone Node server로 실행할 수
있습니다. Docker image는 선택적 패키징 경로입니다. FastAPI는 HTML을 제공하지 않고
JSON·SSE·WebSocket Developer API만 제공합니다.

저장소 전체의 실행 가능 상태는
[`../docs/support-contract.json`](../docs/support-contract.json)을 따릅니다.
3-install 제품은 현재 `product_target_not_available`입니다.

## 실행

필수 조건:

- Node.js 24+
- Corepack과 `package.json#packageManager`에 고정된 pnpm
- `agent_org_network.web:app` Developer API
- Windows에서는 PowerShell 5.1+ 또는 PowerShell 7만 필요하며 WSL/Git Bash/Docker는 필요하지 않음

저장소 루트에서 backend를 먼저 실행합니다.

```bash
uv sync --locked --all-extras --dev
uv run uvicorn agent_org_network.web:app --host 127.0.0.1 --port 8011
```

다른 터미널에서 frontend를 실행합니다.

```bash
cd frontend
corepack pnpm install --frozen-lockfile
AON_FRONTEND_MODE=development \
AON_BACKEND_URL=http://127.0.0.1:8011 \
corepack pnpm dev
```

브라우저에서 `http://127.0.0.1:3000`을 엽니다.

Windows PowerShell:

```powershell
Set-Location frontend
corepack pnpm install --frozen-lockfile
corepack pnpm build
$env:AON_FRONTEND_MODE = 'development'
$env:AON_BACKEND_URL = 'http://127.0.0.1:8011'
corepack pnpm start
```

## standalone server

개발 backend와 함께 production build를 실행하려면:

```bash
corepack pnpm build
AON_FRONTEND_MODE=development \
AON_BACKEND_URL=http://127.0.0.1:8011 \
corepack pnpm start
```

실제 공개 origin 뒤에서 실행할 때는 production mode를 사용합니다.

```bash
AON_FRONTEND_MODE=production \
AON_PUBLIC_ORIGIN=https://aon.example.com \
AON_BACKEND_URL=http://developer-api:8011 \
corepack pnpm start
```

production mode는 HTTPS public origin과 유효한 backend URL이 없으면 시작하지 않습니다.

Central artifact assembly는 `pnpm build`에 포함됩니다. 그것은 `.next/standalone` 안에 `public`,
`.next/static`, canonical manifest를 복사합니다. `aon-central web serve --profile`은 이 prebuilt
artifact만 실행하며 build·download를 수행하지 않습니다.
TLS는 reverse proxy나 배포 플랫폼에서 종료할 수 있지만, Browser Frontend에는
`AON_PUBLIC_ORIGIN`으로 실제 HTTPS origin을 알려야 합니다. `AON_BACKEND_URL`은
검증된 absolute `http` 또는 `https` URL이며, HTTP는 `developer-api`처럼 Browser Frontend와
같은 private trusted network에만 둡니다. 공개망 backend는 HTTPS를 사용합니다.
Central admission을 노출하는 TLS reverse proxy는 Node hop 전에 caller의 `Forwarded`,
모든 `X-Forwarded-*`, `X-AON-Admission-Proxy-Provenance`를 반드시 제거하고 public `Host`를
그대로 보존해야 합니다. 이 조건을 만족하지 않는 proxy 경로는 admission 요청을 403으로 닫습니다.

선택적 Docker 패키징:

```bash
docker build -t agent-org-network-frontend ./frontend
docker run --rm -p 3000:3000 \
  -e AON_FRONTEND_MODE=production \
  -e AON_PUBLIC_ORIGIN=https://aon.example.com \
  -e AON_BACKEND_URL=http://developer-api:8011 \
  agent-org-network-frontend
```

## Backend-for-Frontend 계약

### `/api/*`

`app/api/[...path]/route.ts`는 정적 method/path allowlist에 있는 요청만
`AON_BACKEND_URL`로 전달합니다. 임의 upstream path·host·authorization header를
전달하는 범용 proxy가 아닙니다. 요청 body 크기와 전달 header도 제한하며 SSE는
streaming 응답으로 유지합니다.

`GET /healthz`는 Next process liveness를, `GET /readyz`는 runtime 설정과 Developer API
readiness를 확인합니다.

### Card Owner 경계

중앙 Browser Frontend에는 `/owner-api/*`가 없습니다. raw source와 full draft는 Card
Owner 신뢰 경계를 벗어나지 않으므로 중앙 frontend server가 이를 upload·proxy하지
않습니다. 저작 화면의 Owner-local 기능은 현재 `product_target_not_available`입니다.

## 현재 지원 범위

- 질문 입력과 Question Request 상태 표시
- 답·대기·명시적 거절·실패 projection
- 운영/처리함 화면
- Registry User와 Agent Card 온보딩 UI
- 제한형 BFF와 health/readiness endpoint
- standalone Node build와 Docker packaging
- Docker 없이 Windows PowerShell에서 실행·검증하는 native scripts

Central browser OIDC의 네 dedicated auth BFF route와 Registry admission의 다섯 dedicated BFF route는
이 앱에 들어 있습니다. admission route는 `/api/onboarding/status`, `/api/admin/users`,
`/api/admin/agent-cards`뿐이며 Central local-reference의 fixed private API
`127.0.0.1:8010`로만 연결됩니다. `/onboarding`은 Registry User→Agent Card→Card Owner
Installation handoff이고 `/admin`은 ongoing register-only 목록/등록과 PolicyRevision/scorecard
read-only 패널을 제공합니다. `/console/org`는 safe User/Card graph projection을 전용 BFF로
읽으며, capability unavailable·권한·세션 오류를 명시합니다. 실제 IdP/TLS browser
관통, Card Owner pairing, Owner-local 문서 저작·review/publish, Question User MCP 관통 및
production Central Server는 이 앱만으로 완료할 수 없습니다. 화면별 이관
상태는 [`../docs/frontend-runtime-parity.md`](../docs/frontend-runtime-parity.md)를
참조합니다.

## P0 artifact 분리와 기능 보존 이관

RB3.2b.1에서 이 `frontend/`의 **Central Next packaging/process component**를 구현했습니다.
source checkout의 prebuilt standalone과 wheel에 포함된 standalone을
`aon-central web serve --profile`로 실행할 수 있습니다. 이것은 이 frontend의 일반 지원
상태인 `runnable_developer_reference`나 3-install 지원 수준을 승격하지 않습니다.

Central local-reference component에서는 `127.0.0.1:3000`의 Central Next가 fixed private Central API
`127.0.0.1:8010`만 BFF로 호출합니다. Card Owner 화면은 이 package/route/env를 재사용하거나
`/owner-api`를 추가하지 않고, 별 `owner-frontend/` standalone process
(`127.0.0.1:3001`)와 loopback Owner API(`127.0.0.1:8012`)로 구현합니다.

따라서 Central `/ask`, `/inbox`, `/onboarding`, `/admin`, `/console/*`와 Owner `/card`,
`/workspace`, `/drafts`, `/supervision`은 서로 다른 artifact입니다. legacy nine-screen
success/error/Authority/API parity는 모두 완료 조건이며, `web/*.html` fallback은 없습니다.
Central admin 전체 scorecard는 `/admin`, Card Owner self-supervision은 Owner `/supervision`에
분리됩니다. Central은 Owner raw/full draft/evidence를 proxy·upload·audit하지 않습니다.

Central inbox transport는 generic BFF가 아니라 ADR 0080의 13개
`/api/inbox/{conflicts,backup-reviews,reevaluations,approvals}/**` route만 제공합니다. 각 route는
fixed loopback의 동일 `/v1/inbox/**` private endpoint 하나만 호출하며 cookie-only read,
same-origin CSRF/idempotent write, exact DTO와 safe `{code,message}` 오류 projection을 강제합니다.
화면·접근성 구현은 별 E2 슬라이스입니다.

### Central component source 실행

source checkout은 runtime에 build하지 않으므로 먼저 prebuild합니다.

```bash
corepack pnpm install --frozen-lockfile
corepack pnpm build
cd ..
uv run aon-central migrate --profile /absolute/path/central-profile.json
uv run aon-central bootstrap-admin \
  --profile /absolute/path/central-profile.json \
  --attestation /absolute/path/bootstrap-admin-attestation.yaml
uv run aon-central doctor --profile /absolute/path/central-profile.json
```

API와 web은 같은 strict profile을 사용하지만 별 process입니다.

```bash
uv run aon-central api serve --profile /absolute/path/central-profile.json
```

```bash
uv run aon-central web serve --profile /absolute/path/central-profile.json
```

profile의 API bind는 `127.0.0.1:8010`, web은 `127.0.0.1:3000`으로 고정되고
`central_public_origin`은 path 없는 HTTPS origin이어야 합니다. `/healthz`는 Node process,
`/readyz`는 fixed Central API readiness를 확인합니다. caller environment나 argument로 upstream,
host 또는 port를 바꿀 수 없습니다.

### prebuilt wheel 실행

frontend를 build한 뒤 저장소 루트에서 `uv build --wheel`을 실행하면 assembled standalone이
wheel의 `agent_org_network/_central_frontend/.next/standalone`에 포함됩니다. 그 prebuilt wheel을
새 environment에 설치한 뒤에는 source checkout과 pnpm 없이 Node.js 24+만으로 API/web을 각각
실행합니다. 최초 실행에는 설치된 `aon-central migrate`, `bootstrap-admin`, `doctor`로 같은
strict profile을 준비해야 합니다.

```bash
/absolute/path/aon-central-venv/bin/aon-central api serve \
  --profile /absolute/path/central-profile.json
```

```bash
/absolute/path/aon-central-venv/bin/aon-central web serve \
  --profile /absolute/path/central-profile.json
```

이 component는 wheel 설치 후 Node `/healthz` smoke와 mock Central API의 browser-auth BFF
contract까지 검증됐습니다. 실제 IdP/TLS SSO 관통, Central의 일곱 destination, Owner-local Next와
독립 Central/Owner/MCP bundle은 미완료입니다.
따라서 현재 frontend/Developer API 지원 상태는 계속 `runnable_developer_reference`이고,
3-install은 clean-install acceptance 전까지 `product_target_not_available`입니다. 자세한 contract는
[ADR 0075](../docs/adr/0075-installable-three-artifact-and-feature-preserving-next-migration.md)를
따릅니다.

## 검증

저장소 루트에서 일반 변경은 빠른 gate를 먼저 실행합니다.

```bash
scripts/verify-fast.sh
```

frontend/API 경계를 바꾸면:

```bash
scripts/verify-contract.sh
```

전체 회귀는 main·야간·릴리스 또는 명시적 수동 확인에서 실행합니다.

```bash
scripts/verify-full.sh
```

이 gate들은 Browser Frontend와 Developer API reference의 회귀 방지 근거입니다. 실제
Central Server, Card Owner, IdP, TLS, OS keychain 또는 3-install production readiness를
증명하지 않습니다.
