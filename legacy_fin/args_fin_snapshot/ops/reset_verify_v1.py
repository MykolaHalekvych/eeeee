# args/ops/reset_verify_v1.py
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

SCHEMA = "reset_verify_v1"


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
    ap.add_argument("--out", default=str(DATA_DIR / "reset_verify_v1_report.json"))
    ap.add_argument(
        "--positions-out", default=str(DATA_DIR / "ibkr_positions_live.json")
    )
    ap.add_argument(
        "--open-orders-out", default=str(DATA_DIR / "ibkr_open_orders_live.json")
    )

    args = ap.parse_args(argv)
    ts = _utc_now_iso()

    # ---- control plane ----
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

    allowlist_norm = _normalize_allowlist(cp)
    warnings: List[str] = []
    if not allowlist_norm:
        warnings.append("allowlist_empty: all items classified as UNKNOWN")

    # ---- positions snapshot ----
    pos_snap = snapshot_positions(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        connect_timeout_s=float(args.connect_timeout_s),
        timeout_s=float(args.timeout_s),
    )
    _write_json(Path(args.positions_out), pos_snap)

    if not pos_snap.get("ok"):
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"positions_snapshot_failed: {pos_snap.get('error')}",
        }
        _write_json(Path(args.out), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    pos_unknown: List[Dict[str, Any]] = []
    pos_allow: List[Dict[str, Any]] = []
    pos_nonzero_total = 0

    for r in pos_snap.get("rows", []):
        pos = float(r.get("position") or 0.0)
        if abs(pos) < 1e-9:
            continue
        pos_nonzero_total += 1

        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")
        entry = {
            "symbol": sym,
            "localSymbol": ls,
            "secType": str(r.get("secType") or ""),
            "conId": int(r.get("conId") or 0),
            "position": pos,
        }
        if _is_allowlisted(sym, ls, allowlist_norm):
            pos_allow.append(entry)
        else:
            pos_unknown.append(entry)

    # ---- open orders snapshot ----
    oo_snap = snapshot_open_orders(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        connect_timeout_s=float(args.connect_timeout_s),
        timeout_s=float(args.timeout_s),
    )
    _write_json(Path(args.open_orders_out), oo_snap)

    if not oo_snap.get("ok"):
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"open_orders_snapshot_failed: {oo_snap.get('error')}",
        }
        _write_json(Path(args.out), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    ord_unknown: List[Dict[str, Any]] = []
    ord_allow: List[Dict[str, Any]] = []
    ord_manual_untracked: List[Dict[str, Any]] = []
    ord_open_total = 0

    for r in oo_snap.get("rows", []):
        status = str(r.get("status") or "")
        if _is_terminal_order_status(status):
            continue

        ord_open_total += 1

        oid = int(r.get("orderId") or 0)
        pid = int(r.get("permId") or 0)
        cid = int(r.get("clientId") or 0)

        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")

        entry = {
            "orderId": oid,
            "permId": pid,
            "clientId": cid,
            "account": str(r.get("account") or ""),
            "status": status,
            "symbol": sym,
            "localSymbol": ls,
            "secType": str(r.get("secType") or ""),
            "conId": int(r.get("conId") or 0),
            "action": str(r.get("action") or ""),
            "totalQuantity": float(r.get("totalQuantity") or 0.0),
            "orderType": str(r.get("orderType") or ""),
            "tif": str(r.get("tif") or ""),
        }

        # MANUAL_UNTRACKED: orderId<=0 means cancel/replace via API may be impossible
        if oid <= 0:
            ord_manual_untracked.append(entry)

        if _is_allowlisted(sym, ls, allowlist_norm):
            ord_allow.append(entry)
        else:
            ord_unknown.append(entry)

    if len(ord_manual_untracked) > 0:
        warnings.append(
            "manual_untracked_orders_present: orderId<=0 (likely TWS/manual). "
            "Cancel via API may be impossible; require manual cleanup or wait for fill."
        )

    baseline_flat = (pos_nonzero_total == 0) and (ord_open_total == 0)

    out = {
        "schema": SCHEMA,
        "ts_utc": ts,
        "ok": True,
        "exit_code": 0 if baseline_flat else 1,
        "allowlist": allowlist_norm,
        "baseline_flat": baseline_flat,
        "counts": {
            "positions_nonzero_total": pos_nonzero_total,
            "positions_nonzero_unknown": len(pos_unknown),
            "positions_nonzero_allowlist": len(pos_allow),
            "open_orders_total": ord_open_total,
            "open_orders_unknown": len(ord_unknown),
            "open_orders_allowlist": len(ord_allow),
            "manual_untracked_orders_total": len(ord_manual_untracked),
        },
        "positions_nonzero_unknown": pos_unknown[:25],
        "positions_nonzero_allowlist": pos_allow[:25],
        "open_orders_unknown": ord_unknown[:25],
        "open_orders_allowlist": ord_allow[:25],
        "open_orders_manual_untracked": ord_manual_untracked[:25],
        "open_orders_warnings": list(oo_snap.get("warnings") or [])[:10],
        "warnings": warnings,
        "notes": [
            "exit_code=1 means NOT FLAT (expected before executing reset).",
            "After real reset execution, baseline_flat must become true: positions=0 and open_orders=0.",
            "manual_untracked_orders_present indicates orders with orderId<=0; cancel via API may be impossible.",
        ],
        "written": {
            "positions": str(Path(args.positions_out)),
            "open_orders": str(Path(args.open_orders_out)),
        },
    }

    _write_json(Path(args.out), out)
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return int(out["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
