param(
  [Parameter(Mandatory=$false)][string]$ServerHost = "127.0.0.1",
  [Parameter(Mandatory=$false)][int]$Port = 0,
  [Parameter(Mandatory=$false)][int]$ReadyAfterMs = 750,
  [Parameter(Mandatory=$false)][int]$ServeStartupTimeoutMs = 8000,
  [Parameter(Mandatory=$false)][int]$ReadyTimeoutMs = 12000,
  [Parameter(Mandatory=$false)][int]$StopTimeoutMs = 8000,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$IncludeNegBind = "NO"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Exit codes
$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) }

function RandHex([int]$n) {
  $chars = "0123456789abcdef"
  $sb = New-Object System.Text.StringBuilder
  for ($i=0; $i -lt $n; $i++) {
    $sb.Append($chars[(Get-Random -Minimum 0 -Maximum 16)]) | Out-Null
  }
  return $sb.ToString()
}

function EnsureDir([string]$p) {
  if (-not (Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
}

function ToJsonLine($obj) {
  $json = $obj | ConvertTo-Json -Depth 50
  $lines = $json -split "`r?`n"
  $clean = @()
  foreach ($l in $lines) { $clean += $l.Trim() }
  return ($clean -join "")
}

function WriteJson([string]$path, $obj) {
  $txt = $obj | ConvertTo-Json -Depth 50
  Set-Content -Encoding UTF8 -Path $path -Value $txt
}

function ReadNonEmptyLines([string]$path) {
  if (-not (Test-Path $path)) { return @() }
  $lines = Get-Content -Path $path -ErrorAction SilentlyContinue
  if ($null -eq $lines) { return @() }
  $out = @()
  foreach ($l in $lines) {
    if ($null -ne $l -and $l.Trim().Length -gt 0) { $out += $l }
  }
  return $out
}

function TcpConnectOnce([string]$h,[int]$p,[int]$timeoutMs) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $iar = $client.BeginConnect($h,$p,$null,$null)
    $ok = $iar.AsyncWaitHandle.WaitOne($timeoutMs,$false)
    if (-not $ok) { return $false }
    $client.EndConnect($iar) | Out-Null
    return $true
  } catch {
    return $false
  } finally {
    try { $client.Close() } catch {}
  }
}

function WaitPortOpen([string]$h,[int]$p,[int]$timeoutMs) {
  $t0 = [Environment]::TickCount
  while (($([Environment]::TickCount) - $t0) -lt $timeoutMs) {
    if (TcpConnectOnce $h $p 200) { return $true }
    Start-Sleep -Milliseconds 200
  }
  return $false
}

function WaitPortClosed([string]$h,[int]$p,[int]$timeoutMs) {
  $t0 = [Environment]::TickCount
  while (($([Environment]::TickCount) - $t0) -lt $timeoutMs) {
    if (-not (TcpConnectOnce $h $p 200)) { return $true }
    Start-Sleep -Milliseconds 200
  }
  return $false
}

function PickFreePort([string]$h,[int]$min,[int]$max,[int]$tries) {
  for ($i=0; $i -lt $tries; $i++) {
    $cand = Get-Random -Minimum $min -Maximum $max
    if (-not (TcpConnectOnce $h $cand 200)) { return $cand }
  }
  throw "INFRA_NO_FREE_PORT"
}

function HttpGet([string]$url,[int]$timeoutSec) {
  $r = @{ ok=$false; status=$null; error=$null }
  try {
    $resp = Invoke-WebRequest -UseBasicParsing -Method Get -Uri $url -TimeoutSec $timeoutSec -ErrorAction Stop
    $r.ok = ($resp.StatusCode -eq 200)
    $r.status = $resp.StatusCode
    return $r
  } catch {
    $r.ok = $false
    $r.error = $_.Exception.Message
    try {
      $resp2 = $_.Exception.Response
      if ($resp2 -and $resp2.StatusCode) { $r.status = [int]$resp2.StatusCode }
    } catch {}
    return $r
  }
}

function WaitHttp200([string]$url,[int]$timeoutMs) {
  $t0 = [Environment]::TickCount
  $last = $null
  while (($([Environment]::TickCount) - $t0) -lt $timeoutMs) {
    $last = HttpGet $url 2
    if ($last.ok -and $last.status -eq 200) {
      return @{ ok=$true; last=$last }
    }
    Start-Sleep -Milliseconds 200
  }
  return @{ ok=$false; last=$last }
}

