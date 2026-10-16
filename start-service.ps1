#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot

Get-Content -LiteralPath (Join-Path $repo '.env.runtime') | ForEach-Object {
  if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
  }
}

Set-Location -LiteralPath $repo
$listenHost = if ($env:APP_DASHBOARD_HOST) { $env:APP_DASHBOARD_HOST } elseif ($env:VEDA_DASHBOARD_HOST) { $env:VEDA_DASHBOARD_HOST } else { '0.0.0.0' }
$listenPort = if ($env:APP_DASHBOARD_PORT) { $env:APP_DASHBOARD_PORT } elseif ($env:VEDA_DASHBOARD_PORT) { $env:VEDA_DASHBOARD_PORT } else { '8765' }
$venvPython = Join-Path $repo '.venv\Scripts\python.exe'
$pythonCommand = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
& $pythonCommand -m uvicorn main:app --host $listenHost --port $listenPort
exit $LASTEXITCODE
