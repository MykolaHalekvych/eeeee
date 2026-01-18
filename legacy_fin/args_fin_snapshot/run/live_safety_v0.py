from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


# -----------------------------
# Live-Safety v0 (runtime guardrails)
# BEFORE MA and BEFORE WA
# Deterministic, fail-closed on invalid/missing fields.
# -----------------------------

MAX_DRIFT_MS = 10_000
ALLOWED_QC = {"PASS"}  # canonical QC: PASS/FAIL


@dataclass(frozen=True)
class SafetyDecision:
    decision: str  # "ALLOW", "NO_DECISION", "HALT"
    reason: str
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        # defensive copy to avoid external mutation
        return {
            "decision": self.decision,
            "reason": self.reason,
            "details": dict(self.details),
        }


def _get_dict(
    parent: Dict[str, Any], key: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    v = parent.get(key)
    if v is None:
        return None, f"missing:{key}"
    if not isinstance(v, dict):
        return None, f"invalid_type:{key}:{type(v).__name__}"
    return v, None


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


def evaluate_live_safety(ma_input: Dict[str, Any]) -> SafetyDecision:
    """
    Rules v0 (fail-closed):
    1) exec.kill_switch == True -> HALT
    2) abs(data.timestamp_drift_ms) > MAX_DRIFT_MS -> NO_DECISION
    3) data.missing_bars > 0 -> NO_DECISION
    4) data.qc must be PASS -> otherwise NO_DECISION
    5) Otherwise -> ALLOW

    Notes:
    - Any missing/invalid required fields => NO_DECISION (UNKNOWN => forbid).
    - kill_switch type must be bool; otherwise NO_DECISION (do not guess).
    """
    if not isinstance(ma_input, dict):
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Invalid MA input (expected dict).",
            details={"path": "<root>", "type": type(ma_input).__name__},
        )

    exec_block, exec_err = _get_dict(ma_input, "exec")
    if exec_block is None:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Invalid exec block.",
            details={"path": "exec", "error": exec_err},
        )

    ks = exec_block.get("kill_switch", None)
    if not isinstance(ks, bool):
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Invalid kill_switch type (expected bool).",
            details={
                "path": "exec.kill_switch",
                "value": ks,
                "type": type(ks).__name__,
            },
        )

    if ks:
        return SafetyDecision(
            decision="HALT",
            reason="Kill-switch engaged.",
            details={"path": "exec.kill_switch", "value": True},
        )

    data, data_err = _get_dict(ma_input, "data")
    if data is None:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Invalid data block.",
            details={"path": "data", "error": data_err},
        )

    # 2) time sanity (abs drift)
    if "timestamp_drift_ms" not in data:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Missing timestamp drift field.",
            details={"path": "data.timestamp_drift_ms"},
        )
    drift = _to_int(data.get("timestamp_drift_ms"))
    if drift is None:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Invalid timestamp drift value.",
            details={
                "path": "data.timestamp_drift_ms",
                "value": data.get("timestamp_drift_ms"),
            },
        )
    if abs(drift) > MAX_DRIFT_MS:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Timestamp drift too large.",
            details={
                "path": "data.timestamp_drift_ms",
                "value": drift,
                "max_abs": MAX_DRIFT_MS,
            },
        )

    # 3) missing bars
    if "missing_bars" not in data:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Missing bars field.",
            details={"path": "data.missing_bars"},
        )
    missing = _to_int(data.get("missing_bars"))
    if missing is None:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Invalid missing_bars value.",
            details={"path": "data.missing_bars", "value": data.get("missing_bars")},
        )
    if missing > 0:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Missing bars detected.",
            details={"path": "data.missing_bars", "value": missing},
        )

    # 4) qc
    if "qc" not in data:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Missing data.qc field.",
            details={"path": "data.qc"},
        )
    qc = str(data.get("qc", "")).strip().upper()
    if qc not in ALLOWED_QC:
        return SafetyDecision(
            decision="NO_DECISION",
            reason="Data quality not PASS.",
            details={"path": "data.qc", "value": qc, "allowed": sorted(ALLOWED_QC)},
        )

    return SafetyDecision(decision="ALLOW", reason="Safety gate passed.", details={})
