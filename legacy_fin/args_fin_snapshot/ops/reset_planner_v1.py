# args/ops/reset_planner_v1.py
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from args.ibkr.ibkr_positions_snapshotter_v0 import snapshot_positions
from args.ibkr.ibkr_open_orders_snapshotter_v0b import snapshot_open_orders

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"

SCHEMA = "reset_plan_v1"


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


def _normalize_currency(x: Any) -> str:
    c = str(x or "").strip().upper()
    return c if c else "USD"


def _normalize_exchange(sec_type: str, symbol: str, exchange: Any) -> str:
    # Only fix known critical case for HG/MHG; otherwise keep as-is.
    sec = _u(sec_type)
    sym = _u(symbol)
    exch = str(exchange or "").strip().upper()
    if sec == "FUT" and not exch and sym in {"MHG", "HG"}:
        return "COMEX"
    return exch


@dataclass
class PlanItem:
    priority: int
    kind: str               # CANCEL_ORDER / MANUAL_CANCEL_REQUIRED / CLOSE_POSITION
    bucket: str             # UNKNOWN / ALLOWLIST
    reason: str

    # order identity
    orderId: int = 0
    permId: int = 0
    clientId: int = 0
    status: str = ""
    order_action: str = ""
    order_type: str = ""
    tif: str = ""

    # instrument identity
    conId: int = 0
    symbol: str = ""
    localSymbol: str = ""
    secType: str = ""
    currency: str = ""
    exchange: str = ""
    lastTradeDateOrContractMonth: str = ""

    # position close
    position: float = 0.0
    close_action: str = ""
    qty: float = 0.0


