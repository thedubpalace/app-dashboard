#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$VmrunPath = "C:\Program Files\VMware\VMware Workstation\vmrun.exe",
    [string]$VmxPath = 'D:\Users\ADMIN\Documents\Virtual Machines\home-assistant.vmx'
)

$ErrorActionPreference = "Stop"
$TaskName = "App Dashboard - Home Assistant VM Autostart"

if (-not (Test-Path -LiteralPath $VmrunPath -PathType Leaf)) {
    throw "vmrun.exe not found: $VmrunPath"
}
if (-not (Test-Path -LiteralPath $VmxPath -PathType Leaf)) {
    throw "VMX file not found: $VmxPath"
}

$TaskAction = '"' + $VmrunPath + '" start "' + $VmxPath + '" nogui'
schtasks.exe /Create /TN $TaskName /TR $TaskAction /SC ONSTART /DELAY 0001:00 /RU SYSTEM /RL HIGHEST /F
if ($LASTEXITCODE -ne 0) {
    throw "Task Scheduler could not create $TaskName (exit code $LASTEXITCODE)."
}

schtasks.exe /Query /TN $TaskName /V /FO LIST
