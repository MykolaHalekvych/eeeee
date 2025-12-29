# scripts/ops_loop_5m_stage6c.ps1
# Stage 6C: refresh open-orders snapshot BEFORE running existing loop.
# Safe-by-default: no manual trading; just refresh snapshot + run existing ops loop.

param(
  [int]$SnapshotTimeoutS = 25,
  [int]$SnapshotWaitS = 5,
  [string]$SnapshotOut = ".\args\data\ibkr_open_orders_live.jsonl",
  [string]$InnerLoop = ".\scripts\auto_loop_5m.ps1"
)

$ErrorActionPreference = "Stop"

function Log($msg) {
  $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  $line = "[$ts] $msg"
  Write-Host $line
  try { Add-Content -Path $LogPath -Value $line -Encoding UTF8 } catch {}
}
# Always run from repo root
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

$LogDir = Join-Path $Repo "args\logs"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir "ops_stage6c.log"
Log "Stage6C wrapper start"
Log "Repo: $Repo"

# 1) Refresh open orders snapshot
Log "Refresh open orders snapshot -> $SnapshotOut"
$cmd = @(
  "py", "-3.11", "-m", "args.ibkr.ibkr_open_orders_snapshotter_v0",
  "--host", "127.0.0.1",
  "--port", "7497",
  "--client-id", "11",
  "--timeout-s", "$SnapshotTimeoutS",
  "--wait-s", "$SnapshotWaitS",
  "--out", "$SnapshotOut"
)

try {
  & $cmd[0] $cmd[1] $cmd[2] $cmd[3] $cmd[4] $cmd[5] $cmd[6] $cmd[7] $cmd[8] $cmd[9] $cmd[10] $cmd[11] $cmd[12] $cmd[13] $cmd[14] $cmd[15] $cmd[16] $cmd[17] $cmd[18] $cmd[19] $cmd[20]
  $exit = $LASTEXITCODE
  Log "Snapshotter exit_code=$exit"
try {
  if (Test-Path $SnapshotOut) {
    $mt = (Get-Item $SnapshotOut).LastWriteTime
    Log "Snapshot mtime: $mt"
  } else {
    Log "Snapshot missing after refresh: $SnapshotOut"
  }
} catch {
  Log "Snapshot mtime read error: $(.Exception.Message)"
}
} catch {
  Log "Snapshotter exception: $($_.Exception.Message)"
  $exit = 99
}

# Fail-soft: even if snapshotter fails, still run inner loop (MA/Live safety should protect)
if ($exit -ne 0) {
  Log "WARNING: snapshot refresh failed (exit_code=$exit). Continuing to inner loop."
}

# 2) Run existing inner loop (do not change behavior)
if (-not (Test-Path $InnerLoop)) {
  throw "Inner loop script not found: $InnerLoop"
}

Log "Run inner loop: $InnerLoop"
powershell -ExecutionPolicy Bypass -File $InnerLoop

$exit2 = $LASTEXITCODE
Log "Inner loop exit_code=$exit2"

Log "Stage6C wrapper end"
exit $exit2

