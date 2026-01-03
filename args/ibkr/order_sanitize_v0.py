from __future__ import annotations

try:
    from ibapi.common import UNSET_DOUBLE  # type: ignore
except Exception:
    UNSET_DOUBLE = 1.7976931348623157e308

# Ensure serializer compatibility: some ibapi versions expect Order.nbboPriceCap to exist.
try:
    from ibapi.order import Order as _IbOrder  # type: ignore
    if not hasattr(_IbOrder, "nbboPriceCap"):
        setattr(_IbOrder, "nbboPriceCap", UNSET_DOUBLE)
except Exception:
    pass

from typing import Any, Dict

def sanitize_order_v0(o: Any) -> Dict[str, Any]:
    """
    IBKR/TWS 983+ rejects deprecated attrs:
      10268 eTradeOnly not supported
      10269 firmQuoteOnly not supported
      10270 nbboPriceCap not supported

    Safe rule:
      - force eTradeOnly=False (if present)
      - force firmQuoteOnly=False (if present)
      - NEVER set nbboPriceCap; if someone set it earlier into __dict__, remove it
    """
    applied: Dict[str, Any] = {}

    if hasattr(o, "eTradeOnly"):
        try:
            o.eTradeOnly = False
            applied["eTradeOnly"] = False
        except Exception:
            pass

    if hasattr(o, "firmQuoteOnly"):
        try:
            o.firmQuoteOnly = False
            applied["firmQuoteOnly"] = False
        except Exception:
            pass

    # critical: do not touch nbboPriceCap unless it was explicitly set into __dict__
    try:
        d = getattr(o, "__dict__", None)
        if isinstance(d, dict) and "nbboPriceCap" in d:
            d["nbboPriceCap"] = UNSET_DOUBLE
            applied["nbboPriceCap_removed"] = True
    except Exception:
        pass

    return applied


