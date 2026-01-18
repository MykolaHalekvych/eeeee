# args/ops/reset_executor_paper_v0.py
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order

SCHEMA = "reset_executor_paper_v0"

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

STOP_FLAG = DATA_DIR / "stop.flag"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


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


def _id_key(item: Dict[str, Any]) -> str:
    # Strong idempotency key: conId+secType+action+qty+localSymbol+ltd
    core = {
        "conId": int(item.get("conId") or 0),
        "secType": str(item.get("secType") or ""),
        "action": str(item.get("action") or ""),
        "qty": float(item.get("qty") or 0.0),
        "localSymbol": str(item.get("localSymbol") or ""),
        "ltd": str(item.get("lastTradeDateOrContractMonth") or ""),
    }
    b = json.dumps(core, sort_keys=True).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


class _App(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)

        self._next_valid_id_evt = threading.Event()
        self._done_evt = threading.Event()

        self.next_order_id: Optional[int] = None
        self.errors: List[str] = []

        # order_id -> status
        self.status: Dict[int, str] = {}
        self.filled: Dict[int, float] = {}
        self.remaining: Dict[int, float] = {}

        self._lock = threading.Lock()

    def nextValidId(self, orderId: int) -> None:
        self.next_order_id = int(orderId)
        self._next_valid_id_evt.set()

    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        msg = f"reqId={reqId} code={errorCode} msg={errorString}"
        if advancedOrderRejectJson:
            msg += f" adv={advancedOrderRejectJson}"
        with self._lock:
            self.errors.append(msg)

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        with self._lock:
            self.status[int(orderId)] = str(status or "")
            self.filled[int(orderId)] = float(filled or 0.0)
            self.remaining[int(orderId)] = float(remaining or 0.0)

    def _terminal(self, s: str) -> bool:
        s2 = _u(s)
        return s2 in {"FILLED", "CANCELLED", "INACTIVE"}  # IB statuses

    def wait_all_terminal(
        self, order_ids: List[int], timeout_s: float
    ) -> Tuple[bool, Dict[int, str]]:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            with self._lock:
                snapshot = {oid: self.status.get(oid, "") for oid in order_ids}
            if all(self._terminal(snapshot.get(oid, "")) for oid in order_ids):
                return True, snapshot
            time.sleep(0.25)
        with self._lock:
            snapshot = {oid: self.status.get(oid, "") for oid in order_ids}
        return False, snapshot


def _build_contract(it: Dict[str, Any]) -> Contract:
    c = Contract()
    c.conId = int(it.get("conId") or 0)
    c.symbol = str(it.get("symbol") or "")
    c.secType = str(it.get("secType") or "")
    c.currency = str(it.get("currency") or "USD")

    sec = _u(c.secType)
    sym = _u(it.get("symbol"))

    exch = str(it.get("exchange") or "").strip()

    if sec == "STK":
        # safest default for stocks
        c.exchange = "SMART"
        # primary exchange optional; keep as provided if any
        pe = str(it.get("primaryExchange") or "").strip()
        if pe:
            c.primaryExchange = pe
    elif sec == "FUT":
        # Futures: SMART is often invalid; set a real venue when unknown
        if exch:
            c.exchange = exch
        else:
            if sym in {"MHG", "HG"}:
                c.exchange = "COMEX"
            else:
                # fallback, but ideally instrument-specific
                c.exchange = "GLOBEX"
        ltd = str(it.get("lastTradeDateOrContractMonth") or "")
        if ltd:
            c.lastTradeDateOrContractMonth = ltd
    else:
        c.exchange = exch or "SMART"

    c.localSymbol = str(it.get("localSymbol") or "")
    return c

    # Futures need expiry/contract month sometimes
    ltd = str(it.get("lastTradeDateOrContractMonth") or "")
    if sec == "FUT" and ltd:
        c.lastTradeDateOrContractMonth = ltd

    # localSymbol is informative; conId should be sufficient
    c.localSymbol = str(it.get("localSymbol") or "")
    return c


