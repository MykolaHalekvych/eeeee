from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .execution_v1 import BrokerEvent, EventType


def _get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return default


def _deep_get(d: Dict[str, Any], path: Tuple[str, ...], default: Any = None) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _to_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _to_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _s(x: Any) -> str:
    return str(x or "").strip()


def _norm_type(rec: Dict[str, Any]) -> str:
    return _s(_get(rec, "type", "event_type", "eventType", default="")).upper()


def _ts(rec: Dict[str, Any]) -> str:
    # Prefer tap timestamp; else stable placeholder.
    ts = _s(
        _get(rec, "ts_utc", "tsUtc", "ts", "ingest_ts_utc", "ingestTsUtc", default="")
    )
    return ts or "1970-01-01T00:00:00Z"


# --- ARGS: execId tagging (Stage5) ---
def _extract_exec_id(rec: Dict[str, Any]) -> str:
    """
    Best-effort extraction of IBKR execId from tap payloads.
    Supports multiple shapes:
      - {"execId": "..."}
      - {"execution": {"execId": "..."}}
      - {"data": {"execution": {"execId": "..."}}, ...}
      - {"payload": {"execution": {"execId": "..."}}, ...}
    """
    candidates = [
        ("execId",),
        ("execution", "execId"),
        ("data", "execId"),
        ("data", "execution", "execId"),
        ("payload", "execId"),
        ("payload", "execution", "execId"),
    ]
    for p in candidates:
        v = _deep_get(rec, p, default=None)
        s = _s(v)
        if s:
            return s
    return ""


def _reason_from_exec_id(exec_id: str) -> str:
    exec_id = _s(exec_id)
    return f"execId:{exec_id}" if exec_id else "execDetails"


