# args/ui/run_explorer_tab.py
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st


# -----------------------------
# Auto-refresh helpers (no deps)
# -----------------------------
def _st_rerun() -> None:
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()


def _auto_refresh_tick(enabled: bool, interval_s: int) -> None:
    if not enabled:
        return
    interval_s = int(max(5, min(60, interval_s)))
    time.sleep(interval_s)
    _st_rerun()


# -----------------------------
# Data model
# -----------------------------
@dataclass(frozen=True)
class RunArtifacts:
    run_id: str
    events_path: Optional[Path]
    orders_path: Optional[Path]
    report_path: Optional[Path]
    order_intents_path: Optional[Path]
    payload_path: Optional[Path]
    sendplan_path: Optional[Path]
    last_modified_utc: Optional[str]
    completeness: str  # e.g. "events+orders+report+oi+payload+sendplan"


# -----------------------------
# Filesystem helpers
# -----------------------------
def repo_root() -> Path:
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


def _fmt_age(age_s: float) -> str:
    age_s = max(0.0, float(age_s))
    if age_s < 60:
        return f"{int(age_s)}s"
    if age_s < 3600:
        m = int(age_s // 60)
        s = int(age_s % 60)
        return f"{m}m {s}s"
    h = int(age_s // 3600)
    m = int((age_s % 3600) // 60)
    return f"{h}h {m}m"


def _fmt_task_dt(v: Any) -> str:
    if v is None:
        return "n/a"
    s = str(v)
    m = re.search(r"/Date\((\d+)\)/", s)
    if m:
        ms = int(m.group(1))
        dt = datetime.utcfromtimestamp(ms / 1000.0)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    return s


def _mtime_utc_str(p: Path) -> str:
    ts = p.stat().st_mtime
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")


def _mtime_info(path: Path) -> Dict[str, Any]:
    try:
        mt = path.stat().st_mtime
        return {
            "exists": True,
            "mtime_utc": datetime.utcfromtimestamp(mt).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
            "age_s": float(time.time() - mt),
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


def _latest_matching(dir_path: Path, prefix: str, suffix: str) -> Optional[Path]:
    try:
        if not dir_path.exists():
            return None
        best: Optional[Path] = None
        best_mt = -1.0
        for p in dir_path.iterdir():
            if not p.is_file():
                continue
            n = p.name
            if not (n.startswith(prefix) and n.endswith(suffix)):
                continue
            mt = p.stat().st_mtime
            if mt > best_mt:
                best_mt = mt
                best = p
        return best
    except Exception:
        return None


def _clip(s: str, n: int = 2000) -> str:
    s2 = str(s or "")
    return s2 if len(s2) <= n else (s2[:n] + "\n...[clipped]...")


# -----------------------------
# Stage 6 UI: Reconcile helpers
# -----------------------------
def _run_snapshot_refresh(
    repo: Path, out_path: Path, timeout_s: float = 30.0
) -> Dict[str, Any]:
    """
    Safe action: refresh open-orders snapshot via snapshotter.
    Does NOT trade and does NOT run executor.
    """
    cmd = [
        "py",
        "-3.11",
        "-m",
        "args.ibkr.ibkr_open_orders_snapshotter_v0",
        "--host",
        "127.0.0.1",
        "--port",
        "7497",
        "--client-id",
        "11",
        "--timeout-s",
        "15",
        "--wait-s",
        "5",
        "--out",
        str(out_path),
    ]
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(repo),
        )
        return {
            "ok": cp.returncode == 0,
            "returncode": cp.returncode,
            "stdout": (cp.stdout or "").strip(),
            "stderr": (cp.stderr or "").strip(),
            "out_path": str(out_path),
            "mtime_utc": _mtime_info(out_path).get("mtime_utc")
            if out_path.exists()
            else None,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": repr(e),
            "out_path": str(out_path),
            "mtime_utc": _mtime_info(out_path).get("mtime_utc")
            if out_path.exists()
            else None,
        }


def _scan_last_skip_reconcile(
    exec_events_path: Path, max_lines: int = 200_000
) -> Optional[Dict[str, Any]]:
    """
    Best-effort: scan exec events for last ORDER_SKIP_RECONCILE.
    Works even if there is no run_report wiring.
    """
    last: Optional[Dict[str, Any]] = None
    try:
        with exec_events_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            seen = 0
            for line in f:
                if seen >= max_lines:
                    break
                s = line.strip()
                if not s:
                    continue
                seen += 1
                try:
                    obj = json.loads(s)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                kind = str(obj.get("kind") or obj.get("type") or "").strip().upper()
                if kind == "ORDER_SKIP_RECONCILE":
                    last = obj
    except Exception:
        return None
    return last


# -----------------------------
# Control Plane (file-based)
# -----------------------------
_ALLOWED_GLOBAL_MODES = ["NO_TRADE", "ONLY_EXITS", "ALLOW_NEW_ENTRIES"]


def _normalize_global_mode(v: Any, default: str = "NO_TRADE") -> str:
    if not isinstance(v, str):
        return default
    s = v.strip().upper()
    return s if s in _ALLOWED_GLOBAL_MODES else default


def _control_state_path(data_dir: Path) -> Path:
    return data_dir / "control_state.json"


def _load_control_state(data_dir: Path) -> Dict[str, Any]:
    """
    BOM-safe read of control_state.json.
    Returns dict with guaranteed 'global_mode' key (normalized).
    """
    p = _control_state_path(data_dir)
    if not p.exists():
        return {
            "_ok": True,
            "_exists": False,
            "_path": str(p),
            "global_mode": "NO_TRADE",
        }

    try:
        with p.open("r", encoding="utf-8-sig", errors="replace") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            obj = {}
        obj["global_mode"] = _normalize_global_mode(
            obj.get("global_mode"), default="NO_TRADE"
        )
        obj["_ok"] = True
        obj["_exists"] = True
        obj["_path"] = str(p)
        return obj
    except Exception as e:
        return {
            "_ok": False,
            "_exists": True,
            "_path": str(p),
            "_error": str(e),
            "global_mode": "NO_TRADE",
        }


def _save_control_state(data_dir: Path, global_mode: str) -> Tuple[bool, str]:
    p = _control_state_path(data_dir)
    try:
        obj = {"global_mode": _normalize_global_mode(global_mode, default="NO_TRADE")}
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write without BOM; readers are BOM-safe anyway.
        with p.open("w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return True, str(p)
    except Exception as e:
        return False, str(e)


# -----------------------------
# Step 5 UI: MA Explain helpers
# -----------------------------
def _evt_type(e: dict) -> str:
    for k in ("type", "kind", "event_type", "name"):
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return ""


def _dg(d: dict, path: str, default=None):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _fmt(v):
    if v is None:
        return "None"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def _extract_ma_explain(evt: dict, control_state: dict) -> dict:
    ma_input = evt.get("ma_input", {}) if isinstance(evt.get("ma_input"), dict) else {}
    state_d = (
        ma_input.get("state", {}) if isinstance(ma_input.get("state"), dict) else {}
    )
    risk_d = ma_input.get("risk", {}) if isinstance(ma_input.get("risk"), dict) else {}

    re_d = (
        evt.get("risk_envelope", {})
        if isinstance(evt.get("risk_envelope"), dict)
        else {}
    )
    limits = re_d.get("limits", {}) if isinstance(re_d.get("limits"), dict) else {}

    operator_mode = None
    if isinstance(control_state, dict):
        operator_mode = control_state.get("global_mode")

    exec_mode = _dg(ma_input, "exec.global_mode", None)
    re_exec_mode = re_d.get("exec_global_mode", None)

    out = {
        "operator_global_mode": operator_mode,
        "ma_input_exec_global_mode": exec_mode,
        "risk_envelope_exec_global_mode": re_exec_mode,
        "ma_decision": evt.get("ma_decision"),
        "enforced_no_trade": re_d.get("enforced_no_trade"),
        "risk_mode": re_d.get("mode"),
        "mode_source": re_d.get("mode_source"),
        "mode_invariant_ok": re_d.get("mode_invariant_ok", None),
        "truth": {
            "state.regime": state_d.get("regime"),
            "state.confidence": state_d.get("confidence"),
            "state.tail_risk": state_d.get("tail_risk"),
            "risk.margin_usage": risk_d.get("margin_usage"),
        },
        "limits": {
            "conf_min": limits.get("conf_min"),
            "margin_max": limits.get("margin_max"),
        },
        "violations": evt.get("violations", [])
        if isinstance(evt.get("violations"), list)
        else [],
    }
    return out


def _compute_mismatch_flags(explain: dict) -> List[str]:
    flags: List[str] = []

    op = str(explain.get("operator_global_mode") or "").strip().upper()
    ex = str(explain.get("ma_input_exec_global_mode") or "").strip().upper()
    rx = str(explain.get("risk_mode") or "").strip().upper()
    dec = str(explain.get("ma_decision") or "").strip().upper()
    enforced = bool(explain.get("enforced_no_trade"))

    inv = explain.get("mode_invariant_ok", None)
    if inv is False:
        flags.append("risk_envelope.mode_invariant_ok == false")

    if dec == "ALLOW" and rx == "NO_TRADE":
        flags.append("ma_decision=ALLOW while risk_envelope.mode=NO_TRADE")

    if (not enforced) and ex == "ALLOW_NEW_ENTRIES" and rx != "ALLOW_NEW_ENTRIES":
        flags.append(
            "exec.global_mode=ALLOW_NEW_ENTRIES but risk_envelope.mode != ALLOW_NEW_ENTRIES"
        )

    if op and ex and op != ex:
        flags.append(f"operator global_mode ({op}) != ma_input.exec.global_mode ({ex})")

    return flags


def _render_step5_ma_explain(evt: dict, control_state: dict) -> None:
    if not isinstance(evt, dict) or not evt:
        st.info("No selected event to explain.")
        return

    ex = _extract_ma_explain(evt, control_state)
    mism = _compute_mismatch_flags(ex)

    st.subheader("Step 5 — MA / Control Plane Explain")

    # Control Plane Echo
    c1, c2, c3 = st.columns(3)
    c1.metric("Operator global_mode", _fmt(ex["operator_global_mode"]))
    c2.metric("ma_input.exec.global_mode", _fmt(ex["ma_input_exec_global_mode"]))
    c3.metric(
        "risk_envelope.exec_global_mode", _fmt(ex["risk_envelope_exec_global_mode"])
    )

    # Core decision/mode
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("ma_decision", _fmt(ex["ma_decision"]))
    d2.metric("enforced_no_trade", _fmt(ex["enforced_no_trade"]))
    d3.metric("risk_envelope.mode", _fmt(ex["risk_mode"]))
    d4.metric("mode_source", _fmt(ex["mode_source"]))

    if mism:
        st.error("MISMATCH / FLAGS:\n- " + "\n- ".join(mism))

    # Truth snapshot + limits
    st.markdown("**Truth snapshot (ma_input) + limits**")
    truth = ex["truth"]
    lim = ex["limits"]
    rows = [
        {
            "field": "state.regime",
            "value": _fmt(truth.get("state.regime")),
            "limit": "",
        },
        {
            "field": "state.confidence",
            "value": _fmt(truth.get("state.confidence")),
            "limit": f"conf_min={_fmt(lim.get('conf_min'))}",
        },
        {
            "field": "state.tail_risk",
            "value": _fmt(truth.get("state.tail_risk")),
            "limit": "",
        },
        {
            "field": "risk.margin_usage",
            "value": _fmt(truth.get("risk.margin_usage")),
            "limit": f"margin_max={_fmt(lim.get('margin_max'))}",
        },
    ]
    st.table(rows)

    # Violations list
    v = ex["violations"]
    st.markdown(f"**Violations** ({len(v)})")
    if v:
        st.table(
            [
                {
                    "block": x.get("block"),
                    "rule_id": x.get("rule_id"),
                    "decision": x.get("decision"),
                    "reason": x.get("reason"),
                }
                for x in v[:30]
                if isinstance(x, dict)
            ]
        )
    else:
        st.write("No violations in this event.")

    # Dominance hint
    if (
        bool(ex.get("enforced_no_trade"))
        and str(ex.get("ma_input_exec_global_mode") or "").strip().upper()
        == "ALLOW_NEW_ENTRIES"
    ):
        st.info(
            "Operator ALLOW_NEW_ENTRIES is dominated by enforced_no_trade (expected)."
        )


# -----------------------------
# JSON helpers
# -----------------------------
def read_json_safe(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
            return json.load(f)
    except Exception as e:
        return {"_ok": False, "_error": str(e)}


def _read_ibkr_connection(data_dir: Path) -> Dict[str, Any]:
    cfg_path = data_dir / "ibkr_connection_v0.json"
    if not cfg_path.exists():
        return {
            "ok": False,
            "error": "ibkr_connection_v0.json missing",
            "path": str(cfg_path),
        }
    obj = read_json_safe(cfg_path)
    if obj.get("_ok") is False:
        return {"ok": False, "error": obj.get("_error"), "path": str(cfg_path)}
    host = str(obj.get("host", "localhost"))
    port = int(obj.get("port", 7497))
    return {"ok": True, "host": host, "port": port, "path": str(cfg_path)}


def _tcp_check(host: str, port: int, timeout_s: float = 0.35) -> bool:
    targets: List[Tuple[str, int]]
    h = str(host).strip().lower()
    if h in ("localhost", "127.0.0.1", "::1"):
        targets = [("127.0.0.1", int(port)), ("::1", int(port))]
    else:
        targets = [(host, int(port))]

    for hh, pp in targets:
        try:
            with socket.create_connection((hh, pp), timeout=timeout_s):
                return True
        except Exception:
            continue
    return False


def _get_task_info(task_name: str) -> Dict[str, Any]:
    """
    Read-only Scheduled Task status via PowerShell -> JSON.
    """
    if os.name != "nt":
        return {"ok": False, "error": "windows-only"}

    ps = (
        f"$t=Get-ScheduledTask -TaskName '{task_name}';"
        f"$ti=$t | Get-ScheduledTaskInfo;"
        f"[pscustomobject]@{{"
        f"TaskName=$t.TaskName;"
        f"State=$t.State;"
        f"LastRunTime=$ti.LastRunTime;"
        f"NextRunTime=$ti.NextRunTime;"
        f"LastTaskResult=$ti.LastTaskResult;"
        f"NumberOfMissedRuns=$ti.NumberOfMissedRuns;"
        f"Arguments=$t.Actions[0].Arguments;"
        f"}} | ConvertTo-Json -Compress"
    )

    try:
        cp = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
        if cp.returncode != 0:
            return {
                "ok": False,
                "error": (cp.stderr or cp.stdout or "").strip(),
                "returncode": cp.returncode,
            }
        raw = (cp.stdout or "").strip()
        if not raw:
            return {"ok": False, "error": "empty output"}
        return {"ok": True, "data": json.loads(raw)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -----------------------------
# Run indexing (core artifacts)
# -----------------------------
_RX_EVENTS = re.compile(r"^events_run_(?P<rid>.+)\.jsonl$", re.IGNORECASE)
_RX_ORDERS = re.compile(r"^orders_paper_(?P<rid>.+)\.jsonl$", re.IGNORECASE)
_RX_REPORT = re.compile(r"^run_report_(?P<rid>.+)_paper\.json$", re.IGNORECASE)

# Stage 4.x artifacts
_RX_OI = re.compile(r"^order_intents_(?P<rid>.+)\.jsonl$", re.IGNORECASE)
_RX_PAYLOAD = re.compile(r"^orders_payload_(?P<rid>.+)\.jsonl$", re.IGNORECASE)
_RX_SENDPLAN = re.compile(r"^orders_sendplan_(?P<rid>.+)\.jsonl$", re.IGNORECASE)


def build_run_index(data_dir: Path, logs_dir: Path) -> List[RunArtifacts]:
    runs: Dict[str, Dict[str, Optional[Path]]] = defaultdict(
        lambda: {
            "events": None,
            "orders": None,
            "report": None,
            "oi": None,
            "payload": None,
            "sendplan": None,
        }
    )
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

            m = _RX_OI.match(p.name)
            if m:
                register(m.group("rid"), "oi", p)
                continue

            m = _RX_PAYLOAD.match(p.name)
            if m:
                register(m.group("rid"), "payload", p)
                continue

            m = _RX_SENDPLAN.match(p.name)
            if m:
                register(m.group("rid"), "sendplan", p)
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
        if d["oi"] is not None:
            parts.append("oi")
        if d["payload"] is not None:
            parts.append("payload")
        if d["sendplan"] is not None:
            parts.append("sendplan")

        completeness = "+".join(parts) if parts else "none"
        lm = None
        if rid in mtimes:
            lm = datetime.utcfromtimestamp(mtimes[rid]).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )

        out.append(
            RunArtifacts(
                run_id=rid,
                events_path=d["events"],
                orders_path=d["orders"],
                report_path=d["report"],
                order_intents_path=d["oi"],
                payload_path=d["payload"],
                sendplan_path=d["sendplan"],
                last_modified_utc=lm,
                completeness=completeness,
            )
        )

    out.sort(key=lambda x: (x.last_modified_utc or "", x.run_id), reverse=True)
    return out


# -----------------------------
# JSONL parsing (generic + stage-specific)
# -----------------------------
_DECISION_KEYS = (
    "decision",
    "ma_decision",
    "final_decision",
    "gate_decision",
    "mg_decision",
    "action",
    "result",
)


def _extract_decision(obj: Dict[str, Any]) -> Optional[str]:
    for k in _DECISION_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def summarize_jsonl(
    path: Path, tail_n: int = 25, max_lines: int = 200_000
) -> Dict[str, Any]:
    total = 0
    parse_errors = 0
    decision_counts: Dict[str, int] = defaultdict(int)
    tail_raw: deque[str] = deque(maxlen=tail_n)

    ts_min = None
    ts_max = None

    def consider_ts(v: Any) -> None:
        nonlocal ts_min, ts_max
        if isinstance(v, (int, float)):
            try:
                dt = datetime.utcfromtimestamp(float(v))
                ts_min = min(ts_min, dt) if ts_min else dt
                ts_max = max(ts_max, dt) if ts_max else dt
            except Exception:
                return
        if isinstance(v, str):
            try:
                dt = (
                    datetime.fromisoformat(v.replace("Z", "+00:00"))
                    .astimezone()
                    .replace(tzinfo=None)
                )
                ts_min = min(ts_min, dt) if ts_min else dt
                ts_max = max(ts_max, dt) if ts_max else dt
            except Exception:
                return

    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
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


def _scan_tick_events(
    events_path: Path, max_ticks: int = 250, max_lines: int = 250_000
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with events_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            seen = 0
            for line in f:
                if seen >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                seen += 1
                try:
                    e = json.loads(line)
                    if not isinstance(e, dict):
                        continue
                    if _evt_type(e) == "TICK":
                        out.append(e)
                        if len(out) >= max_ticks:
                            break
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _tick_label(e: Dict[str, Any]) -> str:
    idx = _dg(e, "bar.index", None)
    if idx is None:
        idx = _dg(e, "ma_input.bar.index", None)
    if idx is None:
        idx = e.get("index")

    ts = _dg(e, "bar.ts", None)
    if ts is None:
        ts = _dg(e, "ma_input.bar.ts", None)
    if ts is None:
        ts = e.get("ts")

    dec = e.get("ma_decision") or e.get("decision") or ""
    mode = _dg(e, "risk_envelope.mode", None)
    enforced = _dg(e, "risk_envelope.enforced_no_trade", None)
    v = e.get("violations", [])
    vcount = len(v) if isinstance(v, list) else 0
    return f"idx={idx} ts={ts} dec={dec} mode={mode} enforced={enforced} v={vcount}"


_STEP6_EVENT_TYPES = {
    "ORDER_INTENT",
    "ORDER_SUBMIT",
    "ORDER_ACK",
    "ORDER_REJECT",
    "ORDER_FILL",
    "EXEC_FILL",
}


def _summarize_step6_events(
    events_path: Path, max_lines: int = 250_000
) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    try:
        with events_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            seen = 0
            for line in f:
                if seen >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                seen += 1
                try:
                    e = json.loads(line)
                    if not isinstance(e, dict):
                        continue
                    t = _evt_type(e)
                    if t in _STEP6_EVENT_TYPES:
                        counts[t] += 1
                except Exception:
                    continue
    except Exception:
        return {}
    return dict(sorted(counts.items(), key=lambda kv: kv[0]))


def summarize_order_intents_jsonl(
    path: Path, tail_n: int = 15, max_lines: int = 250_000
) -> Dict[str, Any]:
    total = 0
    parse_errors = 0
    none = 0
    allowed = 0
    gated = 0
    kinds: Dict[str, int] = defaultdict(int)
    reasons: Dict[str, int] = defaultdict(int)
    tail_raw: deque[str] = deque(maxlen=tail_n)

    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
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
                    if not isinstance(obj, dict):
                        continue
                    k = str(obj.get("kind") or "").strip().upper() or "UNKNOWN"
                    kinds[k] += 1

                    gr = obj.get("gate_reason")
                    if isinstance(gr, str) and gr.strip():
                        reasons[gr.strip()] += 1

                    if k == "INTENT_NONE":
                        none += 1
                        if isinstance(gr, str) and gr.strip():
                            gated += 1
                    else:
                        allowed += 1
                except Exception:
                    parse_errors += 1
                    continue
    except Exception as e:
        return {"ok": False, "error": str(e)}

    top_reasons = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "ok": True,
        "total": total,
        "allowed": allowed,
        "none": none,
        "gated": gated,
        "parse_errors": parse_errors,
        "kinds": dict(sorted(kinds.items(), key=lambda kv: kv[0])),
        "top_gate_reasons": top_reasons,
        "tail_raw": list(tail_raw),
        "truncated": total >= max_lines,
    }


def summarize_payload_jsonl(
    path: Path, tail_n: int = 10, max_lines: int = 250_000
) -> Dict[str, Any]:
    total = 0
    parse_errors = 0
    kinds: Dict[str, int] = defaultdict(int)
    tail_raw: deque[str] = deque(maxlen=tail_n)

    def payload_kind(obj: Dict[str, Any]) -> str:
        for k in ("payload_kind", "kind", "type"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().upper()
        return "UNKNOWN"

    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
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
                        kinds[payload_kind(obj)] += 1
                except Exception:
                    parse_errors += 1
                    continue
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "total": total,
        "parse_errors": parse_errors,
        "kinds": dict(sorted(kinds.items(), key=lambda kv: kv[0])),
        "tail_raw": list(tail_raw),
        "truncated": total >= max_lines,
    }


def summarize_sendplan_jsonl(
    path: Path, tail_n: int = 10, max_lines: int = 250_000
) -> Dict[str, Any]:
    total = 0
    parse_errors = 0
    kinds: Dict[str, int] = defaultdict(int)
    tail_raw: deque[str] = deque(maxlen=tail_n)

    def plan_kind(obj: Dict[str, Any]) -> str:
        for k in ("plan_kind", "kind", "type", "action"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().upper()
        return "UNKNOWN"

    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
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
                        kinds[plan_kind(obj)] += 1
                except Exception:
                    parse_errors += 1
                    continue
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "total": total,
        "parse_errors": parse_errors,
        "kinds": dict(sorted(kinds.items(), key=lambda kv: kv[0])),
        "tail_raw": list(tail_raw),
        "truncated": total >= max_lines,
    }


# -----------------------------
# IBKR CSV status (minimal, no pandas)
# -----------------------------
def ibkr_csv_status(csv_path: Path, max_scan_lines: int = 5_000) -> Dict[str, Any]:
    if not csv_path.exists():
        return {"ok": False, "reason": "missing"}

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
    st.subheader("Run Explorer (OPS / Observability)")

    data_dir, logs_dir = default_dirs()
    root = repo_root()

    # Load control state (BOM-safe)
    control_state = _load_control_state(data_dir)

    # Operator mode is used to gate "actions" (safe-by-default)
    operator_mode = bool(st.session_state.get("operator_mode", True))

    # -----------------------------
    # Control Plane panel
    # -----------------------------
    with st.expander("Control Plane (control_state.json)", expanded=True):
        p = control_state.get("_path")
        ok = control_state.get("_ok", True)
        if not ok:
            st.error(
                f"Failed to read control_state.json: {control_state.get('_error')}"
            )
        st.caption(f"Path: {p}")

        current_mode = _normalize_global_mode(
            control_state.get("global_mode"), default="NO_TRADE"
        )
        st.caption(f"File global_mode (loaded): {current_mode}")

        c1, c2, c3 = st.columns([2, 1, 2])
        with c1:
            new_mode = st.selectbox(
                "global_mode",
                _ALLOWED_GLOBAL_MODES,
                index=_ALLOWED_GLOBAL_MODES.index(current_mode)
                if current_mode in _ALLOWED_GLOBAL_MODES
                else 0,
                key="cp_global_mode_select",
                help="Operator intent. Can be dominated by enforced_no_trade.",
                disabled=not operator_mode,
            )
        with c2:
            if st.button("Save", key="cp_save_btn", disabled=not operator_mode):
                ok2, msg = _save_control_state(data_dir, new_mode)
                if ok2:
                    st.success(f"Saved: {msg}")
                    _st_rerun()
                else:
                    st.error(f"Save failed: {msg}")
        with c3:
            st.caption(
                "Operator mode controls whether Save is enabled (safe-by-default)."
            )

    # -----------------------------
    # Ops status (read-only)
    # -----------------------------
    with st.expander("Ops status (read-only)", expanded=True):
        task = _get_task_info("ARGS_AutoLoop_5m")
        c1, c2, c3, c4 = st.columns(4)

        if task.get("ok"):
            d = task["data"]
            last_result = d.get("LastTaskResult")
            try:
                last_hex = f"0x{int(last_result):08X}"
            except Exception:
                last_hex = str(last_result)

            c1.metric("Task state", str(d.get("State", "n/a")))
            c2.metric("LastTaskResult", f"{last_hex}")
            c3.metric("Missed runs", str(d.get("NumberOfMissedRuns", "n/a")))
            c4.metric(
                "Args",
                "… -Once"
                if "-Once" in str(d.get("Arguments", ""))
                else str(d.get("Arguments", ""))[:16] + "…",
            )
            st.caption(
                f"LastRunTime: {_fmt_task_dt(d.get('LastRunTime'))} | NextRunTime: {_fmt_task_dt(d.get('NextRunTime'))}"
            )
        else:
            st.warning(f"ScheduledTask read failed: {task.get('error')}")

        latest_log = _latest_matching(logs_dir, prefix="auto_loop_", suffix=".log")
        if latest_log:
            li = _mtime_info(latest_log)
            st.caption(
                f"Latest auto_loop log: {latest_log.name} | {li.get('mtime_utc')} | age {_fmt_age(li.get('age_s', 0))}"
            )
        else:
            st.caption("Latest auto_loop log: n/a")

        lock_path = logs_dir / "auto_loop.lock"
        lock = _mtime_info(lock_path) if lock_path.exists() else {"exists": False}
        if lock.get("exists"):
            st.warning(
                f"LOCK present: {lock_path} | {lock.get('mtime_utc')} | age {_fmt_age(lock.get('age_s', 0))}"
            )
        else:
            st.caption("LOCK: not present (OK)")

        csv_path = data_dir / "hg_5m_bars_ibkr.csv"
        csv_i = _mtime_info(csv_path) if csv_path.exists() else {"exists": False}
        if csv_i.get("exists"):
            st.caption(
                f"IBKR CSV: {csv_i.get('mtime_utc')} | age {_fmt_age(csv_i.get('age_s', 0))}"
            )
        else:
            st.caption("IBKR CSV: missing")

        conn = _read_ibkr_connection(data_dir)
        if conn.get("ok"):
            host = conn["host"]
            port = conn["port"]
            ok3 = _tcp_check(host, port)
            st.caption(f"TWS socket: {host}:{port} -> {'OK' if ok3 else 'DOWN'}")
        else:
            st.caption(f"TWS socket: config read failed ({conn.get('error')})")

        latest_oi = _latest_matching(data_dir, prefix="order_intents_", suffix=".jsonl")
        if latest_oi:
            oi = _mtime_info(latest_oi)
            st.caption(
                f"Latest order_intents: {latest_oi.name} | {oi.get('mtime_utc')} | age {_fmt_age(oi.get('age_s', 0))}"
            )
        else:
            st.caption("Latest order_intents: n/a")

    # -----------------------------
    # Stage 6 — Reconcile panel (open-orders snapshot)
    # -----------------------------
    with st.expander("Reconcile (Open Orders Snapshot)", expanded=True):
        live_snapshot = data_dir / "ibkr_open_orders_live.jsonl"
        m = _mtime_info(live_snapshot)
        st.caption(f"Live snapshot path: {live_snapshot}")
        if m.get("exists"):
            st.caption(
                f"Live snapshot mtime: {m.get('mtime_utc')} | age {_fmt_age(m.get('age_s', 0.0))}"
            )
        else:
            st.warning("Live snapshot missing (OK if not refreshed yet).")

        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            if st.button("Open data folder", key="recon_open_data"):
                safe_open_folder(data_dir)
        with c2:
            if st.button("Open logs folder", key="recon_open_logs"):
                safe_open_folder(logs_dir)
        with c3:
            if operator_mode:
                if st.button(
                    "Refresh Open Orders Snapshot", key="recon_refresh_snapshot"
                ):
                    res = _run_snapshot_refresh(root, live_snapshot, timeout_s=30.0)
                    st.session_state["recon_last_refresh"] = res
                    _st_rerun()
            else:
                st.caption("Operator mode OFF: refresh disabled.")

        last_refresh = st.session_state.get("recon_last_refresh")
        if isinstance(last_refresh, dict):
            st.markdown("**Last refresh result**")
            st.json(
                {
                    "ok": last_refresh.get("ok"),
                    "returncode": last_refresh.get("returncode"),
                    "out_path": last_refresh.get("out_path"),
                    "mtime_utc": last_refresh.get("mtime_utc"),
                }
            )
            if last_refresh.get("stdout"):
                st.code(_clip(str(last_refresh.get("stdout")), 2000), language="text")
            if last_refresh.get("stderr"):
                st.code(_clip(str(last_refresh.get("stderr")), 2000), language="text")

        # Best-effort: show latest executor reconcile event from latest orders_exec_events_*.jsonl
        latest_exec = _latest_matching(
            data_dir, prefix="orders_exec_events_", suffix=".jsonl"
        )
        if latest_exec and latest_exec.exists():
            st.markdown("**Latest executor events (orders_exec_events_*.jsonl)**")
            li = _mtime_info(latest_exec)
            st.caption(
                f"{latest_exec.name} | {li.get('mtime_utc')} | age {_fmt_age(li.get('age_s', 0.0))}"
            )

            last_skip = _scan_last_skip_reconcile(latest_exec)
            if isinstance(last_skip, dict):
                st.markdown("**Last ORDER_SKIP_RECONCILE**")
                st.json(
                    {
                        "reason": last_skip.get("reason"),
                        "ledger_key": last_skip.get("ledger_key"),
                        "details": last_skip.get("details", {}),
                    }
                )
            else:
                st.caption(
                    "No ORDER_SKIP_RECONCILE found in latest executor events (OK)."
                )
        else:
            st.caption("No orders_exec_events_*.jsonl found yet (run executor once).")

    operator_mode = bool(st.session_state.get("operator_mode", True))

    auto_refresh_enabled = False
    interval_s = 20
    follow_latest = True

    if operator_mode:
        c1, c2, c3 = st.columns([1, 1, 1])
        with c1:
            auto_refresh_enabled = st.checkbox(
                "Auto-refresh (operator mode)",
                value=False,
                key="runx_auto_refresh_enabled",
                help="Automatically refresh this tab every N seconds (safe-by-default).",
            )
        with c2:
            interval_s = int(
                st.number_input(
                    "Interval (seconds)",
                    min_value=5,
                    max_value=60,
                    value=20,
                    step=5,
                    key="runx_auto_refresh_interval_s",
                )
            )
        with c3:
            follow_latest = st.checkbox(
                "Follow latest run",
                value=True,
                key="runx_follow_latest_run",
                help="When auto-refresh is ON, automatically select the newest run.",
            )

        if auto_refresh_enabled:
            st.caption(f"Auto-refresh: ON ({int(interval_s)}s)")
    else:
        st.caption("Operator mode OFF: auto-refresh disabled.")

    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        if st.button("Refresh index", key="runexp_btn_refresh"):
            _st_rerun()
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

    if operator_mode and auto_refresh_enabled and follow_latest and run_labels:
        st.session_state["runexp_select_run"] = run_labels[0]

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
        "order_intents_path": str(selected.order_intents_path)
        if selected.order_intents_path
        else None,
        "payload_path": str(selected.payload_path) if selected.payload_path else None,
        "sendplan_path": str(selected.sendplan_path)
        if selected.sendplan_path
        else None,
    }
    st.json(meta)

    st.divider()

    # -----------------------------
    # Run report
    # -----------------------------
    st.markdown("### Run report (paper)")
    if selected.report_path and selected.report_path.exists():
        rep = read_json_safe(selected.report_path)
        if rep.get("_ok") is False:
            st.error(f"Failed to read report JSON: {rep.get('_error')}")
        else:
            st.json(rep)
    else:
        st.warning("No run_report_*_paper.json found for this run_id.")

    st.divider()

    # -----------------------------
    # Events
    # -----------------------------
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

            # Step 6: execution event counts (may be empty until Step 6 is implemented)
            st.write("Step 6 event counts (if present):")
            st.json(_summarize_step6_events(selected.events_path))

            # Step 5 Explain: pick a TICK
            ticks = _scan_tick_events(selected.events_path, max_ticks=250)
            if ticks:

                def score(t: Dict[str, Any]) -> int:
                    v = t.get("violations", [])
                    vcount = len(v) if isinstance(v, list) else 0
                    enforced = bool(_dg(t, "risk_envelope.enforced_no_trade", False))
                    return (10 if enforced else 0) + (1 if vcount > 0 else 0) + vcount

                best_i = 0
                best_s = -1
                for i, t in enumerate(ticks):
                    s = score(t)
                    if s > best_s:
                        best_s = s
                        best_i = i

                labels = [_tick_label(t) for t in ticks]
                sel_i = st.selectbox(
                    "Select TICK for Step 5 Explain",
                    list(range(len(labels))),
                    index=best_i,
                    format_func=lambda i: labels[i],
                    key=f"runx_tick_select_{selected.run_id}",
                )
                selected_tick = ticks[int(sel_i)]
                _render_step5_ma_explain(selected_tick, control_state)
            else:
                st.info("No TICK events found to explain (unexpected for paper loop).")

            with st.expander("Tail (last 25 lines)", expanded=False):
                st.code("\n".join(ev["tail_raw"]), language="json")
    else:
        st.warning("No events_run_*.jsonl found for this run_id.")

    st.divider()

    # -----------------------------
    # Orders (paper)
    # -----------------------------
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

    # -----------------------------
    # Stage 4.x — WA artifacts (existing)
    # -----------------------------
    st.markdown("### Stage 4.x — WA artifacts")

    # Order intents
    st.markdown("#### Order Intents (order_intents_*.jsonl)")
    if selected.order_intents_path and selected.order_intents_path.exists():
        oi = summarize_order_intents_jsonl(selected.order_intents_path, tail_n=15)
        if not oi.get("ok"):
            st.error(f"Failed to read order_intents JSONL: {oi.get('error')}")
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total", oi["total"])
            c2.metric("Allowed", oi["allowed"])
            c3.metric("None", oi["none"])
            c4.metric("Gated", oi["gated"])

            st.write("Kinds:")
            st.json(oi["kinds"])

            if oi.get("top_gate_reasons"):
                st.write("Top gate reasons:")
                st.json({k: v for (k, v) in oi["top_gate_reasons"]})

            if oi.get("truncated"):
                st.warning(
                    "order_intents file is large; UI scan is truncated for safety."
                )

            with st.expander("Tail (last 15 lines)", expanded=False):
                st.code("\n".join(oi["tail_raw"]), language="json")
    else:
        st.info("No order_intents_*.jsonl found for this run_id yet.")

    # Payload
    st.markdown("#### Payload (orders_payload_*.jsonl)")
    if selected.payload_path and selected.payload_path.exists():
        pl = summarize_payload_jsonl(selected.payload_path, tail_n=10)
        if not pl.get("ok"):
            st.error(f"Failed to read payload JSONL: {pl.get('error')}")
        else:
            c1, c2 = st.columns(2)
            c1.metric("Lines", pl["total"])
            c2.metric("Parse errors", pl["parse_errors"])
            st.write("Payload kinds:")
            st.json(pl["kinds"])

            with st.expander("Tail (last 10 lines)", expanded=False):
                st.code("\n".join(pl["tail_raw"]), language="json")
    else:
        st.info("No orders_payload_*.jsonl found for this run_id yet.")

    # Sendplan
    st.markdown("#### Sendplan (orders_sendplan_*.jsonl)")
    if selected.sendplan_path and selected.sendplan_path.exists():
        sp = summarize_sendplan_jsonl(selected.sendplan_path, tail_n=10)
        if not sp.get("ok"):
            st.error(f"Failed to read sendplan JSONL: {sp.get('error')}")
        else:
            c1, c2 = st.columns(2)
            c1.metric("Lines", sp["total"])
            c2.metric("Parse errors", sp["parse_errors"])
            st.write("Sendplan kinds:")
            st.json(sp["kinds"])

            with st.expander("Tail (last 10 lines)", expanded=False):
                st.code("\n".join(sp["tail_raw"]), language="json")
    else:
        st.info("No orders_sendplan_*.jsonl found for this run_id yet.")

    st.divider()

    # IBKR CSV status
    st.markdown("### IBKR data status (hg_5m_bars_ibkr.csv)")
    csv_path = data_dir / "hg_5m_bars_ibkr.csv"
    stat = ibkr_csv_status(csv_path)
    if not stat.get("ok"):
        st.info("No IBKR CSV found yet: args/data/hg_5m_bars_ibkr.csv")
    else:
        st.json(stat)

    # Auto-refresh tick (last)
    _auto_refresh_tick(auto_refresh_enabled and operator_mode, interval_s)
