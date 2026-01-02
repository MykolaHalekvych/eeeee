param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [int]$Seconds = 86400
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

cd $Repo

# fail-closed if stop.flag exists
if (Test-Path ".\stop.flag") {
  Write-Host "stop.flag exists; refusing to start soak."
  exit 2
}

py -3.11 -m args_core.soak_stage7_v1 --repo $Repo --seconds $Seconds
exit $LASTEXITCODE
