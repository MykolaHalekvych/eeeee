<#
Stage7.7 — Register Scheduled Tasks (Windows Task Scheduler)

Creates/updates:
- ARGS_AutoLoop_1m  : every 1 minute -> py -3.11 -m args.ops.auto_loop_v1 --once
- ARGS_SoakCheck_15m: every 15 minutes -> py -3.11 -m args.ops.soak_check_v0 --last 50 --require_run_change

Notes:
- Runs under current user with highest privileges.
- stop.flag is honored by auto_loop_v1.
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
  if (-not (Test-Path $p)) {
    throw ("Missing {0}: {1}" -f $label, $p)
  }
}

function Split-PythonCmd([string]$cmd) {
  $parts = $cmd.Trim().Split(" ", 2, [System.StringSplitOptions]::RemoveEmptyEntries)
  if ($parts.Count -eq 1) { return @($parts[0], "") }
  return @($parts[0], $parts[1])
}

Assert-PathExists $RepoRoot "RepoRoot"
Assert-PathExists (Join-Path $RepoRoot "args") "args/ folder"
Assert-PathExists (Join-Path $RepoRoot "scripts") "scripts/ folder"

$pyParts = Split-PythonCmd $PythonCmd
$pyExe = $pyParts[0]
$pyArgPrefix = $pyParts[1]

# Build argument strings
$autoArgs = @()
if ($pyArgPrefix) { $autoArgs += $pyArgPrefix }
$autoArgs += @("-m", "args.ops.auto_loop_v1", "--once")
$autoArgStr = ($autoArgs -join " ")

$soakArgs = @()
if ($pyArgPrefix) { $soakArgs += $pyArgPrefix }
$soakArgs += @("-m", "args.ops.soak_check_v0", "--last", "50", "--require_run_change")
$soakArgStr = ($soakArgs -join " ")

Write-Host ("RepoRoot: {0}" -f $RepoRoot)
Write-Host ("Python:   {0}" -f $PythonCmd)
Write-Host ("AutoLoop: {0} {1}" -f $pyExe, $autoArgStr)
Write-Host ("SoakChk:  {0} {1}" -f $pyExe, $soakArgStr)

# Preflight compile
& $pyExe $pyArgPrefix -m py_compile (Join-Path $RepoRoot "args\ops\auto_loop_v1.py") | Out-Null
& $pyExe $pyArgPrefix -m py_compile (Join-Path $RepoRoot "args\ops\soak_check_v0.py") | Out-Null

$taskAutoName = "ARGS_AutoLoop_1m"
$taskSoakName = "ARGS_SoakCheck_15m"

# Triggers (repeat forever)
$triggerAuto = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 1) `
  -RepetitionDuration (New-TimeSpan -Days 3650)

$triggerSoak = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
  -RepetitionInterval (New-TimeSpan -Minutes 15) `
  -RepetitionDuration (New-TimeSpan -Days 3650)

# Actions
$actionAuto = New-ScheduledTaskAction -Execute $pyExe -Argument $autoArgStr -WorkingDirectory $RepoRoot
$actionSoak = New-ScheduledTaskAction -Execute $pyExe -Argument $soakArgStr -WorkingDirectory $RepoRoot

# Settings
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1)

# Run as current user (interactive token) + highest privileges
$principal = New-ScheduledTaskPrincipal -UserId $env:UserName -LogonType Interactive -RunLevel Highest

$taskAuto = New-ScheduledTask -Action $actionAuto -Trigger $triggerAuto -Settings $settings -Principal $principal
$taskSoak = New-ScheduledTask -Action $actionSoak -Trigger $triggerSoak -Settings $settings -Principal $principal

function Upsert-Task([string]$name, $taskObj) {
  $exists = $false
  try { Get-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null; $exists = $true } catch { $exists = $false }

  if ($DryRun) {
    if ($exists) { Write-Host ("[DRYRUN] Would Update task: {0}" -f $name) }
    else { Write-Host ("[DRYRUN] Would Register task: {0}" -f $name) }
    return
  }

  if ($exists) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Start-Sleep -Milliseconds 200
  }
  Register-ScheduledTask -TaskName $name -InputObject $taskObj | Out-Null
  Write-Host ("OK task: {0}" -f $name)
}

Upsert-Task $taskAutoName $taskAuto
Upsert-Task $taskSoakName $taskSoak

Write-Host ""
Write-Host "VERIFY:"
Write-Host ("  Get-ScheduledTask -TaskName {0},{1} | Format-Table TaskName,State" -f $taskAutoName, $taskSoakName)
Write-Host ("  Start-ScheduledTask -TaskName {0}" -f $taskAutoName)
Write-Host "  Wait 2 minutes; check args\data\ops_health.json and args\logs\ops_events.jsonl"
