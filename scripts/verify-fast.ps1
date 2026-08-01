Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Invoke-Checked.ps1')
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

Invoke-Checked 'uv' @('run', 'ruff', 'check', '.')
Invoke-Checked 'uv' @('run', 'pytest', '-q', 'tests/test_a2a_remote_runtime.py', 'tests/test_support_contract.py', 'tests/test_central_bootstrap_admin.py', 'tests/test_central_question_intake.py', 'tests/test_smoke.py', 'tests/test_question_request.py', 'tests/test_question_request_sqlite.py', 'tests/test_question_resolution_application.py', 'tests/test_auth.py', 'tests/test_security_regression.py', 'tests/test_ci_workflow.py')
Push-Location (Join-Path $root 'frontend')
try {
    Invoke-Checked 'corepack' @('pnpm', 'test')
    Invoke-Checked 'corepack' @('pnpm', 'exec', 'tsc', '--noEmit')
    Invoke-Checked 'corepack' @('pnpm', 'lint')
} finally { Pop-Location }
