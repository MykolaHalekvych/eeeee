from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# Stage A: semantic intent kinds
KIND_NONE = "INTENT_NONE"
KIND_CANCEL_ALL = "INTENT_CANCEL_ALL"
KIND_ENTRY = "INTENT_ENTRY"
KIND_EXIT = "INTENT_EXIT"
KIND_REDUCE = "INTENT_REDUCE"
KIND_TAKE_PROFIT = "INTENT_TAKE_PROFIT"

_ALLOWED_INTENT_KINDS = {KIND_NONE, KIND_CANCEL_ALL, KIND_ENTRY, KIND_EXIT, KIND_REDUCE, KIND_TAKE_PROFIT}
_ALLOWED_ORDER_TYPES = {"MKT", "LMT"}  # minimal set


def _u(x: Any) -> str:
    return str(x or "").strip().upper().replace("-", "_")


def _as_pos_int(x: Any) -> Optional[int]:
    try:
        v = int(x)
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


def _infer_side_from_wa_action(wa_action: Dict[str, Any]) -> Optional[str]:
    n = _notes(wa_action)

    # accept side fields on top-level or inside notes (controlled test)
    side = _u(wa_action.get("side") or wa_action.get("ibkr_action") or n.get("side"))
    if side in {"BUY", "SELL"}:
        return side

    # accept action/intent markers
    act = _u(
        wa_action.get("action")
        or wa_action.get("intent")
        or wa_action.get("type")
        or wa_action.get("kind")
        or n.get("intent")
        or ""
    )

    if act in {"BUY", "SELL"}:
        return act
    if act in {"ENTER_LONG", "LONG"}:
        return "BUY"
    if act in {"ENTER_SHORT", "SHORT"}:
        return "SELL"

    return None


def _infer_qty_from_wa_action(wa_action: Dict[str, Any]) -> Optional[int]:
    n = _notes(wa_action)

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

    ot = _u(
        wa_action.get("orderType")
        or wa_action.get("order_type")
        or wa_action.get("ord_type")
        or n.get("orderType")
        or n.get("order_type")
        or ""
    )
    if not ot:
        return None
    return ot if ot in _ALLOWED_ORDER_TYPES else None


def wa_action_to_order_fields(wa_action: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    SAFETY (pre-Level-5):
      - side must be explicit (BUY/SELL)
      - qty must be explicit and >0
      - orderType must be explicit and valid
      - transmit MUST exist and be False at payload layer
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
        "tif": _u(wa_action.get("tif") or _notes(wa_action).get("tif") or "DAY"),
        "transmit": False,
    }

    if order_type == "LMT":
        lp = wa_action.get("lmtPrice") or wa_action.get("limit_price") or wa_action.get("price") or _notes(wa_action).get("lmtPrice")
        lp_f = _as_float(lp)
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

    if not run_id:
        return {
            "payload_kind": "PAYLOAD_NONE",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
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
            "reason": intent.get("reason") or (f"UNKNOWN_INTENT_KIND:{kind}" if kind else "NO_ACTION"),
        }

    # For controlled test we generate orders only for ENTRY
    if kind in {KIND_EXIT, KIND_REDUCE, KIND_TAKE_PROFIT}:
        return {
            "payload_kind": "PAYLOAD_NONE",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
            "reason": f"NON_ENTRY_INTENT:{kind}",
            "notes": {"ma_decision": ma_dec},
        }

    # ENTRY
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
        "reason": "OK",
        "notes": {"ma_decision": ma_dec, "intent_kind": kind},
    }
