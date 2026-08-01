Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Invoke-Checked.ps1')

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
Write-Host '[mcp] ask_org stdio server (Ctrl+C to stop)'
Invoke-Checked 'uv' @('run', 'python', '-m', 'agent_org_network.mcp_server')
