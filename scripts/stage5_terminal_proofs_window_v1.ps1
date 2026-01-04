<#
.SYNOPSIS
  Stage5.B terminal proofs window (PAPER+HALT+stop.flag) with per-proof baseline cleanup + auto-revert.

Exit codes:
  0 = ALL_PASS (all proofs rc==0 AND post baseline clean)
  1 = BLOCKED/FAIL (baseline dirty, any proof failed/blocked, post baseline not clean)
  2 = INFRA/SCRIPT_ERROR (exceptions, infra failures, revert failure)
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [string]$ContractJson = "args\data\ibkr_mhg_contract_v1.json",
  [string]$Symbol = "MHG",
  [double]$LmtPrice = 1.0,
  [int]$RoundtripQty = 1,
  [switch]$AllowDirtyBaseline,
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
  $enablePy = if ($enable) { "True" } else { "False" }
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
    try { $obj = $out | ConvertFrom-Json } catch { $obj = @{ parse_error = "baseline_check_not_json"; raw = ($out | Out-String) } }
    return @{ rc=$rc; obj=$obj }
  } finally { Pop-Location }
}

function Run-BaselineCleaner([string]$repo) {
  Push-Location $repo
  try {
    $out = py -3.11 -m args.ops.baseline_cleaner_v1 --repo $repo
    $rc = $LASTEXITCODE
    $obj = $null
    try { $obj = $out | ConvertFrom-Json } catch { $obj = @{ parse_error = "baseline_cleaner_not_json"; raw = ($out | Out-String) } }
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

# Ensure stop.flag dir exists
New-Item -ItemType Directory -Force -Path (Split-Path $stopFlag) | Out-Null

$exit = 2

$report = @{
  schema="stage5_terminal_proofs_window_v1"
  ts_utc=(Get-Date).ToUniversalTime().ToString("o")
  repo=$repo

  control_plane_path=$cpAbs
  stop_flag_path=$stopFlag

  control_plane_before=$null
  control_plane_armed=$null
  control_plane_after_revert=$null
  git_restored=$false

  ok=$false
  reason=$null

  pre_baseline=$null
  armed=$false

  proofs=@()
  baseline_steps=@()
  cleanup_steps=@()
  post_baseline=$null

  reverted=$false
  revert_error=$null

  exit_code=2
  errors=@()
}

try {
  $report.control_plane_before = Read-ControlPlane $repo

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

  # Arm PAPER execution for proofs
  Patch-ControlPlane $repo "PAPER" $true "HALT"
  New-Item -ItemType File -Force -Path $stopFlag | Out-Null
  $report.armed = $true
  $report.control_plane_armed = Read-ControlPlane $repo

  $proofDefs = @(
    @{ name="CANCELLED"; args=@("--scenario","scenario_cancelled_v1","--confirm-paper","--contract-json",$ContractJson,"--symbol",$Symbol,"--lmt-price",$LmtPrice) },
    @{ name="REJECTED"; args=@("--scenario","scenario_rejected_v1","--confirm-paper","--contract-json",$ContractJson,"--symbol",$Symbol) },
    @{ name="FILLED_ROUNDTRIP"; args=@("--scenario","scenario_fill_v1","--confirm-paper","--confirm-fill","YES","--confirm-roundtrip","YES","--roundtrip-qty",$RoundtripQty,"--contract-json",$ContractJson,"--symbol",$Symbol) }
  )

  foreach ($def in $proofDefs) {
    # Run proof
    $p = Run-Proof $repo $def.args
    $report.proofs += @{ name=$def.name; rc=$p.rc; obj=$p.obj }

    # Baseline after proof
    $b1 = Run-BaselineCheck $repo
    $report.baseline_steps += @{ stage=("$($def.name)_after_proof"); rc=$b1.rc; obj=$b1.obj }

    # If dirty, attempt cleanup (cancel-only, gated)
    if ([int]$b1.rc -ne 0) {
      $c = Run-BaselineCleaner $repo
      $report.cleanup_steps += @{ stage=("$($def.name)_cleanup"); rc=$c.rc; obj=$c.obj }

      $b2 = Run-BaselineCheck $repo
      $report.baseline_steps += @{ stage=("$($def.name)_after_cleanup"); rc=$b2.rc; obj=$b2.obj }
    }

    # Fail-fast if proof not ok
    if ([int]$p.rc -ne 0) {
      $exit = 1
      $report.reason = "PROOF_FAILED_OR_BLOCKED"
      break
    }
  }

  # Post baseline (final)
  $post = Run-BaselineCheck $repo
  $report.post_baseline = $post

  $allRan = ($report.proofs.Count -eq 3)
  $allOk = $allRan
  foreach ($pp in $report.proofs) { if ([int]$pp.rc -ne 0) { $allOk = $false } }
  $postClean = ([int]$post.rc -eq 0)

  if ($allOk -and $postClean) {
    $report.ok = $true
    $exit = 0
    $report.reason = "ALL_PASS"
  } else {
    if ($exit -eq 2) { $exit = 1 }
    $report.ok = $false
    if (-not $report.reason) {
      if (-not $allOk) { $report.reason = "PROOF_FAILED_OR_BLOCKED" }
      elseif (-not $postClean) { $report.reason = "POST_BASELINE_NOT_CLEAN" }
      else { $report.reason = "NOT_ALL_PROOFS_RAN" }
    }
  }

} catch {
  # Only record unexpected exceptions (baseline-block is expected)
  if ($exit -eq 2 -and $report.reason -eq $null) {
    $report.errors += @{ where="exception"; error=($_ | Out-String) }
    $report.reason = "SCRIPT_ERROR"
  }
} finally {
  if (-not $NoRevert) {
    try {
      Patch-ControlPlane $repo "DRYRUN" $false "ONLY_EXITS"
      if (Test-Path $stopFlag) { Remove-Item $stopFlag -Force }
      $report.control_plane_after_revert = Read-ControlPlane $repo
      $report.reverted = $true
    } catch {
      $report.reverted = $false
      $report.revert_error = ($_ | Out-String)
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
$report | ConvertTo-Json -Depth 14
exit $exit
