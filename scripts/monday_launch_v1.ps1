<#
.SYNOPSIS
  One-button launch: baseline clean loop -> Stage5 terminal proofs window.

NOTES
  - Progress goes to STDERR (so it does not pollute JSON stdout).
  - STDOUT: single JSON object only (at the end).
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [int]$MaxAttempts = 20,
  [int]$SleepSec = 60,
  [int]$TailChars = 2000,
  [int]$TailLines = 12
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Tail-Text([string]$text, [int]$maxLines, [int]$maxChars) {
  $lines = ($text -split "`r`n|`n|`r") | Where-Object { $_ -and $_.Trim().Length -gt 0 }
  $tailLines = $lines | Select-Object -Last $maxLines
  $tail = ($tailLines -join "`n")
  if ($tail.Length -gt $maxChars) {
    $tail = $tail.Substring($tail.Length - $maxChars)
  }
  return $tail
}

$repo = (Resolve-Path $Repo).Path
$cmdClean = Join-Path $repo "scripts\run_baseline_clean_window_v1.cmd"
$cmdProof = Join-Path $repo "scripts\run_stage5_terminal_proofs_window_v1.cmd"

$report = @{
  schema="monday_launch_v1"
  ts_utc=(Get-Date).ToUniversalTime().ToString("o")
  repo=$repo
  max_attempts=$MaxAttempts
  sleep_sec=$SleepSec
  baseline_cleared=$false
  attempts=@()
  proofs=$null
  ok=$false
  reason=$null
  exit_code=2
  errors=@()
}

try {
  if (-not (Test-Path $cmdClean)) { throw "Missing cmd: $cmdClean" }
  if (-not (Test-Path $cmdProof)) { throw "Missing cmd: $cmdProof" }

  for ($i=1; $i -le $MaxAttempts; $i++) {
    [Console]::Error.WriteLine(("monday_launch_v1: attempt {0}/{1} baseline_clean_window..." -f $i,$MaxAttempts))

    $out = cmd.exe /c $cmdClean 2>&1 | Out-String
    $rc = $LASTEXITCODE

    $report.attempts += @{
      n=$i
      rc=$rc
      out_tail=(Tail-Text $out $TailLines $TailChars)
    }

    if ($rc -eq 0) {
      $report.baseline_cleared = $true
      break
    }
    if ($rc -eq 2) {
      $report.exit_code = 2
      $report.ok = $false
      $report.reason = "INFRA_FAIL_BASELINE_CLEAN_WINDOW"
      $report | ConvertTo-Json -Depth 8
      exit 2
    }

    if ($i -lt $MaxAttempts -and $SleepSec -gt 0) {
      [Console]::Error.WriteLine(("monday_launch_v1: sleep {0}s" -f $SleepSec))
      Start-Sleep -Seconds $SleepSec
    }
  }

  if (-not $report.baseline_cleared) {
    $report.exit_code = 1
    $report.ok = $false
    $report.reason = "BASELINE_NOT_CLEARED_WITHIN_ATTEMPTS"
    $report | ConvertTo-Json -Depth 8
    exit 1
  }

  [Console]::Error.WriteLine("monday_launch_v1: baseline CLEAN -> running stage5 proofs window...")
  $out2 = cmd.exe /c $cmdProof 2>&1 | Out-String
  $rc2 = $LASTEXITCODE

  $report.proofs = @{
    rc=$rc2
    out_tail=(Tail-Text $out2 $TailLines $TailChars)
  }

  if ($rc2 -eq 0) {
    $report.ok = $true
    $report.exit_code = 0
    $report.reason = "ALL_PASS"
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
  $report.reason = "SCRIPT_ERROR"
  $report | ConvertTo-Json -Depth 8
  exit 2
}
