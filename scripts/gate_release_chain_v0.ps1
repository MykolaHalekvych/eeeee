param(
  [Parameter(Mandatory=$true)][string]$RunId,
  [Parameter(Mandatory=$true)][string]$OutDir,
  [Parameter(Mandatory=$true)][string]$TargetRunId,
  [Parameter(Mandatory=$true)][string]$VaultRepo,

  [Parameter(Mandatory=$false)][string]$GuardianRepo = "C:\Users\mukol\ARGS-Guardian-v0",
  [Parameter(Mandatory=$false)][string]$GovernorRepo = "C:\Users\mukol\ARGS-Release-Governor-v0",

  # Optional overrides
  [Parameter(Mandatory=$false)][string]$GuardianRequestPath = "",

  # Optional JSON with extra args for each child gate
  [Parameter(Mandatory=$false)][string]$GuardianExtraArgsJson = "",
  [Parameter(Mandatory=$false)][string]$GovernorExtraArgsJson = "",
  [Parameter(Mandatory=$false)][string]$VaultExtraArgsJson = "",

  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "NO",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$ChaosCorruptZipBeforeVerify = "NO"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")) }
function Ensure-Dir([string]$Path) { New-Item -ItemType Directory -Force -Path $Path | Out-Null }
function To-FullPath([string]$Path) { return [System.IO.Path]::GetFullPath($Path) }

function Read-JsonOrNull([string]$Path) {
  if (-not (Test-Path $Path)) { return $null }
  $txt = Get-Content $Path -Raw
  if ([string]::IsNullOrWhiteSpace($txt)) { return $null }
  try { return ($txt | ConvertFrom-Json) } catch { return $null }
}

function Load-ExtraArgs([string]$JsonPath) {
  if ([string]::IsNullOrWhiteSpace($JsonPath)) { return @{} }
  if (-not (Test-Path $JsonPath)) { throw "ExtraArgs JSON not found: $JsonPath" }
  $obj = (Get-Content $JsonPath -Raw) | ConvertFrom-Json
  if ($null -eq $obj) { return @{} }
  $h = @{}
  foreach ($p in $obj.PSObject.Properties) { $h[$p.Name] = $p.Value }
  return $h
}

function Filter-ArgsForScript([string]$ScriptPath, [hashtable]$ArgHash) {
  $cmd = Get-Command $ScriptPath -ErrorAction Stop
  $supported = $cmd.Parameters.Keys
  $filtered = @{}
  foreach ($k in $ArgHash.Keys) {
    if ($supported -contains $k) {
      $v = $ArgHash[$k]
      if ($null -ne $v -and "$v" -ne "") { $filtered[$k] = $v }
    }
  }
  return $filtered
}

function Build-ArgList([string]$ScriptPath, [hashtable]$ArgHash) {
  # IMPORTANT: -NonInteractive to prevent mandatory-parameter prompts
  $alist = @("-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",$ScriptPath)
  foreach ($k in $ArgHash.Keys) { $alist += @("-$k", "$($ArgHash[$k])") }
  return $alist
}

function Find-RequestInTargetDir([string]$TargetRunDir) {
  if (-not (Test-Path $TargetRunDir)) { return "" }

  $candidates = @()

  $p1 = Get-ChildItem $TargetRunDir -Recurse -File -Filter "job_request_v1.json" -ErrorAction SilentlyContinue
  if ($p1) { $candidates += $p1 }

  $p2 = Get-ChildItem $TargetRunDir -Recurse -File -Filter "job_request*.json" -ErrorAction SilentlyContinue
  if ($p2) { $candidates += $p2 }

  $p3 = Get-ChildItem $TargetRunDir -Recurse -File -Filter "*request*.json" -ErrorAction SilentlyContinue
  if ($p3) { $candidates += $p3 }

  if (-not $candidates -or $candidates.Count -eq 0) { return "" }

  # Prefer shortest path (usually the intended top-level request artifact)
  $best = $candidates | Sort-Object { $_.FullName.Length } | Select-Object -First 1
  return $best.FullName
}

function Invoke-ChildGate(
  [string]$StepName,
  [string]$ScriptName,
  [hashtable]$BaseArgs,
  [string]$ExtraArgsJsonPath,
  [string]$ParentOutDirFull
) {
  $stepOut = Join-Path $ParentOutDirFull ("step_" + $StepName)
  Ensure-Dir $stepOut

  $scriptPath = Join-Path $PSScriptRoot $ScriptName
  if (-not (Test-Path $scriptPath)) {
    return @{
      step = $StepName
      script = $scriptPath
      out_dir = $stepOut
      exit_code = $RC_INFRA
      result = "INFRA"
      reason_code = "SCRIPT_NOT_FOUND"
      ts_utc = (UtcNowIso)
      summary_path = (Join-Path $stepOut "summary.json")
    }
  }

  # Force standard contract args into child gate
  $BaseArgs["OutDir"] = $stepOut

  $extra = Load-ExtraArgs $ExtraArgsJsonPath
  foreach ($k in $extra.Keys) { $BaseArgs[$k] = $extra[$k] }

  $filtered = Filter-ArgsForScript $scriptPath $BaseArgs
  $argList  = Build-ArgList $scriptPath $filtered

  $stdoutPath = Join-Path $stepOut "child_stdout.txt"
  $stderrPath = Join-Path $stepOut "child_stderr.txt"

  $proc = Start-Process -FilePath "powershell" -ArgumentList $argList -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

  $rc  = [int]$proc.ExitCode
  $res = if ($rc -eq 0) { "PASS" } elseif ($rc -eq 1) { "FAIL" } else { "INFRA" }

  $childSummaryPath = Join-Path $stepOut "summary.json"
  $childSummary = Read-JsonOrNull $childSummaryPath

  $childReason = $null
  if ($null -ne $childSummary -and ($childSummary.PSObject.Properties.Name -contains "reason_code")) {
    $childReason = $childSummary.reason_code
  }

  return @{
    step = $StepName
    script = $scriptPath
    out_dir = $stepOut
    exit_code = $rc
    result = $res
    child_reason_code = $childReason
    summary_path = $childSummaryPath
    child_stdout = $stdoutPath
    child_stderr = $stderrPath
  }
}

$OutDirFull = To-FullPath $OutDir
Ensure-Dir $OutDirFull
$summaryPath = Join-Path $OutDirFull "summary.json"

$repoRoot = To-FullPath (Join-Path $PSScriptRoot "..")
$smokeRoot = Join-Path $repoRoot "args\data\smoke\build_release_no_llm_smoke_v0"
$targetRunDir = Join-Path $smokeRoot $TargetRunId

try {
  # Resolve guardian request path (auto)
  $resolvedGuardianRequest = $GuardianRequestPath
  if ([string]::IsNullOrWhiteSpace($resolvedGuardianRequest)) {
    $resolvedGuardianRequest = Find-RequestInTargetDir $targetRunDir
  }

  $steps = @()

  # guardian
  if ([string]::IsNullOrWhiteSpace($resolvedGuardianRequest)) {
    $steps += @{
      step="guardian"; script=(Join-Path $PSScriptRoot "gate_guardian_v0.ps1"); out_dir=(Join-Path $OutDirFull "step_guardian");
      exit_code=$RC_INFRA; result="INFRA"; child_reason_code="REQUEST_PATH_MISSING";
      summary_path=(Join-Path $OutDirFull "step_guardian\summary.json")
    }
  } else {
    $guardianArgs = @{
      RunId = ($RunId + "__guardian")
      TargetRunId = $TargetRunId
      RunAcceptance = $RunAcceptance
      GuardianRepo = $GuardianRepo
      RequestPath = $resolvedGuardianRequest
    }
    $steps += Invoke-ChildGate "guardian" "gate_guardian_v0.ps1" $guardianArgs $GuardianExtraArgsJson $OutDirFull
  }

  # governor
  $governorArgs = @{
    RunId = ($RunId + "__governor")
    TargetRunId = $TargetRunId
    RunAcceptance = $RunAcceptance
    GovernorRepo = $GovernorRepo
  }
  $steps += Invoke-ChildGate "governor" "gate_governor_v0.ps1" $governorArgs $GovernorExtraArgsJson $OutDirFull

  # vault
  $vaultArgs = @{
    RunId = ($RunId + "__vault")
    TargetRunId = $TargetRunId
    VaultRepo = $VaultRepo
    RunAcceptance = $RunAcceptance
    ChaosCorruptZipBeforeVerify = $ChaosCorruptZipBeforeVerify
  }
  $steps += Invoke-ChildGate "vault" "gate_vault_export_verify_v0.ps1" $vaultArgs $VaultExtraArgsJson $OutDirFull

  $overall = $RC_OK
  if (($steps | Where-Object { $_.exit_code -eq $RC_INFRA } | Measure-Object).Count -gt 0) {
    $overall = $RC_INFRA
  } elseif (($steps | Where-Object { $_.exit_code -eq $RC_FAIL } | Measure-Object).Count -gt 0) {
    $overall = $RC_FAIL
  }

  $overallResult = if ($overall -eq 0) { "PASS" } elseif ($overall -eq 1) { "FAIL" } else { "INFRA" }

  $reason = "PASS"
  if ($overall -eq $RC_INFRA) {
    $infra = $steps | Where-Object { $_.exit_code -eq $RC_INFRA } | Select-Object -First 1
    $reason = "INFRA_" + ($infra.step.ToUpper())
    if ($infra.child_reason_code) { $reason = $reason + "|" + $infra.child_reason_code }
  } elseif ($overall -eq $RC_FAIL) {
    $fail = $steps | Where-Object { $_.exit_code -eq $RC_FAIL } | Select-Object -First 1
    $reason = "DENY_" + ($fail.step.ToUpper())
    if ($fail.child_reason_code) { $reason = $reason + "|" + $fail.child_reason_code }
  }

  $summary = @{
    schema = "gate_release_chain_v0"
    ts_utc = (UtcNowIso)
    run_id = $RunId
    target_run_id = $TargetRunId
    target_run_dir = $targetRunDir
    out_dir = $OutDirFull
    vault_repo = $VaultRepo
    guardian_repo = $GuardianRepo
    governor_repo = $GovernorRepo
    guardian_request_path = $resolvedGuardianRequest
    run_acceptance = $RunAcceptance
    chaos_corrupt_zip_before_verify = $ChaosCorruptZipBeforeVerify
    exit_code = $overall
    ok = ($overall -eq 0)
    result = $overallResult
    reason_code = $reason
    steps = $steps
  }

  $json = $summary | ConvertTo-Json -Depth 20 -Compress
  Set-Content -Path $summaryPath -Value $json -Encoding UTF8
  Write-Output $json
  exit $overall
}
catch {
  $err = ($_ | Out-String)
  $fallback = @{
    schema="gate_release_chain_v0"; ts_utc=(UtcNowIso); run_id=$RunId; target_run_id=$TargetRunId; target_run_dir=$targetRunDir;
    out_dir=$OutDirFull; vault_repo=$VaultRepo; guardian_repo=$GuardianRepo; governor_repo=$GovernorRepo;
    run_acceptance=$RunAcceptance; chaos_corrupt_zip_before_verify=$ChaosCorruptZipBeforeVerify;
    exit_code=$RC_INFRA; ok=$false; result="INFRA"; reason_code="INFRA_EXCEPTION"; error=$err
  }
  $json = $fallback | ConvertTo-Json -Depth 10 -Compress
  Set-Content -Path $summaryPath -Value $json -Encoding UTF8
  Write-Output $json
  exit $RC_INFRA
}
