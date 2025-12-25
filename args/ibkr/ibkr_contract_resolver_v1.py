from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ibapi.client import EClient
from ibapi.common import TickerId
from ibapi.contract import Contract, ContractDetails
from ibapi.wrapper import EWrapper


@dataclass(frozen=True)
class IbkrConn:
    host: str
    port: int
    client_id: int


def load_ibkr_connection(path: Path) -> IbkrConn:
    if not path.exists():
        # Safe defaults for local TWS/Gateway
        return IbkrConn(host="localhost", port=7497, client_id=101)

    obj = json.loads(path.read_text(encoding="utf-8"))
    host = str(obj.get("host") or "localhost")
    port = int(obj.get("port") or 7497)
    client_id = int(obj.get("client_id") or obj.get("clientId") or 101)
    return IbkrConn(host=host, port=port, client_id=client_id)


def make_hg_fut_contract(exchange: str = "COMEX", currency: str = "USD") -> Contract:
    c = Contract()
    c.secType = "FUT"
    c.symbol = "HG"
    c.exchange = exchange
    c.currency = currency
    return c


def make_hg_contfut_contract(exchange: str = "COMEX", currency: str = "USD") -> Contract:
    # Continuous futures (useful for data); trading still needs a конкретный expiry
    c = Contract()
    c.secType = "CONTFUT"
    c.symbol = "HG"
    c.exchange = exchange
    c.currency = currency
    return c


class _ContractDetailsApp(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._connected = threading.Event()
        self._done = threading.Event()
        self._details: List[ContractDetails] = []
        self._errors: List[Tuple[int, int, str]] = []
        self._req_id: Optional[int] = None

    # ---- lifecycle ----

    def nextValidId(self, orderId: int) -> None:
        self._connected.set()

    def error(self, reqId: TickerId, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:
        self._errors.append((int(reqId), int(errorCode), str(errorString)))

    # ---- contract details ----

    def contractDetails(self, reqId: int, contractDetails: ContractDetails) -> None:
        self._details.append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:
        self._done.set()


def fetch_contract_details(
    conn: IbkrConn,
    contract: Contract,
    timeout_s: float = 20.0,
) -> Tuple[List[ContractDetails], List[Tuple[int, int, str]]]:
    app = _ContractDetailsApp()
    app.connect(conn.host, conn.port, conn.client_id)

    t = threading.Thread(target=app.run, daemon=True)
    t.start()

    if not app._connected.wait(timeout=timeout_s):
        try:
            app.disconnect()
        except Exception:
            pass
        return [], [(0, 0, "Timeout waiting for nextValidId (IBKR not connected?)")]

    req_id = 9001
    app._req_id = req_id
    app.reqContractDetails(req_id, contract)

    app._done.wait(timeout=timeout_s)

    # small grace
    time.sleep(0.2)

    try:
        app.disconnect()
    except Exception:
        pass

    return app._details, app._errors


def contract_details_to_dict(cd: ContractDetails) -> Dict[str, Any]:
    c = cd.contract
    return {
        "conId": getattr(c, "conId", None),
        "symbol": getattr(c, "symbol", None),
        "localSymbol": getattr(c, "localSymbol", None),
        "tradingClass": getattr(c, "tradingClass", None),
        "secType": getattr(c, "secType", None),
        "exchange": getattr(c, "exchange", None),
        "primaryExchange": getattr(c, "primaryExchange", None),
        "currency": getattr(c, "currency", None),
        "lastTradeDateOrContractMonth": getattr(c, "lastTradeDateOrContractMonth", None),
        "multiplier": getattr(c, "multiplier", None),
        "includeExpired": getattr(c, "includeExpired", None),
        # Details side (best-effort)
        "marketName": getattr(cd, "marketName", None),
        "minTick": getattr(cd, "minTick", None),
        "validExchanges": getattr(cd, "validExchanges", None),
        "longName": getattr(cd, "longName", None),
        "contractMonth": getattr(cd, "contractMonth", None),
        "timeZoneId": getattr(cd, "timeZoneId", None),
    }


def _parse_yyyymm(last_trade: Any) -> Optional[int]:
    if not isinstance(last_trade, str):
        return None
    s = "".join(ch for ch in last_trade if ch.isdigit())
    if len(s) < 6:
        return None
    try:
        return int(s[:6])
    except Exception:
        return None


def select_front_month(details: List[ContractDetails], now_utc: Optional[datetime] = None) -> Optional[ContractDetails]:
    if not details:
        return None
    now = now_utc or datetime.now(timezone.utc)
    now_yyyymm = now.year * 100 + now.month

    scored: List[Tuple[int, int, ContractDetails]] = []
    for cd in details:
        yyyymm = _parse_yyyymm(getattr(cd.contract, "lastTradeDateOrContractMonth", None))
        if yyyymm is None:
            continue
        # primary score: >= now, secondary: yyyymm itself
        primary = 0 if yyyymm >= now_yyyymm else 1
        scored.append((primary, yyyymm, cd))

    if not scored:
        return None

    scored.sort(key=lambda x: (x[0], x[1]))
    # If there are >=now months, the best is (0, smallest yyyymm). Otherwise (1, smallest yyyymm).
    return scored[0][2]


def utc_now_str() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
