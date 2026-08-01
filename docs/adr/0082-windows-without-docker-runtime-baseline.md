# ADR 0082 — Windows·Docker 미사용을 기본 실행 기준으로 고정

- 상태: accepted (2026-08-01)
- 적용: RB3 설치·실행·검증 기준선
- 관련: ADR 0072(실행 기준선), ADR 0075(세 설치물), ADR 0081(Central 운영 제어면)

## 맥락

회사 기본 운영 환경은 Windows이며 Docker Desktop을 설치하거나 사용할 수 없다. 기존
문서와 일부 검증 이름은 Docker packaging을 기본 경로처럼 보이게 하고, 실행 명령도
POSIX shell에만 제공되어 Windows 사용자가 실제 Central/Frontend 프로세스를 시작하거나
검증하기 어렵다.

## 결정

1. **기본 지원 경로는 Docker 없는 Windows native 실행**이다.
   - Python 3.12+, `uv`, Node.js 24+, Corepack/pnpm, SQLite와 PowerShell 5.1+ 또는
     PowerShell 7만 필요하다.
   - WSL, Git Bash, Docker Desktop, Linux VM은 요구사항이 아니다.
   - Central API, Central Next, Question User MCP, Owner 프로세스는 Windows에서
     각각 별도 프로세스로 실행한다.
2. 저장소는 동일 동작을 위한 `.ps1` 실행 스크립트를 제공한다.
   - `run-web.ps1`, `run-central.ps1`, `run-worker.ps1`, `run-mcp.ps1`
   - `verify-fast.ps1`, `verify-contract.ps1`, `verify-full.ps1`
   - 스크립트는 `Set-StrictMode`, `$ErrorActionPreference = 'Stop'`, 명시적 working
     directory와 인자 검증을 사용하며 Docker 명령을 호출하지 않는다.
3. Dockerfile과 Docker smoke는 **선택적 배포 패키징 검증**으로만 유지한다.
   - Docker가 없는 환경의 설치·실행·Fast/Contract 게이트는 Dockerfile을 읽거나
     Docker daemon을 호출하지 않고 통과해야 한다.
   - Docker 사용을 요구사항, 런타임 의존성 또는 지원 상태 승격 조건으로 삼지 않는다.
4. Windows 환경변수 표기와 POSIX 표기는 README/frontend README/support-contract에
   각각 제공한다. PowerShell의 `$env:AON_*` 설정은 현재 프로세스 범위로 한정하고,
   영속 secret은 사용자가 지정한 profile/Windows secret store 경계로 남긴다.
5. 경로·포트·SQLite semantics는 OS에 따라 달라지지 않는다. Windows 경로는 CLI에서
   absolute path로 받고, shell quoting 차이는 PowerShell 스크립트가 처리한다.

## 결과

- Windows + Docker 미설치 환경이 설치·실행의 첫 번째 지원 기준이 된다.
- Docker artifact는 선택적으로 재현 가능하지만 제품 실행을 위해 필요하지 않다.
- CI의 Linux/Docker packaging 검증과 Windows native 지원 검증은 서로 다른 증거이며,
  하나가 다른 하나를 대체한다고 주장하지 않는다.
