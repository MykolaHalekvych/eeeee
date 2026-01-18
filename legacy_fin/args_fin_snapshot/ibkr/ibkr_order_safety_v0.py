# args/ibkr/ibkr_order_safety_v0.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Tuple

# Known order attrs that IB rejects on some instruments / routes when set non-default.
# We force them to "False/0" if present and truthy.
FORCE_FALSE_ATTRS: Tuple[str, ...] = (
    "eTradeOnly",
    "firmQuoteOnly",
    # defensive variants (in case a different wrapper uses them)
    "EtradeOnly",
    "FirmQuoteOnly",
    "etradeOnly",
    "firmquoteOnly",
)


def _is_truthy_nondefault(v: Any) -> bool:
    if v is None:
        return False
    if v is False:
        return False
    if v == 0:
        return False
    if v == "":
        return False
    return bool(v)


def sanitize_order_inplace(
    order: Any, *, force_false_attrs: Iterable[str] = FORCE_FALSE_ATTRS
) -> Dict[str, Tuple[Any, Any]]:
    """
    Returns dict of changed attrs: {attr: (old, new)}.
    Best-effort: if attribute can't be set, it will be skipped.
    """
    changed: Dict[str, Tuple[Any, Any]] = {}
    for attr in force_false_attrs:
        if not hasattr(order, attr):
            continue
        try:
            old = getattr(order, attr)
        except Exception:
            continue

        if not _is_truthy_nondefault(old):
            continue

        new = False  # bool(False) is acceptable; IB api often treats bool as int(0/1)
        try:
            setattr(order, attr, new)
            changed[attr] = (old, new)
        except Exception:
            # do not crash sender; we will let upstream validation decide
            continue

    return changed


@dataclass(frozen=True, slots=True)
class OrderAttrCheck:
    ok: bool
    violations: Tuple[str, ...]  # attr names that are still non-default


def validate_order_no_forbidden_truthy_attrs(
    order: Any, *, force_false_attrs: Iterable[str] = FORCE_FALSE_ATTRS
) -> OrderAttrCheck:
    violations = []
    for attr in force_false_attrs:
        if not hasattr(order, attr):
            continue
        try:
            v = getattr(order, attr)
        except Exception:
            continue
        if _is_truthy_nondefault(v):
            violations.append(attr)

    return OrderAttrCheck(ok=(len(violations) == 0), violations=tuple(violations))
