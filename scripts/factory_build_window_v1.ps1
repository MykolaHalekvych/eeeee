param(
  [string]$Repo = ".",
  [string]$Python = "py -3.11",
  [string]$ControlPlane = "control_plane.json",
  [string]$Product = "cicd_release_pack_v0",
  [string]$Factory = "local",

  # Compatibility with Control runner: accept these even if Product/Factory defaults are used
  [string]$KitId = "",
  [ValidateSet("YES","NO")]
  [string]$RunAcceptance = "YES",

  # --- Control non-bypass token (MUST be provided by Control) ---
  [string]$ControlTokenPath = "",
  [string]$ControlRunId = "",
  [string]$ControlJobType = "FOUNDRY_BUILD_WINDOW_V1"
)

$ErrorActionPreference = "Stop"

# ----------------------------
# Helpers
# ----------------------------

function _QuoteArg([string]$a) {
  if ($null -eq $a) { return "" }
  if ($a -match "\s") {
    $q = $a -replace '"','\"'
    return '"' + $q + '"'
  }
  return $a
}

function _FmtCmd([string]$exe, [string[]]$args) {
  $parts = @($exe) + $args
  $q = @()
  foreach ($p in $parts) {
    $pp = [string]$p
    $q += (_QuoteArg $pp)
  }
  return ($q -join " ")
}

# Parse $Python ("py -3.11") into exe + base args
$pyParts = $Python -split '\s+'
$pyExe = $pyParts[0]
$pyBaseArgs = @()
if ($pyParts.Length -gt 1) { $pyBaseArgs = $pyParts[1..($pyParts.Length-1)] }

function InvokePyCapture {
  param(
    [Parameter(Mandatory=$true)][string[]]$Args,
    [string]$Label = "PY"
  )

  if ($null -eq $Args -or $Args.Count -eq 0) {
    throw ("PY_ARGS_EMPTY label=" + $Label)
  }

  # Ban stdin-mode "-" in this window (too easy to slip into interactive)
  if ($Args.Count -ge 1 -and $Args[0] -eq "-") {
    throw ("PY_STDIN_MODE_FORBIDDEN label=" + $Label)
  }

  $out = & $pyExe @pyBaseArgs @Args
  $rc = $LASTEXITCODE
  return @{
    out = (($out | Out-String).Trim())
    rc  = [int]$rc
  }
}

# ----------------------------
# M8 RUNS/EVIDENCE v0 (WRAPPED INTO WINDOW)
# writes:
#   args\data\runs\<run_id>\events.jsonl
#   args\data\runs\<run_id>\final_report.json
# stdout remains single JSON
# ----------------------------

$runId = $null
$runDir = $null
$runsEventsPath = $null
$runsFinalReportPath = $null

function RunsSafeCall {
  param([string[]]$Args)

  # Critical: never allow accidental "py -3.11" with empty argv
  if ($null -eq $Args -or $Args.Count -eq 0) { return }

  try {
    & $pyExe @pyBaseArgs @Args | Out-Null
  } catch {
    # best-effort; do not break window execution if evidence logging fails
  }
}

function RunsStatusFromRc([int]$rc) {
  if ($rc -eq 0) { return "PASS" }
  elseif ($rc -eq 2) { return "ERROR" }
  else { return "FAIL" }
}

function RunsInit {
  param([string]$RepoRoot, [string]$ControlPlanePath, [string]$ProductName, [string]$FactoryName)

  $runsRoot = Join-Path $RepoRoot "args\data\runs"
  New-Item -ItemType Directory -Force $runsRoot | Out-Null

  $rid = (& $pyExe @pyBaseArgs -m args.foundry.runs_v0 new-run-id --repo-root $RepoRoot) 2>$null
  if ($LASTEXITCODE -ne 0) { return @{ ok=$false } }

  $rid = ($rid | Out-String).Trim()
  $rdir = Join-Path $runsRoot $rid
  New-Item -ItemType Directory -Force $rdir | Out-Null

  RunsSafeCall @(
    "-m","args.foundry.runs_v0","init",
    "--run-dir",$rdir,
    "--run-id",$rid,
    "--repo-root",$RepoRoot,
    "--product",$ProductName,
    "--factory",$FactoryName,
    "--control-plane",$ControlPlanePath
  )

  # DRIFT_GUARD: fail-closed drift gate (Foundry)
  $__drift_gate_run = "DRIFT_PRE_" + ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"))
  powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "gate_drift_foundry_v0.ps1") -RunId $__drift_gate_run | Out-Host
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

  return @{
    ok=$true
    run_id=$rid
    run_dir=$rdir
    events_path=(Join-Path $rdir "events.jsonl")
    final_report_path=(Join-Path $rdir "final_report.json")
  }
}

