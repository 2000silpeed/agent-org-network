Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Invoke-Checked.ps1')

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
$port = if ($args.Count -ge 1) { [int]$args[0] } else { 8000 }
$hostName = if ($args.Count -ge 2) { [string]$args[1] } else { '127.0.0.1' }
if ($port -lt 1 -or $port -gt 65535) { throw 'PORT must be between 1 and 65535' }
if ([string]::IsNullOrWhiteSpace($hostName)) { throw 'HOST must not be blank' }

Write-Host "[central] http://$hostName`:$port"
Invoke-Checked 'uv' @('run', 'uvicorn', 'agent_org_network.server:central_app', '--host', $hostName, '--port', "$port")
