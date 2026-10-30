#!/usr/bin/env pwsh
<#
.SYNOPSIS
    The dashboard's verify harness (FR-43, decision 30). Canonical; verify-dashboard.sh is parity.

.DESCRIPTION
    Seven stages, always in this order, cheapest failure first. The script stops at the first
    non-zero stage and never runs the later ones. It is its own command and never a seventh stage
    of scripts/verify.ps1, which stays at six.

    `npm ci` and `npx playwright install chromium` fill the caches once, like `uv sync`; every
    stage after runs offline. The build stage writes dashboard/out/ (output: 'export'); the export
    stage stages it into src/harnessed/dashboard_static/ and writes the staleness stamp
    (FR-63, decision 44). The e2e stage starts its own seeded servers, never the operator's.

.EXAMPLE
    pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/verify-dashboard.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# No analytics, ever (the dashboard stack table).
$env:NEXT_TELEMETRY_DISABLED = '1'

# The full harness scores the whole surface - lib/**, components/** and app/** - whether or not a
# test imported the file, against the same floor a narrow per-ticket verify uses (decision 30,
# ticket 118). Unset, the scope is whatever the run imported.
$env:COVERAGE_SCOPE = 'all'

# The quiet-tree lines (FR-159, decision 71). Not stages: HEAD and a sha256 of the porcelain
# status are printed before stage 1 and after the last, and a run whose tree moved between them
# is void and exits 3 whatever the stages did.
$VoidExit = 3

function Get-TreeState {
    $head = ((& git rev-parse HEAD) | Out-String).Trim()
    $lines = @(& git status --porcelain=v1 --untracked-files=all)
    $text = if ($lines.Count -gt 0) { ($lines -join "`n") + "`n" } else { '' }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = ($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($text)) |
        ForEach-Object { $_.ToString('x2') }) -join ''
    return @{ Head = $head; Tree = $hash }
}

$Before = Get-TreeState
Write-Host "verify-dashboard: tree before: HEAD $($Before.Head) status-sha256 $($Before.Tree)"

# Return when the tree is as it was; exit 3 when it moved, in which case the run is void.
function Assert-QuietTree {
    $after = Get-TreeState
    Write-Host "verify-dashboard: tree after:  HEAD $($after.Head) status-sha256 $($after.Tree)"
    if ($after.Head -ne $Before.Head -or $after.Tree -ne $Before.Tree) {
        Write-Host 'void: the tree changed under the run' -ForegroundColor Red
        Write-Host "  before: HEAD $($Before.Head) status-sha256 $($Before.Tree)"
        Write-Host "  after:  HEAD $($after.Head) status-sha256 $($after.Tree)"
        exit $VoidExit
    }
}

$Stage = 0

function Invoke-Stage {
    param(
        [Parameter(Mandatory)] [string]   $Name,
        [Parameter(Mandatory)] [string[]] $Command
    )
    $script:Stage++
    Write-Host ''
    Write-Host "==> [$script:Stage/7] $Name" -ForegroundColor Cyan
    Write-Host "    $($Command -join ' ')" -ForegroundColor DarkGray
    & $Command[0] @($Command[1..($Command.Length - 1)])
    if ($LASTEXITCODE -ne 0) {
        # Read before Assert-QuietTree: its own git calls overwrite $LASTEXITCODE.
        $code = $LASTEXITCODE
        Write-Host "FAILED at stage $script:Stage ($Name), exit code $code" -ForegroundColor Red
        Assert-QuietTree
        exit $code
    }
}

Invoke-Stage 'install'   @('npm', '--prefix', 'dashboard', 'ci', '--prefer-offline')
Invoke-Stage 'lint'      @('npm', '--prefix', 'dashboard', 'run', 'lint')
Invoke-Stage 'typecheck' @('npm', '--prefix', 'dashboard', 'run', 'typecheck')
Invoke-Stage 'test'      @('npm', '--prefix', 'dashboard', 'run', 'test')
Invoke-Stage 'build'     @('npm', '--prefix', 'dashboard', 'run', 'build')
Invoke-Stage 'export'    @('npm', '--prefix', 'dashboard', 'run', 'export')
Invoke-Stage 'e2e'       @('npm', '--prefix', 'dashboard', 'run', 'e2e')

Assert-QuietTree

Write-Host ''
Write-Host 'verify-dashboard: green (7/7 stages)' -ForegroundColor Green
exit 0
