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

function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) }
function RandHex([int]$n) { -join (1..$n | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) }) }

function ReadJsonFile([string]$path) {
  if (!(Test-Path -LiteralPath $path)) { throw "Missing JSON file: $path" }
  $raw = Get-Content -LiteralPath $path -Raw
  if ([string]::IsNullOrWhiteSpace($raw)) { throw "Empty JSON file: $path" }
  return ($raw | ConvertFrom-Json)
}

function WriteJsonFile([string]$path, $obj) {
  $json = ($obj | ConvertTo-Json -Depth 40 -Compress)
  Set-Content -LiteralPath $path -Value $json -Encoding UTF8
}

$ts = UtcNowIso
$runIdEff = $RunId
if ([string]::IsNullOrWhiteSpace($runIdEff)) {
  $runIdEff = ("PROD_" + $ProductId + "_" + (UtcNowId) + "_" + (RandHex 6))
}

$repoRoot = (Get-Location).Path

$manifestPath = Join-Path $repoRoot ("manifests\products\" + $ProductId + ".json")
$configPath   = Join-Path $repoRoot ("args\configs\" + $ProductId + "\product_config_v1.json")

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
  child = [ordered]@{
    script = "scripts/factory_release_window_v1.ps1"
    out_dir = $null
    exit_code = $null
    reason_code = $null
    stdout_path = $null
    stderr_path = $null
    summary_path = $null
  }
}

try {
  $manifest = ReadJsonFile $manifestPath
  $config   = ReadJsonFile $configPath

  $outRoot = $config.out_root
  if ([string]::IsNullOrWhiteSpace($outRoot)) { $outRoot = $manifest.default_out_root }
  if ([string]::IsNullOrWhiteSpace($outRoot)) { throw "INFRA: out_root missing" }

  $outDir = Join-Path $repoRoot $outRoot
  if (!(Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

  $runOutDir = Join-Path $outDir $runIdEff
  New-Item -ItemType Directory -Path $runOutDir -Force | Out-Null
  $summary.out_dir = $runOutDir

  # Resolve repos (params override config)
  $vault = $VaultRepo; $guard = $GuardianRepo; $gov = $GovernorRepo
  if ([string]::IsNullOrWhiteSpace($vault)) { $vault = $config.repos.vault_repo }
  if ([string]::IsNullOrWhiteSpace($guard)) { $guard = $config.repos.guardian_repo }
  if ([string]::IsNullOrWhiteSpace($gov))   { $gov   = $config.repos.governor_repo }

  # Child step folder
  $stepDir = Join-Path $runOutDir "step_release_window"
  New-Item -ItemType Directory -Path $stepDir -Force | Out-Null

  $childOutDir = Join-Path $stepDir "child_out"
  New-Item -ItemType Directory -Path $childOutDir -Force | Out-Null

  $childStdout = Join-Path $stepDir "child.stdout.txt"
  $childStderr = Join-Path $stepDir "child.stderr.txt"

  $childScript = Join-Path $repoRoot "scripts\factory_release_window_v1.ps1"

  $summary.child.out_dir = $childOutDir
  $summary.child.stdout_path = $childStdout
  $summary.child.stderr_path = $childStderr

  # Build child arg list (non-bypass child is the source of truth)
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

  & powershell @childArgs 1> $childStdout 2> $childStderr
  $childRc = $LASTEXITCODE

  $summary.child.exit_code = [int]$childRc
  $summary.exit_code = [int]$childRc
  $summary.ok = ($summary.exit_code -eq $RC_OK)

  $childSummaryPath = Join-Path $childOutDir "summary.json"
  $summary.child.summary_path = $childSummaryPath

  if (!(Test-Path -LiteralPath $childSummaryPath)) {
    $summary.exit_code = $RC_INFRA
    $summary.ok = $false
    $summary.reason_code = "INFRA_CHILD_NO_SUMMARY"
    $summary.child_reason_code = "INFRA_CHILD_NO_SUMMARY"
    $summary.child.reason_code = "INFRA_CHILD_NO_SUMMARY"
    WriteJsonFile (Join-Path $runOutDir "summary.json") $summary
    Write-Output (($summary | ConvertTo-Json -Depth 40 -Compress))
    exit $summary.exit_code
  }

  $childSummary = ReadJsonFile $childSummaryPath
  $childReason = $childSummary.reason_code
  if ([string]::IsNullOrWhiteSpace($childReason)) { $childReason = "UNKNOWN_CHILD_REASON" }

  $summary.child.reason_code = $childReason
  $summary.child_reason_code = $childReason
  $summary.reason_code = $childReason

  WriteJsonFile (Join-Path $runOutDir "summary.json") $summary
  Write-Output (($summary | ConvertTo-Json -Depth 40 -Compress))
  exit $summary.exit_code
}
catch {
  $summary.exit_code = $RC_INFRA
  $summary.ok = $false
  $summary.reason_code = "INFRA_PRODUCT_WINDOW_EXCEPTION"
  $summary.child_reason_code = "INFRA_PRODUCT_WINDOW_EXCEPTION"
  $summary.error = [ordered]@{ message = $_.Exception.Message }

  if ($summary.out_dir) {
    if (!(Test-Path -LiteralPath $summary.out_dir)) { New-Item -ItemType Directory -Path $summary.out_dir -Force | Out-Null }
    WriteJsonFile (Join-Path $summary.out_dir "summary.json") $summary
  }
  Write-Output (($summary | ConvertTo-Json -Depth 40 -Compress))
  exit $summary.exit_code
}
