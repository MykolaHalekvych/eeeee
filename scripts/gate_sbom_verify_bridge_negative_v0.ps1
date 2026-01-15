param(
  [string]$SbomRepo = "C:\Users\mukol\ARGS-SBOM-v0",
  [string]$OutDir = ".\args\data\smoke\gate_sbom_verify_bridge_v0_negative"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function UtcTs() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ") }
function UtcId() { [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") }
function Ensure-Dir([string]$p) { if ($p) { New-Item -ItemType Directory -Force -Path $p | Out-Null } }

function Last-JsonLine([string[]]$lines) {
  $j = ($lines | Where-Object { $_.Trim().StartsWith("{") -and $_.Trim().EndsWith("}") } | Select-Object -Last 1)
  return $j
}

function Run-Gate([string]$zip, [string]$hash, [string]$caseDir) {
  Ensure-Dir $caseDir
  $out = & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\scripts\gate_sbom_verify_bridge_v0.ps1 `
    -ZipPath $zip -HashesPath $hash -SbomRepo $SbomRepo -OutDir $caseDir

  $rc = $LASTEXITCODE
  $jsonLine = Last-JsonLine $out
  if (-not $jsonLine) { throw "No JSON line from gate" }
  $obj = ($jsonLine | ConvertFrom-Json)
  return @{ rc=$rc; obj=$obj }
}

Ensure-Dir $OutDir
$runDir = Join-Path (Resolve-Path $OutDir).Path ("RUN_" + (UtcId))
Ensure-Dir $runDir

# pick latest SBOM release
$zip = (Get-ChildItem (Join-Path $SbomRepo "dist\releases\*.zip") | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1).FullName
$hash = ($zip -replace "\.zip$",".hashes.json")

$results = @()
$failed = 0

function Assert([string]$caseId, $got, [int]$expRc, [string]$expReason) {
  $ok = (($got.rc -eq $expRc) -and ([string]$got.obj.reason_code -eq $expReason))
  $row = @{
    case_id = $caseId
    ok = $ok
    got_rc = $got.rc
    got_reason = [string]$got.obj.reason_code
    exp_rc = $expRc
    exp_reason = $expReason
  }
  $script:results += $row
  if (-not $ok) {
    $script:failed += 1
    Write-Host ("FAIL " + $caseId + " got=(" + $row.got_rc + "," + $row.got_reason + ") exp=(" + $expRc + "," + $expReason + ")")
  } else {
    Write-Host ("PASS " + $caseId)
  }
}

# CASE 1: Corrupt zip => DENY_INPUT_HASH_MISMATCH (rc=1)
$case1 = Join-Path $runDir "case1_corrupt_zip"
Ensure-Dir $case1
$zipBad = Join-Path $case1 "corrupt.zip"
Copy-Item -Force $zip $zipBad

# flip one byte
$bytes = [System.IO.File]::ReadAllBytes($zipBad)
$bytes[0] = ($bytes[0] -bxor 0xFF)
[System.IO.File]::WriteAllBytes($zipBad, $bytes)

$got1 = Run-Gate $zipBad $hash (Join-Path $case1 "gate_out")
Assert "case1_corrupt_zip" $got1 1 "DENY_INPUT_HASH_MISMATCH"

# CASE 2: Missing hashes => INFRA_TOOL_ERROR (rc=2)
$case2 = Join-Path $runDir "case2_missing_hashes"
Ensure-Dir $case2
$got2 = Run-Gate $zip (Join-Path $case2 "nope.hashes.json") (Join-Path $case2 "gate_out")
Assert "case2_missing_hashes" $got2 2 "INFRA_TOOL_ERROR"

$final = @{
  schema = "gate_sbom_verify_bridge_negative_v0"
  ok = ($failed -eq 0)
  exit_code = $(if ($failed -eq 0) { 0 } else { 1 })
  reason_code = $(if ($failed -eq 0) { "OK" } else { "FAIL_CASES_FAILED" })
  ts_utc = (UtcTs)
  out_dir = $runDir
  results = $results
}

$finalPath = Join-Path $runDir "gate_sbom_verify_bridge_negative_v0.json"
($final | ConvertTo-Json -Depth 20) | Out-File -FilePath $finalPath -Encoding utf8 -Force

Write-Output ($final | ConvertTo-Json -Depth 20 -Compress)
exit $final.exit_code
