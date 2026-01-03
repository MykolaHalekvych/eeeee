[CmdletBinding()]
param(
  [string]$Repo = "",
  [string]$IbHost = "",
  [int]$Port = 0,
  [int]$ClientId = 88,
  [int]$ConnectTimeoutSec = 8,
  [int]$TimeoutSec = 25,
  [int]$WaitSec = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function _utc() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") }

function _load_json([string]$p) {
  if (Test-Path -LiteralPath $p) { try { return (Get-Content -LiteralPath $p -Raw | ConvertFrom-Json) } catch { return $null } }
  return $null
}

function _pick_python() {
  $py311 = "C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe"
  if (Test-Path -LiteralPath $py311) { return [ordered]@{ exe=$py311; prefix=@() } }
  return [ordered]@{ exe="C:\Windows\py.exe"; prefix=@("-3.11") }
}

function _tail_text([string]$p, [int]$maxChars=1200) {
  if (-not (Test-Path -LiteralPath $p)) { return "" }
  $t = (Get-Content -LiteralPath $p -Raw)
  if (-not $t) { return "" }
  if ($t.Length -le $maxChars) { return $t.Trim() }
  return ($t.Substring($t.Length - $maxChars)).Trim()
}

function _run_py_capture([string]$exe, [object[]]$argv, [string]$stdoutPath, [string]$stderrPath) {
  Remove-Item $stdoutPath -ErrorAction SilentlyContinue
  Remove-Item $stderrPath -ErrorAction SilentlyContinue
  $out = & $exe @argv 2> $stderrPath
  $rc = $LASTEXITCODE
  $out | Set-Content -LiteralPath $stdoutPath -Encoding UTF8
  return $rc
}

function _parse_jsonl([string]$p) {
  $objs = @(); $invalid = 0
  if (-not (Test-Path -LiteralPath $p)) { return [ordered]@{ objs=@(); invalid=0 } }
  foreach ($line in Get-Content -LiteralPath $p) {
    $t = ([string]$line).Trim()
    if (-not $t.StartsWith("{")) { continue }
    try { $objs += ($t | ConvertFrom-Json) } catch { $invalid += 1 }
  }
  return [ordered]@{ objs=$objs; invalid=$invalid }
}

function _try_open_orders([string]$exe, [object[]]$prefix, [string]$ibHost, [int]$port, [int]$clientId, [int]$timeoutS, [int]$waitS, [string]$outJsonl, [string]$stdoutPath, [string]$stderrPath) {

  $mods = @("args.ibkr.ibkr_open_orders_snapshotter_v0")

  foreach ($m in $mods) {

    # two variants: with client-id and without (some CLIs may not support it)
    $variants = @(
      @("--host",$ibHost,"--port","$port","--client-id","$clientId","--timeout-s","$timeoutS","--wait-s","$waitS","--out",$outJsonl),
      @("--host",$ibHost,"--port","$port","--timeout-s","$timeoutS","--wait-s","$waitS","--out",$outJsonl)
    )

    foreach ($v in $variants) {
      Remove-Item $outJsonl -ErrorAction SilentlyContinue
      Remove-Item $stdoutPath -ErrorAction SilentlyContinue
      Remove-Item $stderrPath -ErrorAction SilentlyContinue

      $argv = @($prefix + @("-m",$m) + $v)
      $rc = _run_py_capture $exe $argv $stdoutPath $stderrPath

      if ($rc -eq 0 -and (Test-Path -LiteralPath $outJsonl)) {
        return [ordered]@{
          ok=$true
          module=$m
          exit_code=0
          argv_used=@($argv)
          stderr_tail=""
        }
      }

      $tail = _tail_text $stderrPath
      # If CLI mismatch (unrecognized args), try next variant/module
      if ($tail -match "unrecognized arguments" -or $tail -match "usage:" -or $tail -match "error:") {
        # continue trying other variant/module
      } else {
        # unknown failure -> return now
        return [ordered]@{ ok=$false; module=$m; exit_code=$rc; argv_used=@($argv); stderr_tail=$tail }
      }
    }

    # if all variants failed for this module, continue to next module
  }

  return [ordered]@{ ok=$false; module=$mods[-1]; exit_code=2; argv_used=@(); stderr_tail="all variants failed" }
}

# Always write latest (even on FAIL)
function _write_latest([string]$latestPath, [object]$obj) {
  try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $latestPath) | Out-Null
    ($obj | ConvertTo-Json -Depth 14) | Set-Content -LiteralPath $latestPath -Encoding UTF8
  } catch {}
}

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
  else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }

  $conn = _load_json (Join-Path $Repo "args\data\ibkr_connection_v0.json")
  if (-not $IbHost) { try { $IbHost = [string]$conn.host } catch {} }
  if ($Port -le 0)  { try { $Port = [int]$conn.port } catch {} }
  if (-not $IbHost) { $IbHost = "localhost" }
  if ($Port -le 0)  { $Port = 7497 }

  $py = _pick_python
  $pyExe  = [string]$py.exe
  $prefix = @($py.prefix)

  $base = Join-Path $Repo "args\ops_evidence\reconcile"
  New-Item -ItemType Directory -Force -Path $base | Out-Null
  $stamp = ([DateTime]::UtcNow).ToString("yyyyMMddTHHmmssZ")
  $runDir = Join-Path $base $stamp
  New-Item -ItemType Directory -Force -Path $runDir | Out-Null

  $latest = Join-Path $Repo "args\data\reconcile_evidence_latest.json"
  $sumPath = Join-Path $runDir "reconcile_summary.json"

  # --- positions snapshot ---
  $posJson = Join-Path $runDir "positions_snapshot.json"
  $posErr  = Join-Path $runDir "positions_snapshot.stderr.txt"
  $posArgv = @($prefix + @(
    "-m","args.ibkr.ibkr_positions_snapshotter_v0",
    "--host",$IbHost,"--port","$Port","--client-id","$ClientId",
    "--connect-timeout-s","$ConnectTimeoutSec","--timeout-s","$TimeoutSec"
  ))
  $posRc = _run_py_capture $pyExe $posArgv $posJson $posErr

  $posOk=$false; $posRows=@(); $posErrMsg=$null
  if ($posRc -eq 0 -and (Test-Path -LiteralPath $posJson)) {
    try {
      $po = Get-Content -LiteralPath $posJson -Raw | ConvertFrom-Json
      $posOk = [bool]$po.ok
      try { $posRows = @($po.rows) } catch { $posRows=@() }
      try { $posErrMsg = [string]$po.error } catch { $posErrMsg=$null }
    } catch { $posOk=$false; $posErrMsg="positions_parse_failed" }
  }

  $posNonZero = @($posRows | Where-Object { $_.position -ne 0 })
  $posSymbols = @()
  foreach ($r in $posNonZero) { try { $posSymbols += [string]$r.symbol } catch {} }
  $posSymbols = @($posSymbols | Where-Object { $_ } | ForEach-Object { ([string]$_).Trim().ToUpperInvariant() } | Sort-Object -Unique)

  # --- open orders snapshot ---
  $ordJsonl = Join-Path $runDir "open_orders_snapshot.jsonl"
  $ordStd   = Join-Path $runDir "open_orders_snapshot.stdout.txt"
  $ordErr   = Join-Path $runDir "open_orders_snapshot.stderr.txt"

  $ordTry = _try_open_orders $pyExe $prefix $IbHost $Port $ClientId $TimeoutSec $WaitSec $ordJsonl $ordStd $ordErr
  $ordOk = [bool]$ordTry.ok

  $ordParsed = _parse_jsonl $ordJsonl
  $orders = @($ordParsed.objs)
  $ordersInvalid = [int]$ordParsed.invalid

  $ordSymbols=@()
  foreach ($o in $orders) { try { if ($o.PSObject.Properties.Name -contains "symbol") { $ordSymbols += [string]$o.symbol } } catch {} }
  $ordSymbols = @($ordSymbols | Where-Object { $_ } | ForEach-Object { ([string]$_).Trim().ToUpperInvariant() } | Sort-Object -Unique)

  $statusCounts=@{}
  foreach ($o in $orders) {
    $st="UNKNOWN"
    try { if ($o.PSObject.Properties.Name -contains "status") { $st=[string]$o.status } } catch {}
    if (-not $statusCounts.ContainsKey($st)) { $statusCounts[$st]=0 }
    $statusCounts[$st]+=1
  }

  $issues=@(); $warns=@()
  if ($posRc -ne 0) { $issues += ("positions_snapshot_exit=" + $posRc) }
  if (-not $posOk)  { $issues += "positions_ok=false" }
  if ($posErrMsg)   { $warns  += ("positions_error=" + $posErrMsg) }

  if (-not $ordOk)  { $issues += ("open_orders_ok=false module=" + [string]$ordTry.module + " exit=" + [string]$ordTry.exit_code) }
  if ($ordersInvalid -gt 0) { $warns += ("open_orders_invalid_json_lines=" + $ordersInvalid) }

  $exit=0; $status="OK"
  if ($issues.Count -gt 0) { $exit=2; $status="FAIL" }
  elseif ($warns.Count -gt 0) { $exit=1; $status="WARN" }

  $summary=[ordered]@{
    schema="stage5_reconcile_evidence_pack_v1"
    ts_utc=_utc
    status=$status
    exit_code=$exit
    repo=$Repo
    ib_host=$IbHost
    port=$Port
    client_id=$ClientId
    python_exe=$pyExe
    python_prefix=@($prefix)

    positions=[ordered]@{
      exit_code=$posRc
      ok=$posOk
      rows_total=(@($posRows)).Count
      nonzero_total=(@($posNonZero)).Count
      symbols=@($posSymbols)
      out_path=$posJson
      err_path=$posErr
      err_tail=_tail_text $posErr
    }

    open_orders=[ordered]@{
      ok=$ordOk
      module=[string]$ordTry.module
      exit_code=[int]$ordTry.exit_code
      argv_used=@($ordTry.argv_used)
      stderr_tail=[string]$ordTry.stderr_tail
      jsonl_path=$ordJsonl
      stdout_path=$ordStd
      err_path=$ordErr
      parsed_total=$orders.Count
      parsed_invalid=$ordersInvalid
      symbols=@($ordSymbols)
      status_counts=$statusCounts
    }

    issues=@($issues)
    warns=@($warns)
    out_dir=$runDir
  }

  ($summary | ConvertTo-Json -Depth 14) | Set-Content -LiteralPath $sumPath -Encoding UTF8
  _write_latest $latest $summary

  ($summary | ConvertTo-Json -Compress -Depth 14) | Write-Output
  exit $exit
}
catch {
  $latest = Join-Path $Repo "args\data\reconcile_evidence_latest.json"
  $out=[ordered]@{ schema="stage5_reconcile_evidence_pack_v1"; ts_utc=_utc; status="FAIL"; exit_code=2; error=$_.Exception.Message }
  _write_latest $latest $out
  ($out | ConvertTo-Json -Compress -Depth 8) | Write-Output
  exit 2
}

