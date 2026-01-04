# args/ibkr/ibkr_order_payload_v1.py
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# Stage A: semantic intent kinds
KIND_NONE = "INTENT_NONE"
KIND_CANCEL_ALL = "INTENT_CANCEL_ALL"
KIND_ORDER = "INTENT_ORDER"          # Stage6 test / generic order intent
KIND_ENTRY = "INTENT_ENTRY"
KIND_EXIT = "INTENT_EXIT"
KIND_REDUCE = "INTENT_REDUCE"
KIND_TAKE_PROFIT = "INTENT_TAKE_PROFIT"

_ALLOWED_INTENT_KINDS = {
    KIND_NONE,
    KIND_CANCEL_ALL,
    KIND_ORDER,
    KIND_ENTRY,
    KIND_EXIT,
    KIND_REDUCE,
    KIND_TAKE_PROFIT,
}

_ALLOWED_ORDER_TYPES = {"MKT", "LMT"}  # minimal set


def _u(x: Any) -> str:
    return str(x or "").strip().upper().replace("-", "_")


def _as_pos_int(x: Any) -> Optional[int]:
    try:
        # allow float-like "1.0"
        v = int(float(x))
        return v if v > 0 else None
    except Exception:
        return None


def _as_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def build_contract_ref(contract_meta: Dict[str, Any]) -> Dict[str, Any]:
    con_id = contract_meta.get("conId")
    local = contract_meta.get("localSymbol")

    sym = contract_meta.get("symbol") or "HG"
    sec_type = contract_meta.get("secType") or "FUT"
    exch = contract_meta.get("exchange") or "COMEX"
    ccy = contract_meta.get("currency") or "USD"
    last_trade = contract_meta.get("lastTradeDateOrContractMonth")

    if con_id is None and not local:
        raise ValueError("contract_meta missing conId/localSymbol")

    return {
        "conId": con_id,
        "localSymbol": local,
        "symbol": sym,
        "secType": sec_type,
        "exchange": exch,
        "currency": ccy,
        "lastTradeDateOrContractMonth": last_trade,
        "multiplier": contract_meta.get("multiplier"),
        "tradingClass": contract_meta.get("tradingClass"),
    }


def _notes(a: Dict[str, Any]) -> Dict[str, Any]:
    n = a.get("notes")
    return n if isinstance(n, dict) else {}


def _order_spec(a: Dict[str, Any]) -> Dict[str, Any]:
    """
    Supports WA v1 shape:
      wa_action = {"action":"PLACE_ORDER", "order": {...}}
    Returns {} if not present.
    """
    o = a.get("order")
    return o if isinstance(o, dict) else {}


def _infer_side_from_wa_action(wa_action: Dict[str, Any]) -> Optional[str]:
    n = _notes(wa_action)
    o = _order_spec(wa_action)

    # 1) Prefer nested order spec
    side = _u(o.get("action") or o.get("side") or "")
    if side in {"BUY", "SELL"}:
        return side

    # 2) accept side fields on top-level or inside notes (controlled test)
    side = _u(wa_action.get("side") or wa_action.get("ibkr_action") or n.get("side"))
    if side in {"BUY", "SELL"}:
        return side

    # 3) accept action/intent markers
    act = _u(
        wa_action.get("action")
        or wa_action.get("intent")
        or wa_action.get("type")
        or wa_action.get("kind")
        or n.get("intent")
        or ""
    )

    # If action is "PLACE_ORDER", side must come from nested order; otherwise treat as unknown.
    if act in {"BUY", "SELL"}:
        return act
    if act in {"ENTER_LONG", "LONG"}:
        return "BUY"
    if act in {"ENTER_SHORT", "SHORT"}:
        return "SELL"

    return None


def _infer_qty_from_wa_action(wa_action: Dict[str, Any]) -> Optional[int]:
    n = _notes(wa_action)
    o = _order_spec(wa_action)

    # Prefer nested order spec fields
    qty = o.get("totalQuantity")
    if qty is None:
        qty = o.get("qty") or o.get("quantity") or o.get("size")

    # Fallback to top-level
    if qty is None:
        qty = wa_action.get("qty")
    if qty is None:
        qty = wa_action.get("quantity")
    if qty is None:
        qty = wa_action.get("size")
    if qty is None:
        qty = n.get("qty") or n.get("quantity") or n.get("size")

    return _as_pos_int(qty)


def _infer_order_type_from_wa_action(wa_action: Dict[str, Any]) -> Optional[str]:
    n = _notes(wa_action)
    o = _order_spec(wa_action)

    ot = _u(
        o.get("orderType")
        or o.get("order_type")
        or o.get("ord_type")
        or wa_action.get("orderType")
        or wa_action.get("order_type")
        or wa_action.get("ord_type")
        or n.get("orderType")
        or n.get("order_type")
        or ""
    )
    if not ot:
        return None
    return ot if ot in _ALLOWED_ORDER_TYPES else None


