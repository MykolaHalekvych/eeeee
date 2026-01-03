[CmdletBinding()]
param(
  [string]$Repo = "",
  [int]$TailLines = 2000,
  [int]$MinSecondsBetweenEvidence = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function _utc_iso() {
  return [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
}

function _sha256_text([string]$text) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $hash = $sha.ComputeHash($bytes)
    return ($hash | ForEach-Object { $_.ToString("x2") }) -join ""
  } finally {
    $sha.Dispose()
  }
}

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) { $Repo = (Get-Location).Path }
  $Repo = (Resolve-Path -LiteralPath $Repo).Path

  $eventsPath = Join-Path $Repo "args\data\ibkr_events_live.jsonl"
  $cursorPath = Join-Path $Repo "args\data\stage5_evidence.cursor.json"
  $outBase    = Join-Path $Repo "args\stage5_evidence"

  if (-not (Test-Path -LiteralPath $eventsPath)) {
    throw "EVENTS_FILE_NOT_FOUND: $eventsPath"
  }

  $fi = Get-Item -LiteralPath $eventsPath
  $size = [int64]$fi.Length
  $mtimeUtc = $fi.LastWriteTimeUtc.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")

  $tailArr = Get-Content -LiteralPath $eventsPath -Tail $TailLines -ErrorAction Stop
  $tailText = ($tailArr -join "`n")
  $tailSha = _sha256_text $tailText

  $cursor = $null
  if (Test-Path -LiteralPath $cursorPath) {
    try { $cursor = Get-Content -LiteralPath $cursorPath -Raw | ConvertFrom-Json } catch { $cursor = $null }
  }

  $nowUtc = [DateTime]::UtcNow
  $nowIso = $nowUtc.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")

  # Throttle
  if ($cursor -and $cursor.last_evidence_ts_utc) {
    try {
      $last = [DateTime]::Parse($cursor.last_evidence_ts_utc).ToUniversalTime()
      $dt = ($nowUtc - $last).TotalSeconds
      if ($dt -lt $MinSecondsBetweenEvidence) {
        $out = [ordered]@{
          schema="stage5_evidence_pack_v0"
          ok=$true
          status="THROTTLED"
          ts_utc=$nowIso
          since_last_seconds=[Math]::Round($dt,2)
          min_seconds_between_evidence=$MinSecondsBetweenEvidence
          events_path=$eventsPath
          last_events_size_bytes=$size
          last_events_mtime_utc=$mtimeUtc
          last_tail_sha256=$tailSha
        }
        ($out | ConvertTo-Json -Compress -Depth 6) | Write-Output
        exit 0
      }
    } catch {}
  }

    $changed = $true
  if ($cursor) {
    $prevSize  = 0
    try { $prevSize = [int64]$cursor.last_events_size_bytes } catch { $prevSize = 0 }
    $prevMtime = [string]$cursor.last_events_mtime_utc
    $prevTail  = [string]$cursor.last_tail_sha256
    $changed = ($prevSize -ne $size) -or ($prevMtime -ne $mtimeUtc)

  }

  if (-not $changed) {
    $out = [ordered]@{
      schema="stage5_evidence_pack_v0"
      ok=$true
      status="NO_CHANGE"
      ts_utc=$nowIso
      events_path=$eventsPath
      last_events_size_bytes=$size
      last_events_mtime_utc=$mtimeUtc
      last_tail_sha256=$tailSha
      cursor_path=$cursorPath
    }
    ($out | ConvertTo-Json -Compress -Depth 6) | Write-Output
    exit 0
  }

  New-Item -ItemType Directory -Force -Path $outBase | Out-Null
  $stamp = $nowUtc.ToString("yyyyMMddTHHmmssZ")
  $runDir = Join-Path $outBase $stamp
  New-Item -ItemType Directory -Force -Path $runDir | Out-Null

  $tailPath = Join-Path $runDir "events_tail.jsonl"
  Set-Content -LiteralPath $tailPath -Value $tailArr -Encoding UTF8

  $summaryPath = Join-Path $runDir "evidence_summary.json"
  $summary = [ordered]@{
    schema="stage5_terminal_evidence_summary_v0"
    ok=$true
    status="EVIDENCE_WRITTEN"
    ts_utc=$nowIso
    repo=$Repo
    out_dir=$runDir
    events_path=$eventsPath
    tail_path=$tailPath
    tail_lines=$TailLines
    tail_sha256=$tailSha
    last_events_size_bytes=$size
    last_events_mtime_utc=$mtimeUtc
    min_seconds_between_evidence=$MinSecondsBetweenEvidence
  }
  ($summary | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $summaryPath -Encoding UTF8

  $newCursor = [ordered]@{
    schema="stage5_evidence_cursor_v0"
    ts_utc=$nowIso
    last_out_dir=$runDir
    last_events_size_bytes=$size
    last_events_mtime_utc=$mtimeUtc
    last_tail_sha256=$tailSha
    last_evidence_ts_utc=$nowIso
  }
  ($newCursor | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $cursorPath -Encoding UTF8

  ($summary | ConvertTo-Json -Compress -Depth 8) | Write-Output
  exit 0
}
catch {
  $out = [ordered]@{
    schema="stage5_evidence_pack_v0"
    ok=$false
    status="FAIL"
    ts_utc=_utc_iso
    error=$_.Exception.Message
  }
  ($out | ConvertTo-Json -Compress -Depth 5) | Write-Output
  exit 2
}



