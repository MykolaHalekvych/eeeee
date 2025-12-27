from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"


# -----------------------------
# Control plane (file-based)
# -----------------------------
def load_control_state(path: Path) -> Dict[str, Any]:
    """
    control_state.json (minimal):
      {
        "armed": false
      }
    Safe defaults: armed=false.
    """
    if not path.exists():
        return {"armed": False, "source": "missing_defaults"}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return {"armed": False, "source": "invalid_defaults"}
        armed = obj.get("armed")
        return {"armed": bool(armed) if isinstance(armed, (bool, int)) else False, **obj, "source": "file"}
    except Exception:
        return {"armed": False, "source": "parse_error_defaults"}


def extract_run_mode(run_report: Dict[str, Any]) -> str:
    """
    Extract risk_envelope.mode from run_report.
    Fail-safe: NO_TRADE.
    """
    mode = None
    re = run_report.get("risk_envelope")
    if isinstance(re, dict):
        mode = re.get("mode")

    if mode is None:
        ma = run_report.get("ma_report")
        if isinstance(ma, dict):
            re2 = ma.get("risk_envelope")
            if isinstance(re2, dict):
                mode = re2.get("mode")

    m = str(mode or "").strip().upper().replace("-", "_")
    if m in {"ALLOW_NEW_ENTRIES", "ONLY_EXITS", "NO_TRADE"}:
        return m
    return "NO_TRADE"


def load_run_report(path: Path) -> Dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("run_report is not a JSON object")
    return obj


# -----------------------------
# JSONL utilities (strict-ish)
# -----------------------------
def iter_jsonl_strict(path: Path) -> Tuple[Iterable[Dict[str, Any]], int]:
    """
    Returns (iterable, parse_errors).
    Counts JSON parse errors (does not silently ignore).
    """
    parse_errors = 0

    def gen():
        nonlocal parse_errors
        if not path.exists():
            return
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        yield obj
                    else:
                        parse_errors += 1
                except Exception:
                    parse_errors += 1
                    continue

    return gen(), parse_errors


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        f.write("\n")


# -----------------------------
# Idempotency store
# -----------------------------
def _read_sent_keys(sent_path: Path) -> Set[str]:
    keys: Set[str] = set()
    if not sent_path.exists():
        return keys
    gen, _ = iter_jsonl_strict(sent_path)
    for r in gen:
        k = r.get("idempotency_key")
        if isinstance(k, str) and k.strip():
            keys.add(k.strip())
    return keys


# -----------------------------
# IBKR real sender (minimal wiring)
# -----------------------------
@dataclass(frozen=True)
class IbkrConn:
    host: str
    port: int
    client_id: int


def load_ibkr_connection(path: Path) -> IbkrConn:
    """
    Reads args/data/ibkr_connection_v0.json:
      {"host":"localhost","port":7497,"client_id":101}
    Safe defaults if missing.
    """
    if not path.exists():
        return IbkrConn(host="localhost", port=7497, client_id=101)
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        return IbkrConn(host="localhost", port=7497, client_id=101)
    host = str(obj.get("host") or "localhost")
    port = int(obj.get("port") or 7497)
    client_id = int(obj.get("client_id") or obj.get("clientId") or 101)
    return IbkrConn(host=host, port=port, client_id=client_id)


def _require(cond: bool, msg: str, errors: List[str]) -> None:
    if not cond:
        errors.append(msg)


def _validate_sendplan_record(rec: Dict[str, Any], errors: List[str]) -> None:
    kind = str(rec.get("kind") or "").strip().upper()
    _require(kind in {"SENDPLAN_ORDER", "SENDPLAN_CANCEL_ALL"}, f"unknown sendplan kind: {kind}", errors)

    run_id = str(rec.get("run_id") or "").strip()
    _require(bool(run_id), "missing run_id in sendplan record", errors)

    if kind == "SENDPLAN_ORDER":
        key = rec.get("idempotency_key")
        _require(isinstance(key, str) and key.strip(), "missing idempotency_key", errors)

        contract = rec.get("contract")
        _require(isinstance(contract, dict), "contract not dict", errors)

        order = rec.get("order")
        _require(isinstance(order, dict), "order not dict", errors)

        # strict: transmit must exist and be False at sendplan layer
        if isinstance(order, dict):
            _require("transmit" in order, "order.transmit missing (must exist; dryrun expects False)", errors)
            _require(order.get("transmit") is False, "order.transmit must be False in sendplan", errors)


