# args/ui/run_explorer_tab.py
from __future__ import annotations

import json
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st


# -----------------------------
# Data model
# -----------------------------

@dataclass(frozen=True)
class RunArtifacts:
    run_id: str
    events_path: Optional[Path]
    orders_path: Optional[Path]
    report_path: Optional[Path]
    last_modified_utc: Optional[str]
    completeness: str  # e.g. "events+orders+report", "events+orders", "events", ...


# -----------------------------
# Filesystem helpers
# -----------------------------

def repo_root() -> Path:
    # args/ui/run_explorer_tab.py -> parents[0]=ui, [1]=args, [2]=repo root
    return Path(__file__).resolve().parents[2]


def default_dirs() -> Tuple[Path, Path]:
    root = repo_root()
    data_dir = root / "args" / "data"
    logs_dir = root / "args" / "logs"
    return data_dir, logs_dir


def safe_open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            st.warning("Open folder is supported on Windows only in this UI.")
    except Exception as e:
        st.error(f"Failed to open folder: {e}")


# -----------------------------
# Run indexing
# -----------------------------

_RX_EVENTS = re.compile(r"^events_run_(?P<rid>.+)\.jsonl$", re.IGNORECASE)
_RX_ORDERS = re.compile(r"^orders_paper_(?P<rid>.+)\.jsonl$", re.IGNORECASE)
_RX_REPORT = re.compile(r"^run_report_(?P<rid>.+)_paper\.json$", re.IGNORECASE)


def _mtime_utc_str(p: Path) -> str:
    ts = p.stat().st_mtime
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_run_index(data_dir: Path, logs_dir: Path) -> List[RunArtifacts]:
    # Collect candidates from both dirs (report sometimes lives in logs, sometimes in data)
    runs: Dict[str, Dict[str, Optional[Path]]] = defaultdict(lambda: {"events": None, "orders": None, "report": None})
    mtimes: Dict[str, float] = {}

    def register(run_id: str, kind: str, path: Path) -> None:
        runs[run_id][kind] = path
        try:
            mt = path.stat().st_mtime
            mtimes[run_id] = max(mtimes.get(run_id, 0.0), mt)
        except Exception:
            pass

    if data_dir.exists():
        for p in data_dir.iterdir():
            if not p.is_file():
                continue
            m = _RX_EVENTS.match(p.name)
            if m:
                register(m.group("rid"), "events", p)
                continue
            m = _RX_ORDERS.match(p.name)
            if m:
                register(m.group("rid"), "orders", p)
                continue
            m = _RX_REPORT.match(p.name)
            if m:
                register(m.group("rid"), "report", p)
                continue

    if logs_dir.exists():
        for p in logs_dir.iterdir():
            if not p.is_file():
                continue
            m = _RX_REPORT.match(p.name)
            if m:
                register(m.group("rid"), "report", p)
                continue

    out: List[RunArtifacts] = []
    for rid, d in runs.items():
        parts = []
        if d["events"] is not None:
            parts.append("events")
        if d["orders"] is not None:
            parts.append("orders")
        if d["report"] is not None:
            parts.append("report")

        completeness = "+".join(parts) if parts else "none"
        lm = None
        if rid in mtimes:
            lm = datetime.utcfromtimestamp(mtimes[rid]).strftime("%Y-%m-%d %H:%M:%S UTC")

        out.append(
            RunArtifacts(
                run_id=rid,
                events_path=d["events"],
                orders_path=d["orders"],
                report_path=d["report"],
                last_modified_utc=lm,
                completeness=completeness,
            )
        )

    # Sort: newest first (by mtime), then by run_id
    out.sort(key=lambda x: (x.last_modified_utc or "", x.run_id), reverse=True)
    return out


# -----------------------------
# JSONL parsing (safe, bounded)
# -----------------------------

_DECISION_KEYS = ("decision", "ma_decision", "final_decision", "gate_decision", "mg_decision", "action", "result")


def _extract_decision(obj: Dict[str, Any]) -> Optional[str]:
    for k in _DECISION_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def summarize_jsonl(
    path: Path,
    tail_n: int = 25,
    max_lines: int = 200_000,
) -> Dict[str, Any]:
    total = 0
    parse_errors = 0
    decision_counts: Dict[str, int] = defaultdict(int)
    tail_raw: deque[str] = deque(maxlen=tail_n)

    # Optional timestamps (best-effort)
    ts_min = None
    ts_max = None

    def consider_ts(v: Any) -> None:
        nonlocal ts_min, ts_max
        if isinstance(v, (int, float)):
            # assume seconds epoch
            try:
                dt = datetime.utcfromtimestamp(float(v))
                ts_min = min(ts_min, dt) if ts_min else dt
                ts_max = max(ts_max, dt) if ts_max else dt
            except Exception:
                return
        if isinstance(v, str):
            # try ISO
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                ts_min = min(ts_min, dt) if ts_min else dt
                ts_max = max(ts_max, dt) if ts_max else dt
            except Exception:
                return

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if total >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                total += 1
                tail_raw.append(line)
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        dec = _extract_decision(obj)
                        if dec:
                            decision_counts[dec] += 1

                        # best-effort time fields
                        for tk in ("ts", "timestamp", "time", "t_utc", "bar_time_utc"):
                            if tk in obj:
                                consider_ts(obj.get(tk))
                                break
                except Exception:
                    parse_errors += 1
                    continue
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "total": 0,
            "parse_errors": 0,
            "decision_counts": {},
            "tail_raw": [],
        }

    return {
        "ok": True,
        "total": total,
        "parse_errors": parse_errors,
        "decision_counts": dict(sorted(decision_counts.items(), key=lambda kv: kv[0])),
        "tail_raw": list(tail_raw),
        "ts_min_utc": ts_min.strftime("%Y-%m-%d %H:%M:%S") + " UTC" if ts_min else None,
        "ts_max_utc": ts_max.strftime("%Y-%m-%d %H:%M:%S") + " UTC" if ts_max else None,
        "truncated": total >= max_lines,
    }


