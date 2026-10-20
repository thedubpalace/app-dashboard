#Requires -RunAsAdministrator
#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)]
    [string]$Account
)
$ErrorActionPreference = 'Stop'
# Grant only query-status, start, and stop on this one service.
$sid = ([System.Security.Principal.NTAccount]$Account).Translate([System.Security.Principal.SecurityIdentifier])
$output = & sc.exe sdshow SunshineService
if ($LASTEXITCODE -ne 0) { throw 'Cannot read Sunshine Service permissions.' }
$sddl = ($output | Where-Object { $_ -match '^[DOGS]:' }) -join ''
$descriptor = [System.Security.AccessControl.CommonSecurityDescriptor]::new($false, $false, $sddl)
$descriptor.DiscretionaryAcl.AddAccess(
    [System.Security.AccessControl.AccessControlType]::Allow,
    $sid, 0x34,
    [System.Security.AccessControl.InheritanceFlags]::None,
    [System.Security.AccessControl.PropagationFlags]::None
)
$updated = $descriptor.GetSddlForm([System.Security.AccessControl.AccessControlSections]::All)
& sc.exe sdset SunshineService $updated
if ($LASTEXITCODE -ne 0) { throw 'Cannot update Sunshine Service permissions.' }
Write-Output "Sunshine restart enabled for $Account."
