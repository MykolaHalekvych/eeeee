[CmdletBinding()]
param(
  [string]$Repo = "",
  [string]$EventsPath = "",
  [int]$TailLines = 5000,
  [int]$MinSecondsBetweenProof = 10,
  [int]$SeenKeysCap = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function _utc_iso() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") }

function _sha256_text([string]$text) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $hash = $sha.ComputeHash($bytes)
    return ($hash | ForEach-Object { $_.ToString("x2") }) -join ""
  } finally { $sha.Dispose() }
}

function _to_lc([object]$x) {
  if ($null -eq $x) { return "" }
  return ([string]$x).Trim().ToLowerInvariant()
}

function _terminal_kind($o) {
  $status = _to_lc (_safe_field $o "status")
  $event  = _to_lc (_safe_field $o "event")
  $type   = _to_lc (_safe_field $o "type")

  if ($status -in @("filled","fill")) { return "FILLED" }
  if ($status -in @("cancelled","canceled","cancel")) { return "CANCELLED" }
  if ($status -in @("rejected","reject")) { return "REJECTED" }

  if ($event -match "fill|exec" -or $type -match "fill|exec") { return "FILLED" }
  if ($event -match "cancel" -or $type -match "cancel") { return "CANCELLED" }
  if ($event -match "reject" -or $type -match "reject") { return "REJECTED" }

  return ""
}

function _safe_field($o, [string]$name) {
  try {
    if ($o.PSObject.Properties.Name -contains $name) { return $o.$name }
  } catch {}
  return $null
}

