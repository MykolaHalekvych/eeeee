# args/ui/ops_watchdog_tab.py
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

OPS_HEALTH_PATH = DATA_DIR / "ops_health.json"
STOP_FLAG_PATH = DATA_DIR / "stop.flag"
WATCHDOG_LOG_PATH = LOGS_DIR / "ops_watchdog.log"


@dataclass(frozen=True)
class HealthSummary:
    stage: str
    run_id: str
    ts_utc: str
    host: str
    health_level: str
    ops_state: str
    stale_locks: int
    main_red_flags: int
    cycle_red_flags: int
    proc_total: int
    task_wrapper_exists: Optional[bool]
    snapshot_status: Optional[str]


def _safe_read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _as_str(x: Any, default: str = "") -> str:
    try:
        s = str(x)
        return s if s else default
    except Exception:
        return default


def _extract_summary(payload: Dict[str, Any]) -> HealthSummary:
    # Keep consistent with Stage7 stdout shape, but derive from full payload.
    stage = _as_str(payload.get("stage"), "Stage7")
    run_id = _as_str(payload.get("run_id"), "")
    ts_utc = _as_str(payload.get("ts_utc"), "")
    host = _as_str(payload.get("host"), "")
    health_level = _as_str(payload.get("health_level"), "UNKNOWN")
    ops_state = _as_str(payload.get("ops_state"), "UNKNOWN")

    stale_locks = _as_int(payload.get("locks", {}).get("stale_count", 0), 0)

    main_red_flags = _as_int(
        payload.get("logs", {}).get("main", {}).get("red_flag_hits", 0), 0
    )
    cycle_red_flags = _as_int(
        payload.get("logs", {}).get("cycle", {}).get("red_flag_hits", 0), 0
    )

    proc_total = _as_int(payload.get("processes", {}).get("total_count", 0), 0)

    task_wrapper_exists = None
    try:
        task_wrapper_exists = (
            payload.get("tasks", {}).get("wrapper", {}).get("exists", None)
        )
    except Exception:
        task_wrapper_exists = None

    snapshot_status = None
    try:
        snapshot_status = payload.get("snapshot", {}).get("status", None)
    except Exception:
        snapshot_status = None

    return HealthSummary(
        stage=stage,
        run_id=run_id,
        ts_utc=ts_utc,
        host=host,
        health_level=health_level,
        ops_state=ops_state,
        stale_locks=stale_locks,
        main_red_flags=main_red_flags,
        cycle_red_flags=cycle_red_flags,
        proc_total=proc_total,
        task_wrapper_exists=task_wrapper_exists,
        snapshot_status=snapshot_status,
    )


def _open_in_explorer(path: Path) -> None:
    try:
        if path.is_dir():
            subprocess.Popen(["explorer.exe", str(path)])
        else:
            # open file with default associated app
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
    except Exception as e:
        st.error(f"Open failed: {e}")


def _tail_text_file(path: Path, max_lines: int = 120, max_bytes: int = 256_000) -> str:
    """
    Safe-ish tail for large logs:
    - read only last max_bytes
    - return last max_lines lines
    """
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with path.open("rb") as f:
            f.seek(start)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[-max_lines:])
    except Exception:
        # fallback
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except Exception:
            return ""