def _infer_lmt_price_from_wa_action(wa_action: Dict[str, Any]) -> Optional[float]:
    n = _notes(wa_action)
    o = _order_spec(wa_action)

    lp = (
        o.get("lmtPrice")
        or o.get("limit_price")
        or o.get("price")
        or wa_action.get("lmtPrice")
        or wa_action.get("limit_price")
        or wa_action.get("price")
        or n.get("lmtPrice")
    )
    return _as_float(lp)


def _infer_tif_from_wa_action(wa_action: Dict[str, Any]) -> str:
    n = _notes(wa_action)
    o = _order_spec(wa_action)

    tif = _u(o.get("tif") or wa_action.get("tif") or n.get("tif") or "DAY")
    return tif or "DAY"


def _infer_idempotency_key(intent: Dict[str, Any], wa_action: Dict[str, Any]) -> Optional[str]:
    ik = intent.get("idempotency_key")
    if isinstance(ik, str) and ik.strip():
        return ik.strip()
    ik2 = wa_action.get("idempotency_key")
    if isinstance(ik2, str) and ik2.strip():
        return ik2.strip()
    return None


def wa_action_to_order_fields(wa_action: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    SAFETY (pre-Level-5):
      - side must be explicit (BUY/SELL) (can be inferred from nested order.action)
      - qty must be explicit and >0
      - orderType must be explicit and valid
      - transmit MUST exist and be False at payload layer (we enforce transmit=False)
      - LMT must have price
    """
    side = _infer_side_from_wa_action(wa_action)
    if side not in {"BUY", "SELL"}:
        return None, "MISSING_SIDE"

    qty_i = _infer_qty_from_wa_action(wa_action)
    if qty_i is None:
        return None, "MISSING_OR_INVALID_QTY"

    order_type = _infer_order_type_from_wa_action(wa_action)
    if order_type is None:
        return None, "MISSING_OR_INVALID_ORDER_TYPE"

    order: Dict[str, Any] = {
        "action": side,
        "orderType": order_type,
        "totalQuantity": qty_i,
        "tif": _infer_tif_from_wa_action(wa_action),
        "transmit": False,  # hard safety at payload layer
    }

    if order_type == "LMT":
        lp_f = _infer_lmt_price_from_wa_action(wa_action)
        if lp_f is None:
            return None, "LMT_MISSING_OR_INVALID_PRICE"
        order["lmtPrice"] = lp_f

    return order, None


def intent_to_payload(intent: Dict[str, Any], contract_ref: Dict[str, Any], *, dry_run: bool = True) -> Dict[str, Any]:
    kind = _u(intent.get("kind"))
    run_id = str(intent.get("run_id") or "").strip()

    idx = intent.get("index")
    ts = intent.get("ts")
    ma_dec = intent.get("ma_decision")

    wa_action = intent.get("wa_action")
    if not isinstance(wa_action, dict):
        wa_action = {}

    ik = _infer_idempotency_key(intent, wa_action)

    if not run_id:
        return {
            "payload_kind": "PAYLOAD_NONE",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
            "idempotency_key": ik,
            "reason": "MISSING_RUN_ID",
        }

    if kind == KIND_CANCEL_ALL:
        return {
            "payload_kind": "PAYLOAD_CANCEL_ALL",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
            "idempotency_key": ik,
            "reason": intent.get("reason") or "CANCEL_ALL",
        }

    if kind == KIND_NONE or kind not in _ALLOWED_INTENT_KINDS:
        return {
            "payload_kind": "PAYLOAD_NONE",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
            "idempotency_key": ik,
            "reason": intent.get("reason") or (f"UNKNOWN_INTENT_KIND:{kind}" if kind else "NO_ACTION"),
        }

    # Controlled test / v1: allow only "entry-like" order intents.
    # Support both INTENT_ENTRY and INTENT_ORDER.
    if kind in {KIND_EXIT, KIND_REDUCE, KIND_TAKE_PROFIT}:
        return {
            "payload_kind": "PAYLOAD_NONE",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
            "idempotency_key": ik,
            "reason": f"NON_ENTRY_INTENT:{kind}",
            "notes": {"ma_decision": ma_dec},
        }

    if kind not in {KIND_ENTRY, KIND_ORDER}:
        # Any other allowed kinds default to NONE
        return {
            "payload_kind": "PAYLOAD_NONE",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
            "idempotency_key": ik,
            "reason": f"UNSUPPORTED_INTENT_FOR_ORDER:{kind}",
            "notes": {"ma_decision": ma_dec},
        }

    # ENTRY / ORDER
    order_fields, err = wa_action_to_order_fields(wa_action)
    if err or order_fields is None:
        return {
            "payload_kind": "PAYLOAD_NONE",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
            "idempotency_key": ik,
            "reason": f"ORDER_MAP_FAIL:{err}",
            "notes": {"ma_decision": ma_dec, "wa_action": wa_action, "intent_kind": kind},
        }

    return {
        "payload_kind": "PAYLOAD_ORDER",
        "dry_run": dry_run,
        "run_id": run_id,
        "index": idx,
        "ts": ts,
        "contract": contract_ref,
        "order": order_fields,
        "idempotency_key": ik,
        "reason": "OK",
        "notes": {"ma_decision": ma_dec, "intent_kind": kind},
    }

