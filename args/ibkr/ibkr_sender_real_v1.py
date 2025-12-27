# args/ibkr/ibkr_sender_real_v1.py
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"

# Guardrails
K_LIMIT_ORDERS_DEFAULT = 1           # per invocation
RUN_LIMIT_ORDERS_DEFAULT = 1         # per run_id across repeated ARMED runs (safety!)
CURSOR_PATH = DATA_DIR / "ibkr_order_id_cursor_v1.json"

# Safety: never start order ids from tiny numbers
ORDER_ID_FLOOR = 1000

# Stage 5.5: execution lifecycle logs + idempotency ledger
EXEC_LEDGER_PATH_DEFAULT = DATA_DIR / "ibkr_exec_ledger_v1.json"


# -----------------------------
# Control plane
# -----------------------------
def load_control_state(path: Path) -> Dict[str, Any]:
    """
    control_state.json (recommended shape):
      {
        "armed": false,
        "simulate": false,
        "k_limit_orders": 1,
        "run_limit_orders": 1,
        "ledger_path": "ibkr_exec_ledger_v1.json"   # optional (relative to args/data)
      }
    """
    if not path.exists():
        return {"armed": False, "simulate": False, "source": "missing_defaults"}

    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return {"armed": False, "simulate": False, "source": "invalid_defaults"}

        armed = obj.get("armed")
        simulate = obj.get("simulate")

        out = {
            "armed": bool(armed) if isinstance(armed, (bool, int)) else False,
            "simulate": bool(simulate) if isinstance(simulate, (bool, int)) else False,
            **obj,
            "source": "file",
        }
        return out
    except Exception:
        return {"armed": False, "simulate": False, "source": "parse_error_defaults"}


def extract_run_mode(run_report: Dict[str, Any]) -> str:
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
# JSONL utilities (strict, with correct parse_errors)
# -----------------------------
def iter_jsonl_strict(path: Path) -> Tuple[Iterable[Dict[str, Any]], Dict[str, int]]:
    stats = {"parse_errors": 0}

    def gen():
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
                        stats["parse_errors"] += 1
                except Exception:
                    stats["parse_errors"] += 1
                    continue

    return gen(), stats


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        f.write("\n")


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# -----------------------------
# Exec lifecycle log (Stage 5.5)
# -----------------------------
def _append_exec(exec_path: Path, obj: Dict[str, Any]) -> None:
    append_jsonl(exec_path, obj)


# -----------------------------
# Execution ledger (Stage 5.5) - strong idempotency/dedupe across runs
# -----------------------------
_VOLATILE_KEYS = {
    "ts",
    "timestamp",
    "created_at",
    "updated_at",
    "ibkr_order_id",
    "orderId",
    "permId",
    "index",
}


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _strip_volatile(x: Any) -> Any:
    if isinstance(x, dict):
        out: Dict[str, Any] = {}
        for k, v in x.items():
            if str(k) in _VOLATILE_KEYS:
                continue
            out[str(k)] = _strip_volatile(v)
        return out
    if isinstance(x, list):
        return [_strip_volatile(v) for v in x]
    return x


def compute_send_key(run_id: str, sendplan_item: Dict[str, Any]) -> str:
    """
    Primary key: run_id + idempotency_key (if present).
    Fallback: stable hash of record with volatile keys stripped.
    """
    ik = sendplan_item.get("idempotency_key")
    if isinstance(ik, str) and ik.strip():
        return f"{run_id}:{ik.strip()}"

    clean = _strip_volatile(sendplan_item)
    raw = _stable_json(clean).encode("utf-8")
    fp = hashlib.sha256(raw).hexdigest()[:16]
    return f"{run_id}:{fp}"


