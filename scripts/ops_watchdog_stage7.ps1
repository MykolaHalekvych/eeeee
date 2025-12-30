#requires -Version 5.1
<#
Stage 7: Ops Watchdog (production-stable)
- Windows PowerShell 5.1+
- StrictMode-safe
- Exactly one JSON in stdout (summary-only)
- Best-effort writes args/data/ops_health.json even on crash
- Optional remediation is operator-safe and skipped if stop.flag exists
- Exit codes: 0=OK, 1=WARN, 2=FAIL
- Hard timeouts via Start-Job + Wait-Job -Timeout + Stop/Remove-Job
#>

[CmdletBinding()]
param(
    [switch]$Remediate,
    [string]$ConfigPath = "args/data/ops_config.yaml"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$VerbosePreference = "SilentlyContinue"
$WarningPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"

# -----------------------------
# Globals
# -----------------------------
$script:RepoRoot = ""
$script:LogPath = ""
$script:AuditJsonlPath = ""
$script:DefaultHealthPath = ""
$script:StopJobSupportsForce = $false
$script:RemoveJobSupportsForce = $false

# -----------------------------
# Helpers (safe / strict)
# -----------------------------
function Get-UtcIsoNow { (Get-Date).ToUniversalTime().ToString("o") }
function New-RunId { ([Guid]::NewGuid().ToString("D")) }

function Ensure-Directory {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Normalize-FullPathLiteral {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $Path }
    try { [System.IO.Path]::GetFullPath($Path) } catch { $Path }
}

function Resolve-RepoPathLiteral {
    param([string]$RepoRoot, [string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $Path }
    if ([System.IO.Path]::IsPathRooted($Path)) { return (Normalize-FullPathLiteral -Path $Path) }
    return (Normalize-FullPathLiteral -Path (Join-Path -Path $RepoRoot -ChildPath $Path))
}

function Resolve-RepoPathPattern {
    param([string]$RepoRoot, [string]$Pattern)
    if ([string]::IsNullOrWhiteSpace($Pattern)) { return $Pattern }
    if ([System.IO.Path]::IsPathRooted($Pattern)) { return $Pattern }
    return (Join-Path -Path $RepoRoot -ChildPath $Pattern)
}

function Split-List {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return @() }
    @($Value -split "[;,]" | ForEach-Object { $_.Trim() } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function To-IntSafe {
    param([string]$Value, [int]$Default)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $Default }
    $n = 0
    if ([int]::TryParse($Value, [ref]$n)) { return $n }
    $Default
}

function Write-LogLine {
    param([ValidateSet("INFO","WARN","ERROR")][string]$Level, [string]$Message)
    if ([string]::IsNullOrWhiteSpace($script:LogPath)) { return }
    $ts = Get-UtcIsoNow
    $line = "$ts [$Level] $Message"
    try { Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue } catch { }
}

function Append-AuditJsonl {
    param([string]$RunId, [string]$Action, [string]$Outcome, [hashtable]$Details)
    if ([string]::IsNullOrWhiteSpace($script:AuditJsonlPath)) { return }
    $obj = @{
        ts_utc  = Get-UtcIsoNow
        run_id  = $RunId
        action  = $Action
        outcome = $Outcome
        details = @{}
    }
    if ($null -ne $Details) { $obj.details = $Details }
    $json = $obj | ConvertTo-Json -Compress -Depth 12
    try { Add-Content -LiteralPath $script:AuditJsonlPath -Value $json -Encoding UTF8 -ErrorAction SilentlyContinue } catch { }
}

function Read-FlatYaml {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    $lines = @()
    try { $lines = Get-Content -LiteralPath $Path -ErrorAction Stop } catch { return $map }

    foreach ($raw in $lines) {
        $line = [string]$raw
        if ($null -eq $line) { continue }
        $line = $line.Trim()
        if ($line.Length -eq 0) { continue }
        if ($line.StartsWith("#")) { continue }

        $hashIdx = $line.IndexOf("#")
        if ($hashIdx -ge 0) {
            $line = $line.Substring(0, $hashIdx).Trim()
            if ($line.Length -eq 0) { continue }
        }

        if ($line -match "^\s*([^:]+)\s*:\s*(.*)\s*$") {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim()
            if ($val.Length -ge 2) {
                if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
                    $val = $val.Substring(1, $val.Length - 2)
                }
            }
            if (-not [string]::IsNullOrWhiteSpace($key)) { $map[$key] = $val }
        }
    }
    $map
}

function Test-IsUnderDir {
    param([string]$Path, [string]$Dir)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    if ([string]::IsNullOrWhiteSpace($Dir)) { return $false }
    $pFull = Normalize-FullPathLiteral -Path $Path
    $dFull = Normalize-FullPathLiteral -Path $Dir
    if ([string]::IsNullOrWhiteSpace($pFull)) { return $false }
    if ([string]::IsNullOrWhiteSpace($dFull)) { return $false }
    if (-not $dFull.EndsWith("\")) { $dFull = $dFull + "\" }
    $pFull.StartsWith($dFull, [System.StringComparison]::OrdinalIgnoreCase)
}

function Init-JobForceSupport {
    try {
        $cmdStop = Get-Command Stop-Job -ErrorAction Stop
        if ($cmdStop.Parameters.ContainsKey("Force")) { $script:StopJobSupportsForce = $true }
    } catch { }
    try {
        $cmdRem = Get-Command Remove-Job -ErrorAction Stop
        if ($cmdRem.Parameters.ContainsKey("Force")) { $script:RemoveJobSupportsForce = $true }
    } catch { }
}

function Stop-And-RemoveJobSafe {
    param([System.Management.Automation.Job]$Job)
    if ($null -eq $Job) { return }
    try {
        if ($script:StopJobSupportsForce) { Stop-Job -Id $Job.Id -Force -ErrorAction SilentlyContinue | Out-Null }
        else { Stop-Job -Id $Job.Id -ErrorAction SilentlyContinue | Out-Null }
    } catch { }
    try {
        if ($script:RemoveJobSupportsForce) { Remove-Job -Id $Job.Id -Force -ErrorAction SilentlyContinue | Out-Null }
        else { Remove-Job -Id $Job.Id -ErrorAction SilentlyContinue | Out-Null }
    } catch { }
}

function Invoke-WithTimeout {
    param(
        [scriptblock]$ScriptBlock,
        [int]$TimeoutSec,
        [string]$Name = "op",
        [object[]]$ArgumentList = @()
    )

    $result = @{
        name        = $Name
        success     = $false
        timed_out   = $false
        error       = $null
        data        = $null
        duration_ms = 0
    }

    $job = $null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $job = Start-Job -ScriptBlock $ScriptBlock -ArgumentList $ArgumentList
        $completed = Wait-Job -Id $job.Id -Timeout $TimeoutSec
        if ($null -eq $completed) {
            $result.timed_out = $true
            $result.error = "TIMEOUT after ${TimeoutSec}s"
            Stop-And-RemoveJobSafe -Job $job
            return $result
        }
        $jobErr = $null
        $data = Receive-Job -Id $job.Id -ErrorAction SilentlyContinue -ErrorVariable jobErr
        $result.data = $data
        if ($null -ne $jobErr -and $jobErr.Count -gt 0) {
            $result.error = ($jobErr | Select-Object -First 1 | ForEach-Object { $_.ToString() })
        }
        $result.success = ([string]::IsNullOrWhiteSpace([string]$result.error))
    } catch {
        $result.error = $_.Exception.Message
    } finally {
        if ($null -ne $job) { Stop-And-RemoveJobSafe -Job $job }
        $sw.Stop()
        $result.duration_ms = [int]$sw.ElapsedMilliseconds
    }
    $result
}

function Add-Finding {
    param(
        [System.Collections.ArrayList]$Findings,
        [ValidateSet("WARN","FAIL")][string]$Severity,
        [string]$Code,
        [string]$Message,
        [hashtable]$Details
    )
    $item = @{
        severity = $Severity
        code     = $Code
        message  = $Message
        details  = @{}
    }
    if ($null -ne $Details) { $item.details = $Details }
    [void]$Findings.Add($item)
}

function Pick-Error {
    param([object]$Primary, [object]$Fallback)
    $p = ""
    if ($null -ne $Primary) { $p = [string]$Primary }
    if (-not [string]::IsNullOrWhiteSpace($p)) { return $p }
    $f = ""
    if ($null -ne $Fallback) { $f = [string]$Fallback }
    if (-not [string]::IsNullOrWhiteSpace($f)) { return $f }
    return $null
}

# -----------------------------
# Safe ops (timeouts)
# -----------------------------
function Get-ScheduledTaskStatusSafe {
    param([string]$TaskName, [int]$TimeoutSec)

    $sb = {
        param([string]$Name)
        $out = @{
            name             = $Name
            exists           = $null
            state            = $null
            last_run_utc     = $null
            next_run_utc     = $null
            last_task_result = $null
            error            = $null
        }
        try {
            $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
            $out.exists = $true
            try { $out.state = [string]$task.State } catch { }
            try {
                $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction Stop
                if ($null -ne $info.LastRunTime -and $info.LastRunTime -ne [datetime]::MinValue) {
                    $out.last_run_utc = $info.LastRunTime.ToUniversalTime().ToString("o")
                }
                if ($null -ne $info.NextRunTime -and $info.NextRunTime -ne [datetime]::MinValue) {
                    $out.next_run_utc = $info.NextRunTime.ToUniversalTime().ToString("o")
                }
                $out.last_task_result = $info.LastTaskResult
            } catch {
                $out.error = $_.Exception.Message
            }
        } catch {
            $msg = $_.Exception.Message
            if ($msg -match "cannot find" -or $msg -match "No MSFT_ScheduledTask objects found" -or $msg -match "The system cannot find") {
                $out.exists = $false
            } else {
                $out.exists = $null
                $out.error = $msg
            }
        }
        $out
    }

    $r = Invoke-WithTimeout -ScriptBlock $sb -TimeoutSec $TimeoutSec -Name ("scheduledtask:" + $TaskName) -ArgumentList @($TaskName)
    $d = $r.data | Select-Object -First 1

    @{
        name             = $TaskName
        exists           = $d.exists
        state            = $d.state
        last_run_utc     = $d.last_run_utc
        next_run_utc     = $d.next_run_utc
        last_task_result = $d.last_task_result
        timed_out        = $r.timed_out
        error            = (Pick-Error -Primary $d.error -Fallback $r.error)
        duration_ms      = $r.duration_ms
    }
}

function Get-LatestFileFromGlobsSafe {
    param([string[]]$Globs, [int]$TimeoutSec)

    $sb = {
        param([object]$PatternsBox)
        $Patterns = @($PatternsBox)
        $res = @{
            found = $false
            newest_path = $null
            newest_bytes = $null
            newest_lastwrite_utc = $null
            match_count = 0
            error = $null
        }
        try {
            $newest = $null
            foreach ($pat in $Patterns) {
                if ([string]::IsNullOrWhiteSpace([string]$pat)) { continue }
                foreach ($f in Get-ChildItem -Path $pat -File -ErrorAction SilentlyContinue) {
                    $res.match_count++
                    if ($null -eq $newest -or $f.LastWriteTimeUtc -gt $newest.LastWriteTimeUtc) { $newest = $f }
                }
            }
            if ($null -ne $newest) {
                $res.found = $true
                $res.newest_path = $newest.FullName
                $res.newest_bytes = [int64]$newest.Length
                $res.newest_lastwrite_utc = $newest.LastWriteTimeUtc.ToString("o")
            }
        } catch {
            $res.error = $_.Exception.Message
        }
        $res
    }

    # IMPORTANT: box the array as a single argument
    $r = Invoke-WithTimeout -ScriptBlock $sb -TimeoutSec $TimeoutSec -Name "glob:newest" -ArgumentList @([object]$Globs)
    $d = $r.data | Select-Object -First 1

    @{
        found = [bool]$d.found
        newest_path = $d.newest_path
        newest_bytes = $d.newest_bytes
        newest_lastwrite_utc = $d.newest_lastwrite_utc
        match_count = [int]$d.match_count
        timed_out = $r.timed_out
        error = (Pick-Error -Primary $d.error -Fallback $r.error)
        duration_ms = $r.duration_ms
    }
}

function Get-ProcessCountsSafe {
    param([string]$WrapperScriptName, [string]$InnerScriptName, [int]$TimeoutSec)

    $sb = {
        param([string]$WrapperName, [string]$InnerName)
        $out = @{
            wrapper_count = 0
            inner_count = 0
            total_count = 0
            sample_wrapper_pids = @()
            sample_inner_pids = @()
            error = $null
        }
        try {
            $wp = [regex]::Escape($WrapperName)
            $ip = [regex]::Escape($InnerName)

            $procs = @()
            try { $procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction Stop) }
            catch { $procs = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop) }

            foreach ($p in $procs) {
                $cmd = $p.CommandLine
                if ([string]::IsNullOrWhiteSpace($cmd)) { continue }
                if ($cmd -match $wp) {
                    $out.wrapper_count++
                    if ($out.sample_wrapper_pids.Count -lt 6) { $out.sample_wrapper_pids += $p.ProcessId }
                }
                if ($cmd -match $ip) {
                    $out.inner_count++
                    if ($out.sample_inner_pids.Count -lt 6) { $out.sample_inner_pids += $p.ProcessId }
                }
            }
            $out.total_count = $out.wrapper_count + $out.inner_count
        } catch {
            $out.error = $_.Exception.Message
        }
        $out
    }

    $r = Invoke-WithTimeout -ScriptBlock $sb -TimeoutSec $TimeoutSec -Name "cim:process_counts" -ArgumentList @($WrapperScriptName, $InnerScriptName)
    $d = $r.data | Select-Object -First 1

    @{
        wrapper_count = $d.wrapper_count
        inner_count = $d.inner_count
        total_count = $d.total_count
        sample_wrapper_pids = @($d.sample_wrapper_pids)
        sample_inner_pids = @($d.sample_inner_pids)
        timed_out = $r.timed_out
        error = (Pick-Error -Primary $d.error -Fallback $r.error)
        duration_ms = $r.duration_ms
    }
}

