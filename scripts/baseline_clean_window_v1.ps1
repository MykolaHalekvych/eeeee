<#
.SYNOPSIS
  Baseline clean window (cancel-only) for IBKR paper.

.DESCRIPTION
  - Temporarily sets control_plane.json to: execution_mode=PAPER, enable_paper_execution=true, global_mode=HALT
  - Ensures args/logs/stop.flag exists
  - Runs args.ops.baseline_cleaner_v1 (cancel-only)
  - Runs args.ops.baseline_check_v1 to confirm
  - Reverts control_plane.json back to DRYRUN + removes stop.flag (default)

  Exit codes:
    0 = CLEAN
    1 = NOT_CLEARED (e.g., PendingCancel stuck / wait market open)
    2 = BLOCKED / INFRA_FAIL
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [switch]$NoRevert
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Patch-ControlPlane([string]$repo, [string]$mode, [bool]$enable, [string]$gmode) {
  Push-Location $repo
  try {
    py -3.11 -c "import json; from pathlib import Path; p=Path('args/data/control_plane.json'); d=json.loads(p.read_bytes().decode('utf-8-sig')); d['execution_mode']='$mode'; d['enable_paper_execution']=$('true' if $enable else 'false'); d['global_mode']='$gmode'; p.write_text(json.dumps(d, indent=2, ensure_ascii=False)+'\n', encoding='utf-8'); print('patched:', p)"
  } finally {
    Pop-Location
  }
}

function Run-BaselineCheck([string]$repo) {
  Push-Location $repo
  try {
    $out = py -3.11 -m args.ops.baseline_check_v1 --repo $repo
    $rc = $LASTEXITCODE
    return @{ rc=$rc; json=$out }
  } finally { Pop-Location }
}

function Run-BaselineCleaner([string]$repo) {
  Push-Location $repo
  try {
    $out = py -3.11 -m args.ops.baseline_cleaner_v1 --repo $repo
    $rc = $LASTEXITCODE
    return @{ rc=$rc; json=$out }
  } finally { Pop-Location }
}

$repo = (Resolve-Path $Repo).Path
$stopFlag = Join-Path $repo "args\logs\stop.flag"
New-Item -ItemType Directory -Force -Path (Split-Path $stopFlag) | Out-Null

$start = @{
  schema="baseline_clean_window_v1"
  ts_utc=(Get-Date).ToUniversalTime().ToString("o")
  repo=$repo
  ok=$false
  before=$null
  cleaner=$null
  after=$null
  reverted=$false
  errors=@()
}

try {
  $start.before = Run-BaselineCheck $repo

  Patch-ControlPlane $repo "PAPER" $true "HALT"
  New-Item -ItemType File -Force -Path $stopFlag | Out-Null

  $start.cleaner = Run-BaselineCleaner $repo
  $start.after = Run-BaselineCheck $repo

  # Decide result based on AFTER baseline_check exit code
  $rcAfter = [int]$start.after.rc
  if ($rcAfter -eq 0) { $start.ok = $true; $exit = 0 }
  elseif ($rcAfter -eq 1) { $exit = 1 }
  else { $exit = 2 }

} catch {
  $start.errors += @{ where="exception"; error=$_ | Out-String }
  $exit = 2
} finally {
  if (-not $NoRevert) {
    try {
      Patch-ControlPlane $repo "DRYRUN" $false "ONLY_EXITS"
      if (Test-Path $stopFlag) { Remove-Item $stopFlag -Force }
      $start.reverted = $true
    } catch {
      $start.errors += @{ where="revert_failed"; error=$_ | Out-String }
    }
  }
}

# JSON stdout (single object)
$start.ts_end_utc = (Get-Date).ToUniversalTime().ToString("o")
$start.exit_code = $exit
$start | ConvertTo-Json -Depth 6

exit $exit
