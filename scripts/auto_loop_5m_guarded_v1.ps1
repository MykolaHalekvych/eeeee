<#
.SYNOPSIS
  Guarded autoloop wrapper: blocks execution when baseline is dirty.

.DESCRIPTION
  - Runs args.ops.baseline_check_v1
  - Reads control_plane.json + stop.flag
  - If baseline dirty AND execution is armed (PAPER + enable_paper_execution=true + stop.flag exists) => exit 2 (HALT)
  - Otherwise runs scripts/auto_loop_5m.ps1
  - JSON-only stdout (single object)

Exit codes:
  0 = OK
  1 = WARN (baseline dirty but execution NOT armed; autoloop still ran)
  2 = FAIL/HALT (baseline dirty while execution armed OR infra fail OR exception)
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
$stopFlag = Join-Path $repo "args\logs\stop.flag"
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
    return @{ rc=$rc; raw=$out; obj=$obj }
  } finally { Pop-Location }
}

$report = @{
  schema = "auto_loop_5m_guarded_v1"
  ts_utc = (Get-Date).ToUniversalTime().ToString("o")
  repo = $repo
  stop_flag_exists = (Test-Path $stopFlag)
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
  $armed = ($execMode -eq "PAPER") -and $enable -and (Test-Path $stopFlag)
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

  # Run autoloop (capture output, JSON-only wrapper output)
  Push-Location $repo
  try {
    $autoOut = & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $autoLoop -Cycles $Cycles -IntervalSec $IntervalSec 2>&1 | Out-String
    $autoRC = $LASTEXITCODE
  } finally { Pop-Location }

  $report.auto_loop_rc = $autoRC
  $report.auto_loop_output_tail = ($autoOut.TrimEnd() | Select-Object -Last 1)

  # If baseline dirty but not armed: WARN if autoloop succeeded
  if ($brc -eq 1 -and -not $armed -and $autoRC -eq 0) {
    $report.decision = "WARN_BASELINE_DIRTY_BUT_EXECUTION_NOT_ARMED"
    $report.exit_code = 1
    $report | ConvertTo-Json -Depth 8
    exit 1
  }

  if ($autoRC -eq 0) {
    $report.decision = "OK"
    $report.exit_code = 0
    $report | ConvertTo-Json -Depth 8
    exit 0
  } else {
    $report.decision = "HALT_AUTO_LOOP_NONZERO"
    $report.exit_code = 2
    $report | ConvertTo-Json -Depth 8
    exit 2
  }

} catch {
  $report.decision = "HALT_EXCEPTION"
  $report.errors += @{ error = ($_ | Out-String) }
  $report.exit_code = 2
  $report | ConvertTo-Json -Depth 8
  exit 2
}
