# args/ops/ops_health_v1.py
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from args.ibkr.ibkr_positions_snapshotter_v0 import snapshot_positions
from args.ibkr.ibkr_open_orders_snapshotter_v0b import snapshot_open_orders

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

SCHEMA = "ops_health_v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _normalize_allowlist(cp: Dict[str, Any]) -> List[str]:
    raw = (
        cp.get("allowlist")
        or cp.get("allowlist_symbols")
        or cp.get("instrument_allowlist")
        or cp.get("instrumentAllowlist")
        or []
    )
    if isinstance(raw, str):
        lst = [raw]
    else:
        try:
            lst = list(raw)
        except Exception:
            lst = []
    seen = set()
    out: List[str] = []
    for s in lst:
        k = _u(s)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def _is_allowlisted(symbol: str, local_symbol: str, allowlist_norm: List[str]) -> bool:
    s = _u(symbol)
    l = _u(local_symbol)
    for a in allowlist_norm:
        if not a:
            continue
        if s == a:
            return True
        if l == a or l.startswith(a):
            return True
    return False


def _is_terminal_order_status(status: str) -> bool:
    return _u(status) in {"FILLED", "CANCELLED", "INACTIVE"}


@dataclass
class GateResult:
    gate_pass: bool
    reasons: List[str]
    unknown_positions: List[Dict[str, Any]]
    manual_untracked_orders: List[Dict[str, Any]]
    forbidden_orders: List[Dict[str, Any]]


