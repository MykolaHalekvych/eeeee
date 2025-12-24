param(
  [string]$Tag = ""
)

$ErrorActionPreference = "Stop"

# Repo root = folder where this script lives (../)
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

# Timestamp
$ts = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
if ([string]::IsNullOrWhiteSpace($Tag)) {
  $snapName = "ARGS-Core-v1_SNAPSHOT_$ts"
  $logPrefix = "run_$ts"
} else {
  $safeTag = ($Tag -replace "[^a-zA-Z0-9_\-]", "_")
  $snapName = "ARGS-Core-v1_SNAPSHOT_${ts}_$safeTag"
  $logPrefix = "run_${ts}_$safeTag"
}

# 1) Reset copy-policy to baseline (safe default)
Copy-Item -Path .\args\data\invariants_hg_v0.yaml -Destination .\args\data\invariants_hg_v0_copy.yaml -Force

# 2) Ensure logs dir
New-Item -ItemType Directory -Force -Path .\args\logs | Out-Null

# 3) Run sanity suite + save logs
py -3.11 -m args.demo.demo_regression  | Tee-Object -FilePath (Join-Path .\args\logs "$logPrefix`_regression.txt")
py -3.11 -m args.demo.demo_policy_diff | Tee-Object -FilePath (Join-Path .\args\logs "$logPrefix`_policy_diff.txt")
py -3.11 -m args.demo.demo_replay      | Tee-Object -FilePath (Join-Path .\args\logs "$logPrefix`_replay.txt")
py -3.11 -m args.demo.demo_meta_audit  | Tee-Object -FilePath (Join-Path .\args\logs "$logPrefix`_meta_audit.txt")

# NOTE: demo_ma_eval appends to events.jsonl; keep it optional/off by default.

# 4) Snapshot repo folder (sibling folder)
$parent = Split-Path $RepoRoot -Parent
$dest = Join-Path $parent $snapName
Copy-Item -Path $RepoRoot -Destination $dest -Recurse -Force

Write-Host "CHECKPOINT OK"
Write-Host ("Snapshot: " + $dest)
Write-Host ("Logs: " + (Join-Path $RepoRoot "args\logs"))
