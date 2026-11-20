<#
.SYNOPSIS
  Local CI on Erik's PC, the merge gate since GitHub Actions is off (L-052). Posts the `local-ci` commit status.

.DESCRIPTION
  Runs scripts/ci/local_ci.py (all the logic and the safety guards live there; read its docstring):
  a temporary worktree of the SHA, a THROWAWAY Postgres cluster on its own port (never 5432, never Erik's
  data directory), the checks of .github/workflows/ci.yml, then `local-ci` = success | failure | error on the SHA.

    .\scripts\ci\local-ci.ps1 -Pr 154          # the PR merged into current main (what lands); status on its head
    .\scripts\ci\local-ci.ps1 -Sha 0123abc...  # exactly that SHA

  One run at a time. Log: %LOCALAPPDATA%\MontneyStack\ci\logs\<run id>.log. Exit 0 success, 1 failure, 2 error.
#>
[CmdletBinding()]
param(
    [string]$Sha = '',
    [int]$Pr = 0
)

$ErrorActionPreference = 'Stop'
# In the body, not a param() default (Windows PowerShell 5.1 leaves $PSScriptRoot empty there under -File, #132).
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent (Split-Path -Parent $here)
if (($Sha -and $Pr) -or (-not $Sha -and -not $Pr)) { Write-Host 'give exactly one of -Sha or -Pr'; exit 2 }
$pyArgs = @((Join-Path $here 'local_ci.py'))
if ($Sha) { $pyArgs += @('--sha', $Sha) } else { $pyArgs += @('--pr', "$Pr") }
& uv run --project $repo python @pyArgs
exit $LASTEXITCODE