function Get-RecentFilesFromGlobSafe {
    param([string]$Glob, [int]$MaxFiles, [int]$TimeoutSec)

    $sb = {
        param([string]$Pattern, [int]$TakeN)
        $out = @{ total_matches = 0; files = @(); error = $null }
        try {
            $buf = @()
            foreach ($f in Get-ChildItem -Path $Pattern -File -ErrorAction SilentlyContinue) {
                $out.total_matches++
                $buf += @{ path = $f.FullName; lastwrite_utc = $f.LastWriteTimeUtc.ToString("o") }
                if ($buf.Count -gt 1000) {
                    $buf = @($buf | Sort-Object lastwrite_utc -Descending | Select-Object -First ([Math]::Max(10, $TakeN*4)))
                }
            }
            $out.files = @($buf | Sort-Object lastwrite_utc -Descending | Select-Object -First $TakeN)
        } catch { $out.error = $_.Exception.Message }
        $out
    }

    $r = Invoke-WithTimeout -ScriptBlock $sb -TimeoutSec $TimeoutSec -Name "glob:recent_files" -ArgumentList @($Glob, $MaxFiles)
    $d = $r.data | Select-Object -First 1

    @{
        total_matches = [int]$d.total_matches
        files = @($d.files)
        timed_out = $r.timed_out
        error = (Pick-Error -Primary $d.error -Fallback $r.error)
        duration_ms = $r.duration_ms
    }
}

