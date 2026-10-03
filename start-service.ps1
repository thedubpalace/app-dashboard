#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot

Get-Content -LiteralPath (Join-Path $repo '.env.runtime') | ForEach-Object {
  if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
  }
}

Set-Location -LiteralPath $repo
$listenHost = if ($env:VEDA_DASHBOARD_HOST) { $env:VEDA_DASHBOARD_HOST } else { '0.0.0.0' }
$listenPort = if ($env:VEDA_DASHBOARD_PORT) { $env:VEDA_DASHBOARD_PORT } else { '8765' }
& python -m uvicorn main:app --host $listenHost --port $listenPort
exit $LASTEXITCODE
