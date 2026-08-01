Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Invoke-Checked.ps1')

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
if ($args.Count -lt 1 -or [string]::IsNullOrWhiteSpace([string]$args[0])) {
    throw 'Usage: .\scripts\run-worker.ps1 <OWNER_ID> [ROLE] [PORT] [CENTRAL_HOST]'
}
$owner = [string]$args[0]
$role = if ($args.Count -ge 2) { [string]$args[1] } else { 'primary' }
$port = if ($args.Count -ge 3) { [int]$args[2] } else { 8000 }
$centralHost = if ($args.Count -ge 4) { [string]$args[3] } else { '127.0.0.1' }
if ($role -notin @('primary', 'backup')) { throw 'ROLE must be primary or backup' }
if ($port -lt 1 -or $port -gt 65535) { throw 'PORT must be between 1 and 65535' }
$url = "ws://$centralHost`:$port/worker"
$workerArgs = @('-u', '-m', 'agent_org_network.worker', '--owner', $owner, '--role', $role, '--url', $url)
if (-not [string]::IsNullOrWhiteSpace($env:TOKEN)) { $workerArgs += @('--token', $env:TOKEN) }

Write-Host "[worker] owner=$owner role=$role -> $url"
Invoke-Checked 'uv' (@('run', 'python') + $workerArgs)
