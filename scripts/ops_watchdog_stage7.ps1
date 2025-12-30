<#
STAGE7_WATCHDOG_V1_STABLE (StrictMode-safe)

- JSON-only stdout: SUMMARY only
- Full JSON written to args/data/ops_health.json (best-effort, also on crash)
- Optional remediation is strictly operator-safe and skipped if stop.flag exists
- Exit codes: 0=OK, 1=WARN, 2=FAIL
- HARD SAFETY: no BUY/SELL, no order placement, no trading actions
- No external deps
#>

[CmdletBinding()]
param(
    [switch]$Remediate,
    [string]$ConfigPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# -------------------------
# StrictMode-safe bootstrap
# -------------------------
$Script:RunId = ([Guid]::NewGuid()).ToString("N")
$Script:RepoRoot = Split-Path -Parent $PSScriptRoot
$Script:DataDir  = Join-Path $Script:RepoRoot "args\data"
$Script:LogsDir  = Join-Path $Script:RepoRoot "args\logs"

$Script:HostName = $env:COMPUTERNAME
if ([string]::IsNullOrWhiteSpace($Script:HostName)) {
    try { $Script:HostName = [System.Net.Dns]::GetHostName() } catch { $Script:HostName = "UNKNOWN_HOST" }
}

$Script:HealthPathDefault = Join-Path $Script:DataDir "ops_health.json"
$Script:HealthPath = $Script:HealthPathDefault
$Script:WatchdogLogPath = Join-Path $Script:LogsDir "ops_watchdog.log"
$Script:RemediationJsonlPath = Join-Path $Script:DataDir "ops_remediation.jsonl"

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $Script:DataDir "ops_config.yaml"
}

# -------------------------
# Helpers (no stdout)
# -------------------------
function UtcIso { (Get-Date).ToUniversalTime().ToString("o") }

function Ensure-Dir {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    try {
        if (-not (Test-Path -LiteralPath $Path)) {
            New-Item -ItemType Directory -Path $Path -Force -ErrorAction SilentlyContinue | Out-Null
        }
    } catch { }
}

