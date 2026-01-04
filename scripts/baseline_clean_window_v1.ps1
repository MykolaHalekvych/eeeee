<#
.SYNOPSIS
  Baseline clean window (cancel-only) for IBKR paper.

.DESCRIPTION
  - Arms PAPER gates (PAPER + enable_paper_execution=true + global_mode=HALT) + creates args/logs/stop.flag
  - Runs cancel-only baseline cleaner
  - Confirms with baseline check
  - Reverts back to DRYRUN + ONLY_EXITS + removes stop.flag (default)
  - Best-effort git-restore control_plane.json to keep repo clean (default)

Exit codes:
  0 = CLEAN (baseline_check after == 0)
  1 = NOT_CLEARED (baseline still dirty; typically WAIT_MARKET_OPEN / PendingCancel)
  2 = BLOCKED / INFRA_FAIL / SCRIPT_ERROR
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [switch]$NoRevert,
  [switch]$NoGitRestore
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = (Resolve-Path $Repo).Path
$stopFlag = Join-Path $repo "args\logs\stop.flag"
$cpRel = "args/data/control_plane.json"
$cpAbs = Join-Path $repo $cpRel

function Read-ControlPlane([string]$repo) {
  Push-Location $repo
  try {
    $out = py -3.11 -c "import json; from pathlib import Path; p=Path('args/data/control_plane.json'); d=json.loads(p.read_bytes().decode('utf-8-sig')); print(json.dumps({'execution_mode': d.get('execution_mode'), 'enable_paper_execution': bool(d.get('enable_paper_execution', False)), 'global_mode': d.get('global_mode')}, ensure_ascii=False))"
    return ($out | ConvertFrom-Json)
  } finally { Pop-Location }
}

function Patch-ControlPlane([string]$repo, [string]$mode, [bool]$enable, [string]$gmode) {
  $enablePy = if ($enable) { "True" } else { "False" }  # Python bool literal
  Push-Location $repo
  try {
    # Silent: no stdout
    $null = py -3.11 -c "import json; from pathlib import Path; p=Path('args/data/control_plane.json'); d=json.loads(p.read_bytes().decode('utf-8-sig')); d['execution_mode']='$mode'; d['enable_paper_execution']=$enablePy; d['global_mode']='$gmode'; p.write_text(json.dumps(d, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')"
  } finally { Pop-Location }
}

function Run-BaselineCheck([string]$repo) {
  Push-Location $repo
  try {
    $out = py -3.11 -m args.ops.baseline_check_v1 --repo $repo
    $rc = $LASTEXITCODE
    $obj = $null
    try { $obj = ($out | ConvertFrom-Json) } catch { $obj = @{ parse_error="baseline_check_not_json"; raw=($out | Out-String) } }
    return @{ rc=$rc; obj=$obj }
  } finally { Pop-Location }
}

function Run-BaselineCleaner([string]$repo) {
  Push-Location $repo
  try {
    $out = py -3.11 -m args.ops.baseline_cleaner_v1 --repo $repo
    $rc = $LASTEXITCODE
    $obj = $null
    try { $obj = ($out | ConvertFrom-Json) } catch { $obj = @{ parse_error="baseline_cleaner_not_json"; raw=($out | Out-String) } }
    return @{ rc=$rc; obj=$obj }
  } finally { Pop-Location }
}

# Ensure stop.flag dir exists (safe)
New-Item -ItemType Directory -Force -Path (Split-Path $stopFlag) | Out-Null

$exit = 2

$report = @{
  schema="baseline_clean_window_v1"
  ts_utc=(Get-Date).ToUniversalTime().ToString("o")
  repo=$repo

  control_plane_path=$cpAbs
  stop_flag_path=$stopFlag

  control_plane_before=$null
  control_plane_armed=$null
  control_plane_after_revert=$null

  before=$null
  cleaner=$null
  after=$null

  ok=$false
  reason=$null
  reverted=$false
  git_restored=$false
  errors=@()
}

try {
  $report.control_plane_before = Read-ControlPlane $repo

  $report.before = Run-BaselineCheck $repo

  # Arm PAPER gates + stop.flag
  Patch-ControlPlane $repo "PAPER" $true "HALT"
  New-Item -ItemType File -Force -Path $stopFlag | Out-Null
  $report.control_plane_armed = Read-ControlPlane $repo

  # Cancel-only attempt
  $report.cleaner = Run-BaselineCleaner $repo

  # Verify baseline
  $report.after = Run-BaselineCheck $repo

  $rcAfter = [int]$report.after.rc
  if ($rcAfter -eq 0) {
    $report.ok = $true
    $report.reason = "CLEAN"
    $exit = 0
  } elseif ($rcAfter -eq 1) {
    $report.ok = $false
    $report.reason = "NOT_CLEARED_WAIT_MARKET_OPEN"
    $exit = 1
  } else {
    $report.ok = $false
    $report.reason = "INFRA_FAIL_BASELINE_CHECK"
    $exit = 2
  }

} catch {
  $report.errors += @{ where="exception"; error=($_ | Out-String) }
  if (-not $report.reason) { $report.reason = "SCRIPT_ERROR" }
  $exit = 2

} finally {
  if (-not $NoRevert) {
    try {
      Patch-ControlPlane $repo "DRYRUN" $false "ONLY_EXITS"
      if (Test-Path $stopFlag) { Remove-Item $stopFlag -Force }
      $report.control_plane_after_revert = Read-ControlPlane $repo
      $report.reverted = $true
    } catch {
      $report.errors += @{ where="revert_failed"; error=($_ | Out-String) }
      $report.reverted = $false
      if ($exit -eq 0) { $exit = 2 }  # never claim PASS if revert failed
    }

    if (-not $NoGitRestore) {
      # Best-effort keep repo clean: restore control_plane.json from HEAD
      try {
        if (Test-Path (Join-Path $repo ".git")) {
          & git -C $repo restore --source=HEAD -- $cpRel *> $null
          $report.git_restored = $true
        }
      } catch {
        $report.git_restored = $false
      }
    }
  }
}

$report.ts_end_utc = (Get-Date).ToUniversalTime().ToString("o")
$report.exit_code = $exit

# JSON-only stdout (single object)
$report | ConvertTo-Json -Depth 10
exit $exit
