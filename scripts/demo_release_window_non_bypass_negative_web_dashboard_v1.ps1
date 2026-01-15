param(
  [string]$Repo = ".",
  [ValidateSet("YES","NO")][string]$RunAcceptance = "NO",

  [string]$TargetRunId = "",

  [string]$ReleaseWindowScript = ".\scripts\factory_release_window_v1.ps1",
  [string]$VaultRepo = "C:\Users\mukol\ARGS-Evidence-Vault-v0",
  [string]$GuardianExtraArgsJson = ".\args\data\smoke\gate_release_chain_v0\guardian_extra_args_v0.json",
  [string]$GovernorExtraArgsJson = ".\args\data\smoke\gate_release_chain_v0\governor_extra_args_v0.json",

  [string]$OutDir = "",
  [string]$RunId = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ")) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")) }
function Ensure-Dir([string]$p) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
function ReadText([string]$p) { Get-Content -LiteralPath $p -Raw -ErrorAction Stop }
function TryParseJson([string]$raw) { if ([string]::IsNullOrWhiteSpace($raw)) { return $null }; try { return ($raw | ConvertFrom-Json) } catch { return $null } }

$repoPath = (Resolve-Path -LiteralPath $Repo).Path
Set-Location -LiteralPath $repoPath

$ts0 = UtcNowIso
if ([string]::IsNullOrWhiteSpace($RunId)) { $RunId = "B07_NEG_" + (UtcNowId) }
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = ".\args\\data\\products\\cicd_release_pack_v0\\anti_bypass_negative_v1\$RunId" }
Ensure-Dir $OutDir
$outDirFull = (Resolve-Path -LiteralPath $OutDir).Path
$summaryPath = Join-Path $outDirFull "summary.json"

$summary = @{
  schema="demo_release_window_non_bypass_negative_v0"
  ts_utc=$ts0
  ts_utc_end=""
  run_id=$RunId
  repo=$repoPath
  out_dir=$outDirFull
  ok=$false
  result="INFRA"
  reason_code="INIT"
  exit_code=$RC_INFRA

  target_run_id=$TargetRunId
  release_window_script=$ReleaseWindowScript

  baseline_release_count=0
  after_release_count=0
  added_files=@()
  removed_files=@()

  release_window_exit_code=$null
  release_window_reason_code=$null
  release_window_out_dir=$null
  release_window_stdout_path=$null
  release_window_stderr_path=$null
}

$finalRc = $RC_INFRA

