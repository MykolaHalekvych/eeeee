\# Stage 6 — OPS 24×7 Runbook (ARGS Core v1)



Repo: `C:\\Users\\mukol\\ARGS-Core-v1`  

Task Scheduler: `ARGS\_AutoLoop\_5m` (every 5 minutes)  

Wrapper: `scripts/ops\_loop\_5m\_stage6c.ps1`  

UI: `py -3.11 -m streamlit run .\\args\\ui\\app\_streamlit.py`



---



\## 1) What Stage 6 provides



Stage 6 turns the paper OPS loop into a predictable, operator-safe 24×7 stand:



\- Scheduled execution every 5 minutes via Windows Task Scheduler.

\- Single-flight wrapper (OS-level lock) to prevent overlaps when MultipleInstances=Parallel.

\- Snapshot refresh before each run:

&nbsp; - `args/data/ibkr\_open\_orders\_live.jsonl`

\- Control plane pause/resume via `stop.flag`:

&nbsp; - `args/data/stop.flag`

\- Operator UI (Streamlit) for:

&nbsp; - Pause/resume (stop.flag)

&nbsp; - Snapshot health (fresh/stale)

&nbsp; - Task Scheduler health

&nbsp; - Overlap processes view

&nbsp; - Tail logs (wrapper + latest cycle)



Safety rule:

\- UI manipulates only control files and shows status.

\- No BUY/SELL, no manual overrides, no safety bypass.



---



\## 2) Normal operation (happy path)



\### Expected steady state

\- Task runs every 5 minutes:

&nbsp; - `LastTaskResult = 0x00000000` after completion

&nbsp; - During execution you may see `0x00041301` (RUNNING) — this is normal.

\- Snapshot updates on each tick:

&nbsp; - `args/data/ibkr\_open\_orders\_live.jsonl`

&nbsp; - `age\_min <= 6.0` minutes (UI should show “Snapshot fresh”)

\- Logs are written:

&nbsp; - Wrapper: `args/logs/ops\_stage6c.log`

&nbsp; - Cycle logs: `args/logs/auto\_loop\_\*\_cycle\*.log`



---



\## 3) Operator controls (UI)



Open UI:

```powershell

cd C:\\Users\\mukol\\ARGS-Core-v1

py -3.11 -m streamlit run .\\args\\ui\\app\_streamlit.py



