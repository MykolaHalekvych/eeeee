param(
  [string]$RunId = "",
  [string]$OutDir = "",
  [string]$DriftRepo = ""
)

$ErrorActionPreference = "Stop"

function UtcNowIso() {
  return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

$ToolRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ([string]::IsNullOrWhiteSpace($RunId)) {
  $RunId = "DRIFT_GATE_" + ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"))
}

if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $ToolRoot ("out\drift_gate\" + $RunId)
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

if ([string]::IsNullOrWhiteSpace($DriftRepo)) { $DriftRepo = $env:ARGS_DRIFT_REPO }
if ([string]::IsNullOrWhiteSpace($DriftRepo)) { $DriftRepo = "C:\Users\mukol\ARGS-Drift-Detector-v0" }

$encNoBom = New-Object System.Text.UTF8Encoding($false)
function Write-Utf8NoBom([string]$path, [string]$text) {
  [System.IO.File]::WriteAllText($path, $text.Replace("`r`n","`n"), $encNoBom)
}

function Fail-Infra([string]$reason) {
  $obj = @{
    schema="gate_drift_foundry_v0"
    ok=$false
    exit_code=2
    ts_utc=(UtcNowIso)
    reason_code=$reason
    run_id=$RunId
    out_dir=$OutDir
    drift_repo=$DriftRepo
  }
  $json = ($obj | ConvertTo-Json -Depth 8)
  Write-Utf8NoBom (Join-Path $OutDir "gate_summary.json") ($json + "`n")
  Write-Output $json
  exit 2
}

if (!(Test-Path $DriftRepo)) { Fail-Infra "DRIFT_GATE.INFRA.DRIFT_REPO_MISSING" }

$policyPath = Join-Path $ToolRoot "args\configs\drift_policy_foundry_v0.json"
if (!(Test-Path $policyPath)) { Fail-Infra "DRIFT_GATE.INFRA.POLICY_MISSING" }

$policy = Get-Content -Raw -Path $policyPath | ConvertFrom-Json
$baselineId = [string]$policy.baseline_id
if ([string]::IsNullOrWhiteSpace($baselineId)) { Fail-Infra "DRIFT_GATE.INFRA.BASELINE_ID_MISSING" }

$baselinePath = Join-Path $ToolRoot ("baselines\drift\" + $baselineId + "\drift_baseline_" + $baselineId + ".json")

$driftOut = Join-Path $OutDir "drift_check"
New-Item -ItemType Directory -Force -Path $driftOut | Out-Null

powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $DriftRepo "scripts\drift_check_v0.ps1") `
  -RepoRoot $ToolRoot `
  -PolicyPath $policyPath `
  -BaselinePath $baselinePath `
  -RunId $RunId `
  -OutDir $driftOut | Out-Host

$rc = $LASTEXITCODE
$reason = "UNKNOWN"
try {
  $sum = Get-Content -Raw -Path (Join-Path $driftOut "drift_summary.json") | ConvertFrom-Json
  $reason = [string]$sum.reason_code
} catch { }

$obj = @{
  schema="gate_drift_foundry_v0"
  ok=($rc -eq 0)
  exit_code=$rc
  ts_utc=(UtcNowIso)
  reason_code=$reason
  run_id=$RunId
  out_dir=$OutDir
  drift_out_dir=$driftOut
  policy_path=$policyPath
  baseline_path=$baselinePath
  baseline_id=$baselineId
  drift_repo=$DriftRepo
}

$json = ($obj | ConvertTo-Json -Depth 10)
Write-Utf8NoBom (Join-Path $OutDir "gate_summary.json") ($json + "`n")
Write-Output $json
exit $rc
