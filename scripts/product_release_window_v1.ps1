param(
  [Parameter(Mandatory=$true)][string]$ProductId,
  [Parameter(Mandatory=$true)][string]$TargetRunId,
  [Parameter(Mandatory=$false)][string]$RunId = "",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "NO",

  # Optional overrides (if empty -> take from product_config_v1.json)
  [Parameter(Mandatory=$false)][string]$VaultRepo = "",
  [Parameter(Mandatory=$false)][string]$GuardianRepo = "",
  [Parameter(Mandatory=$false)][string]$GovernorRepo = "",

  # Optional pass-through reserved (do not use $args name)
  [Parameter(Mandatory=$false)][string]$ScriptArgs = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Exit codes
$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) }
function RandHex([int]$n) { -join (1..$n | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) }) }

function Ensure-Dir([string]$p) {
  if ([string]::IsNullOrWhiteSpace($p)) { return }
  New-Item -ItemType Directory -Force -Path $p | Out-Null
}

function Normalize-Rc([int]$rc) {
  if ($rc -eq 0) { return 0 }
  if ($rc -eq 1) { return 1 }
  if ($rc -eq 2) { return 2 }
  return 2
}

function ReadTextAutoBom([string]$path) {
  if (!(Test-Path -LiteralPath $path)) { throw "Missing file: $path" }
  $sr = New-Object System.IO.StreamReader($path, [System.Text.Encoding]::UTF8, $true)
  try { return $sr.ReadToEnd() } finally { $sr.Close() }
}

function ReadJsonFile([string]$path) {
  $raw = ReadTextAutoBom $path
  if ([string]::IsNullOrWhiteSpace($raw)) { throw "Empty JSON file: $path" }
  return ($raw | ConvertFrom-Json)
}

function WriteTextUtf8NoBom([string]$path, [string]$text) {
  $dir = Split-Path -Parent $path
  Ensure-Dir $dir
  $norm = ($text -replace "`r`n","`n")
  [System.IO.File]::WriteAllText($path, $norm, $Utf8NoBom)
}

function WriteJsonFile([string]$path, $obj) {
  $json = ($obj | ConvertTo-Json -Depth 40 -Compress)
  WriteTextUtf8NoBom $path $json
}

$ts = UtcNowIso
$runIdEff = $RunId
if ([string]::IsNullOrWhiteSpace($runIdEff)) {
  $runIdEff = ("PROD_" + $ProductId + "_" + (UtcNowId) + "_" + (RandHex 6))
}

$repoRoot = (Resolve-Path -LiteralPath (Get-Location).Path).Path

