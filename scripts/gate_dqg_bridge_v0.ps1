param(
  [Parameter(Mandatory=$true)][string]$DqgRequestPath,

  [string]$RunId = ("GATE_DQG_" + ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"))),
  [string]$OutDir = "",

  [string]$DqgRepo = "C:\Users\mukol\ARGS-DQG-v0",
  [string]$TargetRepo = "."
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-JsonFile {
  param([string]$Path, [object]$Obj)
  $dir = Split-Path -Parent $Path
  if ($dir -and !(Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
  ($Obj | ConvertTo-Json -Depth 30) | Out-File -FilePath $Path -Encoding utf8
}

try {
  $foundry = (Resolve-Path ".").Path

  $dqgRepoAbs = (Resolve-Path $DqgRepo).Path
  $targetAbs  = (Resolve-Path $TargetRepo).Path

  # Request path: resolve inside target, then pass ABSOLUTE to DQG wrapper (important)
  $reqAbs = (Resolve-Path (Join-Path $targetAbs $DqgRequestPath)).Path

  if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = Join-Path $foundry ("out\gates\dqg_bridge_v0\" + $RunId)
  }
  $outAbs = (Resolve-Path (New-Item -ItemType Directory -Force $OutDir)).Path
  $evidence = Join-Path $outAbs "evidence"
  New-Item -ItemType Directory -Force $evidence | Out-Null

  $stdoutPath = Join-Path $evidence "dqg_bridge.stdout.txt"
  $stderrPath = Join-Path $evidence "dqg_bridge.stderr.txt"

  # Call external DQG wrapper
  $dqgWrapper = Join-Path $dqgRepoAbs "scripts\dqg_check_v0.ps1"

  $psArgs = @(
    "-NoProfile",
    "-ExecutionPolicy","Bypass",
    "-File",$dqgWrapper,
    "-RequestPath",$reqAbs,
    "-RunId",$RunId,
    "-OutDir",$outAbs,
    "-RepoRoot",$dqgRepoAbs,
    "-TargetRoot",$targetAbs
  )

  $proc = Start-Process -FilePath "powershell" -ArgumentList $psArgs `
    -WorkingDirectory $dqgRepoAbs -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

  $rc = [int]$proc.ExitCode
  if ($rc -notin 0,1,2) { $rc = 2 }

  # Read DQG summary and propagate reason_code
  $dqgSumPath = Join-Path $outAbs "dqg_summary.json"
  $reason = "DQG.INFRA.IO_ERROR"
  $verdict = "INFRA"
  if (Test-Path $dqgSumPath) {
    $sum = Get-Content -Raw $dqgSumPath | ConvertFrom-Json
    $reason = $sum.reason_code
    $verdict = $sum.verdict
  }

  $bridgeSummary = @{
    schema = "gate_dqg_bridge_v0"
    ts_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    ok = ($rc -eq 0)
    exit_code = $rc
    run_id = $RunId
    dqg_repo = ($dqgRepoAbs -replace "\\","/")
    target_repo = ($targetAbs -replace "\\","/")
    request_path = ($reqAbs -replace "\\","/")
    out_dir = ($outAbs -replace "\\","/")
    verdict = $verdict
    reason_code = $reason
    dqg_summary_path = ($dqgSumPath -replace "\\","/")
  }

  Write-JsonFile -Path (Join-Path $outAbs "dqg_bridge_summary.json") -Obj $bridgeSummary

  ($bridgeSummary | ConvertTo-Json -Compress)
  exit $rc
}
catch {
  $err = $_.Exception.Message
  $fallback = @{
    schema = "gate_dqg_bridge_v0"
    ts_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    ok = $false
    exit_code = 2
    run_id = $RunId
    reason_code = "DQG.INFRA.IO_ERROR"
    error = $err
  }
  ($fallback | ConvertTo-Json -Compress)
  exit 2
}
