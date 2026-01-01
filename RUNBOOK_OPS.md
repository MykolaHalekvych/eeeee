# ARGS-Core-v1 — Ops Runbook (with supervision)

Repo: C:\Users\mukol\ARGS-Core-v1  
Python: py -3.11  
Rule: training only OFFLINE between runs. Runtime is deterministic and safe-by-default.

## Scheduled Tasks (core)
- ARGS_AutoLoop_5m_with_rotate (every 5m): main loop (paper/dryrun) + rotate pointers + heartbeat
- ARGS_SoakGate_15m (every 15m): writes args/data/soak_status.json + appends args/logs/soak_history.jsonl
- ARGS_SoakReport_24h_1h (hourly): writes args/data/soak_report_24h.json
- ARGS_SoakReport_7d_daily (daily): writes args/data/soak_report_7d.json
- ARGS_Offline_Nightly (daily 02:30): dataset->train->eval->(promote if PASS)->verify

## One-command status (operator)
PowerShell:
  powershell -ExecutionPolicy Bypass -File .\scripts\status.ps1

Interpretation:
- soak_level PASS: gates green now
- report24/report7d FAIL early: low sample coverage is expected after history reset
- stop_flag=true: system intentionally halted
- latest_run_age_s should be < 600 for “alive”

## Logs (where to look)
- args/logs/scheduler_autoloop_with_rotate.log
- args/logs/scheduler_soakgate.log
- args/logs/scheduler_soakreport_24h.log
- args/logs/scheduler_soakreport_7d.log
- args/logs/scheduler_offline_nightly.log

## Stop / Resume
Stop:
  "manual-stop" | Out-File -Encoding ascii .\args\data\stop.flag

Resume:
  Remove-Item .\args\data\stop.flag -Force
  schtasks /Run /TN "ARGS_AutoLoop_5m_with_rotate"
  schtasks /Run /TN "ARGS_SoakGate_15m"

## Quick health checks
Latest run freshness:
  (Get-Item .\args\data\latest_run_id.txt).LastWriteTime
  Get-Content .\args\data\latest_run_id.txt

Pointers consistency:
  Get-Content .\args\data\latest_run_id.txt
  (Get-Content .\args\data\latest_paths.json -Raw | ConvertFrom-Json).run_id

Soak now:
  Get-Content .\args\logs\scheduler_soakgate.log -Tail 2

## Expected behavior (known)
- refresh_bundle can WARN (exit=2) and loop continues using last available bundle.
- Offline nightly STRICT mode:
  - exit 0 only if eval PASS (and promote may happen)
  - exit 1 if eval FAIL (quality gate), promote skipped
  - exit 2 for infrastructure errors (dataset/train/verify)

## Incident playbook (top cases)
1) LATEST_RUN_STALE / OPS_*_STALE:
   - Check autoloop task is running on schedule.
   - Run once manually:
       schtasks /Run /TN "ARGS_AutoLoop_5m_with_rotate"
     Then verify latest_run_id updated.

2) STOP_FLAG_PRESENT:
   - Remove stop.flag and rerun:
       Remove-Item .\args\data\stop.flag -Force
       schtasks /Run /TN "ARGS_AutoLoop_5m_with_rotate"
       schtasks /Run /TN "ARGS_SoakGate_15m"

3) Offline nightly infra error (exit 2):
   - Open args/logs/scheduler_offline_nightly.log
   - Fix dataset/train/eval invocation; rerun cmd manually.
