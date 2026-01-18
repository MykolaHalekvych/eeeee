from __future__ import annotations
import argparse
import json
import threading
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order


@dataclass
class OrderRec:
    src: str
    orderId: int
    permId: int
    clientId: int
    symbol: str
    secType: str
    exchange: str
    action: str
    orderType: str
    tif: str
    status: str
    whyHeld: str = ""
    msg: str = ""


class App(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self._ready = threading.Event()
        self._open_end = threading.Event()
        self._completed_end = threading.Event()
        self._lock = threading.Lock()

        self.recs: List[OrderRec] = []
        self.errors: List[Tuple[int, int, str]] = []
        self._whyHeld_by_orderId: Dict[int, str] = {}

        # tag to label openOrder source
        self._open_tag = "open"

    def nextValidId(self, orderId: int) -> None:
        self._ready.set()

    def error(self, reqId: int, errorCode: int, errorString: str) -> None:
        self.errors.append((reqId, errorCode, errorString))

    def orderStatus(
        self,
        orderId,
        status,
        filled,
        remaining,
        avgFillPrice,
        permId,
        parentId,
        lastFillPrice,
        clientId,
        whyHeld,
        mktCapPrice,
    ):
        with self._lock:
            if whyHeld:
                self._whyHeld_by_orderId[int(orderId)] = str(whyHeld)

    def openOrder(
        self, orderId: int, contract: Contract, order: Order, orderState
    ) -> None:
        permId = int(getattr(order, "permId", 0) or 0)
        clientId = int(getattr(order, "clientId", 0) or 0)
        sym = str(getattr(contract, "symbol", "") or "")
        secType = str(getattr(contract, "secType", "") or "")
        exch = str(getattr(contract, "exchange", "") or "")
        act = str(getattr(order, "action", "") or "")
        otype = str(getattr(order, "orderType", "") or "")
        tif = str(getattr(order, "tif", "") or "")
        status = str(getattr(orderState, "status", "") or "")
        why = self._whyHeld_by_orderId.get(int(orderId), "")
        self.recs.append(
            OrderRec(
                self._open_tag,
                int(orderId),
                permId,
                clientId,
                sym,
                secType,
                exch,
                act,
                otype,
                tif,
                status,
                why,
            )
        )

    def openOrderEnd(self) -> None:
        self._open_end.set()

    def completedOrder(self, contract: Contract, order: Order, orderState) -> None:
        orderId = int(getattr(order, "orderId", 0) or 0)
        permId = int(getattr(order, "permId", 0) or 0)
        clientId = int(getattr(order, "clientId", 0) or 0)
        sym = str(getattr(contract, "symbol", "") or "")
        secType = str(getattr(contract, "secType", "") or "")
        exch = str(getattr(contract, "exchange", "") or "")
        act = str(getattr(order, "action", "") or "")
        otype = str(getattr(order, "orderType", "") or "")
        tif = str(getattr(order, "tif", "") or "")
        status = str(getattr(orderState, "status", "") or "")
        self.recs.append(
            OrderRec(
                "completed",
                int(orderId),
                permId,
                clientId,
                sym,
                secType,
                exch,
                act,
                otype,
                tif,
                status,
            )
        )

    def completedOrdersEnd(self) -> None:
        self._completed_end.set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=77)
    ap.add_argument("--order-id", type=int, required=True)
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument(
        "--include-manual",
        action="store_true",
        help="CompletedOrders: include manual too (apiOnly=False)",
    )
    args = ap.parse_args()

    app = App()
    app.connect(args.host, args.port, args.client_id)
    th = threading.Thread(target=app.run, daemon=True)
    th.start()

    if not app._ready.wait(args.timeout):
        print(json.dumps({"ok": False, "error": "NO_NEXT_VALID_ID"}))
        return 2

    # 1) OPEN ORDERS (SELF)
    app._open_end.clear()
    app._open_tag = "open_self"
    app.reqOpenOrders()
    app._open_end.wait(args.timeout)

    # 2) OPEN ORDERS (ALL) - may be empty if master client id restriction
    app._open_end.clear()
    app._open_tag = "open_all"
    app.reqAllOpenOrders()
    app._open_end.wait(args.timeout)

    # 3) COMPLETED
    app._completed_end.clear()
    apiOnly = not args.include_manual
    app.reqCompletedOrders(apiOnly)
    app._completed_end.wait(args.timeout)

    matches = [asdict(r) for r in app.recs if r.orderId == args.order_id]
    open_self_cnt = sum(1 for r in app.recs if r.src == "open_self")
    open_all_cnt = sum(1 for r in app.recs if r.src == "open_all")
    completed_cnt = sum(1 for r in app.recs if r.src == "completed")

    out = {
        "ok": True,
        "orderId": args.order_id,
        "counts": {
            "open_self": open_self_cnt,
            "open_all": open_all_cnt,
            "completed": completed_cnt,
            "total_recs": len(app.recs),
        },
        "matches": matches,
        "errors_tail": app.errors[-50:],
        "notes": [
            "open_self should show orders placed by this clientId if they are active.",
            "If open_all is 0 but open_self >0 -> master client id restriction is likely.",
            "If everything is 0 and matches empty -> order likely rejected/inactive instantly or never accepted.",
        ],
    }
    print(json.dumps(out, ensure_ascii=False))

    try:
        app.disconnect()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
