from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


def _u(x: Any) -> str:
    return str(x or "").upper().strip()


def _as_int(x: Any, default: int) -> int:
    try:
        v = int(x)
        return v if v > 0 else default
    except Exception:
        return default


def build_contract_ref(contract_meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a minimal IBKR contract reference (JSON payload style).
    Expects dict like meta["contract"] from hg_5m_bars_ibkr.meta.json or run_report.inputs.csv_meta.contract.
    """
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


def wa_action_to_order_fields(wa_action: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Convert WA action dict -> IBKR-ish order fields (still dry-run).
    If cannot map safely -> (None, reason).
    """

    # 1) Extract action / side
    # Accept common variants:
    # - wa_action["side"] in {"BUY","SELL"}
    # - wa_action["ibkr_action"] in {"BUY","SELL"}
    # - wa_action["action"] in {"BUY","SELL","ENTER_LONG","ENTER_SHORT"}
    side = _u(wa_action.get("side") or wa_action.get("ibkr_action") or "")
    act = _u(wa_action.get("action") or wa_action.get("type") or wa_action.get("kind") or "")

    if side not in {"BUY", "SELL"}:
        if act in {"BUY", "SELL"}:
            side = act
        elif act in {"ENTER_LONG", "LONG"}:
            side = "BUY"
        elif act in {"ENTER_SHORT", "SHORT"}:
            side = "SELL"

    if side not in {"BUY", "SELL"}:
        # If WA is NOOP or not explicit, refuse to map.
        return None, "AMBIGUOUS_WA_ACTION"

    # 2) Quantity
    qty = wa_action.get("qty")
    if qty is None:
        qty = wa_action.get("quantity")
    if qty is None:
        qty = wa_action.get("size")
    qty_i = _as_int(qty, default=1)

    # 3) Order type
    order_type = _u(wa_action.get("orderType") or wa_action.get("order_type") or wa_action.get("ord_type") or "")
    if not order_type:
        order_type = "MKT"

    order: Dict[str, Any] = {
        "action": side,                 # BUY/SELL
        "orderType": order_type,        # MKT/LMT etc
        "totalQuantity": qty_i,
        "tif": _u(wa_action.get("tif") or "DAY"),
        # Safety defaults:
        "transmit": False,
    }

    # 4) Limit price if LMT
    if order_type == "LMT":
        lp = wa_action.get("lmtPrice") or wa_action.get("limit_price") or wa_action.get("price")
        if lp is None:
            return None, "LMT_MISSING_PRICE"
        order["lmtPrice"] = lp

    return order, None


def intent_to_payload(
    intent: Dict[str, Any],
    contract_ref: Dict[str, Any],
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Convert a single OrderIntent dict -> payload dict.
    Does NOT send anything.
    """
    kind = str(intent.get("kind") or "")
    run_id = intent.get("run_id")
    idx = intent.get("index")
    ts = intent.get("ts")
    ma_dec = intent.get("ma_decision")
    wa_action = intent.get("wa_action")
    if not isinstance(wa_action, dict):
        wa_action = {}

    if kind == "INTENT_CANCEL_ALL":
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

    if kind == "INTENT_NONE":
        return {
            "payload_kind": "PAYLOAD_NONE",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
            "reason": intent.get("reason") or "NO_ACTION",
        }

    if kind != "INTENT_ORDER":
        return {
            "payload_kind": "PAYLOAD_NONE",
            "dry_run": dry_run,
            "run_id": run_id,
            "index": idx,
            "ts": ts,
            "contract": contract_ref,
            "order": None,
            "reason": f"UNKNOWN_INTENT_KIND:{kind}",
        }

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
            "notes": {"ma_decision": ma_dec, "wa_action": wa_action},
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
        "notes": {"ma_decision": ma_dec},
    }
