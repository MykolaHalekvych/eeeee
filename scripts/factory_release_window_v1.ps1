<#
factory_release_window_v1.ps1 — Contract Standard v0 + B06.2 non-bypass wiring

Contract Standard v0:
- stdout: EXACTLY 1 JSON
- writes: OutDir\summary.json ALWAYS
- step dirs: OutDir\step_* with stdout/stderr files
- exit codes: 0 PASS, 1 FAIL, 2 INFRA

B06.2:
- requires chain-gate PASS (Guardian+Governor+Vault) BEFORE enabling ALLOW_EXPORT and BEFORE any release/export

Notes:
- Fixes the PowerShell auto-variable collision: DO NOT use parameter name $Args (collides with $args).
- Captures chain-gate reason_code primarily from step_chain\summary.json (if present).
#>

param(
  [string]$Repo = ".",
  [string]$Python = "py -3.11 -c "import sys; sys.exit(0)"",
  [string]$ControlPlane = "control_plane.json",
  [string]$Product = "cicd_release_pack_v0",
  [string]$Factory = "local",
  [string]$ReleaseId = "",

  [ValidateSet("YES","NO")][string]$RunAcceptance = "NO",
  [string]$RunId = "",
  [string]$OutDir = "",

  # B06.2 chain-gate inputs
  [string]$TargetRunId = "",
  [string]$VaultRepo = "C:\Users\mukol\ARGS-Evidence-Vault-v0",
  [string]$GuardianExtraArgsJson = ".\args\\configs\\release_chain_v0\\guardian_extra_args_v0.json",
  [string]$GovernorExtraArgsJson = ".\args\\configs\\release_chain_v0\\governor_extra_args_v0.json",
  [ValidateSet("YES","NO")][string]$ChainChaosCorruptZipBeforeVerify = "NO"
)

# DRIFT_GUARD: fail-closed drift gate (Foundry)
$__drift_gate_run = "DRIFT_PRE_" + ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"))
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "gate_drift_foundry_v0.ps1") -RunId $__drift_gate_run | Out-Host
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }


Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ")) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")) }
function Ensure-Dir([string]$p) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
function ReadText([string]$p) { Get-Content -LiteralPath $p -Raw -ErrorAction Stop }
function WriteText([string]$p, [string]$t) { Set-Content -LiteralPath $p -Value $t -Encoding utf8 -ErrorAction Stop }

function RcToResult([int]$rc) {
  if ($rc -eq 0) { return "PASS" }
  if ($rc -eq 1) { return "FAIL" }
  return "INFRA"
}

function TryParseJson([string]$raw) {
  if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
  try { return ($raw | ConvertFrom-Json) } catch { return $null }
}

function Tokenize([string]$cmdLine) {
  $m = [regex]::Matches($cmdLine, '("([^"\\]|\\.)*"|\S+)')
  $t = @()
  foreach ($x in $m) {
    $s = $x.Value
    if ($s.StartsWith('"') -and $s.EndsWith('"')) { $s = $s.Substring(1, $s.Length-2) }
    $t += $s
  }
  return $t
}

function Run-PowershellFile([string]$StepDir, [string]$ScriptPath, [string[]]$ScriptArgs, [string]$WD) {
  Ensure-Dir $StepDir
  $stdout = Join-Path $StepDir "stdout.txt"
  $stderr = Join-Path $StepDir "stderr.txt"

  $psArgs = @("-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",$ScriptPath) + $ScriptArgs

  $p = Start-Process -FilePath "powershell" -ArgumentList $psArgs -WorkingDirectory $WD `
    -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr

  $rc = [int]$p.ExitCode

  # Prefer child summary.json reason_code if it exists
  $childSummaryPath = Join-Path $StepDir "summary.json"
  $childReason = $null
  if (Test-Path -LiteralPath $childSummaryPath) {
    $csRaw = ReadText $childSummaryPath
    $csObj = TryParseJson $csRaw
    if ($null -ne $csObj -and ($csObj.PSObject.Properties.Name -contains "reason_code")) { $childReason = $csObj.reason_code }
  } else {
    # Fallback: try parse stdout as JSON and take reason_code
    $raw = ""
    if (Test-Path -LiteralPath $stdout) { $raw = ReadText $stdout }
    $obj = TryParseJson $raw
    if ($null -ne $obj -and ($obj.PSObject.Properties.Name -contains "reason_code")) { $childReason = $obj.reason_code }
  }

  return @{
    exit_code=$rc
    stdout_path=$stdout
    stderr_path=$stderr
    child_summary_path=$childSummaryPath
    child_reason_code=$childReason
  }
}

function Run-PythonModule([string]$StepDir, [string]$PythonCmd, [string]$ModuleName, [string[]]$ModuleArgs, [string]$WD) {
  Ensure-Dir $StepDir
  $stdout = Join-Path $StepDir "stdout.txt"
  $stderr = Join-Path $StepDir "stderr.txt"

  $tok = Tokenize $PythonCmd
  $pyExe = $tok[0]
  $pyPrefix = @()
  if ($tok.Count -gt 1) { $pyPrefix = $tok[1..($tok.Count-1)] }

  $args = @()
  if ($pyPrefix.Count -gt 0) { $args += $pyPrefix }
  $args += @("-m", $ModuleName) + $ModuleArgs

  $p = Start-Process -FilePath $pyExe -ArgumentList $args -WorkingDirectory $WD `
    -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr

  $rc = [int]$p.ExitCode
  $raw = ""
  if (Test-Path -LiteralPath $stdout) { $raw = ReadText $stdout }
  $obj = TryParseJson $raw

  $childReason = $null
  if ($null -ne $obj -and ($obj.PSObject.Properties.Name -contains "reason_code")) { $childReason = $obj.reason_code }

  return @{
    exit_code=$rc
    stdout_path=$stdout
    stderr_path=$stderr
    child_reason_code=$childReason
    stdout_json=$obj
  }
}

