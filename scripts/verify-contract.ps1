Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Invoke-Checked.ps1')
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

# Docker is intentionally not invoked. The Dockerfile is an optional packaging artifact.
Invoke-Checked 'corepack' @('pnpm', '--dir', 'frontend', 'build')
Invoke-Checked 'uv' @('run', 'pytest', '-q', 'tests/test_a2a_sdk_adapter.py', 'tests/test_central_bootstrap_device.py', 'tests/test_central_bootstrap_sqlite.py', 'tests/test_central_browser_auth.py', 'tests/test_central_composition.py', 'tests/test_central_inbox_approval.py', 'tests/test_central_inbox_conflict.py', 'tests/test_central_inbox_review.py', 'tests/test_central_operational_evidence.py', 'tests/test_central_policy_revision.py', 'tests/test_central_api.py', 'tests/test_central_next_artifact_contract.py', 'tests/test_central_web_runtime.py', 'tests/test_central_question_request_sqlite.py', 'tests/test_developer_api_boundary.py', 'tests/test_installation_contracts.py', 'tests/test_installation_entrypoints.py', 'tests/test_support_contract.py')
