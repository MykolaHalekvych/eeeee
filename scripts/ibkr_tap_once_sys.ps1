[CmdletBinding()]
param(
  [string]$Repo = "",
  [string]$Host = "",
  [int]$Port = 0,
  [int]$ClientId = 77,
  [int]$Seconds = 55
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function _utc() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") }

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) {
    $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
  } else {
    $Repo = (Resolve-Path -LiteralPath $Repo).Path
  }

  # Prefer python311 explicitly (works under SYSTEM even if py launcher lacks 3.11)
  $py311 = "C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe"
  $py = $null
  if (Test-Path -LiteralPath $py311) { $py = $py311 }
  else { $py = "$env:WINDIR\py.exe" }

  # Host/Port from config if present
  $connPath = Join-Path $Repo "args\data\ibkr_connection_v0.json"
  if ((-not $Host) -or ($Port -le 0)) {
    if (Test-Path -LiteralPath $connPath) {
      try {
        $c = Get-Content -LiteralPath $connPath -Raw | ConvertFrom-Json
        if (-not $Host) { $Host = [string]$c.host }
        if ($Port -le 0) { $Port = [int]$c.port }
        if ($c.client_id) { $ClientId = [int]$c.client_id }
      } catch {}
    }
  }

  if (-not $Host) { $Host = "localhost" }
  if ($Port -le 0) { $Port = 7497 }

  $eventsPath = Join-Path $Repo "args\data\ibkr_events_live.jsonl"
  $healthPath = Join-Path $Repo "args\data\ibkr_event_tap_health.json"

  $beforeSize = 0
  $beforeMtime = ""
  if (Test-Path -LiteralPath $eventsPath) {
    $fi = Get-Item -LiteralPath $eventsPath
    $beforeSize = [int64]$fi.Length
    $beforeMtime = $fi.LastWriteTimeUtc.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
  }

  Push-Location $Repo
  try {
    # Build args (no -3.11 here, because we call python.exe directly)
    $args = @(
      "-m", "args_core.ibkr_event_tap_v0",
      "--host", $Host,
      "--port", "$Port",
      "--client-id", "$ClientId",
      "--seconds", "$Seconds"
    )

    Write-Output ("PY_EXE=" + $py)
    Write-Output ("PY_ARGS=" + ($args -join " "))

    & $py @args
    $rc = $LASTEXITCODE
  } finally {
    Pop-Location
  }

  $afterSize = $beforeSize
  $afterMtime = $beforeMtime
  if (Test-Path -LiteralPath $eventsPath) {
    $fi2 = Get-Item -LiteralPath $eventsPath
    $afterSize = [int64]$fi2.Length
    $afterMtime = $fi2.LastWriteTimeUtc.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
  }

  $health = [ordered]@{
    schema="ibkr_event_tap_health_v1"
    ts_utc=_utc
    ok=($rc -eq 0)
    exit_code=$rc
    run_as=$env:USERNAME
    repo=$Repo
    py_exe=$py
    host=$Host
    port=$Port
    client_id=$ClientId
    seconds=$Seconds
    events_path=$eventsPath
    events_before_size=$beforeSize
    events_after_size=$afterSize
    events_delta_bytes=($afterSize - $beforeSize)
    events_before_mtime_utc=$beforeMtime
    events_after_mtime_utc=$afterMtime
  }

  ($health | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $healthPath -Encoding UTF8
  Write-Output ("HEALTH_WRITTEN=" + $healthPath)
  Write-Output ("EXITCODE=" + $rc)
  exit $rc
}
catch {
  $err = $_.Exception.Message
  $out = [ordered]@{
    schema="ibkr_event_tap_health_v1"
    ts_utc=_utc
    ok=$false
    exit_code=2
    run_as=$env:USERNAME
    error=$err
  }
  try {
    if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
    $healthPath = Join-Path $Repo "args\data\ibkr_event_tap_health.json"
    ($out | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $healthPath -Encoding UTF8
  } catch {}
  ($out | ConvertTo-Json -Compress -Depth 6) | Write-Output
  exit 2
}
