param(
  [Parameter(Mandatory=$true)][string]$ZipPath,
  [string]$HashesPath = "",
  [Parameter(Mandatory=$true)][string]$SbomRepo,
  [string]$OutDir = ".\args\data\smoke\gate_sbom_verify_bridge_v0"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function UtcTs() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ") }
function UtcId() { [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") }

function Ensure-Dir([string]$p) {
  if ([string]::IsNullOrWhiteSpace($p)) { return }
  New-Item -ItemType Directory -Force -Path $p | Out-Null
}

function ReadAllUtf8NoBom([string]$p) {
  if (-not (Test-Path $p)) { return "" }
  return [System.IO.File]::ReadAllText($p, $Utf8NoBom)
}

function WriteAllUtf8NoBom([string]$p, [string]$text) {
  $dir = Split-Path -Parent $p
  Ensure-Dir $dir
  $norm = ($text -replace "`r`n","`n")
  [System.IO.File]::WriteAllText($p, $norm, $Utf8NoBom)
}

function Normalize-Rc([int]$rc) {
  if ($rc -eq 0) { return 0 }
  if ($rc -eq 1) { return 1 }
  if ($rc -eq 2) { return 2 }
  return 2
}

function Last-JsonLine([string]$text) {
  $lines = ($text -split "`r?`n") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
  $j = ($lines | Where-Object { $_.StartsWith("{") -and $_.EndsWith("}") } | Select-Object -Last 1)
  return $j
}

function Emit([bool]$ok, [int]$rc, [string]$reason, $detail, [string]$runDir) {
  $rc = Normalize-Rc $rc
  $obj = @{
    schema = "gate_sbom_verify_bridge_v0"
    ok = $ok
    exit_code = $rc
    reason_code = $reason
    ts_utc = (UtcTs)
    zip_path = $ZipPath
    hashes_path = $HashesPath
    sbom_repo = $SbomRepo
    run_dir = $runDir
    details = $detail
  }

  if ($runDir) {
    $outPath = Join-Path $runDir "gate_sbom_verify_bridge_v0.json"
    ($obj | ConvertTo-Json -Depth 24) | Out-File -FilePath $outPath -Encoding utf8 -Force
  }

  Write-Output ($obj | ConvertTo-Json -Depth 24 -Compress)
  exit $rc
}

try {
  if (-not (Test-Path $ZipPath)) { Emit $false 2 "INFRA_TOOL_ERROR" @{ error="zip not found"; zip=$ZipPath } "" }

  if ([string]::IsNullOrWhiteSpace($HashesPath)) {
    $cand = ($ZipPath -replace "\.zip$",".hashes.json")
    if (Test-Path $cand) { $HashesPath = $cand }
  }
  if ([string]::IsNullOrWhiteSpace($HashesPath) -or -not (Test-Path $HashesPath)) {
    Emit $false 2 "INFRA_TOOL_ERROR" @{ error="hashes not found"; hashes=$HashesPath } ""
  }

  if (-not (Test-Path $SbomRepo)) { Emit $false 2 "INFRA_TOOL_ERROR" @{ error="sbom repo not found"; sbom_repo=$SbomRepo } "" }

  $verifyScript = Join-Path $SbomRepo "scripts\release_verify_sbom_v0.ps1"
  if (-not (Test-Path $verifyScript)) {
    Emit $false 2 "INFRA_TOOL_ERROR" @{ error="verify script not found"; path=$verifyScript } ""
  }

  Ensure-Dir $OutDir
  $outAbs = (Resolve-Path -LiteralPath $OutDir).Path

  $runDir = Join-Path $outAbs ("RUN_" + (UtcId))
  Ensure-Dir $runDir
  $evidenceDir = Join-Path $runDir "evidence"
  Ensure-Dir $evidenceDir

  $innerStdout = Join-Path $evidenceDir "inner_stdout.txt"
  $innerStderr = Join-Path $evidenceDir "inner_stderr.txt"
  $cmdlinePath = Join-Path $evidenceDir "cmdline.txt"

  $cmd = @(
    "-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
    "-File",$verifyScript,
    "-ZipPath",$ZipPath,
    "-HashesPath",$HashesPath,
    "-OutDir",$runDir
  )

  WriteAllUtf8NoBom $cmdlinePath ("powershell " + ($cmd -join " "))

  $p = Start-Process -FilePath "powershell" -ArgumentList $cmd -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $innerStdout -RedirectStandardError $innerStderr

  $rc = Normalize-Rc $p.ExitCode

  $stdoutText = ReadAllUtf8NoBom $innerStdout
  $jsonLine = Last-JsonLine $stdoutText

  $innerObj = $null
  $innerReason = ""
  $innerReleaseId = ""

  if ($jsonLine) {
    try {
      $innerObj = ($jsonLine | ConvertFrom-Json)
      if ($innerObj.reason_code) { $innerReason = [string]$innerObj.reason_code }
      if ($innerObj.release_id) { $innerReleaseId = [string]$innerObj.release_id }
    } catch {}
  }

  if ($rc -eq 0) {
    Emit $true 0 "OK" @{
      verify_out_dir = $runDir
      inner_release_id = $innerReleaseId
      evidence = @{ stdout=$innerStdout; stderr=$innerStderr; cmdline=$cmdlinePath }
      inner = $innerObj
    } $runDir
  }

  if ([string]::IsNullOrWhiteSpace($innerReason)) {
    $innerReason = $(if ($rc -eq 1) { "DENY_SBOM_SCHEMA_INVALID" } else { "INFRA_TOOL_ERROR" })
  }

  Emit $false $rc $innerReason @{
    verify_out_dir = $runDir
    inner_release_id = $innerReleaseId
    evidence = @{ stdout=$innerStdout; stderr=$innerStderr; cmdline=$cmdlinePath }
    inner = $innerObj
  } $runDir
}
catch {
  Emit $false 2 "INFRA_TOOL_ERROR" @{ error=$_.Exception.Message } ""
}