def _ownership_gate_eval(
    *,
    allowlist: List[str],
    global_mode: str,
    positions_rows: List[Dict[str, Any]],
    open_orders_rows: List[Dict[str, Any]],
) -> GateResult:
    unknown_positions: List[Dict[str, Any]] = []
    manual_untracked_orders: List[Dict[str, Any]] = []
    forbidden_orders: List[Dict[str, Any]] = []

    # Unknown positions: any non-zero outside allowlist
    for r in positions_rows:
        p = float(r.get("position") or 0.0)
        if abs(p) < 1e-9:
            continue
        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")
        if not _is_allowlisted(sym, ls, allowlist):
            unknown_positions.append({
                "symbol": sym,
                "localSymbol": ls,
                "secType": str(r.get("secType") or ""),
                "conId": int(r.get("conId") or 0),
                "position": p,
            })

    # Orders: any non-terminal with orderId<=0 is manual/untracked
    for r in open_orders_rows:
        status = str(r.get("status") or "")
        if _is_terminal_order_status(status):
            continue

        oid = int(r.get("orderId") or 0)
        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")
        action = str(r.get("action") or "")

        entry = {
            "orderId": oid,
            "permId": int(r.get("permId") or 0),
            "clientId": int(r.get("clientId") or 0),
            "status": status,
            "symbol": sym,
            "localSymbol": ls,
            "secType": str(r.get("secType") or ""),
            "action": action,
            "orderType": str(r.get("orderType") or ""),
            "tif": str(r.get("tif") or ""),
        }

        if oid <= 0:
            manual_untracked_orders.append(entry)

        if _u(global_mode) == "ONLY_EXITS" and _u(action) == "BUY":
            forbidden_orders.append(entry)

    reasons: List[str] = []
    if unknown_positions:
        reasons.append("UNKNOWN_POSITIONS_PRESENT")
    if manual_untracked_orders:
        reasons.append("MANUAL_UNTRACKED_ORDERS_PRESENT")
    if forbidden_orders:
        reasons.append("FORBIDDEN_ORDERS_PRESENT_ONLY_EXITS")

    return GateResult(
        gate_pass=(len(reasons) == 0),
        reasons=reasons,
        unknown_positions=unknown_positions,
        manual_untracked_orders=manual_untracked_orders,
        forbidden_orders=forbidden_orders,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--client-id", type=int, required=True)
    ap.add_argument("--connect-timeout-s", type=float, default=8.0)
    ap.add_argument("--timeout-s", type=float, default=25.0)

    ap.add_argument("--control-plane", default=str(DATA_DIR / "control_plane.json"))
    ap.add_argument("--out", default=str(DATA_DIR / "ops_health.json"))

    args = ap.parse_args(argv)

    ts = _utc_now_iso()
    out_path = Path(args.out)

    # Default output object (filled progressively)
    out: Dict[str, Any] = {
        "schema": SCHEMA,
        "ts_utc": ts,
        "ok": False,
        "exit_code": 2,
        "severity": "FAIL",
        "error": None,
    }

    try:
        cp = _read_json(Path(args.control_plane))
        allowlist = _normalize_allowlist(cp)
        global_mode = str(cp.get("global_mode") or "")
        execution_mode = str(cp.get("execution_mode") or "")
        enable_paper_execution = bool(cp.get("enable_paper_execution", False))
        stop_flag = (DATA_DIR / "stop.flag").exists()

        # Snapshots
        pos = snapshot_positions(args.host, args.port, args.client_id, float(args.connect_timeout_s), float(args.timeout_s))
        oo = snapshot_open_orders(args.host, args.port, args.client_id, float(args.connect_timeout_s), float(args.timeout_s))

        if not pos.get("ok"):
            out.update({
                "ok": False,
                "exit_code": 2,
                "severity": "FAIL",
                "error": f"positions_snapshot_failed: {pos.get('error')}",
            })
            _write_json(out_path, out)
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        if not oo.get("ok"):
            out.update({
                "ok": False,
                "exit_code": 2,
                "severity": "FAIL",
                "error": f"open_orders_snapshot_failed: {oo.get('error')}",
            })
            _write_json(out_path, out)
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        # Ownership gate
        gate = _ownership_gate_eval(
            allowlist=allowlist,
            global_mode=global_mode,
            positions_rows=list(pos.get("rows") or []),
            open_orders_rows=list(oo.get("rows") or []),
        )

        # Baseline (strict): positions==0 and open_orders==0 (non-terminal)
        pos_nonzero = sum(1 for r in (pos.get("rows") or []) if abs(float(r.get("position") or 0.0)) > 1e-9)
        oo_open = 0
        for r in (oo.get("rows") or []):
            status = str(r.get("status") or "")
            if _is_terminal_order_status(status):
                continue
            oo_open += 1

        baseline_flat = (pos_nonzero == 0) and (oo_open == 0)

        reasons: List[str] = list(gate.reasons)

        # Safety: if execution is armed while ownership gate fails -> FAIL hard
        if _u(execution_mode) == "PAPER" and enable_paper_execution and not gate.gate_pass:
            reasons.append("PAPER_ARMED_BUT_OWNERSHIP_GATE_FAIL")

        # stop.flag means operator requested stop; not a failure by itself
        if stop_flag:
            reasons.append("STOP_FLAG_PRESENT")

        # Severity / exit codes
        # 0 OK, 1 WARN, 2 FAIL
        if not gate.gate_pass:
            severity = "FAIL"
            exit_code = 1  # gate fail is operational FAIL but not infra
        else:
            severity = "OK"
            exit_code = 0

        # If stop.flag present, keep severity at least WARN
        if stop_flag and severity == "OK":
            severity = "WARN"
            exit_code = max(exit_code, 1)

        suggested_actions: List[str] = []
        if "UNKNOWN_POSITIONS_PRESENT" in reasons:
            suggested_actions.append("Close unknown positions or remove them from this account; keep allowlist-only for ARGS.")
        if "MANUAL_UNTRACKED_ORDERS_PRESENT" in reasons:
            suggested_actions.append("Manual/untracked orders detected (orderId<=0). Cancel/adjust in TWS or wait for fill; API may not manage them.")
        if "FORBIDDEN_ORDERS_PRESENT_ONLY_EXITS" in reasons:
            suggested_actions.append("Forbidden BUY orders present while ONLY_EXITS. Cancel immediately in TWS.")
        if "PAPER_ARMED_BUT_OWNERSHIP_GATE_FAIL" in reasons:
            suggested_actions.append("Disable PAPER execution until ownership gate PASS (baseline/ownership cleanup).")
        if "STOP_FLAG_PRESENT" in reasons:
            suggested_actions.append("stop.flag present: system is intentionally paused; remove stop.flag to resume normal ops when safe.")

        out.update({
            "ok": True,
            "exit_code": int(exit_code),
            "severity": severity,
            "error": None,
            "control_plane": {
                "global_mode": global_mode,
                "execution_mode": execution_mode,
                "enable_paper_execution": enable_paper_execution,
                "allowlist": allowlist,
            },
            "ownership_gate": {
                "gate_pass": gate.gate_pass,
                "reasons": gate.reasons,
                "counts": {
                    "unknown_positions": len(gate.unknown_positions),
                    "manual_untracked_orders": len(gate.manual_untracked_orders),
                    "forbidden_orders": len(gate.forbidden_orders),
                },
                "unknown_positions": gate.unknown_positions[:25],
                "manual_untracked_orders": gate.manual_untracked_orders[:25],
                "forbidden_orders": gate.forbidden_orders[:25],
            },
            "baseline": {
                "baseline_flat": baseline_flat,
                "positions_nonzero_total": int(pos_nonzero),
                "open_orders_total": int(oo_open),
            },
            "open_orders_warnings": list(oo.get("warnings") or [])[:10],
            "reasons": reasons,
            "suggested_actions": suggested_actions,
            "paths": {
                "ops_health_json": str(out_path),
            },
        })

        _write_json(out_path, out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return int(exit_code)

    except Exception as e:
        out.update({
            "ok": False,
            "exit_code": 2,
            "severity": "FAIL",
            "error": f"{type(e).__name__}: {e}",
        })
        try:
            _write_json(out_path, out)
        except Exception:
            pass
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
