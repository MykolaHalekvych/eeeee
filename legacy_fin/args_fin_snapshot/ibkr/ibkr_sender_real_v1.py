# args/ibkr/ibkr_sender_real_v1.py
from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Set, Tuple

from args.ibkr.order_sanitize_v0 import sanitize_order_v0

# Optional dependency: central control plane.
# If import fails for any reason, we fall back to local safe readers (DRY_RUN default).
try:
    from args.control.execution_mode_v0 import get_execution_mode as _get_execution_mode  # type: ignore
    from args.control.execution_mode_v0 import is_stop_flag_present as _is_stop_flag_present  # type: ignore
except Exception:
    _get_execution_mode = None
    _is_stop_flag_present = None


SCHEMA = "ibkr_sender_real_v1"

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

EXEC_MODE_PATH = DATA_DIR / "execution_mode.json"  # DRY_RUN / EXIT_ONLY / FULL (safe-by-default)
STOP_FLAG_PATH = DATA_DIR / "stop.flag"

# Guardrails
K_LIMIT_ORDERS_DEFAULT = 1           # per invocation
RUN_LIMIT_ORDERS_DEFAULT = 1         # per run_id across repeated ARMED runs (safety!)
CURSOR_PATH = DATA_DIR / "ibkr_order_id_cursor_v1.json"

# Safety: never start order ids from tiny numbers
ORDER_ID_FLOOR = 1000

# Stage 5.5: execution lifecycle logs + idempotency ledger
EXEC_LEDGER_PATH_DEFAULT = DATA_DIR / "ibkr_exec_ledger_v1.json"


