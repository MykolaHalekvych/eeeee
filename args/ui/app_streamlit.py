# args/ui/app_streamlit.py
from __future__ import annotations

import datetime as _dt
import inspect
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

import streamlit as st
import yaml

from args.audit import event_store as _event_store
from args.ma.ma_runtime import eval_ma
from args.ma.policy_loader import load_policy
from args.ui.run_explorer_tab import render_run_explorer_tab


REPO_ROOT = Path(__file__).resolve().parents[2]
PY = ["py", "-3.11"]

POLICY_PATH = REPO_ROOT / "args" / "data" / "invariants_hg_v0.yaml"
EVENTS_PATH = REPO_ROOT / "args" / "data" / "events.jsonl"

UI_CSS = """
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}

/* Fix: expander icon font missing -> shows text like "keyboard_arrow_down" */
div[data-testid="stExpander"] span.material-icons,
div[data-testid="stExpander"] i.material-icons,
div[data-testid="stExpander"] [data-testid="stExpanderToggleIcon"] {
  display: none !important;
}
/* Fix: material-icons fallback text in JSON views (e.g., "keyboard_arrow_right") */
span.material-icons, i.material-icons {
  display: none !important;
}

/* App background */
.stApp {
  background:
    radial-gradient(1200px 600px at 15% 10%, rgba(62,84,138,0.35) 0%, rgba(10,15,26,0.0) 55%),
    radial-gradient(900px 450px at 85% 15%, rgba(170,80,80,0.22) 0%, rgba(10,15,26,0.0) 60%),
    linear-gradient(180deg, #0a0f1a 0%, #05070c 70%, #04050a 100%);
  color: #e5e7eb;
}

div.block-container {
  padding-top: 1.2rem;
  padding-bottom: 1.6rem;
  max-width: 1350px;
}

/* Sidebar */
div[data-testid="stSidebar"] > div:first-child {
  background: linear-gradient(180deg, rgba(10,15,26,0.95) 0%, rgba(7,10,17,0.98) 100%);
  border-right: 1px solid rgba(51,65,85,0.55);
}

/* Header band */
.args-hero {
  background: linear-gradient(90deg, rgba(15,23,42,0.55) 0%, rgba(15,23,42,0.25) 55%, rgba(15,23,42,0.55) 100%);
  border: 1px solid rgba(51,65,85,0.55);
  border-radius: 16px;
  padding: 16px 18px;
  box-shadow: 0 14px 35px rgba(0,0,0,0.35);
}
.args-title {
  font-size: 38px;
  font-weight: 800;
  letter-spacing: 0.02em;
  margin: 0;
  line-height: 1.0;
}
.args-subtitle {
  font-size: 14px;
  color: rgba(203,213,225,0.85);
  margin-top: 6px;
  letter-spacing: 0.06em;
}

/* Metric strip */
.metric-strip {
  margin-top: 12px;
  background: rgba(2,6,23,0.55);
  border: 1px solid rgba(51,65,85,0.55);
  border-radius: 14px;
  padding: 10px 12px;
  display: grid;
  grid-template-columns: 1fr 1.2fr 1fr;
  gap: 10px;
}
.metric-pill {
  background: rgba(15,23,42,0.55);
  border: 1px solid rgba(51,65,85,0.55);
  border-radius: 12px;
  padding: 10px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.metric-label {
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: rgba(148,163,184,0.95);
}
.metric-value {
  font-size: 14px;
  font-weight: 800;
  letter-spacing: 0.02em;
}

/* Badges */
.badge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 999px;
  border: 1px solid rgba(51,65,85,0.65);
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-size: 12px;
}
.badge.allow {
  background: rgba(16,185,129,0.12);
  color: rgba(52,211,153,1);
  border-color: rgba(16,185,129,0.35);
}
.badge.reduce {
  background: rgba(245,158,11,0.12);
  color: rgba(251,191,36,1);
  border-color: rgba(245,158,11,0.35);
}
.badge.no_trade {
  background: rgba(239,68,68,0.12);
  color: rgba(248,113,113,1);
  border-color: rgba(239,68,68,0.35);
}

/* Cards */
.card {
  background: rgba(15,23,42,0.48);
  border: 1px solid rgba(51,65,85,0.55);
  border-radius: 16px;
  padding: 14px 16px;
  box-shadow: 0 12px 26px rgba(0,0,0,0.32);
  height: 100%;
}
.card-title {
  font-size: 13px;
  font-weight: 900;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  color: rgba(226,232,240,0.88);
  margin-bottom: 10px;
}

/* Key-Value grid */
.kv {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 8px 12px;
}
.k {
  color: rgba(148,163,184,0.95);
  font-size: 13px;
}
.v {
  text-align: right;
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 0.02em;
}
.v.emph-red { color: rgba(248,113,113,1); }
.v.emph-yellow { color: rgba(251,191,36,1); }
.v.emph-green { color: rgba(52,211,153,1); }
.v.emph-blue { color: rgba(147,197,253,1); }

/* Violation rows */
.violation-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 12px;
  border: 1px solid rgba(51,65,85,0.45);
  background: rgba(2,6,23,0.35);
  margin-bottom: 8px;
}
.vi-dot {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  margin-top: 4px;
  background: rgba(248,113,113,1);
  box-shadow: 0 0 0 3px rgba(239,68,68,0.15);
}
.vi-text {
  font-size: 13px;
  color: rgba(226,232,240,0.92);
}

/* Event table */
.event-wrap {
  margin-top: 12px;
  background: rgba(15,23,42,0.48);
  border: 1px solid rgba(51,65,85,0.55);
  border-radius: 16px;
  padding: 12px 14px;
  box-shadow: 0 12px 26px rgba(0,0,0,0.32);
}
.event-title {
  font-size: 13px;
  font-weight: 900;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  color: rgba(226,232,240,0.88);
  margin-bottom: 10px;
}
.event-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.event-table th {
  text-align: left;
  color: rgba(148,163,184,0.95);
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-size: 12px;
  padding: 8px 10px;
  border-bottom: 1px solid rgba(51,65,85,0.55);
}
.event-table td {
  padding: 9px 10px;
  border-bottom: 1px solid rgba(51,65,85,0.25);
  color: rgba(226,232,240,0.92);
}
.event-decision {
  font-weight: 900;
  letter-spacing: 0.06em;
}
.dec-allow { color: rgba(52,211,153,1); }
.dec-reduce { color: rgba(251,191,36,1); }
.dec-no { color: rgba(248,113,113,1); }

/* Buttons */
div.stButton > button {
  border-radius: 12px !important;
  border: 1px solid rgba(71,85,105,0.65) !important;
  background: rgba(15,23,42,0.55) !important;
  color: rgba(226,232,240,0.95) !important;
  font-weight: 800 !important;
  letter-spacing: 0.08em !important;
  text-transform: uppercase !important;
  padding: 10px 14px !important;
}
</style>
"""


