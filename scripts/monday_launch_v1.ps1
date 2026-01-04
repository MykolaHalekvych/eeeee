<#
.SYNOPSIS
  One-button launch: baseline clean loop -> Stage5 terminal proofs window.

Exit codes:
  0 = baseline became CLEAN and Stage5 proofs PASS
  1 = baseline not cleared within attempts OR proofs blocked/failed
  2 = infra/script error
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [int]$MaxAttempts = 20,
  [int]$SleepSec = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = (Resolve-Path $Repo).Path
$cmdClean = Join-Path $repo "scripts\run_baseline_clean_window_v1.cmd"
$cmdProof = Join-Path $repo "scripts\run_stage5_terminal_proofs_window_v1.cmd"

$report = @{
  schema="monday_launch_v1"
  ts_utc=(Get-Date).ToUniversalTime().ToString("o")
  repo=$repo
  attempts=@()
  baseline_cleared=$false
  proofs=$null
  ok=$false
  exit_code=2
  errors=@()
}

try {
  if (-not (Test-Path $cmdClean)) { throw "Missing cmd: $cmdClean" }
  if (-not (Test-Path $cmdProof)) { throw "Missing cmd: $cmdProof" }

  # 1) baseline clean loop
  for ($i=1; $i -le $MaxAttempts; $i++) {
    $out = cmd.exe /c $cmdClean 2>&1 | Out-String
    $rc = $LASTEXITCODE
    $report.attempts += @{ n=$i; rc=$rc; out_tail=($out.TrimEnd() | Select-Object -Last 1) }

    if ($rc -eq 0) {
      $report.baseline_cleared = $true
      break
    }
    if ($rc -eq 2) {
      $report.exit_code = 2
      $report.ok = $false
      $report | ConvertTo-Json -Depth 8
      exit 2
    }

    Start-Sleep -Seconds $SleepSec
  }

  if (-not $report.baseline_cleared) {
    $report.exit_code = 1
    $report.ok = $false
    $report.reason = "BASELINE_NOT_CLEARED_WITHIN_ATTEMPTS"
    $report | ConvertTo-Json -Depth 8
    exit 1
  }

  # 2) Stage5 proofs window
  $out2 = cmd.exe /c $cmdProof 2>&1 | Out-String
  $rc2 = $LASTEXITCODE
  $report.proofs = @{ rc=$rc2; out_tail=($out2.TrimEnd() | Select-Object -Last 1) }

  if ($rc2 -eq 0) {
    $report.ok = $true
    $report.exit_code = 0
    $report | ConvertTo-Json -Depth 8
    exit 0
  } else {
    $report.ok = $false
    $report.exit_code = 1
    $report.reason = "PROOFS_BLOCKED_OR_FAILED"
    $report | ConvertTo-Json -Depth 8
    exit 1
  }

} catch {
  $report.errors += @{ error = ($_ | Out-String) }
  $report.ok = $false
  $report.exit_code = 2
  $report | ConvertTo-Json -Depth 8
  exit 2
}
