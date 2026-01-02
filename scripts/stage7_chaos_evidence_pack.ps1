
<#
STAGE7_CHAOS_EVIDENCE_PACK_V0 (PowerShell 5.1 safe)

Key fix:
- PS 5.1 + ErrorActionPreference=Stop may treat native stderr output as terminating errors.
- We temporarily set ErrorActionPreference=Continue around native invocation.

Uses dedicated lock per test:
  --lock-path <outDir>\lock_*.lock

Exit codes:
  0 = PASS
  1 = FAIL (a test returned non-zero)
  2 = INFRA_FAIL (exception / cannot invoke)
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function UtcStamp(){ (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ") }
function UtcIso(){ (Get-Date).ToUniversalTime().ToString("o") }

$repoRoot = (Resolve-Path -LiteralPath $Repo).Path
$eRoot = Join-Path $repoRoot "args\ops_evidence\chaos"
$ts = UtcStamp
$outDir = Join-Path $eRoot $ts
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$events = Join-Path $repoRoot "args\data\ibkr_events_live.jsonl"
$health = Join-Path $repoRoot "args\data\ops_health.json"
$logs   = Join-Path $repoRoot "args\logs"

$results = @()

function Run-Cmd([string]$name, [string[]]$cmd) {
  $stdout = Join-Path $outDir ("{0}_stdout.txt" -f $name)
  $stderr = Join-Path $outDir ("{0}_stderr.txt" -f $name)
  "" | Set-Content -LiteralPath $stdout -Encoding UTF8
  "" | Set-Content -LiteralPath $stderr -Encoding UTF8

  $t0 = Get-Date
  $rc = 2
  $infra = $false

  $prevEAP = $ErrorActionPreference
  try {
    Push-Location $repoRoot

    $exe = $cmd[0]
    $args = @()
    if ($cmd.Length -gt 1) { $args = $cmd[1..($cmd.Length-1)] }

    # CRITICAL: do not treat native stderr as terminating errors
    $ErrorActionPreference = "Continue"
    & $exe @args 1>> $stdout 2>> $stderr
    $rc = [int]$LASTEXITCODE
  }
  catch {
    $infra = $true
    $rc = 2
    try { Add-Content -LiteralPath $stderr -Value ("EXCEPTION: " + $_.Exception.Message) } catch { }
  }
  finally {
    $ErrorActionPreference = $prevEAP
    Pop-Location
  }

  $dt = ((Get-Date) - $t0).TotalSeconds

  # classify infra only on real exceptions; rc=2 is a normal FAIL for argparse/etc
  return @{ name=$name; exit_code=[int]$rc; infra=$infra; seconds=[Math]::Round($dt,2); stdout=$stdout; stderr=$stderr }
}

# Dedicated lock paths per test (no interference)
$lockReplay  = Join-Path $outDir "lock_replay.lock"
$lockMissing = Join-Path $outDir "lock_missing.lock"
$lockStale   = Join-Path $outDir "lock_stale.lock"

# 1) replay/reset-cursor
$results += Run-Cmd "replay_reset_cursor" @(
  "py","-3.11","-m","args_core.soak_stage7_v1",
  "--repo",$repoRoot,"--seconds","10","--reset-cursor",
  "--lock-path",$lockReplay
)

# 2) missing events file -> rename then run soak then restore
$bak = ""
try {
  if (Test-Path -LiteralPath $events) {
    $bak = ($events + ".bak_" + $ts)
    Move-Item -LiteralPath $events -Destination $bak -Force
  }
} catch { }

$results += Run-Cmd "missing_events_file" @(
  "py","-3.11","-m","args_core.soak_stage7_v1",
  "--repo",$repoRoot,"--seconds","10",
  "--lock-path",$lockMissing
)

try {
  if ($bak -and (Test-Path -LiteralPath $bak)) {
    Move-Item -LiteralPath $bak -Destination $events -Force
  }
} catch { }

# 3) stale lock takeover (create stale lock file)
try {
  New-Item -ItemType File -Force -Path $lockStale | Out-Null
  (Get-Item -LiteralPath $lockStale).LastWriteTime = (Get-Date).AddHours(-3)
} catch { }

$results += Run-Cmd "stale_lock_takeover" @(
  "py","-3.11","-m","args_core.soak_stage7_v1",
  "--repo",$repoRoot,"--seconds","10",
  "--lock-path",$lockStale
)

# snapshots
try { if (Test-Path -LiteralPath $health) { Copy-Item -LiteralPath $health -Destination (Join-Path $outDir "ops_health.json") -Force } } catch { }
try { if (Test-Path -LiteralPath $logs) { Copy-Item -LiteralPath (Join-Path $logs "*.log") -Destination $outDir -Force -ErrorAction SilentlyContinue } } catch { }

# decision
$fail = 0
$infraFail = 0
foreach ($r in $results) {
  if ($r.infra -eq $true) { $infraFail++ }
  if ([int]$r.exit_code -ne 0) { $fail++ }
}

$exitCode = 0
$ok = $true
$reason = "PASS"
if ($infraFail -gt 0) { $exitCode = 2; $ok = $false; $reason = "INFRA_FAIL" }
elseif ($fail -gt 0) { $exitCode = 1; $ok = $false; $reason = "FAIL" }

$summary = @{
  schema="stage7_chaos_evidence_v0"
  ts_utc=(UtcIso)
  repo=$repoRoot
  out_dir=$outDir
  reason=$reason
  tests=$results
  failed=$fail
  infra_failed=$infraFail
  ok=$ok
  exit_code=$exitCode
}

($summary | ConvertTo-Json -Compress) | Set-Content -LiteralPath (Join-Path $outDir "chaos_report.json") -Encoding UTF8
Write-Output ($summary | ConvertTo-Json -Compress)
exit $exitCode
