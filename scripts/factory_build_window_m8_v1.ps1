param(
  [string]$Repo = ".",
  [string]$ControlPlane = "control_plane.json",
  [string]$Product = "cicd_release_pack_v0",
  [string]$Factory = "local",
  [string]$PythonExe = ""   # required in your case
)

$ErrorActionPreference = "Stop"

function ReadText([string]$path) { Get-Content -LiteralPath $path -Raw -ErrorAction Stop }
function WriteText([string]$path, [string]$text) { Set-Content -LiteralPath $path -Value $text -Encoding utf8 -ErrorAction Stop }

function TempJson([string]$leaf) {
  $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmssfff")
  return (Join-Path $env:TEMP ("ARGS_ENGINE_" + $leaf + "_" + $ts + ".json"))
}

function QuoteArg([string]$a) {
  if ($null -eq $a) { return '""' }
  if ($a -match '[\s"]') {
    $escaped = $a -replace '"','\"'
    return '"' + $escaped + '"'
  }
  return $a
}

function ExecProc([string]$Exe, [string[]]$Argv, [string]$WorkDir) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $Exe
  $psi.WorkingDirectory = $WorkDir
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.Arguments = (($Argv | ForEach-Object { QuoteArg $_ }) -join ' ')

  $p = New-Object System.Diagnostics.Process
  $p.StartInfo = $psi
  [void]$p.Start()
  $out = $p.StandardOutput.ReadToEnd()
  $err = $p.StandardError.ReadToEnd()
  $p.WaitForExit()

  return @{
    out = ($out.Trim())
    err = ($err.Trim())
    rc  = $p.ExitCode
    exe = $Exe
    argv = $Argv
  }
}

# --- Resolve repo root ---
$repoPath = (Resolve-Path -LiteralPath $Repo).Path
Set-Location -LiteralPath $repoPath
$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")

# --- Guards ---
if (-not (Test-Path -LiteralPath ".\.args_engine_repo")) {
  Write-Output (@{schema="factory_build_window_m8_v5";ok=$false;ts_utc=$ts;error="ENG guard failed: missing .args_engine_repo";exit_code=2} | ConvertTo-Json -Compress -Depth 20)
  exit 2
}

$stopFlag = ".\args\control\stop.flag"
if (Test-Path -LiteralPath $stopFlag) {
  Write-Output (@{schema="factory_build_window_m8_v5";ok=$false;ts_utc=$ts;error="HALT: stop.flag present";stop_flag=$stopFlag;exit_code=2} | ConvertTo-Json -Compress -Depth 20)
  exit 2
}

# --- Choose Python exe deterministically (no py.exe) ---
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
  $msg = "PythonExe is required on this machine (py.exe is unreliable here). Use -PythonExe to point to python.exe."
  Write-Output (@{schema="factory_build_window_m8_v5";ok=$false;ts_utc=$ts;error=$msg;exit_code=2} | ConvertTo-Json -Compress -Depth 20)
  exit 2
}

$py = $PythonExe
if (-not (Test-Path -LiteralPath $py)) {
  $msg = "Python exe not found: $py"
  Write-Output (@{schema="factory_build_window_m8_v5";ok=$false;ts_utc=$ts;error=$msg;python_expected=$py;exit_code=2} | ConvertTo-Json -Compress -Depth 20)
  exit 2
}

# sanity: must be Python 3.x (accept 3.9/3.11/3.14 etc)
$ver = ExecProc $py @("-c","import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')") $repoPath
if ($ver.rc -ne 0 -or [string]::IsNullOrWhiteSpace($ver.out)) {
  $msg = "Python version probe failed."
  Write-Output (@{schema="factory_build_window_m8_v5";ok=$false;ts_utc=$ts;error=$msg;python_exe=$py;stderr=$ver.err;exit_code=2} | ConvertTo-Json -Compress -Depth 20)
  exit 2
}
$maj = ($ver.out.Split('.')[0] | ForEach-Object {[int]$_})
if ($maj -ne 3) {
  $msg = "Python major version must be 3.x (got '$($ver.out)')."
  Write-Output (@{schema="factory_build_window_m8_v5";ok=$false;ts_utc=$ts;error=$msg;python_exe=$py;python_version=$ver.out;exit_code=2} | ConvertTo-Json -Compress -Depth 20)
  exit 2
}

