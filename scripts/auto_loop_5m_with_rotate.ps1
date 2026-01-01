<#
AUTO_LOOP_5M_WITH_ROTATE (post-step + heartbeat)

- Runs scripts/auto_loop_5m.ps1
- Refreshes args/data/scheduler_heartbeat.txt (soak_gate dependency)
- Runs args.ops.rotate_logs_v0 to refresh latest pointers
- Exit policy:
    main rc != 0 -> propagate
    main rc == 0 and rotate rc != 0 -> WARN (exit 1)
    else exit 0
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

# Heartbeat for soak_gate
$hbPath = Join-Path $repo "args\data\scheduler_heartbeat.txt"
$hbTs = (Get-Date).ToUniversalTime().ToString("o").Replace("+00:00","Z")
$hbTs | Set-Content -Encoding ascii $hbPath

# 1) Main loop
powershell -ExecutionPolicy Bypass -File .\scripts\auto_loop_5m.ps1 -Cycles $Cycles -IntervalSec $IntervalSec
$rcMain = $LASTEXITCODE

# 2) Post-step: rotate_logs
$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmssfffZ")
$logDir = Join-Path $repo "args\logs\auto_loop_5m"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "${ts}_post_rotate_logs.log"

py -3.11 -m args.ops.rotate_logs_v0 *> $logPath
$rcRotate = $LASTEXITCODE

if ($rcMain -ne 0) { exit $rcMain }
if ($rcRotate -ne 0) { exit 1 }
exit 0
