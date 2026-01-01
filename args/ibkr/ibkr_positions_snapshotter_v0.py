from __future__ import annotations

import argparse
import json
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
        self._rows: List[PositionRow] = []
        self._done = False
        self._err: Optional[str] = None

    # positions
    def position(self, account: str, contract, pos: float, avgCost: float) -> None:
        try:
            self._rows.append(
                PositionRow(
                    account=account,
                    symbol=str(getattr(contract, "symbol", "")),
                    secType=str(getattr(contract, "secType", "")),
                    currency=str(getattr(contract, "currency", "")),
                    position=float(pos),
                    avgCost=float(avgCost),
                )
            )
        except Exception:
            pass

    def positionEnd(self) -> None:
        self._done = True

    # errors
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="") -> None:
        # don't fail on harmless connection notes; store first real error
        if self._err is None and int(errorCode) not in (2104, 2106, 2158):
            self._err = f"{errorCode}:{errorString}"


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
    app.connect(args.host, args.port, clientId=args.client_id)

    t0 = time.time()
    app.reqPositions()

    while not app._done and (time.time() - t0) < float(args.timeout_s):
        app.run()
        # ibapi run() blocks in a loop; this line typically won't be reached often

    # best-effort disconnect
    try:
        app.disconnect()
    except Exception:
        pass

    obj = {
        "schema": "ibkr_positions_snapshot_v0",
        "ts_utc": _iso_utc_now(),
        "ok": (app._err is None),
        "error": app._err,
        "rows": [r.to_dict() for r in app._rows],
    }

    out_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(obj, ensure_ascii=False))
    return 0 if app._err is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
