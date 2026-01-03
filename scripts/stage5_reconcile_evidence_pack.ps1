[CmdletBinding()]
param(
  [string]$Repo = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if ([string]::IsNullOrWhiteSpace($Repo)) {
  $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
} else {
  $Repo = (Resolve-Path -LiteralPath $Repo).Path
}

$scriptV2 = Join-Path $Repo "scripts\stage5_reconcile_evidence_pack_v2.ps1"

# WriteCompatLatest keeps legacy consumers (soak gate/UI) working if they still read reconcile_evidence_latest.json
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $scriptV2 -Repo $Repo -WriteCompatLatest

exit $LASTEXITCODE
