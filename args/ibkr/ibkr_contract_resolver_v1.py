from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ibapi.client import EClient
from ibapi.common import TickerId
from ibapi.contract import Contract, ContractDetails
from ibapi.wrapper import EWrapper


ROLL_WINDOW_DAYS_DEFAULT = 7


@dataclass(frozen=True)
class IbkrConn:
    host: str
    port: int
    client_id: int


def load_ibkr_connection(path: Path) -> IbkrConn:
    if not path.exists():
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

    def nextValidId(self, orderId: int) -> None:
        self._connected.set()

    def error(self, reqId: TickerId, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:
        self._errors.append((int(reqId), int(errorCode), str(errorString)))

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
        # details side
        "marketName": getattr(cd, "marketName", None),
        "minTick": getattr(cd, "minTick", None),
        "validExchanges": getattr(cd, "validExchanges", None),
        "longName": getattr(cd, "longName", None),
        "contractMonth": getattr(cd, "contractMonth", None),
        "timeZoneId": getattr(cd, "timeZoneId", None),
    }


def _digits(s: Any) -> str:
    if not isinstance(s, str):
        return ""
    return "".join(ch for ch in s if ch.isdigit())


def _parse_last_trade_date(x: Any) -> Optional[date]:
    """
    IB returns lastTradeDateOrContractMonth as:
      - YYYYMMDD
      - YYYYMM (sometimes)
      - YYYYMMDD HH:MM:SS (rare)
    We prefer YYYYMMDD if present.
    """
    s = _digits(x)
    if len(s) >= 8:
        try:
            return datetime.strptime(s[:8], "%Y%m%d").date()
        except Exception:
            return None
    if len(s) >= 6:
        # Month-only fallback: treat as first day of month (conservative for selection)
        try:
            return datetime.strptime(s[:6] + "01", "%Y%m%d").date()
        except Exception:
            return None
    return None


def _days_to_expiry(cd: ContractDetails, now_utc: Optional[datetime] = None) -> Optional[int]:
    now = now_utc or datetime.now(timezone.utc)
    d = _parse_last_trade_date(getattr(cd.contract, "lastTradeDateOrContractMonth", None))
    if d is None:
        return None
    return int((d - now.date()).days)


def select_front_month(
    details: List[ContractDetails],
    now_utc: Optional[datetime] = None,
    *,
    roll_window_days: int = ROLL_WINDOW_DAYS_DEFAULT,
) -> Optional[ContractDetails]:
    """
    Tradeable front-month selection:
    - Prefer the nearest contract with days_to_expiry > roll_window_days
    - If none found, fall back to the nearest future contract (days_to_expiry >= 0)
    This prevents picking a contract already inside roll/expiry window.
    """
    if not details:
        return None

    now = now_utc or datetime.now(timezone.utc)

    scored_safe: List[Tuple[int, ContractDetails]] = []
    scored_future: List[Tuple[int, ContractDetails]] = []

    for cd in details:
        dte = _days_to_expiry(cd, now_utc=now)
        if dte is None:
            continue
        if dte < 0:
            continue
        scored_future.append((dte, cd))
        if dte > int(roll_window_days):
            scored_safe.append((dte, cd))

    if scored_safe:
        scored_safe.sort(key=lambda x: x[0])
        return scored_safe[0][1]

    if scored_future:
        scored_future.sort(key=lambda x: x[0])
        return scored_future[0][1]

    return None


def utc_now_str() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
