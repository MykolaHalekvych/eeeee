<#
.SYNOPSIS
  Housekeeping / GC for logs and evidence.

DEFAULT: dry-run unless -ConfirmDelete YES is provided.
JSON-only stdout.

Exit codes:
  0 = OK
  1 = WARN (some deletions failed)
  2 = FAIL (script error)
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [int]$KeepDaysAutoLoopLogs = 14,
  [int]$KeepDaysOpsEvidence = 30,
  [string]$ConfirmDelete = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = (Resolve-Path $Repo).Path
$doDelete = ($ConfirmDelete -eq "YES")

function Collect-Files([string]$path, [datetime]$cutoff) {
  if (-not (Test-Path $path)) { return @() }
  return @(
    Get-ChildItem -Path $path -Recurse -File -Force -ErrorAction SilentlyContinue |
      Where-Object { $_.LastWriteTime -lt $cutoff -and $_.Name -notlike "*.lock" }
  )
}

function Sum-Bytes($files) {
  $sum = 0L
  foreach ($f in @($files)) { try { $sum += [int64]$f.Length } catch {} }
  return $sum
}

function Delete-Files($files, $bucket) {
  $deletedCount = 0
  $deletedBytes = 0L
  foreach ($f in @($files)) {
    try {
      $len = [int64]$f.Length
      Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop
      $deletedCount += 1
      $deletedBytes += $len
    } catch {
      $bucket.failed += @{ path=$f.FullName; error=($_ | Out-String) }
    }
  }
  return @{ deleted_count=$deletedCount; deleted_bytes=$deletedBytes }
}

$cutLogs = (Get-Date).AddDays(-$KeepDaysAutoLoopLogs)
$cutEv   = (Get-Date).AddDays(-$KeepDaysOpsEvidence)

$dirAutoLoop = Join-Path $repo "args\logs\auto_loop_5m"
$dirEvidence = Join-Path $repo "args\ops_evidence"

$report = @{
  schema="housekeeping_v1"
  ts_utc=(Get-Date).ToUniversalTime().ToString("o")
  repo=$repo
  dry_run = (-not $doDelete)

  auto_loop_logs = @{
    path=$dirAutoLoop
    keep_days=$KeepDaysAutoLoopLogs
    cutoff_local=$cutLogs.ToString("o")
    candidates_count=0
    candidates_bytes=0
    deleted_count=0
    deleted_bytes=0
    failed=@()
  }

  ops_evidence = @{
    path=$dirEvidence
    keep_days=$KeepDaysOpsEvidence
    cutoff_local=$cutEv.ToString("o")
    candidates_count=0
    candidates_bytes=0
    deleted_count=0
    deleted_bytes=0
    failed=@()
  }

  exit_code=0
  ok=$true
  error=$null
}

try {
  $filesLogs = Collect-Files $dirAutoLoop $cutLogs
  $filesEv   = Collect-Files $dirEvidence $cutEv

  $report.auto_loop_logs.candidates_count = @($filesLogs).Count
  $report.auto_loop_logs.candidates_bytes = (Sum-Bytes $filesLogs)

  $report.ops_evidence.candidates_count = @($filesEv).Count
  $report.ops_evidence.candidates_bytes = (Sum-Bytes $filesEv)

  if ($doDelete) {
    $r1 = Delete-Files $filesLogs $report.auto_loop_logs
    $report.auto_loop_logs.deleted_count = $r1.deleted_count
    $report.auto_loop_logs.deleted_bytes = $r1.deleted_bytes

    $r2 = Delete-Files $filesEv $report.ops_evidence
    $report.ops_evidence.deleted_count = $r2.deleted_count
    $report.ops_evidence.deleted_bytes = $r2.deleted_bytes

    $warn = ($report.auto_loop_logs.failed.Count -gt 0 -or $report.ops_evidence.failed.Count -gt 0)
    if ($warn) { $report.exit_code = 1; $report.ok = $false }
  }

} catch {
  $report.exit_code = 2
  $report.ok = $false
  $report.error = ($_ | Out-String)
}

$report | ConvertTo-Json -Depth 8
exit $report.exit_code
