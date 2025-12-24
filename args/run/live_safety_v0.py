from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class SafetyDecision:
    decision: str   # "ALLOW", "NO_DECISION", "HALT"
    reason: str
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"decision": self.decision, "reason": self.reason, "details": self.details}


def evaluate_live_safety(ma_input: Dict[str, Any]) -> SafetyDecision:
    """
    Deterministic live-safety gate (runtime guardrails).
    This layer is BEFORE MA and BEFORE WA.

    Rules v0:
    1) If exec.kill_switch == True -> HALT (hard stop)
    2) If data.timestamp_drift_ms > 10000 -> NO_DECISION
    3) If data.missing_bars > 0 -> NO_DECISION
    4) If data.qc == "FAIL" -> NO_DECISION
    5) Otherwise -> ALLOW
    """
    # 1) kill-switch
    try:
        ks = bool(ma_input.get("exec", {}).get("kill_switch", False))
    except Exception:
        ks = False

    if ks:
        return SafetyDecision(
            decision="HALT",
            reason="Kill-switch engaged.",
            details={"path": "exec.kill_switch", "value": True},
        )

    # 2) time sanity
    drift = None
    try:
        drift = int(ma_input.get("data", {}).get("timestamp_drift_ms", 0))
    except Exception:
        drift = 0

    if drift > 10_000:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Timestamp drift too large.",
            details={"path": "data.timestamp_drift_ms", "value": drift, "max": 10_000},
        )

    # 3) missing bars
    try:
        missing = int(ma_input.get("data", {}).get("missing_bars", 0))
    except Exception:
        missing = 0

    if missing > 0:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Missing bars detected.",
            details={"path": "data.missing_bars", "value": missing},
        )

    # 4) qc fail
    qc = str(ma_input.get("data", {}).get("qc", "")).strip().upper()
    if qc == "FAIL":
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Data quality FAIL.",
            details={"path": "data.qc", "value": qc},
        )

    return SafetyDecision(decision="ALLOW", reason="Safety gate passed.", details={})
