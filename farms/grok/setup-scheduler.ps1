#Requires -Version 5.1
<#
.SYNOPSIS
    Register the GrokTokenRefresh Windows Task Scheduler task.
    Run once as the current user (no elevation required for "run when logged on").
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── Config ───────────────────────────────────────────────────────────────────
$TaskName   = 'GrokTokenRefresh'
$ScriptDir  = $PSScriptRoot
$Ps1Path    = Join-Path $ScriptDir 'refresh-grok.ps1'

$ActionExe  = 'powershell.exe'
$ActionArgs = "-NonInteractive -ExecutionPolicy Bypass -File `"$Ps1Path`""

# ── Remove existing task if present ──────────────────────────────────────────
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[setup] Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# ── Action ───────────────────────────────────────────────────────────────────
$action = New-ScheduledTaskAction `
    -Execute $ActionExe `
    -Argument $ActionArgs `
    -WorkingDirectory $ScriptDir

# ── Trigger: every 4 hours (access token ~6h; refresh early) ─────────────────
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Hours 4) -Once -At (Get-Date)

# ── Settings ──────────────────────────────────────────────────────────────────
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false `
    -MultipleInstances IgnoreNew `
    -Hidden:$false

# StartWhenAvailable = true covers the "RunIfMissed" requirement:
# if the machine was off at the scheduled time, the task runs ASAP on next login.

# ── Principal: run only when user is logged on (no password needed) ───────────
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

# ── Register ──────────────────────────────────────────────────────────────────
$task = Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Description 'Refreshes grok-cli OAuth tokens in the 9router SQLite DB every 4 hours (HTTP only; revoked tokens need grok-reauth). Runs ASAP if a run was missed while the machine was off.'

Write-Host ''
Write-Host '==========================================='
Write-Host " Task registered: $($task.TaskName)"
Write-Host " Script  : $Ps1Path"
Write-Host " Schedule: every 4 hours (StartWhenAvailable=true)"
Write-Host " Limit   : 10 minutes execution timeout"
Write-Host " Logon   : runs only when user is logged on"
Write-Host '==========================================='
Write-Host ''
Write-Host '[setup] Done. To test immediately:'
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ''
Write-Host '[setup] To view task status:'
Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'"