function TempFile([string]$leaf) {
  $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmssfff")
  return (Join-Path $env:TEMP ("ARGS_ENGINE_" + $leaf + "_" + $ts + ".json"))
}

# Resolve repo + output
$repoPath = (Resolve-Path -LiteralPath $Repo).Path
Set-Location -LiteralPath $repoPath

$ts0 = UtcNowIso
if ([string]::IsNullOrWhiteSpace($RunId)) { $RunId = "RELEASE_WINDOW_" + (UtcNowId) }
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = ".\args\data\ops_evidence\release_window_v1\$RunId" }
Ensure-Dir $OutDir
$outDirFull = (Resolve-Path -LiteralPath $OutDir).Path
$summaryPath = Join-Path $outDirFull "summary.json"

$summary = @{
  schema="factory_release_window_v1"
  ts_utc=$ts0
  ts_utc_end=""
  run_id=$RunId
  repo=$repoPath
  out_dir=$outDirFull

  product=$Product
  factory=$Factory
  control_plane=$ControlPlane
  python=$Python

  target_run_id=$TargetRunId
  vault_repo=$VaultRepo
  guardian_extra_args_json=$GuardianExtraArgsJson
  governor_extra_args_json=$GovernorExtraArgsJson
  chain_chaos_corrupt_zip_before_verify=$ChainChaosCorruptZipBeforeVerify

  ok=$false
  result="INFRA"
  reason_code="INIT"
  exit_code=$RC_INFRA

  steps=@()

  error_type=""
  error_message=""
}

$finalRc = $RC_INFRA
$cpPath = $null
$cpBackup = $null
$cpRaw = $null

