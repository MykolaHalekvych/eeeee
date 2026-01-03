[CmdletBinding()]
param(
  [string]$Repo = "",
  [string]$Host = "",
  [int]$Port = 0,
  [int]$ClientId = 88,
  [int]$ConnectTimeoutSec = 8,
  [int]$TimeoutSec = 25
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function _utc() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") }

function _load_conn([string]$repo) {
  $p = Join-Path $repo "args\data\ibkr_connection_v0.json"
  if (Test-Path -LiteralPath $p) {
    try { return (Get-Content -LiteralPath $p -Raw | ConvertFrom-Json) } catch { return $null }
  }
  return $null
}

function _pick_python() {
  $py311 = "C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe"
  if (Test-Path -LiteralPath $py311) {
    return [ordered]@{ exe=$py311; prefix=@() }
  }
  return [ordered]@{ exe="C:\Windows\py.exe"; prefix=@("-3.11") }
}

function _run_py([string]$exe, [object[]]$argv, [string]$stdoutPath, [string]$stderrPath) {
  # run and capture stdout/stderr
  $out = & $exe @argv 2> $stderrPath
  $rc = $LASTEXITCODE
  $out | Set-Content -LiteralPath $stdoutPath -Encoding UTF8
  return $rc
}

function _module_not_found([string]$stderrPath) {
  if (-not (Test-Path -LiteralPath $stderrPath)) { return $false }
  $t = (Get-Content -LiteralPath $stderrPath -Raw)
  return ($t -match "No module named" -or $t -match "ModuleNotFoundError")
}

function _try_open_orders([string]$pyExe, [object[]]$prefix, [string]$host, [int]$port, [int]$clientId, [int]$cto, [int]$to, [string]$outPath, [string]$errPath) {
  $mods = @(
    "args.ibkr.ibkr_open_orders_snapshotter_v0b",
    "args.ibkr.ibkr_open_orders_snapshotter_v0"
  )
  foreach ($m in $mods) {
    Remove-Item $outPath -ErrorAction SilentlyContinue
    Remove-Item $errPath -ErrorAction SilentlyContinue

    $argv = @($prefix + @("-m", $m, "--host", $host, "--port", "$port", "--client-id", "$clientId", "--connect-timeout-s", "$cto", "--timeout-s", "$to"))
    $rc = _run_py $pyExe $argv $outPath $errPath

    if ($rc -eq 0) { return [ordered]@{ ok=$true; module=$m; exit_code=0 } }

    # fallback only if module missing
    if (-not (_module_not_found $errPath)) {
      return [ordered]@{ ok=$false; module=$m; exit_code=$rc }
    }
  }
  return [ordered]@{ ok=$false; module=$mods[-1]; exit_code=2 }
}

function _parse_json_lines([string]$path) {
  $objs = @()
  $invalid = 0
  if (-not (Test-Path -LiteralPath $path)) { return [ordered]@{ objs=@(); invalid=0 } }
  foreach ($line in Get-Content -LiteralPath $path) {
    $t = ([string]$line).Trim()
    if (-not $t.StartsWith("{")) { continue }
    try { $objs += ($t | ConvertFrom-Json) } catch { $invalid += 1 }
  }
  return [ordered]@{ objs=$objs; invalid=$invalid }
}

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) {
    $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
  } else {
    $Repo = (Resolve-Path -LiteralPath $Repo).Path
  }

  $conn = _load_conn $Repo
  if (-not $Host) { try { $Host = [string]$conn.host } catch {} }
  if ($Port -le 0) { try { $Port = [int]$conn.port } catch {} }
  if (-not $Host) { $Host = "localhost" }
  if ($Port -le 0) { $Port = 7497 }

  $py = _pick_python
  $pyExe = [string]$py.exe
  $prefix = @($py.prefix)

  $base = Join-Path $Repo "args\ops_evidence\reconcile"
  New-Item -ItemType Directory -Force -Path $base | Out-Null
  $stamp = ([DateTime]::UtcNow).ToString("yyyyMMddTHHmmssZ")
  $runDir = Join-Path $base $stamp
  New-Item -ItemType Directory -Force -Path $runDir | Out-Null

  $posOut = Join-Path $runDir "positions_snapshot.json"
  $posErr = Join-Path $runDir "positions_snapshot.stderr.txt"
  $ordOut = Join-Path $runDir "open_orders_snapshot.jsonl"
  $ordErr = Join-Path $runDir "open_orders_snapshot.stderr.txt"
  $sumOut = Join-Path $runDir "reconcile_summary.json"
  $latest = Join-Path $Repo "args\data\reconcile_evidence_latest.json"

  # --- positions snapshot ---
  $posArgv = @($prefix + @(
    "-m","args.ibkr.ibkr_positions_snapshotter_v0",
    "--host",$Host,"--port","$Port","--client-id","$ClientId",
    "--connect-timeout-s","$ConnectTimeoutSec","--timeout-s","$TimeoutSec"
  ))
  $posRc = _run_py $pyExe $posArgv $posOut $posErr

  $posObj = $null
  $posOk = $false
  $posRows = @()
  $posErrMsg = $null
  if ($posRc -eq 0 -and (Test-Path -LiteralPath $posOut)) {
    try {
      $posObj = (Get-Content -LiteralPath $posOut -Raw | ConvertFrom-Json)
      $posOk = [bool]$posObj.ok
      $posErrMsg = $posObj.error
      try { $posRows = @($posObj.rows) } catch { $posRows = @() }
    } catch {
      $posOk = $false
      $posErrMsg = "positions_parse_failed"
    }
  }

  # --- open orders snapshot (try v0b then v0) ---
  $ordTry = _try_open_orders $pyExe $prefix $Host $Port $ClientId $ConnectTimeoutSec $TimeoutSec $ordOut $ordErr
  $ordOk = [bool]$ordTry.ok

  # parse for simple counts (best-effort)
  $ordParsed = _parse_json_lines $ordOut
  $orders = @($ordParsed.objs)
  $ordersInvalid = [int]$ordParsed.invalid

  $statusCounts = @{}
  foreach ($o in $orders) {
    $st = ""
    try { if ($o.PSObject.Properties.Name -contains "status") { $st = [string]$o.status } } catch {}
    if (-not $st) { $st = "UNKNOWN" }
    if (-not $statusCounts.ContainsKey($st)) { $statusCounts[$st] = 0 }
    $statusCounts[$st] += 1
  }

  # positions summary
  $posNonZero = @($posRows | Where-Object { $_.position -ne 0 })
  $posSymbols = @()
  foreach ($r in $posNonZero) { try { $posSymbols += [string]$r.symbol } catch {} }
  $posSymbols = @($posSymbols | Where-Object { $_ } | Sort-Object -Unique)

  # severity
  $issues = @()
  $warns  = @()

  if ($posRc -ne 0) { $issues += ("positions_snapshot_exit=" + $posRc) }
  if (-not $posOk)  { $issues += ("positions_ok=false") }
  if ($posErrMsg)   { $warns  += ("positions_error=" + $posErrMsg) }

  if (-not $ordOk)  { $issues += ("open_orders_ok=false module=" + [string]$ordTry.module + " exit=" + [string]$ordTry.exit_code) }

  # warn on unexpected symbols (soft)
  if ($posSymbols.Count -gt 0) {
    # if you want strict allowlist later, wire from control_plane.json; for now just surface
    $warns += ("positions_symbols=" + ($posSymbols -join ","))
  }

  $exit = 0
  $status = "OK"
  if ($issues.Count -gt 0) { $exit = 2; $status = "FAIL" }
  elseif ($warns.Count -gt 0) { $exit = 1; $status = "WARN" }

  $summary = [ordered]@{
    schema="stage5_reconcile_evidence_pack_v1"
    ts_utc=_utc
    status=$status
    exit_code=$exit
    repo=$Repo
    host=$Host
    port=$Port
    client_id=$ClientId
    python_exe=$pyExe
    python_prefix=@($prefix)

    positions = [ordered]@{
      exit_code=$posRc
      ok=$posOk
      rows_total=(@($posRows)).Count
      nonzero_total=(@($posNonZero)).Count
      symbols=@($posSymbols)
      out_path=$posOut
      err_path=$posErr
    }

    open_orders = [ordered]@{
      ok=$ordOk
      module=[string]$ordTry.module
      exit_code=[int]$ordTry.exit_code
      parsed_total=$orders.Count
      parsed_invalid=$ordersInvalid
      status_counts=$statusCounts
      out_path=$ordOut
      err_path=$ordErr
    }

    issues=@($issues)
    warns=@($warns)
    out_dir=$runDir
  }

  ($summary | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath $sumOut -Encoding UTF8
  ($summary | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath $latest -Encoding UTF8
  ($summary | ConvertTo-Json -Compress -Depth 12) | Write-Output
  exit $exit
}
catch {
  $out = [ordered]@{
    schema="stage5_reconcile_evidence_pack_v1"
    ts_utc=_utc
    status="FAIL"
    exit_code=2
    error=$_.Exception.Message
  }
  ($out | ConvertTo-Json -Compress -Depth 6) | Write-Output
  exit 2
}

