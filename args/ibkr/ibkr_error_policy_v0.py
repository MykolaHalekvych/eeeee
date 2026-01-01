# args/ibkr/ibkr_error_policy_v0.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Set

Severity = Literal["INFO", "WARNING", "ERROR"]

# Typical TWS/IBG "status/info" codes you do NOT want in errors_head:
IBKR_INFO_CODES: Set[int] = {
    2104,  # Market data farm connection is OK
    2106,  # HMDS data farm connection is OK
    2158,  # Sec-def data farm connection is OK
    1101,  # connectivity restored
    1102,  # connectivity restored (sometimes used for different subsystems)
}

# Codes that are usually non-fatal but operationally useful:
IBKR_WARNING_CODES: Set[int] = {
    399,   # order held until (outside trading hours / conditions)
    2103,  # market data farm connection is broken (often not fatal for order routing)
    2105,  # HMDS data farm connection is broken
    2157,  # sec-def data farm connection is broken
    2107,  # farm inactive but should be available
    2108,  # farm inactive but should be available
}

@dataclass(frozen=True, slots=True)
class IbkrErrorClass:
    severity: Severity
    count_as_error_head: bool
    kind: str

def classify_ibkr_error(*, req_id: Optional[int], code: int, msg: str) -> IbkrErrorClass:
    if code in IBKR_INFO_CODES:
        return IbkrErrorClass(severity="INFO", count_as_error_head=False, kind="IBKR_INFO_CODE")
    if code in IBKR_WARNING_CODES:
        return IbkrErrorClass(severity="WARNING", count_as_error_head=False, kind="IBKR_WARNING_CODE")
    return IbkrErrorClass(severity="ERROR", count_as_error_head=True, kind="IBKR_ERROR")