def read_json_safe(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"_ok": False, "_error": str(e)}


# -----------------------------
# IBKR CSV status (minimal, no pandas)
# -----------------------------

def ibkr_csv_status(csv_path: Path, max_scan_lines: int = 5_000) -> Dict[str, Any]:
    if not csv_path.exists():
        return {"ok": False, "reason": "missing"}

    # Best-effort:
    # - count lines
    # - infer first/last timestamp column (usually first col or 'date'/'time')
    try:
        total_lines = 0
        header = None
        first_row = None
        last_row = None

        with csv_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total_lines += 1
                if total_lines == 1:
                    header = line
                elif total_lines == 2:
                    first_row = line
                    last_row = line
                else:
                    last_row = line

                # We do not need to fully scan very large files for UI status
                if total_lines >= max_scan_lines:
                    break

        return {
            "ok": True,
            "lines_scanned": total_lines,
            "file_mtime_utc": _mtime_utc_str(csv_path),
            "header": header,
            "first_row": first_row,
            "last_row": last_row,
            "note": "lines_scanned is bounded for UI safety",
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# -----------------------------
# Streamlit tab renderer
# -----------------------------

def render_run_explorer_tab() -> None:
    st.subheader("Run Explorer (read-only)")

    data_dir, logs_dir = default_dirs()
    col_a, col_b, col_c = st.columns([1, 1, 2])

    with col_a:
        if st.button("Refresh index", key="runexp_btn_refresh"):
            st.rerun()

    with col_b:
        if st.button("Open data folder", key="runexp_btn_open_data"):
            safe_open_folder(data_dir)

    with col_c:
        if st.button("Open logs folder", key="runexp_btn_open_logs"):
            safe_open_folder(logs_dir)

    st.caption(f"Data: {data_dir} | Logs: {logs_dir}")

    runs = build_run_index(data_dir, logs_dir)
    if not runs:
        st.info("No runs found yet. Generate a run first (paper loop / demo).")
        st.stop()

    run_labels = [
        f"{r.run_id}  |  {r.completeness}  |  {r.last_modified_utc or 'mtime: n/a'}"
        for r in runs
    ]
    sel = st.selectbox("Select run", run_labels, index=0, key="runexp_select_run")

    selected = runs[run_labels.index(sel)]
    st.write("")

    meta = {
        "run_id": selected.run_id,
        "completeness": selected.completeness,
        "last_modified_utc": selected.last_modified_utc,
        "events_path": str(selected.events_path) if selected.events_path else None,
        "orders_path": str(selected.orders_path) if selected.orders_path else None,
        "report_path": str(selected.report_path) if selected.report_path else None,
    }
    st.json(meta)

    st.divider()

    # Report
    st.markdown("### Run report")
    if selected.report_path and selected.report_path.exists():
        rep = read_json_safe(selected.report_path)
        if rep.get("_ok") is False:
            st.error(f"Failed to read report JSON: {rep.get('_error')}")
        else:
            st.json(rep)
    else:
        st.warning("No run_report_*_paper.json found for this run_id.")

    st.divider()

    # Events
    st.markdown("### Events (events_run_*.jsonl)")
    if selected.events_path and selected.events_path.exists():
        ev = summarize_jsonl(selected.events_path, tail_n=25)
        if not ev.get("ok"):
            st.error(f"Failed to read events JSONL: {ev.get('error')}")
        else:
            cols = st.columns(4)
            cols[0].metric("Lines", ev["total"])
            cols[1].metric("Parse errors", ev["parse_errors"])
            cols[2].metric("Time min", ev.get("ts_min_utc") or "n/a")
            cols[3].metric("Time max", ev.get("ts_max_utc") or "n/a")

            if ev.get("truncated"):
                st.warning("Events file is large; UI scan is truncated for safety.")

            st.write("Decision histogram:")
            st.json(ev["decision_counts"])

            with st.expander("Tail (last 25 lines)", expanded=False):
                st.code("\n".join(ev["tail_raw"]), language="json")
    else:
        st.warning("No events_run_*.jsonl found for this run_id.")

    st.divider()

    # Orders
    st.markdown("### Orders (orders_paper_*.jsonl)")
    if selected.orders_path and selected.orders_path.exists():
        od = summarize_jsonl(selected.orders_path, tail_n=25)
        if not od.get("ok"):
            st.error(f"Failed to read orders JSONL: {od.get('error')}")
        else:
            c1, c2 = st.columns(2)
            c1.metric("Lines", od["total"])
            c2.metric("Parse errors", od["parse_errors"])

            with st.expander("Tail (last 25 lines)", expanded=False):
                st.code("\n".join(od["tail_raw"]), language="json")
    else:
        st.warning("No orders_paper_*.jsonl found for this run_id.")

    st.divider()

    # IBKR CSV status (minimal)
    st.markdown("### IBKR data status (hg_5m_bars_ibkr.csv)")
    csv_path = data_dir / "hg_5m_bars_ibkr.csv"
    stat = ibkr_csv_status(csv_path)
    if not stat.get("ok"):
        st.info("No IBKR CSV found yet: args/data/hg_5m_bars_ibkr.csv")
    else:
        st.json(stat)
