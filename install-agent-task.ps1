#Requires -Version 5.1
<#
.SYNOPSIS
    Run the App Dashboard host agent at logon and restart it if it exits.

.DESCRIPTION
    Registers a Task Scheduler task for the current user. A scheduled task is
    used instead of a Windows service on purpose: the agent opens terminals and
    starts dev servers, which must happen in the user's interactive session,
    not in a service's session 0. Run again to update the task; pass -Remove
    to delete it.
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$TaskName = 'App Dashboard Agent'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed $TaskName"
    exit 0
}

$launcher = Join-Path $PSScriptRoot 'start-agent.ps1'
$powershell = (Get-Command powershell.exe).Source
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction -Execute $powershell -WorkingDirectory $PSScriptRoot `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
# No time limit, one instance, and restart every minute if the agent exits.
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Host agent for App Dashboard (agent.py on 127.0.0.1:8766).' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Registered and started $TaskName for $user"
