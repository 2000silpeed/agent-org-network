# Agent Org Network

조직 질문을 `Question Request`로 기록하고, `Agent Card`와 중앙 Authority를 기준으로
담당 경로를 결정하는 실험적 구현입니다.

> 현재 저장소는 **개발·검증 기준선**입니다. 설치 가능한 완성형 기업 제품이나
> production/pilot-ready 시스템이 아닙니다. `aon-central`과 `aon-owner`의 fail-closed
> entrypoint skeleton은 생겼지만 완성된 `Central Server`와 `Card Owner` 제품 실행물은
> 아직 제공하지 않습니다.

실행 가능 여부의 기계 판독 단일 기준은
[`docs/support-contract.json`](docs/support-contract.json)입니다. 문서와 패키지 진입점은
이 계약을 함께 변경하지 않으면 지원 상태를 바꿀 수 없습니다.

## 현재 지원 상태

| 표면 | 지원 상태 | 실제 실행/조건 |
|---|---|---|
| Developer API | `runnable_developer_reference` | JSON/SSE/WS 전용 `agent_org_network.web:app` |
| Browser Frontend | `runnable_developer_reference` | Next standalone 서버; Developer API 필요 |
| legacy 중앙 수동 시연 | `runnable_legacy_fixture` | `server:central_app`, `scripts/run_central.sh` |
| legacy Owner Worker 수동 시연 | `runnable_legacy_fixture` | 필수 Owner 인자를 받는 `scripts/run_worker.sh` |
| 인프로세스 MCP | `runnable_legacy_fixture` | `mcp_server`, `scripts/run_mcp.sh` |
| Question User MCP | `installable_dependent_client` | `aon-mcp`; 별도 IdP·HTTPS gateway 필요 |
| 온보딩·저작·pairing·gateway 조립 | `tested_component_factory` | 테스트에서 주입해 검증하는 팩토리; 배포 서버/CLI 아님 |
| A2A Remote Runtime | `tested_component_factory` | Card Owner outbound-only strict A2A 1.0 `HTTP+JSON` adapter; active Owner profile loader는 아직 없음 |
| Central RB3.1a/RB3.2a/RB3.2b.1 entrypoint | `tested_component_factory` | `aon-central migrate\|doctor\|bootstrap-admin\|api serve\|web serve`; durable intake/bootstrap과 prebuilt Central Next packaging/process component |
| Owner entrypoint skeleton | `tested_component_factory` | `aon-owner`; API health/readiness 외 pair/workspace/worker는 unavailable. profile metadata만으로 paired를 주장하지 않고, 주입형 active-binding 검증이 없는 기본 경로는 fail-closed |
| 3-install 제품 | `product_target_not_available` | `Central Server` / `Card Owner` / `Question User MCP` 목표 구조 |

이 표에서 `tested_component_factory`는 “코드와 결정론 테스트가 있다”는 뜻이지,
사용자가 설치해 운영할 수 있다는 뜻이 아닙니다. 과거 구성요소 구현 이력은 ADR과 Git
이력에 남고, 제품 완료 여부는 이 표와 [제품 요구사항](docs/prd-v0.md)의 acceptance gate로
판정합니다.

## 실행 가능한 Browser Frontend

필수 조건은 Python 3.12+, `uv`, Node.js 24+와 Corepack입니다. 회사 Windows 환경에서는
Docker Desktop·WSL·Git Bash 없이 PowerShell과 SQLite만으로 실행할 수 있습니다. 브라우저는
FastAPI 포트가 아니라 Next 포트로만 접속합니다.

```bash
git clone https://github.com/2000silpeed/agent-org-network.git
cd agent-org-network
uv sync --locked --all-extras --dev
uv run uvicorn agent_org_network.web:app --host 127.0.0.1 --port 8011
```

다른 터미널에서 Browser Frontend를 시작합니다.

```bash
cd frontend
corepack pnpm install --frozen-lockfile
corepack pnpm build
AON_FRONTEND_MODE=development \
AON_BACKEND_URL=http://127.0.0.1:8011 \
corepack pnpm start
```

브라우저에서 `http://127.0.0.1:3000`을 엽니다. Developer API는 HTML을 제공하지
않습니다. API 자체는 다음처럼 확인할 수 있습니다.

