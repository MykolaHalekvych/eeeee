param(
  [Parameter(Mandatory=$true)][string]$ZipPath,
  [Parameter(Mandatory=$true)][string]$HashesPath,
  [Parameter(Mandatory=$true)][string]$SbomRepo,
  [Parameter(Mandatory=$true)][string]$MarkerPath,
  [string]$GateOutDir = ".\args\data\smoke\non_bypass_sbom_gate"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function UtcTs() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ") }
function Ensure-Dir([string]$p) { if ($p) { New-Item -ItemType Directory -Force -Path $p | Out-Null } }

function Last-JsonLine([string[]]$lines) {
  return ($lines | Where-Object { $_.Trim().StartsWith("{") -and $_.Trim().EndsWith("}") } | Select-Object -Last 1)
}

Ensure-Dir (Split-Path -Parent $MarkerPath)

$out = & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\scripts\gate_sbom_verify_bridge_v0.ps1 `
  -ZipPath $ZipPath -HashesPath $HashesPath -SbomRepo $SbomRepo -OutDir $GateOutDir

$rc = $LASTEXITCODE
$j = Last-JsonLine $out
if (-not $j) { throw "No JSON from gate" }
$gateObj = ($j | ConvertFrom-Json)

if ($rc -ne 0) {
  $obj = @{
    schema = "run_inner_with_sbom_gate_v0"
    ok = $false
    exit_code = $rc
    reason_code = [string]$gateObj.reason_code
    ts_utc = (UtcTs)
    blocked = $true
    marker_written = $false
    marker_path = $MarkerPath
    gate = $gateObj
  }
  Write-Output ($obj | ConvertTo-Json -Depth 20 -Compress)
  exit $rc
}

# INNER (proof) = write marker file
Set-Content -Path $MarkerPath -Value ("INNER_RAN " + (UtcTs)) -Encoding utf8

$obj = @{
  schema = "run_inner_with_sbom_gate_v0"
  ok = $true
  exit_code = 0
  reason_code = "OK"
  ts_utc = (UtcTs)
  blocked = $false
  marker_written = $true
  marker_path = $MarkerPath
  gate = $gateObj
}

Write-Output ($obj | ConvertTo-Json -Depth 20 -Compress)
exit 0