function RunsEvent {
  param([string]$RunDir, [string]$Event, [int]$Rc, [hashtable]$Data)

  $status = RunsStatusFromRc $Rc
  if ($null -ne $Data) {
    $dataJson = ($Data | ConvertTo-Json -Compress -Depth 20)
    RunsSafeCall @(
      "-m","args.foundry.runs_v0","event",
      "--run-dir",$RunDir,
      "--event",$Event,
      "--status",$status,
      "--exit-code",$Rc,
      "--data-json",$dataJson
    )
  } else {
    RunsSafeCall @(
      "-m","args.foundry.runs_v0","event",
      "--run-dir",$RunDir,
      "--event",$Event,
      "--status",$status,
      "--exit-code",$Rc
    )
  }
}

function RunsFinalize {
  param([string]$RunDir, [int]$OverallRc, [hashtable]$Seed)

  $overallStatus = if ($OverallRc -eq 0) { "PASS" } elseif ($OverallRc -eq 2) { "HALT" } else { "FAIL" }

  $seedPath = Join-Path $RunDir "seed.json"
  if ($null -ne $Seed) {
    ($Seed | ConvertTo-Json -Depth 30) | Set-Content -LiteralPath $seedPath -Encoding utf8
    RunsSafeCall @(
      "-m","args.foundry.runs_v0","finalize",
      "--run-dir",$RunDir,
      "--overall-status",$overallStatus,
      "--overall-exit-code",$OverallRc,
      "--seed-file",$seedPath
    )
  } else {
    RunsSafeCall @(
      "-m","args.foundry.runs_v0","finalize",
      "--run-dir",$RunDir,
      "--overall-status",$overallStatus,
      "--overall-exit-code",$OverallRc
    )
  }
}

# ----------------------------
# original helpers (kept)
# ----------------------------

$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")

function EmitJsonAndExit([hashtable]$obj, [int]$code) {
  $obj.exit_code = $code

  # attach runs info if present
  if ($runId) {
    $obj.run_id = $runId
    $obj.run_dir = $runDir
    if (-not $obj.paths) { $obj.paths = @{} }
    $obj.paths.events_jsonl = $runsEventsPath
    $obj.paths.final_report_json = $runsFinalReportPath
  }

  $json = ($obj | ConvertTo-Json -Compress -Depth 20)
  Write-Output $json
  exit $code
}

function ReadText([string]$path) {
  return Get-Content -LiteralPath $path -Raw -ErrorAction Stop
}

function WriteText([string]$path, [string]$text) {
  Set-Content -LiteralPath $path -Value $text -Encoding utf8 -ErrorAction Stop
}

function TempJson([string]$leaf) {
  $ts2 = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmssfff")
  return (Join-Path $env:TEMP ("ARGS_ENGINE_" + $leaf + "_" + $ts2 + ".json"))
}

# --- Map KitId -> Product if caller uses kit_* and Product left default ---
if (-not [string]::IsNullOrWhiteSpace($KitId)) {
  if ($KitId -match '^kit_(.+)$') {
    $maybeProduct = $Matches[1]
    if ($Product -eq "cicd_release_pack_v0" -and $maybeProduct) {
      $Product = $maybeProduct
    }
  }
}

# --- Resolve repo root ---
$repoPath = (Resolve-Path -LiteralPath $Repo).Path
Set-Location -LiteralPath $repoPath

