# args/wa/wa_action_schema_v1.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

SCHEMA_VERSION = "wa_action_schema_v1"

# Allowed order fields (IBKR-ish)
_ALLOWED_SIDES = {"BUY", "SELL"}
_ALLOWED_ORD_TYPES = {"MKT", "LMT"}


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _as_int(x: Any, default: int = 1) -> int:
    try:
        v = int(x)
        return v if v > 0 else default
    except Exception:
        return default


@dataclass(frozen=True)
class WAActionV1:
    side: str                 # BUY/SELL
    qty: int                  # positive int
    orderType: str            # MKT/LMT
    tif: str                  # DAY/GTC etc
    reason: str               # audit
    schema: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "side": self.side,
            "qty": int(self.qty),
            "orderType": self.orderType,
            "tif": self.tif,
            "reason": self.reason,
        }


def _extract_direction_from_intent(intent: Dict[str, Any]) -> Optional[str]:
    """
    Best-effort direction extraction.
    Returns 'LONG' / 'SHORT' / None.

    NOTE: We do not invent strategy. If direction is not explicit, return None.
    """
    for k in ("direction", "signal", "as_intent", "intent", "action", "kind_raw"):
        v = intent.get(k)
        if isinstance(v, str) and v.strip():
            s = _u(v)
            if any(x in s for x in ("ENTER_LONG", "LONG", "BUY")):
                return "LONG"
            if any(x in s for x in ("ENTER_SHORT", "SHORT", "SELL")):
                return "SHORT"
    return None


def _direction_to_side(direction: str) -> Optional[str]:
    d = _u(direction)
    if d == "LONG":
        return "BUY"
    if d == "SHORT":
        return "SELL"
    return None


def _read_test_spec(control_state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Control-plane test hook (safe-by-default).
    Expected shape:
      control_state = {
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


def _extract_existing_wa_action(intent: Dict[str, Any]) -> Optional[WAActionV1]:
    """
    PASS-THROUGH: if upstream already provided a concrete wa_action with side/qty,
    we preserve it (do NOT wipe it).
    """
    wa = intent.get("wa_action")
    if not isinstance(wa, dict) or not wa:
        return None

    # accept either side or ibkr_action
    side = _u(wa.get("side") or wa.get("ibkr_action") or "")
    if side not in _ALLOWED_SIDES:
        return None

    qty = wa.get("qty")
    if qty is None:
        qty = wa.get("quantity")
    if qty is None:
        qty = wa.get("size")
    qty_i = _as_int(qty, default=1)

    ot = _u(wa.get("orderType") or wa.get("order_type") or wa.get("ord_type") or "MKT")
    if ot not in _ALLOWED_ORD_TYPES:
        ot = "MKT"

    tif = _u(wa.get("tif") or "DAY") or "DAY"
    reason = str(wa.get("reason") or "PASSTHROUGH")

    return WAActionV1(side=side, qty=qty_i, orderType=ot, tif=tif, reason=reason)


def derive_wa_action_v1(
    intent: Dict[str, Any],
    *,
    mode: str,
    position_size: float,
    control_state: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[WAActionV1], str]:
    """
    Deterministic mapping intent -> WAAction fields.

    Rules:
    - If mode != ALLOW_NEW_ENTRIES => None
    - If intent.kind != INTENT_ORDER => None
    - If intent already has a valid wa_action (side/qty) => PASS THROUGH
    - If a test spec exists in control_state.wa_test.enabled => use it (harness only)
    - Else require explicit direction in intent (future AS v1). If missing => None
    """
    _ = position_size  # reserved for future sizing; must not affect v1 determinism

    m = _u(mode).replace("-", "_")
    kind = _u(intent.get("kind"))

    if m != "ALLOW_NEW_ENTRIES":
        return None, f"MODE_BLOCKS:{m}"

    if kind != "INTENT_ORDER":
        return None, f"NOT_INTENT_ORDER:{kind or 'EMPTY'}"

    # PASS-THROUGH: preserve upstream wa_action if already concrete
    existing = _extract_existing_wa_action(intent)
    if existing is not None:
        return existing, "OK_PASSTHROUGH"

    # Control-plane test spec (safe-by-default)
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

    # Production path (future AS v1 should populate direction)
    direction = _extract_direction_from_intent(intent)
    if not direction:
        return None, "NO_DIRECTION"

    side = _direction_to_side(direction)
    if side is None:
        return None, f"BAD_DIRECTION:{direction}"

    # v1: fixed qty=1 until sizing module exists
    return WAActionV1(side=side, qty=1, orderType="MKT", tif="DAY", reason="AS_DIRECTION"), "OK_AS"


def apply_wa_action_schema_v1(
    intent: Dict[str, Any],
    *,
    mode: str,
    position_size: float,
    control_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Returns a shallow-copied intent with:
      - intent["wa_action"] set when derivable,
      - intent["wa_action_reason"] set for audit,
      - intent["wa_action_schema"] set to schema version.

    IMPORTANT: if upstream already provided a valid wa_action, we preserve it.
    """
    out = dict(intent)

    action, why = derive_wa_action_v1(out, mode=mode, position_size=position_size, control_state=control_state)

    out["wa_action_schema"] = SCHEMA_VERSION
    out["wa_action_reason"] = why

    if action is None:
        # Do NOT invent action; also do NOT destroy a valid upstream action
        if _extract_existing_wa_action(out) is not None:
            return out
        out["wa_action"] = {}
        return out

    out["wa_action"] = action.to_dict()
    return out

