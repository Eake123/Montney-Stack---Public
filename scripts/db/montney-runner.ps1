<#
.SYNOPSIS
  Keeps the Postgres runner (`uv run montney run`) alive: restarts it after a crash, with backoff.

.DESCRIPTION
  The supervisor that the "MontneyStack Runner" scheduled task starts at logon (install-runner-task.ps1).
  Exit-code policy for the runner:
    0  clean stop (Ctrl+C)                        -> the supervisor stops too, exit 0
    78 configuration error (runner.EXIT_CONFIG) -> no restart (it would fail the same way), exit 78
    *  anything else, INCLUDING 2 (uv exits 2 when it can't spawn the command, e.g. mid-sync; argparse too)
       a crash, WriterBusy, the DB down
                                                  -> restart after a backoff: MinBackoff, doubling up to MaxBackoff;
                                                     back to MinBackoff once a run lasted HealthySeconds.
  Logs (all under the data dir, never the repo): <LogDir>\supervisor.log (starts, exits, waits, and the tail of a
  crashed run's stderr; rotated at LogMaxBytes, 2 files at most).
  The runner's own log stays <home>\logs\runner.log. The running child's PID and start time are in <LogDir>\runner.pid so
  `install-runner-task.ps1 -Action Stop` can end it.
  Works in Windows PowerShell 5.1 and PowerShell 7 (tests run it under pwsh on Linux CI too).
#>
[CmdletBinding()]
param(
    [string]$Repo = '',              # default: the repo this script is in (resolved below, NOT here; see there)
    [string]$Exe = 'uv',
    [string]$ExeArgs = 'run montney run',
    [string]$LogDir = '',
    [double]$MinBackoff = 10,
    [double]$MaxBackoff = 300,
    [double]$HealthySeconds = 600,
    [int]$MaxRestarts = 0,           # 0 = unlimited; tests set a cap so a broken policy can't loop forever
    [long]$LogMaxBytes = 5MB         # supervisor.log rotates to supervisor.log.1 past this (2 files at most)
)

$ErrorActionPreference = 'Stop'

# The repo: resolved in the body. Windows PowerShell 5.1 leaves $PSScriptRoot EMPTY inside param() defaults
# under -File (the scheduled task's command line), so a default there failed at once with "Cannot bind
# argument to parameter 'Path'" (Erik's install, 2026-11-01). $MyInvocation is the fallback.
if (-not $Repo) {
    $here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Repo = Split-Path -Parent (Split-Path -Parent $here)                     # scripts\db -> the repo
}

# Exit codes that end supervision instead of restarting (see the policy above).
$NoRestartCodes = @(0, 78)

if (-not $LogDir) {
    if ($env:MONTNEY_HOME) { $LogDir = Join-Path $env:MONTNEY_HOME 'logs' }
    else { $LogDir = Join-Path $env:LOCALAPPDATA 'MontneyStack\logs' }
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir 'supervisor.log'
$PidFile = Join-Path $LogDir 'runner.pid'
$OutFile = Join-Path $LogDir 'runner-console.log'
$ErrFile = Join-Path $LogDir 'runner-stderr.log'

function Write-Log([string]$msg) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt $LogMaxBytes) {
        Move-Item -Force $Log "$Log.1"
    }
    $line = '{0} {1}' -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $msg
    Add-Content -Path $Log -Value $line -Encoding UTF8
    Write-Host $line
}

# The runner refuses to start without it (exit 78); the task exists only for the Postgres runner.
if (-not $env:MONTNEY_DB) { $env:MONTNEY_DB = 'postgres' }
# The runner's log is runner.log (5 x 5 MB, rotated by the runner). Its console copy would go to
# runner-console.log/runner-stderr.log, which are only replaced at the next start: keep them for crashes only.
if (-not $env:MONTNEY_LOG_CONSOLE) { $env:MONTNEY_LOG_CONSOLE = '0' }

$backoff = $MinBackoff
$restarts = 0
Write-Log "supervisor start: pid $PID, '$Exe $ExeArgs' in $Repo"
while ($true) {
    $started = Get-Date
    $sp = @{ FilePath = $Exe; WorkingDirectory = $Repo; PassThru = $true; NoNewWindow = $true
             RedirectStandardOutput = $OutFile; RedirectStandardError = $ErrFile }
    if ($ExeArgs) { $sp.ArgumentList = $ExeArgs }
    try {
        $p = Start-Process @sp
    } catch {
        Write-Log "could not start '$Exe': $($_.Exception.Message)"
        $code = 127
        $p = $null
    }
    if ($p) {
        $null = $p.Handle                 # without this, ExitCode can come back empty (PowerShell 5.1)
        # PID + start time (UTC): install-runner-task.ps1 trusts only a process matching BOTH, since after a
        # power loss a stale file's PID may belong to something else (reviewer, #99).
        Set-Content -Path $PidFile -Value ('{0} {1}' -f $p.Id, $p.StartTime.ToUniversalTime().ToString('o')) -Encoding ASCII
        Write-Log "runner started: pid $($p.Id)"
        $p.WaitForExit()
        $code = $p.ExitCode
        # the same race from this side (install-runner-task.ps1 -Action Stop removes it too): never fatal
        try { if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force -ErrorAction Stop } } catch { }
    }
    $ran = ((Get-Date) - $started).TotalSeconds
    Write-Log ("runner exited: code {0} after {1:N1} s" -f $code, $ran)
    if ($NoRestartCodes -contains $code) {
        if ($code -eq 78) { Write-Log 'configuration error: not restarting (see runner-stderr.log / runner-console.log)' }
        Write-Log "supervisor stop: exit $code"
        exit $code
    }
    if ((Test-Path $ErrFile) -and (Get-Item $ErrFile).Length -gt 0) {
        foreach ($l in (Get-Content $ErrFile -Tail 20)) { Write-Log "  stderr| $l" }
    }
    if ($MaxRestarts -gt 0 -and $restarts -ge $MaxRestarts) {
        Write-Log "supervisor stop: $restarts restarts reached the cap (MaxRestarts)"
        exit 3
    }
    if ($ran -ge $HealthySeconds) { $backoff = $MinBackoff }     # a long healthy run resets the backoff
    Write-Log ("restarting in {0:N1} s" -f $backoff)
    Start-Sleep -Milliseconds ([int]($backoff * 1000))
    $restarts++
    $backoff = [Math]::Min($backoff * 2, $MaxBackoff)
}
