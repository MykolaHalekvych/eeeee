
param(
  [string]$ControlTokenPath = "",
  [string]$ControlRunId = "",
  [string]$ControlJobType = "FOUNDRY_BUILD_WINDOW_V1",
  [string]$InnerScript = "",

  # Preferred: path to JSON file containing array of strings
  [string]$InnerArgsPath = "",

  # Fallback only (fragile)
  [string]$InnerArgsJson = "[]",

  [ValidateSet("YES","NO")]
  [string]$PassTokenArgs = "YES",

  [ValidateSet("YES","NO")]
  [string]$RetryWithoutTokenArgs = "YES",

  # Heartbeat cadence (seconds)
  [int]$HeartbeatSec = 20
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# stdio-safe emitters (PS5.1)
function Emit-Err([string]$msg) {
  [Console]::Error.WriteLine(("{0} {1}" -f (Get-Date).ToString("o"), $msg))
}
function Emit-Out([string]$msg) {
  [Console]::Out.WriteLine($msg)
}

# quote args safely for Start-Process (handles whitespace)
function _QuoteArg([string]$a) {
  if ($null -eq $a) { return "" }
  if ($a -match "\s") {
    $q = $a -replace '"','\"'
    return '"' + $q + '"'
  }
  return $a
}

$started = Get-Date
Emit-Err ("WRAPPER.START name={0} pid={1} job_type={2} run_id={3}" -f $MyInvocation.MyCommand.Name, $PID, $ControlJobType, $ControlRunId)

# ---------------------------------------------------------------------------
# compute run_dir / child_dir for artifacts
$runDir  = ""
$childDir = ""
try {
  if (-not [string]::IsNullOrWhiteSpace($InnerArgsPath)) {
    # expected: <run_dir>\child\inner_args.json
    $childDir = Split-Path -Parent $InnerArgsPath
    if (-not [string]::IsNullOrWhiteSpace($childDir)) {
      $runDir = Split-Path -Parent $childDir
    }
  } elseif (-not [string]::IsNullOrWhiteSpace($ControlTokenPath)) {
    # expected: <run_dir>\control_token.json
    $runDir = Split-Path -Parent $ControlTokenPath
    if (-not [string]::IsNullOrWhiteSpace($runDir)) {
      $childDir = Join-Path $runDir "child"
    }
  }
} catch { }

if (-not [string]::IsNullOrWhiteSpace($childDir)) {
  try { New-Item -ItemType Directory -Force -Path $childDir | Out-Null } catch { }
}

$childGateOutPath = ""
if (-not [string]::IsNullOrWhiteSpace($childDir)) {
  $childGateOutPath = Join-Path $childDir "foundry_gate_output.json"
}

Emit-Err ("WRAPPER.DEBUG run_dir=[{0}] child_dir=[{1}] child_gate_out=[{2}]" -f $runDir, $childDir, $childGateOutPath)

function _WriteJsonFile([string]$path, $obj) {
  if ([string]::IsNullOrWhiteSpace($path)) { return }
  try {
    $dir = Split-Path -Parent $path
    if (-not [string]::IsNullOrWhiteSpace($dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    ($obj | ConvertTo-Json -Depth 60) | Set-Content -LiteralPath $path -Encoding UTF8
  } catch {
    Emit-Err ("WRAPPER.WARN write_json_file_failed path=[{0}] err=[{1}]" -f $path, $_.Exception.Message)
  }
}

# DoD#14: create placeholder gate output immediately (survives harness TIMEOUT/kill)
try {
  $ts0 = (Get-Date).ToUniversalTime().ToString("o")
  _WriteJsonFile $childGateOutPath @{
    schema      = "foundry_control_guard_v0"
    ts_utc      = $ts0
    ok          = $false
    exit_code   = 2
    duration_ms = 0
    reason_code = "WRAPPER.RUNNING"
    control     = @{ token_path=$ControlTokenPath; run_id=$ControlRunId; job_type=$ControlJobType }
    inner       = @{ script=$InnerScript; args_path=$InnerArgsPath }
  }
} catch { }

# ---------------------------------------------------------------------------
# emit final JSON + exit (stdout) + always write child gate output (file)
function _EmitJsonAndExit([hashtable]$obj, [int]$code) {
  $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
  $dur_ms = [int]((Get-Date) - $started).TotalMilliseconds

  $obj.schema = "foundry_control_guard_v0"
  $obj.ts_utc = $ts
  $obj.ok = ($code -eq 0)
  $obj.exit_code = $code
  $obj.duration_ms = $dur_ms

  # DoD#14: always present and final
  _WriteJsonFile $childGateOutPath $obj

  # only ONE stdout line (deterministic for Control parser)
  Emit-Out ($obj | ConvertTo-Json -Compress -Depth 60)
  exit $code
}

function _Deny([string]$reason, [string]$detail, [int]$code) {
  Emit-Err ("WRAPPER.DENY reason={0} detail={1}" -f $reason, $detail)
  _EmitJsonAndExit @{
    reason_code = $reason
    detail      = $detail
    control     = @{ token_path=$ControlTokenPath; run_id=$ControlRunId; job_type=$ControlJobType }
    inner       = @{ script=$InnerScript; args_path=$InnerArgsPath }
  } $code
}

function _ExtractLastJsonObject([string[]]$lines) {
  if ($null -eq $lines) { return $null }
  for ($i = $lines.Length - 1; $i -ge 0; $i--) {
    $ln = $lines[$i]
    if ($null -eq $ln) { $ln = "" } else { $ln = [string]$ln }
    $ln = $ln.Trim()
    if ($ln.StartsWith("{") -and $ln.EndsWith("}")) {
      try { return ($ln | ConvertFrom-Json) } catch { }
    }
  }
  return $null
}

function _PickReasonFromInnerJson($j) {
  if ($null -eq $j) { return $null }
  try {
    if ($j.PSObject.Properties.Name -contains "reason_code") {
      $v = [string]$j.reason_code
      if ($v) { return $v }
    }
  } catch { }
  try {
    if ($j.PSObject.Properties.Name -contains "error") {
      $v = [string]$j.error
      if ($v) { return $v }
    }
  } catch { }
  return $null
}

function _ReadTail([string]$path, [int]$tail) {
  if ([string]::IsNullOrWhiteSpace($path)) { return @() }
  if (-not (Test-Path -LiteralPath $path)) { return @() }
  try { return (Get-Content -LiteralPath $path -Tail $tail -ErrorAction Stop) } catch { return @() }
}

# ---------------------------------------------------------------------------
# Token verify (non-bypass)
$expectedJobType = "FOUNDRY_BUILD_WINDOW_V1"
if ([string]::IsNullOrWhiteSpace($ControlJobType)) { $ControlJobType = $expectedJobType }
if ($ControlJobType -ne $expectedJobType) {
  _Deny "CONTROL_TOKEN.JOB_TYPE_UNEXPECTED" ("expected=$expectedJobType got=$ControlJobType") 1
}

if ([string]::IsNullOrWhiteSpace($ControlTokenPath)) { _Deny "CONTROL_TOKEN.MISSING" "ControlTokenPath empty" 1 }
if ([string]::IsNullOrWhiteSpace($ControlRunId))    { _Deny "CONTROL_TOKEN.RUN_ID_MISSING" "ControlRunId empty" 1 }
if (-not (Test-Path -LiteralPath $ControlTokenPath)) { _Deny "CONTROL_TOKEN.MISSING" ("not found: " + $ControlTokenPath) 1 }

$tok = $null
try { $tok = Get-Content -LiteralPath $ControlTokenPath -Raw -ErrorAction Stop | ConvertFrom-Json }
catch { _Deny "CONTROL_TOKEN.BAD_JSON" $_.Exception.Message 1 }

if ($tok.schema -ne "control_token_v0") { _Deny "CONTROL_TOKEN.SCHEMA_MISMATCH" ("schema=" + [string]$tok.schema) 1 }
if ($tok.run_id -ne $ControlRunId)      { _Deny "CONTROL_TOKEN.RUN_ID_MISMATCH" ("token.run_id=" + [string]$tok.run_id + "; expected=" + $ControlRunId) 1 }
if ($tok.job_type -ne $ControlJobType)  { _Deny "CONTROL_TOKEN.JOB_TYPE_MISMATCH" ("token.job_type=" + [string]$tok.job_type + "; expected=" + $ControlJobType) 1 }
if (-not ([string]$tok.nonce -match '^[a-f0-9]{16,64}$')) { _Deny "CONTROL_TOKEN.NONCE_INVALID" ("nonce=" + [string]$tok.nonce) 1 }

Emit-Err "WRAPPER.TOKEN.OK"

# ---------------------------------------------------------------------------
# Resolve inner
if ([string]::IsNullOrWhiteSpace($InnerScript)) { $InnerScript = Join-Path $PSScriptRoot "factory_build_window_v1.ps1" }
if (-not (Test-Path -LiteralPath $InnerScript)) { _Deny "INNER_SCRIPT.MISSING" ("inner not found: " + $InnerScript) 2 }

# ---------------------------------------------------------------------------
# Read args raw (prefer file)
$raw = $InnerArgsJson
$source = "json"
if (-not [string]::IsNullOrWhiteSpace($InnerArgsPath)) {
  if (-not (Test-Path -LiteralPath $InnerArgsPath)) { _Deny "INNER_ARGS_PATH.MISSING" ("not found: " + $InnerArgsPath) 2 }
  try { $raw = Get-Content -LiteralPath $InnerArgsPath -Raw -ErrorAction Stop; $source="path" }
  catch { _Deny "INNER_ARGS_PATH.READ_FAIL" $_.Exception.Message 2 }
}

# Parse args (expect JSON array of strings)
$innerArgs = @()
try {
  $parsed = $raw | ConvertFrom-Json
  if ($null -eq $parsed) { $innerArgs = @() }
  elseif ($parsed -is [System.Array]) {
    foreach ($x in $parsed) { $innerArgs += [string]$x }
  } else {
    _Deny "INNER_ARGS_JSON.INVALID" "Expected JSON array of strings" 2
  }
} catch {
  _Deny "INNER_ARGS_JSON.BAD_JSON" $_.Exception.Message 2
}

# Validate args: no null/empty elements
for ($i=0; $i -lt $innerArgs.Count; $i++) {
  $v = $innerArgs[$i]
  if ([string]::IsNullOrWhiteSpace($v)) {
    _Deny "INNER_ARGS.INVALID_EMPTY" ("args contains empty element at index=" + $i) 2
  }
}

$tokenArgs = @("-ControlTokenPath", $ControlTokenPath, "-ControlRunId", $ControlRunId, "-ControlJobType", $ControlJobType)

# ---------------------------------------------------------------------------
function _InvokeInnerProcess([string[]]$argsToUse, [string]$attemptTag) {
  $baseDir = $childDir
  if ([string]::IsNullOrWhiteSpace($baseDir)) { $baseDir = $env:TEMP }

  $stdoutPath = Join-Path $baseDir ("inner_stdout_" + $attemptTag + ".txt")
  $stderrPath = Join-Path $baseDir ("inner_stderr_" + $attemptTag + ".txt")

  # Build inner command (powershell.exe -File <InnerScript> <args...>)
  $psArgs = @("-NoProfile","-ExecutionPolicy","Bypass","-File", $InnerScript) + $argsToUse

  # Validate psArgs
  for ($i=0; $i -lt $psArgs.Count; $i++) {
    $v = $psArgs[$i]
    if ([string]::IsNullOrWhiteSpace($v)) {
      return @{
        ok=$false; rc=2; stdout_path=$stdoutPath; stderr_path=$stderrPath;
        err=("psArgs has null/empty at index=" + $i)
      }
    }
  }

  $argStr = (($psArgs | ForEach-Object { _QuoteArg $_ }) -join " ")
  Emit-Err ("INNER.INVOKE attempt={0} stdout=[{1}] stderr=[{2}]" -f $attemptTag, $stdoutPath, $stderrPath)

  $proc = $null
  try {
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $argStr -PassThru -NoNewWindow `
      -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -ErrorAction Stop
  } catch {
    return @{
      ok=$false; rc=2; stdout_path=$stdoutPath; stderr_path=$stderrPath;
      err=("Start-Process failed: " + $_.Exception.Message)
    }
  }

  if (-not $proc -or -not $proc.Id) {
    return @{
      ok=$false; rc=2; stdout_path=$stdoutPath; stderr_path=$stderrPath;
      err="Start-Process returned null proc or missing Id"
    }
  }

  Emit-Err ("INNER.START attempt={0} pid={1}" -f $attemptTag, $proc.Id)

  $t0 = Get-Date
  $prevOut = -1
  $prevErr = -1
  $prevCpu = -1.0

  while (-not $proc.HasExited) {
    Start-Sleep -Seconds $HeartbeatSec
    $elapsed = [int]((Get-Date) - $t0).TotalSeconds

    $outLen = 0
    $errLen = 0
    try { if (Test-Path -LiteralPath $stdoutPath) { $outLen = (Get-Item -LiteralPath $stdoutPath).Length } } catch { }
    try { if (Test-Path -LiteralPath $stderrPath) { $errLen = (Get-Item -LiteralPath $stderrPath).Length } } catch { }

    $cpu = -1.0
    $wsMb = -1
    try {
      $gp = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
      if ($gp) {
        $cpu = [double]$gp.CPU
        $wsMb = [int]($gp.WorkingSet64 / 1MB)
      }
    } catch { }

    if ($outLen -ne $prevOut -or $errLen -ne $prevErr) {
      Emit-Err ("INNER.IO attempt={0} stdout_bytes={1} stderr_bytes={2}" -f $attemptTag, $outLen, $errLen)
      $prevOut = $outLen
      $prevErr = $errLen
    }

    if ($cpu -ge 0 -and $cpu -ne $prevCpu) {
      Emit-Err ("INNER.CPU attempt={0} cpu_s={1} ws_mb={2}" -f $attemptTag, $cpu, $wsMb)
      $prevCpu = $cpu
    }

    Emit-Err ("WRAPPER.HEARTBEAT elapsed_s={0} inner_pid={1} attempt={2}" -f $elapsed, $proc.Id, $attemptTag)
  }

  $rc = [int]$proc.ExitCode
  Emit-Err ("INNER.EXIT attempt={0} pid={1} exit_code={2}" -f $attemptTag, $proc.Id, $rc)

  return @{
    ok=$true; rc=$rc; stdout_path=$stdoutPath; stderr_path=$stderrPath; err=""
  }
}

# ---------------------------------------------------------------------------
# Invoke inner (attempt1, optional attempt2)
$token_args_used = $false
$retried_without_token_args = $false

$args1 = $innerArgs
if ($PassTokenArgs -eq "YES") {
  $args1 = $tokenArgs + $innerArgs
  $token_args_used = $true
}

$r1 = _InvokeInnerProcess $args1 "A1"
if (-not $r1.ok) {
  _Deny "INNER.INVOKE.FAIL" $r1.err 2
}

$innerRc = [int]$r1.rc
$stdoutPath = [string]$r1.stdout_path
$stderrPath = [string]$r1.stderr_path

# Retry without token args if inner doesn't accept them
if ($token_args_used -and $RetryWithoutTokenArgs -eq "YES") {
  $tailOut = (_ReadTail $stdoutPath 200) -join "`n"
  $tailErr = (_ReadTail $stderrPath 200) -join "`n"
  $all = ($tailOut + "`n" + $tailErr)

  if ( ($all -match "ControlTokenPath") -and ( ($all -match "matches parameter name") -or ($all -match "cannot be found") -or ($all -match "Не удается найти") -or ($all -match "не удается найти") ) ) {
    $retried_without_token_args = $true
    Emit-Err "WRAPPER.RETRY_NO_TOKEN_ARGS"

    $r2 = _InvokeInnerProcess $innerArgs "A2"
    if (-not $r2.ok) {
      _Deny "INNER.INVOKE.RETRY_FAIL" $r2.err 2
    }

    $innerRc = [int]$r2.rc
    $stdoutPath = [string]$r2.stdout_path
    $stderrPath = [string]$r2.stderr_path
    $token_args_used = $false
  }
}

# ---------------------------------------------------------------------------
# Parse inner JSON (prefer stdout)
$stdoutLines = _ReadTail $stdoutPath 400
$stderrLines = _ReadTail $stderrPath 400

$innerJson = _ExtractLastJsonObject $stdoutLines
if ($null -eq $innerJson) { $innerJson = _ExtractLastJsonObject $stderrLines }
if ($null -eq $innerJson) { $innerJson = _ExtractLastJsonObject ($stdoutLines + $stderrLines) }

$innerReason = _PickReasonFromInnerJson $innerJson

$innerExit = $innerRc
if ($null -ne $innerJson) {
  try {
    if ($innerJson.PSObject.Properties.Name -contains "exit_code") { $innerExit = [int]$innerJson.exit_code }
  } catch { }
}

# Map to wrapper exit codes 0/1/2
$code = 2
if ($innerExit -eq 0) { $code = 0 }
elseif ($innerExit -eq 1) { $code = 1 }
else { $code = 2 }

$reason = $innerReason
if ([string]::IsNullOrWhiteSpace($reason)) {
  if ($code -eq 0) { $reason = "INNER.PASS" }
  elseif ($code -eq 1) { $reason = "INNER.FAIL" }
  else { $reason = "INNER.INFRA" }
}

Emit-Err ("WRAPPER.END reason={0} exit_code={1} raw_exit={2}" -f $reason, $code, $innerRc)

_EmitJsonAndExit @{
  reason_code = $reason
  control = @{ token_path=$ControlTokenPath; run_id=$ControlRunId; job_type=$ControlJobType }
  inner = @{
    script=$InnerScript
    exit_code=$code
    raw_exit_code=$innerRc
    last_json=$innerJson
    args_count=$innerArgs.Count
    args_source=$source
    token_args_used=$token_args_used
    retried_without_token_args=$retried_without_token_args
    stdout_path=$stdoutPath
    stderr_path=$stderrPath
  }
} $code