# -----------------------------
# Local safe control readers (fallback)
# -----------------------------
def _read_json_obj(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _norm_exec_mode(x: Any) -> str:
    s = str(x or "").strip().upper().replace("-", "_")
    if s in {"DRY_RUN", "EXIT_ONLY", "FULL"}:
        return s
    return "DRY_RUN"


def _get_exec_mode(repo_root: Path) -> str:
    """
    Returns one of: DRY_RUN / EXIT_ONLY / FULL.
    Safe-by-default: DRY_RUN if anything is missing or invalid.
    """
    # Prefer centralized control module (if present).
    if _get_execution_mode is not None:
        try:
            m = _get_execution_mode(repo_root)
            # Support: dataclass with .mode, dict, or raw string
            if isinstance(m, str):
                raw = m
            elif isinstance(m, dict):
                raw = m.get("mode")
            else:
                raw = getattr(m, "mode", None)
            return _norm_exec_mode(raw)
        except Exception:
            return "DRY_RUN"

    # Fallback: read args/data/execution_mode.json directly
    p = repo_root / "args" / "data" / "execution_mode.json"
    obj = _read_json_obj(p)
    if isinstance(obj, dict):
        return _norm_exec_mode(obj.get("mode"))
    return "DRY_RUN"


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _decorate_ibkr_error_details(*, error_code: int, error_string: str) -> Dict[str, Any]:
    """
    Adds actionable operator hints for known IBKR errors.
    Keep it small and deterministic.
    """
    details: Dict[str, Any] = {
        "error_code": int(error_code),
        "error_string": str(error_string),
    }

    # IBKR: "Error validating request.. cause - The API interface is currently in Read-Only mode."
    if int(error_code) == 321:
        details["operator_hint"] = (
            "IBKR API вернул Read-Only mode. Обычно это включён флаг 'Read-Only API' в TWS/IB Gateway "
            "или у аккаунта/сессии нет прав на торговые операции."
        )
        details["operator_action"] = (
            "Проверь настройки TWS/IB Gateway: отключи 'Read-Only API', перезапусти TWS/Gateway, "
            "убедись что подключаешься к правильному порту (paper обычно 7497, live часто 7496), "
            "и что аккаунт действительно в режиме Paper/Live как ожидается."
        )

    return details


# -----------------------------
# Stage 5 override policy (self-contained)
# -----------------------------
def _parse_iso_dt_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _update_control_state_json(path: Path, mutator) -> bool:
    """
    Best-effort. Returns True if file updated.
    """
    if not path.exists():
        return False
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    try:
        changed = bool(mutator(obj))
    except Exception:
        return False
    if not changed:
        return False
    try:
        _atomic_write_json(path, obj)
        return True
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class _Stage5OverrideCfg:
    enabled: bool
    autoreset: bool
    expires_at_utc: Optional[datetime]

    @property
    def active(self) -> bool:
        if not self.enabled:
            return False
        if self.expires_at_utc is None:
            return True
        return datetime.now(timezone.utc) < self.expires_at_utc


def _load_stage5_override_cfg(control: Dict[str, Any]) -> _Stage5OverrideCfg:
    enabled = bool(control.get("stage5_test_override") is True)
    autoreset = bool(control.get("stage5_test_override_autoreset", True))
    expires_raw = (
        control.get("stage5_test_override_expires_at_utc")
        or control.get("stage5_test_override_expires_utc")
        or control.get("stage5_test_override_until_utc")
    )
    expires_at = _parse_iso_dt_utc(expires_raw)
    return _Stage5OverrideCfg(enabled=enabled, autoreset=autoreset, expires_at_utc=expires_at)


def _detect_stop_flag(repo_root: Path, control: Dict[str, Any]) -> Tuple[bool, Optional[Path]]:
    """
    Returns (present, path_if_known).
    Checks:
      - control_state.json: stop_flag_path / STOP_FLAG_PATH
      - env: ARGS_STOP_FLAG_PATH
      - args/data/stop.flag
      - args/control/stop.flag
      - repo_root/stop.flag
    """
    # Central control plane may exist, but only returns bool (no path).
    if _is_stop_flag_present is not None:
        try:
            if bool(_is_stop_flag_present(repo_root)):
                return True, None
        except Exception:
            pass

    p_raw = control.get("stop_flag_path") or control.get("STOP_FLAG_PATH")
    if isinstance(p_raw, str) and p_raw.strip():
        p = Path(p_raw.strip())
        if p.exists():
            return True, p

    env = os.getenv("ARGS_STOP_FLAG_PATH")
    if env:
        p = Path(env)
        if p.exists():
            return True, p

    candidates = [
        repo_root / "args" / "data" / "stop.flag",
        repo_root / "args" / "control" / "stop.flag",
        repo_root / "stop.flag",
    ]
    for p in candidates:
        if p.exists():
            return True, p

    return False, None


def _try_autoreset_stage5_override(control_state_path: Path, *, reason: str) -> bool:
    """
    Best-effort auto-reset: stage5_test_override -> false.
    Writes audit fields for traceability.
    """

    def _mut(d: MutableMapping[str, Any]) -> bool:
        if d.get("stage5_test_override") is not True:
            return False
        d["stage5_test_override"] = False
        d["stage5_test_override_last_autoreset_at_utc"] = _utc_now_z()
        d["stage5_test_override_last_autoreset_reason"] = str(reason)
        return True

    return _update_control_state_json(control_state_path, _mut)


# -----------------------------
# IBKR error classification (self-contained)
# -----------------------------
@dataclass(frozen=True, slots=True)
class _IbkrErrorClass:
    severity: str          # INFO / WARNING / ERROR
    count_as_error_head: bool
    kind: str


_IBKR_INFO_CODES: Set[int] = {2104, 2106, 2158, 1101, 1102}
_IBKR_WARNING_CODES: Set[int] = {399, 2103, 2105, 2157, 2107, 2108}


def _classify_ibkr_error(*, req_id: Any, code: int, msg: str) -> _IbkrErrorClass:
    if int(code) in _IBKR_INFO_CODES:
        return _IbkrErrorClass(severity="INFO", count_as_error_head=False, kind="IBKR_INFO_CODE")
    if int(code) in _IBKR_WARNING_CODES:
        return _IbkrErrorClass(severity="WARNING", count_as_error_head=False, kind="IBKR_WARNING_CODE")
    return _IbkrErrorClass(severity="ERROR", count_as_error_head=True, kind="IBKR_ERROR")


# -----------------------------
# Control plane files
# -----------------------------
def load_control_state(path: Path) -> Dict[str, Any]:
    """
    control_state.json (recommended shape):
      {
        "armed": false,
        "simulate": false,
        "k_limit_orders": 1,
        "run_limit_orders": 1,
        "ledger_path": "ibkr_exec_ledger_v1.json",  # optional (relative to args/data)
        "stage5_test_override": false,              # optional
        "stage5_test_override_autoreset": true,     # optional
        "stage5_test_override_expires_at_utc": "2026-01-01T00:00:00Z",  # optional
        "stage5_test_max_lmt_price": 0.05           # optional
      }
    """
    if not path.exists():
        return {"armed": False, "simulate": False, "source": "missing_defaults"}

    try:
        # BOM-tolerant for Windows/PowerShell Set-Content UTF8
        obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
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
    """
    Returns one of: ALLOW_NEW_ENTRIES / ONLY_EXITS / NO_TRADE.
    Safe-by-default: NO_TRADE.
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
    obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    if not isinstance(obj, dict):
        raise ValueError("run_report is not a JSON object")
    return obj


# -----------------------------
# JSONL utilities (strict, with correct parse_errors)
# -----------------------------
def iter_jsonl_strict(path: Path) -> Tuple[Iterable[Dict[str, Any]], Dict[str, int]]:
    stats = {"parse_errors": 0}

    def gen() -> Iterable[Dict[str, Any]]:
        if not path.exists():
            return
        # utf-8-sig to tolerate BOM on Windows/PowerShell pipelines
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
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
            obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
            if isinstance(obj, dict):
                return obj
        except Exception:
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
    """
    Counts SENT_ORDER / SENT_ORDER_SIM for this run_id.
    Used for run_limit_orders across repeated ARMED invocations.
    """
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
    obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
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
        obj = json.loads(CURSOR_PATH.read_text(encoding="utf-8-sig", errors="replace"))
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


def _validate_sendplan_record(rec: Dict[str, Any], errors: List[str], *, expected_run_id: str) -> None:
    """
    Sender-level strict validation.
    - Each JSONL record MUST contain run_id and it MUST match expected_run_id.
    - SENDPLAN_ORDER MUST have transmit=False in the plan.
    """
    kind = str(rec.get("kind") or "").strip().upper()
    _require(kind in {"SENDPLAN_ORDER", "SENDPLAN_CANCEL_ALL"}, f"unknown sendplan kind: {kind}", errors)

    rid = str(rec.get("run_id") or "").strip()
    _require(bool(rid), "missing run_id in sendplan record", errors)
    if rid:
        _require(rid == str(expected_run_id), f"sendplan.run_id mismatch: {rid} != {expected_run_id}", errors)

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
    """
    Build IBKR Order object from sendplan dict.
    IMPORTANT: keep it minimal; sanitation is applied centrally via sanitize_order_v0().
    """
    from ibapi.order import Order  # type: ignore

    o = Order()
    o.action = str(order_dict.get("action") or "")
    o.orderType = str(order_dict.get("orderType") or "")
    o.totalQuantity = float(order_dict.get("totalQuantity") or 0)
    o.tif = str(order_dict.get("tif") or "DAY")
    o.transmit = bool(transmit)

    # Optional fields (only if present)
    if "outsideRth" in order_dict:
        try:
            o.outsideRth = bool(order_dict.get("outsideRth"))
        except Exception:
            pass
    if "lmtPrice" in order_dict and str(o.orderType).upper() == "LMT":
        try:
            o.lmtPrice = float(order_dict.get("lmtPrice") or 0.0)
        except Exception:
            pass
    if "account" in order_dict and isinstance(order_dict.get("account"), str):
        try:
            o.account = str(order_dict.get("account") or "")
        except Exception:
            pass

    return o


# -----------------------------
# IBKR app wrapper (nextValidId + orderStatus visibility + exec logging)
# -----------------------------
class _IbkrApp:
    def __init__(self, *, run_id: str, exec_path: Path) -> None:
        from ibapi.client import EClient  # type: ignore
        from ibapi.wrapper import EWrapper  # type: ignore

        self._run_id = str(run_id)
        self._exec_path = exec_path
        self._lock = threading.Lock()

        self._connected = threading.Event()
        self._errors: List[Dict[str, Any]] = []
        self._order_status: Dict[int, str] = {}
        self._next_id: Optional[int] = None

        # map orderId -> send_key (to tie callbacks back to a sendplan item)
        self._order_to_sendkey: Dict[int, str] = {}

        def _log(ev: Dict[str, Any]) -> None:
            ev = dict(ev)
            ev.setdefault("kind", "IBKR_EVENT")
            ev.setdefault("ts", _utc_now_z())
            ev.setdefault("run_id", self._run_id)
            with self._lock:
                _append_exec(self._exec_path, ev)

        class App(EWrapper, EClient):  # type: ignore
            def __init__(self, outer: "_IbkrApp") -> None:
                EClient.__init__(self, self)
                self._outer = outer

            def nextValidId(self, orderId: int) -> None:
                self._outer._next_id = int(orderId)
                _log({"event": "nextValidId", "order_id": int(orderId)})
                self._outer._connected.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="") -> None:
                try:
                    rid: Any = int(reqId) if str(reqId).lstrip("-").isdigit() else reqId
                except Exception:
                    rid = reqId

                try:
                    code_i = int(errorCode)
                except Exception:
                    code_i = 0
                msg_s = str(errorString)

                cls = _classify_ibkr_error(req_id=rid, code=code_i, msg=msg_s)

                rec: Dict[str, Any] = {
                    "event": "error",
                    "reqId": rid,
                    "code": code_i,
                    "msg": msg_s,
                    "severity": cls.severity,
                    "error_class": cls.kind,
                    "count_as_error_head": bool(cls.count_as_error_head),
                    "details": _decorate_ibkr_error_details(error_code=code_i, error_string=msg_s),
                }

                # best-effort: if reqId looks like orderId, attach send_key
                try:
                    oid = int(reqId)
                    sk = self._outer._order_to_sendkey.get(oid)
                    if sk:
                        rec["order_id"] = oid
                        rec["send_key"] = sk
                except Exception:
                    pass

                if advancedOrderRejectJson:
                    rec["advanced_reject_json"] = str(advancedOrderRejectJson)

                # Only treat true ERRORs as "errors_head" / blockers
                if cls.count_as_error_head:
                    self._outer._errors.append({"reqId": rid, "code": code_i, "msg": msg_s})

                _log(rec)

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

                st = str(status)
                self._outer._order_status[oid] = st

                rec: Dict[str, Any] = {
                    "event": "orderStatus",
                    "order_id": oid,
                    "status": st,
                    "filled": float(filled) if filled is not None else None,
                    "remaining": float(remaining) if remaining is not None else None,
                    "avgFillPrice": float(avgFillPrice) if avgFillPrice is not None else None,
                    "lastFillPrice": float(lastFillPrice) if lastFillPrice is not None else None,
                    "permId": int(permId) if str(permId).isdigit() else permId,
                    "parentId": int(parentId) if str(parentId).isdigit() else parentId,
                    "clientId": int(clientId) if str(clientId).isdigit() else clientId,
                    "whyHeld": str(whyHeld) if whyHeld is not None else "",
                    "mktCapPrice": float(mktCapPrice) if mktCapPrice is not None else None,
                }

                sk = self._outer._order_to_sendkey.get(oid)
                if sk:
                    rec["send_key"] = sk

                _log(rec)

            def openOrder(self, orderId, contract, order, orderState) -> None:
                # keep it light: do not dump entire objects
                try:
                    oid = int(orderId)
                except Exception:
                    return
                rec: Dict[str, Any] = {
                    "event": "openOrder",
                    "order_id": oid,
                    "orderState_status": getattr(orderState, "status", None),
                    "localSymbol": getattr(contract, "localSymbol", None),
                    "secType": getattr(contract, "secType", None),
                    "exchange": getattr(contract, "exchange", None),
                    "action": getattr(order, "action", None),
                    "orderType": getattr(order, "orderType", None),
                    "totalQuantity": getattr(order, "totalQuantity", None),
                }
                sk = self._outer._order_to_sendkey.get(oid)
                if sk:
                    rec["send_key"] = sk
                _log(rec)

        self._app = App(self)

    def register_send_key(self, order_id: int, send_key: str) -> None:
        with self._lock:
            self._order_to_sendkey[int(order_id)] = str(send_key)

    def connect_and_start(self, conn: IbkrConn, timeout_s: float = 8.0) -> int:
        with self._lock:
            _append_exec(
                self._exec_path,
                {
                    "kind": "IBKR_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": self._run_id,
                    "event": "connect_attempt",
                    "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
                },
            )

        self._app.connect(conn.host, conn.port, conn.client_id)
        t = threading.Thread(target=self._app.run, daemon=True)
        t.start()

        if not self._connected.wait(timeout=timeout_s):
            with self._lock:
                _append_exec(
                    self._exec_path,
                    {
                        "kind": "IBKR_EVENT",
                        "ts": _utc_now_z(),
                        "run_id": self._run_id,
                        "event": "connect_timeout",
                        "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
                        "operator_hint": (
                            "TWS/IB Gateway не ответил nextValidId. Проверь что запущен, порт верный, "
                            "API разрешён, firewall не блокирует."
                        ),
                    },
                )
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
        finally:
            with self._lock:
                _append_exec(
                    self._exec_path,
                    {"kind": "IBKR_EVENT", "ts": _utc_now_z(), "run_id": self._run_id, "event": "disconnect"},
                )

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
# Permissions fallback (safe)
# -----------------------------
@dataclass(frozen=True)
class _Perms:
    force_simulate: bool
    allow_cancel_all: bool
    allow_entry_orders: bool
    allow_exit_orders: bool
    explanation: str = ""


def _fallback_compute_perms(exec_mode: str, run_mode: str, *, reason: str) -> _Perms:
    # safest: force simulation and block all orders, still allow cancel_all
    return _Perms(
        force_simulate=True,
        allow_cancel_all=True,
        allow_entry_orders=False,
        allow_exit_orders=False,
        explanation=f"FALLBACK_PERMS:{reason}",
    )


def _fallback_allowed_sendplan_record(perms: _Perms, rec: Dict[str, Any]) -> Tuple[bool, str]:
    kind = str(rec.get("kind") or "").strip().upper()
    if kind == "SENDPLAN_CANCEL_ALL":
        return (bool(perms.allow_cancel_all), "CANCEL_ALL_ALLOWED" if perms.allow_cancel_all else "CANCEL_ALL_BLOCKED")
    if kind == "SENDPLAN_ORDER":
        # safest: block all orders in fallback mode
        return (False, "ORDERS_BLOCKED_FALLBACK")
    return (False, "UNKNOWN_KIND")


# -----------------------------
# Real sender (stage8 guarded)
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

    run_mode = extract_run_mode(report)  # ALLOW_NEW_ENTRIES / ONLY_EXITS / NO_TRADE

    control = load_control_state(control_state_path)
    armed = bool(control.get("armed") is True)
    simulate_cfg = bool(control.get("simulate") is True)

    exec_mode = _get_exec_mode(REPO_ROOT)  # DRY_RUN / EXIT_ONLY / FULL

    # Stop-flag (more complete detection than legacy args/data/stop.flag only)
    stop_flag_present, stop_flag_path = _detect_stop_flag(REPO_ROOT, control)

    # ---- permissions matrix (central) ----
    try:
        from args.control.ibkr_effective_permissions_v0 import (  # type: ignore
            compute_effective_permissions,  # (exec_mode, run_mode) -> perms
            allowed_sendplan_record,  # (perms, rec) -> (ok, why)
        )

        perms_any = compute_effective_permissions(exec_mode, run_mode)
        if isinstance(perms_any, dict):
            getv = perms_any.get  # type: ignore[assignment]
        else:
            getv = lambda k, d=None: getattr(perms_any, k, d)

        perms = _Perms(
            force_simulate=bool(getv("force_simulate", False)),
            allow_cancel_all=bool(getv("allow_cancel_all", True)),
            allow_entry_orders=bool(getv("allow_entry_orders", False)),
            allow_exit_orders=bool(getv("allow_exit_orders", False)),
            explanation=str(getv("explanation", "")),
        )
        _allowed_sendplan_record = allowed_sendplan_record
    except Exception as e:
        perms = _fallback_compute_perms(exec_mode, run_mode, reason=f"IMPORT_FAIL:{type(e).__name__}")
        _allowed_sendplan_record = None

    # DRY_RUN does NOT disarm — it forces simulation
    effective_simulate = bool(simulate_cfg) or bool(perms.force_simulate) or (exec_mode == "DRY_RUN")

    # Stage 5 engineering override (STRICT, opt-in)
    override_cfg = _load_stage5_override_cfg(control)
    try:
        stage5_max_lmt_price = float(control.get("stage5_test_max_lmt_price", 0.05))
    except Exception:
        stage5_max_lmt_price = 0.05
    if stage5_max_lmt_price <= 0:
        stage5_max_lmt_price = 0.05

    def _stage5_is_safe_test_order(rec: Dict[str, Any]) -> bool:
        # override must NOT bypass STOP_FLAG
        # override must NOT bypass EXEC_MODE != FULL (including DRY_RUN)
        # override must bypass ONLY run_mode=NO_TRADE (explicit policy)
        if stop_flag_present:
            return False
        if not override_cfg.active:
            return False
        if exec_mode != "FULL":
            return False
        if run_mode != "NO_TRADE":
            return False
        if str(rec.get("kind") or "").strip().upper() != "SENDPLAN_ORDER":
            return False
        ik = str(rec.get("idempotency_key") or "").strip()
        if not ik.startswith("STAGE5_TEST_"):
            return False
        od = rec.get("order") if isinstance(rec.get("order"), dict) else {}
        action = str(od.get("action") or "").strip().upper()
        otype = str(od.get("orderType") or "").strip().upper()
        try:
            qty = float(od.get("totalQuantity") or 0)
        except Exception:
            qty = 0.0
        try:
            lmt = float(od.get("lmtPrice"))
        except Exception:
            lmt = None
        return (
            action == "BUY"
            and otype == "LMT"
            and qty == 1.0
            and lmt is not None
            and lmt <= stage5_max_lmt_price
        )

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

    # BOOT log (always)
    _append_exec(
        exec_path,
        {
            "kind": "EXEC_EVENT",
            "ts": _utc_now_z(),
            "run_id": run_id,
            "send_key": f"{run_id}:BOOT",
            "status": "BOOT",
            "armed": bool(armed),
            "simulate_cfg": bool(simulate_cfg),
            "simulate": bool(effective_simulate),
            "exec_mode": exec_mode,
            "stop_flag": bool(stop_flag_present),
            "stop_flag_path": str(stop_flag_path) if stop_flag_path else None,
            "run_mode": run_mode,
            "stage5_test_override": bool(override_cfg.enabled),
            "stage5_test_override_active": bool(override_cfg.active),
            "stage5_test_override_autoreset": bool(override_cfg.autoreset),
            "stage5_test_override_expires_at_utc": override_cfg.expires_at_utc.isoformat() if override_cfg.expires_at_utc else None,
            "stage5_test_max_lmt_price": float(stage5_max_lmt_price),
            "perms": {
                "force_simulate": bool(perms.force_simulate),
                "allow_cancel_all": bool(perms.allow_cancel_all),
                "allow_entry_orders": bool(perms.allow_entry_orders),
                "allow_exit_orders": bool(perms.allow_exit_orders),
                "explanation": perms.explanation,
            },
            "control_source": str(control.get("source") or ""),
        },
    )

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
        _validate_sendplan_record(rec, errors, expected_run_id=run_id)
        plans.append(rec)
        if str(rec.get("kind") or "").strip().upper() == "SENDPLAN_ORDER":
            order_plans += 1
        if len(errors) >= 50:
            break

    parse_errors = int(stats.get("parse_errors") or 0)

    # helper: emit would_send + exec event
    def emit_would(rec: Dict[str, Any], *, reason: str, status: str = "WOULD_SEND") -> None:
        nonlocal would
        kind = str(rec.get("kind") or "").strip().upper()
        send_key = compute_send_key(run_id, rec)
        _append_exec(
            exec_path,
            {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": send_key,
                "status": status,
                "armed": bool(armed),
                "simulate": bool(effective_simulate),
                "exec_mode": exec_mode,
                "stop_flag": bool(stop_flag_present),
                "run_mode": run_mode,
                "reason": str(reason),
                "plan_kind": kind,
            },
        )
        append_jsonl(
            would_path,
            {
                "kind": "WOULD_SEND",
                "run_id": run_id,
                "mode": run_mode,
                "exec_mode": exec_mode,
                "stop_flag": bool(stop_flag_present),
                "plan_kind": kind,
                "idempotency_key": rec.get("idempotency_key"),
                "reason": str(reason),
                "send_key": send_key,
                "sendplan": rec,
            },
        )
        would += 1

    # If sendplan has any parse/validation error -> block everything (safe)
    if parse_errors > 0 or errors:
        disarm_reason = f"SENDPLAN_INVALID parse_errors={parse_errors} errors={len(errors)}"
        for rec in plans:
            emit_would(rec, reason=disarm_reason, status="BLOCKED_SENDPLAN_INVALID")
        return {
            "armed": bool(armed),
            "simulate_cfg": bool(simulate_cfg),
            "simulate": bool(effective_simulate),
            "exec_mode": exec_mode,
            "stop_flag": bool(stop_flag_present),
            "disarm_reason": disarm_reason,
            "run_id": run_id,
            "mode": run_mode,
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
            "executed_orders": 0,
            "skipped": skipped,
            "parse_errors": parse_errors,
            "errors_count": len(errors),
            "errors_head": errors[:8],
        }

    # DISARM
    if not armed:
        disarm_reason = "DISARMED"
        for rec in plans:
            emit_would(rec, reason=disarm_reason, status="WOULD_SEND")
        return {
            "armed": bool(armed),
            "simulate_cfg": bool(simulate_cfg),
            "simulate": bool(effective_simulate),
            "exec_mode": exec_mode,
            "stop_flag": bool(stop_flag_present),
            "disarm_reason": disarm_reason,
            "run_id": run_id,
            "mode": run_mode,
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
            "executed_orders": 0,
            "skipped": skipped,
            "parse_errors": parse_errors,
            "errors_count": len(errors),
            "errors_head": errors[:8],
        }

    # Apply permission matrix per record (+ stop_flag gating + stage5 override)
    allowed_plans: List[Dict[str, Any]] = []

    for rec in plans:
        kind = str(rec.get("kind") or "").strip().upper()

        # STOP_FLAG policy: allow only CANCEL ops (no submits)
        if stop_flag_present and kind == "SENDPLAN_ORDER":
            emit_would(rec, reason="STOP_FLAG blocks order submit", status="BLOCKED_STOP_FLAG")
            skipped += 1
            continue

        if _allowed_sendplan_record is None:
            ok, why = _fallback_allowed_sendplan_record(perms, rec)
        else:
            ok, why = _allowed_sendplan_record(perms, rec)

        if ok:
            allowed_plans.append(rec)
            continue

        # -------- Stage 5 engineering override (STRICT, opt-in) --------
        if _stage5_is_safe_test_order(rec):
            send_key = compute_send_key(run_id, rec)
            _append_exec(
                exec_path,
                {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "OVERRIDE_USED",
                    "override": "stage5_test_override",
                    "bypassed": ["run_mode=NO_TRADE"],
                    "simulate": bool(effective_simulate),
                    "exec_mode": exec_mode,
                    "run_mode": run_mode,
                    "blocked_reason": str(why),
                    "stage5_test_max_lmt_price": float(stage5_max_lmt_price),
                },
            )
            allowed_plans.append(rec)
            continue

        emit_would(rec, reason=f"BLOCKED_PERMS:{why}", status="BLOCKED_PERMS")
        skipped += 1

    # Split plans
    cancel_plans = [p for p in allowed_plans if str(p.get("kind") or "").strip().upper() == "SENDPLAN_CANCEL_ALL"]
    order_plans_list = [p for p in allowed_plans if str(p.get("kind") or "").strip().upper() == "SENDPLAN_ORDER"]

    # -----------------------------
    # ARMED + SIMULATE (no live IBKR calls)
    # -----------------------------
    if effective_simulate:
        # explicit log skip connect
        _append_exec(
            exec_path,
            {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": f"{run_id}:CONNECT",
                "status": "CONNECT_SKIPPED",
                "why": "simulate=true (includes exec_mode=DRY_RUN)",
                "has_cancel": bool(cancel_plans),
                "has_submit": bool(order_plans_list),
                "exec_mode": exec_mode,
                "run_mode": run_mode,
                "stop_flag": bool(stop_flag_present),
            },
        )

        conn_sim = load_ibkr_connection(DATA_DIR / "ibkr_connection_v0.json")
        cursor_last_before = _get_cursor_last(conn_sim)
        next_oid = max(int(cursor_last_before) + 1, int(ORDER_ID_FLOOR))
        last_oid_used: Optional[int] = None

        # cancel_all (simulated)
        for rec in cancel_plans:
            cancel_all += 1
            send_key = compute_send_key(run_id, rec)
            _append_exec(
                exec_path,
                {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "CANCEL_ALL_SIM",
                    "simulate": True,
                    "exec_mode": exec_mode,
                    "run_mode": run_mode,
                },
            )
            append_jsonl(
                sent_path,
                {
                    "kind": "SENT_CANCEL_ALL_SIM",
                    "run_id": run_id,
                    "mode": run_mode,
                    "exec_mode": exec_mode,
                    "send_key": send_key,
                    "simulate": True,
                    "reason": rec.get("reason") or "CANCEL_ALL",
                },
            )
            sent += 1

        # orders (simulated)
        for rec in order_plans_list:
            send_key = compute_send_key(run_id, rec)
            ik = rec.get("idempotency_key")

            if (already_sent_count + executed_orders) >= run_limit_i:
                emit_would(rec, reason=f"RUN_LIMIT_REACHED={run_limit_i}", status="BLOCKED_RUN_LIMIT")
                skipped += 1
                continue

            if executed_orders >= k_limit_i:
                emit_would(rec, reason=f"K_LIMIT={k_limit_i}", status="BLOCKED_K_LIMIT")
                skipped += 1
                continue

            if _ledger_has(ledger, send_key) or (isinstance(ik, str) and ik.strip() in seen_sent_keys):
                _append_exec(
                    exec_path,
                    {
                        "kind": "EXEC_EVENT",
                        "ts": _utc_now_z(),
                        "run_id": run_id,
                        "send_key": send_key,
                        "status": "DEDUPED",
                        "simulate": True,
                        "exec_mode": exec_mode,
                    },
                )
                skipped += 1
                continue

            oid = next_oid
            next_oid += 1
            last_oid_used = oid

            _ledger_mark(ledger, send_key, state="RESERVED", order_id=oid, simulate=True)
            _save_ledger_atomic(ledger_path, ledger)

            _append_exec(
                exec_path,
                {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "order_id": oid,
                    "status": "SUBMITTED",
                    "simulate": True,
                    "exec_mode": exec_mode,
                },
            )
            _append_exec(
                exec_path,
                {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "order_id": oid,
                    "status": "ACK",
                    "simulate": True,
                    "exec_mode": exec_mode,
                },
            )

            _ledger_mark(ledger, send_key, state="SENT_SIM", order_id=oid, simulate=True)
            _save_ledger_atomic(ledger_path, ledger)

            append_jsonl(
                sent_path,
                {
                    "kind": "SENT_ORDER_SIM",
                    "run_id": run_id,
                    "mode": run_mode,
                    "exec_mode": exec_mode,
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
            "simulate_cfg": bool(simulate_cfg),
            "simulate": True,
            "exec_mode": exec_mode,
            "stop_flag": bool(stop_flag_present),
            "run_id": run_id,
            "mode": run_mode,
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

    # -----------------------------
    # ARMED REAL (simulate=false) - preflight + connect + send
    # -----------------------------
    conn = load_ibkr_connection(DATA_DIR / "ibkr_connection_v0.json")

    has_cancel = bool(cancel_plans)
    has_submit = bool(order_plans_list)

    # Stage6 connect policy:
    # - if STOP_FLAG blocks submits and there are no cancels -> do not connect
    if stop_flag_present and (not has_cancel):
        _append_exec(
            exec_path,
            {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": f"{run_id}:CONNECT",
                "status": "CONNECT_SKIPPED",
                "why": "STOP_FLAG blocks submit; no cancel ops",
                "has_cancel": has_cancel,
                "has_submit": has_submit,
                "exec_mode": exec_mode,
                "run_mode": run_mode,
                "stop_flag": True,
                "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
            },
        )
        return {
            "armed": True,
            "simulate_cfg": bool(simulate_cfg),
            "simulate": False,
            "exec_mode": exec_mode,
            "stop_flag": True,
            "run_id": run_id,
            "mode": run_mode,
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
            "cancel_all": 0,
            "sent": 0,
            "executed_orders": 0,
            "skipped": skipped,
            "parse_errors": parse_errors,
            "errors_count": len(errors),
            "errors_head": errors[:8],
            "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
        }

    # Decide if we need to connect at all (cancel_all always needs connect).
    # For orders, check quickly whether at least one order could be sent (run_limit/k_limit/dedupe).
    sendable_orders = 0
    for rec in order_plans_list:
        send_key = compute_send_key(run_id, rec)
        ik = rec.get("idempotency_key")

        if (already_sent_count + sendable_orders) >= run_limit_i:
            continue
        if sendable_orders >= k_limit_i:
            continue
        if _ledger_has(ledger, send_key) or (isinstance(ik, str) and ik.strip() in seen_sent_keys):
            continue
        sendable_orders += 1

    need_connect = bool(cancel_plans) or (sendable_orders > 0)

    if not need_connect:
        _append_exec(
            exec_path,
            {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": f"{run_id}:CONNECT",
                "status": "CONNECT_SKIPPED",
                "why": "no actionable ops (after dedupe/limits/perms)",
                "has_cancel": has_cancel,
                "has_submit": has_submit,
                "sendable_orders": int(sendable_orders),
                "exec_mode": exec_mode,
                "run_mode": run_mode,
                "stop_flag": bool(stop_flag_present),
                "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
            },
        )
        return {
            "armed": True,
            "simulate_cfg": bool(simulate_cfg),
            "simulate": False,
            "exec_mode": exec_mode,
            "stop_flag": bool(stop_flag_present),
            "run_id": run_id,
            "mode": run_mode,
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
            "cancel_all": 0,
            "sent": 0,
            "executed_orders": 0,
            "skipped": skipped,
            "parse_errors": parse_errors,
            "errors_count": len(errors),
            "errors_head": errors[:8],
            "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
        }

    _append_exec(
        exec_path,
        {
            "kind": "EXEC_EVENT",
            "ts": _utc_now_z(),
            "run_id": run_id,
            "send_key": f"{run_id}:CONNECT",
            "status": "CONNECT_WILL_CONNECT",
            "why": "actionable ops exist",
            "has_cancel": has_cancel,
            "has_submit": has_submit,
            "sendable_orders": int(sendable_orders),
            "exec_mode": exec_mode,
            "run_mode": run_mode,
            "stop_flag": bool(stop_flag_present),
            "override_enabled": bool(override_cfg.enabled),
            "override_active": bool(override_cfg.active),
            "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
        },
    )

    # --- PRE-FLIGHT (required for REAL) ---
    try:
        timeout_s = float(control.get("ibkr_preflight_timeout_s", 10.0))
    except Exception:
        timeout_s = 10.0

    try:
        from args.ibkr.ibkr_preflight_v0 import IbkrEndpoint, preflight_next_valid_id  # type: ignore
    except Exception as e:
        disarm_reason = f"PREFLIGHT_IMPORT_FAILED:{type(e).__name__}"
        for rec in allowed_plans:
            emit_would(rec, reason=disarm_reason, status="BLOCKED_PREFLIGHT")
        return {
            "armed": True,
            "simulate_cfg": bool(simulate_cfg),
            "simulate": False,
            "exec_mode": exec_mode,
            "stop_flag": bool(stop_flag_present),
            "disarm_reason": disarm_reason,
            "run_id": run_id,
            "mode": run_mode,
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
            "executed_orders": 0,
            "skipped": skipped,
            "parse_errors": parse_errors,
            "errors_count": len(errors),
            "errors_head": errors[:8],
            "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
        }

    try:
        nxt = preflight_next_valid_id(IbkrEndpoint(conn.host, conn.port, conn.client_id), timeout_s)
        _append_exec(
            exec_path,
            {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": f"{run_id}:PREFLIGHT",
                "status": "PREFLIGHT_OK",
                "simulate": False,
                "exec_mode": exec_mode,
                "run_mode": run_mode,
                "server_next_valid_id": int(nxt),
                "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
            },
        )
    except Exception as e:
        err = str(e)
        _append_exec(
            exec_path,
            {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": f"{run_id}:PREFLIGHT",
                "status": "PREFLIGHT_FAILED",
                "simulate": False,
                "exec_mode": exec_mode,
                "run_mode": run_mode,
                "error": err,
                "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
            },
        )
        disarm_reason = f"PREFLIGHT_FAILED:{type(e).__name__}"
        for rec in allowed_plans:
            emit_would(rec, reason=f"{disarm_reason}:{err}", status="BLOCKED_PREFLIGHT")
        return {
            "armed": True,
            "simulate_cfg": bool(simulate_cfg),
            "simulate": False,
            "exec_mode": exec_mode,
            "stop_flag": bool(stop_flag_present),
            "disarm_reason": disarm_reason,
            "run_id": run_id,
            "mode": run_mode,
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
            "executed_orders": 0,
            "skipped": skipped,
            "parse_errors": parse_errors,
            "errors_count": len(errors),
            "errors_head": (errors + [err])[:8],
            "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
        }

    app = _IbkrApp(run_id=run_id, exec_path=exec_path)

    server_next_valid_id: Optional[int] = None
    cursor_last_before = _get_cursor_last(conn)
    start_order_id: Optional[int] = None
    last_oid_used: Optional[int] = None
    ibkr_errors_head: List[Dict[str, Any]] = []

    # auto-reset override after first successful submit (real)
    override_autoreset_done = False

    try:
        server_next_valid_id = app.connect_and_start(conn, timeout_s=timeout_s)

        # start order id = max(server next, cursor_last+1, ORDER_ID_FLOOR)
        start_order_id = max(int(server_next_valid_id), int(cursor_last_before) + 1, int(ORDER_ID_FLOOR))
        app.set_next_id(start_order_id)

        _append_exec(
            exec_path,
            {
                "kind": "EXEC_EVENT",
                "ts": _utc_now_z(),
                "run_id": run_id,
                "send_key": f"{run_id}:CONNECTED",
                "status": "CONNECTED",
                "simulate": False,
                "exec_mode": exec_mode,
                "run_mode": run_mode,
                "server_next_valid_id": server_next_valid_id,
                "cursor_last_before": cursor_last_before,
                "start_order_id": start_order_id,
                "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id},
            },
        )

        # cancel_all first
        for rec in cancel_plans:
            cancel_all += 1
            send_key = compute_send_key(run_id, rec)
            app.req_global_cancel()
            _append_exec(
                exec_path,
                {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "status": "CANCEL_ALL_SENT",
                    "simulate": False,
                    "exec_mode": exec_mode,
                },
            )
            append_jsonl(
                sent_path,
                {
                    "kind": "SENT_CANCEL_ALL",
                    "run_id": run_id,
                    "mode": run_mode,
                    "exec_mode": exec_mode,
                    "send_key": send_key,
                    "reason": rec.get("reason") or "CANCEL_ALL",
                    "server_next_valid_id": server_next_valid_id,
                    "cursor_last_before": cursor_last_before,
                    "start_order_id": start_order_id,
                    "simulate": False,
                },
            )
            sent += 1

        # orders
        for rec in order_plans_list:
            send_key = compute_send_key(run_id, rec)
            key = rec.get("idempotency_key")

            if stop_flag_present:
                emit_would(rec, reason="STOP_FLAG blocks order submit (late guard)", status="BLOCKED_STOP_FLAG")
                skipped += 1
                continue

            if (already_sent_count + executed_orders) >= run_limit_i:
                emit_would(rec, reason=f"RUN_LIMIT_REACHED={run_limit_i}", status="BLOCKED_RUN_LIMIT")
                skipped += 1
                continue

            if executed_orders >= k_limit_i:
                emit_would(rec, reason=f"K_LIMIT={k_limit_i}", status="BLOCKED_K_LIMIT")
                skipped += 1
                continue

            # Strong dedupe: ledger first, then sent-log keys
            if _ledger_has(ledger, send_key) or (isinstance(key, str) and key.strip() and key.strip() in seen_sent_keys):
                _append_exec(
                    exec_path,
                    {
                        "kind": "EXEC_EVENT",
                        "ts": _utc_now_z(),
                        "run_id": run_id,
                        "send_key": send_key,
                        "status": "DEDUPED",
                        "simulate": False,
                        "exec_mode": exec_mode,
                    },
                )
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

            # P1.3B: canonical sanitation BEFORE reserving ledger / placing
            try:
                san = sanitize_order_v0(order)
            except Exception as e:
                _append_exec(
                    exec_path,
                    {
                        "kind": "EXEC_EVENT",
                        "ts": _utc_now_z(),
                        "run_id": run_id,
                        "send_key": send_key,
                        "status": "ORDER_SANITIZE_EXCEPTION",
                        "error": f"{type(e).__name__}: {e}",
                        "why": "sanitize_order_v0 failed; blocking submit for safety",
                    },
                )
                skipped += 1
                continue

            if isinstance(san, dict) and san:
                _append_exec(
                    exec_path,
                    {
                        "kind": "EXEC_EVENT",
                        "ts": _utc_now_z(),
                        "run_id": run_id,
                        "send_key": send_key,
                        "status": "ORDER_SANITIZED",
                        "sanitized": san,
                    },
                )

            # Reserve in ledger before placeOrder (idempotency anchor)
            oid = app.next_order_id()
            _ledger_mark(ledger, send_key, state="RESERVED", order_id=int(oid), simulate=False)
            _save_ledger_atomic(ledger_path, ledger)

            _append_exec(
                exec_path,
                {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "order_id": int(oid),
                    "status": "SUBMITTED",
                    "simulate": False,
                    "exec_mode": exec_mode,
                },
            )

            # tie callbacks to this send_key
            app.register_send_key(int(oid), send_key)

            app.place_order(oid, contract, order)
            last_oid_used = int(oid)

            # auto-reset override after first successful submit (real)
            if (not override_autoreset_done) and override_cfg.enabled and override_cfg.autoreset:
                did = _try_autoreset_stage5_override(
                    control_state_path,
                    reason=f"autoreset_after_successful_submit run_id={run_id} send_key={send_key}",
                )
                override_autoreset_done = True
                _append_exec(
                    exec_path,
                    {
                        "kind": "EXEC_EVENT",
                        "ts": _utc_now_z(),
                        "run_id": run_id,
                        "send_key": f"{run_id}:OVERRIDE_AUTORESET",
                        "status": "OVERRIDE_AUTORESET",
                        "ok": bool(did),
                        "why": "autoreset_after_successful_submit",
                    },
                )

            # Wait for initial status (best effort)
            st: Optional[str] = None
            for _ in range(10):  # up to ~2.5s
                st = app.status_for(oid)
                if st:
                    break
                time.sleep(0.25)

            _append_exec(
                exec_path,
                {
                    "kind": "EXEC_EVENT",
                    "ts": _utc_now_z(),
                    "run_id": run_id,
                    "send_key": send_key,
                    "order_id": int(oid),
                    "status": "ORDER_STATUS",
                    "simulate": False,
                    "exec_mode": exec_mode,
                    "ibkr_order_status": st,
                },
            )

            # Mark as sent in ledger
            _ledger_mark(ledger, send_key, state="SENT_REAL", order_id=int(oid), simulate=False)
            _save_ledger_atomic(ledger_path, ledger)

            append_jsonl(
                sent_path,
                {
                    "kind": "SENT_ORDER",
                    "run_id": run_id,
                    "mode": run_mode,
                    "exec_mode": exec_mode,
                    "idempotency_key": key,
                    "send_key": send_key,
                    "ibkr_order_id": oid,
                    "ibkr_order_status": st,
                    "server_next_valid_id": server_next_valid_id,
                    "cursor_last_before": cursor_last_before,
                    "start_order_id": start_order_id,
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
        "simulate_cfg": bool(simulate_cfg),
        "simulate": False,
        "exec_mode": exec_mode,
        "stop_flag": bool(stop_flag_present),
        "run_id": run_id,
        "mode": run_mode,
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


# -----------------------------
# CLI entrypoint (safe: no side-effects on import)
# -----------------------------
def _resolve_paths_for_run_id(repo_root: Path, run_id: str) -> Tuple[Path, Path, Path]:
    data_dir = repo_root / "args" / "data"
    logs_dir = repo_root / "args" / "logs"

    sendplan_path = data_dir / f"orders_sendplan_{run_id}.jsonl"

    # Primary expected name:
    run_report_path = logs_dir / f"run_report_{run_id}_paper.json"
    if not run_report_path.exists():
        # Fallback: any run_report_{run_id}_*.json (if not paper suffix)
        matches = sorted(logs_dir.glob(f"run_report_{run_id}_*.json"))
        if matches:
            run_report_path = matches[-1]

    control_state_path = data_dir / "control_state.json"
    return sendplan_path, run_report_path, control_state_path


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="ARGS IBKR sender (guarded real/sim)")
    p.add_argument("--run-id", required=True)
    args = p.parse_args(argv)

    sendplan_path, run_report_path, control_state_path = _resolve_paths_for_run_id(REPO_ROOT, args.run_id)

    ts = _utc_now_z()
    try:
        result = real_sender(
            sendplan_path=sendplan_path,
            run_report_path=run_report_path,
            control_state_path=control_state_path,
        )
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": True,
            "exit_code": 0,
            "result": result,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except Exception as e:
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"{type(e).__name__}: {e}",
        }
        print(json.dumps(out, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
