#Requires -Version 5.1
<#
.SYNOPSIS
    Wrapper for refresh-grok.js - runs token refresh, logs output, rotates log.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Paths
$ScriptDir  = $PSScriptRoot
$NodeScript = Join-Path $ScriptDir 'refresh-grok.js'
$LogDir     = Join-Path $ScriptDir 'logs'
$LogFile    = Join-Path $LogDir 'refresh.log'

$MaxLogBytes = 1MB
$KeepLines   = 500

# Ensure logs/ dir exists
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

# Log rotation
function Rotate-Log {
    if (-not (Test-Path -LiteralPath $LogFile)) { return }
    $size = (Get-Item -LiteralPath $LogFile).Length
    if ($size -lt $MaxLogBytes) { return }

    Write-Host "[rotate] Log exceeds 1MB - keeping last $KeepLines lines."
    $lines = Get-Content -LiteralPath $LogFile -Encoding UTF8
    $kept  = if ($lines.Count -gt $KeepLines) { $lines[-$KeepLines..-1] } else { $lines }
    $kept | Set-Content -LiteralPath $LogFile -Encoding UTF8
}

# Helper: write to console AND log
function Write-Log {
    param([string]$Message)
    $stamped = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Write-Host $stamped
    Add-Content -LiteralPath $LogFile -Value $stamped -Encoding UTF8
}

# Main
Rotate-Log

Write-Log '========================================'
Write-Log 'GrokTokenRefresh - START'
Write-Log "Script : $NodeScript"

if (-not (Test-Path -LiteralPath $NodeScript)) {
    Write-Log "ERROR: refresh-grok.js not found at $NodeScript"
    exit 1
}

# Run node, capture stdout+stderr, stream to console+log
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName               = 'node'
$psi.Arguments              = "`"$NodeScript`""
$psi.UseShellExecute        = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError  = $true
$psi.CreateNoWindow         = $true
$psi.WorkingDirectory       = $ScriptDir

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi

[void]$proc.Start()

# Read stdout line by line
while (-not $proc.StandardOutput.EndOfStream) {
    $line = $proc.StandardOutput.ReadLine()
    if ($null -ne $line) {
        Write-Host $line
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    }
}

# Drain stderr
$stderr = $proc.StandardError.ReadToEnd()
if ($stderr) {
    Write-Host $stderr
    Add-Content -LiteralPath $LogFile -Value $stderr -Encoding UTF8
}

$proc.WaitForExit()
$exitCode = $proc.ExitCode

Write-Log "GrokTokenRefresh - END (exit $exitCode)"
Write-Log '========================================'

exit $exitCode