def _build_ibkr_contract(contract_dict: Dict[str, Any]):
    """
    Convert contract dict -> ibapi Contract.
    """
    try:
        from ibapi.contract import Contract  # type: ignore
    except Exception as e:
        raise RuntimeError(f"ibapi not available: {e}")

    c = Contract()
    # Prefer conId if present
    con_id = contract_dict.get("conId")
    if con_id is not None:
        try:
            c.conId = int(con_id)
        except Exception:
            pass
    c.localSymbol = str(contract_dict.get("localSymbol") or "")
    c.symbol = str(contract_dict.get("symbol") or "")
    c.secType = str(contract_dict.get("secType") or "")
    c.exchange = str(contract_dict.get("exchange") or "")
    c.currency = str(contract_dict.get("currency") or "")
    # Optional
    ltd = contract_dict.get("lastTradeDateOrContractMonth")
    if ltd is not None:
        c.lastTradeDateOrContractMonth = str(ltd)
    tc = contract_dict.get("tradingClass")
    if tc is not None:
        c.tradingClass = str(tc)
    mult = contract_dict.get("multiplier")
    if mult is not None:
        c.multiplier = str(mult)
    return c


def _build_ibkr_order(order_dict: Dict[str, Any], *, transmit: bool):
    """
    Convert order dict -> ibapi Order.
    We force transmit value at runtime based on armed state.
    """
    try:
        from ibapi.order import Order  # type: ignore
    except Exception as e:
        raise RuntimeError(f"ibapi not available: {e}")

    o = Order()
    o.action = str(order_dict.get("action") or "")
    o.orderType = str(order_dict.get("orderType") or "")
    o.totalQuantity = float(order_dict.get("totalQuantity") or 0)
    o.tif = str(order_dict.get("tif") or "DAY")
    o.transmit = bool(transmit)

    if str(o.orderType).upper() == "LMT":
        if "lmtPrice" in order_dict:
            o.lmtPrice = float(order_dict.get("lmtPrice") or 0.0)

    return o


class _IbkrApp:
    """
    Minimal IBKR app wrapper for placeOrder + global cancel.
    """
    def __init__(self) -> None:
        try:
            from ibapi.client import EClient  # type: ignore
            from ibapi.wrapper import EWrapper  # type: ignore
        except Exception as e:
            raise RuntimeError(f"ibapi not available: {e}")

        class App(EWrapper, EClient):  # type: ignore
            def __init__(self, outer: "_IbkrApp") -> None:
                EClient.__init__(self, self)
                self._outer = outer

            def nextValidId(self, orderId: int) -> None:
                self._outer._next_id = int(orderId)
                self._outer._connected.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="") -> None:
                self._outer._errors.append(f"IBKR error reqId={reqId} code={errorCode} msg={errorString}")

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
            ) -> None:
                self._outer._order_status[int(orderId)] = str(status)

        self._connected = threading.Event()
        self._done = threading.Event()
        self._errors: List[str] = []
        self._order_status: Dict[int, str] = {}
        self._next_id: Optional[int] = None
        self._app = App(self)

    def connect_and_start(self, conn: IbkrConn, timeout_s: float = 8.0) -> None:
        self._app.connect(conn.host, conn.port, conn.client_id)
        t = threading.Thread(target=self._app.run, daemon=True)
        t.start()
        if not self._connected.wait(timeout=timeout_s):
            try:
                self._app.disconnect()
            except Exception:
                pass
            raise TimeoutError("Timeout waiting for IBKR nextValidId (not connected?)")

    def disconnect(self) -> None:
        try:
            self._app.disconnect()
        except Exception:
            pass

    def place_order(self, order_id: int, contract, order) -> None:
        self._app.placeOrder(order_id, contract, order)

    def req_global_cancel(self) -> None:
        self._app.reqGlobalCancel()

    def next_order_id(self) -> int:
        if self._next_id is None:
            raise RuntimeError("No nextValidId received")
        oid = self._next_id
        self._next_id += 1
        return oid

    @property
    def errors(self) -> List[str]:
        return list(self._errors)