Windows PowerShell에서는 저장소가 제공하는 native 스크립트를 사용할 수 있습니다.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
uv sync --locked --all-extras --dev
Start-Process powershell -ArgumentList '-NoExit', '-File', '.\scripts\run-web.ps1', '8011'
Set-Location frontend
corepack pnpm install --frozen-lockfile
corepack pnpm build
$env:AON_FRONTEND_MODE = 'development'
$env:AON_BACKEND_URL = 'http://127.0.0.1:8011'
corepack pnpm start
```

PowerShell 검증 명령은 `.scripts\verify-fast.ps1`와
`.scripts\verify-contract.ps1`입니다. 두 명령 모두 Docker daemon을 호출하지 않습니다.

```bash
curl -sS http://127.0.0.1:8011/ask \
  -H 'content-type: application/json' \
  -d '{"question":"안녕"}'
```

`안녕`, `안녕하세요`, `hello` 같은 exact-only 단독 인사는 담당 지정으로 보내지
않고 `non_actionable_conversation`으로 종결합니다. 반면
`안녕하세요, 환불 규정은 어떻게 되나요?`처럼 업무 질문이 포함된 입력은 정상 라우팅하며,
실제 0매칭 질문은 `Unowned`로 남아 root User/Manager 처분을 기다립니다.

이 reference는 인메모리/로컬 개발 조립, 샘플 Registry와 개발용 설정을 포함합니다. 실제 조직
데이터, 인터넷에 공개된 포트, production Authority를 연결하지 마세요.

## standalone 운영 경계 (Docker는 선택 사항)

Next는 `output: standalone`로 빌드됩니다. Windows native 운영은 빌드된 standalone을
Node.js로 직접 실행하며 Docker가 필요하지 않습니다. `frontend/Dockerfile`은 Linux/Docker를
선택하는 배포팀을 위한 패키징 예시일 뿐 지원 경로의 전제 조건이 아닙니다.

Windows native standalone 실행:

```powershell
Set-Location frontend
corepack pnpm install --frozen-lockfile
corepack pnpm build
$env:AON_FRONTEND_MODE = 'production'
$env:AON_PUBLIC_ORIGIN = 'https://aon.example.com'
$env:AON_BACKEND_URL = 'https://aon-api.internal.example.com'
corepack pnpm start
```

Docker를 사용할 수 있는 배포 환경에서만 다음 선택 명령을 사용합니다.

```bash
docker build -t aon-frontend ./frontend
docker run --rm -p 3000:3000 \
  -e AON_FRONTEND_MODE=production \
  -e AON_PUBLIC_ORIGIN=https://aon.example.com \
  -e AON_BACKEND_URL=https://aon-api.internal.example.com \
  aon-frontend
