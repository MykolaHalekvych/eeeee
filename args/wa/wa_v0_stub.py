from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# Controlled test flag (OFF by default).
# To enable: set ARGS_TEST_ENTRY_ORDER=1 in environment OR manually toggle in code.
import os
TEST_ENTRY_ORDER = os.environ.get("ARGS_TEST_ENTRY_ORDER", "").strip() in {"1", "true", "TRUE", "yes", "YES"}


@dataclass(frozen=True)
class WAAction:
    action: str  # "NO_ACTION" | "EXIT" | "REDUCE" | "ALLOW"
    reason: str
    notes: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "notes": dict(self.notes)}


def decide_wa_action(ma_decision: Optional[str], violations: List[Dict[str, Any]]) -> WAAction:
    d = (ma_decision or "UNKNOWN").strip().upper().replace("-", "_")

    v_ids: List[str] = []
    for v in violations or []:
        if isinstance(v, dict) and "rule_id" in v:
            v_ids.append(str(v.get("rule_id")))
    v_ids = v_ids[:10]

    if d in ("NO_DECISION", "UNKNOWN", "NO_TRADE"):
        return WAAction(
            action="NO_ACTION",
            reason="MA/Live-safety disallows action (NO_DECISION/UNKNOWN/NO_TRADE).",
            notes={"ma_decision": d, "violation_rule_ids": v_ids, "intent": "NONE"},
        )

    if d == "EXIT":
        return WAAction(
            action="EXIT",
            reason="MA requests EXIT (close position).",
            notes={"ma_decision": d, "violation_rule_ids": v_ids, "intent": "EXIT"},
        )

    if d == "REDUCE":
        return WAAction(
            action="REDUCE",
            reason="MA requests REDUCE (de-risk position).",
            notes={"ma_decision": d, "violation_rule_ids": v_ids, "intent": "REDUCE"},
        )

    if d == "ALLOW":
        notes: Dict[str, Any] = {"ma_decision": d, "violation_rule_ids": v_ids, "intent": "ALLOW"}

        # Controlled test-only order injection (OFF by default)
        if TEST_ENTRY_ORDER:
            notes.update(
                {
                    "__test_entry_order": True,
                    "side": "BUY",
                    "qty": 1,
                    "orderType": "MKT",
                    "tif": "DAY",
                }
            )

        return WAAction(
            action="ALLOW",
            reason="MA allows trading (subject to WA v1 action schema).",
            notes=notes,
        )

    return WAAction(
        action="NO_ACTION",
        reason="Unknown MA decision; default to NO_ACTION.",
        notes={"ma_decision": d, "violation_rule_ids": v_ids, "intent": "NONE"},
    )
