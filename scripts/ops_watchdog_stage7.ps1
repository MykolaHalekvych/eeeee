<#
.SYNOPSIS
  ARGS Stage 7 - Ops Watchdog: health JSON + optional self-healing (safe-by-default)

.DESCRIPTION
  Collects operational health for Stage 6 control plane and outputs a machine-readable JSON:
  - Writes JSON to stdout
  - Writes JSON to args/data/ops_health.json

  Optional remediation mode (-Remediate) performs ONLY operator-safe actions:
    * stop/start Scheduled Task (wrapper)
    * kill overlapping OPS processes (strict pattern + repo scoping)
    * remove stale lock files (only in args/data)
    * refresh snapshot via configured snapshot refresh script (safe allow-list)

HARD SAFETY:
  - No BUY/SELL, no order placement/cancel.
  - If stop.flag exists => remediation is skipped entirely.
  - Idempotent by design.
#>

[CmdletBinding()]
param(
    [switch]$Remediate = $false,
    [string]$ConfigPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# -------------------------
# Globals / bootstrap
# -------------------------
$Script:RunId = ([Guid]::NewGuid()).ToString("N")
$IbHost = $env:COMPUTERNAME

# Repo root is parent of /scripts
$Script:RepoRoot = Split-Path -Parent $PSScriptRoot

# Default dirs
$Script:DefaultDataDir = Join-Path $Script:RepoRoot "args\data"
$Script:DefaultLogsDir = Join-Path $Script:RepoRoot "args\logs"

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $Script:DefaultDataDir "ops_config.yaml"
}

# Default output paths (may be overridden by config)
$Script:WatchdogLogPath = Join-Path $Script:DefaultLogsDir "ops_watchdog.log"
$Script:RemediationJsonlPath = Join-Path $Script:DefaultDataDir "ops_remediation.jsonl"
$Script:HealthOutputPath = Join-Path $Script:DefaultDataDir "ops_health.json"

$Script:RemediationActions = New-Object System.Collections.Generic.List[object]

# -------------------------
# Helpers (safe under StrictMode)
# -------------------------
function New-UtcIsoTimestamp { (Get-Date).ToUniversalTime().ToString("o") }

