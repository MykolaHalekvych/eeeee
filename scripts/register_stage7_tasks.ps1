<# 
Stage7.7 — Register Scheduled Tasks (Windows Task Scheduler)

Creates/updates:
- ARGS_AutoLoop_1m  : every 1 minute -> py -3.11 -m args.ops.auto_loop_v1 --once
- ARGS_SoakCheck_15m: every 15 minutes -> py -3.11 -m args.ops.soak_check_v0 --last 50 --require_run_change

Safe-by-default:
- No trading actions are performed here; it just runs your existing scripts.
- stop.flag is honored by auto_loop_v1.

Run from repo root: C:\Users\mukol\ARGS-Core-v1
#>

[CmdletBinding()]
param(
  [string]$RepoRoot = (Get-Location).Path,
  [string]$PythonCmd = "py -3.11",
  [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-PathExists([string]$p, [string]$label) {
  if (-not (Test-Path $p)) { throw "Missing $label: $p" }
}

function Split-PythonCmd([string]$cmd) {
  # "py -3.11" -> FilePath="py", Args="-3.11"
  $parts = $cmd.Trim().Split(" ", 2, [System.StringSplitOptions]::RemoveEmptyEntries)
  if ($parts.Count -eq 1) { return @($parts[0], "") }
  return @($parts[0], $parts[1])
}

# --- preflight ---
Assert-PathExists $RepoRoot "RepoRoot"
Assert-PathExists (Join-Path $RepoRoot "args") "args/ package folder"
Assert-PathExists (Join-Path $RepoRoot "scripts") "scripts/ folder"

$pyParts = Split-PythonCmd $PythonCmd
$pyExe = $pyParts[0]
$pyArgPrefix = $pyParts[1]

# Build the full argument strings for Task Scheduler.
# NOTE: Task Scheduler stores exe+args separately. Use -Argument for remaining.
$autoLoopArgs = @()
if ($pyArgPrefix) { $autoLoopArgs += $pyArgPrefix }
$autoLoopArgs += @("-m", "args.ops.auto_loop_v1", "--once")
$autoLoopArgStr = ($autoLoopArgs -join " ")

$soakArgs = @()
if ($pyArgPrefix) { $soakArgs += $pyArgPrefix }
$soakArgs += @("-m", "args.ops.soak_check_v0", "--last", "50", "--require_run_change")
$soakArgStr = ($soakArgs -join " ")

Write-Host "RepoRoot: $RepoRoot"
Write-Host "Python:   $PythonCmd"
Write-Host "AutoLoop: $pyExe $autoLoopArgStr"
Write-Host "SoakChk:  $pyExe $soakArgStr"

# Ensure the python modules exist (fail early)
& $pyExe $pyArgPrefix -m py_compile (Join-Path $RepoRoot "args\ops\auto_loop_v1.py") | Out-Null
& $pyExe $pyArgPrefix -m py_compile (Join-Path $RepoRoot "args\ops\soak_check_v0.py") | Out-Null

# --- Task definitions ---
$taskAutoName = "ARGS_AutoLoop_1m"
$taskSoakName = "ARGS_SoakCheck_15m"

# Triggers
$triggerAuto = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 1) `
  -RepetitionDuration (New-TimeSpan -Days 3650)

$triggerSoak = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
  -RepetitionInterval (New-TimeSpan -Minutes 15) `
  -RepetitionDuration (New-TimeSpan -Days 3650)

# Actions
$actionAuto = New-ScheduledTaskAction -Execute $pyExe -Argument $autoLoopArgStr -WorkingDirectory $RepoRoot
$actionSoak = New-ScheduledTaskAction -Execute $pyExe -Argument $soakArgStr -WorkingDirectory $RepoRoot

# Settings
# - Allow start if on battery (laptops)
# - Don't stop on idle
# - Restart on failure (simple)
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1)

# Principal: run under current user, highest privileges to avoid scheduler weirdness
$principal = New-ScheduledTaskPrincipal -UserId $env:UserName -LogonType S4U -RunLevel Highest

# Compose tasks
$taskAuto = New-ScheduledTask -Action $actionAuto -Trigger $triggerAuto -Settings $settings -Principal $principal
$taskSoak = New-ScheduledTask -Action $actionSoak -Trigger $triggerSoak -Settings $settings -Principal $principal

function Upsert-Task([string]$name, $task) {
  $exists = $false
  try {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction Stop
    $exists = $true
  } catch { $exists = $false }

  if ($DryRun) {
    if ($exists) { Write-Host "[DRYRUN] Would Update task: $name" }
    else { Write-Host
