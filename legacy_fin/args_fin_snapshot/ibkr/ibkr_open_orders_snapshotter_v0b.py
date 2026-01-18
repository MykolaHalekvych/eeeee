# args/ibkr/ibkr_open_orders_snapshotter_v0b.py
from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_state import OrderState

SCHEMA_VERSION = "ibkr_open_orders_snapshot_v0b"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class OpenOrderRow:
    orderId: int
    permId: int
    clientId: int
    account: str
    status: str

    conId: int
    symbol: str
    localSymbol: str
    secType: str
    currency: str
    exchange: str
    lastTradeDateOrContractMonth: str

    action: str
    totalQuantity: float
    orderType: str
    tif: str


class _App(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._next_valid_id_evt = threading.Event()
        self._open_end_evt = threading.Event()
        self._lock = threading.Lock()

        self.rows: List[OpenOrderRow] = []
        self._status_by_oid: Dict[int, str] = {}

        # Separate "warnings" vs "errors"
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def nextValidId(self, orderId: int) -> None:
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

        # Treat IB informational farm messages as warnings (not errors)
        if int(errorCode) in {2104, 2106, 2158}:
            with self._lock:
                self.warnings.append(msg)
            return

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
            self._status_by_oid[int(orderId)] = str(status or "")

    def openOrder(
        self, orderId: int, contract: Contract, order: Order, orderState: OrderState
    ) -> None:
        oid = int(orderId)
        with self._lock:
            status = self._status_by_oid.get(oid, "") or str(
                getattr(orderState, "status", "") or ""
            )

        row = OpenOrderRow(
            orderId=oid,
            permId=int(getattr(order, "permId", 0) or 0),
            clientId=int(getattr(order, "clientId", 0) or 0),
            account=str(getattr(order, "account", "") or ""),
            status=str(status or ""),
            conId=int(getattr(contract, "conId", 0) or 0),
            symbol=str(getattr(contract, "symbol", "") or ""),
            localSymbol=str(getattr(contract, "localSymbol", "") or ""),
            secType=str(getattr(contract, "secType", "") or ""),
            currency=str(getattr(contract, "currency", "") or ""),
            exchange=str(getattr(contract, "exchange", "") or ""),
            lastTradeDateOrContractMonth=str(
                getattr(contract, "lastTradeDateOrContractMonth", "") or ""
            ),
            action=str(getattr(order, "action", "") or ""),
            totalQuantity=float(getattr(order, "totalQuantity", 0.0) or 0.0),
            orderType=str(getattr(order, "orderType", "") or ""),
            tif=str(getattr(order, "tif", "") or ""),
        )
        with self._lock:
            self.rows.append(row)

    def openOrderEnd(self) -> None:
        self._open_end_evt.set()


def _dedupe(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Prefer orderId when >0, else permId when >0, else fallback
    seen: Set[Tuple[int, int, str]] = set()
    out: List[Dict[str, Any]] = []
    for r in rows:
        oid = int(r.get("orderId") or 0)
        pid = int(r.get("permId") or 0)
        sym = str(r.get("symbol") or "")
        key = (oid if oid > 0 else 0, pid if pid > 0 else 0, sym)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def snapshot_open_orders(
    host: str, port: int, client_id: int, connect_timeout_s: float, timeout_s: float
) -> Dict[str, Any]:
    ts = _utc_now_iso()
    app = _App()

    try:
        app.connect(host, int(port), int(client_id))
    except Exception as e:
        return {
            "schema": SCHEMA_VERSION,
            "ts_utc": ts,
            "ok": False,
            "error": f"connect_failed: {type(e).__name__}: {e}",
            "warnings": [],
            "rows": [],
            "host": host,
            "port": port,
            "client_id": client_id,
        }

    th = threading.Thread(target=app.run, daemon=True)
    th.start()

    if not app._next_valid_id_evt.wait(timeout=connect_timeout_s):
        try:
            app.disconnect()
        except Exception:
            pass
        return {
            "schema": SCHEMA_VERSION,
            "ts_utc": ts,
            "ok": False,
            "error": f"handshake_timeout: no nextValidId within {connect_timeout_s}s",
            "warnings": [],
            "rows": [],
            "host": host,
            "port": port,
            "client_id": client_id,
        }

    # Ask for all open orders
    app.reqAllOpenOrders()
    ok = app._open_end_evt.wait(timeout=timeout_s)

    try:
        app.disconnect()
    except Exception:
        pass

    rows = _dedupe([asdict(r) for r in app.rows])

    if not ok:
        return {
            "schema": SCHEMA_VERSION,
            "ts_utc": ts,
            "ok": False,
            "error": f"timeout: no openOrderEnd within {timeout_s}s",
            "warnings": app.warnings[:10],
            "rows": rows,
            "host": host,
            "port": port,
            "client_id": client_id,
        }

    # If ok, keep error empty; warnings carry 2104/2106 noise
    return {
        "schema": SCHEMA_VERSION,
        "ts_utc": ts,
        "ok": True,
        "error": None,
        "warnings": app.warnings[:10],
        "rows": rows,
        "host": host,
        "port": port,
        "client_id": client_id,
    }


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--client-id", type=int, required=True)
    ap.add_argument("--connect-timeout-s", type=float, default=8.0)
    ap.add_argument("--timeout-s", type=float, default=25.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    out = snapshot_open_orders(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        connect_timeout_s=float(args.connect_timeout_s),
        timeout_s=float(args.timeout_s),
    )

    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")

    if args.out:
        _write_json(Path(args.out), out)

    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