try {
  # Guards
  if ($RunAcceptance -ne "YES") { $summary.reason_code="RUN_ACCEPTANCE_REQUIRED"; $finalRc=$RC_FAIL; throw "RunAcceptance must be YES" }
  if (-not (Test-Path -LiteralPath ".\.args_engine_repo")) { $summary.reason_code="INFRA_ENGINE_GUARD_MISSING"; throw "Missing .args_engine_repo" }
  if (Test-Path -LiteralPath ".\args\control\stop.flag") { $summary.reason_code="INFRA_HALT_STOP_FLAG"; throw "HALT: stop.flag present" }

  if ([string]::IsNullOrWhiteSpace($TargetRunId)) { $summary.reason_code="MISSING_TARGET_RUN_ID"; $finalRc=$RC_FAIL; throw "TargetRunId required" }
  if (-not (Test-Path -LiteralPath $VaultRepo)) { $summary.reason_code="INFRA_VAULT_REPO_MISSING"; throw "VaultRepo missing" }
  if (-not (Test-Path -LiteralPath $GuardianExtraArgsJson)) { $summary.reason_code="INFRA_GUARDIAN_CONFIG_MISSING"; throw "GuardianExtraArgsJson missing" }
  if (-not (Test-Path -LiteralPath $GovernorExtraArgsJson)) { $summary.reason_code="INFRA_GOVERNOR_CONFIG_MISSING"; throw "GovernorExtraArgsJson missing" }

  $smokeRoot = ".\args\data\smoke\build_release_no_llm_smoke_v0"
  $targetDir = Join-Path $smokeRoot $TargetRunId
  $bundlePath = Join-Path $targetDir "bundle.zip"
  $requestPath = Join-Path $targetDir "job_request_v1.json"
  if (-not (Test-Path -LiteralPath $targetDir)) { $summary.reason_code="INFRA_TARGET_RUN_DIR_MISSING"; throw "Target run dir missing" }
  if (-not (Test-Path -LiteralPath $bundlePath)) { $summary.reason_code="INFRA_BUNDLE_MISSING"; throw "bundle.zip missing" }
  if (-not (Test-Path -LiteralPath $requestPath)) { $summary.reason_code="INFRA_REQUEST_MISSING"; throw "job_request_v1.json missing" }

  # Governor policy path (guarded JSON)
  $govExtraText = ReadText (Resolve-Path -LiteralPath $GovernorExtraArgsJson).Path
  try { $govExtraObj = $govExtraText | ConvertFrom-Json } catch {
    $summary.reason_code="INFRA_GOVERNOR_EXTRA_ARGS_BAD_JSON"
    throw "GovernorExtraArgsJson is not valid JSON"
  }
  $govPolicyPath = $govExtraObj.PolicyPath
  if ([string]::IsNullOrWhiteSpace($govPolicyPath) -or (-not (Test-Path -LiteralPath $govPolicyPath))) {
    $summary.reason_code="INFRA_GOVERNOR_POLICY_MISSING"
    throw "Governor PolicyPath missing/invalid"
  }

  # Chain gate PASS required
  $chainScript = ".\scripts\gate_release_chain_v0.ps1"
  if (-not (Test-Path -LiteralPath $chainScript)) { $summary.reason_code="INFRA_CHAIN_SCRIPT_MISSING"; throw "gate_release_chain_v0.ps1 missing" }

  $stepChain = Join-Path $outDirFull "step_chain"
  Ensure-Dir $stepChain

  $govRuntime = Join-Path $stepChain "governor_extra_args_runtime.json"
  @{
    BundlePath = (Resolve-Path -LiteralPath $bundlePath).Path
    PolicyPath = (Resolve-Path -LiteralPath $govPolicyPath).Path
  } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $govRuntime -Encoding utf8

  $chainArgs = @(
    "-RunId", ($RunId + "__chain"),
    "-OutDir", $stepChain,
    "-VaultRepo", $VaultRepo,
    "-TargetRunId", $TargetRunId,
    "-RunAcceptance", $RunAcceptance,
    "-GuardianExtraArgsJson", (Resolve-Path -LiteralPath $GuardianExtraArgsJson).Path,
    "-GovernorExtraArgsJson", (Resolve-Path -LiteralPath $govRuntime).Path,
    "-ChaosCorruptZipBeforeVerify", $ChainChaosCorruptZipBeforeVerify
  )

  $chain = Run-PowershellFile -StepDir $stepChain -ScriptPath (Resolve-Path -LiteralPath $chainScript).Path -ScriptArgs $chainArgs -WD $repoPath
  $summary.steps += @{
    step="chain_gate"
    out_dir=$stepChain
    exit_code=$chain.exit_code
    result=(RcToResult $chain.exit_code)
    child_reason_code=$chain.child_reason_code
    stdout_path=$chain.stdout_path
    stderr_path=$chain.stderr_path
    child_summary_path=$chain.child_summary_path
  }

  if ($chain.exit_code -ne 0) {
    $summary.reason_code = "DENY_GATES"
    if ($chain.child_reason_code) { $summary.reason_code = $summary.reason_code + "|" + $chain.child_reason_code }
    $finalRc = $chain.exit_code
    throw "chain denied"
  }

  # Backup control plane and enable export only after chain PASS
  $cpPath = (Resolve-Path -LiteralPath $ControlPlane).Path
  $cpBackup = TempFile "control_plane_backup"
  $cpRaw = ReadText $cpPath
  WriteText $cpBackup $cpRaw

  try { $cpObj = $cpRaw | ConvertFrom-Json } catch {
    $summary.reason_code="INFRA_CONTROL_PLANE_BAD_JSON"
    throw "control_plane.json is not valid JSON"
  }

  if ($null -eq $cpObj.engine) { $cpObj | Add-Member -NotePropertyName engine -NotePropertyValue (@{}) }
  if ($null -eq $cpObj.engine.permissions) { $cpObj.engine | Add-Member -NotePropertyName permissions -NotePropertyValue (@{}) }
  $cpObj.engine.permissions.ALLOW_EXPORT = $true
  WriteText $cpPath (($cpObj | ConvertTo-Json -Depth 50))

  # Foundry steps
  $stepGate = Join-Path $outDirFull "step_gate"
  $gate = Run-PythonModule -StepDir $stepGate -PythonCmd $Python -ModuleName "args.foundry.gate_v0" -ModuleArgs @("--control-plane", (".\" + $ControlPlane)) -WD $repoPath
  $summary.steps += @{
    step="gate_v0"
    out_dir=$stepGate
    exit_code=$gate.exit_code
    result=(RcToResult $gate.exit_code)
    child_reason_code=$gate.child_reason_code
    stdout_path=$gate.stdout_path
    stderr_path=$gate.stderr_path
  }
  if ($gate.exit_code -ne 0) { $summary.reason_code="DENY_GATE_V0"; $finalRc=$gate.exit_code; throw "gate_v0 failed" }

  $stepPlan = Join-Path $outDirFull "step_plan"
  $plan = Run-PythonModule -StepDir $stepPlan -PythonCmd $Python -ModuleName "args.foundry.plan_v0" -ModuleArgs @("--control-plane", (".\" + $ControlPlane), "--product", $Product, "--factory", $Factory) -WD $repoPath
  $summary.steps += @{
    step="plan_v0"
    out_dir=$stepPlan
    exit_code=$plan.exit_code
    result=(RcToResult $plan.exit_code)
    child_reason_code=$plan.child_reason_code
    stdout_path=$plan.stdout_path
    stderr_path=$plan.stderr_path
  }
  if ($plan.exit_code -ne 0) { $summary.reason_code="DENY_PLAN_V0"; $finalRc=$plan.exit_code; throw "plan_v0 failed" }

  $stepRelease = Join-Path $outDirFull "step_release"
  $relArgs = @("--control-plane", (".\" + $ControlPlane), "--product", $Product, "--factory", $Factory)
  if (-not [string]::IsNullOrWhiteSpace($ReleaseId)) { $relArgs += @("--release-id", $ReleaseId) }

  $rel = Run-PythonModule -StepDir $stepRelease -PythonCmd $Python -ModuleName "args.foundry.release_v0" -ModuleArgs $relArgs -WD $repoPath
  $summary.steps += @{
    step="release_v0"
    out_dir=$stepRelease
    exit_code=$rel.exit_code
    result=(RcToResult $rel.exit_code)
    child_reason_code=$rel.child_reason_code
    stdout_path=$rel.stdout_path
    stderr_path=$rel.stderr_path
  }
  if ($rel.exit_code -ne 0) { $summary.reason_code="DENY_RELEASE_V0"; $finalRc=$rel.exit_code; throw "release_v0 failed" }

  if ($null -ne $rel.stdout_json) {
    if ($rel.stdout_json.PSObject.Properties.Name -contains "release_id") { $summary.release_id = $rel.stdout_json.release_id }
    if ($rel.stdout_json.PSObject.Properties.Name -contains "zip_path")   { $summary.release_zip_path = $rel.stdout_json.zip_path }
    if ($rel.stdout_json.PSObject.Properties.Name -contains "hashes_path"){ $summary.release_hashes_path = $rel.stdout_json.hashes_path }
  }

  $summary.reason_code = "PASS"
  $finalRc = $RC_OK
}
catch {
  $summary.error_type = $_.Exception.GetType().FullName
  $summary.error_message = $_.Exception.Message
  if ($summary.reason_code -eq "INIT") { $summary.reason_code = "INFRA_EXCEPTION" }
  if ($finalRc -ne $RC_OK -and $finalRc -ne $RC_FAIL -and $finalRc -ne $RC_INFRA) { $finalRc = $RC_INFRA }
}
finally {
  try {
    if ($null -ne $cpPath -and $null -ne $cpBackup -and (Test-Path -LiteralPath $cpBackup)) {
      $orig = ReadText $cpBackup
      WriteText $cpPath $orig
      Remove-Item -Force -LiteralPath $cpBackup -ErrorAction SilentlyContinue
    }
  } catch {
    if ($finalRc -eq $RC_OK) {
      $finalRc = $RC_INFRA
      $summary.reason_code = "INFRA_CONTROL_PLANE_RESTORE_FAILED"
      $summary.error_type = $_.Exception.GetType().FullName
      $summary.error_message = $_.Exception.Message
    }
  }

  $summary.exit_code = $finalRc
  $summary.ok = ($finalRc -eq 0)
  $summary.result = (RcToResult $finalRc)
  $summary.ts_utc_end = (UtcNowIso)

  $json = ($summary | ConvertTo-Json -Compress -Depth 25)
  try { Set-Content -LiteralPath $summaryPath -Value $json -Encoding utf8 -ErrorAction Stop } catch { }

  Write-Output $json
  exit $finalRc
}
