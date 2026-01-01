from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class PositionRow:
    account: str
    symbol: str
    secType: str
    currency: str
    position: float
    avgCost: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "symbol": self.symbol,
            "secType": self.secType,
            "currency": self.currency,
            "position": self.position,
            "avgCost": self.avgCost,
        }


class PosApp(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self.rows: List[PositionRow] = []
        self.errs: List[Dict[str, Any]] = []

        self.connected_ev = threading.Event()
        self.done_ev = threading.Event()

    # handshake: called when connection is established
    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.connected_ev.set()

    def position(self, account: str, contract, pos: float, avgCost: float) -> None:
        try:
            self.rows.append(
                PositionRow(
                    account=str(account),
                    symbol=str(getattr(contract, "symbol", "") or ""),
                    secType=str(getattr(contract, "secType", "") or ""),
                    currency=str(getattr(contract, "currency", "") or ""),
                    position=float(pos),
                    avgCost=float(avgCost),
                )
            )
        except Exception:
            pass

    def positionEnd(self) -> None:
        self.done_ev.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="") -> None:
        try:
            self.errs.append({"reqId": reqId, "code": int(errorCode), "msg": str(errorString)})
            # if connection drops, unblock waits
            if int(errorCode) in (502, 503, 504):
                self.connected_ev.set()
                self.done_ev.set()
        except Exception:
            pass

    def fatal_error(self) -> Optional[str]:
        # treat these as fatal
        for e in self.errs:
            c = int(e.get("code", -1))
            if c in (502, 503, 504, 1100, 1101, 1102):
                return f"{c}:{e.get('msg')}"
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="IBKR positions snapshotter (v0, handshake-safe)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=77)
    ap.add_argument("--timeout-s", type=int, default=25)
    ap.add_argument("--connect-timeout-s", type=int, default=8)
    ap.add_argument("--out", default="", help="Output JSON path (default args/data/ibkr_positions_live.json)")
    args = ap.parse_args()

    repo = _repo_root()
    out_path = Path(args.out) if args.out else (repo / "args" / "data" / "ibkr_positions_live.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    app = PosApp()
    try:
        app.connect(args.host, int(args.port), clientId=int(args.client_id))
    except Exception as e:
        obj = {
            "schema": "ibkr_positions_snapshot_v0",
            "ts_utc": _iso_utc_now(),
            "ok": False,
            "error": f"CONNECT_EXCEPTION:{type(e).__name__}:{e}",
            "rows": [],
            "errs": [],
        }
        out_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(obj, ensure_ascii=False))
        return 2

    th = threading.Thread(target=app.run, daemon=True)
    th.start()

    # wait for handshake
    ok_conn = app.connected_ev.wait(timeout=float(args.connect_timeout_s))
    fatal = app.fatal_error()
    if (not ok_conn) and (fatal is None):
        fatal = "CONNECT_TIMEOUT_NO_NEXTVALIDID"

    if fatal is None:
        try:
            app.reqPositions()
        except Exception as e:
            fatal = f"REQ_EXCEPTION:{type(e).__name__}:{e}"

    ok_done = app.done_ev.wait(timeout=float(args.timeout_s)) if fatal is None else False

    # best-effort cleanup
    try:
        app.cancelPositions()
    except Exception:
        pass
    try:
        app.disconnect()
    except Exception:
        pass

    fatal = fatal or app.fatal_error()
    if (fatal is None) and (not ok_done):
        fatal = "TIMEOUT_NO_POSITION_END"

    obj = {
        "schema": "ibkr_positions_snapshot_v0",
        "ts_utc": _iso_utc_now(),
        "ok": (fatal is None),
        "error": fatal,
        "rows": [r.to_dict() for r in app.rows],
        "errs": app.errs,
        "client_id": int(args.client_id),
    }
    out_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(obj, ensure_ascii=False))
    return 0 if fatal is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
