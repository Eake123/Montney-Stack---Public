<#
.SYNOPSIS
  Installs, starts, stops, checks or removes the "MontneyStack Runner" scheduled task (PLAN P3).

.DESCRIPTION
  The task starts montney-runner.ps1 (the supervisor that restarts `montney run` after a crash) hidden, at your
  logon, and keeps it running with no time limit. It uses the built-in Task Scheduler, so nothing is installed
  and no administrator rights are needed.

  Why at logon and not at boot: the database passwords live in YOUR Windows Credential Manager
  (`montney secrets set`). A boot-time task with no logged-on user can't read them.

    .\scripts\db\install-runner-task.ps1 -Action Install    # register (and start) the task
    .\scripts\db\install-runner-task.ps1 -Action Status     # task state, runner PID, last supervisor lines
    .\scripts\db\install-runner-task.ps1 -Action Stop       # stop the supervisor AND the runner it started
    .\scripts\db\install-runner-task.ps1 -Action Start
    .\scripts\db\install-runner-task.ps1 -Action Uninstall  # stop, then remove the task
    .\scripts\db\install-runner-task.ps1 -Action Runner     # only: the runner process runner.pid names, if valid
  -DryRun shows what Install would register and changes nothing.
#>
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Start', 'Stop', 'Status', 'Runner')]
    [string]$Action = 'Status',
    [string]$TaskName = 'MontneyStack Runner',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
# In the body, not a param() default (Windows PowerShell 5.1 leaves $PSScriptRoot empty there under -File);
# $MyInvocation is the fallback either way.
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Repo = Split-Path -Parent (Split-Path -Parent $here)
$Supervisor = Join-Path $here 'montney-runner.ps1'
if ($env:MONTNEY_HOME) { $LogDir = Join-Path $env:MONTNEY_HOME 'logs' }
elseif ($env:LOCALAPPDATA) { $LogDir = Join-Path $env:LOCALAPPDATA 'MontneyStack\logs' }
else { $LogDir = Join-Path $HOME '.local/share/montney/logs' }
$PidFile = Join-Path $LogDir 'runner.pid'

function Remove-PidFile {
    <# Non-fatal by construction. The supervisor (montney-runner.ps1) removes the same runner.pid when its child exits,
       so right after taskkill the two race: the file can vanish between Remove-Item resolving it and deleting it, which
       throws "The system cannot find the file specified" even with -ErrorAction SilentlyContinue under
       $ErrorActionPreference = 'Stop'. That aborted -Action Stop with the task left disabled (2026-11-03, twice). #>
    try { if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force -ErrorAction Stop } } catch { }
}

