# args/ops/ops_health_v1.py
# JSON-only health gate for ops loop.
# Exit codes:
#   0 = OK
#   1 = WARN / OP_FAIL (ownership/baseline/stop-flags)
#   2 = FAIL / INFRA (exceptions, snapshot infra errors, JSON parse errors)

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from args.ibkr.ibkr_open_orders_snapshotter_v0b import snapshot_open_orders
from args.ibkr.ibkr_positions_snapshotter_v0 import snapshot_positions

SCHEMA = "ops_health_v1"

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

_TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELLED", "INACTIVE"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _as_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _as_int(x: Any) -> int:
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return 0


def _stdout_json(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _write_json_utf8(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _read_json_utf8_sig(path: Path) -> Any:
    """
    BOM-safe JSON read: utf-8-sig strips BOM if present.
    If the file contains a BOM and we decode with plain utf-8, json.loads can throw:
      JSONDecodeError: Unexpected UTF-8 BOM ...
    """
    data = path.read_bytes()
    text = data.decode("utf-8-sig")
    return json.loads(text)


def _normalize_allowlist(cp: dict[str, Any]) -> list[str]:
    raw = (
        cp.get("allowlist")
        or cp.get("allowlist_symbols")
        or cp.get("instrument_allowlist")
        or cp.get("instrumentAllowlist")
        or []
    )
    if isinstance(raw, str):
        items = [raw]
    else:
        try:
            items = list(raw)
        except Exception:
            items = []

    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        k = _u(s)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def _is_allowlisted(symbol: str, local_symbol: str, allowlist_norm: list[str]) -> bool:
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
    return _u(status) in _TERMINAL_ORDER_STATUSES


@dataclass
class GateResult:
    gate_pass: bool
    reasons: list[str]
    unknown_positions: list[dict[str, Any]]
    manual_untracked_orders: list[dict[str, Any]]
    forbidden_orders: list[dict[str, Any]]


def _ownership_gate_eval(
    *,
    allowlist: list[str],
    global_mode: str,
    positions_rows: list[dict[str, Any]],
    open_orders_rows: list[dict[str, Any]],
) -> GateResult:
    unknown_positions: list[dict[str, Any]] = []
    manual_untracked_orders: list[dict[str, Any]] = []
    forbidden_orders: list[dict[str, Any]] = []

    # Unknown positions: any non-zero outside allowlist.
    for r in positions_rows:
        pos = _as_float(r.get("position"))
        if abs(pos) < 1e-9:
            continue

        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")
        if not _is_allowlisted(sym, ls, allowlist):
            unknown_positions.append(
                {
                    "symbol": sym,
                    "localSymbol": ls,
                    "secType": str(r.get("secType") or ""),
                    "conId": _as_int(r.get("conId")),
                    "position": pos,
                }
            )

    # Orders:
    # - manual/untracked: non-terminal with orderId<=0
    # - forbidden: ONLY_EXITS + BUY
    gm = _u(global_mode)
    for r in open_orders_rows:
        status = str(r.get("status") or "")
        if _is_terminal_order_status(status):
            continue

        oid = _as_int(r.get("orderId"))
        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")
        action = str(r.get("action") or "")

        entry = {
            "orderId": oid,
            "permId": _as_int(r.get("permId")),
            "clientId": _as_int(r.get("clientId")),
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

        if gm == "ONLY_EXITS" and _u(action) == "BUY":
            forbidden_orders.append(entry)

    reasons: list[str] = []
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


def main(argv: Optional[list[str]] = None) -> int:
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
    cp_path = Path(args.control_plane)

    stop_flag_ops = (DATA_DIR / "stop.flag").exists()  # ops pause flag
    stop_flag_exec = (LOGS_DIR / "stop.flag").exists()  # execution/proofs arming flag

    out: dict[str, Any] = {
        "schema": SCHEMA,
        "ts_utc": ts,
        "ok": False,
        "exit_code": 2,
        "severity": "FAIL",
        "error": None,
        "traceback": None,
        "meta": {
            "module_file": str(Path(__file__).resolve()),
            "repo_root": str(REPO_ROOT),
        },
        "paths": {
            "control_plane": str(cp_path),
            "ops_stop_flag": str(DATA_DIR / "stop.flag"),
            "exec_stop_flag": str(LOGS_DIR / "stop.flag"),
            "ops_health_json": str(out_path),
        },
    }

    try:
        # Control plane (BOM-safe)
        cp = _read_json_utf8_sig(cp_path)

        allowlist = _normalize_allowlist(cp)
        global_mode = str(cp.get("global_mode") or "")
        execution_mode = str(cp.get("execution_mode") or "")
        enable_paper_execution = bool(cp.get("enable_paper_execution", False))

        execution_armed = (
            (_u(execution_mode) == "PAPER")
            and enable_paper_execution
            and stop_flag_exec
        )

        # Snapshots (read-only)
        pos = snapshot_positions(
            args.host,
            args.port,
            args.client_id,
            float(args.connect_timeout_s),
            float(args.timeout_s),
        )
        oo = snapshot_open_orders(
            args.host,
            args.port,
            args.client_id,
            float(args.connect_timeout_s),
            float(args.timeout_s),
        )

        if not bool(pos.get("ok")):
            out.update(
                {
                    "ok": False,
                    "exit_code": 2,
                    "severity": "FAIL",
                    "error": f"positions_snapshot_failed: {pos.get('error')}",
                }
            )
            _write_json_utf8(out_path, out)
            _stdout_json(out)
            return 2

        if not bool(oo.get("ok")):
            out.update(
                {
                    "ok": False,
                    "exit_code": 2,
                    "severity": "FAIL",
                    "error": f"open_orders_snapshot_failed: {oo.get('error')}",
                }
            )
            _write_json_utf8(out_path, out)
            _stdout_json(out)
            return 2

        pos_rows = list(pos.get("rows") or [])
        oo_rows = list(oo.get("rows") or [])

        gate = _ownership_gate_eval(
            allowlist=allowlist,
            global_mode=global_mode,
            positions_rows=pos_rows,
            open_orders_rows=oo_rows,
        )

        # Baseline (strict): positions==0 and open_orders==0 (non-terminal)
        pos_nonzero_total = sum(
            1 for r in pos_rows if abs(_as_float(r.get("position"))) > 1e-9
        )
        open_orders_total = sum(
            1
            for r in oo_rows
            if not _is_terminal_order_status(str(r.get("status") or ""))
        )
        baseline_flat = (pos_nonzero_total == 0) and (open_orders_total == 0)

        reasons: list[str] = list(gate.reasons)
        if stop_flag_ops:
            reasons.append("STOP_FLAG_PRESENT_OPS")
        if stop_flag_exec:
            reasons.append("STOP_FLAG_PRESENT_EXEC")
        if execution_armed and not gate.gate_pass:
            reasons.append("PAPER_ARMED_BUT_OWNERSHIP_GATE_FAIL")

        # Compute exit_code/severity:
        severity = "OK"
        exit_code = 0

        # Stop flags => at least WARN
        if stop_flag_ops or stop_flag_exec:
            severity = "WARN"
            exit_code = max(exit_code, 1)

        # Ownership gate fail => FAIL (operational fail)
        if not gate.gate_pass:
            severity = "FAIL"
            exit_code = max(exit_code, 1)

        # If execution is armed while ownership gate fails => HARD FAIL
        if "PAPER_ARMED_BUT_OWNERSHIP_GATE_FAIL" in reasons:
            severity = "FAIL"
            exit_code = 2

        suggested_actions: list[str] = []
        if "UNKNOWN_POSITIONS_PRESENT" in reasons:
            suggested_actions.append(
                "Close unknown positions or move them off this account; keep allowlist-only for ARGS."
            )
        if "MANUAL_UNTRACKED_ORDERS_PRESENT" in reasons:
            suggested_actions.append(
                "Manual/untracked orders detected (orderId<=0). Cancel/adjust in TWS or wait for fill; API may not manage them."
            )
        if "FORBIDDEN_ORDERS_PRESENT_ONLY_EXITS" in reasons:
            suggested_actions.append(
                "Forbidden BUY orders present while ONLY_EXITS. Cancel immediately in TWS."
            )
        if "PAPER_ARMED_BUT_OWNERSHIP_GATE_FAIL" in reasons:
            suggested_actions.append(
                "Disable PAPER execution until ownership gate PASS (baseline/ownership cleanup)."
            )
        if "STOP_FLAG_PRESENT_OPS" in reasons:
            suggested_actions.append(
                "ops stop.flag present: ops loop is intentionally paused; remove args/data/stop.flag to resume."
            )
        if "STOP_FLAG_PRESENT_EXEC" in reasons:
            suggested_actions.append(
                "exec stop.flag present: proofs/execution are armed; remove args/logs/stop.flag when safe."
            )

        out.update(
            {
                "ok": (exit_code == 0),
                "exit_code": int(exit_code),
                "severity": severity,
                "error": None,
                "traceback": None,
                "control_plane": {
                    "global_mode": global_mode,
                    "execution_mode": execution_mode,
                    "enable_paper_execution": enable_paper_execution,
                    "allowlist": allowlist,
                },
                "execution": {
                    "execution_armed": bool(execution_armed),
                    "stop_flag_ops": bool(stop_flag_ops),
                    "stop_flag_exec": bool(stop_flag_exec),
                },
                "ownership_gate": {
                    "gate_pass": bool(gate.gate_pass),
                    "reasons": list(gate.reasons),
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
                    "baseline_flat": bool(baseline_flat),
                    "positions_nonzero_total": int(pos_nonzero_total),
                    "open_orders_total": int(open_orders_total),
                },
                "open_orders_warnings": list(oo.get("warnings") or [])[:10],
                "reasons": reasons,
                "suggested_actions": suggested_actions,
            }
        )

        _write_json_utf8(out_path, out)
        _stdout_json(out)
        return int(exit_code)

    except Exception as e:
        out.update(
            {
                "ok": False,
                "exit_code": 2,
                "severity": "FAIL",
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            }
        )
        try:
            _write_json_utf8(out_path, out)
        except Exception:
            pass
        _stdout_json(out)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
