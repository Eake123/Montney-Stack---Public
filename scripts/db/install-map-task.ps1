<#
.SYNOPSIS
  Installs, starts, stops, checks or removes the "MontneyStack Map" scheduled task (L-068): the map server on
  127.0.0.1:8050, always on, so Erik can open it from his phone over Tailscale.

.DESCRIPTION
  Mirrors install-runner-task.ps1. The task runs montney-map.ps1 hidden: at logon, and every 5 minutes
  (one instance), so a crashed or killed server comes back by itself. Nothing is installed; no admin rights.
  It never touches the runner or its task.

    .\scripts\db\install-map-task.ps1 -Action Install    # register + start; the "Montney Map" shortcut just opens the map
    .\scripts\db\install-map-task.ps1 -Action Status
    .\scripts\db\install-map-task.ps1 -Action Stop       # stop + DISABLE (the 5-min trigger would relaunch it)
    .\scripts\db\install-map-task.ps1 -Action Start      # enable + start
    .\scripts\db\install-map-task.ps1 -Action Uninstall  # stop, remove, restore the shortcut's own server
  -DryRun shows what Install would register and changes nothing.
#>
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Start', 'Stop', 'Status')]
    [string]$Action = 'Status',
    [string]$TaskName = 'MontneyStack Map',
    [int]$Port = 8050,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
# In the body: Windows PowerShell 5.1 leaves $PSScriptRoot empty in param() defaults under -File (#132).
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Repo = Split-Path -Parent (Split-Path -Parent $here)
$Wrapper = Join-Path $here 'montney-map.ps1'
if ($env:MONTNEY_HOME) { $LogDir = Join-Path $env:MONTNEY_HOME 'logs' }
elseif ($env:LOCALAPPDATA) { $LogDir = Join-Path $env:LOCALAPPDATA 'MontneyStack\logs' }
else { $LogDir = Join-Path $HOME '.local/share/montney/logs' }
$Url = "http://127.0.0.1:$Port"
$ShortcutName = 'Montney Map.lnk'

function Set-MapShortcut([string]$mode, [string]$uvPath) {
    $desk = [Environment]::GetFolderPath('Desktop')
    $lnk = Join-Path $desk $ShortcutName
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    if ($mode -eq 'open') {              # the task serves; the shortcut only opens it
        $s.TargetPath = 'explorer.exe'
        $s.Arguments = $Url
    } else {                             # back to its own server in a console (the pre-L-068 shortcut)
        $s.TargetPath = 'C:\Windows\System32\cmd.exe'
        $s.Arguments = "/k `"title Montney Map (close this window to stop the map) && cd /d $Repo && $uvPath run montney serve`""
    }
    $s.WorkingDirectory = $Repo
    $s.Save()
    Write-Host "shortcut: $lnk -> $($s.TargetPath) $($s.Arguments)"
}

function Stop-MapServer {
    # Only OUR server: the process listening on 127.0.0.1:$Port whose command line is this task's exact
    # `montney serve ... --port $Port --no-browser --no-schedule`. Never by name; never the runner.
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
        $cmd = "$($p.CommandLine)"
        if ($cmd -match 'montney' -and $cmd -match '\bserve\b' -and $cmd -match "--port $Port\b" -and $cmd -match '--no-schedule') {
            & taskkill.exe /PID $c.OwningProcess /T /F | Out-Host
            Write-Host "stopped map server pid $($c.OwningProcess)"
        } else {
            Write-Host "port $Port is held by pid $($c.OwningProcess), not this task's server: not touched"
        }
    }
}

switch ($Action) {
    'Install' {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        $uvPath = if ($uv) { $uv.Source } else { 'uv' }
        $argLine = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Wrapper`" -Exe `"$uvPath`" -Port $Port"
        $user = "$env:USERDOMAIN\$env:USERNAME"
        Write-Host "task:       $TaskName"
        Write-Host "runs:       powershell.exe $argLine"
        Write-Host "serves:     $Url (127.0.0.1 only; never 0.0.0.0), read-only (--no-schedule)"
        Write-Host "trigger:    at logon of $user, + every 5 min (one instance): relaunches the server if it isn't running"
        Write-Host "logs:       $LogDir\map.log and map-console.log"
        Write-Host "shortcut:   '$ShortcutName' on the desktop will just open $Url"
        if ($DryRun) { Write-Host '(dry run: nothing registered)'; return }
        $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine -WorkingDirectory $Repo
        $trg = New-ScheduledTaskTrigger -AtLogOn -User $user
        $heal = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
        $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
            -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $prn = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger @($trg, $heal) -Settings $set -Principal $prn `
            -Description 'Montney Stack: the map server on 127.0.0.1:8050 (L-068). scripts/db/install-map-task.ps1' `
            -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Set-MapShortcut 'open' $uvPath
        Write-Host "installed and started. Check: .\scripts\db\install-map-task.ps1 -Action Status"
    }
    'Start' {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "started $TaskName"
    }
    'Stop' {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Stop-MapServer
        Write-Host "stopped and disabled $TaskName (-Action Start re-enables it)"
    }
    'Uninstall' {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Stop-MapServer
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        Set-MapShortcut 'serve' $(if ($uv) { $uv.Source } else { 'uv' })
        Write-Host "removed $TaskName"
    }
    'Status' {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $t) { Write-Host "$TaskName is not installed" } else {
            $i = $t | Get-ScheduledTaskInfo
            Write-Host ("task: {0}; last run {1}, last result {2}" -f $t.State, $i.LastRunTime, $i.LastTaskResult)
        }
        $up = $false
        try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "$Url/api/status"; $up = $r.StatusCode -eq 200 } catch { }
        Write-Host "map at ${Url}: $(if ($up) { 'UP' } else { 'not answering' })"
        $log = Join-Path $LogDir 'map.log'
        if (Test-Path $log) { Write-Host '--- map.log (last 5)'; Get-Content $log -Tail 5 }
    }
}