def _build_order(it: Dict[str, Any]) -> Order:
    o = Order()
    o.action = str(it.get("action") or "")
    o.orderType = "MKT"
    o.totalQuantity = float(it.get("qty") or 0.0)
    o.tif = "DAY"
    # Exit-only posture: never allow outside RTH toggles here; keep defaults.
    return o


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--client-id", type=int, required=True)
    ap.add_argument("--connect-timeout-s", type=float, default=8.0)
    ap.add_argument("--order-timeout-s", type=float, default=45.0)

    ap.add_argument("--control-plane", default=str(DATA_DIR / "control_plane.json"))
    ap.add_argument("--plan", default=str(DATA_DIR / "reset_plan.json"))

    ap.add_argument("--out", default=str(DATA_DIR / "reset_exec_report.json"))

    # Second factor confirmation for REAL placing (even if enabled in config)
    ap.add_argument("--confirm-paper", action="store_true")

    args = ap.parse_args(argv)

    lock_path = LOGS_DIR / "reset_executor_paper.lock"
    try:
        _lock_or_fail(lock_path)

        ts = _utc_now_iso()

        # Hard safety: require stop.flag to prevent autoloop concurrency
        if not STOP_FLAG.exists():
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "error": f"STOP_FLAG_REQUIRED: create {str(STOP_FLAG)} before reset execution",
                "executed": False,
            }
            _write_json(Path(args.out), out)
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        cp = _read_json(Path(args.control_plane))
        plan = _read_json(Path(args.plan))

        global_mode = _u(cp.get("global_mode"))
        execution_mode = _u(cp.get("execution_mode"))
        enable_paper = bool(cp.get("enable_paper_execution", False))

        # Always prepare preview summary
        items = list(plan.get("items") or [])
        total = len(items)
        unknown = sum(1 for it in items if _u(it.get("kind")) == "CLOSE_UNKNOWN")
        allow = sum(1 for it in items if _u(it.get("kind")) == "CLOSE_ALLOWLIST")

        # Gate checks
        warnings: List[str] = []
        if global_mode and global_mode != "ONLY_EXITS":
            warnings.append(f"global_mode={global_mode} (expected ONLY_EXITS)")

        # HARD BLOCK: until user explicitly enables + confirm flag
        can_execute = (
            (execution_mode == "PAPER") and enable_paper and bool(args.confirm_paper)
        )
        if not can_execute:
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": True,
                "exit_code": 0,
                "executed": False,
                "block_reason": "PAPER_EXECUTION_NOT_ENABLED",
                "summary": {
                    "global_mode": global_mode,
                    "execution_mode": execution_mode,
                    "enable_paper_execution": enable_paper,
                    "confirm_paper_flag": bool(args.confirm_paper),
                    "reset_items_total": total,
                    "reset_items_unknown": unknown,
                    "reset_items_allowlist": allow,
                },
                "warnings": warnings
                + [
                    "No orders sent. To allow real placing, set execution_mode=PAPER, enable_paper_execution=true AND run with --confirm-paper AND (in chat) issue ENABLE PAPER EXECUTION."
                ],
            }
            _write_json(Path(args.out), out)
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 0

        # If we ever get here, it's REAL order placing.
        # NOTE: In your process, this is still forbidden until you issue the chat command.
        # Treat this block as "armed" code only.

        app = _App()
        try:
            app.connect(args.host, int(args.port), int(args.client_id))
        except Exception as e:
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "error": f"connect_failed: {type(e).__name__}: {e}",
                "executed": False,
            }
            _write_json(Path(args.out), out)
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        th = threading.Thread(target=app.run, daemon=True)
        th.start()

        if not app._next_valid_id_evt.wait(timeout=float(args.connect_timeout_s)):
            try:
                app.disconnect()
            except Exception:
                pass
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "error": f"handshake_timeout: no nextValidId within {args.connect_timeout_s}s",
                "executed": False,
            }
            _write_json(Path(args.out), out)
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        order_id = int(app.next_order_id or 0)
        placed: List[Dict[str, Any]] = []
        order_ids: List[int] = []

        # Place sequentially to keep risk contained
        for it in items:
            qty = float(it.get("qty") or 0.0)
            if qty <= 0:
                continue

            contract = _build_contract(it)
            order = _build_order(it)

            oid = order_id
            order_id += 1

            # Record idempotency key in-memory (file-based ledger can be added later)
            placed.append(
                {
                    "orderId": oid,
                    "kind": it.get("kind", ""),
                    "symbol": it.get("symbol", ""),
                    "secType": it.get("secType", ""),
                    "conId": int(it.get("conId") or 0),
                    "action": order.action,
                    "qty": order.totalQuantity,
                    "key": _id_key(it),
                }
            )

            app.placeOrder(oid, contract, order)
            order_ids.append(oid)
            time.sleep(0.2)

        ok_terminal, statuses = app.wait_all_terminal(
            order_ids, timeout_s=float(args.order_timeout_s)
        )

        try:
            app.disconnect()
        except Exception:
            pass

        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": bool(ok_terminal),
            "exit_code": 0 if ok_terminal else 1,
            "executed": True,
            "placed": placed,
            "statuses": statuses,
            "errors": app.errors[:5],
            "summary": {
                "placed_orders": len(order_ids),
                "reset_items_total": total,
                "reset_items_unknown": unknown,
                "reset_items_allowlist": allow,
            },
            "warnings": warnings,
        }
        _write_json(Path(args.out), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return out["exit_code"]

    except Exception as e:
        out = {
            "schema": SCHEMA,
            "ts_utc": _utc_now_iso(),
            "ok": False,
            "exit_code": 2,
            "error": f"{type(e).__name__}: {e}",
            "executed": False,
        }
        try:
            _write_json(Path(args.out), out)
        except Exception:
            pass
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2
    finally:
        _unlock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
