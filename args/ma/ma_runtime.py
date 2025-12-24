"""
MA runtime (Management / Risk Gate) for ARGS Core.

Key properties:
- Deterministic: same input => same output
- No side-effects
- Evaluates ALL rules (no short-circuit), collects violations, then decides
- UNKNOWN is valid and safety-first
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping

from args.ma.policy_loader import Policy
from args.ma.rule_engine import eval_expr


DECISION_PRIORITY = {
    "ALLOW": 0,
    "REDUCE": 1,
    "NO_TRADE": 2,
    "UNKNOWN": 3,
    "EXIT": 4,
}

BLOCK_ORDER = [
    "hard_gates",
    "margin_gates",
    "tail_risk_gates",
    "liquidity_gates",
    "correlation_gates",
    "kill_switch.triggers",
]


@dataclass(frozen=True)
class Violation:
    rule_id: str
    block: str
    reason: str
    decision: str
    enforce: List[str]


def _get_by_path(ctx: Mapping[str, Any], path: str) -> bool:
    cur: Any = ctx
    for part in path.split("."):
        if not isinstance(cur, Mapping):
            return False
        if part not in cur:
            return False
        cur = cur[part]
    return True


def _require_fields_gate(ctx: Mapping[str, Any]) -> List[Violation]:
    required_paths = [
        "instrument",
        "timeframe",
        "env",
        "data.qc",
        "state.regime",
        "state.confidence",
        "risk.margin_usage",
    ]
    missing = [p for p in required_paths if not _get_by_path(ctx, p)]
    if not missing:
        return []
    return [
        Violation(
            rule_id="required_fields_gate",
            block="hard_gates",
            reason=f"Missing required fields: {', '.join(missing)}",
            decision="UNKNOWN",
            enforce=["NO_TRADE"],
        )
    ]


def _strictest(decisions: List[str], default: str = "UNKNOWN") -> str:
    best = default
    best_p = DECISION_PRIORITY.get(best, -1)
    for d in decisions:
        p = DECISION_PRIORITY.get(d, -1)
        if p > best_p:
            best = d
            best_p = p
    return best


def eval_ma(policy: Policy, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate MA policy on ctx and return:
    {
      "ma_decision": str,
      "violations": list[dict],
      "risk_envelope": dict
    }
    """
    violations: List[Violation] = []
    decision_votes: List[str] = []
    enforce_flags: List[str] = []

    # 0) Required fields gate (built-in hard gate)
    rf_violations = _require_fields_gate(ctx)
    for v in rf_violations:
        violations.append(v)
        decision_votes.append(v.decision)
        for e in v.enforce:
            decision_votes.append(e)
            enforce_flags.append(e)

    # 1) Policy rule blocks in fixed order (evaluate all rules)
    for block_name in BLOCK_ORDER:
        rules = policy.blocks.get(block_name, [])
        if not isinstance(rules, list):
            continue

        for r in rules:
            if not isinstance(r, dict):
                continue
            if r.get("enabled", True) is False:
                continue

            when = r.get("when")
            if when is None:
                continue

            matched = eval_expr(when, ctx).matched
            if not matched:
                continue

            rid = str(r.get("rule_id", "")).strip() or "<missing_rule_id>"
            reason = str(r.get("reason", "")).strip() or "<missing_reason>"
            decision = str(r.get("decision", "UNKNOWN")).strip() or "UNKNOWN"
            enforce = r.get("enforce", [])
            if not isinstance(enforce, list):
                enforce = []

            enforce_clean = [str(x).strip() for x in enforce if isinstance(x, str) and x.strip()]

            v = Violation(
                rule_id=rid,
                block=block_name,
                reason=reason,
                decision=decision,
                enforce=enforce_clean,
            )
            violations.append(v)

            decision_votes.append(decision)
            for e in enforce_clean:
                decision_votes.append(e)
                enforce_flags.append(e)

    # 2) Final decision
    if decision_votes:
        ma_decision = _strictest(decision_votes, default="ALLOW")
    else:
        ma_decision = "UNKNOWN"

    enforced_no_trade = ("NO_TRADE" in enforce_flags) or (ma_decision in {"UNKNOWN", "NO_TRADE", "EXIT"})

    risk_envelope = {
        "limits": dict(policy.risk_limits),
        "enforced_no_trade": enforced_no_trade,
    }

    return {
        "ma_decision": ma_decision,
        "violations": [
            {
                "rule_id": v.rule_id,
                "block": v.block,
                "reason": v.reason,
                "decision": v.decision,
                "enforce": list(v.enforce),
            }
            for v in violations
        ],
        "risk_envelope": risk_envelope,
    }
