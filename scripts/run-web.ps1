Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Invoke-Checked.ps1')

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
$port = if ($args.Count -ge 1) { [int]$args[0] } else { 8011 }
if ($port -lt 1 -or $port -gt 65535) { throw 'PORT must be between 1 and 65535' }

$envFile = Join-Path $root '.env'
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)\s*$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        }
    }
}
if ([string]::IsNullOrWhiteSpace($env:OPERATOR_SESSION_SECRET)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $env:OPERATOR_SESSION_SECRET = ([Convert]::ToHexString($bytes)).ToLowerInvariant()
    Add-Content -LiteralPath $envFile -Value "`n# local development session key (do not commit)`nOPERATOR_SESSION_SECRET=$env:OPERATOR_SESSION_SECRET"
}

Write-Host "[web] http://127.0.0.1:$port"
Invoke-Checked 'uv' @('run', 'uvicorn', 'agent_org_network.web:app', '--host', '127.0.0.1', '--port', "$port")
