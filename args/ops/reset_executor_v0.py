
# args/ops/reset_executor_v0.py
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from args.ibkr.ibkr_positions_snapshotter_v0 import snapshot_positions


SCHEMA_EXEC = "reset_executor_v0"
SCHEMA_PLAN = "reset_plan_v0"
SCHEMA_PREVIEW = "reset_preview_v0"


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _lock_or_fail(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
    except FileExistsError:
        raise RuntimeError(f"ACTIVE_LOCK: {lock_path}")


def _unlock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _normalize_allowlist(cp: Dict[str, Any]) -> List[str]:
    # Back-compat: support multiple possible keys
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
    # normalize + dedupe while preserving order
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
        # Exact match by symbol
        if s == a:
            return True
        # Futures often show localSymbol like "MHG..." — accept startswith
        if l == a or l.startswith(a):
            return True
    return False


@dataclass
class ResetItem:
    priority: int                 # 0 unknown first, 1 allowlist next
    kind: str                     # CLOSE_UNKNOWN / CLOSE_ALLOWLIST
    symbol: str
    secType: str
    currency: str
    exchange: str
    conId: int
    localSymbol: str
    lastTradeDateOrContractMonth: str
    action: str                   # BUY/SELL
    qty: float
    reason: str


def build_reset_plan(positions_snapshot: Dict[str, Any], allowlist_norm: List[str]) -> Dict[str, Any]:
    items: List[ResetItem] = []
    for r in positions_snapshot.get("rows", []):
        pos = float(r.get("position") or 0.0)
        if abs(pos) < 1e-9:
            continue

        symbol = str(r.get("symbol") or "")
        local_symbol = str(r.get("localSymbol") or "")
        secType = str(r.get("secType") or "")
        currency = str(r.get("currency") or "")
        exchange = str(r.get("exchange") or "")
        conId = int(r.get("conId") or 0)
        ltd = str(r.get("lastTradeDateOrContractMonth") or "")

        allow = _is_allowlisted(symbol, local_symbol, allowlist_norm)
        priority = 1 if allow else 0
        kind = "CLOSE_ALLOWLIST" if allow else "CLOSE_UNKNOWN"

        action = "SELL" if pos > 0 else "BUY"
        qty = abs(pos)

        reason = f"flatten position pos={pos} symbol={symbol} localSymbol={local_symbol} secType={secType}"
        items.append(
            ResetItem(
                priority=priority,
                kind=kind,
                symbol=symbol,
                secType=secType,
                currency=currency,
                exchange=exchange,
                conId=conId,
                localSymbol=local_symbol,
                lastTradeDateOrContractMonth=ltd,
                action=action,
                qty=qty,
                reason=reason,
            )
        )

    # sort: unknown first, then allowlist
    items.sort(key=lambda x: (x.priority, _u(x.symbol), _u(x.secType)))

    return {
        "schema": SCHEMA_PLAN,
        "ts_utc": _utc_now_iso(),
        "allowlist": allowlist_norm,
        "items": [asdict(i) for i in items],
    }


def build_preview(plan: Dict[str, Any], control_plane: Dict[str, Any]) -> Dict[str, Any]:
    reqs: List[Dict[str, Any]] = []
    for it in plan.get("items", []):
        contract = {
            "conId": int(it.get("conId") or 0),
            "symbol": it.get("symbol", ""),
            "secType": it.get("secType", ""),
            "exchange": it.get("exchange", ""),
            "currency": it.get("currency", ""),
            "localSymbol": it.get("localSymbol", ""),
            "lastTradeDateOrContractMonth": it.get("lastTradeDateOrContractMonth", ""),
        }
        order = {
            "action": it.get("action", ""),
            "totalQuantity": it.get("qty", 0),
            "orderType": "MKT",
            "tif": "DAY",
        }
        reqs.append(
            {
                "kind": it.get("kind", ""),
                "priority": it.get("priority", 9),
                "contract": contract,
                "order": order,
                "reason": it.get("reason", ""),
            }
        )

    return {
        "schema": SCHEMA_PREVIEW,
        "ts_utc": _utc_now_iso(),
        "execution_mode": control_plane.get("execution_mode", ""),
        "global_mode": control_plane.get("global_mode", ""),
        "enable_paper_execution": bool(control_plane.get("enable_paper_execution", False)),
        "requests": reqs,
        "notes": [
            "DRYRUN mode emits preview only; no IBKR orders are sent.",
            "PAPER execution is blocked until user issues: ENABLE PAPER EXECUTION.",
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--client-id", type=int, required=True)
    ap.add_argument("--connect-timeout-s", type=float, default=8.0)
    ap.add_argument("--timeout-s", type=float, default=25.0)
    ap.add_argument("--control-plane", default=str(DATA_DIR / "control_plane.json"))
    ap.add_argument("--positions-out", default=str(DATA_DIR / "ibkr_positions_live.json"))
    ap.add_argument("--plan-out", default=str(DATA_DIR / "reset_plan.json"))
    ap.add_argument("--preview-out", default=str(DATA_DIR / "reset_preview.json"))

    args = ap.parse_args(argv)

    lock_path = LOGS_DIR / "ownership_reset.lock"
    try:
        _lock_or_fail(lock_path)

        cp = _read_json(Path(args.control_plane))
        allowlist_norm = _normalize_allowlist(cp)

        execution_mode = str(cp.get("execution_mode") or "DRYRUN").upper()
        global_mode = str(cp.get("global_mode") or "").upper()

        snap = snapshot_positions(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            connect_timeout_s=args.connect_timeout_s,
            timeout_s=args.timeout_s,
        )
        _write_json(Path(args.positions_out), snap)

        warnings: List[str] = []
        if not snap.get("ok"):
            out = {
                "schema": SCHEMA_EXEC,
                "ts_utc": _utc_now_iso(),
                "ok": False,
                "exit_code": 2,
                "error": f"positions_snapshot_failed: {snap.get('error')}",
                "written": {"positions": str(Path(args.positions_out))},
            }
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        if global_mode and global_mode != "ONLY_EXITS":
            warnings.append(f"control_plane.global_mode={global_mode} (expected ONLY_EXITS for reset)")

        if not allowlist_norm:
            warnings.append("allowlist_empty: all positions will be treated as UNKNOWN (fix control_plane.json)")

        plan = build_reset_plan(snap, allowlist_norm=allowlist_norm)
        _write_json(Path(args.plan_out), plan)

        preview = build_preview(plan, control_plane=cp)
        _write_json(Path(args.preview_out), preview)

        # Hard block on paper execution until chat command, regardless of config
        enable_paper = bool(cp.get("enable_paper_execution", False))
        executed = False
        if execution_mode == "PAPER" and enable_paper:
            block_reason = "PAPER_EXECUTION_BLOCKED_UNTIL_CHAT_COMMAND"
            warnings.append("paper execution requested by config but blocked by invariant (chat command missing).")
        else:
            block_reason = "PAPER_EXECUTION_DISABLED"

        unknown_count = sum(1 for i in plan.get("items", []) if i.get("kind") == "CLOSE_UNKNOWN")
        allow_count = sum(1 for i in plan.get("items", []) if i.get("kind") == "CLOSE_ALLOWLIST")

        if unknown_count > 0:
            warnings.append(f"unknown_positions_to_close={unknown_count}")

        out = {
            "schema": SCHEMA_EXEC,
            "ts_utc": _utc_now_iso(),
            "ok": True,
            "exit_code": 0,
            "executed": executed,
            "block_reason": block_reason,
            "summary": {
                "positions_rows": len(snap.get("rows", [])),
                "reset_items_total": len(plan.get("items", [])),
                "reset_items_unknown": unknown_count,
                "reset_items_allowlist": allow_count,
                "execution_mode": execution_mode,
                "global_mode": global_mode,
                "allowlist": allowlist_norm,
            },
            "warnings": warnings,
            "written": {
                "positions": str(Path(args.positions_out)),
                "plan": str(Path(args.plan_out)),
                "preview": str(Path(args.preview_out)),
            },
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 0

    except Exception as e:
        out = {
            "schema": SCHEMA_EXEC,
            "ts_utc": _utc_now_iso(),
            "ok": False,
            "exit_code": 2,
            "error": f"{type(e).__name__}: {e}",
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2
    finally:
        _unlock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