function Normalize-Path {
    param([string]$p)
    if ([string]::IsNullOrWhiteSpace($p)) { return "" }
    try { return [System.IO.Path]::GetFullPath($p).TrimEnd('\') } catch { return $p }
}

function Resolve-RepoPath {
    param([string]$p)
    if ([string]::IsNullOrWhiteSpace($p)) { return "" }
    $pp = ($p.Trim() -replace '/', '\')
    if ([System.IO.Path]::IsPathRooted($pp)) { return $pp }
    return (Join-Path $Script:RepoRoot $pp)
}

function Is-UnderDir {
    param([string]$candidate, [string]$dir)
    if ([string]::IsNullOrWhiteSpace($candidate) -or [string]::IsNullOrWhiteSpace($dir)) { return $false }
    $c = (Normalize-Path $candidate).ToLowerInvariant()
    $d = (Normalize-Path $dir).ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($c) -or [string]::IsNullOrWhiteSpace($d)) { return $false }
    return $c.StartsWith($d + "\")
}

function Write-FileUtf8NoBom {
    param([string]$Path, [string]$Text)
    try {
        if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
        Ensure-Dir (Split-Path -Parent $Path)
        $enc = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($Path, $Text, $enc)
        return $true
    } catch { return $false }
}

function Append-FileUtf8NoBom {
    param([string]$Path, [string]$Line)
    try {
        if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
        Ensure-Dir (Split-Path -Parent $Path)
        $enc = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::AppendAllText($Path, $Line + [Environment]::NewLine, $enc)
        return $true
    } catch { return $false }
}

function LogLine {
    param([string]$Level, [string]$Message)
    try {
        $line = ("{0} [{1}] run_id={2} {3}" -f (UtcIso), $Level, $Script:RunId, $Message)
        $null = Append-FileUtf8NoBom -Path $Script:WatchdogLogPath -Line $line
    } catch { }
}

function Safe-Int {
    param($x, [int]$d, [int]$min=0, [int]$max=2147483647)
    try {
        $v = [int]$x
        if ($v -lt $min) { return $min }
        if ($v -gt $max) { return $max }
        return $v
    } catch { return $d }
}

function Safe-Double {
    param($x, [double]$d, [double]$min=0.0, [double]$max=1.0E15)
    try {
        $v = [double]$x
        if ($v -lt $min) { return $min }
        if ($v -gt $max) { return $max }
        return $v
    } catch { return $d }
}

function Split-List {
    param([string]$s)
    if ([string]::IsNullOrWhiteSpace($s)) { return @() }
    $parts = ($s.Trim() -split '[;,]')
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($p in $parts) {
        $t = ($p -as [string]).Trim()
        if (-not [string]::IsNullOrWhiteSpace($t)) { $out.Add($t) | Out-Null }
    }
    return $out.ToArray()
}

function Parse-FlatYaml {
    param([string]$Path)
    $cfg = @{}
    $abs = Resolve-RepoPath $Path
    if ([string]::IsNullOrWhiteSpace($abs) -or (-not (Test-Path -LiteralPath $abs))) { return $cfg }

    try {
        $lines = Get-Content -LiteralPath $abs -ErrorAction Stop
        foreach ($raw in $lines) {
            $line = ($raw -as [string]).Trim()
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            if ($line.StartsWith("#")) { continue }
            if ($line.Contains("#")) { $line = ($line.Split("#",2)[0]).Trim() }
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            if (-not $line.Contains(":")) { continue }

            $kv = $line.Split(":", 2)
            $k = ($kv[0] -as [string]).Trim()
            $v = ($kv[1] -as [string]).Trim()
            if ([string]::IsNullOrWhiteSpace($k)) { continue }

            if ((($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'"))) -and $v.Length -ge 2) {
                $v = $v.Substring(1, $v.Length - 2)
            }

            $low = $v.ToLowerInvariant()
            if ($low -eq "true")  { $cfg[$k] = $true; continue }
            if ($low -eq "false") { $cfg[$k] = $false; continue }

            $iv = 0
            if ([int]::TryParse($v, [ref]$iv)) { $cfg[$k] = $iv; continue }

            $dv = 0.0
            if ([double]::TryParse($v, [ref]$dv)) { $cfg[$k] = $dv; continue }

            $cfg[$k] = $v
        }
    } catch {
        LogLine "WARN" ("config parse failed: {0}" -f $_.Exception.Message)
    }

    return $cfg
}

function Merge-Config {
    param([hashtable]$defaults, [hashtable]$overrides)
    $d = @{}
    foreach ($k in $defaults.Keys) { $d[$k] = $defaults[$k] }
    foreach ($k in $overrides.Keys) { $d[$k] = $overrides[$k] }
    return $d
}

function Write-HealthJson {
    param([string]$jsonFull)
    $ok = $false
    $ok = $ok -or (Write-FileUtf8NoBom $Script:HealthPathDefault $jsonFull)
    $ok = $ok -or (Write-FileUtf8NoBom $Script:HealthPath $jsonFull)
    return $ok
}

# -------------------------
# HARD TIMEOUT runner (Job)
# -------------------------
function Invoke-JobTimeout {
    param(
        [scriptblock]$ScriptBlock,
        [object[]]$ArgumentList,
        [int]$TimeoutSec
    )

    $TimeoutSec = Safe-Int $TimeoutSec 8 1 120
    $job = $null
    try {
        $job = Start-Job -ScriptBlock $ScriptBlock -ArgumentList $ArgumentList
        $done = Wait-Job -Job $job -Timeout $TimeoutSec
        if ($null -eq $done) {
            try { Stop-Job -Job $job -Force -ErrorAction SilentlyContinue | Out-Null } catch { }
            try { Remove-Job -Job $job -Force -ErrorAction SilentlyContinue | Out-Null } catch { }
            return @{ ok=$false; timed_out=$true; error="TIMEOUT"; value=$null }
        }

        $res = $null
        try { $res = Receive-Job -Job $job -ErrorAction SilentlyContinue } catch { $res = $null }
        try { Remove-Job -Job $job -Force -ErrorAction SilentlyContinue | Out-Null } catch { }
        return @{ ok=$true; timed_out=$false; error=$null; value=$res }
    } catch {
        try {
            if ($null -ne $job) {
                Stop-Job -Job $job -Force -ErrorAction SilentlyContinue | Out-Null
                Remove-Job -Job $job -Force -ErrorAction SilentlyContinue | Out-Null
            }
        } catch { }
        return @{ ok=$false; timed_out=$false; error=$_.Exception.Message; value=$null }
    }
}

# -------------------------
# Log helpers
# -------------------------
function Tail-File {
    param([string]$path, [int]$n, [int]$timeoutSec = 3)
    if ([string]::IsNullOrWhiteSpace($path) -or $n -le 0) { return @() }
    if (-not (Test-Path -LiteralPath $path)) { return @() }

    $r = Invoke-JobTimeout -TimeoutSec $timeoutSec -ArgumentList @($path, $n) -ScriptBlock {
        param($p, $k)
        try { return @(Get-Content -LiteralPath $p -Tail $k -ErrorAction Stop) } catch { return @() }
    }

    if (-not $r.ok) { return @() }
    return @($r.value)
}

function RedFlagHits {
    param([object[]]$lines, [string[]]$patterns)

    $hits = New-Object System.Collections.Generic.List[string]
    foreach ($ln in @($lines)) {
        if ($null -eq $ln) { continue }
        $s = [string]$ln
        if ($s.Length -eq 0) { continue }

        foreach ($pat in @($patterns)) {
            if ([string]::IsNullOrWhiteSpace($pat)) { continue }
            try {
                if ($s -match $pat) { $hits.Add($s) | Out-Null; break }
            } catch { }
        }
    }
    return $hits.ToArray()
}

function Get-LogsStatus {
    param([string]$mainLog, [string]$cycleGlob, [int]$tailLines, [string]$patternsStr)

    $tailLines = Safe-Int $tailLines 120 0 5000
    $patterns = Split-List $patternsStr
    if (@($patterns).Count -eq 0) { $patterns = @("ERROR","FATAL","CRITICAL","Exception","Traceback","Unhandled","Terminating") }

    $mainAbs = Resolve-RepoPath $mainLog
    $cycleAbsGlob = Resolve-RepoPath $cycleGlob

    $mainTail = Tail-File $mainAbs $tailLines 3
    $mainHits = RedFlagHits -lines $mainTail -patterns $patterns

    $cycleSel  = $null
    $cycleTail = @()
    $cycleHits = @()

    # HARD timeout for glob enumeration + select newest file
    $rr = Invoke-JobTimeout -TimeoutSec 6 -ArgumentList @($cycleAbsGlob) -ScriptBlock {
        param($glob)
        if ([string]::IsNullOrWhiteSpace($glob)) { return $null }
        $items = @()
        try { $items = @(Get-ChildItem -Path $glob -File -ErrorAction SilentlyContinue) } catch { $items = @() }
        if (@($items).Count -le 0) { return $null }
        try { return @($items | Sort-Object -Property LastWriteTime -Descending | Select-Object -First 1)[0].FullName } catch { return $null }
    }

    if ($rr.ok -and -not $rr.timed_out -and -not [string]::IsNullOrWhiteSpace($rr.value)) {
        $cycleSel = [string]$rr.value
        $cycleTail = Tail-File $cycleSel $tailLines 3
        $cycleHits = RedFlagHits -lines $cycleTail -patterns $patterns
    }

    $mainExists = $false
    try { if (-not [string]::IsNullOrWhiteSpace($mainAbs)) { $mainExists = Test-Path -LiteralPath $mainAbs } } catch { $mainExists = $false }

    $cycleExists = $false
    try { if (-not [string]::IsNullOrWhiteSpace($cycleSel)) { $cycleExists = Test-Path -LiteralPath $cycleSel } } catch { $cycleExists = $false }

    return @{
        red_flag_patterns = @($patterns)
        main_log = @{
            path = $mainAbs
            exists = $mainExists
            tail = @($mainTail)
            red_flags_count = [int](@($mainHits).Count)
            red_flags_sample = @($mainHits | Select-Object -First 10)
        }
        cycle_log = @{
            glob = $cycleAbsGlob
            selected_path = $cycleSel
            exists = $cycleExists
            tail = @($cycleTail)
            red_flags_count = [int](@($cycleHits).Count)
            red_flags_sample = @($cycleHits | Select-Object -First 10)
        }
    }
}

# -------------------------
# Other collectors (timeout-protected)
# -------------------------
function Get-TaskStatusSafe {
    param([string]$taskName, [int]$expectedIntervalMin, [int]$timeoutSec)

    $expectedIntervalMin = Safe-Int $expectedIntervalMin 5 1 1440
    $timeoutSec = Safe-Int $timeoutSec 6 1 60

    $st = @{
        name = $taskName
        exists = $false
        enabled = $null
        state = "UNKNOWN"
        last_run_time = $null
        next_run_time = $null
        last_task_result = $null
        last_task_result_hex = $null
        age_since_last_run_min = $null
        missed_runs_est = $null
        info_error = $null
        timed_out = $false
    }

    if ([string]::IsNullOrWhiteSpace($taskName)) { $st.state="MISSING"; $st.info_error="task_name_not_configured"; return $st }
    if ($null -eq (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) { $st.info_error="scheduledtask_cmdlets_missing"; return $st }

    $r = Invoke-JobTimeout -TimeoutSec $timeoutSec -ArgumentList @($taskName, $expectedIntervalMin) -ScriptBlock {
        param($tn, $exp)
        $ErrorActionPreference = "Stop"
        Import-Module ScheduledTasks -ErrorAction SilentlyContinue | Out-Null

        $out = @{
            name = $tn
            exists = $false
            enabled = $null
            state = "MISSING"
            last_run_time = $null
            next_run_time = $null
            last_task_result = $null
            last_task_result_hex = $null
            age_since_last_run_min = $null
            missed_runs_est = $null
            info_error = $null
        }

        try {
            $task = Get-ScheduledTask -TaskName $tn -ErrorAction Stop
            $info = Get-ScheduledTaskInfo -TaskName $tn -ErrorAction Stop

            $out.exists = $true
            $out.state = ($task.State -as [string])

            $en = $null
            try { if ($null -ne $task.Settings -and $null -ne $task.Settings.Enabled) { $en = [bool]$task.Settings.Enabled } } catch { $en = $null }
            if ($null -eq $en) { $en = ($out.state -ne "Disabled") }
            $out.enabled = $en

            if ($info.LastRunTime -and $info.LastRunTime -gt [DateTime]"2000-01-01") {
                $out.last_run_time = $info.LastRunTime.ToUniversalTime().ToString("o")
                $age = (New-TimeSpan -Start $info.LastRunTime -End (Get-Date)).TotalMinutes
                $out.age_since_last_run_min = [math]::Round($age, 2)
                $missed = [math]::Max(0, [math]::Floor($age / $exp) - 1)
                $out.missed_runs_est = [int]$missed
            }

            if ($info.NextRunTime -and $info.NextRunTime -gt [DateTime]"2000-01-01") {
                $out.next_run_time = $info.NextRunTime.ToUniversalTime().ToString("o")
            }

            $out.last_task_result = $info.LastTaskResult
            try { $out.last_task_result_hex = ('0x{0:X8}' -f [int]$info.LastTaskResult) } catch { $out.last_task_result_hex = $null }
        } catch {
            $out.info_error = $_.Exception.Message
        }

        return $out
    }

    if (-not $r.ok) {
        $st.info_error = $r.error
        $st.timed_out = [bool]$r.timed_out
        return $st
    }

    if ($null -eq $r.value) {
        $st.info_error = "TASK_QUERY_EMPTY"
        return $st
    }

    return $r.value
}

function Get-OpsProcessesSafe {
    param([string]$wrapName, [string]$innerName, [int]$timeoutSec)

    $timeoutSec = Safe-Int $timeoutSec 10 1 60
    $wrap = ($wrapName -as [string]).ToLowerInvariant()
    $inn  = ($innerName -as [string]).ToLowerInvariant()
    $repo = (Normalize-Path $Script:RepoRoot).ToLowerInvariant()

    $out = @{
        error = $null
        timed_out = $false
        wrapper_count = 0
        inner_count = 0
        total_count = 0
        processes = @()
    }

    if ([string]::IsNullOrWhiteSpace($wrap) -and [string]::IsNullOrWhiteSpace($inn)) {
        $out.error = "process_patterns_not_configured"
        return $out
    }

    $r = Invoke-JobTimeout -TimeoutSec $timeoutSec -ArgumentList @() -ScriptBlock {
        $ErrorActionPreference = "Stop"
        if ($null -ne (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) {
            return @(Get-CimInstance -ClassName Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction Stop)
        }
        return @(Get-WmiObject -Class Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction Stop)
    }

    if (-not $r.ok) {
        $out.error = $r.error
        $out.timed_out = [bool]$r.timed_out
        return $out
    }

    $items = New-Object System.Collections.Generic.List[object]
    foreach ($p in @($r.value)) {
        if ($null -eq $p) { continue }
        $cmd = $p.CommandLine
        if ([string]::IsNullOrWhiteSpace($cmd)) { continue }
        $cl = $cmd.ToLowerInvariant()

        $isW = (-not [string]::IsNullOrWhiteSpace($wrap)) -and $cl.Contains($wrap)
        $isI = (-not [string]::IsNullOrWhiteSpace($inn))  -and $cl.Contains($inn)
        if (-not ($isW -or $isI)) { continue }

        $scoped = $false
        if (-not [string]::IsNullOrWhiteSpace($repo) -and $cl.Contains($repo)) { $scoped = $true }
        if (-not $scoped -and ($cl.Contains("\scripts\") -or $cl.Contains("/scripts/"))) { $scoped = $true }

        $typ = if ($isW) { "WRAPPER" } elseif ($isI) { "INNER" } else { "UNKNOWN" }

        $items.Add(@{
            type = $typ
            pid  = [int]$p.ProcessId
            ppid = [int]$p.ParentProcessId
            name = ($p.Name -as [string])
            scoped_to_repo = $scoped
            command_line = $cmd
        }) | Out-Null
    }

    $out.wrapper_count = [int](@($items | Where-Object { $_.type -eq "WRAPPER" }).Count)
    $out.inner_count   = [int](@($items | Where-Object { $_.type -eq "INNER" }).Count)
    $out.total_count   = [int]$items.Count
    $out.processes     = $items.ToArray()

    return $out
}

function Get-SnapshotStatus {
    param([string]$globs, [double]$warnAgeMin, [double]$failAgeMin, [int]$minBytes)

    $warnAgeMin = Safe-Double $warnAgeMin 7.0 0.0
    $failAgeMin = Safe-Double $failAgeMin 20.0 0.0
    $minBytes   = Safe-Int $minBytes 16 0

    $paths   = Split-List $globs
    $checked = New-Object System.Collections.Generic.List[string]
    $files   = New-Object System.Collections.Generic.List[object]

    foreach ($g in $paths) {
        $abs = Resolve-RepoPath $g
        if ([string]::IsNullOrWhiteSpace($abs)) { continue }
        $checked.Add($abs) | Out-Null

        $rr = Invoke-JobTimeout -TimeoutSec 6 -ArgumentList @($abs) -ScriptBlock {
            param($p)
            try { return @(Get-ChildItem -Path $p -File -ErrorAction SilentlyContinue) } catch { return @() }
        }

        if ($rr.ok -and $rr.value) {
            foreach ($it in @($rr.value)) {
                if ($null -ne $it) { $files.Add($it) | Out-Null }
            }
        }
    }

    $st = @{
        paths_checked = $checked.ToArray()
        selected_path = $null
        exists = $false
        mtime_utc = $null
        age_min = $null
        bytes = $null
        warn_age_min = $warnAgeMin
        fail_age_min = $failAgeMin
        min_bytes = $minBytes
        status = "MISSING"
        info_error = $null
    }

    if ($files.Count -eq 0) {
        if ($checked.Count -eq 0) { $st.info_error = "snapshot_globs_not_configured_or_no_matches" }
        return $st
    }

    $sel = $null
    try { $sel = @($files | Sort-Object -Property LastWriteTime -Descending | Select-Object -First 1)[0] } catch { $sel = $null }
    if ($null -eq $sel) { $st.status="FAIL"; $st.info_error="snapshot_select_failed"; return $st }

    try {
        $st.selected_path = $sel.FullName
        $st.exists = $true
        $st.bytes = [int64]$sel.Length
        $st.mtime_utc = $sel.LastWriteTime.ToUniversalTime().ToString("o")
        $age = (New-TimeSpan -Start $sel.LastWriteTime -End (Get-Date)).TotalMinutes
        $st.age_min = [math]::Round($age, 2)

        if ($st.bytes -lt $minBytes)        { $st.status = "FAIL" }
        elseif ($age -gt $failAgeMin)       { $st.status = "FAIL" }
        elseif ($age -gt $warnAgeMin)       { $st.status = "WARN" }
        else                                { $st.status = "OK" }
    } catch {
        $st.status = "FAIL"
        $st.info_error = $_.Exception.Message
    }

    return $st
}

function Get-Locks {
    param([string]$lockList, [double]$staleAgeMin)

    $staleAgeMin = Safe-Double $staleAgeMin 30.0 0.0
    $paths = Split-List $lockList

    $items = New-Object System.Collections.Generic.List[object]
    foreach ($p in $paths) {
        $abs = Resolve-RepoPath $p
        if ([string]::IsNullOrWhiteSpace($abs)) { continue }

        $exists = $false
        try { $exists = Test-Path -LiteralPath $abs } catch { $exists = $false }

        $it = @{
            path = $abs
            exists = $exists
            age_min = $null
            stale = $null
            info_error = $null
        }

        if ($exists) {
            try {
                $fi = Get-Item -LiteralPath $abs -ErrorAction Stop
                $age = (New-TimeSpan -Start $fi.LastWriteTime -End (Get-Date)).TotalMinutes
                $it.age_min = [math]::Round($age, 2)
                $it.stale = ([double]$it.age_min -gt $staleAgeMin)
            } catch {
                $it.info_error = $_.Exception.Message
            }
        }

        $items.Add($it) | Out-Null
    }

    $staleCount = 0
    foreach ($it in $items) {
        if ($null -eq $it) { continue }
        if ($it.exists -eq $true -and $it.stale -eq $true) { $staleCount++ }
    }

    return @{
        stale_age_threshold_min = $staleAgeMin
        stale_count = [int]$staleCount
        locks = $items.ToArray()
    }
}

function Compute-HealthLevel {
    param([object[]]$Findings)

    $hasFail = $false
    $hasWarn = $false
    foreach ($x in @($Findings)) {
        if ($null -eq $x) { continue }
        $sev = $null
        try { $sev = $x.severity } catch { $sev = $null }
        if ($sev -eq "FAIL") { $hasFail = $true; break }
        if ($sev -eq "WARN") { $hasWarn = $true }
    }
    if ($hasFail) { return "FAIL" }
    if ($hasWarn) { return "WARN" }
    return "OK"
}

function Remediate-LogJsonl {
    param([hashtable]$entry)
    try {
        Ensure-Dir (Split-Path -Parent $Script:RemediationJsonlPath)
        $line = ($entry | ConvertTo-Json -Compress -Depth 12)
        $null = Append-FileUtf8NoBom -Path $Script:RemediationJsonlPath -Line $line
    } catch { }
}

# -------------------------
# Main
# -------------------------
Ensure-Dir $Script:DataDir
Ensure-Dir $Script:LogsDir

$defaults = @{
    wrapper_task_name = "ARGS_AutoLoop_5m"
    watchdog_task_name = "ARGS_OpsWatchdog_1m"
    wrapper_interval_min = 5

    wrapper_script_name = "ops_loop_5m_stage6c.ps1"
    inner_script_name = "auto_loop_5m.ps1"

    max_wrapper_processes = 1
    max_inner_processes = 1

    stop_flag_path = "args/data/stop.flag"

    snapshot_path_globs = "args/data/ibkr_open_orders_live*.jsonl;args/data/ibkr_open_orders_live*.json"
    snapshot_warn_age_min = 7
    snapshot_fail_age_min = 20
    snapshot_min_bytes = 16

    lock_paths = "args/data/ops_stage6c.lock;args/data/auto_loop.lock"
    lock_stale_min = 30

    main_log_path = "args/logs/ops_stage6c.log"
    cycle_log_glob = "args/logs/auto_loop_*.log"
    log_tail_lines = 120
    log_red_flag_patterns = "ERROR;FATAL;CRITICAL;Exception;Traceback;Unhandled;Terminating;Stack trace"

    health_output_path = "args/data/ops_health.json"

    task_timeout_sec = 6
    process_timeout_sec = 10
}

$exitCode = 2
$resultJson = "{}"

try {
    LogLine "INFO" ("Starting Stage7 watchdog. Remediate={0}. ConfigPath={1}" -f ([bool]$Remediate), (Resolve-RepoPath $ConfigPath))

    $cfgFile = Parse-FlatYaml $ConfigPath
    $cfg = Merge-Config $defaults $cfgFile

    if (-not [string]::IsNullOrWhiteSpace($cfg.health_output_path)) {
        $Script:HealthPath = Resolve-RepoPath $cfg.health_output_path
    } else {
        $Script:HealthPath = $Script:HealthPathDefault
    }

    $stopFlagAbs = Resolve-RepoPath $cfg.stop_flag_path
    $stopPresent = $false
    try { $stopPresent = (Test-Path -LiteralPath $stopFlagAbs) } catch { $stopPresent = $false }
    $opsState = if ($stopPresent) { "PAUSED" } else { "RUNNING" }

    $wrapperInterval = Safe-Int $cfg.wrapper_interval_min 5 1 1440
    $taskT = Safe-Int $cfg.task_timeout_sec 6 1 60
    $procT = Safe-Int $cfg.process_timeout_sec 10 1 60

    $taskWrapper  = Get-TaskStatusSafe $cfg.wrapper_task_name  $wrapperInterval $taskT
    $taskWatchdog = Get-TaskStatusSafe $cfg.watchdog_task_name 1 $taskT

    $snapshot = Get-SnapshotStatus $cfg.snapshot_path_globs ([double]$cfg.snapshot_warn_age_min) ([double]$cfg.snapshot_fail_age_min) ([int]$cfg.snapshot_min_bytes)
    $procs    = Get-OpsProcessesSafe $cfg.wrapper_script_name $cfg.inner_script_name $procT
    $locks    = Get-Locks $cfg.lock_paths ([double]$cfg.lock_stale_min)
    $logs     = Get-LogsStatus $cfg.main_log_path $cfg.cycle_log_glob ([int]$cfg.log_tail_lines) $cfg.log_red_flag_patterns

    $findings = @()

    function AddFinding([string]$sev, [string]$code, [string]$msg, [object]$details) {
        $script:findings += ,@{ severity=$sev; code=$code; message=$msg; details=$details }
    }
    function Sev([string]$sev) {
        if ($opsState -eq "PAUSED" -and $sev -eq "FAIL") { return "WARN" }
        return $sev
    }

    if ($taskWrapper.exists -ne $true) {
        AddFinding (Sev "FAIL") "TASK_WRAPPER_MISSING" "Wrapper scheduled task missing." @{ task=$cfg.wrapper_task_name; error=$taskWrapper.info_error; timed_out=$taskWrapper.timed_out }
    }

    if ($snapshot.status -eq "MISSING") {
        AddFinding (Sev "FAIL") "SNAPSHOT_MISSING" "Snapshot not found." @{ checked=$snapshot.paths_checked; error=$snapshot.info_error }
    } elseif ($snapshot.status -eq "FAIL") {
        AddFinding (Sev "FAIL") "SNAPSHOT_STALE" "Snapshot stale/invalid." @{ path=$snapshot.selected_path; age_min=$snapshot.age_min; bytes=$snapshot.bytes; error=$snapshot.info_error }
    } elseif ($snapshot.status -eq "WARN") {
        AddFinding "WARN" "SNAPSHOT_WARN" "Snapshot older than warn threshold." @{ path=$snapshot.selected_path; age_min=$snapshot.age_min; warn_age_min=$snapshot.warn_age_min }
    }

    if ($procs.error) {
        AddFinding "WARN" "PROC_ENUM_ERROR" "Failed to enumerate OPS processes." @{ error=$procs.error; timed_out=$procs.timed_out }
    } else {
        $maxW = Safe-Int $cfg.max_wrapper_processes 1 0 10
        $maxI = Safe-Int $cfg.max_inner_processes 1 0 10
        if (($procs.wrapper_count -gt $maxW) -or ($procs.inner_count -gt $maxI)) {
            AddFinding (Sev "FAIL") "OPS_OVERLAP" "OPS processes overlap beyond limits." @{
                wrapper_count=$procs.wrapper_count; max_wrapper=$maxW;
                inner_count=$procs.inner_count; max_inner=$maxI
            }
        }
    }

    if ([int]$locks.stale_count -gt 0) {
        AddFinding "WARN" "STALE_LOCKS" "Stale lock files detected." @{ stale_count=$locks.stale_count; threshold_min=$locks.stale_age_threshold_min }
    }

    if (([int]$logs.main_log.red_flags_count -gt 0) -or ([int]$logs.cycle_log.red_flags_count -gt 0)) {
        AddFinding "WARN" "LOG_REDFLAGS" "Red-flag patterns found in logs." @{ main_red_flags=$logs.main_log.red_flags_count; cycle_red_flags=$logs.cycle_log.red_flags_count }
    }

    $health = Compute-HealthLevel $findings

    # Remediation (safe-only) and skipped if stop.flag exists
    if ([bool]$Remediate -and -not $stopPresent) {
        $did = $false

        if ([int]$locks.stale_count -gt 0) {
            $dataDirAbs = Normalize-Path (Join-Path $Script:RepoRoot "args\data")
            foreach ($lk in @($locks.locks)) {
                if ($null -eq $lk) { continue }
                if ($lk.exists -ne $true -or $lk.stale -ne $true) { continue }
                if (-not (Is-UnderDir $lk.path $dataDirAbs)) { continue }
                try {
                    Remove-Item -LiteralPath $lk.path -Force -ErrorAction SilentlyContinue
                    Remediate-LogJsonl @{ ts_utc=(UtcIso); run_id=$Script:RunId; action="REMOVE_LOCK"; outcome="OK"; details=@{path=$lk.path} }
                    $did = $true
                } catch { }
            }
        }

        if (-not $procs.error) {
            $maxW = Safe-Int $cfg.max_wrapper_processes 1 0 10
            $maxI = Safe-Int $cfg.max_inner_processes 1 0 10
            if (($procs.wrapper_count -gt $maxW) -or ($procs.inner_count -gt $maxI)) {
                if ($null -ne (Get-Command Stop-ScheduledTask -ErrorAction SilentlyContinue)) {
                    try {
                        Stop-ScheduledTask -TaskName $cfg.wrapper_task_name -ErrorAction SilentlyContinue | Out-Null
                        Remediate-LogJsonl @{ ts_utc=(UtcIso); run_id=$Script:RunId; action="STOP_TASK"; outcome="OK"; details=@{task=$cfg.wrapper_task_name} }
                        $did = $true
                    } catch { }
                }
            }
        }

        LogLine "INFO" ("Remediation executed={0}" -f $did)
    }

    $result = @{
        stage = "Stage7"
        run_id = $Script:RunId
        ts_utc = (UtcIso)
        host = $Script:HostName
        repo_root = (Normalize-Path $Script:RepoRoot)
        script_path = (Normalize-Path (Join-Path $Script:RepoRoot "scripts\ops_watchdog_stage7.ps1"))
        config_path = (Resolve-RepoPath $ConfigPath)

        remediate_requested = [bool]$Remediate
        health_level = $health
        ops_state = $opsState

        stop_flag = @{ path=$stopFlagAbs; present=$stopPresent }
        tasks = @{ wrapper=$taskWrapper; watchdog=$taskWatchdog }
        snapshot = $snapshot
        processes = $procs
        locks = $locks
        logs = $logs
        findings = @($findings)
    }

    # FULL -> file
    $resultJsonFull = ($result | ConvertTo-Json -Depth 20)
    $null = Write-HealthJson $resultJsonFull

    # SUMMARY -> stdout
    $summary = @{
        stage = $result.stage
        run_id = $result.run_id
        ts_utc = $result.ts_utc
        host = $result.host
        health_level = $result.health_level
        ops_state = $result.ops_state
        config_path = $result.config_path
        stale_locks = [int]($result.locks.stale_count)
        main_red_flags = [int]($result.logs.main_log.red_flags_count)
        cycle_red_flags = [int]($result.logs.cycle_log.red_flags_count)
        proc_total = [int]($result.processes.total_count)
        task_wrapper_exists = [bool]($result.tasks.wrapper.exists)
        snapshot_status = [string]($result.snapshot.status)
    }
    $resultJson = ($summary | ConvertTo-Json -Depth 6)

    if ($result.health_level -eq "OK") { $exitCode = 0 }
    elseif ($result.health_level -eq "WARN") { $exitCode = 1 }
    else { $exitCode = 2 }

    LogLine "INFO" ("Watchdog complete. health={0} exit={1}" -f $result.health_level, $exitCode)
}
catch {
    $err = ($_ | Out-String)
    LogLine "ERROR" ("CRASH: {0}" -f $err)

    $fallback = @{
        stage = "Stage7"
        run_id = $Script:RunId
        ts_utc = (UtcIso)
        host = $Script:HostName
        repo_root = (Normalize-Path $Script:RepoRoot)
        script_path = (Normalize-Path (Join-Path $Script:RepoRoot "scripts\ops_watchdog_stage7.ps1"))
        config_path = (Resolve-RepoPath $ConfigPath)
        remediate_requested = [bool]$Remediate
        health_level = "FAIL"
        ops_state = "UNKNOWN"
        error = $err
    }

    # FULL fallback -> file
    $fallbackJsonFull = ($fallback | ConvertTo-Json -Depth 20)
    $null = Write-HealthJson $fallbackJsonFull

    # SUMMARY fallback -> stdout
    $summary = @{
        stage = $fallback.stage
        run_id = $fallback.run_id
        ts_utc = $fallback.ts_utc
        host = $fallback.host
        health_level = $fallback.health_level
        ops_state = $fallback.ops_state
        config_path = $fallback.config_path
        error = $fallback.error
    }
    $resultJson = ($summary | ConvertTo-Json -Depth 6)
    $exitCode = 2
}

Write-Output $resultJson
exit $exitCode
