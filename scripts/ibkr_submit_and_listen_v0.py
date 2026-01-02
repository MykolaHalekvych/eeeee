from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order

# ------------------------------------------------------------
# Standard:
# - JSON-only stdout (exactly one JSON)
# - Safe-by-default: no order unless --confirm
# - Exit codes: 0=OK, 1=ORDER/EVAL FAIL, 2=INFRA/CONNECT FAIL
# ------------------------------------------------------------

INFO_CODES = {2104, 2106, 2158}  # farms OK, not errors


@dataclass
class Obs:
    kind: str
    ts_utc: str
    payload: Dict[str, Any]


def _ts_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_bool_set(o: Any, attr: str, value: bool) -> None:
    if hasattr(o, attr):
        try:
            setattr(o, attr, bool(value))
        except Exception:
            pass


def _sanitize_deprecated_order_attrs(o: Order) -> Dict[str, Any]:
    """
    For TWS 983+ the following attributes are desupported and may trigger rejects:
      - eTradeOnly (10268)
      - firmQuoteOnly (10269)
      - nbboPriceCap (10270)
    Practical fix: force eTradeOnly/firmQuoteOnly off.
    IMPORTANT: DO NOT set nbboPriceCap at all (even 0.0 can be treated as "set" and rejected).
    """
    applied: Dict[str, Any] = {}
    if hasattr(o, "eTradeOnly"):
        _safe_bool_set(o, "eTradeOnly", False)
        applied["eTradeOnly"] = False
    if hasattr(o, "firmQuoteOnly"):
        _safe_bool_set(o, "firmQuoteOnly", False)
        applied["firmQuoteOnly"] = False
    # DO NOT touch nbboPriceCap here.
    return applied


