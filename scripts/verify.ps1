#!/usr/bin/env pwsh
<#
.SYNOPSIS
    The verify harness (FR-3). One command reproduces the whole gate from a clean checkout.

.DESCRIPTION
    Six stages, always in this order, because the order is load-bearing: the cheapest failure
    runs first so a syntax error never waits on a test suite. The script stops at the first
    non-zero stage and never runs the later ones.

    No environment variable beyond those documented in .env.example is required, and no API key
    is required. The harness names `fake` for itself rather than relying on a default: ticket 032
    removed the silent fallback (FR-27), so an unconfigured checkout now fails loudly and a gate
    that did not declare its own offline-ness would be red on every clean clone.

.PARAMETER CovFailUnder
    Overrides the coverage ratchet. Defaults to the single integer in .spec/coverage-ratchet.

.EXAMPLE
    pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1
    pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1 -CovFailUnder 90
#>
[CmdletBinding()]
param(
    [int] $CovFailUnder = 0
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if ($CovFailUnder -le 0) {
    $RatchetFile = Join-Path $RepoRoot '.spec/coverage-ratchet'
    if (-not (Test-Path $RatchetFile)) {
        Write-Error "Missing coverage ratchet file: $RatchetFile"
        exit 1
    }
    $CovFailUnder = [int]((Get-Content -Raw $RatchetFile).Trim())
}

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
Write-Host "verify: tree before: HEAD $($Before.Head) status-sha256 $($Before.Tree)"

# Return when the tree is as it was; exit 3 when it moved, in which case the run is void.
function Assert-QuietTree {
    $after = Get-TreeState
    Write-Host "verify: tree after:  HEAD $($after.Head) status-sha256 $($after.Tree)"
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
    Write-Host "==> [$script:Stage/6] $Name" -ForegroundColor Cyan
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

# The gate declares its own offline-ness. FR-27 sanctions exactly three ways to select `fake` -
# the flag, this variable, or a pytest fixture - and `doctor` exposes no --provider flag, so the
# variable is the one available to a script. Set for every stage, not just doctor: the whole
# harness is the offline gate, and `tests/conftest.py` clears this for each test anyway, so the
# suite stays isolated from it.
$env:HARNESSED_PROVIDER = 'fake'

Invoke-Stage 'sync'   @('uv', 'sync', '--all-extras')
Invoke-Stage 'lint'   @('uv', 'run', 'ruff', 'check', '.')
Invoke-Stage 'format' @('uv', 'run', 'ruff', 'format', '--check', '.')
Invoke-Stage 'types'  @('uv', 'run', 'mypy', 'src/harnessed')
Invoke-Stage 'tests'  @('uv', 'run', 'pytest', '-q', '-m', 'not live', '--cov=src/harnessed', "--cov-fail-under=$CovFailUnder")
Invoke-Stage 'doctor' @('uv', 'run', 'harnessed', 'doctor', '--offline')

Assert-QuietTree

Write-Host ''
Write-Host "verify: green (6/6 stages, --cov-fail-under=$CovFailUnder)" -ForegroundColor Green
exit 0