# PS5.1-safe CLI runner: use & + redirection (no Start-Process)
function RunCliStep([string]$stepName,[string]$exe,[string[]]$args,[string]$stepDir) {
  EnsureDir $stepDir
  $stdoutPath = Join-Path $stepDir "stdout.txt"
  $stderrPath = Join-Path $stepDir "stderr.txt"
  $sumPath    = Join-Path $stepDir "summary.json"

  $rc = $RC_INFRA
  $reason = "INFRA_CLI_LAUNCH_EXCEPTION"
  $child  = "INFRA_CLI_LAUNCH_EXCEPTION"
  $ok = $false
  $exeExit = $null
  $errMsg = $null

  try {
    # run + redirect
    & $exe @args 1> $stdoutPath 2> $stderrPath
    $exeExit = [int]$LASTEXITCODE

    if ($exeExit -ne 0) {
      $rc = $RC_FAIL
      $reason = "FAIL_CLI_RC_NONZERO"
      $child  = "FAIL_CLI_RC_NONZERO"
      $ok = $false
    } else {
      $lines = @(ReadNonEmptyLines $stdoutPath)
      if ($lines.Count -ne 1) {
        $rc = $RC_FAIL
        $reason="FAIL_CONTRACT_NOT_ONE_LINE"; $child="FAIL_CONTRACT_NOT_ONE_LINE"
        $ok=$false
      } else {
        try {
          $null = $lines[0] | ConvertFrom-Json -ErrorAction Stop
          $rc=$RC_OK; $reason="OK"; $child="OK"; $ok=$true
        } catch {
          $rc=$RC_FAIL
          $reason="FAIL_CONTRACT_JSON_INVALID"; $child="FAIL_CONTRACT_JSON_INVALID"
          $ok=$false
        }
      }
    }
  } catch {
    $rc=$RC_INFRA
    $reason="INFRA_CLI_LAUNCH_EXCEPTION"
    $child ="INFRA_CLI_LAUNCH_EXCEPTION"
    $errMsg = $_.Exception.Message
    $ok=$false
    try {
      if ($errMsg -and $errMsg.Trim().Length -gt 0) {
        Set-Content -Encoding UTF8 -Path $stderrPath -Value $errMsg
      }
    } catch {}
  }

  $step = @{
    step=$stepName
    ok=$ok
    rc=$rc
    reason_code=$reason
    child_reason_code=$child
    exe=$exe
    args=$args
    exe_exit_code=$exeExit
    error_message=$errMsg
    stdout_path=$stdoutPath
    stderr_path=$stderrPath
    ts_utc=(UtcNowIso)
  }

  WriteJson $sumPath $step
  return $step
}

