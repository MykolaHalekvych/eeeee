# args/wa/wa_ibkr_executor_v0.py
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from args.ibkr.order_sanitize_v0 import sanitize_order_v0

from args.control.execution_mode_v0 import (
    decorate_ibkr_error_details,
    get_execution_mode,
    is_action_allowed,
    is_stop_flag_present,
)

from args.wa.order_ledger_v0 import OrderLedgerV0, compute_idempotency_key_from_plan
from args.wa.reconcile_open_orders_v0 import (
    choose_latest_snapshot,
    load_open_orders_snapshot,
    match_open_orders,
    snapshot_age_seconds,
    snapshot_validity,
)

SCHEMA = "wa_ibkr_executor_v0"
SOURCE = "IBKR_EXECUTOR_V0"

# Supported actionable plans (from orders_sendplan_*.jsonl)
ACTIONABLE_PLAN_KINDS = {
    "PLAN_IBKR_PLACE_ORDER",
    "IBKR_PLACE_ORDER",
    "PLACE_ORDER_IBKR",
}

# IBKR error classification (kept local to keep behavior deterministic)
# INFO codes are not failures (often connectivity/status messages).
_IBKR_INFO_CODES = {2104, 2106, 2158, 1101, 1102}
# WARNING codes should not be treated as rejections (e.g., 399 "will not be placed until ...").
_IBKR_WARNING_CODES = {399, 2103, 2105, 2157, 2107, 2108}


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _classify_ibkr_error(code: int) -> str:
    """
    Returns: INFO / WARNING / ERROR.
    """
    if int(code) in _IBKR_INFO_CODES:
        return "INFO"
    if int(code) in _IBKR_WARNING_CODES:
        return "WARNING"
    return "ERROR"


# ---------------------------
# small utilities
# ---------------------------


def _b01(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _as_int(x: Any) -> Optional[int]:
    if isinstance(x, int):
        return x
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        try:
            return int(s)
        except Exception:
            return None
    return None


def _norm(x: Any) -> str:
    if not isinstance(x, str):
        return ""
    return x.strip().upper()


def _json_compact(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))


def _write_jsonl_line(f, obj: Dict[str, Any]) -> None:
    f.write(_json_compact(obj))
    f.write("\n")


