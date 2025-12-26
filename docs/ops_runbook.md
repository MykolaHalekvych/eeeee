\# ARGS Ops Runbook



\## Scheduler: ARGS\_AutoLoop\_5m (Mode A)

\- Trigger: every 5 minutes

\- Action: powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\\Users\\mukol\\ARGS-Core-v1\\scripts\\auto\_loop\_5m.ps1" -Once

\- WorkingDirectory: C:\\Users\\mukol\\ARGS-Core-v1

\- Rationale: avoid overlapping task instances; each trigger runs exactly one cycle and exits

\- Notes:

&nbsp; - Script uses args\\logs\\auto\_loop.lock (with stale-lock recovery)

&nbsp; - Run artifacts are OUTPUT (jsonl/json/log/csv/meta) and not committed



