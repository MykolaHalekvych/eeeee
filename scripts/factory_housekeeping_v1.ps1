param(
  [string]$Repo = ".",
  [string]$Python = "py -3.11",
  [int]$KeepLast = 3,
  [int]$MaxAgeDays = 30,
  [string]$ConfirmDelete = "NO"
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
  EmitJsonAndExit @{ schema="factory_housekeeping_v1"; ok=$false; ts_utc=$ts; error="ENG guard failed: missing .args_engine_repo" } 2
}

$stopFlag = ".\args\control\stop.flag"
if (Test-Path -LiteralPath $stopFlag) {
  EmitJsonAndExit @{ schema="factory_housekeeping_v1"; ok=$false; ts_utc=$ts; error="HALT: stop.flag present"; stop_flag=$stopFlag } 2
}

try {
  $cmd = "$Python -m args.foundry.housekeeping_v0 --keep-last $KeepLast --max-age-days $MaxAgeDays --confirm-delete $ConfirmDelete"
  $out = Invoke-Expression $cmd
  $rc = $LASTEXITCODE

  EmitJsonAndExit @{
    schema="factory_housekeeping_v1";
    ok=($rc -eq 0);
    ts_utc=$ts;
    repo=$repoPath;
    keep_last=$KeepLast;
    max_age_days=$MaxAgeDays;
    confirm_delete=$ConfirmDelete;
    housekeeping_json=$out;
  } $rc
}
catch {
  EmitJsonAndExit @{ schema="factory_housekeeping_v1"; ok=$false; ts_utc=$ts; repo=$repoPath; error=$_.Exception.Message } 2
}