def _render_findings(payload: Dict[str, Any]) -> None:
    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        findings = []

    if len(findings) == 0:
        st.success("No findings.")
        return

    # compact table
    rows: List[Dict[str, Any]] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        rows.append(
            {
                "severity": f.get("severity"),
                "code": f.get("code"),
                "message": f.get("message"),
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _toggle_stop_flag() -> None:
    try:
        if STOP_FLAG_PATH.exists():
            STOP_FLAG_PATH.unlink()
            st.success("stop.flag removed (ops_state should become RUNNING).")
        else:
            STOP_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
            STOP_FLAG_PATH.write_text("STOP\n", encoding="utf-8")
            st.warning("stop.flag created (ops_state should become PAUSED).")
    except Exception as e:
        st.error(f"Toggle failed: {e}")


def render_ops_watchdog_tab() -> None:
    st.subheader("Stage 7 — Ops Watchdog")

    # Operator gate (safe-by-default)
    operator_mode = st.checkbox(
        "Operator mode (safe controls)", value=False, key="stage7_operator_mode"
    )

    # Quick actions
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
    with c1:
        if st.button("Refresh", key="stage7_refresh"):
            st.rerun()
    with c2:
        if st.button("Open logs folder", key="stage7_open_logs"):
            _open_in_explorer(LOGS_DIR)
    with c3:
        if st.button("Open ops_watchdog.log", key="stage7_open_watchdog_log"):
            _open_in_explorer(WATCHDOG_LOG_PATH)
    with c4:
        if operator_mode:
            if st.button("Toggle stop.flag", key="stage7_toggle_stop"):
                _toggle_stop_flag()
                st.rerun()
        else:
            st.caption("Toggle stop.flag requires Operator mode.")

    # Status strip
    payload = _safe_read_json(OPS_HEALTH_PATH)
    stop_present = STOP_FLAG_PATH.exists()

    if payload is None:
        st.warning("ops_health.json not found. Run watchdog at least once.")
        st.write(
            {
                "ops_health_path": str(OPS_HEALTH_PATH),
                "stop_flag_present": stop_present,
                "watchdog_log_path": str(WATCHDOG_LOG_PATH),
            }
        )
        return

    s = _extract_summary(payload)

    # High-level KPIs
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Health", s.health_level)
    k2.metric("Ops state", s.ops_state)
    k3.metric("Snapshot", s.snapshot_status or "UNKNOWN")
    k4.metric("Proc total", s.proc_total)
    k5.metric("Stale locks", s.stale_locks)
    k6.metric("Wrapper task", str(s.task_wrapper_exists))

    st.caption(
        f"run_id={s.run_id} | host={s.host} | ts_utc={s.ts_utc} | stop.flag_present={stop_present}"
    )

    # Findings
    st.markdown("### Findings")
    _render_findings(payload)

    # Details (operator-friendly, no huge dumps by default)
    st.markdown("### Details")

    with st.expander("Tasks", expanded=False):
        st.json(payload.get("tasks", {}))

    with st.expander("Snapshot", expanded=False):
        st.json(payload.get("snapshot", {}))

    with st.expander("Processes", expanded=False):
        st.json(payload.get("processes", {}))

    with st.expander("Locks", expanded=False):
        st.json(payload.get("locks", {}))

    with st.expander("Logs (red flags only)", expanded=False):
        logs = payload.get("logs", {})
        main = logs.get("main", {}) if isinstance(logs, dict) else {}
        cycle = logs.get("cycle", {}) if isinstance(logs, dict) else {}

        st.write(
            {
                "main": {
                    "path": main.get("path"),
                    "exists": main.get("exists"),
                    "timed_out": main.get("timed_out"),
                    "error": main.get("error"),
                    "red_flag_hits": main.get("red_flag_hits"),
                    "red_flag_patterns": main.get("red_flag_patterns"),
                    "sample_hits": main.get("sample_hits"),
                },
                "cycle": {
                    "glob": cycle.get("glob"),
                    "enum_timed_out": cycle.get("enum_timed_out"),
                    "enum_error": cycle.get("enum_error"),
                    "total_matches": cycle.get("total_matches"),
                    "red_flag_hits": cycle.get("red_flag_hits"),
                    "sample_hits": cycle.get("sample_hits"),
                },
            }
        )

    with st.expander("ops_watchdog.log tail", expanded=False):
        max_lines = st.number_input(
            "Tail lines",
            min_value=20,
            max_value=400,
            value=120,
            step=10,
            key="stage7_tail_lines",
        )
        txt = _tail_text_file(WATCHDOG_LOG_PATH, max_lines=int(max_lines))
        if not txt:
            st.info("No log content found (or file missing).")
        else:
            st.code(txt, language="text")
