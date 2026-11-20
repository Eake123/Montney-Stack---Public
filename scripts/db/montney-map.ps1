<#
.SYNOPSIS
  Serves the map (`uv run montney serve`) on 127.0.0.1:8050 for the "MontneyStack Map" task (L-068).

.DESCRIPTION
  Started hidden by the task (install-map-task.ps1): at logon, and every 5 minutes when it isn't running.
  - Something already listening on the port (the desktop shortcut's server, or this task's own): log it, exit 0.
    There's never a second server and never a fight; the task tries again in 5 minutes.
  - Otherwise: `montney serve --host 127.0.0.1 --port 8050 --no-browser --no-schedule`, bound to localhost ONLY
    (Tailscale serves it to Erik's phone; never 0.0.0.0). It never fetches: the runner is the only writer
    (MONTNEY_DB=postgres is set when unset, so the server reads Postgres as the reader role).
  Logs (data dir, never the repo): <LogDir>\map.log (starts/exits; rotated at 5 MB) and map-console.log (the
  latest run's output, replaced at each start).
#>
[CmdletBinding()]
param(
    [string]$Exe = 'uv',
    [int]$Port = 8050,
    [string]$LogDir = ''
)

$ErrorActionPreference = 'Stop'
# In the body: Windows PowerShell 5.1 leaves $PSScriptRoot empty in param() defaults under -File (#132).
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Repo = Split-Path -Parent (Split-Path -Parent $here)
if (-not $LogDir) {
    if ($env:MONTNEY_HOME) { $LogDir = Join-Path $env:MONTNEY_HOME 'logs' }
    else { $LogDir = Join-Path $env:LOCALAPPDATA 'MontneyStack\logs' }
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir 'map.log'
$Out = Join-Path $LogDir 'map-console.log'

function Write-Log([string]$msg) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 5MB) { Move-Item -Force $Log "$Log.1" }
    Add-Content -Path $Log -Value ('{0} {1}' -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $msg) -Encoding UTF8
}

function Test-PortListening([int]$p) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { $c.Connect('127.0.0.1', $p); return $true } catch { return $false } finally { $c.Close() }
}

if (Test-PortListening $Port) {
    Write-Log "127.0.0.1:$Port is already serving: nothing to do"
    exit 0
}
if (-not $env:MONTNEY_DB) { $env:MONTNEY_DB = 'postgres' }
$serveArgs = @('run', '--project', $Repo, 'montney', 'serve', '--host', '127.0.0.1', '--port', "$Port", '--no-browser', '--no-schedule')
Write-Log "serving on 127.0.0.1:$Port ($Exe $($serveArgs -join ' '))"
& $Exe @serveArgs *> $Out
Write-Log "server exited: code $LASTEXITCODE (the task relaunches it within 5 minutes)"
exit $LASTEXITCODE
