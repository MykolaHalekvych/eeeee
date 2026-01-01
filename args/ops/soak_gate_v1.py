# args/ops/soak_gate_v1.py
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


SCHEMA_VERSION = "soak_gate_v1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_text(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _read_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _mtime_age_s(p: Path, now: datetime) -> Optional[float]:
    try:
        st = p.stat()
        m = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        return max(0.0, (now - m).total_seconds())
    except Exception:
        return None


def _exists(p: Path) -> bool:
    try:
        return p.exists()
    except Exception:
        return False


def _run_py(args: list[str], timeout_s: int) -> Tuple[int, str]:
    """
    Runs a python module command. Returns (returncode, stdout+stderr combined).
    Must never print anything directly.
    """
    try:
        cp = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        out = (cp.stdout or "").strip()
        return cp.returncode, out
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as e:
        return 125, f"EXC:{type(e).__name__}:{e}"


@dataclass
class GateResult:
    ok: bool
    level: str  # PASS/WARN/FAIL
    exit_code: int  # 0 PASS, 1 WARN, 2 FAIL
    reason: str
    details: Dict[str, Any]


def _decide(failures: int, warnings: int, reason: str, details: Dict[str, Any]) -> GateResult:
    if failures > 0:
        return GateResult(ok=False, level="FAIL", exit_code=2, reason=reason, details=details)
    if warnings > 0:
        return GateResult(ok=True, level="WARN", exit_code=1, reason=reason, details=details)
    return GateResult(ok=True, level="PASS", exit_code=0, reason=reason, details=details)