function Get-Runner {
    <# The runner process runner.pid names, or $null. runner.pid holds "<pid> <start time, UTC ISO 8601>"
       (written by montney-runner.ps1). After a power loss or a crash the file is stale, and its PID may belong
       to an unrelated process by now. So a PID is trusted only if that process is uv/python AND started when
       the file says (reviewer, #99). A stale file is reported and removed; its process is never touched. #>
    if (-not (Test-Path $PidFile)) { Write-Host 'runner: not running (no runner.pid)'; return $null }
    $line = "$(Get-Content $PidFile -TotalCount 1)".Trim()
    $parts = $line -split '\s+'
    $rid = 0
    $when = [datetime]::MinValue
    $proc = $null
    $why = $null
    if ($parts.Count -ne 2 -or -not [int]::TryParse($parts[0], [ref]$rid) -or
        -not [datetime]::TryParse($parts[1], [Globalization.CultureInfo]::InvariantCulture,
                                  [Globalization.DateTimeStyles]::RoundtripKind, [ref]$when)) {
        $why = "unreadable: '$line'"
    } else {
        $proc = Get-Process -Id $rid -ErrorAction SilentlyContinue
        if (-not $proc) {
            $why = "pid $rid is not running"
        } elseif ($proc.ProcessName -notmatch '^(uv|python[0-9.]*w?)$') {
            $why = "pid $rid is now '$($proc.ProcessName)', not uv/python"
        } elseif ([Math]::Abs(($proc.StartTime.ToUniversalTime() - $when.ToUniversalTime()).TotalSeconds) -gt 2) {
            $why = "pid $rid started at $($proc.StartTime.ToUniversalTime().ToString('o')), the file says $($when.ToUniversalTime().ToString('o'))"
        }
    }
    if ($why) {
        Write-Host "runner: stale runner.pid ($why): process not touched, file removed"
        Remove-PidFile
        return $null
    }
    Write-Host "runner: pid $rid ($($proc.ProcessName)), running since $($when.ToUniversalTime().ToString('o'))"
    return $proc
}

function Stop-Runner {
    $proc = Get-Runner                                     # validated, or $null: never kill a reused PID
    if (-not $proc) { return }
    & taskkill.exe /PID $proc.Id /T /F | Out-Host         # /T: uv's python child too
    Write-Host "stopped runner pid $($proc.Id)"
    Remove-PidFile
}

switch ($Action) {
    'Install' {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        $uvPath = if ($uv) { $uv.Source } else { 'uv' }  # the full path: a task's PATH can differ from your shell's
        $argLine = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Supervisor`" -Exe `"$uvPath`""
        $user = "$env:USERDOMAIN\$env:USERNAME"
        Write-Host "task:       $TaskName"
        Write-Host "runs:       powershell.exe $argLine"
        Write-Host "in:         $Repo"
        Write-Host "trigger:    at logon of $user (no admin; your Credential Manager secrets are readable)"
        Write-Host "            + every 5 min, indefinitely: relaunches the supervisor if it isn't running (one instance)"
        Write-Host "settings:   no time limit; one instance; restart the task every 1 min if it fails (up to 999 times)"
        Write-Host "logs:       $LogDir\supervisor.log and runner.log"
        if (-not $uv) { Write-Warning 'uv is not on PATH here; the task will look for it on its own PATH' }
        if ($DryRun) { Write-Host '(dry run: nothing registered)'; return }
        $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine -WorkingDirectory $Repo
        $trg = New-ScheduledTaskTrigger -AtLogOn -User $user
        # Self-healing (2026-11-02: runner AND supervisor were killed at 04:36Z, task result 0xC000013A, and
        # nothing restarted them, because "restart on failure" doesn't fire for that). A repeating trigger with
        # MultipleInstances IgnoreNew starts the task only when it isn't already running. -Action Stop disables
        # the task so this doesn't undo a deliberate stop.
        $heal = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
        $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $prn = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger @($trg, $heal) -Settings $set -Principal $prn `
            -Description 'Montney Stack: keeps `montney run` (the Postgres runner) running. scripts/db/install-runner-task.ps1' `
            -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "installed and started. Check: .\scripts\db\install-runner-task.ps1 -Action Status"
    }
    'Start' {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null          # -Action Stop disabled it
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "started $TaskName"
    }
    'Runner' { $null = Get-Runner }
    'Stop' {
        # Disable first: the every-5-min trigger would otherwise relaunch it (CLI writes need it stopped).
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Stop-Runner                                       # ending the task does not end the runner it started
        Write-Host "stopped and disabled $TaskName (-Action Start re-enables it)"
    }
    'Uninstall' {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Stop-Runner
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed $TaskName"
    }
    'Status' {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $t) { Write-Host "$TaskName is not installed"; return }
        $i = $t | Get-ScheduledTaskInfo
        Write-Host ("task: {0}; last run {1}, last result {2}" -f $t.State, $i.LastRunTime, $i.LastTaskResult)
        $null = Get-Runner
        $log = Join-Path $LogDir 'supervisor.log'
        if (Test-Path $log) { Write-Host '--- supervisor.log (last 10)'; Get-Content $log -Tail 10 }
        Write-Host 'The dashboard shows the runner heartbeat too (/api/status).'
    }
}