# ----------------------------
# CONTROL NON-BYPASS TOKEN CHECK (DENY EARLY)
# Must run BEFORE any drift/guards/build work.
# ----------------------------

function _ControlTokenDeny([string]$reason, [string]$detail) {
  EmitJsonAndExit @{
    schema="factory_build_window_m8_v1";
    ok=$false;
    ts_utc=$ts;
    repo=$repoPath;
    product=$Product;
    factory=$Factory;
    control_plane=$ControlPlane;
    error=$reason;
    detail=$detail;
    control_token_path=$ControlTokenPath;
    control_run_id=$ControlRunId;
    control_job_type=$ControlJobType;
  } 1
}

$expectedJobType = "FOUNDRY_BUILD_WINDOW_V1"
if ([string]::IsNullOrWhiteSpace($ControlJobType)) { $ControlJobType = $expectedJobType }
if ($ControlJobType -ne $expectedJobType) {
  _ControlTokenDeny "CONTROL_TOKEN.JOB_TYPE_UNEXPECTED" ("expected=" + $expectedJobType + "; got=" + $ControlJobType)
}

if ([string]::IsNullOrWhiteSpace($ControlTokenPath)) {
  _ControlTokenDeny "CONTROL_TOKEN.MISSING" "ControlTokenPath empty"
}
if ([string]::IsNullOrWhiteSpace($ControlRunId)) {
  _ControlTokenDeny "CONTROL_TOKEN.RUN_ID_MISSING" "ControlRunId empty"
}
if (-not (Test-Path -LiteralPath $ControlTokenPath)) {
  _ControlTokenDeny "CONTROL_TOKEN.MISSING" ("token not found: " + $ControlTokenPath)
}

$tok = $null
try {
  $tok = Get-Content -LiteralPath $ControlTokenPath -Raw -ErrorAction Stop | ConvertFrom-Json
} catch {
  _ControlTokenDeny "CONTROL_TOKEN.BAD_JSON" $_.Exception.Message
}

if ($tok.schema -ne "control_token_v0") {
  _ControlTokenDeny "CONTROL_TOKEN.SCHEMA_MISMATCH" ("schema=" + [string]$tok.schema)
}
if ($tok.run_id -ne $ControlRunId) {
  _ControlTokenDeny "CONTROL_TOKEN.RUN_ID_MISMATCH" ("token.run_id=" + [string]$tok.run_id + "; expected=" + $ControlRunId)
}
if ($tok.job_type -ne $ControlJobType) {
  _ControlTokenDeny "CONTROL_TOKEN.JOB_TYPE_MISMATCH" ("token.job_type=" + [string]$tok.job_type + "; expected=" + $ControlJobType)
}
if (-not ([string]$tok.nonce -match '^[a-f0-9]{16,64}$')) {
  _ControlTokenDeny "CONTROL_TOKEN.NONCE_INVALID" ("nonce=" + [string]$tok.nonce)
}

# Optional human acceptance gate (Control always passes YES)
if ($RunAcceptance -ne "YES") {
  _ControlTokenDeny "RUN_ACCEPTANCE.DENY" ("RunAcceptance=" + $RunAcceptance)
}

# --- Guards ---
if (-not (Test-Path -LiteralPath ".\.args_engine_repo")) {
  EmitJsonAndExit @{
    schema="factory_build_window_m8_v1";
    ok=$false;
    ts_utc=$ts;
    error="ENG guard failed: missing .args_engine_repo";
  } 2
}

