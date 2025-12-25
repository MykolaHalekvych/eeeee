param(
  [int]$IntervalSeconds = 300,
  [switch]$Once,
  [int]$MaxCycles = 0
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

$py = "py"
$pyArgs = @("-3.11", "-m")

$logDir = Join-Path $repoRoot "args\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$lockPath = Join-Path $logDir "auto_loop.lock"
if (Test-Path $lockPath) {
  Write-Host "auto_loop.lock exists -> another scheduler may be running. Exiting with code 0 (no-op)." -ForegroundColor Yellow
  Write-Host "Lock: $lockPath"
  exit 0
}

function Run-Step([string]$name, [string]$module, [string]$logFile) {
  $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  Add-Content -Path $logFile -Value ""
  Add-Content -Path $logFile -Value ("=" * 72)
  Add-Content -Path $logFile -Value ("[{0}] STEP: {1}  (py -3.11 -m {2})" -f $ts, $name, $module)
  Add-Content -Path $logFile -Value ("=" * 72)

  try {
    & $py @pyArgs $module 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Null
    $code = $LASTEXITCODE
  } catch {
    Add-Content -Path $logFile -Value ("EXCEPTION: {0}" -f $_.Exception.Message)
    $code = 99
  }

  Add-Content -Path $logFile -Value ("[{0}] STEP END: {1} exit_code={2}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $name, $code)
  return $code
}

try {
  $lockBody = "pid=$PID started_utc=$([DateTime]::UtcNow.ToString('s'))Z"
  Set-Content -Path $lockPath -Value $lockBody -Encoding UTF8

  $cycle = 0

  while ($true) {
    $cycle += 1
    $stamp = (Get-Date).ToString("yyyy-MM-dd_HH-mm-ss")
    $logFile = Join-Path $logDir ("auto_loop_{0}_cycle{1:D4}.log" -f $stamp, $cycle)

    Add-Content -Path $logFile -Value ("ARGS AUTO LOOP v1 | interval={0}s | cycle={1} | repo={2}" -f $IntervalSeconds, $cycle, $repoRoot)

    # 1) IBKR bundle (may fail if TWS/Gateway down; we still continue using last CSV)
    $c1 = Run-Step "IBKR bundle (contract+bars+meta)" "args.demo.demo_ibkr_refresh_hg_5m_bundle_v1" $logFile

    # 2) Paper loop (must succeed for intents/payload)
    $c2 = Run-Step "Paper loop" "args.demo.demo_paper_loop_v0" $logFile
    if ($c2 -ne 0) {
      Add-Content -Path $logFile -Value "Paper loop failed -> skipping intents/payload/sender this cycle."
      if ($Once) { break }
      if ($MaxCycles -gt 0 -and $cycle -ge $MaxCycles) { break }
      Start-Sleep -Seconds $IntervalSeconds
      continue
    }

    # 3) Intents (dry-run)
    $c3 = Run-Step "WA intents (dry-run)" "args.demo.demo_wa_v1_intents_from_latest_run" $logFile

    # 4) Payload (dry-run)
    $c4 = Run-Step "WA payload (dry-run)" "args.demo.demo_wa_v1_payload_from_latest_run" $logFile

    # 5) Sender (dry-run -> sendplan)
    $c5 = Run-Step "Sender (dry-run -> sendplan)" "args.demo.demo_ibkr_sender_dryrun_from_latest_run" $logFile

    Add-Content -Path $logFile -Value ""
    Add-Content -Path $logFile -Value ("CYCLE SUMMARY: bundle={0} paper={1} intents={2} payload={3} sender={4}" -f $c1, $c2, $c3, $c4, $c5)

    if ($Once) { break }
    if ($MaxCycles -gt 0 -and $cycle -ge $MaxCycles) { break }

    Start-Sleep -Seconds $IntervalSeconds
  }

} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $lockPath
}
exit 0

