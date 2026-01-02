[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [string]$TaskName = "ARGS_IBKR_Tap_1m",
  [int]$IntervalMinutes = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script = Join-Path $Repo "scripts\ibkr_tap_once.ps1"

if (-not (Test-Path -LiteralPath $script)) {
  throw "Missing script: $script"
}

# schtasks has limited trigger types; we use MINUTE schedule.
# Note: This runs under current user context. If you need 'Run whether user is logged on',
# you must configure via Task Scheduler UI with stored credentials.
$tr = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" -Repo `"$Repo`" -Seconds 55"

# delete if exists (idempotent)
schtasks /Delete /TN $TaskName /F | Out-Null 2>$null

schtasks /Create /TN $TaskName `
  /SC MINUTE `
  /MO $IntervalMinutes `
  /TR $tr `
  /RL HIGHEST `
  /F | Out-Null

Write-Host "Created task: $TaskName"
Write-Host "Run: schtasks /Run /TN $TaskName"
Write-Host "Query: schtasks /Query /TN $TaskName /V /FO LIST"