$stopFlag = ".\args\control\stop.flag"
if (Test-Path -LiteralPath $stopFlag) {
  # create a run even on HALT (useful evidence)
  $cpTry = $ControlPlane
  try { $cpTry = (Resolve-Path -LiteralPath $ControlPlane).Path } catch { $cpTry = $ControlPlane }

  $init = RunsInit -RepoRoot $repoPath -ControlPlanePath $cpTry -ProductName $Product -FactoryName $Factory
  if ($init.ok) {
    $runId = $init.run_id
    $runDir = $init.run_dir
    $runsEventsPath = $init.events_path
    $runsFinalReportPath = $init.final_report_path

    RunsEvent -RunDir $runDir -Event "HALT" -Rc 2 -Data @{ reason="stop.flag present"; stop_flag=$stopFlag }
    RunsFinalize -RunDir $runDir -OverallRc 2 -Seed @{
      window="scripts/factory_build_window_v1.ps1"
      repo_root=$repoPath
      product=$Product
      factory=$Factory
      control_plane=$ControlPlane
      stop_flag=$stopFlag
    }
  }

  EmitJsonAndExit @{
    schema="factory_build_window_m8_v1";
    ok=$false;
    ts_utc=$ts;
    error="HALT: stop.flag present";
    stop_flag=$stopFlag;
  } 2
}

# --- Paths ---
$cpPath = (Resolve-Path -LiteralPath $ControlPlane).Path
$backupPath = TempJson "control_plane_backup"

# --- Init runs (after guards) ---
$initRuns = RunsInit -RepoRoot $repoPath -ControlPlanePath $cpPath -ProductName $Product -FactoryName $Factory
if ($initRuns.ok) {
  $runId = $initRuns.run_id
  $runDir = $initRuns.run_dir
  $runsEventsPath = $initRuns.events_path
  $runsFinalReportPath = $initRuns.final_report_path
}

# --- Backup control plane to TEMP ---
$cpRaw = $null
try {
  $cpRaw = ReadText $cpPath
  WriteText $backupPath $cpRaw
} catch {
  if ($runDir) {
    RunsEvent -RunDir $runDir -Event "CONTROL_PLANE_BACKUP" -Rc 2 -Data @{ error=$_.Exception.Message; control_plane=$cpPath }
    RunsFinalize -RunDir $runDir -OverallRc 2 -Seed @{
      window="scripts/factory_build_window_v1.ps1"
      repo_root=$repoPath
      product=$Product
      factory=$Factory
      control_plane=$ControlPlane
    }
  }

  EmitJsonAndExit @{
    schema="factory_build_window_m8_v1";
    ok=$false;
    ts_utc=$ts;
    repo=$repoPath;
    control_plane=$ControlPlane;
    error=("control_plane backup failed: " + $_.Exception.Message);
  } 2
}

function Set-AllowBuild([bool]$enabled) {
  $obj = $cpRaw | ConvertFrom-Json
  if (-not $obj.engine) { $obj | Add-Member -NotePropertyName engine -NotePropertyValue (@{}) }
  if (-not $obj.engine.permissions) { $obj.engine | Add-Member -NotePropertyName permissions -NotePropertyValue (@{}) }
  $obj.engine.permissions.ALLOW_BUILD = $enabled
  $new = ($obj | ConvertTo-Json -Depth 50)
  WriteText $cpPath $new
}

$gateJson = $null
$planJson = $null
$buildJson = $null

$gateRc = $null
$planRc = $null
$buildRc = $null