function _proof_key($o, [string]$kind) {
  $orderId = _safe_field $o "order_id"
  if (-not $orderId) { $orderId = _safe_field $o "orderId" }
  $permId  = _safe_field $o "permId"
  $execId  = _safe_field $o "execId"
  $ts      = _safe_field $o "ts_utc"
  if (-not $ts) { $ts = _safe_field $o "tsUtc" }

  $id = ""
  if ($orderId) { $id = "order:$orderId" }
  elseif ($permId) { $id = "perm:$permId" }
  elseif ($execId) { $id = "exec:$execId" }
  else { $id = "noid" }

  $x = ""
  if ($execId) { $x = "exec:$execId" }

  $t = ""
  if ($ts) { $t = [string]$ts }

  return "$kind|$id|$x|$t"
}

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) {
    $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
  } else {
    $Repo = (Resolve-Path -LiteralPath $Repo).Path
  }

  if ([string]::IsNullOrWhiteSpace($EventsPath)) {
    $EventsPath = Join-Path $Repo "args\data\ibkr_events_live.jsonl"
  } else {
    $EventsPath = (Resolve-Path -LiteralPath $EventsPath).Path
  }

  $OutBase    = Join-Path $Repo "args\stage5_terminal_proofs"
  $CursorPath = Join-Path $Repo "args\data\stage5_terminal_proof.cursor.json"

  if (-not (Test-Path -LiteralPath $EventsPath)) {
    throw "EVENTS_FILE_NOT_FOUND: $EventsPath"
  }

  $fi = Get-Item -LiteralPath $EventsPath
  $size = [int64]$fi.Length
  $mtimeUtc = $fi.LastWriteTimeUtc.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")

  $tailArr = Get-Content -LiteralPath $EventsPath -Tail $TailLines -ErrorAction Stop
  $tailText = ($tailArr -join "`n")
  $tailSha = _sha256_text $tailText

  $cursor = $null
  if (Test-Path -LiteralPath $CursorPath) {
    try { $cursor = Get-Content -LiteralPath $CursorPath -Raw | ConvertFrom-Json } catch { $cursor = $null }
  }

  $nowUtc = [DateTime]::UtcNow
  $nowIso = _utc_iso

  # throttle (UTC-safe)
  if ($cursor -and $cursor.last_proof_ts_utc) {
    try {
      $last = [DateTime]::Parse([string]$cursor.last_proof_ts_utc).ToUniversalTime()
      $dt = ($nowUtc - $last).TotalSeconds
      if ($dt -lt 0) { $dt = 0 }
      if ($dt -lt $MinSecondsBetweenProof) {
        $out = [ordered]@{
          schema="stage5_terminal_proof_pack_v0"
          ok=$true
          status="THROTTLED"
          ts_utc=$nowIso
          since_last_seconds=[Math]::Round($dt,2)
          min_seconds_between_proof=$MinSecondsBetweenProof
          events_path=$EventsPath
          last_events_size_bytes=$size
          last_events_mtime_utc=$mtimeUtc
          last_tail_sha256=$tailSha
        }
        ($out | ConvertTo-Json -Compress -Depth 6) | Write-Output
        exit 0
      }
    } catch { }
  }

  $seen = @()
  if ($cursor -and $cursor.seen_keys) {
    try { $seen = @($cursor.seen_keys) } catch { $seen = @() }
  }
  $seenSet = New-Object "System.Collections.Generic.HashSet[string]"
  foreach ($k in $seen) { [void]$seenSet.Add([string]$k) }

  $invalid = 0
  $hits = @()

  foreach ($line in $tailArr) {
    $o = $null
    try { $o = $line | ConvertFrom-Json -ErrorAction Stop } catch { $invalid += 1; continue }

    $kind = _terminal_kind $o
    if ([string]::IsNullOrWhiteSpace($kind)) { continue }

    $key = _proof_key $o $kind
    if ($seenSet.Contains($key)) { continue }

    $hits += [ordered]@{ kind=$kind; key=$key; line=$line }
  }

  if ($hits.Count -eq 0) {
    $out = [ordered]@{
      schema="stage5_terminal_proof_pack_v0"
      ok=$true
      status="NO_TERMINAL_EVENTS"
      ts_utc=$nowIso
      invalid_json_lines=$invalid
      tail_lines=$TailLines
      events_path=$EventsPath
      last_events_size_bytes=$size
      last_events_mtime_utc=$mtimeUtc
      last_tail_sha256=$tailSha
      cursor_path=$CursorPath
    }
    ($out | ConvertTo-Json -Compress -Depth 6) | Write-Output
    exit 0
  }

  New-Item -ItemType Directory -Force -Path $OutBase | Out-Null
  $stamp = $nowUtc.ToString("yyyyMMddTHHmmssZ")
  $runDir = Join-Path $OutBase ($stamp + "_terminal")
  New-Item -ItemType Directory -Force -Path $runDir | Out-Null

  # write artifacts
  $termPath = Join-Path $runDir "terminal_events.jsonl"
  ($hits | ForEach-Object { $_.line }) | Set-Content -LiteralPath $termPath -Encoding UTF8

  $tailPath = Join-Path $runDir "events_tail.jsonl"
  Set-Content -LiteralPath $tailPath -Value $tailArr -Encoding UTF8

  $sumPath = Join-Path $runDir "proof_summary.json"
  $summary = [ordered]@{
    schema="stage5_terminal_proof_summary_v0"
    ok=$true
    status="PROOF_WRITTEN"
    ts_utc=$nowIso
    repo=$Repo
    out_dir=$runDir
    events_path=$EventsPath
    terminal_events_path=$termPath
    tail_path=$tailPath
    tail_lines=$TailLines
    invalid_json_lines=$invalid
    proofs_count=$hits.Count
    proofs=@($hits | ForEach-Object { [ordered]@{kind=$_.kind; key=$_.key} })
    last_events_size_bytes=$size
    last_events_mtime_utc=$mtimeUtc
    last_tail_sha256=$tailSha
  }
  ($summary | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $sumPath -Encoding UTF8

  # update cursor
  foreach ($h in $hits) { [void]$seenSet.Add([string]$h.key) }
  $seenArr = @($seenSet)
  if ($seenArr.Count -gt $SeenKeysCap) {
    $seenArr = $seenArr[-$SeenKeysCap..-1]
  }

  $newCursor = [ordered]@{
    schema="stage5_terminal_proof_cursor_v0"
    ts_utc=$nowIso
    last_out_dir=$runDir
    last_events_size_bytes=$size
    last_events_mtime_utc=$mtimeUtc
    last_tail_sha256=$tailSha
    last_proof_ts_utc=$nowIso
    seen_keys=$seenArr
  }
  ($newCursor | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $CursorPath -Encoding UTF8

  ($summary | ConvertTo-Json -Compress -Depth 8) | Write-Output
  exit 0
}
catch {
  $out = [ordered]@{
    schema="stage5_terminal_proof_pack_v0"
    ok=$false
    status="FAIL"
    ts_utc=_utc_iso
    error=$_.Exception.Message
  }
  ($out | ConvertTo-Json -Compress -Depth 6) | Write-Output
  exit 2
}

