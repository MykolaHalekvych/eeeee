# args/ibkr/ibkr_positions_snapshotter_v0.py
from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract


SCHEMA_VERSION = "ibkr_positions_snapshot_v0"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class PositionRow:
    account: str
    conId: int
    symbol: str
    localSymbol: str
    secType: str
    currency: str
    exchange: str
    lastTradeDateOrContractMonth: str
    position: float
    avgCost: float


class _App(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._next_valid_id_evt = threading.Event()
        self._positions_end_evt = threading.Event()
        self._lock = threading.Lock()

        self.next_valid_id: Optional[int] = None
        self.rows: List[PositionRow] = []
        self.errors: List[str] = []
        self._connected_ok: bool = False

    # ---- connection lifecycle ----
    def nextValidId(self, orderId: int) -> None:
        self.next_valid_id = int(orderId)
        self._connected_ok = True
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

    # ---- positions ----
    def position(
        self, account: str, contract: Contract, pos: float, avgCost: float
    ) -> None:
        row = PositionRow(
            account=str(account or ""),
            conId=int(getattr(contract, "conId", 0) or 0),
            symbol=str(getattr(contract, "symbol", "") or ""),
            localSymbol=str(getattr(contract, "localSymbol", "") or ""),
            secType=str(getattr(contract, "secType", "") or ""),
            currency=str(getattr(contract, "currency", "") or ""),
            exchange=str(getattr(contract, "exchange", "") or ""),
            lastTradeDateOrContractMonth=str(
                getattr(contract, "lastTradeDateOrContractMonth", "") or ""
            ),
            position=float(pos or 0.0),
            avgCost=float(avgCost or 0.0),
        )
        with self._lock:
            self.rows.append(row)

    def positionEnd(self) -> None:
        self._positions_end_evt.set()


def snapshot_positions(
    host: str,
    port: int,
    client_id: int,
    connect_timeout_s: float,
    timeout_s: float,
) -> Dict[str, Any]:
    ts = _utc_now_iso()
    app = _App()

    # Connect
    try:
        app.connect(host, int(port), int(client_id))
    except Exception as e:
        return {
            "schema": SCHEMA_VERSION,
            "ts_utc": ts,
            "ok": False,
            "error": f"connect_failed: {type(e).__name__}: {e}",
            "rows": [],
            "host": host,
            "port": port,
            "client_id": client_id,
        }

    t = threading.Thread(target=app.run, daemon=True)
    t.start()

    # Handshake-safe: wait nextValidId
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
            "rows": [],
            "host": host,
            "port": port,
            "client_id": client_id,
        }

    # Request positions
    app.reqPositions()

    # Wait end
    ok = app._positions_end_evt.wait(timeout=timeout_s)

    # Disconnect
    try:
        app.disconnect()
    except Exception:
        pass

    rows = [asdict(r) for r in app.rows]
    err = None
    if not ok:
        err = f"timeout: no positionEnd within {timeout_s}s"
    elif app.errors:
        # Not fatal by default, but keep the most relevant
        err = "; ".join(app.errors[:3])

    return {
        "schema": SCHEMA_VERSION,
        "ts_utc": ts,
        "ok": ok,
        "error": err,
        "rows": rows,
        "host": host,
        "port": port,
        "client_id": client_id,
    }


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--client-id", type=int, required=True)
    p.add_argument("--connect-timeout-s", type=float, default=8.0)
    p.add_argument("--timeout-s", type=float, default=25.0)
    p.add_argument("--out", default="")

    args = p.parse_args(argv)

    out = snapshot_positions(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        connect_timeout_s=args.connect_timeout_s,
        timeout_s=args.timeout_s,
    )

    # stdout: exactly one JSON
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.write("\n")

    if args.out:
        _write_json(Path(args.out), out)

    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