def _load_ledger(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return obj
        except Exception:
            # fail-safe: treat as empty ledger; caller still safe due to DISARM unless armed
            return {"schema_version": "ibkr_exec_ledger_v1", "items": {}}
    return {"schema_version": "ibkr_exec_ledger_v1", "items": {}}


def _save_ledger_atomic(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    obj.setdefault("schema_version", "ibkr_exec_ledger_v1")
    obj.setdefault("created_at", _utc_now_z())
    obj["updated_at"] = _utc_now_z()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_stable_json(obj) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ledger_has(ledger: Dict[str, Any], send_key: str) -> bool:
    items = ledger.get("items")
    return isinstance(items, dict) and send_key in items


def _ledger_mark(ledger: Dict[str, Any], send_key: str, *, state: str, order_id: Optional[int], simulate: bool) -> None:
    items = ledger.get("items")
    if not isinstance(items, dict):
        items = {}
        ledger["items"] = items
    items[send_key] = {
        "ts": _utc_now_z(),
        "state": str(state),
        "order_id": int(order_id) if order_id is not None else None,
        "simulate": bool(simulate),
    }


# -----------------------------
# Sent log scanning (idempotency + run-level limit)
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


def _count_sent_orders_for_run(sent_path: Path, run_id: str) -> int:
    if not sent_path.exists():
        return 0
    n = 0
    gen, _ = iter_jsonl_strict(sent_path)
    for r in gen:
        if str(r.get("run_id") or "") != run_id:
            continue
        kind = str(r.get("kind") or "")
        if kind in {"SENT_ORDER", "SENT_ORDER_SIM"}:
            n += 1
    return n


# -----------------------------
# IBKR connection config
# -----------------------------
@dataclass(frozen=True)
class IbkrConn:
    host: str
    port: int
    client_id: int


def load_ibkr_connection(path: Path) -> IbkrConn:
    if not path.exists():
        return IbkrConn(host="localhost", port=7497, client_id=101)
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        return IbkrConn(host="localhost", port=7497, client_id=101)
    host = str(obj.get("host") or "localhost")
    port = int(obj.get("port") or 7497)
    client_id = int(obj.get("client_id") or obj.get("clientId") or 101)
    return IbkrConn(host=host, port=port, client_id=client_id)


# -----------------------------
# Cursor store (global monotonic orderId)
# -----------------------------
def _cursor_key(conn: IbkrConn) -> str:
    return f"{conn.host}:{conn.port}:{conn.client_id}"


def _load_cursor() -> Dict[str, Any]:
    if not CURSOR_PATH.exists():
        return {}
    try:
        obj = json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _save_cursor(obj: Dict[str, Any]) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_cursor_last(conn: IbkrConn) -> int:
    cur = _load_cursor()
    k = _cursor_key(conn)
    v = cur.get(k, {})
    if isinstance(v, dict):
        try:
            return int(v.get("last_used_order_id") or 0)
        except Exception:
            return 0
    return 0


def _set_cursor_last(conn: IbkrConn, last_used: int) -> None:
    cur = _load_cursor()
    k = _cursor_key(conn)
    cur[k] = {
        "last_used_order_id": int(last_used),
        "updated_at_utc": _utc_now_z(),
    }
    _save_cursor(cur)


# -----------------------------
# Validation helpers
# -----------------------------
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

        if isinstance(order, dict):
            _require("transmit" in order, "order.transmit missing (sendplan expects False)", errors)
            _require(order.get("transmit") is False, "order.transmit must be False in sendplan", errors)


# -----------------------------
# IBKR object builders
# -----------------------------
def _build_ibkr_contract(contract_dict: Dict[str, Any]):
    from ibapi.contract import Contract  # type: ignore

    c = Contract()
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
    from ibapi.order import Order  # type: ignore

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


# -----------------------------
# IBKR app wrapper (nextValidId + orderStatus visibility)
# -----------------------------
class _IbkrApp:
    def __init__(self) -> None:
        from ibapi.client import EClient  # type: ignore
        from ibapi.wrapper import EWrapper  # type: ignore

        class App(EWrapper, EClient):  # type: ignore
            def __init__(self, outer: "_IbkrApp") -> None:
                EClient.__init__(self, self)
                self._outer = outer

            def nextValidId(self, orderId: int) -> None:
                self._outer._next_id = int(orderId)
                self._outer._connected.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="") -> None:
                self._outer._errors.append(
                    {
                        "reqId": int(reqId) if str(reqId).lstrip("-").isdigit() else reqId,
                        "code": int(errorCode),
                        "msg": str(errorString),
                    }
                )

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
                try:
                    oid = int(orderId)
                except Exception:
                    return
                self._outer._order_status[oid] = str(status)

        self._connected = threading.Event()
        self._errors: List[Dict[str, Any]] = []
        self._order_status: Dict[int, str] = {}
        self._next_id: Optional[int] = None
        self._app = App(self)

    def connect_and_start(self, conn: IbkrConn, timeout_s: float = 8.0) -> int:
        self._app.connect(conn.host, conn.port, conn.client_id)
        t = threading.Thread(target=self._app.run, daemon=True)
        t.start()
        if not self._connected.wait(timeout=timeout_s):
            try:
                self._app.disconnect()
            except Exception:
                pass
            raise TimeoutError("Timeout waiting for IBKR nextValidId")
        assert self._next_id is not None
        return int(self._next_id)

    def set_next_id(self, v: int) -> None:
        self._next_id = int(v)

    def disconnect(self) -> None:
        try:
            self._app.disconnect()
        except Exception:
            pass

    def place_order(self, order_id: int, contract, order) -> None:
        if self._next_id is None:
            raise RuntimeError("No nextValidId received")
        self._app.placeOrder(order_id, contract, order)

    def req_global_cancel(self) -> None:
        self._app.reqGlobalCancel()

    def next_order_id(self) -> int:
        if self._next_id is None:
            raise RuntimeError("No nextValidId received")
        oid = self._next_id
        self._next_id += 1
        return oid

    def status_for(self, order_id: int) -> Optional[str]:
        return self._order_status.get(int(order_id))

    @property
    def errors(self) -> List[Dict[str, Any]]:
        return list(self._errors)


# -----------------------------
# Real sender
# -----------------------------
def real_sender(
    *,
    sendplan_path: Path,
    run_report_path: Path,
    control_state_path: Path,
) -> Dict[str, Any]:
    report = load_run_report(run_report_path)
    run_id = str(report.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_report missing run_id")

    mode = extract_run_mode(report)

    control = load_control_state(control_state_path)
    armed = bool(control.get("armed") is True)
    simulate = bool(control.get("simulate") is True)

    # per invocation limit
    try:
        k_limit_i = int(control.get("k_limit_orders")) if control.get("k_limit_orders") is not None else K_LIMIT_ORDERS_DEFAULT
    except Exception:
        k_limit_i = K_LIMIT_ORDERS_DEFAULT
    if k_limit_i < 1:
        k_limit_i = 1

    # per run_id limit across repeated ARMED runs
    try:
        run_limit_i = int(control.get("run_limit_orders")) if control.get("run_limit_orders") is not None else RUN_LIMIT_ORDERS_DEFAULT
    except Exception:
        run_limit_i = RUN_LIMIT_ORDERS_DEFAULT
    if run_limit_i < 1:
        run_limit_i = 1

    sent_path = DATA_DIR / f"sent_orders_{run_id}.jsonl"
    would_path = DATA_DIR / f"would_send_{run_id}.jsonl"
    exec_path = DATA_DIR / f"orders_exec_{run_id}.jsonl"

    # Ledger path (optional override)
    lp = control.get("ledger_path")
    if isinstance(lp, str) and lp.strip():
        p = Path(lp.strip())
        ledger_path = (DATA_DIR / p) if not p.is_absolute() else p
    else:
        ledger_path = EXEC_LEDGER_PATH_DEFAULT

    ledger = _load_ledger(ledger_path)

    seen_sent_keys = _read_sent_keys(sent_path)
    already_sent_count = _count_sent_orders_for_run(sent_path, run_id)

    # For clean audits, truncate would_path each invocation
    if would_path.exists():
        try:
            would_path.unlink()
        except Exception:
            pass

    gen, stats = iter_jsonl_strict(sendplan_path)

    total = 0
    would = 0
    sent = 0
    skipped = 0
    cancel_all = 0
    order_plans = 0
    executed_orders = 0
    errors: List[str] = []
    plans: List[Dict[str, Any]] = []

    for rec in gen:
        total += 1
        _validate_sendplan_record(rec, errors)
        plans.append(rec)
        if len(errors) >= 50:
            break

    parse_errors = int(stats.get("parse_errors") or 0)

    # DISARMED
    if not armed:
        for rec in plans:
            kind = str(rec.get("kind") or "").strip().upper()
            send_key = compute_send_key(run_id, rec)
            _append_exec(exec_path, {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": send_key,
                "status": "WOULD_SEND",
                "armed": False,
                "simulate": False,
                "reason": "DISARMED",
                "plan_kind": kind,
            })
            append_jsonl(
                would_path,
                {
                    "kind": "WOULD_SEND",
                    "run_id": run_id,
                    "mode": mode,
                    "plan_kind": kind,
                    "idempotency_key": rec.get("idempotency_key"),
                    "reason": "DISARMED",
                    "send_key": send_key,
                    "sendplan": rec,
                },
            )
            would += 1

        return {
            "armed": False,
            "simulate": False,
            "run_id": run_id,
            "mode": mode,
            "k_limit_orders": k_limit_i,
            "run_limit_orders": run_limit_i,
            "sendplan_path": str(sendplan_path),
            "run_report_path": str(run_report_path),
            "would_send_out": str(would_path),
            "sent_out": str(sent_path),
            "exec_out": str(exec_path),
            "ledger_path": str(ledger_path),
            "total": total,
            "would": would,
            "sent": 0,
            "skipped": 0,
            "parse_errors": parse_errors,
            "errors_count": len(errors),
            "errors_head": errors[:8],
        }

    # ARMED + SIMULATE (Stage 5.5 safe tests, no IBKR calls)
    if simulate:
        conn_sim = load_ibkr_connection(DATA_DIR / "ibkr_connection_v0.json")
        cursor_last_before = _get_cursor_last(conn_sim)
        next_oid = max(int(cursor_last_before) + 1, int(ORDER_ID_FLOOR))
        last_oid_used: Optional[int] = None

        # Cancel-all always allowed (simulated)
        for rec in plans:
            kind = str(rec.get("kind") or "").strip().upper()
            if kind == "SENDPLAN_CANCEL_ALL":
                cancel_all += 1
                send_key = compute_send_key(run_id, rec)
                _append_exec(exec_path, {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "CANCEL_ALL_SIM",
                    "simulate": True,
                })
                append_jsonl(
                    sent_path,
                    {
                        "kind": "SENT_CANCEL_ALL_SIM",
                        "run_id": run_id,
                        "mode": mode,
                        "send_key": send_key,
                        "simulate": True,
                        "reason": rec.get("reason") or "CANCEL_ALL",
                    },
                )
                sent += 1

        # run-level limit reached -> skip all orders
        if already_sent_count >= run_limit_i:
            for rec in plans:
                if str(rec.get("kind") or "").strip().upper() == "SENDPLAN_ORDER":
                    send_key = compute_send_key(run_id, rec)
                    _append_exec(exec_path, {
                        "kind": "EXEC_EVENT",
                        "ts": _utc_now_z(),
                        "run_id": run_id,
                        "send_key": send_key,
                        "status": "BLOCKED_RUN_LIMIT",
                        "simulate": True,
                        "reason": f"RUN_LIMIT_REACHED={run_limit_i}",
                    })
                    append_jsonl(
                        would_path,
                        {
                            "kind": "WOULD_SEND",
                            "run_id": run_id,
                            "mode": mode,
                            "plan_kind": "SENDPLAN_ORDER",
                            "idempotency_key": rec.get("idempotency_key"),
                            "reason": f"RUN_LIMIT_REACHED={run_limit_i}",
                            "send_key": send_key,
                            "sendplan": rec,
                        },
                    )
                    skipped += 1

            return {
                "armed": True,
                "simulate": True,
                "run_id": run_id,
                "mode": mode,
                "k_limit_orders": k_limit_i,
                "run_limit_orders": run_limit_i,
                "sendplan_path": str(sendplan_path),
                "run_report_path": str(run_report_path),
                "would_send_out": str(would_path),
                "sent_out": str(sent_path),
                "exec_out": str(exec_path),
                "ledger_path": str(ledger_path),
                "total": total,
                "order_plans": 0,
                "cancel_all": cancel_all,
                "sent": sent,
                "executed_orders": 0,
                "skipped": skipped,
                "parse_errors": parse_errors,
                "errors_count": len(errors),
                "errors_head": errors[:8],
            }

        # Execute orders (simulated)
        for rec in plans:
            kind = str(rec.get("kind") or "").strip().upper()
            if kind != "SENDPLAN_ORDER":
                continue
            order_plans += 1
            send_key = compute_send_key(run_id, rec)

            if mode != "ALLOW_NEW_ENTRIES":
                _append_exec(exec_path, {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "BLOCKED_MODE",
                    "simulate": True,
                    "reason": mode,
                })
                append_jsonl(
                    would_path,
                    {
                        "kind": "WOULD_SEND",
                        "run_id": run_id,
                        "mode": mode,
                        "plan_kind": kind,
                        "idempotency_key": rec.get("idempotency_key"),
                        "reason": f"MODE_BLOCKS_ORDERS:{mode}",
                        "send_key": send_key,
                        "sendplan": rec,
                    },
                )
                skipped += 1
                continue

            if executed_orders >= k_limit_i:
                _append_exec(exec_path, {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "BLOCKED_K_LIMIT",
                    "simulate": True,
                    "reason": f"K_LIMIT={k_limit_i}",
                })
                append_jsonl(
                    would_path,
                    {
                        "kind": "WOULD_SEND",
                        "run_id": run_id,
                        "mode": mode,
                        "plan_kind": kind,
                        "idempotency_key": rec.get("idempotency_key"),
                        "reason": f"K_LIMIT={k_limit_i}",
                        "send_key": send_key,
                        "sendplan": rec,
                    },
                )
                skipped += 1
                continue

            # Dedupe (ledger first, then sent log)
            ik = rec.get("idempotency_key")
            if _ledger_has(ledger, send_key) or (isinstance(ik, str) and ik.strip() in seen_sent_keys):
                _append_exec(exec_path, {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "DEDUPED",
                    "simulate": True,
                })
                skipped += 1
                continue

            oid = next_oid
            next_oid += 1
            last_oid_used = oid

            _ledger_mark(ledger, send_key, state="RESERVED", order_id=oid, simulate=True)
            _save_ledger_atomic(ledger_path, ledger)

            _append_exec(exec_path, {"kind": "EXEC_EVENT", "ts": _utc_now_z(), "run_id": run_id, "send_key": send_key, "order_id": oid, "status": "SUBMITTED", "simulate": True})
            _append_exec(exec_path, {"kind": "EXEC_EVENT", "ts": _utc_now_z(), "run_id": run_id, "send_key": send_key, "order_id": oid, "status": "ACK", "simulate": True})

            _ledger_mark(ledger, send_key, state="SENT_SIM", order_id=oid, simulate=True)
            _save_ledger_atomic(ledger_path, ledger)

            append_jsonl(
                sent_path,
                {
                    "kind": "SENT_ORDER_SIM",
                    "run_id": run_id,
                    "mode": mode,
                    "idempotency_key": rec.get("idempotency_key"),
                    "send_key": send_key,
                    "ibkr_order_id": oid,
                    "ibkr_order_status": "ACK",
                    "simulate": True,
                    "contract": rec.get("contract"),
                    "order": rec.get("order"),
                    "reason": rec.get("reason") or "OK",
                },
            )

            if isinstance(ik, str) and ik.strip():
                seen_sent_keys.add(ik.strip())

            executed_orders += 1
            sent += 1

        if last_oid_used is not None:
            _set_cursor_last(conn_sim, int(last_oid_used))

        return {
            "armed": True,
            "simulate": True,
            "run_id": run_id,
            "mode": mode,
            "k_limit_orders": k_limit_i,
            "run_limit_orders": run_limit_i,
            "sendplan_path": str(sendplan_path),
            "run_report_path": str(run_report_path),
            "would_send_out": str(would_path),
            "sent_out": str(sent_path),
            "exec_out": str(exec_path),
            "ledger_path": str(ledger_path),
            "total": total,
            "order_plans": order_plans,
            "cancel_all": cancel_all,
            "sent": sent,
            "executed_orders": executed_orders,
            "skipped": skipped,
            "parse_errors": parse_errors,
            "errors_count": len(errors),
            "errors_head": errors[:8],
        }

    # ARMED (real IBKR)
    conn = load_ibkr_connection(DATA_DIR / "ibkr_connection_v0.json")
    app = _IbkrApp()

    server_next_valid_id: Optional[int] = None
    cursor_last_before = _get_cursor_last(conn)
    start_order_id: Optional[int] = None
    last_oid_used: Optional[int] = None
    ibkr_errors_head: List[Dict[str, Any]] = []

    try:
        server_next_valid_id = app.connect_and_start(conn, timeout_s=10.0)

        # start order id = max(server next, cursor_last+1, ORDER_ID_FLOOR)
        start_order_id = max(int(server_next_valid_id), int(cursor_last_before) + 1, int(ORDER_ID_FLOOR))
        app.set_next_id(start_order_id)

        _append_exec(exec_path, {
            "kind": "EXEC_EVENT",
            "ts": _utc_now_z(),
            "run_id": run_id,
            "send_key": f"{run_id}:CONNECT",
            "status": "CONNECTED",
            "simulate": False,
            "server_next_valid_id": server_next_valid_id,
            "cursor_last_before": cursor_last_before,
            "start_order_id": start_order_id,
            "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
        })

        # Cancel-all always allowed
        for rec in plans:
            kind = str(rec.get("kind") or "").strip().upper()
            if kind == "SENDPLAN_CANCEL_ALL":
                cancel_all += 1
                send_key = compute_send_key(run_id, rec)
                app.req_global_cancel()
                _append_exec(exec_path, {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "CANCEL_ALL_SENT",
                    "simulate": False,
                })
                append_jsonl(
                    sent_path,
                    {
                        "kind": "SENT_CANCEL_ALL",
                        "run_id": run_id,
                        "mode": mode,
                        "send_key": send_key,
                        "ts": rec.get("ts"),
                        "index": rec.get("index"),
                        "orderRef": rec.get("orderRef"),
                        "reason": rec.get("reason") or "CANCEL_ALL",
                        "server_next_valid_id": server_next_valid_id,
                        "cursor_last_before": cursor_last_before,
                        "start_order_id": start_order_id,
                        "simulate": False,
                    },
                )
                sent += 1

        # run-level limit reached -> skip all orders
        if already_sent_count >= run_limit_i:
            for rec in plans:
                if str(rec.get("kind") or "").strip().upper() == "SENDPLAN_ORDER":
                    send_key = compute_send_key(run_id, rec)
                    _append_exec(exec_path, {
                        "kind": "EXEC_EVENT",
                        "ts": _utc_now_z(),
                        "run_id": run_id,
                        "send_key": send_key,
                        "status": "BLOCKED_RUN_LIMIT",
                        "simulate": False,
                        "reason": f"RUN_LIMIT_REACHED={run_limit_i}",
                    })
                    append_jsonl(
                        would_path,
                        {
                            "kind": "WOULD_SEND",
                            "run_id": run_id,
                            "mode": mode,
                            "plan_kind": "SENDPLAN_ORDER",
                            "idempotency_key": rec.get("idempotency_key"),
                            "reason": f"RUN_LIMIT_REACHED={run_limit_i}",
                            "send_key": send_key,
                            "sendplan": rec,
                        },
                    )
                    skipped += 1

            ibkr_errors_head = app.errors[:8]
            return {
                "armed": True,
                "simulate": False,
                "run_id": run_id,
                "mode": mode,
                "k_limit_orders": k_limit_i,
                "run_limit_orders": run_limit_i,
                "server_next_valid_id": server_next_valid_id,
                "cursor_last_before": cursor_last_before,
                "start_order_id": start_order_id,
                "sendplan_path": str(sendplan_path),
                "run_report_path": str(run_report_path),
                "would_send_out": str(would_path),
                "sent_out": str(sent_path),
                "exec_out": str(exec_path),
                "ledger_path": str(ledger_path),
                "total": total,
                "order_plans": 0,
                "cancel_all": cancel_all,
                "sent": sent,
                "executed_orders": 0,
                "skipped": skipped,
                "parse_errors": parse_errors,
                "errors_count": len(errors),
                "errors_head": errors[:8],
                "ibkr_errors_head": ibkr_errors_head,
                "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
            }

        # Execute orders
        for rec in plans:
            kind = str(rec.get("kind") or "").strip().upper()
            if kind != "SENDPLAN_ORDER":
                continue
            order_plans += 1

            send_key = compute_send_key(run_id, rec)

            if mode != "ALLOW_NEW_ENTRIES":
                _append_exec(exec_path, {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "BLOCKED_MODE",
                    "simulate": False,
                    "reason": mode,
                })
                append_jsonl(
                    would_path,
                    {
                        "kind": "WOULD_SEND",
                        "run_id": run_id,
                        "mode": mode,
                        "plan_kind": kind,
                        "idempotency_key": rec.get("idempotency_key"),
                        "reason": f"MODE_BLOCKS_ORDERS:{mode}",
                        "send_key": send_key,
                        "sendplan": rec,
                    },
                )
                skipped += 1
                continue

            if executed_orders >= k_limit_i:
                _append_exec(exec_path, {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "BLOCKED_K_LIMIT",
                    "simulate": False,
                    "reason": f"K_LIMIT={k_limit_i}",
                })
                append_jsonl(
                    would_path,
                    {
                        "kind": "WOULD_SEND",
                        "run_id": run_id,
                        "mode": mode,
                        "plan_kind": kind,
                        "idempotency_key": rec.get("idempotency_key"),
                        "reason": f"K_LIMIT={k_limit_i}",
                        "send_key": send_key,
                        "sendplan": rec,
                    },
                )
                skipped += 1
                continue

            key = rec.get("idempotency_key")

            # Strong dedupe: ledger first, then sent-log keys
            if _ledger_has(ledger, send_key) or (isinstance(key, str) and key.strip() and key.strip() in seen_sent_keys):
                _append_exec(exec_path, {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "DEDUPED",
                    "simulate": False,
                })
                skipped += 1
                continue

            contract_dict = rec.get("contract")
            order_dict = rec.get("order")
            if not isinstance(contract_dict, dict) or not isinstance(order_dict, dict):
                errors.append("SENDPLAN_ORDER missing contract/order dict")
                skipped += 1
                continue

            contract = _build_ibkr_contract(contract_dict)
            order = _build_ibkr_order(order_dict, transmit=True)

            oid = app.next_order_id()

            # Reserve in ledger before placeOrder (idempotency anchor)
            _ledger_mark(ledger, send_key, state="RESERVED", order_id=int(oid), simulate=False)
            _save_ledger_atomic(ledger_path, ledger)

            _append_exec(exec_path, {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": send_key,
                "order_id": int(oid),
                "status": "SUBMITTED",
                "simulate": False,
            })

            app.place_order(oid, contract, order)
            last_oid_used = int(oid)

            time.sleep(0.25)
            st = app.status_for(oid)

            _append_exec(exec_path, {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": send_key,
                "order_id": int(oid),
                "status": "ORDER_STATUS",
                "simulate": False,
                "ibkr_order_status": st,
            })

            # Mark as sent in ledger
            _ledger_mark(ledger, send_key, state="SENT_REAL", order_id=int(oid), simulate=False)
            _save_ledger_atomic(ledger_path, ledger)

            append_jsonl(
                sent_path,
                {
                    "kind": "SENT_ORDER",
                    "run_id": run_id,
                    "mode": mode,
                    "idempotency_key": key,
                    "send_key": send_key,
                    "ibkr_order_id": oid,
                    "ibkr_order_status": st,
                    "server_next_valid_id": server_next_valid_id,
                    "cursor_last_before": cursor_last_before,
                    "start_order_id": start_order_id,
                    "ts": rec.get("ts"),
                    "index": rec.get("index"),
                    "orderRef": rec.get("orderRef"),
                    "contract": contract_dict,
                    "order": {**order_dict, "transmit": True},
                    "reason": rec.get("reason") or "OK",
                    "simulate": False,
                },
            )

            if isinstance(key, str) and key.strip():
                seen_sent_keys.add(key.strip())

            executed_orders += 1
            sent += 1

        if last_oid_used is not None:
            _set_cursor_last(conn, int(last_oid_used))

        ibkr_errors_head = app.errors[:8]

    finally:
        try:
            app.disconnect()
        except Exception:
            pass

    return {
        "armed": True,
        "simulate": False,
        "run_id": run_id,
        "mode": mode,
        "k_limit_orders": k_limit_i,
        "run_limit_orders": run_limit_i,
        "server_next_valid_id": server_next_valid_id,
        "cursor_last_before": cursor_last_before,
        "start_order_id": start_order_id,
        "sendplan_path": str(sendplan_path),
        "run_report_path": str(run_report_path),
        "would_send_out": str(would_path),
        "sent_out": str(sent_path),
        "exec_out": str(exec_path),
        "ledger_path": str(ledger_path),
        "total": total,
        "order_plans": order_plans,
        "cancel_all": cancel_all,
        "sent": sent,
        "executed_orders": executed_orders,
        "skipped": skipped,
        "parse_errors": parse_errors,
        "errors_count": len(errors),
        "errors_head": errors[:8],
        "ibkr_errors_head": ibkr_errors_head,
        "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
    }
