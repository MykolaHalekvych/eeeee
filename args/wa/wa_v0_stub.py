from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class WAAction:
    action: str               # "NOOP", "CLOSE", "REDUCE", "ALLOW"
    reason: str
    notes: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "notes": self.notes}


def decide_wa_action(ma_decision: Optional[str], violations: List[Dict[str, Any]]) -> WAAction:
    """
    Deterministic WA v0 action mapping:
    - UNKNOWN / NO_TRADE -> NOOP
    - EXIT -> CLOSE
    - REDUCE -> REDUCE
    - ALLOW -> ALLOW
    - BAN -> NOOP (explicitly do nothing)
    """
    d = (ma_decision or "UNKNOWN").strip().upper()

    # include a compact reason summary
    v_ids = []
    for v in violations or []:
        if isinstance(v, dict) and "rule_id" in v:
            v_ids.append(str(v.get("rule_id")))
    v_ids = v_ids[:10]

    if d in ("UNKNOWN", "NO_TRADE"):
        return WAAction(
            action="NOOP",
            reason="MA disallows trading (UNKNOWN/NO_TRADE).",
            notes={"ma_decision": d, "violation_rule_ids": v_ids},
        )

    if d == "EXIT":
        return WAAction(
            action="CLOSE",
            reason="MA requests EXIT (close position).",
            notes={"ma_decision": d, "violation_rule_ids": v_ids},
        )

    if d == "REDUCE":
        return WAAction(
            action="REDUCE",
            reason="MA requests REDUCE (de-risk position).",
            notes={"ma_decision": d, "violation_rule_ids": v_ids},
        )

    if d == "ALLOW":
        return WAAction(
            action="ALLOW",
            reason="MA allows trading (subject to WA execution policies).",
            notes={"ma_decision": d, "violation_rule_ids": v_ids},
        )

    if d == "BAN":
        return WAAction(
            action="NOOP",
            reason="MA bans trading (BAN).",
            notes={"ma_decision": d, "violation_rule_ids": v_ids},
        )

    # fallback safe
    return WAAction(
        action="NOOP",
        reason="Unknown MA decision; default to NOOP.",
        notes={"ma_decision": d, "violation_rule_ids": v_ids},
    )