def normalize_ibkr_record(rec: Dict[str, Any]) -> Optional[BrokerEvent]:
    """
    Normalizes IBKR tap records to BrokerEvent.

    Rules (Stage5-safe):
    - ORDER_STATUS:
        PreSubmitted/Submitted -> ACK
        Cancelled/Canceled     -> CANCELLED
        Inactive/Rejected      -> REJECT
        Filled                 -> FILL terminal marker (filled_qty=0, remaining=0)
      NOTE: we do NOT treat ORDER_STATUS filled/cumQty as delta fill to avoid double-count.
    - EXEC_DETAILS:
        shares -> FILL delta (filled_qty=shares, remaining=0 placeholder; adapter patches remaining from cache)
        reason -> "execId:<...>" if execId exists, else "execDetails"
    - ERROR:
        reject-like -> REJECT (201/202 or msg contains 'reject')
        ignore INFO codes (2104/2106/2158)
    - Generic FILL records (unit tests):
        type contains "FILL" -> FILL with provided filled/remaining
    """
    typ = _norm_type(rec)

    order_id = _to_int(_get(rec, "order_id", "orderId", "id", default=None))
    if order_id is None:
        return None

    symbol = _s(_get(rec, "symbol", "localSymbol", "ticker", default=""))
    if not symbol and isinstance(rec.get("contract"), dict):
        symbol = _s(rec["contract"].get("symbol"))

    client_order_id = _s(
        _get(
            rec,
            "client_order_id",
            "clientOrderId",
            "clientOrderID",
            default=f"oid_{order_id}",
        )
    )
    ts_utc = _ts(rec)

    status = _s(_get(rec, "status", "orderStatus", default=""))
    status_l = status.lower()

    # -------- ORDER_STATUS --------
    if typ == "ORDER_STATUS":
        remaining = _to_float(
            _get(rec, "remaining", "remaining_qty", "remainingQty", default=0.0), 0.0
        )

        if status in {"PreSubmitted", "Submitted"}:
            return BrokerEvent(
                EventType.ACK,
                order_id,
                client_order_id,
                symbol,
                0.0,
                remaining,
                None,
                ts_utc,
            )

        if status_l in {"cancelled", "canceled"}:
            return BrokerEvent(
                EventType.CANCELLED,
                order_id,
                client_order_id,
                symbol,
                0.0,
                remaining,
                "cancelled",
                ts_utc,
            )

        if status_l in {"inactive", "rejected"}:
            return BrokerEvent(
                EventType.REJECT,
                order_id,
                client_order_id,
                symbol,
                0.0,
                remaining,
                status,
                ts_utc,
            )

        if status_l == "filled":
            # Terminal marker only (no delta)
            return BrokerEvent(
                EventType.FILL,
                order_id,
                client_order_id,
                symbol,
                0.0,
                0.0,
                "filled",
                ts_utc,
            )

        return None

    # -------- EXEC_DETAILS (delta fill) --------
    if typ == "EXEC_DETAILS":
        shares = _to_float(_get(rec, "shares", default=0.0), 0.0)
        if shares <= 0:
            return None

        exec_id = _extract_exec_id(rec)
        reason = _reason_from_exec_id(exec_id)

        # remaining patched by adapter cache
        return BrokerEvent(
            EventType.FILL,
            order_id,
            client_order_id,
            symbol,
            shares,
            0.0,
            reason,
            ts_utc,
        )

    # -------- ERROR --------
    if typ in {"ERROR", "IB_ERROR"}:
        code = _to_int(_get(rec, "code", "errorCode", default=None))
        msg = _s(_get(rec, "msg", "message", "errorMsg", default=""))

        # ignore typical info
        if code in {2104, 2106, 2158}:
            return None

        if code in {201, 202}:
            return BrokerEvent(
                EventType.REJECT,
                order_id,
                client_order_id,
                symbol,
                0.0,
                0.0,
                f"ib_error:{code}:{msg}",
                ts_utc,
            )

        if code == 399 and "reject" in msg.lower():
            return BrokerEvent(
                EventType.REJECT,
                order_id,
                client_order_id,
                symbol,
                0.0,
                0.0,
                f"ib_error:{code}:{msg}",
                ts_utc,
            )

        if "reject" in msg.lower():
            return BrokerEvent(
                EventType.REJECT,
                order_id,
                client_order_id,
                symbol,
                0.0,
                0.0,
                f"ib_error:{code}:{msg}",
                ts_utc,
            )

        return None

    # -------- Generic FILL (unit tests / misc) --------
    if "FILL" in typ:
        filled = _to_float(
            _get(rec, "filled", "filled_qty", "filledQty", "cumQty", default=0.0), 0.0
        )
        remaining = _to_float(
            _get(rec, "remaining", "remaining_qty", "remainingQty", default=0.0), 0.0
        )

        # If caller provided a reason-like field, keep it; else None.
        reason = _s(_get(rec, "reason", "fill_reason", default="")) or None
        return BrokerEvent(
            EventType.FILL,
            order_id,
            client_order_id,
            symbol,
            filled,
            remaining,
            reason,
            ts_utc,
        )

    # informational
    if typ == "OPEN_ORDER":
        return None

    # fallback: status-only (rare)
    if status_l == "filled":
        return BrokerEvent(
            EventType.FILL,
            order_id,
            client_order_id,
            symbol,
            0.0,
            0.0,
            "filled",
            ts_utc,
        )
    if status_l in {"cancelled", "canceled"}:
        rem = _to_float(_get(rec, "remaining", default=0.0), 0.0)
        return BrokerEvent(
            EventType.CANCELLED,
            order_id,
            client_order_id,
            symbol,
            0.0,
            rem,
            "cancelled",
            ts_utc,
        )
    if status_l in {"inactive", "rejected"}:
        rem = _to_float(_get(rec, "remaining", default=0.0), 0.0)
        return BrokerEvent(
            EventType.REJECT,
            order_id,
            client_order_id,
            symbol,
            0.0,
            rem,
            status,
            ts_utc,
        )

    return None


def read_jsonl_events(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except json.JSONDecodeError:
                continue
    return out
