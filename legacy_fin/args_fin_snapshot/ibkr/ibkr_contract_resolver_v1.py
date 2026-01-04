# args/ibkr/ibkr_contract_resolver_v1.py
from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------
# UNATTENDED ELIGIBILITY RULES
# -----------------------------
AUTOMATION_DENY_SYMBOLS = {"HG"}          # physical delivery => NOT unattended
AUTOMATION_ALLOW_SYMBOLS = {"MHG", "QC"}  # intended for unattended

ROLL_WINDOW_TRADING_DAYS_DEFAULT = 7


# -----------------------------
# Helpers
# -----------------------------
def _sym_u(x: Any) -> str:
    return str(x or "").strip().upper()


def is_automation_denied(symbol: str) -> bool:
    return _sym_u(symbol) in AUTOMATION_DENY_SYMBOLS


def is_automation_allowed(symbol: str) -> bool:
    return _sym_u(symbol) in AUTOMATION_ALLOW_SYMBOLS


def utc_now_str() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _get_primary_exch(obj: Any) -> Optional[str]:
    v = getattr(obj, "primaryExchange", None)
    if v:
        return v
    return getattr(obj, "primaryExch", None)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_json_atomic(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _coalesce(*vals: Any) -> Any:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


# -----------------------------
# DATE / EXPIRY PARSING
# -----------------------------
def _parse_last_trade_date(s: Optional[str]) -> Optional[datetime.date]:
    """
    IBKR contract.lastTradeDateOrContractMonth can be:
      - YYYYMMDD
      - YYYYMM (fallback -> YYYYMM01)
    """
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None

    if len(s) >= 8 and s[:8].isdigit():
        try:
            return datetime.strptime(s[:8], "%Y%m%d").date()
        except Exception:
            return None

    if len(s) >= 6 and s[:6].isdigit():
        try:
            return datetime.strptime(s[:6] + "01", "%Y%m%d").date()
        except Exception:
            return None

    return None


def _trading_days_between(start_date: datetime.date, end_date: datetime.date) -> int:
    """Count Mon–Fri days from start_date (exclusive) to end_date (inclusive-ish)."""
    if end_date <= start_date:
        return 0
    d = start_date
    n = 0
    while d < end_date:
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _trading_days_to_expiry(cd: Any, now_utc: Optional[datetime] = None) -> Optional[int]:
    now = now_utc or datetime.now(timezone.utc)
    c = getattr(cd, "contract", None)
    d = _parse_last_trade_date(getattr(c, "lastTradeDateOrContractMonth", None))
    if d is None:
        return None
    return int(_trading_days_between(now.date(), d))


# -----------------------------
# FRONT-MONTH SELECTION
# -----------------------------
def select_front_month_reason(
    details: List[Any],
    now_utc: Optional[datetime] = None,
    *,
    roll_window_days: int = ROLL_WINDOW_TRADING_DAYS_DEFAULT,
    strict: bool = True,
) -> Tuple[Optional[Any], str]:
    if not details:
        return None, "NO_DETAILS"

    now = now_utc or datetime.now(timezone.utc)

    scored_safe: List[Tuple[int, Any]] = []
    scored_future: List[Tuple[int, Any]] = []
    seen_any_date = False

    for cd in details:
        tdte = _trading_days_to_expiry(cd, now_utc=now)
        if tdte is None:
            continue
        seen_any_date = True
        if tdte < 0:
            continue
        scored_future.append((tdte, cd))
        if tdte > int(roll_window_days):
            scored_safe.append((tdte, cd))

    if not seen_any_date:
        return None, "NO_LAST_TRADE_DATE"

    if scored_safe:
        scored_safe.sort(key=lambda x: x[0])
        return scored_safe[0][1], "OK_SAFE"

    if strict:
        if scored_future:
            return None, "IN_ROLL_WINDOW"
        return None, "ALL_EXPIRED"

    if scored_future:
        scored_future.sort(key=lambda x: x[0])
        return scored_future[0][1], "OK_FALLBACK_FUTURE"

    return None, "ALL_EXPIRED"


# COMPAT: legacy demos expect select_front_month()
def select_front_month(
    details: List[Any],
    now_utc: Optional[datetime] = None,
    *,
    roll_window_days: int = ROLL_WINDOW_TRADING_DAYS_DEFAULT,
    strict: bool = True,
) -> Optional[Any]:
    cd, _ = select_front_month_reason(
        details, now_utc=now_utc, roll_window_days=int(roll_window_days), strict=bool(strict)
    )
    return cd


# -----------------------------
# ELIGIBILITY RESULT
# -----------------------------
@dataclass(frozen=True)
class EligibilityResult:
    ok: bool
    reason: str
    symbol: str
    roll_window_trading_days: int = ROLL_WINDOW_TRADING_DAYS_DEFAULT
    selected_cd: Optional[Any] = None

    def to_meta(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ok": bool(self.ok),
            "reason": str(self.reason),
            "symbol": str(self.symbol),
            "roll_window_trading_days": int(self.roll_window_trading_days),
            "ts_utc": utc_now_str(),
        }
        if self.selected_cd is not None:
            c = getattr(self.selected_cd, "contract", None)
            out["selected"] = contract_to_dict(c)
        return out


def evaluate_eligibility_and_select(
    *,
    symbol: str,
    details: List[Any],
    roll_window_trading_days: int = ROLL_WINDOW_TRADING_DAYS_DEFAULT,
    now_utc: Optional[datetime] = None,
) -> EligibilityResult:
    sym = _sym_u(symbol)

    if is_automation_denied(sym):
        return EligibilityResult(False, "BROKER_POLICY_PHYSICAL_DELIVERY", sym, int(roll_window_trading_days), None)

    if not is_automation_allowed(sym):
        return EligibilityResult(False, "SYMBOL_NOT_ALLOWLISTED", sym, int(roll_window_trading_days), None)

    cd, reason = select_front_month_reason(
        details, now_utc=now_utc, roll_window_days=int(roll_window_trading_days), strict=True
    )
    if cd is None:
        return EligibilityResult(False, reason, sym, int(roll_window_trading_days), None)

    return EligibilityResult(True, reason, sym, int(roll_window_trading_days), cd)


# -------------------------------------------------------------------
# COMPAT: contract factories expected by legacy demos
# -------------------------------------------------------------------
def make_fut_contract(
    symbol: str,
    *,
    exchange: str = "COMEX",
    currency: str = "USD",
    last_trade_date_or_contract_month: Optional[str] = None,
    local_symbol: Optional[str] = None,
    con_id: Optional[int] = None,
) -> Any:
    from ibapi.contract import Contract  # type: ignore

    c = Contract()
    c.secType = "FUT"
    c.symbol = _sym_u(symbol)
    c.exchange = str(exchange)
    c.currency = str(currency)

    if last_trade_date_or_contract_month:
        c.lastTradeDateOrContractMonth = str(last_trade_date_or_contract_month)
    if local_symbol:
        c.localSymbol = str(local_symbol)
    if con_id is not None:
        try:
            c.conId = int(con_id)
        except Exception:
            pass

    return c


def make_hg_fut_contract(
    *,
    exchange: str = "COMEX",
    currency: str = "USD",
    last_trade_date_or_contract_month: Optional[str] = None,
    local_symbol: Optional[str] = None,
    con_id: Optional[int] = None,
) -> Any:
    return make_fut_contract(
        "HG",
        exchange=exchange,
        currency=currency,
        last_trade_date_or_contract_month=last_trade_date_or_contract_month,
        local_symbol=local_symbol,
        con_id=con_id,
    )


# -------------------------------------------------------------------
# COMPAT API: dict converters expected by legacy demos
# -------------------------------------------------------------------
def contract_to_dict(c: Any) -> Dict[str, Any]:
    if c is None:
        return {}
    return {
        "conId": getattr(c, "conId", None),
        "symbol": getattr(c, "symbol", None),
        "localSymbol": getattr(c, "localSymbol", None),
        "secType": getattr(c, "secType", None),
        "exchange": getattr(c, "exchange", None),
        "primaryExch": _get_primary_exch(c),
        "primaryExchange": _get_primary_exch(c),
        "currency": getattr(c, "currency", None),
        "multiplier": getattr(c, "multiplier", None),
        "lastTradeDateOrContractMonth": getattr(c, "lastTradeDateOrContractMonth", None),
        "tradingClass": getattr(c, "tradingClass", None),
    }


def contract_details_to_dict(cd: Any) -> Dict[str, Any]:
    if cd is None:
        return {}
    c = getattr(cd, "contract", None)
    return {
        "contract": contract_to_dict(c),
        "marketName": getattr(cd, "marketName", None),
        "longName": getattr(cd, "longName", None),
        "minTick": getattr(cd, "minTick", None),
        "timeZoneId": getattr(cd, "timeZoneId", None),
        "validExchanges": getattr(cd, "validExchanges", None),
        "tradingHours": getattr(cd, "tradingHours", None),
        "liquidHours": getattr(cd, "liquidHours", None),
    }


# -----------------------------
# IBKR CONNECTION LOADER (legacy expects attribute access)
# -----------------------------
@dataclass(frozen=True)
class IbkrConn:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11
    timeout_s: float = 20.0
    wait_s: float = 8.0


def _candidate_conn_paths() -> List[Path]:
    r = _repo_root()
    return [
        r / "args" / "data" / "ibkr_connection_v0.json",
        r / "args" / "data" / "ibkr_connection_v1.json",
        r / "args" / "data" / "ibkr_connection.json",
        r / "args" / "ibkr" / "ibkr_connection_v0.json",
        r / "args" / "ibkr" / "ibkr_connection_v1.json",
        r / "args" / "ibkr" / "ibkr_connection.json",
    ]


def load_ibkr_connection(path: Optional[str] = None) -> IbkrConn:
    """
    Legacy demos expect an object with .host/.port/.client_id.
    """
    p: Optional[Path] = Path(path) if path else None
    if p is None:
        for cand in _candidate_conn_paths():
            if cand.exists():
                p = cand
                break

    if p is None or not p.exists():
        return IbkrConn()

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return IbkrConn()

    host = _coalesce(raw.get("host"), raw.get("Host"), raw.get("hostname"), raw.get("ip"), "127.0.0.1")
    port = _coalesce(raw.get("port"), raw.get("Port"), 7497)
    client_id = _coalesce(raw.get("client_id"), raw.get("clientId"), raw.get("clientID"), raw.get("ClientId"), 11)
    timeout_s = _coalesce(raw.get("timeout_s"), raw.get("timeoutS"), raw.get("timeout"), 20.0)
    wait_s = _coalesce(raw.get("wait_s"), raw.get("waitS"), raw.get("wait"), 8.0)

    return IbkrConn(
        host=str(host),
        port=_as_int(port, 7497),
        client_id=_as_int(client_id, 11),
        timeout_s=_as_float(timeout_s, 20.0),
        wait_s=_as_float(wait_s, 8.0),
    )


def load_ibkr_connection_dict(path: Optional[str] = None) -> Dict[str, Any]:
    c = load_ibkr_connection(path)
    return {"host": c.host, "port": c.port, "client_id": c.client_id, "timeout_s": c.timeout_s, "wait_s": c.wait_s}


# -----------------------------
# IBKR CONTRACT DETAILS FETCH (single canonical _CDApp)
# -----------------------------
class _CDApp:
    def __init__(self, conn: IbkrConn):
        from ibapi.client import EClient  # type: ignore
        from ibapi.wrapper import EWrapper  # type: ignore

        class App(EWrapper, EClient):  # type: ignore
            def __init__(self, outer: "_CDApp"):
                EClient.__init__(self, self)
                self._outer = outer

            def nextValidId(self, orderId: int) -> None:  # noqa: N802
                self._outer._on_ready()

            def contractDetails(self, reqId, contractDetails):  # noqa: N802
                self._outer._on_contract_details(contractDetails)

            def contractDetailsEnd(self, reqId):  # noqa: N802
                self._outer._on_contract_details_end()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802
                self._outer._on_error(reqId, errorCode, errorString, advancedOrderRejectJson)

        self._conn = conn
        self._app = App(self)
        self._thread: Optional[threading.Thread] = None

        self._ready_ev = threading.Event()
        self._end_ev = threading.Event()

        self.details: List[Any] = []
        self.errors: List[Dict[str, Any]] = []

    def _on_ready(self) -> None:
        self._ready_ev.set()

    def _on_contract_details(self, cd: Any) -> None:
        self.details.append(cd)

    def _on_contract_details_end(self) -> None:
        self._end_ev.set()

    def _on_error(self, reqId: Any, errorCode: Any, errorString: Any, advanced: Any) -> None:
        self.errors.append(
            {
                "req_id": reqId,
                "error_code": errorCode,
                "error_string": str(errorString),
                "advanced": str(advanced) if isinstance(advanced, str) else "",
            }
        )

    def connect(self) -> None:
        self._app.connect(self._conn.host, int(self._conn.port), int(self._conn.client_id))
        self._thread = threading.Thread(target=self._app.run, daemon=True)
        self._thread.start()
        if not self._ready_ev.wait(timeout=float(self._conn.timeout_s)):
            raise RuntimeError("IBKR: timeout waiting for nextValidId")

    def disconnect(self) -> None:
        try:
            self._app.disconnect()
        except Exception:
            pass

    def fetch_contract_details(self, contract_obj: Any) -> List[Any]:
        self.details.clear()
        self.errors.clear()
        self._end_ev.clear()

        self._app.reqContractDetails(1, contract_obj)
        self._end_ev.wait(timeout=float(self._conn.wait_s))
        return list(self.details)


# COMPAT: legacy demos import fetch_contract_details from this module
def fetch_contract_details(
    contract: Any,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 11,
    timeout_s: float = 20.0,
    *,
    wait_s: Optional[float] = None,
    **_ignored: Any,
) -> List[Any]:
    conn = IbkrConn(
        host=str(host),
        port=int(port),
        client_id=int(client_id),
        timeout_s=float(timeout_s),
        wait_s=float(wait_s if wait_s is not None else max(5.0, min(25.0, timeout_s))),
    )
    app = _CDApp(conn)
    try:
        app.connect()
        return app.fetch_contract_details(contract)
    finally:
        app.disconnect()


# -----------------------------
# MAIN RESOLVER
# -----------------------------
def resolve_copper_contract_unattended(
    *,
    symbol: str,
    conn: IbkrConn,
    sec_type: str = "FUT",
    exchange: str = "COMEX",
    currency: str = "USD",
    roll_window_trading_days: int = ROLL_WINDOW_TRADING_DAYS_DEFAULT,
    out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    out_dir = out_dir or (_repo_root() / "args" / "data")
    sym = _sym_u(symbol)

    out_contract = out_dir / f"ibkr_{sym.lower()}_contract_v1.json"
    out_meta = out_dir / f"ibkr_{sym.lower()}_contract_v1.meta.json"

    if is_automation_denied(sym):
        meta = EligibilityResult(False, "BROKER_POLICY_PHYSICAL_DELIVERY", sym, int(roll_window_trading_days), None).to_meta()
        _write_json_atomic(out_meta, meta)
        _write_json_atomic(out_contract, {})
        return {"ok": False, "meta_path": str(out_meta), "contract_path": str(out_contract), "meta": meta}

    if not is_automation_allowed(sym):
        meta = EligibilityResult(False, "SYMBOL_NOT_ALLOWLISTED", sym, int(roll_window_trading_days), None).to_meta()
        _write_json_atomic(out_meta, meta)
        _write_json_atomic(out_contract, {})
        return {"ok": False, "meta_path": str(out_meta), "contract_path": str(out_contract), "meta": meta}

    from ibapi.contract import Contract  # type: ignore

    c = Contract()
    c.symbol = sym
    c.secType = str(sec_type)
    c.exchange = str(exchange)
    c.currency = str(currency)

    app = _CDApp(conn)
    try:
        app.connect()
        details = app.fetch_contract_details(c)
        elig = evaluate_eligibility_and_select(
            symbol=sym,
            details=details,
            roll_window_trading_days=int(roll_window_trading_days),
        )
        meta = elig.to_meta()
        meta["details_count"] = int(len(details))
        meta["errors"] = list(app.errors)
        _write_json_atomic(out_meta, meta)

        if not elig.ok or elig.selected_cd is None:
            _write_json_atomic(out_contract, {})
            return {"ok": False, "meta_path": str(out_meta), "contract_path": str(out_contract), "meta": meta}

        contract_dict = contract_to_dict(getattr(elig.selected_cd, "contract", None))
        _write_json_atomic(out_contract, contract_dict)
        return {"ok": True, "meta_path": str(out_meta), "contract_path": str(out_contract), "meta": meta, "contract": contract_dict}
    finally:
        app.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(prog="ibkr_contract_resolver_v1", description="Resolve unattended-safe copper futures contract (MHG/QC). HG is denied.")
    ap.add_argument("--symbol", required=True, help="MHG or QC (HG will be denied)")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--client-id", type=int, default=None)
    ap.add_argument("--timeout-s", type=float, default=None)
    ap.add_argument("--wait-s", type=float, default=None)

    ap.add_argument("--sec-type", default="FUT")
    ap.add_argument("--exchange", default="COMEX")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--roll-window-trading-days", type=int, default=ROLL_WINDOW_TRADING_DAYS_DEFAULT)
    ap.add_argument("--conn-json", default="", help="Optional path to ibkr_connection_*.json")

    a = ap.parse_args()

    base = load_ibkr_connection(a.conn_json or None)

    conn = IbkrConn(
        host=str(a.host if a.host is not None else base.host),
        port=int(a.port if a.port is not None else base.port),
        client_id=int(a.client_id if a.client_id is not None else base.client_id),
        timeout_s=float(a.timeout_s if a.timeout_s is not None else base.timeout_s),
        wait_s=float(a.wait_s if a.wait_s is not None else base.wait_s),
    )

    res = resolve_copper_contract_unattended(
        symbol=str(a.symbol),
        conn=conn,
        sec_type=str(a.sec_type),
        exchange=str(a.exchange),
        currency=str(a.currency),
        roll_window_trading_days=int(a.roll_window_trading_days),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
