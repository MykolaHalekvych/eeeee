<#
ARGS STATUS (operator one-liner)

Prints:
- latest_run_id + age
- soak_gate last (from args/data/soak_status.json)
- soak_report 24h/7d last levels (from args/data/soak_report_24h.json, soak_report_7d.json)
- stop.flag presence
- scheduler heartbeat age
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"

$repo = "C:\Users\mukol\ARGS-Core-v1"
$data = Join-Path $repo "args\data"
$logs = Join-Path $repo "args\logs"

function AgeSec($path) {
  if (-not (Test-Path $path)) { return $null }
  $dt = (Get-Item $path).LastWriteTimeUtc
  return [math]::Round((([DateTime]::UtcNow - $dt).TotalSeconds), 1)
}

function ReadJson($path) {
  if (-not (Test-Path $path)) { return $null }
  try { return (Get-Content $path -Raw | ConvertFrom-Json) } catch { return $null }
}

$latestRunPath = Join-Path $data "latest_run_id.txt"
$latestPaths   = Join-Path $data "latest_paths.json"
$soakStatus    = Join-Path $data "soak_status.json"
$rep24         = Join-Path $data "soak_report_24h.json"
$rep7d         = Join-Path $data "soak_report_7d.json"
$stopFlag      = Join-Path $data "stop.flag"
$hb            = Join-Path $data "scheduler_heartbeat.txt"

$latestRun = if (Test-Path $latestRunPath) { (Get-Content $latestRunPath -ErrorAction SilentlyContinue | Select-Object -First 1).Trim() } else { "" }
$ageLatest = AgeSec $latestRunPath

$ss = ReadJson $soakStatus
$r24 = ReadJson $rep24
$r7 = ReadJson $rep7d

$ageHb = AgeSec $hb
$ageSoak = AgeSec $soakStatus
$age24 = AgeSec $rep24
$age7 = AgeSec $rep7d

$line = [ordered]@{
  ts_utc = ([DateTime]::UtcNow.ToString("s") + "Z")
  latest_run_id = $latestRun
  latest_run_age_s = $ageLatest
  stop_flag = (Test-Path $stopFlag)
  heartbeat_age_s = $ageHb
  soak_status_age_s = $ageSoak
  soak_level = if ($ss) { $ss.level } else { $null }
  soak_reason = if ($ss) { $ss.reason } else { $null }
  report24_age_s = $age24
  report24_level = if ($r24) { $r24.level } else { $null }
  report24_reason = if ($r24) { $r24.reason } else { $null }
  report7d_age_s = $age7
  report7d_level = if ($r7) { $r7.level } else { $null }
  report7d_reason = if ($r7) { $r7.reason } else { $null }
}

$line | ConvertTo-Json -Depth 6