# Main
$schema = "server_contract_v0_proof_pack_v1"
$repo = Split-Path $PSScriptRoot -Parent
$run_id = "SC_PROOF_" + (UtcNowId) + "_" + (RandHex 8)
$out_root = Join-Path $repo ("args\data\smoke\" + $schema)
$out_dir  = Join-Path $out_root $run_id

EnsureDir $out_dir
$steps = @()

$final_exit   = $RC_INFRA
$final_ok     = $false
$final_reason = "INFRA_EXCEPTION"
$final_child  = "INFRA_EXCEPTION"
$final_error  = $null

$exe = Join-Path $repo "dist\web_dashboard_v0\app.exe"
$serverProc = $null
$stopFlag = Join-Path $out_dir "stop.flag"

function CleanupServer() {
  if ($serverProc -and -not $serverProc.HasExited) {
    try { Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue } catch {}
  }
  try { Remove-Item -Force -ErrorAction SilentlyContinue $stopFlag } catch {}
}

try {
  if (-not (Test-Path $exe)) {
    $final_exit = $RC_INFRA
    $final_reason="INFRA_MISSING_EXE"
    $final_child ="INFRA_MISSING_EXE"
    $final_error = @{ kind="infra"; message=("Missing exe: " + $exe) }
    throw "PACK_ABORT"
  }

  if ($Port -le 0) { $Port = PickFreePort $ServerHost 17000 19000 60 }

  # 01 version
  $steps += (RunCliStep "01_version" $exe @("version") (Join-Path $out_dir "step_01_version"))
  if (-not $steps[-1].ok) {
    $final_exit   = $steps[-1].rc
    $final_reason = $steps[-1].reason_code
    $final_child  = $steps[-1].child_reason_code
    $final_error  = @{
      kind = $(if ($final_exit -eq $RC_INFRA) {"infra"} else {"fail"})
      step = $steps[-1].step
      message = $(if ($steps[-1].error_message) {$steps[-1].error_message} else {"see step summary"})
      exe_exit_code = $steps[-1].exe_exit_code
      stdout_path = $steps[-1].stdout_path
      stderr_path = $steps[-1].stderr_path
    }
    throw "PACK_ABORT"
  }

  # 02 selftest
  $steps += (RunCliStep "02_selftest" $exe @("selftest") (Join-Path $out_dir "step_02_selftest"))
  if (-not $steps[-1].ok) {
    $final_exit   = $steps[-1].rc
    $final_reason = $steps[-1].reason_code
    $final_child  = $steps[-1].child_reason_code
    $final_error  = @{
      kind = $(if ($final_exit -eq $RC_INFRA) {"infra"} else {"fail"})
      step = $steps[-1].step
      message = $(if ($steps[-1].error_message) {$steps[-1].error_message} else {"see step summary"})
      exe_exit_code = $steps[-1].exe_exit_code
      stdout_path = $steps[-1].stdout_path
      stderr_path = $steps[-1].stderr_path
    }
    throw "PACK_ABORT"
  }

  # 03 ping
  $steps += (RunCliStep "03_ping" $exe @("ping") (Join-Path $out_dir "step_03_ping"))
  if (-not $steps[-1].ok) {
    $final_exit   = $steps[-1].rc
    $final_reason = $steps[-1].reason_code
    $final_child  = $steps[-1].child_reason_code
    $final_error  = @{
      kind = $(if ($final_exit -eq $RC_INFRA) {"infra"} else {"fail"})
      step = $steps[-1].step
      message = $(if ($steps[-1].error_message) {$steps[-1].error_message} else {"see step summary"})
      exe_exit_code = $steps[-1].exe_exit_code
      stdout_path = $steps[-1].stdout_path
      stderr_path = $steps[-1].stderr_path
    }
    throw "PACK_ABORT"
  }

  # 04 serve + probes
  $stepDir4 = Join-Path $out_dir "step_04_serve"
  EnsureDir $stepDir4
  $stdout4 = Join-Path $stepDir4 "stdout.txt"
  $stderr4 = Join-Path $stepDir4 "stderr.txt"
  $sum4    = Join-Path $stepDir4 "summary.json"

  try { Remove-Item -Force -ErrorAction SilentlyContinue $stopFlag } catch {}

  $argsServe = @("serve","--host",$ServerHost,"--port",$Port,"--stop-flag",$stopFlag,"--ready-after-ms",$ReadyAfterMs)

  try {
    $serverProc = Start-Process -FilePath $exe -ArgumentList $argsServe -NoNewWindow -PassThru `
      -RedirectStandardOutput $stdout4 -RedirectStandardError $stderr4
  } catch {
    $step4 = @{
      step="04_serve"
      ok=$false
      rc=$RC_INFRA
      reason_code="INFRA_SERVER_LAUNCH_EXCEPTION"
      child_reason_code="INFRA_SERVER_LAUNCH_EXCEPTION"
      error_message=$_.Exception.Message
      host=$ServerHost
      port=$Port
      exe=$exe
      args=$argsServe
      ts_utc=(UtcNowIso)
      stdout_path=$stdout4
      stderr_path=$stderr4
    }
    WriteJson $sum4 $step4
    $steps += $step4
    $final_exit=$RC_INFRA; $final_reason=$step4.reason_code; $final_child=$step4.child_reason_code
    $final_error=@{ kind="infra"; step="04_serve"; message=$step4.error_message }
    throw "PACK_ABORT"
  }

  $listening = WaitPortOpen $ServerHost $Port $ServeStartupTimeoutMs
  if (-not $listening) {
    $step4 = @{
      step="04_serve"
      ok=$false
      rc=$RC_INFRA
      reason_code="INFRA_PRIMARY_NOT_LISTENING"
      child_reason_code="INFRA_PRIMARY_NOT_LISTENING"
      host=$ServerHost
      port=$Port
      exe=$exe
      args=$argsServe
      ts_utc=(UtcNowIso)
      stdout_path=$stdout4
      stderr_path=$stderr4
    }
    WriteJson $sum4 $step4
    $steps += $step4
    $final_exit=$RC_INFRA; $final_reason=$step4.reason_code; $final_child=$step4.child_reason_code
    $final_error=@{ kind="infra"; step="04_serve"; message="primary not listening"; host=$ServerHost; port=$Port }
    throw "PACK_ABORT"
  }

  $healthUrl = ("http://{0}:{1}/health" -f $ServerHost, $Port)
  $readyUrl  = ("http://{0}:{1}/ready"  -f $ServerHost, $Port)

  $health = HttpGet $healthUrl 2
  $readyWait = WaitHttp200 $readyUrl $ReadyTimeoutMs
  $readyOk = $readyWait.ok

  $ok4 = ($health.status -eq 200) -and $readyOk

  $step4 = @{
    step="04_serve"
    ok=$ok4
    rc=($(if ($ok4) {0} else {1}))
    reason_code=($(if ($ok4) {"OK"} else {"FAIL_HTTP_PROBES"}))
    child_reason_code=($(if ($ok4) {"OK"} else {"FAIL_HTTP_PROBES"}))
    host=$ServerHost
    port=$Port
    exe=$exe
    args=$argsServe
    health=@{ ok=$health.ok; status=$health.status; error=$health.error; url=$healthUrl }
    ready=@{ ok=$readyOk; status=$readyWait.last.status; error=$readyWait.last.error; url=$readyUrl }
    ts_utc=(UtcNowIso)
    stdout_path=$stdout4
    stderr_path=$stderr4
  }

  WriteJson $sum4 $step4
  $steps += $step4

  if (-not $step4.ok) {
    $final_exit=$RC_FAIL; $final_reason=$step4.reason_code; $final_child=$step4.child_reason_code
    $final_error=@{ kind="fail"; step="04_serve"; message="http probes failed"; health=$step4.health; ready=$step4.ready }
    throw "PACK_ABORT"
  }

  # 05 stop.flag -> exit -> port closed
  $stepDir5 = Join-Path $out_dir "step_05_stop"
  EnsureDir $stepDir5
  $sum5 = Join-Path $stepDir5 "summary.json"

  Set-Content -Encoding ASCII -Path $stopFlag -Value "stop"

  $stopped = $false
  try {
    Wait-Process -Id $serverProc.Id -Timeout ([Math]::Ceiling($StopTimeoutMs/1000.0)) -ErrorAction Stop | Out-Null
    $stopped = $true
  } catch { $stopped = $false }

  $portClosed = WaitPortClosed $ServerHost $Port 8000
  $ok5 = $stopped -and $portClosed

  $step5 = @{
    step="05_stop"
    ok=$ok5
    rc=($(if ($ok5) {0} else {2}))
    reason_code=($(if ($ok5) {"OK"} else {"INFRA_STOP_TIMEOUT"}))
    child_reason_code=($(if ($ok5) {"OK"} else {"INFRA_STOP_TIMEOUT"}))
    host=$ServerHost
    port=$Port
    stop_flag=$stopFlag
    proc_id=($(if ($serverProc) {$serverProc.Id} else {$null}))
    proc_exited=($(if ($serverProc) {$serverProc.HasExited} else {$false}))
    ts_utc=(UtcNowIso)
  }
  WriteJson $sum5 $step5
  $steps += $step5

  if (-not $step5.ok) {
    $final_exit=$RC_INFRA; $final_reason=$step5.reason_code; $final_child=$step5.child_reason_code
    $final_error=@{ kind="infra"; step="05_stop"; message="stop timeout / port not closed"; stopped=$stopped; portClosed=$portClosed }
    throw "PACK_ABORT"
  }

  $final_exit = $RC_OK
  $final_ok = $true
  $final_reason = "OK"
  $final_child  = "OK"
}
catch {
  if ($_.Exception.Message -ne "PACK_ABORT") {
    $final_exit = $RC_INFRA
    $final_ok = $false
    $final_reason = "INFRA_EXCEPTION"
    $final_child  = "INFRA_EXCEPTION"
    $final_error = @{ kind="infra"; message=$_.Exception.Message }
  }
}
finally {
  CleanupServer

  $summaryPath = Join-Path $out_dir "summary.json"
  $summaryObj = @{
    schema=$schema
    ts_utc=(UtcNowIso)
    run_id=$run_id
    ok=$final_ok
    exit_code=$final_exit
    reason_code=$final_reason
    child_reason_code=$final_child
    repo=$repo
    exe=$exe
    host=$ServerHost
    port=$Port
    out_dir=$out_dir
    error=$final_error
    steps=$steps
  }
  WriteJson $summaryPath $summaryObj

  $finalLine = @{
    schema=$schema
    ts_utc=(UtcNowIso)
    run_id=$run_id
    ok=$final_ok
    exit_code=$final_exit
    reason_code=$final_reason
    child_reason_code=$final_child
    out_dir=$out_dir
    repo=$repo
    exe=$exe
    host=$ServerHost
    port=$Port
    error=$final_error
  }

  Write-Output (ToJsonLine $finalLine)
  exit $final_exit
}

