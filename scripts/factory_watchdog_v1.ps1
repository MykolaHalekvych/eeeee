param(
  [string]$Repo = ".",
  [string]$Python = "py -3.11",
  [string]$LatestOnly = "true"
)

$ErrorActionPreference = "Stop"

function EmitJsonAndExit([hashtable]$obj, [int]$code) {
  $obj.exit_code = $code
  $json = ($obj | ConvertTo-Json -Compress -Depth 20)
  Write-Output $json
  exit $code
}

$repoPath = (Resolve-Path -LiteralPath $Repo).Path
Set-Location -LiteralPath $repoPath

$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")

if (-not (Test-Path -LiteralPath ".\.args_engine_repo")) {
  EmitJsonAndExit @{ schema="factory_watchdog_v1"; ok=$false; ts_utc=$ts; error="ENG guard failed: missing .args_engine_repo" } 2
}

$latestFlag = $false
if ($LatestOnly -eq "true" -or $LatestOnly -eq "1") { $latestFlag = $true }

try {
  if ($latestFlag) {
    $cmd = "$Python -m args.foundry.watchdog_v0 --latest-only"
  } else {
    $cmd = "$Python -m args.foundry.watchdog_v0"
  }
  $out = Invoke-Expression $cmd
  $rc = $LASTEXITCODE

  EmitJsonAndExit @{
    schema="factory_watchdog_v1";
    ok=($rc -eq 0);
    ts_utc=$ts;
    repo=$repoPath;
    latest_only=$latestFlag;
    watchdog_json=$out;
  } $rc
}
catch {
  EmitJsonAndExit @{
    schema="factory_watchdog_v1";
    ok=$false;
    ts_utc=$ts;
    repo=$repoPath;
    error=$_.Exception.Message;
  } 2
}
