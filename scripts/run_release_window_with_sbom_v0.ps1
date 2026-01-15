param(
  [Parameter(Mandatory=$true)][string]$SbomRepo,
  [ValidateSet("YES","NO")][string]$Enforce = "YES",
  [string]$InnerScript = ".\scripts\factory_release_window_v1.ps1",
  [Parameter(ValueFromRemainingArguments=$true)][string[]]$InnerArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$oldRepo = $env:SBOM_REPO
$oldEnf  = $env:SBOM_ENFORCE

try{
  $env:SBOM_REPO = $SbomRepo
  $env:SBOM_ENFORCE = $Enforce

  & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $InnerScript @InnerArgs | Out-Default
  exit $LASTEXITCODE
}
finally{
  $env:SBOM_REPO = $oldRepo
  $env:SBOM_ENFORCE = $oldEnf
}