try {
  if ($RunAcceptance -ne "YES") { $summary.reason_code="RUN_ACCEPTANCE_REQUIRED"; $finalRc=$RC_FAIL; throw "RunAcceptance must be YES" }

  if (-not (Test-Path -LiteralPath $ReleaseWindowScript)) { $summary.reason_code="INFRA_RELEASE_WINDOW_SCRIPT_MISSING"; throw "release window script missing" }
  if (-not (Test-Path -LiteralPath $VaultRepo)) { $summary.reason_code="INFRA_VAULT_REPO_MISSING"; throw "VaultRepo missing" }
  if (-not (Test-Path -LiteralPath $GuardianExtraArgsJson)) { $summary.reason_code="INFRA_GUARDIAN_CONFIG_MISSING"; throw "GuardianExtraArgsJson missing" }
  if (-not (Test-Path -LiteralPath $GovernorExtraArgsJson)) { $summary.reason_code="INFRA_GOVERNOR_CONFIG_MISSING"; throw "GovernorExtraArgsJson missing" }

  $releasesDir = ".\dist\releases"
  Ensure-Dir $releasesDir

  # Determine target run id if not provided
  if ([string]::IsNullOrWhiteSpace($TargetRunId)) {
    $root = ".\args\data\smoke\build_release_no_llm_smoke_v0"
    if (-not (Test-Path $root)) { $summary.reason_code="INFRA_SMOKE_ROOT_MISSING"; throw "smoke root missing" }
    $TargetRunId = (Get-ChildItem $root -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1).Name
    if ([string]::IsNullOrWhiteSpace($TargetRunId)) { $summary.reason_code="INFRA_NO_TARGET_RUN"; throw "no target run found" }
    $summary.target_run_id = $TargetRunId
  }

  # Baseline
  $before = Get-ChildItem $releasesDir -File | Sort-Object Name | ForEach-Object { $_.Name }
  $summary.baseline_release_count = ($before | Measure-Object).Count

  # Run release window in DENY mode (vault chaos)
  $rwOutDir = Join-Path $outDirFull "step_release_window"
  Ensure-Dir $rwOutDir

  $rwStdout = Join-Path $rwOutDir "stdout.txt"
  $rwStderr = Join-Path $rwOutDir "stderr.txt"

  $psArgs = @(
    "-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
    "-File",(Resolve-Path -LiteralPath $ReleaseWindowScript).Path,
    "-RunAcceptance","YES",
    "-TargetRunId",$TargetRunId,
    "-VaultRepo",$VaultRepo,
    "-GuardianExtraArgsJson",$GuardianExtraArgsJson,
    "-GovernorExtraArgsJson",$GovernorExtraArgsJson,
    "-ChainChaosCorruptZipBeforeVerify","YES",
    "-OutDir",$rwOutDir,
    "-RunId",("RW_DENY_" + (UtcNowId))
  )

  $p = Start-Process -FilePath "powershell" -ArgumentList $psArgs -WorkingDirectory $repoPath `
    -NoNewWindow -Wait -PassThru -RedirectStandardOutput $rwStdout -RedirectStandardError $rwStderr

  $rwRc = [int]$p.ExitCode
  $summary.release_window_exit_code = $rwRc
  $summary.release_window_out_dir = $rwOutDir
  $summary.release_window_stdout_path = $rwStdout
  $summary.release_window_stderr_path = $rwStderr

  $rwJson = $null
  if (Test-Path -LiteralPath $rwStdout) { $rwJson = TryParseJson (ReadText $rwStdout) }
  if ($null -ne $rwJson -and ($rwJson.PSObject.Properties.Name -contains "reason_code")) {
    $summary.release_window_reason_code = $rwJson.reason_code
  }

  # After snapshot
  $after = Get-ChildItem $releasesDir -File | Sort-Object Name | ForEach-Object { $_.Name }
  $summary.after_release_count = ($after | Measure-Object).Count

  $diffAdd = Compare-Object $before $after | Where-Object { $_.SideIndicator -eq "=>" } | Select-Object -ExpandProperty InputObject
  $diffRem = Compare-Object $before $after | Where-Object { $_.SideIndicator -eq "<=" } | Select-Object -ExpandProperty InputObject
  $summary.added_files = @($diffAdd)
  $summary.removed_files = @($diffRem)

  # Expectations:
  # - release window must FAIL (exit_code=1) with DENY_GATES...
  # - dist\releases must not change
  if ($rwRc -ne 1) {
    $summary.reason_code = "FAIL_UNEXPECTED_RELEASE_WINDOW_RC"
    $finalRc = $RC_FAIL
    throw "release window rc expected 1, got $rwRc"
  }

  if ($summary.release_window_reason_code -notlike "DENY_GATES*") {
    $summary.reason_code = "FAIL_UNEXPECTED_REASON_CODE"
    $finalRc = $RC_FAIL
    throw "expected reason_code starting with DENY_GATES"
  }

  if (($summary.added_files.Count -gt 0) -or ($summary.removed_files.Count -gt 0)) {
    $summary.reason_code = "FAIL_RELEASES_CHANGED"
    $finalRc = $RC_FAIL
    throw "dist/releases changed during DENY run"
  }

  $summary.reason_code = "PASS"
  $finalRc = $RC_OK
}
catch {
  if ($finalRc -ne $RC_OK -and $finalRc -ne $RC_FAIL -and $finalRc -ne $RC_INFRA) { $finalRc = $RC_INFRA }
  if ($summary.reason_code -eq "INIT") { $summary.reason_code = "INFRA_EXCEPTION" }
  $summary.error_message = $_.Exception.Message
}
finally {
  $summary.exit_code = $finalRc
  $summary.ok = ($finalRc -eq 0)
  if ($finalRc -eq 0) { $summary.result = "PASS" } elseif ($finalRc -eq 1) { $summary.result = "FAIL" } else { $summary.result = "INFRA" }
  $summary.ts_utc_end = (UtcNowIso)

  $json = ($summary | ConvertTo-Json -Compress -Depth 25)
  try { Set-Content -LiteralPath $summaryPath -Value $json -Encoding utf8 -ErrorAction Stop } catch { }

  Write-Output $json
  exit $finalRc
}


