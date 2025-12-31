#requires -Version 5.1
Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

function _utc_now { (Get-Date).ToUniversalTime() }
function _utc_iso([DateTime]$dt) { $dt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ") }

function _log([string]$lvl, [string]$msg) {
    $ts = _utc_iso (_utc_now)
    Write-Host "[$ts] [$lvl] $msg"
}

function _json([hashtable]$obj) {
    $obj | ConvertTo-Json -Depth 30 -Compress | Write-Output
}

function _ensure_dir([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { return }
    if (-not (Test-Path -LiteralPath $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
}

function _test_stop([string]$p) { Test-Path -LiteralPath $p }

function _sleep_stopchecked([int]$sec, [string]$stopPath) {
    if ($sec -le 0) { return $false }
    for ($i=0; $i -lt $sec; $i++) {
        if (_test_stop $stopPath) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function _parse_bool([object]$v, [bool]$defaultIfFlagPresent=$true) {
    if ($null -eq $v) { return $defaultIfFlagPresent }
    if ($v -is [bool]) { return $v }
    if ($v -is [int]) { return ($v -ne 0) }
    $s = [string]$v
    $s = $s.Trim()
    if ($s.StartsWith('$')) { $s = $s.TrimStart('$') }
    $s = $s.Trim().ToLowerInvariant()
    switch ($s) {
        "true"  { return $true }
        "false" { return $false }
        "1"     { return $true }
        "0"     { return $false }
        default { return $defaultIfFlagPresent }
    }
}

function _parse_int([object]$v, [int]$fallback) {
    try { return [int]$v } catch { return $fallback }
}

function _try_parse_utc([string]$s) {
    if ([string]::IsNullOrWhiteSpace($s)) { return $null }
    try {
        $dt = [DateTime]::Parse(
            $s,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AssumeUniversal
        )
        return $dt.ToUniversalTime()
    } catch { return $null }
}

# -------------------------
# Manual argv parsing
# Supports:
#   -Name value
#   -Name:value
#   -Name=value
#   -Once
#   -Once:$false
# Aliases:
#   -MaxCycles -> Cycles
#   -IntervalSeconds -> IntervalSec
# -------------------------
$cfg = [ordered]@{
    Cycles = 0
    IntervalSec = 300
    Once = $false
    LockTtlMin = 20
    LockPath = ""
    StopFlagPath = ""
    LogDir = ""
    PythonExe = "py"
    PythonVersionArg = "-3.11"
}

$argv = $args
for ($i=0; $i -lt $argv.Count; $i++) {
    $tok = [string]$argv[$i]
    if (-not $tok.StartsWith('-')) { continue }

    $t = $tok.TrimStart('-')

    $name = $t
    $val = $null
    if ($t.Contains(':')) {
        $parts = $t.Split(':',2); $name = $parts[0]; $val = $parts[1]
    } elseif ($t.Contains('=')) {
        $parts = $t.Split('=',2); $name = $parts[0]; $val = $parts[1]
    } else {
        if (($i+1) -lt $argv.Count) {
            $next = [string]$argv[$i+1]
            if (-not $next.StartsWith('-')) { $val = $next; $i++ }
        }
    }

    $n = $name.Trim().ToLowerInvariant()
    switch ($n) {
        "once" { $cfg.Once = _parse_bool $val $true }
        "cycles" { $cfg.Cycles = _parse_int $val $cfg.Cycles }
        "maxcycles" { $cfg.Cycles = _parse_int $val $cfg.Cycles }
        "intervalsec" { $cfg.IntervalSec = _parse_int $val $cfg.IntervalSec }
        "intervalseconds" { $cfg.IntervalSec = _parse_int $val $cfg.IntervalSec }
        "lockttlmin" { $cfg.LockTtlMin = _parse_int $val $cfg.LockTtlMin }
        "lockpath" { if ($val -ne $null) { $cfg.LockPath = [string]$val } }
        "stopflagpath" { if ($val -ne $null) { $cfg.StopFlagPath = [string]$val } }
        "logdir" { if ($val -ne $null) { $cfg.LogDir = [string]$val } }
        "pythonexe" { if ($val -ne $null) { $cfg.PythonExe = [string]$val } }
        "pythonversionarg" { if ($val -ne $null) { $cfg.PythonVersionArg = [string]$val } }
        default { }
    }
}

if ($cfg.Once) { $cfg.Cycles = 1 }
if ($cfg.Cycles -lt 0) { $cfg.Cycles = 0 }
if ($cfg.IntervalSec -lt 0) { $cfg.IntervalSec = 0 }

# -------------------------
# Paths
# -------------------------
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ([string]::IsNullOrWhiteSpace($cfg.LockPath)) {
    $cfg.LockPath = Join-Path $repoRoot "args\logs\auto_loop.lock"
}
if ([string]::IsNullOrWhiteSpace($cfg.StopFlagPath)) {
    $cfg.StopFlagPath = Join-Path $repoRoot "args\data\stop.flag"
}
if ([string]::IsNullOrWhiteSpace($cfg.LogDir)) {
    $cfg.LogDir = Join-Path $repoRoot "args\logs\auto_loop_5m"
}

_ensure_dir (Split-Path -Parent $cfg.LockPath)
_ensure_dir (Split-Path -Parent $cfg.StopFlagPath)
_ensure_dir $cfg.LogDir

# -------------------------
# Lock helpers
# -------------------------
function _read_lock([string]$path) {
    $res = [ordered]@{ ok=$false; data=$null; error=$null; file_lastwrite_utc=$null }
    if (-not (Test-Path -LiteralPath $path)) { $res.error="NOT_FOUND"; return [pscustomobject]$res }
    try { $fi = Get-Item -LiteralPath $path -ErrorAction Stop; $res.file_lastwrite_utc = $fi.LastWriteTimeUtc } catch { }
    try {
        $raw = Get-Content -LiteralPath $path -Raw -ErrorAction Stop
        $res.data = $raw | ConvertFrom-Json -ErrorAction Stop
        $res.ok = $true
    } catch { $res.error = $_.Exception.Message }
    return [pscustomobject]$res
}

function _proc_snap([int]$processId) {
    $snap = [ordered]@{ process_id=$processId; alive=$false; name=$null; start_utc_dt=$null; start_utc_ok=$false; error=$null }
    try {
        $p = Get-Process -Id $processId -ErrorAction Stop
        $snap.alive = $true
        $snap.name = $p.ProcessName
        try { $snap.start_utc_dt = $p.StartTime.ToUniversalTime(); $snap.start_utc_ok=$true } catch { $snap.error="STARTTIME_UNAVAILABLE: $($_.Exception.Message)" }
    } catch { $snap.error = $_.Exception.Message }
    return [pscustomobject]$snap
}

function _eval_lock([string]$path, [int]$ttlMin) {
    $now = _utc_now
    $ev = [ordered]@{
        path=$path; exists=(Test-Path -LiteralPath $path)
        status="NO_LOCK"; reason=$null
        parse_ok=$false; lock_pid=$null; started_utc=$null; machine=$null; user=$null
        file_lastwrite_utc=$null; lock_age_min=$null
        pid_alive=$null; pid_reused=$null; process_name=$null; process_start_utc=$null
    }
    if (-not $ev.exists) { return [pscustomobject]$ev }

    $r = _read_lock $path
    $ev.file_lastwrite_utc = $r.file_lastwrite_utc

    if (-not $r.ok -or $null -eq $r.data) {
        $ev.parse_ok = $false
        if ($r.file_lastwrite_utc) { $ev.lock_age_min = [Math]::Round(($now - $r.file_lastwrite_utc).TotalMinutes, 1) }
        if ($ev.lock_age_min -ne $null -and $ev.lock_age_min -ge $ttlMin) { $ev.status="STALE"; $ev.reason="TTL_EXCEEDED_UNPARSEABLE" }
        else { $ev.status="ACTIVE"; $ev.reason="UNPARSEABLE_WITHIN_TTL" }
        return [pscustomobject]$ev
    }

    $ev.parse_ok = $true
    $d = $r.data
    try { $ev.lock_pid = $d.pid } catch { }
    try { $ev.started_utc = $d.started_utc } catch { }
    try { $ev.machine = $d.machine } catch { }
    try { $ev.user = $d.user } catch { }

    $startedDt = _try_parse_utc ([string]$ev.started_utc)
    if ($startedDt -ne $null) {
        $ev.lock_age_min = [Math]::Round(($now - $startedDt).TotalMinutes, 1)
        if ($startedDt -gt $now.AddMinutes(2)) { $ev.status="STALE"; $ev.reason="STARTED_UTC_IN_FUTURE"; return [pscustomobject]$ev }
    } elseif ($r.file_lastwrite_utc) {
        $ev.lock_age_min = [Math]::Round(($now - $r.file_lastwrite_utc).TotalMinutes, 1)
    }

    $pidOk = $false; $pidInt = $null
    try { $pidInt = [int]$ev.lock_pid; if ($pidInt -gt 0) { $pidOk = $true } } catch { $pidOk = $false }

    if ($pidOk) {
        $snap = _proc_snap $pidInt
        $ev.pid_alive = $snap.alive
        $ev.process_name = $snap.name
        if ($snap.start_utc_ok -and $snap.start_utc_dt) { $ev.process_start_utc = _utc_iso $snap.start_utc_dt }

        if (-not $snap.alive) { $ev.status="STALE"; $ev.reason="PID_NOT_ALIVE"; return [pscustomobject]$ev }

        if ($startedDt -ne $null -and $snap.start_utc_ok -and $snap.start_utc_dt) {
            if ($snap.start_utc_dt -gt $startedDt.AddMinutes(2)) {
                $ev.status="STALE"; $ev.reason="PID_REUSED"; $ev.pid_reused=$true
                return [pscustomobject]$ev
            }
        }

        $ev.status="ACTIVE"; $ev.reason="PID_ALIVE"; $ev.pid_reused=$false
        return [pscustomobject]$ev
    }

    if ($ev.lock_age_min -ne $null -and $ev.lock_age_min -ge $ttlMin) { $ev.status="STALE"; $ev.reason="TTL_EXCEEDED_NO_PID" }
    else { $ev.status="ACTIVE"; $ev.reason="NO_PID_WITHIN_TTL" }
    return [pscustomobject]$ev
}

function _rm_lock([string]$path) {
    try {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction Stop }
        return $true
    } catch {
        _log "ERROR" "Failed to remove lock: $path :: $($_.Exception.Message)"
        return $false
    }
}

function _acquire_lock([string]$path, [int]$ttlMin) {
    _ensure_dir (Split-Path -Parent $path)

    $pre = _eval_lock $path $ttlMin
    if ($pre.exists -and $pre.status -eq "ACTIVE") {
        return [pscustomobject]@{ acquired=$false; status="ACTIVE_LOCK"; eval=$pre; lock=$null; error=$null }
    }
    if ($pre.exists -and $pre.status -eq "STALE") {
        _log "WARN" "Stale lock detected ($($pre.reason)); removing: $path"
        if (-not (_rm_lock $path)) {
            return [pscustomobject]@{ acquired=$false; status="LOCK_REMOVE_FAILED"; eval=$pre; lock=$null; error="LOCK_REMOVE_FAILED" }
        }
    }

    $now = _utc_now
    $lockObj = [ordered]@{ pid=$PID; started_utc=_utc_iso $now; machine=$env:COMPUTERNAME; user=$env:USERNAME }
    $json = ($lockObj | ConvertTo-Json -Depth 5 -Compress)

    try {
        $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
            $fs.Write($bytes, 0, $bytes.Length)
        } finally { $fs.Close() }
    } catch {
        if (Test-Path -LiteralPath $path) {
            $post = _eval_lock $path $ttlMin
            return [pscustomobject]@{ acquired=$false; status="ACTIVE_LOCK_RACE"; eval=$post; lock=$null; error=$_.Exception.Message }
        }
        return [pscustomobject]@{ acquired=$false; status="LOCK_CREATE_FAILED"; eval=$pre; lock=$null; error=$_.Exception.Message }
    }

    $post2 = _eval_lock $path $ttlMin
    return [pscustomobject]@{ acquired=$true; status="LOCK_ACQUIRED"; eval=$post2; lock=$lockObj; error=$null }
}

function _release_lock([string]$path, $myLock) {
    if ([string]::IsNullOrWhiteSpace($path)) { return }
    if (-not (Test-Path -LiteralPath $path)) { return }
    try {
        $r = _read_lock $path
        if ($r.ok -and $r.data) {
            $pidVal = $null; $startedVal = $null
            try { $pidVal = $r.data.pid } catch { }
            try { $startedVal = $r.data.started_utc } catch { }

            $pidMatch = $false
            try { $pidMatch = ([int]$pidVal -eq [int]$PID) } catch { $pidMatch = $false }
            $startedMatch = ($startedVal -eq $myLock.started_utc)

            if ($pidMatch -and $startedMatch) {
                Remove-Item -LiteralPath $path -Force -ErrorAction Stop
                _log "INFO" "Lock released: $path"
                return
            }
            _log "WARN" "Lock does not match current process; not removing. lock_pid=$pidVal lock_started_utc=$startedVal"
            return
        }
        Remove-Item -LiteralPath $path -Force -ErrorAction Stop
        _log "WARN" "Lock unreadable on release; removed best-effort: $path"
    } catch {
        _log "ERROR" "Failed to release lock: $path :: $($_.Exception.Message)"
    }
}

function _sanitize([string]$s) {
    return ([System.Text.RegularExpressions.Regex]::Replace($s, "[^a-zA-Z0-9_\-]+", "_"))
}

function _run_step([int]$cycle, [string]$name, [string]$exe, [string[]]$argsList, [string]$workdir, [string]$logdir) {
    _ensure_dir $logdir
    $start = _utc_now
    $ts = $start.ToString("yyyyMMdd_HHmmssfff'Z'")
    $safe = _sanitize $name

    $base = Join-Path $logdir ("{0}_c{1:000000}_{2}" -f $ts, $cycle, $safe)
    $stdoutPath = $base + ".stdout.log"
    $stderrPath = $base + ".stderr.log"
    $combinedPath = $base + ".log"

    $cmdDisp = $exe + " " + ($argsList -join " ")
    _log "INFO" ("STEP_START cycle={0} name={1} cmd={2}" -f $cycle, $name, $cmdDisp)

    $exitCode = 9001
    $procError = $null

    try {
        $p = Start-Process -FilePath $exe -ArgumentList $argsList -WorkingDirectory $workdir -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -ErrorAction Stop
        $exitCode = $p.ExitCode
    } catch {
        $procError = $_.Exception.Message
        try { Set-Content -LiteralPath $stderrPath -Value ("Start-Process failed: {0}" -f $procError) -Encoding UTF8 } catch { }
    }

    $end = _utc_now
    $durS = [Math]::Round(($end - $start).TotalSeconds, 3)

    try {
        $header = @(
            ("=== STEP {0} (cycle {1}) ===" -f $name, $cycle),
            ("started_utc: {0}" -f (_utc_iso $start)),
            ("ended_utc:   {0}" -f (_utc_iso $end)),
            ("duration_s:  {0}" -f $durS),
            ("exit_code:   {0}" -f $exitCode),
            ("cmd:         {0}" -f $cmdDisp),
            ("workdir:     {0}" -f $workdir),
            ""
        ) -join "`r`n"
        Set-Content -LiteralPath $combinedPath -Value $header -Encoding UTF8

        Add-Content -LiteralPath $combinedPath -Value "----- STDOUT -----" -Encoding UTF8
        if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -ErrorAction SilentlyContinue | Add-Content -LiteralPath $combinedPath -Encoding UTF8 }
        Add-Content -LiteralPath $combinedPath -Value "" -Encoding UTF8

        Add-Content -LiteralPath $combinedPath -Value "----- STDERR -----" -Encoding UTF8
        if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -ErrorAction SilentlyContinue | Add-Content -LiteralPath $combinedPath -Encoding UTF8 }
        Add-Content -LiteralPath $combinedPath -Value "" -Encoding UTF8

        Add-Content -LiteralPath $combinedPath -Value ("=== STEP_END exit_code={0} ===" -f $exitCode) -Encoding UTF8
    } catch {
        _log "WARN" "Failed to build combined log for step '$name': $($_.Exception.Message)"
    }

    _log "INFO" ("STEP_END cycle={0} name={1} exit={2} dur_s={3} log={4}" -f $cycle, $name, $exitCode, $durS, $combinedPath)

    return [pscustomobject]@{
        name=$name; exit_code=$exitCode
        started_utc=_utc_iso $start; ended_utc=_utc_iso $end; duration_s=$durS
        log=$combinedPath; log_stdout=$stdoutPath; log_stderr=$stderrPath
        error=$procError
    }
}

# -------------------------
# Start
# -------------------------
_log "INFO" ("auto_loop_5m starting | repoRoot={0} | cycles={1} | intervalSec={2} | lock={3} | stopFlag={4} | logDir={5}" -f `
    $repoRoot, $cfg.Cycles, $cfg.IntervalSec, $cfg.LockPath, $cfg.StopFlagPath, $cfg.LogDir)

$acq = _acquire_lock $cfg.LockPath $cfg.LockTtlMin
if (-not $acq.acquired) {
    $ev = $acq.eval
    _json ([ordered]@{
        ts_utc = _utc_iso (_utc_now)
        reason = $acq.status
        ok = $true
        exit_code = if ($acq.status -in @("ACTIVE_LOCK","ACTIVE_LOCK_RACE")) { 0 } else { 1 }

        lock_path = $ev.path
        lock_pid = $ev.lock_pid
        lock_age_min = $ev.lock_age_min
        lock_status = $ev.status
        lock_reason = $ev.reason

        pid_alive = $ev.pid_alive
        pid_reused = $ev.pid_reused
        process_name = $ev.process_name
        process_start_utc = $ev.process_start_utc

        started_utc = $ev.started_utc
        machine = $ev.machine
        user = $ev.user

        stop_flag = (_test_stop $cfg.StopFlagPath)
        error = $acq.error
    })
    if ($acq.status -in @("ACTIVE_LOCK","ACTIVE_LOCK_RACE")) { exit 0 }
    exit 1
}

$myLock = $acq.lock
$exitCodeOverall = 0
$anyHardFail = $false

try {
    $cycle = 0
    while ($true) {
        $cycle++

        if (_test_stop $cfg.StopFlagPath) {
            $evStop = _eval_lock $cfg.LockPath $cfg.LockTtlMin
            _json ([ordered]@{
                ts_utc = _utc_iso (_utc_now)
                reason = "STOP_FLAG"
                ok = $true
                exit_code = 0
                cycle = $cycle
                lock_path = $evStop.path
                lock_pid = $evStop.lock_pid
                lock_age_min = $evStop.lock_age_min
                lock_status = $evStop.status
                lock_reason = $evStop.reason
                stop_flag = $true
            })
            break
        }

        $cycleStart = _utc_now
        _log "INFO" ("CYCLE_START cycle={0}" -f $cycle)

        $steps = @(
            @{ name="refresh_bundle";     module="args.demo.demo_ibkr_refresh_hg_5m_bundle_v1" },
            @{ name="paper_loop";         module="args.demo.demo_paper_loop_v0" },
            @{ name="wa_intents";         module="args.demo.demo_wa_v1_intents_from_latest_run" },
            @{ name="wa_payload";         module="args.demo.demo_wa_v1_payload_from_latest_run" },
            @{ name="ibkr_sender_dryrun"; module="args.demo.demo_ibkr_sender_dryrun_from_latest_run" }
        )

        $stepResults = @()
        $skipped = @{}
        $warnings = @{}
        $cycleFailed = $false
        $failReason = $null

        foreach ($s in $steps) {
            if (_test_stop $cfg.StopFlagPath) { $skipped[$s.name] = "SKIPPED_STOP_FLAG"; continue }
            if ($cycleFailed) { $skipped[$s.name] = "SKIPPED_AFTER_FAILURE"; continue }

            $argsList = @($cfg.PythonVersionArg, "-m", $s.module)
            $r = _run_step -cycle $cycle -name $s.name -exe $cfg.PythonExe -argsList $argsList -workdir $repoRoot -logdir $cfg.LogDir
            $stepResults += $r

            if ($r.exit_code -ne 0) {
                if ($s.name -eq "refresh_bundle") {
                    # soft-fail: continue with last bundle
                    $warnings["refresh_bundle"] = "FAILED_EXIT_" + $r.exit_code
                    _log "WARN" "refresh_bundle failed (exit=$($r.exit_code)); continuing with last available bundle."
                } else {
                    $cycleFailed = $true
                    $failReason = ("STEP_FAILED:{0}" -f $s.name)
                }
            }
        }

        $cycleEnd = _utc_now
        $durS = [Math]::Round(($cycleEnd - $cycleStart).TotalSeconds, 3)

        $stepsExit = @{}
        foreach ($r in $stepResults) { $stepsExit[$r.name] = $r.exit_code }

        $evNow = _eval_lock $cfg.LockPath $cfg.LockTtlMin

        $cycleStatus = if ($cycleFailed) { "FAILED" } elseif ($warnings.Count -gt 0) { "WARN" } else { "OK" }
        if ($cycleFailed) { $anyHardFail = $true }

        _json ([ordered]@{
            ts_utc = _utc_iso (_utc_now)
            reason = "CYCLE_DONE"
            ok = (-not $cycleFailed)
            exit_code = 0

            cycle = $cycle
            cycle_started_utc = _utc_iso $cycleStart
            cycle_ended_utc = _utc_iso $cycleEnd
            duration_s = $durS

            status = $cycleStatus
            fail_reason = $failReason
            warnings = $warnings

            lock_path = $evNow.path
            lock_pid = $evNow.lock_pid
            lock_age_min = $evNow.lock_age_min
            lock_status = $evNow.status
            lock_reason = $evNow.reason

            stop_flag = (_test_stop $cfg.StopFlagPath)
            steps_exit = $stepsExit
            skipped = $skipped
        })

        _log "INFO" ("CYCLE_END cycle={0} status={1} duration_s={2}" -f $cycle, $cycleStatus, $durS)

        if (_test_stop $cfg.StopFlagPath) { _log "INFO" "STOP_FLAG detected after cycle. Exiting."; break }
        if ($cfg.Cycles -gt 0 -and $cycle -ge $cfg.Cycles) { _log "INFO" ("Reached requested cycles={0}. Exiting." -f $cfg.Cycles); break }

        if ($cfg.IntervalSec -gt 0) {
            _log "INFO" ("Sleeping intervalSec={0} (stop-checked every 1s)" -f $cfg.IntervalSec)
            if (_sleep_stopchecked $cfg.IntervalSec $cfg.StopFlagPath) { _log "INFO" "STOP_FLAG detected during sleep. Exiting."; break }
        }
    }

    if ($cfg.Cycles -gt 0 -and $anyHardFail) { $exitCodeOverall = 1 } else { $exitCodeOverall = 0 }
}
catch {
    $exitCodeOverall = 1
    $errMsg = $_.Exception.Message
    _log "ERROR" ("FATAL: {0}" -f $errMsg)
    $evFatal = _eval_lock $cfg.LockPath $cfg.LockTtlMin
    _json ([ordered]@{
        ts_utc = _utc_iso (_utc_now)
        reason = "FATAL"
        ok = $false
        exit_code = 1
        error = $errMsg

        lock_path = $evFatal.path
        lock_pid = $evFatal.lock_pid
        lock_age_min = $evFatal.lock_age_min
        lock_status = $evFatal.status
        lock_reason = $evFatal.reason

        stop_flag = (_test_stop $cfg.StopFlagPath)
    })
}
finally {
    _release_lock $cfg.LockPath $myLock | Out-Null
}

exit $exitCodeOverall
