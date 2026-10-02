#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot

Get-Content -LiteralPath (Join-Path $repo '.env.runtime') | ForEach-Object {
  if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
  }
}

Set-Location -LiteralPath $repo
& python -m uvicorn main:app --host 127.0.0.1 --port 8765
exit $LASTEXITCODE