def _hash_plan(items: List[Dict[str, Any]]) -> str:
    b = json.dumps(items, sort_keys=True).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--client-id", type=int, required=True)
    ap.add_argument("--connect-timeout-s", type=float, default=8.0)
    ap.add_argument("--timeout-s", type=float, default=25.0)

    ap.add_argument("--control-plane", default=str(DATA_DIR / "control_plane.json"))
    ap.add_argument("--out-plan", default=str(DATA_DIR / "reset_plan_v1.json"))
    ap.add_argument("--out-preview", default=str(DATA_DIR / "reset_preview_v1.json"))
    ap.add_argument("--positions-out", default=str(DATA_DIR / "ibkr_positions_live.json"))
    ap.add_argument("--open-orders-out", default=str(DATA_DIR / "ibkr_open_orders_live.json"))

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
        _write_json(Path(args.out_plan), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    allowlist_norm = _normalize_allowlist(cp)
    global_mode = _u(cp.get("global_mode"))
    execution_mode = _u(cp.get("execution_mode"))
    enable_paper = bool(cp.get("enable_paper_execution", False))

    warnings: List[str] = []
    if not allowlist_norm:
        warnings.append("allowlist_empty: all items bucketed as UNKNOWN")

    # ---- snapshots ----
    pos = snapshot_positions(args.host, args.port, args.client_id, args.connect_timeout_s, args.timeout_s)
    _write_json(Path(args.positions_out), pos)
    if not pos.get("ok"):
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"positions_snapshot_failed: {pos.get('error')}",
        }
        _write_json(Path(args.out_plan), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    oo = snapshot_open_orders(args.host, args.port, args.client_id, args.connect_timeout_s, args.timeout_s)
    _write_json(Path(args.open_orders_out), oo)
    if not oo.get("ok"):
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"open_orders_snapshot_failed: {oo.get('error')}",
        }
        _write_json(Path(args.out_plan), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    items: List[PlanItem] = []

    # ---- Phase 0: cancel open orders (if possible) ----
    for r in oo.get("rows", []):
        status = str(r.get("status") or "")
        if _is_terminal_order_status(status):
            continue

        oid = int(r.get("orderId") or 0)
        pid = int(r.get("permId") or 0)
        cid = int(r.get("clientId") or 0)

        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")
        sec = str(r.get("secType") or "")
        cur = _normalize_currency(r.get("currency"))
        exch = _normalize_exchange(sec, sym, r.get("exchange"))
        ltd = str(r.get("lastTradeDateOrContractMonth") or "")

        bucket = "ALLOWLIST" if _is_allowlisted(sym, ls, allowlist_norm) else "UNKNOWN"

        # Only exits posture: any BUY open order is suspicious
        if global_mode == "ONLY_EXITS" and _u(r.get("action")) == "BUY":
            warnings.append(f"suspicious_open_buy_in_only_exits: permId={pid} symbol={sym} status={status}")

        if oid <= 0:
            # Manual/untracked order: cannot reliably cancel via API
            warnings.append(
                f"manual_or_untracked_order: permId={pid} clientId={cid} symbol={sym} status={status} "
                "(orderId<=0; cancel via API may be impossible)"
            )
            items.append(
                PlanItem(
                    priority=0,
                    kind="MANUAL_CANCEL_REQUIRED",
                    bucket=bucket,
                    reason="baseline requires open_orders=0 but orderId<=0 (manual/untracked); cancel in TWS or wait for fill",
                    orderId=oid,
                    permId=pid,
                    clientId=cid,
                    status=status,
                    order_action=str(r.get("action") or ""),
                    order_type=str(r.get("orderType") or ""),
                    tif=str(r.get("tif") or ""),
                    conId=int(r.get("conId") or 0),
                    symbol=sym,
                    localSymbol=ls,
                    secType=sec,
                    currency=cur,
                    exchange=exch,
                    lastTradeDateOrContractMonth=ltd,
                )
            )
        else:
            items.append(
                PlanItem(
                    priority=0,
                    kind="CANCEL_ORDER",
                    bucket=bucket,
                    reason="baseline requires open_orders=0 (cancel non-terminal order)",
                    orderId=oid,
                    permId=pid,
                    clientId=cid,
                    status=status,
                    order_action=str(r.get("action") or ""),
                    order_type=str(r.get("orderType") or ""),
                    tif=str(r.get("tif") or ""),
                    conId=int(r.get("conId") or 0),
                    symbol=sym,
                    localSymbol=ls,
                    secType=sec,
                    currency=cur,
                    exchange=exch,
                    lastTradeDateOrContractMonth=ltd,
                )
            )

    # ---- Phase 1: close positions (UNKNOWN first, then ALLOWLIST) ----
    for r in pos.get("rows", []):
        p = float(r.get("position") or 0.0)
        if abs(p) < 1e-9:
            continue

        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")
        sec = str(r.get("secType") or "")
        cur = _normalize_currency(r.get("currency"))
        exch = _normalize_exchange(sec, sym, r.get("exchange"))
        ltd = str(r.get("lastTradeDateOrContractMonth") or "")

        is_allow = _is_allowlisted(sym, ls, allowlist_norm)
        bucket = "ALLOWLIST" if is_allow else "UNKNOWN"

        close_action = "SELL" if p > 0 else "BUY"
        prio = 10 if bucket == "UNKNOWN" else 20

        items.append(
            PlanItem(
                priority=prio,
                kind="CLOSE_POSITION",
                bucket=bucket,
                reason=f"flatten position pos={p}",
                conId=int(r.get("conId") or 0),
                symbol=sym,
                localSymbol=ls,
                secType=sec,
                currency=cur,
                exchange=exch,
                lastTradeDateOrContractMonth=ltd,
                position=p,
                close_action=close_action,
                qty=abs(p),
            )
        )

    # stable sort
    items.sort(key=lambda x: (x.priority, _u(x.bucket), _u(x.symbol), _u(x.kind), int(x.orderId or 0), int(x.permId or 0)))

    items_dict = [asdict(i) for i in items]
    plan_hash = _hash_plan(items_dict)

    counts = {
        "items_total": len(items_dict),
        "cancel_orders": sum(1 for i in items_dict if i.get("kind") == "CANCEL_ORDER"),
        "manual_cancel_required": sum(1 for i in items_dict if i.get("kind") == "MANUAL_CANCEL_REQUIRED"),
        "close_positions": sum(1 for i in items_dict if i.get("kind") == "CLOSE_POSITION"),
        "cancel_orders_unknown": sum(1 for i in items_dict if i.get("kind") == "CANCEL_ORDER" and i.get("bucket") == "UNKNOWN"),
        "cancel_orders_allowlist": sum(1 for i in items_dict if i.get("kind") == "CANCEL_ORDER" and i.get("bucket") == "ALLOWLIST"),
        "close_positions_unknown": sum(1 for i in items_dict if i.get("kind") == "CLOSE_POSITION" and i.get("bucket") == "UNKNOWN"),
        "close_positions_allowlist": sum(1 for i in items_dict if i.get("kind") == "CLOSE_POSITION" and i.get("bucket") == "ALLOWLIST"),
    }

    plan = {
        "schema": SCHEMA,
        "ts_utc": ts,
        "ok": True,
        "exit_code": 0,
        "plan_hash": plan_hash,
        "allowlist": allowlist_norm,
        "control_plane": {
            "global_mode": global_mode,
            "execution_mode": execution_mode,
            "enable_paper_execution": enable_paper,
        },
        "counts": counts,
        "warnings": warnings,
        "items": items_dict,
    }

    preview = {
        "schema": "reset_preview_v1",
        "ts_utc": ts,
        "plan_hash": plan_hash,
        "notes": [
            "Phase 0: CANCEL_ORDER where orderId>0; MANUAL_CANCEL_REQUIRED where orderId<=0",
            "Phase 1: CLOSE_POSITION for all non-zero positions (UNKNOWN first, then ALLOWLIST)",
            "Execution remains blocked until ENABLE PAPER EXECUTION + control_plane switches + confirm flag.",
        ],
        "counts": counts,
        "warnings": warnings,
        "items": items_dict,
    }

    _write_json(Path(args.out_plan), plan)
    _write_json(Path(args.out_preview), preview)

    sys.stdout.write(json.dumps(plan, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
