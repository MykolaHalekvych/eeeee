<#
.SYNOPSIS
  Stage5.B terminal proofs window (PAPER+HALT+stop.flag) with auto-revert.

Exit codes:
  0 = ALL_PASS (CANCELLED + REJECTED + FILLED_ROUNDTRIP AND post baseline clean)
  1 = BLOCKED/FAIL (baseline dirty, IB 399 blocked, any proof failed, post baseline not clean)
  2 = INFRA/SCRIPT_ERROR
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [string]$ContractJson = "args\data\ibkr_mhg_contract_v1.json",
  [string]$Symbol = "MHG",
  [double]$LmtPrice = 1.0,
  [int]$RoundtripQty = 1,
  [switch]$AllowDirtyBaseline,
  [switch]$NoRevert
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = (Resolve-Path $Repo).Path
$stopFlag = Join-Path $repo "args\logs\stop.flag"

function Patch-ControlPlane([string]$repo, [string]$mode, [bool]$enable, [string]$gmode) {
  $enablePy = if ($enable) { "True" } else { "False" }
  Push-Location $repo
  try {
    $null = py -3.11 -c "import json; from pathlib import Path; p=Path('args/data/control_plane.json'); d=json.loads(p.read_bytes().decode('utf-8-sig')); d['execution_mode']='$mode'; d['enable_paper_execution']=$enablePy; d['global_mode']='$gmode'; p.write_text(json.dumps(d, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')"
  } finally { Pop-Location }
}

function Run-BaselineCheck([string]$repo) {
  Push-Location $repo
  try {
    $out = py -3.11 -m args.ops.baseline_check_v1 --repo $repo
    $rc = $LASTEXITCODE
    $obj = $null
    try { $obj = $out | ConvertFrom-Json } catch { $obj = @{ parse_error = "baseline_check_not_json"; raw = ($out | Out-String) } }
    return @{ rc=$rc; obj=$obj }
  } finally { Pop-Location }
}

function Run-Proof([string]$repo, [string[]]$argsList) {
  Push-Location $repo
  try {
    $out = py -3.11 -m args.demo.demo_terminal_proof_pack_v1c --repo $repo @argsList
    $rc = $LASTEXITCODE
    $obj = $null
    try { $obj = $out | ConvertFrom-Json } catch { $obj = @{ parse_error="proof_not_json"; raw=($out | Out-String) } }
    return @{ rc=$rc; obj=$obj }
  } finally { Pop-Location }
}

$exit = 2
$armed = $false

$report = @{
  schema="stage5_terminal_proofs_window_v1"
  ts_utc=(Get-Date).ToUniversalTime().ToString("o")
  repo=$repo
  ok=$false
  reason=$null

  pre_baseline=$null
  armed=$false
  proofs=@()
  post_baseline=$null

  reverted=$false
  revert_error=$null

  exit_code=2
  errors=@()
}

try {
  # Pre baseline
  $pre = Run-BaselineCheck $repo
  $report.pre_baseline = $pre

  if (-not $AllowDirtyBaseline) {
    if ([int]$pre.rc -ne 0) {
      $exit = 1
      $report.reason = "BLOCKED_BASELINE_DIRTY_RUN_CLEAN_WINDOW_FIRST"
      throw "BLOCKED_BASELINE_DIRTY"
    }
  }

  # Arm PAPER execution
  New-Item -ItemType Directory -Force -Path (Split-Path $stopFlag) | Out-Null
  Patch-ControlPlane $repo "PAPER" $true "HALT"
  New-Item -ItemType File -Force -Path $stopFlag | Out-Null
  $armed = $true
  $report.armed = $true

  # Proof 1: CANCELLED
  $p1 = Run-Proof $repo @(
    "--scenario","scenario_cancelled_v1",
    "--confirm-paper",
    "--contract-json",$ContractJson,
    "--symbol",$Symbol,
    "--lmt-price",$LmtPrice
  )
  $report.proofs += @{ name="CANCELLED"; rc=$p1.rc; obj=$p1.obj }

  # Proof 2: REJECTED
  $p2 = Run-Proof $repo @(
    "--scenario","scenario_rejected_v1",
    "--confirm-paper",
    "--contract-json",$ContractJson,
    "--symbol",$Symbol
  )
  $report.proofs += @{ name="REJECTED"; rc=$p2.rc; obj=$p2.obj }

  # Proof 3: FILLED_ROUNDTRIP
  $p3 = Run-Proof $repo @(
    "--scenario","scenario_fill_v1",
    "--confirm-paper",
    "--confirm-fill","YES",
    "--confirm-roundtrip","YES",
    "--roundtrip-qty",$RoundtripQty,
    "--contract-json",$ContractJson,
    "--symbol",$Symbol
  )
  $report.proofs += @{ name="FILLED_ROUNDTRIP"; rc=$p3.rc; obj=$p3.obj }

  # Post baseline
  $post = Run-BaselineCheck $repo
  $report.post_baseline = $post

  # Decide
  $allOk = $true
  foreach ($pp in $report.proofs) { if ([int]$pp.rc -ne 0) { $allOk = $false } }
  $postClean = ([int]$post.rc -eq 0)

  if ($allOk -and $postClean) {
    $report.ok = $true
    $exit = 0
  } else {
    $report.ok = $false
    $exit = 1
    if (-not $allOk) { $report.reason = "PROOF_FAILED_OR_BLOCKED" }
    elseif (-not $postClean) { $report.reason = "POST_BASELINE_NOT_CLEAN" }
  }

} catch {
  # If we threw a deliberate BLOCKED_BASELINE_DIRTY, keep exit=1
  if ($exit -eq 2) {
    $exit = 2
    $report.errors += @{ where="exception"; error=($_ | Out-String) }
  }
} finally {
  if (-not $NoRevert) {
    try {
      # Always revert to DRYRUN and remove stop.flag (safe even if not armed)
      Patch-ControlPlane $repo "DRYRUN" $false "ONLY_EXITS"
      if (Test-Path $stopFlag) { Remove-Item $stopFlag -Force }
      $report.reverted = $true
    } catch {
      $report.reverted = $false
      $report.revert_error = ($_ | Out-String)
      if ($exit -eq 0) { $exit = 2 }  # do not allow PASS if revert failed
    }
  }
}

$report.ts_end_utc = (Get-Date).ToUniversalTime().ToString("o")
$report.exit_code = $exit

# JSON-only stdout (single object)
$report | ConvertTo-Json -Depth 12
exit $exit
