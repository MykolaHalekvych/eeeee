param(
  [string]$Repo = ".",
  [string]$Python = "py -3.11",
  [string]$ControlPlane = "control_plane.json",
  [string]$Product = "cicd_release_pack_v0",
  [string]$Factory = "local"
)

$ErrorActionPreference = "Stop"

function EmitJsonAndExit([hashtable]$obj, [int]$code) {
  $obj.exit_code = $code
  $json = ($obj | ConvertTo-Json -Compress -Depth 12)
  Write-Output $json
  exit $code
}

function ReadText([string]$path) {
  return Get-Content -LiteralPath $path -Raw -ErrorAction Stop
}

function WriteText([string]$path, [string]$text) {
  Set-Content -LiteralPath $path -Value $text -Encoding utf8 -ErrorAction Stop
}

# --- Resolve repo root ---
$repoPath = (Resolve-Path -LiteralPath $Repo).Path
Set-Location -LiteralPath $repoPath

$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")

# --- Guards ---
if (-not (Test-Path -LiteralPath ".\.args_engine_repo")) {
  EmitJsonAndExit @{ schema="factory_build_window_v1"; ok=$false; ts_utc=$ts; error="ENG guard failed: missing .args_engine_repo"; } 2
}

$stopFlag = ".\args\control\stop.flag"
if (Test-Path -LiteralPath $stopFlag) {
  EmitJsonAndExit @{ schema="factory_build_window_v1"; ok=$false; ts_utc=$ts; error="HALT: stop.flag present"; stop_flag=$stopFlag } 2
}

# --- Paths ---
$cpPath = (Resolve-Path -LiteralPath $ControlPlane).Path
$backupPath = Join-Path $repoPath "args\data\control_plane.backup.json"

# --- Backup control plane ---
try {
  $cpRaw = ReadText $cpPath
  WriteText $backupPath $cpRaw
} catch {
  EmitJsonAndExit @{ schema="factory_build_window_v1"; ok=$false; ts_utc=$ts; error=("control_plane backup failed: " + $_.Exception.Message) } 2
}

# --- Helper: set ALLOW_BUILD true/false deterministically ---
function Set-AllowBuild([bool]$enabled) {
  $obj = $cpRaw | ConvertFrom-Json
  if (-not $obj.engine) { $obj | Add-Member -NotePropertyName engine -NotePropertyValue (@{}) }
  if (-not $obj.engine.permissions) { $obj.engine | Add-Member -NotePropertyName permissions -NotePropertyValue (@{}) }
  $obj.engine.permissions.ALLOW_BUILD = $enabled
  # Keep other perms as-is; do not introduce ALLOW_EXPORT/APPLY/DELETE changes here.
  $new = ($obj | ConvertTo-Json -Depth 20)
  WriteText $cpPath $new
}

$planJson = $null
$buildJson = $null
$planRc = $null
$buildRc = $null

try {
  # Enable build for this window
  Set-AllowBuild $true

  # 1) plan (for trace)
  $planCmd = "$Python -m args.foundry.plan_v0 --control-plane .\$ControlPlane --product $Product --factory $Factory"
  $planJson = Invoke-Expression $planCmd
  $planRc = $LASTEXITCODE
  if ($planRc -ne 0) {
    throw "plan failed rc=$planRc"
  }

  # 2) build
  $buildCmd = "$Python -m args.foundry.build_v0 --control-plane .\$ControlPlane --product $Product --factory $Factory"
  $buildJson = Invoke-Expression $buildCmd
  $buildRc = $LASTEXITCODE
  if ($buildRc -ne 0) {
    throw "build failed rc=$buildRc"
  }

  EmitJsonAndExit @{
    schema="factory_build_window_v1";
    ok=$true;
    ts_utc=$ts;
    repo=$repoPath;
    product=$Product;
    factory=$Factory;
    control_plane=$ControlPlane;
    plan_json=$planJson;
    build_json=$buildJson;
  } 0
}
catch {
  # classify: if we have rc==1 => eval fail; rc==2 => infra/policy
  $err = $_.Exception.Message
  $code = 2
  if ($buildRc -eq 1 -or $planRc -eq 1) { $code = 1 }

  EmitJsonAndExit @{
    schema="factory_build_window_v1";
    ok=$false;
    ts_utc=$ts;
    repo=$repoPath;
    product=$Product;
    factory=$Factory;
    control_plane=$ControlPlane;
    error=$err;
    plan_rc=$planRc;
    build_rc=$buildRc;
    plan_json=$planJson;
    build_json=$buildJson;
  } $code
}
finally {
  # Always restore original control plane
  try {
    if (Test-Path -LiteralPath $backupPath) {
      $orig = ReadText $backupPath
      WriteText $cpPath $orig
    }
  } catch {
    # Last resort: don't throw; we must not break stdout contract
  }
}
