from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import streamlit as st


# -----------------------------
# Constants
# -----------------------------
TASK_NAME = "ARGS_AutoLoop_5m"

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

STOP_FLAG = DATA_DIR / "stop.flag"
SNAPSHOT = DATA_DIR / "ibkr_open_orders_live.jsonl"
OPS_LOG = LOGS_DIR / "ops_stage6c.log"


# -----------------------------
# Helpers
# -----------------------------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_local(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _read_tail(path: Path, n: int = 200) -> str:
    if not path.exists():
        return f"[missing] {path}"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception as e:
        return f"[error reading {path}] {e}"


def _latest_cycle_log() -> Optional[Path]:
    if not LOGS_DIR.exists():
        return None
    files = sorted(
        LOGS_DIR.glob("auto_loop_*_cycle*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _file_stat(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    stt = path.stat()
    age_min = (_now_utc().timestamp() - stt.st_mtime) / 60.0
    return {
        "exists": True,
        "path": str(path),
        "mtime_local": _fmt_local(stt.st_mtime),
        "age_min": round(age_min, 1),
        "bytes": stt.st_size,
    }


def _set_stopflag(on: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if on:
        STOP_FLAG.write_text(
            f"created_utc={_now_utc().strftime('%Y-%m-%dT%H:%M:%SZ')}\n",
            encoding="utf-8",
        )
    else:
        try:
            STOP_FLAG.unlink()
        except FileNotFoundError:
            pass


def _run_powershell(ps_cmd: str, timeout_s: int = 10) -> Dict[str, Any]:
    try:
        p = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return {
            "ok": p.returncode == 0,
            "rc": p.returncode,
            "stdout": p.stdout.strip(),
            "stderr": p.stderr.strip(),
        }
    except Exception as e:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": str(e)}


def _ps_json(ps_cmd: str, timeout_s: int = 10) -> Dict[str, Any]:
    res = _run_powershell(ps_cmd, timeout_s=timeout_s)
    if not res["ok"]:
        return {"ok": False, "error": res["stderr"], "raw": res}
    try:
        return {"ok": True, "data": json.loads(res["stdout"]), "raw": res}
    except Exception as e:
        return {"ok": False, "error": f"json_parse_error: {e}", "raw": res}


def _task_info() -> Dict[str, Any]:
    ps = (
        f'Get-ScheduledTaskInfo -TaskName "{TASK_NAME}" | '
        "Select-Object LastRunTime, NextRunTime, LastTaskResult, NumberOfMissedRuns | "
        "ConvertTo-Json -Compress"
    )
    r = _ps_json(ps, timeout_s=10)
    if not r["ok"]:
        return {"ok": False, "error": r["error"]}
    d = r["data"]
    try:
        last = int(d.get("LastTaskResult", 0))
        d["LastTaskResultHex"] = f"0x{(last & 0xFFFFFFFF):08X}"
    except Exception:
        d["LastTaskResultHex"] = "n/a"
    return {"ok": True, "data": d}


def _task_state() -> Dict[str, Any]:
    ps = (
        f'Get-ScheduledTask -TaskName "{TASK_NAME}" | '
        "Select-Object TaskName, State | ConvertTo-Json -Compress"
    )
    r = _ps_json(ps, timeout_s=10)
    if not r["ok"]:
        return {"ok": False, "error": r["error"]}
    return {"ok": True, "data": r["data"]}


def _state_label(state_val: Any) -> str:
    """
    Task Scheduler state mapping is not always consistent across environments.
    We'll handle both numeric and string forms.
    """
    if isinstance(state_val, str):
        s = state_val.strip()
        return s if s else "Unknown"
    if isinstance(state_val, int):
        # Common mapping: 3=Ready, 4=Running. Others vary.
        if state_val == 3:
            return "Ready"
        if state_val == 4:
            return "Running"
        if state_val == 2:
            return "Queued"
        if state_val == 1:
            return "Disabled"
        return f"State({state_val})"
    return "Unknown"


def _ops_processes() -> Dict[str, Any]:
    # Ensure we always emit valid JSON (at least []) to avoid json_parse_error when no processes.
    rx = r"ops_loop_5m_stage6c\.ps1|auto_loop_5m\.ps1"
    ps = f"""
$rx = '{rx}'
$rows = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
  Where-Object {{ $_.CommandLine -match $rx }} |
  Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine

if ($null -eq $rows) {{
  '[]'
}} else {{
  # Force array semantics
  @($rows) | ConvertTo-Json -Compress
}}
""".strip()

    r = _ps_json(ps, timeout_s=10)
    if not r["ok"]:
        return {"ok": False, "error": r["error"]}

    data = r["data"]
    if isinstance(data, dict):
        data = [data]
    if data is None:
        data = []
    return {"ok": True, "data": data}


# -----------------------------
# Render
# -----------------------------
def render_ops_controls() -> None:
    st.header("OPS Controls")
    st.caption(
        "Safe-by-default: UI only manipulates control files; no BUY/SELL, no overrides."
    )

    # Operator mode is controlled by app_streamlit sidebar.
    operator_mode = bool(st.session_state.get("operator_mode", True))

    top = st.columns([2, 1], gap="large")
    with top[0]:
        st.write({"operator_mode": operator_mode})
    with top[1]:
        if st.button("Refresh", use_container_width=True, key="ops_refresh_btn"):
            st.rerun()

    # --- Stop Flag + Snapshot ---
    c1, c2 = st.columns(2, gap="large")

    with c1:
        st.subheader("Stop Flag")
        sf = _file_stat(STOP_FLAG)
        st.json(sf)

        cur = bool(sf.get("exists", False))
        new_val = st.toggle(
            "Pause OPS loop (stop.flag)",
            value=cur,
            disabled=(not operator_mode),
            key="ops_stopflag_toggle",
            help="Creates/removes args/data/stop.flag. Wrapper exits 0 and skips snapshot + inner loop.",
        )
        if not operator_mode:
            st.info("Operator mode is OFF — controls are locked.")
        if (new_val != cur) and operator_mode:
            _set_stopflag(new_val)
            st.rerun()

    with c2:
        st.subheader("Snapshot Health")
        ss = _file_stat(SNAPSHOT)
        st.json(ss)

        if not ss.get("exists"):
            st.error("Snapshot file missing: args/data/ibkr_open_orders_live.jsonl")
        else:
            if float(ss.get("age_min", 9999)) <= 6.0:
                st.success("Snapshot fresh")
            else:
                st.warning("Snapshot stale (age too high)")

    st.divider()

    # --- Task Scheduler + Processes ---
    c3, c4 = st.columns(2, gap="large")

    with c3:
        st.subheader("Task Scheduler Health")
        ti = _task_info()
        ts = _task_state()

        if ti["ok"]:
            st.json(ti["data"])
        else:
            st.error(f"Task info error: {ti['error']}")

        if ts["ok"]:
            d = ts["data"]
            state_val = d.get("State") if isinstance(d, dict) else d
            st.write(
                {
                    "TaskName": d.get("TaskName") if isinstance(d, dict) else TASK_NAME,
                    "State": _state_label(state_val),
                }
            )
        else:
            st.warning(f"Task state error: {ts['error']}")

    with c4:
        st.subheader("OPS Processes (overlap check)")
        pr = _ops_processes()
        if pr["ok"]:
            st.json(pr["data"])
        else:
            st.error(f"Process query error: {pr['error']}")

    st.divider()

    # --- Logs ---
    st.subheader("Logs (tail)")
    colA, colB = st.columns(2, gap="large")

    with colA:
        st.caption("ops_stage6c.log (last 200 lines)")
        st.text_area(
            "ops_log_tail", _read_tail(OPS_LOG, 200), height=320, key="ops_log_tail_box"
        )
        if OPS_LOG.exists():
            st.download_button(
                "Download ops_stage6c.log",
                data=OPS_LOG.read_bytes(),
                file_name="ops_stage6c.log",
                mime="text/plain",
                key="dl_ops_log",
                use_container_width=True,
            )

    with colB:
        latest = _latest_cycle_log()
        st.caption(
            f"latest cycle log (last 200 lines): {str(latest) if latest else '[missing]'}"
        )
        if latest and latest.exists():
            st.text_area(
                "cycle_log_tail",
                _read_tail(latest, 200),
                height=320,
                key="cycle_log_tail_box",
            )
            st.download_button(
                "Download latest cycle log",
                data=latest.read_bytes(),
                file_name=latest.name,
                mime="text/plain",
                key="dl_cycle_log",
                use_container_width=True,
            )
        else:
            st.text_area(
                "cycle_log_tail",
                "[missing] no auto_loop_*_cycle*.log found",
                height=320,
                key="cycle_log_tail_box",
            )