# -----------------------------
# Helpers (generic)
# -----------------------------
def _html_escape(x: Any) -> str:
    s = "" if x is None else str(x)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_cmd(args: List[str], cwd: Path = REPO_ROOT) -> Tuple[int, str]:
    try:
        p = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            shell=False,
        )
        out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        return p.returncode, out.strip()
    except Exception as e:
        return 99, f"{type(e).__name__}: {e}"


def open_path(path: Path) -> Tuple[int, str]:
    try:
        p = Path(path)
        if not p.exists():
            return 97, f"Path does not exist: {p}"
        args = ["cmd", "/c", "start", "", str(p)]
        return run_cmd(args, cwd=REPO_ROOT)
    except Exception as e:
        return 99, f"{type(e).__name__}: {e}"


def tail_lines(path: Path, n: int) -> str:
    if not path.exists():
        return f"(missing) {path}"
    try:
        data = path.read_bytes()
        if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
            text = data.decode("utf-16", errors="replace")
        elif data.startswith(b"\xef\xbb\xbf"):
            text = data.decode("utf-8-sig", errors="replace")
        else:
            text = data.decode("utf-8", errors="replace")
        if "\x00" in text[:200]:
            text = data.decode("utf-16", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def list_logs() -> List[Path]:
    d = REPO_ROOT / "args" / "logs"
    if not d.exists():
        return []
    return sorted([p for p in d.iterdir() if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)


def git_porcelain() -> Tuple[int, str]:
    return run_cmd(["git", "status", "--porcelain"], cwd=REPO_ROOT)


# -----------------------------
# Control Panel actions
# -----------------------------
def header_status() -> None:
    st.title("ARGS Core v1 — Control Panel")
    st.caption(f"Repo: {REPO_ROOT}")

    code, out = run_cmd(["git", "--version"], cwd=REPO_ROOT)
    if code == 0:
        st.success(out)
    else:
        st.warning("Git not available in PATH for this session.")

    code, out = git_porcelain()
    if code == 0 and out.strip() == "":
        st.success("Git status: working tree clean")
    elif code == 0:
        st.warning("Git status: changes present")
        st.text(out)
    else:
        st.info("Git status: unavailable")


def run_sanity_suite() -> Dict[str, Tuple[int, str]]:
    suite = {
        "demo_regression": PY + ["-m", "args.demo.demo_regression"],
        "demo_policy_diff": PY + ["-m", "args.demo.demo_policy_diff"],
        "demo_replay": PY + ["-m", "args.demo.demo_replay"],
        "demo_meta_audit": PY + ["-m", "args.demo.demo_meta_audit"],
    }
    results: Dict[str, Tuple[int, str]] = {}
    for name, cmd in suite.items():
        code, out = run_cmd(cmd)
        results[name] = (code, out)
    return results


def run_negative_test() -> Tuple[int, str]:
    return run_cmd(PY + ["-m", "args.demo.demo_meta_audit_negative"])


def run_checkpoint(tag: str) -> Tuple[int, str]:
    script = REPO_ROOT / "scripts" / "checkpoint.ps1"
    if not script.exists():
        return 98, f"Missing script: {script}"
    cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Tag",
        tag,
    ]
    return run_cmd(cmd, cwd=REPO_ROOT)


# -----------------------------
# Dashboard helpers
# -----------------------------
def _badge_html(text: str) -> str:
    t = (text or "UNKNOWN").upper()
    key = t.replace("-", "_").replace(" ", "_")
    cls = "no_trade"
    if key == "ALLOW":
        cls = "allow"
    elif key in ("REDUCE", "RESTRICTED"):
        cls = "reduce"
    return f'<span class="badge {cls}">{_html_escape(t)}</span>'


def _derive_system_mode(decision: str) -> str:
    d = (decision or "UNKNOWN").upper().replace("-", "_")
    if d == "ALLOW":
        return "ALLOW"
    if d == "REDUCE":
        return "RESTRICTED"
    return "NO-TRADE"


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _load_events_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    out.sort(key=lambda e: str(e.get("ts_utc") or e.get("ts") or e.get("ts_utc") or ""), reverse=True)
    return out


def _normalize_eval_result(res: Any) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    decision = "UNKNOWN"
    violations: List[Dict[str, Any]] = []
    risk_envelope: Dict[str, Any] = {}

    if isinstance(res, dict):
        decision = str(res.get("ma_decision") or res.get("decision") or res.get("result") or "UNKNOWN").upper()
        vv = res.get("violations") or res.get("rules") or []
        if isinstance(vv, list):
            violations = [v for v in vv if isinstance(v, dict)]
        re = res.get("risk_envelope") or res.get("envelope") or {}
        if isinstance(re, dict):
            risk_envelope = re
        return decision, violations, risk_envelope

    for attr in ("ma_decision", "decision"):
        if hasattr(res, attr):
            try:
                decision = str(getattr(res, attr) or "UNKNOWN").upper()
                break
            except Exception:
                pass

    if hasattr(res, "violations"):
        try:
            vv = getattr(res, "violations") or []
            if isinstance(vv, list):
                violations = [v for v in vv if isinstance(v, dict)]
        except Exception:
            violations = []

    for attr in ("risk_envelope", "envelope"):
        if hasattr(res, attr):
            try:
                re = getattr(res, attr) or {}
                if isinstance(re, dict):
                    risk_envelope = re
                    break
            except Exception:
                pass

    return decision, violations, risk_envelope


def _append_event_safe(event: Dict[str, Any]) -> Tuple[int, str]:
    fn = getattr(_event_store, "append_event", None)
    if callable(fn):
        try:
            sig = inspect.signature(fn)
            n = len(sig.parameters)
            if n == 1:
                fn(event)
                return 0, "append_event(event)"
            if n == 2:
                fn(EVENTS_PATH, event)
                return 0, "append_event(path,event)"
            fn(event)
            return 0, "append_event(?)"
        except Exception as e:
            fallback_err = f"{type(e).__name__}: {e}"
    else:
        fallback_err = "append_event not found"

    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return 0, "direct_jsonl_append"
    except Exception as e:
        return 99, f"direct_append_failed; {fallback_err}; {type(e).__name__}: {e}"


def _demo_ctx(policy_meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ts_utc": _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "instrument": policy_meta.get("instrument", "HG"),
        "timeframe": policy_meta.get("timeframe", "5m"),
        "environment": policy_meta.get("environment", "IBKR_PAPER_LABEL"),
        "ctx_snapshot": {
            "env": {"session": "RTH"},
            "data": {"qc": "OK", "missing_bars": 0, "stale_quotes": False, "timestamp_drift_ms": 0},
            "risk": {"margin_usage": 0.41},
            "state": {"regime": "TREND", "confidence": 0.62, "liquidity": "NORMAL", "tail_risk": "UNKNOWN"},
        },
    }


def _extract_snapshot(event: Dict[str, Any]) -> Tuple[float | None, float | None, str, str, float | None]:
    ctx = event.get("ctx_snapshot")
    ctx = ctx if isinstance(ctx, dict) else {}

    risk = ctx.get("risk") if isinstance(ctx.get("risk"), dict) else {}
    state = ctx.get("state") if isinstance(ctx.get("state"), dict) else {}

    mu = ctx.get("margin_usage")
    if not isinstance(mu, (int, float)):
        mu = risk.get("margin_usage") if isinstance(risk.get("margin_usage"), (int, float)) else None

    ml = ctx.get("margin_limit")
    if not isinstance(ml, (int, float)):
        ml = ctx.get("margin_max") if isinstance(ctx.get("margin_max"), (int, float)) else None

    env = event.get("risk_envelope") if isinstance(event.get("risk_envelope"), dict) else {}
    limits = env.get("limits") if isinstance(env.get("limits"), dict) else {}
    if ml is None:
        ml = limits.get("margin_max") if isinstance(limits.get("margin_max"), (int, float)) else None
    if ml is None:
        ml = limits.get("margin_usage_limit") if isinstance(limits.get("margin_usage_limit"), (int, float)) else None

    tail = (
        ctx.get("tail_risk_state")
        or ctx.get("tail_risk")
        or state.get("tail_risk")
        or state.get("tail_risk_state")
        or "UNKNOWN"
    )
    liq = ctx.get("liquidity") or state.get("liquidity") or "NORMAL"

    conf = ctx.get("regime_confidence")
    if not isinstance(conf, (int, float)):
        conf = ctx.get("confidence") if isinstance(ctx.get("confidence"), (int, float)) else None
    if conf is None:
        conf = state.get("confidence") if isinstance(state.get("confidence"), (int, float)) else None

    return (
        float(mu) if isinstance(mu, (int, float)) else None,
        float(ml) if isinstance(ml, (int, float)) else None,
        str(tail).upper(),
        str(liq).upper(),
        float(conf) if isinstance(conf, (int, float)) else None,
    )


def _infer_active_blocks(violations: Any) -> List[str]:
    blocks = set()
    if isinstance(violations, list):
        for v in violations:
            if not isinstance(v, dict):
                continue
            b = str(v.get("block") or "").lower()
            rid = str(v.get("rule_id") or v.get("id") or "").lower()
            if "margin" in b or "margin" in rid:
                blocks.add("Margin Gates")
            if "tail" in b or "tail" in rid:
                blocks.add("Tail Risk Gates")
    return sorted(blocks)


def _pick_top_reason(event: Dict[str, Any], mu: float | None, ml: float | None, tail: str) -> str:
    vv = event.get("violations")
    if isinstance(vv, list) and vv:
        v0 = vv[0] if isinstance(vv[0], dict) else {}
        return str(v0.get("reason") or v0.get("message") or v0.get("rule_id") or "Violation")
    if mu is not None and ml is not None and mu > ml:
        return "Margin usage above safe threshold."
    if tail == "UNKNOWN":
        return "Tail risk is UNKNOWN/UNRESOLVED."
    return "No violations"


def _decision_to_css(decision: str) -> str:
    d = (decision or "UNKNOWN").upper().replace("-", "_")
    if d == "ALLOW":
        return "dec-allow"
    if d == "REDUCE":
        return "dec-reduce"
    return "dec-no"


# -----------------------------
# ARGS Dashboard
# -----------------------------
def render_args_dashboard() -> None:
    policy_meta = _read_yaml(POLICY_PATH)
    events = _load_events_jsonl(EVENTS_PATH)
    last = events[0] if events else {}

    policy_name = str(last.get("policy_name") or last.get("policy_file") or policy_meta.get("policy_name") or "HG_MA_v0")
    schema_version = str(last.get("schema_version") or policy_meta.get("schema_version") or policy_meta.get("schema") or "0.1")

    decision = str(last.get("ma_decision") or last.get("decision") or "NO_TRADE").upper().replace("-", "_")
    system_mode = _derive_system_mode(decision)

    mu, ml, tail, liq, conf = _extract_snapshot(last)
    conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "0.62"

    decision_display = decision.replace("_", "-")
    if tail == "UNKNOWN" and decision in ("NO_TRADE", "UNKNOWN", "EXIT"):
        decision_display = "UNKNOWN → NO-TRADE"

    mu_pct = f"{mu * 100:.0f}%" if isinstance(mu, (int, float)) else "41%"
    ml_pct = f"{ml * 100:.0f}%" if isinstance(ml, (int, float)) else "35%"

    mu_emph = "emph-yellow"
    if isinstance(mu, (int, float)) and isinstance(ml, (int, float)):
        mu_emph = "emph-red" if mu > ml else "emph-green"

    tail_emph = "emph-yellow" if tail == "UNKNOWN" else "emph-blue"
    liq_emph = "emph-blue" if liq == "NORMAL" else "emph-yellow"

    st.markdown(
        '<div class="args-hero">'
        '<div class="args-title">ARGS</div>'
        '<div class="args-subtitle">Autonomous Risk Governance System</div>'
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown(
        (
            '<div class="metric-strip">'
            '<div class="metric-pill"><div class="metric-label">SYSTEM MODE</div>'
            f'<div class="metric-value">{_badge_html(system_mode)}</div></div>'
            '<div class="metric-pill"><div class="metric-label">MA DECISION</div>'
            f'<div class="metric-value">{_badge_html(decision_display)}</div></div>'
            '<div class="metric-pill"><div class="metric-label">POLICY</div>'
            f'<div class="metric-value" style="font-weight:900;letter-spacing:0.06em;">'
            f'{_html_escape(policy_name)} | SCHEMA {_html_escape(schema_version)}'
            "</div></div></div>"
        ),
        unsafe_allow_html=True,
    )

    now_local = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events_total = len(events) if isinstance(events, list) else 0
    last_ts = str(last.get("ts_utc") or last.get("ts") or "—")
    last_dec = str(last.get("ma_decision") or "UNKNOWN").upper().replace("_", "-")

    mini_html = (
        '<div class="metric-strip" style="grid-template-columns: 1fr 1fr 1fr 1fr;">'
        '<div class="metric-pill"><div class="metric-label">Last refresh</div>'
        f'<div class="metric-value">{_html_escape(now_local)}</div></div>'
        '<div class="metric-pill"><div class="metric-label">Events total</div>'
        f'<div class="metric-value">{events_total}</div></div>'
        '<div class="metric-pill"><div class="metric-label">Last event</div>'
        f'<div class="metric-value">{_html_escape(last_ts)}</div></div>'
        '<div class="metric-pill"><div class="metric-label">Last decision</div>'
        f'<div class="metric-value">{_html_escape(last_dec)}</div></div>'
        '</div>'
    )
    st.markdown(mini_html, unsafe_allow_html=True)

    b1, b2, b3 = st.columns(3)
    with b1:
        if st.button("Open policy YAML", key="dash_open_policy"):
            open_path(POLICY_PATH)
    with b2:
        if st.button("Open events.jsonl", key="dash_open_events"):
            open_path(EVENTS_PATH)
    with b3:
        if st.button("Refresh dashboard", key="dash_refresh"):
            st.rerun()

    vv = last.get("violations") if isinstance(last.get("violations"), list) else []
    if not isinstance(vv, list):
        vv = []

    active_blocks = _infer_active_blocks(vv)
    if not active_blocks:
        active_blocks = ["Margin Gates", "Tail Risk Gates"]

    blocks_rows = ""
    for b in active_blocks:
        blocks_rows += (
            '<div style="display:flex;align-items:center;justify-content:space-between;'
            'padding:8px 10px;border-radius:12px;'
            'border:1px solid rgba(51,65,85,0.35);'
            'background:rgba(2,6,23,0.25);margin-bottom:8px;">'
            f'<div style="font-weight:800;color:rgba(226,232,240,0.92);">{_html_escape(b)}: '
            '<span style="color:rgba(248,113,113,1);font-weight:900;">ACTIVE</span></div>'
            '<div style="width:16px;height:16px;border-radius:999px;background:rgba(239,68,68,0.18);'
            'border:1px solid rgba(239,68,68,0.45);display:flex;align-items:center;justify-content:center;'
            'color:rgba(248,113,113,1);font-weight:900;">!</div>'
            "</div>"
        )

    v_lines: List[str] = []
    if mu is not None and ml is not None:
        v_lines.append(f"margin_usage_gate: {mu:.2f} > {ml:.2f} LIMIT")
    else:
        v_lines.append("margin_usage_gate: 0.41 > 0.35 LIMIT")

    if tail == "UNKNOWN":
        v_lines.append("tail_unknown_gate: Tail Risk = UNKNOWN")
    else:
        v_lines.append(f"tail_unknown_gate: Tail Risk = {tail}")

    v_rows = ""
    for line in v_lines[:2]:
        v_rows += (
            '<div class="violation-row">'
            '<div class="vi-dot"></div>'
            f'<div class="vi-text">{_html_escape(line)}</div>'
            "</div>"
        )

    enforced_html = (
        f'<div style="margin-top:6px;font-weight:900;letter-spacing:0.10em;'
        f'color:rgba(251,191,36,1);text-transform:uppercase;">ENFORCED: {_html_escape(system_mode)}</div>'
    )

    snapshot_html = (
        '<div class="kv">'
        '<div class="k">Margin Usage</div>'
        f'<div class="v {("emph-red" if (isinstance(mu, (int, float)) and isinstance(ml, (int, float)) and mu > ml) else "emph-green")}">'
        f'{_html_escape(mu_pct)} (LIMIT {_html_escape(ml_pct)})</div>'
        '<div class="k">Tail Risk State</div>'
        f'<div class="v {("emph-yellow" if tail == "UNKNOWN" else "emph-blue")}">{_html_escape(tail)}</div>'
        '<div class="k">Liquidity</div>'
        f'<div class="v {("emph-blue" if liq == "NORMAL" else "emph-yellow")}">{_html_escape(liq)}</div>'
        '<div class="k">Regime Confidence</div>'
        f'<div class="v emph-blue">{_html_escape(conf_str)}</div>'
        "</div>"
    )

    c1, c2, c3 = st.columns(3, gap="medium")
    with c1:
        st.markdown(f'<div class="card"><div class="card-title">ACTIVE RISK BLOCKS</div>{blocks_rows}</div>', unsafe_allow_html=True)
    with c2:
        st.markdown(f'<div class="card"><div class="card-title">TOP VIOLATIONS</div>{v_rows}{enforced_html}</div>', unsafe_allow_html=True)
    with c3:
        st.markdown(f'<div class="card"><div class="card-title">RISK SNAPSHOT</div>{snapshot_html}</div>', unsafe_allow_html=True)

    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
    cc1, cc2, cc3 = st.columns([1, 1, 1])
    with cc2:
        if st.button("EVAL MA", use_container_width=True, key="dash_eval_ma"):
            policy = load_policy(POLICY_PATH)
            ctx = _demo_ctx(policy_meta)
            res = eval_ma(policy, ctx)

            decision2, vv2, risk_env2 = _normalize_eval_result(res)
            system_mode2 = _derive_system_mode(decision2)

            ev = {
                "event_id": uuid.uuid4().hex,
                "ts_utc": _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                "policy_name": str(policy_meta.get("policy_name") or policy_name),
                "schema_version": str(policy_meta.get("schema_version") or schema_version),
                "instrument": ctx.get("instrument"),
                "timeframe": ctx.get("timeframe"),
                "environment": ctx.get("environment"),
                "ma_decision": decision2,
                "system_mode": system_mode2,
                "violations": vv2,
                "risk_envelope": risk_env2,
                "ctx_snapshot": ctx.get("ctx_snapshot", {}),
            }

            mu2, ml2, tail2, _, _ = _extract_snapshot(ev)
            ev["top_reason"] = _pick_top_reason(ev, mu2, ml2, tail2)

            code, out = _append_event_safe(ev)
            if code != 0:
                st.error(f"append_event failed: {out}")
            st.rerun()

    rows = events[:8]
    if rows:
        trs = ""
        for e in rows:
            ts = str(e.get("ts_utc") or e.get("ts") or "")
            tshort = ts.split("T")[-1].replace("Z", "")[:8] if "T" in ts else ts[:8]

            d = str(e.get("ma_decision") or "UNKNOWN").upper().replace("-", "_")
            d_show = d.replace("_", "-")

            mu_e, ml_e, tail_e, _, _ = _extract_snapshot(e)
            reason = str(e.get("top_reason") or _pick_top_reason(e, mu_e, ml_e, tail_e))
            pol = str(e.get("policy_name") or e.get("policy_file") or "")

            dcls = _decision_to_css(d)
            trs += (
                "<tr>"
                f"<td>{_html_escape(tshort)}</td>"
                f"<td class='event-decision {dcls}'>{_html_escape(d_show)}</td>"
                f"<td>{_html_escape(reason)}</td>"
                f"<td>{_html_escape(pol)}</td>"
                "</tr>"
            )

        table_html = (
            '<div class="event-wrap">'
            '<div class="event-title">EVENT STREAM</div>'
            '<table class="event-table">'
            "<thead><tr>"
            '<th style="width:120px;">Time</th>'
            '<th style="width:140px;">Decision</th>'
            "<th>Reason</th>"
            '<th style="width:260px;">Policy</th>'
            "</tr></thead>"
            f"<tbody>{trs}</tbody>"
            "</table>"
            "</div>"
        )
        st.markdown(table_html, unsafe_allow_html=True)


# -----------------------------
# Events (structured)
# -----------------------------
def render_events_view() -> None:
    st.subheader("Event Stream (structured)")

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        if st.button("Open events.jsonl", key="ev_open_events_jsonl"):
            open_path(EVENTS_PATH)
    with c2:
        if st.button("Open data folder", key="ev_open_data_folder"):
            open_path(EVENTS_PATH.parent)

    events = _load_events_jsonl(EVENTS_PATH)
    if not events:
        st.info("No events found in args/data/events.jsonl")
        return

    f1, f2, f3 = st.columns([1, 2, 1])
    with f1:
        decision_filter = st.selectbox(
            "Decision",
            ["All", "ALLOW", "REDUCE", "NO_TRADE", "UNKNOWN", "EXIT"],
            index=0,
            key="ev_decision_filter",
        )
    with f2:
        q = st.text_input(
            "Search (reason/policy/instrument)",
            value="",
            key="ev_search",
        ).strip().lower()
    with f3:
        limit = st.number_input(
            "Rows",
            min_value=10,
            max_value=500,
            value=80,
            step=10,
            key="ev_rows",
        )

    filtered: List[Dict[str, Any]] = []
    for e in events:
        d = str(e.get("ma_decision") or "UNKNOWN").upper().replace("-", "_")
        if decision_filter != "All" and d != decision_filter:
            continue

        mu, ml, tail, _, _ = _extract_snapshot(e)
        reason = str(e.get("top_reason") or _pick_top_reason(e, mu, ml, tail))
        pol = str(e.get("policy_name") or e.get("policy_file") or "")
        inst = str(e.get("instrument") or "")

        if q:
            blob = f"{reason} {pol} {inst}".lower()
            if q not in blob:
                continue

        ee = dict(e)
        ee["_reason"] = reason
        ee["_policy"] = pol
        ee["_decision_norm"] = d
        filtered.append(ee)

    if not filtered:
        st.warning("No events match the current filters.")
        return

    rows = filtered[: int(limit)]

    trs = ""
    for e in rows:
        ts = str(e.get("ts_utc") or e.get("ts") or "")
        tshort = ts.split("T")[-1].replace("Z", "")[:8] if "T" in ts else ts[:8]

        d = str(e.get("_decision_norm") or "UNKNOWN")
        d_show = d.replace("_", "-")
        dcls = _decision_to_css(d)

        reason = str(e.get("_reason") or "")
        pol = str(e.get("_policy") or "")

        trs += (
            "<tr>"
            f"<td>{_html_escape(tshort)}</td>"
            f"<td class='event-decision {dcls}'>{_html_escape(d_show)}</td>"
            f"<td>{_html_escape(reason)}</td>"
            f"<td>{_html_escape(pol)}</td>"
            "</tr>"
        )

    table_html = (
        '<div class="event-wrap">'
        '<div class="event-title">EVENT STREAM</div>'
        '<table class="event-table">'
        "<thead><tr>"
        '<th style="width:120px;">Time</th>'
        '<th style="width:140px;">Decision</th>'
        "<th>Reason</th>"
        '<th style="width:260px;">Policy</th>'
        "</tr></thead>"
        f"<tbody>{trs}</tbody>"
        "</table>"
        "</div>"
    )
    st.markdown(table_html, unsafe_allow_html=True)

    st.markdown("### Event details")

    def _label(e: Dict[str, Any]) -> str:
        ts = str(e.get("ts_utc") or "")
        d = str(e.get("_decision_norm") or "UNKNOWN").replace("_", "-")
        r = str(e.get("_reason") or "")
        return f"{ts} | {d} | {r[:60]}"

    pick = st.selectbox("Select event", rows, format_func=_label, index=0, key="ev_select_event")
    st.code(json.dumps(pick, ensure_ascii=False, indent=2), language="json")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    st.set_page_config(page_title="ARGS", layout="wide")
    st.markdown(UI_CSS, unsafe_allow_html=True)

    st.sidebar.markdown("### Mode")
    operator_mode = st.sidebar.checkbox(
        "Operator mode (safe)",
        value=True,
        help="ON = operator-safe (hides dev/destructive actions). OFF = Dev mode.",
        key="mode_operator",
    )

    # Make operator_mode visible to all tabs (Run Explorer uses st.session_state['operator_mode'])
    st.session_state["operator_mode"] = bool(operator_mode)

    tab_args, tab_runexp, tab_dashboard, tab_logs, tab_events, tab_about = st.tabs(
        ["ARGS Dashboard", "Run Explorer", "Control Panel", "Logs", "Events", "About"]
    )

    with tab_args:
        render_args_dashboard()

    with tab_runexp:
        render_run_explorer_tab()

    with tab_dashboard:
        header_status()

        cA, cB = st.columns(2)
        with cA:
            if st.button("Refresh UI state", key="cp_refresh_ui"):
                st.rerun()
        with cB:
            if st.button("Clear results", key="cp_clear_results"):
                for k in ("sanity_results", "sanity_ts", "neg_result", "neg_ts", "chk_result", "chk_ts"):
                    if k in st.session_state:
                        del st.session_state[k]
                st.rerun()

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Actions")

            if st.button("Run sanity suite (regression/diff/replay/meta_audit)", key="cp_run_sanity"):
                st.session_state["sanity_ts"] = _dt.datetime.now().isoformat(timespec="seconds")
                st.session_state["sanity_results"] = run_sanity_suite()

            if operator_mode:
                st.info("Operator mode: Negative test and Checkpoint are hidden.")
            else:
                if st.button("Run Meta Audit negative test (UNKNOWN→ALLOW, expect FAIL)", key="cp_run_negative"):
                    st.session_state["neg_ts"] = _dt.datetime.now().isoformat(timespec="seconds")
                    st.session_state["neg_result"] = run_negative_test()

                tag = st.text_input("Checkpoint tag", value="ui_polish", key="cp_checkpoint_tag")
                if st.button("Run checkpoint.ps1", key="cp_run_checkpoint"):
                    st.session_state["chk_ts"] = _dt.datetime.now().isoformat(timespec="seconds")
                    st.session_state["chk_result"] = run_checkpoint(tag)

            st.subheader("Open folders")
            if st.button("Open repo folder", key="cp_open_repo"):
                code, out = open_path(REPO_ROOT)
                st.text(out or f"exit_code={code}")

            if st.button("Open logs folder", key="cp_open_logs"):
                code, out = open_path(REPO_ROOT / "args" / "logs")
                st.text(out or f"exit_code={code}")

            if st.button("Open data folder", key="cp_open_data_folder"):
                code, out = open_path(REPO_ROOT / "args" / "data")
                st.text(out or f"exit_code={code}")

        with col2:
            st.subheader("Results")

            if "sanity_results" in st.session_state:
                st.write(f"Sanity run: {st.session_state.get('sanity_ts', '')}")
                for name, (code, out) in st.session_state["sanity_results"].items():
                    if code == 0:
                        st.success(f"{name}: OK")
                    else:
                        st.error(f"{name}: FAIL (code={code})")
                    with st.expander(f"Output: {name}", expanded=False):
                        st.text(out or "(no output)")

            if not operator_mode:
                if "neg_result" in st.session_state:
                    code, out = st.session_state["neg_result"]
                    st.write(f"Negative test: {st.session_state.get('neg_ts', '')}")
                    if code == 2:
                        st.success("Negative test: FAIL detected as expected (exit_code=2).")
                    elif code == 0:
                        st.error("Negative test: expected exit_code=2 but got 0.")
                    else:
                        st.error(f"Negative test: error (code={code}).")
                    st.text(out or "(no output)")

                if "chk_result" in st.session_state:
                    code, out = st.session_state["chk_result"]
                    st.write(f"Checkpoint: {st.session_state.get('chk_ts', '')}")
                    if code == 0:
                        st.success("Checkpoint OK")
                    else:
                        st.error(f"Checkpoint FAIL (code={code})")
                    st.text(out or "(no output)")
            else:
                if ("neg_result" in st.session_state) or ("chk_result" in st.session_state):
                    st.caption("Dev outputs are hidden in Operator mode.")

    with tab_logs:
        st.subheader("Logs & Evidence")

        prefix = st.text_input("Filter logs by filename contains", value="", key="logs_filter")
        logs = list_logs()
        if prefix.strip():
            logs = [p for p in logs if prefix.strip().lower() in p.name.lower()]

        if logs:
            pick = st.selectbox("Select log file", logs, format_func=lambda p: p.name, key="logs_select")
            n = st.slider("Tail lines", min_value=20, max_value=400, value=120, step=20, key="logs_tail")
            st.text(tail_lines(pick, n))
        else:
            st.info("No logs found in args/logs (or filtered to none).")

    with tab_events:
        render_events_view()

    with tab_about:
        st.subheader("About / Commands")
        st.markdown(
            """
- Sanity suite: runs regression / policy diff / replay / meta-audit
- Operator mode hides negative test + checkpoint actions
- Run Explorer tab shows per-run artifacts (events_run_*, orders_paper_*, run_report_*)

Run UI:
- `py -3.11 -m streamlit run .\\args\\ui\\app_streamlit.py`
            """
        )


if __name__ == "__main__":
    main()

