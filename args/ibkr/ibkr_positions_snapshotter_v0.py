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
        self.done_ev = threading.Event()
        self.errs: List[Dict[str, Any]] = []

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
            # best-effort
            pass

    def positionEnd(self) -> None:
        self.done_ev.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="") -> None:
        # record all errors, but some are informational
        try:
            self.errs.append(
                {
                    "reqId": reqId,
                    "code": int(errorCode),
                    "msg": str(errorString),
                }
            )
        except Exception:
            pass

    def has_fatal_error(self) -> Optional[str]:
        # treat connection / permission / critical errors as fatal
        for e in self.errs:
            c = int(e.get("code", -1))
            # 502/503/504 are common connection problems; keep generic
            if c in (502, 503, 504, 1100, 1101, 1102):
                return f"{c}:{e.get('msg')}"
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="IBKR positions snapshotter (v0)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=51)
    ap.add_argument("--timeout-s", type=int, default=25)
    ap.add_argument("--out", default="", help="Output JSON path (default args/data/ibkr_positions_live.json)")
    args = ap.parse_args()

    repo = _repo_root()
    out_path = Path(args.out) if args.out else (repo / "args" / "data" / "ibkr_positions_live.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    app = PosApp()

    try:
        app.connect(args.host, args.port, clientId=int(args.client_id))
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

    # Start event loop in background
    th = threading.Thread(target=app.run, daemon=True)
    th.start()

    # Request positions
    try:
        app.reqPositions()
    except Exception as e:
        obj = {
            "schema": "ibkr_positions_snapshot_v0",
            "ts_utc": _iso_utc_now(),
            "ok": False,
            "error": f"REQ_EXCEPTION:{type(e).__name__}:{e}",
            "rows": [],
            "errs": app.errs,
        }
        out_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(obj, ensure_ascii=False))
        try:
            app.disconnect()
        except Exception:
            pass
        return 2

    ok_done = app.done_ev.wait(timeout=float(args.timeout_s))

    # Cancel positions stream (best-effort)
    try:
        app.cancelPositions()
    except Exception:
        pass

    # Disconnect
    try:
        app.disconnect()
    except Exception:
        pass

    fatal = app.has_fatal_error()
    if not ok_done:
        fatal = fatal or "TIMEOUT_NO_POSITION_END"

    obj = {
        "schema": "ibkr_positions_snapshot_v0",
        "ts_utc": _iso_utc_now(),
        "ok": (fatal is None),
        "error": fatal,
        "rows": [r.to_dict() for r in app.rows],
        "errs": app.errs,
    }

    out_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(obj, ensure_ascii=False))
    return 0 if fatal is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
