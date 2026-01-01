<#
AUTO_LOOP_5M_WITH_ROTATE (post-step)

- Runs existing scripts/auto_loop_5m.ps1
- Then runs args.ops.rotate_logs_v0 to refresh latest_run_id/latest_paths + archive pointers
- SAFE: rotate_logs failure downgrades to WARN (exit 0 -> 1), does not hard-fail cycle
#>

[CmdletBinding()]
param(
  [int]$Cycles = 1,
  [int]$IntervalSec = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = "C:\Users\mukol\ARGS-Core-v1"
cd $repo

# 1) Run main loop
powershell -ExecutionPolicy Bypass -File .\scripts\auto_loop_5m.ps1 -Cycles $Cycles -IntervalSec $IntervalSec
$rcMain = $LASTEXITCODE

# 2) Post-step: rotate_logs (refresh latest pointers)
$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmssfffZ")
$logDir = Join-Path $repo "args\logs\auto_loop_5m"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "${ts}_post_rotate_logs.log"

py -3.11 -m args.ops.rotate_logs_v0 *> $logPath
$rcRotate = $LASTEXITCODE

# 3) Exit policy:
# - If main rc != 0 -> propagate main rc (hard fail)
# - If main rc == 0 and rotate_logs failed -> WARN (exit 1)
if ($rcMain -ne 0) { exit $rcMain }
if ($rcRotate -ne 0) { exit 1 }
exit 0
