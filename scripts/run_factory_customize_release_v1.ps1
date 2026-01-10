param(
  [Parameter(Mandatory=$true)][string]$Request,
  [Parameter(Mandatory=$false)][string]$BaseReleaseIdOverride = "",
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoAbs = (Resolve-Path -Path $Repo).Path
$reqAbs = $Request
if (-not [System.IO.Path]::IsPathRooted($reqAbs)) { $reqAbs = Join-Path $repoAbs $Request }

$outLines = & py -3.11 -m args.foundry.customize_release_v1 --repo $repoAbs --request $reqAbs --base-release-id-override $BaseReleaseIdOverride 2>$null
$rc = $LASTEXITCODE

# must output exactly one JSON (pass-through)
$txt = ($outLines | Out-String).Trim()
Write-Output $txt
exit $rc