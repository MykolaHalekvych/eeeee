<#
ARGS IBKR Tap Once (unattended-safe) v0.2

- Avoid reserved $Host => -IbHost
- Always runs from RepoRoot
- Writes a summary JSON for EVERY run (even early-exit/lock)
- Writes stdout/stderr logs (empty files are created)
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [int]$Seconds = 55,
  [string]$IbHost = "127.0.0.1",
  [int]$Port = 7497,
  [int]$ClientId = 77
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function UtcIso() { (Get-Date).ToUniversalTime().ToString("o") }
function UtcStamp() { (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ") }

$repoRoot = (Resolve-Path -LiteralPath $Repo).Path
$logsDir  = Join-Path $repoRoot "args\logs"
$dataDir  = Join-Path $repoRoot "args\data"
New-Item -ItemType Directory -Force -Path $logsDir,$dataDir | Out-Null

$tsStamp = UtcStamp
$tsIso   = UtcIso

$stdoutLog = Join-Path $logsDir ("ibkr_tap_once_stdout_{0}.log" -f $tsStamp)
$stderrLog = Join-Path $logsDir ("ibkr_tap_once_stderr_{0}.log" -f $tsStamp)
$summary   = Join-Path $logsDir ("ibkr_tap_once_summary_{0}.json" -f $tsStamp)

# create empty logs up-front so they always exist
"" | Set-Content -LiteralPath $stdoutLog -Encoding UTF8
"" | Set-Content -LiteralPath $stderrLog -Encoding UTF8

function Write-Summary([bool]$ok, [int]$exitCode, [string]$reason, [string]$extra="") {
  $obj = @{
    schema   = "ibkr_tap_once_summary_v0"
    ts_utc   = $tsIso
    ok       = $ok
    exit_code= $exitCode
    reason   = $reason
    extra    = $extra
    repo     = $repoRoot
    stdout   = $stdoutLog
    stderr   = $stderrLog
    ib_host  = $IbHost
    port     = $Port
    client_id= $ClientId
    seconds  = $Seconds
  }
  ($obj | ConvertTo-Json -Compress) | Set-Content -LiteralPath $summary -Encoding UTF8
}

# stop flag only (tap is READ-ONLY; safe_mode should NOT block ingest)
$stopCandidates = @(
  (Join-Path $repoRoot "stop.flag"),
  (Join-Path $logsDir  "stop.flag"),
  (Join-Path $dataDir  "stop.flag")
)
foreach ($p in $stopCandidates) {
  if (Test-Path -LiteralPath $p) {
    Write-Summary $true 0 "STOP_FLAG" $p
    exit 0
  }
}

# lock
$lockPath = Join-Path $logsDir "ibkr_tap_once.lock"
try {
  if (Test-Path -LiteralPath $lockPath) {
    $age = ((Get-Date) - (Get-Item -LiteralPath $lockPath).LastWriteTime).TotalSeconds
    if ($age -lt 120) {
      Write-Summary $true 0 "ACTIVE_LOCK" ("age_s=" + [Math]::Round($age,2))
      exit 0
    }
  }
  Set-Content -LiteralPath $lockPath -Value $PID -Encoding ASCII
} catch {
  # continue without lock
}

# locate py launcher robustly
$py = "py"
try { $py = (Get-Command py -ErrorAction Stop).Source } catch { }

try {
  Push-Location $repoRoot

  $args = @(
    "-3.11", "-m", "args_core.ibkr_event_tap_v0",
    "--host", $IbHost,
    "--port", "$Port",
    "--client-id", "$ClientId",
    "--seconds", "$Seconds"
  )

  $p = Start-Process -FilePath $py -ArgumentList $args -NoNewWindow -Wait -PassThru `
      -RedirectStandardOutput $stdoutLog `
      -RedirectStandardError  $stderrLog

  $exitCode = [int]$p.ExitCode
  if ($exitCode -eq 0) {
    Write-Summary $true 0 "DONE" ""
  } else {
    Write-Summary $false $exitCode "TAP_FAILED" ""
  }
  exit $exitCode
}
catch {
  Add-Content -LiteralPath $stderrLog -Value ("EXCEPTION: " + $_.Exception.Message)
  Write-Summary $false 2 "EXCEPTION" ($_.Exception.Message)
  exit 2
}
finally {
  Pop-Location
  try { Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue } catch { }
}