function Ensure-Directory {
    param([AllowNull()][AllowEmptyString()][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Resolve-RepoPath {
    param([AllowNull()][AllowEmptyString()][string]$PathValue)

    if ([string]::IsNullOrWhiteSpace($PathValue)) { return "" }

    $p = ($PathValue -as [string]).Trim() -replace '/', '\'
    if ([System.IO.Path]::IsPathRooted($p)) { return $p }
    return (Join-Path $Script:RepoRoot $p)
}

function Normalize-FullPath {
    param([AllowNull()][AllowEmptyString()][string]$PathValue)
    if ([string]::IsNullOrWhiteSpace($PathValue)) { return "" }
    try { return [System.IO.Path]::GetFullPath($PathValue).TrimEnd('\') }
    catch { return ($PathValue.TrimEnd('\')) }
}

function Test-IsUnderDirectory {
    param(
        [AllowNull()][AllowEmptyString()][string]$CandidatePath,
        [AllowNull()][AllowEmptyString()][string]$DirectoryPath
    )
    if ([string]::IsNullOrWhiteSpace($CandidatePath)) { return $false }
    if ([string]::IsNullOrWhiteSpace($DirectoryPath)) { return $false }

    $cand = (Normalize-FullPath $CandidatePath).ToLowerInvariant()
    $dir  = (Normalize-FullPath $DirectoryPath).ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($cand) -or [string]::IsNullOrWhiteSpace($dir)) { return $false }
    return $cand.StartsWith($dir + "\")
}

function Write-Log {
    param(
        [Parameter(Mandatory=$true)][string]$Message,
        [string]$Level = "INFO"
    )
    $ts = New-UtcIsoTimestamp
    $line = "$ts [$Level] run_id=$($Script:RunId) $Message"
    try {
        $logDir = Split-Path -Parent $Script:WatchdogLogPath
        Ensure-Directory -Path $logDir
        Add-Content -LiteralPath $Script:WatchdogLogPath -Value $line -Encoding UTF8
    } catch {
        # Never fail watchdog because of logging
    }
}

function Parse-SimpleYamlKV {
    param([AllowNull()][AllowEmptyString()][string]$YamlPath)

    $cfg = @{}
    if ([string]::IsNullOrWhiteSpace($YamlPath)) { return $cfg }

    $p = Resolve-RepoPath -PathValue $YamlPath
    if ([string]::IsNullOrWhiteSpace($p)) { return $cfg }
    if (-not (Test-Path -LiteralPath $p)) { return $cfg }

    $lines = Get-Content -LiteralPath $p -ErrorAction Stop
    foreach ($raw in $lines) {
        $line = ($raw -as [string]).Trim()
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line.StartsWith("#")) { continue }

        # strip inline comment (simple)
        $hashIdx = $line.IndexOf("#")
        if ($hashIdx -ge 0) { $line = $line.Substring(0, $hashIdx).TrimEnd() }
        if ([string]::IsNullOrWhiteSpace($line)) { continue }

        $colonIdx = $line.IndexOf(":")
        if ($colonIdx -lt 1) { continue }

        $key = $line.Substring(0, $colonIdx).Trim()
        $val = $line.Substring($colonIdx + 1).Trim()
        if ([string]::IsNullOrWhiteSpace($key)) { continue }

        if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
            if ($val.Length -ge 2) { $val = $val.Substring(1, $val.Length - 2) }
        }

        $lower = $val.ToLowerInvariant()
        if ($lower -eq "true")  { $cfg[$key] = $true; continue }
        if ($lower -eq "false") { $cfg[$key] = $false; continue }

        $intVal = $null
        if ([int]::TryParse($val, [ref]$intVal)) { $cfg[$key] = $intVal; continue }

        $dblVal = $null
        if ([double]::TryParse($val, [ref]$dblVal)) { $cfg[$key] = $dblVal; continue }

        $cfg[$key] = $val
    }
    return $cfg
}

function Read-OpsConfig {
    param(
        [AllowNull()][AllowEmptyString()][string]$YamlPath,
        [Parameter(Mandatory=$true)][hashtable]$Defaults
    )
    $cfg = @{}
    foreach ($k in $Defaults.Keys) { $cfg[$k] = $Defaults[$k] }

    try {
        $fromFile = Parse-SimpleYamlKV -YamlPath $YamlPath
        foreach ($k in $fromFile.Keys) { $cfg[$k] = $fromFile[$k] }
    } catch {
        Write-Log -Level "WARN" -Message "Failed to parse config '$YamlPath'. Using defaults. Error: $($_.Exception.Message)"
    }

    return $cfg
}

function Split-ListString {
    param([AllowNull()][AllowEmptyString()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return @() }

    $parts = ($Value -as [string]).Trim() -split '[;,]'
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($p in $parts) {
        $t = ($p -as [string]).Trim()
        if (-not [string]::IsNullOrWhiteSpace($t)) { $out.Add($t) }
    }
    return $out.ToArray()
}

function Clamp-Int {
    param([AllowNull()]$Value, [int]$Default, [int]$Min = 0, [int]$Max = 2147483647)
    try {
        $v = [int]$Value
        if ($v -lt $Min) { return $Min }
        if ($v -gt $Max) { return $Max }
        return $v
    } catch { return $Default }
}

function Clamp-Double {
    param([AllowNull()]$Value, [double]$Default, [double]$Min = 0.0, [double]$Max = 1.0E15)
    try {
        $v = [double]$Value
        if ($v -lt $Min) { return $Min }
        if ($v -gt $Max) { return $Max }
        return $v
    } catch { return $Default }
}

# -------------------------
# Health collectors (NEVER throw; always return structured objects)
# -------------------------
function New-TaskStatusDefault {
    param([string]$TaskName)
    return [ordered]@{
        name = $TaskName
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
}

function Get-TaskStatusSafe {
    param(
        [AllowNull()][AllowEmptyString()][string]$TaskName,
        [int]$ExpectedIntervalMin = 5
    )

    $TaskName = ($TaskName -as [string])
    if ([string]::IsNullOrWhiteSpace($TaskName)) {
        $s = New-TaskStatusDefault -TaskName ""
        $s.info_error = "task_name_not_configured"
        return $s
    }

    $status = New-TaskStatusDefault -TaskName $TaskName
    $ExpectedIntervalMin = (Clamp-Int -Value $ExpectedIntervalMin -Default 5 -Min 1 -Max 1440)

    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop

        $status.exists = $true
        $status.state  = ($task.State -as [string])

        $enabled = $null
        try {
            if ($null -ne $task.Settings -and $null -ne $task.Settings.Enabled) {
                $enabled = [bool]$task.Settings.Enabled
            }
        } catch { $enabled = $null }
        if ($null -eq $enabled) { $enabled = ($status.state -ne "Disabled") }
        $status.enabled = $enabled

        $status.last_run_time = if ($info.LastRunTime -and $info.LastRunTime -gt [DateTime]"2000-01-01") { $info.LastRunTime.ToUniversalTime().ToString("o") } else { $null }
        $status.next_run_time = if ($info.NextRunTime -and $info.NextRunTime -gt [DateTime]"2000-01-01") { $info.NextRunTime.ToUniversalTime().ToString("o") } else { $null }

        $status.last_task_result = $info.LastTaskResult
        if ($null -ne $info.LastTaskResult) {
            try { $status.last_task_result_hex = ('0x{0:X8}' -f [int]$info.LastTaskResult) } catch { $status.last_task_result_hex = $null }
        }

        if ($info.LastRunTime -and $info.LastRunTime -gt [DateTime]"2000-01-01") {
            $ageMin = (New-TimeSpan -Start $info.LastRunTime -End (Get-Date)).TotalMinutes
            $status.age_since_last_run_min = [math]::Round($ageMin, 2)

            $missed = [math]::Max(0, [math]::Floor($ageMin / $ExpectedIntervalMin) - 1)
            $status.missed_runs_est = [int]$missed
        }
    } catch {
        $status.info_error = $_.Exception.Message
    }

    return $status
}

function New-SnapshotStatusDefault {
    param([string[]]$Checked)
    return [ordered]@{
        paths_checked = @($Checked)
        selected_path = $null
        exists = $false
        mtime_utc = $null
        age_min = $null
        bytes = $null
        warn_age_min = $null
        fail_age_min = $null
        min_bytes = $null
        status = "MISSING"
        info_error = $null
    }
}

function Get-SnapshotStatusSafe {
    param(
        [AllowNull()][AllowEmptyString()][string]$GlobString,
        [double]$WarnAgeMin,
        [double]$FailAgeMin,
        [int]$MinBytes
    )

    $WarnAgeMin = Clamp-Double -Value $WarnAgeMin -Default 7.0 -Min 0.0
    $FailAgeMin = Clamp-Double -Value $FailAgeMin -Default 20.0 -Min 0.0
    $MinBytes   = Clamp-Int    -Value $MinBytes   -Default 16  -Min 0

    $globs = Split-ListString -Value $GlobString
    $checked = New-Object System.Collections.Generic.List[string]
    $matches = New-Object System.Collections.Generic.List[object]

    foreach ($g in $globs) {
        $abs = Resolve-RepoPath -PathValue $g
        if ([string]::IsNullOrWhiteSpace($abs)) { continue }
        $checked.Add($abs) | Out-Null

        try {
            foreach ($it in @(Get-ChildItem -Path $abs -File -ErrorAction SilentlyContinue)) {
                if ($null -ne $it) { $matches.Add($it) | Out-Null }
            }
        } catch {
            # ignore per-glob failures
        }
    }

    $status = New-SnapshotStatusDefault -Checked $checked.ToArray()
    $status.warn_age_min = $WarnAgeMin
    $status.fail_age_min = $FailAgeMin
    $status.min_bytes = $MinBytes

    if ($matches.Count -eq 0) {
        if ($checked.Count -eq 0) { $status.info_error = "snapshot_globs_not_configured" }
        return $status
    }

    $selected = $null
    try {
        $selected = @($matches | Sort-Object -Property LastWriteTime -Descending | Select-Object -First 1)[0]
    } catch { $selected = $null }

    if ($null -eq $selected) {
        $status.info_error = "snapshot_select_failed"
        return $status
    }

    try {
        $status.selected_path = $selected.FullName
        $status.exists = $true
        $status.bytes = [int64]$selected.Length

        $mtimeUtc = $selected.LastWriteTime.ToUniversalTime()
        $status.mtime_utc = $mtimeUtc.ToString("o")

        $ageMin = (New-TimeSpan -Start $selected.LastWriteTime -End (Get-Date)).TotalMinutes
        $status.age_min = [math]::Round($ageMin, 2)

        if ($status.bytes -lt $MinBytes) {
            $status.status = "FAIL"
        } elseif ($ageMin -gt $FailAgeMin) {
            $status.status = "FAIL"
        } elseif ($ageMin -gt $WarnAgeMin) {
            $status.status = "WARN"
        } else {
            $status.status = "OK"
        }
    } catch {
        $status.status = "FAIL"
        $status.info_error = $_.Exception.Message
    }

    return $status
}

function Convert-WmiDateToDateTimeUtc {
    param([AllowNull()][AllowEmptyString()][string]$WmiDate)
    if ([string]::IsNullOrWhiteSpace($WmiDate)) { return $null }
    try {
        $dt = [System.Management.ManagementDateTimeConverter]::ToDateTime($WmiDate)
        return $dt.ToUniversalTime()
    } catch { return $null }
}

function Get-OpsProcessStatusSafe {
    param(
        [AllowNull()][AllowEmptyString()][string]$WrapperScriptName,
        [AllowNull()][AllowEmptyString()][string]$InnerScriptName
    )

    $wrapperLower = ($WrapperScriptName -as [string]).ToLowerInvariant()
    $innerLower   = ($InnerScriptName -as [string]).ToLowerInvariant()
    $repoLower    = (Normalize-FullPath $Script:RepoRoot).ToLowerInvariant()

    $items = New-Object System.Collections.Generic.List[object]

    $empty = [ordered]@{
        error = $null
        wrapper_count = 0
        inner_count = 0
        total_count = 0
        processes = @()
    }

    if ([string]::IsNullOrWhiteSpace($wrapperLower) -and [string]::IsNullOrWhiteSpace($innerLower)) {
        $empty.error = "process_patterns_not_configured"
        return $empty
    }

    try {
        foreach ($p in @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)) {
            $cmd = $p.CommandLine
            if ([string]::IsNullOrWhiteSpace($cmd)) { continue }

            $cmdLower = $cmd.ToLowerInvariant()
            $isWrapper = (-not [string]::IsNullOrWhiteSpace($wrapperLower)) -and $cmdLower.Contains($wrapperLower)
            $isInner   = (-not [string]::IsNullOrWhiteSpace($innerLower))   -and $cmdLower.Contains($innerLower)
            if (-not ($isWrapper -or $isInner)) { continue }

            $scoped = $false
            if (-not [string]::IsNullOrWhiteSpace($repoLower) -and $cmdLower.Contains($repoLower)) { $scoped = $true }
            if ($cmdLower.Contains("\scripts\") -or $cmdLower.Contains("/scripts/")) { $scoped = $true }

            $ptype = if ($isWrapper) { "WRAPPER" } elseif ($isInner) { "INNER" } else { "UNKNOWN" }

            $createdUtc = Convert-WmiDateToDateTimeUtc -WmiDate $p.CreationDate
            $createdIso = if ($null -ne $createdUtc) { $createdUtc.ToString("o") } else { $null }

            $items.Add([ordered]@{
                type = $ptype
                pid = [int]$p.ProcessId
                ppid = [int]$p.ParentProcessId
                name = $p.Name
                created_utc = $createdIso
                scoped_to_repo = $scoped
                command_line = $cmd
            }) | Out-Null
        }
    } catch {
        return [ordered]@{
            error = $_.Exception.Message
            wrapper_count = 0
            inner_count = 0
            total_count = 0
            processes = @()
        }
    }

    $wr = @($items | Where-Object { $_.type -eq "WRAPPER" })
    $in = @($items | Where-Object { $_.type -eq "INNER" })

    return [ordered]@{
        error = $null
        wrapper_count = $wr.Count
        inner_count = $in.Count
        total_count = $items.Count
        processes = @($items | Sort-Object -Property created_utc)
    }
}

function Extract-PidFromLockContent {
    param([AllowNull()][AllowEmptyString()][string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }

    # Try JSON
    try {
        $obj = $Text | ConvertFrom-Json -ErrorAction Stop
        foreach ($key in @("pid","PID")) {
            if ($null -ne $obj.$key) {
                $pidVal = $null
                if ([int]::TryParse(($obj.$key -as [string]), [ref]$pidVal)) { return $pidVal }
            }
        }
    } catch { }

    # Fallback: first integer token
    $m = [regex]::Match($Text, '\b(\d{1,9})\b')
    if ($m.Success) {
        $pidVal = $null
        if ([int]::TryParse($m.Groups[1].Value, [ref]$pidVal)) { return $pidVal }
    }
    return $null
}

function Get-LockStatusSafe {
    param(
        [AllowNull()][AllowEmptyString()][string]$LockPathsString,
        [double]$StaleAgeMin
    )

    $StaleAgeMin = Clamp-Double -Value $StaleAgeMin -Default 30.0 -Min 0.0
    $paths = Split-ListString -Value $LockPathsString

    $items = New-Object System.Collections.Generic.List[object]

    foreach ($p in $paths) {
        $abs = Resolve-RepoPath -PathValue $p
        if ([string]::IsNullOrWhiteSpace($abs)) { continue }

        $exists = Test-Path -LiteralPath $abs
        $item = [ordered]@{
            path = $abs
            exists = $exists
            bytes = $null
            mtime_utc = $null
            age_min = $null
            pid_in_lock = $null
            pid_running = $null
            stale = $null
            stale_reason = $null
            info_error = $null
        }

        if ($exists) {
            try {
                $fi = Get-Item -LiteralPath $abs -ErrorAction Stop
                $item.bytes = [int64]$fi.Length
                $item.mtime_utc = $fi.LastWriteTime.ToUniversalTime().ToString("o")
                $ageMin = (New-TimeSpan -Start $fi.LastWriteTime -End (Get-Date)).TotalMinutes
                $item.age_min = [math]::Round($ageMin, 2)

                $content = ""
                try { $content = Get-Content -LiteralPath $abs -Raw -ErrorAction Stop } catch { $content = "" }

                $pid = Extract-PidFromLockContent -Text $content
                $item.pid_in_lock = $pid

                if ($null -ne $pid) {
                    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
                    $item.pid_running = ($null -ne $proc)
                }

                $stale = $false
                $reason = @()

                if (($null -ne $item.pid_in_lock) -and ($item.pid_running -eq $false)) {
                    $stale = $true
                    $reason += "PID_NOT_RUNNING"
                }
                if (($null -eq $item.pid_in_lock) -and ($null -ne $item.age_min) -and ($item.age_min -gt $StaleAgeMin)) {
                    $stale = $true
                    $reason += "AGE_GT_THRESHOLD_NO_PID"
                }

                $item.stale = $stale
                $item.stale_reason = ($reason -join ";")
            } catch {
                $item.stale = $null
                $item.stale_reason = "LOCK_READ_ERROR"
                $item.info_error = $_.Exception.Message
            }
        }

        $items.Add($item) | Out-Null
    }

    $staleCount = @(@($items) | Where-Object { $_.exists -eq $true -and $_.stale -eq $true }).Count

    return [ordered]@{
        stale_age_threshold_min = $StaleAgeMin
        stale_count = $staleCount
        locks = @($items)
    }
}

function Get-FileTailSafe {
    param(
        [AllowNull()][AllowEmptyString()][string]$Path,
        [int]$TailLines
    )
    $TailLines = Clamp-Int -Value $TailLines -Default 120 -Min 0 -Max 5000
    if ([string]::IsNullOrWhiteSpace($Path)) { return @() }
    if ($TailLines -le 0) { return @() }
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    try {
        return @(Get-Content -LiteralPath $Path -Tail $TailLines -ErrorAction Stop)
    } catch {
        return @()
    }
}

function Find-RedFlagsInLines {
    param(
        [AllowNull()]$Lines = @(),
        [AllowNull()]$Patterns = @()
    )

    $linesArr = @()
    foreach ($x in @($Lines)) {
        if ($null -eq $x) { continue }
        $s = [string]$x
        if ($s.Length -eq 0) { continue }
        $linesArr += $s
    }

    $patsArr = @()
    foreach ($p in @($Patterns)) {
        if ($null -eq $p) { continue }
        $ps = ([string]$p).Trim()
        if ($ps.Length -eq 0) { continue }
        $patsArr += $ps
    }

    $hits = New-Object System.Collections.Generic.List[string]
    if ($linesArr.Count -eq 0 -or $patsArr.Count -eq 0) { return $hits }

    foreach ($ln in $linesArr) {
        foreach ($pat in $patsArr) {
            if ($ln -match $pat) { $hits.Add($ln) | Out-Null; break }
        }
    }
    return $hits
}

function New-LogStatusDefault {
    param([string[]]$Patterns, [string]$MainPath, [string]$CycleGlob)
    return [ordered]@{
        error = $null
        red_flag_patterns = @($Patterns)
        main_log = [ordered]@{
            path = $MainPath
            exists = $false
            tail = @()
            red_flags_count = 0
            red_flags_sample = @()
            info_error = $null
        }
        cycle_log = [ordered]@{
            glob = $CycleGlob
            selected_path = $null
            exists = $false
            tail = @()
            red_flags_count = 0
            red_flags_sample = @()
            info_error = $null
        }
    }
}

function Get-LogStatusSafe {
    param(
        [AllowNull()][AllowEmptyString()][string]$MainLogPath,
        [AllowNull()][AllowEmptyString()][string]$CycleLogGlob,
        [int]$TailLines,
        [AllowNull()][AllowEmptyString()][string]$RedFlagPatternsString
    )

    $TailLines = Clamp-Int -Value $TailLines -Default 120 -Min 0 -Max 5000

    $patterns = Split-ListString -Value $RedFlagPatternsString
    if ($patterns.Count -eq 0) { $patterns = @("ERROR","FATAL","CRITICAL","Exception","Traceback","Unhandled","Terminating") }

    $mainAbs = Resolve-RepoPath -PathValue $MainLogPath
    $cycleGlobAbs = Resolve-RepoPath -PathValue $CycleLogGlob

    $status = New-LogStatusDefault -Patterns $patterns -MainPath $mainAbs -CycleGlob $cycleGlobAbs

    # main log
    try {
        if (-not [string]::IsNullOrWhiteSpace($mainAbs) -and (Test-Path -LiteralPath $mainAbs)) {
            $status.main_log.exists = $true
            $mainTail = Get-FileTailSafe -Path $mainAbs -TailLines $TailLines
            $hits = Find-RedFlagsInLines -Lines $mainTail -Patterns $patterns
            $status.main_log.tail = @($mainTail)
            $status.main_log.red_flags_count = $hits.Count
            $status.main_log.red_flags_sample = @($hits | Select-Object -First 10)
        } else {
            $status.main_log.info_error = "main_log_missing"
        }
    } catch {
        $status.main_log.info_error = $_.Exception.Message
    }

    # cycle log (latest)
    try {
        if (-not [string]::IsNullOrWhiteSpace($cycleGlobAbs)) {
            $cycleItems = @(Get-ChildItem -Path $cycleGlobAbs -File -ErrorAction SilentlyContinue)  # IMPORTANT: wrap for .Count
            if ($cycleItems.Count -gt 0) {
                $latest = @($cycleItems | Sort-Object -Property LastWriteTime -Descending | Select-Object -First 1)[0]
                if ($null -ne $latest) {
                    $cycleAbs = $latest.FullName
                    $status.cycle_log.selected_path = $cycleAbs
                    if (Test-Path -LiteralPath $cycleAbs) {
                        $status.cycle_log.exists = $true
                        $cycleTail = Get-FileTailSafe -Path $cycleAbs -TailLines $TailLines
                        $hits2 = Find-RedFlagsInLines -Lines $cycleTail -Patterns $patterns
                        $status.cycle_log.tail = @($cycleTail)
                        $status.cycle_log.red_flags_count = $hits2.Count
                        $status.cycle_log.red_flags_sample = @($hits2 | Select-Object -First 10)
                    } else {
                        $status.cycle_log.info_error = "cycle_log_selected_missing"
                    }
                }
            } else {
                $status.cycle_log.info_error = "no_cycle_logs_found"
            }
        } else {
            $status.cycle_log.info_error = "cycle_log_glob_not_configured"
        }
    } catch {
        $status.cycle_log.info_error = $_.Exception.Message
    }

    return $status
}

# -------------------------
# Remediation logging
# -------------------------
function Record-RemediationAction {
    param(
        [Parameter(Mandatory=$true)][string]$Action,
        [hashtable]$Details = @{},
        [string]$Outcome = "OK",
        [AllowNull()][AllowEmptyString()][string]$ErrorMessage = $null
    )

    $entry = [ordered]@{
        ts_utc = New-UtcIsoTimestamp
        run_id = $Script:RunId
        action = $Action
        outcome = $Outcome
        error = $ErrorMessage
        details = $Details
    }

    $Script:RemediationActions.Add($entry) | Out-Null

    try {
        $jsonLine = ($entry | ConvertTo-Json -Depth 8 -Compress)
        Ensure-Directory -Path (Split-Path -Parent $Script:RemediationJsonlPath)
        Add-Content -LiteralPath $Script:RemediationJsonlPath -Value $jsonLine -Encoding UTF8
    } catch { }

    $msg = "remediation action='$Action' outcome='$Outcome'"
    if (-not [string]::IsNullOrWhiteSpace($ErrorMessage)) { $msg += " error='$ErrorMessage'" }
    Write-Log -Level "INFO" -Message $msg
}

function Invoke-SnapshotRefreshIfConfigured {
    param([Parameter(Mandatory=$true)][hashtable]$Cfg)

    $refreshPathVal = ($Cfg.snapshot_refresh_script -as [string])
    if ([string]::IsNullOrWhiteSpace($refreshPathVal)) {
        Record-RemediationAction -Action "SNAPSHOT_REFRESH" -Outcome "SKIP" -Details @{ reason = "snapshot_refresh_script_not_configured" }
        return $false
    }

    $refreshAbs = Resolve-RepoPath -PathValue $refreshPathVal
    if ([string]::IsNullOrWhiteSpace($refreshAbs) -or (-not (Test-Path -LiteralPath $refreshAbs))) {
        Record-RemediationAction -Action "SNAPSHOT_REFRESH" -Outcome "SKIP" -Details @{ reason = "snapshot_refresh_script_missing"; path = $refreshAbs }
        return $false
    }

    # Allow-list: script name contains "snapshot" or "open_orders" and is under repo root
    $fname = ([System.IO.Path]::GetFileName($refreshAbs)).ToLowerInvariant()
    $allowedName = ($fname.Contains("snapshot") -or $fname.Contains("open_orders"))
    if (-not $allowedName) {
        Record-RemediationAction -Action "SNAPSHOT_REFRESH" -Outcome "SKIP" -Details @{ reason = "snapshot_refresh_script_not_allowlisted"; path = $refreshAbs }
        return $false
    }

    $repoRootAbs = Normalize-FullPath $Script:RepoRoot
    $refreshFull = Normalize-FullPath $refreshAbs
    if (-not ($refreshFull.ToLowerInvariant().StartsWith($repoRootAbs.ToLowerInvariant() + "\"))) {
        Record-RemediationAction -Action "SNAPSHOT_REFRESH" -Outcome "SKIP" -Details @{ reason = "snapshot_refresh_script_outside_repo"; path = $refreshAbs }
        return $false
    }

    $timeoutSec = Clamp-Int -Value $Cfg.snapshot_refresh_timeout_sec -Default 45 -Min 5 -Max 600

    $psExe = "powershell.exe"
    $args = "-NoProfile -ExecutionPolicy Bypass -File `"$refreshAbs`""

    $details = @{
        exe = $psExe
        arguments = $args
        cwd = $Script:RepoRoot
        timeout_sec = $timeoutSec
        script = $refreshAbs
    }

    try {
        $pinfo = New-Object System.Diagnostics.ProcessStartInfo
        $pinfo.FileName = $psExe
        $pinfo.Arguments = $args
        $pinfo.WorkingDirectory = $Script:RepoRoot
        $pinfo.UseShellExecute = $false
        $pinfo.CreateNoWindow = $true
        $pinfo.RedirectStandardOutput = $true
        $pinfo.RedirectStandardError = $true

        $proc = New-Object System.Diagnostics.Process
        $proc.StartInfo = $pinfo

        $null = $proc.Start()
        $exited = $proc.WaitForExit($timeoutSec * 1000)

        if (-not $exited) {
            try { $proc.Kill() } catch { }
            Record-RemediationAction -Action "SNAPSHOT_REFRESH" -Outcome "FAIL" -ErrorMessage "timeout" -Details $details
            return $false
        }

        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $code = $proc.ExitCode

        $details.exit_code = $code
        $details.stdout_tail = @($stdout -split "`r?`n" | Select-Object -Last 20)
        $details.stderr_tail = @($stderr -split "`r?`n" | Select-Object -Last 20)

        if ($code -eq 0) {
            Record-RemediationAction -Action "SNAPSHOT_REFRESH" -Outcome "OK" -Details $details
            return $true
        }

        Record-RemediationAction -Action "SNAPSHOT_REFRESH" -Outcome "FAIL" -ErrorMessage "exit_code=$code" -Details $details
        return $false
    } catch {
        Record-RemediationAction -Action "SNAPSHOT_REFRESH" -Outcome "FAIL" -ErrorMessage $_.Exception.Message -Details $details
        return $false
    }
}

function Stop-ScheduledTaskSafe {
    param([AllowNull()][AllowEmptyString()][string]$TaskName)
    if ([string]::IsNullOrWhiteSpace($TaskName)) {
        Record-RemediationAction -Action "STOP_TASK" -Outcome "SKIP" -Details @{ reason = "task_name_not_configured" }
        return $false
    }
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Record-RemediationAction -Action "STOP_TASK" -Outcome "OK" -Details @{ task = $TaskName }
        return $true
    } catch {
        Record-RemediationAction -Action "STOP_TASK" -Outcome "FAIL" -ErrorMessage $_.Exception.Message -Details @{ task = $TaskName }
        return $false
    }
}

function Start-ScheduledTaskSafe {
    param([AllowNull()][AllowEmptyString()][string]$TaskName)
    if ([string]::IsNullOrWhiteSpace($TaskName)) {
        Record-RemediationAction -Action "START_TASK" -Outcome "SKIP" -Details @{ reason = "task_name_not_configured" }
        return $false
    }
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Record-RemediationAction -Action "START_TASK" -Outcome "OK" -Details @{ task = $TaskName }
        return $true
    } catch {
        Record-RemediationAction -Action "START_TASK" -Outcome "FAIL" -ErrorMessage $_.Exception.Message -Details @{ task = $TaskName }
        return $false
    }
}

function Stop-OpsProcessesOverLimit {
    param(
        [hashtable]$Cfg,
        [object[]]$Processes
    )

    $maxWrapper = Clamp-Int -Value $Cfg.max_wrapper_processes -Default 1 -Min 0 -Max 10
    $maxInner   = Clamp-Int -Value $Cfg.max_inner_processes   -Default 1 -Min 0 -Max 10

    $wr = @($Processes | Where-Object { $_.type -eq "WRAPPER" })
    $in = @($Processes | Where-Object { $_.type -eq "INNER" })

    $toKill = New-Object System.Collections.Generic.List[object]

    if ($wr.Count -gt $maxWrapper) {
        $keep = @($wr | Sort-Object -Property created_utc -Descending | Select-Object -First $maxWrapper)
        foreach ($p in $wr) { if (-not ($keep.pid -contains $p.pid)) { $toKill.Add($p) | Out-Null } }
    }
    if ($in.Count -gt $maxInner) {
        $keep = @($in | Sort-Object -Property created_utc -Descending | Select-Object -First $maxInner)
        foreach ($p in $in) { if (-not ($keep.pid -contains $p.pid)) { $toKill.Add($p) | Out-Null } }
    }

    $killed = 0
    foreach ($p in $toKill) {
        # Hard safety: kill only scoped_to_repo processes
        if ($p.scoped_to_repo -ne $true) {
            Record-RemediationAction -Action "KILL_PROCESS" -Outcome "SKIP" -Details @{ pid=$p.pid; type=$p.type; reason="not_scoped_to_repo"; command_line=$p.command_line }
            continue
        }
        try {
            Stop-Process -Id $p.pid -Force -ErrorAction Stop
            $killed++
            Record-RemediationAction -Action "KILL_PROCESS" -Outcome "OK" -Details @{ pid=$p.pid; type=$p.type; command_line=$p.command_line }
        } catch {
            Record-RemediationAction -Action "KILL_PROCESS" -Outcome "FAIL" -ErrorMessage $_.Exception.Message -Details @{ pid=$p.pid; type=$p.type; command_line=$p.command_line }
        }
    }

    return $killed
}

function Remove-StaleLocks {
    param([hashtable]$LockStatus)

    $dataDir = Join-Path $Script:RepoRoot "args\data"
    $removed = 0

    foreach ($lk in @($LockStatus.locks)) {
        if ($null -eq $lk) { continue }
        if ($lk.exists -ne $true) { continue }
        if ($lk.stale -ne $true) { continue }

        $path = $lk.path
        if (-not (Test-IsUnderDirectory -CandidatePath $path -DirectoryPath $dataDir)) {
            Record-RemediationAction -Action "REMOVE_LOCK" -Outcome "SKIP" -Details @{ path=$path; reason="outside_args_data" }
            continue
        }

        try {
            Remove-Item -LiteralPath $path -Force -ErrorAction Stop
            $removed++
            Record-RemediationAction -Action "REMOVE_LOCK" -Outcome "OK" -Details @{ path=$path; stale_reason=$lk.stale_reason; pid=$lk.pid_in_lock }
        } catch {
            Record-RemediationAction -Action "REMOVE_LOCK" -Outcome "FAIL" -ErrorMessage $_.Exception.Message -Details @{ path=$path }
        }
    }

    return $removed
}

# -------------------------
# Snapshot of whole system
# -------------------------
function Get-HealthSnapshot {
    param([Parameter(Mandatory=$true)][hashtable]$Cfg)

    $stopFlagAbs = Resolve-RepoPath -PathValue ($Cfg.stop_flag_path -as [string])
    $stopPresent = $false
    try { if (-not [string]::IsNullOrWhiteSpace($stopFlagAbs)) { $stopPresent = Test-Path -LiteralPath $stopFlagAbs } } catch { $stopPresent = $false }
    $opsState = if ($stopPresent) { "PAUSED" } else { "RUNNING" }

    $wrapperInterval = Clamp-Int -Value $Cfg.wrapper_interval_min -Default 5 -Min 1 -Max 1440

    $wrapperTask = Get-TaskStatusSafe -TaskName ($Cfg.wrapper_task_name -as [string]) -ExpectedIntervalMin $wrapperInterval
    $watchdogTask = Get-TaskStatusSafe -TaskName ($Cfg.watchdog_task_name -as [string]) -ExpectedIntervalMin 1

    $warnAge = Clamp-Double -Value $Cfg.snapshot_warn_age_min -Default 7.0 -Min 0.0
    $failAge = Clamp-Double -Value $Cfg.snapshot_fail_age_min -Default 20.0 -Min 0.0
    $minBytes = Clamp-Int -Value $Cfg.snapshot_min_bytes -Default 16 -Min 0

    $snapshot = Get-SnapshotStatusSafe -GlobString ($Cfg.snapshot_path_globs -as [string]) -WarnAgeMin $warnAge -FailAgeMin $failAge -MinBytes $minBytes

    $processes = Get-OpsProcessStatusSafe -WrapperScriptName ($Cfg.wrapper_script_name -as [string]) -InnerScriptName ($Cfg.inner_script_name -as [string])

    $lockStaleMin = Clamp-Double -Value $Cfg.lock_stale_min -Default 30.0 -Min 0.0
    $locks = Get-LockStatusSafe -LockPathsString ($Cfg.lock_paths -as [string]) -StaleAgeMin $lockStaleMin

    $tailLines = Clamp-Int -Value $Cfg.log_tail_lines -Default 120 -Min 0 -Max 5000
    $logs = Get-LogStatusSafe -MainLogPath ($Cfg.main_log_path -as [string]) -CycleLogGlob ($Cfg.cycle_log_glob -as [string]) -TailLines $tailLines -RedFlagPatternsString ($Cfg.log_red_flag_patterns -as [string])

    return [ordered]@{
        ops_state = $opsState
        stop_flag = [ordered]@{ path = $stopFlagAbs; present = $stopPresent }
        tasks = [ordered]@{ wrapper = $wrapperTask; watchdog = $watchdogTask }
        snapshot = $snapshot
        processes = $processes
        locks = $locks
        logs = $logs
    }
}

function Evaluate-Findings {
    param(
        [Parameter(Mandatory=$true)][hashtable]$Cfg,
        [Parameter(Mandatory=$true)][hashtable]$Snap
    )

    $findings = New-Object System.Collections.Generic.List[object]
    function Add-FindingLocal {
        param([string]$Severity, [string]$Code, [string]$Message, [hashtable]$Details)
        $findings.Add([ordered]@{ severity=$Severity; code=$Code; message=$Message; details=$Details }) | Out-Null
    }

    $isPaused = (($Snap.ops_state -as [string]) -eq "PAUSED")

    # Wrapper task
    $wrapper = $Snap.tasks.wrapper
    $wrapperInterval = Clamp-Int -Value $Cfg.wrapper_interval_min -Default 5 -Min 1 -Max 1440

    if ($wrapper.exists -ne $true) {
        Add-FindingLocal -Severity (if ($isPaused) { "WARN" } else { "FAIL" }) -Code "TASK_WRAPPER_MISSING" -Message "Wrapper scheduled task is missing." -Details @{ task=$wrapper.name; error=$wrapper.info_error }
    } else {
        if (($wrapper.enabled -eq $false) -and (-not $isPaused)) {
            Add-FindingLocal -Severity "WARN" -Code "TASK_WRAPPER_DISABLED" -Message "Wrapper scheduled task is disabled while stop.flag is absent." -Details @{ task=$wrapper.name; state=$wrapper.state }
        }
        if (($wrapper.last_task_result -ne $null) -and ([int]$wrapper.last_task_result -ne 0)) {
            Add-FindingLocal -Severity "WARN" -Code "TASK_WRAPPER_LAST_RESULT_NONZERO" -Message "Wrapper task last result is non-zero." -Details @{ task=$wrapper.name; last_task_result=$wrapper.last_task_result; last_task_result_hex=$wrapper.last_task_result_hex }
        }
        if (($wrapper.age_since_last_run_min -ne $null) -and (-not $isPaused)) {
            $age = [double]$wrapper.age_since_last_run_min
            if ($age -gt ($wrapperInterval * 6)) {
                Add-FindingLocal -Severity "FAIL" -Code "TASK_WRAPPER_STALE" -Message "Wrapper task last run is too old (severe)." -Details @{ age_min=$age; interval_min=$wrapperInterval; missed_runs_est=$wrapper.missed_runs_est }
            } elseif ($age -gt ($wrapperInterval * 3)) {
                Add-FindingLocal -Severity "WARN" -Code "TASK_WRAPPER_STALE" -Message "Wrapper task last run is older than expected." -Details @{ age_min=$age; interval_min=$wrapperInterval; missed_runs_est=$wrapper.missed_runs_est }
            }
        }
    }

    # Snapshot
    $snap = $Snap.snapshot
    $snapStatus = ($snap.status -as [string])
    if ($snapStatus -eq "MISSING") {
        Add-FindingLocal -Severity (if ($isPaused) { "WARN" } else { "FAIL" }) -Code "SNAPSHOT_MISSING" -Message "Snapshot file not found." -Details @{ checked=$snap.paths_checked; error=$snap.info_error }
    } elseif ($snapStatus -eq "FAIL") {
        Add-FindingLocal -Severity (if ($isPaused) { "WARN" } else { "FAIL" }) -Code "SNAPSHOT_STALE" -Message "Snapshot is stale or invalid." -Details @{ path=$snap.selected_path; age_min=$snap.age_min; bytes=$snap.bytes; fail_age_min=$snap.fail_age_min; min_bytes=$snap.min_bytes; error=$snap.info_error }
    } elseif ($snapStatus -eq "WARN") {
        Add-FindingLocal -Severity "WARN" -Code "SNAPSHOT_WARN" -Message "Snapshot is older than warn threshold." -Details @{ path=$snap.selected_path; age_min=$snap.age_min; warn_age_min=$snap.warn_age_min }
    }

    # Processes overlap
    $procs = $Snap.processes
    if ($null -ne $procs.error) {
        Add-FindingLocal -Severity "WARN" -Code "PROC_ENUM_ERROR" -Message "Failed to enumerate OPS processes." -Details @{ error=$procs.error }
    } else {
        $maxWrapper = Clamp-Int -Value $Cfg.max_wrapper_processes -Default 1 -Min 0 -Max 10
        $maxInner   = Clamp-Int -Value $Cfg.max_inner_processes   -Default 1 -Min 0 -Max 10
        if (($procs.wrapper_count -gt $maxWrapper) -or ($procs.inner_count -gt $maxInner)) {
            Add-FindingLocal -Severity (if ($isPaused) { "WARN" } else { "FAIL" }) -Code "OPS_OVERLAP" -Message "Detected overlapping OPS processes beyond allowed limits." -Details @{
                wrapper_count=$procs.wrapper_count; max_wrapper=$maxWrapper;
                inner_count=$procs.inner_count; max_inner=$maxInner;
                total=$procs.total_count
            }
        }
    }

    # Locks
    $locks = $Snap.locks
    if ([int]$locks.stale_count -gt 0) {
        Add-FindingLocal -Severity "WARN" -Code "STALE_LOCKS" -Message "Detected stale lock files." -Details @{ stale_count=$locks.stale_count; threshold_min=$locks.stale_age_threshold_min }
    }

    # Logs red flags
    $logs = $Snap.logs
    try {
        $mainHits = [int]$logs.main_log.red_flags_count
        $cycleHits = [int]$logs.cycle_log.red_flags_count
        if (($mainHits -gt 0) -or ($cycleHits -gt 0)) {
            Add-FindingLocal -Severity "WARN" -Code "LOG_REDFLAGS" -Message "Red-flag patterns found in log tails." -Details @{
                main_log_path=$logs.main_log.path; main_red_flags=$mainHits;
                cycle_log_path=$logs.cycle_log.selected_path; cycle_red_flags=$cycleHits
            }
        }
    } catch {
        Add-FindingLocal -Severity "WARN" -Code "LOG_PARSE_ERROR" -Message "Error while evaluating log status." -Details @{ error=$_.Exception.Message }
    }

    return @($findings)
}

function HealthLevel-FromFindings {
    param([object[]]$Findings)

    $hasFail = @(@($Findings) | Where-Object { $_.severity -eq "FAIL" }).Count -gt 0
    if ($hasFail) { return "FAIL" }

    $hasWarn = @(@($Findings) | Where-Object { $_.severity -eq "WARN" }).Count -gt 0
    if ($hasWarn) { return "WARN" }

    return "OK"
}

# -------------------------
# Defaults
# -------------------------
$DefaultConfig = @{
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

    watchdog_log_path = "args/logs/ops_watchdog.log"
    remediation_jsonl_path = "args/data/ops_remediation.jsonl"
    health_output_path = "args/data/ops_health.json"

    snapshot_refresh_script = ""
    snapshot_refresh_timeout_sec = 45
}

# -------------------------
# Main
# -------------------------
$exitCode = 2
$jsonOut = $null

try {
    $cfg = Read-OpsConfig -YamlPath $ConfigPath -Defaults $DefaultConfig

    # Apply config-driven output/log paths
    $logPathCfg = ($cfg.watchdog_log_path -as [string])
    if (-not [string]::IsNullOrWhiteSpace($logPathCfg)) { $Script:WatchdogLogPath = Resolve-RepoPath -PathValue $logPathCfg }

    $remPathCfg = ($cfg.remediation_jsonl_path -as [string])
    if (-not [string]::IsNullOrWhiteSpace($remPathCfg)) { $Script:RemediationJsonlPath = Resolve-RepoPath -PathValue $remPathCfg }

    $healthPathCfg = ($cfg.health_output_path -as [string])
    if (-not [string]::IsNullOrWhiteSpace($healthPathCfg)) { $Script:HealthOutputPath = Resolve-RepoPath -PathValue $healthPathCfg }

    Ensure-Directory -Path (Split-Path -Parent $Script:WatchdogLogPath)
    Ensure-Directory -Path (Split-Path -Parent $Script:RemediationJsonlPath)
    Ensure-Directory -Path (Split-Path -Parent $Script:HealthOutputPath)

    Write-Log -Level "INFO" -Message "Starting Stage7 watchdog. Remediate=$($Remediate.IsPresent). ConfigPath=$ConfigPath"

    $baseline = Get-HealthSnapshot -Cfg $cfg
    $remediationSkippedStopFlag = $false

    # Controlled remediation
    if ($Remediate.IsPresent) {
        if ($baseline.stop_flag.present -eq $true) {
            $remediationSkippedStopFlag = $true
            Write-Log -Level "WARN" -Message "Remediation requested but stop.flag is present -> remediation skipped."
        } else {
            $maxWrapper = Clamp-Int -Value $cfg.max_wrapper_processes -Default 1 -Min 0 -Max 10
            $maxInner   = Clamp-Int -Value $cfg.max_inner_processes   -Default 1 -Min 0 -Max 10

            $proc = $baseline.processes
            $overlap = $false
            if ($null -eq $proc.error) {
                if (($proc.wrapper_count -gt $maxWrapper) -or ($proc.inner_count -gt $maxInner)) { $overlap = $true }
            }

            if ($overlap) {
                $wrapperTaskName = ($cfg.wrapper_task_name -as [string])
                $null = Stop-ScheduledTaskSafe -TaskName $wrapperTaskName

                $killed = Stop-OpsProcessesOverLimit -Cfg $cfg -Processes @($proc.processes)
                Record-RemediationAction -Action "OVERLAP_REMEDIATION" -Outcome "OK" -Details @{
                    wrapper_task = $wrapperTaskName
                    killed_processes = $killed
                    max_wrapper = $maxWrapper
                    max_inner = $maxInner
                }

                $removedLocks = Remove-StaleLocks -LockStatus $baseline.locks
                if ($removedLocks -gt 0) {
                    Record-RemediationAction -Action "LOCK_CLEANUP" -Outcome "OK" -Details @{ removed = $removedLocks }
                }

                # Restart wrapper task only if stop.flag still absent
                if (-not (Test-Path -LiteralPath $baseline.stop_flag.path)) {
                    $null = Start-ScheduledTaskSafe -TaskName $wrapperTaskName
                } else {
                    Record-RemediationAction -Action "START_TASK" -Outcome "SKIP" -Details @{ task = $wrapperTaskName; reason = "stop_flag_present" }
                }
            }

            # Snapshot stale -> refresh (optional)
            if (($baseline.snapshot.status -as [string]) -eq "FAIL") {
                $null = Invoke-SnapshotRefreshIfConfigured -Cfg $cfg
            }

            # Stale locks -> remove
            if ([int]$baseline.locks.stale_count -gt 0) {
                $null = Remove-StaleLocks -LockStatus $baseline.locks
            }
        }
    }

    $final = Get-HealthSnapshot -Cfg $cfg
    $findings = Evaluate-Findings -Cfg $cfg -Snap $final
    $healthLevel = HealthLevel-FromFindings -Findings $findings

    $result = [ordered]@{
        stage = "Stage7"
        run_id = $Script:RunId
        ts_utc = New-UtcIsoTimestamp
        host = $IbHost
        repo_root = (Normalize-FullPath $Script:RepoRoot)

        config_path = (Resolve-RepoPath -PathValue $ConfigPath)
        remediate_requested = $Remediate.IsPresent
        remediation_skipped_due_to_stop_flag = $remediationSkippedStopFlag

        health_level = $healthLevel
        ops_state = $final.ops_state

        stop_flag = $final.stop_flag
        tasks = $final.tasks
        snapshot = $final.snapshot
        processes = $final.processes
        locks = $final.locks
        logs = $final.logs

        findings = $findings
        remediation_actions = @($Script:RemediationActions)
    }

    $jsonOut = ($result | ConvertTo-Json -Depth 8)

    try { Set-Content -LiteralPath $Script:HealthOutputPath -Value $jsonOut -Encoding UTF8 }
    catch { Write-Log -Level "WARN" -Message "Failed writing ops_health.json to '$($Script:HealthOutputPath)': $($_.Exception.Message)" }

    if ($healthLevel -eq "OK") { $exitCode = 0 }
    elseif ($healthLevel -eq "WARN") { $exitCode = 1 }
    else { $exitCode = 2 }

    Write-Log -Level "INFO" -Message "Watchdog complete. health_level=$healthLevel exit_code=$exitCode"
}
catch {
    $err = ($_ | Out-String)
    Write-Log -Level "ERROR" -Message "Unhandled watchdog error: $err"

    $fallback = [ordered]@{
        stage = "Stage7"
        run_id = $Script:RunId
        ts_utc = New-UtcIsoTimestamp
        host = $IbHost
        repo_root = (Normalize-FullPath $Script:RepoRoot)
        config_path = (Resolve-RepoPath -PathValue $ConfigPath)
        remediate_requested = $Remediate.IsPresent
        health_level = "FAIL"
        ops_state = "UNKNOWN"
        error = $err
        remediation_actions = @($Script:RemediationActions)
    }

    $jsonOut = ($fallback | ConvertTo-Json -Depth 8)
    try {
        Ensure-Directory -Path (Split-Path -Parent $Script:HealthOutputPath)
        Set-Content -LiteralPath $Script:HealthOutputPath -Value $jsonOut -Encoding UTF8
    } catch { }

    $exitCode = 2
}

Write-Output $jsonOut
exit $exitCode
