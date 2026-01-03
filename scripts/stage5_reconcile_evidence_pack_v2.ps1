<# 
STAGE5_RECONCILE_EVIDENCE_PACK_V2 (ops-grade, Start-Process, WD + anti-flap)

- Safe control_plane access under StrictMode
- Snapshot defaults from control_plane ibkr.snapshot_* if present
- Fallbacks: snapshot_* -> ibkr.* -> hard defaults (client_id=79, timeouts=60, wait_s=12)
- Auto-detect supported CLI flags via "--help"
- Retry handshake_timeout (positions)
- Authoritative latest (LAST OK): args\data\reconcile_evidence_latest_v2.json
- Last fail report: args\data\reconcile_evidence_last_fail_v2.json
- Optional compat (LAST OK): args\data\reconcile_evidence_latest.json (-WriteCompatLatest)
- Exit codes: 0=OK, 1=WARN, 2=FAIL
#>

[CmdletBinding()]
param(
  [string]$Repo = "",
  [string]$IbHost = "",
  [int]$Port = 0,

  # If not provided, defaults come from control_plane ibkr.snapshot_* fields
  [int]$ClientId = 0,
  [int]$ConnectTimeoutSec = 0,
  [int]$TimeoutSec = 0,
  [int]$OpenOrdersWaitSec = 0,

  [int]$Attempts = 3,
  [int]$SleepBetweenSec = 2,

  [switch]$WriteCompatLatest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# will be set after Repo is resolved
$script:REPO_WD = ""

function _utc() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") }
function _utc_dir() { [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") }

function _get_prop($obj, [string]$name) {
  if ($null -eq $obj) { return $null }
  $p = $obj.PSObject.Properties[$name]
  if ($null -eq $p) { return $null }
  return $p.Value
}

function _read_json([string]$p) {
  if (-not (Test-Path -LiteralPath $p)) { return $null }
  try { return (Get-Content -LiteralPath $p -Raw | ConvertFrom-Json) } catch { return $null }
}

function _write_json([string]$p, $obj, [int]$depth=60) {
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null
  ($obj | ConvertTo-Json -Depth $depth) | Set-Content -LiteralPath $p -Encoding UTF8
}

function _pick_python() {
  $py311 = "C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe"
  if (Test-Path -LiteralPath $py311) { return [ordered]@{ exe=$py311; prefix=@() } }
  return [ordered]@{ exe="C:\Windows\py.exe"; prefix=@("-3.11") }
}

function _tail_file([string]$p, [int]$n=200) {
  if (-not (Test-Path -LiteralPath $p)) { return "" }
  try { return ((Get-Content -LiteralPath $p -Tail $n) -join "`n") } catch { return "" }
}

function _tail_text([string]$s, [int]$n=500) {
  if ($null -eq $s) { return "" }
  if ($s.Length -le $n) { return $s }
  return $s.Substring($s.Length - $n)
}

function _run_proc([string]$exe, [string[]]$argsList, [string]$stdoutPath, [string]$stderrPath) {
  Remove-Item -LiteralPath $stdoutPath -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $stderrPath -ErrorAction SilentlyContinue

  $startArgs = @{
    FilePath = $exe
    ArgumentList = $argsList
    NoNewWindow = $true
    Wait = $true
    PassThru = $true
    RedirectStandardOutput = $stdoutPath
    RedirectStandardError = $stderrPath
  }
  if ($script:REPO_WD) { $startArgs.WorkingDirectory = $script:REPO_WD }

  $p = Start-Process @startArgs
  return [int]$p.ExitCode
}

function _help_text([string]$exe, [string[]]$prefix, [string]$module, [string]$stdoutPath, [string]$stderrPath) {
  $args = @($prefix + @("-m", $module, "--help"))
  $rc = _run_proc $exe $args $stdoutPath $stderrPath
  $out = ""
  if (Test-Path -LiteralPath $stdoutPath) { $out = Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue }
  $err = ""
  if (Test-Path -LiteralPath $stderrPath) { $err = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue }
  return [ordered]@{ rc=$rc; text=($out + "`n" + $err) }
}

function _has_flag([string]$help, [string]$flag) {
  return ($help -match [regex]::Escape($flag))
}

function _detect_flags([string]$exe, [string[]]$prefix, [string]$module, [string]$helpOutPath, [string]$helpErrPath) {
  $h = _help_text $exe $prefix $module $helpOutPath $helpErrPath
  $txt = [string]$h.text

  $outFlag = ""
  if (_has_flag $txt "--out") { $outFlag = "--out" }
  elseif (_has_flag $txt "--out-path") { $outFlag = "--out-path" }
  elseif (_has_flag $txt "--out_jsonl") { $outFlag = "--out_jsonl" }
  elseif (_has_flag $txt "--out-jsonl") { $outFlag = "--out-jsonl" }

  return [ordered]@{
    help_rc = [int]$h.rc
    help_path = $helpOutPath
    help_err_path = $helpErrPath
    has_host = (_has_flag $txt "--host")
    has_port = (_has_flag $txt "--port")
    has_client_id = (_has_flag $txt "--client-id")
    has_timeout_s = (_has_flag $txt "--timeout-s")
    has_connect_timeout_s = (_has_flag $txt "--connect-timeout-s")
    has_wait_s = (_has_flag $txt "--wait-s")
    out_flag = $outFlag
  }
}

function _parse_first_json_obj_from_file([string]$p) {
  if (-not (Test-Path -LiteralPath $p)) { return $null }
  $text = Get-Content -LiteralPath $p -Raw -ErrorAction SilentlyContinue
  if (-not $text) { return $null }
  $lines = $text -split "`r?`n"
  foreach ($ln in $lines) {
    $t = ([string]$ln).Trim()
    if ($t.StartsWith("{")) {
      try { return ($t | ConvertFrom-Json) } catch { }
    }
  }
  try { return ($text | ConvertFrom-Json) } catch { return $null }
}

function _write_jsonl_from_stdout([string]$stdoutPath, [string]$jsonlPath) {
  if (-not (Test-Path -LiteralPath $stdoutPath)) { return 0 }
  $written = 0
  Remove-Item -LiteralPath $jsonlPath -ErrorAction SilentlyContinue
  foreach ($ln in Get-Content -LiteralPath $stdoutPath) {
    $t = ([string]$ln).Trim()
    if ($t.StartsWith("{")) {
      Add-Content -LiteralPath $jsonlPath -Value $t -Encoding UTF8
      $written += 1
    }
  }
  return $written
}

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
  else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }

  $script:REPO_WD = $Repo

  $cpPath       = Join-Path $Repo "args\data\control_plane.json"
  $latestV2Path = Join-Path $Repo "args\data\reconcile_evidence_latest_v2.json"          # LAST OK
  $lastFailV2   = Join-Path $Repo "args\data\reconcile_evidence_last_fail_v2.json"      # LAST FAIL
  $latestCompat = Join-Path $Repo "args\data\reconcile_evidence_latest.json"            # legacy LAST OK

  $cp = _read_json $cpPath
  $ib = $null
  if ($cp) { $ib = _get_prop $cp "ibkr" }

  # --- host/port defaults ---
  if ([string]::IsNullOrWhiteSpace($IbHost)) {
    $h = _get_prop $ib "host"
    if ($h) { $IbHost = [string]$h }
  }
  if ($Port -le 0) {
    $p = _get_prop $ib "port"
    if ($p) { $Port = [int]$p }
  }
  if ([string]::IsNullOrWhiteSpace($IbHost)) { $IbHost = "localhost" }
  if ($Port -le 0) { $Port = 7497 }

  # --- snapshot defaults (safe) ---
  $scid = _get_prop $ib "snapshot_client_id"
  $scto = _get_prop $ib "snapshot_connect_timeout_s"
  $sto  = _get_prop $ib "snapshot_timeout_s"
  $ow   = _get_prop $ib "open_orders_wait_s"
  $cid  = _get_prop $ib "client_id"

  if (-not $PSBoundParameters.ContainsKey("ClientId") -or $ClientId -le 0) {
    if ($scid) { $ClientId = [int]$scid }
    elseif ($cid) { $ClientId = [int]$cid }
    else { $ClientId = 79 }
  }

  if (-not $PSBoundParameters.ContainsKey("ConnectTimeoutSec") -or $ConnectTimeoutSec -le 0) {
    if ($scto) { $ConnectTimeoutSec = [int]$scto } else { $ConnectTimeoutSec = 60 }
  }

  if (-not $PSBoundParameters.ContainsKey("TimeoutSec") -or $TimeoutSec -le 0) {
    if ($sto) { $TimeoutSec = [int]$sto } else { $TimeoutSec = 60 }
  }

  if (-not $PSBoundParameters.ContainsKey("OpenOrdersWaitSec") -or $OpenOrdersWaitSec -le 0) {
    if ($ow) { $OpenOrdersWaitSec = [int]$ow } else { $OpenOrdersWaitSec = 12 }
  }

  $py = _pick_python
  $pyExe = [string]$py.exe
  $prefix = @($py.prefix) | ForEach-Object { [string]$_ }

  $outDir = Join-Path $Repo ("args\ops_evidence\reconcile\" + (_utc_dir))
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null

  # help capture
  $posHelpOut = Join-Path $outDir "positions_help.stdout.txt"
  $posHelpErr = Join-Path $outDir "positions_help.stderr.txt"
  $ooHelpOut  = Join-Path $outDir "open_orders_help.stdout.txt"
  $ooHelpErr  = Join-Path $outDir "open_orders_help.stderr.txt"

  # artifacts
  $posJson   = Join-Path $outDir "positions_snapshot.json"
  $posStdout = Join-Path $outDir "positions_snapshot.stdout.txt"
  $posStderr = Join-Path $outDir "positions_snapshot.stderr.txt"

  $ooJsonl   = Join-Path $outDir "open_orders_snapshot.jsonl"
  $ooStdout  = Join-Path $outDir "open_orders_snapshot.stdout.txt"
  $ooStderr  = Join-Path $outDir "open_orders_snapshot.stderr.txt"

  $posFlags = _detect_flags $pyExe $prefix "args.ibkr.ibkr_positions_snapshotter_v0" $posHelpOut $posHelpErr
  $ooFlags  = _detect_flags $pyExe $prefix "args.ibkr.ibkr_open_orders_snapshotter_v0" $ooHelpOut $ooHelpErr

  # -------- positions snapshot (retry handshake_timeout) --------
  $posRc = 2
  $posObj = $null
  $posErr = ""

  for ($k=1; $k -le $Attempts; $k++) {
    $argsPos = @($prefix + @("-m","args.ibkr.ibkr_positions_snapshotter_v0"))
    if ($posFlags.has_host) { $argsPos += @("--host",$IbHost) }
    if ($posFlags.has_port) { $argsPos += @("--port",$Port) }
    if ($posFlags.has_client_id) { $argsPos += @("--client-id",[string]$ClientId) }
    if ($posFlags.has_connect_timeout_s) { $argsPos += @("--connect-timeout-s",[string]$ConnectTimeoutSec) }
    if ($posFlags.has_timeout_s) { $argsPos += @("--timeout-s",[string]$TimeoutSec) }
    if ($posFlags.out_flag -ne "") { $argsPos += @($posFlags.out_flag,$posJson) }

    $posRc = _run_proc $pyExe ($argsPos | ForEach-Object {[string]$_}) $posStdout $posStderr

    $posObj = _read_json $posJson
    if (-not $posObj) {
      $posObj = _parse_first_json_obj_from_file $posStdout
      if ($posObj) { _write_json $posJson $posObj 12 }
    }

    if ($posRc -eq 0 -and $posObj -and $posObj.ok -eq $true) { break }

    $posErr = ""
    if ($posObj -and $posObj.error) { $posErr = [string]$posObj.error }
    if ($posErr -match "handshake_timeout") {
      Start-Sleep -Seconds $SleepBetweenSec
      continue
    }
    break
  }

  $posOk = ($posRc -eq 0 -and $posObj -and $posObj.ok -eq $true)
  if (-not $posObj) { $posErr = "positions_snapshot_missing_or_unparseable" }
  elseif ($posObj.error) { $posErr = [string]$posObj.error }

  # -------- open orders snapshot --------
  $ooRc = 2
  $ooInvalid = 0
  $ooParsed = 0

  $argsOO = @($prefix + @("-m","args.ibkr.ibkr_open_orders_snapshotter_v0"))
  if ($ooFlags.has_host) { $argsOO += @("--host",$IbHost) }
  if ($ooFlags.has_port) { $argsOO += @("--port",$Port) }
  if ($ooFlags.has_client_id) { $argsOO += @("--client-id",[string]$ClientId) }
  if ($ooFlags.has_timeout_s) { $argsOO += @("--timeout-s",[string]$TimeoutSec) }
  if ($ooFlags.has_wait_s) { $argsOO += @("--wait-s",[string]$OpenOrdersWaitSec) }
  if ($ooFlags.out_flag -ne "") { $argsOO += @($ooFlags.out_flag,$ooJsonl) }

  $ooRc = _run_proc $pyExe ($argsOO | ForEach-Object {[string]$_}) $ooStdout $ooStderr

  if (-not (Test-Path -LiteralPath $ooJsonl)) {
    $ooParsed = _write_jsonl_from_stdout $ooStdout $ooJsonl
  }

  if (Test-Path -LiteralPath $ooJsonl) {
    foreach ($line in Get-Content -LiteralPath $ooJsonl) {
      $t = ([string]$line).Trim()
      if (-not $t.StartsWith("{")) { continue }
      try { $null = ($t | ConvertFrom-Json); $ooParsed += 1 } catch { $ooInvalid += 1 }
    }
  }

  $ooOk = ($ooRc -eq 0)

  # -------- policy --------
  $issues = @()
  $warns  = @()

  if (-not $posOk) {
    $issues += "positions_snapshot_failed"
    $warns += ("positions_error:" + $posErr)
    $warns += ("positions_stderr_tail:" + (_tail_file $posStderr 120))
  }

  if (-not $ooOk) {
    $issues += "open_orders_snapshot_failed"
    $warns += ("open_orders_stderr_tail:" + (_tail_file $ooStderr 160))
    $warns += ("open_orders_help_tail:" + (_tail_text (Get-Content -LiteralPath $ooHelpOut -Raw -ErrorAction SilentlyContinue) 500))
  }

  $status = "OK"
  $exitCode = 0
  if (-not $posOk) { $status = "FAIL"; $exitCode = 2 }
  elseif (-not $ooOk) { $status = "WARN"; $exitCode = 1 }

  $report = [ordered]@{
    schema="stage5_reconcile_evidence_pack_v2"
    ts_utc=_utc
    status=$status
    exit_code=$exitCode
    repo=$Repo
    ib_host=$IbHost
    port=$Port
    client_id=$ClientId
    connect_timeout_s=$ConnectTimeoutSec
    timeout_s=$TimeoutSec
    open_orders_wait_s=$OpenOrdersWaitSec
    python_exe=$pyExe
    python_prefix=@($prefix)
    positions=[ordered]@{
      ok=$posOk
      exit_code=$posRc
      out_path=$posJson
      stdout_path=$posStdout
      stderr_path=$posStderr
      error=$posErr
      flags_detected=$posFlags
    }
    open_orders=[ordered]@{
      ok=$ooOk
      exit_code=$ooRc
      jsonl_path=$ooJsonl
      stdout_path=$ooStdout
      stderr_path=$ooStderr
      parsed_total=$ooParsed
      parsed_invalid=$ooInvalid
      flags_detected=$ooFlags
    }
    issues=$issues
    warns=$warns
    out_dir=$outDir
  }

  _write_json (Join-Path $outDir "reconcile_summary.json") $report 60

  # ---- anti-flap: keep latest_v2 as LAST OK ----
  if ($report.status -eq "OK") {
    _write_json $latestV2Path $report 60
    if ($WriteCompatLatest) { _write_json $latestCompat $report 60 }
  } else {
    _write_json $lastFailV2 $report 60
    if (-not (Test-Path -LiteralPath $latestV2Path)) {
      _write_json $latestV2Path $report 60
      if ($WriteCompatLatest -and -not (Test-Path -LiteralPath $latestCompat)) { _write_json $latestCompat $report 60 }
    }
  }

  $report | ConvertTo-Json -Depth 10 -Compress
  exit $exitCode

} catch {
  $err = $_.Exception.ToString()
  $fail = [ordered]@{
    schema="stage5_reconcile_evidence_pack_v2"
    ts_utc=_utc
    status="FAIL"
    exit_code=2
    error=$err
  }
  $repoOut = if ($Repo) { $Repo } else { (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
  _write_json (Join-Path $repoOut "args\data\reconcile_evidence_last_fail_v2.json") $fail 20
  if (-not (Test-Path -LiteralPath (Join-Path $repoOut "args\data\reconcile_evidence_latest_v2.json"))) {
    _write_json (Join-Path $repoOut "args\data\reconcile_evidence_latest_v2.json") $fail 20
  }
  $fail | ConvertTo-Json -Depth 5 -Compress
  exit 2
}
