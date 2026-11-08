#Requires -Version 5.1
# Starts the App Dashboard host agent (agent.py). See README "Host agent".
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot

$runtimeEnv = Join-Path $repo '.env.runtime'
if (Test-Path -LiteralPath $runtimeEnv -PathType Leaf) {
  Get-Content -LiteralPath $runtimeEnv | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
      $envName = $Matches[1]
      $envValue = $Matches[2].Trim()
      if ($envValue.Length -ge 2 -and (($envValue.StartsWith('"') -and $envValue.EndsWith('"')) -or ($envValue.StartsWith("'") -and $envValue.EndsWith("'")))) {
        $envValue = $envValue.Substring(1, $envValue.Length - 2)
      }
      [Environment]::SetEnvironmentVariable($envName, $envValue, 'Process')
    }
  }
}

Set-Location -LiteralPath $repo
$listenHost = if ($env:APP_AGENT_HOST) { $env:APP_AGENT_HOST } else { '127.0.0.1' }
$listenPort = if ($env:APP_AGENT_PORT) { $env:APP_AGENT_PORT } else { '8766' }
$portNumber = 0
if (-not [int]::TryParse($listenPort, [ref]$portNumber) -or $portNumber -lt 1 -or $portNumber -gt 65535) {
  throw 'APP_AGENT_PORT must be a number from 1 to 65535.'
}
$probeHost = if ($listenHost -eq '0.0.0.0') { '127.0.0.1' } elseif ($listenHost -eq '::') { '[::1]' } elseif ($listenHost.Contains(':') -and -not $listenHost.StartsWith('[')) { "[$listenHost]" } else { $listenHost }
$healthUrl = "http://${probeHost}:$portNumber/healthz"
try {
  $existing = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 1
  if ($existing.StatusCode -eq 200 -and $existing.Content -match '"ok"\s*:\s*true') {
    Write-Output "App Dashboard agent is already running at $healthUrl"
    exit 0
  }
} catch {
  # No agent is listening yet; start it below.
}
$venvPython = Join-Path $repo '.venv\Scripts\python.exe'
$pythonCommand = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
& $pythonCommand -c 'import uvicorn, fastapi, psutil'
if ($LASTEXITCODE -ne 0) {
  throw 'The Python environment is unavailable or missing dependencies. Create .venv and install requirements.txt.'
}
& $pythonCommand -m uvicorn agent:app --host $listenHost --port $portNumber
exit $LASTEXITCODE