class App(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._ready = threading.Event()
        self._lock = threading.Lock()

        self.next_valid_id: Optional[int] = None
        self.obs: List[Obs] = []
        self.errors: List[Tuple[int, int, str]] = []

    def nextValidId(self, orderId: int) -> None:
        with self._lock:
            self.next_valid_id = int(orderId)
            self.obs.append(Obs("NEXT_VALID_ID", _ts_utc(), {"orderId": int(orderId)}))
        self._ready.set()

    def error(self, reqId: int, errorCode: int, errorString: str) -> None:
        with self._lock:
            self.errors.append((reqId, errorCode, errorString))
            self.obs.append(
                Obs("ERROR", _ts_utc(), {"reqId": int(reqId), "code": int(errorCode), "msg": str(errorString)})
            )

    def orderStatus(
        self,
        orderId,
        status,
        filled,
        remaining,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        with self._lock:
            self.obs.append(
                Obs(
                    "ORDER_STATUS",
                    _ts_utc(),
                    {
                        "orderId": int(orderId),
                        "status": str(status),
                        "filled": float(filled) if filled is not None else None,
                        "remaining": float(remaining) if remaining is not None else None,
                        "avgFillPrice": avgFillPrice,
                        "permId": int(permId),
                        "clientId": int(clientId),
                        "whyHeld": str(whyHeld or ""),
                    },
                )
            )

    def openOrder(self, orderId, contract, order, orderState) -> None:
        with self._lock:
            self.obs.append(
                Obs(
                    "OPEN_ORDER",
                    _ts_utc(),
                    {
                        "orderId": int(orderId),
                        "permId": int(getattr(order, "permId", 0) or 0),
                        "clientId": int(getattr(order, "clientId", 0) or 0),
                        "account": str(getattr(order, "account", "") or ""),
                        "symbol": str(getattr(contract, "symbol", "") or ""),
                        "secType": str(getattr(contract, "secType", "") or ""),
                        "conId": int(getattr(contract, "conId", 0) or 0),
                        "exchange": str(getattr(contract, "exchange", "") or ""),
                        "currency": str(getattr(contract, "currency", "") or ""),
                        "action": str(getattr(order, "action", "") or ""),
                        "orderType": str(getattr(order, "orderType", "") or ""),
                        "tif": str(getattr(order, "tif", "") or ""),
                        "totalQuantity": str(getattr(order, "totalQuantity", "") or ""),
                        "status": str(getattr(orderState, "status", "") or ""),
                    },
                )
            )


def _build_contract(args: argparse.Namespace) -> Contract:
    c = Contract()
    c.conId = int(args.conId)
    c.symbol = str(args.symbol)
    c.secType = str(args.secType)
    c.exchange = str(args.exchange)
    c.currency = str(args.currency)
    return c


def _build_order(args: argparse.Namespace) -> Tuple[Order, Dict[str, Any]]:
    o = Order()
    o.action = str(args.action).upper()
    o.totalQuantity = float(args.qty)
    o.orderType = str(args.order_type).upper()
    o.tif = str(args.tif).upper()

    if args.account.strip():
        o.account = args.account.strip()

    if hasattr(o, "outsideRth"):
        try:
            o.outsideRth = bool(args.outside_rth)
        except Exception:
            pass

    if o.orderType == "LMT":
        if args.lmt_price is None:
            raise ValueError("LMT requires --lmt-price")
        if hasattr(o, "lmtPrice"):
            o.lmtPrice = float(args.lmt_price)
        else:
            raise ValueError("Order.lmtPrice attribute not available in this ibapi build")

    sanitized = _sanitize_deprecated_order_attrs(o)
    return o, sanitized


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=77)
    ap.add_argument("--connect-timeout-s", type=float, default=8.0)
    ap.add_argument("--listen-s", type=float, default=15.0)

    ap.add_argument("--account", default="")
    ap.add_argument("--conId", type=int, required=True)
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--secType", default="STK")
    ap.add_argument("--exchange", default="SMART")
    ap.add_argument("--currency", default="USD")

    ap.add_argument("--action", default="SELL")
    ap.add_argument("--qty", type=float, default=2.0)
    ap.add_argument("--order-type", default="MKT", choices=["MKT", "LMT"])
    ap.add_argument("--lmt-price", type=float, default=None)
    ap.add_argument("--tif", default="DAY")
    ap.add_argument("--outside-rth", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()

    if args.qty <= 0:
        print(json.dumps({"ok": False, "exit_code": 1, "error": "BAD_QTY"}))
        return 1

    app = App()
    try:
        app.connect(args.host, int(args.port), int(args.client_id))
    except Exception as e:
        print(json.dumps({"ok": False, "exit_code": 2, "error": "CONNECT_EXCEPTION", "detail": str(e)}))
        return 2

    th = threading.Thread(target=app.run, daemon=True)
    th.start()

    if not app._ready.wait(args.connect_timeout_s):
        try:
            app.disconnect()
        except Exception:
            pass
        print(json.dumps({"ok": False, "exit_code": 2, "error": "NO_NEXT_VALID_ID"}))
        return 2

    if not args.confirm:
        with app._lock:
            out = {
                "schema": "ibkr_submit_and_listen_v0",
                "ts_utc": _ts_utc(),
                "ok": True,
                "exit_code": 0,
                "placed": False,
                "note": "SAFE: no --confirm",
                "obs": [asdict(x) for x in app.obs],
            }
        print(json.dumps(out, ensure_ascii=False))
        try:
            app.disconnect()
        except Exception:
            pass
        return 0

    try:
        c = _build_contract(args)
        o, sanitized = _build_order(args)
    except Exception as e:
        try:
            app.disconnect()
        except Exception:
            pass
        print(json.dumps({"ok": False, "exit_code": 1, "error": "BUILD_FAILED", "detail": str(e)}))
        return 1

    with app._lock:
        placed_order_id = int(app.next_valid_id or 0)

    place_error: Optional[str] = None
    try:
        app.placeOrder(int(placed_order_id), c, o)
    except Exception as e:
        place_error = str(e)

    t_end = time.time() + float(args.listen_s)
    while time.time() < t_end:
        time.sleep(0.2)

    # classify errors tied to this orderId
    with app._lock:
        order_errors = [
            {"reqId": rid, "code": code, "msg": msg}
            for (rid, code, msg) in app.errors
            if int(rid) == int(placed_order_id) and int(code) not in INFO_CODES
        ]
        ok = (place_error is None) and (len(order_errors) == 0)
        exit_code = 0 if ok else 1

        out = {
            "schema": "ibkr_submit_and_listen_v0",
            "ts_utc": _ts_utc(),
            "ok": ok,
            "exit_code": exit_code,
            "placed": True,
            "placed_orderId": placed_order_id,
            "place_error": place_error,
            "order_errors": order_errors,
            "contract": {"conId": args.conId, "symbol": args.symbol, "secType": args.secType, "exchange": args.exchange, "currency": args.currency},
            "order": {"action": args.action, "qty": args.qty, "order_type": args.order_type, "lmt_price": args.lmt_price, "tif": args.tif, "outside_rth": bool(args.outside_rth), "sanitized": sanitized},
            "obs": [asdict(x) for x in app.obs],
        }

    print(json.dumps(out, ensure_ascii=False))
    try:
        app.disconnect()
    except Exception:
        pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