```

production mode는 HTTPS public origin과 검증된 backend URL 없이는 시작하지 않습니다.
`/healthz`는 Node liveness, `/readyz`는 backend readiness입니다. Central Browser
Frontend에는 `/owner-api/*`가 없으며 Card Owner 원문·전체 초안을 중계하지 않습니다.
자세한 내용은
[`frontend/README.md`](frontend/README.md)에 있습니다.

Windows에서 중앙 reference를 실행할 때는 PowerShell 창을 여러 개 열어
`.\scripts\run-central.ps1`, `.\scripts\run-web.ps1` 또는 `aon-central`의 API/web
명령을 각각 실행합니다. 프로세스 간 통신은 loopback TCP와 SQLite를 사용하며 Docker
network나 Linux shell을 전제로 하지 않습니다.

## Legacy Fixture

아래 경로는 과거 분산 동작과 MCP 계약을 수동으로 살펴보기 위한 fixture입니다.

```bash
scripts/run_central.sh 8000 127.0.0.1
scripts/run_worker.sh cs_lead primary 8000 127.0.0.1
scripts/run_mcp.sh
```

이 경로에는 개발용 Registry, legacy dispatcher, 로컬 Runtime 선택지와 인증 stub이 섞여
있습니다. 3-install 제품, production Central gateway 또는 현재 Question User MCP의
end-to-end 경로로 간주하지 않습니다. Worker의 기본 Runtime은 로컬 `claude` 로그인이
필요하며, 다른 선택 Runtime은 해당 provider extra와 credential이 필요합니다.

## A2A Remote Runtime 개발 통합

A2A는 MCP나 중앙 라우팅을 대체하지 않습니다. Card Owner가 선택한 외부 실행 Runtime으로만
사용하고, 완료된 text-only 결과를 기존 `SubmitAnswer → ApprovalPolicy → Answer Finalization`
경로에 합류시킵니다. 외부 Remote A2A Agent Card는 통신 metadata이며 내부 `Agent Card`,
Authority 또는 Registry 등록 근거가 아닙니다.

공식 Python SDK adapter를 포함하려면 선택 extra를 설치합니다.

```bash
uv sync --extra a2a
uv run pytest -q tests/test_a2a_remote_runtime.py tests/test_a2a_sdk_adapter.py
```

현재 조립 단위는 Python component API와 결정론 SDK 계약입니다.

```python
from agent_org_network.a2a_remote_runtime import (
    A2ARemoteRuntime,
    A2ARemoteRuntimeProfile,
)
from agent_org_network.a2a_sdk_adapter import A2ASdkInvocationAdapter

profile = A2ARemoteRuntimeProfile.model_validate(owner_local_profile)
invocation = A2ASdkInvocationAdapter(credentials=owner_keychain_credential_provider)
runtime = A2ARemoteRuntime(profile=profile, invocation=invocation)
```

`owner_local_profile`과 keychain provider는 향후 Card Owner 설치의 composition root가
주입해야 합니다. profile은 active Owner Installation binding, canonical HTTPS
endpoint, Remote A2A Agent Card SHA-256, `protocolVersion="1.0"`,
`protocolBinding="HTTP+JSON"`과 opaque credential reference를 포함합니다. SDK adapter는
고정 well-known card 경로, digest/interface/bearer 정합, no-redirect와 response/time limit을
검증하고 direct Message 또는 completed Task의 단일 text-only Artifact만 수용합니다.

위 기본 adapter는 DNS 결과와 실제 dial을 결박하는 SNI 보존 transport가 아직 없어 호출 시
`A2ARemoteUnavailable`로 fail-closed합니다. `for_test(...)`는 exact `httpx.MockTransport`만
받아 공식 SDK codec/client 계약을 검증하며 실제 네트워크를 열지 않습니다. 따라서
`AON_PROVIDER=a2a`, `aon-owner worker --profile`, inbound
A2A server, 자동 discovery/등록 또는 production A2A 지원을 주장하지 않습니다. 결정론
상호운용 계약은 [ADR 0074](docs/adr/0074-card-owner-strict-a2a-outbound-runtime.md), 실제
HTTPS/OOB credential 관통은 [TASK RB3.3a](docs/tasks-v0.md)의 Manual Acceptance에 남아
있습니다.

## Central Next artifact (RB3.2b.1 component)

RB3.2b.1의 packaging/process component는 구현·검증됐습니다. Central Next는 Central API와
별도 child process이고, 이 완료는 support status, 전체 Central Server 또는 3-install을
승격하지 않습니다. strict Central profile에는 HTTPS `central_public_origin`이 있어야 하며
API bind는 `127.0.0.1:8010`, web bind는 `127.0.0.1:3000`으로 고정됩니다.

source checkout에서는 먼저 standalone을 prebuild합니다.

```bash
uv sync --locked --all-extras --dev
cd frontend
corepack pnpm install --frozen-lockfile
corepack pnpm build
cd ..

uv run aon-central migrate --profile /absolute/path/central-profile.json
uv run aon-central bootstrap-admin \
  --profile /absolute/path/central-profile.json \
  --attestation /absolute/path/bootstrap-admin-attestation.yaml
uv run aon-central doctor --profile /absolute/path/central-profile.json
```

그 뒤 서로 다른 터미널에서 API와 web process를 실행합니다.

```bash
uv run aon-central api serve --profile /absolute/path/central-profile.json
```

```bash
uv run aon-central web serve --profile /absolute/path/central-profile.json
```

`web serve`는 API를 함께 시작하지 않습니다. `http://127.0.0.1:3000/healthz`는 Node
liveness이고 `/readyz`는 고정된 `http://127.0.0.1:8010/readyz`까지 확인합니다.
명령은 caller의 `AON_BACKEND_URL`, `HOSTNAME`, `PORT`를 신뢰하지 않고 build·download도
수행하지 않습니다.

현재 Central Next가 제공하는 control-plane BFF는 `/api/admin/policy`, `/api/console/org`,
`/api/admin/scorecard`와 Card Owner 전이·해제 경로입니다. 정책 경로는 v20 DB PolicyRevision을
사용합니다. Card Owner 전이·해제와 조직 그래프/scorecard의 성공 응답은 v21 Card/Registry
same-UoW mutation port가 연결되기 전까지 `503 central_admin_unavailable`로 fail-closed하며,
metadata-only 또는 임시 성공을 반환하지 않습니다.

배포용 wheel은 frontend를 prebuild한 source state에서 만듭니다.

```bash
uv build --wheel
python3.12 -m venv /absolute/path/aon-central-venv
/absolute/path/aon-central-venv/bin/pip install /absolute/path/prebuilt-agent-org-network.whl
```

Windows PowerShell에서는 같은 wheel을 다음처럼 설치합니다.

```powershell
uv build --wheel
py -3.12 -m venv .\aon-central-venv
$wheel = Get-ChildItem .\dist\*.whl | Select-Object -First 1
.\aon-central-venv\Scripts\python.exe -m pip install $wheel.FullName
.\aon-central-venv\Scripts\aon-central.exe migrate --profile C:\AON\central-profile.json
```

이 wheel은 assembled standalone을
`agent_org_network/_central_frontend/.next/standalone`에 포함합니다. 설치된 환경에서는
`pnpm`이나 source checkout 없이 Node.js 24+와 strict profile만 준비합니다. 최초 실행에는
설치 환경의 `aon-central migrate`, `bootstrap-admin`, `doctor`를 위 source 절차와 동일하게
한 번 수행한 뒤 다음 두 process를 각 터미널에서 실행합니다.

```bash
/absolute/path/aon-central-venv/bin/aon-central api serve \
  --profile /absolute/path/central-profile.json
```

PowerShell에서 두 process를 실행할 때는 다음 entrypoint를 각각 별도 창에서 사용합니다.

```powershell
.\aon-central-venv\Scripts\aon-central.exe api serve --profile C:\AON\central-profile.json
.\aon-central-venv\Scripts\aon-central.exe web serve --profile C:\AON\central-profile.json
```

```bash
/absolute/path/aon-central-venv/bin/aon-central web serve \
  --profile /absolute/path/central-profile.json
```

wheel 설치·Node `/healthz` smoke까지 검증됐지만, 이것은 향후 요구되는 독립
`agent-org-central` bundle/image가 아닙니다. browser SSO, Central의 일곱 product destination,
Owner-local Next와 세 독립 install bundle은 후속 RB3 단계에 남아 있습니다.

## Question User MCP 통합 계약

Question User용 artifact가 노출하는 CLI는 `aon-mcp` 하나입니다. 같은 개발 package에는
별 trust boundary의 `aon-central`·`aon-owner` 단계별 entrypoint도 존재합니다.

```bash
uv run aon-mcp --help
uv run aon-mcp pair --help
uv run aon-mcp serve-stdio --help
```

이 CLI는 `{ask_org, get_question}` 두 도구만 노출하는 thin client입니다. 실제 사용에는
다음 외부 조건이 모두 필요합니다.

- `owner-keychain` extra와 동작하는 OS keychain
- 실제 OIDC authorization/token endpoint
- HTTPS로 제공되는 호환 Central question gateway
- gateway가 해석할 수 있는 현재 `Registry User`와 중앙 Authority
- pair 결과를 보관할 사용자 전용 profile 경로

예시 명령 형식은 다음과 같지만, 이 저장소만 실행해서는 pair가 성공하지 않습니다.

```bash
uv run aon-mcp pair \
  --profile /absolute/path/unpaired-profile.json \
  --output /absolute/path/paired-profile.json \
  --port 8765

uv run aon-mcp serve-stdio \
  --profile /absolute/path/paired-profile.json
```

CLI argument로 User ID, 조직, 역할 또는 token을 주입하는 우회로는 없습니다.
[`mcp_server.py`](src/agent_org_network/mcp_server.py)는 별개의 개발 fixture입니다.

## Central bootstrap-admin component 사용

`aon-central bootstrap-admin`은 제품 설치 완료 명령이 아니라, 이미 준비된
local-reference Central profile에서 최초 root User를 한 번 admission하는
`tested_component_factory` slice입니다. Central profile, Authority snapshot, one-time attestation은
조직의 별도 bootstrap control로 준비되어야 합니다. README의 placeholder나 임의 user/email/token으로
만들어서는 안 됩니다. Central profile은 strict JSON이고, attestation은 strict JSON 또는 YAML입니다.

개발 checkout에서 command shape와 준비 상태를 확인하는 명령은 다음과 같습니다.

```bash
uv sync --locked --all-extras --dev
uv run aon-central --help
uv run aon-central migrate --profile /absolute/path/central-profile.json
uv run aon-central bootstrap-admin \
  --profile /absolute/path/central-profile.json \
  --attestation /absolute/path/bootstrap-admin-attestation.yaml
uv run aon-central doctor --profile /absolute/path/central-profile.json
```

bootstrap command는 configured issuer의 RFC 8628 device authorization을 수행합니다. verification
URI와 one-time user code는 stdout/stderr가 아니라 실행 터미널의 `/dev/tty`에만 한 번 표시됩니다.
non-TTY, schema/Authority/attestation 불일치, OIDC 검증 실패는 fail-close하며 User를 만들지 않습니다.
성공 또는 exact replay만 exit `0`이고 conflict `75`, denial `77`, configuration `78`, unavailable
`69`입니다. 공개 출력에는 opaque attestation/User reference, revision과 receipt/seal digest만
있으며 email, OIDC claim, token, device code, user code는 표시되지 않습니다.

이 명령은 Central Next, Owner API, Owner-local Next, raw source/full draft 또는 A2A 경로를 호출하지
않습니다. 질문 routing/answer lifecycle, 일반 browser SSO, 추가 Registry User onboarding, Card Owner
pair/workspace/worker도 제공하지 않습니다. 정확한 attestation/profile field와 one-time replay 경계는
[ADR 0076](docs/adr/0076-one-time-oidc-bootstrap-admin-admission.md)를 따릅니다.

## Product Target: 3-install

목표 아키텍처는 다음 세 trust artifact입니다.

1. `Central Server`: 조직 Registry, Authority, SSO, control aggregate, 공개된 지식 인덱스,
   Question Request와 감사 기록을 소유합니다.
2. `Card Owner`: 원문·전체 초안·로컬 Runtime·로컬 작업공간을 소유하고 검토·공개 명령만
   중앙에 보냅니다.
3. `Question User MCP`: 사용자의 OIDC 세션으로 중앙 HTTPS gateway만 호출합니다.

현재 `aon-central`과 `aon-owner` 패키지 명령의 fail-closed 진입점은 존재합니다.
Central은 strict OIDC→Registry User→Authority 검증 뒤 durable `Received` 질문 접수·본인 조회,
question lifecycle, inbox metadata/control, onboarding admission, v20 PolicyRevision control-plane과
prebuilt Central Next packaging/process component까지 결정론적으로 조립됐습니다. 실제 외부 IdP/TLS
browser 관통, v21 Card Owner mutation의 원장 통합, console/admin의 남은 exact route와 Owner
pair/workspace/worker/Next는 아직 별도 acceptance로 남아 있습니다. 이 명령과 현재 bundled wheel의
존재를 전체 Central 또는 세 독립 설치물 완성으로 해석하지 않습니다. Central API의 browser OIDC
session 경계와 Central Next의 dedicated auth/question/inbox/policy BFF는 구현됐지만, 실제 IdP/TLS
browser 관통은 manual acceptance 전까지 사용자 기능으로 승격하지 않습니다. 로그인 시작은 JavaScript fetch가 아니라
same-origin native `POST` form을 사용하며 `navigate`/`document` Fetch Metadata 뒤 외부 IdP `303`으로
top-level 이동합니다. logout만 same-origin fetch의 `cors`/`empty` 계약을 유지합니다.
이 경계 결정은 [ADR 0067](docs/adr/0067-three-install-product-boundaries-and-pairing.md),
현재 실행 기준선은
[ADR 0072](docs/adr/0072-runtime-baseline-and-support-levels.md)에 기록합니다.

### P0: local reference를 위한 실제 artifact 계획

합의된 다음 단계는 기능을 줄이지 않고 세 artifact를 구현하는 것입니다. Central Installation은
Central Next(`127.0.0.1:3000`)와 private Central API(`127.0.0.1:8010`), Card Owner
Installation은 별 Owner-local Next(`127.0.0.1:3001`)와 loopback Owner API
(`127.0.0.1:8012`)/Worker/workspace로 구성합니다. Question User는 listener 없는 기존
`aon-mcp` thin stdio client입니다. port는 test tenant local reference의 기본값입니다.

Central은 Registry·Authority·workflow·published index만, Owner는 raw source/full draft/Git/
provider credential만 소유합니다. Central Next는 Owner API를 proxy하지 않으며 Central은 raw
evidence 또는 A2A를 relay하지 않습니다. Windows Owner secret bundle은 current-user DPAPI로
암호화 저장할 수 있습니다. 현재 pair/redeem은 `OwnerPairingOrchestrator` 테스트 component까지
확장됐고, secret-free request digest 선행 CAS와 recovery/finalize 순서를 검증합니다. Owner API에는
`/v1/pairing/status`·`/v1/pairing/redeem` route shape와 `OwnerPairingAdapter` injection seam이
있으며, CLI pair는 pairing JSON을 stdin에서만 읽고 code를 profile/argv/output에 저장하지 않습니다.
concrete keychain/device-material provider가 없는 clean-install 기본 경로는 명시적으로 unavailable이며,
이 seam은 production pairing 관통 완료를 주장하지 않습니다.
과거 아홉 HTML의 exact route/API/error/authority
destination은 [frontend runtime parity](docs/frontend-runtime-parity.md), command/file/acceptance
shape는 [ADR 0075](docs/adr/0075-installable-three-artifact-and-feature-preserving-next-migration.md)에
있습니다.

RB3.2b.1의 `aon-central web serve --profile CENTRAL_PROFILE` packaging/process component는
완료됐습니다. source checkout의 prebuilt `frontend/.next/standalone` 또는 wheel에 포함된
bundled standalone만 실행하며 자동 build/download를 하지 않습니다. 이는 Central API와 Next의
결정론적 local-reference 표면을 제공하지만, 실제 external IdP/TLS 관통·Owner Next 또는 독립
Central/Owner/MCP bundle을 제공하지 않습니다.

clean install, test OIDC issuer,
loopback TLS, test keychain, separate processes, 9-screen parity와 Full Gate/review가 갖춰진 뒤에만
`runnable_local_reference` 지원 수준을 새로 정의·승격할 수 있습니다. 실제 enterprise IdP,
public TLS, OS keychain, separate machine, backup/restore와 observability는 그 뒤 P1
production/pilot Manual Acceptance입니다.

## 핵심 도메인 동작

- 모든 업무 질문은 먼저 `Question Request`가 됩니다.
- 0매칭은 `Unowned`, 복수 책임 후보는 `Contested`이며 임의 담당자를 확정하지 않습니다.
- 중앙 Authority만 권한을 선언합니다. `Agent Card`는 능력과 under-claim만 설명합니다.
- 승인 전 후보 답은 최종 답이 아니며, 모든 사용자 표면은 공통 Answer Finalization 결과를
  사용합니다.
- 전이는 도메인 상태 변경이고, 기록은 audit/outbox 증거입니다.
- 비업무 단독 대화는 `Non-actionable Conversational Intake`로 명시 종결합니다.

도메인 언어는 [`CONTEXT.md`](CONTEXT.md), 설계는 [`docs/trd-v0.md`](docs/trd-v0.md),
진행 순서는 [`docs/tasks-v0.md`](docs/tasks-v0.md)를 따릅니다.

## 검증

기본 작업과 pull request는 Fast Gate만 먼저 실행합니다.

```bash
scripts/verify-fast.sh
```

API/frontend/infra 계약 변경은 다음을 추가합니다.

```bash
scripts/verify-contract.sh
```

전체 6천여 Python 회귀와 production build는 매 작은 변경에 반복하지 않고
main/nightly/release 또는 명시 수동 실행에서 보존합니다.

```bash
scripts/verify-full.sh
```

Fast/Contract 통과는 빠른 피드백 근거이고 Full Gate는 넓은 회귀 근거입니다. 어느 쪽도
실제 IdP/TLS/keychain,
별도 프로세스·머신, PostgreSQL, backup/restore, 다중 인스턴스와 운영 관측성을 증명하지
않습니다.

2026-07-31 RB3.2a 기준 Fast는 Python 486개와 frontend 32개, Contract는 139개가
통과했습니다. 마지막 Full 기준선은 Python 7,067개 4분 42초와 frontend production
build입니다. 일반 변경에서 Full을 반복하지 않고 RB3.8에서 한 번 다시 실행합니다.