function Get-FileTailRedFlagsSafe {
    param([string]$Path, [int]$TailLines, [string[]]$PatternStrings, [int]$TimeoutSec)

    $sb = {
        param([string]$LiteralPath, [int]$Lines, [object]$PatternsBox)
        $Patterns = @($PatternsBox)
        $out = @{
            path = $LiteralPath
            exists = $false
            red_flag_hits = 0
            red_flag_patterns = @()
            sample_hits = @()
            tail = @()
            error = $null
        }
        try {
            if (Test-Path -LiteralPath $LiteralPath) {
                $out.exists = $true
                $tail = Get-Content -LiteralPath $LiteralPath -Tail $Lines -ErrorAction Stop
                if ($tail -isnot [System.Array]) { $tail = @($tail) }
                $out.tail = @($tail)

                $regexes = @()
                foreach ($p in $Patterns) {
                    if ([string]::IsNullOrWhiteSpace([string]$p)) { continue }
                    try { $regexes += (New-Object System.Text.RegularExpressions.Regex($p, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) }
                    catch { $regexes += (New-Object System.Text.RegularExpressions.Regex([regex]::Escape($p), [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) }
                }

                $matched = @{}
                $samples = @()

                foreach ($line in $out.tail) {
                    foreach ($rx in $regexes) {
                        if ($rx.IsMatch([string]$line)) {
                            $out.red_flag_hits++
                            $matched[$rx.ToString()] = $true
                            if ($samples.Count -lt 12) { $samples += @{ pattern = $rx.ToString(); line = [string]$line } }
                        }
                    }
                }

                $out.red_flag_patterns = @($matched.Keys)
                $out.sample_hits = @($samples)
            }
        } catch { $out.error = $_.Exception.Message }
        $out
    }

    # IMPORTANT: box the pattern array as a single argument
    $r = Invoke-WithTimeout -ScriptBlock $sb -TimeoutSec $TimeoutSec -Name "tail:redflags" -ArgumentList @($Path, $TailLines, [object]$PatternStrings)
    $d = $r.data | Select-Object -First 1

    @{
        path = $Path
        exists = [bool]$d.exists
        red_flag_hits = [int]$d.red_flag_hits
        red_flag_patterns = @($d.red_flag_patterns)
        sample_hits = @($d.sample_hits)
        tail = @($d.tail)
        timed_out = $r.timed_out
        error = (Pick-Error -Primary $d.error -Fallback $r.error)
        duration_ms = $r.duration_ms
    }
}

function Write-JsonFileSafe {
    param([string]$Path, [string]$Json, [int]$TimeoutSec)

    $sb = {
        param([string]$OutPath, [string]$Content)
        $o = @{ success = $false; error = $null }
        try {
            $dir = Split-Path -Parent $OutPath
            if (-not [string]::IsNullOrWhiteSpace($dir) -and -not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Path $dir -Force | Out-Null
            }
            Set-Content -LiteralPath $OutPath -Value $Content -Encoding UTF8 -Force
            $o.success = $true
        } catch { $o.error = $_.Exception.Message }
        $o
    }

    $r = Invoke-WithTimeout -ScriptBlock $sb -TimeoutSec $TimeoutSec -Name ("writejson:" + $Path) -ArgumentList @($Path, $Json)
    $d = $r.data | Select-Object -First 1

    @{
        success = [bool]$d.success
        timed_out = $r.timed_out
        error = (Pick-Error -Primary $d.error -Fallback $r.error)
        duration_ms = $r.duration_ms
    }
}

function Remove-ItemSafeWithTimeout {
    param([string]$Path, [int]$TimeoutSec)

    $sb = {
        param([string]$P)
        $o = @{ success = $false; error = $null }
        try {
            if (Test-Path -LiteralPath $P) { Remove-Item -LiteralPath $P -Force -ErrorAction Stop }
            $o.success = $true
        } catch { $o.error = $_.Exception.Message }
        $o
    }

    $r = Invoke-WithTimeout -ScriptBlock $sb -TimeoutSec $TimeoutSec -Name ("rm:" + $Path) -ArgumentList @($Path)
    $d = $r.data | Select-Object -First 1

    @{
        success = [bool]$d.success
        timed_out = $r.timed_out
        error = (Pick-Error -Primary $d.error -Fallback $r.error)
        duration_ms = $r.duration_ms
    }
}

function Stop-ScheduledTaskSafeWithTimeout {
    param([string]$TaskName, [int]$TimeoutSec)

    $sb = {
        param([string]$Name)
        $o = @{ success = $false; error = $null }
        try {
            $cmd = Get-Command Stop-ScheduledTask -ErrorAction SilentlyContinue
            if ($null -eq $cmd) { $o.error = "Stop-ScheduledTask cmdlet not available"; return $o }
            Stop-ScheduledTask -TaskName $Name -ErrorAction Stop | Out-Null
            $o.success = $true
        } catch { $o.error = $_.Exception.Message }
        $o
    }

    $r = Invoke-WithTimeout -ScriptBlock $sb -TimeoutSec $TimeoutSec -Name ("stop_task:" + $TaskName) -ArgumentList @($TaskName)
    $d = $r.data | Select-Object -First 1

    @{
        success = [bool]$d.success
        timed_out = $r.timed_out
        error = (Pick-Error -Primary $d.error -Fallback $r.error)
        duration_ms = $r.duration_ms
    }
}

# -----------------------------
# MAIN (only final JSON to stdout)
# -----------------------------
Init-JobForceSupport

$runId = New-RunId
$tsUtc = Get-UtcIsoNow
$hostName = $env:COMPUTERNAME
$scriptPath = $MyInvocation.MyCommand.Path
$scriptDir = Split-Path -Parent $scriptPath
$repoRoot = Split-Path -Parent $scriptDir
$script:RepoRoot = $repoRoot

$script:LogPath = Resolve-RepoPathLiteral -RepoRoot $repoRoot -Path "args/logs/ops_watchdog.log"
Ensure-Directory -Path (Split-Path -Parent $script:LogPath)

$script:AuditJsonlPath = Resolve-RepoPathLiteral -RepoRoot $repoRoot -Path "args/data/ops_remediation.jsonl"
Ensure-Directory -Path (Split-Path -Parent $script:AuditJsonlPath)

$script:DefaultHealthPath = Resolve-RepoPathLiteral -RepoRoot $repoRoot -Path "args/data/ops_health.json"
Ensure-Directory -Path (Split-Path -Parent $script:DefaultHealthPath)

Write-LogLine -Level "INFO" -Message "Stage7 start run_id=$runId script=$scriptPath repo_root=$repoRoot remediate=$($Remediate.IsPresent)"

# Resolve config path
$configPathFull = $ConfigPath
if (-not [System.IO.Path]::IsPathRooted($configPathFull)) { $configPathFull = Join-Path -Path $repoRoot -ChildPath $configPathFull }
$configPathFull = Normalize-FullPathLiteral -Path $configPathFull

$defaults = @{
    wrapper_task_name       = "ARGS_AutoLoop_5m"
    watchdog_task_name      = "ARGS_OpsWatchdog_1m"
    wrapper_interval_min    = "5"
    wrapper_script_name     = "ops_loop_5m_stage6c.ps1"
    inner_script_name       = "auto_loop_5m.ps1"
    max_wrapper_processes   = "1"
    max_inner_processes     = "1"
    stop_flag_path          = "args/data/stop.flag"
    snapshot_path_globs     = "args/data/ibkr_open_orders_live*.jsonl;args/data/ibkr_open_orders_live*.json"
    snapshot_warn_age_min   = "7"
    snapshot_fail_age_min   = "20"
    snapshot_min_bytes      = "16"
    lock_paths              = "args/data/ops_stage6c.lock;args/data/auto_loop.lock"
    lock_stale_min          = "30"
    main_log_path           = "args/logs/ops_stage6c.log"
    cycle_log_glob          = "args/logs/auto_loop_*.log"
    log_tail_lines          = "120"
    log_red_flag_patterns   = ""
    health_output_path      = "args/data/ops_health.json"
    task_timeout_sec        = "6"
    process_timeout_sec     = "10"
}

$yaml = @{}
try { $yaml = Read-FlatYaml -Path $configPathFull } catch { $yaml = @{} }

$cfg = @{}
foreach ($k in $defaults.Keys) {
    $val = $null
    if ($yaml.ContainsKey($k)) { $val = $yaml[$k] }
    if ([string]::IsNullOrWhiteSpace([string]$val)) { $val = $defaults[$k] }
    $cfg[$k] = [string]$val
}

$taskTimeoutSec     = To-IntSafe -Value $cfg.task_timeout_sec -Default 6
$processTimeoutSec  = To-IntSafe -Value $cfg.process_timeout_sec -Default 10
$maxWrapper         = To-IntSafe -Value $cfg.max_wrapper_processes -Default 1
$maxInner           = To-IntSafe -Value $cfg.max_inner_processes -Default 1
$snapshotWarnAgeMin = To-IntSafe -Value $cfg.snapshot_warn_age_min -Default 7
$snapshotFailAgeMin = To-IntSafe -Value $cfg.snapshot_fail_age_min -Default 20
$snapshotMinBytes   = [int64](To-IntSafe -Value $cfg.snapshot_min_bytes -Default 16)
$lockStaleMin       = To-IntSafe -Value $cfg.lock_stale_min -Default 30
$logTailLines       = To-IntSafe -Value $cfg.log_tail_lines -Default 120

$stopFlagPath = Resolve-RepoPathLiteral -RepoRoot $repoRoot -Path $cfg.stop_flag_path
$mainLogPath  = Resolve-RepoPathLiteral -RepoRoot $repoRoot -Path $cfg.main_log_path

$snapshotGlobs = @()
foreach ($g in Split-List -Value $cfg.snapshot_path_globs) { $snapshotGlobs += (Resolve-RepoPathPattern -RepoRoot $repoRoot -Pattern $g) }

$cycleGlob = Resolve-RepoPathPattern -RepoRoot $repoRoot -Pattern $cfg.cycle_log_glob

$lockPaths = @()
foreach ($lp in Split-List -Value $cfg.lock_paths) { $lockPaths += (Resolve-RepoPathLiteral -RepoRoot $repoRoot -Path $lp) }

$healthOutputPathCfg = Resolve-RepoPathLiteral -RepoRoot $repoRoot -Path $cfg.health_output_path
$redFlagPatterns = Split-List -Value $cfg.log_red_flag_patterns

$stopFlagPresent = $false
try { $stopFlagPresent = (Test-Path -LiteralPath $stopFlagPath) } catch { $stopFlagPresent = $false }
$opsState = if ($stopFlagPresent) { "PAUSED" } else { "RUNNING" }

$findings = New-Object System.Collections.ArrayList

$payload = @{
    stage = "Stage7"
    run_id = $runId
    ts_utc = $tsUtc
    host = $hostName
    repo_root = $repoRoot
    script_path = $scriptPath
    config_path = $configPathFull

    remediate_requested = [bool]$Remediate.IsPresent
    health_level = "OK"
    ops_state = $opsState

    stop_flag = @{ path = $stopFlagPath; present = $stopFlagPresent }
    tasks = @{}
    snapshot = @{}
    processes = @{}
    locks = @{}
    logs = @{}
    findings = @()
    remediation = @{ attempted=$false; skipped=$false; skipped_reason=$null; actions=@() }
}

try {
    # Tasks
    $wrapperTask = Get-ScheduledTaskStatusSafe -TaskName $cfg.wrapper_task_name -TimeoutSec $taskTimeoutSec
    $watchdogTask = Get-ScheduledTaskStatusSafe -TaskName $cfg.watchdog_task_name -TimeoutSec $taskTimeoutSec
    $payload.tasks = @{ wrapper = $wrapperTask; watchdog = $watchdogTask }

    if ($wrapperTask.timed_out -or -not [string]::IsNullOrWhiteSpace([string]$wrapperTask.error)) {
        Add-Finding $findings "WARN" "TASK_WRAPPER_QUERY_ISSUE" "Unable to reliably query wrapper scheduled task" @{
            task_name = $cfg.wrapper_task_name; timed_out = $wrapperTask.timed_out; error = $wrapperTask.error
        }
    } elseif ($wrapperTask.exists -eq $false) {
        $sev = if ($stopFlagPresent) { "WARN" } else { "FAIL" }
        Add-Finding $findings $sev "TASK_WRAPPER_MISSING" "Wrapper scheduled task not found" @{
            task_name = $cfg.wrapper_task_name; stop_flag_present = $stopFlagPresent
        }
    }

    # Snapshot
    $snap = Get-LatestFileFromGlobsSafe -Globs $snapshotGlobs -TimeoutSec $taskTimeoutSec
    $snapshotObj = @{
        globs = @($snapshotGlobs)
        found = $snap.found
        path = $snap.newest_path
        bytes = $snap.newest_bytes
        lastwrite_utc = $snap.newest_lastwrite_utc
        age_min = $null
        min_bytes = $snapshotMinBytes
        warn_age_min = $snapshotWarnAgeMin
        fail_age_min = $snapshotFailAgeMin
        status = "MISSING"
        timed_out = $snap.timed_out
        error = $snap.error
        duration_ms = $snap.duration_ms
        match_count = $snap.match_count
    }

    if ($snap.timed_out -or -not [string]::IsNullOrWhiteSpace([string]$snap.error)) {
        $snapshotObj.status = "WARN"
        Add-Finding $findings "WARN" "SNAPSHOT_QUERY_ISSUE" "Unable to reliably evaluate snapshot files (timeout/error)" @{
            timed_out = $snap.timed_out; error = $snap.error; globs = @($snapshotGlobs)
        }
    } elseif (-not $snap.found) {
        $snapshotObj.status = "MISSING"
        Add-Finding $findings "FAIL" "SNAPSHOT_MISSING" "Snapshot file missing" @{ globs = @($snapshotGlobs) }
    } else {
        try {
            $lw = [datetime]::Parse($snap.newest_lastwrite_utc)
            $snapshotObj.age_min = [int][Math]::Floor(((Get-Date).ToUniversalTime() - $lw).TotalMinutes)
        } catch { $snapshotObj.age_min = $null }

        if ($null -eq $snapshotObj.bytes -or [int64]$snapshotObj.bytes -lt $snapshotMinBytes) {
            $snapshotObj.status = "FAIL"
            Add-Finding $findings "FAIL" "SNAPSHOT_TOO_SMALL" "Snapshot file size below minimum" @{
                path = $snapshotObj.path; bytes = $snapshotObj.bytes; min_bytes = $snapshotMinBytes
            }
        } elseif ($null -ne $snapshotObj.age_min -and $snapshotObj.age_min -gt $snapshotFailAgeMin) {
            $snapshotObj.status = "FAIL"
            Add-Finding $findings "FAIL" "SNAPSHOT_STALE_FAIL" "Snapshot file too old (FAIL threshold)" @{
                path = $snapshotObj.path; age_min = $snapshotObj.age_min; fail_age_min = $snapshotFailAgeMin
            }
        } elseif ($null -ne $snapshotObj.age_min -and $snapshotObj.age_min -gt $snapshotWarnAgeMin) {
            $snapshotObj.status = "WARN"
            Add-Finding $findings "WARN" "SNAPSHOT_STALE_WARN" "Snapshot file stale (WARN threshold)" @{
                path = $snapshotObj.path; age_min = $snapshotObj.age_min; warn_age_min = $snapshotWarnAgeMin
            }
        } else {
            $snapshotObj.status = "OK"
        }
    }
    $payload.snapshot = $snapshotObj

    # Processes
    $proc = Get-ProcessCountsSafe -WrapperScriptName $cfg.wrapper_script_name -InnerScriptName $cfg.inner_script_name -TimeoutSec $processTimeoutSec
    $payload.processes = @{
        wrapper_script_name = $cfg.wrapper_script_name
        inner_script_name = $cfg.inner_script_name
        max_wrapper_processes = $maxWrapper
        max_inner_processes = $maxInner
        wrapper_count = $proc.wrapper_count
        inner_count = $proc.inner_count
        total_count = $proc.total_count
        sample_wrapper_pids = @($proc.sample_wrapper_pids)
        sample_inner_pids = @($proc.sample_inner_pids)
        timed_out = $proc.timed_out
        error = $proc.error
        duration_ms = $proc.duration_ms
    }

    if ($proc.timed_out -or -not [string]::IsNullOrWhiteSpace([string]$proc.error)) {
        Add-Finding $findings "WARN" "PROCESS_ENUMERATION_ISSUE" "Process enumeration failed/timeout" @{
            timed_out = $proc.timed_out; error = $proc.error
        }
    } else {
        $tooMany = (($proc.wrapper_count -ne $null -and [int]$proc.wrapper_count -gt $maxWrapper) -or
                    ($proc.inner_count -ne $null -and [int]$proc.inner_count -gt $maxInner))
        if ($tooMany) {
            $sev = if ($stopFlagPresent) { "WARN" } else { "FAIL" }
            Add-Finding $findings $sev "PROCESS_LIMIT_EXCEEDED" "Process count exceeds configured limits" @{
                wrapper_count=$proc.wrapper_count; max_wrapper_processes=$maxWrapper
                inner_count=$proc.inner_count; max_inner_processes=$maxInner
                stop_flag_present=$stopFlagPresent
                sample_wrapper_pids=@($proc.sample_wrapper_pids)
                sample_inner_pids=@($proc.sample_inner_pids)
            }
        }
    }

    # Locks
    $lockItems = @()
    $staleCount = 0
    foreach ($lp in $lockPaths) {
        $exists = $false; $ageMin = $null; $stale = $false
        try {
            if (Test-Path -LiteralPath $lp) {
                $exists = $true
                $it = Get-Item -LiteralPath $lp -ErrorAction Stop
                $ageMin = [int][Math]::Floor(((Get-Date).ToUniversalTime() - $it.LastWriteTimeUtc).TotalMinutes)
                if ($ageMin -gt $lockStaleMin) { $stale = $true }
            }
        } catch { }
        if ($stale) {
            $staleCount++
            Add-Finding $findings "WARN" "LOCK_STALE" "Stale lock file detected" @{ path=$lp; age_min=$ageMin; stale_min=$lockStaleMin }
        }
        $lockItems += @{ path=$lp; exists=$exists; age_min=$ageMin; stale=$stale; stale_min=$lockStaleMin }
    }
    $payload.locks = @{ paths=@($lockPaths); items=@($lockItems); stale_count=$staleCount }

    # Logs
    $mainLog = Get-FileTailRedFlagsSafe -Path $mainLogPath -TailLines $logTailLines -PatternStrings $redFlagPatterns -TimeoutSec $taskTimeoutSec
    $cycleEnum = Get-RecentFilesFromGlobSafe -Glob $cycleGlob -MaxFiles 3 -TimeoutSec $taskTimeoutSec

    $cycleFilesChecked = @()
    $cycleHits = 0
    $cycleSamples = @()

    if (-not $cycleEnum.timed_out -and [string]::IsNullOrWhiteSpace([string]$cycleEnum.error)) {
        foreach ($f in $cycleEnum.files) {
            $p = [string]$f.path
            if ([string]::IsNullOrWhiteSpace($p)) { continue }
            $r = Get-FileTailRedFlagsSafe -Path $p -TailLines $logTailLines -PatternStrings $redFlagPatterns -TimeoutSec $taskTimeoutSec
            $cycleFilesChecked += @{
                path=$p; lastwrite_utc=$f.lastwrite_utc
                red_flag_hits=$r.red_flag_hits; timed_out=$r.timed_out; error=$r.error
                red_flag_patterns=@($r.red_flag_patterns); sample_hits=@($r.sample_hits)
            }
            $cycleHits += [int]$r.red_flag_hits
            foreach ($s in @($r.sample_hits)) { if ($cycleSamples.Count -ge 12) { break }; $cycleSamples += $s }
        }
    }

    $payload.logs = @{
        main = @{
            path=$mainLog.path; exists=$mainLog.exists; tail_lines=$logTailLines
            timed_out=$mainLog.timed_out; error=$mainLog.error
            red_flag_hits=$mainLog.red_flag_hits
            red_flag_patterns=@($mainLog.red_flag_patterns)
            sample_hits=@($mainLog.sample_hits)
            tail=@($mainLog.tail)
        }
        cycle = @{
            glob=$cycleGlob; enum_timed_out=$cycleEnum.timed_out; enum_error=$cycleEnum.error
            total_matches=$cycleEnum.total_matches
            files_checked=@($cycleFilesChecked)
            red_flag_hits=$cycleHits
            sample_hits=@($cycleSamples)
        }
        red_flag_patterns=@($redFlagPatterns)
    }

    if ([int]$mainLog.red_flag_hits -gt 0) {
        Add-Finding $findings "WARN" "LOG_RED_FLAGS_MAIN" "Red-flag patterns found in main log tail" @{
            path=$mainLog.path; red_flag_hits=$mainLog.red_flag_hits; patterns=@($mainLog.red_flag_patterns)
        }
    }
    if ([int]$cycleHits -gt 0) {
        Add-Finding $findings "WARN" "LOG_RED_FLAGS_CYCLE" "Red-flag patterns found in cycle logs tail(s)" @{
            glob=$cycleGlob; red_flag_hits=$cycleHits
        }
    }

    # Remediation (safe-only)
    if ($Remediate.IsPresent) {
        if ($stopFlagPresent) {
            $payload.remediation.skipped = $true
            $payload.remediation.skipped_reason = "STOP_FLAG_PRESENT"
        } else {
            $payload.remediation.attempted = $true
            $argsDataDir = Resolve-RepoPathLiteral -RepoRoot $repoRoot -Path "args/data"

            foreach ($li in $lockItems) {
                if (-not $li.exists) { continue }
                if (-not $li.stale) { continue }
                $lockPath = [string]$li.path

                $action = "remove_stale_lock"
                $details = @{ path=$lockPath; age_min=$li.age_min; stale_min=$lockStaleMin }

                if (-not (Test-IsUnderDir -Path $lockPath -Dir $argsDataDir)) {
                    $details.reason = "outside_args_data"
                    $payload.remediation.actions += @{ action=$action; outcome="SKIP"; details=$details }
                    Append-AuditJsonl -RunId $runId -Action $action -Outcome "SKIP" -Details $details
                    continue
                }

                $rm = Remove-ItemSafeWithTimeout -Path $lockPath -TimeoutSec $taskTimeoutSec
                if ($rm.timed_out -or -not $rm.success) {
                    $details.error = $rm.error; $details.timed_out = $rm.timed_out
                    $payload.remediation.actions += @{ action=$action; outcome="FAIL"; details=$details }
                    Append-AuditJsonl -RunId $runId -Action $action -Outcome "FAIL" -Details $details
                } else {
                    $payload.remediation.actions += @{ action=$action; outcome="OK"; details=$details }
                    Append-AuditJsonl -RunId $runId -Action $action -Outcome "OK" -Details $details
                }
            }

            $limitsExceeded = $false
            if ($proc.wrapper_count -ne $null -and [int]$proc.wrapper_count -gt $maxWrapper) { $limitsExceeded = $true }
            if ($proc.inner_count -ne $null -and [int]$proc.inner_count -gt $maxInner) { $limitsExceeded = $true }

            if ($limitsExceeded) {
                $action = "stop_wrapper_scheduled_task"
                $details = @{
                    task_name=$cfg.wrapper_task_name
                    wrapper_count=$proc.wrapper_count; inner_count=$proc.inner_count
                    max_wrapper_processes=$maxWrapper; max_inner_processes=$maxInner
                }

                $st = Stop-ScheduledTaskSafeWithTimeout -TaskName $cfg.wrapper_task_name -TimeoutSec $taskTimeoutSec
                if ($st.timed_out -or -not $st.success) {
                    $details.error = $st.error; $details.timed_out = $st.timed_out
                    $payload.remediation.actions += @{ action=$action; outcome="FAIL"; details=$details }
                    Append-AuditJsonl -RunId $runId -Action $action -Outcome "FAIL" -Details $details
                } else {
                    $payload.remediation.actions += @{ action=$action; outcome="OK"; details=$details }
                    Append-AuditJsonl -RunId $runId -Action $action -Outcome "OK" -Details $details
                }
            }
        }
    }
}
catch {
    Add-Finding $findings "FAIL" "WATCHDOG_EXCEPTION" "Watchdog exception occurred" @{
        message = $_.Exception.Message
        type = $_.Exception.GetType().FullName
    }
}
finally {
    # Health level from findings
    $hasFail = $false; $hasWarn = $false
    foreach ($f in $findings) {
        if ($f.severity -eq "FAIL") { $hasFail = $true }
        elseif ($f.severity -eq "WARN") { $hasWarn = $true }
    }
    $payload.findings = @($findings)
    if ($hasFail) { $payload.health_level = "FAIL" }
    elseif ($hasWarn) { $payload.health_level = "WARN" }
    else { $payload.health_level = "OK" }

    # Best-effort full JSON write (never throw)
    $fullJson = ""
    try {
        $fullJson = $payload | ConvertTo-Json -Depth 18
    } catch {
        $fullJson = (@{
            stage="Stage7"; run_id=$runId; ts_utc=(Get-UtcIsoNow);
            health_level="FAIL"; error="FULL_JSON_SERIALIZE_FAILED"; message=$_.Exception.Message
        } | ConvertTo-Json -Compress -Depth 6)
    }

    try { [void](Write-JsonFileSafe -Path $script:DefaultHealthPath -Json $fullJson -TimeoutSec $taskTimeoutSec) } catch { }

    try {
        $dNorm = Normalize-FullPathLiteral -Path $script:DefaultHealthPath
        $cNorm = Normalize-FullPathLiteral -Path $healthOutputPathCfg
        if (-not [string]::IsNullOrWhiteSpace($cNorm) -and ($cNorm -ne $dNorm)) {
            [void](Write-JsonFileSafe -Path $cNorm -Json $fullJson -TimeoutSec $taskTimeoutSec)
        }
    } catch { }

    # Summary-only stdout (exactly one JSON)
    $staleLocks = 0
    try { $staleLocks = [int]$payload.locks.stale_count } catch { $staleLocks = 0 }

    $mainRed = 0; $cycleRed = 0; $procTotal = 0
    try { $mainRed = [int]$payload.logs.main.red_flag_hits } catch { }
    try { $cycleRed = [int]$payload.logs.cycle.red_flag_hits } catch { }
    try { if ($payload.processes.total_count -ne $null) { $procTotal = [int]$payload.processes.total_count } } catch { }

    $taskWrapperExists = $null
    try { $taskWrapperExists = $payload.tasks.wrapper.exists } catch { $taskWrapperExists = $null }

    $snapshotStatus = $null
    try { $snapshotStatus = $payload.snapshot.status } catch { $snapshotStatus = $null }

    $summary = @{
        stage="Stage7"
        run_id=$runId
        ts_utc=(Get-UtcIsoNow)
        host=$hostName
        health_level=$payload.health_level
        ops_state=$payload.ops_state
        stale_locks=$staleLocks
        main_red_flags=$mainRed
        cycle_red_flags=$cycleRed
        proc_total=$procTotal
        task_wrapper_exists=$taskWrapperExists
        snapshot_status=$snapshotStatus
    }

    $exitCode = 0
    if ($payload.health_level -eq "WARN") { $exitCode = 1 }
    elseif ($payload.health_level -eq "FAIL") { $exitCode = 2 }

    $summaryJson = $summary | ConvertTo-Json -Compress -Depth 8
    [Console]::Out.WriteLine($summaryJson)
    exit $exitCode
}
