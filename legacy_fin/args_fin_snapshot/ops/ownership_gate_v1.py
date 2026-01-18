# args/ops/ownership_gate_v1.py
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from args.ibkr.ibkr_positions_snapshotter_v0 import snapshot_positions
from args.ibkr.ibkr_open_orders_snapshotter_v0b import snapshot_open_orders

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"

SCHEMA = "ownership_gate_v1"


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


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--client-id", type=int, required=True)
    ap.add_argument("--connect-timeout-s", type=float, default=8.0)
    ap.add_argument("--timeout-s", type=float, default=25.0)

    ap.add_argument("--control-plane", default=str(DATA_DIR / "control_plane.json"))
    ap.add_argument("--out", default=str(DATA_DIR / "ownership_gate_report.json"))

    args = ap.parse_args(argv)
    ts = _utc_now_iso()

    try:
        cp = _read_json(Path(args.control_plane))
    except Exception as e:
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"control_plane_read_failed: {type(e).__name__}: {e}",
        }
        _write_json(Path(args.out), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    allowlist = _normalize_allowlist(cp)
    global_mode = _u(cp.get("global_mode"))

    # Snapshots (authoritative)
    pos = snapshot_positions(
        args.host, args.port, args.client_id, args.connect_timeout_s, args.timeout_s
    )
    if not pos.get("ok"):
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"positions_snapshot_failed: {pos.get('error')}",
        }
        _write_json(Path(args.out), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    oo = snapshot_open_orders(
        args.host, args.port, args.client_id, args.connect_timeout_s, args.timeout_s
    )
    if not oo.get("ok"):
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"open_orders_snapshot_failed: {oo.get('error')}",
        }
        _write_json(Path(args.out), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    unknown_positions: List[Dict[str, Any]] = []
    manual_untracked_orders: List[Dict[str, Any]] = []
    forbidden_orders: List[Dict[str, Any]] = []

    # Positions: any non-zero outside allowlist is UNKNOWN
    for r in pos.get("rows", []):
        p = float(r.get("position") or 0.0)
        if abs(p) < 1e-9:
            continue
        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")
        if not _is_allowlisted(sym, ls, allowlist):
            unknown_positions.append(
                {
                    "symbol": sym,
                    "localSymbol": ls,
                    "secType": str(r.get("secType") or ""),
                    "conId": int(r.get("conId") or 0),
                    "position": p,
                }
            )

    # Orders: any non-terminal with orderId<=0 is MANUAL_UNTRACKED
    for r in oo.get("rows", []):
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

        if global_mode == "ONLY_EXITS" and _u(action) == "BUY":
            forbidden_orders.append(entry)

    reasons: List[str] = []
    if unknown_positions:
        reasons.append("UNKNOWN_POSITIONS_PRESENT")
    if manual_untracked_orders:
        reasons.append("MANUAL_UNTRACKED_ORDERS_PRESENT")
    if forbidden_orders:
        reasons.append("FORBIDDEN_ORDERS_PRESENT_ONLY_EXITS")

    ok_gate = len(reasons) == 0

    out = {
        "schema": SCHEMA,
        "ts_utc": ts,
        "ok": True,
        "exit_code": 0 if ok_gate else 1,
        "gate_pass": ok_gate,
        "reasons": reasons,
        "allowlist": allowlist,
        "global_mode": global_mode,
        "counts": {
            "unknown_positions": len(unknown_positions),
            "manual_untracked_orders": len(manual_untracked_orders),
            "forbidden_orders": len(forbidden_orders),
        },
        "unknown_positions": unknown_positions[:25],
        "manual_untracked_orders": manual_untracked_orders[:25],
        "forbidden_orders": forbidden_orders[:25],
        "open_orders_warnings": list(oo.get("warnings") or [])[:10],
    }

    _write_json(Path(args.out), out)
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return int(out["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
