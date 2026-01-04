<#
.SYNOPSIS
  Guarded autoloop wrapper: blocks execution when baseline is dirty.

BEHAVIOR
  - Runs baseline_check_v1 (read-only)
  - Reads control_plane.json + stop.flag (execution gate)
  - If baseline dirty AND execution is armed (PAPER + enable_paper_execution=true + stop.flag exists) => exit 2 (HALT)
  - Otherwise runs scripts/auto_loop_5m.ps1
  - Maps auto_loop exit codes:
      0 => OK
      1 => WARN (propagate as 1)
      >=2 => HALT (2)
  - JSON-only stdout (single object)
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [int]$Cycles = 1,
  [int]$IntervalSec = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = (Resolve-Path $Repo).Path

# Execution gate stop.flag (used by paper proofs / execution arming)
$stopFlagExec = Join-Path $repo "args\logs\stop.flag"
# Autoloop stop.flag (if your core loop uses it)
$stopFlagOps  = Join-Path $repo "args\data\stop.flag"

$autoLoop = Join-Path $repo "scripts\auto_loop_5m.ps1"

function Read-ControlPlane([string]$repo) {
  Push-Location $repo
  try {
    $cpJson = py -3.11 -c "import json; from pathlib import Path; p=Path('args/data/control_plane.json'); d=json.loads(p.read_bytes().decode('utf-8-sig')); print(json.dumps({'execution_mode': d.get('execution_mode'), 'enable_paper_execution': bool(d.get('enable_paper_execution', False)), 'global_mode': d.get('global_mode')}, ensure_ascii=False))"
    return $cpJson | ConvertFrom-Json
  } finally { Pop-Location }
}

function Run-BaselineCheck([string]$repo) {
  Push-Location $repo
  try {
    $out = py -3.11 -m args.ops.baseline_check_v1 --repo $repo
    $rc = $LASTEXITCODE
    $obj = $null
    try { $obj = $out | ConvertFrom-Json } catch { $obj = @{ parse_error = "baseline_check_not_json"; raw = ($out | Out-String) } }
    return @{ rc=$rc; obj=$obj }
  } finally { Pop-Location }
}

function Tail-Text([string]$text, [int]$maxLines, [int]$maxChars) {
  $lines = ($text -split "`r`n|`n|`r") | Where-Object { $_ -and $_.Trim().Length -gt 0 }
  $tailLines = $lines | Select-Object -Last $maxLines
  $tail = ($tailLines -join "`n")
  if ($tail.Length -gt $maxChars) {
    $tail = $tail.Substring($tail.Length - $maxChars)
  }
  return $tail
}

$report = @{
  schema = "auto_loop_5m_guarded_v1"
  ts_utc = (Get-Date).ToUniversalTime().ToString("o")
  repo = $repo

  stop_flag_exec_exists = (Test-Path $stopFlagExec)
  stop_flag_ops_exists  = (Test-Path $stopFlagOps)

  control_plane = $null
  baseline = $null

  execution_armed = $false
  decision = "UNKNOWN"

  auto_loop_rc = $null
  auto_loop_output_tail = ""

  exit_code = 2
  errors = @()
}

try {
  if (-not (Test-Path $autoLoop)) {
    $report.decision = "HALT_MISSING_AUTO_LOOP_SCRIPT"
    $report.exit_code = 2
    $report | ConvertTo-Json -Depth 8
    exit 2
  }

  $cp = Read-ControlPlane $repo
  $report.control_plane = $cp

  $b = Run-BaselineCheck $repo
  $report.baseline = @{ rc=$b.rc; obj=$b.obj }

  $execMode = ("" + $cp.execution_mode).ToUpper()
  $enable = [bool]$cp.enable_paper_execution
  $armed = ($execMode -eq "PAPER") -and $enable -and (Test-Path $stopFlagExec)
  $report.execution_armed = $armed

  $brc = [int]$b.rc

  if ($brc -eq 2) {
    $report.decision = "HALT_INFRA_FAIL_BASELINE_CHECK"
    $report.exit_code = 2
    $report | ConvertTo-Json -Depth 8
    exit 2
  }

  if ($brc -eq 1 -and $armed) {
    $report.decision = "HALT_BASELINE_DIRTY_EXECUTION_ARMED"
    $report.exit_code = 2
    $report | ConvertTo-Json -Depth 8
    exit 2
  }

  # Run autoloop (capture output; wrapper prints JSON only)
  Push-Location $repo
  try {
    $autoOut = & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $autoLoop -Cycles $Cycles -IntervalSec $IntervalSec 2>&1 | Out-String
    $autoRC = $LASTEXITCODE
  } finally { Pop-Location }

  $report.auto_loop_rc = $autoRC
  $report.auto_loop_output_tail = Tail-Text $autoOut 12 2000

  if ($autoRC -eq 0) {
    if ($brc -eq 1 -and -not $armed) {
      $report.decision = "WARN_BASELINE_DIRTY_EXECUTION_NOT_ARMED"
      $report.exit_code = 1
      $report | ConvertTo-Json -Depth 8
      exit 1
    }
    $report.decision = "OK"
    $report.exit_code = 0
    $report | ConvertTo-Json -Depth 8
    exit 0
  }

  if ($autoRC -eq 1) {
    # Treat as WARN, not HALT
    if ($brc -eq 1 -and -not $armed) {
      $report.decision = "WARN_AUTO_LOOP_EXIT_1_AND_BASELINE_DIRTY"
    } else {
      $report.decision = "WARN_AUTO_LOOP_EXIT_1"
    }
    $report.exit_code = 1
    $report | ConvertTo-Json -Depth 8
    exit 1
  }

  # autoRC >= 2
  $report.decision = "HALT_AUTO_LOOP_EXIT_GE2"
  $report.exit_code = 2
  $report | ConvertTo-Json -Depth 8
  exit 2

} catch {
  $report.decision = "HALT_EXCEPTION"
  $report.errors += @{ error = ($_ | Out-String) }
  $report.exit_code = 2
  $report | ConvertTo-Json -Depth 8
  exit 2
}
