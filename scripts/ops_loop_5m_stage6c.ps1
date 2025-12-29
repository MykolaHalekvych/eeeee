# scripts/ops_loop_5m_stage6c.ps1
# Stage 6C (OPS wrapper)
# - Single-flight wrapper (OS-level lock file).
# - Pause via args\data\stop.flag (UI control plane).
# - Best-effort snapshot refresh (retry) then run inner loop.
# - Safe-by-default: orchestration only.
# - Logs: args\logs\ops_stage6c.log (append), run-id tagged.

param(
  [int]$SnapshotTimeoutS = 25,
  [int]$SnapshotWaitS    = 5,

  [string]$SnapshotOut   = ".\args\data\ibkr_open_orders_live.jsonl",
  [string]$InnerLoop     = ".\scripts\auto_loop_5m.ps1",

  [string]$IbHost        = "localhost",
  [int]$IbPort           = 7497,
  [int]$IbClientId       = 11,

  [string]$PythonExe     = "py",
  [string]$PythonVerArg  = "-3.11",

  [int]$SnapshotMaxAttempts = 2,
  [int]$SnapshotRetryDelayS = 2,

  [string]$WrapperLockFile = ".\args\data\ops_stage6c.lock",
  [string]$StopFlagFile    = ".\args\data\stop.flag",
  [string]$MainLogFile     = ".\args\logs\ops_stage6c.log"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Ensure-Dir([string]$p) {
  if (-not (Test-Path -LiteralPath $p)) {
    New-Item -ItemType Directory -Path $p -Force | Out-Null
  }
}

function New-RunId {
  return ("{0:yyyyMMdd_HHmmssfff}_pid{1}" -f (Get-Date), $PID)
}

function Resolve-RepoPath([string]$repoRoot, [string]$p) {
  if ([string]::IsNullOrWhiteSpace($p)) { return $p }
  if ([System.IO.Path]::IsPathRooted($p)) { return $p }
  return (Join-Path $repoRoot $p)
}

function Acquire-ExclusiveLock([string]$LockPath, [string]$runId) {
  Ensure-Dir (Split-Path -Parent $LockPath)
  try {
    $fs = [System.IO.File]::Open(
      $LockPath,
      [System.IO.FileMode]::OpenOrCreate,
      [System.IO.FileAccess]::ReadWrite,
      [System.IO.FileShare]::None
    )

    $meta = @{
      run_id    = $runId
      pid       = $PID
      start_utc = (Get-Date).ToUniversalTime().ToString("o")
      user      = "$env:USERDOMAIN\$env:USERNAME"
      host      = $env:COMPUTERNAME
      script    = $PSCommandPath
    } | ConvertTo-Json -Compress

    $fs.SetLength(0)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($meta)
    $fs.Write($bytes, 0, $bytes.Length)
    $fs.Flush()
    return $fs
  }
  catch [System.IO.IOException] { return $null }
}

# Repo root
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Repo

$RunId = New-RunId
$StartedAt = Get-Date

# Logging
$logFull = Resolve-RepoPath $Repo $MainLogFile
Ensure-Dir (Split-Path -Parent $logFull)

function Log([string]$msg) {
  $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss.fff")
  $line = "[$ts] [$RunId] $msg"
  Write-Host $line
  try { Add-Content -LiteralPath $logFull -Value $line -Encoding UTF8 } catch {}
}

$lockHandle = $null
$exitCode = 99

try {
  # Single-flight lock
  $lockFull = Resolve-RepoPath $Repo $WrapperLockFile
  $lockHandle = Acquire-ExclusiveLock $lockFull $RunId
  if (-not $lockHandle) {
    $exitCode = 0
    return
  }

  Log "Stage6C START repo=$Repo ibhost=$IbHost ibport=$IbPort clientId=$IbClientId"

  # Stop flag (UI kill-switch)
  $stopFlagFull = Resolve-RepoPath $Repo $StopFlagFile
  if (Test-Path -LiteralPath $stopFlagFull) {
    Log "STOP.FLAG present -> skipping run (no snapshot, no inner loop)"
    $exitCode = 0
    return
  }

  # Snapshot refresh (best-effort)
  $snapshotOutFull = Resolve-RepoPath $Repo $SnapshotOut
  Ensure-Dir (Split-Path -Parent $snapshotOutFull)

  $snapExit = 99
  for ($attempt = 1; $attempt -le $SnapshotMaxAttempts; $attempt++) {
    Log "Snapshot attempt $attempt/$SnapshotMaxAttempts -> $snapshotOutFull"
    try {
      $pyArgs = @()
      if (-not [string]::IsNullOrWhiteSpace($PythonVerArg)) { $pyArgs += $PythonVerArg }

      $pyArgs += @(
        "-m","args.ibkr.ibkr_open_orders_snapshotter_v0",
        "--host",$IbHost,
        "--port","$IbPort",
        "--client-id","$IbClientId",
        "--timeout-s","$SnapshotTimeoutS",
        "--wait-s","$SnapshotWaitS",
        "--out",$snapshotOutFull
      )

      Log "Snapshot cmd: $PythonExe $($pyArgs -join ' ')"
      & $PythonExe @pyArgs
      $snapExit = $LASTEXITCODE
      Log "Snapshotter exit_code=$snapExit"
      if ($snapExit -eq 0) { break }
    }
    catch {
      Log "Snapshotter exception: $($_.Exception.Message)"
      $snapExit = 99
    }

    if ($attempt -lt $SnapshotMaxAttempts) { Start-Sleep -Seconds $SnapshotRetryDelayS }
  }

  if (Test-Path -LiteralPath $snapshotOutFull) {
    $it = Get-Item -LiteralPath $snapshotOutFull
    Log "Snapshot stat: size=$($it.Length) mtime=$($it.LastWriteTime)"
  } else {
    Log "Snapshot missing after refresh: $snapshotOutFull"
  }

  if ($snapExit -ne 0) { Log "WARNING: snapshot failed (exit_code=$snapExit). Continuing to inner loop." }

  # Inner loop
  $innerFull = Resolve-RepoPath $Repo $InnerLoop
  if (-not (Test-Path -LiteralPath $innerFull)) { throw "Inner loop not found: $innerFull" }

  $pwshExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
  Log "Run inner loop: $innerFull"
  & $pwshExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $innerFull

  $exitCode = $LASTEXITCODE
  Log "Inner loop exit_code=$exitCode"

  $elapsed = [math]::Round(((Get-Date) - $StartedAt).TotalSeconds, 2)
  Log "Stage6C END duration_s=$elapsed"
}
catch {
  Log "FATAL: $($_.Exception.ToString())"
  $exitCode = 99
}
finally {
  if ($lockHandle) { try { $lockHandle.Dispose() } catch {} }
}

exit $exitCode