# --- M8 runs/evidence (best-effort) ---
$runId = $null
$runDir = $null
$runsEventsPath = $null
$runsFinalReportPath = $null

function RunsSafe([string[]]$Argv) {
  try { [void](ExecProc $py $Argv $repoPath) } catch { }
}

function RunsInit([string]$CpAbs) {
  $runsRoot = Join-Path $repoPath "args\data\runs"
  New-Item -ItemType Directory -Force $runsRoot | Out-Null

  $rid = ExecProc $py @("-m","args.foundry.runs_v0","new-run-id","--repo-root",$repoPath) $repoPath
  if ($rid.rc -ne 0 -or [string]::IsNullOrWhiteSpace($rid.out)) { return $false }

  $script:runId = $rid.out.Trim()
  $script:runDir = Join-Path $runsRoot $script:runId
  New-Item -ItemType Directory -Force $script:runDir | Out-Null

  RunsSafe @("-m","args.foundry.runs_v0","init","--run-dir",$script:runDir,"--run-id",$script:runId,"--repo-root",$repoPath,"--product",$Product,"--factory",$Factory,"--control-plane",$CpAbs)

  $script:runsEventsPath = (Join-Path $script:runDir "events.jsonl")
  $script:runsFinalReportPath = (Join-Path $script:runDir "final_report.json")
  return $true
}

function RunsEvent([string]$Event, [int]$Rc, [hashtable]$Data) {
  if (-not $runDir) { return }
  $status = if ($Rc -eq 0) { "PASS" } elseif ($Rc -eq 2) { "ERROR" } else { "FAIL" }
  if ($null -ne $Data) {
    $dataJson = ($Data | ConvertTo-Json -Compress -Depth 20)
    RunsSafe @("-m","args.foundry.runs_v0","event","--run-dir",$runDir,"--event",$Event,"--status",$status,"--exit-code",$Rc,"--data-json",$dataJson)
  } else {
    RunsSafe @("-m","args.foundry.runs_v0","event","--run-dir",$runDir,"--event",$Event,"--status",$status,"--exit-code",$Rc)
  }
}

function RunsFinalize([int]$OverallRc, [hashtable]$Seed) {
  if (-not $runDir) { return }
  $overallStatus = if ($OverallRc -eq 0) { "PASS" } elseif ($OverallRc -eq 2) { "HALT" } else { "FAIL" }
  $seedPath = Join-Path $runDir "seed.json"
  if ($null -ne $Seed) {
    ($Seed | ConvertTo-Json -Depth 30) | Set-Content -LiteralPath $seedPath -Encoding utf8
    RunsSafe @("-m","args.foundry.runs_v0","finalize","--run-dir",$runDir,"--overall-status",$overallStatus,"--overall-exit-code",$OverallRc,"--seed-file",$seedPath)
  } else {
    RunsSafe @("-m","args.foundry.runs_v0","finalize","--run-dir",$runDir,"--overall-status",$overallStatus,"--overall-exit-code",$OverallRc)
  }
}

function EmitJsonAndExit([hashtable]$obj, [int]$code) {
  $obj.exit_code = $code
  $obj.python_exe = $py
  $obj.python_version = $ver.out
  if ($runId) {
    $obj.run_id = $runId
    $obj.run_dir = $runDir
    if (-not $obj.paths) { $obj.paths = @{} }
    $obj.paths.events_jsonl = $runsEventsPath
    $obj.paths.final_report_json = $runsFinalReportPath
  }
  Write-Output ($obj | ConvertTo-Json -Compress -Depth 20)
  exit $code
}

# --- Paths ---
$cpPath = (Resolve-Path -LiteralPath $ControlPlane).Path
$backupPath = TempJson "control_plane_backup"

[void](RunsInit $cpPath)

