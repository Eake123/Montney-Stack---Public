<#
.SYNOPSIS
  One click for Erik: apply (or revert) the Postgres memory/IO settings of docs/db/TUNING.md.

.DESCRIPTION
  Asks once for administrator rights (Windows' own prompt; the service restart needs them), then:
    1. stops the runner (install-runner-task.ps1 -Action Stop), so nothing writes during the restart;
    2. `uv run python -m montney.db.tune apply` (or `revert`): ALTER SYSTEM, never a postgresql.conf edit;
    3. restarts the service postgresql-x64-18 and waits until it answers;
    4. `... tune verify` (apply only): every proposed value must be live, or it says FAIL;
    5. starts the runner again (even if a step failed), and waits for Enter so the window can be read.
  No password is in this file: the superuser password is read from Windows Credential Manager
  (POSTGRES_SUPERUSER_PASSWORD, stored at install) by montney itself.

    .\scripts\db\apply-tuning.ps1                  # apply
    .\scripts\db\apply-tuning.ps1 -Revert          # back to the previous values
    .\scripts\db\apply-tuning.ps1 -CreateShortcut  # put "Montney - apply DB tuning" on the desktop
#>
[CmdletBinding()]
param(
    [switch]$Revert,
    [switch]$CreateShortcut,
    [switch]$Elevated,
    [string]$Uv = '',                # uv's full path, found BEFORE elevating (the elevated PATH can differ)
    [string]$Service = 'postgresql-x64-18'
)

$ErrorActionPreference = 'Stop'
# Resolved in the body: Windows PowerShell 5.1 leaves $PSScriptRoot empty in param() defaults under -File (#132).
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Repo = Split-Path -Parent (Split-Path -Parent $here)
$self = Join-Path $here 'apply-tuning.ps1'

if ($CreateShortcut) {
    $desk = [Environment]::GetFolderPath('Desktop')
    $lnk = Join-Path $desk 'Montney - apply DB tuning.lnk'
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = 'powershell.exe'
    $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$self`""
    $s.WorkingDirectory = $Repo
    $s.Description = 'Montney Stack: apply the Postgres memory settings (docs/db/TUNING.md)'
    $s.Save()
    Write-Host "shortcut: $lnk"
    return
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $Uv) {
    $u = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $u) { Write-Host "uv is not on PATH: open a new terminal (or reinstall uv), then run this again." -ForegroundColor Red; exit 1 }
    $Uv = $u.Source
}
if (-not $isAdmin) {
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$self`" -Elevated -Uv `"$Uv`""
    if ($Revert) { $argList += ' -Revert' }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -WorkingDirectory $Repo
    return
}

$what = if ($Revert) { 'revert' } else { 'apply' }
$failed = $false
Set-Location $Repo
try {
    Write-Host "== 1/5 stopping the runner"
    & (Join-Path $here 'install-runner-task.ps1') -Action Stop
    Write-Host "== 2/5 montney.db.tune $what"
    & $Uv run python -m montney.db.tune $what
    if ($LASTEXITCODE -ne 0) { throw "tune $what failed ($LASTEXITCODE)" }
    Write-Host "== 3/5 restarting $Service"
    Restart-Service -Name $Service
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 1
        $ok = (Test-NetConnection -ComputerName localhost -Port 5432 -WarningAction SilentlyContinue).TcpTestSucceeded
    } until ($ok -or (Get-Date) -gt $deadline)
    if (-not $ok) { throw "$Service did not answer on port 5432 within 60 s" }
    if (-not $Revert) {
        Write-Host "== 4/5 verify"
        & $Uv run python -m montney.db.tune verify
        if ($LASTEXITCODE -ne 0) { $failed = $true; Write-Host "VERIFY FAILED: see above" -ForegroundColor Red }
    }
} catch {
    $failed = $true
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Write-Host "== 5/5 starting the runner"
    & (Join-Path $here 'install-runner-task.ps1') -Action Start
}
if ($failed) { Write-Host "Something failed. Nothing is lost: re-run, or run with -Revert. Tell the agent what it says." -ForegroundColor Red }
else { Write-Host "Done: $what complete." -ForegroundColor Green }
if ($Elevated) { Read-Host 'Press Enter to close' | Out-Null }