try {
  # Enable build for this window
  Set-AllowBuild $true

  # 0) Gate (NO Invoke-Expression; argv only)
  $gateArgs = @("-m","args.foundry.gate_v0","--control-plane", (".\" + $ControlPlane))
  $gateCmdStr = _FmtCmd $pyExe (@($pyBaseArgs) + $gateArgs)
  $gateRes = InvokePyCapture -Args $gateArgs -Label "GATE"
  $gateJson = $gateRes.out
  $gateRc = $gateRes.rc
  if ($runDir) { RunsEvent -RunDir $runDir -Event "GATE" -Rc $gateRc -Data @{ cmd=$gateCmdStr } }
  if ($gateRc -ne 0) { throw ("gate failed rc=" + $gateRc) }

  # 1) Plan
  $planArgs = @(
    "-m","args.foundry.plan_v0",
    "--control-plane", (".\" + $ControlPlane),
    "--product", $Product,
    "--factory", $Factory
  )
  $planCmdStr = _FmtCmd $pyExe (@($pyBaseArgs) + $planArgs)
  $planRes = InvokePyCapture -Args $planArgs -Label "PLAN"
  $planJson = $planRes.out
  $planRc = $planRes.rc
  if ($runDir) { RunsEvent -RunDir $runDir -Event "PLAN" -Rc $planRc -Data @{ cmd=$planCmdStr; product=$Product; factory=$Factory } }
  if ($planRc -ne 0) { throw ("plan failed rc=" + $planRc) }

  # 2) Build
  $buildArgs = @(
    "-m","args.foundry.build_v0",
    "--control-plane", (".\" + $ControlPlane),
    "--product", $Product,
    "--factory", $Factory
  )
  $buildCmdStr = _FmtCmd $pyExe (@($pyBaseArgs) + $buildArgs)
  $buildRes = InvokePyCapture -Args $buildArgs -Label "BUILD"
  $buildJson = $buildRes.out
  $buildRc = $buildRes.rc
  if ($runDir) { RunsEvent -RunDir $runDir -Event "BUILD" -Rc $buildRc -Data @{ cmd=$buildCmdStr; product=$Product } }
  if ($buildRc -ne 0) { throw ("build failed rc=" + $buildRc) }

  if ($runDir) {
    RunsFinalize -RunDir $runDir -OverallRc 0 -Seed @{
      window="scripts/factory_build_window_v1.ps1"
      repo_root=$repoPath
      product=$Product
      factory=$Factory
      control_plane=$ControlPlane
      artifacts=@(
        @{ label="bundle_zip";  path="dist/$Product/bundle.zip" },
        @{ label="evidence_md"; path="dist/$Product/evidence.md" },
        @{ label="hashes_json"; path="dist/$Product/hashes.json" },
        @{ label="runbook_md";  path="dist/$Product/runbook.md" }
      )
    }
  }

  EmitJsonAndExit @{
    schema="factory_build_window_m8_v1";
    ok=$true;
    ts_utc=$ts;
    repo=$repoPath;
    product=$Product;
    factory=$Factory;
    control_plane=$ControlPlane;
    gate_rc=$gateRc;
    plan_rc=$planRc;
    build_rc=$buildRc;
    gate_json=$gateJson;
    plan_json=$planJson;
    build_json=$buildJson;
  } 0
}
catch {
  $err = $_.Exception.Message
  $code = 2
  if ($gateRc -eq 1 -or $planRc -eq 1 -or $buildRc -eq 1) { $code = 1 }

  if ($runDir) {
    RunsEvent -RunDir $runDir -Event "WINDOW_ERROR" -Rc $code -Data @{
      error=$err; gate_rc=$gateRc; plan_rc=$planRc; build_rc=$buildRc
    }

    RunsFinalize -RunDir $runDir -OverallRc $code -Seed @{
      window="scripts/factory_build_window_v1.ps1"
      repo_root=$repoPath
      product=$Product
      factory=$Factory
      control_plane=$ControlPlane
      error=$err
      artifacts=@(
        @{ label="bundle_zip";  path="dist/$Product/bundle.zip" },
        @{ label="evidence_md"; path="dist/$Product/evidence.md" },
        @{ label="hashes_json"; path="dist/$Product/hashes.json" },
        @{ label="runbook_md";  path="dist/$Product/runbook.md" }
      )
    }
  }

  EmitJsonAndExit @{
    schema="factory_build_window_m8_v1";
    ok=$false;
    ts_utc=$ts;
    repo=$repoPath;
    product=$Product;
    factory=$Factory;
    control_plane=$ControlPlane;
    error=$err;
    gate_rc=$gateRc;
    plan_rc=$planRc;
    build_rc=$buildRc;
    gate_json=$gateJson;
    plan_json=$planJson;
    build_json=$buildJson;
  } $code
}
finally {
  # Always restore original control plane
  try {
    if (Test-Path -LiteralPath $backupPath) {
      $orig = ReadText $backupPath
      WriteText $cpPath $orig
      Remove-Item -Force $backupPath -ErrorAction SilentlyContinue
    }
  } catch {
    # no-op
  }
}
