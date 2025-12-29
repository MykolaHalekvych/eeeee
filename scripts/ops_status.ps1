param(
  [string]$TaskName = "ARGS_AutoLoop_5m",
  [string]$SnapshotPath = ".\args\data\ibkr_open_orders_live.jsonl",
  [string]$StopFlagPath = ".\args\data\stop.flag"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Repo

Write-Host "=== ARGS OPS STATUS (Stage 6) ==="
Write-Host ("Repo: {0}" -f (Get-Location))

# Task info
try {
  $ti = Get-ScheduledTaskInfo -TaskName $TaskName
  $hex = "0x{0:X8}" -f ([uint32]$ti.LastTaskResult)
  Write-Host ""
  Write-Host "Task Scheduler:"
  Write-Host ("  TaskName         : {0}" -f $TaskName)
  Write-Host ("  LastRunTime      : {0}" -f $ti.LastRunTime)
  Write-Host ("  NextRunTime      : {0}" -f $ti.NextRunTime)
  Write-Host ("  LastTaskResult   : {0} ({1})" -f $ti.LastTaskResult, $hex)
  Write-Host ("  MissedRuns       : {0}" -f $ti.NumberOfMissedRuns)
} catch {
  Write-Host ""
  Write-Host ("Task Scheduler: ERROR: {0}" -f $_.Exception.Message)
}

# Task state
try {
  $t = Get-ScheduledTask -TaskName $TaskName
  Write-Host ("  State            : {0}" -f $t.State)
} catch {
  Write-Host ("  State            : ERROR: {0}" -f $_.Exception.Message)
}

# Stop flag
$sf = Join-Path $Repo $StopFlagPath
Write-Host ""
Write-Host "Control plane:"
if (Test-Path -LiteralPath $sf) {
  $it = Get-Item -LiteralPath $sf
  Write-Host ("  stop.flag        : PRESENT (mtime={0})" -f $it.LastWriteTime)
} else {
  Write-Host "  stop.flag        : not present"
}

# Snapshot
$sp = Join-Path $Repo $SnapshotPath
Write-Host ""
Write-Host "Snapshot:"
if (Test-Path -LiteralPath $sp) {
  $it = Get-Item -LiteralPath $sp
  $ageMin = [math]::Round(((Get-Date) - $it.LastWriteTime).TotalMinutes, 1)
  Write-Host ("  path             : {0}" -f $it.FullName)
  Write-Host ("  mtime            : {0}" -f $it.LastWriteTime)
  Write-Host ("  ageMin           : {0}" -f $ageMin)
  Write-Host ("  bytes            : {0}" -f $it.Length)
} else {
  Write-Host ("  MISSING          : {0}" -f $sp)
}

# Processes
Write-Host ""
Write-Host "OPS Processes:"
$rx = 'ops_loop_5m_stage6c\.ps1|auto_loop_5m\.ps1'
try {
  $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
    Where-Object { $_.CommandLine -match $rx } |
    Select-Object ProcessId, ParentProcessId, CommandLine

  if (-not $procs) {
    Write-Host "  (none)"
  } else {
    $procs | Format-Table -AutoSize
  }
} catch {
  Write-Host ("  ERROR: {0}" -f $_.Exception.Message)
}

# Latest cycle log
Write-Host ""
Write-Host "Latest cycle log:"
try {
  $latest = Get-ChildItem .\args\logs -Filter "auto_loop_*_cycle*.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

  if ($latest) {
    Write-Host ("  {0}" -f $latest.FullName)
    Write-Host ("  mtime={0} bytes={1}" -f $latest.LastWriteTime, $latest.Length)
  } else {
    Write-Host "  (not found)"
  }
} catch {
  Write-Host ("  ERROR: {0}" -f $_.Exception.Message)
}

Write-Host ""
Write-Host "=== END ==="
