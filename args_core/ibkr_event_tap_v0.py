from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

from .common_v0 import append_jsonl, atomic_write_json, utc_now_iso


@dataclass
class TapState:
    ok: bool = False
    connected: bool = False
    last_error: Optional[str] = None
    events_written: int = 0


class IbkrEventTap(EWrapper, EClient):
    """
    Minimal IBKR callback tap:
    - connects
    - subscribes to open orders (reqOpenOrders), executions (reqExecutions), positions (reqPositions optional)
    - writes normalized-ish callback records to JSONL
    """

    def __init__(self, out_jsonl: Path, health_path: Path) -> None:
        EClient.__init__(self, self)
        self.out_jsonl = out_jsonl
        self.health_path = health_path
        self.state = TapState()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ----- lifecycle -----

    def stop(self) -> None:
        self._stop.set()

    def _write_event(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            append_jsonl(self.out_jsonl, rec)
            self.state.events_written += 1

    def _write_health(self) -> None:
        atomic_write_json(
            self.health_path,
            {
                "schema": "ibkr_event_tap_health_v0",
                "ts_utc": utc_now_iso(),
                "ok": self.state.ok,
                "connected": self.state.connected,
                "events_written": self.state.events_written,
                "last_error": self.state.last_error,
            },
        )

    # ----- callbacks -----

    def nextValidId(self, orderId: int) -> None:
        self.state.connected = True
        self.state.ok = True
        self._write_health()
        # Pull current live state
        try:
            self.reqOpenOrders()
        except Exception:
            pass
        try:
            self.reqExecutions(1, None)
        except Exception:
            pass

    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        rec = {
            "ts_utc": utc_now_iso(),
            "type": "ERROR",
            "reqId": reqId,
            "code": errorCode,
            "msg": errorString,
        }
        if advancedOrderRejectJson:
            rec["advancedRejectJson"] = advancedOrderRejectJson
        self._write_event(rec)
        # Don't fail health on INFO codes
        self.state.last_error = f"{errorCode}:{errorString}"
        self._write_health()

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
        rec = {
            "ts_utc": utc_now_iso(),
            "type": "ORDER_STATUS",
            "orderId": int(orderId),
            "status": str(status),
            "filled": float(filled),
            "remaining": float(remaining),
            "avgFillPrice": float(avgFillPrice),
            "permId": int(permId),
            "clientId": int(clientId),
            "whyHeld": str(whyHeld or ""),
        }
        self._write_event(rec)

    def openOrder(self, orderId: int, contract, order, orderState) -> None:
        # Minimal contract/order fields for later correlation
        rec = {
            "ts_utc": utc_now_iso(),
            "type": "OPEN_ORDER",
            "orderId": int(orderId),
            "symbol": getattr(contract, "symbol", ""),
            "secType": getattr(contract, "secType", ""),
            "action": getattr(order, "action", ""),
            "totalQuantity": float(getattr(order, "totalQuantity", 0.0)),
            "orderType": getattr(order, "orderType", ""),
            "tif": getattr(order, "tif", ""),
            "status": getattr(orderState, "status", ""),
        }
        self._write_event(rec)

    def execDetails(self, reqId: int, contract, execution) -> None:
        rec = {
            "ts_utc": utc_now_iso(),
            "type": "EXEC_DETAILS",
            "reqId": int(reqId),
            "orderId": int(getattr(execution, "orderId", 0)),
            "permId": int(getattr(execution, "permId", 0)),
            "symbol": getattr(contract, "symbol", ""),
            "side": getattr(execution, "side", ""),
            "shares": float(getattr(execution, "shares", 0.0)),
            "price": float(getattr(execution, "price", 0.0)),
            "execId": getattr(execution, "execId", ""),
            "time": getattr(execution, "time", ""),
        }
        self._write_event(rec)

    # ----- run loop -----

    def run_loop(self) -> None:
        self._write_health()
        while not self._stop.is_set():
            try:
                self._write_health()
            except Exception:
                pass
            time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=77)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--out", default=r"args\data\ibkr_events_live.jsonl")
    ap.add_argument("--health", default=r"args\data\ibkr_event_tap_health.json")
    args = ap.parse_args()

    repo = Path.cwd()
    out = repo / args.out
    health = repo / args.health
    out.parent.mkdir(parents=True, exist_ok=True)
    health.parent.mkdir(parents=True, exist_ok=True)

    tap = IbkrEventTap(out, health)

    tap.connect(args.host, args.port, args.client_id)

    # Start IBKR network thread
    t = threading.Thread(target=tap.run, daemon=True)
    t.start()

    # Wait for events
    end = time.time() + float(args.seconds)
    while time.time() < end:
        time.sleep(0.2)

    tap.stop()
    try:
        tap.disconnect()
    except Exception:
        pass

    # final health
    tap._write_health()

    print(
        json.dumps(
            {
                "ok": tap.state.ok,
                "connected": tap.state.connected,
                "events_written": tap.state.events_written,
                "out": str(out),
                "health": str(health),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
