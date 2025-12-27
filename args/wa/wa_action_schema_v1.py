from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

SCHEMA_VERSION = "wa_action_schema_v1"

_ALLOWED_SIDES = {"BUY", "SELL"}
_ALLOWED_ORD_TYPES = {"MKT", "LMT"}

# Treat these as "order intents" in v1
_ORDER_INTENT_KINDS = {"INTENT_ORDER", "INTENT_ENTRY"}


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _as_int(x: Any, default: int = 1) -> int:
    try:
        v = int(x)
        return v if v > 0 else default
    except Exception:
        return default


def _is_nonempty_dict(x: Any) -> bool:
    return isinstance(x, dict) and any(str(k).strip() for k in x.keys())


@dataclass(frozen=True)
class WAActionV1:
    side: str                 # BUY/SELL
    qty: int                  # positive int
    orderType: str            # MKT/LMT
    tif: str                  # DAY/GTC etc
    reason: str               # audit
    schema: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        # Provide both "side" and "action" for max compatibility
        return {
            "schema": self.schema,
            "side": self.side,
            "action": self.side,  # BUY/SELL (compat)
            "qty": int(self.qty),
            "orderType": self.orderType,
            "tif": self.tif,
            "reason": self.reason,
        }


def _read_test_spec(control_state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    control_state.json optional test hook:
      {
        "wa_test": {
          "enabled": true,
          "side": "BUY"|"SELL",
          "qty": 1,
          "orderType": "MKT"|"LMT",
          "tif": "DAY",
          "reason": "..."
        }
      }
    """
    if not isinstance(control_state, dict):
        return None
    wa_test = control_state.get("wa_test")
    if not isinstance(wa_test, dict):
        return None
    if wa_test.get("enabled") is not True:
        return None
    return wa_test


def _coerce_existing_wa_action(a: Dict[str, Any]) -> Tuple[Optional[WAActionV1], str]:
    """
    If an existing wa_action already carries order fields (side/qty/orderType/tif),
    normalize it into WAActionV1.
    """
    side = _u(a.get("side") or a.get("action") or a.get("ibkr_action") or "")
    if side not in _ALLOWED_SIDES:
        return None, "EXISTING_MISSING_SIDE"

    qty = _as_int(a.get("qty") if a.get("qty") is not None else (a.get("quantity") if a.get("quantity") is not None else a.get("size")), default=1)
    ot = _u(a.get("orderType") or a.get("order_type") or a.get("ord_type") or "MKT")
    if ot not in _ALLOWED_ORD_TYPES:
        ot = "MKT"
    tif = _u(a.get("tif") or "DAY") or "DAY"

    return WAActionV1(side=side, qty=qty, orderType=ot, tif=tif, reason="KEEP_EXISTING"), "OK_KEEP_EXISTING"


def derive_wa_action_v1(
    intent: Dict[str, Any],
    *,
    mode: str,
    position_size: float,
    control_state: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[WAActionV1], str]:
    """
    Deterministic mapping intent -> WAActionV1.

    Rules:
    - If mode != ALLOW_NEW_ENTRIES => None
    - Accept kind in {INTENT_ENTRY, INTENT_ORDER} as order intents
    - If control_state.wa_test.enabled => return test action
    - Else: if existing intent.wa_action already has side/qty/orderType => keep it (normalize)
    - Else: no strategy yet => None
    """
    m = _u(mode).replace("-", "_")
    kind = _u(intent.get("kind"))

    if m != "ALLOW_NEW_ENTRIES":
        return None, f"MODE_BLOCKS:{m}"

    if kind not in _ORDER_INTENT_KINDS:
        return None, f"NOT_ORDER_INTENT:{kind or 'EMPTY'}"

    # 1) Control-plane test hook
    test = _read_test_spec(control_state)
    if test is not None:
        side = _u(test.get("side"))
        if side not in _ALLOWED_SIDES:
            return None, f"TEST_INVALID_SIDE:{side or 'EMPTY'}"
        qty = _as_int(test.get("qty"), default=1)
        ot = _u(test.get("orderType") or "MKT")
        if ot not in _ALLOWED_ORD_TYPES:
            ot = "MKT"
        tif = _u(test.get("tif") or "DAY") or "DAY"
        reason = str(test.get("reason") or "WA_TEST_SPEC")
        return WAActionV1(side=side, qty=qty, orderType=ot, tif=tif, reason=reason), "OK_TEST"

    # 2) Keep existing wa_action if it already contains a tradable side
    wa = intent.get("wa_action")
    if isinstance(wa, dict) and _is_nonempty_dict(wa):
        act, why = _coerce_existing_wa_action(wa)
        if act is not None:
            return act, why

    # 3) No direction/strategy available yet
    return None, "NO_DIRECTION_NO_TEST"


def apply_wa_action_schema_v1(
    intent: Dict[str, Any],
    *,
    mode: str,
    position_size: float,
    control_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Applies schema v1 to intent.
    CRITICAL: If schema cannot derive an action, DO NOT wipe existing intent["wa_action"].
    """
    out = dict(intent)

    action, why = derive_wa_action_v1(out, mode=mode, position_size=position_size, control_state=control_state)

    out["wa_action_schema"] = SCHEMA_VERSION
    out["wa_action_reason"] = why

    if action is None:
        # Preserve existing wa_action (do not overwrite)
        if not isinstance(out.get("wa_action"), dict):
            out["wa_action"] = {}
        return out

    out["wa_action"] = action.to_dict()
    return out