$manifestPath = Join-Path $repoRoot ("manifests\products\" + $ProductId + ".json")
$configPath   = Join-Path $repoRoot ("args\configs\" + $ProductId + "\product_config_v1.json")

# fallback out_dir so we ALWAYS have a place to write summary even on early INFRA
$fallbackOutRoot = Join-Path $repoRoot "_out\product_release_window_v1"
$fallbackRunOut  = Join-Path $fallbackOutRoot $runIdEff

$summary = [ordered]@{
  schema = "product_release_window_v1"
  ts_utc = $ts
  ok = $false
  exit_code = $RC_INFRA
  reason_code = "INFRA_INIT"
  child_reason_code = "INFRA_INIT"

  repo = $repoRoot
  product_id = $ProductId
  run_id = $runIdEff
  target_run_id = $TargetRunId
  out_dir = $null

  manifest_path = $manifestPath
  config_path = $configPath

  child = [ordered]@{
    script = "scripts/factory_release_window_v1.ps1"
    out_dir = $null
    exit_code_raw = $null
    exit_code = $null
    reason_code = $null
    stdout_path = $null
    stderr_path = $null
    summary_path = $null
  }
}

function Finalize-And-Exit([int]$rc, [string]$reason) {
  $rc = Normalize-Rc $rc
  $summary.exit_code = $rc
  $summary.ok = ($rc -eq 0)
  $summary.reason_code = $reason
  $summary.child_reason_code = $summary.child.reason_code

  # Ensure out_dir exists
  if ([string]::IsNullOrWhiteSpace([string]$summary.out_dir)) {
    Ensure-Dir $fallbackRunOut
    $summary.out_dir = $fallbackRunOut
  } else {
    Ensure-Dir $summary.out_dir
  }

  $sumPath = Join-Path $summary.out_dir "summary.json"
  WriteJsonFile $sumPath $summary

  Write-Output (($summary | ConvertTo-Json -Depth 40 -Compress))
  exit $rc
}

try {
  $manifest = ReadJsonFile $manifestPath
  $config   = ReadJsonFile $configPath

  $outRoot = [string]$config.out_root
  if ([string]::IsNullOrWhiteSpace($outRoot)) { $outRoot = [string]$manifest.default_out_root }
  if ([string]::IsNullOrWhiteSpace($outRoot)) {
    # still write summary (fallback) with INFRA
    $summary.child.reason_code = "INFRA_OUT_ROOT_MISSING"
    Finalize-And-Exit 2 "INFRA_OUT_ROOT_MISSING"
  }

  $outDir = Join-Path $repoRoot $outRoot
  Ensure-Dir $outDir

  $runOutDir = Join-Path $outDir $runIdEff
  Ensure-Dir $runOutDir
  $summary.out_dir = $runOutDir

  # Resolve repos (params override config)
  $vault = $VaultRepo; $guard = $GuardianRepo; $gov = $GovernorRepo
  if ([string]::IsNullOrWhiteSpace($vault)) { $vault = [string]$config.repos.vault_repo }
  if ([string]::IsNullOrWhiteSpace($guard)) { $guard = [string]$config.repos.guardian_repo }
  if ([string]::IsNullOrWhiteSpace($gov))   { $gov   = [string]$config.repos.governor_repo }

  # Child step folder
  $stepDir = Join-Path $runOutDir "step_release_window"
  Ensure-Dir $stepDir

  $childOutDir = Join-Path $stepDir "child_out"
  Ensure-Dir $childOutDir

  $evidenceDir = Join-Path $stepDir "evidence"
  Ensure-Dir $evidenceDir

  $childStdout = Join-Path $evidenceDir "child.stdout.txt"
  $childStderr = Join-Path $evidenceDir "child.stderr.txt"
  $childCmd    = Join-Path $evidenceDir "child.cmdline.txt"

  $childScript = Join-Path $repoRoot "scripts\factory_release_window_v1.ps1"

  $summary.child.out_dir = $childOutDir
  $summary.child.stdout_path = $childStdout
  $summary.child.stderr_path = $childStderr

  if (-not (Test-Path -LiteralPath $childScript)) {
    $summary.child.reason_code = "INFRA_CHILD_SCRIPT_MISSING"
    Finalize-And-Exit 2 "INFRA_CHILD_SCRIPT_MISSING"
  }

  # Build child argument list (non-bypass child is the source of truth)
  $childArgs = @(
    "-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
    "-File", $childScript,
    "-OutDir", $childOutDir,
    "-RunAcceptance", $RunAcceptance,
    "-TargetRunId", $TargetRunId
  )

  if (![string]::IsNullOrWhiteSpace($vault)) { $childArgs += @("-VaultRepo",$vault) }
  if (![string]::IsNullOrWhiteSpace($guard)) { $childArgs += @("-GuardianRepo",$guard) }
  if (![string]::IsNullOrWhiteSpace($gov)) { $childArgs += @("-GovernorRepo",$gov) }
  if (![string]::IsNullOrWhiteSpace($ScriptArgs)) { $childArgs += @("-ScriptArgs",$ScriptArgs) }

  WriteTextUtf8NoBom $childCmd ("powershell " + ($childArgs -join " "))

  # Run child (working directory = repo root)
  $p = Start-Process -FilePath "powershell" -ArgumentList $childArgs -WorkingDirectory $repoRoot -NoNewWindow -Wait -PassThru `
      -RedirectStandardOutput $childStdout -RedirectStandardError $childStderr

  $childRcRaw  = [int]$p.ExitCode
  $childRcNorm = Normalize-Rc $childRcRaw

  $summary.child.exit_code_raw = $childRcRaw
  $summary.child.exit_code = $childRcNorm

  # Child summary.json expected
  $childSummaryPath = Join-Path $childOutDir "summary.json"
  $summary.child.summary_path = $childSummaryPath

  if (!(Test-Path -LiteralPath $childSummaryPath)) {
    $summary.child.reason_code = "INFRA_CHILD_NO_SUMMARY"
    Finalize-And-Exit 2 "INFRA_CHILD_NO_SUMMARY"
  }

  $childSummary = ReadJsonFile $childSummaryPath
  $childReason = [string]$childSummary.reason_code
  if ([string]::IsNullOrWhiteSpace($childReason)) {
    $childReason = $(if ($childRcNorm -eq 1) { "FAIL_CHILD" } elseif ($childRcNorm -eq 2) { "INFRA_CHILD" } else { "OK" })
  }

  $summary.child.reason_code = $childReason
  $summary.child_reason_code = $childReason

  # Final top-level reason_code:
  $finalReason = $childReason
  if ($childRcNorm -eq 0) { $finalReason = "OK" }

  Finalize-And-Exit $childRcNorm $finalReason
}
catch {
  $summary.ok = $false
  $summary.exit_code = $RC_INFRA
  $summary.reason_code = "INFRA_PRODUCT_WINDOW_EXCEPTION"
  $summary.child_reason_code = "INFRA_PRODUCT_WINDOW_EXCEPTION"
  $summary.error = [ordered]@{ message = $_.Exception.Message }

  # Ensure out_dir exists (fallback if needed)
  if ([string]::IsNullOrWhiteSpace([string]$summary.out_dir)) {
    Ensure-Dir $fallbackRunOut
    $summary.out_dir = $fallbackRunOut
  } else {
    Ensure-Dir $summary.out_dir
  }

  WriteJsonFile (Join-Path $summary.out_dir "summary.json") $summary
  Write-Output (($summary | ConvertTo-Json -Depth 40 -Compress))
  exit $RC_INFRA
}