def _iter_jsonl_dicts(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _repo_root() -> Path:
    # .../args/wa/wa_ibkr_executor_v0.py -> parents[2] == repo root
    return Path(__file__).resolve().parents[2]


def _stable_event_id(
    kind: str, *, ledger_key: str, run_id: Any, index: Any, plan_id: Any
) -> str:
    """
    Merge-proof stable id.
    Prefer ledger_key (idempotency key), fallback to (run_id,index,plan_id).
    """
    k = str(kind or "").strip().upper() or "EVENT"
    lk = str(ledger_key or "").strip()
    if lk:
        return f"{k}:{lk}"
    return f"{k}:run={run_id}:idx={index}:plan={plan_id}"


def _stable_status_event_id(
    *, ledger_key: str, status: Any, filled: Any, remaining: Any
) -> str:
    lk = str(ledger_key or "").strip()
    st = str(status or "").strip().upper() or "UNKNOWN"
    fd = "" if filled is None else str(filled)
    rm = "" if remaining is None else str(remaining)
    return f"ORDER_STATUS:{lk}:{st}:{fd}:{rm}"


def _compute_ledger_key(plan: Dict[str, Any], *, prefix: str) -> Tuple[str, str]:
    """
    Always return a non-empty ledger_key if we can.
    Primary: compute_idempotency_key_from_plan().
    Fallback: prefix + (run_id,index,plan_id/payload_id).
    """
    try:
        lk, fp = compute_idempotency_key_from_plan(plan, prefix=prefix)
        lk = str(lk or "").strip()
        fp = str(fp or "").strip()
        if lk:
            return lk, fp
    except Exception:
        pass

    rid = plan.get("run_id")
    idx = plan.get("index")
    pid = plan.get("plan_id") or plan.get("payload_id") or ""
    lk2 = f"{prefix}:run={rid}:idx={idx}:plan={pid}"
    return lk2, ""


def _extract_intent_kind(plan: Dict[str, Any]) -> str:
    v = plan.get("intent_kind")
    if isinstance(v, str) and v.strip():
        return v
    it = plan.get("intent")
    if isinstance(it, dict):
        k = it.get("kind") or it.get("intent_kind") or it.get("type")
        if isinstance(k, str) and k.strip():
            return k
    # fallback: sometimes stored as "intentType"
    v2 = plan.get("intentType") or plan.get("intent_type")
    if isinstance(v2, str) and v2.strip():
        return v2
    return ""


def _pick_fields(d: Dict[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in keys:
        if k in d and d.get(k) is not None:
            out[k] = d.get(k)
    return out


# ---------------------------
# order dict sanitization (defense-in-depth)
# ---------------------------

# Order attributes known to be rejected on some accounts if present at all.
_FORBIDDEN_ORDER_KEYS_REMOVE = {
    "nbboPriceCap",
    "NbboPriceCap",
}

# Order attributes we force to False (even if present in plan).
_FORBIDDEN_ORDER_KEYS_FORCE_FALSE = {
    "eTradeOnly",
    "EtradeOnly",
    "etradeOnly",
    "firmQuoteOnly",
    "FirmQuoteOnly",
    "firmquoteOnly",
}


def sanitize_order_dict_v0(
    order_dict: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Sanitizes a plain dict that will later be converted into ibapi.order.Order.
    - force eTradeOnly/firmQuoteOnly variants to False
    - remove nbboPriceCap variants
    Returns (sanitized_dict, applied_changes_summary)
    """
    out = dict(order_dict or {})
    applied: Dict[str, Any] = {}

    # Remove forbidden keys entirely
    for k in list(out.keys()):
        if k in _FORBIDDEN_ORDER_KEYS_REMOVE:
            out.pop(k, None)
            applied[f"{k}_removed"] = True

    # Force False for problematic booleans
    for k in _FORBIDDEN_ORDER_KEYS_FORCE_FALSE:
        if k in out and out.get(k) is not False:
            out[k] = False
            applied[k] = False

    return out, applied


# ---------------------------
# plan extractors
# ---------------------------


def _default_contract_path(repo_root: Path, instrument: str) -> Optional[Path]:
    inst = _norm(instrument)
    data_dir = repo_root / "args" / "data"

    mapping = {
        "HG": data_dir / "ibkr_hg_contract_v1.json",
        "MHG": data_dir / "ibkr_mhg_contract_v1.json",
        "QC": data_dir / "ibkr_qc_contract_v1.json",
    }
    p = mapping.get(inst)
    return p if (p and p.exists()) else None


def _extract_contract_dict(
    plan: Dict[str, Any], repo_root: Path
) -> Optional[Dict[str, Any]]:
    for k in ("contract", "ibkr_contract"):
        v = plan.get(k)
        if isinstance(v, dict) and v:
            return v

    cp = plan.get("contract_path")
    if isinstance(cp, str) and cp.strip():
        p = Path(cp.strip())
        if not p.is_absolute():
            p = repo_root / p
        if p.exists():
            obj = _read_json(p)
            return obj if isinstance(obj, dict) else None

    inst = str(plan.get("instrument") or "")
    dp = _default_contract_path(repo_root, inst)
    if dp and dp.exists():
        obj = _read_json(dp)
        return obj if isinstance(obj, dict) else None

    return None


def _extract_order_dict(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for k in ("order", "ibkr_order"):
        v = plan.get(k)
        if isinstance(v, dict) and v:
            return v

    payload = plan.get("payload")
    if isinstance(payload, dict):
        for k in ("order", "ibkr_order"):
            v = payload.get(k)
            if isinstance(v, dict) and v:
                return v

    return None


def _safe_setattr(obj: Any, k: str, v: Any) -> None:
    if not isinstance(k, str) or not k:
        return
    try:
        setattr(obj, k, v)
    except Exception:
        pass


def _dict_to_ib_contract(contract_dict: Dict[str, Any]):
    from ibapi.contract import Contract  # type: ignore

    c = Contract()
    for k, v in contract_dict.items():
        _safe_setattr(c, k, v)
    return c


# Keys we will NOT set from dict directly (defense-in-depth); sanitize_order_v0 is the last barrier.
_SKIP_ORDER_ATTRS_ON_BUILD = _FORBIDDEN_ORDER_KEYS_REMOVE.union(
    _FORBIDDEN_ORDER_KEYS_FORCE_FALSE
)


def _dict_to_ib_order(order_dict: Dict[str, Any]):
    """
    Build ibapi.order.Order from dict.
    - Avoid setting known-forbidden attrs.
    - Force eTradeOnly/firmQuoteOnly False if attribute exists.
    NOTE: sanitize_order_v0(order_obj) must still be applied before placeOrder.
    """
    from ibapi.order import Order  # type: ignore

    o = Order()
    for k, v in order_dict.items():
        if k in _SKIP_ORDER_ATTRS_ON_BUILD:
            continue
        _safe_setattr(o, k, v)

    # Critical: disable unsupported IBKR attrs on some accounts/instruments
    _safe_setattr(o, "eTradeOnly", False)
    _safe_setattr(o, "firmQuoteOnly", False)

    # Safety: avoid accidental GTD/expiry if some defaults exist
    if hasattr(o, "goodTillDate") and not getattr(o, "goodTillDate", ""):
        _safe_setattr(o, "goodTillDate", "")

    return o


# ---------------------------
# IBKR connection (minimal direct fallback)
# ---------------------------


@dataclass(frozen=True)
class IbkrConn:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11
    timeout_s: float = 15.0


class _IBSimpleApp:
    """
    Minimal wrapper around IB API.
    Used only as fallback if args.ibkr.ibkr_sender_real_v1 does not expose a compatible callable.

    Standard guarantees (P1.3B):
      - sanitize_order_v0() applied before placeOrder
      - eTradeOnly/firmQuoteOnly forced off
      - nbboPriceCap removed from __dict__ if present
      - error code 399 treated as WARNING (not a reject)
      - info codes 2104/2106/2158 captured as sys messages (not reject)
    """

    def __init__(self, conn: IbkrConn):
        from ibapi.client import EClient  # type: ignore
        from ibapi.wrapper import EWrapper  # type: ignore

        class App(EWrapper, EClient):  # type: ignore
            def __init__(self, outer: "_IBSimpleApp"):
                EClient.__init__(self, self)
                self._outer = outer

            def nextValidId(self, orderId: int) -> None:  # noqa: N802
                self._outer._on_next_valid_id(orderId)

            def openOrder(self, orderId, contract, order, orderState) -> None:  # noqa: N802
                self._outer._on_open_order(orderId, contract, order, orderState)

            def orderStatus(  # noqa: N802
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
                self._outer._on_order_status(
                    orderId=orderId,
                    status=status,
                    filled=filled,
                    remaining=remaining,
                    avgFillPrice=avgFillPrice,
                    lastFillPrice=lastFillPrice,
                    whyHeld=whyHeld,
                )

            def error(
                self, reqId, errorCode, errorString, advancedOrderRejectJson=""
            ) -> None:  # type: ignore  # noqa: N802
                self._outer._on_error(
                    reqId, errorCode, errorString, advancedOrderRejectJson
                )

        self._conn = conn
        self._app = App(self)

        self._thread: Optional[threading.Thread] = None
        self._next_id: Optional[int] = None
        self._next_id_ev = threading.Event()

        self._lock = threading.Lock()

        # per-order observations
        self._open_order: Dict[int, Dict[str, Any]] = {}
        self._order_status: Dict[int, Dict[str, Any]] = {}
        self._order_errors: Dict[
            int, Dict[str, Any]
        ] = {}  # fatal errors keyed by orderId
        self._order_warnings: Dict[
            int, List[Dict[str, Any]]
        ] = {}  # warnings keyed by orderId

        # global messages (reqId=-1, connectivity info, etc)
        self._sys_msgs: List[str] = []
        self._global_errors: List[Dict[str, Any]] = []
        self._global_warnings: List[Dict[str, Any]] = []

    def connect(self) -> None:
        self._app.connect(
            self._conn.host, int(self._conn.port), int(self._conn.client_id)
        )
        self._thread = threading.Thread(target=self._app.run, daemon=True)
        self._thread.start()
        if not self._next_id_ev.wait(timeout=self._conn.timeout_s):
            raise RuntimeError("IBKR connect: timeout waiting for nextValidId()")

    def disconnect(self) -> None:
        try:
            self._app.disconnect()
        except Exception:
            pass

    def _on_next_valid_id(self, order_id: int) -> None:
        with self._lock:
            self._next_id = int(order_id)
        self._next_id_ev.set()

    def _on_open_order(
        self, order_id: int, contract: Any, order: Any, order_state: Any
    ) -> None:
        try:
            st = getattr(order_state, "status", None)
        except Exception:
            st = None
        with self._lock:
            self._open_order[int(order_id)] = {
                "event": "openOrder",
                "order_id": int(order_id),
                "order_state_status": st,
            }

    def _on_order_status(
        self,
        *,
        orderId: int,
        status: Any,
        filled: Any,
        remaining: Any,
        avgFillPrice: Any,
        lastFillPrice: Any,
        whyHeld: Any,
    ) -> None:
        with self._lock:
            self._order_status[int(orderId)] = {
                "event": "orderStatus",
                "order_id": int(orderId),
                "status": status,
                "filled": filled,
                "remaining": remaining,
                "avgFillPrice": avgFillPrice,
                "lastFillPrice": lastFillPrice,
                "whyHeld": whyHeld,
            }

    def _on_error(
        self, req_id: Any, error_code: Any, error_str: Any, advanced: Any
    ) -> None:
        # Best-effort normalize
        try:
            code_i = int(error_code)
        except Exception:
            code_i = 0
        msg_s = str(error_str)

        sev = _classify_ibkr_error(code_i)

        # IBKR "info" connectivity lines: keep as sys msgs (string list)
        sys_line = f"reqId={req_id} code={code_i} msg={msg_s}"
        if sev == "INFO":
            with self._lock:
                self._sys_msgs.append(sys_line)
            return

        # Determine order_id if possible
        try:
            oid = int(req_id)
        except Exception:
            oid = -1

        payload: Dict[str, Any] = {
            "event": "error",
            "req_id": req_id,
            "error_code": code_i,
            "error_string": msg_s,
            "severity": sev,
        }
        if isinstance(advanced, str) and advanced.strip():
            payload["advanced_order_reject_json"] = advanced

        # Add operator hints (e.g., error 321 read-only)
        payload = decorate_ibkr_error_details(payload)

        with self._lock:
            if oid <= 0:
                if sev == "WARNING":
                    self._global_warnings.append(payload)
                else:
                    self._global_errors.append(payload)
            else:
                if sev == "WARNING":
                    self._order_warnings.setdefault(int(oid), []).append(payload)
                else:
                    # ERROR: treat as fatal for this order
                    self._order_errors[int(oid)] = payload

    def next_order_id(self) -> int:
        if not self._next_id_ev.is_set():
            if not self._next_id_ev.wait(timeout=self._conn.timeout_s):
                raise RuntimeError("IBKR: no nextValidId available")
        with self._lock:
            assert self._next_id is not None
            oid = int(self._next_id)
            self._next_id += 1
        return oid

    def place_order(
        self, *, order_id: int, contract_obj: Any, order_obj: Any
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Returns (ok, detail).
        ok=False only when we observe a fatal IBKR error for this order_id (not INFO/WARNING),
        or when we hit a hard timeout with no openOrder/orderStatus callbacks.
        """
        oid = int(order_id)

        # Reset per-order observations
        with self._lock:
            self._open_order.pop(oid, None)
            self._order_status.pop(oid, None)
            self._order_errors.pop(oid, None)
            self._order_warnings.pop(oid, None)

        # P1.3B: sanitize order object before submit
        try:
            san = sanitize_order_v0(order_obj)
        except Exception as e:
            # If sanitization fails, we still submit (do not hide), but record the failure
            san = {"sanitize_error": f"{type(e).__name__}: {e}"}

        self._app.placeOrder(oid, contract_obj, order_obj)

        t0 = time.time()
        while (time.time() - t0) < float(self._conn.timeout_s):
            with self._lock:
                global_err_tail = self._global_errors[-6:]
                global_warn_tail = self._global_warnings[-6:]

                fatal = self._order_errors.get(oid)
                if isinstance(fatal, dict):
                    return False, {
                        "event": "order_reject",
                        "order_id": oid,
                        "fatal": dict(fatal),
                        "warnings": list(self._order_warnings.get(oid, [])),
                        "sys_msgs_tail": self._sys_msgs[-12:],
                        "global_errors_tail": list(global_err_tail),
                        "global_warnings_tail": list(global_warn_tail),
                        "sanitized": san,
                    }

                ack = self._open_order.get(oid)
                if isinstance(ack, dict):
                    st = self._order_status.get(oid)
                    out = {
                        **dict(ack),
                        "order_status": dict(st) if isinstance(st, dict) else None,
                        "warnings": list(self._order_warnings.get(oid, [])),
                        "sys_msgs_tail": self._sys_msgs[-12:],
                        "global_errors_tail": list(global_err_tail),
                        "global_warnings_tail": list(global_warn_tail),
                        "sanitized": san,
                    }
                    return True, out

            time.sleep(0.05)

        # Timeout: return best-effort if we saw orderStatus
        with self._lock:
            st = self._order_status.get(oid)
            warns = list(self._order_warnings.get(oid, []))
            sys_tail = self._sys_msgs[-12:]
            fatal = self._order_errors.get(oid)
            global_err_tail = list(self._global_errors[-6:])
            global_warn_tail = list(self._global_warnings[-6:])

        if isinstance(fatal, dict):
            return False, {
                "event": "timeout_with_fatal",
                "order_id": oid,
                "fatal": dict(fatal),
                "warnings": warns,
                "sys_msgs_tail": sys_tail,
                "global_errors_tail": global_err_tail,
                "global_warnings_tail": global_warn_tail,
                "sanitized": san,
            }

        if isinstance(st, dict):
            return True, {
                "event": "timeout_but_status_seen",
                "order_id": oid,
                "order_status": dict(st),
                "warnings": warns,
                "sys_msgs_tail": sys_tail,
                "global_errors_tail": global_err_tail,
                "global_warnings_tail": global_warn_tail,
                "sanitized": san,
            }

        return False, {
            "event": "timeout",
            "order_id": oid,
            "error_string": "Timeout waiting for openOrder/orderStatus/error callback",
            "warnings": warns,
            "sys_msgs_tail": sys_tail,
            "global_errors_tail": global_err_tail,
            "global_warnings_tail": global_warn_tail,
            "sanitized": san,
        }


# ---------------------------
# sender_real bridge (best-effort)
# ---------------------------


def _try_submit_via_sender_real(
    *,
    contract_dict: Dict[str, Any],
    order_dict: Dict[str, Any],
    conn: IbkrConn,
) -> Optional[Tuple[bool, Dict[str, Any]]]:
    """
    Attempts to submit via args.ibkr.ibkr_sender_real_v1 if it exposes a compatible callable.
    Returns None if no compatible callable found.
    """
    try:
        import args.ibkr.ibkr_sender_real_v1 as sender  # type: ignore
    except Exception:
        return None

    candidates = [
        "submit_order_v0",
        "submit_order",
        "send_order",
        "place_order",
        "execute_order",
    ]

    def _coerce_result(res: Any) -> Optional[Tuple[bool, Dict[str, Any]]]:
        if isinstance(res, tuple) and len(res) == 2:
            ok = bool(res[0])
            detail = res[1] if isinstance(res[1], dict) else {"detail": res[1]}
            return ok, detail
        if isinstance(res, dict):
            ok = bool(res.get("ok", True))
            return ok, res
        if isinstance(res, bool):
            return res, {"ok": res}
        return None

    for name in candidates:
        fn = getattr(sender, name, None)
        if not callable(fn):
            continue

        attempts = [
            lambda: fn(
                contract_dict=contract_dict,
                order_dict=order_dict,
                host=conn.host,
                port=conn.port,
                client_id=conn.client_id,
                timeout_s=conn.timeout_s,
            ),
            lambda: fn(
                contract=contract_dict,
                order=order_dict,
                host=conn.host,
                port=conn.port,
                client_id=conn.client_id,
                timeout_s=conn.timeout_s,
            ),
            lambda: fn(contract_dict, order_dict, conn.host, conn.port, conn.client_id),
            lambda: fn(contract_dict, order_dict),
        ]

        for a in attempts:
            try:
                res = a()
                out = _coerce_result(res)
                if out is not None:
                    return out
            except TypeError:
                continue
            except Exception as e:
                return False, {
                    "event": "sender_real_exception",
                    "error": repr(e),
                    "callable": name,
                }

    return None


# ---------------------------
# plan/events helpers
# ---------------------------


def _is_actionable_plan(plan: Dict[str, Any]) -> bool:
    pk = _norm(plan.get("plan_kind"))
    if pk in ACTIONABLE_PLAN_KINDS:
        return True
    if pk.startswith("PLAN_IBKR_"):
        return True
    return False


def _build_event_base(plan: Dict[str, Any], *, exec_id: str) -> Dict[str, Any]:
    """
    Standard event envelope for JSONL output.
    """
    evt: Dict[str, Any] = {
        "schema": SCHEMA,
        "ts_utc": _utc_now_iso(),  # event emission time
        "ts": plan.get("ts"),  # plan time (keep for backward-compat)
        "run_id": plan.get("run_id"),
        "index": plan.get("index"),
        "instrument": plan.get("instrument"),
        "timeframe": plan.get("timeframe"),
        "env": plan.get("env"),
        "exec_id": exec_id,
        "source": SOURCE,
        "plan_id": plan.get("plan_id"),
        "plan_kind": plan.get("plan_kind"),
        "payload_id": plan.get("payload_id"),
        "payload_kind": plan.get("payload_kind"),
        "execute": _b01(plan.get("execute", False)),
    }
    if "payload_execute" in plan:
        evt["payload_execute"] = _b01(plan.get("payload_execute"))
    if "gate_reason" in plan and isinstance(plan.get("gate_reason"), str):
        evt["gate_reason"] = plan.get("gate_reason")
    if "ma_decision" in plan and isinstance(plan.get("ma_decision"), str):
        evt["ma_decision"] = plan.get("ma_decision")
    if "decision" in plan and isinstance(plan.get("decision"), str):
        evt["decision"] = plan.get("decision")
    return evt


def _finalize_ledger_safe(
    ledger: Optional[OrderLedgerV0],
    *,
    ledger_key: str,
    run_id: str,
    status: str,
    result: Dict[str, Any],
) -> None:
    if ledger is None or not ledger_key:
        return
    try:
        ledger.finalize(ledger_key, run_id=run_id, status=status, result=result)
    except Exception:
        return


# ---------------------------
# main
# ---------------------------


def main() -> int:
    ap = argparse.ArgumentParser(prog=SCHEMA)

    ap.add_argument(
        "--sendplan",
        default="",
        help="Path to orders_sendplan_<run_id>.jsonl (optional if --run-id is given)",
    )
    ap.add_argument(
        "--run-id",
        default="",
        help="If set, will use args/data/orders_sendplan_<run_id>.jsonl",
    )

    ap.add_argument(
        "--execute",
        default="0",
        help="0/1. When 1, executor may attempt IBKR placeOrder for actionable plans (still guarded by execution_mode.json + stop.flag)",
    )
    ap.add_argument(
        "--max-orders", type=int, default=0, help="Safety limit: 0 means no limit"
    )

    ap.add_argument("--ledger-path", default="args/data/order_ledger_v0.jsonl")
    ap.add_argument(
        "--ledger-enabled",
        default="1",
        help="0/1. Enabled only when execute=1 (SAFE default 1)",
    )
    ap.add_argument(
        "--ledger-allow-retry",
        default="0",
        help="0/1. SAFE default 0 (no retry after REJECT/ERROR)",
    )

    ap.add_argument(
        "--reconcile",
        default="0",
        help="0/1. Preflight open-orders snapshot blocks submit if match found.",
    )
    ap.add_argument(
        "--reconcile-empty-ok",
        default="1",
        help="0/1. When 1, snapshot_empty does NOT block reconcile.",
    )
    ap.add_argument(
        "--snapshot-path",
        default="",
        help="Optional snapshot jsonl path. Default: latest args/data/ibkr_open_orders_*.jsonl",
    )
    ap.add_argument(
        "--snapshot-max-age-s",
        type=float,
        default=600.0,
        help="Max allowed age of snapshot in seconds (default 600). 0 disables age check.",
    )

    ap.add_argument(
        "--out",
        default="",
        help="Optional output JSONL for exec events. Default: args/data/orders_exec_events_<run_id>.jsonl",
    )

    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=11)
    ap.add_argument("--timeout-s", type=float, default=15.0)

    ap.add_argument(
        "--prefer-sender-real",
        default="1",
        help="0/1. Prefer args.ibkr.ibkr_sender_real_v1 if it provides a compatible submit function (default 1).",
    )

    args = ap.parse_args()
    repo_root = _repo_root()

    ts_start = _utc_now_iso()

    run_id_arg = str(args.run_id or "").strip()
    sendplan_arg = str(args.sendplan or "").strip()

    if not sendplan_arg and not run_id_arg:
        raise SystemExit("Need --sendplan <path> or --run-id <RUN_ID>")

    if not sendplan_arg and run_id_arg:
        sendplan_path = (
            repo_root / "args" / "data" / f"orders_sendplan_{run_id_arg}.jsonl"
        )
    else:
        sendplan_path = Path(sendplan_arg)
        if not sendplan_path.is_absolute():
            sendplan_path = repo_root / sendplan_path

    if not sendplan_path.exists():
        raise FileNotFoundError(f"sendplan not found: {sendplan_path}")

    cli_execute = _b01(args.execute)
    prefer_sender_real = _b01(args.prefer_sender_real)

    conn = IbkrConn(
        host=str(args.host),
        port=int(args.port),
        client_id=int(args.client_id),
        timeout_s=float(args.timeout_s),
    )
    exec_id = uuid.uuid4().hex[:12]

    # Arming (file-based)
    exec_mode = get_execution_mode(repo_root)

    # Determine run_id from first plan (best-effort)
    first_run_id: Optional[str] = run_id_arg or None
    if first_run_id is None:
        for plan0 in _iter_jsonl_dicts(sendplan_path):
            rid = plan0.get("run_id")
            if isinstance(rid, str) and rid.strip():
                first_run_id = rid.strip()
                break
    run_id_for_ledger = str(first_run_id or "unknown")

    # Resolve output path
    if str(args.out or "").strip():
        out_path = Path(str(args.out).strip())
        if not out_path.is_absolute():
            out_path = repo_root / out_path
    else:
        data_dir = repo_root / "args" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        suffix = first_run_id or "unknown"
        out_path = data_dir / f"orders_exec_events_{suffix}.jsonl"

    # Ledger init (only when cli_execute=1)
    ledger: Optional[OrderLedgerV0] = None
    ledger_path_resolved: str = ""
    ledger_enabled = _b01(args.ledger_enabled)
    ledger_allow_retry = _b01(args.ledger_allow_retry)

    if cli_execute and ledger_enabled:
        lp = Path(str(args.ledger_path))
        if not lp.is_absolute():
            lp = repo_root / lp
        ledger_path_resolved = str(lp.resolve())
        ledger = OrderLedgerV0(lp)

    # Reconcile preflight init
    reconcile_requested = _b01(args.reconcile)
    reconcile_enabled = bool(reconcile_requested and cli_execute)
    reconcile_empty_ok = _b01(args.reconcile_empty_ok)

    snapshot_idx = None
    snapshot_used = ""
    snapshot_age_s: Optional[float] = None
    snapshot_parse_errors: Optional[int] = None
    reconcile_block_all_reason = ""
    snapshot_max_age_s = float(args.snapshot_max_age_s)

    if reconcile_enabled:
        sp_arg = str(args.snapshot_path or "").strip()
        if sp_arg:
            sp = Path(sp_arg)
            if not sp.is_absolute():
                sp = repo_root / sp
        else:
            sp = choose_latest_snapshot(repo_root)

        if sp is None:
            reconcile_block_all_reason = "snapshot_missing"
        else:
            snapshot_used = str(sp)

            if not sp.exists():
                reconcile_block_all_reason = "snapshot_missing"
            else:
                try:
                    snapshot_idx = load_open_orders_snapshot(sp)
                except Exception as e:
                    reconcile_block_all_reason = (
                        f"snapshot_exception:{type(e).__name__}"
                    )
                    snapshot_idx = None
                else:
                    snapshot_parse_errors = getattr(snapshot_idx, "parse_errors", None)
                    snapshot_age_s = snapshot_age_seconds(snapshot_idx)

                    ok_snap, snap_reason = snapshot_validity(snapshot_idx)

                    if (
                        (not ok_snap)
                        and snap_reason == "snapshot_empty"
                        and reconcile_empty_ok
                    ):
                        ok_snap = True
                        snap_reason = ""

                    if not ok_snap:
                        reconcile_block_all_reason = snap_reason
                        snapshot_idx = None
                    else:
                        if snapshot_max_age_s > 0:
                            if snapshot_age_s is None:
                                reconcile_block_all_reason = "snapshot_age_unknown"
                                snapshot_idx = None
                            elif snapshot_age_s > snapshot_max_age_s:
                                reconcile_block_all_reason = "snapshot_stale"
                                snapshot_idx = None

    # IB connection (lazy)
    ib_app: Optional[_IBSimpleApp] = None
    ib_connected = False

    def _ensure_connected() -> None:
        nonlocal ib_app, ib_connected
        if ib_connected:
            return
        ib_app = _IBSimpleApp(conn)
        ib_app.connect()
        ib_connected = True

    def _emit_event(
        fout, evt: Dict[str, Any], *, event_id_override: Optional[str] = None
    ) -> None:
        lk = str(evt.get("ledger_key") or "").strip()
        if event_id_override:
            evt["event_id"] = str(event_id_override)
        else:
            evt["event_id"] = _stable_event_id(
                str(evt.get("kind") or ""),
                ledger_key=lk,
                run_id=evt.get("run_id"),
                index=evt.get("index"),
                plan_id=evt.get("plan_id"),
            )
        _write_jsonl_line(fout, evt)

    def _emit_status_event_if_present(
        fout,
        *,
        plan: Dict[str, Any],
        exec_id: str,
        ledger_key: str,
        ibkr_meta: Dict[str, Any],
        detail: Any,
    ) -> int:
        if not ledger_key:
            return 0
        if not isinstance(detail, dict):
            return 0
        st = detail.get("order_status")
        if not isinstance(st, dict) or not st:
            return 0

        sev = _build_event_base(plan, exec_id=exec_id)
        sev["kind"] = "ORDER_STATUS"
        sev["ledger_key"] = ledger_key
        sev["ibkr"] = dict(ibkr_meta)
        sev["details"] = dict(st)

        eid = _stable_status_event_id(
            ledger_key=ledger_key,
            status=st.get("status"),
            filled=st.get("filled"),
            remaining=st.get("remaining"),
        )
        _emit_event(fout, sev, event_id_override=eid)
        return 1

    # ---------------------------
    # counters
    # ---------------------------
    plans_seen = 0
    plans_actionable = 0

    orders_attempted = 0
    orders_skipped = 0
    orders_skipped_guard = 0
    orders_skipped_duplicate = 0
    orders_skipped_reconcile = 0

    acks = 0
    rejects = 0
    parse_errors = 0
    status_events = 0

    # ---------------------------
    # execution
    # ---------------------------
    with (
        out_path.open("w", encoding="utf-8") as fout,
        sendplan_path.open("r", encoding="utf-8-sig", errors="replace") as fplan,
    ):
        # BOOT event
        boot_evt = {
            "schema": SCHEMA,
            "ts_utc": _utc_now_iso(),
            "kind": "BOOT",
            "exec_id": exec_id,
            "source": SOURCE,
            "run_id": run_id_for_ledger,
            "details": {
                "cli_execute": bool(cli_execute),
                "exec_mode": exec_mode,
                "stop_flag": bool(is_stop_flag_present(repo_root)),
                "prefer_sender_real": bool(prefer_sender_real),
                "conn": {
                    "host": conn.host,
                    "port": conn.port,
                    "client_id": conn.client_id,
                    "timeout_s": conn.timeout_s,
                },
                "ledger": {
                    "enabled": bool(ledger is not None),
                    "path": ledger_path_resolved,
                    "allow_retry": bool(ledger_allow_retry),
                },
                "reconcile": {
                    "enabled": bool(reconcile_enabled),
                    "requested": bool(reconcile_requested),
                    "empty_ok": bool(reconcile_empty_ok),
                    "snapshot_path": snapshot_used,
                    "snapshot_age_s": snapshot_age_s,
                    "snapshot_max_age_s": snapshot_max_age_s,
                    "snapshot_parse_errors": snapshot_parse_errors,
                    "block_reason": reconcile_block_all_reason,
                },
            },
        }
        _emit_event(fout, boot_evt, event_id_override=f"BOOT:{exec_id}")

        for raw_line in fplan:
            s = raw_line.strip()
            if not s:
                continue
            try:
                plan = json.loads(s)
            except Exception:
                parse_errors += 1
                continue
            if not isinstance(plan, dict):
                continue

            plans_seen += 1

            if not _is_actionable_plan(plan):
                continue
            plans_actionable += 1

            # Always compute ledger key for traceability (even when skipping)
            ledger_key, plan_fp = _compute_ledger_key(plan, prefix="ibkr_place_order")

            # Plan-level execute gate (separate from arming)
            plan_execute = _b01(plan.get("execute", False))
            if (not cli_execute) or (not plan_execute):
                orders_skipped += 1
                evt = _build_event_base(plan, exec_id=exec_id)
                evt.update(
                    {
                        "kind": "EXEC_SKIPPED",
                        "reason": "CLI_EXECUTE_OFF"
                        if (not cli_execute)
                        else "PLAN_EXECUTE_FALSE",
                        "ledger_key": ledger_key,
                        "details": {
                            "cli_execute": bool(cli_execute),
                            "plan_execute": bool(plan_execute),
                            "execution_mode": exec_mode,
                        },
                    }
                )
                _emit_event(fout, evt)
                continue

            if (
                args.max_orders
                and int(args.max_orders) > 0
                and orders_attempted >= int(args.max_orders)
            ):
                orders_skipped += 1
                evt = _build_event_base(plan, exec_id=exec_id)
                evt.update(
                    {
                        "kind": "EXEC_SKIPPED",
                        "reason": "MAX_ORDERS_LIMIT",
                        "ledger_key": ledger_key,
                        "details": {
                            "max_orders": int(args.max_orders),
                            "orders_attempted": int(orders_attempted),
                        },
                    }
                )
                _emit_event(fout, evt)
                continue

            # ---------------------------
            # EXECUTION GUARD (arming + MA/gates)
            # ---------------------------
            stop_present = is_stop_flag_present(repo_root)
            ma_decision = str(plan.get("ma_decision") or plan.get("decision") or "")
            gate_reason = str(plan.get("gate_reason") or plan.get("reason") or "")
            intent_kind = _extract_intent_kind(plan)
            plan_kind = str(plan.get("plan_kind") or "")
            payload_kind = str(plan.get("payload_kind") or "")

            allowed, why = is_action_allowed(
                mode=exec_mode,
                stop_flag=stop_present,
                ma_decision=ma_decision,
                gate_reason=gate_reason,
                intent_kind=intent_kind,
                plan_kind=plan_kind,
                payload_kind=payload_kind,
                sendplan_item=plan,
            )

            if not allowed:
                evt = _build_event_base(plan, exec_id=exec_id)
                evt.update(
                    {
                        "kind": "EXEC_SKIPPED",
                        "reason": why,
                        "ledger_key": ledger_key,
                        "details": {
                            "mode": exec_mode,
                            "stop_flag": bool(stop_present),
                            "ma_decision": ma_decision,
                            "gate_reason": gate_reason,
                            "intent_kind": intent_kind,
                        },
                    }
                )
                _emit_event(fout, evt)
                orders_skipped_guard += 1
                continue
            # ---------------------------

            contract_dict = _extract_contract_dict(plan, repo_root)
            order_dict0 = _extract_order_dict(plan)

            if not isinstance(contract_dict, dict) or not contract_dict:
                evt = _build_event_base(plan, exec_id=exec_id)
                evt.update(
                    {
                        "kind": "ORDER_REJECT",
                        "reason": "missing_contract",
                        "details": {
                            "error": "No contract info in plan and no default contract mapping found."
                        },
                        "ledger_key": ledger_key,
                    }
                )
                _emit_event(fout, evt)
                rejects += 1
                _finalize_ledger_safe(
                    ledger,
                    ledger_key=ledger_key,
                    run_id=run_id_for_ledger,
                    status="REJECT",
                    result={"reject": {"reason": "missing_contract"}},
                )
                evt_done = _build_event_base(plan, exec_id=exec_id)
                evt_done.update(
                    {
                        "kind": "ORDER_DONE",
                        "ledger_key": ledger_key,
                        "details": {"ok": False, "reason": "missing_contract"},
                    }
                )
                _emit_event(fout, evt_done)
                continue

            if not isinstance(order_dict0, dict) or not order_dict0:
                evt = _build_event_base(plan, exec_id=exec_id)
                evt.update(
                    {
                        "kind": "ORDER_REJECT",
                        "reason": "missing_order",
                        "details": {
                            "error": "No order info in plan (expected plan.order/plan.ibkr_order/payload.*)"
                        },
                        "ledger_key": ledger_key,
                    }
                )
                _emit_event(fout, evt)
                rejects += 1
                _finalize_ledger_safe(
                    ledger,
                    ledger_key=ledger_key,
                    run_id=run_id_for_ledger,
                    status="REJECT",
                    result={"reject": {"reason": "missing_order"}},
                )
                evt_done = _build_event_base(plan, exec_id=exec_id)
                evt_done.update(
                    {
                        "kind": "ORDER_DONE",
                        "ledger_key": ledger_key,
                        "details": {"ok": False, "reason": "missing_order"},
                    }
                )
                _emit_event(fout, evt_done)
                continue

            # Stage 5E.2: reconcile gate BEFORE ledger.reserve
            if reconcile_enabled:
                if reconcile_block_all_reason:
                    evt = _build_event_base(plan, exec_id=exec_id)
                    evt.update(
                        {
                            "kind": "ORDER_SKIP_RECONCILE",
                            "reason": reconcile_block_all_reason,
                            "ledger_key": ledger_key,
                            "details": {
                                "snapshot_path": snapshot_used,
                                "snapshot_age_s": snapshot_age_s,
                                "snapshot_max_age_s": snapshot_max_age_s,
                                "snapshot_parse_errors": snapshot_parse_errors,
                                "empty_ok": bool(reconcile_empty_ok),
                            },
                        }
                    )
                    _emit_event(fout, evt)
                    orders_skipped_reconcile += 1
                    # note: no ledger finalize since no reserve and no submit
                    continue

                if snapshot_idx is not None:
                    matched, match_by, matches = match_open_orders(
                        snapshot_idx, contract_dict
                    )
                    if matched:
                        evt = _build_event_base(plan, exec_id=exec_id)
                        evt.update(
                            {
                                "kind": "ORDER_SKIP_RECONCILE",
                                "reason": "open_order_exists",
                                "ledger_key": ledger_key,
                                "details": {
                                    "snapshot_path": snapshot_used,
                                    "snapshot_age_s": snapshot_age_s,
                                    "snapshot_max_age_s": snapshot_max_age_s,
                                    "snapshot_parse_errors": snapshot_parse_errors,
                                    "empty_ok": bool(reconcile_empty_ok),
                                    "match_by": match_by,
                                    "matches": matches,
                                },
                            }
                        )
                        _emit_event(fout, evt)
                        orders_skipped_reconcile += 1
                        continue

            # Ledger dedupe: reserve BEFORE submit (only when ledger enabled)
            if ledger is not None and ledger_key:
                try:
                    reserved = ledger.reserve(
                        ledger_key,
                        run_id=run_id_for_ledger,
                        plan_meta={
                            "plan_kind": plan.get("plan_kind"),
                            "plan_fp": plan_fp,
                            "plan_id": plan.get("plan_id"),
                            "payload_id": plan.get("payload_id"),
                            "index": plan.get("index"),
                        },
                        allow_retry=ledger_allow_retry,
                    )
                except Exception as e:
                    evt = _build_event_base(plan, exec_id=exec_id)
                    evt.update(
                        {
                            "kind": "ORDER_REJECT",
                            "reason": "ledger_error",
                            "ledger_key": ledger_key,
                            "details": {"error": repr(e)},
                        }
                    )
                    _emit_event(fout, evt)
                    rejects += 1
                    evt_done = _build_event_base(plan, exec_id=exec_id)
                    evt_done.update(
                        {
                            "kind": "ORDER_DONE",
                            "ledger_key": ledger_key,
                            "details": {"ok": False, "reason": "ledger_error"},
                        }
                    )
                    _emit_event(fout, evt_done)
                    continue

                if not reserved:
                    evt = _build_event_base(plan, exec_id=exec_id)
                    evt.update(
                        {
                            "kind": "ORDER_SKIP_DUPLICATE",
                            "ledger_key": ledger_key,
                            "details": {"plan_fp": plan_fp},
                        }
                    )
                    _emit_event(fout, evt)
                    orders_skipped_duplicate += 1
                    continue

            # Order dict sanitization (defense-in-depth) BEFORE submission
            order_dict, order_dict_san_applied = sanitize_order_dict_v0(order_dict0)

            # Emit ORDER_SUBMIT right before the actual submit attempt (Stage 5 trace)
            evt_submit = _build_event_base(plan, exec_id=exec_id)
            evt_submit.update(
                {
                    "kind": "ORDER_SUBMIT",
                    "ledger_key": ledger_key,
                    "details": {
                        "execution_mode": exec_mode,
                        "stop_flag": bool(stop_present),
                        "prefer_sender_real": bool(prefer_sender_real),
                        "conn": {
                            "host": conn.host,
                            "port": conn.port,
                            "client_id": conn.client_id,
                        },
                        "contract": _pick_fields(
                            contract_dict,
                            [
                                "conId",
                                "symbol",
                                "localSymbol",
                                "secType",
                                "exchange",
                                "primaryExch",
                                "currency",
                                "multiplier",
                                "lastTradeDateOrContractMonth",
                            ],
                        ),
                        "order": _pick_fields(
                            order_dict0,
                            [
                                "action",
                                "totalQuantity",
                                "orderType",
                                "lmtPrice",
                                "auxPrice",
                                "tif",
                                "goodTillDate",
                                "outsideRth",
                            ],
                        ),
                        "order_sanitize_applied": order_dict_san_applied,
                    },
                }
            )
            _emit_event(fout, evt_submit)

            orders_attempted += 1

            # 1) Prefer sender_real_v1 if it exposes a compatible callable
            sender_res: Optional[Tuple[bool, Dict[str, Any]]] = None
            if prefer_sender_real:
                sender_res = _try_submit_via_sender_real(
                    contract_dict=contract_dict, order_dict=order_dict, conn=conn
                )

            if sender_res is not None:
                ok, detail = sender_res
                if isinstance(detail, dict):
                    detail = decorate_ibkr_error_details(detail)

                evt = _build_event_base(plan, exec_id=exec_id)
                evt["ibkr"] = {"via": "ibkr_sender_real_v1"}
                evt["ledger_key"] = ledger_key

                # enrich with our order dict sanitization summary
                if isinstance(detail, dict) and order_dict_san_applied:
                    detail = dict(detail)
                    detail.setdefault("order_sanitize_applied", order_dict_san_applied)

                if ok:
                    evt["kind"] = "ORDER_ACK"
                    evt["details"] = detail
                    acks += 1
                    _finalize_ledger_safe(
                        ledger,
                        ledger_key=ledger_key,
                        run_id=run_id_for_ledger,
                        status="ACK",
                        result={"ack": detail},
                    )
                else:
                    evt["kind"] = "ORDER_REJECT"
                    evt["details"] = detail
                    rejects += 1
                    _finalize_ledger_safe(
                        ledger,
                        ledger_key=ledger_key,
                        run_id=run_id_for_ledger,
                        status="REJECT",
                        result={"reject": detail},
                    )

                _emit_event(fout, evt)

                status_events += _emit_status_event_if_present(
                    fout,
                    plan=plan,
                    exec_id=exec_id,
                    ledger_key=ledger_key,
                    ibkr_meta={"via": "ibkr_sender_real_v1"},
                    detail=detail,
                )

                evt_done = _build_event_base(plan, exec_id=exec_id)
                evt_done.update(
                    {
                        "kind": "ORDER_DONE",
                        "ledger_key": ledger_key,
                        "ibkr": {"via": "ibkr_sender_real_v1"},
                        "details": {"ok": bool(ok)},
                    }
                )
                _emit_event(fout, evt_done)
                continue

            # 2) Fallback: direct ibapi
            try:
                _ensure_connected()
                assert ib_app is not None

                plan_order_id = plan.get("ibkr_order_id") or plan.get("order_id")
                oid = _as_int(plan_order_id)
                order_id = int(oid) if oid is not None else ib_app.next_order_id()

                contract_obj = _dict_to_ib_contract(contract_dict)
                order_obj = _dict_to_ib_order(order_dict)

                ok, detail = ib_app.place_order(
                    order_id=order_id, contract_obj=contract_obj, order_obj=order_obj
                )
                if isinstance(detail, dict):
                    detail = decorate_ibkr_error_details(detail)

                # Ensure our dict-level sanitize summary is present in detail
                if isinstance(detail, dict) and order_dict_san_applied:
                    detail = dict(detail)
                    detail.setdefault("order_sanitize_applied", order_dict_san_applied)

                evt = _build_event_base(plan, exec_id=exec_id)
                evt["ibkr"] = {"via": "ibapi_direct", "order_id": int(order_id)}
                evt["ledger_key"] = ledger_key

                if ok:
                    evt["kind"] = "ORDER_ACK"
                    evt["details"] = detail
                    acks += 1
                    _finalize_ledger_safe(
                        ledger,
                        ledger_key=ledger_key,
                        run_id=run_id_for_ledger,
                        status="ACK",
                        result={"ack": detail},
                    )
                else:
                    evt["kind"] = "ORDER_REJECT"
                    evt["details"] = detail
                    rejects += 1
                    _finalize_ledger_safe(
                        ledger,
                        ledger_key=ledger_key,
                        run_id=run_id_for_ledger,
                        status="REJECT",
                        result={"reject": detail},
                    )

                _emit_event(fout, evt)

                status_events += _emit_status_event_if_present(
                    fout,
                    plan=plan,
                    exec_id=exec_id,
                    ledger_key=ledger_key,
                    ibkr_meta={"via": "ibapi_direct", "order_id": int(order_id)},
                    detail=detail,
                )

                evt_done = _build_event_base(plan, exec_id=exec_id)
                evt_done.update(
                    {
                        "kind": "ORDER_DONE",
                        "ledger_key": ledger_key,
                        "ibkr": {"via": "ibapi_direct", "order_id": int(order_id)},
                        "details": {"ok": bool(ok)},
                    }
                )
                _emit_event(fout, evt_done)

            except Exception as e:
                evt = _build_event_base(plan, exec_id=exec_id)
                evt.update(
                    {
                        "kind": "ORDER_REJECT",
                        "reason": "executor_exception",
                        "ledger_key": ledger_key,
                        "details": {"error": repr(e)},
                    }
                )
                _emit_event(fout, evt)
                rejects += 1
                _finalize_ledger_safe(
                    ledger,
                    ledger_key=ledger_key,
                    run_id=run_id_for_ledger,
                    status="ERROR",
                    result={"error": {"type": type(e).__name__, "msg": str(e)}},
                )

                evt_done = _build_event_base(plan, exec_id=exec_id)
                evt_done.update(
                    {
                        "kind": "ORDER_DONE",
                        "ledger_key": ledger_key,
                        "details": {"ok": False, "exception": type(e).__name__},
                    }
                )
                _emit_event(fout, evt_done)

    if ib_app is not None:
        try:
            ib_app.disconnect()
        except Exception:
            pass

    ok_final = (rejects == 0) and (parse_errors == 0)
    exit_code = 0 if ok_final else 1

    summary: Dict[str, Any] = {
        "schema": SCHEMA,
        "ts_utc": ts_start,
        "ok": bool(ok_final),
        "exit_code": int(exit_code),
        "exec_id": exec_id,
        "execution_mode": exec_mode,
        "cli_execute": bool(cli_execute),
        "prefer_sender_real": bool(prefer_sender_real),
        "sendplan_path": str(sendplan_path),
        "out_path": str(out_path),
        "written": {"events": str(out_path)},
        "ledger": {
            "enabled": bool(ledger is not None),
            "path": ledger_path_resolved,
            "allow_retry": bool(ledger_allow_retry),
        },
        "reconcile": {
            "requested": bool(reconcile_requested),
            "enabled": bool(reconcile_enabled),
            "empty_ok": bool(reconcile_empty_ok),
            "snapshot_path": snapshot_used,
            "snapshot_age_s": snapshot_age_s,
            "snapshot_max_age_s": snapshot_max_age_s,
            "snapshot_parse_errors": snapshot_parse_errors,
            "block_reason": reconcile_block_all_reason,
        },
        "conn": {
            "host": conn.host,
            "port": conn.port,
            "client_id": conn.client_id,
            "timeout_s": conn.timeout_s,
        },
        "counts": {
            "plans_seen": plans_seen,
            "plans_actionable": plans_actionable,
            "orders_attempted": orders_attempted,
            "orders_skipped": orders_skipped,
            "orders_skipped_guard": orders_skipped_guard,
            "orders_skipped_duplicate": orders_skipped_duplicate,
            "orders_skipped_reconcile": orders_skipped_reconcile,
            "acks": acks,
            "rejects": rejects,
            "order_status_events": status_events,
            "parse_errors": parse_errors,
        },
        "notes": [
            "P1.3B: sanitize_order_v0 applied before placeOrder (direct ibapi path).",
            "P1.3B: nbboPriceCap is removed if present in Order.__dict__ (sanitize_order_v0).",
            "P1.3B: IBKR code 399 treated as WARNING (not reject).",
            "INFO codes (2104/2106/2158) are collected as sys messages (not reject).",
        ],
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
