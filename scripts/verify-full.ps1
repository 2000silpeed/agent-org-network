Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Invoke-Checked.ps1')
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

Invoke-Checked 'uv' @('run', 'pytest', '-q')
Invoke-Checked 'uv' @('run', 'pyright')
Invoke-Checked 'uv' @('run', 'ruff', 'check', '.')
Push-Location (Join-Path $root 'frontend')
try {
    Invoke-Checked 'corepack' @('pnpm', 'test')
    Invoke-Checked 'corepack' @('pnpm', 'exec', 'tsc', '--noEmit')
    Invoke-Checked 'corepack' @('pnpm', 'lint')
    Invoke-Checked 'corepack' @('pnpm', 'build')
} finally { Pop-Location }