# -----------------------------
# Real sender v1
# -----------------------------
def real_sender(
    *,
    sendplan_path: Path,
    run_report_path: Path,
    control_state_path: Path,
) -> Dict[str, Any]:
    """
    Level 5.0 wiring:
    - DISARMED (armed=false): no IBKR connect, write would_send_<run_id>.jsonl
    - ARMED (armed=true): connect IBKR Paper, execute CANCEL_ALL and ORDERs (if mode allows)
    Idempotency by idempotency_key in sent_orders_<run_id>.jsonl
    """
    report = load_run_report(run_report_path)
    run_id = str(report.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_report missing run_id")

    mode = extract_run_mode(report)

    control = load_control_state(control_state_path)
    armed = bool(control.get("armed") is True)

    sent_path = DATA_DIR / f"sent_orders_{run_id}.jsonl"
    would_path = DATA_DIR / f"would_send_{run_id}.jsonl"

    seen_sent = _read_sent_keys(sent_path)

    gen, parse_errors = iter_jsonl_strict(sendplan_path)

    total = 0
    would = 0
    sent = 0
    skipped = 0
    cancel_all = 0
    order_plans = 0
    errors: List[str] = []

    # First pass validation + classification
    plans: List[Dict[str, Any]] = []
    for rec in gen:
        total += 1
        _validate_sendplan_record(rec, errors)
        plans.append(rec)
        if len(errors) >= 50:
            break

    # If DISARMED: do not connect to IBKR, only write would-send records
    if not armed:
        if would_path.exists():
            would_path.unlink()
        for rec in plans:
            kind = str(rec.get("kind") or "").strip().upper()
            out = {
                "kind": "WOULD_SEND",
                "run_id": run_id,
                "mode": mode,
                "plan_kind": kind,
                "idempotency_key": rec.get("idempotency_key"),
                "reason": "DISARMED",
                "sendplan": rec,
            }
            append_jsonl(would_path, out)
            would += 1

        return {
            "armed": False,
            "run_id": run_id,
            "mode": mode,
            "sendplan_path": str(sendplan_path),
            "run_report_path": str(run_report_path),
            "would_send_out": str(would_path),
            "sent_out": str(sent_path),
            "total": total,
            "would": would,
            "sent": 0,
            "skipped": 0,
            "parse_errors": parse_errors,
            "errors_count": len(errors),
            "errors_head": errors[:8],
        }

    # ARMED: connect to IBKR and execute (still mode-restricted)
    conn = load_ibkr_connection(DATA_DIR / "ibkr_connection_v0.json")

    app = _IbkrApp()
    ibkr_errors: List[str] = []

    try:
        app.connect_and_start(conn, timeout_s=10.0)

        # Cancel-all allowed in any mode (especially HALT)
        for rec in plans:
            kind = str(rec.get("kind") or "").strip().upper()
            if kind == "SENDPLAN_CANCEL_ALL":
                cancel_all += 1
                app.req_global_cancel()
                append_jsonl(
                    sent_path,
                    {
                        "kind": "SENT_CANCEL_ALL",
                        "run_id": run_id,
                        "mode": mode,
                        "ts": rec.get("ts"),
                        "index": rec.get("index"),
                        "orderRef": rec.get("orderRef"),
                        "reason": rec.get("reason") or "CANCEL_ALL",
                    },
                )
                sent += 1

        # Orders are allowed only in ALLOW_NEW_ENTRIES for v1 wiring
        for rec in plans:
            kind = str(rec.get("kind") or "").strip().upper()
            if kind != "SENDPLAN_ORDER":
                continue

            order_plans += 1

            if mode != "ALLOW_NEW_ENTRIES":
                skipped += 1
                append_jsonl(
                    would_path,
                    {
                        "kind": "WOULD_SEND",
                        "run_id": run_id,
                        "mode": mode,
                        "plan_kind": kind,
                        "idempotency_key": rec.get("idempotency_key"),
                        "reason": f"MODE_BLOCKS_ORDERS:{mode}",
                        "sendplan": rec,
                    },
                )
                continue

            key = rec.get("idempotency_key")
            if isinstance(key, str) and key.strip() and key.strip() in seen_sent:
                skipped += 1
                continue

            contract_dict = rec.get("contract")
            order_dict = rec.get("order")
            if not isinstance(contract_dict, dict) or not isinstance(order_dict, dict):
                errors.append("SENDPLAN_ORDER missing contract/order dict")
                skipped += 1
                continue

            # Build IBKR objects; force transmit=True ONLY because ARMED
            contract = _build_ibkr_contract(contract_dict)
            order = _build_ibkr_order(order_dict, transmit=True)

            oid = app.next_order_id()
            app.place_order(oid, contract, order)

            append_jsonl(
                sent_path,
                {
                    "kind": "SENT_ORDER",
                    "run_id": run_id,
                    "mode": mode,
                    "idempotency_key": key,
                    "ibkr_order_id": oid,
                    "ts": rec.get("ts"),
                    "index": rec.get("index"),
                    "orderRef": rec.get("orderRef"),
                    "contract": contract_dict,
                    "order": {**order_dict, "transmit": True},
                    "reason": rec.get("reason") or "OK",
                },
            )
            seen_sent.add(str(key).strip())
            sent += 1

        ibkr_errors = app.errors

    finally:
        try:
            app.disconnect()
        except Exception:
            pass

    return {
        "armed": True,
        "run_id": run_id,
        "mode": mode,
        "sendplan_path": str(sendplan_path),
        "run_report_path": str(run_report_path),
        "would_send_out": str(would_path),
        "sent_out": str(sent_path),
        "total": total,
        "order_plans": order_plans,
        "cancel_all": cancel_all,
        "sent": sent,
        "skipped": skipped,
        "parse_errors": parse_errors,
        "errors_count": len(errors),
        "errors_head": errors[:8],
        "ibkr_errors_head": ibkr_errors[:8],
        "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
    }
