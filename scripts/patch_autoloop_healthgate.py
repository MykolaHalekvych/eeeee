from __future__ import annotations

from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "scripts" / "auto_loop_5m.ps1"

CFG_INSERT = r"""
    # ---- OPS health gate ----
    HealthGate = $true
    IbHost = "localhost"
    IbPort = 7497
    IbClientId = 77
"""

SWITCH_INSERT = r"""
        # ---- OPS health gate argv ----
        "healthgate" { $cfg.HealthGate = _parse_bool $val $true }
        "ibhost" { if ($val -ne $null) { $cfg.IbHost = [string]$val } }
        "ibport" { $cfg.IbPort = _parse_int $val $cfg.IbPort }
        "ibclientid" { $cfg.IbClientId = _parse_int $val $cfg.IbClientId }
"""

FUNC_INSERT = r"""
# -------------------------
# OPS HEALTH gate runner (inline, no Start-Process hang)
# -------------------------
function _run_health_gate_inline([hashtable]$cfg, [string]$repoRoot, [string]$logdir, [int]$cycle) {
    if (-not $cfg.HealthGate) {
        return [pscustomobject]@{ exit_code = 0; ok = $true; skipped = $true; stdout = ""; log_stdout=$null; log_stderr=$null }
    }

    $healthScript = Join-Path $repoRoot "scripts\ops_health.ps1"
    if (-not (Test-Path -LiteralPath $healthScript)) {
        return [pscustomobject]@{ exit_code = 2; ok = $false; skipped = $false; stdout = "missing ops_health.ps1"; log_stdout=$null; log_stderr=$null }
    }

    _ensure_dir $logdir

    $start = _utc_now
    $ts = $start.ToString("yyyyMMdd_HHmmssfff'Z'")
    $base = Join-Path $logdir ("{0}_c{1:000000}_ops_health" -f $ts, $cycle)
    $stdoutPath = $base + ".stdout.log"
    $stderrPath = $base + ".stderr.log"

    $argsList = @(
        "-NoProfile",
        "-ExecutionPolicy","Bypass",
        "-File",$healthScript,
        "-IbHost",$cfg.IbHost,
        "-IbPort",$cfg.IbPort,
        "-IbClientId",$cfg.IbClientId
    )

    $exitCode = 2
    $stdoutText = ""

    try {
        $outObj = & powershell @argsList 2>&1
        $exitCode = $LASTEXITCODE
        $stdoutText = ($outObj | Out-String)

        try { Set-Content -LiteralPath $stdoutPath -Value $stdoutText -Encoding UTF8 } catch { }
        try { Set-Content -LiteralPath $stderrPath -Value "" -Encoding UTF8 } catch { }
    } catch {
        $stdoutText = ""
        try { Set-Content -LiteralPath $stderrPath -Value ("health gate failed: {0}" -f $_.Exception.Message) -Encoding UTF8 } catch { }
        $exitCode = 2
    }

    _log "INFO" ("HEALTH_GATE exit={0} stdout_log={1}" -f $exitCode, $stdoutPath)

    return [pscustomobject]@{
        exit_code = $exitCode
        ok = ($exitCode -eq 0)
        skipped = $false
        stdout = $stdoutText
        log_stdout = $stdoutPath
        log_stderr = $stderrPath
    }
}
"""

CALL_INSERT = r"""
        # OPS HEALTH gate (ideal): stop before running steps if health fails
        $hg = _run_health_gate_inline $cfg $repoRoot $cfg.LogDir $cycle
        if (-not $hg.skipped -and $hg.exit_code -ne 0) {
            $cycleEnd = _utc_now
            $durS = [Math]::Round(($cycleEnd - $cycleStart).TotalSeconds, 3)
            $evNow = _eval_lock $cfg.LockPath $cfg.LockTtlMin

            _json ([ordered]@{
                ts_utc = _utc_iso (_utc_now)
                reason = "OPS_HEALTH_GATE_FAIL"
                ok = $false
                exit_code = $hg.exit_code

                cycle = $cycle
                cycle_started_utc = _utc_iso $cycleStart
                cycle_ended_utc = _utc_iso $cycleEnd
                duration_s = $durS

                health_exit_code = $hg.exit_code
                health_log_stdout = $hg.log_stdout
                health_log_stderr = $hg.log_stderr
                health_stdout = $hg.stdout

                lock_path = $evNow.path
                lock_pid = $evNow.lock_pid
                lock_age_min = $evNow.lock_age_min
                lock_status = $evNow.status
                lock_reason = $evNow.reason

                stop_flag = (_test_stop $cfg.StopFlagPath)
            })

            $forcedExitCode = [int]$hg.exit_code
            $anyHardFail = $true
            break
        }
"""

def main() -> None:
    if not TARGET.exists():
        raise SystemExit(f"Missing: {TARGET}")

    text = TARGET.read_text(encoding="utf-8")

    # Backup
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
    bak = TARGET.with_suffix(TARGET.suffix + f".bak_{ts}")
    bak.write_text(text, encoding="utf-8")

    changed = False

    # 1) $cfg inject
    if "HealthGate" not in text:
        marker = '    PythonVersionArg = "-3.11"\n'
        if marker not in text:
            raise SystemExit("Cannot find cfg marker for insertion.")
        text = text.replace(marker, marker + CFG_INSERT + "\n")
        changed = True

    # 2) argv switch inject
    if '"healthgate"' not in text:
        marker = '        "pythonversionarg" { if ($val -ne $null) { $cfg.PythonVersionArg = [string]$val } }\n'
        if marker not in text:
            raise SystemExit("Cannot find switch marker for insertion.")
        text = text.replace(marker, marker + SWITCH_INSERT + "\n")
        changed = True

    # 3) function insert before "# Start" section
    if "_run_health_gate_inline" not in text:
        marker = "\n# -------------------------\n# Start\n# -------------------------\n"
        idx = text.find(marker)
        if idx < 0:
            raise SystemExit("Cannot find Start section marker.")
        text = text[:idx] + "\n" + FUNC_INSERT + "\n" + text[idx:]
        changed = True

    # 4) call insert after CYCLE_START log
    if 'reason = "OPS_HEALTH_GATE_FAIL"' not in text:
        needle = '_log "INFO" ("CYCLE_START cycle={0}" -f $cycle)\n'
        idx = text.find(needle)
        if idx < 0:
            raise SystemExit("Cannot find CYCLE_START log line.")
        insert_at = idx + len(needle)
        text = text[:insert_at] + CALL_INSERT + "\n" + text[insert_at:]
        changed = True

    if not changed:
        print("patch_autoloop_healthgate: nothing to do (already patched).")
    else:
        TARGET.write_text(text, encoding="utf-8")
        print(f"patch_autoloop_healthgate: patched OK. backup={bak}")

if __name__ == "__main__":
    main()
