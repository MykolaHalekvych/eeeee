from __future__ import annotations

from dataclasses import dataclass

from .control_plane_v0 import ControlPlane


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def is_exit_order(position_qty: float, side: str, qty: float) -> bool:
    side = side.upper()
    pos = float(position_qty)
    q = float(qty)
    if q <= 0:
        return False
    if pos > 0 and side == "SELL" and q <= pos:
        return True
    if pos < 0 and side == "BUY" and q <= abs(pos):
        return True
    return False


def would_increase_exposure(position_qty: float, side: str, qty: float) -> bool:
    side = side.upper()
    pos = float(position_qty)
    q = float(qty)
    if q <= 0:
        return False
    delta = q if side == "BUY" else -q
    new_pos = pos + delta
    return abs(new_pos) > abs(pos)


def check_order_allowed(
    cp: ControlPlane,
    *,
    symbol: str,
    side: str,
    qty: float,
    position_qty: float,
    action: str,  # PLACE/CANCEL/REPLACE
) -> PolicyDecision:
    sym = symbol.upper().strip()
    act = action.upper().strip()
    side_u = side.upper().strip()

    if act == "CANCEL":
        return PolicyDecision(True, "cancel_always_allowed")

    if cp.global_mode.upper() == "ONLY_EXITS":
        if is_exit_order(position_qty, side_u, qty):
            return PolicyDecision(True, "only_exits_exit_allowed")
        return PolicyDecision(False, "only_exits_forbids_entry_increase_or_flip")

    # NORMAL
    if cp.allowlist:
        allow = {s.upper() for s in cp.allowlist}
        if sym not in allow and not is_exit_order(position_qty, side_u, qty):
            return PolicyDecision(False, "symbol_not_in_allowlist")

    if act == "REPLACE" and would_increase_exposure(position_qty, side_u, qty):
        return PolicyDecision(False, "replace_would_increase_exposure")

    return PolicyDecision(True, "allowed")