# --- Backup control plane ---
$cpRaw = $null
try {
  $cpRaw = ReadText $cpPath
  WriteText $backupPath $cpRaw
} catch {
  RunsEvent "CONTROL_PLANE_BACKUP" 2 @{ error=$_.Exception.Message; control_plane=$cpPath }
  RunsFinalize 2 @{ window="scripts/factory_build_window_m8_v5.ps1"; repo_root=$repoPath; product=$Product; factory=$Factory; control_plane=$ControlPlane }
  EmitJsonAndExit @{ schema="factory_build_window_m8_v5"; ok=$false; ts_utc=$ts; repo=$repoPath; control_plane=$ControlPlane; error=("control_plane backup failed: " + $_.Exception.Message) } 2
}

function Set-AllowBuild([bool]$enabled) {
  $obj = $cpRaw | ConvertFrom-Json
  if (-not $obj.engine) { $obj | Add-Member -NotePropertyName engine -NotePropertyValue (@{}) }
  if (-not $obj.engine.permissions) { $obj.engine | Add-Member -NotePropertyName permissions -NotePropertyValue (@{}) }
  $obj.engine.permissions.ALLOW_BUILD = $enabled
  WriteText $cpPath ($obj | ConvertTo-Json -Depth 50)
}

$gateJson=$null; $planJson=$null; $buildJson=$null
$gateRc=$null;   $planRc=$null;   $buildRc=$null

try {
  Set-AllowBuild $true

  $gate = ExecProc $py @("-m","args.foundry.gate_v0","--control-plane",$cpPath) $repoPath
  $gateJson = $gate.out; $gateRc = $gate.rc
  RunsEvent "GATE" $gateRc @{ err=$gate.err }
  if ($gateRc -ne 0) { throw ("gate failed rc=" + $gateRc + " err=" + $gate.err) }

  $plan = ExecProc $py @("-m","args.foundry.plan_v0","--control-plane",$cpPath,"--product",$Product,"--factory",$Factory) $repoPath
  $planJson = $plan.out; $planRc = $plan.rc
  RunsEvent "PLAN" $planRc @{ err=$plan.err }
  if ($planRc -ne 0) { throw ("plan failed rc=" + $planRc + " err=" + $plan.err) }

  $build = ExecProc $py @("-m","args.foundry.build_v0","--control-plane",$cpPath,"--product",$Product,"--factory",$Factory) $repoPath
  $buildJson = $build.out; $buildRc = $build.rc
  RunsEvent "BUILD" $buildRc @{ err=$build.err }
  if ($buildRc -ne 0) { throw ("build failed rc=" + $buildRc + " err=" + $build.err) }

  RunsFinalize 0 @{
    window="scripts/factory_build_window_m8_v5.ps1"
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

  EmitJsonAndExit @{
    schema="factory_build_window_m8_v5";
    ok=$true;
    ts_utc=$ts;
    repo=$repoPath;
    product=$Product;
    factory=$Factory;
    control_plane=$ControlPlane;
    gate_rc=$gateRc; plan_rc=$planRc; build_rc=$buildRc;
    gate_json=$gateJson; plan_json=$planJson; build_json=$buildJson;
  } 0
}
catch {
  $err = $_.Exception.Message
  $code = 2
  if ($gateRc -eq 1 -or $planRc -eq 1 -or $buildRc -eq 1) { $code = 1 }

  RunsEvent "WINDOW_ERROR" $code @{ error=$err; gate_rc=$gateRc; plan_rc=$planRc; build_rc=$buildRc }
  RunsFinalize $code @{ window="scripts/factory_build_window_m8_v5.ps1"; repo_root=$repoPath; product=$Product; factory=$Factory; control_plane=$ControlPlane; error=$err }

  EmitJsonAndExit @{
    schema="factory_build_window_m8_v5";
    ok=$false;
    ts_utc=$ts;
    repo=$repoPath;
    product=$Product;
    factory=$Factory;
    control_plane=$ControlPlane;
    error=$err;
    gate_rc=$gateRc; plan_rc=$planRc; build_rc=$buildRc;
    gate_json=$gateJson; plan_json=$planJson; build_json=$buildJson;
  } $code
}
finally {
  try {
    if (Test-Path -LiteralPath $backupPath) {
      $orig = ReadText $backupPath
      WriteText $cpPath $orig
      Remove-Item -Force $backupPath -ErrorAction SilentlyContinue
    }
  } catch { }
}
