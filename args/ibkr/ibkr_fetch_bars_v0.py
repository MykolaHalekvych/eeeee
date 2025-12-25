from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper


@dataclass(frozen=True)
class ConnCfg:
    host: str
    port: int
    client_id: int
    timeout_sec: int
    throttle_ms: int


@dataclass(frozen=True)
class FetchCfg:
    symbol: str
    exchange: str
    currency: str
    sec_type: str
    bar_size: str          # e.g. "5 mins"
    duration: str          # e.g. "2 D"
    what_to_show: str      # "TRADES" usually
    use_rth: int           # 0/1
    out_csv: Path


class _App(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._bars: List[Dict[str, Any]] = []
        self._done = threading.Event()
        self._err: Optional[str] = None
        self._next_req_id = 1001

    def nextValidId(self, orderId: int) -> None:
        # called after connection is established
        self._next_req_id = max(self._next_req_id, int(orderId))

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:
        # errorCode 2104/2106/2158 are info messages; not fatal
        msg = f"IBKR error reqId={reqId} code={errorCode} msg={errorString}"
        if errorCode in (2104, 2106, 2158):
            return
        self._err = msg
        self._done.set()

    def historicalData(self, reqId: int, bar) -> None:
        # bar.date for historicalData is usually "YYYYMMDD  HH:MM:SS" (or epoch for some modes)
        self._bars.append(
            {
                "date": str(bar.date),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            }
        )

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        self._done.set()


def _load_conn_cfg(repo_root: Path) -> ConnCfg:
    p = repo_root / "args" / "data" / "ibkr_connection_v0.json"
    obj = json.loads(p.read_text(encoding="utf-8"))
    return ConnCfg(
        host=str(obj["host"]),
        port=int(obj["port"]),
        client_id=int(obj["client_id"]),
        timeout_sec=int(obj.get("timeout_sec", 8)),
        throttle_ms=int(obj.get("throttle_ms", 250)),
    )


def _make_contract(cfg: FetchCfg) -> Contract:
    c = Contract()
    c.symbol = cfg.symbol
    c.secType = cfg.sec_type
    c.exchange = cfg.exchange
    c.currency = cfg.currency
    return c


def fetch_historical_bars(fetch: FetchCfg) -> Dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    conn = _load_conn_cfg(repo_root)

    app = _App()
    app.connect(conn.host, conn.port, conn.client_id)

    t = threading.Thread(target=app.run, daemon=True)
    t.start()

    # throttle a bit after connect
    time.sleep(conn.throttle_ms / 1000.0)

    req_id = app._next_req_id
    contract = _make_contract(fetch)

    app.reqHistoricalData(
        req_id,
        contract,
        "",                 # endDateTime: "" = now
        fetch.duration,
        fetch.bar_size,
        fetch.what_to_show,
        fetch.use_rth,
        1,                  # formatDate: 1 = yyyyMMdd HH:mm:ss
        False,
        [],
    )

    ok = app._done.wait(timeout=conn.timeout_sec)
    app.disconnect()

    if not ok:
        raise TimeoutError(f"Timeout waiting for historical data (timeout_sec={conn.timeout_sec})")

    if app._err:
        raise RuntimeError(app._err)

    # Write CSV in our AS format: ts,open,high,low,close,volume
    fetch.out_csv.parent.mkdir(parents=True, exist_ok=True)
    with fetch.out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in app._bars:
            # Convert "YYYYMMDD  HH:MM:SS" to ISO-ish "YYYY-MM-DDTHH:MM:SS"
            d = b["date"].strip()
            if len(d) >= 17 and d[8] == " ":
                ts = f"{d[0:4]}-{d[4:6]}-{d[6:8]}T{d[10:18]}"
            else:
                ts = d
            w.writerow([ts, b["open"], b["high"], b["low"], b["close"], b["volume"]])

    return {"bars": len(app._bars), "out_csv": str(fetch.out_csv)}


def main() -> int:
    # smoke test config (does not run unless called explicitly)
    repo_root = Path(__file__).resolve().parents[2]
    out_csv = repo_root / "args" / "data" / "hg_5m_bars_ibkr.csv"
    cfg = FetchCfg(
        symbol="HG",
        exchange="COMEX",
        currency="USD",
        sec_type="FUT",
        bar_size="5 mins",
        duration="1 D",
        what_to_show="TRADES",
        use_rth=0,
        out_csv=out_csv,
    )
    res = fetch_historical_bars(cfg)
    print("IBKR_FETCH_OK:", res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
