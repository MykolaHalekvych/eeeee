<# 
OPS_HEALTH_V1 runner (JSON-only stdout)

Exit codes:
  0 = OK
  1 = WARN/FAIL (operational gating fail)
  2 = INFRA FAIL (snapshot/connect failure)
#>

[CmdletBinding()]
param(
  [string]$IbHost = "localhost",
  [int]$IbPort = 7497,
  [int]$IbClientId = 77,
  [int]$ConnectTimeoutS = 8,
  [int]$TimeoutS = 25
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# repo root = parent of scripts folder
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo

try {
  $cmd = @(
    "py","-3.11","-m","args.ops.ops_health_v1",
    "--host",$IbHost,
    "--port",$IbPort,
    "--client-id",$IbClientId,
    "--connect-timeout-s",$ConnectTimeoutS,
    "--timeout-s",$TimeoutS
  )

  $json = & $cmd[0] $cmd[1..($cmd.Length-1)]
  $code = $LASTEXITCODE

  Write-Output $json
  exit $code
}
finally {
  Pop-Location
}