def main() -> int:
    ap = argparse.ArgumentParser(description="ARGS Stage7 Hard-Soak Gate (file-based health checks)")
    ap.add_argument("--max-heartbeat-age-s", type=int, default=900)
    ap.add_argument("--max-ops-health-age-s", type=int, default=900)
    ap.add_argument("--max-ops-events-age-s", type=int, default=900)
    ap.add_argument("--max-latest-run-age-s", type=int, default=1800)
    ap.add_argument("--require-latest-paths", action="store_true")
    ap.add_argument("--allow-stop-flag", action="store_true")
    ap.add_argument("--halt-on-fail", action="store_true", help="On FAIL: write stop.flag (safe halt)")
    ap.add_argument("--stop-flag-path", type=str, default="")
    ap.add_argument("--verify-registry-audit", action="store_true")
    ap.add_argument("--verify-evidence-latest", action="store_true")
    ap.add_argument("--py-timeout-s", type=int, default=30)
    ap.add_argument("--write-out", type=str, default="", help="Write status JSON to this path (default args/data/soak_status.json)")
    args = ap.parse_args()

    repo = _repo_root()
    data_dir = repo / "args" / "data"
    logs_dir = repo / "args" / "logs"
    offline_dir = repo / "args" / "offline"

    now = _utc_now()

    stop_flag = Path(args.stop_flag_path) if args.stop_flag_path else (data_dir / "stop.flag")
    hb = data_dir / "scheduler_heartbeat.txt"
    ops_health = data_dir / "ops_health.json"
    ops_events = logs_dir / "ops_events.jsonl"
    latest_run = data_dir / "latest_run_id.txt"
    latest_paths = data_dir / "latest_paths.json"

    failures = 0
    warnings = 0
    d: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts_utc": _iso(now),
        "repo_root": str(repo),
        "checks": {},
        "warnings": [],
        "failures": [],
    }

    # stop.flag
    if _exists(stop_flag):
        if args.allow_stop_flag:
            warnings += 1
            d["warnings"].append("STOP_FLAG_PRESENT_ALLOWED")
        else:
            failures += 1
            d["failures"].append("STOP_FLAG_PRESENT")
    d["checks"]["stop_flag"] = {"path": str(stop_flag), "exists": _exists(stop_flag)}

    # heartbeat freshness
    hb_age = _mtime_age_s(hb, now)
    if hb_age is None:
        failures += 1
        d["failures"].append("HEARTBEAT_MISSING")
    else:
        if hb_age > args.max_heartbeat_age_s:
            failures += 1
            d["failures"].append("HEARTBEAT_STALE")
    d["checks"]["scheduler_heartbeat"] = {"path": str(hb), "age_s": hb_age, "max_age_s": args.max_heartbeat_age_s}

    # ops_health freshness + content
    ops_health_age = _mtime_age_s(ops_health, now)
    ops_health_json = _read_json(ops_health) if ops_health_age is not None else None
    if ops_health_age is None or ops_health_json is None:
        failures += 1
        d["failures"].append("OPS_HEALTH_MISSING_OR_BAD_JSON")
    else:
        if ops_health_age > args.max_ops_health_age_s:
            failures += 1
            d["failures"].append("OPS_HEALTH_STALE")
        # best-effort ok flag (if present)
        ok_val = ops_health_json.get("ok", None)
        if ok_val is False:
            failures += 1
            d["failures"].append("OPS_HEALTH_OK_FALSE")
    d["checks"]["ops_health"] = {
        "path": str(ops_health),
        "age_s": ops_health_age,
        "max_age_s": args.max_ops_health_age_s,
        "ok_field": (ops_health_json.get("ok") if isinstance(ops_health_json, dict) else None),
    }

    # ops_events freshness (file mtime only; schema-independent)
    ops_events_age = _mtime_age_s(ops_events, now)
    if ops_events_age is None:
        failures += 1
        d["failures"].append("OPS_EVENTS_MISSING")
    else:
        if ops_events_age > args.max_ops_events_age_s:
            failures += 1
            d["failures"].append("OPS_EVENTS_STALE")
    d["checks"]["ops_events"] = {"path": str(ops_events), "age_s": ops_events_age, "max_age_s": args.max_ops_events_age_s}

    # latest_run freshness
    lr_age = _mtime_age_s(latest_run, now)
    lr_txt = _read_text(latest_run)
    if lr_age is None or not (lr_txt or "").strip():
        failures += 1
        d["failures"].append("LATEST_RUN_MISSING_OR_EMPTY")
    else:
        if lr_age > args.max_latest_run_age_s:
            failures += 1
            d["failures"].append("LATEST_RUN_STALE")
    d["checks"]["latest_run_id"] = {"path": str(latest_run), "age_s": lr_age, "max_age_s": args.max_latest_run_age_s, "run_id": (lr_txt or "").strip()}

    # latest_paths optional
    lp_age = _mtime_age_s(latest_paths, now)
    if args.require_latest_paths:
        if lp_age is None:
            failures += 1
            d["failures"].append("LATEST_PATHS_MISSING")
    d["checks"]["latest_paths"] = {"path": str(latest_paths), "age_s": lp_age, "required": bool(args.require_latest_paths)}

    # Optional: verify audit chain in model_registry_events.jsonl
    if args.verify_registry_audit:
        cmd = ["py", "-3.11", "-m", "args.offline.model_registry_v0", "--verify-audit"]
        rc, out = _run_py(cmd, timeout_s=args.py_timeout_s)
        if rc != 0:
            failures += 1
            d["failures"].append("MODEL_REGISTRY_AUDIT_VERIFY_FAIL")
        d["checks"]["verify_registry_audit"] = {"enabled": True, "rc": rc, "out": out[:800]}
    else:
        d["checks"]["verify_registry_audit"] = {"enabled": False}

    # Optional: verify eval evidence latest pointer (sha256)
    if args.verify_evidence_latest:
        latest_ptr = offline_dir / "evidence" / "eval_gate" / "latest.json"
        cmd = ["py", "-3.11", "-m", "args.offline.evidence_history_v0", "verify", "--latest_pointer", str(latest_ptr)]
        rc, out = _run_py(cmd, timeout_s=args.py_timeout_s)
        if rc != 0:
            failures += 1
            d["failures"].append("EVIDENCE_LATEST_VERIFY_FAIL")
        d["checks"]["verify_evidence_latest"] = {"enabled": True, "rc": rc, "out": out[:800], "latest_pointer": str(latest_ptr)}
    else:
        d["checks"]["verify_evidence_latest"] = {"enabled": False}

    # Safe remediation: on FAIL optionally write stop.flag (halt trading/loop)
    if failures > 0 and args.halt_on_fail:
        try:
            stop_flag.write_text(_iso(now) + " SOAK_GATE_FAIL\n", encoding="utf-8")
            d["checks"]["halt_on_fail"] = {"enabled": True, "stop_flag_written": True, "path": str(stop_flag)}
        except Exception as e:
            # Even remediation failure should be visible
            d["checks"]["halt_on_fail"] = {"enabled": True, "stop_flag_written": False, "error": f"{type(e).__name__}:{e}"}
            # remediation failure -> still FAIL, but add warning
            warnings += 1
            d["warnings"].append("HALT_ON_FAIL_WRITE_FAILED")
    else:
        d["checks"]["halt_on_fail"] = {"enabled": bool(args.halt_on_fail), "stop_flag_written": False, "path": str(stop_flag)}

    reason = "OK"
    if failures > 0:
        reason = d["failures"][0]
    elif warnings > 0:
        reason = d["warnings"][0]

    res = _decide(failures, warnings, reason, d)

    out_obj = {
        "schema_version": SCHEMA_VERSION,
        "ts_utc": d["ts_utc"],
        "ok": res.ok,
        "level": res.level,
        "exit_code": res.exit_code,
        "reason": res.reason,
        "details": d,
    }

    out_path = Path(args.write_out) if args.write_out else (data_dir / "soak_status.json")
    try:
        out_path.write_text(json.dumps(out_obj, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # Do not print non-JSON; just embed write error
        out_obj["details"]["warnings"].append("WRITE_OUT_FAILED")

    # JSON-only stdout
    sys.stdout.write(json.dumps(out_obj, ensure_ascii=False))
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
