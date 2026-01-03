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

function _run_py_capture([string]$exe, [object[]]$argv, [string]$stdoutPath, [string]$stderrPath) {
  Remove-Item $stdoutPath -ErrorAction SilentlyContinue
  Remove-Item $stderrPath -ErrorAction SilentlyContinue
  $out = & $exe @argv 2> $stderrPath
  $rc = $LASTEXITCODE
  $out | Set-Content -LiteralPath $stdoutPath -Encoding UTF8
  return $rc
}

function _parse_jsonl([string]$p) {
  $objs=@(); $invalid=0
  if (-not (Test-Path -LiteralPath $p)) { return [ordered]@{ objs=@(); invalid=0 } }
  foreach ($line in Get-Content -LiteralPath $p) {
    $t = ([string]$line).Trim()
    if (-not $t.StartsWith("{")) { continue }
    try { $objs += ($t | ConvertFrom-Json) } catch { $invalid += 1 }
  }
  return [ordered]@{ objs=$objs; invalid=$invalid }
}

function _write_json([string]$p, $obj, [int]$depth=12) {
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null
  ($obj | ConvertTo-Json -Depth $depth) | Set-Content -LiteralPath $p -Encoding UTF8
}

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
  else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }

  $latest = Join-Path $Repo "args\data\reconcile_evidence_latest.json"

  $conn = _load_json (Join-Path $Repo "args\data\ibkr_connection_v0.json")
  if (-not $IbHost) { try { $IbHost = [string]$conn.host } catch {} }
  if ($Port -le 0)  { try { $Port = [int]$conn.port } catch {} }
  if (-not $IbHost) { $IbHost = "localhost" }
  if ($Port -le 0)  { $Port = 7497 }
  try { if ($conn.client_id) { $ClientId = [int]$conn.client_id } } catch {}

  $py = _pick_python
  $pyExe  = [string]$py.exe
  $prefix = @($py.prefix)

  $base = Join-Path $Repo "args\ops_evidence\reconcile"
  New-Item -ItemType Directory -Force -Path $base | Out-Null
  $stamp = ([DateTime]::UtcNow).ToString("yyyyMMddTHHmmssZ")
  $runDir = Join-Path $base $stamp
  New-Item -ItemType Directory -Force -Path $runDir | Out-Null

  # positions snapshot
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
    $po = Get-Content -LiteralPath $posJson -Raw | ConvertFrom-Json
    $posOk = [bool]$po.ok
    try { $posRows = @($po.rows) } catch { $posRows=@() }
    try { $posErrMsg = [string]$po.error } catch { $posErrMsg=$null }
  }

  $posNonZero = @($posRows | Where-Object { $_.position -ne 0 })
  $posSymbols=@()
  foreach ($r in $posNonZero) { try { $posSymbols += [string]$r.symbol } catch {} }
  $posSymbols=@($posSymbols | Where-Object { $_ } | ForEach-Object { ([string]$_).Trim().ToUpperInvariant() } | Sort-Object -Unique)

  # open orders snapshot (use v0 which is proven)
  $ordJsonl = Join-Path $runDir "open_orders_snapshot.jsonl"
  $ordStd   = Join-Path $runDir "open_orders_snapshot.stdout.txt"
  $ordErr   = Join-Path $runDir "open_orders_snapshot.stderr.txt"
  $ordArgv = @($prefix + @(
    "-m","args.ibkr.ibkr_open_orders_snapshotter_v0",
    "--host",$IbHost,"--port","$Port","--client-id","$ClientId",
    "--timeout-s","$TimeoutSec","--wait-s","$WaitSec","--out",$ordJsonl
  ))
  $ordRc = _run_py_capture $pyExe $ordArgv $ordStd $ordErr
  $ordOk = ($ordRc -eq 0 -and (Test-Path -LiteralPath $ordJsonl))

  $ordParsed = _parse_jsonl $ordJsonl
  $orders=@($ordParsed.objs); $ordersInvalid=[int]$ordParsed.invalid

  $statusCounts=@{}
  foreach ($o in $orders) {
    $st="UNKNOWN"
    try { if ($o.PSObject.Properties.Name -contains "status") { $st=[string]$o.status } } catch {}
    if (-not $statusCounts.ContainsKey($st)) { $statusCounts[$st]=0 }
    $statusCounts[$st]+=1
  }

  # severity
  $issues=@(); $warns=@()

  if ($posRc -ne 0) { $issues += ("positions_snapshot_exit=" + $posRc) }
  if (-not $posOk)  { $issues += "positions_ok=false" }

  # ignore farm OK messages entirely
  if ($posErrMsg) {
    $m = [string]$posErrMsg
    $farmOk = ($m -match "Market data farm connection is OK" -or $m -match "HMDS data farm connection is OK" -or $m -match "Sec-def data farm connection is OK")
    if (-not $farmOk) { $warns += ("positions_error=" + $posErrMsg) }
  }

  if (-not $ordOk)  { $issues += ("open_orders_ok=false exit=" + $ordRc) }
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
    }

    open_orders=[ordered]@{
      ok=$ordOk
      exit_code=$ordRc
      jsonl_path=$ordJsonl
      stdout_path=$ordStd
      err_path=$ordErr
      parsed_total=$orders.Count
      parsed_invalid=$ordersInvalid
      status_counts=$statusCounts
    }

    issues=@($issues)
    warns=@($warns)
    out_dir=$runDir
  }

  _write_json (Join-Path $runDir "reconcile_summary.json") $summary 14
  _write_json $latest $summary 14

  ($summary | ConvertTo-Json -Compress -Depth 14) | Write-Output
  exit $exit
}
catch {
  $latest = Join-Path $Repo "args\data\reconcile_evidence_latest.json"
  $out=[ordered]@{ schema="stage5_reconcile_evidence_pack_v1"; ts_utc=_utc; status="FAIL"; exit_code=2; error=$_.Exception.Message }
  _write_json $latest $out 8
  ($out | ConvertTo-Json -Compress -Depth 8) | Write-Output
  exit 2
}
